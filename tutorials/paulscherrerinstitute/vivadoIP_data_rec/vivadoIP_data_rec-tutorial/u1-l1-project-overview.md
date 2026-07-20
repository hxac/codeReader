# 项目总览：data_rec 是什么、能做什么

## 1. 本讲目标

本讲是整个学习手册的第一篇，面向「完全没接触过这个项目」的读者。读完本讲后，你应该能够：

- 说清楚 `vivadoIP_data_rec` 这个 IP 核是做什么的、典型用在哪些场景。
- 列出它的核心特性：前/后触发记录、自触发、多种触发模式、最多 8 通道、可配置采样深度。
- 知道项目由谁维护、采用什么许可证、依赖哪些 PSI FPGA 库。
- 知道版本是怎么演进的，特别是 v2.4 新增的 `Trig_Out` 端口解决了什么问题。
- 知道在哪里查阅完整的数据手册（datasheet）。

本讲**不**深入 VHDL 源码细节，那是后续讲义的任务。本讲只帮你建立「项目是什么」的整体印象，为后面的源码阅读打好索引。

## 2. 前置知识

在开始前，建议你大致了解以下概念（不熟悉也没关系，本讲会顺带解释）：

- **IP 核（IP Core）**：可复用的硬件功能模块。在 Xilinx/AMD Vivado 中，IP 核通常以 IP-XACT 形式打包，可以被拖进 Block Design（BD）里像积木一样使用。
- **FPGA**：现场可编程门阵列，一种可以通过代码（这里用 VHDL）配置硬件逻辑的芯片。
- **VHDL**：一种硬件描述语言，本项目的 RTL 源码用 VHDL-2008 编写。
- **数据记录（Data Recording）**：把连续到达的样本数据按触发事件前后的一段窗口「抓」下来存到存储里，供软件事后读取，类似于示波器的抓波形功能。
- **触发（Trigger）**：决定「从哪个时刻开始/围绕哪个时刻记录」的事件，是这类记录器最核心的概念。
- **AXI4**：ARM 定义的一种总线协议，Vivado IP 通常用 AXI4 Slave 接口让 CPU（如 MicroBlaze、Zynq 的 PS 端）读写寄存器与存储。本讲只需知道「软件通过 AXI 访问这个 IP」即可。

## 3. 本讲源码地图

本讲只涉及「文档层」，不读 RTL 源码。涉及的关键文件如下：

| 文件 | 作用 | 本讲用来讲什么 |
| --- | --- | --- |
| `README.md` | 项目总说明：维护者、许可证、依赖、功能列表、仿真入口 | 项目定位、核心特性、依赖与运行入口 |
| `Changelog.md` | 版本变更记录 | 版本演进，重点 v2.4 的 `Trig_Out` |
| `doc/data_rec.pdf` | 完整数据手册（datasheet） | 告诉读者详细文档在哪里、包含什么 |

后面讲义会逐步打开 `hdl/` 下的三个 VHDL 文件（`data_rec.vhd`、`data_rec_register_pkg.vhd`、`data_rec_vivado_wrp.vhd`），本讲先不展开。

## 4. 核心概念与源码讲解

### 4.1 项目说明（README）：data_rec 是什么

#### 4.1.1 概念说明

`vivadoIP_data_rec` 是瑞士保罗谢勒研究所（Paul Scherrer Institute, PSI）开发的一个**通用数据记录 IP 核（general purpose data recorder）**。它最常见的用途，是在 FPGA 系统里实现类似「示波器抓波形」的功能：当某个触发事件发生时，把触发前后一段时间内的多通道样本数据捕获下来，存到片内存储里，再由软件通过 AXI 总线读走。

理解它的关键，是抓住「**触发 + 采样窗口 + 多通道 + 存储**」这组词。下面三小节我们围绕这组词展开。

#### 4.1.2 核心流程（一次录制在概念上怎么发生）

虽然源码细节留给后面讲义，但从用户视角，一次完整的录制大致经历下面几个阶段。先建立一个直观印象即可：

```text
软件 Arm（装弹）
   │
   ▼
[ 预触发阶段 ]  ── 不停地把样本写进环形存储，但随时可能被新数据覆盖
   │              一直积累，直到攒够 PreTrigSpls 个「触发前」样本
   ▼
[ 等待触发 ]    ── 某个触发源到来（外部/软件/自触发）
   │
   ▼
[ 后触发阶段 ]  ── 继续记录 TotalSpls - PreTrigSpls 个「触发后」样本
   │
   ▼
[ Done 完成 ]   ── 置完成状态/中断，软件从存储里把这段窗口读走
```

这里有两个对初学者最容易混淆的概念，先点透：

- **前触发（Pre-Trigger）**：触发事件**之前**的样本。能记录前触发，意味着你可以看到「触发那一刻之前发生了什么」——这对排查偶发故障非常重要。
- **后触发（Post-Trigger）**：触发事件**之后**的样本。决定你能跟踪事件发生多久之后的行为。

`PreTrigSpls` 与 `TotalSpls`（总采样数）两个参数共同决定了录制窗口的形状，这部分会在第 3 单元的计数器讲义里严格推导。

#### 4.1.3 文档精读

README 对项目的定位只有一句话，但很关键。注意它把自己定义为「simple general purpose（简单、通用）」：

```text
This IP-core implements a simple general purpose data recorder.
```

完整定位与功能列表见 [README.md:L45-L55](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/README.md#L45-L55)，中文含义是：这是一个简单、通用的数据记录器；主要特性包括前/后触发记录、基于信号电平的自触发、多种触发模式、最多 8 通道、可配置采样深度。

把它逐条展开成中文表（这是后续讲义的索引，记住每条对应一个专题）：

| README 里的特性原文 | 含义 | 后续在哪讲 |
| --- | --- | --- |
| Pre- and Post-Trigger Recording | 前触发 + 后触发记录 | u3（核心记录器） |
| Self-Trigger (based on signal levels) | 基于信号电平的自触发 | u4-l4 |
| Different trigger modes (normal, free-running, self-trigger, external-trigger) | 四种触发模式 | u4 整个单元 |
| Up to 8 channels | 最多 8 路数据通道 | u3-l1（generics） |
| Configurable sample depth | 可配置的存储采样深度 | u3-l4 / u3-l5 |

> 提示：这五条特性基本就是本项目的「卖点清单」。后面所有讲义，本质上都是在解释这五条如何用 VHDL 实现。

#### 4.1.4 代码实践

**实践目标**：把 README 的英文特性列表内化为自己的理解，并区分「前/后触发」。

**操作步骤**：

1. 打开 [README.md:L45-L55](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/README.md#L45-L55)。
2. 把五条 Main features 抄下来，每条用一句中文解释。
3. 在纸上画一条时间轴，标出「触发点」，在它左边标出「前触发样本区」，右边标出「后触发样本区」。

**需要观察的现象**：你会发现自己能用一句话讲清「为什么要前触发」（看到事件发生之前的状态）。

**预期结果**：得到一张时间轴草图和一份中文特性表。本步无需运行任何工具，是纯阅读理解。

#### 4.1.5 小练习与答案

**练习 1**：README 说「Up to 8 channels」，这里的「8」是由什么决定的？
**答案**：由 IP 的 generic 参数 `NumOfInputs_g` 决定，最大可配置为 8。具体取值范围在第 3 单元讲 `data_rec` 实体 generics 时给出。

**练习 2**：如果一个偶发故障只在触发**之前**有征兆，你应该依赖「前触发」还是「后触发」？
**答案**：前触发。前触发样本记录的是触发点之前的数据，正好用来捕捉征兆；后触发只能看到触发之后的状态。

---

### 4.2 核心特性：四种触发模式与通道/深度

#### 4.2.1 概念说明

「触发模式」是这个 IP 最有特色的部分。README 在 [README.md:L51](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/README.md#L51) 明确列出了四种：normal、free-running、self-trigger、external-trigger。理解它们的差异，是理解整个 IP 行为的钥匙。

先解释三个反复出现的词：

- **外部触发（external trigger）**：由 IP 之外的信号（比如某个传感器输出的脉冲）来告诉记录器「现在该记」。
- **软件触发（software trigger）**：由软件写一个寄存器位来人为产生触发，常用于调试或自由循环录制。
- **自触发（self trigger）**：记录器**自己**看着输入数据，当数据落进某个设定的电平范围时自己触发，不需要外部信号。

#### 4.2.2 核心流程（四种模式分别怎么用）

下表把四种触发模式映射到「实际由谁产生触发」。注意：这些模式并不是互斥的硬件开关，而是「配置不同触发源使能 + 不同使用习惯」的统称。第 4 单元会从源码层面讲清楚 `TrigEna` 掩码如何同时使能多种触发源。

| 触发模式 | 触发由谁产生 | 典型用途 |
| --- | --- | --- |
| normal | 外部信号（外部触发） | 标准示波器式抓波形 |
| free-running | 软件触发，循环反复 | 不停地把数据切片抓出来观察 |
| self-trigger | 数据自身落入设定范围 | 抓偶发越限事件，无需外部接线 |
| external-trigger | 外部信号（可多路 OR） | 多个外部条件任一满足即触发 |

与触发模式并列的另两个特性是「**最多 8 通道**」和「**可配置采样深度**」：

- 通道数决定「同时记几路数据」，比如同时记 4 个传感器。
- 采样深度（sample depth）决定「每路最多记多少个样本」，由片内存储大小限制。

两者共同决定了 IP 内部存储资源的用量，也决定了端口位宽。具体推导在 u3-l1。

#### 4.2.3 文档精读

四种触发模式的原始表述只有一行，见 [README.md:L51](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/README.md#L51)：

```text
* Different trigger modes (normal, free-running, self-trigger, external-trigger)
```

最多 8 通道与可配置深度的表述见相邻两行 [README.md:L52-L53](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/README.md#L52-L53)。

关于「最多 8 路**外部**触发输入」还有一个历史细节值得注意：它在 v1.1.0 才从「只有 1 路」扩展到「最多 8 路、且每路可独立使能」，见 [Changelog.md:L43-L47](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/Changelog.md#L43-L47)。注意区分两个「8」：一个是 **8 个数据通道**，一个是 **8 个外部触发输入**，它们是两回事。

#### 4.2.4 代码实践

**实践目标**：建立「触发模式 ⇄ 触发源」的映射，避免把四个模式名字和实际来源对不上。

**操作步骤**：

1. 重新阅读 [README.md:L48-L54](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/README.md#L48-L54) 的完整特性列表。
2. 画一张表：左列是四种触发模式，中列是「谁产生触发」，右列是「我会把它用在什么场景」。
3. 在右列里至少为「self-trigger」写一个真实场景（提示：监测某信号是否越出正常范围）。

**需要观察的现象**：你会意识到「free-running」其实并不需要外部接线，它靠软件触发循环实现。

**预期结果**：得到一张三列表格。这是本讲综合实践所需表格的基础。

#### 4.2.5 小练习与答案

**练习 1**：free-running 模式主要靠哪种触发源实现？
**答案**：软件触发（software trigger）。软件反复置位触发位，从而不断重新启动录制。具体「sticky pending」机制在 u4-l3 讲。

**练习 2**：「8 通道」和「8 个外部触发输入」是同一个 8 吗？
**答案**：不是。「8 通道」指最多 8 路被记录的数据（`NumOfInputs_g`）；「8 个外部触发输入」指最多 8 路外部触发信号（`TrigInputs_g`），后者在 v1.1.0 引入，见 [Changelog.md:L43-L47](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/Changelog.md#L43-L47)。

---

### 4.3 版本变更（Changelog）与 Trig_Out 新端口

#### 4.3.1 概念说明

`Changelog.md` 记录了项目从早期版本到当前 v2.4 的演进。对学习者来说，Changelog 有两个价值：一是了解「这个功能是什么时候、为什么加的」，二是了解「哪些版本之间有兼容性陷阱」。

本模块的重点是当前 HEAD 对应的 **v2.4**，它引入了一个新的可选端口 `Trig_Out`。

#### 4.3.2 核心流程（版本演进的脉络）

按时间从新到旧，关键版本脉络如下（仅列与本讲相关的条目）：

```text
v2.4   ── 新增可选端口 Trig_Out（转发内部触发）        ← 当前 HEAD
v2.3.2 ── 修复：非二次幂 MemoryDepth_g 的回绕 bug
v2.3.1 ── 修复：Clk 与 s00_axi 之间错误的时钟关联
v2.3.0 ── 首次开源发布
v2.0.0 ── 改用开源后的新库版本（不向下兼容）
v1.1.0 ── 外部触发从 1 路扩展到最多 8 路、可独立使能
V1.00  ── 首次发布
```

两个值得记的「陷阱」版本：

- **v2.0.0**：标注为 *not reverse compatible*（不向下兼容），因为切换到了开源后的新库（psi_common 3.x 等）。如果你看到老资料提到不同的库版本，原因就在这里，详见 [README.md:L20-L35](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/README.md#L20-L35)。
- **v2.3.2**：修复非二次幂存储深度的回绕问题。这意味着「非二次幂深度」是一条曾出过 bug 的特殊代码路径，后续 u3-l5 会专门讲它。

#### 4.3.3 文档精读

v2.4 的 changelog 原文见 [Changelog.md:L1-L3](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/Changelog.md#L1-L3)：

```text
## 2.4
* New Features
  * New optional Trig_Out port added. It forwards the selected internal trigger used by the logic.
```

这句话信息量不小，拆开理解：

- **optional（可选）**：这个端口不是必须连的，由一个 generic（`TrigForwarding_g`，在 u4-l5 讲）控制是否启用。
- **Trig_Out**：端口名，字面意思是「触发输出」。
- **forwards the selected internal trigger**：它把 IP **内部已经选定并使用**的那个触发信号转发出去。也就是说，无论触发最终来自外部、软件还是自触发，经过 IP 内部裁决后得到的那个「真正用上的触发」，会从 `Trig_Out` 复制一份输出。
- **used by the logic**：强调转发的是「逻辑实际使用的那一个」，而不是原始的某一路输入。

git 历史也印证了这一点——当前 HEAD 的提交信息就是 [f68c931 "New Trig_Out optional port added to forward the trigger to other logic"](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/)。

**Trig_Out 解决的问题**：在 v2.4 之前，外部逻辑无法知道「这个 IP 实际是哪一拍触发的」。有了 `Trig_Out`，你可以把这个内部触发**转发给其他逻辑**，比如让另一块电路与本次录制精确同步，或者级联多个记录器一起抓同一段事件。这正是本讲实践任务要你总结的要点。

v2.3.x 两个修复见 [Changelog.md:L5-L11](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/Changelog.md#L5-L11)，首次开源发布见 [Changelog.md:L13-L16](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/Changelog.md#L13-L16)。

#### 4.3.4 代码实践

**实践目标**：用 Changelog 理解 v2.4 相对 v2.3 多了什么，并体会「转发内部触发」的价值。

**操作步骤**：

1. 打开 [Changelog.md:L1-L16](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/Changelog.md#L1-L16)。
2. 用 `git log --oneline` 查看最近提交，确认 HEAD 对应的正是 Trig_Out 那条提交。
3. 写两三句话：如果你要让「记录器 A 的触发同时去触发记录器 B」，v2.4 之前怎么做比较麻烦？v2.4 之后呢？

**需要观察的现象**：你会看到最新一次提交（HEAD）和 Changelog 顶部 v2.4 条目描述的是同一件事，两者可以互相印证。

**预期结果**：得到一段说明，指出 v2.4 的 `Trig_Out` 让「把内部触发转发给其他逻辑」变得直接（之前则需要外部额外接线或软协调）。

#### 4.3.5 小练习与答案

**练习 1**：`Trig_Out` 转发的是「某一路原始外部触发输入」还是「IP 内部最终用上的触发」？
**答案**：是后者。Changelog 原文 "the selected internal trigger used by the logic"，即经过内部裁决后实际生效的那个触发，见 [Changelog.md:L1-L3](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/Changelog.md#L1-L3)。

**练习 2**：v2.3.2 修复的是什么 bug？它暗示哪条代码路径需要特别小心？
**答案**：修复「非二次幂 `MemoryDepth_g` 的回绕问题」，见 [Changelog.md:L5-L7](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/Changelog.md#L5-L7)。它暗示「非二次幂存储深度」是一条需要特殊处理的路径，u3-l5 会专门讲解。

---

### 4.4 项目元信息与数据手册（doc/data_rec.pdf）

#### 4.4.1 概念说明

除了功能本身，一个成熟的开源 IP 还会带三类「元信息」：**谁维护、什么许可证、依赖什么库**。这些信息大多集中在 README 顶部，决定了你能不能、以及如何把这个 IP 用到自己的工程里。

同时，README 多次指向一份 PDF **数据手册** `doc/data_rec.pdf`，它是本项目最权威的详细文档（寄存器地图、时序图、端口定义等都以它为准）。本模块教你「去哪里找权威细节」。

#### 4.4.2 核心流程（如何定位权威信息）

当你对某个细节有疑问时，建议按下面的顺序查找，避免只看二手资料：

```text
有疑问
  │
  ├─ 寄存器地址 / 字段位 ──► doc/data_rec.pdf 数据手册
  │                          （并对照 hdl/data_rec_register_pkg.vhd）
  ├─ 端口 / 时钟域 ────────► doc/data_rec.pdf
  │                          （并对照 hdl/data_rec_vivado_wrp.vhd）
  ├─ 版本差异 ─────────────► Changelog.md
  └─ 总体定位 / 依赖 / 运行 ► README.md
```

记住一句话：**README 是入口，PDF 是权威，源码是事实**。三者不一致时，以源码为准并对照 PDF。

#### 4.4.3 文档精读

项目元信息（维护者、作者、许可证、详细文档入口）见 README 顶部 [README.md:L1-L16](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/README.md#L1-L16)。要点摘录：

- **Maintainer（维护者）**：Waldemar Koprek（PSI）。
- **Author（作者）**：Oliver Bründler。
- **License（许可证）**：PSI HDL Library License，本质是 LGPL2.1 加上一些针对固件开发的例外条款。商业使用前请阅读 `License.txt` 与 `LGPL2_1.txt`。
- **详细文档**：明确指向 `doc/data_rec.pdf`，见 [README.md:L12-L13](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/README.md#L12-L13) 与 [README.md:L55](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/README.md#L55)。

依赖关系（决定你能否构建/仿真）见 [README.md:L20-L35](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/README.md#L20-L35)，整理成表：

| 类别 | 依赖 | 版本要求 | 用途 |
| --- | --- | --- | --- |
| TCL | PsiSim | ≥ 2.1.0 | 仿真框架（仅开发需要） |
| TCL | PsiIpPackage | 2.1.0 | IP 打包脚本（仅开发需要） |
| VHDL | psi_common | ≥ 3.0.0 | 通用 RTL 组件（AXI slave、跨时钟域、RAM 等） |
| VHDL | psi_tb | ≥ 3.0.0 | 仿真验证辅助库 |

> 提示：也可以直接使用聚合仓库 [psi_fpga_all](https://github.com/paulscherrerinstitute/psi_fpga_all)，它把上述库以 submodule 形式放在正确的目录结构里。本仓库在其中的相对路径，将在 u1-l2 讲清。

关于数据手册本身：`doc/data_rec.pdf` 是一份二进制 PDF（同目录还有源文件 `doc/data_rec.docx` 与框图 `doc/data_rec.vsd`）。它通常包含：功能概述、端口与 generics 说明、**寄存器地图**、时序图、典型使用方式。

> **待本地验证**：由于本讲义编写环境无法直接渲染该 PDF，数据手册的具体章节标题与页码请你在本地打开 `doc/data_rec.pdf` 后确认。后续讲义中涉及寄存器地址时，会以源码 `hdl/data_rec_register_pkg.vhd` 为准，并提示你到 PDF 中对照。

仿真入口（供你提前知道「怎么跑起来」，详见 u1-l3）见 [README.md:L57-L63](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/README.md#L57-L63)：在 Modelsim 的 `sim` 目录里执行 `source ./run.tcl` 即可跑回归测试。

#### 4.4.4 代码实践

**实践目标**：亲手打开数据手册，建立「PDF 是权威」的习惯，并核对依赖。

**操作步骤**：

1. 在本地用 PDF 阅读器打开 `doc/data_rec.pdf`。
2. 浏览目录，找到「寄存器地图（Register Map）」相关章节，记下它所在的页码。
3. 打开 [README.md:L20-L35](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/README.md#L20-L35)，确认 `psi_common`、`psi_tb` 的最低版本要求。
4. （可选）阅读 `License.txt`，确认 LGPL 例外条款是否允许你的使用场景。

**需要观察的现象**：你会看到数据手册里有比 README 详细得多的端口表与寄存器表。

**预期结果**：记录下寄存器地图章节的页码（**待本地验证**），并写出 psi_common / psi_tb 的最低版本（3.0.0）。本步需要本地 PDF 阅读器；若环境没有，标注「待本地验证」即可。

#### 4.4.5 小练习与答案

**练习 1**：当 README 的简述和 `doc/data_rec.pdf` 的详细描述不一致时，应以哪个为准？
**答案**：以 PDF 数据手册为准，并最终以源码为事实。README 只是入口级概述，细节以 PDF 和源码为准。

**练习 2**：如果你只想「使用」这个 IP（不做开发、不跑仿真），是否必须安装 PsiSim 和 PsiIpPackage？
**答案**：不必。README 标注这两个是 "for development only"，见 [README.md:L26-L28](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/README.md#L26-L28)。普通使用只需打包好的 IP 即可。

---

## 5. 综合实践

把本讲学到的内容串成一张总表。请完成下面的「一页纸速查表」，这是本讲唯一的交付物，也是后面所有讲义的索引。

**任务**：

1. **触发模式表**：列出 README 中四种触发模式（normal / free-running / self-trigger / external-trigger），各写明「触发由谁产生」和「一个典型场景」。
2. **Trig_Out 说明**：用一两句话说明 v2.4 相对 v2.3 新增的 `Trig_Out` 端口的作用，引用 [Changelog.md:L1-L3](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/Changelog.md#L1-L3) 的原文。
3. **我的使用场景**：结合自己的工作/学习，写一段话：你会把这个 IP 用在哪个具体场景？需要几通道、用什么触发模式、是否需要 `Trig_Out`？

**参考答案骨架**（请用自己的话补全场景部分）：

| 触发模式 | 触发由谁产生 | 典型场景 |
| --- | --- | --- |
| normal | 外部信号 | 标准示波器式抓波形 |
| free-running | 软件触发，循环 | 连续不断地切片观察数据流 |
| self-trigger | 数据落入设定电平范围 | 抓偶发越限，无需外部接线 |
| external-trigger | 多路外部信号 OR | 多个外部条件任一满足即记录 |

`Trig_Out` 说明示例：v2.4 新增的可选端口 `Trig_Out` 会把「IP 内部实际使用的那个触发」转发出来（[Changelog.md:L1-L3](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/Changelog.md#L1-L3)），用于让其他逻辑与本次录制同步或级联多个记录器。

> 提示：场景部分没有标准答案，但应至少包含「通道数」「触发模式」「是否需要 Trig_Out」三个要素。

## 6. 本讲小结

- `vivadoIP_data_rec` 是 PSI 开发的**通用多通道数据记录 IP 核**，行为类似示波器抓波形，见 [README.md:L45-L55](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/README.md#L45-L55)。
- 五大特性：前/后触发记录、基于电平的自触发、四种触发模式、最多 8 通道、可配置采样深度。
- 四种触发模式为 normal、free-running、self-trigger、external-trigger，分别对应外部信号、软件循环、数据自身、多路外部 OR。
- 当前版本为 **v2.4**，新增可选端口 `Trig_Out`，转发 IP 内部实际使用的触发信号，便于与其他逻辑同步/级联，见 [Changelog.md:L1-L3](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/Changelog.md#L1-L3)。
- 权威细节查 `doc/data_rec.pdf`；项目依赖 psi_common ≥ 3.0.0、psi_tb ≥ 3.0.0，仿真与打包工具 PsiSim / PsiIpPackage 仅开发需要。
- 记住口诀：**README 是入口，PDF 是权威，源码是事实**。

## 7. 下一步学习建议

本讲只建立了「项目是什么」的印象，还没碰目录结构与源码。建议按手册顺序继续：

- **下一讲 u1-l2《仓库目录结构与外部依赖》**：搞清 `hdl / testbench / sim / scripts / epics / doc` 等目录的职责，以及 PSI FPGA 库的文件夹结构约定。这是阅读任何源码前的导航课。
- 之后 **u1-l3** 学会在 Modelsim/PsiSim 里把回归仿真跑起来，**u1-l4** 学会把工程打包成 Vivado IP。
- 等第一单元结束，再进入 u2（顶层端口与寄存器地图）、u3（核心记录器状态机）。

在进入下一讲前，建议你先把本讲的「综合实践」总表做完——它会在整本手册里反复被引用。
