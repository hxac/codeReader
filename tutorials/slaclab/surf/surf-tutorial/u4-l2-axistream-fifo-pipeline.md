# u4-l2 AXI-Stream FIFO、流水与位宽调整

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `AxiStreamFifoV2` 是怎样把「带帧边界的 AXI-Stream 数据流」安全地缓存、跨时钟域、并可在两侧用不同总线宽度对接的。
- 看懂 `AxiStreamPipeline` 如何用零延迟直通或多级 skid 流水寄存器切割组合路径，从而改善时序（timing closure）。
- 理解 `AxiStreamResize` 如何在做「窄↔宽」位宽变换的同时保持 `tKeep/tStrb/tLast/tDest/tUser` 的语义一致。
- 能够独立阅读这三个文件的源码，并能动手跑一个 cocotb 回归测试观察其行为。

本讲承接 u4-l1 建立的 `AxiStreamMasterType/SlaveType` 记录与 `AxiStreamConfigType` 配置，把「描述一条流」推进到「搬运、缓冲、整形一条流」。

---

## 2. 前置知识

在进入源码前，先回顾三个本讲反复用到的概念（细节见 u4-l1）。

### 2.1 记录化总线与「最大宽度」约定

SURF 把一条 AXI-Stream 流折叠成两个记录（[AxiStreamPkg.vhd:30-61](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L30-L61)）：

- `AxiStreamMasterType`：生产方驱动，含 `tValid` 与 `tData/tStrb/tKeep/tLast/tDest/tId/tUser`。
- `AxiStreamSlaveType`：消费方驱动，仅含 `tReady`。

关键字段全部按仓库最大宽度声明（`tData` 与 `tUser` 都是 `AXI_STREAM_MAX_TDATA_WIDTH_C` 位，`tKeep/tStrb` 是 `AXI_STREAM_MAX_TKEEP_WIDTH_C` 字节），所以任意两条流类型相同、可直接相连；真实位宽由编译期常量 `AxiStreamConfigType`（[AxiStreamPkg.vhd:82-91](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L82-L91)）描述。这套「记录统一宽、配置定真宽」的设计，是本讲三个模块能复用同一套存储/搬运动作的根基。

### 2.2 tValid/tReady 握手与帧边界

AXI-Stream 是单向握手：一拍数据当且仅当 `tValid='1'` 且 `tReady='1'` 时被「成交（handshake）」。一帧由若干拍组成，`tLast='1'` 标记帧的最后一拍。本讲所有模块都必须做到两点：

1. 不得丢拍、不得乱序（背压下要能 stall）。
2. 帧的 `tLast`、以及挂在 `tUser` 上的侧带（如 SSI 的 SOF/EOF/EOFE，见 u5-l1）要随对应的那一拍原样传过去。

### 2.3 FIFO 与双进程骨架

本讲的 FIFO 内部复用 u2-l2 的 `FifoCascade`（Gray 指针异步 FIFO）与 u1-l5 的双进程风格（`RegType`/`REG_INIT_C`/`r`/`rin`/`comb`/`seq`）。如果你对这些还不熟，建议先翻 u2-l2 与 u1-l5。

---

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [axi/axi-stream/rtl/AxiStreamFifoV2.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamFifoV2.vhd) | 流式 FIFO 的「门面」：两侧位宽协商 + 异步/同步 FIFO + 帧门控 + 输出流水，全仓库 AXI-Stream 缓存的事实标准。 |
| [axi/axi-stream/rtl/AxiStreamPipeline.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPipeline.vhd) | 纯流水/skid 寄存器，`PIPE_STAGES_G=0` 时零延迟直通，>0 时多级寄存并自动消除内部空洞。 |
| [axi/axi-stream/rtl/AxiStreamResize.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamResize.vhd) | 窄↔宽位宽变换器，小端对齐，逐字节搬运 `tKeep/tStrb/tUser`，并在末级挂一个可选 `AxiStreamPipeline`。 |
| [axi/axi-stream/rtl/AxiStreamPkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd) | 记录、`AxiStreamConfigType`，以及把记录打包成扁平 `slv` 的 `toSlv/toAxiStreamMaster/getSlvSize`（FIFO 存储用）。 |
| [axi/axi-stream/rtl/AxiStreamGearbox.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamGearbox.vhd) | 被 `AxiStreamFifoV2` 在两侧各例化一次，做进/出 FIFO 前后的位宽整形。 |
| [base/fifo/rtl/FifoCascade.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/FifoCascade.vhd) | 底层可级联异步/同步 FIFO，承载真正的存储体（来自 u2-l2）。 |

---

## 4. 核心概念与源码讲解

### 4.1 AXI-Stream FIFO（AxiStreamFifoV2）

#### 4.1.1 概念说明

`AxiStreamFifoV2` 是 SURF 里用得最多的流缓冲器。你可以把它理解为一个「智能的水箱」：

- **两侧时钟可以不同**（`sAxisClk` 写侧、`mAxisClk` 读侧），内部用异步 FIFO 跨时钟域；设 `GEN_SYNC_FIFO_G=true` 则退化为同步 FIFO（省一套 Gray 指针同步）。
- **两侧总线宽度可以不同**：比如写侧 8 字节、读侧 16 字节。它在进/出 FIFO 的两端各放一个 `AxiStreamGearbox` 做整形，让 FIFO 本体只用一套「协商后的宽度」存数据。
- **它懂「帧」**：通过 `VALID_THOLD_G` 可以让输出 `tValid` 只在「攒够 N 拍」或「一帧到齐」时才拉起，用于帧级门控；并配套一个独立的「tLast 侧带 FIFO」做超前窥视。

它还顺带提供流控背压（`pause`/`overflow`/`idle` 三件套，类型 `AxiStreamCtrlType`，见 [AxiStreamPkg.vhd:115-124](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L115-L124)），以及末级输出流水寄存器（`PIPE_STAGES_G`），所以很多地方例化它就同时拿到了「缓冲 + 位宽适配 + 时序优化」三件事。

#### 4.1.2 核心流程

`AxiStreamFifoV2` 内部是一条「四级流水」：

```text
sAxisMaster ──► [SlaveResize(Gearbox)] ──► fifoWriteMaster
                                                  │  toSlv() 打包成 FIFO_BITS_C 宽的扁平向量
                                                  ▼
                                          ┌─────────────────┐
                                          │  FifoCascade    │  异步/同步存储体
                                          │  (+可选 tLast   │  FWFT、可配深度、可级联
                                          │   侧带 FIFO)    │
                                          └─────────────────┘
                                                  │  toAxiStreamMaster() 还原成记录
                                                  ▼
fifoReadMaster ──► [MasterResize(Gearbox)] ──► axisMaster ──► [AxiStreamPipeline] ──► mAxisMaster
                                                  ▲
                              VALID_THOLD_G 门控 fifoValid（帧/块级有效）
```

要点：

1. **宽度协商**：`FIFO_CONFIG_C` 在编译期取两侧配置的「较宽/较窄/自定义」数据宽度，以及较窄的 `TDEST/TID/TUSER` 宽度。
2. **打包/解包**：进出 FIFO 用 `toSlv`/`toAxiStreamMaster` 把记录与扁平 `slv` 互转，存储宽度 `FIFO_BITS_C = getSlvSize(FIFO_CONFIG_C)`。
3. **背压**：写侧 `tReady` 来自 `not fifoAFull`；当 `fifoWrCount >= fifoPauseThresh` 时拉 `sAxisCtrl.pause`，供上游做更早的流控。
4. **帧门控**：`VALID_THOLD_G=1`（默认）时 `fifoValid = fifoValidInt`（逐拍正常输出）；`VALID_THOLD_G/=1` 时由一个状态机 `fifoInFrame` 决定何时放行，并维护一个独立的 tLast 侧带 FIFO。

深度与有效容量的关系与 u2-l2 一致：地址宽度 `FIFO_ADDR_WIDTH_G` 的 FIFO 有效容量为 \(2^{N}-1 \) 拍（留一个「满/空区分位」）。

#### 4.1.3 源码精读

**(1) 泛型分四组**：通用/复位、帧门控、FIFO 存储体、两侧配置。[AxiStreamFifoV2.vhd:28-68](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamFifoV2.vhd#L28-L68)。几个高频项：

- `INT_PIPE_STAGES_G` / `PIPE_STAGES_G`：前者是 FIFO 内部输出流水，后者是末级 `AxiStreamPipeline` 的级数（默认 1）。
- `VALID_THOLD_G`：`=1` 正常逐拍；`=0` 仅在帧到齐时输出；`>1` 攒够若干拍或帧尾才输出（`VALID_BURST_MODE_G` 控制是否突发）。注释明确提示「interleaved tdest 时必须为 1」。
- `INT_WIDTH_SELECT_G`：`"WIDE"`/`"NARROW"`/`"CUSTOM"`，决定 FIFO 存储体用多宽的总线。
- `SLAVE_AXI_CONFIG_G` / `MASTER_AXI_CONFIG_G`：两侧的 `AxiStreamConfigType`，可不同。

**(2) 端口两侧对称 + 流控**：[AxiStreamFifoV2.vhd:69-90](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamFifoV2.vhd#L69-L90)。注意 `sAxisCtrl` 默认值是 `AXI_STREAM_CTRL_INIT_C`（`pause='1'`，即复位期安全地反压上游），`mTLastTUser` 只在 `VALID_THOLD_G/=1` 时有意义（用来把帧尾的 `tUser` 侧带随帧送出）。

**(3) 编译期协商 FIFO 配置**：[AxiStreamFifoV2.vhd:98-131](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamFifoV2.vhd#L98-L131)。这段是「宽度适配」的核心——它逐字段取两侧的并/交集：

```vhdl
-- 取两侧较宽者（WIDE）或较窄者（NARROW），或用自定义宽度
TDATA_BYTES_C => ite(INT_WIDTH_SELECT_G = "CUSTOM", INT_DATA_WIDTH_G,
                     ite(INT_WIDTH_SELECT_G = "WIDE",
                         ite(SLAVE > MASTER, SLAVE, MASTER),   -- 取较宽
                         ite(SLAVE > MASTER, MASTER, SLAVE))),  -- 取较窄
-- TSTRB 仅在两侧都启用时才存
TSTRB_EN_C => SLAVE_AXI_CONFIG_G.TSTRB_EN_C and MASTER_AXI_CONFIG_G.TSTRB_EN_C,
-- TDEST/TID/TUSER 一律取较窄
TDEST_BITS_C => ... MASTER/SLAVE 较小者 ...
```

随后 `FIFO_BITS_C := getSlvSize(FIFO_CONFIG_C)` 算出存储体的位宽（`getSlvSize` 见 [AxiStreamPkg.vhd:622-647](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L622-L647)，它按 `TKEEP_MODE/TUSER_MODE` 决定 `tKeep/tUser` 占多少位）。

**(4) 写侧整形 + 打包**：[AxiStreamFifoV2.vhd:185-238](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamFifoV2.vhd#L185-L238)。先用 `U_SlaveResize`（一个 `AxiStreamGearbox`）把 `sAxisMaster` 整形成 `FIFO_CONFIG_C` 宽度；再把记录扁平化进 FIFO：

```vhdl
fifoDin   <= toSlv(fifoWriteMaster, FIFO_CONFIG_C);           -- 记录 → 扁平向量
fifoWrite <= fifoWriteMaster.tValid and fifoReady;            -- 写使能=有效且未满
fifoWriteSlave.tReady <= fifoReady;                           -- 回拉 tReady
fifoReady <= (not fifoAFull) when SLAVE_READY_EN_G else '1';  -- 满则反压
```

注意 `SLAVE_READY_EN_G=false` 时写侧永远「就绪」（`fifoReady='1'`），此时若下游来不及读，溢出的拍会被 `sAxisCtrl.overflow` 标出——这是「宁可丢拍也不反压」的高速直通模式。

**(5) 背压产生**：[AxiStreamFifoV2.vhd:208-223](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamFifoV2.vhd#L208-L223)。`FIFO_FIXED_THRESH_G=true`（默认）时 `pause` 直接来自可编程满标志 `fifoPFullVec`；否则比较 `fifoWrCount >= fifoPauseThresh`。这段同时示范了「同一进程内用 `RST_ASYNC_G` 在同步/异步复位间二选一」的写法（与 u1-l5 一致）。

**(6) 存储体本体**：[AxiStreamFifoV2.vhd:240-274](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamFifoV2.vhd#L240-L274)。例化 `FifoCascade`，`FWFT_EN_G => true`（首字直通，见 u2-l2），`DATA_WIDTH_G => FIFO_BITS_C`，`ADDR_WIDTH_G => FIFO_ADDR_WIDTH_G`，`FULL_THRES_G => FIFO_PAUSE_THRESH_G`，`EMPTY_THRES_G => 8`（留几拍防「空」标志抖动）。`CASCADE_SIZE_G` 支持多个 FIFO 级联，`CASCADE_PAUSE_SEL_G` 指定用哪一级做反压判定。

**(7) 帧门控与 tLast 侧带 FIFO**：[AxiStreamFifoV2.vhd:292-400](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamFifoV2.vhd#L292-L400)。当 `VALID_THOLD_G/=1` 时，额外例化一个小 FIFO（`U_LastFifo`）只存「帧尾那一拍的 `tUser`」：

```vhdl
U_LastFifoEnGen : if VALID_THOLD_G /= 1 generate
   U_LastFifo : entity surf.FifoCascade
      generic map ( DATA_WIDTH_G => maximum(FIFO_USER_BITS_C, 1),
                    ADDR_WIDTH_G => LAST_FIFO_ADDR_WIDTH_C, ... )
      port map ( wr_en => fifoWriteLast, din => fifoWriteUser,
                 rd_en => fifoReadLast,  dout => fifoReadUser, ...);
```

读侧的 `fifoInFrame` 状态机（[AxiStreamFifoV2.vhd:321-391](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamFifoV2.vhd#L321-L391)）决定何时开始放行一帧/一块：在 `fifoValidLast='1'`（帧尾已到）或 `fifoRdCount >= VALID_THOLD_G`（攒够拍数）时拉起，遇到 `tLast` 或 FIFO 空则落下。最终 `fifoValid <= fifoValidInt and fifoInFrame`（[L391](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamFifoV2.vhd#L391)）。`VALID_THOLD_G=1` 时这一切被 `U_LastFifoDisGen` 直接短路（[L395-400](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamFifoV2.vhd#L395-L400)），`fifoValid=fifoValidInt`。

**(8) 读侧整形 + idle 同步 + 末级流水**：[AxiStreamFifoV2.vhd:402-471](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamFifoV2.vhd#L402-L471)。`toAxiStreamMaster` 把扁平向量还原成记录（[L405](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamFifoV2.vhd#L405)），再经 `U_MasterResize` 整形成 `MASTER_AXI_CONFIG_G` 宽度；一个 `Synchronizer` 把读侧 `tValid` 取反同步回写侧当 `idle`（注释自嘲 "This is a total hack"，[L437-447](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamFifoV2.vhd#L437-L447)）；最后挂一个 `AxiStreamPipeline`（级数 `PIPE_STAGES_G`）做输出寄存。也就是说——**FIFO 内部已经自带了一个流水寄存器**，4.2 节就是它。

#### 4.1.4 代码实践

实践目标：用源码阅读 + 跑现成 cocotb 回归的方式，验证 FIFO 的「帧门控」与背压行为。

1. **操作步骤（源码阅读型）**：打开 `AxiStreamFifoV2.vhd`，对照 4.1.3 的 (3)(6)(7) 三段，画出 `sAxisMaster → mAxisMaster` 的逐级数据通路图，标注每一级的位宽（用你假设的两侧配置，例如写 8B/读 16B）。
2. **操作步骤（运行型）**：按 u1-l2/u9-l1 的工具链，先生成源缓存再跑 `AxiStreamPipeline` 的回归（它最贴近「末级流水」行为）：
   ```bash
   make MODULES=$PWD import
   ./.venv/bin/python -m pytest -q tests/axi/axi_stream/test_AxiStreamPipeline.py
   ```
3. **需要观察的现象**：测试里 `latency_and_backpressure_test` 断言「无背压时 sink 相对 source 的延迟 = `PIPE_STAGES_G + 2` 拍」（见 [test_AxiStreamPipeline.py:237-238](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/axi/axi_stream/test_AxiStreamPipeline.py#L237-L238)），而 `stage2_sync` 用例的 `PIPE_STAGES_G=2`。
4. **预期结果**：三组参数用例（zero_stage_sync / stage2_sync / stage1_async_active_low）全部 PASSED；阅读 `PARAMETER_SWEEP`（[test_AxiStreamPipeline.py:321-346](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/axi/axi_stream/test_AxiStreamPipeline.py#L321-L346)）确认它们覆盖了「直通 / 多级同步 / 异步低有效复位」三条路径。
5. 若本地未配好 GHDL+ruckus 环境，上述运行结果标注「待本地验证」，但源码阅读部分应能独立完成。

#### 4.1.5 小练习与答案

**练习 1**：若把 `SLAVE_READY_EN_G` 设为 `false`，写侧 `tReady` 会变成什么？溢出会从哪里报告？
**答案**：`fifoReady` 恒为 `'1'`（[L226](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamFifoV2.vhd#L226)），写侧永不反压；溢出的拍由底层 FIFO 的 `overflow` 口报告到 `sAxisCtrl.overflow`（[L264](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamFifoV2.vhd#L264)）。

**练习 2**：为什么 tLast 的 `tUser` 要单独用一个 FIFO（`U_LastFifo`）存，而不是和数据放一起？
**答案**：因为 `VALID_THOLD_G/=1` 时，输出 `tValid` 被帧门控延后，需要在帧尾真正送出前「超前窥视」那一拍的 `tUser` 侧带（经 `mTLastTUser` 暴露）；把这条窄信息单独走一个小 FIFO，既省存储，又能和数据 FIFO 解耦地做这个 look-ahead。

**练习 3**：默认 `PIPE_STAGES_G=1`，意味着 `AxiStreamFifoV2` 输出端最少有几级寄存？
**答案**：至少 1 级（末级 `AxiStreamPipeline`，[L453-471](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamFifoV2.vhd#L453-L471)）；若 `INT_PIPE_STAGES_G>0` 还会在 FIFO 内部再加。

---

### 4.2 流水寄存器（AxiStreamPipeline）

#### 4.2.1 概念说明

当一条组合路径太长（比如跨了半个芯片的布线 + 一堆查找表），时钟跑不上去，工程师会沿路插寄存器把它「切断」。`AxiStreamPipeline` 就是 AXI-Stream 版的切刀：

- `PIPE_STAGES_G = 0`：**零延迟直通**，三个信号原样连过去，连时钟都不用——这是「我先占个位，必要时再插寄存」的占位符。
- `PIPE_STAGES_G > 0`：插入 N 级寄存，并且是一个**带 skid（打滑缓冲）+ 空洞压缩**的流水线：即使下游突然反压，已进入流水线的数据也不会丢、不会乱。

它还顺带搬运一路通用侧带 `sSideBand → mSideBand`（`SIDE_BAND_WIDTH_G` 位），这样像「帧尾 tUser」这类窄信息可以和数据一起被寄存、一起被反压保护。

#### 4.2.2 核心流程

`PIPE_STAGES_G>0` 时，内部维护一个深度为 `PIPE_STAGES_C = PIPE_STAGES_G+1` 的寄存器阵列（`mAxisMaster(0..PIPE_STAGES_C)`），每拍：

```text
若 最末级空 或 下游 ready：整体右移（高端向输出端推进）
   └ 若第 0 级空：拉 sAxisSlave.tReady，把 sAxisMaster 装入第 1 级
   └ 若第 0 级满：把第 0 级推进到第 1 级，再视上游情况补第 0 级
否则（末级满且下游不 ready）：
   └ 不整体右移；但做「空洞压缩」——若某空格前一级是满的，就把它前移填洞
```

「空洞压缩」是关键：如果只做朴素移位，反压会造成流水线中间出现气泡、吞吐下降；这段逻辑会主动把后面的数据往前挤，尽量保持流水线被填满。

#### 4.2.3 源码精读

**(1) 实体与泛型**：[AxiStreamPipeline.vhd:23-42](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPipeline.vhd#L23-L42)。极简：复位三件套 + `SIDE_BAND_WIDTH_G` + `PIPE_STAGES_G`，端口是标准的主/从记录对。

**(2) RegType 用数组存多级**：[AxiStreamPipeline.vhd:46-59](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPipeline.vhd#L46-L59)。

```vhdl
constant PIPE_STAGES_C : natural := PIPE_STAGES_G+1;   -- 多一级做 skid
type RegType is record
   sAxisSlave  : AxiStreamSlaveType;
   mAxisMaster : AxiStreamMasterArray(0 to PIPE_STAGES_C);
   mSideBand   : SideBandArray(0 to PIPE_STAGES_C);
end record;
```

`REG_INIT_C` 把每级都置为 `AXI_STREAM_MASTER_INIT_C`（`tValid='0'`），所以复位后整条流水是空的。

**(3) 零延迟直通分支**：[AxiStreamPipeline.vhd:66-72](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPipeline.vhd#L66-L72)。

```vhdl
ZERO_LATENCY : if (PIPE_STAGES_G = 0) generate
   mAxisMaster <= sAxisMaster;
   mSideBand   <= sSideBand;
   sAxisSlave  <= mAxisSlave;   -- tReady 直通
end generate;
```

注意这是纯组合直连，`tValid/tReady` 同一拍可见，**零拍延迟**——正是 cocotb 测试里 `expected_latency = 0` 的来源。

**(4) 多级流水的 comb 进程**：[AxiStreamPipeline.vhd:74-158](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPipeline.vhd#L74-L158)。骨架仍是 u1-l5 的「`v:=r` → 改 `v` → 同步复位 → `rin<=v`」。核心判断在 [L83](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPipeline.vhd#L83)：

```vhdl
if (r.mAxisMaster(PIPE_STAGES_C).tValid = '0') or (mAxisSlave.tReady = '1') then
   -- 末级空 或 下游收 → 整体右移
   for i in PIPE_STAGES_C downto 2 loop
      v.mAxisMaster(i) := r.mAxisMaster(i-1);  -- 高端向输出端推
   end loop;
   ...装入/推进第 0、1 级...
```

**(5) 空洞压缩**：[AxiStreamPipeline.vhd:132-142](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPipeline.vhd#L132-L142)。当末级被下游堵住时，扫描中间各级，「空格 + 前一级有数据」就把数据前移：

```vhdl
for i in PIPE_STAGES_C-1 downto 1 loop
   if (r.mAxisMaster(i).tValid = '0') and (r.mAxisMaster(i-1).tValid = '1') then
      v.mAxisMaster(i) := r.mAxisMaster(i-1);     -- 前移填洞
      v.mAxisMaster(i-1).tValid := '0';
   end if;
end loop;
```

**(6) 输出与 seq**：输出取最末级 `r.mAxisMaster(PIPE_STAGES_C)`（[L145-148](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPipeline.vhd#L145-L148)）；`seq` 进程（[L160-167](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPipeline.vhd#L160-L167)）仍是「异步复位在前、上升沿 `r<=rin after TPD_G` 在后」的标准双进程尾部。

关于 **TLAST 传播**：`tLast` 只是 `AxiStreamMasterType` 的一个字段，它随所在的那一拍一起被寄存、移位、前移，所以本模块对帧边界是「透明」的——`tLast='1'` 的那一拍走完整条流水后仍标着 `tLast='1'`，顺序不变。这也解释了 cocotb 测试里为何能逐帧比对 `tdata/tid/tdest`。

#### 4.2.4 代码实践

> 这是本讲的主实践任务：**在一条 64 位（8 字节）流的前后各加一个 `AxiStreamPipeline`，并说明它对时序路径与帧 TLAST 传播的影响。**

下面是一段**示例代码**（不是仓库原有文件，供你在自己的 scratch 工程里练习；请勿写入 surf 源码树）：

```vhdl
-- 示例代码：用两个 AxiStreamPipeline 夹住一个待改善时序的 64 位处理级 DUT
constant AXIS_CFG_C : AxiStreamConfigType := (
   TSTRB_EN_C => false, TDATA_BYTES_C => 8,          -- 64 位
   TDEST_BITS_C => 4, TID_BITS_C => 0,
   TKEEP_MODE_C => TKEEP_NORMAL_C,
   TUSER_BITS_C => 0, TUSER_MODE_C => TUSER_NONE_C);

-- 输入侧：1 级流水，切割进入 DUT 的长组合路径
U_InPipe : entity surf.AxiStreamPipeline
   generic map ( PIPE_STAGES_G => 1, SIDE_BAND_WIDTH_G => 1 )
   port map ( axisClk => clk, axisRst => rst,
              sAxisMaster => sAxisMaster, sAxisSlave => sAxisSlave,
              mAxisMaster => dutInMaster,  mAxisSlave  => dutInSlave );

-- 中间是你要改善时序的处理级（占位）
-- DUT : entity work.MyProcessing port map ( ... dutInMaster/dutInSlave, dutOutMaster/dutOutSlave ... );

-- 输出侧：2 级流水，切割 DUT 输出的长组合路径
U_OutPipe : entity surf.AxiStreamPipeline
   generic map ( PIPE_STEPS_G => 2, SIDE_BAND_WIDTH_G => 1 )  -- 注意：正确写法是 PIPE_STAGES_G
   port map ( axisClk => clk, axisRst => rst,
              sAxisMaster => dutOutMaster, sAxisSlave => dutOutSlave,
              mAxisMaster => mAxisMaster,  mAxisSlave  => mAxisSlave );
```

1. **实践目标**：理解「在长组合路径两端插流水」如何把一条大路径切成三段，并验证帧边界不被破坏。
2. **操作步骤**：
   - 修正上面输出侧故意的拼写（`PIPE_STEPS_G` → `PIPE_STAGES_G`），体会泛型名必须精确。
   - 把输入侧设 `PIPE_STAGES_G=>1`、输出侧设 `PIPE_STAGES_G=>2`，分别在 DUT 两侧各加 1 拍 / 2 拍寄存延迟。
   - 跑 [test_AxiStreamPipeline.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/axi/axi_stream/test_AxiStreamPipeline.py) 的 `stage2_sync` 用例作为对照。
3. **需要观察的现象与时序影响**：
   - 组合路径被切成 `输入寄存 → DUT 组合逻辑 → 输出寄存`，每段的关键路径变短，时序（setup slack）改善；代价是端到端多了 `PIPE_STAGES_G(前)+PIPE_STAGES_G(后)` 拍延迟。
   - 背压下 `tReady` 会逐级回传，DUT 仍能正确 stall。
4. **预期结果（TLAST 传播）**：一帧的 `tLast='1'` 拍走完两个流水后仍为最后一拍、顺序不变——这就是 `stream_order_and_sideband_test` 逐帧断言 `rx_frame.tdata == frame.tdata` 能通过的原因。延迟方面，由 [test_AxiStreamPipeline.py:237](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/axi/axi_stream/test_AxiStreamPipeline.py#L237) 可知每段寄存的「wrapper 可见 sink 延迟 = `PIPE_STAGES_G + 2`」拍，两段串联则延迟相加（待本地验证具体相加后的总拍数）。
5. 若无本地仿真环境，端到端拍数标注「待本地验证」；但「路径被切段 + TLAST 透传」这两点可仅凭源码确认。

#### 4.2.5 小练习与答案

**练习 1**：`PIPE_STAGES_G=0` 和 `PIPE_STAGES_G=1` 在资源与延迟上的差别？
**答案**：`=0` 是纯连线，零寄存器、零延迟；`=1` 会生成一个深度 2 的寄存器阵列（`PIPE_STAGES_C=2`，多出的一级做 skid），引入寄存器并至少 1 拍延迟，但能承受背压。

**练习 2**：若没有「空洞压缩」逻辑，反压恢复后吞吐会怎样？
**答案**：流水线里会残留气泡，吞吐在一段时间内低于 1 拍/周期；空洞压缩把数据前移填洞，使流水线尽快回到满吞吐。

**练习 3**：为什么 `AxiStreamPipeline` 要把 `tLast` 也当普通数据字段一起寄存？
**答案**：因为帧边界必须随原拍原样、原序到达，`tLast` 只是一拍数据的属性；与数据同步寄存才能保证「打标记的那一拍」仍是同一拍，帧语义不被破坏。

---

### 4.3 位宽 Resize（AxiStreamResize）

#### 4.3.1 概念说明

很多系统里「上游窄、下游宽」或反过来（例如 32 位 AXI-Lite 域接到 64 位 AXI-Stream，或 64 位流接到 16 位 MAC）。`AxiStreamResize` 专门做这件事，且遵循两条铁律（写在文件头注释里）：

- **小端对齐**（little endian）：低字节始终落在低位。
- **不要用于 interleave（交错）tDest 的场景**——因为窄化时一拍会拆成多拍，交错路由会乱。

它的两侧位宽必须互为整数倍（由 assert 强制），所以变换是「干净的 2:1 / 4:1 等」，不会出现零碎字节对不齐。

#### 4.3.2 核心流程

用一个计数器 `count` 在 \(0 \ldots COUNT_C-1\) 间循环，\(COUNT_C = \max(SLV\_BYTES/MST\_BYTES, MST\_BYTES/SLV\_BYTES)\)：

- **升档（窄→宽，`MST_BYTES > SLV_BYTES`）**：每收一拍窄数据，按 `count` 写到宽输出的对应字节段；攒满 `COUNT_C` 拍（或遇到 `tLast` 提前结束）才置 `tValid='1'` 输出一拍宽。期间持续拉写侧 `tReady`。
- **降档（宽→窄，`SLV_BYTES > MST_BYTES`）**：每收一拍宽数据，按 `count` 从中切出一拍窄输出；切满 `COUNT_C` 拍，或已切到 `tKeep` 指示的有效字节数且遇 `tLast`，才放行写侧并进下一拍。会丢弃「没有 tKeep 位、且非 tLast」的空拍。
- **不变（`SLV_BYTES = MST_BYTES`）**：走直通，但仍可能做 `TKEEP_COUNT`↔其他模式的 `tKeep` 重编码与 `tUser` 位宽重整。

所有变换都同步搬运 `tKeep/tStrb/tData/tUser/tDest/tId`，`tLast` 跟着最后一拍走。

#### 4.3.3 源码精读

**(1) 实体与互整数倍约束**：[AxiStreamResize.vhd:26-105](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamResize.vhd#L26-L105)。三个 assert（[L90-105](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamResize.vhd#L90-L105)）很关键：

```vhdl
assert ((SLV_BYTES_C >= MST_BYTES_C and SLV_BYTES_C mod MST_BYTES_C = 0) or
        (MST_BYTES_C >= SLV_BYTES_C and MST_BYTES_C mod SLV_BYTES_C = 0))
   report "Data widths must be even number multiples of each other" ...
-- 宽→窄必须开 READY_EN_G（要反压才能切完一拍）
assert (SLV_BYTES_C <= MST_BYTES_C or READY_EN_G = true) ...
-- 主侧 TKEEP_FIXED 时从侧也必须 FIXED
assert (not (MASTER.TKEEP_MODE_C = TKEEP_FIXED_C and SLAVE.TKEEP_MODE_C /= TKEEP_FIXED_C)) ...
```

**(2) COUNT_C 与 RegType**：[AxiStreamResize.vhd:64-81](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamResize.vhd#L64-L81)。`COUNT_C` 就是「一拍宽对应几拍窄」；`obMaster` 是组装中的输出记录。

**(3) comb 公共头**：[AxiStreamResize.vhd:107-149](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamResize.vhd#L107-L149)。先按 `TKEEP_COUNT` 模式算出本拍有效字节数 `byteCnt`，再把 `tUser` 按「每字节 SLV_USER 位」标准化成「每字节 8 位」的内部表示（[L140-149](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamResize.vhd#L140-L149)），统一后续切片下标计算。

**(4) 升档**：[AxiStreamResize.vhd:154-183](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamResize.vhd#L154-L183)。按 `idx` 把窄数据塞进宽输出的对应段，到 `COUNT_C-1` 或 `tLast` 时置有效：

```vhdl
v.obMaster.tData((SLV_BYTES_C*8*idx)+SLV_BYTES_C*8-1 downto SLV_BYTES_C*8*idx) := ibM.tData(SLV_BYTES_C*8-1 downto 0);
...
if ibM.tValid = '1' then
   if r.count = (COUNT_C-1) or ibM.tLast = '1' then
      v.obMaster.tValid := '1';   -- 攒满/帧尾 → 输出
      v.count := (others => '0');
   else v.count := r.count + 1; end if;
end if;
```

**(5) 降档**：[AxiStreamResize.vhd:185-215](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamResize.vhd#L185-L215)。反向切片，关键在「已切到有效字节且 tLast」才放行上游，并丢弃空拍（[L213](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamResize.vhd#L213)）：

```vhdl
v.obMaster.tValid := ibM.tValid and (uOr(v.obMaster.tKeep(COUNT_C-1 downto 0)) or v.obMaster.tLast);
```

**(6) 不变宽度直通 + tKeep/tUser 重整**：[AxiStreamResize.vhd:219-265](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamResize.vhd#L219-L265)。即使 `SLV_BYTES_C=MST_BYTES_C`，仍可能需要把 `TKEEP_COUNT` 编码转成 `TKEEP_NORMAL` 掩码（或反之），以及把每字节 `tUser` 在不同 `TUSER_BITS` 间重整。

**(7) seq + 末级流水**：[AxiStreamResize.vhd:271-300](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamResize.vhd#L271-L300)。`seq` 在「不变宽度」时恒走复位分支（因为直通无状态，见 [L273/L276](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamResize.vhd#L273) 的 `or (SLV_BYTES_C = MST_BYTES_C)`）；末级固定挂一个 `AxiStreamPipeline`（级数 `PIPE_STAGES_G`）做输出寄存——这与 4.1 的 FIFO 末级是同一个模块。

#### 4.3.4 代码实践

实践目标：用现成 cocotb 测试观察升档/降档的字节流守恒与侧带对齐。

1. **操作步骤**：阅读 [test_AxiStreamResize.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/axi/axi_stream/test_AxiStreamResize.py)，关注三个用例（[L202-230](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/axi/axi_stream/test_AxiStreamResize.py#L202-L230)）：`equal_width_sync`(4→4)、`upsize_sync`(2→4)、`downsize_async_active_low`(4→2)。
2. **运行**：`./.venv/bin/python -m pytest -q tests/axi/axi_stream/test_AxiStreamResize.py`
3. **需要观察的现象**：`frame_round_trip_test` 断言 `rx_frame.tdata == frame.tdata`（[L159](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/axi/axi_stream/test_AxiStreamResize.py#L159)）——即无论升档还是降档，**字节流完全一致**；侧带按「输出拍数」展开（[L162-163](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/axi/axi_stream/test_AxiStreamResize.py#L162-L163)）。
4. **预期结果**：三组参数用例全部 PASSED，证明 `tKeep` 语义被正确保持（有效字节边界不变），`tDest/tId` 随帧不变。
5. 运行结果若未本地执行，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么宽→窄（降档）必须 `READY_EN_G=true`？
**答案**：一拍宽要切成多拍窄输出，切完之前不能放行上游，必须用 `tReady` 反压；窄→宽则可以一直收（攒够再发），故无此约束（见 [L98-99](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamResize.vhd#L98-L99) 的 assert）。

**练习 2**：升档时若一帧窄数据只有 3 拍（不足 `COUNT_C`），输出会怎样？
**答案**：遇到 `tLast` 会提前置 `tValid='1'` 输出最后一拍宽（[L177-178](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamResize.vhd#L177-L178)），未填满的高位字节由 `tKeep` 标为无效，下游据 `tKeep` 即可还原原始字节数。

**练习 3**：为什么文件头注释禁止把它用于 interleave tDest？
**答案**：窄化会把一拍拆成多拍，且 `tLast` 被重新对齐到切出的最后一拍；若同时按 `tDest` 交错路由，帧的归属与边界会被拆分打乱，故交错场景应改用 `AxiStreamGearbox` 之外的、按 VC 逐帧处理的模块。

---

## 5. 综合实践

把本讲三件事串起来：**设计一个「8 字节写、4 字节读、跨时钟域、带 1 级输出流水」的流缓冲，并解释数据通路。**

1. **实例化选择**：你会选 `AxiStreamFifoV2` 还是「`AxiStreamResize` + `FifoAsync` + `AxiStreamPipeline`」手搭？对照 4.1.2 的通路图你会发现——`AxiStreamFifoV2` 内部正是后者（两侧各一个 Gearbox 起整形作用 + 一个 FifoCascade + 一个 AxiStreamPipeline），所以**直接例化 `AxiStreamFifoV2` 一步到位**。
2. **配置**：写出关键泛型：`SLAVE_AXI_CONFIG_G.TDATA_BYTES_C=8`、`MASTER_AXI_CONFIG_G.TDATA_BYTES_C=4`、`GEN_SYNC_FIFO_G=false`（跨时钟域）、`PIPE_STAGES_G=1`、`FIFO_ADDR_WIDTH_G=9`（512 深度）。
3. **画通路并标注**：写侧 8B → SlaveResize 整形到 FIFO 宽度（取 WIDE 即 8B）→ FifoAsync 跨域 → MasterResize 整形到读侧 4B → AxiStreamPipeline 1 级 → 输出。
4. **自检问题**（用源码回答）：
   - 一帧 11 字节的数据，读侧会看到几拍？（答：3 拍，最后一拍 `tKeep` 仅低 3 字节有效，对应窄化逻辑 [L185-215](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamResize.vhd#L185-L215)。）
   - 读侧若长期不 ready，写侧何时开始反压？（答：FIFO 将满时先 `pause`，真满时 `tReady='0'`，见 [L208-238](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamFifoV2.vhd#L208-L238)。）
5. 把上述配置写成一个**示例**顶层（仅在你的 scratch 工程里），用 `axiStreamSimSendFrame`（[AxiStreamPkg.vhd:496-525](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L496-L525)）发一帧、用 `axiStreamSimReceiveTxn` 收回，比对字节流。

---

## 6. 本讲小结

- `AxiStreamFifoV2` 是 SURF 流缓冲的「瑞士军刀」：**两侧位宽协商 + 异步/同步 FIFO + 帧门控 + 末级流水**四合一，内部由 `AxiStreamGearbox`（整形）+ `FifoCascade`（存储）+ `AxiStreamPipeline`（输出寄存）拼成。
- 记录靠 `toSlv`/`toAxiStreamMaster`/`getSlvSize` 与扁平 `slv` 互转进/出 FIFO；`FIFO_CONFIG_C` 在编译期用 `ite` 三元组取两侧的并/交集完成宽度协商。
- `VALID_THOLD_G` 把逐拍输出升级为「帧/块级门控」，配套一个独立的 tLast 侧带 FIFO 做 look-ahead；默认 `=1` 时这条路径被短路。
- `AxiStreamPipeline` 在 `PIPE_STAGES_G=0` 时零延迟直通、`>0` 时是多级带 skid 与**空洞压缩**的流水寄存器，是切组合路径、改善时序的标准件；它对 `tLast` 透传，帧边界不破坏。
- `AxiStreamResize` 做互整数倍的窄↔宽变换，小端对齐，逐字节保持 `tKeep/tStrb/tUser/tDest/tId`，宽→窄必须开 `READY_EN_G`，且不适用于 interleave tDest。
- 这三个模块共享同一套记录/配置/双进程骨架，且都「自带末级 `AxiStreamPipeline`」，所以在 SURF 里例化它们往往同时拿到了缓冲、整形与时序优化。

---

## 7. 下一步学习建议

- **下一步学 u4-l3（AXI-Stream 路由：Mux/DeMux/Gearbox）**：本讲的 `AxiStreamGearbox` 将在那里与 `AxiStreamMux/DeMux` 汇合，讲清多流合并、按 `tDest` 分发与位宽变换的关系。
- **回头印证 u2-l2**：本讲的 `FifoCascade` 就是 u2-l2 讲的异步 FIFO；建议对照确认 Gray 指针、FWFT、`almost_full/empty` 的判定在本讲里如何被消费。
- **前瞻 u5-l1（SSI 侧带与帧边界）**：SSI 把 SOF/EOF/EOFE 编码进 `tUser`，正好靠本讲模块「`tUser` 随拍透传 + 帧边界保持」的特性才能正确流过 FIFO/Pipeline/Resize，这是承上启下的关键。
- **想动手验证**：把 [tests/axi/axi_stream/](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/axi/axi_stream/) 下的 `test_AxiStreamPipeline.py` 与 `test_AxiStreamResize.py` 当作「可执行规约」阅读，是理解这两个模块行为最快的路径。
