# 项目概览与整体架构

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标是带你从零认识 **RISC-V²（RISC-V-Vector）** 这个项目「是什么、解决什么问题、怎么组织」。读完本讲，你应当能够：

- 说清楚向量处理器在高吞吐、高能效上的价值，以及 RISC-V² 为什么把向量数据通路挂到一个双发射（two-way）超标量标量核上；
- 画出「标量核 + 向量数据通路」的整体连接关系，并解释指令如何被分流到两条路径；
- 认出向量数据通路里的四大阶段——`vrrm`、`vis`、`vex`、`vmu`，知道每一级大致做什么；
- 用自己的话讲清三大核心创新：寄存器重映射（register remapping）、解耦执行（decoupled execution）、动态归约树（reduction tree），外加「变延迟执行」。

本讲只做「俯瞰」，不展开任何一级的内部细节（那些留给后续讲义）。你只要建立起一张地图就够了。

## 2. 前置知识

本讲面向初学者，但有几个名词最好先有个印象：

- **ISA（Instruction Set Architecture，指令集架构）**：软件与硬件之间的「合同」，规定了一条指令长什么样、做什么运算。本项目的 ISA 是 RISC-V，并实现了它的 **V（Vector，向量）扩展**。
- **标量核（scalar core）**：一次处理一个数据（或少量数据）的通用处理器核，比如常见的 RISC-V RV32I 流水线。
- **向量指令（vector instruction）**：一条指令一次性对一组数据（一个「向量」）做同样的运算。例如一条 `vadd` 可以同时算好几个加法。
- **超标量（superscalar）/ 乱序（OoO, Out-of-Order）**：每个周期能发射并执行多条指令、并且允许打乱顺序执行的处理器设计。
- **RTL（Register Transfer Level，寄存器传输级）**：用硬件描述语言（本项目用 SystemVerilog）描述的、可以综合成真实电路的代码。
- **数据通路（datapath）**：数据从输入到输出流经的所有运算与存储部件连成的通路。

如果你对上面某项完全陌生也没关系，本讲会在用到时用一句大白话再解释一次。

## 3. 本讲源码地图

本讲涉及的「源码」其实主要是文档与一张架构图，外加一个顶层 RTL 文件用来佐证架构。这些都是你后续阅读所有讲义的「地基」。

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/README.md) | 项目总说明：定位、三大创新、目录结构、当前能力与未来计划。 |
| [rtl/README.md](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/README.md) | RTL 目录说明，列出向量数据通路里每个单元的职责，以及当前支持的全部指令。 |
| [images/core_ppln.png](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/images/core_ppln.png) | 核心流水线示意图（标量核 + 向量数据通路），是理解整体架构的关键图。 |
| [rtl/vector/vector_top.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv) | 向量数据通路顶层模块。本讲只用它来「验证」架构图里画的四级确实在代码里存在。 |

> 说明：`images/core_ppln.png` 是一张二进制图片，本讲义无法直接把它渲染出来。建议你在阅读时打开仓库里的这张图对照观看；下面我会用文字把它重新描述一遍。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1 项目定位与背景** —— RISC-V² 是什么、为什么需要它。
2. **4.2 架构概览：标量核 + 向量数据通路** —— 整体怎么连、指令怎么分流。
3. **4.3 三大核心创新点** —— 寄存器重映射、解耦执行、归约树（加变延迟）。

### 4.1 项目定位与背景

#### 4.1.1 概念说明

一句话定位（来自 README 标题）：

> RISC-V²: A vector processor core for the RISC-V Vector ISA extension
> （RISC-V²：一个面向 RISC-V 向量 ISA 扩展的向量处理器核）

它的全称写成 `RISC-V<sup>2</sup>`，读作「RISC-V 平方」，含义是「在 RISC-V 标量核之上，再叠加一个 RISC-V 向量扩展」，两层合在一起。

为什么有人愿意花大力气做向量处理器？README 第一段给出了向量架构「几乎独一无二」的能力组合（[README.md:2](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/README.md#L2)）：

- **高可编程性（high programmability）**：一条向量指令代表一组运算，程序员写起来像写标量循环，但硬件一次性算很多。
- **高吞吐（high computational throughput）**：靠多个 lane（运算通道）并行，一条指令等效多个标量运算。
- **高能效（high energy efficiency）**：取指/译码只发生一次，分摊到很多个数据上，每算一个数据的能耗很低。

这三者通常难以兼得，而向量架构是少数能做到兼顾的设计风格——这正是项目立项的动机。

#### 4.1.2 核心流程

从「需求」到「这个项目」的逻辑链可以这么走：

1. **应用需求**：图像/卷积、点积、SAXPY、FIR 滤波这类内核需要同时对大量数据做同样的运算。
2. **标量核不够用**：让标量核一个一个算，吞吐太低、能耗太高。
3. **引入向量数据通路**：加一条「向量数据通路」，一条指令吃进一整个向量。
4. **但控制流还是标量核说了算**：取指、译码、分支都交给成熟的标量核，向量通路只专心算。
5. **本项目 = 标量核 + 向量数据通路**：README 明确写了「RISC-V² is integrated in a two-way superscalar OoO core」（[README.md:11](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/README.md#L11)）——它被集成在一个双发射、乱序的超标量核里。

⚠️ 一个重要的现状提醒：README 里写明「目前标量核的 RTL 还没有公开」([README.md:19](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/README.md#L19))。所以你在仓库里能看到的、能仿真的，**只是向量数据通路本身**，外加一个测试台（testbench）来模拟标量核给它喂指令。这一点直接决定了后续讲义的重点都在向量侧。

#### 4.1.3 源码精读

项目当前的「能力清单」集中在 README 的「Repo State」一节（[README.md:29-L35](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/README.md#L29-L35)）：

```text
- Support for Integer Arithmetic, Memory operations & Reduction operations
- Support for register grouping and dynamic register file allocation
- Decoupled execution between computational and memory instructions
- Current maximum vector lanes supported is 16.
- SVAs have been used in simulation only. No formal verification runs at the moment.
```

读法翻译：

- 支持 **整数算术、访存、归约** 三大类运算（浮点还没做，见 Future Work）。
- 支持 **寄存器分组 + 动态寄存器堆分配**（这就是后面要讲的「寄存器重映射」的产物）。
- 计算指令流和访存指令流之间是 **解耦执行** 的。
- 当前 **最多支持 16 个 lane**（为什么是 16，跟归约树有关，4.3 会讲）。
- **SVA 断言只在仿真里用过，还没做形式验证**——这是验证侧的边界，后面有专门讲义。

README 的「Future Work」一节（[README.md:37-L42](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/README.md#L37-L42)）则告诉你这个项目「还没做到但想做」的方向：对齐更新的 RISC-V ISA、换更快的乘除法硬件、把 `vis↔vmu` 的路径解耦以缓解时序紧张、给执行流水加背压以支持 >16 lane、加浮点 lane。这些都是后续讲义会反复提到的「硬限制」来源。

#### 4.1.4 代码实践

> **实践：阅读 README，用一句话定义本项目。**

1. **实践目标**：建立你自己的、不超过 30 字的项目一句话定义。
2. **操作步骤**：
   - 打开 [README.md](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/README.md)，只读第 1–20 行。
   - 找到三个关键词：向量处理器、RISC-V 向量扩展、双发射超标量核。
   - 用这三个词组织一句话。
3. **需要观察的现象**：你会注意到 README 同时提到了「scalar core（标量核）」和「vector datapath（向量数据通路）」，并且强调标量核才是主控处理器。
4. **预期结果**：你的定义里应当同时包含「控制由标量核负责」和「重活由向量数据通路干」这两层意思。例如：*RISC-V² 是一个把 RISC-V 向量扩展数据通路挂到双发射超标量标量核旁、由标量核统一取指译码的向量处理器核。*
5. 这是纯阅读型实践，不需要运行任何命令。

#### 4.1.5 小练习与答案

**练习 1**：README 说向量架构「几乎独一无二」地同时具备哪三种能力？

<details><summary>参考答案</summary>

高可编程性（high programmability）、高吞吐（high computational throughput）、高能效（high energy efficiency），见 [README.md:2](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/README.md#L2)。

</details>

**练习 2**：为什么仓库里能跑仿真的只是「向量数据通路」，而不是完整的标量核 + 向量通路？

<details><summary>参考答案</summary>

因为标量核的 RTL 目前尚未公开（[README.md:19](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/README.md#L19)）。所以测试台用一个 driver 来模拟标量核，把已译码的向量指令喂给向量数据通路。

</details>

---

### 4.2 架构概览：标量核 + 向量数据通路

#### 4.2.1 概念说明

整体架构可以用一句话概括（README 第 15–18 行）：

> The scalar core acts as the main control processor, with all the instructions being fetched and decoded in the scalar pipeline. During the superscalar issue stage, the instructions are diverted to the correct path (i.e., scalar, or vector), based on their type. A vector instruction queue decouples the execution rates of the two datapaths.

来源：[README.md:15-L18](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/README.md#L15-L18)。

翻译成三件事：

1. **标量核是主控**：所有指令（无论标量还是向量）都先在标量流水线里取指、译码。
2. **发射期分流**：在超标量发射阶段，按指令类型把它送到「标量路径」或「向量路径」。
3. **向量指令队列解耦**：两条数据通路各跑各的，靠一个向量指令队列缓冲，互不卡死。

这就是 `images/core_ppln.png` 那张图的核心内容：左边是标量核流水线，到发射阶段分出一条岔路走向右边的向量数据通路。

#### 4.2.2 核心流程

下面用文字把 `core_ppln.png` 重新画一遍，方便没图也能看懂：

```text
            ┌──────────────────────── 标量核（主控，本仓库暂未公开）────────────────────────┐
            │   取指 Fetch → 译码 Decode → 超标量发射 Issue（在这里按类型分流）            │
            └──────────┬──────────────────────────────────────────────┬────────────────────┘
                       │ 标量指令                                      │ 向量指令
                       ▼                                              ▼
                 标量数据通路                              ┌── 向量指令队列（解耦两路速率）──┐
                 （本项目不含）                            │              ▼                  │
                                                          │     vector_top（向量数据通路）   │
                                                          └──────────────────────────────────┘
```

进入 `vector_top` 之后，向量数据通路内部是一条「主路 + 一条存储岔路」的结构（这点在 4.2.3 用源码确认）。主路是 **vRRM → vIS → vEX** 三级整数执行流水，存储岔路是 **vMU**：

```text
                         to_vector instr_in
                                 │
                                 ▼
                        ┌────────────────┐
                        │   vRRM 重映射   │  寄存器重映射、分组、分配
                        └─────┬────┬──────┘
              整数指令路径 ───┘    └─── 存储指令路径
                     ▼                        ▼
                ┌─────────┐              ┌─────────┐
                │  vIS    │ 计分板/冒险   │  vMU    │ load/store/分块预取
                │  发射   │              │ 存储单元│
                └────┬────┘              └────┬────┘
                     ▼                        │
                ┌─────────┐                   │
                │  vEX    │ 执行 lane、归约树  │
                │  执行   │                   │
                └────┬────┘                   │
                     │   写回                 │ 写回 / unlock
                     └─────────── 向量寄存器堆（VRF）─────┘
```

四级的名字和职责，`rtl/README.md` 的表格给得最清楚（[rtl/README.md:12-L21](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/README.md#L12-L21)）：

| 单元 | 全称 | 职责（README 原意） |
| --- | --- | --- |
| **vrrm** | Vector Register Remap | 寄存器分组与分配（register grouping and allocation） |
| **vis** | Vector Issue | 基于计分板做冒险跟踪与操作数选择（hazard tracking and operand selection） |
| **vex** | Vector Execution | 包含向量 lane 及其外围逻辑与连接 |
| **vmu** | Vector Memory Unit | 三个子引擎（load/store/tile-prefetch）+ 它们之间的仲裁 |

> 表格里还列了 `vex_pipe`（单条执行 lane）、`vmu_ld_eng` / `vmu_st_eng` / `vmu_tp_eng`（load/store/分块预取引擎）。它们都是上面四大阶段的「内部零件」，本讲不展开。

#### 4.2.3 源码精读

现在用顶层 RTL 来「验证」上面这张图。打开 [rtl/vector/vector_top.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv)，能看到四级被依次例化：

- **vRRM 阶段**：模块例化与注释 `// vRRM STAGE`（[vector_top.sv:45-L73](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L45-L73)）。它输出两路：`instr_remapped`（整数路径）和 `m_instr_out`（存储路径）——**这就是数据通路在顶层「分叉」的地方**。
- **vRR/vIS 与 vRR/vMU 之间**：用弹性缓冲 `eb_buff_generic` 做流水线寄存器（[vector_top.sv:84-L136](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L84-L136)）。注意这里有个 `generate if (USE_HW_UNROLL)`：开硬件展开时插入缓冲，关掉则直接旁路（bypass）。
- **vmu 存储单元**：注释 `// MEMORY UNIT`，例化 `vmu`（[vector_top.sv:137-L216](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L137-L216)）。它的输入正是上面分叉出来的 `m_instr_out_r`。
- **vIS 发射级**：注释 `// ISSUE STAGE`，例化 `vis`（[vector_top.sv:217-L298](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L217-L298)）。它同时连到 vmu 的读端口、转发点和写回——也就是说 **vIS 是整数路径和存储路径「交汇」的地方**。
- **vEX 执行级**：注释 `// EX STAGE`，例化 `vex`（[vector_top.sv:340-L378](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L340-L378)）。

还有一个很关键的「全景信号」——`vector_idle_o`。它把四级是否空闲「与」在一起（[vector_top.sv:42-L44](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L42-L44)）：

```systemverilog
logic vrrm_idle, vis_idle, vex_idle, vmu_idle;

assign vector_idle_o = vrrm_idle & vis_idle & vex_idle & vmu_idle & rst_n;
```

含义：**只有当 vrrm、vis、vex、vmu 四级全都空闲、且复位已释放时，整个向量数据通路才被认为「空闲」**。这个信号后面会被测试台用来判断「程序跑完了没」。

`vector_top` 的参数列表（[vector_top.sv:10-L24](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L10-L24)）也透露了架构的几个关键旋钮：`VECTOR_REGISTERS=32`（32 个向量寄存器）、`VECTOR_LANES=8`（默认 8 lane）、`USE_HW_UNROLL=1`（默认开硬件展开）、`FWD_POINT_A=1` / `FWD_POINT_B=3`（两个转发点位置）。这些参数会在后续讲义逐一展开，本讲你只要知道「它们存在、且在顶层就能调」即可。

#### 4.2.4 代码实践

> **实践：手画整体连接图，并标注四大阶段。**

1. **实践目标**：把「标量核 + 向量数据通路」的连接关系内化成一张你自己画的图。
2. **操作步骤**：
   - 准备一张纸或任意画图工具。
   - 画出标量核（取指 → 译码 → 超标量发射），在发射阶段画一个「分叉点」。
   - 从分叉点画出两条路：一条到「标量数据通路」，一条经过「向量指令队列」到 `vector_top`。
   - 在 `vector_top` 内部，按 [vector_top.sv:45](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L45) 起的顺序画出 `vrrm → vis → vex` 主路，并从 `vrrm` 处分叉出一条到 `vmu` 的存储岔路。
   - 在 `vis` 处画一个「与 vmu 相连」的交汇点（对应它连到 vmu 读端口/写回/unlock）。
3. **需要观察的现象**：你会清楚地看到整数指令和存储指令在 `vrrm` 之后就「分家」了，但在 `vis` 处又有数据/控制上的交汇。
4. **预期结果**：图上能明确标出 `vrrm`、`vis`、`vex`、`vmu` 四个标签，以及一条「向量指令队列」把标量核和向量通路隔开。
5. 纯阅读 + 画图实践，无需运行命令。

#### 4.2.5 小练习与答案

**练习 1**：指令是在哪个阶段被分流到标量路径或向量路径的？

<details><summary>参考答案</summary>

在标量核的 **超标量发射阶段（superscalar issue stage）** 按指令类型分流，见 [README.md:16](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/README.md#L16)。

</details>

**练习 2**：`vector_top` 里，整数指令路径和存储指令路径在哪里「分叉」，又在哪里「交汇」？

<details><summary>参考答案</summary>

在 **vRRM 阶段**分叉（`instr_remapped` 走整数路、`m_instr_out` 走存储路，见 [vector_top.sv:45-L73](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L45-L73)）；在 **vIS 发射级**交汇（vis 连到 vmu 的读端口、写回与 unlock，见 [vector_top.sv:256-L282](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L256-L282)）。

</details>

---

### 4.3 三大核心创新点

#### 4.3.1 概念说明

README 在开头列举了本项目相对「传统向量处理」的几项新设计（[README.md:2-L9](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/README.md#L2-L9)）。这里把它们拆成「三大创新 + 一项附加特性」：

**创新 1：寄存器重映射（register remapping）+ 动态寄存器堆**
> 一种新的寄存器重映射技术，配合动态分配的寄存器堆，实现 **基于硬件的动态循环展开**，并在运行时优化指令调度。

直觉解释：传统向量核里，一个「架构寄存器名」（比如 `v2`）固定对应一份物理存储。RISC-V² 让 `v2` 在不同循环迭代里可以 **映射到不同的物理寄存器**，于是硬件就能把多个迭代的运算「铺开」并行做，而不用担心它们抢同一个寄存器。这就是「硬件循环展开」，但完全由硬件在运行时自动完成，不需要编译器插指令。这一创新的硬件落点就是 **vRRM** 阶段（以及配套的 VRAT 别名表、VRF 寄存器堆）。

**创新 2：解耦执行（decoupled execution）的 acquire-release 语义**
> 设计的解耦执行方案用「资源获取-释放」语义，在并行的计算指令流和访存指令流之间消除歧义，从而允许两条流有各自独立的速率。

直觉解释：计算（vEX）和访存（vMU）本来是两条独立的流——算数快、访存慢。如果让它们互相等待，快的就被慢的拖死。RISC-V² 让它们各跑各的：访存先「锁住（acquire）」它要写的寄存器，算完再「解锁（release）」；计算侧靠一个 **ticket（票据）** 知道数据什么时候到。这样两条流的速率可以不同步，整体吞吐更高。

**创新 3：动态生成的硬件归约树（reduction tree）**
> 动态生成的硬件归约树，能显著加速归约指令（如点积、求和）。

直觉解释：归约（reduction）是把一串数据「折」成一个，比如求和、点积。用一棵二叉树状的加法器网络，可以在 \(\log_2 N\) 层里把 N 个 lane 的结果并起来，而不是一个个串行加。RISC-V² 的做法是 **按需动态生成** 这棵树的连线（见后续 vEX/v_int_alu 讲义），所以硬件代价被控制得比较好。

**附加特性：变延迟执行（variable execution latency）**
> 基于指令类型的变执行延迟。

直觉解释：简单运算（如 `vadd`）1 周期就完事，乘法、除法要多个周期。RISC-V² 不强行让所有指令走一样的节拍，而是让每种指令用「该用的」周期数，配合计分板管理依赖。这能提升频率、省面积。

#### 4.3.2 核心流程

把三项创新对应到数据通路：

```text
创新                  主要硬件落点                 带来的好处
────────────────────────────────────────────────────────────────────
寄存器重映射          vRRM（+ VRAT/VRF）           硬件循环展开、运行时调度
解耦执行              vIS 的 lock ↔ vMU 的 unlock  计算流/访存流各自独立速率
归约树                vEX 内的跨 lane 归约网络      归约指令 log2(LANES) 级加速
变延迟                vEX 各 ALU 不同周期数         提频、省面积
```

关于「为什么最多 16 lane」：归约树是一棵跨所有 lane 的二叉树，lane 数翻倍，树深 +1、互连也更复杂。README 在 Future Work 里写得很直白——要支持 `vector_lanes > 16`，得先给执行流水加背压（back-pressure），因为归约树支撑不了更多 lane（[README.md:41](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/README.md#L41)）。所以 16 lane 是当前架构的一个「硬上限」。

#### 4.3.3 源码精读

本讲的「源码精读」主要是把这些创新 **定位到文件**，让你知道后续在哪一篇讲义会真正读它（本讲不展开内部实现）：

- **寄存器重映射**：创新 1 的硬件在 [rtl/vector/vrrm.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv)（重映射主控）、[rtl/vector/vrat.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrat.sv)（架构→物理别名表）、[rtl/vector/vrf.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrf.sv)（动态分配的寄存器堆）。这三者会在 u2-l3 / u2-l4 详讲。
- **解耦执行**：创新 2 体现在 `vis` 的 lock 接口与 `vmu` 的 unlock 接口在 `vector_top` 里直接对连。看 [vector_top.sv:211-L215](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L211-L215)（vmu 输出 unlock）与 [vector_top.sv:278-L282](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L278-L282)（vis 接收 unlock）——同一组信号 `unlock_en/unlock_reg_a/unlock_reg_b/unlock_ticket` 一头连 vmu、一头连 vis。这条线就是「acquire-release」的物理体现，会在 u4-l1 专题讲。
- **归约树**：创新 3 的代码在 [rtl/vector/v_int_alu.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/v_int_alu.sv) 与 [rtl/vector/vex.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex.sv) 的跨 lane 连线里，u2-l8 详讲。
- **变延迟**：附加特性体现在 `vex_pipe` 内不同 ALU 的周期数，由参数 `FWD_POINT_A/B`（[vector_top.sv:20-L21](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L20-L21)）控制转发点，u4-l2 详讲。

> 提示：本节给的链接只是为了「定位」。如果你现在点进去看不懂内部实现，完全正常——那是后续讲义的内容。本讲你只要记住「这项创新在哪个文件里」即可。

#### 4.3.4 代码实践

> **实践：在 README 里为每项创新找到原文依据。**

1. **实践目标**：确认三大创新都是项目自己的声明（而非我们杜撰），并能把每条声明翻译成大白话。
2. **操作步骤**：
   - 打开 [README.md 第 2–9 行](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/README.md#L2-L9)。
   - 把四条 bullet（register remapping / decoupled execution / reduction tree / variable latency）分别用中文抄一遍。
   - 在每条后面标注「它对应的硬件模块/文件」。
3. **需要观察的现象**：你会发现 README 的措辞里，「decoupled execution」明确提到「resource acquire-and-release semantics」——这正是后面 lock/unlock 的英文叫法。
4. **预期结果**：你得到一张「创新 → 原文 → 硬件落点」的小表，与上面 4.3.2 的表内容一致。
5. 阅读型实践，无需运行命令。

#### 4.3.5 小练习与答案

**练习 1**：创新 1（寄存器重映射）最大的好处是什么？

<details><summary>参考答案</summary>

实现 **基于硬件的动态循环展开**（dynamic hardware-based loop unrolling），并在运行时优化指令调度。来源 [README.md:4-L5](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/README.md#L4-L5)。

</details>

**练习 2**：README 说要支持超过 16 lane，需要先做什么？

<details><summary>参考答案</summary>

需要先给执行流水加 **背压（back-pressure）**，因为归约树当前无法支撑更多 lane。来源 [README.md:41](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/README.md#L41)。

</details>

**练习 3**：「解耦执行」用的是什么语义来消除两条流之间的歧义？

<details><summary>参考答案</summary>

「资源获取-释放（acquire-and-release）」语义，见 [README.md:6-L7](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/README.md#L6-L7)。在硬件上对应 vis 侧的 lock（acquire）与 vmu 侧的 unlock（release）。

</details>

---

## 5. 综合实践

> **综合任务：写一份一页纸的「RISC-V² 架构速览」。**

把本讲三个模块串起来，假设你要向一个完全没接触过这个项目的同事用一页纸讲清楚它。要求你的速览包含：

1. **一句话定位**（来自 4.1）：标量核 + 向量数据通路，标量核主控、向量通路干活。
2. **一张整体连接图**（来自 4.2）：标量核 → 发射分流 → 向量指令队列 → `vector_top`，内部标注 `vrrm`/`vis`/`vex`/`vmu` 四级，并画出整数主路与存储岔路的分叉/交汇。
3. **三大创新 + 一项附加特性**（来自 4.3）：寄存器重映射、解耦执行、归约树、变延迟，各配一句大白话和一个硬件落点文件。
4. **两个已知边界**：标量核 RTL 暂未公开（所以只仿真向量侧）；当前最多 16 lane（受归约树限制）。

完成后，你可以用本讲给出的链接清单（README、rtl/README、vector_top.sv）逐条核对，确保速览里没有任何一句是你「脑补」出来的、找不到源码依据的话。如果你愿意，还可以把这张速览贴在你后续阅读每一篇讲义的第一页，作为总索引。

> 这个综合实践是纯阅读 + 写作型，无需运行任何仿真命令。真正的「跑仿真」实践安排在 u1-l5。

## 6. 本讲小结

- RISC-V² = 把 **RISC-V 向量扩展数据通路** 挂到一个 **双发射超标量标量核** 旁的向量处理器核；标量核是主控，负责取指译码，向量通路负责重活。
- 指令在标量核的 **超标量发射阶段** 被分流到标量路径或向量路径；一个 **向量指令队列** 把两条数据通路的速率解耦。
- 向量数据通路内部是 **vRRM（重映射）→ vIS（发射）→ vEX（执行）** 主路，外加一条 **vMU（存储）** 岔路；整数路径和存储路径在 `vrrm` 分叉、在 `vis` 交汇，这些都在 `vector_top.sv` 里能看到。
- `vector_idle_o = vrrm_idle & vis_idle & vex_idle & vmu_idle & rst_n`——四级全空闲才算整路空闲。
- 三大核心创新是 **寄存器重映射（vRRM/VRAT/VRF）、解耦执行（vis 的 lock ↔ vmu 的 unlock）、动态归约树（vEX/v_int_alu）**，外加 **变延迟执行**。
- 当前能力边界：只仿真向量侧（标量核 RTL 未公开）、最多 16 lane（归约树限制）、只做了仿真断言未做形式验证、暂无浮点 lane。

## 7. 下一步学习建议

本讲只搭了「俯瞰图」，接下来建议你：

1. **先看仓库怎么组织、怎么编译**：进入 **u1-l2（仓库目录结构与构建流程）**，搞清楚 `rtl/`、`sva/`、`vector_simulator/` 各自装了什么，以及 `files_rtl.f` / `compile_vector_simulator.do` 怎么把设计编译起来。
2. **再看可调旋钮**：进入 **u1-l3（设计参数与可调旋钮）**，读懂 `params.sv`，搞清楚哪些参数能调（如 `VECTOR_LANES`）、哪些不能。
3. **再认识共享类型**：进入 **u1-l4（共享类型与宏定义）**，读懂 `vstructs.sv` / `vmacros.sv`，为后续阅读 `vector_top.sv` 内部做好准备。
4. **最后亲手跑一次仿真**：进入 **u1-l5（端到端跑通仿真）**，把 `vvadd` 示例跑起来，看到真实的波形和性能日志。

> 想提前「偷看」实现细节也没问题：u2 单元会自顶向下带你读 `vector_top` 顶层连线、弹性缓冲、寄存器子系统、计分板与执行流水。但建议先把 u1 的五篇过一遍，建立全局观再钻细节。
