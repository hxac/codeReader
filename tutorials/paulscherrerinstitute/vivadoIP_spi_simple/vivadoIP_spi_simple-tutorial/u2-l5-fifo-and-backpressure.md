# FIFO 缓冲与背压机制

## 1. 本讲目标

学完本讲，读者应该能够：

- 说清 `spi_simple` 内部为什么需要**两个** FIFO（命令 FIFO 与响应 FIFO），以及它们如何把快速 AXI 总线和慢速 SPI 引擎解耦。
- 读懂两个 `psi_common_sync_fifo` 的实例化代码：宽度、深度、RAM 风格等 generic 如何取值，各端口连到哪些信号。
- 解释 `TxLevel` / `RxLevel`、`TxEmpty` / `TxFull` / `RxEmpty` / `RxFull` 这些标志如何被组合进 **Status 寄存器**，以及它们如何构成软件侧的「背压」。
- 理解 `CfgTxAlmEmpty` / `CfgRxAlmFull` 两个**运行时可配阈值**寄存器，以及 `<=` 与 `>=` 两种比较方向为何如此选择。
- 能够依据 `top_tb.vhd` 的「Fill FIFO and check status」场景，推演出 9 次 SPI 事务中每个状态位的期望取值。

本讲承接 u2-l2（核心架构与数据流），把镜头从「整体通路」推进到「FIFO 本身的配置、水位与阈值」。

## 2. 前置知识

在进入源码前，先用三段通俗的话把概念立起来。

**为什么要 FIFO？—— 速度匹配。** AXI 总线（例如 Zynq 的 ARM 核）可以在几个时钟周期内写一个寄存器；而一次 SPI 事务要按 `ClockDivider_g` 分频后的 SCK 逐 bit 移位，再加上 `CsHighCycles_g` 的片选间隔，往往需要几十甚至几百个时钟周期。如果让 AXI 主机「写一个命令 → 等 SPI 跑完 → 再写下一个」，CPU 就会被 SPI 拖着空转。FIFO 的作用就是在这两个速度悬殊的部件之间放一个**弹性缓冲**：AXI 把命令一股脑塞进命令 FIFO 就可以去干别的活，SPI 引擎按自己的节奏从 FIFO 里取命令执行。

**什么是背压（backpressure）？** 当下游来不及处理时，向上游「顶住」、不让上游继续灌数据的机制就叫背压。本 IP 里背压分两层：

1. **引擎侧（硬件自流控）**：SPI 引擎忙时不再从命令 FIFO 取下一条命令，这是天然的消费端节流。
2. **软件侧（建议性背压）**：命令 FIFO 的写入端口 `TxWrite` 在硬件上**没有**被 `TxFull` 门控住，AXI 永远能写。因此「写满前停手」的责任交给了软件——驱动要读 Status 寄存器的 `TxFull` 位，满了就等。

**almost empty / almost full 又是什么？** 「空」「满」是两个极端，等到真的空了或满了才反应往往太晚：空了引擎就要停转（浪费 SPI 带宽），满了就要丢数据。所以工程上常用「快空了」「快满了」两个**提前量**标志：当 FIFO 里剩余数据降到某个阈值以下（almost empty）或升到某个阈值以上（almost full）时就提前报警。本 IP 把这两个阈值做成**寄存器可配**，软件可以现场调整提前量。

> 本讲会用到 u2-l1 建立的寄存器地图（Status、IrqVec、各 `RegIdx_*` 索引）和 u2-l2 建立的双进程方法、命令字拼接等概念。

## 3. 本讲源码地图

| 文件 | 在本讲中的作用 |
| --- | --- |
| `hdl/spi_simple.vhd` | 核心。两个 FIFO 的实例化、水位/空满标志的组合、阈值的比较、引擎侧 SpiStart 流控，全部在这里。 |
| `hdl/spi_vivado_wrp.vhd` | 顶层 wrapper。把阈值寄存器读回、把 `TxLevel`/`RxLevel` 接到 AXI 可见的寄存器槽位。 |
| `tb/top_tb.vhd` | 仿真。其「Fill FIFO and check status」段是本讲代码实践的依据，也是验证阈值与水位逻辑的「金标准」。 |
| `hdl/definitions_pkg.vhd` | 常量。Status 各 bit、Irq 各 bit、寄存器索引的单一数据源（u2-l1 已建立）。 |
| `drivers/spi_simple/src/spi_simple.c` | 仅用于说明软件侧背压如何落地（非本讲主线，u2-l7 详讲）。 |

## 4. 核心概念与源码讲解

### 4.1 命令 FIFO 与响应 FIFO：配置与接口

#### 4.1.1 概念说明

`spi_simple` 内部有**两个独立的同步 FIFO**，都来自外部依赖 `psi_common`（见 u1-l3）中的 `psi_common_sync_fifo`：

- **命令 FIFO（TX 侧，实例 `i_tx_fifo`）**：AXI 写 `Data` 寄存器时，把「是否存 RX + 目标从机 + 待发数据」打包成一个**命令字**推入此 FIFO；SPI 引擎每完成一次事务再从中弹一条命令执行。它缓冲的是「待执行的 SPI 命令」。
- **响应 FIFO（RX 侧，实例 `i_resp_fifo`）**：SPI 引擎每次（在 `StoreRx=1` 时）把从 MISO 读回的数据写入此 FIFO；AXI 读 `Data` 寄存器时从中弹一个字。它缓冲的是「待软件取走的接收数据」。

二者一进一出，构成了 u2-l2 描述的「命令 FIFO → SPI 引擎 → 响应 FIFO」数据通路。

#### 4.1.2 核心流程

```text
┌─────────┐  TxWrite/TxData   ┌────────────┐  SpiStart(弹) ┌────────────┐
│  AXI    │ ───────────────▶  │ 命令 FIFO  │ ────────────▶ │ SPI 引擎   │
│ 写Data  │                   │  i_tx_fifo │               │ spi_master │
└─────────┘                   └────────────┘               └─────┬──────┘
                                                                 │ RxWrite(StoreRx & Done)
┌─────────┐  RxAck/读Data     ┌────────────┐  SpiRxData(写) ┌─────▼──────┐
│  AXI    │ ◀───────────────  │ 响应 FIFO  │ ◀────────────  │            │
│ 读Data  │                   │ i_resp_fifo│                └────────────┘
└─────────┘                   └────────────┘
```

- **写命令 FIFO**：`TxWrite`（来自 AXI 写 `Data` 寄存器的写脉冲，见 u2-l3）作为 `vld_i`，`CmdIn` 作为 `dat_i`。
- **弹命令 FIFO**：`r.SpiStart`（引擎启动脉冲，兼作 `rdy_i`）——一根线同时「弹命令」与「启动引擎」（详见 u2-l2）。
- **写响应 FIFO**：`r.RxWrite = r.StoreRx and SpiDone` 作为 `vld_i`，只读不写的事务不会污染响应 FIFO。
- **弹响应 FIFO**：`RxAck`（来自 AXI 读 `Data` 寄存器的读脉冲）作为 `rdy_i`。

#### 4.1.3 源码精读

命令字三段拼接的代码（u2-l2 已解释字段含义）：[`hdl/spi_simple.vhd:222-224`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L222-L224) —— 把粘性的 `CfgStoreRx`、`CfgSlave` 与 `TxData` 拼成 `CmdIn`，准备推入命令 FIFO。

命令 FIFO 的实例化：[`hdl/spi_simple.vhd:226-243`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L226-L243) —— 关键映射：

- `width_g => CmdIn'length`：宽度等于命令字总位宽（`StoreRx` 1 位 + `Slave` ⌈log2 SlaveCnt⌉ 位 + `Data` TransWidth 位）。
- `depth_g => FifoDepth_g`：深度由顶层 generic 决定，wrapper 默认 256，testbench 里设为 8。
- `ram_style_g => "auto"`、`ram_behavior_g => "RBW"`：让综合工具自动选择 BRAM/分布式 RAM，读写采用 Read-Before-Write。
- `vld_i => TxWrite`、`rdy_i => r.SpiStart`、`empty_o => TxEmpty`、`full_o => TxFull`、`out_level_o => TxLevel_I`。

响应 FIFO 的实例化：[`hdl/spi_simple.vhd:251-268`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L251-L268) —— 与命令 FIFO 结构对称，但有三个值得对照的点：

- `width_g => TransWidth_g`：只存纯数据，不带命令元信息。
- `vld_i => r.RxWrite`、`rdy_i => RxAck`：写由引擎驱动（且只在 `StoreRx=1` 时），读由 AXI 驱动。
- **水位端口方向不同**：命令 FIFO 用 `out_level_o`（读侧水位），响应 FIFO 用 `in_level_o`（写侧水位）。对同步 FIFO 二者数值相等，但方向选择体现了语义——TX 报「引擎还能消费多少」，RX 报「引擎已经生产多少」。这一点会在 4.3 节用到。

> 注意：`psi_common_sync_fifo` 是 `psi_common` 里的外部组件，本仓库不含其源码。它的内部实现（例如「写满再写」时是丢弃还是忽略）超出本讲范围，**待本地验证**（可到 `psi_common` 仓库查阅 `psi_common_sync_fifo.vhd`）。本讲只描述 `spi_simple` 如何通过端口契约使用它。

FIFO 相关端口在实体声明里的位置：[`hdl/spi_simple.vhd:64-70`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L64-L70)（`RxData/RxAck/RxLevel/TxData/TxWrite/TxLevel`），以及两个阈值输入端口：[`hdl/spi_simple.vhd:51-52`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L51-L52)（`CfgTxAlmEmpty` / `CfgRxAlmFull`）。

#### 4.1.4 代码实践

**实践目标**：用一张表把两个 FIFO 的 generic 与端口映射「钉死」，建立代码与概念的对应。

**操作步骤**：

1. 打开 [`hdl/spi_simple.vhd:226-268`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L226-L268)。
2. 仿照下表，把命令 FIFO 与响应 FIFO 的每一行 generic / 端口抄写成「FIFO 侧信号 → spi_simple 侧信号 / 含义」。

| 项 | 命令 FIFO `i_tx_fifo` | 响应 FIFO `i_resp_fifo` |
| --- | --- | --- |
| `width_g` | `CmdIn'length`（命令字） | `TransWidth_g`（纯数据） |
| `depth_g` | `FifoDepth_g` | `FifoDepth_g` |
| `dat_i` | `CmdIn` | `SpiRxData` |
| `vld_i`（写） | `TxWrite`（AXI 写 Data） | `r.RxWrite`（StoreRx & Done） |
| `rdy_i`（读） | `r.SpiStart`（弹+启动） | `RxAck`（AXI 读 Data） |
| `empty_o` | `TxEmpty` | `RxEmpty` |
| `full_o` | `TxFull` | `RxFull` |
| 水位 | `out_level_o => TxLevel_I` | `in_level_o => RxLevel_I` |

**需要观察的现象**：两个 FIFO 的 `depth_g` 都来自同一个 generic `FifoDepth_g`，意味着 TX 与 RX 容量永远相等；但 `width_g` 不同（命令字比数据宽）。

**预期结果**：能不看源码说出「AXI 写 Data 推命令 FIFO、AXI 读 Data 弹响应 FIFO」这组对称关系。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `FifoDepth_g` 从 256 改成 8，`TxLevel` 信号位宽会变吗？

**答案**：会变。`TxLevel` 的位宽是 `log2ceil(FifoDepth_g)+1`（见 [`hdl/spi_simple.vhd:67`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L67) 与 L70）。`FifoDepth_g=256` 时 `log2ceil(256)=8`，位宽 9 位（能表示 0..256）；`FifoDepth_g=8` 时 `log2ceil(8)=3`，位宽 4 位（能表示 0..8）。多出的一位正是为了能表示「满」（=深度本身）这个值。

**练习 2**：为什么命令 FIFO 报 `out_level_o`、响应 FIFO 报 `in_level_o`？

**答案**：这是语义标注。命令 FIFO 的消费者是 SPI 引擎，关心「读侧还有多少命令可取」，所以报 `out_level_o`；响应 FIFO 的生产者是 SPI 引擎，关心「写侧已经塞了多少、会不会满」，所以报 `in_level_o`。对同步 FIFO 二者数值相同，但对异步 FIFO 会有差异——这里的选择为将来换成异步 FIFO 留好了语义。

---

### 4.2 水位与空满标志：状态可见性与背压

#### 4.2.1 概念说明

FIFO 对外提供四类原始信号：`empty_o`、`full_o`、`level_o`（水位）、以及数据口。`spi_simple` 把这些原始信号**组合成 Status 寄存器的各个 bit**，让 AXI 软件用一个读操作就能看到 FIFO 全貌。这些 bit 同时承担两个职责：

1. **可见性**：软件查询 Status，判断能不能写 / 能不能读。
2. **背压落地**：软件（C 驱动）依据这些 bit 决定「等」还是「返回错误码」。

需要特别强调：**TX 写端口没有硬件背压**——AXI 写 `Data` 的脉冲 `TxWrite` 直接连到 FIFO 的 `vld_i`，中间**没有** `TxFull` 的门控。所以「满了别写」是软件契约。这一点直接决定了 C 驱动的 API 形态。

#### 4.2.2 核心流程

Status 寄存器在 `p_comb` 中**每个时钟周期重新清零再置位**（组合输出，反映瞬时状态）：

```text
每个 Clk 上升沿，p_comb 计算 r_next.Status：
  v.Status := (others => '0')          -- 先全清
  if TxEmpty  = '1' then Status[TxEmpty]   := '1'   -- 来自命令 FIFO empty_o
  if TxFull   = '1' then Status[TxFull]    := '1'   -- 来自命令 FIFO full_o
  if TxLevel <= 阈值 then Status[TxAlmEmpty] := '1' -- 见 4.3
  if RxFull   = '1' then Status[RxFull]    := '1'   -- 来自响应 FIFO full_o
  if RxEmpty  = '1' then Status[RxEmpty]   := '1'   -- 来自响应 FIFO empty_o
  if RxLevel >= 阈值 then Status[RxAlmFull]  := '1' -- 见 4.3
  -- Busy 单独由 (TxEmpty=0) or (SpiBusy=1) 决定
```

Status bit 到符号的映射在 [`hdl/definitions_pkg.vhd:36-43`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/definitions_pkg.vhd#L36-L43)：

| bit | 符号 | 含义 | 来源 |
| --- | --- | --- | --- |
| 0 | `BitIdx_Status_TxEmpty_c` | 命令 FIFO 空 | `TxEmpty`（FIFO `empty_o`） |
| 1 | `BitIdx_Status_TxFull_c` | 命令 FIFO 满 | `TxFull`（FIFO `full_o`） |
| 2 | `BitIDx_Status_TxAlmEmpty_c` | 命令 FIFO 快空 | 阈值比较（4.3） |
| 3 | `BitIdx_Status_RxEmpty_c` | 响应 FIFO 空 | `RxEmpty`（FIFO `empty_o`） |
| 4 | `BitIdx_Status_RxFull_c` | 响应 FIFO 满 | `RxFull`（FIFO `full_o`） |
| 5 | `BitIdx_Status_RxAlmFull_c` | 响应 FIFO 快满 | 阈值比较（4.3） |
| 6 | `BitIdx_Status_Busy_c` | 引擎忙或命令 FIFO 非空 | `(not TxEmpty) or SpiBusy` |

**引擎侧的硬件自流控**（消费端背压）藏在 `SpiStart` 的产生条件里：[`hdl/spi_simple.vhd:131-136`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L131-L136) —— 只有当 `SpiBusy='0'`（引擎闲）且 `TxEmpty='0'`（有命令可取）且上一拍没有 already 发启动脉冲时，才置 `SpiStart='1'`。这保证引擎**每完成一次事务才取下一条命令**，是 FIFO 不被一次抽干的根本原因。

#### 4.2.3 源码精读

Status 的组合生成：[`hdl/spi_simple.vhd:141-172`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L141-L172)。注意第 142 行 `v.Status := (others => '0');` 每拍先清零，所以 Status 是**电平型**（条件消失即回落），与锁存的 IrqVec（u2-l6）截然不同。

`Busy` 的生成是关键：[`hdl/spi_simple.vhd:181-186`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L181-L186) —— `Busy = (TxEmpty='0') or SpiBusy='1'`。也就是说，只要命令 FIFO 还有待执行命令（哪怕引擎此刻闲），Busy 就为 1。这给软件提供了一个简单的「全部发完了吗」判据。

Status / 水位的对外输出：[`hdl/spi_simple.vhd:195-199`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L195-L199)（`Status <= r.Status`、`TxLevel <= TxLevel_I`、`RxLevel <= RxLevel_I`）。

软件侧背压如何落地（仅作佐证，详讲见 u2-l7）：C 驱动的非阻塞发送会先查 `TxFull`，满了就返回错误码而不写——[`drivers/spi_simple/src/spi_simple.c:92-98`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/src/spi_simple.c#L92-L98)；阻塞发送则在 `TxFull` 上自旋等待——[`drivers/spi_simple/src/spi_simple.c:46-50`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/src/spi_simple.c#L46-L50)。这就是「TX 无硬件背压、由软件兜底」的直接证据。

#### 4.2.4 代码实践

**实践目标**：把「Status bit → FIFO 原始信号 → 软件动作」这条链走通。

**操作步骤**：

1. 阅读 [`hdl/spi_simple.vhd:148-168`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L148-L168)，确认 `TxEmpty/TxFull/RxEmpty/RxFull` 四个标志都只是把 FIFO 的 `empty_o/full_o` 直接搬到 Status 对应 bit，没有任何额外条件。
2. 阅读 [`drivers/spi_simple/src/spi_simple.c:147-165`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/src/spi_simple.c#L147-L165)，确认 `SpiSimple_IsTxFifoFull` / `SpiSimple_IsRxFifoEmpty` 等函数就是读 Status 寄存器再按位与。
3. 回答下面的问题。

**需要观察的现象 / 预期结果**：Status 是电平型（实时反映 FIFO），而 IrqVec 是锁存型（粘住直到清除）。同一条 `TxEmpty='1'` 条件，在 Status 里是「当前空」，在 IrqVec 里是「曾经空过」。

**思考题（待本地验证）**：假如软件无视 `TxFull`，连续向命令 FIFO 写第 `FifoDepth+1` 个命令，硬件会怎样？——按本仓库代码，`TxWrite` 不被门控，多出的写会进入 `psi_common_sync_fifo` 的「写满再写」分支，其具体行为（丢弃 / 忽略 / 覆盖）取决于 `psi_common` 实现，**待本地验证**。这正是软件必须遵守背压的原因。

#### 4.2.5 小练习与答案

**练习 1**：`Busy` 为 1 是否意味着 SPI 引擎此刻正在移位？

**答案**：不一定。`Busy = (TxEmpty='0') or SpiBusy`（[`hdl/spi_simple.vhd:182`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L182)）。即使引擎本身已空闲（`SpiBusy='0'`），只要命令 FIFO 里还有未执行命令（`TxEmpty='0'`），Busy 仍为 1。Busy 表达的是「IP 还有没干完的活」，比「引擎正在移位」更宽。

**练习 2**：为什么 `TxFull` 只进 Status，却**不**用来门控 `TxWrite`？

**答案**：因为 AXI 从接口一旦接受写事务就必须按协议完成（给 B 通道响应），无法在硬件上把一次 AXI 写「挡回去」。所以设计选择是：AXI 永远接受写，把「别写满」的责任交给软件（读 `TxFull` 自旋或返回错误码）。这是 AXI 寄存器型 IP 的常见取舍。

---

### 4.3 Almost Empty / Almost Full 阈值机制

#### 4.3.1 概念说明

`TxEmpty` / `TxFull` 这些「硬空满」标志粒度太粗。本 IP 另提供**两个运行时可配的阈值寄存器**：

- `CfgTxAlmEmpty`（寄存器 `RegIdx_TxAlmEmptyLevel_c`，地址 0x18）：命令 FIFO 的「快空」阈值。
- `CfgRxAlmFull`（寄存器 `RegIdx_RxAlmFullLevel_c`，地址 0x1C）：响应 FIFO 的「快满」阈值。

二者在 [`hdl/spi_simple.vhd:51-52`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L51-L52) 声明，宽度与水位一致：`log2ceil(FifoDepth_g)+1` 位。

软件通过 AXI 写这两个寄存器即可现场调整提前量，无需重新综合。C 驱动提供 `SpiSimple_SetTxAlmEmptyThreshold` / `SpiSimple_SetRxAlmFullThreshold` 封装（[`drivers/spi_simple/src/spi_simple.c:205-213`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/src/spi_simple.c#L205-L213)）。

#### 4.3.2 核心流程与原理

两个阈值的比较方向**故意相反**，分别贴合「快空」与「快满」的直觉：

\[
\text{TxAlmEmpty 有效} \iff \text{TxLevel} \leq \text{CfgTxAlmEmpty}
\]

\[
\text{RxAlmFull 有效} \iff \text{RxLevel} \geq \text{CfgRxAlmFull}
\]

- 「快空」看的是**消费侧**（命令 FIFO 还剩多少可取）：剩余少了就报警，所以用 `<=`，阈值 `CfgTxAlmEmpty` 是「剩余条数的下限」。
- 「快满」看的是**生产侧**（响应 FIFO 已收了多少）：攒得多了就报警，所以用 `>=`，阈值 `CfgRxAlmFull` 是「已收条数的下限」。

注意符号：`\leq` 与 `\geq` 都是**含等号**的比较（VHDL 里 `<=` 作为比较运算符与赋值符号同形，但在 `unsigned` 比较上下文中是比较）。这意味着阈值设为 N 时，水位恰好等于 N 也会触发。

阈值一旦满足，会**同时**做两件事：

1. 置位 Status 的对应 bit（电平型，实时）。
2. 锁存 IrqVec 的对应 bit（粘性，直到软件清除——u2-l6 详讲）。

阈值寄存器本身的读回：wrapper 把 `reg_wdata` 回环到 `reg_rdata`，属于 u2-l3 讲过的「配置类回环读回」——[`hdl/spi_vivado_wrp.vhd:209-210`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L209-L210)。水位的 AXI 可见连接在同一文件 [`hdl/spi_vivado_wrp.vhd:252-255`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L252-L255)。

#### 4.3.3 源码精读

阈值比较的核心两段：

- Almost Empty：[`hdl/spi_simple.vhd:155-158`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L155-L158) —— `if unsigned(TxLevel_I) <= unsigned(CfgTxAlmEmpty) then` 同时置 Status bit 2 与锁存 IrqVec bit 1。
- Almost Full：[`hdl/spi_simple.vhd:169-172`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L169-L172) —— `if unsigned(RxLevel_I) >= unsigned(CfgRxAlmFull) then` 同时置 Status bit 5 与锁存 IrqVec bit 4。

注意 `unsigned()` 类型转换：`TxLevel_I` / `CfgTxAlmEmpty` 都是 `std_logic_vector`，不能直接用数值比较，必须先转 `unsigned` 当无符号数比。这也是为什么阈值是**数值**语义而非位掩码。

IrqVec 各 bit 的索引常量定义在 [`hdl/definitions_pkg.vhd:24-30`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/definitions_pkg.vhd#L24-L30)：`Irq_TxEmpty_c=0`、`Irq_TxAlmEmpty_c=1`、`Irq_TfDone_c=2`、`Irq_RxFull_c=3`、`Irq_RxAlmFull_c=4`。

阈值寄存器到 `spi_simple` 端口的映射在 wrapper：[`hdl/spi_vivado_wrp.vhd:236-237`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L236-L237)，即把 AXI 写入的 `reg_wdata` 截取 `log2ceil(FifoDepth_g)+1` 位送给 `CfgTxAlmEmpty` / `CfgRxAlmFull`。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：依据 `top_tb.vhd` 的「Fill FIFO and check status」段，推演 9 次 SPI 事务中各状态位的期望取值，从而把 4.1～4.3 的概念全部串起来。

**场景设置**（请先打开 [`tb/top_tb.vhd:207-271`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L207-L271) 对照）：

- DUT 配置：`FifoDepth_g => 8`、`TransWidth_g => 8`、`ClockDivider_g => 20`、`CsHighCycles_g => 50`（见 [`tb/top_tb.vhd:86-99`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L86-L99)）。
- 阈值设置：`TxAlmEmpty = 3`、`RxAlmFull = 2`（[`tb/top_tb.vhd:214-215`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L214-L215)）。
- 一次性把 9 条命令灌进命令 FIFO（[`tb/top_tb.vhd:221-225`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L221-L225)），其中 `StoreRx = i mod 2`（奇数 i 存 RX、偶数 i 只写），`Data = i`。
- 之后循环 i=1..9，每次在「本事务完成前」与「完成后」各读一次 Status / 水位并比对（[`tb/top_tb.vhd:228-270`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L228-L270)）。

**关键直觉：为什么 9 条命令能塞进深度 8 的 FIFO 而不丢数据？** 因为 AXI 写入比 SPI 执行快得多。9 次 AXI 单写总共只要一两百个 `Clk`，而 SPI 引擎一次事务要 `ClockDivider_g × 移位周期 + CsHighCycles_g` ≈ 数百个 `Clk`。所以在全部 9 条命令写完时，引擎**只来得及弹出并执行第 1 条**（`SpiStart` 在引擎空闲的第一拍就发了），FIFO 里恰好剩 8 条（即「满」）。这就是 FIFO 解耦 + 软件背压的价值：生产端可以短暂地领先消费端，但不会溢出。

**推导水位公式**。设 `i` 为当前事务编号（1..9），令 `TxLevel_before(i)` / `TxLevel_after(i)` 为第 `i` 次事务「完成前 / 完成后」的命令 FIFO 水位，RX 同理。由「写满 9 条、引擎按序每事务弹 1 条」可得：

\[
\text{TxLevel\_before}(i) = 9 - i
\]

\[
\text{TxLevel\_after}(i) = \max(9 - i - 1,\ 0)
\]

RX 水位取决于「在此之前有多少条 `StoreRx=1` 的事务完成」。奇数 i 才存 RX，故 1..i-1 中奇数个数为 `i/2`（整除），1..i 中为 `(i+1)/2`：

\[
\text{RxLevel\_before}(i) = \lfloor i/2 \rfloor,\qquad
\text{RxLevel\_after}(i) = \lfloor (i+1)/2 \rfloor
\]

**由水位推出各状态位**（阈值 TxAlmEmpty=3、RxAlmFull=2，FifoDepth=8）：

| 状态位 | 完成前（关于 i 的条件） | 完成后（关于 i 的条件） |
| --- | --- | --- |
| `TxEmpty` | `9-i == 0` → i=9 | `max(8-i,0)==0` → i≥8 |
| `TxFull` | `9-i == 8` → i=1 | 永不成立（写阶段已结束） → 0 |
| `TxAlmEmpty` | `9-i ≤ 3` → i≥6（即 i≥9−3） | `max(8-i,0) ≤ 3` → i≥5（即 i≥8−3） |
| `RxEmpty` | `i/2 == 0` → i=1 | `(i+1)/2 ≥ 1` → 恒 0 |
| `RxFull` | 永不（最多 4 条 < 8） → 0 | 永不 → 0 |
| `RxAlmFull` | `i/2 ≥ 2` → i≥4 | `(i+1)/2 ≥ 2` → i≥3 |
| `Busy` | 恒 1（还有事要干） | i=9 时为 0，否则 1 |

把上表与 testbench 的 `choose(...)` 断言逐一对照（[`tb/top_tb.vhd:234-240`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L234-L240) 为「完成前」，[`tb/top_tb.vhd:259-265`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L259-L265) 为「完成后」），可见完全吻合，例如：

- `StdlCompare(choose(i>=9-3,1,0), ..., TxAlmEmpty)` ↔ `9-i ≤ 3`；
- `StdlCompare(choose(i>=4,1,0), ..., RxAlmFull)` ↔ `i/2 ≥ 2`。

**抽样验算**（请读者自行填入）：

| i | StoreRx | TxLevel 前/后 | RxLevel 前/后 | TxAlmEmpty 前/后 | RxAlmFull 前/后 | Busy 前/后 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 1 | 8 / 7 | 0 / 1 | 0 / 0 | 0 / 0 | 1 / 1 |
| 4 | 0 | 5 / 4 | 2 / 2 | 0 / 0 | 1 / 1 | 1 / 1 |
| 6 | 0 | 3 / 2 | 3 / 3 | 1 / 1 | 1 / 1 | 1 / 1 |
| 9 | 1 | 0 / 0 | 4 / 5 | 1 / 1 | 1 / 1 | 1 / 0 |

**操作步骤**：

1. 打开 [`tb/top_tb.vhd:207-271`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L207-L271)，逐行确认上表的「前/后」断言。
2. 用上表的公式手算 i=2、3、5、7、8 的取值，与 testbench 的 `choose(...)` 表达式核对。
3.（可选，待本地验证）按 u1-l4 的方法在 sim 目录跑回归：`source ./run.tcl`，确认这段断言全部通过。

**预期结果**：手算结果与 testbench 断言一致；i=4 那一行能清楚看到「`StoreRx=0` 的事务不改变 RxLevel」——这正是 u2-l2 讲的 `RxWrite = StoreRx and SpiDone` 在水位上的体现。

#### 4.3.5 小练习与答案

**练习 1**：若把 `CfgRxAlmFull` 从 2 改成 4，「完成后 RxAlmFull」的条件会变成什么？

**答案**：变成 `(i+1)/2 ≥ 4`，即 `i+1 ≥ 8`，即 `i ≥ 7`。所以 testbench 中对应的断言应改写为 `choose(i>=7, 1, 0)`。这也说明阈值是**纯软件可调**的，不需要改 RTL。

**练习 2**：`TxAlmEmpty` 与 `TxEmpty` 的关系是什么？阈值设为 0 时会如何？

**答案**：`TxEmpty` 是 FIFO 硬件 `empty_o`（水位为 0）；`TxAlmEmpty` 是 `TxLevel ≤ CfgTxAlmEmpty`。若把 `CfgTxAlmEmpty` 设为 0，则 `TxAlmEmpty` 退化为「`TxLevel ≤ 0`」，与 `TxEmpty` 完全等价。可见 `TxEmpty` 是 `TxAlmEmpty` 在阈值=0 时的特例。

**练习 3**：为什么 testbench 在每次事务完成后要写一次 `RegIdx_IrqVec_c ← 0xFF`（[`tb/top_tb.vhd:251`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L251)），但下一轮检查时 `Irq_TxAlmEmpty` / `Irq_RxAlmFull` 位又「自动」为 1？

**答案**：写 0xFF 是按位清除 IrqVec（u2-l6 详讲）。但这些位对应的电平条件（`TxLevel ≤ 阈值` / `RxLevel ≥ 阈值`）仍然成立，于是清除后立刻被 p_comb 重新锁存为 1。这就是「条件持续有效的中断位清除后会自动重置」的现象，细节留给 u2-l6。

## 5. 综合实践

把本讲三块内容串成一个端到端的小任务：

**任务**：假设你要用这个 IP 连续向某 SPI 从机发送 12 个字并读回 12 个字，`FifoDepth_g=8`。请回答：

1. 你能否一次性把 12 个「读事务」命令全部写入命令 FIFO？为什么？参考 4.3.4 的「速度差」直觉给出定性判断。
2. 写一个最小的 C 调用序列（使用 u2-l7 / [`drivers/spi_simple/src/spi_simple.c`](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/drivers/spi_simple/src/spi_simple.c) 的 API），安全地发完这 12 个读事务并取回 12 个接收字。要求：处理 `SpiSimple_TxFifoFull` 与 `SpiSimple_RxFifoEmpty` 返回码。
3. 如果想让 CPU 在命令 FIFO「快空」时被中断、从而及时续投命令，应配置哪两个寄存器、调用哪两个驱动函数？参考 4.3.1。

**参考思路**：

1. 不能盲目一次写 12 条。深度只有 8，若 SPI 引擎在写期间来不及消耗足够的命令，第 9 条之后的写入会落到「写满再写」分支而**可能丢数据**（具体行为待本地验证）。正确做法是软件遵守背压：写前查 `TxFull`，满了就等（阻塞 API 内部已做）或重试（非阻塞 API 返回 `SpiSimple_TxFifoFull`）。
2. 用循环 + 非阻塞 API：每次 `SpiSimple_RxTxNonBlocking`，若返回 `SpiSimple_TxFifoFull` 就稍后重试；发完后用 `SpiSimple_GetRxData` 循环取 12 个字，遇到 `SpiSimple_RxFifoEmpty` 就等。或更简单地直接用阻塞 API `SpiSimple_RxTxBlocking`（它内部已处理背压），但要注意它要求调用前 RX FIFO 为空。
3. 写 `RegIdx_TxAlmEmptyLevel_c`（0x18）设一个合适的「快空」阈值（例如 2），并通过 `SpiSimple_SetTxAlmEmptyThreshold` 设置；再写 `RegIdx_IrqEna_c`（0x24）使能 `Irq_TxAlmEmpty_c` 位（用 `SpiSimple_SetIrqEna`）。这样命令 FIFO 水位降到阈值时就会触发 `Irq`（中断细节见 u2-l6）。

## 6. 本讲小结

- `spi_simple` 用两个 `psi_common_sync_fifo` 解耦 AXI 与 SPI：命令 FIFO 缓冲「待执行命令」，响应 FIFO 缓冲「待取走的接收数据」，二者深度都等于 `FifoDepth_g`。
- 引擎侧的硬件自流控藏在 `SpiStart` 的产生条件里——引擎闲且有命令才弹一条，保证 FIFO 按事务节奏消费。
- TX 写端口**没有**硬件背压，AXI 永远能写；「写满前停手」是软件契约，由 C 驱动读 `TxFull` 自旋或返回错误码实现。
- Status 寄存器是电平型，实时反映 `TxEmpty/TxFull/RxEmpty/RxFull` 等原始标志；`Busy` 还额外涵盖「命令 FIFO 非空」。
- `CfgTxAlmEmpty` / `CfgRxAlmFull` 是运行时可配阈值，用 `TxLevel ≤ 阈值` 与 `RxLevel ≥ 阈值` 两种相反方向的比较产生「快空 / 快满」，既置 Status bit 又锁存 IrqVec bit。
- testbench 的「Fill FIFO and check status」段用 9 条命令灌深度 8 的 FIFO，验证了水位公式与各状态位取值，是理解本讲的金标准。

## 7. 下一步学习建议

- **u2-l6 中断向量与状态机制**：本讲多次提到 IrqVec 的锁存与「清除后自动重置」，其完整机制（按位清除、条件持续时自动重置、IrqEna 使能与 Irq 聚合输出）在下一讲展开。
- **u2-l7 C 驱动软件接口**：本讲的软件侧背压（`SpiSimple_IsTxFifoFull` 自旋、`SpiSimple_TxFifoFull` 错误码、阈值设置函数）会在驱动层完整讲解。
- **u2-l8 测试平台结构**：若想更深理解 4.3.4 的断言是如何被 AXI BFM 驱动执行的，可读 testbench 的 `p_control` 与 `p_spi` 进程结构。
- **进阶（u3-l1）**：`FifoDepth_g`、`TransWidth_g` 等 generic 如何在 Vivado GUI 中暴露为可配参数，将在专家层的参数化讲义中讲解。
