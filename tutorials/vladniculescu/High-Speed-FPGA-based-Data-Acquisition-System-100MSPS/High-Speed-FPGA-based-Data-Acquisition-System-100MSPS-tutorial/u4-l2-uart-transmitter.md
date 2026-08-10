# UART 发射机

## 1. 本讲目标

上一讲（u4-l1）我们看了 PC→FPGA 方向的接收机 `serial_rx`，理解了 UART 帧格式、16× 过采样与中点对齐。本讲反过来看 **FPGA→PC 方向的发射机**，它负责把处理好的波形/频谱样本一拍一拍地串行送回上位机。

学完本讲你应该能：

- 读懂字节级串行发送状态机 `serial_tx`（起始位→8 数据位→停止位→间隔），并算出它的波特率。
- 读懂发送控制器 `serialt`，理解它如何用一个 1 位计数器把 16 位 `status` 拆成「高字节 + 低字节」两次交给 `serial_tx`。
- 理解 TOP 中 `aggregated`、`en`、`start` 三者如何完成「装载数据→允许发送→通知下次装载」的握手，把整条 DSP 链的产物送出串口。

## 2. 前置知识

在进入源码前，先回顾三个要点（细节见 u4-l1）。

**UART 帧格式。** 异步串行没有共享时钟，靠帧结构自同步：空闲时线为高电平 `1`；一个字节以 1 拍低电平 **起始位（start bit）** 开头，接着是 8 位数据（通常低位先发），最后以 1 拍高电平 **停止位（stop bit）** 收尾。收发双方必须事先约定相同的 **波特率**（每秒发的位数）。

**16× 过采样节拍 `rs_tick`。** 发送端同样用一个比波特率高 16 倍的内部节拍 `rs_tick` 来「切」每一位：一位数据维持 \(16\) 个 `rs_tick`。接收端用它在位中央采样，发送端用它精确控制每位时长。

**本项目的多时钟域。** 发射机跑在 `clk_UART`（来自 PLL 的 50 MHz，精确频率见 u2-l1，待确认）。注意 TOP 的主状态机跑在 200 MHz 的 `clk` 域，而 `aggregated` 的装载逻辑由 `start` 边沿触发——存在跨时钟域的握手，本讲会讲清这条边界。

## 3. 本讲源码地图

| 文件 | 模块 | 作用 |
|------|------|------|
| `vhdl files/serialt.vhd` | `serialt` | 发送控制器：把 16 位 `status` 拆成两个字节，逐字节启动底层发送 |
| `vhdl files/serial_tx.vhd` | `serial_tx` | 字节级串行发送状态机：把 1 字节按 UART 帧逐位发出 |
| `verilog files/TOP.v` | `TOP` | 例化 `serialt`，用 `aggregated`/`en`/`start` 与之握手 |

**命名提醒**（贯穿全手册）：文件名、模块名、例化名并不总是一致。这里 `serialt`（控制器）与 `serial_tx`（底层字节发送器）名字相近但职责不同，务必分清。

## 4. 核心概念与源码讲解

### 4.1 serial_tx：单字节串行发送状态机

#### 4.1.1 概念说明

`serial_tx` 是最底层的发送原子：给它 **1 个字节**（`input`）和一个启动脉冲（`begin_tx`），它就按 UART 帧格式把这 8 位逐位推到 `serial` 输出线上，发完后回到空闲。它内部用一个 generic 参数 `M` 做分频，把系统时钟切成 `rs_tick` 节拍，每位占 16 个 `rs_tick`。

#### 4.1.2 核心流程

一次字节发送的状态机为：

```text
s_waiting_i  ──begin_tx=1──►  s_waiting  ──rs_tick──►  s_start
   (空闲)                       (对齐节拍)              (起始位:输出0, 16 tick)
                                                           │
            ┌─────────────── s_bit ◄──────────────────────┘
            │                 (8 位数据, 各 16 tick; LSB 先发)
            ▼
          s_stop  ──16 tick──►  s_stop2  ──900 tick 间隔──►  s_waiting_i
        (停止位:输出1)          (保持高 + 帧间间隔)
```

波特率与节拍的关系：

\[
f_{\text{rs\_tick}} = \frac{f_{\text{clk\_UART}}}{M}
\]

\[
f_{\text{baud}} = \frac{f_{\text{clk\_UART}}}{16 \cdot M}
\]

代入 `clk_UART = 50 MHz`、`M = 2`：

\[
f_{\text{baud}} = \frac{50\,\text{MHz}}{16 \times 2} = 1.5625\,\text{Mbaud}
\]

即一位时长 \(T_{\text{bit}} = 16 \times 2 / 50\,\text{MHz} = 640\,\text{ns}\)。（与 u4-l1 的接收机波特率一致；精确值待本地验证。）

#### 4.1.3 源码精读

**实体与分频参数。** [vhdl files/serial_tx.vhd:6-17](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/serial_tx.vhd#L6-L17) 定义了端口和 generic `M`。`index` 是「当前发的是第几个字节」的标记（由上层 `serialt` 的计数器送来），`begin_tx` 是启动脉冲，`tx_busy` 是忙信号，`flag`/`start` 是反馈给上层的状态/节拍信号。

**`rs_tick` 的生成。** [vhdl files/serial_tx.vhd:33-39](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/serial_tx.vhd#L33-L39) 用一个模 `M` 计数器 `cnt` 产生 `rs_tick`——每 `M` 个 `clk` 拉高 1 拍，这是整条发送时序的「心跳」。

**空闲态 `s_waiting_i`。** [vhdl files/serial_tx.vhd:42-52](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/serial_tx.vhd#L42-L52) 中 `busy<='0'`（表示空闲可用），一旦 `begin_tx='1'` 就置忙并进入 `s_waiting`。注意此处还根据 `index` 决定是否拉高 `start` 输出——这个 `start` 正是回送给 TOP 的「新样本装载」节拍（见 4.3）。

**起始位 `s_start`。** [vhdl files/serial_tx.vhd:60-71](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/serial_tx.vhd#L60-L71) 把输出拉低（起始位），用 `counter` 数满 16 个 `rs_tick` 后进入 `s_bit`。

**数据位 `s_bit`。** [vhdl files/serial_tx.vhd:73-95](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/serial_tx.vhd#L73-L95) 用 `bit_pos`（0~7）逐位输出 `input(bit_pos)`，每位占 16 个 `rs_tick`，LSB 先发；`bit_pos=7` 走完后进入停止位。

**停止位与帧间间隔。** [vhdl files/serial_tx.vhd:96-107](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/serial_tx.vhd#L96-L107) 是停止位（输出高）。随后 [vhdl files/serial_tx.vhd:108-123](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/serial_tx.vhd#L108-L123) 的 `s_stop2` 维持高电平并额外等 **900 个 `rs_tick`** 才回空闲。这个 900-tick 间隔是一个值得注意的设计——它给每个字节之间加了约 \(900 \times 2 / 50\,\text{MHz} \approx 36\,\mu s\) 的保护间隔，直接拉低了实际上行吞吐。

**忙信号组合输出。** [vhdl files/serial_tx.vhd:130-131](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/serial_tx.vhd#L130-L131) 给出 `tx_busy <= busy OR begin_tx`——空闲时为 0，一旦被启动（哪怕只看到 `begin_tx` 这一拍）立即变 1，防止上层在发送过程中重复触发。

#### 4.1.4 代码实践

**实践目标：** 通过阅读源码，复算一次字节发送占用多少个 `rs_tick` 和多少微秒。

1. 打开 `serial_tx.vhd`，定位 `s_start`、`s_bit`、`s_stop`、`s_stop2` 四段。
2. 数出每个状态消耗的 `rs_tick` 数：起始位 16，数据位 \(8 \times 16 = 128\)，停止位 16，间隔 900。
3. 求和：\(16 + 128 + 16 + 900 = 1060\) 个 `rs_tick`。
4. 换算：每个 `rs_tick` = \(M = 2\) 个 `clk_UART` = 40 ns，故一字节约 \(1060 \times 40\,\text{ns} \approx 42.4\,\mu s\)。

**需要观察的现象：** 注意「真正的 UART 位时长」只占其中约 \(160\) 个 `rs_tick`（\(10\) 位 × \(16\)），剩余 \(900\) 是本项目额外加的帧间间隔。

**预期结果：** 单字节净发送约 \(10 \times 640\,\text{ns} = 6.4\,\mu s\)，但加上 `s_stop2` 的 900-tick 间隔后实际约 \(42\,\mu s\)。（具体微秒数待本地验证，取决于 `clk_UART` 精确频率。）

#### 4.1.5 小练习与答案

**练习 1.** 若想让波特率降为原来的一半，只改 `serial_tx` 的一个参数，改哪个？改成多少？
**答：** 把 generic `M` 从 2 改为 4。波特率 \(f_{\text{baud}} = f_{\text{clk}}/(16M)\)，`M` 翻倍则波特率减半。

**练习 2.** `s_bit` 里 `output <= input(bit_pos)` 是组合读取（没有把 `input` 锁进内部寄存器）。这对 `input` 的稳定性提出了什么要求？
**答：** 要求 `input` 在整个 `s_bit` 阶段（约 128 个 `rs_tick`）保持不变。上层 `serialt` 通过在 `tx_busy=1` 期间不切换字节选择来满足这一约束（见 4.2）。

---

### 4.2 serialt：16 位拆两字节的发送控制器

#### 4.2.1 概念说明

`serial_tx` 一次只能发 1 字节，而 TOP 要送的数据（如 10 位幅度、帧头字符）都装在 16 位的 `aggregated`（即 `serialt` 的 `status` 端口）里。`serialt` 就是这二者之间的「翻译层」：它用一个 **1 位计数器 `counter`** 在两个字节间来回切换，每空闲一次就启动 `serial_tx` 发当前选中的字节，发完翻转 `counter` 再发下一个，从而把一个 16 位值拆成连续两字节送出。

#### 4.2.2 核心流程

```text
            ┌───────────── tx_busy=0 且 en=1 ────────────┐
            ▼                                            │
  counter=1 ─► 选 status[7:0]   (低字节)                 │
  counter=0 ─► 选 status[15:8]  (高字节)                 │
            │                                            │
            └─► start_tx<=1 启动 serial_tx, counter 翻转 ─┘
                              (发完一个, tx_busy 回 0, 再发下一个)
```

字节选择的多路选择：

\[
\text{data\_in\_serial} =
\begin{cases}
\text{status}[15{:}8] & \text{counter}=0 \\
\text{status}[7{:}0]  & \text{counter}=1
\end{cases}
\]

`counter` 在每次启动发送时翻转（`0\to1\to0\to1\cdots`），且 `tx_busy=1` 期间不翻转，所以两个字节的输入在各自发送过程中都保持稳定。

#### 4.2.3 源码精读

**实体。** [vhdl files/serialt.vhd:12-22](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/serialt.vhd#L12-L22) 中 `status` 是 16 位待发送数据，`en` 是发送使能（来自 TOP），`start`/`flag` 回送 TOP，`res` 可复位 `counter`。

**例化底层 `serial_tx`。** [vhdl files/serialt.vhd:43-52](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/serialt.vhd#L43-L52) 把自己的 `counter` 直接接到 `serial_tx.index`，把 `start_tx` 接到 `begin_tx`，把 `data_in_serial` 接到 `input`。

**驱动计数器与启动脉冲。** [vhdl files/serialt.vhd:54-70](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/serialt.vhd#L54-L70) 是关键：当 `tx_busy='0'` 且 `en='1'` 时，拉高 `start_tx` 一个节拍去启动 `serial_tx`，并翻转 `counter`；`res='1'` 时把 `counter` 复位为 0。

**字节选择 MUX。** [vhdl files/serialt.vhd:72-73](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/serialt.vhd#L72-L73) 用 `counter` 在高/低字节间二选一。

> **深度细节（字节顺序，待本地验证）：** 因为 `counter` 的翻转与 `start_tx` 的拉高发生在同一拍，而 `serial_tx` 在 `s_bit` 阶段才真正读取 `input`（此时 `counter` 已是翻转后的值且保持不变），所以实际先发出的字节对应 `counter=1` 选中的 **低字节 `status[7:0]`**，随后才是 `counter=0` 的高字节。即线上顺序是 **低字节在前、高字节在后**。上位机重组时需匹配此顺序；建议在仿真或抓包中确认。

#### 4.2.4 代码实践

**实践目标：** 手动模拟 `counter` 与 MUX，验证一个 16 位值被拆成哪两个字节、以什么顺序发出。

1. 设 `status = 16'b0000_0011_1100_0000`（即高字节 `0x03`、低字节 `0xC0`）。
2. 复位后 `counter=0`。第一次 `tx_busy=0, en=1`：`start_tx` 拉高，`counter` 翻为 1。
3. 此字节发送期间 `counter=1`，故 `data_in_serial = status[7:0] = 0xC0`（低字节先发）。
4. 发完后 `counter` 翻回 0，第二字节期间 `data_in_serial = status[15:8] = 0x03`（高字节后发）。

**需要观察的现象：** 线上依次出现 `0xC0`、`0x03` 两个字节的 UART 帧（各带起始/停止位）。

**预期结果：** 每来一个 `status`，`serial_out` 上连续出现 2 个字节帧；`counter` 每字节翻转一次，呈 `1,0,1,0,...`。（精确顺序以 4.2.3 的深度细节为准，待本地验证。）

#### 4.2.5 小练习与答案

**练习 1.** 为什么 `counter` 用 1 位就够了？
**答：** 因为只需要在两个字节（高/低）之间二选一，1 位即可编码两种状态；翻转就是 `0↔1`。

**练习 2.** 若 `en` 一直为 0，会发生什么？
**答：** [vhdl files/serialt.vhd:57](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/vhdl%20files/serialt.vhd#L57) 的条件 `tx_busy='0' AND en='1'` 不成立，`start_tx` 恒为 0，`serial_tx` 一直停在 `s_waiting_i`，不发任何字节。

**练习 3.** `res` 信号的作用是什么？TOP 何时用到它？
**答：** `res` 把 `counter` 强制清 0，用于让下一次发送从确定的字节序开始。TOP 在 `wait_state` 里把 `res_serial<=1`（见 4.3），保证每轮采集发送前字节序对齐。

---

### 4.3 TOP 的接入：aggregated、en、start 三方握手

#### 4.3.1 概念说明

`serialt` 自身并不知道「要发什么数据」——它只盯着 16 位 `status` 端口。真正决定内容的是 TOP：用一个 16 位寄存器 `aggregated` 当发送缓冲，按主状态机的进度往里装载样本；用 `en` 开关打开发送；用 `serialt` 回送的 `start` 边沿作为「该装下一个样本了」的节拍。三者构成一个简洁的生产者—消费者握手。

#### 4.3.2 核心流程

```text
        (主状态机进入 send_state)
                    │
        en <= 1  ───┘   打开发送
                    │
   serialt 在 tx_busy=0 时启动 serial_tx, 两字节发完一组
                    │
   serial_tx 在 index=1(低字节)起始时拉高 start
                    │
   TOP 的 always@(posedge start) 触发:
        ├── 若在 send_state / final_state: aggregated <= {6'b0, data_send}, cnt++
        └── 否则进入帧头/波形打包子状态 state3 (s3_1..s3_4)
                    │
   (回到 wait_state 时 en<=0, res_serial<=1, 停发并复位 counter)
```

要点：`start` 的上升沿每个样本（每两字节）来一次，正好驱动 TOP 把下一个样本装进 `aggregated`；`en` 只在「发送类」状态（`send_state`/`final_state`/`send_state2`/`send_state3`）为高，其余时间关闭发送。

#### 4.3.3 源码精读

**相关声明。** [verilog files/TOP.v:37-47](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L37-L47) 声明了发送缓冲 `aggregated`、使能 `en`、复位 `res_serial` 和节拍 `start`（`start` 是 `serialt` 的输出，注释明确写着「indicates when a new sample is ready to be sent」）。

**例化 `serialt`。** [verilog files/TOP.v:110-117](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L110-L117) 把 `aggregated` 接到 `status`、`en` 接使能、`serial_out` 接顶层物理 TX 引脚、`start` 接回节拍线、`res` 接 `res_serial`。注意它用 `clk_UART`（50 MHz）作时钟。

**发送使能与复位的开关。** 在 `wait_state` 里 [verilog files/TOP.v:274-275](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L274-L275) 设 `en<=0; res_serial<=1`（停发 + 复位计数器）；进入 `send_state` 时 [verilog files/TOP.v:394-397](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L394-L397) 设 `en<=1`（开始发送频谱样本）。`data_send` 是 ram3（开方后幅度）的输出，见 [verilog files/TOP.v:65](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L65) 与 [verilog files/TOP.v:149](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L149)。

**`posedge start` 装载逻辑。** [verilog files/TOP.v:415-457](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L415-L457) 是发送侧的核心块：每次 `start` 上升沿，若处于 `send_state`/`final_state`，就把 10 位频谱样本 `data_send` 装进 `aggregated[9:0]`（高 6 位补 0）并 `cnt+1`；否则走 `state3` 子状态机插入帧头字符 `'F'`/`'T'` 并装载波形样本 `buffer`。这一块的帧拼装细节属于下一阶段的 `state3` 打包子状态机（u5-l3），本讲只需理解它由 `start` 边沿驱动。

#### 4.3.4 代码实践

**实践目标：** 追踪一个频谱样本 `aggregated[15:0]` 从装载到串口发出的完整路径。

1. 在 `TOP.v` 中找到 `send_state`（[L394](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L394)）确认 `en<=1`，随后 [L417-L422](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L417-L422) 在 `posedge start` 把 `data_send` 装入 `aggregated`。
2. 跟随 `aggregated` → [L112](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L112) `serialt.status` → `serialt` 内 `counter`+MUX 选字节 → `serial_tx.input`。
3. 在 `serial_tx` 中跟一遍状态：`s_waiting_i`（`busy=0`，收 `begin_tx`）→ `s_waiting`（对齐 `rs_tick`）→ `s_start`（起始位）→ `s_bit`（8 位）→ `s_stop`（停止位）→ `s_stop2`（900-tick 间隔）→ 回 `s_waiting_i`。
4. 注意 `counter` 在两次发送间翻转一次：低字节先发、高字节后发；发完两字节后 `start` 再来一个上升沿，TOP 装入下一个样本。

**需要观察的现象：** 每个频谱样本在 `serial_out` 上表现为 2 个连续的 UART 字节帧；`cnt` 每发完一个样本（2 字节）加 1；`tx_busy` 在每字节发送期间为 1、间隔为 0。

**预期结果：** 能用一张时序表把「posedge start → 装载 aggregated → 发低字节 → counter 翻转 → 发高字节 → 下一个 posedge start」完整画出来。（`start` 跨 50 MHz/200 MHz 时钟域，精确建立/保持时序关系待本地验证。）

#### 4.3.5 小练习与答案

**练习 1.** 为什么用 `always @(posedge start)` 而不是用某个固定时钟来装载 `aggregated`？
**答：** 发送节拍由 `serial_tx` 实际发完字节的真实速度决定（受波特率、`s_stop2` 间隔影响），不是固定时钟。用 `start` 上升沿做触发，保证「上一个样本刚发出去，立刻装下一个」，既不漏发也不覆盖未发完的数据。

**练习 2.** `aggregated[15:10]` 在装载频谱样本时被设为 `6'b000000`，为什么？
**答：** 频谱幅度 `data_send` 只有 10 位，装进 `aggregated[9:0]`，高 6 位补 0 后整个 16 位值就是该幅度；这样高字节里只是 2 位有效数据，上位机按 16 位重组时自然还原出 10 位幅度。

**练习 3.** 若去掉 `wait_state` 里的 `en<=0`，系统在采集/计算阶段会怎样？
**答：** `en` 会保持为 1，`serialt` 可能在 `aggregated` 还没装好有效数据时就把旧值/乱序字节发出去，造成上行数据污染。`en` 是发送窗口的硬开关。

---

## 5. 综合实践

把本讲三部分串起来，做一个「上行一帧的纸面追踪」：

1. 假设主状态机刚进入 `send_state`，`en` 由 0 变 1，此时 `res_serial` 已在 `wait_state` 把 `counter` 复位为 0。
2. 第一个 `posedge start` 到来：TOP 把 ram3 的第一个幅度样本 `data_send` 写入 `aggregated`，`cnt` 加 1。
3. `serialt` 见 `en=1, tx_busy=0`，拉高 `start_tx`，`counter` 翻为 1；`serial_tx` 走完 `s_waiting_i→s_waiting→s_start→s_bit→s_stop→s_stop2` 发出低字节。
4. `counter` 翻回 0，再发高字节。两字节组成一个样本。
5. 第二个 `posedge start` 到来，TOP 装入下一个样本，循环直至 `send_state` 结束、进入波形上传阶段（由 `state3` 插入 `F`/`T` 帧头，见 u5-l3）。

**交付物：** 画一张时序图，横轴为 `clk_UART`，画出 `en`、`start`、`counter`、`tx_busy`、`serial_out`（标出起始位/数据位/停止位）的变化，并在图上标出 TOP 在哪个沿装载 `aggregated`。若能用 Vivado 仿真跑出 `serialt`+`serial_tx` 的波形对照则更佳（待本地验证）。

## 6. 本讲小结

- `serial_tx` 是字节级 UART 发送状态机：`s_waiting_i→s_waiting→s_start→s_bit→s_stop→s_stop2`，每位 16 个 `rs_tick`，波特率 \(f_{\text{clk}}/(16M)\)，本项目约 1.5625 Mbaud。
- `s_stop2` 额外等待 900 个 `rs_tick`，给每字节加了约 36 µs 的帧间间隔，显著影响上行吞吐。
- `serialt` 用 1 位 `counter` 把 16 位 `status` 拆成两字节，靠 `tx_busy` 反馈实现「发完一个再发下一个」；线上字节顺序为低字节在前、高字节在后（细节待本地验证）。
- TOP 用 `aggregated` 装载数据、`en` 开关发送、`start` 上升沿作节拍，构成生产者—消费者握手。
- `en` 只在发送类状态为高，`res_serial` 在 `wait_state` 复位 `counter`，二者保证发送窗口与字节序受控。
- 发射机（本讲）与接收机（u4-l1）共同构成 FPGA↔PC 的串口双向通路，为下一阶段「打包子状态机 state3」（u5-l3）与「PC 协议/GUI」（u6-l2）奠定基础。

## 7. 下一步学习建议

- **横向**：回头对照 u4-l1 的 `serial_rx`，体会收发两侧「16× 过采样 vs 16× 节拍切位」的对称设计。
- **纵向**：进入 u5（TOP 三态机），先看 u5-l1 主状态机如何决定何时进入 `send_state`（从而打开 `en`），再看 u5-l3 的 `state3` 打包子状态机如何把帧头 `'F'`/`'T'` 与波形/频谱样本按序装进 `aggregated`。
- **源码阅读**：精读 `serial_tx.vhd` 的 `s_bit` 分支，弄清 `bit_pos` 与 `counter` 如何配合实现「LSB 先发、每位精确 16 拍」，这是 UART 时序的最核心细节。
