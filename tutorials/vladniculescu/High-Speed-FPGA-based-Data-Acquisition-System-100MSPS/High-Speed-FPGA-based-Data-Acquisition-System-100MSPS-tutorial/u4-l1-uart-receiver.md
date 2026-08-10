# UART 接收机

## 1. 本讲目标

学完本讲，你应当能够：

- 说出标准 UART 异步串行帧的格式（起始位 / 8 数据位 / 停止位），并解释为什么接收端需要“自己按波特率节拍采样”。
- 读懂 VHDL 写的 `serial_rx` 模块：它如何用 `generic M` 把时钟分频成采样节拍 `rs_tick`，又如何用 `s_waiting`/`s_start`/`s_bit`/`s_stop` 四态状态机配合 16× 过采样完成一个字节的接收。
- 解释 `counter=7`（半位偏移）与 `counter=15`（整位）如何共同实现“中点对齐”，让每个数据位都在位单元正中央被采样。
- 看懂 `serial_rx` 与 TOP 主状态机之间的握手：`d_avail` 单拍脉冲通知“一个字节已就绪”，`d_out` 给出该字节，TOP 在 `wait_state` 里消费它。

本讲只聚焦“接收方向”（PC→FPGA）。发送方向（FPGA→PC）由 `serialt`/`serial_tx` 完成，留待下一讲 u4-l2。

## 2. 前置知识

在进入源码前，先用通俗语言把几个基础概念讲清楚。

**什么是 UART。** UART（通用异步收发器）是一种最简单的串行通信方式：只用一根线（接收 RX / 发送 TX）按位传送数据。它“异步”的含义是——收发双方没有共享的时钟线，接收方必须自己产生一个与发送方约定好速率的“采样节拍”（波特率）来逐位读取。

**UART 帧格式。** 线路空闲时为高电平（1）。发送一个字节时，帧的结构是：

| 区段 | 宽度 | 电平 | 含义 |
|------|------|------|------|
| 起始位 (start) | 1 位 | 0 | 把线拉低，通知接收方“帧来了” |
| 数据位 | 8 位 | 0/1 | 真正的载荷，**低位先发（LSB first）** |
| 停止位 (stop) | 1 位 | 1 | 把线拉回高，标志帧结束 |

所以一个字节帧共 10 位。起始位的下降沿是接收方唯一的“时间零点”同步信号。

**波特率与 16× 过采样。** 波特率（baud）就是每秒传送的位数，例如 115200 baud 表示每秒 115200 位。为了让采样更稳，接收方常用 16× 过采样：把每个位时间再切成 16 份（16 个采样节拍），在位单元的**正中央**取值——因为中央处信号最稳定，远离两端的跳变区。本工程的 `serial_rx` 正是 16× 过采样设计。

**为什么要“中点对齐”。** 起始位下降沿被检测到的时刻是随机的（可能在位内的任意位置）。如果从这一刻起“每 16 拍采一次”，采样点会逐位漂移，最后几位可能落到跳变区而读错。解决办法是：检测到下降沿后，先等**半个位**（8 拍）再开始正式采样——这样第一次采样恰好落在起始位中央，之后每 16 拍一次就稳定地落在每个数据位的中央。这就是本讲反复要讲的“中点对齐”。

**VHDL 极简速查。** 本模块用 VHDL 写（前几讲的 DSP 链路是 Verilog）。只需认识几样东西：

- `entity ... port(...)`：模块的端口声明，相当于 Verilog 的 `module` 端口列表。
- `generic ( M: integer := 2 )`：参数化常量，例化时可覆盖，相当于 Verilog 的 `#(.M(2))`。
- `signal`：内部信号，相当于 Verilog 的 `wire`/`reg`。
- `process(clk) ... if rising_edge(clk)`：时钟进程，相当于 Verilog 的 `always @(posedge clk)`。
- `<=`：信号赋值（非阻塞语义）。

## 3. 本讲源码地图

本讲涉及两个文件：

| 文件 | 语言 | 角色 |
|------|------|------|
| [vhdl files/serial_rx.vhd](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/serial_rx.vhd) | VHDL | UART 接收机本体：分频产生节拍 + 四态状态机收字节 |
| [verilog files/TOP.v](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v) | Verilog | 顶层：例化 `serial_rx`，并在 `wait_state` 消费收到的字节 |

记忆口诀（承接 u1-l2）：**文件名常与模块名不一致，读代码一律认 `module`/`entity` 关键字后的名字。** 这里 `serial_rx.vhd` 的 entity 名就是 `serial_rx`，比较一致；但 TOP 例化时把实例命名为 `receiver`，别被名字绕晕。

## 4. 核心概念与源码讲解

按“节拍产生 → 状态机收位 → 与 TOP 握手”三步，把 `serial_rx` 拆成三个最小模块来讲。

### 4.1 波特率节拍：rs_tick 的分频产生

#### 4.1.1 概念说明

UART 是异步的，接收方没有外部时钟可抄，只能拿本地系统时钟“分频”出一个固定速率的采样节拍。`serial_rx` 用一个 generic 参数 `M` 做整数分频：每 `M` 个系统时钟产生 1 个节拍脉冲 `rs_tick`。这个 `rs_tick` 就是 16× 过采样的“最小刻度”——16 个 `rs_tick` 正好等于一个位时间。

源码顶部的注释一语道破分频关系：

> `--system clock/16/M=> baud rate`（系统时钟 ÷ 16 ÷ M = 波特率）

#### 4.1.2 核心流程

节拍发生器是一个模 M 计数器，伪代码如下：

```
每个 clk 上升沿：
    若 cnt < M-1：rs_tick=0，cnt ← cnt+1
    否则（cnt 到顶）：rs_tick=1，cnt ← 0
```

于是 `rs_tick` 每 `M` 拍拉高一次，是一个占空比约 1/M 的脉冲。它的频率为：

\[
f_{\text{tick}} = \frac{f_{\text{clk}}}{M}
\]

而每位占 16 个 `rs_tick`，故波特率为：

\[
f_{\text{baud}} = \frac{f_{\text{tick}}}{16} = \frac{f_{\text{clk}}}{16 \cdot M}
\]

#### 4.1.3 源码精读

实体声明：端口 `input1`（串行位输入）、`clk`、`d_avail`（字节就绪标志）、`d_out`（字节输出），generic `M` 默认 2。

[vhdl files/serial_rx.vhd:8-14](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/serial_rx.vhd#L8-L14) —— 声明 `generic(M:=2)` 与四个端口；注释把波特率公式写在 `M` 旁边。

节拍信号 `rs_tick` 与计数器 `cnt` 的声明，注意 `cnt` 的取值范围是 `0 to M+1`：

[vhdl files/serial_rx.vhd:22-23](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/serial_rx.vhd#L22-L23) —— `signal rs_tick` 是节拍脉冲，`cnt` 是分频计数器。

节拍发生逻辑（紧跟在 `rising_edge(clk)` 之后、`case state` 之前，因此**每个时钟周期都执行**）：

[vhdl files/serial_rx.vhd:30-36](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/serial_rx.vhd#L30-L36) —— `cnt` 数到 `M-1` 翻转，产生 `rs_tick` 脉冲。

#### 4.1.4 代码实践

**实践目标：** 推导 `serial_rx` 的实际波特率。

**操作步骤：**

1. 确认驱动时钟：TOP 例化时 `.clk(clk_UART)`（见 4.3.3），而 `clk_UART` 由 PLL 的 `CLK_OUT4` 给出（u2-l1 已确认标注为 50 MHz，但 PLL 精确系数藏于二进制工程包）。
2. 取 `generic M` 默认值 `M=2`。
3. 套公式：\( f_{\text{tick}} = f_{\text{clk\_UART}}/M = 50\,\text{MHz}/2 = 25\,\text{MHz} \)。
4. 再算波特率：\( f_{\text{baud}} = 25\,\text{MHz}/16 = 1\,562\,500\,\text{baud} \approx 1.5625\,\text{Mbaud} \)。

**需要观察的现象 / 预期结果：** 1.5625 Mbaud 是一个**非标准**波特率（常见值是 9600、115200 等）。这意味着要么 `clk_UART` 的实际频率与 50 MHz 标注不符，要么本工程刻意用了高速非标波特。**波特率与 `clk_UART` 的精确值均为「待本地验证」**——需要在 Vivado 里打开 PLL IP 核确认 `CLK_OUT4` 的真实输出频率，或用逻辑分析仪/示波器在 `serial_in` 上实测。

> 说明：本讲义不假装已运行硬件，以上数字是基于源码可读部分的推导，结论性部分标注待确认。

#### 4.1.5 小练习与答案

**Q1：** 若想让波特率降低为原来的一半（其它不变），`M` 应改为多少？

**答：** 波特率 \( \propto 1/M \)，故波特率减半需把 `M` 翻倍，即 `M=4`。

**Q2：** `rs_tick` 是一个占空比很低的脉冲（每 `M` 拍才高 1 拍）。状态机为什么只在 `rs_tick='1'` 时才推进 `counter`，而不是每个 `clk` 都推进？

**答：** 状态机的 `counter` 数的是“位时间内的节数”，必须以 `rs_tick`（16× 节拍）为单位计数，才能保证 16 拍正好等于一位。若每个 `clk` 都数，节奏就比设计快 `M` 倍，位时间会缩短为 1/M，采样完全错位。

---

### 4.2 接收状态机：四态 FSM 与 16× 过采样中点对齐

#### 4.2.1 概念说明

`serial_rx` 的核心是一个四态状态机，沿 `input1` 这根串行线把一个字节“拼”出来：

- `s_waiting`：空闲，盯着线路，等起始位的下降沿。
- `s_start`：检测到起始位后，先等**半位**（8 拍）走到起始位正中央——这就是中点对齐的“对齐”动作。
- `s_bit`：在数据位正中央采样，连续采 8 个数据位，低位先收。
- `s_stop`：等过停止位（1 位），收尾，回到 `s_waiting`。

注意一个关键细节：`s_start` 里数到 `counter=7`（共 8 拍 = 半位），而 `s_bit`/`s_stop` 里数到 `counter=15`（共 16 拍 = 整位）。**前者提供初始的半位偏移，后者维持每 16 拍一位的节奏**——两者合起来才把采样点钉死在每个位的中央。这正是本讲实践任务要讲清楚的“用 counter 实现中点对齐”。

#### 4.2.2 核心流程

一次完整接收的时序流程（`↓` 表示下降沿，每格 = 1 个 `rs_tick`）：

```
线路空闲(1) ──┐ 下降沿(起始位 0)
              ↓
s_waiting ──> s_start : 数 counter 0→7（8 拍，半位），到达【起始位中央】
                          ↓
              s_bit   : 数 counter 0→15（16 拍/位），在 counter=15 采样位0
                        位0 … 位7（共 8 次，每次 16 拍），采样点都落在【各数据位中央】
                          ↓
              s_stop  : 数 counter 0→15（16 拍），到【停止位中央】，拉 d_avail，回 s_waiting
```

数据位装配用移位寄存器：每采一位，`out_bits <= input1 & out_bits(7 downto 1)`，即把新位塞进最高位、整体右移。由于 UART 低位先发，第一个收到的位（数据 LSB）经过 8 次右移最终落到 `out_bits` 的最低位——正好还原成正确的字节顺序。

#### 4.2.3 源码精读

状态类型与状态机骨架：

[vhdl files/serial_rx.vhd:17-22](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/serial_rx.vhd#L17-L22) —— 定义四态 `state_type` 与 `counter`（0~15）、`out_bits`（移位寄存器）、`bit_pos`（已收位数）等内部信号。

`s_waiting`：盯起始位下降沿。注意它**不看 `rs_tick`**，每个 `clk` 都检查，确保不漏掉起始位。

[vhdl files/serial_rx.vhd:40-48](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/serial_rx.vhd#L40-L48) —— `input1='0'` 即判为起始位，跳 `s_start`。

`s_start`：半位偏移。仅在 `rs_tick='1'` 时数 `counter`，到 `7` 即跳 `s_bit`。

[vhdl files/serial_rx.vhd:50-65](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/serial_rx.vhd#L50-L65) —— `counter` 数到 7 表示走了 8 拍（半位）。源码里有一段被注释掉的 `if (input1='0')`（第 54、62 行），说明作者**原本想在半位处复查起始位是否仍为 0 以防毛刺误触发，但最终注释掉了**——这是一处值得留意的健壮性细节。

`s_bit`：每 16 拍采一位。`counter=15` 时采样并右移 `out_bits`，`bit_pos=7` 表示收满 8 位，跳 `s_stop`。

[vhdl files/serial_rx.vhd:67-86](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/serial_rx.vhd#L67-L86) —— 采样语句 `out_bits <= input1 & out_bits(7 downto 1)`，LSB 先收的装配逻辑。

`s_stop`：过停止位，收尾。`counter=15` 时回 `s_waiting` 并拉高 `d_avail`。

[vhdl files/serial_rx.vhd:88-101](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/serial_rx.vhd#L88-L101) —— 收满一位停止位后，`d_avail<='1'` 通知 TOP。

#### 4.2.4 代码实践

**实践目标：** 手动跟踪接收一个字节 `0x41`（即字符 `'A'`，二进制 `0100_0001`），画出 `counter` 与采样点的关系。

**操作步骤：**

1. `0x41 = 0b01000001`，UART 帧低位先发，所以线上位序为：起始位 0，然后 `1,0,0,0,0,0,1,0`（LSB→MSB），停止位 1。
2. 在 `s_waiting` 检测到线变 0，跳 `s_start`，`counter` 从 0 数到 7（8 个 `rs_tick`）——到达**起始位中央**。
3. 跳 `s_bit`：第一个 `counter` 数到 15 时（16 拍后，到达**位0 中央**）采到 `1`，`out_bits=1000_0000`，`bit_pos=1`。
4. 第二个 16 拍后采到位1=`0`，`out_bits=0100_0000`，`bit_pos=2`。
5. ……重复，第 8 次采到位7=`0`，`out_bits=0100_0001`=`0x41`，`bit_pos` 到 7，跳 `s_stop`。
6. `s_stop` 数 16 拍后回 `s_waiting`，`d_avail` 拉高一拍。

**需要观察的现象：** 每个采样点都恰好落在对应位的中央（距上一位边沿 8 拍、距下一位边沿 8 拍），这正是 `counter=7` 半位偏移 + `counter=15` 整位节奏共同保证的。

**预期结果：** 收完后 `out_bits` = `0x41`，与发送的字节一致，验证了“中点对齐 + LSB 先收”的正确性。

> 若无法在硬件上跑，可对照源码人工推演上述 8 步采样，结论一致即算通过。

#### 4.2.5 小练习与答案

**Q1：** 为什么 `s_start` 里 `counter` 数到 **7** 就跳走，而 `s_bit` 里要数到 **15**？

**答：** `s_start` 的任务是走“半位”（8 拍）以对齐到起始位中央，`counter` 从 0 到 7 正好 8 个 `rs_tick`；`s_bit` 要走“整位”（16 拍）以从当前位中央走到下一位中央，`counter` 从 0 到 15 正好 16 个 `rs_tick`。两者单位相同（都以 `rs_tick` 计），但目标行程不同。

**Q2：** 如果把 `s_start` 的 `counter=7` 改成 `counter=15`（即也对齐到整位而不是半位），会发生什么？

**答：** 起始位检测后等的是 16 拍 = 整位，于是第一个数据位采样点会落在**位0 的起始边沿附近**（跳变区），后续每位采样点都偏移半位，极易采错。中点对齐失效。

**Q3：** 数据位装配为什么用 `input1 & out_bits(7 downto 1)`（右移）而不是左移？

**答：** 因为 UART 规定低位先发。先收到的位是数据的 LSB，右移能让它逐拍向低位挪动，经过 8 次后正好停在 bit0；最后收到的 MSB 停在 bit7。若用左移，位序会完全颠倒。

---

### 4.3 与 TOP 的握手：d_avail / d_out 字节交付

#### 4.3.1 概念说明

`serial_rx` 收完一个字节后，要告诉 TOP“字节就绪了，快来取”。它用两个信号完成这个握手：

- `d_avail`：**单拍脉冲**。每收完一帧（`s_stop` 结束）拉高**恰好一个 `clk` 周期**，作为“有新字节”的通知。
- `d_out`：字节本身，持续保持（等于内部移位寄存器 `out_bits`），供 TOP 在看到 `d_avail` 时读取。

TOP 这边的 `wait_state`（命令空闲态）每个时钟周期轮询 `dserial_avail && rx_allowed`，命中就解析 `dserial_in` 这个字节，构成一套简单的命令协议（详见 u5-l1）：`P` 触发采集、`A/B/C/D` 选配置项并等下一字节给参数。

#### 4.3.2 核心流程

```
serial_rx (clk_UART 域)              TOP wait_state (clk 域)
─────────────────────────            ─────────────────────────
收完一帧                             每个 clk 轮询：
  d_avail ← 1 (1 拍)        ──┐        if (dserial_avail && rx_allowed)
  d_out   ← 字节             │            解析 dserial_in：
下一拍 d_avail ← 0 (默认)     │              'P' → 触发采集
                             └────────────►  'A'/'B'/'C'/'D' → 置 conf_index
                                             下一字节 → 写入对应配置
```

握手要点：

1. `d_avail` 是脉冲而非电平——它在每个 `clk` 开头被默认置 0（源码第 38 行），仅在 `s_stop` 收尾那一拍被覆盖为 1。所以 TOP 必须在那一拍（或紧随的几拍）内捕获，否则就错过。
2. TOP 的 `rx_allowed` 虽然名为“接收允许”，但**全工程从未对它重新赋值**（声明处 `=1'b1`），始终为 1，相当于一个预留但未启用的闸门。
3. **跨时钟域提醒（进阶）**：`serial_rx` 跑在 `clk_UART`（50 MHz），而消费它的 `wait_state` 跑在 `clk`（200 MHz）。`d_avail` 是 50 MHz 域的 1 拍脉冲（宽约 20 ns），对 200 MHz（周期 5 ns）而言会持续约 4 拍，所以一般能被采到；但源码里**没有两级触发器同步链**，严格说存在亚稳态风险。这是本工程的一处可改进点，了解即可。

#### 4.3.3 源码精读

`d_avail` 的“默认 0”与收尾“置 1”：

[vhdl files/serial_rx.vhd:38](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/serial_rx.vhd#L38) —— 进程每拍先把 `d_avail` 默认置 0，保证它只会是单拍脉冲。

[vhdl files/serial_rx.vhd:88-101](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/serial_rx.vhd#L88-L101) —— 仅在 `s_stop` 的 `counter=15` 那一拍把 `d_avail` 覆盖为 1。

`d_out` 持续输出：

[vhdl files/serial_rx.vhd:104](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/serial_rx.vhd#L104) —— 每拍把移位寄存器 `out_bits` 送到 `d_out`，保持最近一字节。

TOP 例化 `serial_rx`（实例名 `receiver`），把串行脚、`clk_UART`、握手信号一一连上：

[verilog files/TOP.v:104-107](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L104-L107) —— `.input1(serial_in)`、`.clk(clk_UART)`、`.d_avail(dserial_avail)`、`.d_out(dserial_in)`。

TOP 在 `wait_state` 消费字节（这是 u5-l1 命令协议的入口，本讲只看握手接续）：

[verilog files/TOP.v:277-310](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L277-L310) —— `if (dserial_avail && rx_allowed)` 命中后，按 `dserial_in` 的 ASCII 值（如 `8'b01010000`=`'P'`）分发命令。

`rx_allowed` 与握手缓冲的声明（注意 `rx_allowed` 初始化为 1 但全程无再赋值）：

[verilog files/TOP.v:35-36](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L35-L36) 与 [verilog files/TOP.v:44](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L44) —— `dserial_avail`/`dserial_in` 缓冲与 `rx_allowed` 标志。

#### 4.3.4 代码实践

**实践目标：** 跟踪一个命令字节从串口线到 TOP 配置寄存器的完整握手路径。

**操作步骤（源码阅读型）：**

1. 假设 PC 发送字符 `'A'`（`0x41`）。在 `serial_rx` 内部按 4.2.4 的过程走完一帧，`s_stop` 收尾那拍 `d_avail` 拉高、`d_out=0x41`。
2. 这两个信号经 TOP 例化映射为 `dserial_avail`、`dserial_in`。
3. 主 FSM（`always @(posedge clk)`，即 200 MHz 域）处在 `wait_state`，命中 `dserial_avail && rx_allowed`（`rx_allowed` 恒 1）。
4. `dserial_in==8'b01000001` 命中第 280 行的分支 → `conf_index<=3'b001`（记住“下一个字节是时基参数”）。
5. 当 PC 紧接着发来第二个字节时，再次走完 `serial_rx` 一帧，`wait_state` 这次落入 `case(conf_index)` 的 `3'b001` 分支（第 285-288 行），把该字节的 `[5:2]` 位写入 `timebase`，并把 `conf_index` 清回 0。

**需要观察的现象：** 一次完整的“A + 参数”配置需要**两次** `d_avail` 脉冲——第一次选命令、第二次送参数。`conf_index` 正是用来记住“现在处于两步式命令的第几步”。

**预期结果：** 能用一句话说清“`serial_rx` 用单拍 `d_avail` 脉冲通知 TOP，TOP 用 `conf_index` 把单字节命令协议扩展成两步式配置写入”。跨时钟域无同步器这一点标注为“待本地验证/可改进”。

#### 4.3.5 小练习与答案

**Q1：** `d_avail` 为什么设计成单拍脉冲，而不是持续保持高电平直到 TOP 取走？

**答：** 单拍脉冲天然带上“边沿”语义——每收完一帧只通知一次，TOP 用 `if(dserial_avail)` 就能精确对应“一个新字节”，不会把同一个字节当多个事件重复处理。若改成电平保持，TOP 还需另设“已取走”应答线来复位它，握手更复杂。

**Q2：** `rx_allowed` 这个信号在当前工程里实际起作用吗？

**答：** 不起实质作用。它被声明为 `1'b1` 且全工程无任何再赋值，始终为 1，所以 `dserial_avail && rx_allowed` 等价于 `dserial_avail`。它更像一个预留的软件接收开关，目前未启用。

**Q3：** 为什么说 `d_avail` 从 50 MHz 域进 200 MHz 域“一般能采到但有亚稳态风险”？

**答：** 50 MHz 的 1 拍脉冲宽约 20 ns，200 MHz 采样周期 5 ns，所以脉冲会被 200 MHz 采到约 4 拍，捕获概率高。但源码没有先用两级触发器把 `d_avail` 同步到 200 MHz 域，采到的那一拍可能正好落在脉冲的翻转窗口内，理论上存在亚稳态。工程上正确做法是加 2-FF 同步链（或把 `serial_rx` 也放到 `clk` 域、用握手应答）。

---

## 5. 综合实践

把本讲三个最小模块串起来，做一次“端到端跟踪”。

**任务：** PC 想把示波器时基切到某一档，于是依次发送两个字节：`'A'`（`0x41`）和参数字节 `0b00001000`（即十进制 8）。请写一段时序叙述，说明这两个字节各自如何穿越本讲的全部环节，最终改写 TOP 的 `timebase` 寄存器。要求覆盖：

1. 每个字节在线路上的帧结构（起始位 + 8 数据位 LSB 先 + 停止位）。
2. `serial_rx` 内部经历的状态序列（`s_waiting→s_start→s_bit×8→s_stop`）与中点对齐过程（`counter=7` 半位偏移、`counter=15` 整位采样）。
3. 每个字节如何产生一次 `d_avail` 单拍脉冲。
4. TOP `wait_state` 如何用 `conf_index` 把这两次事件串成“先选 A 命令、再写时基参数”的两步式配置。

**参考思路（请自己先写，再对照）：**

- 第一个字节 `0x41` 走完一帧后，`wait_state` 命中 `'A'` 分支，`conf_index<=001`，此时**还没改 `timebase`**。
- 第二个字节 `0b00001000` 走完一帧后，`wait_state` 落入 `case(conf_index)` 的 `3'b001` 分支，执行 `timebase<=dserial_in[5:2]`，即取 `0b00001000` 的 `[5:2]`=`0b0010`=2，`timebase` 被设为 2，同时 `conf_index` 清 0，完成一次配置。
- 跨时钟域、波特率精确值等均标注“待本地验证”。

完成本任务后，你应能向别人解释清楚：“PC 发一个字符，FPGA 是怎么逐位收下来、又怎么变成一条配置命令的。”

## 6. 本讲小结

- `serial_rx` 是一个 VHDL 写的 UART 接收机，用 `generic M` 把系统时钟整数分频成采样节拍 `rs_tick`，关系为 \( f_{\text{baud}} = f_{\text{clk}}/(16M) \)。
- 核心是 `s_waiting`/`s_start`/`s_bit`/`s_stop` 四态状态机：`s_waiting` 检测起始位下降沿，`s_start` 走半位（`counter=7`）做中点对齐，`s_bit` 每 16 拍（`counter=15`）在位中央采一位、共 8 位，`s_stop` 过停止位收尾。
- 数据位用右移寄存器 `input1 & out_bits(7 downto 1)` 装配，正好适配 UART 的“低位先发”，还原出正确字节。
- 与 TOP 的握手靠 `d_avail`（收完一帧的单拍脉冲）+ `d_out`（保持的字节）；TOP 在 `wait_state` 用 `conf_index` 把单字节协议扩展成两步式命令（命令字 + 参数字节）。
- 源码有两处值得留意：`s_start` 里复查起始位的逻辑被注释掉（抗毛刺弱化）；`d_avail` 跨 50 MHz→200 MHz 时钟域时无同步链（亚稳态风险），`rx_allowed` 则是预留但未启用的开关。
- 波特率（约 1.5625 Mbaud）与 `clk_UART` 精确频率均为推导值，**待本地验证**。

## 7. 下一步学习建议

- **接着学发送方向：** 本讲只讲了 PC→FPGA 的接收。下一讲 **u4-l2 UART 发射机** 讲 `serialt` + `serial_tx`，看 FPGA 如何把 16 位数据拆成两字节、逐位串行发回 PC，状态机与本讲镜像对称（`s_waiting_i`/`s_start`/`s_bit`/`s_stop`/`s_stop2`）。
- **回到命令协议：** 本讲只是点到 `wait_state` 的命令分发。完整的 `P`/`A`/`B`/`C`/`D` 协议与主状态机调度在 **u5-l1 主采集状态机与命令协议** 中详讲。
- **延伸阅读源码：** 若想自己改波特率，重点改 `serial_rx.vhd` 第 9 行的 `generic M` 以及 `serialt.vhd`/`serial_tx.vhd` 中对应的分频参数，并确认上位机 LabVIEW GUI（u6-l2）使用一致波特率。
