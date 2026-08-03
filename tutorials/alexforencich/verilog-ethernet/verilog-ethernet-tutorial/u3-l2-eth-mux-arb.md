# 帧复用、解复用与仲裁

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清「多路以太网帧合并成一路」为什么必须按**整帧**切换，而不能按字节（beat）切换；
- 读懂 `eth_mux`（外部选通复用）、`eth_demux`（带 drop 的解复用）、`eth_arb_mux`（自带仲裁的复用）这三个模块的端口与状态机；
- 解释优先级仲裁与轮询（round-robin）仲裁的区别，以及 `arbiter` 模块如何保证「一帧之内不被打断」；
- 独立实例化一个 4 端口、轮询模式的 `eth_arb_mux`，并用仿真验证两路同时到达的帧不会被交错。

## 2. 前置知识

本讲默认你已经掌握 [u1-l3 AXI-Stream 接口约定](u1-l3-axi-stream-interface.md) 中的握手规则（`tvalid`/`tready` 同时为 1 才算一次 transfer、源在握手前必须保持稳定）、`tlast` 划定帧尾、以及 [u3-l1](u3-l1-eth-axis-framing.md) 中介绍的「以太网头部用并行字段 + `hdr_valid`/`hdr_ready` 握手、载荷用 AXI-Stream 流」这一本库通用的接口风格。本讲的三个模块正是建立在这种「头 + 载荷」接口之上，负责把多路这样的帧汇聚或分发。

几个本讲会反复用到的术语：

- **beat**：AXI-Stream 上一拍数据，对应一个时钟周期里 `tvalid&&tready` 的数据。
- **帧级（frame-level）切换**：复用器一旦决定从某一路开始发一帧，就必须把这一帧的所有 beat 发完，才允许切到另一路。
- **仲裁（arbitration）**：当多路同时请求发送时，用一个确定的规则（优先级或轮询）选出当前让哪一路先发。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [rtl/eth_mux.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mux.v) | 以太网帧复用器：N 路入 1 路出，由外部 `enable`+`select` 信号选通，**不含仲裁**。 |
| [rtl/eth_demux.v](https://github.com/alexforencich-verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_demux.v) | 以太网帧解复用器：1 路入 N 路出，由外部 `select` 选目的端口，支持 `drop` 丢弃整帧。 |
| [rtl/eth_arb_mux.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_arb_mux.v) | 带仲裁的复用器：N 路入 1 路出，内部例化 `arbiter`，支持优先级与轮询两种模式。 |
| [lib/axis/rtl/arbiter.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/lib/axis/rtl/arbiter.v) | 通用仲裁器（来自 verilog-axis），`eth_arb_mux` 依赖它，内部用 `priority_encoder`。 |
| [lib/axis/rtl/priority_encoder.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/lib/axis/rtl/priority_encoder.v) | 优先级编码器，`arbiter` 的底层构件。 |
| [rtl/ip_complete.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_complete.v) | 完整 IPv4 栈，**真实使用** `eth_arb_mux` 把 IP 发送与 ARP 发送两路合并为一根以太网输出。 |

## 4. 核心概念与源码讲解

三个模块的端口参数几乎一致（`DATA_WIDTH`、`KEEP_ENABLE`、`ID_ENABLE`、`USER_ENABLE` 等，与 [u3-l1](u3-l1-eth-axis-framing.md) 的成帧模块同源），区别只在「选哪一路」由谁决定：

- `eth_mux`：由**外部** `select` 决定；
- `eth_demux`：方向反过来，由**外部** `select` 选目的端口；
- `eth_arb_mux`：由**内部 arbiter** 在多路同时请求时自动决定。

三者都用一个共同的机制保证「整帧不拆」：用一位 `frame_reg` 记录「当前正在发某一路的帧」，从帧首字节置 1，到 `tlast`（帧尾）才清 0；在 `frame_reg=1` 期间忽略新的选路请求。下面逐个精读。

### 4.1 帧级复用与 eth_mux

#### 4.1.1 概念说明

复用器（multiplexer）要解决的问题很朴素：把多路以太网输出接到同一根物理链路上。例如 CPU 同时有「业务数据」和「ARP 应答」两路要发，但 MAC 只有一个发送口，必须轮流把这两路合并到一根线上。

关键约束是**绝不能按字节交错**。以太网帧是一个不可打断的整体：目的 MAC、源 MAC、类型、载荷、FCS 必须连续。如果复用器这一拍从 A 路发一个字节、下一拍从 B 路发一个字节，下游收到的就是一锅乱码。因此复用必须以**整帧**为最小切换单位——一旦开始发 A 路的帧，就要发到它的 `tlast` 才能切到 B 路。

`eth_mux` 是其中最简单的一种：由外部逻辑给出 `enable` 和 `select`（一个端口号），告诉它「现在轮到第 `select` 路发」。它本身不做任何选择，只是忠实地把选中的一路透明转发，并锁住整帧。

#### 4.1.2 核心流程

`eth_mux` 的核心是一个两段式状态机（用 `frame_reg` 这一位即可表达「空闲 / 帧中」两个状态）：

```text
空闲 (frame_reg==0):
    若 enable && !m_eth_hdr_valid && 第 select 路有 hdr_valid:
        锁存 select -> select_reg
        抓取该路头部 (dest/src mac, type) 到输出
        frame_reg <= 1            // 进入「帧中」
        只给第 select 路 assert hdr_ready
帧中 (frame_reg==1):
    透明转发第 select_reg 路 (即 select_reg 锁存值) 的载荷 beat
    若当前 beat 是 tlast:
        frame_reg <= 0            // 回到空闲，允许下一次选路
```

注意「选路」只在**帧起始那一拍**采样 `select`，之后整帧都用锁存下来的 `select_reg`，所以即便外部在帧中途改了 `select` 也不会生效——这就是「整帧不拆」的实现方式。

载荷通路用一个组合 mux 从打包的多路总线里按 `select_reg` 抽出当前路：

```text
current_s_tdata  = s_eth_payload_axis_tdata[ select_reg*DW +: DW ]
current_s_tvalid = s_eth_payload_axis_tvalid[ select_reg ]
... (tkeep/tlast/tid/tdest/tuser 同理)
```

#### 4.1.3 源码精读

端口声明：注意控制端口 `enable` 和 `select`，以及输入是 `S_COUNT` 路打包总线、输出是单路。[rtl/eth_mux.v:85-90](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mux.v#L85-L90)：

```verilog
    /*
     * Control
     */
    input  wire                          enable,
    input  wire [$clog2(S_COUNT)-1:0]    select
```

帧起始锁存 `select`、抓头部，是整段逻辑的灵魂所在。[rtl/eth_mux.v:156-167](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mux.v#L156-L167)：

```verilog
    if (!frame_reg && enable && !m_eth_hdr_valid && (s_eth_hdr_valid & (1 << select))) begin
        // start of frame, grab select value
        frame_next = 1'b1;
        select_next = select;

        s_eth_hdr_ready_next = (1 << select);

        m_eth_hdr_valid_next = 1'b1;
        m_eth_dest_mac_next = s_eth_dest_mac[select*48 +: 48];
        m_eth_src_mac_next  = s_eth_src_mac[select*48 +: 48];
        m_eth_type_next     = s_eth_type[select*16 +: 16];
    end
```

要点逐条对应前面流程图：`!frame_reg` 保证只在空闲时选路；`(s_eth_hdr_valid & (1<<select))` 确认被选中的那一路确实有帧要发；`s_eth_hdr_ready_next = (1<<select)` 用 one-hot 只回握被选中那一路（其它路即使 hdr_valid 为 1 也得不到 ready，从而被挂起，整帧轮不到它们）。

被选中路的 ready 生成与载荷透传。[rtl/eth_mux.v:170-176](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mux.v#L170-L176)：

```verilog
    // generate ready signal on selected port
    s_eth_payload_axis_tready_next = (m_eth_payload_axis_tready_int_early && frame_next) << select_next;

    // pass through selected packet data
    m_eth_payload_axis_tdata_int  = current_s_tdata;
    ...
    m_eth_payload_axis_tvalid_int = current_s_tvalid && current_s_tready && frame_reg;
```

`m_eth_payload_axis_tready_int_early` 来自模块末尾的输出级（见下方「输出级」），表示内部能否再吞一拍；它与 `frame_next` 相与后左移到 `select_next` 位，于是**只有帧中且被选中的那一路**拿到 `tready`。`tvalid_int` 再用 `frame_reg` 门控，确保帧外不会有杂散数据漏到输出。

> 小提示：README 对 `eth_mux` 的描述里写了「Supports priority and round-robin arbitration」，这是文档复用造成的笔误。`eth_mux` 本身**没有**仲裁，选路完全由外部 `select` 决定；带仲裁的是 `eth_arb_mux`。一切以源码为准。

#### 4.1.4 代码实践

**实践目标**：通过阅读源码与对照波形，确认 `eth_mux`「锁存 `select`」的行为。

**操作步骤**：

1. 打开 [rtl/eth_mux.v:94-95](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mux.v#L94-L95)，确认 `select_reg`、`frame_reg` 是寄存器。
2. 假设外部在端口 0 发一帧期间，把 `select` 从 0 改成 1。
3. 跟踪 [rtl/eth_mux.v:149-154](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mux.v#L149-L154) 的帧尾检测：只有 `current_s_tlast`（即 `select_reg` 锁存值对应那一路的 tlast）出现，`frame_reg` 才清 0。

**需要观察的现象**：`select` 的中途变化只进了 `select` 端口，却进不了 `select_reg`，因此输出始终是原本选中那一路的数据。

**预期结果**：输出帧与端口 0 的输入帧逐字节一致，端口 1 的 `tready` 全程为 0（被挂起）。

**待本地验证**：若你已配置好 cocotb，可在仿真里用 `axis_ep.AXIStreamSource` 驱动两路、在帧中途翻转 `select` 来抓波形确认。

#### 4.1.5 小练习与答案

**练习 1**：如果 `S_COUNT=1`，`eth_mux` 退化成什么？
**答案**：退化为一个带输出缓冲的直通（实质是把单路数据做一次反压解耦），`select` 恒为 0，选路逻辑形同虚设。

**练习 2**：`s_eth_hdr_ready` 为什么用 one-hot `(1<<select)` 而不是给所有路都回握？
**答案**：若同时回握多路，多路都会以为自己的帧被接收而开始送载荷，但输出只有一路，其余路的数据会被丢弃或错位。one-hot 确保只有真正被选中的一路开始传输。

### 4.2 解复用与端口选择：eth_demux

#### 4.2.1 概念说明

解复用器（demultiplexer）是复用器的镜像：一根线进、N 根线出，把到达的每一整帧路由到 `select` 指定的输出端口。典型场景是接收侧按 EtherType 分流——例如 IP 帧（type=0x0800）送 IP 协议栈、ARP 帧（type=0x0806）送 ARP 模块。

`eth_demux` 在选路之外还提供一个实用的 `drop` 输入：置 1 时整帧被「吞掉但不送出」，用于优雅丢弃不需要的帧（既不让上游反压卡死，也不让坏帧污染下游）。

#### 4.2.2 核心流程

```text
空闲 (frame_reg==0):
    若 s_eth_hdr_valid 且本模块给 ready (即 !frame && !m_eth_hdr_valid):
        在握手那一拍锁存 select_ctl 与 drop_ctl
        frame_reg <= 1
        m_eth_hdr_valid[select] <= !drop_ctl   // drop 时不拉 hdr_valid
帧中 (frame_reg==1):
    把载荷数据广播给所有端口，但只在 select_ctl 端口上 assert tvalid
    若 drop_ctl: tready 跟随 (允许吞掉)，但所有端口 tvalid=0
    tlast 出现: frame_reg <= 0, drop_reg <= 0
```

注意一个反直觉的细节：载荷数据（`tdata`/`tkeep`/`tlast`/`tid`/`tdest`/`tuser`）被**广播**到全部 M 个输出端口（同一份值复制 M 份），只有 `tvalid` 和 `hdr_valid` 是 one-hot 的。这样设计是因为硬件上「复制一根总线给所有端口」比「用大 mux 选一个端口」更省逻辑，而下游各端口只在自己 `tvalid=1` 时才采样，对其它端口的广播值视而不见。

#### 4.2.3 源码精读

控制端口：`enable`、`drop`、`select`。[rtl/eth_demux.v:85-91](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_demux.v#L85-L91)：

```verilog
    input  wire                          enable,
    input  wire                          drop,
    input  wire [$clog2(M_COUNT)-1:0]    select
```

`enable` 直接门控输入侧的 ready，使能关闭时整个模块对上游「不 ready」，从而停摆。[rtl/eth_demux.v:119-126](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_demux.v#L119-L126)：

```verilog
assign s_eth_hdr_ready = s_eth_hdr_ready_reg && enable;
assign s_eth_payload_axis_tready = s_eth_payload_axis_tready_reg && enable;

assign m_eth_hdr_valid = m_eth_hdr_valid_reg;
assign m_eth_dest_mac = {M_COUNT{m_eth_dest_mac_reg}};   // 广播
assign m_eth_src_mac  = {M_COUNT{m_eth_src_mac_reg}};    // 广播
assign m_eth_type     = {M_COUNT{m_eth_type_reg}};       // 广播
```

帧起始握手时锁存 `select_ctl`/`drop_ctl`，并用 `(!drop_ctl) << select_ctl` 生成 one-hot 的 `hdr_valid`。[rtl/eth_demux.v:155-171](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_demux.v#L155-L171)：

```verilog
    if (!frame_reg && s_eth_hdr_valid && s_eth_hdr_ready) begin
        // start of frame, grab select value
        select_ctl = select;
        drop_ctl = drop;
        ...
        m_eth_hdr_valid_next = (!drop_ctl) << select_ctl;
        m_eth_dest_mac_next = s_eth_dest_mac;
        ...
    end
```

载荷阶段：`tready` 在 drop 时仍允许上游送（`... || drop_ctl`）以便快速吞帧；`tvalid` 用 one-hot 只点亮选中端口，drop 时全 0。[rtl/eth_demux.v:175-179](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_demux.v#L175-L179)：

```verilog
    s_eth_payload_axis_tready_next = (m_eth_payload_axis_tready_int_early || drop_ctl) && frame_ctl;

    m_eth_payload_axis_tdata_int  = s_eth_payload_axis_tdata;
    ...
    m_eth_payload_axis_tvalid_int = (s_eth_payload_axis_tvalid && s_eth_payload_axis_tready && !drop_ctl) << select_ctl;
```

#### 4.2.4 代码实践

**实践目标**：理解 `drop` 的「吞帧」语义。

**操作步骤**：

1. 阅读 [rtl/eth_demux.v:147-153](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_demux.v#L147-L153) 的帧尾检测。
2. 设想上游持续发来一帧，期间 `drop=1`：`tready` 因 `|| drop_ctl` 一直为 1，上游不会反压；但 `tvalid_int` 因 `!drop_ctl` 而为 0，下游任何端口都收不到这一帧。

**需要观察的现象**：被 drop 的帧「消失」了，但它占用的上游时钟周期被正常消费（上游不卡住），模块在 `tlast` 后干净地回到空闲。

**预期结果**：drop 帧结束后，紧接着的正常帧能被正确路由到 `select` 指定端口。

**待本地验证**：可在仿真里给一帧中途翻转 `drop`，观察下游 `tvalid` 全 0 而上游 `tready` 仍为 1。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `m_eth_payload_axis_tdata` 要 `{M_COUNT{...}}` 广播给所有端口，而不是只接到 `select` 端口？
**答案**：硬件实现上「把同一组寄存器扇出到多个输出端口」比「用 M 选 1 的大 mux 切换」面积更小、时序更好；下游端口靠自己的 `tvalid` 区分，未选中端口对广播值忽略即可。

**练习 2**：`enable=0` 时，模块对外表现如何？
**答案**：`s_eth_hdr_ready` 与 `s_eth_payload_axis_tready` 都被拉 0（[rtl/eth_demux.v:119-121](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_demux.v#L119-L121)），上游被反压挂起，模块整体停摆。

### 4.3 仲裁复用：优先级与轮询（eth_arb_mux + arbiter）

#### 4.3.1 概念说明

`eth_mux` 把「选哪一路」的难题甩给了外部逻辑。但在很多场景里，多路是异步、随机到达的，外部很难提前知道该选谁。`eth_arb_mux` 内部集成了一个仲裁器（arbiter），让模块自己根据各路的请求自动挑一路发送。它仍然遵守「整帧不拆」——仲裁授权（grant）一旦给出，就锁到这一帧的 `tlast` 才允许下一次仲裁。

两种仲裁模式由参数 `ARB_TYPE_ROUND_ROBIN` 选择：

- **优先级仲裁**（`ARB_TYPE_ROUND_ROBIN=0`，默认）：每次都按固定优先级选当前请求中编号最小（或最大，由 `ARB_LSB_HIGH_PRIORITY` 决定）的那一路。优点是简单、低延迟，缺点是高优先级路持续发数据时会「饿死」低优先级路。
- **轮询仲裁**（`ARB_TYPE_ROUND_ROBIN=1`）：每授权完一路，就把它的优先级降到最低，下一轮从「下一个」位置开始扫，保证各路公平、不被饿死。

#### 4.3.2 核心流程

`eth_arb_mux` 把选路工作交给 `arbiter`，自己只负责「拿到 grant 后透明转发」：

```text
request[k]  = s_eth_hdr_valid[k] & ~grant[k]      // k 路有帧要发且当前未被授权
acknowledge = grant & tvalid & tready & tlast      // 当且仅当帧尾那一拍回 ack

arbiter 每个 clk:
    若上一帧未 ack 完 (blocking): 保持当前 grant
    否则在 request 中按优先级/轮询选一路 -> grant (one-hot), grant_encoded

eth_arb_mux:
    tready 只回给 grant_encoded 路:  (内部 ready && grant_valid) << grant_encoded
    若 grant_valid 且输出能接 -> 抓头部, frame_reg <= 1
    透明转发 grant_encoded 路, 直到其 tlast -> frame_reg <= 0, arbiter 收到 ack 释放
```

关键点：`acknowledge` 绑定在 `tlast` 上（[rtl/eth_arb_mux.v:156](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_arb_mux.v#L156)），配合 arbiter 的 `ARB_BLOCK_ACK=1`（blocking），意味着 grant 会被**整帧保持**——这是「不拆帧」的仲裁版实现。

#### 4.3.3 源码精读

仲裁相关参数。[rtl/eth_arb_mux.v:46-49](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_arb_mux.v#L46-L49)：

```verilog
    // select round robin arbitration
    parameter ARB_TYPE_ROUND_ROBIN = 0,
    // LSB priority selection
    parameter ARB_LSB_HIGH_PRIORITY = 1
```

`eth_arb_mux` 不再有外部 `select`，取而代之的是内部与 arbiter 之间的 `request`/`acknowledge`/`grant`。[rtl/eth_arb_mux.v:101-105](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_arb_mux.v#L101-L105)：

```verilog
wire [S_COUNT-1:0] request;
wire [S_COUNT-1:0] acknowledge;
wire [S_COUNT-1:0] grant;
wire               grant_valid;
wire [CL_S_COUNT-1:0] grant_encoded;
```

例化 `arbiter`，并定义 request/acknowledge。[rtl/eth_arb_mux.v:138-156](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_arb_mux.v#L138-L156)：

```verilog
arbiter #(
    .PORTS(S_COUNT),
    .ARB_TYPE_ROUND_ROBIN(ARB_TYPE_ROUND_ROBIN),
    .ARB_BLOCK(1),
    .ARB_BLOCK_ACK(1),
    .ARB_LSB_HIGH_PRIORITY(ARB_LSB_HIGH_PRIORITY)
) arb_inst (...);

assign request     = s_eth_hdr_valid & ~grant;
assign acknowledge = grant & s_eth_payload_axis_tvalid & s_eth_payload_axis_tready & s_eth_payload_axis_tlast;
```

注意 `request = s_eth_hdr_valid & ~grant`：一旦某路拿到 grant，它的 request 立即被屏蔽，避免重复请求；`acknowledge` 只在帧尾（`tlast`）那一拍为真。

被授权路的 `tready` 生成（与 `eth_mux` 思路一致，只是 `select` 换成了 `grant_encoded`）。[rtl/eth_arb_mux.v:120](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_arb_mux.v#L120)：

```verilog
assign s_eth_payload_axis_tready = (m_eth_payload_axis_tready_int_reg && grant_valid) << grant_encoded;
```

帧起始抓头部（条件由 `grant_valid` 驱动）。[rtl/eth_arb_mux.v:175-185](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_arb_mux.v#L175-L185)：

```verilog
    if (!frame_reg && grant_valid && (m_eth_hdr_ready || !m_eth_hdr_valid)) begin
        // start of frame
        frame_next = 1'b1;
        s_eth_hdr_ready_next = grant;
        m_eth_hdr_valid_next = 1'b1;
        m_eth_dest_mac_next = s_eth_dest_mac[grant_encoded*48 +: 48];
        ...
    end
```

再深入一层看 `arbiter` 如何实现轮询。轮询靠一个 `mask_reg`：每授权一路 `request_index`，就把优先级窗口移到「它之后」，下一轮先在被屏蔽后的请求里找。[lib/axis/rtl/arbiter.v:114-134](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/lib/axis/rtl/arbiter.v#L114-L134)：

```verilog
    if (ARB_TYPE_ROUND_ROBIN) begin
        if (masked_request_valid) begin
            grant_next = masked_request_mask;            // 优先照顾被「让位」的请求
            ...
            mask_next = {PORTS{1'b1}} << (masked_request_index + 1);  // 窗口后移
        end else begin
            grant_next = request_mask;                    // 没有被让位的就回到普通优先级
            ...
            mask_next = {PORTS{1'b1}} << (request_index + 1);
        end
    end
```

blocking 行为：未收到 ack 时保持 grant。[lib/axis/rtl/arbiter.v:109-113](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/lib/axis/rtl/arbiter.v#L109-L113)：

```verilog
    end else if (ARB_BLOCK && ARB_BLOCK_ACK && grant_valid && !(grant_reg & acknowledge)) begin
        // granted request not yet acknowledged; hold it
        grant_valid_next = grant_valid_reg;
        grant_next = grant_reg;
        grant_encoded_next = grant_encoded_reg;
```

底层 `priority_encoder` 是一棵二叉压缩树，把 one-hot 的 request 在常数级逻辑深度内编码成端口号（[lib/axis/rtl/priority_encoder.v:56-84](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/lib/axis/rtl/priority_encoder.v#L56-L84)），`LSB_HIGH_PRIORITY` 决定每对里是低位还是高位优先。

**真实工程用法**：`ip_complete` 用 `eth_arb_mux` 把「IP 发送」和「ARP 发送」两路合并成一根以太网输出，优先级模式、ARP（LSB=port 0）优先。[rtl/ip_complete.v:258-285](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_complete.v#L258-L285)：

```verilog
eth_arb_mux #(
    .S_COUNT(2),
    .DATA_WIDTH(8),
    ...
    .ARB_TYPE_ROUND_ROBIN(0),
    .ARB_LSB_HIGH_PRIORITY(1)
) eth_arb_mux_inst (
    ...
    .s_eth_hdr_valid({ip_tx_eth_hdr_valid, arp_tx_eth_hdr_valid}),
    ...
```

注意位拼接 `{ip_tx_..., arp_tx_...}`：`arp_tx_*` 在 LSB（port 0），`ip_tx_*` 在 port 1，配合 `ARB_LSB_HIGH_PRIORITY=1`，于是 ARP 帧优先于数据帧发出——这避免了「正要发数据却发现没有对端 MAC、ARP 还排在数据后面」的死锁。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：实例化一个 4 端口、**轮询模式**的 `eth_arb_mux`，构造两路同时到达的完整帧，验证输出端口不会把两帧的字节交错。

**操作步骤**：

1. 在 `tb/` 下新建目录 `eth_arb_mux/`，仿照 [tb/eth_axis_rx/Makefile](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_axis_rx/Makefile) 写一份 cocotb Makefile。关键是把三个 RTL 都列入源文件、并设置轮询参数：

   ```makefile
   TOPLEVEL_LANG = verilog
   SIM ?= icarus
   DUT      = eth_arb_mux
   TOPLEVEL = $(DUT)
   MODULE   = test_$(DUT)
   VERILOG_SOURCES += ../../rtl/$(DUT).v
   VERILOG_SOURCES += ../../lib/axis/rtl/arbiter.v
   VERILOG_SOURCES += ../../lib/axis/rtl/priority_encoder.v

   export PARAM_S_COUNT := 4
   export PARAM_DATA_WIDTH := 8
   export PARAM_ARB_TYPE_ROUND_ROBIN := 1
   export PARAM_ARB_LSB_HIGH_PRIORITY := 1
   ```

   （`eth_arb_mux` 的 `S_COUNT=4` 时 `CL_S_COUNT=2`，无需 `KEEP_ENABLE`，因 8 位通路默认省略 tkeep。）

2. 写 `test_eth_arb_mux.v`，例化 DUT 并把 `S_COUNT` 路的 `s_eth_*` 信号、单路 `m_eth_*` 信号、`clk`/`rst` 引到 `tb` 顶层（可仿照仓库里历史遗留的 [tb/test_eth_arb_mux_4.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/test_eth_arb_mux_4.v) 的端口列表，但**不要**保留其中的 `$from_myhdl/$to_myhdl`——那是已弃用的 myhdl 接口，cocotb 不需要）。

3. 写 `test_eth_arb_mux.py`，用 `tb/eth_ep.py` 里的 `EthFrameSource` 同时给端口 1、端口 2 各送一帧（`source_list[1].send(f1)`、`source_list[2].send(f2)`），用 `EthFrameSink` 收输出，再 `assert rx_frame == f1` 与 `== f2`。这正是历史用例 [tb/test_eth_arb_mux_4.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/test_eth_arb_mux_4.py) 中 "test 4: back-to-back packets, different ports"（第 287-315 行）覆盖的场景，可直接参考其帧构造与断言写法。

4. 运行 `make`。

**需要观察的现象**：sink 先完整收到一帧（其 `eth_src_mac` 与 f1 或 f2 之一完全一致，逐字节连续），再完整收到另一帧。两帧的 `tdata` 序列在时间上**绝不交错**。

**预期结果**：两个 `assert rx_frame == ...` 都通过。轮询模式下，若两路同时请求，先发的那一路结束后，下一帧会从「另一路」开始（轮询让位），你可以通过把 `ARB_TYPE_ROUND_ROBIN` 改回 `0`、重跑，观察优先级模式下端口编号小的（LSB）总是先发来对比两种模式差异。

**待本地验证**：本仓库 `tb/` 下当前没有 `eth_arb_mux` 目录（这些模块目前主要通过 `tb/test_ip_complete.py` / `tb/test_ip_complete_64.py` 间接回归），上述三件套需自行创建；运行结果以你本地 cocotb + iverilog 环境为准。

#### 4.3.5 小练习与答案

**练习 1**：把 `ip_complete` 里的 `ARB_TYPE_ROUND_ROBIN` 从 0 改成 1，会有什么潜在影响？
**答案**：IP 数据帧与 ARP 帧会轮流发送而非 ARP 恒优先。在「大量数据帧持续发送 + 偶发需要 ARP」的极端场景下，ARP 可能要等一轮，理论上有微弱的时延变化；但因 `request` 只在 `hdr_valid` 有效时拉起、且帧一般很短，实际影响很小。设计者选优先级是为了让 ARP 这种「控制类」帧总能插队。

**练习 2**：为什么 `acknowledge` 必须包含 `tlast`，而不是随便哪一拍都可以 ack？
**答案**：`ARB_BLOCK_ACK=1` 使得 arbiter 在收到 ack 前一直保持 grant。若在帧中间 ack，grant 会立即释放，arbiter 可能马上授权另一路，导致两路字节交错。把 ack 绑定 `tlast` 才能确保「整帧发完才换路」。

**练习 3**：`grant_encoded` 与 `grant`（one-hot）分别用在哪？
**答案**：`grant`（one-hot）用于回握 `s_eth_hdr_ready = grant`（一次只点亮一路）；`grant_encoded`（二进制端口号）用于从打包总线里用 `[grant_encoded*DW +: DW]` 抽取该路数据、以及左移生成 `tready`。

---

### 关于三者共用的输出级（补充）

三个模块末尾都有一段几乎相同的「双寄存器 + temp 缓冲」输出级（例如 [rtl/eth_arb_mux.v:235-273](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_arb_mux.v#L235-L273)）。它的作用是**解耦上游反压与下游反压**：当下游突然 `tready=0` 时，内部用一个 `temp_*_reg` 暂存上游已经送来的一拍，使上游仍能再推一拍而不丢数据；`m_eth_payload_axis_tready_int_early` 就是「输出寄存器与 temp 都空、或下游已 ready」时提前为真，从而让上游获得一拍 lookahead 的 ready。这一模板与 [u2-l2](u2-l2-ethernet-fcs.md) 中 `axis_eth_fcs*` 系列完全同源，理解一处即可贯通全库。

## 5. 综合实践

**任务**：搭建一个「2 路合成、按内容分流」的迷你分发结构，把本讲三个模块串起来。

1. 用一个 `eth_arb_mux`（`S_COUNT=2`，轮询）把两路 `EthFrameSource` 合并成一路。
2. 把合并后的单路输出接到一个 `eth_demux`（`M_COUNT=2`）的输入；在 `eth_demux` 的 `select` 上接一段组合逻辑：**当以太网 `type==0x0806`（ARP）时选端口 0，否则选端口 1**；并让 `type` 既不在 0x0800 也不在 0x0806 时 `drop=1`。
3. 给两个输出端口各接一个 `EthFrameSink`。

**验证**：

- 从两路 source 交替/同时发若干 ARP 帧（type=0x0806）和 IP 帧（type=0x0800），核对端口 0 的 sink 只收到 ARP 帧、端口 1 的 sink 只收到 IP 帧。
- 发一个 type=0x1234 的帧，验证两个 sink 都收不到它（被 drop），但上游没有卡死（后续帧仍能正常路由）。
- 用波形确认：无论两路如何同时到达，`eth_arb_mux` 的输出线上任意一帧的字节都是连续的，没有被另一帧打断。

**提示**：`eth_demux` 的 `select` 在帧起始握手那一拍被锁存（[rtl/eth_demux.v:157-158](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_demux.v#L157-L158)），所以你的 `select` 组合逻辑只要在 `s_eth_hdr_valid && s_eth_hdr_ready` 那一拍给出正确值即可，帧中途变化不影响路由结果——这与 `eth_mux` 锁存 `select` 的道理一致。

> 待本地验证：本综合实践需要自行编写 cocotb testbench（参考第 4.3.4 节的三件套模板），具体运行结果以本地环境为准。

## 6. 本讲小结

- 三个模块都以「整帧不拆」为铁律，靠一位 `frame_reg` 实现：帧首字节置 1、`tlast` 清 0，期间忽略一切换路请求。
- `eth_mux` 是外部选通的复用器（`enable`+`select`），最简单、无仲裁；README 里「支持仲裁」的描述是笔误，以源码为准。
- `eth_demux` 是带 `drop` 的解复用器，载荷广播给所有端口、仅 `hdr_valid`/`tvalid` 为 one-hot；`drop` 能优雅吞帧而不反压上游。
- `eth_arb_mux` 内部例化 `arbiter`，支持优先级（`ARB_TYPE_ROUND_ROBIN=0`）与轮询（`=1`）两种模式；`acknowledge` 绑定 `tlast` + `ARB_BLOCK_ACK=1` 保证整帧保持 grant。
- 轮询靠 `arbiter` 里的 `mask_reg` 每授权一路就把优先级窗口后移，避免低优先级路被饿死。
- `ip_complete` 真实用 `eth_arb_mux(S_COUNT=2)` 合并 IP/ARP 两路、ARP（LSB）优先；三者末尾共用与 `axis_eth_fcs*` 同源的「双寄存器 + temp」输出级。

## 7. 下一步学习建议

- 下一讲进入 **[u4 单元：千兆 MAC](../)** 之前，建议先用本讲的 `eth_demux` 配合 [u3-l1](u3-l1-eth-axis-framing.md) 的 `eth_axis_rx`，动手把一根以太网流按 EtherType 分流——这正是 `ip_complete` 接收侧（[rtl/ip_complete.v:248-253](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_complete.v#L248-L253) 附近）做的事，不过那里用手写逻辑而非 `eth_demux`，对比阅读能加深理解。
- 想直接看仲裁在系统里的作用，可阅读 [rtl/ip_complete.v:255-300](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_complete.v#L255-L300) 的「Output arbiter」段，以及 64 位等价物 [rtl/ip_complete_64.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_complete_64.v) 中对 `eth_arb_mux` 的实例化。
- 若对反压与输出级模板的细节感兴趣，可回看 [u2-l2 axis_eth_fcs](u2-l2-ethernet-fcs.md) 中对该模板的讲解，或直接读 [lib/axis/rtl/axis_fifo.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/lib/axis/rtl/axis_fifo.v)，那将在 [u5 MAC FIFO 集成](../) 正式登场。
