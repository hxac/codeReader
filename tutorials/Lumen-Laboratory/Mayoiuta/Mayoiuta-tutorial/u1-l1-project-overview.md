# Mayoiuta 是什么：开源 NPU 项目定位

> 本讲是 Mayoiuta 学习手册的第一篇。它不要求你懂 Verilog 或 Windows 驱动开发，只需要你跟着 README，先把「这个项目到底是什么、由什么组成、缺什么」搞清楚。后面所有讲义都建立在这张全局地图之上。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清楚 **NPU（神经网络处理器）** 是什么，以及它和通用 CPU、GPU 在 AI 加速上的根本区别。
2. 复述 Mayoiuta 的 **四大愿景**（开放、灵活、可扩展、教育）和 **五条核心特性**，并能在 README 中找到对应原文。
3. 识别本仓库的 **两部分源码构成**：`hardware/rtl/` 下的 Verilog RTL 硬件设计，与 `driver/win32/` 下的 Windows 内核驱动。
4. 区分 README 中 **「承诺提供」** 与仓库中 **「实际存在」** 的内容，并把缺失项（编译器/汇编器/调试器工具链、仿真环境、详细文档）逐项标注为「待确认」。

## 2. 前置知识

本讲是入门第一篇，几乎不需要专业背景，但下面几个名词先建立直觉会有帮助：

- **AI 加速（AI Acceleration）**：训练和运行神经网络需要海量的「乘加」运算。通用 CPU 不擅长这种重复计算，所以人们造专门的硬件来加速它。
- **RTL（Register Transfer Level，寄存器传输级）**：用硬件描述语言（如 Verilog）写出来的、描述数字电路在寄存器之间如何搬运和计算数据的代码。它离「真实的芯片」最近，是芯片设计的核心产物。
- **内核驱动（Kernel Driver）**：操作系统内核里的一段程序，负责让操作系统认识并指挥一块硬件。Mayoiuta 选用的是 **Windows WDF（Windows Driver Framework）** 驱动。
- **开源（Open Source）**：源代码公开，任何人都可以阅读、学习、修改并在协议允许下重新发布。Mayoiuta 使用 **Apache License 2.0**。

如果你对上面某个词还陌生，没关系，本讲会用最直白的方式带你过一遍。

## 3. 本讲源码地图

本讲主要是「读 README 建立全局印象」，因此只直接引用两个文件，但会盘点整个仓库的真实结构。

| 文件 / 目录 | 作用 | 本讲如何使用 |
| --- | --- | --- |
| `README.md` | 项目自述文件，描述定位、愿景、特性、架构概览 | 本讲的核心阅读对象 |
| `LICENSE` | Apache License 2.0 全文，明确开源协议 | 确认项目的开源协议 |
| `hardware/rtl/` | 9 个 Verilog 文件，描述 NPU 的硬件电路 | 盘点「硬件部分」的实际构成 |
| `driver/win32/` | 3 个文件（`.c`/`.h`/`.inf`），Windows 内核驱动 | 盘点「驱动部分」的实际构成 |

> 提示：`hardware/` 与 `driver/` 两个子目录的内部结构会在后续讲义（u1-l2「仓库结构与源码导航」）里逐层展开。本讲只关心它们「整体上代表什么」。

## 4. 核心概念与源码讲解

### 4.1 什么是 NPU：为什么 AI 需要专门的加速器

#### 4.1.1 概念说明

**NPU（Neural Processing Unit，神经网络处理器）** 是为神经网络运算量身打造的专用芯片。要理解它为什么存在，先看三类处理器的分工：

- **CPU（通用处理器）**：擅长「复杂的、带分支判断的逻辑」。它核心数少，但每个核都很聪明，适合跑操作系统、数据库这类任务。
- **GPU（图形处理器）**：擅长「成千上万个互相独立的、简单的并行计算」。它本来是为渲染画面设计的，后来发现也很适合矩阵运算，于是成了 AI 训练的主力。
- **NPU（神经网络处理器）**：比 GPU 还要「专」。它直接把神经网络里最频繁的「乘加运算（multiply-accumulate, MAC）」做成电路里固定的、极高效的单元（比如脉动阵列），用更低的功耗换来更高的 AI 算力。

一句话直觉：**CPU 追求「什么都会」，GPU 追求「并行得多」，NPU 追求「为 AI 算得又快又省电」。**

#### 4.1.2 核心流程

为什么「专用」能换来更高能效？可以粗略地这样理解 AI 算力：

\[ \text{算力(TOPS)} \;\approx\; \text{并行计算单元数} \times \text{时钟频率} \times \text{每个周期完成的运算数} \]

CPU 的并行单元少；GPU 多但每次取指令有开销；NPU 把「乘加」焊死成固定电路（即所谓 **固定功能硬件**），所以「每个周期完成的运算数」特别大，且不用频繁取指令，功耗更低。这就是 NPU 存在的意义，也是 Mayoiuta 想要亲手实现的「专用加速器」。

> 说明：上面的公式只是一个用于建立直觉的示意式（**示例公式**），并非 README 给出的规范定义，实际芯片的 TOPS 还受存储带宽、数据复用、散热等大量因素影响。

#### 4.1.3 源码精读

README 的开头一句话就给 Mayoiuta 下了定义：

[README.md:1-5](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/README.md#L1-L5) — 项目标题、一句口号（"Loss gradients are irrelevant, let's fit while lost!"），以及对 Mayoiuta 的定义：一个开源 NPU 项目，目标是为研究者、开发者和爱好者提供灵活、可扩展的 AI 加速技术探索平台。

注意第 5 行的关键定语：**"open-source Neural Processing Unit (NPU) project"**，这把 Mayoiuta 的身份交代得很清楚——它不是软件库，而是一颗「NPU 的设计」。

#### 4.1.4 代码实践

1. **实践目标**：用最直白的方式区分 CPU / GPU / NPU 三者。
2. **操作步骤**：
   - 打开本仓库的 [README.md:1-5](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/README.md#L1-L5)。
   - 找到定义 Mayoiuta 身份的那一句（提示：含 "open-source Neural Processing Unit"）。
   - 画一张三列表格：`处理器类型 | 擅长什么 | 为什么适合/不适合 AI`，分别填 CPU、GPU、NPU。
3. **需要观察的现象**：你会发现自己能用一句话讲清「为什么 AI 要专门的 NPU」。
4. **预期结果**：NPU 一栏应当突出「为乘加运算定制电路、高能效」这类关键词。
5. 本实践为纯阅读与归纳，**待本地验证**的是你自己的总结是否准确。

#### 4.1.5 小练习与答案

**练习 1**：为什么 CPU 不适合大规模 AI 推理？请用一个理由回答。
**参考答案**：CPU 并行计算单元少、且每个周期需频繁取指译码，无法像 NPU 那样把海量乘加运算做成高密度固定电路，因此在同样功耗下 AI 算力远低于专用加速器。

**练习 2**：NPU 比 GPU 更「专用」，体现在哪里？
**参考答案**：NPU 直接把神经网络最高频的乘加（MAC）做成固定的硬件单元（如脉动阵列），省去大量取指与通用控制开销，用牺牲「通用性」换取 AI 任务上的高能效。

---

### 4.2 四大愿景与五条核心特性

#### 4.2.1 概念说明

README 用两个小节说明了 Mayoiuta「想成为什么」（Project Vision）和「宣称提供什么」（Key Features）。这两部分是我们判断「项目承诺 vs. 仓库现状」的基准线，后面 4.4 节会拿它对照。

#### 4.2.2 核心流程

四个愿景与五条特性的关系是：**愿景 = 目标**（我们希望它成为的样子），**特性 = 手段**（为此它宣称具备的能力）。阅读时可以这样对应：

- 愿景「开放 Openness」← 特性「开源 + Apache 2.0 协议」
- 愿景「灵活 Flexibility」← 特性「可配置参数 Configurable Parameters」
- 愿景「可扩展 Scalability」← 特性「模块化设计 Modular Design」
- 愿景「教育 Education」← 特性「详细文档 Detailed Documentation」+「仿真环境 Simulation Environment」

注意：上表是我们根据文字做的「逻辑对应」，README 本身并没有显式画这张对照表，**待确认**这些对应是否就是作者本意。

#### 4.2.3 源码精读

先看四大愿景：

[README.md:7-14](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/README.md#L7-L14) — Project Vision 小节，列出四条目标：Openness（开放，鼓励社区参与）、Flexibility（灵活，支持多种神经网络结构）、Scalability（可扩展，允许用户调整 NPU 规模与性能）、Education（教育，提供学习 NPU 设计实现的实践平台）。

再看五条核心特性：

[README.md:16-22](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/README.md#L16-L22) — Key Features 小节，列出五条：Modular Design（模块化设计，便于定制扩展）、Configurable Parameters（丰富配置参数）、Simulation Environment（提供基于模拟器的仿真环境）、Open-Source Toolchain（开源工具链，含编译器/汇编器/调试器）、Detailed Documentation（详细文档）。

#### 4.2.4 代码实践

1. **实践目标**：验证「可配置参数」「模块化设计」这两条特性在仓库里是否有迹可循。
2. **操作步骤**：
   - 用编辑器打开 `hardware/rtl/top/npu_soc.v` 的前若干行，找形如 `parameter CORES = ...` 的参数声明（这种 `parameter` 就是 Verilog 的「可配置参数」）。
   - 观察 `hardware/rtl/` 下被分成 `core/ memory/ power/ sparse/ control/ top/` 多个子目录——这就是「模块化设计」的物理体现。
3. **需要观察的现象**：你能指出至少 1 个 `parameter` 与至少 3 个模块子目录。
4. **预期结果**：确认「模块化设计」与「可配置参数」两条特性在仓库里有真实证据支撑（具体模块会在后续讲义展开）。
5. 本实践为源码阅读型，**待本地验证**你找到的具体 `parameter` 名称与取值。

#### 4.2.5 小练习与答案

**练习 1**：请按 README 原文顺序，默写出 Mayoiuta 的四大愿景。
**参考答案**：Openness（开放）、Flexibility（灵活）、Scalability（可扩展）、Education（教育）。

**练习 2**：README 的 Key Features 一共列了几条？其中哪几条目前**无法**仅凭仓库内容证实？
**参考答案**：共 5 条（Modular Design、Configurable Parameters、Simulation Environment、Open-Source Toolchain、Detailed Documentation）。其中 **Simulation Environment（仿真环境）**、**Open-Source Toolchain（工具链）**、**Detailed Documentation（详细文档）** 三条在当前仓库里找不到对应文件，属于「承诺但尚未提供」，需标注为待确认（详见 4.4 节）。

---

### 4.3 仓库的两部分源码构成：硬件设计 + Windows 驱动

#### 4.3.1 概念说明

一颗 NPU 要真正可用，通常需要 **两样东西**：

1. **硬件设计**：描述芯片内部电路长什么样的设计文件（Mayoiuta 用 Verilog RTL）。
2. **驱动程序**：让操作系统能认识这块硬件、给它发任务、读它结果的软件（Mayoiuta 提供 Windows 内核驱动）。

Mayoiuta 的仓库正好体现了这两层：`hardware/rtl/` 是「硬件长这样」，`driver/win32/` 是「Windows 怎么指挥它」。

#### 4.3.2 核心流程

两类源码在 AI 推理中的协作流程，可以粗略画成：

```
用户程序（应用层）
     │  发送推理请求（如 IOCTL 命令）
     ▼
driver/win32/ （Windows 内核驱动）
     │  把请求翻译成寄存器写 / DMA 传输 / 中断处理
     ▼
hardware/rtl/ （NPU 硬件电路）
     │  PE 阵列、卷积引擎等真正完成乘加运算
     ▼
   计算结果（经中断/状态寄存器回报给驱动）
```

也就是说：**驱动负责「通信与调度」，硬件负责「算」**。理解这个分工，就理解了为什么要同时学这两部分源码。

> 说明：上图是我们基于「硬件 + 驱动」两部分的存在所做的合理推断，**待确认**真实的请求链路在细节上是否完全如此（端到端链路会在 u4-l3「全系统数据通路与集成」讲义里细化）。

#### 4.3.3 源码精读

我们用 `git ls-files` 盘点仓库真实包含的全部文件（共 14 个），按两部分归类如下。

**硬件部分 `hardware/rtl/`（9 个 Verilog 文件）**：按功能域分子目录——计算（core）、存储（memory）、能耗（power）、稀疏（sparse）、控制（control）、顶层（top）。典型代表是顶层 SoC：

[README.md:24-31](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/README.md#L24-L31) — Architecture Overview 小节，README 把 NPU 架构概括为四个核心模块：Compute Unit（计算单元，执行卷积/矩阵乘）、Memory Unit（存储单元，存权重与激活）、Interconnect Network（互连网络，在 CU 与 MU 间搬数据）、Control Unit（控制单元，整体调度）。这四类正好对应仓库里 `core/`、`memory/`、`top/`（含片上网络 NoC）、`control/` 的子目录划分。

**驱动部分 `driver/win32/`（3 个文件）**：`npudriver.c`（驱动主体逻辑）、`npudriver.h`（寄存器与设备上下文定义）、`setup.inf`（Windows 安装信息文件，声明 PCI 设备匹配）。

**协议部分**：根目录的 `LICENSE` 为 Apache License 2.0 全文：

[LICENSE:1-2](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/LICENSE#L1-L2) — LICENSE 文件首行即声明 "Apache License, Version 2.0"，与 README 第 44 行的说法一致。

#### 4.3.4 代码实践

1. **实践目标**：亲手确认仓库的「两部分」构成。
2. **操作步骤**：在仓库根目录执行只读命令：
   ```bash
   git ls-files
   ```
   然后把输出按 `hardware/` 和 `driver/` 两个前缀分组，分别数一数文件数量。
3. **需要观察的现象**：你会看到 `hardware/` 下 9 个 `.v` 文件、`driver/` 下 3 个文件（外加根目录 `README.md` 与 `LICENSE`），总计 14 个被 Git 跟踪的文件。
4. **预期结果**：验证本节给出的「9 + 3 + 2 = 14」分类，并确认仓库里**没有**任何 `Makefile`、构建脚本、`docs/`、testbench 或 CI 配置文件。
5. 本实践可在本地直接运行验证。

#### 4.3.5 小练习与答案

**练习 1**：`hardware/rtl/` 和 `driver/win32/` 分别对应「硬件设计」和「驱动程序」，请说出二者各自的职责。
**参考答案**：`hardware/rtl/` 用 Verilog 描述 NPU 芯片内部的计算、存储、互连等电路（硬件长什么样）；`driver/win32/` 是 Windows 内核驱动，负责让操作系统识别该 NPU 设备并向其下发任务、读取结果（系统怎么指挥硬件）。

**练习 2**：仓库根目录下除 `hardware/`、`driver/` 外还有哪些文件？它们分别是什么？
**参考答案**：还有 `README.md`（项目自述）和 `LICENSE`（Apache License 2.0 协议全文），共 2 个文件。

---

### 4.4 架构概览与「待确认」清单：承诺与现状的差距

#### 4.4.1 概念说明

一个值得养成的源码阅读习惯是：**把「文档怎么说」和「代码有什么」对照着看**。Mayoiuta 的 README 描述了一个相当完整的项目（带仿真环境、工具链、详细文档），但当你打开仓库，会发现很多东西其实并不存在。把这些差距诚实地标注出来，是后续学习不踩坑的前提。

#### 4.4.2 核心流程

对照检查的流程很简单：

1. 从 README 的 **Key Features** 与 **Architecture Overview** 里逐条摘出「承诺项」。
2. 在仓库里寻找对应文件 / 目录。
3. 找到 → 标「已提供」；找不到 → 标「待确认」。

#### 4.4.3 源码精读

先看 README 列出的关键特性中，涉及「配套基础设施」的几条：

[README.md:16-22](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/README.md#L16-L22) — 其中 Simulation Environment、Open-Source Toolchain（含 compiler / assembler / debugger）、Detailed Documentation 三条，是关于「项目周边工具与资料」的承诺。

再看架构概览：

[README.md:24-31](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/README.md#L24-L31) — 给出 CU/MU/IN/CU 四类核心模块的概念划分。这部分在仓库里有 `core/`、`memory/`、`top/` 等目录与之大致对应，属于「有据可查」。

把这些承诺与仓库现状对照，得到下表：

| README 承诺 / 描述 | 仓库现状 | 结论 |
| --- | --- | --- |
| Modular Design（模块化设计） | `hardware/rtl/` 下按 core/memory/power/sparse/control/top 分目录 | 已提供（有据） |
| Configurable Parameters（可配置参数） | 多个 `.v` 含 `parameter` 声明 | 已提供（有据） |
| Simulation Environment（仿真环境） | 无 testbench、无 Makefile、无仿真脚本 | **待确认（仓库未提供）** |
| Open-Source Toolchain（编译器/汇编器/调试器） | 无任何工具链源码 | **待确认（仓库未提供）** |
| Detailed Documentation（详细文档） | 仅有 README，无 `docs/` 目录 | **待确认（仓库未提供）** |
| Architecture Overview（CU/MU/IN/CU） | 目录划分大致对应，但 README 提到的子模块（如 `npu_controller`、`performance_monitor`）**待确认**是否已实现 | 部分待确认 |

> 提示：「待确认」不等于「项目是错的」。它只表示：**就当前 HEAD（`100706e`）的仓库内容而言，找不到对应证据**。这些项可能在未来的提交中补齐，也可能由作者在仓库外维护。学习时我们如实标注，不假装它们存在。

#### 4.4.4 代码实践

1. **实践目标**：亲手验证上表中三项「待确认」确实在仓库里找不到。
2. **操作步骤**：在仓库根目录执行：
   ```bash
   # 是否有 testbench / 仿真脚本？
   git ls-files | grep -iE 'tb|test|sim|bench'
   # 是否有工具链相关目录？
   git ls-files | grep -iE 'toolchain|compiler|assembler|debugger|asm|tools/'
   # 是否有文档目录？
   git ls-files | grep -iE '^docs/|\.md$'
   ```
3. **需要观察的现象**：前两条命令应当几乎无输出（找不到匹配），第三条命令只会命中 `README.md`。
4. **预期结果**：确认「仿真环境 / 工具链 / 详细文档」三项在仓库内均无对应文件，从而验证 4.4.3 表格中的「待确认」标注是实事求是的。
5. 本实践可在本地直接运行验证。

#### 4.4.5 小练习与答案

**练习 1**：README 提到的「开源工具链」具体包含哪三样东西？仓库里能找到吗？
**参考答案**：包含编译器（compiler）、汇编器（assembler）、调试器（debugger）三样。当前仓库中没有它们的源码，故标注为「待确认（仓库未提供）」。

**练习 2**：为什么我们在学习前要专门做一次「承诺 vs 现状」的对照？
**参考答案**：因为 README 描述的是一个相对完整的愿景，而仓库当前只包含 RTL 与驱动两类核心源码。提前把「承诺但缺失」的部分（仿真环境、工具链、详细文档）标注为待确认，可以避免学习时把不存在的东西当成已有，也让我们对项目的真实成熟度有准确判断。

---

## 5. 综合实践

把本讲学到的内容串起来，完成下面这个任务（这是本讲的主实践）：

1. **实践目标**：用一段话准确概括 Mayoiuta 是什么，并产出一份「待确认」清单。
2. **操作步骤**：
   - 重新通读 [README.md](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/README.md) 全文。
   - **任务一**：用 **200 字以内** 写一段「Mayoiuta 项目定位摘要」，要求覆盖：它是什么（开源 NPU 项目）、它的两部分源码构成（`hardware/rtl` + `driver/win32`）、它的四大愿景。
   - **任务二**：列出 README 中提到但仓库**尚未提供**的内容，逐项标注「待确认」。至少应包含：编译器 / 汇编器 / 调试器（工具链）、仿真环境、详细文档。
3. **需要观察的现象**：你会发现「愿景/特性」写得宏大，而仓库实际只交付了「RTL + 驱动」两块硬核源码，二者之间存在明显落差。
4. **预期结果**：得到一段 ≤200 字的精炼摘要，以及一份清晰的「待确认」清单，作为后续阅读源码时的心理基线。
5. 摘要内容因人而异，**待本地验证**；但「待确认」清单的条目应当与 4.4.3 表格一致。

## 6. 本讲小结

- **NPU** 是为神经网络运算（尤其乘加）量身打造的专用芯片，用「专用固定电路」换取比 CPU/GPU 更高的 AI 能效。
- Mayoiuta 是一个 **开源 NPU 项目**（Apache 2.0），README 给出 **四大愿景**（开放、灵活、可扩展、教育）与 **五条核心特性**（模块化、可配置参数、仿真环境、开源工具链、详细文档）。
- 仓库源码由 **两部分** 构成：`hardware/rtl/`（9 个 Verilog 文件，描述硬件电路）与 `driver/win32/`（3 个文件，Windows 内核驱动）。
- README 的架构概览把 NPU 分为 CU / MU / IN / CU 四类核心模块，与仓库目录大致对应。
- **诚实标注差距**：仿真环境、工具链（编译器/汇编器/调试器）、详细文档三项在当前仓库中均无对应文件，需标注为「待确认」。

## 7. 下一步学习建议

下一讲是 **u1-l2「仓库结构与源码导航」**，建议你：

1. 在阅读下一讲前，先自己用 `git ls-files` 把全部 14 个文件列一遍，建立第一手印象。
2. 重点留意 `hardware/rtl/` 下每个 `.v` 文件名（如 `pe_array.v`、`conv_engine.v`、`mem_ctl.v`），下一讲会把「文件路径 → 顶层模块名 → 功能域」整理成一张导航表。
3. 想提前建立全局观的话，可以先扫一眼 `hardware/rtl/top/npu_soc.v`（顶层 SoC），这会在 **u1-l3「顶层 SoC 架构：NPU_SOC」** 里详细拆解。

本讲覆盖的最小模块：NPU 概念与定位、四大愿景与五条核心特性、仓库两部分源码构成、架构概览与「待确认」清单。
