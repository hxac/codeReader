# Packetizer / Batcher 与线路码、ECC

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `AxiStreamPacketizer`（V0）如何给一条 AXI-Stream 加「帧头 + 帧尾」、如何把超长帧拆成多个带序号的包，以及 `AxiStreamDepacketizer` 如何还原侧带。
- 说清楚 `AxiStreamBatcher`（V1/V2）如何把多个「子帧」聚合成一个「超级帧」，以及字节阈值、子帧数、时钟间隔三种触发超级帧结束的条件。
- 掌握 8B/10B 线路码的「不均度（running disparity）」原理，会用 `Code8b10bPkg` 的 `encode8b10b`/`decode8b10b` 过程以及 `Encoder8b10b`/`Decoder8b10b` 模块。
- 理解扩展汉明码（SEC-DED）「单比特纠正、双比特检测」的数学原理，能读懂 `HammingEccPkg` 的编解码函数与 `errSbit`/`errDbit` 两个错误标志的语义。
- 能把「成帧 / 聚合 → 线路码 → 纠错」三类「数据格式化与保护」积木对应到真实协议核中的使用位置。

## 2. 前置知识

本讲是 u5 单元的第 4 篇，承接以下已建立的认知（不再重复）：

- **u4-l1 AXI-Stream 记录与配置**：`AxiStreamMasterType`/`AxiStreamSlaveType` 记录、`AxiStreamConfigType`（含 `TDATA_BYTES_C`、`TKEEP_MODE_C`、`TUSER_MODE_C`），以及 `genTKeep`/`getTKeep`、`axiStreamSetUserBit`/`Field` 等包函数。
- **u5-l1 SSI 侧带与帧边界**：SSI 把 SOF/EOFE 编码进 TUSER、EOF 复用 `tLast`、`TUSER_FIRST_LAST_C` 模式，以及 `ssiSetUserSof`/`ssiSetUserEofe`、`SSI_SOF_C`/`SSI_EOFE_C` 常量。
- **u1-l5 双进程 RTL 风格**：`RegType`/`REG_INIT_C`/`r`/`rin`/`comb`/`seq` 三明治骨架。
- **u1-l4 StdRtlPkg**：`sl`/`slv`、`log2`/`bitSize`/`ite`、`isPowerOf2`、`onesCount`、归约函数 `uOr`/`uXor`。

本讲涉及的三类模块都属于 `protocols/` 下的「数据格式化与保护辅助」族。`protocols/README.md` 把它们归为一类：

> Data formatting and protection helpers include `batcher/`, `packetizer/`, `line-codes/`, `hamming-ecc/`, and `event-frame-sequencer/`.

直觉上记住一句话即可：**Packetizer/Batcher 解决「帧怎么切、怎么合」（逻辑层），8B/10B 解决「比特在线上怎么保持直流平衡」（物理层），Hamming ECC 解决「比特翻了一位能不能纠回来」（存储/控制层）**。三者不一定串在同一条链上，但都是为「把数据安全地搬过一条不可靠通路」服务的。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用它讲什么 |
|---|---|---|
| `protocols/packetizer/rtl/AxiStreamPacketizer.vhd` | 可综合 RTL | V0 成帧：加帧头/帧尾、长帧拆包 |
| `protocols/packetizer/rtl/AxiStreamDepacketizer.vhd` | 可综合 RTL | 还原侧带、校验帧/包序号 |
| `protocols/batcher/rtl/AxiStreamBatcher.vhd` | 可综合 RTL | V1/V2 聚合：子帧→超级帧 |
| `protocols/line-codes/rtl/Code8b10bPkg.vhd` | 纯函数包 | 8B/10B 编解码算法、K 字符常量 |
| `protocols/line-codes/rtl/Encoder8b10b.vhd` | 可综合 RTL | 多字节并行 8B/10B 编码器 |
| `protocols/line-codes/rtl/Decoder8b10b.vhd` | 可综合 RTL | 多字节并行 8B/10B 解码器（含 `codeErr`/`dispErr`） |
| `protocols/line-codes/tb/LineCode8b10bTb.vhd` | 仿真外壳 | 编码器→解码器环回连线范例 |
| `protocols/hamming-ecc/rtl/HammingEccPkg.vhd` | 纯函数包 | 汉明编解码算法、奇偶位宽度计算 |
| `protocols/hamming-ecc/rtl/HammingEccEncoder.vhd` | 可综合 RTL | 汉明编码器外壳 |
| `protocols/hamming-ecc/rtl/HammingEccDecoder.vhd` | 可综合 RTL | 汉明解码器外壳（含 `errSbit`/`errDbit`） |
| `protocols/hamming-ecc/tb/HammingEccTb.vhd` | 自检测试台 | 穷举注入 0/1/2 比特错误验证 SEC-DED |

每个子目录各有自己的 `ruckus.tcl` 负责登记源码（见 u1-l2、u1-l3），本讲聚焦文件内部逻辑，不再赘述构建清单。

---

## 4. 核心概念与源码讲解

### 4.1 成帧：AxiStreamPacketizer（给流加帧头/尾、长帧拆包）

#### 4.1.1 概念说明

AXI-Stream 的侧带（`tDest`/`tId`/`tUser`）是「每拍都跟着数据走」的。但很多传输链路（PGP、以太网、光纤）只愿意搬「纯粹的字节流」，侧带根本无处安放。`AxiStreamPacketizer` 解决的就是这个问题：

> 把应用层一帧的侧带信息**塞进数据流的第一个字（帧头）**，把帧结束标记和末拍 `tUser` 塞进**最后一个字节（帧尾）**，中间只留纯数据。如果应用帧太长，超过一个包能装下的字节数，就**拆成多个带序号的包**。

文件头注释把它说得很直白：

[protocols/packetizer/rtl/AxiStreamPacketizer.vhd:6-9](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/packetizer/rtl/AxiStreamPacketizer.vhd#L6-L9) ——「Formats an AXI-Stream for a transport link. Sideband fields are placed into the data stream in a header. Long frames are broken into smaller packets.」

这是「V0」协议（`VERSION_C = "0000"`），仓库里还有一套独立的 `AxiStreamPacketizer2`/`Pkg`（V2 成帧，带 CRC），本讲只讲 V0。

#### 4.1.2 核心流程

Packetizer 内部固定按 **8 字节一个字**（`WORD_SIZE_C = 8`）工作，状态机三个状态：

```
IDLE_S ──(有输入 & 输出空闲)──> MOVE_S   发出 64 位「帧头」字
MOVE_S ──(逐字搬运数据)────────> MOVE_S   每搬一个数据字，wordCount++
   │
   ├── 达到 maxWords（包长上限）─> TAIL_S  本包到此封顶，发非 EOF 尾
   └── 输入 tLast=1（应用帧结束）> IDLE_S  尽量把尾塞进当前字；塞不下则去 TAIL_S
TAIL_S ──(输出空闲)────────────> IDLE_S   补发一个独立的「帧尾」字节，tLast=1
```

一个输出包的结构是：

```
┌──────────┬─────────────────────────────┬──────────┐
│ 帧头(1字) │ 数据字 × N (N = maxWords-3) │ 帧尾(字节)│
└──────────┴─────────────────────────────┴──────────┘
```

其中「3 个协议字」= 1（帧头）+ 1（帧尾）+ 1（安全余量，见源码注释）。应用帧若被拆成多个包，靠帧号（`frameNumber`，整个应用帧共用一个）和包号（`packetNumber`，每个包 +1）让对端 `AxiStreamDepacketizer` 能按序重组并检测丢包。

#### 4.1.3 源码精读

**固定 8 字节字宽与内部 AXIS 配置**。注意输出配置把 `TUSER_MODE_C` 设成 `TUSER_FIRST_LAST_C`（SSI 风格），`OUTPUT_SSI_G=true` 时帧头那拍会在 TUSER 里打 SOF：

[protocols/packetizer/rtl/AxiStreamPacketizer.vhd:55-72](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/packetizer/rtl/AxiStreamPacketizer.vhd#L55-L72) —— `LD_WORD_SIZE_C=3`、`WORD_SIZE_C=8`、`PROTO_WORDS_C=3`，以及 `AXIS_CONFIG_C`（8 字节数据、SSI 风格 TUSER）。

**帧头的 64 位布局**。在 `IDLE_S` 里，把输入第一拍的侧带一字段一字段地拼进输出第一个字的 `tData`：

[protocols/packetizer/rtl/AxiStreamPacketizer.vhd:182-199](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/packetizer/rtl/AxiStreamPacketizer.vhd#L182-L199) —— 帧头字段拼装 + 可选 SOF。

字段表（位段从低到高）：

| 位段 | 字段 | 来源 |
|---|---|---|
| `[3:0]` | 版本号 | 固定 `VERSION_C = 0` |
| `[15:4]` | 帧号 frameNumber（12 位） | 内部计数器 |
| `[39:16]` | 包号 packetNumber（24 位） | 内部计数器 |
| `[47:40]` | `tDest` | 输入第一拍的 tDest |
| `[55:48]` | `tId` | 输入第一拍的 tId |
| `[63:56]` | `tUser`（首拍） | 输入第一拍的 tUser |

**数据搬运**。`MOVE_S` 把输入数据原样转发，但**清零所有侧带**（因为侧带已存进帧头），并把 `tKeep` 强制成整字有效：

[protocols/packetizer/rtl/AxiStreamPacketizer.vhd:209-214](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/packetizer/rtl/AxiStreamPacketizer.vhd#L209-L214) —— 复用输入数据但抹掉 `tUser`/`tDest`/`tId`，`tKeep` 设为 `0x00FF`。

**帧尾的两种安放方式**。这是 V0 最精巧的一处。当应用帧结束（输入 `tLast=1`）时，帧尾只有 1 个字节（bit7=EOF 标志，bit[6:0]=末拍 tUser）。源码用一个 `case (tKeep)` 判断：如果当前最后一个数据字**还有空字节**，就把帧尾挤进去（同一拍同时是数据 + 尾）；如果 8 个字节都满了（`tKeep=0xFF` 的 others 分支），就只能单独发一个字：

[protocols/packetizer/rtl/AxiStreamPacketizer.vhd:238-274](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/packetizer/rtl/AxiStreamPacketizer.vhd#L238-L274) —— 按 `tKeep` 把单字节尾追加到当前字，或退到 `TAIL_S` 单独发尾。

`TAIL_S` 负责发那个独立的尾字节，并置 `tLast=1`：

[protocols/packetizer/rtl/AxiStreamPacketizer.vhd:282-295](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/packetizer/rtl/AxiStreamPacketizer.vhd#L282-L295) —— 独立尾字节：`tData(7)=eof`、`tData(6:0)=tUserLast`，`tLast=1`。

**对端：Depacketizer**。`AxiStreamDepacketizer` 是镜像，靠帧头恢复 `tDest/tId/tUser` 与 SOF，靠帧号/包号自检（帧号应 +1、包号应按序 +1，否则进入 `BLEED_S` 丢弃残帧并补 EOFE）：

[protocols/packetizer/rtl/AxiStreamDepacketizer.vhd:181-222](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/packetizer/rtl/AxiStreamDepacketizer.vhd#L181-L222) —— 从帧头恢复侧带并校验帧/包序号，错序则转 `BLEED_S`。

注意 Packetizer 的输出配置 `TDEST_BITS_C=0`、`TID_BITS_C=0`（侧带已搬进数据），而 Depacketizer 的配置 `TDEST_BITS_C=8`、`TID_BITS_C=8`（要把侧带还原回来），二者内部配置互补。

#### 4.1.4 代码实践

**实践目标**：通过现成的 cocotb 回归，亲眼看到「帧头 + 数据 + 帧尾」三种输出形态。

**操作步骤**：

1. 阅读 [tests/protocols/packetizer/test_AxiStreamPacketizer.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/protocols/packetizer/test_AxiStreamPacketizer.py)，它用扁平端口的 `AxiStreamPacketizerWrapper` 直接驱动 8 字节流。
2. 重点关注三个 `@cocotb.test()`，它们分别对应三种尾的安放方式：
   - `packetize_appended_tail_test`：15 字节帧 → 尾**追加**进最后那个没满的字（[test_AxiStreamPacketizer.py:60-105](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/protocols/packetizer/test_AxiStreamPacketizer.py#L60-L105)）。
   - `packetize_separate_tail_test`：16 字节帧 → 尾**单独**占一个字（[test_AxiStreamPacketizer.py:108-150](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/protocols/packetizer/test_AxiStreamPacketizer.py#L108-L150)）。
   - `packetize_split_frame_on_max_size_test`：把 `maxPktBytes` 设成 32，逼出「长帧拆成两个包」+ 中间一个非 EOF 尾（[test_AxiStreamPacketizer.py:153-210](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/protocols/packetizer/test_AxiStreamPacketizer.py#L153-L210)）。
3. 在配置好 cocotb+GHDL+ruckus 的环境下运行（工具链细节见 u9-l1）：

   ```bash
   make MODULES=$PWD import
   ./.venv/bin/python -m pytest -q tests/protocols/packetizer/test_AxiStreamPacketizer.py
   ```

**需要观察的现象**：

- 第一个输出字的低 64 位正好是 `packetizer0_header_word(frame, packet, tdest, tid, tuser)` 拼出的值，且 `user=0x2`（SSI SOF 位）。
- 拆包测试里，第一个包的尾 `first_tail` 的 EOF 位是 0、第二个包的尾 `final_tail` 的 EOF 位是 1；第二个包的帧号不变、包号从 0 变 1。

**预期结果**：三个测试全部通过。若本地未搭好仿真环境，此项**待本地验证**，但可先靠读测试断言理解行为（不假装已运行）。

#### 4.1.5 小练习与答案

**练习 1**：把 `MAX_PACKET_BYTES_G` 从默认 1440 改成 16，一帧 40 字节的输入会被拆成几个包？每个包含几个数据字？

**参考答案**：内部字宽 8 字节，`MAX_PACKET_BYTES_G=16` → 每包总字数 `16/8=2`，扣掉 `PROTO_WORDS_C=3` 后数据字预算为负 → 触发 [AxiStreamPacketizer.vhd:168](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/packetizer/rtl/AxiStreamPacketizer.vhd#L168) 的 `fits := false`，模块会卡住不输出。**结论**：`MAX_PACKET_BYTES_G` 必须足够大（远大于 `3 × 8 = 24` 字节），否则装不下协议开销。源码的 assert 只校验「是 8 的倍数」，不校验下限，这是隐藏陷阱。

**练习 2**：为什么 `MOVE_S` 里要把 `tKeep` 强制成 `0x00FF`（整字有效）？

**参考答案**：因为 V0 协议把「末拍有效字节位置」信息编码进了**帧尾字节本身的位置**（用 `MIN_TKEEP_G` 配合 `case tKeep` 还原），而不是靠逐拍的 `tKeep` 传递。中间数据字必须是整字，末拍的 `tKeep` 只在生成尾时被消费一次。

---

### 4.2 聚合：AxiStreamBatcher（多子帧合并成超级帧）

#### 4.2.1 概念说明

很多应用产生大量**小帧**（例如每个触发事件一帧），如果每帧都独立走链路，帧头/帧尾的协议开销会吃掉大量带宽。`AxiStreamBatcher` 解决相反方向的问题：

> 把 N 个连续的「子帧」**装进一个「超级帧」**里。超级帧开头加一个超级帧头，每个子帧末尾加一个子帧尾（记录这子帧的字节数、tDest、首末 tUser），最后用 `tLast` 结束整个超级帧。

注释一句话概括：

[protocols/batcher/rtl/AxiStreamBatcher.vhd:7-8](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/batcher/rtl/AxiStreamBatcher.vhd#L7-L8) ——「combines sub-frames into a larger super-frame」。

它有两个版本：**V1**（零填充，每个头/尾独占整字，要求字宽是 2 的幂）和 **V2**（无零填充，用 `tKeep` 标真实字节，要求字宽 ≥ 8）。

#### 4.2.2 核心流程

状态机比 Packetizer 复杂，核心是「超级帧头 → 子帧搬运 → 子帧尾 → 决定继续还是收尾」：

```
HEADER_S      发超级帧头（version + width + seqCnt），置 SOF
   │
   ▼
SUB_FRAME_S   逐字搬子帧数据，累计 superByteCnt、subByteCnt；遇到子帧 tLast → TAIL_S
   │
   ▼
TAIL_S        发子帧尾（subByteCnt/tDest/tUserFirst/tUserLast），再调 doTail 判定：
   ├── 触发超级帧结束 ─> tLast=1，回 HEADER_S
   ├── 还有新子帧     ─> 回 SUB_FRAME_S
   └── 暂时没数据     ─> GAP_S（等数据或超时）
GAP_S         来新数据 → 回 SUB_FRAME_S；clkGap 超时 → 强制 tLast 收尾，回 HEADER_S
```

**触发超级帧结束的三种条件**（在 `doTail` 里集中判断）：

1. **字节阈值**：累计 `superByteCnt` 达到 `superFrameByteThreshold`（默认 8192 字节）。
2. **子帧数上限**：`subFrameCnt` 达到 `maxSubFrames`（默认 32）。
3. **外部强制**：`forceTerm=1`（此时还会在 `tLast` 那拍置 EOFE）。
4. **空闲超时**：`clkGapCnt` 达到 `maxClkGap`（默认 256 拍，在 `GAP_S` 处理）。

#### 4.2.3 源码精读

**超级帧头**。在 `HEADER_S`，把版本、字宽编码、序列号写进低 16 位；V2 用 `genTKeep(2)` 标记只有 2 字节有效（不零填充）：

[protocols/batcher/rtl/AxiStreamBatcher.vhd:222-240](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/batcher/rtl/AxiStreamBatcher.vhd#L222-L240) —— 超级帧头：`tData[3:0]=VERSION_G`、`[7:4]=WIDTH_C`、`[15:8]=seqCnt`，V2 下 `tKeep=genTKeep(2)`。

其中 `WIDTH_C = log2(AXIS_WORD_SIZE_C/2)` 是字宽的编码（仅 V1 用）。

**子帧尾**。在 `TAIL_S`，把该子帧的元数据写进低 7 字节；V2 用 `genTKeep(7)`：

[protocols/batcher/rtl/AxiStreamBatcher.vhd:287-299](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/batcher/rtl/AxiStreamBatcher.vhd#L287-L299) —— 子帧尾：`[31:0]=subByteCnt`、`[39:32]=tDest`、`[47:40]=tUserFirst`、`[55:48]=tUserLast`、`[59:56]=WIDTH_C`，V2 下 `tKeep=genTKeep(7)`。

**`doTail` 过程**：超级帧结束判定的集中点。注意它是一个定义在 `comb` 进程内部的 **procedure**，被 `TAIL_S` 和强制终止两处复用：

[protocols/batcher/rtl/AxiStreamBatcher.vhd:148-170](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/batcher/rtl/AxiStreamBatcher.vhd#L148-L170) —— 任一结束条件命中则 `tLast=1`、`forceTerm` 时置 EOFE、回 `HEADER_S`；否则有数据续搬、无数据进 `GAP_S`。

**窄字宽的尾分块**。当字宽是 2 或 4 字节时，7 字节的子帧尾（V2）放不下一个字，需要 `CHUNK_TAIL_2BYTE_S`/`CHUNK_TAIL_4BYTE_S` 把尾**移位分多次发**：

[protocols/batcher/rtl/AxiStreamBatcher.vhd:323-359](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/batcher/rtl/AxiStreamBatcher.vhd#L323-L359) —— 窄字宽下把尾数据逐字左移输出。

**V1 vs V2 的输出级**。V1 直接接 `AxiStreamPipeline`；V2 因为头/尾不是整字、要和后续子帧数据**无缝压紧**，所以输出级接的是 `AxiStreamGearbox`（FORCE_GEARBOX_IMPL_G=true）做字节打包：

[protocols/batcher/rtl/AxiStreamBatcher.vhd:425-457](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/batcher/rtl/AxiStreamBatcher.vhd#L425-L457) —— V1 用 `AxiStreamPipeline`，V2 用 `AxiStreamGearbox` 压紧字节。

三个 elaboration 期 assert 限定了版本与字宽的搭配：

[protocols/batcher/rtl/AxiStreamBatcher.vhd:120-127](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/batcher/rtl/AxiStreamBatcher.vhd#L120-L127) —— V1 要 2 的幂字宽，V2 要 ≥8 字节字宽。

#### 4.2.4 代码实践

**实践目标**：理解超级帧「何时结束」的四种触发，并能复现「子帧数上限」触发。

**操作步骤**（源码阅读型 + 可选仿真）：

1. 阅读 [tests/protocols/batcher/test_AxiStreamBatcher.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/protocols/batcher/test_AxiStreamBatcher.py)，看它如何构造多个子帧、设置 `maxSubFrames`/`superFrameByteThreshold`/`maxClkGap`。
2. 在脑中（或纸上）追踪这样一个场景：`VERSION_G=2`、`DATA_BYTES_G=8`、`maxSubFrames=2`，连续送 3 个各 8 字节的子帧。
3. 可选运行：

   ```bash
   ./.venv/bin/python -m pytest -q tests/protocols/batcher/test_AxiStreamBatcher.py
   ```

**需要观察的现象**：

- 前 2 个子帧被装进第 1 个超级帧（`subFrameCnt` 到 2 触发 `maxSubFramesDet`），该超级帧以 `tLast=1` 结束。
- 第 3 个子帧开启第 2 个超级帧，`seqCnt` 自增。
- 每个子帧后面都跟一个 7 字节的子帧尾（V2）。

**预期结果**：能画出两个超级帧的字节布局草图，标出超级帧头、各子帧数据、各子帧尾、超级帧 tLast 位置。仿真通过情况**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 V2 要求字宽 ≥ 8 字节，而 V1 只要求 2 的幂？

**参考答案**：V2 的子帧尾固定是 7 字节，要放进一个字里并留出空间，字宽至少要能容纳这些字节，故 ≥8；同时 V2 靠 `AxiStreamGearbox` 把非整字的头/尾与数据压紧，需要足够宽的数据通路。V1 用零填充，每个头/尾独占整字，只要字宽是 2 的幂就能用移位逻辑（`CHUNK_TAIL_*`）分块，2 字节也能工作。

**练习 2**：`superFrameByteThreshold` 在 `HEADER_S` 里被「按字取整」（低位清零）。为什么？

**参考答案**：见 [AxiStreamBatcher.vhd:208-215](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/batcher/rtl/AxiStreamBatcher.vhd#L208-L215)，注释写明「remove the `>` operator」。把阈值对齐到整字边界后，判定只需做相等比较（`superByteCnt = superFrameByteThreshold`），省去一个大于比较器，改善时序。

---

### 4.3 线路码：8B/10B 编解码

#### 4.3.1 概念说明

8B/10B 把每个 8 位字节编成 10 位线路符号。它的两大作用：

1. **直流平衡（DC balance）**：保证线上 0 和 1 的数量长期相等，使接收端的交流耦合电容不饱和、判决阈值稳定。靠「不均度（Running Disparity, RD）」机制实现——编码器跟踪当前 0/1 失衡，对同一个字节在两种编码间选择，使 RD 总在 ±1 之间来回。
2. **控制字符与定界**：12 个特殊的 **K 字符**（K28.0~K28.7、K23.7、K27.7、K29.7、K30.7）不对应普通数据，用于帧定界、对齐、空闲。其中 K28.1、K28.5、K28.7 含「逗号（comma）」序列，接收端靠它做字对齐。

算法上把 8 位拆成 **5B/6B + 3B/4B** 两段独立编码。`Code8b10bPkg` 是一个**纯函数包**（无状态、可综合），是全仓库 8B/10B 的单一事实来源——`Code10b12bPkg`、PGP2b、JESD204B 等都直接复用它。

#### 4.3.2 核心流程

**编码** `encode8b10b(dataIn, dataKIn, dispIn, dataOut, dispOut)`：

```
dispIn (当前RD) ──> 5B/6B 编码 ──> 中间 disp6 ──> 3B/4B 编码 ──> dispOut (新RD)
                         │                              │
                         └── 按需对 6 位取反 ──┐         └── 按需对 4 位取反
                                                  └── 合成 10 位 dataOut
```

- `dispIn`/`dispOut` 是单比特 RD（约定 '1'/'0' 代表两种极性）。
- `compls6`/`compls4` 是「是否对 6B/4B 部分取反」的标志，由当前 RD 和码字自身的失衡方向共同决定，保证输出后 RD 向反方向拉。
- `illegalk` 标志识别非法 K 码（只允许那 12 个）。

**解码** `decode8b10b(...)` 多输出两个错误标志：

- `codeErr`：收到的 10 位不是任何合法码字（含非法 K）。
- `dispErr`：码字本身合法，但与传入 RD 不符（暗示链路上有过跳变，即「隐性错误」）。

**多字节并行**。`Encoder8b10b`/`Decoder8b10b` 用 `NUM_BYTES_G` 把 N 个字节并行编/解码，关键在于 **RD 在字节间串行传递**（`dispChainVar`）——字节 0 的 `dispOut` 喂给字节 1 的 `dispIn`，保证整条流的 RD 连续。

#### 4.3.3 源码精读

**K 字符与示例 D 字符常量**：

[protocols/line-codes/rtl/Code8b10bPkg.vhd:29-43](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/line-codes/rtl/Code8b10bPkg.vhd#L29-L43) —— 12 个 K 字符（K28.0~K30.7）与 D10.2/D21.5。注释标出哪几个是 comma。

**编解码过程声明**（注意 `dispIn`/`dispOut` 把 RD 显式穿在接口里）：

[protocols/line-codes/rtl/Code8b10bPkg.vhd:45-59](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/line-codes/rtl/Code8b10bPkg.vhd#L45-L59) —— `encode8b10b` 与 `decode8b10b` 过程签名。

**5B/6B 编码核心**（节选）：

[protocols/line-codes/rtl/Code8b10bPkg.vhd:105-126](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/line-codes/rtl/Code8b10bPkg.vhd#L105-L126) —— 用一堆中间布尔量（`l22`/`l40`/`l04`/`l13`/`l31` 等）描述码字的不均度分类，再据此决定每位输出与是否取反。

**取反与 RD 更新**：

[protocols/line-codes/rtl/Code8b10bPkg.vhd:166-184](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/line-codes/rtl/Code8b10bPkg.vhd#L166-L184) —— `compls6`/`compls4` 决定 6B/4B 是否取反，`dispOut` 由 `disp6` 经 3B/4B 段更新，最后拼出 10 位 `dataOut`。

> 说明：算法实现来自经典的 Widmer–Franaszek 8B/10B 组合逻辑，逐位用 XOR/AND 推导，目的是单拍纯组合完成、无查表、可高速综合。初读不必逐位推导，只要理解「输入 8 位 + RD → 输出 10 位 + 新 RD」的契约即可。

**`codeErr` 的生成**（解码端识别非法/不一致码字的全部条件之和）：

[protocols/line-codes/rtl/Code8b10bPkg.vhd:327-342](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/line-codes/rtl/Code8b10bPkg.vhd#L327-L342) —— 一长串条件 OR 起来：全 0/全 1 段、不均度与 RD 不符等任一命中即 `codeErr=1`。

**编码器模块的并行结构**。注意 `NUM_BYTES_G` 字节循环里，`dispChainVar` 在字节间串接：

[protocols/line-codes/rtl/Encoder8b10b.vhd:80-88](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/line-codes/rtl/Encoder8b10b.vhd#L80-L88) —— 循环里把上一字节的 `dispOut` 作为下一字节的 `dispIn`，循环结束写回 `runDisp`。

**流控可选**。`FLOW_CTRL_EN_G` 开启时，编码器变成带 ready/valid 的握手，输出在下游没ready时保持（用于仿真 stall）：

[protocols/line-codes/rtl/Encoder8b10b.vhd:72-89](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/line-codes/rtl/Encoder8b10b.vhd#L72-L89) —— `readyIn` 直接跟随 `readyOut`，仅在输出被消费后才采新输入。

**解码器模块**结构对称，多出 `codeErr`/`dispErr` 向量（每字节一位）：

[protocols/line-codes/rtl/Decoder8b10b.vhd:79-87](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/line-codes/rtl/Decoder8b10b.vhd#L79-L87) —— 解码循环同样串接 RD。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：用 `Encoder8b10b`/`Decoder8b10b` 做一对编解码，发送一个 K 字符（K28.5）加若干数据字节，验证解码端还原一致、且 `codeErr`/`dispErr` 全 0。

**操作步骤**：

1. 先看现成的环回连线范例 [protocols/line-codes/tb/LineCode8b10bTb.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/line-codes/tb/LineCode8b10bTb.vhd)：编码器的 `dataOut`（10 位/字节）直接接到解码器的 `dataIn`，`validEncode` 同时回拉编码器的 `readyOut` 形成单拍通路：

   [protocols/line-codes/tb/LineCode8b10bTb.vhd:50-85](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/line-codes/tb/LineCode8b10bTb.vhd#L50-L85) —— 编码器实例 + 解码器实例的环回接线。

2. 运行仓库已有的 cocotb 回归（工具链见 u9-l1）：

   ```bash
   make MODULES=$PWD import
   ./.venv/bin/python -m pytest -q tests/protocols/line_codes/test_Encoder8b10b.py tests/protocols/line_codes/test_Decoder8b10b.py tests/protocols/line_codes/test_Code8b10bPkg.py
   ```

3. **手动追踪一个 K 字符**：取 `dataIn = 0xBC`（K28.5）、`dataKIn = 1`、`dispIn = 0`，对照 `encode8b10b` 的 5B/6B + 3B/4B 段，手算或仿真读出 `dataOut`（应为 K28.5 的 comma 码 `001111 1010` 或其反相，取决于 RD），再把该 `dataOut` 喂回 `decode8b10b`，确认还原出 `dataOut = 0xBC`、`dataKOut = 1`。K28.5 是千兆以太网/PGP2b 的常用对齐逗号。

**需要观察的现象**：

- 编码器输出 `validOut` 滞后输入一拍（寄存输出）；解码器同样滞后一拍。
- `runDisp`（RD）在数据字节间来回翻转，但长期均值在 0 附近。
- 连续发 K28.5 时，`dataOut` 会在两种极性间交替出现（同字节、不同 RD → 不同 10 位码），这是直流平衡的直接体现。
- 解码端 `codeErr`、`dispErr` 始终为 0。

**预期结果**：`dataKOut` 在 K 字符那拍为 1、其余拍为 0；解码数据与输入逐字节相等。若本地无仿真环境，**待本地验证**——但 step 3 的手算追踪不依赖工具，可直接做。

#### 4.3.5 小练习与答案

**练习 1**：如果故意把编码器输出的某一比特翻转后再送解码器，`codeErr` 和 `dispErr` 哪个会报？

**参考答案**：取决于翻转后是否仍是合法码字。8B/10B 的一些单比特错误会撞成另一个合法码字（此时 `codeErr=0` 但 `dispErr=1`，因为 RD 连续性被破坏）；另一些会撞成非法码字（`codeErr=1`）。**这正是 8B/10B 的局限**：它只有检测能力、没有纠正能力，且部分错误会逃过 `codeErr`。要纠正单比特错误，需要下一节的汉明码。

**练习 2**：为什么多字节编码器里 RD 必须在字节间串行传递，而不能每个字节独立编码？

**参考答案**：因为直流平衡是**整条流**的属性。每个字节的正确编码取决于进入它时的 RD，而 RD 是前一字节编码的结果。若各字节独立用固定 RD 编码，流的长期 0/1 失衡会发散，破坏直流平衡。源码用 `dispChainVar` 串接（[Encoder8b10b.vhd:80-88](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/line-codes/rtl/Encoder8b10b.vhd#L80-L88)）正是为此。

---

### 4.4 汉明纠错：HammingEcc（SEC-DED）

#### 4.4.1 概念说明

8B/10B 能「检测」错误但不能「纠正」。当数据存放在易翻转的存储器里、或经过无法重传的控制通路时，我们需要**前向纠错（FEC）**：在写入时加几个校验位，读出时能定位并纠正单比特错误。

`HammingEccPkg` 实现的是 **扩展汉明码（SEC-DED）**：

- **SEC**（Single Error Correct）：能纠正任意单比特错误。
- **DED**（Double Error Detect）：能检测出（但不能纠正）双比特错误。

原理：把 k 位数据配上 m 位奇偶校验位，组成 n = k + m 位码字；再加 1 位**整体奇偶位**（extended parity）得到 n+1 位。校验位放在 2 的幂次位置（第 1、2、4、8… 位），读出时重算「校验子（syndrome）」，它直接指向出错比特的位置。

#### 4.4.2 核心流程与数学

**求需要多少校验位**。对 k 位数据，找最小的 m 使码字长度 \(n = k+m\) 满足汉明界：

\[
n = k + m \leq 2^m - 1
\]

直观含义：m 位校验子共 \(2^m\) 种取值，其中 1 种表示「无错」，其余 \(2^m-1\) 种要能逐一指向 n 个可能的出错位置，故 \(2^m - 1 \geq n\)。

以 k = 8（1 字节）为例：

| k | 满足 \(k+m \leq 2^m-1\) 的最小 m | n = k+m | 加扩展位后 |
|---|---|---|---|
| 8 | 4（\(12 \leq 15\)） | 12 | 13 |

所以 8 位数据 → 12 位汉明码字 → 13 位扩展码字。`HammingEccEncoder` 的输出端口宽度正是 `hammingEccDataWidth(DATA_WIDTH_G) downto 0`（+1 即扩展位）。

**编码**：

1. 把 k 位数据填进码字的「非 2 的幂」位置。
2. 每个校验位 = 它所覆盖位置（位置号在该位为 1）的数据位的异或。
3. 扩展奇偶位 = 整个 n 位码字的异或（使总码字偶校验）。

**解码**：

1. 重算校验子 syndrome（用收到的码字重新异或一遍校验覆盖集）。
2. 若 syndrome ≠ 0 且 ≤ n，翻转该位置的比特 → **纠正**。
3. 重算整体奇偶 parity。
4. 用 syndrome 与 parity 组合判定错误类型：

\[
\text{errSbit} = \text{parity} \lor \text{or}(\text{syndrome}), \qquad
\text{errDbit} = \overline{\text{parity}} \land \text{or}(\text{syndrome})
\]

| parity | syndrome | errSbit | errDbit | 含义 |
|---|---|---|---|---|
| 0 | 0 | 0 | 0 | 无错 |
| 1 | ≠0 | 1 | 0 | 单比特错（已纠正，数据可信）|
| 0 | ≠0 | 1 | 1 | 双比特错（检测到，不可纠正）|

> 关键：仅当 `errDbit=0` 时 `errSbit=1` 才表示「已成功纠正的单错」；若 `errDbit=1`，数据已不可信。`errSbit` 在双错时也会为 1，要靠 `errDbit` 来区分。

#### 4.4.3 源码精读

**求校验位宽度**。函数用循环找最小 m，注释写出核心公式 \(k = 2^m - m - 1\)：

[protocols/hamming-ecc/rtl/HammingEccPkg.vhd:41-60](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/hamming-ecc/rtl/HammingEccPkg.vhd#L41-L60) —— `while (2**m < k+m+1)` 递增 m，等价于求 \(2^m \geq k+m+1\)。

**编码：数据位就位**。遍历 1..n，跳过 2 的幂位置填数据：

[protocols/hamming-ecc/rtl/HammingEccPkg.vhd:87-92](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/hamming-ecc/rtl/HammingEccPkg.vhd#L87-L92) —— `if (isPowerOf2(i) = false) then codeWord(i) := data(bitPtr)`。

**编码：算校验向量并就位**。第 i 个校验位覆盖所有「位置号 j 与 \(2^{i-1}\) 相与不为 0」的位置：

[protocols/hamming-ecc/rtl/HammingEccPkg.vhd:95-106](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/hamming-ecc/rtl/HammingEccPkg.vhd#L95-L106) —— 双重循环算 `pVector(i)`，再 `codeWord(2**(i-1)) := pVector(i)`。

**编码：扩展奇偶位**。整体异或后拼到码字末尾：

[protocols/hamming-ecc/rtl/HammingEccPkg.vhd:108-115](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/hamming-ecc/rtl/HammingEccPkg.vhd#L108-L115) —— `pExtended := uXor(codeWord)`，`encWord := codeWord & pExtended`。

**解码：算校验子并纠错**。重算 syndrome，若其数值 ≤ n 则翻转对应位：

[protocols/hamming-ecc/rtl/HammingEccPkg.vhd:140-154](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/hamming-ecc/rtl/HammingEccPkg.vhd#L140-L154) —— `parity := uXor(encWord)`，算 syndrome，`codeWord(syndrome) := not codeWord(syndrome)`。

**解码：错误标志**。两行决定 SEC-DED 语义：

[protocols/hamming-ecc/rtl/HammingEccPkg.vhd:165-166](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/hamming-ecc/rtl/HammingEccPkg.vhd#L165-L166) —— `errSbit := parity or uOr(syndrome)`，`errDbit := not(parity) and uOr(syndrome)`。

**模块外壳**。`HammingEccEncoder` 几乎只做「调用包函数 + 寄存输出」：

[protocols/hamming-ecc/rtl/HammingEccEncoder.vhd:79-87](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/hamming-ecc/rtl/HammingEccEncoder.vhd#L79-L87) —— `v.obData := hammingEccEncode(ibData)`。

`HammingEccDecoder` 同样调包过程，端口注释精确点明两个标志的语义：

[protocols/hamming-ecc/rtl/HammingEccDecoder.vhd:44-45](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/hamming-ecc/rtl/HammingEccDecoder.vhd#L44-L45) —— `obErrSbit: Only 1 bit error that was corrected`、`obErrDbit: 2 or more bit error detected`。

#### 4.4.4 代码实践

**实践目标**：用自检测试台 `HammingEccTb` 穷举验证 SEC-DED——对每个 8 位数据、注入 0/1/2 比特错误，确认单错被纠正、双错被检测。

**操作步骤**：

1. 阅读 [protocols/hamming-ecc/tb/HammingEccTb.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/hamming-ecc/tb/HammingEccTb.vhd)。它把编码器输出与一个 `bitErrorMask` 异或来注入错误：

   [protocols/hamming-ecc/tb/HammingEccTb.vhd:117](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/hamming-ecc/tb/HammingEccTb.vhd#L117) —— `encDataMask <= encData xor r.bitErrorMask`。

2. 看它的状态机 `LOAD_S → WAIT_S → PASSED_S/FAILED_S`，分三类校验（0 错、1 错纠正、2 错检测），用 `onesCount` 判断注入了几位错：

   [protocols/hamming-ecc/tb/HammingEccTb.vhd:167-203](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/hamming-ecc/tb/HammingEccTb.vhd#L167-L203) —— 0 错要 `sbit=dbit=0`；1 错要 `sbit=1,dbit=0` 且数据还原；≥2 错要 `dbit=1`。

3. 用 GHDL 跑这个自检 TB（它用 `std.env.finish` 自终止）：

   ```bash
   # 概念性命令（具体目标名依本地 Makefile/include，待本地验证）
   ghdl -a --std=08 --ieee=synopsys protocols/hamming-ecc/rtl/HammingEccPkg.vhd
   ghdl -a --std=08 --ieee=synopsys protocols/hamming-ecc/rtl/HammingEccEncoder.vhd
   ghdl -a --std=08 --ieee=synopsys protocols/hamming-ecc/rtl/HammingEccDecoder.vhd
   ghdl -a --std=08 --ieee=synopsys protocols/hamming-ecc/tb/HammingEccTb.vhd
   ghdl -e --std=08 HammingEccTb
   ghdl -r --std=08 HammingEccTb --stop-time=10ms
   ```

**需要观察的现象**：

- 仿真最终打印 `Simulation Passed!`（穷举完所有 8 位数据 × {0,1,2 位错} 组合），不进 `FAILED_S`。
- 注入 1 位错时，解码器 `obData` 与原 `ibData` 相等（已被纠正），`obErrSbit=1`。
- 注入 2 位错时，`obErrDbit=1`（数据不可信，但 TB 只检查 `dbit` 标志）。

**预期结果**：`passed` 拉高、`failed` 保持 0。GHDL 精确命令**待本地验证**（仓库主回归用 cocotb，此 VHDL TB 需手动用 ghdl 跑）。

#### 4.4.5 小练习与答案

**练习 1**：若只发原始汉明码（不加扩展奇偶位），还能区分「单错」和「双错」吗？

**参考答案**：不能。原始汉明码的 syndrome 对「单错」和「双错」都会非零，且双错的 syndrome 可能恰好等于某个合法位置号，导致**误纠正**（把正确的数据改错）。加上扩展奇偶位后，单错会使整体奇偶反转（parity=1）、双错不会（parity=0），才有了区分依据——这正是「扩展」二字的由来，也是 DED 的来源。

**练习 2**：`DATA_WIDTH_G=16` 时，编码后输出多少位？

**参考答案**：k=16，求最小 m 使 \(16+m \leq 2^m-1\)：m=5 时 \(21 \leq 31\) 成立（m=4 时 \(20 \leq 15\) 不成立）。故 n=21，加扩展位共 22 位。可对照函数 `hammingEccDataWidth(16)` 返回 21，端口宽度为 22。

---

## 5. 综合实践

把本讲三类积木串起来，做一次「定位真实使用位置」的源码侦察。

**任务**：在仓库中找出每个 helper 被哪些协议核实际例化，并填写下表，然后画一条「应用帧 → Packetizer/Batcher → 8B/10B」的组装链草图，说明每层解决什么问题。

| 积木 | 用 grep 找到的典型例化位置 | 该层解决的问题 |
|---|---|---|
| `AxiStreamPacketizer` | （自己 grep） | 侧带搬进数据，适配无侧带传输链路 |
| `AxiStreamBatcher` | （自己 grep） | 聚合小帧，降低协议开销 |
| `Encoder8b10b` / `Code8b10bPkg` | （自己 grep，提示：PGP2b、JESD204B） | 直流平衡 + 控制字符定界 |
| `HammingEccEncoder` / `HammingEccPkg` | （自己 grep） | 存储器/控制数据的单比特纠错 |

**建议命令**（只读侦察，不改源码）：

```bash
grep -rn "entity surf.AxiStreamPacketizer " --include=*.vhd protocols/ | head
grep -rn "Encoder8b10b\|encode8b10b" --include=*.vhd protocols/pgp | head
grep -rn "hammingEccEncode\|HammingEccEncoder" --include=*.vhd | head
```

**交付物**：

1. 一张填好的上表（每个积木至少 1 个真实文件路径）。
2. 一段话回答：为什么 PGP2b 这类链路会**同时**用到 Packetizer（或等价成帧）和 8B/10B？它们分别防御什么？
3. 思考题：如果要保护一段「经 SRP 远程读写的、存放在易失存储里的关键控制字」，你会选本讲的哪个积木？为什么 8B/10B 不适合这个场景？

**参考思路**：PGP2b 在线路上用 8B/10B（物理层直流平衡 + K 字符定界），在其上用类 Packetizer 的成帧承载 SSI 帧（逻辑层侧带）；而存储器里的关键字适合用 HammingEcc（位翻转是主要失效模式，且无法重传，必须就地纠正）。8B/10B 不适合存储保护，因为它有 25% 的开销且只为链路直流平衡设计，不提供纠错能力。

## 6. 本讲小结

- **Packetizer（V0）** 把 AXI-Stream 侧带塞进 64 位**帧头**、把 EOF+末拍 tUser 塞进 1 字节**帧尾**，超长帧按帧号/包号拆成多个包；尾有「追加进未满字」和「独立成字」两种安放方式，靠 `PROTO_WORDS_C=3` 预留协议开销。
- **Batcher（V1/V2）** 反向操作，把多个**子帧**装进一个**超级帧**；超级帧结束有字节阈值、子帧数、`forceTerm`、时钟间隔四种触发；V1 零填充、V2 用 `tKeep` 压紧并由 `AxiStreamGearbox` 打包。
- **8B/10B**（`Code8b10bPkg`）是纯函数包，靠**不均度（RD）**在 5B/6B+3B/4B 两段间选码，实现直流平衡；12 个 **K 字符**用于定界/对齐；`codeErr`/`dispErr` 提供检测能力，但**无纠错能力**。
- **扩展汉明码**（`HammingEccPkg`）实现 **SEC-DED**：校验子定位并纠正单比特错，扩展奇偶位区分单错/双错；`errSbit`/`errDbit` 两个标志要联合判读（仅 `errDbit=0` 时数据可信）。
- 三类模块都用 u1-l5 的双进程骨架与 u4-l1/u5-l1 的 AXI-Stream/SSI 记录，复用同一个 `AXIS_CONFIG_C` / `genTKeep` / `ssiSetUser*` 工具链。
- 设计取舍：Packetizer/Batcher 是**逻辑层**成帧（改帧结构），8B/10B 是**物理层**线路码（改比特表示），Hamming 是**存储/控制层**纠错（加冗余位）——三者不可互相替代。

## 7. 下一步学习建议

- **u7-l1 PGP 协议族总览**：PGP2b 是 8B/10B + 成帧的真实综合应用，可对照本讲看到 K 字符定界、SSI 帧承载如何在一条链上协作；PGP4 引入 CRC（见 u2-l4 的 `Crc32Parallel`），可与 Packetizer2 的 CRC 成帧对照。
- **u7-l3 JESD204B**：JESD204B 的 8B/10B 字符集与 ILAS 对齐序列直接复用 `Code8b10bPkg`，是本讲线路码的高带宽实战。
- **深入 V2 成帧**：阅读 `AxiStreamPacketizer2Pkg.vhd` 与 `AxiStreamPacketizer2.vhd`，对比 V0，理解带 CRC 的成帧如何把 u2-l4 的 CRC 与本讲的帧结构结合。
- **更多线路码**：`protocols/line-codes/` 还提供 10B/12B（`Code10b12bPkg`）、12B/14B（`Code12b14bPkg`）两套线路码，结构与 8B/10B 高度对称，可作为本讲的迁移练习。
