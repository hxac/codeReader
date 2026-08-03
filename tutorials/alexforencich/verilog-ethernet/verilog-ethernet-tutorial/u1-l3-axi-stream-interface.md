# AXI-Stream 接口约定

## 1. 本讲目标

verilog-ethernet 里的几乎所有模块（MAC、成帧、ARP、IP、UDP、FIFO……）都通过同一种接口交换数据，它叫 **AXI4-Stream**（本项目里简称 **AXI-Stream** 或 **axis**）。接口本身只有 5~6 根信号，但全库 90 多个文件都遵守同一套约定。学完本讲，你应当能够：

1. 说出 `tdata / tvalid / tready / tlast / tuser / tkeep` 每一根信号的含义和方向。
2. 看懂 README 里的握手波形，并能解释“在哪个时钟沿数据真正被传走”。
3. 理解 `tlast` 划定帧边界、`tuser` 标记坏帧、`tkeep`（仅 64 位通路）标记末字有效字节这三件事。
4. 拿到任意一个模块的端口列表，能立刻识别出哪几根是 AXI-Stream 信号、它用的是 8 位还是 64 位通路、支不支持反压（backpressure）。

本讲不涉及任何协议（ARP/IP/UDP 都在后面），只讲“数据怎么在模块之间流动”这一件事。这是后续所有讲义的共同语言。

## 2. 前置知识

- **时钟域与同步逻辑**：FPGA 里的寄存器在时钟上升沿采样。本讲所有的时序都以“某个 `clk` 的上升沿”为时间单位。
- **主/从（source/sink）方向**：产生数据的一方叫 **源（source / master）**，接收数据的一方叫 **宿（sink / slave）**。在本项目端口命名里，`m_axis_*`（master）是模块往外送的 AXI-Stream，`s_axis_*`（slave）是模块往里收的 AXI-Stream。
- **帧（frame / packet）**：以太网以“帧”为单位收发，一帧有明确的起止。本讲的 AXI-Stream 同样以“帧”为逻辑单位，但物理上被切成一个个时钟周期的“节拍（beat / transfer）”。
- **数据通路宽度**：u1-l1 已建立的概念——本项目有 8 位（千兆）和 64 位（10G/25G）两档通路。这一点直接决定 AXI-Stream 是否出现 `tkeep`。

如果“时钟上升沿采样”“主从方向”这些说法让你感到陌生，建议先动手画一两个波形再往下读。

## 3. 本讲源码地图

本讲的“权威定义”只有一处：README 的两小节。配合三个真实 RTL 文件把定义落实成代码。

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md) | 用一段“Common signals”定义全库信号语义，再用 4 段 ASCII 波形展示典型时序。 |
| [rtl/eth_axis_rx.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_rx.v) | 以太网帧解析器，端口里能看到一套**完整的、带反压**的 8 位 AXI-Stream（含 `tready`、可选 `tkeep`）。 |
| [rtl/axis_gmii_rx.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v) | GMII 接收器，演示一种**精简的** AXI-Stream 源：没有 `tready`、没有 `tkeep`，并把 `tuser` 从 1 位扩展成“PTP 时间戳 + 坏帧位”。 |
| [rtl/ip_eth_tx_64.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_tx_64.v) | 64 位 IP 成帧器，端口里能看到 64 位 `tdata` 配 8 位 `tkeep` 的标准 64 位写法。 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**信号语义** → **帧边界与坏帧标志** → **握手时序**。三者层层递进，先认识信号，再认识一帧，最后认识一次传输。

### 4.1 AXI-Stream 信号语义

#### 4.1.1 概念说明

AXI-Stream 是 ARM 定义的一种“流式”接口，专门用来在两个模块之间搬运一串连续数据（尤其适合以太网帧）。它把“数据”和“控制”拆成几根独立的信号：

| 信号 | 方向 | 位宽 | 含义 |
| --- | --- | --- | --- |
| `tdata` | 源→宿 | `DATA_WIDTH` | 这一拍真正要传的数据。 |
| `tvalid` | 源→宿 | 1 | 源宣告：“本拍数据有效”。 |
| `tready` | 宿→源 | 1 | 宿宣告：“我这一拍能收”。 |
| `tlast` | 源→宿 | 1 | 标记当前拍是**一帧的最后一拍**。 |
| `tuser` | 源→宿 | 1（可扩展） | 本项目里固定含义：**坏帧标志**（仅与最后一拍同时有意义）。 |
| `tkeep` | 源→宿 | `KEEP_WIDTH` | 每 1 位对应 `tdata` 里 1 个字节是否有效；**只在 64 位等宽通路出现**。 |

README 把这几根信号的语义浓缩在一段“Common signals”里，是全库最权威的一句定义：

> tdata : Data (width generally DATA_WIDTH)
> tkeep : Data word valid (width generally KEEP_WIDTH, present on _64 modules)
> tvalid : Data valid
> tready : Sink ready
> tlast : End-of-frame
> tuser : Bad frame (valid with tlast & tvalid)
>
> —— [README.md:L420-L427](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L420-L427)

几个要点先记住：

- **只有当 `tvalid` 与 `tready` 同时为 1 的那个时钟沿，这一拍才算真正传走**（叫一次 *transfer* / *handshake*）。详见 4.3。
- **`tdata` 的位宽就是“数据通路宽度”**：千兆模块 8 位、10G/25G 模块 64 位（个别用 32 位）。所以吞吐量等于
  \[ \text{吞吐量} = \text{DATA\_WIDTH} \times f_{\text{clk}} \]
  例如 8 位 × 125 MHz = 1 Gb/s；64 位 × 156.25 MHz = 10 Gb/s。这印证了 u1-l1 讲过的“速率与位宽绑定”。
- **`tkeep` 只出现在宽度大于 8 位的通路上**，因为 8 位通路每一拍正好 1 个字节，根本不存在“半个字节”的问题。本项目里
  \( \text{KEEP\_WIDTH} = \text{DATA\_WIDTH} / 8 \)，例如 64 位通路的 `tkeep` 是 8 位。`tkeep[i]` 对应 `tdata` 里第 `i` 个字节（低位对应低字节）。

#### 4.1.2 核心流程

一个 AXI-Stream 接口的每一拍可以这样描述（伪代码）：

```
每个 clk 上升沿：
    if (tvalid && tready):
        # 这一拍传走了
        处理 tdata
        if (tlast):
            本帧结束；若 tuser==1 则本帧是坏帧
            下一拍（若 tvalid）属于新的一帧
    # tvalid 或 tready 任一为 0，则本拍无传输，源/宿可各自继续等待
```

注意“帧”是逻辑概念，接口本身只用 `tlast` 把一串节拍切成帧，并没有单独的“帧开始”信号——新一帧从上一帧 `tlast` 之后的下一个有效拍开始。

#### 4.1.3 源码精读

**一、`tkeep` 是可选的：`KEEP_ENABLE` 参数。**
`eth_axis_rx` 是一个参数化的 8 位/宽位通用的成帧器，它用两个参数把 `tkeep` 做成可选：

```verilog
// Width of AXI stream interfaces in bits
parameter DATA_WIDTH = 8,
// Propagate tkeep signal
// If disabled, tkeep assumed to be 1'b1
parameter KEEP_ENABLE = (DATA_WIDTH>8),
// tkeep signal width (words per cycle)
parameter KEEP_WIDTH = (DATA_WIDTH/8)
```

—— [eth_axis_rx.v:L36-L43](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_rx.v#L36-L43)

关键直觉：当 `DATA_WIDTH` 等于 8 时，`KEEP_ENABLE` 自动取 0，模块认为“每个字节都有效”，于是 `tkeep` 实际上不存在。这正是全库“8 位模块没有 `tkeep`、64 位模块才有”的根因。

**二、一套完整的 AXI-Stream 输入口。**
紧跟着就是 `eth_axis_rx` 的 slave 端口，6 根信号一次到齐：

```verilog
input  wire [DATA_WIDTH-1:0] s_axis_tdata,
input  wire [KEEP_WIDTH-1:0] s_axis_tkeep,
input  wire                  s_axis_tvalid,
output wire                  s_axis_tready,
input  wire                  s_axis_tlast,
input  wire                  s_axis_tuser,
```

—— [eth_axis_rx.v:L51-L56](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_rx.v#L51-L56)

注意 `s_axis_tready` 是 `output`——反压信号由**接收方（这个模块）**驱动，这正是“宿给源”的方向。模块还加了一条断言，要求位宽必须是 8 的整数倍：

```verilog
if (BYTE_LANES * 8 != DATA_WIDTH) begin
    $error("Error: AXI stream interface requires byte (8-bit) granularity ...");
```

—— [eth_axis_rx.v:L90-L96](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_rx.v#L90-L96)

**三、64 位通路的 `tkeep` 长什么样。**
切到 `ip_eth_tx_64`（10G/25G 的 IP 成帧器），它的输入 payload 是标准 64 位写法：`tdata` 64 位、`tkeep` 8 位，正好 \( 64/8 = 8 \)。

```verilog
input wire [63:0] s_ip_payload_axis_tdata,
input wire [7:0]  s_ip_payload_axis_tkeep,
input wire        s_ip_payload_axis_tvalid,
output wire       s_ip_payload_axis_tready,
input wire        s_ip_payload_axis_tlast,
input wire        s_ip_payload_axis_tuser,
```

—— [ip_eth_tx_64.v:L57-L62](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_tx_64.v#L57-L62)

对比上面 8 位的 `eth_axis_rx`，你就能看出位宽差异如何体现在端口上：**只是 `DATA_WIDTH`/`KEEP_WIDTH` 的数字不同，信号名和握手规则完全一致**。这正是 AXI-Stream 作为“通用接口”的价值——同一套规则横跨千兆和 10G。

#### 4.1.4 代码实践（源码阅读型）

**目标**：用真实端口验证“8 位无 `tkeep`、64 位有 `tkeep`、`tkeep` 位宽 = 字节数”这条规律。

**操作步骤**：

1. 打开 [eth_axis_rx.v:L48-L71](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_rx.v#L48-L71)，数一数它的输入 `s_axis_*` 有几根、输出 `m_eth_payload_axis_*` 有几根。
2. 打开 [ip_eth_tx_64.v:L57-L77](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_tx_64.v#L57-L77)，对比 `tdata`/`tkeep` 的位宽。

**需要观察并填写下表**：

| 模块 | 通路宽度 | `tdata` 位宽 | 是否有 `tkeep` | `tkeep` 位宽 |
| --- | --- | --- | --- | --- |
| `eth_axis_rx`（默认参数） | 8 | ? | ? | ? |
| `ip_eth_tx_64` | 64 | ? | ? | ? |

**预期结果**：`eth_axis_rx` 默认 `DATA_WIDTH=8`、`KEEP_ENABLE=0`，故 `tkeep` 缺省（视为常 1）；`ip_eth_tx_64` 的 `tdata` 64 位、`tkeep` 8 位。

#### 4.1.5 小练习与答案

**练习 1**：一个模块声明了 `input wire [7:0] s_axis_tkeep`，你能推断它的 `tdata` 是几位、属于哪一档速率吗？
**答**：`tkeep` 8 位 ⇒ `KEEP_WIDTH=8` ⇒ `DATA_WIDTH=64`，是 64 位通路，对应 10G/25G。

**练习 2**：为什么 8 位通路的模块几乎都省略 `tkeep`？
**答**：8 位通路每拍正好 1 字节，没有“末字不满”的情况，`tlast` 之外不需要再标记哪些字节有效，所以 `KEEP_ENABLE` 默认为 0、`tkeep` 被视为常 1。

---

### 4.2 帧边界与坏帧标志（tlast / tuser）

#### 4.2.1 概念说明

一帧由若干拍组成，但接口里**没有专门的“帧开始”信号**，怎么知道一帧从哪儿到哪儿？靠两根信号：

- **`tlast`**：源在**一帧的最后一拍**把它拉高。从“上一拍 `tlast=1` 的下一拍”到“本拍 `tlast=1`”之间所有有效拍，就是一帧。两帧之间**不需要空闲周期**，可以背靠背紧挨着（见 4.3）。
- **`tuser`**：本项目固定用它当**坏帧标志**。README 写得很明确：“Bad frame (valid with tlast & tvalid)”——也就是说 `tuser` **只在最后一拍（与 `tlast` 同时）才有意义**：`tuser=1` 表示这帧坏了（比如 FCS 校验错、接收过程出错），`tuser=0` 表示好帧。帧中间的那些拍，`tuser` 一般保持 0（视为无关）。

> 一个常见困惑：`tuser` 不是“每一拍都有效的用户自定义数据”吗？在通用 AXI-Stream 里确实可以，但**本项目把它收窄成 1 位的坏帧标志**，并且在打开 PTP 时把它“扩展”成“时间戳 + 坏帧位”。所以读本项目代码时，`tuser` 就按“坏帧位”理解即可。

#### 4.2.2 核心流程

接收侧对一帧的处理可以这样概括：

```
进入一帧：
    逐拍收 tdata，做 CRC/解析……
最后一拍（tvalid && tlast）：
    if tuser == 1:   标记本帧为坏帧，丢弃或上报告警
    else:            本帧有效，交付上层
```

发送侧反过来：源在拼好整帧后，在最后一拍同时给出 `tlast=1` 和 `tuser=0`（好帧）；若自己已经知道这帧有问题，就给 `tuser=1`。

#### 4.2.3 源码精读

`axis_gmii_rx` 是把 GMII 物理信号（`gmii_rxd/gmii_rx_dv/gmii_rx_er`）转成 AXI-Stream 帧的接收器，它演示了三件事：一个**没有 `tready` 的精简源**、`tuser` 作为坏帧位、以及 PTP 打开时 `tuser` 被扩展。

**一、精简的 AXI-Stream 输出（无 `tready`、无 `tkeep`）。**

```verilog
output wire [DATA_WIDTH-1:0]    m_axis_tdata,
output wire                     m_axis_tvalid,
output wire                     m_axis_tlast,
output wire [USER_WIDTH-1:0]    m_axis_tuser,
```

—— [axis_gmii_rx.v:L55-L58](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v#L55-L58)

注意它**没有 `m_axis_tready` 输入**。原因很直观：GMII 接收是“线缆送来什么就得收什么”，下游没法让对端暂停，所以这个源不提供反压入口（它就是一个无反压的流式源）。这正是“并非每个 AXI-Stream 都用全 6 根信号”的活样本——下游若跟不上，只能靠 FIFO 缓冲（见 u5-l1）。模块还用断言把位宽钉死在 8 位：

```verilog
if (DATA_WIDTH != 8) begin
    $error("Error: Interface width must be 8");
```

—— [axis_gmii_rx.v:L85-L90](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v#L85-L90)

**二、坏帧如何变成 `tuser=1`。**
在 `STATE_PAYLOAD` 状态里，模块处理三类帧尾：传输错误、FCS 正确、FCS 错误。三者在最后一拍分别给 `tlast` 和 `tuser` 赋值：

```verilog
if (gmii_rx_dv_d4 && gmii_rx_er_d4) begin
    // error
    m_axis_tlast_next = 1'b1;
    m_axis_tuser_next = 1'b1;          // 错误 → 坏帧
    error_bad_frame_next = 1'b1;
    ...
end else if (!gmii_rx_dv) begin
    // end of packet
    m_axis_tlast_next = 1'b1;
    if (... FCS 字节里有错误 ...) begin
        m_axis_tuser_next = 1'b1;      // 坏帧
    end else if ({gmii_rxd_d0,...,gmii_rxd_d3} == ~crc_next) begin
        m_axis_tuser_next = 1'b0;      // FCS 正确 → 好帧
    end else begin
        m_axis_tuser_next = 1'b1;      // FCS 错误 → 坏帧
        error_bad_fcs_next = 1'b1;
    end
end
```

—— [axis_gmii_rx.v:L207-L228](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v#L207-L228)

这段代码完美对应 README 的定义：`tuser` 只在 `tlast` 那一拍被赋成有意义的 0/1，其余节拍它是默认的 0。

**三、`tuser` 被扩展成“时间戳 + 坏帧位”。**
当 `PTP_TS_ENABLE=1` 时，模块要把接收时间戳随帧带走，于是把 `tuser` 从 1 位扩成 `USER_WIDTH` 位，把坏帧位拼到最低位、时间戳拼在高位：

```verilog
assign m_axis_tuser = PTP_TS_ENABLE ? {ptp_ts_reg, m_axis_tuser_reg} : m_axis_tuser_reg;
```

—— [axis_gmii_rx.v:L143-L146](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v#L143-L146)

`USER_WIDTH` 的定义见端口上方的参数（[axis_gmii_rx.v:L36-L40](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v#L36-L40)）：关闭 PTP 时为 1（就是坏帧位），打开 PTP 时为 `PTP_TS_WIDTH + 1`。这是一个关键设计：**坏帧位永远占据 `tuser` 的最低位，其余位用来夹带 sideband（如时间戳）**。这条约定到 u11-l3 讲 PTP 时间戳提取时还会用到。

#### 4.2.4 代码实践（源码阅读型）

**目标**：跟踪 `axis_gmii_rx` 在“好帧 / FCS 错帧 / 接收错误帧”三种情况下，`tlast` 和 `tuser` 的最终取值。

**操作步骤**：

1. 阅读 [axis_gmii_rx.v:L200-L233](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v#L200-L233)（`STATE_PAYLOAD` 状态）。
2. 注意它判断“FCS 正确”用的是 `{gmii_rxd_d0,d1,d2,d3} == ~crc_next`——即把最后 4 字节与 CRC 比较取反值比较（CRC-32 的标准校验判断）。

**需要观察的现象**：无论哪种结束，`tlast` 都会在最后一拍置 1；区别只在于 `tuser`。坏帧场景下 `error_bad_frame` / `error_bad_fcs` 也会同时拉高，作为额外的状态输出。

**预期结果**：

| 场景 | `tlast`（末拍） | `tuser`（末拍） |
| --- | --- | --- |
| FCS 正确 | 1 | 0 |
| FCS 错误 | 1 | 1 |
| 接收过程 `gmii_rx_er` 置位 | 1 | 1 |

#### 4.2.5 小练习与答案

**练习 1**：为什么 `axis_gmii_rx` 没有 `tready` 输入，而 `eth_axis_rx` 有？
**答**：`axis_gmii_rx` 的数据来自线缆，物理上无法暂停对端，是无反压的流式源；`eth_axis_rx` 收的是上游 AXI-Stream，上游可以被反压，所以需要 `tready`。

**练习 2**：打开 PTP 后，下游想判断“这帧是不是坏帧”，应该看 `tuser` 的哪一位？
**答**：最低位（`tuser[0]`）。`PTP_TS_ENABLE` 时 `tuser = {ptp_ts, bad_frame}`，坏帧位始终在最底，时间戳在高位。

---

### 4.3 握手时序（tvalid / tready）

#### 4.3.1 概念说明

`tvalid` 和 `tready` 这对信号组成 **valid-ready 握手**，是 AXI-Stream 的心脏。规则只有一条：

> **在某个时钟上升沿，只有当 `tvalid=1` 且 `tready=1` 同时成立，这一拍的 `tdata/tlast/tuser/tkeep` 才算被传走（一次 transfer）。**

由此推出几条行为：

- 源产生数据时拉高 `tvalid`，宿愿意收时拉高 `tready`；只要两者没有同时为 1，数据就“原地等待”。
- 任一方都可以**随时**拉低自己的信号来节流：源没数据就把 `tvalid` 拉低；宿忙不过来就把 `tready` 拉低。这就是**反压（backpressure）**。
- AXI 规范要求：**源一旦把 `tvalid` 拉高，在握手完成之前不许撤回**（数据必须保持稳定）。本项目模块都遵守这一条。
- 因为没有“帧开始”信号，所以**两帧可以紧挨着**：上一帧 `tlast=1` 的那一拍之后，紧接着的下一个 `tvalid=1` 拍就是下一帧的第一拍——中间不需要任何空闲。

#### 4.3.2 核心流程

```
for 每个时钟上升沿:
    transfer = tvalid && tready      # 是否真正传走
    if transfer:
        consume(tdata, tkeep, tlast, tuser)
    # tvalid、tready 互相独立，可各自变化
```

下面三个波形（直接取自 README）展示了三种典型场景，请重点体会“`tvalid && tready` 同为 1 的沿”。

#### 4.3.3 源码精读

**场景 A：带头部握手的单包传输**（README 第 1 段波形）。这里还演示了 `hdr_valid/hdr_ready`——本项目成帧器常用一对独立的头部握手信号，把 14 字节以太网头部单独传一次，再接载荷的 AXI-Stream。注意 `tready` 在传输中途短暂拉低，于是 `tvalid` 保持、传输暂停几拍后继续：

```
              __    __    __    __    __    __    __
clk        __/  \__/  \__/  \__/  \__/  \__/  \__/  \__
hdr_ready  ____________                   ___________
                       \_________________/
               _____ 
hdr_valid  ___/     \_______________________________
                   _____
hdr_data   XXX_HDR_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
                   ___________ _____ _____
tdata      XXXXXXXX_A0________X_A1__X_A2__XXXXXXXXXX
                   ___________ _____ _____
tkeep      XXXXXXXX_K0________X_K1__X_K2__XXXXXXXXXX
                   _______________________
tvalid     ________/                       \________
                             _________________
tready     __________________/                 \____
                                         _____
tlast      ____________________________/     \_____
tuser      __________________________________________
```

—— [README.md:L516-L537](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L516-L537)

读法：只有 `tvalid` 与 `tready` 都高的沿（A0、A1、A2 各一个）才真正传数据；中间 `tready` 被拉低的几拍，源保持 `tvalid` 与 `tdata` 不变，等待宿恢复——这就是反压。

**场景 B：宿每个字节后暂停一次**（README 第 2 段波形），演示 `tready` 频繁起伏：

—— 完整波形见 [README.md:L540-L555](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L540-L555)。可以看到 `tready` 像脉冲一样，宿每收一字节就停一拍，`tvalid` 一直保持，传输被拉长。

**场景 C：两包背靠背、无停顿**（README 第 3 段波形），本讲综合实践的主角：

```
              __    __    __    __    __    __    __    __    __
clk       __/  \__/  \__/  \__/  \__/  \__/  \__/  \__/  \__/  \__
                  _____ _____ _____ _____ _____ _____
tdata  XXXXXXXXX_A0__X_A1__X_A2__X_B0__X_B1__X_B2__XXXXXXXXXXXX
                  _____ _____ _____ _____ _____ _____
tkeep  XXXXXXXXX_K0__X_K1__X_K2__X_K0__X_K1__X_K2__XXXXXXXXXXXX
                  ___________________________________
tvalid ________/                                   \___________
tready  ________________________________________________________
                          _____             _____
tlast  ____________________/     \___________/     \___________
tuser  ________________________________________________________
                       ↑A包末拍     ↑B包末拍
                        A2          B2
```

—— [README.md:L558-L573](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L558-L573)

**两帧如何区分**：A 包的末拍是 A2（`tlast=1`），紧接着的下一个有效拍 **B0 就是 B 包的第一拍**——两包之间没有任何空闲周期。`tvalid` 在整个 6 拍区间一直为 1（因为 `tready` 也一直为 1，每拍都完成握手），`tuser` 全程 0（都是好帧）。

**场景 D：坏帧**（README 第 4 段波形）：唯一与好帧不同的，是在 `tlast=1` 的末拍上 `tuser` 也被拉成 1：

—— 见 [README.md:L576-L591](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L576-L591)。把这条波形和场景 C 对比，就能直观看到 `tuser` 的作用。

> 小结：这四段波形几乎涵盖了你在本项目里会遇到的所有 AXI-Stream 时序形态。日后调试任何模块，回到 README 这四张图就能对照。

#### 4.3.4 代码实践（波形绘制型，必做）

**目标**：把场景 C（两包背靠背）亲手画一遍，确保你能从波形反推出“第二包从哪一拍开始”。

**操作步骤**：

1. 在纸上（或任意画图工具）画一条时钟 `clk`，至少 9 个上升沿。
2. 画出两包各 3 拍的数据：A0、A1、A2、B0、B1、B2，分别占据 6 个连续的拍。
3. 画 `tvalid`：在 A0 起始沿拉高、B2 结束后拉低，中间不断。
4. 画 `tready`：**全程拉高**（无停顿场景）。
5. 画 `tlast`：仅在 A2、B2 两个拍上各出一个 1 拍宽的高脉冲。
6. 画 `tuser`：全程 0。
7. **标注第二包起始**：在 B0 这一拍上方画一个箭头，写明“第二包（B）第一拍，紧接 A 包末拍 A2，无空闲”。

**需要观察的现象**：A2 和 B0 是相邻的两个时钟周期——A2 是 `tlast=1` 的末拍，B0 是下一拍 `tvalid=1` 的首拍，两者之间没有空隙。

**预期结果**：你画出的波形应当与 [README.md:L558-L573](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L558-L573) 一致；尤其确认“第二包起始 = 上一包 `tlast` 之后的下一个 `tvalid` 拍”。

> 想加一步可选的运行验证（待本地验证）：配好 cocotb + iverilog 后，本库 `tb/` 下的端点驱动（如 `axis_ep.py` 中的 `AXIStreamFrame`，见 [axis_ep.py:L29-L48](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/axis_ep.py#L29-L48)）能把一段字节构造成 AXI-Stream 帧。后续 u1-l4、u13 会讲如何实际跑仿真；本讲先用波形建立直觉即可。

#### 4.3.5 小练习与答案

**练习 1**：在场景 C 里，如果把 `tready` 在 A2 这一拍拉低，A2 还算被传走吗？
**答**：不算。`tlast` 只表示“这是末拍”，真正传走仍需 `tvalid && tready`。`tready` 为 0 时 A2 原地等待，源必须保持 `tvalid=1` 和数据不变，直到 `tready` 恢复。

**练习 2**：源能不能在 `tvalid=1` 但还没握手时，把 `tdata` 改成另一个值？
**答**：不能。AXI-Stream 规范要求源在 `tvalid` 拉高后、握手完成前保持数据稳定，否则宿会在不该采样的时候采到错值。

---

## 5. 综合实践

把本讲三个最小模块串起来，做一次“端口考古”：

1. **挑一个真实模块**：打开 [eth_axis_rx.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_rx.v)。
2. **识别 AXI-Stream 端口**：在它的端口列表里找出输入侧（`s_axis_*`）和输出侧（`m_eth_payload_axis_*`）的 6 根信号，逐一标注方向（源→宿还是宿→源）。
3. **判断能力**：它有没有 `tready`（能不能反压）？它是不是 64 位（有没有 `tkeep`）？参考 [eth_axis_rx.v:L48-L71](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_rx.v#L48-L71)。
4. **画出一次传输**：假设上游给它喂一帧，载荷为 3 拍（D0、D1、D2），下游全程 ready。画出 `s_axis_tvalid / s_axis_tready / s_axis_tdata / s_axis_tlast / s_axis_tuser` 五条线，并指出哪一拍是末拍、`tuser` 在末拍应取何值（好帧）。
5. **对照答案**：末拍 D2 上 `tlast=1`、`tuser=0`；其余拍 `tlast=0`；`tvalid` 在 D0~D2 期间为 1、之后为 0；`tready` 全程 1。若你的图与 [README.md:L516-L537](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L516-L537) 的形态一致（去掉头部那段），就对了。

完成这一题，意味着你已经能用 AXI-Stream 的语言读懂任意模块的端口和时序。

## 6. 本讲小结

- AXI-Stream 用 6 根信号搬运数据：`tdata`（数据）、`tvalid`/`tready`（握手）、`tlast`（帧尾）、`tuser`（坏帧位）、`tkeep`（末字有效字节，仅 64 位）。权威定义见 [README.md:L420-L427](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L420-L427)。
- **只有 `tvalid && tready` 同为 1 的时钟沿才算一次传输**；任一方拉低即可节流（反压），但源在握手前不许撤回 `tvalid` 或改 `tdata`。
- 帧的边界完全由 `tlast` 划定，两帧可背靠背无空闲；`tuser` 仅在末拍有意义，1 = 坏帧。
- `tkeep` 只出现在宽度 > 8 位的通路（`KEEP_WIDTH = DATA_WIDTH/8`），8 位通路默认省略；典型对照见 [eth_axis_rx.v:L36-L43](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_rx.v#L36-L43) 与 [ip_eth_tx_64.v:L57-L62](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_tx_64.v#L57-L62)。
- 并非每个接口都用全 6 根信号：无反压的流式源（如 [axis_gmii_rx.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v)）可省 `tready`；`tuser` 还能被扩展成“时间戳 + 坏帧位”以夹带 sideband。
- README 的四段 ASCII 波形（[L514-L591](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L514-L591)）是日后调试任何模块时序的速查表。

## 7. 下一步学习建议

- 接下来 **u1-l4（测试框架与仿真运行方式）** 会告诉你怎么把本讲画的波形用 cocotb 真正“跑”出来，并用 `axis_ep.py` 这类端点驱动 AXI-Stream 接口。
- 想立刻看到 AXI-Stream 在真实成帧器里如何被解析/封装，可直接跳读 **u3-l1（eth_axis_rx/tx 成帧）**——本讲的 `eth_axis_rx` 就是那里的主角。
- 若你对 `tuser` 被扩展来夹带 PTP 时间戳感到好奇，可以提前扫一眼 [ptp_ts_extract.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_ts_extract.v)，完整的讲解在 **u11-l3**。
