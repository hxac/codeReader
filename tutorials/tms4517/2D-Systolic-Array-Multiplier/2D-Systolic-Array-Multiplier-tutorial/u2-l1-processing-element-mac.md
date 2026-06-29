# 处理单元 PE 的乘加运算

## 1. 本讲目标

本讲深入到脉动阵列的「最小细胞」——处理单元 PE（Processing Element）。

读完本讲，你应当能够：

- 说清楚一个 PE 内部的 MAC（Multiply-Accumulate，乘加）数据通路：`mult`、`mac_q`/`mac_d`、`o_y` 各自是什么、怎么连起来。
- 解释 `i_doProcess` 这个控制信号如何充当 PE 的「开关」：它不仅决定要不要累加，还决定输入数据要不要继续向相邻 PE 流动。
- 理解 `o_a`/`o_b` 这两个寄存器输出在二维阵列里的「接力棒」作用，以及由此产生的逐拍延迟如何成为脉动算法正确性的关键。
- 把 PE 放回 `systolicArray.sv` 的 `generate` 互联中，看懂一个 PE 如何与左、上邻居以及输出端口连接。

本讲是 u1-l3（顶层接口）的「下钻」：顶层里那个被 `doProcess_q` 门控的 `systolicArray` 实例，里面装的就是成百上千个本讲所讲的 PE。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

### 2.1 什么是 MAC

矩阵乘法 \(C = A \times B\) 的每个元素都是一组「乘法再相加」：

\[
C_{ij} = \sum_{k=0}^{N-1} A_{ik}\cdot B_{kj}
\]

硬件上把「乘一次、加一次」打包成一个原子操作，叫 **MAC（Multiply-Accumulate）**。一个累加器寄存器保存中间和，每个时钟周期让它加上一个新的乘积，\(N\) 拍后正好算出一个 \(C_{ij}\)。脉动阵列的每个 PE 就是一个 MAC 单元。

### 2.2 为什么要寄存器「接力」

如果所有 PE 同时读到同一份数据，就退化成普通的并行乘法器，布线会很长。脉动架构的精髓是：**数据只在相邻 PE 之间流动**。一个 PE 算完后，把数据「传给」右边的 PE 和下面的 PE，让数据在阵列里像波浪一样推进。为了每拍只走一格，PE 会把输入数据存进寄存器，下一拍再输出给邻居——这就是 `o_a`/`o_b` 的作用。

### 2.3 为什么需要一个 doProcess 开关

PE 不能一直累加：算完一个 \(C_{ij}\) 之后必须清零，才能开始算下一个。而且数据流入阵列的时机是精心错开的（见 u1-l1 的「skew 错峰」）。所以顶层给每个 PE 共享一个 `i_doProcess` 信号：为高时「正常工作」，为低时「清零累加器、冻结数据流动」。理解这个开关，是理解整个阵列时序的钥匙。

> 约定：本讲用「拍」「周期」指代一个时钟上升沿到下一个上升沿之间的过程；`_d` 后缀表示组合逻辑算出的「下一拍要打的值」（next-state），`_q` 后缀表示「当前寄存器里已存的值」（present-state）。这是整个项目的命名风格，务必记住。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的地方 |
|------|------|----------------|
| [rtl/pe.sv](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/pe.sv) | 定义单个处理单元 PE，实现 8 位乘、32 位累加，并把输入寄存后透传给邻居。 | 全文精读，这是本讲主角 |
| [rtl/systolicArray.sv](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/systolicArray.sv) | 用 `generate` 把 \(N \times N\) 个 PE 连成二维阵列。 | 看 PE 的端口如何接到邻居与输出 |
| [rtl/topSystolicArray.sv](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv) | 顶层；生成 `doProcess` 门控信号并实例化阵列。 | 仅引用 `doProcess_q` 来源，承接 u1-l3 |

> 提示：`rtl/README.md` 在项目主 README 中被提到，但仓库里并不存在该文件，因此本讲不引用它，所有结论均来自上面三个 `.sv` 源码本身。

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：

1. **MAC 乘加数据通路**：`mult` → `mac_q`/`mac_d` → `o_y`。
2. **输入寄存与 `o_a`/`o_b` 透传**：数据如何向右、向下接力。
3. **`i_doProcess` 门控与异步复位**：开关如何同时控制「累加/清零」与「流动/冻结」。

### 4.1 MAC 乘加数据通路

#### 4.1.1 概念说明

MAC 通路要解决的问题是：在一个时钟周期内完成「读两个 8 位数 → 相乘 → 加到累加器 → 输出累加结果」，并且这套结构可以被复用 \(N\) 次来累加出一个 \(C_{ij}\)。

它由三部分组成：

- **乘法器**（组合）：算出当前拍的两个输入之积。
- **累加寄存器**（时序）：保存「到目前为止的累加和」，每拍更新一次。
- **输出**（组合）：把累加寄存器的值直接送出去。

#### 4.1.2 核心流程

一个 PE 的 MAC 在一个周期内的执行流程：

```text
         i_a (8位) ─┐
                    ├──[ 乘法器 mult = i_a*i_b ]──┐
         i_b (8位) ─┘                              │
                                                   ▼
   mac_q(当前累加和) ──[ + ]──► mac_d(下一拍的累加和)
                                   │
                          （在时钟上升沿打入 mac_q）
                                   │
   o_y ────────────────────── ◄── mac_q（直接输出当前累加和）
```

用伪代码描述时序逻辑：

```text
每个时钟上升沿：
    若 i_arst == 1：     mac_q <= 0          // 异步复位清零
    否则：               mac_q <= mac_d      // 打入下一拍值

mac_d（组合）：
    若 i_doProcess == 1：mac_d = mac_q + mult   // 累加
    否则：               mac_d = 0              // 准备清零
```

注意一个微妙但关键的细节：`mac_d` 是「下一拍将要写入 `mac_q` 的值」。当 `i_doProcess` 为低时，`mac_d` 被强制设为 0，于是**下一个上升沿** `mac_q` 就会被清零。也就是说「清零」要等到下一拍才生效，而不是当拍立即清零。这一点在第 4.1.4 的实践里会亲眼验证。

位宽方面，`mult` 声明为 32 位（见下方源码），而 \(8 \times 8\) 的乘积最多只有 16 位，所以乘法永远不会溢出；`mac_q` 也是 32 位，对于本项目支持的最大规模 \(N=16\)，累加上界为：

\[
16 \times (255 \times 255) = 16 \times 65025 = 1\,040\,400 \approx 2^{20}
\]

20 位即可装下，32 位绰绰有余（这与 u1-l1 讲到的「输出 32 位是为验证方便、理论上 20 位够」完全一致）。

#### 4.1.3 源码精读

先看 PE 的端口定义。PE 对外暴露的信号非常精简：

[rtl/pe.sv:6-18](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/pe.sv#L6-L18) 定义了 PE 的全部端口：两个 8 位输入 `i_a`/`i_b`，两个 8 位透传输出 `o_a`/`o_b`，一个 32 位累加结果输出 `o_y`，以及时钟 `i_clk`、异步复位 `i_arst` 和门控信号 `i_doProcess`。

MAC 部分从乘法器开始：

[rtl/pe.sv:22-25](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/pe.sv#L22-L25) 声明 32 位 `mult` 并用 `always_comb` 在组合逻辑里算出 `mult = i_a*i_b`：

```systemverilog
logic [31:0] mult;

always_comb
  mult = i_a*i_b;
```

接着是累加寄存器，典型的「`_d`/`_q`」一对：

[rtl/pe.sv:27-33](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/pe.sv#L27-L33) 声明累加器的「下一拍值」`mac_d` 与「当前值」`mac_q`，并用 `always_ff` 在 `posedge i_clk` 或 `posededge i_arst` 时更新——异步复位时清零，否则打入 `mac_d`：

```systemverilog
logic [31:0] mac_d, mac_q;

always_ff @(posedge i_clk, posedge i_arst)
  if (i_arst)
    mac_q <= '0;
  else
    mac_q <= mac_d;
```

> 注意 `always_ff @(posedge i_clk, posedge i_arst)` 把 `i_arst` 写进敏感列表，这正是「异步、高有效复位」的写法：复位一来立刻生效，不必等时钟沿。这与 u1-l3 介绍的顶层约定一致。

下一拍值的计算逻辑在这里——本讲最关键的一行：

[rtl/pe.sv:35-36](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/pe.sv#L35-L36) 用 `always_comb` 决定 `mac_d`：`i_doProcess` 为高时累加 `mac_q + mult`，为低时直接给 `0`：

```systemverilog
always_comb
  mac_d = (i_doProcess) ? mac_q + mult : '0;
```

最后把累加结果送出端口：

[rtl/pe.sv:38-39](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/pe.sv#L38-L39) 用 `always_comb` 让输出 `o_y` 持续等于当前累加值 `mac_q`：

```systemverilog
always_comb
  o_y = mac_q;
```

到这里 MAC 通路就闭合了：`o_y` 永远反映 `mac_q`，而 `mac_q` 每拍根据 `mac_d` 更新。

#### 4.1.4 代码实践

这是一个「纸上追踪型」实践，不需要运行任何命令，但能帮你彻底搞懂累加时序。

**实践目标**：亲手验证 `mac_q` 在 `i_doProcess` 拉高期间如何累加，以及它为低时如何被清零。

**操作步骤**：假设复位已撤销（`i_arst=0`），某个 PE 连续三拍收到如下输入，且 `i_doProcess` 这三拍都为 1：

| 拍号（上升沿后看到的输入） | `i_a` | `i_b` | `mult = i_a*i_b` |
|---|---|---|---|
| 0 | 10 | 5  | 50  |
| 1 | 20 | 2  | 40  |
| 2 | 30 | 4  | 120 |

请逐拍填写 `mac_d` 与「下一拍 `mac_q`」。

**预期结果**（初始 `mac_q = 0`）：

- 拍 0：`mac_d = 0 + 50 = 50` → 上升沿后 `mac_q = 50`。
- 拍 1：`mac_d = 50 + 40 = 90` → 上升沿后 `mac_q = 90`。
- 拍 2：`mac_d = 90 + 120 = 210` → 上升沿后 `mac_q = 210`。

最终 `o_y = 210`，正好等于 \(10\cdot5 + 20\cdot2 + 30\cdot4\)。

**接着把第 3 拍的 `i_doProcess` 拉低**（输入随意）：

- 拍 3：因为 `i_doProcess=0`，`mac_d = 0` → 上升沿后 `mac_q = 0`。

**需要观察的现象**：清零发生在 `i_doProcess` 拉低之后的**那个上升沿**，而不是拉低的当拍——因为当拍 `o_y` 仍等于旧的 `mac_q`（210），直到下一个沿才变成 0。这正是「`mac_d` 是下一拍值」的体现。

> 说明：以上数值为示例代码，便于你按 [pe.sv:35-36](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/pe.sv#L35-L36) 的逻辑手算；若要在波形里确认，需要把 PE 内部信号暴露给 Verilator（当前测试台未做，见第 5 节综合实践），结果「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `mac` 的累加宽度从 32 位改成 16 位，对 \(N=16\)、输入元素全为 255 的极端情况会出现什么问题？

> **答案**：最大累加值为 \(1\,040\,400\)，需要 20 位才能表示（\(2^{20}=1\,048\,576\)）。16 位最大只能表示 \(65\,535\)，会**溢出**，导致 `o_y` 给出错误（截断）的结果。这就是输出位宽选 32 位的安全余量来源。

**练习 2**：`mult` 为什么用 `always_comb` 而不是 `always_ff`？

> **答案**：乘法是纯组合运算，没有需要记忆的状态。用 `always_comb` 让 `mult` 在 `i_a`/`i_b` 一变化就立刻更新，不占用额外的时钟周期；只有需要「记忆」的累加和才用 `always_ff` 存进 `mac_q`。

**练习 3**：当 `i_doProcess` 恒为 0 且持续多个周期，`mac_q` 最终稳定在什么值？为什么不是「当 `i_doProcess` 一变 0，`mac_q` 当拍就变 0」？

> **答案**：稳在 0。因为 `i_doProcess=0` 使 `mac_d=0`，第一个上升沿后 `mac_q` 变 0，之后 `mac_d` 仍是 0，故维持 0。它不会「当拍」清零，是因为 `mac_q` 是寄存器，必须等到时钟上升沿才会采样 `mac_d` 的值——这是时序逻辑的本质。

### 4.2 输入寄存与 o_a/o_b 透传

#### 4.2.1 概念说明

PE 不只要算，还要把数据「传出去」。一个 PE 收到的 `i_a`（来自左邻居或行矩阵）算完后，要原样送给右邻居的 `i_a`；收到的 `i_b`（来自上邻居或列矩阵）要原样送给下邻居的 `i_b`。这样数据才能在阵列里流动。

为什么不直接把 `i_a` 用一根线连到右邻居，而要经过寄存器？因为**寄存会带来一拍延迟**，而这一拍延迟正是脉动算法需要的「错峰」：同一行里，越靠右的 PE 越晚一拍收到数据，从而保证正确的两个操作数在正确的那一拍相遇（见 u2-l3 的行/列变换）。换句话说，`o_a`/`o_b` 既是「数据通路」，也是「时序对齐器」。

#### 4.2.2 核心流程

两个 8 位输入各有一个寄存器 `a_q`/`b_q`，逻辑完全对称：

```text
每个时钟上升沿：
    若 i_arst == 1：        a_q <= 0
    否则若 i_doProcess == 1：a_q <= i_a      // 采样并更新
    否则：                  a_q <= a_q       // 保持（冻结流动）

o_a = a_q    （b_q / o_b 同理）
```

要点：

- **采样受 `i_doProcess` 门控**：只有开关为高时，`i_a` 才会被「吃进」`a_q`，并在下一拍出现在 `o_a` 上。开关为低时 `a_q` 保持不变，数据流动被冻结。
- **`o_a`/`o_b` 是组合输出**：它们直接等于寄存器值，没有额外延迟。

#### 4.2.3 源码精读

寄存输入这段代码位于文件后半部分：

[rtl/pe.sv:45-67](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/pe.sv#L45-L67) 声明 `a_q`/`b_q` 两个 8 位寄存器，各自用 `always_ff` 在 `i_arst`/`i_doProcess` 控制下采样或保持，最后用 `always_comb` 把它们赋给 `o_a`/`o_b`。摘录 `a_q` 部分：

```systemverilog
logic [7:0] a_q, b_q;

always_ff @(posedge i_clk, posedge i_arst)
  if (i_arst)
    a_q <= '0;
  else if (i_doProcess)
    a_q <= i_a;
  else
    a_q <= a_q;
```

`b_q` 的写法与之完全镜像（`i_b` → `b_q` → `o_b`）。

现在把 PE 放回阵列里，看 `o_a`/`o_b` 接到了哪里。[rtl/systolicArray.sv:56-74](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/systolicArray.sv#L56-L74) 用两层 `generate` 实例化每个 PE，端口连接清楚地展现了「向右、向下」的流动方向：

```systemverilog
pe u_pe
( .i_clk
, .i_arst
, .i_doProcess
, .i_a (rowInterConnect[i][j])      // 来自左侧（首列来自行矩阵）
, .i_b (colInterConnect[i][j])      // 来自上方（首行来自列矩阵）
, .o_a (rowInterConnect[i][j+1])    // 送给右侧邻居
, .o_b (colInterConnect[i+1][j])    // 送给下方邻居
, .o_y (o_c[i][j]) );               // 本 PE 的累加结果
```

- `i_a` 取自 `rowInterConnect[i][j]`，`o_a` 写入 `rowInterConnect[i][j+1]` —— 同一行内向右传一格。
- `i_b` 取自 `colInterConnect[i][j]`，`o_b` 写入 `colInterConnect[i+1][j]` —— 同一列内向下传一格。

首列与首行的「源头」由两段 dummy 互联提供：

[rtl/systolicArray.sv:42-54](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/systolicArray.sv#L42-L54) 把行矩阵 `i_row[i][0]` 接到首列 PE 的水平输入 `rowInterConnect[i][0]`，把列矩阵 `i_col[i][0]` 接到首行 PE 的垂直输入 `colInterConnect[0][i]`：

```systemverilog
always_comb
  rowInterConnect[i][0] = i_row[i][0];   // 行矩阵 → 首列 i_a
...
always_comb
  colInterConnect[0][i] = i_col[i][0];   // 列矩阵 → 首行 i_b
```

而互联网本身的声明在更上方，注意它们的维度比阵列多一格，正是为了容纳首列/首行的「源头」和末列/末行的「溢出」：

[rtl/systolicArray.sv:35-39](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/systolicArray.sv#L35-L39) 声明水平互联 `rowInterConnect [N-1:0][N:0][7:0]`（行数 = N，列数 = N+1）与垂直互联 `colInterConnect [N:0][N-1:0][7:0]`（行数 = N+1，列数 = N）。

> 一句话总结这一节：`o_a`/`o_b` 把每个 PE 变成数据通路上的「一拍中继站」，阵列靠它实现逐拍流动与错峰对齐。

#### 4.2.4 代码实践

**实践目标**：理解「每经过一个 PE，数据延迟一拍」这一性质。

**操作步骤**（源码阅读型）：参照 [systolicArray.sv:6-15](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/systolicArray.sv#L6-L15) 的阵列示意图，画一个 \(4\times4\) 的 PE 网格。假设某拍 `i_row[0][0]` 上的值是 `0xAB`。

- 它先进入 `PE[0][0]` 的 `i_a`。
- 下一拍被 `PE[0][0]` 的 `a_q` 采样，出现在 `o_a` = `rowInterConnect[0][1]`，也就是 `PE[0][1]` 的 `i_a`。
- 再下一拍进入 `PE[0][1]` 的 `a_q`，出现在 `PE[0][2]` 的 `i_a`……

**需要观察的现象**：`0xAB` 在水平方向上每过一格就晚一拍。在图上把 `0xAB` 标注在第 0、1、2、3 拍分别到达 `PE[0][0]`、`PE[0][1]`、`PE[0][2]`、`PE[0][3]` 的位置，你会看到一条向右下推进的「对角线」轨迹。

**预期结果**：这正是脉动阵列的标志性波形——数据沿对角线推进。把它与垂直方向的 `i_b` 流动叠加，就能理解为什么两个正确的操作数会在某个特定 PE 的某一拍恰好相遇并相乘。

#### 4.2.5 小练习与答案

**练习 1**：`a_q <= a_q;`（else 分支）这一行有什么作用？删掉会怎样？

> **答案**：它表示「保持原值」。如果不写，综合工具通常会推断出锁存器或在某些风格下行为未定义；显式写出保持赋值，能让综合器明确推断出带使能的寄存器（`i_doProcess` 即写使能），意图更清晰。

**练习 2**：`o_a` 用 `always_comb` 赋值，意味着 `o_a` 与 `a_q` 之间没有额外延迟。那么数据从 `i_a` 到达下一级 PE 的 `i_a`，总共延迟几拍？

> **答案**：延迟 1 拍。`i_a` 在某个上升沿被打入 `a_q`，`o_a`（=`a_q`）在该沿之后立即更新为 `i_a` 的旧值，于是下一级 PE 在**下一个**上升沿才采样到它。所以每跨过一个 PE 就晚一拍。

**练习 3**：`o_a`/`o_b` 永远等于当前寄存器值，即使 `i_doProcess=0` 时它们也保持上次的值。这对阵列意味着什么？

> **答案**：意味着当 `i_doProcess` 为低时，整张阵列的数据流动被「冻结」——所有 `a_q`/`b_q` 保持不变，数据停在原地。这与 4.3 节讲的「`i_doProcess` 既是累加开关也是流动开关」相呼应。

### 4.3 i_doProcess 门控与异步复位

#### 4.3.1 概念说明

`i_doProcess` 是 PE 唯一的控制输入，却同时控制两件事：

1. **累加还是清零**（见 4.1，控制 `mac_d`）。
2. **流动还是冻结**（见 4.2，控制 `a_q`/`b_q` 是否更新）。

把这两件事用同一个信号控制，是一个非常聪明的设计：当一次矩阵乘法结束时，顶层把 `i_doProcess` 拉低，于是**所有 PE 同时清零累加器、同时停止流动**——既准备好了下一轮的干净初值，又避免了残余数据继续乱跑。

异步复位 `i_arst` 则是更高优先级的「总清零」：无论 `i_doProcess` 是什么，复位一来所有寄存器立刻变 0。

#### 4.3.2 核心流程

把三个寄存器的复位与门控行为并排放在一起对比：

| 信号 | 复位 `i_arst=1` | `i_doProcess=1` | `i_doProcess=0` |
|------|-----------------|-----------------|-----------------|
| `mac_q`（累加器） | `<= 0`（异步） | `<= mac_q + mult` | `<= 0`（下一拍清零） |
| `a_q`（行数据寄存） | `<= 0`（异步） | `<= i_a`（采样） | `<= a_q`（保持） |
| `b_q`（列数据寄存） | `<= 0`（异步） | `<= i_b`（采样） | `<= b_q`（保持） |

优先级链：`i_arst` 最高（写在 `always_ff` 的 `if` 最外层），其次是 `i_doProcess`，最后是默认保持。

`i_doProcess` 从哪来？它不是 PE 自己产生的，而是顶层 `topSystolicArray` 用一个状态机生成的、**全阵列共享**的信号 `doProcess_q`：

[rtl/topSystolicArray.sv:161-173](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv#L161-L173) 实例化 `systolicArray` 时，把顶层的 `doProcess_q` 连到阵列的 `i_doProcess`，于是阵列里每个 PE 共享同一个开关：

```systemverilog
systolicArray #(.N (N)) u_systolicArray
( .i_clk
, .i_arst
, .i_doProcess (doProcess_q)   // ← 全阵列共享
, .i_row (row_q)
, .i_col (col_q)
, .o_c );
```

而 `doProcess_q` 的生成逻辑（u1-l3 已介绍、u2-l4 会详讲）在这里：

[rtl/topSystolicArray.sv:73-89](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv#L73-L89) 用 `i_validInput` 置位、用计数器到达 `MULT_CYCLES+1` 时清零，中间保持——即一次乘法期间持续为高、结束后拉低：

```systemverilog
always_comb
  if (i_validInput)                              doProcess_d = 1;
  else if (counter_q == MULT_CYCLES_W'(MULT_CYCLES+1)) doProcess_d = 0;
  else                                           doProcess_d = doProcess_q;
```

其中一次乘法的总周期数由 [topSystolicArray.sv:36](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv#L36) 定义为 `MULT_CYCLES = 3*N-2`（\(N=4\) 时为 10）。也就是说：`doProcess_q` 会连续高 \(3N-2\) 拍左右，让每个 PE 在这期间持续累加与流动，期满后统一拉低、清零、准备下一轮。

#### 4.3.3 源码精读

门控逻辑其实已经在前两节出现过，这里把「同一信号、两处用法」并列点出来，便于体会设计的统一性：

- 在 MAC 中，[pe.sv:35-36](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/pe.sv#L35-L36) 用 `i_doProcess` 选择「累加」或「清零」。
- 在输入寄存中，[pe.sv:47-61](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/pe.sv#L47-L61) 用同一个 `i_doProcess` 作为寄存器的写使能。

而异步复位在三个寄存器里写法一致，都是 `always_ff @(posedge i_clk, posedge i_arst)` 配 `if (i_arst) <= '0;`，见 [pe.sv:29-33](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/pe.sv#L29-L33)（`mac_q`）、[pe.sv:47-53](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/pe.sv#L47-L53)（`a_q`）、[pe.sv:55-61](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/pe.sv#L55-L61)（`b_q`）。

文件最外层还有两个看似无关、实则重要的编译指令：

[rtl/pe.sv:4](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/pe.sv#L4) 的 `` `default_nettype none `` 要求所有线网必须显式声明类型，避免拼写错误的信号名被静默推断成隐式线网；[rtl/pe.sv:73](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/pe.sv#L73) 的 `` `resetall `` 在文件末尾恢复默认设置，防止这些指令「泄漏」到后续编译的文件里。这是工程级的健壮写法，值得在自己的项目里照搬。

#### 4.3.4 代码实践

**实践目标**：体会「`i_doProcess` 一拉低，全阵列同时清零并冻结」这一全局效果。

**操作步骤**（源码追踪型）：

1. 打开 [topSystolicArray.sv:73-89](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/topSystolicArray.sv#L73-L89)，找到 `doProcess_d` 被赋为 0 的条件：`counter_q == MULT_CYCLES+1`。
2. 对 \(N=4\)，算出 `MULT_CYCLES = 3*4-2 = 10`，所以 `doProcess_d` 在 `counter_q == 11` 时变 0。
3. 追踪这个 0 从 `doProcess_d` →（上升沿）→ `doProcess_q` →（端口）→ 阵列的 `i_doProcess` →（每个 PE 的）`mac_d=0` 与 `a_q/b_q` 保持。

**需要观察的现象**：从 `counter_q` 计满，到所有 PE 的 `mac_q` 实际变 0，中间要经过：组合逻辑算出 `doProcess_d=0` → 一个上升沿更新 `doProcess_q` → 信号传到 PE → PE 内 `mac_d` 变 0 → 再一个上升沿 `mac_q` 才变 0。即 **`mac_q` 的清零比 `counter_q` 计满晚约两拍**。

**预期结果**：在纸上面画出 `counter_q`、`doProcess_q`、某个 PE 的 `mac_d`、`mac_q` 四条曲线的时间关系，能看到这条「计满 → 拉低 → 清零」的因果链。这是 u2-l4（控制计数器）的预演，届时会结合完整时序图再讲一遍。

> 说明：具体节拍数「待本地验证」，取决于仿真器对组合路径的求值顺序，但「清零比计满晚」这一因果关系是确定的。

#### 4.3.5 小练习与答案

**练习 1**：为什么用 `i_doProcess` 同时控制「累加」和「流动」，而不是分成两个独立信号？

> **答案**：因为两者必须**同步**。如果累加停了但数据还在流，残余数据会继续进入下游 PE 产生无意义的乘积；如果数据停了但累加还在加，PE 会把上一拍的 `mult` 重复相加。用同一个信号保证「算和流」同开同关，时序自然对齐，控制也更简单。

**练习 2**：`i_arst` 与 `i_doProcess` 谁优先级更高？从源码哪里能看出来？

> **答案**：`i_arst` 更高。在三个 `always_ff` 里，`if (i_arst)` 都写在最外层，`i_doProcess` 的判断嵌套在 `else` 里。因此复位期间无论 `i_doProcess` 是什么，寄存器都被强制清零。

**练习 3**：假设 `i_doProcess` 一直是 1、从不拉低，会发生什么？

> **答案**：`mac_q` 会**永不停止地累加**，把后续轮次的乘积也加进来，导致 `o_y` 越来越大、完全错误。这说明 `i_doProcess` 必须在每次乘法结束后拉低一次来清零——这正是顶层控制器的职责（见 u2-l4）。

## 5. 综合实践

把三个最小模块串起来，做一个能把 PE 内部状态「看见」的小改造。

**实践目标**：通过 Verilator 波形，亲眼观察单个 PE 的 `mult`、`mac_q`、`a_q`、`b_q` 在一次完整乘法中的变化，验证前几节的所有结论。

**背景**：当前 [tb/tb_topSystolicArray.cpp](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/tb_topSystolicArray.cpp) 只通过顶层端口（`o_c`、`o_validResult`）观察结果，PE 内部信号默认不会出现在 `waveform.vcd` 里。

**操作步骤**（源码阅读 + 小改造型，需修改 RTL 一行注释以暴露信号——这是观察型改动，不改变功能）：

1. 阅读 [tb_topSystolicArray.cpp:188-216](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/tb_topSystolicArray.cpp#L188-L216)，确认波形 dump 用的是 `dut->trace(m_trace, 5)`，深度为 5 层层次。
2. 在 [pe.sv](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/rtl/pe.sv) 里给想观察的内部信号加上 `/* verilator public */` 注释，例如把 `logic [31:0] mac_d, mac_q;` 改为 `logic [31:0] mac_d /* verilator public */, mac_q /* verilator public */;`，使 Verilator 把它们暴露成 C++ 可见成员。
3. 重新 `cd tb && make clean && make all`，再用 gtkwave 打开 `waveform.vcd`，找到 `topSystolicArray.u_systolicArray.u_pe[...].mac_q` 等信号。

**需要观察的现象**：

- 在 `i_doProcess` 为高的若干拍里，`mac_q` 每拍递增一个 `mult` 的量。
- `a_q`/`b_q` 每拍更新为新的 `i_a`/`i_b`。
- `i_doProcess` 拉低后的那一拍，`mac_q` 归零。
- 同一行相邻 PE 的 `a_q` 波形呈现「一模一样、错开一拍」的形状。

**预期结果**：波形与第 4.1.4、4.2.4 的手算/手画结论一致。

> 重要：本步骤需要修改 RTL 源码，仅用于本地学习观察，**不要把该改动提交回仓库**（本讲义的 worker 规则也禁止改源码；这是给你的学习建议）。若不想改源码，可退而求其次：在波形里只看顶层的 `o_c[i][j]`，在 `o_validResult` 拉高那一拍对照 [tb_topSystolicArray.cpp:144-154](https://github.com/tms4517/2D-Systolic-Array-Multiplier/blob/59378e8caacdc23086ac09e1c04bc19500d70cfb/tb/tb_topSystolicArray.cpp#L144-L154) 的 `calculateResultMatrix` 手算期望值，验证最终累加结果正确。运行结果「待本地验证」。

## 6. 本讲小结

- PE 是脉动阵列的最小细胞，核心是一条 **MAC 通路**：组合乘法 `mult` → 下一拍值 `mac_d` → 累加寄存器 `mac_q` → 输出 `o_y`。
- `mac_d = i_doProcess ? mac_q + mult : 0`：`i_doProcess` 为高就累加、为低就让累加器在**下一拍**清零。
- 输入数据经 `a_q`/`b_q` 寄存后从 `o_a`/`o_b` 透传给右邻居和下邻居，每跨一个 PE 延迟一拍，这正是脉动错峰的来源。
- `i_doProcess` 是「一信号两用」：既控累加/清零，又作输入寄存器的写使能（流动/冻结），两者天然同步。
- `i_arst` 是最高优先级的异步复位，三个寄存器写法一致；文件首尾的 `` `default_nettype none `` 与 `` `resetall `` 是工程级健壮写法。
- 整个阵列共享顶层生成的 `doProcess_q`（一次乘法持续约 \(3N-2\) 拍），期满统一拉低，让全部 PE 同时清零、冻结，准备下一轮。

## 7. 下一步学习建议

- **下一讲 u2-l2（阵列互联与 generate 生成）**：本讲只看了单个 PE 如何接邻居，下一讲会完整拆解 `systolicArray.sv` 的两层 `generate`、`rowInterConnect`/`colInterConnect` 的维度设计，以及首行首列 dummy 互联如何把行/列矩阵接进阵列。
- **横向 u2-l3（行/列矩阵变换）**：想知道为什么「每过一格晚一拍」恰好能让正确的操作数相遇，需要回到顶层的 `invertedRowElements`、补零、移位逻辑。
- **纵向 u2-l4（控制计数器与时钟门控）**：本讲反复提到的 `doProcess_q`、`MULT_CYCLES = 3N-2`、清零比计满晚两拍，都将在那里给出完整时序图。
- **延伸阅读**：可对照 README 提到的经典论文 *"Why Systolic Architectures?"* 与 Google TPU 资料体会 MAC 单元在工业级实现中的形态。
