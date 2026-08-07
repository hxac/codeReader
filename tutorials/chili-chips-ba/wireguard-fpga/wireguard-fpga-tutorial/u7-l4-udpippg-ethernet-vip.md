# 以太网 VIP udpIpPg 与 BFM

## 1. 本讲目标

本讲解决一个具体问题：**在仿真里，没有真实的以太网 PHY、没有网线、没有对端节点，怎么给 DUT（被测设计）的 4 个 GMII 口喂进去真实格式的 UDP/IPv4 数据包，又怎么把 DUT 发出来的包接住并检查？**

答案是引入一组「总线功能模型」（Bus Functional Model，BFM）——它们在仿真里扮演**链路对端**和**PHY 芯片**的角色。学完本讲，你应当能够：

- 说清 `bfm_ethernet` 如何把 4 个 `udpIpPg` 包发生器、4 个 `bfm_phy_mdio` 从机、可选的 RGMII 转换器装进一个驱动模块。
- 解释 `udp_ip_pg.v` 这层 HDL 桥：VProc 的通用总线如何「逐字节、逐拍」地驱动 GMII 引脚，以及 `TXD/TXC/TICKS/HLT` 四个地址各自映射到什么。
- 用 `genUdpIpPkt()` / `UdpVpSendRawEthFrame()` / `UdpVpSendIdle()` 这组 C++ API 亲手构造并发送一个 UDP 包。
- 用 `registerUsrRxCbFunc()` 注册接收回调，通过 `rxInfo_t` 拿到对端的源 MAC/IP/端口与载荷。
- 理解 `bfm_phy_mdio` 如何把 MDIO 串行事务译码并映射到共享稀疏内存，从而无需真实 PHY 也能验证 MDIO 配置。

## 2. 前置知识

本讲依赖 Unit 7 前三讲建立的仿真认知，这里只做要点回顾，不重复细节：

- **VProc 虚拟处理器（u7-l2）**：HDL 里留一个空壳，真正的「CPU 程序」是主机原生编译的 C++，通过 DPI-C 把 `write(addr,data)` / `read(addr,&data)` / `tick(n)` 三类请求送进仿真，**每次 write/read 都是一个同步点，推进一拍仿真时间**。每个 VProc 实例（node）对应一个 C 入口 `VUserMain<n>`。
- **mem_model 共享稀疏内存（u7-l1/u7-l3）**：一块 64 位地址的稀疏内存，可被多个 VProc 程序与 HDL 同时访问，是跨节点、跨语言交换数据的「公共黑板」。
- **GMII 接口（u2-l3/u8-l3）**：千兆以太网的介质无关接口，8 位数据 @125 MHz（8 bit × 125 M = 1 Gbps），含 `txd/txen/txer`（发送）与 `rxd/rxdv/rxer`（接收）两组信号。

本讲的**新意**在于：udpIpPg 是 VProc 的**第二种用法**。u7-l2 里 VProc 驱动的是 `soc_if` 这条带地址译码的总线（访问 CSR/DMEM）；而 udpIpPg 里，VProc 驱动的是**最底层的 GMII 物理引脚**——它把 8 根数据线和 2 根控制线直接内存映射进自己的地址空间，**每写/读一次就驱动一拍 GMII 时序**，从而用 C++ 一字节一字节地「手搓」出以太网帧。再加上一层 UDP/IPv4 封装的 C++ 类，就得到了本讲的包发生器 VIP（Verification IP）。

> 术语速查
> - **VIP**：Verification IP，验证知识产权，指可复用的、专为某类接口/协议做验证的现成模型。
> - **BFM**：Bus Functional Model，总线功能模型，在协议层而非字节层模拟一个总线对端。
> - **MDIO**：Management Data Input/Output，以太网 MAC 用来配置/读取 PHY 寄存器的两线串行总线（MDC 时钟 + MDIO 数据），遵循 IEEE 802.3 Clause 22。
> - **RGMII**：Reduced GMII，把 GMII 的 8 位数据 + 控制信号复用到 4 位上、用双边沿采样，引脚更少。本项目 DUT 物理网口走 RGMII，仿真可用参数在 GMII/RGMII 间切换。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [4.sim/models/udpIpPg/bfm_ethernet.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/udpIpPg/bfm_ethernet.sv) | **总装模块**：用 `generate` 循环把 4 个 `udp_ip_pg` 包发生器 + 4 个 `bfm_phy_mdio` 从机 + 可选 `gmii_rgmii_conv` 装成一块四口以太网驱动 BFM。 |
| [4.sim/models/udpIpPg/udp_ip_pg.v](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/udpIpPg/udp_ip_pg.v) | **HDL 桥**：把 VProc 的通用总线译成 GMII 引脚，定义 `TXD/TXC/TICKS/HLT` 四个地址的读写语义。 |
| [4.sim/models/udpIpPg/include/udpIpPg.h](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/udpIpPg/include/udpIpPg.h) | **C++ API 头**：`udpIpPg` 类，声明 `genUdpIpPkt` / `UdpVpSendRawEthFrame` / `registerUsrRxCbFunc` 等方法及 `rxInfo_t` / `udpConfig_t` 结构。 |
| [4.sim/models/udpIpPg/include/udpVProc.h](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/udpIpPg/include/udpVProc.h) | **VProc 驱动基类**：`udpVProc`，实现 `UdpVpSendIdle` / `UdpVpSendRawEthFrame` / `UdpVpExtractRx`，把高层 API 翻译成对 `TXD/TXC` 地址的逐拍读写。 |
| [4.sim/usercode/VUserMainUdp.cpp](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/usercode/VUserMainUdp.cpp) | **示例用户程序**：4 个 node 的入口 `VUserMain1~4`，演示「构造包→发送」与「注册回调→接收」两种用法。 |
| [4.sim/models/bfm_phy_mdio.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/bfm_phy_mdio.sv) | **MDIO 从机模型**：译码 Clause 22 MDIO 事务，映射到 mem_model 共享内存。 |
| [4.sim/models/udpIpPg/README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/udpIpPg/README.md) | udpIpPg VIP 的设计说明。 |

## 4. 核心概念与源码讲解

### 4.1 bfm_ethernet：四口以太网驱动 BFM 的总装

#### 4.1.1 概念说明

仿真里 DUT 的 4 个网口各自需要一个「对端」来收发帧。`bfm_ethernet` 就是把这个对端打包成一个模块：它对外暴露 4 路 `gmii_if`（接 DUT 的 4 个网口）和一组 MDIO 线，对内用 `generate` 循环**实例化 4 份**相同的子结构——每份含一个 `udp_ip_pg`（包发生器）和一个 `bfm_phy_mdio`（PHY 配置从机）。

关键设计取舍：
- **VIP 的 TX 接 DUT 的 RX，VIP 的 RX 接 DUT 的 TX**。即 BFM 在扮演「链路对端」，方向与 DUT 相反。这一点在 `gmii_if.SLV` modport 的连接里体现（见 4.1.3）。
- **GMII / RGMII 可参数切换**。物理板卡上网口走 RGMII（4 位双边沿），但仿真包发生器天然产 GMII（8 位）。参数 `RGMII` 非 0 时插入 `gmii_rgmii_conv` 做转换，否则直通。

#### 4.1.2 核心流程

```text
                bfm_ethernet (NUM_PORTS=4, START_NODE=1)
        ┌──────────────────────────────────────────────────────┐
        │  generate for UDP = 0..3:                            │
        │    ┌─────────────┐    gmii[UDP]      ┌────────────┐  │
        │    │ udp_ip_pg   │◄────────────────► │  DUT 网口  │  │
        │    │ NODE=1+UDP  │   (TX/RX 交叉)    │  eth1..4   │  │
        │    └─────────────┘                   └────────────┘  │
        │    ┌─────────────┐    mdio[UDP]                      │
        │    │bfm_phy_mdio │◄────────────────► (DUT 的 MDIO 主)│
        │    │ NODE=1+UDP  │                                   │
        │    └─────────────┘                                   │
        │  halt_req = |halt  (任一 node 停机则整体请求停机)     │
        └──────────────────────────────────────────────────────┘
```

`START_NODE=1` 意味着 4 个包发生器占用 VProc 的 node 1~4（node 0 留给 `soc_cpu`，见 u7-l2）。`halt_req` 是 4 个 node 停机信号的「或」，可在测试台里用作仿真结束条件。

#### 4.1.3 源码精读

先看模块端口与参数：4 路 `gmii_if.SLV`、MDIO 双向线、可选的 Verilator 专用拆分线、以及停机请求。

[4.sim/models/udpIpPg/bfm_ethernet.sv:44-68](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/udpIpPg/bfm_ethernet.sv#L44-L68) — 模块声明。`START_NODE` 决定 VProc node 起始号；`MDIO_BUFF_ADDR`（默认 `0x5000_0000`）是 MDIO 从机在共享内存里的基址；`RGMII` 默认 1。

核心是这段 `generate` 循环，对每个端口实例化包发生器和 MDIO 从机：

[4.sim/models/udpIpPg/bfm_ethernet.sv:138-170](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/udpIpPg/bfm_ethernet.sv#L138-L170) — 每个端口例化一个 `udp_ip_pg`（`.NODE(START_NODE+UDP)`，故 node 号为 1/2/3/4）和一个 `bfm_phy_mdio`。注意 MDIO 从机的内存基址逐端口偏移 256 字节：`.MDIO_BUFF_ADDR(MDIO_BUFF_ADDR + UDP*256)`，让 4 个 PHY 的寄存器区互不重叠。

RGMII 模式下的方向交叉在这里最清楚：

[4.sim/models/udpIpPg/bfm_ethernet.sv:95-135](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/udpIpPg/bfm_ethernet.sv#L95-L135) — RGMII 分支把 VIP 的 GMII 发送（`udp_gmii_txd/txen/txer`）经转换器接到 DUT 接口的 `rxd/rxdv`（DUT 的接收方向）；GMII 直通分支同理（`gmii[UDP].rxd = udp_gmii_txd[UDP]`）。**这正印证了「VIP 的 TX 即 DUT 的 RX」**。

#### 4.1.4 代码实践

**实践目标**：看清 4 个 node 的编号与 MDIO 内存分区。

1. 打开 `4.sim/models/udpIpPg/bfm_ethernet.sv`，定位 `generate` 循环。
2. 推算每个端口的 `udp_ip_pg` 的 `NODE` 值与 `bfm_phy_mdio` 的 `MDIO_BUFF_ADDR` 值，填入下表：

| 端口 UDP | udp_ip_pg NODE | bfm_phy_mdio NODE | MDIO 内存基址 |
|----------|----------------|-------------------|---------------|
| 0 (eth1) | ?              | ?                 | ?             |
| 1 (eth2) | ?              | ?                 | ?             |
| 2 (eth3) | ?              | ?                 | ?             |
| 3 (eth4) | ?              | ?                 | ?             |

3. **预期结果**：NODE 依次为 1/2/3/4；MDIO 基址依次为 `0x5000_0000 / 0x5000_0100 / 0x5000_0200 / 0x5000_0300`（每次 +256 = 0x100）。
4. 若想验证连接方向，可读 `tb.sv` 中 `bfm_ethernet` 的例化，确认 `gmii[PORT].txen = e<PORT>_txen`（DUT 的发送送进 BFM 的 gmii 接口）。

#### 4.1.5 小练习与答案

**练习**：为什么 `bfm_ethernet` 要把 4 个 `halt` 信号「或」起来作为一个 `halt_req` 输出，而不是单独引出 4 根？

**答案**：测试台通常希望「所有以太网驱动都跑完才算仿真结束」。把 4 个停机信号或起来，tb 只需检测一根 `halt_req` 即可判断 4 个 node 是否全部完成；若要单独知道哪个 node 结束，再读各 node 状态即可。这是「整体结束条件」与「个体状态」的分工。

---

### 4.2 udp_ip_pg.v：VProc 通用总线如何驱动 GMII 引脚

#### 4.2.1 概念说明

`udp_ip_pg.v` 是整条 VIP 链的**底层**。它的职责非常薄：把 VProc 那条「带地址/数据/读写的通用总线」**翻译成 GMII 的物理引脚电平**。它本身**不懂 UDP、不懂 IP、不懂以太网帧格式**——那些是上层 C++ 类的事。它只认识 4 个地址：

| 地址宏 | 偏移 | 写（WE=1）含义 | 读（RD=1）返回 |
|--------|------|----------------|----------------|
| `TXD_ADDR` | 0x0 | 设置发送数据 `txd[7:0]` | 返回接收数据 `rxd[7:0]` |
| `TXC_ADDR` | 0x1 | 设置 `txen`(bit0)、`txer`(bit1) | 返回 `{rxer, rxdv}`(2 位) |
| `TICKS_ADDR` | 0x2 | —（无副作用） | 返回自由运行计数 `count` |
| `HLT_ADDR` | 0x3 | 设置 `halt` 输出 | — |

注意一个**易混淆点**：地址名虽叫 `TXD_ADDR`/`TXC_ADDR`，但**读它们返回的是接收方向的引脚电平**（`rxd` / `{rxer,rxdv}`）。也就是说同一个地址，写时驱动发送引脚、读时采样接收引脚——这是把收发复用到一处的设计。

#### 4.2.2 核心流程

VProc 每发起一次 `write` 或 `read`，HDL 侧的 `Update` 信号就被脉冲一次；`always @(Update)` 块据此把 `DataOut`（VProc 想写的值）搬到对应引脚寄存器，或把引脚当前电平拼进 `DataIn` 回送给 VProc，最后**翻转 `UpdateResponse`** 表示「本次访问完成」。

```text
VProc(C++) ──write(TXD, byte)──► Update 脉冲 ──► txd<=byte; 翻转 UpdateResponse
VProc(C++) ──read(TXD)────────► Update 脉冲 ──► DataIn={24'h0,rxd}; 翻转 UpdateResponse
                                                   (count 在 posedge clk 自增，作为节拍计数)
```

由于一次 VProc 访问 = 一拍同步点，**C++ 用一连串 write/read 就能逐字节、逐拍地手工驱动 GMII 时序**——这正是 u7-l2「lock-step 协同仿真」的又一次落地。

#### 4.2.3 源码精读

地址定义与模块端口：

[4.sim/models/udpIpPg/udp_ip_pg.v:35-59](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/udpIpPg/udp_ip_pg.v#L35-L59) — 4 个地址宏、`NODE` 参数、以及 GMII 收发引脚（`txd/txen/txer` 为 `output reg`，`rxd/rxdv/rxer` 为 `input`）。

核心译码与应答逻辑：

[4.sim/models/udpIpPg/udp_ip_pg.v:133-187](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/udpIpPg/udp_ip_pg.v#L133-L187) — `always @(Update)` 内的 `case(Addr)`：`TXD_ADDR` 分支 `DataIn={24'h0,rxd_int}` 且写时 `txd=DataOut[7:0]`；`TXC_ADDR` 分支 `DataIn={30'h0,rxc_int}` 且写时 `txen=DataOut[0], txer=DataOut[1]`；末行 `UpdateResponse=~UpdateResponse` 是完成握手。`rxd_int/rxc_int` 是为消除上升沿竞争而延迟一拍的采样版（见下条）。

消除竞争的采样延迟与 VProc 例化：

[4.sim/models/udpIpPg/udp_ip_pg.v:84-103](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/udpIpPg/udp_ip_pg.v#L84-L103) — 非 Verilator 时用 `assign #1` 给 `rxd/rxc` 加 1ps 延迟得 `rxd_int/rxc_int`；Verilator 下改用 `negedge clk` 采样，规避同一上升沿上「更新输入」与「同步进程读取」的顺序竞争。

[4.sim/models/udpIpPg/udp_ip_pg.v:194-220](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/udpIpPg/udp_ip_pg.v#L194-L220) — 例化 VProc 实体 `vp`，把 `Addr/WE/RD/DataOut/DataIn/Update/UpdateResponse` 与上面逻辑对接，`Node` 取 `NODE[3:0]`。注意 `.WRAck(WE)` / `.RDAck(RD)` 直接把请求信号当应答——因为本设计每次访问当拍即完成。

#### 4.2.4 代码实践

**实践目标**：体会「一次 VProc 访问 = 一拍 GMII 时序」。

1. 假设 C++ 想在 GMII 上发送字节 `0x55`（前导码），它会做：`VWrite(TXD_ADDR, 0x55, ...)` 接着 `VWrite(TXC_ADDR, TX_CTRL_VALID, ...)`。
2. 在 `udp_ip_pg.v` 里追踪这两次写：分别落入 `TXD_ADDR` 分支（`txd<=0x55`）和 `TXC_ADDR` 分支（`txen<=1`）。
3. **需要观察的现象**：每次 write 后 `UpdateResponse` 翻转一次，仿真推进一拍；连续写 N 个字节即驱动 N 拍 GMII 发送时序。
4. **预期结果**：若在波形里看 `txd/txen`，会看到字节逐拍变化，与真实 GMII 发送波形一致。**待本地验证**（需跑仿真出波形）。

#### 4.2.5 小练习与答案

**练习 1**：为什么读 `TICKS_ADDR` 返回的是 `count`，而 `count` 只在 `posedge clk` 自增、与 `Update` 无关？

**答案**：`count` 是一个**自由运行的时钟节拍计数器**（[udp_ip_pg.v:123-126](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/udpIpPg/udp_ip_pg.v#L123-L126)），给 C++ 提供一个「墙上时钟」。C++ 用它在两次实际收发之间做延时（如「发 125000 拍 idle 等于 1 ms 初始化」），见 4.3。

**练习 2**：若 C++ 向一个未定义的地址（如 0x4）发起访问，会发生什么？

**答案**：落入 `default` 分支，`$display` 报 `***ERROR: udp_ip_pg---access to invalid address` 并 `$finish` 终止仿真（[udp_ip_pg.v:178-181](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/udpIpPg/udp_ip_pg.v#L178-L181)）。这是一种 fail-fast 保护，避免错误地址悄悄静默。

---

### 4.3 包生成 API：构造并发送一个 UDP/IPv4 帧

#### 4.3.1 概念说明

`udp_ip_pg.v` 只懂「写一个字节到 TXD、写控制位到 TXC」，离「一个合法的 UDP/IPv4 以太网帧」还很远。C++ 类 `udpIpPg`（继承自 `udpVProc`）就在这层之上，提供高层 API：

- `genUdpIpPkt(cfg, frm_buf, payload, payload_len)`：**离线**把载荷封装成完整的「前导码+SFD+以太网头+IPv4 头+UDP 头+载荷+CRC」帧，写进 `frm_buf`，返回帧长。此时**还不碰 GMII**。
- `UdpVpSendRawEthFrame(frame, len)`：把 `frm_buf` 里的帧**逐字节**写进 `TXD/TXC` 地址，每字节推进一拍，真正把帧打到 GMII 上。
- `UdpVpSendIdle(ticks)`：不发帧时，持续写 idle 符号保持链路活，并顺带轮询接收（见 4.4）。

封装所需的 CRC32、IP 校验和、UDP 校验和等计算都在预编译库 `lib/libudplnx.a`（Linux）/`libudpwin.a`（Windows）里，由 Makefile 链接（[4.sim/MakefileVProc.mk](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/MakefileVProc.mk) 里 `UDPLDOPT=-ludplnx`）。

#### 4.3.2 核心流程

```text
发送一个 UDP 包的三步：

  ①  udpIpPg pUdp(node, my_ip, my_mac, my_port);   // 构造对象，绑定本 node 身份
  ②  frameLen = pUdp.genUdpIpPkt(cfg, frmBuf, payload, len);  // 离线封装成帧
  ③  pUdp.UdpVpSendRawEthFrame(frmBuf, frameLen);   // 逐字节打到 GMII

其中 UdpVpSendRawEthFrame 内部（逐字节循环）：
  for each byte in frame:
      VWrite(TXD_ADDR, byte & 0xff)     // 设置 txd
      VWrite(TXC_ADDR, TX_CTRL_VALID)   // 拉高 txen（或首尾特殊控制）
      UdpVpExtractRx()                  // 顺带采样一拍 rxd/rxc（见 4.4）
  UdpVpSendIdle(1)                      // 帧间间隔
```

`UdpVpSendIdle` 同理：写 `IDLE` 符号到 TXD、写 `TX_CTRL_IDLE` 到 TXC，然后循环 `ticks` 次、每次读 `TICKS_ADDR` 取节拍并 `UdpVpExtractRx()`。

#### 4.3.3 源码精读

高层 API 声明：

[4.sim/models/udpIpPg/include/udpIpPg.h:108-125](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/udpIpPg/include/udpIpPg.h#L108-L125) — 构造函数绑定本节点的 IP/MAC/UDP 端口身份；`genUdpIpPkt` 的参数：配置结构 `cfg`（目的 MAC/IP/端口）、帧缓冲 `frm_buf`、载荷指针与长度，返回帧总长。

发送的逐字节实现（基类 `udpVProc`）：

[4.sim/models/udpIpPg/include/udpVProc.h:118-144](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/udpIpPg/include/udpVProc.h#L118-L144) — `UdpVpSendRawEthFrame`：对 `frame[idx]` 写 `TXD_ADDR`（低 8 位）与 `TXC_ADDR`（错误标志优先，否则 `TX_CTRL_VALID`），每字节后调 `UdpVpExtractRx()` 推进一拍并采样接收；末尾补一拍 idle。

idle 与停机：

[4.sim/models/udpIpPg/include/udpVProc.h:96-113](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/udpIpPg/include/udpVProc.h#L96-L113) — `UdpVpSendIdle(ticks)`：写 idle 符号与 idle 控制，循环 `ticks` 次读 `TICKS_ADDR` 并采样接收。

完整范例（node 1 发给 node 2）：

[4.sim/usercode/VUserMainUdp.cpp:74-94](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/usercode/VUserMainUdp.cpp#L74-L94) — `VUserMain1`：构造 `udpIpPg pUdp(1, NODE1_IP, NODE1_MAC, NODE1_PORT)`；发 125000 拍 idle（≈1 ms）等初始化；填充 64 字节递增载荷；填 `cfg` 指向 node2 的 MAC/IP/端口；`genUdpIpPkt` 得到 `frameLen`；`UdpVpSendRawEthFrame` 发出；之后死循环发 idle 保持链路。

#### 4.3.4 代码实践

**实践目标**：用 udpIpPg API 写一段「构造并发送一个 UDP 包」的代码骨架（基于真实 API，标注为示例代码）。

```cpp
// ===== 示例代码：在某个 VUserMain<n> 里发送一个 UDP 包 =====
extern "C" void VUserMain1(void) {
    // 1) 构造对象，绑定本 node 身份（IP/MAC/端口）
    udpIpPg pUdp(1, 0xc0a81908ULL, 0xd89ef3887ec3ULL, 0x0400);

    // 2) 等待链路对端初始化（约 1ms = 125000 拍 @125MHz）
    for (int i = 0; i < 125000; ++i) pUdp.UdpVpSendIdle(1);

    // 3) 准备载荷（64 字节）
    static uint32_t payload[2*1024], frmBuf[2*1024];
    for (uint32_t i = 0; i < 64; ++i) ((uint8_t*)payload)[i] = (uint8_t)i;

    // 4) 配置目的 MAC/IP/端口
    udpIpPg::udpConfig_t cfg;
    cfg.mac_dst_addr = 0x90324b070bd1ULL;
    cfg.ip_dst_addr  = 0xc0a89801;
    cfg.dst_port     = 0x0401;

    // 5) 离线封装成帧，再逐字节打到 GMII
    uint32_t frameLen = pUdp.genUdpIpPkt(cfg, frmBuf, payload, 64);
    pUdp.UdpVpSendRawEthFrame(frmBuf, frameLen);

    // 6) 之后持续 idle 保持链路
    while (true) pUdp.UdpVpSendIdle(1);
}
```

**操作步骤**：把上述骨架与 `VUserMainUdp.cpp` 对照；确认每一步对应的 API 名与参数顺序与 `udpIpPg.h` 一致。
**预期结果**：这是一段可编译的骨架；真正运行需链接 `libudplnx.a` 并在 `make -f MakefileVProc.mk run` 下跑仿真，输出会出现 `frameLen = N` 与 `frame sent` 日志。**待本地验证**。

#### 4.3.5 小练习与答案

**练习**：`genUdpIpPkt` 与 `UdpVpSendRawEthFrame` 被故意拆成两步（先离线封装、再在线发送），而不是合二为一。这样做有什么好处？

**答案**：① **可检查性**——封装好的帧在 `frm_buf` 里，可在发送前打印或断言其字段（如目的 MAC、CRC）正确；② **可复用**——同一帧可多次重发（如压测），不必每次重算；③ **职责分离**——封装是纯计算（与仿真时间无关），发送才推进仿真节拍，分离后更易推理时序。

---

### 4.4 接收回调机制：registerUsrRxCbFunc 与 rxInfo_t

#### 4.4.1 概念说明

VIP 既能发也能收。接收不靠轮询缓冲区，而用**回调（callback）**：用户注册一个函数 `rxCallback(rxInfo_t info, void* hdl)`，每当 VIP 在 GMII 上完整收到一个合法帧、解析出 MAC/IP/UDP 头后，就调用它，把源地址、端口、载荷等信息打包进 `rxInfo_t` 传给你。

这套机制之所以能工作，关键在 `udpVProc::UdpVpExtractRx()`：因为发送/空闲时每拍都要「读 `TXD_ADDR` 取 `rxd`、读 `TXC_ADDR` 取 `rxc`」推进仿真，**接收采样是搭在发送/空闲的便车上的**——每驱动一拍 GMII，顺带就把 DUT 发出来的接收字节收进缓冲，攒够一帧就回调。

#### 4.4.2 核心流程

```text
每拍（发送字节或发 idle 时都会调用 UdpVpExtractRx）：
  读 rxc（含 rxdv/rxer）
  若 rxdv 由 0→1：开始收帧，清缓冲索引
  若正在收帧且 rxdv=1：把 rxd 字节压入 rx_buf（遇 rxer 置错误标志）
  若 rxdv 由 1→0：帧结束
      剥掉前导码/SFD
      若无错误：processFrame(rx_buf, len)
          └─ 解析 Eth/IPv4/UDP 头，填充 rxInfo_t
          └─ 调用 usrRxCbFunc(info, hdl)   ← 用户的回调在这里被触发
```

`rxInfo_t` 字段（来自 `udpIpPg.h`）：`mac_src_addr`(源 MAC)、`ipv4_src_addr`(源 IP)、`udp_src_port`/`udp_dst_port`、`rx_payload[]`(载荷，最大 ETH_MTU=1500)、`rx_len`(载荷长度)。

#### 4.4.3 源码精读

回调注册与类型定义：

[4.sim/models/udpIpPg/include/udpIpPg.h:79-122](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/udpIpPg/include/udpIpPg.h#L79-L122) — `rxInfo_t` 结构（含 `rx_payload[ETH_MTU]`）、回调函数指针类型 `pUsrRxCbFunc_t`、以及 `registerUsrRxCbFunc`（把函数指针与可选 `hdl` 句柄存起来）。

接收采样的逐拍实现：

[4.sim/models/udpIpPg/include/udpVProc.h:157-229](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/udpIpPg/include/udpVProc.h#L157-L229) — `UdpVpExtractRx`：读 `TXD_ADDR` 得 `rxd`、读 `TXC_ADDR` 得 `rxc`；用 `RX_VALID_MASK`/`RX_ERROR_MASK` 判断帧起止与错误；帧结束时剥前导/SFD，无错则调虚函数 `processFrame`（由 `udpIpPg` 实现，解析头部并回调）。注意最大帧保护：超过 `ETH_MTU+各头` 仍未见帧尾则告警截断。

真实范例（node 2 注册回调接收）：

[4.sim/usercode/VUserMainUdp.cpp:57-69](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/usercode/VUserMainUdp.cpp#L57-L69) — `rxCallback`：用 `VPrint` 打印源 MAC、源 IP、源/目的端口与载荷十六进制。

[4.sim/usercode/VUserMainUdp.cpp:99-104](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/usercode/VUserMainUdp.cpp#L99-L104) — `VUserMain2`：构造 `udpIpPg pUdp(2, ...)`，`pUdp.registerUsrRxCbFunc(rxCallback, nullptr)` 注册回调，然后死循环 `UdpVpSendIdle(1)`——**接收方什么都不发，只是持续 idle 以驱动采样**。node 4 同理（`VUserMain4`）。

#### 4.4.4 代码实践

**实践目标**：写一段「注册回调、打印收到的源地址与载荷」的骨架（示例代码）。

```cpp
// ===== 示例代码：接收方回调，打印源地址与载荷 =====
static void myRxCallback(udpIpPg::rxInfo_t info, void* /*hdl*/) {
    VPrint("收到包: 源MAC=%012lX 源IP=%08X 源端口=%04X 目的端口=%04X 载荷%u字节\n",
           (unsigned long)info.mac_src_addr, info.ipv4_src_addr,
           info.udp_src_port, info.udp_dst_port, info.rx_len);
    for (uint32_t i = 0; i < info.rx_len; ++i) {
        VPrint(" %02X", info.rx_payload[i]);
        if ((i & 0xF) == 0xF) VPrint("\n");
    }
}

extern "C" void VUserMain2(void) {
    udpIpPg pUdp(2, 0xc0a89801, 0x90324b070bd1ULL, 0x0401);
    pUdp.registerUsrRxCbFunc(myRxCallback, nullptr);  // 注册回调
    while (true) pUdp.UdpVpSendIdle(1);               // 持续 idle 驱动接收采样
}
```

**操作步骤**：对照 `VUserMainUdp.cpp` 的 `rxCallback`/`VUserMain2`，确认回调签名与 `rxInfo_t` 字段名一致。
**需要观察的现象**：当 node 1 发出包、经 DUT 转发到 node 2 的网口后，node 2 的 `myRxCallback` 被触发，打印出 node 1 的源地址与 64 字节递增载荷。
**预期结果**：日志形如 `源MAC=D89EF3887EC3 源IP=C0A81908 ...`（与 [4.sim/README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/README.md) 的示例控制台输出一致）。**待本地验证**。

#### 4.4.5 小练习与答案

**练习**：接收方 `VUserMain2` 的主循环是 `while (true) pUdp.UdpVpSendIdle(1);`——它明明在「发 idle」，为什么却能收到包？

**答案**：因为 `UdpVpSendIdle` 内部每拍都调用 `UdpVpExtractRx()` 采样 GMII 接收引脚（`rxd/rxc`）。所谓「发 idle」只是把 TX 方向置成空闲符号，**RX 方向的采样照常进行**。这正是「接收搭发送/空闲便车」的设计：只要 VIP 在推进节拍（无论发数据还是发 idle），就在持续收帧。

---

### 4.5 MDIO slave 模型：bfm_phy_mdio 与共享内存寄存器映射

#### 4.5.1 概念说明

DUT 的以太网 MAC 会通过 MDIO 总线去配置/读取板载 PHY（如 Realtek）的寄存器（链路速率、自协商等）。仿真里没有真 PHY，`bfm_phy_mdio` 就扮演这个 **MDIO 从机**：它监听 MDIO 串行线，按 IEEE 802.3 Clause 22 协议译码出「读/写 + 端口地址 + 寄存器号 + 数据」，然后：

- **写事务**：把数据存进一块 mem_model 共享内存，并打印是哪个寄存器被写。
- **读事务**：从同一块共享内存取数据返回给 MAC，并打印。

妙处在于「共享内存」：测试程序（如 `soc_cpu.VPROC` 上的代码）可以**预先**往这块内存写好期望的读返回值，DUT 一读就「正好」拿到；也可以在 DUT 写完后去内存里**核对** MAC 写了什么。这样无需真 PHY 就完成了 MDIO 双向验证，闭环。

#### 4.5.2 核心流程

```text
MDIO 串行帧（Clause 22）：
  32 个 1（前导） + 01（起始） + OP(2) + PHYAD(5) + REGAD(5) + TA(2) + DATA(16)

bfm_phy_mdio 持续把 mdio 位移入 shiftin；
  start = 连续 32 个 1 之后出现 01         → 锁定一个事务，count=29 倒数
  count==19 时：已收完 OP+PHYAD+REGAD       → 译码命令与地址，计算 memaddr
      若 OP=读：拉高 memrd、使能 mdio 输出（准备回数据）
      若 OP=写：等数据收完
      若 OP 非法：报错中止
  count==1 时：数据收完
      若 OP=写：拉高 memwr，把 16 位数据按字写入 mem_model
  读返回数据由 mem_model 下一拍给出（memrdatavalid），装入 shiftout 串行移出
```

寄存器号 0~31 对应 Clause 22 标准寄存器（Control/Status/PHY ID/自协商…），代码里用 `regname[]` 字符串数组给每个号配了可读名字，打印时一并显示。

#### 4.5.3 源码精读

模块与命令常量：

[4.sim/models/bfm_phy_mdio.sv:43-59](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/bfm_phy_mdio.sv#L43-L59) — 模块端口（`clk/arst_n/mdio`，Verilator 下把双向 `mdio` 拆成 `mdio_en/mdio_out`）；`MDIO_CMD_WR=2'b01`、`MDIO_CMD_RD=2'b10`。

事务检测与地址译码（核心时序）：

[4.sim/models/bfm_phy_mdio.sv:116-182](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/bfm_phy_mdio.sv#L116-L182) — `start` 检测（32 个 1 + 01）；`count` 倒数；`count==19` 时取 `cmd19`/`addr19`，读命令置 `memrd`+使能输出、非法命令报错中止，并按 `memaddr <= {MDIO_BUFF_ADDR[31:14], 2'b00, addr19, 2'b00}` 把 16 位 MDIO 地址对齐到 32 位字边界。其中 `addr19={shiftin[8:0], mdio}` 是 10 位「PHYAD(5)+REGAD(5)」。

写数据与读返回（含格式化打印）：

[4.sim/models/bfm_phy_mdio.sv:194-232](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/bfm_phy_mdio.sv#L194-L232) — `count==1` 时若为写命令，拼出 16 位 `memwdata={16'h0, shiftin[14:0], mdio}` 并置 `memwr`；写/读发生时 `$display` 打印 `Wrote/Read DATA ... to/from PORT ... ADDR ... [寄存器名]`，端口取 `memaddr[11:7]`、寄存器号取 `memaddr[6:2]`。

mem_model 例化（共享内存后端）：

[4.sim/models/bfm_phy_mdio.sv:238-269](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/bfm_phy_mdio.sv#L238-L269) — 例化 `mem_model`：读写口接 `memaddr/memwdata/memwr/memrd/memrdata/memrdatavalid`，`byteenable=4'h3`（只写低 16 位到 32 位字）；其余写口与突发口全部禁用。这正是把 MDIO 寄存器「挂」到共享稀疏内存上的连接点。

#### 4.5.4 代码实践

**实践目标**：理解「DUT 经 MDIO 读 PHY 寄存器」时数据从何而来。

1. 假设 DUT 的 MAC 要读 PHY 的 Status 寄存器（REGAD=1），期望返回 `0xFFFF`。
2. 测试程序（在 `soc_cpu.VPROC` 或任意 VProc node 上）**预先**往共享内存写值：地址 = `MDIO_BUFF_ADDR + (REGAD 对应字偏移)`。由 `memaddr={MDIO_BUFF_ADDR[31:14],2'b00,addr19,2'b00}`、`addr19={PHYAD,REGAD}` 可算出字地址。
3. DUT 发起 MDIO 读 → `bfm_phy_mdio` 译码 → 置 `memrd` → `mem_model` 返回预置值 → 移位送回 DUT。
4. **需要观察的现象**：仿真日志出现 `bfm_phy_mdio(N): Read DATA 0xffff from PORT ... ADDR ... [Status]`。
5. **预期结果**：DUT 读到的正是测试程序预置的值，闭环成立。具体地址推导**待本地验证**（建议在仿真里打印 `memaddr` 对照）。

#### 4.5.5 小练习与答案

**练习**：`bfm_ethernet` 给 4 个 `bfm_phy_mdio` 各分配 256 字节的内存偏移（`MDIO_BUFF_ADDR + UDP*256`），但 Clause 22 只有 32 个寄存器、每寄存器 2 字节，理论上 64 字节就够。为什么留 256 字节？

**答案**：从 `memaddr` 的拼接 `{MDIO_BUFF_ADDR[31:14], 2'b00, addr19, 2'b00}` 看，`addr19` 是 10 位（PHYAD5+REGAD5），其后补 2 位 0、再按字对齐，单端口最大可寻址 `10 位 × 4 字节 = 4096` 字节区间。留 256 字节是一个**保守的无重叠分区**（保证 4 端口的可寻址范围互不踩踏），同时每端口区间内寄存器号 `memaddr[6:2]`（5 位 = 0~31）正好覆盖 Clause 22 全部 32 个标准寄存器。分区宁可宽松以避免跨端口串扰。

---

## 5. 综合实践：搭一条「node1 发 → DUT 转发 → node2 收」的端到端验证

把本讲三块内容（包生成、接收回调、MDIO 从机）串成一个完整的端到端以太网验证任务。

**任务背景**：DUT 是 wireguard-fpga 的 4 口 SoC。你希望在仿真里证明「从 eth1 进来的 UDP 包，能被 DPE（当前是 dummy_switch 直通）原样从 eth2 转发出去」。

**操作步骤**：

1. **分配角色**：node1（`VUserMain1`）扮 eth1 的对端发送方；node2（`VUserMain2`）扮 eth2 的对端接收方。沿用 `VUserMainUdp.cpp` 的现有实现即可。
2. **配置发送方**：在 `VUserMain1` 里设 `cfg` 的目的 MAC/IP/端口指向 node2；载荷用 64 字节递增序列。先发足够长的 idle（≈1 ms）等 DUT 启动。
3. **配置接收方**：在 `VUserMain2` 里 `registerUsrRxCbFunc` 注册一个回调，回调里**断言**收到的载荷等于发送的递增序列、源地址等于 node1。
4. **MDIO 侧（可选扩展）**：让 DUT 在启动时经 MDIO 读 PHY 的 Status 寄存器；预先在 node2 对应的 `0x5000_0100 + (REGAD<<2)` 共享内存处写好「链路 up」的值，验证 DUT 能读到。
5. **运行**：`make -f MakefileVProc.mk run`（默认编译 `VUserMain0.cpp` 作 soc_cpu、`VUserMainUdp.cpp` 作 4 个以太网 node）。

**需要观察的现象**：
- node1 日志出现 `frame sent to Node2`。
- node2 的回调被触发，打印出 node1 的源 MAC/IP 与完整 64 字节载荷。
- （若做 MDIO 扩展）`bfm_phy_mdio` 日志出现对应端口的读事务与预置值。

**预期结果**：收到的载荷字节序与发送完全一致，证明 DUT 的 eth1→eth2 转发通路（在当前 Phase1 PoC 下即 dummy_switch 直通）功能正确。这是后续接入真实 WG 加解密流水线后、做加密隧道端到端验证的基础设施。**待本地验证**（需 Verilator + VProc 环境）。

## 6. 本讲小结

- `bfm_ethernet` 是四口以太网驱动 BFM 的**总装**：用 `generate` 循环把 4 个 `udp_ip_pg` 包发生器 + 4 个 `bfm_phy_mdio` 从机 + 可选 `gmii_rgmii_conv` 装在一起，占用 VProc node 1~4（node 0 留给 soc_cpu）。
- `udp_ip_pg.v` 是一层很薄的 **HDL 桥**：把 VProc 通用总线译成 GMII 引脚，靠 `TXD/TXC/TICKS/HLT` 四个地址实现「写驱动发送引脚、读采样接收引脚」，一次 VProc 访问即一拍 GMII 时序。
- **包生成**分两步：`genUdpIpPkt` 离线封装完整以太网+IPv4+UDP 帧，`UdpVpSendRawEthFrame` 逐字节打到 GMII；不发帧时用 `UdpVpSendIdle` 保持链路。
- **接收**靠回调：`registerUsrRxCbFunc` 注册的函数会在每收到一帧时被调用，源 MAC/IP/端口/载荷打包在 `rxInfo_t` 里；接收采样搭在发送/idle 的逐拍推进上。
- `bfm_phy_mdio` 是 **MDIO 从机模型**：译码 Clause 22 串行事务，把每个寄存器映射到 mem_model 共享内存，从而无需真 PHY 即可双向验证 MDIO 配置。
- 关键设计思想：**VIP 的 TX 即 DUT 的 RX**（方向交叉），以及**接收搭发送便车**（每驱动一拍就采样一拍）——两者合起来让纯 C++ 测试程序就能驱动完整以太网数据通路。

## 7. 下一步学习建议

- **u7-l5（mem_model 稀疏内存、PCAP 回放与逐模块测试台）**：本讲的 MDIO 从机与 udpIpPg 都重度依赖 mem_model 共享内存；下一讲会讲清这块「公共黑板」的稀疏存储原理，以及如何用 PCAP 文件做更接近真实抓包的端到端回放/录制验证。
- **实践延伸**：试着把综合实践里的 node1→node2 直通验证，改成在回调里**比对仿真 PCAP 与实网抓包**（呼应 u8-l4 两节点实验室验证），理解仿真与实网的对应关系。
- **源码延伸阅读**：想了解 RGMII 双边沿采样的细节可读 [4.sim/models/udpIpPg/gmii_rgmii_conv.v](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/udpIpPg/gmii_rgmii_conv.v)；想看 udpIpPg VIP 的官方设计图与 `halt` 信号用法可重读 [4.sim/models/udpIpPg/README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/udpIpPg/README.md)。
