# IP 帧接收与发送

## 1. 本讲目标

学完本讲，你应当能够：

- 画出 IPv4 头部 20 字节的字段布局（Version / IHL / DSCP / ECN / Total Length / Identification / Flags / Fragment Offset / TTL / Protocol / Header Checksum / Source IP / Destination IP），并说出每个字段的作用。
- 读懂 `ip_eth_rx` 如何从一段以太网载荷（payload）字节流中逐字节拆出 IPv4 头、在帧尾判定「版本/IHL 是否合法」「校验和是否正确」，并把后续载荷原样转发。
- 用「16 位反码求和（one's complement sum）」**手算**一个 IPv4 头的校验和，并解释为什么 `ip_eth_rx` 把「累加结果 == 0xFFFF」当作校验通过的判据。
- 读懂 `ip_eth_tx` 如何把一组并行 IPv4 字段重新串行化成 20 字节头，**并在发送过程中自行计算校验和**（它没有校验和输入端口）。
- 理解 `ip_eth_rx`/`ip_eth_tx` 与 `eth_axis_rx`/`arp_eth_rx` 共用同一套「并行头 + AXI-Stream 载荷」接口风格，是协议栈逐层拆/封包的又一层。

## 2. 前置知识

本讲承接 **u3-l1（eth_axis_rx/tx）** 与 **u6-l1（arp_eth_rx/tx）**，需要你先建立以下认知：

- **「并行头 + AXI-Stream 载荷」接口风格**：本库每一层协议模块都把该层协议头用并行信号（配 `hdr_valid`/`hdr_ready` 握手）传递，把该层载荷用标准 AXI-Stream（`tdata`/`tvalid`/`tready`/`tlast`/`tuser`）流式传递。`eth_axis_rx` 已把 14 字节以太网头拆成 `dest_mac`/`src_mac`/`type` 并行字段 + 一根 payload AXI 流；`ip_eth_rx` 就接在这根流之后，继续往上拆 IPv4 头。
- **网络字节序（大端）**：IPv4 在线路上按大端传输，多字节字段的高字节先发、占据更小的字节偏移。
- **反码求和（直觉）**：IP 校验和不是 CRC，而是一种「把头部切成 16 位字、用反码加法求和、再把和取反填回去」的轻量校验。它的好处是**收发双方用同一套逻辑**：发送方把校验和字段先当 0 算出和再取反填入；接收方把整头（含校验和）再求一次和，正确时结果应为全 1（`0xFFFF`）。本讲 4.2 会从数学上讲清这一点。

> 简单记忆：`ip_eth_rx` 和 `ip_eth_tx` 这一对，本质上和 `arp_eth_rx`/`arp_eth_tx` 是同一类「拆头/封头」模块。区别只在于 IPv4 头比 ARP 头多了**校验和**这件事——`ip_eth_rx` 要校验它，`ip_eth_tx` 要现场算它。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [rtl/ip_eth_rx.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_rx.v) | IPv4 帧接收器：输入「以太网头（并行）+ 载荷（AXI 流）」，剥掉并解析 20 字节 IPv4 头，输出并行 IP 字段 + IP 载荷 AXI 流；同时做版本/IHL 合法性检查与校验和校验。 |
| [rtl/ip_eth_tx.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_tx.v) | IPv4 帧发送器：输入并行 IP 字段 + IP 载荷 AXI 流，**自行计算校验和**，串行化出 20 字节 IPv4 头并拼到以太网载荷前。 |
| [tb/test_ip_eth_rx.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/test_ip_eth_rx.py) | `ip_eth_rx` 的测试文件（myhdl 时代遗留，见下文说明）。它用 12 组用例覆盖了正常帧、坏 IHL、坏校验和、截断头/载荷等所有错误分支，是理解模块行为的最佳「行为说明书」。 |
| [rtl/ip.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip.v) | 顶层 IP 模块（下一篇 u7-l2 详解）。本讲引用它，说明 `ip_eth_rx`/`ip_eth_tx` 在系统里被谁例化。 |

> **关于测试文件的一个诚实说明**：和 `arp_eth_rx` 不同，本仓库**没有** `tb/ip_eth_rx/` 这样的 cocotb 子目录（没有 `Makefile` + `test_*.py` 的现行三件套）。`tb/test_ip_eth_rx.py` 顶部是 `from myhdl import *`，属 u1-l4 讲过的 **myhdl 时代历史遗留测试**，现行 tox/pytest 回归并不编译它。但它的 12 组用例完整、断言清晰，仍是理解 `ip_eth_rx` 行为的最权威依据——因此本讲的代码实践以「阅读这份测试 + 手算校验和」为主（属 worker 规范允许的「源码阅读型实践」）。

## 4. 核心概念与源码讲解

### 4.1 IPv4 头解析（ip_eth_rx）

#### 4.1.1 概念说明

`ip_eth_rx` 做的事情和 `arp_eth_rx` 同构：把一段固定长度的协议头（这里是 20 字节 IPv4 头）从字节流里「翻译」成一组并行字段，同时把头部之后的载荷原样转发出去。

它的上游同样是 `eth_axis_rx`（或等价成帧层），后者已经把以太网帧的 14 字节头剥掉，分成了并行以太网头（`s_eth_dest_mac`/`s_eth_src_mac`/`s_eth_type`）和载荷 AXI 流。`ip_eth_rx` 接住这两部分，**透传以太网头**，同时把载荷的前 20 个字节映射成 IPv4 各字段，从第 21 字节起当作 IP 载荷继续往下游送。

与 `arp_eth_rx` 相比，有三处关键的不同（这些是本讲的重点）：

- **IPv4 头是 20 字节，不是 28 字节**，且本模块**只认 IHL=5**（即 20 字节、无选项）。IHL≠5 或 Version≠4 一律判为 `error_invalid_header` 并丢弃。
- **多了一项校验和校验**：边收头边用反码求和累加，帧尾判定和是否为 `0xFFFF`，不是就报 `error_invalid_checksum`。
- **载荷长度由 `ip_length` 字段决定**：模块会用 `ip_length - 20`（头长）算出载荷字节数，并据此在帧尾判定「实际收到的载荷是否与声明长度一致」，不一致报 `error_payload_early_termination`。

需要注意：和 ARP 一样，`ip_eth_rx` **不校验 EtherType** 是不是 `0x0800`——EtherType 的分发由上层（如 `ip_complete` 里的复用器）负责，它只管「假设这是一段 IPv4 载荷，把它拆开」。

#### 4.1.2 核心流程

`ip_eth_rx` 是一个 5 状态机（[rtl/ip_eth_rx.v:L121-L126](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_rx.v#L121-L126)），靠一个 6 位字节指针 `hdr_ptr_reg` 在头部里逐拍推进：

```
STATE_IDLE（空闲，等以太网头握手）
  └─ s_eth_hdr_valid & s_eth_hdr_ready
       ├─ store_eth_hdr=1：锁存以太网头
       └─ 进入 STATE_READ_HEADER，hdr_ptr=0

STATE_READ_HEADER（消费前 20 字节 = IPv4 头）
  每收到一个有效字节（tvalid & tready）：
       ├─ hdr_ptr += 1
       ├─ 按 hdr_ptr 把该字节存进对应的 m_ip_*_reg 字段
       ├─ 同时把该字节按大端拼进 16 位反码和 hdr_sum
       └─ hdr_ptr==0x13（第 20 字节，头收齐）时做最终判定：
            ├─ version≠4 或 ihl≠5            → error_invalid_header，转 WAIT_LAST 丢帧
            ├─ hdr_sum != 0xFFFF              → error_invalid_checksum，转 WAIT_LAST 丢帧
            └─ 否则                            → m_ip_hdr_valid=1，转 READ_PAYLOAD
  若收头期间出现 tlast（头没收齐就结束）→ error_header_early_termination

STATE_READ_PAYLOAD（按 ip_length-20 转发载荷）
  每个有效字节透传到 m_ip_payload_axis_*，word_count 递减：
       ├─ 正常：继续
       ├─ word_count 减到 1（声明载荷已发完）但还没 tlast：
       │     存住最后一字节，转 STATE_READ_PAYLOAD_LAST（处理以太网填充）
       └─ tlast 先到但 word_count>1：声明长度对不上 → error_payload_early_termination

STATE_READ_PAYLOAD_LAST / STATE_WAIT_LAST
  丢弃后续字节（如以太网最小帧填充），等到真正的 tlast 收尾，回 IDLE
```

> 关键直觉：`ip_eth_rx` 在「不知道帧长」的情况下，靠 `ip_length` 字段**自己算出载荷该有多少字节**，因此能识别并丢弃以太网为了凑最小帧长而追加的尾部填充（padding），只把真正的 IP 载荷交给上层。

#### 4.1.3 源码精读

**接口**——严格遵循「并行以太网头 + AXI 载荷」风格（[rtl/ip_eth_rx.v:L34-L88](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_rx.v#L34-L88)）：输入侧 `s_eth_*` 是并行以太网头加 `s_eth_payload_axis_*` 载荷流；输出侧除了透传的以太网头，多出一大组 `m_ip_*` 并行字段（version/ihl/dscp/ecn/length/identification/flags/fragment_offset/ttl/protocol/header_checksum/source_ip/dest_ip）加 `m_ip_payload_axis_*` 载荷流。注意 `m_ip_*` 字段比 ARP 多得多——这就是 IPv4 头比 ARP 头复杂之所在。

**IPv4 头布局注释**——源码头部用一张表列出了 20 字节字段布局（[rtl/ip_eth_rx.v:L90-L119](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_rx.v#L90-L119)），把它整理成下表（偏移即 `hdr_ptr` 走过的字节位置）：

| 字节偏移 | 字段 | 长度 | 典型值 | 解析后输出端口 |
|---|---|---|---|---|
| 0 | Version + IHL | 1 | 0x45（v4, IHL=5） | `m_ip_version`/`m_ip_ihl` |
| 1 | DSCP + ECN | 1 | 0x00 | `m_ip_dscp`/`m_ip_ecn` |
| 2–3 | Total Length | 2 | 头+载荷总长 | `m_ip_length` |
| 4–5 | Identification | 2 | 0 | `m_ip_identification` |
| 6–7 | Flags + Fragment Offset | 2 | 0x4000（DF=1） | `m_ip_flags`/`m_ip_fragment_offset` |
| 8 | TTL | 1 | 64 | `m_ip_ttl` |
| 9 | Protocol | 1 | 0x11=UDP / 0x06=TCP | `m_ip_protocol` |
| 10–11 | Header Checksum | 2 | 现算 | `m_ip_header_checksum` |
| 12–15 | Source IP | 4 | 如 192.168.1.100 | `m_ip_source_ip` |
| 16–19 | Destination IP | 4 | 如 192.168.1.101 | `m_ip_dest_ip` |

**逐字节提取——用 `case` 而非宏**——与 `arp_eth_rx` 用 `` `_HEADER_FIELD_ `` 宏不同，`ip_eth_rx` 直接用一个 `case (hdr_ptr_reg)` 把每个字节偏移映射到一个 `store_*` 脉冲（[rtl/ip_eth_rx.v:L311-L345](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_rx.v#L311-L345)）：

```verilog
case (hdr_ptr_reg)
    6'h00: store_ip_version_ihl = 1'b1;
    6'h01: store_ip_dscp_ecn = 1'b1;
    6'h02: store_ip_length_1 = 1'b1;   // 长度高字节（偏移2，大端）
    6'h03: store_ip_length_0 = 1'b1;   // 长度低字节（偏移3）
    ...
    6'h0C: store_ip_source_ip_3 = 1'b1; // src IP 最高字节（偏移12）
    6'h0D: store_ip_source_ip_2 = 1'b1;
    6'h0E: store_ip_source_ip_1 = 1'b1;
    6'h0F: store_ip_source_ip_0 = 1'b1; // src IP 最低字节（偏移15）
    6'h10: store_ip_dest_ip_3 = 1'b1;
    ...
    6'h13: begin /* 头收齐，做最终判定 */ end
endcase
```

随后这些 `store_*` 脉冲在时钟沿把字节写进对应字段（[rtl/ip_eth_rx.v:L476-L495](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_rx.v#L476-L495)）。注意大端拆分：`store_ip_source_ip_3`（`[31:24]`，最高字节）接偏移 0x0C（最小偏移，先到），`store_ip_source_ip_0`（`[7:0]`）接偏移 0x0F——与 u3-l1/u6-l1 的大端约定一致。

**头收齐时的三态判定**——`hdr_ptr` 到 0x13（第 20 字节）时，模块一次性做三项检查（[rtl/ip_eth_rx.v:L331-L343](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_rx.v#L331-L343)）：

```verilog
6'h13: begin
    store_ip_dest_ip_0 = 1'b1;
    if (m_ip_version_reg != 4'd4 || m_ip_ihl_reg != 4'd5) begin
        error_invalid_header_next = 1'b1;     // 只支持 IPv4 + 20 字节头
        state_next = STATE_WAIT_LAST;          // 丢弃本帧剩余字节
    end else if (hdr_sum_next != 16'hffff) begin
        error_invalid_checksum_next = 1'b1;    // 校验和不符
        state_next = STATE_WAIT_LAST;
    end else begin
        m_ip_hdr_valid_next = 1'b1;            // 头合法，提交给下游
        state_next = STATE_READ_PAYLOAD;
    end
end
```

这一段是 `ip_eth_rx` 的「大脑」：版本必须是 4、IHL 必须是 5（**本库不支持 IP 头选项**，否则 IHL>5），校验和累加结果必须等于 `0xFFFF`。任一不满足，本帧的 IP 头都不提交（`m_ip_hdr_valid` 不拉高），并转入 `STATE_WAIT_LAST` 把帧剩余字节安静地消费掉，准备下一帧。

**载荷长度与尾部填充处理**——进入 `STATE_READ_HEADER` 时，模块用刚收到的长度字段算出载荷字节数（[rtl/ip_eth_rx.v:L298](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_rx.v#L298)）：

```verilog
word_count_next = m_ip_length_reg - 5*4;   // 载荷 = 总长 - 20 字节头
```

随后在 `STATE_READ_PAYLOAD` 里逐字节递减 `word_count`（[rtl/ip_eth_rx.v:L359-L392](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_rx.v#L359-L392)）。这里有一个精妙设计：当 `word_count` 减到 1（声明的最后一个载荷字节）但帧还没到 `tlast` 时，模块把这个字节存进 `last_word_data_reg` 并转入 `STATE_READ_PAYLOAD_LAST`（[rtl/ip_eth_rx.v:L381-L384](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_rx.v#L381-L384)），在那里**丢弃后续字节**（以太网 padding），直到真正的 `tlast` 才把存住的最后一字节连同 `tlast` 一起发出去。这样上层收到的载荷长度精确等于 `ip_length-20`，padding 被自动剥离。

若反过来——`tlast` 先到、但 `word_count` 还大于 1（实际载荷比声明短）——则报 `error_payload_early_termination` 并在末字节打 `tuser=1`（[rtl/ip_eth_rx.v:L371-L376](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_rx.v#L371-L376)）。

**输出反压级**——IP 载荷输出沿用全库通用的「输出寄存器 + temp 缓冲」结构（[rtl/ip_eth_rx.v:L498-L520](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_rx.v#L498-L520)），与 `arp_eth_tx`/`axis_eth_fcs` 同源，下游反压时数据不丢，这里不再展开。

#### 4.1.4 代码实践

**实践目标**：通过阅读遗留测试 [tb/test_ip_eth_rx.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/test_ip_eth_rx.py)，把 12 组用例与 `ip_eth_rx` 的 4 个错误信号逐一对应，建立起「什么输入触发什么报错」的行为地图。

**操作步骤**：

1. 打开 [tb/test_ip_eth_rx.py:L254-L1022](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/test_ip_eth_rx.py#L254-L1022)，定位 `for payload_len in range(1,18)` 这段主循环。
2. 每组用例都会构造一个 `ip_ep.IPFrame()`，关键字段是：`eth_type=0x0800`、`ip_version=4`、`ip_ihl=5`、`ip_ttl=64`、`ip_protocol=0x11`（UDP）、`ip_source_ip=0xc0a80164`（192.168.1.100）、`ip_dest_ip=0xc0a80165`（192.168.1.101），载荷是 `bytearray(range(payload_len))`。`ip_length`/`ip_header_checksum` 设为 `None`，由 `ip_ep` 的 `build()` 自动算出（这正是我们 4.2 要手算的同一组值）。
3. 对照下表，逐个找到每组用例的「破坏点」与它断言的错误信号：

| 用例 | 破坏点（相对正常帧） | DUT 应报的信号 | 源码定位 |
|---|---|---|---|
| test 1 | 无（正常帧） | 无错误，`m_ip_hdr_valid` 拉高 | [L331-L343](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_rx.v#L331-L343) |
| test 4/5 | 载荷后追加 1/10 字节 padding | 无错误（padding 被 `READ_PAYLOAD_LAST` 剥离） | [L381-L384](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_rx.v#L381-L384) |
| test 8/9 | 把载荷末 1/10 字节截掉 | `error_payload_early_termination` | [L371-L376](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_rx.v#L371-L376) |
| test 10 | `ip_ihl=6`（非法头长） | `error_invalid_header` | [L333-L335](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_rx.v#L333-L335) |
| test 11 | `ip_header_checksum=0x1234`（故意写错） | `error_invalid_checksum` | [L336-L338](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_rx.v#L336-L338) |
| test 12 | 把整段载荷截到不足 20 字节（头没收齐就 `tlast`） | `error_header_early_termination` | [L347-L353](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_rx.v#L347-L353) |

**需要观察的现象**：test 11 把校验和改成 `0x1234` 后，DUT 收完 20 字节头时 `hdr_sum` 不再等于 `0xFFFF`，于是 `error_invalid_checksum` 拉高一拍，且该帧的 IP 头不会被提交（`m_ip_hdr_valid` 不拉高），紧跟其后的 test 帧（正常）仍能被正确解析——说明错误帧被「安静丢弃」而不污染后续帧。

**预期结果**：你能不查源码就说出「给 `ip_eth_rx` 喂一个 IHL=6 的包会发生什么」「喂一个校验和错的包会发生什么」「喂一个载荷被截短的包会发生什么」。

> 说明：这份测试依赖 myhdl，**不在现行 tox/pytest 回归内**（详见 u1-l4 与本讲源码地图的说明），因此本实践以「阅读 + 推理」为主，**待本地验证**的部分是指：若你已配好 myhdl + iverilog，可直接 `python tb/test_ip_eth_rx.py` 运行确认断言通过。

#### 4.1.5 小练习与答案

**练习 1**：如果上游送来的 IPv4 包 `ip_ihl=7`（带 8 字节 IP 选项），`ip_eth_rx` 会怎样处理这些选项？

**答案**：会报 `error_invalid_header` 并丢弃整帧。模块在 [rtl/ip_eth_rx.v:L333](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_rx.v#L333) 硬性要求 `m_ip_ihl_reg == 4'd5`，**不支持任何 IP 头选项**——这是本库的有意简化（大多数嵌入式以太网应用不需要 IP 选项）。

**练习 2**：`ip_eth_rx` 报了 `error_invalid_checksum` 之后，紧跟其后的下一帧还能被正常解析吗？

**答案**：能。报错后模块转入 `STATE_WAIT_LAST` 把当前帧剩余字节消费干净（[rtl/ip_eth_rx.v:L414-L429](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_rx.v#L414-L429)），等 `tlast` 后干净地回到 `STATE_IDLE`，状态机不残留，下一帧从头开始正常解析。test 11 的断言正是验证这一点（错误帧后紧跟的正常帧仍被正确接收）。

---

### 4.2 头部校验和：16 位反码求和

#### 4.2.1 概念说明

IPv4 头部校验和（Header Checksum）只覆盖**头部 20 字节**（不含载荷），目的是让接收方能快速确认「头在传输中没被改坏」。它的算法与 FCS（CRC-32）完全不同，是一套**反码求和（one's complement sum）**：

- 把头部按 16 位（2 字节）切成一个个「字」。
- 把这些字用**反码加法**相加（即普通相加后，把溢出进位折回来再加）。
- 发送方：把校验和字段先当 0，算出其余字的反码和 `S`，再把校验和填成 `~S`（按位取反）。
- 接收方：把整头（含校验和）再求一次反码和。由于校验和 = `~S`，总和 = `S + ~S` = 全 1（`0xFFFF`）。所以**「和 == 0xFFFF」即校验通过**。

这就是为什么 `ip_eth_rx` 在 [rtl/ip_eth_rx.v:L336](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_rx.v#L336) 用 `hdr_sum_next != 16'hffff` 作为「坏校验和」判据。

#### 4.2.2 核心流程（数学原理）

反码加法把任意长度的「溢出进位」折回最低位，等价于在模 \(2^{16}-1\) 下求和：

\[ \text{ones\_complement\_sum}(w_0,\dots,w_{n-1}) \;=\; \left(\sum_{i=0}^{n-1} w_i\right) \bmod (2^{16}-1) \]

由于发送方填的校验和 \(c = \lnot S\)（按 16 位取反），而 \(S + \lnot S = 2^{16}-1\)，故接收方重算时：

\[ S_{\text{rx}} \;=\; \text{ones\_complement\_sum}(\text{含校验和的整头}) \;=\; (S + c) \bmod (2^{16}-1) \;=\; 2^{16}-1 \;=\; \texttt{0xFFFF} \]

硬件上不需要做真正的「模运算」，只需用一个 17 位加法器累加，再把最高位（进位）折回最低位加一次即可——这正是模块里 `add1c16b` 函数做的事（[rtl/ip_eth_rx.v:L223-L230](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_rx.v#L223-L230)）：

```verilog
function [15:0] add1c16b;       // 16 位反码加法
    input [15:0] a, b;
    reg [16:0] t;
    begin
        t = a+b;                 // 17 位：t[16] 是进位
        add1c16b = t[15:0] + t[16];  // 把进位折回最低位
    end
endfunction
```

> 严格说，多次累加可能产生「折回后又再进位」的情况，但因每步都折回一次，最终残留最多一个进位，工程上已足够；若要绝对严格可在末尾再折一次。本模块逐字节累加、每步折回，等价正确。

#### 4.2.3 源码精读

**逐字节拼成 16 位字**——`ip_eth_rx` 收头时，按 `hdr_ptr` 的奇偶性把当前字节拼成 16 位字的高/低字节再累加（[rtl/ip_eth_rx.v:L305-L309](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_rx.v#L305-L309)）：

```verilog
if (hdr_ptr_reg[0]) begin
    // 奇偏移：当前字节是某 16 位字的低字节
    hdr_sum_next = add1c16b(hdr_sum_reg, {8'd0, s_eth_payload_axis_tdata});
end else begin
    // 偶偏移：当前字节是高字节（大端：高字节先到）
    hdr_sum_next = add1c16b(hdr_sum_reg, {s_eth_payload_axis_tdata, 8'd0});
end
```

偏移 0（偶）的字节进高字节、偏移 1（奇）进低字节，两者拼成头部的第 0 个 16 位字；以此类推。`hdr_sum_reg` 在 `STATE_IDLE` 清零（[rtl/ip_eth_rx.v:L283](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_rx.v#L283)），收完 20 字节（10 个字，含校验和字）后与 `0xFFFF` 比较。

**关键判据**——前文已引用的 [rtl/ip_eth_rx.v:L336](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_rx.v#L336)：`else if (hdr_sum_next != 16'hffff)`。理解了 4.2.2 的数学，这一行就不再「神秘」——它是反码求和「正确即全 1」性质的直接硬件体现。

#### 4.2.4 代码实践（手算校验和）

**实践目标**：用 [tb/test_ip_eth_rx.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/test_ip_eth_rx.py) test 1 的同一组参数，**手算**出一个 IPv4 头的校验和，验证它满足「整头反码和 == 0xFFFF」，从而把 4.1/4.2 的知识串起来。

**给定参数**（与 test 1 完全一致，payload 长度取 1）：

| 字段 | 值 | 头部字节 |
|---|---|---|
| Version / IHL | 4 / 5 | `0x45` |
| DSCP / ECN | 0 / 0 | `0x00` |
| Total Length | 20 头 + 1 载荷 = 21 | `0x00 0x15` |
| Identification | 0 | `0x00 0x00` |
| Flags / Frag Off | DF=1 (0x4000) | `0x40 0x00` |
| TTL / Protocol | 64 / 0x11(UDP) | `0x40 0x11` |
| Header Checksum | **待算** | `0x?? 0x??` |
| Source IP | 192.168.1.100 | `0xC0 0xA8 0x01 0x64` |
| Dest IP | 192.168.1.101 | `0xC0 0xA8 0x01 0x65` |

**操作步骤**：

1. 把头部按大端切成 9 个 16 位字（校验和字段先空着，当 0）：
   `0x4500, 0x0015, 0x0000, 0x4000, 0x4011, 0xC0A8, 0x0164, 0xC0A8, 0x0165`
2. 普通相加：
   \[
   0x4500+0x0015+0x0000+0x4000+0x4011+0xC0A8+0x0164+0xC0A8+0x0165 = 0x2493F
   \]
3. 把超出 16 位的进位折回（`0x2493F` = `0x2 << 16` + `0x493F`）：
   \[
   S = 0x493F + 0x2 = 0x4941
   \]
4. 校验和 = 取反：
   \[
   c = \lnot S = \lnot\texttt{0x4941} = \texttt{0xB6BE}
   \]
   即头部第 10、11 字节为 `0xB6 0xBE`。
5. **验证（接收侧逻辑）**：把校验和字 `0xB6BE` 加入再求一次反码和：
   \[
   S_{\text{rx}} = \texttt{0x4941} + \texttt{0xB6BE} = \texttt{0xFFFF}\ \checkmark
   \]
   等于 `0xFFFF`，与 `ip_eth_rx` 的判据完全吻合。

**需要观察的现象**：你手算的 `0xB6BE` 应当与 `ip_ep.IPFrame.build()` 自动生成的值一致（test 1 正是用 `ip_header_checksum=None` 让 `ip_ep` 算出这个值，再喂给 DUT，DUT 校验通过、`m_ip_hdr_valid` 拉高）。

**预期结果**：手算校验和 `0xB6BE`；接收侧重算和 `0xFFFF`。这同时验证了 4.1（字段提取与判据）与 4.2（反码求和数学）。

> 本实践为纸笔手算，不依赖仿真器；若要机器对照，可用任意 IP 校验和工具或 `ip_ep`/scapy 生成同一包核对。**手算结果可直接复核，无需待本地验证**；与 `ip_ep` 自动生成值的一致性**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 IP 校验和只覆盖头部、不覆盖载荷？

**答案**：为了让路由器在转发时只需重算头部（每跳 TTL 减 1，头部变了，校验和必须更新）而不必扫描整个包，降低转发延迟。载荷完整性交给上层（如 TCP/UDP 自己的校验和，本库 `udp_checksum_gen` 即处理 UDP 校验和）。

**练习 2**：如果某包头部第 4 个字（Identification）在传输中由 `0x0000` 翻成了 `0x0001`，`ip_eth_rx` 能发现吗？

**答案**：能。反码和会从 `0xFFFF` 变成 `0x0000`（差了 1），不再等于 `0xFFFF`，于是触发 `error_invalid_checksum`。这正是校验和能检出的典型单字节/多位翻转错误。但它不是 CRC，对某些「抵消型」双错（如两个字同时各翻一点、和不变）无能为力——这是反码求和相对 CRC-32 的固有弱点，换来的是极其低廉的硬件代价。

---

### 4.3 IPv4 头构造（ip_eth_tx）

#### 4.3.1 概念说明

`ip_eth_tx` 是 `ip_eth_rx` 的逆运算：输入一组并行 IPv4 字段 + IP 载荷 AXI 流，输出串行的 20 字节 IPv4 头拼到以太网载荷前。它和 `eth_axis_tx`/`arp_eth_tx` 一样负责「把并行头重新打成字节流」。

但它有一个**与 `arp_eth_tx` 截然不同**的关键特性：**校验和不是输入，而是模块自己算的**。看端口（[rtl/ip_eth_tx.v:L42-L61](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_tx.v#L42-L61)）——输入里有 `s_ip_dscp`/`s_ip_length`/`s_ip_ttl`/`s_ip_protocol`/`s_ip_source_ip`/`s_ip_dest_ip` 等，但**没有 `s_ip_version`、`s_ip_ihl`、`s_ip_header_checksum`**。原因有二：

- **Version/IHL 被硬编码**：本库只发 IPv4、20 字节头（无选项），所以 Version 恒为 4、IHL 恒为 5，没必要做成端口。
- **校验和现场计算**：发送方的校验和本来就是「根据其余字段算出来的」，既然所有字段都在模块手里，模块就在串行化头部的过程中**顺带累加反码和、并在校验和字段位置把 `~sum` 打出去**。这比让上游算好再传进来更简洁，也避免了「字段变了但校验和没更新」的不一致风险。

#### 4.3.2 核心流程

`ip_eth_tx` 是一个 5 状态机（[rtl/ip_eth_tx.v:L115-L120](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_tx.v#L115-L120)）：

```
STATE_IDLE（等上游头握手）
  └─ s_ip_hdr_valid & s_ip_hdr_ready
       ├─ store_ip_hdr=1：锁存全部 IP 字段到内部寄存器
       ├─ m_eth_hdr_valid=1：通知下游以太网头就绪
       ├─ 立刻发出第 0 字节（0x45），hdr_ptr=1
       └─ 进入 STATE_WRITE_HEADER

STATE_WRITE_HEADER（逐字节发 20 字节头）
  每个 tready 周期：
       ├─ hdr_ptr += 1
       ├─ 按 hdr_ptr 把对应字节填进 m_eth_payload_axis_tdata（大端：高字节先发）
       ├─ 顺带用 add1c16b 把对应 16 位字累加进 hdr_sum（见 4.3.3）
       └─ hdr_ptr==0x13（最后字节）→ 进入 STATE_WRITE_PAYLOAD

STATE_WRITE_PAYLOAD（转发 IP 载荷）
  逐字节把 s_ip_payload_axis 透传到 m_eth_payload_axis，word_count 递减：
       ├─ 声明长度发完且 tlast 到 → 正常收尾，回 IDLE
       ├─ tlast 先到但长度没发完 → error_payload_early_termination，tuser=1
       └─ 长度发完但 tlast 没到  → STATE_WRITE_PAYLOAD_LAST（等真正的 tlast）

STATE_WRITE_PAYLOAD_LAST / STATE_WAIT_LAST：收尾、回 IDLE
```

其中 `word_count` 同样由 `ip_length - 5*4` 初始化（[rtl/ip_eth_tx.v:L231](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_tx.v#L231)），与 rx 对称。

#### 4.3.3 源码精读

**握手即锁存 + 立刻首发**——上游一握手，模块立刻锁存全部字段、拉高以太网头有效，并发出第 0 字节 `0x45`（Version=4, IHL=5，硬编码）（[rtl/ip_eth_tx.v:L210-L228](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_tx.v#L210-L228)）：

```verilog
if (s_ip_hdr_ready && s_ip_hdr_valid) begin
    store_ip_hdr = 1'b1;
    m_eth_hdr_valid_next = 1'b1;
    if (m_eth_payload_axis_tready_int_reg) begin
        m_eth_payload_axis_tvalid_int = 1'b1;
        m_eth_payload_axis_tdata_int = {4'd4, 4'd5};  // Version=4, IHL=5 硬编码
        hdr_ptr_next = 6'd1;
    end
    state_next = STATE_WRITE_HEADER;
end
```

`store_ip_hdr` 在时钟沿把 `s_ip_*` 全部锁存（[rtl/ip_eth_tx.v:L397-L411](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_tx.v#L397-L411)），保证串行化期间上游字段可变而不影响本帧。

**串行化 + 顺带累加校验和**——这是本模块最精彩的一段（[rtl/ip_eth_tx.v:L237-L291](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_tx.v#L237-L291)）。每个 `hdr_ptr` 既决定**发什么字节**，又决定**把哪个 16 位字加进 hdr_sum**，且两者巧妙错位——累加在「发某字的低字节那一拍」用完整的 16 位值一次加入：

```verilog
case (hdr_ptr_reg)
    6'h01: begin
        m_eth_payload_axis_tdata_int = {ip_dscp_reg, ip_ecn_reg};        // 发第1字节
        hdr_sum_next = {4'd4, 4'd5, ip_dscp_reg, ip_ecn_reg};           // 用第0字初值初始化 hdr_sum
    end
    6'h02: begin
        m_eth_payload_axis_tdata_int = ip_length_reg[15: 8];             // 发长度高字节
        hdr_sum_next = add1c16b(hdr_sum_reg, ip_length_reg);            // 加整个长度字
    end
    ...
    6'h09: begin
        m_eth_payload_axis_tdata_int = ip_protocol_reg;                  // 发 protocol
        hdr_sum_next = add1c16b(hdr_sum_reg, ip_dest_ip_reg[15:0]);     // 加 dst IP 低字
    end
    6'h0A: m_eth_payload_axis_tdata_int = ~hdr_sum_reg[15: 8];          // 发校验和高字节 = ~sum
    6'h0B: m_eth_payload_axis_tdata_int = ~hdr_sum_reg[ 7: 0];          // 发校验和低字节 = ~sum
    6'h0C: m_eth_payload_axis_tdata_int = ip_source_ip_reg[31:24];      // 之后只发，不再累加
    ...
endcase
```

注意三个要点：

1. **第 0 字（Version/IHL/DSCP/ECN）的累加被推迟到 ptr=0x01 这一拍**，用完整 16 位值 `{4'd4,4'd5,dscp,ecn}` 作为 `hdr_sum` 的初值（而不是累加进去）——因为握手时已经把第 0 字节发出去了，到 ptr=1 时正好补上这个字的累加。
2. **校验和字段（ptr 0x0A/0x0B）只发送、不累加**——它发的是 `~hdr_sum_reg`，即「其余 9 个字的反码和再取反」，正是 4.2 推导的发送方公式 \(c = \lnot S\)。此时 `hdr_sum_reg` 已在 ptr 0x09 累加完 dst IP 低字，包含全部 9 个非校验和字。
3. **源/目的 IP 的字节先发、后（在更早的 ptr）已累加**——累加用 16 位寄存器原值一次完成（如 ptr 0x06 累加 `source_ip[31:16]`，而该字的高字节要到 ptr 0x0C 才发出去），保证校验和算的是「实际要发出去的头部」。

把这段与 4.2 的手算对照：`ip_eth_tx` 在硬件上做的就是你在 4.2.4 用纸笔做的事——累加 9 个字、取反、填回校验和位置。

**载荷长度一致性检查**——`STATE_WRITE_PAYLOAD` 同样比较 `word_count` 与实际 `tlast`，不一致时报 `error_payload_early_termination` 并打 `tuser=1`（[rtl/ip_eth_tx.v:L296-L329](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_tx.v#L296-L329)）。注意 `ip_eth_tx` 的状态端口**只有** `busy` 和 `error_payload_early_termination`（[rtl/ip_eth_tx.v:L80-L82](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_tx.v#L80-L82)）——没有 `error_invalid_header`/`error_invalid_checksum`，因为头部是它自己生成的，天然合法。

#### 4.3.4 代码实践

**实践目标**：通过阅读 `ip_eth_tx` 的串行化 `case`，验证「它发出的校验和 == 4.2.4 手算的 0xB6BE」，从而确认 rx/tx 是真正互逆的一对。

**操作步骤**：

1. 打开 [rtl/ip_eth_tx.v:L237-L291](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_tx.v#L237-L291)。
2. 假设给 `ip_eth_tx` 喂入与 4.2.4 完全相同的字段（`s_ip_length=21`、`s_ip_ttl=64`、`s_ip_protocol=0x11`、`s_ip_source_ip=0xc0a80164`、`s_ip_dest_ip=0xc0a80165`、`s_ip_dscp=0`、`s_ip_ecn=0`、`s_ip_flags=2`、`s_ip_fragment_offset=0`、`s_ip_identification=0`）。
3. 逐拍跟踪 `hdr_sum_reg`：
   - ptr=1：`hdr_sum = 0x4500`（第 0 字初值）
   - ptr=2：`hdr_sum = 0x4500 + 0x0015 = 0x4515`
   - ptr=3：`+ 0x0000 = 0x4515`
   - ptr=4：`+ 0x4000 = 0x8515`
   - ptr=5：`+ 0x4011 = 0xC526`
   - ptr=6：`+ 0xC0A8 = 0x1_85CE → 折回 → 0x85CF`
   - ptr=7：`+ 0x0164 = 0x8733`
   - ptr=8：`+ 0xC0A8 = 0x1_47DB → 折回 → 0x47DC`
   - ptr=9：`+ 0x0165 = 0x4941`
4. 在 ptr=0x0A/0x0B，模块发出 `~hdr_sum_reg = ~0x4941 = 0xB6BE`。

**需要观察的现象**：跟踪得到的 `hdr_sum_reg` 末值 `0x4941` 与 4.2.4 手算的 `S` 完全一致；发出的校验和 `0xB6BE` 也与手算值一致。

**预期结果**：`ip_eth_tx` 算出的校验和 = `ip_eth_rx` 判据所需的校验和 = 你手算的 `0xB6BE`。三者一致，证明 rx/tx 构成闭合的「发—收」对。

> 这是纯源码跟踪实践，结果可由本讲的数学推导直接复核，**无需待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ip_eth_tx` 没有 `s_ip_header_checksum` 输入端口，而 `ip_eth_rx` 有 `m_ip_header_checksum` 输出端口？

**答案**：发送方的校验和是「由其余字段算出来的」，`ip_eth_tx` 手握全部字段，自己在串行化时累加即可（发 `~sum`），不需要外部输入。接收方则是「把收到的校验和字段原样上报」，供上层记录或调试，所以 `ip_eth_rx` 把它作为输出。这是收发不对称的合理设计。

**练习 2**：若上游把 `s_ip_length` 设成了一个比实际载荷大的值（比如声明 100 字节、却只发 10 字节载荷就 `tlast`），`ip_eth_tx` 会怎样？

**答案**：在 `STATE_WRITE_PAYLOAD` 里，`tlast` 到来时 `word_count != 1`（还差很多），模块会报 `error_payload_early_termination` 并在末字节打 `tuser=1`（[rtl/ip_eth_tx.v:L308-L313](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_eth_tx.v#L308-L313)）。它不会替你「编造」缺失的载荷，而是把不一致如实标出来交给上层处理。

## 5. 综合实践

把 `ip_eth_rx` 与 `ip_eth_tx` 背靠背连成一个「IP 头重构」小系统，串起本讲全部知识：

1. **接线**：把 `ip_eth_rx` 的并行输出（`m_ip_*`，**不含** `m_ip_header_checksum`）接到 `ip_eth_tx` 的并行输入（`s_ip_*`），`m_ip_payload_axis_*` 直连 `s_ip_payload_axis_*`；以太网头侧同理对接（`m_eth_*` → `s_eth_*`）。
2. **激励**：用 4.2.4 的参数构造一个 IP 帧（payload 1 字节），送入 `ip_eth_rx`。
3. **验证**：从 `ip_eth_tx` 的 `m_eth_payload_axis_*` 抓取输出，重建出 20 字节头部：
   - 各字段（version/ihl/dscp/ecn/length/identification/flags/frag/ttl/protocol/src_ip/dst_ip）与输入一一对应；
   - **校验和字段 = 0xB6BE**（即 `ip_eth_tx` 现算的值，应与 `ip_eth_rx` 透传上来的 `m_ip_header_checksum` 相同——因为输入帧本就是合法帧）；
   - 把重建出的整头再做一次反码求和，结果应为 `0xFFFF`。
4. **进阶**：人为把接给 `ip_eth_tx` 的 `s_ip_ttl` 改成 63（模拟「路由器转发时 TTL 减 1」），重新抓输出——校验和应当**自动更新**（不再是 `0xB6BE`），这正是 `ip_eth_tx` 现算校验和的价值：只要字段变了，校验和自动跟着对。

这个任务实际上就是 IP 层「收—改 TTL—重发」的最小复刻。做完后，你会直观理解为什么校验和由发送侧模块现算、而不是由上层算好传进来。

## 6. 本讲小结

- `ip_eth_rx` 把 20 字节 IPv4 头从字节流解析成并行字段，用一个 `case(hdr_ptr_reg)`（而非宏）逐字节映射；头部固定 20 字节，**只支持 Version=4、IHL=5**（无 IP 选项），否则报 `error_invalid_header`。
- IP 校验和是 **16 位反码求和**：发送方填 `~S`、接收方重算应得 `0xFFFF`；`ip_eth_rx` 用 `add1c16b` 逐字累加并在帧尾比对 `0xFFFF`，不符则报 `error_invalid_checksum`。
- `ip_eth_rx` 用 `ip_length-20` 自算载荷长度，能识别并剥离以太网尾部 padding；实际载荷与声明不符时报 `error_payload_early_termination`，头没收齐就 `tlast` 则报 `error_header_early_termination`。
- `ip_eth_tx` 是 `ip_eth_rx` 的逆运算，**Version/IHL 硬编码为 4/5**，且**没有校验和输入端口**——它在串行化头部的过程中顺带累加反码和，在校验和位置发 `~sum`。
- 两模块严格遵循 u3-l1 确立的「并行头 + AXI-Stream 载荷」接口风格，能和 `eth_axis_rx`/`eth_axis_tx` 及上层 `udp`/`ip` 模块无缝对接。
- 一个可验证的实例：对 TTL=64、protocol=0x11、src=192.168.1.100、dst=192.168.1.101、长度 21 的 IPv4 头，校验和 = `0xB6BE`（手算、`ip_eth_tx` 现算、`ip_eth_rx` 判据三者一致）。

## 7. 下一步学习建议

- 下一篇 **u7-l2（ip/ip_64 核心 IP 模块）** 会把本讲的 `ip_eth_rx`/`ip_eth_tx` 与上一篇的 `arp` 组装成完整的 `ip` 顶层，加入「收发主通路、与 ARP 协作查 MAC、IP 分片」等逻辑。本讲引用的 [rtl/ip.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip.v) 正是下一篇的主角，届时你会看到 `ip_eth_rx_inst`/`ip_eth_tx_inst` 在系统里的真实接线。
- 之后 **u7-l3（ip_complete）** 会进一步把 `ip` + `arp` + `eth_arb_mux` 合成完整可用的 IPv4 协议栈，本讲的「EtherType 不由 ip_eth_rx 检查」正是在那里由复用器统一分发。
- 想提前理解「校验和由谁算」的完整图景，可在学完 u8（UDP 层）后回看本讲——UDP 有自己的载荷校验和（由 `udp_checksum_gen` 算，且含 IP 伪头部），与本讲的 IP 头校验和是两套独立机制。
