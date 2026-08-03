# 组装完整 UDP 回显系统

## 1. 本讲目标

前面十几讲我们分别拆解了以太网的每一层：PHY 物理接口、MAC、成帧、ARP、IPv4、UDP，以及 PTP 时间同步。但真正把它们用起来，是要把所有积木「拼成一块能上板跑的网卡」。

本讲以仓库自带的板级参考设计 `example/Arty` 为例，讲解如何把以下模块组装成一个完整的、可综合、可上板的 **UDP 回显系统**：

- `eth_mac_mii_fifo`（带 FIFO 的 MII 千兆 MAC，承接 [u4-l3](u4-l3-eth-mac-1g-core.md)、[u5-l1](u5-l1-mac-fifo-cdc.md)）
- `eth_axis_rx` / `eth_axis_tx`（以太网成帧层，承接 [u3-l1](u3-l1-eth-axis-framing.md)）
- `udp_complete`（完整 UDP 协议栈，承接 [u8-l3](u8-l3-udp-complete-top.md)）
- 一段用户自定义的「端口匹配 + FIFO 缓存 + 回显」应用逻辑

学完本讲，你应当能够：

1. 画出从 PHY 管脚到 UDP 载荷、再从应用回到 PHY 管脚的**完整端到端数据通路**，并说清每一拍数据经过哪个模块。
2. 读懂并改写 Arty 设计中的**端口匹配与回显逻辑**——理解「组合判定 vs 整帧锁存」的区别，以及为什么回显必须配一个 payload FIFO。
3. 理解 `local_mac` / `local_ip` / `gateway_ip` / `subnet_mask` 这套**网络配置如何接线**到 `udp_complete`，以及为什么本设计的原始 IP 通路被关闭。

---

## 2. 前置知识

本讲是「集成」讲，几乎不引入新模块，而是把旧积木拼起来。阅读前请确认你已建立以下认知（若生疏可回看对应讲义）：

- **AXI-Stream 接口**（[u1-l3](u1-l3-axi-stream-interface.md)）：`tdata/tvalid/tready/tlast/tuser` 的握手语义，`tlast` 划帧边界，`tuser` 最低位是坏帧标志。
- **以太网成帧**（[u3-l1](u3-l1-eth-axis-framing.md)）：`eth_axis_rx` 把一根裸 AXI 帧拆成「并行以太网头字段 + 载荷流」，`eth_axis_tx` 是逆运算；二者用 `hdr_valid/hdr_ready` 握手头部。
- **千兆 MAC**（[u4-l3](u4-l3-eth-mac-1g-core.md)、[u4-l4](u4-l4-phy-if-and-tri-mode.md)）：MAC 对外是裸 AXI 帧（含目的/源 MAC、类型、载荷，但 FCS 已剥离）；`_fifo` 变体把 PHY 时钟域桥接到 `logic_clk`，并给 RX 补上反压。
- **udp_complete**（[u8-l3](u8-l3-udp-complete-top.md)）：一个布线层模块，对外暴露**三层头接口**（`s_eth_*` / `s_ip_*` / `s_udp_*`），内部含 ARP、IPv4、UDP 收发与校验和生成。
- **MII 物理接口**（[u4-l1](u4-l1-axis-gmii-rx-tx.md)）：4 位半字节接口，靠 `clk_enable` 与 `mii_select` 在单一时钟下覆盖 10/100/1000 Mbps。

另外补充一个工程背景概念：

- **板级参考设计（reference design）**：仓库 `example/` 下每块开发板都有一份独立工程，模式高度统一——都用 `udp_complete` 组协议栈、都实现 UDP 回显、默认监听 `192.168.1.128:1234`。Arty 是其中最典型的一份，**核心逻辑全部集中在 `fpga_core.v` 这一个文件里**，因此本讲只精读它。

> 本仓库已被继任者 **taxi** 取代、停止维护（见 [u1-l1](u1-l1-project-overview.md)），但 Arty 设计的集成模式在 taxi 中几乎照搬，理解它即可举一反三。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [example/Arty/fpga/rtl/fpga_core.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v) | **本讲主角**。把 MAC + 成帧 + UDP 栈 + 回显应用拼在一起的「核心逻辑」模块，全部连线与应用逻辑都在这里。 |
| [example/Arty/fpga/rtl/fpga.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga.v) | 真正的 FPGA 顶层：用 MMCM 把板上 100 MHz 倍频出 125 MHz，再把 MII/管脚接到 `fpga_core`。 |
| [example/Arty/fpga/README.md](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/README.md) | 说明默认监听 `192.168.1.128:1234`，以及用 `netcat`/`hping` 测试的命令。 |
| [example/Arty/fpga/tb/fpga_core/test_fpga_core.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/tb/fpga_core/test_fpga_core.py) | cocotb 仿真：构造 ARP + UDP 报文驱动 MII，端到端验证回显。 |

被 `fpga_core` 例化的库模块（均在 `rtl/` 下，Arty 工程在本目录 `lib/eth/rtl/` 内嵌了一份副本以保证工程自包含）：`eth_mac_mii_fifo`、`eth_axis_rx`、`eth_axis_tx`、`udp_complete`、`axis_fifo`。本讲把它们当作「已知黑盒」，重点放在**它们如何被连线**，而非内部实现。

---

## 4. 核心概念与源码讲解

### 4.1 端到端数据通路：从 PHY 管脚到 UDP 载荷

#### 4.1.1 概念说明

「数据通路」（data path）指的是一个字节从网线进入 FPGA、到被你的应用代码看到，中间依次经过哪些模块；以及反过来，应用产生的字节如何回到网线。

Arty 设计最值得学习的一点是它的**分层非常干净**，每一层只解一种封装：

```
PHY MII 管脚  ──►  MAC（剥前导/FCS、跨时钟域）  ──►  成帧（拆以太网头）  ──►  UDP 栈（拆 IP/UDP 头）  ──►  应用
   物理层           链路层(MAC)                     以太网成帧层            网络/传输层              用户逻辑
```

一个关键集成要点（也是初学者最容易踩的坑）：**MAC 对外吐出的是「裸 AXI-Stream 帧」**（一串字节，帧头含目的/源 MAC 与 EtherType，但 FCS 已被剥离），而 **`udp_complete` 的以太网侧接口（`s_eth_*`）是「结构化头 + 载荷流」**——它要的是拆开的 `dest_mac`/`src_mac`/`type` 并行字段加上独立的 payload 流。两者「语言」不同，所以中间必须插一对 `eth_axis_rx`（收：裸帧→结构化头）和 `eth_axis_tx`（发：结构化头→裸帧）做翻译。这正是 [u3-l1](u3-l1-eth-axis-framing.md) 那对成帧模块在真实系统中的位置。

#### 4.1.2 核心流程

**接收方向（RX）**，一个 UDP 字节自上而下流动：

1. PHY 芯片（Arty 上是 TI DP83848J）通过 MII 把 4 位半字节送进 `eth_mac_mii_fifo`。
2. MAC 剥离前导码/SFD、校验并剥离 FCS，把有效载荷（完整以太网帧）以 8 位 AXI-Stream 吐到 `rx_axis_*`；同时把 PHY 时钟域桥接到内部 `logic_clk`。
3. `eth_axis_rx` 读出 `rx_axis_*`，拆出 `rx_eth_dest_mac/src_mac/type` 并行头 + `rx_eth_payload_*` 载荷流。
4. `udp_complete` 吃下以太网头，按 EtherType=0x0800 走 IP、按协议号 0x11 走 UDP，最终从 `rx_udp_*` 输出 UDP 头字段 + `rx_udp_payload_*` 载荷流。
5. 应用逻辑（端口匹配 + FIFO）消费 `rx_udp_payload_*`。

**发送方向（TX）**完全对称，只是把「拆包」换成「封包」，并且数据来自应用的回显 FIFO：

```
应用回显 FIFO ──► udp_complete(封 UDP/IP/ARP 头) ──► eth_axis_tx(封以太网头) ──► MAC(补前导/FCS/IFG) ──► PHY
```

数据通路里有一条**关键的不对称**，必须记住：接收方向 PHY 是线速不可反压的（网线不会等你），所以 RX 必须有缓冲；而发送方向应用可以反压。这条不对称正是 4.2 节那个 payload FIFO 存在的根本原因。

#### 4.1.3 源码精读

先看 `fpga_core` 如何用一组 wire 把四个模块「手拉手」串起来。MAC 与成帧层之间的裸帧总线在这里声明：

[example/Arty/fpga/rtl/fpga_core.v:89-100](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v#L89-L100) — 声明 MAC 与成帧模块之间的裸 AXI-Stream 帧（`rx_axis_*` / `tx_axis_*`），8 位数据，标准五信号。

MAC 实例 `eth_mac_mii_fifo` 是 MII（100BASE-T）+ FIFO 的千兆 MAC 封装。注意它把对外管脚 `phy_rxd/phy_rx_clk/...` 与内部 `logic_clk` 解耦：

[example/Arty/fpga/rtl/fpga_core.v:321-369](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v#L321-L369) — 例化 `eth_mac_mii_fifo`。`logic_clk` 接到本模块唯一的 125 MHz `clk`；MII 物理时钟 `mii_rx_clk/mii_tx_clk` 来自 PHY；RX/TX 各开 4096 深度的帧 FIFO（`TX_FRAME_FIFO=1`/`RX_FRAME_FIFO=1`），保证整帧不拆、可丢坏帧。

成帧层 `eth_axis_rx` 吃 MAC 的裸帧、吐结构化以太网头：

[example/Arty/fpga/rtl/fpga_core.v:371-395](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v#L371-L395) — `eth_axis_rx` 把 `s_axis_*`（裸帧）翻译成 `m_eth_hdr_valid/dest_mac/src_mac/type` 头字段 + `m_eth_payload_axis_*` 载荷流。

发送侧的 `eth_axis_tx` 是它的镜像：

[example/Arty/fpga/rtl/fpga_core.v:397-420](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v#L397-L420) — `eth_axis_tx` 把 `s_eth_*`（结构化头 + 载荷）重组为 `m_axis_*` 裸帧，交还给 MAC 发送。

中间的 `rx_eth_*` / `tx_eth_*` 这套结构化以太网头总线把成帧层与 UDP 栈对接：

[example/Arty/fpga/rtl/fpga_core.v:102-123](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v#L102-L123) — 声明成帧层与 `udp_complete` 之间的结构化以太网头 + 载荷总线。

最后是协议栈主体 `udp_complete`。它的端口极多，但本质上就是「以太网头侧」与上层三对接口的接线。以太网侧 RX 接 `rx_eth_*`、TX 接 `tx_eth_*`：

[example/Arty/fpga/rtl/fpga_core.v:422-447](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v#L422-L447) — `udp_complete` 的以太网侧端口：`s_eth_*` 接收（连 `rx_eth_*`）、`m_eth_*` 发送（连 `tx_eth_*`）。

把以上五个实例的接线画成一张图，就是完整的端到端通路：

```
            ┌──────────── RX 方向 ────────────►
 PHY MII ─► eth_mac_mii_fifo ─rx_axis─► eth_axis_rx ─rx_eth─► udp_complete ─rx_udp─► [应用回显]
                                                                       ▲
 PHY MII ◄─ eth_mac_mii_fifo ◄tx_axis─ eth_axis_tx ◄tx_eth─ udp_complete ◄tx_udp─ [应用回显]
            └──────────── TX 方向 ◄────────────
```

#### 4.1.4 代码实践

**实践目标**：用仓库自带的 cocotb 仿真，亲眼看到一个 UDP 包走完「PHY → MAC → 成帧 → UDP 栈 → 应用 → 回显 → PHY」的完整往返。

**操作步骤**：

1. 确认已安装 cocotb、cocotbext-eth、cocotb-test 与 Icarus Verilog（见 [u1-l4](u1-l4-testbench-and-simulation.md)）。
2. 阅读 [example/Arty/fpga/tb/fpga_core/test_fpga_core.py:82-140](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/tb/fpga_core/test_fpga_core.py#L82-L140)，它先用 scapy 构造一个 `IP(src=192.168.1.100, dst=192.168.1.128) / UDP(sport=5678, dport=1234)` 报文，经 `MiiPhy` 注入；由于目的 MAC 未解析，FPGA 先发回一个 **ARP 请求**；测试注入 ARP 应答后，FPGA 才把 UDP 包回显。
3. 进入测试目录运行：
   ```bash
   cd example/Arty/fpga/tb/fpga_core
   pytest test_fpga_core.py        # 或直接 make
   ```

**需要观察的现象**：

- 仿真日志先打印 `receive ARP request`（FPGA 主动广播 ARP 查 `192.168.1.100` 的 MAC），再打印 `receive UDP packet`（回显包）。
- 断言 `rx_pkt[UDP].dport == test_pkt[UDP].sport` 与 `rx_pkt[UDP].payload == test_pkt[UDP].payload` 通过——说明源/目的端口被对调、载荷原样回送。

**预期结果**：测试通过，证明端到端通路（含 ARP 协商）在仿真层面闭环成立。若本机未配齐工具链，则**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `eth_axis_rx` / `eth_axis_tx` 这对模块删掉、直接把 MAC 的 `rx_axis_*` 接到 `udp_complete` 的 `s_eth_*`，能工作吗？为什么？

> **答案**：不能。MAC 的 `rx_axis_*` 是一根裸 AXI-Stream 字节流，而 `udp_complete` 的 `s_eth_*` 期望的是已拆开的并行头字段（`dest_mac/src_mac/type`）加独立 payload 流，端口名和语义都对不上。`eth_axis_rx/tx` 正是这两种「语言」之间必不可少的翻译器。

**练习 2**：本设计的数据通路位宽是多少？为什么不需要 `tkeep`？

> **答案**：8 位/拍（千兆 MII 通路）。`tkeep` 只在位宽大于 8 的通路上出现，用于标记末字有效字节（见 [u1-l3](u1-l3-axi-stream-interface.md)）；8 位通路每拍正好 1 字节，故全链路省略 `tkeep`。


---

### 4.2 端口匹配与回显逻辑

#### 4.2.1 概念说明

「应用逻辑」是用户在 `fpga_core.v` 里手写的那段代码——它才是这个工程的「业务」。Arty 的业务很简单：**收到目的端口为 1234 的 UDP 包，就把载荷原样回发给发送方**（源/目的 IP 与端口对调）。

要把这件事做对，应用逻辑必须解决三个子问题：

1. **端口匹配**：怎么判断当前这帧是不是「目标端口 = 1234」？
2. **整帧锁存**：一个 UDP 包有几十上百拍载荷，匹配判定必须在帧首做一次、并稳定保持到帧尾，绝不能中途改变（否则会把一帧拆成两半路由到不同去处）。
3. **载荷缓冲**：回显包要等 `udp_complete` 先解出 ARP 拿到对端 MAC 后才能发出，期间 RX 载荷持续到达，必须有地方暂存——这就是那个 `axis_fifo`。

第 1、2 点呼应 [u3-l2](u3-l2-eth-mux-arb.md) 讲过的「整帧不拆」铁律；第 3 点呼应 [u5-l1](u5-l1-mac-fifo-cdc.md) 的「PHY 不可反压、必须缓冲」。

#### 4.2.2 核心流程

回显逻辑可拆成三段，分别处理「头部」「载荷写入 FIFO」「载荷读出 FIFO」：

```
帧首：用组合逻辑判定 match_cond = (dest_port == 1234)
        │
        ├─ 命中：把回显头部字段接到 tx_udp_*（源/目的 IP、端口对调）
        └─ 未命中：丢弃（rx_udp_hdr_ready 直接拉高吞掉头部）

帧体：把 match_cond 锁存进 match_cond_reg，整帧保持
        │
        └─ 命中帧的载荷写入 rx_fifo_udp_payload_*（axis_fifo 缓存）

帧尾及之后：udp_complete 解出 ARP 后从 tx_udp_* 发出回显包，
           载荷从 axis_fifo 的 tx_fifo_udp_payload_* 读出回放
```

**回显头部字段的对调规则**（这是回显的核心语义）：

| 字段 | 回显包取值 | 含义 |
| --- | --- | --- |
| 目的 IP `tx_udp_ip_dest_ip` | 收到包的源 IP | 发给「刚才那个发送者」 |
| 源 IP `tx_udp_ip_source_ip` | `local_ip` | 用本机 IP |
| 目的端口 `tx_udp_dest_port` | 收到包的源端口 | |
| 源端口 `tx_udp_source_port` | 收到包的目的端口（1234） | |
| TTL `tx_udp_ip_ttl` | 64 | 重新置一个合理 TTL |
| 校验和 `tx_udp_checksum` | 0 | 交给 `udp_complete` 内部的 `udp_checksum_gen` 重算 |

#### 4.2.3 源码精读

**(a) 组合匹配 vs 整帧锁存**

匹配判定的「源头」是一根组合 wire：

[example/Arty/fpga/rtl/fpga_core.v:246-248](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v#L246-L248) — `match_cond = (rx_udp_dest_port == 1234)`，`no_match` 为其取反。这是对**当前帧**的端口判定。

但载荷流要跨越很多拍，必须把判定「冻结」成寄存器，稳定保持整帧。下面这段 `always` 块实现「帧首采样、整帧保持、帧尾释放」：

[example/Arty/fpga/rtl/fpga_core.v:250-269](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v#L250-L269) — `match_cond_reg`/`no_match_reg` 锁存逻辑。

它的状态转移规则（伪代码）：

```
每拍（posedge clk）：
  若 rst：两 reg 清 0
  否则若 rx_udp_payload_tvalid：
      若 处于空闲（两 reg 都为 0） 或 当前拍是帧尾（握手且 tlast）：
          重新采样 match_cond / no_match        # 新帧开始
      # 否则保持不变（整帧锁定）
  否则（帧间空闲）：
      两 reg 清 0
```

要点：当两个 reg 都为 0，说明「没有帧正在被处理」，此时收到第一个 valid 即采样并锁存；之后直到 `tlast` 那拍才允许下一次采样。这就保证了一帧之内 `match_cond_reg` 恒定不变。

> **为什么头部用组合 `match_cond`、载荷却用寄存器 `match_cond_reg`？** 头部是一次性握手，发生帧首那一刻 `rx_udp_dest_port` 已经由 `udp_complete` 稳定给出，组合判定即可；载荷流跨越多拍，必须用锁存值才能保证整帧一致路由。这是 [u3-l2](u3-l2-eth-mux-arb.md)「整帧不拆」原则在应用层的落地。

**(b) 回显头部接线**

头部字段全部用组合 `assign` 把「收到的字段」翻转后接到 TX：

[example/Arty/fpga/rtl/fpga_core.v:271-281](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v#L271-L281) — 回显头部接线。

其中两条最关键：

- `tx_udp_hdr_valid = rx_udp_hdr_valid && match_cond;` —— 只在命中时才把回显头交给 `udp_complete`，未命中的帧根本不产生 TX 头。
- `rx_udp_hdr_ready = (tx_eth_hdr_ready && match_cond) || no_match;` —— 命中帧的 RX 头在下游就绪时才消费；未命中帧则无条件拉高 `rx_udp_hdr_ready`，**主动吞掉头部完成丢弃**（吞掉头部后载荷也会被下面的逻辑丢弃）。

端口/IP 对调就在这几行：

- `tx_udp_ip_dest_ip = rx_udp_ip_source_ip;`
- `tx_udp_source_port = rx_udp_dest_port;`
- `tx_udp_dest_port = rx_udp_source_port;`

**(c) 载荷 FIFO 通路**

命中帧的载荷写入一个 `axis_fifo`，回显时再读出。注意 `tvalid` 与 `tready` 都被 `match_cond_reg` 选通：

[example/Arty/fpga/rtl/fpga_core.v:289-293](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v#L289-L293) — RX 载荷写入 FIFO：`rx_fifo_udp_payload_axis_tvalid = rx_udp_payload_axis_tvalid && match_cond_reg`，且 `rx_udp_payload_axis_tready` 在未命中时由 `no_match_reg` 直接拉高（丢弃而不反压）。

[example/Arty/fpga/rtl/fpga_core.v:283-287](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v#L283-L287) — TX 载荷从 FIFO 读出，原样接到 `tx_udp_payload_*`。

FIFO 实例本身是个普通的字节流 FIFO（不是帧 FIFO）：

[example/Arty/fpga/rtl/fpga_core.v:554-592](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v#L554-L592) — `axis_fifo` 实例：`DEPTH=8192`、`DATA_WIDTH=8`、`FRAME_FIFO=0`、`USER_ENABLE(1)`（透传 1 位 `tuser` 坏帧位）。

> **为什么需要这个 FIFO？** 因为回显是「先收完/边收边存，后发出」。TX 侧要等 `udp_complete` 解出 ARP、拿到对端 MAC 后才能开始发包；这期间 RX 载荷还在源源不断地到达。FIFO 把 RX 载荷先存下来，等 TX 就绪再回放，从而解耦收发时序。它用 `FRAME_FIFO=0`（普通流 FIFO）即可，因为帧边界由上面的 `match_cond_reg` 选通逻辑保证，`tlast` 会随流透传。

**(d) 附加彩蛋：首字节上 LED**

设计顺手把回显包的**第一个载荷字节**显示到板上的 LED，方便上板肉眼调试：

[example/Arty/fpga/rtl/fpga_core.v:296-316](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v#L296-L316) — 用 `valid_last` 标志在每帧首拍锁存 `tx_udp_payload_axis_tdata` 到 `led_reg`，帧尾清标志；`led_reg` 驱动 8 个 LED。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：把回显目标端口从 `1234` 改为 `5678`，并说清要动哪些信号、为什么只动一处即可。

**操作步骤**：

1. 打开 [example/Arty/fpga/rtl/fpga_core.v:247](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v#L247)，把
   ```verilog
   wire match_cond = rx_udp_dest_port == 1234;
   ```
   改为
   ```verilog
   wire match_cond = rx_udp_dest_port == 5678;
   ```
2. 这是**唯一**需要改的 RTL 信号。原因：`match_cond` 是整条回显逻辑的总开关，头部选通（`tx_udp_hdr_valid`、`rx_udp_hdr_ready`）与载荷选通（`match_cond_reg`）全部由它派生；改这一处，所有下游行为自动跟着变。
3. 为验证改动，同步修改测试 [test_fpga_core.py:87](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/tb/fpga_core/test_fpga_core.py#L87)，把 `UDP(sport=5678, dport=1234)` 改为 `UDP(sport=1234, dport=5678)`（让测试包的目的端口命中新规则）。
4. 重新跑 `pytest test_fpga_core.py`。

**需要观察的现象**：

- 改动前：发往 `dport=1234` 的包被回显，发往 `5678` 的包被丢弃。
- 改动后：行为反转——`dport=5678` 的包被回显，`dport=1234` 的包被丢弃。

**预期结果**：测试在改端口后通过；若保持测试包 `dport=1234` 不变，则仿真会因等不到回显包而**超时失败**——这反过来证明端口匹配确实生效。**待本地验证**。

> 提示：README 里写明的对外端口（[example/Arty/fpga/README.md:7-8](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/README.md#L7-L8)）与 `netcat` 命令里的 `1234` 只是文档/使用说明，不影响综合；但若你打算上板用，记得把 README 与 `netcat -u 192.168.1.128 5678` 一并更新，保持一致。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `match_cond_reg` 改回直接用组合 `match_cond` 去选通载荷（即 `rx_fifo_udp_payload_axis_tvalid = rx_udp_payload_axis_tvalid && match_cond`），功能上通常会出什么问题？

> **答案**：虽然 `udp_complete` 在整帧期间会保持 `rx_udp_dest_port` 稳定，理论上组合判定也能工作，但用寄存器锁存是「整帧不拆」的稳健写法——它保证即便端口字段在帧间过渡期出现短暂变化，也绝不会把一帧的载荷路由到两个去向。直接用组合值缺少这层保证，遇到时序毛刺或下游反压交错时可能把帧拆散。

**练习 2**：未命中端口（`no_match`）的帧，其载荷是怎么被丢弃的？会反压上游吗？

> **答案**：靠 `rx_udp_payload_axis_tready = (... || no_match_reg)` 把 `tready` 无条件拉高，于是 `udp_complete` 的 RX 载荷被「读空丢弃」，且**不写入 FIFO**（因为写侧 `tvalid` 被 `match_cond_reg` 选通为 0）。由于主动给 `tready`，不会反压上游，链路继续线速吞吐。

**练习 3**：回显包的 UDP 校验和 `tx_udp_checksum` 被赋为 0，这样发出去的包校验和会对吗？

> **答案**：会对。`udp_complete` 内部默认启用 `udp_checksum_gen`（见 [u8-l2](u8-l2-udp-checksum-gen.md)、[u8-l3](u8-l3-udp-complete-top.md)），会把应用填入的 `0` 替换为正确计算值（含 IP 伪头部）。所以应用层填 0 表示「让硬件算」，并非发一个校验和为 0 的非法包。

---

### 4.3 网络配置接线

#### 4.3.1 概念说明

一块网卡要能工作，除了数据通路，还必须有**网络身份**：自己的 MAC 地址、IP 地址、默认网关、子网掩码。在 `udp_complete` 里，这四个参数是模块端口，运行时（或综合时）注入，驱动 ARP 的「同子网判定」与发包时的源地址填充（详见 [u6-l2](u6-l2-arp-cache-and-top.md)、[u7-l3](u7-l3-ip-complete-top.md)）。

Arty 设计用一组 `wire` 常量定义本机网络身份，然后接到 `udp_complete` 的配置端口。这一节同时讲清一个容易被忽略的细节：**本设计关闭了原始 IP 通路**（只跑 UDP，不暴露 raw IP 接口给应用）。

#### 4.3.2 核心流程

```
local_mac / local_ip / gateway_ip / subnet_mask  (wire 常量)
        │
        └──► udp_complete 的配置端口
              ├─ local_mac, local_ip   → 驱动 ip + arp（源地址、本机身份）
              ├─ gateway_ip, subnet_mask → 驱动 arp 的子网判定 ((p⊕g)&m)
              └─ clear_arp_cache = 0    → 运行时不主动清 ARP 缓存

raw IP 通路（udp_complete 的 m_ip_* 输出 / s_ip_* 输入）：
   RX：rx_ip_hdr_ready=1, rx_ip_payload_axis_tready=1  → 主动吞掉（应用不收 raw IP）
   TX：tx_ip_* 全部置 0 / 无效                          → 应用从不发 raw IP
```

#### 4.3.3 源码精读

**(a) 本机网络身份**

四个常量在这里定义（注意 `local_ip` 用拼接 `{8'd192, 8'd168, 8'd1, 8'd128}` 直观表示 `192.168.1.128`）：

[example/Arty/fpga/rtl/fpga_core.v:223-227](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v#L223-L227) — 定义 `local_mac=02:00:00:00:00:00`、`local_ip=192.168.1.128`、`gateway_ip=192.168.1.1`、`subnet_mask=255.255.255.0`。

> `local_mac` 用 `02:00:00:00:00:00` 是**本地管理地址（LAA）**：第一字节次低位为 1 表示「本地分配」，不与 IEEE 分配的全球唯一地址冲突，适合 FPGA 这种无烧录 MAC 的场景。

**(b) 配置端口接线**

这四个常量接到 `udp_complete` 的配置端口（与 [u7-l3](u7-l3-ip-complete-top.md) 讲的 `ip_complete` 配置同源）：

[example/Arty/fpga/rtl/fpga_core.v:547-551](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v#L547-L551) — `.local_mac(local_mac), .local_ip(local_ip), .gateway_ip(gateway_ip), .subnet_mask(subnet_mask), .clear_arp_cache(0)`。

其中 `gateway_ip` 与 `subnet_mask` 只被内部的 `arp` 模块用于子网判定：目的 IP 与本机同子网（\((\text{dst}\oplus\text{local})\,\&\,\text{mask}=0\)）时直接查目的 IP 的 MAC，跨网段时改查网关 MAC（见 [u6-l2](u6-l2-arp-cache-and-top.md)）。`clear_arp_cache=0` 表示运行时不主动清空 ARP 表。

**(c) 关闭原始 IP 通路**

`udp_complete` 除了 UDP 接口，还暴露一对原始 IP 接口（`m_ip_*` 输出非 UDP 的 IP 包、`s_ip_*` 输入应用自构的 IP 包）。Arty 只做 UDP 回显，不用 raw IP，所以把这对接口「钉死」：

[example/Arty/fpga/rtl/fpga_core.v:229-244](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v#L229-L244) — 把 RX 侧 `rx_ip_hdr_ready`/`rx_ip_payload_axis_tready` 拉高（吞掉 raw IP），TX 侧 `tx_ip_*` 全部置 0/无效（永不发送 raw IP）。

这是一种常见的「弃用接口」写法：输入端给恒定 `tready=1` 把数据读空（避免堵塞协议栈），输出端给恒定 `valid=0` 表示本应用不产生该类数据。

#### 4.3.4 代码实践

**实践目标**：把本机 IP 从 `192.168.1.128` 改为 `10.0.0.5`，并验证 ARP/回显随之改变。

**操作步骤**：

1. 修改 [example/Arty/fpga/rtl/fpga_core.v:225](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v#L225)：
   ```verilog
   wire [31:0] local_ip    = {8'd10, 8'd0, 8'd0, 8'd5};
   ```
2. 同步把测试 [test_fpga_core.py:86](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/tb/fpga_core/test_fpga_core.py#L86) 里 scapy 包的 `dst='192.168.1.128'` 改为 `dst='10.0.0.5'`，并把 ARP 应答里的 `pdst`/`psrc` 对应改对。
3. 重新仿真。

**需要观察的现象**：FPGA 发出的 ARP 请求里，`psrc`（发送方协议地址，即本机 IP）变为 `10.0.0.5`；发回的 UDP 回显包 IP 头里源 IP 也是 `10.0.0.5`。

**预期结果**：测试通过，证明 `local_ip` 这一个常量同时驱动了 ARP 源地址与回显包源 IP。若不改测试包目的 IP，FPGA 会因「目的 IP 不是本机」而丢包。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：若想让 FPGA 响应 ping（ICMP），本设计能直接做到吗？

> **答案**：不能。ICMP 是 IP 层协议（协议号 1），会被 `udp_complete` 从 `m_ip_*` 输出，但本设计把 `rx_ip_payload_axis_tready` 直接拉高吞掉了所有 raw IP 载荷，且没有 ICMP 处理逻辑。要支持 ping，需要在 `m_ip_*` 侧接一个 ICMP 模块（仓库未提供），并放开 raw IP 通路。

**练习 2**：`clear_arp_cache` 接 0，意味着什么？什么场景下会想拉高它？

> **答案**：接 0 表示运行期不清空 ARP 缓存表，已学到的 IP→MAC 映射一直有效（直到被新条目覆盖）。若网络拓扑变更（某台机器换了网卡/MAC），旧缓存可能导致发包到错误 MAC，此时可拉高 `clear_arp_cache` 一个周期强制清表、重新学习（见 [u6-l2](u6-l2-arp-cache-and-top.md) 的 `clear_cache` 扫表机制）。

---

## 5. 综合实践

**任务**：在 Arty `fpga_core.v` 基础上，把「单端口回显」升级为「双端口回显」——同时监听 `1234` 与 `5678` 两个端口，两个端口的包都原样回显，但**回显包的首字节**分别用不同 LED 组显示，以便上板区分。

**提示与步骤**：

1. **匹配条件**：把 [fpga_core.v:247](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v#L247) 的 `match_cond` 改成「命中任一端口」：
   ```verilog
   wire match_cond = (rx_udp_dest_port == 1234) || (rx_udp_dest_port == 5678);
   ```
   `match_cond_reg` 锁存逻辑无需改动——它本就是整帧保持的。
2. **区分端口**：再锁存一个 `port_id_reg`（0 表示来自 1234、1 表示来自 5678），在帧首与 `match_cond_reg` 一同采样，帧尾清零。参考 [fpga_core.v:250-269](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v#L250-L269) 的写法。
3. **LED 区分**：修改 [fpga_core.v:296-316](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/example/Arty/fpga/rtl/fpga_core.v#L296-L316) 的 `led_reg` 逻辑，按 `port_id_reg` 把首字节分别驱动到 `{led0_g..led3_g,led4..led7}` 或另一组 LED（板上有 4 个 RGB LED，可借用红/绿通道）。
4. **验证**：在 testbench 里发两个包（`dport=1234` 与 `dport=5678`），断言两者都被回显，且载荷完整。

**验收标准**：

- 两个端口的包都能被回显，源/目的端口、IP 正确对调。
- 仿真中 `led_reg` 在两种包下呈现不同值（或落到不同 LED 组）。
- 非目标端口的包（如 `dport=9999`）仍被正确丢弃。

这个任务把本讲的三个最小模块（端到端通路、端口匹配、配置接线）全部串起来改一遍，做完你就真正掌握了「在 `udp_complete` 之上写自己的网络应用」。

---

## 6. 本讲小结

- Arty 的 `fpga_core.v` 用一条干净的分层链路把整套 IP 栈拼起来：**PHY MII → `eth_mac_mii_fifo` → `eth_axis_rx/tx`（成帧翻译）→ `udp_complete` → 应用**，发送方向完全对称。
- MAC 说「裸 AXI 帧」，`udp_complete` 说「结构化头 + 载荷流」，两者语言不同，必须用 `eth_axis_rx/tx` 这对成帧模块翻译——这是集成时最容易漏的一环。
- 应用逻辑核心是**端口匹配 + 整帧锁存 + 载荷 FIFO**：头部用组合 `match_cond` 选通（一次性握手），载荷用寄存器 `match_cond_reg` 整帧保持（整帧不拆），FIFO 解耦「先收后发」的收发时序。
- 回显语义就是字段对调：目的 IP/端口 ← 收到包的源 IP/端口，源 IP ← `local_ip`，校验和填 0 交给硬件 `udp_checksum_gen` 重算。
- 网络身份（`local_mac/local_ip/gateway_ip/subnet_mask`）作为常量注入 `udp_complete` 配置端口；原始 IP 通路被「读空 + 置无效」钉死，因为本设计只跑 UDP。
- 改监听端口只需改 `match_cond` 一处；改本机 IP 只需改 `local_ip` 一处——两者都验证了这套设计的参数化与可维护性。

---

## 7. 下一步学习建议

- **[u12-l2 综合约束与时序](u12-l2-synthesis-constraints.md)**：本讲只讲了逻辑集成，真正上板还需为 MII/RGMII 源同步接口加 SDC/TCL 时序约束，下一讲覆盖 `syn/` 目录。
- **[u13-l1 cocotb 仿真平台架构](u13-l1-cocotb-testbench-arch.md)**：本讲的 testbench 用了 `MiiPhy` 与 scapy，下一讲系统讲解 `cocotbext-eth` 的端点驱动体系。
- **动手扩展**：尝试在 `udp_complete` 的 `m_ip_*` 侧接一个简易 ICMP echo 模块实现 ping 响应，体会 raw IP 通路的打开方式。
- **对比 taxi**：把本设计与 taxi 仓库的等价 UDP 示例对照阅读，理解继任者在接口命名与参数化上的演进（见 [u1-l1](u1-l1-project-overview.md) 的弃用说明）。
