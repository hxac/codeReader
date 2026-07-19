# 项目概览与定位

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标只有一个：**让你在还没读任何源码之前，先弄清楚 PsiSim 到底是什么、为谁解决什么问题**。

读完本讲你应该能够：

1. 用一两句话向同事解释 PsiSim 是什么、能用来做什么。
2. 说出 PsiSim 支持的三种仿真器（Modelsim / GHDL / Vivado）以及它们各自的优缺点和适用场景。
3. 认识 PsiSim 的作者、维护者、所属机构和许可证类型，理解它的版本号（major.minor.bugfix）策略。

本讲**不会**深入 TCL 代码细节，那是后面进阶和专家层讲义的任务。本讲只读 `README.md`、`Changelog.md`、`License.txt` 三个非代码文件，并在必要处瞄一眼核心源码 `PsiSim.tcl` 的整体外观。

## 2. 前置知识

在开始之前，你最好对下面几个概念有个大概印象。不熟悉也没关系，我们会在用到时解释。

- **VHDL**：一种硬件描述语言（Hardware Description Language），用来描述数字电路（比如 FPGA / ASIC 里的逻辑）。PsiSim 处理的“源文件”主要就是 `.vhd` 这类 VHDL 文件。
- **Testbench（测试台 / tb）**：一段专门用来“测试”另一段 VHDL 代码的 VHDL 代码。它给被测电路喂入激励信号，然后检查输出是否正确。
- **Regression test（回归测试）**：把一整套测试台批量跑一遍，确保代码改动后“以前能跑过的测试现在还能跑过”。PsiSim 的核心目的就是让这种批量回归测试变得简单。
- **Simulator（仿真器）**：执行 VHDL 代码、模拟电路行为的工具软件。Modelsim、GHDL、Vivado Simulator 都是仿真器。
- **TCL（Tool Command Language）**：一种脚本语言。很多 EDA（电子设计自动化）工具（包括 Modelsim）都把 TCL 作为内置脚本语言。PsiSim 本身就是用 TCL 写的。

> 小提示：如果你完全没接触过 FPGA / VHDL，可以把 PsiSim 类比成“前端项目里的测试运行器（比如 Jest）”——它本身不写测试，而是负责把一堆测试组织起来、编译、跑、判断通过与否。

## 3. 本讲源码地图

PsiSim 仓库非常精简，根目录只有 6 个文件，没有 `src/`、`examples/` 之类子目录。本讲涉及的关键文件如下：

| 文件 | 行数 | 作用 |
|------|------|------|
| `README.md` | 169 | 项目说明书：定位、特性、用法示例、作者、许可证、版本号策略。**本讲的主要依据。** |
| `Changelog.md` | 109 | 版本变更记录，能看到三种仿真器是何时加入的、当前版本号是多少。 |
| `License.txt` | 22 | 许可证全文（PSI HDL Library License）。 |
| `PsiSim.tcl` | 966 | 唯一的核心源码文件，整个框架都在这一个文件里。本讲只看它的“外观”和分区。 |
| `CommandRef.md` | 543 | 命令参考手册。本讲暂不深入，留给后续讲义。 |
| `LGPL2_1.txt` | — | LGPL 许可证全文（License.txt 中引用的基础协议）。 |

一个关键认知：**PsiSim 的全部核心逻辑都集中在一个 966 行的 `PsiSim.tcl` 文件里**。这既是它的优点（极易阅读、无依赖），也意味着后续所有讲义都会反复回到这一个文件。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 PsiSim 的定位与解决的问题**：它是什么、为什么需要它。
- **4.2 三种仿真器对比**：Modelsim / GHDL / Vivado 的特点与取舍。
- **4.3 许可证与版本号策略**：谁能用、怎么用、版本号怎么读。

### 4.1 PsiSim 的定位与解决的问题

#### 4.1.1 概念说明

PsiSim 是**保罗谢尔研究所（Paul Scherrer Institute, PSI，瑞士）**开发的一个 **TCL 框架**，用来快速、简单地创建 **VHDL 回归测试**。

它的核心卖点用一句话概括就是：**用 TCL 脚本来描述和运行仿真，让仿真流程对版本控制（Git 等）友好、易于合并**。

为什么这一点重要？因为在 PsiSim 出现之前，大家通常用两种方式管理仿真：

1. **Modelsim 自带的工程文件（`.mpf` / 图形化工程）**：这是二进制或半结构化文件，里面记录了文件列表、编译顺序、各种绝对路径。两个人同时改工程、再合并，几乎必然冲突。
2. **Vivado 工程**：同样的问题——工程文件庞大、包含大量机器生成的杂乱内容，极难用 Git 管理。

PsiSim 的做法是：**把“要仿真哪些文件、有哪些测试台、每个测试台要跑几组参数”全部用纯文本 TCL 脚本描述**。纯文本脚本天然适合 Git：diff 清晰、合并容易、可 review。

> 类比：与其把一个 IDE 的工程配置文件提交到 Git，不如写一个 `Makefile` 或 `build.sh`——PsiSim 就是仿真领域的那个“`Makefile`”。

#### 4.1.2 核心流程

PsiSim 的典型工作流是**两文件模式**（two-file workflow）：

1. **`config.tcl`（描述文件）**：声明有哪些库、哪些源文件、哪些测试台、每个测试台跑几组参数（generics）、可选的前后置脚本。它只“描述”，不“执行”。
2. **`run.tcl`（执行文件）**：加载 PsiSim → 初始化 → `source` 配置文件 → 编译 → 跑测试 → 检查错误。

之所以拆成两个文件，是为了**支持嵌套**：每个库可以有自己的 `config.tcl` + `run.tcl`；项目级的 `run.tcl` 可以 `source` 各个库的 `config.tcl`，把所有测试汇总成一次大回归。本讲只建立这个直觉，具体命令留到单元 1 的第 3 讲。

#### 4.1.3 源码精读

**① 项目定位——README 的 Features 段落**

[README.md:19-26](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L19-L26) 直接点明了 PsiSim 是什么、为什么比 Modelsim/Vivado 工程文件更好：

- 第 19–22 行说明它能做什么：单条命令编译文件或文件组、轻松跑完整回归、自动解析测试台结果。
- 第 23–24 行是核心卖点：**与 Modelsim 工程和 Vivado 工程相比，用这个 TCL 包写出的仿真脚本对版本控制友好、易于合并**。
- 第 26 行点出它能跨三种仿真器跑同一套仿真。

**② 两文件工作流——README 的 Usage 段落**

[README.md:33-45](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L33-L45) 解释了为什么是两个文件，以及“嵌套”的来源。注意第 37–40 行对两个文件职责的划分，和第 42–45 行对“库级 / 项目级嵌套”的说明。

**③ 核心源码的外观——PsiSim.tcl 头部**

[PsiSim.tcl:1-14](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L1-L14) 是整个框架的“门面”：第 1–5 行是版权声明（PSI，2018，作者 Oliver Bruendler），第 9–11 行是一句话功能说明，第 14 行 `namespace eval psi::sim {` 则表明所有功能都封装在 `psi::sim` 这个 TCL 命名空间里——这也是后面所有讲义里你会反复看到的 `psi::sim::*` 命令的来源。

#### 4.1.4 代码实践

**实践目标**：亲手确认 PsiSim 的核心卖点确实写在 README 里，而不是我们编造的。

**操作步骤**：

1. 打开 `README.md`，找到 `## Features` 段落（大约第 18 行起）。
2. 阅读第 19–26 行。
3. 打开任意一个你见过的 Modelsim 工程（`.mpf`）或 Vivado 工程文件，或者回忆它的样子。

**需要观察的现象**：

- README 第 23–24 行明确把 PsiSim 脚本和 Modelsim/Vivado 工程文件做了对比。
- Modelsim/Vivado 工程文件通常是大量机器生成的内容、夹杂绝对路径，而 PsiSim 的配置（如 README 第 56–136 行的 `config.tcl` 示例）是干净、可读的纯文本。

**预期结果**：你能用自己的话写出“为什么 PsiSim 脚本比工程文件更适合 Git”。参考答案见下面的练习。

> 说明：本实践不需要运行任何命令，是“源码阅读型实践”，目的是建立直觉。

#### 4.1.5 小练习与答案

**练习 1**：PsiSim 解决的核心问题是什么？请用一句话回答。

> **参考答案**：PsiSim 用纯文本 TCL 脚本来描述和运行 VHDL 仿真，使仿真流程对版本控制友好、易于合并，克服了 Modelsim/Vivado 工程文件难以用 Git 管理的问题。

**练习 2**：PsiSim 的典型工作流为什么拆成 `config.tcl` 和 `run.tcl` 两个文件？

> **参考答案**：为了让“描述”和“执行”分离，从而支持嵌套——每个库可以有自己的配置和运行脚本，项目级运行脚本可以 `source` 各库的配置文件，把所有测试汇总成一次回归。

---

### 4.2 三种仿真器对比 (Modelsim / GHDL / Vivado)

#### 4.2.1 概念说明

PsiSim 最大的特色之一是：**同一套仿真脚本，可以切换底层仿真器来跑**。它目前支持三种：

| 仿真器 | 性质 | 在 PsiSim 中的地位 | 备注 |
|--------|------|-------------------|------|
| **Modelsim** | 商业（Mentor/Siemens） | **默认仿真器** | PsiSim 最早、支持最完整的目标；可以直接在 Modelsim 控制台里跑 TCL 脚本。 |
| **GHDL** | 开源 | 通过 `init -ghdl` 启用 | 速度快、VHDL-2008 支持比 Vivado 好；但需要独立的 TCL 解释器（如 ActiveTCL）来运行脚本。 |
| **Vivado Simulator** | 商业（AMD/Xilinx，随 Vivado 附带） | 通过 `init -vivado` 启用 | 速度较慢、对 VHDL-2008 支持有限；仅在其他两个都不方便时才建议使用。 |

> 名词解释：
> - **VHDL-2008**：VHDL 语言的一个较新标准版本。很多现代测试台（包括 PSI 自家库里的）都用了 2008 的语法。仿真器对它的支持程度差异很大。
> - **独立 TCL 解释器（standalone TCL interpreter）**：像 ActiveTCL 这种独立安装的 TCL 运行环境。Modelsim 自带 TCL 解释器，所以跑 Modelsim 时直接在它的控制台里执行脚本即可；GHDL 不自带，所以要在系统的独立 TCL 里跑。

#### 4.2.2 核心流程

切换仿真器的入口是 PsiSim 的 `init` 命令：

- `init`（不带参数）→ 默认用 **Modelsim**。
- `init -ghdl` → 用 **GHDL**。
- `init -vivado` → 用 **Vivado Simulator**。

`init` 内部会把选择记到一个叫 `Simulator` 的状态变量里，之后框架里所有与仿真器打交道的代码（称为 SAL，模拟器抽象层，见单元 3）都会根据这个变量走不同分支。这就是“同一套脚本，多仿真器”的实现基础——本讲只需知道这个开关的存在。

#### 4.2.3 源码精读

**① README 对三种仿真器的明确说明**

[README.md:26](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L26) 一句话点明支持范围：“该框架允许使用 Modelsim、GHDL 或 Vivado 中的任意一种来运行相同的仿真。”

**② 关于 Vivado 的特别警告**

[README.md:28-31](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L28-L31) 给出了非常直接的工程建议：Vivado 不支持很多 VHDL-2008 语句、速度慢；只在前两个都不行时才用；GHDL 更快且支持更多 VHDL-2008。第 31 行还特别指出 PSI 库里很多测试台用了 VHDL 2008，所以无法用 Vivado 执行。

**③ 从 Changelog 看三种仿真器的加入时间**

[Changelog.md:71-74](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/Changelog.md#L71-L74)（1.4.0）记录了 **GHDL** 的加入，并说明 GHDL 需要把目录加入系统 PATH、脚本必须由独立 TCL 解释器执行。

[Changelog.md:36-38](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/Changelog.md#L36-L38)（2.2.0）记录了 **Vivado** 仿真器的加入。

> 时间线直觉：Modelsim 是最初唯一支持的目标（1.x 之前），GHDL 在 1.4.0 加入，Vivado 直到 2.2.0 才加入。这也解释了为什么 Modelsim 是默认、支持最完整的那个。

**④ 源码里的仿真器开关**

[PsiSim.tcl:347-367](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L347-L367) 是 `init` 命令的实现：第 354 行先把 `Simulator` 默认设为 `"Modelsim"`，第 358–361 行根据 `-ghdl` / `-vivado` 参数改写它。这就是 4.2.2 里那个“开关”的真实代码。

#### 4.2.4 代码实践

**实践目标**：阅读 README 与 Changelog，自己归纳出三种仿真器的特点，而不是死记本讲给出的表格。

**操作步骤**：

1. 读 `README.md` 第 26 行和第 28–31 行。
2. 读 `Changelog.md` 中 `## 1.4.0`（GHDL 加入）和 `## 2.2.0`（Vivado 加入）两段。
3. 自己画一张表，列出：仿真器名 / 商业还是开源 / 默认与否 / README 或 Changelog 提到的注意事项。

**需要观察的现象**：

- 三种仿真器在 README/Changelog 中被提到的次数和“附加说明”数量差异很大——Modelsim 几乎不需要附加说明（它是默认基准），而 Vivado 有一整段警告。
- GHDL 的注意事项集中在“运行环境”（需要独立 TCL、需要 PATH）。

**预期结果**：你应能得到一张与本讲 4.2.1 表格内容相近的对比表，并能解释“为什么 PSI 内部更倾向用 GHDL 而非 Vivado”。

> 说明：本实践为文档阅读型实践，无需运行仿真器。

#### 4.2.5 小练习与答案

**练习 1**：要在 GHDL 下跑仿真，`run.tcl` 里的 `init` 应该怎么写？相比 Modelsim 默认情况，还需要额外满足什么环境条件？

> **参考答案**：应写成 `init -ghdl`。额外条件：GHDL 的目录必须加入系统 PATH，且脚本必须由独立的 TCL 解释器（如 ActiveTCL）来执行，因为 GHDL 不像 Modelsim 那样自带 TCL 控制台。

**练习 2**：为什么 README 建议“只在其他选项都不可行时才用 Vivado”？

> **参考答案**：因为 Vivado Simulator 速度较慢，且不支持很多 VHDL-2008 语句；而 PSI 库里大量测试台使用了 VHDL 2008，导致它们根本无法在 Vivado 下运行。相比之下 GHDL 更快、对 VHDL-2008 支持更好。

**练习 3**：从 Changelog 看，三种仿真器是同时支持的吗？

> **参考答案**：不是。Modelsim 是最初的目标；GHDL 在 1.4.0 加入；Vivado 直到 2.2.0 才加入。Modelsim 因此是默认且支持最完整的仿真器。

---

### 4.3 许可证与版本号策略

#### 4.3.1 概念说明

在把一个库用到自己的项目里之前，了解**谁能用、怎么用、版本怎么读**是基本素养。PsiSim 在这三点上都有明确说明。

- **作者**：Oliver Bründler（`oli.bruendler@gmx.ch`），也是核心源码 `PsiSim.tcl` 头部署名的作者。
- **维护者**：Patric Bucher（`patric.bucher@psi.ch`）。
- **所属机构**：Paul Scherrer Institute, Switzerland（瑞士保罗谢尔研究所）。
- **许可证**：**PSI HDL Library License**——本质是 **LGPL**（GNU 宽通用公共许可证）加上一些针对固件/HDL 开发场景的额外例外条款。

> 名词解释：
> - **LGPL**：一种开源许可证，允许你把库以动态链接等方式用于闭源/商业项目，但如果你**修改了库本身**，修改后的库源码必须同样以 LGPL 开源。它比 GPL 宽松，比 MIT 严格。
> - **PSI HDL Library License 的“例外”**：因为 FPGA/固件开发里，HDL 代码会被综合成**比特流 / 二进制**烧进芯片，传统的 LGPL 条款在这种场景下含义模糊。PSI 的例外条款明确允许“以二进制形式（包括 FPGA 比特流、flash 镜像）使用、复制、链接、修改和分发”，从而让 LGPL 在硬件语境下可用。

#### 4.3.2 核心流程

PsiSim 的**版本号遵循 `major.minor.bugfix`（主版本.次版本.修订号）三段式**，规则如下：

- **major（主版本号）**：当改动**不完全向后兼容**（breaking change）时，递增。
- **minor（次版本号）**：当**新增功能**时，递增。
- **bugfix（修订号）**：当**只修复 bug、没有功能变化**时，递增。

这是一个典型的 [语义化版本（Semantic Versioning）思想](https://semver.org) 的简化版。配合 `Changelog.md`，读者可以快速判断升级某个版本会不会破坏自己现有的脚本。

#### 4.3.3 源码精读

**① 作者、维护者、许可证——README 头部**

[README.md:3-10](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L3-L10) 列出了维护者（Patric Bucher）、作者（Oliver Bründler）、以及许可证（PSI HDL Library License = LGPL + 针对固件开发的例外）。

**② 许可证全文——License.txt**

[License.txt:1-13](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/License.txt#L1-L13) 给出许可证本体：第 4 行是版权人，第 11 行说明本库基于 GNU Library General Public License（即 LGPL）第 2 版或更新版。

[License.txt:15-19](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/License.txt#L15-L19) 是关键的“例外通知（EXCEPTION NOTICE）”：第 19 行明确把“FPGA 比特流、flash 镜像”等设备配置文件纳入“binary（二进制）”范畴，从而允许它们在自有条款下分发——这正是 4.3.1 里讲的“让 LGPL 在硬件场景可用”的具体条款。

**③ 版本号策略——README 的 Tagging Policy**

[README.md:47-52](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L47-L52) 用三行 bullet 精确定义了 major / minor / bugfix 三个号码的递增规则。

**④ 当前版本与版本演进——Changelog**

[Changelog.md:1-8](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/Changelog.md#L1-L8) 顶部 `## 2.5.0` 就是**当前版本**（截至本 HEAD `434f6a9`），本版新增了 glob 通配符支持，并修复了 GHDL/Vivado 的若干问题。

[Changelog.md:55-57](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/Changelog.md#L55-L57)（2.0.0）说明 2.0.0 是“首个开源发布版本”，更早的历史不保留。所以从 2.0.0 到现在的 2.5.0 都能在 Changelog 里追到。

#### 4.3.4 代码实践

**实践目标**：用版本号规则去解释一次真实的版本递增，验证你真的理解了 tagging policy。

**操作步骤**：

1. 读 `README.md` 第 47–52 行的 Tagging Policy。
2. 翻 `Changelog.md`，对比下面几个相邻版本的变化类型：
   - 2.4.0 → 2.5.0
   - 2.3.0 → 2.3.1
   - 2.0.1 → 2.0.2
3. 对每个跳跃，判断它是 major / minor / bugfix 哪一类，并用 Changelog 内容印证。

**需要观察的现象**：

- 2.5.0 相比 2.4.0，次版本号（minor）从 4 变 5，对应 Changelog 里写着 “Added Features”（新增 glob 支持）——符合 minor 规则。
- 2.3.1 相比 2.3.0，修订号（bugfix）从 0 变 1，Changelog 里只有 “Bugfixes”——符合 bugfix 规则。
- 这些版本之间主版本号（major）一直是 2，说明没有引入破坏性改动。

**预期结果**：你能用规则正确预测每个版本号段递增的原因。

> 说明：本实践为文档分析型实践，无需运行命令。如果手头有其他版本（比如旧 tag），也可对比验证，否则按上述 Changelog 条目即可。

#### 4.3.5 小练习与答案

**练习 1**：PsiSim 用的是什么许可证？它和纯 LGPL 有什么区别？

> **参考答案**：PSI HDL Library License。它本质是 LGPL（第 2 版或更新）加上一份“例外通知”，例外明确允许把库以二进制形式（包括 FPGA 比特流、flash 镜像）使用、复制、链接、修改和分发，从而把 LGPL 适配到固件/HDL 开发场景。

**练习 2**：假设下一个版本只修了一个 bug，没有新功能、也不破坏兼容性，版本号应该怎么变？

> **参考答案**：当前是 2.5.0，只修 bug 应递增 bugfix 号，变为 **2.5.1**。

**练习 3**：如果某次改动让旧脚本不再能用（破坏了向后兼容），按 PsiSim 的策略版本号怎么变？

> **参考答案**：应递增主版本号（major），即从 2.5.0 变为 **3.0.0**。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个“一页纸项目简介”任务：

> 假设你刚加入一个使用 PsiSim 的 FPGA 团队，主管让你写一份不超过 200 字的内部 wiki，向新同事介绍 PsiSim。请覆盖以下要点：
>
> 1. PsiSim 是什么、解决什么核心问题（模块 4.1）。
> 2. 团队现在可以选哪三种仿真器，你们为什么优先用 GHDL 而不是 Vivado（模块 4.2）。
> 3. 这个库的许可证允许你们把它综合进自家 FPGA 比特流并闭源分发吗？为什么（模块 4.3）。

**操作建议**：

- 先只读 `README.md` 的 Features、Notes regarding Vivado、Tagging Policy 三段，以及 `License.txt` 的 EXCEPTION NOTICE。
- 写完后，对照本讲 4.1.5 / 4.2.5 / 4.3.5 的参考答案自检三个要点是否都答到。

**预期结果**：一份能让完全没接触过 PsiSim 的新同事在 2 分钟内建立正确认知的简介。

> 说明：这是写作型实践，无需运行任何工具。它的真正价值在于逼迫你把三个模块的信息重新组织成自己的语言——能组织出来，才算真的读懂了。

## 6. 本讲小结

- **PsiSim 是 PSI 用 TCL 写的 VHDL 回归测试框架**，核心卖点是：用纯文本脚本描述仿真，对版本控制友好、易于合并，比 Modelsim/Vivado 工程文件更适合 Git。
- 典型用法是 **`config.tcl`（描述）+ `run.tcl`（执行）** 的两文件模式，拆分是为了支持库级与项目级的嵌套回归。
- 支持 **三种仿真器**：Modelsim（默认，支持最完整）、GHDL（开源，快，VHDL-2008 支持好，需独立 TCL 解释器）、Vivado（商业，慢，VHDL-2008 支持差，仅作备选）。
- 仿真器通过 `init` / `init -ghdl` / `init -vivado` 切换，内部记录到 `Simulator` 状态变量。
- 许可证是 **PSI HDL Library License（LGPL + 固件例外）**，允许把库综合进 FPGA 比特流后闭源分发。
- 版本号遵循 **major.minor.bugfix**：破坏兼容性 → major，新增功能 → minor，仅修 bug → bugfix；当前版本 **2.5.0**。

## 7. 下一步学习建议

本讲只看了文档，还没真正进入代码。建议按以下顺序继续：

1. **下一讲 u1-l2《仓库结构与文件组织》**：打开 `PsiSim.tcl`，认识它的三大分区（namespace 变量 / SAL 模拟器抽象层 / 接口函数），建立“代码地图”。
2. 之后 **u1-l3《两文件工作流与首次运行》** 会带你写出第一个能跑的 `config.tcl` + `run.tcl`。
3. 如果你想先自己热身，可以先浏览一遍 `README.md` 后半部分给出的 `config.tcl` / `run.tcl` 完整示例（[README.md:56-167](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L56-L167)），对 PsiSim 长什么样有个直观印象——里面的命令细节我们会在后续讲义逐一拆解。
