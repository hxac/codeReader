# PoC 项目总览与定位

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标只有一个：**让你在不动手写任何代码的前提下，搞清楚 PoC 到底是什么、为谁服务、目前处于什么状态。**

读完本讲你应该能够：

- 用一两句话向别人解释「PoC 是一个什么样的项目」。
- 说出 PoC 提供的至少三类硬件功能，并把它们和仓库里的目录对应起来。
- 说出项目由谁维护、采用什么开源许可证。
- 说清楚本仓库（`VLSI-EDA/PoC`）和它的新家 `VHDL/PoC` 之间的关系——这一点会直接影响你后续要不要在本仓库继续学习。

## 2. 前置知识

本讲几乎不需要任何前置知识，但下面几个名词会反复出现，先建立一个最粗浅的印象即可，不必死记：

- **IP 核（IP Core）**：在芯片/FPGA 设计里可以复用的硬件功能模块，类似于软件里的「库函数」。比如一个 FIFO、一个加法器，都可以是一个 IP 核。
- **HDL（Hardware Description Language，硬件描述语言）**：用来描述硬件行为的编程语言。PoC 主要用 **VHDL**，少量用 **Verilog**。
- **FPGA / ASIC**：FPGA 是可编程的芯片，ASIC 是定制芯片。PoC 的核主要面向 FPGA 设计，也能用于 ASIC。
- **仿真 / 综合（Simulation / Synthesis）**：仿真指在电脑上跑模型验证功能对不对；综合指把 HDL 代码「翻译」成真实电路（网表）。这俩是硬件开发的两条主流程。
- **厂商工具链（Vendor Tool Chain）**：Xilinx、Altera（现 Intel）、Lattice 等 FPGA 厂商各自的配套软件。PoC 要同时兼容多家，这是它的一大设计难点，也是后面多讲的核心主题。

> 如果上面某个词暂时没完全懂也没关系，本讲不会用到它们的细节，后续讲义会逐一展开。

## 3. 本讲源码地图

本讲只读「项目门面」文件，不进入任何 VHDL 代码。这些文件集中回答「项目是什么、归谁、怎么用、变成什么样了」：

| 文件 | 作用 | 本讲用它来回答 |
| --- | --- | --- |
| [README.md](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/README.md) | 项目主说明文档（自动生成） | 项目定位、能力清单、维护方、迁移现状、目录总览 |
| [README.tpl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/README.tpl) | README 的模板源文件 | 解释 README.md 是怎么「生成」出来的 |
| [LICENSE.md](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/LICENSE.md) | 许可证全文 | 采用哪种开源协议、能怎么用 |
| [CHANGES.md](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/CHANGES.md) | 变更日志 | 项目从诞生到 1.x 的时间线 |
| [AUTHORS.md](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/AUTHORS.md) | 作者与贡献者名单 | 谁在维护这个项目 |

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

- **4.1 项目定位与历史**——PoC 是什么、提供哪些能力、怎么一步步发展、现在搬到了哪里。
- **4.2 许可证与贡献者**——项目归谁、用什么协议、由谁维护。

### 4.1 项目定位与历史

#### 4.1.1 概念说明

**PoC** 是 **「Pile of Cores」** 的缩写，直译就是「一堆核」。它是一个**可复用的硬件 IP 核库**：把数字电路设计里反复要用到的常见功能（加法器、缓存、FIFO、RAM 包装器、各种 I/O 控制器等）事先实现好，打包成一个库，让设计师可以直接拿来用，而不必每次从零造轮子。

可以把它类比成：

- 软件世界的 **标准库**（如 C 的 `libc`、Python 的 `stdlib`）：提供一批「常用工具」。
- 前端世界的 **组件库**（如 Ant Design、Element）：提供一批「现成积木」。

理解 PoC 的关键有三点：

1. **它是源码形式的库**，不是黑盒 IP。核以 VHDL/Verilog 源代码提供，你可以读、可以改、可以集成进自己的工程。
2. **它强调可移植**。同一份描述尽量同时支持多家 FPGA 厂商，靠一套配置机制（`my_config` / `DEVICE_INFO`，后续讲义会专门讲）来选择具体实现。
3. **它自带基础设施**。因为核太多、工具链太杂，PoC 配了一套基于 Python 的命令行前端（pyIPCMI）来统一驱动仿真与综合。

#### 4.1.2 核心流程（项目自述的能力清单）

README 的 Overview 一段直接列出了 PoC 提供的硬件功能类别。我们可以把它整理成下面这张「能力 → 目录」对照表（目录名来自 `src/` 下的实际命名空间，本讲已核对存在）：

| README 自述的能力 | 英文原文 | 对应 `src/` 下的命名空间 |
| --- | --- | --- |
| 算术单元 | Arithmetic Units | `src/arith` |
| 缓存 | Caches | `src/cache` |
| 时钟域穿越电路 | Clock-Domain-Crossing Circuits | `src/misc/sync` |
| 先进先出队列 | FIFOs | `src/fifo` |
| 片上 RAM 包装器 | RAM wrappers | `src/mem`（如 `ocram`） |
| 输入输出控制器 | I/O Controllers | `src/io` |

> 名词小贴士：**时钟域穿越（CDC, Clock-Domain-Crossing）** 指信号从一个时钟区域传到另一个不同频率/相位的时钟区域，需要专门的同步电路来避免「亚稳态」。这是数字设计里很容易出 bug 的地方，所以 PoC 专门提供了 `sync` 系列核。

整个项目的「自述逻辑」可以这样概括（伪流程）：

```
PoC 提供一批硬件 IP 核（VHDL/Verilog 源码）
        │
        ├── 这些核共享一套公共 VHDL 包（types / 子程序 / 常量）
        ├── 还共享一套仿真辅助包，方便写测试台
        ├── 核按「子命名空间」分组，形成清晰层级
        └── 用一套 Python 基础设施统一驱动多家厂商的仿真/综合工具
```

也就是说：**核 + 公共包 + 分组层级 + 工具链前端**，这四样合起来才是完整的 PoC。本讲只看「核和分组」这一层，其余三样在后续单元逐步展开。

#### 4.1.3 源码精读

**① 最重要的「迁移公告」——读 README 第一眼就该看到**

README 顶部有一个醒目的警告，它决定了你对整个项目的认知。注意第 1 行那句注释，说明 README 本身是自动生成的：

[README.md:L1-L1](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/README.md#L1-L1) —— 声明 `README.md` 是由 `.tpl` 模板生成的，**不要直接手编**（这解释了为什么仓库里同时有 `README.md` 和 `README.tpl`）。

紧接着的警告块是全篇最关键的信息：

[README.md:L3-L8](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/README.md#L3-L8) —— 明确说明 **PoC-Library 已经 fork 到 `https://github.com/VHDL/PoC`**，后续开发、修 bug、处理 issue 都在新仓库进行，新仓库归 [Open-Source VHDL Group](https://github.com/VHDL) 所有，并用 OSVVM 测试台、GHDL 和 NVC 通过 GitHub Actions 检查代码。

> 对学习者的含义：本仓库（`VLSI-EDA/PoC`）是「历史版本/快照」，代码完整可读、可学；但如果你要追最新进展或提 issue，应该去 `VHDL/PoC`。本套讲义基于本仓库的 HEAD 讲解源码，学到的「机制」在新仓库依然适用。

**② 项目定位的一句话定义**

[README.md:L43-L46](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/README.md#L43-L46) —— 这就是 PoC 的官方定义：为常用硬件功能（算术、缓存、CDC、FIFO、RAM 包装器、I/O 控制器）提供实现，以 VHDL/Verilog 源码形式交付，便于在各种设计中复用。

**③ 为什么要有公共包和命名空间分组**

[README.md:L48-L51](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/README.md#L48-L51) —— 说明所有核共享一组公共 VHDL 包，并提供仿真辅助包；又因为核数量巨大，所以用「子命名空间」来构建清晰层级。这正是后面 u2（公共包）、u3（命名空间）两个单元要深挖的内容。

**④ 为什么要有 Python 基础设施**

[README.md:L53-L55](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/README.md#L53-L55) —— 解释了为了兼容多家厂商的免费/商业工具链，PoC 附带了一套基于 Python 的基础设施，提供命令行前端。这就是第 5 单元要讲的 pyIPCMI。

**⑤ 一句话看懂整个仓库的目录布局**

[README.md:L269-L286](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/README.md#L269-L286) —— 给出顶层目录一览：`src` 放源码、`tb` 放测试台、`lib` 放第三方库、`netlist`/`xst` 跟综合网表有关、`ucf` 放约束、`py` 放 Python 脚本等。这张表先混个眼熟，**详细的目录讲解在下一讲（u1-l2）**。

**⑥ README 是怎么生成出来的（顺带理解 README.tpl）**

[README.tpl:L1-L2](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/README.tpl#L1-L2) —— 模板第 1 行是 `{@GENERATED_HEADER@}` 占位符，说明生成时会被替换成真正的头部注释（也就是 `README.md` 第 1 行那句「DO NOT EDIT」）。

模板里到处是 `{@BRANCH@}` 这样的占位符，例如徽章链接里：

[README.tpl:L4-L5](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/README.tpl#L4-L5) —— CI 徽章里的分支名用 `{@BRANCH@}` 占位，生成 `release` 分支的 README 时就会被替换成 `release`。对比 `README.md` 里已经没有这些占位符，就能直观理解「模板 → 生成文档」的关系。

#### 4.1.4 代码实践（源码阅读型）

本讲没有可运行的代码，因此采用**源码阅读型实践**。

1. **实践目标**：凭自己的理解，说出 PoC 提供的硬件功能类别，并建立「功能 → 目录」的直觉；同时确认本仓库与新仓库的关系。
2. **操作步骤**：
   - 打开 [README.md 的 Overview 段](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/README.md#L41-L55)，找到那一句列举硬件功能的话。
   - 从中**挑出三类**你比较感兴趣的功能（例如：算术单元、FIFO、I/O 控制器）。
   - 用本讲 4.1.2 的对照表，把这三类功能映射到 `src/` 下的目录名。
   - 回到 README 顶部 [L3-L8](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/README.md#L3-L8)，确认当前仓库与 `VHDL/PoC` 的关系。
3. **需要观察的现象**：你会发现 README 列出的能力项，几乎都能在 `src/` 下找到一个同名的命名空间目录——这说明 README 的描述和实际代码组织是一一对应的。
4. **预期结果**：写出一段话，包含三句话，每句话形如「PoC 用 `src/<目录>` 提供 <能力>」，再加一句「本仓库 `VLSI-EDA/PoC` 已 fork 到 `VHDL/PoC`，后者由 Open-Source VHDL Group 维护」。
5. 由于不涉及命令执行，结果以你写出的文字为准，无需「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：README 说「PoC hosts a huge amount of IP cores」，所以采用了什么手段来保持清晰？请引用对应行号。

> **答案**：采用「子命名空间（sub-namespaces）分组」来构建层级。见 [README.md:L50-L51](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/README.md#L50-L51)。

**练习 2**：如果你发现 `README.md` 里有个错别字想改正，直接改 `README.md` 行不行？为什么？

> **答案**：不行。`README.md` 第 1 行明确写了「DO NOT EDIT! This file is generated from .tpl」，真正的源是 [README.tpl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/README.tpl)，应该改模板再重新生成。

**练习 3**：新仓库 `VHDL/PoC` 用哪两个开源仿真器来检查代码？

> **答案**：用 **GHDL** 和 **NVC**，通过 GitHub Actions 检查，并用 OSVVM 测试台。见 [README.md:L6-L7](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/README.md#L6-L7)。

### 4.2 许可证与贡献者

#### 4.2.1 概念说明

一个开源项目能不能放心用、能不能改、能不能放进商业产品，由两件事决定：

- **许可证（License）**：规定你可以怎么使用、修改、再分发这份代码的法律条款。
- **维护方 / 贡献者（Maintainers / Contributors）**：项目归谁所有、由谁持续维护——这关系到项目的可靠性和生命周期。

PoC 在这两点上对学习者都非常友好：

- 许可证是 **Apache License 2.0**，这是业界最宽松、最常用的开源协议之一，**允许商业使用、修改和再分发**，只要保留版权与免责声明。这对一个「要被嵌进别人工程」的 IP 库来说几乎是必备属性。
- 维护方是 **德累斯顿工业大学（Technische Universität Dresden, TU Dresden）** 计算机学院的 **VLSI 设计、诊断与架构教席（Chair for VLSI Design, Diagnostics and Architecture）**，是一个学术机构。

> 名词小贴士：**Apache 2.0** 相比 MIT 多了一条「专利授权」条款，简单说就是：贡献者把自己相关的专利权也一并授权给你用，对工业用户更安全。

#### 4.2.2 核心流程（许可证如何影响使用方式）

Apache 2.0 对使用者的核心约束可以归纳成下面的判断流程：

```
我想用 PoC 的某个核
   │
   ├─ 仅在自己内部用？ ───→ 可以，无需任何操作
   │
   ├─ 修改了 PoC 的源码并再分发？
   │        └─→ 必须：保留 LICENSE；在改过的文件上加显著「已修改」声明；保留版权声明
   │
   └─ 想申请专利保护自己的贡献？
            └─→ 注意：Apache 2.0 的专利授权是相互的，发起专利诉讼会导致授权终止
```

对绝大多数学习者来说结论很简单：**放心读、放心学、放心用到自己的 FPGA 工程里，只要别删 LICENSE 和版权声明即可。**

#### 4.2.3 源码精读

**① 许可证是什么、原文在哪**

[LICENSE.md:L1-L8](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/LICENSE.md#L1-L8) —— 第 1 行说明这是 Apache License 2.0 的本地副本，原文可从 apache.org 获取；第 8 行写明版本是「Version 2.0, January 2004」。README 的徽章里也印着 `Apache License 2.0`。

**② 维护方是谁**

[README.md:L21-L23](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/README.md#L21-L23) —— 明确写明项目由 **TU Dresden 的 VLSI Design, Diagnostics and Architecture 教席**发布和维护，并给出官网 `http://vlsi-eda.inf.tu-dresden.de`。

**③ 学术引用条目（也是确认项目身份的好地方）**

[README.md:L300-L309](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/README.md#L300-L309) —— README 提供了一个 biblatex 引用条目，标题写的是 `PoC - Pile of Cores`，组织是 TU Dresden，年份 2016。这进一步印证了项目的学术出处和正式名称。

**④ 具体的贡献者名单**

[AUTHORS.md:L1-L12](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/AUTHORS.md#L1-L12) —— 列出 8 位作者/贡献者（按字母序）：Genßler Paul、Köhler Steffen、Lehmann Patrick、Preußer Thomas B.、Reichel Peter、Schirok Jan、Voß Jens、Zabel Martin。从邮箱后缀看，主要来自 TU Dresden（`tu-dresden.de`），也有 Fraunhofer（`eas.iis.fraunhofer.de`）等机构。这是一份「真实的人」的名单，说明项目有明确的作者归属。

**⑤ 项目的时间线（从 CHANGES.md 看历史）**

[CHANGES.md:L306-L307](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/CHANGES.md#L306-L307) —— 最早的 `0.0` 版本，**Initial commit**，日期 **16.12.2014**（2014 年 12 月 16 日），这是 PoC 的「生日」。

[CHANGES.md:L41-L43](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/CHANGES.md#L41-L43) —— `1.0` 版本发布于 **13.05.2016**（2016 年 5 月 13 日），标记为「Python Infrastructure (Completely Reworked)」，即 Python 基础设施被完全重写。这也是后面 u5 单元要讲的命令行界面成型的时间点。

[CHANGES.md:L271-L303](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/CHANGES.md#L271-L303) —— `0.1` 版本（19.02.2015）已经能看到很多后续讲义要讲的命名空间雏形：`arith`、`fifo`、`mem.ocram`，以及公共包 `board/config/utils/strings/vectors`。这说明项目的目录结构很早就定型了。

把这几条串起来，PoC 的简史就是：

\[ \text{2014-12 初次提交} \;\xrightarrow{\text{半年迭代至 0.21}}\; \text{2015 年持续积累} \;\xrightarrow{\text{基础设施大重写}}\; \text{2016-05 发布 1.0} \;\xrightarrow{\text{后续 1.x 微调}}\; \text{之后 fork 到 VHDL/PoC} \]

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：确认 PoC 的许可证类型与维护方，并能在 AUTHORS.md 里找到具体的贡献者。
2. **操作步骤**：
   - 打开 [LICENSE.md:L1-L8](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/LICENSE.md#L1-L8)，确认版本号。
   - 打开 [AUTHORS.md:L1-L12](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/AUTHORS.md#L1-L12)，数一下共有多少位贡献者，挑出 1~2 位记下来。
   - 在 [CHANGES.md](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/CHANGES.md) 里找到「Initial commit」和「1.0」两个里程碑的日期。
3. **需要观察的现象**：你会看到 LICENSE.md 明确写了「Apache License / Version 2.0」；AUTHORS.md 是一张干净的表格，每个人对应一个 `tu-dresden.de`（或合作机构）邮箱。
4. **预期结果**：写出三句话：「PoC 采用 Apache License 2.0；由 TU Dresden 的 VLSI 教席维护；共有 8 位贡献者，例如 Patrick Lehmann、Thomas B. Preußer」；并给出 Initial commit（2014-12-16）与 1.0（2016-05-13）两个日期。
5. 结果以你阅读到的原文为准，无需「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：Apache 2.0 允许你把 PoC 的核用进商业 FPGA 产品吗？

> **答案**：允许。Apache 2.0 允许商业使用、修改和再分发，前提是保留 LICENSE 与版权/免责声明，且若再分发修改后的源码需在改动文件上加「已修改」标识。详见 [LICENSE.md](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/LICENSE.md) 的第 2、4 条。

**练习 2**：项目的「Initial commit」和「1.0」分别发生在什么时间？1.0 的关键变化是什么？

> **答案**：Initial commit 在 **2014-12-16**（[CHANGES.md:L306-L307](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/CHANGES.md#L306-L307)）；1.0 在 **2016-05-13**（[CHANGES.md:L41-L43](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/CHANGES.md#L41-L43)），关键变化是「Python Infrastructure (Completely Reworked)」——Python 基础设施完全重写。

**练习 3**：如果要在论文里引用 PoC，README 提供了什么？

> **答案**：提供了一个 biblatex 条目，`@online{poc, ...}`，标题 `PoC - Pile of Cores`，组织 TU Dresden，年份 2016。见 [README.md:L300-L309](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/README.md#L300-L309)。

## 5. 综合实践

把本讲两个模块串起来，完成一份**「PoC 一页速览卡」**（一张纸/一个 Markdown 文件即可）。要求包含以下五个板块，每个板块都要**引用一条本讲提到的源码行号或永久链接**作为依据：

1. **一句话定位**：用你自己的话写出 PoC 是什么（提示：IP 核库 + VHDL/Verilog 源码 + 可复用）。
2. **三类核心能力**：从 README 的能力清单里挑三类，写出「能力 → `src/` 目录」的映射。
3. **维护方与许可证**：写出维护机构和许可证名称。
4. **项目时间线**：写出 Initial commit 与 1.0 两个日期，并用一句话说明 1.0 的关键变化。
5. **仓库现状**：写出本仓库与新仓库 `VHDL/PoC` 的关系，并说明这对你学习本仓库的影响。

> 这个速览卡建议保存下来——它会在你读完整本手册后，作为「回头检验自己理解深度」的基准。

## 6. 本讲小结

- **PoC（Pile of Cores）** 是一个以 VHDL/Verilog 源码形式提供的**可复用硬件 IP 核库**，目标是让常见硬件功能不必重复造轮子。
- 它的核心能力覆盖**算术、缓存、时钟域穿越、FIFO、RAM 包装器、I/O 控制器**等，每类都能在 `src/` 下找到对应命名空间目录。
- 项目由 **TU Dresden 的 VLSI 教席**维护，采用 **Apache License 2.0**，允许商业使用与修改，对嵌入到其他工程非常友好。
- 项目 **2014-12-16 初次提交**，**2016-05-13 发布 1.0**（Python 基础设施大重写），之后 fork 到 **`VHDL/PoC`**（Open-Source VHDL Group 维护）继续发展。
- 本仓库是「可完整学习的历史快照」，新仓库是「最新进展所在地」——学机制看本仓库，追最新看新仓库。
- `README.md` 是由 `README.tpl` **自动生成**的，改文档要改模板而不是改生成物。

## 7. 下一步学习建议

本讲只建立了最外层的认知，还没真正走进代码。建议按下面的顺序继续：

1. **下一讲 u1-l2《仓库目录结构解析》**：逐个吃透 `src`、`tb`、`lib`、`netlist`、`ucf`、`xst` 等顶层目录，建立完整的「空间地图」。这是看懂任何后续源码的前提。
2. 之后再进入 **u1-l3《获取、运行与配置 PoC》**，亲手把仓库克隆下来、把 `my_config.vhdl` 配好。
3. 如果你已经急着想看真实 VHDL，可以先跳去 **u1-l4《VHDL 编码规范与命名约定》**，了解 PoC 的命名和文件组织约定；但目录地图（u1-l2）仍建议先读。
4. 阅读源码时，记得随时回来对照本讲的「能力 → 目录」表，确认自己没迷路。
