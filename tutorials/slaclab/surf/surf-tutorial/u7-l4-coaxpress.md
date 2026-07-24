# CoaXPress 核心

## 1. 本讲目标

CoaXPress（CXP）是机器视觉相机与采集卡之间的高速串行接口标准。本讲聚焦 SURF 中 CXP 核心的内部实现，学完后你应当能够：

- 说清 CoaXPress 的**非对称双方向**模型：高速下行（相机→FPGA，传图像）与低速上行（FPGA→相机，发触发与配置寄存器）。
- 读懂高速接收状态机 `CoaXPressRxHsFsm` 如何把一串带标记的字节流还原成「图像头 + 一行行像素」的结构化帧。
- 读懂低速发送状态机 `CoaXPressTxLsFsm` 如何用一个「心跳节拍」把触发、配置、空闲三类报文按时分复用串行送出。
- 理解 `CoaXPressOverFiberBridge` 如何用 10GBASE-R 光纤（XGMII）承载原本跑在同轴电缆上的 CXP，以及 `core/` 与家族 PHY 目录的拆分。
- 在 `CoaXPressPkg.vhd` 中找到 SOP/EOP/IDLE 等定界常量，并说明它们在高速流中的作用。

## 2. 前置知识

- **8B/10B 与 K 字符**（见 u5-l4）：CXP 在物理层用 8B/10B 编码，数据字节称 D 字符，控制字节称 K 字符（如 K28.5 是用于位对齐的 comma）。一串全由 K 字符组成的 32 位字只可能是「定界符」而不会与图像数据混淆——这是 CXP 定界能可靠工作的根本原因。
- **AXI-Stream 记录与握手**（见 u4-l1 / u5-l1）：CXP 核内部用 `AxiStreamMasterType/SlaveType` 搬运数据与配置，并用 SSI 的 SOF/EOF/EOFE 表达帧边界。
- **双进程 RTL 风格**（见 u1-l5）：本讲的 FSM 全部沿用 `RegType` + `REG_INIT_C` + `r/rin` + `comb`/`seq` 三明治骨架，`comb` 用 `variable v := r` 算次态、`seq` 在上升沿 `r <= rin after TPD_G`。
- **目录约定与家族拆分**（见 u1-l3）：`core/` 放家族无关逻辑（可在 GHDL 纯仿真），家族 PHY 收发器胶水放 `gthUs`/`gthUs+`/`gtyUs+` 等目录，由 `ruckus.tcl` 用 `getFpgaArch` 选择。

> 名词速查：**下行/上行（down/up connection）**、**lane（通道）**、**UI（unit interval，比特周期）**、**XGMII（10G 介质无关接口，64 位数据 + 8 位控制 @156.25 MHz）**。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [CoaXPressPkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressPkg.vhd) | 协议包：定界常量（SOP/EOP/IDLE/MARKER…）、速率枚举、Over-Fiber 帧符。是全模块的「单一事实来源」。 |
| [CoaXPressCore.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressCore.vhd) | 纯结构顶层：把 Config / Tx / Rx / AxiL 四块拼成完整核心，跨 5 个时钟域。 |
| [CoaXPressRxHsFsm.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressRxHsFsm.vhd) | **高速接收 FSM**：解析图像头与逐行像素（本讲重点 1）。 |
| [CoaXPressTxLsFsm.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressTxLsFsm.vhd) | **低速发送 FSM**：心跳节拍 + 触发/配置/空闲时分复用（本讲重点 2）。 |
| [CoaXPressOverFiberBridge.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressOverFiberBridge.vhd) | **Over-Fiber 桥顶层**：CXP↔XGMII（本讲重点 3）。 |
| [CoaXPressOverFiberBridgeRx.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressOverFiberBridgeRx.vhd) / [...Tx.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressOverFiberBridgeTx.vhd) | 桥的收/发子状态机，做 XGMII 帧符 ↔ CXP 帧符互译。 |
| [ruckus.tcl](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/ruckus.tcl) | 总清单：无条件加载 `core/`，按家族加载 PHY 目录。 |

## 4. 核心概念与源码讲解

### 4.1 CoaXPress 帧定界常量与 Core 总体结构

#### 4.1.1 概念说明

CoaXPress 是**非对称双方向**接口，这一点是理解整个核心的钥匙：

- **下行（Down Connection，高速）**：相机向 FPGA 发送图像数据，单 lane 可达 CXP-6（6.25 Gbps）到 CXP-12（12.5 Gbps），多 lane 并行（本核心支持 1–8 lane）。这一路只发图像，不走寄存器。
- **上行（Up Connection，低速）**：FPGA 向相机发送两类东西——① **触发（Trigger）**：告诉相机「现在曝光/采样」；② **配置（寄存器读写）**：访问相机内部寄存器。上行固定为低速 20.833 Mbps（CXP 默认）或 41.666 Mbps。

正因为方向不对称，本核心把收发拆成两个独立 FSM：高速收（`CoaXPressRxHsFsm`）与低速发（`CoaXPressTxLsFsm`）。

CXP 在链路上靠**全 K 字符的 32 位字**做定界。回忆 8B/10B：正常图像数据是 D 字符，控制字符是 K 字符。当一个 32 位字的 4 个字节全是 K 字符（`rxDataK = 0xF`）时，它绝不可能是像素数据，只能是一个定界符。`CoaXPressPkg` 正是把这些定界符集中定义的地方。

#### 4.1.2 核心流程

CXP 核心的数据流（下行方向）大致是：

```
相机 ──(8B/10B 串行)──> [每 lane 一个 CoaXPressRxLane: 解码/对齐/剥 SOP·EOP]
                          │
                          v
                   [CoaXPressRxLaneMux: 多 lane 合并]
                          │
                          v
                   [CoaXPressRxHsFsm: 解析 图像头 + 逐行像素] ──> dataMaster (像素)
                                                          └──> hdrMaster  (图像头)
```

低速上行方向：

```
软件寄存器 ──> [CoaXPressConfig: 组 config 报文] ─┐
触发输入   ──────────────────────────────────────┼──> [CoaXPressTxLsFsm: 心跳节拍时分复用]
事件 ACK  ──> [CoaXPressEventAckMsg] ────────────┘            │
                                                              v
                                                   txData/txDataK (8B/10B 字节流, 20.83/41.66 Mbps)
```

`CoaXPressCore` 是纯结构顶层，本身不写逻辑，只把上面四块（Config / Tx / Rx / AxiL）用信号连起来，并处理多时钟域。

#### 4.1.3 源码精读

**定界常量**集中定义在 `CoaXPressPkg` 中：

[protocols/coaxpress/core/rtl/CoaXPressPkg.vhd:30-36](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressPkg.vhd#L30-L36) 定义了 7 个 32 位定界字与一个 4 位 K 标志。要点：

- 每个定界字都是**同一个 K 字符重复 4 次**（如 `CXP_SOP_C = K_27_7_C × 4 = 0xFBFBFBFB`），保证 `rxDataK = 0xF` 时一眼可辨。
- `CXP_IDLE_C` 是例外：它由 `D_21_5 & K_28_1 & K_28_1 & K_28_5` 混合组成（`0xB53C3CBC`）。其中 **K28.5 是 comma**，接收端靠它在比特流里做位对齐；空闲时持续发 IDLE 既保持链路锁定又表示「无数据」。

各常量的语义对照表：

| 常量 | K 字符 | 数值 | 含义 | 谁用它 |
|------|--------|------|------|--------|
| `CXP_IDLE_C` | D21.5,K28.1,K28.5 | `0xB53C3CBC` | 空闲（含 comma） | RxLane 判空闲、Bridge 填充 |
| `CXP_SOP_C` | K27.7 | `0xFBFBFBFB` | 高速包起始 | RxLane 剥头、Config/Event 组包 |
| `CXP_EOP_C` | K29.7 | `0xFDFDFDFD` | 高速包结束 | RxLane 剥尾、Config/Event 组包 |
| `CXP_MARKER_C` | K28.3 | `0x7C7C7C7C` | 图像传输流标记 | **RxHsFsm** 找图像结构起点 |
| `CXP_TRIG_C` | K28.2 | `0x5C5C5C5C` | 触发指示 | 低速触发报文 |
| `CXP_IO_ACK_C` | K28.6 | `0xDCDCDCDC` | I/O 应答 | RxLane 识别 |

注意一个层次区别：`CXP_SOP_C`/`CXP_EOP_C` 是**链路层**包定界（由 `CoaXPressRxLane` 消费并转成 AXI-Stream 的 `tLast` 等），而 `CXP_MARKER_C` 是 `CoaXPressRxHsFsm` 在**图像传输层**用的标记——4.2 节会看到它在状态机里是「找图像结构起点」的唯一钥匙。

`CoaXPressCore` 顶层是四块拼图：

[protocols/coaxpress/core/rtl/CoaXPressCore.vhd:114-134](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressCore.vhd#L114-L134) 例化 `CoaXPressConfig`（配置报文组装，`cfgClk` 域）；
[protocols/coaxpress/core/rtl/CoaXPressCore.vhd:136-160](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressCore.vhd#L136-L160) 例化 `CoaXPressTx`（内含本讲的 `TxLsFsm`，`txClk` 域）；
[protocols/coaxpress/core/rtl/CoaXPressCore.vhd:162-196](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressCore.vhd#L162-L196) 例化 `CoaXPressRx`（内含本讲的 `RxHsFsm`，跨 `dataClk`/`rxClk`）；
[protocols/coaxpress/core/rtl/CoaXPressCore.vhd:198-248](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressCore.vhd#L198-L248) 例化 `CoaXPressAxiL`（AXI-Lite 寄存器，`axilClk` 域，是各域状态/控制的汇聚点）。

可见 `dataClk`、`cfgClk`、`txClk`、`rxClk`、`axilClk` 五个时钟域的信号都在顶层交汇，跨域搬运由各子模块内部的 FIFO / 同步器完成——这是 CXP 核心比单时钟模块「看起来更碎」的原因。

#### 4.1.4 代码实践

**目标**：在 `CoaXPressPkg.vhd` 中找到 SOP/EOP/IDLE 等定界常量，并解释高速流如何用它们定界。

**步骤**：
1. 打开 `protocols/coaxpress/core/rtl/CoaXPressPkg.vhd`，定位 30–36 行的常量块。
2. 对照 `protocols/line-codes/rtl/Code8b10bPkg.vhd`，确认每个 K 字符的 8 位码（如 `K_27_7_C = 0xFB`、`K_29_7_C = 0xFD`、`K_28_3_C = 0x7C`、`K_28_5_C = 0xBC`）。
3. 在仓库内搜索 `CXP_SOP_C`、`CXP_EOP_C`、`CXP_MARKER_C` 的使用点（参考本讲已列出的 grep 结果）。

**需要观察的现象**：
- `CXP_SOP_C`/`CXP_EOP_C` 几乎都出现在「组包」（`CoaXPressConfig`、`CoaXPressEventAckMsg`）和「剥包」（`CoaXPressRxLane` 的 `if (rxDataK = x"F") and (rxData = CXP_SOP_C)`）两侧，说明它们是**链路层**的包界。
- `CXP_MARKER_C` 只在 `CoaXPressRxHsFsm` 出现，说明它是更上层的**图像结构**界。
- `CXP_IDLE_C` 含 comma（K28.5），这是接收端能在上电时锁定比特对齐的关键。

**预期结果**：你能用一句话回答「为什么用全 K 字符定界」——因为图像像素是 D 字符，全 K 字符的 32 位字（`rxDataK = 0xF`）天然不可能与数据冲突，故可作可靠边界。

#### 4.1.5 小练习与答案

**练习 1**：`CXP_IDLE_C` 为什么不也用「同一个 K 字符重复 4 次」，而要混入 D21.5？
**答**：IDLE 既要表示「链路空闲无数据」，又要持续提供 comma（K28.5）让接收端维持比特/字对齐。混入 D 字符并保留 K28.5 comma 即可同时满足两点；纯 K 字符重复虽也能对齐，但标准选定了这个特定图案（`0xB53C3CBC`）。

**练习 2**：为什么 `CoaXPressCore` 要拆成 Config/Tx/Rx/AxiL 四块而不是一个大 FSM？
**答**：因为收发是物理上非对称、且分属不同时钟域（高速 `rxClk`/`dataClk`、低速 `txClk`、配置 `cfgClk`、寄存器 `axilClk`）。拆分让每个 FSM 只在自己的域里跑，跨域用 FIFO/同步器隔离，符合 u2-l1 的 CDC 原则。

---

### 4.2 高速接收 FSM（CoaXPressRxHsFsm）

#### 4.2.1 概念说明

`CoaXPressRxHsFsm` 处于高速下行链路的最末端。在它之前，`CoaXPressRxLane` 已经做完 8B/10B 解码、comma 对齐，并把 `CXP_SOP_C`/`CXP_EOP_C` 消费掉、转成 AXI-Stream 的帧边界。于是到达 `RxHsFsm` 的 `rxMaster` 已经是「干净的 32 位字流」，但仍是**一串字节**——`RxHsFsm` 的任务是把它**重新结构化**成「一张图像 = 1 个图像头 + 若干行像素」。

CXP 图像传输协议用一个很朴素的办法表达结构：

- 先发一个 `CXP_MARKER_C`（K28.3）标记「一段流开始」；
- 紧跟一个「类型字」说明这段是图像头（`0x01010101`）还是一行像素的行标记（`0x02020202`）；
- 若是图像头，后面跟 25 个字（每个字的有效信息只在最低字节，且 4 个字节必须相同——这是一种冗余校验）；
- 若是行标记，后面跟的就是这一行的像素数据，行长度由图像头里的 `dsizeL` 字段决定。

> 注意类型字 `0x01010101`/`0x02020202` 不是 K 字符（它们是普通数据 `0x01`/`0x02` 重复 4 次），但用「4 字节相同」来表示，仍可和数据区分。

#### 4.2.2 核心流程

状态机有 5 个状态（[CoaXPressRxHsFsm.vhd:51-56](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressRxHsFsm.vhd#L51-L56)）：

```
              CXP_MARKER_C
IDLE_S ──────────────────────> TYPE_S
  ^                                │ 0x01010101(图像头)        0x02020202(行标记,且 hdrValid)
  │                                ├────────────────> HDR_S     ─────────────> LINE_S
  │                                │  收满 25 字头               逐字搬像素,到行末
  │                                │  输出 hdrMaster              │
  │                                │                              │ 行末对齐不上
  │                                │                              v
  │                                │                           STEP_S
  └────────────────────────────────┴──────────────────────────────┘
                (任何不符预期的字都置 errDet 并回 IDLE_S)
```

- **IDLE_S**：在每个 32 位槽里找 `CXP_MARKER_C`，找到就进 `TYPE_S`，否则记一次错误。
- **TYPE_S**：据类型字分流。注意进入 `LINE_S` 前要求 `hdrValid='1'`（先收到过完整图像头），否则视为乱序、回 `IDLE_S`。
- **HDR_S**：累加图像头。每拍检查 4 字节是否一致（不一致即损坏），计到 25 拍时把拼好的头从 `hdrMaster` 单拍送出。
- **LINE_S**：把输入字原样搬到 `dataMasters(0)` 当像素输出，并用 `lineRem`（剩余字数）判断行末。多 lane 时一个 beat 含多个 32 位字，需逐字计 `dCnt`。
- **STEP_S**：当行末落在一个 beat 的中间、剩余字不足以填满整 beat 时，用 `STEP_S` 逐字「步进」搬完最后一个不完整 beat，再回 `IDLE_S`。

输出分两路：`hdrMaster`（图像头，单字、带 SOF）、`dataMaster`（像素，经 `CoaXPressRxWordPacker` 把 32 位字重打包成下游需要的宽度）。

#### 4.2.3 源码精读

**找标记与类型分流**：

[protocols/coaxpress/core/rtl/CoaXPressRxHsFsm.vhd:188-200](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressRxHsFsm.vhd#L188-L200) 是 `IDLE_S`：仅当 `tData = CXP_MARKER_C` 才进 `TYPE_S`，否则 `dbg.errDet := '1'`。

[protocols/coaxpress/core/rtl/CoaXPressRxHsFsm.vhd:202-242](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressRxHsFsm.vhd#L202-L242) 是 `TYPE_S`：`0x01010101` → `HDR_S`；`0x01010101` 检测到时还顺手做一次「乱序检查」——若上次已有有效头 (`hdrValid='1'`) 但行计数 `yCnt` 不等于 `hdr.ySize`，说明上一帧没收够行数就开了新头，记错误。

**图像头累加（25 字）**：

[protocols/coaxpress/core/rtl/CoaXPressRxHsFsm.vhd:244-276](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressRxHsFsm.vhd#L244-L276) 是 `HDR_S`。先做 4 字节一致性检查（245–247 行）：CXP 头里每个 32 位字的 4 个字节必须相同，否则判损坏。计到 `hdrCnt = 25` 时置 `hdrValid` 并把头送出。

每个 `hdrCnt` 值对应头里一个字段，[CoaXPressRxHsFsm.vhd:444-481](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressRxHsFsm.vhd#L444-L481) 用一个大 `case` 把 `tData(7 downto 0)` 装进 `ImageHdrType` 的各字段（`steamId`、`sourceTag`、`xSize`、`xOffs`、`ySize`、`yOffs`、`dsizeL`、`pixelF`、`tapG`、`flags`）。`ImageHdrType` 记录定义见 [58-69 行](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressRxHsFsm.vhd#L58-L69)。

**逐行像素搬运与行末处理**：

[protocols/coaxpress/core/rtl/CoaXPressRxHsFsm.vhd:299-356](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressRxHsFsm.vhd#L299-L356) 是 `LINE_S`。核心是一段 `for i in 0 to NUM_LANES_G-1` 循环，逐个 32 位槽统计本 beat 有效字数 `wordCnt`，并把有效槽的 `tKeep` 置 `0xF`、`dCnt` 递增；当 `wordCnt = r.lineRem` 时表示行末落在本 beat，置 `endOfLine` 并回 `IDLE_S`。若行末没落在 beat 末尾（`eolWrd /= NUM_LANES_G-1`），则暂不拉 `tReady`、记住 `wrd` 偏移，下一拍让该 beat 里行末之后的字被重新当作新 marker/type 解析——这正是 `STEP_S` 存在的原因。

**行末/帧末判定**：

[protocols/coaxpress/core/rtl/CoaXPressRxHsFsm.vhd:411-426](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressRxHsFsm.vhd#L411-L426)：上一拍的 `endOfLine` 在本拍生效，`yCnt` 递增；当 `yCnt = hdr.ySize` 时给 `dataMasters(1)` 的 `tLast` 置 1——即收满 `ySize` 行就结束一帧。

**`lineRem` 的时序优化**（值得专门一看）：

[protocols/coaxpress/core/rtl/CoaXPressRxHsFsm.vhd:483-495](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressRxHsFsm.vhd#L483-L495) 把「剩余字数 = dsizeL − dCnt」算出来并**饱和**到一个不超过 `NUM_LANES_G+1` 的小量 `lineRem`。注释解释：这切断了「宽位 `dCnt` → `wrd`」的长组合路径，便于时序收敛。这正是顶层泛型 `RX_FSM_CNT_WIDTH_G`（[CoaXPressCore.vhd:31](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressCore.vhd#L31)）注释「按相机优化此值以帮助 `CoaXPressRxHsFsm` 收时序」的落点。

**输出打包**：

[protocols/coaxpress/core/rtl/CoaXPressRxHsFsm.vhd:548-568](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressRxHsFsm.vhd#L548-L568)：输入侧挂一个 `AxiStreamPipeline`（切组合路径），输出侧挂 `CoaXPressRxWordPacker` 把 32 位字重打成下游宽度。

#### 4.2.4 代码实践

**目标**：跟踪一次「图像头 + 一行像素」在 `RxHsFsm` 里的解析路径。

**步骤**（源码阅读型实践）：
1. 读 [CoaXPressRxHsFsm.vhd:51-56](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressRxHsFsm.vhd#L51-L56) 的 `StateType`，记住 5 个状态名。
2. 假设激励顺序为：`CXP_MARKER_C` → `0x01010101` → 25 个头字 → `CXP_MARKER_C` → `0x02020202` → `dsizeL` 个像素字。在纸上标出每个字到来时状态机的 `state` 跳转。
3. 找到 [444-481 行](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressRxHsFsm.vhd#L444-L481) 的 `case r.hdrCnt`，确认 `hdrCnt=12,13,14` 装的是哪个字段（答案：`ySize`，它后来决定一帧多少行）。

**需要观察的现象**：
- 图像头的 25 个字里，`hdrCnt` 从 3 开始才装字段（0–2 是 marker/type/保留），到 25 装完 `flags`。
- `LINE_S` 里 `tKeep` 是逐槽设置的，不是整字——这保证行末不完整 beat 也能正确表达有效字节。
- `dCnt` 与 `hdr.dsizeL` 的比较决定行末，`yCnt` 与 `hdr.ySize` 的比较决定帧末。

**预期结果**：你能解释「为什么需要 `STEP_S`」——当一行的最后一个有效字落在一个多字 beat 的中间位置时，必须逐字步进搬完，才能让下一个 `CXP_MARKER_C` 对齐到字边界被正确识别。完整仿真运行需本地搭建 cocotb 测试台（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `HDR_S` 要检查「4 字节必须相同」？
**答**：CXP 标准规定图像头每个 32 位字的有效信息只在最低字节，高位是低字节的副本。接收端比较 4 字节是否一致即可在不开销额外校验位的情况下检测传输错误；不一致就判头损坏、丢弃整帧头。

**练习 2**：`RX_FSM_CNT_WIDTH_G` 设大（如 24）和设小（如 12）各有什么权衡？
**答**：它决定 `yCnt`/`dCnt` 的位宽，也就决定能支持的最大图像高度（`ySize`）与行长（`dsizeL`）。设大支持更大图像，但 `dCnt`/`yCnt` 比较器更宽、时序更紧；设小则反之。因此注释提示「按相机实际分辨率调到刚好够用」以帮助 `CoaXPressRxHsFsm` 收时序。

---

### 4.3 低速发送 FSM（CoaXPressTxLsFsm）

#### 4.3.1 概念说明

`CoaXPressTxLsFsm` 是低速上行链路的发动机。低速上行只有 20.833 Mbps（或 41.666 Mbps），要在这条窄带链路上**时分复用**三类报文：

1. **触发报文**（最高优先级）：6 字节，告诉相机触发。必须低延迟、低抖动。
2. **空闲报文**：4 字节的 IDLE 图案，维持链路锁定。
3. **配置报文**：来自 `CoaXPressConfig` 的寄存器读写字节流。

关键设计：低速链路的速率远低于 `txClk`（312.5 MHz），所以不可能每拍发一个字节，而要用一个**心跳计数器**（heartbeat）节拍——每个心跳周期才发一个 8B/10B 字节。

#### 4.3.2 核心流程

每个心跳周期做一件事，按固定优先级选择发什么：

```
每个 heartbeat (txStrobe=1):
   if 正在发触发报文 (txTrigCnt /= 6):       发触发报文的下一字节   ← 最高优先
   elsif 正在发空闲报文 (txIdleCnt /= 4):    发空闲报文的下一字节
   elsif cfgMaster.tValid 且当前不在空闲报文: 发配置报文的下一字节
   else:                                      开始一个新的空闲报文
```

**心跳周期**由 `txRate` 决定（[CoaXPressTxLsFsm.vhd:139-159](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressTxLsFsm.vhd#L139-L159)）：

- `txRate=0`（20.833 Mbps）：`heartbeatCnt = 149`，即每 150 个 `txClk` 周期发一个字节。
- `txRate=1`（41.666 Mbps）：`heartbeatCnt = 74`，即每 75 个 `txClk` 周期发一个字节。

为什么是这两个数？因为 8B/10B 把每字节编成 10 比特，于是字节周期 \(T_{\text{byte}} = 10 / R\)（R 为线速率）。心跳计数长度为：

\[
N = f_{\text{txClk}} \cdot T_{\text{byte}} - 1 = \frac{f_{\text{txClk}} \cdot 10}{R} - 1
\]

代入 \(f_{\text{txClk}} = 312.5\,\text{MHz}\)：\(R=20.833\,\text{Mbps} \Rightarrow N=149\)；\(R=41.666\,\text{Mbps} \Rightarrow N=74\)，与代码注释完全吻合。

**触发报文**用 K 字符标识两种触发类型：`LinkTrigger0`（K28.2 开头）与 `LinkTrigger1`（K28.4 开头），由 `txTrigInv` 选择，并对 rising-edge 触发与超时续发分别用正/反相。触发报文后 3 字节是 `TX_DLY_C` 查表得到的**触发延迟补偿值**——用来抵消电缆/光纤的传播时延，让多相机的触发时刻对齐。

#### 4.3.3 源码精读

**心跳节拍**：

[protocols/coaxpress/core/rtl/CoaXPressTxLsFsm.vhd:139-159](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressTxLsFsm.vhd#L139-L159)：`heartbeatCnt` 从预置值递减到 0 时置 `heartbeat := '1'`，并按 `txRate` 重装 149 或 74。注意 `heartbeat` 是单拍 strobe（每周期开头被清零，136 行）。

**触发检测与报文组装**：

[protocols/coaxpress/core/rtl/CoaXPressTxLsFsm.vhd:184-239](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressTxLsFsm.vhd#L184-L239)：检测 `txTrig` 上升沿（或脉宽定时器 `txTrigWidthCnt` 到 1）启动一次触发报文。若上一次触发报文还没发完又来新触发，置 `txTrigDrop`（告诉软件「这个触发被丢了」）——这是低速链路带宽有限的必然保护。触发延迟补偿表 `TX_DLY_C` 由函数 [53-61 行](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressTxLsFsm.vhd#L53-L61) `genTxDly` 在编译期生成，并用 `rom_style=distributed`、`rom_extract=TRUE`、`syn_keep` 属性（[112-117 行](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressTxLsFsm.vhd#L112-L117)）钉成分布式 ROM。

**优先级仲裁输出**：

[protocols/coaxpress/core/rtl/CoaXPressTxLsFsm.vhd:242-292](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressTxLsFsm.vhd#L242-L292)：仅当 `heartbeat='1'` 时才发字节，按「触发 > 空闲 > 配置 > 插空闲」优先级选 `txData/txDataK`。`CXP_TRIG_K_C = "000111"`（[63 行](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressTxLsFsm.vhd#L63)）标出触发报文 6 字节里哪些是 K 字符（前 3 字节）。配置字节则直接取 `cfgMaster.tData(7 downto 0)` 与 `tUser(0)`。

**它在系统里的位置**：`CoaXPressTx`（[CoaXPressTx.vhd:127-146](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressTx.vhd#L127-L146)）把配置流和事件 ACK 经 `AxiStreamMux` 合并、过一个 store-and-forward 的 `AxiStreamFifoV2` 跨到 `txClk` 域，再喂给 `CoaXPressTxLsFsm`。触发则由 `trigger <= txTrig or swTrig`（[148 行](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressTx.vhd#L148)）合并硬件触发与软件触发。

#### 4.3.4 代码实践

**目标**：验证心跳周期与低速线速率的换算关系。

**步骤**：
1. 读 [CoaXPressTxLsFsm.vhd:139-159](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressTxLsFsm.vhd#L139-L159)，记录两个预置值 149 与 74。
2. 用公式 \(N = f_{\text{txClk}} \cdot 10 / R - 1\)、\(f_{\text{txClk}}=312.5\,\text{MHz}\) 反推两个 R，确认得到 20.833 Mbps 与 41.666 Mbps。
3. 读 [242-292 行](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressTxLsFsm.vhd#L242-L292)，确认发字节的 `if (r.heartbeat = '1')` 门控——即没有心跳就不发字节。

**需要观察的现象**：
- `heartbeat` 是单拍脉冲，`heartbeatCnt` 是自由运行的递减计数器，二者解耦。
- 触发报文一旦开始（`txTrigCnt /= 6`），即使有更高频的配置数据到来也会被阻塞，直到 6 字节触发报文发完——保证触发完整性。
- `txTrigDrop` 只在「触发报文未发完又来新触发」时置 1。

**预期结果**：你能口头推出「在 20.83 Mbps 模式下，发一个 6 字节触发报文需要 6 × 150 = 900 个 `txClk` 周期 ≈ 2.88 µs」，这正是 CXP 触发的典型量级延迟。完整波形需本地仿真验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么配置报文的优先级低于空闲报文？
**答**：空闲报文维持链路 8B/10B 锁定与 comma 对齐，一旦长时间不发 IDLE，接收端可能失锁。配置报文是普通寄存器访问，可延迟。故协议把「正在发的空闲报文」优先级排在配置之前，但新空闲报文的「插入」又排在配置之后，兼顾了链路保活与配置吞吐。

**练习 2**：`TX_DLY_C` 为什么要做成 ROM 而非运行时计算？
**答**：触发延迟补偿值随心跳相位索引 `idx` 离线确定，运行时无需变化；做成编译期 ROM（并用 `distributed`/`syn_keep` 属性）既省去组合计算、改善时序，又保证不被综合优化掉。

---

### 4.4 Over-Fiber 桥接（CoaXPressOverFiberBridge）

#### 4.4.1 概念说明

原生 CoaXPress 跑在同轴电缆上：下行高速 + 上行低速共用一根缆，还兼供 电（Power-over-Coax）。但同轴的传输距离有限（通常几米）。**CXP Over Fiber**（标准 CXPR-008-2021）把 CXP 搬到 10GBASE-R 光纤上，换取更远距离与更高抗干扰。

`CoaXPressOverFiberBridge` 就是这个翻译器：一侧是 CXP 核心原生的 32 位 @312.5 MHz 接口（`txLsData`/`rxData` 等），另一侧是 10G 以太网的 XGMII 接口（64 位数据 + 8 位控制 @156.25 MHz）。它的全部工作就是：**改宽度、改时钟域、改帧格式**。

10GBASE-R 用自己的帧符（来自 [CoaXPressPkg.vhd:52-56](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressPkg.vhd#L52-L56)）：

| Over-Fiber 帧符 | 值 | 含义 |
|-----------------|----|----|
| `CXPOF_IDLE_C` | `0x07` | /I/ 空闲（nGMII IDLE） |
| `CXPOF_SEQ_C` | `0x9C` | /Q/ Sequence（仅 lane 0 有效） |
| `CXPOF_START_C` | `0xFB` | /S/ 起始（仅 lane 0 有效） |
| `CXPOF_TERM_C` | `0xFD` | /T/ 终止 |
| `CXPOF_ERROR_C` | `0xFE` | /E/ 错误 |

注意：下行每路 CXP lane 映射到一根独立光纤 lane，故 RX 桥在**所有 lane** 都启用；而上行低速链路只有一条、走 lane 0，故 TX 桥只在 lane 0 启用（由 `LANE0_G` 控制）。

#### 4.4.2 核心流程

桥顶层（[CoaXPressOverFiberBridge.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressOverFiberBridge.vhd)）的拼装：

```
RX 方向 (光纤 → CXP):
  XGMII(64b@156) ──[AsyncGearbox 64→32]──> 32b@312 ──[BridgeRx]──> CXP rxData/rxDataK

TX 方向 (CXP → 光纤, 仅 lane0):
  CXP txLs* ──[BridgeTx]──> 32b@312 ──[AsyncGearbox 32→64]──> XGMII(64b@156)
```

- 两个 `AsyncGearbox`（u2-l5 的位宽+跨域件）完成 64↔32 位宽度变换与 156.25 MHz↔312.5 MHz 时钟域跨越。
- `CoaXPressOverFiberBridgeRx` 把 XGMII 的 `/S/.../T/` 帧格式译回 CXP 的 `SOP/EOP/IDLE`，并区分普通数据包与 HKP（heartbeat/keep-alive）包。
- `CoaXPressOverFiberBridgeTx` 把 CXP 低速字节流装进 XGMII 的 `/S/`+载荷+`/T/` 帧，并把每字节前缀一个 LS-CTRL（`0x01`=数据、`0x02`=K 字符）。

#### 4.4.3 源码精读

**顶层宽度/时钟变换**：

[protocols/coaxpress/core/rtl/CoaXPressOverFiberBridge.vhd:63-81](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressOverFiberBridge.vhd#L63-L81) 是 RX 侧 `U_64bTo32b`：把 64 位 XGMII 数据 + 8 位控制（共 72 位）从 `rxClk156` 跨到 `rxClk312` 并拆成 32+4。`slaveRst => '0'`（异步 gearbox 内部自理复位）。

[protocols/coaxpress/core/rtl/CoaXPressOverFiberBridge.vhd:97-136](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressOverFiberBridge.vhd#L97-L136) 是 TX 侧，包在 `GEN_TX: if (LANE0_G = true) generate` 里——这正是「上行只走 lane 0」的硬件表达。

**RX 帧↔CXP 译码（3 状态机）**：

[protocols/coaxpress/core/rtl/CoaXPressOverFiberBridgeRx.vhd:43-46](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressOverFiberBridgeRx.vhd#L43-L46) 定义 `IDLE_S`/`HKP_S`/`PAYLOAD_S`。[87-131 行](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressOverFiberBridgeRx.vhd#L87-L131) 的 `IDLE_S` 等 `/S/`（`xgmiiRxc="0001"` 且首字节为 `CXPOF_START_C`），据次字节的控制位区分 HKP 包（`xgmiiRxd(8)='1'`）与普通数据包；若是数据包且第 3 字节是 `CXP_SOP_C` 的低字节，则向 CXP 侧发 `CXP_SOP_C`。[133-144 行](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressOverFiberBridgeRx.vhd#L133-L144) 的 `HKP_S` 把 XGMII 字透传，遇到 `CXP_EOP_C` 收尾。[146-177 行](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressOverFiberBridgeRx.vhd#L146-L177) 的 `PAYLOAD_S` 识别 `/T/` 终止并据此发 `CXP_EOP_C` 或 `CXP_IDLE_C`。

**TX CXP→帧编码（3 状态机）**：

[protocols/coaxpress/core/rtl/CoaXPressOverFiberBridgeTx.vhd:46-49](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressOverFiberBridgeTx.vhd#L46-L49) 定义 `IDLE_S`/`LS_SOP_S`/`LS_PAYLOAD_S`。[98-110 行](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressOverFiberBridgeTx.vhd#L98-L110) 的 `IDLE_S` 在 `txLsValid` 时锁存低速字节并进入 SOP。[112-147 行](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressOverFiberBridgeTx.vhd#L112-L147) 的 `LS_SOP_S` 构造 XGMII 起始字：lane0 放 `/S/`，lane1 的控制字节编码了「包类型=0（低速）、update 标志、`txLsRate`、高速上行连接状态」等信息。[149-224 行](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressOverFiberBridgeTx.vhd#L149-L224) 的 `LS_PAYLOAD_S` 每个字塞 2 个低速字节（各带 1 字节 LS-CTRL），发 2 拍后用 `/T/` 终止。未被 `txLsLaneEn` 选中的通道填 IDLE 字节。

**core/ 与家族 PHY 的拆分（ruckus）**：

[protocols/coaxpress/ruckus.tcl:4-25](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/ruckus.tcl#L4-L25) 无条件加载 `core/`，再用 `getFpgaArch` 选 PHY：`kintexu/virtexu → gthUs`、`kintexuplus/zynquplus/zynquplusRFSOC → gthUs+ 与 gtyUs+`、`virtexuplus/virtexuplusHBM → gtyUs+`。家族目录如 [gthUs+/ruckus.tcl:4-15](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/gthUs%2B/ruckus.tcl#L4-L15) 还用 `VIVADO_VERSION >= 2021.2` 门控，并把厂商 GT 的 `.dcp` 网表（`CoaXPressOverFiberGthUsIp.dcp`）接进工程。也就是说：本节讲的桥逻辑全在家族无关的 `core/`，而真正驱动光纤的 Xilinx GTH/GTY 收发器封装在家族目录里（`CoaxpressOverFiberGthUs.vhd` 等）。

#### 4.4.4 代码实践

**目标**：理解 Over-Fiber 桥如何把 CXP 的 SOP/EOP「翻译」成 XGMII 的 `/S/`、`/T/`。

**步骤**：
1. 对比两套帧符：CXP 侧 [CoaXPressPkg.vhd:30-36](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressPkg.vhd#L30-L36)（`CXP_SOP_C` 等）与 Over-Fiber 侧 [52-56 行](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressPkg.vhd#L52-L56)（`CXPOF_START_C` 等）。
2. 在 `CoaXPressOverFiberBridgeRx.vhd` 的 `IDLE_S`（[87-131 行](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/coaxpress/core/rtl/CoaXPressOverFiberBridgeRx.vhd#L87-L131)）里找到「检测到 `/S/` 且第 3 字节是 `CXP_SOP_C` 低字节 → 向 CXP 侧输出 `CXP_SOP_C`」的映射。
3. 在 `PAYLOAD_S` 里找到「检测到 `/T/` → 输出 `CXP_EOP_C`」的映射。

**需要观察的现象**：
- 桥是**双向独立**的：RX 桥把以太网帧符译成 CXP 帧符，TX 桥反向。
- RX 桥有一个 HKP（keep-alive）旁路状态，说明 Over-Fiber 在 CXP 业务帧之外还承载了链路保活包。
- TX 桥的 SOP 控制字节里携带了 `txLsRate`，说明对端可从帧头读出当前低速速率——这就是 Over-Fiber 把 CXP 的带外控制信息编码进以太网带内的方式。

**预期结果**：你能画出「CXP SOP → (TX 桥) → XGMII `/S/` → 光纤 → (RX 桥) → CXP SOP」的完整往返，并指出宽度变换（32↔64）与时钟变换（312↔156）发生在 `AsyncGearbox` 里。完整链路仿真需本地搭建（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `CoaXPressOverFiberBridge` 的 TX 部分包在 `if (LANE0_G = true) generate` 里，而 RX 部分不包？
**答**：下行（RX）每路 CXP lane 映射到一根独立光纤，所以每路都需要 RX 桥；上行（TX）低速链路在 CXP 里是单一共享链路，只走 lane 0，所以只有 lane 0 需要 TX 桥，其它 lane 用 `generate` 直接省掉这套逻辑。

**练习 2**：为什么家族 PHY 目录（`gthUs+`）的 `ruckus.tcl` 要门控 `VIVADO_VERSION >= 2021.2`？
**答**：因为那里引用了厂商 GT IP 的 `.dcp` 网表（`CoaXPressOverFiberGthUsIp.dcp`），该网表由特定版本 Vivado 生成，旧版本无法正确导入。`core/` 不依赖厂商原语故无此限制，可在任意版本（含 GHDL）下分析。这正体现了 u1-l3 讲的 `core/` 与家族 PHY 拆分的价值。

---

## 5. 综合实践

把本讲三个 FSM 串起来，做一次「端到端跟踪」：

**任务**：假设一台 CXP 相机经 Over-Fiber 光纤接到 FPGA，软件要触发一次曝光并接收一帧图像。请在纸上画出整个数据/控制通路，并标注每个 FSM 的工作。

**参考步骤**：
1. **触发下发**：软件写 AXI-Lite 触发寄存器 → `CoaXPressAxiL` 把 `swTrig` 送到 `txClk` 域 → `CoaXPressTx` 合并为 `trigger` → **`CoaXPressTxLsFsm`**（4.3）在下一个心跳节拍发出 6 字节触发报文 → `CoaXPressOverFiberBridgeTx`（4.4）把它装进 XGMII `/S/.../T/` 帧 → 经家族目录里的 GTH/GTY 收发器上光纤。
2. **图像上行**：相机收到触发，回传图像 → 光纤 → 收发器 → XGMII → `AsyncGearbox` 64→32 → `CoaXPressOverFiberBridgeRx`（4.4）把 `/S/.../T/` 译回 `CXP_SOP_C`/数据/`CXP_EOP_C` → `CoaXPressRxLane` 剥 SOP/EOP → `CoaXPressRxLaneMux` 合并 → **`CoaXPressRxHsFsm`**（4.2）解析 `CXP_MARKER_C` → 图像头 → 逐行像素 → `dataMaster`/`hdrMaster`。
3. **状态回报**：`RxHsFsm` 的 `rxFsmError`、各 lane 的 `rxLinkUp` 等经 `CoaXPressAxiL` 同步到 `axilClk`，软件可读。

**验收点**：你能指出——触发延迟由 4.3 的 `TX_DLY_C` 补偿；图像帧的「多少行」由 4.2 的 `hdr.ySize` 决定；定界的可靠性来自 4.1 的全 K 字符常量；跨域与跨介质由 4.4 的 gearbox 与帧符翻译完成。完整运行需本地硬件/仿真环境（待本地验证）。

## 6. 本讲小结

- CoaXPress 是**非对称双方向**接口：高速下行传图像、低速上行传触发与配置，故核心拆成 `CoaXPressRxHsFsm`（收）与 `CoaXPressTxLsFsm`（发）两个独立 FSM。
- 链路靠**全 K 字符的 32 位字**定界（`CXP_SOP_C`/`CXP_EOP_C`/`CXP_MARKER_C` 等），集中定义在 `CoaXPressPkg.vhd`；`CXP_IDLE_C` 含 K28.5 comma 用于接收端比特对齐。
- `CoaXPressRxHsFsm` 用 `IDLE_S→TYPE_S→HDR_S→LINE_S/STEP_S` 五态机把字节流结构化成「图像头 + 逐行像素」，`CXP_MARKER_C` 是图像层起点，`RX_FSM_CNT_WIDTH_G` 用于时序/分辨率权衡。
- `CoaXPressTxLsFsm` 用**心跳计数器**（149 或 74）节拍，按「触发 > 空闲 > 配置」优先级时分复用低速上行链路；触发报文带 `TX_DLY_C` 延迟补偿。
- `CoaXPressOverFiberBridge` 用两个 `AsyncGearbox` 完成 32↔64 位与 312↔156 MHz 变换，再用 `BridgeRx`/`BridgeTx` 两个状态机在 CXP 帧符与 XGMII 帧符间互译；RX 桥全 lane、TX 桥仅 lane 0。
- 协议逻辑全在家族无关的 `core/`，家族 PHY（GTH/GTY）封装在 `gthUs`/`gthUs+`/`gtyUs+` 目录，由 `ruckus.tcl` 用 `getFpgaArch` 与 `VIVADO_VERSION` 选择。

## 7. 下一步学习建议

- **往下钻收发器层**：读家族目录里的 `CoaxpressOverFiberGthUs.vhd` / `CoaXPressOverFiberGthUsIpWrapper.vhd`，看 Xilinx GTH 如何接到本讲的 XGMII 接口，这会补齐「光纤物理层」这一段。
- **横向对比其它高速链路协议**：本单元 u7-l1（PGP）、u7-l2（RSSI）、u7-l3（JESD204B）与 CXP 同属高速串行协议，对比它们的定界方式（CXP 用 K 字符、PGP3/4 用 BTF、JESD204B 用 K28.5 comma + ILAS）与可靠性机制（CXP 无重传、RSSI 有重传），能加深对「协议分层取舍」的理解。
- **补配置通道**：本讲只讲了 TxLsFsm 的「发」侧，配置报文如何组装在 `CoaXPressConfig.vhd`、事件 ACK 如何在 `CoaXPressEventAckMsg.vhd` 里用 `CXP_SOP_C`/`CXP_EOP_C` 拼包，建议作为延伸阅读。
- **回归测试**：参考 `protocols/coaxpress/core/tb/CoaXPressCrcTb.vhd`，了解 CXP 的 CRC-32（多项式 `0x04C11DB7`，与以太网同）如何在仿真中被验证。
