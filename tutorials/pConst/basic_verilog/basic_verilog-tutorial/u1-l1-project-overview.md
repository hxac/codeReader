# 项目概览：basic_verilog 是什么

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标只有一个：**让你在不动手写任何代码的前提下，搞清楚 basic_verilog 这个仓库到底是什么、里面装了什么、以及该怎么读它**。

学完本讲你应该能够：

- 用一句话向别人解释 basic_verilog 是什么类型的仓库（它不是一个"应用"，而是一堆可复用的硬件积木）。
- 看懂仓库根目录下的顶层目录（`example_projects/`、`benchmark_projects/`、`scripts/` 等）各自负责什么。
- 知道仓库采用 **CC BY-SA 4.0** 开源协议，并理解它对你使用、修改、再分发代码的约束。
- 学会用 README 提供的 **绿圈 :green_circle: / 红圈 :red_circle: 难度标签**，挑选适合自己水平的模块开始阅读。

本讲**不要求**你已经会写 Verilog。它只做"导览"，为你后续阅读真正的 `.sv` 源码做好心理预期。

## 2. 前置知识

为了让你完全跟得上，先把几个名词用大白话解释一下。已经熟悉的读者可以跳过本节。

- **Verilog / SystemVerilog**：用来描述数字硬件（也就是 FPGA / ASIC 内部逻辑）的编程语言。你可以把它想象成"画电路图的文字版"。`.v` 是老版 Verilog 文件后缀，`.sv` 是 SystemVerilog（Verilog 的增强版）文件后缀。
- **FPGA（现场可编程门阵列）**：一种可以反复"重新接线"的芯片。你用 Verilog 写的逻辑，最终会被烧进 FPGA，让它变成你想要的电路。ASIC 则是一次性制造、不可改的芯片。
- **可综合（synthesizable）**：指这段 Verilog 能被工具"翻译"成真实的电路（逻辑门、触发器）。不能综合的代码只能在仿真里跑、做不了真硬件。basic_verilog 强调"全部可综合"。
- **RTL（Register Transfer Level，寄存器传输级）**：一种常见的硬件代码风格，描述数据在寄存器之间如何流动和运算。本仓库的模块基本都是 RTL。
- **模块（module）**：Verilog 里描述一个电路单元的基本单位，类似软件里的"函数/类"。本仓库就是由大量独立 `.sv` / `.v` 模块组成的。
- **testbench（测试平台）**：一段专门用来给被测模块"施加激励、观察输出"的代码，本身不会烧进硬件，只在仿真器里跑。仓库里以 `_tb.sv` 结尾的文件就是 testbench。
- **README**：仓库根目录下的说明文件（`README.md`），是任何开源项目的"门面"和说明书，也是本讲的主要阅读对象。

## 3. 本讲源码地图

本讲几乎只围绕一个文件展开，但会"远远看一眼"几个目录：

| 文件 / 目录 | 作用 |
|-------------|------|
| `README.md` | 仓库的说明书。包含项目定位、开源协议、难度标签约定、目录索引表和模块索引表。本讲的绝对主角。 |
| `license/` | 存放 CC BY-SA 4.0 协议的正式文本与图标，是 README 里"协议"一节的实物佐证。 |
| `example_projects/` | 可直接打开的真实 FPGA 工程模板（Quartus / Vivado / Gowin 等）。 |
| `benchmark_projects/` | 用同一份 Verilog 在不同 IDE 下跑、对比综合结果的基准工程。 |
| `scripts/` | 一堆 TCL / 批处理 / Shell 脚本，用于编译、清理、提取时序数据等工程化操作。 |

> 说明：本讲只读 `README.md` 的文字与表格，**不深入任何 `.sv` 模块内部**。模块源码的精读从下一讲（u1-l2）开始。

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

1. **项目定位与目录结构**——回答"这仓库是什么、目录怎么组织的"。
2. **难度标签约定**——回答"这么多模块我该从哪个开始读"。

### 4.1 项目定位与目录结构

#### 4.1.1 概念说明

很多人第一次打开 basic_verilog 会疑惑：它的"入口"在哪？`main` 函数在哪？

答案是：**它根本没有传统意义上的入口**。basic_verilog 不是一个"跑起来给你一个功能"的应用程序，而是一个 **"必备可综合 Verilog / SystemVerilog 模块库"**——你可以把它理解成一个 **硬件代码的"标准零件库"或"乐高积木盒"**。

作者 Konstantin Pavlov 把多年 FPGA 项目里反复要用到的小电路（时钟分频、边沿检测、FIFO、UART、SPI……）整理成一个个风格统一、互相独立的 `.sv` / `.v` 文件，任何人都可以从中挑出需要的"积木"，拷贝到自己工程里直接例化使用。

这种定位决定了它有几个鲜明特点：

- **高度可复用**：每个模块都尽量参数化（位宽、深度可配），跨主流 FPGA 厂商通用。
- **风格统一**：几乎每个源文件都自带详细说明和"例化模板（instantiation template）"，复制粘贴即可用。
- **没有单一主链路**：不像一个 Web 服务有"请求→处理→响应"的主流程，它的价值在于"一篮子零件"本身。

#### 4.1.2 核心流程

阅读这样一个"积木库"仓库，推荐的认知流程是：

```text
读 README 标题区  →  理解项目定位
        ↓
读 Licensing 区   →  搞清能否商用、要不要署名
        ↓
读 DIRECTORY 表   →  在脑子里建立"目录→职责"的地图
        ↓
读 FILE 表        →  知道有哪些"零件"可挑，按难度标签排序阅读
        ↓
挑一个绿圈模块     →  打开对应 .sv，进入下一讲的学习
```

换言之，本仓库的"流程"不是代码执行流程，而是**读者的阅读流程**。README 就是这张流程图的导航员。

#### 4.1.3 源码精读

下面逐段对照真实的 `README.md` 来讲。每段都给出永久链接，点击可直接跳转到 GitHub 上对应行。

**① 项目定位：标题与开场白**

README 的开头就点明了仓库性质：

- [README.md:1-12](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/README.md#L1-L12)：标题写明 "Must-have verilog systemverilog modules"（必备的 Verilog / SystemVerilog 模块），并说明作者、原始仓库地址，以及"This is a collection of Verilog SystemVerilog **synthesizable** modules"——关键词 synthesizable（可综合）说明这些代码能变成真实电路。

> 第 1 行 `Must-have verilog systemverilog modules` 就是整个仓库的"一句话定位"；第 8 行强调代码在主流 FPGA 厂商和典型项目里**高度可复用**。

**② 开源协议：CC BY-SA 4.0**

- [README.md:14-18](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/README.md#L14-L18)：明确代码采用 **CC BY-SA 4.0**（知识共享 署名-相同方式共享 4.0）协议。

它的含义可以拆成两句：

- **你可以自由用**：remix（混编）、transform（改造）、build upon（在你的工程里使用），甚至**商业用途**都允许。
- **但有两个义务**：必须署名原作者（BY）；你基于它产出的衍生作品，必须以**相同的协议**（SA，Share-Alike）再发布。

协议的正式文本不在 README 里，而在仓库的 `license/` 目录下（例如 `license/Creative Commons — Attribution-ShareAlike 4_0 International — CC BY-SA 4_0.htm`），还有一个 `88x31.png` 是 CC 的官方小图标。这一点对工业项目尤其重要——上板量产前一定要确认协议义务。

**③ 目录索引表（DIRECTORY）**

README 用一张表把顶层目录的职责列清楚了：

- [README.md:29-44](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/README.md#L29-L44)：DIRECTORY 表。

挑几个对你后续学习最关键的目录说明（描述直接取自 README）：

| 目录（实际名） | README 中的说明 | 学习意义 |
|----------------|-----------------|----------|
| `example_projects/` | "FPGA project boilerplates and examples"（FPGA 工程样板与示例） | 第 4 讲（u1-l4）会带你打开这里的 Quartus / Vivado 模板，看一个真实工程长什么样。 |
| `benchmark_projects/` | "benchmarking various IDEs to compile exact same Verilog project"（用不同 IDE 编译同一份 Verilog 来做基准对比） | 第 u7-l3 讲会用它讲"同一份代码跨工具对比 Fmax"。 |
| `scripts/` | "useful TCL, batch and shell scripts"（实用的 TCL / 批处理 / Shell 脚本） | 第 u1-l3、u7-l2 讲会用到这里的 `iverilog_compile.bat`、`modelsim_compile.tcl`、`get_fmax_vivado.tcl` 等。 |
| `Advanced Synthesis Cookbook/` | "useful code from Altera's cookbook" | Altera（现 Intel FPGA）官方综合手册里的实用代码。 |
| `KCPSM6_Release9_30Sept14/` | "Xilinx's Picoblaze soft processor sources" | Xilinx 的 Picoblaze 软核处理器源码（第三方带入，非作者原创积木）。 |

> **两个值得注意的小细节（体现"读源码要核对现实"的好习惯）：**
>
> 1. README 第 38 行写的是 `dual_port_ram_templates/`，但仓库里实际的目录名是 `dual_port_single_port_ram_templates/`——README 的目录表略有滞后。
> 2. 仓库里还有一些 README 目录表**没有列出**的目录，例如 `info/`（存放 Questa 错误码列表）、`interfaces/`（存放 AXI/AXI-Stream/Wishbone 接口定义）、`scripts_common/`（最近一次提交 `2654273 separate directory for common scripts` 才独立出来的公共脚本目录）。这些都是 README 没及时同步、但实际存在的部分。

**④ 模块索引表（FILE）**

README 紧接着用第二张表逐文件列出每个"积木"：

- [README.md:46-104](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/README.md#L46-L104)：FILE 表，左列就是难度标签，中列是文件名，右列是一句话描述。

这张表是你日后"按需挑积木"的总索引。比如你想找"UART 发送器"，扫一眼就能定位到 `uart_tx.sv`。难度标签的用法在 4.2 节专门讲。

**⑤ 关于 testbench**

- [README.md:105-105](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/README.md#L105-L105)：README 最后一句 "Also added testbenches for selected modules." 说明作者为**部分**模块配了 testbench（即那些 `_tb.sv` 文件）。注意是"selected（精选的）"，并非每个模块都有测试。

#### 4.1.4 代码实践

> 这是一个**源码阅读型实践**，不需要任何编译工具，目的是让你亲手熟悉 README 的目录表与实际目录的对应关系。

**实践目标**：建立"README 说的目录 ↔ 仓库里真实存在的目录"的对照表，并体会 README 与现实之间可能存在的细微出入。

**操作步骤**：

1. 在本地克隆仓库（或在 GitHub 网页端浏览根目录）。
2. 打开 `README.md` 的 DIRECTORY 表（[README.md:29-44](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/README.md#L29-L44)）。
3. 对照仓库根目录的实际目录，列出三栏：**README 列出的目录名**、**实际存在的目录名**、**README 给的一句话职责**。

**需要观察的现象**：

- 大部分目录名一一对应（如 `example_projects/`、`benchmark_projects/`、`scripts/`）。
- 少数目录名对不上（如 README 的 `dual_port_ram_templates/` 对应实际的 `dual_port_single_port_ram_templates/`）。
- 有些实际目录 README 根本没列（如 `info/`、`interfaces/`、`scripts_common/`）。

**预期结果**（示例片段，待本地验证补全）：

| README 目录名 | 实际目录名 | README 职责 |
|---------------|-----------|-------------|
| `example_projects/` | `example_projects/` ✓ | FPGA 工程样板与示例 |
| `benchmark_projects/` | `benchmark_projects/` ✓ | 跨 IDE 编译同一份 Verilog 做基准 |
| `scripts/` | `scripts/` ✓ | 实用 TCL/批处理/Shell 脚本 |
| `dual_port_ram_templates/` | `dual_port_single_port_ram_templates/`（名不一致） | Block RAM 模板 |
| （未列出） | `info/` / `interfaces/` / `scripts_common/` | README 未说明 |

如果你观察到与本表不同的结果，以你本地的实际目录为准——**现实永远优先于文档**。

#### 4.1.5 小练习与答案

**练习 1**：basic_verilog 仓库里有没有一个叫 `main` 的"程序入口"？为什么？

<details>
<summary>参考答案</summary>

没有传统意义上的程序入口。因为 basic_verilog 是一个**可复用 RTL 模块库（积木盒）**，不是单一应用程序。仓库里确实存在名为 `main.sv` 的文件，但它们位于 `example_projects/*/src/` 下，是**示例工程的顶层模块**（把若干积木连起来演示），而不是"程序入口"。每个 `.sv` 模块都是独立可挑用的零件。
</details>

**练习 2**：某公司想把 basic_verilog 里的 `clk_divider.sv` 用进自己的商用产品，需要遵守什么义务？

<details>
<summary>参考答案</summary>

CC BY-SA 4.0 允许商业用途，但要求：(1) **署名（BY）**——必须注明原作者 Konstantin Pavlov 及原始仓库；(2) **相同方式共享（SA）**——基于这些代码产出的衍生作品，必须以同样的 CC BY-SA 4.0 协议发布。实务中"衍生作品"的边界（尤其是固化进比特流的硬件）建议咨询法务，但 README 第 14-18 行的字面义务就是这两条。
</details>

---

### 4.2 难度标签约定

#### 4.2.1 概念说明

一个积木盒里有上百个零件，初学者最容易卡在"我该先看哪个"。作者在 README 里给了一套非常贴心的小约定——**用两种颜色的圆圈给源文件打难度标签**：

- **绿圈** :green_circle: —— 最基础的（the most basic tasks）模块。
- **红圈** :red_circle: —— 进阶或特殊用途（advanced or special purpose routines）的模块。

这套标签的目的是**给读者一个推荐的阅读顺序**：如果你是硬件设计新手，就从绿圈文件开始读，它们实现简单、易于理解；等积累了信心，再去啃红圈的高级模块。

> 注意：没有圆圈的文件（FILE 表里左列为空的行）并不代表"中等难度"，只是作者**没有给它们打标签**，需要你自己看描述判断。

#### 4.2.2 核心流程

利用难度标签规划学习的流程非常简单：

```text
打开 README FILE 表
        ↓
只看左列带 :green_circle: 的行
        ↓
逐个打开这些 .sv 文件阅读（它们通常最短最直观）
        ↓
再挑选感兴趣的 :red_circle: 文件深入
```

这套"先绿后红"的顺序，也正是本学习手册前几个单元（u1、u2）刻意挑选绿圈模块（如 `clk_divider.sv`、`edge_detect.sv`、`gray2bin.sv`）作为起点的原因。

#### 4.2.3 源码精读

难度标签的定义写在 README 的 "Contents description" 一节：

- [README.md:20-27](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/README.md#L20-L27)：作者说明"为了方便，我按 difficulty（难度）给一些源文件打了标签"，并给出绿圈/红圈含义；第 26 行明确建议新手**先从绿圈代码看起**；第 27 行强调"几乎每个源文件都包含详细描述和例化模板"。

在 FILE 表（[README.md:46-104](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/README.md#L46-L104)）里，带绿圈的行可以一一找出来。下表汇总了 README 中**所有标记为绿圈**的文件（描述原文取自 README）：

| README 行 | 文件名 | README 一句话描述（原文） | 本地是否存在 |
|-----------|--------|---------------------------|--------------|
| [L50](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/README.md#L50-L50) | `bin2gray.sv` | "combinational Gray code to binary converter"（组合格雷码转二进制） | ✓ 存在 |
| [L54](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/README.md#L54-L54) | `clk_divider.sv` | "wide reference clock divider"（宽位参考时钟分频器） | ✓ 存在 |
| [L56](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/README.md#L56-L56) | `debounce.v` | "two-cycle debounce for input buttons"（按键两周期去抖） | ✗ **缺失**（见下方说明） |
| [L57](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/README.md#L57-L57) | `delay.sv` | "useful module to make static delays or to synchronize across clock domains"（静态延迟 / 跨时钟域同步） | ✓ 存在 |
| [L60](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/README.md#L60-L60) | `edge_detect.sv` | "combinational edge detector, gives one-tick pulses on every signal edge"（组合边沿检测，每个边沿出一个单拍脉冲） | ✓ 存在 |
| [L67](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/README.md#L67-L67) | `gray2bin.sv` | "combinational binary to Gray code converter"（组合二进制转格雷码） | ✓ 存在 |
| [L69](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/README.md#L69-L69) | `hex2ascii.sv` | "converts 4-bit binary nibble to 8-bit human-readable ASCII char"（4 位半字节转 8 位 ASCII 字符） | ✓ 存在 |
| [L100](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/README.md#L100-L100) | `uart_rx.sv` | "straightforward yet simple UART receiver"（简单直接的 UART 接收器） | ✓ 存在 |
| [L102](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/README.md#L102-L102) | `uart_tx.sv` | "straightforward yet simple UART transmitter"（简单直接的 UART 发送器） | ✓ 存在 |

> **两个务必留意的"坑"（核对现实后发现的）：**
>
> 1. **`debounce.v` 在仓库里并不存在**。README 第 56 行把 `debounce.v` 标为绿圈，但当前 HEAD 下根目录里只有 `debounce_v1.v`、`debounce_v2.sv`、`debounce_v2.v` 三个去抖相关文件，没有叫 `debounce.v` 的。这是 README 滞后于代码的典型例子。如果你要做绿圈清单，**请用实际存在的 `debounce_v1.v` / `debounce_v2.sv` 替代**。
> 2. **`bin2gray.sv` 与 `gray2bin.sv` 的描述疑似写反**。README 给 `bin2gray.sv` 的描述是"Gray code **to** binary"（格雷转二进制），给 `gray2bin.sv` 的描述是"binary **to** Gray"（二进制转格雷）——单看文件名（`bin→gray`、`gray→bin`）与描述的方向恰好对不上。具体谁对谁错，要等到 u6-l1 讲编码转换时打开源码才能定论，这里先留个心眼。

至于红圈 :red_circle:，README 中标记为红圈的条目包括 `fast_counter.sv`、`fifo_single_clock_ram_*.sv`、`fifo_single_clock_reg_*.sv`、`read_ahead_buf.sv`、`soft_latch.sv`、`true_dual_port_write_first_2_clock_ram.sv`、`true_single_port_write_first_ram.sv`、`gray_functions.vh`，以及目录表里的 `XilinxBoardStore_with_Alveo_cards_support`、`scripts_for_intel_hls/`、`scripts_for_xilinx_hls/` 等。它们大多涉及高级时序技巧或对特定厂商底层资源的依赖，初学阶段可以暂时跳过。

#### 4.2.4 代码实践

> 这是本讲的核心实践，也是规格指定的任务：**整理出所有绿圈模块清单**。

**实践目标**：把 README FILE 表里所有 `:green_circle:` 模块的"文件名 + 一句话用途"整理成一张干净的速查表，作为后续几讲的阅读目录；同时验证这些文件在仓库里真实存在。

**操作步骤**：

1. 打开 README 的 FILE 表（[README.md:46-104](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/README.md#L46-L104)）。
2. 逐行扫描左列，挑出所有带 `:green_circle:` 的行。
3. 对每个文件，在仓库根目录确认它是否真实存在（`ls` 或在 GitHub 网页查看）。
4. 把结果整理成下表那样的清单。

**需要观察的现象**：

- 绿圈模块共有 9 项（按 README 计）。
- 其中 `debounce.v` 在本地找不到，需要替换为实际存在的去抖文件。
- 其余 8 个文件都能在根目录直接打开。

**预期结果**（绿圈速查表，描述据 README 原文翻译）：

| 文件名 | 一句话用途 | 状态 |
|--------|-----------|------|
| `bin2gray.sv` | 组合电路：格雷码 ↔ 二进制转换（描述与文件名方向存疑，待 u6-l1 核实） | ✓ |
| `clk_divider.sv` | 宽位参考时钟分频器，用一个计数器同时得到多个慢时钟 | ✓ |
| `debounce.v` → 实际为 `debounce_v1.v` / `debounce_v2.sv` | 按键去抖动电路 | README 文件名已失效，以实际文件为准 |
| `delay.sv` | 静态延迟 / 跨时钟域同步的通用延迟模块 | ✓ |
| `edge_detect.sv` | 组合边沿检测，信号每个边沿输出一个单拍脉冲 | ✓ |
| `gray2bin.sv` | 组合电路：二进制 ↔ 格雷码转换（描述与文件名方向存疑，待 u6-l1 核实） | ✓ |
| `hex2ascii.sv` | 把 4 位二进制半字节转成 8 位可读 ASCII 字符 | ✓ |
| `uart_rx.sv` | 简单直接的 UART 接收器 | ✓ |
| `uart_tx.sv` | 简单直接的 UART 发送器 | ✓ |

> 如果你整理出的清单与上表不同（例如发现了更多/更少的绿圈项），以你**本地仓库当前 HEAD** 的 README 为准。这正是本实践想训练的"文档核对"习惯。

#### 4.2.5 小练习与答案

**练习 1**：FILE 表里左列为空（既没绿圈也没红圈）的行，代表什么含义？

<details>
<summary>参考答案</summary>

它表示作者**没有给该文件打难度标签**，并不等于"中等难度"或"不重要"。你需要阅读右列的一句话描述自行判断。例如 `cdc_data.sv`（标准两级数据同步器）没有标签，但它其实是跨时钟域设计的核心模块——本手册会在 u3-l1 重点讲解。
</details>

**练习 2**：请用难度标签，为一位完全的硬件新手排出前三个推荐的阅读对象（仅限绿圈）。

<details>
<summary>参考答案</summary>

可任选三个绿圈中"最像组合逻辑、最短"的入手，推荐顺序：`edge_detect.sv`（边沿检测，概念直观）→ `clk_divider.sv`（计数器分频，时序入门）→ `bin2gray.sv` / `gray2bin.sv`（纯组合位运算，最短小）。这三个绿圈模块恰好对应本手册 u2 单元的起点。
</details>

**练习 3**：为什么 README 标的 `debounce.v` 在仓库里找不到？遇到这种情况你该怎么办？

<details>
<summary>参考答案</summary>

最可能是作者把去抖模块重命名/拆分成了带版本号的 `debounce_v1.v`、`debounce_v2.sv`，但忘了同步更新 README。遇到"文档与现实不符"时，**永远以仓库当前代码为准**：用 `ls debounce*` 之类的方式找到真实文件，并可以顺手给上游提 issue 或 PR 修正 README。这也是为什么本手册强调"不编造、核对现实"。
</details>

## 5. 综合实践

把本讲两个最小模块串起来，完成一次"5 分钟仓库导览"小任务：

**任务**：假设你要向一位没接触过 FPGA 的同事介绍 basic_verilog，请基于本讲内容，产出一份**一页纸的《项目速览》**，必须包含以下四部分：

1. **一句话定位**：用你自己的话（不要照抄 README 标题）说明 basic_verilog 是什么。
2. **协议提示**：用一句话提醒同事使用这些代码时的法律义务（CC BY-SA 4.0 的两条核心要求）。
3. **目录地图**：从 README 的 DIRECTORY 表里挑 3 个你认为对新手最重要的目录，各写一句中文职责说明。
4. **新手阅读清单**：列出 3 个绿圈模块（文件名 + 一句话用途），作为同事的"第一周阅读作业"，并特别提醒他 README 里 `debounce.v` 与实际文件不符的坑。

**验收标准**：

- 第 1 点不能出现"应用/程序/软件入口"等误导性表述（因为它不是应用）。
- 第 2 点必须同时提到"署名"和"相同方式共享"。
- 第 4 点至少包含一个真实的、本地确实存在的绿圈文件（如 `clk_divider.sv`）。
- 全程不编造仓库里不存在的文件名。

> 完成后，把这张《项目速览》保存到你的笔记里——它就是你后续阅读源码时的"导航首页"。本任务无需任何工具链，纯文档阅读即可完成。

## 6. 本讲小结

- basic_verilog 是 Konstantin Pavlov 维护的 **必备可综合 Verilog / SystemVerilog 模块库**，本质是"硬件积木盒"，不是单一应用程序，没有传统入口。
- 代码采用 **CC BY-SA 4.0** 协议：可商用，但必须**署名**并以**相同协议**发布衍生作品；正式文本在 `license/` 目录。
- 顶层目录按职责组织：`example_projects/`（工程模板）、`benchmark_projects/`（跨 IDE 基准）、`scripts/`（编译/清理脚本）等，README 的 DIRECTORY 表是导航地图。
- README 用 **绿圈 :green_circle: / 红圈 :red_circle:** 给源文件标难度，新手应**先绿后红**地阅读。
- 本手册前几个单元（u1、u2）刻意从绿圈模块（`clk_divider.sv`、`edge_detect.sv`、`gray2bin.sv` 等）切入。
- **文档会滞后于代码**：README 里的 `debounce.v` 在仓库中实际不存在（应为 `debounce_v1.v` / `debounce_v2.sv`），`bin2gray.sv`/`gray2bin.sv` 的描述也疑似写反——养成"读文档、更核对代码"的习惯。

## 7. 下一步学习建议

本讲只是"站在门口看了一眼"。下一讲起，我们将真正打开 `.sv` 文件、看模块内部长什么样。推荐的学习顺序：

1. **下一讲 u1-l2《一个模块长什么样：统一的文件结构》**：以 `clk_divider.sv` 为标本，拆解仓库里几乎每个模块文件都遵循的"四段式"约定（头注释 / INFO / 例化模板 / module 实现）。这是读懂所有后续源码的"语法基础"。
2. **u1-l3《用仿真器跑起来》**：学会用 `main_tb.sv` 和 `scripts/` 下的脚本，亲手把一个模块仿真出波形。
3. **u1-l4《建一个真实 FPGA 工程》**：打开 `example_projects/` 里的 Quartus / Vivado 模板，看积木如何拼成一个能上板的工程。
4. 在阅读源码前，建议先自己完成本讲的"绿圈速查表"实践——它会成为你接下来几周的随手参考。

> 建议继续阅读的真实源码（按难度由低到高）：根目录下的 `clk_divider.sv`、`edge_detect.sv`、`gray2bin.sv` 三个绿圈文件，它们是 u2 单元的直接素材。
