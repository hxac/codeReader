# 独立后端 cuda-oxide-codegen：rustc 无关的 dialect-mir→PTX

## 1. 本讲目标

本讲深潜 PR #314 抽出的全新 crate `cuda-oxide-codegen`。它是 cuda-oxide 流水线里**唯一一段不依赖 rustc** 的后段：只要你能用 `dialect-mir` / `dialect-nvvm` 的 op 把一个内核拼出来，它就能把你产出可被 `ptxas` 接受的 PTX，全程不需要 `rustc_private`、不需要 nightly 工具链与 `rustc_driver` 版本对齐。

学完后你应当能够：

- 说清 `cuda-oxide-codegen` 为什么必须、以及如何做到「独立于 rustc」，以及它的 `experimental` v1 契约约束了什么。
- 用 `CodegenModule` / `mark_kernel_entry` / `Compiler` / `CompileOptions` / `Target` 写出一条最小编译调用链。
- 复述共享后段 `compile_translated_module` 的 `verify → mem2reg/unroll → lower → export → llc` 编排顺序，以及 standalone 路径「永不输出 NVVM IR、拒绝未解析符号」这条硬约束。
- 解释 `Toolchain` 如何在 Rust sysroot 与 `PATH` 上发现并配对 `llc`/`opt`，为何要求二者同一 LLVM 大版本。
- 说明 `mir-importer`（rustc 前端）与 standalone 前端如何复用**同一份**后段实现，从而不会静默分叉。

本讲承接 u4-l2（mir-importer 鸟瞰）与 u4-l4（MIR lowering 鸟瞰）：那两讲分别讲了「翻译」和「lowering」两头，本讲讲夹在它们之间、被两个前端共享的「后段编排器」。

## 2. 前置知识

阅读本讲前，最好已经建立以下直觉（均为前序讲义的结论）：

- **单源编译与 host/device 分流**（u1-l1、u4-l1）：rustc 把 `#[kernel]` 函数识别为设备代码，交给 cuda-oxide 流水线；普通宿主代码仍走标准 LLVM 后端。
- **流水线分段**（u4-l2、u4-l4）：设备代码经过 `rustc MIR → dialect-mir（Pliron IR）→ mem2reg/unroll → LLVM dialect → LLVM IR → llc → PTX`。`mir-importer` 负责前半段「MIR → dialect-mir」，`mir-lower` 负责把 dialect op 降级到 LLVM dialect。
- **Pliron 是类 MLIR 的多方言 IR 框架**：`Context` 拥有所有 IR 对象（arena 分配），op 之间用 `Ptr` 句柄引用，句柄本身不带 context 身份。
- **dialect-mir / dialect-nvvm 两层方言**（u4-l3）：前者贴近 Rust 语义，后者贴近 PTX/NVVM 指令。
- **FMA 收缩契约**（u4-l4、u4-l5）：浮点乘加是否融合成 FMA 是一条贯穿 codegen→制品→运行时的策略链。

本讲用到但**不再展开**的术语：`rustc_private`（让一个 crate 能链接 rustc 内部结构的 nightly feature）、`__rustc_codegen_backend`（rustc `dlopen` 后端 dylib 时查找的固定入口符号）。它们在 u1-l2 与 u4-l1 已解释过，是理解「为什么本 crate 显式不要它们」的对照背景。

## 3. 本讲源码地图

本讲聚焦 `cuda-oxide-codegen` crate 自身，并对照 `mir-importer` 看复用关系。

| 文件 | 作用 |
|------|------|
| `crates/cuda-oxide-codegen/Cargo.toml` | 依赖只有 Pliron +各方言+ lower/transforms，**没有 rustc**——这是「rustc 无关」的物证。 |
| `crates/cuda-oxide-codegen/src/lib.rs` | crate 入口。只暴露 `experimental`（前端契约）与 `#[doc(hidden)] __private`（mir-importer 内部钩子）两个命名空间。 |
| `crates/cuda-oxide-codegen/src/api.rs` | experimental 公共 API 全集：`Target`/`CompileOptions`/`CodegenModule`/`Toolchain`/`Compiler`/`Compilation`/`CompileError`。 |
| `crates/cuda-oxide-codegen/src/pipeline.rs` | 共享后段编排器 `compile_translated_module`，两个前端唯一的后段实现。 |
| `crates/cuda-oxide-codegen/src/prep.rs` | dialect-mir 准备阶段：verify → mem2reg → verify → 标注驱动循环展开 → verify。 |
| `crates/cuda-oxide-codegen/src/lower.rs` | 调 `mir-lower` 把 dialect 降到 LLVM dialect 的薄封装。 |
| `crates/cuda-oxide-codegen/src/options.rs` | `BackendOptions`：用结构体字段取代后段内部所有 `CUDA_OXIDE_*` 环境变量读取。 |
| `crates/cuda-oxide-codegen/src/llvm_tools.rs` | `llc`/`opt` 的发现与「同主版本配对」决策。 |
| `crates/cuda-oxide-codegen/src/ptx.rs` | 实际拼装 `llc`/`opt` 命令行、产出 PTX。 |
| `crates/cuda-oxide-codegen/src/target.rs` | 从导出的 LLVM 文本里探测架构特性，选最小 `sm_XX`。 |
| `crates/mir-importer/src/pipeline.rs` | rustc 前端如何 `use cuda_oxide_codegen::__private::{...}` 复用同一后段。 |
| `crates/cuda-oxide-codegen/tests/spine_kernel_ptx.rs` | 一份**真实**的「手写 dialect-mir → 标记入口 → 编出 PTX → ptxas 验证」端到端用例，本讲实践的蓝本。 |

## 4. 核心概念与源码讲解

### 4.1 为何独立于 rustc：experimental 公共 API 与 v1 实验契约

#### 4.1.1 概念说明

cuda-oxide 的主流水线深度绑定 rustc：`rustc-codegen-cuda` 是一个被 rustc `dlopen` 的 dylib 插件，靠 `#![feature(rustc_private)]` 直接读 rustc 内部数据结构（`rustc_middle::TyCtxt`、MIR 等）。这意味着主流水线只能在与之版本对齐的 nightly rustc 上运行——对一个想复用 cuda-oxide 的 PTX 后段、却不愿绑死某条 nightly 的第三方前端（例如某个基于 MLIR 的 GPU 前端、或一个差分测试生成器）而言，这道门槛过重。

#314 的解法是**把「需要 rustc 的翻译段」与「不需要 rustc 的后段」物理拆开**：

- 翻译段（`rustc MIR → dialect-mir`）留在 `mir-importer`，它仍然 `rustc_private`。
- 后段（`dialect-mir → verify → mem2reg/unroll → LLVM dialect → LLVM IR → PTX`）整体搬进新 crate `cuda-oxide-codegen`，它的公共边界就是 **dialect-mir IR**，输入接受 Pliron 的 in-memory IR，输出 PTX 字节。

于是任何能产出 dialect-mir IR 的前端，都能拿到这条与 rustc 解耦的 PTX 后段。物证就在 `Cargo.toml` 的依赖表里——没有任何 rustc 相关依赖：

```toml
[dependencies]
pliron = { workspace = true }
thiserror = { workspace = true }
dialect-mir = { workspace = true }
dialect-nvvm = { workspace = true }
mir-lower = { workspace = true }
mir-transforms = { workspace = true }
llvm-export = { workspace = true }
libnvvm-sys = { workspace = true }
nvvm-transforms = { workspace = true }
```

见 [crates/cuda-oxide-codegen/Cargo.toml:11-20](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/Cargo.toml#L11-L20)，中文说明：该 crate 的全部外部依赖只有 Pliron IR 框架、cuda-oxide 自己的各方言/lower/transforms，以及 `libnvvm-sys`（用于解析 `CudaArch`）。没有 `rustc_private`、没有 nightly 绑定——这就是「rustc 无关」的字面含义。

#### 4.1.2 核心流程

crate 只暴露两个命名空间：

```text
cuda_oxide_codegen
├── experimental   ← 唯一受支持的公共前端契约（v1 实验）
└── __private      ← #[doc(hidden)]，仅供 mir-importer 复用的内部钩子
```

`experimental` 的 v1 契约有几条硬约束（直接决定你能用它做什么、不能做什么）：

1. **源码级兼容，非 IR 稳定**：API 只保证与「发布它的这一个 cuda-oxide 修订版」源码兼容。前端必须把 cuda-oxide、Pliron、`dialect-mir`、`dialect-nvvm` 钉到同一修订；它们的内存 IR **不是**稳定的交换格式。
2. **输入边界**：模块由 `dialect-mir` / `dialect-nvvm` / builtin op 组装；内核入口是顶层的 `MirFuncOp`，必须用 `CodegenModule::mark_kernel_entry` 显式标记。
3. **产物必须自包含**：v1 PTX 不允许未解析的外部符号。一旦检测到 libdevice 调用（`__nv_*`）或任何 extern 声明，直接返回 `CompileError::UnsupportedLinking`——standalone 路径**不提供**链接步骤。
4. **同步、无缓存**：编译是阻塞的；v1 没有缓存、没有取消、没有 link step。

#### 4.1.3 源码精读

crate 顶部的模块文档把 v1 契约写得非常明确。[crates/cuda-oxide-codegen/src/lib.rs:30-49](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/lib.rs#L30-L49) 给出「版本契约 / 接受输入」两节，中文说明：API 是实验性 v1，只与发布它的精确 cuda-oxide 修订源码兼容；输入模块的内核入口必须是顶层 `MirFuncOp` 且经 `mark_kernel_entry` 标记；v1 产物必须自包含，libdevice 等未解析函数返回 `UnsupportedLinking`。

两个命名空间的导出见 [crates/cuda-oxide-codegen/src/lib.rs:90-119](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/lib.rs#L90-L119)：`experimental` 再导出 `api` 模块里的全部公开类型；`__private` 则把 `PipelineError`、`compile_translated_module`、`BackendOptions` 等内部类型以 `#[doc(hidden)]` 暴露给 mir-importer，并附注释「This is not part of the experimental standalone frontend contract」——即这两套表面有意隔离：前端用 `experimental`，rustc 前端用 `__private`，互不污染。

文档里还嵌了一段可运行的最小流程（`no_run`），它就是本讲「代码实践」要追踪的那条调用链的样板：[crates/cuda-oxide-codegen/src/lib.rs:53-71](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/lib.rs#L53-L71)。

#### 4.1.4 代码实践

**目标**：从依赖层面确认本 crate 与 rustc 解耦，并跑通文档里的最小流程（在你机器上未必装了 `llc`，因此以「阅读 + 编译该 doctest」为主）。

1. 打开 `crates/cuda-oxide-codegen/Cargo.toml`，确认 `[dependencies]` 表里没有 `rustc*`、没有 `nightly` 字样。
2. 阅读本 crate 顶部没有 `#![feature(rustc_private)]`（与 `rustc-codegen-cuda` 形成对比）。
3. 把 [lib.rs:53-71](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/lib.rs#L53-L71) 的最小流程抄进一个临时 binary，对照 `api.rs` 给每个符号（`CodegenModule::new` / `Compiler::discover` / `CompileOptions::new` / `Target::parse` / `compile` / `into_ptx`）补上行号注释。
4. 用一条 stable rustc（不需要 nightly）执行 `cargo build -p cuda-oxide-codegen`，确认它能在非 nightly 工具链下编译通过——这正是「不依赖 rustc_private」的可执行证据。

**需要观察的现象**：`cargo build` 不报 `crate rustc... is private` 之类的 nightly 专属错误；最小流程里 `Compiler::discover()?` 会在缺 `llc` 时返回结构化错误而不是编译期失败。

**预期结果**：本 crate 在 stable rustc 上构建通过（运行时仍需外部 `llc`）。若你的环境没有 `llc-21+`，则运行时 `discover()` 会返回 `CompileError::Toolchain`——这属于运行期行为，不影响「编译期 rustc 无关」的结论。如无法本地验证运行结果，明确标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 v1 契约要求前端把 Pliron 与两个 dialect crate 钉到同一修订，而不是承诺 IR 的长期稳定？

> **答案**：因为 Pliron 的 in-memory IR 是 arena 句柄，其布局、op 定义、属性集都会随修订演化，并不是设计成跨版本的序列化交换格式。承诺稳定 IR 会冻结内部实现；改为「源码级兼容 + 一次性钉版本」既保留重构自由，又让前端在一次构建内拿到一致视图。

**练习 2**：`experimental` 与 `__private` 都是 `pub mod`，为什么后者要加 `#[doc(hidden)]` 且注释「not part of the experimental standalone frontend contract」？

> **答案**：二者服务不同受众。`experimental` 是给第三方前端的稳定表面，必须保守；`__private` 是给同仓 `mir-importer` 复用后段的内部钩子，签名会随重构频繁变化。加 `doc(hidden)` 与免责注释是为了阻止前端意外依赖内部钩子，从而让后段实现可以自由演进而不破坏外部承诺。

### 4.2 CodegenModule 与 mark_kernel_entry：组装并标记内核入口

#### 4.2.1 概念说明

`CodegenModule` 是前端持有一份「待编译模块」的把手。它的核心设计点是：**把 Pliron `Context` 与根 `ModuleOp` 永久绑定在同一个结构体里**。原因是 Pliron 的指针只是 arena key，本身不带 context 身份——一个 `Ptr<Operation>` 脱离了它所属的 context 就毫无意义。`CodegenModule` 通过把两者锁在一起，保证前端通过 `edit`/`inspect` 拿到的句柄始终来自正确的 context。

`mark_kernel_entry` 解决的是「在这堆顶层函数里，哪个是 PTX 入口」的问题。PTX 入口不是靠名字约定、也不是靠调用约定手动设置，而是靠给 `MirFuncOp` 挂一个名为 `gpu_kernel` 的属性——后续 `mir-lower` 与 `llvm-export` 只检查这个属性的**存在性**（值不查），把它一路传播成 `llc` 渲染的 `.visible .entry`。

#### 4.2.2 核心流程

```text
CodegenModule::new(name)
  ├── 创建 Pliron Context
  ├── 注册 dialect-mir + dialect-nvvm       ← new() 内置，前端无需手动 register
  └── 创建根 ModuleOp，记录其句柄

前端通过 edit(|ctx, module| { ... }) 组装 IR：
  └── 在 module 的顶层 block 里塞 MirFuncOp（函数体由 BasicBlock/op 组成）

CodegenModule::mark_kernel_entry("add_kernel")
  ├── 校验 module 仍合法（恰好 1 region / 1 block）
  ├── 在顶层 block 里按 symbol 名查找 MirFuncOp
  └── 给命中的 func 挂 gpu_kernel = "true" 属性
```

#### 4.2.3 源码精读

`CodegenModule` 的字段就是把 context 与根模块绑死，文档明确指出 Pliron 句柄不带 context 身份：[crates/cuda-oxide-codegen/src/api.rs:227-236](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/api.rs#L227-L236)。

`new` 在构造时就为前端注册好两个 dialect，省去前端手动 `register`：[crates/cuda-oxide-codegen/src/api.rs:240-251](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/api.rs#L240-L251)，中文说明：创建 Context 后立即 `dialect_mir::register` 与 `dialect_nvvm::register`，再 `ModuleOp::new` 建根模块。

`mark_kernel_entry` 是本模块的机制核心。它先做结构校验（恰好一个 region、一个 block），再遍历顶层 op 找出 symbol 等于入参的 `MirFuncOp`，最后给它挂属性：[crates/cuda-oxide-codegen/src/api.rs:275-353](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/api.rs#L275-L353)。其中真正「打标记」的两行是：

```rust
let key: Identifier = "gpu_kernel"
    .try_into()
    .expect("gpu_kernel is a valid Pliron identifier");
function
    .deref_mut(&self.context)
    .attributes
    .set(key, StringAttr::new("true".to_string()));
```

见 [crates/cuda-oxide-codegen/src/api.rs:345-351](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/api.rs#L345-L351)，中文说明：给目标函数挂 `gpu_kernel="true"` 字符串属性；下游只看属性是否存在、不看值。这条 `gpu_kernel` 标记如何变成 PTX 的 `.visible .entry`，`tests/spine_kernel_ptx.rs` 顶部注释画出了完整链路（mir-lower 的 `is_kernel_func` → lowering 传播属性 → llvm-export 用 `ptx_kernel` 调用约定 → llc 渲染 `.visible .entry`），见 [crates/cuda-oxide-codegen/tests/spine_kernel_ptx.rs:15-32](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/tests/spine_kernel_ptx.rs#L15-L32)。

`mark_kernel_entry` 对异常形状（多 region、多 block、symbol 缺失、重复）一律返回结构化 `CompileError::InvalidModule`，绝不 panic：见函数中段 [crates/cuda-oxide-codegen/src/api.rs:288-343](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/api.rs#L288-L343)。

#### 4.2.4 代码实践

**目标**：动手拼一个最小内核并标记入口，体会 `gpu_kernel` 属性的「存在即入口」语义。

1. 以 `tests/spine_kernel_ptx.rs` 的 `build_add_kernel` 为蓝本，阅读它如何用 `MirFuncOp` + 一串 `MirAddOp`/`MirLoadOp`/`MirStoreOp`/`ReadPtxSreg*Op` 拼出 `out[i] = a[i] + b[i]`：[crates/cuda-oxide-codegen/tests/spine_kernel_ptx.rs:59-236](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/tests/spine_kernel_ptx.rs#L59-L236)。
2. 注意函数末尾只有一行 `module.mark_kernel_entry("add_kernel").unwrap();`（[spine_kernel_ptx.rs:235](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/tests/spine_kernel_ptx.rs#L235)）——没有任何调用约定或命名约定的手工设置。
3. 仿照它写一个 `copy_kernel(out, a)`，只把 `a[i]` 原样写入 `out[i]`（去掉加法），在 `edit` 闭包里组装后调用 `mark_kernel_entry("copy_kernel")`。
4. 用 `module.inspect(|_ctx, module| module.get_operation()...)` 打印模块文本，确认 `gpu_kernel` 出现在 IR 里。

**需要观察的现象**：打印出的 IR 里 `copy_kernel` 的 func op 携带 `gpu_kernel` 属性；若你忘记调用 `mark_kernel_entry`，则下游 `llc` 产出的 PTX 里**不会有** `.visible .entry`。

**预期结果**：标记后 IR 含 `gpu_kernel`；漏标则 PTX 无入口。后者是常见踩坑点。如无法本地跑 `llc`，标注「待本地验证」，仅做静态阅读亦可确认属性出现与否。

#### 4.2.5 小练习与答案

**练习 1**：`mark_kernel_entry` 为什么要求 module 恰好 1 个 region、1 个顶层 block？

> **答案**：因为查找入口用的是「遍历唯一顶层 block 里的 op」这个线性过程；多于一个 region 或 block 意味着 IR 形状不符合 builtin `ModuleOp` 的规范（一个单块 region），属异常状态。与其在错误形状下静默找不到入口，不如在校验阶段就报结构化 `InvalidModule`。

**练习 2**：假如你给两个不同函数都挂上 `gpu_kernel`，会发生什么？`mark_kernel_entry` 本身能拦住吗？

> **答案**：`mark_kernel_entry` 对**同一个 symbol** 的重复会报 `InvalidModule`（duplicate），但它不会主动清理别的入口标记。如果对两个不同 symbol 分别调用两次 `mark_kernel_entry`，两个函数都会带属性，下游会把两者都渲成 `.entry`——这本身合法（一个模块可有多个 kernel），是否合意由前端自证。

### 4.3 共享后段编排：verify → mem2reg/unroll → lower → export → llc

#### 4.3.1 概念说明

`compile_translated_module` 是整个 cuda-oxide 后段的**唯一**编排者。它是本讲最重要的结论之一：rustc 前端（`mir-importer`）与 standalone 前端（`experimental::Compiler`）**调用的是同一个函数**，差别只在于它们各自构造的 `ModulePipelineRequest`。这种「一份后段、两个前端」的设计意味着流水线行为不可能在两条路径间静默分叉——任何后段改动对两边同时生效。

standalone 路径相对 rustc 路径有两条刻意的硬约束：

1. **永不输出 NVVM IR**：standalone 走 `OutputPolicy::SelfContainedPtx`，无论是否检测到 libdevice，都只走 `llc` 出 PTX，不会切到 NVVM IR 路径（那条需要后续 libNVVM/nvJitLink 链接，而 standalone 不提供链接）。
2. **拒绝未解析符号**：导出前显式扫描 unresolved external symbols，非空就直接 `UnsupportedLinking` 报错，宁可早失败也不产出无法加载的 PTX。

#### 4.3.2 核心流程

`compile_translated_module` 的阶段顺序（standalone 与 rustc 路径共同）：

```text
1. （可选 trace）dump dialect-mir
2. verify dialect-mir 模块
3. prepare_mir_module：
     verify → mem2reg → verify → 标注驱动循环展开 → verify
     （full-debug 模式跳过 mem2reg/unroll，保留栈槽供调试）
4. （仅 rustc 路径）插入 device extern 声明；standalone 此处为空
5. lower_to_llvm：dialect-mir/nvvm → LLVM dialect（受 allow_fma_contraction 控制）
6. 探测 libdevice 调用 → 决定是否走 NVVM IR
7. （仅当出 NVVM IR）nvmm-transforms legalize + 选 NVVM 目标
8. verify LLVM dialect 模块
9. （仅 standalone）扫描未解析外部符号，非空 → UnsupportedLinking
10. 导出 .ll 文本
11. （standalone）llc/opt → PTX； （rustc NVVM 路径）跳过 llc，交宿主侧 libNVVM/nvJitLink
```

关键分支点在第 6 步：是否出 NVVM IR 由 `should_emit_nvvm_ir(policy, needs_libdevice)` 决定，而 standalone 的 `SelfContainedPtx` 永远返回 `false`。

#### 4.3.3 源码精读

编排器本体是 `compile_translated_module`：[crates/cuda-oxide-codegen/src/pipeline.rs:152-375](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L152-L375)。其中几个对 standalone 至关重要的片段：

准备阶段调用，`promote_and_unroll` 在 full-debug 时为 false：[crates/cuda-oxide-codegen/src/pipeline.rs:169-184](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L169-L184)。

`should_emit_nvvm_ir` 的全部逻辑——standalone 永远不出 NVVM IR：[crates/cuda-oxide-codegen/src/pipeline.rs:377-382](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L377-L382)：

```rust
fn should_emit_nvvm_ir(policy: OutputPolicy, needs_libdevice: bool) -> bool {
    match policy {
        OutputPolicy::SelfContainedPtx => false,
        OutputPolicy::ExternalLinkAllowed { request_nvvm_ir } => request_nvvm_ir || needs_libdevice,
    }
}
```

standalone 拒绝未解析符号的守卫，发生在导出之后、`llc` 之前：[crates/cuda-oxide-codegen/src/pipeline.rs:291-296](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L291-L296)，中文说明：当 policy 是 `SelfContainedPtx` 时，扫描未解析外部符号，非空就返回 `UnsupportedLinking`。这条规则被单测 `standalone_never_silently_switches_to_a_linkable_artifact` 锁死：[crates/cuda-oxide-codegen/src/pipeline.rs:413-417](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L413-L417)。

准备阶段 `prepare_mir_module` 的三段 verify 包夹 mem2reg 与循环展开：[crates/cuda-oxide-codegen/src/prep.rs:25-53](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/prep.rs#L25-L53)。

lowering 薄封装把 `mir-lower` 接进来，FMA 收缩由 `allow_fma_contraction` 传入：[crates/cuda-oxide-codegen/src/lower.rs:33-51](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/lower.rs#L33-L51)。

`experimental::Compiler::compile` 在调用编排器之前，会先把模块**克隆**一份，所有破坏性 pass 都作用在克隆上，调用方 IR 原封不动：[crates/cuda-oxide-codegen/src/api.rs:499-518](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/api.rs#L499-L518)。这是「编译不污染输入、可重复编译」的保证，单测 `compilation_preserves_input_and_is_repeatable` 验证了同模块连编两次得到相同 PTX 且输入 IR 不变：[crates/cuda-oxide-codegen/tests/compile_to_ptx.rs:220-248](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/tests/compile_to_ptx.rs#L220-L248)。

#### 4.3.4 代码实践

**目标**：用一个故意调用 libdevice 的内核，验证 standalone 路径「拒绝未解析符号」的早失败行为。

1. 阅读 `tests/compile_to_ptx.rs` 的 `add_exp_call` 与单测 `lowered_libdevice_call_is_rejected_before_ptx_generation`：[crates/cuda-oxide-codegen/tests/compile_to_ptx.rs:373-444](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/tests/compile_to_ptx.rs#L373-L444)。它构造了一个调用 `CALLEE_EXP_F32`（会 lower 成 `__nv_expf`）的函数。
2. 在你的最小前端里复刻这个内核，调用 `compiler.compile(&mut module, &options)`。
3. 观察返回值。

**需要观察的现象**：编译返回 `Err(CompileError::UnsupportedLinking { symbols: ["__nv_expf"] })`，且 `error.stage()` 等于 `CompilationStage::Linking`；调用方模块 IR 保持原样（因为编译用的是克隆）。

**预期结果**：standalone v1 拒绝任何 libdevice/extern 调用，错误发生在 `Linking` 阶段而非 `Codegen` 阶段——即「在交给 `llc` 之前就拦下」。需要 libdevice 的内核必须走 rustc 前端 + NVVM IR + libNVVM/nvJitLink（见 u4-l5）。如无法本地验证，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 standalone 路径即便检测到 libdevice 也坚持不出 NVVM IR？

> **答案**：出 NVVM IR 意味着产物里带着未内联的 `__nv_*` 符号，必须再经 libNVVM 把 libdevice.10.bc 链接进来才能成机器码。standalone v1 不提供这个链接步骤，也没有相应的宿主侧运行时支持。与其产出一个无法加载的「半成品」NVVM IR，不如直接报 `UnsupportedLinking` 把责任交还给前端。

**练习 2**：`compile_translated_module` 里 `as_lowered_verification` 这个小帮手（[pipeline.rs:384-391](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L384-L391)）在做什么？为什么需要它？

> **答案**：它把「lowering 之后那次 verify」抛出的 `PipelineError::Verification` 改写成 `LoweredVerification`。原因是同一个 verify 函数在流水线里被前后用两次：一次在 dialect-mir 上（翻译期），一次在 lowering 后的 LLVM dialect 上。若不重映射，lowering 后的校验失败会被错报成 `MirPreparation` 阶段，误导排错。`stage()` 据此把错误归到 `Lowering` 阶段。

### 4.4 Toolchain：LLVM 21/22 工具发现与配对

#### 4.4.1 概念说明

后段最终要把 LLVM IR 喂给 `llc`（出 PTX）和可选的 `opt`（跑 `-O2` 中端）。这两个工具**必须来自同一个 LLVM 大版本**，因为 textual IR 不是跨大版本稳定的——`llvm_tools.rs` 的模块注释记录了真实踩坑（issue #150）：LLVM 22 的 inliner 会生成新形态的 `llvm.lifetime.start`（去掉了 `i64` size 参数），而 LLVM 21 的 `llc` 会以 `Intrinsic has incorrect argument type!` 拒绝它。在配对机制出现前，`opt` 与 `llc` 各自独立发现，于是 pin 到 LLVM 21 `llc` 的用户仍可能拿到 sysroot 里 LLVM 22 的 `opt`。

`experimental::Toolchain` 把「发现 + 配对」做成显式、可复用、可注入的对象：要么 `discover()` 自动发现，要么 `from_paths()` 由调用方显式指定，且两种方式都会校验主版本 ≥ 21、`opt`/`llc` 主版本一致。

#### 4.4.2 核心流程

`llc` 发现优先级（`resolve_llc`）：

```text
1. llc_override（历史上 CUDA_OXIDE_LLC；exclusive，即使探测失败也用它）
2. Rust 工具链 llvm-tools 里的 llc：<sysroot>/lib/rustlib/<host>/bin/llc
3. PATH 上的 llc-22 → llc-21（首个可跑的胜出）
```

`opt` 配对决策（`choose_opt`，要求与 `llc` 同主版本）：

```text
1. opt_override（CUDA_OXIDE_OPT）永远生效，主版本不匹配则记录警告
2. 与 llc 同目录的 sibling opt（版本号未知也信任——同安装目录即同版本）
3. 其余候选（sysroot opt / opt-22 / opt-21 / opt），仅接受精确同主版本
4. 都不匹配 → 跳过中端（OptChoice::Skip），并报告所有被拒候选
```

standalone API 对「跳过中端」是**严格**的：若你请求 `Optimization::O2` 却没有匹配的 `opt`，直接报 `OptimizationUnavailable`，不像 rustc 路径那样回退到未优化。

#### 4.4.3 源码精读

踩坑背景与配对动机写在模块顶部：[crates/cuda-oxide-codegen/src/llvm_tools.rs:6-36](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/llvm_tools.rs#L6-L36)，中文说明：必须先选 `llc`、读其主版本，再挑同主版本的 `opt`，并解释了 issue #150 的具体失败模式。

`LlvmToolchain::resolve` 编排上述两步：[crates/cuda-oxide-codegen/src/llvm_tools.rs:78-117](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/llvm_tools.rs#L78-L117)。`resolve_llc` 给出 `llc` 的优先级链：[crates/cuda-oxide-codegen/src/llvm_tools.rs:125-143](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/llvm_tools.rs#L125-L143)。纯决策函数 `choose_opt` 是配对逻辑的核心，可单测：[crates/cuda-oxide-codegen/src/llvm_tools.rs:175-247](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/llvm_tools.rs#L175-L247)。

`experimental::Toolchain::discover` 在 `experimental` 表面上封装 `LlvmToolchain::resolve`，并校验主版本 ≥ 21：[crates/cuda-oxide-codegen/src/api.rs:369-382](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/api.rs#L369-L382)；其版本下限检查函数 `validate_llvm_major` 要求 `>= 21`：[crates/cuda-oxide-codegen/src/api.rs:446-456](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/api.rs#L446-L456)。显式注入路径的 `from_paths` 会跑 `--version` 探测并强制 `opt`/`llc` 同主版本：[crates/cuda-oxide-codegen/src/api.rs:385-428](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/api.rs#L385-L428)。

最终 `llc` 命令行在 `ptx.rs` 里拼装，关键参数 `-march=nvptx64 -mcpu=<target>` 与可选的 `-fp-contract=fast`：[crates/cuda-oxide-codegen/src/ptx.rs:290-311](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/ptx.rs#L290-L311)。文档里还说明：LLVM 21 是通用下限，需要 PTX 9.0 的目标要 LLVM 22——见 [crates/cuda-oxide-codegen/src/lib.rs:73-79](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/lib.rs#L73-L79)。

#### 4.4.4 代码实践

**目标**：用 `from_paths` 注入一对「假」工具，体会配对与严格失败语义（无需真实 LLVM）。

1. 阅读 `tests/compile_to_ptx.rs` 的 `FakeTools`：[crates/cuda-oxide-codegen/tests/compile_to_ptx.rs:37-137](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/tests/compile_to_ptx.rs#L37-L137)。它写两个 shell 脚本冒充 `llc`/`opt`，`--version` 时打印 `LLVM version 21.0.0`，正常调用时产出一个最小 PTX。
2. 仿照它构造一个**只有 `llc`、没有 `opt`** 的 `Toolchain`，然后用默认 `CompileOptions::new(...)`（即 `Optimization::O2`）调用 `compile`。
3. 再把 options 改成 `.with_optimization(Optimization::None)` 重试。

**需要观察的现象**：第一次返回 `CompileError::OptimizationUnavailable`，`stage()` 为 `Optimization`；第二次成功（因为 `None` 不需要 `opt`）。

**预期结果**：standalone 路径对缺 `opt` 是严格失败，不像 rustc 路径会静默回退——这条行为由单测 `requested_optimization_requires_opt` 锁定：[crates/cuda-oxide-codegen/tests/compile_to_ptx.rs:446-464](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/tests/compile_to_ptx.rs#L446-L464)。如无法本地验证，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`choose_opt` 为什么在 `llc` 主版本号读不出来时（`llc_major == None`）仍然信任同目录的 sibling `opt`，却拒绝所有其它候选？

> **答案**：sibling `opt` 与 `llc` 同处一个安装目录，LLVM 安装惯例把工具并排放置，同目录即可合理推断为同一发行版、同主版本，故可信任。而其它候选（sysroot / PATH 上的 `opt-XX`）没有这种目录同源信号，在 `llc` 主版本未知时无法验证匹配，只能全部拒绝以免混版本。

**练习 2**：standalone 的 `discover()` 注释说「does not read cuda-oxide environment knobs」。这与 rustc 路径读 `CUDA_OXIDE_LLC` 矛盾吗？

> **答案**：不矛盾。`discover()` 在 `experimental` 表面上调用 `LlvmToolchain::resolve(&BackendOptions::default())`，而默认 `BackendOptions` 的 `llc_override`/`opt_override` 都是 `None`，所以它只走 sysroot + PATH。读 `CUDA_OXIDE_*` 的工作由 `BackendOptions::from_env()` 完成，它在 `mir-importer`（rustc 前端）边界被调用一次，standalone 表面从不调用它——这是「env 读取被推到 rustc 边界、后段本身不读 env」原则的体现（详见 4.5）。

### 4.5 与 mir-importer 的复用关系：一份后段，两个前端

#### 4.5.1 概念说明

#314 之前，`mir-importer` 既翻译 MIR、又自己跑后段（mem2reg/unroll/lower/export/llc）。#314 把后段整体抽出后，`mir-importer` 退化成「纯翻译器」：它只负责把 rustc 的 stable MIR 翻译成 dialect-mir IR；翻译完之后，它**调用 `cuda-oxide-codegen` 提供的 `compile_translated_module`** 完成后段。

这条复用关系靠 `cuda_oxide_codegen::__private` 暴露的内部钩子维系：`mir-importer` 的 `pipeline.rs` 直接 `use cuda_oxide_codegen::__private::{BackendOptions, ModulePipelineRequest, OutputFiles, PipelineTrace, append_to_module, compile_translated_module, verify_operation, ...}`。两个前端通过两个不同的 `ModulePipelineRequest` 构造器区分行为：

- `for_rust_pipeline`：允许外部链接、可出 NVVM IR、自动发现工具链（discover）。
- `for_standalone_ptx`：自包含 PTX、永不出 NVVM IR、用显式工具链。

如此一来，整条工具链的「后段」实现只有一份。

#### 4.5.2 核心流程

```text
mir-importer::run_pipeline(functions, device_externs, config)
  ├── Context::new + register_dialects
  ├── 逐函数 translate_body（MIR → dialect-mir）+ verify + append_to_module
  ├── BackendOptions::from_env()   ← 唯一一次读 CUDA_OXIDE_*，发生在 rustc 边界
  ├── 用 config 字段覆盖 backend_options（target/no_fma/...）
  ├── ModulePipelineRequest::for_rust_pipeline(...)   ← ExternalLinkAllowed + discover
  └── cuda_oxide_codegen::compile_translated_module(...)  ← 同一个编排器
           ↑
           └── standalone 前端用 for_standalone_ptx(...) 也调到这里
```

注意 `mir-importer` 还做了 standalone 不做的事：写 `.target`/`.options` 边车（host 制品层关切，留在 mir-importer）、维护 LTOIR/cubin 缓存键等。这些是宿主侧运行时（`oxide-artifacts`、`cuda-host`）的契约，不属于 rustc 无关的后段，所以不进 `cuda-oxide-codegen`。

#### 4.5.3 源码精读

`mir-importer` 顶部直接从 `cuda_oxide_codegen::__private` 引入后段钩子：[crates/mir-importer/src/pipeline.rs:40-46](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs#L40-L46)，中文说明：`mir-importer` 复用 `cuda_oxide_codegen` 的 `BackendOptions`、`ModulePipelineRequest`、`compile_translated_module`、`verify_operation`、`append_to_module` 等，自身不再实现后段。

在 `run_pipeline` 边界一次性读取环境变量构造 `BackendOptions`，再用显式 config 覆盖：[crates/mir-importer/src/pipeline.rs:294-303](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs#L294-L303)。注意 `backend_options.no_fma = !config.allow_fma_contraction`——把「允许 FMA 收缩」这条策略从 rustc 前端的布尔语义翻译成后段的 `no_fma` 语义。

构造 rustc 路径专属的请求并调用共享编排器：[crates/mir-importer/src/pipeline.rs:305-322](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs#L305-L322)，中文说明：用 `for_rust_pipeline` 带 `device_externs`、`emit_nvvm_ir`、discover 工具链，然后调用同一个 `compile_translated_module`。

对照 standalone 侧 `for_standalone_ptx` 的构造：[crates/cuda-oxide-codegen/src/pipeline.rs:110-125](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L110-L125)——`device_externs` 为空、`SelfContainedPtx`、`Explicit(toolchain)`。两个构造器共享同一个 `compile_translated_module`（[pipeline.rs:152](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L152)），差异完全被 `ModulePipelineRequest` 的字段吸收。

`BackendOptions::from_env` 是后段 crate 内**唯一**读环境的地方，且只由 rustc 宿主调用：[crates/cuda-oxide-codegen/src/options.rs:31-46](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/options.rs#L31-L46)，其字段定义见 [options.rs:12-29](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/options.rs#L12-L29)——注释明确说它「replaces every `CUDA_OXIDE_*` env read inside the backend」。

#### 4.5.4 代码实践

**目标**：在源码层确认「两个前端、一个后段」，并找出策略如何在边界翻译。

1. 在 `crates/mir-importer/src/pipeline.rs` 里 `grep` 出所有对 `cuda_oxide_codegen::__private::*` 的引用，列出 `mir-importer` 不再自己实现的后段能力。
2. 对照 `crates/cuda-oxide-codegen/src/api.rs` 的 `compile_clone`（[api.rs:520-574](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/api.rs#L520-L574)），看 standalone 如何把 typed `CompileOptions` 翻译成 `BackendOptions`（`no_fma = !options.fma_contraction`、`no_opt = optimization == None`、`target_arch = Some(options.target.sm())`）。
3. 对比 `mir-importer` 里的同款翻译（[pipeline.rs:295-303](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs#L295-L303)），确认两者用同一份 `BackendOptions` 结构、同一种 `no_fma` 语义。
4. 画出「typed 选项 → BackendOptions → ModulePipelineRequest → compile_translated_module」的数据流图。

**需要观察的现象**：两条路径都汇聚到 `compile_translated_module`，且 FMA 策略在两处都以「`no_fma = !allow_fma_contraction`」的相同形式翻译，证明策略语义一致。

**预期结果**：你应当能画出一张两前端共用一个后段的依赖图，并能指出 standalone 与 rustc 路径在「NVVM IR、extern 链接、工具链发现」三处的差异全在 `ModulePipelineRequest` 字段里，而不在后段实现里。这是纯阅读型实践，无需运行。

#### 4.5.5 小练习与答案

**练习 1**：为什么把 `.target`/`.options` 边车写在 `mir-importer` 而不是 `cuda-oxide-codegen`？

> **答案**：这些边车是「host 制品层」（`oxide-artifacts` / `cuda-host`）的契约——它们告诉运行时的 libNVVM/nvJitLink 该用什么 FMA 策略与目标架构链接 LTOIR。`cuda-oxide-codegen` 的定位是 rustc 无关的纯后段，不持有「制品如何被宿主消费」的知识，所以把 host 相关关切留给仍处在 rustc/host 生态内的 `mir-importer`，保持后段的纯净与可独立复用。

**练习 2**：假如未来要加第三个前端（比如某 MLIR 前端），它应当走 `experimental` 还是 `__private`？如果它需要链接外部 LTOIR，v1 能支持吗？

> **答案**：第三方前端应走 `experimental`（受支持的公共契约），把 cuda-oxide/Pliron/各方言钉到同一修订。但 v1 的 `SelfContainedPtx` 策略**不**支持链接——它会拒绝未解析符号。需要外部链接的前端要么等 v2 提供链接步骤，要么自行接管后段产出 NVVM IR 后用 libNVVM/nvJitLink 链接（即复刻 rustc 路径的宿主侧逻辑），不能直接用 `experimental` 的 `compile`。

## 5. 综合实践

把本讲五个最小模块串起来，完成一次「无 rustc 的端到端 PTX 编译」源码追踪任务：

1. **组装**：阅读 `tests/spine_kernel_ptx.rs` 的 `build_add_kernel`，理解它如何用 `MirFuncOp`、`MirPtrType`、`ReadPtxSreg*Op`、`MirAddOp`/`MirLoadOp`/`MirStoreOp`/`MirPtrOffsetOp`/`MirReturnOp` 拼出 `out[i] = a[i] + b[i]`（[spine_kernel_ptx.rs:59-236](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/tests/spine_kernel_ptx.rs#L59-L236)）。指出 `CodegenModule::edit` 闭包里的 `ctx` 与 `module` 来自哪里（提示：`CodegenModule` 把 context 与根模块绑死）。

2. **标记入口**：确认 `module.mark_kernel_entry("add_kernel")` 只是给 func 挂 `gpu_kernel` 属性（[api.rs:345-351](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/api.rs#L345-L351)），并描述这条属性如何一路变成 `.visible .entry`（参考 [spine_kernel_ptx.rs:15-32](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/tests/spine_kernel_ptx.rs#L15-L32) 的注释链）。

3. **配置并编译**：写出调用序列——`Compiler::discover()?` → `CompileOptions::new(Target::parse("sm_120")?)` → `compiler.compile(&mut module, &options)?.into_ptx()`（对应 [spine_kernel_ptx.rs:239-247](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/tests/spine_kernel_ptx.rs#L239-L247)），并说明这一行最终进入的是 `compile_translated_module`（[pipeline.rs:152](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L152)）。

4. **工具链**：解释 `Compiler::discover` 为何不读 `CUDA_OXIDE_LLC`（[api.rs:369-382](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/api.rs#L369-L382)），并说出 `llc` 的发现优先级（[llvm_tools.rs:125-143](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/llvm_tools.rs#L125-L143)）。

5. **对照 rustc 前端**：在 `mir-importer/src/pipeline.rs` 找到它对同一 `compile_translated_module` 的调用（[pipeline.rs:322](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs#L322)），指出两者在 `ModulePipelineRequest` 上的两点关键差异（NVVM IR 策略、工具链发现策略）。

6. **解释 rustc 无关**：用一句话回答「为什么这个 crate 不需要 `rustc_private`」——以 `Cargo.toml` 依赖表（[Cargo.toml:11-20](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/Cargo.toml#L11-L20)与 `lib.rs` 没有 `#![feature(rustc_private)]`）为证。

**预期产出**：一张标注了「组装 → 标记 → 编排（verify/prep/lower/verify/export/llc）→ PTX」的数据流图，以及一句话：本 crate 之所以能 rustc 无关，是因为它只依赖 Pliron IR 与 cuda-oxide 自家各方言，而把所有读 rustc 内部数据的翻译工作留给了 `mir-importer`。若本地装了 `llc-21+` 与 `ptxas`，可直接 `cargo test -p cuda-oxide-codegen --test spine_kernel_ptx -- --nocapture` 跑出真实 PTX 并被 `ptxas` 验证；否则标注「待本地验证」。

## 6. 本讲小结

- `cuda-oxide-codegen` 是 #314 抽出的 **rustc 无关** PTX 后段，公共边界是 dialect-mir IR；`Cargo.toml` 无任何 rustc 依赖、无 `#![feature(rustc_private)]` 即为物证。
- 唯一受支持的公共表面是 `experimental`（v1 实验契约：源码级兼容、产物自包含、无链接步骤），`__private` 仅供 `mir-importer` 复用。
- `CodegenModule` 把 Pliron `Context` 与根 `ModuleOp` 绑死；`mark_kernel_entry` 通过给 `MirFuncOp` 挂 `gpu_kernel` 属性来标记 PTX 入口，下游只看属性存在性。
- `compile_translated_module` 是两个前端共享的**唯一**后段编排器，顺序为 `verify → mem2reg/unroll → lower → (NVVM legalize) → verify LLVM → 链接守卫 → export → llc`；standalone 永不出 NVVM IR、拒绝未解析符号。
- `Toolchain` 显式发现并配对 `llc`/`opt`，要求同 LLVM 大版本（issue #150），下限 LLVM 21（PTX 9.0 需 22）；`Optimization::O2` 在缺 `opt` 时严格失败。
- `mir-importer` 现在是纯翻译器，通过 `__private` 钩子与 `for_rust_pipeline` 复用同一后段；env 读取集中在 `BackendOptions::from_env`，由 rustc 边界调用一次，后段本身不读 env。

## 7. 下一步学习建议

- 想看后段里每个 dialect op 是怎么降到 LLVM dialect 的，进入 **u6-l3（mir-lower 深潜）**，重点对照 `lower.rs` 里调用的 `mir_lower::lower_mir_to_llvm_with_options`。
- 想理解 NVVM IR / libdevice / nvJitLink 那条 standalone 故意不走的路径，进入 **u4-l5（从 NVVM IR 到 cubin）**，对照本讲 `should_emit_nvvm_ir` 的 `ExternalLinkAllowed` 分支。
- 想了解 `mir-importer` 的翻译段（MIR → dialect-mir）如何在调用本后段之前把 IR 准备好，进入 **u4-l2（mir-importer 鸟瞰）** 与 **u6-l2（mir-importer 深潜）**。
- 想为本 crate 新增一条 intrinsic，参照 **u6-l4（端到端新增一个 intrinsic）** 的五阶段模板——注意新 intrinsic 在 dialect/翻译/lowering 三层落地后，本讲的共享编排器会自动让 standalone 与 rustc 两条路径同时支持它。
