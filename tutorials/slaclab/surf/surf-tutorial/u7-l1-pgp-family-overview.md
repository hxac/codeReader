# PGP 协议族总览（PGP2b / PGP3 / PGP4）

## 1. 本讲目标

本讲是单元七「高速链路协议」的第一讲，目标是让你在深入任何单个 PGP 实现之前，先建立一张**全局对比地图**。

学完后你应该能够：

- 说清 PGP（Parallel General Protocol，SLAC 的点对点高速链路协议）为什么用 **TDEST/虚拟通道（VC）** 来路由数据，以及这套思想在三代协议里如何保持一致。
- 对比三代协议的**定界方式**：PGP2b 用 8B/10B 的 K 字符定界，PGP3/PGP4 改用「扰码 + BTF（块类型字段）」定界，并理解为什么会这样演进。
- 准确说出 PGP4 相对 PGP3 **到底增加了什么 CRC**——这里有一个常见误解需要澄清：PGP3 本来就带 CRC-32，PGP4 真正新增的是 **每个 K-code 控制字的头部 CRC-8**，以及更细粒度的错误分类。
- 能在源码里找到三代协议各自的「身份证」（版本号、位宽、BTF/K 字符常量、流控位宽）。

> 本讲是**总览与对比**，侧重「读包（*Pkg.vhd）看清协议骨架」，不展开任何单个状态机的逐拍实现。状态机细节留给 u7-l2（RSSI）以及后续按需阅读。

## 2. 前置知识

阅读本讲前，请确认你已经理解以下内容（都来自前置讲义）：

- **SSI 侧带与帧边界（u5-l1）**：SOF/EOF/EOFE 编码在 TUSER，TDEST 作为虚拟通道（VC）。本讲会反复用到「TDEST = VC」这个语义。
- **线路码与 ECC（u5-l4）**：8B/10B 的 K 字符定界、直流平衡（RD）、dispErr/decErr 检错；以及 CRC 的基本概念。本讲里 PGP2b 的定界就是 8B/10B，PGP4 的头部 CRC-8 与 u5-l4 的 CRC 思路一致。
- **AxiStreamConfigType（u4-l1）**：一条流用 `ssiAxiStreamConfig(...)` 描述字节宽、TKEEP/TUSER 模式、TDEST 位数等。三代 PGP 各自定义了自己的 `*_AXIS_CONFIG_C`，你需要看得懂这些字段。
- **目录约定（u1-l3）与速率核 PHY 拆分（u6-l4）**：`core/` 放家族无关、可在 GHDL 仿真的协议逻辑；家族 PHY 目录（gtp7/gtx7/gth7/gthUltraScale+/gtyUltraScale+ 等）放厂商收发器胶水。PGP 三代都遵循这个拆分。

几个术语先统一：

- **VC（Virtual Channel，虚拟通道）**：一条物理 PGP 链路上逻辑独立的数据通道，由 TDEST 区分。可以把一条 PGP 链路想象成一捆「虚拟双绞线」，每根走一路 AXI-Stream。
- **BTF（Block Type Field，块类型字段）**：PGP3/PGP4 里用来标识「这一拍是数据还是某种控制字」的 8 位编码，作用类似于 8B/10B 里的 K 字符。
- **Cell（信元）**：PGP3/PGP4 把一帧数据切成若干固定大小的段来传输，每一段叫一个 cell，带自己的头部和 CRC。

## 3. 本讲源码地图

本讲聚焦三代 PGP 的**协议包**与**核心结构**，全部位于 `protocols/pgp/` 下：

| 文件 | 作用 |
|---|---|
| [protocols/pgp/pgp2b/core/rtl/Pgp2bPkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp2b/core/rtl/Pgp2bPkg.vhd) | PGP2b 协议包：8B/10B K 字符常量、2 字节流配置、4 路 VC 的收发记录 |
| [protocols/pgp/pgp3/core/rtl/Pgp3Pkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp3/core/rtl/Pgp3Pkg.vhd) | PGP3 协议包：BTF 常量、扰码抽头、8 字节流配置、linkInfo 字段定义 |
| [protocols/pgp/pgp4/core/rtl/Pgp4Pkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp4/core/rtl/Pgp4Pkg.vhd) | PGP4 协议包：在 PGP3 基础上加 K-code 头部 CRC-8、版本号扩到 8 位、细分 cell 错误 |
| [protocols/pgp/pgp3/core/rtl/Pgp3Core.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp3/core/rtl/Pgp3Core.vhd) | PGP3 核心结构：例化 Tx/Rx/AxiL 三块，展示 VC 数、Mux 模式等顶层泛型 |

辅助引用（用于佐证具体行为）：

- [protocols/pgp/pgp2b/core/rtl/Pgp2bTx.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp2b/core/rtl/Pgp2bTx.vhd)：PGP2b 的 `NUM_VC_EN_G` 上限为 4。
- [protocols/pgp/pgp3/core/rtl/Pgp3TxProtocol.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp3/core/rtl/Pgp3TxProtocol.vhd)：展示 PGP3 如何把帧头/帧尾/CRC 填进 BTF 字段。
- [protocols/pgp/pgp4/core/rtl/Pgp4TxProtocol.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp4/core/rtl/Pgp4TxProtocol.vhd)：展示 PGP4 在发送每个 K-code 时调用 `pgp4KCodeCrc` 填头部 CRC。
- [protocols/pgp/pgp4/core/rtl/Pgp4RxKCodeChecker.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp4/core/rtl/Pgp4RxKCodeChecker.vhd)：PGP4 接收侧实时校验头部 CRC-8 的模块。

> 注意：仓库里还有 `pgp2fc`（PGP2b 的 front-end / CXP 变体）和 `shared/`，它们不在本讲的「三代」对比范围内，留待后续。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **PGP 虚拟通道**——三代协议共享的路由思想。
2. **定界方式对比**——8B/10B K 字符（PGP2b）vs 扰码 + BTF（PGP3/PGP4）。
3. **PGP4 相对 PGP3 增加的 CRC 与错误上报**——澄清「PGP3 本来就有 CRC-32」这一事实。

### 4.1 PGP 虚拟通道：用 TDEST/VC 统一三代协议

#### 4.1.1 概念说明

PGP 是一条**点对点**的高速串行链路协议（跑在 GTP/GTX/GTH/GTY 收发器上）。它的核心设计哲学是：**一条物理链路，承载多条逻辑独立的 AXI-Stream 数据流**，这些逻辑流就是**虚拟通道（VC）**。

为什么需要 VC？想象一块 ATCA 板卡，FPGA 之间用一根光纤相连，但这根光纤上同时要传：

- 寄存器访问流量（走 SRP，见 u5-l3）；
- 事件/触发数据；
- 大块科学数据；
- 慢控制心跳。

如果为每种流量各拉一根光纤，成本和引脚都受不了。PGP 的做法是：在一根光纤上把带宽切成若干 VC，每个 VC 是一条独立的 AXI-Stream，靠 **TDEST** 来区分去哪条 VC。这与 u5-l1 讲的「TDEST 充当虚拟通道」完全一致——PGP 只是把这件事固化进了链路协议。

**统一思想**：尽管 PGP2b/PGP3/PGP4 的物理编码、定界方式、位宽都不一样，但「用 TDEST 选 VC、每个 VC 一条 AXI-Stream、VC 间可独立流控」这个上层模型三代完全相同。这就是为什么用户侧接口在三代里长得几乎一样：一组 `pgpTxMasters : AxiStreamMasterArray(NUM_VC-1 downto 0)` 输入、一组 `pgpRxMasters` 输出，外加每个 VC 一位的流控。

#### 4.1.2 核心流程

一条 PGP 链路的数据通路可以这样概括（伪代码）：

```text
发送侧（每方向）:
  for vc in 0 .. NUM_VC-1 loop
      if pgpTxMasters(vc).tValid = '1' and 该VC未被远端反压 then
          把这一拍数据打上 vc 编号, 送进协议成帧器
      end if
  end loop;
  成帧器按协议(PGP2b/3/4)把 (vc, data, tLast, ...) 编码成线路码, 经 GT 串行发出

接收侧:
  GT 解串 -> 解码 -> 按定界符还原出 (vc, data, tLast, ...)
  pgpRxMasters(vc).tValid <= '1';  -- 路由到对应 VC 的 AXI-Stream
  -- 同时把本地每个 VC 的反压(pause/overflow)编码进链路状态, 回传给发送方
```

关键点：

- **VC 数量随代际增长**：PGP2b 最多 4 个 VC，PGP3/PGP4 最多 16 个 VC。
- **流控位宽 = VC 数**：远端会把每个 VC 的 pause/overflow 状态打包回传，所以三代协议里 `remPause`/`remOverflow` 的位宽正好对应 VC 数（PGP2b 是 4 位，PGP3/PGP4 是 16 位）。
- **VC 路由既可「索引」也可「按 tDest 路由」**：PGP3 核心提供 `TX_MUX_MODE_G = "INDEXED"`（VC 号即数组下标）或 `"ROUTED"`（用 `std_match` 通配表按 tDest 选路）两种模式。

#### 4.1.3 源码精读

先看 PGP3 的核心结构，它最清晰地展示了「VC 化的用户接口」：

[Pgp3Core.vhd:26-93](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp3/core/rtl/Pgp3Core.vhd#L26-L93) 定义了 PGP3 核的实体。注意这两组端口，它们是「VC 思想」的直接体现：

- `pgpTxMasters : in AxiStreamMasterArray(NUM_VC_G-1 downto 0)`——用户送进来的，是**一个 VC 数组**，每个元素是一条完整的 AXI-Stream 主机记录。
- `pgpRxMasters : out AxiStreamMasterArray(NUM_VC_G-1 downto 0)`——解出来的数据按 VC 分发回一组 AXI-Stream。
- `pgpRxCtrl : in AxiStreamCtrlArray(NUM_VC_G-1 downto 0)`——每个 VC 一个 `AxiStreamCtrlType`（含 pause/overflow），用于本地反压。

泛型 `NUM_VC_G : integer range 1 to 16 := 4`（[Pgp3Core.vhd:30](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp3/core/rtl/Pgp3Core.vhd#L30)）说明 PGP3 最多 16 个 VC，默认 4 个。`TX_MUX_MODE_G`（[Pgp3Core.vhd:35](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp3/core/rtl/Pgp3Core.vhd#L35)）则给出索引/路由两种 VC 选择方式。

再对比三代的流控位宽（位宽 = VC 上限）：

- PGP2b：`remOverflow : slv(3 downto 0)` 与 `remPause : slv(3 downto 0)`（[Pgp2bPkg.vhd:80-81](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp2b/core/rtl/Pgp2bPkg.vhd#L80-L81)），4 位 → 4 个 VC。其 `NUM_VC_EN_G : integer range 1 to 4 := 4`（[Pgp2bTx.vhd:35](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp2b/core/rtl/Pgp2bTx.vhd#L35)）确认上限是 4。
- PGP3：`locOverflow : slv(15 downto 0)` 与 `locPause : slv(15 downto 0)`（[Pgp3Pkg.vhd:126-127](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp3/core/rtl/Pgp3Pkg.vhd#L126-L127)），16 位 → 16 个 VC。
- PGP4：同样是 `locPause : slv(15 downto 0)`、`locOverflow : slv(15 downto 0)`（[Pgp4Pkg.vhd:117-118](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp4/core/rtl/Pgp4Pkg.vhd#L117-L118)），16 位 → 16 个 VC。

最后看 VC 编号如何在协议帧里被携带。PGP3 的帧头（SOFC，start-of-frame cell）里专门有一个 4 位的 VC 字段：

```vhdl
subtype PGP3_SOFC_VC_FIELD_C  is natural range 43 downto 40;  -- 虚拟通道号
subtype PGP3_SOFC_SEQ_FIELD_C is natural range 55 downto 44;  -- 帧序号
```

见 [Pgp3Pkg.vhd:74-75](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp3/core/rtl/Pgp3Pkg.vhd#L74-L75)。4 位 VC 字段正好编码 0~15 号 VC。PGP4 保留了同样的字段（[Pgp4Pkg.vhd:70](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp4/core/rtl/Pgp4Pkg.vhd#L70)）。

而发送侧如何把 AXI-Stream 的 tDest 写进这个 VC 字段，见 [Pgp3TxProtocol.vhd:217](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp3/core/rtl/Pgp3TxProtocol.vhd#L217)：

```vhdl
v.protTxData(PGP3_SOFC_VC_FIELD_C) := resize(pgpTxMaster.tData(PACKETIZER2_HDR_TDEST_FIELD_C), 4);
```

即：成帧器从 packetizer 头里取出 TDEST，写进 PGP 帧头的 VC 字段。这正是「TDEST = VC」在硅里的落点。

#### 4.1.4 代码实践

**实践目标**：通过读源码，确认三代 PGP 的「VC 数上限」和「流控位宽」严格对应。

**操作步骤（源码阅读型）**：

1. 打开 `protocols/pgp/pgp2b/core/rtl/Pgp2bTx.vhd`，找到 `NUM_VC_EN_G` 的取值范围，记下上限。
2. 打开 `Pgp2bPkg.vhd`，找到 `Pgp2bRxOutType` 里的 `remOverflow` / `remPause`，看它们的位宽。
3. 打开 `Pgp3Pkg.vhd` 与 `Pgp4Pkg.vhd`，找到各自的 `locOverflow` / `locPause` 位宽，以及 `NUM_VC_G` 的取值范围（在各自的 Core 里）。
4. 把结果填进一张三列表格。

**需要观察的现象**：流控位宽应该恰好等于 VC 数上限——PGP2b 是 4，PGP3/PGP4 是 16。

**预期结果**：

| 协议 | VC 上限 | 流控位宽 |
|---|---|---|
| PGP2b | 4 | 4 位 |
| PGP3 | 16 | 16 位 |
| PGP4 | 16 | 16 位 |

如果运行环境里没有仿真器，这是纯阅读任务，不需要「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 PGP2b 的流控位宽是 4，而 PGP3/PGP4 是 16？

**参考答案**：因为流控是**每 VC 一位**（pause/overflow 各一位），位宽必须等于 VC 数上限。PGP2b 最多支持 4 个 VC（`NUM_VC_EN_G range 1 to 4`），所以是 4 位；PGP3/PGP4 最多 16 个 VC，所以是 16 位。

**练习 2**：在 PGP3 的发送侧，用户的 AXI-Stream 是怎么变成「带 VC 号」的协议帧的？

**参考答案**：成帧器从 packetizer 头里取出 TDEST 字段（`PACKETIZER2_HDR_TDEST_FIELD_C`），`resize` 后写进 SOFC 帧头的 `PGP3_SOFC_VC_FIELD_C`（4 位）。接收侧解出该字段后，把数据路由到对应下标的 `pgpRxMasters(vc)`。

---

### 4.2 定界方式对比：8B/10B K 字符 vs 扰码 + BTF

#### 4.2.1 概念说明

任何串行链路都要解决一个根本问题：**接收方怎么知道一拍数据从哪里开始、是数据还是控制字、一帧的边界在哪里？** 这叫**定界（framing / delineation）**。

PGP 三代用了两种截然不同的定界方式：

- **PGP2b：8B/10B + K 字符定界。** 沿用经典光纤通道/千兆以太网的做法。8B/10B 把每个字节编成 10 位，其中一部分码字是「控制字」（K 字符），其余是「数据字」（D 字符）。K 字符在数据流中不会出现（8B/10B 的编码保证），所以收到 K 字符就一定是控制信息。PGP2b 用不同的 K 字符标记帧头（SOF）、帧尾（EOF）、错误帧尾（EOFE）、信元起止（SOC/EOC）、空闲（COMMA/K28.5）等。8B/10B 还附带直流平衡（RD）和检错（dispErr/decErr）能力。

- **PGP3 / PGP4：扰码 + BTF（块类型字段）定界。** 这是更现代的 64b/66b 风格编码。每 64 位数据附带一个 2 位的「头」（header），头 = `01` 表示数据字（D_HEADER），头 = `10` 表示控制字（K_HEADER）。控制字里的最高字节是 **BTF（Block Type Field）**，用不同的 8 位码（IDLE/SOF/EOF/...）标识这一拍控制字的类型。为了防止数据中出现长串 0/1 影响时钟恢复和 DC 平衡，整条数据流还要先经过**扰码器（scrambler）**。

**为什么 PGP3/4 要放弃 8B/10B？** 主要是效率。8B/10B 有 25% 的开销（每 8 位数据传 10 位），在 10Gbps 以上的速率下太浪费。64b/66b 风格编码开销只有 ~3%，配合扰码器就能同时满足 DC 平衡和时钟恢复。所以 PGP3/4 是为更高速率设计的。

> 注意 PHY 的差异：PGP2b 的 PHY 接口是「每通道 16 位数据 + dataK + dispErr + decErr」，直接对接 GT 内置的 8B/10B 编解码器；PGP3/4 的 PHY 接口是「64 位数据 + 2 位 header」，GT 工作在 raw/scrambled 模式，编解码在 PGP 逻辑里做（还需要一个 gearbox 对齐器 `Pgp3RxGearboxAligner`）。

#### 4.2.2 核心流程

PGP2b 的定界（概念）：

```text
线路: ... K_COM(K28.5) K_COM K_SOF(K23.7) D D D ... K_EOF(K29.7) K_COM ...
接收: 看到 K_COM 对齐 -> 看到 K_SOF 知道帧开始 -> 数据直到 K_EOF 帧结束
       若收到 dispErr/decErr -> 这拍的 8B/10B 解码出错
```

PGP3/PGP4 的定界（概念）：

```text
线路(扰码后): ... [K_HEADER=10][BTF=IDLE] [K_HEADER=10][BTF=SOF] [D_HEADER=01][data] ... [K_HEADER=10][BTF=EOF] ...
接收: 先解扰 -> 用 2 位 header 区分数据/控制 -> 控制字里读 BTF 决定语义
       gearbox 对齐器靠搜寻合法 header 模式来锁定字边界
```

PGP3/PGP4 用同一套扰码多项式抽头（都是 39、58，见下文源码），同一套 BTF 码值（IDLE=0x99、SOF=0xAA、EOF=0x55、SOC=0xCC、EOC=0x33、SKP=0x66）。两代在「定界」这一层几乎完全相同。

#### 4.2.3 源码精读

先看 PGP2b 的 8B/10B K 字符常量。在 [Pgp2bPkg.vhd:34-45](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp2b/core/rtl/Pgp2bPkg.vhd#L34-L45)：

```vhdl
constant K_COM_C  : slv(7 downto 0) := "10111100";  -- K28.5, 0xBC  (逗号/对齐)
constant K_SKP_C  : slv(7 downto 0) := "00011100";  -- K28.0, 0x1C  (Skip, 时钟补偿)
constant K_SOC_C  : slv(7 downto 0) := "11111011";  -- K27.7, 0xFB  (信元起始)
constant K_SOF_C  : slv(7 downto 0) := "11110111";  -- K23.7, 0xF7  (帧起始)
constant K_EOF_C  : slv(7 downto 0) := "11111101";  -- K29.7, 0xFD  (帧尾)
constant K_EOFE_C : slv(7 downto 0) := "11111110";  -- K30.7, 0xFE  (错误帧尾)
constant K_EOC_C  : slv(7 downto 0) := "01011100";  -- K28.2, 0x5C  (信元尾)
```

每个常量后面注释了它对应的 8B/10B 命名（如 K28.5）和十六进制值（如 0xBC）。注释里清晰标出每个 K 字符的语义角色（对齐、帧起止、信元起止、错误帧尾）。

再看 PGP2b 的 PHY 接口——典型的 8B/10B 接口形态，见 [Pgp2bPkg.vhd:167-172](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp2b/core/rtl/Pgp2bPkg.vhd#L167-L172)：

```vhdl
type Pgp2bRxPhyLaneInType is record
   data    : slv(15 downto 0);  -- PHY receive data       (2 字节/拍)
   dataK   : slv(1 downto 0);   -- PHY receive data is K character
   dispErr : slv(1 downto 0);   -- PHY receive data has disparity error
   decErr  : slv(1 downto 0);   -- PHY receive data not in table
end record Pgp2bRxPhyLaneInType;
```

`dataK` 标识每字节是否为 K 字符，`dispErr`/`decErr` 是 8B/10B 解码器报出的两类错误（直流平衡错 / 非法码字）。这正是 8B/10B 定界方式的「检错靠线路码本身」。

然后看 PGP3 的 BTF 与扰码定义。在 [Pgp3Pkg.vhd:43-68](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp3/core/rtl/Pgp3Pkg.vhd#L43-L68)：

```vhdl
-- BTF (块类型字段) 码值
constant PGP3_IDLE_C : slv(7 downto 0) := X"99";
constant PGP3_SOF_C  : slv(7 downto 0) := X"AA";
constant PGP3_EOF_C  : slv(7 downto 0) := X"55";
constant PGP3_SOC_C  : slv(7 downto 0) := X"CC";
constant PGP3_EOC_C  : slv(7 downto 0) := X"33";
constant PGP3_SKP_C  : slv(7 downto 0) := X"66";

-- 2 位 header: 数据字 vs 控制字
constant PGP3_D_HEADER_C : slv(1 downto 0) := "01";
constant PGP3_K_HEADER_C : slv(1 downto 0) := "10";

-- 扰码器抽头 (自同步扰码)
constant PGP3_SCRAMBLER_TAPS_C : IntegerArray(0 to 1) := (0 => 39, 1 => 58);
```

BTF 字段本身定位在 64 位字的最高字节，见 [Pgp3Pkg.vhd:72](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp3/core/rtl/Pgp3Pkg.vhd#L72)：`subtype PGP3_BTF_FIELD_C is natural range 63 downto 56;`。

PGP4 的 BTF、header、扰码抽头与 PGP3 **逐字相同**，见 [Pgp4Pkg.vhd:42-62](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp4/core/rtl/Pgp4Pkg.vhd#L42-L62)（IDLE/SOF/EOF/SOC/EOC/SKP 码值一致，`PGP4_SCRAMBLER_TAPS_C := (39, 58)`）。这说明 PGP4 在「定界」这一层没有改动 PGP3，只是在此基础上加了头部 CRC（见 4.3）。

最后注意 PGP2b 的数据位宽是 **2 字节/VC**（`SSI_PGP2B_CONFIG_C := ssiAxiStreamConfig(2, TKEEP_COMP_C)`，[Pgp2bPkg.vhd:31](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp2b/core/rtl/Pgp2bPkg.vhd#L31)），而 PGP3/PGP4 是 **8 字节/VC**：

```vhdl
constant PGP3_AXIS_CONFIG_C : AxiStreamConfigType :=
   ssiAxiStreamConfig(dataBytes => 8, tKeepMode => TKEEP_COMP_C,
                      tUserMode => TUSER_FIRST_LAST_C, tDestBits => 4, tUserBits => 2);
```

见 [Pgp3Pkg.vhd:34-40](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp3/core/rtl/Pgp3Pkg.vhd#L34-L40)，PGP4 同样是 `dataBytes => 8`（[Pgp4Pkg.vhd:33-39](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp4/core/rtl/Pgp4Pkg.vhd#L33-L39)）。`tDestBits => 4` 正好对应 4 位 VC 字段（16 个 VC）。

#### 4.2.4 代码实践

**实践目标**：在源码里验证「PGP3 和 PGP4 用同一套 BTF 与扰码抽头，而定界方式与 PGP2b 完全不同」。

**操作步骤（源码阅读型）**：

1. 打开 `Pgp3Pkg.vhd`，记录 6 个核心 BTF 码值（IDLE/SOF/EOF/SOC/EOC/SKP）和扰码抽头 `(39, 58)`。
2. 打开 `Pgp4Pkg.vhd`，逐字对比上述常量是否一致。
3. 打开 `Pgp2bPkg.vhd`，确认它**没有** BTF、没有扰码抽头、没有 D/K header，而是有一组 `K_*_C` 8B/10B K 字符常量。
4. 在 `Pgp2bPkg.vhd` 里找到 `Pgp2bRxPhyLaneInType`，确认它有 8B/10B 专用的 `dataK/dispErr/decErr` 字段，而 PGP3/PGP4 的 PHY 接口（在 Core 实体里）是 `phyRxData : slv(63 downto 0)` + `phyRxHeader : slv(1 downto 0)`。

**需要观察的现象**：PGP3 与 PGP4 的定界常量应当完全相同；PGP2b 则走的是另一套（K 字符）体系。

**预期结果**：你会得到一张「定界体系」的对照——PGP2b 是 8B/10B K 字符 + dispErr/decErr 自检错；PGP3/PGP4 是扰码 + 2 位 header + BTF，需要外部 gearbox 对齐。

#### 4.2.5 小练习与答案

**练习 1**：8B/10B 编码有 25% 的开销，为什么 PGP3/PGP4 还要换掉它？

**参考答案**：为了线效率。8B/10B 的 25% 开销在 10Gbps+ 速率下浪费太大；64b/66b 风格编码（64 位数据 + 2 位 header）开销仅约 3%，配合扰码器实现 DC 平衡与时钟恢复，更适合高速链路。

**练习 2**：在 PGP3/PGP4 里，接收方怎么区分「这一拍是数据」还是「这一拍是控制字」？

**参考答案**：靠 2 位 header：`PGP3_D_HEADER_C = "01"` 表示数据字，`PGP3_K_HEADER_C = "10"` 表示控制字。如果是控制字，再读最高字节的 BTF（如 SOF/EOF/IDLE）确定其具体语义。

**练习 3**：PGP2b 靠什么检测线路上的比特错误？

**参考答案**：靠 8B/10B 解码器报出的 `dispErr`（直流平衡错）和 `decErr`（非法码字）——线路码本身带检错能力，不需要额外 CRC 就能发现单拍错误。

---

### 4.3 PGP4 相对 PGP3 增加的 CRC 与错误上报

#### 4.3.1 概念说明

这是一个**最容易产生误解**的模块，必须先澄清：

> **PGP3 本来就带 CRC-32。** 它对每个数据 cell 都做 CRC-32 校验（多项式 `0x04C11DB7`，即 IEEE 802.3 的标准 CRC-32）。所以「PGP4 相对 PGP3 新增了 CRC」这句话如果理解成「PGP3 没有 CRC、PGP4 才有」，就是错的。

那么 PGP4 到底新增了什么？两件事：

1. **每个 K-code（控制字）的头部 CRC-8。** 这是 PGP4 最实质的 CRC 增量。PGP3 的控制字（携带 BTF、linkInfo、流控、版本号等）**没有任何校验**——一旦控制字里的某个比特翻转，接收方会默默地把错误的流控/版本/BTF 当成真的来用，可能引发难以定位的故障。PGP4 给每个控制字加了 8 位 CRC（`pgp4KCodeCrc`，多项式 `0x07`），接收侧用 `Pgp4RxKCodeChecker` 实时校验，校验失败就丢弃这个控制字并报 `linkError`。

2. **更细粒度的 cell 错误分类。** PGP3 的 `Pgp3RxOutType` 只有一个笼统的 `cellError`；PGP4 的 `Pgp4RxOutType` 把它拆成 `cellSofError`、`cellSeqError`、`cellVersionError`、`cellCrcModeError`、`cellCrcError`、`cellEofeError` 六类，方便软件精确定位链路问题。

**为什么头部 CRC 重要？** 数据 cell 的 CRC-32 只保护「数据」。但控制字携带的是「元数据」——链路是否 ready、对端每个 VC 的 pause 状态、协议版本号。这些字段一旦被干扰，后果往往比单个数据比特错误更严重（比如把 pause 看反了会导致丢帧）。PGP4 给头部单独加 CRC-8，就是把保护范围从「数据」扩展到「控制平面」。

**关于数据 CRC 的实现细节（容易看走眼的地方）**：在 PGP3 和 PGP4 的「主路（full）」发送路径里，cell 的 CRC-32 **不是 PGP 自己算的**，而是复用了 `AxiStreamPacketizer2`（见 u5-l4）算出的尾 CRC——PGP 成帧器只是把这个 CRC 拷贝进 EOFC（帧尾 cell）字段。但在 PGP4 的「Lite 低速变体」（`Pgp4TxLiteProtocol`）里，因为不再用 packetizer，所以自己例化了一个 `Crc32Parallel` 来算。这说明「数据 CRC」在三代里都存在，只是来源不同。

#### 4.3.2 核心流程

PGP3 控制字的保护（概念）：

```text
发送: K-code = [BTF(8) | linkInfo/seq/vc(56)]   -- 64 位, 无校验
接收: 直接信任 BTF 与 linkInfo; 若 8B/66b header 或扰码同步丢失才报错
```

PGP4 控制字的保护（概念）：

```text
发送: K-code = [BTF(8) | headerCrc-8(8) | payload(48)]
       其中 headerCrc-8 = pgp4KCodeCrc(整字)  -- 覆盖 BTF + payload 共 56 位
接收: Pgp4RxKCodeChecker 每拍重算 pgp4KCodeCrc, 与收到的 headerCrc-8 比对
       不等 -> 丢弃该 K-code, 拉高 linkError, 进入 holdoff 一拍
```

PGP4 cell 错误分类（数据平面）：

```text
depacketizer 报出 -> pgpRxOut.cellCrcError     (CRC 不匹配)
                  -> pgpRxOut.cellCrcModeError  (CRC 模式不一致)
协议引擎报出      -> cellSofError / cellSeqError / cellVersionError / cellEofeError
聚合              -> cellError (以上任一发生即置位)
```

#### 4.3.3 源码精读

先确认「PGP3 也有 CRC-32」。在 [Pgp3Pkg.vhd:77-83](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp3/core/rtl/Pgp3Pkg.vhd#L77-L83)：

```vhdl
subtype PGP3_EOFC_CRC_FIELD_C is natural range 55 downto 24;   -- EOFC 里的 32 位 CRC 字段
constant PGP3_CRC_POLY_C      : slv(31 downto 0) := X"04C11DB7";  -- IEEE 802.3 CRC-32
```

并且 PGP3 发送时把这个字段填成 packetizer 的尾 CRC，见 [Pgp3TxProtocol.vhd:227](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp3/core/rtl/Pgp3TxProtocol.vhd#L227)：

```vhdl
v.protTxData(PGP3_EOFC_CRC_FIELD_C) := pgpTxMaster.tData(PACKETIZER2_TAIL_CRC_FIELD_C);  -- CRC
```

注释里的 `-- CRC` 明确这是 CRC 字段。可见 PGP3 有数据 CRC。

PGP4 也有完全相同的 EOFC CRC-32（同多项式），见 [Pgp4Pkg.vhd:75-80](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp4/core/rtl/Pgp4Pkg.vhd#L75-L80)，主路同样从 packetizer 拷贝（[Pgp4TxProtocol.vhd:245](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp4/core/rtl/Pgp4TxProtocol.vhd#L245)）。

**真正的新增：PGP4 的头部 CRC-8。** 在 [Pgp4Pkg.vhd:64-65](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp4/core/rtl/Pgp4Pkg.vhd#L64-L65) 多出了一个 PGP3 没有的字段：

```vhdl
subtype PGP4_BTF_FIELD_C      is natural range 63 downto 56;  -- BTF(8)
subtype PGP4_K_CODE_CRC_FIELD_C is natural range 55 downto 48; -- 头部 CRC-8 (PGP3 无此字段)
```

对应的计算函数 `pgp4KCodeCrc` 在 [Pgp4Pkg.vhd:225-257](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp4/core/rtl/Pgp4Pkg.vhd#L225-L257)，是一个 CRC-8（多项式 `X"07"`，即 \(x^8 + x^2 + x + 1\) 的二进制表示，对应注释里的 \(x^8+x^3+x^2+x+1\) 描述与 Koopman 记法 0x83）：

```vhdl
function pgp4KCodeCrc (kCodeWord : slv(63 downto 0)) return slv is
   constant CRC_POLY_C : slv(7 downto 0) := X"07";   -- CRC-8 多项式
   variable data : slv(55 downto 0);  -- 只取 56 个非 CRC 位
   ...
begin
   data(47 downto 0)  := kCodeWord(47 downto 0);
   data(55 downto 48) := kCodeWord(63 downto 56);  -- 汇总 BTF + payload
   data := bitReverse(data);                        -- 按比特反转输入
   for d in 0 to 55 loop
      fb  := (others => (ret(7) xor data(d)));
      ret := ret(6 downto 0) & fb(0);
      ret := (fb and CRC_POLY_C) xor ret;           -- LFSR 除法
   end loop;
   ret := bitReverse(ret);                          -- 反转并取反输出
   ret := not ret;
   return ret;
end function;
```

它覆盖的是 56 位（BTF 8 位 + 控制字 payload 48 位），输出 8 位 CRC。这是一个标准的 CRC-8 串行 LFSR 实现（位反转 + 取反是 CRC 常见的初值/输出处理），其检错概率为：

\[ P_{\text{漏检}} \approx 2^{-8} = \frac{1}{256} \]

即单个控制字的突发错误有约 1/256 的概率恰好通过 CRC-8 校验。

发送侧在每个 K-code 发出前填入这个 CRC，见 [Pgp4TxProtocol.vhd:339](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp4/core/rtl/Pgp4TxProtocol.vhd#L339)：

```vhdl
v.protTxData(PGP4_K_CODE_CRC_FIELD_C) := pgp4KCodeCrc(v.protTxData);
```

接收侧的校验模块 `Pgp4RxKCodeChecker` 在 [Pgp4RxKCodeChecker.vhd:73-90](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp4/core/rtl/Pgp4RxKCodeChecker.vhd#L73-L90)：

```vhdl
if (phyRxValid = '1') and (phyRxHeader = PGP4_K_HEADER_C) and
   (phyRxData(PGP4_K_CODE_CRC_FIELD_C) /= pgp4KCodeCrc(phyRxData)) then
   badKCode := '1';
end if;
...
-- Drop K-codes with invalid checksum
if (badKCode = '1') then
   v.checkedValid := '0';   -- 丢弃这个坏 K-code
   v.linkError    := '1';   -- 上报链路错误
   v.holdoff      := '1';   -- holdoff 一拍
end if;
```

注意它采用 u1-l5 的双进程骨架（`RegType` + `REG_INIT_C` + `r/rin` + `comb`/`seq`），`comb` 里 `badKCode` 是组合判断、`v` 是次态变量，`seq` 只在上升沿打寄存器。

最后看错误分类的细化。PGP3 的 `Pgp3RxOutType` 只有一个 `cellError`（[Pgp3Pkg.vhd:163](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp3/core/rtl/Pgp3Pkg.vhd#L163)），而 PGP4 在 [Pgp4Pkg.vhd:160-166](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp4/core/rtl/Pgp4Pkg.vhd#L160-L166) 把它拆成 6 类：

```vhdl
cellError        : sl;  -- 聚合: 以下任一发生
cellSofError     : sl;  -- SOF 异常
cellSeqError     : sl;  -- 序号错
cellVersionError : sl;  -- 版本不匹配
cellCrcModeError : sl;  -- CRC 模式不一致 (来自 depacketizer)
cellCrcError     : sl;  -- CRC 校验失败 (来自 depacketizer)
cellEofeError    : sl;  -- EOFE 错误帧
```

其中数据 CRC 错误直接来自解包器，见 [Pgp4Rx.vhd:268-269](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp4/core/rtl/Pgp4Rx.vhd#L268-L269)：

```vhdl
pgpRxOut.cellCrcModeError <= depacketizerDebug.crcModeError;
pgpRxOut.cellCrcError     <= depacketizerDebug.crcError;
```

> 额外细节：PGP4 还多了一个 `RX_CRC_PIPELINE_G`（0 或 1，见 [Pgp4Rx.vhd:37](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/pgp/pgp4/core/rtl/Pgp4Rx.vhd#L37)），用于在高速设计里给 CRC 校验逻辑插一拍流水寄存器以改善时序；PGP3 没有这个选项。

#### 4.3.4 代码实践

**实践目标**：亲手在源码里确认「PGP3 已经有数据 CRC-32」「PGP4 新增的是头部 CRC-8」「PGP4 把 cell 错误拆成 6 类」这三句话，而不是凭印象记忆。

**操作步骤（源码阅读型）**：

1. 在 `Pgp3Pkg.vhd` 里搜索 `CRC`，找到 `PGP3_EOFC_CRC_FIELD_C` 与 `PGP3_CRC_POLY_C`，确认 PGP3 有 32 位数据 CRC。
2. 在 `Pgp3TxProtocol.vhd` 第 227 行附近，确认该 CRC 来自 `PACKETIZER2_TAIL_CRC_FIELD_C`（即 packetizer 算的）。
3. 在 `Pgp4Pkg.vhd` 里搜索 `K_CODE_CRC`，确认 PGP4 多出了 `PGP4_K_CODE_CRC_FIELD_C`（8 位）与函数 `pgp4KCodeCrc`，多项式是 `X"07"`；在 `Pgp3Pkg.vhd` 里确认**没有**任何 K-code 头部 CRC。
4. 在 `Pgp4RxKCodeChecker.vhd` 第 73–90 行，读懂校验失败时「丢弃 + 报错 + holdoff」的处理。
5. 对比 `Pgp3RxOutType` 与 `Pgp4RxOutType` 里以 `cell` 开头的字段数量。

**需要观察的现象**：

- PGP3 有 EOFC CRC-32，但**没有** K-code 头部 CRC。
- PGP4 两者都有。
- PGP3 的 cell 错误字段只有 1 个（`cellError`），PGP4 有 7 个（含聚合的 `cellError`）。

**预期结果**：你会得出本讲最关键的一句结论——**「PGP4 相对 PGP3 增加的 CRC」特指控制字头部的 CRC-8，外加更细的 cell 错误分类；数据 CRC-32 两代都有。**

如果要在仿真器里实际观察头部 CRC 的纠错/检错行为，需要搭建 PGP4 loopback 并注入 K-code 比特错误，本仓库的 `protocols/pgp/pgp4/core/tb/` 与 `tests/` 下有相应测试，但配置较重，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：「PGP4 比 PGP3 多了 CRC」这种说法哪里不准确？

**参考答案**：不准确之处在于暗示「PGP3 没有 CRC」。事实上 PGP3 已经对每个数据 cell 做了 CRC-32（`PGP3_CRC_POLY_C = 0x04C11DB7`，字段 `PGP3_EOFC_CRC_FIELD_C`）。PGP4 真正新增的是**控制字（K-code）头部的 CRC-8**（`pgp4KCodeCrc`，多项式 `0x07`），把校验保护从数据平面扩展到了控制平面。

**练习 2**：`pgp4KCodeCrc` 覆盖的是多少位数据？为什么选 CRC-8 而不是 CRC-32？

**参考答案**：覆盖 56 位（BTF 8 位 + 控制字 payload 48 位，输出 8 位）。选 CRC-8 是因为控制字每拍都要校验、且位宽不大，CRC-8 开销小（仅 8 位）、逻辑浅，足以把单比特/短突发错误的漏检概率压到约 \(2^{-8}\)；对更长的数据 cell 才用 CRC-32。

**练习 3**：在 PGP4 里，数据 cell 的 CRC-32 是谁算的？主路和 Lite 路一样吗？

**参考答案**：**主路（full）**复用 `AxiStreamPacketizer2` 算出的尾 CRC，PGP 成帧器只把它拷进 EOFC 字段（`Pgp4TxProtocol.vhd:245`）。**Lite 低速变体**不再用 packetizer，所以自己例化了一个 `Crc32Parallel` 来算（见 `Pgp4TxLiteProtocol.vhd`）。两路最终都产生同样的 CRC-32 字段。

---

## 5. 综合实践

把本讲三个模块串起来，完成一张「PGP 三代对比表」。这是本讲代码实践任务的最终交付物，也是后续阅读任何 PGP 源码时的速查表。

**任务**：阅读 `Pgp2bPkg.vhd`、`Pgp3Pkg.vhd`、`Pgp4Pkg.vhd`（以及必要的 Tx/Core 文件），填写下表并回答表后的问题。

| 对比维度 | PGP2b | PGP3 | PGP4 |
|---|---|---|---|
| 数据位宽（每 VC） | ？字节（找 `SSI_PGP2B_CONFIG_C`） | ？字节（找 `PGP3_AXIS_CONFIG_C`） | ？字节（找 `PGP4_AXIS_CONFIG_C`） |
| PHY 数据/头部接口 | data[15:0]+dataK+dispErr+decErr | slv(63:0)+header[1:0] | slv(63:0)+header[1:0] |
| 定界方式 | ？（K 字符） | ？（扰码+BTF） | ？（扰码+BTF） |
| 版本号常量与位宽 | `PGP2B_ID_C`（？位） | `PGP3_VERSION_C`（？位） | `PGP4_VERSION_C`（？位） |
| VC 数上限 | ？（看 `NUM_VC_EN_G`） | ？（看 `NUM_VC_G`） | ？（看 `NUM_VC_G`） |
| 数据 cell CRC | 无显式（靠 8B/10B） | CRC-32，由 packetizer 算 | CRC-32，主路同 PGP3，Lite 自算 |
| 控制/K-code 头部 CRC | 无 | ？ | ？（`pgp4KCodeCrc`，CRC-8） |
| RX cell 错误粒度 | 单一 `cellError` | 单一 `cellError` | 6 类细分 |

**需要回答**：

1. 三代里，哪两代在「定界」这一层几乎完全相同？（答：PGP3 与 PGP4——BTF 码值、2 位 header、扰码抽头 `(39,58)` 逐字一致。）
2. PGP4 相对 PGP3 最实质的协议层增量是什么？（答：控制字头部 CRC-8 与细分 cell 错误。）
3. 为什么 PGP2b 不需要单独的数据 CRC？（答：8B/10B 线路码本身带 dispErr/decErr 检错，且速率低、cell 概念弱。）

**预期结果**：填完表后，你应当能在不看源码的情况下，向别人讲清三代 PGP 的演进脉络：**PGP2b（8B/10B、2 字节、4 VC、线路码自检错）→ PGP3（扰码+BTF、8 字节、16 VC、数据 CRC-32）→ PGP4（在 PGP3 基础上加控制字头部 CRC-8 与细分错误上报）**。

> 想动手验证的同学：仓库 `tests/protocols/` 下有 PGP 相关的 cocotb 回归（参考 u9-l1/u9-l2 的工具链），可在 GHDL 里跑 loopback 观察 `linkReady`/`cellError` 等信号。搭建完整 PGP 仿真较重，**待本地验证**。

## 6. 本讲小结

- **PGP 是一条点对点高速链路**，用 **TDEST/虚拟通道（VC）** 在一根光纤上承载多条独立 AXI-Stream。这套「VC 路由」思想在三代协议里完全一致，是 PGP 的核心抽象。
- **VC 数随代际增长**：PGP2b 最多 4 个 VC（4 位流控），PGP3/PGP4 最多 16 个 VC（16 位流控），流控位宽始终等于 VC 数。
- **定界方式分两派**：PGP2b 用 **8B/10B 的 K 字符**（K28.5/K23.7/K29.7…）定界，靠线路码的 dispErr/decErr 自检错；PGP3/PGP4 改用 **扰码 + 2 位 header + BTF**（IDLE 0x99/SOF 0xAA/EOF 0x55…，扰码抽头 39/58），开销更低、适合更高速率。
- **数据位宽**：PGP2b 是 2 字节/VC，PGP3/PGP4 是 8 字节/VC。
- **关于 CRC 的重要澄清**：PGP3 **本来就带数据 CRC-32**（多项式 `0x04C11DB7`，主路由 packetizer 计算）。PGP4 真正新增的是**每个 K-code 控制字的头部 CRC-8**（`pgp4KCodeCrc`，多项式 `0x07`），把保护从数据平面扩展到控制平面。
- **错误上报更细**：PGP3 只有一个笼统的 `cellError`，PGP4 拆成 SOF/Seq/Version/CrcMode/Crc/Eofe 六类，便于软件定位。
- 三代都遵循 **`core/`（家族无关、可 GHDL 仿真）+ 家族 PHY 目录** 的拆分（见 u1-l3、u6-l4）。

## 7. 下一步学习建议

本讲是「地图」，接下来建议按以下顺序深入：

1. **u7-l2 RSSI：可靠流式传输**。RSSI 跑在 PGP 之上，给原本「尽力而为」的 PGP 链路加上连接、序号、重传，是把本讲的链路协议变成「可靠通道」的关键一层。读 RSSI 时你会再次遇到 BTF 式的帧头与 EOFE 错误语义。
2. **若要深入单个 PGP 代际的状态机**：从 `Pgp3Core.vhd` 出发，依次读 `Pgp3Tx` → `Pgp3TxProtocol`（成帧）、`Pgp3Rx` → `Pgp3RxProtocol`（解帧）、`Pgp3RxGearboxAligner`（字边界对齐）。PGP4 的对应文件结构几乎相同，多出 `Pgp4RxKCodeChecker`。
3. **若关注 PHY 与家族封装**：对比 `pgp3/gthUs+/rtl/` 与 `pgp3/gtyUs+/rtl/`，理解 `core/` 如何经窄接口（config/status）与家族 PHY 互通，并把 `.dcp` 网表接进 Vivado 工程（回顾 u6-l4）。
4. **若关注验证**：阅读 `tests/protocols/` 下 PGP 相关的 cocotb 测试与 `*_test_utils.py`，看回归如何注入错误、检查 `cellError` 与头部 CRC（回顾 u9-l1、u9-l2）。
