# 网络协议栈：net 命名空间

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `PoC.net` 命名空间如何对应 OSI/TCP-IP 分层模型，以及每层落在 `src/net/` 下哪个子目录。
- 看懂每一个 `<layer>_Wrapper.vhdl`（MAC / ARP / IPv4 / UDP）是如何把「编码发送（TX）」与「解析接收（RX）」两条子链路封装成一个对外统一的流式接口的。
- 理解贯穿全栈的两类横切机制：流式帧校验（`net_FrameChecksum`）与 IP↔MAC 地址缓存（ARP Cache）。
- 能够手动追踪一个 UDP 数据包从 `mac_Wrapper` 进入、逐层解析、最终在 `udp_Wrapper` 输出给应用的完整 RX 路径。

本讲是专家层内容，承接 [u5-l4（总线与流式协议）](u5-l4-bus-stream-protocols.md) 中讲过的 `stream_Mux` / `stream_DeMux` / `Valid-Ack` 握手，以及 [u5-l3（cache 子系统）](u5-l3-cache-subsystem.md) 中的 LRU 缓存与 `T_CACHE_RESULT`。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**网络为什么是「分层」的。** 一台 FPGA 要发出一个 UDP 包，需要依次封装四层头部：以太网 MAC 头（目的/源 MAC 地址 + EthType）、IPv4 头（目的/源 IP 地址 + Protocol）、UDP 头（源/目的端口 + 长度 + 校验和）、最后才是应用载荷。接收方则反过来，一层层剥头并据此决定把包送到哪里。这就是「分层」——每一层只关心自己那层头部里的「类型/地址」字段，用来做多路分解（demultiplex）。

**PoC 的流式接口（Stream）。** net 栈里所有数据都以「帧」为单位在层与层之间流动，每一拍要么是数据字节、要么是帧定界。统一接口长这样：

- `Valid` / `Ack`：握手，同拍都有效则成交（等价 AXI-Stream 的 valid/ready）。
- `Data`（8 位）：帧内容字节流。
- `SOF` / `EOF`：帧起始 / 帧结束标记。
- `Meta_rst` / `Meta_nxt` / `Meta_Data`：**边带元数据**通道，用一个 `rst` 复位、`nxt` 步进、`Data` 字节流的「读地址发生器」式握手，逐字节搬运 MAC 地址、IP 地址、端口等控制信息。这样数据流和地址信息解耦，地址可以有不同字节宽度（IPv4 是 4 字节、IPv6 是 16 字节），却用同一套握手。

**TX 是漏斗、RX 是分叉。** 发送方向，N 个上层端口的数据要合并成 1 条线下传给物理层——所以是 `stream_Mux`（N→1），并在合并过程中「前缀」上自己这层的头部（prepender）。接收方向反过来，1 条线上来的帧要按头部字段分发到 N 个上层端口——所以是 `stream_DeMux`（1→N），分发依据就是头部里的类型字段。记住这个「TX=漏斗+前缀头、RX=分叉+剥头」的形状，下面所有 Wrapper 都是它的实例。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/net/net.pkg.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/net.pkg.vhdl) | 全栈共享类型与常量目录页：MAC/IP 地址类型、EthType、IP Protocol、UDP 端口对、ARP 缓存行等 |
| [src/net/net_FrameChecksum.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/net_FrameChecksum.vhdl) | 流式互联网校验和计算核，边收帧边累加，EOF 时吐出 checksum 与 length |
| [src/net/mac/mac_Wrapper.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/mac/mac_Wrapper.vhdl) | MAC 层封装：RX 按目的 MAC + EthType 分发，TX 前缀 MAC 头 |
| [src/net/arp/arp_Wrapper.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/arp/arp_Wrapper.vhdl) | ARP 层封装：维护 IPv4→MAC 缓存，未命中时广播请求并等响应 |
| [src/net/ipv4/ipv4_Wrapper.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/ipv4/ipv4_Wrapper.vhdl) | IPv4 层封装：TX 经 ARP 查询目的 MAC，RX 按 Protocol 字段分发 |
| [src/net/udp/udp_Wrapper.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/udp/udp_Wrapper.vhdl) | UDP 层封装：TX 先算校验和再前缀 UDP 头，RX 按 目的端口 分发到应用端口 |

补充阅读（不在最小模块内，但与本讲强相关）：`src/net/README.md` 列出全部子命名空间；`src/net/arp/README.md` 给出 ARP 报文的字节布局；`src/net/icmpv4/icmpv4_Wrapper.vhdl` 是 ICMPv4（ping）的同类封装范例。

## 4. 核心概念与源码讲解

### 4.1 协议分层：`net.pkg.vhdl` 作为全栈类型字典

#### 4.1.1 概念说明

`PoC.net` 把整张 TCP/IP 协议栈拆成 `src/net/` 下的若干子命名空间，目录名基本等于协议名：

```
src/net/
├── eth/        以太网物理层（PHY/PCS/RS，GMII/RGMII/SGMII 接口记录）
├── mac/        MAC 数据链路层（IEEE 802.3 帧）
├── arp/        地址解析协议（IPv4→MAC）
├── ndp/        邻居发现协议（IPv6 对应 ARP 的角色）
├── ipv4/       互联网协议 v4
├── ipv6/       互联网协议 v6
├── icmpv4/     ICMPv4（如 ping）
├── icmpv6/     ICMPv6
├── udp/        用户数据报协议（传输层）
├── stack/      预装配的整套协议栈（目前为空，README 标注 *No files published, yet.*）
└── net.pkg.vhdl  全栈共享类型与常量
```

这套分层对应经典的 OSI 模型：`eth/mac` 是物理+链路层，`arp/ipv4/ipv6` 是网络层，`udp` 是传输层。各层有自己的子核与一个 `<layer>_Wrapper.vhdl` 总装实体。

由于所有层都要用到 MAC 地址、IP 地址、端口号、协议号这些「跨层共享名词」，PoC 把它们集中声明在根包 `net.pkg.vhdl`（即 `package net`）里，作为全栈唯一的「类型字典」。任何子命名空间的核都靠 `use PoC.net.all;` 引用它（可在前述各 Wrapper 的 `library/use` 段看到）。

#### 4.1.2 核心流程

分层的关键不在「有多少个核」，而在「每层靠哪个字段做多路分解」。net.pkg 里把这些「关键字段」都定义成了具名常量：

- 链路层用 **EthType** 区分上层协议。
- 网络层 IPv4 用 **Protocol** 字段区分上层协议。
- 传输层 UDP 用 **目的端口号** 区分应用。

接收路径因此是一条「逐层剥头 + 逐层分发」的流水线：

```text
Eth (字节流)
  │  mac_Wrapper 按 EthType 分发      （0x0800→IPv4 端口, 0x0806→ARP 端口）
  ▼
IPv4 (剥掉 MAC 头)
  │  ipv4_Wrapper 按 Protocol 分发    （0x11→UDP 端口, 0x01→ICMP 端口）
  ▼
UDP  (剥掉 IPv4 头)
  │  udp_Wrapper 按 目的端口 分发      （如 7→Echo 应用）
  ▼
应用载荷
```

#### 4.1.3 源码精读

地址类型是逐字节切片的数组，便于通过 Meta 通道一拍一拍搬运：

```vhdl
type T_NET_MAC_ADDRESS       is array (5 downto 0) of T_SLV_8;   -- 6 字节 MAC
type T_NET_MAC_ETHERNETTYPE  is array (1 downto 0) of T_SLV_8;   -- 2 字节 EthType
```
[src/net/net.pkg.vhdl:230-231](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/net.pkg.vhdl#L230-L231) — 把 MAC 地址、EthType 定义成字节向量，与流式接口「逐字节 Meta」天然契合。

```vhdl
type T_NET_IPV4_ADDRESS is array (3 downto 0) of T_SLV_8;        -- 4 字节 IPv4
type T_NET_IPV6_ADDRESS is array (15 downto 0) of T_SLV_8;       -- 16 字节 IPv6
```
[src/net/net.pkg.vhdl:305](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/net.pkg.vhdl#L305) 与 [src/net/net.pkg.vhdl:353](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/net.pkg.vhdl#L353) — 同样的「字节向量」思路；注意 IPv4 与 IPv6 仅长度不同，故 Meta 通道用同一套字节握手就能兼容二者（见 4.4 节 `IP_VERSION` generic）。

「关键字段」的具名常量：

```vhdl
constant C_NET_MAC_ETHERNETTYPE_IPV4 : T_NET_MAC_ETHERNETTYPE := to_net_mac_ethernettype(x"0800");
constant C_NET_MAC_ETHERNETTYPE_ARP  : T_NET_MAC_ETHERNETTYPE := to_net_mac_ethernettype(x"0806");
constant C_NET_MAC_ETHERNETTYPE_IPV6 : T_NET_MAC_ETHERNETTYPE := to_net_mac_ethernettype(x"86DD");
```
[src/net/net.pkg.vhdl:550-555](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/net.pkg.vhdl#L550-L555) — 链路层多路分解依据：IPv4=0x0800、ARP=0x0806、IPv6=0x86DD。

```vhdl
constant C_NET_IP_PROTOCOL_ICMP : T_NET_IP_PROTOCOL := x"01";
constant C_NET_IP_PROTOCOL_TCP  : T_NET_IP_PROTOCOL := x"06";
constant C_NET_IP_PROTOCOL_UDP  : T_NET_IP_PROTOCOL := x"11";
```
[src/net/net.pkg.vhdl:574-579](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/net.pkg.vhdl#L574-L579) — 网络层多路分解依据：ICMP=1、TCP=6、UDP=17（0x11）。

传输层的端口对定义把「本机对外监听端口」与「发往对端端口」绑成一对：

```vhdl
type T_NET_UDP_PORTPAIR is record
  Ingress : T_NET_UDP_PORT;   -- incoming port number  （本机监听端口，RX 分发依据）
  Egress  : T_NET_UDP_PORT;   -- outgoing port number  （发往对端端口，TX 填入 UDP 头）
end record;
```
[src/net/net.pkg.vhdl:531-534](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/net.pkg.vhdl#L531-L534) — 一个 PORTPAIR 描述一条「应用流」，`udp_Wrapper` 的端口数等于 `PORTPAIRS'length`。

根包还提供配置记录与辅助函数，例如 MAC 层用 `T_NET_MAC_CONFIGURATION` 描述「本机接口地址 + 源过滤表 + EthType 分发表」，并由 `getPortCount` 统计出实际对外端口数：
[src/net/net.pkg.vhdl:271-275](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/net.pkg.vhdl#L271-L275)、[src/net/net.pkg.vhdl:721-733](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/net.pkg.vhdl#L721-L733)。

#### 4.1.4 代码实践

**实践目标**：用源码确认「逐层分发的关键字段」与对应常量值。

**操作步骤**：

1. 打开 [src/net/net.pkg.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/net.pkg.vhdl)。
2. 找到 `C_NET_MAC_ETHERNETTYPE_*`、`C_NET_IP_PROTOCOL_*` 两组常量。
3. 用一张三列表格写下：层级（链路 / 网络 / 传输）、字段名（EthType / Protocol / 目的端口）、用于分发的具名常量示例（IPv4 / UDP / 你任选一个端口号）。

**需要观察的现象**：三类字段在源码里都只是 `T_SLV_16` 或 `T_SLV_8`，没有任何「魔法」——分发的本质就是「拿帧里某个字段去和一个常量做相等比较」。

**预期结果**：你会得到一张清晰的「字段→常量→分发层」对照表，这正是后面 `stream_DeMux` 的 `Control` 信号来源。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `T_NET_MAC_ADDRESS` 用 `array of T_SLV_8` 而不是直接用 `T_SLV_48`？

> **答**：因为 MAC 地址要通过 Meta 通道**逐字节**搬运（`Meta_nxt` 每拉一拍出一个字节）。数组下标天然对应「第几个字节」，便于在流式接口里串行化；而 `T_SLV_48` 是整体 48 位，不利于按字节步进。包内同时提供 `to_slv(mac)` / `to_net_mac_address(slv)` 做两种表示的互转。

**练习 2**：`src/net/stack/README.md` 写着「pre-configured network stacks」，但 entities 为空。这说明什么？

> **答**：说明 PoC 的 net 栈目前提供的是「积木」（各层 Wrapper），尚未提供「开箱即用的整栈顶层」；组装一个完整 UDP/IPv4 栈仍需使用者自行把 `mac_Wrapper`、`arp_Wrapper`、`ipv4_Wrapper`、`udp_Wrapper` 像本讲综合实践那样连起来。

---

### 4.2 Wrapper 封装：TX 漏斗 + RX 分叉的统一骨架

#### 4.2.1 概念说明

每一层都有一个总装实体 `<layer>_Wrapper.vhdl`，它对外暴露：

- **向下层**的接口（如 `udp_Wrapper` 的 `IP_TX_*` / `IP_RX_*` 连到 IPv4 层）。
- **向上层**的接口（如 `udp_Wrapper` 的 `TX_*` / `RX_*` 是若干「应用端口」，数量由 generic 决定）。

Wrapper 内部几乎总是两条独立链路：

- **TX 链**（发送，向下）：把 N 个上层端口用 `stream_Mux` 合并成 1 条，途中前缀本层头部（prepender），必要时查询本层附属服务（如 IPv4 查 ARP 拿目的 MAC、UDP 算校验和）。
- **RX 链**（接收，向上）：把下层送上来的 1 条流，先用 parser 剥本层头、提取关键字段，再用 `stream_DeMux` 按「关键字段」分发到 N 个上层端口。

这个骨架在 MAC、IPv4、UDP 三个 Wrapper 里几乎一模一样，区别只在于「前缀什么头」「按什么字段分发」。看懂一个，另两个就是套模板。

#### 4.2.2 核心流程

以 RX 方向（接收一个帧）为例，三层 Wrapper 的共性流程：

```text
下层流入 1 条帧 (Valid/Data/SOF/EOF + Meta)
   │
   ├─ 1. parser 剥本层头：抽取关键字段（EthType / Protocol / DestPort）写入 Meta
   │
   ├─ 2. 计算 Control 向量：对每个上层端口 i，比较「关键字段 == 该端口配置值」
   │
   └─ 3. stream_DeMux 按 Control 把帧分发到命中的端口 i
```

TX 方向反过来：N 个端口 → `stream_Mux` 选 1 → 前缀头部 → 下层。

#### 4.2.3 源码精读

**`udp_Wrapper`：最干净的模板。** 它的 RX 路径就是「剥 UDP 头 → 按目的端口分发」：

```vhdl
RX_UDP : entity PoC.udp_RX
  port map ( ... Out_Meta_DestPort => UDP_RX_Meta_DestPort, ... );

genStmDeMux_Control : for i in 0 to UDP_SWITCH_PORTS - 1 generate
  StmDeMux_Control(i) <= to_sl(UDP_RX_Meta_DestPort = PORTPAIRS(i).Ingress);
end generate;
```
[src/net/udp/udp_Wrapper.vhdl:461-463](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/udp/udp_Wrapper.vhdl#L461-L463) — `udp_RX` 解析出目的端口后，这里的 `for-generate` 对每个端口比较「收到的 DestPort 是否等于该端口配置的 `Ingress`」，生成 one-hot 的 `Control`，交给 `stream_DeMux` 分发。这正是「RX 分叉」的精髓。

```vhdl
RX_StmDeMux : entity PoC.stream_DeMux
  generic map ( PORTS => UDP_SWITCH_PORTS, ... )
  port map ( DeMuxControl => StmDeMux_Control, In_Data => UDP_RX_Data,
             Out_Valid => RX_Valid, Out_Data => StmDeMux_Out_Data, ... );
```
[src/net/udp/udp_Wrapper.vhdl:483-511](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/udp/udp_Wrapper.vhdl#L483-L511) — 复用 [u5-l4](u5-l4-bus-stream-protocols.md) 讲过的 `stream_DeMux`，把分发逻辑从协议解析里彻底解耦。

TX 方向则先 `stream_Mux` 合并、再算校验和、再交给 `udp_TX` 前缀 UDP 头：
[src/net/udp/udp_Wrapper.vhdl:295-321](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/udp/udp_Wrapper.vhdl#L295-L321)（Mux）→ [src/net/udp/udp_Wrapper.vhdl:373-408](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/udp/udp_Wrapper.vhdl#L373-L408)（`udp_TX` 前缀头并下传给 IP 层）。

**`ipv4_Wrapper`：同样的骨架，分发字段换成 Protocol。**

```vhdl
genStmDeMux_Control : for i in 0 to IPV4_SWITCH_PORTS - 1 generate
  StmDeMux_Control(i) <= to_sl(IPv4_RX_Meta_Protocol = PACKET_TYPES(i));
end generate;
```
[src/net/ipv4/ipv4_Wrapper.vhdl:433-435](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/ipv4/ipv4_Wrapper.vhdl#L433-L435) — 与 UDP 唯一的区别：比较对象从 `DestPort` 换成 IPv4 头里的 `Protocol`，端口配置从 `PORTPAIRS` 换成 generic `PACKET_TYPES : T_NET_IPV4_PROTOCOL_VECTOR`（[src/net/ipv4/ipv4_Wrapper.vhdl:44-46](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/ipv4/ipv4_Wrapper.vhdl#L44-L46)）。

IPv4 的 TX 链多了一个「跨层查询」：发 IPv4 包前要知道目的 MAC，于是 `ipv4_TX` 向 ARP 层发起 `ARP_IPCache_Query`，等 `ARP_IPCache_Valid` 拿回 MAC 地址：
[src/net/ipv4/ipv4_Wrapper.vhdl:350-389](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/ipv4/ipv4_Wrapper.vhdl#L350-L389)。这就是网络层与 ARP 服务的耦合点。

**`mac_Wrapper`：RX 三级串联，TX 三级前缀。** 它的 RX 不是一次剥完，而是「目的 MAC 过滤 → 源 MAC 过滤 → EthType 分发」三级子核串联：

```vhdl
RX_DestMAC : entity PoC.mac_RX_DestMAC_Switch  -- 按「目的 MAC 是否命中本机接口」接收/拒绝
   ...
RX_EthType : entity PoC.mac_RX_Type_Switch     -- 读 EthType，分发到对应上层端口
   port map ( ... ETHERNET_TYPES => SWITCH_TYPES,
              Out_Valid => RX_Valid(PORT_INDEX_TO downto PORT_INDEX_FROM), ... );
```
[src/net/mac/mac_Wrapper.vhdl:203-227](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/mac/mac_Wrapper.vhdl#L203-L227)（DestMAC）与 [src/net/mac/mac_Wrapper.vhdl:290-321](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/mac/mac_Wrapper.vhdl#L290-L321)（EthType 分发）。注意它用 `genInterface` 对每个配置的接口展开一整套 RX 子链路（[src/net/mac/mac_Wrapper.vhdl:229-350](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/mac/mac_Wrapper.vhdl#L229-L350)），并把对外端口编号映射回 `RX_Valid(PORT_INDEX_TO downto PORT_INDEX_FROM)` 这段切片——端口数 `PORTS` 由 `getPortCount(MAC_CONFIG)` 在 elaboration 期算出（[src/net/mac/mac_Wrapper.vhdl:167-168](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/mac/mac_Wrapper.vhdl#L167-L168)）。

TX 方向则是三段前缀：每个接口先 `mac_TX_Type_Prepender`（前缀 EthType），再共用 `mac_TX_SrcMAC_Prepender`（前缀源 MAC）、`mac_TX_DestMAC_Prepender`（前缀目的 MAC），最后从 `Eth_TX_*` 送出（[src/net/mac/mac_Wrapper.vhdl:381-401](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/mac/mac_Wrapper.vhdl#L381-L401)）。这正是「TX 漏斗」逐层加头的实例。

#### 4.2.4 代码实践

**实践目标**：确认三个 Wrapper 的 RX 分发字段，体会「同骨架、换字段」。

**操作步骤**：

1. 在 `udp_Wrapper.vhdl` 搜 `StmDeMux_Control`，确认它比较 `DestPort = PORTPAIRS(i).Ingress`。
2. 在 `ipv4_Wrapper.vhdl` 搜 `StmDeMux_Control`，确认它比较 `Protocol = PACKET_TYPES(i)`。
3. 在 `mac_Wrapper.vhdl` 找到 `mac_RX_Type_Switch` 的 `ETHERNET_TYPES` generic，确认它按 EthType 分发。

**需要观察的现象**：三处分发逻辑结构几乎一致（`for-generate` 生成 one-hot `Control`），只是被比较的字段不同。

**预期结果**：你能用一句话总结——「Wrapper 的 RX 就是：parser 提取关键字段 → 逐端口相等比较生成 one-hot → `stream_DeMux` 分发」。

> 说明：本实践为源码阅读型，未执行仿真命令；如需运行，可参考 [u4-l2（测试台结构与编写）](u4-2-writing-testbenches.md) 搭建 testbench，但需自备 PHY 侧激励，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `mac_Wrapper` 的 RX 要先做 `RX_DestMAC_Switch` 再做 `RX_Type_Switch`，能否反过来？

> **答**：不能。目的 MAC 过滤是「这个帧是不是发给我的」的准入判断——若目的 MAC 既不是本机接口也不是广播，帧就该在链路层被丢弃，没必要再看 EthType。先过滤可避免对无关帧做无用的协议解析，节省功耗与后续带宽。

**练习 2**：`ipv4_Wrapper` 的 TX 链里，`stream_Buffer`（[src/net/ipv4/ipv4_Wrapper.vhdl:260-289](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/ipv4/ipv4_Wrapper.vhdl#L260-L289)）起什么作用？

> **答**：它把每个上层端口送上来的帧缓存一拍以上，解耦「上层发送速率」与「`stream_Mux` / ARP 查询是否就绪」。当 ARP 还在查 MAC、下游暂时收不下时，buffer 能吸收一个未完成帧，配合其 commit/rollback 机制在查询失败时回滚。

---

### 4.3 帧校验与缓存：横跨全栈的两类服务

分好层、封好装之后，还有两件事每层都可能用到：**给帧算一个校验和**、**把「地址→地址」的映射缓存起来**。PoC 把它们抽成可复用组件。

#### 4.3.1 概念说明

**互联网校验和（Internet Checksum）。** UDP/TCP/ICMP 的校验和用的是同一种算法（RFC 1071）：把报文按 16 位字大端拼接，做**反码和（one's complement sum）**，遇到进位要折回低位（end-around carry），最后对结果取反。UDP 还要在报文前虚拟一个「伪头部」（含源/目的 IP、协议号、长度）一起参与求和，目的是让接收方顺带校验 IP 地址没被篡改。

由于帧是**逐字节**到达的，校验和必须在线累加，不能等整帧到齐再算。`net_FrameChecksum` 就是一个流式累加器：边收边加，到 EOF 时把累加结果和帧长度一起吐出。

**ARP 缓存。** IPv4 发包前必须知道目的 MAC。不可能每发一个包都广播一次 ARP 请求，于是把「IP→MAC」映射存进一张表，未命中才广播请求并等响应、命中则直接查表。这张表会被多个来源读写，所以用一个带替换策略（LRU）的缓存实现——它直接复用了 [u5-l3](u5-l3-cache-subsystem.md) 讲过的 `PoC.cache` 子系统。

#### 4.3.2 核心流程

**流式校验和累加**（每收一个数据字节 `d`）：

\[ S_{\text{next}} = S + d + \text{carry}(S) \]

其中 `carry` 是上一拍的进位位，折回到低位参与下一拍加法（end-around carry）。到 EOF 时：

\[ \text{Checksum} = \sim S \quad (\text{取反码}) \]

由于字节流可能字节数为奇（最后一个字只有高字节），实现里用 `ST_CARRY_1` / `ST_CARRY_2` 两个状态补一个 0 字节凑齐 16 位，再决定是否做字节序交换（大端在 wire 上）。

**ARP 缓存查询**（请求一个 IPv4 对应的 MAC）：

```text
查表 (Lookup IPv4)
  ├─ HIT   → 直接读出 MAC，本次完成
  └─ MISS  → 广播 ARP 请求 (BroadCast_Requester)
              → 启动超时计数器
              → 等单播响应 (UniCast_Receiver)
                  ├─ 收到  → 写回缓存 (CMD_ADD)，再回到查表（这次必命中）
                  ├─ 超时  → 重发广播请求
                  └─ 出错  → 进入 ST_ERROR
```

#### 4.3.3 源码精读

**`net_FrameChecksum`：一个核同时管「数据缓冲 + 元数据缓冲 + 校验和」。** 它的对外端口同时有数据流和元数据流：

```vhdl
port (
  ... In_Valid, In_Data, In_SOF, In_EOF, In_Ack,       -- 输入帧流
      In_Meta_rst, In_Meta_nxt, In_Meta_Data, ...      -- 输入元数据（如源/目的 IP、端口）
  ... Out_Valid, Out_Data, Out_SOF, Out_EOF, Out_Ack,  -- 输出帧流（透传）
      Out_Meta_Length   : out T_SLV_16;                 -- 输出补充：帧长度
      Out_Meta_Checksum : out T_SLV_16                  -- 输出补充：校验和
);
```
[src/net/net_FrameChecksum.vhdl:41-71](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/net_FrameChecksum.vhdl#L41-L71) — 它像一条「插入帧流的管道」：帧数据原样穿过，但边上多算出 Length 和 Checksum 两个 16 位值，随帧的 Meta 一起输出。

核心累加是一个 17 位累加器（含进位位），三行并发赋值实现「反码和 + end-around carry」：

```vhdl
Checksum0_nxt_cy <= Checksum0_nxt_us(Checksum0_nxt_us'high);                                -- 进位位
Checksum0_nxt_us <= ('0' & Checksum1_d_us) + ('0' & Checksum_Data_us)
                    + ((Checksum1_d_us'range => '0') & Checksum0_d_us(Checksum0_d_us'high)); -- 把上一拍进位折回低位
Checksum1_nxt_us <= Checksum0_d_us(Checksum1_d_us'range);
```
[src/net/net_FrameChecksum.vhdl:292-294](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/net_FrameChecksum.vhdl#L292-L294) — 字节级累加，进位位在下一拍折回，正是 RFC 1071 反码和的硬件化写法。

写侧 FSM 有 4 个状态，其中 `ST_CARRY_1/2` 专门处理奇数字节数的进位收敛：
[src/net/net_FrameChecksum.vhdl:79-81](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/net_FrameChecksum.vhdl#L79-L81)。

为了让「校验和」与「帧数据」在时间上对齐，它用两个 FIFO 缓冲：`DataFIFO`（`fifo_cc_got`）存帧字节，`MetaFIFO_Misc` 存校验和+长度；此外每个元数据子流各一个支持回滚的 `fifo_cc_got_tempgot`：

```vhdl
DataFIFO : entity PoC.fifo_cc_got
  generic map ( D_BITS => DataFIFO_DataIn'length, MIN_DEPTH => MAX_FRAME_LENGTH, ... )
  ...
MetaFIFO_Misc : entity PoC.fifo_cc_got
  generic map ( D_BITS => MetaFIFO_Misc_DataIn'length, MIN_DEPTH => MAX_FRAMES, ... )
```
[src/net/net_FrameChecksum.vhdl:315-338](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/net_FrameChecksum.vhdl#L315-L338) 与 [src/net/net_FrameChecksum.vhdl:343-366](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/net_FrameChecksum.vhdl#L343-L366)。元数据 FIFO 用 `tempgot`（[src/net/net_FrameChecksum.vhdl:424-450](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/net_FrameChecksum.vhdl#L424-L450)）是因为帧可能在中途被下游拒绝——此时要 `rollback` 丢弃已缓冲的元数据，而只有成功成交（`FrameCommit`）时才 `commit`：

```vhdl
FrameCommit <= DataFIFO_Valid and DataFIFO_DataOut(EOF_BIT) and Out_Ack;
MetaFIFO_Misc_got <= FrameCommit;
... MetaFIFO_Commit <= FrameCommit;  MetaFIFO_Rollback <= Out_Meta_rst;
```
[src/net/net_FrameChecksum.vhdl:371-372](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/net_FrameChecksum.vhdl#L371-L372) 与 [src/net/net_FrameChecksum.vhdl:452-454](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/net_FrameChecksum.vhdl#L452-L454)。这是 [u3-l4](u3-l4-fifo-family.md) 讲过的「暂存回滚 FIFO」在网络栈里的真实用法。

**`udp_Wrapper` 调用 `net_FrameChecksum` 算 UDP 校验和。** TX 链上，`stream_Mux` 合并后先穿过 `TX_FCS`（即 `net_FrameChecksum`）再进 `udp_TX`，这样 `udp_TX` 前缀 UDP 头时手里就有了校验和：

```vhdl
TX_FCS : entity PoC.net_FrameChecksum
  generic map ( MAX_FRAMES => 4, MAX_FRAME_LENGTH => 2048,
                META_BITS => TX_FCS_META_BITS, META_FIFO_DEPTH => TX_FCS_META_FIFO_DEPTHS )
  port map ( ... Out_Meta_Checksum => TX_FCS_Meta_Checksum, Out_Meta_Length => TX_FCS_Meta_Length );
```
[src/net/udp/udp_Wrapper.vhdl:329-359](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/udp/udp_Wrapper.vhdl#L329-L359)。

这里有一个 IPv4/IPv6 兼容细节：地址字节宽度不同（4 vs 16），故 Meta FIFO 深度随 `IP_VERSION` 变：

```vhdl
TX_FCS_META_FIFO_DEPTHS : T_POSVEC := (
  TX_FCS_META_STREAMID_SRCIP  => ite((IP_VERSION = 6), 16, 4),
  TX_FCS_META_STREAMID_DESTIP => ite((IP_VERSION = 6), 16, 4), ... );
```
[src/net/udp/udp_Wrapper.vhdl:179-185](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/udp/udp_Wrapper.vhdl#L179-L185) — 同一套 Wrapper 用 `IP_VERSION` generic 兼容两个 IP 版本，地址仍走「逐字节 Meta」，所以只需调深度。

**`arp_Wrapper`：ARP 缓存 + 请求超时。** 它内部有两条 FSM：`FSMPool`（响应别人的请求）与 `FSMCache`（自己查表/发请求）。`FSMCache` 的状态机覆盖了 4.3.2 的整条查询流程：

```vhdl
type T_FSMCACHE_STATE is (
  ST_IDLE,
    ST_CACHE, ST_CACHE_WAIT, ST_READ_CACHE,
    ST_SEND_BROADCAST_REQUEST, ST_SEND_BROADCAST_REQUEST_WAIT, ST_WAIT_FOR_UNICAST_RESPONSE,
    ST_UPDATE_CACHE,
  ST_ERROR
);
```
[src/net/arp/arp_Wrapper.vhdl:178-184](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/arp/arp_Wrapper.vhdl#L178-L184)。

缓存本体直接实例化 `arp_Cache`，并指定 LRU 替换策略与初始内容：

```vhdl
ARPCache : entity PoC.arp_Cache
  generic map ( CLOCK_FREQ => CLOCK_FREQ, REPLACEMENT_POLICY => "LRU",
                TAG_BYTE_ORDER => BIG_ENDIAN, DATA_BYTE_ORDER => BIG_ENDIAN,
                INITIAL_CACHE_CONTENT => INITIAL_ARPCACHE_CONTENT )
  port map ( ... CacheResult => ARPCache_CacheResult, ... );
```
[src/net/arp/arp_Wrapper.vhdl:718-746](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/arp/arp_Wrapper.vhdl#L718-L746) — `CacheResult` 是 `T_CACHE_RESULT`（`CACHE_RESULT_HIT` / `MISS`），命中/未命中由此驱动 FSM 走不同分支。这正是 [u5-l3](u5-l3-cache-subsystem.md) 缓存子系统在网络栈的落地。

请求超时把「人话时间」换成计数器宽度，复用 [u2-l4](u2-l4-physical-strings-vectors-math.md) 的 `physical` 包：

```vhdl
constant ARPREQ_TIMEOUTCOUNTER_MAX  : positive := TimingToCycles(APR_REQUEST_TIMEOUT, CLOCK_FREQ);
constant ARPREQ_TIMEOUTCOUNTER_BITS : positive := log2ceilnz(ARPREQ_TIMEOUTCOUNTER_MAX);
```
[src/net/arp/arp_Wrapper.vhdl:221-222](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/arp/arp_Wrapper.vhdl#L221-L222) — generic 默认 `APR_REQUEST_TIMEOUT : time := 100 ms`（[src/net/arp/arp_Wrapper.vhdl:44-51](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/arp/arp_Wrapper.vhdl#L44-L51)），在 125 MHz 下被换算成具体计数值。

未命中时由 `arp_BroadCast_Requester` 生成广播请求帧：
[src/net/arp/arp_Wrapper.vhdl:748-776](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/arp/arp_Wrapper.vhdl#L748-L776)；它和「响应别人」的 `arp_UniCast_Responder` 两路 TX 输出，经一个 `stream_Mux` 合并成一条上行帧流：
[src/net/arp/arp_Wrapper.vhdl:826-852](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/arp/arp_Wrapper.vhdl#L826-L852)（TX 漏斗模式的又一实例）。

#### 4.3.4 代码实践

**实践目标**：验证「校验和」与「缓存查询」在源码里的真实信号流。

**操作步骤**：

1. 在 `udp_Wrapper.vhdl` 中跟踪 `TX_FCS_Meta_Checksum`：它由 `net_FrameChecksum` 的 `Out_Meta_Checksum` 产生（[L357](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/udp/udp_Wrapper.vhdl#L357)），再接到 `udp_TX` 的 `In_Meta_Checksum`（[L395](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/net/udp/udp_Wrapper.vhdl#L395)）。确认这是一条「校验和随帧同行」的边带通路。
2. 在 `arp_Wrapper.vhdl` 中找到 `ARPCache_CacheResult` 的三处使用点，确认：未命中（`CACHE_RESULT_MISS`）跳到 `ST_SEND_BROADCAST_REQUEST`，命中（`CACHE_RESULT_HIT`）跳到 `ST_READ_CACHE`。

**需要观察的现象**：校验和是**边带计算**，帧数据本身不被修改地穿过 `net_FrameChecksum`；缓存查询结果是**枚举值**，直接驱动 FSM 下一态。

**预期结果**：你能画出 `Checksum` 与 `CacheResult` 两条控制信号在各自 Wrapper 内的传递路径。

> 说明：本实践为源码跟踪型，未执行仿真。若要在波形上观察 `Checksum` 实际值，需用 GHDL/NVC 跑 net 栈 testbench——但本仓库 `tb/net/` 目录下当前无已发布的 testbench（与 `stack` 子命名空间同样待补），**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`net_FrameChecksum` 为什么要同时例化 `fifo_cc_got` 和 `fifo_cc_got_tempgot` 两类 FIFO？

> **答**：`fifo_cc_got`（`DataFIFO` / `MetaFIFO_Misc`）只需「先进先出」地把帧字节和算好的 Length/Checksum 缓冲到下游能接收时，不需要回滚。而每路输入元数据（`MetaFIFO_*`）用 `fifo_cc_got_tempgot`，是因为下游可能拒收当前帧（`Out_Meta_rst` 触发 rollback），此时要能把这一帧的元数据丢弃；只有成交时才 commit。两类 FIFO 对应「不可回滚的帧体/结果」与「可回滚的输入元数据」两种语义。

**练习 2**：UDP 校验和计算为什么要把源/目的 IP 也喂给 `net_FrameChecksum`（通过 `TX_FCS_META`）？

> **答**：因为 UDP 校验和覆盖「伪头部」（含源 IP、目的 IP、协议号、UDP 长度）。把 IP 地址作为元数据喂给校验和核，让它一并参与反码和，接收方就能顺带验证 IP 地址未被篡改。这也是为什么 `TX_FCS_META_BITS` 里专门有 `SRCIP` / `DESTIP` 槽位。

**练习 3**：ARP 缓存用 LRU 替换策略，对一个「少量邻居反复通信」的场景合不合适？为什么？

> **答**：合适。LRU（最近最少使用）会淘汰最久没通信的表项；少量邻居反复通信时，活跃表项总被刷新从而保留，冷的偶发表项被淘汰，命中率最高。这正是 [u5-l3](u5-l3-cache-subsystem.md) 引入的局部性原理在 ARP 表上的体现。

## 5. 综合实践

**任务**：手动梳理一个「发往本机的 UDP 包」从 `mac_Wrapper` 进入、到 `udp_Wrapper` 输出给应用的完整 RX 路径，画出逐层处理图并标注每层完成的工作。

假设条件：

- 本机配置了 1 个 MAC 接口，`mac_Wrapper` 的 `TypeSwitch` 允许 EthType=`0x0800`（IPv4）。
- `ipv4_Wrapper` 的 `PACKET_TYPES` 含 `x"11"`（UDP）。
- `udp_Wrapper` 的 `PORTPAIRS(0).Ingress = x"0007"`（Echo 端口 7）。
- 收到的帧：目的 MAC 命中本机、EthType=0x0800、IPv4 Protocol=0x11、UDP 目的端口=7。

**要求产出**：

1. 一张从 `Eth_RX_*` 到应用端口 `RX_Data(0)` 的方框流程图。
2. 每个方框旁注明：所用子核名、提取/检查的字段、本框「做了什么」（如「剥 MAC 头并校验目的 MAC」「按 EthType=0x0800 选通 IPv4 端口」）。
3. 标出「关键字段」在三层中分别是 EthType、Protocol、DestPort，并指出它们各自对应源码里的 `Control` 生成语句位置。

**参考骨架**（请自行补全字段与子核名）：

```text
Eth_RX_* ─▶ [mac_Wrapper.RX]
              ├─ mac_RX_DestMAC_Switch   检查：目的 MAC 是否命中本机接口
              ├─ mac_RX_SrcMAC_Filter    可选：源 MAC 过滤
              └─ mac_RX_Type_Switch      按 EthType=0x0800 选通 → IPv4 端口
                    │  (RX_Valid(port_i), Meta 携带 SrcMAC/DestMAC/EthType)
                    ▼
            [ipv4_Wrapper.RX]
              ├─ ipv4_RX                 剥 IPv4 头，提取 SrcIP/DestIP/Protocol/Length
              └─ stream_DeMux            按 Protocol=0x11 选通 → UDP 端口
                    │  (Meta 增加 SrcIP/DestIP/Length/Protocol)
                    ▼
            [udp_Wrapper.RX]
              ├─ udp_RX                  剥 UDP 头，提取 SrcPort/DestPort/Length
              └─ stream_DeMux            按 DestPort=7 选通 → 应用端口 0
                    │
                    ▼
              应用层 RX_Data(0) = UDP 载荷
```

完成后，再对照本讲 4.2.3 节给出的三个 `StmDeMux_Control` / `mac_RX_Type_Switch` 源码点，核对你标注的字段是否与代码一致。

## 6. 本讲小结

- `PoC.net` 按 OSI 分层组织子命名空间（`eth/mac` → `arp/ipv4/ipv6` → `udp`），全部跨层共享类型集中在根包 `net.pkg.vhdl`。
- 分层的本质是「逐层多路分解」：链路层按 EthType、网络层按 Protocol、传输层按目的端口，三者都是「拿头部某字段去和具名常量比较」。
- 每层的 `<layer>_Wrapper` 都是同一副骨架：TX 是 `stream_Mux`（N→1）漏斗 + prepender 逐层前缀头，RX 是 parser 剥头 + `stream_DeMux`（1→N）分叉。
- 统一流式接口（`Valid/Data/SOF/EOF/Ack` + `Meta_rst/nxt/Data` 边带）让 8 位数据流与可变宽地址（MAC/IP/端口）解耦，同一套握手兼容 IPv4/IPv6。
- `net_FrameChecksum` 是流式互联网校验和累加器，用 `fifo_cc_got` + 可回滚 `fifo_cc_got_tempgot` 让校验和/长度与帧对齐且支持丢弃。
- `arp_Wrapper` 复用 `PoC.cache`（LRU）做 IPv4→MAC 缓存，未命中时广播请求 + 超时重发，体现网络栈对缓存与流式组件的横向复用。

## 7. 下一步学习建议

- **横向看姊妹实现**：读 `ipv6_Wrapper.vhdl`、`icmpv4_Wrapper.vhdl`，验证它们是否同样遵循「TX 漏斗 + RX 分叉」骨架，体会模板的可复用性。
- **向下追 PHY**：本讲止步于 `mac_Wrapper` 的 `Eth_TX/RX_*` 接口。若关心 GMII/RGMII/SGMII 如何对接真实 PHY 芯片，可读 `src/net/eth/`（目前主要为接口记录与 PHY 控制器类型定义，见 `net.pkg.vhdl` 第 92-154 行的 `T_NET_ETH_PHY_INTERFACE_*` 记录）。
- **补 stack 层**：`src/net/stack/` 目前为空，建议作为练手——参考本讲综合实践，尝试写一个顶层把 `mac_Wrapper` + `arp_Wrapper` + `ipv4_Wrapper` + `udp_Wrapper` 连成完整 UDP/IPv4 栈（注意 ARP 与 IPv4 之间的 `IPCache_*` 互联信号要一一对接）。
- **复习依赖**：若对流式握手、`stream_Mux`/`DeMux` 的 `Control` 生成仍有疑问，回看 [u5-l4](u5-l4-bus-stream-protocols.md)；若对缓存命中/缺失与 LRU 不熟，回看 [u5-l3](u5-l3-cache-subsystem.md)。
