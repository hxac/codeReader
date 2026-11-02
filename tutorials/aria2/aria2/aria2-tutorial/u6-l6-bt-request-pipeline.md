# 请求流水线：DefaultBtRequestFactory 与排队策略

## 1. 本讲目标

上一讲（u6-l4）我们看清了 `DefaultBtInteractive::doInteractionProcessing()` 的固定节律：收消息 → 分发 → 决策 → **补请求流水线** → 冲刷。本讲下钻其中的「补请求」一环，读完你应当能够：

1. 解释 BT 请求流水线（pipelining）的深度为什么初始是 6、上限是 256，以及深度在什么条件下翻倍。
2. 复述 `DefaultBtRequestFactory` 如何从目标 piece 中挑出「缺失且未被占用」的 16KB 块，生成 `request` 消息。
3. 说出超时请求（`--bt-request-timeout`，默认 60 秒）如何被清账重排，以及 snubbing 标志的作用。
4. 对比 end-game（收官）模式与普通模式在「领 piece」和「挑块」两层的策略差异，并指出模式切换的判断条件。
5. 理解 BT 的 piece 请求与 HTTP/FTP 的 segment 下载共用同一个 `PieceStorage` 时，两套切片如何互不踩脚。

## 2. 前置知识

- **块（block）与 piece 两层切分**（承接 u3-l4/u5-l2）：文件按 piece 切分（大小由种子 info 字典给出），piece 内再按 16KB 的 block 切分。BT 协议里 `request`/`piece`/`cancel` 消息的三元组 `(index, begin, length)` 寻址的正是块：`index` 是 piece 编号，`begin` 是块在 piece 内的字节偏移，`length` 是块长。
- **在途请求与 RequestSlot 账本**（承接 u6-l5）：`DefaultBtMessageDispatcher` 的 `requestSlots_` 记录本连接已发出但尚未收到数据的请求。所谓「流水线深度」就是在途请求的数量上限。
- **流水线的直觉**：假设到某 peer 的往返时延（RTT）是 100ms、块长 16KB。如果串行地「发一个 request → 等一个 piece → 再发下一个」，单连接吞吐只有 \( 16\mathrm{KB} / 0.1\mathrm{s} = 160\mathrm{KB/s} \)。若同时在途 \( D \) 个请求，理论上限提高到 \( D \times 16\mathrm{KB} / \mathrm{RTT} \)——这就是 pipelining。深度太小撑不满带宽，太大则浪费 peer 缓冲、超时损失也大，所以需要动态调整。
- **收官（end-game）问题**：下载临近结束时，剩下的缺失 piece 可能全部「在别的连接手里在途」。按普通规则（只领未被占用的 piece）本连接领不到新任务，流水线空转；而持有者在等慢 peer。经典解法是收官阶段允许**多个连接同时请求同一 piece 的不同缺失块**，谁先到算谁的，输家用 `cancel` 撤销。
- **useBitfield（在用位图）**（承接 u3-l4）：`BitfieldMan` 用「完成位图 + 在用位图 + 过滤位图」三张位图记账。「缺失且未在用」= 位运算上「未完成且未被占」的块，是普通模式挑块的依据。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/DefaultBtRequestFactory.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtRequestFactory.h) / [.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtRequestFactory.cc) | 本讲主角：持有目标 piece 队列，生成 `BtRequestMessage`，普通/收官两套挑块策略 |
| [src/BtConstants.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtConstants.h) | 流水线深度与块长常量：`DEFAULT_MAX_OUTSTANDING_REQUEST`、`UB_MAX_OUTSTANDING_REQUEST`、`MAX_BLOCK_LENGTH` |
| [src/DefaultBtInteractive.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc) | `addRequests`/`fillPiece`/深度自适应的宿主，收官模式的触发点 |
| [src/DefaultBtMessageDispatcher.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageDispatcher.cc) | 在途请求账本：超时清理、撞车检测、`isOutstandingRequest` |
| [src/DefaultPieceStorage.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc) / [.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.h) | `isEndGame`/`enterEndGame` 标志、领 piece 的普通/收官两个分支、HTTP/FTP 的 `StreamPieceSelector` 装配 |
| [src/InorderStreamPieceSelector.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InorderStreamPieceSelector.cc) / [src/StreamPieceSelector.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/StreamPieceSelector.h) | HTTP/FTP 侧的顺序选片策略，与 BT 的 `PieceSelector` 接口对照 |
| [src/Piece.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.cc) / [src/BitfieldMan.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BitfieldMan.cc) | 块级位图运算：`getMissingUnusedBlockIndex`、`getAllMissingBlockIndexes` |
| [src/SegmentMan.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc) | HTTP/FTP 连接把 piece 包装成 `PiecedSegment` 的 `checkoutSegment`，两套切片的交汇点 |

## 4. 核心概念与源码讲解

### 4.1 DefaultBtRequestFactory：目标 piece 队列与块请求生成

#### 4.1.1 概念说明

一个 BT 连接同一时刻通常并行下载若干个 piece（每个 piece 又由多个 16KB 块组成）。`DefaultBtRequestFactory` 就是这条连接的「弹药库 + 装填手」：

- **弹药库**：成员 `pieces_`（`std::deque<std::shared_ptr<Piece>>`）记录本连接正在下的目标 piece；
- **装填手**：`createRequestMessages(max, endGame)` 从目标 piece 里挑块、经消息工厂铸成 `BtRequestMessage`；
- **回收员**：piece 完成、被 choke、连接关闭时负责把 piece 归还 `PieceStorage`。

分工上，`DefaultBtInteractive` 只说「还差几个请求」，工厂决定「从哪个 piece 的哪个块造」。

#### 4.1.2 核心流程

```text
每个交互 tick（PeerInteractionCommand 驱动，见 u6-l4）：
  1. removeCompletedPiece()      # 已完成的 piece 出队并撤销其在途请求
  2. addRequests()
     a. 若无「缺失且未占用」的 piece 且未进收官 → enterEndGame()
     b. fillPiece(D)             # 目标 piece 的缺失块总数不足 D 时领新 piece
     c. reqNumToCreate = max(0, D - countOutstandingRequest())
     d. createRequestMessages(reqNumToCreate, endGame) → 入发送队列
```

普通模式挑块：顺序遍历 `pieces_`，对每个 piece 调 `getMissingUnusedBlockIndex`，取出「**未完成 ∧ 未在用**」的前 N 个块索引，同时**立即置在用位**（防止重复请求同一块），再逐个生成 `request` 消息。

#### 4.1.3 源码精读

先看类的状态定义——工厂的全部家当就是这张目标 piece 队列和四项依赖：

[src/DefaultBtRequestFactory.h:L52-L62](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtRequestFactory.h#L52-L62) 定义了 `DefaultBtRequestFactory`：`pieces_` 是目标 piece 双端队列，`pieceStorage_`/`peer_`/`dispatcher_`/`messageFactory_` 分别提供归还 piece、allowed-fast 判定、在途账本与消息铸造能力。

普通模式的请求生成：

[src/DefaultBtRequestFactory.cc:L166-L195](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtRequestFactory.cc#L166-L195) `createRequestMessages(max, endGame)` 先看 `endGame` 分流（本节先读普通分支）；随后按 `getnum = max - requests.size()` 的配额遍历目标 piece，对每个 piece 调 `piece->getMissingUnusedBlockIndex(blockIndexes, getnum)` 拿到一批块索引，逐个 `messageFactory_->createRequestMessage(piece, *i)` 铸成请求，并打出 `Creating RequestMessage index=..., begin=..., blockIndex=...` 的 debug 日志——注意 `begin` 是 `blockIndex × piece->getBlockLength()`，正好印证了块寻址公式。

块的「缺失且未在用」判定与在用置位在 `Piece`：

[src/Piece.cc:L141-L153](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.cc#L141-L153) `getMissingUnusedBlockIndex(indexes, n)` 委托 `BitfieldMan::getFirstNMissingUnusedIndex` 取前 N 个可用块，**取到后立刻 `setUseBit`**。这一步是防重复的关键：同一连接不会对同一块发出两个在途 request。

再看位图运算本体：

[src/BitfieldMan.cc:L188-L202](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BitfieldMan.cc#L188-L202) `getFirstNMissingUnusedIndex` 的选择条件是 `~bitfield & ~useBitfield`（启用过滤时再交 `filterBitfield`），即「未完成且未在用」的块按索引升序取前 N 个——普通模式下块是**顺序**分配的。

目标 piece 队列的三种回收出口：

- [src/DefaultBtRequestFactory.cc:L91-L98](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtRequestFactory.cc#L91-L98) `removeCompletedPiece`：已完成的 piece 先让 dispatcher 撤销其残余在途请求（`doAbortOutstandingRequestAction`），再移出队列。
- [src/DefaultBtRequestFactory.cc:L148-L155](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtRequestFactory.cc#L148-L155) `doChokedAction`：被对端 choke 后，不属于该 peer allowed-fast 集合（`peer_->isInPeerAllowedIndexSet`）的 piece 全部 `cancelPiece` 归还存储层并出队——choke 意味着这些 piece 要不到了，与其占着不如让别人下。
- [src/DefaultBtRequestFactory.cc:L157-L164](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtRequestFactory.cc#L157-L164) `removeAllTargetPiece`：连接关闭时的全量清理。

辅助计数：

[src/DefaultBtRequestFactory.cc:L258-L262](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtRequestFactory.cc#L258-L262) `countMissingBlock` 累加所有目标 piece 的缺失块数，供 `fillPiece` 判断「弹药是否够填满流水线」。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到普通模式下 request 按块顺序生成。
2. **操作步骤**：
   - 用 debug 级日志启动一次种子下载（待本地验证）：
     ```bash
     ./src/aria2c --seed-time=0 -l bt.log --log-level=debug <torrent 文件>
     ```
   - 在日志中检索 `Creating RequestMessage`（大小写按 aria2 日志惯例）。
3. **需要观察的现象**：同一 CUID 的相邻日志行里，`blockIndex` 从小到大递增；一条 piece 的块用尽后切换到下一个 `index`。
4. **预期结果**：能按日志复原「每个 tick 为每个目标 piece 顺序取块」的装填过程，且同一 `(index, blockIndex)` 不会在收到对应 `piece` 前重复出现。
5. 若手头没有可用的种子与网络环境，本实践结果**待本地验证**；可退化为阅读 `test/DefaultBtRequestFactoryTest.cc` 中对 `createRequestMessages` 的断言来确认行为。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `getMissingUnusedBlockIndex` 取到块后要立刻 `setUseBit`？如果不置位会发生什么？

**答案**：在用位是「本连接已对该块发出在途请求」的记账。不置位的话，下一个 tick 的 `createRequestMessages` 会再次挑中同一块，向同一 peer 重复发出相同 request，浪费带宽且让 RequestSlot 对账出现重复条目；对端若开启 Fast 扩展还可能回 `reject`。

**练习 2**：`doChokedAction` 为什么保留 allowed-fast 集合中的 piece？

**答案**：BT 的 Fast Extension 约定，对端即使在 choke 状态下也承诺持续发送 allowed-fast 集合内的 piece 数据。所以被 choke 时这部分 piece 仍然「要得到」，不必归还。

**练习 3**：`createRequestMessages` 里 `begin` 的计算为什么是 `blockIndex × piece->getBlockLength()` 而不是 `blockIndex × 16384`？

**答案**：因为 piece 的**最后一个块**可能不足 16KB——但更重要的是最后一个块的长度由 `BitfieldMan::getBlockLength(index)` 按剩余字节数截短（见 [src/BitfieldMan.cc:L141-L148](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BitfieldMan.cc#L141-L148)），除尾块外每块长度一致，因此块内偏移仍由块索引乘块长得到；`getBlockLength()` 无参版本返回的是该 piece 的标准块长。用常量 16384 硬编码在尾块长度变化时会算错 `request.length`。

### 4.2 BtConstants 常量与流水线深度的动态调整

#### 4.2.1 概念说明

流水线深度不是写死的：aria2 以 6 起步，视「消化速度」逐步翻倍到上限 256。相关常量集中在 `BtConstants.h`：

| 常量 | 值 | 含义 |
| --- | --- | --- |
| `DEFAULT_MAX_OUTSTANDING_REQUEST` | 6 | 每 peer 在途请求的初始上限 |
| `UB_MAX_OUTSTANDING_REQUEST` | 256 | 在途请求上限的上界（注释：Upper Bound） |
| `MAX_BLOCK_LENGTH` | 64KB | 单个请求/piece 消息载荷的最大长度上限 |
| `Piece::BLOCK_LENGTH` | 16KB | 实际请求块长（[src/Piece.h:L75](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.h#L75)） |

`MAX_BLOCK_LENGTH`（64KB）并不用于常规请求——常规块是 16KB。它是消息层的防御上限：`BtPieceMessage` 断言收到的载荷不超过它（[src/BtPieceMessage.cc:L217](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtPieceMessage.cc#L217)），`PeerConnection` 的接收缓冲容量也按它预留。

#### 4.2.2 核心流程

深度自适应的信号是「这一轮收消息消化掉了多少在途请求」。设本 tick 开始时在途数为 \( C_{old} \)，结束时为 \( C_{new} \)，当前深度为 \( D \)，则：

\[
\text{若 } \lnot \text{endGame} \;\land\; (C_{old} - C_{new}) \times 4 \ge D, \quad D \leftarrow \min(256,\; 2D)
\]

直觉：如果一个回合就消化掉了四分之一以上的在途请求，说明网络远比流水线快，深度翻倍；否则维持。深度只增不减（进入收官后停止增长），连接重建时从 6 重新起步。

每轮补货数量则是：

\[
\text{reqNumToCreate} = \max(0,\; D - C_{new})
\]

#### 4.2.3 源码精读

常量定义：

[src/BtConstants.h:L49-L54](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtConstants.h#L49-L54) 定义 `MAX_BLOCK_LENGTH`、`DEFAULT_MAX_OUTSTANDING_REQUEST = 6` 与 `UB_MAX_OUTSTANDING_REQUEST = 256`。

深度成员的初值：

[src/DefaultBtInteractive.cc:L106](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L106) 构造函数把 `maxOutstandingRequest_` 初始化为 `DEFAULT_MAX_OUTSTANDING_REQUEST`（6）。

消化量的采集：

[src/DefaultBtInteractive.cc:L301-L318](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L301-L318) `receiveMessages` 在进入收消息循环**之前**记录 `countOldOutstandingRequest`，循环内每收到一条消息就 `doReceivedAction()`——`piece` 消息到达时 dispatcher 会核销对应 RequestSlot，`countOutstandingRequest()` 随之下降。

翻倍判定：

[src/DefaultBtInteractive.cc:L341-L348](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L341-L348) 收完消息后比较新旧在途数：若 `(旧-新)×4 ≥ maxOutstandingRequest_` 则深度翻倍并钳制在 256。注意条件里的 `!pieceStorage_->isEndGame()`——收官阶段不扩容。

补流水线的总入口：

[src/DefaultBtInteractive.cc:L414-L432](https://github.com/aria2/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L414-L432) `addRequests` 是三步曲：可能切入收官（下一节展开）→ `fillPiece(maxOutstandingRequest_)` 保证目标 piece 的缺失块总量不低于深度 → 按差额 `reqNumToCreate` 调工厂造请求并入队。

`fillPiece` 的领片逻辑：

[src/DefaultBtInteractive.cc:L372-L412](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L372-L412) 先算 `diffMissingBlock = maxMissingBlock - countMissingBlock()`，再按「是否被 choke × 是否收官」四路领取：未 choke 走 `getMissingPiece`；被 choke 但对端开了 Fast 扩展走 `getMissingFastPiece`（只从 allowed-fast 集合挑）；收官时两种都会附加「排除已有目标 piece」的参数。领到的 piece 逐个 `addTargetPiece` 入队。

节律与调用位置：

[src/DefaultBtInteractive.cc:L566-L579](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L566-L579) `doInteractionProcessing` 的稳态分支里，`checkRequestSlotAndDoNecessaryThing()`（超时清理，见 4.3）以 1 秒为周期执行，而 `removeCompletedPiece()` 与 `addRequests()` **每个 tick** 都跑——补流水线是交互循环的高频动作。

#### 4.2.4 代码实践

1. **实践目标**：观察深度从 6 逐步翻倍的过程。
2. **操作步骤**（待本地验证）：用一个健康种子下载，`--log-level=info` 即可（发送侧消息日志为 INFO 级，见 [src/SimpleBtMessage.cc:L56](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SimpleBtMessage.cc#L56)，格式见 [src/RangeBtMessage.cc:L66-L70](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RangeBtMessage.cc#L66-L70)）：
   ```bash
   ./src/aria2c --seed-time=0 -l bt.log --log-level=info <torrent>
   # 统计单个 CUID 的在途 request 窗口
   grep "request index=" bt.log | head -100
   ```
3. **需要观察的现象**：某 CUID 发出的 `request` 与已收到的 `piece` 之差（粗略的在途数）先停在个位数，随吞吐稳定而台阶式上涨。
4. **预期结果**：在日志中能看到同一 CUID 的请求间隔随时间明显变大（一次 tick 装填更多），对应深度翻倍；进入收官后不再增长。
5. 精确数值需 debug 级日志与脚本统计，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么翻倍条件用 `(旧-新)×4 ≥ D` 而不是「只要消化了就翻倍」？

**答案**：这是迟滞（hysteresis）设计。一个 tick 只消化一两个请求属于正常波动；只有单回合消化量达到深度的四分之一，才足以证明「流水线是瓶颈」。避免深度被噪声反复推高——深度过大时对端缓冲占用多、超时损失大。

**练习 2**：深度上限为什么是 256 而不是无限？

**答案**：在途请求占用对端发送缓冲与本端 RequestSlot 内存（每个 slot 记三元组与时间戳）；深度超过「带宽 × RTT / 16KB」后再增加只会积压而非提速。256 × 16KB = 4MB 窗口对单连接已非常宽裕。

**练习 3**：`fillPiece` 的参数为什么是 `maxOutstandingRequest_`（请求数）而不是 piece 数？

**答案**：流水线按**块**计数，而 piece 含多个块。`fillPiece` 的语义是「让目标 piece 集合的缺失块总数 ≥ 深度」，以块为单位对齐流水线口径，避免「领了 piece 却填不满流水线」或「领太多 piece 长期占着」。

### 4.3 超时请求的重排：checkRequestSlotAndDoNecessaryThing

#### 4.3.1 概念说明

发出 request 后对方可能迟迟不回（慢 peer、即将断线）。aria2 给每个 RequestSlot 记了派遣时间，超过 `--bt-request-timeout`（默认 60 秒，范围 1–600，隐藏选项）即判超时：

1. **超时路径**：撤销块的在用记账（`cancelBlock`）并把 peer 标记为 **snubbing**（「被怠慢」）。snubbing 会影响 choke 算法对该 peer 的分类（见 [src/BtLeecherStateChoke.cc:L222](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtLeecherStateChoke.cc#L222) 附近对 snubbing peer 的处理）。
2. **撞车路径**：块已被**别的连接**先下回来（`piece->hasBlock`）。为避免对端白传数据，主动发 `cancel` 消息撤销在途请求。

两条路径都会把 slot 从账本删除；下一个 tick 的 `addRequests` 发现 `countOutstandingRequest()` 变小，自然补发新请求——超时块的 `cancelBlock` 已清掉在用位，它会重新成为可选项。

#### 4.3.2 核心流程

```text
每 1 秒（perSecTimer_ 节流，见 doInteractionProcessing L566-569）:
  for slot in requestSlots_:
    if slot.dispatched_at 距今 > bt-request-timeout:
        slot.piece.cancelBlock(slot.blockIndex)   # 清在用位 → 块可被再次请求
        peer.snubbing(true)
    elif slot.piece.hasBlock(slot.blockIndex):    # 别的连接已把该块下回来
        queue.push(cancel(index, begin, length))
  移除命中的 slot
下一 tick 的 addRequests() 按差额补新请求
```

#### 4.3.3 源码精读

[src/DefaultBtMessageDispatcher.cc:L215-L242](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageDispatcher.cc#L215-L242) `checkRequestSlotAndDoNecessaryThing` 一次遍历同时处理超时与撞车，随后用 `remove_if` 统一清除命中槽位。超时分支打印 `MSG_DELETING_REQUEST_SLOT_TIMEOUT` 日志，撞车分支打 `..._ACQUIRED` 并构造 cancel 消息。

超时时长的来源：

[src/PeerInteractionCommand.cc:L182-L183](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L182-L183) 会话建立时把选项 `PREF_BT_REQUEST_TIMEOUT` 灌给 dispatcher；[src/OptionHandlerFactory.cc:L1633-L1638](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L1633-L1638) 显示该选项默认 60、范围 1–600 且 `hide()`（不出现在 --help）。

日志文本（实践时用来检索）：

[src/message.h:L85-L88](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h#L85-L88) 定义了 `... because of time out` 与 `... because the block has been acquired.` 两种删除原因的日志模板。

#### 4.3.4 代码实践

1. **实践目标**：在日志中区分两类请求槽删除。
2. **操作步骤**（待本地验证）：`--log-level=debug` 下载种子，之后：
   ```bash
   grep "Deleting request slot" bt.log | grep -c "time out"
   grep "Deleting request slot" bt.log | grep -c "been acquired"
   ```
3. **需要观察的现象**：健康网络下 `time out` 很少；**收官阶段** `been acquired` 明显增多——这正是 4.4 节「多连接竞速同一 piece」的痕迹。
4. **预期结果**：两类计数随下载阶段分布不同，收官段 acquired 占比升高。
5. 数值取决于网络与种子热度，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：超时清理为什么放在「每秒一次」的节流里，而不是每个 tick 都跑？

**答案**：超时判定以秒为粒度已足够（默认阈值 60 秒），而交互循环每秒可能跑很多次；节流把 O(在途数) 的全量扫描成本摊薄到 1Hz，符合该函数的「周期巡检」语义。

**练习 2**：`cancelBlock` 之后这个块会被谁重新请求？一定是原来的连接吗？

**答案**：不一定。`cancelBlock` 只清 piece 内位图的在用位；下一个 `addRequests` 周期里，**本连接**若还持有该 piece 就可能重选它，但若 piece 被归还（如 choke），任何对该 piece 感兴趣的连接都能在领取后请求该块。超时块转移到更快的 peer 正是这一机制的目的。

**练习 3**：`snubbing` 标志置位后，本讲范围内它的直接效果是什么？

**答案**：在本讲的直接效果是随超时记录一起暴露给 choke 算法——`BtLeecherStateChoke` 会把 snubbing 的 peer 从常规 unchoke 候选中特殊处理（视作只值得乐观 unchoke），从而减少对慢 peer 的上传回报；它并不直接阻止继续向该 peer 请求。

### 4.4 end-game 模式：触发条件与请求策略变化

#### 4.4.1 概念说明

普通模式有一条隐含假设：**piece 独占**——一个 piece 被某连接领取（在用位置位）后，其他连接不再碰它，靠 `~useBitfield` 过滤。这在前 95% 的时间都工作良好；但尾声时剩余缺失 piece 可能全部在途于别的连接，按独占规则本连接将无片可领、流水线清零，而持有者若恰好连着慢 peer，最后几块会拖很久。

end-game（收官）模式改变两层策略：

| 层 | 普通模式 | 收官模式 |
| --- | --- | --- |
| 领 piece（PieceStorage） | 只领「缺失且未在用」的 piece（独占） | 允许领取「已在用」的 piece（**共享**），随机挑选 |
| 挑块（RequestFactory） | 顺序取「未完成 ∧ 未在用」的块 | 收集**全部缺失块**，随机洗牌，跳过本连接已有在途请求的块 |

竞速的善后是 4.3 的撞车路径：某块被任一连接下回来后，其余连接发 `cancel` 撤销重复请求。代价是少量冗余流量，收益是避免「最后一块等慢 peer」的长尾。

#### 4.4.2 核心流程

```text
addRequests() 每 tick:
  if 未收官 且 不存在「缺失且未在用」的 piece:   # 触发条件
      pieceStorage.enterEndGame()                # 置 endGame_ = true（单向，不可逆）

收官后的 fillPiece:
  getMissingPiece(pieces, diff, peer, exclude=本连接已有目标 piece, cuid)
    → 存储层收集全部缺失 piece（含在用），shuffle 后领取
    → 例外：getUsedBySegment() 的 piece（HTTP/FTP 正用）不共享

收官后的 createRequestMessagesOnEndGame(max):
  for piece in pieces_:
      收集 piece 的全部缺失块（不看在用位）
      洗牌
      逐块检查 dispatcher.isOutstandingRequest(index, block)
        → 无在途才生成 request（保证本连接不向同一 peer 重复要同一块）
```

注意「共享」的精确边界：end-game 允许**多个连接**各自向**各自的 peer** 请求同一 piece 的块；但**同一连接**内仍靠 `isOutstandingRequest` 避免重复。

#### 4.4.3 源码精读

模式开关本身非常朴素：

[src/DefaultPieceStorage.h:L229-L231](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.h#L229-L231) `isEndGame()` 只是读 `endGame_` 标志（成员见 [L83](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.h#L83)），`enterEndGame()` 置 true——一次性、不可逆。

触发条件（本讲实践要找的判断）：

[src/DefaultBtInteractive.cc:L416-L418](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L416-L418) `addRequests` 开头：`!pieceStorage_->isEndGame() && !pieceStorage_->hasMissingUnusedPiece()` 时调用 `enterEndGame()`。而 [src/DefaultPieceStorage.cc:L380-L384](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L380-L384) `hasMissingUnusedPiece` 的实现就是「能否找到一个缺失且未在用的 piece」。**每个连接独立执行这段判断**，各自进入自己的收官状态。

收官后的领片：

[src/DefaultBtInteractive.cc:L396-L400](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L396-L400) `fillPiece` 在收官分支改用带 `excludedIndexes` 的 `getMissingPiece` 重载，排除本连接已有的目标 piece；该重载（[src/DefaultPieceStorage.cc:L280-L291](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L280-L291)）用临时位图先设对端拥有位、再清排除位。

存储层的收官分支：

[src/DefaultPieceStorage.cc:L195-L230](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L195-L230) `getMissingPiece(pieces, minMissingBlocks, bitfield, length, cuid)` 的 `isEndGame()` 分支用 `getAllMissingIndexes`（**不管在用位**）收集缺失 piece，`std::shuffle` 随机打乱后按缺口领取。其中 [L221-L224](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L221-L224) 对 `getUsedBySegment()` 的 piece 调 `removeUser` 直接跳过——源码注释写明 *"We don't share piece downloaded via HTTP/FTP"*，这是与 HTTP 侧共存的护栏（见 4.5）。

收官版的挑块：

[src/DefaultBtRequestFactory.cc:L197-L239](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtRequestFactory.cc#L197-L239) `createRequestMessagesOnEndGame` 对每个目标 piece：`getAllMissingBlockIndexes`（[src/Piece.cc:L160-L164](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.cc#L160-L164)，同样不看在用位）收齐全部缺失块索引，`std::shuffle` 随机化，再逐个用 `dispatcher_->isOutstandingRequest(index, blockIndex)`（[src/DefaultBtMessageDispatcher.cc:L249-L258](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageDispatcher.cc#L249-L258)，线性扫 `requestSlots_` 按 `(index, blockIndex)` 匹配）过滤掉本连接已在途的块，剩下的才铸成 request。

竞速善后：

[src/DefaultBtMessageDispatcher.cc:L226-L233](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageDispatcher.cc#L226-L233) 每秒巡检时发现块已被别的连接补齐（`hasBlock`）即发 cancel——收官阶段冗余请求的退出通道。

领取时的在用登记仍照常发生：

[src/DefaultPieceStorage.cc:L119-L134](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L119-L134) `checkOutPiece` 置 piece 级在用位、按需新建 `Piece` 对象并 `addUser(cuid)` 记录共享者计数——收官下多个 cuid 会挂到同一 piece 上，最后一个使用者离开时 piece 才真正销毁。

#### 4.4.4 代码实践（本讲指定实践）

1. **实践目标**：用日志确认收官后 request 转向「分散的缺失块」，并定位模式切换的判断条件。
2. **操作步骤**（待本地验证）：
   ```bash
   ./src/aria2c --seed-time=0 --bt-max-peers=8 \
       -l bt.log --log-level=info <torrent 文件>
   # 发送侧 request 日志（INFO）:  "CUID#xx - To: ip:port request index=.., begin=.., length=16384"
   grep " request index=" bt.log > requests.log
   # 观察最后一分钟的 index 分布
   tail -200 requests.log
   ```
   - 下载进度接近 100% 时，注意每个 CUID 请求的 `index` 与 `begin` 的分布；
   - 同时检索收官痕迹：`grep "been acquired" bt.log | tail`。
3. **需要观察的现象**：
   - 前期：单个 CUID 的 request 集中在少数几个 `index`，且 `begin` 顺序递增；
   - 尾声：同一 `index` 出现在**多个不同 CUID** 的请求里（共享 piece），`begin` 值散乱不连续（随机洗牌 + 跳过在途块）；`been acquired` 的槽删除与 `cancel` 发送增多。
4. **预期结果**：能在日志时间轴上划出一条分界线，之前请求「按片顺序、按块顺序」，之后「跨片分散、跨块随机」。
5. **控制切换的判断条件**（源码答案）：`DefaultBtInteractive::addRequests` 中 `!pieceStorage_->isEndGame() && !pieceStorage_->hasMissingUnusedPiece()` → `enterEndGame()`（[src/DefaultBtInteractive.cc:L416-L418](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L416-L418)）；`hasMissingUnusedPiece` 即「找不到任何缺失且未在用的 piece」（[src/DefaultPieceStorage.cc:L380-L384](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L380-L384)）。
6. 本实践依赖真实种子与 peer，具体日志形态**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么收官版挑块要 `shuffle` 随机化，而普通版是顺序取？

**答案**：收官时多个连接竞速同一批缺失块。若各连接都顺序取（都从小块号开始），会一窝蜂请求完全相同的块，冗余最大化；随机化让各连接大概率错开，同一时刻覆盖更多不同缺失块，竞速收益最大。

**练习 2**：`enterEndGame()` 之后还会退出收官模式吗？连接断开重连后会怎样？

**答案**：不会，`endGame_` 只被置 true，没有复位路径。但该标志属于 `PieceStorage`（每个下载任务一份），而进入与否由**每条连接**在 `addRequests` 里自行判断；新连接同样会在条件满足时观察到 `isEndGame()==true`（存储层标志已置）从而直接按收官策略工作。

**练习 3**：收官模式下同一 piece 可能有多条连接在请求，最终由谁写入磁盘？

**答案**：谁先收到 `piece` 消息谁先写——`completeBlock` 置块完成位后，其余连接的对应数据到达时会被 `hasBlock` 识别为冗余（或提前被 cancel 撤销），`Piece` 对象由 `usedPieces_` 统一持有，写入走的是同一个 PieceStorage/DiskAdaptor，不存在双写冲突。

### 4.5 StreamPieceSelector 与两套切片的共存

#### 4.5.1 概念说明

aria2 允许 HTTP(S)/FTP/SFTP 与 BT 混合下载同一个任务（多协议多源），此时 HTTP 连接与 BT 连接**共享同一个 `DefaultPieceStorage`**，但挑片器是两套接口：

- **`PieceSelector`**（BT 用）：默认 `RarestPieceSelector`（最稀有优先），服务「尽快拼齐全量」的目标；
- **`StreamPieceSelector`**（HTTP/FTP 用）：按顺序流式推进，服务「单连接顺序读写、利于边下边写边校验」的目标。

接口注释写得非常直白。两套切片在 piece 完成时汇入同一个 `completePiece` 记账出口（承接 u5-l2 的结论），交叉点的两条护栏是：

1. HTTP 侧经 `SegmentMan::checkoutSegment` 把 piece 包成 `PiecedSegment` 写入游标并置 `usedBySegment` 标志，还会先冲刷该 piece 的写盘缓存（BT 多连接交叠写同片时缓存数据可能重叠）；
2. BT 收官共享 piece 时跳过 `getUsedBySegment()` 的 piece，避免向 HTTP 连接正在顺序写的 piece 里乱序插块。

#### 4.5.2 核心流程

```text
HTTP/FTP 连接（u3-l4 的 SegmentMan 路径）:
  SegmentMan 按需从 PieceStorage 领 piece
    → checkoutSegment(piece): 若有写盘缓存先 flush → setUsedBySegment(true)
    → 包装为 PiecedSegment（写入游标），响应数据沿游标顺序落盘

BT 连接（本讲路径）:
  fillPiece → PieceStorage 领 piece（PieceSelector/RarestPieceSelector 或收官共享）
    → createRequestMessages 按 16KB 块请求
  收官共享时: getUsedBySegment()==true 的 piece 一律不共享
```

#### 4.5.3 源码精读

接口与分工注释：

[src/StreamPieceSelector.h:L44-L64](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/StreamPieceSelector.h#L44-L64) 类注释明说 *"Select piece for HTTP/FTP download. For BitTorrent downloads, use PieceSelector interface."*；`select()` 的 `minSplitSize` 参数即 `-k` 选项对应的最小分段建议（承接 u3-l4）。

顺序实现：

[src/InorderStreamPieceSelector.cc:L47-L53](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InorderStreamPieceSelector.cc#L47-L53) `InorderStreamPieceSelector::select` 直接委托 `BitfieldMan::getInorderMissingUnusedIndex`——按索引升序找第一个「缺失且未在用（且不在忽略位图）」的 piece。

选择器的装配（`--stream-piece-selector` 选项的落点）：

[src/DefaultPieceStorage.cc:L94-L115](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L94-L115) 构造函数里 BT 的 `pieceSelector_` 默认是 `RarestPieceSelector`，而 `streamPieceSelector_` 依据 `PREF_STREAM_PIECE_SELECTOR` 选项在 default/geom、inorder、random 之间选择（对应 u3-l4 讲过的四种流式策略）。两个成员并列在 [src/DefaultPieceStorage.h:L94-L95](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.h#L94-L95)。

HTTP 领片入口（与 BT 的 `getMissingPiece(peer…)` 形成对照）：

[src/DefaultPieceStorage.cc:L386-L399](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L386-L399) 带 `minSplitSize/ignoreBitfield` 的 `getMissingPiece` 重载走 `streamPieceSelector_->select`，这正是 SegmentMan 为 HTTP 连接挑片的路径。

两套切片的交汇点：

[src/SegmentMan.cc:L135-L162](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L135-L162) `checkoutSegment`：若 piece 挂着写盘缓存先 `flushWrDiskCache`（注释说明因 BT peer 介入可能产生重叠数据），随后 [L152](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L152) `piece->setUsedBySegment(true)` 并包装成 `PiecedSegment`（长度为 0 的退化为 `GrowSegment`）。

BT 侧的护栏：

[src/DefaultPieceStorage.cc:L221-L224](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L221-L224) 收官共享领取时，`getUsedBySegment()` 的 piece 只 `removeUser` 归还、不入共享列表——HTTP 连接的顺序写不被 BT 的随机插块打扰。

#### 4.5.4 代码实践

1. **实践目标**：在日志中看到「piece 被包装为 segment」的交接动作。
2. **操作步骤**（待本地验证）：对含 HTTP 种子源（url-list）的任务或混合下载，用 `--log-level=debug` 运行，检索 SegmentMan 的交接日志（`checkoutSegment` 在 [src/SegmentMan.cc:L140](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L140) 打印 `Attach segment#%lu to CUID#...`）：
   ```bash
   grep "Attach segment" bt.log | head
   ```
3. **需要观察的现象**：HTTP 类命令的 CUID 出现 `Attach segment` 行；BT 连接的 CUID 不出现（BT 不经 SegmentMan 建游标）。
4. **预期结果**：两类 CUID 的日志形态截然不同，印证「同一 PieceStorage、两条消费路径」。
5. 依赖具体的混合下载场景，**待本地验证**；亦可阅读 `test/DefaultPieceStorageTest.cc` 中对 `getMissingPiece` 两个重载的用例作旁证。

#### 4.5.5 小练习与答案

**练习 1**：BT 与 HTTP 为什么不能共用一个选片器？

**答案**：目标函数不同。BT 多连接并行，希望先拼「全网最稀有」的 piece（对 swarm 健康）；HTTP 单连接流式读写，希望顺序推进（顺序写盘、顺序校验、利于 pipelining 与缓存）。一个接口难以同时表达两种意图，且 `StreamPieceSelector::select` 还带 `minSplitSize`（`-k`）这类流式专属参数。

**练习 2**：`checkoutSegment` 里为什么要在包装 `PiecedSegment` 前先冲刷写盘缓存？

**答案**：写盘缓存（`--disk-cache`，承接 u5-l6）按全局偏移缓存未满块数据。piece 从 BT 连接转手给 HTTP segment（或反之）意味着写入方变更、写入顺序可能改变，若不先把缓存数据落盘，新写入方与缓存中的旧数据可能对同一区间各写一份，产生交叠损坏。

**练习 3**：若 `--stream-piece-selector=inorder` 且同时开启 BT，BT 的选片策略会因此变成顺序吗？

**答案**：不会。`--stream-piece-selector` 只影响 `streamPieceSelector_`（HTTP/FTP 路径），BT 用的是独立的 `pieceSelector_`（默认 `RarestPieceSelector`，见 [src/DefaultPieceStorage.cc:L94](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L94)），两者互不干扰。

## 5. 综合实践

**任务：为一次完整的 BT 下载绘制「请求流水线四阶段时间轴」。**

1. 准备（待本地验证）：
   ```bash
   ./src/aria2c --seed-time=0 --bt-max-peers=8 --bt-request-timeout=60 \
       -l bt.log --log-level=info <torrent 文件>
   ```
2. 从 `bt.log` 提取三类证据：
   - 发出的 request：`grep " request index=" bt.log`（INFO 级，含 `To: ip:port`）；
   - 收到的 piece：`grep "piece:" bt.log`（接收侧同为 INFO 级消息日志）；
   - 请求槽删除：`grep "Deleting request slot" bt.log`（DEBUG 级，需要时重跑用 `--log-level=debug`）。
3. 按时间轴标注四个阶段并给出证据：
   - **起步**：新 CUID 的 request 逐条出现，在途数小（深度 6 起步）；
   - **扩容**：单 CUID 一轮内发出的 request 数量上台阶（翻倍条件 `（旧−新）×4 ≥ D`）；
   - **稳定**：request 与 piece 节奏匹配，`time out` 稀少；
   - **收官**：多个 CUID 请求相同 `index`、`begin` 分散随机、`been acquired`/cancel 增多。
4. 在源码中抄录并解释两处控制条件：
   - 深度翻倍：[src/DefaultBtInteractive.cc:L341-L348](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L341-L348)；
   - 收官切换：[src/DefaultBtInteractive.cc:L416-L418](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L416-L418) + [src/DefaultPieceStorage.cc:L380-L384](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L380-L384)。
5. 产出：一张时间轴图（阶段 × 证据行号）+ 一份「每个阶段的请求策略一句话总结」。本实践依赖真实网络环境，全部观察结果**待本地验证**。

## 6. 本讲小结

- `DefaultBtRequestFactory` 持有每连接的目标 piece 队列，普通模式按「未完成 ∧ 未在用」**顺序**挑 16KB 块，挑中即置在用位防止重复请求；piece 完成、被 choke、断连三个出口分别对应 `removeCompletedPiece`、`doChokedAction`、`removeAllTargetPiece`。
- 流水线深度由 `BtConstants.h` 的常量界定：初始 `DEFAULT_MAX_OUTSTANDING_REQUEST = 6`，上限 `UB_MAX_OUTSTANDING_REQUEST = 256`；`receiveMessages` 里「单回合消化在途请求 ×4 ≥ 当前深度」即翻倍，`addRequests` 每个交互 tick 按差额补货。
- 超时请求由每秒一次的 `checkRequestSlotAndDoNecessaryThing` 清账：超过 `--bt-request-timeout`（默认 60 秒）的块 `cancelBlock` 并给 peer 打 snubbing 标志；块被其他连接先下回则发 `cancel` 撤销——后者正是收官竞速的退出通道。
- end-game 的触发条件是「找不到任何缺失且未在用的 piece」，此后领 piece 允许**共享**（随机挑选、排除已有目标、跳过 HTTP 正用的 piece），挑块改为**全部缺失块随机洗牌**并过滤本连接在途块；切换不可逆且由各连接自行判断。
- BT 与 HTTP/FTP 共用 `PieceStorage` 但各持选片器：HTTP 走 `StreamPieceSelector`（inorder/geom/random，`--stream-piece-selector` 控制）并经 `SegmentMan::checkoutSegment` 包装 `PiecedSegment`、置 `usedBySegment`、先冲刷写盘缓存；BT 收官共享时明确不碰这些 piece，两套切片由此互不踩脚。

## 7. 下一步学习建议

- **u6-l7（Tracker 交互）**：本讲的「peer 从哪来」前置——`DefaultBtAnnounce` 如何向 tracker 汇报进度并领取新 peer 列表，收官阶段零连接时缩短 announce 间隔的自救逻辑也在那里。
- **u6-l8（扩展协议）**：`ut_pex` 如何在不经过 tracker 的情况下补充 peer，与本讲的流水线扩容配合决定吞吐上限。
- 源码延伸阅读：`test/DefaultBtRequestFactoryTest.cc` 与 `test/DefaultBtInteractiveTest`（如存在，待确认）对 `createRequestMessages` 普通/收官两分支的断言，是验证本讲结论的最快实验台；`RarestPieceSelector.cc` 则是 BT 侧选片策略（本讲 4.5 的对照面）的完整实现。
