# ARP 帧接收与发送

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 ARP 报文的字段构成（HTYPE/PTYPE/HLEN/PLEN/OPER/SHA/SPA/THA/TPA），并理解请求（OPER=1）与应答（OPER=2）的区别。
- 读懂 `arp_eth_rx` 如何从一段以太网载荷（payload）字节流中逐字节拆出 ARP 字段、校验长度、报告错误。
- 读懂 `arp_eth_tx` 如何把一组并行 ARP 字段重新串行化成 28 字节的以太网载荷。
- 理解请求帧与应答帧的差异其实不在 `arp_eth_tx` 内部，而由上层（`arp.v`）填充不同字段值决定。
- 用 cocotb + scapy 构造一个 ARP 请求帧送入 `arp_eth_rx`，验证解析出的 sender IP/MAC 与 opcode。

## 2. 前置知识

本讲承接 **u3-l1（eth_axis_rx/tx：以太网帧解析与封装）**，需要你先建立以下认知：

- **「并行头 + AXI-Stream 载荷」的接口风格**：本库的协议模块几乎都把一层协议头用并行信号（配 `hdr_valid`/`hdr_ready` 握手）传递，而把该层的载荷用标准 AXI-Stream（`tdata`/`tvalid`/`tready`/`tlast`/`tuser`）流式传递。`eth_axis_rx` 已经把 14 字节的以太网头拆成了 `dest_mac`/`src_mac`/`type` 并行字段 + 一根 payload AXI 流。
- **网络字节序（大端）**：以太网/IP/ARP 在线路上都按大端传输，即多字节字段的高字节先发（占据更小的字节偏移）。
- **ARP 的作用（直觉）**：以太网帧靠 MAC 地址投递，而应用程序只关心 IP 地址。ARP（Address Resolution Protocol）就是「已知 IP，问 MAC」的协议——发出一个广播请求「谁拥有这个 IP？」，拥有者回一个单播应答「是我，我的 MAC 是 X」。这一问一答就是本讲的两类帧。

> 简单记忆：ARP 帧 = 28 字节的固定载荷。请求和应答**结构完全相同**，只靠 OPER 字段区分。请求里 THA（目标 MAC）通常填 0（因为我正想知道它），应答里 THA 则填上真实值。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [rtl/arp_eth_rx.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp_eth_rx.v) | ARP 帧接收器：输入「以太网头（并行）+ 载荷（AXI 流）」，输出解析后的 ARP 各字段（并行）。 |
| [rtl/arp_eth_tx.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp_eth_tx.v) | ARP 帧发送器：输入并行 ARP 字段，输出串行化的 28 字节载荷（AXI 流）。请求与应答共用此模块。 |
| [rtl/arp.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp.v) | 顶层 ARP 模块（下一讲 u6-l2 详解）。本讲引用它，说明「请求 vs 应答」的字段填充差异由它决定。 |
| [tb/arp_eth_rx/test_arp_eth_rx.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/arp_eth_rx/test_arp_eth_rx.py) | `arp_eth_rx` 的 cocotb 测试，含构造 ARP 帧、注入、收结果的完整样板，是本讲代码实践的依据。 |

## 4. 核心概念与源码讲解

### 4.1 ARP 报文解析（arp_eth_rx）

#### 4.1.1 概念说明

`arp_eth_rx` 做的事情很纯粹：把一段 28 字节的 ARP 载荷「翻译」成一组并行的字段。

它的上游是 `eth_axis_rx`（或等价的成帧层）——后者已经把以太网帧的 14 字节头剥掉，分成了并行以太网头（`s_eth_dest_mac`/`s_eth_src_mac`/`s_eth_type`）和载荷 AXI 流。`arp_eth_rx` 接住这两部分，**透传以太网头**，同时把载荷字节逐个映射到 ARP 字段，最后在帧尾一次性输出「合法 ARP 帧」及其全部字段。

需要注意的不对称设计：

- **输入是流，输出是并行**：载荷按 AXI 字节流进来，但解析结果（`m_arp_*`）是并行寄存器，只在帧尾（`tlast`）变「有效」。
- **不校验 EtherType**：`arp_eth_rx` 不关心 `s_eth_type` 是不是 `0x0806`，EtherType 的分发由上层（如 `ip_complete` 里的复用器）负责。它只管「假设这是一段 ARP 载荷，把它拆开」。
- **校验的是长度**：它会检查 HLEN 是否为 6、PLEN 是否为 4（以太网+IPv4 的固定长度），不满足就报 `error_invalid_header`。

#### 4.1.2 核心流程

`arp_eth_rx` 是一个两段式状态机，靠一个字节指针 `ptr_reg` 在载荷里逐拍推进：

```
状态 read_eth_header（默认/空闲）
  └─ 上游握手 s_eth_hdr_valid & s_eth_hdr_ready
       ├─ store_eth_hdr=1：锁存以太网头
       └─ 切到 read_arp_header，ptr=0

状态 read_arp_header（消费 28 字节）
  每收到一个有效字节（tvalid & tready）：
       ├─ ptr += 1
       ├─ 按 ptr 位置把该字节写进对应的 m_arp_*_next 字段
       └─ 若 ptr 到达第 28 字节（偏移 27）：标记头已读完

  帧尾 tlast 拍做最终判定：
       ├─ 若头还没读完          → error_header_early_termination
       ├─ 若 HLEN≠6 或 PLEN≠4    → error_invalid_header
       └─ 否则                   → m_frame_valid = !tuser（好帧才有效）
       最后回到 read_eth_header，准备收下一帧
```

宽位宽（如 64 位）时，一个时钟周期能进来 8 个字节，所以 `ptr_reg` 实际数的是「周期数」而非「字节数」，每个周期内用 `offset % BYTE_LANES` 把 8 个字节各就各位。相关参数：

\[ \text{BYTE\_LANES} = \text{KEEP\_WIDTH} \quad(\text{8 位时}=1,\ \text{64 位时}=8) \]
\[ \text{CYCLE\_COUNT} = \lceil \text{HDR\_SIZE} / \text{BYTE\_LANES} \rceil = \lceil 28 / \text{BYTE\_LANES} \rceil \]

8 位时 CYCLE_COUNT=28（28 拍），64 位时 CYCLE_COUNT=4（4 拍，末拍只有 4 个有效字节）。

#### 4.1.3 源码精读

**接口**——注意它严格遵循「并行以太网头 + AXI 载荷」的接口风格（[rtl/arp_eth_rx.v:L48-L87](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp_eth_rx.v#L48-L87)）：输入侧 `s_eth_*` 是并行以太网头加 `s_eth_payload_axis_*` 载荷流；输出侧 `m_*` 是并行 ARP 字段加透传的以太网头。这段端口定义就是它与 `eth_axis_rx` 无缝对接的「插座」。

**ARP 帧结构注释**——源码头部用一张表清晰列出了 28 字节的字段布局（[rtl/arp_eth_rx.v:L107-L129](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp_eth_rx.v#L107-L129)），下表的「偏移」列就是解析时 `ptr` 走过的字节位置：

| 字节偏移 | 字段 | 长度 | 典型值 | 解析后输出 |
|---|---|---|---|---|
| 0–1 | HTYPE | 2 | 0x0001（以太网） | `m_arp_htype` |
| 2–3 | PTYPE | 2 | 0x0800（IPv4） | `m_arp_ptype` |
| 4 | HLEN | 1 | 6 | `m_arp_hlen` |
| 5 | PLEN | 1 | 4 | `m_arp_plen` |
| 6–7 | OPER | 2 | 1=请求 / 2=应答 | `m_arp_oper` |
| 8–13 | SHA | 6 | 发送方 MAC | `m_arp_sha` |
| 14–17 | SPA | 4 | 发送方 IP | `m_arp_spa` |
| 18–23 | THA | 6 | 目标 MAC | `m_arp_tha` |
| 24–27 | TPA | 4 | 目标 IP | `m_arp_tpa` |

**逐字节提取的宏**——解析的核心是一个在 `always @*` 块内定义的 `` `_HEADER_FIELD_ `` 宏（[rtl/arp_eth_rx.v:L219-L251](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp_eth_rx.v#L219-L251)）。每个字段一行，把「载荷第 `offset` 字节」写进「目标字段的某一字节」：

```verilog
`define _HEADER_FIELD_(offset, field) \
    if (ptr_reg == offset/BYTE_LANES && (!KEEP_ENABLE || s_eth_payload_axis_tkeep[offset%BYTE_LANES])) begin \
        field = s_eth_payload_axis_tdata[(offset%BYTE_LANES)*8 +: 8]; \
    end

`_HEADER_FIELD_(0,  m_arp_htype_next[1*8 +: 8])   // 线上第0字节 → htype高字节
`_HEADER_FIELD_(1,  m_arp_htype_next[0*8 +: 8])   // 线上第1字节 → htype低字节
...
`_HEADER_FIELD_(6,  m_arp_oper_next[1*8 +: 8])    // 线上第6字节 → oper高字节
`_HEADER_FIELD_(7,  m_arp_oper_next[0*8 +: 8])    // 线上第7字节 → oper低字节
```

注意 `[1*8 +: 8]`（高字节）对应**更小的偏移**——这正是网络大端序：线上先到的字节是高位。这一段宏是本模块的「大脑」，所有字段映射都在这里，改协议字段就看这里。

**帧尾的三态判定**——在 `tlast` 拍，模块决定这一帧是合法、还是该报错（[rtl/arp_eth_rx.v:L260-L275](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp_eth_rx.v#L260-L275)）：

```verilog
if (s_eth_payload_axis_tlast) begin
    if (read_arp_header_next) begin
        // 头还没收齐就结束了
        error_header_early_termination_next = 1'b1;
    end else if (m_arp_hlen_next != 4'd6 || m_arp_plen_next != 4'd4) begin
        // 长度字段不是以太网+IPv4
        error_invalid_header_next = 1'b1;
    end else begin
        // 合法帧：把 tuser（坏帧位）取反后作为 m_frame_valid
        m_frame_valid_next = !s_eth_payload_axis_tuser;
    end
    // 无论结果如何，回到读以太网头状态，准备下一帧
end
```

这里的 `m_frame_valid_next = !s_eth_payload_axis_tuser` 很巧妙：上游若在 `tuser` 拉高（标记坏帧，例如 CRC 错），这里就不会产出有效的 ARP 帧——错误帧被悄悄丢弃而不污染 ARP 解析结果。28 字节之后的尾部填充字节（如以太网最小帧长填充）会被继续消费但被忽略，因此「带尾部字节的 ARP 帧」仍能正确解析。

#### 4.1.4 代码实践

**实践目标**：用 cocotb + scapy 构造一个 ARP **请求**帧，送入 `arp_eth_rx`，验证解析出的 sender IP/MAC、opcode 与构造时一致。

**操作步骤**（参照 [tb/arp_eth_rx/test_arp_eth_rx.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/arp_eth_rx/test_arp_eth_rx.py) 的 `TB.send()`/`TB.recv()`）：

1. 安装好 cocotb、cocotbext-axi、cocotbext-eth、iverilog（见 u1-l4）。
2. 进入目录并运行现成测试，确认环境正常：
   ```bash
   cd tb/arp_eth_rx
   make
   ```
3. 在 `test_arp_eth_rx.py` 的 `run_test` 里，把 scapy 构造的 ARP 改成**请求**（`op=1`），其余字段用你指定的值：
   ```python
   from scapy.layers.l2 import Ether, ARP
   eth = Ether(src='5A:51:52:53:54:55', dst='ff:ff:ff:ff:ff:ff')   # 请求是广播
   arp = ARP(hwtype=1, ptype=0x0800, hwlen=6, plen=4, op=1,        # op=1 = 请求
       hwsrc='5A:51:52:53:54:55', psrc='192.168.1.100',             # 发送方
       hwdst='00:00:00:00:00:00', pdst='192.168.1.101')             # 目标 MAC 未知→填0
   test_pkt = eth / arp
   await tb.send(test_pkt)
   rx_pkt = await tb.recv()
   ```
4. 仿照测试末尾的断言，针对解析结果单独检查关键字段（`TB.recv()` 已把 DUT 的 `m_arp_*` 还原成一个 scapy ARP 对象）：
   ```python
   assert rx_pkt[ARP].op == 1
   assert rx_pkt[ARP].hwsrc == '5a:51:52:53:54:55'
   assert rx_pkt[ARP].psrc == '192.168.1.100'
   assert rx_pkt[ARP].pdst == '192.168.1.101'
   ```

**需要观察的现象**：DUT 的 `m_frame_valid` 在帧尾拉高一拍；`m_arp_oper=1`、`m_arp_sha`/`m_arp_spa` 与发送方一致、`m_arp_tha=0`。

**预期结果**：断言全部通过。说明 28 字节载荷被正确拆成了各 ARP 字段，opcode 被识别为请求。

> 如果未实际运行，标注：上述命令与断言的通过情况**待本地验证**。现成测试 `tb/arp_eth_rx/test_arp_eth_rx.py` 默认发送的是 `op=2`（应答）的帧，原理完全相同，可作为对照。

#### 4.1.5 小练习与答案

**练习 1**：如果上游送来的帧只有 20 字节载荷（少于 28）就 `tlast`，`arp_eth_rx` 会怎样？

**答案**：会触发 `error_header_early_termination`（`read_arp_header_next` 仍为 1），且不产出 `m_frame_valid`。参见源码 [rtl/arp_eth_rx.v:L260-L266](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp_eth_rx.v#L260-L266)。对应测试 `tb/test_arp_eth_rx.py` 的 "test 7: truncated packet"。

**练习 2**：为什么 `m_arp_oper` 的高字节对应**偏移 6**、低字节对应**偏移 7**？

**答案**：网络字节序是大端，多字节字段高字节先在线上出现，所以偏移小的是高位。源码用 `m_arp_oper_next[1*8 +: 8]`（高字节）接偏移 6、`[0*8 +: 8]`（低字节）接偏移 7。

---

### 4.2 ARP 请求构造：arp_eth_tx 的序列化机制

#### 4.2.1 概念说明

`arp_eth_tx` 是 `arp_eth_rx` 的逆运算：输入一组并行的 ARP 字段，输出串行的 28 字节载荷 AXI 流。它和 `eth_axis_tx`（u3-l1）一样，负责「把并行头重新打成字节流」。

关键点：**请求和应答在 `arp_eth_tx` 眼里没有区别**。它只是一个通用的序列化器——你喂给它什么 `s_arp_oper`、`s_arp_sha`、`s_arp_spa`……它就把这些值按 ARP 字段布局原样打到线上。所谓「请求构造」其实是：上层把 `s_arp_oper` 设成 1、把 THA 设成 0、TPA 设成目标 IP，然后让 `arp_eth_tx` 把它串行化出去。

另外注意两个「硬编码」：

- **HLEN 和 PLEN 不是输入端口**：`arp_eth_tx` 直接把 HLEN 写死为 6、PLEN 写死为 4（见下文源码），因为这个库只支持「以太网硬件地址 + IPv4 协议地址」这一种组合，没必要做成参数。这也和 `arp_eth_rx` 的校验（HLEN==6, PLEN==4）互为印证。
- **EtherType 由上层填**：`s_eth_type` 是输入端口，但调用方（`arp.v`）会把它固定接到 `16'h0806`。

#### 4.2.2 核心流程

`arp_eth_tx` 的工作分两拍启动 + 逐拍发送：

```
空闲（send_arp_header=0）
  └─ 上游握手 s_frame_valid & s_frame_ready
       ├─ store_frame=1：锁存全部 ARP 字段到内部寄存器
       ├─ m_eth_hdr_valid=1：通知下游以太网头就绪
       └─ 进入 send_arp_header，ptr=0

发送阶段 send_arp_header（每个 tready 周期）
  ├─ ptr += 1
  ├─ 按 ptr 把对应字节填进 m_eth_payload_axis_tdata
  ├─ 每填一字节置对应的 tkeep=1
  └─ 当 ptr 到达第 28 字节（偏移 27）：tlast=1，回到空闲

反压处理（输出级）
  用「输出寄存器 + temp 缓冲」双级结构，
  下游不 ready 时把待发数据暂存到 temp，不反压上游的序列化状态机。
```

`arp_eth_tx` 与 `arp_eth_rx` 共用同一组 `CYCLE_COUNT`/`BYTE_LANES` 计算，因此同样支持 8/16/32/64 位等宽位宽：宽位宽时一个周期可同时发出多个字段字节。

#### 4.2.3 源码精读

**握手即锁存**——上游一握手，模块立刻把所有并行字段存进内部寄存器并拉高以太网头有效（[rtl/arp_eth_tx.v:L183-L188](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp_eth_tx.v#L183-L188)）：

```verilog
if (s_frame_ready && s_frame_valid) begin
    store_frame = 1'b1;
    m_eth_hdr_valid_next = 1'b1;
    ptr_next = 0;
    send_arp_header_next = 1'b1;
end
```

随后 `store_frame` 在时钟沿把 `s_arp_*` 全部锁存（[rtl/arp_eth_tx.v:L257-L268](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp_eth_tx.v#L257-L268)），这样序列化期间上游字段可以变化而不影响本帧。

**序列化宏与硬编码长度**——与 rx 的宏互逆，这里是「把字段字节放进 `tdata` 的对应位置」（[rtl/arp_eth_tx.v:L200-L211](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp_eth_tx.v#L200-L211)）：

```verilog
`_HEADER_FIELD_(4,  8'd6)      // HLEN 硬编码为 6
`_HEADER_FIELD_(5,  8'd4)      // PLEN 硬编码为 4
`_HEADER_FIELD_(6,  arp_oper_reg[1*8 +: 8])   // oper 高字节→偏移6
`_HEADER_FIELD_(7,  arp_oper_reg[0*8 +: 8])   // oper 低字节→偏移7
```

注意第 4、5 行传的是常量 `8'd6`、`8'd4`，而不是某个输入端口——这就是「HLEN/PLEN 不可配」的源码证据。字节序与 rx 完全镜像：高字节进小偏移。

**帧尾与就绪**——发送到第 28 字节时拉 `tlast` 并退出（[rtl/arp_eth_tx.v:L235-L238](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp_eth_tx.v#L235-L238)）；而 `s_frame_ready` 的产生条件是「既没在发头、也没在等下游消费头」（[rtl/arp_eth_tx.v:L244](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp_eth_tx.v#L244)），保证一次只处理一帧：

```verilog
if (ptr_reg == 27/BYTE_LANES) begin
    m_eth_payload_axis_tlast_int = 1'b1;
    send_arp_header_next = 1'b0;
end
...
s_frame_ready_next = !m_eth_hdr_valid_next && !send_arp_header_next;
```

**输出反压级**——载荷输出用了和 `eth_axis_tx`、`axis_eth_fcs` 同源的「输出寄存器 + temp 缓冲」结构（[rtl/arp_eth_tx.v:L279-L304](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp_eth_tx.v#L279-L304)）。核心是这一行「提前就绪」（[rtl/arp_eth_tx.v:L304](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp_eth_tx.v#L304)）：

```verilog
assign m_eth_payload_axis_tready_int_early =
    m_eth_payload_axis_tready || (!temp_m_eth_payload_axis_tvalid_reg && !m_eth_payload_axis_tvalid_reg);
```

含义：只要下游 ready，或者两級输出寄存器都空，就让上游的序列化状态机继续推进。这样即使下游偶尔反压，序列化状态机也能借助 temp 缓冲多走一拍，不至于卡死。

#### 4.2.4 代码实践

**实践目标**：通过阅读 `arp.v` 里 `arp_eth_tx` 的实例化，确认一个「ARP 请求」是怎么被填出来的。

**操作步骤**：

1. 打开 [rtl/arp.v:L179-L214](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp.v#L179-L214)，这是 `arp_eth_tx_inst`。
2. 观察哪些字段是常量、哪些是寄存器：
   - `s_eth_type(16'h0806)`、`s_arp_htype(16'h0001)`、`s_arp_ptype(16'h0800)` —— ARP 帧的固定身份。
   - `s_eth_src_mac(local_mac)`、`s_arp_sha(local_mac)`、`s_arp_spa(local_ip)` —— 本机的 MAC/IP，请求和应答都用本机身份发送。
   - `s_arp_oper(outgoing_arp_oper_reg)`、`s_arp_tha(outgoing_arp_tha_reg)`、`s_arp_tpa(outgoing_arp_tpa_reg)` —— 这三个是「变量」，决定帧类型。
3. 找到发请求的地方（[rtl/arp.v:L347](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp.v#L347) 与 [rtl/arp.v:L374](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp.v#L374)），你会看到 `outgoing_arp_oper_next = ARP_OPER_ARP_REQUEST`（即 1）。

**需要观察的现象**：发请求时 `outgoing_arp_oper_reg=1`；由于此时还不知道目标 MAC，THA 通常保持 0，TPA 设成待解析的目标 IP。

**预期结果**：你能用自己的话说出——「请求」与「应答」走的是**同一个 `arp_eth_tx` 模块**，区别只在这三个寄存器的取值，而不是两套硬件。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `arp_eth_tx` 不需要 `s_arp_hlen`、`s_arp_plen` 输入端口？

**答案**：因为本库只支持以太网+IPv4（HLEN=6、PLEN=4），所以直接在序列化时硬编码（`8'd6`、`8'd4`），省去端口也省去出错的可能。

**练习 2**：若下游长时间不拉 `m_eth_payload_axis_tready`，`arp_eth_tx` 会无限积压字节吗？

**答案**：不会。输出级只有一个 temp 缓冲（一拍容量），当下游反压且 temp 已满时，`m_eth_payload_axis_tready_int_early` 会变 0，序列化状态机 `ptr` 就停止推进，直到下游恢复。整帧停在流水线里等待，不会丢数据。

---

### 4.3 ARP 应答构造：操作码与字段交换

#### 4.3.1 概念说明

如上一节所述，`arp_eth_tx` 本身不分请求/应答。真正的「应答构造」逻辑在上层 `arp.v` 里：当收到一个针对本机 IP 的请求时，`arp.v` 决定回一个应答，并按 ARP 协议约定**交换发送方/目标字段**。

操作码常量定义在 [rtl/arp.v:L108-L111](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp.v#L108-L111)：

```verilog
ARP_OPER_ARP_REQUEST  = 16'h0001,   // 请求
ARP_OPER_ARP_REPLY    = 16'h0002,   // 应答
ARP_OPER_INARP_REQUEST = 16'h0008,  // 反向 ARP 请求（本库支持但少见）
ARP_OPER_INARP_REPLY   = 16'h0009;
```

请求与应答的字段差异总结：

| 字段 | 请求（OPER=1） | 应答（OPER=2） |
|------|---------------|---------------|
| `s_arp_oper` | 1 | 2 |
| `s_eth_dest_mac` | 广播 `ff:ff:ff:ff:ff:ff` | 请求方的单播 MAC |
| `s_arp_sha` / `s_arp_spa` | 本机 MAC/IP | 本机 MAC/IP |
| `s_arp_tha` | 0（未知） | 请求方的 MAC |
| `s_arp_tpa` | 待解析的目标 IP | 请求方的 IP |

核心规律：**应答就是把请求里的「对方」填进 THA/TPA，把目的 MAC 改成对方单播，OPER 改成 2**。SHA/SPA 永远是本机身份。

#### 4.3.2 核心流程

`arp.v` 收到一帧并解析后（由 `arp_eth_rx` 完成），在 `always @*` 块里做决策：

```
收到合法 ARP 帧（incoming_frame_valid）：
  if OPER == REQUEST 且 TPA == local_ip：
      // 这是问我的请求 → 回应答
      outgoing_arp_oper      = REPLY (2)
      outgoing_eth_dest_mac  = incoming_sha       // 回给请求方
      outgoing_arp_tha       = incoming_sha       // 目标 MAC = 请求方
      outgoing_arp_tpa       = incoming_spa       // 目标 IP  = 请求方
      触发 arp_eth_tx 发送
```

随后 `arp_eth_tx` 把这组字段串行化成应答帧发出。

#### 4.3.3 源码精读

**应答决策**——`arp.v` 中处理「收到的请求」并组装应答字段的核心片段（[rtl/arp.v:L303-L319](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp.v#L303-L319)）：

```verilog
if (incoming_arp_oper == ARP_OPER_ARP_REQUEST) begin
    // 命中本机 IP 才回应（具体判定条件见源码上下文）
    ...
    outgoing_arp_oper_next = ARP_OPER_ARP_REPLY;   // OPER=2
    // outgoing_eth_dest_mac / outgoing_arp_tha / outgoing_arp_tpa
    // 在此处被填成请求方的地址（交换发送方/目标）
end
```

把这段与 4.2.3 的实例化对照看：这里写进 `outgoing_arp_oper_next` 等寄存器的值，下一拍就会出现在 `arp_eth_tx_inst` 的 `s_arp_oper` 等端口上，被串行化成应答帧。**决策在 `arp.v`，执行在 `arp_eth_tx`**——这条分工是理解整个 ARP 子系统的关键。

**字段来源对照**——回看 [rtl/arp.v:L188-L199](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp.v#L188-L199) 的端口连接：`s_eth_src_mac`/`s_arp_sha` 接 `local_mac`、`s_arp_spa` 接 `local_ip`，是常量；`s_arp_oper`/`s_arp_tha`/`s_arp_tpa`/`s_eth_dest_mac` 接寄存器，是变量。应答和请求的差别，全部落在这些「变量」端口上。

#### 4.3.4 代码实践

**实践目标**：用 `arp_eth_tx` 单独仿真发送一个 ARP **应答**帧，抓取输出载荷，确认 OPER=2 且字段顺序正确。

**操作步骤**（参照 `tb/arp_eth_tx/test_arp_eth_tx.py` 的结构）：

1. 进入目录运行现成测试：
   ```bash
   cd tb/arp_eth_tx
   make
   ```
2. 该 testbench 用 `ArpHdrSource`（由 `define_stream` 生成）驱动 `arp_eth_tx` 的 `s_*` 并行输入，用 `AxiStreamSink` 收 `m_eth_payload_axis_*`。仿照其用例，构造一个应答帧输入：
   - `s_arp_oper = 2`、`s_arp_sha = 本机 MAC`、`s_arp_spa = 本机 IP`、`s_arp_tha = 对方 MAC`、`s_arp_tpa = 对方 IP`、`s_eth_dest_mac = 对方 MAC`。
3. 收到 28 字节载荷后，用 scapy 的 `ARP` 解析，断言 `op == 2`、`hwsrc`/`hwdst` 正确。

**需要观察的现象**：输出载荷恰好 28 字节，末拍 `tlast=1`；第 5、6 字节（HLEN、PLEN）恒为 6、4；第 6–7 字节（OPER）为大端的 0x0002。

**预期结果**：scapy 解析出的 ARP 字段与输入一一对应，OPER=2。

> 运行结果**待本地验证**。`tb/arp_eth_tx/test_arp_eth_tx.py` 是可运行的现成样板。

#### 4.3.5 小练习与答案

**练习 1**：如果想让 `arp_eth_tx` 同时支持 IPv6（PLEN=16），需要改哪些地方？

**答案**：需要把 HLEN/PLEN 从硬编码改成输入端口（或参数），同时 `arp_eth_rx` 的校验 `m_arp_hlen_next != 4'd6 || m_arp_plen_next != 4'd4` 也要放宽，且 THA/TPA 等字段的字节偏移、`HDR_SIZE` 都要重新计算。本库未做此泛化。

**练习 2**：为什么应答帧的 `s_eth_dest_mac` 要设成请求方的 MAC，而不是继续广播？

**答案**：ARP 请求是广播（问所有人），但应答是单播——只有请求方需要这个回答，单播可以节省网络带宽，也符合 ARP 协议约定。

## 5. 综合实践

把 `arp_eth_rx` 与 `arp_eth_tx` 背靠背连成一个「ARP 回显」小系统，串起本讲全部知识：

1. **接线**：把 `arp_eth_rx` 的并行输出（`m_arp_*`）直接接到 `arp_eth_tx` 的并行输入（`s_arp_*`），做一个简单变换——把 `m_arp_oper`（请求=1）翻成 `s_arp_oper`（应答=2），把 `m_arp_sha`/`m_arp_spa`（请求方）填进 `s_arp_tha`/`s_arp_tpa`，本机 MAC/IP 填进 `s_arp_sha`/`s_arp_spa`。
2. **激励**：用 cocotb 构造一个 ARP 请求（`op=1`，广播目的）送入 `arp_eth_rx`。
3. **验证**：从 `arp_eth_tx` 的 `m_eth_payload_axis_*` 抓取输出，用 scapy 解析，确认：
   - OPER 变成了 2（应答）；
   - THA/TPA 是原请求方的 SHA/SPA；
   - SHA/SPA 是本机地址；
   - 28 字节长度、HLEN=6、PLEN=4。
4. **进阶**：人为把输入帧截短（少于 28 字节），确认 `arp_eth_rx` 报 `error_header_early_termination`，且 `arp_eth_tx` 不会发出残缺帧。

这个任务实际上就是 `arp.v` 里「收到请求→回应答」那一段逻辑的最小复刻。做完后，再去读 [rtl/arp.v:L303-L319](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp.v#L303-L319)，你会发现真实代码和你写的变换几乎一样。

## 6. 本讲小结

- `arp_eth_rx` 把 28 字节的 ARP 载荷流解析成并行字段，靠 `` `_HEADER_FIELD_ `` 宏逐字节映射；帧尾做「头是否收齐 + HLEN/PLEN 是否合法 + tuser 坏帧位」三态判定。
- `arp_eth_tx` 是 `arp_eth_rx` 的逆运算，握手时锁存全部字段，再逐字节串行化；HLEN=6、PLEN=4 被硬编码。
- 请求（OPER=1）和应答（OPER=2）**共用同一个 `arp_eth_tx`**，区别只在上层填什么字段值，不在硬件本身。
- 两模块都严格遵循 u3-l1 确立的「并行头 + AXI-Stream 载荷」接口风格，能和 `eth_axis_rx`/`eth_axis_tx` 无缝对接。
- 字节序全程网络大端：多字节字段的高字节占据更小的字节偏移，rx/tx 的宏互为镜像。
- 输出级沿用全库通用的「输出寄存器 + temp 缓冲」反压模板，保证下游反压时数据不丢。

## 7. 下一步学习建议

- 下一篇 **u6-l2（ARP 缓存与顶层 arp 模块）** 会把本讲的两个模块与 `arp_cache` 组装成完整的 `arp` 顶层，加入「IP→MAC 查询、未命中自动发请求、超时重试」的完整逻辑。本讲引用的 [rtl/arp.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp.v) 正是下一篇的主角。
- 在那之前，建议先回到 u3-l1 复习 `eth_axis_rx/tx` 的握手细节，因为 ARP 层完全建立在它之上。
- 想提前感受 ARP 在系统里的位置，可以跳读 [rtl/ip_complete.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_complete.v)，看 `arp` 模块如何与 IP 层、复用器协同（这属于 u7-l3 的内容）。
