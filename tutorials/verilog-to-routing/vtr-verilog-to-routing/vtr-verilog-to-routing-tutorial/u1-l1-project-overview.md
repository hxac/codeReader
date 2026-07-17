# VTR 是什么：项目定位与设计理念

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标不是教你写代码，而是帮你建立对 VTR（Verilog-to-Routing）这个项目的**全局认知**。学完本讲，你应当能够：

- 用一句话说清 VTR 解决什么问题、它的输入和输出分别是什么。
- 说清 PARMYS → ABC → VPR 三段式 CAD 流程各自的职责，以及它们之间传递的是什么中间产物。
- 理解 VTR 最核心的设计哲学——**架构驱动（architecture-driven）**：目标 FPGA 不是写死在代码里的，而是在运行时通过一个 XML 文件传入的。

这三点是后续所有讲义的地基。如果地基没打好，后面读打包（Packing）、布局（Placement）、布线（Routing）的源码时，你会分不清「哪些是算法逻辑」「哪些是被架构文件驱动出来的行为」。

## 2. 前置知识

在开始前，你需要一点点背景概念。不熟悉的也没关系，下面用通俗的话解释。

- **FPGA（Field-Programmable Gate Array，现场可编程门阵列）**：一种可以通过编程重新配置内部连线的芯片。你可以把它想象成一块「可以反复擦写的电路板」——写一段 Verilog 代码，经过一套工具处理后，就能让这块芯片实现你想要的数字电路功能。
- **CAD（Computer-Aided Design，计算机辅助设计）工具**：把人类写的高级描述（如 Verilog）自动转换成芯片能用的底层配置的工具链。商业 FPGA 厂商（如 Intel、Xilinx）都有自己的闭源 CAD 工具；VTR 就是这类工具的**开源版本**，主要面向 FPGA 架构研究与教学。
- **Verilog**：一种硬件描述语言（HDL），用代码描述数字电路的逻辑行为，是 VTR 的输入。
- **网表（Netlist）**：电路的一种内部表示，由「器件（块）」和「连线（网）」组成。你可以把它理解成一张「元器件 + 连线」的清单。整个 CAD 流程，本质上就是在不断改写这张清单：先从 Verilog 翻译成原子级网表，再把原子聚成大块，最后决定每个块摆在芯片哪个位置、连线怎么走。
- **架构（Architecture）**：指目标 FPGA 芯片「长什么样」——它有哪些类型的逻辑块、布线通道有多宽、开关盒怎么连接等等。VTR 用一个 XML 文件来描述它。

## 3. 本讲源码地图

本讲不深入算法源码，主要阅读三类「项目说明型」文件，它们是理解项目定位的入口：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/README.md) | 面向所有读者的项目主页，用一两段话讲清了 VTR 是什么、流程是什么、怎么构建。 |
| [AGENTS.md](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/AGENTS.md) | 面向 AI 代理和开发者的精简指南，浓缩了项目概述、构建方式和测试命令。 |
| [doc/agents/codebase.md](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/agents/codebase.md) | 代码库地图：顶层目录职责、VPR 内部子目录、数据流走向、共享库一览。是后续定位源码的「索引」。 |

此外，本讲还会引用一个真实的 FPGA 架构示例文件 [vtr_flow/arch/common/arch.xml](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/arch/common/arch.xml)，用来直观感受「架构文件长什么样」。

> 说明：本讲引用的是项目说明文档与示例文件。从下一篇讲义（u1-l2）起，才会进入真正的 C++ 源码。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**项目背景与价值**、**三段式 CAD 流程总览**、**架构驱动设计理念**。

### 4.1 VTR 项目背景与价值

#### 4.1.1 概念说明

要理解一个项目，先问三个问题：**它是什么？为谁服务？为什么有价值？**

VTR（Verilog-to-Routing）是一个**开源的 FPGA CAD 框架**，由全球多个高校和公司（README 里列出了来自 Intel、Huawei、Lattice、Altera、Google、Antmicro 等公司的贡献者）协作维护。它的服务对象主要是：

- **FPGA 架构研究者**：想设计一种新的 FPGA 内部结构（比如更宽的布线通道、新型的逻辑块），需要一套工具来评估这个新架构的「速度」和「面积」表现。
- **CAD 算法研究者**：想发明更好的打包/布局/布线算法，需要一个真实、可扩展的实验平台。
- **教学场景**：帮助学生理解从代码到芯片的完整过程。

它的**价值**在于「开源 + 可换架构」。商业 FPGA 厂商的闭源工具只能针对自家芯片，且无法改动内部算法；而 VTR 把整套流程和源码都公开，并且**允许你自定义目标 FPGA 架构**——这正是 FPGA 学术研究的刚需。

#### 4.1.2 核心流程（价值定位）

从「价值」的角度，VTR 的工作可以概括为一条因果链：

```
研究/教学需求
   │  需要「能换架构、能改算法」的开源 FPGA CAD
   ▼
VTR 框架
   │  接收：Verilog 电路 + 目标 FPGA 架构（XML）
   ▼
评估结果：FPGA 速度（时序）与面积
（可选附加产物：FASM，可用于编程部分商业 FPGA）
```

注意最后一点：VTR 主要产出的是**评估结果（speed & area）**，而不是一定要把比特流烧进某块真实芯片。不过它也能产生 [FASM](https://fasm.readthedocs.io/en/latest/) 格式，配合 F4PGA/SymbiFlow 工具链去编程部分商业 FPGA，这让它兼具研究与实用价值。

#### 4.1.3 源码精读

VTR 的自我定位写得最清楚的地方，是 README 开头的 Introduction：

[README.md:L6-L13](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/README.md#L6-L13) — 这段话用三句话讲清了 VTR 是什么、输入是什么、产出是什么。原文要点：

> The VTR design flow takes as input a Verilog description of a digital circuit, and a description of the target FPGA architecture. … to generate FPGA speed and area results.

这段是全本手册最重要的一句话，请记住它：**两个输入（电路 Verilog + 架构 XML）→ 一个产出（速度与面积结果）**。

[README.md:L16](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/README.md#L16) — 这一行点出 VTR 还能产生 FASM，并通过 SymbiFlow/F4PGA 编程部分商业 FPGA，体现了它的实用延展性。

[README.md:L36-L39](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/README.md#L36-L39) — 这里给出了 VTR 的权威学术引用（VTR 9 论文，发表于 ACM TRETS 2025）。标题里的 "Open-Source CAD for Fabric and Beyond FPGA Architecture Exploration" 正好印证了上面说的价值定位：开源 CAD，面向 FPGA 架构探索。

#### 4.1.4 代码实践

这是一道**源码阅读型实践**，目的是让你亲手从项目说明里提炼定位。

1. **实践目标**：用自己的话写出一句话定位 VTR。
2. **操作步骤**：
   - 打开 [README.md:L5-L17](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/README.md#L5-L17)，重点读 Introduction。
   - 打开 [AGENTS.md:L14-L24](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/AGENTS.md#L14-L24) 的 Project Overview。
3. **需要观察的现象**：两个文件对 VTR 的描述是否一致？AGENTS.md 是否更精简、更面向开发者？
4. **预期结果**：你能写出类似「VTR 是一个开源 FPGA CAD 框架，输入 Verilog 电路和 FPGA 架构描述，输出布局布线结果及速度面积评估」这样一句话。
5. 如无法判断，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：VTR 主要服务哪两类核心人群？
> **参考答案**：FPGA 架构研究者（评估新架构的速度/面积）和 CAD 算法研究者/教学者（在一个可扩展的开源平台上开发或讲解算法）。

**练习 2**：VTR 的最终产物是「把配置烧进某块真实 FPGA 芯片」吗？
> **参考答案**：不完全是。VTR 的核心产物是 FPGA 的**速度（时序）与面积评估结果**；它可选地产生 FASM，再借助 F4PGA/SymbiFlow 才能编程部分商业 FPGA。所以「评估」是主线，「编程真实芯片」是附加能力。

---

### 4.2 三段式 CAD 流程总览

#### 4.2.1 概念说明

VTR 把从 Verilog 到布线结果的整个过程，分成**三个大阶段**，每段由一个独立工具负责：

| 阶段 | 工具 | 职责 | 是否在仓库内维护 |
|------|------|------|------------------|
| 1. Elaboration, Synthesis & Partial Mapping | **PARMYS**（基于 Yosys） | 把 Verilog 展开、综合、做部分映射 | 仓库内维护（但 Yosys 本身是外部依赖） |
| 2. Logic Optimization & Technology Mapping | **ABC** | 逻辑优化与技术映射（把逻辑映射成 LUT/FF 等原语） | 外部，**不可直接修改** |
| 3. Packing, Placement, Routing & Timing Analysis | **VPR** | 打包、布局、布线、时序分析 | 仓库内，**主要开发目标** |

几个要点：

- **PARMYS** 取代了早期的 Odin 前端（`odin_ii/` 现在默认禁用，仅作遗留）。
- **ABC** 是一个外部项目，VTR 直接调用它的可执行文件，所以代码在仓库里但**不应直接修改**。
- **VPR** 是整个仓库的核心开发目标（primary development target），后面绝大多数讲义都在讲 VPR。

#### 4.2.2 核心流程

整个流程的输入输出与中间产物可以这样画：

```
输入1: circuit.v (Verilog)        输入2: architecture.xml (目标 FPGA 架构)
            │                                  │
            ▼                                  │
   ┌─────────────────┐                         │
   │  ① PARMYS       │  综合 + 部分映射          │
   │  (Yosys-based)  │                         │
   └────────┬────────┘                         │
            ▼  (网表/映射后逻辑)                 │
   ┌─────────────────┐                         │
   │  ② ABC          │  逻辑优化 + 技术映射       │
   │  (外部)          │  产出: 原子级网表 (.blif)  │
   └────────┬────────┘                         │
            ▼                                  ▼
   ┌──────────────────────────────────────────────┐
   │  ③ VPR (Packing → Placement → Routing →      │
   │          Timing Analysis)                    │
   │  输入: 原子级网表 + 架构                        │
   │  产出: 打包/布局/布线结果 + 速度与面积评估        │
   └──────────────────────────────────────────────┘
```

进入 VPR 之后，内部还有一条更细的数据流（这在第 3 单元会深入，这里先建立印象）。根据代码库文档，VPR 的传统数据流是：

```
AtomNetlist → Prepacker → Packer → ClusteredNetlist → Placer → Router
```

不必现在记住每个名字，只需理解一条主线：**网表在不断被「改写」——先从原子级（一个个 LUT/FF），聚合成逻辑块级（ClusteredNetlist），再决定每块摆在器件网格（Device Grid）的哪个位置，最后用布线资源图（Routing Resource Graph）把所有连线连通。**

#### 4.2.3 源码精读

README 把三段流程列得最清楚：

[README.md:L7-L13](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/README.md#L7-L13) — 明确写出「输入是 Verilog 电路 + 架构描述」，随后列出三个 `*` 要点：PARMYS、ABC、VPR，最后「to generate FPGA speed and area results」。这是三段式流程的权威出处。

AGENTS.md 用更精简、面向开发者的方式复述了同一条流程，并标注了哪些是仓库内维护、哪些是外部：

[AGENTS.md:L18-L20](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/AGENTS.md#L18-L20) — 三个编号项分别说明：PARMYS「maintained here; Yosys itself is external」、ABC「external, do not modify」、VPR「primary development target」。

代码库地图把三段流程落到了**目录**上，让你知道每段对应仓库里的哪个文件夹：

[doc/agents/codebase.md:L9-L15](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/agents/codebase.md#L9-L15) — 顶层组件表里，`vpr/`、`parmys/`、`abc/`、`odin_ii/` 一一对应三段流程的各个工具。

进入 VPR 内部后的细粒度数据流：

[doc/agents/codebase.md:L38-L46](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/agents/codebase.md#L38-L46) — 这里画出 `AtomNetlist → Prepacker → Packer → ClusteredNetlist → Placer → Router`，并解释每一步在哪个子目录、做了什么。这一段是后面第 3–6 单元的总纲。

#### 4.2.4 代码实践

这是一道**目录与流程对应型实践**，帮你把抽象流程落到具体文件位置。

1. **实践目标**：把三段流程的每个阶段，对应到仓库里的真实目录。
2. **操作步骤**：
   - 打开 [doc/agents/codebase.md:L5-L32](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/agents/codebase.md#L5-L32)。
   - 在「Top-Level Components」表里找到 `parmys/`、`abc/`、`vpr/` 三个目录，确认它们对应三段流程。
   - 在「VPR Internal Structure」表里，找到 `pack/`、`place/`、`route/`、`timing/` 四个子目录，分别对应 VPR 内部的打包、布局、布线、时序分析。
3. **需要观察的现象**：哪些目录标注了「external / do not modify」？哪些是 VPR 的核心子目录？
4. **预期结果**：你能填出类似下面的对应关系——
   - PARMYS → `parmys/`
   - ABC → `abc/`（外部，不可改）
   - VPR 打包 → `vpr/src/pack/`
   - VPR 布局 → `vpr/src/place/`
   - VPR 布线 → `vpr/src/route/`
5. 如无法确认目录是否存在，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：三段流程中，哪一段是「外部、不可直接修改」的？
> **参考答案**：第二段 ABC。它是外部项目，仓库里虽有代码但不应直接改动（顶层 `abc/` 同样属于外部，见 [doc/agents/codebase.md:L66](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/agents/codebase.md#L66)）。

**练习 2**：VPR 内部的 `AtomNetlist → Prepacker → Packer → ClusteredNetlist → Placer → Router` 这条数据流里，哪一个环节把「原子级」提升为「逻辑块级」？
> **参考答案**：Packer。它把分子（molecules）聚簇成逻辑块，产出 ClusteredNetlist（见 [doc/agents/codebase.md:L44](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/agents/codebase.md#L44)）。

---

### 4.3 架构驱动设计理念

#### 4.3.1 概念说明

如果说前两个模块回答了「VTR 做什么」，那么这个模块回答的是「**VTR 为什么这样设计**」——这是理解整个代码库最关键的一把钥匙。

VTR 的一个**定义性特征（defining feature）**是：它能针对**很多种不同的 FPGA 架构**工作，而目标架构是**在运行时通过一个 XML 文件传入**的。这句话的含义非常深远：

- VPR 的算法代码里**不能写死**任何「这块 FPGA 一定是 8 输入 LUT」「布线通道一定是 12 根线」之类的假设。
- 所有架构相关的事实（有哪些逻辑块类型、LUT 几输入、有多少内存、布线通道多宽、开关盒怎么连……）都必须**来自运行时解析的 XML**。
- 启动时，VTR 把这个 XML 解析成一套数据结构（定义在共享库 `libarchfpga` 里），此后所有阶段都基于这套数据结构工作。

这种设计叫做**架构驱动（architecture-driven）**。它的好处是：研究者只需改一个 XML 文件，就能让同一套 CAD 算法跑在一块「完全不同」的 FPGA 上，从而对比不同架构的优劣——这正是 FPGA 架构探索的核心工作流。

#### 4.3.2 核心流程（架构如何驱动全流程）

架构文件从「输入」变成「驱动一切的数据结构」，过程如下：

```
architecture.xml  (人类可读的目标 FPGA 描述)
        │
        ▼  启动时解析（pugixml 封装）
libarchfpga  (架构 XML 解析器 + 数据结构, arch_types.h)
        │
        ▼  产出
t_arch 等数据结构  (器件类型 / 开关 / 线段 / 网格…)
        │
        ▼  被各阶段读取
   Packing / Placement / Routing / Timing
   (算法逻辑通用，行为由架构数据结构决定)
```

一个直接推论：**当你在 VPR 里看到某个算法的怪异行为，先怀疑是不是架构文件配置导致的，而不是算法写错了。** 这个思维习惯会贯穿你阅读后续所有源码的过程。

#### 4.3.3 源码精读

这条设计理念在 AGENTS.md 里被明确点出：

[AGENTS.md:L22](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/AGENTS.md#L22) — 原文：

> A defining feature: VTR targets many different FPGA architectures, with the target architecture passed in as an XML file at runtime. CAD algorithms must therefore avoid hardcoding architecture-specific assumptions — the architecture is parsed at startup into data structures defined in `libarchfpga`.

这一行是本讲、乃至全本手册最重要的一句话。它同时给出了「为什么」和「怎么做」：因为要支持多种架构，所以不能硬编码；架构在启动时被解析进 `libarchfpga` 的数据结构。

那么 `libarchfpga` 是什么？代码库地图里有明确说明：

[doc/agents/codebase.md:L56](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/agents/codebase.md#L56) — 在共享库表里，`libarchfpga` 的描述是「FPGA architecture XML parser and data structures (`arch_types.h`)」。也就是说，架构解析器和它产出的数据结构都住在这个库里。第 2 单元会专门精读它。

为了让你直观感受「架构文件长什么样」，我们看一个真实的、最简单的示例架构文件开头：

[vtr_flow/arch/common/arch.xml:L1-L13](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/arch/common/arch.xml#L1-L13) — 关键片段（示例代码，仅摘取结构骨架）：

```xml
<architecture>
  <models>
    <model name="DFF">          <!-- 定义一个原语模型：D 触发器 -->
      <input_ports> ... </input_ports>
      <output_ports> ... </output_ports>
    </model>
  </models>
  <tiles>                       <!-- 定义器件上的物理瓦片 -->
    <tile name="ff_tile">
      <sub_tile name="ff_tile">
        <equivalent_sites> ... </equivalent_sites>
        ...
      </sub_tile>
    </tile>
  </tiles>
  ...
</architecture>
```

可以看到，`<architecture>` 是根元素，里面用 `<models>`、`<tiles>`、`<sub_tile>` 等标签**声明式地**描述了 FPGA 上有什么。换一个 XML，就是一块不同的 FPGA——而 VPR 的算法代码一行都不用改。这就是「架构驱动」的具体体现。

#### 4.3.4 代码实践

这是一道**架构文件阅读型实践**，帮你亲手验证「架构是运行时配置」。

1. **实践目标**：打开一个真实架构 XML，识别它的主要组成部分，并确认它就是「目标 FPGA」的描述。
2. **操作步骤**：
   - 用编辑器打开 [vtr_flow/arch/common/arch.xml](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/arch/common/arch.xml)，浏览它的顶层标签（`<architecture>` 下的 `<models>`、`<tiles>`、`<layout>`、`<device>`、`<switchlist>`、`<segmentlist>` 等）。
   - 再打开一个更接近真实商业芯片的架构，例如 [vtr_flow/arch/COFFE_22nm/stratix10_arch.xml](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/arch/COFFE_22nm/stratix10_arch.xml) 或 [vtr_flow/arch/xilinx/simple-7series.xml](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/arch/xilinx/simple-7series.xml)。
3. **需要观察的现象**：两个文件描述的是不是「同一块 FPGA」？（显然不是，标签内容差异很大。）但 VPR 用的是不是同一套算法代码？（是。）
4. **预期结果**：你能体会到——**改 XML = 换一块 FPGA，而算法代码不变**。这正好印证了 [AGENTS.md:L22](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/AGENTS.md#L22) 那句「avoid hardcoding architecture-specific assumptions」。
5. 如无法打开或不确定标签含义，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 VPR 的算法代码不能写死「LUT 输入数为 6」这样的假设？
> **参考答案**：因为目标 FPGA 是运行时由 XML 决定的，不同架构的 LUT 输入数可能不同（仓库里就有 `k4`、`k6` 等不同输入数的架构文件）。写死假设会让同一套算法无法适配其他架构，违背 VTR「架构驱动、支持多种架构」的设计目标（见 [AGENTS.md:L22](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/AGENTS.md#L22)）。

**练习 2**：架构 XML 在启动时被解析成数据结构，这些数据结构定义在哪个库里？
> **参考答案**：`libarchfpga`（见 [doc/agents/codebase.md:L56](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/doc/agents/codebase.md#L56)），核心类型头文件是 `arch_types.h`。第 2 单元会精读它。

**练习 3**：当你在调试 VPR 时发现某个行为很反常，根据本讲的理念，第一反应应该怀疑什么？
> **参考答案**：先怀疑是不是架构 XML 的配置导致的（比如某个 `depop`、通道宽度、开关盒设置），而不是算法本身写错了。因为算法行为是由运行时架构数据结构驱动的。

## 5. 综合实践

本实践把三个模块串起来，是本讲的「结业任务」，也对应本讲规格里的代码实践任务。

**任务**：阅读 [README.md](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/README.md) 与 [AGENTS.md](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/AGENTS.md)，完成以下两件事：

1. **用一段话写出 VTR 的输入、三段流程与最终产物**。要求：
   - 明确写出**两个输入**（电路 Verilog + 架构 XML）。
   - 写出**三段流程**及其负责工具（PARMYS / ABC / VPR），并标注哪段是外部不可改、哪段是主开发目标。
   - 写出**最终产物**（速度与面积评估，可选 FASM）。

2. **列出 VTR 支持的两到三个目标 FPGA 架构文件来源**。在 `vtr_flow/arch/` 下找出真实的 XML 文件并给出路径，例如（这些都是仓库中真实存在的文件）：
   - 通用示例：`vtr_flow/arch/common/arch.xml`
   - 类 Stratix 10 架构：`vtr_flow/arch/COFFE_22nm/stratix10_arch.xml`
   - 类 Xilinx 7 系列架构：`vtr_flow/arch/xilinx/simple-7series.xml`
   - Titan 基准测试架构：`vtr_flow/arch/titan/stratix10_arch.timing.xml`
   - 类 Ultrascale 架构：`vtr_flow/arch/ispd/ultrascale_ispd.xml`

**交付物**：一段文字描述 + 一个架构文件路径清单。

**自检**：如果你的描述里出现了「写死某架构」「只支持一种 FPGA」之类的话，说明还没理解 4.3 的架构驱动理念，请回看 [AGENTS.md:L22](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/AGENTS.md#L22)。

> 注：本实践为源码/文档阅读型，不涉及编译运行；如需确认文件是否存在，可在本地 `ls vtr_flow/arch/` 验证。

## 6. 本讲小结

- VTR 是一个**开源 FPGA CAD 框架**，核心价值是「开源 + 可换架构」，服务 FPGA 架构研究与 CAD 算法开发。
- 它的输入是**两个**：数字电路的 Verilog 描述 + 目标 FPGA 架构的 XML 描述；产出是 FPGA 的**速度（时序）与面积评估**（可选 FASM）。
- 整个流程分**三段**：PARMYS（综合/部分映射，仓库内维护）→ ABC（逻辑优化/技术映射，外部不可改）→ VPR（打包/布局/布线/时序分析，**主开发目标**）。
- VPR 内部数据流是 `AtomNetlist → Prepacker → Packer → ClusteredNetlist → Placer → Router`，本质是网表被不断改写、逐层抽象。
- 最关键的设计理念是**架构驱动**：目标 FPGA 由运行时 XML 决定，算法代码**绝不硬编码**架构假设；XML 在启动时被解析进 `libarchfpga` 的数据结构，驱动后续所有阶段。
- 记住一条调试直觉：VPR 的反常行为，先怀疑架构 XML，再怀疑算法。

## 7. 下一步学习建议

本讲建立了全局观，下一篇讲义建议学习 **u1-l2《构建与运行 VTR》**，亲手把项目编译出来，让抽象的流程变成可运行的命令。

之后再按顺序学习：
- **u1-l3《仓库目录结构与组件地图》**：把本讲提到的目录细化成一张速查表。
- **u1-l4《一键跑通 VTR 全流程》**：用 `run_vtr_flow.py` 把一个真实电路从 Verilog 跑到布线结果，亲眼看到三段流程的中间产物。

在进入第 2 单元（架构 XML 与 `libarchfpga` 解析）之前，务必先跑通一次完整流程——**架构驱动**这条理念，只有当你亲自换一个架构 XML、看到结果变化时，才会真正理解。

> 建议继续阅读的源码（按顺序）：[BUILDING.md](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/BUILDING.md)、[Makefile](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/Makefile)、[vtr_flow/README.md](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/README.md)。
