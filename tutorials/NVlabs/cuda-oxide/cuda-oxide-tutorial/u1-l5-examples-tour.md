# 示例导览：cuda-oxide 能做什么

> 本讲是入门单元（U1）的最后一篇。前几讲你已经建立项目定位（u1-l1）、crate 地图（u1-l2）、工具链与 `cargo oxide` 驱动（u1-l3），并用 `vecadd` 跑通了端到端闭环（u1-l4）。本讲不再引入新机制，而是带你**俯瞰整个示例库**，建立一张「cuda-oxide 现阶段到底能做什么」的能力地图，帮助你在后续 U2–U7 中选准切入点。

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `examples/` 目录的组织方式：每个示例都是独立的 standalone crate，靠 `cargo oxide run <name>` / `cargo oxide build <name>` 驱动。
- 对照 README 的「示例表」与「能力清单」，复述 cuda-oxide 的能力边界（单源编译、泛型/闭包、原子、集群、异步、Blackwell 张量核、设备 FFI、打包原子加法、**嵌套模块**……）。
- 区分「能编译」与「能运行」：理解 `sm_xx` 架构要求、`--arch` / `CUDA_OXIDE_DEVICE_ARCH` / `CUDA_OXIDE_TARGET` 三者的关系，以及为何 Hopper/Blackwell 示例必须有 `llc-21+`。
- 认出本轮（#318/#324/#327/#328/#329）给示例库带来的三类变化：**所有内核启动调用统一包进 `unsafe` 块**、新增 **`cuda_module_nested`（嵌套 inline 模块收集 kernel）**、新增 **f16 / tf32 / s8 三条 mma.sync intrinsic**。
- 根据自己的学习目标，从上百个示例里挑出 2–3 个最适合自己的起点（建议含 `cuda_module_nested` 与 `packed_atomic_add`）。

## 2. 前置知识

本讲是「导览」，不深入任何单一机制，但会用到你前几讲建立的几个概念。先快速复习：

- **单源编译**：同一个 `.rs` 文件里，`#[kernel]` 函数被 `rustc-codegen-cuda` 后端编进 PTX（设备端），其余代码走标准 LLVM 后端编成 x86_64（宿主端），全程不需要 `#[cfg]` 切分（见 u1-l1、u1-l4）。
- **`#[cuda_module]` / `#[kernel]`**：宏在编译期为模块生成类型安全的启动方法（`module.vecadd(...)`）和加载器（`kernels::load(&ctx)`），见 u1-l4。
- **`LaunchConfig::for_num_elems(N)`**：便捷启动配置，块大小固定 256、grid 自动取 \(\lceil N/256\rceil\)；总线程数为 \(\text{gridDim}\times\text{blockDim}\)，见 u1-l4。
- **raw 启动是 `unsafe` 的**：本轮 #318 把 `LaunchConfig` 明确为「未经证明的原始数据」，所以每个示例里 `module.<kernel>(...)` 都包在 `unsafe { ... }` 中、由调用方用 `// SAFETY:` 注释自证形状/资源/缓冲区匹配；免 `unsafe` 的受检启动（`#[launch_contract]` + `prepare_*` → `PreparedLaunch`）留到 u2-l1/u2-l4 展开。
- **`cargo oxide` 子命令**：`run`（编译并运行）、`build`（只编译不运行）、`pipeline`（打印全流水线中间产物）、`doctor`（环境体检）、`sanitize`（在 NVIDIA Compute Sanitizer 下运行，见 u1-l3），见 u1-l3。

如果你对上述任何一项感到陌生，建议先回看对应讲义再继续。

## 3. 本讲源码地图

本讲主要「读」而不是「写」，涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| `README.md` | 项目首页，含「Examples」示例表与「Highlights」能力清单，是能力地图的一手来源。 |
| `crates/rustc-codegen-cuda/examples/vecadd/Cargo.toml` | 一个最小示例的 standalone crate 结构样本。 |
| `crates/rustc-codegen-cuda/examples/tcgen05/Cargo.toml` | Blackwell 专用示例的 Cargo.toml，注释里标注了硬件要求。 |
| `crates/rustc-codegen-cuda/examples/async_vecadd/src/main.rs` | 异步执行模型示例（`cuda-async` / `DeviceOperation`）。 |
| `crates/rustc-codegen-cuda/examples/atomics/src/main.rs` | GPU 原子操作综合测试（6 类型 × 3 作用域 × 5 排序，共 20 个测试）。 |
| `crates/rustc-codegen-cuda/examples/cluster/src/main.rs` | Hopper 线程块集群 + 分布式共享内存（DSMEM）示例。 |
| `crates/rustc-codegen-cuda/examples/packed_atomic_add/src/main.rs` | 打包 f16x2 / bf16x2 全局原子加法端到端示例（sm_90+）。 |
| `crates/rustc-codegen-cuda/examples/cuda_module_nested/src/main.rs` | **本轮新增（#324）**：`#[cuda_module]` 收集嵌套 inline 模块里的 kernel，演示按命名空间分层的 `LoadedModule` 启动视图。 |
| `crates/cargo-oxide/src/main.rs` | `cargo oxide` 的 CLI 定义，含 `build` 与 `sanitize` 子命令。 |

> 提示：本讲引用的永久链接基于当前 HEAD `29396b7`。示例源码会随项目演进，行号可能变化；若链接失配，以你本地 checkout 为准。

## 4. 核心概念与源码讲解

### 4.1 examples 目录的组织与 README 示例表

#### 4.1.1 概念说明

`crates/rustc-codegen-cuda/examples/` 是 cuda-oxide 的「能力展厅」，目前目录下共有 **133 个**子目录。README 这样介绍它：

> **60+ examples** in `crates/rustc-codegen-cuda/examples/`. Highlights:

参见 [README.md:241](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/README.md#L241)。这里有两点要建立直觉：

1. **README 标称「60+」，但目录下实际有 133 个子目录。** 这是因为示例库里既有「正面演示」（runnable demo），也有少量「反面测试」（compile-fail，即 `error_*` 系列、`cuda_module_contract`），它们的用途是**断言编译器会拒绝不安全的写法**，属于编译期契约测试，而非可运行 demo。所以你看到的条目数远大于 60，但「能 `cargo oxide run` 起来」的正面示例大致与「60+」的量级吻合。
2. **每个示例都是一个独立的 standalone crate**，而不是某个父 crate 下的 `[[example]]`。这与 `rustc-codegen-cuda` 本体「编外于 workspace」是同一套手法。

#### 4.1.2 核心流程

以 `vecadd` 为例，一个示例目录的最小结构是：

```
examples/vecadd/
├── Cargo.toml      # 带 [workspace]，主动退出父 workspace
└── src/
    └── main.rs     # host main + #[cuda_module] mod kernels
```

`Cargo.toml` 里有一行 `[workspace]`，把自身标记为独立 crate：

```toml
# Mark as standalone crate (not part of parent workspace)
[workspace]
```

参见 [crates/rustc-codegen-cuda/examples/vecadd/Cargo.toml:1-26](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/vecadd/Cargo.toml#L1-L26)。这样设计的好处是：每个示例可以被 `cargo oxide` 当成一个完整的小项目单独编译/运行，彼此互不干扰，也方便你把某个示例直接拷出去作为自己项目的模板。`cuda_module_nested` 的 Cargo.toml 用了同样的写法，并在注释里点明它演示的是「嵌套 inline 模块的命名空间局部启动器」，见 [crates/rustc-codegen-cuda/examples/cuda_module_nested/Cargo.toml:8-13](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cuda_module_nested/Cargo.toml#L8-L13)。

驱动方式（复习 u1-l3）：

- `cargo oxide run vecadd` —— 编译并启动到 GPU、回收结果。
- `cargo oxide build vecadd` —— **只编译不运行**（产出 PTX，不碰 GPU）。
- `cargo oxide pipeline vecadd` —— 打印 Rust MIR → dialect-mir → mem2reg → LLVM dialect → LLVM IR → PTX 的全流水线中间产物。
- `cargo oxide sanitize vecadd --tool memcheck` —— 在 NVIDIA Compute Sanitizer 下运行（详见 u1-l3、u7-l3）。

#### 4.1.3 源码精读

README 的示例表是能力地图的浓缩版（共 16 行），挑几行最具代表性的：

| 示例 | 说明（摘自 README） |
|------|---------------------|
| `vecadd` | 向量加法——规范的首个示例 |
| `host_closure` | 从宿主传入闭包的泛型内核 |
| `generic` | 带单态化的泛型内核（`scale<T>`） |
| `ord_cmp` | 设备端 `Ord::cmp` lowering（有符号/无符号整数） |
| `gemm_sol_final` | 规范的 Blackwell GEMM SoL：size-specialized CLC + cg2 + 向量存储 |
| `tcgen05` | Blackwell 张量核（sm_100a）：TMEM、MMA、cta_group::2 |
| `atomics` | GPU 原子：6 类型 × 3 作用域 × 5 排序（20 个测试） |
| `atomic_f16` | 标量 f16 原子：分作用域正确性检查 + f32 vs f16 基准 |
| `cluster` | 线程块集群 + DSMEM 环形交换（Hopper+） |
| `async_mlp` | 异步 MLP 流水线：GEMM → MatVec → ReLU，跨并发流 |
| `device_ffi_test` | 设备 FFI：Rust 内核通过 LTOIR 调用 C++ CCCL warp 级归约 |
| `async_vecadd` | 用 `cuda-async` 与 `DeviceOperation` 的异步 GPU 执行 |

完整表格见 [README.md:243-260](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/README.md#L243-L260)。> 注：README 的精选表只列了 16 个最具代表性的示例，**没有**单独列出 `packed_atomic_add`、`cuda_module_nested`、`gemm`、`tiled_gemm`、`wgmma`、`tma_*`、`cp_async_*` 等——它们都在目录里、可 `cargo oxide run <name>`，只是未被收进首页精选表。紧接其后的「Highlights」清单则是项目当前状态的「成绩单」，见 [README.md:307-318](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/README.md#L307-L318)，覆盖端到端 Rust→PTX、单源、泛型单态化、带捕获闭包、用户自定义结构体/枚举/模式匹配、全套 GPU intrinsic（thread/warp/共享内存/屏障/TMA/集群/原子）、跨 crate 内核、Blackwell+ 的 LTOIR、设备 FFI、MathDx 集成等。

#### 4.1.4 代码实践

**实践目标**：亲手确认「每个示例都是独立 crate」这一组织方式。

**操作步骤**：

1. 在仓库根目录列出示例目录：`ls crates/rustc-codegen-cuda/examples/ | wc -l`，确认条目数量（当前 133 个），体会「README 标称 60+，实际更多」。
2. 打开任意两个示例的 `Cargo.toml`（如 `vecadd` 与 `cuda_module_nested`），确认它们都含 `[workspace]` 这一行。
3. 挑一个 `error_*` 开头的目录（如 `error_missing_device_attr`），打开它的 `src/main.rs` 顶部注释，看看它**期望编译失败**的契约是什么；再打开 `cuda_module_contract`（本轮扩了大量启动契约负向测试），看看它断言的是什么。

**需要观察的现象**：

- 正面示例的 `main.rs` 顶部注释通常以 `//! ... Example` 开头，并给出 `cargo oxide run <name>` 的运行命令。
- `error_*` 系列的注释会说明它应当被编译器拒绝（这类示例在 CI 里以「期望失败」的方式被验证）。

**预期结果**：你能口头区分「正面示例」与「反面测试」，并知道前者用 `run`、后者通常只参与编译期断言。

**待本地验证**：具体某个 `error_*` 示例的失败信息文案，建议本地 `cargo oxide build error_missing_device_attr` 实际触发一次再记录。

#### 4.1.5 小练习与答案

**练习 1**：为什么示例要用 `[workspace]` 把自己标记成 standalone crate，而不是放进父 workspace？

> **参考答案**：因为这些示例需要被 `rustc-codegen-cuda` 这个**编外于 workspace 的 dylib 后端**编译（见 u1-l2）。放进父 workspace 会让 cargo 用默认后端去编它们，无法触发设备代码生成；用 `[workspace]` 退出父 workspace 后，`cargo oxide` 才能为每个示例单独注入 `-Z codegen-backend=...librustc_codegen_cuda.so`。

**练习 2**：README 里 `vecadd` 被称作「canonical first example」，结合 u1-l4，它「规范」在哪？

> **参考答案**：它用最少的代码串起了完整闭环——`#[cuda_module] mod kernels` + `#[kernel] fn vecadd` + 宿主 `DeviceBuffer::from_host/zeroed/to_host_vec` + `LaunchConfig::for_num_elems` + `kernels::load`，是学习单源编译模型的最小完整样本。

---

### 4.2 能力矩阵概览

#### 4.2.1 概念说明

光看 README 表格还不够——它是「按示例名」罗列的。要选学习切入点，更实用的是**按能力主题**重新归类。本节把示例库整理成一张「能力矩阵」，每个主题给你 1–3 个最值得先读的示例。这些归类都基于真实源码的模块注释，不是凭空推测。

> **关于「启动都包在 `unsafe` 里」**：下面矩阵里几乎所有示例的宿主 `main` 都长这样——`unsafe { module.<kernel>(&stream, config, ...) }`，配一条 `// SAFETY:` 注释。这是 #318 后的统一约定：raw `LaunchConfig` 是未经证明的原始数据，启动正确性由调用方自证。本讲关注「示例演示了什么能力」，这条约定只作背景，受检启动专题见 u2-l1/u2-l4。

#### 4.2.2 核心流程：按主题归类的能力矩阵

| 能力主题 | 代表示例 | 一句话能力 | 对应后续讲义 |
|----------|----------|-----------|--------------|
| 单源基础 | `vecadd`、`async_vecadd` | host/device 同文件，端到端闭环 | u1-l4、u3-l3 |
| 泛型与闭包 | `generic`、`host_closure` | `scale<T>` 单态化；带 0–4 个捕获的闭包当 kernel 参数 | u2-l6 |
| GPU 原子（标量） | `atomics`、`atomic_f16` | 6 类型 × 3 作用域 × 5 排序；作用域原子；f16 标量原子 | u5（专家层） |
| GPU 原子（打包） | `packed_atomic_add` | f16x2 / bf16x2 两条 16-bit lane 打包进一个 32-bit 字内独立原子累加 | u5-l2 |
| 共享内存与同步 | `sharedmem`、`dynamic_smem`、`barrier` | 静态/动态共享内存、块级屏障 | u2-l3 |
| warp 级编程 | `warp_reduce`、`shuffle_64`、`redux_sum`、`lanemask_scan` | warp shuffle、warp 归约、投票 | u5 |
| 张量核 mma.sync | `gemm`、`tiled_gemm` | warp 级矩阵乘加速、ldmatrix 片段布局；支持 f16 / bf16 m16n8k16、tf32 m16n8k8、f64 m8n8k4、**s8 m16n8k32（本轮 #329 新增）** | u5-l6 |
| 嵌套模块组织 | `cuda_module_nested` | **本轮 #324 新增**：`#[cuda_module]` 收集嵌套 inline 模块里的 kernel，按命名空间分层启动 | u2-l1 |
| Hopper 集群/DSMEM | `cluster`、`mcast_barrier_test` | 线程块集群、分布式共享内存 | u5 |
| 异步拷贝/屏障 | `cp_async_small`、`cp_async_zfill` | `cp.async` 异步全局→共享 | u5 |
| Blackwell 张量核/TMA | `tcgen05`、`tcgen05_matmul`、`tma_copy`、`tma_multicast`、`wgmma` | TMEM、MMA、TMA、WGMMA | u5 |
| GEMM SoL | `gemm`、`tiled_gemm`、`gemm_sol`、`gemm_sol_final` | 分块 GEMM、size-specialized CLC | u5 |
| 异步运行时 | `async_vecadd`、`async_mlp`、`future_apis` | 惰性 `DeviceOperation`、流池调度 | u3-l3、u3-l4 |
| 设备 FFI 互操作 | `device_ffi_test`、`cpp_consumes_rust_device`、`mathdx_ffi_test`、`cutile_inter_kernel` | Rust↔C++/CCCL/MathDx 经 LTOIR 互调 | u5/u6 |
| 跨 crate / 库 crate | `cross_crate_kernel`、`cross_crate_embedded`、`cuda_module_in_lib` | 内核定义在 lib crate，bundle 进 bin | u3-l2 |
| 设备全局/常量内存 | `device_global`、`constant_memory_simple`、`constant_memory_coeffs` | `__device__` 全局、常量内存 | u5 |
| 数学/libm/libdevice | `libdevice_math`、`libm_math`、`math_atan`、`extern_libdevice` | 设备端数学函数降级 | u2/u6 |
| 数据结构 | `hashmap`、`hashmap_v2`、`hashmap_v3` | GPU 上构建哈希表 | （扩展阅读） |
| 负向 / 契约测试 | `error_*`、`cuda_module_contract` | 断言编译器拒绝不安全写法 | u7 |
| 调试 | `debug`、`printf`、`inline_ptx` | `cuda-gdb`、`printf`、内联 PTX | u7 |

> 说明：上表中「对应后续讲义」给出的是本手册里最相关的讲义编号，方便你按主题跳转；部分高级主题（如 GEMM SoL、TMA、打包原子）集中在专家层 U5。本轮（#327/#328/#329）为 mma.sync 家族新增了 f16 m16n8k16、tf32 m16n8k8、s8 m16n8k32 三条 intrinsic（与上一轮已有的 bf16 m16n8k16、f64 m8n8k4 合流），它们在 U5-l6、U6-2/U6-3 深入讲解；嵌套模块能力则在 U2-l1 展开。

#### 4.2.3 源码精读：用五个示例验证矩阵

为了证明这张矩阵不是「纸上谈兵」，我们快速读五个分别代表「异步」「标量原子」「集群」「打包原子」「嵌套模块」的示例头部注释与关键代码。**注意五者的启动调用都被 `unsafe` 包裹**，这是 #318 后的统一约定。

**(1) 异步：`async_vecadd`**

它的模块级注释明确写了它演示的是 `cuda-async` 执行模型——`vecadd_async` 返回一个**惰性** `DeviceOperation`，直到 `.sync()` / `.await` 才真正调度到 GPU：

```rust
//! - `vecadd_async` returns a lazy `DeviceOperation`, no GPU work yet
//! - `.await` schedules it on a round-robin stream pool and waits
//! - `.sync()` does the same but blocks the calling thread
//! - `and_then` chains operations on the same stream
//! - `zip!` runs independent operations on the same stream
```

参见 [crates/rustc-codegen-cuda/examples/async_vecadd/src/main.rs:6-17](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/async_vecadd/src/main.rs#L6-L17)。它的内核本身和同步版 `vecadd` 一模一样（`#[kernel] pub fn vecadd(a, b, mut c)`），区别全在宿主侧——用 `vecadd_async(...)?.sync()?` 取代同步的 `module.vecadd(...)`，且启动包在 `unsafe` 块中：

```rust
// SAFETY: launch shape/resources match the kernel; buffers cover its accesses.
unsafe {
    module.vecadd_async(
        LaunchConfig::for_num_elems(N as u32),
        &a_dev,
        &b_dev,
        &mut c_dev,
    )
}?
.sync()?;
```

参见 [crates/rustc-codegen-cuda/examples/async_vecadd/src/main.rs:95-104](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/async_vecadd/src/main.rs#L95-L104)。这印证了矩阵里「异步运行时」一行的能力描述。

**(2) 标量原子：`atomics`**

`atomics` 的注释直接给出了一份「20 个测试」的清单，按两个 Phase 组织，覆盖 `DeviceAtomicU32/I32/U64/I64/F32/F64`、`BlockAtomicU32`（块作用域）、乃至标准库 `core::sync::atomic::AtomicU32`（系统作用域），以及 `fetch_add/sub/and/or/xor/min/max`、`swap`、`compare_exchange` 等读改写（RMW）操作：

```rust
//! **Phase 1 (DeviceAtomicU32/I32, load/store/fetch_add/CAS):**
//!  1. `atomic_fetch_add_test` -- DeviceAtomicU32 fetch_add (Relaxed)
//!  ...
//! 20. `core_atomic_fetch_add_test` -- core::sync::atomic::AtomicU32 (system scope)
```

参见 [crates/rustc-codegen-cuda/examples/atomics/src/main.rs:8-37](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/atomics/src/main.rs#L8-L37)。测试 1 是最典型的「N 个线程各 `fetch_add(1)`，最终计数器应等于 N」模式：

```rust
let atomic_counter = unsafe { &*(counter.as_ptr() as *const DeviceAtomicU32) };
let old = atomic_counter.fetch_add(1, AtomicOrdering::Relaxed);
```

参见 [crates/rustc-codegen-cuda/examples/atomics/src/main.rs:59-73](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/atomics/src/main.rs#L59-L73)。注意注释里提到的「fence-splitting workaround」——LLVM NVPTX 后端在 `atomicrmw` 上会丢掉排序信息，cuda-oxide 用 `fence release + atomicrmw monotonic + fence acquire` 来绕过（见测试 4 的注释，[crates/rustc-codegen-cuda/examples/atomics/src/main.rs:138-141](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/atomics/src/main.rs#L138-L141)）。

`atomics` 在宿主侧用的是「按 PTX 文件名加载」的模式：先用 `ctx.load_module_from_file("atomics.ptx")` 读出模块，再 `kernels::from_module(module)` 把类型化启动器绑定上去；随后每个测试的启动都包在 `unsafe` 里：

```rust
let module = ctx
    .load_module_from_file("atomics.ptx")
    .expect("Failed to load PTX module");
let module = kernels::from_module(module).expect("Failed to initialize typed CUDA module");
...
// SAFETY: launch shape/resources match the kernel; buffers cover its accesses.
unsafe { module.atomic_fetch_add_test((stream).as_ref(), cfg, &counter_dev, &mut out_dev) }
    .expect("Kernel launch failed");
```

参见 [crates/rustc-codegen-cuda/examples/atomics/src/main.rs:546-549](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/atomics/src/main.rs#L546-L549) 与 [crates/rustc-codegen-cuda/examples/atomics/src/main.rs:568-570](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/atomics/src/main.rs#L568-L570)。这条 `load_module_from_file` + `from_module` 路径与 `vecadd` 的内嵌 bundle `kernels::load(&ctx)` 路径是两种并存的加载方式（详见 u3-l2）。

**(3) 集群：`cluster`**

`cluster` 的头部注释直接写明硬件门槛与三大演示点：

```rust
//! - Cluster special registers (`cluster_ctaidX`, `cluster_nctaidX`, etc.)
//! - Cluster synchronization (`cluster_sync`)
//! - Distributed shared memory (`map_shared_rank`)
//!
//! **Hardware Requirements:** Hopper (H100, H200) or newer GPUs with sm_90+
```

参见 [crates/rustc-codegen-cuda/examples/cluster/src/main.rs:6-15](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cluster/src/main.rs#L6-L15)。它用 `#[cluster_launch(4, 1, 1)]` 属性让编译器在 PTX 里发射 `.reqnctapercluster 4, 1, 1`（见 [cluster/src/main.rs:28-50](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cluster/src/main.rs#L28-L50)），并通过 `cluster::dsmem_read_u32(...)` 跨块读取邻居的共享内存，实现环形交换（见 [cluster/src/main.rs:128-157](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cluster/src/main.rs#L128-L157)）。宿主侧同样用 `load_module_from_file("cluster.ptx")` + `from_module`，并在 `unsafe` 块里经 `cuLaunchKernelEx` 启动（见 [cluster/src/main.rs:224-254](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cluster/src/main.rs#L224-L254)）。

**(4) 打包原子：`packed_atomic_add`**

它的模块注释一句话点明演示的是 f16x2 与 bf16x2 的**端到端打包全局原子加法**，并标出硬件门槛：

```rust
//! End-to-end packed f16x2 and bf16x2 global atomic-add example.
//!
//! The native bf16x2 instruction makes this combined example require sm_90.
```

参见 [crates/rustc-codegen-cuda/examples/packed_atomic_add/src/main.rs:6-8](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/packed_atomic_add/src/main.rs#L6-L8)。它的内核 `add_packed` 接收一个 `*mut u32` 基址，每个线程对**同一个 32-bit 字**分别做 f16x2 与 bf16x2 原子加——也就是把两条 16-bit lane 打包进一个字、各自独立原子累加：

```rust
#[kernel]
pub fn add_packed(base: *mut u32) {
    if thread::index_1d().get() >= THREADS as usize {
        return;
    }
    unsafe {
        let _ = atom_add_f16x2(base, PACKED_F16_ONE);
        let _ = atom_add_bf16x2(base.add(1), PACKED_BF16_ONE);
    }
}
```

参见 [crates/rustc-codegen-cuda/examples/packed_atomic_add/src/main.rs:25-35](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/packed_atomic_add/src/main.rs#L25-L35)。宿主侧用内嵌 bundle `kernels::load(&ctx)` 加载，启动同样包在 `unsafe` 中（见 [packed_atomic_add/src/main.rs:63-72](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/packed_atomic_add/src/main.rs#L63-L72)）。

> 直觉：相对于 `atomics` 里「一个字 = 一个原子值」，`packed_atomic_add` 演示的是「一个 32-bit 字里塞两个 16-bit 值，硬件仍保证**两条 lane 各自原子**」。这正是矩阵「GPU 原子（打包）」一行的能力。注意它和标量原子不能混用同一地址（详见 u5-l2）。

**(5) 嵌套模块：`cuda_module_nested`（本轮 #324 新增）**

这是本轮随「`#[cuda_module]` 收集嵌套 inline 模块」能力一起新增的示例。它的模块级注释直接说清了演示点：kernel 分布在三层嵌套里，根模块**故意不直接放 kernel**，每个命名空间各拥有一个 `LoadedModule` 启动视图，子视图通过 `LoadedModule::from_parent` 借用同一份已加载的 CUDA 模块：

```rust
//! - `init::fill_index`, `scale::scale_by`, and `offset::offset_by` one level
//!   down,
//! - `post::double::double_all` two levels down.
//!
//! Each namespace owns a `LoadedModule` launcher view. Child views borrow the
//! same loaded CUDA module through `LoadedModule::from_parent`.
```

参见 [crates/rustc-codegen-cuda/examples/cuda_module_nested/src/main.rs:6-17](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cuda_module_nested/src/main.rs#L6-L17)。设备侧的结构长这样——根 `mod kernels` 里嵌着 `init`、`scale`、`offset`、`post`（其中 `post` 又嵌着 `double`），每个子模块各放一个 `#[kernel]`：

```rust
#[cuda_module]
mod kernels {
    /// Inline nested module: out[i] = i.
    /// The root intentionally has no direct kernel. ...
    pub mod init {
        use cuda_device::{DisjointSlice, kernel, thread};
        #[kernel]
        pub fn fill_index(mut out: DisjointSlice<f32>) { ... }
    }
    ...
}
```

参见 [crates/rustc-codegen-cuda/examples/cuda_module_nested/src/main.rs:23-40](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cuda_module_nested/src/main.rs#L23-L40)。宿主侧先 `kernels::load(&ctx)` 把整棵树的内嵌制品加载并钉住（哪怕根模块自己没有 kernel，制品完全由后代拥有也能加载），再为每个命名空间用 `LoadedModule::from_parent` 构造子视图，最深一层 `post::double` 则从其父视图 `post` 再 `from_parent` 一次：

```rust
let module = kernels::load(&ctx).expect("Failed to load embedded CUDA module");
let init =
    kernels::init::LoadedModule::from_parent(&module).expect("Failed to bind init launchers");
...
let double = kernels::post::double::LoadedModule::from_parent(&post)
    .expect("Failed to bind double launchers");
```

参见 [crates/rustc-codegen-cuda/examples/cuda_module_nested/src/main.rs:99-110](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cuda_module_nested/src/main.rs#L99-L110)。最后所有启动都在同一个 `unsafe` 块里完成，由一条总 `SAFETY:` 注释自证「每个启动都是一维、覆盖 N 个元素、缓冲区覆盖 kernel 的全部访问」：

```rust
// SAFETY: every launch is one-dimensional over N elements, and each
// input/output buffer covers the full range accessed by its kernel.
unsafe {
    init.fill_index(&stream, config, &mut idx_dev).expect("fill_index launch failed");
    scale.scale_by(&stream, config, &idx_dev, &mut scaled_dev).expect("scale_by launch failed");
    ...
}
```

参见 [crates/rustc-codegen-cuda/examples/cuda_module_nested/src/main.rs:112-127](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cuda_module_nested/src/main.rs#L112-L127)。这正是矩阵「嵌套模块组织」一行的能力：把一个大型内核集合按命名空间拆开，启动视图也随之分层。

#### 4.2.4 代码实践

**实践目标**：从矩阵里挑 3 个分属不同主题的示例，验证它们各自的「能力一句话」（建议含本轮新增的 `cuda_module_nested` 与 `packed_atomic_add`）。

**操作步骤**：

1. 选三个不同主题的示例，例如 `cuda_module_nested`（嵌套模块）、`async_vecadd`（异步）、`packed_atomic_add`（打包原子）。
2. 分别打开它们的 `src/main.rs` 顶部 `//!` 注释，找到一句能概括其能力的话。
3. 用 `cargo oxide build <name>` 分别编译（**只编译、不运行，无需 GPU**），确认它们都能通过 cuda-oxide 流水线产出 PTX。
   - `packed_atomic_add` 内部用到的 native bf16x2 指令需 sm_90+，但 `build` 阶段只要 `llc-21+` 即可编出 PTX，运行才需要 Hopper+ GPU（见 4.3）。
   - `cuda_module_nested` 是纯 f32 一维内核，对架构没有特殊要求，是最容易编译通过的本轮新示例。

**需要观察的现象**：

- `cuda_module_nested` 的注释强调「根模块无直接 kernel、各命名空间分层启动」。
- `async_vecadd` 强调「惰性 `DeviceOperation`」。
- `packed_atomic_add` 强调「f16x2 / bf16x2 打包原子、需 sm_90+」。
- 三者的启动调用都被 `unsafe { ... }` 包裹。

**预期结果**：你能用一句话概括每个示例演示的能力，且这三句话分别落在矩阵的不同行。

**待本地验证**：`cargo oxide build packed_atomic_add` 在你这台机器上能否成功，取决于是否有 `llc-21+`；若无，记录下报错信息，留到 4.3 节对照。

#### 4.2.5 小练习与答案

**练习 1**：`async_vecadd` 的内核函数体与同步版 `vecadd` 几乎相同，区别在哪一层？

> **参考答案**：区别全在**宿主侧启动方式**。同步版用 `module.vecadd(&stream, config, ...)`（绑定到具体流、立即提交）；异步版用 `unsafe { module.vecadd_async(config, ...) }?.sync()?`，返回惰性 `DeviceOperation`，由 `cuda-async` 的调度策略在 `.sync()`/`.await` 时才选流并提交。设备端 PTX 是同一份计算逻辑。

**练习 2**：`atomics` 示例里为什么要把 `&[u32]` 用 `unsafe { &*(... as *const DeviceAtomicU32) }` 转成原子引用，而不是直接传一个原子类型进 kernel？

> **参考答案**：这是「内部可变性」（interior mutability）模式：宿主分配的是普通 `DeviceBuffer<u32>`，kernel 内部把它**重新解释**为原子视图来执行 RMW，从而让多个线程能安全地对同一地址做 `fetch_add`。直接传原子类型会牵涉设备端原子类型的 ABI 与布局，而 reinterpret 一个普通 `&[u32]` 更贴近底层 `atomicrmw` 指令的内存模型。

**练习 3**：`packed_atomic_add` 与 `atomics` 都做「原子加」，本质区别是什么？为何注释说它需要 sm_90+？

> **参考答案**：`atomics` 是「一个 32/64-bit 字 = 一个原子值」的标量原子；`packed_atomic_add` 是把**两个 16-bit 浮点值打包进一个 32-bit 字**，靠 `atom_add_f16x2` / `atom_add_bf16x2` 让硬件保证两条 lane 各自原子累加。其中 f16x2 打包原子加法从 sm_70 起支持，而 **native bf16x2 打包原子加法需要 sm_90+（Hopper）**，所以这个同时演示两者的组合示例整体要求 sm_90+（见 `packed_atomic_add/src/main.rs:7-8`）。

**练习 4**：`cuda_module_nested` 的根 `mod kernels` 里没有直接放 kernel，为什么 `kernels::load(&ctx)` 仍然能成功？

> **参考答案**：因为 #324 让 `#[cuda_module]` 会**递归收集嵌套 inline 模块里的 `#[kernel]`**，整棵模块树的内核会被打包进同一个内嵌制品。`kernels::load(&ctx)` 钉住的是「整棵树共有的那份制品」，它完全由后代（`init`/`scale`/`offset`/`post::double`）拥有；根模块自己有没有 kernel 不影响制品的存在。加载后再用 `LoadedModule::from_parent` 逐层构造各命名空间的启动视图。

---

### 4.3 硬件要求（sm_xx 与 arch 推导）

#### 4.3.1 概念说明

示例库里有一条隐含的「阶梯」：越靠后的高级示例，对 GPU 架构（`sm_xx`，即 CUDA compute capability）和工具链（`llc` 版本）的要求越高。理解这条阶梯，能帮你避免「明明代码对、却编译/运行失败」的困惑。

关键术语：

- **`sm_xx` / compute capability**：NVIDIA GPU 的架构版本号，如 `sm_80`（Ampere A100）、`sm_90`（Hopper H100）、`sm_100`/`sm_100a`（Blackwell）。`xx` 越大，支持的指令集越新。
- **`--arch`**：`cargo oxide build/run` 的参数，显式指定目标架构，如 `--arch sm_90`。
- **`CUDA_OXIDE_DEVICE_ARCH`**：环境变量，对架构的**建议性 hint**。
- **`CUDA_OXIDE_TARGET`**：环境变量，对架构的**硬覆盖**（优先级最高）。

这三者的关系在 u1-l3 已建立：`CUDA_OXIDE_TARGET` 硬覆盖 > `--arch` 显式参数 > `CUDA_OXIDE_DEVICE_ARCH` 建议 hint > 自动探测。

#### 4.3.2 核心流程

架构选择的优先级可以表示为：

\[
\text{最终 arch} = \begin{cases}
\text{CUDA\_OXIDE\_TARGET} & \text{若已设置（硬覆盖）}\\
\text{--arch} & \text{若命令行显式给出}\\
\text{CUDA\_OXIDE\_DEVICE\_ARCH} & \text{若已设置（建议）}\\
\text{自动探测 GPU 0 的 compute capability} & \text{否则}
\end{cases}
\]

不同示例的最低架构门槛（来自源码注释）：

| 示例 / 能力 | 最低架构 | 出处 |
|-------------|----------|------|
| 基础示例（`vecadd`、`generic`、`cuda_module_nested` 等） | sm_80 起的目标基线 | 项目默认目标 |
| `cluster`（线程块集群 / DSMEM） | **sm_90+**（Hopper） | cluster 注释 |
| `packed_atomic_add`（native bf16x2 打包原子） | **sm_90+**（Hopper） | packed_atomic_add 注释 |
| s8 m16n8k32 mma.sync（本轮 #329 新增） | **sm_80+**（Ampere mma） | wmma.rs 注释 |
| `tcgen05` / TMA / WGMMA / `gemm_sol_final` | **sm_100 / sm_100a**（Blackwell） | tcgen05 Cargo.toml 注释 |
| `atomics` 的 f64 原子 | sm_60+（项目实际目标 sm_80+） | atomics 测试 15 注释 |

此外还有一条**工具链硬约束**：cuda-oxide 会发射 TMA / tcgen05 / WGMMA 等 Hopper/Blackwell 专用 intrinsic，**LLVM 20 及以下的 `llc` 处理不了**，必须 `llc-21+`。README 原文：

> We emit TMA / tcgen05 / WGMMA intrinsics that `llc` from LLVM 20 and earlier can't handle. Simple kernels might still work with an older `llc`, but anything Hopper / Blackwell needs 21+.

参见 [README.md:198-199](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/README.md#L198-L199)。

「能编译」与「能运行」是两件事：

- **能编译**：只要有工具链（nightly + `llc-21+` + clang），即便机器上**没有 GPU**，`cargo oxide build` 也能产出 PTX。
- **能运行**：`cargo oxide run` 需要一张**架构达标**的 GPU 才能加载 PTX 并启动 kernel。不达标时代码可能编译通过却在运行时跳过或报错。

#### 4.3.3 源码精读

`tcgen05` 的 `Cargo.toml` 注释直接点明它是 Blackwell 专用：

```toml
# SM100+ tcgen05 (Tensor Core Gen 5) example - Blackwell only.
# Build with:
#   cargo oxide run tcgen05
```

参见 [crates/rustc-codegen-cuda/examples/tcgen05/Cargo.toml:11-13](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/tcgen05/Cargo.toml#L11-L13)。

`packed_atomic_add` 与 `cluster` 则都在**运行时**自查 compute capability，不达标就优雅跳过，而不是崩溃。`packed_atomic_add` 的写法：

```rust
let (major, minor) = ctx.compute_capability().expect("compute capability");
if major < 9 {
    println!(
        "skipping: native bf16x2 atomic add requires sm_90+ (device is sm_{major}{minor})"
    );
    return;
}
```

参见 [crates/rustc-codegen-cuda/examples/packed_atomic_add/src/main.rs:54-60](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/packed_atomic_add/src/main.rs#L54-L60)；`cluster` 用的是同样的 `if major < 9 { ... return; }` 模式（见 [cluster/src/main.rs:215-221](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cluster/src/main.rs#L215-L221)）。这是一种值得借鉴的写法：把硬件门槛既写进注释（给人看），又在运行时显式检查（给程序看）。

`cargo oxide build` 子命令的 `--arch` 参数定义如下（其上方的 `Sanitize` 子命令结构相同），证实它接受 `sm_90` / `sm_100` / `sm_120` 这样的值，且「只编译不运行」：

```rust
/// Build an example or project (compile only, don't run)
Build {
    /// Example name (required in workspace, optional for standalone projects)
    example: Option<String>,
    /// Target architecture (e.g., sm_90, sm_100, sm_120)
    #[arg(long)]
    arch: Option<String>,
    ...
}
```

参见 [crates/cargo-oxide/src/main.rs:112-143](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/main.rs#L112-L143)。紧挨在它上方的 `Sanitize { ... }` 子命令（[main.rs:82-110](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/main.rs#L82-L110)）则用同样的 `--arch`/`--tool` 字段驱动 Compute Sanitizer，详见 u1-l3、u7-l3。

#### 4.3.4 代码实践

**实践目标**：体会「能编译 ≠ 能运行」，并学会用 `--arch` 显式指定目标架构。

**操作步骤**：

1. 在**没有 GPU**（或 GPU 不达标）的机器上运行 `cargo oxide build vecadd`，确认它能编译通过并产出 PTX（可在 `target/` 下找到 `.ptx`/`.ll` 制品）。
2. 接着 `cargo oxide build packed_atomic_add --arch sm_90`（或 `cargo oxide build cluster --arch sm_90`），显式按 Hopper 架构编译。
3. 若你的机器有 GPU，运行 `cargo oxide run packed_atomic_add`，观察它在 `major < 9` 时的「skipping」输出（即 4.3.3 那段 self-check 生效）。

**需要观察的现象**：

- 步骤 1：无 GPU 也能完成编译，说明 `build` 只走 codegen 流水线、不触碰设备。
- 步骤 2：指定 `--arch sm_90` 后，`packed_atomic_add` 的 PTX 里应出现打包原子加法相关指令。
- 步骤 3：GPU 不达标时程序**主动跳过**而非崩溃。

**预期结果**：你理解了「编译期架构（`--arch`）」与「运行期架构（GPU 实际 compute capability）」是两个独立维度。

**待本地验证**：步骤 2 中 PTX 是否真的含打包原子指令，建议用 `cargo oxide pipeline packed_atomic_add --arch sm_90` 打开生成的 `.ptx` 文件核对。

#### 4.3.5 小练习与答案

**练习 1**：`CUDA_OXIDE_DEVICE_ARCH=sm_80` 与 `CUDA_OXIDE_TARGET=sm_90` 同时设置时，最终用哪个架构？

> **参考答案**：用 `sm_90`。`CUDA_OXIDE_TARGET` 是**硬覆盖**，优先级最高；`CUDA_OXIDE_DEVICE_ARCH` 只是**建议性 hint**，会被前者压过（见 u1-l3）。

**练习 2**：为什么 README 强调 Hopper/Blackwell 示例需要 `llc-21+`？

> **参考答案**：cuda-oxide 会发射 TMA / tcgen05 / WGMMA 等新型 intrinsic，LLVM 20 及更早的 `llc` 不认识这些指令，会在后端报错；只有 `llc-21+` 才能正确把它们 lowering 到 PTX。简单内核碰巧可能用旧 `llc` 也能过，但任何 Hopper/Blackwell 特性都必须 21+。

---

### 4.4 选型指引：根据目标选起点

#### 4.4.1 概念说明

有了能力矩阵（4.2）和硬件阶梯（4.3），最后一步是**根据你自己的学习目标**选 2–3 个起点示例。这一节给出一份「目标 → 示例 → 后续讲义」的对照，帮你把本讲建立的地图转化为行动。

#### 4.4.2 核心流程：按学习目标分流

| 你的目标 | 先读这几个示例 | 再去这本讲义 |
|----------|----------------|--------------|
| 「先把内核写顺」 | `vecadd` → `generic` → `host_closure` | u2-l1（宏）、u2-l2（索引安全）、u2-l6（泛型/闭包） |
| 「想用模块组织一堆内核」 | `vecadd` → `cuda_module_nested` → `cuda_module_in_lib` | u2-l1（宏与嵌套模块） |
| 「搞懂共享内存与协作」 | `sharedmem` → `dynamic_smem` → `barrier` | u2-l3（共享内存与同步） |
| 「搞懂宿主运行时」 | `vecadd` → `async_vecadd` → `async_mlp` | u3-l1（cuda-core）、u3-l3（异步模型）、u3-l4（调度组合子） |
| 「想看编译流水线长啥样」 | `vecadd`（配 `cargo oxide pipeline vecadd`） | u4（编译流水线总览） |
| 「玩 Hopper/Blackwell 高级特性」 | `packed_atomic_add` → `cluster` → `cp_async_small` → `tcgen05`/`wgmma` | u5（专家层设备能力） |
| 「想做设备 FFI / 二次开发」 | `device_ffi_test` → `mathdx_ffi_test` | u5/u6 |
| 「想给编译器加一个 intrinsic」 | 任一简单示例 + 阅读 `error_*` 契约 | u6（编译器深潜）、u7（测试与工程化） |

选型三条经验法则：

1. **从 `vecadd` 出发永远没错**。它是所有主题的最小公分母。
2. **先选「设备端」还是「宿主端」方向**。前者进 U2，后者进 U3，两条线在 U4（编译流水线）汇合。
3. **硬件不达标就先用 `build` 代替 `run`**。即便没有 Hopper/Blackwell，`cargo oxide build` 也能让你读懂 PTX、理解机制。

#### 4.4.3 源码精读：两个典型起点的最小内核

**起点 A：泛型内核（`generic`）**。它的 `scale<T>` 内核展示「同一个泛型 kernel 被单态化成多个 PTX 入口」：

```rust
#[kernel]
pub fn scale<T: Copy + Mul<Output = T>>(factor: T, input: &[T], mut out: DisjointSlice<T>) {
    let idx = thread::index_1d();
    let idx_raw = idx.get();
    if let Some(out_elem) = out.get_mut(idx) {
        *out_elem = input[idx_raw] * factor;
    }
}
```

参见 [crates/rustc-codegen-cuda/examples/generic/src/main.rs:42-49](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/generic/src/main.rs#L42-L49)。宿主侧分别用 `module.scale::<f32>(...)` 和 `module.scale::<i32>(...)` 调用（包在 `unsafe` 中），会触发 rustc 单态化，各生成一个独立的设备入口。

**起点 B：带捕获闭包（`host_closure`）**。它的 `map<T, F: Fn(T)->T + Copy>` 内核把闭包当成一个 byval 参数整体传入：

```rust
#[kernel]
pub fn map<T: Copy, F: Fn(T) -> T + Copy>(f: F, input: &[T], mut out: DisjointSlice<T>) {
    ...
    *out_elem = f(input[idx_raw]);
}
```

参见 [crates/rustc-codegen-cuda/examples/host_closure/src/main.rs:70-77](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/host_closure/src/main.rs#L70-L77)。这两个示例是进入 U2「编写 GPU 内核」单元的最佳预习材料。

#### 4.4.4 代码实践

**实践目标**：为自己选定 2 个后续起点，并用 `cargo oxide build` 验证它们可编译（建议本轮把 `cuda_module_nested` 作为其中一个，它是观察「嵌套模块」这条新能力线的最短路径）。

**操作步骤**：

1. 对照上面的「目标 → 示例」表，确定你接下来最想深入的方向（设备端 / 宿主端 / 编译器）。
2. 为该方向挑 2 个示例，分别 `cargo oxide build <name>` 编译。
3. 给每个示例写一句话总结（这就是本讲规格里要求的「3 个示例各一句话」实践的核心）。

**需要观察的现象**：编译成功即代表你的工具链（nightly + `llc-21+` + clang）配置正确；若失败，先用 `cargo oxide doctor` 体检（见 u1-l3）。

**预期结果**：你手里有 2 个「已验证可编译」的示例，作为进入 U2 或 U3 的现成实验台。

**待本地验证**：具体编译耗时与产物路径（`target/` 下的 `.ptx`/`.ll`）以本地为准。

#### 4.4.5 小练习与答案

**练习 1**：如果你的机器只有一张 sm_80 的 GPU，但你想学 `cluster`（sm_90+）或 `packed_atomic_add`（sm_90+），该怎么办？

> **参考答案**：先用 `cargo oxide build cluster --arch sm_90`（或 `cargo oxide build packed_atomic_add --arch sm_90`）**编译**并阅读生成的 PTX（学习机制），运行层面则只能等拿到 Hopper+ 设备。`build` 不需要达标的 GPU，所以「读懂」永远可行。

**练习 2**：本讲反复强调「能编译 ≠ 能运行」，请用 `packed_atomic_add` 举一个例子说明二者为何可能分离。

> **参考答案**：`packed_atomic_add` 用到的 native bf16x2 打包原子加法指令需要 sm_90+，但项目整体目标 sm_80+。在一张 sm_80 的 GPU 上，代码可以正常**编译**成 PTX（编译期不检查运行期硬件），但**运行**时 native bf16x2 指令不被支持——示例的 `if major < 9 { ... return; }` 自检会优雅跳过，避免触发非法指令。这正是「编译期架构」与「运行期架构」分离的体现。

**练习 3**：`cuda_module_nested` 与 `vecadd` 都用 `kernels::load(&ctx)` 加载，但 `atomics`/`cluster` 用的是 `load_module_from_file` + `from_module`。这两条路径有何区别？

> **参考答案**：`kernels::load(&ctx)` 走的是**内嵌 bundle** 路径——PTX 在编译期被打包进可执行文件的 `.oxart` 段，运行时自动发现并加载（见 u3-l2）。`load_module_from_file("X.ptx")` + `kernels::from_module(module)` 走的是**按文件名加载**路径——先把 PTX 写到一个独立的 `X.ptx` 文件，运行时按路径读入一个 `CudaModule`，再用 `from_module` 把宏生成的类型化启动器绑定上去。两者都把启动调用包在 `unsafe` 中；区别只在「制品从哪里来」。

---

## 5. 综合实践

把本讲的四块知识串起来，完成下面这个小任务：

**任务：制作一份「个人版 cuda-oxide 能力速查卡」。**

1. **目录勘察**：`ls crates/rustc-codegen-cuda/examples/`，把条目分成三类——正面 demo、`error_*` 负面测试、其它（FFI/烟雾测试），各数一下数量。
2. **矩阵提炼**：从 4.2 的能力矩阵里挑出你最感兴趣的 **3 个主题**，每个主题选 **1 个示例**（建议含 `cuda_module_nested` 与 `packed_atomic_add`）。
3. **编译验证**：对选中的 3 个示例分别 `cargo oxide build <name>`（注意高级示例加 `--arch sm_90`），确认全部能编译；若有失败，记录报错并判断是工具链问题（`llc` 版本）还是架构问题。
4. **一句话总结**：为每个示例写一句话能力总结，并标注它的最低 `sm_xx` 与对应的后读讲义。
5. **选型决策**：基于上面结果，写下你接下来要进入的单元（U2 设备端 / U3 宿主端 / U4 编译流水线）及理由。

**交付物**：一张含「示例名 | 能力一句话 | 最低架构 | 后读讲义 | 是否编译通过」五列的小表，外加一段 3–5 句的选型理由。

> 如果没有 GPU，全程用 `build` 即可完成；如果某示例需要 Hopper/Blackwell 而你本地 `llc` 不足，把该行标为「待本地验证」并说明原因，不要假装它通过了。

## 6. 本讲小结

- `examples/` 目录下有 133 个子目录，README 标称「60+」；每个示例都是带 `[workspace]` 的独立 standalone crate，靠 `cargo oxide run/build/pipeline/sanitize <name>` 驱动。
- 示例库混含「正面 demo」与「`error_*` 负面契约测试」两类，后者断言编译器拒绝不安全写法。
- **本轮 #318 起，所有示例的内核启动调用都统一包在 `unsafe { ... }` 中**并由 `SAFETY:` 注释自证，因为 raw `LaunchConfig` 是未经证明的原始数据；免 `unsafe` 的受检启动（`#[launch_contract]` + `prepare_*` → `PreparedLaunch`）在 u2-l1/u2-l4 展开。
- 按能力主题可把示例归为单源基础、泛型/闭包、标量原子、打包原子、共享内存、warp、张量核 mma.sync（**本轮新增 f16 m16n8k16 / tf32 m16n8k8 / s8 m16n8k32 intrinsic**）、**嵌套模块（本轮新增 `cuda_module_nested`）**、Hopper 集群/DSMEM、异步拷贝、Blackwell 张量核/TMA、GEMM SoL、异步运行时、设备 FFI、跨 crate、常量内存、设备数学、数据结构、调试等主题——这就是你的能力地图。
- 硬件要求呈阶梯：基础示例（含 `cuda_module_nested`）sm_80 基线，`cluster` 与 `packed_atomic_add` 需 sm_90+，`tcgen05`/TMA/WGMMA 需 sm_100/sm_100a；架构选择优先级为 `CUDA_OXIDE_TARGET`（硬覆盖）> `--arch` > `CUDA_OXIDE_DEVICE_ARCH`（建议）> 自动探测。
- 「能编译」只需工具链（nightly + `llc-21+` + clang），「能运行」还需架构达标的 GPU；`cluster`、`packed_atomic_add` 等示例会在运行时自查 compute capability 并优雅跳过。
- 选型三原则：从 `vecadd` 出发；先定设备端还是宿主端方向；硬件不达标就用 `build` 代替 `run`。

## 7. 下一步学习建议

入门单元（U1）到此结束。接下来根据你在 4.4 选定的方向：

- **想写 GPU 内核** → 进入 **U2《编写 GPU 内核》**，从 `u2-l1 #[kernel] 与 #[cuda_module] 宏` 开始（其中专门讲了嵌套模块与启动契约），预习材料就是本讲提到的 `vecadd` / `generic` / `host_closure` / `cuda_module_nested`。
- **想搞宿主运行时** → 进入 **U3《宿主运行时》**，从 `u3-l1 cuda-core 安全封装` 开始，预习材料是 `vecadd` / `async_vecadd`。
- **想懂编译原理** → 进入 **U4《编译流水线总览》**，建议先跑一遍 `cargo oxide pipeline vecadd`，带着中间产物再读。

无论选哪条线，U2/U3 都会在 U4 汇合；而要挑战 Hopper/Blackwell 高级特性（`cluster`、`packed_atomic_add`、`tcgen05`、`wgmma`、新增的 f16/tf32/s8 mma）或给编译器加 intrinsic，则要等到专家层 U5–U7。
