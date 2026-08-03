# PTP 时间戳标记与 MAC 集成

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清「把时间戳塞进 AXI-Stream 的 `tuser` 旁带」这件事的物理含义：哪一位是坏帧、哪些位是时间戳、哪些位是 tag。
- 解释为什么 **RX 时间戳走带内 `tuser`**，而 **TX 时间戳却要走旁带总线**——这是由发送时刻与 AXI 输入结束时刻的先后关系决定的。
- 读懂 `ptp_ts_extract`（从 `tuser` 取出时间戳）与 `ptp_tag_insert`（向 `tuser` 注入 tag）这两个极简工具模块的全部源码。
- 掌握 `eth_mac_1g` 在 `PTP_TS_ENABLE` 打开后如何把上一讲 `ptp_clock` 产生的 `output_ts` 旁路到帧上，以及 `PTP_TS_FMT_TOD`、`TX_PTP_TAG_ENABLE`、`TX_PTP_TS_CTRL_IN_TUSER` 三个关键参数的作用。
- 会用仓库自带的 `tb/eth_mac_1g` 仿真（已默认开启 PTP）观察一发一收两个方向的时间戳，并验证 `ptp_ts_extract` 的 `tuser >> 1` 行为。

## 2. 前置知识

本讲是 PTP 子系统的第三讲，承接两段已建立的认知，不再重复：

- **u11-l1（ptp_clock）**：`ptp_clock` 每拍自行走时间，同时输出 **96 位 ToD**（秒+纳秒+16 位小数纳秒 fns）与 **64 位相对**两种时间戳。请记住「低 16 位是 fns」这一点——本讲里你会反复看到 `tuser / 2**16` 把时间戳换算回纳秒。
- **u4-l3（eth_mac_1g）**：`eth_mac_1g` 是一个「布线层」，例化 `axis_gmii_rx`/`axis_gmii_tx`；接收方向无 `tready`、线速不可反压；PTP 时间戳分两路——**RX 时间戳在 SFD 处锁存、帧尾搭车进 `tuser` 高位（带内）**，**TX 时间戳走旁带总线 `tx_axis_ptp_ts` 连同 `tag` 异步回送**。本讲就是把这两条「伏笔」彻底展开。
- **u1-l3（AXI-Stream）**：`tuser` 是「每拍都有的旁带」，但约定里它**只在 `tlast` 拍（坏帧位）或帧首拍（时间戳）有意义**。本讲大量依赖这一约定。

一个直觉性的问题先放在脑子里：PTP 协议要把报文的**真实收发时刻**精确到纳秒级。但 MAC 的数据通路只搬运字节，并不天然携带「这一拍是几点几分」。解决办法就是**借用 `tuser` 这根本来就存在的旁带线**，把时间戳「搭车」在帧上送出去——这就是本讲的全部核心。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
|------|------|----------|
| [rtl/ptp_ts_extract.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_ts_extract.v) | 从 AXI-Stream `tuser` 旁带里取出时间戳 | **精读**：RX 侧取时间戳的工具 |
| [rtl/ptp_tag_insert.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_tag_insert.v) | 把一个 tag 注入到 AXI-Stream `tuser` 旁带 | **精读**：TX 侧打标记的工具 |
| [rtl/axis_gmii_rx.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v) | GMII→AXI 接收翻译器 | **精读 PTP 段**：RX 时间戳在哪里被锁存、怎么拼进 `tuser` |
| [rtl/axis_gmii_tx.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_tx.v) | AXI→GMII 发送翻译器 | **精读 PTP 段**：TX 时间戳与 tag 如何走旁带回送 |
| [rtl/eth_mac_1g.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v) | 千兆 MAC 顶层布线层 | **精读 PTP 段**：参数如何下传、子模块如何接线 |
| tb/eth_mac_1g/Makefile | 仿真参数（默认已开 PTP） | 综合实践的运行入口 |
| tb/eth_mac_1g/test_eth_mac_1g.py | cocotb 测试 | 综合实践的行为参照 |

## 4. 核心概念与源码讲解

### 4.1 tuser 时间戳提取（ptp_ts_extract）

#### 4.1.1 概念说明

接收方向的时间戳由 `axis_gmii_rx` 在帧起始（SFD）处锁存，然后**塞进 AXI 输出帧 `tuser` 的高位**，伴随整帧输出。问题来了：下游应用拿到这帧 AXI 数据时，怎么把时间戳「摘」出来？

`ptp_ts_extract` 就是干这件事的极简工具。它的全部逻辑只有两点：

1. 把 `tuser` **右移 1 位**，丢掉最低位的「坏帧标志」，剩下的就是纯时间戳。
2. 只在**每帧的第一拍**输出一个 `valid` 脉冲——因为时间戳对整帧只有一份，没必要每拍都报。

`tuser` 的位布局（`PTP_TS_ENABLE=1` 时）如下：

| 位段 | 含义 | 来源 |
|------|------|------|
| bit 0 | 坏帧标志（1=坏帧） | `axis_gmii_rx` 的 `m_axis_tuser_reg` |
| bit [PTP_TS_WIDTH : 1] | RX 时间戳（96 或 64 位） | `axis_gmii_rx` 锁存的 `ptp_ts_reg` |

`TS_OFFSET=1` 正好对应「最低 1 位是坏帧标志」这一约定。

#### 4.1.2 核心流程

```
对每个进入的 AXI beat：
    若 tvalid：
        frame_reg <= !tlast          # 不是末拍 → 置 1（表示"帧内"）
                                   # 是末拍  → 清 0（为下一帧的首拍做准备）

输出（组合逻辑，本拍即时）：
    m_axis_ts       = tuser >> TS_OFFSET     # 丢掉坏帧位，取时间戳
    m_axis_ts_valid = tvalid && !frame_reg   # 仅"帧首拍"有效
```

关键时序细节：`frame_reg` 在**首拍时仍是 0**（它要到这个时钟沿之后才被赋成 `!tlast`），所以 `!frame_reg` 在首拍为 1、此后整帧为 0，直到 `tlast` 那拍把它清回 0，迎接下一帧。于是 `m_axis_ts_valid` 每帧**精确地脉冲一次**——正好位于帧首拍。

对于「单拍帧」（`tvalid` 与 `tlast` 同拍），首拍即末拍：该拍 `!frame_reg` 仍为 1（用的是旧值 0），所以依然正确地输出一次有效。

#### 4.1.3 源码精读

模块只有三个参数，核心是把时间戳宽度与偏移参数化：

[rtl/ptp_ts_extract.v:34-56](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_ts_extract.v#L34-L56) —— 模块声明。`TS_WIDTH` 默认 96（对应 ToD 格式），`TS_OFFSET` 默认 1（坏帧位占 1 位），`USER_WIDTH = TS_WIDTH+TS_OFFSET` 自动跟随。

真正干活的只有两行组合逻辑 + 一个寄存器：

[rtl/ptp_ts_extract.v:60-61](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_ts_extract.v#L60-L61) —— 右移取时间戳、首拍有效。`s_axis_tuser >> TS_OFFSET` 把坏帧位丢掉，`!frame_reg` 保证只在帧首拍拉高 `valid`。

[rtl/ptp_ts_extract.v:63-71](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_ts_extract.v#L63-L71) —— `frame_reg` 状态机。`tvalid` 期间 `frame_reg <= !tlast`，复位清 0。

> 旁证：仓库自带的 cocotb 测试 `tb/eth_mac_1g/test_eth_mac_1g.py` 在接收侧正是用 `ptp_ts = rx_frame.tuser >> 1` 取时间戳——这与本模块的 `>> TS_OFFSET` 完全等价。换句话说，`ptp_ts_extract` 就是把测试里那一行 Python 翻译成了可综合硬件。

#### 4.1.4 代码实践

1. **实践目标**：确认 `ptp_ts_extract` 的「右移 + 首拍有效」行为。
2. **操作步骤**：
   - 打开 [rtl/ptp_ts_extract.v:58-71](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_ts_extract.v#L58-L71)，在脑中（或临时 testbench 里）构造一帧 3 拍的 AXI 输入：`tuser` 低位设一个已知坏帧位、高位设一个已知时间戳值，三拍的 `tlast` 序列为 `0,0,1`。
   - 逐拍推断 `frame_reg` 与 `m_axis_ts_valid`：第 1 拍 `frame_reg=0`→`valid=1`；第 2、3 拍 `frame_reg=1`→`valid=0`。
3. **需要观察的现象**：`m_axis_ts` 全程等于 `tuser>>1`（不随拍变化，因为整帧时间戳不变）；`m_axis_ts_valid` 仅在第 1 拍为 1。
4. **预期结果**：3 拍输入只产出 1 个有效时间戳，值等于设定值右移 1 位。
5. 若不实际仿真，可标注「待本地验证」——但上述推断可直接从源码得出，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `TS_OFFSET` 改成 0，`ptp_ts_extract` 会怎样？
**答**：`m_axis_ts = tuser >> 0 = tuser`，时间戳里会**混入最低位的坏帧标志**，应用必须自己再屏蔽一次，否则坏帧时时间戳最低位会被污染。所以 `TS_OFFSET=1` 是为了「免费」丢掉坏帧位。

**练习 2**：为什么 `m_axis_ts_valid` 不直接用 `tlast`（在帧尾报一次），而要在帧首报？
**答**：因为时间戳是**帧首 SFD 时刻**锁存的，代表「帧到达时刻」，语义上属于帧的起点；而且很多下游处理（如 PTP 报文解析）在拿到帧头时就想立刻知道时间戳，等到帧尾再报会增大延迟。

---

### 4.2 TX tag 注入（ptp_tag_insert）

#### 4.2.1 概念说明

发送方向遇到一个本质难题：**TX 时间戳在帧真正发出去那一刻（SFD 上线路）才确定**，可那时 AXI 输入早就结束了，应用已经「撒手」。等 MAC 事后把时间戳从旁带总线送回来时，应用怎么知道这个时间戳属于哪一帧？

解决办法是 **tag（标签）关联**：

1. 应用在发送前给每帧分配一个唯一 tag（如序号），用 `ptp_tag_insert` 把它塞进帧的 `tuser`。
2. MAC 在帧首发出去时，**同时**捕获线路时间戳与 `tuser` 里的 tag，把它们**一起**从旁带总线送回。
3. 应用收到 `(tag, timestamp)` 配对，按 tag 回查「这是我发的第 N 帧」，从而把帧与它的真实发送时刻对应起来。

`ptp_tag_insert` 就是第 1 步的工具：它把一个 tag 写进每帧 `tuser` 的指定位段，并保证「一帧只吃一个 tag」。

#### 4.2.2 核心流程

```
状态：tag_valid_reg（当前是否已持有待用 tag）

每拍：
    若尚未持有 tag（!tag_valid_reg）：
        锁存 s_axis_tag → tag_reg
        tag_valid_reg <= s_axis_tag_valid
    若已持有 tag：
        # 阻塞数据流，直到 tag 就位
        s_axis_tready = m_axis_tready && tag_valid_reg
        # 当本帧末拍被下游消费时，释放 tag，准备接受下一个
        if (tvalid && tready && tlast)  tag_valid_reg <= 0

输出 tuser（组合）：
    user = s_axis_tuser                       # 先拷贝原 tuser
    user[TAG_OFFSET +: TAG_WIDTH] = tag_reg   # 把 tag 覆盖进指定位段
```

两个要点：

- **反压耦合**：`s_axis_tready = m_axis_tready && tag_valid_reg`。若还没拿到 tag，整个 AXI 流被顶住不让过——保证发出的每一帧都带着 tag，绝不漏帧。
- **整帧只注入一次**：`tag_valid_reg` 在帧末拍（`tlast` 被消费）才清零，所以同一帧的所有拍都会被打上同一个 tag（下游通常只读帧首拍，故等价于「一帧一 tag」）。

#### 4.2.3 源码精读

参数与端口：

[rtl/ptp_tag_insert.v:34-72](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_tag_insert.v#L34-L72) —— `TAG_WIDTH` 默认 16，`TAG_OFFSET` 默认 1（跳过最低位的坏帧位）。注意它**同时有 `tready`/`tkeep`**，是个标准带反压的 64 位通路模块（`DATA_WIDTH` 默认 64），与无反压的 `ptp_ts_extract` 不同。

反压与 tag 就位的握手：

[rtl/ptp_tag_insert.v:79-87](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_tag_insert.v#L79-L87) —— `s_axis_tready = m_axis_tready && tag_valid_reg`，`s_axis_tag_ready = !tag_valid_reg`。即「没 tag 就顶住数据」「有 tag 就不再收新 tag」，互斥。

把 tag 写进 tuser 指定位段：

[rtl/ptp_tag_insert.v:89-92](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_tag_insert.v#L89-L92) —— `user[TAG_OFFSET +: TAG_WIDTH] = tag_reg`。`+:` 是 Verilog 的「从某位起、连续 N 位」位选语法，这里就是从 bit 1 起覆盖 16 位。

tag 的加载与释放：

[rtl/ptp_tag_insert.v:94-107](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_tag_insert.v#L94-L107) —— 没持有时锁存输入 tag；持有时只在「本帧末拍被消费」时清零 `tag_valid_reg`。

#### 4.2.4 代码实践

1. **实践目标**：理解「一帧一 tag」与反压耦合。
2. **操作步骤**：阅读 [rtl/ptp_tag_insert.v:79-107](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_tag_insert.v#L79-L107)。设想「tag 输入端迟迟不给 tag」的场景：因为 `s_axis_tready` 里有 `tag_valid_reg` 这一项，AXI 数据帧会被卡在入口不能进入 MAC，直到 tag 到来。
3. **需要观察的现象**：tag 一旦就位，整帧立刻放行；帧末拍 `tlast` 后 `tag_valid_reg` 清零，下一个 tag 才能被接收。
4. **预期结果**：连续发 3 帧时，3 个 tag 与 3 帧严格一一对应，不会错位。
5. 标注「待本地验证」亦可——上述行为可直接从源码的 `if/else` 分支读出。

#### 4.2.5 小练习与答案

**练习 1**：`TAG_OFFSET` 为什么默认是 1 而不是 0？
**答**：bit 0 是坏帧标志位（全库约定）。若 tag 从 bit 0 起写，会覆盖坏帧位，下游就无法再用 bit 0 判坏帧了。`TAG_OFFSET=1` 让 tag 跳过坏帧位。

**练习 2**：如果应用连续发的两帧用了相同的 tag，系统会出错吗？
**答**：不会「出错」，但会**失去区分能力**——TX 时间戳回送时，应用无法判断某个时间戳属于这两帧中的哪一帧。tag 的唯一性由应用保证，硬件不做去重。

---

### 4.3 MAC PTP 旁路（eth_mac_1g 的时间戳通路）

#### 4.3.1 概念说明

`ptp_ts_extract` 与 `ptp_tag_insert` 只是「用户侧工具」，真正产生 / 消费时间戳的是 MAC 内部的 `axis_gmii_rx` / `axis_gmii_tx`。本模块就把上一讲 `ptp_clock` 的 `output_ts`（即 MAC 端口 `tx_ptp_ts` / `rx_ptp_ts`）接到这两个翻译器上，完成「旁路」。

两条通路的不对称是本节最重要的结论，先记结论再看源码：

| 方向 | 时间戳去哪 | 为什么 |
|------|-----------|--------|
| **RX**（接收） | 带内 `tuser` 高位，随帧流出 | 帧到达时下游还在收，`tuser` 此时是「空闲旁带」，可搭车 |
| **TX**（发送） | 旁带总线 `tx_axis_ptp_ts` + `tag`，异步回送 | 时间戳在帧**发完**那一刻才确定，可此时 AXI 输入**早已结束**，没有 `tuser` 可搭，只能走单独总线 |

#### 4.3.2 核心流程

**RX 方向**（`axis_gmii_rx` 内）：

```
1. 在 GMII 输入上检测到 SFD（帧首定界符）→ 置 start_packet_int_reg
2. 下一拍：ptp_ts_reg <= ptp_ts            # 锁存此刻的 PTP 时间
3. 输出：m_axis_tuser = {ptp_ts_reg, bad_frame_flag}
                       # 高位=时间戳，bit0=坏帧
4. 下游用 ptp_ts_extract 取出
```

**TX 方向**（`axis_gmii_tx` 内）：

```
1. 在 GMII 输出上即将发 SFD（帧首）→ start_packet_reg 脉冲
2. 同一拍：
       m_axis_ptp_ts       <= ptp_ts              # 锁存线路发送时刻
       m_axis_ptp_ts_tag   <= s_axis_tuser 里的 tag   # 把应用注入的 tag 取出
       m_axis_ptp_ts_valid <= 1 (或由 tuser 控制位决定)
3. (tag, ts) 作为单拍脉冲从旁带总线送回，应用按 tag 匹配
```

这里有个精巧的参数 **`TX_PTP_TS_CTRL_IN_TUSER`**，它决定「是不是每一帧都要打时间戳」：

- `=0`（默认）：**每一帧都打**，`valid` 恒为 1。简单，适合所有帧都需要时间戳的场合。
- `=1`：由应用通过 `tuser` 的一个控制位**逐帧请求**——只有 PTP 报文才需要纳秒级时间戳，普通数据帧不必浪费这条通路。此时 `tuser` 多占 1 位。

`tuser` 在 TX 侧的位布局因此有两种：

| `TX_PTP_TS_CTRL_IN_TUSER` | bit 0 | bit 1 | bit [TAG_WIDTH+1 : 2] 或 [TAG_WIDTH : 1] |
|---|---|---|---|
| 0（默认） | 坏帧 | tag[0] | tag 高位（tag 从 bit 1 起，共 16 位） |
| 1 | 坏帧 | **时间戳请求** | tag（从 bit 2 起） |

#### 4.3.3 源码精读

**先看 eth_mac_1g 的 PTP 参数与端口**：

[rtl/eth_mac_1g.v:39-49](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L39-L49) —— PTP 相关参数。要点：
- `PTP_TS_ENABLE = 0`（默认关，整个 PTP 旁路不综合）。
- `PTP_TS_FMT_TOD = 1`：`1`→96 位 ToD 格式，`0`→64 位相对格式；`PTP_TS_WIDTH` 由它派生。
- `TX_PTP_TAG_ENABLE = PTP_TS_ENABLE`：开了时间戳就默认开 tag。
- `TX_USER_WIDTH` / `RX_USER_WIDTH` 随 PTP 开关与 tag 宽度自动膨胀——这正是 u1-l3 讲过的「`tuser` 位宽随 `PTP_TS_ENABLE` 自动膨胀」。

[rtl/eth_mac_1g.v:86-90](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L86-L90) —— PTP 端口。注意 `tx_ptp_ts`/`rx_ptp_ts` 是**输入**（由外部 `ptp_clock` 喂入），`tx_axis_ptp_ts`/`_tag`/`_valid` 是**输出**（TX 时间戳旁带回送）。

**RX 时间戳的锁存与拼接（axis_gmii_rx）**：

[rtl/axis_gmii_rx.v:39](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v#L39) —— `USER_WIDTH = (PTP_TS_ENABLE ? PTP_TS_WIDTH : 0) + 1`，那 `+1` 就是坏帧位。

[rtl/axis_gmii_rx.v:301-304](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v#L301-L304) —— 1G 模式下检测到 SFD 时置 `start_packet_int_reg`（MII 模式见 [L269-272](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v#L269-L272)，原理相同）。

[rtl/axis_gmii_rx.v:258-261](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v#L258-L261) —— **下一拍**把 `ptp_ts` 锁进 `ptp_ts_reg`。注意是 `start_packet_int_reg` 置位后的下一个时钟沿才采样，所以时间戳对应 SFD 之后约 1 个 `rx_clk` 周期（125 MHz 下即 8 ns）的时刻。

[rtl/axis_gmii_rx.v:146](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v#L146) —— `m_axis_tuser = PTP_TS_ENABLE ? {ptp_ts_reg, m_axis_tuser_reg} : m_axis_tuser_reg`。开了 PTP 就把时间戳拼到高位，否则只输出坏帧位。这就是「带内 `tuser`」的实现。

**TX 时间戳的旁带回送（axis_gmii_tx）**：

[rtl/axis_gmii_tx.v:200-209](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_tx.v#L200-L209) —— 帧首（`start_packet_reg`）时，一次性锁存三样东西：线路时间戳 `ptp_ts`、从 `tuser` 取出的 tag、以及 valid。注意两种取法：
- `PTP_TS_CTRL_IN_TUSER=1`：`tag = tuser >> 2`、`valid = tuser[1]`（应用逐帧请求）。
- `=0`：`tag = tuser >> 1`、`valid = 1`（每帧都打）。

[rtl/axis_gmii_tx.v:155-157](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_tx.v#L155-L157) —— 把锁存的值送到端口；`PTP_TS_ENABLE` 关时这些输出恒 0，零面积。

**eth_mac_1g 把它们接起来**：

[rtl/eth_mac_1g.v:205-228](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L205-L228) —— 例化 `axis_gmii_rx`，把外部 `rx_ptp_ts` 接到它的 `ptp_ts`（[L221](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L221)），`RX_USER_WIDTH` 决定 `tuser` 宽度。

[rtl/eth_mac_1g.v:230-262](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L230-L262) —— 例化 `axis_gmii_tx`，把 `tx_ptp_ts` 接入（[L252](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L252)），把它的旁带输出 `m_axis_ptp_ts/_tag/_valid` 引到 MAC 顶层端口 `tx_axis_ptp_ts*`（[L253-255](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L253-L255)）。注意它向子模块传的 `PTP_TS_CTRL_IN_TUSER` 用了一个条件：`MAC_CTRL_ENABLE ? PTP_TS_ENABLE : TX_PTP_TS_CTRL_IN_TUSER`——当开了 PAUSE/PFC 流控时，中间隔着 `mac_ctrl_tx`，需要强制每帧打时间戳，原因见下一处。

[rtl/eth_mac_1g.v:340-344](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L340-L344) —— 当 `MAC_CTRL_ENABLE`（即开了流控）且应用没显式用 `TX_PTP_TS_CTRL_IN_TUSER` 时，把进入 `mac_ctrl_tx` 的 `tuser` 的控制位**强制写 1**：`{tx_axis_tuser[hi:1], 1'b1, tx_axis_tuser[0]}`。因为 `mac_ctrl_tx` 不懂这个 PTP 控制位，若不强制，时间戳请求会被吞掉。这是「布线层」为兼容流控子模块做的细节修补。

> 10G 版 `eth_mac_10g` 的 PTP 参数与端口与 `eth_mac_1g` **完全同名同义**（见 [rtl/eth_mac_10g.v:42-49](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_10g.v#L42-L49) 与 [L86-93](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_10g.v#L86-L93)），只是底层翻译器换成 `axis_xgmii_rx/tx_64`、时间戳在 `START` 控制字符处锁存、lane swap 时做半拍补偿。本节结论对两者都成立。

#### 4.3.4 代码实践

1. **实践目标**：用仓库自带仿真观察一发一收两个方向的时间戳，并验证 `ptp_ts_extract` 的行为。
2. **操作步骤**：
   - 确认已装好 cocotb + iverilog（见 u1-l4）。
   - 进入 `tb/eth_mac_1g`，打开 `Makefile`，确认它已经默认开了 PTP：`PARAM_PTP_TS_ENABLE := 1`、`PARAM_PTP_TS_FMT_TOD := 1`、`PARAM_TX_PTP_TAG_ENABLE := 1`、`PARAM_TX_PTP_TS_CTRL_IN_TUSER := 1`、`PARAM_PFC_ENABLE := 1`（见 [tb/eth_mac_1g/Makefile:42-54](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/Makefile#L42-L54)）。
   - 运行：
     ```bash
     cd tb/eth_mac_1g
     make
     ```
3. **需要观察的现象**：仿真日志里会打印每个帧的 `RX frame PTP TS` 与 `TX frame PTP TS`（单位 ns，已除以 \(2^{16}\)）。对应源码在 [tb/eth_mac_1g/test_eth_mac_1g.py:212-213](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L212-L213)（RX：`ptp_ts = rx_frame.tuser >> 1`）与 [tb/eth_mac_1g/test_eth_mac_1g.py:254-256](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L254-L256)（TX：从 `tx_ptp_ts_sink` 收旁带时间戳）。
4. **预期结果**：
   - RX：`ptp_ts_ns` 与 GMII 帧 SFD 的仿真时刻之差约为 \(8\,\text{ns}\)（即 1 个 `rx_clk` 周期，对应 4.3.3 里「下一拍采样」的延迟）。断言见 [test_eth_mac_1g.py:223](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L223)。
   - TX：发送时应用给 `tuser=2`（即 bit1=1，请求时间戳，见 [L250](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L250)），随后从旁带总线收到配对的 `(tag, ts)`。
5. **关键验证**：测试里的 `rx_frame.tuser >> 1` 与 `ptp_ts_extract` 的 `s_axis_tuser >> TS_OFFSET`（`TS_OFFSET=1`）**逐位相同**——这证明该模块就是把测试中的取时间戳操作硬件化。若你把 `ptp_ts_extract` 实例化在 RX AXI 输出后，它会输出与 `rx_frame.tuser >> 1` 完全一致的时间戳。
6. 若本地未配好工具链，上述命令标注「待本地验证」；但源码与断言逻辑可直接核对。

#### 4.3.5 小练习与答案

**练习 1**：为什么 RX 时间戳能搭车在 `tuser` 里，TX 却不能？
**答**：RX 方向，帧到达时下游正在接收，`tuser` 是现成的旁带、正好空闲；TX 方向，时间戳要到帧**发完**（SFD 上线路）才确定，可那时 AXI 输入早已结束、已经没有 `tuser` 可用，所以只能走独立的旁带总线 `tx_axis_ptp_ts` 异步回送。

**练习 2**：`PTP_TS_FMT_TOD` 从 1 改成 0，会改动哪些地方？
**答**：`PTP_TS_WIDTH` 从 96 变 64；`RX_USER_WIDTH` 因此从 97 变 65（时间戳段缩窄）；`ptp_ts_reg` 位宽随之变窄；下游 `ptp_ts_extract` 的 `TS_WIDTH` 也要相应改成 64。时间戳语义从「ToD（秒+纳秒+fns）」变成「相对（纳秒+fns）」。

**练习 3**：开了 PAUSE/PFC 流控时，[eth_mac_1g.v:340-344](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L340-L344) 为什么要把 `tuser` 控制位强制写 1？
**答**：因为数据帧要先穿过 `mac_ctrl_tx` 才到 `axis_gmii_tx`，而 `mac_ctrl_tx` 不认识 `tuser` 里的 PTP 时间戳请求位。若不强制置 1，请求位会被当成普通旁带透传甚至丢失，导致 `axis_gmii_tx` 以为「这帧不需要时间戳」而不回送。强制写 1 保证每帧都被打时间戳。

## 5. 综合实践

把本讲三个最小模块串起来，画一张**完整的 PTP 时间戳数据流图**，并用源码行号标注每个环节。要求覆盖：

1. **TX 链路**：应用生成 tag → `ptp_tag_insert` 注入 `tuser` → `eth_mac_1g`（穿过 `mac_ctrl_tx`，控制位被强制）→ `axis_gmii_tx` 在 SFD 处捕获 `ptp_ts` + tag → 旁带总线 `tx_axis_ptp_ts*` 回送 → 应用按 tag 匹配。
2. **RX 链路**：`axis_gmii_rx` 在 SFD 处锁存 `rx_ptp_ts` → 拼进 `tuser` 高位随帧输出 → `ptp_ts_extract` 右移取出 → 应用得到帧到达时刻。
3. 在图上标出三个关键参数的作用位置：`PTP_TS_FMT_TOD`（决定位宽）、`TX_PTP_TS_CTRL_IN_TUSER`（决定是否逐帧请求）、`TX_PTP_TAG_ENABLE`（决定是否带 tag）。

进阶（可选）：仿照 `tb/eth_mac_1g` 的写法，把 `ptp_ts_extract` 实例化在 `eth_mac_1g` 的 RX AXI 输出之后，断言它的 `m_axis_ts` 等于测试里 `rx_frame.tuser >> 1`、且每帧只脉冲一次 `m_axis_ts_valid`——这将端到端验证本讲的全部结论。

## 6. 本讲小结

- **`tuser` 是 PTP 时间戳的「免费搭车通道」**：bit 0 永远是坏帧标志，时间戳/tag 占据高位，位宽随 `PTP_TS_ENABLE` 自动膨胀。
- **`ptp_ts_extract`**：右移 `TS_OFFSET`（默认 1）丢掉坏帧位取时间戳，靠 `frame_reg` 保证每帧只在首拍输出一次 `valid`。
- **`ptp_tag_insert`**：把 tag 写进 `tuser` 指定位段，靠 `tag_valid_reg` 与 `s_axis_tready` 耦合实现「一帧一 tag、没 tag 就顶住数据流」。
- **RX 走带内、TX 走旁带**：这一不对称源于「发送时间戳在 AXI 输入结束后才确定」，只能用 `tx_axis_ptp_ts*` 异步回送，再用 tag 关联回具体帧。
- **`eth_mac_1g` 是布线层**：把外部 `ptp_clock` 的 `tx_ptp_ts`/`rx_ptp_ts` 接到 `axis_gmii_tx`/`rx`；`PTP_TS_ENABLE=0` 时整个旁路零面积。
- **三个关键参数**：`PTP_TS_FMT_TOD`（96/64 位）、`TX_PTP_TS_CTRL_IN_TUSER`（逐帧请求 vs 每帧都打）、`TX_PTP_TAG_ENABLE`（是否带 tag 关联）；开了流控时控制位被强制置 1。

## 7. 下一步学习建议

- **u11-l4（PTP 时间分发：PHC 与 leaf）**：本讲里 MAC 用到的 `tx_ptp_ts`/`rx_ptp_ts` 来自一个共享的 `ptp_clock`。当 MAC 的 `tx_clk`/`rx_clk` 与 PTP 时钟不同源、或要把时间分发给多片 MAC 时，就要用 `ptp_td_phc`/`ptp_td_leaf` 做串行时间分发——那是下一讲的主题。
- **u11-l5（ptp_perout）**：如果想用 PTP 时间触发一个周期性硬件脉冲（如触发采样），继续读 `ptp_perout`。
- **回到 u4-l3 / u9-l2**：若想对照 10G MAC 的时间戳通路，重读 `eth_mac_10g` 与 `axis_xgmii_rx/tx_64`，体会 lane swap 下的半拍时间戳补偿。
- **建议阅读源码**：`tb/eth_mac_1g/test_eth_mac_1g.py` 是理解本讲行为的最佳参考——它把 `tuser >> 1`、`/ 2**16` 换算、SFD 时刻比对都写成了可运行的断言。
