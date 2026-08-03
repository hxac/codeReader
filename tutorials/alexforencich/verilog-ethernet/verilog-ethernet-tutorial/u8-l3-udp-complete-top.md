# udp_complete：顶层 UDP 协议栈

## 1. 本讲目标

学完本讲后，读者应该能够：

- 说清 `udp_complete`（千兆 8 位）与 `udp_complete_64`（10G/25G 64 位）这两个"成品级"UDP 栈由哪几个子模块拼成，以及为什么这样拼。
- 画出一条完整的端到端数据通路：以太网帧入 → IP 层 → UDP 层 → 应用，以及反向的应用 → UDP → IP → 以太网帧出。
- 读懂模块对外暴露的三层头接口（以太网头 `eth`、IP 头 `ip`、UDP 头 `udp`），理解接收侧按 `ip_protocol` 分流、发送侧用仲裁复用器合流的机制。
- 理解 UDP 校验和在顶层只是"参数透传"，真正的计算由 `udp` 子模块内部的 `udp_checksum_gen`（经 `generate` 按需选配）完成。

本讲是 UDP 单元的收尾，把前面 u7（IPv4 栈 `ip_complete`）与 u8-l1/u8-l2（`udp` 核心 + 校验和生成）三块积木拼成一个可直接接 MAC、对接真实网络的 UDP 协议栈。

## 2. 前置知识

在进入本讲前，请确认你已经掌握以下概念（它们都在前置讲义中讲过）：

- **AXI-Stream 接口与"并行头 + 流式载荷"风格**（u1-l3、u3-l1）：本库所有协议层模块都用同一套接口——头部用一组并行字段配 `hdr_valid`/`hdr_ready` 握手，载荷用 `tdata`/`tvalid`/`tready`/`tlast`/`tuser` 流式搬运。`udp_complete` 的三层头接口全部沿用这一风格。
- **`ip_complete` 顶层 IPv4 栈**（u7-l3）：它把 `ip`（IPv4 收发，自身不查 MAC）、`arp`（IP→MAC 解析与缓存）、`eth_arb_mux`（在**以太网帧层**合并 IP 与 ARP）拼成完整 IP 栈。`udp_complete` 直接例化它。
- **`udp` 核心 UDP 模块**（u8-l1）：它在 IP 载荷上收发 UDP 报文，把 `udp_ip_rx`/`udp_ip_tx` 与可选的 `udp_checksum_gen` 拼成 UDP 收发双通路，IP 协议号硬编码为 `0x11`（17）。
- **UDP 校验和生成**（u8-l2）：`udp_checksum_gen` 用"IP 伪头部 + UDP 头 + 载荷"做 16 位反码求和，靠双 FIFO 化解"校验和须看完整帧才能定值"的矛盾；可由 `UDP_CHECKSUM_GEN_ENABLE` 旁路。

一个关键术语先点明：`udp_complete` 里出现的 `ip_arb_mux` 与 `ip_complete` 里出现的 `eth_arb_mux` 是**不同层**的复用器——`eth_arb_mux` 在以太网帧层合并（操作的是完整以太网+IP 帧），`ip_arb_mux` 在 IP 帧层合并（操作的是"并行 IP 头 + IP 载荷流"，还没有套以太网头）。本讲会讲清它们为何分层。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [rtl/udp_complete.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_complete.v) | 千兆（8 位）顶层 UDP 协议栈，本讲主角 |
| [rtl/udp_complete_64.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_complete_64.v) | 10G/25G（64 位）等价栈，对照讲解位宽差异 |
| [rtl/udp.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp.v) | `udp_complete` 内部例化的 UDP 核心模块，校验和在这里集成 |
| [rtl/ip_arb_mux.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_arb_mux.v) | IP 帧层仲裁复用器，发送侧合并"外部 IP"与"UDP 生成的 IP"两路 |
| [example/Arty/fpga/rtl/fpga_core.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v) | 板级参考设计，真实例化 `udp_complete` 并实现 UDP 回显应用，是本讲综合实践的依据 |

`ip_complete.v` / `ip_complete_64.v` 已在 u7-l3 详读，本讲当作黑盒引用。

## 4. 核心概念与源码讲解

### 4.1 UDP 栈顶层组装

#### 4.1.1 概念说明

`udp_complete` 是一个**布线层（wiring / glue logic）模块**——它本身几乎不做协议计算，只把三块已经成熟的积木拼起来：

1. `ip_complete`：完整 IPv4 栈（含 ARP 解析），负责以太网帧 ↔ IP 帧的转换、MAC 地址查找。
2. `udp`：UDP 核心，负责 IP 载荷 ↔ UDP 报文的转换（含可选校验和生成）。
3. `ip_arb_mux`：IP 帧层仲裁复用器，负责把"外部应用直接发的原始 IP 包"和"UDP 模块生成的 IP 包"两路合并成一路，再喂给 `ip_complete`。

为什么要这么拼？因为 `ip_complete` 只懂 IP 帧（一组并行 IP 头字段 + IP 载荷流），它不知道上层是 UDP、TCP 还是别的。`udp_complete` 在 `ip_complete` 之上再叠一层 UDP，并对收发各做一次"分流/合流"：

- **接收（RX）**：`ip_complete` 把以太网帧拆成 IP 帧后，`udp_complete` 按 IP 头里的协议号 `ip_protocol` 判断——是 UDP（`0x11`）就交给 `udp` 模块拆成 UDP 报文送给应用，否则原样转发到外部 `m_ip_*` 端口（留给 TCP 等其他协议用）。
- **发送（TX）**：应用可以从两个口发包——`s_udp_*`（UDP 口，模块自动帮你套 UDP 头 + IP 头）或 `s_ip_*`（原始 IP 口，应用自己负责 IP 层之上的封装）。两路 IP 帧经 `ip_arb_mux` 合并成一路，再由 `ip_complete` 套以太网头、查 ARP、发出去。

一句话：**`udp_complete` = `ip_complete` + `udp` + 一个把两路 IP 合流/分流的中间层**。

#### 4.1.2 核心流程

端到端数据通路如下（以 8 位 `udp_complete` 为例，64 位版结构完全相同）：

**接收方向（RX）：**

```
s_eth_* (以太网帧入)
   │
   ▼
ip_complete          ── 剥以太网头，校验 IP 头，输出 IP 帧
   │   (m_ip_* 内部信号 ip_rx_ip_*)
   ▼
┌─ 按 ip_protocol 分类 ─────────────────────┐
│ == 0x11 (UDP)?  ──是──▶ udp (udp_ip_rx)   │──▶ m_udp_* (UDP 报文给应用)
│                ──否──▶ 直通 m_ip_*        │──▶ m_ip_*  (原始 IP 给应用)
└──────────────────────────────────────────┘
```

**发送方向（TX）：**

```
s_udp_* (应用发 UDP) ──▶ udp (udp_checksum_gen + udp_ip_tx) ──▶ udp_tx_ip_* (IP 帧)
                                                                          │
s_ip_*  (应用发原始 IP) ─────────────────────────────────────────────────┤
                                                                          ▼
                                                              ip_arb_mux (2→1 合并)
                                                                          │
                                                                          ▼
                                                              ip_complete (套以太网头, 查 ARP)
                                                                          │
                                                                          ▼
                                                              m_eth_* (以太网帧出)
```

注意发送方向的精妙之处：`udp` 模块输出的已经是"IP 帧"（UDP 头被封装进 IP 载荷），所以它和外部 `s_ip_*` 在**同一层（IP 帧层）**竞争 `ip_complete` 的入口。这正是用 `ip_arb_mux`（而非 `eth_arb_mux`）的原因——合流发生在套以太网头**之前**。

#### 4.1.3 源码精读

模块声明与参数——7 个参数里，前 4 个属于 ARP（直接透传给 `ip_complete`），后 3 个属于 UDP 校验和（透传给 `udp`）：

[rtl/udp_complete.v:34-42](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_complete.v#L34-L42) 定义模块与全部参数；`UDP_CHECKSUM_GEN_ENABLE` 默认为 1（开启校验和自动生成）。

`ip_complete` 的例化——注意它把内部信号 `ip_rx_ip_*`（RX）和 `ip_tx_ip_*`（TX）分别接到 `ip_complete` 的 `m_ip_*` / `s_ip_*`，而把对外的 `s_eth_*` / `m_eth_*` 直接连到模块端口：

[rtl/udp_complete.v:431-516](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_complete.v#L431-L516) 例化 `ip_complete_inst`，以太网口对外、IP 口对内（接仲裁器与 UDP 模块），并把 `local_mac`/`local_ip`/`gateway_ip`/`subnet_mask`/`clear_arp_cache` 五个配置信号原样下发。

`udp` 的例化——它的 IP 输入口接 RX 分类后的 `udp_rx_ip_*`，IP 输出口接 `udp_tx_ip_*`（送往仲裁器），UDP 收发口直接对外：

[rtl/udp_complete.v:521-637](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_complete.v#L521-L637) 例化 `udp_inst`；其中 `CHECKSUM_GEN_ENABLE(UDP_CHECKSUM_GEN_ENABLE)` 等三个参数把顶层的校验和配置透传下去。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：在源码里把三个子模块实例"对上号"，确认它们各自的输入输出口连到了哪。

**操作步骤**：

1. 打开 [rtl/udp_complete.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_complete.v)。
2. 定位三处实例：`ip_arb_mux_inst`（约 L358）、`ip_complete_inst`（约 L431）、`udp_inst`（约 L521）。
3. 对每个实例，只看它的 `clk/rst` 之后的前两三个端口，确认：`ip_arb_mux` 的两路输入是 `{s_ip_*, udp_tx_ip_*}`；`ip_complete` 的以太网口对外、IP 口接内部 `ip_rx_ip_*`/`ip_tx_ip_*`；`udp` 的 UDP 口对外、IP 口接内部 `udp_rx_ip_*`/`udp_tx_ip_*`。

**需要观察的现象**：三个实例之间没有"胶水状态机"——`udp_tx_ip_*` → `ip_arb_mux` → `ip_tx_ip_*` → `ip_complete` 是纯连线，证明这是一个布线层模块。

**预期结果**：你能用一句话讲清"应用 UDP 包 → `udp` → `ip_arb_mux` → `ip_complete` → 以太网帧出"这条链路上每一段由哪个实例负责。

#### 4.1.5 小练习与答案

**练习 1**：`udp_complete` 内部出现了 `ip_arb_mux`，而 `ip_complete` 内部用的是 `eth_arb_mux`。为什么 `udp_complete` 需要再加一个 `ip_arb_mux`，而 `ip_complete` 自己的 `eth_arb_mux` 不够用？

**参考答案**：`ip_complete` 的 `eth_arb_mux` 在以太网帧层合并 IP 帧与 ARP 帧（此时 IP 包已套好以太网头）。但 `udp_complete` 要合并的是"UDP 模块生成的 IP 帧"和"外部应用发的原始 IP 帧"——两者都还没套以太网头、还停留在"并行 IP 头 + IP 载荷流"的 IP 帧层，所以必须在进入 `ip_complete` 之前用 `ip_arb_mux` 在 IP 帧层先合流。两层复用器对应两个不同的合流层级。

**练习 2**：如果把 `udp_complete` 拆成"只用 `ip_complete` + `udp`，不要 `ip_arb_mux`"会发生什么？

**参考答案**：那么 `s_udp_*`（经 `udp` 转出的 IP 帧）和 `s_ip_*`（外部原始 IP 帧）就没有仲裁机制，两路会争抢 `ip_complete` 唯一的 `s_ip_*` 入口，无法正确共享。`ip_arb_mux` 正是用来把两路 IP 帧按整帧不拆地、有优先级地合并成一路。

### 4.2 接收分类与发送合流：UDP/IP 头接口的路由

#### 4.2.1 概念说明

`udp_complete` 对外暴露**三层头接口**，分别对应协议栈的三层：

| 接口前缀 | 层级 | 方向 | 含义 |
|----------|------|------|------|
| `s_eth_*` / `m_eth_*` | 以太网层 | RX/TX | 完整以太网帧（dest/src MAC + EtherType + 载荷），对接 MAC |
| `s_ip_*` / `m_ip_*` | IP 层 | TX/RX | 原始 IP 帧旁路口，应用可绕过 UDP 直接发/收 IP 包 |
| `s_udp_*` / `m_udp_*` | UDP 层 | TX/RX | UDP 报文口，应用在这里收发 UDP（只需给端口、IP、载荷） |

关键机制有两个：

- **接收侧的分类（demux）**：`ip_complete` 输出的 IP 帧，按 IP 头里的协议号 `ip_protocol` 分成两路——等于 `0x11`（UDP）的送进 `udp` 模块（最终从 `m_udp_*` 出），其余的从 `m_ip_*` 直通输出。
- **发送侧的合流（mux）**：`s_ip_*`（外部原始 IP）与 `udp` 模块生成的 IP 帧经 `ip_arb_mux` 合并为一路。

这两种路由都必须遵守一条铁律：**整帧不拆**——一旦某帧开始走某条路，必须等它的 `tlast` 到达才能切换，绝不能按字节把两帧交错。这与 u3-l2 讲的 `eth_arb_mux`/`eth_demux` 完全同理。

#### 4.2.2 核心流程

**接收分类**用一个寄存器 `s_select_udp_reg` 锁定当帧的选择：

```
组合逻辑：s_select_udp = (ip_rx_ip_protocol == 8'h11)   // 当前帧是不是 UDP？
                  s_select_ip   = !s_select_udp

时序逻辑（每拍）：
  若 IP 载荷有效且（当前未选路 或 当前帧刚结束 tlast）：
      s_select_udp_reg <= s_select_udp     // 在帧首锁存选择
      s_select_ip_reg  <= s_select_ip
  否则若载荷无效：清零两个 reg
```

锁存后，载荷的 `tvalid` 被 `s_select_udp_reg`/`s_select_ip_reg` 分别"与"一下，做到把同一份 IP 载荷按选择路由给 UDP 模块或外部 IP 口；`tready` 则反向汇聚（两个下游谁的 tready 有效就取谁）。

**发送合流**用 `ip_arb_mux`（内部例化 `arbiter`），靠 `request`/`grant`/`acknowledge` 机制整帧选路，`acknowledge` 绑定 `tlast` 保证 grant 整帧保持。仲裁模式由两个参数控制（见 4.2.3）。

#### 4.2.3 源码精读

接收分类的组合判定与帧首锁存——`0x11` 即 UDP 协议号 17：

[rtl/udp_complete.v:279-301](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_complete.v#L279-L301) `s_select_udp = (ip_rx_ip_protocol == 8'h11)` 判定协议；`always` 块在帧首（`!sel && !sel`）或上一帧 `tlast` 时更新 `s_select_udp_reg`/`s_select_ip_reg`，实现整帧锁定。

把 IP 帧路由给 UDP 模块——注意第 317 行 `udp_rx_ip_protocol` 被**硬编码为 `8'h11`**（无论原 IP 头协议号是什么，进了 UDP 路径就强制是 UDP）：

[rtl/udp_complete.v:303-324](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_complete.v#L303-L324) 把 IP 帧头与载荷复制给 `udp_rx_ip_*`，其中 `udp_rx_ip_payload_axis_tvalid = s_select_udp_reg && ip_rx_ip_payload_axis_tvalid` 用锁存的选择门控载荷有效。

外部 IP 口的直通输出与 `tready` 反向汇聚：

[rtl/udp_complete.v:327-353](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_complete.v#L327-L353) `m_ip_*` 用 `s_select_ip_reg` 门控；`ip_rx_ip_hdr_ready` 与 `ip_rx_ip_payload_axis_tready` 都是"UDP 下游 ready 或外部 IP 下游 ready"的或运算，把两个下游的反压正确反馈给 `ip_complete`。

发送侧 `ip_arb_mux` 的实例——`S_COUNT=2`、优先级仲裁（`ARB_TYPE_ROUND_ROBIN=0`）、LSB 高优先级（`ARB_LSB_HIGH_PRIORITY=1`）：

[rtl/udp_complete.v:358-398](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_complete.v#L358-L398) 端口连接用 Verilog 拼接 `{s_ip_*, udp_tx_ip_*}`——拼接中**左侧（高位，下标 1）是外部 `s_ip_*`，右侧（低位，下标 0）是 UDP 模块的 `udp_tx_ip_*`**。配合 `ARB_LSB_HIGH_PRIORITY=1`，意味着**UDP 模块（下标 0）的优先级高于外部原始 IP（下标 1）**。

`ip_arb_mux` 内部如何保证整帧不拆——`frame_reg` 在帧首置 1、`tlast` 清 0，期间 `request = s_ip_hdr_valid & ~grant` 不再受理新请求；`acknowledge` 绑 `tlast`：

[rtl/ip_arb_mux.v:207-208](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_arb_mux.v#L207-L208) `request` 与 `acknowledge` 的定义；`acknowledge` 只在某路 `tvalid&tready&tlast` 同时成立时拉高，保证 grant 持续整帧。

#### 4.2.4 代码实践（源码阅读 + 推理型）

**实践目标**：理解"UDP 路径优先级高于外部 IP"这一设计的后果。

**操作步骤**：

1. 阅读 [rtl/udp_complete.v:373-391](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_complete.v#L373-L391)，确认拼接顺序 `{外部 s_ip_*, UDP udp_tx_ip_*}` 与参数 `ARB_LSB_HIGH_PRIORITY(1)`。
2. 想象一个场景：应用同时从 `s_udp_*` 发一个 UDP 包、从 `s_ip_*` 发一个原始 IP 包，两者几乎同时到达 `ip_arb_mux`。
3. 阅读 [rtl/ip_arb_mux.v:34-50](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ip_arb_mux.v#L34-L50) 的参数说明，确认 `ARB_TYPE_ROUND_ROBIN=0` 表示**固定优先级**而非轮询。

**需要观察的现象 / 预期结果**：在固定优先级 + LSB 高优先级下，**UDP 路（下标 0）总会先获得 grant**，外部原始 IP 路（下标 1）必须等 UDP 那帧的 `tlast` 之后才能发包。这意味着 UDP 流量优先，但若 UDP 持续满载，理论上外部 IP 路可能被饿死（与 u3-l2 讲的优先级仲裁特性一致）。若希望公平，可把 `ARB_TYPE_ROUND_ROBIN` 改为 1（轮询），但这需要修改 `udp_complete` 内部实例的参数。

> 待本地验证：可仿照 `tb/test_udp_arb_mux_4.v` 的思路构造两路同时到达的 IP 帧，观察 grant 顺序。

#### 4.2.5 小练习与答案

**练习 1**：接收侧为什么用 `s_select_udp_reg`（寄存器）门控载荷，而不直接用组合的 `s_select_udp`？

**参考答案**：因为整帧不可拆。组合的 `s_select_udp` 在载荷字节流中可能因 `ip_rx_ip_protocol` 稳定而保持不变，但规范做法是在帧首把选择锁存进寄存器，确保一帧从头到尾走同一条路；只有在上一帧 `tlast` 之后才允许更新。直接用组合信号在边界情况下可能让一帧的前半段走 UDP、后半段走外部 IP，破坏帧完整性。

**练习 2**：`udp_complete` 同时有 `m_ip_*`（IP 输出）和 `m_udp_*`（UDP 输出）两个输出端口。一个进入的以太网帧最终从哪个口出来，由什么决定？

**参考答案**：由 IP 头里的协议号 `ip_protocol` 决定。若为 `0x11`（UDP），经 `ip_complete` 拆成 IP 帧后被分类器送进 `udp` 模块，最终从 `m_udp_*` 输出 UDP 报文；否则从 `m_ip_*` 直通输出原始 IP 帧。两者不会同时输出同一帧。

### 4.3 校验和集成与 UDP 头接口的填充

#### 4.3.1 概念说明

`udp_complete` **本身不计算任何校验和**。它只做两件事：

1. 把顶层参数 `UDP_CHECKSUM_GEN_ENABLE`（及两个 FIFO 深度参数）**透传**给内部的 `udp` 模块。
2. 在把应用送的 `s_udp_*` 头字段转交给 `udp` 模块时，**把大量 UDP 模块不需要外部提供的字段填 0**（如以太网 MAC、IP 版本/IHL、IP 校验和等），因为这些字段要么由 `ip_complete` 自动生成，要么由 `udp_ip_tx` 在串行化时计算。

真正的 UDP 校验和计算发生在 `udp` 模块内部：当 `CHECKSUM_GEN_ENABLE=1` 时，`udp` 用 `generate if` 在发送路径上插入 `udp_checksum_gen`（详见 u8-l2）；当 `=0` 时，用 `generate else` 把这段退化为纯连线（零面积），由应用通过 `s_udp_checksum` 自供校验和。

#### 4.3.2 核心流程

发送一个 UDP 包时，应用只需提供业务字段，其余自动补齐：

```
应用提供（s_udp_*）：            模块自动处理：
  s_udp_source_port               UDP 源端口 ──┐
  s_udp_dest_port                 UDP 目的端口 ├─ udp_checksum_gen 算出 UDP 长度 + 校验和
  s_udp_ip_source_ip              源 IP ────────┤   (校验和含 IP 伪头部)
  s_udp_ip_dest_ip                目的 IP ──────┤
  s_udp_payload_axis_*            UDP 载荷流 ───┘
  s_udp_length / s_udp_checksum   可填 0（开启校验和生成时被覆盖）

  被填 0 的字段：s_udp_eth_dest_mac/src_mac/type、s_udp_ip_version/ihl、
                 s_udp_ip_identification/flags/fragment_offset/header_checksum
  → 这些由 ip_complete / ip_eth_tx / udp_ip_tx 自动生成
```

`udp` 模块把应用载荷流过 `udp_checksum_gen`：载荷一边进 FIFO 缓存，一边累加反码和；帧末定值后，算好的 UDP 头（含长度与校验和）与缓存的载荷配对输出，再交给 `udp_ip_tx` 套上 UDP 头、补上 IP 长度与协议号 `0x11`。

#### 4.3.3 源码精读

顶层把校验和参数透传给 `udp`：

[rtl/udp_complete.v:521-525](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_complete.v#L521-L525) `.CHECKSUM_GEN_ENABLE(UDP_CHECKSUM_GEN_ENABLE)` 等三个参数下发。

把应用的 `s_udp_*` 头字段转交 `udp` 模块，并把无需外部提供的字段填 0：

[rtl/udp_complete.v:578-602](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_complete.v#L578-L602) `.s_udp_eth_dest_mac(48'd0)`、`.s_udp_ip_version(4'd0)`、`.s_udp_ip_header_checksum(16'd0)` 等被硬填为 0；而 `.s_udp_source_port(s_udp_source_port)`、`.s_udp_dest_port(s_udp_dest_port)`、`.s_udp_ip_source_ip(s_udp_ip_source_ip)` 等业务字段原样接入。

`udp` 模块内部的 `generate` 选配——开启时插 `udp_checksum_gen`，关闭时退化为连线：

[rtl/udp.v:256-353](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp.v#L256-L353) `if (CHECKSUM_GEN_ENABLE)` 分支例化 `udp_checksum_gen_inst`（[rtl/udp.v:260-321](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp.v#L260-L321)），`else` 分支用一串 `assign` 把输入直通到 `tx_udp_*`（[rtl/udp.v:323-351](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp.v#L323-L351)）。

`udp_ip_tx` 把 IP 协议号硬编码为 `0x11`——这是 UDP 在 IP 层的身份：

[rtl/udp.v:373](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp.v#L373) `.s_ip_protocol(8'h11)`。

#### 4.3.4 代码实践（参数对比型）

**实践目标**：通过对比参数，理解"开启/关闭校验和生成"对模块体积与接口要求的影响。

**操作步骤**：

1. 阅读 [rtl/udp_complete.v:39](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_complete.v#L39) 确认 `UDP_CHECKSUM_GEN_ENABLE = 1`（默认开启）。
2. 阅读 [rtl/udp.v:323-351](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp.v#L323-L351) 的 `else` 分支，看关闭后 `tx_udp_checksum` 直接等于 `s_udp_checksum`。
3. 在 `example/Arty/fpga/rtl/fpga_core.v` 中查看应用如何填校验和字段：

[example/Arty/fpga/rtl/fpga_core.v:280-281](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v#L280-L281) `tx_udp_length = rx_udp_length`、`tx_udp_checksum = 0`——Arty 回显应用把校验和填 0，**依赖 `udp_complete` 默认开启的校验和生成自动补上正确值**。

**需要观察的现象**：当 `UDP_CHECKSUM_GEN_ENABLE=1` 时，应用把 `s_udp_checksum` 填 0 即可，模块会算出正确值；若把它改为 0，则 `s_udp_checksum=0` 会被原样发出（UDP 允许校验和为 0 表示"不校验"，但对 IPv4 这是发送方选项，接收方仍可能要求非零）。

**预期结果**：理解"校验和生成是可选的、默认开启、可旁路"这一设计，以及它为何放在 `udp` 模块内部而非 `udp_complete` 顶层。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `udp_complete` 把 `s_udp_eth_dest_mac`、`s_udp_ip_version` 等字段填 0 交给 `udp` 模块，而不是由应用提供？

**参考答案**：因为这些字段会在下游被自动生成——以太网 MAC 由 `ip_complete` 通过 ARP 查到目的 MAC 后填入；IP 版本/IHL 固定为 4/5；IP 校验和由 `ip_eth_tx` 在串行化时累加。`udp_complete` 对应用的 `s_udp_*` 接口刻意只暴露业务字段（端口、IP、载荷、DSCP/TTL），降低应用层负担。

**练习 2**：`UDP_CHECKSUM_GEN_ENABLE=0`（旁路）时，`udp_complete` 还能正常发 UDP 包吗？应用需要做什么改变？

**参考答案**：能。旁路时 `udp` 模块的 `generate else` 把校验和计算逻辑退化为纯连线，`s_udp_checksum` 原样透传到输出。此时应用必须自行通过 `s_udp_checksum` 提供正确的 UDP 校验和（含 IP 伪头部），否则发出的包校验和为 0 或错误，对端可能丢弃。

### 4.4 64 位变体 udp_complete_64

> 本节作为 4.1–4.3 的对照，说明 10G/25G 用的 64 位版本与千兆版的等价性。它不单独配练习，因为其机制与 8 位版逐行对应。

`udp_complete_64` 与 `udp_complete` **同构**，唯一区别是数据通路加宽到 64 位并引入 `tkeep`：

- 数据位宽：`s_eth_payload_axis_tdata`、`s_ip_payload_axis_tdata`、`s_udp_payload_axis_tdata` 全部从 `[7:0]` 变为 `[63:0]`，并各加一根 `[7:0]` 的 `tkeep`（标记末字有效字节，详见 u1-l3）。对照 [rtl/udp_complete_64.v:55-60](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_complete_64.v#L55-L60) 与 [rtl/udp_complete.v:55-59](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_complete.v#L55-L59)。
- 子模块实例名：`ip_complete_64_inst`、`udp_64_inst`，参数与控制逻辑（分类器、仲裁器）与 8 位版**逐行相同**。
- `ip_arb_mux` 参数：`DATA_WIDTH(64)`、`KEEP_ENABLE(1)`，并多了 `tkeep` 的拼接 `{s_ip_payload_axis_tkeep, udp_tx_ip_payload_axis_tkeep}`：[rtl/udp_complete_64.v:370-380](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/udp_complete_64.v#L370-L380)。

接收分类的状态机、发送合流的仲裁逻辑、校验和参数透传，三者完全一致——这是本库"8 位与 64 位双胞胎"命名约定的典型体现（见 u1-l1）。

## 5. 综合实践

本实践让读者跑通一个**真实例化 `udp_complete` 的完整系统**，并验证端到端的端口、长度与校验和处理。我们用 `example/Arty` 这个板级参考设计——它把 MAC 与 `udp_complete` 接起来，并在应用层实现了一个"UDP 回显器"（收到 `dport=1234` 的 UDP 包，交换源/目的后原样回发）。

### 5.1 阅读应用如何使用 udp_complete

先看 Arty 如何实例化 `udp_complete` 并接线：

[example/Arty/fpga/rtl/fpga_core.v:422-447](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v#L422-L447) `udp_complete_inst` 的以太网收发口接 MAC 的 RX/TX；它的 `s_udp_*`（发送）接应用回显逻辑的 `tx_udp_*`，`m_udp_*`（接收）接应用的 `rx_udp_*`——这正是本讲要求的"从 `s_udp_*` 注入、从 `m_udp_*` 输出"的闭环（这里应用把 `m_udp_*` 收到的包回灌进 `s_udp_*`）。

网络配置与"IP 口未使用"的处理：

[example/Arty/fpga/rtl/fpga_core.v:224-244](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v#L224-L244) `local_ip = 192.168.1.128` 等；外部原始 IP 口 `s_ip_*` 全部接 0、`m_ip_*` 的 ready 接 1（即丢弃非 UDP 的 IP 包），说明这个应用只用 UDP 通路。

回显匹配与头部交换：

[example/Arty/fpga/rtl/fpga_core.v:247-281](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v#L247-L281) `match_cond = rx_udp_dest_port == 1234`；匹配后 `tx_udp_source_port = rx_udp_dest_port`、`tx_udp_dest_port = rx_udp_source_port`、`tx_udp_ip_dest_ip = rx_udp_ip_source_ip`（源/目的互换），`tx_udp_checksum = 0`（交给 `udp_complete` 自动计算）。

### 5.2 运行已有的 cocotb 端到端测试

`example/Arty/fpga/tb/fpga_core/` 下有一份 cocotb 测试，它通过 MII 物理层注入一个 UDP 包，验证整条 MAC → `udp_complete` → 回显 → `udp_complete` → MAC 链路：

**实践目标**：跑通端到端测试，确认端口交换、载荷回显、校验和正确。

**操作步骤**：

1. 确认已安装 cocotb、cocotbext-eth、cocotbext-axi、Icarus Verilog（见 u1-l4）。
2. 进入 `example/Arty/fpga/tb/fpga_core/`，运行 `make`（该目录已存在 `Makefile` 与 `test_fpga_core.py`）。
3. 关注测试中这两段：
   - 发包：[test_fpga_core.py:85-88](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/tb/fpga_core/test_fpga_core.py#L85-L88) 用 scapy 构造 `Ether/IP(src=192.168.1.100,dst=192.168.1.128)/UDP(sport=5678,dport=1234)/payload`。
   - 收包与断言：[test_fpga_core.py:136-140](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/tb/fpga_core/test_fpga_core.py#L136-L140) 断言回显包的 `IP.dst==原 src`、`IP.src==原 dst`、`UDP.dport==原 sport(5678)`、`UDP.sport==原 dport(1234)`、载荷一致。

**需要观察的现象**：测试会先触发一次 ARP 交互（因为 `192.168.1.100` 不在缓存里），完成 MAC 解析后，`udp_complete` 自动算出的 UDP 校验和使回显包通过 scapy 的校验。

**预期结果**：`make` 通过，日志显示 `test UDP RX packet` 成功，所有断言通过。

> 待本地验证：具体仿真器与依赖版本请按 `tox.ini` 锁定的版本安装；若 iverilog 未配置，命令会报错，这是环境问题而非设计问题。

### 5.3 修改实践：把回显端口从 1234 改为 5678

**实践目标**：通过改动一处应用逻辑，验证你已理解 `m_udp_*` → 应用 → `s_udp_*` 的接口闭环。

**操作步骤**：

1. 在 `example/Arty/fpga/rtl/fpga_core.v` 第 247 行把 `rx_udp_dest_port == 1234` 改为 `rx_udp_dest_port == 5678`。
2. 同步修改 `test_fpga_core.py` 第 87 行的 `dport=1234` 为 `dport=5678`。
3. 重新 `make`。

**需要观察的现象**：改为 5678 后，发往 1234 的包不再被回显（走 `no_match` 分支被丢弃），发往 5678 的包才回显。

**预期结果**：测试用 5678 通过、用 1234 失败（无回显包），证明 `match_cond` 这一根线确实控制了 `s_udp_*` 发送口的握手（[fpga_core.v:271-272](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v#L271-L272) 中 `tx_udp_hdr_valid = rx_udp_hdr_valid && match_cond`）。

> 注意：本实践如需在仓库内改动源码，请在工作副本上操作，勿提交对 `example/` 的修改。

## 6. 本讲小结

- `udp_complete`（8 位）与 `udp_complete_64`（64 位）是**布线层**模块，把 `ip_complete` + `udp` + `ip_arb_mux` 三块积木拼成一个成品 UDP 协议栈，自身几乎不做协议计算。
- **接收方向**：`ip_complete` 拆出 IP 帧后，按 `ip_protocol == 0x11`（UDP）分流——UDP 帧送进 `udp` 模块从 `m_udp_*` 输出，其余从 `m_ip_*` 直通；选择在帧首锁存、整帧不拆。
- **发送方向**：`s_udp_*`（经 `udp` 转成 IP 帧）与外部 `s_ip_*`（原始 IP 帧）在 **IP 帧层**经 `ip_arb_mux` 合流（默认固定优先级、UDP 路 LSB 高优先级），再由 `ip_complete` 套以太网头、查 ARP 发出。
- 对外暴露**三层头接口**（`eth`/`ip`/`udp`），应用从 `s_udp_*` 发包只需给端口、IP、载荷；以太网 MAC、IP 版本/IHL、IP 校验和等字段被填 0 后由下游自动生成。
- **校验和**在顶层只是参数透传：`UDP_CHECKSUM_GEN_ENABLE` 下发给 `udp` 模块，由其内部 `generate` 决定插入 `udp_checksum_gen`（默认）或退化为连线（旁路）。
- 64 位版与 8 位版**逐行同构**，差异仅在数据位宽 8→64、新增 `tkeep`、子模块名加 `_64`。

## 7. 下一步学习建议

- **向上——系统集成**：本讲的 `udp_complete` 已是一个可用的 UDP 栈。建议进入 u12-l1（组装完整 UDP 回显系统），看 `fpga_core.v` 如何把 MAC、`udp_complete` 与应用逻辑端到端连起来并上板。
- **向下——校验和细节**：若对"分期预扣 + 双 FIFO"的反码求和实现感兴趣，回顾 u8-l2 的 `udp_checksum_gen` 精读。
- **横向——10G/25G**：若目标是高速网络，对照阅读 `rtl/udp_complete_64.v` 与 u9（10G MAC）、u10（PCS/PMA PHY），理解 64 位通路 + `tkeep` 在整条高速链路中如何贯穿。
- **测试方法学**：本讲的端到端验证依赖 cocotb + scapy；建议阅读 u13-l1（cocotb 仿真平台架构），理解 `test_fpga_core.py` 如何用 scapy 构造报文、用断言验证回显。
