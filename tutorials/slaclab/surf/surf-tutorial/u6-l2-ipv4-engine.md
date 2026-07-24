# IPv4 引擎：ARP / ICMP / IGMP

## 1. 本讲目标

本讲承接 u6-l1 的以太网 MAC 核心。`EthMacCore` 只负责「收发一帧完整的以太网帧 + 校验 IPv4 头校验和」，但它并不认识 IP 地址，也不会回应 `ping`。真正让这块网卡「像一个网络节点」的，是本讲的 `IpV4Engine`：它在 MAC 之上再搭一层 IPv4 协议栈，完成三件事——

- **IPv4 收发与协议解复用**：把进来的 IPv4 帧剥掉 IP 头、按协议号（UDP/ICMP/IGMP…）分发给对应的协议引擎；把协议引擎送出来的数据加上 IP 头和以太网头发出去。
- **ARP**：把「IP 地址」翻译成「MAC 地址」，否则连以太网帧的目的 MAC 都填不出来。
- **ICMP / IGMP**：在 FPGA 内部就地回应 `ping`（ICMP echo），并支持 IGMPv2 组播成员上报。

学完本讲你应当能够：

1. 画出 `IpV4Engine` 顶层的数据通路：`DeMux → (ARP / IPv4 Rx) → 协议引擎 → IPv4 Tx → Mux`。
2. 说清楚 `IpV4EngineRx` 如何剥 IP 头、`IpV4EngineTx` 如何加 IP 头，以及二者共享的「非标准 IPv4 伪头部」约定。
3. 描述 `ArpEngine` 的请求/应答状态机、1 秒客户端缓存超时、以及本地回环短路。
4. 解释 `IcmpEngine` 如何用「交换源/目的 IP + 翻转类型 + 增量修校验和」就地生成 echo reply。
5. 描述一次完整 `ping` 往返在四个引擎之间的流转。

## 2. 前置知识

在进入源码前，先建立三条直觉。

**第一，以太网帧在 SURF 里是「小端字节排列的 128 位 AXI-Stream」。** `EthMacPkg` 把以太网流配置成 16 字节（128 位）一拍、`TUSER_MODE = TKEEP_FIRST_LAST_C`，即 SOF/EOFE 编码在 TUSER 里（见 u5-l1、u6-l1）。一帧的第一个 16 字节里：

| 字节偏移 | 含义 | 对应 `tData` 位段 |
|---|---|---|
| 0–5 | 目的 MAC | `tData(47:0)` |
| 6–11 | 源 MAC | `tData(95:48)` |
| 12–13 | EtherType | `tData(111:96)` |
| 14 | IPv4 版本+头长（IHL） | `tData(119:112)` |
| 15 | DSCP/ECN | `tData(127:120)` |

因为字节 0 落在 `tData` 最低位，所以 EtherType `0x0800`（IPv4）在线缆上是「字节12=0x08, 字节13=0x00」，拼到 `tData(111:96)` 就成了 `x"0008"`——这就是 [EthMacPkg.vhd:30-37](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacPkg.vhd#L30-L37) 里那些「看起来反序」的常量（`IPV4_TYPE_C = x"0008"`、`ARP_TYPE_C = x"0608"`）的由来。这一点 u6-l1 已详细说明，本讲直接沿用。

**第二，IPv4 头里决定路由的两个关键字段是「协议号」和「源/目的 IP」。** 协议号占 1 字节，常见值：ICMP=0x01、IGMP=0x02、TCP=0x06、UDP=0x11（见同一组常量）。`IpV4Engine` 就是靠协议号把 IP 包分发到不同子引擎的。

**第三，本讲所有模块都沿用 u1-l5 的双进程骨架（`RegType` / `REG_INIT_C` / `r` / `rin` / `comb` / `seq`）和 `TPD_G` / `RST_POLARITY_G` / `RST_ASYNC_G` 三件套。** 看状态机时，把注意力放在 `comb` 进程的 `case r.state is` 上即可。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [ethernet/IpV4Engine/rtl/IpV4Engine.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/IpV4Engine.vhd) | 纯结构顶层。把 DeMux、Mux、ArpEngine、Rx、Tx、IcmpEngine（可选 IgmpV2Engine）连成完整协议栈。 |
| [ethernet/IpV4Engine/rtl/IpV4EngineDeMux.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/IpV4EngineDeMux.vhd) | 入口分流：按 EtherType 把帧分给 ARP 或 IPv4 通路。 |
| [ethernet/IpV4Engine/rtl/IpV4EngineRx.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/IpV4EngineRx.vhd) | IPv4 接收：剥 IP 头、解析协议号、按协议分发到子引擎。 |
| [ethernet/IpV4Engine/rtl/IpV4EngineTx.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/IpV4EngineTx.vhd) | IPv4 发送：给子引擎数据加上以太网头+IP 头，区分本地回环/外发。 |
| [ethernet/IpV4Engine/rtl/ArpEngine.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/ArpEngine.vhd) | ARP 引擎：请求/应答、IP→MAC 客户端缓存、1 秒超时。 |
| [ethernet/IpV4Engine/rtl/IcmpEngine.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/IcmpEngine.vhd) | ICMP 引擎：就地回 `ping`（echo request → echo reply）。 |
| [ethernet/IpV4Engine/rtl/IgmpV2Engine.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/IgmpV2Engine.vhd) | IGMPv2 引擎：响应组成员查询、上报 Membership Report。 |
| [ethernet/EthMacCore/rtl/EthMacPkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacPkg.vhd) | 提供 EtherType / 协议号常量与 `EMAC_AXIS_CONFIG_C`。 |

---

## 4. 核心概念与源码讲解

### 4.1 IPv4 收发与协议解复用

#### 4.1.1 概念说明

`IpV4Engine` 要同时服务「多个上层协议」（UDP、ICMP、IGMP……），又要对接「一条 MAC 数据流」。它采用的架构是经典的**入口解复用 + 出口复用**：

- **入口**：`IpV4EngineDeMux` 按 EtherType 把进来的以太网帧切成两路——ARP（`0x0806`）和 IPv4（`0x0800`）。
- **IPv4 收**：`IpV4EngineRx` 把 IPv4 帧的 IP 头剥掉，**按协议号**用 `tDest` 路由到对应子引擎。
- **IPv4 发**：`IpV4EngineTx` 给子引擎送来的数据重新加上 IP 头和以太网头。
- **出口**：`AxiStreamMux` 把 ARP 通路和 IPv4 通路合并回一条 MAC 发送流。

整个顶层是纯结构（`architecture mapping`），不含状态机，只做连线。

#### 4.1.2 核心流程

```text
                 ┌───────────────┐
  obMacMaster ──▶│ IpV4EngineDeMux│── ARP  ─────────────▶┐ (到 ArpEngine)
  (MAC 收)       │  按 EtherType  │── IPv4 ──▶ IpV4EngineRx ──┐
                 └───────────────┘                          │
                                                            ▼
                                              ┌─────────────────────────┐
                                              │ 按 protocol 号 DeMux 到  │
                                              │ UDP / ICMP / IGMP 子引擎 │
                                              └─────────────────────────┘
                                                            │
                      ┌── ARP  ◀─────────────── AxiStreamMux ◀── obArpMaster
  ibMacMaster ◀── Mux │
  (MAC 发)            └── IPv4 ◀── IpV4EngineTx ◀── (子引擎发送数据)
```

关键设计：顶层用编译期函数 `genIPv4List` 把用户传入的 `PROTOCOL_G`（默认只有 UDP）**追加** ICMP（必要时再加 IGMP），拼成内部协议表 `PROTOCOL_C`。这样用户协议占表头，ICMP/IGMP 固定占表尾，索引位置确定，便于把固定槽连到内部引擎。

#### 4.1.3 源码精读

**协议表的编译期拼装** —— [IpV4Engine.vhd:62-77](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/IpV4Engine.vhd#L62-L77)：

```vhdl
constant PROTOCOL_SIZE_C : positive := ite(IGMP_G, PROTOCOL_SIZE_G+2, PROTOCOL_SIZE_G+1);
...
retVar(PROTOCOL_SIZE_G) := ICMP_C;          -- ICMP 固定接在用户协议后面
if IGMP_G then
   retVar(PROTOCOL_SIZE_G+1) := IGMP_C;     -- 开启 IGMP 时再占一个槽
end if;
```

注意 `PROTOCOL_SIZE_C` 比 `PROTOCOL_SIZE_G` 多 1 或 2：多出来的就是 ICMP（和 IGMP）。这个常量决定了内部总线的宽度，并被传给 `IpV4EngineRx`/`Tx`。

**顶层连线** —— DeMux / Mux / 三个引擎的例化见 [IpV4Engine.vhd:99-251](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/IpV4Engine.vhd#L99-L251)。其中把内部固定槽连到 ICMP/IGMP 引擎、把用户槽透传到外部 `ibProtocolMasters`/`obProtocolSlaves` 的两段是：

```vhdl
-- ICMP 占 PROTOCOL_SIZE_G+0 槽  (IpV4Engine.vhd:215-218)
ibIcmpMaster => ibMasters(PROTOCOL_SIZE_G+0),
obIcmpMaster => obMasters(PROTOCOL_SIZE_G+0),

-- 用户协议槽 0..PROTOCOL_SIZE_G-1 直接透传到端口 (IpV4Engine.vhd:245-251)
for i in (PROTOCOL_SIZE_G-1) downto 0 generate
   obMasters(i)         <= obProtocolMasters(i);
   ibProtocolMasters(i) <= ibMasters(i);
   ...
end generate;
```

**入口分流（DeMux）** —— [IpV4EngineDeMux.vhd:98-111](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/IpV4EngineDeMux.vhd#L98-L111) 在帧首拍同时检查 EtherType、目的 MAC、IP 版本：

```vhdl
if (obMacMaster.tData(111 downto 96) = ARP_TYPE_C) then       -- EtherType = ARP
   if(dest MAC = 广播) or (dest MAC = localMac) then arpSel  := '1'; ... end if;
elsif (obMacMaster.tData(111 downto 96) = IPV4_TYPE_C)
   and (obMacMaster.tData(119 downto 112) = x"45") then        -- IPv4 且 版本4/头长5(20B)
   if(dest MAC = 广播) or (dest MAC = localMac) then ipv4Sel := '1'; ... end if;
end if;
```

`x"45"` 是 IPv4 头的「版本=4、IHL=5（即 20 字节定长头）」，`IpV4EngineRx` 后续正是按 20 字节头去剥的。不匹配 EtherType、或目的 MAC 既不是本机也不是广播的帧会被静默丢弃（`arpSel`/`ipv4Sel` 保持 0，不再向下游转发）。`arpSel`/`ipv4Sel` 是帧级锁存，一旦在 SOF 拍选定，整帧都路由到同一路，到 `tLast` 复位（[IpV4EngineDeMux.vhd:112-121](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/IpV4EngineDeMux.vhd#L112-L121)）。

**IPv4 接收：剥头 + 协议路由** —— `IpV4EngineRx` 的状态机 [IpV4EngineRx.vhd:51-57](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/IpV4EngineRx.vhd#L51-L57) 是 `IDLE_S → IPV4_HDR0_S → IPV4_HDR1_S → (IPV4_HDR2_S →) MOVE_S → LAST_S`。它在 `IDLE_S` 记下源 MAC（当作「remote MAC」留给将来回包用）[IpV4EngineRx.vhd:138-139](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/IpV4EngineRx.vhd#L138-L139)，在 `IPV4_HDR0_S`（IP 头第二拍，含协议号、源/目的 IP）解析关键字段并按协议号查表路由：

```vhdl
v.protocol := rxMaster.tData(63 downto 56);          -- 协议号 (IpV4EngineRx.vhd:157)
v.txMaster.tData(95 downto 64) := rxMaster.tData(111 downto 80);  -- 源 IP -> remote IP
v.txMaster.tData(111 downto 96):= rxMaster.tData(127 downto 112); -- 目的 IP 高16 -> local IP
v.state := IDLE_S;                                   -- 默认丢弃
for i in (PROTOCOL_SIZE_G-1) downto 0 loop           -- 协议号匹配则路由
   if (v.protocol = PROTOCOL_G(i)) then
      v.txMaster.tDest := toSlv(i, 8);               -- 用 tDest 选中子引擎
      v.state := IPV4_HDR1_S;
   end if;
end loop;
```

匹配不上的协议号会留在 `IDLE_S`，帧被丢弃。匹配上之后，`IPV4_HDR1_S`（[IpV4EngineRx.vhd:175-204](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/IpV4EngineRx.vhd#L175-L204)）把后续数据重新打包成「非标准 IPv4 伪头部」输出，并由末端的 `AxiStreamDeMux` 按 `tDest` 分发到各子引擎端口（[IpV4EngineRx.vhd:310-326](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/IpV4EngineRx.vhd#L310-L326)）。

**「非标准 IPv4 伪头部」约定** —— Rx 剥掉 IP 头后，输出帧的首拍不再是 IPv4 头，而是这样一个统一头部（注释见 [IcmpEngine.vhd:93-108](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/IcmpEngine.vhd#L93-L108)）：

| 位段 | 含义 |
|---|---|
| `tData(47:0)` | remote MAC（即收到帧的源 MAC，回包的目的 MAC） |
| `tData(63:48)` | 保留 0 |
| `tData(95:64)` | 源 IP（对收方向 = 对端 IP） |
| `tData(127:96)` | 目的 IP（对收方向 = 本机 IP） |
| 第二拍 `tData(15:8)` | 协议号 |
| 第二拍 `tData(31:16)` | 伪头部长度（= IP 总长 − 20） |

「非标准」在于它把 remote MAC、协议号、长度都塞进了头部——这恰好是 `IpV4EngineTx` 重建一帧所需的最小信息。于是 ICMP/IGMP/UDP 子引擎只需读写这个统一头部，不用各自重新解析 IP 头。

**IPv4 发送：重建 IP 头** —— `IpV4EngineTx` 在 `IDLE_S` 用伪头部里的目的 MAC 填以太网头、判断本地回环，在 `IPV4_HDR0_S` 填 TTL/协议号/IP 等：

```vhdl
v.txMaster.tData(111 downto 96) := IPV4_TYPE_C;      -- EtherType (IpV4EngineTx.vhd:159)
v.txMaster.tData(119 downto 112):= x"45";            -- 版本4/头长5
v.txMaster.tData(55 downto 48) := TTL_G;             -- 跳数 (默认 0x20=32)
v.txMaster.tData(63 downto 56) := PROTOCOL_G(conv_integer(r.tDest));  -- 由来源 tDest 反查协议号
```

注意第 181 行的精妙之处：**协议号不是写死的，而是用子引擎的 `tDest`（= 它在协议表里的下标）反查 `PROTOCOL_G` 得到**。这正是前面「Rx 用 tDest 路由、Tx 用 tDest 反查」的对称设计。另外 [IpV4EngineTx.vhd:149-155](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/IpV4EngineTx.vhd#L149-L155) 实现了**本地回环**：若目的 MAC == 本机 MAC，则把输出 `tDest` 置为 `0x01`，经末端 DeMux（[IpV4EngineTx.vhd:343-361](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/IpV4EngineTx.vhd#L343-L361)）送回 `IpV4EngineRx` 的 `localhost` 输入，让设备能「ping 自己」。IP 总长和 IP 头校验和这两栏在 Tx 里留 0（[IpV4EngineTx.vhd:174-175](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/IpV4EngineTx.vhd#L174-L175)、`182-183`），由下游 `EthMacCore` 计算（见 Rx/Tx 文件头注释 “IPv4 checksum checked in EthMac core”）。

#### 4.1.4 代码实践

**实践目标**：用现成的 cocotb 回归测试，验证「IPv4 顶层把入向 UDP 帧路由到正确的协议输出槽」。这是一个可运行实践。

**操作步骤**：

1. 按 u1-l2 / u9-l1 的流程先做源缓存：`make MODULES=$PWD import`。
2. 进入虚拟环境后跑 IPv4 顶层测试（`待本地验证`）：

   ```bash
   ./.venv/bin/python -m pytest -q tests/ethernet/IpV4Engine/test_IpV4Engine.py
   ```

3. 打开 [tests/ethernet/IpV4Engine/test_IpV4Engine.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/ethernet/IpV4Engine/test_IpV4Engine.py)，对照它的方法学头（“inbound UDP routing / outbound protocol TX / ICMP echo / ARP client lookup” 四个场景）。

**需要观察的现象**：测试会断言「入向 UDP 流应出现在协议输出槽上、且为一个伪头部帧」「出向协议流量应在 MAC 输出上呈现为线格式 IPv4 帧」。

**预期结果**：四组场景全部 PASS。若失败，先核对 `localIp`/`localMac` 配置字是否与测试构造的帧目的地址一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `IpV4EngineDeMux` 在检查 EtherType 之外，还要同时检查 `tData(119:112) = x"45"` 和目的 MAC？

**答案**：`x"45"` 确保这是一个「版本 4、头长 20 字节」的标准 IPv4 包，`IpV4EngineRx` 才能按固定 20 字节剥头；目的 MAC 检查（本机或广播）确保只处理发给自己的帧，丢弃其余帧以省下游带宽。

**练习 2**：`IpV4EngineTx` 第 181 行用 `PROTOCOL_G(conv_integer(r.tDest))` 取协议号，而不是直接写 `UDP_C`。这样设计的好处是什么？

**答案**：Tx 与 Rx 对称——Rx 用协议号查表得到 `tDest` 路由进子引擎，Tx 用子引擎回送的 `tDest` 反查同一张表得到协议号写回 IP 头。这样添加/移除一个协议只需改协议表，Tx/Rx 都自动适配，无需硬编码。

---

### 4.2 ARP 缓存引擎（ArpEngine）

#### 4.2.1 概念说明

以太网寻址靠 MAC，而软件只知道 IP。**ARP（Address Resolution Protocol）** 就是「IP → MAC」的查询协议：想给某个 IP 发帧但不知道它的 MAC 时，就广播一个 ARP 请求，拥有该 IP 的节点单播回一个 ARP 应答。

`ArpEngine` 同时扮演三个角色：

1. **服务端**：响应别人发来的、目标 IP 是本机的 ARP 请求。
2. **客户端缓存**：替本板上的其它引擎（如 UDP）查询远端 IP 的 MAC，查到后回 ACK；并用一个 **1 秒超时** 的定时器管理缓存有效期。
3. **本地短路**：若客户端要查的 IP 就是本机 IP，直接回本机 MAC，连请求都不发。

#### 4.2.2 核心流程

`ArpEngine` 是一个 5 状态机（[ArpEngine.vhd:64-69](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/ArpEngine.vhd#L64-L69)）：

```text
        ┌──── 有入向帧 ────▶ RX_S ─── 收满3字、校验EOFE ───▶ CHECK_S
        │                                                     │
        │                ┌────────────────────────────────────┤
        │                ▼ (请求 & 目标IP=本机)              ▼ (应答 & 目标=本机)
        │              改写成应答帧                         SCAN_S: 轮询客户端,
        │                ▼                                   匹配 IP 则回 MAC+ACK,
        │               TX_S ◀─────────────────────────────  清计时器
        │
        └──── 无入向帧: 轮询客户端 reqCnt ──▶ 有请求 & 计时器=0 ?
                                          │
                                 是本机IP ▶ 直接 ACK 本机MAC (短路)
                                 否       ▶ 构造广播 ARP 请求 ▶ TX_S
```

ARP 分组固定 28 字节，加上 14 字节以太网头共 42 字节 = 2 个满 16 字节字 + 10 字节，所以发送时最后一拍的 `tKeep = x"03FF"`（低 10 位有效，见 [ArpEngine.vhd:305](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/ArpEngine.vhd#L305)）。

客户端缓存用一组 1 秒倒计时器 `arpTimers`：每发起一次查询就置位 1 秒，期间即使应用层重复请求也不会重复发包；收到应答或命中本机 IP 时清零。

#### 4.2.3 源码精读

**ARP 常量** —— [ArpEngine.vhd:55-62](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/ArpEngine.vhd#L55-L62) 把协议字段（硬件类型=以太网 `0x0001`、协议类型=IP `0x0800`、操作码 请求 `0x0001`/应答 `0x0002`）按小端字节排列预定义成常量，与 u6-l1 的「反序」约定一致。`TIMER_1_SEC_C := getTimeRatio(CLK_FREQ_G, 1.0)` 用 u1-l4 的 `getTimeRatio` 把「1 秒」换算成本时钟域的周期数，所以 `CLK_FREQ_G` 必须配成真实时钟频率。

**客户端轮询 + 本地短路 + 计时器** —— [ArpEngine.vhd:133-175](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/ArpEngine.vhd#L133-L175)：

```vhdl
-- 计时器每拍自减 (ArpEngine.vhd:123-128)
for i in CLIENT_SIZE_G-1 downto 0 loop
   if r.arpTimers(i) /= 0 then  v.arpTimers(i) := r.arpTimers(i) - 1;  end if;
end loop;
...
when IDLE_S =>
   if (ibArpMaster.tValid = '1') then  v.state := RX_S;     -- 优先服务入向帧
   else
      -- 轮询下一个客户端
      if (arpReqMasters(r.reqCnt).tValid='1') and (r.arpTimers(r.reqCnt)=0) then
         v.arpTimers(r.reqCnt) := TIMER_1_SEC_C;            -- 1 秒内不重发
         if localIp = arpReqMasters(r.reqCnt).tData(31:0) then  -- 本机 IP: 短路
            v.arpAckMasters(r.ackCnt).tData(47:0) := localMac;  -- 直接回本机 MAC
         else                                                 -- 否则构造广播请求
            v.tData(0)(47:0) := BROADCAST_MAC_C; ...          -- (省略, 见 158-172 行)
            v.state := TX_S;
         end if;
      end if;
   end if;
```

注意优先级：**入向帧优先于客户端请求**（先 `if ibArpMaster.tValid` 再 `else` 处理客户端），避免在收应答的关键时刻被新查询打断。

**入向帧校验（CHECK_S）** —— [ArpEngine.vhd:233-266](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/ArpEngine.vhd#L233-L266) 先验证硬件/协议类型与长度全部合法，再分两种操作码处理：

- **请求且目标 IP = 本机**：把缓冲里的帧「原地改写」成应答——目的/源 MAC 对调、操作码改 `ARP_REPLY_C`、发送方字段填本机——然后转 `TX_S`（[ArpEngine.vhd:243-257](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/ArpEngine.vhd#L243-L257)）。
- **应答且目标 MAC+IP = 本机**：转 `SCAN_S` 去匹配等待中的客户端。

**应答匹配（SCAN_S）** —— [ArpEngine.vhd:268-289](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/ArpEngine.vhd#L268-L289) 轮询 `ackCnt`，若某客户端请求的 IP 等于应答里的源 IP，就把源 MAC 回填给该客户端（`arpAckMasters(ackCnt).tData(47:0)`）并清零它的计时器：

```vhdl
if arpReqMasters(r.ackCnt).tData(31 downto 0) = r.tData(1)(127 downto 96) then  -- IP 匹配
   v.arpReqSlaves(r.ackCnt).tReady := '1';                  -- 握手完成
   v.arpAckMasters(r.ackCnt).tData(47:0) := r.tData(1)(95:48);  -- 回填源 MAC
   v.arpTimers(r.ackCnt) := 0;                               -- 清缓存计时器
end if;
```

#### 4.2.4 代码实践

**实践目标**：源码阅读型实践——跟踪一次「客户端查询远端 IP」的完整调用链，并验证 ARP 请求/应答的内存布局。

**操作步骤**：

1. 打开 `ArpEngine.vhd`，从 `IDLE_S` 的客户端分支（第 148 行）开始，假设某客户端 `arpReqMasters(i).tData(31:0)` = 一个非本机 IP。
2. 跟踪 `tData(0/1/2)` 三个字在 [ArpEngine.vhd:158-172](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/ArpEngine.vhd#L158-L172) 的赋值，画出广播请求帧的 42 字节布局。
3. 对照 [tests/ethernet/IpV4Engine/ipv4_test_utils.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/ethernet/IpV4Engine/ipv4_test_utils.py) 里的 `build_arp_frame(...)`，确认你画的字节序与 Python 参考实现一致。

**需要观察的现象**：请求帧目的 MAC = `FF:FF:FF:FF:FF:FF`、EtherType = `0x0806`、操作码 = `0x0001`、发送方 MAC/IP = 本机、目标 MAC = 全 0（或广播）、目标 IP = 被查询 IP。

**预期结果**：你手画的布局与 `build_arp_frame` 逐字节吻合；并理解为什么发送状态里 `tKeep = x"03FF"`（10 字节尾）。

#### 4.2.5 小练习与答案

**练习 1**：`arpTimers` 计时器置为 `TIMER_1_SEC_C` 后，这一秒内同一客户端再次请求会发生什么？为什么这么设计？

**答案**：因为 `if ... and (r.arpTimers(r.reqCnt) = 0)` 条件不再成立，这一秒内不会重复发起 ARP 请求，但请求也不会被 ACK（`arpReqSlaves` 不拉 tReady），应用层被自然反压。这避免了在网络上对同一 IP 刷屏式广播请求。

**练习 2**：`CHECK_S` 里处理请求分支时，并没有重新分配发送缓冲，而是「原地改写」`r.tData`。这样做的依据是什么？

**答案**：ARP 请求和应答的帧结构几乎对称，只有「目的/源 MAC 对调、操作码改应答、发送方字段换成本机、目标字段换成对端」这几处差异。复用同一缓冲原地修改，省去了额外存储与拼接逻辑，是最省资源的做法。

---

### 4.3 ICMP 回显与 IGMPv2 组播

#### 4.3.1 概念说明

**ICMP（Internet Control Message Protocol，协议号 0x01）** 最广为人知的用途就是 `ping`：对端发一个 ICMP **Echo Request**（类型 `0x08`），本机回一个 **Echo Reply**（类型 `0x00`），负载原样返回。`IcmpEngine` 就在 FPGA 内部就地完成这个回显，不需要软件介入。

**IGMP（协议号 0x02）** 用于 IPv4 组播的「组成员管理」。本机想加入某个组播组时，向组地址发 **Membership Report**（类型 `0x16`）；组播路由器会周期性发 **Membership Query**（类型 `0x11`），成员必须在「最大响应时间」内回应 Report，否则被剔出组。`IgmpV2Engine` 负责在 FPGA 内自动完成这套握手。

#### 4.3.2 核心流程

**ICMP 回显**（4 状态机 `IDLE_S → RX_HDR_S → TX_HDR_S → MOVE_S`）：

```text
IDLE_S: 收到伪头部首拍, 交换源/目的 IP, 若目的IP=本机则 RX_HDR_S
RX_HDR_S: 判断是不是 Echo Request(类型=0x08), 算回包校验和, 发出回包首拍(伪头部)
TX_HDR_S: 把类型从 0x08 改成 0x00(Echo Reply), 写新校验和, 透传其余字段
MOVE_S : 原样搬运后续数据拍直到 tLast, EOFE 原样回显
```

**Internet 校验和的增量更新**：把类型字节从 `0x08` 改成 `0x00`，相当于校验和的「反码和」减小了 `0x0800`。要保持校验和仍合法，回包校验和应在原值基础上**加回 `0x0800`**；若原值 ≥ `0xF800`，加 `0x0800` 会产生进位溢出 16 位，需再多加 `0x0001`（回卷进位）。这就是 [IcmpEngine.vhd:141-147](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/IcmpEngine.vhd#L141-L147) 的来历：

\[ \text{newChecksum} = \text{oldChecksum} + 0x0800 + (\text{carry? } 0x0001 : 0x0000) \]

为省资源，`IcmpEngine` **不缓存整个数据包重算校验和**，而是直接基于入向校验和增量修正（见源码注释 149-155 行），并依赖对端计算机校验回包。

**IGMPv2** 用 `RX_IDLE_S/RX_MSG_S` 收查询/报告，用 `TX_IDLE_S/TX_MSG_S` 发报告；上电即发送 Report（`sendReport` 初值全 1），收到 Query 后在 `timer`（最大响应时间）内随机延迟后回应 Report，避免组内所有成员同时回复造成风暴。

#### 4.3.3 源码精读

**ICMP 交换 IP 并判本机** —— [IcmpEngine.vhd:113-130](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/IcmpEngine.vhd#L113-L130)：

```vhdl
v.tData(63 downto 0)   := ibIcmpMaster.tData(63 downto 0);       -- remote MAC + 保留
v.tData(95 downto 64)  := ibIcmpMaster.tData(127 downto 96);     -- 新源IP = 原目的IP(本机)
v.tData(127 downto 96) := ibIcmpMaster.tData(95 downto 64);      -- 新目的IP = 原源IP(对端)
if ibIcmpMaster.tData(127 downto 96) = localIp then              -- 仅当原目的IP=本机才回应
   v.state := RX_HDR_S;
end if;
```

回想 4.1 里伪头部的约定：`tData(95:64)` 是源 IP、`tData(127:96)` 是目的 IP。这里交换二者，使回包的源=本机、目的=对端；同时只对「发给自己」的包回应，其余丢弃。

**ICMP 增量校验和 + Echo Request 识别** —— [IcmpEngine.vhd:132-166](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/IcmpEngine.vhd#L132-L166)。入向 ICMP 头部里，类型字段在 `tData(39:32)`（`0x0008` 小端表示类型 `0x08`），校验和在 `tData(63:48)`：

```vhdl
if (ibIcmpMaster.tData(47 downto 32) = x"0008") then            -- Echo Request
   v.checksum := endian_swap(ibIcmpMaster.tData(63:48));        -- 取入向校验和
   if (v.checksum >= x"F800") then  v.checksum := v.checksum + x"0801";  -- 含回卷进位
   else                             v.checksum := v.checksum + x"0800";  -- 普通 +0x0800
   end if;
   v.obIcmpMaster.tValid := '1';  ...  v.state := TX_HDR_S;     -- 开始发回包
else  v.state := IDLE_S;                                        -- 非 echo 直接丢
end if;
```

**ICMP 写回包并改类型** —— [IcmpEngine.vhd:168-191](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/IcmpEngine.vhd#L168-L191)：

```vhdl
v.obIcmpMaster.tData(47 downto 32) := x"0000";                 -- 类型=0x00 (Echo Reply)
v.obIcmpMaster.tData(55 downto 48) := r.checksum(15 downto 8); -- 新校验和
v.obIcmpMaster.tData(63 downto 56) := r.checksum(7 downto 0);
v.obIcmpMaster.tData(127 downto 64):= ibIcmpMaster.tData(127:64); -- 其余 ICMP 头+数据原样
...
if ibIcmpMaster.tLast = '1' then  ssiSetUserEofe(..., eofe); v.state := IDLE_S;
else                              v.state := MOVE_S;           -- 还有数据拍, 继续 MOVE_S
end if;
```

`MOVE_S`（[IcmpEngine.vhd:193-209](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/IcmpEngine.vhd#L193-L209)）把后续数据拍原封不动搬运，末拍把入向 EOFE 原样回显。

**IGMPv2 成员查询处理** —— [IgmpV2Engine.vhd:143-170](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/IgmpV2Engine.vhd#L143-L170)。先校验 IGMP 校验和（`rxXsum`，第 114-118 行），再分两种消息类型：收到 `0x11` Query 就对每个配置过的组 `igmpIp(i)` 武装 `sendReport` 并设响应定时器；收到 `0x16` Report 且组地址匹配则清掉自己的 `sendReport`（别人已替本组回复，自己不必再发）。

**IGMPv2 上报 Membership Report** —— [IgmpV2Engine.vhd:205-264](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/IgmpV2Engine.vhd#L205-L264)。`timer` 减到 0 时，对每个 `sendReport=1` 且组 IP 非零的组，构造一个发往组地址的 IPv4 帧：目的 MAC 用 IPv4 组播前缀 `01:00:5E:..`（[IgmpV2Engine.vhd:217](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/IgmpV2Engine.vhd#L217)），第二拍填类型 `0x16`、校验和、组地址，`tKeep = x"FFF"`（96 位 = 12 字节有效，第 255 行）。注意 IGMP 默认不开启，需在 `IpV4Engine` 顶层把 `IGMP_G` 设为 `true` 才会例化（[IpV4Engine.vhd:223-243](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/IpV4Engine/rtl/IpV4Engine.vhd#L223-L243)）。

#### 4.3.4 代码实践

**实践目标**：运行 ICMP 引擎的 cocotb 测试，验证「只有发给自己、且类型为 Echo Request 的包才会被回显」。

**操作步骤**：

1. 运行（`待本地验证`）：

   ```bash
   ./.venv/bin/python -m pytest -q tests/ethernet/IpV4Engine/test_IcmpEngine.py
   ```

2. 打开 [test_IcmpEngine.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/ethernet/IpV4Engine/test_IcmpEngine.py)，阅读其方法学头列出的四类激励：有效多拍 echo、单拍截断请求、非本机请求、非 echo 包。

**需要观察的现象**：只有「目的 IP = localIp 且类型 = 0x08」的请求会产生回包；回包的源/目的 IP 已交换、类型变为 `0x00`、校验和已被增量修正；末拍 EOFE 被保留。

**预期结果**：测试断言「仅本机 echo 请求有回包、回包是正确交换的 echo reply、终态 EOFE 保留、被拒流量后能干净恢复」全部通过。

#### 4.3.5 小练习与答案

**练习 1**：如果把回包校验和改成「把整个回包重新算一遍 Internet 校验和」，相比现在的增量法，代价是什么？

**答案**：需要把整个 echo 数据缓存起来再求和，至少要一块能容纳最大包长的 RAM 与一次遍历；而增量法只改了一个类型字节，用「原校验和 + 0x0800（含进位回卷）」一行组合逻辑即可，几乎不耗资源。代价是依赖对端做校验（通常都做）。

**练习 2**：`IgmpV2Engine` 收到别人的 Membership Report 后，为什么要把自己的 `sendReport` 清掉？

**答案**：IGMP 用组播 Report 通告组成员关系，组内只要有一台成员回复过，路由器就知道该组还有成员。清掉自己的 `sendReport` 可以避免重复上报，这正是 IGMP「抑制」机制，可降低组播风暴风险。

---

## 5. 综合实践

**任务**：描述从主机 `ping` 本设备一次完整往返（ARP 解析 → ICMP 请求 → ICMP 应答），标注每个引擎分别处理了哪些帧、各帧的关键字段如何变化。这是把本讲三个模块串起来的源码阅读型综合实践。

**建议步骤**：

1. 设定参数：本机 `localMac = 08:00:56:00:00:01`、`localIp = 192.168.1.10`；主机 MAC/IP 自拟。主机尚未缓存本机 MAC，故先走 ARP。

2. **第一帧 ARP 请求（主机→广播，由 `ArpEngine` 处理）**
   - 路径：`EthMacCore` 收帧 → `IpV4EngineDeMux`（EtherType `0x0806`，目的 MAC 广播 → 选 `arpSel`）→ `ArpEngine.RX_S` → `CHECK_S`。
   - 关键判定：硬件/协议类型合法、操作码 `0x0001`、目标 IP = `localIp` → 把请求原地改写为应答，转 `TX_S`。

3. **第二帧 ARP 应答（本机→主机单播，由 `ArpEngine` 处理）**
   - 路径：`ArpEngine.TX_S` → `AxiStreamMux`（合并回 MAC 发流）→ `EthMacCore` 发出。
   - 关键字段：目的 MAC = 主机 MAC、操作码 `0x0002`、发送方 MAC/IP = 本机、`tKeep = x"03FF"`。
   - 同时：若有客户端在排队等这个 IP，`SCAN_S` 会回填 MAC 并清计时器（本 ping 场景中 ICMP 不需要 ARP 客户端，因为它复用入向帧里的 remote MAC）。

4. **第三帧 ICMP Echo Request（主机→本机，由 DeMux→Rx→IcmpEngine 处理）**
   - 路径：`IpV4EngineDeMux`（EtherType `0x0800`、`x"45"`、目的 MAC = 本机 → 选 `ipv4Sel`）→ `IpV4EngineRx.IDLE_S`（记 remote MAC）→ `IPV4_HDR0_S`（协议号 `0x01` = `ICMP_C`，命中协议表，`tDest` 指向 ICMP 槽）→ 剥头为伪头部 → `IcmpEngine.IDLE_S`（交换 IP、确认目的 IP = 本机）→ `RX_HDR_S`（类型 `0x08` = Echo Request，算增量校验和）。

5. **第四帧 ICMP Echo Reply（本机→主机，由 IcmpEngine→Tx 处理）**
   - 路径：`IcmpEngine.TX_HDR_S`（类型改 `0x00`、写新校验和、透传负载）→ `MOVE_S`（搬运剩余数据拍）→ `IpV4EngineTx.IDLE_S`（用伪头部里的 remote MAC 填以太网头、`IPV4_HDR0_S` 加 IP 头，协议号由 `tDest` 反查得 `0x01`）→ `AxiStreamMux` → `EthMacCore` 发出。
   - 关键字段：源/目的 IP 已交换、ICMP 类型 `0x00`、负载与 Request 一致、EOFE 透传。

6. **验证**：对照 [ipv4_test_utils.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/ethernet/IpV4Engine/ipv4_test_utils.py) 中的 `build_icmp_echo_packet` / `build_icmp_echo_reply_packet` / `build_arp_frame`，确认你画出的字段与参考实现一致。若本地已配好 cocotb 环境，可直接运行 `test_IpV4Engine.py` 与 `test_IcmpEngine.py` 作为端到端佐证（`待本地验证`）。

## 6. 本讲小结

- `IpV4Engine` 是 MAC 之上的纯结构顶层：`DeMux` 按 EtherType 分 ARP/IPv4，`Rx` 按协议号分发到子引擎，`Tx` 给数据加 IP 头，`Mux` 合并回 MAC 发流；协议表用编译期函数把 ICMP/IGMP 追加到用户协议尾部。
- `IpV4EngineRx`/`Tx` 之间靠一份「非标准 IPv4 伪头部」（remote MAC + 源/目的 IP + 协议号 + 长度）传递信息，使子引擎无需重复解析/构造 IP 头；Tx 用 `tDest` 反查协议表得到协议号，与 Rx 对称。
- `ArpEngine` 是请求/应答/缓存三合一：5 状态机，入向帧优先于客户端查询，目标 IP 是本机时原地改写应答，客户端缓存带 1 秒计时器防重发，本机 IP 直接短路回 MAC。
- `IcmpEngine` 就地回 `ping`：交换源/目的 IP、Echo Request(`0x08`)→Reply(`0x00`)、用 Internet 校验和增量法（`+0x0800`，含进位回卷）修正校验和，不缓存整包。
- `IgmpV2Engine`（可选）完成 IGMPv2 组成员管理：上电与收到 Query 时上报 Membership Report，收到同组 Report 时自我抑制，并用随机响应延时避免风暴。
- 这些引擎全部沿用 u1-l5 的双进程骨架与 `EMAC_AXIS_CONFIG_C`（128 位、`TUSER_FIRST_LAST_C`）的 SSI 帧边界语义。

## 7. 下一步学习建议

- **下一讲 u6-l3**：进入 `UdpEngine`，看上层协议引擎如何消费本讲定义的伪头部、并经 `ArpEngine` 的客户端端口做 IP→MAC 查询；同时了解 `RawEthFramer` 的二层成帧。
- **横向对照**：回头读 `IpV4EngineTx` 的本地回环（`tDest=0x01`）与 u4-l3 的 `AxiStreamDeMux`，体会「同一 DeMux 既做协议路由又做本地回环」的复用。
- **延伸阅读**：若关心 IPv4 校验和的硬件实现，可结合 u2-l4 的 `CrcPkg`/`Crc32Parallel` 与 `EthMacRxCsum`（u6-l1），对比 CRC32 与 Internet 反码校验和的差异。
