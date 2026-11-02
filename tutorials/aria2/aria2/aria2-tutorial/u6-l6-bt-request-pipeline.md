# 请求流水线：DefaultBtRequestFactory 与排队策略

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 aria2 在单个 peer 连接上如何维护一条「请求流水线」：为什么不等一个 block 返回再发下一个请求，而是在途同时挂多个请求。
2. 说出流水线深度 `maxOutstandingRequest_` 如何从默认 6 起步、在什么条件下翻倍、上限为什么是 256（对应 `BtConstants.h` 的两个常量）。
3. 对照源码写出普通模式与 end-game 模式两套请求生成算法的差异：块的选择方式、是否跳过已有在途请求的块、以及 end-game 的触发条件 `!hasMissingUnusedPiece()`。
4. 跟踪请求的「作废—重排」路径：超时的 RequestSlot 如何被清理并归还块、被 choke 时目标 piece 如何清账、完成的 piece 如何退出工厂。
5. 理解 BT 的 piece 选择器（稀有优先）与 HTTP/FTP 的流式 piece 选择器（`StreamPieceSelector`）如何共用同一个 `DefaultPieceStorage` 而互不干扰，以及 `getUsedBySegment` 标志为什么是两套切片之间的「防火墙」。

## 2. 前置知识

本讲建立在 u6-l4（DefaultBtInteractive 交互循环）与 u6-l5（BtMessage 消息分发）之上，先把几个关键概念复习并补全：

- **piece 与 block 的两级切分**：整个下载按 piece 划分（HTTP 默认 1MB，BT 由 .torrent 的 piece length 给定）；每个 piece 内部再切成 16KiB 的 block——这个 16KiB 定义在 `Piece::BLOCK_LENGTH`（[src/Piece.h:75](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.h#L75)）。BT 协议里 `request` 消息请求的是 block，不是整个 piece。
- **request 消息的三元组**：`(index, begin, length)`——piece 编号、block 在 piece 内的字节偏移、block 长度。对端用 `piece` 消息回送同样三元组定位的数据。
- **在途请求（outstanding request）**：已经发出 `request`、但对应 `piece` 数据还没收齐的请求。u6-l5 讲过 `DefaultBtMessageDispatcher` 用 `requestSlots_` 账本登记每个在途请求。本讲要回答的问题是：**谁决定在途请求有多少个、各指向哪些块**。
- **流水线（pipelining）**：BT 不采用「一问一答」。因为一个 block 只有 16KiB，若串行请求，往返时延（RTT）会严重限制单连接吞吐（吞吐上限约为 block 大小 ÷ RTT）。所以客户端同时挂 \(D\) 个在途请求，理想吞吐约为 \(D \times 16\text{KiB} \div \text{RTT}\)。这个 \(D\) 就是流水线深度。
- **choke / snubbing**：对端 choke 我方后，我方不能再发普通 request（只能请求 Allowed Fast 集合中的 piece）；若某请求长时间无响应，该 peer 被标记为 snubbing（「被冷落」），在 choke 算法中失去被解 choke 的资格。
- **end-game（收尾阶段）**：下载临近结束时常出现「所有缺失 piece 都已被某个连接拿走，但个别块迟迟不到」的尾部瓶颈。end-game 模式允许向多个 peer 请求**同一个缺失块**来对冲慢 peer，收到后再向其余 peer 发 cancel。

u6-l4 已经给出交互循环每 tick 的固定顺序：收消息 → choke/interest 决策 → `removeCompletedPiece()` → `addRequests()`。本讲就聚焦最后这两步内部的实现。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/DefaultBtRequestFactory.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtRequestFactory.cc) / [.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtRequestFactory.h) | 本讲主角：每个 peer 一个实例，管理「目标 piece」集合，并把它们翻译成 `BtRequestMessage` 批量产出 |
| [src/BtConstants.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtConstants.h) | BT 协议常量：默认/最大在途请求数、最大 block 长度等 |
| [src/DefaultBtInteractive.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc) | 流水线深度的持有者与决策者：`fillPiece` 补目标 piece、`addRequests` 补请求、`receiveMessages` 里的深度翻倍 |
| [src/DefaultBtMessageDispatcher.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageDispatcher.cc) | 在途请求账本：超时清理 `checkRequestSlotAndDoNecessaryThing`、`isOutstandingRequest` 查询 |
| [src/DefaultBtMessageFactory.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageFactory.cc) | `createRequestMessage`：把 (piece, blockIndex) 换算成线上三元组 |
| [src/Piece.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.cc) / [src/Piece.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.h) | 单个 piece 的 block 级位图：缺失/在用块的查询与登记 |
| [src/DefaultPieceStorage.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc) / [.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.h) | end-game 状态位、BT/流式两套 piece 选择器的装配点、end-game 分支的取件逻辑 |
| [src/StreamPieceSelector.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/StreamPieceSelector.h) 及其四个实现 | HTTP/FTP 流式下载的 piece 选择策略接口（`--stream-piece-selector`） |
| [src/InorderStreamPieceSelector.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InorderStreamPieceSelector.cc) | 顺序选择实现，本讲选作流式侧的精读样本 |
| [src/SegmentMan.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc) | HTTP segment 检出时打 `setUsedBySegment` 标志的位置 |
| [test/DefaultBtRequestFactoryTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DefaultBtRequestFactoryTest.cc) | 工厂的单元测试，本讲多个实践的素材 |

## 4. 核心概念与源码讲解

### 4.1 DefaultBtRequestFactory：目标 piece 容器与生命周期

#### 4.1.1 概念说明

一个 BT 下载会同时连着几十个 peer。对每个 peer，aria2 都挂一套独立的「请求工厂」`DefaultBtRequestFactory`（在 `PeerInteractionCommand` 装配连接时创建，与 dispatcher、receiver 并列注入 `DefaultBtInteractive`，见 u6-l4 的装配流程）。

工厂内部只有一个核心数据结构：`pieces_`——一个 `std::deque<std::shared_ptr<Piece>>`，保存「本 peer 当前负责下载的 piece」。注意这些 `Piece` 对象是从 `DefaultPieceStorage::checkOutPiece` 拿出来的**共享对象**：同一个 piece 可以同时出现在多个 peer 的工厂里，各自认领不同的 block；piece 的 block 位图（完成位 + 在用位）就是协调多个连接不重复要块的依据（位图语义回顾 u3-l4/u5-l2）。

工厂要回答三个问题：

1. **进**：哪些 piece 进入 `pieces_`？（由 `DefaultBtInteractive::fillPiece` 从 PieceStorage 领来后调 `addTargetPiece`）
2. **出**：piece 何时退出？（下载完成、被 choke、被消息层作废、整条连接关闭）
3. **产出**：如何把 piece 翻译成 `request` 消息？（4.2 与 4.4）

#### 4.1.2 核心流程

目标 piece 的进出与三个出口、一个入口：

```
fillPiece() ──addTargetPiece(piece)──▶ pieces_（目标 piece 队列）
                                         │
        ┌────────────────────────────────┼─────────────────────────────┐
        ▼（每 tick）                      ▼（收到 choke）               ▼（连接关闭/强制放弃）
 removeCompletedPiece()             doChokedAction()            removeAllTargetPiece()
  piece 完成 → 通知 dispatcher        peer 不允许的 piece →        逐个作废在途请求 +
  作废该 piece 残留在途请求，          cancelPiece 归还存储，        cancelPiece 归还，清空
  从 pieces_ 移除                     从 pieces_ 移除               pieces_
                                        │
                        removeTargetPiece(piece)（单点放弃，如收到 reject /
                        消息层主动撤销）：作废在途请求 + 归还 + 移除
```

关键点：**「移出工厂」永远伴随「归还存储」与「作废在途请求」两个动作**，否则块会被永久占着（在用位不清）或对端白发的数据无处对账。

#### 4.1.3 源码精读

入口与完成清理。`addTargetPiece` 只是入队；`removeCompletedPiece` 对已完成 piece 先让 dispatcher 作废其残留的在途请求（end-game 时同一 piece 可能还有发往别处的请求），再从队列剔除：

- [src/DefaultBtRequestFactory.cc:65-69](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtRequestFactory.cc#L65-L69)：`addTargetPiece` 把共享 `Piece` 压入 `pieces_`。
- [src/DefaultBtRequestFactory.cc:91-98](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtRequestFactory.cc#L91-L98)：`removeCompletedPiece` 用 `AbortCompletedPieceRequest` 函数对象对每个 `pieceComplete()` 的 piece 调 `doAbortOutstandingRequestAction`，然后 `remove_if` 清队。

被 choke 时的清账。peer choke 我方后，普通 piece 不允许再请求，只剩 Allowed Fast 集合内的 piece 可保留（Fast Extension，见 u6-l3）：

- [src/DefaultBtRequestFactory.cc:148-155](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtRequestFactory.cc#L148-L155)：`doChokedAction` 遍历 `pieces_`，凡是不在 `peer_->isInPeerAllowedIndexSet` 中的 piece 一律 `pieceStorage_->cancelPiece(piece, cuid_)` 归还，并从队列移除。`ProcessChokedPiece` 与 `FindChokedPiece` 两个函数对象（L111-146）分别负责「归还」与「判定」。

单点放弃与整队清空：

- [src/DefaultBtRequestFactory.cc:100-108](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtRequestFactory.cc#L100-L108)：`removeTargetPiece` 移除指定 piece，作废其在途请求并归还存储。
- [src/DefaultBtRequestFactory.cc:157-164](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtRequestFactory.cc#L157-L164)：`removeAllTargetPiece` 在连接收尾（`cancelAllPiece`）时把所有目标 piece 归还后清空。

工厂还提供两个统计口径，供交互循环决策用：

- [src/DefaultBtRequestFactory.cc:258-262](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtRequestFactory.cc#L258-L262)：`countMissingBlock` 累加各目标 piece 还缺的 block 数——这是 4.3 中 `fillPiece` 判断「要不要再领新 piece」的依据。
- [src/DefaultBtRequestFactory.cc:264-271](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtRequestFactory.cc#L264-L271)：`getTargetPieceIndexes` 导出目标 piece 编号——这是 4.4 中 end-game 模式「排除自己已持有的 piece」的依据。

#### 4.1.4 代码实践

**实践目标**：用单元测试验证工厂容器行为的三个断言。

1. **操作步骤**：阅读 [test/DefaultBtRequestFactoryTest.cc:102-130](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DefaultBtRequestFactoryTest.cc#L102-L130) 的 `testAddTargetPiece` 与 `testRemoveCompletedPiece`。前者构造一个 160KiB 的 piece（10 个 block）加一个已完成 1 block 的 144KiB piece，断言 `countMissingBlock()` 为 18（10 + 9 - 1）；后者给 piece1（未完成）与 piece2（`setAllBlock` 置满）入队后调 `removeCompletedPiece`，断言只剩 1 个目标且 index 为 0。若已完成编译，运行 `./test/aria2c`（或 `make check`）确认这两个用例通过。
2. **需要观察的现象**：`countMissingBlock` 的计数包含「已被别人认领但还没下完」的块，它衡量的是缺口而非在途量。
3. **预期结果**：测试通过；并从断言数值反推出 piece 内 block 数按 \(\lceil \text{length}/16\text{KiB} \rceil\) 计算。
4. 若无法本地编译运行，以上结论可作为「源码阅读型实践」直接从断言读出，运行部分**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `removeCompletedPiece` 在移除已完成 piece 之前要调 `doAbortOutstandingRequestAction`？去掉这一步会发生什么？

**答案**：end-game 模式下，同一 piece 的同一 block 可能同时请求了多个 peer。若本连接已把 piece 下完但账本里还挂着发往当前 peer 的在途 RequestSlot，当迟到的 `piece` 消息到达时会按三元组对账失败或重复写盘；作废动作让 dispatcher 把这些槽位清掉（必要时向对端补发 cancel）。去掉后最直接的后果是 `requestSlots_` 里残留脏账，`countOutstandingRequest()` 虚高，流水线补货公式（4.3）会少发甚至不发新请求。

**练习 2**：`doChokedAction` 里为什么用 `cancelPiece` 归还 piece，而不是像 `removeCompletedPiece` 那样只处理在途请求？

**答案**：被 choke 意味着这些 piece 短期内无法从当前 peer 继续下载，占着「在用位」只会让其他 peer 的稀有优先选择（4.5）绕开它们；`cancelPiece` 把 piece 归还存储、清掉本 cuid 的占用，其他连接可立即认领剩余 block。而已完成 piece 本来就要从在用集合毕业（completePiece），只需清残留请求即可。

**练习 3**：多个 peer 的工厂可以同时持有同一个 `Piece` 对象吗？靠什么避免两个 peer 请求同一个 block？

**答案**：可以。`checkOutPiece` 返回共享指针并 `addUser(cuid)`。避免重复靠 piece 内部 block 位图的「在用位」：`getMissingUnusedBlockIndex` 只返回**缺失且未在用**的 block 并当场把在用位置 1（见 4.2），第二个 peer 再来取时会跳过这些块。

### 4.2 普通模式：块级请求的批量生成与 begin 换算

#### 4.2.1 概念说明

普通模式下，请求生成遵循一条朴素的贪心规则：**在自己的目标 piece 里按入队顺序取前 N 个「缺失且未在用」的 block，一次性生成 N 条 request 消息**。N 由交互循环按流水线深度算好传入（见 4.3）。

「未在用（unused）」是关键定语：block 位图有两位——完成位（数据已落袋）与在用位（已被某个在途请求占用）。只查缺失会重复请求，只查在用会重发已完成块，两者都要过滤。

#### 4.2.2 核心流程

```
createRequestMessages(max, endGame=false):
  getnum = max                        # 还需生成的请求数
  for piece in pieces_:               # 按入队顺序
    piece.getMissingUnusedBlockIndex(blockIndexes, getnum)
    #  ↑ 返回 ≤ getnum 个「缺失且未在用」的块号，并把它们在用位置 1
    for bi in blockIndexes:
      生成 BtRequestMessage(piece, bi)
    getnum -= 返回数
    直到 getnum == 0 或 piece 用尽
```

注意两个细节：

- 取块时**当场把在用位置 1**，所以同一轮内不会重复，下一轮也会跳过这些块，直到 `piece` 数据到达（`completeBlock` 清在用位）或超时（`cancelBlock` 清在用位）。
- 一个 piece 的块取不完会**溢出到下一个 piece**：请求先集中打满前面的 piece，再自然延伸到后面的 piece。这正是 testCreateRequestMessages 断言的形态（piece0 的 block0、block1 + piece1 的 block0）。

#### 4.2.3 源码精读

- [src/DefaultBtRequestFactory.cc:166-195](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtRequestFactory.cc#L166-L195)：`createRequestMessages` 主体。`endGame` 为真直接转到 4.4 的专版（L169-171）；否则按上述流程循环，L183-188 对每个生成的块打 DEBUG 日志（`Creating RequestMessage index=... begin=... blockIndex=...`，本讲实践的观察点之一）。
- [src/Piece.cc:141-153](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.cc#L141-L153)：`getMissingUnusedBlockIndex(indexes, n)` 委托给 piece 内部位图的 `getFirstNMissingUnusedIndex` 取前 n 个可用块，然后**循环把在用位置 1**——「查询即登记」的原子语义。
- [src/DefaultBtMessageFactory.cc:272-280](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageFactory.cc#L272-L280)：`createRequestMessage` 完成块号到线上三元组的换算：

\[
\text{begin} = \text{blockIndex} \times \text{blockLength}, \qquad
\text{length} = \text{piece->getBlockLength(blockIndex)}
\]

  其中 `getBlockLength(blockIndex)` 处理了 piece 尾块不足 16KiB 的情况，保证 `begin + length` 不越过 piece 边界。协议上限侧，`BtConstants.h` 的 `MAX_BLOCK_LENGTH = 64_k`（[src/BtConstants.h:49](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtConstants.h#L49)）约束的是**收到对端 request 时的合法性检查**（[src/bittorrent_helper.cc:785-794](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L785-L794)），我方发出的块恒为 16KiB 或尾块，天然满足。

#### 4.2.4 代码实践

**实践目标**：手工执行一次普通模式的请求生成，验证「先打满前一个 piece 再溢出」的规律。

1. **操作步骤**：阅读 [test/DefaultBtRequestFactoryTest.cc:132-157](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DefaultBtRequestFactoryTest.cc#L132-L157)。两个 32KiB 的 piece（各 2 个 block）入队，第一次 `createRequestMessages(3, false)` 断言产出 3 条：piece0/block0、piece0/block1、piece1/block0；**紧接着第二次 `createRequestMessages(3, false)` 只产出 1 条**（piece1/block1）。
2. **需要观察的现象**：第二次调用为什么只剩 1 条——因为第一次调用已把 3 个块的在用位置 1，第二次只剩 piece1 的 block1 可用。
3. **预期结果**：从断言数值验证「查询即登记」语义：两次调用合计 4 条 = 两个 piece 的全部块，无重复。
4. 运行 `./test/aria2c` 观察用例通过；无法编译则按源码推演，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：若某 peer 的流水线深度是 6，而它只有 1 个目标 piece（256KiB，16 个块），一次 `createRequestMessages(6, false)` 会请求哪些块？

**答案**：该 piece 的 block0～block5（`getFirstNMissingUnusedIndex` 取前 6 个），begin 分别为 0、16384、32768、…、81920，length 均为 16384。

**练习 2**：普通模式生成的请求会不会请求「其他 peer 已在下载的块」？为什么？

**答案**：不会。块级在用位是 `Piece` 对象全局共享的，任何 peer 的工厂取块都会置位，其他 peer 取块时自然跳过。这正是普通模式与 end-game 模式的本质区别——end-game 刻意打破这条规则（见 4.4）。

### 4.3 流水线深度：BtConstants 常量与在途请求的动态调整

#### 4.3.1 概念说明

流水线深度 \(D\)（源码中是 `DefaultBtInteractive::maxOutstandingRequest_`）决定「每个 peer 同时挂多少个在途请求」。它的两个锚点定义在 `BtConstants.h`：

- `DEFAULT_MAX_OUTSTANDING_REQUEST = 6`：起步深度（[src/BtConstants.h:51](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtConstants.h#L51)）。
- `UB_MAX_OUTSTANDING_REQUEST = 256`：深度上限（[src/BtConstants.h:54](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtConstants.h#L54)）。

为什么需要动态调整？深度太小喂不饱高带宽peer（吞吐受 RTT 限制），太大则既占内存又会在 peer 变慢/断线时积压大量作废请求。aria2 的策略是**按吞吐表现自适应**：这个 peer 每轮能消化多少请求，就给它配多少深度——起步 6，用得好翻倍，封顶 256。深度以「块数」为单位，256 块 × 16KiB = 4MiB 在途数据上限。

#### 4.3.2 核心流程

每个交互 tick（u6-l4 的 `doInteractionProcessing`）中与深度相关的完整因果链：

```
1. removeCompletedPiece()             # 完成的 piece 退出工厂
2. addRequests():
   a. 若 !isEndGame() 且 !hasMissingUnusedPiece() → enterEndGame()   # 4.4
   b. fillPiece(maxOutstandingRequest_):
        若 目标 piece 缺块总数 < D:
            从 PieceStorage 领新 piece（ choked 且支持 Fast 时领 fast piece；
            end-game 时排除自己已有的 piece），直到缺块总数补到 ≈ D
   c. reqNumToCreate = max(0, D − dispatcher 在途请求数 O)
   d. requests = createRequestMessages(reqNumToCreate, isEndGame())
   e. 逐条 addMessageToQueue（dispatcher 记账并入发送队列）
3. receiveMessages() 末尾的深度翻倍：
   记本轮收前在途数 O_old、收后 O_new，若
       !isEndGame() 且 O_old > O_new 且 (O_old − O_new) × 4 ≥ D
   则 D = min(256, D × 2)
```

补货公式 \( \text{reqNumToCreate} = \max(0,\, D - O) \) 是一条**恒定水位线**：`piece` 消息每核销一个在途请求，\(O\) 下降，下一 tick 立即补一条新请求，使在途数始终贴着 \(D\) 运行。

翻倍条件 \((O_{old} - O_{new}) \times 4 \ge D\) 的直觉：一轮之内核销掉了至少四分之一个深度窗口的请求，说明这条管道的「消耗速度」对当前深度游刃有余，值得加倍试探；反之核销慢的 peer 深度停留在低位，不浪费。注意翻倍在 end-game 后被冻结（`!isEndGame()` 前置），因为收尾阶段策略不同，盲目加大深度没有意义。

与深度并列的另一个节律是**超时清理**：每秒一次（`perSecTimer_` 闸门）调 dispatcher 的 `checkRequestSlotAndDoNecessaryThing`，扫描在途账本。两类槽位被清除：

- 超时槽（超过 `--bt-request-timeout` 秒无响应，默认 60、范围 1–600 的隐藏选项，经 [src/PeerInteractionCommand.cc:182-183](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L182-L183) 注入，定义见 [src/OptionHandlerFactory.cc:1633-1638](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L1633-L1638)）：清块的在用位、标记 peer 为 snubbing、删槽。**被清的块立即回到「缺失且未在用」状态**，下一次 `addRequests` 会重新生成对它的请求（可能发往别的块区间甚至别的 piece）——这就是「超时请求的重排」：不是把旧请求移回队首，而是作废后按当前策略重新分配。
- 已到手槽：块已由别的途径获得（end-game 多发命中），删槽并向对端补发 cancel，避免浪费对端上行。

#### 4.3.3 源码精读

- [src/DefaultBtInteractive.cc:106](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L106)：构造函数把 `maxOutstandingRequest_` 初始化为 `DEFAULT_MAX_OUTSTANDING_REQUEST`（6）。
- [src/DefaultBtInteractive.cc:414-432](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L414-L432)：`addRequests` 全貌。L416-418 是 end-game 触发判定；L419 领 piece；L420-423 计算补货数；L425-431 生成并入队。
- [src/DefaultBtInteractive.cc:372-412](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L372-L412)：`fillPiece`。L375-379 用 `countMissingBlock()` 判断目标 piece 的缺块总数是否已达 `maxMissingBlock`（即深度 \(D\)）；L381-394 处理被 choke 且支持 Fast Extension 的情形（只领 Allowed Fast 集合内的 piece）；L396-405 区分 end-game 与普通模式取件（end-game 版本多传 `getTargetPieceIndexes()` 作为排除集，见 4.4）。
- [src/DefaultBtInteractive.cc:301-350](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L301-L350)：`receiveMessages`。L303 先记 \(O_{old}\)，L341-348 在收消息循环结束后执行翻倍判定：`countOldOutstandingRequest > countOutstandingRequest` 且差值×4 ≥ 当前深度时 `maxOutstandingRequest_ = min(UB_MAX_OUTSTANDING_REQUEST, maxOutstandingRequest_ * 2)`。
- [src/DefaultBtInteractive.cc:564-580](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L564-L580)：非 metadata 模式的交互循环体。L566-569 每秒一次调 `checkRequestSlotAndDoNecessaryThing`；L576 `removeCompletedPiece`；L577-579 未完成则 `addRequests`——把本讲所有机制串到 u6-l4 的循环骨架上。
- [src/DefaultBtMessageDispatcher.cc:215-242](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageDispatcher.cc#L215-L242)：超时清理。超时分支（L218-225）调 `slot->getPiece()->cancelBlock(...)` 归还在用位并 `peer_->snubbing(true)`；已到手分支（L226-233）补发 cancel；L235-241 统一删除两类槽位。snubbing 的后果在 choke 算法中兑现：[src/BtLeecherStateChoke.cc:220-227](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtLeecherStateChoke.cc#L220-L227) 中被 snubbing 的 peer 直接失去常规解 choke 资格——超时不仅作废请求，还降级 peer。
- [src/Piece.cc:110-113](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.cc#L110-L113)：`cancelBlock` 只清在用位，块回到可请求状态。

#### 4.3.4 代码实践

**实践目标**：通过日志观察深度翻倍与超时重排。

1. **操作步骤**：找一个速度较快的公开种子，运行
   `./src/aria2c -d /tmp/bt --enable-dht=true -l bt.log --log-level=debug "<magnet 或 torrent>"`。
2. **需要观察的现象**：在日志中按同一 CUID 过滤 `Creating RequestMessage` 行（4.2 源码精读中引用的 DEBUG 日志），统计相邻两批之间单批请求数量的变化：起步约 6 条/批，速度上来后应看到 12、24、…的台阶式增长；若中途某 peer 卡住，60 秒后会出现 `Deleting request slot`（超时）日志并伴随同一块稍后被重新请求。
3. **预期结果**：单批请求量的台阶与翻倍公式吻合，且不超过 256。
4. 依赖真实网络与种子的表现，具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：当前深度 \(D=6\)，一轮交互中在途数从 10 降到 6，会触发翻倍吗？

**答案**：会。条件是 \((O_{old}-O_{new})\times 4 \ge D\)：本轮核销量 \(O_{old}-O_{new}=4\)，\(4\times4=16 \ge 6\) 成立，深度翻倍到 12。换一组数验证边界：若本轮只核销 1 个（\(1\times4=4 < 6\)）则不翻倍。这道题的关键是意识到判定阈值随 \(D\) 增大而变苛刻：深度越大，需要越猛的单轮核销量才能继续翻倍，形成自然的收敛。

**练习 2**：深度上限 256 的现实意义是什么？为什么不定成 1 万？

**答案**：在途请求是要付出代价的——每个在途槽占一条 `RequestSlot` 记账、对端要为pending请求准备上行缓冲；而 256 块 × 16KiB = 4MiB 在途窗口已足以打满绝大多数链路（例如 RTT 200ms 时理论吞吐 20MiB/s）。更大的深度只会放大 peer 断线/choke 时的作废风暴。

**练习 3**：超时清理把块的在用位清掉之后，这个块的下一条请求一定还发往同一个 peer 吗？

**答案**：不一定。清位后该块回到「缺失且未在用」池，下一次 `addRequests` 由**本 peer 的工厂**重新生成请求（普通模式下通常还是同一 peer 先取到它，但 end-game 模式下会随机分散到多个 peer）。注意超时判定只作废请求并 snubbing 该 peer，并不会主动断开连接。

### 4.4 end-game：收尾阶段的策略切换

#### 4.4.1 概念说明

下载到尾声时会出现一个结构性问题：所有缺失 piece 都已被各连接认领（全局在用），`fillPiece` 领不到新 piece，新连接更是无事可做。若某个块恰好被一个慢 peer 拿着，整个下载就被这一个块拖住——「最后一块瓶颈」。

end-game 模式的解法是**放弃「一块只发给一个 peer」的排他规则**：允许把同一个缺失块同时发给多个 peer，谁先回就采纳谁，其余命中后用 cancel 通知作废。代价是多余的上行请求与重复数据，因此只在收尾阶段启用。

还有一个容易误读的点：`DefaultPieceStorage.h` 里保留了常量 `END_GAME_PIECE_NUM = 20` 与 `setEndGamePieceNum`（[src/DefaultPieceStorage.h:56](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.h#L56)，接口处甚至标注 "TODO We can remove this"，[src/PieceStorage.h:222-223](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PieceStorage.h#L222-L223)）。但**当前代码并不按「剩余 piece 数」切换 end-game**——真正的触发条件是 `addRequests` 开头的「全局已无缺失且未在用的 piece」判定。读源码时应以调用点为准，不要被遗留常量误导。

#### 4.4.2 核心流程

```
触发（每 tick 检查一次）：
  !isEndGame() && !hasMissingUnusedPiece()   # 全局没有「缺失且未被认领」的 piece
      → pieceStorage_->enterEndGame()        # 置 endGame_ = true（不可逆，本任务内不再退出）

end-game 下的取件（fillPiece）：
  领 piece 时排除自己已有的目标 piece（getTargetPieceIndexes 作排除集）
    → 于是会领到「别的连接正在下载」的 piece，进入共享下载状态

end-game 下的取块（createRequestMessagesOnEndGame）：
  对每个目标 piece：
    取「全部缺失块」（不管在不在用——别的 peer 在用的块正是要抢的）
    洗牌（shuffle）
    逐个检查：本连接的 dispatcher 账本里是否已有该块的在途请求？
      无 → 生成请求；有 → 跳过（自己不重复请求同一块）
    直到凑满 max 条
```

与普通模式的三点对照：

| 维度 | 普通模式 | end-game 模式 |
| --- | --- | --- |
| 候选块 | 缺失**且未在用**（排他） | **全部缺失块**（允许跨连接共享） |
| 选择顺序 | piece 内顺序取前 N | 全部缺失块**随机洗牌** |
| 去重范围 | 全局在用位天然去重 | 仅对本连接的 `isOutstandingRequest` 去重 |

洗牌的意义：多个 peer 若都按固定顺序抢块，会同时命中同一批块造成最大化重复；随机化让各连接的请求天然错开。

#### 4.4.3 源码精读

- 触发条件：[src/DefaultBtInteractive.cc:416-418](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L416-L418)——`if (!pieceStorage_->isEndGame() && !pieceStorage_->hasMissingUnusedPiece()) { pieceStorage_->enterEndGame(); }`。`hasMissingUnusedPiece` 的实现只是找第一个「缺失且未在用」的全局 piece（[src/DefaultPieceStorage.cc:380-384](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L380-L384)）；`enterEndGame` 置位 `endGame_`（[src/DefaultPieceStorage.h:229-231](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.h#L229-L231)）。
- end-game 取件带排除集：[src/DefaultBtInteractive.cc:396-405](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L396-L405) 调用多出的 `excludedIndexes` 重载，其实现把排除集从 peer 位图中抹掉（[src/DefaultPieceStorage.cc:280-291](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L280-L291)），于是 `fillPiece` 会领回「自己还没有、别人正在下」的 piece。
- PieceStorage 的 end-game 分支：[src/DefaultPieceStorage.cc:195-249](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L195-L249)。end-game 时（L203-230）用 `getAllMissingIndexes` 收集全部缺失 piece、**洗牌**后逐个 `checkOutPiece`；普通时（L231-248）走 4.5 的稀有优先选择器取「缺失且未在用」的 piece。特别注意 L221-224：检出的 piece 若带 `getUsedBySegment()` 标志（正被 HTTP/FTP segment 持有）则**立即归还、不共享**——注释直言 "We don't share piece downloaded via HTTP/FTP"，这是两套切片之间的隔离墙（4.5）。
- end-game 取块专版：[src/DefaultBtRequestFactory.cc:197-239](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtRequestFactory.cc#L197-L239)。L204-219 用 `getAllMissingBlockIndexes` 把 piece 的全部缺失块展开成编号表（位运算逐位扫描）；L220-221 `std::shuffle` 随机化；L226 逐块查 `dispatcher_->isOutstandingRequest`，只对本连接没有在途请求的块生成消息（其实现见 [src/DefaultBtMessageDispatcher.cc:249-256](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageDispatcher.cc#L249-L256)，按 (index, blockIndex) 精确匹配）。
- 重复命中的善后：4.3 引用的 [src/DefaultBtMessageDispatcher.cc:226-233](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageDispatcher.cc#L226-L233)——每秒扫描时发现块已被别的连接送达，删槽并补发 cancel。

#### 4.4.4 代码实践

**实践目标**：用单元测试读懂 end-game 取块的去重语义。

1. **操作步骤**：阅读 [test/DefaultBtRequestFactoryTest.cc:159-184](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DefaultBtRequestFactoryTest.cc#L159-L184) 的 `testCreateRequestMessages_onEndGame`。两个 32KiB piece（共 4 个块）全部入队，`createRequestMessages(3, true)` 后排序断言产出为 piece0/block1、piece1/block0、piece1/block1——恰好缺 piece0/block0。原因藏在测试文件内的 mock：[test/DefaultBtRequestFactoryTest.cc:66-73](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DefaultBtRequestFactoryTest.cc#L66-L73) 的 `MockBtMessageDispatcher2::isOutstandingRequest` 硬编码返回 `index == 0 && blockIndex == 0`，模拟「该块已在本连接在途」。
2. **需要观察的现象**：即使 `max=3` 小于缺失块总数 4，被 mock 标记为在途的块也永远不会出现在产出里。
3. **预期结果**：断言全部通过；你能向别人解释「为什么缺的恰好是 block(0,0)」。
4. 运行 `./test/aria2c` 验证；无法编译则按源码推演，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：end-game 触发后，如果又有新连接加入（比如 DHT 送来一个新 peer），这个新连接能立刻参与收尾吗？

**答案**：能。新连接的 `fillPiece` 走 end-game 分支，领 piece 时只排除**自己**的目标 piece（起初为空），因此会领到别人正在下载的缺失 piece 并对其全部缺失块发起请求——这正是 end-game 想要的「多连接抢同一块」。

**练习 2**：`enterEndGame()` 有对应的「退出」函数吗？为什么？

**答案**：没有。`endGame_` 只被置 true（[src/DefaultPieceStorage.h:229-231](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.h#L229-L231)）。因为触发条件「无缺失且未在用的 piece」在收尾阶段是单调的：piece 只会陆续完成，缺口只减不增（除非校验失败重下，那会走另一条 PieceStorage 生命周期）。深度翻倍也在 end-game 后冻结（4.3），整个子系统进入「直线冲刺」状态。

**练习 3**：为什么 end-game 取块要检查 `isOutstandingRequest`，而普通模式不需要？

**答案**：普通模式的在用位是全局共享的排他锁，取块时已排除一切在途块；end-game 有意绕过在用位去抢「别人在用的块」，只剩本连接的 dispatcher 账本还能防止**自己**重复请求同一块。两层去重的边界不同：全局排他 vs. 单连接去重。

### 4.5 StreamPieceSelector：BT 稀有优先与 HTTP 流式选择的共存

#### 4.5.1 概念说明

`DefaultPieceStorage` 同时服务两类消费者：BT 的请求工厂（本讲前四节）与 HTTP/FTP 的 `SegmentMan`（u3-l4）。两类消费者对「下一个下哪个 piece」的诉求完全不同，于是存储里并存两个选择器：

- **`pieceSelector_`（`PieceSelector` 接口）——BT 专用**，默认装配 `RarestPieceSelector`（稀有优先）：优先下载全网持有者最少的 piece，这是 BT 世界保住「 swarm 存活」的核心策略。
- **`streamPieceSelector_`（`StreamPieceSelector` 接口）——HTTP/FTP 专用**，由 `--stream-piece-selector` 选项选择四种实现（default/inorder/random/geom），追求的是减少连接建立、支持顺序播放等流式目标。接口注释写得很清楚：「Select piece for HTTP/FTP download. For BitTorrent downloads, use PieceSelector interface.」（[src/StreamPieceSelector.h:44-46](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/StreamPieceSelector.h#L44-L46)）。

当一次任务同时走 HTTP 与 BT（比如 .torrent 里的 WebSeed，或 --follow-* 派生的混合任务），两套切片会在同一个存储上并发选 piece。aria2 用两个机制保证互不踩踏：**接口分离**（两个虚接口、两条调用路径）与 **`getUsedBySegment` 标志**（HTTP segment 检出的 piece 打标，BT 的 end-game 共享逻辑遇到该标志直接让开，见 4.4 引用的 DefaultPieceStorage.cc L221-224）。

#### 4.5.2 核心流程

两条取件路径在 `DefaultPieceStorage` 内的分岔：

```
BT：DefaultBtInteractive::fillPiece
      → getMissingPiece(pieces, minBlocks, peer[, excluded], cuid)     # L195-249
          普通：getAllMissingUnusedIndexes → pieceSelector_->select(index, bitfield)   # 稀有优先
          end-game：getAllMissingIndexes → shuffle → checkOutPiece（跳过 segment 占用）

HTTP/FTP：SegmentMan::getSegment
      → getMissingPiece(minSplitSize, ignoreBitfield, length, cuid)     # L386-399
          → streamPieceSelector_->select(index, minSplitSize, ignoreBitfield)
              default → getSparseMissingUnusedIndex   # 稀疏：挑最长缺失区间的中部
              inorder → getInorderMissingUnusedIndex  # 顺序：从前往后
              random  → 随机
              geom    → 几何分布
      → SegmentMan::checkoutSegment → piece->setUsedBySegment(true)     # 打隔离标志
```

注意 BT 侧选择以「piece」为粒度（bitfieldMan_ 的「块」就是 piece），HTTP 侧的 `minSplitSize`（`-k` 选项）只是给选择器的**建议值**：别选出离已有 segment 太近的 piece（保证新段有足够的连续空间，回顾 u3-l4）。

#### 4.5.3 源码精读

- 装配点：[src/DefaultPieceStorage.cc:78-115](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L78-L115)。构造函数 L94 固定把 BT 选择器设为 `RarestPieceSelector`（以 `PieceStatMan` 的统计为依据）；L97-114 按 `--stream-piece-selector` 选项装配流式选择器，default → `DefaultStreamPieceSelector`、inorder → `InorderStreamPieceSelector`、random、geom（参数 1.5）。
- 稀有优先的实现：[src/RarestPieceSelector.cc:51-72](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RarestPieceSelector.cc#L51-L72)。`pieceStatMan_->getOrder()` 给出按稀缺度排序的 piece 次序，`select` 在候选位图里挑出**计数最小**（持有 peer 最少）的 piece。
- 流式接口契约：[src/StreamPieceSelector.h:46-64](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/StreamPieceSelector.h#L46-L64)。`select(index, minSplitSize, ignoreBitfield, length)` 与 `onBitfieldInit()`（初始位图确定后的优化钩子）。
- 两个精读样本：`DefaultStreamPieceSelector::select` 委托 `getSparseMissingUnusedIndex`（[src/DefaultStreamPieceSelector.cc:47-53](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultStreamPieceSelector.cc#L47-L53)）；`InorderStreamPieceSelector::select` 委托 `getInorderMissingUnusedIndex`（[src/InorderStreamPieceSelector.cc:47-53](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InorderStreamPieceSelector.cc#L47-L53)）。两者都落在 BitfieldMan 的「missing+unused」查询上，与 BT 侧共享同一套位图语义。
- HTTP 侧入口：[src/DefaultPieceStorage.cc:386-399](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L386-L399)，`streamPieceSelector_->select` 命中后 `checkOutPiece`——与 BT 取件走**同一个**检出函数，piece 对象、写盘缓存、block 位图全部复用。
- 隔离标志的两侧：HTTP 检出时 [src/SegmentMan.cc:152](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L152) 打 `setUsedBySegment(true)`，segment 归还时（L303）清除；BT 的 end-game 分支 [src/DefaultPieceStorage.cc:221-224](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L221-L224) 遇到该标志立即 `removeUser` 归还，绝不把 HTTP 正在写的 piece 拿去多源抢块——否则同一块可能被 HTTP 连接和多个 BT peer 同时写，写盘交叠无法协调。

#### 4.5.4 代码实践

**实践目标**：对比同一存储上两种选择策略的差异。

1. **操作步骤**：用同一个大文件的 HTTP 下载跑两次，分别加 `--stream-piece-selector=default` 与 `--stream-piece-selector=inorder`，各开 `-l log --log-level=debug`；在日志中检索 `Attach segment#` 行（[src/SegmentMan.cc:140-141](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L140-L141) 的 DEBUG 输出），记录两次运行各 segment 的编号序列。
2. **需要观察的现象**：inorder 的 segment 编号应基本单调递增；default（稀疏策略）的编号会出现「挑最大空洞中部」的跳跃分布。再对照 `--enable-rpc=false` 无关紧要，重点是两序列形状差异。
3. **预期结果**：能画出两条序列并指出稀疏策略如何把多连接自动散开到文件不同区域（呼应 u3-l4 的最长缺失区间策略）。
4. 需要真实网络下载，**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：为什么 BT 默认用「稀有优先」而 HTTP 默认用「稀疏散开」？两者的优化目标差在哪里？

**答案**：BT 的稀缺 piece 一旦某个 peer 掉线就可能从 swarm 消失，优先下载稀有 piece 是在为整个网络的副本冗余投保（集体利益）；HTTP 的镜像源内容全同、无稀缺性问题，稀疏散开是为了让多条连接各自拿到互不重叠的大块区间，减少连接建立与区间争用（个体吞吐）。

**练习 2**：一个 piece 正被 HTTP segment 下载到一半，BT 的请求工厂（普通模式）还会领它吗？

**答案**：普通模式下不会。普通模式只从「缺失且未在用」的 piece 里选，而 `checkOutPiece` 的 `setUseBit` 使该 piece 处于在用状态（全局位图层面已排他）；即便走到 end-game 分支，`getUsedBySegment` 检查也会显式让开（DefaultPieceStorage.cc L221-224）。双重保险确保 HTTP 与 BT 不对同一 piece 并发抢块。

**练习 3**：`fillPiece` 领 piece 时传入的第三个参数 `minMissingBlocks`（= 深度 \(D\)）控制的是什么？传 1 会怎样？

**答案**：它控制「领到目标 piece 的缺块总数达到约 \(D\) 为止」——即工厂的目标 piece 集合大小与深度联动：深度翻倍时领件量也随之增加，保证有足够的块可请求。传 1 则每 tick 只领一个 piece 的一个块就停，流水线将长期「无货可补」，深度再大也用不满。

## 5. 综合实践

**任务：用日志完整复盘一次 end-game 的发生与收尾**（本讲实践主任务）。

1. **准备**：选一个总大小 50～200MB、piece 数较多（几百个以上）的种子，确保下载能在几分钟内完成：

   ```bash
   ./src/aria2c -d /tmp/bt -l bt.log --log-level=debug \
       --bt-request-timeout=30 "<torrent 文件或 magnet>"
   ```

   （把 `--bt-request-timeout` 从默认 60 调小到 30，可以更容易在收尾前观察到超时重排。）

2. **观察一：end-game 切换点**。日志里没有专门的 "end-game" 输出行，需要用间接证据定位切换点：对比同一 CUID 的 `Creating RequestMessage` 日志，切换前 blockIndex 通常规整（顺序、集中在一个 piece），切换后 begin/blockIndex 变得离散（跨多个 piece、随机）。把下载最后 2 分钟的 request 行按 CUID 分组画成时间线。
3. **观察二：请求是否转向「分散的缺失块」**。在时间线里找出同一 `(index, blockIndex)` 出现在**两个不同 CUID** 中的行——这就是 end-game 多源抢块的直接证据；随后在其中一方收到 piece 数据后，查找发往另一方的 cancel 迹象（每秒清理逻辑，DefaultBtMessageDispatcher.cc L226-233）。
4. **观察三：定位切换条件**。在源码中抄下控制切换的判断：[src/DefaultBtInteractive.cc:416-418](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L416-L418) 的 `!pieceStorage_->isEndGame() && !pieceStorage_->hasMissingUnusedPiece()`，并顺着 `hasMissingUnusedPiece` → `getFirstMissingUnusedIndex`（[src/DefaultPieceStorage.cc:380-384](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L380-L384)）读到底层位图查询。写一段 100 字以内的说明：为什么这个条件在收尾阶段必然成立。
5. **加分项**：对照 4.3 的翻倍条件，统计切换前最后一批 request 的数量，验证进入 end-game 后深度不再翻倍（后续批次数量不再台阶式增长）。
6. **预期产出**：一张请求时间线（可手工绘制 CUID × 时间的表格）+ 切换条件的源码引用链 + 一段结论。若手头没有可用种子，可改用本机两个 aria2 实例互为 peer 做种/下载来完成同样的观察；无法运行时全部标记**待本地验证**并完成纯源码推演。

## 6. 本讲小结

- 每个 peer 拥有独立的 `DefaultBtRequestFactory`，其核心是目标 piece 队列 `pieces_`；piece 是跨连接共享的对象，「移出工厂」永远伴随「作废在途请求 + 归还 PieceStorage」。
- 普通模式按「piece 入队顺序 × 块的缺失且未在用位」批量生成 request，取块即置在用位（查询即登记）；`begin = blockIndex × 16KiB`，尾块按 `getBlockLength(blockIndex)` 截短。
- 流水线深度从 `DEFAULT_MAX_OUTSTANDING_REQUEST=6` 起步，单轮核销量 ≥ 深度¼ 时翻倍，封顶 `UB_MAX_OUTSTANDING_REQUEST=256`；深度同时是 `fillPiece` 的目标缺块数，补货公式为 \(D - O\)。
- 超时请求的处理是「作废重排」而非排队重发：清在用位 + snubbing + 删槽，块回到可用池后按当前策略重新分配；已到手的重复块由每秒清理逻辑补发 cancel。
- end-game 由「无缺失且未在用的 piece」触发（不是剩余 piece 数阈值）：取件排除自己的目标 piece、取块改为全部缺失块随机洗牌、仅对本连接在途去重，实现多源抢块对冲慢 peer。
- BT 的稀有优先（`pieceSelector_`）与 HTTP/FTP 的流式选择器（`streamPieceSelector_`，`--stream-piece-selector` 四选一）并存于 `DefaultPieceStorage`；`setUsedBySegment` 标志保证 end-game 绝不共享 HTTP 正在下载的 piece。

## 7. 下一步学习建议

本讲补全了 BT 数据面「请求侧」的最后一块拼图，接下来建议：

1. **u6-l7（Tracker 交互）**：peer 列表从哪来——`DefaultBtAnnounce` 如何按 AnnounceList 轮询 tracker、`TrackerWatcherCommand` 如何守护 announce，与 HTTP/UDP 两种 tracker 协议的差异。
2. **u6-l8（扩展协议）**：ut_pex 如何在不经过 tracker 的情况下给本讲的请求工厂持续供应新 peer。
3. **回读 u3-l4 与 u5-l2**：现在再读 SegmentMan 的 RequestSlot 与 PieceStorage 的位图实现，会有「两套账本对同一份位图」的新视角；也可以对照 `test/DefaultBtInteractiveTest.cc` 中对 `addRequests`/`fillPiece` 的测试加深理解。
