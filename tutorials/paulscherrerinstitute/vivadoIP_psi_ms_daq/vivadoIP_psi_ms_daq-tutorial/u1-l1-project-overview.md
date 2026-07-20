# 项目定位：psi_ms_daq 是什么、这个仓库做什么

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标是帮读者在接触任何源码之前，先建立一个清晰的「项目画像」。读完本讲，你应该能够：

- 用一句话说清 **psi_ms_daq** 这个 IP-Core 解决了什么问题（多通道数据采集、直接写内存）。
- 严格区分两个容易混淆的仓库：本仓库 `vivadoIP_psi_ms_daq`（Vivado 封装层）与 `psi_multi_stream_daq`（真正的 VHDL 功能实现），并知道「改功能」要去哪里改。
- 看懂项目的版本号约定（`major.minor.bugfix`）与许可证（PSI HDL Library License ≈ LGPL + FPGA 例外条款）。
- 学会阅读 `README.md` 与 `Changelog.md` 这两个最重要的「项目自述文件」，并能据此回答版本之间的差异。

本讲**不涉及任何 VHDL 或 C 代码细节**，只读文档。后续讲义才会进入源码。

---

## 2. 前置知识

本讲面向零基础读者，但有几个名词先解释清楚会更顺畅：

- **IP-Core（知识产权核）**：在 FPGA（现场可编程门阵列）开发中，一段可复用的硬件功能模块称为一个 IP-Core。Vivado（Xilinx/AMD 的 FPGA 开发工具）允许把 IP-Core 封装成一个带图形界面的「积木」，拖进 Block Design（原理图式连线）里直接例化使用。
- **AXI（Advanced eXtensible Interface）**：ARM 提出的一种片上总线协议，FPGA 里几乎 everywhere。本项目中至少有两类 AXI 接口：
  - **AXI Slave**：CPU（如 Zynq 的 ARM 核）用它读写 IP 内部的「寄存器」，从而配置和控制 IP。
  - **AXI Master**：IP 主动发起读写，本项目里用它把采集到的数据**直接写进 DDR 内存**，不需要 CPU 搬运。
- **AXI-Stream（AXIS）**：AXI 的「流」变种，专门用来传一串连续的数据（带 `TValid/TReady/TLast` 等握手信号）。本项目里每一路被采集的数据源就是一路 AXI-Stream 输入。
- **DDR（Double Data Rate SDRAM）**：开发板上的主内存。所谓「直写 DDR」就是采集到的样本不经过 CPU，由硬件直接落盘到内存。
- **触发（Trigger）与窗口（Window）**：采集时往往不是无脑一直录，而是围绕某个「事件（触发）」录一段样本，这段样本叫一个「窗口」。本项目每条流最多支持 32 个这样的窗口。

> 一句话直觉：**psi_ms_daq 是一个「多路数据采集卡」的 FPGA 版本，它把多路输入流的数据自动写到内存里，CPU 只需要事后去取。**

---

## 3. 本讲源码地图

本讲只读三个纯文档文件，它们都在仓库根目录：

| 文件 | 作用 | 本讲用法 |
| --- | --- | --- |
| `README.md` | 项目自述：维护者、许可证、文档入口、依赖、功能描述（Description）、封装层声明（Important Note） | 看清「它是什么」与「它不是什么」 |
| `Changelog.md` | 版本变更日志，记录每个版本做了什么 | 看清版本演进与「1.2.1 改了什么」 |
| `License.txt` | PSI HDL Library License 全文（LGPL + FPGA 例外） | 看清「能不能商用/改源码」 |

> 这三个文件是后续所有讲义的「地基」。比如版本号、依赖关系、封装层定位，全都会在后面的讲义里反复出现。

仓库整体只有 41 个被 git 跟踪的文件，规模很小，但横跨 VHDL / TCL / C / IP-XACT 多种语言。本讲只关心文档，其他目录（`hdl/`、`scripts/`、`drivers/`、`refdesign/` 等）留到下一讲（u1-l2）再逐个介绍。

---

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

- **4.1 IP-Core 功能与本仓库定位**：精读 `README.md` 的 Description、Important Note 与 License。
- **4.2 版本演进**：精读 `Changelog.md` 的版本历史。

---

### 4.1 IP-Core 功能与本仓库定位

#### 4.1.1 概念说明

要理解这个项目，必须先分清「**它做什么**」和「**它在哪个仓库实现**」两件事，否则很容易在错误的仓库里改代码。

- **它做什么**：psi_ms_daq 是一个**通用多流（multi-stream）数据采集引擎**。它能同时接收多路输入数据流，把数据**直接写进一块通过 AXI 连接的内存**（典型就是 DDR），全程不需要 CPU 参与「搬运」。CPU 只需要在采集完成后，去内存里把数据读走处理。
- **它在哪个仓库实现**：这是本仓库最容易踩坑的地方。真正的功能逻辑（VHDL 代码、状态机、缓冲管理）**不在本仓库**，而在另一个仓库 `psi_multi_stream_daq` 里。本仓库 `vivadoIP_psi_ms_daq` **只提供一个 Vivado 封装层（wrapper）**——把那个通用 VHDL 实现包装成可以在 Vivado Block Design 里例化的 IP-Core。

这个「封装 vs 实现」的划分会贯穿整本手册。记住一个原则：**想改采集功能 → 去 `psi_multi_stream_daq`；想改 Vivado 打包方式/端口暴露 → 留在本仓库。**

#### 4.1.2 核心流程

从用户视角，这个 IP-Core 的数据流可以画成一条单向链路：

```text
   多路数据源                 psi_ms_daq IP-Core                    内存
 ┌────────────┐   AXI-Stream   ┌──────────────────────────┐   AXI Master   ┌──────┐
 │ 流0 ~ 流15 │ ─────────────▶ │ 采集 + 触发判定 + 缓冲   │ ─────────────▶ │ DDR  │
 │ (最多16路) │   (握手信号)   │ (线性/环形缓冲)          │  (直接写)      │      │
 └────────────┘                └──────────────────────────┘                └──────┘
        ▲                                   ▲
        │                                   │ AXI Slave
        │                                   │ (CPU 配置寄存器/读状态)
   外部触发信号                          Zynq ARM 核 / 其它 CPU
```

要点：

1. **输入端**：最多 **16 路** AXI-Stream，每路可以有**不同的位宽**。
2. **触发与窗口**：在软件来读数据之前，最多可以为 **32 个触发** 各自记录一段样本（窗口）。也就是说硬件能「先攒着」，等软件有空再来取，不会因为 CPU 反应慢而丢数据。
3. **缓冲模式**：支持**线性缓冲**和**环形缓冲**两种布局。
4. **输出端**：通过 AXI Master 把数据直写内存。带宽指标在文档里写明：**64 位内部数据通路，250 MHz 下可达 2 GB/s**。换算如下：

\[
\text{吞吐} = \frac{64\ \text{bit}}{8} \times 250 \times 10^{6}\ \text{Hz} = 8\ \text{B} \times 2.5 \times 10^{8}\ \text{s}^{-1} = 2 \times 10^{9}\ \text{B/s} = 2\ \text{GB/s}
\]

5. **附赠参考设计**：仓库还带了一个完整参考设计（ZCU102 开发板），后续 u5 单元会精读。

#### 4.1.3 源码精读

**Description（功能描述）** —— 这是 README 里对 IP 能力的权威列举，位于 [README.md:54-62](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/README.md#L54-L62)：

> 这段代码把 IP 定义为「通用多流数据采集引擎，直接写 AXI 内存」，并列出 6 条主要特性：16 路、32 触发、线性/环形缓冲、64 位 @ 250 MHz = 2 GB/s、附带参考设计。**这是判断「这个 IP 能不能干某件事」的第一手依据。**

**Detailed Documentation（文档去向）** —— 注意 README 在 [README.md:15-20](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/README.md#L15-L20) 里明确写道：「主要文档在 `psi_multi_stream_daq` 仓库……逻辑实现放在那个通用 VHDL 仓库里」。这从文档角度再次印证了 4.1.1 的封装定位。

**Important Note（封装层声明）** —— 这是本仓库最关键的一句话，位于 [README.md:64-65](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/README.md#L64-L65)：

> 「本仓库**只包含一个 wrapper**，封装的是 `psi_multi_stream_daq` 里的 VHDL 功能实现。因此任何功能性的改动都必须在那边实现。」
>
> 这条 Note 直接决定了贡献者的工作流：在本仓库改 RTL 通常是徒劳的，因为本仓库唯一的 RTL 文件 `hdl/psi_ms_daq_vivado.vhd` 只是把信号「转接」给 `psi_multi_stream_daq` 提供的实体（详见 u2 单元）。

**License（许可证）** —— README 在 [README.md:9-10](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/README.md#L9-L10) 声明采用 **PSI HDL Library License**，并说明它本质是 **LGPL 加上一些额外例外**。完整例外条款在 [License.txt:15-22](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/License.txt#L15-L22)：

> 例外条款允许你**以自己的条款**使用、复制、链接、修改和分发「基于本库的**二进制形式**作品或**包含二进制的硬件**」，并且明确把 **FPGA 比特流（bitstream）/ flash 镜像**也算作「二进制」。
>
> 直白说：**你拿这个 IP 去做产品、烧进 FPGA 出货，不需要把你的整个工程源码开源**（这一点比纯 LGPL 对固件开发者更友好）；但如果你**修改了本库本身的源码**并分发，仍受 LGPL 约束。

#### 4.1.4 代码实践

> 这是一个**源码阅读型实践**，不需要运行任何命令。

1. **实践目标**：用一段话（3–5 句）说清「本仓库为何只是封装层、真正功能在哪里」。
2. **操作步骤**：
   - 打开 [README.md:54-62](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/README.md#L54-L62)（Description）和 [README.md:64-65](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/README.md#L64-L65)（Important Note）。
   - 再看一眼 [README.md:15-20](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/README.md#L15-L20)（文档去向）。
3. **需要观察的现象**：你会注意到 README 里反复把读者「往外推」到 `psi_multi_stream_daq` 仓库（文档、实现都在那里）。
4. **预期结果**：你应该能写出类似下面这样的回答：
   > 「本仓库 `vivadoIP_psi_ms_daq` 只是 Vivado IP 的封装层（wrapper），它把 `psi_multi_stream_daq` 仓库里实现的通用 VHDL 多流数据采集引擎，包装成可在 Vivado Block Design 中例化的 IP-Core。真正的采集、触发、缓冲等功能逻辑都在 `psi_multi_stream_daq` 里实现，所以改功能要去那个仓库，而不是本仓库。」
5. 此结论无需运行验证——它直接来自 README 文本。

#### 4.1.5 小练习与答案

**练习 1**：如果有人想给采集引擎增加一种新的触发模式，应该给哪个仓库提 PR？为什么？

> **答案**：应该给 `psi_multi_stream_daq` 提。因为本仓库（`vivadoIP_psi_ms_daq`）只是封装层，[README.md:64-65](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/README.md#L64-L65) 明确说「任何功能性的改动都必须在那里实现」。

**练习 2**：某公司把这个 IP 烧进自家 FPGA 板子去卖，却不愿公开自家工程的 Verilog 源码，这违反许可证吗？

> **答案**：不违反。License.txt 的例外条款（[License.txt:15-22](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/License.txt#L15-L22)）明确把 FPGA 比特流/flash 镜像算作「二进制」，允许以自有条款分发包含该二进制的硬件，不强制公开工程源码。例外仅当该公司**修改并分发本库源码本身**时才需要额外处理。

---

### 4.2 版本演进

#### 4.2.1 概念说明

光知道「它是什么」还不够，还要知道「现在是哪个版本、版本之间差在哪」。本项目采用一套清晰的**语义化版本号**约定，写在 README 的 Tagging Policy 里（[README.md:22-27](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/README.md#L22-L27)）：格式为 `major.minor.bugfix`。

| 版本号位 | 何时自增 | 含义 |
| --- | --- | --- |
| `major` | 改动**不完全向下兼容**时 | 破坏性变更（升级要小心） |
| `minor` | 新增功能时 | 向下兼容的新特性 |
| `bugfix` | 仅修 bug、无功能变化时 | 纯修复 |

版本的具体内容则记录在 `Changelog.md`，当前最新版本是 **1.2.1**（仓库 git HEAD 对应此版本）。

#### 4.2.2 核心流程

阅读 Changelog 的正确姿势是「**自上而下**」：最上面是最新版本，每条版本下列出 Features（新功能）、Changes（变更）、Bugfixes（修复）。把版本号和 Tagging Policy 对照着看，就能判断每次升级的风险等级。

```text
读到一条 Changelog 条目
        │
        ▼
看版本号哪一位变了？
        │
 ├── bugfix 变  → 纯修复，升级风险最低
 ├── minor 变  → 有新功能，向下兼容，一般安全
 └── major 变  → 不向下兼容，必须检查调用方
```

#### 4.2.3 源码精读

`Changelog.md` 全文很短，完整四个版本如下。

**1.2.1（当前版本）** —— 见 [Changelog.md:1-4](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/Changelog.md#L1-L4)，只有两条 Bugfix：

1. **用 `psi_multi_stream_daq` 的 1.2.1 版本 VHDL 仓库来构建本 IP（含新驱动）**。注意这条「修复」的本质是**跟着上游功能仓库一起升级**——这正是「封装层」的典型特征：本仓库的「修复」很多时候就是把底层实现换到新版本。
2. **修复了参考设计工程里的绝对路径问题**（让工程在不同机器/目录下可移植）。

**1.2.0** —— 见 [Changelog.md:6-8](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/Changelog.md#L6-L8)：首次开源发布（更早的版本未保留在历史中），并添加了许可证与版权头。

**1.1.0** —— 见 [Changelog.md:10-13](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/Changelog.md#L10-L13)：新增依赖解析脚本，并把 AXI Slave 从旧版换成 `psi_common` 提供的版本。

**1.0.0** —— 见 [Changelog.md:14-15](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/Changelog.md#L14-L15)：首次发布。

> 对照 Tagging Policy：从 1.2.0 → 1.2.1 只有 `bugfix` 位变化，说明**没有新功能、也没有破坏性变更**，是一次低风险的纯修复升级。这也意味着：把项目从 1.2.0 升到 1.2.1 通常是安全的，主要收益是拿到上游新驱动和参考设计的路径修复。

#### 4.2.4 代码实践

1. **实践目标**：明确说出「1.2.1 相对 1.2.0 修复了什么」。
2. **操作步骤**：
   - 打开 [Changelog.md:1-8](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/Changelog.md#L1-L8)。
   - 对比 1.2.1 和 1.2.0 两节。
   - 再对照 [README.md:22-27](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/README.md#L22-L27) 的 Tagging Policy，判断这次升级的风险等级。
3. **需要观察的现象**：1.2.1 全是 Bugfixes，没有 Features / Changes；版本号只有第三位变化。
4. **预期结果**：你应该能写出类似——
   > 「1.2.1 相对 1.2.0 做了两处修复：(1) 用 `psi_multi_stream_daq` 1.2.1 版的 VHDL 仓库重新构建本 IP（顺带带入了新驱动）；(2) 修复参考设计工程中的绝对路径。因为只有 bugfix 位变化、且没有功能新增或破坏性改动，按 README 的 Tagging Policy，这是一次向下兼容的纯修复升级。」
5. 此结论无需运行验证——它直接来自 Changelog 文本。

#### 4.2.5 小练习与答案

**练习 1**：如果下一个版本号是 `1.3.0`，按 Tagging Policy 你预期它会包含哪类改动？升级时要注意什么？

> **答案**：`minor` 位变化意味着**新增了向下兼容的新功能**（可能有 Features）。升级一般安全，但应阅读 Changelog 确认新功能不影响现有调用方式。

**练习 2**：某用户报告「我从 1.1.0 升到 1.2.1 后，AXI Slave 的行为变了」。结合 Changelog，给出一个可能的原因假设。

> **答案**：1.1.0 的 Changes（[Changelog.md:12-13](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/Changelog.md#L12-L13)）记录了「改用 `psi_common` 的 AXI Slave，替换旧版本」。如果用户原本依赖旧 AXI Slave 的某些行为，跨版本升级时这一变更可能就是原因。这是「读 Changelog 复现问题」的典型用法。

---

## 5. 综合实践

把 4.1 和 4.2 串起来，完成下面这个「项目速览卡」小任务：

假设你要向一位新同事口头介绍这个项目，请基于本讲读过的三个文件，准备一张不超过 6 行的「速览卡」，必须涵盖以下五点（缺一不可）：

1. **一句话定位**：psi_ms_daq 是什么（输入是什么、输出到哪里）。
2. **关键能力数字**：最多几路流、最多几个触发窗口、峰值带宽多少。
3. **仓库职责边界**：本仓库 vs `psi_multi_stream_daq` 各自负责什么；想加新功能去哪里。
4. **版本现状**：当前版本号、它相对上一个版本修复了什么、升级风险等级。
5. **许可证要点**：能否用于闭源商业 FPGA 产品。

**参考答案（示例）**：

> 1. psi_ms_daq 是一个多流数据采集 IP，把多路 AXI-Stream 输入的数据通过 AXI Master 直接写进 DDR 内存。
> 2. 最多 16 路流（可不同位宽）、最多 32 个触发窗口、64 位 @ 250 MHz 达 2 GB/s；附带参考设计。
> 3. 本仓库 `vivadoIP_psi_ms_daq` 只是 Vivado 封装层（wrapper），真正功能在 `psi_multi_stream_daq`；加新功能要去后者。
> 4. 当前版本 1.2.1，相对 1.2.0 修复了两点（用上游 1.2.1 VHDL 重建 + 修复参考设计绝对路径），属纯 bugfix 升级，风险低。
> 5. PSI HDL Library License（LGPL + 例外）：可用于闭源商业 FPGA 产品（比特流算二进制）；但修改并分发本库源码仍受 LGPL 约束。

> 这个任务不需要运行任何命令，但要求你**把文档里的散点信息组织成一段连贯陈述**——这正是后续阅读源码前最该练的「全局观」。

---

## 6. 本讲小结

- **psi_ms_daq 是通用多流数据采集引擎**：最多 16 路 AXI-Stream 输入、最多 32 个触发窗口、支持线性/环形缓冲，通过 AXI Master 直写 DDR，峰值 2 GB/s（64 位 @ 250 MHz）。
- **本仓库只是封装层**：`vivadoIP_psi_ms_daq` 只是把 `psi_multi_stream_daq` 的 VHDL 实现包装成 Vivado IP-Core；改功能要去后者。
- **文档入口**：README 的 Description（能力清单）、Important Note（封装声明）、Detailed Documentation（指向 `psi_multi_stream_daq`）是三处最权威的描述。
- **版本号约定**：`major.minor.bugfix`，分别对应破坏性变更、新增功能、纯修复。
- **当前版本 1.2.1**：相对 1.2.0 是纯 bugfix（跟随上游 1.2.1 重建 + 修复参考设计路径），升级风险低。
- **许可证**：PSI HDL Library License = LGPL + FPGA 例外，允许闭源商业比特流分发。

---

## 7. 下一步学习建议

本讲只读了文档，还没有看任何工程文件。下一讲 **u1-l2《仓库目录结构与关键文件导览》** 会带你走一遍仓库的 41 个文件：

- `hdl/`（唯一 RTL）、`scripts/`（打包脚本）、`xgui/`（参数界面）、`bd/`（Block Design 钩子）、`drivers/`（C 驱动）、`refdesign/`（ZCU102 参考设计）、`component.xml`（IP-XACT 描述）。
- 建议同时收藏 [README.md](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/README.md) 与 [Changelog.md](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/Changelog.md)，后续讲义会反复引用其中的依赖清单与版本约定。

> 阅读建议：在进入 u1-l2 之前，先在本讲的基础上，自己用 30 秒向空气复述一遍「这个项目是什么、版本到哪了、能不能商用」。能顺畅说出来，就说明本讲的目标达成了。
