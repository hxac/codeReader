# 发送打包子状态机与 LED 调试

## 1. 本讲目标

前几讲我们已经把整条 DSP 链（ADC→ram1→FFT→平方求和→ram2→开方→ram3）走完了，最后一步是**把算好的数据通过 UART 送回 PC**。本讲聚焦 `TOP.v` 里专门负责「**打包并发送**」的第三套状态机 `state3`，以及随它一起出现的 LED 调试指示。学完后你应当能够：

- 说清 `state3` 为何**不是用 `clk` 驱动、而是用串口发射机回送的 `start` 脉冲驱动**（`always @(posedge start)`），以及它如何与 `serialt` 构成「生产者—消费者」握手。
- 复述一次采集结束后、主状态机在 `send_state → final_state → send_state2 → send_state3` 四个状态之间如何接力，把**频谱（ram3）**和**原始波形（ram1）**分两段依次送出。
- 还原上行数据帧的**字节顺序**：先发频谱样本、再用 `F`、`F`、`T` 三个帧头字节分隔、最后发波形样本；并解释 `state3` 的 `s3_1~s3_4` 如何在「插帧头」与「送波形」之间切换。
- 读懂 `aggregated[15:0]` 这个 16 位发送缓冲是怎么把一个 10 位样本（或一个 ASCII 帧头） packing 成线上两个字节（低字节在前、高字节在后）的。
- 读懂 `always @(state)` 驱动的 10 位 LED 条如何用**一热编码（one-hot）**直观显示主状态机当前处在哪一阶段，以及它为何只覆盖 6 个状态。

本讲承接 u5-l1（主状态机与命令协议）与 u4-l2（UART 发射机 `serialt`/`serial_tx`）：主 FSM 决定「**何时开始发、发哪段**」，而 `state3` 决定「**每个节拍把什么装进发送缓冲**」。

## 2. 前置知识

- **`start` 握手脉冲**：`serialt` 每把 `aggregated` 里的两个字节发完一组，就回送一个 `start` 上升沿给 TOP，意思是「上一组发完了，请装入下一组」。所以 `start` 的频率 = 波特率 ÷ 16 ÷ 2（每 2 个字节一次）。详见 u4-l2。
- **`aggregated` 发送缓冲**：一个 16 位寄存器（[TOP.v:38](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L38)），`serialt` 把它拆成高、低两个字节发出。`serialt` 内部的 1 位 `counter` 从 0 翻到 1 后才发第一个字节，配合 `data_in_serial = status[15:8] when counter=0 else status[7:0]`，可得**线上顺序是低字节在前、高字节在后**（u4-l2 已推导）。
- **三块 RAM 的读源**：`buffer` 是 ram1（原始波形）的组合读输出（[TOP.v:43](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L43)）；`data_send` 是 ram3（开方后幅度谱）的组合读输出（[TOP.v:65](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L65)）。本讲就是把这两个数据源交替装进 `aggregated`。
- **MUX 切换读地址**：`mux_ram3`（`sel`）在 ram3 的「开方写地址 `cnt_s`」与「上传读地址 `cnt`」之间二选一；`mux_ram1`（`sel2`）在 ram1 的「FFT 读地址 `index_in`」与「波形上传读地址 `cnt_waveform`」之间二选一。二者都是 `sel ? a : b`（见 u2-l4）。
- **一热编码（one-hot）**：用 N 位中恰好某 1 位为 1 来表示一个状态。本讲 LED 条就是一热：同一时刻只有 1 个 LED 亮。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`verilog files/TOP.v`](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v) | 顶层。本讲关注三段：`state3` 的 `always @(posedge start)` 打包块、主 FSM 里的 `send_state/final_state/send_state2/send_state3` 四个上传状态、以及 `always @(state)` 的 LED 调试块。 |
| [`vhdl files/serialt.vhd`](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/serialt.vhd) | 发射控制器。用 1 位 `counter` 把 `status`（即 `aggregated`）拆成两字节、用 `en` 作开关、用 `start` 回送「装入下一样本」节拍。本讲只引用其结论，细节见 u4-l2。 |

> 命名提醒：`state3`（2 位寄存器）和主 FSM 的 `state`（5 位寄存器）是两个东西；`state3` 的状态用 `s3_1~s3_4`，而主 FSM 里另有一个 ADC 域的 `state2` 用 `s1~s4`（见 u5-l2）。三套状态机名字相近，读代码时务必看清是哪个 `always` 块。

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

1. **发送打包子状态机（`state3`）**——用 `start` 脉冲驱动，把频谱/帧头/波形按序装进 `aggregated`。
2. **LED 调试指示**——`always @(state)` 用一热点亮 LED，让人眼能「看见」主状态机走到了哪一步。

### 4.1 发送打包子状态机（state3）

#### 4.1.1 概念说明

经过采集、FFT、平方求和、开方之后，要送回 PC 的数据其实有**两类**：

- **频谱（幅度谱）**：存在 ram3 里，10 位/点，端口名 `data_send`。
- **原始波形（时域采样）**：存在 ram1 里，10 位/点，端口名 `buffer`。

PC 端的 LabVIEW GUI 需要同时画出时域波形和频谱，所以 FPGA 必须**把两类数据都在一次采集后依次送出**，并且要让 PC 能区分「现在收到的是波形还是频谱」。这就是 `state3` 要解决的事：它是一个**装配工**，在每个发送节拍决定「该把哪个数据源的字节装进发送缓冲 `aggregated`」。

最关键的设计点是 `state3` 的**驱动时钟**——它不是 200 MHz 的 `clk`，而是串口发射机回送的 `start` 脉冲：

```verilog
always @(posedge start ) begin   // 不是 posedge clk！
```

为什么？因为发送的节奏完全由**波特率**决定，与 200 MHz 系统时钟无关。`serialt` 每发完一组（2 个字节）才回送一个 `start`，TOP 就在这条边沿上「装好下一组」。这构成了一个干净的**生产者—消费者**握手：

- **消费者**：`serialt`，按波特率一口一口地把 `aggregated` 发出去，发完一组喊一声 `start`。
- **生产者**：`state3`（在 `always @(posedge start)` 里），听到 `start` 就把下一组数据装进 `aggregated`。

这样无论波特率多快多慢，生产者都不会溢出、消费者都不会饿死——节奏完全锁在 `start` 上。

#### 4.1.2 核心流程

整个上传过程由**主 FSM 的四个状态**接力，`state3` 在其中扮演不同角色。先看主 FSM 这四态怎么走（开方结束后从 `square_state` 进入 `send_state`，`sel` 已被置 1，见 [TOP.v:357-369](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L357-L369)）：

```
                    ┌────────── 上传阶段（en=1，发射机一直开）──────────┐
                    │                                                     │
 square_state ──> send_state ──> final_state ──> send_state2 ──> send_state3 ──> wait_state
 (开方结束)        发频谱前段        发频谱后段       发帧头+波形前段     发波形后段
                   ADR_r 0..127     ADR_r 128..2047  ram_read 0..255    ram_read 256..2047
                   (127 时跳出)     (2047 时跳出,    (255 时跳出)       (2047 时回 wait)
                                     sel2←0 切到
                                     ram1 波形读址)
```

四个状态的实际判据都是「**读地址走到某个阈值就跳下一态**」，阈值就写在主 FSM 里（[TOP.v:394-411](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L394-L411)）：

| 主状态 | 读地址 | 跳出阈值 | 该段发什么 | `state3` 在做什么 |
| --- | --- | --- | --- | --- |
| `send_state` | `ADR_r`（=ram3 读址 `cnt`） | 127 | 频谱前段 | 每个 `start` 装 `data_send` |
| `final_state` | `ADR_r`（=ram3 读址 `cnt`） | 2047 | 频谱后段；跳出时 `sel2←0` | 每个 `start` 装 `data_send` |
| `send_state2` | `ram_read`（=ram1 读址 `cnt_waveform`） | 255 | **帧头 F/F/T** + 波形前段 | 先走 `s3_1→s3_2→s3_3` 插帧头，再 `s3_4` 装 `buffer` |
| `send_state3` | `ram_read`（=ram1 读址 `cnt_waveform`） | 2047 | 波形后段 | 继续 `s3_4` 装 `buffer` |

> 注意一个反直觉的点：**频谱先发、帧头在中间、波形后发**，而不是「帧头在最前面」。也就是说 PC 收到的顺序是「一堆频谱字节 → F、F、T → 一堆波形字节」。`F`、`T` 这组帧头的作用是**分隔频谱段与波形段**（推测 F = Frequency/FFT、T = Time/时域；GUI 端具体如何解析 `.vi` 是二进制工程文件，待确认）。

而 `state3` 这个 2 位子状态机本身只有 4 个状态（`s3_1~s3_4`，参数见 [TOP.v:235-238](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L235-L238)），它只在主 FSM 处于 `send_state2`/`send_state3`（即 `else` 分支）时才推进，循环如下：

```
        ┌──────────── 在 send_state2 / send_state3 期间（else 分支）────────────┐
        │                                                                          │
        │   s3_1 ──> s3_2 ──> s3_3 ──> s3_4 ──> s3_4 ──> s3_4 ──> ... (一直 s3_4)│
        │   装 'F'    装 'F'    装 'T'    装 buffer   装 buffer                 │
        │  (0x46)    (0x46)    (0x54)   cnt_waveform+1                          │
        │                     (之后不再跳出，停在 s3_4 持续送波形)                │
        └──────────────────────────────────────────────────────────────────────────┘
```

也就是说：`s3_1~s3_3` 是一次性的「插帧头」序列，`s3_4` 是稳态「送波形」。一旦进入 `s3_4` 就不再跳走，每个 `start` 都装一个波形样本、`cnt_waveform+1`，直到主 FSM 在 `send_state3` 检测到 `ram_read==2047` 跳回 `wait_state`。

#### 4.1.3 源码精读

整个 `state3` 逻辑集中在一个 `always @(posedge start)` 块里（[TOP.v:415-457](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L415-L457)）。它分两大分支：主 FSM 在 `send_state`/`final_state` 时发频谱；否则走 `state3` 子 FSM 发帧头 + 波形。

**(a) 发频谱的分支**（[TOP.v:417-429](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L417-L429)）——每来一个 `start`，就把 ram3 的当前读址 `cnt` 对应的频谱样本 `data_send` 装进 `aggregated`，并把读地址 `+1`：

```verilog
if(state==send_state) begin
    state3<=s3_1;                 // 复位子 FSM，准备以后插帧头
    cnt<=cnt+1;                   // ram3 读地址 +1（ADR_r=cnt，sel=1）
    aggregated[9:0]<=data_send;   // 装入 10 位频谱样本
    aggregated[15:10]<=6'b000000; // 高 6 位补 0
end
else if(state==final_state) begin ... end  // 完全相同的装法
```

这里 `cnt` 既是 ram3 的读地址（经 `mux_ram3`，`sel=1` 时 `ADR_r=cnt`，[TOP.v:190-193](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L190-L193)），又是频谱点的序号——一址两用。`data_send` 是 ram3 的组合读输出，`cnt+1` 后下一拍就自动给出下一个频谱点。

**(b) 发帧头 + 波形的 `else` 分支**（[TOP.v:430-456](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L430-L456)）——`state3` 子 FSM 推进：

```verilog
s3_1: begin aggregated[7:0]<=8'b01000110; aggregated[15:8]<=8'b00000000; state3<=s3_2; end // 'F'=0x46
s3_2: begin aggregated[7:0]<=8'b01000110; aggregated[15:8]<=8'b00000000; state3<=s3_3; end // 'F'=0x46
s3_3: begin aggregated[7:0]<=8'b01010100; aggregated[15:8]<=8'b00000000; state3<=s3_4; end // 'T'=0x54
s3_4: begin cnt_waveform<=cnt_waveform+1; aggregated[9:0]<=buffer; aggregated[15:10]<=6'b000000; end // 波形
```

读法要点：

- `8'b01000110` = 0x46 = ASCII `'F'`；`8'b01010100` = 0x54 = ASCII `'T'`。帧头字节直接以 ASCII 形式放进 `aggregated[7:0]`，高字节 `aggregated[15:8]` 填 0。
- `s3_4` 里 `buffer` 是 ram1 的组合读输出，读址 `cnt_waveform` 经 `mux_ram1` 给出（`sel2=0` 时 `ram_read=cnt_waveform`，`sel2` 在 `final_state` 跳出时被置 0，[TOP.v:195-198](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L195-L198) 与 [TOP.v:399-403](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L399-L403)）。`cnt_waveform+1` 让波形样本逐点送出。

**(c) `aggregated` 的字节打包**。无论装的是 10 位样本还是 ASCII 帧头，都遵循同一个打包格式（10 位数据 `d[9:0]`）：

```
aggregated[15:0]
 位:  15 14 13 12 11 10 | 9  8 | 7  6  5  4  3  2  1  0
       0  0  0  0  0  0 | d9 d8 | d7 d6 d5 d4 d3 d2 d1 d0
       \____ 高字节 [15:8] = {6'b0, d[9:8]} ____/ \__ 低字节 [7:0] = d[7:0] __/
```

`serialt` 把它拆成两个字节发出，**低字节在前、高字节在后**（u4-l2 已由 `serialt`/`serial_tx` 推导）。所以：

- 一个 10 位频谱/波形样本 `d`，线上两个字节依次是：`d[7:0]`、`{6'b0, d[9:8]}`。
- 一个帧头 `'F'`（`aggregated = 16'h0046`），线上两个字节依次是：`0x46`（'F'）、`0x00`。

**(d) 主 FSM 四个上传状态**（[TOP.v:394-411](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L394-L411)）负责「开 `en`、判读址阈值、切 `sel2`」：

```verilog
send_state:  begin en<=1'b1; if(ADR_r==11'b00001111111) state<=final_state; end       // ADR_r==127
final_state: begin if(ADR_r==11'b11111111111) begin state<=send_state2; sel2<=1'b0; end end // ==2047，切到 ram1 波形读址
send_state2: begin if(ram_read==11'b00011111111) state<=send_state3; end              // ==255
send_state3: begin if(ram_read==11'b11111111111) state<=wait_state; end               // ==2047，收工
```

读址阈值含义：`11'b00001111111`=127、`11'b00011111111`=255、`11'b11111111111`=2047。`send_state` 与 `final_state` 都发频谱（`state3` 走 `if/else if` 分支装 `data_send`），二者的区别只在副作用：`send_state` 开 `en`，`final_state` 在结束时把 `sel2←0`、把 ram1 的读址从「喂 FFT」切到「送波形」。`send_state2`/`send_state3` 都发波形（`state3` 走 `else` 分支），区别只是阈值不同。

#### 4.1.4 代码实践

**实践目标**：根据本讲源码，还原「一次采集后 PC 收到的完整上行字节流」的结构，画出帧格式图。

**操作步骤（源码阅读型）**：

1. 打开 [TOP.v:415-457](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L415-L457) 与 [TOP.v:394-411](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L394-L411)。
2. 按主 FSM 的执行顺序，列出每一段「装的是什么、装了多少」。
3. 用「低字节在前、高字节在后」的规则，把每一段展开成线上的字节序列。
4. 拼出完整帧。

**参考答案（帧结构）**：

```
一次采集后的上行帧（FPGA → PC）：

┌───────────────────────────┬──────────────┬───────────────────────────┐
│  ① 频谱段                 │  ② 帧头分隔  │  ③ 波形段                  │
│  ram3 样本 × ~2048        │  F , F , T   │  ram1 样本 × ~2048        │
│  每点 2 字节: [d7..d0],[00,d9,d8] │ 每个也是 2 字节 │ 每点 2 字节: 同左          │
└───────────────────────────┴──────────────┴───────────────────────────┘
   send_state + final_state     send_state2            send_state2 + send_state3
   (state3 走 if/else if 分支)  (s3_1,s3_2,s3_3)        (s3_4 稳态)
```

展开成字节流（`S[i]` 表示第 i 个频谱/波形样本的低/高字节对）：

```
S_lo[0] S_hi[0]  S_lo[1] S_hi[1]  ...  S_lo[2047] S_hi[2047]    <- 频谱段
0x46 0x00  0x46 0x00  0x54 0x00                                       <- 帧头 F, F, T
W_lo[0] W_hi[0]  W_lo[1] W_hi[1]  ...  W_lo[2047] W_hi[2047]        <- 波形段
```

**需要观察/思考的现象**：

- 频谱段在前、波形段在后，帧头夹在中间——与「先帧头后数据」的直觉相反。
- 频谱样本数量按地址阈值算是 `2048` 个点（地址 0..2047），但开方阶段只真正计算了 `0..1022` 共 **1023** 个点（`cnt_s<11'b1111111111`，见 u3-l3）。这意味着频谱段后半（地址 1023..2047）是 **ram3 里的陈旧/未更新数据**。待本地验证 GUI 是否只显示前半段。
- `cnt`（[TOP.v:41](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L41)）与 `cnt_waveform`（[TOP.v:75](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L75)）**在两次采集之间都没有被复位**（全仓库只有 `+1`，无清零）。第一次采集从 0 开始没问题；连续触发第二次采集时，这两个地址会从上次的终值接着走、超过 2047 后在 11 位空间里回绕。这是一个潜在的多帧对齐问题，待硬件验证。

**预期结果**：你能向别人讲清楚「PC 在收到 `P` 命令触发的采集结果时，会先收到一段频谱、再看到 `F F T` 分隔符、最后收到一段波形」，并能指出字节序与 10→16 位 packing 的对应关系。若无法在硬件上抓串口，相关计数值标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `state3` 用 `always @(posedge start)` 而不是 `always @(posedge clk)`？如果改成 `posedge clk` 会怎样？

> **答案**：发送节奏由波特率决定，`start` 是 `serialt`「发完一组、请求下一组」的握手脉冲，用它驱动可以保证「每发一组才装一组」，与波特率自动同步。若改成 `posedge clk`（200 MHz），`state3` 会以 200 MHz 疯狂改写 `aggregated`，而 `serialt` 还在慢吞吞按波特率发，结果 `serialt` 发出去的会是「被反复覆盖、几乎随机」的字节，帧结构完全崩溃。

**练习 2**：`aggregated` 里装一个值为 `10'd723`（二进制 `10'b1011010011`）的波形样本，PC 上依次收到的两个字节分别是什么（用 8 位二进制和十六进制表示）？

> **答案**：`d = 10'b1011010011`。低字节 `aggregated[7:0] = d[7:0] = 8'b11010011 = 0xD3`；高字节 `aggregated[15:8] = {6'b000000, d[9:8]} = {000000, 10} = 8'b00000010 = 0x02`。线上低字节在前：先 `0xD3`，再 `0x02`。

**练习 3**：`s3_4` 状态里没有 `state3<=...` 的跳转，这意味着什么？它会不会永远卡在 `s3_4`？

> **答案**：没有跳转就意味着「停在 `s3_4` 不走」，这正是设计意图——稳态送波形，每个 `start` 装一个波形样本、`cnt_waveform+1`。它不会永远卡住，因为**跳出条件不在 `state3` 里，而在主 FSM**：`send_state3` 检测到 `ram_read==2047` 后把主 `state` 拉回 `wait_state`，`en` 随之关闭，`start` 不再产生，`state3` 自然不再推进。子 FSM 的「停下」由主 FSM 负责。

### 4.2 LED 调试指示

#### 4.2.1 概念说明

FPGA 在高速运行时，内部状态肉眼看不见。Nexys 4 DDR 板上有一排 LED，作者把主状态机的当前状态映射到这排 LED 上，让人眼能**直接看到系统走到了流水线的哪一步**。源码里有两行注释点明了它的用途——「the state indicator」「just for debugging」（[TOP.v:497-498](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L497-L498)）。

它的实现非常直白：一个对主状态 `state` 敏感的 `always` 块，用**一热编码**点亮 LED——某个主状态对应某个固定的 LED，同一时刻只有那 1 个 LED 亮。这样 LED 条就像一根「状态指针」，从左到右移动就代表流水线在推进。

#### 4.2.2 核心流程

LED 映射表（[TOP.v:500-581](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L500-L581)）：

| 主状态 | 点亮的 LED | `leds[9:0]`（二进制） | 含义 |
| --- | --- | --- | --- |
| `init_state` | led[5] | `0000010000` | 上电初始化（程序加载后只执行一次） |
| `wait_state` | led[0] | `0000000001` | 空闲，等 PC 命令 |
| `trig_state` | led[1] | `0000000010` | 等触发条件 |
| `acq_state` | led[2] | `0000000100` | 正在采集、填 ram1 |
| `send_state` | led[3] | `0000001000` | 正在发频谱前段 |
| `final_state` | led[4] | `0000010000` | 正在发频谱后段 |

几个要点：

- **只覆盖 6 个状态**。15 个主状态里，`fft_state`、`fft_write_state`、`square_state`~`square_state5`、`send_state2`、`send_state3`、`trig_state` 之外的 FFT/开方/波形上传段**没有 case 分支**。`case` 未命中时 `leds` 保持原值，所以进入 FFT 段后 LED 会「停在 `acq_state` 的 led[2]」不动，直到进入 `send_state` 才跳到 led[3]。
- `leds[6..9]` 始终为 0（每个分支都显式写了 0），实际只用了 6 个 LED。
- `always @(state)` 是**电平敏感**（只在 `state` 变化时触发），不是时钟沿敏感，所以它更像一个组合译码器，只是借了 `reg` + 非阻塞赋值的写法。

#### 4.2.3 源码精读

LED 块整体（[TOP.v:497-581](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L497-L581)），典型分支如下（[TOP.v:503-514](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L503-L514)）：

```verilog
always @(state) begin
case(state)
wait_state : begin
    leds[0]<=1'b1;   // 只有 led[0] 亮
    leds[1]<=1'b0; ... leds[9]<=1'b0;
end
...
endcase
end
```

每个分支都是「目标位置 1、其余 9 位清 0」的标准一热写法。端口声明是 `output reg [9:0] leds`（[TOP.v:27](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L27)），注释也写明 `//led bar for debugging`。

#### 4.2.4 代码实践

**实践目标**：用 LED 映射表预测「采集一次」过程中 LED 条的亮灭顺序，验证你对主 FSM 流程的理解。

**操作步骤（源码阅读型）**：

1. 对照 u5-l1 的主 FSM 流程（`init → wait → trig → acq → fft → ... → square → send → final → send2 → send3 → wait`）。
2. 对每一步，用上面的映射表回答「此时哪个 LED 亮、或保持上一个」。
3. 写出你预期的 LED 亮灭时间线。

**需要观察的现象 / 预期结果**：

```
上电:        led[5] 亮一下（init_state，仅一次）
空闲:        led[0] 常亮（wait_state）
收到 P:      led[1] 亮（trig_state，等触发）
开始采集:    led[2] 亮（acq_state）
FFT/开方段:  LED 停在 led[2] 不变（这些状态无 case 分支，保持 acq_state 的值）
开始上传:    led[3] 亮（send_state，发频谱前段）
频谱后段:    led[4] 亮（final_state）
波形段:      LED 停在 led[4] 不变（send_state2/send_state3 无 case 分支）
回到空闲:    led[0] 亮（wait_state）
```

**一个可立即上手的小改动（示例代码，非项目原有）**：若你想在板子上看到 FFT 段的指示，可在 LED 块里给 `fft_state` 加一个分支，比如把 `leds[6]<=1'b1`（其余清 0）。这能填补「采集到上传之间 LED 长时间不动」的盲区。修改 LED 块**不会影响数据通路**，是安全的纯调试改动。

> 若无硬件，以上为「源码阅读型预测」，实际亮灭待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么在 FFT 和开方阶段（`fft_state`~`square_state5`）LED 不动？

> **答案**：`always @(state)` 的 `case` 里没有这些状态的分支。Verilog 的 `case` 未命中且无 `default` 时，寄存器保持原值，所以 LED 一直显示进入 FFT 段前的最后一个有映射的状态——即 `acq_state` 的 led[2]。

**练习 2**：`always @(state)` 与 `always @(posedge clk)` 写 LED 有什么区别？这里为什么用前者？

> **答案**：`always @(state)` 只在 `state` 变化时才重新求值，本质上是一个对状态译码的组合逻辑（只是用 `reg` 存了输出）；`always @(posedge clk)` 则每个时钟沿都更新。这里 LED 只需在状态改变时变化、状态不变时保持，用 `always @(state)` 更省、更直观，也避免了每个 200 MHz 时钟沿都无谓地重写 `leds`。

**练习 3**：如果想让 `send_state2`/`send_state3`（波形上传段）也有独立指示，最小改动是什么？

> **答案**：在 LED 块的 `case` 里新增两个分支：`send_state2: leds[6]<=1; 其余清 0;` 与 `send_state3: leds[7]<=1; 其余清 0;`（或复用任一空闲位）。因为 LED 块与数据通路解耦，这样改不会影响发送逻辑。

## 5. 综合实践

**任务**：把本讲的两条线——`state3` 打包逻辑与 LED 指示——串起来，写一份「一次采集的完整上行报告」。

请结合 u5-l1（主 FSM）、u3-l4（DSP 链）与本讲，完成下面三件事：

1. **画一张时序甘特图**：横轴是时间，纵轴是 `{主状态 state, state3 子状态, 点亮的 LED, 此时 aggregated 装的是什么}`。从 PC 发出 `P` 命令开始，依次标出 `wait → trig → acq → fft → fft_write → square(×5) → send → final → send2 → send3 → wait`，并标注在 `send2` 段内 `state3` 如何从 `s3_1` 走到 `s3_4`。

2. **写一段串口抓包解析伪代码**：假设你在 PC 端用串口工具抓到一次采集后的字节流。请写伪代码，用 `F`、`F`、`T` 这组分隔符把字节流切成「频谱段」和「波形段」，并按「低字节在前、高字节在后 + 高字节高 6 位补 0」的规则把每两个字节还原成一个 10 位样本。注意提示读者：频谱段长度按地址阈值是 ~2048 点，但有效数据只有前 1023 点。

3. **指出两处可加固点**：(a) `cnt` / `cnt_waveform` 在帧间不复位；(b) 频谱段送出了 2048 点但只计算了 1023 点。各写一句改进建议（例如在 `wait_state` 里把这两个计数器清零；或在 `final_state` 跳出条件里把阈值改成实际有效点数）。

**预期成果**：一张图 + 一段伪代码 + 两条改进建议。如果手头有 Nexys 4 DDR，可以在板子上观察 LED 的真实亮灭顺序来核对第 1 步；否则全部标注「待本地验证」。

## 6. 本讲小结

- `state3` 是 `TOP.v` 的第三套状态机，**用 `start` 脉冲驱动而非 `clk`**，与 `serialt` 构成生产者—消费者握手：发完一组（2 字节）才装下一组，节奏自动锁在波特率上。
- 上传由主 FSM 的 `send_state → final_state → send_state2 → send_state3` 四态接力：**前两态发频谱（ram3 的 `data_send`）**，后两态发帧头 + 波形（ram1 的 `buffer`）。
- 帧头 `F`、`F`、`T`（0x46、0x46、0x54）由 `state3` 的 `s3_1→s3_2→s3_3` 一次性插入，**夹在频谱段与波形段之间**；之后 `s3_4` 稳态地逐点送波形，停在 `s3_4` 直到主 FSM 把状态拉回 `wait_state`。
- `aggregated[15:0]` 统一打包：10 位样本放在 `[9:0]`、高 6 位补 0，`serialt` 按低字节在前、高字节在后发出。
- LED 调试块 `always @(state)` 用一热编码把 6 个主状态映射到 LED 条，肉眼可见流水线进度；FFT/开方/波形上传段无映射，LED 保持上一值。
- 读址切换靠两个 MUX：`sel`（ram3：开方写址 `cnt_s` ↔ 上传读址 `cnt`）、`sel2`（ram1：FFT 读址 `index_in` ↔ 波形读址 `cnt_waveform`），都在主 FSM 的状态切换里被设定。

## 7. 下一步学习建议

到这里，`TOP.v` 的**三套状态机**（主 FSM `state`、ADC 域 `state2`、打包域 `state3`）已经全部讲完，整条「PC 命令 → 触发 → 采集 → FFT → 开方 → 打包上传」的闭环也就闭合了。接下来建议：

- **u6-l1（混合语言集成与 Xilinx IP）**：从工程组织角度回看 TOP.v，把所有例化模块分成「自研 Verilog / 自研 VHDL / Xilinx IP 封装」三类，理解 Vivado 下 Verilog+VHDL 混合语言工程如何拼装。
- **u6-l2（PC 命令协议与 LabVIEW GUI）**：站在系统层面把本讲的上行帧格式（频谱 + F/F/T + 波形）与 u5-l1 的下行命令（P/A/B/C/D）对应到上位机 LabVIEW GUI 的收发与绘图，并尝试写上位机测试脚本。
- **重读源码**：把 [TOP.v:247-413](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L247-L413) 的主 FSM 与 [TOP.v:415-457](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L415-L457) 的 `state3` 对照着读一遍，确认你已经能解释「主 FSM 决定发哪段、`state3` 决定装什么」的分工。

如果你打算做改进，本讲的 `cnt`/`cnt_waveform` 帧间不复位、频谱段长度与有效点数不匹配，是两个门槛最低的入手点。
