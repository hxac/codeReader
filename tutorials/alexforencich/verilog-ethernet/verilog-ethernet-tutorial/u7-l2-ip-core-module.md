# ip / ip_64 核心 IP 模块

## 1. 本讲目标

上一讲（u7-l1）我们拆开了 IPv4 收发的「半字节」：`ip_eth_rx` 把以太网载荷里的 IP 头解析成并行字段、`ip_eth_tx` 把并行字段重新封装成 IP 头。但要把 IPv4 真正「跑起来」，只封/解包头还不够——发送一个 IP 包之前，必须先知道对端「MAC 地址是多少」，否则以太网帧根本无处投递。

本讲的目标是把这两块拼成完整的核心模块 `ip`（8 位）/ `ip_64`（64 位），学完后你应当能够：

- 说清 `ip` 顶层如何把 `ip_eth_rx`、`ip_eth_tx` 和一段三状态机布线成一个完整的 IPv4 收发主通路。
- 说清 IP 层为什么、以及如何与 ARP 层协作：发送前查 MAC、未命中如何丢包、ARP 失败如何上报。
- 说清 8 位 `ip` 与 64 位 `ip_64` 在端口与子模块上「只差位宽」的对应关系。

## 2. 前置知识

阅读本讲前，你应已掌握：

- **AXI-Stream 接口约定**（u1-l3）：`tdata/tvalid/tready/tlast/tuser` 的握手语义，以及「并行头 + 流式载荷」的接口风格（`hdr_valid/hdr_ready`）。
- **IPv4 头结构与校验和**（u7-l1）：20 字节定长头、Version/IHL、源/目的 IP、以及头部校验和用的「16 位反码求和」（one's complement sum，重算应得 `0xFFFF`）。
- **ARP 的作用与顶层 `arp` 模块的接口**（u6-l2）：ARP 负责「IP → MAC」的解析，`arp` 模块对外提供 `arp_request(IP) → arp_response(MAC/error)` 的查询接口。

一个核心直觉先建立起来：**以太网靠 MAC 寻址，IP 层靠 IP 寻址，两层地址必须由 ARP 桥接。** 当应用要把一个 IP 包发出去时，IP 层手里只有「目的 IP」，它必须先向 ARP 层发一次查询「这个 IP 的 MAC 是多少？」，拿到 MAC 后才能把以太网帧的目的 MAC 字段填对、把帧送出去。`ip` 模块这段「查 MAC 再发送」的逻辑，就是本讲的重点。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [rtl/ip.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip.v) | 8 位 IPv4 核心顶层：例化 `ip_eth_rx/tx`，加一段「查 ARP → 发包」状态机，是本讲的主角。 |
| [rtl/ip_eth_rx.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_rx.v) | 接收子模块：以太网帧进 → 解析 IP 头 + 校验 → IP 载荷出（u7-l1 已讲，本讲当作黑盒的下层）。 |
| [rtl/ip_eth_tx.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_tx.v) | 发送子模块：并行 IP 头 + 载荷进 → 拼装 IP 头（含算校验和）→ 以太网帧出（u7-l1 已讲，本讲当黑盒）。 |
| [rtl/ip_64.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_64.v) | 64 位 IPv4 核心顶层：与 `ip.v` 几乎逐行对应，仅数据位宽与子模块名不同。 |
| [rtl/ip_complete.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_complete.v) | 上层参考：把 `ip` 与 `arp` 真正接在一起，说明本讲 ARP 接口的「真实另一端」是谁。 |
| [tb/test_ip.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/test_ip.py) | `ip` 模块的仿真：用一张内存表 stub 出 ARP 查询接口，是本讲代码实践的依据。 |

README 对这两个模块的定位也写得很明确：

> `ip` module — IPv4 block with 8 bit data width for gigabit Ethernet. Manages IPv4 packet transmission and reception. Interfaces with ARP module for MAC address lookup.（[README.md:243-246](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L243-L246)）

「Interfaces with ARP module for MAC address lookup」一句话点明了本讲核心。

---

## 4. 核心概念与源码讲解

### 4.1 IP 收发主通路：ip 顶层的布线与子模块层级

#### 4.1.1 概念说明

`ip` 模块本身几乎不含数据处理逻辑，它是一个**布线层（wiring / glue layer）**：把「以太网帧侧」和「IP 侧」用两个现成子模块连起来，外加一段控制「发送前查 ARP」的小状态机。这样设计的好处是职责清晰——封/解 IP 头的细节全在 `ip_eth_rx/tx` 里，`ip` 只负责「调度与拼接」。

模块对外有四组接口，构成两条独立的数据通路：

- **RX（接收）通路**：`s_eth_*`（以太网帧入）→ `ip_eth_rx` → `m_ip_*`（IP 头 + 载荷出）。
- **TX（发送）通路**：`s_ip_*`（IP 头 + 载荷入）→ 状态机先查 ARP → `ip_eth_tx` → `m_eth_*`（以太网帧出）。
- **ARP 接口**：`arp_request_*` / `arp_response_*`，仅 TX 侧使用。
- **配置**：`local_mac`、`local_ip`。

#### 4.1.2 核心流程

两条通路的数据流可以画成：

```
RX 方向（被动，线速）
  以太网帧 ──► ip_eth_rx ──► IP 头(并行) + IP 载荷(AXI)

TX 方向（主动，先查 ARP 再发）
  IP 头 + 载荷 ──► [状态机] ──查 ARP──► ip_eth_tx ──► 以太网帧
                       │
                       └─► arp_request_ip = 目的 IP
                       ◄─ arp_response_mac = 目的 MAC（或 error）
```

关键点：**RX 是纯数据搬运**（不需要查 ARP，收到什么就解什么），而 **TX 在第一拍载荷真正发出之前，必须先完成一次 ARP 查询**。这正是 `ip` 顶层存在状态机的原因——RX 侧没有状态机，TX 侧有。

#### 4.1.3 源码精读

模块端口分四组，注意 RX 与 TX 各有完整的「头 + 载荷」AXI 接口（[rtl/ip.v:34-140](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip.v#L34-L140)）。ARP 接口是 5 对握手信号加一个目的 IP 与一个返回 MAC（[rtl/ip.v:67-76](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip.v#L67-L76)）。

**RX 通路——直接把子模块端口透传出去**（[rtl/ip.v:157-202](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip.v#L157-L202)）：`s_eth_*` 接到 `ip_eth_rx` 的输入，`ip_eth_rx` 的 `m_ip_*` 输出直接就是 `ip` 模块的 `m_ip_*`。注意 `ip_eth_rx` 的 `busy` 与四个 `error_*` 直接对外暴露成 `rx_busy` / `rx_error_*`，没有任何中间逻辑——印证了 RX 是「直通」。

**TX 通路——子模块的输入不是外部信号直连，而是经过寄存器/常量整形**（[rtl/ip.v:204-243](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip.v#L204-L243)）。这是理解 TX 的关键，值得逐条看 `ip_eth_tx` 的输入接的是什么：

| `ip_eth_tx` 输入 | 接到的信号 | 含义 |
| --- | --- | --- |
| `s_ip_hdr_valid` | `outgoing_ip_hdr_valid_reg` | **不是**直接接外部 `s_ip_hdr_valid`，而是状态机「拿到 MAC 之后」才拉高的内部寄存器 |
| `s_eth_dest_mac` | `outgoing_eth_dest_mac_reg` | ARP 返回的目的 MAC，存进寄存器 |
| `s_eth_src_mac` | `local_mac` | 源 MAC 用本机 MAC |
| `s_eth_type` | `16'h0800` | 以太网类型硬编码为 IPv4 |
| `s_ip_identification` | `16'd0` | IP 标识固定 0 |
| `s_ip_flags` | `3'b010` | Flags = `010`，即 **Don't Fragment（不准分片）** |
| `s_ip_fragment_offset` | `13'd0` | 分片偏移固定 0 |
| `s_ip_dscp/ecn/length/ttl/protocol/source_ip/dest_ip` | 外部 `s_ip_*` | 这些字段透传给应用 |

这说明 `ip` 顶层在 TX 侧做了一件重要的事：**应用只需要提供「业务字段」（DSCP、TTL、协议号、源/目的 IP、长度、载荷），而以太网类型、IP 标识、分片标志等「格式字段」由 `ip` 顶层按约定填死。** 其中源 MAC 由 `local_mac` 提供（[rtl/ip.v:212](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip.v#L212)）。

> 小注：`local_ip` 在 `ip.v` 里只声明为输入端口、却**没有**在内部被使用（见 [rtl/ip.v:139](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip.v#L139) 与全文搜索），IP 源地址实际来自应用提供的 `s_ip_source_ip`。`local_ip` 是为上层栈（如 `ip_complete`）预留的配置位。

#### 4.1.4 代码实践（源码阅读型）

**目标**：从端口接线看清「RX 直通、TX 受控」的层级结构。

1. 打开 [rtl/ip.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip.v)。
2. 在 `ip_eth_rx_inst`（L157–L202）中，数一下有几个输入端口接的是 `s_*` 外部信号、有几个输出接 `m_*` 外部信号——应当几乎全部一一对应。
3. 在 `ip_eth_tx_inst`（L204–L243）中，找出 4 个接「常量或内部寄存器」而非外部信号的输入（答案：`s_eth_dest_mac`/`s_eth_src_mac`/`s_eth_type`/`s_ip_hdr_valid`/`s_ip_identification`/`s_ip_flags`/`s_ip_fragment_offset`）。

**需要观察的现象 / 预期结果**：你会确认 RX 通路是纯透传（因此 RX 不需要任何状态机），而 TX 通路的「头有效」与「目的 MAC」被状态机把控——这正是下一节要讲的 ARP 协作的硬件体现。

#### 4.1.5 小练习与答案

**练习 1**：为什么 RX 方向不需要状态机，而 TX 方向需要？

> **参考答案**：RX 是被动的——收到一帧就解一帧，MAC 地址已经在以太网头里，不需要任何「查询」动作；TX 是主动的——手里只有目的 IP，必须先发起一次 ARP 查询拿到目的 MAC，才能开始发送，这个「查询—等待—发包」的时序必须由状态机编排。

**练习 2**：`ip_eth_tx` 的 `s_ip_flags` 被固定为 `3'b010`，对应 IPv4 头里的什么含义？

> **参考答案**：3 位 flags 中最高位保留为 0、中间位 `1` 表示 **DF（Don't Fragment，不允许分片）**、最低位 `0` 表示 MF（More Fragments）为否。即 `ip` 顶层默认发出的是「不分片的完整包」。

---

### 4.2 IP 与 ARP 协作：查 MAC、未命中丢包、ARP 失败上报

#### 4.2.1 概念说明

这一节是 `ip` 模块的「大脑」。当应用把一个 IP 包递给 TX 侧（`s_ip_hdr_valid` 拉高），`ip` 不会立刻把载荷送进 `ip_eth_tx`，而是先进入一个三状态的小状态机：

- **STATE_IDLE**：空闲，等待应用发起新包。
- **STATE_ARP_QUERY**：已经向 ARP 层发出查询，等待 `arp_response_valid`。
- **STATE_WAIT_PACKET**：已经决定发包或丢包，等待整帧载荷传输完成（`tlast`）才回 IDLE。

这套接口与 u6-l2 讲的 `arp` 顶层模块**完全对偶**：`arp` 提供 `arp_request_*`/`arp_response_*`，`ip` 消费它。两者用同样的信号名，在 `ip_complete` 里直接同名线网对接即可。

#### 4.2.2 核心流程

发送一包的三拍状态流：

```
STATE_IDLE
  应用拉高 s_ip_hdr_valid
  ► 向 ARP 发请求：arp_request_valid=1, arp_request_ip = s_ip_dest_ip
  ► 同时声明 arp_response_ready=1，准备收应答
  → STATE_ARP_QUERY

STATE_ARP_QUERY（每拍重查 arp_response_valid）
  收到应答 arp_response_valid=1：
    若 arp_response_error=1（ARP 解析失败）：
      ► s_ip_hdr_ready=1（握手吞掉这个头）
      ► drop_packet=1（整帧载荷读进来后丢弃，不进 ip_eth_tx）
      → STATE_WAIT_PACKET
    否则（拿到 MAC）：
      ► outgoing_eth_dest_mac = arp_response_mac（锁存目的 MAC）
      ► outgoing_ip_hdr_valid=1（真正启动 ip_eth_tx）
      ► s_ip_hdr_ready=1
      → STATE_WAIT_PACKET

STATE_WAIT_PACKET
  等到 s_ip_payload 的 tlast 拍（整帧传完）
  → STATE_IDLE
```

两条关键的硬件细节：

1. **ARP 请求的目的 IP 永远是当前包的目的 IP**：`assign arp_request_ip = s_ip_dest_ip;`（[rtl/ip.v:257](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip.v#L257)）。跨网段时由谁改成网关 IP？由更上层的 `arp` 模块自己判断（见 u6-l2 的「子网/网关判断」），`ip` 只负责把目的 IP 原样丢给 ARP 层。
2. **丢包是「吞掉」而非「反压」**：当 ARP 失败时，应用的载荷已经在路上了，没法回退，所以 `ip` 用 `drop_packet_reg` 把 `s_ip_payload_axis_tready` 强行拉高，把整帧读空丢弃（[rtl/ip.v:254](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip.v#L254)），同时通过 `tx_error_arp_failed` 上报。

#### 4.2.3 源码精读

三个状态常量定义在 [rtl/ip.v:142-145](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip.v#L142-L145)。

**STATE_IDLE → 发起 ARP 请求**（[rtl/ip.v:275-285](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip.v#L275-L285)）：只要 `s_ip_hdr_valid` 为 1，就置 `arp_request_valid_next=1`、`arp_response_ready_next=1`，进入查询态。注意此时**还没有**把包送进 `ip_eth_tx`（`outgoing_ip_hdr_valid` 仍为 0）。

**STATE_ARP_QUERY → 分流「发包」或「丢包」**（[rtl/ip.v:286-306](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip.v#L286-L306)）：这是整段逻辑的「判官」。`arp_response_error` 为真走丢包分支（`drop_packet_next=1`，不启动 `ip_eth_tx`）；为假走发包分支（锁存 `arp_response_mac` 进 `outgoing_eth_dest_mac_next`，置 `outgoing_ip_hdr_valid_next=1` 启动 `ip_eth_tx`）。

**STATE_WAIT_PACKET → 等整帧传完**（[rtl/ip.v:307-316](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip.v#L307-L316)）：判据是 `s_ip_payload_axis_tlast && s_ip_payload_axis_tready && s_ip_payload_axis_tvalid` 三者同时成立——即载荷末拍真正握手成功，才回 IDLE。

**丢包的硬件实现**——一个 `||` 把两条路合并到同一根 `tready`（[rtl/ip.v:254](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip.v#L254)）：

```verilog
assign s_ip_payload_axis_tready = outgoing_ip_payload_axis_tready || drop_packet_reg;
```

正常发包时 `outgoing_ip_payload_axis_tready`（来自 `ip_eth_tx`）有效；丢包时 `drop_packet_reg` 有效，把应用的载荷「读进来扔掉」，避免上游卡死。

**ARP 失败上报**就一行直通（[rtl/ip.v:260](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip.v#L260)）：

```verilog
assign tx_error_arp_failed = arp_response_error;
```

ARP 接口的「真实另一端」是谁？在 `ip_complete` 里，`ip` 的 `arp_request_*`/`arp_response_*` 与 `arp` 模块的对应端口**共享同一组线网**直接对接——`ip_inst` 侧（[rtl/ip_complete.v:371-377](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_complete.v#L371-L377)）和 `arp_inst` 侧（[rtl/ip_complete.v:427-433](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_complete.v#L427-L433)）连的是同名信号。这正是 `ip` 的 ARP 接口设计成「请求/应答」语义的用意：它把「如何真正去解析 MAC」完全外包给了 `arp` 模块。

#### 4.2.4 代码实践（源码阅读 + 行为推断型）

本模块没有现代 cocotb testbench（`tb/` 下没有 `tb/ip/Makefile`），但有完整的仿真脚本 [tb/test_ip.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/test_ip.py)，它**正是用一个内存字典 stub 出 ARP 查询接口**——与本讲规格要求的实践完全吻合。

**实践目标**：读懂这个 ARP stub，并推断三种场景下 `ip` 的行为。

1. **阅读 ARP stub**（[tb/test_ip.py:348-361](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/test_ip.py#L348-L361)）：`arp_table` 是一个 Python 字典（IP→MAC）。`arp_emu` 协程每个时钟上升沿检查 `ip` 是否发来 ARP 请求；若请求的 IP 在表里，就回 `(error=0, mac)`，否则回 `(error=1, 0)`。
2. **追踪三个测试用例**：
   - test 1（[L410-446](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/test_ip.py#L410-L446)）：RX 方向，从以太网侧送入一帧，在 IP 侧收到——不涉及 ARP。
   - test 2（[L449-487](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/test_ip.py#L449-L487)）：TX 方向，目的 IP `0xc0a80165` 已在 `arp_table`（[L407](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/test_ip.py#L407)），发包成功，以太网侧收到帧。
   - test 3（[L490-531](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/test_ip.py#L490-L531)）：TX 方向，目的 IP `0xc0a80166` **不在**表里，断言 `tx_error_arp_failed` 被拉高、以太网侧收不到任何帧。

**需要观察的现象 / 预期结果**：

- test 2 中，`ip` 应当先在 `arp_request_ip` 上送出 `0xc0a80165`，收到 MAC `0xDAD1D2D3D4D5` 后，输出的以太网帧目的 MAC 正是这个值。
- test 3 中，`tx_error_arp_failed` 被置位，`ip` 走 `drop_packet` 分支把载荷吞掉，`eth_sink` 为空。

> 说明：`test_ip.py` 是 **myhdl 时代的历史脚本**（开头 `from myhdl import *`、用 `vvp -m myhdl` 启动，详见 u1-l4 的辨析），无法直接用当前的 cocotb + iverilog 流程 `make` 运行。因此本实践定位为「源码阅读 + 行为推断」。如果你想动手跑，可参照 u13-l2 的方法，为 `ip` 新建一份 cocotb 三件套，把这里的 `arp_table` 字典逻辑改写成 cocotb 协程——ARP 接口的 stub 思路与此处完全一致。运行结果「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果 ARP 一直不返回 `arp_response_valid`，`ip` 会怎样？

> **参考答案**：状态机会一直停在 `STATE_ARP_QUERY`，`s_ip_hdr_ready` 保持 0、`s_ip_payload_axis_tready` 也保持 0（既没发包也没丢包），相当于把上游**反压住**，整条 TX 通路 stall。真正的「超时报错」由对端的 `arp` 模块负责（见 u6-l2 的 `REQUEST_TIMEOUT`）——超时后 `arp` 回 `arp_response_error=1`，`ip` 才进入丢包分支。所以 `ip` 自身没有超时机制，它依赖 ARP 层的 `error` 来解锁。

**练习 2**：丢包分支里为什么必须把 `s_ip_payload_axis_tready` 拉高，而不是直接忽略？

> **参考答案**：AXI-Stream 的反压是「拉低 `tready` 就停」。但应用一旦看到 `s_ip_hdr_ready` 握手成功，就会开始源源不断地送载荷；如果 `ip` 此时不收，载荷会堵在应用侧、整条流水线卡死。所以丢包的本质是「读空并丢弃」——用 `drop_packet_reg` 把 `tready` 拉高，把整帧读完扔掉，才能干净地回到 IDLE。

---

### 4.3 8 位与 64 位数据通路的差异

#### 4.3.1 概念说明

`ip` 与 `ip_64` 是「同一设计的两种位宽」。8 位版本服务千兆以太网（每拍 1 字节），64 位版本服务 10G/25G 以太网（每拍 8 字节）。承接 u1-l1 的命名约定：**`_64` 后缀表示 64 位数据通路，且宽位宽通路特有的 `tkeep` 信号会现身**。

#### 4.3.2 核心流程

把两个文件并排放，差异集中在三处，且**只涉及数据宽度，不涉及控制逻辑**：

1. **AXI 数据位宽**：8 位 `tdata` → 64 位 `tdata`，并新增 8 位 `tkeep`。
2. **子模块名**：`ip_eth_rx`/`ip_eth_tx` → `ip_eth_rx_64`/`ip_eth_tx_64`。
3. **ARP 状态机**：逐行相同，连状态名、判据、寄存器都一样。

换言之，64 位版本是「把 8 位的通路口加宽、把子模块换成宽位宽版」，控制逻辑（含本讲重点的 ARP 协作状态机）原样照搬。

#### 4.3.3 源码精读

**端口加宽 + 出现 `tkeep`**：以太网侧与 IP 侧的载荷 AXI 接口都从 `[7:0] tdata` 变成 `[63:0] tdata` 配 `[7:0] tkeep`（[rtl/ip_64.v:47-52](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_64.v#L47-L52) 与 [rtl/ip_64.v:92-97](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_64.v#L92-L97)）。`tkeep` 的作用与 u1-l3 讲的完全一致：标记最后一拍里哪些字节有效（因为 64 位通路一拍 8 字节，帧尾常常不是完整的 8 字节）。

**子模块换成宽位宽版**：`ip_eth_rx_64_inst`（[rtl/ip_64.v:161](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_64.v#L161)）与 `ip_eth_tx_64_inst`（[rtl/ip_64.v:210](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_64.v#L210)），多接的端口就是 `tkeep`。

**ARP 状态机逐行相同**：把 [rtl/ip_64.v:270-326](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_64.v#L270-L326) 与 [rtl/ip.v:262-318](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip.v#L262-L318) 对照，状态定义、`arp_request_ip = s_ip_dest_ip`、`drop_packet` 合并 `tready`、`tx_error_arp_failed = arp_response_error` 全部一致。

> 题外话：宽位宽下的 IP 头校验和、`tkeep` 边界处理（帧尾跨字）等细节都封装在 `ip_eth_rx_64`/`ip_eth_tx_64` 内部，`ip_64` 顶层并不关心——这正是分层的好处。

#### 4.3.4 代码实践（源码对比型）

**目标**：用工具量化两个文件的差异，验证「只差位宽」。

1. 在仓库根目录执行 `diff rtl/ip.v rtl/ip_64.v`（只读操作，不改源码）。
2. 观察差异行：绝大多数应当是 `8`↔`64` 的位宽数字、`[7:0]`↔`[63:0]` 的声明、`ip_eth_rx`↔`ip_eth_rx_64` 的模块名，以及新增的 `tkeep` 端口与连线。

**需要观察的现象 / 预期结果**：`STATE_IDLE/STATE_ARP_QUERY/STATE_WAIT_PACKET` 三段状态机的逻辑行**不应出现在 diff 中**（即完全相同）。如果你看到状态机里有差异，说明你看错了文件或版本。

#### 4.3.5 小练习与答案

**练习 1**：为什么 64 位通路必须引入 `tkeep`，而 8 位通路不需要？

> **参考答案**：8 位通路一拍正好 1 字节，帧尾必然是整字节，`tlast` 一根线就能标边界。64 位通路一拍 8 字节，帧尾那拍常常只有几个字节有效（例如总长 21 字节的 IP 头 +1 字节载荷，末拍只有 2 字节），必须用 `tkeep` 的每一位标记对应字节是否有效，否则接收方无法知道末拍里哪些是真实数据、哪些是填充。

**练习 2**：如果要把 `ip` 用在一条 32 位通路的链路上，最小改动是什么？

> **参考答案**：仿照 `ip_64` 新建一个 `ip_32`，把数据位宽改成 32 位、`tkeep` 改成 4 位，并例化对应的 `ip_eth_rx_32`/`ip_eth_tx_32`（若存在）；ARP 状态机可以原样复制。控制逻辑与位宽解耦，是这套设计可扩展的关键。

---

## 5. 综合实践

把本讲三块知识串起来，做一个「纸上追踪」任务：

**任务**：假设应用要通过 `ip` 模块发送一个目的 IP 为 `192.168.1.200`（`0xc0a801c8`）、协议为 UDP（`0x11`）、载荷 32 字节的包，而 ARP 表里只有 `192.168.1.101` 的表项。请按顺序回答：

1. **通路定位**：这个包走 RX 还是 TX 通路？应用侧需要提供哪些 `s_ip_*` 字段、哪些字段会被 `ip` 顶层填死？
2. **ARP 协作**：状态机会经历哪几个状态？`arp_request_ip` 上会出现什么值？ARP 层最终会回什么？
3. **结局**：`tx_error_arp_failed` 是否置位？以太网侧 `m_eth_*` 是否有帧输出？`s_ip_payload` 的载荷最终去了哪里？

**参考思路**（请先自己想再对照）：

1. 走 TX 通路。应用需提供 `s_ip_dest_ip=0xc0a801c8`、`s_ip_protocol=0x11`、`s_ip_length=20+32=52`、`s_ip_ttl`、`s_ip_source_ip`、`s_ip_dscp/ecn` 及 32 字节载荷。`ip` 顶层会填死 `s_eth_type=0x0800`、`s_ip_flags=3'b010`、`s_ip_identification=0`、`s_ip_fragment_offset=0`、`s_eth_src_mac=local_mac`，目的 MAC 待 ARP 返回。
2. 状态：`STATE_IDLE` →（`s_ip_hdr_valid` 触发，发 `arp_request_ip=0xc0a801c8`）→ `STATE_ARP_QUERY` →（ARP 查不到该 IP，回 `arp_response_error=1`）→ 走丢包分支 → `STATE_WAIT_PACKET`。
3. `tx_error_arp_failed` 置位；`m_eth_*` **没有**帧输出（因为 `outgoing_ip_hdr_valid` 始终没拉高，`ip_eth_tx` 没被启动）；32 字节载荷被 `drop_packet` 分支「读空丢弃」。

完成这个追踪，说明你已经把「IP 收发主通路 + ARP 协作 + 位宽无关」三件事打通了。

---

## 6. 本讲小结

- `ip`/`ip_64` 是 IPv4 核心的**布线层**：RX 通路直接透传 `ip_eth_rx`，TX 通路在 `ip_eth_tx` 前面加一段控制状态机。
- TX 侧由 `ip` 顶层填死以太网类型 `0x0800`、IP 标识 `0`、分片标志 `DF=1`，源 MAC 用 `local_mac`；应用只提供业务字段。
- 发送前必须先查 ARP：状态机 `IDLE → ARP_QUERY → WAIT_PACKET`，`arp_request_ip` 永远等于当前包的 `s_ip_dest_ip`。
- ARP 失败走**丢包**分支：用 `drop_packet_reg` 把 `tready` 拉高读空载荷，并经 `tx_error_arp_failed` 上报；`ip` 自身不超时，依赖 ARP 层的 `error` 解锁。
- `ip` 的 ARP 接口与 `arp` 模块对偶，在 `ip_complete` 中同名线网直接对接，把「如何解析 MAC」外包给 ARP 层。
- `ip_64` 与 `ip` **只差位宽**：数据加宽到 64 位、新增 `tkeep`、子模块换成 `_64` 版，ARP 状态机逐行相同。

---

## 7. 下一步学习建议

本讲把 `ip` 核心模块讲透了，但它的 ARP 接口还「悬空」着——真正的 ARP 解析、缓存、重试都在 `arp` 模块（u6-l2）里。下一讲 **u7-l3「ip_complete：顶层 IPv4 协议栈」** 会把 `ip` + `arp` + `eth_arb_mux` 组装成开箱即用的 `ip_complete`，你会看到本讲的 `arp_request_*/arp_response_*` 是如何被接到真实的 `arp` 实例上的，以及本地 IP、网关、子网掩码是如何配置的。

如果想提前加深理解，建议阅读：
- [rtl/ip_complete.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_complete.v)：看 `ip_inst` 与 `arp_inst` 如何共享 ARP 线网。
- [tb/test_ip_complete.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/test_ip_complete.py)：看集成后的端到端测试如何驱动 ARP 与 IP。
