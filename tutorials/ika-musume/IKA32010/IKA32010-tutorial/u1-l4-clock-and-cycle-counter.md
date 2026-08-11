# 时钟分频与周期计数器

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 **为什么 IKA32010 需要 4 个 `i_EMUCLK` 周期才等于 1 个 DSP 机器周期**，并把这件事和 TMS32010 原始芯片的「CLKIN 四分频得到 CLKOUT」对应起来。
- 读懂 [`cyclecntr`](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L49-L53) 这个 2 位计数器是如何自增、回绕、复位的。
- 弄清 `cyc_pcen` / `cyc_ncen` / `o_CLKOUT` 三个时钟相关信号的时序关系：谁在哪个相位（phase）拉高、它们和 `o_CLKOUT` 的上升/下降沿如何对齐。
- 理解 `i_CLKIN_PCEN` 这个「时钟使能」在多时钟域系统里起什么作用，以及它在 testbench 里被驱动成什么样子。

本讲是讲义 u1-l3（端口定义）的直接续篇。u1-l3 告诉我们端口叫什么、极性是什么；本讲回答**这些时钟信号内部到底怎么动**。

## 2. 前置知识

在进入源码前，先用通俗语言把几个底层概念讲清楚。

### 2.1 时钟、周期与相位

- **时钟（clock）**：一种在 0/1 之间周期性跳变的方波信号，驱动芯片里所有寄存器「按节拍」更新。
- **周期（cycle）**：时钟信号重复一次所需的时间。如果用 \(T\) 表示周期，用 \(f\) 表示频率，则 \(f = 1/T\)。
- **相位 / 节拍（phase）**：把一个完整周期再切成若干等份，每一份叫一个相位。原始 TMS32010 把一个机器周期切成 4 个相位（可以理解为一小节里的 4 拍）。

### 2.2 时钟使能（clock enable）

「时钟使能」是一种**不让每个时钟沿都生效**的机制。形象地说：时钟一直在敲鼓，但鼓手只在「使能信号为 1」的那几拍才真正击鼓；使能为 0 的拍子，大家原地不动。这样可以用一个快时钟，模拟出一个慢时钟的节拍，而不必真的再生成一条慢时钟线。这是 FPGA 设计中非常常见的做法，IKA32010 也大量采用。

### 2.3 时序逻辑与组合逻辑（Verilog 视角）

- **时序逻辑**：`always @(posedge clk)` 描述的块，里面的变量（`reg`）只在时钟上升沿更新，更新后的值要到下一个沿才「稳定可见」。
- **组合逻辑**：`assign` 或 `always @(*)` 描述的电路，输出随输入**立刻**变化，不等待时钟。

本讲里 `cyclecntr` 属于时序逻辑（节拍计数器），而 `o_CLKOUT` / `o_CLKOUT_PCEN` / `o_CLKOUT_NCEN` 属于组合逻辑（由 `cyclecntr` 当场译出）。

### 2.4 四分频的直觉

原始 TMS32010 芯片对外输入一个高频时钟 CLKIN，内部把它四分频，得到一个较慢的 CLKOUT 提供给外部电路。也就是说：

\[ f_{\text{CLKOUT}} = \frac{f_{\text{CLKIN}}}{4}, \qquad T_{\text{CLKOUT}} = 4 \cdot T_{\text{CLKIN}} \]

IKA32010 用 `i_EMUCLK` 来扮演 CLKIN 的角色，于是一个 DSP「机器周期」就要消耗 4 个 `i_EMUCLK` 周期——这正是本讲要解释的核心。

## 3. 本讲源码地图

本讲只涉及一个源码文件，但会引用其中两段，外加 testbench 里的一小段作为实践素材。

| 文件 | 本讲关注的位置 | 作用 |
|------|--------------|------|
| `src/IKA32010.sv` | [第 3–9 行：端口声明](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L3-L9) | 定义 `i_EMUCLK` / `i_CLKIN_PCEN` / `o_CLKOUT` / `o_CLKOUT_PCEN` / `o_CLKOUT_NCEN` 五个时钟相关端口 |
| `src/IKA32010.sv` | [第 48–61 行：Clock 模块](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L48-L61) | `cyclecntr` 计数器与三个派生时钟信号的全部实现 |
| `src/IKA32010.sv` | [第 192–252 行：总线控制器里的 `case(cyclecntr)`](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L192-L252) | 演示 `cyclecntr` 的 4 个相位如何驱动外部总线时序（本讲只用它来佐证「4 相位」的存在） |
| `src/IKA32010_tb.v` | [第 10–13 行：testbench 的分频](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v#L10-L13) | 实践任务里用来观察 `i_CLKIN_PCEN` 如何被驱动 |

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

- **4.1 时钟模块 `cyclecntr`**：解释 4 分频计数器本身。
- **4.2 时钟使能 `cyc_pcen` / `cyc_ncen`**：解释从计数器派生出的两个相位脉冲，以及它们如何驱动芯片内部寄存器。

### 4.1 时钟模块（cyclecntr）

#### 4.1.1 概念说明

IKA32010 想要复刻 TMS32010 在**机器周期粒度**上的行为（这就是 README 里「semi-cycle-accurate，半周期精确」的含义）。原始芯片内部把一个机器周期切成 4 个相位，外部总线时序、取指时序都依赖这 4 个相位。因此 IKA32010 也必须在自己的实现里**显式地走出 4 个相位**。

它采用的办法是：用一个 2 位计数器 `cyclecntr` 在 `0 → 1 → 2 → 3 → 0` 之间循环，每个值代表一个相位。计数器以 `i_EMUCLK`（扮演原始 CLKIN）为节拍源前进，于是：

- 走完 4 个 `i_EMUCLK` 周期 = 走完 4 个相位 = 1 个 DSP 机器周期。
- 这就实现了「四分频」——把快时钟 `i_EMUCLK` 变成了慢节拍的机器周期。

为什么必须是 4 个相位、而不是 2 个或 8 个？因为原始 TMS32010 的总线时序天然分成 4 拍：地址建立、数据稳定、数据锁存、收尾。比如下一讲会看到，指令读取在相位 0–2 期间拉低 `o_MEN_n`，在相位 3 把数据锁存进指令寄存器。少了相位，就没办法在这一个周期内完成「先给地址、再读数据、最后锁存」这一连串动作。

#### 4.1.2 核心流程

`cyclecntr` 的更新规则可以用下面这段伪代码描述（每个 `i_EMUCLK` 上升沿检查一次）：

```
if (复位 i_RS_n == 0):
    cyclecntr = 0          # 复位强制归零
else if (i_CLKIN_PCEN == 1):   # 时钟使能打开时才前进
    if (cyclecntr == 3):
        cyclecntr = 0      # 走到末尾就回绕
    else:
        cyclecntr = cyclecntr + 1
# i_CLKIN_PCEN == 0 时：什么都不做（保持原值）
```

要点有三条：

1. **复位优先**：只要 `i_RS_n` 为低（复位有效），`cyclecntr` 立刻被钉在 0，与使能无关。
2. **使能门控**：只有 `i_CLKIN_PCEN` 为高的那些 `i_EMUCLK` 沿，计数器才真正前进。这是「时钟使能」的典型用法。
3. **自然回绕**：到 3 之后下一个值是 0，形成一个 0–1–2–3 的循环，不需要额外的进位逻辑。

#### 4.1.3 源码精读

计数器本身的声明和更新逻辑在 [src/IKA32010.sv:48-53](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L48-L53)：

```verilog
//master cycle counter
reg     [1:0]   cyclecntr;
always @(posedge i_EMUCLK) if(i_CLKIN_PCEN) begin
    if(!i_RS_n) cyclecntr <= 2'd0;
    else cyclecntr <= (cyclecntr == 2'd3) ? 2'd0 : cyclecntr + 2'd1;
end
```

逐行解读：

- `reg [1:0] cyclecntr;` —— 2 位宽，正好能表示 0、1、2、3 四个值。
- `always @(posedge i_EMUCLK)` —— 每个 `i_EMUCLK` 上升沿触发。
- `if(i_CLKIN_PCEN)` —— 整个块被时钟使能包住；使能为 0 时块内语句不执行，计数器保持不变。
- `if(!i_RS_n) cyclecntr <= 2'd0;` —— 复位有效时归零（同步复位，因为它在时钟沿里生效）。
- `cyclecntr <= (cyclecntr == 2'd3) ? 2'd0 : cyclecntr + 2'd1;` —— 到 3 回 0，否则加 1。这是一个标准的三目写法。

> 注意：u1-l3 已经指出，复位会把 `cyclecntr` 清零、把 PC 清零、把中断屏蔽位 `reg_intm` 置 1。本讲只关注 `cyclecntr` 这一被清零的对象。

**4 个相位到底被用来干什么？** 为了让你相信「4 个相位是真实存在且被广泛使用的」，看一下总线控制器里的写法 [src/IKA32010.sv:192-252](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L192-L252)。比如「指令读取」事务（`busctrl_mode == OPCODE_READ`）：

```verilog
case(cyclecntr)
    2'd0: begin o_MEN_n <= 1'b0; ... end   // 相位 0：开始拉低 MEN_n，发起读
    2'd1: begin o_MEN_n <= 1'b0; ... end   // 相位 1：保持
    2'd2: begin o_MEN_n <= 1'b0; ... end   // 相位 2：保持，数据应已稳定
    2'd3: begin o_MEN_n <= 1'b1; ... end   // 相位 3：释放总线，准备锁存
endcase
```

可以看到 `cyclecntr` 的 4 个取值确实对应了 4 个不同的总线节拍。详细的逐相位时序留到 u2-l3（总线控制器）深入，本讲只需要记住：**`cyclecntr` 是整颗芯片节拍的「总指挥」**。

#### 4.1.4 代码实践

**实践目标**：亲手算出「testbench 里一个 DSP 机器周期等于多少个 `i_EMUCLK` 周期」，从而验证四分频关系。

**操作步骤（源码阅读型 + 计算）**：

1. 打开 [src/IKA32010_tb.v:10-13](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v#L10-L13)，阅读 testbench 是如何驱动 `i_CLKIN_PCEN` 的：

   ```verilog
   reg [1:0] divider = 2'd0;
   always @(posedge EMUCLK) divider <= divider + 2'd1;
   wire cen_n = ~(divider == 2'd3);
   // ...
   .i_CLKIN_PCEN(~cen_n)
   ```

2. 推算 `divider` 的取值序列：它在每个 `EMUCLK` 上升沿加 1，序列是 `0,1,2,3,0,1,2,3,...`。
3. 推算 `i_CLKIN_PCEN = ~cen_n = (divider == 3)`：只有当 `divider == 3` 时为 1，其余为 0。所以 `i_CLKIN_PCEN` 是一个 **4 拍里只有 1 拍为高** 的窄脉冲（占空比 1/4）。
4. 根据 4.1.2 的规则：`cyclecntr` 只在 `i_CLKIN_PCEN == 1` 的那些沿前进。既然这些沿每 4 个 `EMUCLK` 才出现一次，那么 `cyclecntr` 走一步要花 4 个 `EMUCLK`。
5. `cyclecntr` 走完一整圈（0→1→2→3）需要 4 步，所以在该 testbench 里：

   \[ 1 \text{ 个 DSP 机器周期} = 4 \text{ 步} \times 4 \text{ EMUCLK/步} = 16 \text{ 个 } i\_EMUCLK \text{ 周期} \]

**需要观察的现象**：你会得到「16」这个数字。它比理论最小值「4」大，原因是 testbench 特意用 1/4 占空比的使能脉冲，把有效节拍再放慢了一倍，给仿真留出时序余量并贴近真实板子上的时钟关系。

**预期结果**：明确区分两个层次——

- **核心设计层面**：`cyclecntr` 本身每个使能拍前进一次，4 拍一圈，这是「四分频」的本质。
- **testbench 层面**：因为 testbench 的 `i_CLKIN_PCEN` 是 1/4 占空比，等效机器周期被拉长到 16 个 `EMUCLK`。在你自己实例化时，若把 `i_CLKIN_PCEN` 常接 1，则 1 个机器周期就严格等于 4 个 `i_EMUCLK`。

> 待本地验证：若你有仿真器，可在 testbench 里把 `i_CLKIN_PCEN` 改成常 1，重新数 `o_CLKOUT` 的周期，应观察到它恰好等于 4 个 `EMUCLK`。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `cyclecntr` 从 2 位改成 3 位，并把回绕点改成 7，IKA32010 还能正确仿真 TMS32010 吗？为什么？

**参考答案**：不能。原始 TMS32010 的机器周期就是 4 个相位，所有总线时序（`o_MEN_n` 等的低电平宽度、指令锁存时机）都按「相位 0–3」设计。改成 8 相位会改变 `o_CLKOUT` 的分频比（变成 8 分频）和所有外部时序，破坏「半周期精确」。

**练习 2**：复位期间 `i_RS_n == 0`，此时 `cyclecntr` 的值是什么？`i_CLKIN_PCEN` 的取值有影响吗？

**参考答案**：复位期间 `cyclecntr` 恒为 0（源码第 51 行 `if(!i_RS_n) cyclecntr <= 2'd0;` 优先级最高）。`i_CLKIN_PCEN` 此时无影响，因为复位分支在外层 `if(i_CLKIN_PCEN)` 之内——但要注意，整个 `always` 块被 `if(i_CLKIN_PCEN)` 门控，所以严格说复位也只在使能拍上生效；不过 testbench 的使能是周期性脉冲，复位效果仍会在下一个使能拍落地。

---

### 4.2 时钟使能信号（cyc_pcen / cyc_ncen）

#### 4.2.1 概念说明

`cyclecntr` 给出了 4 个相位，但芯片内部真正需要的是**两个关键节拍**：

- 一个对应原始 CLKOUT 的**上升沿附近**——叫 `cyc_pcen`（PCEN = Positive-edge Clock ENable）。
- 一个对应原始 CLKOUT 的**下降沿附近**——叫 `cyc_ncen`（NCEN = Negative-edge Clock ENable）。

为什么要区分这两个节拍？因为原始 TMS32010 的很多内部操作是「在 CLKOUT 的某个沿上发生」的：有的寄存器在上升沿更新，有的在下降沿更新。IKA32010 用同一个 `i_EMUCLK` 当主时钟，再用 `cyc_pcen` / `cyc_ncen` 这两个单拍脉冲当**时钟使能**，让不同寄存器各自在「正确的那一拍」更新——这就等效地重现了「上升沿寄存器」和「下降沿寄存器」的差别。

除此之外还有一个对外信号 `o_CLKOUT`，它是 `cyclecntr[1]` 的直接取值，对外呈现一个真正的方波，供外部电路当作参考时钟使用。

#### 4.2.2 核心流程

三个信号全部由 `cyclecntr` 和 `i_CLKIN_PCEN` **组合译出**（不占时钟沿，立刻跟随）：

```
o_CLKOUT      = cyclecntr[1]                       # 第 1 位：方波
o_CLKOUT_PCEN  = (cyclecntr == 1) 且 (i_CLKIN_PCEN == 1)   # 相位 1 的脉冲
o_CLKOUT_NCEN  = (cyclecntr == 3) 且 (i_CLKIN_PCEN == 1)   # 相位 3 的脉冲
```

为什么 `o_CLKOUT = cyclecntr[1]` 就是四分频方波？看 `cyclecntr` 四个取值的第 1 位（最高位）：

| `cyclecntr` | `cyclecntr[1]`（=`o_CLKOUT`） |
|:-----------:|:------------------------------:|
| 0 (00) | 0 |
| 1 (01) | 0 |
| 2 (10) | 1 |
| 3 (11) | 1 |

`cyclecntr` 走一圈，`o_CLKOUT` 的取值序列是 `0,0,1,1`——正好是「两拍低、两拍高」的方波，周期为 4 个 `i_EMUCLK`，于是：

\[ f_{\text{o\_CLKOUT}} = \frac{f_{\text{i\_EMUCLK}}}{4} \]

而 `o_CLKOUT_PCEN` 只在 `cyclecntr == 1` 那一拍为高，恰好紧挨在 `o_CLKOUT` 从 0→1 上升之前；`o_CLKOUT_NCEN` 只在 `cyclecntr == 3` 那一拍为高，紧挨在 `o_CLKOUT` 从 1→0 下降之前。两者都额外要求 `i_CLKIN_PCEN == 1`，以保证脉冲只在「真正前进的拍」上出现。

#### 4.2.3 源码精读

派生信号的实现集中在 [src/IKA32010.sv:56-61](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L56-L61)：

```verilog
//divided clock
assign  o_CLKOUT = cyclecntr[1];
assign  o_CLKOUT_NCEN = (cyclecntr == 2'd3) & i_CLKIN_PCEN;
assign  o_CLKOUT_PCEN = (cyclecntr == 2'd1) & i_CLKIN_PCEN;

wire            cyc_ncen = o_CLKOUT_NCEN;
wire            cyc_pcen = o_CLKOUT_PCEN;
```

解读：

- 三条 `assign` 都是纯组合逻辑，输出随 `cyclecntr` 立刻变化。
- `o_CLKOUT_PCEN` / `o_CLKOUT_NCEN` 用 `& i_CLKIN_PCEN` 做了使能门控——只有使能打开的那一拍，脉冲才真正输出。
- 最后两行把对外信号赋了内部别名 `cyc_pcen` / `cyc_ncen`，方便芯片内部别处引用。也就是说，`cyc_pcen` 和 `o_CLKOUT_PCEN` 是同一根线，`cyc_ncen` 和 `o_CLKOUT_NCEN` 是同一根线。

**这两个脉冲内部被谁用了？** 用 Grep 在 `src/IKA32010.sv` 里统计 `if(cyc_ncen)` 与 `if(cyc_pcen)` 的出现：

- `cyc_ncen`（相位 3，下降沿节拍）被用了 **13 次**，是绝对主力。程序计数器 PC [第 104 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L104)、溢出模式位 `reg_ovm` [第 265 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L265)、辅助寄存器、堆栈、ALU 结果写回等几乎所有「真正改状态」的动作，都在这一拍完成。
- `cyc_pcen`（相位 1，上升沿节拍）只被用了 **2 次**：一次是采样 `i_BIO_n` 引脚 [第 71 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L71)，另一次是中断同步链的一级 [第 363 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L363)。

这印证了「`cyc_ncen` 是机器周期里的主工作拍，`cyc_pcen` 只用于少数需要在上升沿采样的信号」。

> 小贴士：把对内（`cyc_*`）和对外（`o_CLKOUT_*`）信号取成同一根线，是一种很省事的写法——外部想看节拍、内部想用节拍，都引用同一个源，不会出现内外不一致。

#### 4.2.4 代码实践

**实践目标**：通过统计和归类，建立「哪个寄存器在哪个节拍更新」的直观印象。

**操作步骤（源码阅读型）**：

1. 在 `src/IKA32010.sv` 里搜索 `if(cyc_pcen)`，定位全部使用点（应为 2 处）。
2. 对每一处，记录它更新的寄存器名和用途：
   - [第 71 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L71)：`bio_n <= i_BIO_n;`——采样 BIO 输入。
   - [第 363 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L363)：`int_n_zz <= int_n_z;`——中断同步链中间级。
3. 再搜索 `if(cyc_ncen)`，挑出 3 个有代表性的使用点并记录：PC（第 104 行）、`reg_ovm`（第 265 行）、堆栈（第 513 行附近）。
4. 把结果填进一张表：

   | 节拍 | 代表性寄存器 | 数量级 | 直觉解释 |
   |------|------------|--------|---------|
   | `cyc_pcen`（相位 1） | `bio_n`、`int_n_zz` | 极少（2） | 需要「靠上升沿」对齐外部异步信号的采样 |
   | `cyc_ncen`（相位 3） | `if_pc`、`reg_ovm`、栈、AR… | 很多（13） | 机器周期主工作拍，绝大多数状态更新在这里 |

**需要观察的现象**：你会清楚地看到 `cyc_ncen` 远多于 `cyc_pcen`，从而理解「下降沿节拍是这颗芯片的主力节拍」。

**预期结果**：得到一张说明 `cyc_pcen` / `cyc_ncen` 分工的表，并能解释为什么 BIO 和中断同步偏偏要用 `cyc_pcen`（因为它们是异步输入，需要在周期较早的相位就采样好，供相位 3 的主逻辑使用）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `o_CLKOUT_PCEN` 和 `o_CLKOUT_NCEN` 的表达式里都要 `& i_CLKIN_PCEN`？如果去掉这个与项会怎样？

**参考答案**：因为这两个脉冲的目的是「在计数器真正前进的那一拍标记相位」。如果 `i_CLKIN_PCEN == 0`，`cyclecntr` 不会变，相位是「停滞」的，此时不应该产生新的节拍脉冲。若去掉 `& i_CLKIN_PCEN`，那么在使能关闭的若干拍里，`cyclecntr` 停在某个值（比如 1 或 3），脉冲就会**连续多个 `EMUCLK` 拉高**，被内部寄存器误当成多个有效节拍，导致一个机器周期内状态被更新多次，破坏时序。

**练习 2**：`o_CLKOUT` 上升沿和 `cyc_pcen` 脉冲，谁先出现？

**参考答案**：`cyc_pcen` 先。`cyc_pcen` 在 `cyclecntr == 1`（即 `o_CLKOUT` 仍为 0 的最后一拍）时拉高；紧接着 `cyclecntr` 进入 2，`o_CLKOUT` 才上升为 1。所以 `cyc_pcen` 是「紧挨在 `o_CLKOUT` 上升沿之前」的那个使能脉冲——这正是它叫 Positive-edge Clock ENable 的原因。

---

## 5. 综合实践

把本讲两个模块串起来：**画出在一个 DSP 机器周期内（`cyclecntr` 从 0 走到 3），`EMUCLK` / `cyclecntr` / `o_CLKOUT` / `o_CLKOUT_PCEN` / `o_CLKOUT_NCEN` 五条信号的时序波形图。**

### 5.1 实践目标

用一个波形图同时说清三件事：

1. 4 个 `i_EMUCLK` 对应 1 个机器周期（四分频）。
2. `o_CLKOUT` 是 `0,0,1,1` 的方波。
3. `cyc_pcen` 落在 `o_CLKOUT` 上升沿前、`cyc_ncen` 落在下降沿前。

### 5.2 操作步骤

1. 假设 `i_CLKIN_PCEN` 常接 1（最简单的实例化方式），以「每个 `i_EMUCLK` 周期」为一列。
2. 从复位刚释放、`cyclecntr` 为 0 开始，逐列填表。

   | 列（EMUCLK 周期） | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
   |---|---|---|---|---|---|---|---|---|
   | `cyclecntr` | 0 | 1 | 2 | 3 | 0 | 1 | 2 | 3 |
   | `o_CLKOUT`=`c[1]` | 0 | 0 | 1 | 1 | 0 | 0 | 1 | 1 |
   | `o_CLKOUT_PCEN` | 0 | **1** | 0 | 0 | 0 | **1** | 0 | 0 |
   | `o_CLKOUT_NCEN` | 0 | 0 | 0 | **1** | 0 | 0 | 0 | **1** |

3. 把上表转成横向 ASCII 波形（示例，高用 `▔`、低用 `_`，每列代表一个 EMUCLK 周期）：

   ```
   cyclecntr    : 0   1   2   3 | 0   1   2   3 |
   o_CLKOUT     : ____▔▔▔▔____|____▔▔▔▔____|
                                  ↑升            ↑降
   o_CLKOUT_PCEN: ____█________|____█________|     ← 相位 1
   o_CLKOUT_NCEN: __________█__|__________█__|     ← 相位 3
   ```

4. 标注两个关键对齐关系：
   - `o_CLKOUT_PCEN` 的脉冲（相位 1）紧贴 `o_CLKOUT` 上升沿之前。
   - `o_CLKOUT_NCEN` 的脉冲（相位 3）紧贴 `o_CLKOUT` 下降沿之前。

### 5.3 需要观察的现象

- 一个 `o_CLKOUT` 完整周期（一升一降）正好跨 4 个 `i_EMUCLK` 周期，验证 \( f_{\text{o\_CLKOUT}} = f_{\text{i\_EMUCLK}} / 4 \)。
- `o_CLKOUT_PCEN` 与 `o_CLKOUT_NCEN` 在一个机器周期内各只出现一次，且分别对应「升」与「降」两个节拍。

### 5.4 预期结果

得到一张与上表一致的波形图。若条件允许，可进一步用 testbench 仿真验证：

- 打开 [src/IKA32010_tb.v](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v)，把 `o_CLKOUT` / `o_CLKOUT_PCEN` / `o_CLKOUT_NCEN` 三个端口接到波形窗口（目前 testbench 第 34–36 行把它们留空，你需要补上观测线）。
- 注意：因为 testbench 的 `i_CLKIN_PCEN` 是 1/4 占空比（见 4.1.4），仿真里每个相位会占 4 个 `EMUCLK`，所以一个完整机器周期会显示为 16 个 `EMUCLK` 宽——形状不变，只是被横向拉长。

> 待本地验证：仿真波形的具体电平宽度取决于 testbench 的 `EMUCLK` 半周期（`always #1 EMUCLK = ~EMUCLK`，即 2 个时间单位为一个 `EMUCLK` 周期，见 [tb 第 8 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v#L8)）。本讲不假设你已经跑过仿真。

## 6. 本讲小结

- IKA32010 用一个 2 位计数器 [`cyclecntr`](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L48-L53) 在 `0→1→2→3` 循环，对应原始 TMS32010 机器周期的 4 个相位，因此 **4 个 `i_EMUCLK` = 1 个 DSP 机器周期**，这就是「四分频」的本质。
- 计数器只在 `i_CLKIN_PCEN == 1` 的 `i_EMUCLK` 上升沿前进，并被同步复位 `i_RS_n` 强制清零。
- [`o_CLKOUT = cyclecntr[1]`](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L56) 是一个 `0,0,1,1` 的方波，对外频率为 \( f_{\text{i\_EMUCLK}}/4 \)。
- [`cyc_pcen`](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L58)（相位 1，对应上升沿）只用于 BIO 采样和中断同步；[`cyc_ncen`](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L57)（相位 3，对应下降沿）是主力节拍，驱动 PC、栈、ALU 写回等几乎所有状态更新。
- testbench 里 `i_CLKIN_PCEN` 被做成 1/4 占空比窄脉冲（[tb 第 10–13 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v#L10-L13)），使得仿真中一个机器周期等于 16 个 `EMUCLK`；自己实例化时常接 1 则严格为 4 个。

## 7. 下一步学习建议

现在你已经掌握了芯片的「节拍发生器」。接下来可以：

- **u1-l5（仿真与 testbench 入门）**：动手把 testbench 跑起来，在波形窗口里亲眼看到本讲画的那些波形，把抽象的时序图变成屏幕上的真实信号。
- **u2-l2（程序计数器 PC 与取指）**：看 `cyc_ncen` 这个主力节拍如何具体驱动 PC 更新与指令锁存（`if_opcodereg` 在 `cyclecntr == 3` 时锁存），把「相位」和「取指时序」联系起来。
- **u2-l3（外部总线控制器）**：深入 [第 192–252 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L192-L252) 的 `case(cyclecntr)`，搞清 4 个相位如何各自控制 `o_MEN_n` / `o_DEN_n` / `o_WE_n` 的电平，本讲只点了名，那里才是完整的故事。
