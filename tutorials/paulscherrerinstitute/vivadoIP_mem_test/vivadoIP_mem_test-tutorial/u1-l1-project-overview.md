# 项目概览：它是什么、解决什么问题

## 1. 本讲目标

本讲是整本学习手册的第一篇，面向「完全没接触过这个项目」的读者。读完本讲后，你应该能够：

- 说清楚 **vivadoIP_mem_test** 这个 IP 核是做什么的、解决什么工程问题；
- 用一句话描述它的**输入与输出**：控制接口是什么、被测存储器接在哪里；
- 知道项目的**维护者、作者、许可证**以及**详细文档**放在哪里；
- 看懂 `Changelog.md`，能指出**当前版本号**以及与上一版相比的主要变化。

本讲只读三个非源码文件（`README.md`、`Changelog.md`、`License.txt`），不涉及任何 VHDL 语法。后续讲义才会进入寄存器、RTL 和仿真细节。

## 2. 前置知识

在开始之前，先用最通俗的语言解释几个会反复出现的术语。即使你完全不懂，也能继续往下读——这里只要求建立一个「大致印象」。

- **FPGA**：一块可以在出厂后重新「编程」连线、实现各种数字电路的芯片。本项目的 IP 核就是放在 FPGA 内部使用的一块功能电路。
- **IP 核（IP core）**：Intellectual Property core，可以理解为「预先做好、可复用的电路模块」。本仓库交付的就是一个可被 Vivado 打包、复用的 IP 核。
- **AXI**：Advanced eXtensible Interface，是 ARM 提出的一套**总线协议**，被 Xilinx/AMD 的 FPGA 和 SoC 广泛采用，用来让不同电路模块之间交换数据。本项目里你会看到两种 AXI：
  - **AXI-Lite**：轻量版，每次只读写一个 32 位寄存器，速度低，常用于「CPU 配置 / 读取状态」。
  - **AXI4（Full）**：高吞吐版，支持**突发传输（burst）**，一次连读 / 连写一大批数据，常用于访问 DDR 等大容量存储器。
- **存储器（memory）/ DDR**：被测试的对象，比如挂在 FPGA 上的 DDR 内存。本 IP 不直接实现 DDR 控制器，而是通过 AXI4 主机接口去访问「某个已经存在的、用 AXI 暴露出来的存储器」。
- **Pattern（测试图形）**：写到存储器里的「已知的、有规律的数据」，例如递增计数、走 1 等。回读后再和这个 pattern 对比，对不上就说明出错。

> 一句话直觉：本 IP 像一个**自动质检员**——它按你给的规则往一块存储器里写满「标准答案」，再读回来批改，数一数错了多少题、第一道错题在哪。

## 3. 本讲源码地图

本讲涉及的文件都是项目根目录下的「说明性」文件，不包含 VHDL 代码：

| 文件 | 作用 | 本讲用途 |
| --- | --- | --- |
| `README.md` | 项目总说明：维护者、作者、许可证、依赖、功能描述、仿真入口 | 提取项目定位、接口、许可证、文档与依赖信息 |
| `Changelog.md` | 版本变更记录 | 了解版本演进与当前版本号 |
| `License.txt` | PSI HDL Library License 正文 | 理解许可证条款与 FPGA 特例 |

> 提示：项目根目录还有一个 `LGPL2_1.txt`，它是本许可证所基于的 LGPL 正文（见 4.2 节）。`doc/mem_test.pdf` 是官方数据手册，本讲会告诉你怎么找到它，但不会展开。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**项目描述**、**许可证与维护者**、**版本历史**。

### 4.1 项目描述：它到底是个什么 IP

#### 4.1.1 概念说明

很多 FPGA 系统里都会挂一块大容量存储器（典型如 DDR），软件和硬件都依赖它「存得对、读得准」。但 DDR 涉及 PCB 走线、时序、控制器配置、PHY 校准等大量环节，任何一环出问题都可能导致**数据错误**，而且这种错误往往随机、难复现。

**内存测试器（memory tester）** 就是为了解决这个可靠性验证问题：它自动地、大批量地、可重复地「写—读—比对」，从而把存储子系统是否健康量化成几个数字（错误数、首个错误地址等）。

本项目 `vivadoIP_mem_test` 正是这样一个 IP 核。`README.md` 用一句话点明了它的核心功能：

> This IP-core implements a memory tester for memories connected over AXI. It writes patterns, reads them back and checks whether there are no errors.

翻译过来就是：**这个 IP 核实现了一个针对「通过 AXI 连接的存储器」的内存测试器：它写入测试图形、读回、并检查是否有错误。**

#### 4.1.2 核心流程

从「外部视角」看，一次完整的内存测试可以概括成下面这条流水线（细节会在 u2、u3 讲义展开，这里只要建立直觉）：

```text
   软件侧 (CPU / 裸机程序)                 本 IP 核 (FPGA 内)                  被测存储器 (如 DDR)
   ─────────────────────                  ─────────────────                  ───────────────────
        │                                       │                                   │
        │  ① 经 AXI-Lite 写寄存器：              │                                   │
        │     选择 pattern、地址范围、模式       │                                   │
        ├──────────────────────────────────────►│                                   │
        │  ② 写 START 寄存器启动                 │                                   │
        ├──────────────────────────────────────►│  ③ 生成 pattern，AXI4 burst 写   │
        │                                       ├──────────────────────────────────►│
        │                                       │  ④ AXI4 burst 读回               │
        │                                       │◄──────────────────────────────────┤
        │                                       │  ⑤ 逐拍比对，累计错误数、         │
        │                                       │     记录首个错误地址              │
        │  ⑥ 经 AXI-Lite 读 STATUS/ERRORS/      │                                   │
        │     FIRSTERR 寄存器                   │                                   │
        │◄──────────────────────────────────────┤                                   │
```

把它抽象成一个最朴素的「批改」公式（用 \(E\) 表示错误计数）：

\[
E = \sum_{i=0}^{N-1} \mathbb{1}\!\left(\text{read}_i \neq \text{expected}_i\right)
\]

即：把 \(N\) 个读回的数据和期望 pattern 逐个比对，每出现一次不一致就把错误计数加 1。若 \(E=0\)，就认为这一轮测试通过。

> 说明：上式只是帮你建立「比对 + 计数」的直觉；具体的地址换算、首个错误地址记录等实现细节在 u3-l4 讲义精读，本讲不展开。

#### 4.1.3 源码精读

项目把这句功能描述写在 `README.md` 的 `# Description` 段：

- [README.md:L45-L46](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/README.md#L45-L46) —— **项目的官方一句话定位**：「这是一个面向 AXI 存储器的内存测试器，写 pattern、读回、检查错误」。这是理解整个项目的起点。

虽然本讲不读 VHDL，但为了让你对「输入 / 输出」有具体画面，下面给出顶层 wrapper 实体的端口分类（来自 `hdl/mem_test_wrapper.vhd`，后续 u3-l1 会逐行精读，这里只看分类）：

- [hdl/mem_test_wrapper.vhd:L14-L24](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L14-L24) —— 顶层 `entity` 的 generics：声明了 AXI 从机 ID 宽度，以及 AXI4 主机的**数据宽度 / 地址宽度 / 最大突发长度 / 最大未完成事务数**等可配置参数。这些参数决定了本 IP 能适配哪种位宽的存储器。
- [hdl/mem_test_wrapper.vhd:L32-L53](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L32-L53) —— **AXI-Lite 从机接口（S00_AXI）** 的读地址 / 读数据通道。这就是「控制接口」：CPU 通过它配置寄存器、读取状态。注意 `s00_axi_araddr` 只有 8 位（`7 downto 0`），意味着寄存器地址空间很小。

把上面两段端口信息归纳成一张「黑盒」图：

| 方向 | 接口 | 协议 | 作用 |
| --- | --- | --- | --- |
| 输入（控制） | S00_AXI | AXI-Lite 从机 | CPU 写寄存器配置 / 启动；读状态 / 错误统计 |
| 输出（访问被测存储器） | M00_AXI | AXI4 主机 | 向被测存储器发起 burst 写、burst 读 |
| 公共 | axi_aclk / axi_aresetn | — | 时钟与复位 |

所以「被测存储器接在哪里」的答案是：**接在本 IP 的 AXI4 主机端口 M00_AXI 上**。

#### 4.1.4 代码实践

> 这是一个**源码阅读型实践**（本讲不要求运行任何工具链），目标是把「项目定位」内化为一张你能默画出来的黑盒图。

1. **实践目标**：用一段话（3–5 句）写清楚本 IP 的输入输出——控制接口是什么、被测存储器接在哪里、它对存储器做了哪三件事。
2. **操作步骤**：
   - 打开 [README.md:L45-L46](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/README.md#L45-L46)，读官方那句描述。
   - 打开 [hdl/mem_test_wrapper.vhd:L14-L53](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L14-L53)，确认确实存在 AXI-Lite 从机和 AXI4 主机两套端口。
   - 在笔记本上画一个方框，左边标 `S00_AXI (AXI-Lite)`，右边标 `M00_AXI (AXI4)`，框内写「写 pattern → 读回 → 比对」。
3. **需要观察的现象**：你会发现 `s00_axi_araddr` 是 8 位（`7 downto 0`），而 M00_AXI 的地址宽度由 generic `C_M00_AXI_ADDR_WIDTH`（默认 32）决定。这说明**控制平面地址空间小、数据平面地址空间大**。
4. **预期结果**：你能写出类似下面这样的段落——
   > 「本 IP 有两个 AXI 端口。控制接口是 AXI-Lite **从机** S00_AXI（8 位地址），CPU 通过它写寄存器来选择 pattern、设定地址范围与模式并启动测试，再通过它读回状态与错误统计。被测存储器接在本 IP 的 AXI4 **主机**端口 M00_AXI 上；IP 会向该存储器发起突发写、突发读，并把读回数据与期望 pattern 逐拍比对，统计错误数与首个错误地址。」
5. **运行结果**：本实践无命令需运行，结果以你写出的段落与画的方框图为准。

#### 4.1.5 小练习与答案

**练习 1**：如果一块存储器的数据位宽是 64 位，你会修改 `hdl/mem_test_wrapper.vhd` 里的哪一个 generic？为什么？

> **参考答案**：修改 `C_M00_AXI_DATA_WIDTH`（见 [hdl/mem_test_wrapper.vhd:L20](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L20)）。它定义了 AXI4 主机的数据位宽，必须与被测存储器的数据位宽一致，否则突发读写的数据无法正确对齐。

**练习 2**：为什么控制接口（S00_AXI）用 AXI-Lite 而不是 AXI4 Full？

> **参考答案**：控制接口只用来读写少量 32 位寄存器、偶发访问，不需要高吞吐；AXI-Lite 信号更少、实现更简单、面积更小。而访问存储器需要大批量数据搬运，才用支持突发的 AXI4 Full。这是「控制平面轻、数据平面重」的常见设计取舍。

---

### 4.2 许可证、维护者与文档位置

#### 4.2.1 概念说明

在动用任何开源 IP 之前，先搞清三件事：**谁在维护**、**用什么许可证**、**详细文档在哪**。这决定了你能不能用、怎么用、出问题找谁。

本项目来自 **Paul Scherrer Institute（PSI，瑞士保罗谢尔研究所）**，是 PSI 一系列 FPGA HDL 公开库中的一个 IP。它和几个兄弟仓库（`psi_common`、`psi_tb`、`PsiSim`、`PsiIpPackage` 等）共同构成一套 PSI FPGA 开发生态。

许可证叫 **PSI HDL Library License**，它的本质是 **LGPL（GNU 宽通用公共许可证）外加一条针对硬件场景的特例**。这一点对 FPGA 工程师尤其重要，下面专门讲。

#### 4.2.2 核心流程

读这类项目元信息的标准顺序是：**README 顶部 → License 正文 → Datasheet**。

1. 看 `README.md` 顶部的 Maintainer / Author / License / Detailed Documentation 四个小节，建立「谁、什么许可、文档在哪」的整体印象。
2. 打开 `License.txt` 看许可证正文，重点看「特例（EXCEPTION NOTICE）」——它解释了为什么这个许可证对 FPGA 友好。
3. 需要寄存器位级细节时，再翻 `doc/mem_test.pdf` 数据手册。

#### 4.2.3 源码精读

**维护者与作者**（README 顶部）：

- [README.md:L3-L4](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/README.md#L3-L4) —— **维护者（Maintainer）**：Jonas Purtschert。当前由他负责仓库维护。
- [README.md:L6-L7](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/README.md#L6-L7) —— **作者（Author）**：Oliver Bründler，IP 的原始作者。

> 这与 git 历史也吻合：提交 `ccd227b DOC: take over repo maintenance from oliver` 记录了维护权从作者 Oliver 交接给当前维护者的过程。

**许可证**：

- [README.md:L9-L10](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/README.md#L9-L10) —— README 对许可证的一句话说明：本库采用 **PSI HDL Library License**，它就是 **LGPL** 加上一些「为固件开发场景澄清 LGPL 条款」的额外例外。
- [License.txt:L1](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/License.txt#L1) —— 许可证名称与版本：**PSI HDL Library License, Version 1.0**。
- [License.txt:L4](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/License.txt#L4) —— 版权声明：Copyright (c) 1998-2018 Oliver Bründler, Julian Smart, Robert Roebling et al。
- [License.txt:L11](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/License.txt#L11) —— 许可证基础：本库基于 **GNU Library General Public License（LGPL）第 2 版或（你可选）任意更高版本**授权。
- [License.txt:L15-L19](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/License.txt#L15-L19) —— **EXCEPTION NOTICE（特例）**：这是对 FPGA 工程师最关键的一条。它允许你以**自己的条款**使用、复制、链接、修改并分发「基于本库的二进制形式作品」或「包含该二进制的硬件」，并明确把 **FPGA 比特流（bitstream）/ flash 镜像**也算作「二进制」。换句话说：**你把本 IP 综合进自己的比特流去烧 FPGA、做产品，不受 LGPL 的源码披露义务约束**；但若你修改了本库的源码本身并分发，则仍需遵循 LGPL 条款。

**详细文档**：

- [README.md:L12-L13](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/README.md#L12-L13) —— **Datasheet**：详细文档见 `doc/mem_test.pdf`。需要寄存器位级说明、时序图时查阅它。

#### 4.2.4 代码实践

1. **实践目标**：确认你「可以」在自己的 FPGA 工程里使用本 IP，并知道出问题时去哪里查文档、找谁。
2. **操作步骤**：
   - 打开 [README.md:L3-L13](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/README.md#L3-L13)，把维护者邮箱、作者、许可证、Datasheet 路径抄到自己的笔记里。
   - 打开 [License.txt:L15-L19](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/License.txt#L15-L19)，确认「FPGA 比特流属于二进制、可按自己条款分发」这一条。
   - 用 `ls doc/` 查看数据手册文件确实存在（`mem_test.pdf` / `mem_test.docx` / `mem_test.vsd`）。
3. **需要观察的现象**：`doc/` 目录下应能看到 `mem_test.pdf`。
4. **预期结果**：你能用一句话回答「我把这个 IP 综合进自己的比特流去做产品，要不要公开我自己工程的源码？」——答案是**不需要**（依据特例条款），但若你修改并重新分发本 IP 的源码，则需遵循 LGPL。
5. **运行结果**：`ls doc/` 的输出应包含 `mem_test.pdf`（待本地验证具体文件名与你的检出状态一致）。

#### 4.2.5 小练习与答案

**练习 1**：项目当前的维护者和原始作者分别是谁？

> **参考答案**：维护者是 Jonas Purtschert（[README.md:L4](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/README.md#L4)），原始作者是 Oliver Bründler（[README.md:L7](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/README.md#L7)）。

**练习 2**：PSI HDL Library License 和普通 LGPL 的关键区别是什么？为什么这对 FPGA 工程师重要？

> **参考答案**：区别在于 [License.txt:L15-L19](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/License.txt#L15-L19) 的 EXCEPTION NOTICE：它把「含本库二进制的硬件 / FPGA 比特流」排除在 LGPL 的源码披露义务之外。这让工程师可以放心地把本 IP 综合进产品比特流分发，而不必公开自己整个工程的源码——这对商业 FPGA 产品至关重要。

---

### 4.3 版本历史：当前版本号与主要变化

#### 4.3.1 概念说明

`Changelog.md` 记录了项目的版本演进。学会读 changelog 是「快速了解一个项目成熟度与近期改动」的最便宜手段：你能一眼看出哪些版本是功能新增、哪些是 bugfix、当前停在哪个版本。

#### 4.3.2 核心流程

读 changelog 的顺序是**自上而下**（最新的在最上面）。每个版本号下用 `* Features` / `* Changes` / `* Bugfixes` 分组列出改动。

#### 4.3.3 源码精读

完整的四段版本记录：

- [Changelog.md:L1-L3](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/Changelog.md#L1-L3) —— **当前版本 1.2.1**，只有一个 Bugfix：**让 C 驱动兼容 C++**（Made driver C++ tolerant）。
- [Changelog.md:L5-L7](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/Changelog.md#L5-L7) —— **1.2.0**：首次开源发布（更早版本未保留在历史中），并添加了许可证与版权头。
- [Changelog.md:L9-L13](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/Changelog.md#L9-L13) —— **1.1.0**：新增依赖解析脚本（dependency resolution script）；改用来自 `psi_common` 的 AXI 从机，替代旧版（legacy）。
- [Changelog.md:L15-L16](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/Changelog.md#L15-L16) —— **1.0.0**：首次发布。

把四个版本整理成一张演进表：

| 版本 | 类型 | 主要内容 |
| --- | --- | --- |
| 1.0.0 | 首发 | 第一次发布 |
| 1.1.0 | 功能 + 变更 | 新增依赖解析脚本；AXI 从机改用 `psi_common` 版本 |
| 1.2.0 | 首发（开源） | 首个开源版本；加许可证/版权头 |
| **1.2.1（当前）** | Bugfix | C 驱动兼容 C++ |

> 关于 git 历史与 changelog 的对应（供参考）：1.2.1 的「Made driver C++ tolerant」对应提交 `ae6fce1 DEVEL: Made driver C++ tolerant`。在此之后仓库还有几笔提交（如 `c731a8f BUGFIX: Fix driver Makefile to work with Vitis (Windows)`、`756fa79` 合并 PR #3），这些改动**尚未在 `Changelog.md` 里产生新的版本号**。所以严格按 changelog，当前版本仍是 **1.2.1**；若你关心 Vitis 驱动 Makefile 的修复，需直接看 git log 而非 changelog。

#### 4.3.4 代码实践

1. **实践目标**：确认「当前版本号」以及「与上一版相比的主要变化」。
2. **操作步骤**：
   - 打开 [Changelog.md:L1-L3](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/Changelog.md#L1-L3)，读最新的 1.2.1 段。
   - 紧接着读 [Changelog.md:L5-L7](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/Changelog.md#L5-L7) 的 1.2.0 段作为对照。
   - （可选）运行 `git log --oneline -8`，观察 changelog 版本与 git 提交的对应关系。
3. **需要观察的现象**：changelog 最顶部版本是 1.2.1，紧随其下是 1.2.0；git log 里能看到比 1.2.1 更晚的提交（Vitis Makefile 修复）。
4. **预期结果**：你能写出——「**当前版本号是 1.2.1**；与上一版 1.2.0 相比，主要变化是一个 Bugfix：**让 C 驱动兼容 C++**（Made driver C++ tolerant）」。
5. **运行结果**：待本地验证 `git log` 输出与上文列出的提交一致。

#### 4.3.5 小练习与答案

**练习 1**：当前版本号是多少？相比上一版改了什么？

> **参考答案**：当前是 **1.2.1**（[Changelog.md:L1-L3](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/Changelog.md#L1-L3)）。相比 1.2.0，改动只有一条 Bugfix：让 C 驱动兼容 C++。

**练习 2**：从 1.1.0 到 1.2.0，项目发生了什么「质变」？

> **参考答案**：1.2.0 是**首个开源发布**（First Open Source Release），更早版本未保留在历史中；同时添加了许可证与版权头（[Changelog.md:L5-L7](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/Changelog.md#L5-L7)）。这是项目走向公开可用的关键节点。

**练习 3**：如果你在用 Vitis（Windows）编译驱动时遇到 Makefile 问题，应该信 changelog 还是 git log？为什么？

> **参考答案**：信 **git log**。因为该修复（提交 `c731a8f`，合并于 PR #3）发生在 1.2.1 之后，尚未在 `Changelog.md` 中体现为新版本。changelog 当前只到 1.2.1，不会记录这笔 Vitis Makefile 修复。

---

## 5. 综合实践

把本讲三个模块串起来，完成一份**「项目速览卡」**（一页以内）：

1. **黑盒图**：画一个方框，标出 `S00_AXI（AXI-Lite 从机，控制）` 与 `M00_AXI（AXI4 主机，接被测存储器）`，框内写「写 pattern → 读回 → 比对 → 统计错误」。
2. **一句话定位**：基于 [README.md:L45-L46](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/README.md#L45-L46)，用你自己的话写出本 IP 做什么。
3. **元信息表**：维护者、作者、许可证（含「比特流特例」一句）、Datasheet 路径、当前版本号。
4. **自检问题**：假设你要测一块挂在 FPGA 上的 64 位 DDR，你会动哪个 generic？控制接口为什么用 AXI-Lite？

> 完成后，这张速览卡就是你后续阅读寄存器地图（u2-l1）和 RTL（u3）时的「地图首页」，随时可以回来看。

## 6. 本讲小结

- **定位**：`vivadoIP_mem_test` 是一个 **AXI 内存测试器 IP 核**——自动写 pattern、读回、比对，统计存储器错误。
- **接口**：控制接口是 **AXI-Lite 从机 S00_AXI**（8 位地址，CPU 配置/读状态）；被测存储器接在 **AXI4 主机 M00_AXI** 上。
- **出处**：来自 **PSI**（Paul Scherrer Institute）FPGA HDL 公开库体系；维护者 Jonas Purtschert，作者 Oliver Bründler。
- **许可证**：**PSI HDL Library License** = LGPL + 硬件特例；**FPGA 比特流可按自己条款分发**，修改源码再分发才受 LGPL 约束。
- **文档**：详细说明见 `doc/mem_test.pdf`；版本变更见 `Changelog.md`。
- **版本**：当前 **1.2.1**（C 驱动兼容 C++）；Vitis Makefile 修复在 git log 中但尚未进入 changelog 版本号。

## 7. 下一步学习建议

你已经知道这个 IP「做什么」和「黑盒接口长什么样」，下一步建议：

- 想知道**到底有哪些寄存器、每个寄存器怎么配置测试** → 进入 **u2-l1 寄存器地图：mem_test_pkg 详解**。
- 想知道**四种测试模式与四种 pattern 的区别** → 进入 **u2-l2 测试模式与数据 pattern**。
- 想先看**怎么把它跑起来** → 进入 **u1-l3 运行仿真：PsiSim / Modelsim 回归测试**（依赖 u1-l2 仓库结构）。

推荐顺序：u1-l2（仓库结构）→ u1-l3（跑仿真）→ u2-l1（寄存器地图），先把「项目长什么样、怎么跑」彻底弄熟，再进入 RTL 内部。
