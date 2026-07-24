# 以太网 MAC 核心（EthMacCore）

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `EthMacTop` 如何把 TX/RX FIFO、TX/RX 通路、PAUSE、校验和、过滤这些子模块串成一条完整的以太网 MAC 数据通路。
- 看懂 IEEE 802.3x PAUSE 帧在 SURF 里的**两个方向**：本端收不到时如何发 PAUSE、收到对端 PAUSE 时如何停发。
- 理解 RX 路径如何按**目的 MAC** 过滤、如何做 IP/TCP/UDP **校验和硬件卸载**，并把校验结果编码进 TUSER。
- 解释以太网帧在 128 位 AXI-Stream 上的字节排布，以及 `EthMacPkg` 里 EtherType 常量为何看起来是「反」的。
- 知道 `EthMacCore` 内部**不做** ARP/IPv4 引擎级分流——它只交付完整帧，真正的协议分流在更上层（u6-l2、u6-l3）。

## 2. 前置知识

在进入 MAC 之前，先建立三点直觉。本讲依赖 u4-l1 的 AXI-Stream 记录化总线。

**第一，以太网帧是一串字节，MAC 把它搬进搬出。** 一个 Ethernet II 帧的字节布局是固定的：

| 偏移(字节) | 字段 | 长度 |
|---|---|---|
| 0–5 | 目的 MAC | 6 |
| 6–11 | 源 MAC | 6 |
| 12–13 | EtherType | 2 |
| 14+ | 载荷（IP/ARP/…） | 可变 |
| 末尾 4 | FCS（帧校验，CRC32） | 4 |

MAC 的职责是：发送侧加上前导码/帧定界/填充/FCS 送到 PHY，接收侧做反向工作并校验 FCS。

**第二，SURF 把这串字节铺在 128 位 AXI-Stream 上。** `EMAC_AXIS_CONFIG_C` 规定每拍 16 字节（`TDATA_BYTES_C => 16`），且**帧的第 0 字节落在 `tData` 的最低字节通道**（小端字节通道排列，见 [ethmac_test_utils.py:185-191](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/ethernet/EthMacCore/ethmac_test_utils.py#L185-L191) 的注释与 `pack_bytes`）。于是：

- 目的 MAC 在 `tData(47 downto 0)`
- 源 MAC 在 `tData(95 downto 48)`
- EtherType 在 `tData(111 downto 96)`（字节 12 在 bits 103:96，字节 13 在 bits 111:104）

记住这个映射，后面所有「按字段比较」的代码都能一眼读懂。

**第三，三种 PHY 接口对应三种速率。** `EthMacTop` 同时声明了三套 PHY 端口，由 `PHY_TYPE_G` 选择其一：

- `"GMII"`：8 位，1 GbE
- `"XGMII"`：64 位，10 GbE
- `"XLGMII"`：128 位，40 GbE

`EthMacTxExport`/`EthMacRxImport` 负责把内部统一的 128 位 AXI-Stream 适配成对应位宽的 PHY 接口。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [ethernet/EthMacCore/rtl/EthMacPkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacPkg.vhd) | MAC 包：MAC 地址、EtherType/协议常量、AXI-Stream 配置、配置/状态记录、并行校验和过程 `getEthMacCsum` |
| [ethernet/EthMacCore/rtl/EthMacTop.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacTop.vhd) | 顶层：实例化 TX FIFO、TX、FlowCtrl、RX、RX FIFO 五大块并连线 |
| [ethernet/EthMacCore/rtl/EthMacTx.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacTx.vhd) | TX 包装器：Bypass→Csum→(RoCEv2)→Pause→Export 流水 |
| [ethernet/EthMacCore/rtl/EthMacRx.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacRx.vhd) | RX 包装器：Import→Pause→Csum→(RoCEv2)→Bypass→Filter 流水 |
| [ethernet/EthMacCore/rtl/EthMacRxPause.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacRxPause.vhd) | RX PAUSE 帧识别与剥离，输出 `rxPauseReq/rxPauseValue` |
| 辅助：[EthMacTxPause.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacTxPause.vhd)、[EthMacRxFilter.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacRxFilter.vhd)、[EthMacRxBypass.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacRxBypass.vhd)、[EthMacRxCsum.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacRxCsum.vhd)、[EthMacFlowCtrl.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacFlowCtrl.vhd) | TX PAUSE 注入、RX MAC 过滤、RX EtherType 分流、RX 校验和、流控合并 |

> 说明：本讲聚焦 `EthMacCore` 内部。`EthMacTxExport`/`EthMacRxImport` 负责与 GMII/XGMII/XLGMII PHY 的位宽适配与 FCS，本讲只点到为止；具体的厂商 PHY 封装（GTX7/GTH7/…）见 u6-l4。

## 4. 核心概念与源码讲解

### 4.1 MAC 收发通路

#### 4.1.1 概念说明

`EthMacTop` 是一个**纯结构（`architecture mapping`）顶层**——它自己几乎没有逻辑，只做「实例化 + 连线」。理解 MAC 的第一件事，就是把这条流水线在脑子里画出来。

MAC 对外有两组 AXI-Stream 接口：

- **Primary（主）接口**：承载正常的以太网帧（IPv4/ARP/…），跨 `primClk` 时钟域与上游/下游用户相连。
- **Bypass（旁路）接口**：可选，用来承载一种自定义 EtherType 的「带外」帧（由 `BYP_EN_G`/`BYP_ETH_TYPE_G` 配置），跨 `bypClk` 时钟域。

数据在 `ethClk`（MAC 核心时钟）域内穿行，FIFO 负责时钟域跨越。

#### 4.1.2 核心流程

**TX 方向（用户 → 线缆）：**

```
ibMacPrimMaster ─┐
                 ├─► [EthMacTxFifo: primClk/bypClk → ethClk] ─► sPrimMaster/sBypMaster
ibMacBypMaster  ─┘                                              │
                                                                ▼
[EthMacTx]  Bypass(合流) → Csum(插IP/TCP/UDP校验和) → (RoCEv2) → Pause(注入/门控PAUSE) → Export(→PHY) → gmii/xgmii/xlgmii
```

**RX 方向（线缆 → 用户）：**

```
gmii/xgmii/xlgmii ─► [EthMacRx]  Import(→AXIS,FCS) → Pause(识别&剥离) → Csum(校验) → (RoCEv2) → Bypass(按EtherType分流) → Filter(按MAC过滤)
                                                                                                                          │ mPrimMaster/mBypMaster
                                                                                                                          ▼
                                                                                                       [EthMacRxFifo: ethClk → primClk/bypClk]
                                                                                                                          │
                                                                                                                          ▼
                                                                                                          obMacPrimMaster / obMacBypMaster
```

**反压闭环：** RX 输出侧 FIFO 快满时，`U_RxFifo` 生成 `mPrimCtrl.pause`，经 `U_FlowCtrl` 合并成 `flowCtrl.pause`，送进 TX 的 Pause 模块当作 `clientPause`，触发本端发出一帧 PAUSE，让对端暂缓发送。这是一条从「RX FIFO 水位」到「TX 发 PAUSE」的闭环，详见 4.2。

#### 4.1.3 源码精读

顶层把五大块串起来。先看信号声明里几根关键的「内部总线」与 PAUSE/流控连线：

[ethernet/EthMacCore/rtl/EthMacTop.vhd:105-117](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacTop.vhd#L105-L117) —— `rxPauseReq/rxPauseValue` 把 RX 收到的对端 PAUSE 请求送到 TX；`flowCtrl` 把 RX FIFO 水位反压送到 TX；`ethStatus.rxPauseCnt/rxOverFlow` 是上报状态。

TX 模块接收已经跨域到 `ethClk` 的 `sPrimMaster/sBypMaster`，并把 PAUSE 相关信号接进来：

[ethernet/EthMacCore/rtl/EthMacTop.vhd:154-201](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacTop.vhd#L154-L201) —— `U_Tx` 实例化。注意 `clientPause => flowCtrl.pause`、`rxPauseReq/rxPauseValue` 两个方向都接到 TX 的 Pause 模块。

`U_FlowCtrl` 把 primary 与 bypass 两路反压合并（任一路 pause/overflow 即生效）：

[ethernet/EthMacCore/rtl/EthMacFlowCtrl.vhd:64-79](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacFlowCtrl.vhd#L64-L79) —— 先采样 `primCtrl`，若 `BYP_EN_G` 再「或」上 `bypCtrl`。

RX 模块把 PHY 接口转成 AXI-Stream，并输出 PAUSE 请求与状态：

[ethernet/EthMacCore/rtl/EthMacTop.vhd:224-268](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacTop.vhd#L224-L268) —— `U_Rx` 实例化。`mPrimCtrl`（来自 `U_RxFifo`）既驱动 RX Filter 的 `dropOnPause` 行为，也通过 FlowCtrl 反压 TX。

再看 `EthMacTx`/`EthMacRx` 各自的内部流水。TX 是五级串联，RoCEv2 用 `generate` 按需插入，否则直通：

[ethernet/EthMacCore/rtl/EthMacTx.vhd:141-161](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacTx.vhd#L141-L161) —— `ROCEV2_EN_G=false` 时 `ibPauseMaster <= obCsumMaster`，把校验和输出直接接到 Pause 输入，零开销绕过 RoCEv2。

RX 同样是六级流水，结构与 TX 镜像：

[ethernet/EthMacCore/rtl/EthMacRx.vhd:82-216](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacRx.vhd#L82-L216) —— `Import → Pause → Csum → (RoCEv2) → Bypass → Filter`。注意 `U_Filter` 在最后，使用 `ethConfig.macAddress/filtEnable/dropOnPause`。

最后是包里两条 AXI-Stream 配置常量。对外配置带 8 位 TDEST（用作虚拟通道/旁路标记），对内配置把 TDEST 关掉：

[ethernet/EthMacCore/rtl/EthMacPkg.vhd:54-73](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacPkg.vhd#L54-L73) —— `EMAC_AXIS_CONFIG_C`（外部：16 字节、8 位 TDEST、4 位 TUSER、`TUSER_FIRST_LAST_C`）与 `INT_EMAC_AXIS_CONFIG_C`（内部：相同，但 `TDEST_BITS_C => 0`，注释「TDEST not used internally of EthMacTop.vhd」）。

> TUSER 的 4 个比特在首拍/末拍各司其职，定义在 [EthMacPkg.vhd:43-51](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacPkg.vhd#L43-L51)：首拍是 `FRAG`（分片）/`SOF`；末拍是 `EOFE`/`IPERR`/`TCPERR`/`UDPERR`。校验和模块正是把结果写进末拍这几位，见 4.3。

#### 4.1.4 代码实践

**目标：** 用纯源码阅读，在脑中重建 `EthMacTop` 的拓扑。

**步骤：**

1. 打开 `EthMacTop.vhd`，从 `U_TxFifo` 开始，沿 `sPrimMaster` 信号追到 `U_Tx`，再追到 `xgmiiTxd` 等引脚。
2. 反向从 `xgmiiRxd` 追到 `U_Rx`，沿 `mPrimMaster` 追到 `U_RxFifo`，再到 `obMacPrimMaster`。
3. 单独追三根「控制信号」：`rxPauseReq`、`flowCtrl`、`mPrimCtrl`，看它们各自连接了哪两个模块。

**需要观察的现象：** `EthMacTop` 的 `architecture` 里除了信号赋值和实例化，没有任何 `process`——它是纯结构。

**预期结果：** 你应当得到与 4.1.2 完全一致的两条流水线图，并能指出「反压闭环」由 `U_RxFifo → mPrimCtrl → U_FlowCtrl → flowCtrl.pause → U_Tx(clientPause)` 这条路径构成。

#### 4.1.5 小练习与答案

**练习 1：** `EthMacTop` 同时声明了 GMII/XGMII/XLGMII 三组 PHY 端口，实际只用一组。是哪一个泛型决定哪组生效？

**答案：** `PHY_TYPE_G`（[EthMacTop.vhd:33](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacTop.vhd#L33)），取值 `"GMII"`/`"XGMII"`/`"XLGMII"`，向下传给 `EthMacTxExport`/`EthMacRxImport` 做内部适配。

**练习 2：** 为什么对外配置有 TDEST、对内配置没有？

**答案：** TDEST 在 `EthMacTop` 边界用于区分虚拟通道/旁路流量，但 MAC 内部数据通路不需要它，故 `INT_EMAC_AXIS_CONFIG_C` 把 `TDEST_BITS_C` 设为 0 以省资源（见包内注释）。

---

### 4.2 PAUSE 流控

#### 4.2.1 概念说明

IEEE 802.3x PAUSE 是以太网的链路层流控：一端处理不过来时，发一个特殊的 PAUSE 帧告诉对端「先停 N 个时间单位」。PAUSE 帧有固定模样：

- 目的 MAC = `01:80:C2:00:00:01`（组播慢协议地址）
- EtherType = `0x8808`（MAC Control）
- OpCode = `0x0001`（PAUSE）
- 后跟 2 字节 pause time（单位是 512 比特时间，即一个「quanta」）

在 SURF 里，PAUSE 有**两个方向**，由两个模块分别处理，务必分清：

| 模块 | 触发 | 动作 |
|---|---|---|
| `EthMacRxPause` | 收到一个 PAUSE 帧 | 剥离该帧（不往用户送），输出 `rxPauseReq` 脉冲与 `rxPauseValue` |
| `EthMacTxPause` | (a) `clientPause`（本端 RX FIFO 满）；(b) `rxPauseReq`（对端要我停） | (a) 注入一帧 PAUSE 发给对端；(b) 门控本端 TX，暂停发送数据 |

注意 `rxPauseReq/rxPauseValue` 这对信号：它们是 **RX 模块的输出**（收到对端 PAUSE），却接到 **TX 模块的输入**（用来停自己的发送）。

#### 4.2.2 核心流程

**接收对端 PAUSE（`EthMacRxPause`）：** 一个 4 状态机 `IDLE_S → PAUSE_S → (DUMP_S) → IDLE_S` / `IDLE_S → PASS_S → IDLE_S`。在 `IDLE_S` 检查帧头是否匹配 PAUSE 帧特征（目的 MAC + EtherType + OpCode）；匹配则进 `PAUSE_S` 提取 pause value，不匹配则进 `PASS_S` 透传。匹配的帧被「吞掉」（`mAxisMaster.tValid` 保持 0），从而对用户不可见。

**发送 PAUSE 与门控（`EthMacTxPause`）：** 3 状态机 `IDLE_S → PAUSE_S → IDLE_S` / `IDLE_S → PASS_S → IDLE_S`。

- 当 `clientPause='1'`（本端 RX FIFO 满）且不在被对端 PAUSE 期间，进 `PAUSE_S`，用 4 拍构造一个 64 字节 PAUSE 帧发出。
- 当 `rxPauseReq='1'`（收到对端 PAUSE），把 `rxPauseValue` 装入 `locPauseCnt`，此后 `locPauseCnt≠0` 期间不让数据进 `PASS_S`，即停发数据。

**quanta 换算：** pause time 的单位是「512 比特时间」。不同速率下 512 比特对应的核心时钟数不同，由 `PAUSE_512BITS_G` 配置：

\[
\text{PAUSE\_512BITS\_G} = \frac{512}{\text{PHY 位宽}}
\]

- 10 GbE XGMII（64 位，156.25 MHz）：512/64 = **8**（默认值）
- 1 GbE GMII（8 位，125 MHz）：512/8 = **64**

`EthMacTxPause` 用一个预置计数器 `locPreCnt/remPreCnt`（位宽由 `bitSize(PAUSE_512BITS_G-1)` 决定）来按这个比例缩放 pause 计时。

#### 4.2.3 源码精读

RX PAUSE 帧识别。注意帧头是用「字节通道排列后」的常量比较——目的 MAC `01:80:C2:00:00:01` 在 `tData(47:0)` 里呈现为 `x"01_00_00_C2_80_01"`（最低字节通道 = 帧首字节 = 0x01）：

[ethernet/EthMacCore/rtl/EthMacRxPause.vhd:88-110](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacRxPause.vhd#L88-L110) —— `IDLE_S` 匹配 PAUSE 帧头。`tData(127:96)=x"01_00_08_88"` 即 EtherType `0x8808` + OpCode `0x0001`。匹配则进 `PAUSE_S`，否则透传。

提取 pause value 并丢弃该帧（除非该帧本身带 EOFE 错误）：

[ethernet/EthMacCore/rtl/EthMacRxPause.vhd:112-131](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacRxPause.vhd#L112-L131) —— `PAUSE_S` 把 2 字节 pause time 拼成 `pauseValue`，在帧末置 `pauseEn := not EOFE`（即只有无错的 PAUSE 帧才真正生效）。因为全程 `mAxisMaster.tValid` 被设 0，PAUSE 帧被静默丢弃。

TX 侧构造 PAUSE 帧，4 拍 = 64 字节（含 PHY 补的 FCS）：

[ethernet/EthMacCore/rtl/EthMacTxPause.vhd:159-192](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacTxPause.vhd#L159-L192) —— `txCount=0` 写目的 MAC + 源 MAC(0) + EtherType `x"08_88"` + OpCode `x"01_00"`；`txCount=1` 写 pause time（大端）+ 填充；`txCount=2/3` 写剩余填充并在末拍置 `tLast`，同时把 `pauseTime` 的一半存入 `remPauseCnt`（注释「retransmit if half of pauseTime time」——过半若 `clientPause` 仍在则重发一帧）。

quanta 缩放与门控逻辑：

[ethernet/EthMacCore/rtl/EthMacTxPause.vhd:113-130](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacTxPause.vhd#L113-L130) —— 预置计数器注释「8 clocks ~= 512 bit times of 10G」；`locPauseCnt` 在收到 `rxPauseReq` 时装载 `rxPauseValue`，倒计数期间数据不得进 `PASS_S`。

`IDLE_S` 的两个分支把「发 PAUSE」与「停发数据」分开：

[ethernet/EthMacCore/rtl/EthMacTxPause.vhd:135-144](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacTxPause.vhd#L135-L144) —— `clientPause` 且未被对端 PAUSE → 发 PAUSE；否则有数据且 `locPauseCnt=0` → 正常发送。

#### 4.2.4 代码实践

**目标：** 验证 PAUSE 帧的字节排布与 quanta 换算。

**步骤：**

1. 在 `EthMacRxPause.vhd:94-95` 找到目的 MAC 常量 `x"01_00_00_C2_80_01"`，按「最低字节通道=帧首字节」还原成线序 MAC 地址。
2. 对照 [ethmac_test_utils.py:46-48](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/ethernet/EthMacCore/ethmac_test_utils.py#L46-L48) 的 `MAC_CONTROL_ETHERTYPE=0x8808`、`MAC_CONTROL_PAUSE_OPCODE=b"\x00\x01"`、`MAC_CONTROL_PAUSE_DST=0x0180C2000001`，核对一致。
3. 计算 1 GbE GMII 下 `PAUSE_512BITS_G` 应取何值。

**需要观察的现象：** RX 常量里的 MAC 看似「反序」，实则是字节通道排列的结果。

**预期结果：** 还原得到 `01:80:C2:00:00:01`，与 test_utils 的 `MAC_CONTROL_PAUSE_DST` 一致；1 GbE 下 `PAUSE_512BITS_G = 64`。（仿真验证待本地运行。）

#### 4.2.5 小练习与答案

**练习 1：** `rxPauseReq` 是哪个模块的输出、又驱动哪个模块？

**答案：** 它是 `EthMacRxPause`（RX 内）的输出，表示「收到了对端的 PAUSE」；它驱动 `EthMacTxPause`（TX 内）的 `locPauseCnt`，使本端暂停发送数据。

**练习 2：** 为什么需要 `clientPause`？它从哪里来？

**答案：** `clientPause = flowCtrl.pause`，由 `EthMacFlowCtrl` 合并 RX 输出 FIFO 的反压而来（[EthMacTop.vhd:192](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacTop.vhd#L192)）。当本端 RX FIFO 快满时，用它触发 TX 主动发 PAUSE，请对端暂缓。

---

### 4.3 过滤与校验和

#### 4.3.1 概念说明

RX 路径的最后两站是 **Bypass 分流** 和 **Filter 过滤**，再加上夹在中间的 **Checksum 校验和卸载**。三者都用「比较 `tData` 某段字段」实现，理解了 4.1 的字节映射，这里就很简单。

**Filter（MAC 过滤）：** 默认 `FILT_EN_G=false` 时直通；使能后只放行三类帧：

1. 目的 MAC = 本机 `macAddress`（单播给本机）
2. 目的 MAC 首比特 = 1（组播/多播）
3. 目的 MAC = `FFFFFFFFFFFF`（广播）

其余帧整帧丢弃。另外，当 `dropOnPause=1` 且反压生效（`mAxisCtrl.pause=1`）时也丢帧，避免下游出错。

**Bypass 分流：** 仅当 `BYP_EN_G=true` 时启用。在 `IDLE_S` 检查 EtherType（`tData(111:96)`）是否等于 `BYP_ETH_TYPE_G`：相等则整帧走向 `mBypMaster`（旁路），否则走向 `mPrimMaster`（主路）。

> **关键：** `EthMacCore` 内部**没有** ARP/IPv4 引擎级的分流。Bypass 只是把一种可配置的自定义 EtherType 摘到旁路通道；把 IPv4 帧交给 IPv4 引擎、把 ARP 帧交给 ARP 引擎，是更上层（u6-l2 `IpV4Engine`/`ArpEngine`）的职责。MAC 只负责交付完整的以太网帧。

**Checksum 卸载：** `EthMacRxCsum` 在 RX 流上**边走边算** IP/TCP/UDP 校验和，把「是否正确」写进末拍 TUSER 的 `IPERR/TCPERR/UDPERR` 位（出错时同时拉 `EOFE`），这样上层无需软件重算。校验和用的是互联网校验和：把数据按 16 位字做**反码和**（one's complement sum），最后取反。

#### 4.3.2 核心流程

`EthMacRxCsum` 用一个流水线状态机逐拍解析帧头：

```
IDLE_S  ──(EtherType==0x0800?)──►  置 ipv4Det，缓存 IPv4 头前两字节
   │
   ▼
IPV4_HDR0_S  ──►  缓存 IPv4 头其余字段；据 Protocol 字段置 udpDet/tcpDet；检测分片
   │
   ▼
IPV4_HDR1_S  ──►  锁存入口 UDP 校验和与长度；进入 MOVE_S
   │
   ▼
MOVE_S  ──►  逐拍喂给 getEthMacCsum 累加；到 EOF 或超长(>MAX_FRAME_SIZE_C) 结束
```

校验和计算被流水化（`EMAC_CSUM_PIPELINE_C = 3`），结果在帧末拍回写 TUSER：

- IPv4 头校验失败 → 置 `EMAC_IPERR_BIT_C` 与 `EMAC_EOFE_BIT_C`
- UDP 校验失败（且非分片）→ 置 `EMAC_UDPERR_BIT_C` 与 `EMAC_EOFE_BIT_C`
- TCP 校验失败（且非分片）→ 置 `EMAC_TCPERR_BIT_C` 与 `EMAC_EOFE_BIT_C`

UDP 有个特例：UDP 允许 `0x0000` 表示「不算校验和」，故入口为 0 时直接判 valid；而计算结果若恰为 `0x0000` 则需替换成 `0xFFFF`（见包内注释与 RFC 768/793）。

#### 4.3.3 源码精读

**EtherType 与协议常量。** 这是本讲实践任务的核心。注意常量值与人类读到的 EtherType 是「反」的——因为字节通道是小端排列：

[ethernet/EthMacCore/rtl/EthMacPkg.vhd:29-37](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacPkg.vhd#L29-L37) —— `IPV4_TYPE_C := x"0008"`（注释 `EtherType = IPV4 = 0x0800`）、`ARP_TYPE_C := x"0608"`（`0x0806`）。`UDP_C/TCP_C/ICMP_C/IGMP_C` 是单字节 IPv4 Protocol 字段，无需交换。

> 推导：IPv4 的 EtherType 是字节 12=0x08、字节 13=0x00。字节 12 在 `tData(103:96)`、字节 13 在 `tData(111:104)`，于是 `tData(111:96)=0x0008`。这正是代码里比较 `tData(111 downto 96)` 的依据。

**Bypass 按 EtherType 分流：**

[ethernet/EthMacCore/rtl/EthMacRxBypass.vhd:85-106](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacRxBypass.vhd#L85-L106) —— `if sAxisMaster.tData(111 downto 96) = BYP_ETH_TYPE_G then` 走 `mBypMaster`，否则走 `mPrimMaster`。`BYP_EN_G=false` 时 `mPrimMaster <= sAxisMaster` 直通（[L153-156](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacRxBypass.vhd#L153-L156)）。

**Filter 按 MAC 过滤：**

[ethernet/EthMacCore/rtl/EthMacRxFilter.vhd:85-117](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacRxFilter.vhd#L85-L117) —— `filtEnable=0`（软件关闭）或目的 MAC 命中本机/组播(`tData(0)=1`)/广播(`FFFFFFFFFFFF`)则放行，否则整帧丢弃；`mAxisCtrl.pause` 且 `dropOnPause` 时也丢。

**Checksum 检测 IPv4 与 L4 协议：**

[ethernet/EthMacCore/rtl/EthMacRxCsum.vhd:199-205](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacRxCsum.vhd#L199-L205) —— `IDLE_S` 比较 `tData(111 downto 96) = IPV4_TYPE_C`，命中则置 `ipv4Det`。

[ethernet/EthMacCore/rtl/EthMacRxCsum.vhd:249-261](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacRxCsum.vhd#L249-L261) —— `ipv4Hdr(9)`（IPv4 Protocol 字段）等于 `UDP_C`/`TCP_C` 时置 `udpDet`/`tcpDet`；并据 Flags/Fragment Offset 检测分片（分片帧不校验 L4）。

**校验和结果回写 TUSER：**

[ethernet/EthMacCore/rtl/EthMacRxCsum.vhd:154-181](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacRxCsum.vhd#L154-L181) —— 帧末拍把 `not(valid)` 写进对应 TUSER 错误位与 EOFE 位。

**反码和的核心：** 实际累加在包过程 `getEthMacCsum` 里，用多级加法树把 16 位字折叠，最后取反：

[ethernet/EthMacCore/rtl/EthMacPkg.vhd:281-293](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacPkg.vhd#L281-L293) —— `ipCsum := not(r(0).sum5)`、`csum := not(r(1).sum5)`（反码取反），并处理 UDP `0x0000`→`0xFFFF` 特例。

#### 4.3.4 代码实践

**目标（本讲指定实践任务）：** 在 `EthMacPkg` 中找出 EtherType（ARP/IPv4）与协议 ID 常量，并说明 RX 路径如何按 EtherType 分流。

**步骤：**

1. 打开 [EthMacPkg.vhd:29-37](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacPkg.vhd#L29-L37)，列出 EtherType 常量（`ARP_TYPE_C=x"0608"`、`IPV4_TYPE_C=x"0008"`）与 IPv4 协议常量（`UDP_C=x"11"`、`TCP_C=x"06"`、`ICMP_C=x"01"`、`IGMP_C=x"02"`）。
2. 解释为何 EtherType 常量是「反」的（字节 12 在 `tData(103:96)`、字节 13 在 `tData(111:104)`）。
3. 在 RX 路径里搜索 `tData(111 downto 96)` 的所有比较点，回答「RX 如何按 EtherType 分流」。

**需要观察的现象 / 预期结论：**

`EthMacCore` 内部按 EtherType 的「分流」只发生在两处，且都不是 ARP↔IPv4 引擎级路由：

- **`EthMacRxBypass`**（[L89](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacRxBypass.vhd#L89)）：把 EtherType == `BYP_ETH_TYPE_G` 的帧摘到旁路通道，其余走主通道。
- **`EthMacRxCsum`**（[L201](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacRxCsum.vhd#L201)）：识别 EtherType == IPv4 以决定是否做校验和卸载，并进一步用 IPv4 Protocol 字段区分 UDP/TCP。

真正的「ARP 帧给 ARP 引擎、IPv4 帧给 IPv4 引擎」分流在 MAC **之上**（u6-l2 的 `IpV4Engine`/`ArpEngine`），MAC 只是把整帧交付给上游。`EthMacPkg` 里定义 `ARP_TYPE_C`/`ICMP_C` 等常量，主要是供上层模块复用。

#### 4.3.5 小练习与答案

**练习 1：** 若要把一种自定义 EtherType `0xABCD` 的帧走旁路通道，该如何配置？比较发生在 `tData` 的哪一段？

**答案：** 置 `BYP_EN_G=true`、`BYP_ETH_TYPE_G` 设为字节通道排列后的值。由于 `0xABCD` 在线上是字节 12=0xAB、字节 13=0xCD，对应 `tData(111:96)=0xCDAB`，故 `BYP_ETH_TYPE_G := x"CDAB"`。比较发生在 [EthMacRxBypass.vhd:89](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacRxBypass.vhd#L89) 的 `tData(111 downto 96)`。

**练习 2：** 一个 IPv4/UDP 帧 UDP 校验和错误，RX 出口末拍 TUSER 会有哪些位置 1？

**答案：** `EMAC_UDPERR_BIT_C`（bit 3）与 `EMAC_EOFE_BIT_C`（bit 0）置 1（[EthMacRxCsum.vhd:166-167](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacRxCsum.vhd#L166-L167)）。若同时 IPv4 头校验和也错，则 `EMAC_IPERR_BIT_C`（bit 1）也会置 1。

**练习 3：** 为什么分片（fragmented）的 IPv4 帧不做 L4 校验和？

**答案：** 分片帧的 L4 头/载荷不完整，重算校验和无意义，故代码在 `fragDet=1` 时跳过 UDP/TCP 校验判断（[EthMacRxCsum.vhd:164,176](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacRxCsum.vhd#L164-L176) 的 `r.fragDet(...) = '0'` 条件）。

## 5. 综合实践

**任务：** 构造一帧 IPv4/UDP 以太网帧，让它走过 `EthMacRx` 全路径，预测出口 TUSER 与通道，再用 cocotb 测试验证。

**步骤：**

1. 用 [ethmac_test_utils.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/ethernet/EthMacCore/ethmac_test_utils.py) 的 `build_ipv4_udp_frame()` 构造一帧：
   - 目的 MAC 设为 `MAC_ADDR_INIT_C` 对应的 `08:00:56:00:00:00`（注意 [L27](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/EthMacCore/rtl/EthMacPkg.vhd#L27) 的初值是字节通道排列的 `x"000000560008"`，还原成线序即 `08:00:56:00:00:00`）。
   - EtherType = `0x0800`（IPv4），Protocol = `0x11`（UDP），IP/UDP 校验和正确。
2. 追踪这帧在 RX 内的旅程，逐站写出结论：
   - `Import`：PHY→AXIS，FCS 正常 → 通过。
   - `Pause`：非 PAUSE 帧 → 透传，`rxPauseReq=0`。
   - `Csum`：EtherType==IPv4、Protocol==UDP、无错 → 末拍 `IPERR/UDPERR/EOFE` 全 0。
   - `Bypass`（假设 `BYP_EN_G=false`）→ 走 `mPrimMaster`。
   - `Filter`（`FILT_EN_G=true`）→ 目的 MAC 命中本机 → 放行。
3. 跑现成回归核对预测：

   ```bash
   # 详见 u9-l1/u9-l2 的 cocotb 工具链
   make MODULES=$PWD import
   ./.venv/bin/python -m pytest -q tests/ethernet/EthMacCore/test_EthMacRxCsum.py
   ./.venv/bin/python -m pytest -q tests/ethernet/EthMacCore/test_EthMacRxFilter.py
   ```

4. 进阶：故意把 UDP 校验和改错（`udp_checksum_override=0xDEAD`），重跑 `test_EthMacRxCsum`，观察末拍 `EMAC_UDPERR_BIT_C` 与 `EMAC_EOFE_BIT_C` 是否如练习 2 所述置 1。

**预期结果：** 正确帧在 `obMacPrimMaster` 末拍 TUSER 为 0；UDP 校验和错的帧末拍 TUSER 的 bit3 与 bit0 置 1。命令的实际输出**待本地验证**（取决于本地是否已配置 GHDL+cocotb 环境）。

## 6. 本讲小结

- `EthMacTop` 是纯结构顶层，把 `TxFifo → Tx → FlowCtrl → Rx → RxFifo` 五块串成双向流水，内部统一走 128 位（16 字节）AXI-Stream。
- PAUSE 流控有两个方向：`EthMacRxPause` 识别并剥离对端 PAUSE 帧、输出 `rxPauseReq/Value`；`EthMacTxPause` 既据此停发数据，又据 `clientPause`（RX FIFO 反压）主动发 PAUSE；`PAUSE_512BITS_G` 按 PHY 位宽换算 quanta。
- 字节通道是小端排列（帧首字节在 `tData` 最低字节通道），所以 EtherType 常量呈「反序」（`IPV4_TYPE_C=x"0008"` 对应 `0x0800`）。
- RX 过滤只放行本机/组播/广播 MAC；校验和卸载把 IP/TCP/UDP 校验结果写进末拍 TUSER 的 `IPERR/TCPERR/UDPERR`，出错同时拉 `EOFE`。
- `EthMacCore` 内部仅做 Bypass EtherType 摘流与 IPv4 识别，**不**做 ARP/IPv4 引擎级路由——那是上层 `IpV4Engine`/`ArpEngine` 的职责。
- 反压闭环：`RxFifo → mPrimCtrl → FlowCtrl → clientPause → TxPause 发 PAUSE`。

## 7. 下一步学习建议

- **u6-l2 IPv4 引擎**：看 `IpV4Engine` 如何承接 MAC 交付的帧，按 IPv4 Protocol 字段分流到 ARP/ICMP/IGMP，真正完成「协议级」路由。
- **u6-l3 UDP 引擎与 RawEth**：看 `UdpEngine` 的端口路由与 `RawEthFramer` 的二层成帧，补全 TX 侧如何造帧送进 MAC。
- **u6-l4 速率核与 PHY 拆分**：看 `GigEthCore`/`TenGigEthCore` 如何把本讲的 `EthMacTop` 与 GTX7/GTH7/GTY 等家族 PHY 封装组合，并用 `ruckus.tcl` 的 `getFpgaArch` 选择。
- **源码延伸阅读**：`EthMacTxExport`/`EthMacRxImport`（位宽适配与 FCS）、`EthMacTxCsum`（TX 侧校验和插入，与本讲 RX 校验和对称）。
