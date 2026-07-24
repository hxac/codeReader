# AXI-Stream DMA V2 与描述符

## 1. 本讲目标

本讲讲解 SURF 如何用 `AxiStreamDmaV2` 在「AXI-Stream 数据流」与「AXI4 内存」之间搬运整帧数据，并重点拆解其核心机制——**描述符驱动（descriptor-driven）**与**环形缓冲（ring buffer）**。

学完后你应当掌握：

- `AxiDmaPkg` 定义的请求/应答/描述符记录有哪些字段，它们在 DMA 引擎与描述符管理器之间如何流动。
- `AxiStreamDmaV2Write` 如何用一套状态机把一帧 AXI-Stream 数据逐拍写到 AXI4 内存，并支持多帧交错（interleaved）。
- `AxiStreamDmaV2Desc` 描述符管理器如何用空闲缓冲 FIFO + 完成描述符环形缓冲，把软件、写/读引擎、内存四方串成一个完整的缓冲池模型。
- 一次「流→内存」DMA 的完整生命历程：软件预投缓冲 → 引擎申请描述符 → 写内存 → 回写完成描述符 → 触发中断。

## 2. 前置知识

本讲建立在你已经学完以下两讲的基础上：

- **u4-l1 AXI-Stream 记录与配置**：理解 `AxiStreamMasterType`/`AxiStreamSlaveType` 记录、`AxiStreamConfigType`、`tValid`/`tReady` 握手，以及 `tKeep`/`tLast`/`tDest`/`tUser` 等侧带语义。
- **u8-l1 完整 AXI4 总线与 AxiRam**：理解 AXI4 五通道（读 AR/R、写 AW/W/B）记录、突发（burst）由 `arlen`/`awlen`（存「次数−1」）与 `arsize`/`awsize`（存 log2 字节数）刻画，以及 `wstrb` 写选通的作用。

再用三句话补两个本讲会反复用到的小概念：

- **突发长度 helper**：`AxiPkg` 提供 `getAxiLen` / `getAxiLenProc`，给定「剩余字节数、当前地址、最大突发字节」，算出本拍 AWLEN/ARLEN 的值。它会把传输对齐到 **4 KB 边界**（AXI 规范要求一次突发不跨 4 KB），并取「剩余字节、突发上限、到 4 KB 边界的余量」三者最小值。
- **帧（frame）**：一条 AXI-Stream 上由 `tLast=1` 收尾的一段数据。`AxiStreamDmaV2` 是「逐帧」DMA——一帧对应内存里一段连续缓冲。
- **VC（虚拟通道）/ tDest**：流上的 `tDest` 字段标识不同逻辑通道。V2 写引擎允许不同 `tDest` 的帧交错到达，各自独立落内存。

一个直觉性的比喻：把 `AxiStreamDmaV2` 想象成一座「快递分拣站」。进站的是一连串包裹（AXI-Stream 帧），每件包裹都要送到某个地址（内存缓冲）。站里有个调度员（描述符管理器），手里攥着一沓软件预先发来的「空地址卡片」（空闲缓冲 FIFO）。流水线（写引擎）每来一件包裹，就向调度员要一张卡片，按卡片上的地址把货送进仓库（AXI4 内存），送完再把一张「送达回执」（完成描述符）按顺序贴到墙上的公告板（环形缓冲）上，并按铃（中断）通知软件来收回执。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [axi/dma/rtl/AxiDmaPkg.vhd](#) | 纯包文件，定义全部 DMA 请求/应答/描述符记录、`_INIT_C` 初值，以及记录↔`slv` 互转的 `toSlv`/`toAxiXxx` 函数。是 DMA 子系统的「契约层」。 |
| [axi/dma/rtl/v2/AxiStreamDmaV2.vhd](#) | 纯结构顶层。把 1 个描述符管理器与 N 个读/写引擎通道拼装起来，对外暴露 AXI-Lite 寄存器口、AXI-Stream 流口和 AXI4 总线口。 |
| [axi/dma/rtl/v2/AxiStreamDmaV2Write.vhd](#) | 写引擎。把一帧 AXI-Stream 数据搬到 AXI4 内存，支持交错帧、超长帧续写（continue）、溢出丢弃、可选元数据（meta）回写。本讲主角之一。 |
| [axi/dma/rtl/v2/AxiStreamDmaV2Desc.vhd](#) | 描述符管理器。管理空闲缓冲 FIFO、向引擎派发描述符、把完成描述符写回内存环形缓冲、维护中断与缓冲组计数。本讲主角之二。 |
| axi/dma/rtl/v2/AxiStreamDmaV2Read.vhd | 读引擎（内存→流），结构与写引擎对称，本讲只做对照引用。 |
| axi/dma/rtl/v2/AxiStreamDmaV2WriteMux.vhd | 把「数据写」与「描述符写」两条 AXI4 写通路时分复用到同一根物理总线上，保证回执在数据之后发出。 |
| axi/axi4/rtl/AxiPkg.vhd | 提供 `AxiLenType` 与 `getAxiLenProc`，是写/读引擎计算突发长度的公共工具。 |

永久链接前缀（本讲所有链接均基于此 HEAD）：

```
https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/
```

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：

1. **DMA 请求/应答记录**（`AxiDmaPkg`）——先把「零件清单」认清。
2. **流→内存写引擎**（`AxiStreamDmaV2Write`）——看一帧数据怎么落进内存。
3. **描述符管理与环形缓冲**（`AxiStreamDmaV2Desc`）——看缓冲池与回执怎么运转。

### 4.1 DMA 请求/应答记录（AxiDmaPkg）

#### 4.1.1 概念说明

DMA 引擎和描述符管理器是两块独立的电路，它们之间不靠共享内存通信，而是靠一组**握手记录**点对点传递。`AxiDmaPkg` 就是这组记录的合同。理解 DMA 的第一步，是认清这几条记录分别「谁发给谁、装了什么」。

SURF 的 DMA 把一次写搬运拆成三段握手（读侧类似但更简单）：

| 记录 | 方向 | 含义 |
|------|------|------|
| `AxiWriteDmaDescReqType` | 引擎 → Desc | 「我要一个缓冲，目的地是 tDest/tId」 |
| `AxiWriteDmaDescAckType` | Desc → 引擎 | 「给你：地址、buffId、maxSize、是否丢弃/续写」 |
| `AxiWriteDmaDescRetType` | 引擎 → Desc | 「这帧写完了：大小、首/末 tUser、结果、buffId」 |
| `AxiWriteDmaTrackType` | 引擎内部 | 跟踪一帧在途状态（不跨模块，存于 tracking RAM） |

此外还有一组「原始」（非 V2）的 `AxiWriteDmaReqType`/`AxiWriteDmaAckType`，用于不带描述符管理器的简易 DMA（V1 风格），调用方直接给出地址和 maxSize。本讲聚焦 V2，但这两条记录体现了 DMA 的最小语义：**请求里给「地址 + 上限」，应答里回「完成大小 + 错误」**。

#### 4.1.2 核心流程

描述符请求/应答是一次经典的「请求−授权」握手：

```
引擎(写)                       描述符管理器
   |  dmaWrDescReq.valid=1          |
   |  dest=tDest, id=tId            |
   | -----------------------------> |  从空闲缓冲FIFO弹出一项
   |                                |
   |  dmaWrDescAck.valid=1          |
   |  address, buffId, maxSize,     |
   |  contEn, dropEn, timeout       |
   | <----------------------------- |
   |  (引擎置 req.valid=0,开始写)   |
```

写完后是「回执−确认」握手：

```
引擎(写)                       描述符管理器
   |  dmaWrDescRet.valid=1          |
   |  size, firstUser, lastUser,    |
   |  result, continue, buffId      |
   | -----------------------------> |  把回执写成128b描述符入环形缓冲
   |  dmaWrDescRetAck=1             |
   | <----------------------------- |  (引擎置 ret.valid=0, 回 IDLE)
```

#### 4.1.3 源码精读

先看请求记录。`AxiWriteDmaDescReqType` 极简，只带「目的地」：

[AxiDmaPkg.vhd:L162-L172](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/dma/rtl/AxiDmaPkg.vhd#L162-L172) 定义描述符请求记录与初值。`valid` 是握手位，`id`/`dest` 取自到达帧的 `tId`/`tDest`——也就是说，引擎只告诉管理器「这帧要去哪个逻辑通道」，具体内存地址由管理器从缓冲池里分配。

授权记录 `AxiWriteDmaDescAckType` 装满了管理器给引擎的「这帧怎么写」全部参数：

[AxiDmaPkg.vhd:L188-L210](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/dma/rtl/AxiDmaPkg.vhd#L188-L210) 定义授权记录。关键字段：

- `address`：64 位内存基地址，引擎从这里开始写。
- `buffId`：缓冲标识，回执时原样带回，供软件定位是哪个缓冲。
- `maxSize`：本缓冲最多能写多少字节，溢出会触发 continue 或 drop。
- `contEn`：缓冲写满后是否「续写」到下一个缓冲（多描述符拼成一帧）。
- `dropEn`：是否直接丢弃这帧。
- `metaEnable`/`metaAddr`：是否在帧末额外写一份 64 位「元数据」回执到 `metaAddr`。
- `timeout`：等待内存总线 BRESP 的超时阈值。

回执记录 `AxiWriteDmaDescRetType` 是引擎向软件汇报的「送达回执」：

[AxiDmaPkg.vhd:L224-L246](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/dma/rtl/AxiDmaPkg.vhd#L224-L246) 定义回执记录。`size` 是实际写入字节数；`firstUser`/`lastUser` 保存帧首拍与末拍的 TUSER（SSI 下即 SOF/EOFE 等侧带）；`continue` 表示「这帧还没完，下一个缓冲接着写」；`result` 是 4 位错误/状态码（bit1:0 = AXI BRESP，bit2 = overflow，bit3 = ACK 超时）。

最后是引擎**内部**的跟踪记录 `AxiWriteDmaTrackType`，它不跨模块，而是按 `tDest` 索引存进一块 tracking RAM，用来支持交错帧：

[AxiDmaPkg.vhd:L260-L292](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/dma/rtl/AxiDmaPkg.vhd#L260-L292) 定义跟踪记录。注意它把 `AxiWriteDmaDescAckType` 的全部字段（地址、maxSize、contEn……）连同运行时累加的 `size`、`firstUser`、`overflow` 都收进一个记录——本质上是「一个在途缓冲的全部上下文」。每个 `tDest`（最多 256 个 VC）对应一项，于是不同 VC 的帧可以交替到达而互不干扰。

包体里还为每条记录提供了 `toSlv` / `toAxiXxx` 互转函数。以跟踪记录为例：

[AxiDmaPkg.vhd:L473-L511](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/dma/rtl/AxiDmaPkg.vhd#L473-L511) 记录↔`slv` 互转。这套函数用 `assignSlv`/`assignRecord` 把记录逐字段打包/解包成一个定宽 `slv`，宽度由 `AXI_WRITE_DMA_TRACK_SIZE_C`（=253）等常量钉死。为什么要互转？因为跟踪记录要写进一块通用 `DualPortRam`，而 RAM 的数据口只能是 `slv`，于是进 RAM 前 `toSlv`、出 RAM 后 `toAxiWriteDmaTrack` 还原。这是 SURF 里「记录 + RAM」的标准套路。

#### 4.1.4 代码实践

**目标**：用源码确认「请求里没有地址、地址由管理器从缓冲池给」，从而理解为什么 DMA 是「描述符驱动」而非「调用方给地址」。

**步骤**：

1. 打开 `axi/dma/rtl/AxiDmaPkg.vhd`，对照本节三张记录表，在文件里逐一找到 `AxiWriteDmaDescReqType`、`AxiWriteDmaDescAckType`、`AxiWriteDmaDescRetType` 的字段定义。
2. 验证：`DescReq` 里**没有** `address` 字段，`address` 只出现在 `DescAck`（管理器→引擎）和 `DescRet` 里没有（回执只汇报大小与状态）。
3. 再看 V1 风格的 `AxiWriteDmaReqType`（[L43-L49](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/dma/rtl/AxiDmaPkg.vhd#L43-L49)），对比它直接带 `address`/`maxSize`——这就是「带描述符管理器」与「不带」的根本差异。

**预期结果**：你会清楚看到 V2 把「地址从哪来」这件事从调用方移到了描述符管理器，调用方只负责按 `tDest` 申请缓冲。这是后面环形缓冲能工作的前提。

#### 4.1.5 小练习与答案

**练习 1**：`AxiWriteDmaDescRetType.result` 是 4 位，但 AXI 的 BRESP 只有 2 位，多出的两位分别是什么？
<details><summary>参考答案</summary>
bit1:0 是 AXI 写响应 BRESP（00=OKAY，10=SLVERR，11=DECERR）；bit2 是 `overflow`（帧超过 maxSize 且未续写）；bit3 是 ACK 超时（等 BRESP 超时）。见写引擎 RETURN_S 里 `result(3)/result(2)/result(1:0)` 的赋值。</details>

**练习 2**：为什么 `AxiWriteDmaTrackType` 要按 `tDest` 索引存进 RAM，而不是只用一个寄存器？
<details><summary>参考答案</summary>
因为 V2 支持交错帧：不同 `tDest` 的帧可能交替到达同一写引擎。每个在途 VC 需要各自保存「基地址、已写大小、buffId」上下文，新数据到来时按其 `tDest` 取回对应上下文继续累加。单一寄存器只能记一路，会丢失其它在途帧的状态。</details>

### 4.2 流→内存写引擎（AxiStreamDmaV2Write）

#### 4.2.1 概念说明

写引擎是「快递分拣站」的流水线本体。它的职责很纯粹：拿到一张授权卡片（`dmaWrDescAck`，含内存地址与上限），就把随后到达的一帧 AXI-Stream 数据，按地址逐拍写进 AXI4 内存；写完生成回执（`dmaWrDescRet`）。

两个关键能力让它区别于一个「傻瓜搬运工」：

- **突发化与 4 KB 对齐**：它不是一字节一字节写，而是组织成 AXI4 突发（burst），每次突发不超过 `BURST_BYTES_G`，且绝不跨 4 KB 边界。这靠 `getAxiLenProc` 算出 AWLEN。
- **交错帧与 tracking RAM**：用一块按 `tDest` 索引的双口 RAM 保存每个 VC 的在途上下文，于是 A、B 两路帧可以交替喂进来而不串货。文件头注释明说："Version 2 supports interleaved frames."

它还处理边界情况：帧超过缓冲上限（`maxSize`）时，按 `contEn` 决定「续写下一个缓冲」或「标记 overflow 并丢弃余量」；内存总线出错（BRESP≠0）时记进 `result`；可选地在帧末写一份 64 位「meta」元数据。

#### 4.2.2 核心流程

写引擎的主状态机有 10 个状态，核心主干是 `IDLE → REQ → ADDR → MOVE → (PAD) → (META) → RETURN → IDLE`：

```
            ┌──────────────────────────┐
            │ RESET_S (上电等100拍)      │
            └────────────┬─────────────┘
                         ▼
            ┌──────────────────────────┐
            │ INIT_S  (遍历初始化dest)   │
            └────────────┬─────────────┘
                         ▼
   ┌──────────────────► IDLE_S ◄─────────────────────┐
   │   到来帧的tDest匹配某在途VC?                       │
   │     是且在途 ──► ADDR_S                            │
   │     是但新帧 ──► REQ_S (申请描述符)                 │
   │                                                    │
   │                    REQ_S ──收到DescAck──► ADDR_S   │
   │                                                    │
   │   ADDR_S: getAxiLenProc算AWLEN,发awvalid ──► MOVE_S│
   │                                                    │
   │   MOVE_S: 逐拍 tReady<=1, wdata<=axisData          │
   │            address+=DATA_BYTES, size+=bytes        │
   │            ┌─ tLast?  ──► PAD_S/META_S/RETURN_S     │
   │            ├─ awlen到0? ──► (续ADDR_S 或 RETURN_S)  │
   │            └─ dest变了/超时 ──► PAD_S               │
   │                                                    │
   │   PAD_S:  用wstrb=0把当前突发补满到wlast            │
   │   META_S: 可选,写64b元数据到metaAddr               │
   │   RETURN_S: 装配dmaWrDescRet,等所有BRESP收齐──► IDLE│
   │                                                    │
   │   DUMP_S: dropEn=1时,吞掉本帧数据不发写请求         │
   └────────────────────────────────────────────────────┘
```

「`awlen` 到 0」表示当前突发写完。若帧还没结束（还有数据），回 `ADDR_S` 发起下一个突发；若恰好写满缓冲（`maxSize` 用尽）且 `contEn=1`，则直接走 `RETURN_S` 并把 `continue=1` 带上，让管理器派发下一个缓冲继续接这同一帧。

#### 4.2.3 源码精读

先看状态机的「骨架」——状态枚举、RegType 与初值常量，沿用 u1-l5 的双进程风格：

[AxiStreamDmaV2Write.vhd:L65-L112](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/dma/rtl/v2/AxiStreamDmaV2Write.vhd#L65-L112) 状态枚举、RegType 记录与 `REG_INIT_C`。注意 RegType 里同时摆了 `dmaWrDescReq`（请求）、`dmaWrTrack`（在途上下文）、`dmaWrDescRet`（回执）三件套，外加 AXI 写主机 `wMaster`、突发计数 `awlen`、`reqCount`/`ackCount`（发出的突发数 vs 收到的 BRESP 数）、`timeoutCnt`。

`IDLE_S` 是分拣入口，它决定「这帧是续接在途缓冲，还是要新申请一个」：

[AxiStreamDmaV2Write.vhd:L214-L261](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/dma/rtl/v2/AxiStreamDmaV2Write.vhd#L214-L261) IDLE 状态。它先用当前 `r.dmaWrTrack.dest` 比对到来帧的 `tDest`：若匹配且 `inUse=1`，说明该 VC 有在途缓冲，直接续写（去 `ADDR_S`）；若 `inUse=0` 则发 `dmaWrDescReq.valid=1` 去 `REQ_S` 申请新缓冲。若 `dest` 不匹配，则用 `intAxisMaster.tDest` 作地址去 tracking RAM 查（`trackData`），把查到的上下文加载进 `dmaWrTrack` 再判断。这正是「按 tDest 索引 RAM 支持交错」的体现。

`REQ_S` 收授权，把 `DescAck` 的参数搬进跟踪记录：

[AxiStreamDmaV2Write.vhd:L263-L287](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/dma/rtl/v2/AxiStreamDmaV2Write.vhd#L263-L287) REQ 状态。`inUse:=1`、`size:=0`，把 address/maxSize/contEn/dropEn/buffId/metaEnable/metaAddr/timeout 全部从授权记录搬进跟踪记录。若 `dropEn=1` 直接去 `DUMP_S` 丢弃。

`ADDR_S` 计算并发起一次突发写地址：

[AxiStreamDmaV2Write.vhd:L289-L313](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/dma/rtl/v2/AxiStreamDmaV2Write.vhd#L289-L313) ADDR 状态。调用 `getAxiLenProc(AXI_CONFIG_G, BURST_BYTES_G, maxSize, address, r.axiLen, v.axiLen)` 算本拍 AWLEN；当 `axiLen.valid="11"`（两拍流水结果就绪）且 `awvalid=0` 且未 pause，就把地址送上 `awaddr`、长度送上 `awlen`，拉高 `awvalid` 进 `MOVE_S`。注意 `awlen` 同时存进记录字段供 MOVE_S 递减。

> 这里调用的 `getAxiLenProc` 把 `getAxiLen` 里的两次比较拆到两个时钟周期，以打断长组合链、改善时序。详见 [AxiPkg.vhd:L401-L443](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiPkg.vhd#L401-L443)，其 `AxiLenType` 定义在 [AxiPkg.vhd:L266-L276](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiPkg.vhd#L266-L276)。

`MOVE_S` 是数据搬运主循环，逐拍把流数据搬到 AXI 写数据通道：

[AxiStreamDmaV2Write.vhd:L315-L421](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/dma/rtl/v2/AxiStreamDmaV2Write.vhd#L315-L421) MOVE 状态。当流 `tValid=1`、写通道空闲（`wvalid=0`）、dest 未变、未溢出时：置 `slave.tReady=1` 接收，把 `tData` 送到 `wMaster.wdata`，把 `tKeep` 送到 `wstrb`（TKEEP_COUNT 模式下用 `genTKeep(bytes)` 重建），然后 `address += DATA_BYTES_C`、`size += bytes`、`maxSize -= bytes`。首拍（`size=0`）记录 `firstUser` 与 `tId`；末拍（`tLast=1`）记录 `lastUser`、`inUse=0`。`awlen` 每拍减 1，减到 0 时拉 `wlast` 收尾当前突发，然后决定回 `ADDR_S`（帧继续）还是去 `RETURN_S`（帧结束/缓冲满）。

这段里有一处值得专门指出的健壮性设计——当突发恰好填满缓冲而帧仍在继续时：

[AxiStreamDmaV2Write.vhd:L382-L408](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/dma/rtl/v2/AxiStreamDmaV2Write.vhd#L382-L408) 突发收尾的 continue 路径。源码注释解释：若 `maxSize` 恰好归零且 `contEn=1`，就在这里直接置 `continue=1` 并走 RETURN_S，而**不是**回 `ADDR_S`。因为回 `ADDR_S` 时 `maxSize=0` 会触发一次「在下一个缓冲基地址上的零长度突发」off-by-one 写；在主机型 DMA（如经 IOMMU）上，这个越界写会落到映射页之外并触发 IOMMU page fault。这是一个真实的、被注释固化下来的防御性细节。

`PAD_S` 用「空写」把未填满的突发补到 `wlast`，确保 AXI 突发完整收尾：

[AxiStreamDmaV2Write.vhd:L423-L447](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/dma/rtl/v2/AxiStreamDmaV2Write.vhd#L423-L447) PAD 状态。`wstrb` 清零表示「这些字节不写」，但 `wvalid`/`wlast` 照常驱动，把当前突发补满。帧结束则去 `META_S`/`RETURN_S`，否则回 `IDLE_S` 续接。

`META_S` 可选地在帧末写一份 64 位元数据回执（含 size、firstUser/lastUser、continue、overflow、result）到 `metaAddr`：

[AxiStreamDmaV2Write.vhd:L449-L475](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/dma/rtl/v2/AxiStreamDmaV2Write.vhd#L449-L475) META 状态。把回执信息打包进一次单拍 AXI 写（`awlen=0`、`wlast=1`、`wstrb=0xFF`），写到独立的 `metaAddr`。这让软件能在帧数据之外，另拿到一份「带状态标志」的索引。

`RETURN_S` 装配回执，并**等所有已发突发的 BRESP 收齐**后才置 `valid`：

[AxiStreamDmaV2Write.vhd:L477-L508](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/dma/rtl/v2/AxiStreamDmaV2Write.vhd#L477-L508) RETURN 状态。关键约束 `if (r.ackCount = r.reqCount)`：发出的突发数（`reqCount`）必须等于收到的 BRESP 数（`ackCount`），才认为全部写真正落地，才能让回执 `valid=1`。否则继续等，等到 `stCount=timeout` 就判定 ACK 超时，置 `result="11"` 与 `result(3)=1`。这保证软件看到的「完成」一定意味着数据已真正进内存。

最后是支持交错的关键结构——tracking RAM：

[AxiStreamDmaV2Write.vhd:L571-L593](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/dma/rtl/v2/AxiStreamDmaV2Write.vhd#L571-L593) tracking RAM。这是一块 256 项（`ADDR_WIDTH_G=8`，按 `tDest` 索引）、每项 `AXI_WRITE_DMA_TRACK_SIZE_C` 位的 `DualPortRam`。A 口写：每拍把当前 `r.dmaWrTrack` 按 `r.dmaWrTrack.dest` 地址写回（`wea='1'`），即「持久化在途状态」。B 口读：用**到来帧的 `tDest`** 作地址读出 `trackData`，供 IDLE_S 加载。于是状态机本体只持有一份 `dmaWrTrack`（当前正在服务的那路），其余 VC 的上下文全存在 RAM 里，按需切换——这就是「单状态机服务多路交错帧」的窍门。

#### 4.2.4 代码实践

**目标**：跟踪一次「流→内存」写 DMA 的内存地址推进过程，验证 `address` 如何按 `DATA_BYTES_C` 递增、`size` 如何按本拍有效字节累加。

**步骤**：

1. 打开 `axi/dma/rtl/v2/AxiStreamDmaV2Write.vhd`，定位 `MOVE_S` 里的地址与计数更新（约 L349-L357）。
2. 假设 `AXIS_CONFIG_G.TDATA_BYTES_C = 8`（64 位流）、`BURST_BYTES_G = 4096`、到来一帧共 100 字节（末拍 `tKeep` 表示 4 个有效字节）。
3. 在纸上逐拍推演：
   - 前 12 拍每拍 8 字节全有效：`address` 每拍 +8，`size` 每拍 +8（累计 96 字节，`maxSize` 同步 −8）。
   - 第 13 拍是末拍，`bytes=4`：`address` 仍 +8（整字对齐，`ADDR_LSB_C` 位清零），但 `size` 只 +4（累计 100 字节），`tLast=1` 触发收尾。
4. 对照 `awlen` 的递减：`ADDR_S` 算出的 AWLEN 对应一次最多 4096 字节的突发，本例 100 字节远小于 4096，且不跨 4 KB，故 `awlen` 只在一拍内从计算值减到 0 并拉 `wlast`。

**需要观察的现象**：

- `address` 永远按整字（`DATA_BYTES_C`）步进，低 `ADDR_LSB_C` 位被强制清零，保证 AXI 地址对齐。
- `size` 累加的是**真实有效字节**（用 `getTKeep`/计数算出的 `bytes`），而非整字宽度，所以回执里的 `size=100` 而非 `104`。
- 末拍 `tLast` 与 `awlen=0` 同时成立时，状态机一次性走到 `RETURN_S`（本例无 meta）。

**预期结果**：你能用一句话解释「为什么 address 步进宽度与 size 累加宽度可能不同」——因为地址必须对齐到总线字宽，而 size 记的是有效数据量。若你手头有 GHDL+cocotb 环境，可运行 `tests/axi/dma/test_AxiStreamDmaV2Write.py` 观察真实波形中的 `dmaWrTrack.address` 与 `dmaWrTrack.size`；否则以上为「源码阅读型实践」，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `RETURN_S` 必须等 `ackCount = reqCount` 才能置回执 `valid`？
<details><summary>参考答案</summary>
`reqCount` 记的是已发出的 AXI 写突发数，`ackCount` 记的是已收到 BRESP 的突发数。AXI4 的写响应 BRESP 是「整段突发真正落地的总承诺」。只有两者相等，才能保证回执宣称「写完」时，全部数据已真正被从机接收，否则软件可能读到未写完的内存。</details>

**练习 2**：`dropEn=1` 时，引擎还会向内存写数据吗？走的是哪个状态？
<details><summary>参考答案</summary>
不会。`dropEn=1` 时引擎进 `DUMP_S`，只置 `slave.tReady=1` 把帧数据吞掉（丢弃），不驱动 `wvalid`，不发任何写请求。帧末仍会走 `META_S`/`RETURN_S` 生成回执（带 overflow/result）汇报「已丢弃」。</details>

**练习 3**：tracking RAM 为什么用 `MODE_G => "write-first"` 且带输出寄存？
<details><summary>参考答案</summary>
write-first 保证「同一拍写进去的更新状态，同地址读出来是最新值」，避免读到旧上下文导致状态丢失；输出寄存（`REG_EN_G`/`DOA_REG_G`/`DOB_REG_G`）换得块 RAM 的 2 拍读延迟以改善时序，状态机里相应地用 `trackData`（已寄存）做判断、用 `r.dmaWrTrack`（当前态）做写回，二者配合避免读写竞争。</details>

### 4.3 描述符管理与环形缓冲（AxiStreamDmaV2Desc）

#### 4.3.1 概念说明

如果说写引擎是流水线，描述符管理器（`AxiStreamDmaV2Desc`）就是那位「调度员」兼「公告板管理员」。它做三件事，正好对应缓冲池模型的三个动作：

1. **派发缓冲**：维护一组「空闲缓冲 FIFO」（软件预投）。引擎来要描述符时，弹出一项，把其地址/buffId 作为 `dmaWrDescAck` 授权出去。
2. **回收回执**：引擎写完一帧返回 `dmaWrDescRet` 时，把它打包成一条 **128 位的完成描述符**，写进内存里一段「环形缓冲」，并把环形写指针 `wrIndex` 加 1（自动回绕 = 环）。
3. **通知软件**：每写一条完成描述符就累加中断请求计数，按可配节流（holdoff）驱动 `interrupt`，让软件来消费环形缓冲。

读侧对称：软件把「读请求描述符」（地址、大小、buffId、firstUser/lastUser……）投进读 FIFO，管理器弹出后作为 `dmaRdDescReq` 发给读引擎；读引擎完成后回 `dmaRdDescRet`，管理器同样写一条完成描述符进**读环形缓冲**（`rdBaseAddr` + `rdIndex`）。

整个模块还承载了全部 AXI-Lite 寄存器配置面（使能、基地址、maxSize、cache、中断、缓冲组阈值……），是软件控制 DMA 的唯一入口。

#### 4.3.2 核心流程

完成描述符进环形缓冲的核心是地址生成与回绕：

```
wrMemAddr = wrBaseAddr + (wrIndex << 4)     // 每条描述符128b=16字节
                                         │
            ┌────────────────────────────┘
            ▼
   内存: wrBaseAddr+0x00  ┌────────────────┐  ← 第0条完成描述符(128b)
          wrBaseAddr+0x10  ├────────────────┤  ← 第1条
          wrBaseAddr+0x20  ├────────────────┤  ← 第2条
            ...            ├────────────────┤
          wrBaseAddr+N     └────────────────┘
            ...                          ▲ wrIndex 回绕(2^DESC_AWIDTH_G)
```

`wrIndex` 是 `DESC_AWIDTH_G` 位宽（默认 12 位 = 4096 项），自然回绕成环。软件一边从环形缓冲消费完成描述符、一边往空闲缓冲 FIFO 补货，构成经典的**生产者-消费者**循环：

```
软件                        AxiStreamDmaV2Desc              写引擎
 │ 1.往空闲FIFO投地址+buffId      │                            │
 │ ────────────────────────────> │                            │
 │                               │ <──DescReq(tDest)──────── │ 2.帧到达
 │                               │ ──DescAck(地址,buffId)──> │
 │                               │                            │ 3.写内存
 │                               │ <──DescRet(size,结果)──── │ 4.写完
 │                               │ 5.写128b完成描述符入环      │
 │                               │    wrIndex++, 中断计数++    │
 │ <──── interrupt(ring有新条目)── │                            │
 │ 6.软件读环形缓冲,回收buffId     │                            │
 │   再投回空闲FIFO(回到第1步)     │                            │
```

#### 4.3.3 源码精读

先看模块对外的整体接口与一组关键本地常量：

[AxiStreamDmaV2Desc.vhd:L29-L86](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/dma/rtl/v2/AxiStreamDmaV2Desc.vhd#L29-L86) 实体声明。注意它同时有「写描述符」接口（`dmaWrDescReq/Ack/Ret/RetAck`）和「读描述符」接口（`dmaRdDescReq/Ack/Ret/RetAck`），还有一组 AXI4 写口（`axiWriteMasters/Slaves`）专门用来把完成描述符写回内存，以及 AXI-Lite 寄存器口。

[AxiStreamDmaV2Desc.vhd:L90-L104](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/dma/rtl/v2/AxiStreamDmaV2Desc.vhd#L90-L104) 关键常量。`AXI_DESC_CONFIG_C` 强制描述符总线为 **128 位**（`DATA_BYTES_C => 16`），所以一条完成描述符正好一次单拍写。`WR_FIFO_CNT_C=2`（写空闲缓冲 FIFO 数）、`RD_FIFO_CNT_C=4`（读请求 FIFO 数，因为读请求字段更多，分多条 32 位 FIFO 拼装）。

**空闲缓冲 FIFO** 是软件向硬件「投递空缓冲」的通道：

[AxiStreamDmaV2Desc.vhd:L294-L337](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/dma/rtl/v2/AxiStreamDmaV2Desc.vhd#L294-L337) FIFO 例化。两个 generate 各建一组同步 FWFT FIFO（`Fifo`，复用 u2-l2），深度 `2^DESC_AWIDTH_G`。写侧连 `r.wrFifoWr`/`r.rdFifoWr`（由寄存器写驱动），读侧连 `r.wrFifoRd`/`r.rdFifoRd`（由派发逻辑驱动）。

软件怎么往这些 FIFO 里投递？通过 AXI-Lite 寄存器写，配合 `axiWrDetect`（检测一次写事务）触发 `wrFifoWr`：

[AxiStreamDmaV2Desc.vhd:L458-L485](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/dma/rtl/v2/AxiStreamDmaV2Desc.vhd#L458-L485) 空闲 FIFO 的寄存器写入。例如写 `0x048` 把 `fifoDin` 推进写空闲 FIFO 0，写 `0x040`/`0x044`/`0x060`/`0x064` 推进读请求 FIFO，写 `0x070` 推进写空闲 FIFO 1。`fifoDin`（32 位）先由软件写 `0x040` 等地址的数据字段准备好，再由 `axiWrDetect` 在同一拍触发对应 FIFO 的写使能——这是 SURF 里「寄存器写触发 FIFO 入队」的标准手法。

> 完整的寄存器地图见 [L432-L499](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/dma/rtl/v2/AxiStreamDmaV2Desc.vhd#L432-L499)：`0x000` 使能/版本、`0x010/0x014` 写环形基地址、`0x018/0x01C` 读环形基地址、`0x028` maxSize、`0x02C` online、`0x050` 中断请求计数（RO）、`0x054` wrIndex（RO）、`0x058` rdIndex（RO）等，全部沿用 u3-l2 的四步骨架。

**派发缓冲**：当某写引擎申请描述符时，管理器仲裁多通道、弹 FIFO、组装 `dmaWrDescAck`：

[AxiStreamDmaV2Desc.vhd:L530-L601](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/dma/rtl/v2/AxiStreamDmaV2Desc.vhd#L530-L601) 写描述符请求仲裁。先把各通道 `dmaWrDescReq(i).valid` 收集进 `wrReqList`，用 `arbitrate`（复用 u2-l5 的轮询仲裁）选一个；选中后在 `dmaWrDescAck` 里填地址与 buffId：

[AxiStreamDmaV2Desc.vhd:L580-L595](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/dma/rtl/v2/AxiStreamDmaV2Desc.vhd#L580-L595) 授权字段组装。地址取自 FIFO 弹出值 `wrFifoDout` 的高 36 位（放到 40 位地址域的 `[39:4]`，低 4 位清零——16 字节对齐），`buffId` 取低 28 位，再叠上全局配置 `dropEn`/`contEn`/`maxSize`/`wrTimeout`。最后给选中通道 `dmaWrDescAck(i).valid=1` 并拉 `wrFifoRd=1` 弹出该 FIFO 项。

**回收回执并写环形缓冲**：这是本模块最核心的状态机，4 个状态 `IDLE_S → WRITE_S/READ_S → WAIT_S`：

[AxiStreamDmaV2Desc.vhd:L629-L648](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/dma/rtl/v2/AxiStreamDmaV2Desc.vhd#L629-L648) 环形地址生成与回执仲裁。`wrMemAddr = wrBaseAddr + (wrIndex & "0000")`（左移 4 = ×16），`rdMemAddr` 同理。IDLE_S 把各通道的写/读回执 `valid` 收集进 `descRetList`（每个通道占 2 位：写、读），用 `arbitrate` 选出一个待处理的回执。

[AxiStreamDmaV2Desc.vhd:L681-L710](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/dma/rtl/v2/AxiStreamDmaV2Desc.vhd#L681-L710) WRITE_S：把写回执打包成 128 位完成描述符。这是软件最终看到的内存布局，值得逐字段记下：

| 比特位 | 字段 |
|--------|------|
| 127 | 有效位 = 1 |
| 107:104 | 通道号 |
| 103:96 | tDest |
| 95:64 | 实际写入字节数 size |
| 63:32 | buffId |
| 31:24 | firstUser |
| 23:16 | lastUser |
| 15:8 | tId |
| 4 | result(3) ACK 超时 |
| 3 | continue（帧未完） |
| 2:0 | result(2:0) overflow+BRESP |

写完置 `awvalid`/`wvalid`、`wrIndex += 1`、给该通道 `dmaWrDescRetAck=1` 告诉引擎「回执已收」。`READ_S`（[L713-L739](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/dma/rtl/v2/AxiStreamDmaV2Desc.vhd#L713-L739)）对称，把读回执（buffId + 3 位 result）写进读环形缓冲，`rdIndex += 1`。

**中断节流**：每写完一条回执置 `intReqEn`，累加 `intReqCount`，按 `intHoldoff`（默认 10000 拍 ≈ 20 kHz）节流后才真正拉 `interrupt`：

[AxiStreamDmaV2Desc.vhd:L772-L807](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/dma/rtl/v2/AxiStreamDmaV2Desc.vhd#L772-L807) 中断与软件 ACK。`intReqCount` 是固件已投递、尚未被软件确认的回执数；当它非 0 且超过 holdoff 窗口，或软件写 `forceInt`，才驱动 `interrupt := intEnable`。软件处理完一批后写 `0x04C`（带 `intSwAckReq`）回 ACK，DSP 减法器算出 `intReqCount - intAckCount` 的差值作为新基线——这是「中断合并 + 软件确认」的标准做法，避免每帧一中断。

**缓冲组流控**（可选）：管理器还按 `tId` 低 3 位统计在途缓冲数 `idBuffCount`，超过阈值 `idBuffThold` 时拉 `buffGrpPause` 反压对应分组（[L382-L392](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/dma/rtl/v2/AxiStreamDmaV2Desc.vhd#L382-L392) 与 [L845-L861](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/dma/rtl/v2/AxiStreamDmaV2Desc.vhd#L845-L861)），防止某组缓冲被饿死。

#### 4.3.4 代码实践

**目标**：把「描述符如何给出基地址与长度、写引擎如何把帧写到内存并回写状态」这条完整链路，落到管理器这一侧——亲手配置一个最小写通路并观察 `wrIndex` 推进。

**步骤**（阅读 + 仿测试，参考 `tests/axi/dma/test_AxiStreamDmaV2.py` 的寄存器驱动范式）：

1. 打开 `AxiStreamDmaV2Desc.vhd` 的寄存器段（[L432-L499](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/dma/rtl/v2/AxiStreamDmaV2Desc.vhd#L432-L499)），按下列顺序规划一组 AXI-Lite 写：
   - `0x010`/`0x014` 写 `wrBaseAddr`（如 `0x1000_0000`）——完成描述符环形缓冲的起点。
   - `0x028` 写 `maxSize`（如 `0x1000` = 4 KB）——每个缓冲上限。
   - `0x088` 写 `wrTimeout`——BRESP 超时。
   - `0x048` 反复写：先写 `fifoDin`（地址+buffId 拼成的 32 位值），由 `axiWrDetect` 推进写空闲 FIFO 0——这是在「向缓冲池投递空缓冲」。
   - `0x004` 写 `intEnable=1`、`0x000` 写 `enable=1` 启动。
2. 想象一个写引擎此时收到一帧：它发 `dmaWrDescReq`，管理器弹 FIFO、给 `dmaWrDescAck`（地址来自你投的 `fifoDin`，maxSize 来自 `0x028`）。
3. 写引擎写完回 `dmaWrDescRet`（size=帧长，result=0）。管理器进 `WRITE_S`，把上表 128 位描述符写到 `wrBaseAddr + wrIndex*16`，`wrIndex` 从 0 变 1。
4. 读回 `0x054`（`wrIndex`，RO）应已递增；读回 `0x050`（`intReqCount`，RO）应为 1。

**需要观察的现象**：

- `wrIndex` 每完成一帧加 1，到达 `2^DESC_AWIDTH_G` 后回绕到 0（环形）。
- 内存里 `wrBaseAddr + 0x00` 处出现一条 bit127=1 的 128 位描述符，其中 `[95:64]` = 帧长、`[63:32]` = 你投的 buffId。
- 若 `intEnable=1`，`interrupt` 在 holdoff 窗口后被拉起。

**预期结果**：你能在不写一行 HDL 的前提下，用寄存器读写驱动一次完整 DMA（投缓冲→使能→等中断→读环形缓冲）。若手头有仿真环境，可运行：

```
./.venv/bin/python -m pytest -q tests/axi/dma/test_AxiStreamDmaV2.py
```

它正是用 AXI-Lite 写 `0x02C`/`0x030`/`0x004`/`0x000` 并读回状态来验证这条寄存器通路（见 [test_AxiStreamDmaV2.py:L65-L89](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/axi/dma/test_AxiStreamDmaV2.py#L65-L89)）。若无可运行环境，以上为「源码阅读 + 寄存器规划型实践」，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：环形缓冲的「环」靠什么实现？容量多大？
<details><summary>参考答案</summary>
靠 `wrIndex` 的自然回绕：它是 `DESC_AWIDTH_G` 位宽（默认 12 位），加到 `2^DESC_AWIDTH_G`（4096）后自动溢出回 0，于是 `wrBaseAddr + wrIndex*16` 在一段 `2^DESC_AWIDTH_G × 16` 字节的内存里循环写。容量就是 `2^DESC_AWIDTH_G` 条完成描述符。</details>

**练习 2**：完成描述符里 bit127 恒为 1 有什么用？
<details><summary>参考答案</summary>
作为「有效标志」。软件扫描环形缓冲时，读到的每一项只要 bit127=1 就表示「固件已写入一条未消费的完成描述符」；软件消费后需自行清零或靠 `wrIndex`/`rdIndex` 的读写指针来区分已消费与未消费项。它让裸内存具备自描述能力。</details>

**练习 3**：为什么读请求 FIFO 有 4 条（`RD_FIFO_CNT_C=4`）而写空闲 FIFO 只有 2 条（`WR_FIFO_CNT_C=2`）？
<details><summary>参考答案</summary>
因为一次读请求要携带的字段多（地址、buffId、size、firstUser、lastUser、dest、continue……），一条 32 位 FIFO 装不下，需拆成 4 条 32 位 FIFO 并行装载，拼成一条完整的 128 位读请求描述符；而写空闲缓冲只需「地址 + buffId」两项，2 条 32 位 FIFO 足够。字段多少决定了 FIFO 条数。</details>

## 5. 综合实践

把本讲三个模块串起来，完成一次「描述符驱动写 DMA」的全链路推演与配置规划。

**任务**：假设你要把一台 ADC 通过一条 AXI-Stream（64 位、`tDest=0x05`）持续送来的数据帧落到 DDR 的一段缓冲池里，每帧约 8 KB，软件异步消费。请回答并规划：

1. **缓冲池容量**：若软件最坏 10 ms 才来消费一次，ADC 帧率 10 kHz，需要至少多少个缓冲？`DESC_AWIDTH_G` 至少设多少？
2. **寄存器配置清单**：列出启动前要写 `AxiStreamDmaV2Desc` 的哪些寄存器地址、各写什么值（设 `wrBaseAddr`、`maxSize`、投递若干空缓冲、使能、开中断）。
3. **一帧的生命历程**：用本讲 4.2 与 4.3 的状态机，描述第 1 帧从 `tDest=0x05` 到达流口、到软件在环形缓冲里读到它的完成描述符，依次经过哪些状态、哪些记录在哪两个模块间传递。
4. **续写场景**：若某帧 12 KB 超过 `maxSize`（8 KB）且 `contEn=1`，描述引擎如何在 `MOVE_S` 收尾、`continue` 如何在回执里置位、软件如何据 `continue=1` 把两段缓冲拼成一帧。

**参考思路**：

1. 10 ms × 10 kHz = 100 帧，故缓冲池至少 100 项；`DESC_AWIDTH_G` 需 ≥ 7（`2^7=128`），向上取 2 的幂并预留在途余量，可设 8 或更大。注意环形缓冲容量 = `2^DESC_AWIDTH_G` 必须大于「软件消费间隔内的最大帧数」，否则会「满」丢回执。
2. 写 `0x010/0x014`（wrBaseAddr）、`0x028`（maxSize=0x2000）、`0x088`（wrTimeout）、连续写 `0x048` 投入 ≥100 个空缓冲（每个 = 地址高 28 位拼 buffId）、`0x004`（intEnable=1）、`0x000`（enable=1）。
3. 流口 `tDest=05` 到达 → 写引擎 `IDLE_S` 查 tracking RAM（首次 `inUse=0`）→ `REQ_S` 发 DescReq → 管理器弹写 FIFO、回 DescAck（地址/buffId/maxSize）→ 写引擎 `ADDR_S` 算 AWLEN、`MOVE_S` 逐拍写内存 → 帧末 `tLast` → `RETURN_S` 等 `ackCount=reqCount` → 回 DescRet（size=8KB, result=0, buffId）→ 管理器 `IDLE_S→WRITE_S` 写 128b 描述符到 `wrBaseAddr+wrIndex*16`、`wrIndex++`、`intReqCount++` → holdoff 后 `interrupt` → 软件 `0x054` 读 wrIndex、读内存环形缓冲拿回执。
4. 前 8 KB 写满时 `MOVE_S` 检测到 `maxSize` 归零且 `contEn=1`，置 `continue=1`、`inUse=0`，走 `RETURN_S` 回首个缓冲的回执（size=8KB, continue=1）；管理器派发下一个缓冲；引擎再 `REQ_S` 取新地址续写剩余 4 KB，第二条回执 size=4KB、continue=0（帧末 `tLast`）。软件据 `continue=1` 知道这两条回执同属一帧，按 buffId 顺序拼接。

> 若你有仿真环境，可把上述配置写进一个仿照 `test_AxiStreamDmaV2.py` 的 cocotb 测试，用 `AxiLiteMaster` 驱动寄存器、用 `AxiStreamSource` 喂帧、用 `AxiRam` 当内存、最后断言环形缓冲里的描述符字段；否则以上为「源码阅读 + 配置规划型实践」，待本地验证。

## 6. 本讲小结

- `AxiDmaPkg` 用一组记录定义了 DMA 的全部契约：请求（`DescReq`，只要 dest/id）、授权（`DescAck`，给地址/buffId/maxSize/contEn/dropEn）、回执（`DescRet`，回 size/firstUser/lastUser/result/continue），地址由管理器分配而非调用方给出——这是「描述符驱动」的本质。
- `AxiStreamDmaV2Write` 用 `IDLE→REQ→ADDR→MOVE→PAD→META→RETURN` 状态机把一帧流逐突发写进 AXI4 内存，突发长度由 `getAxiLenProc` 对齐到 4 KB；tracking RAM 按 `tDest` 索引保存每路 VC 的在途上下文，从而支持交错帧。
- `RETURN_S` 靠 `ackCount=reqCount` 等齐所有 BRESP 才置回执 `valid`，保证「完成」即「数据真正落地」；溢出时 `contEn` 决定续写或丢弃，并在恰好填满时走 continue 路径以避免越界写。
- `AxiStreamDmaV2Desc` 是缓冲池调度员：用空闲缓冲 FIFO（软件预投）向引擎派发描述符，把完成回执打包成 128 位描述符写进 `wrBaseAddr + wrIndex*16` 的环形缓冲，`wrIndex` 自然回绕成环。
- 中断采用「请求计数 + holdoff 节流 + 软件 ACK 差值」的合并机制，避免每帧一中断；缓冲组计数 `idBuffCount` 提供按 tId 分组的反压。
- 顶层 `AxiStreamDmaV2` 是纯结构拼装：1 个 `AxiStreamDmaV2Desc` + N 个（`AxiStreamDmaV2Read` + `AxiStreamDmaV2Write` + `AxiStreamDmaV2WriteMux`），写数据与描述符经 WriteMux 时分复用同一 AXI4 写总线，保证回执在数据之后发出。

## 7. 下一步学习建议

- **读引擎对称侧**：阅读 [AxiStreamDmaV2Read.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/dma/rtl/v2/AxiStreamDmaV2Read.vhd)，对照本讲写引擎，理解它如何用「请求引擎」与「数据收集引擎」两个并行状态机，把内存突发读出再组装成 AXI-Stream 帧（注意 `PEND_THRESH_G` 控制的预取水位）。
- **WriteMux 的总线复用**：阅读 [AxiStreamDmaV2WriteMux.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/dma/rtl/v2/AxiStreamDmaV2WriteMux.vhd)，看它如何用 5 状态机把「数据写」与「描述符写」时分复用到一根总线上，并保证描述符在数据之后发出（注释里 "make sure that the write descriptor is sent after the data is sent"）。
- **回归测试**：浏览 `tests/axi/dma/` 目录，尤其是 `test_AxiStreamDmaRingWrite.py`、`test_AxiStreamDmaV2WriteContinue.py`、`test_AxiStreamDmaV2Desc.py`，它们分别覆盖环形缓冲、continue 续写、描述符管理器三个核心场景，是理解边界条件的最佳范例。
- **软件侧**：本讲的环形缓冲/描述符格式与用户态驱动 `aes-stream-driver` 一一对应（寄存器 `0x000` 的版本号 5 正是给该驱动 case 用）。建议结合该驱动源码理解软件如何消费环形缓冲、回投空缓冲，闭环「硬件描述符 ↔ 软件缓冲池」。
- **进阶单元**：本讲属单元八。后续可将 DMA 放回真实系统，结合 u6 以太网与 u7 链路协议，理解一帧从光纤/网口进来、经 AXI-Stream 落到内存的完整数据通路。
