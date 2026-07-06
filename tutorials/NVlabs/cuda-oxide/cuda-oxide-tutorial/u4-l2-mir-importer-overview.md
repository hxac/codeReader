# MIR 导入器鸟瞰：rustc MIR → Pliron IR（后段委托 codegen）

## 1. 本讲目标

上一讲（u4-l1）我们停在后端入口 `codegen_crate` 的 host/device 分流，并追到 `device_codegen::generate_device_code` 把控制权交给 `mir_importer::run_pipeline` 这一步。本讲就跨过这道门，鸟瞰 `mir-importer` crate 内部发生的事——并且要讲清一件本轮（#314）发生的关键变化：**`mir-importer` 不再独自跑完整条编译流水线了**。

读完本讲，你应该能够：

1. 说出 `mir-importer` 在 cuda-oxide 流水线里的新定位——它只负责把 rustc 的 **stable MIR** 翻译成基于 **Pliron** 的 `dialect-mir` IR，并对每个函数做一次校验；**后段**（mem2reg、循环展开、lowering、导出、跑 `llc` 出 PTX）已整体委托给新 crate `cuda-oxide-codegen`。
2. 画出 `run_pipeline` 的两条职责带：前段「翻译 + 逐函数校验」留在 `mir-importer`，后段由一次 `compile_translated_module(...)` 调用进入 `cuda-oxide-codegen`。
3. 解释 `translator` 的**分层结构**（body / block / statement / rvalue / terminator / values / types）以及贯穿全层的 **alloca + load/store 模型**。
4. 说明一个 rustc MIR 的「基本块」是如何被映射成一串 Pliron IR 块、unwind cleanup 块又是如何被处理的。

本讲是「鸟瞰」（overview）性质：只讲清楚 `mir-importer` 的骨架、它与 `cuda-oxide-codegen` 的新分工边界、以及数据流，不深入单条 MIR 语句如何翻译成具体 op。对 terminator/intrinsic 的逐类翻译、对 lowering 细节的深潜，分别留给 u6-l2（mir-importer 深潜）与 u6-l3（mir-lower 深潜）；对 `cuda-oxide-codegen` 这条独立后端的公共 API 深潜，留给 u6-l6。

## 2. 前置知识

### 2.1 什么是 MIR

rustc 把 Rust 源码经过 AST、HIR，最终降到 **MIR（Mid-level IR，中级中间表示）**。MIR 是一个**有向的控制流图**：

- **基本块（basic block, bb）**：一段顺序执行的语句，末尾挂一个**终止符（terminator）**决定下一个去哪个块。
- **语句（statement）**：主要是 `Assign(place, rvalue)`，即「把右值算出来，写到左值位置上」。
- **终止符（terminator）**：控制流转移，如 `Goto`、`Return`、`Call`、`SwitchInt`。
- **局部变量（local）**：MIR 用编号的局部（`_0` 是返回值，`_1..` 是参数与临时量）传递数据。

MIR 已经做完了借用检查、类型推导，是 rustc 最「接近后端」的、仍然保留 Rust 语义的表示。cuda-oxide 的 device 路径就**从 MIR 接手**，不再回头碰 AST。

### 2.2 什么是 Pliron / dialect

[Pliron](https://github.com/mbartling/pliron) 是一个用 Rust 写的、**MLIR 风格**的 IR 框架。和 MLIR 一样，它的核心思想是**方言（dialect）**：

- 一个 IR 里可以同时存在多个方言，每个方言自带一组 **op（操作）** 和 **类型（type）**。
- cuda-oxide 定义了两个自有方言：`dialect-mir`（高层、贴近 Rust 语义，如 `mir.add`、`mir.store`、`mir.func`）和 `dialect-nvvm`（贴近 PTX/NVVM，如 `warp`、`tma`、`atomic`）。后者会在 u4-l3 详讲。
- Pliron 提供的容器是 **`Context`**（全局 IR 上下文）、**`Operation`**（一个 op 节点）、**`BasicBlock`**（基本块）、**`Region`**（被 op 持有的区域，如函数体）。

如果你用过 MLIR，可以把 `dialect-mir` 类比成 MLIR 里自定义的 dialect，把 `mir.func`/`mir.add` 类比成 `func.func`/`arith.addi`。如果没用过也不必担心——本讲只把它当成「一种带类型的、可打印的 IR 文本」即可。

### 2.3 为什么不直接 MIR → LLVM IR，以及为什么要拆出 codegen 后端

你可能会问两个问题。

**第一**：既然最终要得到 LLVM IR 再生成 PTX，为什么不直接把 MIR 翻译成 LLVM IR？原因在于**分层解耦**：

- MIR 仍然带着 Rust 的高级语义（枚举判别式、变长切片、地址空间未定的引用），一步到位降到 LLVM 会非常臃肿、难维护。
- `dialect-mir` 作为中间层，既保留 Rust 语义、又贴近 IR 框架，方便插入 **`mem2reg`、循环展开**等 pass。
- 真正降到 LLVM 方言的工作由 `mir-lower` crate 完成（u4-l4 / u6-l3），与翻译器解耦。

**第二（本轮 #314 的核心动机）**：`mem2reg`/lowering/导出/`llc` 这些后段 pass **本质上和 rustc 无关**——它们只认 Pliron IR，不需要 `rustc_private`、不依赖某条 nightly。把它们留在 `mir-importer`（一个 `#![feature(rustc_private)]` 的 crate）里，意味着任何想复用这套「dialect-mir → PTX」能力的人都得先匹配一条特定的 nightly rustc。#314 的做法是把后段整体抽成新 crate `cuda-oxide-codegen`：它**没有 rustc 链接**，公共边界就是 `dialect-mir`，任何前端只要能用 Pliron 组装出一个 `dialect-mir` 模块，就能拿到 PTX。`mir-importer` 由此瘦身为「专职翻译器」，并复用同一套后段，使 lowering 流水线在整条工具链里**只存在一份**。

理解了这两点，你就理解了 `mir-importer` 当前的边界：**它只负责 MIR → dialect-mir 及逐函数校验；翻译之后的一切都委托给 `cuda-oxide-codegen`。**

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [crates/mir-importer/src/lib.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/lib.rs) | crate 顶层：架构图（translator → cuda-oxide-codegen 两盒布局）、模块表、对外的 `pub use` 导出 |
| [crates/mir-importer/src/pipeline.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs) | 编排入口 `run_pipeline`：注册方言 → 翻译 + 逐函数校验 → 组装 `BackendOptions`/`ModulePipelineRequest` → 调 `cuda-oxide-codegen` 完成后段 |
| [crates/mir-importer/src/translator/mod.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/mod.rs) | translator 模块入口：`register_dialects`、`translate_function`、分层表 |
| [crates/mir-importer/src/translator/body.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/body.rs) | 函数体翻译：签名、建块、entry allocas、可达块遍历、unwind 块兜底 |
| [crates/mir-importer/src/translator/block.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/block.rs) | 单个基本块内容翻译（先语句、后终止符） |
| [crates/mir-importer/src/translator/values.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/values.rs) | MIR local → alloca 槽位的映射与 slot 操作（emit_alloca/load_local/store_local） |
| [crates/mir-importer/src/translator/statement.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/statement.rs) | 语句翻译（赋值、投影、存储标记） |
| [crates/mir-importer/src/translator/rvalue.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/rvalue.rs) | 右值（表达式）翻译为 dialect-mir op |
| [crates/mir-importer/src/translator/terminator/mod.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/mod.rs) | 终止符翻译：控制流 + GPU intrinsic 分派 |
| [crates/cuda-oxide-codegen/src/pipeline.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs) | 后段编排器 `compile_translated_module`：校验 → mem2reg/unroll → 插 extern → lower → 导出 → PTX/NVVM IR（本讲作为「黑盒对岸」来看） |

辅助记忆：`run_pipeline`（编排）调 `translator::body::translate_body`（每函数一次，做翻译 + 校验），随后**一次** `compile_translated_module` 把整个模块交给 `cuda-oxide-codegen` 跑完后段。`translator` 内部仍是 `body` → `block` → `statement`/`rvalue`/`terminator`，`values` 与 `types` 是贯穿全层的工具层。

## 4. 核心概念与源码讲解

### 4.1 Pliron IR 载体：rustc MIR 之外的第二种 IR

#### 4.1.1 概念说明

cuda-oxide 在 device 路径上其实同时处理**两种** IR：

1. **rustc 的 MIR**：输入，由 rustc 内部数据结构（`rustc_middle::mir`）承载。但 `mir-importer` **不直接**啃 rustc 内部结构，而是经 `stable_mir`（`rustc_public` / `rustc_public_bridge`）拿到一份「跨版本相对稳定」的 MIR 视图。
2. **Pliron 的 `dialect-mir`**：输出，由 `pliron::context::Context` 承载。translator 的工作就是把前者逐节点搬进后者。

为什么需要这一层？因为后续的 `mem2reg`、`lowering` 都建立在 Pliron 的 pass 框架之上——它们只认 Pliron 的 `Operation`/`BasicBlock`，不认 rustc 的 MIR。所以 `dialect-mir` 是「给下游 pass 看的 Rust 语义 IR」。也正因为下游 pass 只认 Pliron、不认 rustc，#314 才能把这些 pass 整体搬进一个「无 rustc 链接」的新 crate（`cuda-oxide-codegen`）里复用。

crate 顶部的文档注释把这件事画成一张图，读它最快建立直觉——而且这张图本轮已经改成「translator → cuda-oxide-codegen」的两盒布局。

#### 4.1.2 核心流程

```text
rustc 内部 MIR (rustc_middle)
        │  rustc_internal::stable()  ← 由 u4-l1 的 device_codegen 完成
        ▼
stable MIR (rustc_public)         ← mir-importer 实际消费的形态
        │  translator::translate_body （逐函数）
        ▼
Pliron Context 持有 dialect-mir   ← 这就是「翻译」的产物
        │  run_pipeline 把整个 module 交给 cuda-oxide-codegen
        ▼
cuda-oxide-codegen: compile_translated_module
   verify → mem2reg/unroll → lower(LLVM dialect) → 导出 .ll → llc → PTX
```

注意图中 `mir-importer` 的产出（Pliron `Context`）**并不落盘成独立的 dialect-mir 文件**——它存在内存里，被 `run_pipeline` 直接喂给 `cuda-oxide-codegen`。能「看到」它的途径只有打开 dump（见 4.4 的实践）。

#### 4.1.3 源码精读

crate 顶层文档注释给出了**新架构图**与模块分工，是本讲最好的总览入口：

[crates/mir-importer/src/lib.rs:18-46](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/lib.rs#L18-L46) —— 注意 ASCII 图现在是**两盒布局**：左侧 `translator` 只做 `MIR → dialect-mir (alloca)`，右侧 `cuda-oxide-codegen` 接手 `mem2reg → unroll → LLVM dialect → LLVM IR → PTX`。这是 #314 之后 cuda-oxide 全局最重要的职责切分。模块表里 [`translator`] 的说明也明确写着「then hands that module to the shared `cuda-oxide-codegen` backend」。

紧接着的文档说明了贯穿 translator 的核心建模：

[crates/mir-importer/src/lib.rs:63-70](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/lib.rs#L63-L70) —— **alloca + load/store 模型**：每个非 ZST 的 MIR local 都物化成一个 `mir.alloca`，写为 `mir.store`、读为 `mir.load`，跨块数据流走槽位而非块参数；`mem2reg`（现在住在 `cuda-oxide-codegen` 的 `prepare_mir_module` 里）之后再把标量槽位提升回 SSA。这个模型会在 4.3 重点展开。

crate 对外导出的「门面」类型（下游 `device_codegen` 只用这几个）：

[crates/mir-importer/src/lib.rs:86-89](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/lib.rs#L86-L89) —— `run_pipeline`、`PipelineConfig`、`CollectedFunction`、`CompilationResult` 等。注意 `DeviceExternDecl`/`DeviceExternType` 等现在是「从 `cuda-oxide-codegen` 重新导出（re-export）」的——因为后段已搬走，但 `mir-importer` 仍把这层门面透传给 `rustc-codegen-cuda`，使上层调用者**完全无感**。

#### 4.1.4 代码实践

这是一个纯阅读型实践，目标是把「两种 IR」与「两段 crate」的边界看清楚。

1. **实践目标**：确认 `mir-importer` 消费的是 stable MIR、产出的是 Pliron `Context`，并定位两者之间的桥接点；同时确认后段入口是 `cuda-oxide-codegen`。
2. **操作步骤**：
   - 打开 [crates/mir-importer/src/lib.rs:72-79](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/lib.rs#L72-L79)，看 `extern crate rustc_public;` 与 `extern crate rustc_public_bridge;`，确认它依赖的是 stable MIR 而非 rustc 内部类型。
   - 在 `translate_function` 签名里找到参数 `body: &mir::Body`（来自 `rustc_public::mir`）与返回值 `Ptr<Operation>`（Pliron 操作指针），见 [translator/mod.rs:90-96](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/mod.rs#L90-L96)。
   - 再看 `pipeline.rs` 顶部的 `use cuda_oxide_codegen::__private::{...}`（[pipeline.rs:40-44](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs#L40-L44)），确认 `compile_translated_module`/`verify_operation`/`append_to_module` 等后段原语来自新 crate。
3. **需要观察的现象**：函数签名「左侧是 stable MIR、右侧是 Pliron」正是「翻译器」一词的字面体现；而 `pipeline.rs` 顶部那一行 `use cuda_oxide_codegen::__private::{...}` 则是「后段已搬走」的字面证据。
4. **预期结果**：你能用一句话说出「`translate_function` 把一个 `rustc_public::mir::Body` 变成一个 Pliron `Operation`；`run_pipeline` 再把这个模块交给 `cuda-oxide-codegen::compile_translated_module`」。
5. 「待本地验证」：本实践无需运行任何命令。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `mir-importer` 通过 `rustc_public`（stable MIR）而不是直接用 `rustc_middle::mir`？

**参考答案**：stable MIR 是 rustc 提供的、跨 nightly 版本相对稳定的接口，能把 translator 与 rustc 内部数据结构的频繁变动隔离开；同时 `#![feature(rustc_private)]` 已经让 crate 能访问内部类型，但「能访问」不等于「应该依赖」。stable MIR 是为这类外部工具专门设计的契约层。

**练习 2**：`dialect-mir` 的 IR 在哪一步会落盘成一个文件？

**参考答案**：默认不落盘。`dialect-mir` 只存在于内存里的 Pliron `Context` 中，被 `run_pipeline` 直接喂给 `cuda-oxide-codegen`。落盘的产物是更下游的 `*.ll`（LLVM IR）与 `*.ptx`。要看 dialect-mir 文本需要打开 dump（见 4.4）。

**练习 3**：为什么 `PipelineError`/`DeviceExternDecl` 这些类型在 `mir-importer` 里是 `pub use` 而不是自己定义？

**参考答案**：因为它们的真正实现已经随 #314 搬进 `cuda-oxide-codegen`。`mir-importer` 只是把它们 re-export，维持「上层 `rustc-codegen-cuda` 调用面不变」这一兼容承诺；逻辑上的单一真相源在 `cuda-oxide-codegen`，避免两份定义漂移。

---

### 4.2 mir-importer 的翻译职责与后段委托

#### 4.2.1 概念说明

`run_pipeline` 是 `mir-importer` 对外的「一键编译」入口。它把「一批已收集的设备函数（`CollectedFunction` 列表）」一路推到「PTX 或 NVVM IR 文件」。但本轮 #314 之后，要把它理解成**两段职责的编排者**，而不是「什么都自己干」的巨型函数：

```text
┌─ mir-importer::run_pipeline ──────────────────────────────────────┐
│  前段（自己干）：                                                   │
│    注册方言 → 建模块 → 逐函数：translate_body → dump → verify → append │
│  后段（委托）：                                                     │
│    组装 BackendOptions + ModulePipelineRequest                     │
│    → 一次 compile_translated_module(ctx, module, &request)          │
└─────────────────────────┬─────────────────────────────────────────┘
                          ▼
┌─ cuda-oxide-codegen::compile_translated_module ───────────────────┐
│  verify → prepare(mem2reg+unroll) → 插 device externs → lower      │
│  → 检测 libdevice → 必要时 NVVM 合法化 → verify LLVM → 导出 .ll     │
│  → 跑 llc 出 PTX（或跳过 llc、产出 NVVM IR）                         │
└────────────────────────────────────────────────────────────────────┘
```

注意一个易混点：尽管函数叫 `run_pipeline`、文件叫 `pipeline.rs`，但**「translator 把 MIR 翻成 dialect-mir」只是前段的一步**；而后段那一长串 pass（mem2reg、循环展开、lowering、导出、llc）现在**全部不在 `mir-importer` 里**，它们住在 `cuda-oxide-codegen` 的 `compile_translated_module`。`run_pipeline` 对后段的全部「调用」就是 [pipeline.rs:322](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs#L322) 那一行 `compile_translated_module(...)`。

这样设计的好处已在 2.3 说过：后段与 rustc 无关，抽出后可被「无 nightly」的实验性前端复用（见 u6-l6），并保证 lowering 流水线全工具链只此一份。`mir-importer` 因此从一个 ~4500 行的庞然大物瘦身成一个 ~600 行的「专职翻译器 + 编排器」。

#### 4.2.2 核心流程

`run_pipeline` 前段自己的步骤：

```text
1. 注册 dialect-mir / dialect-nvvm / builtin 方言
2. 建一个 builtin.module 容器
3. 对每个 CollectedFunction：
     a. instance.body() 取 stable MIR
     b. translator::body::translate_body(...) → 一个 mir.func 操作
     c. （若开启）dump 这份「pre-verify」的 dialect-mir
     d. verify_operation 校验这一个函数
     e. append_to_module 挂到模块上
```

随后组装一份 `BackendOptions`（由 `CUDA_OXIDE_TARGET` / `CUDA_OXIDE_DEVICE_ARCH` / `CUDA_OXIDE_NO_FMA` 等环境变量在 `mir-importer` 边界读一次，[pipeline.rs:295](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs#L295)），把它和输出文件路径、trace sink 一起打包成 `ModulePipelineRequest::for_rust_pipeline(...)`，交给 `cuda-oxide-codegen`。**关键设计**：后段里的每一个 `CUDA_OXIDE_*` 环境读取都被替换成了显式的 `BackendOptions` 字段——`mir-importer` 在自己的边界构造一份，后段不再偷偷读环境，所以 `rustc-codegen-cuda` 与 `cargo-oxide` 完全无感。

后段 `compile_translated_module`（本讲当黑盒看，深潜见 u6-l6）会按序做：整模块 verify → `prepare_mir_module`（非 full-debug 时跑 mem2reg + 带注解循环展开）→ 插入 device-extern 声明 → `lower_to_llvm` → 检测 `__nv_*` libdevice（若有则改走 NVVM IR、跳过 llc）→ 必要时 NVVM 合法化 → verify LLVM 方言 → 导出 `.ll` → 跑 `llc` 出 `.ptx`。

几条关键判断值得记住（这些判断的**实现**都在 `cuda-oxide-codegen` 里，但 `mir-importer` 通过 `BackendOptions`/`PipelineConfig` 控制它们）：

- **full-debug 跳过 mem2reg/unroll**：当 `debug_kind.variables_enabled()`（cuda-gdb 可读变量的 `-G` 风格构建）时，源变量必须留在稳定的栈槽里，所以 `prepare_mir_module` 收到 `promote_and_unroll = false`。这一点 u7-l2 会用到。
- **libdevice 自动改道**：如果 lowering 后出现 `__nv_*`（如 `__nv_sinf`），`llc` 无法解析，必须走 libNVVM + nvJitLink，于是后段改产出 NVVM IR（`.ll`）并跳过 llc。这会在 u4-l5 详讲。
- **目标架构自动探测**：后段会在导出的 LLVM 文本里扫描 PTX 指令特征（WGMMA、TMA、tcgen05、bf16x2……），据此选 `sm_XX`，可用 `CUDA_OXIDE_TARGET` 覆盖。
- **FMA 策略穿透**：`config.allow_fma_contraction` 经 `backend_options.no_fma` 透传，后段同时控制「IR 层的 `contract` 快速数学标志」与「`llc -fp-contract=fast`」两道闸门（见 u6-l3、u4-l5）。

#### 4.2.3 源码精读

`pipeline.rs` 顶部的文档仍然把**概念上的完整流水线**列出来（翻译 → verify → mem2reg → unroll → LLVM dialect → LLVM IR → PTX），并附一张「GPU 特征 → 目标架构」表。读它时要注意：这是「`run_pipeline` 编排的端到端流程」，但其中后段的**实现**已委托出去——文档描述的是 orchestrator 视角：

[crates/mir-importer/src/pipeline.rs:6-46](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs#L6-L46) —— 这张「GPU 特征 → 目标架构」表是后续 u5（高级设备能力）各讲的「架构门禁」依据，例如 WGMMA→`sm_90a`、tcgen05→`sm_100a`、INT8 `mma.m16n8k32`→`sm_80`。

`run_pipeline` 的签名与文档：

[crates/mir-importer/src/pipeline.rs:182-215](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs#L182-L215) —— 三个入参：`functions`（设备函数集）、`device_externs`（外部设备函数声明，用于 LTOIR FFI）、`config`（`PipelineConfig`）。注意文档里那段「# Pipeline Steps」列的 9 步，1～3 步在本函数体里，4～9 步现在等于「调一次 `compile_translated_module`」。

第 1～3 步（注册方言、建模块、逐函数翻译）：

[crates/mir-importer/src/pipeline.rs:216-285](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs#L216-L285) —— 对每个 `CollectedFunction` 取出 `instance.body()`，调 `translator::body::translate_body`，dump（若开启）、`verify_operation`、再 `append_to_module`。注意 dump 故意放在 verify **之前**，这样即使校验失败，用户也能在 `--show-mir-dialect` 里看到出问题的 IR。这里 `verify_operation` 与 `append_to_module` 都是从 `cuda_oxide_codegen::__private` 导入的后段原语。

后段委托那一「手」：

[crates/mir-importer/src/pipeline.rs:295-322](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs#L295-L322) —— 先 `BackendOptions::from_env()` 在 mir-importer 边界读环境、再用 `config` 字段覆盖（`target_arch`/`device_arch_hint`/`verbose`/`no_fma`），打包成 `ModulePipelineRequest::for_rust_pipeline(...)`，最后 `compile_translated_module(&mut ctx, module_op_ptr, &request)`。这一行就是「后段委托」的全部入口。

后段返回后的收尾（NVVM 制品要写 `.target`/`.options` 边车）：

[crates/mir-importer/src/pipeline.rs:324-352](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs#L324-L352) —— 注意这两个边车写盘**故意留在 `mir-importer`**：`.target`/`.options` 是宿主制品（`oxide-artifacts`）关心的事，不该污染那个「无 rustc」的实验性后端（见函数 [write_nvvm_target_sidecar](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs#L375-L394) 的文档注释）。

「对岸」的 `compile_translated_module`（本讲只看它的阶段标题，确认它就是后段的单一入口）：

[crates/cuda-oxide-codegen/src/pipeline.rs:150-181](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L150-L181) —— 整模块 `verify` + `prepare_mir_module`（mem2reg/unroll）。注意 `promote_and_unroll = !request.debug_kind.variables_enabled()` 这条分支就是「full-debug 跳过提升」的落点。

[crates/cuda-oxide-codegen/src/pipeline.rs:206-211](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L206-L211) —— `lower_to_llvm(ctx, module, !request.backend.no_fma)`，把 dialect-mir 交给 `mir-lower`，并把 FMA 策略透传（这条线在 u6-l3 与 FMA 收缩策略相关）。

[crates/cuda-oxide-codegen/src/pipeline.rs:321-374](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L321-L374) —— PTX/NVVM 分叉：emit NVVM IR 时跳过 llc、直接返回；否则调 `generate_ptx` 跑 llc。

#### 4.2.4 代码实践

源码阅读型实践：对照 `run_pipeline` 的两段职责，逐一指出「前段在 `mir-importer`、后段在 `cuda-oxide-codegen`」的分界点。

1. **实践目标**：能从 `pipeline.rs` 的 `run_pipeline` 函数体里指出前段（翻译 + 逐函数校验）的源码段落，并定位那「一行」把控制权交给 `cuda-oxide-codegen` 的调用。
2. **操作步骤**：
   - 打开 [pipeline.rs:211](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs#L211) 起的函数体。
   - 找到注册方言（[L221](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs#L221)）、建模块（[L224-L230](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs#L224-L230)）、逐函数翻译循环（[L235-L285](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs#L235-L285)）。
   - 找到那行 `compile_translated_module`（[L322](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs#L322)），翻进它的定义 [cuda-oxide-codegen/src/pipeline.rs:152](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L152)，确认 mem2reg（[L181](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L181)）、lower（[L211](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L211)）、生成 PTX（[L345-L362](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L345-L362)）都在对岸。
3. **需要观察的现象**：`run_pipeline` 函数体里**没有**任何 `mem2reg`/`unroll`/`lower`/调 `llc` 的代码——它们全在 `compile_translated_module` 内部。这正是「后段委托」的可读证据。
4. **预期结果**：你能口头复述「`mir-importer` 只翻译 + 校验，剩下的一行调用甩给 `cuda-oxide-codegen`」。
5. 「待本地验证」：本实践无需运行命令。

#### 4.2.5 小练习与答案

**练习 1**：#314 之后，`mem2reg` 跑在哪一侧？为什么这样切分？

**参考答案**：跑在 `cuda-oxide-codegen` 的 `compile_translated_module` → `prepare_mir_module` 里（[pipeline.rs:181](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L181)）。切分依据是「是否依赖 rustc」：`mem2reg` 是 Pliron 上的通用 SSA 提升 pass，与 rustc 无关，所以归入无 rustc 链接的后段，既可被实验性前端复用，又保证 lowering 流水线全工具链只此一份。

**练习 2**：`run_pipeline` 里那些 `CUDA_OXIDE_*` 环境变量是怎么传到后段的？

**参考答案**：`mir-importer` 在自己边界调一次 `BackendOptions::from_env()`（[pipeline.rs:295](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs#L295)），再用 `config` 字段覆盖若干项，把结果塞进 `ModulePipelineRequest`。后段**不再自己读环境**，只消费显式的 `BackendOptions`。这样既让后段可在无 nightly 的实验性环境复用，又让 `rustc-codegen-cuda`/`cargo-oxide` 上层调用面完全不变。

**练习 3**：为什么 NVVM 的 `.target`/`.options` 边车写盘留在 `mir-importer`，而不是搬进 `cuda-oxide-codegen`？

**参考答案**：因为这两个 sidecar 是**宿主制品**（`oxide-artifacts` 的 `.oxart` bundle 关心的 FMA 策略与目标）的契约，属于「rustc 前端这条链路」的下游约定。`cuda-oxide-codegen` 定位为「无 rustc、产出 PTX/NVVM IR 文件」的纯后端，不该掺入宿主制品语义；把边车写盘留在 `mir-importer`，既守住后端的纯粹性，又不破坏 rustc 链路的现有行为（接 u3-l2）。

---

### 4.3 translator 分层与 alloca + load/store 模型

#### 4.3.1 概念说明

`run_pipeline` 前段的「翻译」背后，是 `translator` 模块。它采用**与 MIR 结构一一对应的分层**：

| translator 子模块 | 对应 MIR 概念 | 职责 |
|---|---|---|
| `body` | 整个函数体 | 建函数、建块、entry allocas、遍历可达块 |
| `block` | 一个基本块 | 按序翻译语句，最后翻译终止符 |
| `statement` | 语句（`Assign` 等） | 赋值、投影、存储标记 |
| `rvalue` | 右值（表达式） | 二元运算、cast、聚合等 → dialect-mir op |
| `terminator` | 终止符 | 控制流 + GPU intrinsic 分派 |
| `values` | 局部变量 | local → alloca 槽位映射 |
| `types` | 类型 | Rust 类型 → dialect-mir 类型 |

贯穿全层的是 **alloca + load/store 模型**。它的核心思想非常简单：

> 每个非 ZST 的 MIR local，都在函数入口块顶部申请一个栈槽 `mir.alloca`。对该 local 的「写」变成 `mir.store` 到这个槽，「读」变成 `mir.load` 从这个槽。**跨基本块的数据流一律走槽位，块之间不传参。**

这样做的好处是：translator 不需要在翻译每个块时都维护一套 SSA 构造算法（SSA 构造是出了名的麻烦），只需要「写槽 / 读槽」两条简单规则；把「槽位 → SSA」这件难事交给成熟的 `mem2reg` pass 统一完成（mem2reg 现在住在 `cuda-oxide-codegen` 里，但它的输入输出契约不变）。

#### 4.3.2 核心流程

一个 MIR 函数被翻译的完整调用链（来自 `translator/mod.rs` 的文档）：

```text
translate_function()                       // 入口：建 builtin.module
  └─▶ body::translate_body()               // 函数级
        ├─▶ emit_entry_allocas()           // 每个 non-ZST local 一个 alloca
        └─▶ 对每个可达块：
              └─▶ block::translate_block()
                    ├─▶ statement::translate_statement()   // 逐条语句
                    │     └─▶ rvalue::translate_rvalue()   // 右值
                    └─▶ terminator::translate_terminator() // 末尾终止符
```

数据如何在块间流动？用 terminator/mod.rs 文档里那个经典例子最直观：

```text
// Rust MIR
bb0: { _1 = 42_i32; goto -> bb1 }
bb1: { _0 = _1;     return }

// dialect-mir（mem2reg 之前）
^bb0:
  %s1 = mir.alloca          : !mir.ptr<i32>
  %c  = mir.constant 42_i32 : i32
  mir.store %c, %s1
  mir.goto ^bb1                    // 零操作数；_1 经 %s1 流动
^bb1:                              // 没有块参数
  %r = mir.load %s1 : i32
  mir.return %r : i32

// mem2reg 之后塌缩成
//   mir.return %c : i32
```

这个例子同时回答了两个问题：块间怎么传数据（走槽位）、mem2reg 做了什么（把槽位往返折叠成 SSA）。

#### 4.3.3 源码精读

`translator/mod.rs` 顶部的模块结构表与调用流程图：

[crates/mir-importer/src/translator/mod.rs:11-44](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/mod.rs#L11-L44) —— 这张表对应上面的分层表，并把 alloca 模型再强调了一遍。

`register_dialects`：translator 开始前必须把会用到的方言都注册进 `Context`：

[crates/mir-importer/src/translator/mod.rs:67-80](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/mod.rs#L67-L80) —— 注册 `dialect-mir`（建模 Rust 语义）与 `dialect-nvvm`（GPU intrinsic，如 thread/block/warp）。注释说明 builtin 方言（`ModuleOp` 等）由 pliron 0.14 自动注册。

`translate_function`：对外「单函数翻译」入口（主要供测试/工具用，真实 pipeline 走的是 `body::translate_body`）：

[crates/mir-importer/src/translator/mod.rs:90-150](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/mod.rs#L90-L150) —— 它建一个 `builtin.module`，把 `translate_body` 产出的 `mir.func` 塞进去。注意它固定 `is_inline_always = false`（真实 pipeline 会从 rustc 的 `CodegenFnAttrs` 取这个标志并透传，接 u6-l1）。

alloca 模型的「槽位账本」`ValueMap`：

[crates/mir-importer/src/translator/values.rs:52-86](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/values.rs#L52-L86) —— 一个 `Vec<Option<Value>>`，按下标对应 MIR local 编号；ZST local 留 `None`。`get_slot`/`set_slot` 是它的读写接口。

三个 slot 操作的 emitter：

[crates/mir-importer/src/translator/values.rs:88-172](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/values.rs#L88-L172) —— `emit_alloca` 发 `mir.alloca`，`load_local` 发 `mir.load`，`store_local` 发 `mir.store`。`store_local` 还会在指针地址空间不一致时自动插一个 `mir.cast <PtrToPtr>`（见 `maybe_ptr_coerce`，[values.rs:178](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/values.rs#L178)），这是 GPU 地址空间（shared/global/constant）正确性的关键，与 u2-l3 共享内存、u5 系列直接相关。

`emit_entry_allocas` 的文档把 alloca 模型讲得最完整：

[crates/mir-importer/src/translator/body.rs:570-649](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/body.rs#L570-L649) —— 函数入口块顶部，对每个 non-ZST local 发一个 alloca、记录到 `value_map`，再把函数参数 `store` 进各自的槽；返回最后一条 op，供后续语句「接在后面」追加（否则会被 `insert_at_front` 推到 alloca 链前面）。注意它还会从「写入」而非「声明类型」推断指针 local 的地址空间（`SlotAddrSpaceMap`）。

各分层模块的「能力表」也很值得扫一眼，能快速知道哪些 Rust 写法已被支持：

- 语句表：[statement.rs:10-20](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/statement.rs#L10-L20)（`Assign`/`StorageLive`/`SetDiscriminant`/`Nop`，以及投影的专用路径）。
- 右值表：[rvalue.rs:10-20](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/rvalue.rs#L10-L20)（`BinaryOp`→`mir.add/sub/mul/div`、`Cast`→`mir.cast` 等）。

#### 4.3.4 代码实践

源码阅读 + 对照式实践：用一个真实内核，把它的 Rust 写法对应到 translator 各层的 op。

1. **实践目标**：对一个最简内核（vecadd），手工把它的每条语句「投射」到 translator 的分层表上。
2. **操作步骤**：
   - 打开 [examples/vecadd/src/main.rs:39-46](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs#L39-L46) 的 `vecadd` 内核。
   - 逐句判断它会落到哪个分层模块：`thread::index_1d()` → `terminator`（intrinsic 分派，落到 `dialect-nvvm` 的特殊寄存器 op）；`a[idx] + b[idx]` → `rvalue`（`mir.add`）；`*c_elem = ...` → `statement`（投影写）。
3. **需要观察的现象**：你会发现「读线程号」和「算加法」分属两个不同的分层模块，前者还要跨到 `dialect-nvvm`。
4. **预期结果**：你能画出 vecadd 内核对应的「translator 调用链」草图。
5. 「待本地验证」：本实践无需运行命令；真实的 op 文本在 4.4 的 pipeline 实践里观察。

#### 4.3.5 小练习与答案

**练习 1**：为什么 translator 不直接构造 SSA，而要绕一圈「alloca + load/store 再 mem2reg」？

**参考答案**：因为 SSA 构造（需要支配边界、φ 节点插入点等）实现复杂、易错。alloca + load/store 是一个**机械、局部**的翻译方案：每个 local 一个槽、写即 store、读即 load，不需要在翻译时做任何跨块分析。把「提升回 SSA」这件难事交给成熟、可复用的 `mem2reg` pass，换来 translator 的简单与正确。

**练习 2**：`store_local` 为什么会在某些情况下自动插入一个 `mir.cast <PtrToPtr>`？

**参考答案**：Rust 的引用/裸指针类型本身不携带 GPU 地址空间信息（`&mut f32` 翻译出来是 generic 指针），但实际写入槽位的值可能是具体地址空间的指针（如 `addrspace(3)` 的共享内存指针）。两者指针布局相同但类型不同，于是用一个免费的 `PtrToPtr` cast 桥接，避免后续 lowering 走到错误的（generic）访存路径。

---

### 4.4 MIR 基本块映射到 Pliron IR

#### 4.4.1 概念说明

translator 要解决的最后一个结构性问题是：**rustc MIR 的基本块图，怎么变成 Pliron IR 的基本块图？** 这里有三个细节决定了正确性：

1. **可达性**：MIR 里有些块只是 unwind cleanup（栈展开清理），而 CUDA 工具链根本不支持栈展开——这些块在 GPU 上是死代码。translator 用 BFS 从入口块出发，只翻译真正可达的块。
2. **块参数**：因为 alloca 模型让数据走槽位，**只有入口块带参数**（函数形参），其余块都是零参数的。这让所有分支终止符都是「零操作数」的，结构极简。
3. **死块的兜底**：Pliron 要求每个块都有终止符。被跳过的 unwind 块不能空着，translator 给它们补一个 `mir.unreachable`，让校验通过，后续 pass 可把它们当死代码删掉。

#### 4.4.2 核心流程

`translate_body` 把 MIR 块图搬进 Pliron 的三阶段（来自其文档）：

```text
PHASE 1   建 Pliron 块：每个 MIR 块一个 Pliron 块；仅入口块带函数形参
PHASE 1.5 entry allocas：入口块顶部为每个 non-ZST local 发 alloca，存入函数参数
PHASE 2   按「可达块」集合逐块翻译（block::translate_block）
          不可达的 unwind 块补 mir.unreachable
```

可达块的计算是一个从入口块（索引 0）出发、沿「非 unwind 后继」做 BFS 的过程；unwind cleanup 块最终落在可达集之外。

#### 4.4.3 源码精读

BFS 可达性分析：

[crates/mir-importer/src/translator/body.rs:215-253](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/body.rs#L215-L253) —— `non_unwind_successors`（[L222-L232](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/body.rs#L222-L232)）的关键判断是：`Drop`/`Call`/`Assert` 等终止符的 unwind 目标被剔除，只保留「正常」目标；`compute_reachable_blocks`（[L240-L253](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/body.rs#L240-L253)）从入口块出发做 BFS。注释点明 CUDA 工具链不接栈展开。

PHASE 1 + PHASE 1.5 + PHASE 2 的主循环：

[crates/mir-importer/src/translator/body.rs:943-1035](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/body.rs#L943-L1035) —— 注意三处：建块时只有 `idx == 0` 用 `arg_types`（[L952-L953](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/body.rs#L952-L953)）；entry allocas 在 [L979-L987](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/body.rs#L979-L987) 调 `emit_entry_allocas`；可达块逐块翻译在 [L1001-L1017](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/body.rs#L1001-L1017)；最后给未处理块补 `mir.unreachable` 在 [L1023-L1035](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/body.rs#L1023-L1035)。

单块内容翻译（先语句后终止符）：

[crates/mir-importer/src/translator/block.rs:30-77](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/block.rs#L30-L77) —— `translate_block` 用 `prev_op` 这条「游标」把语句一条条 `insert_after` 串起来，保证 IR 文本顺序与 MIR 一致；终止符最后翻译。注释强调「块本身由 `body` 创建，本模块只负责填充内容」。

`translate_body` 的总体文档（含 `gpu_kernel` 属性注入等细节）：

[crates/mir-importer/src/translator/body.rs:651-672](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/body.rs#L651-L672) —— 还能看到 `#[kernel]` 时给 `mir.func` 加 `gpu_kernel` 属性（落点在 [body.rs:812-816](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/body.rs#L812-L816)），扫描 `#[cluster(...)]`/`#[launch_bounds(...)]` 标记注入 `cluster_dim_*`/`maxntid` 属性（接 u2-l4、u5-l4、u7-l2）。

#### 4.4.4 代码实践（本讲主实践）

这是本讲唯一需要实际运行（或阅读运行逻辑）的实践：用 `cargo oxide pipeline` 把 vecadd 的 dialect-mir 中间产物「显形」，对照 `pipeline.rs` 与 `cuda-oxide-codegen` 说明它经过了哪几个阶段——并借此**亲眼验证「翻译在 mir-importer 结束、后段在 cuda-oxide-codegen 继续」**。

1. **实践目标**：亲眼看到 translator 产出的 `dialect-mir`（preparation 前后两份）、lowering 后的 LLVM IR、最终 PTX，并把它们对回两段 crate 的职责；特别要确认「mem2reg/lowering/PTX 的阶段标题」来自 `cuda-oxide-codegen`。
2. **操作步骤**：
   - 对照 `mir-importer/README.md` 的新架构图（[README.md:12-22](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/README.md#L12-L22)）：上半 `mir-importer` 只做「Stable MIR → dialect-mir translation」，下半 `cuda-oxide-codegen` 做「verify → mem2reg/unroll → lower → LLVM export → PTX/NVVM IR」。
   - 进入仓库根目录，运行：
     ```bash
     cargo oxide pipeline vecadd
     ```
   - `pipeline` 子命令（`cargo-oxide` 的 `codegen_show_pipeline`）会设置 `CUDA_OXIDE_DUMP_MIR=1` 与 `CUDA_OXIDE_DUMP_LLVM=1`（[commands.rs:1923-1924](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/commands.rs#L1923-L1924)），经后端映射成 `PipelineConfig::show_mir_dialect=true`（[device_codegen.rs:682-693](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/device_codegen.rs#L682-L693)），再被 `run_pipeline` 透传成后段 trace 的 `dump_mir`。
3. **需要观察的现象**：在 stderr 里应能看到分属**两段 crate** 的阶段标题：
   - 来自 `mir-importer`（逐函数）：`Translating kernel: vecadd`、`=== dialect-mir func: vecadd (pre-verify) ===`（[pipeline.rs:237-279](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs#L237-L279)）。
   - 来自 `cuda-oxide-codegen`（整模块后段）：`=== dialect-mir module (pre-verify) ===`、`=== Running shared mem2reg + annotated loop-unroll preparation ===`、`=== dialect-mir module (after preparation) ===`、`=== Lowering dialect-mir → LLVM dialect ===`、`=== Generating PTX ===`（[cuda-oxide-codegen/src/pipeline.rs:160-342](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-oxide-codegen/src/pipeline.rs#L160-L342)）。
   - 构建结束后，落盘产物 `vecadd.ll`（LLVM IR）与 `vecadd.ptx`（PTX）位于示例目录下。
4. **预期结果**：你能指出「`Translating ...` 标题是 mir-importer 打的、`Running shared mem2reg ...` 之后所有标题是 cuda-oxide-codegen 打的」，从而用运行时证据坐实那条 `compile_translated_module` 边界；并在 `vecadd.ll` 里看到 `mir.func vecadd` 已经被 lower 成 `define void @vecadd(...)`，在 `vecadd.ptx` 里看到 `.entry vecadd`。
5. 「待本地验证」：上面所有「应能看到」的具体输出文本均依赖本机具备 nightly 工具链与 `llc-21+`（见 u1-l3）。若本机缺 GPU，`pipeline` 仍可工作（它只编译不运行内核）；若工具链不全，则按 u1-l3 的 `cargo oxide doctor` 先补齐。若无法运行，请改为阅读 `run_pipeline` 与 `compile_translated_module` 两段源码，对照上面的阶段标题逐段确认。

#### 4.4.5 小练习与答案

**练习 1**：假如一个内核用了 `Result` 且写了 `?`，MIR 里会带 unwind cleanup 块。translator 怎么处理它们？

**参考答案**：`non_unwind_successors` 在收集后继时把 `Call`/`Drop` 等终止符的 unwind 目标剔除，`compute_reachable_blocks` 的 BFS 因此不会遍历到这些 cleanup 块。它们落在可达集之外，被 [body.rs:1023-1035](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/body.rs#L1023-L1035) 补上一个 `mir.unreachable` 终止符以满足 Pliron 校验，后续 pass 会把它们当死代码删掉。

**练习 2**：为什么除了入口块，所有 Pliron 块都是「零参数」的？

**参考答案**：因为 alloca 模型让跨块数据流全部走 local 的栈槽（`mir.store`/`mir.load`），不需要在块之间显式传值。只有入口块需要带参数——那是函数形参的落脚点，`emit_entry_allocas` 会立刻把它们 store 进各自槽位。这同时让所有分支终止符（`mir.goto`/`mir.cond_br`/`mir.switch`）都是零操作数的，结构最简。

---

## 5. 综合实践

把本讲四个模块串起来，做一个「静态追踪 + 动态对照」的小任务：给 vecadd 的内核**加一个条件分支**，然后观察它在两段 crate 各阶段里的形态变化。

具体步骤：

1. 复制 vecadd 示例（或直接改 `examples/vecadd/src/main.rs` 的 `vecadd` 内核），把内核体改成带一个 `if idx_raw % 2 == 0` 的分支，例如偶数线程做加法、奇数线程做减法：
   ```rust
   #[kernel]
   pub fn vecadd(a: &[f32], b: &[f32], mut c: DisjointSlice<f32>) {
       let idx = thread::index_1d();
       let idx_raw = idx.get();
       if let Some(c_elem) = c.get_mut(idx) {
           if idx_raw % 2 == 0 {
               *c_elem = a[idx_raw] + b[idx_raw];
           } else {
               *c_elem = a[idx_raw] - b[idx_raw];
           }
       }
   }
   ```
   > 这是「示例代码」，仅用于说明；真实 vecadd 示例不含此分支。
2. 运行 `cargo oxide pipeline vecadd`，在 stderr 的 dialect-mir dump 里找到你的 `mir.func`。
3. 验证你对本讲的掌握，回答：
   - **4.1（Pliron 载体 + 新架构）**：核对 README 架构图，确认「翻译」标题属于 `mir-importer`、「mem2reg/lower/PTX」标题属于 `cuda-oxide-codegen`——这次带分支的内核不会改变这一边界。
   - **4.2（翻译职责与后段委托）**：分支会让块变多，但 `run_pipeline` 在 `mir-importer` 里仍只做翻译 + 逐函数校验，随后那行 `compile_translated_module` 把整模块甩给后段。确认你仍能看到 `pre-verify → preparation(mem2reg) → after preparation → lowering → PTX` 这条**横跨两 crate** 的标题链。
   - **4.3（alloca 模型）**：在「pre-verify」dump 里，应能看到 `mir.alloca`、`mir.store`、`mir.load`；在「after preparation」里它们应被折叠成 SSA 值（不再有针对标量 local 的 load/store 往返）。对照 terminator/mod.rs 的经典例子说明你看到了同样的「塌缩」。
   - **4.4（块映射）**：分支应在 dialect-mir 里产生至少两个目标块；`if` 在 MIR 里通常变成 `SwitchInt` 终止符，会被 `terminator` 模块翻译成 `mir.cond_br`。确认它是零操作数的（数据靠槽位流动，不靠块参数）。
4. 最后核对正确性：若本机有 GPU，`cargo oxide run vecadd` 应输出「偶数下标相加、奇数下标相减」的结果。

> 这个任务同时触动了本讲的全部四个最小模块，并自然地为下一讲（u4-l3 方言层）、u6-l2（terminator 深潜）与 u6-l6（独立后端深潜）埋下伏笔——你会开始好奇 `SwitchInt` 到底分派到哪个 dialect op，以及 `cuda-oxide-codegen` 那条无 rustc 后端长什么样。

## 6. 本讲小结

- `mir-importer` 的职责已**收缩为**：把 rustc 的 stable MIR 翻译成 Pliron 的 `dialect-mir`，并对每个函数做一次校验。后段（mem2reg、循环展开、lowering、导出、跑 `llc` 出 PTX）整体委托给新 crate `cuda-oxide-codegen`。
- `run_pipeline` 现在是**两段职责的编排者**：前段「注册方言 → 建模块 → 逐函数 translate_body + dump + verify + append」留在 `mir-importer`；后段是一次 `compile_translated_module(ctx, module, &request)` 调用进入 `cuda-oxide-codegen`。
- 后段所有 `CUDA_OXIDE_*` 环境读取被替换为显式的 `BackendOptions`，由 `mir-importer` 在边界构造一份，使 `rustc-codegen-cuda`/`cargo-oxide` 上层调用面**完全无感**；`PipelineError`/`DeviceExternDecl` 等类型经 re-export 保持门面不变。
- translator 采用**与 MIR 一一对应的分层**（body / block / statement / rvalue / terminator / values / types），并贯穿 **alloca + load/store 模型**：每个 non-ZST local 一个栈槽，写即 store、读即 load，把 SSA 构造这件难事推迟给（现已搬走的）mem2reg。
- MIR 块图的映射有三个要点：BFS 只翻译可达块（unwind cleanup 被当死代码）、仅入口块带函数形参、不可达块补 `mir.unreachable` 兜底校验。
- `dialect-mir` 默认只在内存里；要看它需用 `cargo oxide pipeline`（打开 `CUDA_OXIDE_DUMP_MIR`），落盘的可见产物是下游的 `.ll` 与 `.ptx`。stderr 里的阶段标题天然分两色，可用来运行时验证那条「翻译/后段」边界。
- 本讲全程是「鸟瞰」：单条 MIR 语句如何落到具体 op、terminator 如何分派 GPU intrinsic、lowering 内部细节、独立后端的公共 API，分别留给 u6-l2、u6-l3 与 u6-l6。

## 7. 下一步学习建议

- **u4-l3（MLIR 方言层）**：本讲反复提到 `dialect-mir` 与 `dialect-nvvm`，下一讲正式拆解这两个方言各自提供哪些 op、它们如何承接「Rust 语义 → GPU 指令」的过渡。
- **u4-l4（MIR Lowering 鸟瞰）**：本讲后段里的 `lower_to_llvm` 把控制权交给了 `mir-lower`，下一讲鸟瞰那个 crate 的 ops/intrinsics 两类转换入口。
- **u6-l2（mir-importer 深潜：terminator/intrinsics 翻译机）**：当你想知道 `thread::index_1d()`、`atom_add`、`mma` 等具体如何在 `terminator/mod.rs` 里被分派成方言 op 时，去读这一篇。
- **u6-l6（独立后端 cuda-oxide-codegen 深潜）**：本讲把 `compile_translated_module` 当黑盒；想了解这条「无 rustc、无 nightly」的实验性后端的公共 API（`CodegenModule`/`Compiler`/`CompileOptions`/`Target`/`Toolchain`）、`llc`/`opt` 工具发现，以及它如何被无 rustc 前端复用，去读这一篇。
- **建议继续阅读的源码**：想加深对 alloca 模型的体感，推荐读 `translator/values.rs` 的 `ValueMap` 与 `maybe_ptr_coerce`；想理解可达性剪枝，读 `translator/body.rs` 的 `non_unwind_successors` + `compute_reachable_blocks`；想确认后段边界，逐行读 `mir-importer/src/pipeline.rs` 的 `run_pipeline` 与 `cuda-oxide-codegen/src/pipeline.rs` 的 `compile_translated_module`。
