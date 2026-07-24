# SURF 项目总览：它是什么、为什么这样组织

## 1. 本讲目标

读完本讲后，你应该能够：

- 用一句话说清楚 SURF 是什么、它解决了什么问题；
- 记住 `axi / base / devices / dsp / ethernet / protocols / xilinx / python / tests` 九大顶层子树各自的职责；
- 知道 `README.md`、`AGENTS.md` 和顶层 `ruckus.tcl` 这三个文件分别在仓库里起什么导航作用；
- 拿到一个需求后，能大致判断该去哪个子树找代码。

本讲是整本学习手册的**起点**，不会涉及任何具体 RTL 语法细节。它的唯一目的是让你在动手读源码之前，先在脑子里建立一张「仓库地图」。

## 2. 前置知识

本讲面向完全没接触过 SURF 的读者。你只需要了解以下常识：

- **FPGA / VHDL**：SURF 的核心是用 VHDL 写的数字逻辑库。你不需要现在就会写 VHDL，但要知道「RTL」「综合」「仿真」这几个词大概指什么。
- **IP 库（IP Library）**：很多 FPGA 项目会把可复用的模块（FIFO、总线接口、协议核……）整理成一个共享库，多个板卡工程共同引用。SURF 就是这样一个库。
- **PyRogue**：SLAC 开发的软件框架，用来在 PC 端通过寄存器映射访问 FPGA。SURF 在 `python/` 子树里提供了与 RTL 寄存器一一对应的 PyRogue 设备模型。
- **Markdown / 链接**：仓库里的导航全靠 `README.md` 之间的相互链接，就像网页之间的超链接。

> 关键心态：**SURF 不是一个「单板工程」，而是一套「跨板卡复用的基础设施」**。理解这一点，才能理解它为什么这样分目录、为什么有那么多 `*Pkg.vhd`（包文件）和 `ruckus.tcl`（构建清单）。这个定位在 [AGENTS.md:L3](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L3) 里被明确写出。

## 3. 本讲源码地图

本讲只读「导航类」文件，不读任何具体 RTL。涉及的文件如下：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/README.md) | 仓库门面：项目名称、外部链接、以及最重要的「Repository Map」子树索引。 |
| [AGENTS.md](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md) | 贡献者/编码助手的「宪法」：项目定位、目录约定、VHDL 风格、ruckus 约定、验证流程。 |
| [ruckus.tcl](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ruckus.tcl) | 顶层构建清单：声明 SURF 由哪几个顶层子树组装而成。 |
| 各子树 `README.md`（如 `base/README.md`、`axi/README.md` 等） | 每个子树自己的「小门面」，给出更细的子目录职责。 |

本讲的三个最小模块是：**仓库地图**、**README 导航**、**AGENTS 贡献者指南**。

## 4. 核心概念与源码讲解

### 4.1 仓库地图：SURF 是什么、如何分块

#### 4.1.1 概念说明

SURF 的全称是 **SLAC Ultimate RTL Framework**（SLAC 终极 RTL 框架），这是 [README.md:L5](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/README.md#L5) 写明的项目副标题。

它的本质是 SLAC（美国斯坦福直线加速器中心）实验室内部沉淀下来的一套**共享 VHDL/IP 基础设施库**。它的目标不是「做出某一块板子」，而是「让所有板子工程都能复用同一套总线、协议、FIFO、RAM、收发器封装和软件镜像」。这一定位决定了它的目录是按「能力」而不是按「板卡」来分的：

- 你要 FIFO？去 `base/fifo/`。
- 你要 AXI 总线？去 `axi/`。
- 你要以太网？去 `ethernet/`。
- 你要 PGP / JESD204B 这种协议？去 `protocols/`。

这种「按能力横向切分」的组织方式，使得任何一块新板卡只要从这些子树里「挑模块、搭积木」就能组装出自己的固件。

#### 4.1.2 核心流程

仓库顶层有几个特殊文件，它们共同构成了 SURF 的「组装关系」：

1. 顶层 `ruckus.tcl` 是**总清单**，它用 `loadRuckusTcl` 逐个加载各顶层子树。
2. 每个子树内部又有自己的 `ruckus.tcl`，递归地把该子树的 `rtl/`、`sim/` 等目录登记进构建。
3. `README.md` 给人类读者一张「子树索引表」（Repository Map）。
4. `AGENTS.md` 给贡献者一张「规则索引表」。

用伪代码描述这种层级清单关系：

```text
顶层 ruckus.tcl
 ├── loadRuckusTcl axi
 ├── loadRuckusTcl base
 ├── loadRuckusTcl dsp
 ├── loadRuckusTcl devices
 ├── loadRuckusTcl ethernet
 ├── loadRuckusTcl protocols
 └── loadRuckusTcl xilinx
```

注意：`python/` 和 `tests/` **不在** `ruckus.tcl` 里加载——前者是纯 Python/PyRogue，后者是纯 Python/cocotb 测试，它们都不参与 HDL 综合，所以只通过各自的 README 被人类导航，而不进入 ruckus 构建图。这是「构建清单只管 HDL」的边界。

#### 4.1.3 源码精读

顶层 `ruckus.tcl` 非常短，只有二十多行，却能让你一眼看清 SURF 的「骨架」。关键片段：

[ruckus.tcl:L14-L21](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ruckus.tcl#L14-L21) —— 这 7 行 `loadRuckusTcl` 就是 SURF 的七块 HDL 拼图，加载顺序也隐含了从底层到上层的依赖直觉（`axi`/`base` 在前，`protocols`/`ethernet` 在后）。

[ruckus.tcl:L2](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ruckus.tcl#L2) —— `source $::env(RUCKUS_PROC_TCL)` 引入 ruckus 工具自身提供的 Tcl 命令（如 `loadRuckusTcl`、`loadSource`、`getFpgaArch`）。它说明 SURF 依赖一个名为 `ruckus` 的外部构建工具（作为 git submodule，版本受 [ruckus.tcl:L6](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ruckus.tcl#L6) 的 `SubmoduleCheck {ruckus} {4.9.0}` 守卫）。

这一加载清单在 [AGENTS.md:L22](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L22) 也被明确总结：「Top-level `ruckus.tcl` loads `axi`, `base`, `dsp`, `devices`, `ethernet`, `protocols`, and `xilinx`.」可以作为对照记忆。

#### 4.1.4 代码实践

这是一个**源码阅读 + 目录核对型**实践，帮助你把「清单」和「真实目录」对上。

1. **实践目标**：确认顶层 `ruckus.tcl` 加载的 7 个子树在磁盘上真实存在，并理解哪两个顶层目录（`python/`、`tests/`）不进入 HDL 构建。
2. **操作步骤**：
   - 打开 [ruckus.tcl:L15-L21](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ruckus.tcl#L15-L21)，记下 7 个被 `loadRuckusTcl` 的名字。
   - 列出仓库根目录（可以用文件浏览器或 `git ls-files | awk -F/ '{print $1}' | sort -u`），对比有哪些顶层目录。
3. **需要观察的现象**：你会看到根目录除了清单里的 7 个子树外，还有 `python/`、`tests/`、`docs/`、`scripts/`、`conda-recipe/`、`.github/` 等目录。
4. **预期结果**：`python/` 和 `tests/` 出现在磁盘上但**不在** `ruckus.tcl` 中，印证「它们是软件/测试，不参与 HDL 综合」。
5. 如果无法运行命令，可在 GitHub 网页上直接浏览仓库根目录做同样核对（待本地验证仅指命令行结果，结论本身可由阅读源码得出）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 SURF 的顶层目录是 `base/`、`axi/`、`ethernet/` 这种「按能力」划分，而不是 `boardA/`、`boardB/` 这种「按板卡」划分？

> **参考答案**：因为 SURF 是**共享基础设施库**而非单板工程（见 [AGENTS.md:L3](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L3)）。按能力划分能让任意板卡工程复用同一份 FIFO/总线/协议代码，避免每块板子各抄一遍。

**练习 2**：`python/` 和 `tests/` 两个目录为什么不出现在顶层 `ruckus.tcl` 里？

> **参考答案**：`ruckus.tcl` 只登记参与 HDL 综合/仿真的 VHDL 源文件。`python/` 是 PyRogue 软件镜像，`tests/` 是 cocotb/pytest 测试，都不是 HDL，所以不进入构建清单（见 [ruckus.tcl:L15-L21](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ruckus.tcl#L15-L21)）。

---

### 4.2 README 导航：如何用顶层 README 找到任何子系统

#### 4.2.1 概念说明

一个大型仓库最容易让人迷路。SURF 的做法是：**每个目录层级都放一个简短的 `README.md`，只做「导航」不做「教学」**——它告诉你「这个文件夹装了什么、子目录是什么、上层链接在哪」，然后把你交给更深一层的 README 或具体源码。

顶层 [README.md](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/README.md) 的核心是一个叫 **Repository Map** 的小节，它把所有顶层子树列成一张带链接的清单。这张表就是你「找东西」的入口。

#### 4.2.2 核心流程

要找一个模块时的标准动线：

```text
1. 看顶层 README.md 的 Repository Map
2. 点击对应子树的链接（如 protocols/README.md）
3. 在子树 README 的 Layout 小节里定位到具体子目录
4. 进入子目录读 ruckus.tcl 或 *Pkg.vhd
```

例如「我想找 PGP 协议怎么实现」：

- 顶层 README 里 `Protocols` 一行指向 [protocols/README.md](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/README.md)；
- 该 README 第 7 行告诉你 link/协议族包含 `pgp/`、`ssi/`、`srp/` 等；
- 于是你知道该进 `protocols/pgp/`。

#### 4.2.3 源码精读

顶层 README 的 Repository Map 是本讲最重要的「地图本体」：

[README.md:L9-L20](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/README.md#L9-L20) —— 这 11 行（含标题）就是整张子树索引。逐行含义如下：

| README 行 | 子树 | 一句话职责（据该行原文） |
| --- | --- | --- |
| L11 | `AGENTS.md` | 贡献者与编码助手的项目布局/约定/验证说明 |
| L12 | `axi/` | AXI-Lite、AXI4、AXI Stream、DMA、桥、仿真链路 RTL |
| L13 | `base/` | 基础包、CDC、FIFO、RAM、复位、延迟、CRC、通用 RTL 助手 |
| L14 | `devices/` | 厂商/器件相关的 RTL 支持 |
| L15 | `dsp/` | 通用与 Xilinx 专用 DSP 支持 |
| L16 | `ethernet/` | MAC、原始以太网、IPv4、UDP、RoCEv2、高速以太网核 |
| L17 | `protocols/` | PGP、SSI、SRP、RSSI、CoaXPress、JESD204B、外设总线等协议核 |
| L18 | `xilinx/` | Xilinx 家族封装、原语集成、XVC UDP 支持 |
| L19 | `python/` | `python/surf` 下的 PyRogue 包布局 |
| L20 | `tests/` | cocotb 回归布局、方法学、助手、仿真器约定 |

注意这 11 行索引覆盖了 **9 个子树 + AGENTS.md**，比 `ruckus.tcl` 多出了 `python/` 和 `tests/`——因为 README 是给「完整理解仓库」用的，要把软件和测试也纳入导航。

每个子树 README 内部又重复了这个套路。以 `base/` 为例：

[base/README.md:L3](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/README.md#L3) 一句话点明 `base/` 是「其余 SURF 都会用到的底层 RTL」，然后 [base/README.md:L7-L12](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/README.md#L7-L12) 用一个 `Layout` 列表把 `general/`、`sync/`、`fifo/`、`ram/`、`delay/`、`crc/` 六个二级目录各用一句话说清。这就是「导航 README」的标准写法：**短、可点击、只说「是什么」不说「怎么用」**。

> 顺带一提，顶层 README 还提供了两个对外链接入口：[README.md:L34-L38](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/README.md#L34-L38) 是两份 Google Slides 演讲（SURF 入门、IEEE RT 2024 Workshop），适合在读完本讲后作为高层背景材料浏览。

#### 4.2.4 代码实践

1. **实践目标**：仅靠 README 的链接，从顶层走到某个具体二级目录，体会「导航 README」的层层下钻。
2. **操作步骤**：
   - 从 [README.md:L9-L20](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/README.md#L9-L20) 出发，挑选 `Base` 这一行点进 `base/README.md`。
   - 在 `base/README.md` 的 Layout 里找到 `fifo/` 的描述。
   - 再尝试对 `axi/`（见 [axi/README.md:L7-L12](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/README.md#L7-L12)）做同样的事，记录 `axi-lite/`、`axi-stream/`、`dma/`、`simlink/` 分别是什么。
3. **需要观察的现象**：每次下钻一层，README 都变短、更聚焦，最终把读者交给具体源码或子目录的 `ruckus.tcl`。
4. **预期结果**：你能为 `axi/` 和 `base/` 各写出一张二级目录职责表（这正是本讲综合实践的一部分）。

#### 4.2.5 小练习与答案

**练习 1**：如果我想找一个 AXI-Stream 数据流的 FIFO，应该先打开哪个 README？

> **参考答案**：先打开顶层 [README.md:L12](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/README.md#L12) 的 `AXI` 行，进入 [axi/README.md](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/README.md)，其 Layout（[axi/README.md:L8](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/README.md#L8)）指出 AXI Stream FIFO 在 `axi-stream/` 子目录。

**练习 2**：为什么 SURF 的 README 都写得很短，只做导航而不展开教学？

> **参考答案**：因为仓库约定 README 只描述「文件夹里装什么、子目录、本地构建/测试约定」，然后把读者向上回链到父 README、向下交给源码（见 [AGENTS.md:L226-L230](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L226-L230) 的 Documentation Updates 一节）。教学/细节属于本学习手册，而非仓库 README。

---

### 4.3 AGENTS 贡献者指南：仓库的「宪法」

#### 4.3.1 概念说明

如果说 `README.md` 是给「使用者」的导航，那么 [AGENTS.md](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md) 就是给「贡献者」（包括人类和 AI 编码助手）的**规则手册**。它一开始就强调：

> 「Treat it as reusable infrastructure, not a single board project. Keep changes narrow, preserve existing public interfaces...」
> （[AGENTS.md:L3](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L3)）

这条原则会影响你后续读每一篇讲义：SURF 高度重视**接口稳定**和**复用既有原语**，几乎不允许「重复造轮子」。

对初学者来说，AGENTS.md 的价值在于：它把 SURF 里所有「为什么这样写」的隐含约定都写下来了。你现在不需要记住全部细节，只需要知道**遇到疑问时去 AGENTS.md 的哪一节找答案**。

#### 4.3.2 核心流程

AGENTS.md 用一节一节的小标题组织规则，主要章节及其用途：

| AGENTS.md 章节 | 你什么时候会用到它 |
| --- | --- |
| Repository Map | 想要一份「更面向开发者」的目录索引时 |
| VHDL Conventions / Two-Process Style | 读/写任何 RTL 时（后续 u1-l4、u1-l5 详讲） |
| VHDL Package Conventions | 读/写任何 `*Pkg.vhd` 时 |
| Ruckus Conventions | 改构建清单、增删 HDL 文件时（u1-l2、u1-l3 详讲） |
| Reset And CDC Rules | 涉及时钟域跨越/复位时（u2-l1 详讲） |
| Bus And Protocol Semantics | 改 AXI/SSI/PGP 等总线行为时 |
| AXI-Lite Register Implementation Pattern | 写寄存器从机时（u3-l2 详讲） |
| Tests And Verification | 写/跑 cocotb 回归时（u9 详讲） |
| Python Conventions / PyRogue Register Maps | 改 `python/surf` 软件镜像时（u9-l4 详讲） |

也就是说，AGENTS.md 是整本学习手册的**索引之索引**：本手册后续每一篇讲义，本质上都是在展开 AGENTS.md 里的某一条规则。

#### 4.3.3 源码精读

[AGENTS.md:L7-L20](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L7-L20) —— AGENTS 版的 Repository Map，比 README 版多了 `docs/plans/README.md` 一项（[AGENTS.md:L20](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L20)），它指向任务计划与交接笔记的存放约定（见 [AGENTS.md:L232-L236](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L232-L236)）。这是开发者视角比使用者视角多看到的一块。

[AGENTS.md:L24-L35](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L24-L35) —— VHDL Conventions 一节，给出了后续所有讲义都会用到的命名约定：泛型后缀 `_G`、常量后缀 `_C`、记录类型用 `Type` 后缀、模块名用 PascalCase；并要求 RTL 按 `rtl/`、`sim/`、`tb/`、`wrappers/`、家族目录分类。这些约定是 u1-l3（目录约定）和 u1-l4（StdRtlPkg）的直接依据。

[AGENTS.md:L37-L50](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L37-L50) —— Two-Process VHDL Style 一节，规定了 SURF 的 `RegType` / `REG_INIT_C` / `r` / `rin` / `comb` / `seq` 双进程写法。这是 u1-l5 的核心内容，也是阅读几乎任何 SURF 状态机的前提。你现在只要记住「SURF 的寄存器逻辑长这个样子」即可。

[AGENTS.md:L74-L82](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L74-L82) —— Ruckus Conventions 一节，明确「`ruckus.tcl` 是构建清单」，改 HDL 必须同步改最近的 `ruckus.tcl`，并用 `getFpgaArch` 做家族选择。这是 u1-l2、u1-l3 的依据。

#### 4.3.4 代码实践

1. **实践目标**：学会「带着问题查 AGENTS.md 的某一节」，而不是通读全文。
2. **操作步骤**：假设你有这样一个疑问——「在 SURF 里写一个 AXI-Lite 寄存器从机，该用什么标准模式？」
   - 打开 [AGENTS.md](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md)，浏览小标题，定位到「AXI-Lite Register Implementation Pattern」一节（[AGENTS.md:L103-L113](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L103-L113)）。
   - 读这一节，记下它推荐的四个 helper 过程名字（`axiSlaveWaitTxn` / `axiSlaveRegister` / `axiSlaveRegisterR` / `axiSlaveDefault`）。
3. **需要观察的现象**：你会发现 AGENTS.md 几乎每一节都对应后续某篇讲义，而且它给出的都是「规则」而非「教程」。
4. **预期结果**：你能说出 AGENTS.md 至少 3 个章节标题，以及它们各自对应后续哪一类工作（RTL / ruckus / 测试 / PyRogue）。

#### 4.3.5 小练习与答案

**练习 1**：AGENTS.md 开篇要求「Treat it as reusable infrastructure, not a single board project.」这一句话对后续读源码有什么实际影响？

> **参考答案**：它意味着你会看到大量「同一套 FIFO/总线/协议代码被许多模块复用」的结构，而不是每处各写一份；也意味着改动公共接口必须非常谨慎（见 [AGENTS.md:L3](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L3)）。

**练习 2**：AGENTS.md 的 Repository Map 比 README.md 的多出了哪一项？

> **参考答案**：多出了 `docs/plans/README.md`（[AGENTS.md:L20](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L20)），它指向贡献者存放任务计划与交接笔记的目录（[AGENTS.md:L232-L236](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L232-L236)）。这是开发者视角才需要的导航。

**练习 3**：如果你要给 SURF 加一个新的 `.vhd` 文件，根据 AGENTS.md，你至少要同步更新哪个文件？

> **参考答案**：要更新离该文件最近的 `ruckus.tcl` 清单（见 [AGENTS.md:L75-L76](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L75-L76) 与 [AGENTS.md:L82](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L82)）。如果它影响寄存器映射，还要同步 PyRogue 模型与测试。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这张「SURF 顶层子树职责表」。这是本讲唯一的产出物，但它会成为你后续阅读所有讲义时的速查表。

**任务**：浏览 [README.md:L9-L20](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/README.md#L9-L20) 的 Repository Map，为下列每个顶层子树写一句话「它提供什么」，并标注它**是否**进入顶层 `ruckus.tcl` 的 HDL 构建。

| 子树 | 一句话职责（用自己的话写） | 在 ruckus.tcl 中？（是/否） |
| --- | --- | --- |
| `base/` | | |
| `axi/` | | |
| `dsp/` | | |
| `devices/` | | |
| `ethernet/` | | |
| `protocols/` | | |
| `xilinx/` | | |
| `python/` | | |
| `tests/` | | |

**完成步骤**：

1. 职责列：从 README 的 Repository Map 抄要点，再点击进入对应子树 README（如 [base/README.md:L3](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/README.md#L3)、[protocols/README.md:L3](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/README.md#L3)）补充半句细节。
2. 构建列：对照 [ruckus.tcl:L15-L21](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ruckus.tcl#L15-L21)，填「是」或「否」。
3. 自检：`python/` 和 `tests/` 应当是「否」，其余 7 个应当是「是」。

**预期结果**：你得到一张 9 行的表，能据此判断任何需求「该去哪个子树找代码」，并理解软件/测试与 HDL 之间的边界。

## 6. 本讲小结

- SURF（SLAC Ultimate RTL Framework）是一套**跨板卡复用的 VHDL/IP/ruckus/cocotb/PyRogue 基础设施库**，不是单板工程（[AGENTS.md:L3](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L3)）。
- 顶层目录按「能力」而非「板卡」划分：`base / axi / dsp / devices / ethernet / protocols / xilinx` 是 HDL 子树，`python / tests` 是软件与测试（[README.md:L9-L20](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/README.md#L9-L20)）。
- 顶层 `ruckus.tcl` 只加载 7 个 HDL 子树，是 SURF 的构建骨架（[ruckus.tcl:L15-L21](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ruckus.tcl#L15-L21)）。
- `README.md` 提供「使用者视角」的导航地图，每个子树再各放一个简短的导航 README，层层下钻。
- `AGENTS.md` 是「贡献者视角」的规则手册，几乎每一节都对应后续一篇讲义；它是整本手册的「索引之索引」。
- 找代码的标准动线：顶层 README Repository Map → 子树 README 的 Layout → 子目录 `ruckus.tcl` / `*Pkg.vhd`。

## 7. 下一步学习建议

有了这张地图之后，建议按以下顺序继续：

1. **u1-l2 构建与仿真工具链**：弄清 `ruckus.tcl`、`Makefile`、GHDL、CI 是如何协作把这份仓库「跑起来」的——这是理解后续所有「跑测试」「做语法分析」的前提。
2. **u1-l3 目录结构与文件分类约定**：深入 `rtl/` / `sim/` / `tb/` / `wrappers/` 与家族 PHY 目录的拆分规则（AGENTS.md VHDL Conventions 一节的展开）。
3. **u1-l4 StdRtlPkg 基础类型**：开始接触真正的 VHDL，掌握 `sl` / `slv`、`_G` / `_C` 命名与复位泛型。
4. 想要高层背景，可先浏览 [README.md:L34-L38](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/README.md#L34-L38) 的两份 SURF 演讲链接作为补充阅读。

> 一句话提醒：本讲只建立了「地图」。从 u1-l2 起，你将开始真正打开 `.vhd` 文件、读懂 ruckus 清单和双进程 RTL——那时再回头对照 AGENTS.md 的相应章节，会有更深的体会。
