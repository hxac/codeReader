# SSI 测试码型与帧过滤

## 1. 本讲目标

本讲在 u5-l1（SSI 侧带与帧边界）建立的 SOF / EOF / EOFE / VC 语义之上，进入「如何给一条 SSI 流做内置自检（BIST）」与「如何用过滤器保护一条 SSI 流」两件事。读完本讲你应该能够：

- 看懂 `SsiPrbsTx` 如何用一台 LFSR 产生带 SOF/EOF/EOFE 的 PRBS 帧，以及帧内「种子字 → 长度字 → 数据字」的三段式结构。
- 看懂 `SsiPrbsRx` 如何用同一台 LFSR 在接收端逐拍预测期望数据、与实收数据比对，并把检测结果汇总成「丢包 / 长度错 / 数据总线错 / EOFE / 字错」五类错误。
- 理解 `SsiIbFrameFilter` / `SsiObFrameFilter` 如何在 FIFO 两侧强制 SSI 帧不变量（单 SOF、单 VC、不交错），把畸形帧要么丢弃、要么改造成 EOFE 帧。
- 理解 `SsiFrameLimiter` 如何用「最大帧长 + 超时」两道闸门，防止超长帧或挂死帧拖垮下游。
- 能够把 `SsiPrbsTx` 直连 `SsiPrbsRx` 构成环回，注入一帧错误并预测/观察 EOFE 行为。

## 2. 前置知识

本讲默认你已经掌握 u5-l1 的内容，尤其是：

- **SSI 侧带编码**：SOF 与 EOFE 各占 TUSER 一位（`SSI_EOFE_C = 0`、`SSI_SOF_C = 1`），EOF 复用 `tLast`；由 `ssiSetUserSof` / `ssiSetUserEofe` / `ssiGetUserSof` / `ssiGetUserEofe` 读写。
- **TDEST 即虚拟通道（VC）**：一帧之内 VC 不允许交错变化。
- **`ssiAxiStreamConfig`** 一次性填好 SSI 的 `AxiStreamConfigType` 约定。

并默认你掌握 u2-l5 的内容：

- **LFSR / PRBS**：`StdRtlPkg.lfsrShift(lfsr, taps, input)` 是单步 Galois 型线性反馈移位寄存器函数，给定相同种子与抽头，序列完全确定、可复现。这正是 PRBS 既「像随机」又「可预测」的原因。

一句话复习：SSI = AXI-Stream + 帧语义（SOF/EOF/EOFE）+ 虚拟通道（TDEST）。本讲所有模块都在这层语义上工作。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `protocols/ssi/rtl/SsiPrbsTx.vhd` | PRBS 帧发送器：状态机驱动 LFSR，产出 SOF→长度→数据的完整 SSI 帧，可软件触发或外部触发，可强制 EOFE。 |
| `protocols/ssi/rtl/SsiPrbsRx.vhd` | PRBS 帧校验器：用同款 LFSR 逐拍预测期望数据并比对，输出五类错误与帧率/帧长统计，错误帧在输出副本上标注 EOFE。 |
| `protocols/ssi/rtl/SsiIbFrameFilter.vhd` | 入向帧过滤器：保护下游 FIFO，丢弃无 SOF 的散字、把「重复 SOF / 帧中途换 VC」截断成 EOFE 帧、溢出时补一个 EOFE 收尾。 |
| `protocols/ssi/rtl/SsiObFrameFilter.vhd` | 出向帧过滤器：读 FIFO 侧，可丢弃已被标 EOFE 的错误帧，并同样强制帧不变量。 |
| `protocols/ssi/rtl/SsiFrameLimiter.vhd` | 帧长/超时限制器：超过 `FRAME_LIMIT_G` 字或超过 `TIMEOUT_G` 时强行截断并标 EOFE。 |
| `protocols/ssi/rtl/SsiPkg.vhd` | SSI 包：定义 EOFE/SOF 比特位与读写过程（u5-l1 已讲，本讲引用）。 |
| `protocols/ssi/wrappers/SsiPrbsWrapper.vhd` | 把 Tx/Rx 直连成环回的 cocotb 顶层，是本讲代码实践的实物载体。 |
| `tests/protocols/ssi/test_SsiPrbs.py` | 上述环回的 cocotb 回归测试，含「强制 EOFE」用例。 |

> 提示：`SsiIbFrameFilter` 与 `SsiObFrameFilter` 平时并不单独出现，而是被 `SsiFifo` 内部组合成「入向过滤 → FIFO → 出向过滤」的三明治（u5-l1 已讲 `SsiFifo`）。本讲把它们拆出来单独精读。

## 4. 核心概念与源码讲解

### 4.1 PRBS 帧收发：SsiPrbsTx 与 SsiPrbsRx

#### 4.1.1 概念说明

PRBS（Pseudo-Random Binary Sequence，伪随机二进制序列）是链路自检最常用的激励：它「看起来随机」（频谱丰富、能压测数据通路），却「完全确定」（收发两端用同一台 LFSR、同一个种子就能复现同一串比特）。因此校验侧不需要缓存整帧期望数据，只要同步好 LFSR 的种子，就能逐拍预测期望值并与实收值比对。

SURF 把这套自检做成了标准核：

- `SsiPrbsTx` 负责造帧：每一帧带一个递增的「事件号（eventCnt）」当种子，再用 LFSR 产生若干拍伪随机数据，按 SSI 帧格式（SOF + EOF + 可选 EOFE）发出去。
- `SsiPrbsRx` 负责查帧：从 SOF 里读出事件号，重建同一台 LFSR 的状态，逐拍预测、逐拍比对，统计错误。

二者合起来就是一条 SSI 链路的「内置自检（BIST）」——这也是为什么几乎所有 PGP / 以太网 / RSSI 的回归测试里都能看到 `SsiPrbsTx`↔`SsiPrbsRx` 的身影。

#### 4.1.2 核心流程

**帧格式（三段式）。** 一帧由若干「字（word）」组成，每个字宽度为 `PRBS_SEED_SIZE_G` 向上取整到整字节（`wordCount(PRBS_SEED_SIZE_G, 8)`）：

```
┌──────────┬──────────┬───────────────┬─────┬───────────────┐
│ 字0 (SOF)│ 字1 长度 │ 字2 数据       │ ... │ 字N 数据 (EOF) │
│ = 事件号 │ = 帧长   │ = LFSR 输出    │     │ tLast=1       │
└──────────┴──────────┴───────────────┴─────┴───────────────┘
```

- **字0（种子字）**：低 `EVENT_CNT_SIZE_C` 位放事件号（=本帧的 PRBS 种子），高位补 0，并把 SOF 位置 1。
- **字1（长度字）**：低 32 位放 `length`（数据段字数），高位应为 0。
- **字2..N（数据字）**：LFSR 逐拍输出的伪随机数据；最后一拍 `tLast=1`，并把 EOFE 位置成「溢出/强制」标志。

**Tx 状态机**：`IDLE_S → SEED_RAND_S → LENGTH_S → DATA_S → IDLE_S`。

```text
IDLE_S        等 trigger；命中后锁存 tDest/tId、强制最小帧长≥2、装种子
   ↓
SEED_RAND_S   发种子字(SOF=1)，LFSR 前进一步，eventCnt++
   ↓
LENGTH_S      发长度字
   ↓
DATA_S        每拍发一数据字(LFSR 当前值)再前进一步；到 length 拍时
              置 tLast=1、EOFE=overflow，回 IDLE_S
```

**Rx 状态机**：`IDLE_S → LENGTH_S → DATA_S → IDLE_S`（无 SEED 态，因为种子在 IDLE 态从输入直接读）。

```text
IDLE_S     等 SOF：读事件号→判定丢包→用事件号初始化 LFSR
   ↓
LENGTH_S   读长度字；校验高位字节全 0(否则 errDataBus)；LFSR 前进一步
   ↓
DATA_S     每拍：LFSR 前进一步，用「前进前的值」与实收比对
           不符→errWordStrb/errWordCnt++；到 EOF 时统计长度/EOFE，回 IDLE_S
```

**为什么收发 LFSR 能逐拍对齐？** 关键在于两边在「正式数据」之前都恰好让 LFSR 前进一步：Tx 在 `SEED_RAND_S` 前进一步、产生第一个数据字 `S1`；Rx 在 `LENGTH_S` 前进一步、得到用来比对的 `S1`。此后双方每收/发一个数据字都前进一步，于是第 k 个数据字在两端恒等于 \(S_k \)。用递推写出来，PRBS-32（抽头 31,6,2,1）满足：

\[
S_{k+1}=\mathrm{lfsrShift}(S_k,\,(31,6,2,1))
\]

种子 \(S_0 \) 由事件号决定。两端只要种子相同、步数相同，序列逐比特相等——这正是「逐拍比对、无需缓存」的基础。

**错误模型（Rx 五类错误）**：

| 信号 | 触发条件 |
| --- | --- |
| `errMissedPacket` | 实收事件号 ≠ 期望事件号（连号断裂，例如 Tx 被复位导致种子回卷） |
| `errDataBus` | 长度字的高位字节非 0（数据总线/位宽错位） |
| `errLength` | 帧太短（SOF 即 EOF）或 `dataCnt ≠ packetLength` |
| `errEofe` | 实收帧自带 EOFE |
| `errWordCnt` | 数据字逐拍比对不符的累计字数（饱和到全 1） |

任一错误都会让 `errorDet=1`，并在 Rx 输出副本的 EOF 上回写 EOFE。

#### 4.1.3 源码精读

**Tx 的 SSI 配置与种子宽度。** `PRBS_SSI_CONFIG_C` 把数据字宽设成种子向上取整的字节数，TUSER 固定 2 位（SOF/EOFE），TDEST/TID 各 8 位：

[protocols/ssi/rtl/SsiPrbsTx.vhd:80-89](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPrbsTx.vhd#L80-L89) — 计算 `EVENT_CNT_SIZE_C = min(PRBS_SEED_SIZE_G, 32)` 与每字节数 `PRBS_BYTES_C`，并据此构造内部 SSI 配置。

**强制/溢出 EOFE 的来源。** Tx 把 `forceEofe` 端口与下游 FIFO 的 `txCtrl.overflow` 合并成一个 `overflow` 锁存位，最终写在 EOF 拍的 EOFE 比特上——这就是「注入一帧错误」的官方入口：

[protocols/ssi/rtl/SsiPrbsTx.vhd:232-236](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPrbsTx.vhd#L232-L236) — 一旦 `txCtrl.overflow='1'` 或 `forceEofe='1'`，锁存 `v.overflow := '1'`。

**IDLE 态：触发、装种子、最小帧长。** 命中 `trigger` 后，把事件号塞进 `randomData` 低位当种子，并把请求帧长 0/1 钳位到最小值 2（保证至少有种子字+长度字+数据字的可见结构）：

[protocols/ssi/rtl/SsiPrbsTx.vhd:248-279](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPrbsTx.vhd#L248-L279) — 锁存 `tDest/tId`、用 `eventCnt` 装种子、强制 `length≥2`，进入 `SEED_RAND_S`。

**SEED_RAND_S：发种子字并让 LFSR 前进一步。** 这是 Tx 侧唯一的「发字 + 前进一步」对齐点：

[protocols/ssi/rtl/SsiPrbsTx.vhd:281-302](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPrbsTx.vhd#L281-L302) — 把 `eventCnt` 写进 `tData` 低段、调用 `ssiSetUserSof(...,'1')` 置 SOF、`v.randomData := lfsrShift(v.randomData, PRBS_TAPS_G, '0')` 前进一步、`eventCnt+1`、`frameCnt+1`。

**DATA_S：发数据、计数到 length 时收尾。** 末拍置 `tLast=1` 并把锁存的 `overflow` 写进 EOFE：

[protocols/ssi/rtl/SsiPrbsTx.vhd:317-350](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPrbsTx.vhd#L317-L350) — `cntData='0'` 时发 PRBS（`r.randomData`），`'1'` 时发自增计数器；每拍 `lfsrShift` 前进一步；`r.dataCnt = r.length` 时 `tLast:=1` 并 `ssiSetUserEofe(..., r.overflow)`。

**Tx 经 AxiStreamFifoV2 跨域并适配宽度。** 内部窄字（种子宽）经 FIFO 跨到 `mAxisClk` 域并整形到 `MASTER_AXI_STREAM_CONFIG_G`：

[protocols/ssi/rtl/SsiPrbsTx.vhd:384-419](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPrbsTx.vhd#L384-L419) — `SLAVE_AXI_CONFIG_G = PRBS_SSI_CONFIG_C`、`MASTER_AXI_CONFIG_G = MASTER_AXI_STREAM_CONFIG_G`，FIFO 同时承担 CDC 与位宽整形。

**Rx 的输入 FIFO 与输出 gearbox。** Rx 先用 `AxiStreamFifoV2` 把外部流整形到内部 PRBS 配置，再用 `AxiStreamGearbox` 把校验后的副本整形回外部配置输出（Rx 是「穿透 + 标注」型，不丢帧）：

[protocols/ssi/rtl/SsiPrbsRx.vhd:218-271](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPrbsRx.vhd#L218-L271) — `AxiStreamFifo_Rx` 入向整形，`U_Tx` 出向 gearbox。

**Rx IDLE：读种子、判丢包、对齐事件号。** SOF 字的低段就是事件号；若不等于本地 `eventCnt` 则 `errMissedPacket`，随后把本地 `eventCnt` 强行对齐到「实收+1」以恢复同步：

[protocols/ssi/rtl/SsiPrbsRx.vhd:354-365](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPrbsRx.vhd#L354-L365) — 比对 `rxAxisMaster.tData(EVENT_CNT_SIZE_C-1 downto 0) /= r.eventCnt` 判丢包，再 `v.eventCnt := 实收 + 1`，并用实收事件号初始化 `randomData`。

**Rx LENGTH：校验高位字节、LFSR 前进一步。** 长度字只用低 32 位，循环检查第 4 字节起应为 0；同时这里前进一步以与 Tx 的 `SEED_RAND_S` 对齐：

[protocols/ssi/rtl/SsiPrbsRx.vhd:395-406](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPrbsRx.vhd#L395-L406) — `v.randomData := lfsrShift(r.randomData, PRBS_TAPS_G)`，再 `for i in 4 to TDATA_BYTES_C-1 loop` 检查高位字节全 0，否则 `errDataBus`。

**Rx DATA：用「前进前的值」比对、统计字错。** 注意比较用的是 `r.randomData`（本拍前进前的旧值），与 Tx 发出的当前数据字一一对应：

[protocols/ssi/rtl/SsiPrbsRx.vhd:447-460](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPrbsRx.vhd#L447-L460) — 先 `v.randomData := lfsrShift(...)` 前进一步，再用 `r.randomData /= rxAxisMaster.tData(...)` 判字错，命中则 `errWordStrb` 脉冲、`errWordCnt` 自增（饱和到 `MAX_CNT_C`）。

**Rx EOF：综合判定并在输出副本回写 EOFE。** 收到 `tLast` 时，读入帧自带 EOFE、检查帧长，并把 `errorDet` 汇总写回输出的 EOFE：

[protocols/ssi/rtl/SsiPrbsRx.vhd:462-490](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPrbsRx.vhd#L462-L490) — 锁存 `eofe := ssiGetUserEofe(...)`、`if r.dataCnt /= r.packetLength then errLength`、最后 `ssiSetUserEofe(..., v.errorDet)`。

**结果跨域上报。** 帧级标量（packetLength/packetRate/errWordCnt）用 96 位 `SynchronizerFifo` 搬到 `axiClk`，各类错误脉冲则用 `SyncStatusVector` 做事件计数，再经 AXI-Lite 暴露：

[protocols/ssi/rtl/SsiPrbsRx.vhd:535-593](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPrbsRx.vhd#L535-L593) — `SyncFifo_Inst` 搬标量结果，`SyncStatusVec_Inst` 把 `updatedResults/errWordStrb/errDataBus/eofe/errLength/errMissedPacket` 及 FIFO 的 pause/overflow 做成跨域计数器。

> 小结：`SsiPrbsTx` 与 `SsiPrbsRx` 是一对「同款 LFSR + 事件号握手」的确定性自检对，错误分类清晰、结果跨域可读，是 SURF 里几乎所有数据链路回归测试的底层激励/校验源。

#### 4.1.4 代码实践：环回 + 强制 EOFE

本实践的实物载体已经存在：`SsiPrbsWrapper` 把 `SsiPrbsTx.mAxisMaster` 直连到 `SsiPrbsRx.sAxisMaster`，并暴露 `forceEofe` 端口；`test_SsiPrbs.py` 正是「注入一帧错误并观察 EOFE」的回归测试。

**1) 实践目标。** 验证：把 Tx 直连 Rx，发一帧正常数据应零错误；把 `forceEofe` 拉高一帧，应只置位 `errEofe` 而其余错误位保持 0。

**2) 操作步骤（阅读型 → 运行型）。**

- 先读环回拓扑，确认 Tx 的 master 直接连到 Rx 的 slave：

[protocols/ssi/wrappers/SsiPrbsWrapper.vhd:83-141](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/wrappers/SsiPrbsWrapper.vhd#L83-L141) — `U_SsiPrbsTx` 的 `mAxisMaster => axisMaster` 与 `U_SsiPrbsRx` 的 `sAxisMaster => axisMaster` 共享同一信号；`forceEofe` 端口透传到 Tx。

- 再读测试里「强制 EOFE」用例的断言，理解预期：

[tests/protocols/ssi/test_SsiPrbs.py:126-138](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/protocols/ssi/test_SsiPrbs.py#L126-L138) — `trigger_packet(packet_length=4, force_eofe=True)` 后，期望 `errEofe==1` 且其余错误位与 `errWordCnt` 均为 0、`rxPacketLength==4`。

- 运行该回归（沿用 u9-l1 的工具链：先 `import` 生成 cocotb 源缓存，再 pytest）：

```bash
make MODULES=$PWD import
.venv/bin/python -m pytest tests/protocols/ssi/test_SsiPrbs.py -q
```

> 上述命令基于 u9-l1 介绍的标准回归栈；具体标志（如 `-n auto` 并行、GHDL 后端选择）以仓库 `tests/README.md` 与 `pytest.ini` 为准，待本地验证。

**3) 需要观察的现象。** `force_eofe=True` 那一帧的 `updated` 上升沿后，`errEofe=1`、`errMissedPacket/errLength/errDataBus=0`、`errWordCnt=0`、`rxPacketLength=4`。

**4) 预期结果。** 与 `test_SsiPrbs.py` 中 `force_eofe=True` 用例的断言完全一致——这等价于「Rx 正确识别了 Tx 在 EOF 拍上写入的 EOFE」。LFSR 数据本身仍逐拍匹配，故 `errWordCnt=0`。

**5) 不确定项。** 若本地 GHDL/cocotb 版本与 `pip_requirements.txt` 不一致，`import` 或仿真可能报错；以本地实际工具链为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1.** 若把 `SsiPrbsTx` 的 `PRBS_INCREMENT_G` 设为 `true`，`SsiPrbsRx` 还能正常校验吗？为什么？

> **答：** `cntData` 会变成 `'1'`，Tx 在 `DATA_S` 改发自增计数器（`dataCnt`）而非 LFSR 输出。此时 Rx 的 LFSR 预测与实收不再相符，`errWordCnt` 会逐拍增长。所以「自检」语义失效——`PRBS_INCREMENT_G=true` 是一种「已知非 PRBS」的调试模式，不能与默认 Rx 配对做比特级校验。

**练习 2.** 为什么只复位 Tx（不复位 Rx）会让 Rx 报 `errMissedPacket`？

> **答：** Tx 复位会把它内部的 `eventCnt` 回到初值（1），而 Rx 仍记得上一次对齐后的期望事件号。下一帧 Tx 发出的小事件号 ≠ Rx 期望的大事件号，连号断裂，故 `errMissedPacket=1`；随后 Rx 用「实收+1」重新对齐，链路自愈。这正是 `test_SsiPrbs.py` 里 `pulse_fast_reset` 用例的原理。

**练习 3.** `errWordCnt` 为什么设了饱和值 `MAX_CNT_C`（全 1）？

> **答：** 防止一个坏帧把 32 位计数器溢出回 0 而掩盖故障规模。饱和到全 1 表示「错误多到数不清」，是保守的可观测上界。

---

### 4.2 帧过滤器：SsiIbFrameFilter 与 SsiObFrameFilter

#### 4.2.1 概念说明

SSI 的帧不变量有三条：每帧恰有一个 SOF、每帧内 VC（TDEST）不变、帧与帧之间不交错。但真实上游（尤其是不可信的链路或软件 DMA）可能送来散字、重复 SOF、或一帧中途切 VC。如果把这些直接灌进 `SsiFifo` 里的 AXI-Stream FIFO，会污染下游、甚至让按帧语义工作的逻辑（如 `SsiPrbsRx`、SRP、PGP）错乱。

`SsiIbFrameFilter`（入向）和 `SsiObFrameFilter`（出向）就是 FIFO 两侧的「帧海关」：

- **入向过滤器**面向上游用户，职责是「无论上游多脏，下游 FIFO 只看到合法 SSI 帧」——把脏数据要么丢弃，要么改造成带 EOFE 的合法短帧。
- **出向过滤器**面向下游 FIFO 读口，职责包括「可选地丢弃已被标 EOFE 的错误帧」并再次强制帧不变量。

#### 4.2.2 核心流程

**SsiIbFrameFilter（4 态：IDLE_S / BLOWOFF_S / MOVE_S / INSERT_EOFE_S）。**

```text
IDLE_S ── 无 SOF 的散字 ──► BLOWOFF_S（丢弃到 EOF）
     │                        ▲
     ├── 正常 SOF ──► MOVE_S ─┘ (EOF 回 IDLE)
     │                  │
     │                  ├── 重复 SOF / VC 变化 ──► 截断成 EOFE 帧 → BLOWOFF_S
     │                  └── 下游溢出(无 tReady) ──► INSERT_EOFE_S（补一个 EOFE 收尾字）
     └── 下游溢出时收到字 ──► 丢字、置 overflow
```

要点：
- **丢散字**：IDLE 态收到无 SOF 的有效字，认为是「没有帧头的孤儿」，置 `wordDropped`/`frameDropped` 后进 `BLOWOFF_S` 一路丢到 EOF。
- **截断畸形帧**：MOVE 态若再出现 SOF（重复 SOF）或 `tDest` 与帧首不同（VC 交错），立刻把当前字改成 `tLast=1 + EOFE=1 + SOF=0`，把畸形帧「拦腰截断」成一个合法的 EOFE 短帧，剩余部分进 `BLOWOFF_S` 丢弃。
- **溢出收尾**：若下游反压导致字丢失（`SLAVE_READY_EN_G=false` 且 FIFO 满），进 `INSERT_EOFE_S` 补写一个 EOFE 的 EOF 字，保证下游看到的帧边界完整。
- **上报**：`sAxisCtrl.overflow`、`sAxisDropWord`、`sAxisDropFrame` 三个脉冲供监控使用。

**SsiObFrameFilter（3 态：IDLE_S / BLOWOFF_S / MOVE_S）。**

```text
IDLE_S ── 无 SOF 或 FIFO 标记 EOFE(VALID_THOLD_G=0) ──► BLOWOFF_S（丢整帧）
     └── 正常 SOF ──► MOVE_S ── 重复 SOF / VC 变化 ──► 截断成 EOFE 帧 → IDLE
```

要点：
- 当 `VALID_THOLD_G=0` 时，过滤器会从 FIFO 的 `sTLastTUser` 缓存里读末拍 EOFE；若一帧在 FIFO 里就被标了 EOFE，出向过滤器**直接整帧丢弃**（`BLOWOFF_S`），不把错误帧往下游送。
- 同样会在 MOVE 态强制「重复 SOF / VC 变化」时截断成 EOFE 帧。
- 末尾还接了一级 `AxiStreamPipeline` 做时序整形。

> 对照记：**入向**把脏数据「改造/丢弃成合法帧」喂给 FIFO；**出向**把 FIFO 里「已被标错的帧」可选地丢掉、再做一次出帧把关。二者方向对称、职责互补。

#### 4.2.3 源码精读

**入向：无 SOF 散字被丢弃。** IDLE 态若无 SOF，则置错误脉冲并视情况进 `BLOWOFF_S`：

[protocols/ssi/rtl/SsiIbFrameFilter.vhd:159-172](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiIbFrameFilter.vhd#L159-L172) — `else`（无 SOF）分支置 `wordDropped/frameDropped`，`tLast='0'` 时进 `BLOWOFF_S`。

**入向：重复 SOF 或 VC 变化 → 截断成 EOFE 帧。** 这是过滤器最具代表性的一段：

[protocols/ssi/rtl/SsiIbFrameFilter.vhd:226-248](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiIbFrameFilter.vhd#L226-L248) — 条件 `if (sof = '1') or (CHECK_TDEST_C and (r.tDest /= sAxisMaster.tDest(...)))` 命中时，`v.master.tLast:='1'`、`ssiSetUserEofe(...,'1')`、`ssiSetUserSof(...,'0')`，把当前字改造成 EOFE 的 EOF，剩余进 `BLOWOFF_S`。

**入向：溢出时补一个 EOFE 收尾字。** 保证下游帧边界完整：

[protocols/ssi/rtl/SsiIbFrameFilter.vhd:252-281](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiIbFrameFilter.vhd#L252-L281) — `INSERT_EOFE_S` 在 `v.master.tValid='0'` 时写一个 `tLast=1 + EOFE=1` 的字再回 `IDLE_S`。

**入向：overflow 上报是「本地溢出 OR 下游溢出」。** 让上游反压感知到任何一处的拥塞：

[protocols/ssi/rtl/SsiIbFrameFilter.vhd:285-290](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiIbFrameFilter.vhd#L285-L290) — `sAxisCtrl.overflow <= r.overflow or mAxisCtrl.overflow`，并输出 `sAxisDropWord/sAxisDropFrame`。

**出向：`VALID_THOLD_G=0` 时从缓存读 EOFE 并丢弃错误帧。**

[protocols/ssi/rtl/SsiObFrameFilter.vhd:111-141](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiObFrameFilter.vhd#L111-L141) — `if VALID_THOLD_G=0 then v.eofe := sTLastTUser(SSI_EOFE_C)`；IDLE 态 `if (v.sof='0') or (v.eofe='1')` 则丢帧进 `BLOWOFF_S`。

**出向：重复 SOF / VC 变化 → 截断成 EOFE 帧。** 与入向 MOVE 态同构：

[protocols/ssi/rtl/SsiObFrameFilter.vhd:191-210](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiObFrameFilter.vhd#L191-L210) — `if (v.sof='1') or (r.tDest /= sAxisMaster.tDest)` 命中时同样 `tLast:=1`、`ssiSetUserEofe(...,'1')`、`ssiSetUserSof(...,'0')`。

**EOFE/SOF 比特位与读写过程。** 过滤器所有「置/清 SOF、置 EOFE」都经 `SsiPkg`，不直接碰 TUSER 物理位，从而与 `TUSER_MODE_C` 解耦：

[protocols/ssi/rtl/SsiPkg.vhd:29-30](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPkg.vhd#L29-L30) — `SSI_EOFE_C := 0`、`SSI_SOF_C := 1`；

[protocols/ssi/rtl/SsiPkg.vhd:272-296](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPkg.vhd#L272-L296) — `ssiSetUserEofe` / `ssiGetUserSof` / `ssiSetUserSof` 均委托给 `axiStreamSetUserBit/GetUserBit`。

#### 4.2.4 代码实践：用测试理解入向过滤器的「截断成 EOFE」

**1) 实践目标。** 通过既有回归测试，确认 `SsiIbFrameFilter` 在收到「重复 SOF」或「帧中途换 VC」时，确实把畸形帧截断成带 EOFE 的合法短帧，而非把脏数据透传给下游。

**2) 操作步骤。**

- 打开入向过滤器的专用测试：

  路径 `tests/protocols/ssi/test_SsiIbFrameFilter.py`（仓库已存在，见本讲源码地图的同类 glob 结果）。阅读其 `Test methodology` 头，找到「重复 SOF」「VC 交错」用例对应的激励构造方式。

- 运行该测试：

```bash
make MODULES=$PWD import
.venv/bin/python -m pytest tests/protocols/ssi/test_SsiIbFrameFilter.py -q
```

- 对照源码行 [protocols/ssi/rtl/SsiIbFrameFilter.vhd:226-248](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiIbFrameFilter.vhd#L226-L248)，确认测试里「畸形帧」激励对应的输出端确实出现 `tLast=1` 且 EOFE 位的断言。

**3) 需要观察的现象。** 下游 master 口收到的帧以 EOFE 结尾、长度被截短；`sAxisDropWord`/`sAxisDropFrame` 在丢弃段出现脉冲。

**4) 预期结果。** 下游 FIFO 永远只收到「单 SOF、单 VC、合法 EOF」的帧；任何畸形都被改造成 EOFE 短帧或被整段丢弃。具体断言以测试文件为准（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1.** 为什么入向过滤器选择「截断成 EOFE 帧」而不是「整帧丢弃」？

> **答：** 因为下游（如 `SsiPrbsRx`、SRP）按帧边界工作。整帧丢弃会让下游「看不见任何结束」，可能挂等 EOF；而补一个 EOFE 的 EOF，既给出了明确帧边界，又用 EOFE 标注「这帧不可信」，让下游能据此报错或丢数，不破坏帧层同步。

**练习 2.** `SsiObFrameFilter` 在 `VALID_THOLD_G=0` 与 `=1`（默认）下行为有何不同？

> **答：** `=0` 时它会从 FIFO 的 `sTLastTUser` 缓存读取末拍 EOFE，并**主动丢弃**已被标 EOFE 的错误帧（IDLE 态 `v.eofe='1'` 即进 `BLOWOFF_S`）；`=1`（默认）时不读缓存、`v.eofe` 恒为 0，错误帧会照常送往下游（由下游自行处理 EOFE）。

**练习 3.** 入向过滤器的 `sAxisCtrl.overflow` 为何要 OR 上 `mAxisCtrl.overflow`？

> **答：** 让上游同时感知「本过滤器丢字」与「再下游 FIFO 也满了」两种拥塞，形成端到端的反压可见性，避免上游只看到局部信号而持续灌数据。

---

### 4.3 帧长限制：SsiFrameLimiter

#### 4.3.1 概念说明

即便帧合法，仍可能出问题：一个失控的生产者可能发一个「无限长」的帧，或一个死掉的逻辑让一帧永远不结束。这会占死下游缓冲、甚至让 DMA/解析器耗尽内存。`SsiFrameLimiter` 给 SSI 流加两道防御性闸门：

- **最大帧长 `FRAME_LIMIT_G`**（单位是 master 口的字数）：超过即强行截断并标 EOFE。
- **超时 `TIMEOUT_G`**（秒，配合 `MAXIS_CLK_FREQ_G` 换算成周期）：一帧移动到一半若长时间无进展，强行截断并标 EOFE。

它和过滤器的区别：过滤器针对「帧不合法」，限制器针对「帧合法但过大/过慢」。二者常串联使用。

#### 4.3.2 核心流程

`SsiFrameLimiter` 主体是一个 2 态状态机 `IDLE_S / MOVE_S`，外围按需裹输入/输出 FIFO 或 gearbox。

```text
        ┌──────── 可选输入级 ────────┐
sAxis ─►│ 异构时钟/位宽/反压时插 FIFO │─► rxMaster
        │   否则直接 AxiStreamGearbox │
        └────────────────────────────┘
                     │
              ┌──────▼──────┐
              │  IDLE_S     │  等 SOF，cnt=1
              │  → MOVE_S   │
              └──────┬──────┘
                     │
              ┌──────▼──────────────────────────────┐
              │ MOVE_S：每字 cnt++                   │
              │   重复 SOF ──► 截断 EOFE → IDLE      │
              │   正常 EOF ──► IDLE                  │
              │   cnt=FRAME_LIMIT_G-1 ──► 截断 EOFE  │
              │   (超时计时到) ──► 截断 EOFE          │
              └──────────────────────────────────────┘
                     │
        ┌──────── 可选输出级 ────────┐
txMaster├►│ MASTER_FIFO_G=true 时加 FIFO │─► mAxis
        └────────────────────────────┘
```

超时的换算：

\[
\text{TIMEOUT\_C} = \mathrm{getTimeRatio}(\text{MAXIS\_CLK\_FREQ\_G} \times \text{TIMEOUT\_G},\ 1.0)
\]

即把「时钟频率 × 秒数」转成周期数。默认 \(156.25\,\text{MHz} \times 1\,\text{ms} \approx 156250 \) 个周期。计时器只在「非 IDLE」期间累加，一旦回到 IDLE（或即将回 IDLE）就清零；计满后若仍能写出一字，就补一个 EOFE 的 EOF。

#### 4.3.3 源码精读

**超时常量与是否需要输入 FIFO 的判定。**

[protocols/ssi/rtl/SsiFrameLimiter.vhd:57-59](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiFrameLimiter.vhd#L57-L59) — `TIMEOUT_C` 换算周期数；`SLAVE_FIFO_C` 在「跨时钟域 / 非公共时钟 / 非 slave-ready」时任一为真即强制加输入 FIFO。

**输入级：无 FIFO 时用 gearbox 做位宽整形。**

[protocols/ssi/rtl/SsiFrameLimiter.vhd:89-140](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiFrameLimiter.vhd#L89-L140) — `BYPASS_FIFO_RX` 实例化 `AxiStreamGearbox`；`GEN_FIFO_RX` 实例化 `AxiStreamFifoV2`（`GEN_SYNC_FIFO_G => COMMON_CLK_G`）。

**MOVE 态：三道终止条件 + 计数。**

[protocols/ssi/rtl/SsiFrameLimiter.vhd:181-209](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiFrameLimiter.vhd#L181-L209) — 重复 SOF（`sofDet='1'`）→ 截断 EOFE；正常 `tLast` → 回 IDLE；`r.cnt = FRAME_LIMIT_G-1` → 截断 EOFE；否则 `v.cnt := r.cnt + 1`。注意截断时一律 `ssiSetUserEofe(MASTER_AXI_CONFIG_G, v.txMaster, '1')`。

**超时闸门。**

[protocols/ssi/rtl/SsiFrameLimiter.vhd:214-237](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiFrameLimiter.vhd#L214-L237) — `EN_TIMEOUT_G` 为真时，在（或即将进入）IDLE 清零计时器，否则累加；计到 `TIMEOUT_C-1` 仍能写一字时，补 `tLast=1 + EOFE=1` 并回 IDLE。

**可选输出 FIFO。**

[protocols/ssi/rtl/SsiFrameLimiter.vhd:264-297](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiFrameLimiter.vhd#L264-L297) — `MASTER_FIFO_G=true` 时在输出加一级同步 `AxiStreamFifoV2`。

#### 4.3.4 代码实践：改 `FRAME_LIMIT_G` 观察截断行为

**1) 实践目标。** 通过既有回归测试与参数扫描，验证：当一帧长度超过 `FRAME_LIMIT_G` 时，输出帧会在第 `FRAME_LIMIT_G` 字被强行截断并标注 EOFE。

**2) 操作步骤。**

- 运行既有测试（仓库已提供 `tests/protocols/ssi/test_SsiFrameLimiter.py` 与 `test_SsiFrameLimiterPreserve.py`）：

```bash
make MODULES=$PWD import
.venv/bin/python -m pytest tests/protocols/ssi/test_SsiFrameLimiter.py tests/protocols/ssi/test_SsiFrameLimiterPreserve.py -q
```

- 阅读测试的 `Test methodology` 头，找到「发送一帧长度 > `FRAME_LIMIT_G`」的用例，对照源码 [protocols/ssi/rtl/SsiFrameLimiter.vhd:199-205](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiFrameLimiter.vhd#L199-L205) 确认截断点。

- 若要亲手观察参数影响，可在测试的 `PARAMETER_SWEEP` 里新增一组小 `FRAME_LIMIT_G`（例如 4）并用例发送 8 字帧（这是修改测试代码，不是修改源码，符合本讲约束）。

**3) 需要观察的现象。** 输出帧长度恰为 `FRAME_LIMIT_G` 字，末字 `tLast=1` 且 EOFE 位为 1；计数 `cnt` 从 1 计到 `FRAME_LIMIT_G-1` 后触发截断。

**4) 预期结果。** 超长帧被限制器「削」成 `FRAME_LIMIT_G` 字的 EOFE 帧；正常短帧（长度 ≤ 限制）原样通过、EOFE 不置位。具体断言以测试文件为准（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1.** `FRAME_LIMIT_G` 的单位是字节还是字？

> **答：** 是 master 口的「字（word）」数，对应 `MASTER_AXI_CONFIG_G.TDATA_BYTES_C` 宽度的一拍。源码注释 `In units of MASTER_AXI_CONFIG_G.TDATA_BYTES_C` 即此意，`cnt` 每移动一个 axis 字自增 1。

**练习 2.** 若 `COMMON_CLK_G=false`，输入级一定会加 FIFO 吗？

> **答：** 会。`SLAVE_FIFO_C` 在 `COMMON_CLK_G=false` 时为真，走 `GEN_FIFO_RX` 分支用 `AxiStreamFifoV2` 做跨时钟域；只有公共时钟、使能 slave-ready 且未强制开 `SLAVE_FIFO_G` 时，才走 `BYPASS_FIFO_RX` 的 gearbox 直通。

**练习 3.** 超时闸门为何在「即将进入 IDLE」时也清零计时器？

> **答：** 条件 `(r.state = IDLE_S) or (v.state = IDLE_S)` 同时覆盖「已在 IDLE」和「本拍正常 EOF 即将回 IDLE」。这样合法帧的正常结束不会被误判为超时，避免在帧尾多塞一个 EOFE。

---

## 5. 综合实践：搭一条「带海关与限速」的 PRBS 自检链路

把本讲三个模块串起来，设计一条更贴近真实工程的链路自检通路：

```text
           ┌──────────┐    ┌────────────┐    ┌──────────┐    ┌────────────┐    ┌──────────┐
 trig ───► │ SsiPrbsTx│──► │SsiIbFrame  │──► │ SsiFrame │──► │SsiObFrame │──► │ SsiPrbsRx│──► 错误统计
 forceEofe │  (造帧)  │    │ Filter(入) │    │ Limiter  │    │ Filter(出) │    │  (校验)  │
           └──────────┘    └────────────┘    └──────────┘    └────────────┘    └──────────┘
```

**任务。**

1. **说明数据语义连贯性。** Tx 在 EOF 拍用 `ssiSetUserEofe(..., r.overflow)` 写 EOFE（[SsiPrbsTx.vhd:344](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPrbsTx.vhd#L344)）。请叙述这一位 EOFE 沿途会如何被各模块对待：入向过滤器是否会改写它？限制器在正常（未超长未超时）情况下是否保留它？出向过滤器在 `VALID_THOLD_G=0` 时是否会因此丢帧？Rx 又如何把它汇总到 `errEofe`（[SsiPrbsRx.vhd:466-471](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPrbsRx.vhd#L466-L471)）？

2. **预测两类故障的观测现象。**
   - 在 Tx 拉高 `forceEofe` 发一帧：预测 Rx 的五类错误位中哪些为 1、`errWordCnt` 是否仍为 0，并解释原因（提示：LFSR 序列本身未被破坏）。
   - 把 `SsiFrameLimiter` 的 `FRAME_LIMIT_G` 设成小于 Tx 的 `packetLength`：预测 Rx 会看到哪几类错误同时置位（提示：帧被截断 → 既触发 EOFE，又触发 `errLength`，因为 `dataCnt ≠ packetLength`）。

3. **运行验证。** 以 `SsiPrbsWrapper` + `test_SsiPrbs.py` 为蓝本，运行前述 PRBS 回归确认第 1 类故障的预测；第 2 类（限制器截断）可在阅读 `test_SsiFrameLimiter.py` 后描述其激励与预期，不要求改源码。

```bash
make MODULES=$PWD import
.venv/bin/python -m pytest tests/protocols/ssi/test_SsiPrbs.py tests/protocols/ssi/test_SsiFrameLimiter.py -q
```

**预期结论。** EOFE 是一条贯穿全链路的「错误语义总线」：Tx 可主动注入、过滤器/限制器可在故障点补盖、Rx 据此汇总报错。正常数据则一路透传且 LFSR 逐拍吻合，`errWordCnt=0`。这条「PRBS + 帧海关 + 限速器」的组合，正是 SURF 数据链路回归测试的典型骨架。

> 本综合实践中关于「运行命令」的部分受本地工具链（GHDL/cocotb/ruckus 版本）影响，具体标志以 `tests/README.md`、`pytest.ini`、`pip_requirements.txt` 为准，待本地验证。

## 6. 本讲小结

- `SsiPrbsTx` 用「事件号种子 + LFSR」产出三段式 SSI 帧（种子字 / 长度字 / 数据字），末拍据 `forceEofe` 或 FIFO 溢出写 EOFE；最小帧长钳位到 2。
- `SsiPrbsRx` 用同款 LFSR 在 `LENGTH_S` 前进一步对齐 Tx 的 `SEED_RAND_S`，再逐拍比对，输出丢包/长度/数据总线/EOFE/字错五类错误，结果经 `SynchronizerFifo` + `SyncStatusVector` 跨域上报。
- 收发两端 LFSR 之所以逐拍吻合，是因为种子（事件号）相同、每数据字前进步数相同；这是「无需缓存期望数据」的确定性自检基础。
- `SsiIbFrameFilter` 在 FIFO 入向强制 SSI 帧不变量：丢散字、把重复 SOF / VC 交错截断成 EOFE 帧、溢出时补 EOFE 收尾字。
- `SsiObFrameFilter` 在 FIFO 出向可（`VALID_THOLD_G=0` 时）丢弃已被标 EOFE 的错误帧，并再次把关帧不变量。
- `SsiFrameLimiter` 用 `FRAME_LIMIT_G`（字数）与 `TIMEOUT_G`（秒→周期）两道闸门，把超长/挂死帧截断成 EOFE 帧，是防御性限速器。
- EOFE 是贯穿全链路的统一错误语义：Tx 可注入、过滤器/限制器可补盖、Rx 据此汇总——这条语义总线是 SURF 链路自检的核心约定。

## 7. 下一步学习建议

- **进入 SRP（u5-l3）**：SRPv3 把 AXI-Lite 寄存器事务封装进 SSI 帧，本讲的「帧边界 / EOFE / VC」正是其承载体；学 SRP 时你会再次看到 `SsiPrbsTx/Rx` 被用作链路回归激励。
- **阅读 `SsiFifo`（u5-l1）**：把本讲的入向/出向过滤器放回「过滤→FIFO→过滤」的三明治里整体理解，并对照 `SsiFrameLimiter` 与 `SsiFifo` 的 `VALID_THOLD_G` / 帧级门控异同。
- **对照 PGP（u7-l1）**：PGP 用 TDEST/TVC 做虚拟通道路由，其回归测试大量使用 `SsiPrbsTx/Rx`；学完 PGP 可回头看本讲 PRBS 对如何被挂到多 VC 链路上。
- **动手扩展**：仿照 `test_SsiPrbs.py` 的 `PARAMETER_SWEEP`，为 PRBS 对增加一组「`PRBS_INCREMENT_G=true` 应被 Rx 报字错」的用例，检验你对 LFSR 对齐机制的理解。
