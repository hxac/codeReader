# PieceStorage：piece 级别的数据管理

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 piece、bitfield、DiskWriter 三者在存储层的组合关系：谁记账、谁存数据、谁落地。
2. 完整跟踪 `getMissingPiece`（领取 piece）与 `completePiece`（归还 piece）两条调用链，既包括 HTTP/FTP 分段下载路径，也包括 BitTorrent 路径。
3. 理解 `DefaultPieceStorage` 与 `UnknownLengthPieceStorage` 的分工：为什么「长度未知」的下载需要一套完全退化的实现。
4. 解释 `--select-file` 如何通过 bitfield 的第三张「过滤位图」改变 piece 的选择范围。

本讲承接 u3-l4（SegmentMan 与分段调度）。u3-l4 从 SegmentMan 的视角讲了「段怎么切、怎么记」，本讲下潜一层，看 SegmentMan 背后的 `PieceStorage` 如何以 piece 为单位管理全局下载状态。

## 2. 前置知识

- **piece（块）**：aria2 把整个下载任务的总字节流按固定长度切成 piece。BT 下载的 piece 长度由 .torrent 文件指定；HTTP/FTP 下载默认 1M（`--min-split-size` 相关行为见 u3-l4）。总 piece 数为 \(\lceil \text{totalLength} / \text{pieceLength} \rceil\)。
- **block（子块）**：piece 内部再按 16KB（`Piece::BLOCK_LENGTH`）切成 block。block 是 BT 请求消息和写盘记账的最小粒度；一个 1MB 的 piece 含 64 个 block。
- **bitfield（位图）**：用一个 bit 表示一个 piece（或 block）的状态。\(N\) 个块只需 \(\lceil N/8 \rceil\) 字节，10 万个 piece 也只要约 12.5KB，这是 BT 协议中 peer 之间交换「我有哪些 piece」的标准编码。
- **Segment 与 Piece 的关系（u3-l4 回顾）**：Segment 是 piece 面向单条连接的「写入游标」视图；HTTP 侧由 `PiecedSegment` 包装 piece，长度未知时由 `GrowSegment` 包装。SegmentMan 只管登记哪个 cuid 拿了哪个 segment，真正的「这个 piece 下载完没有」由 PieceStorage 回答。
- **在途（in-flight）piece**：已经被某条连接领取、还没下载完的 piece。它们既不算「已完成」，也不能再分给别的连接（end-game 模式除外）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `src/PieceStorage.h` | 存储层抽象接口：领取/归还 piece、查询完成度、初始化磁盘适配器 |
| `src/DefaultPieceStorage.cc` | 默认实现：长度已知时的 piece 管理，HTTP 与 BT 共用 |
| `src/DefaultPieceStorage.h` | `UsedPieceSet`、`END_GAME_PIECE_NUM` 等私有结构定义 |
| `src/BitfieldMan.cc` | 位图运算器：三张位图 + 各种「找缺失块」的策略 |
| `src/Piece.h` / `src/Piece.cc` | 单个 piece 的运行时对象：block 级位图、增量哈希、写缓存挂载点 |
| `src/UnknownLengthPieceStorage.cc` | 长度未知时的退化实现：永远只有一个 piece |
| `src/SegmentMan.cc` | 调用方：把 piece 包装成 Segment 分给命令 |
| `src/PiecedSegment.cc` / `src/GrowSegment.cc` | Segment 的两种实现，连接「写字节数」与「完成 block」 |
| `src/BtPieceMessage.cc` | BT 侧调用方：收到 piece 数据后校验并归还 |
| `src/RequestGroup.cc` | 工厂选择点：按「是否知道总长度」决定用哪套 PieceStorage |

## 4. 核心概念与源码讲解

### 4.1 PieceStorage 接口：存储层的统一契约

#### 4.1.1 概念说明

`PieceStorage` 是 aria2 存储层的抽象基类，回答三类问题：

1. **分配问题**——「还有哪些 piece 缺失？给我一个（或一批）」（`getMissingPiece` 家族）。
2. **记账问题**——「第 N 个 piece 下完了吗？」「整个任务完成百分之多少？」（`hasPiece`、`getCompletedLength`、`downloadFinished`）。
3. **落地问题**——「数据最终写到哪？」（`initStorage` 创建 `DiskAdaptor`，后者再创建 `DiskWriter`，见 u5-l1 与 u5-l3）。

它有两套并行的「领 piece」接口：一套面向 BT（带 `Peer` 参数，会考虑 peer 的位图、稀有度优先），一套面向 HTTP/FTP 等流式协议（带 `minSplitSize` 参数，走「最长空闲区间」策略）。这正是「HTTP 分段下载复用同一套 piece 机制」的接口体现。

#### 4.1.2 核心流程

接口的生命周期围绕一个状态机展开：

```text
piece 状态（由 BitfieldMan 的两个 bit 组合表达）
─────────────────────────────────────────────
bit=0, use=0   空闲缺失   ──getMissingPiece──▶ 在途（bit=0, use=1）
在途           ──completePiece──▶  完成（bit=1, use=0）
在途           ──cancelPiece───▶   空闲缺失（bit=0, use=0）
完成           ──markPieceMissing──▶ 空闲缺失（校验失败时回退）
```

#### 4.1.3 源码精读

接口声明集中在 [src/PieceStorage.h:56-58](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PieceStorage.h#L56-L58)，纯虚类、无成员数据。

两套领取接口的对比：

- BT 向量版 [src/PieceStorage.h:75-78](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PieceStorage.h#L75-L78)：一次领多个 piece，凑够 `minMissingBlocks` 个缺失 block 为止，只领「这个 peer 有而我没有」的。注释（L67-74）明确说明了 end-game 模式下同一 piece 可能被多个命令领走。
- 流式版 [src/PieceStorage.h:136-138](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PieceStorage.h#L136-L138)：带 `minSplitSize`（即 `-k` 选项）与 `ignoreBitfield`（别的连接正在下的范围），一次只领一个 piece。
- 指定下标版 [src/PieceStorage.h:146](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PieceStorage.h#L146)：`getMissingPiece(index, cuid)`，断点续传/抢占时用。

归还与查询：

- [src/PieceStorage.h:162](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PieceStorage.h#L162) `completePiece`：宣告一个 piece 下载完成。
- [src/PieceStorage.h:167-168](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PieceStorage.h#L167-L168) `cancelPiece`：连接放弃未完成的 piece。
- [src/PieceStorage.h:195](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PieceStorage.h#L195) 与 [src/PieceStorage.h:201](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PieceStorage.h#L201)：`downloadFinished`（考虑文件过滤，`--select-file` 选中的文件下完即真）与 `allDownloadFinished`（忽略过滤，全部下完才真）——两者的区别正是 `--select-file` 语义的落点。
- [src/PieceStorage.h:207](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PieceStorage.h#L207) `initStorage`：创建 DiskAdaptor，是存储层与磁盘 I/O 层（u5-l1/u5-l3）的接缝。

#### 4.1.4 代码实践

打开 `src/PieceStorage.h`，对照源文件数一数接口方法总数，并按「分配 / 记账 / 落地 / BT 专属（`#ifdef ENABLE_BITTORRENT` 包住的）/ 杂项」分类抄成一张表。注意 L60 与 L127 两处 `#ifdef ENABLE_BITTORRENT` 把哪些方法整个裁掉了——这解释了为什么关闭 BT 支持编译出的 aria2 依然能 HTTP 分段下载。

预期结果：约 40 个纯虚方法中，BT 专属的约 8 个，全部分配类方法都要求「防止重复分配」这一契约（读注释即可验证）。待本地验证具体数目。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `getMissingPiece` 领走 piece 后要标记「used」，而不是领走即从缺失集合删除？

**答案**：标记 used（`useBitfield_`）只是「预订」，piece 仍未完成；如果连接中途失败（超时、连接断开），`cancelPiece` 只需清掉 use 位，piece 自动回到可分配状态，不用恢复任何数据结构。若领走即删，取消时还得想办法把 piece 加回缺失集合，且期间进度统计（缺失字节数）会失真。

**练习 2**：`downloadFinished()` 和 `allDownloadFinished()` 分别在什么场景下被关心？

**答案**：`downloadFinished()` 在启用 `--select-file` 时只要求选中文件覆盖的 piece 全完成（选择性下载到此即可收工，哪怕没选的文件还是空洞）；`allDownloadFinished()` 要求整个位图全 1，BT 做种前的「下载完成」判定、以及 `completePiece` 里防止多算（见 4.4.3）都要用它。

### 4.2 BitfieldMan：三张位图管理全局进度

#### 4.2.1 概念说明

`BitfieldMan` 是一个可复用的位图运算器（Piece 内部还会嵌一个 block 级的实例）。`DefaultPieceStorage` 用它持有整个任务的**三张位图**：

| 位图 | 含义 | 谁置位 |
|---|---|---|
| `bitfield_` | 完成：该 piece 数据已完整写盘 | `completePiece` → `setBit` |
| `useBitfield_` | 在用：该 piece 已被某连接领取 | `checkOutPiece` → `setUseBit` |
| `filterBitfield_` | 过滤：该 piece 属于用户选中的文件 | `setupFileFilter` → `addFilter` |

所有「找缺失块」的查询都是这三张位图的按位运算组合，例如「缺失且不在用且在过滤范围内」就是 \(\lnot bit \land \lnot use \land filter\)。

#### 4.2.2 核心流程

构造时由总长度和块长度推导规模（[src/BitfieldMan.cc:62-70](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BitfieldMan.cc#L62-L70)）：

\[ blocks = \left\lceil \frac{totalLength}{blockLength} \right\rceil,\qquad bitfieldLength = \left\lceil \frac{blocks}{8} \right\rceil \]

查询策略（谁被选中）有两族：

- **稀疏策略**（默认，HTTP/FTP 用）：扫描所有「缺失且不在用」的连续区间，选**最长**的区间；若区间前一个 piece 正被别人使用，则改取区间**中点**——这样多条连接自动散开、互不干扰（u3-l4 讲过直觉）。
- **顺序策略**（`--stream-piece-selector=inorder`）：从前往后找第一个可用的；遇到前一个 piece 已完成的可立即接续；否则要求从该点起有至少 `minSplitSize` 的连续空闲空间。

每次位图变动后 `updateCache()` 重算缓存值（缺失块数、已完成长度等），把 O(n) 的统计变成查询时 O(1)。

#### 4.2.3 源码精读

**置位/清位原语**：[src/BitfieldMan.cc:561-574](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BitfieldMan.cc#L561-L574) 的 `setBitInternal` 用掩码 `128 >> (index % 8)` 操作单个 bit（bit 序从每字节最高位开始，与 BT 协议位图一致）。`setBit`/`unsetBit`（L586-598）在改动后调用 `updateCache()`。

**三态查询的位运算表达**：以 `getFirstMissingUnusedIndex` 为例，[src/BitfieldMan.cc:174-186](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BitfieldMan.cc#L174-L186) 直接对 `~bitfield_ & ~useBitfield_`（启用过滤时再 `& filterBitfield_`）求第一个置位位。`array(...)` 是 [src/bitfield.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bitfield.h) 提供的位图表达式包装，让 C++ 运算符重载生成逐字节的与或非。

**稀疏选择算法**：[src/BitfieldMan.cc:242-301](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BitfieldMan.cc#L242-L301)。`getStartIndex`/`getEndIndex`（L216-240）切出缺失区间；L260-264 检查区间前一块是否在用，在用则跳到 `getMidIndex()`；L278-295 在返回前还要过一道 `minSplitSize` 检查——除非前一块「已完成且无人使用」（可以无缝接续），否则新区间的连续空间必须达到 `minSplitSize` 才允许另起炉灶，这就是 `-k` 选项防止把文件切得太碎的机制。

**顺序选择算法**：[src/BitfieldMan.cc:381-423](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BitfieldMan.cc#L381-L423)。L387-392：起点 piece 永远优先返回；L397-400：前一块已完成则可直接接续；L405-415：从 `i` 向后累计空闲块，直到 \((j-i+1)\times blockLength \ge minSplitSize\)。

**完成长度统计**：[src/BitfieldMan.cc:824-834](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BitfieldMan.cc#L824-L834) 的 `getCompletedLength` 把「已完成块数」换算成字节数，注意最后一块要用 `getLastBlockLength()`（[src/BitfieldMan.cc:136-139](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BitfieldMan.cc#L136-L139)，总长减去前面整块）。缓存刷新见 [src/BitfieldMan.cc:846-853](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BitfieldMan.cc#L846-L853)。

**过滤位图**：[src/BitfieldMan.cc:714-725](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BitfieldMan.cc#L714-L725) 的 `addFilter(offset, length)` 把 \([offset/blockLength,\ (offset+length-1)/blockLength]\) 闭区间内的所有块置过滤位——注意一个 piece 若横跨两个文件的边界，两个文件任一被选中都会导致这个 piece 被置位（也就是会连带下载另一文件的一角数据）。`enableFilter` 见 L759-764。

#### 4.2.4 代码实践

用 `aria2c --help` 找到 `--stream-piece-selector` 选项的说明，然后分别用默认与 `=inorder` 下载同一个支持断点的大文件（如某个 ISO 的 HTTP 直链），开 `--log-level=debug -l log.txt`，在日志中过滤 `Attach segment#`（由 [src/SegmentMan.cc:140-141](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L140-L141) 打印）：

```bash
aria2c -x2 -s2 --stream-piece-selector=inorder \
  --log-level=debug -l log.txt <URL>
grep 'Attach segment#' log.txt
```

观察现象：`inorder` 时两条连接领到的 segment 编号会出现「0, 1」这样紧邻递增的序列；默认（稀疏）时会出现「0, 大编号中点」这样分散的序列。

预期结果：inorder 模式 segment 编号从小到大紧凑推进；默认模式编号分散。待本地验证（需要服务器支持 Range 且文件足够大）。

#### 4.2.5 小练习与答案

**练习 1**：`updateCache()` 在每次 `setBit`/`unsetBit` 后都重算一遍统计值，会不会成为性能瓶颈？

**答案**：单次 `setBit` 本身是 O(1) 的位操作，但 `updateCache` 要遍历位图（O(blocks)）。好在每次完成的 piece 只触发一次 `completePiece`，piece 数量级通常是几千到几万，且位图统计用 `countSetBit` 按字节查表/位运算，实测开销可接受；换来的是 `countMissingBlock()` 等高频查询变成 O(1)（[src/BitfieldMan.cc:537](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BitfieldMan.cc#L537) 直接返回缓存值）。

**练习 2**：`--select-file=2` 选中了种子里一个 100KB 的小文件，但 piece 长度是 1MB 且该文件横跨 piece 边界，磁盘上会写出什么？

**答案**：与该文件区间有交集的所有 piece（可能 2 个）都被置过滤位并下载，未选中文件在与这些 piece 重叠的区域也会被写入真实数据；但未选中 piece 对应的其余区域是预分配的空洞（或稀疏文件）。`downloadFinished()` 只检查过滤位图（`isFilteredAllBitSet`，[src/BitfieldMan.cc:600-613](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BitfieldMan.cc#L600-L613)），所以任务照样判完成。

### 4.3 Piece：单个 piece 的运行时对象

#### 4.3.1 概念说明

如果说 `BitfieldMan` 是「全局进度的账本」，`Piece` 就是「正在进行的 piece 的工作台账」。关键设计：**`DefaultPieceStorage` 只为「在途」的 piece 保留 `Piece` 对象，piece 一旦完成，对象即销毁，完成状态只留全局位图里的一个 bit**（见 [src/DefaultPieceStorage.cc:145-148](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L145-L148) 的注释：新建即完成的 Piece 不进 `usedPieces_`，因为「浪费内存且以后没机会再用」）。

`Piece` 内部有四类状态：

1. **block 级位图**：又是一个 `BitfieldMan`（block 长度 16K），记录 piece 内哪些 block 完成了——这是 BT 请求粒度的记账。
2. **用户表 `users_`**：记录哪些 cuid 正在下载这个 piece（普通模式只有 1 个，end-game 可多个）。
3. **增量哈希 `mdctx_` + `nextBegin_`**：BT 的 piece 校验要求整块 SHA1，aria2 边下边算，避免完成时再整块读盘。
4. **写缓存挂载点 `wrCache_`**：`--disk-cache` 开启时未落盘数据暂存在此（u5-l6 展开）。

#### 4.3.2 核心流程

一个 piece 的典型一生（HTTP 路径）：

```text
checkOutPiece: new Piece(index, length) → 加入 usedPieces_ → addUser(cuid)
      │
      ▼  连接每写一段数据
PiecedSegment::updateWrittenLength(bytes)
      │  跨过的每个 block 边界
      ▼
Piece::completeBlock(i)   ── bitfield 置位 + use 位清除
      │  最后一个 block 完成
      ▼
SegmentMan::completeSegment → DefaultPieceStorage::completePiece
      │  从 usedPieces_ 移除对象，全局位图 setBit
      ▼
（对象销毁，只剩 1 个 bit）
```

#### 4.3.3 源码精读

**16KB block 常量**：[src/Piece.h:75](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.h#L75) `static const int32_t BLOCK_LENGTH = 16_k;`。成员总览见 [src/Piece.h:56-75](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.h#L56-L75)。

**构造**：[src/Piece.cc:56-63](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.cc#L56-L63)。每个 Piece 内嵌一个以 `blockLength`（默认 16K）为块长度的 `BitfieldMan`——同一个类，两层用途。

**block 完成**：[src/Piece.cc:67-71](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.cc#L67-L71)。`completeBlock` = 位图置位 + 清 use 位；piece 完成判定 `pieceComplete()`（L84）即位图全 1。

**BT 的 block 领取**：[src/Piece.cc:141-153](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.cc#L141-L153) `getMissingUnusedBlockIndex(indexes, n)` 批量领 n 个「缺失且未在用」的 block 并立刻置 use 位——BT 请求流水线（u6-l6）每次填请求队列就从这里拿 block。

**增量哈希**：[src/Piece.cc:194-212](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.cc#L194-L212) `updateHash` 只在数据**按序连续**到达时才喂给摘要上下文（`begin == nextBegin_` 判定），乱序到达直接返回 false，等 `getDigestWithWrCache`（L253-275）在完成时用「缓存 + 读盘」补算。

**多用户记账**：[src/Piece.cc:283-298](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.cc#L283-L298) `users_` 向量的增删；`usedBySegment_` 标志（[src/Piece.h:174-175](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.h#L174-L175)）标记该 piece 被 HTTP/FTP 的 Segment 持有——BT 侧领 piece 时会避开这类 piece（见 4.4.3 end-game 分支里 `getUsedBySegment` 的判断）。

**长度可变**：[src/Piece.cc:172-183](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.cc#L172-L183) `reconfigure` 重新设定长度并重建位图；注释说明它只被 `GrowSegment::updateWrittenLength` 调用，且为省开销故意用「最大块长」让位图退化为单块。

#### 4.3.4 代码实践

阅读 [src/PiecedSegment.cc:69-82](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PiecedSegment.cc#L69-L82) 的 `updateWrittenLength`，回答：为什么 L78-80 要单独处理 `newWrittenLength == piece_->getLength()` 的分支，而不是靠循环覆盖？

提示：整除边界。假设 piece 长 100KB、block 长 16K，写满 100KB 时 `100/16=6`，循环只完成 block 0..5，第 6 个 block（残余的 4KB）永远不会被循环覆盖，所以最后一块要手工补上。

预期结果：你能独立推导出「最后一块长度 = length - (countBlock()-1)×blockLength，恒小于等于 blockLength」并解释 L79 的 `completeBlock(countBlock() - 1)`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Piece 的哈希更新要求「按序连续」，乱序数据宁可重算？

**答案**：SHA1 之类的摘要只能按字节序流式更新，无法乱序拼接。BT 的 block 可能乱序到达（不同请求完成时间不同），若强行缓存乱序数据等齐再算，内存开销和复杂度都高；aria2 的折中是按序增量算一部分，piece 完成时若哈希未算完，用 `getDigestWithWrCache` 把「写缓存里的数据 + 磁盘上的数据」按偏移顺序读一遍补算（[src/Piece.cc:253-275](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.cc#L253-L275)）。

**练习 2**：`Piece::operator==`（[src/Piece.h:82](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.h#L82)）只比较 index，这意味着什么？

**答案**：在 `usedPieces_`（按 index 排序的 set）里「同 index 的 piece」就是同一个 piece。`findUsedPiece`（[src/DefaultPieceStorage.cc:172-185](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L172-L185)）正是先造一个只带 index 的空 Piece 当查找键去 set 里捞真对象的。

### 4.4 DefaultPieceStorage：领取与归还的完整调用链

#### 4.4.1 概念说明

`DefaultPieceStorage` 是长度已知时的唯一正牌实现，HTTP/FTP/BT/Metalink 全走它。它的私有状态很简单：一个全局 `BitfieldMan`（piece 级）、一个 `usedPieces_` 集合（在途 Piece 对象，按 index 排序）、一个 `DiskAdaptor`、两个「选择器」：

- `pieceSelector_`（BT 用，默认 `RarestPieceSelector`——全局越稀有的 piece 越优先，对 swarm 健康有利）；
- `streamPieceSelector_`（HTTP/FTP 用，由 `--stream-piece-selector` 决定 default/inorder/random/geom）。

两个选择器分别服务 4.2 中讲的 BT 位图运算族和流式运算族——同一份位图，两种挑法。

#### 4.4.2 核心流程

**领取链（HTTP/FTP）**：

```text
SegmentMan::getSegment(cuid, minSplitSize)
  └─ PieceStorage::getMissingPiece(minSplitSize, ignoreBitfield, len, cuid)
       └─ streamPieceSelector_->select(index, ...)     # 位图策略挑 index
            └─ checkOutPiece(index, cuid)
                 ├─ bitfieldMan_->setUseBit(index)      # 预订
                 ├─ findUsedPiece / new Piece           # 复用或新建台账
                 ├─ piece->addUser(cuid)
                 └─ piece->initWrCache(...)             # 可选写缓存
  └─ checkoutSegment(cuid, piece)                       # 包成 PiecedSegment
```

**归还链（HTTP/FTP）**：

```text
DownloadCommand 写满一段 → Segment::updateWrittenLength → Piece::completeBlock×n
  └─ segment 完成 → DownloadCommand::completeSegment
       └─ SegmentMan::completeSegment
            ├─ pieceStorage_->completePiece(piece)   # 记账 + 收尾
            └─ pieceStorage_->advertisePiece(...)    # 通知其他命令发 have
```

**归还链（BT）**：`BtPieceMessage` 收齐数据 → `checkPieceHash` 校验 → `onNewPiece` → `completePiece`。

#### 4.4.3 源码精读

**构造**：[src/DefaultPieceStorage.cc:78-115](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L78-L115)。全局位图以 `pieceLength × totalLength` 构造（L82-83）；BT 选择器默认稀有度优先（L94）；L97-114 按 `--stream-piece-selector` 装配流式选择器。

**checkOutPiece——领取的公共出口**：[src/DefaultPieceStorage.cc:119-143](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L119-L143)。四步：置 use 位 → 找/建 Piece 对象（还顺手设置 piece 哈希类型）→ `addUser(cuid)` → 可选挂写缓存（L136-141，内存下载如 magnet 元数据不挂）。

**流式领取**：[src/DefaultPieceStorage.cc:386-399](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L386-L399)，一行选择器调用 + `checkOutPiece`。指定下标版在 L401-412，多三重防御：已完成 / 已在用 / 不在过滤范围内都返回空。

**BT 批量领取**：[src/DefaultPieceStorage.cc:195-249](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L195-L249)。普通模式（L231-248）：`getAllMissingUnusedIndexes` 求出「我缺 ∧ 不在用 ∧ peer 有」的位图（[src/BitfieldMan.cc:514-535](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BitfieldMan.cc#L514-L535)），循环让 `pieceSelector_`（稀有度优先）挑 index，直到凑够 `minMissingBlocks` 个缺失 block。**end-game 模式**（L203-230）：改用 `getAllMissingIndexes`（不排除「在用」！），再整体 shuffle 随机领——收尾阶段缺失 block 太少，允许多个 peer 同时下载同一 piece 的不同 block（这就是 Piece 的 `users_` 是向量的原因）；L221-224 还会避开被 HTTP Segment 持有的 piece（`getUsedBySegment`），不与流式连接抢食。调用方是 [src/DefaultBtInteractive.cc:375-411](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L375-L411) 的 `fillPiece`；进入 end-game 的判定在其上方 [src/DefaultBtInteractive.cc:414-419](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L414-L419)（`!hasMissingUnusedPiece()` 即触发，阈值常量 `END_GAME_PIECE_NUM=20` 见 [src/DefaultPieceStorage.h:56](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.h#L56)）。

**completePiece——归还的记账与收尾**：[src/DefaultPieceStorage.cc:459-511](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L459-L511)。逐行看：

- L464 `deleteUsedPiece`：先把 Piece 对象从 `usedPieces_` 移除（台账销毁，见 4.3.1 的内存设计）；
- L468-470：若**全部**下完（`allDownloadFinished`），直接返回——不再需要任何记账（做种场景收尾）；
- L471-473：全局位图 `setBit` + 清 use 位 + 稀有度统计 `addPieceStats`；
- L474-510：若触发 `downloadFinished()`，重置停止计时、打日志，BT 场景（L486-508）还执行 `on-bt-download-complete` 钩子、发通知、`enableSeedOnly()` 切做种。

**进度统计要算「在途」**：[src/DefaultPieceStorage.cc:556-565](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L556-L565)。`getCompletedLength` = 位图换算的完成长度 **+** 遍历 `usedPieces_` 累加各在途 piece 的 `getCompletedLength()`（L573-580）。只看位图会低估进度——在途 piece 已下载的部分（比如 1MB 的 piece 已到手 900KB）在位图上还是 0。

**cancelPiece——失败归还**：[src/DefaultPieceStorage.cc:519-534](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L519-L534)。`removeUser` 后若无人再用则清 use 位；非 end-game 且 piece 一字节未下（L530）时直接销毁台账对象——部分下载的 piece 保留对象，下次领到同一个 piece 还能续上 block 位图。

**`--select-file` 的入口**：[src/DefaultPieceStorage.cc:594-614](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L594-L614) `setupFileFilter`：遍历 `FileEntry`，只要有一个文件未被 `isRequested()` 就把所有被请求文件的 `[offset, offset+length)` 加入过滤位图并 `enableFilter()`。此后 4.2.3 列出的所有缺失查询都自动带上 `& filterBitfield_`，未选中文件的 piece 从此不可能被任何选择器选中。

**磁盘适配器选择**：[src/DefaultPieceStorage.cc:634-662](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L634-L662) `initStorage`：单文件任务用 `DirectDiskAdaptor`（内部一个 DiskWriter 直写目标文件），多文件任务用 `MultiDiskAdaptor`（按文件树逐文件开写，逻辑偏移到物理文件的映射在那一层做，见 u5-l3）。文件预分配方式（falloc/trunc）也在这里下发给适配器。

**断点续传的状态恢复**：[src/DefaultPieceStorage.cc:754-783](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L754-L783) `markPiecesDone(length)`：`--check-integrity` 校验后按实际已有字节长度标记位图——整块部分 `setBitRange(0, numPiece-1)`，不满一块的尾巴（L769-781）造一个带部分完成 block 的 Piece 放进 `usedPieces_`，续传时从 block 粒度接续。入口在 u5-l5 的校验体系。

**HTTP 侧的上游**（承接 u3-l4）：[src/SegmentMan.cc:203-210](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L203-L210) `getSegment` 调流式版领取；[src/SegmentMan.cc:134-162](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L134-L162) `checkoutSegment` 做 `setUsedBySegment(true)` 并按 piece 长度是否为 0 决定包 `PiecedSegment` 还是 `GrowSegment`；[src/SegmentMan.cc:373-388](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L373-L388) `completeSegment` 调 `completePiece` 并 `advertisePiece`。HTTP 下载收尾的真正发起者是 [src/DownloadCommand.cc:383-408](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L383-L408)：`validatePieceHash` 校验 piece 哈希通过才调 `completeSegment`（失败则 `segment->clear` + `cancelSegment` + 抛重试异常，L391-399）。

**BT 侧的上游**：[src/BtPieceMessage.cc:265-284](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtPieceMessage.cc#L265-L284) `onNewPiece`：先冲刷写缓存（L267-278），打 INFO 日志 `we got new piece`（L279），再 `completePiece` + `advertisePiece`。校验失败走 [src/BtPieceMessage.cc:286-293](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtPieceMessage.cc#L286-L293) `onWrongPiece`：清空全部 block、销毁哈希上下文、把 piece 移出请求目标——整块重来。

#### 4.4.4 代码实践

**实践目标**：观察一个多文件种子的 piece 完成顺序，验证「BT 稀有度优先 + 随机性」导致下载顺序与磁盘顺序不一致；再验证 `--select-file` 收窄了 piece 选择范围。

**方法 A（免编译，推荐先做）**：aria2 本身已在 `onNewPiece` 打 INFO 日志（消息模板见 [src/message.h:56](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h#L56)：`CUID#% - we got new piece. index=%lu`）：

```bash
# 全量下载
aria2c -l full.log --log-level=info your.torrent
# 只下第 1 个文件（文件编号用 --show-files 查）
aria2c --show-files your.torrent
aria2c -l sel.log --log-level=info --select-file=1 your.torrent

grep -o 'index=[0-9]*' full.log | head -50
grep -o 'index=[0-9]*' sel.log  | head -50
```

观察与预期：

1. `full.log` 的 index 序列**不是**从 0 递增——稀有度优先策略让 aria2 先抓 swarm 中较少的 piece，且 end-game 前后顺序更乱；日志行首自带时间戳，可直接当完成时间轴用。
2. `sel.log` 的 index 全部落在一个连续区间内（选中文件覆盖的 piece 区间；区间外的 piece 永不出现）。
3. 若两个文件共享边界 piece，`sel.log` 里会出现属于邻接文件的 index（对照 4.2.3 `addFilter` 的区间闭包）。

**方法 B（按规格改码，得到精确时间戳）**：在 [src/DefaultPieceStorage.cc:459](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L459) 的 `completePiece` 开头插入一行日志（示例代码，非项目原有）：

```cpp
void DefaultPieceStorage::completePiece(const std::shared_ptr<Piece>& piece)
{
  if (!piece) {
    return;
  }
  // 示例代码：打印每个 piece 的完成时刻（相对进程启动的毫秒数）
  A2_LOG_NOTICE(fmt("PIECE-DONE index=%lu elapsed-ms=%ld",
                    static_cast<unsigned long>(piece->getIndex()),
                    static_cast<long>(
                        global::wallclock().differenceFromCreationMillis())));
  ...
```

注意：`wallclock()` 的 Timer API 以何种单位提供「与进程启动的差值」需查看 [src/TimerA2.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TimerA2.h) 后按实际方法名调整（`differenceFromCreationMillis` 这个名字是示意，待确认）；最简单的稳妥写法是直接依赖日志行首的 aria2 自带时间戳，即回到方法 A。改完后 `make -j$(nproc)` 重编译并重跑上面的两条命令。

预期结果与方法 A 相同；两条路径互为印证。BT 下载需要真实网络与可用 peer，「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `completePiece` 里 L468 的 `allDownloadFinished()` 提前返回不会丢账（比如最后两个 piece 同时完成时）？

**答案**：`allDownloadFinished()` 查的是**全局位图** `isAllBitSet()`。两个 piece 并发完成时，第一个到达的 `completePiece` 先 `setBit` 自己的位；第二个到达时若发现位图已全 1，说明「收尾动作」（钩子、通知、切做种）已被前者做过，自己只需保证 `deleteUsedPiece` 已执行（L464 在检查之前），跳过后续记账是安全的。而 `setBit` 的幂等性（重复置同一 bit 无副作用）保证了即使顺序交错也不会出错。

**练习 2**：HTTP 下载为什么可以和 BT 共用 `completePiece`，两者对「piece 完成」的判定时机有何不同？

**答案**：HTTP 侧由 `DownloadCommand::validatePieceHash`（有 piece 哈希时）或写满 segment（无哈希时，[src/DownloadCommand.cc:274](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L274)）判定后调 `SegmentMan::completeSegment`；BT 侧由 `BtPieceMessage` 在校验整块哈希通过后的 `onNewPiece` 调用。判定时机不同（HTTP 按 segment 写满、BT 按 piece 哈希），但都汇聚到 `completePiece` 这一个记账出口——这正是抽象的价值：进度位图、完成检查、收尾钩子只写一遍。

**练习 3**：`getCompletedLength()` 为什么要遍历 `usedPieces_` 累加在途进度，控制台进度条不这样会怎样？

**答案**：位图只认「整块完成」，在途 piece 的部分字节不可见。若不补偿，用户会看到进度条长时间不动（一个大 piece 下完才跳一格），对 4MB piece 的种子尤其明显。遍历代价可控，因为 `usedPieces_` 里的对象数 ≈ 并发连接数 + end-game 少量，通常几十个。

### 4.5 UnknownLengthPieceStorage：长度未知时的退化实现

#### 4.5.1 概念说明

HTTP 下载经常遇到响应没有 Content-Length（chunked 编码、某些动态生成页面）的情况——总长度未知，piece 数无法计算，位图无从建起。此时 [src/RequestGroup.cc:555-612](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L555-L612) 的 `initPieceStorage` 走 else 分支（L606-612）换装 `UnknownLengthPieceStorage`。判定条件（L558-565）：`knowsTotalLength()` 且 `totalLength > 0`（或带 BT 属性）才用 `DefaultPieceStorage`。

这套实现的哲学是「一个动态生长的 piece」：永远只有 index 0 的一个 piece，长度从 0 开始随下载增长，直到下载结束长度才定下来。

#### 4.5.2 核心流程

```text
getMissingPiece ──▶ new Piece()          # index=0, length=0，无位图
      │
      ▼  GrowSegment::updateWrittenLength(bytes)
piece->reconfigure(writtenLength_)        # 长度撑大，位图重建为"单块"
piece->setAllBlock()                      # 已下载部分视为整块完成
      │
      ▼  连接结束
completePiece ──▶ totalLength_ = piece 长度；createBitfield() 全 1 位图
```

#### 4.5.3 源码精读

**退化的领取**：[src/UnknownLengthPieceStorage.cc:129-144](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UnknownLengthPieceStorage.cc#L129-L144)。`getMissingPiece` 不看任何位图：没有在途 piece 就 new 一个默认 `Piece()`（对应 [src/Piece.cc:54](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.cc#L54) 的无参构造，长度 0、位图为空）发出去；已有在途就返回空——**同一时刻最多一条连接在下载**，这正是长度未知时无法分段（无 Range 依据）的体现。

**完成的瞬间才建位图**：[src/UnknownLengthPieceStorage.cc:172-183](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UnknownLengthPieceStorage.cc#L172-L183) `completePiece`：把最终长度写回 `totalLength_`、同步给 DiskAdaptor，然后 `createBitfield()`（L234-241）按 `DownloadContext` 的 piece 长度建一个全 1 位图——下载已经结束，这个位图不是给调度用的，而是给 `.aria2` 控制文件、RPC 状态上报等「读位图」的消费者一个一致的视图。

**BT 接口全部 `abort()`**：[src/UnknownLengthPieceStorage.cc:76-127](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UnknownLengthPieceStorage.cc#L76-L127)。所有带 `Peer` 参数的方法（含 `hasMissingUnusedPiece`，L127）直接 `abort()` 崩掉进程。这不是偷懒：BT 协议握手必须互发位图，长度未知的任务根本不可能进入 BT 交互，调用到这些方法即是程序逻辑错误，宁可快速失败。同理 `getPieceLength`（L227-232）也是 abort。

**配对的 GrowSegment**：长度为 0 的 piece 在 [src/SegmentMan.cc:154-156](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L154-L156) 被包成 `GrowSegment`；其 `updateWrittenLength`（[src/GrowSegment.cc:48-53](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/GrowSegment.cc#L48-L53)）每次调用 `piece->reconfigure(writtenLength_)` 撑长 piece 再 `setAllBlock()`——「已下载部分」整体视为完成，位图形同虚设（也正因此 `reconfigure` 用最大块长避免频繁重建的开销，见 [src/Piece.cc:174-182](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.cc#L174-L182) 的注释）。

**magnet 的过渡态**：magnet 链接先创建一个「只下载 torrent 元数据」的任务（u6-l9 展开），其长度同样未知，也用这套存储；元数据下完后由 `UTMetadataDataExtensionMessage` 调 `markAllPiecesDone`（[src/UnknownLengthPieceStorage.cc:243-253](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UnknownLengthPieceStorage.cc#L243-L253)）收尾并建全 1 位图。

#### 4.5.4 代码实践

用一个「无 Content-Length」的响应做实验。Python 起一个最小 chunked 服务（示例代码）：

```python
# serve_chunked.py —— 示例代码
from http.server import BaseHTTPRequestHandler, HTTPServer

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for i in range(5):
            body = b"x" * 1024
            self.wfile.write(b"%x\r\n%s\r\n" % (len(body), body))
        self.wfile.write(b"0\r\n\r\n")

HTTPServer(("127.0.0.1", 8931), H).serve_forever()
```

（注意：请把脚本放在独立目录再运行，遵守本仓库对下载/脚本的目录隔离要求。）然后：

```bash
python3 serve_chunked.py &
aria2c -l log.txt --log-level=debug http://127.0.0.1:8931/
grep -E 'Attach segment|Instantiating' log.txt
```

观察与预期：

1. 日志里 DiskAdaptor 是 `DirectDiskAdaptor`（单文件），但没有 piece 切分的多次 `Attach segment#`——只有一次 segment 且 index 恒为 0（长度未知，`GrowSegment` 路径）。
2. 对照源码 [src/UnknownLengthPieceStorage.cc:129-144](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UnknownLengthPieceStorage.cc#L129-L144)：第二条连接（`-x2`）领不到任何 piece。
3. 下载完成后文件正好 5120 字节。待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `UnknownLengthPieceStorage` 的 `getCompletedLength()`（[src/UnknownLengthPieceStorage.cc:213-220](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UnknownLengthPieceStorage.cc#L213-L220)）返回的是「piece 总长」而不是位图换算？

**答案**：唯一的 piece 在 `GrowSegment::updateWrittenLength` 里被 `setAllBlock()` 标成全完成，位图换算与直接返回 `piece->getLength()` 等价；源码选择直接读长度（在途时）或 `totalLength_`（已结束时），语义是「凡已收到的字节都算完成」。源码里还留着 TODO 注释（L215）自问是否该返回真实完成长度。

**练习 2**：一个 HTTP 服务器先返回 `Content-Length: 0` 加 chunked body（Google 下载服务的历史行为，[src/RequestGroup.cc:559-561](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L559-L561) 的注释提到了这个 case），会走哪套存储？

**答案**：走 `DefaultPieceStorage`。判定条件是 `totalLength > 0 || hasAttribute(CTX_ATTR_BT)`——BT 属性即便长度为 0 也强制走默认实现，`Content-Length: 0` 且非 BT 的任务才会掉进 `UnknownLengthPieceStorage`。注释明说这两条豁免就是为 chunked + content-length=0 的服务器加的。

## 5. 综合实践

**任务：给一个多文件种子做「piece 完成时间轴」分析，并用源码解释三条观察。**

1. 准备：任选一个含 3 个以上文件、总大小 50MB 以上的种子（或用 `mktorrent` 自制并本地做种）；确认 `aria2c` 已按 u1-l2 构建。
2. 采集：按 4.4.4 的方法 A 跑两次——全量与 `--select-file=<某中间文件>`，各留一份 `--log-level=info` 日志。
3. 分析一（下载顺序 vs 磁盘顺序）：把 `grep -o 'index=[0-9]*' full.log` 的序列画成散点（x=日志行序即时间，y=index）。预期散点分散无趋势；用 4.4.3 的 `RarestPieceSelector`（构造于 [src/DefaultPieceStorage.cc:94](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L94)）与 end-game 的 shuffle（[src/DefaultPieceStorage.cc:215-216](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L215-L216)）解释原因。
4. 分析二（`--select-file` 边界）：对照 `--show-files` 输出的文件列表与 `sel.log` 出现的 index 集合，找出「不属于选中文件但被下载」的边界 piece，用 [src/DefaultPieceStorage.cc:608-613](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L608-L613) 与 [src/BitfieldMan.cc:714-725](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BitfieldMan.cc#L714-L725) 解释区间闭包如何产生这种现象。
5. 分析三（完成判定）：确认 `--select-file` 下载结束时 `sel.log` 出现 `MSG_SELECTIVE_DOWNLOAD_COMPLETED` 对应的完成日志（由 [src/DefaultPieceStorage.cc:476-477](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L476-L477) 打印），而全量下载打印的是另一条普通完成日志（L483）——两条路径同在 `completePiece` 内分叉。
6. 产出：一份包含散点图、边界 piece 对照表和三条源码引用的简短笔记。

预期结果：三个观察都能用本讲的位图三态、过滤闭包、downloadFinished/allDownloadFinished 分叉解释。BT 部分依赖真实 peer，环境不具备时步骤 3-5 的结论标注「待本地验证」，源码推导部分仍可完成。

## 6. 本讲小结

- `PieceStorage` 是存储层的统一契约：两套领取接口（BT 向量版 / 流式单块版）+ 归还接口（`completePiece`/`cancelPiece`）+ 落地接口（`initStorage` 创建 DiskAdaptor→DiskWriter）。
- `BitfieldMan` 用完成、在用、过滤三张位图的按位运算回答所有「找缺失块」问题；稀疏策略取最长区间（邻居在用则取中点），inorder 策略受 `minSplitSize` 约束；统计值经 `updateCache` 缓存。
- `Piece` 只为在途 piece 存活：block 级位图、多用户表、增量哈希、写缓存挂载点；完成后对象销毁，全局位图只留一个 bit——内存与信息的平衡点。
- `completePiece` 是唯一记账出口：HTTP（`DownloadCommand`→`SegmentMan::completeSegment`）与 BT（`BtPieceMessage::onNewPiece` 校验后）殊途同归，随后触发完成检查、钩子与做种切换；`getCompletedLength` 要额外累加在途 piece 的部分进度。
- `--select-file` 经 `setupFileFilter` 落到过滤位图，此后一切选择查询带 `& filter`，且 piece 与文件区间是闭包相交——边界 piece 会连带邻文件数据。
- `UnknownLengthPieceStorage` 是「单个生长 piece」的退化实现：单连接、无位图，完成瞬间才建全 1 位图；BT 接口直接 `abort()`，因为长度未知的任务不可能有 BT 语义。

## 7. 下一步学习建议

本讲把「piece 的账」讲完了，数据「落到磁盘」的路径只碰到 `DiskAdaptor` 的门口。建议：

1. **u5-l3（DiskAdaptor）**：看 `initStorage` 创建的两个适配器如何把「piece 的逻辑偏移」映射到单文件/多文件树的真实磁盘布局，`--select-file` 未选文件的空洞如何在那一层落实。
2. **u5-l5（校验与哈希）**：`markPiecesDone` 的调用方 `IteratableChunkChecksumValidator` 如何在启动时把已有文件折算成 piece 位图，与本讲的断点恢复衔接。
3. **u5-l6（写盘缓存）**：`Piece` 上挂的 `WrDiskCacheEntry` 与 `completePiece`/`cancelPiece` 里冲刷缓存的时机。
4. 若你更关心 BT 主线：先读 u6-l2（BtRegistry/BtSetup），再回来看 `DefaultBtInteractive::fillPiece` 如何消费本讲的 `pieceSelector_`。
