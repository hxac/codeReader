# UART 串口收发

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 UART 异步串行通信的帧格式（起始位、8 个数据位、停止位），并解释为什么线路空闲时是高电平。
- 理解「没有共享时钟线」的两台设备如何靠**过采样（oversampling）**在比特中央对齐采样。
- 读懂 `uart_baud.sv` 如何用一个 DDS 累加器，从 100 MHz 时钟**同时**产生 1× 波特节拍与 16× 过采样节拍。
- 读懂 `uart_tx.sv` 的 `IDLE/START/DATA/STOP` 状态机如何把一个并行字节按比特移位成一帧串行数据。
- 读懂 `uart_rx.sv` 如何用两级触发器同步器抗亚稳态、用 16× 过采样与三点采样定位每一个比特。
- 在仿真中观察一个字符（例如 `'A'`）的完整帧时序，并手算给定时钟/波特率下的分频系数。

## 2. 前置知识

### 2.1 串行、异步、UART 是什么

UART（Universal Asynchronous Receiver-Transmitter，通用异步收发器）是最朴素的串口通信方式：发送方（TX）和接收方（RX）之间**只有一根数据线各自一个方向**，外加一根公共地线，**没有共享的时钟线**。这与 SPI/I2c 这种「带时钟线的同步通信」形成鲜明对比。

既然没有时钟线，收发双方怎么知道每一位数据从哪里开始、到哪里结束？答案是：双方事先**约定一个相同的速率——波特率（baud rate）**，即每秒传输的比特数。常见的 9600 表示每秒 9600 个比特。发送方按这个节拍把比特逐个推到线上，接收方按同样的节拍去读取。

### 2.2 帧格式：8N1

UART 最常用的配置叫 **8N1**，含义是：**8** 个数据位、**N**o parity（无校验位）、**1** 个停止位。一帧的结构如下：

```
空闲(高) | 起始位(0) | D0 D1 D2 D3 D4 D5 D6 D7 | 停止位(1) | 空闲(高)
```

要点有三：

1. **线路空闲为高电平**。这样接收方一旦看到线上从高变低，就知道「有数据要来了」——这个下降沿就是起始位。
2. **起始位是 0**，持续一个比特周期；**停止位是 1**，也持续一个比特周期。停止位把线路拉回高电平，保证下一帧的起始位又能产生一个干净的下降沿。
3. **数据位低位先发（LSB first）**：D0 先发，D7 最后发。本讲的 `uart_tx` 严格遵循这一点。

所以一帧 8N1 共 10 个比特周期：1 个起始位 + 8 个数据位 + 1 个停止位。

### 2.3 为什么需要过采样

异步通信最大的难题是：接收方的本地时钟和发送方并不同源，两者频率不可能完全一致。如果接收方「天真地」在估计的比特边界处采样一次，频率误差会让采样点逐比特漂移，几比特之后就漂出了正确区间。

解决办法是**过采样（oversampling）**：接收方用一个远高于波特率的本地时钟（经典做法是 16×）去数拍子。检测到起始位下降沿后，从比特中央采样。这样即使收发时钟有百分之几的偏差，10 个比特之内采样点仍停留在有效区间内。本讲的 `uart_rx` 正是用 16× 过采样。

### 2.4 承接前两讲

本讲是 Unit 5（projf 库基础模块）的一篇，与前面紧密衔接：

- **承接 u5-l1（Verilog 库总览与 SystemVerilog 风格）**：三个 UART 模块都使用 projf 库那套精简的 SystemVerilog 子集——`logic`、`enum`、`always_ff`、`always_comb`、`default_nettype none`。读源码时你会反复看到它们。
- **承接 u5-l2（跨时钟域同步器）**：`uart_rx` 的第一件事就是把外部进来的 `data_in` 用**两级触发器同步器**打两拍，正是 u5-l2 讲过的抗亚稳态手法——因为外部串口信号相对于 FPGA 时钟是完全异步的。

## 3. 本讲源码地图

本讲涉及的源码全部位于 `ThreePart/projf-explore/lib/uart/` 下：

| 文件 | 作用 |
| --- | --- |
| [uart_baud.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/uart/uart_baud.sv) | 波特率发生器：从系统时钟派生 1× 波特节拍 `stb_baud` 与 16× 过采样节拍 `stb_sample` |
| [uart_tx.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/uart/uart_tx.sv) | 发送器：把并行字节按 8N1 帧格式串行移位输出 |
| [uart_rx.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/uart/uart_rx.sv) | 接收器：用过采样与三点采样还原出并行字节 |
| [README.md](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/uart/README.md) | 模块清单与说明。作者自述「These designs are not polished」，且官方**尚未提供 testbench** |
| [examples/top_uart.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/uart/examples/top_uart.sv) | 回显（echo）示例顶层：把收到的字符原样发回，9600 波特 8N1 |

> 说明：「发送/接收」均以 **FPGA 的视角**命名：`uart_tx` 是「从 FPGA 发出」，`uart_rx` 是「收到 FPGA 里」（见 README 末尾备注）。

## 4. 核心概念与源码讲解

### 4.1 波特率发生器：uart_baud

#### 4.1.1 概念说明

`uart_baud` 要解决的问题是：FPGA 上通常只有一个固定频率的系统时钟（本例是 100 MHz），但 UART 需要的是 9600 Hz 的慢节拍，以及 16× 即 153600 Hz 的过采样节拍。怎么从 100 MHz 产生这两个频率？

最直观的想法是**整数分频**：数到 N 就翻转一次。但 \(100\,000\,000 / 153\,600 = 651.04 \)，**不是整数**。整数分频无法精确得到 153600 Hz，累计误差会破坏 UART 时序。

工程上更通用的做法是 **DDS（Direct Digital Synthesis，直接数字频率合成）累加器**，也叫 NCO：用一个固定宽度的累加器，每个时钟周期加上一个增量 `CNT_INC`；每当累加器溢出，就产生一个脉冲。输出频率为

\[ f_{\text{out}} = f_{\text{clk}} \cdot \frac{\text{CNT\_INC}}{2^{N}} \]

反过来，给定想要的频率，增量为

\[ \text{CNT\_INC} = \operatorname{round}\!\left( 2^{N} \cdot \frac{f_{\text{out}}}{f_{\text{clk}}} \right) \]

它的妙处在于：增量可以是任意整数，能逼近**任意分数分频比**，输出频率在长时间上精确，短期抖动被限制在一个时钟周期内。这正是 `uart_baud` 采用的方法。

#### 4.1.2 核心流程

`uart_baud` 内部其实有**两个独立的累加器**，但用**同一个增量** `CNT_INC=25770`，靠**不同的位宽**产生两个频率：

1. **24 位累加器** `cnt_16x`：溢出产生 `stb_sample`（16× 过采样节拍）。
   \[ f_{\text{sample}} = 100\,\text{MHz} \times 25770 / 2^{24} \approx 153\,600\,\text{Hz} \]
2. **28 位累加器** `cnt`：溢出产生 `stb_baud`（1× 波特节拍）。
   \[ f_{\text{baud}} = 100\,\text{MHz} \times 25770 / 2^{28} \approx 9\,600\,\text{Hz} \]

关键观察：\( 2^{28} / 2^{24} = 16 \)。所以 28 位累加器溢出一次，恰好对应 24 位累加器溢出 16 次——也就是说 **`stb_baud` 严格等于每 16 个 `stb_sample` 出现一次**，两者天然相位锁定。这正合 16× 过采样的需要，且只需要维护一个增量常数。

源码顶部的注释直接给出了这套推导：

> `// baud generator: 100 MHz -> 153,600 (16 x 9,600)`
> `// 100 MHz / 153,600 = 651.04; 2^24/651.04 = 25,770`

#### 4.1.3 源码精读

模块端口只有时钟、复位和两个节拍输出：

[ThreePart/projf-explore/lib/uart/uart_baud.sv:L11-L19](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/uart/uart_baud.sv#L11-L19) —— 声明参数 `CNT_W=24`（累加器位宽基准）、`CNT_INC=25770`（增量），以及 `stb_baud`（波特节拍）与 `stb_sample`（过采样节拍）两个输出。这两个参数都是可覆盖的，换时钟或换波特率时只改它们即可。

产生 `stb_baud` 的 28 位累加器：

```verilog
logic [CNT_W+3:0] cnt;                 // CNT_W=24 → [27:0]，共 28 位
always_ff @(posedge clk) begin
    {stb_baud, cnt} <= cnt + {4'b0000, CNT_INC};  // 进位溢出即 stb_baud
    ...
end
```

[ThreePart/projf-explore/lib/uart/uart_baud.sv:L21-L31](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/uart/uart_baud.sv#L21-L31) —— `{stb_baud, cnt}` 是 29 位拼接，把 28 位加法的**进位位**单独放到 `stb_baud`。于是每次累加器超过 2²⁸ 溢出，`stb_baud` 就拉高一拍——这就是一个波特节拍。复位时清零。

产生 `stb_sample` 的 24 位累加器，结构完全对称，只是位宽少 4 位：

```verilog
logic [CNT_W-1:0] cnt_16x;             // 24 位
always_ff @(posedge clk) begin
    {stb_sample, cnt_16x} <= cnt_16x + CNT_INC;  // 进位溢出即 stb_sample
    ...
end
```

[ThreePart/projf-explore/lib/uart/uart_baud.sv:L33-L40](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/uart/uart_baud.sv#L33-L40) —— 同样的「进位即脉冲」套路，只是累加器只有 24 位，溢出频率是 28 位的 16 倍。

#### 4.1.4 代码实践：手算分频系数

**实践目标**：验证仓库里 `CNT_INC=25770` 这个数字是怎么来的，并体会为什么不能用整数分频。

**操作步骤**：

1. 给定参数：\( f_{\text{clk}} = 100\,\text{MHz} \)、目标波特率 \( 9600 \)、过采样倍数 \( 16 \)。
2. 算出过采样频率：\( f_{\text{sample}} = 16 \times 9600 = 153\,600\,\text{Hz} \)。
3. 用 24 位累加器（\( N=24 \)）反推增量：
   \[ \text{CNT\_INC} = \operatorname{round}\!\left( 2^{24} \times \frac{153\,600}{100\,000\,000} \right) = \operatorname{round}(25\,769.8) = 25\,770 \]
4. 用同一个增量算 28 位累加器输出的波特频率：
   \[ f_{\text{baud}} = \frac{100\,000\,000 \times 25\,770}{2^{28}} \approx 9\,603\,\text{Hz} \approx 9\,600 \]

**需要观察的现象**：

- 步骤 3 算出的 `25770` 与源码 `CNT_INC` **完全一致**。
- 若尝试整数分频，\( 100\,000\,000 / 153\,600 = 651.04 \) 不是整数——这就是为什么必须用 DDS 累加器。
- 由于 \( 2^{28}/2^{24}=16 \)，两个节拍严格 16:1，无需额外同步。

**预期结果**：`CNT_INC=25770`，`stb_sample` ≈ 153600 Hz，`stb_baud` ≈ 9600 Hz，二者相位锁定。

**待本地验证**：以上为纯手算，建议你用计算器或一段小程序再核一遍。

#### 4.1.5 小练习与答案

**练习 1**：若系统时钟改为 50 MHz，要得到同样的 9600 波特、16× 过采样，`CNT_INC` 应取多少？

**参考答案**：\( f_{\text{sample}}=153\,600 \)，\( \text{CNT\_INC}=\operatorname{round}(2^{24}\times 153\,600/50\,000\,000)=\operatorname{round}(51\,539.6)=51\,540 \)。（注意：此时若仍想用同增量驱动 28 位累加器得到 9600，关系依旧成立，因为位宽差仍是 16 倍。）

**练习 2**：为什么 `uart_baud` 不直接用一个 651 分频的计数器？

**参考答案**：因为 \( 100\,\text{MHz}/153\,600=651.04 \) 不是整数，整数分频会引入累积频率误差，破坏 UART 对波特率精度的要求；DDS 累加器用增量逼近分数比，长期频率精确，抖动仅限一个时钟周期。

---

### 4.2 发送器：uart_tx

#### 4.2.1 概念说明

`uart_tx` 的任务是：拿到一个 8 位并行字节后，按 8N1 帧格式把它**串行**地推到 `data_out` 线上。它需要：

- 在 `tx_start` 被拉高时开始一帧发送；
- 按波特节拍 `stb_baud`（每拍一个比特）依次输出起始位、D0..D7、停止位；
- 发送期间用 `tx_busy` 告诉外部「我正忙，别动数据」；
- 用 `tx_next`（在停止位时为高）提示「现在更新 `data_in` 是安全的」。

它不需要关心过采样，只用 1× 的 `stb_baud` 即可——发送方是自己定节奏，不存在对齐问题。

#### 4.2.2 核心流程

发送用经典的四状态有限状态机，状态切换**只在 `stb_baud` 为高的那一拍**发生（被节拍门控）：

```
        tx_start=1
IDLE ─────────────► START ─► DATA ──(8 比特后)──► STOP ─► IDLE
(数据线=1)         (输出0)   (逐位输出D0..D7)    (输出1)
```

- **IDLE**：数据线保持高（空闲）。`tx_start` 一拉高，下一拍进 START。
- **START**：`data_out=0`（起始位），下一拍进 DATA。
- **DATA**：输出 `data_in[data_idx]`，`data_idx` 从 0 自增到 7；到第 7 位后下一拍进 STOP。低位先发。
- **STOP**：`data_out` 回到默认的 1（停止位），下一拍回 IDLE。

整帧正好消耗 10 个 `stb_baud` 节拍。

#### 4.2.3 源码精读

端口定义，注意输出除串行数据外还有两个状态信号：

[ThreePart/projf-explore/lib/uart/uart_tx.sv:L8-L17](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/uart/uart_tx.sv#L8-L17) —— 输入 `stb_baud`（节拍）、`tx_start`（触发）、`data_in[7:0]`（待发字节）；输出 `data_out`（串行线）、`tx_busy`、`tx_next`。

状态机用 `enum` 声明（SystemVerilog 特性，可读性好）：

```verilog
enum {IDLE, START, DATA, STOP} state, state_next;
logic [2:0] data_idx, data_idx_next;   // 8 个数据位：0-7
localparam LAST_BIT = 3'd7;
```

[ThreePart/projf-explore/lib/uart/uart_tx.sv:L19-L21](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/uart/uart_tx.sv#L19-L21) —— 当前态/次态分离，外加一个 3 位比特下标 `data_idx`。

时序进程——状态寄存器**只在节拍为高时**更新，这是把慢速波特节奏引入快速时钟域的关键：

```verilog
always_ff @(posedge clk) begin
    if (stb_baud) begin
        state <= state_next;       // 只在波特节拍那一拍推进
        data_idx <= data_idx_next;
    end
    if (rst) begin state <= IDLE; data_idx <= 0; end
end
```

[ThreePart/projf-explore/lib/uart/uart_tx.sv:L23-L32](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/uart/uart_tx.sv#L23-L32) —— 复位优先，回到 IDLE。

组合逻辑给出输出与次态。注意 `data_out` 的**默认值是 1**，这同时覆盖了 IDLE 和 STOP（都该是高）：

```verilog
always_comb begin
    data_out = 1'b1;                 // 默认高：覆盖 IDLE 与 STOP
    state_next = IDLE;
    data_idx_next = 0;
    case(state)
        IDLE:  state_next = (tx_start) ? START : IDLE;
        STOP:  state_next = IDLE;
        START: begin data_out = 0;            state_next = DATA; end
        DATA:  begin data_out = data_in[data_idx];
                     data_idx_next = data_idx + 1;
                     state_next = (data_idx == LAST_BIT) ? STOP : DATA; end
    endcase
end
```

[ThreePart/projf-explore/lib/uart/uart_tx.sv:L34-L52](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/uart/uart_tx.sv#L34-L52) —— 这是整个帧的核心：START 把线拉低（起始位），DATA 逐位输出并自增下标，到第 7 位后转 STOP；STOP 里 `data_out` 保持默认 1（停止位）。

状态信号：

```verilog
always_comb begin
    tx_busy = (state != IDLE);
    tx_next = (state == STOP);   // 进入停止位时，外部可安全更新 data_in
end
```

[ThreePart/projf-explore/lib/uart/uart_tx.sv:L54-L57](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/uart/uart_tx.sv#L54-L57) —— `tx_busy` 在整个帧期间为高；`tx_next` 在停止位那一拍为高，提示上游「现在换下一个字节是安全的」。

#### 4.2.4 代码实践：仿真观察 'A' 的完整帧

**实践目标**：让 `uart_tx` 发送字符 `'A'`，观察 10 个节拍里 `data_out` 的完整序列，验证帧格式与 LSB-first。

**操作步骤**：

1. `'A'` 的 ASCII 码是 `0x41` = 二进制 `0100 0001`。LSB first 意味着发送顺序为 `D0..D7 = 1,0,0,0,0,0,1,0`。
2. 因此完整帧应为：起始位 0、然后 `1,0,0,0,0,0,1,0`、停止位 1，即 `0,1,0,0,0,0,0,1,0,1`。
3. 由于官方 README 明确写「Test benches still need to be added」，下面是一个**示例代码**（非项目自带）的最小 testbench。为了在仿真里快速看清 10 比特，用一个简单计数器直接生成 `stb_baud`（每 16 个时钟一拍），真实工程中应替换为 `uart_baud` 实例：

```verilog
// 示例代码：观察 uart_tx 发送 'A' (0x41) 的最小 testbench
`timescale 1ns/1ps
module tb_uart_tx;
    logic clk = 0, rst = 1;
    logic stb_baud = 0;
    logic tx_start = 0;
    logic [7:0] data_in = 8'h41;        // 'A'
    logic data_out, tx_busy, tx_next;

    always #5 clk = ~clk;               // 100 MHz

    // 仿真用节拍：每 16 个时钟拉高一拍（真实工程改用 uart_baud）
    logic [3:0] div = 0;
    always @(posedge clk) begin
        div <= div + 1;
        stb_baud <= (div == 4'd15);
    end

    uart_tx dut (
        .clk(clk), .rst(rst), .stb_baud(stb_baud),
        .tx_start(tx_start), .data_in(data_in),
        .data_out(data_out), .tx_busy(tx_busy), .tx_next(tx_next)
    );

    initial begin
        repeat(20) @(posedge clk);
        rst = 0;
        @(posedge stb_baud);            // 对齐到一个节拍
        tx_start = 1;                   // 请求发送 'A'
        @(posedge stb_baud);
        tx_start = 0;
        wait(tx_busy == 1);             // 等发送开始
        wait(tx_busy == 0);             // 等发送结束（回到 IDLE）
        repeat(5) @(posedge clk);
        $finish;
    end
endmodule
```

4. 用 Icarus Verilog / Verilator / Vivado 仿真器跑这个 testbench，在波形里展开 `data_out`。

**需要观察的现象**：在连续 10 个 `stb_baud` 节拍周期里，`data_out` 的电平依次变化。

**预期结果**：`data_out` 在 10 拍内依次为

```
0, 1, 0, 0, 0, 0, 0, 1, 0, 1
起 D0 D1 D2 D3 D4 D5 D6 D7 停
```

即起始位 `0`、数据 `1,0,0,0,0,0,1,0`（=`0x41` 的 LSB-first）、停止位 `1`，与第 2 步的手算完全吻合。

**待本地验证**：节拍寄存器与 DUT 在同一时钟域，不同仿真器的非阻塞赋值细节可能让打印沿偏移一拍；以**波形**为最终判据，数 `data_out` 在 10 个节拍里的电平序列即可。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `data_in` 设为 `8'h55`（即 `0101 0101`），写出预期帧序列。

**参考答案**：`0x55` = `0101 0101`，LSB first 的 D0..D7 = `1,0,1,0,1,0,1,0`，整帧为 `0,1,0,1,0,1,0,1,0,1`（起始 0、交替数据、停止 1）。

**练习 2**：`tx_next` 为什么被定义为 `state == STOP` 而不是 `state == IDLE`？

**参考答案**：停止位那一拍数据线上是固定的 1，`data_in` 已不再影响输出，此时更新 `data_in` 不会破坏当前帧；若等到 IDLE 再提示，留给上游准备下一字节的时间窗口就更短。这是给上游「提前一拍」的安全更新窗口。

---

### 4.3 接收器：uart_rx

#### 4.3.1 概念说明

`uart_rx` 是三个模块里最复杂的，因为它要在**没有共享时钟**的情况下还原字节。它的核心策略是：

1. **先同步**：外部 `data_in` 相对 FPGA 时钟异步，先用两级触发器打两拍（抗亚稳态，复用 u5-l2 的同步器）。
2. **检测起始位**：同步后的信号 `rx` 从高变低，说明起始位到来。
3. **16× 过采样**：用 `stb_sample` 节拍数拍子，每个比特周期内有 16 个采样点。
4. **中央三点采样**：在每个比特的中央（采样点 6、7、8）各取一个样本，用以抗噪声——当前实现只用中间那个 `sample_b`。

#### 4.3.2 核心流程

接收状态机同样是 `IDLE/START/DATA/STOP`，但用 16× 的 `stb_sample` 驱动，并维护一个 0–15 的采样计数器 `s_cnt`：

```
IDLE ──(rx=0 起始下降沿)──► START ──(s_cnt 数满 16)──► DATA ──(8 比特)──► STOP ──► IDLE
                                                                    │
                                  每比特在 s_cnt=6,7,8 采样 ──────────┘
                                  bit_done 时锁存 sample_b 到 data_out[data_idx]
```

关键时序对齐：起始位到来后，`START` 状态用满 16 个采样点「吃掉」整个起始位周期，于是 `s_cnt` 在进入 `DATA` 时归零并恰好对齐到数据比特边界。此后每个数据比特周期里，`s_cnt=6,7,8` 落在该比特的中央区间——采样点取中央，正是为了容忍收发时钟的频率偏差。

> 注意：`IDLE` 里检测 `rx==0` **不**受 `stb_sample` 门控（直接在系统时钟域看同步后的 `rx`），所以从起始沿到第一个 `stb_sample` 之间最多有约一个采样周期的偏差。这就是「过采样容错」要吸收的误差来源。

#### 4.3.3 源码精读

第一段是两级同步器（承接 u5-l2）：

```verilog
logic rx_0, rx;
always_ff @(posedge clk) begin
    rx_0 <= data_in;     // 第一拍
    rx   <= rx_0;        // 第二拍（同步后供状态机使用）
    if (rst) begin rx_0 <= 1; rx <= 1; end   // 复位为高（空闲电平）
end
```

[ThreePart/projf-explore/lib/uart/uart_rx.sv:L17-L26](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/uart/uart_rx.sv#L17-L26) —— 把异步的 `data_in` 打两拍得到 `rx`；复位值为 1，因为起始位是靠 `rx` 由高变低触发的，空闲态必须为高。

采样位置常量定义了「中央三点」与周期末尾：

```verilog
logic [3:0] s_cnt;                 // 16× 采样计数：0-15
localparam S_SAMPLE_A = 4'd6;      // 第 1 个采样点
localparam S_SAMPLE_B = 4'd7;      // 第 2 个（中央，实际使用）
localparam S_SAMPLE_C = 4'd8;      // 第 3 个采样点
localparam S_END       = 4'd15;    // 比特周期末尾
```

[ThreePart/projf-explore/lib/uart/uart_rx.sv:L33-L38](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/uart/uart_rx.sv#L33-L38) —— 三个采样点 6/7/8 集中在一个 16 等分比特周期的正中央。

DATA 状态的采样与推进逻辑：

```verilog
DATA: begin
    if (stb_sample) begin
        if      (s_cnt == S_SAMPLE_A) begin sample_a_next = rx; ... end
        else if (s_cnt == S_SAMPLE_B) begin sample_b_next = rx; ... end
        else if (s_cnt == S_SAMPLE_C) begin
            sample_c_next = rx;
            bit_done_next = 1;                 // 第三个样本取完，本比特就绪
        end
        else if (s_cnt == S_END) begin
            if (data_idx == LAST_BIT) state_next = STOP;   // 第 8 个数据比特结束
            data_idx_next = data_idx + 1;
            s_cnt_next = 0;
        end
        else s_cnt_next = s_cnt + 1;
    end
end
```

[ThreePart/projf-explore/lib/uart/uart_rx.sv:L92-L110](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/uart/uart_rx.sv#L92-L110) —— 每个数据比特周期内，在中央三点抓取样本，到 `S_END` 切换到下一比特，第 8 个比特结束后进入 STOP。

最终只锁存中央样本 `sample_b`（注意作者注释：三点样本是为后续做多数表决、抗噪声预留的，当前未用 a/c）：

```verilog
// We only considers one sample (sample_b) at the moment
// Using sample_a and sample_c will help with noise etc.
always @(posedge clk) begin
    if (bit_done) data_out[data_idx] <= (sample_b) ? 1 : 0;
end
```

[ThreePart/projf-explore/lib/uart/uart_rx.sv:L122-L126](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/uart/uart_rx.sv#L122-L126) —— `bit_done` 为高的那一拍把 `sample_b` 写入 `data_out` 对应位。`sample_a`/`sample_c` 已抓取但暂未参与判决——这是一个「可改进点」，见练习。

STOP 状态数满 16 拍后回到 IDLE 并拉高 `rx_done`：

[ThreePart/projf-explore/lib/uart/uart_rx.sv:L111-L118](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/uart/uart_rx.sv#L111-L118) —— 停止位周期结束，`rx_done` 拉高一拍通知上游「一个字节已就绪」。

#### 4.3.4 代码实践：源码阅读型跟踪

**实践目标**：不用上板，仅通过阅读 `uart_rx.sv`，跟踪它接收一个 `'A'`（`0x41`）字节时 `s_cnt` 与锁存结果的全过程，验证「中央采样」能正确还原数据。

**操作步骤**：

1. 假设发送方在串行线上发出 `'A'` 的帧：起始位 0、数据 `1,0,0,0,0,0,1,0`、停止位 1。
2. 在 [uart_rx.sv:L76-L82](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/uart/uart_rx.sv#L76-L82) 的 IDLE 态：同步后的 `rx` 一旦为 0，进入 START，`s_cnt` 清零。
3. 在 [uart_rx.sv:L83-L91](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/uart/uart_rx.sv#L83-L91) 的 START 态：`s_cnt` 在 `stb_sample` 上从 0 数到 15，整段对应起始位周期；到 `S_END` 进入 DATA，`data_idx=0`，`s_cnt` 再次清零——此时已对齐到 D0 的边界。
4. 在 DATA 态对每个比特：`s_cnt=6,7,8` 时抓取 `sample_a/b/c`，`s_cnt=8` 时 `bit_done=1` 把 `sample_b` 锁存进 `data_out[data_idx]`。
5. 对 D0：线上是 1，故 `data_out[0]<=1`；对 D1..D5：线上是 0；对 D6：线上是 1，`data_out[6]<=1`；对 D7：线上是 0。

**需要观察的现象**：8 个比特走完后，`data_out` 逐位拼出 `0100 0001`（bit7..bit0）。

**预期结果**：`data_out = 8'h41 = 'A'`，与发送字节一致。最后 STOP 态 `rx_done` 拉高一拍。

**待本地验证**：本跟踪基于阅读源码的逻辑推导，建议你后续用仿真（驱动 `data_in` 按帧时序变化）实测 `data_out` 与 `rx_done`。

#### 4.3.5 小练习与答案

**练习 1**：作者注释说 `sample_a`/`sample_c` 当前未用，是为了「help with noise」。请说明一种利用这三个样本抗噪声的简单做法。

**参考答案**：对 `sample_a`、`sample_b`、`sample_c` 做**三选二多数表决**（`majority = (a&b)|(b&c)|(a&c)`），用表决结果替代单独的 `sample_b`。这样即使中央附近出现单点毛刺，仍以多数为准，提高抗噪声能力。

**练习 2**：为什么接收端用 16× 过采样，而不是直接用 1× 波特节拍采样？

**参考答案**：异步通信下收发时钟不同源，1× 采样无法定位比特中央，频率误差会让采样点快速漂移出有效区。16× 过采样在检测到起始沿后从中央采样，能容忍百分之几的频率偏差撑完整整 10 比特帧；同时也为多点采样/表决留出余量。

## 5. 综合实践：搭建一个 UART 回显器

把三个模块串起来，完成「收到什么就发回什么」的回显器——这正是仓库自带示例 `top_uart.sv` 做的事。

**实践目标**：理解 `uart_baud`、`uart_rx`、`uart_tx` 三者如何用两个节拍信号协作，以及如何用一个 `always_ff` 把「接收完成」事件转成「发送请求」。

**操作步骤**：

1. 阅读 [examples/top_uart.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/uart/examples/top_uart.sv)。
2. 波特发生器同时给两个节拍，RX 与 TX 各取所需：

   [examples/top_uart.sv:L28-L36](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/uart/examples/top_uart.sv#L28-L36) —— 例化 `uart_baud`，参数 `CNT_W=24`、`CNT_INC=25770`，输出 `stb_baud`（给 TX）与 `stb_sample`（给 RX）。
3. 接收器用过采样节拍，把串行线还原成 `received`：

   [examples/top_uart.sv:L41-L48](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/uart/examples/top_uart.sv#L41-L48) —— `uart_rx` 用 `stb_sample`，`data_in` 接板上的 `uart_rx` 引脚，`data_out` 送到 `received`。
4. 发送器用波特节拍，把 `received` 原样发回：

   [examples/top_uart.sv:L52-L61](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/uart/examples/top_uart.sv#L52-L61) —— `uart_tx` 用 `stb_baud`，`data_in` 直接接 `received`（回显）。
5. 关键的「事件桥接」——把 `rx_done` 脉冲缓存为 `tx_start`：

   ```verilog
   always_ff @(posedge clk_100m) begin
       if (rx_done) tx_start <= 1;   // 收完一帧，请求发送
       if (stb_baud) tx_start <= 0;  // 节拍到，清除请求
   end
   ```

   [examples/top_uart.sv:L64-L67](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/uart/examples/top_uart.sv#L64-L67) —— 用 `rx_done` 置位 `tx_start`，下一个 `stb_baud` 清零，形成一个对齐到波特节拍的发送请求脉冲。
6. 若手头有 Arty A7 开发板，参考 [examples/arty.xdc](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/uart/examples/arty.xdc) 绑定 `clk_100m`(E3)、`btn_rst`(C2)、`uart_tx`(D10)、`uart_rx`(A9)，综合上板后用 PC 串口工具发字符，应看到字符被回显。

**预期结果**：在 PC 串口工具里输入任意字符，FPGA 把同一字符回发，屏幕上出现重复字符（如输入 `H` 显示 `HH`）。

**待本地验证**：上板结果取决于具体开发板与电平标准；无硬件时可用仿真驱动 `uart_rx` 引脚按帧时序变化，观察 `uart_tx` 引脚是否回发出同样帧。

## 6. 本讲小结

- UART 是**异步**串口：只有数据线、无共享时钟，收发双方靠约定的**波特率**和**过采样**对齐，空闲电平为高，起始位靠下降沿触发。
- 一帧 **8N1** = 起始位(0) + 8 数据位(LSB first) + 停止位(1)，共 10 个比特周期。
- `uart_baud` 用 **DDS 累加器**而非整数分频：同一增量 `25770` 配 24 位/28 位两种宽度，分别得到 16× 的 `stb_sample` 与 1× 的 `stb_baud`，二者严格 16:1 相位锁定。
- `uart_tx` 是四状态 FSM，状态寄存器被 `stb_baud` 门控，逐比特把并行字节移位成串行帧；`tx_busy`/`tx_next` 提供流控信号。
- `uart_rx` 先用**两级同步器**抗亚稳态，再用 16× `stb_sample` 过采样，在比特中央（采样点 6/7/8）采样，当前只锁存中央样本 `sample_b`。
- 三个模块靠「同一 `uart_baud` 产出的两个节拍」耦合：TX 用 `stb_baud`，RX 用 `stb_sample`，天然同步。

## 7. 下一步学习建议

- **横向联系**：回到 u5-l2 的两级同步器，对比 `uart_rx` 里的 `rx_0/rx` 打两拍——你会看到同一个抗亚稳态手法在真实模块里的落地。
- **动手改进**：按 4.3.5 练习 1 的思路，把 `uart_rx` 改成对 `sample_a/b/c` 做三选二多数表决，用仿真验证抗噪声能力提升（这是作者预留的「未完成项」）。
- **继续 Unit 5**：下一篇 u5-l5 将精读 `essential/debounce.sv`，它同样用「同步 + 计数器稳定」的思路处理机械按键抖动，与本讲的「过采样 + 中央采样稳定」异曲同工，值得对照阅读。
- **若对显示感兴趣**：UART 是 FPGA 与 PC 通信的最简通道，学完它你可以把后续 Unit 6（图形与显示）里的调试信息通过 UART 打印到 PC 终端，作为低成本调试手段。
