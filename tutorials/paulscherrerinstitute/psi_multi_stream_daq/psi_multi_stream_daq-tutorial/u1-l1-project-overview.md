# 项目总览与定位：psi_multi_stream_daq 是什么

## 1. 本讲目标

本讲是整个学习手册的第一篇，目标是让你在 **不看任何源码细节** 的情况下，先建立起对 `psi_multi_stream_daq` 这个项目的整体认识。读完本讲，你应当能够：

- 用一句话说清楚这个项目是做什么的、解决什么问题、用在哪里。
- 说出项目的维护者、作者、所采用的许可证（PSI HDL Library License）及其与 LGPL 的关系。
- 看懂项目的版本标签策略（major.minor.bugfix），并能根据 `Changelog.md` 说出最新稳定版本号和它带来的主要变更。
- 列出项目依赖哪些 `psi_*` 开源库，理解"为什么一个 FPGA IP 核还会有外部依赖"。

本讲只引用三个最顶层的项目说明文件：`README.md`、`Changelog.md`、`License.txt`。后续讲义才会进入 VHDL 源码、C 驱动和仿真测试平台。

## 2. 前置知识

在开始之前，建议你先了解以下几个基础概念。如果某些术语暂时不完全理解也没有关系，本讲不会深入到实现层面。

- **FPGA（现场可编程门阵列）**：一种可以通过代码（硬件描述语言）重新配置内部逻辑的芯片。本项目的"硬件部分"就是用 VHDL 写的、最终会综合到 FPGA 里的逻辑。
- **HDL / VHDL**：Hardware Description Language（硬件描述语言），VHDL 是其中一种。用 VHDL 写的代码描述的是数字电路，而不是普通意义上"逐行执行"的软件程序。
- **IP 核（IP Core）**：Intellectual Property Core，指可以复用的、封装好的硬件功能模块。本项目就是一个可以被集成到更大 FPGA 设计里的 IP 核。
- **SoC / PS（Processing System）**：很多现代 FPGA（如 Xilinx Zynq）内部除了可编程逻辑（PL），还包含一个硬核处理器（PS）。本项目采集到的数据最终会被写到处理器一侧的系统内存（如 DDR）中。
- **DMA（Direct Memory Access）**：直接内存访问。一种无需 CPU 逐字节搬移、由专用硬件把数据从一处搬到另一处（这里是从 FPGA 搬到系统内存）的机制。本项目核心能力之一就是把采集到的数据通过 DMA 写入内存。
- **AXI 总线**：ARM 提出的一种片上总线协议，广泛用于 FPGA 与处理器之间通信。本项目对外提供 AXI Slave（供 CPU 读写寄存器）和 AXI Master（供 FPGA 主动写内存）两套接口。
- **DAQ（Data Acquisition）**：数据采集。把传感器、ADC 等模拟或数字信号采集、数字化并存储下来的整个过程。

一句话定位：`psi_multi_stream_daq` 是一个 **多流数据采集 IP 核**，它把 FPGA 里多路并行数据流，经 DMA 写入系统内存。

## 3. 本讲源码地图

本讲只涉及项目最顶层的三个说明性文件，它们都是项目根目录下的纯文本：

| 文件 | 作用 | 本讲用它来回答什么 |
| --- | --- | --- |
| [README.md](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/README.md) | 项目的"门面"：维护者、作者、许可证、文档、标签策略、依赖、仿真入口 | 这是什么项目？谁维护？依赖什么？怎么跑仿真？ |
| [Changelog.md](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/Changelog.md) | 版本变更日志，从 0.1.0 到最新 1.2.3 | 项目经历了哪些版本？每个版本改了什么？最新稳定版是什么？ |
| [License.txt](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/License.txt) | PSI HDL Library License 全文 | 我能不能商用？能不能用到 FPGA 比特流里？与 LGPL 是什么关系？ |

此外，项目根目录还包含这些目录（本讲只点名，详细讲解见后续讲义）：

- `hdl/` —— VHDL 硬件实现源码（IP 核本体）。
- `driver/` —— 嵌入式 C 驱动，供处理器一侧控制 IP 核、读取数据。
- `tb/` —— VHDL 仿真测试平台（testbench）。
- `sim/` —— 仿真流程脚本（Tcl）。
- `scripts/` —— 依赖解析、CI 流程等 Python 脚本。
- `doc/` —— 功能说明文档（PDF / DOCX 等）。

## 4. 核心概念与源码讲解

### 4.1 General Information（项目门面信息）

#### 4.1.1 概念说明

任何成熟的开源项目都会有一个 `README.md` 作为入口，它回答三个最基本的问题：**这是什么、谁负责、按什么协议发布**。本项目的 `README.md` 第一节就叫 `General Information`，集中给出了维护者、作者、许可证、文档、标签策略这些"身份证"信息。

对一个 FPGA IP 核来说，这部分信息尤其重要，原因有二：

1. **FPGA 项目天然依赖生态**。一个 IP 核通常不是孤立存在的，它会引用其他 IP 核（这里依赖 `psi_common`、`psi_tb`），所以"谁维护、版本怎么演进"直接决定了你能不能稳定地把它集成进自己的工程。
2. **许可证对硬件很敏感**。软件世界的 LGPL 在 FPGA / 比特流语境下会有歧义，所以本项目专门使用了一个"LGPL + 硬件例外"的许可证，这一点我们到 4.1.3 再细说。

#### 4.1.2 核心流程

阅读一个陌生项目的 `README`，可以按下面这个固定套路快速定位关键信息：

1. **定位与用途**：先找项目名和一句话描述，判断它属于哪一类（这里是 DAQ IP 核）。
2. **维护者 / 作者**：决定遇到问题时该联系谁、向谁提 issue。注意"维护者"和"原作者"不一定是同一人——本项目就是如此。
3. **许可证**：决定你能不能、以及如何使用它。
4. **文档**：找到深入资料（这里指向 `doc/psi_multi_stream_daq.pdf`）。
5. **标签策略**：看懂版本号的含义，判断升级风险。
6. **依赖**：知道还需要拉哪些别的仓库。

#### 4.1.3 源码精读

`README.md` 开头的 `General Information` 区块集中了维护者、作者与许可证：

[README.md:L1-L23](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/README.md#L1-L23) —— 这一段定义了项目维护者（Daniele Felici）、作者（Oliver Bründler）、许可证（PSI HDL Library License）、详细文档链接（`doc/psi_multi_stream_daq.pdf`）、标签策略，以及指向 Changelog 的入口。注意第 3-7 行，**维护者与作者是不同的人**：

```markdown
## Maintainer
Daniele Felici [daniele.felici@psi.ch]

## Author
Oliver Bründler [oli.bruendler@gmx.ch]
```

许可证部分（第 9-10 行）声明本项目使用 **PSI HDL Library License**，并说明它是 LGPL 加上一些额外例外。其完整条款在 `License.txt` 中，我们后面会读。

[License.txt:L1-L13](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/License.txt#L1-L13) —— PSI HDL Library License 的标题、版权声明（Copyright (c) 1998-2018 Oliver Bründler 等）以及核心条款。核心条款（第 11 行）明确：本库在 GNU 库通用公共许可证（LGPL，版本 2 或更高）的条款下发布。

真正让这个许可证"对 FPGA 友好"的是第 15 行起的 **EXCEPTION NOTICE（例外通知）**：

[License.txt:L15-L21](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/License.txt#L15-L21) —— 这段例外条款允许你"以自己的条款"使用、复制、链接、修改和分发 **基于本库的二进制形式产物或包含二进制的硬件**。最关键的是第 19 行明确：**术语"二进制"明确包含器件配置文件，例如 FPGA 比特流（bitstream）或 flash 镜像**。

> **直觉理解**：纯 LGPL 在 FPGA 世界有歧义——有人担心把 LGPL IP 综合进自己的比特流后，会不会被迫公开自己整个设计的源码。这个例外条款正是为了打消这种顾虑：**你可以自由地把本 IP 核编译进你的 FPGA 比特流并闭源分发该比特流**，但如果你修改了 IP 核本身的源码并分发，仍需遵循 LGPL 对源码的要求。

`README.md` 还定义了版本标签策略（第 15-20 行）：

[README.md:L15-L20](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/README.md#L15-L20) —— 标签采用 `major.minor.bugfix` 三段式：

- 改动**不完全向下兼容** → 增加 **major**（主版本号）。
- 新增功能 → 增加 **minor**（次版本号）。
- 仅修 bug、无功能改动 → 增加 **bugfix**（修订号）。

这个策略在后续读 Changelog 时会反复用到：看到一个版本号，你就能大致判断升级它的风险等级。

#### 4.1.4 代码实践

**实践目标**：亲手从源码中提取项目"身份信息"，建立对维护者/作者/许可证/文档入口的肌肉记忆。

**操作步骤**：

1. 打开 [README.md:L1-L23](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/README.md#L1-L23)。
2. 找到 `## Maintainer`、`## Author`、`## License`、`## Detailed Documentation` 四个小节。
3. 打开 [License.txt:L15-L21](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/License.txt#L15-L21)，找到关于"binary"和"FPGA-bitstreams"的原文。

**需要观察的现象**：

- 维护者与作者是否为同一人。
- 许可证正文里"FPGA-bitstreams"这个单词出现在哪一条、说的是"包含"还是"排除"在 binary 之外。

**预期结果**：维护者是 Daniele Felici，作者是 Oliver Bründler（非同一人）；License 第 19 行明确把 FPGA 比特流 **包含** 进 binary 的定义，因此可以闭源分发包含本 IP 的比特流。

#### 4.1.5 小练习与答案

**练习 1**：为什么本项目要专门定义一个"PSI HDL Library License"，而不是直接用标准的 LGPL？

> **参考答案**：因为标准 LGPL 主要面向软件，在 FPGA / 硬件语境下有歧义（特别是"链接""二进制"等概念）。PSI HDL Library License 在 LGPL 基础上加了 EXCEPTION NOTICE，明确允许把本库编译进 FPGA 比特流或 flash 镜像后闭源分发，从而让硬件工程师可以放心集成。

**练习 2**：根据标签策略，如果一个新版本把 `1.2.3` 变成了 `2.0.0`，你能推断出什么？

> **参考答案**：major 号从 1 跳到 2，说明本次改动 **不完全向下兼容**。升级时需要仔细核对接口、寄存器映射或驱动 API 是否有破坏性变更，不能直接平滑替换。

---

### 4.2 Dependencies 章节（依赖与目录约定）

#### 4.2.1 概念说明

一个 FPGA IP 核很少是"自包含"的。本项目复用了 Paul Scherrer Institute（PSI）维护的一系列公共库：用于通用电路的 `psi_common`、用于仿真验证的 `psi_tb`、以及用于自动化仿真的 `PsiSim` 工具链。`README.md` 中专门有一段被特殊注释包裹的 `Dependencies` 章节，这段注释不是给人看的提示，而是 **会被脚本自动解析** 的——这是本项目依赖管理的一个关键设计。

#### 4.2.2 核心流程

依赖管理的整体流程是：

1. `README.md` 用一对 HTML 注释标记出"可解析的依赖区块"。
2. 一个 Python 脚本 `scripts/dependencies.py` 读取这段区块，自动把依赖仓库按要求的目录结构克隆到本地。
3. 仿真与综合工具再按这个固定的目录结构去找到 `psi_common`、`psi_tb` 等依赖。
4. 用户也可以选择直接使用一个"全家桶"仓库 `psi_fpga_all`，它把所有相关仓库以子模块形式组织好。

这意味着：**目录结构本身是契约的一部分**。文件夹名字必须精确匹配，否则脚本和工具都找不到依赖。

#### 4.2.3 源码精读

`README.md` 的 Dependencies 区块如下：

[README.md:L27-L40](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/README.md#L27-L40) —— 注意第 25 行的特殊注释 `<!-- DO NOT CHANGE FORMAT: this section is parsed to resolve dependencies -->` 和第 40 行的 `<!-- END OF PARSED SECTION -->`。脚本只会解析这两个标记之间的内容，所以这段的格式是"机器契约"，不能随意改格式。

第 33-38 行列出了三类依赖：

- **TCL**：`PsiSim`（≥ 2.1.0），仅开发（仿真）时需要。
- **VHDL**：`psi_common`（≥ 3.0.0）——运行时必需的硬件公共库；`psi_tb`（≥ 3.0.0）——仅开发（仿真）时需要的测试公共库。
- **psi_multi_stream_daq** 本身。

注意区分两类依赖：

| 标记 | 含义 | 例子 |
| --- | --- | --- |
| 运行时必需 | 综合/上板时必须存在 | `psi_common` |
| "for development only" | 仅在跑仿真、写测试时需要，上板不需要 | `PsiSim`、`psi_tb` |

紧接着，`README.md` 给出了用脚本拉取依赖的入口：

[README.md:L42-L48](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/README.md#L42-L48) —— 说明可以用 `python dependencies.py -help` 查看依赖解析脚本用法，并提示运行该脚本需要先安装 [PsiFpgaLibDependencies](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies) 这个 Python 包。

最后，`README.md` 还给出了仿真入口：

[README.md:L50-L58](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/README.md#L50-L58) —— 在 Modelsim 中进入 `sim` 目录后执行 `source ./run.tcl` 即可跑回归测试。这是本项目的"一键验证"入口，详细机制见后续讲义 u1-l2 与 u5-l1。

#### 4.2.4 代码实践

**实践目标**：理解依赖区块是如何被结构化标注的，并区分"运行时依赖"与"仅开发依赖"。

**操作步骤**：

1. 打开 [README.md:L27-L40](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/README.md#L27-L40)。
2. 在源码里圈出第 25 行和第 40 行的两条 HTML 注释。
3. 为三个依赖（`PsiSim`、`psi_common`、`psi_tb`）各列一个表格行：名字、最低版本号、是否"for development only"。
4. 打开 `scripts/` 目录确认 `dependencies.py` 确实存在（用 `ls scripts/` 即可，不必运行）。

**需要观察的现象**：

- 哪些依赖带版本号下限（≥），哪些不带。
- 哪些依赖后面跟了 "for development only"。

**预期结果**：`PsiSim`（≥ 2.1.0，仅开发）、`psi_common`（≥ 3.0.0，运行时必需）、`psi_tb`（≥ 3.0.0，仅开发）。`psi_multi_stream_daq` 自身不带版本号下限（它就是本项目）。

> 注：本实践是"源码阅读型实践"，不需要真的运行脚本；脚本运行涉及外部仓库下载，属于后续讲义 u1-l2 的内容。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Dependencies` 区块要用一对 HTML 注释包起来，并写明 "DO NOT CHANGE FORMAT"？

> **参考答案**：因为这段内容会被 `scripts/dependencies.py` 自动解析，用来克隆依赖仓库。格式是机器契约，一旦改动（比如缩进、列表符号），脚本可能解析失败，导致依赖拉不下来。HTML 注释对人类读者是隐藏的提示，对脚本是边界标记。

**练习 2**：如果你只想把本 IP 核综合到 FPGA 上板运行，**不**做任何仿真，你需要安装 `psi_tb` 吗？

> **参考答案**：不需要。`psi_tb` 被标注为 "for development only"，只在仿真测试时使用。上板综合只需 `psi_common`（运行时必需）。但出于工程稳健性，通常建议仿真也一并跑通。

---

### 4.3 Changelog 版本条目（版本演进）

#### 4.3.1 概念说明

`Changelog.md` 记录了项目从早期内部版本到当前开源版本的演进历史。它是你判断"这个版本能不能用、升级会不会踩坑"的第一手资料。结合 4.1 学到的标签策略（major.minor.bugfix），你能从每一条版本号直接读出变更的性质。

#### 4.3.2 核心流程

阅读 Changelog 的推荐顺序是 **从最新往回看**：先看最新稳定版（1.2.3）做了什么，再按需回溯。本项目 Changelog 的小节结构是：

1. 版本号（如 `## 1.2.3`）。
2. 分类（`Features` / `Changes` / `Bugfixes` / `Doc`）。
3. 具体条目（bullet 列表）。

由于本项目"首次开源发布"是 1.2.0，更早的 0.x / 1.0 / 1.1 版本属于开源前的内部历史，理解时可以把注意力主要放在 1.2.0 及之后。

#### 4.3.3 源码精读

最新稳定版本是 **1.2.3**：

[Changelog.md:L1-L3](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/Changelog.md#L1-L3) —— 1.2.3 只有 `Doc` 一类改动：更换了仓库维护者（"Changed repository mantainer"，原文拼写如此）。这正好呼应了 4.1 中"维护者是 Daniele Felici"——这次维护者变更记录在这里。

往前一版 1.2.2 是一个工具链适配的 bugfix：

[Changelog.md:L5-L7](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/Changelog.md#L5-L7) —— 针对只有一个 stream 的场景，绕过了 ISE 工具"把存储器实现成触发器（FF）"的问题。这是一个针对特定综合工具（Xilinx ISE）的工程性修复。

1.2.1 是一个内容更丰富的修复版本：

[Changelog.md:L9-L15](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/Changelog.md#L9-L15) —— 涵盖：优化 input FIFO 与 DMA 之间的时序、修复驱动中的数据解包（unwrapping）、修正若干文档问题、让驱动兼容 C++（"Made driver C++ tolerant"）、让代码在仅一个 stream 时也能正常工作。

> 注：原文第 15 行末尾有 "git" 一词粘连（"Made code working for only one streamgit"），这是仓库历史中遗留的笔误，阅读时按 "Made code working for only one stream" 理解即可——本讲义不修改源码，仅如实说明。

开源首版的里程碑是 1.2.0：

[Changelog.md:L17-L19](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/Changelog.md#L17-L19) —— 明确写着 "First Open Source Release (older versions not kept in history)"，即首次开源发布，更早版本不保留在历史中；并添加了许可证与版权头。这一条解释了为什么 1.2.0 之前的条目（1.1.0、1.0.0、0.x）信息很少。

为了完整，把更早的内部版本也列出：

[Changelog.md:L21-L38](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/Changelog.md#L21-L38) —— 1.1.0（新增依赖解析脚本、改用 `psi_common` 的 AXI Slave）、1.0.0（首个在硬件上测试通过的版本）、0.9.0（加入 AXI 版本、Tosca 版本拆分到独立仓库）、0.2.0（开源后按新库版本更新）、0.1.0（准备首次硬件测试）。

把版本号和标签策略对照起来看，可以得到下面这张演进表：

| 版本 | 类型（按标签策略推断） | 关键事件 |
| --- | --- | --- |
| 0.1.0 / 0.2.0 | 早期内部版 | 首次硬件测试、开源后库对齐 |
| 0.9.0 | minor（新功能） | 引入 AXI 版本；Tosca 版拆分 |
| 1.0.0 | major | 首个在硬件上测试通过的里程碑 |
| 1.1.0 | minor（新功能） | 加入依赖解析脚本、换用 psi_common 的 AXI Slave |
| 1.2.0 | minor（新功能） | **首次开源发布**，加许可证/版权头 |
| 1.2.1 | bugfix | 时序优化、驱动解包修复、C++ 兼容、单流支持 |
| 1.2.2 | bugfix | ISE 工具单流存储器实现规避 |
| 1.2.3 | doc | 更换仓库维护者（**当前最新稳定版**） |

#### 4.3.4 代码实践

**实践目标**：亲手完成本讲规格要求的实践——用一段话总结本 IP 核解决什么问题、依赖哪些 psi_* 库，并指出最新稳定版本号与对应的主要变更。

**操作步骤**：

1. 重读 [README.md:L27-L40](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/README.md#L27-L40)（依赖）和 [Changelog.md:L1-L19](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/Changelog.md#L1-L19)（最新三个版本）。
2. 在自己的笔记里写一段话（建议 80-150 字），覆盖三点：(a) 项目解决什么问题；(b) 依赖哪些 psi_* 库；(c) 最新稳定版及其主要变更。
3. 用标签策略自检：你写下的最新版本属于 major / minor / bugfix / doc 中的哪一类？升级它的风险高不高？

**需要观察的现象**：

- 最新版本号是 1.2.3 还是别的。
- 1.2.3 的条目里没有 `Features` / `Bugfixes`，只有 `Doc`。

**预期结果**：最新稳定版为 **1.2.3**，其唯一变更是文档类（更换仓库维护者），无功能/接口改动，因此从 1.2.2 升级到 1.2.3 风险极低。

**参考答案示例（你可以对照自己的措辞）**：

> `psi_multi_stream_daq` 是 PSI 开发的多流数据采集 FPGA IP 核，把多路并行数据流经 DMA 写入系统内存。它运行时依赖 `psi_common`（≥ 3.0.0），仿真开发还需 `psi_tb`（≥ 3.0.0）与 `PsiSim`（≥ 2.1.0）。当前最新稳定版为 **1.2.3**，相对 1.2.2 仅做了文档类变更（更换仓库维护者），无功能或接口改动。

> 注：本实践为"源码阅读 + 总结型实践"，不需要运行任何命令。

#### 4.3.5 小练习与答案

**练习 1**：如果你当前在生产环境用的是 1.2.0，想升级到 1.2.3，中间跨越了哪些版本？其中哪一次升级最需要谨慎？

> **参考答案**：跨越 1.2.1、1.2.2、1.2.3。最需要谨慎的是 **1.2.1**，因为它修改了驱动中的数据解包逻辑（"Fix data unwrapping in driver"）并优化了 input FIFO 与 DMA 之间的时序——这两项都可能影响实际采集数据的正确性，升级后需要重新验证数据通路。

**练习 2**：为什么 1.2.0 之前的版本（1.1.0、1.0.0、0.x）在 Changelog 里信息这么少？

> **参考答案**：因为 1.2.0 是 "First Open Source Release"，开源前的版本历史没有保留（"older versions not kept in history"）。早期条目只是遗留的简短记录，不代表当时没有改动，而是没把细节公开保留下来。

**练习 3**：结合标签策略判断，1.2.1 → 1.2.2 → 1.2.3 这三次版本号变化，分别属于哪一类升级？

> **参考答案**：三次都只动了 bugfix 位（最后一位），都属于"仅修 bug / 文档、无功能改动"的修订级升级。其中 1.2.3 连 bug 都没修，只是文档（维护者）变更，是风险最低的一类。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个"项目速览卡片"任务。假设你要向一位刚加入团队的同事介绍这个项目，请基于本讲阅读的三个文件，填写下面这张卡片（建议另存为一份 Markdown 笔记）：

```
# psi_multi_stream_daq 速览卡片

## 一句话定位
（用一句话说明它是什么、做什么）

## 身份信息
- 维护者：
- 作者：
- 许可证：
- 详细文档入口：

## 许可证要点
- 能否闭源分发包含本 IP 的 FPGA 比特流？依据是 License.txt 第几行？

## 依赖
- 运行时必需的 VHDL 库及版本：
- 仅仿真开发需要的库/工具及版本：

## 版本
- 当前最新稳定版：
- 该版本变更性质（major/minor/bugfix/doc）：
- 首次开源发布的版本号：
```

填写时的参考依据：

- "一句话定位"可参考本讲第 1、2 节。
- "身份信息"全部来自 [README.md:L1-L23](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/README.md#L1-L23)。
- "许可证要点"来自 [License.txt:L15-L21](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/License.txt#L15-L21)，重点是第 19 行。
- "依赖"来自 [README.md:L27-L40](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/README.md#L27-L40)。
- "版本"来自 [Changelog.md:L1-L19](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/Changelog.md#L1-L19)。

**自检标准**：如果你的卡片能独立回答"这是什么、谁维护、按什么协议、依赖什么、最新版是什么"这五个问题，本讲的目标就达成了。

## 6. 本讲小结

- `psi_multi_stream_daq` 是 Paul Scherrer Institute 开发的 **多流数据采集 FPGA IP 核**，核心能力是把多路并行数据流经 DMA 写入系统内存（如 DDR）。
- 项目的 **维护者** 是 Daniele Felici，**作者** 是 Oliver Bründler，二者不是同一人；变更维护者这件事记录在 1.2.3 的 Changelog 里。
- 许可证是 **PSI HDL Library License**——LGPL 加上一个硬件例外，明确允许把本库编译进 FPGA 比特流后闭源分发（`License.txt` 第 19 行）。
- 版本号采用 `major.minor.bugfix` 三段式语义：不兼容改动动 major，新功能动 minor，仅修 bug 动 bugfix。
- 依赖通过 `README.md` 中一段 **可被脚本解析** 的区块声明：运行时需要 `psi_common`（≥ 3.0.0），仿真开发还需要 `psi_tb`（≥ 3.0.0）与 `PsiSim`（≥ 2.1.0）。
- 当前最新稳定版本是 **1.2.3**（仅文档类变更）；首次开源发布是 1.2.0。

## 7. 下一步学习建议

本讲只看了项目"门面"。接下来建议：

- **下一篇（u1-l2）**：进入 [仓库结构与仿真/构建运行方式](u1-l2-repo-and-simulation.md)，了解 `hdl/driver/tb/sim/scripts` 各目录的职责，以及用 `dependencies.py`、`sim/run.tcl`、`ciFlow.py` 跑回归仿真的完整流程。
- **u1-l3**：解析顶层实体 `psi_ms_daq_axi` 的 generic 与端口，第一次接触真实 VHDL 源码。
- **u1-l4**：通过 C 驱动 `driver/psi_ms_daq.h` 的示例代码，从软件视角快速上手"初始化→配置→读数据"的完整调用链。

如果想在阅读后续讲义前先建立更直观的功能理解，可以翻阅 `README.md` 指向的详细文档 [doc/psi_multi_stream_daq.pdf](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/doc/psi_multi_stream_daq.pdf)（这是项目自带的功能说明，不是本讲强制要求）。
