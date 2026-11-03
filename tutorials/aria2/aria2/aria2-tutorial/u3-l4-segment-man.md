# SegmentMan 与分段调度：多线程下载的切分逻辑

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 aria2 如何用两张「尺子」（piece 与 block）把一个文件切分成可管理的单元，以及 BitfieldMan 的三张位图（完成位图、在用位图、过滤位图）分别记录什么。
2. 跟踪一次 segment 分配的完整调用链：`AbstractCommand::execute` → `SegmentMan::getSegment` → `PieceStorage::getMissingPiece` → `BitfieldMan::getSparseMissingUnusedIndex` → `checkOutPiece`，并说出 `minSplitSize`（`-k` 选项）在其中的作用。
3. 区分 segment 的三种归宿：完成（`completeSegment`）、取消归还（`cancelSegment`，配合 `segmentWrittenLengthMemo_` 记忆量）、以及「抢占空闲邻居」（`getCleanSegmentIfOwnerIsIdle`）。
4. 说明 RequestSlot 如何在 BitTorrent 下载中充当「在途请求账本」，超时与 choke 时如何回收。
5. 理解 SegmentMan 记录的 PeerStat 如何沉淀为 ServerStat，进而影响下一个镜像源的挑选。

## 2. 前置知识

- **piece（片）与 block（块）**：aria2 把整个下载看成一串定长的 piece。对 HTTP/FTP 下载，piece 长度由 `--piece-length` 决定（默认 1M，见 [src/OptionHandlerFactory.cc:906](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L906)）；对 BT 下载由种子文件 info 字典给出。piece 内部再按 16K（`Piece::BLOCK_LENGTH`）切成 block，block 是 BT 协议 request 消息的粒度，也是记录「写到哪了」的最小刻度。
- **位图（bitfield）**：用一个字节数组表示大量布尔值，第 \(i\) 个 piece 的状态存在第 \(i/8\) 个字节的第 \(i \bmod 8\) 位上。aria2 用「高位在前」的排列（字节内第 0 位是最高位 0x80）。
- **cuid**：Command Unique ID，每条下载连接（命令）的唯一编号。segment 分配记录的就是「哪个 cuid 正在用哪个 segment」。
- **`-s` 与 `-k`**：`-s/--split=N` 表示最多用 N 条连接下载一个文件（[src/usage_text.h:53-62](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/usage_text.h#L53-L62)）；`-k/--min-split-size=SIZE` 表示小于 2*SIZE 的剩余空间不再切分（[src/usage_text.h:745-753](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/usage_text.h#L745-L753)）。注意 `-s` 只是预算上限，同一主机的连接数还受 `-x/--max-connection-per-server` 限制（u3-l1 已讲）。
- **「多线程」其实是多连接**：aria2 是单线程事件循环（u2-l1），所谓多线程下载是多条 Command 各自持有 HTTP Range 请求并行搬运不同的 piece。
- **前置讲义**：本讲依赖 u3-l1（RequestGroup 的并发模型）、u2-l3（Command 生命周期与 `prepareForRetry`）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/BitfieldMan.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BitfieldMan.cc) | 位图容器与各种「找缺失块」的挑选算法（sparse/inorder/geom），是整个分段调度的算法核心 |
| [src/Piece.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.cc) | 单个 piece 的运行时状态：块级位图、分块哈希、写盘缓存挂接 |
| [src/Segment.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Segment.h) | Segment 抽象接口：面向单条连接的「写入进度」视图 |
| [src/PiecedSegment.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PiecedSegment.cc) / [src/GrowSegment.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/GrowSegment.h) | Segment 的两个实现：定长 piece 版与「文件长度未知」的生长版 |
| [src/SegmentMan.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc) / [src/SegmentMan.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.h) | segment 的登记处：分配、取消、完成、记忆量、PeerStat 统计 |
| [src/RequestSlot.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestSlot.h) / [src/RequestSlot.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestSlot.cc) | BT 在途请求的一行账：发给了谁、请求哪块、何时发出 |
| [src/AbstractCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc) / [src/DownloadCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc) | segment 的消费方：命令在哪里领取 segment、写完如何换下一个 |
| [src/DefaultPieceStorage.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc) | segment 分配的下游：选中 index 后真正 checkout piece（u5-l2 深入） |
| [src/ServerStat.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ServerStat.cc) / [src/RequestGroupMan.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc) | 镜像服务器速度画像：PeerStat 沉淀为 ServerStat 的通路 |

## 4. 核心概念与源码讲解

### 4.1 BitfieldMan 与 Piece：位图运算的基石

#### 4.1.1 概念说明

分段下载要回答三个问题：

1. 哪些数据已经有了？（完成位图 `bitfield_`）
2. 哪些数据正被某条连接下载？（在用位图 `useBitfield_`）
3. 哪些数据本次不要？（过滤位图 `filterBitfield_`，例如多文件任务里未选中的文件）

`BitfieldMan` 就是这三张位图加上一组「在满足约束的前提下挑一个下标」的算法。它有两个使用层次：

- **全局层**：`DefaultPieceStorage` 用一个覆盖全文件的 `BitfieldMan`（以 piece 长度为块长）回答「哪个 piece 缺失」。
- **piece 层**：每个 `Piece` 对象内部又有一个以 16K 为块长的小 `BitfieldMan`，回答「这个 piece 里哪个 block 缺失」（BT 的 request 消息按 block 发）。

#### 4.1.2 核心流程

设总长度为 \(L\)、块长为 \(b\)，则块数与位图字节数为：

\[
n = \left\lceil \frac{L}{b} \right\rceil, \qquad
\text{bytes} = \left\lceil \frac{n}{8} \right\rceil
\]

第 \(i\) 块覆盖的字节区间是 \([i\cdot b,\ \min((i+1)\cdot b,\ L))\)，最后一块可能不足 \(b\)。

「找一块可下载的」通用公式是把几张位图按位组合成一个「不可用位图」，再在它的补集里找。例如默认的稀疏选择：

```
不可用 = ignore(调用方排除) | 已完成 | 在用        # 有过滤时还要 | ~filter
在 [0, n) 中找一个「不可用=0」的下标，策略见下
```

四个挑选策略（`--stream-piece-selector`）：

| 策略 | 算法 | 特点 |
| --- | --- | --- |
| `default`（sparse） | 扫描所有「连续可用区间」，选**最长**的，从区间头部（或中部）开始 | 多连接自动均匀散开，像二分 |
| `inorder` | 从头顺序找：优先紧贴已完成前缀的块；否则找到第一个长度 ≥ minSplitSize 的空闲连续段 | 顺序落盘，利于边下边播 |
| `geom` | 从上次起点开始按几何级数（底 1.5）扩张搜索窗口 | 兼顾顺序与均衡 |
| `random` | 随机起点 + inorder 兜底 | 减少与其它客户端的挤兑 |

#### 4.1.3 源码精读

**构造与位图布局**——[src/BitfieldMan.cc:47-71](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BitfieldMan.cc#L47-L71) 按上面的公式算出 `blocks_` 与 `bitfieldLength_`，分配三张位图并清零；最后一块的长度单独由 [src/BitfieldMan.cc:136-139](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BitfieldMan.cc#L136-L139) 计算：`totalLength_ - blockLength_ * (blocks_ - 1)`。

**置位原语**——[src/BitfieldMan.cc:561-574](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BitfieldMan.cc#L561-L574)：`mask = 128 >> (index % 8)` 说明字节内高位对应小下标，这与 BT 协议线上 bitfield 字节序一致，可以直接整体拷贝比较。

**基础挑选**——[src/BitfieldMan.cc:174-186](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BitfieldMan.cc#L174-L186) 的 `getFirstMissingUnusedIndex` 用表达式模板把 `~bitfield_ & ~useBitfield_ (& filterBitfield_)` 惰性求值成一个数组，再找第一个为 1 的位（`array_fun.h` 的 `array` 让多个字节数组像一个大数组一样被逐字节按位运算，不产生临时内存）。

**稀疏策略（默认，最重要）**——[src/BitfieldMan.cc:242-300](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BitfieldMan.cc#L242-L300)：

- L252-277：扫描出所有连续可用区间 `Range(startIndex, endIndex)`。关键在 [L260-264](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BitfieldMan.cc#L260-L264)：如果区间起点的前一块「正被别人使用」，说明邻居正从那里向前推进，本连接就从**区间中点** `getMidIndex()` 开始，避免两人向同一汇合点挤。
- L267-274：区间一样大时的平局裁决，偏好「前一块已完成且不在用」的区间（能无缝续传）。
- [L278-299](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BitfieldMan.cc#L278-L299)：最后一道闸门——要么前一块已完成且空闲（紧贴着续传），要么区间长度满足 `(endIndex - startIndex) * blockLength >= minSplitSize`。这就是 `-k` 选项唯一生效的地方：**切出来的新段至少要有 minSplitSize 那么大的连续空间**。

外层包装 [src/BitfieldMan.cc:303-320](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BitfieldMan.cc#L303-L320) 把调用方传入的 `ignoreBitfield`（SegmentMan 的忽略位图）也并入不可用集合。

**顺序策略**——[src/BitfieldMan.cc:381-423](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BitfieldMan.cc#L381-L423)：L387-391 永远先给起始块；L393-402 若某空闲块的前一块「已完成且不在用」则直接可用；L404-415 否则向前累计连续空闲块直到凑够 `minSplitSize`。

**过滤位图**——[src/BitfieldMan.cc:714-725](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BitfieldMan.cc#L714-L725) 的 `addFilter(offset, length)` 把某个字节区间覆盖的块标记为「候选」，[L740-757](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BitfieldMan.cc#L740-L757) 的 `addNotFilter` 则相反（区间外全标记），后者正是 SegmentMan 实现「只在某个 FileEntry 范围内选段」的工具。

**性能缓存**——位图计数类查询（缺多少块、完成字节数）被缓存在 [src/BitfieldMan.cc:846-853](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BitfieldMan.cc#L846-L853) 的 `updateCache()`，只在位变化时重算，供进度条等高频消费者使用。

**Piece 的块级位图**——[src/Piece.cc:56-63](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.cc#L56-L63) 每个 Piece 内建一个 16K 块长的 BitfieldMan；[L67-71](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.cc#L67-L71) `completeBlock` 完成一块 = 完成位置 1、在用位清 0；[L130-153](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.cc#L130-L153) `getMissingUnusedBlockIndex` 给 BT 请求工厂批量领块并立刻标记在用。另外 [L194-212](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.cc#L194-L212) 的 `updateHash` 只接受**严格连续**的数据段（`begin == nextBegin_`）喂给哈希上下文——这就是 piece 分块校验必须按序到达的原因。

#### 4.1.4 代码实践

**实践目标**：验证你对稀疏策略「二分式散开」的理解。

1. 阅读测试 [test/BitfieldManTest.cc:306-330](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/BitfieldManTest.cc#L306-L330)：`BitfieldMan(1_m, 10_m)` 即 10 个 1M 块，`minSplitSize = 1_m`。
2. **先手算再对答案**：初始时稀疏策略选哪块？把块 0 标记「在用」后再选哪块？再把块 5 标记在用呢？（提示：全空区间 [0,10) 取头部 0；之后最大区间是 [1,10)，但 0 正在用，于是取中点；依此类推。）
3. 在已按 u1-l2 构建好的仓库里运行测试核对：`make check -C test`（测试可执行文件为 `test/aria2c`，由 [test/Makefile.am:3-4](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/Makefile.am#L3-L4) 定义），全套通过即包含 `testGetSparseMissingUnusedIndex`。

**需要观察的现象**：断言序列与你手算的下标序列一致。

**预期结果**：依次是 0 → 5 → 3 → 8。这正是 `-s4` 时 4 条连接拿到的 piece 分布——不排队、而是彼此远离地散开。测试运行结果以本地为准（若尚未编译项目，此步**待本地验证**）。

#### 4.1.5 小练习与答案

**练习 1**：一个 10MiB+1B 的文件，piece 长度 1M，位图需要几个字节？最后一块多长？

答案：块数 \(n = \lceil (10\cdot 2^{20}+1)/2^{20} \rceil = 11\)，字节数 \(\lceil 11/8 \rceil = 2\)；最后一块长度为 \(10\cdot 2^{20}+1 - 10\cdot 2^{20} = 1\) 字节（`getLastBlockLength` 的计算，测试 [test/BitfieldManTest.cc:80-89](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/BitfieldManTest.cc#L80-L89) 也验证了这一点）。

**练习 2**：`bitfield_` 与 `useBitfield_` 为什么必须分成两张，而不能只用一张「未完成」位图？

答案：一张位图无法区分「还没下」和「正被别的连接下载」。若不区分，第二条连接会再领到同一块，两条连接重复拉同样的数据还互相覆盖写位置。分成两张后，挑选条件是 `~bitfield & ~useBitfield`，天然排除了在途 piece。

**练习 3**：`-k` 选项是在哪一行代码里真正发挥作用的？

答案：在稀疏策略的闸门 [src/BitfieldMan.cc:286-288](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BitfieldMan.cc#L286-L288)（`(endIndex - startIndex) * blockLength >= minSplitSize`），以及顺序策略的 [L411](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BitfieldMan.cc#L411)。所以 `-k1M` 的含义是：只有 ≥1M 的连续空闲区才会被切给新连接。

### 4.2 Segment：piece 面向连接的视图

#### 4.2.1 概念说明

Piece 是全局的存储单元，但对一条 HTTP 连接来说，它关心的视角是：「我从文件的哪个字节开始写、已经写了多少、写到哪了」。`Segment` 就是这个视角的抽象——它不拥有数据，只是把一个 `Piece` 包装成「带写入游标的进度条」：

- `getPosition()`：本 segment 在整个下载中的起始偏移（`index × pieceLength`）。
- `getWrittenLength()`：已写入字节数（游标）。
- `getPositionToWrite()`：下一次写入的位置 = 起始偏移 + 游标。

两个实现：`PiecedSegment`（长度已知的普通下载/BT）和 `GrowSegment`（总长度未知时「边下边长」的单段下载，如无 Content-Length 的 HTTP 响应）。

#### 4.2.2 核心流程

`PiecedSegment` 的游标推进规则：

```
updateWrittenLength(bytes):
    newWrittenLength = writtenLength + bytes
    对每个被完整越过的 16K block:  piece.completeBlock(i)   # 块级位图置位
    若 newWrittenLength == piece.length:  完成最后一块
    writtenLength = newWrittenLength
```

注意游标是**只进不退**的：它假设数据按顺序连续到达（HTTP Range 响应正是如此）。领取 segment 时游标定位到「第一个缺失块」的边界：

```
构造 PiecedSegment:
    index = piece 中第一个缺失的 block
    writtenLength = index * 16K
```

这样断点续传时游标自动落在正确位置。

#### 4.2.3 源码精读

**接口定义**——[src/Segment.h:49-85](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Segment.h#L49-L85)：注意 `getLength()`（本 piece 实际长度，末块可能短）与 `getSegmentLength()`（标称 piece 长度）是两个概念；`operator==` 只比较 index（同一 piece 即同一 segment）。

**构造时定位游标**——[src/PiecedSegment.cc:41-49](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PiecedSegment.cc#L41-L49)：`getFirstMissingBlockIndexWithoutLock` 找第一个缺失块，游标设为 `index × blockLength`。

**位置换算**——[src/PiecedSegment.cc:57-65](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PiecedSegment.cc#L57-L65)：`getPosition() = index * pieceLength`，`getPositionToWrite()` 在其上加游标。HTTP 请求的 Range 起点、写盘偏移都取自这里。

**游标推进与块置位**——[src/PiecedSegment.cc:69-82](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PiecedSegment.cc#L69-L82)：对应上面伪代码；`DownloadCommand` 每读完一块数据调用一次。

**未知长度的生长段**——[src/GrowSegment.h:52-70](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/GrowSegment.h#L52-L70)：`complete()` 恒为 false、`getLength()` 恒为 0、index 恒为 0——整个下载只有一个从 0 开始无限生长的段，下载结束时由 `DownloadCommand::prepareForNextSegment` 把已写字节数回填给 FileEntry（见 [src/DownloadCommand.cc:325-329](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L325-L329)）。

#### 4.2.4 代码实践

**实践目标**：在真实下载中观察 segment 的游标初始化。

1. 用调试日志下载一个文件：
   `./src/aria2c -x2 -s2 --log=seg.log --log-level=debug --console-log-level=info <某个HTTP大文件URL>`
2. 在日志中搜索 `Attach segment`（由 [src/SegmentMan.cc:163-167](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L163-L167) 打印），每行形如 `index=N, length=..., segmentLength=..., writtenLength=...`。
3. 对比两条连接各自 segment 的 `index` 与 `writtenLength`：新任务都应为 0；若先 Ctrl-C 中断再用 `-c` 续传，重跑后 `writtenLength` 会非 0，且正好等于 16K 的整数倍（游标落在块边界，原因见 4.2.2 构造规则）。

**需要观察的现象**：续传后各 segment 的初始 `writtenLength` 是 16K 的倍数，且不同连接的 `index` 互相错开。

**预期结果**：与 PiecedSegment 构造函数 `writtenLength = index * blockLength` 一致。具体日志内容**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `PiecedSegment::updateWrittenLength` 里要单独处理 `newWrittenLength == piece_->getLength()` 的最后一块？

答案：整除边界问题。设 piece 长 1M、块长 16K，恰好 64 块，循环条件 `i < newWrittenLength / blockLength` 在恰好写满时循环 64 次本可覆盖；但当长度不是块长整数倍（末 piece），最后一段不足 16K，除法向下取整会漏掉它，所以 [L78-80](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PiecedSegment.cc#L78-L80) 显式补上最后一块。

**练习 2**：`Segment::operator==` 只比较 index，这蕴含了什么约定？

答案：一个 piece 同时最多被一个「segment 视图」持有（HTTP 场景）；`SegmentMan::cancelSegment(cuid, segment)` 正是靠这个语义在登记表中按 `(cuid, index)` 定位要归还的条目（[src/SegmentMan.cc:326-340](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L326-L340)）。BT 场景一个 piece 可被多个 peer 同时传，但那是通过 Piece 的 `users_` 列表与块级在用位图协调的，不经过 Segment。

### 4.3 SegmentMan：分配、归还与完成

#### 4.3.1 概念说明

`SegmentMan` 是「segment 台账」：一张 `usedSegmentEntries_` 登记表记录哪个 cuid 领了哪些 segment，外加三样附属设施——

1. `ignoreBitfield_`：按文件排除某些 piece（`--select-file` / 多文件任务的文件归属）；
2. `segmentWrittenLengthMemo_`：归还 segment 时记住「当时写到多少」，下次重领时恢复游标；
3. `peerStats_ / fastestPeerStats_`：每条连接的测速与各服务器最快画像（喂给 ServerStat）。

SegmentMan 自身不做挑选算法，它把「选哪块」委托给 `PieceStorage`（进而委托给 4.1 的 BitfieldMan），自己负责**记账与生命周期**。

#### 4.3.2 核心流程

一次 HTTP 下载中 segment 的完整生命周期：

```
命令就绪 (AbstractCommand::execute, shouldProcess 分支)
   │ 计算 minSplitSize = calculateMinSplitSize()
   │     ├─ HTTP pipeline 启用 → pieceLength
   │     └─ 否则 → PREF_MIN_SPLIT_SIZE (-k)
   ▼
SegmentMan::getSegment(cuid, minSplitSize)          ← 单文件路径
   └→ PieceStorage::getMissingPiece(minSplitSize, ignoreBitfield…)
        └→ StreamPieceSelector::select → BitfieldMan 挑 index
        └→ checkOutPiece(index): useBit 置位、找/建 Piece、addUser(cuid)
   └→ SegmentMan::checkoutSegment: 建 PiecedSegment/GrowSegment、查 memo 恢复游标、登记
   ▼
DownloadCommand 循环读数据 → segment->updateWrittenLength → 写盘
   ▼
segment 写满 → completeSegment: completePiece(全局位图置位) + 从登记表摘除
   ▼ (keep-alive 复用连接时)
prepareForNextSegment: getSegmentWithIndex(index+1)
   └→ 抢不到就 getCleanSegmentIfOwnerIsIdle(index+1)：邻居空闲且游标为 0 → 抢占
   └→ 还抢不到 → prepareForRetry(0) 归还重试
   ▼
出错/超时 → cancelSegment: flush 写缓存、cancelPiece、memo 记下 writtenLength
```

多文件路径（`getSegment(vector, …, fileEntry, maxSegments)`）在整体流程前多一步：用 `addNotFilter(fileEntry.offset, length)` 把该文件范围外的 piece 全部屏蔽，领到不属于本文件的跨界 piece 会被立即取消归还（跨界 piece 可能同时覆盖两个文件的字节）。

#### 4.3.3 源码精读

**命令侧入口**——[src/AbstractCommand.cc:258-303](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L258-L303)：单文件下载走 [L263-271](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L263-L271) 循环领段（上限 `maxSegments` = HTTP pipeline 深度，无请求时为 1）；领不到段且所有段被忽略则直接退出（L279-285），否则 `prepareForRetry` 稍后再来。多文件走 [L292-297](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L292-L297) 的带 FileEntry 版本。

**minSplitSize 的来源**——[src/AbstractCommand.cc:888-895](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L888-L895)：HTTP pipelining 开启时用 piece 长度（每段恰好一个 piece），否则用 `-k`。

**分配主路径**——[src/SegmentMan.cc:203-210](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L203-L210)：一行委托 `pieceStorage_->getMissingPiece(minSplitSize, ignoreBitfield_.getFilterBitfield(), …, cuid)`；PieceStorage 侧 [src/DefaultPieceStorage.cc:386-399](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L386-L399) 调选择器拿 index，再由 [L119-143](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L119-L143) 的 `checkOutPiece` 完成出库：全局 useBit 置位、复用或新建 `Piece`、`addUser(cuid)`、挂接写盘缓存。默认选择器就是稀疏策略（[src/DefaultPieceStorage.cc:99-115](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L99-L115) 按 `--stream-piece-selector` 分派，`default` → [src/DefaultStreamPieceSelector.cc:47-51](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultStreamPieceSelector.cc#L47-L51)）。

**checkoutSegment：包装与记忆量恢复**——[src/SegmentMan.cc:134-188](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L134-L188)：

- [L143-150](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L143-L150)：若 piece 有写盘缓存先强制落盘——同一 piece 可能同时有 BT peer 在写，缓存里的脏数据会互相覆盖。
- [L152-160](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L152-L160)：piece 长度为 0（未知长度任务）建 `GrowSegment`，否则建 `PiecedSegment`，登记进 `usedSegmentEntries_`。
- [L169-186](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L169-L186)：查 `segmentWrittenLengthMemo_`——若记忆的写入量比块位图推出来的多、且差值不足一个 block，就把游标直接补上去。原因：取消时数据可能已写盘但没凑满一个 16K 块（位图没置位），不恢复就会重下这「半块」。

**按 index 领段与抢占**——`getSegmentWithIndex`（[src/SegmentMan.cc:245-252](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L245-L252)）供 keep-alive 连接**顺序**领下一段（Range 起点可预测）；`getCleanSegmentIfOwnerIsIdle`（[L254-283](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L254-L283)）是它的补充：目标段若被一条**空闲**（NetStat::IDLE）连接占着且尚未写任何字节，就先 `cancelSegment(owner)` 再接管——避免慢连接囤着下一段不动。调用方在 [src/DownloadCommand.cc:358-369](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L358-L369)：先试 `index+1`，再试抢占；若抢到的段 `writtenLength > 0` 则宁可放弃复用（把数据写到别人写过一半的位置会损坏文件），走 `prepareForRetry(0)`。

**归还与记忆量写入**——`cancelSegmentInternal`（[src/SegmentMan.cc:285-309](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L285-L309)）：同样先 flush 缓存，再 `pieceStorage_->cancelPiece`（清 useBit、从 users 移除），最后把 `writtenLength` 存进 memo。两个外层重载分别按 cuid 批量取消（[L311-324](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L311-L324)，命令重试/退出时用）与按 (cuid, segment) 精确取消。

**完成**——`completeSegment`（[src/SegmentMan.cc:373-388](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L373-L388)）：`completePiece` 在全局位图置位，`advertisePiece` 把「我有这块了」登记给 BT 的 have 广播机制（HTTP 下载回哺 BT 的关键钩子，u1-l1 提过），最后从登记表摘除。分块校验失败时则相反：[src/DownloadCommand.cc:383-401](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L383-L401) `validatePieceHash` 失败 → `segment->clear()`（清块位图）→ `cancelSegment(cuid)` → 抛重试异常。

**忽略位图与文件选择**——构造函数 [src/SegmentMan.cc:68-76](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L68-L76) 以 piece 长度与总长建 `ignoreBitfield_` 并启用过滤；`ignoreSegmentFor/recognizeSegmentFor`（[L469-482](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L469-L482)）增删某个 FileEntry 覆盖的块；`allSegmentsIgnored`（[L484-487](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L484-L487)）为真说明无 URI 可用，命令直接收摊。

**PeerStat → ServerStat：数据怎么流**——

1. 每条连接注册一个 `PeerStat`（[src/SegmentMan.cc:405-408](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L405-L408)），`updateFastestPeerStat`（[L439-456](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L439-L456)）按「主机名+协议」维护每台服务器最快的连接统计，替换时把被替者的会话流量并入替者，保证速度均值不突变。
2. 任务结束后 `RequestGroupMan::collectStat`（[src/RequestGroupMan.cc:313-341](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L313-L341)）把 `getFastestPeerStats()` 的平均速度写进 `ServerStat`：整组只有 1 条连接时更新「单连接均值」，否则更新「多连接均值」（[src/ServerStat.cc:87-113](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ServerStat.cc#L87-L113)，前 5 次按样本数加权，之后固定为指数滑动平均 \(\bar{v}_t = \frac{4}{5}\bar{v}_{t-1} + \frac{1}{5}v_t\)，若骤降超过 20% 还会清零计数器重新学习）。
3. 下一次下载时 [src/AbstractCommand.cc:233-248](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L233-L248) 每 10 秒调用 `FileEntry::findFasterRequest`（[src/FileEntry.cc:265-311](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FileEntry.cc#L265-L311)）：在备用 URI 里找 ServerStat 速度显著更快（> 当前 1.5 倍或 > 20K 阈值）且未占满 `-x` 名额的主机，主动换源。

#### 4.3.4 代码实践

**实践目标**：用一条命令看清 4 条连接各自负责的字节区间。

1. 准备一个 ≥20M 的 HTTP 直链文件，执行：
   `./src/aria2c -x4 -s4 -k1M --log=seg.log --log-level=debug --console-log-level=info -o big.bin <URL>`
2. 下载中按一次 Ctrl-C 优雅停止（`globalHaltRequested` 状态机，u1-l3），让 aria2 保存 `.aria2` 控制文件后退出。
3. 解析控制文件位图。文件布局由 [src/DefaultBtProgressInfoFile.cc:111-191](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtProgressInfoFile.cc#L111-L191) 定义，依次是：版本(2B) + 扩展位(4B) + infoHash 长度(4B，HTTP 下载为 0) + pieceLength(4B 网络序) + totalLength(8B) + uploadLength(8B) + **bitfieldLength(4B)** + **bitfield 本体**。即位图从偏移 34 字节处开始，长度为 \(\lceil \lceil L/p \rceil / 8 \rceil\) 字节：
   ```bash
   # 示例命令：跳过 34 字节头部，导出位图部分（字节数按你的文件大小计算）
   od -Ad -tu1 -j 34 -N <bitfieldLength> big.bin.aria2
   ```
4. 把每个字节的 8 个位按「高位在前」展开成 0/1 串，对照日志：`grep "Attach segment" seg.log` 里 4 条 CUID 各自领到的 index 序列，验证位图中为 1 的位是否与日志吻合。

**需要观察的现象**：位图不是前缀连续的 111...1000...0，而是散布着已完成的位——4 条连接从文件的不同区域各自推进。

**预期结果**：例如 20M 文件（20 个 1M piece，位图 3 字节），日志里 4 条连接的首批 index 大致是 0、10、5、15 一类的「对半散开」序列；Ctrl-C 时刻位图中的 1 正是这些连接各自完成的 piece。`od` 输出与日志对得上即验证成功（具体值**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `checkoutSegment` 与 `cancelSegmentInternal` 都要先 `flushWrDiskCache`？

答案：piece 的数据可能还在 `--disk-cache` 的内存里没落盘。segment 的取消/重领意味着写这个 piece 的主体要变（比如从 HTTP 连接换成 BT peer，或换一条 HTTP 连接），两个写入者的缓存视图不一致就会把对方的数据冲掉。先 flush 保证磁盘上是最新状态，新的持有者从磁盘事实出发。见 [src/SegmentMan.cc:143-150](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L143-L150) 与 [L293-302](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L293-L302)。

**练习 2**：keep-alive 场景下，连接想顺序领 `index+1` 的段却领不到，什么时候会成功走「抢占」分支？

答案：仅当该段已被登记、当前持有者连接的 PeerStat 处于 `NetStat::IDLE`（一段时间没有数据到达）、且该段 `writtenLength == 0`（一个字节都没写）时，`getCleanSegmentIfOwnerIsIdle` 才会取消原持有者并把段交给请求方（[src/SegmentMan.cc:254-283](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L254-L283)）。游标非 0 时宁可不抢——复用连接会把数据写到固定的 Range 起点，与已写部分错位就会损坏文件（[src/DownloadCommand.cc:365-370](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L365-L370) 注释原文说明了这一点）。

**练习 3**：`segmentWrittenLengthMemo_` 的恢复条件为什么限制「差值小于一个 block」？

答案：memo 记录的是字节级进度，而块级位图只认整块。差值小于一个 block 说明只是「最后半块写盘了但位图没置位」，直接把游标补上即可，不会跳过任何未写数据；若差值大于一个 block，说明两个数据源对进度的认知已经严重分叉（例如文件被外部修改、校验失败清空了块），此时宁可从头重写这个 piece 也不能盲目信任 memo（[src/SegmentMan.cc:176-185](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L176-L185)）。

### 4.4 RequestSlot：BT 的在途请求账本

#### 4.4.1 概念说明

HTTP 下载里「一条连接持有整个 segment」；BT 下载里粒度更细——一个 piece 被切成 16K 的 block，向**每个 peer** 分别请求若干 block，这些「已发出但还没收到数据」的请求就是 **outstanding request**。`RequestSlot` 是其中一个请求的记账条目：请求哪个 piece 的哪个 block、何时发出。`DefaultBtMessageDispatcher` 维护一个 `requestSlots_` 队列，就是这本账。

它与 SegmentMan 的分工：SegmentMan/BitfieldMan 决定「要下哪些 piece」（macro 层），RequestSlot 记录「向哪个 peer 要了哪些 block、要了多久」（micro 层）。账本上的 slot 在 piece 的块级 `useBitfield` 上占位，防止重复请求；对账（收到 piece 消息）或销账（超时/choke）时释放占位。

#### 4.4.2 核心流程

```
BtRequestMessage::onQueued (请求即将发出)
   └→ addOutstandingRequest( RequestSlot(index, begin, length, blockIndex, piece) )
        # piece 的块级 useBit 已在请求工厂选块时置位

每个交互 tick (DefaultBtInteractive):
   checkRequestSlotAndDoNecessaryThing:
     for slot in requestSlots_:
       slot 超时 (--bt-request-timeout, 默认60s)
          → piece->cancelBlock(blockIndex)   # 释放块占位
          → peer->snubbing(true)             # 标记该 peer「磨蹭」
       slot 的 block 已从别处获得
          → 向该 peer 发 cancel 消息（别再发了）
     清理上述两类 slot

收到 BtPieceMessage:
   getOutstandingRequest(index, begin, length) 对账 → removeOutstandingRequest

收到 choke 消息:
   doChokedAction → 对 peer 不允许的 piece 的所有 slot 取消占位并出账
```

#### 4.4.3 源码精读

**账目条目**——[src/RequestSlot.h:45-60](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestSlot.h#L45-L60)：五个字段 `index_`（piece）、`begin_/length_`（piece 内偏移与长度，即 block）、`blockIndex_`、`dispatchedTime_`（发出时刻）。[L67-86](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestSlot.h#L67-L86) 定义了按 `(index, begin, length)` 的相等与全序比较——相等用于对账，全序让 slot 可排序去重。[L114-119](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestSlot.h#L114-L119) 解释了为何缓存 `piece_` 共享指针：对账时本要反复 `PieceStorage::getPiece()` 查找，缓存省掉约 1.7% 的处理时间。

**超时判定**——[src/RequestSlot.cc:39-42](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestSlot.cc#L39-L42)：发出时刻距当前墙钟超过阈值即超时；阈值由 [src/PeerInteractionCommand.cc:183](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L183) 从 `--bt-request-timeout` 选项（默认 60 秒、范围 1-600，[src/OptionHandlerFactory.cc:1633-1637](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L1633-L1637)）注入。

**记账**——[src/BtRequestMessage.cc:81-86](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtRequestMessage.cc#L81-L86)：request 消息入队时创建 slot 并追加到账本尾部（[src/DefaultBtMessageDispatcher.cc:286-290](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageDispatcher.cc#L286-L290)）。

**周期对账**——[src/DefaultBtMessageDispatcher.cc:215-242](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageDispatcher.cc#L215-L242)（见上面流程图注释）：注意两种清理的后续动作不同——超时是**单方面止损**（释放块、标记 peer snubbing，choke 优化器随后会降低它的优先级）；block 已获得则是**礼貌通知**（发 cancel 消息，因为 peer 可能已经在上传路上）。该方法由交互主循环 [src/DefaultBtInteractive.cc:568](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L568) 每 tick 调用一次。

**choke 清账**——[src/DefaultBtMessageDispatcher.cc:184-202](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageDispatcher.cc#L184-L202)：被 choke 后，对方不再给我它「不允许集合」之外的 piece，相关 slot 全部 `cancelBlock` 释放占位并出账；这些块随后可向其他 peer 重新请求。

**对账查询**——[src/DefaultBtMessageDispatcher.cc:260-271](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageDispatcher.cc#L260-L271)：收到 piece 数据时按三元组 `(index, begin, length)` 找到对应 slot；找不到说明是「不请自来」的数据（比如 cancel 没赶上），[src/BtPieceMessage.cc:149](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtPieceMessage.cc#L149) 会记日志并丢弃。

#### 4.4.4 代码实践

**实践目标**：观察一次 BT 下载中在途请求的建立与超时清理。

1. 下载任一种子并开调试日志：
   `./src/aria2c --bt-request-timeout=5 --log=bt.log --log-level=debug --console-log-level=info -o test.torrent <某个种子>`
   把超时阈值人为调低到 5 秒，更容易触发清理分支。
2. 在日志中检索 `Deleting request slot` 相关消息（来自 [src/DefaultBtMessageDispatcher.cc:219-232](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageDispatcher.cc#L219-L232) 的三条日志，按触发原因分别为 timeout / already acquired / choked）。
3. 统计三种 `Deleting request slot` 各出现多少次，并挑一条 timeout 记录，按 `index=... begin=...` 反推它请求的是该 piece 内第几个 16K 块（`begin / 16384`）。

**需要观察的现象**：正常网络下多为 `already acquired` 类清理（重复请求被别的 peer 先满足）；人为调低超时后 `timeout` 类清理明显增多，且伴随该 peer 的下载停滞。

**预期结果**：能从日志还原「某 peer 的某 block 请求在 5 秒内未兑现而被释放」的事件序列（具体日志**待本地验证**）。

#### 4.4.5 小练习与答案

**练习 1**：RequestSlot 的 `operator==` 为什么不含 `blockIndex_` 与 `dispatchedTime_`？

答案：相等语义服务于**对账**：BT piece 消息只携带 `(index, begin, length)` 三个字段，对账时只能按这三元组匹配（[src/DefaultBtMessageDispatcher.cc:264-266](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageDispatcher.cc#L264-L266)）。`blockIndex_` 可由三元组推导（缓存而已），`dispatchedTime_` 是本地记账属性、与协议无关。

**练习 2**：超时清理时为什么要 `peer_->snubbing(true)`，而不只是删掉 slot？

答案：删 slot 只回收了这一个 block 的占位，但「这个 peer 响应很慢」的信息会丢失。`snubbing` 标记把信息传给后续的 choke/unchoke 决策（u6-l4 的交互循环），让慢 peer 在乐观阻塞竞争中吃亏，把上传带宽让给响应快的 peer。见 [src/DefaultBtMessageDispatcher.cc:223-224](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageDispatcher.cc#L223-L224)。

**练习 3**：HTTP 下载需要 RequestSlot 吗？

答案：不需要。HTTP 一条连接持有整个 segment 且 TCP 本身保证顺序交付，`Segment::getWrittenLength()` 就是进度；BT 中一个 piece 的 block 来自多个 peer、乱序到达，必须有一本独立的在途账本来防止重复请求和检测赖账，这正是 RequestSlot 存在的理由。

## 5. 综合实践

**任务：亲手画出一台机器上 4 条连接的分段作战地图。**

1. 选一个 50M 左右的 HTTP 直链文件，用 4.3.4 的命令下载（`-x4 -s4 -k1M`，debug 日志）。
2. 从日志提取三组事件：`Attach segment#N to CUID#M`（分配）、`Canceling segment#N`（归还）、以及每条 CUID 的 `Attach` 时间顺序，整理成下表（示例格式，非真实数据）：

   | CUID | 领取 piece 的先后顺序（index） | 对应字节区间（index×1M 起） |
   | --- | --- | --- |
   | #11 | 0, 4, 9, … | 0M-, 4M-, 9M- |

3. 在下载进行到一半时 Ctrl-C，用 4.3.4 第 3 步的方法解析 `big.bin.aria2` 的位图，把 1 的位涂黑，得到「当时哪些 piece 已完成」的条带图。
4. 把三样东西叠起来回答：稀疏策略让 4 条连接的起始 index 呈什么样的空间分布？为什么几乎不会出现两条连接长时间写同一个 piece？`Canceling segment` 之后同一 index 被重新 `Attach` 时，memo 机制如何体现在日志的 `writtenLength(in memo)` 行（[src/SegmentMan.cc:173-175](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L173-L175)）里？
5. 附加题：把 `--stream-piece-selector=inorder` 加上重跑一遍，对比 4 条连接起始 index 的分布差异（顺序策略会偏向文件头部与紧邻已完成区的位置）。

**验收标准**：你能指着图说出任意一个 1 位的 piece 是哪条连接、在哪个分配事件之后完成的；并能解释位图中「洞」的成因（该 piece 的持有连接被取消或尚未写完）。

## 6. 本讲小结

- **两张尺子**：piece（HTTP 默认 1M/BT 由种子决定）是调度与校验的单位，block（16K）是 BT 请求与写入记账的单位；`BitfieldMan` 用完成/在用/过滤三张位图统一回答「哪些块可用」。
- **算法与记账分离**：挑选算法（sparse/inorder/geom/random 与 `minSplitSize` 闸门）在 BitfieldMan/StreamPieceSelector，生命周期管理（登记、归还、恢复游标、忽略范围）在 SegmentMan，piece 出库入账在 PieceStorage。
- **默认稀疏策略让多连接自动散开**：取最大连续空闲区间、邻居在用则改从中点起，`-k` 保证新段不小于 minSplitSize——这就是 `-s4` 时 4 条连接均匀瓜分文件的原因。
- **segment 三个出口**：`completeSegment`（全局位图置位并广播 have）、`cancelSegment`（归还并把 writtenLength 存入 memo，重领时不足一块的差额直接补游标）、被 `getCleanSegmentIfOwnerIsIdle` 抢占（仅当持有者空闲且未写一字节）。
- **RequestSlot 是 BT 的在途请求账本**：`(index, begin, length)` 三元组对账，超时释放块并标记 snubbing，choke 时批量清账。
- **测速数据向前流动**：SegmentMan 的 PeerStat → 每主机最快统计 → ServerStat（EWMA）→ 下次下载的 `findFasterRequest` 换源决策。

## 7. 下一步学习建议

- 下一讲 u3-l5 讲速度统计与限速，其中 SpeedCalc 正是本讲 PeerStat 的内核，建议先回头看 [src/PeerStat.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerStat.h) 再学 [src/SpeedCalc.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SpeedCalc.cc)。
- PieceStorage 的内部结构（usedPieces、RarestPieceSelector、end-game）本讲只碰到 `checkOutPiece/completePiece` 两个入口，u5-l2 会完整展开；`.aria2` 控制文件的加载路径则在 u6-l11 与 BT 会话恢复一起讲。
- 想继续 BT 方向的读者，本讲 4.4 的 RequestSlot 是 u6-l5（消息分发）与 u6-l6（请求流水线 DefaultBtRequestFactory）的前置，建议按序阅读。
- 动手方向：给 `SegmentMan::checkoutSegment` 的 memo 恢复分支加一行计数日志，跑 BT 与 HTTP 各一次，观察哪类下载更容易触发「半块恢复」，并思考为什么。
