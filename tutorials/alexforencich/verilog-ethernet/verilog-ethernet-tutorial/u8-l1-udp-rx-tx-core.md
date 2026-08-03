# UDP 帧接收、发送与 udp 核心模块

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 UDP 头部（源端口、目的端口、长度、校验和）的 8 字节结构，以及它在 IP 载荷中的位置。
- 读懂 `udp_ip_rx` 如何从「IP 头 + IP 载荷流」中拆出 UDP 头并把载荷以 AXI-Stream 流出。
- 读懂 `udp_ip_tx` 如何把 UDP 头字段重新串行化进 IP 载荷，并自动算出 IP 总长度。
- 理解顶层 `udp` 模块如何把收发与（可选的）校验和生成拼成完整的 UDP 核心通路。

本讲建立在 u7-l1（IP 帧接收与发送）之上：UDP 报文就是「IP 头之后的那段载荷」，而 `udp_ip_rx`/`udp_ip_tx` 与 `ip_eth_rx`/`ip_eth_tx`、`eth_axis_rx`/`tx` 完全同构——沿用「并行头字段 + AXI-Stream 载荷 + `hdr_valid`/`hdr_ready` 握手」的接口风格，只是又向上拆了一层。

## 2. 前置知识

### 2.1 UDP 是什么

UDP（User Datagram Protocol，用户数据报协议）是传输层协议中最简单的一个：**无连接、不可靠、不排序**。它几乎只做一件事——给上层应用提供一个「端口号」，让同一台机器上多个进程能共享同一个 IP 地址。相比之下，TCP 要维护连接状态、重传、拥塞控制，复杂得多。正因为简单，UDP 在 FPGA 里可以用纯 RTL 高效实现，本库的 UDP 收发核心就是几百行 Verilog。

### 2.2 端口号

一台机器只有一个 IP 地址，但可能同时跑很多网络程序（网页、DNS、视频流……）。端口号（16 位，范围 0~65535）就是用来区分「这一包数据该交给哪个程序」的。比如 DNS 默认用 53，本库示例程序的 UDP 回显固定监听 **1234**。源端口 + 目的端口共 4 字节，是 UDP 头的核心。

### 2.3 反码校验和（与本讲的关系）

UDP 头里有一个 16 位校验和字段。它覆盖 UDP 头 + UDP 载荷 + IP 伪头部（pseudo header，含源/目的 IP、协议号、UDP 长度），用 16 位反码求和计算。**本讲的 `udp_ip_rx`/`udp_ip_tx` 只搬运这个字段，不计算它**——真正的计算在 `udp_checksum_gen`（u8-l2 专题讲解）。注意：UDP 校验和在 IPv4 里是**可选的**，全 0 表示「不校验」，这也是本库允许旁路校验和生成的原因。

### 2.4 网络字节序（大端）

所有以太网/IP/UDP 字段在链路上都是**大端序（big-endian）**：多字节字段的高字节先发、占更小的字节偏移。本讲的 RTL 把每个 16 位字段拆成两拍字节逐个收发，先到的字节存进 `[15:8]`，后到的存进 `[7:0]`，这与 u3-l1、u7-l1 的处理方式完全一致。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [rtl/udp_ip_rx.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_ip_rx.v) | UDP 帧接收器：IP 帧（并行 IP 头 + AXI 载荷）入，UDP 帧（并行头 + AXI 载荷）出。拆掉 8 字节 UDP 头。 |
| [rtl/udp_ip_tx.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_ip_tx.v) | UDP 帧发送器：UDP 帧入，IP 帧出。把 UDP 头字段串行化进 IP 载荷，并计算 IP 总长度。 |
| [rtl/udp.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp.v) | 8 位 UDP 核心模块（布线层）：实例化 `udp_ip_rx`、可选 `udp_checksum_gen`、`udp_ip_tx`，拼出收发双通路。 |

README 对它们的简述见 [README.md:338-391](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/README.md#L338-L391)。

---

## 4. 核心概念与源码讲解

### 4.1 UDP 头格式与它在 IP 载荷中的位置

#### 4.1.1 概念说明

一个完整的「以太网帧 → IP 包 → UDP 报文」是层层嵌套的：

```
以太网帧 = [以太网头 14B] [IP 头 20B] [IP 载荷]
IP 载荷   = [UDP 头 8B] [UDP 载荷（应用数据）]
UDP 头    = [源端口 2B] [目的端口 2B] [长度 2B] [校验和 2B]
```

关键点：

- **IP 头里的「协议」字段 = 17（0x11）**，表示 IP 载荷是 UDP（TCP 是 6）。本库在 `udp.v` 发送侧把该字段**硬编码为 `8'h11`**。
- **UDP 头只有 8 字节**，是各层头部里最短的。
- **UDP「长度」字段包含头部本身**：`长度 = 8 + 载荷字节数`。例如 5 字节载荷对应长度 13。
- **IP「总长度」字段 = IP 头 + IP 载荷 = 20 + UDP 长度**。本库在发送侧自动算出 `IP 长度 = UDP 长度 + 20`。

`udp_ip_rx` 与 `udp_ip_tx` 正是处理「IP 载荷 ↔ UDP 头 + UDP 载荷」这一层边界的翻译器。它们**不碰**以太网头和 IP 头的内容——那两层字段只是在模块端口上原样透传（pass-through）。

#### 4.1.2 核心流程：字节在 IP 载荷流中的排布

UDP 头 8 字节在 IP 载荷流（`s_ip_payload_axis_tdata`）里按如下顺序逐字节到达（大端序）：

| 字节偏移 | 字段 | 在 16 位寄存器中的位置 |
|---------|------|----------------------|
| 0 | 源端口高字节 | `source_port[15:8]` |
| 1 | 源端口低字节 | `source_port[7:0]` |
| 2 | 目的端口高字节 | `dest_port[15:8]` |
| 3 | 目的端口低字节 | `dest_port[7:0]` |
| 4 | 长度高字节 | `length[15:8]` |
| 5 | 长度低字节 | `length[7:0]` |
| 6 | 校验和高字节 | `checksum[15:8]` |
| 7 | 校验和低字节 | `checksum[7:0]` |
| 8… | UDP 载荷 | （直接流出，不进寄存器） |

例如目的端口 **1234 = 0x04D2**：高字节 `0x04` 在偏移 2，低字节 `0xD2` 在偏移 3。

### 4.2 udp_ip_rx：UDP 头解析与载荷剥离

#### 4.2.1 概念说明

`udp_ip_rx` 是接收侧翻译器。它的输入是上一层（`ip_eth_rx` 或 `ip` 模块）给出的「并行 IP 头 + IP 载荷 AXI-Stream」，输出是「并行 UDP 头 + UDP 载荷 AXI-Stream」。它做的事：

1. 握手收下 IP 头字段（透传，不改）。
2. 从 IP 载荷流的**前 8 个字节**里逐字节提取 UDP 头。
3. 把第 9 个字节开始的载荷原样转发出去。
4. 用 UDP 长度字段精确控制要输出多少个载荷字节，多余的字节（如以太网 padding）丢弃。

#### 4.2.2 核心流程：五状态机

`udp_ip_rx` 用一个 5 状态的有限状态机驱动，状态定义见 [rtl/udp_ip_rx.v:142-147](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_ip_rx.v#L142-L147)：

```
STATE_IDLE
   │  收到 s_ip_hdr_valid && s_ip_hdr_ready → 锁存 IP 头
   ▼
STATE_READ_HEADER            ← 逐字节读 8 字节 UDP 头
   │  hdr_ptr 0→7，每字节存入对应寄存器
   │  在第 8 字节(hdr_ptr==7)置 m_udp_hdr_valid，转去读载荷
   │  若期间提前出现 tlast → error_header_early_termination
   ▼
STATE_READ_PAYLOAD           ← 转发 UDP 载荷，word_count 递减
   │  word_count = udp_length - 8（载荷字节数）
   │  每个 transfer：word_count--，输出一字节
   │  tlast 到且 word_count==1 → 正常结束
   │  tlast 到但 word_count≠1 → error_payload_early_termination
   │  word_count 减到 1 但还没 tlast → 进 LAST 状态（说明有 trailing 字节）
   ▼
STATE_READ_PAYLOAD_LAST      ← 把最后一个有效载荷字节暂存，吞掉 trailing 字节
   │  持续读并丢弃，直到真正的 tlast 才把最后一字节输出
   ▼
STATE_IDLE
```

这里有两个精妙之处：

- **载荷计数** `word_count` 在 `STATE_READ_HEADER` 里每拍都被赋值为 `m_udp_length_reg - 8`（[L283](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_ip_rx.v#L283)）。等头部的「长度」两字节（偏移 4、5）到达后，该值就稳定下来，进入 `STATE_READ_PAYLOAD` 时已正确。
- **trailing 字节处理**：以太网规定最小帧 64 字节，短 UDP 包会被 MAC 层补 padding，导致 IP 载荷比 UDP 长度更长。模块靠 `STATE_READ_PAYLOAD_LAST` 把「最后一个有效载荷字节」暂存（`last_word_data_reg`，[L439-441](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_ip_rx.v#L439-L441)），吞掉所有 padding，直到真正的 `tlast` 才输出该字节——这样输出帧的 `tlast` 与输入帧对齐，载荷又不被 padding 污染。

#### 4.2.3 源码精读

**头部分字节解析**（[rtl/udp_ip_rx.v:290-304](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_ip_rx.v#L290-L304)）：用 `hdr_ptr_reg` 作字节指针，每收到一个有效字节指针加 1，`case` 决定该字节存进哪个字段的哪一半：

```verilog
case (hdr_ptr_reg)
    3'h0: store_udp_source_port_1 = 1'b1;  // 偏移0 → 源端口[15:8]
    3'h1: store_udp_source_port_0 = 1'b1;  // 偏移1 → 源端口[7:0]
    3'h2: store_udp_dest_port_1   = 1'b1;  // 偏移2 → 目的端口[15:8]
    3'h3: store_udp_dest_port_0   = 1'b1;  // 偏移3 → 目的端口[7:0]
    3'h4: store_udp_length_1      = 1'b1;  // 偏移4 → 长度[15:8]
    3'h5: store_udp_length_0      = 1'b1;  // 偏移5 → 长度[7:0]
    3'h6: store_udp_checksum_1    = 1'b1;  // 偏移6 → 校验和[15:8]
    3'h7: begin                            // 偏移7 → 校验和[7:0]
        store_udp_checksum_0    = 1'b1;
        m_udp_hdr_valid_next    = 1'b1;    // 头收齐，向下游宣告
        state_next              = STATE_READ_PAYLOAD;
    end
endcase
```

这跟 u7-l1 里 `ip_eth_rx` 用 `case(hdr_ptr)` 逐字节解析 IP 头是同一套手法（本库的 `*_rx` 模块基本都用这种 `hdr_ptr` + `case` 模式）。

**寄存器写入**（[rtl/udp_ip_rx.v:443-450](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_ip_rx.v#L443-L450)）：大端拆装，先到的字节进高位：

```verilog
if (store_udp_source_port_0) m_udp_source_port_reg[ 7:0] <= s_ip_payload_axis_tdata;
if (store_udp_source_port_1) m_udp_source_port_reg[15:8] <= s_ip_payload_axis_tdata;
if (store_udp_dest_port_0)   m_udp_dest_port_reg[ 7:0]   <= s_ip_payload_axis_tdata;
if (store_udp_dest_port_1)   m_udp_dest_port_reg[15:8]   <= s_ip_payload_axis_tdata;
...
```

**载荷透传与长度校验**（[rtl/udp_ip_rx.v:318-351](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_ip_rx.v#L318-L351)）：进入 `STATE_READ_PAYLOAD` 后，把 `s_ip_payload_axis_tdata` 直接接到输出 `m_udp_payload_axis_tdata_int`，并按 `word_count` 判断是否提前结束或还有 trailing 字节。

**输出级反压解耦**（[rtl/udp_ip_rx.v:454-475](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_ip_rx.v#L454-L475)）：与 `axis_eth_fcs*`、`eth_arb_mux` 同源的「双寄存器 + temp 缓冲」模板——内部产生的载荷先落 `m_udp_payload_axis_tdata_reg`，若下游没准备好（`tready=0`）就暂存到 `temp_*_reg`，从而把上游的 `tready` 与下游解耦，避免反压传导到 IP 层。

#### 4.2.4 代码实践

**实践目标**：构造一个目的端口为 1234 的 UDP 包送入 `udp_ip_rx`，验证解析出的源/目的端口与载荷长度。

由于本库目前**没有**为 `udp_ip_rx`/`udp_ip_tx`/`udp` 提供 cocotb 现代仿真子目录（仅有 myhdl 时代的历史测试 `tb/test_udp_ip_rx.py`，已被 `tox.ini` 的 `--ignore-glob` 排除，详见 u1-l4），本实践采用「源码追踪型 + 参考测试阅读型」两条路线。

**操作步骤（路线 A：纯源码追踪，无需工具链）**

1. 设计一个 UDP 报文：源端口 `0x1234`、目的端口 `1234 = 0x04D2`、载荷 5 字节 `"hello"`（`0x68 65 6C 6C 6F`），不校验（校验和填 0）。
2. 推算 UDP 长度 = 8 + 5 = 13 = `0x000D`。
3. 写出送给 `udp_ip_rx` 的 IP 载荷字节流（前 8 字节是 UDP 头）：

   | 偏移 | 字节 | 含义 |
   |------|------|------|
   | 0 | `0x12` | 源端口高 |
   | 1 | `0x34` | 源端口低 |
   | 2 | `0x04` | 目的端口高 |
   | 3 | `0xD2` | 目的端口低 |
   | 4 | `0x00` | 长度高 |
   | 5 | `0x0D` | 长度低 |
   | 6 | `0x00` | 校验和高 |
   | 7 | `0x00` | 校验和低 |
   | 8–12 | `68 65 6C 6C 6F` | 载荷 |

4. 对照 4.2.3 的 `case(hdr_ptr)` 与寄存器写入逻辑，逐拍推出：
   - 偏移 0 → `m_udp_source_port[15:8]=0x12`；偏移 1 → `[7:0]=0x34` ⇒ `m_udp_source_port = 0x1234`。
   - 偏移 2 → `0x04`，偏移 3 → `0xD2` ⇒ `m_udp_dest_port = 0x04D2 = 1234`。✓
   - 偏移 4、5 ⇒ `m_udp_length = 0x000D = 13`。
   - `word_count = 13 - 8 = 5`，于是输出恰好 5 个载荷字节 `"hello"`。

**预期结果**：`m_udp_source_port=0x1234`、`m_udp_dest_port=0x04D2(1234)`、`m_udp_length=0x000D(13)`，且 `m_udp_payload` 流出 `"hello"` 这 5 字节并带 `tlast`。

**操作步骤（路线 B：参考测试阅读）**

阅读 [tb/test_udp_ip_rx.py:301-334](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/test_udp_ip_rx.py#L301-L334)，看它如何用 `udp_ep.UDPFrame()` 构造一帧（`udp_dest_port=2`）、调用 `.build_ip()` 生成 IP 帧、用 `ip_ep.IPFrameSource` 喂入 DUT、再用 `udp_ep.UDPFrameSink` 收回并断言 `rx_frame == test_frame`。这正是路线 A 的自动化版本。注意它依赖 `myhdl`，属历史遗留测试——**待本地验证**（若环境无 myhdl 则无法直接运行）。

> 说明：若你已配置好 cocotb + iverilog（见 u1-l4），可仿照 `tb/eth_mac_1g/` 的三件套结构，为 `udp_ip_rx` 新建 cocotb testbench，用 `cocotbext-eth` 驱动 IP 头 + AXI 载荷。这属于 u13-l2 的内容，本讲不展开。

#### 4.2.5 小练习与答案

**练习 1**：若把上面的载荷改成 0 字节（纯 UDP 头），`m_udp_length` 与 `word_count` 分别是多少？

**答案**：`m_udp_length = 8`（UDP 长度至少含 8 字节头），`word_count = 8 - 8 = 0`，模块不输出任何载荷字节，收到 `tlast` 即结束。

**练习 2**：若 IP 载荷流在第 5 个字节（偏移 4）就出现 `tlast`，模块会怎样？

**答案**：此时处于 `STATE_READ_HEADER` 且 `hdr_ptr` 还没到 7，`s_ip_payload_axis_tlast` 提前到来会触发 `error_header_early_termination_next = 1`，并撤销 `m_udp_hdr_valid`、回到 `STATE_IDLE`（见 [L306-312](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_ip_rx.v#L306-L312)）。

---

### 4.3 udp_ip_tx：UDP 头组装与 IP 长度计算

#### 4.3.1 概念说明

`udp_ip_tx` 是 `udp_ip_rx` 的逆运算：输入「并行 UDP 头字段 + UDP 载荷 AXI-Stream」，输出「并行 IP 头字段 + IP 载荷 AXI-Stream」。它做的事：

1. 握手锁存 UDP 头字段（端口、长度、校验和）以及透传的 IP 头字段。
2. 在 IP 载荷流前面**串行化**插入 8 字节 UDP 头。
3. 接着把 UDP 载荷原样转发为 IP 载荷。
4. 用锁存的 UDP 长度推算并填好 **IP 总长度** = UDP 长度 + 20。

注意端口命名上的不对称：TX 的并行头输入是 `s_udp_*`（应用侧给的 UDP 字段），输出是 `m_ip_*`（交给下层 IP 模块的 IP 载荷）。

#### 4.3.2 核心流程：五状态机

状态定义见 [rtl/udp_ip_tx.v:139-144](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_ip_tx.v#L139-L144)：

```
STATE_IDLE
   │  收到 s_udp_hdr_valid && s_udp_hdr_ready → store_udp_hdr 锁存全部头字段
   │  同时立刻发出第 0 字节(源端口高字节)，进 WRITE_HEADER
   ▼
STATE_WRITE_HEADER            ← 串行化剩余 7 字节 UDP 头
   │  hdr_ptr 0→7，case 选出该拍要发的字段字节
   │  在第 8 字节(hdr_ptr==7)切换到 WRITE_PAYLOAD
   ▼
STATE_WRITE_PAYLOAD           ← 把 UDP 载荷透传为 IP 载荷
   │  word_count = udp_length - 8 递减，逻辑与 rx 对称
   │  tlast/word_count 判断与 rx 同构
   ▼
STATE_WRITE_PAYLOAD_LAST      ← 处理 trailing / 长度不匹配
   ▼
STATE_IDLE
```

TX 与 RX 在载荷阶段几乎逐行对称（`store_last_word`、`word_count` 递减、长度不匹配报 `error_payload_early_termination`），差别只在头部：RX 是「读入并存」，TX 是「选出并发」。

#### 4.3.3 源码精读

**头字段串行化**（[rtl/udp_ip_tx.v:260-282](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_ip_tx.v#L260-L282)）：`case(hdr_ptr_reg)` 从已锁存的 16 位寄存器里**选高低字节**发出去，顺序与 4.1.2 的字节表严格对应（互逆）：

```verilog
case (hdr_ptr_reg)
    3'h0: m_ip_payload_axis_tdata_int = udp_source_port_reg[15:8];
    3'h1: m_ip_payload_axis_tdata_int = udp_source_port_reg[ 7:0];
    3'h2: m_ip_payload_axis_tdata_int = udp_dest_port_reg[15:8];
    3'h3: m_ip_payload_axis_tdata_int = udp_dest_port_reg[ 7:0];
    3'h4: m_ip_payload_axis_tdata_int = udp_length_reg[15:8];
    3'h5: m_ip_payload_axis_tdata_int = udp_length_reg[ 7:0];
    3'h6: m_ip_payload_axis_tdata_int = udp_checksum_reg[15:8];
    3'h7: begin
        m_ip_payload_axis_tdata_int = udp_checksum_reg[7:0];
        state_next = STATE_WRITE_PAYLOAD;
    end
endcase
```

**IP 总长度自动计算**（[rtl/udp_ip_tx.v:394](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_ip_tx.v#L394)）：锁存头字段时，`m_ip_length` 由 UDP 长度直接推得，应用侧无需提供 IP 长度：

```verilog
m_ip_length_reg <= s_udp_length + 20;   // IP 总长 = UDP 长度 + 20 字节 IP 头
```

> 这解释了为什么 `udp_ip_tx` 的输入端口里有 `s_udp_length` 却**没有** `s_ip_length`——它在模块内部算好了。其余 IP 头字段（version/ihl、TTL、flags、源/目的 IP、校验和等）则是端口透传，由更上层填好。

输出级反压模板与 `udp_ip_rx` 完全同源（[rtl/udp_ip_tx.v:414-436](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_ip_tx.v#L414-L436)），不再赘述。

#### 4.3.4 代码实践

**实践目标**：验证 `udp_ip_tx` 的串行化顺序与 IP 长度计算。

**操作步骤**：

1. 给 `udp_ip_tx` 输入：`s_udp_source_port=0x1234`、`s_udp_dest_port=0x04D2`、`s_udp_length=13`（即 5 字节载荷）、`s_udp_checksum=0`，载荷喂 `"hello"`。
2. 对照 4.3.3 的 `case(hdr_ptr)`，预期 `m_ip_payload` 流出的前 8 字节依次为 `12 34 04 D2 00 0D 00 00`，其后是 `68 65 6C 6C 6F`。
3. 检查 `m_ip_length` 输出。

**预期结果**：`m_ip_length = 13 + 20 = 33 = 0x0021`。若把 `udp_length` 改成 8（零载荷），则 `m_ip_length = 28`。**待本地验证**（同样可参考 `tb/test_udp_ip_tx.py` 的 myhdl 用例）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `udp_ip_tx` 的输入端口没有 `s_ip_protocol`？

**答案**：因为承载 UDP 的 IP 协议号固定为 17（`0x11`），不该让应用侧填错，所以由上层 `udp.v` 在例化时硬编码（见 4.4.3）。

**练习 2**：若应用侧给的 `s_udp_length` 与实际喂入的载荷字节数不一致，会发生什么？

**答案**：与 RX 对称——若 `tlast` 提前到来而 `word_count≠1`，模块置 `m_ip_payload_axis_tuser_int=1` 并报 `error_payload_early_termination`（[L299-304](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_ip_tx.v#L299-L304)）；若载荷多于声明长度，多余字节在 `STATE_WRITE_PAYLOAD_LAST` 被吞掉。

---

### 4.4 udp 核心：收发主通路与校验和集成

#### 4.4.1 概念说明

`udp` 是 8 位（千兆）UDP 核心模块，本质是一个**布线层**（与 `ip`、`ip_complete` 同风格）：它本身几乎没有数据通路逻辑，只把三个子模块连起来——

- **RX 通路**：`udp_ip_rx`，IP 帧 → UDP 帧（直接透传，无额外处理）。
- **TX 通路**：`udp_ip_tx`，UDP 帧 → IP 帧。
- **TX 侧可选的校验和生成**：`udp_checksum_gen`，插在应用输入与 `udp_ip_tx` 之间。

它对外暴露四个方向：`s_ip_*`/`m_ip_*`（连下层 IP 栈）和 `s_udp_*`/`m_udp_*`（连上层应用）。

#### 4.4.2 核心流程：三段式布线

```
                ┌───────────── RX 通路（直连）──────────────┐
  s_ip_*  ───►  │ udp_ip_rx                                  │ ───► m_udp_*
                │  (IP 头透传 + 拆 UDP 头 + 转发 UDP 载荷)     │
                └────────────────────────────────────────────┘

                ┌── TX 通路（可选 checksum_gen + udp_ip_tx）──┐
  s_udp_* ──►  │ [CHECKSUM_GEN_ENABLE? udp_checksum_gen : 直连]│
                │            │                                 │
                │            ▼                                 │ ───► m_ip_*
                │        udp_ip_tx                              │
                │  (锁存头 + 插 UDP 头 + 转发载荷 + 算 IP 长度)  │
                └─────────────────────────────────────────────┘
```

参数（[rtl/udp.v:34-39](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp.v#L34-L39)）：

- `CHECKSUM_GEN_ENABLE = 1`（默认开）：是否在 TX 侧插入 `udp_checksum_gen`。
- `CHECKSUM_PAYLOAD_FIFO_DEPTH = 2048` / `CHECKSUM_HEADER_FIFO_DEPTH = 8`：校验和模块内部的双 FIFO 深度（因算校验和需要先看完整个载荷，故要缓存）。

#### 4.4.3 源码精读

**RX 直接透传**：`udp_ip_rx` 的所有端口与 `udp` 模块的 `s_ip_*`/`m_udp_*` 一一对应连接（[rtl/udp.v:194-254](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp.v#L194-L254)），中间没有任何逻辑。

**TX 侧的 `generate` 选择**（[rtl/udp.v:256-353](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp.v#L256-L353)）：

- 若 `CHECKSUM_GEN_ENABLE=1`：实例化 `udp_checksum_gen`，吃进 `s_udp_*`，吐出内部 `tx_udp_*`（同时填好 `udp_length`、`udp_checksum`、`ip_length`），再喂给 `udp_ip_tx`。
- 若 `CHECKSUM_GEN_ENABLE=0`：用一串 `assign` 把 `s_udp_*` 直连到 `tx_udp_*`，此时应用侧必须自己提供正确的 `udp_length` 与 `udp_checksum`（校验和可填 0 表示不校验）。

**协议号硬编码**（[rtl/udp.v:373](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp.v#L373)）：例化 `udp_ip_tx` 时把 `.s_ip_protocol(8'h11)` 写死，这正是 4.3.5 练习 1 的答案——`udp` 模块输出的 IP 包必然标记为 UDP：

```verilog
.s_ip_protocol(8'h11),   // UDP 协议号 = 17
```

注意：因为协议号在 `udp.v` 这层就被定死，`udp` 模块的 TX 输入端口里**没有** `s_udp_ip_protocol`，只有 version/ihl/dscp/ecn 等其他 IP 头字段。

**校验和模块的输出悬空**：例化 `udp_checksum_gen` 时，`.m_ip_length()` 与 `.m_ip_protocol()` 留空（[L301](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp.v#L301)、[L306](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp.v#L306)）——因为 IP 长度由 `udp_ip_tx` 自己算（+20），协议号由 `udp.v` 写死，校验和模块算出的这两个值不需要往下传。

#### 4.4.4 代码实践

**实践目标**：对比 `udp` 模块在 `CHECKSUM_GEN_ENABLE=1` 与 `=0` 两种配置下的 TX 数据通路差异。

**操作步骤**：

1. 打开 [rtl/udp.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp.v)，定位 L256-353 的 `generate` 块。
2. 跟踪 `s_udp_payload_axis_tdata` 这一根信号在两种配置下的去向：
   - `=1`：`s_udp_payload_axis_tdata` → `udp_checksum_gen.s_udp_payload_axis_tdata` → `tx_udp_payload_axis_tdata` → `udp_ip_tx.s_udp_payload_axis_tdata` → `m_ip_payload_axis_tdata`。
   - `=0`：`s_udp_payload_axis_tdata` 经 `assign tx_udp_payload_axis_tdata = s_udp_payload_axis_tdata;` 直接连到 `udp_ip_tx`。
3. 思考：若 `=0`，应用侧必须保证 `s_udp_length` 正确（否则 `udp_ip_tx` 算出的 `m_ip_length` 也错），且 `s_udp_checksum` 合法（可填 0）。

**预期结果**：理解「校验和生成是可旁路的」这一设计——对延迟敏感或上层已自带校验的场景可关闭，换取更短流水线。**待本地验证**（可用仿真对比两种配置下 `m_ip_length`、`m_udp_checksum` 的输出）。

#### 4.4.5 小练习与答案

**练习 1**：`udp` 模块对外有 `m_ip_protocol` 输出端口，它的值由谁决定？应用侧能改吗？

**答案**：由 `udp.v` 在 L373 硬编码为 `8'h11`，应用侧**不能**通过端口改它——这是设计上的约束，确保该模块只发 UDP 包。

**练习 2**：为什么 `udp_ip_rx` 在 `udp` 模块里是「直接透传」，而 TX 侧却插了一个可选模块？

**答案**：接收侧只需拆头，无需额外计算（校验和是可选的，且 `udp_ip_rx` 只搬运校验和字段不校验）；发送侧若要生成正确的 UDP 长度/校验和，需要先把整个载荷过一遍（算反码和），故插入 `udp_checksum_gen` 并用 FIFO 缓存载荷，这一步代价较高，所以设为可选。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「应用 → UDP → IP 载荷 → 还原回 UDP」的环回追踪。

**任务**：假设应用想发送一个 UDP 报文：源端口 `0x1234`、目的端口 `1234`、载荷 `"abc"`（3 字节），不校验。

1. **推算字段**：UDP 长度 = `0x000B`（11）；若经 `udp_ip_tx`，IP 总长度 = `11 + 20 = 31 = 0x001F`。
2. **写出 `udp_ip_tx` 输出的 IP 载荷字节流**：`12 34 04 D2 00 0B 00 00 61 62 63`（8 字节头 + `"abc"`）。
3. **把这串字节作为 IP 载荷喂回 `udp_ip_rx`**（RX/TX 互逆），按 4.2 的 `case(hdr_ptr)` 逐拍追踪：
   - `m_udp_source_port = 0x1234`、`m_udp_dest_port = 0x04D2`、`m_udp_length = 0x000B`。
   - `word_count = 11 - 8 = 3`，载荷流出 `"abc"`。
4. **校验自洽**：TX 发出的字节流经 RX 还原后，头部字段与载荷完全一致——这验证了 `udp_ip_rx`/`udp_ip_tx` 是一对闭合的「发—收」对（与 u7-l1 的 `ip_eth_rx`/`ip_eth_tx` 关系同构）。

**延伸思考**：若把 `udp` 模块的 `CHECKSUM_GEN_ENABLE` 设为 0，上述字节流里的校验和字段（偏移 6、7）必须由谁填？答：应用侧（填 0 表示不校验）。这正是「可旁路」设计带来的责任转移。

## 6. 本讲小结

- UDP 报文 = IP 载荷，前 8 字节是 UDP 头（源端口 2 + 目的端口 2 + 长度 2 + 校验和 2），其后是应用载荷；UDP「长度」含头，IP「总长度」= UDP 长度 + 20。
- `udp_ip_rx` 用 `hdr_ptr` + `case` 逐字节拆出 UDP 头（大端拆装），并用 `word_count = udp_length - 8` 精确控制载荷字节数，靠 `STATE_READ_PAYLOAD_LAST` 吞掉以太网 padding 等 trailing 字节。
- `udp_ip_tx` 是 RX 的逆运算，用 `case(hdr_ptr)` 把锁存的头字段串行化插入 IP 载荷，并自动算出 `m_ip_length = udp_length + 20`。
- 顶层 `udp` 是布线层：RX 直接透传 `udp_ip_rx`，TX 在 `udp_ip_tx` 前按 `CHECKSUM_GEN_ENABLE` 选插 `udp_checksum_gen` 或直连，并把 IP 协议号硬编码为 `8'h11`。
- 收发子模块沿用全库统一的「并行头字段 + AXI-Stream 载荷 + `hdr_valid`/`hdr_ready` 握手」接口风格与「双寄存器 + temp」输出级反压模板。

## 7. 下一步学习建议

- **u8-l2（UDP 校验和生成）**：精读 `udp_checksum_gen`，理解它如何用双 FIFO 缓存载荷、同时算出 UDP 长度、IP 长度与含 IP 伪头部的反码校验和——补上本讲刻意留白的校验和计算环节。
- **u8-l3（udp_complete 顶层 UDP 协议栈）**：看 `udp_complete` 如何把本讲的 `udp` 与 `ip_complete` 拼成完整可用的 UDP 栈，端到端跑通「以太网帧 → 应用」。
- **并行阅读 64 位变体**：`rtl/udp_ip_rx_64.v`、`rtl/udp_64.v` 在 10G/25G 场景下与 8 位版逻辑同构，仅位宽与 `tkeep` 不同，学完本讲后可快速对照。
