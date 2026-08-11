# 主采集状态机与命令协议

## 1. 本讲目标

本讲进入整个项目的「大脑」——`TOP.v` 里的**主采集状态机**。学完后你应该能够：

- 读懂主状态机 `state` 的全部状态参数与跳转条件，能画出完整状态转移图。
- 理解 PC 如何通过串口用一个 ASCII 字符控制 FPGA：`P` 触发一次采集，`A/B/C/D` 分别配置时基、触发电平、斜率、增益。
- 理解 `conf_index` 这个 3 位寄存器实现的「两步式命令解析」：先收命令字母，再收参数字节。
- 把主状态机看作一条「流水线调度器」：它按 `采集 → FFT → 开方 → 上传` 的顺序，用三个写使能 `we / we2 / we3` 独占调度三块 RAM。

本讲是专家层的第一篇，会把前面所有讲义（时钟、RAM、FFT、UART）串到同一个调度核心上。

## 2. 前置知识

阅读本讲前，请确认你已经理解以下概念（对应前置讲义）：

- **数据流总览**（u1-l3）：系统按 `ADC → ram1 → FFT → 平方求和 → ram2 → 开方 → ram3 → UART` 流转，主状态机就是这条流水线的「总指挥」。
- **三块 RAM**（u2-l2）：`ram1` 存采样、`ram2` 存平方和、`ram3` 存幅度；每块 RAM 有独立的写使能，主状态机通过控制写使能来决定「现在谁可以写」。
- **FFT 握手**（u3-l1）：FFT 核有 `start / rfd / edone / done / dv` 等握手信号，主状态机要等 `edone` 才能进入卸载阶段。
- **UART 收发**（u4-l1 / u4-l2）：PC 发来的字节由 `serial_rx` 接收，收到后 `dserial_avail` 拉高一拍、`dserial_in` 给出字节；FPGA 用 `serialt` 发回数据。

本讲用到的两个关键术语：

- **FSM（Finite State Machine，有限状态机）**：用一组离散状态 + 状态间的跳转条件来描述控制逻辑。在 Verilog 里通常写成 `always @(posedge clk) case(state) ...` 的形式。
- **写使能（Write Enable, `we`）**：RAM 的一个控制脚，为 1 才允许写入。三块 RAM 各有 `we / we2 / we3`，主状态机靠「同一时刻只开一个写使能」来避免数据冲突。

主状态机跑在 **200 MHz 的 `clk`** 上（见 [TOP.v:247](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L247)），所以下面提到的「一拍」就是 5 ns（200 MHz 的周期，待本地验证）。

## 3. 本讲源码地图

本讲几乎全部内容集中在同一个文件里：

| 文件 | 作用 |
|---|---|
| `verilog files/TOP.v` | 顶层模块，包含主状态机 `state`、命令解析、三块 RAM 与各 IP 的例化、LED 调试逻辑、ADC 域子状态机 `state2` |

只需要精读 `TOP.v` 中三段：

1. **状态参数声明**（[TOP.v:213-227](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L213-L227)）：15 个主状态的名字与编码。
2. **主 `always` 块**（[TOP.v:247-413](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L247-L413)）：每个状态的跳转条件与副作用。
3. **`wait_state` 内的命令解析**（[TOP.v:266-311](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L266-L311)）：`P/A/B/C/D` 协议与 `conf_index` 两步式解析。

> 提醒（沿用 u1-l2）：`TOP.v` 文件名与模块名一致，都是 `TOP`。但仓库里其他文件常有「文件名 ≠ 模块名」的陷阱，读代码一律认 `module` 关键字后的名字。

---

## 4. 核心概念与源码讲解

### 4.1 TOP 主状态机（`state`）

#### 4.1.1 概念说明

整条信号链上的模块（ADC 接口、三块 RAM、FFT、平方/求和、开方、UART）各自是「干活的工人」，但它们节奏完全不同：

- 采样填满 `ram1` 需要很多拍，且由 **ADC 时钟域**驱动；
- FFT 一次变换需要若干拍，跑在 **100 MHz**；
- 开方是逐点串行的，每个点要约 13 拍，是**最慢的一级**；
- UART 发送又跑在 **50 MHz**，一个字节要几十拍。

如果让它们同时乱跑，三块 RAM 会被多个模块抢着写，数据就乱了。**主状态机的作用，就是当一个「流水线调度器」**：决定现在该谁工作、该开哪块 RAM 的写使能、该等待哪个握手信号。

主状态机的「输入」有两类：

- **外部触发**：PC 通过串口发来的命令字节（`P` 触发采集）。
- **内部握手信号**：`carry`（ram1 写满）、`edone`（FFT 算完）、`sqr_rdy`（开方出结果）、各种地址计数器到达终值。

它的「输出」是一组控制信号：`we / we2 / we3`（三块 RAM 的写使能）、`start_fft`（启动 FFT）、`sclr`（开方核复位）、`en`（UART 发送开关）、`sel / sel2`（读地址多路选择）、`res_serial`（串口字节序复位）等。

#### 4.1.2 核心流程

把 15 个状态按职责分成 5 组，整个流程如下（箭头表示典型跳转方向）：

```text
[空闲/配置组]
  init_state ──(上电一拍)──> wait_state <──────┐
                                  │            │
                          (收到 P) │            │
                                  ▼            │
[触发组]                          │            │
  trig_state ──(触发命中/超时)──> acq_state     │
                                  │            │
[采集组]                          │            │
  acq_state ──(ram1 满, carry)──> fft_state     │
                                  │            │
[FFT 组]                          │            │
  fft_state ──(edone)──> fft_write_state        │
  fft_write_state ──(index_out==1022)──> square_state
                                  │            │
[开方组（自循环）]                │            │
  square_state <─┐                 │            │
       │        │ (每点 5 状态循环)│            │
       ▼        │                 │            │
  square_state2~5 ─┘              │            │
  square_state ──(cnt_s>=1023)──> send_state   │
                                  │            │
[上传组]                          │            │
  send_state ─> final_state ─> send_state2 ─> send_state3 ─┘
   (发频谱)      (发频谱剩余)     (发波形)        (回到 wait_state)
```

一句话概括：**`init → wait（等命令）→ trig（等触发）→ acq（存 ram1）→ fft/fft_write（算 FFT 存 ram2）→ square×5（开方存 ram3）→ send/final/send2/send3（上传）→ 回 wait`**。

注意状态编码有个有意思的地方：`wait_state = 5'b10001`、`trig_state = 5'b10010`，它们的最高位（bit[4]）是 1；而其余 13 个状态编码在 `00000 ~ 01101` 范围内，bit[4] 全是 0。也就是说作者把「等待外部事件」的两个状态（等命令、等触发）放到了编码空间的另一半，把「确定性流水线」状态放在低半区。这看起来像是刻意区分「阻塞等待」与「流水推进」，但源码没有注释说明，仅作为观察记录，**不一定是设计意图，待确认**。

#### 4.1.3 源码精读

**(a) 状态参数声明** —— [TOP.v:213-227](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L213-L227)

```verilog
parameter   init_state = 5'b00000, 
            wait_state = 5'b10001,
            acq_state  = 5'b00010,
            send_state = 5'b00011, 
            final_state= 5'b00100,
            fft_state  = 5'b00101,
            fft_write_state = 5'b00110,
            square_state    = 5'b00111,
            square_state2   = 5'b01000,
            square_state3   = 5'b01001,
            square_state4   = 5'b01010,
            square_state5   = 5'b01011,
            send_state2 = 5'b01100,
            send_state3 = 5'b01101,
            trig_state  = 5'b10010;
```

共 15 个状态，用 5 位宽寄存器 `state`（[TOP.v:241](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L241)）保存。`square_state2~5` 是开方专用的小循环状态，详细原理见 u3-l3，本讲只把它们当作「开方阶段」整体看待。

**(b) 主 `always` 块的时钟** —— [TOP.v:247](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L247)

```verilog
always @(posedge clk ) begin   
case(state)
```

主状态机跑在 **200 MHz 的 `clk`** 上升沿。注意：ADC 写 ram1 的子状态机 `state2` 跑在另一个时钟 `clock_adc_out` 上（[TOP.v:461](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L461)），二者是**跨时钟域**关系，详见 u5-l2。本讲只看主状态机。

**(c) `init_state`：上电一次性初始化** —— [TOP.v:251-261](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L251-L261)

```verilog
init_state: begin
    trig_value<=8'b10000000;   // 触发电平设为中点 128
    slope_adj<=1'b0;           // 默认下降沿触发
    state<=wait_state;         // 无条件进入空闲
    conf_index<=0;             // 命令解析回到空闲
    adj<=3'b100;               // 模拟前端增益初值
    timebase<=4'b0000;         // 时基初值
    adc_div_sel<=1'b1;         // 选快速 ADC 时钟档
    adc_state<=1'b1;           // ADC 始终使能
end
```

`init_state` 只在上电后执行一拍，把所有可配置参数设为默认值，然后无条件跳到 `wait_state`。注意源码里 `adc_div_sel` 在这段被赋值了两次（先 `2'b00` 再 `1'b1`），后者覆盖前者——这是源码里一处冗余，最终生效的是 `1'b1`。

> 顺带提一个源码小瑕疵：`trig_value` 在声明处写成了 `reg [7:0] trig_value=8'b1000000000;`（[TOP.v:78](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L78)），字面量里有 10 个二进制位却声明成 8 位宽，综合工具通常会告警并截断。好在 `init_state` 里用正确的 `8'b10000000`（8 位）覆盖了它，所以实际运行值仍是 128。

**(d) `trig_state`：等待触发或超时** —— [TOP.v:316-327](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L316-L327)

```verilog
trig_state: begin
    if(adc_read[9:2]==trig_value) begin              // 信号电平 == 触发电平
        if(slope==slope_adj) state<=acq_state;        // 且斜率方向匹配 → 开始采集
    end
    else begin
        if(trigger_counter==30'b000000000111111111111111111111) begin  // 超时
            trigger_counter<=0;
            state<=acq_state;                         // 强制采集
        end
        else trigger_counter<=trigger_counter+1;      // 否则继续等
    end
end
```

这是主状态机层面看到的触发逻辑：要么等到信号电平与斜率都满足条件（`slope` 由 `state2` 子状态机算出，见 u5-l2），要么等到一个超时计数器满，就强制进入采集。那个超时常数是：

\[ 30'b\,000000000\,111111111111111111111 = 2^{21}-1 = 2\,097\,151 \]

即 9 个前导 0 加 21 个 1，共 30 位。`trigger_counter` 在主 `always` 块里每拍（200 MHz）加 1，所以超时约 \( 2\,097\,151 / 2\times10^{8} \approx 10.5 \text{ ms} \)。这个数值依赖 `clk` 的精确频率（PLL 倍频系数待确认，见 u2-l1），**实际超时时间待本地验证**。

**(e) `acq_state`：填满 ram1** —— [TOP.v:330-336](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L330-L336)

```verilog
acq_state: begin
    if(carry==1) begin               // ram1 写满（地址走到 2047）
        state<=fft_state;
        we=1'b0;                     // 关闭 ram1 写使能
        res_serial<=1'b0;
    end
    else we=1'b1;                    // 否则保持 ram1 可写
end
```

`carry` 是 ram1 的「满标志」（u2-l2），当写地址走到 2047 时拉高。这里轮询 `carry`，一旦 ram1 满，立刻关掉 `we` 切入 FFT。`we` 用的是阻塞赋值 `=`（而非 `<=`），是源码里一处风格不一致，功能上不影响本拍判断。

**(f) `fft_state` / `fft_write_state`：启动并卸载 FFT** —— [TOP.v:339-354](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L339-L354)

```verilog
fft_state: begin
    we2<=1'b1;            // 打开 ram2 写使能
    start_fft<=1'b1;      // 启动 FFT 核
    if(edone==1'b1) state<=fft_write_state;   // FFT「提前一拍完成」信号
end

fft_write_state: begin
    start_fft<=1'b0;
    if(index_out==10'b1111111110) begin        // 卸载到第 1022 个点
        we2<=1'b0;                             // 关 ram2 写
        we3<=1'b1;                             // 开 ram3 写
        state<=square_state;
        sclr<=1'b0;                            // 释放开方核复位
        cnt_s<=11'b00000000000;                // 开方计数清零
    end
end
```

FFT 核的握手细节见 u3-l1。这里只需理解主状态机的角色：`fft_state` 拉 `start_fft` 启动变换，等 `edone`（比 `done` 早一拍，用来提前打开 ram2 写使能、不漏第 0 个输出点）；`fft_write_state` 等输出计数 `index_out` 走到 1022 就关闭 ram2、打开 ram3，进入开方阶段。

**(g) `square_state` ~ `square_state5`：逐点开方自循环** —— [TOP.v:357-391](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L357-L391)

这 5 个状态构成一个小循环，配合 CORDIC 开方核的 8 拍流水线，对 ram2 里的每个点算开方、写进 ram3。循环判据是：

```verilog
square_state: begin
    if(cnt_s<11'b1111111111)   // cnt_s < 1023
        ...                     // 继续开方下一个点
    else begin
        state<=send_state;      // 全部处理完 → 进入上传
        ...
    end
end
```

即处理 `cnt_s = 0..1022` 共 1023 个点（第 1023 号频点未被开方，详见 u3-l3 的提醒）。每个点内部的 5 状态节拍见 u3-l3，本讲不再展开。

**(h) 上传组 `send_state` / `final_state` / `send_state2` / `send_state3`** —— [TOP.v:394-411](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L394-L411)

```verilog
send_state:  begin en<=1'b1; if(ADR_r==11'b00001111111) state<=final_state; end   // ADR_r==127
final_state: begin if(ADR_r==11'b11111111111) begin state<=send_state2; sel2<=1'b0; end end  // ADR_r==2047
send_state2: begin if(ram_read==11'b00011111111) state<=send_state3; end          // ram_read==255
send_state3: begin if(ram_read==11'b11111111111) state<=wait_state; end            // ram_read==2047
```

这一组负责把 ram3（频谱）和 ram1（原波形）依次发回 PC，发完后回到 `wait_state` 等待下一条命令。具体的字节打包、帧头插入逻辑在 `state3` 子状态机里（u5-l3），本讲只关注主状态机层面「发完没有」的判据——靠读地址计数器（`ADR_r`、`ram_read`）到达终值来判断。

#### 4.1.4 代码实践：列出主 FSM 全部状态与跳转条件

这是一个**源码阅读型实践**，不需要硬件。

1. **实践目标**：把主状态机的 15 个状态、各自的下一条状态、跳转条件、主要副作用整理成一张表，从而把「调度器」的全貌装进脑子。
2. **操作步骤**：
   - 打开 [TOP.v:247-413](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L247-L413) 的主 `always` 块。
   - 对每个 `xxx_state:` 分支，找出其中所有 `state<=...` 赋值及其前面的 `if` 条件。
3. **需要观察的现象**：哪些状态有**多个**出口（条件分支），哪些状态是**无条件单出口**。
4. **预期结果**：应得到与下表一致的状态图（编码列见 4.1.3 (a)）。

| 状态 | 下一状态 | 跳转条件 | 该状态主要动作 |
|---|---|---|---|
| `init_state` | `wait_state` | 无条件（上电一拍） | 初始化所有参数 |
| `wait_state` | `trig_state` | 收到 `'P'`（0x50） | 空闲；解析串口命令（见 4.2） |
| `wait_state` | `wait_state` | 收到 `A/B/C/D` 或参数字节 | 设置/消费 `conf_index` |
| `trig_state` | `acq_state` | `adc_read[9:2]==trig_value` 且 `slope==slope_adj` | 等触发命中 |
| `trig_state` | `acq_state` | `trigger_counter` 计满 \(2^{21}-1\) | 超时强制采集 |
| `acq_state` | `fft_state` | `carry==1`（ram1 满） | `we=1` 写 ram1 |
| `fft_state` | `fft_write_state` | `edone==1` | `start_fft=1`、`we2=1` |
| `fft_write_state` | `square_state` | `index_out==1022` | 卸载 FFT 到 ram2 |
| `square_state`（循环入口） | `square_state2` | `cnt_s<1023` 且 `sqr_rdy==1` | 逐点开方 |
| `square_state`（出口） | `send_state` | `cnt_s>=1023` | 关 ram3、切读地址 |
| `square_state2~5` | 依次推进回 `square_state` | 各自无条件 | 5 拍节拍（复位/计数） |
| `send_state` | `final_state` | `ADR_r==127` | `en=1`，发频谱前段 |
| `final_state` | `send_state2` | `ADR_r==2047` | 发频谱剩余 |
| `send_state2` | `send_state3` | `ram_read==255` | 发波形前段 |
| `send_state3` | `wait_state` | `ram_read==2047` | 发完波形，回到空闲 |

5. **若无法本地综合**：这张表本身就是「待本地验证」的静态分析结论，你可以在阅读时逐行核对源码确认。

#### 4.1.5 小练习与答案

**练习 1**：`trig_state` 有几个出口？分别是什么条件？
> **答案**：两个出口，都指向 `acq_state`。一个是「触发命中」——`adc_read[9:2]==trig_value` 且 `slope==slope_adj`；另一个是「超时」——`trigger_counter` 计满 \(2^{21}-1\)。前者保证波形在想要的边沿开始，后者保证即使一直等不到触发也不会卡死。

**练习 2**：为什么 `acq_state` 进入 `fft_state` 时要把 `we` 置 0？
> **答案**：`we` 是 ram1 的写使能。采集阶段 `we=1` 让 ADC 数据持续写进 ram1；一旦 `carry==1`（ram1 满），必须立刻关掉 `we`，否则下一帧 ADC 数据会覆盖刚存好的样本，FFT 读到的就是被破坏的数据。主状态机的核心职责之一就是「在同一时刻只让一个模块写某块 RAM」。

**练习 3**：从 `wait_state` 收到 `P` 开始，到回到 `wait_state`，主状态机会依次经过哪些状态？（按 4.1 的分组回答即可）
> **答案**：`wait_state → trig_state → acq_state → fft_state → fft_write_state → square_state（含 square_state2~5 自循环）→ send_state → final_state → send_state2 → send_state3 → wait_state`。

---

### 4.2 命令协议解析（`conf_index` 两步式）

#### 4.2.1 概念说明

PC 通过串口给 FPGA 下达命令。本项目用了一套非常简洁的**ASCII 字符协议**：每个命令是 1 个字节，而且就是普通的英文字母。命令分两类：

- **立即命令**：单字节，收到就执行。只有一个——`P`（0x50），表示「现在开始一次采集」。
- **配置命令**：**两字节**，第一个字节是命令字母（`A/B/C/D`），告诉 FPGA「接下来我要配置哪个参数」；第二个字节是**参数值**。比如要改时基，就先发 `A`，再发一个携带时基值的字节。

为什么要两步？因为一个字节既要表达「改哪个参数」又要表达「改成什么值」是不够的（参数种类就有 4 种）。所以协议把命令拆成「先选参数、再给值」两步。FPGA 用一个 3 位寄存器 `conf_index` 来记住「现在在等哪个参数的值」：

- `conf_index == 0`：空闲，等命令字母。
- `conf_index == 001/010/011/100`：分别表示正在等 `A/B/C/D` 的参数值。

#### 4.2.2 核心流程

命令解析发生在 `wait_state` 里，流程如下：

```text
wait_state 中收到一个字节 (dserial_avail && rx_allowed):
│
├─ 是 'P'(0x50)?  ──yes──> state<=trig_state        （立即触发，单字节命令）
│
├─ 是 'A'(0x41)?  ──yes──> conf_index<=001           （记住：下一个字节是 A 的参数）
├─ 是 'B'(0x42)?  ──yes──> conf_index<=010
├─ 是 'C'(0x43)?  ──yes──> conf_index<=011
├─ 是 'D'(0x44)?  ──yes──> conf_index<=100
│
└─ 都不是（说明这是「参数字节」）:
        case(conf_index):
          001 (A): timebase  <= 字节[5:2];   conf_index<=0   （取中 4 位）
          010 (B): trig_value<= 字节;        conf_index<=0   （整字节）
          011 (C): 若字节=='H' slope_adj<=1; 若=='L' slope_adj<=0;  conf_index<=0
          100 (D): adj       <= 字节[2:0];   conf_index<=0   （取低 3 位）
          （000：无匹配，丢弃该字节）
```

关键点：**收到命令字母时不立即生效，只是把 `conf_index` 置位**；真正的参数要等下一个字节到来时，在 `case(conf_index)` 里才被写入对应寄存器，写完立刻把 `conf_index` 清零，回到「等命令字母」状态。

> 一个边界情况：如果在 `conf_index != 0`（正等参数）时又收到一个命令字母（比如发了 `A` 之后还没发参数就发了 `B`），那么这个字母会命中前面的 `else if` 分支，把 `conf_index` 改成新字母对应的值——也就是说**后到的命令字母会覆盖还没完成的配置**，协议是「最新命令字母优先」。

#### 4.2.3 源码精读

命令解析全部在 `wait_state` 的 `if (dserial_avail && rx_allowed)` 块里 —— [TOP.v:277-307](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L277-L307)：

```verilog
if (dserial_avail && rx_allowed) begin
    if(dserial_in == 8'b01010000) state<=trig_state;          // 'P' = 0x50
    else if(dserial_in == 8'b01000001) conf_index<=3'b001;    // 'A' = 0x41
    else if(dserial_in == 8'b01000010) conf_index<=3'b010;    // 'B' = 0x42
    else if(dserial_in == 8'b01000011) conf_index<=3'b011;    // 'C' = 0x43
    else if(dserial_in == 8'b01000100) conf_index<=3'b100;    // 'D' = 0x44
    else begin
        case(conf_index)
            3'b001: begin timebase<=dserial_in[5:2]; conf_index<=3'b000; end      // A: 时基
            3'b010: begin trig_value<=dserial_in;    conf_index<=3'b000; end      // B: 触发电平
            3'b011: begin conf_index<=3'b000;                                         // C: 斜率
                         if(dserial_in==8'b01001100) slope_adj<=1'b0;  // 'L'
                         else if(dserial_in==8'b01001000) slope_adj<=1'b1; // 'H'
                     end
            3'b100: begin conf_index<=3'b000; adj<=dserial_in[2:0]; end           // D: 增益
        endcase
    end
end
```

逐条解读：

- **`dserial_avail`** 是 `serial_rx` 收完一帧后拉高的单拍脉冲（u4-l1），`dserial_in` 是那一拍上的字节。所以整个解析在每个新字节到来的一拍内完成。
- **`P`（`8'b01010000` = 0x50）**：唯一一个直接改 `state` 的命令，立即跳 `trig_state`，不需要第二个字节。
- **`A`/`B`/`C`/`D`**：只置 `conf_index`，不改任何配置寄存器，也不改 `state`（仍留在 `wait_state`）。
- **参数字节**：走 `else` 分支的 `case(conf_index)`，根据当前等待的参数类型，把字节的不同位段写入对应寄存器：
  - `A` → `timebase <= dserial_in[5:2]`：取字节中间 4 位作为时基编码（时基经 `Transcoder` 译码成分频值，见 u2-l1）。
  - `B` → `trig_value <= dserial_in`：整个字节作为触发电平（8 位，对应 `adc_read[9:2]` 的高 8 位）。
  - `C` → 把字节当 ASCII 解释：`'H'`（0x48）选上升沿触发（`slope_adj=1`），`'L'`（0x4C）选下降沿（`slope_adj=0`）。
  - `D` → `adj <= dserial_in[2:0]`：取低 3 位作为模拟前端增益/衰减控制（见 u6-l3）。

`conf_index` 的声明是 3 位宽 —— [TOP.v:85](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L85)：

```verilog
reg [2:0] conf_index;            //indcator for which adjustment is wanted
```

> **关于 `rx_allowed`**：它在 [TOP.v:44](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L44) 声明为 `reg rx_allowed=1'b1;`，在 [TOP.v:277](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L277) 作为条件被读取，但**全文没有任何地方给它赋过新值**。也就是说它永远是 1，这个「接收允许」标志实际上是**失效的死代码**，条件 `dserial_avail && rx_allowed` 等价于 `dserial_avail`。阅读时可以忽略它。

#### 4.2.4 代码实践：A/B/C/D/P 命令行为表

这是本讲的主实践任务（**源码阅读型**）。

1. **实践目标**：用一张表说清收到 `A/B/C/D/P` 后系统分别进入什么行为、是否需要再收一个字节、参数怎么用。
2. **操作步骤**：
   - 对照 [TOP.v:277-307](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L277-L307)，把每个命令字母的 ASCII 码、置的 `conf_index`、是否需第二字节、第二字节如何使用，填进表里。
   - 再查 `timebase`（[TOP.v:83](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L83)）、`trig_value`（[TOP.v:78](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L78)）、`slope_adj`（[TOP.v:82](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L82)）、`adj`（[TOP.v:29](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L29)）这些目标寄存器的位宽，确认参数位段是否合理。
3. **需要观察的现象**：`P` 与 `A/B/C/D` 在「是否改 `state`」「是否需第二字节」上的差别。
4. **预期结果**：应得到下表。

| 命令字节 | ASCII | 十六进制 | 置 `conf_index` | 是否需第二字节 | 第二字节如何使用 | 系统行为 |
|---|---|---|---|---|---|---|
| `P` | P | 0x50 | 不置（立即） | **否** | — | 立即跳 `trig_state`，开始一次采集 |
| `A` | A | 0x41 | `001` | **是** | `timebase ← 字节[5:2]`（4 位时基） | 仅置 `conf_index`，仍留 `wait_state` |
| `B` | B | 0x42 | `010` | **是** | `trig_value ← 字节`（8 位触发电平） | 仅置 `conf_index` |
| `C` | C | 0x43 | `011` | **是** | `'H'`(0x48)→`slope_adj=1`（上升沿）；`'L'`(0x4C)→`slope_adj=0`（下降沿） | 仅置 `conf_index` |
| `D` | D | 0x44 | `100` | **是** | `adj ← 字节[2:0]`（3 位增益） | 仅置 `conf_index` |

5. **拓展观察**：用一张时序草图把一次完整的「配置 + 触发」串起来，例如要设「时基=5、上升沿触发、然后采集」，PC 应依次发送：`A` → `(某字节，其[5:2]=0101)` → `C` → `H` → `P`。注意 `A` 和 `C` 各占两个字节，`P` 单字节。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `P` 是单字节命令，而 `A/B/C/D` 都需要两个字节？
> **答案**：`P` 只表达「开始采集」这一个动作，不需要额外参数，所以一个字节够了。`A/B/C/D` 既要表达「改哪个参数」，又要携带「参数值」，一个字节装不下两份信息，所以拆成两步：第一字节选参数（置 `conf_index`），第二字节给值。

**练习 2**：如果在 `conf_index==001`（正等 `A` 的参数）时，PC 发来的不是参数而是一个杂乱字节 `0x00`，会发生什么？
> **答案**：`0x00` 不等于 `P/A/B/C/D` 任何一个，所以进入 `else` 的 `case(conf_index)`，命中 `3'b001` 分支，执行 `timebase <= 0x00[5:2] = 0`，然后把 `conf_index` 清零。也就是说杂乱字节会被当成 `A` 的参数（时基=0）「误吞」。协议**没有校验**，靠 PC 端保证不发错——这是一个值得注意的鲁棒性弱点。

**练习 3**：`C` 命令的第二字节为什么用 `'H'`/`'L'` 两个 ASCII 字符，而不是 0/1？
> **答案**：这是作者的协议选择，用 `H`(High，上升沿)/`L`(Low，下降沿) 让上位机代码更可读、串口抓包也更直观。代价是 `C` 的参数只认这两个特定字节，其他值会被静默忽略（`slope_adj` 保持不变）。

---

## 5. 综合实践

把本讲两块内容（主状态机 + 命令协议）串起来，做一次**纸上推演**。

**任务**：假设你是上位机程序，要让示波器完成这样一次测量——

> 设时基为最快档（`timebase=0`，对应 100 MSPS）、触发电平设为中点（`trig_value=128`）、选择上升沿触发、模拟前端增益设为 `adj=3'b010`，然后启动采集。

请完成：

1. **写出 PC 应依次发送的串口字节序列**（用十六进制和 ASCII 两种形式）。提示：`A` 的参数字节要满足 `[5:2]==0`；`B` 的参数字节就是 128；`C` 的参数是 `'H'`；`D` 的参数字节低 3 位是 `010`。
2. **画出从发送最后一个字节 `P` 开始，主状态机 `state` 依次经过的状态序列**，直到回到 `wait_state`，并标注每个状态的跳转条件（参考 4.1.4 的表）。
3. **指出在哪几个状态会用到本讲之外的子状态机/模块**：例如 `trig_state` 依赖谁算 `slope`？`acq_state` 期间是谁在写 ram1？`square_state` 期间在等谁的握手？

**参考答案要点**：

1. 字节序列（一种可行编码）：
   - `A`（0x41）→ 参数：`timebase[5:2]=0000`，可取字节 `0x00`（[5:2]=0）。
   - `B`（0x42）→ 参数 `0x80`（=128）。
   - `C`（0x43）→ 参数 `'H'`（0x48）。
   - `D`（0x44）→ 参数：低 3 位 `010`，可取字节 `0x02`。
   - `P`（0x50）→ 触发。
   - 即 `0x41,0x00, 0x42,0x80, 0x43,0x48, 0x44,0x02, 0x50`。
2. `P` 之后的状态序列：`wait_state → trig_state →（命中或超时）acq_state → fft_state → fft_write_state → square_state↔square_state2~5（自循环）→ send_state → final_state → send_state2 → send_state3 → wait_state`。
3. 依赖关系：
   - `trig_state` 用到的 `slope` 由 **ADC 时钟域的 `state2` 子状态机**计算（u5-l2）。
   - `acq_state` 期间真正写 ram1 的也是 **`state2`**（它生成 ram1 写地址 `ADR`），`we` 只是写使能闸门。
   - `square_state` 等的是 **CORDIC 开方核的 `sqr_rdy`** 握手（u3-l3）。

---

## 6. 本讲小结

- 主状态机 `state` 是整个系统的调度核心，跑在 200 MHz 的 `clk` 上，共 15 个状态，按 `空闲 → 触发 → 采集 → FFT → 开方 → 上传` 五组职责推进。
- 它通过控制 `we / we2 / we3` 三个写使能，保证三块 RAM 在同一时刻只被一个模块写入，避免数据冲突。
- 关键跳转条件都来自握手信号：`carry`（ram1 满）→ 进 FFT；`edone`（FFT 完成）→ 卸载；`cnt_s>=1023`（开方完）→ 上传；读地址到终值 → 回 `wait_state`。
- PC 用 ASCII 字符协议控制 FPGA：`P` 是单字节立即触发命令；`A/B/C/D` 是两字节配置命令，分别配时基、触发电平、斜率、增益。
- 两步式解析靠 3 位寄存器 `conf_index`：命令字母置位、参数字节消费并清零；协议无校验、靠 PC 端自律。
- `rx_allowed` 是一处失效的死代码（恒为 1），`trig_value` 声明处有一处位宽不匹配的字面量瑕疵（被 `init_state` 覆盖），阅读时需留意。

## 7. 下一步学习建议

- **u5-l2 触发与斜率子状态机**：本讲把 `trig_state` 用到的 `slope` 当成「别人算好的」，下一讲就去看 ADC 时钟域的 `state2` 子状态机如何用 `trig1/trig2` 算斜率、如何按 `out_trans` 分频寻址写 ram1。
- **u5-l3 发送打包子状态机与 LED 调试**：本讲把上传阶段当成「发完就回」，下一讲去看 `state3` 子状态机如何给数据加 `F`/`T` 帧头、如何在波形与频谱之间切换。
- **回头巩固**：如果想再确认 `timebase` 怎么变成实际采样率，复习 u2-l1（`Transcoder` + `ADC_clock_mux`）；想确认 `adj` 怎么影响模拟前端，预习 u6-l3。
