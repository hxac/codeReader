# Verilog状态机热身：UART串口收发

## 1. 本讲目标

本讲是全册第一次正式进入 Verilog 源码精读。我们故意挑了一个**与图像拼接主链路无关、却最简单独立**的模块——UART 串口收发器——来做「读 Verilog 状态机」的热身。

读完本讲你应该能够：

- 看懂一个标准 Verilog 模块的端口声明、`parameter` 参数化与 `localparam` 常量；
- 自己手算波特率分频常数 `CYCLE`，理解「时钟频率 ÷ 波特率」的本质；
- 画出三段式状态机（状态寄存器 / 次态逻辑 / 输出逻辑）的结构；
- 解释 UART 接收端为什么需要**下降沿检测**与**位中间采样**；
- 对照 `uart_rx` 和 `uart_tx` 两个模块，理解收发状态机的对称与不对称之处。

这些技能在后续讲义里会反复用到：[mem_burst.v](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_burst.v) 的 DDR3 突发读写、[DynamicSeam.v](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v) 的缝合线查找，本质都是「状态机 + 计数器」，只是状态更多、数据更宽。先把 UART 读透，后面就不怕。

## 2. 前置知识

### 2.1 什么是 UART

UART（Universal Asynchronous Receiver/Transmitter，通用异步收发器）是一种**异步**串行通信协议。「异步」是关键词：发送端和接收端之间**只有一根数据线，没有时钟线**。双方靠事先约定好的「波特率（baud rate）」各自本地计时。

一帧 UART 数据的物理形态（以本项目 8 数据位、无校验、1 停止位为例）：

```
空闲(高) | 起始位(低) | D0 D1 D2 D3 D4 D5 D6 D7 | 停止位(高) | 空闲(高)
   1     |     0      | 8 个数据位(LSB先发)      |     1      |
```

要点：

- **线路空闲时为高电平**（`1`）。
- **起始位**是一个低电平（`0`），它的**下降沿**就是一帧开始的信号——这是接收端唯一的「同步锚点」。
- 8 个**数据位低位先发**（LSB first）。
- **停止位**拉回高电平，标志一帧结束，也为下一帧的下降沿做准备。

### 2.2 为什么异步通信需要「约定波特率」

既然没有时钟线，接收端怎么知道每个位占多长时间？答案是：双方事先约定相同的波特率（每秒传输的位数，如 115200 bps）。接收端用自己的本地时钟数数，数够 `CYCLE` 个时钟周期就认为过了一个数据位。因此接收端必须把本土地时钟周期数换算成「一个数据位对应的时钟周期数」，这就是后面要讲的 `CYCLE`。

### 2.3 Verilog 三段式状态机

本项目的状态机写法接近经典的「三段式」：

| 段 | 所在 `always` 块 | 职责 |
| --- | --- | --- |
| 第 1 段 | 时序 `always@(posedge clk)` | 状态寄存器：`state <= next_state` |
| 第 2 段 | 组合 `always@(*)` | 次态逻辑：根据当前 `state` 和输入算出 `next_state` |
| 第 3 段 | 时序 `always@(posedge clk)` | 输出逻辑：根据 `state` 驱动输出信号 |

> 风格提示：本项目第 2 段组合逻辑里用了非阻塞赋值 `<=`。严格来说组合 `always` 习惯用阻塞 `=`，但在 `always@(*)` 中只要每条路径都给 `next_state` 赋了值，综合结果是一致的。读代码时把它当普通 `case` 真值表即可。

### 2.4 亚稳态与两级同步寄存器

外部串行信号 `rx_pin` 对 FPGA 时钟域是**异步**的，直接拿来用可能踩中「亚稳态」（信号在 `clk` 采样窗口内翻转，寄存器输出一段时间不确定）。通用做法是先让信号穿过两级触发器（`rx_d0`、`rx_d1`）再使用，把亚稳态概率降到极低。这两级寄存器同时也能用来做**边沿检测**——一举两得。

> 关于本项目：u1-l2 已经说明这是一个「按模块收集的源码片段集」，没有顶层集成、没有构建系统，IP 核未收录。所以本讲的实践以**源码阅读 + 手算 + 波形推演**为主，凡涉及上板运行的地方都标注「待本地验证」。

## 3. 本讲源码地图

本讲只涉及两个文件（u1-l2 已确认根目录的 `UART串口通信.v` 是 0 字节空文件，真正代码在同名目录下）：

| 文件 | 行数 | 角色 |
| --- | --- | --- |
| `UART串口通信/uart_rx.v` | 170 | 串口**接收**模块：把 `rx_pin` 上的串行数据拼成 8 位并行字节 |
| `UART串口通信/uart_tx.v` | 161 | 串口**发送**模块：把 8 位并行字节按 UART 帧格式逐位打到 `tx_pin` |

两个模块同源（同为 ALINX 的 meisq 作者 2017 年模板），结构高度对称，非常适合对照阅读。它们在七路拼接系统里属于**独立的调试旁路**（u1-l2 结论），不参与图像主数据流，所以可以单独读懂，不依赖其它模块。

## 4. 核心概念与源码讲解

### 4.1 Verilog 模块骨架、参数化与波特率分频

#### 4.1.1 概念说明

这一小节解决一个贯穿收发两端的共同基础：**怎么把「50 MHz 系统时钟」切分成「115200 bps 的位节拍」**。

两件事要先讲清楚：

1. **模块端口与参数化**：Verilog 用 `parameter` 让模块可被「实例化时改写」地复用。收发模块都暴露 `CLK_FRE`（时钟频率，单位 MHz）和 `BAUD_RATE`（波特率）两个参数，换平台或换波特率时不用改内部逻辑。
2. **波特率分频常数 `CYCLE`**：一个数据位包含多少个系统时钟周期。它是个**派生常量**，用 `localparam` 由两个 `parameter` 算出来，写成

\[
CYCLE = \frac{CLK\_FRE \times 10^{6}}{BAUD\_RATE}
\]

以默认值 `CLK_FRE=50`、`BAUD_RATE=115200` 为例：\( CYCLE = 50 \times 10^6 / 115200 \approx 434.03 \)，整数除法得 **434**。也就是说，每数 434 个 50 MHz 时钟周期（434 × 20 ns = 8.68 µs）就过了一个数据位，正好对上 1/115200 s ≈ 8.68 µs。

#### 4.1.2 核心流程

波特率分频的通用思路（伪代码）：

```
每个时钟上升沿:
    if (处于某一位的计数中 且 还没数到 CYCLE):
        cycle_cnt <= cycle_cnt + 1
    else if (数到 CYCLE-1 或 即将切换状态):
        cycle_cnt <= 0          # 一位结束，归零
        (推进 bit_cnt 或 切换状态)
```

关键点：`cycle_cnt` 是一个 16 位计数器，它在**位内部**线性递增、在**位边界**归零。所有「位节拍」都由它驱动：发送端用它决定何时移到下一位，接收端用它决定何时采样。

#### 4.1.3 源码精读

**模块声明与参数**（两文件几乎一致，以 rx 为例）——声明了端口、两个可改写参数 `CLK_FRE`/`BAUD_RATE`：

[uart_rx.v:29-41](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/UART串口通信/uart_rx.v#L29-L41)：定义 `uart_rx` 模块，`parameter CLK_FRE = 50`、`parameter BAUD_RATE = 115200`，端口含 `clk`/`rst_n`（低有效异步复位）、输出 `rx_data[7:0]` 与 `rx_data_valid`、输入 `rx_data_ready` 与串行输入 `rx_pin`。

**派生常数 CYCLE**——`localparam` 由参数算出，换参数即自动重算，无需手改：

[uart_rx.v:43](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/UART串口通信/uart_rx.v#L43) 与 [uart_tx.v:43](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/UART串口通信/uart_tx.v#L43)：`localparam CYCLE = CLK_FRE * 1000000 / BAUD_RATE;`——这就是波特率分频的全部数学。

**状态编码**——用 `localparam` 给每个状态起名字，避免代码里到处出现魔数：

[uart_rx.v:45-49](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/UART串口通信/uart_rx.v#L45-L49)：`S_IDLE=1 / S_START=2 / S_REC_BYTE=3 / S_STOP=4 / S_DATA=5`（接收端多一个 `S_DATA` 用于和下游握手）；

[uart_tx.v:45-48](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/UART串口通信/uart_tx.v#L45-L48)：`S_IDLE=1 / S_START=2 / S_SEND_BYTE=3 / S_STOP=4`（发送端只有 4 个状态）。

**位计数器 `cycle_cnt`**（以 rx 为例，tx 完全同构）——这就是「波特率分频」在硬件里的落地：

[uart_rx.v:152-160](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/UART串口通信/uart_rx.v#L152-L160)：复位归零；当处于 `S_REC_BYTE` 且 `cycle_cnt == CYCLE-1`（一位数满），或 `next_state != state`（即将切状态）时归零；否则每拍 `+1`。

> 对照：发送端的同款计数器在 [uart_tx.v:134-142](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/UART串口通信/uart_tx.v#L134-L142)，逻辑一字不差，只是把 `S_REC_BYTE` 换成了 `S_SEND_BYTE`。

#### 4.1.4 代码实践

**实践目标**：验证 `CYCLE` 公式，体会「换波特率 = 改一个参数」。

**操作步骤**：

1. 打开 `uart_rx.v` 第 43 行的 `localparam CYCLE = CLK_FRE * 1000000 / BAUD_RATE;`。
2. 默认 `CLK_FRE=50`、`BAUD_RATE=115200`，手算 `CYCLE`。
3. 把波特率想象成 9600（不真的改源码，先心算），再算一次 `CYCLE`。

**需要观察的现象**：两个 `CYCLE` 值应该相差约 12 倍（115200 / 9600 = 12），因为波特率降到 1/12，每个位要数 12 倍的周期。

**预期结果**：

| BAUD_RATE | CYCLE（整数除法） | 每位实际时长 = CYCLE × 20 ns | 理论位时长 1/波特率 |
| --- | --- | --- | --- |
| 115200 | 50e6/115200 = **434** | 8.68 µs | 8.6806 µs |
| 9600 | 50e6/9600 = **5208** | 104.16 µs | 104.167 µs |

（完整改参数与误差分析见第 5 节综合实践。）

#### 4.1.5 小练习与答案

**练习 1**：如果系统时钟不是 50 MHz 而是 100 MHz，`CYCLE` 会变成多少（仍用 115200 波特率）？

**参考答案**：\( CYCLE = 100 \times 10^6 / 115200 \approx 868.05 \)，整数除法得 **868**。这正是 `parameter` 化的好处——换平台只改 `CLK_FRE`，`CYCLE` 自动更新。

**练习 2**：`CYCLE` 用 `localparam` 而不是 `parameter` 声明，为什么？

**参考答案**：`CYCLE` 是由 `CLK_FRE`、`BAUD_RATE` **派生**出来的，使用者不应该也不需要在外部覆盖它；`localparam` 正是「模块内部常量、不可被实例化时改写」的声明方式，能防止误用。

---

### 4.2 uart_rx：边沿检测与接收状态机

#### 4.2.1 概念说明

接收端要解决的核心难题：**没有时钟线，怎么知道一帧什么时候开始、每一位在哪个时刻最稳？**

`uart_rx` 用两个设计回答它：

1. **下降沿检测 `rx_negedge`**：UART 线路空闲为高，起始位把它拉低。捕获这个「高→低」跳变，就抓住了一帧的起点，也同步了本地计时的相位。
2. **位中间采样**：在每个数据位的**正中间**读取 `rx_pin`。位中间离两侧跳变沿最远，电平最稳定，即使收发双方时钟有微小偏差也不会采错。

接收状态机有 5 个状态（比发送多一个 `S_DATA` 用来和下游做 `rx_data_ready` 握手）：

```
S_IDLE --(rx_negedge)--> S_START --(数满 CYCLE)--> S_REC_BYTE
   ^                                                       |
   |                                                       (采满 8 位)
  S_DATA <--(数满 CYCLE/2)-- S_STOP <----------------------+
   |
 (rx_data_ready)
   |
   v
 S_IDLE
```

#### 4.2.2 核心流程

```
S_IDLE:    等待 rx_negedge（起始位下降沿）→ S_START
S_START:   数满一个 CYCLE，"吃掉"起始位       → S_REC_BYTE
S_REC_BYTE:循环 8 次：
             每次数满一个 CYCLE；
             在 cycle_cnt == CYCLE/2-1（位中间）把 rx_pin 采进 rx_bits[bit_cnt]；
             bit_cnt 从 0 数到 7           → S_STOP
S_STOP:    数满 CYCLE/2（半个停止位，提前结束以便接下一帧）
           把 rx_bits 锁进 rx_data，拉高 rx_data_valid → S_DATA
S_DATA:    等下游 rx_data_ready 应答         → S_IDLE
```

两个细节值得记：

- **`S_START` 用整周期 `CYCLE` 而非半周期**：起始位的下降沿被两级同步寄存器延迟约 1~2 拍捕获，整周期恰好把这点延迟吸收，使后续 8 次采样都落在各数据位的中点附近。
- **`S_STOP` 只等 `CYCLE/2`**：注释写得很明白——`to avoid missing the next byte receiver`，即只等半个停止位就回到就绪态，避免因为本帧拖太久而错过下一帧的起始下降沿。

#### 4.2.3 源码精读

**两级同步 + 下降沿检测**——这是本模块最值得学的硬件技巧：

[uart_rx.v:53-60](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/UART串口通信/uart_rx.v#L53-L60)：声明 `rx_d0`/`rx_d1` 两级延迟寄存器和 `rx_negedge` 线网，并写 `assign rx_negedge = rx_d1 && ~rx_d0;`——当「上一拍(`rx_d1`)为高、当前拍(`rx_d0`)为低」时为真，正好是一次下降沿。

[uart_rx.v:62-74](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/UART串口通信/uart_rx.v#L62-L74)：每个时钟把 `rx_pin` 打一拍进 `rx_d0`、再把 `rx_d0` 打一拍进 `rx_d1`。这两拍既做**亚稳态同步**，又为边沿检测提供了「前一拍/前两拍」的样本。

**第 1 段：状态寄存器**——

[uart_rx.v:77-83](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/UART串口通信/uart_rx.v#L77-L83)：复位进 `S_IDLE`，否则 `state <= next_state`。

**第 2 段：次态逻辑（状态转移表）**——

[uart_rx.v:85-116](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/UART串口通信/uart_rx.v#L85-L116)：用 `case(state)` 描述全部转移，对应 4.2.2 的流程；注意 `S_REC_BYTE` 的出口条件是 `cycle_cnt == CYCLE-1 && bit_cnt == 3'd7`（采满 8 位），`S_STOP` 的出口条件是 `cycle_cnt == CYCLE/2-1`（半个停止位）。

**第 3 段：输出与数据通路**——这几条 `always` 都属于输出逻辑：

[uart_rx.v:118-126](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/UART串口通信/uart_rx.v#L118-L126)：在 `S_STOP → S_DATA` 切换瞬间拉高 `rx_data_valid`，在下游应答后拉低——典型的 valid 握手。

[uart_rx.v:128-134](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/UART串口通信/uart_rx.v#L128-L134)：同一切换瞬间把移位缓冲 `rx_bits` 锁进输出 `rx_data`。

[uart_rx.v:136-149](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/UART串口通信/uart_rx.v#L136-L149)：`bit_cnt` 在 `S_REC_BYTE` 内每满一个 CYCLE 加 1（0→7），离开该状态时归零。

[uart_rx.v:162-170](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/UART串口通信/uart_rx.v#L162-L170)：**采样核心**——`if(state == S_REC_BYTE && cycle_cnt == CYCLE/2-1) rx_bits[bit_cnt] <= rx_pin;`，这就是「在每位正中间采一位」。

#### 4.2.4 代码实践

**实践目标**：用波形推演一次完整的接收过程，吃透 `rx_negedge` 与「位中间采样」的配合。

**操作步骤**：

1. 假设收到一帧数据 `0x41`（即 `8'b0100_0001`，LSB 先发，所以线上依次是 `1,0,0,0,0,0,1,0`）。
2. 在纸上画一行 `rx_pin` 的电平：`…1,1 | 0(起始) | 1,0,0,0,0,0,1,0 | 1(停止) | 1…`。
3. 标出 `rx_negedge` 在哪里拉高一拍（起始位 `1→0` 处）。
4. 标出 8 个采样点：每个数据位的 `cycle_cnt == CYCLE/2-1` 处，按 `bit_cnt=0..7` 把电平填进 `rx_bits`。
5. 验证 `rx_bits` 重组后是否等于 `8'b0100_0001`。

**需要观察的现象**：`rx_negedge` 只在起始位那一拍为高；8 次采样分别落在 8 个数据位的中央；最终 `rx_data` 在 `S_STOP` 末尾被锁成 `0x41`。

**预期结果**：`rx_bits` 按位收集得到 `bit0=1, bit1=0, …, bit6=1, bit7=0`，即 `8'b0100_0001 = 0x41`，与发送内容一致。（这是纸面推演；若要仿真验证需自写 testbench，**待本地验证**。）

#### 4.2.5 小练习与答案

**练习 1**：把 `assign rx_negedge = rx_d1 && ~rx_d0;` 改成 `assign rx_posedge = ~rx_d1 && rx_d0;` 检测的是什么？

**参考答案**：检测 `rx_pin` 的**上升沿**（低→高跳变）。UART 用下降沿找起始位，所以这里用 `rx_negedge`；但同一个「两级延迟 + 异或式比较」模板可同时支持上升沿/下降沿/双沿检测。

**练习 2**：为什么采样点选 `cycle_cnt == CYCLE/2-1` 而不是 `CYCLE-1`（位末尾）？

**参考答案**：位中间离前后两个跳变沿各距 `CYCLE/2`，是电平最稳、对时钟偏差最不敏感的位置；位末尾离下一位的跳变沿太近，收发时钟的累积偏差极易导致采到错误电平。

**练习 3**：`S_STOP` 为什么只等 `CYCLE/2` 而不是一个完整 `CYCLE`？

**参考答案**：注释明说 `to avoid missing the next byte receiver`——只等半个停止位就提前回到就绪态，给捕获下一帧的起始下降沿留足裕量，防止连续帧之间因本帧收尾太慢而丢帧。

---

### 4.3 uart_tx：发送状态机与握手

#### 4.3.1 概念说明

发送端比接收端**简单**，因为它自己是「节拍源」——按本地时钟主动产生波形即可，不需要检测对端边沿、不需要位中间采样。它的核心是一个**输出多路选择器**：根据当前状态，把 `tx_pin` 拉成起始位（`0`）、某个数据位（`tx_data_latch[bit_cnt]`）或停止位/空闲（`1`）。

发送端有 4 个状态（没有接收端的 `S_DATA`，握手改成 `tx_data_valid`/`tx_data_ready` 反向）：

```
S_IDLE --(tx_data_valid=1)--> S_START --(数满 CYCLE)--> S_SEND_BYTE
   ^                                                            |
   |                                                            (发满 8 位)
    -------------- S_STOP <------------------------------------+
                (数满 CYCLE)
```

#### 4.3.2 核心流程

```
S_IDLE:      tx_pin=1(空闲)。上游置 tx_data_valid=1 表示有字节要发
             → 把 tx_data 锁进 tx_data_latch → S_START
S_START:     tx_pin=0(起始位)，数满 CYCLE → S_SEND_BYTE
S_SEND_BYTE: 循环 8 次：tx_pin = tx_data_latch[bit_cnt]（LSB 先发）
             bit_cnt 0→7，每位数满 CYCLE → S_STOP
S_STOP:      tx_pin=1(停止位)，数满 CYCLE → S_IDLE
```

握手信号含义（与接收端方向相反，注意别混淆）：

| 信号 | 方向 | 含义 |
| --- | --- | --- |
| `tx_data` | 上游→tx | 要发送的字节 |
| `tx_data_valid` | 上游→tx | 「我给你了一个有效字节」 |
| `tx_data_ready` | tx→上游 | 「我准备好了 / 已发完，可以再给」 |

#### 4.3.3 源码精读

**模块声明**——注意端口方向与 rx 对称反过来：`tx_data`/`tx_data_valid` 是输入，`tx_data_ready` 是输出：

[uart_tx.v:29-41](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/UART串口通信/uart_tx.v#L29-L41)。

**输出线 `tx_pin`**——`tx_reg` 是真正的驱动寄存器，`tx_pin` 只是它的别名：

[uart_tx.v:55](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/UART串口通信/uart_tx.v#L55)：`assign tx_pin = tx_reg;`。

**第 1 段：状态寄存器**——

[uart_tx.v:56-62](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/UART串口通信/uart_tx.v#L56-L62)：与 rx 同构。

**第 2 段：次态逻辑**——触发条件是 `tx_data_valid`，出口条件 `cycle_cnt == CYCLE-1 && bit_cnt == 3'd7`：

[uart_tx.v:64-90](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/UART串口通信/uart_tx.v#L64-L90)。

**第 3 段：握手与输出**——

[uart_tx.v:91-104](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/UART串口通信/uart_tx.v#L91-L104)：`tx_data_ready` 的产生——`S_IDLE` 内若 `tx_data_valid` 则忙（拉低），否则就绪（拉高）；`S_STOP` 数满时再次拉高表示「发完了」。

[uart_tx.v:107-116](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/UART串口通信/uart_tx.v#L107-L116)：在 `S_IDLE` 且 `tx_data_valid` 时把输入 `tx_data` 锁进 `tx_data_latch`，保证后续逐位发送期间输入可以变化而不影响本帧。

**输出多路选择器（本模块最核心的一段）**——

[uart_tx.v:144-159](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/UART串口通信/uart_tx.v#L144-L159)：复位时 `tx_reg <= 1`（空闲高）；运行时按状态选值——`S_IDLE`/`S_STOP` 输出 `1`，`S_START` 输出 `0`，`S_SEND_BYTE` 输出 `tx_data_latch[bit_cnt]`（LSB 先发）。这一段直接对应 4.3.2 的波形生成。

> 这里没有用移位寄存器，而是用 `tx_data_latch[bit_cnt]` **按位索引**逐位取出，配 `bit_cnt` 计数器实现「逐位发送」，效果等价但更直观。

#### 4.3.4 代码实践

**实践目标**：手画一帧发送波形，确认「状态 → `tx_pin` 电平」的对应关系。

**操作步骤**：

1. 设要发送 `0x41`（`8'b0100_0001`）。
2. 画一条时间轴，按 `S_IDLE → S_START → S_SEND_BYTE(×8) → S_STOP → S_IDLE` 切段，每段长度为一个 `CYCLE`。
3. 对照 [uart_tx.v:144-159](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/UART串口通信/uart_tx.v#L144-L159)，逐段填 `tx_pin` 电平。
4. 在 `S_SEND_BYTE` 段，按 `bit_cnt=0..7` 填 `tx_data_latch[0..7]`，即线上序列 `1,0,0,0,0,0,1,0`。

**需要观察的现象**：完整波形应为 `1(空闲) | 0(起始) | 1,0,0,0,0,0,1,0(数据,LSB先) | 1(停止) | 1(空闲)`，与 4.2.4 里接收端假设的输入帧**完全一致**——这正好说明收发两端是一对配套协议。

**预期结果**：把本节画出的发送波形，当成 4.2.4 接收端的 `rx_pin` 输入，二者严丝合缝。这就是「自收自发回环（loopback）」能工作的原理。（上板回环验证 **待本地验证**。）

#### 4.3.5 小练习与答案

**练习 1**：为什么发送端不需要 `rx_negedge` 那样的边沿检测？

**参考答案**：发送端是节拍源，主动按本地 `CYCLE` 产生每一位，不需要和外部信号对齐相位；而接收端面对的是异步到来的外部串行数据，必须靠下降沿找到帧起点。这正是「发易收难」的根源。

**练习 2**：`tx_data_latch` 的作用是什么？直接用 `tx_data[bit_cnt]` 行不行？

**参考答案**：`tx_data_latch` 在帧开始时把输入字节**快照**下来，保证逐位发送期间即便上游改了 `tx_data`，本帧内容也不变。若直接用 `tx_data[bit_cnt]`，发送中途输入变化会导致同一帧输出错乱。

**练习 3**：收发两端的握手方向有何不同？

**参考答案**：接收端 `rx_data_valid`（模块输出，告诉下游「有数据」）/ `rx_data_ready`（下游输入，「我准备好了」）；发送端 `tx_data_valid`（上游输入，「有字节要发」）/ `tx_data_ready`（模块输出，「我能接活了」）。valid/ready 的**产生方正好对调**，这是 valid-ready 握手在不同角色下的自然体现。

---

## 5. 综合实践

把本讲三块内容串起来，完成下面这个贯穿性小任务（即本讲指定的实践任务）。

### 任务 A：把波特率从 115200 改成 9600 并重新计算 CYCLE

1. 找到 [uart_rx.v:31-32](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/UART串口通信/uart_rx.v#L31-L32) 的 `parameter BAUD_RATE = 115200`。
2. 方案一（改默认值）：直接改成 `parameter BAUD_RATE = 9600`，则 [uart_rx.v:43](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/UART串口通信/uart_rx.v#L43) 的 `CYCLE` 自动变为 `50*1000000/9600 = 5208`（整数除法）。
3. 方案二（不改源码，实例化时覆盖）：在例化处写
   ```verilog
   uart_rx #(.CLK_FRE(50), .BAUD_RATE(9600)) u_rx ( /* 端口连接 */ );
   ```
   这正是 `parameter` 化的价值——同一份 RTL 适配多个波特率。
4. 手算并填表验证：每位时长 = `CYCLE × 20 ns` 应接近 `1/9600 s = 104.167 µs`；采样点 `CYCLE/2-1 = 2603`。

> 注意：本仓库没有构建系统（u1-l2 结论），所以这一步是「源码修改 + 手算」，不要求真的综合。若要实测，需要自行搭建 Vivado 工程并连接 USB-串口，**待本地验证**。

### 任务 B：说明 `rx_negedge`（下降沿检测）在接收状态机中的作用

结合 [uart_rx.v:60](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/UART串口通信/uart_rx.v#L60) 与 [uart_rx.v:62-74](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/UART串口通信/uart_rx.v#L62-L74)、[uart_rx.v:88-92](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/UART串口通信/uart_rx.v#L88-L92)，写一段 150 字左右的说明，要点应覆盖：

- UART 线路空闲为高、起始位为低，所以「下降沿 = 一帧的起点」；
- UART 是异步协议（无时钟线），接收端必须靠这个下降沿**同步本地计时的相位**，否则不知道从哪一拍开始数 `CYCLE`；
- `rx_negedge = rx_d1 && ~rx_d0` 由两级同步寄存器派生，既检测跳变又顺便抑制了外部异步信号的亚稳态；
- 在状态机里，`S_IDLE` 正是靠 `if(rx_negedge) → S_START` 才被「唤醒」，没有它接收端会永远停在空闲态。

### 任务 C（进阶）：回环一致性核对

把 4.3.4 画出的 `0x41` 发送波形，当作 4.2.4 接收端的输入，核对：发送端 `S_START` 的下降沿能否触发接收端的 `rx_negedge`？8 个数据位的电平能否被接收端在位中间正确采样回 `0x41`？这一步把「发」和「收」真正连成一个闭环。

## 6. 本讲小结

- **参数化与分频**：`uart_rx`/`uart_tx` 用 `parameter CLK_FRE/BAUD_RATE` 暴露可配置项，用 `localparam CYCLE = CLK_FRE*1e6/BAUD_RATE` 派生出位节拍；换波特率只改一个参数。
- **三段式状态机**：第 1 段状态寄存器、第 2 段次态组合逻辑、第 3 段输出逻辑——这是读后续所有状态机（`mem_burst`、`DynamicSeam`）的通用框架。
- **接收靠下降沿 + 位中间采样**：`rx_negedge = rx_d1 && ~rx_d0` 抓起始位并同步相位；`cycle_cnt == CYCLE/2-1` 在每位正中间采样，兼顾稳定与时钟偏差容限。
- **发送靠输出多路选择器**：`tx_reg` 按 `S_IDLE/S_START/S_SEND_BYTE/S_STOP` 选择 `1 / 0 / tx_data_latch[bit_cnt] / 1`，主动生成波形，比接收简单。
- **valid-ready 握手方向对调**：接收端 valid 由模块输出、ready 由下游输入；发送端反过来——这是后续跨模块数据通路（如 DDR3 突发读写）会反复出现的握手范式。
- **读 RTL 的方法**：先看端口和参数定「接口契约」，再看 `CYCLE`/状态编码定「节拍与格局」，最后逐段读三段式 `always`——这套方法直接迁移到后续讲义。

## 7. 下一步学习建议

本讲练完「读状态机」的基本功后，建议按以下顺序继续：

1. **进入算法主线**：u2-l1 从 [圆柱面投影.cpp](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.cpp) 的 `main` 函数看 OpenCV 拼接流水线全景，先建立「软件参考实现」的整体认知，再看后续如何把它移植到硬件。
2. **对照硬件状态机**：当你读到 u3-l1 的 [mem_burst.v](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_burst.v) 时，回忆本讲的 `S_IDLE/S_START/…` 与 `cycle_cnt`——你会发现 DDR3 突发读写用的是同一套「状态机 + 计数器 + 握手」骨架，只是状态更多、握手对象变成了 MIG 的 `app_*` 接口。
3. **延伸阅读**（项目外）：若想补 Verilog 状态机与亚稳态基础，可阅读 Xilinx UG949（UltraFast 设计方法论）中关于「时钟域交叉 CDC」与「两级同步器」的章节，理解本讲 `rx_d0/rx_d1` 之所以必要的底层原因。
