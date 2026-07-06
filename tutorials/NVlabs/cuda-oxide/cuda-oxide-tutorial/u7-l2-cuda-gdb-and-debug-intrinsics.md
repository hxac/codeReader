# cuda-gdb 调试与设备端 debug intrinsics

## 1. 本讲目标

内核在 GPU 上跑飞了、算错了、甚至把整个 launch 拉成「cudaErrorLaunchFailure」，而宿主端只看到一个迟到的同步错误——这是 GPU 编程里最让人抓狂的调试场景。本讲教你用 cuda-oxide 自带的「设备端 debug intrinsics」与 `cargo oxide debug` 工具链把这种黑盒打开。学完后你应当能够：

- 用 `cargo oxide debug` 一条命令把示例编译成带调试信息的二进制并拉起 cuda-gdb，并知道 #330 之后它如何精确发现要调试的可执行文件。
- 在内核里用 `clock64()`/`globaltimer()` 量周期、用 `trap()` 与 `gpu_assert!` 做运行时断言、用 `breakpoint()` 给 cuda-gdb 下硬件断点、用 `prof_trigger()` 给 Nsight 打标记。
- 理解这些设备端函数为什么在源码里全是 `unreachable!()` 桩，以及它们是如何被编译器「按名字识别」最终降级成 PTX 指令的。
- 用 `#[launch_bounds(max, min)]` 给编译器占空提示，并说清它落到 PTX 的 `.maxntid` / `.minnctapersm` 的全链路。
- 用 `--bin` / `--features` 精确指定要构建与调试的目标。

## 2. 前置知识

阅读本讲前，建议你已经掌握：

- **设备桩模型**：`cuda-device` 里几乎所有的 intrinsic（warp、原子、共享内存……）都是 `#[inline(never)]` + `unreachable!()` 的空函数。它们在宿主编译时是「合法但永远跑不到」的占位符；真正的语义由 cuda-oxide 编译器在 device 流水线里**按函数全限定名识别**后改写。这是 u2-l1 与 u6-l2 反复出现的核心模式，本讲的 debug intrinsics 只是同一模式的又一族。
- **单源编译与启动的不安全性**（u1-l4 / u2-l4）：同一份 `.rs` 同时产出宿主机器码与内嵌 PTX；用 raw `LaunchConfig` 启动内核是 `unsafe` 的，需要调用方在 `SAFETY:` 注释里自证形状与资源匹配。本讲的 `debug` 示例正是这种 raw 启动风格。
- **cargo-oxide 是如何驱动一切**（u1-l3）：它是一个被 `cargo` 按约定发现的子命令，负责「找后端 `.so` → 组装 rustflags → 调 `cargo run` → 上 GPU」。本讲的 `debug` 子命令是其中专门走「带调试信息 + cuda-gdb」的一条支路。
- 一点点 cuda-gdb / gdb 的常识（断点、单步、`print`）会有帮助，但不强制。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`crates/cuda-device/src/debug.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/debug.rs) | 设备端 debug intrinsics 的「桩」定义：`clock`/`clock64`/`globaltimer`/`trap`/`breakpoint`/`prof_trigger` 与 `gpu_assert!` 宏。 |
| [`crates/cuda-device/src/thread.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/thread.rs) | `#[launch_bounds]` 在设备端的「标记函数」`__launch_bounds_config`。 |
| [`crates/rustc-codegen-cuda/examples/debug/src/main.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/debug/src/main.rs) | 把上面这些 intrinsic 全部串起来演示的端到端示例，是本讲代码实践的主战场。 |
| [`crates/cargo-oxide/src/main.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/main.rs) | `Debug` 子命令的 CLI 定义与分发。 |
| [`crates/cargo-oxide/src/commands.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/commands.rs) | `codegen_debug` 的全部实现：发现 cuda-gdb、构建带调试信息的二进制、拉起调试器，以及 `--bin`/`--features` 与可执行发现。 |
| [`crates/mir-importer/src/translator/body.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/body.rs) | mir-importer 扫描 `__launch_bounds_config` 标记调用，把它转成函数属性。 |
| [`crates/mir-lower/src/convert/intrinsics/debug.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/debug.rs) | 把 debug dialect op 降级成 LLVM intrinsic 或内联 PTX 的转换器。 |

## 4. 核心概念与源码讲解

### 4.1 cargo oxide debug → cuda-gdb

#### 4.1.1 概念说明

`cuda-gdb` 是 NVIDIA 对 GNU gdb 的扩展，能让你在 GPU 源码级别单步、查看设备变量、按线程/块切换焦点。但它有两个使用门槛：一是它本身要带调试信息（DWARF）的宿主二进制 + 对应的 PTX；二是 cuda-oxide 的构建流程是「特殊编译的编译器」，普通 `cargo run` 拉不起来。`cargo oxide debug` 子命令就是把这两件事打包：它复用正常的 codegen 后端构建，再叠加 `-C debuginfo=2`，然后定位到产物可执行文件并拉起 cuda-gdb（或 cgdb / TUI 前端）。

#### 4.1.2 核心流程

`debug` 子命令的整体流程：

1. 解析 CLI（`Debug` 变体，含 `example`/`arch`/`features`/`bin`/`cgdb`/`tui`）。
2. **发现 cuda-gdb**：先用 `find_cuda_toolkit_executable` 沿 `PATH` → CUDA toolkit root → 常见安装路径查找。
3. 解析示例目录、自动探测本机 GPU 架构（`detect_run_target_arch`，让生成的模块能在本机 GPU 上加载）。
4. 清理生成物、`touch main.rs` 触发重编。
5. 组装 `cargo build --release` 命令，叠加：codegen 后端的 rustflags、`CARGO_PROFILE_RELEASE_DEBUG=2`、设备架构 hint、`LD_LIBRARY_PATH`。
6. **用 Cargo 的 `compiler-artifact` JSON 精确发现产物可执行文件**（#330 的核心改进）。
7. 打印一份 cuda-gdb 速查表，然后 `Command::new(cuda_gdb).arg(binary)` 接管终端。

#### 4.1.3 源码精读

`Debug` 子命令的 CLI 定义在 main.rs，注意本轮 #330 补齐的 `--features` 与 `--bin` 两个旗标（[`crates/cargo-oxide/src/main.rs:207-229`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/main.rs#L207-L229)）：

```rust
/// Build with debug info and launch cuda-gdb
Debug {
    example: Option<String>,
    #[arg(long)]
    arch: Option<String>,
    /// Cargo features to enable
    #[arg(long)]
    features: Option<String>,
    /// Specific binary target to build and debug
    #[arg(long)]
    bin: Option<String>,
    #[arg(long)]
    cgdb: bool,
    #[arg(long)]
    tui: bool,
},
```

分发处把所有字段透传给 `codegen_debug`（[`crates/cargo-oxide/src/main.rs:474-493`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/main.rs#L474-L493)）。`codegen_debug` 的开头先用 toolkit root 解析 cuda-gdb（[`crates/cargo-oxide/src/commands.rs:1964-1981`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/commands.rs#L1964-L1981)），找不到就给出设置 `PATH` / `CUDA_TOOLKIT_PATH` 的提示后退出。

发现 cuda-gdb 的查找函数是「`PATH` → toolkit root/bin → 硬编码回退路径」三段式（[`crates/cargo-oxide/src/commands.rs:3562-3589`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/commands.rs#L3562-L3589)）：

```rust
fn find_cuda_toolkit_executable(ctx, name, fallback_paths) -> Option<PathBuf> {
    if let Some(path) = find_executable(name, &[]) { return Some(path); }   // 1) PATH
    let toolkit = cuda_toolkit_root(/* env + project config */);
    let configured = PathBuf::from(toolkit).join("bin").join(name);          // 2) <toolkit>/bin/<name>
    if configured.exists() { return Some(configured); }
    for path in fallback_paths { /* 3) /usr/local/cuda/bin/cuda-gdb ... */ }
    None
}
```

构建命令组装的关键是叠加调试信息与 `--bin`/`--features`（[`crates/cargo-oxide/src/commands.rs:2015-2030`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/commands.rs#L2015-L2030)）：

```rust
let mut cmd = Command::new("cargo");
cmd.args(["build", "--release"]).current_dir(&example_dir);
if let Some(bin) = bin { cmd.args(["--bin", bin]); }
if let Some(features) = features { cmd.args(["--features", features]); }
apply_config_env(&mut cmd, ctx);
apply_codegen_rustflags(&mut cmd, ctx, true, &[]);
cmd.env("CARGO_PROFILE_RELEASE_DEBUG", "2");          // 关键：开 DWARF
apply_output_mode(&mut cmd, false, target_arch);
// ...
let binary = run_cargo_build_for_executable(&mut cmd, &example_dir, bin)/* ... */;
```

最后打印速查表并拉起调试器（[`crates/cargo-oxide/src/commands.rs:2054-2097`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/commands.rs#L2054-L2097)）。速查表里最有用的一条是 `set cuda break_on_launch application`——它让你在每个内核启动的入口自动停下，这是定位「内核跑飞」的第一步。

> 提示：`cargo oxide doctor` 也会顺手探测 cuda-gdb 是否可用（[commands.rs:2497-2507](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/commands.rs#L2497-L2507)），在动手 debug 前先跑一遍能省很多事。

#### 4.1.4 代码实践

1. **目标**：在没有 GPU 的机器上也能走通 `cargo oxide debug` 的「构建 + 发现 cuda-gdb」两步（拉起调试器那步可放弃）。
2. **步骤**：进入示例目录或仓库根，执行 `cargo oxide debug debug`（前一个 `debug` 是子命令，后一个是示例名）。观察输出的「Building debug with debug info...」「Launching cuda-gdb...」与速查表。
3. **需要观察的现象**：若本机装了 CUDA toolkit，会看到 cuda-gdb 被拉起；若没装，会看到 `Error: cuda-gdb not found!` 与设置 `PATH` / `CUDA_TOOLKIT_PATH` 的提示。
4. **预期结果**：能从输出复述 cuda-gdb 的三条查找路径与速查表里的至少三条命令。
5. 待本地验证（拉起调试器需 GPU 与 CUDA toolkit；纯构建可在 CI 上验证）。

#### 4.1.5 小练习与答案

- **练习**：为什么 `debug` 子命令要单独叠加 `CARGO_PROFILE_RELEASE_DEBUG=2`，而不是让你直接用 `cargo build`？
- **答案**：cuda-oxide 默认走 release profile 出设备制品；调试需要 DWARF，但 cuda-oxide 的 rustflags 注入是它自己掌控的（包括末尾的 `-Zmir-enable-passes=-JumpThreading` 等 last-one-wins 标志），用户直接 `cargo build` 拿不到正确的后端与设备流水线。`codegen_debug` 在复用整套注入的基础上额外开 `debug=2`，两者缺一不可。
- **练习**：`cgdb` 与 `--tui` 两个选项差别在哪？
- **答案**：`--cgdb` 走 `cgdb -d <cuda-gdb> <binary>`，给一个带源码窗口、vim 风按键的外部前端；`--tui` 则是直接给 cuda-gdb 传 `--tui`，用 gdb 自带的 TUI。两者都不改变底层调试器。

### 4.2 clock64 / globaltimer / trap：计时与中止

#### 4.2.1 概念说明

调试分两类需求：一类是「我想知道这段代码花了多久/跑没跑到」，对应 **计时 intrinsic**；另一类是「这里绝不应该发生，发生了就立刻停」，对应 **中止 intrinsic**。

- `clock()` / `clock64()`：读取 GPU 上每个 SM 自己的时钟计数器，单位是周期。32 位版会在约 40 亿周期回绕，长测量用 64 位版。
- `globaltimer()`：读取 PTX 的 `%globaltimer`，是一个跨 SM 一致的全局时基，适合量「跨 SM 的交互」。
- `trap()`：等价于 CUDA C++ 的 `__trap()`，任一线程执行它，整个内核立刻终止并向宿主报错。

这三者都只是给「相对测量」和「紧急刹车」用的，不是同步原语。

#### 4.2.2 核心流程

它们在源码里都是 `unreachable!()` 桩。device 流水线对它们的处理是：

```
debug::clock64()                       (Rust 调用，#[inline(never)] 桩)
   │  mir-importer 按名 "cuda_device::debug::clock64" 识别
   ▼
nvvm.read_ptx_sreg_clock64  op         (dialect-nvvm，见 intrinsics/debug.rs)
   │  mir-lower convert_clock64
   ▼
call @llvm.nvvm.read.ptx.sreg.clock64  (LLVM intrinsic)
   │  llc / NVPTX 后端
   ▼
mov.u64 %rd, %clock64;                 (PTX 指令)
```

`trap()` 走的是「内联 PTX」分支（不走 LLVM intrinsic，直接 `trap;` 汇编），因为它要无条件终止执行。

#### 4.2.3 源码精读

桩定义极其简洁，注释里直接写明了最终降级目标（[`crates/cuda-device/src/debug.rs:101-105`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/debug.rs#L101-L105)）：

```rust
#[inline(never)]
pub fn clock64() -> u64 {
    // Lowered to: call i64 @llvm.nvvm.read.ptx.sreg.clock64()
    unreachable!("clock64 called outside CUDA kernel context")
}
```

`globaltimer` 同构（[`crates/cuda-device/src/debug.rs:123-127`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/debug.rs#L123-L127)）。`trap` 的特殊之处在于返回类型是 `!`（发散），意味着调用之后控制流不会继续（[`crates/cuda-device/src/debug.rs:159-163`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/debug.rs#L159-L163)）：

```rust
#[inline(never)]
pub fn trap() -> ! {
    // Lowered to: call void @llvm.nvvm.trap()
    unreachable!("trap called outside CUDA kernel context")
}
```

降级侧的对照表最直观（[`crates/mir-lower/src/convert/intrinsics/debug.rs:8-16`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/debug.rs#L8-L16)）：`clock64`/`globaltimer` 走 LLVM intrinsic，`trap` 走 convergent 内联 PTX。`convert_trap` 只有一行实质代码（[`crates/mir-lower/src/convert/intrinsics/debug.rs:98-108`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/debug.rs#L98-L108)）：

```rust
pub(crate) fn convert_trap(ctx, rewriter, op, _) -> Result<()> {
    let void_ty = llvm_types::VoidType::get(ctx);
    inline_asm_convergent(ctx, rewriter, void_ty.into(), vec![], "trap;", "");
    rewriter.erase_operation(ctx, op);
    Ok(())
}
```

`debug` 示例里的 `clock_test` 内核展示了典型的「前后各读一次再相减」测周的模式（[`crates/rustc-codegen-cuda/examples/debug/src/main.rs:34-55`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/debug/src/main.rs#L34-L55)）：它用 `sum & 0` 这种「看似无用」的写法防止编译器把循环优化掉。`trap_test` 内核演示「任一线程看到负值就 trap」（[`crates/rustc-codegen-cuda/examples/debug/src/main.rs:91-104`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/debug/src/main.rs#L91-L104)）。

#### 4.2.4 代码实践

1. **目标**：用 `clock64()` 量一段循环的周期数，并验证 `trap()` 在条件触发时会真的让内核失败。
2. **步骤**：
   - 跑通示例：`cargo oxide run debug`，看 Test 1 打印的「Average cycles for 100 iterations」。
   - 把 `trap_test` 的输入改成含负数（例如 `vec![-1, 2, 3]`），重新 `cargo oxide run debug`，观察 Test 2。
3. **需要观察的现象**：正常输入下 `trap_test` 输出 `val * 2`；含负数输入时，`stream.synchronize()` 会返回一个 CUDA 错误（launch 失败）。
4. **预期结果**：能解释「trap 是整内核级的终止，宿主在同步时才看到错误」。
5. 待本地验证（运行需 GPU）。

#### 4.2.5 小练习与答案

- **练习**：`clock64()` 与 `globaltimer()` 一个「per-SM」一个「全局」，这对测量意味着什么？
- **答案**：`clock64` 适合量同一线程/warp 内的相对周期差（比如一段算术花多少周期），但不同 SM 的读数不可直接比较；`globaltimer` 是跨 SM 一致的时基，适合量「跨 SM 协作」的时间差，但精度/粒度不同，且仍只用于相对测量（绝对值与电源状态等有关）。
- **练习**：为什么 `trap()` 的返回类型是 `!` 而 `clock64()` 是 `u64`？
- **答案**：`!` 是发散类型，表示该函数永不正常返回（调用点之后代码不可达），这样 `gpu_assert!` 才能在条件分支里调用 `trap()` 而不需要给 `else` 分支填值；`clock64` 正常返回计数值，故是 `u64`。

### 4.3 gpu_assert! / breakpoint / prof_trigger：断言、断点与剖析标记

#### 4.3.1 概念说明

本模块的三件套面向「更结构化的调试与剖析」：

- `gpu_assert!(cond)` / `gpu_assert!(cond, "msg")`：等价于 Rust 的 `assert!`，但跑在内核里。条件为假就调 `trap()`。
- `breakpoint()`：等价于 CUDA C++ 的 `__brkpt()`，在 cuda-gdb 下是一个**硬件断点**——执行到这里的线程会停下来交回调试器。
- `prof_trigger::<N>()`：等价于 `__prof_trigger(N)`，给 NVIDIA Nsight Systems / Compute 打一个编号为 `N` 的事件标记，便于在时间轴上对齐区域。

三者各有微妙：`gpu_assert!` 在 release 编译里也保留（不像 std `assert!` 可被移除）；`breakpoint` 在**没有** cuda-gdb 时通常表现为陷阱或 no-op，所以示例会主动跳过它；`prof_trigger` 用 `const generic` 携带编号，开销很小。

#### 4.3.2 核心流程

`gpu_assert!` 的展开非常朴素——它就是「`if !cond { trap() }`」，没有像 CUDA C++ 那样通过 `assertfail` 打印文件行号（注释里写明这是 TODO）：

```
gpu_assert!(x >= 0)
   └─展开─> if !(x >= 0) { $crate::debug::trap(); }
```

`breakpoint` 与 `prof_trigger` 的降级路径：

```
breakpoint()        → nvvm brkpt op → inline_asm_convergent("brkpt;")
prof_trigger::<N>() → nvvm PmEvent op(事件号=N) → inline_asm_convergent("pmevent N;")
```

`prof_trigger` 走特殊路径：mir-importer 在 `translate_call` 里**专门拦截**它，从 const generic 抠出 `N`，再调 `emit_prof_trigger`（因为它需要把编译期的 `N` 变成立即数嵌进 PTX，而不是当普通参数）。

#### 4.3.3 源码精读

`gpu_assert!` 宏定义（[`crates/cuda-device/src/debug.rs:261-274`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/debug.rs#L261-L274)）：

```rust
#[macro_export]
macro_rules! gpu_assert {
    ($cond:expr) => { if !$cond { $crate::debug::trap(); } };
    ($cond:expr, $msg:expr) => {
        if !$cond {
            // TODO (npasham): Use llvm.nvvm.assertfail for ... file/line
            $crate::debug::trap();
        }
    };
}
```

`breakpoint` 桩（[`crates/cuda-device/src/debug.rs:190-194`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/debug.rs#L190-L194)），其降级 `convert_breakpoint` 与 `convert_trap` 几乎一模一样，只差汇编串 `brkpt;`（[`crates/mir-lower/src/convert/intrinsics/debug.rs:110-120`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/debug.rs#L110-L120)）：

```rust
pub(crate) fn convert_breakpoint(ctx, rewriter, op, _) -> Result<()> {
    let void_ty = llvm_types::VoidType::get(ctx);
    inline_asm_convergent(ctx, rewriter, void_ty.into(), vec![], "brkpt;", "");
    rewriter.erase_operation(ctx, op);
    Ok(())
}
```

`prof_trigger` 的 const generic `N` 在 mir-importer 里被专门抠出来（[`crates/mir-importer/src/translator/terminator/mod.rs:980-1003`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/mod.rs#L980-L1003)），降级时 `convert_pm_event` 把事件号格式化进汇编串（[`crates/mir-lower/src/convert/intrinsics/debug.rs:122-139`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/debug.rs#L122-L139)）：

```rust
let pmevent_op = PmEventOp::new(op);
let event_id = pmevent_op.get_event_id(ctx).unwrap_or(0);
let asm_str = format!("pmevent {};", event_id);
inline_asm_convergent(ctx, rewriter, void_ty.into(), vec![], &asm_str, "");
```

示例里 `assert_test` 内核演示了带消息和不带消息两种 `gpu_assert!` 用法（[`crates/rustc-codegen-cuda/examples/debug/src/main.rs:109-122`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/debug/src/main.rs#L109-L122)）；`breakpoint_test` 只在线程 0 触发断点以「避免淹没调试器」（[`crates/rustc-codegen-cuda/examples/debug/src/main.rs:125-136`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/debug/src/main.rs#L125-L136)）；`profiler_test` 用 `prof_trigger::<0>()` / `::<1>()` 给一段平方计算打「起/止」标记（[`crates/rustc-codegen-cuda/examples/debug/src/main.rs:139-153`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/debug/src/main.rs#L139-L153)）。

值得注意：示例的 `main` 在 Test 4 主动跳过 `breakpoint_test`，并写明「brkpt 在不在 cuda-gdb 下跑会导致 launch 失败」（[`crates/rustc-codegen-cuda/examples/debug/src/main.rs:278-285`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/debug/src/main.rs#L278-L285)）——这正是 `trap()` 与 `breakpoint()` 在 cuda-gdb 外的最大区别：`trap()` 一定会终止内核，`breakpoint()` 在 cuda-gdb 下是断点（暂停），在 cuda-gdb 外通常也是个陷阱，所以「能不能脱离调试器直接跑」两者表现不同。

#### 4.3.4 代码实践

1. **目标**：用 `gpu_assert!` 给内核加一个运行时断言，并理解 `trap()` 与 `breakpoint()` 在 cuda-gdb 内外的不同表现。
2. **步骤**：
   - 复制 `assert_test` 内核，把断言改成 `gpu_assert!(val < 1000);`，把输入改成含 `1000` 的数组，`cargo oxide run debug` 观察 Test 3 的同步错误。
   - 在 cuda-gdb 里跑 `breakpoint_test`：`cargo oxide debug debug`，进入调试器后 `set cuda break_on_launch application`，`run`，再 `cuda thread (0,0,0)` 切到线程 0，`continue` 直到命中 `breakpoint()`。
3. **需要观察的现象**：`gpu_assert!` 失败时表现与 `trap()` 一致（launch 失败）；`breakpoint()` 在 cuda-gdb 下命中后会停在那个线程、那行源码，可以 `print output_elem` 看值。
4. **预期结果**：能说清「`trap()` = 无条件终止、总是失败；`breakpoint()` = 调试器下的暂停点，离开调试器跑会变成陷阱导致 launch 失败」。
5. 待本地验证（断点实践必须有 GPU + CUDA toolkit + cuda-gdb）。

#### 4.3.5 小练习与答案

- **练习**：`gpu_assert!(cond, "msg")` 的消息当前去哪了？为什么？
- **答案**：当前被忽略。宏的第二个分支虽然匹配了 `$msg`，但体内仍只调 `trap()`，注释标注了 TODO：未来改用 `llvm.nvvm.assertfail` 才能把文件/行号/消息打印出来。当前要拿消息，得靠 cuda-gdb 在 `trap()` 处停下来人工查看。
- **练习**：为什么 `prof_trigger` 用 `const generic`（`::<N>()`）而不是普通参数 `prof_trigger(n: u32)`？
- **答案**：事件号必须是**编译期立即数**，最终要嵌进 PTX 的 `pmevent N;` 字面量里。普通运行期参数会变成寄存器值，PTX `pmevent` 不接受这种形式，mir-importer 也专门为此从 const generic 抠值。这与 `#[launch_bounds]` 用 const generic 传 `MAX/MIN` 是同一个理由。

### 4.4 #[launch_bounds]：给编译器的占空提示

#### 4.4.1 概念说明

`#[launch_bounds(max_threads, min_blocks)]` 不是调试 intrinsic，但它和本讲的内核性能/可调试性关系密切，且示例 `debug` 把它和 debug intrinsics 放在一起演示。它告诉编译器两件事：

- **每块最大线程数**（`max_threads`）→ 落到 PTX `.maxntid`。
- **每 SM 最少驻留块数**（`min_blocks`，可选）→ 落到 `.minnctapersm`，是 occupancy（占空率）提示。

编译器据此分配寄存器、决定能否更大胆地使用寄存器换取每线程更多资源，从而影响实际占空率与性能。

#### 4.4.2 核心流程

`#[launch_bounds]` 走的是 cuda-oxide 里反复出现的「**标记函数注入 + 编译期扫描**」模式，与 `#[launch_contract]`、`#[unroll]` 同族：

```
#[launch_bounds(256, 2)]                      用户属性
   │  cuda-macros 在内核首部注入标记调用
   ▼
__launch_bounds_config::<256, 2>()            (设备桩，函数体为空)
   │  mir-importer detect_launch_bounds_config 扫描 MIR，抠出 const generic
   ▼
给 mir.func op 挂 maxntid=256 / minctasm=2 属性
   │  llvm-export 读属性 → !nvvm.annotations 元数据
   ▼
.entry foo .maxntid 256 .minnctapersm 2 { ... }   PTX
```

关键点：标记调用本身**不生成任何运行期代码**，它在 mir-importer 里被识别后就不再被翻译成指令，只用来「旁路」地把属性挂到函数 op 上。

#### 4.4.3 源码精读

设备端的标记函数 `__launch_bounds_config`，文档注释把全链路讲得很清楚（[`crates/cuda-device/src/thread.rs:717-764`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/thread.rs#L717-L764)）：

```rust
/// This function should NOT be called directly. Use the `#[launch_bounds(max, min)]`
/// attribute macro instead, which injects this marker:
#[inline(never)]
pub fn __launch_bounds_config<const MAX_THREADS: u32, const MIN_BLOCKS: u32>() {
    // This function is detected at compile time and removed.
    // The const generics are extracted to set launch bounds.
    // No runtime code is generated.
}
```

mir-importer 扫描到这个标记调用后，把 `MAX_THREADS`/`MIN_BLOCKS` 写成函数属性 `maxntid` / `minctasm`（[`crates/mir-importer/src/translator/body.rs:861-887`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/body.rs#L861-L887)）：

```rust
if let Some(launch_bounds) = detect_launch_bounds_config(body) {
    let apint_max = APInt::from_u32(launch_bounds.max_threads, width);
    let max_attr = IntegerAttr::new(u32_ty, apint_max);
    op_mut.attributes.set("maxntid".try_into().unwrap(), max_attr);
    if launch_bounds.min_blocks > 0 {
        let apint_min = APInt::from_u32(launch_bounds.min_blocks, width);
        op_mut.attributes.set("minctasm".try_into().unwrap(), /*...*/);
    }
}
```

> 注意属性键的拼写：mir-importer 里写 `minctasm`，最终 PTX 指令是 `.minnctapersm`，中间由 llvm-export 把属性翻译成 `!nvvm.annotations` 元数据（`maxntid` / `minctasm` 是 LLVM NVPTX 后端认识的两个 metadata 名字）。

示例里两个内核分别用了不同的 launch bounds：`clock_test` 用 `#[launch_bounds(256, 2)]`（[`crates/rustc-codegen-cuda/examples/debug/src/main.rs:32-34`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/debug/src/main.rs#L32-L34)），`launch_bounds_test` 用 `#[launch_bounds(128, 4)]`（[`crates/rustc-codegen-cuda/examples/debug/src/main.rs:156-158`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/debug/src/main.rs#L156-L158)），后者在 Test 6 还提示「Check PTX for `.maxntid 128 .minnctapersm 4`」（[`main.rs:325-355`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/debug/src/main.rs#L325-L355)）。

#### 4.4.4 代码实践

1. **目标**：用 `cargo oxide pipeline` 看 `#[launch_bounds]` 落到 PTX 的真实样子。
2. **步骤**：执行 `cargo oxide pipeline debug`，找到 `launch_bounds_test` 的 `.ptx`，定位 `.entry launch_bounds_test` 那一行。
3. **需要观察的现象**：该 `.entry` 行应带 `.maxntid 128 .minnctapersm 4`（连字符/下划线无关，名字是 PTX 内核符号）。把属性改成 `#[launch_bounds(64, 8)]` 重新生成，对照变化。
4. **预期结果**：能复述「属性 → mir func 属性 → nvvm.annotations → PTX 指令」的链路，并指出 `.minnctapersm` 只在第二个参数非零时出现。
5. 待本地验证（生成 PTX 不需要 GPU，`cargo oxide pipeline` 在仅有 toolchain 的环境即可）。

#### 4.4.5 小练习与答案

- **练习**：`#[launch_bounds]` 与 `#[launch_contract]`（u2-l1/u2-l4）都和「启动」有关，二者本质区别是什么？
- **答案**：`#[launch_bounds]` 是**给编译器的性能/占空提示**，影响寄存器分配与 PTX 指令注解，运行期不校验、错了也不一定立刻报错；`#[launch_contract]` 是**给类型系统的安全契约**，会被 `prepare_*` → `PreparedLaunch` 在活设备上**校验**（block 形状、共享内存、算力等），违反会被 `LaunchContractError` 拒绝。前者管「跑得多快」，后者管「跑得对不对」。
- **练习**：`min_blocks` 设得越大越好吗？
- **答案**：不是。它是一个**最小**占空目标，编译器会尝试通过压低寄存器使用来满足，但设得过高会强制 spills（把寄存器溢出到本地内存），反而变慢甚至无法满足。需要结合 `cuobjdump` / Nsight Compute 的寄存器与占空统计来调。

### 4.5 #330：debug 的可执行发现与 --bin / --features

#### 4.5.1 概念说明

PR #330 修了 `cargo oxide debug` 的两个老问题：

1. **可执行文件发现不准**：以前 `debug` 只能在示例的包名与可执行名「恰好一致」时正确找到要调试的二进制；当一个包产出多个 bin、或包名带连字符而 bin 名不带时，就会找错或找不到。#330 让它复用 `run_cargo_build_for_executable`，**解析 Cargo 的 `--message-format=json` 输出**，从 `reason == "compiler-artifact"` 的消息里取真实 `executable` 字段。
2. **缺少 `--bin` / `--features`**：以前没法指定「调试哪个 bin」或「开哪些 cargo feature」。#330 给 `Debug` 变体补了这两个旗标，并一路透传到 `cargo build` 命令。
3. **cuda-gdb 解析**：经 `find_cuda_toolkit_executable` 走 toolkit root 解析，不再只靠 `PATH`。

#### 4.5.2 核心流程

「可执行发现」是 #330 的核心，机制如下：

```
cargo build --release --message-format=json-render-diagnostics
   │  Cargo 把每个产出的 crate 作为一行 JSON 输出
   ▼
逐行解析 JSON
   ├─ reason == "compiler-artifact" ?
   ├─ target.kind 含 "bin" ?
   └─ 取 message["executable"] / ["target"]["name"] / ["package_id"]
   ▼
收集到 CargoExecutableArtifact 列表
   ▼
select_cargo_executable_artifact(selection, executables)
   └─ 用 --bin / 默认 run / 唯一性规则选出一个精确路径
```

这套逻辑还兼顾了「一个包有多个 bin」「workspace 选了多个包各产出 bin」等歧义场景，歧义时报错并提示用户传 `--bin <name>`。

#### 4.5.3 源码精读

`run_cargo_build_for_executable` 的解析主体（[`crates/cargo-oxide/src/commands.rs:724-800`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/commands.rs#L724-L800)），关键是只认 `compiler-artifact` 且 `kind` 含 `bin` 的消息：

```rust
cmd.arg("--message-format=json-render-diagnostics");
let output = cmd.output()/* ... */;
let mut executables = Vec::<CargoExecutableArtifact>::new();
for line in String::from_utf8_lossy(&output.stdout).lines() {
    let message: serde_json::Value = serde_json::from_str(line)?;
    // ...
    if message.get("reason").and_then(|r| r.as_str()) != Some("compiler-artifact") {
        continue;
    }
    let is_binary = /* target.kind 含 "bin" */;
    if !is_binary { continue; }
    let Some(path) = message.get("executable").and_then(|p| p.as_str()) else { continue; };
    // ... 收集 package_id / target_name / path
}
select_cargo_executable_artifact(&selection, &executables)
```

歧义处理在 `select_cargo_executable_artifact` 里：一个包产出多个 bin 时报 `"Cargo produced multiple executable targets for package ... pass --bin <name>"`（[`crates/cargo-oxide/src/commands.rs:1048-1062`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/commands.rs#L1048-L1062)）。CLI 侧对 `--bin` / `--features` 的解析正确性由单元测试固化（[`crates/cargo-oxide/src/main.rs:682-710`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/main.rs#L682-L710)）：

```rust
#[test]
fn debug_parser_accepts_bin_and_features() {
    let cli = Cli::try_parse_from([
        "cargo-oxide", "debug", "my_app",
        "--bin", "debug-target",
        "--features", "foo,bar",
        "--tui",
    ]).expect("debug command should parse");
    let Commands::Debug { example, features, bin, tui, .. } = cli.command else { panic!() };
    assert_eq!(example.as_deref(), Some("my_app"));
    assert_eq!(bin.as_deref(), Some("debug-target"));
    assert_eq!(features.as_deref(), Some("foo,bar"));
    assert!(tui);
}
```

#### 4.5.4 代码实践

1. **目标**：验证 `--bin` / `--features` 真的被传给了 `cargo build`，并理解歧义时报错。
2. **步骤**（无 GPU 也可做）：
   - 在 `debug` 示例的 `Cargo.toml` 里**临时**加一个二进制目标别名（例如 `[[bin]] name = "debug-alt" path = "src/main.rs"`），让它产出两个 bin。
   - 跑 `cargo oxide debug debug`，观察是否报「multiple executable targets ... pass --bin」。
   - 再跑 `cargo oxide debug debug --bin debug`，确认能精确选中。
3. **需要观察的现象**：不指定 `--bin` 时报歧义错误；指定后正常构建。
4. **预期结果**：能复述「可执行发现走 Cargo 的 compiler-artifact JSON，歧义时强制要求 `--bin`」。
5. 待本地验证（仅构建即可，不需要 GPU；记得改完 `Cargo.toml` 后还原，避免污染示例）。

> **注意**：本实践要求修改示例的 `Cargo.toml`。本讲义**只读源码**，请你在自己的 fork / 临时副本上做这个改动，做完还原，不要把改动提交进仓库。

#### 4.5.5 小练习与答案

- **练习**：为什么用 `--message-format=json-render-diagnostics` 而不是直接去 `target/release/` 目录里找一个同名文件？
- **答案**：因为包名（带连字符）与 bin 名（带下划线）经常不一致，一个包可能产出多个 bin，workspace 模式下还可能选多个包。Cargo 的 JSON 输出是「权威来源」——它精确告诉你这次构建实际产出了哪些可执行文件、各自路径与所属包。靠文件名猜测在多 bin / 连字符场景必错。
- **练习**：`--features foo,bar` 与 `--features foo --features bar` 等价吗？
- **答案**：在本实现里 `features: Option<String>` 是单个字符串，直接 `cmd.args(["--features", features])` 透传给 cargo，所以 `foo,bar` 由 cargo 自己按逗号拆分，与多次 `--features` 等价（cargo 的语义）。但若你想传带空格的 feature 名就得注意引号转义。

## 5. 综合实践

把本讲的所有要素串成一个**可调试的最小内核**。在 `debug` 示例基础上新增一个内核 `squared_sum`：

1. 用 `#[launch_bounds(128, 4)]` 给它占空提示。
2. 在内核开头 `debug::prof_trigger::<0>()`、结尾 `debug::prof_trigger::<1>()`，方便用 nsys 标记区域。
3. 用 `gpu_assert!(input.len() > 0)` 做一个运行期前置断言（注意 `gpu_assert!` 在内核里跑，断言的对象应是线程可见的值，而非 host 的 `len`——请你改为对每个线程读取的值做断言，思考为什么不能直接断言 host 的长度）。
4. 用 `clock64()` 量计算前后周期差，写回输出。
5. 在线程 0 放一个 `debug::breakpoint()`，用 `cargo oxide debug debug --bin debug` 拉起 cuda-gdb，`set cuda break_on_launch application` → `run` → 命中断点后 `print` 一个内核局部变量。
6. 最后用 `cargo oxide pipeline debug` 找到 `squared_sum` 的 PTX，确认 `.maxntid 128 .minnctapersm 4`、`pmevent`、`clock64`、`trap` 都正确出现。

完成后再写一段话：解释「`trap()`、`breakpoint()`、`gpu_assert!` 在 cuda-gdb 内与外分别表现为什么」，并指出这三个 intrinsic 在源码层为什么都是 `unreachable!()` 桩却能在 GPU 上真正生效。

## 6. 本讲小结

- `cargo oxide debug` 是 cuda-gdb 的一条龙入口：叠加 `CARGO_PROFILE_RELEASE_DEBUG=2` 出带 DWARF 的 release 二进制，发现 cuda-gdb（`PATH` → toolkit root → 回退路径），再拉起调试器并打印速查表。
- 设备端 debug intrinsics（`clock`/`clock64`/`globaltimer`/`trap`/`breakpoint`/`prof_trigger`）在源码里全是 `#[inline(never)]` + `unreachable!()` 桩，由编译器按全限定名识别后降级：计时类走 LLVM intrinsic，`trap`/`breakpoint`/`prof_trigger` 走 convergent 内联 PTX。
- `gpu_assert!` = `if !cond { trap() }`，当前忽略消息（TODO 用 `llvm.nvvm.assertfail`）；`trap()` 总是终止内核，`breakpoint()` 在 cuda-gdb 下是暂停点、离开调试器会变成陷阱。
- `prof_trigger::<N>()` 用 const generic 把事件号做成 PTX 立即数，mir-importer 专门拦截抠值。
- `#[launch_bounds(max, min)]` 走「标记函数注入 → mir-importer 扫描 → 函数属性 `maxntid`/`minctasm` → PTX `.maxntid`/`.minnctapersm`」全链路，是性能提示而非安全契约（与 `#[launch_contract]` 区分）。
- #330 让 `debug` 复用 Cargo `compiler-artifact` JSON 精确发现可执行文件、补齐 `--bin`/`--features`、经 toolkit root 解析 cuda-gdb，歧义时报错并要求 `--bin`。

## 7. 下一步学习建议

- **继续调试线**：本讲的「正确性检查」是手动断点；下一讲 **u7-l3（Compute Sanitizer）** 讲 `cargo oxide sanitize`，让你用 memcheck/racecheck/initcheck/synccheck 自动发现越界、数据竞争、未初始化读与同步错误，两者互补。
- **深潜 intrinsic 落地**：如果你想给 cuda-oxide 自己新增一个 debug intrinsic，按 **u6-l4（端到端新增 intrinsic）** 的五阶段模板（cuda-device 桩 → dialect-nvvm op → mir-importer 翻译 → mir-lower lowering → llvm-export）走一遍，本讲的 `trap`/`breakpoint` 是最简单的范本。
- **理解宏与契约的全貌**：本讲的 `gpu_assert!` 是一个最朴素的过程宏；复杂的宏与启动安全契约在 **u2-l1** 与 **u7-l1（compile_fail 与安全契约）** 里有完整展开，可对照阅读。
- **阅读建议**：把 `crates/mir-lower/src/convert/intrinsics/debug.rs` 的整张降级表（[L8-L16](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/debug.rs#L8-L16)）和 `crates/cuda-device/src/debug.rs` 的桩函数逐行对照，是巩固「设备桩模型」最直接的方式。
