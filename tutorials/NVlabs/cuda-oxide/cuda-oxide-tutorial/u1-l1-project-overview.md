# 项目定位与编译流水线总览

## 1. 本讲目标

本讲是整本学习手册的起点。读完本讲，你应当能够：

- 用一句话说清楚 **cuda-oxide 是什么**、它处于什么阶段（alpha）、解决什么问题。
- 画出一段 Rust 代码「从源码到 GPU 上运行的 PTX」所经过的完整阶段。
- 区分两条编译路径：**host 路径**（交给标准 LLVM 后端）与 **device 路径**（交给 cuda-oxide 流水线）。
- 看懂 `rustc-codegen-cuda/src/lib.rs` 顶部那张 ASCII 架构图，并理解它为什么「拦截 codegen、其余全转发」。
- 注意到 README 头部示例里**启动内核被包在 `unsafe` 块中**——`LaunchConfig` 是未经证明的「原始数据」，并知道存在 `#[launch_contract]` 这条「把证明移进安全 API」的受检启动路径。

本讲只做「鸟瞰」，不深入任何一个阶段的实现细节——那些留给后续单元。

## 2. 前置知识

在开始之前，最好对下面几个名词有最粗浅的印象；如果完全没听过也没关系，本讲会顺带解释。

- **GPU / CUDA**：显卡既能画图也能做通用计算。CUDA 是 NVIDIA 提供的通用 GPU 编程平台。GPU 上的一个函数叫**内核（kernel）**，会被成千上万个线程同时执行（SIMT 模型）。
- **PTX**：一种接近汇编的「GPU 中间指令文本」，是 NVIDIA 工具链（`nvcc` / `llc`）认可的输入。内核最终要变成 PTX（再编译成机器码）才能在 GPU 上跑。
- **rustc**：Rust 官方编译器。它先把源码解析成 HIR，再做类型检查，再生成 **MIR**（Mid-level IR，中级中间表示），最后才到「代码生成（codegen）」阶段产出机器码。
- **代码生成后端（codegen backend）**：rustc 的代码生成阶段是可插拔的。默认后端把 MIR 翻译成 LLVM IR，再用 LLVM 产出机器码。cuda-oxide 就是一个**自定义后端**，替换掉这一阶段。
- **LLVM**：一套成熟的编译器后端框架，rustc 默认用它生成 x86_64 等平台机器码。
- **单源编译（single-source）**：host（CPU）代码和 device（GPU）代码写在同一个 `.rs` 文件里，一次编译同时产出两部分。C++ 世界里 NVIDIA 的 `nvc++` 就支持这种模式；cuda-oxide 把它带到了 Rust。

> 一句话直觉：cuda-oxide 让你**用纯 Rust 写 GPU 内核**，不需要 CUDA C、不需要 DSL、不需要单独的 `.cu` 文件——host 和 device 在同一个 Rust 源文件里，一条 `cargo oxide run` 全部搞定。

## 3. 本讲源码地图

本讲只涉及两个核心文件，但它们是整个项目的「门面」：

| 文件 | 作用 |
|------|------|
| `README.md` | 项目定位、状态（alpha）、快速上手、能力矩阵、crate 概览表。理解「cuda-oxide 是什么」的入口。 |
| `crates/rustc-codegen-cuda/src/lib.rs` | 自定义 rustc 后端的实现。文件顶部有完整 ASCII 架构图，包含入口函数 `__rustc_codegen_backend` 和分流核心 `codegen_crate`。 |

后续讲义会逐层展开其它 crate（`cuda-device`、`cuda-core`、`cargo-oxide`、编译器各阶段等），但本讲只看这两处。

## 4. 核心概念与源码讲解

### 4.1 项目定位与状态

#### 4.1.1 概念说明

cuda-oxide 的自我定位写得很明确：**一个自定义 rustc 后端，用纯 Rust 把 GPU 内核编译成 CUDA PTX**。它不是「给 Rust 加一个 CUDA 绑定」（那是 `cuda-bindings` 干的事），也不是「写一套 DSL」，而是**改造编译器本身**，让普通 Rust 函数能落到 GPU 上。

围绕这个核心，它还提供了：

- **单源编译**：host 与 device 代码同处一个文件、一次构建。
- **设备端抽象**：类型安全的线程索引、共享内存、作用域原子、屏障、TMA、warp/cluster 操作等。
- **宿主端运行时**：内存管理、锁页传输、内核启动（同步的 `cuda-core` 与异步的 `cuda-async`）。
- **Rust 原生编译流水线**：用 [Pliron](https://github.com/vaivaswatha/pliron)（一个 Rust 写的、类 MLIR 的 IR 框架）把 `Rust → Rust MIR → Pliron IR → LLVM IR → PTX` 串起来。

同时要记住它的**阶段声明**：项目目前是 **alpha（实验性、早期阶段）**，应该预期有 bug、功能不完整、API 可能被破坏。也就是说：适合学习、实验、参与共建，但还不适合上生产。

#### 4.1.2 核心流程

项目自我介绍里浓缩了三件事，可以看成三条主线：

1. **单源**：一次 `cargo oxide build` 同时编译 host 与 device。
2. **后端**：拦截 rustc 的 codegen 阶段，把 `#[kernel]` 函数编译成 PTX。
3. **流水线**：Rust → MIR → Pliron IR → LLVM IR → PTX。

#### 4.1.3 源码精读

README 开篇就给出了项目定义，把上面三点压成一句话，并列出了四条产品线（单源、后端、设备抽象、宿主运行时）以及那条贯穿全书的流水线：

> cuda-oxide is a custom rustc backend for compiling GPU kernels in pure Rust.

引用位置（含单源说明与端到端流水线一行）：

- [README.md:11-18](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/README.md#L11-L18) —— 项目定义、四条产品线、以及 `Rust → Rust MIR → Pliron IR → LLVM IR → PTX` 流水线声明。

紧接着 README 用一段「Project Status」明确它是 alpha、实验性、可能 breaking change，并欢迎反馈：

- [README.md:20-23](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/README.md#L20-L23) —— alpha 状态声明：实验性编译器、有 bug、功能不完整、API 可能变。

README 还给了几条最常用的命令，能让你立刻获得「这条流水线」的体感（其中 `cargo oxide pipeline vecadd` 会把每个中间阶段都打印出来）：

- [README.md:112-124](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/README.md#L112-L124) —— `cargo oxide run` / `pipeline` / `sanitize` / `debug` 四件套；注释里写出了 `Rust MIR → dialect-mir → mem2reg → LLVM dialect → LLVM IR → PTX`。

> 名词小贴士：
> - **dialect-mir / LLVM dialect**：都是 Pliron IR 里的「方言（dialect）」，前者贴近 Rust MIR，后者贴近 LLVM。后续单元会细讲。
> - **mem2reg**：一个经典优化 pass，把「内存里的临时变量」提升为「寄存器/SSA 值」，是 MIR 风格 IR 走向 LLVM 风格 SSA IR 的常见一步。

#### 4.1.4 代码实践

这是一个**纯阅读型实践**，目标是把「项目自我定位」内化成自己的话。

1. **实践目标**：不看本讲，复述 cuda-oxide 是什么、处于什么阶段、能给使用者带来什么。
2. **操作步骤**：
   - 打开 [README.md:11-23](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/README.md#L11-L23)。
   - 用 3 句话分别回答：① 它解决的核心问题；② 它「不是」什么（区别于 CUDA 绑定 / DSL / `.cu` 文件）；③ 它当前的成熟度。
3. **需要观察的现象**：你会注意到 README 同时强调了「能力很全」（单源、泛型、闭包、各种 GPU intrinsic）和「还很早」（alpha、有 bug、会 breaking）——这两点并不矛盾，理解这种「能力广度 vs 工程成熟度」的张力是理解本项目的前提。
4. **预期结果**：你能写出类似「cuda-oxide 是一个把纯 Rust 内核编译成 PTX 的自定义 rustc 后端，支持 host/device 单源编译，目前 alpha，适合学习与共建」这样的句子。

#### 4.1.5 小练习与答案

**练习 1**：cuda-oxide 与「给 Rust 加 CUDA FFI 绑定」最本质的区别是什么？

> **参考答案**：FFI 绑定只是让你能在 Rust 里**调用**已有的 CUDA C 编译产物；cuda-oxide 改造的是**编译器本身**，让你直接用 Rust 写内核并编译成 PTX，不需要 CUDA C 这一层。

**练习 2**：README 里反复出现「single-source」。它具体指什么？

> **参考答案**：指 host（CPU）代码与 device（GPU）内核写在**同一个 `.rs` 文件**里，用**同一条命令**（`cargo oxide build`）一次性编译出 host 二进制和内嵌 PTX，不需要 `#[cfg]` 在两种目标间手动切换。

---

### 4.2 代码生成后端架构图

#### 4.2.1 概念说明

要理解 cuda-oxide 的实现，关键在于理解它**不是一个从零写的编译器**，而是一个「**包了一层的 rustc 后端**」。它的策略非常务实：

- **拦截** `codegen_crate`（rustc 代码生成的总入口），在里头先把 device 代码抠出来、走自己的流水线；
- **其余所有事情都转发给标准 LLVM 后端**（`rustc_codegen_llvm`）。

这样做的好处是：host 代码继续享受久经考验的 LLVM 后端，device 代码则走专门的 cuda-oxide 流水线。一张完整的 ASCII 架构图就画在后端源码的文件头注释里，是全书最重要的一张图。

#### 4.2.2 核心流程

把那张图拆成竖向流程（从上到下）：

```text
Rust 源码 (.rs)
   │  rustc 前端：解析 → HIR → 类型检查 → 生成 MIR → MIR 优化 pass
   ▼
优化后的 MIR（TyCtxt，单态化已完成）   ← 此时 MIR pass 已跑完
   │
   ▼  进入 rustc_codegen_cuda（本后端）的 codegen_crate
┌─────────────────────────────────────────────┐
│ 1. KERNEL 检测：扫描 CGU，找保留命名空间       │
│       cuda_oxide_kernel_<hash>_*            │
│ 2. DEVICE 函数收集（collector）：             │
│       从 kernel 入口沿调用图遍历，收全可达函数 │
└─────────────────────────────────────────────┘
   │                    │
   ▼ DEVICE PATH        ▼ HOST PATH
 cuda-oxide 流水线      委托给 rustc_codegen_llvm
 dialect-mir            （标准 LLVM 后端）
   ▼ mem2reg
 LLVM dialect           产出 host 目标文件
   ▼                     (.o / .rlib)
 LLVM IR (.ll)
   ▼ llc                标准 x86_64 机器码
 PTX (.ptx)
```

一句话：**前端共享，后端分流**。前端（rustc）对所有代码一视同仁地生成优化后的 MIR；后端根据「是否被 kernel 可达」决定每段代码走哪条路径。

#### 4.2.3 源码精读

后端文件顶部就是那张完整架构图（建议直接打开看原版对齐效果）：

- [crates/rustc-codegen-cuda/src/lib.rs:15-86](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L15-L86) —— 总架构：rustc 前端产出优化 MIR → 本后端做 kernel 检测/收集 → 分成 DEVICE PATH（cuda-oxide 流水线）与 HOST PATH（标准 LLVM 后端）。

图中明确画出「左右分流」的那一段，是理解整个项目的枢纽：

- [crates/rustc-codegen-cuda/src/lib.rs:59-81](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L59-L81) —— DEVICE PATH 走 `dialect-mir →(mem2reg)→ LLVM dialect → LLVM IR →(llc)→ PTX`；HOST PATH 产出标准 host 目标文件（`.o`/`.rlib`）。

支撑这张图的结构体定义在这里——注意它内部**持有一个被包装的 LLVM 后端**：

```rust
pub struct CudaCodegenBackend {
    config: CudaCodegenConfig,
    /// The underlying LLVM backend for host code generation
    llvm_backend: Box<dyn CodegenBackend>,
}
```

- [crates/rustc-codegen-cuda/src/lib.rs:352-356](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L352-L356) —— `CudaCodegenBackend` 持有 `llvm_backend` 字段，印证「包装而非重写」的策略。

而整个后端被 rustc 加载的入口，是一个固定的、带 `#[unsafe(no_mangle)]` 的导出函数 `__rustc_codegen_backend`，rustc 通过 `dlopen` + `dlsym` 找到它：

- [crates/rustc-codegen-cuda/src/lib.rs:939-952](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L939-L952) —— 入口：带 `#[unsafe(no_mangle)]` 的 `__rustc_codegen_backend`，读 `CUDA_OXIDE_*` 环境变量构造 config，再 `LlvmCodegenBackend::new()` 拿到被包装的 LLVM 后端，组装成 `CudaCodegenBackend` 返回。

> 小贴士：因为是通过 `dlopen` 动态加载的 `.so`，所以 `rustc-codegen-cuda` **不在 workspace 里直接编进二进制**，而是单独构建出一个动态库，再由 rustc 在编译期加载（下一讲会细讲这种特殊构建关系）。

#### 4.2.4 代码实践

1. **实践目标**：把那张 ASCII 架构图从「看着像」变成「能复述」。
2. **操作步骤**：
   - 打开 [crates/rustc-codegen-cuda/src/lib.rs:15-86](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L15-L86)。
   - 在编辑器里把它折叠成「三大块」：① rustc 前端、② DEVICE PATH、③ HOST PATH。
   - 给每一块用一句话标注它**输入是什么、输出是什么**。
3. **需要观察的现象**：你会看到图里特意写了一句 `MIR passes have ALREADY run by this point`——这说明本后端**不重新跑** MIR 优化，它消费的是 rustc 已经优化好的 MIR。
4. **预期结果**：你能在白纸上画出「源码 → 前端 → MIR → {device 流水线 / host LLVM}」这条主干，并能指出「分流」发生的位置就是 `codegen_crate`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 cuda-oxide 选择「包装 `rustc_codegen_llvm`」而不是从零写一个完整后端？

> **参考答案**：因为 host 代码不需要任何特殊处理，复用久经考验的 LLVM 后端最省力、最可靠；cuda-oxide 只需在 codegen 入口拦截、把 device 部分导流到自己的流水线，其余方法全部 delegate。这把「要自己实现的范围」压缩到了最小。

**练习 2**：rustc 是怎么找到并调用这个自定义后端的？

> **参考答案**：通过 `-Z codegen-backend=path/to/librustc_codegen_cuda.so` 指定一个动态库；rustc `dlopen` 该库、用 `dlsym` 找到导出符号 `__rustc_codegen_backend`，调用它拿到一个 `Box<dyn CodegenBackend>`，之后就把代码生成交给这个对象。

---

### 4.3 host 与 device 分流

#### 4.3.1 概念说明

「分流」是本后端唯一真正干活的地方。问题是：rustc 并不知道哪些函数是 GPU 内核。cuda-oxide 用一个约定解决了它——

`#[kernel]` 宏在展开时，会把内核函数重命名成一个**保留命名空间**下的符号：`cuda_oxide_kernel_<hash>_<名字>`。后端只要在 codegen 阶段扫描所有代码生成单元（CGU），找出名字以 `cuda_oxide_kernel_` 开头的函数，就知道了「哪些是 kernel 入口」。

找到入口之后，再从这些入口**沿调用图遍历**，把所有被内核调到的函数（本地 crate 的、`cuda_device` 的 intrinsic、`core` 里的 `Option`/迭代器等）都收进来，作为「device 代码集合」。其余没有被 kernel 可达的代码，就默认是 host 代码。

> 关键直觉：**「是不是 device 代码」不是用 `#[cfg]` 标注决定的，而是由「能不能从某个 kernel 调用图走到」决定的。** 这就是 README 反复强调的「不需要 `#[cfg(cuda_device)]`」。

#### 4.3.2 核心流程

分流发生在 `codegen_crate` 内部，伪代码如下：

```text
fn codegen_crate(tcx):
    1. mono_items = tcx.collect_and_partition_mono_items()
       # 拿到 rustc 单态化、分好 CGU 的全部条目
    2. kernel_count = count_kernels_in_cgus(tcx, cgus)
       # 扫描 CGU，数 cuda_oxide_kernel_* 符号
    3. if 有 device 代码 且 本 crate 被选中:
         collection = collector::collect_device_functions(tcx, cgus)
           # 从 kernel 入口沿调用图收集全部可达函数
         device_codegen::generate_device_code(...)
           # 走 cuda-oxide 流水线，产出 .ll/.ptx
    4. # 无论上面走没走，host 代码一律交给 LLVM：
       llvm_backend.codegen_crate(tcx, crate_info)
```

注意第 3 步和第 4 步**不是 if/else**：只要 crate 里有 kernel，device 路径会先跑一次产出 PTX，然后 host 路径**照常再跑一次**产出 host 目标文件。最终二进制里两者都有。

#### 4.3.3 源码精读

`codegen_crate` 的执行流程在源码注释里也画了一张图，是上一节那张图的「代码视角」细化版：

- [crates/rustc-codegen-cuda/src/lib.rs:478-525](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L478-L525) —— `codegen_crate` 的 4 步执行流注释：① 拿单态化条目；② 数 kernel；③ 若有 kernel 则收集 + 走 cuda-oxide 流水线；④ 委托 LLVM 处理全部 host 代码。

实现的开头正是「检测 device 代码」，可以看到对保留命名空间的计数：

- [crates/rustc-codegen-cuda/src/lib.rs:505-523](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L505-L523) —— `codegen_crate` 起步：`collect_and_partition_mono_items` → `count_kernels_in_cgus` / `count_device_fns_in_cgus` → 判定 `has_device_code`。

而「host 路径永远会跑」这一关键事实，体现在函数末尾——不管 device 分支走没走，都会无条件调用 LLVM 后端：

- [crates/rustc-codegen-cuda/src/lib.rs:734](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L734) —— `let host_result = self.llvm_backend.codegen_crate(tcx, crate_info);` —— host 代码一律委托给被包装的 LLVM 后端。

保留命名空间的实际来源，可以在 vecadd 示例的文件头注释里看到佐证：

- [crates/rustc-codegen-cuda/examples/vecadd/src/main.rs:14-19](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs#L14-L19) —— 注释说明：`#[kernel]` 展开后会得到 `cuda_oxide_kernel_<hash>_vecadd`，后端据此把它路由到 device 流水线，而把 `main` 路由到标准 LLVM。

#### 4.3.4 代码实践

1. **实践目标**：用一个最小示例确认「同一文件里的 kernel 与 host 函数分别走两条路」。
2. **操作步骤**：
   - 阅读 [crates/rustc-codegen-cuda/examples/vecadd/src/main.rs:35-47](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs#L35-L47)：`#[cuda_module] mod kernels` 里的 `#[kernel] fn vecadd` 是 device 代码。
   - 再看同一文件 [L53-L84](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs#L53-L84) 的 `fn main`：分配显存、加载模块、启动内核、拷回结果——这是 host 代码。
   - 在心里（或纸上）把这两个函数分别贴上「→ PTX」和「→ x86_64」两个标签。
3. **需要观察的现象**：注意 `vecadd` 的文档注释里写了一句「This function exists in BOTH host MIR and device PTX」——也就是说，**同一个函数的 MIR 会被两条路径分别消费**：host 路径里它的函数体不会被调用（只做类型检查），device 路径里它才被真正编译成 PTX。
4. **预期结果**：你能解释为什么单源编译不需要 `#[cfg]`——因为分流发生在 codegen 的代码层（按符号命名 + 调用图可达性），而不是源码层。
5. 如果想看真实运行效果：在有 CUDA 的机器上 `cargo oxide run vecadd`，应输出 `✓ SUCCESS: All 1024 elements correct!`；没有 GPU 则标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：假如一个普通函数既被 `main` 调用、又被某个 `#[kernel]` 调用，它会怎样？

> **参考答案**：它会同时出现在两条路径里——device 路径会把它编译进 PTX（因为从 kernel 可达），host 路径会把它编译进 host 二进制（因为被 `main` 调用）。两份代码互不干扰，分别服务各自的执行环境。

**练习 2**：为什么后端要「沿调用图收集」而不是只编译 `#[kernel]` 函数本身？

> **参考答案**：因为内核通常会调用别的函数（`thread::index_1d`、`Option::get_mut`、各种工具函数）。PTX 必须是自包含的，所以要把所有从 kernel 可达的函数一并收进来一起编译，否则链接 PTX 时会出现未定义符号。

---

### 4.4 端到端编译流水线

#### 4.4.1 概念说明

把前面几节拼起来，就能画出 cuda-oxide 的端到端流水线。这条流水线横跨「编译器前端 → device IR 变换 → PTX 生成」三大段。这里只要记住**每一阶段的输入/输出**，不需要懂内部实现：

| 阶段 | 输入 | 输出 | 谁负责 |
|------|------|------|--------|
| 前端 + MIR | `.rs` 源码 | 优化后的 MIR | rustc（共享） |
| kernel 检测 + 收集 | MIR + CGU | device 函数集合 | 本后端 `collector` |
| MIR → dialect | rustc MIR | `dialect-mir`（Pliron） | `mir-importer` |
| 优化（mem2reg 等） | `dialect-mir` | 优化后 IR | cuda-oxide pass |
| → LLVM dialect | 优化后 IR | LLVM dialect（Pliron） | `mir-lower` |
| → LLVM IR | LLVM dialect | 文本 `.ll` | `llvm-export` |
| → PTX | `.ll` | `.ptx` | 外部 `llc`（NVPTX 后端） |

> 名词小贴士：
> - **Pliron**：Rust 写的、类 MLIR 的 IR 框架。MLIR 的核心思想是「多种方言（dialect）共存、逐步 lowering」——`dialect-mir` 和 `llvm dialect` 就是两个方言，从前者 lower 到后者，最后导出成 LLVM 文本 IR。
> - **`llc`**：LLVM 的静态编译器，这里用它把 LLVM IR 翻译成 PTX（走 LLVM 的 NVPTX 后端）。README 要求 `llc-21` 及以上，因为 Hopper/Blackwell 的高级 intrinsic 老版本不支持。

#### 4.4.2 核心流程

从「一段 Rust 源码」到「GPU 上跑起来」的完整时间线：

```text
[Rust 源码 vecadd.rs]
   │ rustc 前端（解析/HIR/类型检查/MIR/MIR pass）
   ▼
[优化后的 MIR]  ←─── host 与 device 共享同一份 MIR
   │
   ├──── device 路径 ────────────────────────────────┐
   │   collector 收集 kernel + 可达函数               │
   │   mir-importer:  rustc MIR → dialect-mir        │
   │   mem2reg 等 pass                                │
   │   mir-lower:     dialect-mir → LLVM dialect     │
   │   llvm-export:   LLVM dialect → .ll             │
   │   llc (NVPTX):   .ll → .ptx                     │
   ▼                                                 ▼
[PTX（内嵌进 host 二进制的 .oxart 制品段）]
   │  运行期：host 程序加载内嵌 PTX → cuLaunchKernel
   ▼
[GPU 上执行]

   └──── host 路径 ── rustc_codegen_llvm → 标准 x86_64 二进制 ──┘
```

一个值得强调的工程细节：**rustc 给的 MIR 已经跑过优化 pass 了**。本后端不重新优化 MIR，而是受 `-C opt-level`、`-Z mir-enable-passes` 控制——而且有一个**必须关闭**的 pass：`JumpThreading`。

原因是：JumpThreading 会复制代码以消除跳转，可能把一次 `__syncthreads()` 屏障调用复制进不同分支，导致同一 block 内不同线程执行**不同的屏障实例**，在 GPU 上直接死锁。形式化地看：屏障语义要求「同一组线程在程序同一位置共同到达」，即

\[
\forall\, t_1, t_2 \in \text{block}:\ \text{到达屏障的程序计数位置必须相同}
\]

而 JumpThreading 复制屏障后，第 0–15 号线程可能走 `bb1` 里的屏障、第 16–31 号走 `bb2` 里的屏障，破坏了上式 → 死锁。所以编译必须带 `-Z mir-enable-passes=-JumpThreading`。

#### 4.4.3 源码精读

源码里专门有一段讲「MIR 是怎么拿到的」，强调本后端消费的是**已优化** MIR：

- [crates/rustc-codegen-cuda/src/lib.rs:88-120](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L88-L120) —— 解释 `codegen_crate` 被调用时，rustc 已完成解析、类型检查、MIR 生成与 MIR 优化 pass；收到的 `TyCtxt` 里是优化后的 MIR，受 `-C opt-level` / `-Z mir-enable-passes` 影响，且 **host 与 device 用同一份 MIR**。

紧接着是「必须关闭 JumpThreading」的硬性要求，配了一张死锁示意图：

- [crates/rustc-codegen-cuda/src/lib.rs:141-166](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L141-L166) —— JumpThreading 会把屏障复制进不同分支，使不同线程执行不同屏障实例 → 死锁，因此编译必须 `-Z mir-enable-passes=-JumpThreading`。

device 路径「调用 cuda-oxide 流水线」的真正调用点在 `codegen_crate` 中段：

- [crates/rustc-codegen-cuda/src/lib.rs:568-730](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L568-L730) —— 有 device 代码时：`collector::collect_device_functions` 收集可达函数，再 `device_codegen::generate_device_code` 进入 cuda-oxide 流水线（内部会调 `mir_importer::run_pipeline()`），产出 `.ll` 与 `.ptx`；并用 `catch_unwind` 把流水线 panic 转成 cuda-oxide 自己的诊断（避免被误报成 rustc 的 ICE）。

而最终把「PTX 产出」和「host 目标文件」合并、并内嵌制品的收尾逻辑在函数末尾：

- [crates/rustc-codegen-cuda/src/lib.rs:737-742](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L737-L742) —— 把 device 产出的制品对象与 host 结果一起封装成 `CudaOngoingCodegen` 返回，后续 `join_codegen` 会把内嵌制品并入编译产物（运行时由 `cuda-host` 从二进制里发现并加载——见后续宿主运行时单元）。

> 旁注（不必记，留个印象）：本后端还支持通过环境变量调参，例如 `CUDA_OXIDE_DUMP_MIR` / `CUDA_OXIDE_DUMP_LLVM` 把中间 IR 落盘、`CUDA_OXIDE_TARGET` 指定 `sm_90a` 等 GPU 目标。这些在 [crates/rustc-codegen-cuda/src/lib.rs:277-286](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L277-L286) 列出，调试流水线时很有用。

#### 4.4.4 代码实践

这是本讲的**主实践**，直接对应本讲任务。

1. **实践目标**：用自己的话写出一段 Rust 代码「从源码到 GPU PTX」经过的阶段，并标注每段属于 host 还是 device。
2. **操作步骤**：
   - 重读 [README.md:11-18](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/README.md#L11-L18) 与 [crates/rustc-codegen-cuda/src/lib.rs:15-86](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L15-L86) 的架构图。
   - 仿照本节 4.4.2 的流程，画一张你**自己**的版本（可以更简略），但要包含：① rustc 前端（共享）；② device 五段（dialect-mir / mem2reg / LLVM dialect / LLVM IR / PTX）；③ host 一段（LLVM）。
   - 在每个阶段旁边标注 `[host]` / `[device]` / `[共享]`。
3. **需要观察的现象**：
   - 你会发现「前端」是**共享**的——host 与 device 都源自同一份 MIR。
   - 你会发现「PTX 之后」还有一步运行期加载（host 程序把内嵌 PTX 装进 GPU 再启动）——这一步严格说是「运行时」而非「编译期」，容易漏掉。
4. **预期结果**：得到一张至少包含 7 个节点（源码 / 前端 / MIR / device 流水线 / PTX / host LLVM / 运行期启动）的流程图，并正确区分 host/device。
5. 进阶（可选，需 CUDA 环境）：运行 `cargo oxide pipeline vecadd`，对照它打印的每个阶段（`Rust MIR → dialect-mir → mem2reg → LLVM dialect → LLVM IR → PTX`），把它和你画的图一一对应。无 GPU 环境则标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：流水线里 `dialect-mir` 和 `LLVM dialect` 都是 Pliron IR，为什么不直接从 rustc MIR 一步生成 `.ll`？

> **参考答案**：分层 lowering 更易实现与维护。`dialect-mir` 贴近 Rust MIR 的语义（变量、基本块、借用），方便做正确性翻译；`LLVM dialect` 贴近 LLVM 的 SSA 形式，方便导出。中间用 `mem2reg` 等 pass 逐步把「内存式」MIR 变成「寄存器/SSA 式」LLVM——一步到位会非常复杂且易错。这也是 MLIR 风格框架的核心方法论。

**练习 2**：如果编译时**没有**带 `-Z mir-enable-passes=-JumpThreading`，最可能出什么问题？

> **参考答案**：`__syncthreads()` 这类屏障可能被复制到不同分支，导致同一 block 内不同线程执行不同的屏障实例，在 GPU 上**死锁**（或产生错误的同步语义）。这就是为什么它被列为「CRITICAL / MUST」关闭的 pass。

**练习 3**：host 代码和 device 代码最终都「住在」同一个可执行文件里吗？

> **参考答案**：是的。host 代码是标准的 x86_64 机器码；device 代码被编译成 PTX 后，作为一个**内嵌的制品段（`.oxart` bundle）**塞进同一个二进制。运行时 host 程序再从这个二进制里把 PTX 取出来、加载到 GPU 执行——所以分发只需要一个文件。（内嵌与加载机制在后续「宿主运行时」单元细讲。）

---

### 4.5 启动契约与许可变更

#### 4.5.1 概念说明

本轮（#318 与 #331）对「项目门面」做了两处需要初学者注意的变更，它们都直接体现在 README 头部，本讲必须点出来：

1. **启动内核现在是 `unsafe` 的**。README 顶部那段 Quick Start 里，`module.map::<f32, _>(...)` 被整个包进了 `unsafe { ... }`。原因是 `LaunchConfig` 被**有意设计成「原始数据」**——它只是一组 grid/block/shared_mem 的数字，编译器无法从类型上证明这些维度与资源是否匹配某个具体内核（比如该内核到底用 `index_1d()` 还是 `index_2d()`、需要多少共享内存、最低要求哪个算力）。因此「用一份 raw 配置去启动内核」是一个需要调用方自负其责的操作，必须写 `unsafe`，并在旁边用 `// SAFETY:` 注释说明你证明了什么。

2. **存在一条「把证明移进安全 API」的受检启动路径**。README 紧接着告诉你：如果给内核标注 `#[launch_contract(...)]`（声明它的 domain、block 形状、动态共享内存对齐、最低算力等），宏就会生成一套**受检启动 API**——`module.prepare_<name>()` 在活设备上校验配置后产出 `PreparedLaunch`，再用**安全的** `module.<name>()` 方法入队。这样正确性证明就从「每次启动写 unsafe 注释」变成了「在模块定义处声明一次、之后类型系统替你把关」。

> 一句话直觉：**raw `LaunchConfig` 启动是「自证」的 unsafe 操作；`#[launch_contract]` 启动是「类型替你证」的 safe 操作**。本讲只要记住这个区分即可，受检启动的完整机制留给后续 u2 单元深讲。

3. **许可证统一为 Apache-2.0**。本轮 #331 把原本「`cuda-bindings` 用 NVIDIA Software License、其余 crate 用 Apache-2.0」的混合许可，统一成「整个 cuda-oxide 项目采用 Apache License 2.0」；第三方组件各自保留其文件内声明的许可，统一登记在 `dependency-licenses.csv` 里。原来的 `LICENSE-NVIDIA` 文件被移除。对使用者而言，这意味着 cuda-oxide 的许可更加友好、统一，不再有 NVIDIA 专有许可那一层。

#### 4.5.2 核心流程

把两种启动模式并列对比，建立直觉：

```text
【raw 启动（unsafe）】
  LaunchConfig::for_num_elems(1024)        ← 只是一组数字，未证明
        │
        ▼  调用方在 unsafe 块里写 SAFETY 注释，自证维度/资源匹配
  module.map::<f32,_>(&stream, cfg, ...)   ← unsafe fn

【受检启动（safe，需 #[launch_contract]）】
  LaunchConfig1D::new(...) / 2D / 3D       ← 仍是配置数据
        │
        ▼  module.prepare_map(cfg)         ← 在活设备上校验 block/shared/compute-cap/cluster
  PreparedLaunch<map>                      ← 一份「品牌化」的、特化证明
        │
        ▼  module.map(prepared, ...)        ← 安全的受检启动
  入队执行
```

关键差别：raw 路径的「证明责任」在**每次调用点**（写成 unsafe + 注释）；受检路径的「证明责任」在**模块定义点**（声明一次 `#[launch_contract]`），之后由宏生成的安全 API 持有一份不可伪造的 `PreparedLaunch` 凭证。

#### 4.5.3 源码精读

README 头部示例的启动调用现在被包在 `unsafe` 块里，并带 `// SAFETY:` 注释——这是本轮最显眼的门面变化：

- [README.md:58-72](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/README.md#L58-L72) —— `module.map::<f32, _>(...)` 被包进 `unsafe { ... }`，注释说明这份 raw 配置是 1-D、匹配 `index_1d()`、每输出元素一个线程，并提示「A launch contract can move this proof into the generated safe API」。

紧接着的说明文字把「为什么 unsafe」和「如何变 safe」一次讲清：

- [README.md:83-86](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/README.md#L83-L86) —— 明确 `LaunchConfig` 是 intentionally raw data，用它启动内核是 unsafe（调用方须证明维度与资源匹配）；带 `#[launch_contract(...)]` 的内核改走受检的 `PreparedLaunch` 安全方法。

异步路径同样如此：`map_async` 的 raw 配置调用也被包进了 `unsafe`：

- [README.md:97-106](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/README.md#L97-L106) —— 异步启动示例：`let launch = unsafe { module.map_async::<f32,_>(...) };` 再 `launch.sync()?;`，同样带 SAFETY 注释。

许可变更落在 README 末尾的 License 段：

- [README.md:333-337](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/README.md#L333-L337) —— 整个项目统一为 Apache License 2.0，第三方组件保留各自文件内许可，清单见 `dependency-licenses.csv`（原先的 `LICENSE-NVIDIA` 已移除）。

> 名词小贴士：
> - **`PreparedLaunch`**：一个「品牌化（branded）」的凭证类型——它的构造只能由 `prepare_*` 在活设备上完成校验后产出，调用方无法伪造。后续 u2 单元会细讲它如何用 sealed trait 与生命周期把证明绑死。
> - **raw / 原始启动**：指直接用 `LaunchConfig`（一组未经证明的数字）启动，区别于受检启动。本讲只要建立这个对照即可。

#### 4.5.4 代码实践

这是一个**阅读 + 推理型实践**，帮助你在不依赖 GPU 的情况下确认自己理解了两种启动模式。

1. **实践目标**：解释 README 示例里那段启动**为什么必须**是 `unsafe`，并说出把它「变成 safe」需要做什么。
2. **操作步骤**：
   - 打开 [README.md:58-72](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/README.md#L58-L72)，读 `// SAFETY:` 注释，圈出调用方到底**证明了哪几件事**（提示：维度、索引空间、线程数）。
   - 再读 [README.md:83-86](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/README.md#L83-L86)，用一句话回答：「如果不想写 unsafe，要给内核加什么属性？它会生成哪两个方法/类型来替代当前的 unsafe 调用？」
3. **需要观察的现象**：你会注意到 `LaunchConfig::for_num_elems(1024)` 本身**没有任何类型信息**能反映「这个内核是 1-D 的」——这正是它「原始、需要 unsafe」的根因；而受检路径的证明是靠 `PreparedLaunch` 这个**类型**来携带的。
4. **预期结果**：你能写出类似「该调用是 unsafe，因为 `LaunchConfig` 只是一组未经证明的维度/资源数字，编译器无法保证它匹配 `map` 内核的 1-D 索引空间；要变 safe，可给 `map` 加 `#[launch_contract(domain=1, ...)]`，改用 `prepare_map()` 产出 `PreparedLaunch` 后再安全启动」这样的解释。
5. 许可确认：打开 [README.md:333-337](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/README.md#L333-L337)，确认 cuda-oxide 现在整体是 Apache-2.0，且不再依赖 `LICENSE-NVIDIA`。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `LaunchConfig` 被设计成「原始数据」，而不是带类型参数的强类型？

> **参考答案**：因为同一份 `LaunchConfig` 要能描述任意内核的启动（grid/block/共享内存都是纯数字），类型系统无法仅凭这几个数字就知道它们是否匹配某个具体内核的索引空间与资源需求。把它做成「原始数据 + 启动时 unsafe」是把证明责任显式交给调用方；而 `#[launch_contract]` + `PreparedLaunch` 则是用宏在模块定义处补上这份类型层面的证明，从而换出一个安全 API。两种设计是同一个问题的两个出口。

**练习 2**：本轮 #331 之后，`cuda-bindings` 还是不是 NVIDIA 专有许可？

> **参考答案**：不再是。本轮把整个 cuda-oxide 项目（含 `cuda-bindings`）统一为 Apache License 2.0，移除了 `LICENSE-NVIDIA`；第三方组件保留各自许可，统一登记在 `dependency-licenses.csv`。

## 5. 综合实践

把本讲所有知识点串成一个小任务：**写一份「cuda-oxide 一页纸速查表」**。

要求这张速查表包含：

1. **定位**：一句话项目定义 + alpha 状态提醒（参考 4.1）。
2. **架构**：手画的「前端共享、后端分流」草图（参考 4.2 / 4.3）。
3. **流水线**：从源码到 PTX 的阶段序列，每阶段标 `[host]`/`[device]`/`[共享]`（参考 4.4）。
4. **关键约束**：列出「必须关闭 JumpThreading」「需要 `llc-21+`」「kernel crate 用 `#![no_std]`」这几条硬约束，并各写一句原因。
5. **入口点**：标注 `__rustc_codegen_backend` 是后端加载入口、`codegen_crate` 是分流发生地。
6. **启动与许可**：写明「raw `LaunchConfig` 启动是 unsafe、`#[launch_contract]` 走受检 `PreparedLaunch`」，并标注「项目整体为 Apache-2.0」（参考 4.5）。

完成后再回到 [crates/rustc-codegen-cuda/src/lib.rs:15-86](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L15-L86) 对照原图查漏补缺。这张速查表会作为你阅读后续所有讲义时的「总地图」。

## 6. 本讲小结

- cuda-oxide 是一个**自定义 rustc 后端**，把纯 Rust 的 `#[kernel]` 函数编译成 CUDA PTX，支持 host/device **单源**编译。
- 项目目前是 **alpha**：能力广（泛型、闭包、原子、集群、张量核……），但工程成熟度还低，会有 bug 和 breaking change。
- 它的策略是**包装而非重写**：拦截 `codegen_crate` 做 device 分流，其余全部委托给标准 `rustc_codegen_llvm`。
- 「device 与否」由**保留命名空间 `cuda_oxide_kernel_*` + 调用图可达性**决定，不需要 `#[cfg(cuda_device)]`。
- 端到端流水线：`Rust → MIR（共享）→ dialect-mir →（mem2reg）→ LLVM dialect → LLVM IR →（llc）→ PTX`，host 路径走标准 LLVM。
- 编译必须 `-Z mir-enable-passes=-JumpThreading`，否则屏障可能被复制导致 GPU 死锁。
- README 头部示例的启动被包在 `unsafe` 块中：`LaunchConfig` 是未经证明的「原始数据」，启动内核需要调用方自证；`#[launch_contract]` 可把这份证明移进宏生成的安全 `PreparedLaunch` API（受检启动细节见 u2）。
- 许可证本轮（#331）统一为 Apache-2.0，移除 `LICENSE-NVIDIA`，第三方清单见 `dependency-licenses.csv`。

## 7. 下一步学习建议

本讲只画了鸟瞰图，接下来建议按手册的 U1 单元继续：

- **u1-l2（Workspace 与 crate 地图）**：搞清楚上面提到的 `cuda-device` / `cuda-core` / `cuda-async` / `mir-importer` / `cargo-oxide` 等每个 crate 各自的职责，以及 `rustc-codegen-cuda` 为什么不进 workspace。
- **u1-l3（工具链与 cargo-oxide 驱动）**：动手装好工具链、跑通 `cargo oxide doctor` 与 `cargo oxide run vecadd`，把本讲的流水线在真实机器上跑起来。
- **关于启动契约（前瞻）**：本讲 4.5 只点了「raw 启动 unsafe / `#[launch_contract]` 走 `PreparedLaunch`」这层皮。想真正弄懂 `LaunchConfig1D/2D/3D`、`prepare_*` 的活设备校验、以及 `PreparedLaunch` 如何用品牌化 sealed trait 防伪，请等到 **u2-l1（宏与启动契约属性）** 与 **u2-l4（从宿主启动内核：raw 配置与类型化启动契约）**。
- **延伸阅读**：想直接看「流水线每个阶段长什么样」，可以在装好环境后运行 `cargo oxide pipeline vecadd`，对照本讲 4.4 的阶段表逐段观察；也可阅读 `crates/rustc-codegen-cuda/src/lib.rs` 文件头注释（[L88-L232](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L88-L232)）里关于 `no_std` 与 crate 过滤的补充说明，为后续 device 编程单元做铺垫。
