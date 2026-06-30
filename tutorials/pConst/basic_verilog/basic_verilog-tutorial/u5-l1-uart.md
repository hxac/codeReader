# UART 收发：uart_tx / uart_rx

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 UART 异步串行帧的格式：**起始位（0）+ 8 位数据（LSB 在先）+ 停止位（1）**，以及为什么空闲电平是 1。
- 解释 `uart_tx` 怎样用参数 `BAUD_DIVISOR = CLK_HZ / BAUD` 把一个高速系统时钟**切成位节拍**（`tx_do_sample` 采样脉冲）。
- 看懂发送器那条 `{ tx_shifter[9:0], txd } <= { ... } >> 1` 的**整体右移**是如何依次把起始位、8 位数据、停止位挤到 `txd` 上的。
- 解释 **`busy` 握手的“提前置位 / 提前复位”**技巧：为什么 `busy` 在打出停止位的**同一个沿**就落下，从而支持**连续背靠背**全速发送。
- 读懂接收器 `uart_rx` 如何复用 `delay`（同步）和 `edge_detect`（起始位检测），并在**每位中点**采样、最后用停止位做**帧校验**。

## 2. 前置知识

本讲承接 **u2-l3（delay）**、**u2-l2（edge_detect）** 和 **u2-l1（clk_divider）**，请确保你已经掌握：

- **`delay.sv`** 当 `LENGTH=2` 时是一串两级触发器，可作**同步器**消除外部引脚的亚稳态（u2-l3、u3-l1）。
- **`edge_detect.sv`** 用一级延迟寄存器比较得到 `rising`/`falling`/`both` 单拍脉冲（u2-l2）。
- **`clk_divider.sv`** 用一个自由运行的二进制计数器得到 `clk/2^(N+1)` 的派生时钟（u2-l1）——本讲的波特分频和它思想同源，但切出来的是**采样脉冲**而非新时钟。

另外你需要一点 UART 的直觉：UART（Universal Asynchronous Receiver/Transmitter，通用异步收发器）只有一根数据线（TXD/RXD），收发双方**不共享时钟**，靠事先约好的**波特率（BAUD，每秒位数）**各自计时。于是发送方必须自己按时节拍“一位一位”把电平推到线上，接收方必须自己识别帧的开头并在恰当位置采样。本讲就是把这两件事在源码里讲透。

> 小提示：README 里 [uart_tx.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/uart_tx.sv) 与 [uart_rx.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/uart_rx.sv) 都是 🟢 绿圈基础模块。仓库里还有一组 `uart_tx_shifter.sv` / `uart_rx_shifter.sv`（参数化起止位、用于 FPGA 之间简单同步通信），本讲**不讲**它们，但 [uart_tx_rx_shifter_tb.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/uart_tx_rx_shifter_tb.sv) 是很好的回环测试写法范本，综合实践会借鉴它的风格。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲怎么看 |
|------|------|-----------|
| [uart_tx.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/uart_tx.sv) | 简洁的 UART **发送器**（SystemVerilog） | 重点：`BAUD_DIVISOR` 分频计数器、10 位帧的装入与右移、`busy` 提前复位 |
| [uart_tx.v](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/uart_tx.v) | 与 `uart_tx.sv` **等价**的 Verilog-2001 版本 | 做对照：只有 `always`/`reg` 与 `always_ff`/`logic` 的写法差异，逻辑完全一致 |
| [uart_rx.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/uart_rx.sv) | 简洁的 UART **接收器** | 重点：`delay` 同步 `rxd`、`edge_detect` 找起始位、1.5 位周期后开始**中点采样**、停止位校验 |
| [uart_tx_rx_shifter_tb.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/uart_tx_rx_shifter_tb.sv) | 收发回环 + FIFO 自检的 testbench | 借鉴它的时钟/复位生成与“发送端写 FIFO、接收端比对”的自检结构 |

---

## 4. 核心概念与源码讲解

本讲覆盖四个最小模块：**波特分频**、**移位帧（含起止位）**、**busy 握手**、**接收器 uart_rx**。前三者属于发送器 `uart_tx`，第四者把同一套波特思想用在接收侧并把前几讲的 `delay`/`edge_detect` 串起来。

### 4.1 波特分频：从系统时钟切出位节拍

#### 4.1.1 概念说明

FPGA 内部跑的是几十到几百兆赫的系统时钟（如 50 MHz、200 MHz），而 UART 一位只占 `1/BAUD` 秒（115200 波特时约 8.68 µs）。发送器要“每隔一个位周期换一位输出”，最直接的做法是用一个**向下计数器**把系统时钟分频，每计满一个位周期就产生一个**单拍脉冲** `tx_do_sample`，所有“换位”动作都只在这个脉冲发生的时钟沿执行。

这样 `tx_do_sample` 就是整条发送链的“节拍器”：它在每个位周期里只高 1 个时钟周期，把它当作“现在该切换到下一位了”的信号，位与位之间就严格相差一个位周期。

#### 4.1.2 核心流程

位周期（以系统时钟周期计）由参数 `BAUD_DIVISOR` 给出：

\[
\text{BAUD\_DIVISOR} = \left\lfloor \frac{\text{CLK\_HZ}}{\text{BAUD}} \right\rfloor
\]

计数器 `tx_sample_cntr` 从 `BAUD_DIVISOR-1` 向下数到 0，到达 0 的那一拍：

- `tx_do_sample` 拉高（仅 1 拍）；
- 下一拍计数器自动重装为 `BAUD_DIVISOR-1`，开始下一个位周期。

于是 `tx_do_sample` 的周期恰为 `BAUD_DIVISOR` 个时钟周期 = 一个位周期：

\[
T_{\text{bit}} = \text{BAUD\_DIVISOR} \times T_{\text{clk}} = \frac{1}{\text{BAUD}}
\]

```text
clk            ┌─┐ ┌─┐ ┌─┐ ┌─┐ ┌─┐ ┌─┐        （系统时钟，周期 T_clk）
tx_sample_cntr │BAUD_DIV-1 │… 衰减 …│ 0 │BAUD_DIV-1 │…
tx_do_sample                          ┌─┐                （每位周期 1 拍脉冲）
                                     位 0   位 1   位 2 …
```

#### 4.1.3 源码精读

参数与端口里，`BAUD_DIVISOR` 由 `CLK_HZ / BAUD` 直接算出，是本讲的“分频比”：

[uart_tx.sv:L38-L51](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/uart_tx.sv#L38-L51) —— 参数化端口，`bit [15:0] BAUD_DIVISOR = CLK_HZ / BAUD` 让你只填时钟频率和波特率，分频比自动算：

```systemverilog
module uart_tx #( parameter
  CLK_HZ = 200_000_000,
  BAUD = 9600,
  bit [15:0] BAUD_DIVISOR = CLK_HZ / BAUD
)(
  input clk,
  input nrst,
  input [7:0] tx_data,
  input tx_start,                 // write strobe
  output logic tx_busy = 1'b0,
  output logic txd = 1'b1         // 空闲电平为 1
);
```

注意 `txd` 的初值是 `1'b1`——UART 空闲时数据线必须保持高电平，否则对方会把误出现的低电平当成起始位。

位节拍计数器是典型的“数到 0 就重装”结构：

[uart_tx.sv:L53-L64](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/uart_tx.sv#L53-L64) —— 自由向下计数器 + 组合产生 `tx_do_sample` 脉冲：

```systemverilog
logic [15:0] tx_sample_cntr = '0;
always_ff @ (posedge clk) begin
  if( (~nrst) || (tx_sample_cntr[15:0] == '0) ) begin
    tx_sample_cntr[15:0] <= (BAUD_DIVISOR-1'b1);   // 到 0 或复位 → 重装
  end else begin
    tx_sample_cntr[15:0] <= tx_sample_cntr[15:0] - 1'b1;
  end
end

logic tx_do_sample;
assign tx_do_sample = (tx_sample_cntr[15:0] == '0);  // 每位周期 1 拍
```

关键点：计数器与发送状态机**互相独立**——它永远在跑，不受 `tx_busy` 影响。INFO 注释里那句“多个 `uart_tx` 实例应共用 `tx_sample_cntr`”正是这个意思：节拍器可以一份逻辑喂多个发送器，省面积。

#### 4.1.4 代码实践

**目标**：肉眼确认“`tx_do_sample` 每 `BAUD_DIVISOR` 拍出现一次”。

1. 例化 `uart_tx`，参数 `CLK_HZ(50_000_000)`、`BAUD(115200)`。
2. 在 testbench 里用 `always @(posedge clk) if(tx_do_sample) $display(...)` 打印每次采样脉冲的时间戳。
3. 在波形里用游标量两个相邻 `tx_do_sample` 之间的时钟周期数。

**预期结果**：相邻两次脉冲间隔 **434** 个时钟周期（\( \lfloor 50\,000\,000 / 115200 \rfloor = 434 \)），对应实际波特率 \( 50\,000\,000/434 \approx 115207 \) bps，与标称 115200 偏差仅约 0.006%，远在 UART 容忍范围（通常 ±2~3%）之内。

#### 4.1.5 小练习与答案

**Q1**：为什么 `BAUD_DIVISOR` 用 `CLK_HZ / BAUD`（整除）而不是 `BAUD / CLK_HZ`？
**答**：前者得到“一位对应多少个系统时钟周期”（一个很大的整数，如 434），正好拿来做计数器模值；后者是个接近 0 的小数，无法直接当计数周期用。

**Q2**：若 `CLK_HZ=50MHz` 却想要 `BAUD=3_000_000`，会发生什么？
**答**：`BAUD_DIVISOR = 50_000_000/3_000_000 = 16`，仍可工作；但 INFO 写明 **最大波特率为 `CLK_HZ/2`**（此时 `BAUD_DIVISOR=2`，每位只占 2 拍，已接近极限），再高就无法保证每拍采样质量。

---

### 4.2 移位帧与起止位：把一个字节装配成 UART 帧

#### 4.2.1 概念说明

UART 的一帧由三段拼成：**1 位起始位（0）+ 8 位数据（LSB 在先）+ 1 位停止位（1）**，共 10 位。`uart_tx` 把这 10 位塞进一个 10 位移位寄存器 `tx_shifter[9:0]`，让它们排队从最低位（`tx_shifter[0]`）逐位“挤”到 `txd` 上。

起止位的作用：
- **起始位（0）**：空闲线本是 1，一个突然的 0（下降沿）就是“我要开始说话了”的信号，接收方据此对齐帧头。
- **停止位（1）**：保证一帧结束后线回到高电平，既是给接收方的“结束确认”，也是为了在连续发送时下一帧的起始位（下降沿）能被正确识别——若停止位也是 0，两帧之间就没有从 1 到 0 的跳变。

`uart_tx` 的起止位长度是**硬编码**的（INFO：One stop bit setting is hardcoded），即固定 1 起始位 + 8 数据位 + 1 停止位。

#### 4.2.2 核心流程

装入时把字节拼成 10 位帧（注意大括号里从高位到低位是 停止位、数据、起始位）：

```text
tx_shifter[9:0] = { 1'b1, tx_data[7:0], 1'b0 }
                    ↑停止      数据 D7..D0     ↑起始
                   [9]      [8]……[1]          [0]
```

此后每个 `tx_do_sample` 沿，把 `{tx_shifter[9:0], txd}` 这 **11 位**整体右移 1 位：`tx_shifter[0]` 溢出到 `txd`，最高位补 0。连续 10 次右移后，`txd` 上依次出现：

```text
空闲(1) │ 起始(0) │ D0 │ D1 │ D2 │ D3 │ D4 │ D5 │ D6 │ D7 │ 停止(1) │ 空闲(1)
```

例如字符 `'A' = 8'h41 = 8'b0100_0001`，LSB 在先，则 D0..D7 = `1,0,0,0,0,0,1,0`，整帧电平序列为 `0,1,0,0,0,0,0,1,0,1`。

#### 4.2.3 源码精读

装入语句把三段拼成帧，是 4.2.1 的直接对应：

[uart_tx.sv:L73-L78](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/uart_tx.sv#L73-L78) —— 当不忙且收到 `tx_start` 写脉冲时，异步装入 10 位帧并立即把 `tx_busy` 拉高：

```systemverilog
if( ~tx_busy ) begin
  // asynchronous data load and 'busy' set
  if( tx_start ) begin
    tx_shifter[9:0] <= { 1'b1,tx_data[7:0],1'b0 };  // 停止|数据|起始
    tx_busy <= 1'b1;
  end
end
```

真正“把位挤上 `txd`”的是这条精巧的整体右移：

[uart_tx.sv:L81-L83](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/uart_tx.sv#L81-L83) —— 仅在 `tx_do_sample` 节拍移位，保证每位宽度恰为一个位周期：

```systemverilog
if( tx_do_sample ) begin    // next bit
  // txd MUST change only on tx_do_sample although data may be loaded earlier
  { tx_shifter[9:0],txd } <= { tx_shifter[9:0],txd } >> 1;
```

把 `{tx_shifter[9:0], txd}` 看成一个 11 位整体：每右移 1 位，最低位的 `tx_shifter[0]` 就掉进 `txd`，于是 `txd` 依次等于 `tx_shifter[0]` 在各时刻的值——即起始位、D0、D1……停止位。注意数据可以**早一点**装入（注释明说 `data may be loaded earlier`），但 `txd` 的变化**只**被 `tx_do_sample` 节拍驱动，从而保证每位等宽。

#### 4.2.4 代码实践

**目标**：验证一帧的电平序列与 4.2.2 推断一致。

1. 例化 `uart_tx`（50 MHz / 115200），发送 `8'h41`。
2. 在 testbench 里用一个 10 位的“期望序列”寄存器装 `{1'b1, 8'h41, 1'b0}`，再写一个进程：每当 `tx_do_sample` 采样到 `txd`，就把它压进一个观察向量。
3. 发送 10 次采样后，把观察向量与期望序列右移后的逐位输出做对比。

**预期结果**：观察到的 `txd` 序列为 `0,1,0,0,0,0,0,1,0,1`（起始 + D0..D7 + 停止），与手算完全一致。

#### 4.2.5 小练习与答案

**Q1**：为什么数据是 **LSB 在先**而不是 MSB 在先？
**答**：这是 UART 的历史约定。收发双方必须一致；本模块把 `tx_data[0]` 放在最靠近 `txd` 输出端（`tx_shifter[0]`），所以最先移出的是最低位。

**Q2**：停止位为什么必须是 1？
**答**：一是把线路拉回空闲高电平，二是为下一帧的起始位（下降沿）创造条件。若停止位为 0，连续两帧之间没有 1→0 的跳变，接收方就无法识别第二帧的开始。

**Q3**：`{ tx_shifter[9:0],txd } >> 1` 一共右移 10 次后，`tx_shifter` 里还剩什么？
**答**：原始内容（停止位+数据+起始位）已全部移出，最高位补了 10 个 0，因此 `tx_shifter[9:0]` 全为 0——这正是下一节“提前复位 `busy`”的判据来源。

---

### 4.3 busy 握手：提前置位与提前复位

#### 4.3.1 概念说明

`tx_busy` 是发送器给上游（要送字节来的模块）的握手信号：高表示“正在发，别打断/请等我”。上游典型的用法是：等 `tx_busy` 为 0，把字节放到 `tx_data` 上并给一个 `tx_start` 脉冲，然后继续等下一次空闲。

`uart_tx` 有个关键的优化叫 **early asynchronous 'busy' set and reset**（提前异步置位/复位）：

- **提前置位**：收到 `tx_start` 的**同一拍**就拉高 `busy`（不等第一个位真正打出），让上游立刻知道“收到了，别再送”。
- **提前复位**：`busy` 在打出**停止位**的那个沿就落下，而不是等停止位完整地在 `txd` 上维持一个位周期之后。由于停止位电平（1）和空闲电平（1）一模一样，这段时间可以用来**预装下一字节**，从而实现**背靠背连续全速发送**。

#### 4.3.2 核心流程

```text
           装入帧                  打出起始位……打出D7        打出停止位
clk  ────────┬─────────────────────────────┬──────────┬───────────────┬──────
tx_start  ───┐(1 拍)                                            │
tx_busy     0└─────────────── 1 ──────────────────────────────┘│── 0 ────
txd     …1(空闲)1 起始(0) D0……D7 1 停止位          1 停止位完整一位│1(空闲)…
                                                  ↑ busy 在此沿落下，
                                                    停止位仍在 txd 上完整维持一位，
                                                    这段时间内可装入下一帧
```

“提前复位”的判据是 `~|tx_shifter[9:1]`——即 `tx_shifter` 的高 9 位全为 0。结合 4.2.5 的 Q3：移了 9 次后，原始 10 位只剩最低位 `[0]`（即原来的停止位 1 还没挤上 `txd`），高位 `[9:1]` 已全被补 0 填满——这恰是“下一位将打出停止位”的时刻，于是 `busy` 在这拍落下。

#### 4.3.3 源码精读

提前置位与 4.2.3 的装入是同一段代码：`tx_busy <= 1'b1` 与数据装入在同一时钟沿生效。

提前复位在状态机的 `else`（忙）分支里，仍在 `tx_do_sample` 节拍下：

[uart_tx.sv:L82-L89](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/uart_tx.sv#L82-L89) —— 右移之后立刻判 `tx_shifter[9:1]` 是否归零，归零则提前撤 `busy`：

```systemverilog
if( tx_do_sample ) begin    // next bit
  { tx_shifter[9:0],txd } <= { tx_shifter[9:0],txd } >> 1;
  // early asynchronous 'busy' reset
  if( ~|tx_shifter[9:1] ) begin       // 归约或非：高 9 位全 0
    // txd still holds data, but shifter is ready to get new info
    tx_busy <= 1'b0;
  end
end
```

注释 `txd still holds data` 说得很清楚：这一拍 `txd` 刚拿到停止位（1）并会完整维持一个位周期，但 `busy` 已经撤掉，于是上游可以在这一个位周期里装入下一帧，使下一帧的起始位紧接当前停止位之后出现，没有空隙。这就是 INFO 里 **“continuous data output at BAUD levels up to CLK_HZ/2”** 的实现原理。

#### 4.3.4 代码实践

**目标**：观察“提前复位”带来的背靠背连续发送。

1. 把 `tx_start` 恒接 `1'b1`（参考 [uart_tx_rx_shifter_tb.sv:L104](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/uart_tx_rx_shifter_tb.sv#L104) 的 `assign start = 1'b1;`），并让 `tx_data` 不断变化（例如接一个计数器）。
2. 在波形里看 `txd`：应看到一帧接一帧，每帧的停止位（1）后**紧跟**下一帧的起始位（0），帧与帧之间没有额外的空闲高电平拉长。

**预期结果**：连续 10 个位周期一帧、无空隙；若把提前复位那行（`if( ~|tx_shifter[9:1] ) tx_busy<=0;`）注释掉再仿真，会看到帧之间多出一段不定长的高电平空隙——这正是优化被去掉的后果。

#### 4.3.5 小练习与答案

**Q1**：如果没有“提前复位”，连续发送时会发生什么？
**答**：`busy` 要等停止位在 `txd` 上完整维持一位之后才落下，下一帧的装入会被推迟，帧与帧之间出现至少一个位周期（甚至更多，取决于对齐）的空闲间隙，无法做到全速背靠背。

**Q2**：判据为何用 `~|tx_shifter[9:1]`（不含 `[0]`）？
**答**：此时 `[0]` 里还留着尚未挤出的停止位（1），若把它也算进归约就永远不会全 0；排除 `[0]` 后，高 9 位归零恰能精确表示“就剩最后一位要发出”。

---

### 4.4 接收器 uart_rx：同步、起始位检测、中点采样与停止位校验

#### 4.4.1 概念说明

`uart_rx` 把同一套“位节拍”思想反过来用：发送方按节拍换位，接收方按节拍采样。难点在于接收方**不知道帧何时开始**，必须：

1. **同步**：外部 `rxd` 引脚是异步信号，先用两级触发器同步成本域（复用 u2-l3 的 `delay`），避免亚稳态（u3-l1）。
2. **找帧头**：空闲时 `rxd=1`，起始位把它拉成 0——这个**下降沿**就是帧头。用 `edge_detect` 的 `falling` 输出捕获它（复用 u2-l2）。
3. **中点采样**：在下降沿之后等 **1.5 个位周期**到达 D0 的正中间，此后**每隔 1 个位周期**采一位（D1…D7），保证每位都在其电平最稳定的中央被读取，避开边沿抖动。
4. **停止位校验**：第 9 次采样应采到 1（停止位）；若采到 0 说明帧错（framing error），报告 `rx_err`，否则给一个 `rx_done` 读脉冲。

#### 4.4.2 核心流程

```text
rxd(异步)──▶ delay(2级同步) ──▶ rxd_s(本域干净)
                                     │
                          edge_detect.falling ──▶ start_bit_strobe（帧头脉冲）

  start_bit_strobe 到来 → rx_busy=1，rx_sample_cntr 装入 1.5 位周期(BAUD_DIVISOR_2*3-1)
                                    │
                        数到 0（=D0 中点）→ 采第 1 位，重装 1 位周期(BAUD_DIVISOR_2*2-1)
                                    │
                        每数到 0 采一位：D0…D7（共 8 次），用“标记位”计数
                                    │
                  第 9 次采样(停止位中点) → rx_done(=1) / rx_err(=0) ，rx_busy 提前撤
```

`BAUD_DIVISOR_2 = CLK_HZ/BAUD/2` 是**半位周期**。于是 `BAUD_DIVISOR_2*3` = 1.5 位周期（用于从帧头跳到 D0 中点），`BAUD_DIVISOR_2*2` = 1 位周期（后续逐位间隔）。

接收用一个 9 位移位寄存器 `{rx_data[7:0], rx_data_9th_bit}`：初始化时往 `rx_data[7]` 放一个“标记位”1，其余为 0。每采一位把 `rxd_s` 从最高位移入，标记位随之向低位推进；当它推进到 `rx_data_9th_bit` 时，说明已采满 8 位数据，下一次采样就是停止位。

#### 4.4.3 源码精读

参数与端口，`BAUD_DIVISOR_2` 是半位周期，输出多了 `rx_done`（读脉冲）与 `rx_err`（帧错）：

[uart_rx.sv:L35-L48](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/uart_rx.sv#L35-L48) —— 接收器端口，注意 `rx_done` 是 read strobe、`rx_err` 报告停止位异常：

```systemverilog
module uart_rx #( parameter
  CLK_HZ = 200_000_000,
  BAUD = 9600,
  bit [15:0] BAUD_DIVISOR_2 = CLK_HZ / BAUD / 2          // 半位周期
)(
  input clk, input nrst,
  output logic [7:0] rx_data = '0,
  output logic rx_busy = 1'b0,
  output logic rx_done,         // read strobe
  output logic rx_err,
  input rxd
);
```

外部引脚先过两级同步器，正是 u2-l3/u3-l1 的标准做法：

[uart_rx.sv:L51-L62](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/uart_rx.sv#L51-L62) —— 用 `delay LENGTH=2` 同步 `rxd`，消除异步引脚的亚稳态：

```systemverilog
logic rxd_s;
delay #(
  .LENGTH( 2 ),
  .WIDTH( 1 )
) rxd_synch (
  .clk( clk ), .nrst( nrst ), .ena( 1'b1 ),
  .in( rxd ), .out( rxd_s )
);
```

起始位检测用 `edge_detect` 的下降沿输出，正是 u2-l2 的直接复用：

[uart_rx.sv:L65-L71](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/uart_rx.sv#L65-L71) —— 捕获 `rxd_s` 的下降沿作为帧头脉冲 `start_bit_strobe`：

```systemverilog
logic start_bit_strobe;
edge_detect rxd_fall_detector (
  .clk( clk ), .anrst( nrst ), .in( rxd_s ),
  .falling( start_bit_strobe )
);
```

收到帧头后装入 1.5 位周期、置忙、并把标记位放进移位寄存器最高位：

[uart_rx.sv:L88-L96](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/uart_rx.sv#L88-L96) —— 帧头到来时设置首次采样时刻（D0 中点）与标记位初值：

```systemverilog
if( start_bit_strobe ) begin
  // wait for 1,5-bit period till next sample
  rx_sample_cntr[15:0] <= (BAUD_DIVISOR_2 * 3 - 1'b1);   // 1.5 位周期
  rx_busy <= 1'b1;
  {rx_data[7:0],rx_data_9th_bit} <= 9'b10000000_0;        // 标记位在 rx_data[7]
end
```

后续每位重装 1 位周期，并在采样沿把 `rxd_s` 移入；标记位推进到 `rx_data_9th_bit` 时提前撤忙：

[uart_rx.sv:L99-L114](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/uart_rx.sv#L99-L114) —— 忙时每个位周期采一次：未满 8 位就移入 `rxd_s`，满 8 位（标记位到顶）就提前撤 `busy`：

```systemverilog
if( rx_sample_cntr[15:0] == '0 ) begin
  rx_sample_cntr[15:0] <= (BAUD_DIVISOR_2 * 2 - 1'b1);   // 重装 1 位周期
end else begin
  rx_sample_cntr[15:0] <= rx_sample_cntr[15:0] - 1'b1;
end

if( rx_do_sample ) begin
  if( rx_data_9th_bit == 1'b1 ) begin
    rx_busy <= 1'b0;                       // 已采满 8 位，提前撤忙
  end else begin
    {rx_data[7:0],rx_data_9th_bit} <= {rxd_s, rx_data[7:0]};  // 右移并入
  end
end
```

8 次采样后标记位恰好到达 `rx_data_9th_bit`，`rx_data` 自动排成 `rx_data[0]=D0 … rx_data[7]=D7`（LSB 在先，方向正确）。最后用组合逻辑在第 9 次（停止位）采样上做校验：

[uart_rx.sv:L120-L124](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/uart_rx.sv#L120-L124) —— 停止位采样时 `rxd_s` 为 1 给 `rx_done`，为 0 给 `rx_err`：

```systemverilog
always_comb begin
  // rx_done and rx_busy fall simultaneously
  rx_done <= rx_data_9th_bit && rx_do_sample && rxd_s;     // 停止位正确
  rx_err  <= rx_data_9th_bit && rx_do_sample && ~rxd_s;    // 帧错
end
```

#### 4.4.4 代码实践

**目标**：用发送器喂接收器做回环，自检收到的字节是否等于发送字节。

1. 把 `uart_tx` 的 `txd` 直接连到 `uart_rx` 的 `rxd`（电平回环，参考 [uart_tx_rx_shifter_tb.sv:L98-L135](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/uart_tx_rx_shifter_tb.sv#L98-L135) 的结构，但改用本讲的 `uart_tx`/`uart_rx`）。
2. 发送若干已知字节，在每个 `rx_done` 脉冲比对 `rx_data` 与发送值。
3. 编译时记得把 `uart_rx.sv` 依赖的 `delay.sv`、`edge_detect.sv` 一起加入工程（`uart_tx` 无外部依赖，可单独编译）。

**预期结果**：每个发送字节都能在 `rx_done` 拍被正确收回，`rx_err` 始终为 0。注意 `uart_rx` 用的是“中点采样”，可在波形上确认每次 `rx_do_sample` 都落在每位电平的正中央。

#### 4.4.5 小练习与答案

**Q1**：为什么首次采样要等 **1.5** 个位周期，而不是 1 个？
**答**：帧头（下降沿）在起始位的**开头**。从此处走 0.5 位周期到起始位中点，再走 1.0 位周期到 D0 中点，共 1.5 位周期。这样第一次采的就是 D0 正中央，后续每 1.0 位周期采一位，每位都落在各自中点，最抗抖动。

**Q2**：`rx_data_9th_bit` 这个“标记位”有什么用？
**答**：它既是“已收到几位”的计数器（每收一位向低位推进），又是“第 9 次采样=停止位”的判别位——到达 `rx_data_9th_bit` 时说明前 8 位数据已收齐。用一个移位标记替代了单独的位计数器。

**Q3**：若线路噪声让停止位那一拍采到 0，会发生什么？
**答**：`rx_err` 会拉高一个时钟周期（帧错），`rx_done` 不产生；数据 `rx_data` 仍更新但上游可据 `rx_err` 丢弃这一帧。这是 UART 最基本的差错检测。

---

## 5. 综合实践

把四个模块串起来，完成规格要求的核心实践：**在 50 MHz、115200 波特下例化 `uart_tx` 连续发送字符 `'A'`（0x41），抓 `txd` 波形并验证帧格式与位时序**。

**手算预期值**：

- `BAUD_DIVISOR = floor(50_000_000 / 115200) = 434`，位周期 = 434 × 20 ns = **8680 ns**，整帧 10 位 = **86.8 µs**。
- `'A' = 8'h41 = 8'b0100_0001`，LSB 在先 D0..D7 = `1,0,0,0,0,0,1,0`。
- `txd` 上应观察到序列：`0(起始) 1 0 0 0 0 0 1 0 1(停止)`，每位宽 8680 ns。

**操作步骤**：新建下面这个**示例 testbench**（仓库没有为 `uart_tx.sv` 现成的 tb，本讲按 [uart_tx_rx_shifter_tb.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/uart_tx_rx_shifter_tb.sv) 的风格写一个最小版）：

```systemverilog
// 示例代码：uart_tx_hello_tb.sv —— 最小化验证 uart_tx 发送 'A'
`timescale 1ns / 1ps
module uart_tx_hello_tb;
  logic clk;                                    // 50 MHz 系统时钟
  initial begin clk = 1'b0; forever #10 clk = ~clk; end   // 半周期 10ns → 20ns 周期

  logic nrst;
  initial begin nrst = 1'b0; #25 nrst = 1'b1; end         // 上电复位

  logic [7:0] tx_data;
  logic       tx_start, tx_busy, txd;

  uart_tx #(
    .CLK_HZ( 50_000_000 ),
    .BAUD ( 115200 )            // BAUD_DIVISOR 自动算得 434
  ) dut (
    .clk( clk ), .nrst( nrst ),
    .tx_data( tx_data ), .tx_start( tx_start ),
    .tx_busy( tx_busy ), .txd( txd )
  );

  // 在每个 tx_do_sample 节拍把 txd 压进观察向量，便于核对帧格式
  logic [9:0] frame_seen;
  int         bit_idx;
  always @(posedge clk) begin
    if (dut.tx_do_sample && (tx_busy || bit_idx>0) && bit_idx<10) begin
      frame_seen[bit_idx] <= txd;
      $display("bit[%0d]=%b @%0t", bit_idx, txd, $time);
      bit_idx <= bit_idx + 1;
    end
  end

  initial begin
    $dumpfile("uart_tx_hello_tb.vcd");
    $dumpvars(0, uart_tx_hello_tb);
    tx_data = 8'h00; tx_start = 1'b0; bit_idx = 0; frame_seen = '0;
    @(posedge nrst); #100;

    tx_data = 8'h41;                            // 'A'
    @(posedge clk); tx_start = 1'b1;            // 跨至少一个上升沿
    @(posedge clk); tx_start = 1'b0;

    wait (!tx_busy && bit_idx==10);             // 等整帧 10 位采完
    #500;
    $display("frame_seen = %b (期望 0_10000010_1 等效 LSB 序列)", frame_seen);
    $finish;
  end
endmodule
```

用 iverilog 编译运行（`uart_tx.sv` 无外部依赖，编译这两个文件即可）：

```bash
iverilog -g2012 -o uart_tx_hello.vvp uart_tx.sv uart_tx_hello_tb.sv
vvp uart_tx_hello.vvp
gtkwave uart_tx_hello_tb.vcd     # 查看 txd 波形
```

**需要观察的现象与预期结果**：

1. 打印的 `bit[0]..bit[9]` 依次为 `0,1,0,0,0,0,0,1,0,1`（起始 + LSB 在先的 8 位数据 + 停止）。
2. 波形中相邻位之间的时间差为 **8680 ns**（434 个 20 ns 时钟周期）。
3. `tx_busy` 在 `tx_start` 后立即拉高，并在打出停止位（第 10 个 `tx_do_sample`）的那个沿落下；若把 `tx_start` 改为恒 1，可看到帧与帧之间无空隙的背靠背发送。

> 若本地未装 iverilog/GTKWave，可改用 ModelSim：把上述两个文件加入工程，`vlog -sv uart_tx.sv uart_tx_hello_tb.sv` 后 `vsim uart_tx_hello_tb` 运行，波形结论一致。位周期与帧电平序列属“待本地验证”项，以你工具实测为准。

## 6. 本讲小结

- UART 异步帧 = **起始位(0) + 8 位数据(LSB 在先) + 停止位(1)**，空闲电平为 1；`uart_tx` 用 `{1'b1, tx_data[7:0], 1'b0}` 一次性拼好 10 位帧。
- **波特分频**用一个自由向下计数器产生 `tx_do_sample` 脉冲，周期 = `BAUD_DIVISOR = CLK_HZ/BAUD` 个时钟 = 一个位周期；它是整条发送链的节拍器，且可被多发送器共用。
- 发送靠 `{tx_shifter[9:0], txd} >> 1` 的**整体右移**把帧逐位挤上 `txd`，移位只发生在 `tx_do_sample` 沿，保证每位等宽。
- **`busy` 提前置位/复位**：收到 `tx_start` 当拍置位、打出停止位当拍复位，使停止位期间可预装下一帧，实现全速背靠背连续发送。
- **接收器 `uart_rx`** 复用 `delay`（同步 `rxd`）与 `edge_detect`（找起始位下降沿），在 1.5 位周期后开始**中点采样**，用“标记位”计数 8 位数据，最后用停止位做 `rx_done`/`rx_err` 校验。
- `uart_tx.v` 与 `uart_tx.sv` 逻辑完全等价，只是 Verilog-2001 与 SystemVerilog 写法之别。

## 7. 下一步学习建议

- **协议层往上**：本讲只讲了最小 UART（1 起始 + 8 数据 + 1 停止，无校验）。若要做带奇偶校验、可变起止位的版本，可阅读仓库的 [uart_tx_shifter.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/uart_tx_shifter.sv) / [uart_rx_shifter.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/uart_rx_shifter.sv)，它们把起止位/数据位都参数化了。
- **波形里的字节流**：结合 [uart_debug_printer.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/uart_debug_printer.sv) 学习如何把内部数据通过 UART 打到上位机终端做调试。
- **下一讲 u5-l2（SPI 主机）**：与 UART 的“异步、按波特率自定时”不同，SPI 是**同步**串行（带 SCLK），阅读 `spi_master.sv` 时可对比二者“谁来产生节拍”的根本差异。
- **综合实战 u7-l4**：把本讲的 `uart_tx` 与 `clk_divider`、`debounce_v2`、`edge_detect`、`fifo_single_clock_ram` 串成“按键计数 → FIFO 缓存 → UART 上报”的完整数据通路，本讲是其最后一块拼图。
