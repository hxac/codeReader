# 项目定位与整体概览

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标是让你在完全不预设背景的情况下，回答清楚三个问题：

1. `vivadoIP_spi_simple` 到底**是什么**？它解决了什么实际问题？
2. 这个项目**由谁维护**、**作者是谁**、**用什么协议授权**？
3. 它从 1.0.0 到 1.3.0 **经历了哪些版本演进**？

学完本讲，你应当能够用一句话向同事介绍这个 IP，看懂 README 里的「Dependencies」与「Description」两节，并能从 Changelog 里读出版本背后的功能变化。本讲不涉及任何 VHDL 代码细节，那些留给后续讲义。

---

## 2. 前置知识

本讲面向零基础读者，但有几个名词先解释清楚会更好理解。

### 2.1 什么是 SPI

SPI（Serial Peripheral Interface，串行外设接口）是一种**主从式、同步、串行**通信协议，常用于芯片之间短距离、中高速的数据交换。一条 SPI 链路通常包含四根信号线：

| 信号 | 全称 | 方向 | 作用 |
|------|------|------|------|
| SCK | Serial Clock | 主→从 | 主机生成的时钟，决定比特率 |
| MOSI | Master Out, Slave In | 主→从 | 主机发往从机的数据 |
| MISO | Master In, Slave Out | 从→主 | 从机发往主机的数据 |
| SS / CS_n | Slave Select / Chip Select | 主→从 | 选中某个从机（常低有效） |

「主机（Master）」负责**生成时钟**并**发起传输**，「从机（Slave）」被动响应。所谓 **SPI Master IP**，就是把「当一个 SPI 主机」这件事封装成一块可以直接放进 FPGA 的硬件电路。

> 术语提示：本项目中片选信号写作 `CS_n`（下划线 n 表示低电平有效），后续讲义会反复出现。

### 2.2 什么是 Vivado IP-core

Vivado 是 Xilinx（现 AMD）FPGA 的官方开发工具。**IP-core**（Intellectual Property core）是一段预先设计好、可参数化、可复用的硬件模块。把 IP 打包后，使用者可以在 Vivado 的图形界面里像搭积木一样把它放进工程，通过 AXI 总线用 CPU（如 Zynq 的 ARM 核）来配置和驱动它。

### 2.3 什么是 AXI4 寄存器接口

AXI4 是 ARM 提出的片上总线协议。本项目用的是它的一个简化子集——**AXI4-Lite 风格的寄存器接口**：CPU 通过读写一组内存映射的寄存器（如「数据寄存器」「状态寄存器」）来控制 IP。你暂时把它理解成「CPU 往某个地址写数据 = 让 IP 干活，CPU 读某个地址 = 拿结果」即可，细节在第 u2-l3 讲细讲。

### 2.4 什么是 PSI

PSI 是 **Paul Scherrer Institute**（保罗·谢勒研究所，瑞士）的缩写。本项目是 PSI 开源的 **PSI HDL Library**（PSI 硬件描述语言库）的一部分，仓库里的 `psi_common`、`psi_tb`、`PsiSim`、`PsiIpPackage` 等都是这个库家族的成员。

---

## 3. 本讲源码地图

本讲只读三份「文档型」文件，不涉及任何 `.vhd` 源码。

| 文件 | 作用 | 本讲用途 |
|------|------|----------|
| `README.md` | 项目主说明：维护者、作者、License、依赖、简介、仿真方式 | 提取项目定位、人员、依赖关系 |
| `Changelog.md` | 版本变更记录 | 梳理 1.0.0 → 1.3.0 的功能演进 |
| `License.txt` | PSI HDL Library License 全文 | 理解授权方式与「LGPL + 例外」 |

> 提示：README 第 13 行还指向一份 PDF 数据手册 `doc/spi_simple.pdf`，那是更详细的硬件规格说明，本讲不展开，但你知道它的存在即可。

---

## 4. 核心概念与源码讲解

### 4.1 项目定位与 SPI Master 概念

#### 4.1.1 概念说明

一句话定位：**`vivadoIP_spi_simple` 是一个基于 AXI4 寄存器接口的、简单且高度可配置的 SPI Master IP-core**。

它要解决的问题是：FPGA 系统里的 CPU（或别的 AXI 主机）需要和外挂的 SPI 从器件（传感器、ADC、配置芯片等）通信，但又不想自己写时序逻辑。把本 IP 放进设计后，CPU 只需读写寄存器就能完成 SPI 收发。

README 的 Description 节直接点明了它和 Xilinx 官方 SPI IP 的差异：本 IP **可配置性更强**，例如支持**任意传输宽度**（arbitrary transfer sizes）。这意味着你不必被固定在 8/16/32 位上，可以按器件需要灵活设定。

#### 4.1.2 核心流程

从使用者视角，一次 SPI 通信的端到端流程可以概括为：

```text
CPU 写 AXI 寄存器        IP 内部                   物理引脚           从器件
   |                       |                          |                 |
   |--写 Data/SlaveNr----->|                          |                 |
   |                       |--打包进 TX 命令 FIFO----->|                 |
   |                       |--启动 SPI 引擎---------->|                 |
   |                       |               驱动 SCK/MOSI/CS_n--------->|（被选中）
   |                       |               采样 MISO<-------------------|（回数据）
   |                       |--写入 RX 响应 FIFO<-------|                 |
   |<--读 RxData/Status----|                          |                 |
```

注意：上面这个流程只是帮你建立直觉，具体的 FIFO、引擎、握手信号会在第 u2-l2 讲逐步拆开。本讲你只需要记住「CPU 经 AXI 寄存器 ↔ IP ↔ SPI 引脚」这条主线。

#### 4.1.3 源码精读

**Description 节**——这是整个项目最核心的一句话定位：

[README.md:45-46](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/README.md#L45-L46) —— 说明本 IP 实现了一个简单的 SPI 接口，并强调相对 Xilinx SPI IP-Core 提供更高可配置性（如任意传输宽度）。

**仿真运行方式**——README 给出了回归测试的入口命令：

[README.md:48-54](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/README.md#L48-L54) —— 在 Modelsim 里、于 `sim` 目录下执行 `source ./run.tcl` 即可跑通回归测试。

**依赖声明节**——README 中有一段被特殊注释包裹、会被脚本自动解析的依赖清单：

[README.md:18-35](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/README.md#L18-L35) —— 列出 TCL 层（PsiSim、PsiIpPackage）与 VHDL 层（psi_common、psi_tb）依赖，并提到可用聚合仓库 `psi_fpga_all` 一次性获得正确目录结构。注意第 18 行的注释「DO NOT CHANGE FORMAT: this section is parsed」，说明这段格式被 `dependencies.py` 依赖解析脚本读取，不能随意改动（第 u1-l3 讲会细讲）。

#### 4.1.4 代码实践

**实践目标**：用一段话讲清楚「这个 IP 解决什么问题」。

**操作步骤**：

1. 打开 [README.md](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/README.md)，重点读 `# Description` 一节（第 45–46 行）。
2. 结合本讲 2.1 节对 SPI 的解释，思考：如果没有这个 IP，你要让 FPGA 里的 CPU 和一颗 SPI 传感器通信，需要自己做什么？
3. 用一段话（3–5 句）写下本 IP 解决的问题，要点包括：① 它是 SPI Master；② 通过 AXI 寄存器供 CPU 驱动；③ 相对 Xilinx 官方 IP 的差异化优势。

**需要观察的现象**：你会注意到 README 没有罗列任何引脚或寄存器细节，这些都在数据手册 `doc/spi_simple.pdf` 和后续源码里。本篇刻意只讲「是什么、为什么」。

**预期结果**：你能写出类似下面这样的句子——

> 这个 IP 让 FPGA 中的 AXI 主机（如 Zynq ARM 核）通过读写一组寄存器就能完成 SPI 主机收发，省去手写时序逻辑；相比 Xilinx 官方 SPI IP，它支持任意传输宽度等更灵活的配置。

#### 4.1.5 小练习与答案

**练习 1**：SPI 通信中，时钟 SCK 由谁生成？这决定了谁是 Master、谁是 Slave？

**参考答案**：SCK 由 Master 生成。生成时钟并主动发起传输的一方就是 Master，被动响应的一方是 Slave。因此本 IP 作为 SPI **Master**，自己产出 SCK。

**练习 2**：README 用哪个词概括本 IP 相对 Xilinx SPI IP 的优势？举一个具体例子。

**参考答案**：用「configurability（可配置性）」概括，具体例子是「arbitrary transfer sizes（任意传输宽度）」——即不必固定在 8/16/32 位。

---

### 4.2 维护者/作者/License 背景

#### 4.2.1 概念说明

开源项目通常要分清三个角色：

- **Author（作者）**：最初写出这份代码的人。
- **Maintainer（维护者）**：当前负责接收 issue、合并改动的人（可能与作者不是同一人）。
- **License（授权协议）**：规定别人可以怎样使用、修改、分发这份代码。

本项目的 Author 是 Oliver Bründler，Maintainer 是 Waldemar Koprek（PSI 员工）。授权采用 **PSI HDL Library License**，它的本质是 **LGPL（GNU 宽通用公共许可证）外加一条针对硬件场景的例外条款**。这条例外对 FPGA 工程非常关键，下面专门讲。

#### 4.2.2 核心流程

PSI HDL Library License 的结构可以这样理解：

```text
            PSI HDL Library License
                      |
        +-------------+-------------+
        |                           |
   基础：LGPL v2（或更高）       例外条款（EXCEPTION NOTICE）
   - 修改源码后必须              - 允许把库以「二进制形式」
     以同样协议开源                （明确包含 FPGA bitstream）
   - 衍生作品受 LGPL 约束          纳入你自己的闭源/专有硬件
                                  - 即：你可以用本 IP 烧出
                                    商业 bitstream 而不必开源你的工程
```

换句话说：**纯 LGPL 会让 FPGA 商用变得别扭**（因为 bitstream 是否算「衍生作品」存疑），而 PSI 的例外条款正是为了消除这个疑虑——它明确把 FPGA bitstream 列为「binary」，允许你在自有条款下使用、链接、分发。

#### 4.2.3 源码精读

**维护者与作者**——README 顶部的人事信息：

[README.md:3-7](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/README.md#L3-L7) —— Maintainer 为 Waldemar Koprek（ PSI 邮箱），Author 为 Oliver Bründler。

**License 概述**——README 对授权的一句话说明：

[README.md:9-10](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/README.md#L9-L10) —— 指明本库采用 PSI HDL Library License，其本质是 [LGPL](LGPL2_1.txt) 加上一些为固件开发场景澄清 LGPL 条款的例外。

**License 全文头部**——License.txt 开篇：

[License.txt:1-4](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/License.txt#L1-L4) —— 协议名称为 PSI HDL Library License v1.0，版权归 Oliver Bründler 等人（1998–2018）。

**关键例外条款**——这是对 FPGA 使用者最重要的那一段：

[License.txt:15-21](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/License.txt#L15-L21) —— 「EXCEPTION NOTICE」第 2 条明确：可以基于本库生成的**二进制形式**作品（**明确包含 FPGA bitstream 或 flash 镜像这类器件配置文件**）在你自己的条款下使用、复制、链接、修改和分发；同时明确把「能还原源码的数据」排除在 binary 之外。

#### 4.2.4 代码实践

**实践目标**：弄清楚「我能不能把这个 IP 用进我的商业 FPGA 工程，而不必开源我的工程」。

**操作步骤**：

1. 打开 [License.txt](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/License.txt)，找到第 15 行起的「EXCEPTION NOTICE」。
2. 重点读第 19 行（即上面引用的第 2 条），圈出两个关键词：`binary` 和 `FPGA-bitstreams`。
3. 用一句话回答：如果你只是**使用**本 IP（综合进 bitstream）但不修改它的源码，你需要开源你的工程吗？

**需要观察的现象**：你会看到协议把「使用/链接/分发二进制（含 bitstream）」与「修改库源码」分两种情形对待。

**预期结果**：结论应是——**仅把本 IP 综合进 bitstream 使用，可依自有条款分发，无需开源你的工程**；但若你**修改了本 IP 的源码**并分发，则需遵循 LGPL（要么同样开源修改、要么保留/调整例外声明），具体法律结论建议交由法务确认（待本地/法务验证）。

#### 4.2.5 小练习与答案

**练习 1**：本项目的 Author 和 Maintainer 是不是同一个人？这说明了什么？

**参考答案**：不是。Author 是 Oliver Bründler，Maintainer 是 Waldemar Koprek。说明代码最初由前者编写，当前由后者（PSI 员工）负责维护，是一个有持续维护主体的开源项目。

**练习 2**：PSI HDL Library License 与纯 LGPL 的最大区别是什么？为什么这对 FPGA 项目重要？

**参考答案**：区别在于多了一条「例外条款」，明确把 FPGA bitstream 列为可自由分发的 binary。这让商业 FPGA 工程可以安全使用本 IP 而不必开源整个工程，消除了纯 LGPL 在硬件场景下的不确定性。

---

### 4.3 Changelog 版本演进

#### 4.3.1 概念说明

**Changelog（变更日志）** 是项目按版本记录「新增了什么、改了什么、修了什么」的文件。阅读 Changelog 是快速理解一个项目「成长轨迹」的最高效方式——你能看出哪些功能是后加的、哪些是修过的 bug。

本项目的 Changelog 从 1.0.0（首次实现）一直到 1.3.0（新增 LE 输出），下面把这条演进线串起来。

#### 4.3.2 核心流程

把 1.0.0 → 1.3.0 的关键变化按时间顺序梳理：

```text
1.0.0  首次实现（First Implementation）
  │
1.1.0  + 依赖解析脚本（dependencies.py）
  │    + 改用 psi_common 的 AXI slave（替换 legacy 版本）
  │
1.2.0  首个开源发布（First Open Source Release）
  │    + 加上 License / 版权头
  │
1.2.1  ~ 修复：C 驱动兼容 C++
  │
1.2.2  ~ 修复：R/W 寄存器支持 AXI 读回（readback）
  │
1.3.0  + 新功能：LE（Latch Enable）输出
```

读法提示：`+` 表示新增功能、`~` 表示修复/改进。可以看到项目从「能跑」→「工程化（依赖脚本、换 AXI 实现）」→「开源合规（License）」→「健壮性（C++ 兼容、读回）」→「功能扩展（LE）」的成熟过程。

> 一处诚实的说明：仓库当前 HEAD（提交 `fda4db7`，标题「DEVEL: 3-Wires SPI interface signal added」）包含了**正在开发中的 3-Wire SPI 三态扩展**，这部分功能**尚未出现在 Changelog 的任何已发布版本里**（Changelog 顶部仍是 1.3.0）。3-Wire SPI 的细节属于第 u3-l2 讲的内容，本讲只覆盖 Changelog 中已记录的 1.0.0–1.3.0。

#### 4.3.3 源码精读

下面逐版本引用 Changelog 原文（永久链接以当前 HEAD `fda4db7` 为准）：

**1.3.0 —— 新增 LE 输出**：

[Changelog.md:1-3](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/Changelog.md#L1-L3) —— 唯一一条 Features 是「Added LE output」（新增 Latch Enable 锁存使能输出，每从机一根）。

**1.2.2 —— R/W 寄存器支持 AXI 读回**：

[Changelog.md:5-7](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/Changelog.md#L5-L7) —— Bugfix：增加对读写（R/W）寄存器的 AXI 读回能力（此前读这些寄存器可能拿不到正确值）。

**1.2.1 —— C 驱动兼容 C++**：

[Changelog.md:9-11](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/Changelog.md#L9-L11) —— Bugfix：让驱动 C++ tolerant（可在 C++ 工程中编译链接）。

**1.2.0 —— 首个开源发布**：

[Changelog.md:13-15](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/Changelog.md#L13-L15) —— First Open Source Release（更早版本未保留在历史中），并加上 License 与版权头。

**1.1.0 —— 工程化**：

[Changelog.md:17-22](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/Changelog.md#L17-L22) —— 两条变化：新增依赖解析脚本；AXI slave 改用 `psi_common` 的实现，弃用 legacy 版本。

**1.0.0 —— 首次实现**：

[Changelog.md:24-28](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/Changelog.md#L24-L28) —— First Implementation，无 Bugfix。

#### 4.3.4 代码实践

**实践目标**：从 Changelog 里提炼出 1.0.0 → 1.3.0 之间**至少三个**功能层面的变化。

**操作步骤**：

1. 打开 [Changelog.md](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/Changelog.md)。
2. 列一张表，左列写版本号，右列写「该版本最有代表性的一条变化」。
3. 从中挑出至少 3 条**功能/行为变化**（而不是纯文本变化）。

**需要观察的现象**：注意区分「Features（新功能）」「Changes（改动）」「Bugfixes（修复）」三类标签，它们在 Changelog 里被分开列。

**预期结果**：你应能列出类似下面的清单（任选三条即可）：

| 版本 | 代表性变化 |
|------|-----------|
| 1.1.0 | 新增依赖解析脚本 `dependencies.py`；AXI slave 改用 `psi_common` 实现 |
| 1.2.2 | 支持 R/W 寄存器的 AXI 读回（readback） |
| 1.3.0 | 新增 LE（Latch Enable）输出 |

#### 4.3.5 小练习与答案

**练习 1**：从 1.0.0 到 1.3.0，哪两个版本是「新增功能」，哪几个是「修复」？

**参考答案**：新增功能为主的是 1.1.0（依赖解析脚本）和 1.3.0（LE 输出）；以修复/改进为主的是 1.2.1（C++ 兼容）和 1.2.2（AXI 读回）。1.2.0 是首个开源发布并加 License 头，1.0.0 是首次实现。

**练习 2**：如果你手上有一个 1.2.0 的工程，发现读「配置类寄存器」读不到正确值，根据 Changelog 你应该升级到哪个版本？为什么？

**参考答案**：升级到 **1.2.2**。因为 Changelog 1.2.2 明确修复了「R/W 寄存器的 AXI 读回」问题，正是这个症状。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个综合小任务（纯阅读 + 写作，无需运行任何工具）：

**任务**：假设你要在团队内部立项评审里用一页 PPT 介绍这个 IP，请基于本讲读到的 `README.md`、`Changelog.md`、`License.txt`，产出以下四块内容：

1. **一句话定位**：它是什么、给谁用、替代了什么（参考 4.1）。
2. **人事与合规**：Author / Maintainer 是谁、License 是什么、能否用于商业闭源 bitstream（参考 4.2）。
3. **成熟度判断**：根据 Changelog，用 2–3 句话评价这个项目当前处于什么成熟阶段（是否有持续功能迭代、是否做过健壮性修复）。
4. **跟进问题**：列出 2 个你还回答不了、需要看后续讲义或数据手册才能回答的问题（例如：到底支持哪些 SPI 模式？寄存器地图长什么样？）。

**验收标准**：

- 第 1 点必须出现「SPI Master」「AXI」「可配置性」等关键词。
- 第 2 点必须正确区分 Author 与 Maintainer，并点明 LGPL + 例外的含义。
- 第 3 点必须引用至少两个具体版本号作为证据。
- 第 4 点的问题应当能在后续讲义（u2-l1 寄存器地图、u2-l4 SPI 时序等）里找到答案。

> 待本地验证：如果你在团队里真做了这份介绍，可以把第 4 点的「跟进问题」记下来，作为后续学习手册各讲的「带着问题阅读」清单。

---

## 6. 本讲小结

- `vivadoIP_spi_simple` 是一个**基于 AXI4 寄存器接口的简单、高可配置 SPI Master IP-core**，属于 PSI HDL Library 家族。
- 它相对 Xilinx 官方 SPI IP 的差异化优势是**可配置性**，典型例子是**任意传输宽度**。
- 项目由 **Oliver Bründler** 创作、**Waldemar Koprek**（PSI）维护，采用 **PSI HDL Library License（LGPL + 硬件例外）**，允许把本 IP 综合进商业 bitstream 而不必开源整个工程。
- 回归测试入口是 `sim` 目录下的 `source ./run.tcl`（Modelsim/Vsim）。
- Changelog 从 1.0.0（首次实现）演进到 1.3.0（新增 LE 输出），期间经历了工程化（依赖脚本、换用 psi_common AXI slave）、开源合规（License 头）、健壮性修复（C++ 兼容、AXI 读回）。
- 当前 HEAD `fda4db7` 含有**尚未发布到 Changelog 的 3-Wire SPI 开发中改动**，属于后续讲义范畴。

---

## 7. 下一步学习建议

本讲只读了文档，还没碰任何代码。建议按下面顺序继续：

1. **下一讲 u1-l2「目录结构与文件分工」**：先把仓库的 `hdl`、`tb`、`sim`、`scripts`、`drivers` 等目录和三个关键入口文件（顶层 RTL、testbench、打包脚本）对应起来，建立「代码地图」。
2. **u1-l3「工具链、依赖与获取方式」**：搞清楚 `dependencies.py` 如何解析 README 里那段被注释包裹的依赖清单。
3. **u1-l4「仿真与回归测试运行方式」**：动手（或在脑子里走查）跑一遍 `source ./run.tcl`。
4. 进入第二单元（u2）后，再开始读 `definitions_pkg.vhd`、`spi_simple.vhd` 等 VHDL 源码，那时你将带着本讲建立的「宏观定位」去理解细节。

> 推荐先读：README 指向的数据手册 `doc/spi_simple.pdf`（如仓库中提供），它能填补本讲刻意略过的硬件细节。
