# 顶层接口与数据流总览

## 1. 本讲目标

前两讲（u1-l1、u1-l2）我们已经知道了「这个项目是做什么的」以及「怎么把它跑起来」。本讲要回答第三个问题：**仿真在跑的那块硬件，从外面看长什么样？数据是怎么从输入一步步走到输出的？**

我们打开整个设计的「外壳」——顶层模块 `topSystolicArray.sv`。它把内部复杂的脉动阵列、矩阵变换、控制计数器全部封装起来，对外只暴露最简单的接口。本讲我们只看这层外壳和它的整体数据通路，暂不深入内部细节（那些留给进阶层 u2）。

读完本讲，你应当能够：

1. 读懂 `topSystolicArray` 的端口表：说出 `i_a/i_b/i_validInput` 这些输入、`o_c/o_validResult` 这些输出各自的位宽与含义，以及时钟 `i_clk` 和异步复位 `i_arst` 的约定。
2. 用一句话说清从 `i_validInput` 拉高到 `o_validResult` 拉高之间，数据依次经过了哪几个处理环节，并能把这些环节对到源码行号。
3. 把 README 里的接口波形图（interface、inputSequence）对应到源码信号，理解「何时驱动输入、何时采样输出」。

## 2. 前置知识

如果你跟着前两讲读过 README 和 Makefile，本讲用到的新概念不多。这里只补三个最关键的。

### 2.1 什么是顶层模块（top module）

一个硬件设计通常由很多小模块拼成（本项目就有 PE、阵列、顶层三个层次）。**顶层模块**是放在最外面、把所有内部模块包起来的那一个，它对外的端口就是整个芯片（或整个 IP）的管脚。仿真器、综合工具、测试台都只直接认识顶层。本项目的顶层叫 `topSystolicArray`，这也是 u1-l2 里 Makefile 中 `TOP_MODULE` 的取值。

### 2.2 同步时序与异步复位

数字电路里有一种最常见的写法：所有寄存器都在**时钟上升沿**更新。本项目的 `always_ff @(posedge i_clk, ...)` 就是这个意思。`i_arst` 是**异步复位**：它一旦拉高，不等下一个时钟沿，立刻把寄存器清零。本项目里复位是**高有效**（active high）——`i_arst = 1` 表示「正在复位」。这套约定会反复出现在后面每个模块里。

### 2.3 valid 握手（valid-based handshake）

模块之间怎么知道「现在数据有效」？最简单的方式是配一根「有效」信号：发送方放好数据的同时把这根信号拉高，接收方看到信号拉高就去读数据。这叫 **valid-based handshake**（基于 valid 的握手）。本项目顶层用 `i_validInput` 表示「输入矩阵有效」，用 `o_validResult` 表示「结果矩阵有效」，就是这种握手。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲怎么用它 |
|------|------|--------------|
| `rtl/topSystolicArray.sv` | 顶层模块，封装矩阵变换 + 阵列 + 控制 | 本讲主角，精读端口表与整体通路 |
| `README.md` | Design Outline 段落描述了顶层接口与波形 | 把 README 的文字、波形图对应到源码信号 |

本讲还会顺带提到（但不深入）两个被顶层调用的内部模块，它们的细节在进阶层 u2 精读：

- `rtl/systolicArray.sv`：把 N×N 个 PE 连成阵列（对应 u2-l2）。
- `rtl/pe.sv`：单个处理单元，做一次 8 位乘加（对应 u2-l1）。

> 提示：本讲默认 N=4（4×4 方阵），与前两讲一致。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：先看**顶层端口与时钟/复位约定**，再串起 **validInput → 变换 → 阵列 → validResult 的整体通路**，最后做**接口时序波形对照**。

### 4.1 顶层端口与时钟/复位约定

#### 4.1.1 概念说明

一个好的顶层模块，接口要尽量简单：使用者不需要懂内部细节，只要知道「给什么、拿什么」。README 在 Design Outline 一开头就强调了这一点：

> Interfacing to the top level module - `topSystolicArray.sv` has been kept simple.

也就是说，作者刻意把顶层接口做得简洁。本小节我们就来读懂这个简洁的接口表，并理解它的时钟、复位约定。

#### 4.1.2 核心流程

顶层模块对外暴露的端口可以分成三组：

| 分组 | 端口 | 方向 | 位宽（N=4 时） | 含义 |
|------|------|------|----------------|------|
| 时钟/复位 | `i_clk` | 输入 | 1 bit | 时钟，所有寄存器在上升沿更新 |
| 时钟/复位 | `i_arst` | 输入 | 1 bit | 异步复位，高有效 |
| 数据输入 | `i_a` | 输入 | N×N×8 = 128 bit | 输入矩阵 A（每个元素 8 位） |
| 数据输入 | `i_b` | 输入 | N×N×8 = 128 bit | 输入矩阵 B（每个元素 8 位） |
| 控制输入 | `i_validInput` | 输入 | 1 bit | 输入有效脉冲，触发一次乘法 |
| 数据输出 | `o_c` | 输出 | N×N×32 = 512 bit | 结果矩阵 C = A×B（每个元素 32 位） |
| 控制输出 | `o_validResult` | 输出 | 1 bit | 结果有效脉冲，表示可以采样 o_c |

这里有几个对初学者重要的细节：

1. **`i_a`、`i_b` 是「二维打包数组」**。位宽写作 `[N-1:0][N-1:0][7:0]`，SystemVerilog 里打包数组**从右往左读**：最右边 `[7:0]` 是每个元素 8 位，中间 `[N-1:0]` 是一行 N 个元素，最左边 `[N-1:0]` 是 N 行。这正是前两讲说的「8 位输入」在源码里的体现（u1-l1 讲过为何选 8 位）。
2. **`o_c` 每个元素是 32 位**。对应 `[N-1:0][N-1:0][31:0]`。这也是 u1-l1 讲过的「32 位输出，保证 N=16 时累加不溢出」。
3. **没有 ready 信号**。输入端只有 `valid` 没有 `ready`，意味着这是个「单向触发」的握手：使用者拉高 `i_validInput`，模块就开始算，算完用 `o_validResult` 通知，期间不接受中途取消或反压。

#### 4.1.3 源码精读

模块声明与完整端口表：

[rtl/topSystolicArray.sv:3-16](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv#L3-L16) —— 顶层模块 `topSystolicArray`，参数 `N` 默认为 4，后面紧跟全部端口。

逐行对照端口表：

- 第 4 行：`parameter int unsigned N = 4` —— 唯一的可配置参数，决定矩阵规模。行内注释 `/* Modify this */` 提示「想改规模就改这里」（u1-l2 讲过改规模还要同步改测试台里的宏 N）。
- 第 5–6 行：`i_clk` 与 `i_arst`，时钟与异步复位。
- 第 8–9 行：`i_a`、`i_b`，两个 N×N×8 的输入矩阵。
- 第 11 行：`i_validInput`，输入有效脉冲。
- 第 13 行：`o_c`，N×N×32 的结果矩阵。
- 第 15 行：`o_validResult`，结果有效脉冲。

关于复位约定，看任何一段 `always_ff` 即可，例如计数器寄存器：

[rtl/topSystolicArray.sv:42-46](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv#L42-L46) —— `always_ff @(posedge i_clk, posedge i_arst)` 的敏感列表里同时列了时钟上升沿和复位上升沿，且复位分支把寄存器清零，这正是「**异步、高有效复位**」的标准写法。整个顶层里所有寄存器（counter_q、doProcess_q、validResult_q、row_q、col_q）都用同一个 `i_arst` 复位。

文件开头和结尾还有两处编码约定（u3-l2 会专门讲，这里先记住作用）：

[rtl/topSystolicArray.sv:1](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv#L1) —— `` `default_nettype none `` 关闭「隐式线网」，强制所有信号必须显式声明，避免笔误产生意外的隐式连线。

[rtl/topSystolicArray.sv:176](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv#L176) —— 文件末尾的 `` `resetall `` 把编译器指令恢复默认，防止本文件的设置泄漏到后续编译的文件。

> 小结：顶层端口极简——2 个矩阵输入 + 1 个 valid + 1 个时钟 + 1 个复位，输出 1 个矩阵 + 1 个 valid。位宽严格对应 u1-l1 讲的 8 位输入 / 32 位输出取舍。

#### 4.1.4 代码实践

1. **实践目标**：能在源码里迅速定位每个端口，并把端口位宽换算成「几个元素、每位几 bit」。
2. **操作步骤**：
   - 打开 `rtl/topSystolicArray.sv`，定位到第 3–16 行的端口表。
   - 用纸笔写出 N=4 时 `i_a` 的总位宽：`[3:0][3:0][7:0]` = 4×4×8 = 128 bit。
   - 同样算出 `o_c` 的总位宽：4×4×32 = 512 bit。
3. **需要观察的现象**：端口声明顺序是「时钟/复位 → 数据 → 控制 → 输出」，分组清晰；`i_a` 与 `i_b` 位宽完全相同，`o_c` 只是元素位宽换成 32。
4. **预期结果**：N=4 时 `i_a`/`i_b` 各 128 bit，`o_c` 为 512 bit；若把 N 改成 3，`i_a` 变成 3×3×8 = 72 bit，`o_c` 变成 3×3×32 = 288 bit。
5. 运行结果：**待本地验证**（位宽换算可手算确认）。

#### 4.1.5 小练习与答案

**练习 1**：`i_a` 的位宽 `[N-1:0][N-1:0][7:0]` 里，最右边一维 `[7:0]` 代表什么？
**答案**：代表矩阵里「单个元素」的位宽，即 8 bit。整个 `i_a` 是「N 行 × N 列、每元素 8 位」的二维打包数组。SystemVerilog 打包数组从右往左读，最右边是最内层（最接近单个 bit 的那一维）。

**练习 2**：从哪一行能看出 `i_arst` 是「异步、高有效」复位？
**答案**：看任意 `always_ff`，例如第 42 行 `always_ff @(posedge i_clk, posedge i_arst)`——敏感列表里同时有 `posedge i_clk` 和 `posedge i_arst`，说明复位不等时钟（异步）；第 43–44 行 `if (i_arst) ... <= 0`，说明 `i_arst = 1` 时复位（高有效）。

**练习 3**：为什么顶层只有 `valid` 信号而没有 `ready` 信号？这意味着什么？
**答案**：这说明顶层采用「单向触发」握手——使用者用 `i_validInput` 启动一次乘法，模块用 `o_validResult` 通知完成，中间不能反压或取消。好处是接口极简；代价是使用者必须自己保证在计算期间不要发起新的 `i_validInput`。

---

### 4.2 validInput → 变换 → 阵列 → validResult 整体通路

#### 4.2.1 概念说明

上一节我们看清了「外壳上的插孔」，本节来看「插孔之间的内部走线」。顶层模块内部其实做了四件事：

1. **检查参数**：确保 N 在合法范围（2 < N < 257）。
2. **变换矩阵**：把输入矩阵 A、B 重排成驱动阵列所需的「行矩阵」和「列矩阵」。
3. **驱动阵列**：把行/列矩阵喂给 N×N 个 PE 组成的脉动阵列，让它们算乘加。
4. **控制时序**：用一个计数器决定「算多久」，并生成 `o_validResult`。

本节我们只把这四件事**串成一条通路**，看数据怎么流；每件事的内部细节分别对应 u2 的四篇讲义。

#### 4.2.2 核心流程

整个数据通路和控制通路可以画成下面这张图：

```
                 ┌──────────── 数据通路 ────────────┐
                 │                                  │
  i_a ──┐        ▼                                  │
        ├─► [1] 矩阵变换 ──► row_q, col_q ─► [2] 脉动阵列 ──► o_c
  i_b ──┘   (反转/补零/移位)                     (N×N PE)

                 ┌──────────── 控制通路 ────────────┐
                 │                                  │
  i_validInput ─► [3] 控制器 ─► doProcess_q ────────┘ (门控阵列 + 触发变换)
                      │
                      ▼
                 counter_q 计数
                      │
                 计满 3N-2 拍
                      │
                      ▼
                 o_validResult (1 拍脉冲)
```

读法：**数据通路**（上半）从左到右——输入矩阵先进 `[1]` 变换，得到行/列矩阵，再喂给 `[2]` 阵列算出 `o_c`。**控制通路**（下半）由 `i_validInput` 启动——`[3]` 控制器生成 `doProcess_q` 去门控阵列和变换，同时计数器自增；计满 `3N-2` 拍后拉高 `o_validResult`。

控制信号里最关键的是 `doProcess_q`（代码里叫 `doProcess`）。它就像阵列的「运行开关」：为 1 时阵列每拍做乘加、行/列矩阵每拍移位送数；为 0 时阵列保持、不再更新。这个开关在 `i_validInput` 拉高时打开，在算完后关上。

#### 4.2.3 源码精读

**[0] 参数检查**（编译期，u3-l2 详讲）：

[rtl/topSystolicArray.sv:18-29](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv#L18-L29) —— 用 `localparam N_VALID` 把「N>2 且 N<257」缩成一个位（`&{...}` 是对拼接结果的归约与），再用 `if (!N_VALID)` 配 `$error` 在编译期就报错。注意这是 elaboration 期检查，不是运行期——N 非法时综合/仿真根本起不来。README 宣称支持 `2 < N < 17`（要保证 32 位累加不溢出），但硬件检查这里把上限放宽到 257。

**[3] 控制时序：开关 + 计数器 + 完成脉冲**

先看「运行开关」`doProcess`：

[rtl/topSystolicArray.sv:71-89](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv#L71-L89) —— `doProcess_d` 在 `i_validInput` 时置 1，在计数器计到 `MULT_CYCLES+1` 时置 0，其余时刻保持 `doProcess_q`。`doProcess_q` 是它的寄存器输出，正是送给阵列的「运行开关」。

再看计数器与完成脉冲：

[rtl/topSystolicArray.sv:36](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv#L36) —— `MULT_CYCLES = 3*N-2`，一次乘法需要的周期数（N=4 时为 10）。这正是 README 波形里说的「After, 10 (3N-2) clock cycles」。

\[ T_{\text{mult}} = 3N - 2 \quad \xrightarrow{N=4} \quad 10 \text{ 个周期} \]

[rtl/topSystolicArray.sv:40-52](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv#L40-L52) —— 计数器 `counter_q`：`doProcess_d` 为 1 时每拍加 1，否则归零。

[rtl/topSystolicArray.sv:54-67](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv#L54-L67) —— `validResult_q`：当 `counter_q` 计到 `MULT_CYCLES` 时拉高，下一拍又回落，所以 `o_validResult` 是一个**单周期脉冲**。它拉高的那一拍，`o_c` 就是最终结果，使用者应当在这一拍采样 `o_c`。

（计数器的逐拍时序细节——比如 `MULT_CYCLES_W` 的位宽、`+1` 的余量、脉冲为何恰好一拍——留给 u2-l4 精讲。）

**[1] 矩阵变换**（u2-l3 详讲，这里只看它在通路里的位置）：

[rtl/topSystolicArray.sv:91-159](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv#L91-L159) —— 这一大段把 `i_a`/`i_b` 变换成 `row_q`/`col_q`。关键信号有：`invertedRowElements`/`invertedColElements`（元素反转）、`APPEND_ZERO`（高位补零）、以及每拍的移位（送下一个元素进阵列）。本节只要知道「它的输入是 `i_a`/`i_b`，输出是 `row_q`/`col_q`」即可，变换的具体数学留在 u2-l3。

**[2] 阵列实例**（u2-l2 详讲）：

[rtl/topSystolicArray.sv:161-173](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv#L161-L173) —— 顶层实例化子模块 `systolicArray`，把 `.i_row(row_q)`、`.i_col(col_q)` 喂进去，`.i_doProcess(doProcess_q)` 当运行开关，结果从 `.o_c` 直接接到顶层的 `o_c`（用同名端口简写）。

注意端口连接里两种 SystemVerilog 简写：`.i_clk` 等同 `.i_clk(i_clk)`（同名端口省略括号），`.o_c` 等同 `.o_c(o_c)`——所以子模块的 `o_c` 直接驱动顶层输出。

> 小结：顶层 = 参数检查 + 矩阵变换（→row/col_q）+ 阵列实例（doProcess 门控）+ 控制计数器（→validResult）。数据从 `i_a/i_b` 一路流到 `o_c`，控制从 `i_validInput` 一路到 `o_validResult`。

#### 4.2.4 代码实践

1. **实践目标**：把 4.2.2 的数据通路图在源码里「对号入座」，确认每个方框对应哪几行代码。
2. **操作步骤**：
   - 在 `topSystolicArray.sv` 里找到四个方框对应的代码段：`[1] 矩阵变换`（第 91–159 行）、`[2] 阵列实例`（第 161–173 行）、`[3] 控制器`（第 31–89 行，含计数器与 doProcess）、`[0] 参数检查`（第 18–29 行）。
   - 在 4.2.2 的图上每个方框旁边标注它对应的行号区间。
   - 用不同颜色（或不同符号）标出「数据信号」（`i_a/i_b → row_q/col_q → o_c`）和「控制信号」（`i_validInput → doProcess_q → counter_q → o_validResult`）。
3. **需要观察的现象**：数据信号和控制信号在源码里是**交织**在一起的（变换段里既有数据 `row_q`，也有控制条件 `i_validInput`/`counter_q`），这正是「数据流 + 控制流」耦合的典型写法。
4. **预期结果**：得到一张标注了行号、区分了数据/控制信号的完整通路图。
5. 运行结果：**待本地验证**（手绘）。

#### 4.2.5 小练习与答案

**练习 1**：顶层内部做了哪四件事？哪一件是编译期就完成的？
**答案**：① 参数检查 ② 矩阵变换 ③ 驱动阵列 ④ 控制时序。其中①参数检查（`N_VALID` + `$error`）是编译期 / elaboration 期完成的，N 非法时根本无法综合或仿真。

**练习 2**：`doProcess_q` 这个信号在通路里扮演什么角色？它由谁打开、由谁关闭？
**答案**：它是阵列的「运行开关」，门控阵列的乘加和行/列矩阵的移位。由 `i_validInput` 拉高而打开（`doProcess_d = 1`），由计数器计到 `MULT_CYCLES+1` 而关闭（`doProcess_d = 0`）。

**练习 3**：第 161–173 行实例化 `systolicArray` 时，`.o_c` 这种写法是什么意思？
**答案**：这是 SystemVerilog 的「同名端口连接」简写，`.o_c` 等价于 `.o_c(o_c)`，即子模块的 `o_c` 端口直接连到顶层同名信号 `o_c`。同理 `.i_clk`、`.i_arst` 也是简写。

---

### 4.3 接口时序波形对照

#### 4.3.1 概念说明

知道了通路，还要知道「时序」：`i_validInput` 该拉高几拍？算完要等多久？`o_validResult` 高电平持续几拍？这些决定了使用者该怎么驱动这个 IP。本节把 README 里的两张波形图（`interface.png` 和 `inputSequence.png`）对应到源码信号上。

#### 4.3.2 核心流程

一次完整的「输入 → 计算 → 输出」时序如下（N=4 为例，图为示意，目的是建立直觉）：

```
周期:         t0        t1    t2     ......      t10       t11       t12
i_validInput: ─┐1├─────────────── 0 ──────────────────────────────────────
i_a, i_b:     准备好 ─(被采样)────────────────────────────────────────────
doProcess_q:    0  └────────── 1 ──────────────────────┐0├──────────────
counter_q:      0         1     2      ......     10    11         0
o_validResult:  0  ─────────────────────────────────────┐1├──── 0 ──────
o_c:            x        (阵列计算中, 部分和持续变化)        稳定结果    保持
                                                            ↑
                                                     在此拍采样 o_c
```

要点：

1. **`i_validInput` 只需拉高 1 拍**（t0）。这一拍 `i_a`/`i_b` 被采样并变换进 row/col 矩阵，同时启动 `doProcess`。
2. **接下来约 `3N-2` 拍是计算期**。这期间 `doProcess_q` 保持 1，阵列每拍做乘加，`counter_q` 逐拍加 1。`o_c` 这期间是「部分和」，会变化，**不应采样**。
3. **`o_validResult` 拉高 1 拍**（t11）表示完成。这一拍 `o_c` 是最终结果，使用者应当**在这一拍采样** `o_c`。
4. 之后 `doProcess_q` 回 0，阵列停摆，等下一次 `i_validInput`。

> 说明：上图里 t0/t11 这类「第几拍」是示意性的相对位置，目的是帮你建立「输入一拍、算若干拍、输出一拍」的直觉。计数器逐拍的精确取值（为什么 `o_validResult` 恰好是单拍脉冲、为什么要有 `+1` 余量）属于 u2-l4 的内容，本讲不展开。

#### 4.3.3 源码精读

**README 对接口波形的描述**：

[README.md:45-54](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/README.md#L45-L54) —— Design Outline 说明：`i_a`/`i_b` 在 `i_validInput` 拉高时被采样，变换后送入阵列；算完后 `o_validResult` 拉高，可采样 `o_c`；并给出接口波形图 `images/interface.png`。

[README.md:89-95](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/README.md#L89-L95) —— 说明计算期间每拍给阵列送入新值，`10 (3N-2)` 拍后完成，并给出 `images/inputSequence.png`（水平/垂直端口每拍加载新值的波形）。

**源码里对应「完成脉冲」的逻辑**：

[rtl/topSystolicArray.sv:58-64](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv#L58-L64) —— `validResult_q` 只在 `counter_q == MULT_CYCLES` 那一拍为 1，否则为 0，这正是「单拍脉冲」的来源。把 `counter_q` 想成秒表：它数到 `3N-2` 的那一拍，`o_validResult` 闪一下。

[rtl/topSystolicArray.sv:82-87](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv#L82-L87) —— `doProcess_d` 的置 1 / 保持 / 清零逻辑，决定了「计算窗」的长度：从 `i_validInput` 开始，到计数器计满后结束。

> 把这三段连起来：`i_validInput`（第 82 行）打开 `doProcess` → 计数器（第 48–52 行）开始数 → 数到 `MULT_CYCLES`（第 61 行）时点亮 `o_validResult` 一拍 → 使用者在这一拍采样 `o_c`。这就是 README 两张波形图背后的源码逻辑。

#### 4.3.4 代码实践

1. **实践目标**：用真实波形验证 4.3.2 的时序直觉，亲眼看到 `i_validInput` 与 `o_validResult` 这两个脉冲的相对位置。
2. **操作步骤**：
   - 按 u1-l2 的方法 `cd tb && make sim`，生成 `tb/waveform.vcd`。
   - 用 gtkwave 打开（`make waves`），添加信号：`i_clk`、`i_arst`、`i_validInput`、`o_validResult`，以及（在 `topSystolicArray` 实例下的）`counter_q`、`o_c` 的某一两个元素。
   - 找到 `i_validInput` 拉高的那一拍作为起点，数到 `o_validResult` 拉高的那一拍，记录相隔多少个时钟周期。
3. **需要观察的现象**：`i_validInput` 是单拍脉冲；`o_validResult` 也是单拍脉冲；两者之间 `counter_q` 从 0 递增到约 10（N=4）；`o_validResult` 拉高那一拍 `o_c` 不再变化（已是最终结果）。
4. **预期结果**：N=4 时，从 `i_validInput` 拉高到 `o_validResult` 拉高，中间相隔约 `3N-2 = 10` 个时钟周期量级（具体拍数以波形为准，精确推导见 u2-l4）。
5. 运行结果：**待本地验证**（需要本地装有 Verilator；矩阵数值由测试台随机生成）。

#### 4.3.5 小练习与答案

**练习 1**：为什么在 `o_validResult` 还没拉高时，不应采样 `o_c`？
**答案**：因为计算期间 `o_c` 里是「部分和」——PE 的累加器还在不断累加，值会随拍变化，还没算完。只有 `o_validResult` 拉高的那一拍，所有 PE 的累加才结束，`o_c` 才是稳定的最终结果。

**练习 2**：README 说「After, 10 (3N-2) clock cycles the multiplication is complete」，源码里这个 `10` / `3N-2` 体现在哪一行？
**答案**：第 36 行 `localparam int unsigned MULT_CYCLES = 3*N-2;`。N=4 时 `MULT_CYCLES = 10`。它既被计数器用作「计满」阈值，也被 `validResult` 用作「点亮」条件。

**练习 3**：如果使用者连续两拍都拉高 `i_validInput`，从源码看会发生什么？（提示：看第 107–109 行注释与第 119–125 行的优先级。）
**答案**：源码注释（第 107–109 行）指出，`i_validInput` 条件优先级最高（`if/else if` 会被综合工具识别为优先编码）。所以只要 `i_validInput` 为 1，行/列矩阵就被重新装载（第 121 行 `row_d = {APPEND_ZERO, invertedRowElements[i]} << i*8`），相当于每次 `validInput` 都重新开始一轮。连续拉高会反复重置输入，实际使用时应在计算期内（`o_validResult` 拉高前）保持 `i_validInput` 为 0。（精确行为以 u2-l3 / u2-l4 为准。）

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「从端口到通路的完整对照」：

1. **读端口**：打开 `rtl/topSystolicArray.sv`，把第 3–16 行的端口表抄成一张表，标出每个端口的「分组 / 方向 / N=4 时位宽 / 含义」（参考 4.1.2）。
2. **画通路图**：在纸上画出 4.2.2 那张「数据通路 + 控制通路」框图，并在每个方框旁标注对应的源码行号区间：
   - 参数检查：第 18–29 行
   - 控制（计数器 + doProcess + validResult）：第 31–89 行
   - 矩阵变换：第 91–159 行
   - 阵列实例：第 161–173 行
3. **标握手**：在图上用箭头标出 `i_validInput` 如何触发 `doProcess_q`、`counter_q` 如何计到 `MULT_CYCLES`、`o_validResult` 如何点亮一拍。
4. **对波形**（可选，需本地装 Verilator）：`cd tb && make sim` 生成波形，在 gtkwave 里验证 `i_validInput` 与 `o_validResult` 两个脉冲的相对位置，以及 `o_validResult` 拉高时 `o_c` 是否稳定。

**验收标准**：能复述顶层七个端口（`i_clk`/`i_arst`/`i_a`/`i_b`/`i_validInput`/`o_c`/`o_validResult`）的含义与位宽；能画出从 `i_validInput` 到 `o_validResult` 的完整通路并标注行号；能解释为什么要在 `o_validResult` 那一拍采样 `o_c`。

## 6. 本讲小结

- 顶层 `topSystolicArray` 接口极简：输入 `i_a/i_b`（N×N×8）+ `i_validInput`，输出 `o_c`（N×N×32）+ `o_validResult`，外加时钟 `i_clk` 与异步高有效复位 `i_arst`。
- 位宽严格对应 u1-l1 的取舍：输入每元素 8 位（契合 INT8），输出每元素 32 位（保证 N=16 时累加不溢出）。
- 内部由四部分组成：编译期参数检查（N_VALID/`$error`）、矩阵变换（→row_q/col_q）、脉动阵列实例（doProcess 门控）、控制计数器（→o_validResult）。
- 控制握手是「单向触发」：`i_validInput` 单拍脉冲启动，阵列运行约 `3N-2` 拍，`o_validResult` 单拍脉冲通知完成，使用者在该拍采样 `o_c`。
- `3N-2` 这个周期数在源码里就是 `MULT_CYCLES`（第 36 行），既驱动计数器的「计满」阈值，也驱动 `o_validResult` 的「点亮」条件。
- 接口时序可对照 README 的 `interface.png` 与 `inputSequence.png` 两张波形图；波形背后的逻辑全部能在第 31–89 行的控制段找到。

## 7. 下一步学习建议

本讲只看了「外壳和走线」，内部每个方框还是黑盒。接下来进入进阶层 u2，自底向上逐个打开黑盒：

1. **u2-l1 处理单元 PE 的乘加运算**：打开 `rtl/pe.sv`，看清 8×8 乘法 + 32 位累加的 MAC 通路，以及 `o_a`/`o_b` 如何向邻居 PE 透传数据。
2. **u2-l2 阵列互联与 generate 生成**：打开 `rtl/systolicArray.sv`，看嵌套 generate 如何把 N×N 个 PE 连成二维阵列。
3. **u2-l3 行/列矩阵变换**：回到 `topSystolicArray.sv` 的第 91–159 行，看清元素反转、补零、移位的数学。
4. **u2-l4 控制计数器与时钟门控**：回到第 31–89 行，逐拍推导计数器与 `o_validResult` 脉冲的精确时序。

> 建议顺序：先 u2-l1（最小单元 PE）→ u2-l2（PE 连成阵列）→ u2-l3（数据怎么喂进去）→ u2-l4（整体跑多久）。每读完一篇，回到本讲的通路图，把对应的黑盒「点亮」——这是把整个设计读薄的最有效方式。
