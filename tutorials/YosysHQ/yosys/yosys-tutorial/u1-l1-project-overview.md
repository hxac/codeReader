# Yosys 是什么：项目定位与能力总览

## 1. 本讲目标

本讲是整本《Yosys 学习手册》的第一篇。读完本讲，你应当能够：

- 用一句话说清 **Yosys 是什么**，以及它在「数字设计 / 芯片设计」流程中处于哪个位置。
- 区分 Yosys 与 **Verilator、iverilog** 这两类常见 EDA 工具的职责差异。
- 说出 Yosys 的四大能力支柱：**前端（读 HDL）→ 综合_pass（变换网表）→ 后端（写出目标格式）→ 工艺库（目标单元）**，并理解「组合 pass 完成任意综合任务」这一核心理念。
- 看懂仓库里 **README.md 与 docs/source/** 这两份最重要的入口文档，能独立在文档树里定位到「综合（synthesis）」相关章节。
- 了解 Yosys 的 **ISC 许可证** 与它对 **sv-elab / slang** 等第三方库的依赖关系。

本讲**不要求你懂 C++、不要求你装好 Yosys、也不要求你看懂任何算法**。它只建立「全局地图」。具体的构建、运行、源码细节会在后续讲义（u1-l2 起）逐步展开。

---

## 2. 前置知识

在进入源码之前，先用最朴素的语言把几个术语讲清楚。如果你已经熟悉，可以跳到第 3 节。

### 2.1 什么是 HDL 与 Verilog

**HDL（Hardware Description Language，硬件描述语言）** 是一种用文本描述数字电路的语言。最常见的两门 HDL 是 **Verilog** 和 **VHDL**。你写下的不是「一步步执行的程序」，而是「一堆导线和逻辑门如何连接、在时钟沿如何更新」的描述。例如下面这行 Verilog 描述了一个「当 clk 上升沿到来时，把输入 d 锁存到输出 q」的触发器行为：

```verilog
always @(posedge clk)
    q <= d;
```

**SystemVerilog（IEEE 1800）** 是 Verilog 的超集，加入了接口、结构体、断言（SVA）等更现代的特性。

### 2.2 什么是「综合（synthesis）」

把 HDL 文本变成「真实可制造的门级网表」的过程，就叫**综合**。它大致经历几个抽象层级：

| 抽象层级 | 大致含义 | 典型产物 |
| --- | --- | --- |
| 行为级（Behavioural） | 用 `always`、`if`、`case` 描述「电路该做什么」 | 你写的 Verilog |
| RTL 级（Register Transfer Level） | 描述「寄存器之间数据的流动与运算」 | 数据通路 + 触发器 |
| 逻辑门级（Logic Gate） | 与门、或门、非门、多路选择器、触发器 | 门级网表 |
| 物理门级（Physical Gate） | 工艺库里具体的标准单元（如 `AND2X1`） | 可用于布局布线的网表 |

Yosys 的工作就是**接收行为级 / RTL 的 HDL，输出逻辑门级或物理门级的网表**。它**不做**布局布线（place & route），那是 nextpnr、OpenROAD 等工具的事。

### 2.3 几个容易混淆的工具

初学者经常把下面三个工具混为一谈，本讲末尾的实践会要求你亲手辨析它们：

- **Yosys**：综合工具（synthesis）。把 HDL 变成网表。
- **Verilator**：仿真工具（simulation）。把 HDL 编译成 C++ 再高速仿真，本身**不综合**电路。
- **iverilog（Icarus Verilog)**：仿真工具。解释执行 Verilog，同样主要用于**仿真**而非综合。

一句话区分：**Yosys 负责「把代码变成电路」，Verilator/iverilog 负责「验证代码描述的电路行为对不对」**。三者互补，常常配合使用。

---

## 3. 本讲源码地图

本讲是导论，涉及的「源码」主要是文档与项目说明文件，它们是你后续阅读所有 C++ 源码的导航地图。

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目最权威的「一句话定位」、许可证、构建方式、快速上手示例都在这里。 |
| `docs/source/introduction.rst` | 文档站点的「Yosys 是什么」长篇介绍：历史、能力、能做与不能做的事、Yosys 工具家族。 |
| `docs/source/index.rst` | 整个文档站的**目录树根节点**，决定了文档的组织结构。 |
| `docs/source/using_yosys/synthesis/index.rst` | 「综合详解」章节入口，是实践任务要定位的目标。 |

> 说明：Yosys 文档用 Sphinx（`.rst` 格式）编写，最终发布在 Read the Docs 上。本讲里我们把 `.rst` 当作「带格式的文本文档」来读即可，不影响理解。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**README 概述**、**文档站点结构**、**许可证与依赖（sv-elab/slang）**。

### 4.1 README 概述：Yosys 到底是什么

#### 4.1.1 概念说明

打开任何开源项目，第一份该读的文件永远是 `README.md`。Yosys 的 README 用一句话就把项目定位说清楚了：它是一个 **RTL 综合框架（framework for RTL synthesis tools）**。

注意这里有两个关键词：

- **RTL 综合**：把 HDL 翻译成网表（见第 2.2 节）。
- **框架（framework）**：Yosys 不只是一个「能跑的命令行工具」，更是一个**可以被人扩展、组合、二次开发的平台**。这是理解 Yosys 设计哲学的钥匙——后续几乎所有的源码设计（Pass 注册机制、插件、C++/Python API）都服务于「框架」这个定位。

#### 4.1.2 核心流程

Yosys 的核心理念可以用一句话概括：**通过组合现成的 pass（算法）来完成任意综合任务**。一次典型的综合运行，数据流如下：

```text
HDL 源文件
   │  ① 前端 (frontend)：read_verilog / read -sv
   ▼
RTLIL 内部表示  ← Yosys 的「通用语言」，所有 pass 都围绕它工作
   │  ② 一串 pass：hierarchy → proc → opt → memory → techmap → ...
   ▼
变换后的 RTLIL
   │  ③ 后端 (backend)：write_verilog / write_json / write_smt2 ...
   ▼
目标格式网表
```

这里的 **pass** 就是一个「对设计做一次变换的命令/算法」。`proc`、`opt`、`techmap` 都是 pass。你写一个 `.ys` 脚本，本质上就是**编排一串 pass 的执行顺序**。这一点 README 里讲得非常直白。

#### 4.1.3 源码精读

**① 一句话定位**。README 开篇即点题：

[README.md:L4-L6](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/README.md#L4-L6) —— 这段说明 Yosys 是一个 RTL 综合框架，目前对 **Verilog-2005** 有完整支持，并为多个应用领域提供基础综合算法。

**② 通过组合 pass 完成任意综合**。这是全篇最重要的一段：

[README.md:L11-L14](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/README.md#L11-L14) —— 这段告诉你：Yosys 可以通过「用综合脚本组合现有 pass，并按需用 C++ 扩展新 pass」来适配任意综合任务。这就是「框架」二字的来源。

**③ 端到端示例**。README 的 Getting Started 给出了一个最小可运行的交互流程，把上面流程图里的三步具象化了：

[README.md:L168-L195](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/README.md#L168-L195) —— 这几行依次演示了：用 `read -sv` 读入 Verilog（前端）、`hierarchy -top` 确定顶层、`write_rtlil` 打印内部表示、`proc; opt` 把 `always` 块转成网表并优化、`techmap; opt` 映射到门级、最后 `write_verilog` 输出网表（后端）。这一串命令就是「组合 pass」的最直观例子。

**④ 默认综合脚本**。除了手动编排，Yosys 还内置了两条「一键综合」命令，覆盖绝大多数场景：

[README.md:L240-L252](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/README.md#L240-L252) —— 这段说明 `synth` 提供了通用门级综合的默认脚本，而 `prep` 提供了面向 **SMT 形式验证** 的字级（word-level）综合默认脚本。记住这两个名字，它们会在 u4-l2（ScriptPass 与 synth/prep）深入讲解。

> 提示：`synth` 与 `prep` 本身也是 pass，只不过它们是「编排其他 pass 的 pass」（ScriptPass）。这是 Yosys 把复杂流程封装起来的方式。

#### 4.1.4 代码实践

**实践目标**：通过阅读 README，亲手辨析 Yosys 与 Verilator / iverilog 的职责差异，巩固「综合 vs 仿真」的核心区分。

**操作步骤**：

1. 打开本仓库的 `README.md`，重点阅读第 4-14 行（定位）和第 258-262 行（与 Verilator 的关系）。
2. 找到这段关键说明：

   [README.md:L258-L262](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/README.md#L258-L262) —— 这段明确指出 `read_verilog` **不做语法检查**，建议你先用 Verilator 做 lint。这说明 Yosys 与 Verilator 在工作流里是**互补**关系，而非替代关系。

3. 基于以上阅读，写一段 3-5 句的话，回答：**Yosys、Verilator、iverilog 三者各自的核心职责是什么？为什么 Yosys 官方推荐「综合前先用 Verilator lint」？**

**需要观察的现象**：你会注意到 README 把 Verilator 定位为「外部 lint / 仿真工具」，而不是竞争对手。这是理解 Yosys 在工具链中位置的关键线索。

**预期结果**：你的段落应当包含类似如下的结论（仅供参考，鼓励用自己的话写）：

> Yosys 是综合工具，把 Verilog 编译成门级网表；Verilator 与 iverilog 是仿真工具，用于验证电路行为。由于 Yosys 的 `read_verilog` 前端为了综合效率而**不做完整语法/类型检查**，所以官方建议先用 Verilator 做静态 lint，再用 Yosys 综合，形成「先验证、再综合」的流程。

#### 4.1.5 小练习与答案

**练习 1**：README 说 Yosys 是「framework for RTL synthesis tools」。这里的「framework」比「tool」多了什么含义？

**参考答案**：framework 强调 Yosys 是一个**可扩展、可组合、可被二次开发**的平台，而不只是一个固定功能的命令行工具。用户可以通过组合 pass、编写插件、调用 C++/Python API 来定制自己的综合流程。

**练习 2**：在 README 的 Getting Started 里，`proc; opt` 这一步的作用是什么？它对应流程图里的哪一阶段？

**参考答案**：`proc` 把 Verilog 的 `always` 块（行为级）转换成网表元素（多路选择器、触发器等），`opt` 做简单优化。它对应「② 一串 pass」阶段，是把行为级 RTLIL 变换为更接近门级 RTLIL 的关键一步。

---

### 4.2 文档站点结构：去哪里找答案

#### 4.2.1 概念说明

Yosys 是个有几十万行 C++ 的大型项目，**不可能只靠 README 讲清楚**。它附带了一套用 Sphinx 编写的完整文档，发布在 Read the Docs 上。学会使用这套文档，是你后续自学能力的根基。

文档以 `.rst`（reStructuredText）源文件形式存放在 `docs/source/` 下。理解文档结构，本质上就是理解 `index.rst` 里的 **toctree（目录树）**。

#### 4.2.2 核心流程

文档站的导航逻辑，是从根目录 `index.rst` 的 toctree 一层层往下展开的：

```text
docs/source/index.rst（根目录树）
 ├── introduction.rst            ←「Yosys 是什么」（本讲重点）
 ├── getting_started/index       ← 安装、构建、第一个综合示例
 ├── using_yosys/index           ← 进阶使用
 │    ├── synthesis/index        ←「综合详解」★ 实践任务要找的章节
 │    └── more_scripting/index   ← 脚本技巧、选择机制、形式验证
 └── yosys_internals/index       ← 内部原理（数据结构、扩展开发）
      ├── flow/index             ← 前端/后端流程
      ├── formats/index          ← RTLIL 等内部格式
      └── extending_yosys/index  ← 如何写自定义 pass
```

记住这条路径：**想看「综合相关章节」→ `using_yosys/synthesis/`**。

#### 4.2.3 源码精读

**① 文档根节点**。`index.rst` 开头一句话定位，并给出导航建议：

[docs/source/index.rst:L5-L9](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/index.rst#L5-L9) —— 这段说明 Yosys 是开源 RTL 综合框架，并指引读者：想了解全貌看 `introduction`，想快速上手看 `getting_started`，想查命令看 `cmd_ref`。

**② 主目录树**。整个文档的骨架就在这个 toctree 里：

[docs/source/index.rst:L19-L28](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/index.rst#L19-L28) —— 这段定义了文档的四大主章节：`introduction`（介绍）、`getting_started`（入门）、`using_yosys`（进阶使用）、`yosys_internals`（内部原理）。你后续查阅任何主题，都从这四块入手。

**③ 综合章节入口**。顺着 `using_yosys` 找下去，就来到实践任务的目标：

[docs/source/using_yosys/synthesis/index.rst:L3-L15](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/using_yosys/synthesis/index.rst#L3-L15) —— 这段把综合分成 **粗粒度（coarse-grain）** 与 **细粒度（fine-grain）** 两个阶段，并点名了 `proc`、`fsm`、`memory`、`opt`、`techmap`、`abc` 等核心命令。这正好对应我们第 4.1.2 节流程图里的「一串 pass」。

**④ 离线阅读技巧**。如果你没法上网，README 提供了一个本地读文档的小窍门：

[README.md:L268-L271](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/README.md#L268-L271) —— 这段告诉你：在线文档地址是 `https://yosyshq.readthedocs.io/en/latest/`，离线时把 URL 里的 `/en/latest` 直接换成仓库里的 `docs/source` 路径即可读到同样的内容。

#### 4.2.4 代码实践

**实践目标**：在本地文档树里亲手定位到「综合」相关章节，建立「遇到问题知道去哪查」的肌肉记忆。

**操作步骤**：

1. 打开 `docs/source/index.rst`，找到第 19-28 行的主 toctree，确认 `using_yosys/index` 这一项存在。
2. 打开 `docs/source/using_yosys/index.rst`，确认它把内容分成 `synthesis`（综合详解）与 `more_scripting`（更多脚本技巧）两部分。
3. 打开 `docs/source/using_yosys/synthesis/index.rst`，阅读第 3-20 行。
4. 列出该章节 toctree 里出现的命令名（如 `synth`、`proc`、`fsm`、`memory`、`opt`、`techmap`、`extract`、`abc`、`cell_libs`）。

**需要观察的现象**：你会看到综合被明确划分为「粗粒度」和「细粒度」两个阶段，不同命令面向不同阶段。这是后续 u6（核心综合流程）整章的纲领。

**预期结果**：你应当能画出一棵从 `index.rst` 到 `synthesis/index.rst` 的目录路径，并说出综合章节里至少 5 个命令的名字。

> 待本地验证：如果你的仓库是完整 clone 的，`docs/source/` 下应能直接找到上述 `.rst` 文件；若为部分检出，可改用在线版 `https://yosyshq.readthedocs.io/en/latest/using_yosys/synthesis/`。

#### 4.2.5 小练习与答案

**练习 1**：想了解 Yosys 内部的 RTLIL 数据结构，应该去文档树的哪一块？

**参考答案**：去 `yosys_internals/`，具体是其下的 `formats/`（格式，含 RTLIL 表示）和 `flow/`（流程）。这对应本手册后续的 u2（RTLIL 入门）和 u3（RTLIL 深入）。

**练习 2**：`index.rst` 里 toctree 的 `:maxdepth: 3` 是什么意思？删掉它会怎样？

**参考答案**：`:maxdepth: 3` 控制左侧目录树最多展开 3 层。删掉它，Sphinx 会用默认深度，可能导致目录树展开层级变化、导航体验不同。它只影响**显示**，不影响文档内容本身。

---

### 4.3 许可证与依赖：sv-elab / slang 是什么

#### 4.3.1 概念说明

开源项目有两条「家规」必须先弄清：**用什么许可证**、**依赖哪些第三方库**。这两点决定了你能否、以及如何合法使用和二次开发 Yosys。

- **许可证（License）**：规定你能否商用、修改、再分发。Yosys 用的是非常宽松的 **ISC 许可证**。
- **依赖（Dependencies）**：Yosys 本身是 C++ 写的，但它的 **SystemVerilog 支持** 依赖两个外部库：**sv-elab** 和 **slang**。理解这一点，能解释为什么构建 Yosys 需要 `git submodule update --init`。

#### 4.3.2 核心流程

Yosys 的依赖栈可以这样分层：

```text
┌─────────────────────────────────────────────┐
│  Yosys 主体（C++，ISC 许可证）               │
├─────────────────────────────────────────────┤
│  Verilog-2005 前端：Yosys 自带（flex/bison） │
│  SystemVerilog 前端：依赖 sv-elab + slang    │ ← git submodule
├─────────────────────────────────────────────┤
│  ABC：门级逻辑优化（abc/ 子目录，独立许可）  │
├─────────────────────────────────────────────┤
│  可选：readline / libffi / Tcl / zlib        │
└─────────────────────────────────────────────┘
```

关键点：**Verilog-2005 由 Yosys 自己实现，SystemVerilog 的支持则外包给了 sv-elab + slang 这两个第三方库**（以 git submodule 形式引入）。

#### 4.3.3 源码精读

**① SystemVerilog 支持**。README 明确说明了 SV 支持的来源：

[README.md:L8-L9](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/README.md#L8-L9) —— 这段说明 Yosys 使用 **sv-elab**（来自 povik）和 **slang**（来自 MikePopoloski）两个库来提供完整的 SystemVerilog 支持，覆盖 IEEE 1800-2017 / 1800-2023 的可综合子集。

> 名词解释：**slang** 是一个高性能的 SystemVerilog 编译器前端（词法/语法/语义分析）；**sv-elab** 是基于 slang 的精化（elaboration）层，把它接入 Yosys。二者配合，让 Yosys 能读现代 SystemVerilog。这也是为什么本仓库最近若干提交（如 `0135a61db` sv-elab 相关）会持续维护这条依赖链。

**② ISC 许可证**。Yosys 主体的许可证声明：

[README.md:L16-L18](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/README.md#L16-L18) —— 这段声明 Yosys 是自由软件，采用 **ISC 许可证**（与 GPL 兼容，条款类似 MIT 或两条款 BSD）。这意味着你可以自由地商用、修改、再分发，只需保留版权声明。

**③ 第三方软件的许可证**。仓库里 bundled 的第三方组件有各自的许可：

[README.md:L20-L22](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/README.md#L20-L22) —— 这段提示随 Yosys 分发的第三方软件（如 `abc/` 和 `libs/` 子目录里的内容）采用各自兼容的许可证，需要分别查阅。所以**商业使用前，务必检查这些子目录的许可条款**。

**④ 为什么需要 submodule**。因为 sv-elab / slang 等是独立仓库，构建前必须初始化子模块：

[README.md:L70-L75](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/README.md#L70-L75) —— 这段给出 clone 与初始化子模块的标准命令：`git submodule update --init`。如果你跳过这一步，SystemVerilog 相关的源码目录会是空的，构建可能失败或缺少 SV 支持。

**⑤ 构建工具链要求**。README 还列出了编译 Yosys 需要的工具链版本：

[README.md:L77-L82](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/README.md#L77-L82) —— 这段说明需要支持 **C++20** 的编译器，以及 Flex、Bison（>=3.8）、CMake（>=3.28）、Python（>=3.11）；readline、libffi、Tcl、zlib 是可选的。这些版本门槛会在 u1-l2（构建与运行）里实际用到。

#### 4.3.4 代码实践

**实践目标**：摸清 Yosys 的许可证边界与第三方依赖，为后续（合法地）使用和二次开发打好基础。

**操作步骤**：

1. 在仓库根目录执行 `git submodule status`，观察输出里是否包含 sv-elab、slang 等子模块及其当前 commit。**（只读命令，安全）**
2. 打开 `README.md` 第 16-22 行，确认 ISC 许可证与第三方许可证的分层关系。
3. 检查本地是否存在 `abc/` 与 `libs/` 子目录（这是第三方代码所在处）。
4. 写一句话回答：「如果我公司想闭源商用一个基于 Yosys 修改的工具，许可证层面需要注意什么？」

**需要观察的现象**：`git submodule status` 会列出一行形如 `<commit> <路径>` 的条目；若子模块未初始化，前面可能有 `-` 前缀。

**预期结果**：你能说出「Yosys 主体是 ISC（极宽松，可闭源商用），但 `abc/`、`libs/` 及 sv-elab/slang 子模块有各自的许可，需逐一核对」。

> 待本地验证：`git submodule status` 的具体输出取决于 clone 时是否执行过 `--init`；若未执行，输出会显示子模块未检出。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Yosys 要把 SystemVerilog 支持交给 sv-elab + slang，而不是自己实现？

**参考答案**：SystemVerilog 是一个庞大复杂的语言标准，自己实现完整前端成本极高。slang 已经是一个成熟、高性能的 SV 前端编译器，通过 sv-elab 把它接入 Yosys，可以复用社区成果、降低维护成本，让 Yosys 专注于综合本身。这是典型的「不要重复造轮子」。

**练习 2**：ISC 许可证和 GPL 的最大区别是什么？对使用者意味着什么？

**参考答案**：ISC 是**宽松型（permissive）**许可证，允许闭源商用、修改后不公开源码，只需保留版权声明；GPL 是**著佐权型（copyleft）**，要求衍生作品也必须开源。对使用者而言，ISC 意味着「可以放心地用在闭源商业产品里」，这正是 Yosys 能被工业界广泛采用的原因之一。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个小任务（纯阅读型，无需安装 Yosys）：

**任务：制作一张「Yosys 项目名片」**

请你打开仓库，综合运用本讲所学，产出一页「Yosys 项目名片」，包含以下四栏，每栏 1-3 句话，且**每栏都要引用一个真实的源码位置（文件:行号）作为依据**：

1. **一句话定位**：Yosys 是什么？（提示：参考 `README.md` 第 4-6 行）
2. **核心能力**：它如何完成一次综合？（提示：参考 `README.md` 第 11-14、168-195 行）
3. **文档入口**：想深入了解综合，去文档哪里找？（提示：参考 `docs/source/index.rst` 第 19-28 行与 `using_yosys/synthesis/index.rst`）
4. **许可证与依赖**：能用 / 怎么用 / 依赖谁？（提示：参考 `README.md` 第 8-9、16-22 行）

**验收标准**：

- 四栏齐全，每栏的结论都能在引用的源码处找到支撑。
- 第 2 栏能体现「前端 → pass → 后端」三阶段。
- 第 4 栏能区分「Yosys 主体（ISC）」与「第三方（abc/libs/sv-elab/slang）」两层许可。

完成这张名片，你就具备了阅读后续所有讲义的全局视野。

---

## 6. 本讲小结

- **Yosys 是一个开源的 RTL 综合框架**，把行为级 / RTL 的 Verilog、SystemVerilog 翻译成门级 / 物理门级网表（见 `README.md:L4-L6`）。
- 它的核心理念是**通过组合 pass（算法）完成任意综合任务**，并可按需用 C++ 扩展新 pass（见 `README.md:L11-L14`）。
- 一次综合的数据流是 **前端（读 HDL）→ 一串 pass（变换 RTLIL）→ 后端（写目标格式）**，`synth` / `prep` 是两条内置默认脚本（见 `README.md:L240-L252`）。
- **Yosys 是综合工具，Verilator / iverilog 是仿真工具**，三者互补；官方建议综合前先用 Verilator lint（见 `README.md:L258-L262`）。
- 文档以 `.rst` 存放在 `docs/source/`，根目录 `index.rst` 的 toctree 分为 introduction / getting_started / using_yosys / yosys_internals 四大块，「综合详解」在 `using_yosys/synthesis/` 下。
- Yosys 主体采用宽松的 **ISC 许可证**；SystemVerilog 支持依赖 **sv-elab + slang**（git submodule），`abc/`、`libs/` 等第三方组件有各自许可（见 `README.md:L8-L9`、`L16-L22`）。

---

## 7. 下一步学习建议

本讲只建立了「全局地图」，还没有真正运行 Yosys。建议接下来按顺序学习：

1. **u1-l2 构建 Yosys：CMake 构建与可执行入口** —— 动手用 CMake 编译出 `./build/yosys`，并理解 `kernel/driver.cc` 里 `main()` 如何初始化。这是把本讲的「文档知识」变成「能跑的工具」的关键一步。
2. **u1-l3 顶层目录结构地图** —— 把 `kernel / frontends / passes / backends / techlibs` 这些目录的职责对应起来，为后续读源码建立空间感。
3. **u1-l4 第一次综合：交互式 shell 与 cmos 计数器示例** —— 在 `yosys>` 里亲手跑一遍 `read → proc → opt → techmap → write_verilog`，把本讲的流程图变成真实体验。

学完 u1 这四个讲义，你就能独立完成一次端到端综合，届时再进入 u2（RTLIL 内部表示）深入源码内部。
