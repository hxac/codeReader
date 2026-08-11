# PC 命令协议与 LabVIEW GUI 集成

## 1. 本讲目标

本讲是一篇「系统级」讲义。前面几讲（u5-l1 主状态机、u5-l3 发送打包子状态机、u4-l2 UART 发射机）已经把 FPGA **内部**怎么做讲透了；本讲不再深入 HDL 细节，而是站到**链路两端**，把 FPGA 和上位机（PC）看作一对通过串口对话的「协议对端」。

学完本讲你应该能够：

- 用一张「协议规格表」描述 PC→FPGA 的命令（`P / A / B / C / D`）和 FPGA→PC 的数据帧（频谱段 + `F/F/T` 分隔符 + 波形段）。
- 在**字节级**还原线上的真实数据流：一个 10 位样本如何被拆成两字节、帧头字母如何编码、上位机如何据此重建样本与对齐帧边界。
- 说清 LabVIEW GUI 在这个协议里扮演的角色，并知道为什么它的具体实现只能标注「待确认」。
- 独立写出一份上位机测试脚本（伪代码），完成「发命令、收帧、解析频谱与波形」的完整闭环。

## 2. 前置知识

本讲默认你已读过以下讲义，下面只做最简回顾，不重复其细节：

- **u4-l2 UART 发射机**：`serialt` 把 16 位 `status` 拆成「低字节在前、高字节在后」两字节发出；`start` 脉冲是「装下一组数据」的节拍。
- **u5-l1 主采集状态机与命令协议**：主 FSM 在 `wait_state` 解析 PC 来的字符，`P` 立即触发采集，`A/B/C/D` 是「两步式」配置命令；`conf_index` 是两步解析的核心寄存器。
- **u5-l3 发送打包子状态机与 LED 调试**：`state3` 在上传阶段插入 `F/F/T` 帧头，整帧顺序是「频谱段 → F/F/T → 波形段」。

几个本讲会用到的术语：

- **协议对端（protocol peer）**：通信双方各自实现同一份约定的一半。本讲里 FPGA 是一端，LabVIEW GUI（或你写的脚本）是另一端。
- **帧（frame）**：一次完整上传的数据集合，以 `F/F/T` 为内部边界分成频谱段与波形段。
- **字节对（byte pair）**：`serialt` 每次发 2 字节，所以线上的数据天然按「2 字节一组」排列，这是上位机解析的基本单位。
- **二进制工程文件**：`.vi`（LabVIEW）、`.bit`（比特流）、`.PcbDoc`（Altium）等无法用文本编辑器阅读的产物，需专用工具打开。

> 提醒：本仓库文件名与模块名经常错位（详见 u1-l2），但本讲引用的 `TOP.v`、`readme.md`、`nexys_serial_XUP.vi` 文件名与内容一致，可直接对照。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲解度 |
| --- | --- | --- |
| `verilog files/TOP.v` | 顶层编排器，**协议的 FPGA 端实现**全部在此 | 命令解析（`wait_state`）、帧打包（`state3`）、串口例化 |
| `readme.md` | 项目说明，提到 `sw contains the LabView GUI` 与「Open the GUI」 | 推断 GUI 在系统中的角色 |
| `LabView GUI/nexys_serial_XUP.vi` | LabVIEW 上位机工程（**二进制**） | 文件类型已确认，内部实现待确认 |

## 4. 核心概念与源码讲解

本讲含两个最小模块：**串口命令/帧协议**、**LabVIEW 上位机（二进制，待确认）**。

### 4.1 串口命令/帧协议：PC 与 FPGA 之间的双向契约

#### 4.1.1 概念说明

整个数据采集系统是一个「**PC 主问、FPGA 主答**」的主从结构：

- **下行（PC→FPGA）是控制信道**：PC 发 ASCII 字符告诉 FPGA「开始采集」「换时基」「调触发电平」等。下行数据量极小（一次一两个字节），但决定了 FPGA 何时、以何种参数工作。
- **上行（FPGA→PC）是数据信道**：FPGA 把一整帧（频谱 + 波形）以连续字节流回送给 PC。上行数据量大（上千个样本），是 GUI 用来画图的原材料。

这条链路的物理载体是串口（UART），在 FPGA 侧分两半：接收机 `serial_rx` 接下行、发射机 `serialt` 发上行。注意顶层端口方向——[verilog files/TOP.v:24](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L24) 是 FPGA 的串行输入（RX，接 PC 的 TX），[verilog files/TOP.v:28](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L28) 是 FPGA 的串行输出（TX，接 PC 的 RX）。经 Nexys 4 DDR 板上的 USB↔串口桥（MCP2200，见 u6-l3）转成 USB 后连到 PC。

本模块要回答的核心问题是：**这份「契约」到底规定了什么？** 答案是两件事——下行命令的编码与两步式握手、上行帧的字节级封装。

#### 4.1.2 核心流程

一次完整的「问答」往返如下：

```text
        PC (LabVIEW GUI 或你的脚本)                       FPGA (TOP.v)
                │                                              │
  1. 发 'P' (0x50) ────────────────────────────────►  wait_state 识别 P → trig_state
                │                                              │
                │                                  触发/采集/FFT/开方/打包
                │                                              │
  2. （可选）发配置：'A'+字节 / 'B'+字节 / 'C'+'L|H' / 'D'+字节
                │  ──────────────────────────────►  wait_state 两步式解析
                │                                  （改 timebase/trig_value/slope_adj/adj）
                │                                              │
                │  ◄────────────────────────────  上行帧字节流：
                │     [频谱段 字节对...] [46 00][46 00][54 00] [波形段 字节对...]
  3. 按 F/F/T 分隔符切分，重建 10 位样本，画频谱与波形
```

下行的关键特征是**两步式**：`A/B/C/D` 不是「带参一步完成」，而是「先发命令字母（置位 `conf_index`），再发一个参数字节（消费 `conf_index`）」。这与 `P`（单字节立即生效）不同。上行的关键特征是**字节对封装**：每个 10 位样本占 2 字节，帧头字母也占 2 字节，因此整条流是「2 字节一组」的均匀结构。

#### 4.1.3 源码精读

**(a) 下行命令解析：`wait_state`**

下行接收机的例化见 [verilog files/TOP.v:104-107](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L104-L107)：`serial_rx` 把串行位还原成字节，以 `dserial_avail`（数据有效）+ `dserial_in`（8 位字节）握手交给顶层。

[verilog files/TOP.v:44](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L44) 的 `rx_allowed` 初值为 1，且全文件再无赋值——所以接收通道在 `wait_state` **始终打开**，协议完全靠字节内容驱动，没有额外使能门。

命令解析核心在 [verilog files/TOP.v:277-310](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L277-L310)。先看命令字母识别：

```verilog
if (dserial_avail && rx_allowed) begin
  if      (dserial_in == 8'b01010000) state<=trig_state;   // 'P'  0x50 → 立即触发
  else if (dserial_in == 8'b01000001) conf_index<=3'b001;  // 'A'  0x41 → 准备收时基参数
  else if (dserial_in == 8'b01000010) conf_index<=3'b010;  // 'B'  0x42 → 准备收触发电平
  else if (dserial_in == 8'b01000011) conf_index<=3'b011;  // 'C'  0x43 → 准备收斜率方向
  else if (dserial_in == 8'b01000100) conf_index<=3'b100;  // 'D'  0x44 → 准备收增益
  else begin case(conf_index) ... endcase                  // 不是命令字母 → 当参数消费
```

再看参数消费分支 [verilog files/TOP.v:284-306](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L284-L306)，每个 `conf_index` 对应一种配置，消费后清零：

| 命令 | 第 1 字节 | 第 2 字节（参数） | FPGA 行为 | 源码 |
| --- | --- | --- | --- | --- |
| `P` | `0x50` 'P' | 无 | 进入 `trig_state`，开始一次采集 | [TOP.v:279](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L279) |
| `A`（时基） | `0x41` 'A' | 任意字节，取 `dserial_in[5:2]`（4 位） | 写入 `timebase`，改变采样分频 | [TOP.v:286](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L286) |
| `B`（触发电平） | `0x42` 'B' | 任意字节（8 位） | 整字节写入 `trig_value` | [TOP.v:291](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L291) |
| `C`（斜率方向） | `0x43` 'C' | `0x4C` 'L' 或 `0x48` 'H' | `slope_adj` 置 0（下降沿）或 1（上升沿） | [TOP.v:297-298](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L297-L298) |
| `D`（增益） | `0x44` 'D' | 任意字节，取 `dserial_in[2:0]`（3 位） | 写入 `adj`，控制模拟前端放大/衰减 | [TOP.v:303](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L303) |

把上表整理成「协议规格表」，就是交给上位机程序员的契约：

```text
下行帧（PC → FPGA），无起始/停止标记、无校验、靠 PC 自律：
  触发：    50
  配置时基：41 <X>          X[5:2] → timebase
  配置电平：42 <Y>          Y      → trig_value
  配置斜率：43 4C | 43 48   'L'=下降沿 / 'H'=上升沿
  配置增益：44 <Z>          Z[2:0] → adj
```

注意三个**易踩的坑**（都对上位机实现有直接影响）：

1. **两步之间不能插入别的字节**。`conf_index` 是「记忆」：发了 `A` 后，下一个**非命令字母**的字节会被当成时基参数。若 PC 误发，状态会错位。
2. **命令字母优先**。若发了 `A` 之后又发一个 `B`（而不是参数），代码会走「命令字母」分支，把 `conf_index` 改成 `010`，原先的 `A` 被丢弃——所以参数字节本身**不能等于 `0x50/0x41/0x42/0x43/0x44`**，否则会被误解为新命令。
3. **协议无校验、无显式 ACK**。FPGA 收到命令后不会回执；PC 只能靠随后到来的上行帧确认「确实触发了」。

**(b) 上行帧封装：`state3` 与 `serialt`**

上行发射机例化见 [verilog files/TOP.v:110-117](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L110-L117)：16 位寄存器 [verilog files/TOP.v:38](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L38) 是发送缓冲，`serialt` 每收到一个 `start` 节拍就把 `aggregated` 拆成两字节发走（低字节先、高字节后，见 u4-l2）。

每拍装入什么，由 [verilog files/TOP.v:416-457](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L416-L457) 决定。关键代码：

```verilog
// 频谱样本（send_state / final_state 期间）：10 位样本塞进低 10 位
aggregated[9:0]  <= data_send;          // 见 TOP.v:421-422 与 427-428
aggregated[15:10]<= 6'b000000;

// 帧头字母（state3 的 s3_1/s3_2/s3_3）
s3_1: begin aggregated[7:0] <= 8'b01000110; aggregated[15:8] <= 8'b00000000; end // 'F'=0x46
s3_2: begin aggregated[7:0] <= 8'b01000110; aggregated[15:8] <= 8'b00000000; end // 'F'=0x46
s3_3: begin aggregated[7:0] <= 8'b01010100; aggregated[15:8] <= 8'b00000000; end // 'T'=0x54

// 波形样本（s3_4 稳态）
s3_4: begin aggregated[9:0] <= buffer; aggregated[15:10] <= 6'b00000000; end     // 见 TOP.v:450-454
```

由此可推出**字节级线上格式**。设某 10 位样本为 \(S = S_{9..0}\)，`serialt` 把 `aggregated` 拆成两字节发出：

\[
\text{byte}_0 = \text{aggregated}_{7..0} = S_{7..0}, \qquad
\text{byte}_1 = \text{aggregated}_{15..8} = \{6'b0,\ S_{9..8}\}
\]

所以重建公式为：

\[
S = \{\text{byte}_1[1{:}0],\ \text{byte}_0[7{:}0]\}
\]

注意 \(\text{byte}_1\) 只取低 2 位（范围恒为 `0x00~0x03`）。对帧头字母，`aggregated[7:0]` 是 ASCII、`aggregated[15:8]=0`，所以 `'F'` 在线上是 `46 00`、`'T'` 是 `54 00`。

把整帧拼起来，PC 看到的字节流是这样的（每行 = 一个 2 字节组）：

```text
[ 频谱段 ]   data_send[0] : {00, data_send[0][9:8]}      ← 低字节:高字节
            data_send[1] : {00, data_send[1][9:8]}
            ...
[ 分隔符 ]   46 00   ← 'F'
            46 00   ← 'F'
            54 00   ← 'T'
[ 波形段 ]   buffer[0] : {00, buffer[0][9:8]}
            buffer[1] : {00, buffer[1][9:8]}
            ...
```

两个对上位机很关键的观察：

- **数据样本的高字节恒为 `0x00~0x03`**，而帧头字母的低字节是 `0x46/0x54`、高字节是 `0x00`。这意味着 `0x46` 只可能出现在「帧头字母位」或「数据样本低字节位」——单看一个 `0x46` 无法判定角色。
- **可靠的帧分隔符是完整的 6 字节模式 `46 00 46 00 54 00`**。因为数据样本的高字节不会是 `0x00` 配 `0x46`（高字节最大 `0x03`），这串「F,F,T」三连不会和数据样本混淆，是 PC 切分频谱段与波形段的唯一可靠锚点。

至于频谱段、波形段各多少点，由主 FSM 上传四态的读地址上限决定（`send_state` 在 `ADR_r==127` 切到 `final_state`、`final_state` 在 `ADR_r==2047` 切到 `send_state2`、`send_state2` 在 `ram_read==255`、`send_state3` 在 `ram_read==2047`，见 [TOP.v:394-411](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L394-L411)）。精确点数与 `cnt`/`cnt_waveform` 的起始值有关，已在 u5-l3 讨论，本讲不重复，实际点数**待本地验证**。

最后，TOP.v 头部注释把这条链路概括成两句：第 16 行「LabView displays the waveforms」（[TOP.v:16](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L16)）、第 17 行「Another process is started in FPGA, just when it is requested by PC」（[TOP.v:17](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L17)）——正好对应「PC 问（下行命令）→ FPGA 答（上行帧）→ LabVIEW 画」。

#### 4.1.4 代码实践

> 本仓库**没有**提供上位机脚本（GUI 是二进制 `.vi`）。下面的脚本属于**示例代码**，用来把上面的协议契约落地成可运行的伪代码。项目未提供运行环境，运行结果**待本地验证**。

**实践目标**：用一份 Python 风格伪代码实现协议的「PC 端对端」，验证你是否真正理解了下行的两步式命令与上行的字节对封装。

**操作步骤**：

1. 阅读下面的伪代码，对照 4.1.3 的协议表，逐行解释每条 `write` 发出的字节含义。
2. 找到 `reconstruct_sample`，验证它实现了 \(S=\{\text{byte}_1[1{:}0],\text{byte}_0\}\)。
3. 找到帧切分逻辑，解释为什么必须匹配 6 字节 `46 00 46 00 54 00` 而不是单个 `0x46`。

```python
# ===== 示例代码（伪代码，非项目自带）=====
# PC 端协议对端：发命令、收帧、解析频谱与波形
import serial, time

PORT, BAUD = "/dev/ttyUSB0", 1562500   # 波特率约 1.5625 Mbaud，待本地验证（见 u4-l1/u4-2）

def cmd_trigger(ser):                  # P：单字节立即触发
    ser.write(bytes([0x50]))

def cmd_timebase(ser, tb):             # A：<X>，X[5:2] -> timebase，故把 tb 放到 bit[5:2]
    x = (tb & 0x0F) << 2               # 例：tb=3 -> X=0x0C
    assert x not in (0x50,0x41,0x42,0x43,0x44), "参数字节撞上命令字母！"
    ser.write(bytes([0x41, x]))

SEPARATOR = bytes([0x46,0x00, 0x46,0x00, 0x54,0x00])   # F F T 的完整 6 字节模式

def reconstruct_sample(lo, hi):        # S = {hi[1:0], lo[7:0]}
    return ((hi & 0x03) << 8) | lo

def receive_frame(ser):
    buf = bytearray()
    while SEPARATOR not in buf:        # 先读到完整的 F/F/T 分隔符
        buf += ser.read(64)
    idx = buf.index(SEPARATOR)
    spec_bytes  = buf[:idx]            # 分隔符之前 = 频谱段
    wave_bytes  = buf[idx+len(SEPARATOR):]   # 分隔符之后 = 波形段
    spectrum = [reconstruct_sample(spec_bytes[i], spec_bytes[i+1])
                for i in range(0, len(spec_bytes),  2)]
    waveform = [reconstruct_sample(wave_bytes[i], wave_bytes[i+1])
                for i in range(0, len(wave_bytes), 2)]
    return spectrum, waveform

# —— 主流程 ——
ser = serial.Serial(PORT, BAUD, timeout=1)
cmd_timebase(ser, 0)                   # 先把时基设为 0（100 MSPS 档，见 u2-l1）
cmd_trigger(ser)                       # 再触发一次采集
time.sleep(0.2)
spec, wave = receive_frame(ser)
print(f"频谱点数={len(spec)}  波形点数={len(wave)}")
```

**需要观察的现象 / 预期结果**：

- `spectrum` 的每个值应在 `0..1023`（10 位）范围内；`waveform` 同理。
- 若把 `SEPARATOR` 改成单字节 `b'\x46'`，频谱段里凡低字节恰好等于 `0x46` 的样本都会被误判为分隔符，解析立刻错位——这验证了「必须用 6 字节模式」的结论。
- 若故意发 `cmd_timebase(ser, 0x14)` 让参数字节等于 `0x50`，FPGA 会把它当成新的 `P` 命令而不是参数——验证 4.1.3 的「坑 2」。
- 实际波特率、点数、是否能稳定同步，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：PC 想把触发电平设到 8 位的 `0x80`，应该发哪几个字节？为什么参数字节可以放心等于 `0x80`？

> **答案**：发 `0x42 0x80`（`B` 命令 + 参数）。`0x80` 不是命令字母（`0x50/0x41/0x42/0x43/0x44`），所以会被 `case(conf_index)` 的 `3'b010` 分支整字节写入 `trig_value`，不会误触发新命令。

**练习 2**：线路上连续收到 `0C 00 46 00 46 00 54 00 2A 01`，请还原含义。

> **答案**：按字节对分组——`(0C,00)` 是一个频谱样本 \(S=\{00_2, 0C\}=12\)；`(46,00)(46,00)(54,00)` 是 `F/F/T` 分隔符；`(2A,01)` 是一个波形样本 \(S=\{01_2, 2A\}=0x12A=298\)。即频谱段末尾值为 12，分隔符之后波形段开头值为 298。

**练习 3**：为什么不能用「等待固定字节个数」来切分频谱段与波形段，而要用 `F/F/T` 分隔符？

> **答案**：因为频谱段与波形段的点数由主 FSM 的读地址上限决定，且 `cnt`/`cnt_waveform` 是否每帧归零并不显然（本讲标注待验证）；同时串口流没有显式帧起始/结束标记。靠「内容模式」`46 00 46 00 54 00` 定位是唯一与数据值无关的可靠锚点。

---

### 4.2 LabVIEW 上位机：协议的另一端（二进制，待确认）

#### 4.2.1 概念说明

协议有两个对端：一端是 FPGA（已在 4.1 讲清），另一端就是 LabVIEW GUI。从协议角度看，GUI 要做三件事，正好是 4.1 里 PC 这一侧的全部职责：

1. **下行**：响应用户操作（点「开始」、拉时基滑块、选触发沿），翻译成 `P / A / B / C / D` 字节序列发出去。
2. **上行**：接收字节流，按 `F/F/T` 切分，重建 10 位频谱与波形样本。
3. **呈现**：把频谱段画成频谱图、把波形段画成时域波形，刷新显示。

换言之，GUI 就是「协议的人机界面」。它本身**不参与采样和 DSP**——那些都在 FPGA 里完成。GUI 失灵，FPGA 照常采集；FPGA 断电，GUI 也就无数据可画。两者唯一的耦合就是这条串口协议。

#### 4.2.2 核心流程

GUI 侧的运行循环（推断）：

```text
事件循环：
  用户操作 ──► 生成下行命令字节 ──► 串口写
  串口读线程 ──► 累积字节 ──► 找 F/F/T 分隔符 ──► 重建样本 ──► 刷两个波形图表
```

这与 4.1.4 的伪代码是同一件事的图形化版本——LabVIEW 用「数据流连线」代替文本代码，但协议契约完全相同。

#### 4.2.3 源码精读

LabVIEW 工程是 [LabView GUI/nexys_serial_XUP.vi](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/LabView%20GUI/nexys_serial_XUP.vi)。`file` 命令确认其类型为「National Instruments, LabVIEW File, Virtual Instrument Program」——这是一个**二进制工程文件**，必须用 LabVIEW 打开，无法用文本编辑器或本讲的方式阅读其内部框图。

因此，关于 GUI 的具体实现（用了哪些 VI、串口配置参数、是否正好按 6 字节模式解析、采样率/触发 UI 长什么样），本讲只能**推断**其角色、**不能**给出源码级结论，统一标注**待确认**。能从文本来源确认的只有两点：

- [readme.md:25](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/readme.md#L25) 写明 `sw contains the LabView GUI`——GUI 属于软件部分。
- [readme.md:33-34](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/readme.md#L33-L34) 给出复现第 3 步「Open the GUI, and connect the board to PC」——GUI 是系统就位的最后一块。

加上 TOP.v 注释 [TOP.v:16](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L16)「LabView displays the waveforms」，可以确认 GUI 的职责就是「显示波形（含频谱）」。

> 这也说明一个工程现实：**FPGA 端的协议是可读源码（`TOP.v`），是本系统的「协议事实标准」**；而 GUI 是二进制，理解协议应以 `TOP.v` 为准。如果 GUI 与 `TOP.v` 行为不一致，应以 `TOP.v` 为准去复现/调试 GUI。

#### 4.2.4 代码实践

> 这是「源码阅读/推理型实践」，无需运行 LabVIEW。

**实践目标**：把 4.1 的协议规格与 GUI 的角色对上号，检验你是否能独立描述协议两端。

**操作步骤**：

1. 打开 [TOP.v:277-310](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L277-L310)，列出一个 GUI「时基滑块」控件应当发送的字节序列模板。
2. 假设 GUI 上有「触发电平」旋钮（范围 0~255），写出它每次旋动应发的两字节。
3. 假设 GUI 频谱图突然变成「乱码」（随机点），列出至少 3 个可能原因，并区分哪些在 FPGA 侧、哪些在 GUI 侧。

**需要观察的现象 / 预期结果**：

- 第 1 步应得到「`0x41 <X>`，且把滑块值映射到 `X[5:2]`（4 位时基）」。
- 第 2 步应得到「`0x42 <旋钮值>`」。
- 第 3 步的可能原因示例：FPGA 侧——波特率不匹配、`adc_read` 接线松动、`decoder` 编码异常；GUI/链路侧——串口波特率/校验位不符、字节对齐错位（没用 6 字节分隔符）、USB↔串口桥丢字节。**待本地验证**具体根因。

#### 4.2.5 小练习与答案

**练习 1**：为什么说「即使打不开 `.vi`，我们仍能完整描述 GUI 的协议职责」？

> **答案**：因为协议由可读的 `TOP.v` 单方面定义——下行命令的字节编码、上行帧的字节级封装都在源码里。GUI 只是这个协议的「另一端实现」，其职责（发命令、收帧、画图）由协议决定，不依赖 `.vi` 的内部框图。`.vi` 的具体实现细节才需要标注「待确认」。

**练习 2**：若想在 GUI 上加一个「触发沿选择」单选框（上升/下降），它应当映射到协议的哪条命令？

> **答案**：`C` 命令。上升沿发 `0x43 0x48`（`'C' 'H'`），下降沿发 `0x43 0x4C`（`'C' 'L'`），对应 [TOP.v:297-298](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L297-L298) 的 `slope_adj` 置 1/0。

## 5. 综合实践

**任务**：为本系统设计一份《PC–FPGA 串口协议说明文档》草稿，并配套一份可读的解析验证用例。

要求：

1. 用一张「下行命令表」覆盖 `P / A / B / C / D`，列出每条的 ASCII、字节数、参数位宽、FPGA 行为、对应源码行号。
2. 用一张「上行帧结构图」画出「频谱段 → `46 00 46 00 54 00` → 波形段」，并写出 10 位样本的重建公式 \(S=\{\text{byte}_1[1{:}0],\text{byte}_0\}\)。
3. 构造一段**虚构的**线上字节流（至少含 3 个频谱样本、完整分隔符、2 个波形样本），写出它解码后的数值列表，并标明这是你构造的示例数据而非真实抓包。
4. 在文档末尾单列「待确认」清单：`.vi` 内部实现、精确波特率、频谱/波形段精确点数、跨时钟域握手是否需要加固。

完成后，你应当能拿着这份文档，在**不看 `TOP.v`** 的情况下，向别人解释清楚 PC 与 FPGA 之间如何对话——这正是系统级理解达标的标准。

## 6. 本讲小结

- 本讲把 FPGA 与 PC 视作**协议对端**：下行是控制信道（ASCII 命令），上行是数据信道（连续字节流帧）。
- 下行命令分两类：`P`（单字节立即触发）、`A/B/C/D`（两步式配置，先字母置 `conf_index`、再参数字节消费），**无校验、无 ACK、参数字节不能等于任何命令字母**。
- 上行帧是「频谱段 → `F/F/T` 分隔符 → 波形段」的字节流；每个 10 位样本占 2 字节（低字节先、高字节后），重建公式 \(S=\{\text{byte}_1[1{:}0],\text{byte}_0\}\)。
- `F/F/T` 在线上是 6 字节模式 `46 00 46 00 54 00`，是唯一可靠的帧切分锚点（数据样本高字节恒为 `0x00~0x03`，不会与帧头混淆）。
- LabVIEW GUI 是协议的另一端，负责发命令、收帧、画图；`.vi` 是二进制工程文件，内部实现**待确认**——**协议以可读的 `TOP.v` 为事实标准**。
- 本讲给出了完整的 PC 端伪代码（示例代码），把协议契约落地为「发 P、配时基、收帧、解析频谱与波形」的闭环。

## 7. 下一步学习建议

- 若你想把「协议无校验」这个弱点补上：阅读 u6-l4（扩展实践与改进方向），思考如何在本讲协议上加帧头/长度/校验，以及 GUI 侧如何配合。
- 若你对硬件链路（USB↔串口桥、模拟前端、多板连接）感兴趣：继续 u6-l3（硬件前端与多板系统），把本讲的「串口」落实为「传感器→模拟前端板→ADC→FPGA→MCP2200→PC」的完整物理通路。
- 若你想亲眼看到帧：在本地用 Vivado 烧录 `TOP.bit`、用逻辑分析仪抓 `serial_out`，对照本讲的字节级格式逐字节验证（波特率、点数等待本地验证项可一并落实）。
