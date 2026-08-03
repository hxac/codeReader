# XGMII 与 64 位 AXI-Stream 互转

## 1. 本讲目标

本讲进入 10G/25G 以太网的数据通路。学完后你应当能够：

- 说清 XGMII（10 Gigabit Media Independent Interface）接口的「数据 + 控制」双总线结构，以及 `IDLE`/`START`/`TERM`/`ERROR` 四个控制字符的作用。
- 理解一帧在 XGMII 上是如何被 `START` 与 `TERM` 两个定界符夹住的，以及 64 位通路为什么允许帧从 lane 0 或 lane 4 起始。
- 掌握 `axis_xgmii_rx_64` 如何用一个 `tkeep` 移位公式精确剥离末尾 4 字节 FCS，并用「魔数残留法」一次性校验 CRC。
- 掌握 `axis_xgmii_tx_64` 如何重组线路帧（前导码/SFD、可选填充、追加 FCS、IFG/DIC），把 64 位 AXI-Stream 还原成 XGMII。
- 看懂 32 位变体与 64 位变体在状态机、前导码打包、起始 lane 上的差异。

本讲是 [u4-l1（GMII/MII 与 AXI-Stream 互转）](u4-l1-axis-gmii-rx-tx.md) 的「宽位宽升级版」：GMII 是 8 位、125 MHz、1 Gbps；XGMII 把数据加宽到 32/64 位、156.25/161.13 MHz，对应 10G/25G。两者的成帧思路一脉相承，区别全在「宽位宽带来的边界对齐问题」上。

## 2. 前置知识

- **AXI-Stream 接口**（见 [u1-l3](u1-l3-axi-stream-interface.md)）：`tdata`/`tvalid`/`tready`/`tlast`/`tkeep`/`tuser` 的语义。本讲的 64 位通路上 `KEEP_WIDTH = DATA_WIDTH/8 = 8`，`tkeep` 每一位对应一个字节。
- **以太网帧结构**：前导码（7 字节 `0x55`）+ SFD（1 字节 `0xD5`）+ 目的 MAC + 源 MAC + EtherType + 载荷 + FCS（4 字节 CRC-32）。
- **CRC-32 与 FCS**（见 [u2-l1](u2-l1-lfsr-crc-engine.md)、[u2-l2](u2-l2-ethernet-fcs.md)）：以太网 FCS 是参数固定的 CRC-32（多项式 `0x04C11DB7`、初值 `0xFFFFFFFF`、反射、末尾取反）。一个关键性质是「魔数残留」：把**整帧（含 FCS）**喂进 CRC-32 引擎，正确帧会留下一个固定残留值，接收方据此判好坏帧而无需真正剥出 FCS 再比较。
- **GMII 收发器**（见 [u4-l1](u4-l1-axis-gmii-rx-tx.md)）：`axis_gmii_rx/tx` 用「逐字节累加 CRC + 帧尾比对」的方式处理 8 位通路。本讲的 64 位模块把同样的思路「并行展开」到一拍处理 8 字节。

> **lane（通道）**：XGMII 把一个 64 位字看成 8 个并列的 8 位「车道」，编号 lane 0（最低字节）到 lane 7（最高字节）。每个 lane 配 1 位控制位。后文反复用到 lane 概念。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [rtl/axis_xgmii_rx_64.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_rx_64.v) | XGMII（64 位）→ AXI-Stream 接收器：剥离前导码/SFD、剥离 FCS、校验 CRC、报告坏帧。 |
| [rtl/axis_xgmii_tx_64.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_tx_64.v) | AXI-Stream → XGMII（64 位）发送器：补前导码/SFD、可选填充、追加 FCS、管理帧间隔 IFG/DIC。 |
| [rtl/axis_xgmii_rx_32.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_rx_32.v) | 32 位接收器，结构同 64 位版但 lane 数减半，起始只允许 lane 0。 |
| [rtl/axis_xgmii_tx_32.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_tx_32.v) | 32 位发送器，前导码需跨两个字，状态机多出 `PREAMBLE` 与 `FCS_3` 两个状态。 |

四个模块都把 CRC 计算委托给 [rtl/lfsr.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/lfsr.v)（见 [u2-l1](u2-l1-lfsr-crc-engine.md)），本讲不重复讲解 LFSR 内部，只关注它如何被「并行展开」使用。

## 4. 核心概念与源码讲解

### 4.1 XGMII 数据与控制字符

#### 4.1.1 概念说明

GMII 用 `txd[7:0]` + `tx_en` + `tx_er` 三根语义不同的线表达「这是数据 / 现在有效 / 出错了」。XGMII 把它改造成更适合高速并行的一种结构：**两根等宽总线并行**。

- `rxd`/`txd`（DATA_WIDTH 位）：数据总线，按字节分 lane。
- `rxc`/`txc`（CTRL_WIDTH = DATA_WIDTH/8 位）：**逐 lane** 的控制位。某 lane 的控制位为 1，表示该 lane 当前是「控制字符」而非数据，此时对应字节是如表所列的特殊字符；控制位为 0，表示该 lane 是普通数据字节。

这样每拍 64 位数据 + 8 位控制，就能在一拍里同时表达「数据字节」和「帧定界/空闲/错误」等控制语义，不需要额外信号线。

四个控制字符定义在两个模块里完全一致：

| 字符 | 值 | 含义 |
| --- | --- | --- |
| `XGMII_IDLE` | `0x07` | 空闲，线路无帧 |
| `XGMII_START` | `0xfb` | 帧起始定界符（替代前导码首字节） |
| `XGMII_TERM` | `0xfd` | 帧终止定界符 |
| `XGMII_ERROR` | `0xfe` | 错误（发送欠载 / 接收坏块时插入） |

#### 4.1.2 核心流程

线路空闲时，每个 lane 都发 `{XGMII_IDLE}` 且控制位全 1。一帧在线路上长这样（按 lane 0→7 的时间-空间展开）：

```
IDLE IDLE ... | START d0 d1 d2 d3 d4 d5 d6 | d7 ... dn | ... dn+1 TERM IDLE IDLE ...
控制: 1 1 ...    1     0 0 0 0 0 0 0           0          0      1     1 1
```

- `START` 出现的 lane 之后的同拍数据字节，是该帧的最前面几个字节；
- 其后若干拍全是数据（控制位全 0）；
- 直到某一拍的某个 lane 出现 `TERM`，该帧在该 lane 处结束；
- `TERM` 之后的 lane 重新变成 `IDLE`。

发送出错（如 AXI 源供不上数据）时，TX 把后续 lane 全置为 `XGMII_ERROR` 再接 `TERM`，通知对端丢帧。

#### 4.1.3 源码精读

控制字符在 RX/TX 两侧用相同的 `localparam` 定义：

[rtl/axis_xgmii_rx_64.v:94-102](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_rx_64.v#L94-L102) 定义了 `ETH_PRE`/`ETH_SFD` 与四个 XGMII 控制字符（`IDLE=07`/`START=fb`/`TERM=fd`/`ERROR=fe`）。

接收侧每拍都把控制 lane 的数据「屏蔽成 0」并标记 `TERM` 位置：

```verilog
// rtl/axis_xgmii_rx_64.v:196-201
for (j = 0; j < 8; j = j + 1) begin
    xgmii_rxd_masked[j*8 +: 8] = xgmii_rxc[j] ? 8'd0 : xgmii_rxd[j*8 +: 8];
    xgmii_term[j] = xgmii_rxc[j] && (xgmii_rxd[j*8 +: 8] == XGMII_TERM);
end
```

这段组合逻辑做两件事：① 把控制 lane（`xgmii_rxc[j]==1`）的数据替换成 0，得到 `xgmii_rxd_masked` 喂给 CRC；② 用 `xgmii_term[j]` 记下「lane j 是否是 `TERM`」。TX 侧对应的「屏蔽」逻辑在 [rtl/axis_xgmii_tx_64.v:230-234](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_tx_64.v#L230-L234)，用 `tkeep` 把无效字节清零。

TX 的错误终止状态用一个字表达 `TERM + 7×ERROR`：

```verilog
// rtl/axis_xgmii_tx_64.v:478-489（STATE_ERR）
xgmii_txd_next = {XGMII_TERM, {7{XGMII_ERROR}}};
xgmii_txc_next = {CTRL_WIDTH{1'b1}};
```

#### 4.1.4 代码实践

**目标**：建立对「空闲字」与控制字符值的直觉。

1. 打开 [rtl/axis_xgmii_rx_64.v:98-102](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_rx_64.v#L98-L102)，抄下四个控制字符的值。
2. 复位/空闲时整拍线路字是什么？看 testbench 初值 [tb/test_axis_xgmii_rx_64.v:49-50](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/test_axis_xgmii_rx_64.v#L49-L50)：`xgmii_rxd = 64'h0707070707070707`、`xgmii_rxc = 8'hff`。
3. **需要观察的现象**：空闲拍 = 8 个 `0x07` + 控制位全 1。
4. **预期结果**：你能口算出空闲字 `0x0707070707070707`，并解释「控制位全 1」意味着 8 个 lane 都在发 `IDLE`。

#### 4.1.5 小练习与答案

- **Q1**：为什么 XGMII 要给每个 lane 配一位控制位，而不是像 GMII 那样用 `tx_en` 一根线？  
  **A1**：因为一拍里有 8 个字节，它们可能「一部分是数据、一部分是 `TERM`/`IDLE`」。逐 lane 控制位才能在一拍内同时表达帧尾（`TERM`）与紧随其后的 `IDLE`，避免拆成多拍。
- **Q2**：`xgmii_rxc[j] == 1` 但 `xgmii_rxd` 对应字节不是任何已知控制字符时，接收侧会怎样？  
  **A2**：它既不匹配 `TERM` 也不被当作数据（被屏蔽成 0），通常会被状态机当成 framing error（坏帧）处理。

---

### 4.2 帧定界：起始字符与终止字符

#### 4.2.1 概念说明

XGMII 没有单独的「帧起始信号」或「帧长」字段，完全靠两个控制字符定界：

- **`START`（0xfb）**：标志一帧开始。它「替换掉」前导码的第一个字节 `0x55`——所以标准前导码是 7×`0x55`+SFD，而 XGMII 上是 `START` + 6×`0x55` + `0xD5`（SFD）。
- **`TERM`（0xfd）**：标志一帧结束。`TERM` 可以出现在任意 lane，从而表达「本拍前若干字节是帧的最后字节」。

64 位通路有一个 32 位通路没有的能力：**`START` 可以出现在 lane 0 或 lane 4**。这是为了配合 DIC（Deficit Idle Count，缺陷填充）机制——为了让帧能更紧凑地背靠背发送，允许新帧从半字（lane 4）开始。

#### 4.2.2 核心流程

**接收侧 `START` 检测**：每拍检查两个位置：

```
if (lane0 == START)  → 帧从 lane 0 起，整字有效
else if (lane4 == START) → 帧从 lane 4 起，需要「lane 对齐」
```

当 `START` 在 lane 4 时，本拍的有效数据是 lane 5-7（3 字节），其余 5 字节（lane 0-3）是上一帧之后的 `IDLE`。为了后续按「整字」处理，RX 必须把数据**重新对齐到 lane 0**：把本拍低 32 位与「缓存的上拍高 32 位」拼接，等效于把数据流左移 4 字节。

**终止 `TERM` 检测**：在 8 个 lane 里从高到低找第一个 `TERM`，其 lane 号 `term_lane` 决定本拍有多少有效字节（见 4.3）。

#### 4.2.3 源码精读

RX 在时序逻辑里同时检查 lane 0 与 lane 4 的 `START`，并分别置 `lanes_swapped` 标志：

[rtl/axis_xgmii_rx_64.v:375-391](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_rx_64.v#L375-L391) —— lane 0 命中则 `xgmii_start_d0<=1`、`lanes_swapped<=0`；lane 4 命中则 `xgmii_start_swap<=1`、`lanes_swapped<=1`。

lane 4 起始时的「重新对齐」靠缓存拼接实现：

```verilog
// rtl/axis_xgmii_rx_64.v:340-355（lanes_swapped 分支核心）
xgmii_rxd_d0 <= {xgmii_rxd_masked[31:0], swap_rxd};   // 本拍低32 + 上拍高32
xgmii_rxc_d0 <= {xgmii_rxc[3:0], swap_rxc};
```

其中 `swap_rxd` 在 [L324](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_rx_64.v#L324) 缓存了上一拍的高 32 位。这样无论帧从 lane 0 还是 lane 4 起，后续看到的都是「从 lane 0 开始的连续数据流」。

`start_packet` 是给上层（PTP 时间戳）用的 2 位信号，编码起始位置：`2'b01`=lane 0 起，`2'b10`=lane 4 起（见 [L395](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_rx_64.v#L395) 与 [L406](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_rx_64.v#L406)）。lane 4 起始时，时间戳还做了半拍插值 `ptp_ts + (ts_inc_reg >> 1)`（[L397](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_rx_64.v#L397)），因为 `START` 实际发生在字的中点。

#### 4.2.4 代码实践

**目标**：理解 testbench 里一个真实存在的「反误触发」测试。

1. 阅读 [tb/test_axis_xgmii_rx_64.py:435-466](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/test_axis_xgmii_rx_64.py#L435-L466)（旧 myhdl 版 test 7）的注释：*"Ensure 0xfb in FCS in lane 4 is not detected as start code in lane 0"*。
2. 思考：FCS 的某个字节可能恰好等于 `0xfb`（`START` 的值）。如果它落在 lane 4，RX 是否会把它误判为新帧起始？
3. **需要观察的现象**：该测试断言 `not error_bad_frame_asserted` 且帧正常解析。
4. **预期结果**：因为 RX 只在「空闲态（控制位为 1 的 IDLE）」里找 `START`，帧内数据（FCS 也是数据，控制位为 0）里的 `0xfb` 不会触发起始检测。**待本地验证**：若环境就绪，可在 `tb/axis_xgmii_rx_64/` 下 `make` 跑全量回归确认。

#### 4.2.5 小练习与答案

- **Q1**：为什么 64 位 RX 要支持 lane 4 起始，32 位 RX 却只支持 lane 0？  
  **A1**：DIC 以 4 字节为粒度借还空闲字节。64 位字 = 8 字节，半字 = 4 字节，正好是一个 DIC 粒度，所以允许 lane 4 起；32 位字 = 4 字节，半字 = 2 字节，不是 DIC 粒度，故只允许 lane 0。见 32 位版 [rtl/axis_xgmii_rx_32.v:332](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_rx_32.v#L332) 只检查 lane 0。
- **Q2**：`START` 替换了前导码的第一个 `0x55`，那 RX 输出给上层的是带前导码的帧吗？  
  **A2**：不是。RX 把 `START` + 剩余前导码 + SFD 全部剥离，只输出目的 MAC 起始的帧内容（见 4.3 与综合实践里 `rx_frame.tdata == test_data` 的断言）。

---

### 4.3 64 位接收：tkeep 映射与 FCS 校验/剥离

#### 4.3.1 概念说明

这是 64 位通路最核心、也最巧妙的部分。问题陈述：

- 一帧的长度任意，末尾大概率不是整 8 字节对齐，所以最后一拍是「部分有效」的字。
- 末尾 4 字节是 FCS，必须从输出中剥离，但又要参与 CRC 校验。
- 在不知道帧长的前提下，怎么一边流式输出、一边在帧尾精确地切掉 FCS？

`axis_xgmii_rx_64` 的解法是：**用一个两拍延时流水线（`xgmii_rxd_d0` → `xgmii_rxd_d1`）让「检测到 `TERM`」比「输出对应数据」早一拍**，从而在输出最后一拍时已经知道 `TERM` 的 lane 号，用一个移位公式一次性算出 `tkeep`。

#### 4.3.2 核心流程

设 `TERM` 出现在 lane `t`（即 `term_lane = t`，取值 0..7）。记 `TERM` 所在的那一拍为「term 字」，它前一拍为「P 字」。帧的末尾字节分布在这两拍里：

- term 字的 lane 0..t-1 是帧的最后 t 个数据字节（其中含 FCS），lane t 是 `TERM`，其后是 `IDLE`。
- P 字是整 8 字节数据。

FCS 是帧的最后 4 字节。按 `t` 分两种情况：

| term_lane t | FCS 位置 | P 字有效载荷字节 | 输出拍 & tkeep |
| --- | --- | --- | --- |
| 0 | 全在 P 字（lane4-7） | 4（lane0-3） | STATE_PAYLOAD，`0xFF >> 4 = 0x0F` |
| 1 | P 字 lane5-7 + term 字 lane0 | 5（P 的 0-4） | STATE_PAYLOAD，`0xFF >> 3 = 0x1F` |
| 2 | P 字 lane6-7 + term 字 lane0-1 | 6 | `0xFF >> 2 = 0x3F` |
| 3 | P 字 lane7 + term 字 lane0-2 | 7 | `0xFF >> 1 = 0x7F` |
| 4 | 全在 term 字（lane0-3） | 8（P 字全有效） | STATE_PAYLOAD，`0xFF >> 0 = 0xFF` |
| 5 | term 字 lane1-4 | term 字还有 1 字节载荷 | STATE_LAST，`0xFF >> 7 = 0x01` |
| 6 | term 字 lane2-5 | term 字还有 2 字节载荷 | STATE_LAST，`0xFF >> 6 = 0x03` |
| 7 | term 字 lane3-6 | term 字还有 3 字节载荷 | STATE_LAST，`0xFF >> 5 = 0x07` |

规律：

- t ≤ 4：term 字里没有载荷字节（全是 FCS+TERM），只需输出 P 字，`tkeep = 0xFF >> (4 - t)`，本拍同时拉高 `tlast`，结束。
- t > 4：term 字里还有 (t − 4) 字节载荷，先输出 P 字（`tkeep=0xFF`、`tlast=0`），再花一拍（STATE_LAST）输出 term 字的载荷，`tkeep = 0xFF >> (12 - t)`，本拍 `tlast=1`。

FCS 本身从不进入 AXI 输出——它被 CRC 引擎「顺路」消费掉了。

#### 4.3.3 源码精读

`TERM` 在 t ≤ 4 时的 `tkeep` 移位公式：

```verilog
// rtl/axis_xgmii_rx_64.v:251-268（STATE_PAYLOAD 内 term_present 分支）
if (term_lane_reg <= 4) begin
    m_axis_tkeep_next = {KEEP_WIDTH{1'b1}} >> (CTRL_WIDTH-4-term_lane_reg);  // = 0xFF>>(4-t)
    m_axis_tlast_next = 1'b1;
    ... CRC 命中则好帧，否则 tuser[0]=1、error_bad_fcs ...
    state_next = STATE_IDLE;
end else begin
    state_next = STATE_LAST;   // t>4 需要额外一拍
end
```

t > 4 时 STATE_LAST 的 `tkeep`：

```verilog
// rtl/axis_xgmii_rx_64.v:277-280
m_axis_tkeep_next = {KEEP_WIDTH{1'b1}} >> (CTRL_WIDTH+4-term_lane_d0_reg);  // = 0xFF>>(12-t)
```

FCS 校验用「魔数残留法」。由于 term 字里 `TERM`/`IDLE` 这些控制 lane 被屏蔽成 0（见 4.1.3），喂进 CRC 的末拍带有 (8−t) 个尾零，残留值随 `t` 不同而不同，故预先比较 8 个魔数：

[rtl/axis_xgmii_rx_64.v:155-162](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_rx_64.v#L155-L162) 定义 8 个 `crc_valid[k] = (crc_next == ~magic[k])`。CRC 引擎本身是单个 64 位并行实例（[rtl/axis_xgmii_rx_64.v:177-191](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_rx_64.v#L177-L191)），每拍算一次 `crc_next`，再与 8 个魔数同时比对，按 `term_lane` 选对应那一位判断好坏。这正是 [u2-l2](u2-l2-ethernet-fcs.md) 提到的 64 位 FCS 「魔数残留法」在此处的落地。

#### 4.3.4 代码实践（本讲主实践）

**目标**：用 XGMII 源驱动 `axis_xgmii_rx_64`，发送一帧「末尾非整字」的数据，验证输出 `tkeep`/`tlast` 正确反映边界、FCS 被剥离。

参照真实存在的 cocotb 测试 [tb/axis_xgmii_rx_64/test_axis_xgmii_rx_64.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/axis_xgmii_rx_64/test_axis_xgmii_rx_64.py) 与 Makefile [tb/axis_xgmii_rx_64/Makefile](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/axis_xgmii_rx_64/Makefile)。

操作步骤（示例代码，基于该测试已用的 `cocotbext.eth.XgmiiSource`）：

```python
# 示例代码：节选并改写自 tb/axis_xgmii_rx_64/test_axis_xgmii_rx_64.py
from cocotbext.eth import XgmiiFrame, XgmiiSource
from cocotbext.axi import AxiStreamBus, AxiStreamSink

# tb.source = XgmiiSource(dut.xgmii_rxd, dut.xgmii_rxc, dut.clk, dut.rst)
# tb.sink   = AxiStreamSink(AxiStreamBus.from_prefix(dut, "m_axis"), dut.clk, dut.rst)

payload = bytearray(range(61))                 # 61 字节 -> 整帧 61+4(FCS)=65 字节
                                          #   末拍 t = 65 mod 8 = 1 -> term_lane=1
frame = XgmiiFrame.from_payload(payload)        # 自动加前导码/SFD/FCS/TERM
await tb.source.send(frame)

rx = await tb.sink.recv()
assert rx.tdata == payload        # FCS 与前导码被剥离，只剩载荷
assert rx.tuser & 1 == 0          # 好帧
assert rx.tlast == 1              # 末拍
# 手算：65 字节 = 8*8 + 1，最后一拍 1 字节有效 -> tkeep 应为 0x01
```

需要观察的现象：
1. `rx.tdata == payload`（FCS/前导码被剥离）。
2. `rx.tuser & 1 == 0`（FCS 校验通过）。
3. 最后一拍的 `tkeep` 与上表一致（例如 65 字节帧 `tkeep=0x01`）。

预期结果：仿真通过所有断言。**若未配置 cocotb/iverilog 环境**，可退化为「源码阅读型实践」：取 `payload_len=61`，按 4.3.2 的表手算 `term_lane` 与 `tkeep`，再到 [rtl/axis_xgmii_rx_64.v:255](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_rx_64.v#L255) 与 [:280](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_rx_64.v#L280) 核对移位公式。

> 运行方式：进入 `tb/axis_xgmii_rx_64/` 执行 `make`（需 cocotb + iverilog + cocotbext-eth + cocotbext-axi）。Makefile 在 [L32-L33](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/axis_xgmii_rx_64/Makefile#L32-L33) 声明只编译 `rtl/axis_xgmii_rx_64.v` 与 `rtl/lfsr.v`。

#### 4.3.5 小练习与答案

- **Q1**：一帧总长（含 FCS，不含前导码/SFD）为 100 字节，`term_lane` 是多少？最后一拍 `tkeep` 是多少？  
  **A1**：100 mod 8 = 4，所以 `term_lane = 4`，落在 t ≤ 4 区间，输出 P 字、`tkeep = 0xFF >> (4-4) = 0xFF`、`tlast=1`，单拍结束。
- **Q2**：为什么需要 STATE_LAST，而不能像 t ≤ 4 那样一拍结束？  
  **A2**：t > 4 时 term 字里除 FCS 外还有 (t−4) 字节载荷需要输出，这只能放在第二拍；P 字仍按 `tkeep=0xFF` 在第一拍输出。
- **Q3**：把 FCS 某一位翻转后再发送，`tkeep` 会变吗？坏帧怎么上报？  
  **A3**：`tkeep` 不变（它只由 `term_lane` 决定）；但 `crc_next` 不再等于任何魔数，于是 `m_axis_tuser[0]=1`、`error_bad_frame` 与 `error_bad_fcs` 同时拉高（[L262-L267](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_rx_64.v#L262-L267)）。

---

### 4.4 64 位发送：成帧、FCS 追加与 IFG/DIC

#### 4.4.1 概念说明

`axis_xgmii_tx_64` 是 RX 的逆运算：吃 64 位 AXI-Stream（带 `tkeep`/`tlast`/`tuser`），吐 XGMII 线路字。它要补齐 RX 剥掉的东西，并管理线路规范要求的帧间隔：

- **补前导码/SFD**：在帧前插 `START` + 6×`0x55` + `0xD5`。
- **可选填充（PAD）**：短帧补 0 到 `MIN_FRAME_LENGTH`（默认 64）。
- **追加 FCS**：在帧尾追加 4 字节 `~crc_state`（末尾取反）。
- **帧间隔 IFG / 缺陷填充 DIC**：帧与帧之间至少留 12 字节空闲；DIC 允许把不足整字的空闲「借」给下一帧从 lane 4 起始，平均仍满足规范。

#### 4.4.2 核心流程

状态机：`IDLE → PAYLOAD → (PAD) → FCS_1 → FCS_2 → (IFG) → IDLE`，外加错误态 `ERR`。

```
IDLE     : 等待 AXI 数据；命中后本拍发 {SFD, 6×PRE, START}（控制位仅 lane0=1）
PAYLOAD  : 逐拍输出 AXI 数据（控制位全0），同时累加 CRC
PAD      : 短帧补 0，继续累加 CRC
FCS_1/2  : 把 ~crc_state 与剩余数据/TERM/IDLE 拼成 1~2 拍线路字
ERR      : 发 {TERM, 7×ERROR}
IFG      : 发 IDLE，按 ifg_count 倒计时；DIC 决定是否让下一帧 lane4 起始
```

`DIC` 的关键：`deficit_idle_count_reg`（2 位）记录「欠」了多少空闲字节。当剩余 IFG 不足以让下帧从 lane 0 起、但又 ≥ 4 字节时，就让下帧从 lane 4 起（`swap_lanes=1`），把差额记入 deficit，留待后续帧偿还。这使平均 IFG 仍达标，同时提升吞吐。

#### 4.4.3 源码精读

`IDLE` 命中时，**一个 64 位字就装下了 `START`+完整前导码+SFD**（这是 64 位相对 32 位的优势）：

```verilog
// rtl/axis_xgmii_tx_64.v:344-350
xgmii_txd_next = {ETH_SFD, {6{ETH_PRE}}, XGMII_START};  // lane0=START,...,lane7=SFD
xgmii_txc_next = 8'b00000001;   // 仅 lane0 是控制字符
state_next = STATE_PAYLOAD;
```

`tkeep` 到「空字节数」的转换用 `keep2empty` 函数（[rtl/axis_xgmii_tx_64.v:212-225](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_tx_64.v#L212-L225)），它假设有效字节从 lane 0 起连续：

```verilog
8'bzzz01111: keep2empty = 3'd4;   // tkeep=0x0F -> 末尾 4 字节空
8'b11111111: keep2empty = 3'd0;   // tkeep=0xFF -> 无空字节
```

FCS 追加：因为最后一拍可能有 0..7 个有效字节，TX 实例化了 **8 个不同 `DATA_WIDTH`（8,16,…,64）的 `lfsr`**，分别预算「最后一拍只处理 n 字节」时的 CRC，再用 `casez(s_empty)` 选出正确的 `~crc_state` 拼进线路字（[rtl/axis_xgmii_tx_64.v:189-210](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_tx_64.v#L189-L210) 的 generate 与 [:237-296](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_tx_64.v#L237-L296) 的 FCS 拼装）。例如 `s_empty=0`（末拍满字）时：

```verilog
// rtl/axis_xgmii_tx_64.v:288-294（s_empty==0）
fcs_output_txd_0 = s_tdata_reg;                                   // 本拍全数据
fcs_output_txd_1 = {{3{XGMII_IDLE}}, XGMII_TERM, ~crc_state_reg[7][31:0]}; // FCS+TERM+IDLE
```

DIC 与 lane 交换的输出级在时序逻辑里：

[rtl/axis_xgmii_tx_64.v:619-625](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_tx_64.v#L619-L625) —— `swap_lanes_reg=1` 时把线路字高低 32 位交换，等效让 `START` 落到 lane 4；`deficit_idle_count` 的更新逻辑在 [L453-L477](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_tx_64.v#L453-L477) 与 [L504-L518](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_tx_64.v#L504-L518)。

#### 4.4.4 代码实践

**目标**：观察 `ENABLE_PADDING` 与 FCS/TERM 拼装。

1. 实例化 `axis_xgmii_tx_64`，从 AXI 侧发一帧 40 字节载荷（小于 64）。
2. 在 `xgmii_txd`/`xgmii_txc` 上抓波形，找到 `START`（lane0 的 `0xfb`）。
3. 数从 `START` 之后到 `TERM` 之间的数据字节数。
4. **需要观察的现象**：`ENABLE_PADDING=1` 时输出被补到 60 字节 + 4 字节 FCS = 64 字节线路数据；`TERM` 后跟 `IDLE`。
5. **预期结果**：`ENABLE_PADDING=0` 时只有 40 字节 + 4 FCS，更短。可改 `PARAM_ENABLE_PADDING` 在 Makefile 里对比两次波形。**待本地验证**具体字节数。

#### 4.4.5 小练习与答案

- **Q1**：为什么 TX 要例化 8 个 `lfsr` 而不是一个？  
  **A1**：最后一拍的有效字节数（0..7）决定了 CRC 要「少移」几次。8 个不同 `DATA_WIDTH` 的引擎并行预算所有可能，运行时按 `s_empty` 选一个，避免在帧尾插入额外延迟周期。
- **Q2**：`cfg_ifg` 设为 0 会怎样？  
  **A2**：TX 仍会强制最小 IFG（见 [L439](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_tx_64.v#L439) 的 `cfg_ifg > 12 ? cfg_ifg : 12`），并通过 DIC 让后续帧尽量紧凑（可从 lane 4 起）。

---

### 4.5 32 位变体与 64 位的差异

#### 4.5.1 概念说明

32 位变体（`axis_xgmii_rx_32`/`tx_32`）面向 4 字节/拍、312.5 MHz 的 40G/单 lane 或较低频 10G 场景。它和 64 位版思路一致，但因 lane 数减半，有三处明显不同。

#### 4.5.2 核心流程与差异

| 方面 | 64 位 | 32 位 |
| --- | --- | --- |
| 字/拍 | 8 字节 | 4 字节 |
| 起始 lane | lane 0 或 lane 4 | 仅 lane 0 |
| 前导码打包 | 1 拍（`START`+6×PRE+SFD） | 2 拍（IDLE 发 `START`+3×PRE，`PREAMBLE` 态发 3×PRE+SFD） |
| RX 状态机 | `IDLE/PAYLOAD/LAST`（无 PREAMBLE） | `IDLE/PREAMBLE/PAYLOAD/LAST` |
| TX 状态机 | 7 态（无 PREAMBLE） | 9 态（多 `PREAMBLE` 与 `FCS_3`） |
| CRC 引擎数 | 8 个（DATA_WIDTH 8..64） | 4 个（DATA_WIDTH 8..32） |
| 魔数个数 | 8 | 4 |

为什么 32 位 RX 多一个 `PREAMBLE` 态？因为 32 位一拍只装 4 字节，`START`+前导码+SFD 共 8 字节要跨两拍：第一拍 `START+3×PRE` 之后，还剩 3×PRE+SFD 要消费，需要专门的 `PREAMBLE` 态吞掉它，下一拍才是真正的帧数据。64 位一拍就装完了，所以 `IDLE` 命中 `START` 后直接进 `PAYLOAD`。

#### 4.5.3 源码精读

32 位 RX 只检测 lane 0 起始、无 lane 交换：

[rtl/axis_xgmii_rx_32.v:332](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_rx_32.v#L332) `xgmii_start_d0 <= xgmii_rxc[0] && xgmii_rxd[7:0] == XGMII_START;`。状态机多了 `STATE_PREAMBLE`（[L103-107](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_rx_32.v#L103-L107)），魔数只剩 4 个（[L145-148](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_rx_32.v#L145-L148)）。

32 位 TX 的前导码分两拍：

```verilog
// rtl/axis_xgmii_tx_32.v:322（STATE_IDLE）  xgmii_txd_next = {{3{ETH_PRE}}, XGMII_START};
// rtl/axis_xgmii_tx_32.v:339（STATE_PREAMBLE） xgmii_txd_next = {ETH_SFD, {3{ETH_PRE}}};
```

9 态状态机见 [rtl/axis_xgmii_tx_32.v:116-125](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_tx_32.v#L116-L125)，比 64 位多了 `PREAMBLE` 与 `FCS_3`（FCS 跨 3 拍）。

#### 4.5.4 代码实践

**目标**：用状态计数验证前导码打包差异。

1. 分别打开 [rtl/axis_xgmii_tx_64.v:330-350](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_tx_64.v#L330-L350) 与 [rtl/axis_xgmii_tx_32.v:307-345](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_tx_32.v#L307-L345)。
2. 数从 `START` 出现到第一拍帧数据出现，各经过几个时钟。
3. **需要观察的现象**：64 位 1 拍，32 位 2 拍。
4. **预期结果**：相同线路字节（`START`+6×PRE+SFD）下，32 位因位宽减半多花 1 拍。

#### 4.5.5 小练习与答案

- **Q1**：把 64 位 TX 的 `START`+前导码那拍直接搬到 32 位行不行？  
  **A1**：不行。32 位一拍只有 4 字节，装不下 `START`+6×PRE+SFD 共 8 字节，必须拆两拍，因此多了 `PREAMBLE` 态。
- **Q2**：32 位 RX 为什么没有 lane 交换逻辑？  
  **A2**：DIC 粒度是 4 字节，32 位字本身只有 4 字节，不存在「半字=4 字节」的第二起始位置，故只支持 lane 0 起。

## 5. 综合实践

把 RX 与 TX 背靠背连成一个「线路环回」：`axis_xgmii_tx_64` 的 `xgmii_txd/txc` 直接连到 `axis_xgmii_rx_64` 的 `xgmii_rxd/rxc`，TX 的 AXI 输入与 RX 的 AXI 输出都接到 cocotb 端点。

任务：

1. 参照 [tb/axis_xgmii_rx_64/test_axis_xgmii_rx_64.py:51-52](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/axis_xgmii_rx_64/test_axis_xgmii_rx_64.py#L51-L52) 的端点接法，增加一个 `XgmiiSink`/`AxiStreamSource` 把 TX 也驱动起来（或直接物理连线）。
2. 用 `AxiStreamSource` 发送若干变长帧（含 < 64 字节的短帧、> 64 字节且末尾非整字的帧）。
3. 在 RX 输出侧断言：① 收到的 `tdata` 与发送载荷逐字节相等（证明 TX 补的前导码/SFD/FCS 被 RX 正确剥离）；② 好帧 `tuser&1==0`；③ 末拍 `tkeep` 与你按 4.3.2 表手算的结果一致。
4. 把 `tb.source.ifg` 在 12 与 0 之间切换（参考 [:74](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/axis_xgmii_rx_64/test_axis_xgmii_rx_64.py#L74)），观察 `ifg=0` 时是否出现 lane 4 起始的帧（`start_packet==2'b10`），并核对 PTP 时间戳的半拍插值（[:96-98](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/axis_xgmii_rx_64/test_axis_xgmii_rx_64.py#L96-L98)）。

这个实践同时串起了本讲的四个最小模块：控制字符、起始终止定界、tkeep/FCS 映射、IFG/DIC 与 lane 交换。

## 6. 本讲小结

- XGMII 用「数据总线 + 逐 lane 控制位」在一拍内同时表达数据与 `IDLE`/`START`/`TERM`/`ERROR` 控制语义。
- 帧由 `START` 与 `TERM` 两个控制字符定界；64 位通路允许 `START` 落在 lane 0 或 lane 4，后者由 RX 做 lane 重对齐、由 DIC 驱动。
- `axis_xgmii_rx_64` 用两拍延时流水线 + 一个移位公式 `tkeep = 0xFF >> (4-t)`（或 `0xFF >> (12-t)`）精确剥离末尾 4 字节 FCS，并用「8 魔数残留法」校验 CRC，前导码/SFD/FCS 均不出现在 AXI 输出。
- `axis_xgmii_tx_64` 是其逆运算：单拍打包前导码+SFD、8 个并行 CRC 引擎处理变宽末拍、追加 `~crc_state` 作 FCS、用 `deficit_idle_count` 实现 DIC。
- 32 位变体因 lane 数减半，起始只支持 lane 0、前导码需两拍、状态机多 `PREAMBLE`/`FCS_3`、CRC 引擎与魔数各减为 4 个。
- 真实仿真在 `tb/axis_xgmii_rx_64/` 下用 cocotb + cocotbext-eth 的 `XgmiiSource`/`XgmiiFrame.from_payload` 驱动，断言 `rx.tdata == payload`、`rx.tuser & 1 == 0`。

## 7. 下一步学习建议

- **[u9-l2 eth_mac_10g](u9-l2-eth-mac-10g.md)**：本讲的两个 XGMII 收发器是 10G MAC 的最底层数据通路，下一讲看 `eth_mac_10g` 如何在它们之上组装出完整 MAC，并重点理解 `ENABLE_DIC` 对帧间间隔的精确补偿。
- **[u10-l1 64b/66b 与 BASE-R](u10-l1-64b66b-baser.md)**：XGMII 之外，10GBASE-R 还要把 XGMII 进一步编码成 64b/66b 经 serdes 传输，那里会再次用到本讲的 XGMII 控制字符映射。
- 继续精读源码：对照 [rtl/axis_xgmii_rx_64.v:196-201](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_rx_64.v#L196-L201)（屏蔽/TERM 检测）与 [rtl/axis_xgmii_tx_64.v:237-296](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_xgmii_tx_64.v#L237-L296)（FCS 拼装），把 8 个 CRC 引擎与 8 个魔数的对应关系彻底走通。
