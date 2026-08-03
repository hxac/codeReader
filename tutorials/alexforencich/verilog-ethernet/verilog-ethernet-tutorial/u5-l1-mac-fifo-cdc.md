# MAC FIFO 变体与跨时钟域

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `eth_mac_1g_fifo` / `eth_mac_1g_rgmii_fifo` 这一类带 `_fifo` 后缀的 MAC 变体**在 MAC 两侧各加了一个什么**，以及为什么要加。
- 解释 `logic_clk` / `logic_rst` 这一组新增时钟域的意义：它如何把应用侧（logic 域）与 PHY 侧（`tx_clk` / `rx_clk` 域）解耦，并让接收方向获得原本没有的 `tready` 反压能力。
- 理解底层 `axis_async_fifo`（异步、跨时钟域）与 `axis_fifo`（同步、同域缓冲）的差异：Gray 码指针、双时钟、复位同步。
- 掌握 `FRAME_FIFO` 模式下的「整帧提交 / 丢帧 / 坏帧统计」机制，以及 `TX_DROP_OVERSIZE_FRAME`、`TX_DROP_BAD_FRAME`、`TX_DROP_WHEN_FULL` 等参数的取舍。

本讲承接 [u4-l3 eth_mac_1g 核心千兆 MAC](u4-l3-eth-mac-1g-core.md)：那里我们把 `eth_mac_1g` 当作「布线层」读了一遍，本讲就看 verilog-ethernet 如何在它外面包一层 FIFO，把它变成一个**对应用侧更友好、跨时钟域、可反压、带统计**的成品 MAC。

## 2. 前置知识

### 2.1 为什么 MAC 需要外挂 FIFO

回顾 [u4-l3](u4-l3-eth-mac-1g-core.md) 与 [u4-l1](u4-l1-axis-gmii-rx-tx.md) 的两个关键事实：

1. **接收方向（RX）没有 `tready`**：`axis_gmii_rx` 是线速流式源，线上来多少字节，MAC 就吐多少字节，应用侧必须线速接走，否则只能丢。看 [rtl/eth_mac_1g.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v) 的 RX 输出端口——只有 `rx_axis_tvalid`，没有 `rx_axis_tready`。
2. **收发各自独立时钟域**：`eth_mac_1g` 的 AXI 输入运行在 `tx_clk`，AXI 输出运行在 `rx_clk`，二者都来自 PHY，未必等于 FPGA 内部应用逻辑的时钟（`logic_clk`）。

这意味着：如果你的应用逻辑跑在另一个时钟上，或者偶发地来不及处理接收帧，直接用 `eth_mac_1g` 就很难受。`_fifo` 变体就是来解决这两件事的——**用 FIFO 做时钟域跨越（CDC）+ 突发缓冲**。

### 2.2 跨时钟域（CDC）与异步 FIFO 的直觉

把数据从一个时钟域搬到另一个时钟域，最稳的硬件结构是**异步 FIFO**：

- 写侧用自己的时钟 `s_clk` 把数据顺序写进一块双端口 RAM，并把「写到了哪里」用一个**写指针**记录下来。
- 读侧用自己的时钟 `m_clk` 读出数据，并维护一个**读指针**。
- 难点在于：写指针要被读侧「看见」才能判断「还有多少没读」，而它俩不在同一个时钟里。直接传二进制多位指针很危险——多位同时翻转时，采样可能错位。

解决办法是 **Gray 码**：任意两个相邻 Gray 码只差 1 位，因此跨域采样时即便采到「翻转瞬间」的值，也最多只有 1 位处于亚稳态，再经 2 级触发器同步就能拿到一个确定的、不会错乱的指针值。

### 2.3 AXI-Stream 回顾

回顾 [u1-l3 AXI-Stream 接口约定](u1-l3-axi-stream-interface.md)：`tvalid`/`tready` 握手，`tlast` 划帧尾，`tuser` 作坏帧标志；宽度 > 8 位时出现 `tkeep` 标记末字有效字节。本讲会频繁对照 `tready` 的有无，这是理解「反压解耦」的钥匙。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `rtl/eth_mac_1g_fifo.v` | 千兆 GMII MAC 的 FIFO 封装：`eth_mac_1g` + TX/RX 两个 `axis_async_fifo_adapter` |
| `rtl/eth_mac_1g_rgmii_fifo.v` | RGMII 版的 FIFO 封装：`eth_mac_1g_rgmii` + TX/RX 两个 `axis_async_fifo_adapter` |
| `lib/axis/rtl/axis_async_fifo.v` | **异步** FIFO（双时钟域），Gray 码指针，是 `_fifo` 变体真正用的 CDC 构件 |
| `lib/axis/rtl/axis_fifo.v` | **同步** FIFO（单时钟域），二进制指针，作对照 |
| `lib/axis/rtl/axis_async_fifo_adapter.v` | 异步 FIFO + 位宽适配器（`axis_adapter`），把 8 位 GMII 与宽位 logic 接口互转 |
| `rtl/eth_mac_1g.v` | 上一讲的主角，这里只作端口对照基准 |

> 说明：`lib/axis/` 是内嵌的 verilog-axis 第三方库（vendoring，参见 [u1-l2](u1-l2-repository-structure.md)）。本讲讲到的 `axis_fifo` / `axis_async_fifo` / `axis_async_fifo_adapter` 都来自它。

## 4. 核心概念与源码讲解

### 4.1 MAC FIFO 封装：在 MAC 两侧各加一个 FIFO

#### 4.1.1 概念说明

`eth_mac_1g_fifo` 本质上是一个**布线层**——它几乎不含新逻辑，核心就是三件事：

1. 例化一个 `eth_mac_1g`（与上一讲完全相同）；
2. 在它的 TX 输入前面挂一个 **TX FIFO**（`axis_async_fifo_adapter`）；
3. 在它的 RX 输出后面挂一个 **RX FIFO**（`axis_async_fifo_adapter`）。

两个 FIFO 同时承担两项职责：**跨时钟域**（logic ↔ PHY）与**位宽适配**（宽位 logic 接口 ↔ 8 位 GMII）。应用侧只看见一组运行在 `logic_clk` 上的 AXI-Stream 接口，PHY 侧的 `tx_clk` / `rx_clk` 被藏在内部。

#### 4.1.2 核心流程

数据通路如下（`eth_mac_1g_fifo` 的 TX 与 RX 对称）：

```
TX 方向（应用 → 线）：
  logic_clk 域                 CDC + 8↔N 位宽适配            tx_clk 域
  tx_axis_tdata ──► [TX FIFO (axis_async_fifo_adapter)] ──► eth_mac_1g (TX) ──► gmii_txd

RX 方向（线 → 应用）：
  rx_clk 域                   CDC + 8↔N 位宽适配            logic_clk 域
  gmii_rxd ──► eth_mac_1g (RX) ──► [RX FIFO (axis_async_fifo_adapter)] ──► rx_axis_tdata
```

关键观察：

- **TX FIFO** 的写侧（`s_clk`）是 `logic_clk`，读侧（`m_clk`）是 `tx_clk`；输入位宽 `AXIS_DATA_WIDTH`、输出位宽 8。
- **RX FIFO** 的写侧（`s_clk`）是 `rx_clk`，读侧（`m_clk`）是 `logic_clk`；输入位宽 8、输出位宽 `AXIS_DATA_WIDTH`。
- 于是 **logic 侧的 AXI 接口可以是宽位（如 64 位）**，而 MAC 内部仍是 8 位 GMII——位宽转换由 `axis_async_fifo_adapter` 内部的 `axis_adapter` 顺带完成。

#### 4.1.3 源码精读

先看端口。`eth_mac_1g_fifo` 相比 `eth_mac_1g` 多出了一组 `logic_clk` / `logic_rst`：

新增 logic 时钟域：[rtl/eth_mac_1g_fifo.v:L55-L61](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_fifo.v#L55-L61)

```verilog
input  wire rx_clk,
input  wire rx_rst,
input  wire tx_clk,
input  wire tx_rst,
input  wire logic_clk,   // ← 新增：应用侧时钟
input  wire logic_rst,   // ← 新增：应用侧复位
```

而对外暴露的 AXI 接口（应用侧）就工作在 `logic_clk` 上，并且 **RX 侧新增了 `rx_axis_tready`**：[rtl/eth_mac_1g_fifo.v:L75-L80](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_fifo.v#L75-L80)

```verilog
output wire [AXIS_DATA_WIDTH-1:0] rx_axis_tdata,
output wire [AXIS_KEEP_WIDTH-1:0] rx_axis_tkeep,
output wire                       rx_axis_tvalid,
input  wire                       rx_axis_tready,   // ← 新增：接收侧可反压了
output wire                       rx_axis_tlast,
output wire                       rx_axis_tuser,
```

> 对比 [rtl/eth_mac_1g.v:L68-L71](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L68-L71)：原版 MAC 的 RX 输出**没有** `tready`。这正是 `_fifo` 变体的核心增值之一（详见 4.1.4 实践）。

中间的 `eth_mac_1g` 例化很「干净」——把 FIFO 输出（`tx_fifo_axis_*`，8 位，`tx_clk` 域）喂给 MAC，把 MAC 的 RX 输出（`rx_fifo_axis_*`，8 位，`rx_clk` 域）喂回 RX FIFO：[rtl/eth_mac_1g_fifo.v:L193-L227](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_fifo.v#L193-L227)

```verilog
eth_mac_1g #(
    .ENABLE_PADDING(ENABLE_PADDING),
    .MIN_FRAME_LENGTH(MIN_FRAME_LENGTH)
)
eth_mac_1g_inst (
    .tx_clk(tx_clk), .tx_rst(tx_rst),
    .rx_clk(rx_clk), .rx_rst(rx_rst),
    .tx_axis_tdata(tx_fifo_axis_tdata),   // 来自 TX FIFO
    ...
    .rx_axis_tdata(rx_fifo_axis_tdata),   // 送入 RX FIFO
    ...
);
```

注意它只透传了 `ENABLE_PADDING` / `MIN_FRAME_LENGTH` 两个参数——**PTP、PAUSE/PFC 在 `_fifo` 变体里被刻意省略了**。如果你需要 PTP 时间戳，`_fifo` 这一层会截断 `tuser` 里的时间戳旁带（因为 FIFO 把 `USER_WIDTH` 固定为 1，仅保留坏帧位），所以带 PTP 的场景通常直接用 `eth_mac_1g` 而非 `eth_mac_1g_fifo`。PTP 时间戳机制将在 [u11-l3](u11-l3-ptp-timestamp-tagging.md) 详述。

TX FIFO 的例化体现了「CDC + 位宽适配」二合一：[rtl/eth_mac_1g_fifo.v:L229-L247](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_fifo.v#L229-L247)

```verilog
axis_async_fifo_adapter #(
    .DEPTH(TX_FIFO_DEPTH),
    .S_DATA_WIDTH(AXIS_DATA_WIDTH),   // logic 侧宽位
    .M_DATA_WIDTH(8),                 // MAC 侧 8 位
    ...
    .FRAME_FIFO(TX_FRAME_FIFO),
    .DROP_OVERSIZE_FRAME(TX_DROP_OVERSIZE_FRAME),
    .DROP_BAD_FRAME(TX_DROP_BAD_FRAME),
    .DROP_WHEN_FULL(TX_DROP_WHEN_FULL)
)
tx_fifo (
    .s_clk(logic_clk), .s_rst(logic_rst),   // 写侧 = logic 域
    ...
    .m_clk(tx_clk),    .m_rst(tx_rst),      // 读侧 = PHY TX 域
    ...
);
```

RX FIFO 是镜像（`S_DATA_WIDTH=8`、`M_DATA_WIDTH=AXIS_DATA_WIDTH`、`s_clk=rx_clk`、`m_clk=logic_clk`），见 [rtl/eth_mac_1g_fifo.v:L280-L329](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_fifo.v#L280-L329)。

#### 4.1.4 代码实践：对比 `eth_mac_1g` 与 `eth_mac_1g_fifo` 的端口

> 这是本讲的主实践任务（源码阅读型）。

1. **实践目标**：通过并排读两个模块的端口列表，亲眼看清 `_fifo` 变体新增了什么、改了什么，并据此解释 `logic_clk` 域与 `ready` 反压解耦。
2. **操作步骤**：
   - 打开 [rtl/eth_mac_1g.v:L50-L71](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L50-L71)（原版 MAC 端口）。
   - 打开 [rtl/eth_mac_1g_fifo.v:L54-L91](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_fifo.v#L54-L91)（FIFO 版端口）。
   - 按下表逐项核对：
   
   | 信号 | `eth_mac_1g` | `eth_mac_1g_fifo` | 含义 |
   | --- | --- | --- | --- |
   | `logic_clk` / `logic_rst` | ❌ 无 | ✅ 新增 | 应用侧时钟域 |
   | TX AXI 时钟域 | `tx_clk` | `logic_clk` | 生产者改在 logic 域送数 |
   | RX AXI 时钟域 | `rx_clk` | `logic_clk` | 消费者改在 logic 域取数 |
   | `rx_axis_tready` | ❌ 无（线速不可反压） | ✅ 新增 | 接收侧可反压 |
   | `tx_axis_tkeep` / `rx_axis_tkeep` | ❌ 无（8 位） | ✅ 有（可宽位） | logic 侧可宽位接口 |
   | PTP / PAUSE / PFC 端口 | ✅ 有 | ❌ 省略 | FIFO 版专注数据通路 |
   | `*_fifo_overflow` / `*_fifo_good_frame` / `*_fifo_bad_frame` | ❌ 无 | ✅ 新增 | FIFO 级统计 |

3. **需要观察的现象**：`eth_mac_1g` 的 AXI 输入跨在 `tx_clk`、输出跨在 `rx_clk`，且 RX 无 `tready`；而 `eth_mac_1g_fifo` 把 AXI 全部搬到 `logic_clk`，并补出 `rx_axis_tready`。
4. **预期结果**：你能用一句话总结——「`_fifo` 变体用两个异步 FIFO 把 MAC 的 PHY 时钟域桥接到 `logic_clk` 域，并让原本线速不可反压的 RX 方向获得了 `tready`，代价是 PTP/流控旁带被截断」。
5. 反压解耦的细节：RX 侧有了 `tready` 不等于能「让线上的 PHY 停下来」（物理链路无法停），而是 **FIFO 在 `logic_clk` 侧给消费者一个握手**；一旦 FIFO 被写满（消费跟不上突发），RX FIFO 会按 `RX_DROP_WHEN_FULL` 决定整帧丢弃（见 4.3）。

#### 4.1.5 小练习与答案

**练习 1**：`eth_mac_1g_rgmii_fifo` 对外只暴露 `gtx_clk` 和 `logic_clk` 两个时钟输入，而 `eth_mac_1g_fifo` 却暴露了 `rx_clk` / `tx_clk` / `logic_clk` 三个。为什么？

> **答案**：`eth_mac_1g_rgmii_fifo` 内部例化的 `eth_mac_1g_rgmii` 自带 PHY 接口（`rgmii_phy_if`），会从 `gtx_clk` 和 `rgmii_rx_clk` **内部派生**出 `tx_clk` / `rx_clk` 并连回 FIFO（见 [rtl/eth_mac_1g_rgmii_fifo.v:L214-L252](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_rgmii_fifo.v#L214-L252)）；而 `eth_mac_1g_fifo` 里的 `eth_mac_1g` 不含 PHY 时钟生成，所以 `tx_clk` / `rx_clk` 必须由外部提供。

**练习 2**：为什么 TX 方向原本就有 `tready`，却仍然要加 FIFO？

> **答案**：TX 的 `tready` 只解决「MAC 暂时不发」的反压，但不解决 (a) 应用侧时钟域与 `tx_clk` 不同；(b) 应用侧想以宽位（如 64 位）喂数据；(c) 应用侧想整帧投递、由 FIFO 负责丢坏帧/超长帧统计。这些都需要 FIFO 兜底。

### 4.2 logic/PHY 时钟域桥接：异步 FIFO 与位宽适配

#### 4.2.1 概念说明

`_fifo` 变体跨时钟域的真正实现者是 `axis_async_fifo`（被 `axis_async_fifo_adapter` 包了一层）。它和同步版 `axis_fifo` 的根本区别在于**指针如何被对端看到**：

- **同步 `axis_fifo`**：读写同一个 `clk`，指针是普通二进制，`full` / `empty` 直接比较二进制指针即可。
- **异步 `axis_async_fifo`**：读写不同 `clk`，指针写成 **Gray 码**再经 **2 级触发器同步**送到对端，避免多位同时翻转被错采。

位宽适配则由 `axis_async_fifo_adapter` 额外承担：当输入比输出窄（如 logic 侧 64 位、MAC 侧 8 位），它先经 `axis_adapter` 把数据「拼宽」再进 FIFO；反之则先过 FIFO 再「拆窄」。这样 MAC 内部永远只见到 8 位。

#### 4.2.2 核心流程

异步 FIFO 的跨域握手（以「写侧把写指针告诉读侧」为例）：

```
写域 s_clk:  wr_ptr(二进制) --bin2gray--> wr_ptr_gray --[2级FF同步]--> 读域 m_clk
读域 m_clk:  比较 wr_ptr_gray_sync 与 rd_ptr_gray 判断 empty
```

满（`full`）判据用 Gray 码表述为：两个指针的**最高 2 位都不同、其余位相同**（这是二进制「最高位不同、其余相同」的 Gray 等价形式）。其代数形式为：

\[
\text{full} \;\Longleftrightarrow\; \text{wr\_gray} = \text{rd\_gray\_sync} \oplus \texttt{11}\underbrace{00\cdots0}_{\text{ADDR\_WIDTH}-1}
\]

空（`empty`）判据最简单：两指针完全相等。

#### 4.2.3 源码精读

**同步版 `axis_fifo`**——只有一组 `clk` / `rst`，二进制指针直接比较：[lib/axis/rtl/axis_fifo.v:L94-L95](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/lib/axis/rtl/axis_fifo.v#L94-L95) 与 [lib/axis/rtl/axis_fifo.v:L186-L201](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/lib/axis/rtl/axis_fifo.v#L186-L201)

```verilog
input wire clk,
input wire rst,
...
reg [ADDR_WIDTH:0] wr_ptr_reg = ...;
reg [ADDR_WIDTH:0] rd_ptr_reg = ...;
// full: 最高位不同、其余相同（二进制）
wire full  = wr_ptr_reg == (rd_ptr_reg ^ {1'b1, {ADDR_WIDTH{1'b0}}});
wire empty = wr_ptr_commit_reg == rd_ptr_reg;
```

**异步版 `axis_async_fifo`**——双时钟、Gray 码、2 级同步：[lib/axis/rtl/axis_async_fifo.v:L97-L112](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/lib/axis/rtl/axis_async_fifo.v#L97-L112)（`s_clk` / `m_clk` 分离）；Gray 编解码函数 [lib/axis/rtl/axis_async_fifo.v:L194-L203](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/lib/axis/rtl/axis_async_fifo.v#L194-L203)：

```verilog
function [ADDR_WIDTH:0] bin2gray(input [ADDR_WIDTH:0] b);
    bin2gray = b ^ (b >> 1);          // 二进制 → Gray
endfunction
function [ADDR_WIDTH:0] gray2bin(input [ADDR_WIDTH:0] g);
    integer i;
    for (i = 0; i <= ADDR_WIDTH; i = i + 1)
        gray2bin[i] = ^(g >> i);      // Gray → 二进制（异或归约）
endfunction
```

跨域用的 Gray 指针与 2 级同步寄存器：[lib/axis/rtl/axis_async_fifo.v:L217-L226](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/lib/axis/rtl/axis_async_fifo.v#L217-L226)

```verilog
(* SHREG_EXTRACT = "NO" *)
reg [ADDR_WIDTH:0] wr_ptr_gray_sync1_reg = ...;   // 读域第 1 级（可能亚稳态）
(* SHREG_EXTRACT = "NO" *)
reg [ADDR_WIDTH:0] wr_ptr_gray_sync2_reg = ...;   // 读域第 2 级（已稳定）
```

> `(* SHREG_EXTRACT = "NO" *)` 是告诉综合工具「别把这 2 级同步器折叠进 SRL 移位寄存器」，必须落在真正的触发器上，否则会破坏亚稳态分辨率。这是 CDC 代码的常见标注。

满 / 空判据用 Gray 码实现：[lib/axis/rtl/axis_async_fifo.v:L264-L266](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/lib/axis/rtl/axis_async_fifo.v#L264-L266)

```verilog
// full: 高 2 位都翻转、其余相同
wire full  = wr_ptr_gray_reg == (rd_ptr_gray_sync2_reg ^ {2'b11, {ADDR_WIDTH-1{1'b0}}});
// empty: 两指针相等
wire empty = FRAME_FIFO ? (rd_ptr_reg == wr_ptr_commit_sync_reg)
                        : (rd_ptr_gray_reg == wr_ptr_gray_sync2_reg);
```

复位也要跨域同步——每个域的复位都被同步到对方域后再生效：[lib/axis/rtl/axis_async_fifo.v:L356-L380](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/lib/axis/rtl/axis_async_fifo.v#L356-L380)。

**位宽适配器** `axis_async_fifo_adapter` 的策略：输出更宽就「先拼宽再进 FIFO」，输出更窄就「先出 FIFO 再拆窄」，等宽则两侧都旁路：[lib/axis/rtl/axis_async_fifo_adapter.v:L201-L240](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/lib/axis/rtl/axis_async_fifo_adapter.v#L201-L240)（上采样前置 `axis_adapter`），[lib/axis/rtl/axis_async_fifo_adapter.v:L320-L359](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/lib/axis/rtl/axis_async_fifo_adapter.v#L320-L359)（下采样后置）。

**一个容易被忽略的细节：MAC 状态脉冲也要跨域**。`eth_mac_1g` 产生的 `tx_error_underflow`、`rx_error_bad_frame` / `rx_error_bad_fcs` 是各自时钟域里的**单周期脉冲**，而 `_fifo` 变体要把它们报给 `logic_clk` 域。直接同步脉冲可能漏采，所以代码用了「**翻转 + 2 级同步 + 异或边沿检测**」的闭环脉冲同步器：[rtl/eth_mac_1g_fifo.v:L132-L160](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_fifo.v#L132-L160)

```verilog
// 源域：每来一个脉冲就翻转一次
always @(posedge tx_clk or posedge tx_rst)
    if (tx_rst) tx_sync_reg_1 <= 1'b0;
    else        tx_sync_reg_1 <= tx_sync_reg_1 ^ tx_error_underflow_int;
...
// 目的域：2 级同步（reg_2→reg_3），再延迟 1 拍（reg_4）做异或
always @(posedge logic_clk or posedge logic_rst) begin
    tx_sync_reg_2 <= tx_sync_reg_1;
    tx_sync_reg_3 <= tx_sync_reg_2;
    tx_sync_reg_4 <= tx_sync_reg_3;
end
// 相邻两拍异或 → 每个 源域脉冲 还原为一个 目的域脉冲
assign tx_error_underflow = tx_sync_reg_3[0] ^ tx_sync_reg_4[0];
```

> 注意：这组脉冲同步器与 FIFO 内部的 Gray 指针 CDC 是**两套独立机制**——FIFO 跨的是「数据流」，这套同步器跨的是「MAC 伴随状态」。两者缺一不可。

#### 4.2.4 代码实践：跟踪一条 logic→PHY 的跨域路径

1. **实践目标**：把 4.1.2 的方框图落到具体信号上，确认 TX FIFO 的写/读时钟分属不同域。
2. **操作步骤**：
   - 在 [rtl/eth_mac_1g_fifo.v:L248-L262](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_fifo.v#L248-L262) 找到 `tx_fifo` 的端口连接：`.s_clk(logic_clk)` 写侧、`.m_clk(tx_clk)` 读侧。
   - 再到 [lib/axis/rtl/axis_async_fifo.v:L583-L605](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/lib/axis/rtl/axis_async_fifo.v#L583-L605) 看读侧如何采样经同步过来的 `wr_ptr_gray_sync2_reg`。
3. **需要观察的现象**：写指针在 `logic_clk` 更新并转成 Gray 码；它经过 `wr_ptr_gray_sync1_reg` → `wr_ptr_gray_sync2_reg` 两级 `m_clk` 寄存器后才被读侧使用。
4. **预期结果**：你能指出「`logic_clk` 与 `tx_clk` 的频率/相位可以完全独立，数据不会丢、不会错位」，并解释 Gray 码 + 2 级同步在此承担了正确性。
5. 若想进一步看动态行为（指针如何随帧增长），可对照 `tb/eth_mac_1g_fifo/Makefile` 配置一套 cocotb 仿真环境观察波形（参见 4.3.4 与 [u1-l4](u4-l1-axis-gmii-rx-tx.md) 的仿真说明），具体波形**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `axis_async_fifo` 的 `full` 判据看「高 2 位都翻转」，而同步 `axis_fifo` 只看「最高 1 位翻转」？

> **答案**：Gray 码下「最高位不同、其余相同」对应二进制的满条件，但 Gray 指针跨域后，为了在「满」状态留出安全余量并区分「满」与「空」，异步 FIFO 把判据扩展到「最高 2 位都不同、其余相同」——这是 Gray 域里等价于「二进制最高位翻转」的安全形式。同步 FIFO 指针不经跨域，可直接用二进制「最高位翻转」判满。

**练习 2**：`eth_mac_1g_rgmii_fifo` 里的 2 位 `speed` 信号是用普通 2 级寄存器同步到 `logic_clk` 的（[L209-L212](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_rgmii_fifo.v#L209-L212)），为什么多位总线可以这么同步，而不像指针那样用 Gray 码？

> **答案**：`speed` 是一个**变化极慢的电平型信号**（只在链路速率切换时改变，且切换间间隔很长），而指针是**每个时钟都可能变的计数器**。对慢变多位总线，逐位 2 级同步在工程上可接受（最坏情况只是读到一次过渡值，下一拍即正确）；而对高频计数器，多位同时跳变会被错采，必须用 Gray 码。这是 CDC 里「电平」与「计数器」处理方式不同的典型例子。

### 4.3 帧 FIFO 与统计：整帧提交、丢帧策略与状态计数

#### 4.3.1 概念说明

`_fifo` 变体的 FIFO 默认开启 `FRAME_FIFO=1`（见参数 [rtl/eth_mac_1g_fifo.v:L43](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_fifo.v#L43) 与 [L49](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_fifo.v#L49)）。**帧 FIFO 模式**与普通 FIFO 的区别在于：

- **整帧不可拆**：`m_axis_tvalid` 在一帧之内不会被撤销，保证下游不会收到「半个帧」。
- **提交式写指针**：写入时用 `wr_ptr_reg`，但只有当一帧完整写完（`tlast`）且没被判坏/超长丢弃时，才把它「提交」到 `wr_ptr_commit_reg`；若帧中途被丢弃，写指针回滚到提交点。
- **可配置丢帧策略**：超长丢、坏帧丢、满了也照收（标记坏）等。
- **逐帧统计**：每帧结束产生 `overflow` / `bad_frame` / `good_frame` 脉冲，供上层计数。

#### 4.3.2 核心流程

帧 FIFO 写侧的状态流（简化伪代码）：

```
每拍写入：
  if (FIFO 已满 或 帧超长 或 正在丢弃):
      进入 drop_frame；写指针不前进
      若当前是 tlast：写指针回滚到 commit 点，置 overflow=1
  else:
      mem[wr_ptr] <= data;  wr_ptr++
      if (tlast):
          if (启用 DROP_BAD_FRAME 且 tuser 标记为坏帧):
              写指针回滚到 commit 点，置 bad_frame=1
          else:
              wr_ptr_commit <= wr_ptr   # 整帧提交
              good_frame=1
```

其精髓是：**写指针 `wr_ptr` 大胆前进，提交指针 `wr_ptr_commit` 谨慎跟进**。下游可见的数据量由 `commit` 指针决定，因此被丢弃的半帧永远不会漏到下游。

#### 4.3.3 源码精读

`axis_async_fifo` 中维护了两套指针：[lib/axis/rtl/axis_async_fifo.v:L205-L212](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/lib/axis/rtl/axis_async_fifo.v#L205-L212)

```verilog
reg [ADDR_WIDTH:0] wr_ptr_reg        = ...;   // 当前写入位置
reg [ADDR_WIDTH:0] wr_ptr_commit_reg = ...;   // 已提交（整帧完成）位置
```

帧 FIFO 的丢帧与提交流程：[lib/axis/rtl/axis_async_fifo.v:L413-L463](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/lib/axis/rtl/axis_async_fifo.v#L413-L463)，核心几行：

```verilog
if ((full && DROP_WHEN_FULL) || (full_wr && DROP_OVERSIZE_FRAME) || drop_frame_reg) begin
    // 丢弃中：写指针不前进
    drop_frame_reg <= 1'b1;
    if (s_axis_tlast) begin
        wr_ptr_reg <= wr_ptr_commit_reg;   // 帧尾回滚到提交点
        drop_frame_reg <= 1'b0;
        overflow_reg <= 1'b1;
    end
end else begin
    mem[wr_ptr_reg[...]] <= s_axis;        // 正常写入
    wr_ptr_reg <= wr_ptr_reg + 1;
    if (s_axis_tlast) begin
        if (DROP_BAD_FRAME && /* tuser 命中坏帧掩码 */) begin
            wr_ptr_reg <= wr_ptr_commit_reg;   // 坏帧：回滚
            bad_frame_reg <= 1'b1;
        end else begin
            wr_ptr_commit_reg <= wr_ptr_reg + 1;  // 好帧：提交
            good_frame_reg <= 1'b1;
        end
    end
end
```

这些逐帧脉冲再经 4.2.3 的「翻转+同步+异或」机制（`axis_async_fifo` 内部版本见 [L608-L642](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/lib/axis/rtl/axis_async_fifo.v#L608-L642)）跨到读侧，最终在 `_fifo` 变体的端口上露出为 `tx_fifo_overflow` / `tx_fifo_bad_frame` / `tx_fifo_good_frame` / `rx_fifo_overflow` / `rx_fifo_bad_frame` / `rx_fifo_good_frame`（[rtl/eth_mac_1g_fifo.v:L102-L111](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_fifo.v#L102-L111)）。

**同步 vs 异步 FIFO 对照**（本讲学习目标要求的对比）：

| 维度 | `axis_fifo`（同步） | `axis_async_fifo`（异步） |
| --- | --- | --- |
| 时钟 | 单 `clk` / `rst` | `s_clk`/`s_rst` + `m_clk`/`m_rst` |
| 指针编码 | 二进制 | Gray 码（`bin2gray` / `gray2bin`） |
| 跨域同步 | 不需要 | 2 级 `(* SHREG_EXTRACT="NO" *)` 触发器 |
| `full` 判据 | 二进制「最高位翻转、其余相同」 | Gray「最高 2 位翻转、其余相同」 |
| `empty` 判据 | `wr_commit == rd` | `rd_gray == wr_gray_sync` |
| 复位 | 单域同步 | 双域互相同步（`s_rst_sync*` / `m_rst_sync*`） |
| 状态脉冲 | 同域直出 | 翻转 + 2 级同步 + 异或到对端 |
| 典型用途 | 同域缓冲 / 整形 / 统计 | **跨时钟域**（本讲 MAC FIFO 的核心） |

注意：`eth_mac_*_fifo` 系列里**两个 FIFO 一律用异步版**（经 `axis_async_fifo_adapter`），即便你的 `logic_clk` 恰好和 PHY 时钟同频——因为同频不一定同相，异步 FIFO 对相位差是免疫的，是最稳妥的选择。

#### 4.3.4 代码实践：从 Makefile 参数理解丢帧策略

1. **实践目标**：把抽象的 `DROP_*` 参数落到一组真实配置上，推断出该配置下的行为。
2. **操作步骤**：阅读 [tb/eth_mac_1g_fifo/Makefile:L42-L56](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g_fifo/Makefile#L42-L56)，摘录参数：

   ```
   PARAM_TX_FIFO_DEPTH      := 16384
   PARAM_TX_FRAME_FIFO      := 1
   PARAM_TX_DROP_OVERSIZE_FRAME := $(TX_FRAME_FIFO)   # = 1
   PARAM_TX_DROP_BAD_FRAME  := $(TX_DROP_OVERSIZE_FRAME)  # = 1
   PARAM_TX_DROP_WHEN_FULL  := 0
   PARAM_RX_DROP_WHEN_FULL  := $(RX_DROP_OVERSIZE_FRAME) # = 1
   ```

3. **需要观察的现象 / 推断**：
   - TX 方向 `DROP_WHEN_FULL=0`：TX FIFO 满时**不**无脑接收，而是按帧丢弃（`DROP_OVERSIZE_FRAME=1`），并在帧尾回滚写指针、置 `tx_fifo_overflow`。
   - RX 方向 `DROP_WHEN_FULL=1`：RX FIFO 满时**照收**（`s_axis_tready` 恒拉高，物理上也无法反压 PHY），但收下的整帧会被标记并最终丢弃，置 `rx_fifo_overflow`。
4. **预期结果**：你能解释为什么 TX 与 RX 的 `DROP_WHEN_FULL` 默认不同——**TX 侧生产者可被反压，所以满了就丢整帧并回滚；RX 侧 PHY 不可停，所以只能先照收再丢弃**。这正呼应了 4.1 里「RX 的 `tready` 只解耦 logic 侧、不能停线」的结论。
5. 该 Makefile 同时给出了仿真需要的 `VERILOG_SOURCES` 清单（含 `axis_adapter.v` / `axis_async_fifo.v` / `axis_async_fifo_adapter.v`），可作为搭仿真环境的依据；注意与之同名的顶层 `tb/test_eth_mac_1g_fifo.{v,py}` 是 myhdl 时代的历史遗留（含 `$from_myhdl` / `from myhdl import`），已被 `tox.ini` 的 `--ignore-glob=tb/test_*.py` 排除在回归之外，详见 [u1-l4](u1-l4-testbench-and-simulation.md)，不要直接拿来当 cocotb 用例。

#### 4.3.5 小练习与答案

**练习 1**：`wr_ptr_reg` 与 `wr_ptr_commit_reg` 在帧 FIFO 里各自的作用？

> **答案**：`wr_ptr_reg` 是「当前写到哪」，每拍都可能前进；`wr_ptr_commit_reg` 是「已确认整帧完成、对下游可见的数据量」。只有好帧的 `tlast` 拍才会把 `wr_ptr_reg` 推进到 `commit`；坏帧/超长帧的写指针会回滚到 `commit`，于是被丢弃的字节永不下漏。

**练习 2**：如果想让 RX 方向「宁可标记坏帧也不要丢帧」，应改哪个参数？

> **答案**：`RX_DROP_WHEN_FULL` 控制满时的行为，但「标记而非丢弃」对应的是 `MARK_WHEN_FULL`（见 [axis_async_fifo.v:L87](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/lib/axis/rtl/axis_async_fifo.v#L87)）。注意 `MARK_WHEN_FULL` 与 `FRAME_FIFO` 互斥（代码里有 `$error` 断言），所以不能在帧模式下使用——`eth_mac_*_fifo` 默认开 `FRAME_FIFO`，因此实际只能在「丢」与「不丢但坏帧被标记并保留」之间经 `DROP_BAD_FRAME` 调整。

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「读图 + 推演」任务：

**任务**：假设你要把 `eth_mac_1g_fifo` 接到一个跑在 50 MHz `logic_clk`、64 位数据总线的软核上，而 PHY 提供 125 MHz 的 `tx_clk` / `rx_clk`（8 位 GMII）。请回答：

1. **数据通路**：画（或文字描述）出从软核 64 位 AXI 发送到 GMII `gmii_txd` 的完整路径，标出每一级的时钟域与位宽，指出位宽转换发生在 `axis_async_fifo_adapter` 的哪一侧（提示：对照 [L229-L247](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_fifo.v#L229-L247) 与 [axis_async_fifo_adapter.v:L201-L240](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/lib/axis/rtl/axis_async_fifo_adapter.v#L201-L240)）。
2. **反压推演**：若软核某时刻来不及读 RX FIFO，`rx_axis_tready` 被拉低，线上却持续来帧，会发生什么？结合 `RX_DROP_WHEN_FULL=1` 说明丢帧与 `rx_fifo_overflow` 的产生时机。
3. **CDC 正确性**：指出路径上有几处「跨时钟域」，分别用什么机制保证正确（Gray 指针？脉冲同步器？复位同步？）。

**参考要点**：

1. 软核 64 位 @ `logic_clk` →（等宽直连）→ `axis_async_fifo`（**64 位宽存储**，`logic_clk`→`tx_clk`）→ `axis_adapter`（64→8 拆窄）@ `tx_clk` → `eth_mac_1g`（8 位）→ `gmii_txd`。关键：TX fifo 的 `S_DATA_WIDTH=AXIS_DATA_WIDTH(64)`、`M_DATA_WIDTH=8`，属**输入宽、输出窄**（`M_BYTE_LANES < S_BYTE_LANES`，`EXPAND_BUS=0`），因此走 [axis_async_fifo_adapter 的「下采样后置」分支](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/lib/axis/rtl/axis_async_fifo_adapter.v#L320-L359)——**adapter 在 FIFO 之后**，FIFO 本体以 64 位宽运行。注意默认仿真配置 `AXIS_DATA_WIDTH=8` 时两侧等宽、两个 adapter 都旁路，是更简单的特例。
2. `tready` 拉低 → RX FIFO 不再被读 → 写到一定程度 `full` → 因 `RX_DROP_WHEN_FULL=1`，`s_axis_tready` 恒高继续收 PHY 字节但不提交 → 帧尾回滚写指针、置 `rx_fifo_overflow` 脉冲 → 经跨域同步后在 `logic_clk` 域可见。
3. 两处 CDC：数据流走 `axis_async_fifo` 的 Gray 指针 + 2 级同步；MAC 状态脉冲（`rx_error_bad_frame` 等）走「翻转+2 级同步+异或」；另有双域复位同步。

## 6. 本讲小结

- `eth_mac_*_fifo` 是「`eth_mac_*` + TX/RX 两个 `axis_async_fifo_adapter`」的布线层，核心增值是**跨时钟域 + 位宽适配 + 突发缓冲**。
- 它新增了 `logic_clk` / `logic_rst` 域，把应用侧 AXI 从 PHY 时钟域解放出来；并给原本线速不可反压的 **RX 方向补上了 `rx_axis_tready`**（反压解耦）。
- 代价是 PTP / PAUSE / PFC 旁带被截断（`USER_WIDTH` 降为 1），所以需要 PTP 时间戳时应直接用 `eth_mac_1g`。
- 跨时钟域由 `axis_async_fifo` 用 **Gray 码指针 + 2 级 `SHREG_EXTRACT=NO` 同步** 实现；MAC 状态脉冲另用「翻转 + 同步 + 异或」闭环同步器跨域；`speed` 这类慢变多位信号用普通 2 级寄存器同步即可。
- `FRAME_FIFO` 模式用「写指针 + 提交指针」保证整帧不可拆、坏/超长帧可回滚丢弃，并在帧尾产出 `overflow` / `bad_frame` / `good_frame` 统计脉冲。
- 同步 `axis_fifo`（单时钟、二进制指针）与异步 `axis_async_fifo`（双时钟、Gray 指针）的差异，决定了前者只适合同域整形、后者才是 MAC FIFO 的 CDC 选择。

## 7. 下一步学习建议

- **向上**：进入 [u6 ARP 子系统](u6-l1-arp-eth-rx-tx.md)，看应用侧的 AXI-Stream 接口如何接上协议栈（`_fifo` 变体正是协议栈与 MAC 之间最常见的衔接点）。
- **横向**：阅读 `rtl/eth_mac_10g_fifo.v`，对照本讲理解 64 位宽 MAC 的 FIFO 封装（位宽适配方向相反）。
- **深入 CDC**：精读 `lib/axis/rtl/axis_async_fifo.v` 全文，重点理解 `wr_ptr_update_*` 那一套「按帧提交后才同步指针」的握手（[L388-L396](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/lib/axis/rtl/axis_async_fifo.v#L388-L396)），这是帧 FIFO 在异步场景下的精妙之处。
- **板级参考**：看 `example/*/fpga/rtl/fpga_core.v` 如何实例化 `eth_mac_*_rgmii_fifo` 并把 `logic_clk` 侧接到 `udp_complete`，这是 `_fifo` 变体最典型的真实用法（详见 [u12-l1](u12-l1-udp-echo-system.md)）。
