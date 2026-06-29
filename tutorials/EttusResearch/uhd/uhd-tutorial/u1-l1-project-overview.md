# UHD 项目总览：USRP 与 UHD 是什么

## 1. 本讲目标

本讲是整本《UHD 学习手册》的第一篇，目标是让一个**完全没接触过 USRP/UHD** 的读者，读完后能够回答下面这几个问题：

- USRP 是什么？UHD 又是什么？两者是什么关系？
- UHD 这个仓库里到底放了哪些东西，它们各自运行在哪里？
- UHD 是开源的吗？用什么许可证？支持哪些操作系统？
- UHD 的版本号怎么读？现在处于哪个版本？CHANGELOG 有什么用？

这些是后续所有讲义的地基。从第二讲开始，我们会进入源码、构建系统和具体 API，所以本讲务必把「项目定位」这一层先建立清楚。

## 2. 前置知识

本讲不需要你懂射频（RF），也不需要会写 C++。但有几个名词最好先有个直觉：

- **SDR（Software Defined Radio，软件无线电）**：传统无线电的收发规则（滤波、变频、调制解调）大多烧死在硬件电路里；SDR 的思路是「尽快把模拟信号变成数字样本，剩下的处理全交给软件」。这样同一块硬件，换一段代码就能解调 Wi‑Fi、GSM、LTE 或雷达。
- **USRP（Universal Software Radio Peripheral，通用软件无线电外设）**：Ettus Research 公司出品的一整套 SDR 硬件。你可以把它理解成一块「能收发射频、并且把原始数字样本通过 USB/千兆网/万兆网/PCIe 送给电脑」的板子。
- **驱动（driver）**：硬件本身只是一堆芯片，电脑要用它就得有一层软件负责「发现设备、配置参数、搬运样本」。对 USRP 来说，这层软件就叫 **UHD**。

如果你对「模拟信号 → 数字样本」这件事还想再具象一点，可以这样理解：声卡把声音变成一串整数样本交给电脑；USRP 把射频信号变成一串复数样本（I/Q 样本）交给电脑，只是采样率高得多、带宽宽得多。

## 3. 本讲源码地图

本讲只看仓库最顶层的几个「门牌文件」，不进任何子目录深处。这些文件本身都不是源代码逻辑，而是**说明项目是什么**的元信息文件：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目的「自我介绍」：定位、文档入口、支持的操作系统、生态、顶层目录分工。 |
| `LICENSE.md` | 许可证说明：默认 GPLv3，并列出 FPGA 等组件的特殊情况。 |
| `CHANGELOG` | 版本变更日志：每个版本新增了什么功能、修了什么 bug、有哪些 API 变动。 |
| `host/cmake/Modules/UHDVersion.cmake` | 真正定义版本号数字（主/次/ABI/补丁）的 CMake 脚本。 |
| `host/include/uhd/version.hpp.in` | 编译期版本宏与运行期版本查询函数的公共头文件模板。 |

记住一个原则：**README 告诉你「这是什么」，LICENSE 告诉你「能不能用、怎么用」，CHANGELOG 告诉你「现在到哪了、最近改了啥」**。

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

- **4.1 README**：项目定位与四大/六大组件分层。
- **4.2 版本与许可证**：版本号方案、CHANGELOG、GPLv3 许可证。

### 4.1 README：USRP 与 UHD 的分层关系

#### 4.1.1 概念说明

很多人第一次看到 USRP 和 UHD 会搞混。其实它们分属「硬件」和「软件」两层：

- **USRP™**：硬件平台（主板 + 子板 + 固件 + FPGA 镜像）。它由 Ettus Research 设计制造。
- **UHD™（USRP Hardware Driver）**：驱动软件。它是**自由且开源**的，提供统一的 API，让上层应用（GNU Radio、srsRAN、Matlab、LabVIEW……）可以用同一套代码驱动所有型号的 USRP。

一句话总结两者的关系：**USRP 是「身体」，UHD 是「神经系统 + 接口」**。没有 UHD，应用软件无法统一地指挥各种型号的硬件；没有 USRP，UHD 也就没有可驱动的对象。

UHD 的一个核心价值是**抽象屏蔽硬件差异**：README 明确说「UHD 支持所有 Ettus Research USRP 硬件，包括所有主板和子板及其组合」。也就是说，不管你插的是 B 系列（USB 小盒子）、X 系列（万兆网大板子）还是 N 系列，应用层面对的都是同一套 API。

#### 4.1.2 核心流程

从「一个应用想用 USRP」到「样本真正流动」的分层，可以粗略画成下面这样（自上而下）：

```
你的应用 / GNU Radio / srsRAN / Matlab ...
        │  统一调用 UHD 提供的 API（C++ / C / Python）
        ▼
┌──────────────────────────────────────────┐
│  UHD（host/）  ← 本手册的主角，跑在你的电脑上  │
│  设备发现 → 配置 → 样本收发 → 格式转换       │
└──────────────────────────────────────────┘
        │  通过 USB / 千兆网 / 万兆网 / PCIe
        ▼
┌──────────────────────────────────────────┐
│  USRP 硬件设备端                          │
│  ├─ MPM（mpm/）：设备端外设管理进程          │
│  ├─ firmware（firmware/）：单片机固件        │
│  └─ FPGA（fpga/）：FPGA 镜像（CHDR/RFNoC）   │
└──────────────────────────────────────────┘
```

这张图里出现了一个本讲要建立的**关键认知**：UHD 不是一个单独的二进制，而是一个**横跨主机端和设备端**的软件集合。README 的 *Directories* 一节把仓库分成了六大块，正好对应「跑在哪里」。

#### 4.1.3 源码精读

README 开篇一句话就讲清了 UHD 的定位：

[README.md:L1-L8](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/README.md#L1-L8) — 说明 UHD 是 USRP SDR 平台的「自由且开源的驱动与 API」，并支持所有 Ettus USRP 硬件（主板、子板及其组合）。

接着 README 用 *Directories* 一节交代了六大顶层目录各自的职责：

[README.md:L55-L84](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/README.md#L55-L84) — 把仓库分成 `host/`、`mpm/`、`firmware/`、`fpga/`、`images/`、`tools/` 六个部分。

整理成表格（这是后续讲义的目录索引）：

| 目录 | 运行位置 | 职责 |
| --- | --- | --- |
| `host/` | 主机（你的电脑） | 用户态驱动源码，本手册绝大多数讲义都在这里。 |
| `mpm/` | 设备端（嵌入式） | Module Peripheral Manager，现代设备（N/X 系列）上管理外设的进程。 |
| `firmware/` | 设备端（单片机） | USRP 硬件里各微处理器的固件源码。 |
| `fpga/` | 设备端（FPGA） | UHD FPGA 镜像（含 RFNoC / CHDR 数据通路）的源码。 |
| `images/` | 打包工具 | 固件与 FPGA 镜像的打包器，主要给维护者用。 |
| `tools/` | 主机 | 调试用辅助工具。 |

> 小提示：本学习手册主要围绕 `host/`（主机驱动）展开，因为它是大多数开发者唯一会直接打交道的部分；`mpm/`、`firmware/`、`fpga/` 只在高级篇里点到为止。

README 还说明了**支持的操作系统**和**生态**：

[README.md:L26-L36](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/README.md#L26-L36) — 主要在 Linux 上开发，同时测试并支持 Linux（Fedora/Ubuntu）、Mac OS X（Intel）、Windows 10。

[README.md:L38-L53](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/README.md#L38-L53) — 列出 UHD 可配合的上层工具链：GNU Radio、RFNoC OOT Blocks、NI LabVIEW、Matlab/Simulink、REDHAWK、OpenBTS、Osmocom、Amarisoft、srsRAN、OpenAirInterface 等。

> 这些生态信息很重要：它说明 UHD 不是「端到端应用」，而是**被上层 SDR 框架调用的底层库**。你学完 UHD 之后，通常会把它嵌进 GNU Radio 或 srsRAN 这类框架里使用。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，不需要任何硬件。

1. **实践目标**：用一张图把「UHD 的六大目录」和「它们运行在哪里」对应起来。
2. **操作步骤**：
   - 打开 [README.md:L55-L84](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/README.md#L55-L84)。
   - 在本地的仓库根目录运行 `ls`（或用文件浏览器），对照 README 里每个目录的描述。
3. **需要观察的现象**：你会看到本地确实存在 `host/ mpm/ firmware/ fpga/ images/ tools/` 这六个目录，它们和 README 文字一一对应。
4. **预期结果**：你能不看书地说出「`host/` 跑在主机上、`mpm/` 跑在设备端嵌入式系统上」。
5. **待本地验证**：仓库的实际目录列表需要你自己在本地执行 `ls` 确认，本讲不替你断言你机器上的状态。

#### 4.1.5 小练习与答案

**练习 1**：UHD 和 USRP 是同一个东西吗？如果不是，区别是什么？

> **参考答案**：不是。USRP 是硬件平台（主板 + 子板 + 固件 + FPGA 镜像），由 Ettus Research 制造；UHD 是驱动软件，开源、运行在主机上，通过统一 API 让应用软件指挥各种型号的 USRP。

**练习 2**：如果有人问你「UHD 算不算一个完整的应用层 SDR 软件（能直接解调 LTE）」，你怎么回答？依据 README 的哪一节？

> **参考答案**：不算。UHD 是底层驱动/库，负责发现设备、配置射频、搬运样本。真正解调 LTE 这类高层协议的是上层框架（如 srsRAN、OpenAirInterface）。依据是 README 的 *Applications* 一节（[README.md:L38-L53](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/README.md#L38-L53)），UHD 被列为这些框架的「可被调用」对象。

### 4.2 版本与许可证

#### 4.2.1 概念说明

知道项目「是什么」之后，还要知道两件工程上很关键的事：

1. **版本号怎么读**：UHD 的版本号不是简单的「主.次.补丁」，而是四段式（MAJOR.API.ABI.PATCH），每一段的含义不同。读懂它能帮你判断「升级会不会破坏我的程序」。
2. **许可证是什么**：开源不等于「随便用」。UHD 默认是 GPLv3，对商业闭源产品有约束；但 Ettus 提供了「替代许可证」的渠道。

#### 4.2.2 核心流程

UHD 的版本号由四个整数组成，定义在 CMake 脚本里。注释里明确写了每一段的含义：

- **MAJOR**：大范围库改动时递增。
- **API**：API 变化时递增。
- **ABI**：二进制接口（ABI）变化时递增——这一段直接决定你编译好的程序能不能链接新版本库。
- **PATCH**：bug 修复和文档时递增；在 master/开发分支上则用 git 计数代替。

为了让程序能在编译期或运行期检查版本，UHD 还把版本号压成一个整数宏，公式是：

\[
\text{UHD\_VERSION} = \text{MAJOR} \times 1000000 + \text{API} \times 10000 + \text{ABI} \times 100 + \text{PATCH}
\]

例如，对当前版本 4.10.0.0：

\[
\text{UHD\_VERSION} = 4 \times 1000000 + 10 \times 10000 + 0 \times 100 + 0 = 4010000
\]

> 这个整数宏的作用是：你写的代码可以用 `#if UHD_VERSION >= ...` 来做版本兼容判断，避免在新旧 API 之间写错。

至于许可证，核心规则只有三条：

1. UHD 和 MPM **默认**是 GPLv3。
2. 想要非 GPL 的替代许可证，需联系 `info@ettus.com`。
3. `fpga/` 目录有自己的许可证情况，需要单独看。

#### 4.2.3 源码精读

版本号四个整数的真正出处是 CMake 脚本：

[host/cmake/Modules/UHDVersion.cmake:L12-L29](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/cmake/Modules/UHDVersion.cmake#L12-L29) — 用 `set(UHD_VERSION_MAJOR 4)` 等四行定义版本，并用 `UHD_VERSION_DEVEL TRUE` 标明这是开发分支。

从这几行能直接读出**当前版本是 4.10.0.0**，且当前 HEAD 处于**开发（master）分支**状态。这与最近一次提交信息「Update to final 4.10.0.0 release candidate」一致。

> 注意：`UHD_VERSION_DEVEL TRUE` 意味着版本号里的 PATCH 在正式发布时会被 git 计数替换（参见同文件后续关于 `UHD_GIT_COUNT` 的逻辑）。所以你在 master 上看到的 `4.10.0.0` 实际是「下一个正式版的基底」。

CHANGELOG 的顶部条目印证了这一点：

[CHANGELOG:L1-L7](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/CHANGELOG#L1-L7) — 最新版本条目是 `004.010.000.000`，并列出主要变化（如支持 USRP X420、新增 timed complex gain 特性等）。

> CHANGELOG 是你判断「要不要升级」的第一手依据。每次升级 UHD 前，先翻 CHANGELOG 看有没有 *API Changes* 一节（本版本就有，例如 `meta_range_t` 和 `uhd::dict` 现在支持初始化列表），它会告诉你哪些代码可能要改。

公共头文件模板则把版本号暴露给上层程序：

[host/include/uhd/version.hpp.in:L16-L24](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/version.hpp.in#L16-L24) — 定义 `UHD_VERSION_ABI_STRING`（形如 `4.10.0` 的 ABI 串）和 `UHD_VERSION` 整数宏，并声明 `get_version_string()`、`get_abi_string()`、`get_component()` 三个运行期查询函数。

许可证则集中在 LICENSE.md 顶部：

[LICENSE.md:L1-L17](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/LICENSE.md#L1-L17) — 说明 UHD 和 MPM 默认 GPLv3，可联系 `info@ettus.com` 获取替代许可证；并指出 `fpga/` 有独立许可证情况。

[LICENSE.md:L18-L25](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/LICENSE.md#L18-L25) — 给出 GPLv3 License Text 的开头（GNU GENERAL PUBLIC LICENSE Version 3, 29 June 2007）。

> 工程上要记住一点：如果你的产品要**闭源商用**，GPLv3 会要求你开放派生作品的源码；这时通常需要走「替代许可证」渠道。`fpga/` 下的代码又是另一套规则，做硬件相关二次开发时必须单独确认。

#### 4.2.4 代码实践

这是一个**源码阅读 + 数据记录型实践**，无需硬件。

1. **实践目标**：独立确认 UHD 的当前版本号，并理解四段版本号的来源。
2. **操作步骤**：
   - 打开 [host/cmake/Modules/UHDVersion.cmake:L22-L29](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/cmake/Modules/UHDVersion.cmake#L22-L29)，记下 MAJOR/API/ABI/PATCH 四个数字。
   - 打开 [CHANGELOG:L1-L7](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/CHANGELOG#L1-L7)，看顶部条目是否与上面四个数字拼出来的版本一致。
   - 用本节的公式手算一遍整数宏 `UHD_VERSION`。
3. **需要观察的现象**：CMake 里的四段数字、CHANGELOG 顶部条目、最近一次 git 提交信息，三处对版本号的描述应当自洽。
4. **预期结果**：四段为 `4 / 10 / 0 / 0`，即版本 `4.10.0.0`；整数宏 `UHD_VERSION = 4010000`。
5. **待本地验证**：如果你切到别的 git tag/分支，这些数字会变，请以你本地 checkout 的版本为准重新核对。

#### 4.2.5 小练习与答案

**练习 1**：版本号 4.10.0.0 里，哪一段变化时最可能让你「重新编译并修改代码」才能用上新版 UHD？

> **参考答案**：**API** 段。API 变化意味着公共接口签名/语义改变，调用方代码通常要跟着改。ABI 段变化则意味着要重新编译链接但未必改代码；MAJOR 是大改；PATCH 一般只修 bug。

**练习 2**：你想用 UHD 做一个**闭源商业产品**，可以直接用仓库里默认的 GPLv3 版本吗？依据是哪里？

> **参考答案**：默认 GPLv3 一般不适合闭源商用（GPL 会要求派生作品也开源）。需要联系 `info@ettus.com` 申请替代许可证。依据是 [LICENSE.md:L1-L17](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/LICENSE.md#L1-L17)。

**练习 3**：怎么在不运行程序的前提下，预判升级到 4.10.0.0 会不会破坏现有代码？

> **参考答案**：读 [CHANGELOG](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/CHANGELOG) 顶部 `004.010.000.000` 条目里的 *API Changes* 一节，逐条核对是否会触及你用到的接口。

## 5. 综合实践

把本讲两块内容串起来，完成下面这个**项目简介小任务**（这也是本讲规格里指定的实践任务）：

1. 读 [README.md:L1-L8](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/README.md#L1-L8) 和 [CHANGELOG:L1-L20](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/CHANGELOG#L1-L20)。
2. 用**自己的话**写一段约 200 字的 UHD 项目简介，要求覆盖：
   - UHD 是什么、服务什么硬件；
   - 仓库里包含哪几大组件、分别运行在哪里；
   - 许可证一句话结论；
   - 当前主版本号（写出四段）。
3. 把这段简介存到你的学习笔记里（不要写进本仓库，本仓库只允许放在 `uhd-tutorial/` 下且本讲只写本文件）。
4. 自查：你写的版本号能否与 `UHDVersion.cmake`、CHANGELOG、git 提交信息三处对得上？

> 这一步看起来简单，但它强制你把「定位 + 组件 + 许可证 + 版本」四件事用自己的语言重组一遍，是后续读懂源码前最划算的一次投入。

## 6. 本讲小结

- **USRP 是硬件，UHD 是驱动**：UHD 是 Ettus USRP 平台自由开源的驱动与 API，用统一接口屏蔽不同型号硬件差异。
- **UHD 是横跨主机与设备端的软件集合**：`host/` 跑在主机，`mpm/` 跑在设备端嵌入式系统，`firmware/`、`fpga/` 是设备端固件/镜像，`images/`、`tools/` 是打包与调试辅助。
- **支持平台**：主要在 Linux 开发，官方支持 Linux（Fedora/Ubuntu）、Mac OS X（Intel）、Windows 10。
- **许可证**：UHD 与 MPM 默认 GPLv3，可申请替代许可证；`fpga/` 单独有自己的许可证规则。
- **版本号是四段式**（MAJOR.API.ABI.PATCH），当前为 **4.10.0.0**，且 HEAD 处于开发分支（`UHD_VERSION_DEVEL TRUE`）。
- **CHANGELOG 是升级的第一手依据**，升级前务必看 *API Changes* 一节。

## 7. 下一步学习建议

本讲只建立了「项目是什么」的认知，还没碰任何真正的源码逻辑。建议按下面的顺序继续：

1. **下一讲 u1-l2「仓库结构与四大组件」**：会带你逐层深入六大目录，特别是 `host/` 的内部组织，建立更细的代码地图。
2. 在读源码之前，建议先浏览 README 里给出的 [UHD and USRP Manual](http://files.ettus.com/manual/)（官方手册），它和本学习手册互补：手册讲「怎么用」，本手册讲「源码怎么实现」。
3. 想提前感受 UHD「长什么样」的读者，可以先跳到 u1-l5 看命令行工具，或 u1-l6 看第一个示例程序 `rx_samples_to_file`，但理解上仍建议先把 u1-l2～u1-l4 的结构与 API 头文件过一遍。

> 一句话：本讲回答「UHD 是什么、能不能用、现在到哪了」，下一讲开始回答「代码长什么样」。
