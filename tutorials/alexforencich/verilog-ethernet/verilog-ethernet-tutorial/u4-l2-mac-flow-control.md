# MAC 控制帧与暂停/PFC 流量控制

## 1. 本讲目标

以太网不只是「把帧发出去」，还需要在接收方来不及处理时**让对方先停一停**。本讲讲解 verilog-ethernet 如何用四个模块实现 IEEE 802.3 的链路级流量控制。学完后你应当能够：

1. 说清楚 **MAC 控制帧（MAC Control Frame）** 的报文格式：目的 MAC、Ethertype `0x8808`、opcode、参数区。
2. 读懂 `mac_ctrl_rx` 如何从一根 AXI-Stream 里「旁路」识别出控制帧，`mac_ctrl_tx` 又如何把控制帧**高优先级插队**进数据流。
3. 掌握 **PAUSE（LFC）** 流控：量子（quanta）倒计时如何驱动 `rx_lfc_req`，以及发送侧如何在暂停持续期间自动重发。
4. 理解 **PFC（Priority Flow Control）**：用 8 个优先级位独立暂停 8 条虚拟通道。
5. 理解这四个模块在 `eth_mac_1g` 里如何串联成完整的收发流控通路。

---

## 2. 前置知识

在进入流控之前，请先具备以下概念（来自前置讲义 u1-l3、u3-l1、u4-l1）：

- **AXI-Stream 握手**：`tvalid`/`tready` 同时为 1 才完成一次传输（beat）；`tlast` 标记帧尾；`tuser[0]` 常用作坏帧标志。
- **以太网帧结构**：14 字节帧头 = 6 字节目的 MAC + 6 字节源 MAC + 2 字节 EtherType，其后是载荷。
- **MAC 与 PHY 的关系**：MAC 负责成帧、流量控制；PHY 负责物理线路编解码。本讲的模块位于 MAC 层内部。

本讲还需要三个新术语：

- **MAC 控制帧**：一种特殊以太网帧，EtherType = `0x8808`，专门承载链路层控制命令（如 PAUSE）。它走的是和普通数据完全相同的物理通路，靠 EtherType + opcode 区分。
- **LFC（Link-level Flow Control，链路级流控）**：即传统 **PAUSE**（IEEE 802.3 Annex 31B）。整条链路要么全停、要么全走，不区分业务。
- **PFC（Priority Flow Control，优先级流控）**：IEEE 802.3 Annex 31D / 802.1Qbb。把流量划分到 8 个优先级，可以「只暂停优先级 3，其余照发」，避免队头阻塞，是数据中心以太网（RoCE、无损网络）的基础。

> 一个直觉类比：LFC 像路口的红灯——所有车都停；PFC 像按车道分别亮灯——只拦某几条车道。

---

## 3. 本讲源码地图

| 文件 | 角色 | 关键点 |
|------|------|--------|
| [rtl/mac_ctrl_rx.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/mac_ctrl_rx.v) | 接收侧控制帧识别 | 数据帧透传，命中控制帧时在 `tlast` 拍从 `mcf_*` 并行端口吐出解码结果 |
| [rtl/mac_ctrl_tx.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/mac_ctrl_tx.v) | 发送侧控制帧插入 | 把并行 `mcf_*` 串行化，**优先于**数据帧插入输出流；同时实现 PAUSE 反压本端发送 |
| [rtl/mac_pause_ctrl_rx.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/mac_pause_ctrl_rx.v) | 接收侧 PAUSE/PFC 处理 | 消费 `mcf_*`，用量子倒计时产生 `rx_lfc_req` / `rx_pfc_req` |
| [rtl/mac_pause_ctrl_tx.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/mac_pause_ctrl_tx.v) | 发送侧 PAUSE/PFC 产生 | 根据本端反压状态生成并发送 PAUSE/PFC 帧，含刷新重发 |
| [rtl/eth_mac_1g.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v) | 千兆 MAC 顶层 | 把上面四个模块串成完整流控通路（仅作集成参考） |

四个模块的依赖关系是**严格分层**的：

```
线路帧 ──► mac_ctrl_rx ──mcf_*──► mac_pause_ctrl_rx ──► rx_lfc_req / rx_pfc_req（暂停本端 TX）
                                                                         ▲
                          (对端要求我们暂停发送)                              │
                                                                    控制 MAC 停发
                                                                         │
线路帧 ◄── mac_ctrl_tx ◄──mcf_*── mac_pause_ctrl_tx ◄── tx_lfc_req / tx_pfc_req（我们要求对端暂停）
            ▲ 串行化控制帧                                            ▲
            │ 高优先级插队                                   由本端 FIFO 水位/反压触发
```

- `mac_ctrl_rx`/`mac_ctrl_tx` 负责**帧 ↔ 并行字段**的编解码，与「暂停多久」无关。
- `mac_pause_ctrl_rx`/`mac_pause_ctrl_tx` 才真正理解 PAUSE/PFC 语义，负责「量子倒计时」与「何时发、何时重发」。

---

## 4. 核心概念与源码讲解

### 4.1 MAC 控制帧：格式与收发（mac_ctrl_rx / mac_ctrl_tx）

#### 4.1.1 概念说明

MAC 控制帧本质上是一类**特殊的以太网帧**。源码注释给出了它的标准结构（[rtl/mac_ctrl_rx.v:137-145](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/mac_ctrl_rx.v#L137-L145)）：

| 字段 | 长度 | 典型值 |
|------|------|--------|
| 目的 MAC | 6 字节 | `01:80:C2:00:00:01`（慢协议组播） |
| 源 MAC | 6 字节 | 发送方 MAC |
| EtherType | 2 字节 | `0x8808`（MAC Control） |
| Opcode | 2 字节 | `0x0001`=PAUSE(LFC)，`0x0101`=PFC |
| Parameters | 0–44 字节 | PAUSE：2 字节量子；PFC：18 字节 |

关键设计思想：**控制帧与数据帧共用同一条 AXI-Stream 物理通路**，不单独走线。`mac_ctrl_rx` 一边把帧原样透传给后续模块，一边「旁路」地扫描前 60 字节头部；如果命中（目的 MAC、Ethertype、opcode 都匹配配置），就在帧尾 `tlast` 那一拍，把解码出的并行字段（`mcf_opcode`、`mcf_params` 等）从独立的 `mcf_*` 端口送出去，供下游 `mac_pause_ctrl_rx` 消费。这样数据通路零损失，控制信息走侧信道。

#### 4.1.2 核心流程

**接收侧 `mac_ctrl_rx`** 用一个字节指针 `ptr_reg` 在帧内逐字节推进，过程如下：

1. 帧到达后，`read_mcf_reg=1`，每个有效 beat `ptr_reg` 自增。
2. 用 `_HEADER_FIELD_` 宏按字节偏移把 `s_axis_tdata` 的字节挑出来填进 `mcf_eth_dst_next` / `mcf_eth_src_next` / `mcf_eth_type_next` / `mcf_opcode_next`。
3. 读到 opcode 末尾（`ptr_reg == 15`）时，**一次性锁存命中结果** `mcf_frame_next`。
4. 读到第 60 字节（头部读完）后停止扫描，剩余载荷继续透传。
5. 帧尾 `tlast` 拍：若命中且帧未损坏，拉高 `mcf_valid` 一拍，把完整字段送出；若 `cfg_mcf_rx_forward=0`，还会把该帧在数据通路上标记为坏帧（`tuser[0]=1`），即「吞掉」控制帧不让它继续上行。

**发送侧 `mac_ctrl_tx`** 做逆操作，并多了「插队」逻辑：

1. 默认透传数据帧（`send_data` 状态）。
2. 当 `mcf_valid` 有效且当前不在发数据帧的中间字节时，**立即切换**到 `send_mcf` 状态，把并行的 `mcf_*` 字段用同一个 `_HEADER_FIELD_` 宏逐字节串行化输出，补 0 填满到 60 字节，最后一拍拉 `tlast`。
3. 控制帧发完后才继续数据帧——即**控制帧优先级高于数据帧**，但不会打断已经开始的数据帧（保证整帧完整）。

#### 4.1.3 源码精读

**接收侧命中判定**——把多个可配置检查项「与」起来（[rtl/mac_ctrl_rx.v:196-213](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/mac_ctrl_rx.v#L196-L213)）：

```verilog
wire mcf_eth_dst_match = ((mcf_eth_dst_mcast_match && cfg_mcf_rx_check_eth_dst_mcast) ||
    (mcf_eth_dst_ucast_match && cfg_mcf_rx_check_eth_dst_ucast) ||
    (!cfg_mcf_rx_check_eth_dst_mcast && !cfg_mcf_rx_check_eth_dst_ucast));

wire mcf_opcode_match = ((mcf_opcode_lfc_match && cfg_mcf_rx_check_opcode_lfc) ||
    (mcf_opcode_pfc_match && cfg_mcf_rx_check_opcode_pfc) ||
    (!cfg_mcf_rx_check_opcode_lfc && !cfg_mcf_rx_check_opcode_pfc));

wire mcf_match = (mcf_eth_dst_match &&
    (mcf_eth_src_match || !cfg_mcf_rx_check_eth_src) &&
    mcf_eth_type_match && mcf_opcode_match);
```

注意每个 `check` 位的语义：`check=1` 表示「要检查」，`check=0` 表示「跳过该项」。所以当所有 check 位都为 0 时 `mcf_match` 恒为 1（不检查任何字段），这是模块的「宽进」默认态；实际使用时由 `eth_mac_1g` 顶层把检查位按需置 1。

**在 opcode 末尾锁存命中**（[rtl/mac_ctrl_rx.v:286-289](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/mac_ctrl_rx.v#L286-L289)）：

```verilog
if (ptr_reg == 15/BYTE_LANES && (!KEEP_ENABLE || s_axis_tkeep[13%BYTE_LANES])) begin
    // record match at end of opcode field
    mcf_frame_next = mcf_match && cfg_mcf_rx_enable;
}
```

`cfg_mcf_rx_enable` 是总开关；`BYTE_LANES` 让同一逻辑在 8 位（=1）和 64 位（=8）通路下都能定位到正确的字节。

**帧尾送出解码结果**（[rtl/mac_ctrl_rx.v:298-314](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/mac_ctrl_rx.v#L298-L314)）：

```verilog
if (s_axis_tlast) begin
    if (s_axis_tuser[0]) begin
        // frame marked invalid
    end else if (mcf_frame_next) begin
        if (!cfg_mcf_rx_forward) begin
            m_axis_tuser_int[0] = 1'b1;   // 标记为坏帧，吞掉它
        end
        mcf_valid_next = 1'b1;            // 侧信道送出控制帧
        stat_rx_mcf_next = 1'b1;
    end
    read_mcf_next = 1'b1;
    mcf_frame_next = 1'b0;
    ptr_next = 0;
end
```

**发送侧插队**——空闲且无数据可发时，优先启动控制帧（[rtl/mac_ctrl_tx.v:206-211](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/mac_ctrl_tx.v#L206-L211)）：

```verilog
end else if (mcf_valid) begin
    s_axis_tready_next = 1'b0;     // 暂停接收数据
    ptr_next = 0;
    send_mcf_next = 1'b1;          // 切到发控制帧
    mcf_ready_next = (CYCLE_COUNT == 1) && m_axis_tready_int_early;
end
```

并且在数据帧发完的最后一拍也会检查是否有待发控制帧（[rtl/mac_ctrl_tx.v:228-233](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/mac_ctrl_tx.v#L228-L233)），做到「数据帧一结束就无缝接上控制帧」。

> 这对模块与前置讲义 u3-l1 的 `eth_axis_rx/tx` 共享同一种「头部并行字段 + `_HEADER_FIELD_` 宏逐字节搬运」的设计风格，是本库协议模块的通用范式。

#### 4.1.4 代码实践

**实践目标**：验证 `mac_ctrl_rx` 能从一根普通 AXI-Stream 中识别出 PAUSE 帧，并从侧信道吐出 opcode 与参数。

由于本仓库没有 `mac_ctrl_rx` 的独立 testbench，我们采用「源码阅读 + 最小 cocotb 用例」实践。

**操作步骤**（示例代码，需自行搭 testbench）：

1. 参照 `tb/eth_mac_1g/Makefile` 的三件套结构，新建 `tb/mac_ctrl_rx/` 目录，写一份 `Makefile` 把 `rtl/mac_ctrl_rx.v` 列入 `VERILOG_SOURCES`，`TOPLEVEL` 设为 `mac_ctrl_rx`。
2. 在 `test_mac_ctrl_rx.py` 中用 cocotbext-eth 的 `GmiiSource` 或 `AxisSource` 驱动 `s_axis_*`，构造一帧：
   - 目的 MAC = `01:80:C2:00:00:01`
   - EtherType = `0x8808`
   - opcode = `0x0001`（PAUSE）
   - 参数 = 2 字节量子 `0x1000`
3. 配置 `cfg_mcf_rx_eth_dst_mcast = 0x0180C2000001`、`cfg_mcf_rx_check_eth_dst_mcast = 1`、`cfg_mcf_rx_eth_type = 0x8808`、`cfg_mcf_rx_opcode_lfc = 0x0001`、`cfg_mcf_rx_check_opcode_lfc = 1`、`cfg_mcf_rx_enable = 1`、`cfg_mcf_rx_forward = 0`。

**需要观察的现象**：

- 帧尾后一拍 `mcf_valid` 出现一个时钟周期的脉冲。
- `mcf_opcode == 0x0001`。
- `mcf_params`（2 字节）中量子字段为 `0x1000`（注意字节序，见 4.2.4）。
- 由于 `cfg_mcf_rx_forward=0`，输出 `m_axis_tuser[0]` 在该帧尾被拉高（控制帧被吞）。

**预期结果**：上述四点全部满足，即证明接收侧识别正确。**具体周期级波形待本地验证。**

#### 4.1.5 小练习与答案

**练习 1**：如果只把 `cfg_mcf_rx_enable` 设为 0，其余配置不变，`mcf_valid` 还会脉冲吗？

> **答案**：不会。`mcf_frame_next = mcf_match && cfg_mcf_rx_enable`（[mac_ctrl_rx.v:288](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/mac_ctrl_rx.v#L288)），总开关关掉后即使帧匹配也不会送出。

**练习 2**：`mac_ctrl_tx` 在「正在发送一个长数据帧」时收到 `mcf_valid`，会立刻打断数据帧吗？

> **答案**：不会。它处于 `send_data_reg` 状态时会等当前数据帧发到 `tlast`，才在最后一拍切到 `send_mcf`（[mac_ctrl_tx.v:228-233](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/mac_ctrl_tx.v#L228-L233)），保证整帧不被拆散。

---

### 4.2 PAUSE 流控：量子倒计时与暂停（LFC）

#### 4.2.1 概念说明

PAUSE（LFC）解决一个具体问题：接收方的 FIFO 快满了，希望发送方在一段**精确时长**内停止发数据。标准做法是发一个 PAUSE 帧，里面带一个 16 位的「**量子值（quanta）**」。发送方收到后，就暂停这么多个「量子」的时间。

一个量子（quantum）= 512 个比特时间（bit time），即：

\[ 1\ \text{quantum} = 512\ \text{bit} \]

在 GMII（8 位、125 MHz）下，每个时钟传 8 bit，所以：

\[ 1\ \text{quantum} = 512 / 8 = 64\ \text{时钟周期} \]

量子值 = 0 表示 XON（立即恢复发送），非 0 表示 XOFF（暂停）。

#### 4.2.2 核心流程

`mac_pause_ctrl_rx` 消费 `mac_ctrl_rx` 送来的 `mcf_*`，维护一个**带小数的倒计时器**：

1. 收到 LFC opcode 的控制帧时，把 16 位量子值左移 8 位存入 `lfc_quanta_reg`（低 8 位是小数部分，`QFB=8`）。
2. 每个有效时钟（`cfg_quanta_clk_en=1` 且本端确实在暂停发送 `rx_lfc_ack=1`）从倒计时器减去 `cfg_quanta_step`。
3. 当倒计时器减到 0，`rx_lfc_req` 自动拉低，恢复发送。
4. `rx_lfc_req` 同时受三重使能：倒计时非 0、`rx_lfc_en`、`cfg_rx_lfc_en`，三者都为真才输出暂停请求。

为什么要有小数部分（`QFB=8`）？因为不同速率下「每时钟减多少」可能不是整数个量子，引入 8 位小数让倒计时在不同速率下都精确对齐到 512 比特时间。

**发送侧 `mac_pause_ctrl_tx`** 则相反：当本端希望对端暂停（`tx_lfc_req` 跳变）时，组装一个 PAUSE 帧发出；并且在暂停持续期间用刷新定时器（`cfg_tx_lfc_refresh`）周期性重发，避免对端的暂停过期后又开始狂发。

#### 4.2.3 源码精读

**量子步长的小数倒计时**（[rtl/mac_pause_ctrl_rx.v:141-151](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/mac_pause_ctrl_rx.v#L141-L151)）：

```verilog
if (cfg_quanta_clk_en && rx_lfc_ack) begin
    if (lfc_quanta_reg > cfg_quanta_step) begin
        lfc_quanta_next = lfc_quanta_reg - cfg_quanta_step;
    end else begin
        lfc_quanta_next = 0;
    end
end else begin
    lfc_quanta_next = lfc_quanta_reg;
end

lfc_req_next = (lfc_quanta_reg != 0) && rx_lfc_en && cfg_rx_lfc_en;
```

注意 `lfc_req_next` 用的是 `lfc_quanta_reg`（当前值）而不是 `lfc_quanta_next`（下一值），所以 `rx_lfc_req` 与倒计时严格同步。

**收到 LFC 帧时重装量子**（[rtl/mac_pause_ctrl_rx.v:167-172](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/mac_pause_ctrl_rx.v#L167-L172)）：

```verilog
if (mcf_opcode == cfg_rx_lfc_opcode && cfg_rx_lfc_en) begin
    stat_rx_lfc_pkt_next = 1'b1;
    stat_rx_lfc_xon_next  = {mcf_params[7:0], mcf_params[15:8]} == 0;   // 量子=0 → XON
    stat_rx_lfc_xoff_next = {mcf_params[7:0], mcf_params[15:8]} != 0;   // 量子≠0 → XOFF
    lfc_quanta_next = {mcf_params[7:0], mcf_params[15:8], {QFB{1'b0}}};
end
```

`{mcf_params[7:0], mcf_params[15:8]}` 这种「交换高低字节」的写法是因为线路上是**大端序**（高位先传），而 `mac_ctrl_rx` 是按字节顺序塞进 `mcf_params` 的，第一个字节（`mcf_params[7:0]`）其实是量子的高字节。

**量子步长在 `eth_mac_1g` 中的自动计算**（[rtl/eth_mac_1g.v:531](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L531)）：

```verilog
.cfg_quanta_step(tx_mii_select ? (4*256)/512 : (8*256)/512),   // MII:2, GMII:4
.cfg_quanta_clk_en(tx_clk_enable),
```

GMII 模式：`(8*256)/512 = 4`，即每时钟减 4 个 LSB。验证：1 量子 = 256 LSB（因为左移 8 位），64 时钟减完 → 每时钟减 `256/64 = 4`，吻合。

#### 4.2.4 代码实践

**实践目标**：直接驱动 `mac_pause_ctrl_rx` 的 `mcf_*` 接口，注入一个量子为 `0x1000` 的 PAUSE，观察 `rx_lfc_req` 的持续时长。这是规格中要求的核心实践。

> 说明：`mac_pause_ctrl_rx` 的输入不是线路帧，而是 `mac_ctrl_rx` 解码后的并行 `mcf_*` 信号。所以这里直接构造 `mcf_*`，等价于「假设上游已正确解码」。规格中提到的「tx_pause_req」概念上就是「请求暂停本端 TX」，在 RTL 里对应 `rx_lfc_req`（接收侧告诉 MAC：对方让我们暂停发送）。

**操作步骤**（示例代码，需自建 testbench；可用 iverilog + cocotb）：

1. 新建 `tb/mac_pause_ctrl_rx/`，把 `rtl/mac_pause_ctrl_rx.v` 列入 `VERILOG_SOURCES`，参数 `MCF_PARAMS_SIZE=2`（仅 LFC）、`PFC_ENABLE=0`。
2. 复位后配置：
   - `cfg_rx_lfc_opcode = 16'h0001`、`cfg_rx_lfc_en = 1`
   - `rx_lfc_en = 1`、`rx_lfc_ack = 1`（表示本端确实暂停，允许倒计时走）
   - `cfg_quanta_clk_en = 1`、`cfg_quanta_step = 4`（GMII 步长）
3. 在某一拍同时拉高 `mcf_valid`、设 `mcf_opcode = 16'h0001`、设 `mcf_params = 16'h0010`。

   > **字节序要点**：要让 16 位量子值 = `0x1000`，因为 `{mcf_params[7:0], mcf_params[15:8]}` 会把第一字节当高字节，所以 `mcf_params` 寄存器值应写成 `16'h0010`（即 `mcf_params[7:0]=0x10`、`mcf_params[15:8]=0x00`）。写反会得到 `0x0010`。
4. 下一拍撤掉 `mcf_valid`，让时钟自由运行。

**需要观察的现象**：

- `mcf_valid` 那一拍之后，`rx_lfc_req` 立即为 1 并保持。
- 倒计时持续约 \( 0x1000 \times 64 = 4096 \times 64 = 262144 \) 个时钟后，`rx_lfc_req` 自动回落为 0。
- 期间 `stat_rx_lfc_xoff` 出现一个周期脉冲（XOFF），`stat_rx_lfc_xon` 不亮。

**预期结果**：

\[ N_{cycles} = \text{quanta} \times 64 = 0x1000 \times 64 = 262144 \]

由于 26 万周期仿真较慢，**建议先用小量子值（如 `0x0004`）观察 `rx_lfc_req` 持续 \(4 \times 64 = 256\) 个周期后准确回落**，再用上述公式外推 `0x1000`。**精确周期数待本地验证。**

#### 4.2.5 小练习与答案

**练习 1**：若把 `rx_lfc_ack` 接成 0（本端实际没停发），`rx_lfc_req` 还会随时间自动消失吗？

> **答案**：不会。倒计时条件是 `cfg_quanta_clk_en && rx_lfc_ack`（[mac_pause_ctrl_rx.v:141](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/mac_pause_ctrl_rx.v#L141)）。`rx_lfc_ack=0` 时倒计时冻结，`rx_lfc_req` 会一直保持，直到本端真正开始暂停（ack=1）才继续走。这是一种「对齐」机制：暂停时长从真正停发那一刻才开始计量。

**练习 2**：发送侧 `mac_pause_ctrl_tx` 为什么需要 `cfg_tx_lfc_refresh` 刷新定时器？

> **答案**：对端收到的 PAUSE 有有效期（量子倒计时）。若本端要求暂停的时间很长，单发一帧的量子可能不够覆盖整个暂停期；刷新定时器在暂停快过期前自动重发一帧（[mac_pause_ctrl_tx.v:188-199](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/mac_pause_ctrl_tx.v#L188-L199)），让对端的倒计时被不断续上，从而维持连续暂停。

---

### 4.3 PFC 优先级流控（mac_pause_ctrl_rx/tx 的 PFC 部分）

#### 4.3.1 概念说明

PFC 是 PAUSE 的「分车道」升级版。它把以太网流量按 IEEE 802.1Q 优先级标记划分成 **8 个优先级（priority 0–7）**，可以只暂停其中某几个优先级，其余优先级照常收发。这在无损以太网（用于 RoCE/iWARP 的数据中心网络）中至关重要：它能让某一类流量临时停顿而不阻塞其他流量，避免全局暂停拖垮吞吐。

PFC 帧的参数区（18 字节）布局：

| 偏移 | 内容 |
|------|------|
| 0 | 优先级使能向量低字节 |
| 1 | 保留（0） |
| 2–17 | 8 个 16 位量子值，每个优先级一个 |

opcode 为 `0x0101`。参数区里的「使能向量」是一个 8 位位图，第 k 位为 1 表示本次要对优先级 k 设置新的量子值。

#### 4.3.2 核心流程

PFC 与 LFC **共用同一份倒计时逻辑**，只是从「1 个倒计时器」扩展到「8 个独立的倒计时器 `pfc_quanta_reg[0:7]`」：

1. 收到 PFC 帧时，遍历 8 个优先级；对于使能位为 1 的优先级 k，把对应量子值左移 8 位装入 `pfc_quanta_reg[k]`。
2. 每个优先级独立倒计时，各自驱动 `rx_pfc_req[k]`。
3. `rx_pfc_req` 是 8 位向量，哪位为 1 就表示对应优先级当前被暂停。

发送侧 `mac_pause_ctrl_tx` 同理维护 8 个刷新定时器，并组装 18 字节的 PFC 参数区。当 LFC 与 PFC 同时有帧要发时，**PFC 优先**（见下文 `mcf_pfc_sel`）。

#### 4.3.3 源码精读

**PFC 帧解析——遍历 8 个优先级**（[rtl/mac_pause_ctrl_rx.v:173-182](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/mac_pause_ctrl_rx.v#L173-L182)）：

```verilog
end else if (PFC_ENABLE && mcf_opcode == cfg_rx_pfc_opcode && cfg_rx_pfc_en) begin
    stat_rx_pfc_pkt_next = 1'b1;
    for (k = 0; k < 8; k = k + 1) begin
        if (mcf_params[k+8]) begin           // 使能向量的第 k 位
            stat_rx_pfc_xon_next[k]  = {mcf_params[16+(k*16)+0 +: 8], mcf_params[16+(k*16)+8 +: 8]} == 0;
            stat_rx_pfc_xoff_next[k] = {mcf_params[16+(k*16)+0 +: 8], mcf_params[16+(k*16)+8 +: 8]} != 0;
            pfc_quanta_next[k] = {mcf_params[16+(k*16)+0 +: 8], mcf_params[16+(k*16)+8 +: 8], {QFB{1'b0}}};
        end
    end
end
```

注意：`mcf_params[k+8]` 读取的是参数区第 1 字节（偏移 8–15 位），正好是 8 位使能向量；偏移 `16+k*16` 起的 2 字节是该优先级的量子值。只有使能位为 1 的优先级才会被更新，其余保持原值。

**8 路独立倒计时**（[rtl/mac_pause_ctrl_rx.v:153-165](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/mac_pause_ctrl_rx.v#L153-L165)）：

```verilog
for (k = 0; k < 8; k = k + 1) begin
    if (cfg_quanta_clk_en && rx_pfc_ack[k]) begin
        if (pfc_quanta_reg[k] > cfg_quanta_step) begin
            pfc_quanta_next[k] = pfc_quanta_reg[k] - cfg_quanta_step;
        end else begin
            pfc_quanta_next[k] = 0;
        end
    end else begin
        pfc_quanta_next[k] = pfc_quanta_reg[k];
    end
    pfc_req_next[k] = (pfc_quanta_reg[k] != 0) && rx_pfc_en[k] && cfg_rx_pfc_en;
end
```

这与 LFC 的倒计时结构完全对称，只是 `pfc_quanta_reg` 是数组、`rx_pfc_req`/`rx_pfc_en`/`rx_pfc_ack` 都是 8 位总线。

**发送侧组装 PFC 参数区**（[rtl/mac_pause_ctrl_tx.v:134-143](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/mac_pause_ctrl_tx.v#L134-L143)）：

```verilog
wire [18*8-1:0] mcf_pfc_params;
assign mcf_pfc_params[16*0 +: 16] = {pfc_en_reg, 8'd0};          // 使能向量 + 保留
assign mcf_pfc_params[16*1 +: 16] = pfc_req_reg[0] ? {cfg_tx_pfc_quanta[16*0+0 +: 8], cfg_tx_pfc_quanta[16*0+8 +: 8]} : 0;
// ... priority 1..7 同理
```

`pfc_req_reg[k]` 为 0 的优先级量子字段写 0（即 XON，恢复）；为 1 的写配置的量子值（XOFF，暂停）。

**PFC 与 LFC 的发送仲裁**——PFC 优先（[rtl/mac_pause_ctrl_tx.v:224](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/mac_pause_ctrl_tx.v#L224)）：

```verilog
if (lfc_send_reg && !(PFC_ENABLE && cfg_tx_pfc_en && pfc_send_reg)) begin
```

即「想发 LFC，但当 PFC 也要发且使能时，让 PFC 先发」。最终经 `mcf_pfc_sel_reg` 选择把 LFC 还是 PFC 的字段送上 `mcf_*`（[mac_pause_ctrl_tx.v:146-150](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/mac_pause_ctrl_tx.v#L146-L150)）。

**在 `eth_mac_1g` 中的默认开关**（[rtl/eth_mac_1g.v:47-48](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L47-L48)）：

```verilog
parameter PFC_ENABLE = 0,
parameter PAUSE_ENABLE = PFC_ENABLE
```

默认 PFC 和 PAUSE **都关闭**（`MAC_CTRL_ENABLE = PAUSE_ENABLE || PFC_ENABLE` 为 0，整个控制帧子系统不综合）。需要流控时由用户在实例化时把 `PFC_ENABLE` 或 `PAUSE_ENABLE` 设为 1，参数区大小随之自动确定（[eth_mac_1g.v:268](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L268)）：`MCF_PARAMS_SIZE = PFC_ENABLE ? 18 : 2`。

#### 4.3.4 代码实践

**实践目标**：构造一个只暂停优先级 3 的 PFC 帧，观察只有 `rx_pfc_req[3]` 被拉高、其余位不变。

**操作步骤**（示例代码，需自建 testbench）：

1. 参数设 `MCF_PARAMS_SIZE=18`、`PFC_ENABLE=1`。
2. 配置 `cfg_rx_pfc_opcode = 16'h0101`、`cfg_rx_pfc_en = 1`、`rx_pfc_en = 8'hff`、`rx_pfc_ack = 8'hff`、`cfg_quanta_clk_en = 1`、`cfg_quanta_step = 4`。
3. 拉高 `mcf_valid` 一拍，设 `mcf_opcode = 16'h0101`，`mcf_params` 设为：
   - 使能向量（偏移 0，即 `mcf_params[7:0]`）= `0x08`（仅 bit3=1，对应优先级 3）
   - 偏移 1（`mcf_params[15:8]`）= `0x00`（保留）
   - 优先级 3 的量子字段在偏移 `16+3*16 = 64`，即 `mcf_params[64 +: 16]`，设为某个非零值如 `0x0010`（同 4.2.4 的字节序约定）。
4. 释放 `mcf_valid`，运行时钟。

**需要观察的现象**：

- `rx_pfc_req[3]` 被拉高并保持，倒计时结束后回落。
- `rx_pfc_req` 的其他 7 位保持 0 不变。
- `stat_rx_pfc_xoff[3]` 出现一个周期脉冲，其余位不亮。

**预期结果**：只有 bit3 被触发，证明 PFC 的「按优先级独立暂停」语义正确。**精确波形待本地验证。**

#### 4.3.5 小练习与答案

**练习 1**：如果 PFC 帧的使能向量把优先级 3 和优先级 5 都置 1，但优先级 5 的量子字段为 0，会发生什么？

> **答案**：优先级 3 按 XOFF 处理（暂停），优先级 5 按 XON 处理（立即恢复）。因为 `stat_rx_pfc_xon_next[k] = (量子==0)`（[mac_pause_ctrl_rx.v:177-178](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/mac_pause_ctrl_rx.v#L177-L178)）。量子 0 会把 `pfc_quanta_next[5]` 装成 0，于是 `rx_pfc_req[5]` 立即为 0。

**练习 2**：默认 `PFC_ENABLE=0` 时，`MCF_PARAMS_SIZE` 是多少？还能收 PFC 帧吗？

> **答案**：`MCF_PARAMS_SIZE = PFC_ENABLE ? 18 : 2 = 2`（[eth_mac_1g.v:268](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L268)），参数区只够装 LFC 的 2 字节量子，且 PFC 分支被 `PFC_ENABLE` 编译期屏蔽，无法收 PFC。需要 PFC 必须把 `PFC_ENABLE` 设为 1。

---

## 5. 综合实践

把本讲三个最小模块串起来，做一个端到端的流控自测。

**任务**：在 `eth_mac_1g` 顶层（或直接用四个底层模块级联）上，复现一次完整的 PAUSE 交互：

1. **接收链路**：构造一个真实 PAUSE 线路帧（`01:80:C2:00:00:01` + `0x8808` + opcode `0x0001` + 量子 `0x0010`），经 `mac_ctrl_rx` 解码，再喂给 `mac_pause_ctrl_rx`。
2. **观察**：`rx_lfc_req` 应被拉高约 \(16 \times 64 = 1024\) 个 GMII 时钟周期，期间 TX 数据通路应停止发送新帧（如果接到真实 MAC，会看到 `tx_axis_tready` 被反压或暂停）。
3. **发送链路**：人为给 `mac_pause_ctrl_tx` 的 `tx_lfc_req` 一个上升沿，观察它经 `mac_ctrl_tx` 在输出 AXI-Stream 上合成出一帧完整 PAUSE 帧（用 cocotbext-eth 的 `AxisSink` 抓帧并断言 opcode、量子字段、目的 MAC 正确）。
4. **刷新**：把 `cfg_tx_lfc_refresh` 设成较短值，在 `tx_lfc_req` 保持高期间，观察是否周期性重发 PAUSE 帧。

**验收要点**：

- 接收侧 `stat_rx_lfc_xoff` 在收到帧时脉冲一次；倒计时结束后 `rx_lfc_req` 自动回落。
- 发送侧合成的帧字段（含字节序）与预期一致。
- 注意 `cfg_quanta_step` 必须与实际 MII/GMII 模式匹配，否则暂停时长会偏差。

这是一个把「帧识别 → 语义解码 → 倒计时 → 反压 → 帧合成」整条链路打通的练习，完成后你就真正理解了 IEEE 802.3 流量控制在硬件里的落地方式。**端到端波形与周期数待本地仿真验证。**

---

## 6. 本讲小结

- **MAC 控制帧**走和普通数据相同的物理通路，靠 EtherType `0x8808` + opcode 区分；`mac_ctrl_rx` 在透传数据的同时旁路扫描头部，命中后在 `tlast` 拍从 `mcf_*` 侧信道送出解码字段。
- **`mac_ctrl_tx`** 把并行 `mcf_*` 串行化，且**控制帧优先于数据帧**插入，但不会拆散正在发送的数据帧。
- **PAUSE（LFC）** 用 16 位量子值定义暂停时长，1 量子 = 512 比特时间（GMII 下 = 64 时钟）；`mac_pause_ctrl_rx` 用 8 位小数倒计时器精确计量，`rx_lfc_req` 受倒计时 + 双重使能共同控制。
- **量子步长 `cfg_quanta_step`** 由 `eth_mac_1g` 按速率自动算出（GMII=4、MII=2），保证不同速率下时长都精确。
- **PFC** 是 PAUSE 的 8 优先级版本，参数区 18 字节含使能向量 + 8 个量子值；每个优先级独立倒计时，可只暂停部分优先级，是无损以太网的基础。
- 默认 `PFC_ENABLE=0`、`PAUSE_ENABLE=0`，整个流控子系统不综合；需要时由用户开启，参数区大小（2 或 18 字节）自动适配。

---

## 7. 下一步学习建议

- **下一讲 u4-l3**：进入 `eth_mac_1g` 顶层，看本讲的四个流控模块如何与 `axis_gmii_rx/tx`、FCS、填充子模块一起组装成完整的千兆 MAC，以及 `PTP_TS_ENABLE` 打开后的时间戳旁路。
- **横向对比**：阅读 [rtl/eth_mac_10g.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_10g.v) 中同名实例，观察 10G MAC 下流控接口（尤其 `cfg_quanta_step` 与 64 位通路）的差异，为第 9 单元做铺垫。
- **协议层延伸**：流控只决定「何时停发」，而「发给谁」由 ARP/IP 决定。学完 u4-l3 后可进入第 6 单元（ARP）与第 7 单元（IPv4），看上层协议如何复用同一条 MAC 数据通路。
- **标准阅读**：如有兴趣，对照 IEEE 802.3 Annex 31B（PAUSE）与 Annex 31D（PFC）阅读本讲源码，会发现 RTL 里的字段布局与标准文档逐一对应。
