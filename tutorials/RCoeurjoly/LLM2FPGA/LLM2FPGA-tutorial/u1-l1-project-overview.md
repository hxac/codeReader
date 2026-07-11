# LLM2FPGA 是什么：项目目标与所选技术路线

## 1. 本讲目标

本讲是整本学习手册的第一篇，面向**完全没接触过本项目的读者**。读完本讲后，你应当能够：

- 用一句话说清 LLM2FPGA 要解决什么问题，以及它最关键的约束是「**全开源 EDA 工具链**」。
- 画出 `PyTorch → Torch-MLIR → CIRCT → Verilog → Yosys` 这条五阶段降级路线，并能解释每一阶段为什么存在。
- 解释「为什么不直接用 Vitis HLS」，并指出所选路线的三个主要风险。
- 看懂项目的当前状态：TinyStories-1M 已经能被降级并综合，但 LUT 用量约为目标 FPGA 的 **141 倍**，因此下一步是「资源最小化（Task 6）」而不是「上板（Task 4）」。

本讲不涉及任何代码细节，只读 README、`1c-selected_route`、`project-plan_v2` 与 `3e` 报告，帮你建立全局地图。后面的讲义会逐层钻进每一段源码。

## 2. 前置知识

在开始之前，先用最朴素的语言建立几个概念。后面的讲义会反复用到它们。

- **LLM（大语言模型）**：像 GPT、Llama、TinyStories 这类模型。它们通常用 PyTorch 框架写成、训练好后供「推理（inference）」使用。本项目只做**推理**，不做训练。
- **FPGA（现场可编程门阵列）**：一块可以被重新「编程」成任意数字电路的芯片。你写硬件描述语言（HDL），工具把它「烧」进芯片，芯片就变成了你描述的电路。它的优势是低延迟、可定制、不依赖云厂商。
- **EDA（电子设计自动化）工具链**：把 HDL 变成可以在 FPGA 上运行的比特流（bitstream）所需的一整套软件。典型步骤包括：综合（synthesis）、布局布线（place and route，简称 PnR）、生成比特流。
- **开源 vs 闭源 EDA**：Xilinx/AMD 官方的 Vivado、Vitis HLS 是**闭源（专有）**的；而 Yosys、nextpnr、CIRCT、yosys-slang、openXC7 等是**开源**的。LLM2FPGA 的核心约束是：**全程只能用开源工具**。
- **HLS（高层次综合）**：把 C/C++ 代码直接编译成硬件的工具。Vitis HLS 是最常用的，但它闭源。
- **MLIR / CIRCT**：MLIR 是一个「可扩展的编译器中间表示」框架；CIRCT 是基于 MLIR、专门面向硬件设计的子项目。你可以把它们粗略理解为「一条模块化的编译流水线，中间产物叫 dialect（方言）」。

如果你对 FPGA 或编译器完全陌生，也不用担心——本讲只要求你理解「方向」，具体机制会在后续讲义里拆开讲。

## 3. 本讲源码地图

本讲引用四个文件，全部是**文档类**文件（不是可执行代码），它们定义了项目的目标、路线、任务与结论：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/README.md) | 项目门面：用一段话说清目标、所选路线、当前状态，并给出「一键复现」命令。 |
| [deliverables/1c-selected_route.md](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/deliverables/1c-selected_route.md) | Task 1 的结论：为什么选 `PyTorch→Torch-MLIR→CIRCT→Verilog→Yosys`，以及它的风险。 |
| [docs/project-plan_v2.md](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/docs/project-plan_v2.md) | 完整项目计划：把工作拆成 Task 1～6，每个 Task 有目标、风险、子任务和交付物。 |
| [deliverables/3e-tiny-stories-1m-resource-report.md](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/deliverables/3e-tiny-stories-1m-resource-report.md) | Task 3 的最终报告：给出「141 倍超配」的瓶颈结论与下一步方向。 |

> 提示：仓库里 `.org` 文件才是权威源（canonical source），`.md` 是用 pandoc 自动生成的（详见第 7 单元讲义）。两者内容一致，读哪个都可以。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**①项目动机与开源约束**、**②PyTorch 到 Yosys 的总路线**、**③当前阶段结论与风险**。

### 4.1 项目动机与开源约束

#### 4.1.1 概念说明

LLM 推理已经在专有硬件（GPU、TPU）和专有软件栈上被反复演示过。但本项目作者发现一个空白：**没有广泛认可的项目能用「全开源 EDA 流程」把开源 LLM 跑到 FPGA 上**。

这就是 LLM2FPGA 的动机。它要回答的问题是：

> 能不能拿一个开源的小 LLM，用一套完全开源的工具，把它编译成 FPGA 上能跑的硬件？

这里有两个关键词必须同时满足：

1. **开源 LLM**：模型本身是开源的（本项目从 TinyStories-1M 起步，这是能找到的最小 LLM）。
2. **全开源 EDA**：从模型到比特流的**每一步工具**都必须开源，不能依赖 Vivado / Vitis HLS 这类闭源软件。

为什么要强调「全开源」？因为本项目由 NLnet / NGI0 Commons Fund 资助，目标是提供一种**透明、灵活、保护隐私**的本地 LLM 推理方案——既不被云厂商绑定，也不被闭源 EDA 锁死。

#### 4.1.2 核心流程

「全开源约束」直接决定了技术选型的取舍。可以用下面的「排除法」来理解：

```text
目标：开源 LLM ──?──> FPGA 比特流（全程开源工具）

候选 1：HLS 路线（C/C++ → 硬件）
  └─ 主流工具是 Vitis HLS ──> 闭源 ✗ 排除
  └─ 开源替代 Panda Bambu ──> 与现有 Vitis 代码不兼容，需大量手工改造 ✗ 排除

候选 2：编译器链路线（PyTorch → MLIR → 硬件）
  └─ Torch-MLIR + CIRCT + Yosys ──> 全开源 ✓ 选中
```

结论很清楚：**因为不能用 Vitis HLS，又没有好用的开源 HLS，所以只能走「基于 MLIR 的编译器链」这条路。** 这正是 4.2 要讲的总路线。

#### 4.1.3 源码精读

项目目标在 README 开头一段话里写得很明确——注意它特意强调「fully open-source toolchain」和「fully open-source EDA flow」：

[README.md:L1-L13](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/README.md#L1-L13) —— 说明项目要让开源 LLM 在 FPGA 上做本地推理，并且明确点出「我们不知道有任何项目用全开源 EDA 流程做到过这件事」，这正是本项目要填补的空白。

「为什么不用 Vitis HLS」的论证在 `1c-selected_route` 的调研结论里。调研覆盖了 14 篇相关论文/仓库，关键发现是：**所有基于 HLS 的方案都依赖专有的 Vitis HLS**，而唯一的开源 HLS（Panda Bambu）兼容性很差：

[deliverables/1c-selected_route.md:L7-L40](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/deliverables/1c-selected_route.md#L7-L40) —— 列出调研发现：HLS 候选全靠 Vitis HLS（闭源），开源 HLS 替代兼容性不足；同时这里也透露了目标硬件的选择（Xilinx XC7K480T，约 48 万 LUT，是目前开源工具支持的最大 FPGA）。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，不需要运行任何命令。

1. **实践目标**：理解「全开源约束」如何否决了 HLS 路线。
2. **操作步骤**：
   - 打开 [deliverables/1c-selected_route.md:L7-L40](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/deliverables/1c-selected_route.md#L7-L40)。
   - 找到提到「Vitis HLS」和「Panda Bambu」的两条 bullet。
   - 再打开 [README.md:L1-L13](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/README.md#L1-L13)，确认 README 同样把「fully open-source」写进了项目定义。
3. **需要观察的现象**：注意调研结论里用词是「**rely on the proprietary toolchain Vitis HLS**」——这不是「某几个」方案，而是**所有** HLS 候选都依赖它。
4. **预期结果**：你能用自己的话解释——开源 HLS（Panda Bambu）虽然存在，但和现有 Vitis HLS 代码（尤其 pragma 和头文件）兼容性有限，需要大量手工改造，所以不实用。
5. 本实践结论为「待本地验证」的部分：如果你感兴趣，可以试着在本机装一次 Panda Bambu 并编译一段带 `#pragma HLS` 的代码，观察兼容性问题（非本讲必需）。

#### 4.1.5 小练习与答案

**练习 1**：如果有一天 Xilinx 把 Vitis HLS 完全开源了，LLM2FPGA 的「全开源」约束是否就自动满足了？

> **参考答案**：不一定。「全开源」要求的是**整条 EDA 流程**都开源，而不只是 HLS 这一个环节。即便 Vitis HLS 开源，配套的综合/PnR/比特流工具、以及它生成的 IP 是否开源，都还要逐一确认。此外，本项目选定的 MLIR 编译器链路线本身也有独立价值（可扩展、可增量开发），未必会因为 HLS 开源而切换。

**练习 2**：README 里说本项目要「offer a transparent, flexible, and privacy-friendly way to run your own LLM on local hardware」。请把这三个形容词（透明 / 灵活 / 保护隐私）分别和「开源」或「本地硬件」对应起来。

> **参考答案**：「透明」来自开源（代码可审计）；「保护隐私」来自本地硬件（数据不出本机）；「灵活」两者都有贡献（开源可改、FPGA 可重编程）。

---

### 4.2 PyTorch 到 Yosys 的总路线

#### 4.2.1 概念说明

排除 HLS 之后，剩下的是一条**编译器链（compiler chain）路线**。它把 PyTorch 模型当成「源代码」，经过多次「翻译（lowering，降级）」，最终变成 FPGA 能识别的硬件描述。整条路线是：

```text
PyTorch  →  Torch-MLIR  →  CIRCT  →  Verilog(SystemVerilog)  →  Yosys
```

五个阶段各自的角色：

| 阶段 | 是什么 | 在流水线里的职责 |
| --- | --- | --- |
| **PyTorch** | 你熟悉的深度学习框架 | 提供模型定义（`nn.Module`），是整条链的**输入**和**唯一真相源**。 |
| **Torch-MLIR** | 把 PyTorch 翻译成 MLIR 的前端 | 把模型图导出成 MLIR 的 `torch` 方言，再降级到与硬件更近的 `linalg` 方言。 |
| **CIRCT** | 基于 MLIR 的硬件编译器 | 经过一长串方言降级（Linalg → 控制流 → Handshake 数据流 → HW），最终导出 SystemVerilog。 |
| **Verilog / SystemVerilog** | 硬件描述语言 | CIRCT 的**输出**，也是 Yosys 的**输入**，是「软件世界」和「硬件世界」的交界。 |
| **Yosys** | 开源综合工具 | 把 SystemVerilog 综合成网表（RTLIL），并统计资源用量；配合 nextpnr 还能做布局布线、出比特流。 |

> 术语提示：**lowering（降级）** 是 MLIR 的核心动作——把一个高层、抽象的表示，一步步换成底层、接近硬件的表示。每一次 lowering 都可能经过好几个「dialect（方言）」。后面第 2、3、5 单元会逐个 dialect 展开讲，本讲只看大方向。

#### 4.2.2 核心流程

把整条路线画成数据流：

```text
┌─────────┐    ┌────────────┐    ┌────────┐    ┌──────────────┐    ┌───────┐
│ PyTorch │───>│ Torch-MLIR │───>│  CIRCT │───>│ SystemVerilog│───>│ Yosys │
│  模型   │    │ torch方言  │    │ 一长串 │    │   .sv 文件   │    │ RTLIL │
│ (真相源)│    │  → linalg  │    │ 降级链 │    │ (硬件描述)   │    │ + 资源│
└─────────┘    └────────────┘    └────────┘    └──────────────┘    └───────┘
   阶段1            阶段2           阶段3            阶段4            阶段5
```

这条链有一个非常重要的工程特性：**它可以增量开发**。如果某个 PyTorch 算子在 Torch-MLIR 或 CIRCT 里不被支持，开发者可以把它**改写、简化或分解**成支持的算子，而不必推倒重来。这正是本项目能在「上游工具不完美」的现实下持续推进的关键。

#### 4.2.3 源码精读

所选路线在 `1c-selected_route` 里有简洁的一行总结和逐条理由：

[deliverables/1c-selected_route.md:L42-L62](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/deliverables/1c-selected_route.md#L42-L62) —— 给出 `PyTorch -> Torch-MLIR -> CIRCT -> Verilog -> Yosys` 这一行，并列出选中它的六条理由：全开源、Torch-MLIR 可维护可扩展、CIRCT 能降级到可综合 Verilog、有发表论文背书（HLSfromPyTorch 与 StreamTensor）、支持增量开发、上游工具仍在活跃开发（截至 2025 年 12 月）。

README 里则用更精炼的话复述了同一条路线，并指明它有两个学术参考：

[README.md:L15-L41](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/README.md#L15-L41) —— 说明 Task 1 基于论文和「只用开源工具」的约束选定了这条编译器链，并列出两个参考：①「HLS with MLIR and CIRCT」论文及其 demo 仓库（发表了流水线，但只在很小的 demo 模型上测过）；②StreamTensor（同样用 torch-mlir 式路线做 LLM FPGA 推理，但不公开代码）。本项目的贡献正是把这条路线**真正跑通到一个真实 LLM**。

> 注意这条路线的递进式验证策略：Task 2 先用**最小 matmul 模型**验证流水线能跑通；Task 3 再换成**最小的真实 LLM（TinyStories-1M）**。这种「先最小核、再真实模型」的节奏，是理解后续所有讲义的关键。

#### 4.2.4 代码实践

1. **实践目标**：把五阶段路线和「它解决的理由」一一对应起来。
2. **操作步骤**：
   - 打开 [deliverables/1c-selected_route.md:L42-L62](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/deliverables/1c-selected_route.md#L42-L62)。
   - 在一张纸上画出 4.2.2 的数据流图。
   - 把「Selected route」下面的六条理由，分别用箭头标到它对应的阶段（例如「Torch-MLIR 可维护可扩展」标到阶段 2；「CIRCT 能降级到可综合 Verilog」标到阶段 3）。
3. **需要观察的现象**：注意六条理由里，有些是关于「为什么选这条路线」（开源、有论文背书、可增量开发），有些是关于「每个工具的具体能力」。
4. **预期结果**：你能指着图说出每个阶段「为什么是它、它能干什么、它替换掉哪个闭源工具」。
5. 本实践为纯阅读型，运行结果「待本地验证」不适用。

#### 4.2.5 小练习与答案

**练习 1**：在这条路线里，谁充当「唯一真相源（single source of truth）」？为什么这个选择对验证很重要？

> **参考答案**：PyTorch 模型是唯一真相源。因为整条链都是「降级」，理论上每一阶段的输出都应等价于 PyTorch 的计算结果。把 PyTorch 当黄金参考（golden reference），就能在仿真里逐阶段比对，定位是哪个 lowering pass 引入了偏差（这正是第 4 单元讲义的主题）。

**练习 2**：把 `PyTorch → Torch-MLIR → CIRCT → Verilog → Yosys` 里「软件」和「硬件」的分界线画在哪两个阶段之间？为什么？

> **参考答案**：分界线在 **CIRCT → Verilog** 之间。CIRCT 之前（含 CIRCT 内部）都是 MLIR 中间表示，属于「编译器/软件」世界；从 SystemVerilog 开始进入「硬件描述」世界；Yosys 则是把这些描述落实成网表/资源的「EDA」工具。SystemVerilog 是两个世界的交界点。

---

### 4.3 当前阶段结论与风险

#### 4.3.1 概念说明

理解项目现状，要先区分两个概念：

- **能否降级（lowering）**：模型能不能顺利通过编译器链，最终生成 SystemVerilog 并被 Yosys 综合。这考察的是**工具链的正确性与完备性**。
- **能否装下（fitting）**：生成的硬件**资源用量（LUT、FF、BRAM、DSP）**是否在目标 FPGA 的容量之内。这考察的是**硬件规模**。

TinyStories-1M 的结论是：**能降级，但远远装不下**。这是一个「成功了一半」的结果——它证明路线在技术上可行（流水线跑通了），但暴露了规模瓶颈（资源超配）。

衡量「装不装得下」最关键的指标是 **CLB LUT（查找表）**数量。LUT 是 FPGA 实现组合逻辑的基本单元，目标芯片 Xilinx XC7K480T 的 CLB LUT 容量是 **298,600**（注意：这是「CLB LUT」口径，与芯片名里的「480k」原始 LUT 口径不同，资源报告用的是更精确的 298,600）。

#### 4.3.2 核心流程

项目计划（`project-plan_v2`）把工作分成 6 个 Task，它们的依赖关系大致是：

```text
Task 1  调研与选路线（已完成 DONE）
  │
  ├─> Task 2  最小 matmul 核端到端跑通 + 语义等价验证
  │
  ├─> Task 3  最小 LLM（TinyStories-1M）降级 + 资源报告   ← 当前最相关结果
  │       │
  │       └─ 结论：能降级，但超配 141×
  │              └─> 所以下一步跳到 Task 6，而不是 Task 4
  │
  ├─> Task 4  FPGA 集成与硬件验证（依赖设计装得下）
  ├─> Task 5  TinyStories 家族 scaling 分析
  └─> Task 6  资源用量削减策略（DDR3 卸载、换掉 Handshake 方言、量化…）
```

关键的工程判断是：既然 Task 3 发现设计比目标 FPGA 大约 **141 倍**，那么直接做 Task 4（上板）是徒劳的——必须先做 Task 6（把资源降下来）。这是一个「先治病、再上板」的合理取舍。

资源超配倍数的计算很简单：

\[
\text{超配倍数} \;=\; \frac{\text{设计所需 CLB LUT}}{\text{目标 FPGA CLB LUT 容量}} \;=\; \frac{42{,}123{,}250}{298{,}600} \;\approx\; 141
\]

#### 4.3.3 源码精读

README 的「Current status」一段给出了最关键的两个数字和 nextpnr OOM 的事实：

[README.md:L42-L54](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/README.md#L42-L54) —— 说明当前最相关结果是 Task 3：baseline-float 流程装不下目标 FPGA，Yosys 估算需要 42,123,250 个 CLB LUT，而设备容量只有 298,600，约超 141 倍；并说明本来想用更合适的 nextpnr-xilinx 做资源报告，但每次都内存溢出（OOM），这和 141 倍超配的结论一致。

`1c-selected_route` 在「Risks」一节里早就预判了这三类风险，而 Task 3 正好命中了第三条：

[deliverables/1c-selected_route.md:L64-L69](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/deliverables/1c-selected_route.md#L64-L69) —— 列出三条风险：①部分 PyTorch 算子不被 Torch-MLIR 支持；②降级 LLM 时 Torch-MLIR 或 CIRCT 崩溃；③即便最小的 LLM 也装不进 Kintex 480k FPGA。Task 3 的结局正是风险③的现实化。

完整的 Task 划分在项目计划里，Task 3 与 Task 6 的目标定义尤其重要：

[docs/project-plan_v2.md:L177-L201](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/docs/project-plan_v2.md#L177-L201) —— Task 3 的目标：测试 TinyStories-1M 能否用全开源流程降级到可综合 RTL，并明确「本任务只验证降级路径是否存在，规模与硬件执行留给后续任务」「成功的产物是不含算子打桩（stubbing）的 RTL 网表和综合资源估算；否则是一份瓶颈报告」。这说明 Task 3 的「141 倍超配」其实是一个**被允许的成功结论**——它完成了「探明降级路径 + 给出瓶颈报告」的使命。

[docs/project-plan_v2.md:L440-L449](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/docs/project-plan_v2.md#L440-L449) —— Task 6 的目标：如果 scaling 显示更大的模型装不下，就评估各种缓解技术能否削减资源用量，基线是 scaling 任务里成功降级的最大模型。这正是 Task 3 之后要做的事。

最后，3e 报告把「为什么下一步是 Task 6 而不是 Task 4」讲得最直白：

[deliverables/3e-tiny-stories-1m-resource-report.md:L63-L87](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/deliverables/3e-tiny-stories-1m-resource-report.md#L63-L87) —— 结论：即便已经把超大 Handshake 存储外部化，TinyStories-1M 的外壳设计仍装不下；因为设计约比目标 FPGA 大 141 倍（按 LUT 计），所以下一个任务应是 Task 6（资源最小化）而非 Task 4；并指出两条可探索方向：①更直接地使用板载内存；②换用 Handshake 以外的 MLIR 方言（Handshake 方言在当前流程里消耗大量资源）。

#### 4.3.4 代码实践

本讲义要求的**核心代码实践**就在这一节：阅读 README 与 `1c-selected_route`，写一段不超过 200 字的说明。

1. **实践目标**：把「为什么不能直接用 Vitis HLS」和「所选路线的三个风险」内化为自己的表达。
2. **操作步骤**：
   - 精读 [README.md:L1-L54](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/README.md#L1-L54) 和 [deliverables/1c-selected_route.md:L7-L69](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/deliverables/1c-selected_route.md#L7-L69)。
   - 用**自己的话**写一段不超过 200 字的说明，必须包含两点：
     - 为什么不能直接用 Vitis HLS（闭源；开源 HLS 替代兼容性差）。
     - 所选路线的三个风险（算子不被支持；降级时工具崩溃；最小 LLM 也装不下）。
3. **需要观察的现象**：在写作时，检查自己是否真的理解了「全开源约束 → 否决 HLS → 选择 MLIR 编译器链」这条因果链，而不是照抄原文。
4. **预期结果**：你写出一段逻辑自洽的短文，能向一个没读过项目的同事解释清楚「为什么走这条路、它有什么风险」。
5. 本实践为写作型，无命令输出，不涉及「待本地验证」。

> 想进一步动手的读者，可以尝试真正复现资源报告（这一步会在第 5 单元讲义详细讲，本讲只列出命令供你先有个印象）：
>
> ```bash
> nix build .#tiny-stories-1m-baseline-float-selftest-all-memory-utilization -L
> cat result/summary.txt
> ```
>
> 该命令会构建整条工具链并产出 `result/summary.txt`。**首次构建会下载并编译 torch-MLIR、CIRCT、Yosys 等大量工具，耗时极长且需要较大磁盘/内存**，本讲不强求执行；若机器资源不足，记为「待本地验证」即可。

#### 4.3.5 小练习与答案

**练习 1**：README 说设计需要 42,123,250 个 CLB LUT，容量是 298,600。请手算这个倍数，并解释为什么 nextpnr-xilinx 会 OOM。

> **参考答案**：\( 42{,}123{,}250 / 298{,}600 \approx 141 \)。nextpnr 做的是布局布线（PnR），它要把设计里的每个单元摆放到芯片上并连线；当设计比芯片大两个数量级时，PnR 所需的内存会爆炸式增长，于是 nextpnr-xilinx 在能产出结果前就因内存耗尽而退出。这也说明：在资源严重超配时，用 Yosys 的 `stat` 做**估算**比强行跑 PnR 更现实。

**练习 2**：Task 3 的结论是「装不下」，那它算成功还是失败？请依据 `project-plan_v2` 里 Task 3 的目标定义作答。

> **参考答案**：算**成功（按 Task 3 自身定义）**。Task 3 的目标是「验证降级路径是否存在」并给出「资源估算或瓶颈报告」。TinyStories-1M 确实被成功降级、综合，并产出了清晰的瓶颈报告（141 倍超配 + nextpnr OOM），完全符合交付物 3e 的要求。「装不下」是 Task 4/6 要解决的问题，不是 Task 3 的失败。

**练习 3**：3e 报告提到「Handshake 方言在当前流程里消耗大量资源，换掉它是 Task 6 的目标之一」。结合本讲的五阶段路线，Handshake 方言大致出现在哪个阶段？

> **参考答案**：出现在**阶段 3（CIRCT 内部）**。CIRCT 会把控制流降到 Handshake 弹性数据流方言，再降到 HW 方言。Handshake 在第 3 单元讲义会详细展开；本讲只需记住「它是 CIRCT 降级链里资源开销最大的环节之一」。

## 5. 综合实践

设计一个贯穿本讲的小任务：**为 LLM2FPGA 画一张「一页纸项目全景图」**。

要求在一张纸上包含以下要素，并尽量用你自己的话（不要复制粘贴原文）：

1. **一句话项目目标**：包含「开源 LLM」「FPGA」「全开源 EDA」三个关键词。
2. **五阶段降级路线图**：`PyTorch → Torch-MLIR → CIRCT → SystemVerilog → Yosys`，并在每个阶段下用半句话注明它的职责。
3. **三个风险**：用三个小图标或短句标在路线图旁边，指明每个风险大概会卡在哪个阶段。
4. **当前结论**：写上「TinyStories-1M：能降级，但超配约 141×（42,123,250 vs 298,600 CLB LUT）」，并画一个箭头指向「下一步：Task 6 资源最小化」。
5. **三个关键数字**：141×（超配倍数）、298,600（目标容量）、XC7K480T（目标芯片）。

完成后，把这张图讲给一个不熟悉项目的同学听。如果你能在 3 分钟内让他明白「这个项目在做什么、用了什么路线、现在卡在哪」，说明你已经掌握了本讲的全部内容。

> 自检：如果你的图里漏掉了「全开源约束」或「PyTorch 是唯一真相源」这两点，请补上——它们是后续所有讲义反复出现的前提。

## 6. 本讲小结

- LLM2FPGA 的目标是用**全开源 EDA 工具链**把**开源 LLM**（从 TinyStories-1M 起步）跑到 FPGA 上，填补「没有全开源方案」的空白。
- 因为所有 HLS 方案都依赖闭源的 Vitis HLS、而开源 HLS 替代兼容性差，所以项目选择了**编译器链路线**：`PyTorch → Torch-MLIR → CIRCT → SystemVerilog → Yosys`。
- 这条路线有六条选中理由，核心是全开源、可增量开发、有论文背书；但也有三个预设风险（算子不支持、工具崩溃、装不下）。
- PyTorch 模型是整条链的**唯一真相源**，用于逐阶段验证语义等价（第 4 单元主题）。
- 当前状态：Task 3 已证明 TinyStories-1M **能降级**，但资源**超配约 141 倍**（42,123,250 vs 298,600 CLB LUT），nextpnr-xilinx 因此 OOM。
- 工程判断：下一步是 **Task 6（资源最小化）**，而不是 Task 4（上板）；可探索方向包括更直接地用板载内存、换掉 Handshake 方言、量化等。

## 7. 下一步学习建议

本讲建立了全局地图，接下来建议按以下顺序深入：

1. **先补环境与结构**：读第 1 单元剩余讲义——`u1-l2`（仓库结构与文档体系）、`u1-l3`（Nix 可复现工具链）、`u1-l4`（跑通第一个构建命令）。其中 `u1-l4` 会带你真正执行 `nix build` 并读懂 `result/summary.txt`，是本讲「待本地验证」部分的延续。
2. **再钻降级链**：第 2 单元讲 PyTorch 前端与 `torch.export`，第 3 单元讲 CIRCT 那一长串方言降级（这是全项目最核心、最难的部分）。
3. **想深入当前瓶颈**：可以直接跳到 `deliverables/3e-tiny-stories-1m-resource-report.md` 和第 6 单元（TinyStories 实战与瓶颈分析），理解「141 倍超配」到底是怎么来的、以及 Task 6 打算怎么削减它。
4. **权威阅读顺序**（README 推荐）：如果只读两份文件，先读 `deliverables/1c-selected_route.md`，再读 `deliverables/3e-tiny-stories-1m-resource-report.md`——它们正好对应本讲的 4.2 与 4.3。

> 建议在进入第 2 单元前，先把本讲「综合实践」的全景图画一遍，它会成为你阅读后续源码时的「导航图」。
