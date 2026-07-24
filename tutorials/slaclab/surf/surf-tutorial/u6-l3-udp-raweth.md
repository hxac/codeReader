# UDP 引擎与原始以太网成帧

## 1. 本讲目标

本讲承接 u6-l2 的 IPv4 引擎。`IpV4Engine` 已经能把进来的 IPv4 帧按协议号分发出去、把子引擎送出的数据加上 IP 头发出去——但「谁来消费协议号 0x11（UDP）那一槽」它并不关心。真正填上这个位置、让 FPGA 成为一个能收发 UDP 数据报的节点的是本讲的 `UdpEngine`；而当你完全不想用 IPv4/UDP、只想在二层（MAC 层）裸跑自定义协议时，本讲的 `RawEthFramer` 提供了一条更轻的通路。

学完本讲你应当能够：

1. 说清 `UdpEngine` 的「服务器 / 客户端」双模型：服务器被动监听固定端口、从入向帧里「学到」对端的 MAC/IP/端口；客户端主动外发、远端 IP/端口由配置给出、靠 ARP 表把 IP 翻译成 MAC。
2. 跟踪 `UdpEngineRx` 如何用本地端口号在入向帧里做端口路由、`UdpEngineTx` 如何把应用数据封装成 u6-l2 定义的「非标准 IPv4 伪头部 + UDP 头」。
3. 描述 `UdpEngineDhcp` 的 Discover→Offer→Request→ACK 状态机、租约与续约定时器。
4. 读懂 `RawEthFramer` 如何给应用数据加上/剥掉「目的 MAC + 源 MAC + EtherType」二层头，并用一个小缓存保证短帧也能被正确成帧。
5. 在 `UdpEngineWrapper` 的 AXI-Lite 寄存器空间里写出每个客户端通道的「远端端口 / 远端 IP」布局。

## 2. 前置知识

进入源码前，先建立四条直觉。前三条在 u6-l1 / u6-l2 已建立，这里只做必要回顾。

**第一，以太网流是「小端字节排列的 128 位 AXI-Stream」，端口与 IP 都按 big-Endian 配置。** 如 u6-l1 所述，帧首字节落在 `tData` 最低位，所以一个端口号 `8192 = 0x2000` 在线缆上是「高字节 0x20 在前、低字节 0x00 在后」，存进 16 位 `tData` 字段时就成了 `x"0020"`——「看起来反序」。`EthMacPkg` 专门提供 [`EthPortArrayBigEndian`](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacPkg.vhd#L161-L173) 把整数端口号批量翻转成这种 big-Endian 的 `slv`，DHCP 端口常量 [`DHCP_CPORT = x"4400"`（68）、`DHCP_SPORT = x"4300"`（67）](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacPkg.vhd#L40-L41) 也是同理。本讲看到的所有「端口/IP 常量看起来反序」都源于此。

**第二，UDP 的协议号是 `0x11`，校验和与长度都不在 `UdpEngine` 里算。** [`UDP_C = x"11"`](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacPkg.vhd#L34) 就是 IPv4 头里的协议字段。UDP 头里的「UDP 长度」和「UDP 校验和」两栏在 `UdpEngineTx` 里被故意填 0，由下游 `EthMacCore` 的校验和卸载逻辑补上（见 `UdpEngineTx.vhd` / `UdpEngineRx.vhd` 文件头注释 “UDP checksum checked in EthMac core”）。

**第三，子引擎与 `IpV4EngineTx` 之间传递的是「非标准 IPv4 伪头部」。** u6-l2 已详述：首拍含 `目的MAC(47:0) + 保留(63:48) + 源IP(95:64) + 目的IP(127:96)`，第二拍含 `协议号 + 伪头长度 + 源端口 + 目的端口 + UDP长度 + UDP校验和 + 数据`。`UdpEngineTx` 正是按这份约定去**填**这个头部、`UdpEngineRx` 正是按这份约定去**读**它。这是本讲与 u6-l2 的直接接口。

**第四，本讲新增一个概念：「服务器」与「客户端」的差别只在「远端 MAC 从哪来」。** 二者的收发引擎代码是同一份（`UdpEngineTx` / `UdpEngineRx`），区别仅在于：

| | 服务器（Server） | 客户端（Client） |
|---|---|---|
| 本地端口 | 编译期 `SERVER_PORTS_G` 固定 | 编译期 `CLIENT_PORTS_G` 固定 |
| 远端 IP/端口 | **运行时从入向帧学到**（被动） | **配置给出**（主动） |
| 远端 MAC | 直接复用入向帧的源 MAC | **需查 ARP 表**把远端 IP 翻成 MAC |

理解了这张表，本讲代码就明白了一大半。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [ethernet/UdpEngine/rtl/UdpEngine.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngine.vhd) | 纯结构顶层。例化 `UdpEngineRx`、可选 `UdpEngineDhcp`、服务器/客户端各一份 `UdpEngineTx`、客户端的 ARP 表与 `UdpEngineArp`，最后用 `AxiStreamMux` 合并发送流。 |
| [ethernet/UdpEngine/rtl/UdpEngineRx.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineRx.vhd) | UDP 接收：读伪头部里的本地端口号，在 `SERVER`/`CLIENT`/`DHCP` 三类端口里查表路由，剥掉 UDP 头把数据交给应用。 |
| [ethernet/UdpEngine/rtl/UdpEngineTx.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineTx.vhd) | UDP 发送：给应用数据加上伪头部首拍 + UDP 头，服务器/客户端/DHCP 共用一份状态机。 |
| [ethernet/UdpEngine/rtl/UdpEngineArp.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineArp.vhd) | 客户端 ARP 驱动：把客户端要查的 IP 经 `ArpEngine` 解析成 MAC，并刷新 ARP 表。 |
| [ethernet/UdpEngine/rtl/ArpIpTable.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/ArpIpTable.vhd) | ARP 缓存表：IP→MAC 的小型查找表，带超时刷新。 |
| [ethernet/UdpEngine/rtl/UdpEngineDhcp.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineDhcp.vhd) | DHCP 客户端：Discover/Offer/Request/ACK 状态机，协商本机 IP 与租约。 |
| [ethernet/UdpEngine/rtl/UdpEngineWrapper.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineWrapper.vhd) | 带 AXI-Lite 配置口的封装：可选地把客户端远端端口/IP 暴露为寄存器。 |
| [ethernet/RawEthFramer/rtl/RawEthFramer.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/RawEthFramer/rtl/RawEthFramer.vhd) | 二层成帧顶层：例化 `RawEthFramerTx`/`RawEthFramerRx`，并用小状态机仲裁 RX/TX 共用的 remoteMac 查询。 |
| [ethernet/RawEthFramer/rtl/RawEthFramerTx.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/RawEthFramer/rtl/RawEthFramerTx.vhd) | TX：给应用数据加 DST MAC + SRC MAC + EtherType 头，短帧先入小缓存。 |
| [ethernet/RawEthFramer/rtl/RawEthFramerRx.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/RawEthFramer/rtl/RawEthFramerRx.vhd) | RX：校验目的 MAC / EtherType / 帧长，剥掉二层头交给应用。 |
| [ethernet/RawEthFramer/rtl/RawEthFramerPkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/RawEthFramer/rtl/RawEthFramerPkg.vhd) | 定义 `RAW_ETH_CONFIG_INIT_C`（8 字节数据流）与广播标志 BCF 的 TUSER 访问函数。 |
| [ethernet/EthMacCore/rtl/EthMacPkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacPkg.vhd) | 提供 `UDP_C`、DHCP 端口常量、`EthPortArrayBigEndian`、`EMAC_AXIS_CONFIG_C`。 |

---

## 4. 核心概念与源码讲解

### 4.1 UDP 收发与端口路由

#### 4.1.1 概念说明

`UdpEngine` 把「UDP 数据报」与「IPv4 伪头部流」对接起来。它对上（应用层）暴露若干组**服务器通道**和**客户端通道**，每组就是一对 AXI-Stream 收发总线；对下（`IpV4Engine`）只暴露一条入向、一条出向 UDP 流。

一个 UDP 数据报在网络上的结构是：

```text
[ IPv4 头 ][ UDP 头(8B) | UDP 负载 ]
              └─源端口/目的端口/UDP长度/校验和
```

但在 SURF 内部，`IpV4EngineRx` 已经把 IPv4 头剥掉、重打包成伪头部（见 u6-l2），所以 `UdpEngine` 看到的「UDP 帧」首拍其实是「伪头部首拍（含 remote MAC + 源/目的 IP）」、第二拍才是「协议号 + 端口号 + UDP 长度 + 校验和 + 头几个数据字节」。`UdpEngineRx` 用第二拍里的**本地端口号**决定这帧送给哪个应用通道；`UdpEngineTx` 则反过来，给应用数据补上这两拍头部。

#### 4.1.2 核心流程

顶层 `UdpEngine` 是纯结构（`architecture mapping`），数据通路如下：

```text
            IpV4Engine                              应用层
                │ obUdpMaster(收)                       ▲ obServerMasters / obClientMasters
                ▼                                       │
        ┌────────────────┐   serverRemoteMac/Port/Ip   │
        │  UdpEngineRx   │────────────────────────────▶│ (服务器: 远端信息从入向帧学到)
        │  按本地端口路由 │                              │
        └────────────────┘   clientRemoteDetValid/Ip   │
                │   ┌─────────────────────────────────▶│
                │   ▼                                   │
                │  ArpIpTable/UdpEngineArp ◀── 远端IP──│ (客户端: 远端IP配置给出, 查表得MAC)
                │   │ arpTabMacAddr/Found/Pos           │
                ▼   ▼                                   │
        ┌────────────────────┐  obUdpMasters(1:0)       │ ibServerMasters / ibClientMasters
        │ UdpEngineTx(server)│─────────────┐           │
        │ UdpEngineTx(client)│──┐          │           ▼
        └────────────────────┘  ├──▶ AxiStreamMux ──▶ ibUdpMaster(发) ──▶ IpV4EngineTx
                                │
         (可选) UdpEngineDhcp ──┘
```

关键设计有三点：

1. **服务器与客户端共用同一份 `UdpEngineTx`/`UdpEngineRx`**，靠泛型 `IS_CLIENT_G` 区分是否走 ARP 表。
2. **服务器/客户端的本地端口号都是编译期常量**（`SERVER_PORTS_G`/`CLIENT_PORTS_G`），在例化时用 `EthPortArrayBigEndian` 一次性翻成 big-Endian。
3. **两条发送流（服务器 + 客户端）经 `AxiStreamMux` 合并成一条**交给 `IpV4EngineTx`；若只启用其中之一，则用 `NO_CLIENT`/`NO_SERVER` generate 直连并把另一路总线终结掉。

#### 4.1.3 源码精读

**顶层泛型与断言** —— [UdpEngine.vhd:22-43](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngine.vhd#L22-L43) 定义了服务器/客户端的开关与端口数组，并用一个断言强制至少开一个：

```vhdl
assert ((SERVER_EN_G = true) or (CLIENT_EN_G = true)) report
   "UdpEngine: Either SERVER_EN_G or CLIENT_EN_G must be true" severity failure;
```

注意 `ARP_TAB_ENTRIES_G : positive range 1 to 255 := 4`——这是客户端 ARP 缓存的表项数上限，`UdpEngineTx` 用 `tDest` 当索引来查这张表（见下文）。

**DHCP / 服务器 / 客户端的三段例化** —— 顶层用三个 `generate` 分别例化 [DHCP（L158-191）](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngine.vhd#L158-L191)、[服务器 Tx（L193-222）](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngine.vhd#L193-L222)、[客户端 ARP 表 + UdpEngineArp + 客户端 Tx（L224-315）](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngine.vhd#L224-L315)。其中客户端那份 `UdpEngineTx` 多了两处：`IS_CLIENT_G => true`，并把 ARP 表的 `arpTabPos/arpTabFound/arpTabIpAddr/arpTabMacAddr` 接上；而服务器那份 `UdpEngineTx` 直接吃 `UdpEngineRx` 学到的 `serverRemoteMac`。最后 [L317-336](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngine.vhd#L317-L336) 用 `AxiStreamMux`（NUM_SLAVES_G=2）合并两路发送流。

**接收端口路由（UdpEngineRx）** —— 端口常量先转 big-Endian（[UdpEngineRx.vhd:73-74](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineRx.vhd#L73-L74)），状态机 `IDLE_S → CHECK_PORT_S → MOVE_S/LAST_S`（[L82-86](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineRx.vhd#L82-L86)）。核心的端口查找发生在 `CHECK_PORT_S`（[UdpEngineRx.vhd:212-266](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineRx.vhd#L212-L266)）：先校验目的 IP 是本机/广播/组播，再用入向第二拍的**本地端口号** `rxMaster.tData(63 downto 48)` 在服务器端口表与客户端端口表里各扫一遍：

```vhdl
-- 服务器端口匹配 (UdpEngineRx.vhd:239-248)
for i in (SERVER_SIZE_G-1) downto 0 loop
   if (v.route = NULL_S) and (rxMaster.tData(63 downto 48) = SERVER_PORTS_C(i)) then
      v.route               := SERVER_S;
      v.tDestServer         := toSlv(i, 8);
      v.serverRemotePort(i) := rxMaster.tData(47 downto 32);  -- 学到对端源端口
      v.serverRemoteIp(i)   := r.tData(95 downto 64);         -- 学到对端 IP
      v.serverRemoteMac(i)  := r.tData(47 downto 0);          -- 学到对端 MAC
   end if;
end loop;
```

这段同时体现了「服务器学到远端三元组」的核心：本地端口匹配上后，把入向帧里的**源端口 / 源 IP / 源 MAC** 直接锁存为该服务器通道的远端信息，回包时就用它们。DHCP 帧则靠固定端口对单独识别（[UdpEngineRx.vhd:264-266](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineRx.vhd#L264-L266)）：源端口 `DHCP_SPORT`(67)、目的端口 `DHCP_CPORT`(68)。路由结果经末端 `AxiStreamDeMux` 按 `tDest` 分发到各应用通道（[L522-556](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineRx.vhd#L522-L556)）。注意 `byteCnt` 由 UDP 长度字段（`tData(79:64)`）减去 8 字节 UDP 头得到（[L268-271](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineRx.vhd#L268-L271)），用于精确控制末拍 `tKeep`。

**链路状态判定（linkUp）** —— `UdpEngineTx` 每拍重新计算每个通道的 `linkUp`（[UdpEngineTx.vhd:135-149](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineTx.vhd#L135-L149)）：本机 MAC/IP、本地端口、远端 MAC/IP/端口**五者皆非零**才认为链路通。`TX_FLOW_CTRL_G` 决定链路断时的策略——`true`（默认）直接丢弃（blow off）数据，`false` 则反压上游直到链路恢复。

**发送封装（UdpEngineTx 的伪头部 + UDP 头）** —— 状态机 `IDLE_S → (ACC_ARP_TAB_S →) HDR_S → BUFFER_S → LAST_S`（[UdpEngineTx.vhd:67-74](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineTx.vhd#L67-L74)）。源码里那段重要的字段布局注释（[UdpEngineTx.vhd:260-275](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineTx.vhd#L260-L275)）正是 u6-l2 的「非标准 IPv4 伪头部」。`HDR_S` 负责写出第二拍（[UdpEngineTx.vhd:320-361](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineTx.vhd#L320-L361)）：

```vhdl
v.txMaster.tData(7 downto 0)    := x"00";                      -- 保留
v.txMaster.tData(15 downto 8)   := UDP_C;                      -- 协议号 = UDP(0x11)
v.txMaster.tData(31 downto 16)  := x"0000";                    -- 伪头长度: EthMac 算
v.txMaster.tData(47 downto 32)  := PORT_C(r.chPntr);           -- 源端口(本机, big-Endian)
v.txMaster.tData(63 downto 48)  := remotePort(r.chPntr);       -- 目的端口(远端)
v.txMaster.tData(79 downto 64)  := x"0000";                    -- UDP 长度: EthMac 算
v.txMaster.tData(95 downto 80)  := x"0000";                    -- UDP 校验和: EthMac 算
v.txMaster.tData(127 downto 96) := ibMasters(r.chPntr).tData(31 downto 0);  -- UDP 数据前4字节
```

而首拍（含 DST MAC / 源 IP / 目的 IP）在 `IDLE_S` 选定通道时就已经写好（[UdpEngineTx.vhd:195-200](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineTx.vhd#L195-L200)），服务器用 `remoteMac`、客户端经 ARP 表后用 `arpTabMacAddr`（见下）。`BUFFER_S`/`LAST_S`（[L396-440](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineTx.vhd#L396-L440)）做「跨拍对齐」：因为加了两拍头，应用数据要错位搬运、末拍用 `tKeep` 精确收尾。整个发送路径末端挂一级 `AxiStreamPipeline`（[L470-482](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineTx.vhd#L470-L482)）改善时序。

**客户端的 ARP 表集成** —— 当应用流的 `tDest /= 0x00` 时（[UdpEngineTx.vhd:182-183](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineTx.vhd#L182-L183)），`UdpEngineTx` 把 `tDest` 当作 ARP 表索引写入 `arpTabPos`，进入 `ACC_ARP_TAB_S`（[L215-258](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineTx.vhd#L215-L258)）查 `arpTabFound`/`arpTabMacAddr`/`arpTabIpAddr`：

```vhdl
v.arpTabPos(r.index) := ibMasters(r.index).tDest;   -- tDest = ARP 表下标
...
when ACC_ARP_TAB_S =>
   if arpTabFound(r.chPntr) = '0' then               -- 表里没有: 丢帧直到末拍
      v.ibSlaves(r.chPntr).tReady := '1';
      if (ssiGetUserEofe(...) = '1' or ibMasters(r.chPntr).tLast = '1') then
         v.state := IDLE_S;
      end if;
   else                                              -- 命中: 用表里的 MAC/IP 填首拍
      v.txMaster.tData(47 downto 0)   := arpTabMacAddr(r.chPntr);
      v.txMaster.tData(127 downto 96) := arpTabIpAddr(r.chPntr);
      ...
```

表项的填充由 `UdpEngineArp` + `ArpIpTable` 负责：`UdpEngineRx` 在收到客户端端口的入向帧时置位 `clientRemoteDetValid/Ip`（[UdpEngineRx.vhd:254-259](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineRx.vhd#L254-L259)），`ArpIpTable` 据此刷新「最近见过的 IP」，`UdpEngineArp` 则对配置里给出的远端 IP 主动发起 ARP 请求（经 u6-l2 的 `ArpEngine`）并把解到的 MAC 写回表。

#### 4.1.4 代码实践

**实践目标**：运行 `UdpEngineTx` 的 cocotb 回归，验证「服务器流量与 DHCP 流量都能被正确封装成伪 UDP 帧、且 `linkUp` 在端点有效后拉高」。这是一个可运行实践。

**操作步骤**：

1. 按 u1-l2 / u9-l1 的流程生成源缓存：`make MODULES=$PWD import`。
2. 进入虚拟环境后运行（`待本地验证`）：

   ```bash
   ./.venv/bin/python -m pytest -q tests/ethernet/UdpEngine/test_UdpEngineTx.py
   ```

3. 打开 [test_UdpEngineTx.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/ethernet/UdpEngine/test_UdpEngineTx.py)，对照其方法学头：分别驱动一路「带有效远端端点的应用负载」和一路「DHCP 负载」，检查发出的伪 UDP 帧里源/目的元数据是否正确。

**需要观察的现象**：发出的帧首拍含正确的 DST MAC / 源 IP / 目的 IP；第二拍含 `UDP_C`(0x11) 协议号、正确的源/目的端口、UDP 长度与校验和字段为 0（留给 EthMac）；`linkUp` 在 `localMac/localIp/remoteMac/remoteIp/remotePort` 全部非零后拉高。

**预期结果**：服务器场景与 DHCP 场景两组断言全部 PASS。若失败，先核对 [udp_test_utils.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/ethernet/UdpEngine/udp_test_utils.py) 里构造激励用的 `UDP_SERVER_PORT=8192` 等常量是否与 RTL 的 `SERVER_PORTS_G` 默认值一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么服务器不需要 ARP 表，而客户端需要？

**答案**：服务器的远端 MAC/IP/端口是从**入向帧的源字段**直接学到的（`UdpEngineRx` 在端口匹配时锁存 `serverRemoteMac/Ip/Port`），回包用这些值即可；客户端是**主动外发**，配置里只给了远端 IP，没有现成的源 MAC 可用，必须靠 ARP 把远端 IP 翻译成 MAC 才能填出以太网头。

**练习 2**：`UdpEngineTx` 在 `HDR_S` 里把 UDP 长度与 UDP 校验和都填 `x"0000"`，这样安全吗？

**答案**：安全。这两个字段由下游 `EthMacCore` 的发送校验和卸载逻辑在帧离开 MAC 前补上（见文件头注释 “UDP checksum checked in EthMac core”）。`UdpEngineTx` 只负责装配伪头部与端口，不重复实现长度/校验和计算，与 u6-l2「IP 头校验和留给 EthMac」是同一套约定。

**练习 3**：客户端路径里，应用流的 `tDest` 被当作 ARP 表索引使用。这意味着 `tDest` 的取值范围受什么约束？

**答案**：受 `ARP_TAB_ENTRIES_G`（默认 4，上限 255）约束。`tDest` 必须落在 `[0, ARP_TAB_ENTRIES_G-1]`，否则 `ArpIpTable` 查不到对应表项、`arpTabFound` 永远为 0，该帧会被一直丢弃到末拍。`tDest = 0x00` 在 `UdpEngineTx` 里被特判为「不查表、直接用 `remoteMac`」（[L182-183](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineTx.vhd#L182-L183)），即退化为服务器式行为。

---

### 4.2 DHCP：运行时获取本机 IP

#### 4.2.1 概念说明

`UdpEngine` 的本机 IP `localIp` 可以由软件静态配置，也可以开启 `DHCP_G = true` 让 `UdpEngineDhcp` 在上电时自动向 DHCP 服务器申请一个。DHCP（Dynamic Host Configuration Protocol）跑在 UDP 之上，客户端用端口 68（`DHCP_CPORT`）、服务器用端口 67（`DHCP_SPORT`），底层是 BOOTP 报文加一组 TLV 选项。

一次完整的 DHCP 申请是经典的四步握手：

| 步骤 | 客户端发出 | 服务器回 | DHCP 消息类型（option 53） |
|---|---|---|---|
| 1 | Discover（广播） | — | Discover = `0x01` |
| 2 | — | Offer（单播/广播） | Offer = `0x02` |
| 3 | Request（广播） | — | Request = `0x03` |
| 4 | — | ACK | ACK = `0x05` |

收到 ACK 后客户端才算正式拿到 IP，并启动「租约（lease）」与「续约（renewal）」两个定时器。

#### 4.2.2 核心流程

`UdpEngineDhcp` 是一个 5 状态机（[UdpEngineDhcp.vhd:60-65](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineDhcp.vhd#L60-L65)），外加一个解析 DHCP 选项的子状态机 `DecodeType`（[L67-70](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineDhcp.vhd#L67-L70)）：

```text
IDLE_S ──commCnt到0 & 无入向──▶ REQ_S (构造 Discover/Request, 66拍BOOTP+选项)
   ▲                                │
   │                                │ 发出后启动 commCnt 通信超时
   │                                ▼
   ▲ ◀──校验失败/超时则回IDLE──── 收到 Offer/ACK: BOOTP_S (验 XID/CHADDR/cookie)
   │                                │
   │                                ▼
   │                             DHCP_S (CODE_S→LEN_S→DATA_S 解析 option 53/51)
   │                                │
   │                                ▼
   └─────────────────────────── VERIFY_S (按 msgType 决定下一步)
```

租约时间用两个定时器管理（[UdpEngineDhcp.vhd:204-234](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineDhcp.vhd#L204-L234)）：

- `renewCnt`：续约倒计时，初值 = `leaseTime / 2`（标准 DHCP 把续约时刻 T1 定为租约一半）。
- `leaseCnt`：租约倒计时，初值 = `leaseTime`；减到 0 视为租约过期，丢弃 IP、回到广播状态重新 Discover。

二者都靠一个 1 秒心跳（`TIMER_1_SEC_C = getTimeRatio(CLK_FREQ_G, 1.0)`，[L54](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineDhcp.vhd#L54)）每秒减 1，所以 `leaseTime` 的单位是「秒」。

续约的数学关系为：

\[
T_1 = 0.5 \times T_{\text{lease}}, \qquad T_{\text{renew}} \leftarrow \text{leaseTime}(31 \text{ downto } 1) = \lfloor T_{\text{lease}} / 2 \rfloor
\]

即 `renewCnt` 取 `leaseTime` 右移一位，等价于除二取整。

#### 4.2.3 源码精读

**关键常量** —— [UdpEngineDhcp.vhd:53-58](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineDhcp.vhd#L53-L58)：

```vhdl
constant DHCP_CONFIG_C  : AxiStreamConfigType := ssiAxiStreamConfig(4);   -- 内部用 4 字节(32位)流
constant CLIENT_HDR_C   : slv(31 downto 0) := x"00060101";  -- OP=1(BOOTPREQ)/HTYPE=1(ETH)/HLEN=6 → 0x01010600
constant SERVER_HDR_C   : slv(31 downto 0) := x"00060102";  -- OP=2(BOOTPREPLY)        → 0x02010600
constant MAGIC_COOKIE_C : slv(31 downto 0) := x"63538263";  -- DHCP magic cookie       → 0x63825363
```

再次看到 big-Endian「反序」：`CLIENT_HDR_C = x"00060101"` 对应线序字节 `01 06 01 00`（HLEN=6, HTYPE=1, OP=1）。`DHCP_CONFIG_C` 用 `ssiAxiStreamConfig(4)` 把内部 DHCP 流配成 4 字节一拍，方便逐字解析 BOOTP 报文（收发两端各挂一个 `AxiStreamFifoV2` 在 `EMAC_AXIS_CONFIG_C`(16B) 与 `DHCP_CONFIG_C`(4B) 间做位宽变换，[L145-174](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineDhcp.vhd#L145-L174)、[L578-607](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineDhcp.vhd#L578-L607)）。

**构造 Discover / Request（REQ_S）** —— `REQ_S` 是一个按 `cnt` 逐拍填 BOOTP 报文的计数器状态机（[UdpEngineDhcp.vhd:273-362](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineDhcp.vhd#L273-L362)）。关键拍：

- `cnt=0`：写 `CLIENT_HDR_C`（OP/HTYPE/HLEN），置 SOF（[L284-287](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineDhcp.vhd#L284-L287)）。
- `cnt=1`：写事务 ID `xid`（大端拆字节，[L289-294](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineDhcp.vhd#L289-L294)），用于匹配应答。
- `cnt=7/8`：写客户端硬件地址 CHADDR = 本机 MAC（[L305-309](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineDhcp.vhd#L305-L309)）。
- `cnt=59`：写 magic cookie（[L311-312](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineDhcp.vhd#L311-L312)）。
- `cnt=60`：写 option 53（消息类型）。**Discover 与 Request 在这里分叉**（[L314-332](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineDhcp.vhd#L314-L332)）：`dhcpReq='0'` 时发 Discover(`0x01`) 并立即 `tLast` 收尾、启动通信超时；`dhcpReq='1'` 时发 Request(`0x03`)，继续写 option 50（请求 IP = `yiaddr`）和 option 54（服务器标识 = `siaddr`），到 `cnt=65` 写结束标记 `0xFF` 收尾。

**解析应答（BOOTP_S + DHCP_S）** —— `BOOTP_S`（[L364-425](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineDhcp.vhd#L364-L425)）逐拍校验：`cnt=1` 验 XID 与本机发出的一致、`cnt=4` 取 `yiaddr`（分配给本机的 IP）、`cnt=5` 取 `siaddr`（服务器 IP）、`cnt=7/8` 验 CHADDR 是本机 MAC、`cnt=59` 验 magic cookie。任一不匹配即回 `IDLE_S` 丢弃。通过后进 `DHCP_S` 用三态子状态机 `CODE_S→LEN_S→DATA_S`（[L427-515](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineDhcp.vhd#L427-L515)）遍历 TLV 选项，只关心两种：

```vhdl
if (r.opCode = 53) then        -- DHCP Message Type
   if (r.len = 1) then  v.msgType := tData;  v.valid(0) := '1';  end if;
elsif (r.opcode = 51) then     -- IP Address Lease Time (4 字节, 大端拼回)
   ...逐字节拼入 v.leaseTime(31:0)...   v.valid(1..4) := '1';
end if;
```

**VERIFY_S：决定下一步** —— [UdpEngineDhcp.vhd:517-538](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineDhcp.vhd#L517-L538)（要求 `uAnd(r.valid)` 即五个有效位全亮）：

```vhdl
if (r.dhcpReq = '0') and (r.msgType = 2) then   -- 发了Discover, 收到Offer → 转去发Request
   v.dhcpReq := '1';  v.commCnt := 0;            -- 立刻触发 REQ_S 发 Request
elsif (r.dhcpReq = '1') and (r.msgType = 5) then -- 发了Request, 收到ACK → 落实 IP
   v.dhcpIp   := r.yiaddrTemp;                   -- 这就是本机 IP, 经 UdpEngine 顶层回灌给 localIp
   v.renewCnt := r.leaseTime(31 downto 1);       -- T1 = 租约/2
   v.leaseCnt := r.leaseTime;                    -- 租约总长
end if;
```

`dhcpIp` 在顶层 [UdpEngine.vhd:116](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngine.vhd#L116) `dhcpIpOut <= localIp` 引出，又经 `UdpEngineDhcp` 的 `dhcpIp` 端口回灌为 `UdpEngine` 内部用的 `localIp`（[UdpEngine.vhd:173](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngine.vhd#L173)），形成闭环。若 `DHCP_G = false`，则 `BYPASS_DHCP` generate 直接把 `localIpIn` 接给 `localIp`（[UdpEngine.vhd:185-191](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngine.vhd#L185-L191)）。

注意 [L551](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineDhcp.vhd#L551) 的复位条件里多了 `(r.localMac /= v.localMac) or (r.localMac = 0)`：一旦本机 MAC 变化或仍为 0，整个 DHCP 状态机就被复位重来，避免用旧 MAC 申请来的 IP 继续生效。

#### 4.2.4 代码实践

**实践目标**：源码阅读型实践——跟踪一次「Discover → Offer → Request → ACK」在 `UdpEngineDhcp` 状态机里的字段流转，并验证 BOOTP 报文的关键字节布局。

**操作步骤**：

1. 打开 `UdpEngineDhcp.vhd`，从 `IDLE_S` 的 `commCnt = 0` 分支（[L262-269](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineDhcp.vhd#L262-L269)）出发，设 `dhcpReq='0'`，跟随 `REQ_S` 的 `cnt=0,1,7,8,59,60` 画出 Discover 帧的 BOOTP 头与选项布局。
2. 用 [udp_test_utils.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/ethernet/UdpEngine/udp_test_utils.py) 里的 `build_dhcp_reply_payload(...)` 构造一个 Offer（msgType=2）和一个 ACK（msgType=5），核对 `extract_dhcp_xid` / `extract_dhcp_message_type` / `extract_dhcp_requested_ip` 的解析与 RTL 的 `BOOTP_S`/`DHCP_S` 一致。
3. 若本地已配好 cocotb 环境，可运行（`待本地验证`）：`./.venv/bin/python -m pytest -q tests/ethernet/UdpEngine/test_UdpEngineDhcp.py`。

**需要观察的现象**：Discover 帧目的 MAC/IP 全广播、源端口 68/目的端口 67、CHADDR = 本机 MAC、option 53 = 0x01；收到 Offer 后 `dhcpReq` 翻成 1、立即发 Request（option 53 = 0x03、option 50 = Offer 里的 yiaddr、option 54 = siaddr）；收到 ACK 后 `dhcpIp` 更新为 yiaddr、`renewCnt = leaseTime/2`。

**预期结果**：你画的 Discover 帧布局与 `build_dhcp_reply_payload`/`extract_*` 参考实现逐字节吻合；并理解为什么 Request 要带上 option 50/54（告诉服务器「我就要这个 IP、向这台服务器要」）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `renewCnt` 取 `leaseTime(31 downto 1)` 而不是直接 `leaseTime`？

**答案**：RFC 2131 规定客户端在租约过半（T1 = 0.5 × 租约）时就开始尝试续约。`leaseTime(31 downto 1)` 是 `leaseTime` 右移一位，等价于 `floor(leaseTime/2)`，正好实现 T1。若直接用完整 `leaseTime`，客户端会一直用到租约到期才续约，失去了「提前续约避免断网」的余地。

**练习 2**：`BOOTP_S` 里为什么要逐拍校验 XID 和 CHADDR？只校验 magic cookie 不够吗？

**答案**：DHCP 服务器在同一广播域里可能同时给多台主机下发 Offer/ACK，这些报文都以广播形式出现。XID（事务 ID）是客户端在 Discover 时随机生成的、用于区分「这是我这轮事务的应答」；CHADDR 是客户端硬件地址，用于确认「这个 IP 确实是分给我的」。只验 magic cookie 只能说明「这是个 DHCP 报文」，无法区分是不是发给本机的，会把别人的 Offer 误当成自己的，导致 IP 冲突。

**练习 3**：`DHCP_G = false` 时 `UdpEngine` 还能正常工作吗？本机 IP 从哪来？

**答案**：能。`BYPASS_DHCP` generate（[UdpEngine.vhd:185-191](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngine.vhd#L185-L191)）直接把外部 `localIpIn` 接给内部 `localIp`，并把 DHCP 收发总线终结到安全静止态。本机 IP 由软件经顶层端口静态配置。DHCP 只是「自动获取 IP」的可选增强，不影响 UDP 收发本身。

---

### 4.3 RawEthFramer：二层（裸以太网）成帧

#### 4.3.1 概念说明

并非所有应用都需要完整的 IPv4/UDP 协议栈。当你想在两块板子之间跑一个**自定义的、点到点的二层协议**（例如私有的触发/同步信令），`RawEthFramer` 提供了一条最短路径：它跳过 IP，直接给应用数据套上标准的以太网二层头——

```text
[ 目的 MAC(6B) | 源 MAC(6B) | EtherType(2B) | 应用负载(可变) ]
```

EtherType 用一个可配置的私有值（`ETH_TYPE_G`，默认 `0x1000`，big-Endian 存为 `x"0010"`）来标识「这是本框架的私有帧」，从而与 IPv4(`0x0800`)/ARP(`0x0806`) 等标准帧互不干扰。`RawEthFramer` 与 MAC 之间用 8 字节（64 位）AXI-Stream（`RAW_ETH_CONFIG_INIT_C`），比 UDP 用的 16 字节更窄。

它还解决两个二层特有的问题：

1. **短帧处理**：以太网规范要求帧负载不少于 46 字节（否则需填充）。Framer 用一个小 LutRam 缓存前若干拍，确保短帧也能凑出合规的帧长并精确收尾。
2. **广播帧**：用 TUSER 里的一位 BCF（Broadcast Frame）标志区分单播与广播，广播帧目的 MAC 自动填全 1。

#### 4.3.2 核心流程

`RawEthFramer` 顶层例化 `RawEthFramerTx`/`RawEthFramerRx`，并维护一个 3 状态小 FSM（`IDLE_S → RX_S / TX_S`，[RawEthFramer.vhd:51-54](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/RawEthFramer/rtl/RawEthFramer.vhd#L51-L54)）来**仲裁** RX 与 TX 共享的「remoteMac 查询」：

```text
                  外部 remoteMac (配置给出的对端 MAC)
                          │
              ┌───────────┴───────────┐
   txReq ──▶ │ IDLE_S: 谁先 req 谁先用 │ ◀── rxReq
              │  TX_S: 把 remoteMac 给TX│
              │  RX_S: 把 remoteMac 给RX│
              └────────────────────────┘
                          │
              ┌───────────┴───────────┐
              ▼                       ▼
        RawEthFramerTx           RawEthFramerRx
   (加二层头发送)             (剥二层头接收)
```

TX 侧状态机 `IDLE_S → TDEST_S → CACHE_S → MOVE_S`（[RawEthFramerTx.vhd:51-55](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/RawEthFramer/rtl/RawEthFramerTx.vhd#L51-L55)）：在 SOF 拍锁存 `tDest` 与广播标志 BCF、向顶层请求 remoteMac，然后把应用数据前若干拍（最多 64 字节）写入一个小 LutRam 缓存，再边读缓存边输出完整的二层帧。

RX 侧状态机 `IDLE_S → HDR_S → TDEST_S → MOVE_S`（[RawEthFramerRx.vhd:53-57](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/RawEthFramer/rtl/RawEthFramerRx.vhd#L53-L57)）：在帧首拍过滤目的 MAC（本机或广播），第二拍校验 EtherType 与帧长，剥头后把负载交给应用。

#### 4.3.3 源码精读

**流配置与 BCF 标志** —— [RawEthFramerPkg.vhd:29-36](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/RawEthFramer/rtl/RawEthFramerPkg.vhd#L29-L36)：

```vhdl
constant ETH_BCF_C : integer := 2;    -- TUSER 里 BCF 位的索引 (位0=SOF, 位1=EOFE, 位2=BCF)
constant RAW_ETH_CONFIG_INIT_C : AxiStreamConfigType :=
   ssiAxiStreamConfig(8, TKEEP_COMP_C, TUSER_FIRST_LAST_C, 8, 3);
   -- 8 字节数据 / 8 位 tDest / 3 位 tUser(SOF,EOFE,BCF)
```

`ssiGetUserBcf`/`ssiSetUserBcf`（[L52-68](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/RawEthFramer/rtl/RawEthFramerPkg.vhd#L52-L68)）就是对 TUSER 第 2 位的读写封装，沿用 u5-l1 的 SSI 侧带访问函数约定。

**TX 的小缓存（保证短帧合规收尾）** —— [RawEthFramerTx.vhd:98-114](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/RawEthFramer/rtl/RawEthFramerTx.vhd#L98-L114) 例化一个 `LutRam`（3 位地址 = 8 项、64 位宽、1 拍读）。应用数据进来时先按字节写进缓存（无效字节补 0），同时累计 `minByteCnt`（[L178](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/RawEthFramer/rtl/RawEthFramerTx.vhd#L178)、[L229](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/RawEthFramer/rtl/RawEthFramerTx.vhd#L229)）；这个计数最终被写进帧头第二拍的 `tData(54:48)`（[L257](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/RawEthFramer/rtl/RawEthFramerTx.vhd#L257)、[L285](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/RawEthFramer/rtl/RawEthFramerTx.vhd#L285)），告诉对端 RX「这帧的缓存段有多长」，从而让 RX 能精确还原末拍 `tKeep`。

**TX 构造二层头** —— 首拍（HDR[0]）写目的 MAC + 源 MAC 低 16 位（[RawEthFramerTx.vhd:194-206](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/RawEthFramer/rtl/RawEthFramerTx.vhd#L194-L206)）：

```vhdl
ssiSetUserSof(RAW_ETH_CONFIG_INIT_C, v.ibMacMaster, '1');  -- 帧起始
if (r.bcf = '1') then
   v.ibMacMaster.tData(47 downto 0) := (others => '1');    -- 广播: 目的 MAC 全 1
else
   v.ibMacMaster.tData(47 downto 0) := remoteMac;          -- 单播: 配置的远端 MAC
end if;
v.ibMacMaster.tData(63 downto 48) := localMac(15 downto 0);-- 源 MAC 低 16 位
```

第二拍（HDR[1]）写源 MAC 高 32 位 + EtherType + `minByteCnt` + `tDest`（[RawEthFramerTx.vhd:248-266](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/RawEthFramer/rtl/RawEthFramerTx.vhd#L248-L266)）：

```vhdl
v.ibMacMaster.tData(31 downto 0)  := localMac(47 downto 16);  -- 源 MAC 高 32 位
v.ibMacMaster.tData(47 downto 32) := ETH_TYPE_G;               -- EtherType (big-Endian, 默认 0x1000)
v.ibMacMaster.tData(54 downto 48) := toSlv(r.minByteCnt, 7);   -- 缓存段长度(供 RX 还原 tKeep)
if (r.bcf = '1') then
   v.ibMacMaster.tData(63 downto 55) := (others => '1');       -- 广播: tDest 字段也全 1
else
   v.ibMacMaster.tData(63 downto 55) := r.tDest & '0';         -- 单播: 应用 tDest 传给对端
end if;
```

注意源 MAC 被拆成「低 16 位在首拍、高 32 位在第二拍」，正是因为帧首字节落在 `tData` 最低位、字节按线序连续排列。之后 `MOVE_S`（[L270-328](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/RawEthFramer/rtl/RawEthFramerTx.vhd#L270-L328)）先把缓存里的数据读出来发、再直通后续应用数据，末拍据 `tLast`/EOFE 收尾。

**RX 过滤与校验** —— `IDLE_S`（[RawEthFramerRx.vhd:111-127](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/RawEthFramer/rtl/RawEthFramerRx.vhd#L111-L127)）在帧首拍校验目的 MAC 是本机或广播：

```vhdl
if (ssiGetUserSof(RAW_ETH_CONFIG_INIT_C, obMacMaster) = '1') then
   if (localMac /= 0) and ((localMac = v.dstMac) or (v.dstMac = BC_MAC_C)) then
      v.state := HDR_S;   -- 只有发给本机或广播的帧才继续
   end if;
end if;
```

`HDR_S`（[RawEthFramerRx.vhd:129-159](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/RawEthFramer/rtl/RawEthFramerRx.vhd#L129-L159)）做四重校验：EtherType 必须等于 `ETH_TYPE_G`、`minByteCnt` 合法（`>64` 或 `<=16` 且非零都判非法）、广播标志与 `tDest`/目的 MAC 必须自洽。任一不过即回 `IDLE_S` 丢弃。通过后，`MOVE_S`（[L185-230](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/RawEthFramer/rtl/RawEthFramerRx.vhd#L185-L230)）把后续负载原样交给应用，并在首拍把 SOF 与 BCF 写回 TUSER；若 TX 端用过缓存（`r.eof='1'`），则用 `minByteCnt` 还原末拍 `tKeep`（[L214-228](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/RawEthFramer/rtl/RawEthFramerRx.vhd#L214-L228)），与 TX 端的 `minByteCnt` 计数严格对称。

#### 4.3.4 代码实践

**实践目标**：运行 `RawEthFramer` 的环回测试，验证「加二层头 → 剥二层头」后应用负载与 `tDest`/BCF 一致。

**操作步骤**：

1. 生成源缓存后运行成帧对测试（`待本地验证`）：

   ```bash
   ./.venv/bin/python -m pytest -q tests/ethernet/RawEthFramer/test_RawEthFramerPair.py
   ```

2. 打开 [test_RawEthFramerPair.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/ethernet/RawEthFramer/test_RawEthFramerPair.py)，对照 [raw_eth_test_utils.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/ethernet/RawEthFramer/raw_eth_test_utils.py) 里的帧构造函数，看它如何设置 SOF/BCF 与 `tDest`。

**需要观察的现象**：发送端应用负载经 `RawEthFramerTx` 后变成「目的 MAC + 源 MAC + EtherType(0x1000) + 负载」的线格式帧；接收端 `RawEthFramerRx` 剥头后还原出原始负载、`tDest`、SOF 与 BCF；短帧（不足一拍）也能靠 `minByteCnt` 正确还原末拍 `tKeep`。

**预期结果**：单播与广播两类帧、不同 `tDest`、不同长度的负载全部环回 PASS。若失败，先核对收发两端的 `ETH_TYPE_G` 与 `localMac`/`remoteMac` 是否一致。

#### 4.3.5 小练习与答案

**练习 1**：`RawEthFramerTx` 为什么要把应用数据的前若干拍先写进 LutRam 缓存，而不是直接透传？

**答案**：因为它要在数据前面插入两拍二层头（目的 MAC + 源 MAC + EtherType + 元信息）。如果不缓存，插入头拍时应用数据就会丢失。缓存让「头」与「前几拍数据」能在输出端按正确顺序拼接；同时 `minByteCnt` 记录缓存段长度，使对端 RX 能精确还原短帧的末拍 `tKeep`。这与 `UdpEngineTx` 用 `tData`/`tKeep` 暂存「错位 leftovers」是同一类「插头导致数据错位」的问题（见 4.1.3）。

**练习 2**：广播帧（BCF=1）在 TX/RX 两端各做了什么特殊处理？

**答案**：TX 端把目的 MAC 字段填全 1（`FF:FF:FF:FF:FF:FF`）、`tDest` 字段也填全 1，并向顶层请求 remoteMac 时跳过查询（`req := not(v.bcf)`，BCF=1 时不发 req）。RX 端在 `IDLE_S` 接受目的 MAC = `BC_MAC_C` 的帧；在 `HDR_S` 额外校验「BCF=1 时 `tDest` 必须是 `0xFF` 且目的 MAC 必须是广播」，防止伪造的广播帧混入。

**练习 3**：`RawEthFramer` 与 `UdpEngine` 都做「给数据加头/剥头」，二者的本质差别是什么？

**答案**：`RawEthFramer` 加的是**二层（MAC 层）头**（目的/源 MAC + EtherType），不涉及 IP，EtherType 用私有值隔离，适合点到点私有协议；`UdpEngine` 加的是**伪头部 + UDP 头**，跑在 `IpV4Engine` 之上，参与标准 IPv4 路由、需要 ARP 解析 MAC、支持 DHCP 自动取 IP。前者轻、私有；后者重、标准、可路由。

---

## 5. 综合实践

**任务**：为 `UdpEngineWrapper` 配置「1 个服务器 + 2 个客户端」的通道，写出每个通道的本地端口、远端端口与 IP 在「泛型 / AXI-Lite 寄存器」里的布局，并说明服务器与客户端在「远端信息来源」上的差异。这是把本讲端口路由与 u6-l2 ARP 串起来的源码阅读型综合实践。

**建议步骤**：

1. **设定参数**：服务器监听 `8192`；客户端 0 主动连到远端 `192.168.1.100:8200`、客户端 1 连到 `192.168.1.200:8300`。本机 `localMac = 08:00:56:00:00:01`、`localIp = 192.168.1.10`。

2. **本地端口（编译期泛型）**：打开 [UdpEngineWrapper.vhd:28-39](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineWrapper.vhd#L28-L39)，按以下方式例化：

   ```vhdl
   SERVER_EN_G   => true,  SERVER_SIZE_G  => 1,  SERVER_PORTS_G => (0 => 8192),
   CLIENT_EN_G   => true,  CLIENT_SIZE_G  => 2,  CLIENT_PORTS_G => (0 => 8193, 1 => 8194),
   ARP_TAB_ENTRIES_G => 4,  CLIENT_EXT_CONFIG_G => true,  DHCP_G => false,
   ```

   注意：服务器与客户端的**本地端口都是编译期固定的**，分别用 `SERVER_PORTS_G`/`CLIENT_PORTS_G` 给出，并在 RTL 内部由 `EthPortArrayBigEndian` 翻成 big-Endian 的 `slv`（见 4.1.3）。

3. **客户端远端端口/IP（AXI-Lite 寄存器布局）**：当 `CLIENT_EXT_CONFIG_G = true` 时，`UdpEngineWrapper` 把每个客户端的远端端口/IP 暴露为 AXI-Lite 寄存器。读 [UdpEngineWrapper.vhd:236-239](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/UdpEngine/rtl/UdpEngineWrapper.vhd#L236-L239)：

   ```vhdl
   for i in (CLIENT_SIZE_G-1) downto 0 loop
      axiSlaveRegister(regCon, toSlv((8*i)+0, 12), 0, v.clientRemotePort(i));  -- 16b, big-Endian
      axiSlaveRegister(regCon, toSlv((8*i)+4, 12), 0, v.clientRemoteIp(i));    -- 32b, big-Endian
   end loop;
   ```

   由此得到寄存器地图（每个客户端占 8 字节）：

   | 客户端 | 偏移 | 寄存器 | 位数 | 含义（big-Endian） |
   |---|---|---|---|---|
   | 0 | `0x000` | `ClientRemotePortRaw` | 16 | 远端端口，写 `8200` → 存 `0x0020` |
   | 0 | `0x004` | `ClientRemoteIpRaw` | 32 | 远端 IP，写 `192.168.1.100` → 存 `0xC0A80164` 的大端序 |
   | 1 | `0x008` | `ClientRemotePortRaw` | 16 | 远端端口 `8300` |
   | 1 | `0x00C` | `ClientRemoteIpRaw` | 32 | 远端 IP `192.168.1.200` |

   对照 PyRogue 镜像 [_UdpEngineClient.py:22-56](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/ethernet/udp/_UdpEngineClient.py#L22-L56)：`ClientRemotePortRaw` 在 `0x00`、`ClientRemoteIpRaw` 在 `0x04`，二者均标注 “big-Endian configuration”，并用 `LinkVariable` + `getPortValue`/`getIpValue` 把大端原始值转成人类可读的小端形式。这正是 u3-l4 强调的「RTL 寄存器布局必须与 PyRogue 镜像逐字段对齐」。

4. **服务器远端信息（只读、运行时学到）**：服务器**没有**对应的可写远端寄存器。它的远端端口/IP/MAC 由 `UdpEngineRx` 在收到匹配 `8192` 端口的入向帧时锁存（见 4.1.3）。PyRogue 侧把它们镜像为只读变量 [_UdpEngineServer.py:21-53](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/ethernet/udp/_UdpEngineServer.py#L21-L53)（`ServerRemotePort`@`0x00`、`ServerRemoteIp`@`0x04`，`mode="RO"`），仅供软件观察「当前是谁在跟我说话」。

5. **客户端的 MAC 从哪来**：写出远端 IP 后，`UdpEngineArp` 会经 u6-l2 的 `ArpEngine` 把 `192.168.1.100` 解析成 MAC、写入 `ArpIpTable`；`UdpEngineTx` 用应用流的 `tDest`（这里 = 客户端下标 0 或 1）查表得到 MAC 后才能发出帧（见 4.1.3 的 `ACC_ARP_TAB_S`）。若 ARP 未命中，该客户端的帧会被丢弃到末拍。

6. **验证**：若本地已配好 cocotb 环境，可运行 [test_UdpEngineWrapper.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/ethernet/UdpEngine/test_UdpEngineWrapper.py)（`待本地验证`），并用 [udp_test_utils.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/ethernet/UdpEngine/udp_test_utils.py) 的 `port_config_word(port)`（[L142](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/ethernet/UdpEngine/udp_test_utils.py#L142)）核对你要写入寄存器的大端字面值是否与工具函数一致。

## 6. 本讲小结

- `UdpEngine` 是 IPv4 之上的 UDP 协议层：顶层纯结构，例化一份 `UdpEngineRx`、可选 `UdpEngineDhcp`、服务器与客户端各一份 `UdpEngineTx`（靠 `IS_CLIENT_G` 区分）、客户端的 `ArpIpTable`+`UdpEngineArp`，最后用 `AxiStreamMux` 合并发送流。
- 服务器与客户端的差别只在「远端 MAC 从哪来」：服务器从入向帧源字段学到（`UdpEngineRx` 锁存 `serverRemoteMac/Ip/Port`），客户端配置远端 IP、靠 ARP 表把 IP 翻成 MAC；二者本地端口都是编译期 `SERVER_PORTS_G`/`CLIENT_PORTS_G` 经 `EthPortArrayBigEndian` 翻成的大端常量。
- `UdpEngineRx` 用入向第二拍的本地端口号在服务器/客户端端口表里查表路由（`SERVER_S`/`CLIENT_S`/`DHCP_S`），DHCP 帧靠 67/68 固定端口对单独识别；`UdpEngineTx` 按 u6-l2 的「非标准 IPv4 伪头部」装配首拍、按 UDP 头布局填协议号/端口，UDP 长度与校验和留 0 交给 `EthMacCore`。
- 客户端用应用流的 `tDest` 当 ARP 表索引，`UdpEngineTx` 在 `ACC_ARP_TAB_S` 查 `arpTabFound`/`arpTabMacAddr`，未命中则丢帧到末拍；`linkUp` 要求本机/远端的 MAC/IP/端口五元组皆非零，`TX_FLOW_CTRL_G` 决定断链时丢弃还是反压。
- `UdpEngineDhcp`（可选）实现 Discover→Offer→Request→ACK 四步握手：用 `REQ_S` 计数器逐拍构造 BOOTP 报文、用 `BOOTP_S`+`DHCP_S` 校验 XID/CHADDR 并解析 option 53(消息类型)/51(租约)，`VERIFY_S` 据消息类型推进；收到 ACK 后落实 IP、`renewCnt = leaseTime/2`、`leaseCnt = leaseTime`。
- `RawEthFramer` 是跳过 IP 的二层成帧器：给应用数据加/剥「目的 MAC + 源 MAC + EtherType(默认 0x1000)」头，用小 LutRam 缓存与 `minByteCnt` 处理短帧末拍 `tKeep`，用 TUSER 里的 BCF 位支持广播；TX/RX 共享的 remoteMac 由顶层 3 状态小 FSM 仲裁。

## 7. 下一步学习建议

- **下一讲 u6-l4**：把视角拉回 `GigEthCore`/`TenGigEthCore`，看本讲的 `UdpEngine`/`RawEthFramer`/`IpV4Engine` 这些家族无关的 `core/` RTL 是如何与 GTX7/GTH7/GTY 等家族 PHY 封装组合成一块完整网卡的。
- **横向对照**：回头读 `UdpEngineTx` 的「插头导致数据错位」处理（`tData`/`tKeep` leftovers）与 `RawEthFramerTx` 的 LutRam 缓存，体会两类「在数据流前插头」的通用手法；并与 u4-l2 的 `AxiStreamFifoV2` 在不同位宽间做位宽变换（gearbox）的思路对比。
- **延伸阅读**：若关心 DHCP/BOOTP 报文细节，可对照 RFC 2131 与本讲 `UdpEngineDhcp.vhd` 的 `REQ_S` 计数器逐拍布局；若关心 UDP 校验和的硬件实现，回到 u6-l1 的 `EthMacRxCsum`/`getEthMacCsum` 看「伪头部 + UDP 头 + 数据」的反码和是如何边走边算的。
