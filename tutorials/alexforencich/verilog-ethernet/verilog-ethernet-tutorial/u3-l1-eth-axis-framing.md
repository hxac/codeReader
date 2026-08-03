# eth_axis_rx/tx：以太网帧解析与封装

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清一个以太网帧的头部（14 字节）由哪些字段组成，以及它们在线上的字节顺序。
- 看懂 `eth_axis_rx` 如何把一根 AXI-Stream「拆」成「并行头部字段 + 独立的 payload AXI-Stream」两路输出，并掌握它的 `hdr_valid`/`hdr_ready` 握手语义。
- 看懂 `eth_axis_tx` 如何把「并行头部字段 + payload AXI-Stream」「合」回一根完整的 AXI-Stream 帧。
- 理解这两个模块在真实系统里所处的位置：MAC 与协议栈（ARP/IP/UDP）之间的「成帧层」。

本讲是整个协议栈系列（ARP → IPv4 → UDP）的入口——后续每一层（`arp_eth_rx`、`ip_eth_rx`、`udp_ip_rx`）都是在这套「头部并行 + payload 流」的接口约定上继续往上拆包。

## 2. 前置知识

### 2.1 AXI-Stream 握手（来自 u1-l3）

回顾核心规则：仅当某时钟沿 `tvalid` 与 `tready` 同时为 1，一拍数据（beat）才真正传走；源在握手前须保持 `tvalid`/`tdata` 稳定；帧尾由 `tlast` 标记；`tuser` 在末拍为 1 表示坏帧。本讲里 `eth_axis_rx`/`eth_axis_tx` 的输入输出都是标准 AXI-Stream，全部遵循这套规则。

### 2.2 以太网帧长什么样

一个最基础的以太网帧（不含前导码和 FCS）是这样的：

| 偏移(字节) | 字段 | 长度 |
|------------|------|------|
| 0–5 | 目的 MAC（Destination MAC） | 6 字节 |
| 6–11 | 源 MAC（Source MAC） | 6 字节 |
| 12–13 | EtherType / 长度（Type） | 2 字节 |
| 14… | 载荷（Payload） | 可变 |

头部共 **14 字节**，后面才是上层协议关心的载荷。`0x0800` 表示载荷是 IPv4，`0x0806` 表示 ARP，`0x86DD` 表示 IPv6。本讲的两个模块只负责「拆/装这 14 字节头」，**不**碰 FCS（那是上一讲 `u2-l2` 的 `axis_eth_fcs*` 的职责）。

### 2.3 为什么要「拆成两路」

MAC 吐出来/吃进去的是一根连续的 AXI-Stream 帧（头 + 载荷混在一起）。但上层协议（ARP、IP）第一件事就是读头部来决定「这包要不要我处理、要送到哪个子模块」。如果把头部和载荷混在一根流里，下游就得自己数字节、自己缓存前 14 字节，很麻烦。所以成帧层把头部「提升」成并行信号（`dest_mac`、`src_mac`、`type` 一次性可见），载荷则原样以 AXI-Stream 继续传递——这就是本库后续所有协议模块共用的接口风格。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [rtl/eth_axis_rx.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_rx.v) | 接收成帧：AXI-Stream 帧 → 并行头部 + payload AXI-Stream |
| [rtl/eth_axis_tx.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_tx.v) | 发送成帧：并行头部 + payload AXI-Stream → AXI-Stream 帧 |
| [tb/eth_axis_rx/test_eth_axis_rx.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_axis_rx/test_eth_axis_rx.py) | rx 的 cocotb 测试：用 scapy 构帧、拆帧、再拼回断言 |
| [tb/eth_axis_tx/test_eth_axis_tx.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_axis_tx/test_eth_axis_tx.py) | tx 的 cocotb 测试：用 scapy 拆出头部+载荷、送入 tx、断言重组帧 |
| [example/Arty/fpga/rtl/fpga_core.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v) | 真实系统用法：MAC ↔ eth_axis_rx/tx ↔ udp_complete |

## 4. 核心概念与源码讲解

### 4.1 以太网帧头结构与「两条输出」设计

#### 4.1.1 概念说明

`eth_axis_rx` 和 `eth_axis_tx` 是一对互逆的模块，它们处理的都是「14 字节以太网头 + payload」这一层：

- `eth_axis_rx`（接收，AXI in → Ethernet frame out）：吃进一根完整 AXI-Stream 帧，把前 14 字节解析成 3 个并行字段，剩下的字节作为另一根 AXI-Stream（payload）继续输出。
- `eth_axis_tx`（发送，Ethernet frame in → AXI out）：吃进 3 个并行头部字段 + 一根 payload AXI-Stream，把它们合并成一根完整 AXI-Stream 帧输出。

二者的端口都围绕「头部并行 + payload 流」展开。看端口分组就能直观感受到这种拆分。

#### 4.1.2 核心流程

整体数据流（以默认 8 位 `DATA_WIDTH` 为例）：

```
           eth_axis_rx                                   eth_axis_tx
完整帧 ──► [解析14B头] ──► dest_mac/src_mac/type (并行)
AXI-Stream               ──► payload (AXI-Stream)  ──► [拼装14B头] ──► 完整帧
                                                              AXI-Stream
```

注意头部走的是「并行寄存器 + 握手」，载荷走的是「标准 AXI-Stream」。两路在时间上交叠：头部一旦凑齐就并行送出，payload 紧随其后流式输出。

#### 4.1.3 源码精读

`eth_axis_rx` 的端口分成三组：AXI 输入、以太网头部并行输出、payload AXI-Stream 输出，外加状态信号：

[rtl/eth_axis_rx.v:61-71](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_rx.v#L61-L71) —— 头部三字段 + payload 流分成两组端口输出：`m_eth_hdr_valid/ready`、`m_eth_dest_mac`(48 位)、`m_eth_src_mac`(48 位)、`m_eth_type`(16 位) 是头部；`m_eth_payload_axis_*` 是载荷流。

`eth_axis_tx` 的端口正好镜像：头部 + payload 流是输入，合并后的 AXI-Stream 是输出：

[rtl/eth_axis_tx.v:51-71](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_tx.v#L51-L71) —— 输入侧 `s_eth_hdr_valid/ready` + 三头部字段 + `s_eth_payload_axis_*`，输出侧 `m_axis_*`。

两个模块都把头部固定为 14 字节，并据此推导几个关键常量（以 rx 为例，tx 完全相同）：

[rtl/eth_axis_rx.v:80-88](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_rx.v#L80-L88) —— 定义 `BYTE_LANES`（每拍多少字节）、`HDR_SIZE=14`、`CYCLE_COUNT`（收齐头部需要的拍数）、`OFFSET`（头部最后一拍里「头/载荷」的字节分界）。

其中：

\[
\text{CYCLE\_COUNT} = \left\lceil \frac{14}{\text{BYTE\_LANES}} \right\rceil, \qquad \text{OFFSET} = 14 \bmod \text{BYTE\_LANES}
\]

对默认 8 位（`BYTE_LANES=1`）：`CYCLE_COUNT=14`，`OFFSET=0`——每拍 1 字节，14 拍收齐头部，且头部恰好结束在拍边界上，不需要跨拍拼接，逻辑最简单。本讲后续讲解以这种最直观的 8 位情形为主线；宽位宽下的跨拍拼接（`OFFSET≠0`）在 4.2/4.3 末尾作为进阶补充。

> 文件头部的注释也明确写了这两个模块各自的方向与字段定义，可对照阅读：[rtl/eth_axis_rx.v:100-110](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_rx.v#L100-L110)。

#### 4.1.4 代码实践：读懂端口分组

1. **实践目标**：建立「头部并行 + payload 流」的接口直觉。
2. **操作步骤**：打开 `rtl/eth_axis_rx.v` 与 `rtl/eth_axis_tx.v` 的端口声明，用三种颜色分别标出「时钟/复位」「头部信号」「payload AXI-Stream 信号」。
3. **观察现象**：你会发现 rx 的头部信号全带 `m_eth_` 前缀（输出），tx 的全带 `s_eth_` 前缀（输入）；payload 流则都带 `_payload_axis_` 中缀。
4. **预期结果**：能口述出「rx 把头拆出来往外送，tx 把外面的头收进来拼上」。
5. 运行结果：待本地验证（纯阅读型实践，无需仿真）。

#### 4.1.5 小练习与答案

**练习 1**：为什么头部要做成并行字段，而 payload 仍是 AXI-Stream，而不是把整个帧都做成并行字段？

> **答案**：头部短（14 字节）且固定，下游需要一次性「看全」才能快速路由（比如按 EtherType 分发）；payload 长度可变、体积大，做成并行不现实，用流式接口才能复用现成的 AXI-Stream 反压与 FIFO 基础设施。

**练习 2**：`m_eth_dest_mac` 是 48 位，线上第一个字节（偏移 0）对应它的哪一段？

> **答案**：对应最高字节 `[47:40]`（即 `[5*8 +: 8]`），MAC 在线上按大端序传输，第一个字节是地址的最高字节。

---

### 4.2 eth_axis_rx：头部握手与逐字节提取（以太网帧头解析）

#### 4.2.1 概念说明

`eth_axis_rx` 内部是一个简单的两段状态机：**读头（read_eth_header）→ 读载荷（read_eth_payload）**，配合一个字节/字指针 `ptr` 记录「当前收到的头部位置」。它的关键设计有两点：

1. **逐字节提取头部**：每收到一拍，就根据 `ptr` 把对应字节填入 `dest_mac`/`src_mac`/`type` 的对应位段。头部凑齐后，把 `m_eth_hdr_valid` 拉高一拍并保持，通知下游「头来啦」。
2. **头部握手反压输入**：头部送出后会被「按住」（保持 `hdr_valid`）直到下游用 `hdr_ready` 把它取走；在下游取走头部之前，rx 不再向上游 `tready`，从而把整条接收通路卡住，避免载荷在头部还没被消费时就冲过去。

#### 4.2.2 核心流程

```
状态: read_eth_header=1, read_eth_payload=0, ptr=0
  ├─ 每收到 1 拍 (tvalid&tready):
  │     • 按 ptr 把字节填入对应头部字段; ptr++
  │     • 若 ptr 到达头部最后一字节 且 本帧未在此提前结束(tlast):
  │           m_eth_hdr_valid <= 1   // 头部凑齐,通知下游
  │           切到 read_eth_payload=1
  ├─ 若 tlast 在头部凑齐之前到来:
  │     error_header_early_termination <= 1   // 帧太短,头都没收全
  │     复位,回到读头状态等待下一帧
状态: read_eth_payload=1
  └─ 把 (移位后的) payload 直接接到 m_eth_payload_axis_* 输出
        • 遇到 tlast: 复位 ptr, 回到 read_eth_header=1, 等下一帧
```

头部握手的耦合点：`s_axis_tready`（对上游的握手）同时取决于三件事——下游 payload 通路就绪、内部移位就绪、**头部已被下游消费**（`!m_eth_hdr_valid || m_eth_hdr_ready`）。

#### 4.2.3 源码精读

状态机寄存器与指针定义在此：

[rtl/eth_axis_rx.v:113-115](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_rx.v#L113-L115) —— `read_eth_header_reg`/`read_eth_payload_reg` 是两段状态，`ptr_reg` 是头部字节/字指针。

头部字段是普通的并行寄存器，凑齐后由 `m_eth_hdr_valid_reg` 标记有效性：

[rtl/eth_axis_rx.v:122-125](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_rx.v#L122-L125) —— `m_eth_dest_mac_reg`/`m_eth_src_mac_reg`/`m_eth_type_reg` 与 `m_eth_hdr_valid_reg`。

逐字节提取的核心是一个 `_HEADER_FIELD_` 宏，它把「偏移 offset 处的字节」写到「某字段对应位段」，并且只在当前拍确实包含该字节（`tkeep` 有效）时才写：

[rtl/eth_axis_rx.v:219-237](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_rx.v#L219-L237) —— 14 行宏调用，分别把偏移 0–5 写入 dest_mac（大端，最高字节在前）、6–11 写入 src_mac、12–13 写入 type。注意 type 的高字节在偏移 12、低字节在偏移 13，与线上大端序一致。

头部凑齐的判定与状态切换：

[rtl/eth_axis_rx.v:239-245](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_rx.v#L239-L245) —— 当 `ptr` 到达头部最后一字节（`13/BYTE_LANES`）且这帧没有在这里就结束（`!shift_axis_tlast`）时，置 `hdr_valid=1` 并切到读载荷状态。

头部握手与对上游的反压耦合：

[rtl/eth_axis_rx.v:193](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_rx.v#L193) —— `s_axis_tready_next` 同时要求 payload 通路就绪、内部移位就绪、且头部已被下游消费（`!m_eth_hdr_valid || m_eth_hdr_ready`）。

[rtl/eth_axis_rx.v:198](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_rx.v#L198) —— `m_eth_hdr_valid_next` 默认保持原值，仅当下游拉 `m_eth_hdr_ready` 时才清零——也就是头部会一直「挂着」直到被取走。

帧过短（头部没收齐就遇到 `tlast`）的错误上报：

[rtl/eth_axis_rx.v:259-263](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_rx.v#L259-L263) —— 若 `tlast` 到来时仍处在「读头」状态，置 `error_header_early_termination`，并在 [rtl/eth_axis_rx.v:265-268](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_rx.v#L265-L268) 复位 `ptr`、回到读头状态等下一帧。

> **进阶（宽位宽）**：当 `DATA_WIDTH>8` 时 `OFFSET=14 mod BYTE_LANES` 通常非零，意味着「头部最后一拍」里同时含有头部末尾字节和 payload 首字节。模块用一个 `save_*` 寄存器 + 移位逻辑把这拍「跨界的两个半字」正确拆给头部和 payload：

[rtl/eth_axis_rx.v:162-186](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_rx.v#L162-L186) —— `OFFSET==0` 时直接透传；否则用 `{s_axis_tdata, save_axis_tdata_reg} >> (OFFSET*8)` 做跨拍字节对齐。初学者可先只关注 `OFFSET==0` 分支。

#### 4.2.4 代码实践：阅读 rx 测试，理解「拆完再拼回」

1. **实践目标**：通过现有测试理解 rx 的输出如何被消费，并确认头部字段语义。
2. **操作步骤**：
   1. 打开 [tb/eth_axis_rx/test_eth_axis_rx.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_axis_rx/test_eth_axis_rx.py)。
   2. 关注 `TB.recv()`（约 86–98 行）：它分别从 `header_sink` 取头部、从 `payload_sink` 取载荷，然后用 `dest_mac.integer.to_bytes(6,'big')`、`src_mac`、`type` 重新拼出一个 scapy `Ether()`，再 `Ether(bytes(rx_pkt))` 解析回来。
   3. 运行测试：`cd tb/eth_axis_rx && make`（需先装好 cocotb + iverilog，参见 u1-l4）。
3. **观察现象**：测试会对 `1..127`、`512`、`1500`、`9200` 等多种长度、并叠加随机 idle/backpressure 的帧断言 `bytes(rx_pkt) == bytes(test_pkt)`（即拆完再拼回与原帧逐字节相等）。
4. **预期结果**：所有用例通过；若失败，多半是头部字节序理解错了或 payload 丢失了字节。
5. 运行结果：待本地验证（取决于本地是否已配置 cocotb/iverilog 工具链）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `m_eth_hdr_ready` 一直拉低（下游永不取头部），会发生什么？

> **答案**：由于 [rtl/eth_axis_rx.v:193](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_rx.v#L193) 中 `s_axis_tready_next` 含有 `(!m_eth_hdr_valid || m_eth_hdr_ready)`，头部一旦送出且未被取走，`s_axis_tready` 会被压低，整条输入通路停摆，后续帧进不来。

**练习 2**：`error_header_early_termination` 在什么情况下置位？

> **答案**：当一帧的 `tlast` 在头部 14 字节凑齐之前就到来（比如某帧总长 < 14 字节），见 [rtl/eth_axis_rx.v:259-263](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_rx.v#L259-L263)。这是一种畸形帧告警。

**练习 3**：8 位模式下，收齐头部需要几拍？为什么 `OFFSET==0`？

> **答案**：14 拍（`CYCLE_COUNT=14`）。因为 `BYTE_LANES=1`，14 正好是 1 的整数倍，`14 mod 1 = 0`，头部恰好在拍边界结束，无需跨拍拼接。

---

### 4.3 eth_axis_tx：根据头部字段重建完整帧（帧封装重建）

#### 4.3.1 概念说明

`eth_axis_tx` 是 rx 的逆过程。它的输入是「并行头部 + payload AXI-Stream」，输出是合并后的完整帧。内部同样是两段状态机：**发头（send_eth_header）→ 发载荷（send_eth_payload）**。

关键点：

1. **锁存头部**：tx 只在握手成功的瞬间（`s_eth_hdr_ready && s_eth_hdr_valid`）把三个头部字段锁进内部寄存器，之后即使外部头部信号变化也不受影响，这样发送一帧期间头部是稳定的。
2. **按指针把头部字节「注入」输出流**：发头阶段，根据 `ptr` 把头部寄存器里对应字节塞进 `m_axis_tdata` 的对应字节通道，同时把对应 `tkeep` 位置 1；头部发完无缝衔接到 payload 流。
3. **只在空闲时接收新头部**：`s_eth_hdr_ready` 仅当 tx 既不在发头也不在发载荷时才为 1，保证一次只处理一帧。

#### 4.3.2 核心流程

```
状态: 空闲 (send_eth_header=0, send_eth_payload=0)
  └─ s_eth_hdr_ready=1, 等头部
     • 握手成功 (hdr_ready&hdr_valid): 锁存 dest/src/type, send_eth_header=1, ptr=0
状态: 发头 (send_eth_header=1)
  ├─ 每发一拍: 按 ptr 把头部字节注入 m_axis_tdata 对应通道, tkeep 位置 1, ptr++
  └─ ptr 到达头部最后一拍: 切到 send_eth_payload=1
状态: 发载荷 (send_eth_payload=1)
  └─ 把 payload AXI-Stream (经移位对齐) 直接送上 m_axis_*
     • 遇到 payload 的 tlast: 回到空闲, 准备发下一帧
```

#### 4.3.3 源码精读

头部寄存器与 `store_eth_hdr` 控制信号：

[rtl/eth_axis_tx.v:122-127](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_tx.v#L122-L127) —— `eth_dest_mac_reg`/`eth_src_mac_reg`/`eth_type_reg` 是锁存后的头部。

头部握手与锁存时机：

[rtl/eth_axis_tx.v:203-209](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_tx.v#L203-L209) —— 当 `s_eth_hdr_ready && s_eth_hdr_valid` 时置 `store_eth_hdr=1`、清 `ptr`、进入发头状态。锁存动作在时序块里完成：

[rtl/eth_axis_tx.v:290-294](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_tx.v#L290-L294) —— `store_eth_hdr` 有效时把外部三字段打入内部寄存器。

按指针注入头部字节的 `_HEADER_FIELD_` 宏（与 rx 的宏互逆：rx 是「读出」，tx 是「写入」）：

[rtl/eth_axis_tx.v:244-263](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_tx.v#L244-L263) —— 当 `ptr` 落在偏移所在的拍时，把字段字节写入 `m_axis_tdata_int` 的对应字节通道，并把 `m_axis_tkeep_int` 对应位置 1。头部字节顺序与 rx 一致（dest MAC 最高字节在偏移 0）。

发头到发载荷的无缝切换：

[rtl/eth_axis_tx.v:265-271](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_tx.v#L265-L271) —— `ptr` 到达头部最后一拍时，拉起 payload 通路就绪并切换状态；同时 `send_eth_header_next <= 0` 结束发头。

空闲判定（何时才肯接收新头部）：

[rtl/eth_axis_tx.v:277](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_tx.v#L277) —— `s_eth_hdr_ready_next = !(send_eth_header_next || send_eth_payload_next)`，即只要还在发头或发载荷，就拒绝接收新头部。

> **进阶（宽位宽）**：tx 同样用 `save_*` + 移位处理 `OFFSET≠0` 的情形，方向与 rx 相反——它是把「payload 首字节」对齐到「头部末尾字节」之后的位置：

[rtl/eth_axis_tx.v:158-182](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_tx.v#L158-L182) —— `OFFSET==0` 时透传；否则 `{s_*, save_*} >> ((KEEP_WIDTH-OFFSET)*8)` 把头尾拼到同一拍输出。初学先看 `OFFSET==0`。

#### 4.3.4 代码实践：阅读 tx 测试，理解「拆出再喂入」

1. **实践目标**：确认 tx 能把「并行头部 + payload 流」精确重组为原帧。
2. **操作步骤**：
   1. 打开 [tb/eth_axis_tx/test_eth_axis_tx.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_axis_tx/test_eth_axis_tx.py)。
   2. 关注 `TB.send()`（约 84–91 行）：它从一个 scapy `Ether` 包里取出 `dst`/`src`/`type`，构造 `EthHdrTransaction` 发给 `header_source`，再把 `pkt[Ether].payload` 的裸字节发给 `payload_source`。
   3. `TB.recv()` 直接从 `m_axis` 收一整帧，用 scapy 解析回来。
   4. 运行测试：`cd tb/eth_axis_tx && make`。
3. **观察现象**：测试对 `1..127`、`512`、`1500`、`9200` 等长度、叠加 idle/backpressure，断言 `bytes(rx_pkt) == bytes(test_pkt)`。
4. **预期结果**：所有用例通过；重组帧的目的/源 MAC、EtherType、载荷与输入完全一致。
5. 运行结果：待本地验证（取决于本地工具链）。

#### 4.3.5 小练习与答案

**练习 1**：tx 在发送一帧的过程中，如果外部 `s_eth_dest_mac` 等信号发生变化，会影响输出吗？

> **答案**：不会。tx 在头部握手瞬间把字段锁进 `eth_dest_mac_reg` 等内部寄存器（[rtl/eth_axis_tx.v:290-294](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_tx.v#L290-L294)），之后输出用的是锁存值。

**练习 2**：为什么 `s_eth_hdr_ready` 在发头/发载荷期间必须为 0？

> **答案**：保证一次只处理一帧。若期间又接收新头部，会覆盖正在使用的头部寄存器或打乱头/载荷对齐。见 [rtl/eth_axis_tx.v:277](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_tx.v#L277)。

**练习 3**：tx 的 `_HEADER_FIELD_` 宏与 rx 的同名宏在「读写方向」上是什么关系？

> **答案**：互逆。rx 的宏是「从 `s_axis_tdata` 读出某字节写入头部字段」；tx 的宏是「从头部字段读出某字节写入 `m_axis_tdata_int` 的对应通道」。字节偏移与位段定义完全对称，保证 rx 拆出的字段能被 tx 原样拼回。

---

### 4.4 两个模块共用的「双寄存器 + temp」反压输出级

#### 4.4.1 概念说明

rx 和 tx 的输出 AXI-Stream（rx 的 `m_eth_payload_axis_*`、tx 的 `m_axis_*`）都套了同一套「输出弹性缓冲」模板：一个主输出寄存器 + 一个 `temp` 暂存寄存器。它的作用是——即使下游瞬时未就绪（`tready=0`），模块内部也能多缓存一拍，从而让上游的 ready 计算可以「提前一拍」给出（`*_tready_int_early`），降低组合路径深度、提高频率。这套模板在本库的 `axis_eth_fcs*`、`axis_gmii_*` 等众多模块里反复出现（u2-l2 已见过同类结构）。

#### 4.4.2 核心流程

```
内部产生一拍数据 (tvalid_int):
  if 下游 tready 或 主寄存器空:  直接进主输出寄存器
  else:                          进 temp 暂存
当主寄存器被下游取走后:           把 temp 里的那一拍搬进主寄存器
对上游的 ready (early): 下游 tready 或 (主空 且 temp 空)
```

#### 4.4.3 源码精读

以 tx 为例（rx 的 payload 输出级结构完全对应）：

[rtl/eth_axis_tx.v:344-345](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_tx.v#L344-L345) —— `m_axis_tready_int_early`：当下游就绪或两寄存器皆空时，提前一拍告诉内部「可以再送一拍来」。

[rtl/eth_axis_tx.v:356-368](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_axis_tx.v#L356-L368) —— 选择把内部数据送进主寄存器还是 temp 暂存，或把 temp 搬进主寄存器。

> 这套结构不是本讲的重点，但读懂它能解释一个常见疑惑：「为什么模块内部算出的 `tvalid_int` 不会因为下游卡一拍就丢数据？」——因为有 temp 兜底。后续讲 `axis_gmii_rx`/`axis_xgmii_rx` 时还会再遇到。

## 5. 综合实践：背靠背连接 rx 与 tx

把 `eth_axis_rx` 与 `eth_axis_tx` 背靠背连起来（rx 的输出喂给 tx 的输入），就构成一个「拆包→装包」的回环。这正是 Arty 参考设计里 MAC 与协议栈之间的真实连线：MAC 出来的 AXI-Stream 帧 → `eth_axis_rx` 拆成头部+载荷 → 交给上层（在 Arty 里是 `udp_complete`）→ 处理后的头部+载荷 → `eth_axis_tx` 装回完整帧 → 送回 MAC。

**任务**：搭建 rx→tx 回环，输入一帧 `EtherType=0x0800` 的数据，验证目的/源 MAC、类型被正确透传，载荷完整。

### 5.1 参考真实连线

先看 Arty 是怎么连的（这是最权威的范本）：

[example/Arty/fpga/rtl/fpga_core.v:371-420](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v#L371-L420) —— `eth_axis_rx_inst` 把 MAC 的 `rx_axis_*` 拆成 `rx_eth_hdr_*` + `rx_eth_payload_axis_*`；`eth_axis_tx_inst` 把 `tx_eth_hdr_*` + `tx_eth_payload_axis_*` 装成 `tx_axis_*` 送回 MAC。中间的 `rx_eth_*` → `udp_complete` → `tx_eth_*` 就是上层协议的位置。

在我们的回环里，把「上层协议」短路掉：直接把 rx 的头部与 payload 接到 tx 的头部与 payload。

### 5.2 操作步骤

1. **新建一个 testbench**（示例代码，非项目原有文件），把 rx 的输出连到 tx 的输入：

   ```verilog
   // 示例代码：rx -> tx 背靠背回环 (8 位)
   eth_axis_rx #(.DATA_WIDTH(8)) rx_inst (
       .clk(clk), .rst(rst),
       .s_axis_tdata(rx_in_tdata), .s_axis_tvalid(rx_in_tvalid),
       .s_axis_tready(rx_in_tready), .s_axis_tlast(rx_in_tlast),
       .s_axis_tuser(rx_in_tuser),
       // 头部 -> 直接连到 tx 的头部输入
       .m_eth_hdr_valid(tx_hdr_valid), .m_eth_hdr_ready(tx_hdr_ready),
       .m_eth_dest_mac(tx_dest_mac), .m_eth_src_mac(tx_src_mac),
       .m_eth_type(tx_type),
       // payload -> 直接连到 tx 的 payload 输入
       .m_eth_payload_axis_tdata(tx_pay_tdata),
       .m_eth_payload_axis_tvalid(tx_pay_tvalid),
       .m_eth_payload_axis_tready(tx_pay_tready),
       .m_eth_payload_axis_tlast(tx_pay_tlast),
       .m_eth_payload_axis_tuser(tx_pay_tuser)
   );

   eth_axis_tx #(.DATA_WIDTH(8)) tx_inst (
       .clk(clk), .rst(rst),
       .s_eth_hdr_valid(tx_hdr_valid), .s_eth_hdr_ready(tx_hdr_ready),
       .s_eth_dest_mac(tx_dest_mac), .s_eth_src_mac(tx_src_mac),
       .s_eth_type(tx_type),
       .s_eth_payload_axis_tdata(tx_pay_tdata),
       .s_eth_payload_axis_tvalid(tx_pay_tvalid),
       .s_eth_payload_axis_tready(tx_pay_tready),
       .s_eth_payload_axis_tlast(tx_pay_tlast),
       .s_eth_payload_axis_tuser(tx_pay_tuser),
       .m_axis_tdata(tx_out_tdata), .m_axis_tvalid(tx_out_tvalid),
       .m_axis_tready(tx_out_tready), .m_axis_tlast(tx_out_tlast),
       .m_axis_tuser(tx_out_tuser)
   );
   ```

   注意：头部三字段（`dest_mac`/`src_mac`/`type`）是组合直连，`hdr_valid`/`hdr_ready` 也是直连——这正是 4.2 里讲的握手会自然协调（tx 空闲时 `hdr_ready=1`，rx 才送出头）。

2. **用 cocotb 驱动**（示例代码）：用 scapy 构造一帧 `EtherType=0x0800` 的以太网包，从 `rx_in` 发入，从 `tx_out` 收出，断言二者逐字节相等。

   ```python
   # 示例代码：驱动 rx->tx 回环
   from scapy.layers.l2 import Ether, IP
   pkt = Ether(src='5A:51:52:53:54:55', dst='DA:D1:D2:D3:D4:D5', type=0x0800)
   pkt = pkt / IP() / b'\x01\x02\x03\x04'
   await tb.rx_source.send(bytes(pkt))          # 发完整帧给 rx
   rx_frame = await tb.tx_sink.recv()           # 从 tx 收完整帧
   assert bytes(rx_frame) == bytes(pkt)         # 逐字节比对
   ```

3. **观察点**：
   - 目的 MAC `DA:D1:D2:D3:D4:D5`、源 MAC `5A:51:52:53:54:55`、`type=0x0800` 是否原样出现在 tx 输出帧的头部；
   - payload（`IP()/0x01020304`）是否完整无丢失；
   - 叠加随机 backpressure（用现有测试里的 `cycle_pause`）是否仍逐字节相等。

### 5.3 预期结果

无论是否叠加反压，`tx_out` 收到的帧与 `rx_in` 发出的帧逐字节相同——说明 rx 正确拆出了头部字段、tx 正确把它们拼了回去。这正是两个 `_HEADER_FIELD_` 宏互逆性的端到端验证。

运行结果：待本地验证（需自行新建上述 testbench 并配置 cocotb/iverilog；若只想快速验证，可直接复用现有 `tb/eth_axis_rx` 与 `tb/eth_axis_tx`，它们已分别覆盖「拆」和「装」两侧的正确性）。

## 6. 本讲小结

- `eth_axis_rx` 与 `eth_axis_tx` 是一对互逆的成帧模块，处理「14 字节以太网头 + payload」这一层，不碰 FCS。
- 它们确立了本库后续所有协议模块的接口风格：**头部用并行字段 + `hdr_valid`/`hdr_ready` 握手，载荷用标准 AXI-Stream**。
- rx 内部是「读头 → 读载荷」两段状态机，靠 `_HEADER_FIELD_` 宏逐字节提取头部；头部送出后会「挂住」直到下游消费，从而把反压耦合回输入。
- tx 内部是「发头 → 发载荷」两段状态机，握手瞬间锁存头部，再用与 rx 互逆的 `_HEADER_FIELD_` 宏把头部字节注入输出流。
- 8 位模式（`OFFSET=0`）逻辑最直观；宽位宽下用 `save_*` 寄存器 + 移位处理头部末字节与 payload 首字节同处一拍的对齐问题。
- 二者输出都套了「主寄存器 + temp 暂存」的弹性缓冲模板，使内部 ready 可提前一拍计算。
- 在真实系统（如 Arty）里，它们夹在 MAC 与 `udp_complete` 之间，是「裸帧」与「结构化头/载荷」的分界线。

## 7. 下一步学习建议

- **横向**：阅读 [rtl/eth_mux.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mux.v)、[rtl/eth_demux.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_demux.v)、[rtl/eth_arb_mux.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_arb_mux.v)（下一讲 u3-l2），看多路帧如何在「头部+载荷」接口上被复用与仲裁。
- **纵向（向上）**：进入协议栈系列。先看 [rtl/arp_eth_rx.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp_eth_rx.v)/[rtl/arp_eth_tx.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/arp_eth_tx.v)（u6-l1），它们与本讲的 rx/tx 接口约定一脉相承，只是头部变成了 ARP 字段。
- **纵向（向下）**：看 [rtl/axis_gmii_rx.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v)/[rtl/axis_gmii_tx.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_tx.v)（u4-l1），理解 eth_axis_rx 的输入那一侧的 AXI-Stream 帧是从哪来的（GMII 物理信号）。
- **动手**：完成第 5 节的 rx→tx 回环，作为进入 ARP/IP/UDP 协议栈前的「接口约定」实战验收。
