# 项目总览：32 点 FFT 处理器是什么

## 1. 本讲目标

本讲是整本学习手册的第一篇，面向**完全没有接触过这个项目**的读者。读完本讲，你应当能够：

- 说清楚这个项目到底做了一个什么东西（一颗 32 点 FFT 处理器的 Verilog RTL，并跑通了 ASIC 全流程）；
- 用自己的话讲明白它采用的 **radix-2 DIF（频率抽取）算法**和 **SDC（单路延迟换向器）流水线架构**到底解决了什么问题；
- 记住并解释关键设计规格：周期 10 ns（100 MHz）、工艺 UMC 130nm、面积与功耗指标；
- 把 README 里的文字描述，与真实 RTL 端口、testbench 参数对应起来，而不是停留在"看过一遍"。

本讲只读 `README.md` 这一份文档，并交叉核对几处关键源码（端口、testbench 参数），不展开任何具体模块的实现细节——那是后续讲义的任务。

## 2. 前置知识

在开始之前，用最通俗的方式建立三个直觉概念即可，不要求你做过数字 IC 设计。

- **FFT（快速傅里叶变换）是什么？**
  傅里叶变换把一个时域信号（比如一段声音波形）拆成不同频率的正弦波叠加，得到它的"频谱"。直接算 N 点信号的频谱需要 \(O(N^2)\) 次乘法；FFT 是一种巧妙算法，把运算量降到 \(O(N\log_2 N)\)。本项目处理的是 N=32 的离散信号。

- **复数运算为什么重要？**
  FFT 的输入和输出都是复数（有实部 real 和虚部 imag 两路）。一次复数乘法 \((a+jb)(c+jd)\) 展开后包含 4 次实数乘法、2 次实数加法。复数乘法器是 FFT 硬件里最贵的资源，所以"如何少用乘法器/加法器"是这个项目反复出现的优化主题。

- **什么是流水线（pipeline）？**
  把一个大任务切成若干级，每一级处理完后把中间结果交给下一级，自己同时开始处理下一个输入。就像工厂流水线——单个产品要走完全程才能出厂（有延迟），但工厂每个时钟都能产出一个新产品（吞吐高）。本项目就是一条流水线式的 FFT。

- **什么是 ASIC？**
  ASIC（Application-Specific Integrated Circuit，专用集成电路）是为某个特定功能量身定制的芯片。本项目不只是写 Verilog 代码，还把代码经过"综合（Synthesis）→ 布局布线（Place & Route）"最终变成能在 UMC 130nm 工艺下流片的版图（GDS）。

下面所有内容都会围绕这些直觉展开。

## 3. 本讲源码地图

本讲涉及的关键文件很少，主要是一份文档加两处用于"核对事实"的源码：

| 文件 | 作用 | 本讲怎么用 |
| --- | --- | --- |
| [README.md](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/README.md) | 项目的唯一说明文档，描述算法、架构、模块、规格与全流程结果 | 本讲的"主教材"，四个最小模块都围绕它展开 |
| [RTL/FFT.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v) | FFT 顶层模块，定义了整个设计的对外端口 | 只看它的端口声明，用来核对输入/输出位宽 |
| [SIM/FFT_tb.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v) | 仿真测试平台（testbench） | 只看顶部的 `parameter`，用来核对 FFT 点数、位宽、时钟周期 |

> 提示：仓库根目录下还有 `RTL/`（10 个 Verilog 源文件）、`SIM/`（参考模型与测试激励）、`SYN/`（综合脚本与报告）、`Pnr/`（布局布线脚本与版图）、`Pics/`（架构示意图）等目录。它们的具体职责会在 [u1-l2 仓库结构与文件地图](u1-l2-repo-structure.md) 中专门讲解，本讲暂不深入。

## 4. 核心概念与源码讲解

### 4.1 项目背景与动机

#### 4.1.1 概念说明

首先要回答的问题是：**这个项目为什么存在？它想证明什么？**

数字信号处理（DSP）里，FFT 是出现频率最高的算法之一，从通信（OFDM 调制）、音频处理、雷达到医学成像都要用。软件算 FFT 太慢，所以人们用硬件（FPGA 或 ASIC）来加速它。硬件 FFT 处理器的研究重点通常落在三件事上：

1. **吞吐**——能不能每个时钟都吃进一个样本、吐出一个结果；
2. **资源**——用了多少乘法器、加法器、存储器（直接决定面积和功耗）；
3. **输出顺序**——FFT 中间结果天然是"乱序"的，最终输出要不要再排回正常顺序。

本项目就是一颗面向 ASIC 的 32 点流水线 FFT 处理器，它的核心贡献（动机）是在这三件事上做了一组权衡：**用一种新的"单路延迟换向器处理元（SDC PE）"，相比典型 radix-2 蝶形单元省掉了一个复数加法器；并用一个位反转器（bit reverser）把输出整理成正常顺序，同时把所需存储减半。**

#### 4.1.2 核心流程

作者在 README 开头给出的设计主张可以归纳成下面这条因果链：

```text
目标：一个"输出顺序正常、吞吐高、资源省"的流水线 FFT
        │
        ├── 算法选择：radix-2 DIF（频率抽取）→ 适合流水线展开
        ├── 架构选择：单路 SDC 流水线        → 100% 硬件利用率
        ├── 处理元创新：SDC PE               → 比典型蝶形省 1 个复数加法器
        │                                      → 整体加法器数量减半
        ├── 输出处理：bit reverser           → 输出正常顺序 + 存储减半
        │
        └── 验证：RTL 仿真 + Design Compiler 综合 + Innovus 布局布线 → 全流程跑通
```

四个关键卖点（来自 README 引言）需要记住，后续讲义会逐一从源码层面证明它们：

- 100% 硬件利用率（hardware utilization）；
- 整体加法器数量相比传统流水线 FFT 减半（50% reduction）；
- 用 bit reverser 让输出保持正常顺序（normal order）；
- bit reverser 同时实现 50% 的存储节省。

#### 4.1.3 源码精读

先读 README 最顶部的概述：

> 这段概述定位了项目：一个 32 点、基于 radix-2 DIF、采用流水线架构的 FFT 处理器。
>
> [README.md:L1-L2](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/README.md#L1-L2) —— 项目的 Overview，点明算法是 radix-2 DIF、结构是 pipelined。

接着读引言，这一段集中了项目的全部"创新点声明"：

> 引言里同时出现了四个关键术语：SDC PE（单路延迟换向器处理元）、100% 利用率、加法器减半、bit reverser。本讲的 4.3 节会逐一解释它们。
>
> [README.md:L4-L5](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/README.md#L4-L5) —— Introduction，列出 SDC PE、100% 利用率、50% 加法器节省、bit reverser 四个核心创新。

#### 4.1.4 代码实践

**实践目标**：用"勾画因果链"的方式把 README 引言里散落的卖点整理成结构化笔记，避免读完就忘。

**操作步骤**：

1. 打开 [README.md:L4-L5](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/README.md#L4-L5)。
2. 在笔记里建一张三列表格：`创新点 | README 原文短语 | 解决的问题`。
3. 至少填入四行：SDC PE、100% hardware utilization、50% adder reduction、bit reverser。

**需要观察的现象**：你会发现 README 把"省加法器"归功于 SDC PE，把"输出正常顺序"和"省存储"归功于 bit reverser——两者分工不同。

**预期结果**：得到一张类似下表的笔记（参考答案，可对照）：

| 创新点 | README 原文短语 | 解决的问题 |
| --- | --- | --- |
| SDC PE | "single-path delay commutator processing element" | 比典型蝶形单元省一个复数加法器 |
| 硬件利用率 | "100% hardware utilization" | 让流水线每个时钟都在干活，不空转 |
| 加法器节省 | "50% reduction in the overall number of adders" | 降低面积与功耗 |
| 位反转器 | "bit reverser … 50% reduction in memory usage" | 输出正常顺序 + 存储减半 |

#### 4.1.5 小练习与答案

**练习 1**：README 说 SDC PE "saves a complex adder compared with the typical radix-2 butterfly unit"。这里的"复数加法器"指的是几次实数加法？

> **参考答案**：一次复数加法 = 实部相加 + 虚部相加 = 2 次实数加法。所以"省一个复数加法器"等价于省 2 个实数加法器。详细的代数推导会在 [u7-l1 SDC 处理元的加法器优化分析](u7-l1-sdc-pe-adder-optimization.md) 展开。

**练习 2**：项目同时声称"输出正常顺序"和"存储减半"。这两件事是同一个模块（bit reverser）实现的吗？

> **参考答案**：是的。根据 README 引言，bit reverser 同时实现两个目标——把 DIF 天然的位反转输出还原成正常顺序，并在此过程中把所需存储减少 50%。具体硬件实现见 [u4-l2 输出排序模块](u4-l2-output-sort-bit-reversal.md)。

---

### 4.2 radix-2 DIF 算法概述

#### 4.2.1 概念说明

**DIF（Decimation In Frequency，频率抽取）** 是 radix-2 FFT 的两种经典分解方式之一（另一种是 DIT，时间抽取）。它的核心思想是：**把一个 N 点 DFT 按输出频率的奇偶拆成两个 N/2 点 DFT，递归下去直到变成 1 点。**

N 点 DFT 的定义是：

\[
X[k] = \sum_{n=0}^{N-1} x[n]\, W_N^{kn}, \quad W_N = e^{-j\frac{2\pi}{N}}
\]

DIF 把求和按下标 n 的前半（\(0 \sim N/2-1\)）和后半（\(N/2 \sim N-1\)）拆开，利用 \(W_N^{kN/2} = (-1)^k\)，可以得到：

\[
X[k] = \sum_{n=0}^{N/2-1} \big(x[n] + (-1)^k x[n+N/2]\big)\, W_N^{kn}
\]

- 当 k 为**偶数**（\(k=2m\)）：\(X[2m] = \sum_{n=0}^{N/2-1} (x[n]+x[n+N/2])\, W_{N/2}^{mn}\) —— 这是输入做**加法**后的 N/2 点 DFT。
- 当 k 为**奇数**（\(k=2m+1\)）：\(X[2m+1] = \sum_{n=0}^{N/2-1} \big((x[n]-x[n+N/2])\, W_N^n\big)\, W_{N/2}^{mn}\) —— 这是输入做**减法**再乘旋转因子后的 N/2 点 DFT。

这一加一减，加上一次乘旋转因子，就是一个 **radix-2 蝶形（butterfly）**。这正是后面"first half 做加减、second half 乘旋转因子"的数学根源。

#### 4.2.2 核心流程

对 32 点 FFT，DIF 递归分解的级数是 \(\log_2 32 = 5\) 级，README 把它描述成"逐级对半分"：

```text
Stage 1: 32 点 → 拆成两个 16 点序列
Stage 2: 16 点 → 拆成两个 8 点序列
Stage 3:  8 点 → 拆成 4 点序列
Stage 4:  4 点 → 拆成 2 点序列
Stage 5:  2 点 → 拆成 1 点（单个频域样本）
```

每一级做的都是同一件事——对当前序列做 radix-2 蝶形：上分支（加法）送往偶数频率子问题，下分支（减法 × 旋转因子）送往奇数频率子问题。

> 关键记忆点：**级数 = \(\log_2 N\)**。32 点要 5 级，64 点要 6 级，依此类推。这条规律在 [u7-l3 设计扩展](u7-l3-extending-the-design.md) 里会把 32 点推广到 64 点。

#### 4.2.3 源码精读

README 专门有一节用 4 步描述了这 5 级分解：

> 这段文字是算法侧的总纲，对应数学上的 DIF 递归。注意 Stage 3 的表述（"four 4-point"）在算法含义上是"继续对半分"，与"级数=5"一致。
>
> [README.md:L12-L22](https://github.com/abdelazeem201-Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/README.md#L12-L22) —— Radix-2 DIF FFT 节，把 32 点分解描述为 Stage 1~4 的逐级对半分。

更具体的"硬件上每一级蝶形长什么样"，README 在后面用三态机描述（waiting / first half / second half）：

> 这三态正是 DIF 蝶形的硬件实现：waiting 等够一半数据，first half 算 \(x[n]+x[n+N/2]\) 与 \(x[n]-x[n+N/2]\)，second half 把减法结果乘旋转因子。
>
> [README.md:L69-L75](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/README.md#L69-L75) —— RADIX-2 BUTTERFLY 节，描述蝶形的三态与"4 乘 2 加 → 3 乘 5 加"的优化。

> 说明：本节只建立"算法长什么样"的直觉，蝶形三态机和"3 乘 5 加"的源码级实现分别在 [u3-l2 radix2 蝶形单元](u3-l2-radix2-butterfly-pe.md) 与 [u7-l1 SDC 加法器优化](u7-l1-sdc-pe-adder-optimization.md) 深入。

#### 4.2.4 代码实践

**实践目标**：把 README 文字里的"5 级分解"和数学上的 \(\log_2 N\) 对齐，建立"级数 = 点数对数"的直觉。

**操作步骤**：

1. 读 [README.md:L12-L22](https://github.com/abdelazeem201-Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/README.md#L12-L22)。
2. 列一张表：`级数 | 该级输入序列长度 | 该级需要的蝶形个数`。
3. 提示：第 1 级有 32 个样本配成 16 对蝶形；第 2 级 16 个样本配 8 对……依此类推。

**预期结果**：

| 级数 | 输入序列长度 | 蝶形个数 |
| --- | --- | --- |
| 1 | 32 | 16 |
| 2 | 16 | 8 |
| 3 | 8 | 4 |
| 4 | 4 | 2 |
| 5 | 2 | 1 |

总蝶形数 = 16+8+4+2+1 = 31（正是 N−1 = 31）。这个数字会在 [u4-l1 流水线数据流串讲](u4-l1-pipeline-dataflow.md) 里再次出现。

#### 4.2.5 小练习与答案

**练习 1**：如果要把这个设计改成 64 点 FFT，需要多少级蝶形？

> **参考答案**：\(\log_2 64 = 6\) 级。需要在现有 5 级基础上再加 1 级。

**练习 2**：DIF 蝶形的"上分支"和"下分支"分别对应哪种频率输出？

> **参考答案**：上分支（加法 \(x[n]+x[n+N/2]\)）对应偶数频率；下分支（减法再乘旋转因子）对应奇数频率。这也正是 DIF 名字里"频率抽取"的由来——按频率的奇偶来抽取分解。

**练习 3**：为什么 DIF 的最终输出是"乱序"的，需要 bit reverser？

> **参考答案**：每一级都按"偶数频率在前、奇数频率在后"重排了输出，递归 5 级之后，最终输出顺序正好是自然顺序索引的位反转（bit-reversed）顺序。所以硬件需要一个排序模块把它还原成正常顺序。详见 [u2-l3 位反转与输出顺序还原](u2-l3-bit-reversal-output-order.md)。

---

### 4.3 SDC 流水线架构与设计亮点

#### 4.3.1 概念说明

**SDC = Single-path Delay Commutator（单路延迟换向器）**。拆开看三个词：

- **Single-path（单路）**：数据走一条主通路，而不是像 MDC（多路延迟换向器）那样并行多路。单路让控制简单，但要靠精细的时序让流水线不空转。
- **Delay（延迟）**：用 FIFO 式的移位寄存器把样本"暂存 N 拍"，等它的"配对样本"到来再做蝶形。比如要算 \(x[0]+x[16]\)，必须让 \(x[0]\) 在延迟单元里等到 \(x[16]\) 到来。
- **Commutator（换向器）**：让数据在上下两条支路之间切换，配合蝶形完成加减分支。

**SDC PE（处理元）** 是本项目首创的概念：它把"延迟换向"和"radix-2 蝶形"融合进同一个处理元，从而相比"典型蝶形单元"省下了一个复数加法器。这是本项目最核心的架构创新。

**流水线（pipelined）** 的含义：5 级蝶形串成一条流水线，每级之间插入寄存器，使得每个时钟都能从输入端喂入一个新样本、从输出端理论上得到一个新结果。

#### 4.3.2 核心流程

一条 SDC 流水线的每一级都由三类模块组成，三者形成反馈回路：

```text
              ┌─────────── 每一级的反馈回路 ───────────┐
              ↓                                        │
   din ──► [radix2 蝶形] ──加/减分支──► [shift 延时] ──┘
                 ▲                         │
                 │ state + 旋转因子          │ 延时后的"配对样本"
                 │                         │ 回流到蝶形
              [ROM 状态控制]
              （存旋转因子 + 产生 state）
```

- **radix2 蝶形**：做加减与复数乘法（三态机：waiting / first half / second half）。
- **shift 延时**：FIFO 移位寄存器，把样本暂存若干拍，凑齐蝶形的两个输入。
- **ROM 状态控制**：存旋转因子，同时用计数器产生 state 信号，告诉蝶形当前该处于哪个态。

5 级流水线的延时深度逐级减半：第 1 级延时 16、第 2 级延时 8、第 3 级延时 4、第 4 级延时 2、第 5 级延时 1。这与 4.2 节"序列长度逐级减半"完全对应——延时深度 = 该级序列长度的一半。

> 设计亮点小结（来自 README 引言）：单路 SDC 实现 100% 硬件利用率；SDC PE 把复数乘法从"4 乘 2 加"优化为"3 乘 5 加"，从而省下一个复数加法器、整体加法器减半；最后用 bit reverser 让输出排回正常顺序并节省存储。

#### 4.3.3 源码精读

README 对流水线架构与单路特性有专门描述：

> "Single-Path Delay" 一句点明了架构选择——单路、整条流水线，控制简单但要求精细的同步。
>
> [README.md:L24-L28](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/README.md#L24-L28) —— Pipelined Architecture 节，定义"单路延迟"架构。

延时单元的结构在 README 里有具体描述（这是后续 [u3-l3 移位寄存器](u3-l3-shift-delay-registers.md) 的入口）：

> 延时块就是 FIFO 移位寄存器，每拍移 24 位；延时 16 时寄存器大小 = 16×24 = 384 位。"24 位"对应内部数据通路宽度（输入 12 位符号扩展到 24 位以匹配旋转因子）。
>
> [README.md:L63-L67](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/README.md#L63-L67) —— Delay Block 节，给出 FIFO 移位寄存器与 16×24=384 的尺寸。

radix-2 蝶形的三态机与"3 乘 5 加"优化：

> 这是本项目最核心的两段描述：三态机（waiting/first half/second half）+ 复数乘法从 4 乘 2 加优化为 3 乘 5 加。
>
> [README.md:L69-L75](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/README.md#L69-L75) —— RADIX-2 BUTTERFLY 节，三态机与复乘优化。

ROM 与排序模块的职责：

> ROM 同时承担"存旋转因子"和"用计数器产生 state 驱动蝶形状态机"两件事；SORT 模块把乱序输出按已知顺序写入二维数组，用 32 拍完成排序。
>
> [README.md:L77-L81](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/README.md#L77-L81) —— ROM AND STATE CONTROL MODULE 与 SORT MODULE 两节。

#### 4.3.4 代码实践

**实践目标**：在文字层面把"三类模块的协作"在脑子里跑通一次，为后续读真实 RTL 打基础。

**操作步骤**：

1. 依次重读 [README.md:L63-L81](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/README.md#L63-L81)（Delay / RADIX-2 / ROM / SORT 四节）。
2. 用方框画一张单级流水线的草图：`din → 蝶形 → 延时（回流到蝶形）`，并把 ROM 用箭头指向蝶形标注"提供 state + 旋转因子"。
3. 在草图旁标注三态机的进入条件：
   - **waiting**：数据未到齐（如要等 \(x[16]\) 才能算 \(x[0]+x[16]\)）；
   - **first half**：输出加法结果，同时把减法结果送入延时模块；
   - **second half**：把延时回来的减法结果乘旋转因子。

**需要观察的现象**：你会发现 first half 的减法结果"暂时离开主通路"进 shift 延时，到 second half 才"回来"做乘法——这就是 SDC 反馈回路的精髓。

**预期结果**：得到一张能解释"为什么需要延时单元"和"三态怎么衔接"的草图。如果画不清楚，重读 [README.md:L69-L75](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/README.md#L69-L75)。

#### 4.3.5 小练习与答案

**练习 1**：README 说延时 16 的移位寄存器大小是 16×24=384 位。那么延时 8、延时 2 的寄存器分别有多大？

> **参考答案**：延时 8 → 8×24 = 192 位；延时 2 → 2×24 = 48 位。规律：寄存器位宽 = 延时深度 × 24 位。

**练习 2**：为什么 radix-2 蝶形需要 waiting 态？

> **参考答案**：因为 DIF 蝶形要配对计算 \(x[n]\) 与 \(x[n+N/2]\)。第一级 N/2=16，所以必须等 \(x[16]\) 到来才能和存在延时单元里的 \(x[0]\) 配对；前 16 个样本（\(x[0]\sim x[15]\)）因此都处于 waiting 态。

**练习 3**："3 乘 5 加"相比"4 乘 2 加"，节省了什么？代价是什么？

> **参考答案**：省下 1 个乘法器（4→3），代价是多用 3 个加法器（2→5）。由于加法器面积/功耗远小于乘法器，整体仍是净收益；再结合 SDC PE 的融合设计，整体加法器数量反而减半。完整推导见 [u7-l1](u7-l1-sdc-pe-adder-optimization.md)。

---

### 4.4 设计规格表解读

#### 4.4.1 概念说明

任何 ASIC 设计都有一份"规格表（specification）"，它列出芯片的关键物理指标，是衡量设计是否达标的尺子。需要先理解几个术语：

- **Cycle time（时钟周期）**：时钟信号一个完整周期的时间。周期 10 ns 意味着时钟频率 \(f = 1/T = 1/10\,\text{ns} = 100\,\text{MHz}\)。周期越小，芯片跑得越快，但时序越难满足。
- **Total area（总面积）**：综合后所有标准单元的面积之和，单位是 µm²（平方微米）。面积直接关系到芯片成本。
- **Power（功耗）**：芯片运行时消耗的功率，单位 mW。功耗关系到发热和电池续航。
- **Technology（工艺）**：晶圆代工厂的制造工艺节点。UMC 130nm 指台积电竞争对手联电（UMC）的 130 纳米工艺。工艺节点越小，晶体管越小、越快、越省电，但制造成本越高。

#### 4.4.2 核心流程

README 给出的设计规格表只有 4 行，但每一行都是一条设计约束：

```text
┌─────────────┬────────────────┐
│ Cycle time  │ 10 ns          │ ← 决定时钟频率 100 MHz
│ Total area  │ 202213.12 µm²  │ ← 综合后的标准单元总面积
│ Power       │ 9.9519 mW      │ ← 综合后功耗
│ Technology  │ UMC 130nm      │ ← 制造工艺
└─────────────┴────────────────┘
```

需要特别说明一个**容易混淆的点**：README 末尾的结论段还给了另一组数字——"100 MHz、1.27 mm 面积、28 mW 功耗、1.2V"。这两组数字并不矛盾，它们来自**流程的不同阶段**：

| 指标 | 综合后（Design Compiler） | 版图后（Innovus, post-layout） |
| --- | --- | --- |
| 面积 | 202213.12 µm² ≈ 0.20 mm²（纯标准单元） | 1.27 mm²（含 floorplan 白边的芯片/核面积） |
| 功耗 | 9.9519 mW | 28 mW（含实际互连线电容） |

简言之：规格表里的面积/功耗是"综合阶段"的估计值，结论段里的是"版图后"更接近真实的值。面积从 0.20 mm² 变成 1.27 mm²，是因为版图里还要留出电源环、布线通道和利用率（floorplan utilization≈0.7）造成的空白。

#### 4.4.3 源码精读

README 的设计规格表（本节的"主源码"）：

> 这是项目对外承诺的四项硬指标，本讲的实践任务就是要把它和源码端口对上。
>
> [README.md:L53-L59](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/README.md#L53-L59) —— Design Specification 表：周期 10 ns、面积 202213.12 µm²、功耗 9.9519 mW、工艺 UMC 130nm。

README 结论段的版图后指标：

> 注意这里出现"input size of 32 bits"——它指的是 FFT 规模为 32 点（32 samples），不要和单样本数据位宽（输入 12 位）混淆。100 MHz、1.27 mm、28 mW、1.2V 是版图后结果。
>
> [README.md:L89](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/README.md#L89) —— 结论段，给出版图后面积/功耗/电压及与现有方法对比（3× 字长、2× 低功耗）。

接下来用真实 RTL 端口来核对"输入 12 位、输出 16 位"——这两条不在规格表里，但写讲义时必须用源码证实，否则就是编造：

> FFT 顶层端口声明：`din_r/din_i` 是 `signed [11:0]`（12 位有符号），`dout_r/dout_i` 是 `signed [15:0]`（16 位有符号）。
>
> [RTL/FFT.v:L25-L34](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L25-L34) —— FFT 顶层模块端口：12 位输入、16 位输出。

testbench 顶部的参数把所有关键数字集中在一起，是最方便的核对入口：

> `FFT_size=32`、`IN_width=12`、`OUT_width=16`、`cycle=10.0`——这四个参数正好对应"32 点、12 位输入、16 位输出、10 ns 周期"。
>
> [SIM/FFT_tb.v:L10-L16](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L10-L16) —— testbench 参数区，集中定义点数、位宽、周期。

#### 4.4.4 代码实践

**实践目标**：完成本讲开篇交代的实践任务——整理出输入位宽、输出位宽、FFT 点数、时钟周期、工艺节点，并写一段 200 字以内的项目定位说明。

**操作步骤**：

1. 打开 [README.md:L53-L59](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/README.md#L53-L59)，抄下规格表。
2. 打开 [SIM/FFT_tb.v:L10-L16](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L10-L16)，核对 `FFT_size/IN_width/OUT_width/cycle` 四个参数。
3. 打开 [RTL/FFT.v:L25-L34](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L25-L34)，确认 `[11:0]` 与 `[15:0]` 与 testbench 一致。
4. 填写下表（参考答案已给出，请先自己填再对照）：

| 项目 | 值 | 出处 |
| --- | --- | --- |
| 输入位宽 | 12 bit（有符号） | FFT.v:29-30 / FFT_tb.v:12 |
| 输出位宽 | 16 bit（有符号） | FFT.v:32-33 / FFT_tb.v:13 |
| FFT 点数 | 32 | FFT_tb.v:10 |
| 时钟周期 | 10 ns（=100 MHz） | README.md:56 / FFT_tb.v:16 |
| 工艺节点 | UMC 130nm | README.md:59 |

5. 用自己的话写一段**不超过 200 字**的项目定位说明。

**预期结果**（参考定位说明，写自己的版本）：

> 本项目是一颗面向 ASIC 的 32 点流水线 FFT 处理器，采用 radix-2 DIF 算法与单路延迟换向器（SDC）流水线架构。输入为 12 位有符号复数（实部+虚部），输出为 16 位有符号复数；数据进入后符号扩展到 24 位以匹配定点旋转因子，经 5 级蝶形流水线处理后，由排序模块把位反转输出还原成正常顺序。设计目标周期 10 ns（100 MHz），基于 UMC 130nm 工艺，综合后面积约 202213 µm²、功耗约 9.95 mW。核心卖点是首创的 SDC 处理元，实现 100% 硬件利用率、整体加法器数量减半，并配合 bit reverser 让输出保持正常顺序且存储减半。

#### 4.4.5 小练习与答案

**练习 1**：周期 10 ns 对应的时钟频率是多少？请写出换算过程。

> **参考答案**：\(f = 1/T = 1/(10\,\text{ns}) = 1/(10 \times 10^{-9}\,\text{s}) = 10^{8}\,\text{Hz} = 100\,\text{MHz}\)。

**练习 2**：README 同时出现"面积 202213.12 µm²"和"1.27 mm"，这两个数为什么不冲突？

> **参考答案**：202213.12 µm²（≈0.20 mm²）是综合阶段的纯标准单元总面积；1.27 mm² 是版图后包含电源环、布线通道和 floorplan 白边的芯片/核面积。两者度量对象不同。

**练习 3**：输入是 12 位，但 README 在描述蝶形时说"din is extended to 24bits"。为什么要扩展？扩展成什么？

> **参考答案**：为了与 24 位的定点旋转因子对齐做乘法，并把输入符号扩展（sign-extend）防止溢出。12 位有符号输入通过高位符号扩展变成 24 位内部数据通路宽度。这与延时块"每拍移 24 位"一致。详见 [u3-l1 顶层 FFT 模块结构](u3-l1-top-level-fft-module.md)。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成下面这个"项目一页纸（one-pager）"小任务。

**任务**：假设你要向一位没读过 README 的同事用一页纸介绍这个项目，请基于本讲内容产出一份 **Markdown 笔记**，必须包含以下五块，且每块都要引用至少一条永久链接作为依据：

1. **一句话定位**：这是什么芯片/设计？（点数、算法、架构）
2. **数据接口**：输入/输出位宽、点数、时钟（用 `FFT.v` 与 `FFT_tb.v` 核对）。
3. **算法骨架**：radix-2 DIF 的 5 级分解，画出 32→16→8→4→2→1 的分解链。
4. **架构亮点**：SDC PE、100% 利用率、加法器减半、bit reverser 各一句话。
5. **物理指标**：周期、面积、功耗、工艺，并标注哪些是综合值、哪些是版图后值。

**评判标准**：

- 五块齐全、每块都有永久链接依据；
- 没有把"input size of 32 bits"误写成"32 位数据总线"（应理解为 32 点）；
- 没有把综合面积和版图面积混为一谈。

完成后，这份 one-pager 就是你后续阅读源码的"地图"——下一篇 [u1-l2 仓库结构与文件地图](u1-l2-repo-structure.md) 会带你把这张地图上的每个目录逐个打开。

## 6. 本讲小结

- 本项目是一颗 **32 点、radix-2 DIF、单路 SDC 流水线**的 FFT 处理器，完整跑通了 RTL 仿真 → Design Compiler 综合 → Innovus 布局布线的 ASIC 全流程。
- radix-2 DIF 把 32 点 DFT 递归分解成 5 级（\(\log_2 32=5\)）蝶形，每级做"加减 + 乘旋转因子"。
- 架构上由三类模块组成反馈回路：**radix2 蝶形 + shift 延时 + ROM 状态控制**，延时深度逐级减半（16/8/4/2/1）。
- 核心创新是 **SDC PE**：把复数乘法从 4 乘 2 加优化为 3 乘 5 加，整体加法器减半，并实现 100% 硬件利用率。
- 输出由 **bit reverser（SORT 模块）** 还原成正常顺序，同时节省 50% 存储。
- 关键规格（已用源码核对）：**12 位输入、16 位输出、32 点、10 ns（100 MHz）周期、UMC 130nm 工艺**；综合后面积约 202213 µm²、功耗约 9.95 mW。

## 7. 下一步学习建议

本讲只建立了"项目长什么样"的整体印象，还没有真正进入源码。建议按这个顺序继续：

1. **下一篇 [u1-l2 仓库结构与文件地图](u1-l2-repo-structure.md)**：把 `RTL/`、`SIM/`、`SYN/`、`Pnr/` 四大目录逐个打开，认清每个 Verilog 文件属于哪类模块（顶层 / 蝶形 / 移位 / ROM），这是后续所有源码阅读的索引。
2. **接着 [u1-l3 仿真快速上手](u1-l3-simulation-quickstart.md)**：用 testbench 第一次跑通设计，亲眼看到 in_valid/out_valid 与 SNR 判定，建立"这个设计真的能算"的信心。
3. 如果你对算法本身还不太熟，可以平行阅读 **U2 单元**（[u2-l1 radix-2 DIF 算法原理](u2-l1-radix2-dif-algorithm.md)），用仓库自带的 Python 参考模型 `SIM/FFT.py` 把本讲的算法直觉落到代码上。

> 阅读源码时，建议把本讲的"一页纸"笔记放在手边，每读一个模块就回来对照：它属于五级流水线的哪一级？它在 SDC 反馈回路里扮演 shift / radix2 / ROM 哪个角色？这样不会在细节里迷路。
