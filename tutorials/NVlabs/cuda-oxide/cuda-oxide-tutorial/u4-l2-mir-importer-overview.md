# MIR 导入器鸟瞰：rustc MIR → Pliron IR

## 1. 本讲目标

上一讲（u4-l1）我们停在后端入口 `codegen_crate` 的 host/device 分流，并追到 `device_codegen::generate_device_code` 把控制权交给 `mir_importer::run_pipeline` 这一步。本讲就跨过这道门，鸟瞰 `mir-importer` crate 内部发生的事。

读完本讲，你应该能够：

1. 说出 `mir-importer` 在 cuda-oxide 流水线里的定位——把 rustc 的 **MIR** 翻译成基于 **Pliron** 的 `dialect-mir` IR，再驱动后续的 `mem2reg`、lowering、导出 PTX。
2. 画出 `run_pipeline` 的阶段序列，并指出 `translator` 只负责其中「MIR → dialect-mir」这一段。
3. 解释 `translator` 的**分层结构**（body / block / statement / rvalue / terminator / values / types）以及贯穿全层的 **alloca + load/store 模型**。
4. 说明一个 rustc MIR 的「基本块」是如何被映射成一串 Pliron IR 块、unwind cleanup 块又是如何被处理的。

本讲是「鸟瞰」（overview）性质：只讲清楚 `mir-importer` 的骨架与数据流，不深入单条 MIR 语句如何翻译成具体 op。对 terminator/intrinsic 的逐类翻译、对 lowering 细节的深潜，分别留给 u6-l2（mir-importer 深潜）与 u6-l3（mir-lower 深潜）。

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

### 2.3 为什么不直接 MIR → LLVM IR

你可能会问：既然最终要得到 LLVM IR 再生成 PTX，为什么不直接把 MIR 翻译成 LLVM IR？原因在于**分层解耦**：

- MIR 仍然带着 Rust 的高级语义（枚举判别式、变长切片、地址空间未定的引用），一步到位降到 LLVM 会非常臃肿、难维护。
- `dialect-mir` 作为中间层，既保留 Rust 语义、又贴近 IR 框架，方便插入 **`mem2reg`、循环展开**等 pass（见 pipeline.rs 里的步骤）。
- 真正降到 LLVM 方言的工作由 `mir-lower` crate 完成（u4-l4 / u6-l3），与 `mir-importer` 解耦。

理解了这一点，你就理解了 `mir-importer` 的边界：**它只负责 MIR → dialect-mir，及其后的几个 IR 变换；lowering 之后的事交给别的 crate。**

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [crates/mir-importer/src/lib.rs](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/lib.rs) | crate 顶层：架构图、模块表、对外的 `pub use` 导出 |
| [crates/mir-importer/src/pipeline.rs](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/pipeline.rs) | 编译流水线 `run_pipeline`：注册方言 → 翻译 → 校验 → mem2reg → 循环展开 → lowering → 导出 |
| [crates/mir-importer/src/translator/mod.rs](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/translator/mod.rs) | translator 模块入口：`register_dialects`、`translate_function` |
| [crates/mir-importer/src/translator/body.rs](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/translator/body.rs) | 函数体翻译：签名、建块、entry allocas、可达块遍历 |
| [crates/mir-importer/src/translator/block.rs](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/translator/block.rs) | 单个基本块内容翻译（先语句、后终止符） |
| [crates/mir-importer/src/translator/values.rs](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/translator/values.rs) | MIR local → alloca 槽位的映射与 slot 操作 |
| [crates/mir-importer/src/translator/statement.rs](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/translator/statement.rs) | 语句翻译（赋值、投影、存储标记） |
| [crates/mir-importer/src/translator/rvalue.rs](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/translator/rvalue.rs) | 右值（表达式）翻译为 dialect-mir op |
| [crates/mir-importer/src/translator/terminator/mod.rs](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/translator/terminator/mod.rs) | 终止符翻译：控制流 + GPU intrinsic 分派 |

辅助记忆：`run_pipeline`（流水线编排）调 `translator::body::translate_body`（每函数一次），后者再调 `block` → `statement`/`rvalue`/`terminator`。`values` 与 `types` 是贯穿全层的两个工具层。

## 4. 核心概念与源码讲解

### 4.1 Pliron IR 载体：rustc MIR 之外的第二种 IR

#### 4.1.1 概念说明

cuda-oxide 在 device 路径上其实同时处理**两种** IR：

1. **rustc 的 MIR**：输入，由 rustc 内部数据结构（`rustc_middle::mir`）承载。但 `mir-importer` **不直接**啃 rustc 内部结构，而是经 `stable_mir`（`rustc_public` / `rustc_public_bridge`）拿到一份「稳定的、跨版本相对稳定」的 MIR 视图。
2. **Pliron 的 `dialect-mir`**：输出，由 `pliron::context::Context` 承载。translator 的工作就是把前者逐节点搬进后者。

为什么需要这一层？因为后续的 `mem2reg`、`lowering` 都建立在 Pliron 的 pass 框架之上——它们只认 Pliron 的 `Operation`/`BasicBlock`，不认 rustc 的 MIR。所以 `dialect-mir` 是「给下游 pass 看的 Rust 语义 IR」。

crate 顶部的文档注释把这件事画成一张图，读它最快建立直觉。

#### 4.1.2 核心流程

```text
rustc 内部 MIR (rustc_middle)
        │  rustc_internal::stable()  ← 由 u4-l1 的 device_codegen 完成
        ▼
stable MIR (rustc_public)         ← mir-importer 实际消费的形态
        │  translator::translate_function
        ▼
Pliron Context 持有 dialect-mir   ← 下游 pass 的输入
        │  verify → mem2reg → unroll
        ▼
mir-lower: dialect-mir → LLVM dialect → .ll → PTX
```

注意图中 `mir-importer` 的产出（Pliron `Context`）**并不落盘成独立的 dialect-mir 文件**——它存在内存里，被 `run_pipeline` 直接喂给后续 pass。能「看到」它的途径只有打开 dump（见 4.4 的实践）。

#### 4.1.3 源码精读

crate 顶层文档注释给出了整体架构与模块分工，是本讲最好的总览入口：

[crates/mir-importer/src/lib.rs:18-46](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/lib.rs#L18-L46) —— 注意 ASCII 图把 `translator` 与 `pipeline` 分开：translator 负责 `MIR → dialect-mir (alloca)`，pipeline 负责 `mem2reg → unroll → LLVM dialect → LLVM IR → PTX`。这是 cuda-oxide 全局最重要的职责切分。

紧接着的文档说明了贯穿 translator 的核心建模：

[crates/mir-importer/src/lib.rs:63-70](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/lib.rs#L63-L70) —— **alloca + load/store 模型**：每个非 ZST 的 MIR local 都物化成一个 `mir.alloca`，写为 `mir.store`、读为 `mir.load`，跨块数据流走槽位而非块参数；`mem2reg` 之后再把标量槽位提升回 SSA。这个模型会在 4.3 重点展开。

crate 对外导出的「门面」类型（下游 `device_codegen` 只用这几个）：

[crates/mir-importer/src/lib.rs:86-90](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/lib.rs#L86-L90) —— `run_pipeline`、`PipelineConfig`、`CollectedFunction`、`CompilationResult` 等。`CollectedFunction` 是输入（已收集、已单态化的设备函数实例），`CompilationResult` 是输出（产物路径、目标架构、FMA 策略）。

#### 4.1.4 代码实践

这是一个纯阅读型实践，目标是把「两种 IR」的边界看清楚。

1. **实践目标**：确认 `mir-importer` 消费的是 stable MIR、产出的是 Pliron `Context`，并定位两者之间的桥接点。
2. **操作步骤**：
   - 打开 `crates/mir-importer/src/translator/mod.rs`，看顶部的 `extern crate rustc_public;` 与 `extern crate rustc_public_bridge;`（[mod.rs:72-79](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/lib.rs#L72-L79) 区域），确认它依赖的是 stable MIR 而非 rustc 内部类型。
   - 在 `translate_function` 签名里找到参数 `body: &mir::Body`（来自 `rustc_public::mir`）与返回值 `Ptr<Operation>`（Pliron 操作指针），见 [translator/mod.rs:90-96](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/translator/mod.rs#L90-L96)。
3. **需要观察的现象**：函数签名「左侧是 stable MIR、右侧是 Pliron」正是「翻译器」一词的字面体现。
4. **预期结果**：你能用一句话说出「`translate_function` 把一个 `rustc_public::mir::Body` 变成一个 Pliron `Operation`」。
5. 「待本地验证」：本实践无需运行任何命令。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `mir-importer` 通过 `rustc_public`（stable MIR）而不是直接用 `rustc_middle::mir`？

**参考答案**：stable MIR 是 rustc 提供的、跨 nightly 版本相对稳定的接口，能把 translator 与 rustc 内部数据结构的频繁变动隔离开；同时 `#![feature(rustc_private)]` 已经让 crate 能访问内部类型，但「能访问」不等于「应该依赖」。stable MIR 是为这类外部工具专门设计的契约层。

**练习 2**：`dialect-mir` 的 IR 在哪一步会落盘成一个文件？

**参考答案**：默认不落盘。`dialect-mir` 只存在于内存里的 Pliron `Context` 中，被 `run_pipeline` 直接喂给后续 pass。落盘的产物是更下游的 `*.ll`（LLVM IR）与 `*.ptx`。要看 dialect-mir 文本需要打开 dump（见 4.4）。

---

### 4.2 mir-importer pipeline 阶段

#### 4.2.1 概念说明

`run_pipeline` 是 `mir-importer` 对外的「一键编译」入口。它把「一批已收集的设备函数（`CollectedFunction` 列表）」一路推到「PTX 或 NVVM IR 文件」。理解它的**阶段序列**，就理解了 `mir-importer` 在整条流水线里做了哪些事——以及哪些事其实不是它做的（lowering、llc 调用分别委托给了 `mir-lower` 与外部 `llc`）。

注意一个易混点：尽管函数叫 `run_pipeline`、文件叫 `pipeline.rs`，但**「translator 把 MIR 翻成 dialect-mir」只是其中第 2 步**。pipeline 还要负责注册方言、校验、mem2reg、循环展开、调用 lowering、导出文本、跑 llc。把这些全放在一个函数里，是为了让 `device_codegen` 只需一次调用就能拿到 PTX。

#### 4.2.2 核心流程

`run_pipeline` 的官方步骤序列（来自其文档注释）：

```text
1. 注册 dialect-mir / dialect-nvvm / LLVM 方言
2. 对每个函数：把 MIR body 翻译成 dialect-mir
3. 校验 dialect-mir 模块
4. （非 full-debug 时）跑 mem2reg，把 alloca+load/store 提升回 SSA
4.6 在 SSA 形态上跑「带注解的循环展开」（#[unroll]）
4.9 插入 device-extern 声明（供调用 lowering 时保留地址空间）
5. 调 mir-lower：dialect-mir → LLVM 方言
6. 校验 LLVM 方言模块
7. 导出为文本 LLVM IR（.ll）
8. 跑 llc 生成 PTX；或检测到 libdevice 时改走 NVVM IR（跳过 llc）
```

几条关键判断值得记住：

- **full-debug 跳过 mem2reg/unroll**：当 `debug_kind.variables_enabled()`（cuda-gdb 可读变量的 `-G` 风格构建）时，源变量必须留在稳定的栈槽里，所以跳过提升与展开。这一点 u7-l2 会用到。
- **libdevice 自动改道**：如果 lowering 后出现了 `__nv_*`（如 `__nv_sinf`），`llc` 无法解析，必须走 libNVVM + nvJitLink，于是 pipeline 改产出 NVVM IR（`.ll`）并跳过 llc。这会在 u4-l5 详讲。
- **目标架构自动探测**：pipeline 会在导出的 LLVM 文本里扫描 PTX 指令特征（WGMMA、TMA、tcgen05、bf16x2……），据此选 `sm_XX`，可用 `CUDA_OXIDE_TARGET` 覆盖。

#### 4.2.3 源码精读

pipeline.rs 顶部的文档给出了与上面一致的步骤序列与一张「GPU 特征 → 目标架构」表：

[crates/mir-importer/src/pipeline.rs:6-46](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/pipeline.rs#L6-L46) —— 这张表是后续 u5（高级设备能力）各讲的「架构门禁」依据，例如 WGMMA→`sm_90a`、tcgen05→`sm_100a`、bf16 mma→`sm_80`。

`run_pipeline` 的签名与文档：

[crates/mir-importer/src/pipeline.rs:248-281](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/pipeline.rs#L248-L281) —— 三个入参：`functions`（设备函数集）、`device_externs`（外部设备函数声明，用于 LTOIR FFI）、`config`（`PipelineConfig`）。

第 2 步「逐函数翻译」的循环：

[crates/mir-importer/src/pipeline.rs:300-351](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/pipeline.rs#L300-L351) —— 对每个 `CollectedFunction` 取出 `instance.body()`，调 `translator::body::translate_body`，dump（若开启）、`verify`、再 `append_to_module`。注意 dump 故意放在 verify **之前**，这样即使校验失败，用户也能在 `--show-mir-dialect` 里看到出问题的 IR。

第 4 / 4.6 步的 mem2reg + 循环展开：

[crates/mir-importer/src/pipeline.rs:367-425](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/pipeline.rs#L367-L425) —— 关键分支是 `if config.debug_kind.variables_enabled()` 决定**跳过** mem2reg；否则跑 pliron 的 `mem2reg`，再跑 `mir_transforms::unroll::unroll_annotated_loops`。注释点明了「full-debug 把变量留在内存里以便 cuda-gdb 读取」的动机。

第 5 步调用 lowering：

[crates/mir-importer/src/pipeline.rs:443-447](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/pipeline.rs#L443-L447) —— 一行 `lower_to_llvm(...)` 把 `dialect-mir` 交给 `mir-lower`，并把 `allow_fma_contraction` 透传下去（这条线在 u6-l1/u6-l3 与 FMA 收缩策略相关）。

第 7、8 步的导出与 PTX/NVVM 分叉：

[crates/mir-importer/src/pipeline.rs:537-624](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/pipeline.rs#L537-L624) —— `emit_nvvm_ir` 为真时跳过 llc、产出 `.ll` 作为 NVVM IR 制品；否则调 `generate_ptx` 跑 llc 得到 `.ptx`。`CompilationResult` 里同时带上了 `allow_fma_contraction`，供制品边车记录（接 u3-l2）。

#### 4.2.4 代码实践

源码阅读型实践：对照 `run_pipeline` 的步骤编号，确认每一步对应的源码段落。

1. **实践目标**：能从 `pipeline.rs` 的 `run_pipeline` 函数体里逐一指出第 1～8 步的源码位置。
2. **操作步骤**：
   - 打开 [pipeline.rs:277](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/pipeline.rs#L277) 起的函数体。
   - 找到第 1 步 `register_dialects`（约 [L287](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/pipeline.rs#L287)）、第 2 步翻译循环（[L301](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/pipeline.rs#L301)）、第 4 步 mem2reg（[L379](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/pipeline.rs#L379)）、第 5 步 `lower_to_llvm`（[L447](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/pipeline.rs#L447)）、第 8 步 PTX/NVVM 分叉（[L560](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/pipeline.rs#L560)）。
3. **需要观察的现象**：步骤之间用 `verify_operation` 反复校验，任何一步失败都会带上下文里的 op 文本报错。
4. **预期结果**：你能口头复述这 8 步，并指出「translator 只占第 2 步」。
5. 「待本地验证」：本实践无需运行命令。

#### 4.2.5 小练习与答案

**练习 1**：`run_pipeline` 里，mem2reg 跑在 lowering 之前还是之后？为什么必须在那个位置？

**参考答案**：之前。mem2reg 把 translator 产生的 `alloca + load/store` 槽位提升回 SSA，正是为了给 lowering 一个「干净的 SSA 输入」；若放在 lowering 之后，LLVM 方言里已经没有 `mir.alloca` 这种 op 可供提升了。

**练习 2**：为什么 full-debug 构建要跳过 mem2reg？

**参考答案**：mem2reg 会把每个源变量从「稳定的栈槽」收窄到「寄存器的活跃区间」，cuda-gdb 在优化构建里就会显示 `<optimized out>`。full-debug 希望变量在整个作用域内都可从固定内存地址读出，所以必须保留栈槽，自然就跳过提升（也跳过循环展开）。

---

### 4.3 translator 分层与 alloca + load/store 模型

#### 4.3.1 概念说明

`run_pipeline` 的第 2 步背后，是 `translator` 模块。它采用**与 MIR 结构一一对应的分层**：

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

这样做的好处是：translator 不需要在翻译每个块时都维护一套 SSA 构造算法（SSA 构造是出了名的麻烦），只需要「写槽 / 读槽」两条简单规则；把「槽位 → SSA」这件难事交给成熟的 `mem2reg` pass 在第 4 步统一完成。

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

[crates/mir-importer/src/translator/mod.rs:11-44](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/translator/mod.rs#L11-L44) —— 这张表对应上面的分层表，并把 alloca 模型再强调了一遍。

`register_dialects`：translator 开始前必须把会用到的方言都注册进 `Context`：

[crates/mir-importer/src/translator/mod.rs:67-80](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/translator/mod.rs#L67-L80) —— 注册 `dialect-mir`（建模 Rust 语义）与 `dialect-nvvm`（GPU intrinsic，如 thread/block/warp）。注释说明 builtin 方言（`ModuleOp` 等）由 pliron 0.14 自动注册。

`translate_function`：对外「单函数翻译」入口（主要供测试/工具用，真实 pipeline 走的是 `body::translate_body`）：

[crates/mir-importer/src/translator/mod.rs:90-150](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/translator/mod.rs#L90-L150) —— 它建一个 `builtin.module`，把 `translate_body` 产出的 `mir.func` 塞进去。注意它固定 `is_inline_always = false`（真实 pipeline 会从 rustc 的 `CodegenFnAttrs` 取这个标志并透传，接 u6-l1）。

alloca 模型的「槽位账本」`ValueMap`：

[crates/mir-importer/src/translator/values.rs:52-87](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/values.rs#L52-L87) —— 一个 `Vec<Option<Value>>`，按下标对应 MIR local 编号；ZST local 留 `None`。`get_slot`/`set_slot` 是它的读写接口。

三个 slot 操作的发 emitter：

[crates/mir-importer/src/translator/values.rs:96-172](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/translator/values.rs#L96-L172) —— `emit_alloca` 发 `mir.alloca`，`load_local` 发 `mir.load`，`store_local` 发 `mir.store`。`store_local` 还会在指针地址空间不一致时自动插一个 `mir.cast <PtrToPtr>`（见 `maybe_ptr_coerce`），这是 GPU 地址空间（shared/global/constant）正确性的关键，与 u2-l3 共享内存、u5 系列直接相关。

`emit_entry_allocas` 的文档把 alloca 模型讲得最完整：

[crates/mir-importer/src/translator/body.rs:514-541](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/translator/body.rs#L514-L541) —— 函数入口块顶部，对每个 non-ZST local 发一个 alloca、记录到 `value_map`，再把函数参数 `store` 进各自的槽；返回最后一条 op，供后续语句「接在后面」追加（否则会被 `insert_at_front` 推到 alloca 链前面）。

各分层模块的「能力表」也很值得扫一眼，能快速知道哪些 Rust 写法已被支持：

- 语句表：[statement.rs:10-35](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/translator/statement.rs#L10-L35)（`Assign`/`StorageLive`/`SetDiscriminant`/`Nop`，以及 1～2 级投影的专用路径）。
- 右值表：[rvalue.rs:10-31](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/translator/rvalue.rs#L10-L31)（`BinaryOp`→`mir.add/sub/mul/div`、`Cast`→`mir.cast`、`Aggregate`→`mir.construct_*` 等）。

#### 4.3.4 代码实践

源码阅读 + 对照式实践：用一个真实内核，把它的 Rust 写法对应到 translator 各层的 op。

1. **实践目标**：对一个最简内核（vecadd），手工把它的每条语句「投射」到 translator 的分层表上。
2. **操作步骤**：
   - 打开 [examples/vecadd/src/main.rs:39-46](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs#L39-L46) 的 `vecadd` 内核。
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

[crates/mir-importer/src/translator/body.rs:191-210](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/translator/body.rs#L191-L210) —— `compute_reachable_blocks` 从入口块出发，调 `non_unwind_successors` 收集后继。`non_unwind_successors`（[body.rs:172-189](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/translator/body.rs#L172-L189)）的关键判断是：`Drop`/`Call`/`Assert` 等终止符的 unwind 目标被剔除，只保留「正常」目标。注释点明 CUDA 工具链不接栈展开。

PHASE 1 + PHASE 1.5 + PHASE 2 的主循环：

[crates/mir-importer/src/translator/body.rs:873-965](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/translator/body.rs#L873-L965) —— 注意三处：建块时只有 `idx == 0` 用 `arg_types`（[L882-887](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/translator/body.rs#L882-L887)）；entry allocas 在 [L909](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/translator/body.rs#L909) 调 `emit_entry_allocas`；可达块逐块翻译在 [L931-947](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/translator/body.rs#L931-L947)；最后给未处理块补 `mir.unreachable` 在 [L953-965](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/translator/body.rs#L953-L965)。

单块内容翻译（先语句后终止符）：

[crates/mir-importer/src/translator/block.rs:30-77](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/translator/block.rs#L30-L77) —— `translate_block` 用 `prev_op` 这条「游标」把语句一条条 `insert_after` 串起来，保证 IR 文本顺序与 MIR 一致；终止符最后翻译。注释强调「块本身由 `body` 创建，本模块只负责填充内容」。

`translate_body` 的总体文档（含「单元返回变成 void 签名」等细节）：

[crates/mir-importer/src/translator/body.rs:608-640](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/translator/body.rs#L608-L640) —— 还能看到 `#[kernel]` 时给 `mir.func` 加 `gpu_kernel` 属性、扫描 `#[cluster(...)]`/`#[launch_bounds(...)]` 标记注入 `cluster_dim_*`/`maxntid` 属性（接 u2-l4、u5-l4、u7-l2）。

#### 4.4.4 代码实践（本讲主实践）

这是本讲唯一需要实际运行（或阅读运行逻辑）的实践：用 `cargo oxide pipeline` 把 vecadd 的 dialect-mir 中间产物「显形」，对照 `pipeline.rs` 说明它经过了哪几个阶段。

1. **实践目标**：亲眼看到 translator 产出的 `dialect-mir`（mem2reg 前后两份）、lowering 后的 LLVM IR、最终 PTX，并把它们对回 `run_pipeline` 的步骤编号。
2. **操作步骤**：
   - 进入仓库根目录，运行：
     ```bash
     cargo oxide pipeline vecadd
     ```
   - `pipeline` 子命令（`cargo-oxide` 的 `codegen_show_pipeline`）会自动打开若干诊断开关，其中关键是：
     - `CUDA_OXIDE_DUMP_MIR=1` → 经后端映射成 `PipelineConfig::show_mir_dialect=true`，让 `run_pipeline` 把 dialect-mir 文本打印到 **stderr**（见 [commands.rs:1675](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cargo-oxide/src/commands.rs#L1675) 与 [device_codegen.rs:563-687](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/src/device_codegen.rs#L563-L687)）。
     - `CUDA_OXIDE_DUMP_LLVM=1` → 打印 LLVM 方言模块。
   - 构建结束后，`pipeline` 会把落盘产物打印到 stdout：`vecadd.ll`（LLVM IR）与 `vecadd.ptx`（PTX），位于示例目录下（见 [commands.rs:3022-3047](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cargo-oxide/src/commands.rs#L3022-L3047) 的 `show_generated_artifacts`）。
3. **需要观察的现象**：在 stderr 里应能看到形如下面的阶段标题（由 [pipeline.rs:355-365](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/pipeline.rs#L355-L365) 与 [L401-404](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/pipeline.rs#L401-L404) 打印）：
   - `=== dialect-mir module (pre-verify) ===`（第 2 步 translator 产物）
   - `=== Running mem2reg ===` + `=== dialect-mir module (after mem2reg) ===`（第 4 步）
   - `=== Lowering dialect-mir → LLVM dialect ===`（第 5 步）
   - `=== Generating PTX ===`（第 8 步）
4. **预期结果**：你能把 stderr 里每个阶段标题对应到 `run_pipeline` 文档里的一个步骤号，并在 `vecadd.ll` 里看到 `mir.func vecadd` 已经被 lower 成 `define void @vecadd(...)`，在 `vecadd.ptx` 里看到 `.entry vecadd`。
5. 「待本地验证」：上面所有「应能看到」的具体输出文本均依赖本机具备 nightly 工具链与 `llc-21+`（见 u1-l3）。若本机缺 GPU，`pipeline` 仍可工作（它只编译不运行内核）；若工具链不全，则按 u1-l3 的 `cargo oxide doctor` 先补齐。若无法运行，请改为阅读 `run_pipeline` 源码逐段确认步骤。

#### 4.4.5 小练习与答案

**练习 1**：假如一个内核用了 `Result` 且写了 `?`，MIR 里会带 unwind cleanup 块。translator 怎么处理它们？

**参考答案**：`non_unwind_successors` 在收集后继时把 `Call`/`Drop` 等终止符的 unwind 目标剔除，`compute_reachable_blocks` 的 BFS 因此不会遍历到这些 cleanup 块。它们落在可达集之外，被 [body.rs:953-965](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/mir-importer/src/translator/body.rs#L953-L965) 补上一个 `mir.unreachable` 终止符以满足 Pliron 校验，后续 pass 会把它们当死代码删掉。

**练习 2**：为什么除了入口块，所有 Pliron 块都是「零参数」的？

**参考答案**：因为 alloca 模型让跨块数据流全部走 local 的栈槽（`mir.store`/`mir.load`），不需要在块之间显式传值。只有入口块需要带参数——那是函数形参的落脚点，`emit_entry_allocas` 会立刻把它们 store 进各自槽位。这同时让所有分支终止符（`mir.goto`/`mir.cond_br`/`mir.switch`）都是零操作数的，结构最简。

---

## 5. 综合实践

把本讲四个模块串起来，做一个「静态追踪 + 动态对照」的小任务：给 vecadd 的内核**加一个条件分支**，然后观察它在四个阶段里的形态变化。

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
2. 运行 `cargo oxide pipeline vecadd`，在 stderr 的 `dialect-mir module` dump 里找到你的 `mir.func`。
3. 验证你对本讲的掌握，回答：
   - **4.1（Pliron 载体）**：这个带分支的内核，`if` 在 MIR 里通常变成 `SwitchInt` 终止符。它会被哪个分层模块翻译？预期在 dialect-mir 里看到哪种 op？（提示：`terminator/mod.rs`，`mir.cond_br`。）
   - **4.2（pipeline）**：分支会让块变多，但不会改变阶段序列。确认你仍能看到 `pre-verify → mem2reg → after mem2reg → lowering → PTX` 这条标题链。
   - **4.3（alloca 模型）**：在 `pre-verify` 的 dump 里，应能看到 `mir.alloca`、`mir.store`、`mir.load`；在 `after mem2reg` 里它们应被折叠成 SSA 值（不再有针对标量 local 的 load/store 往返）。对照 terminator/mod.rs 的经典例子说明你看到了同样的「塌缩」。
   - **4.4（块映射）**：分支应在 dialect-mir 里产生至少两个目标块；确认 `mir.cond_br` 是零操作数的（数据靠槽位流动，不靠块参数）。
4. 最后核对正确性：若本机有 GPU，`cargo oxide run vecadd` 应输出「偶数下标相加、奇数下标相减」的结果。

> 这个任务同时触动了本讲的全部四个最小模块，并自然地为下一讲（u4-l3 方言层）与 u6-l2（terminator 深潜）埋下伏笔——你会开始好奇 `SwitchInt` 到底分派到哪个 dialect op。

## 6. 本讲小结

- `mir-importer` 的职责是**把 rustc 的 stable MIR 翻译成 Pliron 的 `dialect-mir`**，并编排其后的 mem2reg、循环展开、lowering、PTX 导出；但 lowering 本身委托给 `mir-lower`，PTX 生成委托给外部 `llc`。
- `run_pipeline` 是一条清晰的 8 步流水线：注册方言 → 逐函数翻译 → 校验 → mem2reg → 循环展开 → lowering → 校验 → 导出 `.ll` → 跑 `llc` 出 PTX（或检测到 libdevice 改走 NVVM IR）。
- translator 采用**与 MIR 一一对应的分层**（body / block / statement / rvalue / terminator / values / types），并贯穿 **alloca + load/store 模型**：每个 non-ZST local 一个栈槽，写即 store、读即 load，把 SSA 构造这件难事推迟给 mem2reg。
- MIR 块图的映射有三个要点：BFS 只翻译可达块（unwind cleanup 被当死代码）、仅入口块带函数形参、不可达块补 `mir.unreachable` 兜底校验。
- `dialect-mir` 默认只在内存里；要看它需用 `cargo oxide pipeline`（打开 `CUDA_OXIDE_DUMP_MIR`），落盘的可见产物是下游的 `.ll` 与 `.ptx`。
- 本讲全程是「鸟瞰」：单条 MIR 语句如何落到具体 op、terminator 如何分派 GPU intrinsic、lowering 内部细节，分别留给 u6-l2 与 u6-l3。

## 7. 下一步学习建议

- **u4-l3（MLIR 方言层）**：本讲反复提到 `dialect-mir` 与 `dialect-nvvm`，下一讲正式拆解这两个方言各自提供哪些 op、它们如何承接「Rust 语义 → GPU 指令」的过渡。
- **u4-l4（MIR Lowering 鸟瞰）**：本讲的第 5 步 `lower_to_llvm` 把控制权交给了 `mir-lower`，下一讲鸟览那个 crate 的 ops/intrinsics 两类转换入口。
- **u6-l2（mir-importer 深潜：terminator/intrinsics 翻译机）**：当你想知道 `thread::index_1d()`、`atom_add`、`mma` 等具体如何在 `terminator/mod.rs` 里被分派成方言 op 时，去读这一篇。
- **建议继续阅读的源码**：想加深对 alloca 模型的体感，推荐读 `translator/values.rs` 的 `ValueMap` 与 `maybe_ptr_coerce`；想理解可达性剪枝，读 `translator/body.rs` 的 `non_unwind_successors` + `compute_reachable_blocks`。
