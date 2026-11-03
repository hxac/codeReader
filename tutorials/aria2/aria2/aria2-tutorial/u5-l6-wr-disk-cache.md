# 写盘缓存：WrDiskCache 的块管理

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `--disk-cache` 选项背后的完整实现链条：`RequestGroupMan` 持有全局 `WrDiskCache`，每个在途 piece 挂一个 `WrDiskCacheEntry`，数据以 `DataCell` 为单位缓存在内存。
2. 说清 `WrDiskCacheEntry` 的块组织方式（按全局偏移 `goff` 排序的 `DataCell` 集合、尾部连续追加、≥4KiB 容量预留）与两种销毁路径（`writeToDisk` 落盘 / `clear` 丢弃）。
3. 推演 `WrDiskCache` 的容量淘汰算法：为什么 `ensureLimit` 总是先冲刷「缓存字节最大、其次最久未更新」的条目，以及 `std::set` 键值不可变约定如何逼出「先 erase 再改键再 insert」的更新舞步。
4. 列出缓存数据被 flush 到磁盘的全部五个时机，以及写盘失败时「记错误码 → 清块 → 重下」的容错链。
5. 通过日志与（可选的）插桩统计，定量对比缓存开/关时写盘调用次数与写出顺序的差异。

## 2. 前置知识

本讲建立在 u5-l2（PieceStorage）之上，复习三个概念即可：

- **piece 与全局偏移 goff**：aria2 把总字节流按 piece 切分（HTTP 默认 1MiB、BT 由种子给定）；`goff` 是数据在整个下载任务中的全局字节偏移。`DefaultPieceStorage` 之上的所有写入最终都以 `(数据, goff)` 的形式交给 `DiskAdaptor`（见 u5-l3）。
- **在途 piece**：只有正在被某条连接下载的 piece 才有 `Piece` 运行时对象；piece 完成后对象销毁，全局位图里只留一个 bit（u5-l2 的结论）。本讲的缓存条目正是寄生在 `Piece` 对象上的。
- **写盘为什么要优化**：一条 HTTP 连接每轮最多搬运 16KiB（`SocketRecvBuffer` 容量），一个 BT piece 消息最大 64KiB 块（`BtConstants.h` 的 `MAX_BLOCK_LENGTH`）。多连接并行时，磁盘收到的是散落在各处偏移上的小块写——机械硬盘会疯狂寻道，写放大也高。`--disk-cache` 的思路就是：**先把小块攒在内存，凑成按偏移有序的一批，再一次性按序写出**。官方帮助文本（`src/usage_text.h:992-1004`）概括了两个收益：数据以更大的单位、按文件偏移重排后写出；校验哈希时若数据还在内存，无需从磁盘读回。

另外需要一个 C++ 知识点：`std::set` 的元素按比较器排序后**键不可变**——直接修改元素会破坏排序。本讲会看到 `WrDiskCache::update` 为此专门做「erase → 改键 → insert」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/WrDiskCacheEntry.h/.cc` | 单个 piece 的写缓冲区：`DataCell` 结构、按 `goff` 排序的数据块集合、追加/落盘/丢弃 |
| `src/WrDiskCache.h/.cc` | 全局缓存容器：容量记账、按「大且陈旧优先」淘汰、逻辑时钟 |
| `src/Piece.cc`（部分） | `Piece` 与缓存的粘合层：`initWrCache/flushWrCache/updateWrCache/appendWrCache/releaseWrCache` 与免回读校验 `getDigestWithWrCache` |
| `src/DefaultPieceStorage.cc`（部分） | 集成点：领取 piece 时创建缓存条目、释放 piece 时注销、全量 flush 接口 |
| `src/SinkStreamFilter.cc` | HTTP/FTP 流式数据的写入路径：先追加缓存、不够再建新块 |
| `src/BtPieceMessage.cc`（部分） | BT piece 消息的写入路径与 piece 完成时的 flush |
| `src/RequestGroupMan.cc` / `src/DownloadEngineFactory.cc` | 缓存的创建入口：按 `--disk-cache` 值构造全局唯一实例 |
| `src/RequestGroup.cc` / `src/SegmentMan.cc` / `src/DownloadCommand.cc`（部分） | 各个 flush 触发点 |
| `test/WrDiskCacheTest.cc` / `test/WrDiskCacheEntryTest.cc` | 单元测试，是本讲实践的主要实验台 |

## 4. 核心概念与源码讲解

### 4.1 WrDiskCacheEntry：单个 piece 的写缓冲区

#### 4.1.1 概念说明

`WrDiskCacheEntry` 回答的问题是：**一个尚未写满的 piece，它的已到数据放在内存里怎么组织？**

最朴素的做法是给每个 piece 开一整块 `pieceLength` 大小的缓冲。但 HTTP 分段下载通常顺序填充，BT 则按 16/64KiB 的块随机到达——按块记录、写到磁盘前再按偏移排序，比整块预留省内存且天然支持乱序。于是 aria2 选择用一组小的 `DataCell` 来装数据，一个 entry 就是「一个 piece 的所有已到数据块」。

注意它与 u5-l1 的 `DiskWriter` 分层不同：`WrDiskCacheEntry` 不直接碰文件，它持有 `DiskAdaptor` 的引用，flush 时把每个 cell 交给 `DiskAdaptor::writeCache`，由后者翻译成物理文件写入（单文件直写、多文件按 u5-l3 的映射切分）。

#### 4.1.2 核心流程

```
DataCell { goff, data, offset, len, capacity }
   │  data 指向 new[] 出的裸缓冲：
   │  有效数据 = data[offset, offset+len)
   │  可写余量 = capacity - len
   ▼
DataCellSet = std::set<DataCell*, 按 goff 升序>
   │
   ├─ cacheData(cell)：新块按 goff 插入，size_ += len
   ├─ append(goff, data, len)：仅当 goff 恰好等于「最后一个块的结尾」时
   │      原地 memcpy 追加 min(capacity-len, len) 字节（零新分配）
   ├─ writeToDisk()：按 goff 升序逐块交给 diskAdaptor_->writeCache(this)
   │      然后无论成败都 deleteDataCells()（释放内存、size_ 归零）
   └─ clear()：直接 deleteDataCells()，数据丢弃不落盘
```

有效容量利用率的直觉：追加能拷贝的字节数是

\[ \text{appendLen} = \min(\text{capacity} - \text{len},\ \text{len}_{in}) \]

`capacity` 在创建时预留（HTTP 路径至少 4KiB），所以零散小数据能持续吃掉余量；一旦余量耗尽或新数据与尾部不连续，就另开新块。

#### 4.1.3 源码精读

DataCell 的结构定义——`goff` 是数据在 `DiskAdaptor` 逻辑空间中的落点，`data/offset/len/capacity` 四个字段描述一块「带前导空隙的缓冲」：

- [src/WrDiskCacheEntry.h:53-64](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WrDiskCacheEntry.h#L53-L64) — `DataCell` 以 `goff` 作为集合排序键（`operator<` 只比较 `goff`），注释明确了「`data` 必须有 `len+offset` 字节、缓存数据是 `[data+offset, data+offset+len)`」的内存约定。

块集合与错误状态：

- [src/WrDiskCacheEntry.h:66](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WrDiskCacheEntry.h#L66) — `DataCellSet` 是按 `goff` 升序的指针集合（`DerefLess` 解引用比较，见 [src/a2functional.h:124-126](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/a2functional.h#L124-L126)）。**升序是本讲一切「按偏移写出」收益的根源。**
- [src/WrDiskCacheEntry.h:94-97](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WrDiskCacheEntry.h#L94-L97) — `CACHE_ERR_SUCCESS/CACHE_ERR_ERROR` 与保存的 `error_code`，供调用方在 flush 之后检查。

内存回收与两条销毁路径：

- [src/WrDiskCacheEntry.cc:67-75](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WrDiskCacheEntry.cc#L67-L75) — `deleteDataCells()` 逐块 `delete[] data; delete cell` 并清零 `size_`：**entry 拥有 cell 缓冲的所有权**（HTTP/BT 写入方 `new[]` 出来后就移交了）。
- [src/WrDiskCacheEntry.cc:77-88](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WrDiskCacheEntry.cc#L77-L88) — `writeToDisk()` 把整包数据交给 `diskAdaptor_->writeCache(this)`；若抛出 `RecoverableException`，只记录 `error_/errorCode_` 不向外抛，**无论成败最后都释放缓存**。错误上交延迟给调用方判定。
- [src/WrDiskCacheEntry.cc:90-103](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WrDiskCacheEntry.cc#L90-L103) — `clear()` 丢弃数据；`cacheData()` 插入新块并累加 `size_`。

尾部连续追加：

- [src/WrDiskCacheEntry.cc:105-123](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WrDiskCacheEntry.cc#L105-L123) — `append()` 只看集合的**最后一个块**（`goff` 最大者）：若 `末块.goff + 末块.len == goff`（字节级连续），就往余量里 `memcpy`；否则返回 0 由调用方另开新块。这就是「顺序到达的数据不产生新分配」的分支。

落盘的下游——两种 DiskAdaptor 的 `writeCache` 实现完全同构：按集合迭代顺序（即 `goff` 升序）逐块 `writeData`：

- [src/AbstractSingleDiskAdaptor.cc:97-104](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractSingleDiskAdaptor.cc#L97-L104) — 单文件：每块一次定位写，并打出 `Cache flush goff=...` 调试日志（实践环节会用到这行日志）。
- [src/MultiDiskAdaptor.cc:413-420](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiDiskAdaptor.cc#L413-L420) — 多文件：同样逐块写，跨文件切分由 `writeData` 内部处理。

#### 4.1.4 代码实践：读测试推演缓冲行为

1. **实践目标**：不运行任何程序，仅凭 4.1.3 的源码推演出单元测试的断言值，验证你读懂了 `DataCell` 的内存约定与 `append` 的连续性判定。
2. **操作步骤**：
   - 阅读 [test/WrDiskCacheEntryTest.cc:40-48](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/WrDiskCacheEntryTest.cc#L40-L48) 的 `testWriteToDisk`：先缓存 `createDataCell(0, "??01234567", 2)`（即 `goff=0`、有效数据为偏移 2 之后的 `"01234567"`）和 `createDataCell(8, "890")`，再 `writeToDisk()`。测试借用 `ByteArrayDiskWriter`（内存写入器）断言最终字符串是 `"01234567" + "890"`。
   - 再阅读 [test/WrDiskCacheEntryTest.cc:50-69](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/WrDiskCacheEntryTest.cc#L50-L69) 的 `testAppend`：手工构造 `offset=2, len=3("foo"), capacity=6` 的块，然后调用 `e.append(3, "barbaz", 6)`。
3. **需要观察的现象**（纸面推演）：
   - `testWriteToDisk` 中两块分别落在 `goff=0` 与 `goff=8`，正好首尾相接拼成 `"01234567890"`；`??` 前导两个字节被 `offset` 跳过。
   - `testAppend` 中末块结尾是 `0+3=3`，与传入 `goff=3` 连续，故可追加；追加量 = `min(6-3, 6) = 3`，只有 `"bar"` 拷入，`"baz"` 被截断留给下一个 DataCell；之后 `e.append(7, "FOO", 3)` 因末块结尾已变成 `0+6=6 ≠ 7` 而返回 0。
4. **预期结果**：你的推演与测试断言（返回 3、`cell->len==6`、`e.getSize()==6`；再次 append 返回 0）逐条一致。
5. 若想实际验证，可在构建后运行单个测试套件：`./src/aria2c --version` 确认构建成功后，在构建目录执行 `make check TESTS=''` 过滤方式不适用时，可直接运行 `test/aria2Tests`（CppUnit 可执行文件）；具体过滤参数**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `DataCell` 要同时有 `offset` 和 `capacity` 两个字段？只用 `len` 不行吗？

**答案**：`offset` 让一块缓冲可以携带「前导无效字节」，写入方可以把接收缓冲的任意子区间直接包装成 cell 而不必先搬移数据；`capacity` 记录 `data+offset` 之后还有多少可写空间，是 `append()` 判断「能否原地追加、能追加多少」的依据。只有 `len` 的话，每次缓存都必须精确分配或先做一次紧凑拷贝，失去了零拷贝包装与余量复用两个优化。

**练习 2**：`writeToDisk()` 在捕获异常后为什么仍然调用 `deleteDataCells()` 把数据丢掉，而不是保留待重试？

**答案**：因为调用方约定的失败恢复粒度是「整 piece 重下」：`error_` 被置为 `CACHE_ERR_ERROR` 后，上层（见 4.3.4 的三个检查点）会清掉该 piece 所有块并抛出下载失败异常，piece 数据本来就要作废重传；保留脏数据反而占内存且可能与重传数据交叠。缓存是「尽力加速」层，不是「可靠存储」层——可靠性由 piece 哈希校验保证。

**练习 3**：两个 `DataCell` 的 `goff` 分别是 100（len=50）和 130（len=10），集合里它们如何排列？`append(150, ...)` 会成功吗？

**答案**：集合按 `goff` 升序，130 在后。`append` 检查的是末块（130+10=140 ≠ 150），不连续，返回 0——即便 100 那块后面（150 恰好接上 100+50=150）有连续空间也不会被使用。`append` 只会追加到 `goff` 最大的块，这是为顺序写入路径设计的廉价检查，不是通用的空洞填充算法。

### 4.2 WrDiskCache：全局缓存与容量淘汰

#### 4.2.1 概念说明

`WrDiskCache` 回答的问题是：**内存预算有限（`--disk-cache=SIZE`），超了先冲刷谁？**

它是整个 aria2 进程唯一的缓存容器（由 `RequestGroupMan` 持有，所有下载任务共享），内部是一个以 `WrDiskCacheEntry*` 为元素的 `std::set`，外加三个记账字段：`limit_`（字节数上限）、`total_`（当前缓存字节总数）、`clock_`（逻辑时钟）。它只存**裸指针**做成员登记，不拥有 entry——entry 的生命周期归 `Piece`（见 4.3），这是一种「注册表/观察者」式的弱耦合。

淘汰策略是本模块的精髓：**缓存字节数最大的条目优先被冲刷；并列时，最久未更新的优先**。直觉是：在「总量必须降到 limit 以下」的目标下，冲刷一个大条目一次就能释放最多的内存，迭代次数最少；大小并列时选陈旧者，近似 LRU，避免反复冲刷正在高速增长的活跃条目。

#### 4.2.2 核心流程

条目在集合中的排序由 `WrDiskCacheEntry::operator<` 定义（注意方向是反的）：

\[ \text{lhs} < \text{rhs} \iff \text{sizeKey}_{\text{lhs}} > \text{sizeKey}_{\text{rhs}} \ \lor\ (\text{sizeKey 相等} \land \text{lastUpdate}_{\text{lhs}} < \text{lastUpdate}_{\text{rhs}}) \]

`std::set` 升序排列，于是 `set_.begin()` 恒指向「sizeKey 最大；并列则 lastUpdate 最小」的条目——淘汰目标。容量不变式为：

\[ \text{while } total_ > limit\_:\quad \text{flush}(\text{set\_.begin()});\ total\_ \mathrel{-}= \text{被冲刷字节数} \]

```
add(ent):    设键(sizeKey=getSize, lastUpdate=++clock) → insert → total_ += size → ensureLimit
remove(ent): erase → total_ -= size        （piece 释放时注销，不冲刷）
update(ent, delta):
             erase → 刷新两个键 → insert → total_ += delta → ensureLimit
             （set 键不可变，必须先摘再改再挂）
ensureLimit():while total_ > limit_: 取 begin() 条目
                 total_ -= 其 size → ent->writeToDisk()（落盘并清空数据）
                 → 重设键（此时 sizeKey 通常为 0）→ 重新 insert
```

注意「淘汰 ≠ 移除」：被 `ensureLimit` 冲刷的条目仍留在集合里（它对应的 piece 还在下载，后续数据会继续进缓存），只是它的数据被写到了磁盘。

#### 4.2.3 源码精读

容器与比较器的定义：

- [src/WrDiskCache.h:48-75](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WrDiskCache.h#L48-L75) — 类声明与四个成员。注释明确 `update` 的 `delta` 语义：增量为正、缩量为负。
- [src/WrDiskCacheEntry.h:84-92](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WrDiskCacheEntry.h#L84-L92) — `sizeKey/lastUpdate` 两个排序键与反向的 `operator<`（`sizeKey_ > rhs.sizeKey_` 打头）。`sizeKey_` 是**快照**：只在 add/update/淘汰后刷新，缓存数据变化不会自动同步到排序位置——这正是必须经 `WrDiskCache::update` 中转的原因。

三个核心方法：

- [src/WrDiskCache.cc:55-74](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WrDiskCache.cc#L55-L74) — `add()`：设键、插入、记账、`ensureLimit()`；重复插入（同一 piece 重复初始化）只告警返回 false。
- [src/WrDiskCache.cc:76-88](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WrDiskCache.cc#L76-L88) — `remove()`：只摘除与减账，**不落盘**（调用场景是 piece 彻底释放，彼时数据已先行 flush，见 4.3）。
- [src/WrDiskCache.cc:90-109](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WrDiskCache.cc#L90-L109) — `update()`：完整的「erase → 改键 → insert → 记账 → ensureLimit」舞步；负增量带 `assert(total_ >= -delta)` 防止记账穿透。

淘汰循环：

- [src/WrDiskCache.cc:111-127](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WrDiskCache.cc#L111-L127) — `ensureLimit()`：`while (total_ > limit_)` 取 `set_.begin()` 冲刷。日志 `Force flush cache entry ...` 是观察淘汰行为的窗口。冲刷后条目重新入集（新 sizeKey 通常是 0）。

生命周期的兜底告警：

- [src/WrDiskCache.cc:47-53](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WrDiskCache.cc#L47-L53) — 析构时若 `total_` 非零打 WARN：正常流程下所有条目都应在下载收尾时被 flush+remove（见 4.3），这条日志意味着有数据没落盘就退场了。

缓存的创建入口（谁设定 `limit_`）：

- [src/RequestGroupMan.cc:1048-1055](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L1048-L1055) — `initWrDiskCache()` 读 `--disk-cache`（`PREF_DISK_CACHE`，键名注册于 [src/prefs.cc:354](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/prefs.cc#L354)），**值为 0 时干脆不创建实例**（`getWrDiskCache()` 返回 nullptr，全链路退化为直写）。
- [src/DownloadEngineFactory.cc:148-160](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngineFactory.cc#L148-L160) — 引擎装配时创建 `RequestGroupMan` 并调用 `initWrDiskCache()`：缓存与引擎同生共死、全实例共享。
- [src/OptionHandlerFactory.cc:174-184](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L174-L184) — 选项定义：`UnitNumberOptionHandler`，默认 `16M`（可被编译期 `DEFAULT_DISK_CACHE` 宏覆盖），最小值 0。
- [src/usage_text.h:992-1004](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/usage_text.h#L992-L1004) — 帮助文本原文：缓存「per aria2 instance、所有下载共享」，两大收益即「更大写出单位 + 按偏移重排」「哈希校验免回读」。

#### 4.2.4 代码实践：推演 testAdd 的淘汰序列

1. **实践目标**：用 4.2.2 的排序与不变式，逐行预测 [test/WrDiskCacheTest.cc:36-75](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/WrDiskCacheTest.cc#L36-L75) 中 `limit=20` 时每一步的 `dc.getSize()` 与被冲刷者。
2. **操作步骤**：准备一张表格，列为「操作 / 各条目(sizeKey, clock) / total_ / set_.begin() 即淘汰目标 / 落盘内容」，按下面顺序填写：
   - `e1` 缓存 10 字节（clock=1）→ add：total_=10。
   - `e2` 缓存 10 字节（clock=2）→ add：total_=20，恰好不超限。
   - `e3` 缓存 5 字节（clock=3）→ add：total_=25 > 20 触发 `ensureLimit`。比较：e1=(10,1)、e2=(10,2)、e3=(5,3)。sizeKey 最大是 10（e1、e2 并列），并列取 lastUpdate 小者 → **e1 被冲刷**，total_=25−10=15。测试断言写入器出现 `"who knows?"`。
   - `e3` 再缓存 6 字节 → `update(e3, +6)`（clock=4，sizeKey=11）：total_=21 > 20。比较：e3=(11,4) 最大 → **e3 被冲刷**，total_=10。断言写入器追加 `"hello world"`。
   - `e2` 再缓存 11 字节 → `update(e2, +11)`（sizeKey=21）：total_=21 > 20 → **e2 被冲刷**，total_=0。
3. **需要观察的现象**：淘汰并不总是「最早加入」的条目——第三步选 e1 是大小并列下的时钟决胜，第四步选 e3 是纯粹因为它的 sizeKey(11) 压过了 e2(10)。
4. **预期结果**：你表格里每一步的 total_（10 → 20 → 15 → 10 → 0）与测试断言完全吻合。
5. 运行验证方式同 4.1.4 第 5 点，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`clock_` 为什么用自增整数而不是直接存系统时间戳？

**答案**：它只需要在全集内比较「谁更早被更新」，单调递增的计数器即可提供全序，且不受系统时钟跳变（NTP 校时、手动改时间）影响；同时 `int64_t` 计数比较比取系统时间开销小。这是典型的逻辑时钟（Lamport clock）用法。

**练习 2**：如果把 `operator<` 里的 `sizeKey_ > rhs.sizeKey_` 改成 `<`（即最小的条目先被冲刷），会发生什么？

**答案**：`set_.begin()` 变成缓存字节数最小的条目。淘汰一个小条目释放的字节很少，`ensureLimit` 的 while 循环可能要冲刷很多次才能把 total_ 压到 limit 以下；极端情况下（大量接近空的条目）每一小块新数据都会立刻触发对其他小块的冲刷，缓存几乎不起聚合作用，还平添大量小写入——正好与设计目标相反。并列时的 `lastUpdate_ < rhs.lastUpdate_` 保持不变的话，陈旧tiebreak 语义也随之反转，不再近似 LRU。

**练习 3**：`--disk-cache=0` 时程序里还有 `WrDiskCache` 对象吗？各写入路径如何感知「缓存关闭」？

**答案**：没有。`initWrDiskCache()` 在值为 0 时不构造对象，`getWrDiskCache()` 返回 nullptr；`Piece` 的各个 `*WrCache` 方法第一行都是 `if (!diskCache) return;`（如 [src/Piece.cc:300-310](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.cc#L300-L310)），`checkOutPiece` 也不会创建 entry，于是 `SinkStreamFilter` 走 `out->writeData(...)` 直写分支（见 4.3.3）。用空指针而非「limit=0 的实例」省掉了整条链路的无效调用。

### 4.3 DefaultPieceStorage 集成：缓存的挂接、写入路径与 flush 时机

#### 4.3.1 概念说明

前两个模块是数据结构，本模块回答**它们在下载主流程中何时被创建、写入、冲刷与销毁**。集成遵循 u5-l2 的结论「Piece 对象只为在途 piece 存活」：缓存条目作为 `Piece` 的成员（`unique_ptr<WrDiskCacheEntry> wrCache_`，[src/Piece.h:59](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.h#L59)）随之生灭——piece 领取时创建并注册进全局缓存，piece 释放时注销。因此缓存里永远是「正在下载中的 piece」的数据，与「完成即销毁」的内存纪律对齐。

两条写入路径共享同一个 entry：

- **HTTP/FTP 流式路径**：`DownloadCommand` 的过滤链尾端 `SinkStreamFilter`，每轮最多处理 16KiB（SocketRecvBuffer 容量）。
- **BT 路径**：`BtPieceMessage` 收到 piece 消息（单块最大 64KiB），每块一个 cell。

#### 4.3.2 核心流程

```
创建：SegmentMan::getSegment → DefaultPieceStorage::getMissingPiece → checkOutPiece
      └─ 条件满足 → Piece::initWrCache(全局缓存, diskAdaptor) → WrDiskCache::add

写入：
  HTTP/FTP: SinkStreamFilter::transform
      ├─ Piece::appendWrCache → 尾部连续? 原地追加
      └─ 追加不完 → new[] 拷贝(容量 ≥ max(len, 4KiB)) → Piece::updateWrCache → 新 cell
  BT:      BtPieceMessage::onPieceReceived → new[] 拷贝整块 → Piece::updateWrCache

flush 时机（五处）：
  F1 容量超限    WrDiskCache::ensureLimit（4.2）
  F2 piece 完成  BtPieceMessage::onNewPiece / DownloadCommand::completeSegment
  F3 segment 换手 SegmentMan::checkoutSegment / cancelSegmentInternal
                 （BT 多 peer 可能交叠写同一 piece，换手前必须先落盘）
  F4 周期自动保存 AutoSave → RequestGroupMan::save → saveControlFile
                 → flushWrDiskCacheEntry(false)   ← 冲刷但保留条目
  F5 收尾关文件  RequestGroup::closeFile
                 → flushWrDiskCacheEntry(true)    ← 冲刷并释放条目

销毁：DefaultPieceStorage::deleteUsedPiece → Piece::releaseWrCache → WrDiskCache::remove

免回读校验：piece 完成且需算哈希时
  Piece::getDigestWithWrCache = 内存 cell 逐段 update + 磁盘只补空洞
```

#### 4.3.3 源码精读

**挂接与注销（DefaultPieceStorage 侧）**：

- [src/DefaultPieceStorage.cc:119-143](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L119-L143) — `checkOutPiece()`：领取 piece 时，若任务不是内存下载（magnet 元数据走 `ByteArrayDiskWriter`，无需缓存）、全局缓存存在、且该 piece 尚无 entry，则 `piece->initWrCache(...)`。注释交代了一个前提：`diskAdaptor_` 在会话中不会被重建，所以 entry 可以安全地长期持有它的引用。
- [src/Piece.cc:300-310](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.cc#L300-L310) — `initWrCache()`：构造 entry 并 `add` 进全局缓存。
- [src/DefaultPieceStorage.cc:414-421](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L414-L421) — `deleteUsedPiece()`：piece 从在途集合移除时 `releaseWrCache` 注销。
- [src/Piece.cc:372-378](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.cc#L372-L378) — `releaseWrCache()`：从缓存 remove 并销毁 entry 对象（`wrCache_.reset()`）。
- [src/RequestGroup.cc:598-600](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L598-L600) — 任务初始化时把 `RequestGroupMan` 的全局缓存注入 `DefaultPieceStorage`（`setWrDiskCache`）。长度未知的流式任务用 `UnknownLengthPieceStorage`，其 [src/UnknownLengthPieceStorage.h:217-219](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UnknownLengthPieceStorage.h#L217-L219) 恒返回 nullptr——**不定长下载不进缓存**。

**HTTP/FTP 写入路径（SinkStreamFilter）**：

- [src/DownloadCommand.cc:101-104](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L101-L104) — 数据泵构造过滤链时把 `getWrDiskCache()` 传入 `SinkStreamFilter`（u4-l6 讲过过滤链恒以 Sink 结尾）。
- [src/SinkStreamFilter.cc:54-101](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SinkStreamFilter.cc#L54-L101) — `transform()` 的分流：piece 有 entry 时走缓存，否则 `out->writeData(...)` 直写（[L88-90](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SinkStreamFilter.cc#L88-L90)）。缓存分支两步：先 `appendWrCache` 尝试原地追加，追加不完的余量 `new[]` 一块**容量至少 4KiB** 的缓冲拷贝进去再 `updateWrCache`——注释写明了动机：接收 1~2 字节的小数据时，逐块建 cell 会碎片化到「令人头痛」，4KiB 余量让后续连续小数据可以免费搭车。

**BT 写入路径（BtPieceMessage）**：

- [src/BtPieceMessage.cc:105-152](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtPieceMessage.cc#L105-L152) — `onPieceReceived` 的接收处理：有 entry 时把消息载荷 `new[]` 拷贝一份再入缓存（[L118-125](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtPieceMessage.cc#L118-L125)），注释承认这「不幸地引入了一次额外拷贝」——因为消息缓冲是收发复用的临时存储，不能直接持有；无 entry 则直接 `getDiskAdaptor()->writeData`。
- [src/Piece.cc:334-354](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.cc#L334-L354) / [src/Piece.cc:356-370](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.cc#L356-L370) — `updateWrCache`（包装新 cell 并 `diskCache->update(+len)`）与 `appendWrCache`（尾部追加并按增量 `update`）。

**五个 flush 时机**：

- F2a（BT piece 完成）：[src/BtPieceMessage.cc:265-284](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtPieceMessage.cc#L265-L284) — `onNewPiece`：注释「每当凑齐一整个 piece 就冲刷缓存」，随后检查 `getError()`，失败则 `clearAllBlock` 清块并抛 `DOWNLOAD_FAILURE_EXCEPTION2`。
- F2b（HTTP segment 完成）：[src/DownloadCommand.cc:114-131](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L114-L131) 与 [src/DownloadCommand.cc:403-408](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L403-L408) — 文件内静态助手 `flushWrDiskCacheEntry` 在 `completeSegment` 时冲刷并做同样的错误检查。
- F3（segment 换手/取消）：[src/SegmentMan.cc:134-151](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L134-L151) 与 [src/SegmentMan.cc:285-305](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SegmentMan.cc#L285-L305) — 两处注释一致：「因为 BT peer 可能介入，缓存数据可能与磁盘交叠，换手前先冲刷」。（u3-l4 讲过 end-game 阶段同一 piece 会被多条连接/多个 peer 同时下，落盘前不冲刷就可能同一偏移写两次。）
- F4（周期自动保存）：[src/RequestGroup.cc:1291-1300](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L1291-L1300) — `saveControlFile()` 写 `.aria2` 控制文件前先 `flushWrDiskCacheEntry(false)` 并 `flushOSBuffers`：**控制文件里的位图说这块完成了，数据就必须真的在磁盘上**，否则断点续传会读到旧数据。调用链上游是 [src/RequestGroupMan.cc:595-612](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L595-L612) 的 `save()`（由 u2-l1 讲过的 AutoSave 周期命令驱动）。
- F5（收尾关文件）：[src/RequestGroup.cc:210-217](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L210-L217) — `closeFile()` 用 `flushWrDiskCacheEntry(true)` 冲刷**并释放**全部条目，再刷 OS 缓冲、关文件；调用点在 [src/RequestGroupMan.cc:370-378](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L370-L378)（任务完成/暂停/移除时）。

**全量冲刷接口与「按偏移有序」的关键注释**：

- [src/DefaultPieceStorage.cc:688-705](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L688-L705) — `flushWrDiskCacheEntry(bool releaseEntries)`：遍历在途 piece 集合逐个 `flushWrCache`。注释点明：**在途集合按 piece 索引排序，因此冲刷天然按不递减的文件偏移进行，有利于减少磁盘寻道**（除非文件已严重碎片化）。cell 级升序（4.1）+ piece 级升序（此处）= 两层排序保证。
- [src/Piece.cc:312-332](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.cc#L312-L332) — `flushWrCache`（`update(-size)` 记账后 `writeToDisk`）与 `clearWrCache`（记账后 `clear` 丢弃）。

**免回读校验**：

- [src/Piece.cc:253-275](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Piece.cc#L253-L275) — `getDigestWithWrCache()`：BT piece 凑齐后算哈希（调用点 [src/BtPieceMessage.cc:242-263](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtPieceMessage.cc#L242-L263) 的 `checkPieceHash`）。遍历升序的 cell 集合：cell 之间的空洞才从磁盘 `readData` 补齐，cell 本身直接喂给哈希上下文——**刚写的数据不必从磁盘读回来**，这是帮助文本承诺的第二个收益。

#### 4.3.4 代码实践：对比 `--disk-cache` 开关的 flush 行为

> 说明：讲义规格中提到的 `WrDiskCacheEntry::writeDataTo` 在源码中并不存在，实际的冲刷链路是 `WrDiskCacheEntry::writeToDisk()` → `DiskAdaptor::writeCache()` → `writeData()`，本实践按真实链路设计。

**方式 A（零改动，仅用现有日志）**：

1. **实践目标**：观察缓存开启时冲刷确实发生、且按偏移有序；确认缓存关闭时直写路径不产生冲刷日志。
2. **操作步骤**：
   - 构建项目（u1-l2 的 `autoreconf -i && ./configure && make`）。
   - 选一个合法的较大种子（如某 Linux 发行版官方 DVD 种子），执行：
     ```bash
     ./src/aria2c --seed-time=0 --disk-cache=64M \
       --log=cache64.log --log-level=debug <torrent 文件>
     ```
   - 统计冲刷日志（4.1.3 提到 `writeCache` 每块打出 `Cache flush goff=`）：`grep -c "Cache flush" cache64.log`；再看 `Force flush cache entry`（`ensureLimit` 淘汰）出现了几次。
   - 同一种子换 `--disk-cache=0` 重跑，得到 `cache0.log`，同样统计。
3. **需要观察的现象**：
   - `cache0.log` 中 `Cache flush` 与 `Force flush` 均为 0（没有缓存对象，走 `SinkStreamFilter`/`writeData` 直写分支）。
   - `cache64.log` 中每条 `Cache flush goff=X, len=Y` 的 `goff` 序列在单个 flush 批次内不递减；64MiB 缓存对多数下载足够大，`Force flush`（容量淘汰）可能一次都不出现——flush 主要由 F2（piece 完成）驱动。
4. **预期结果**：缓存开启时冲刷次数约等于「cell 数」（HTTP 每段至多每 4KiB 余量一次 + 换块；BT 每块一个 cell），写出按偏移有序；缓存关闭时无冲刷日志、每次直写 ≤16KiB（HTTP）。具体数字**待本地验证**。

**方式 B（插桩计数，对应规格中的加日志任务）**：

1. **实践目标**：定量统计「写盘调用次数」在缓存开/关时的差异。
2. **操作步骤**：
   - 在 `src/WrDiskCacheEntry.cc` 的 `writeToDisk()` 开头加一行计数日志，例如（示例代码，需自行重新编译且不要提交）：
     ```cpp
     static std::atomic<size_t> flushCount{0};
     A2_LOG_INFO(fmt("WRDISKCACHE flush#%lu cells=%lu bytes=%lu",
         static_cast<unsigned long>(++flushCount),
         static_cast<unsigned long>(set_.size()),
         static_cast<unsigned long>(size_)));
     ```
   - 重新 `make`，用方式 A 的两组参数各跑一次同一种子。
   - 或者不改代码，用 `strace -f -c -e trace=pwrite64,pwrite,write ./src/aria2c ...` 分别统计两种配置的系统调用次数与耗时。
3. **需要观察的现象**：关闭缓存时 `write/pwrite` 调用次数 ≈ 接收次数（每 16KiB 一次）；开启后调用被推迟并按 `goff` 聚合。同时留意 `getDigestWithWrCache` 带来的读侧变化：缓存命中时 piece 完成校验应几乎不需要 `pread`。
4. **预期结果**：开启 64MiB 缓存后写调用总数下降、单次写出增大、偏移序列更有序；下降幅度取决于 piece 大小与并发数（并发越高、交叠越多，聚合收益越明显）。具体倍数关系**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `checkOutPiece` 要排除 `inMemoryDownload()` 的任务？如果不排除会怎样？

**答案**：内存下载（典型是 magnet 元数据任务）的 `DiskAdaptor` 底下是 `ByteArrayDiskWriter`，数据本来就常驻内存且量很小（几十 KiB 的种子元数据）。再套一层写缓存毫无收益，反而多一次拷贝、多占全局预算、还要维护 flush 语义。不排除的话最坏情况是元数据被缓存却长期不冲刷，`UTMetadataPostDownloadHandler` 读取时依赖的是内存写入器自身的内容，缓存层反而引入了「何时落盘」的不确定性。

**练习 2**：F4（自动保存）为什么传 `releaseEntries=false` 而 F5（关文件）传 `true`？

**答案**：自动保存发生在下载**进行中**（AutoSave 周期触发），目的只是让磁盘内容与即将写入的 `.aria2` 位图一致；piece 还在下载、entry 还要继续接收数据，所以只冲刷数据、保留条目（entry 留在缓存中，sizeKey 归零后重新参与排序）。关文件则发生在任务收尾，piece 不再接收数据，条目连同 entry 对象一并释放，`total_` 清零——这也是 `WrDiskCache` 析构时 `total_` 应为零、否则告警（4.2.3）的保证来源。

**练习 3**：假设磁盘写失败（如磁盘满）发生在 `ensureLimit` 的 `writeToDisk` 里，这次失败如何传到下载层？数据会丢失吗？

**答案**：`writeToDisk` 捕获 `RecoverableException`，把 `error_` 置为 `CACHE_ERR_ERROR` 并保存错误码，缓存数据随即被丢弃。`ensureLimit` 本身不检查错误，继续记账；错误在下一次调用方检查点爆发——`BtPieceMessage::onNewPiece`（L270-277）、`DownloadCommand` 的静态助手（L121-128）或 `SegmentMan` 的助手（L123-130）都会发现 `getError() != CACHE_ERR_SUCCESS`，于是清掉该 piece 全部块（缓存与位图一起清）并抛出携带原始错误码的 `DOWNLOAD_FAILURE_EXCEPTION2`，任务按错误码走对应的失败/重试出口。丢失的只是这个 piece 的缓存数据，piece 会被重新下载——缓存层可以丢，哈希校验兜底。

## 5. 综合实践

设计一个三组对照实验，把本讲的「容量淘汰、写出有序、免回读」全部串起来：

1. **准备**：构建 aria2；准备一个 ≥2GiB 的合法多文件种子（Linux 发行版 ISO 种子最合适）与一块机械硬盘或较慢的存储介质（收益更明显，SSD 上偏移有序的收益会被掩盖）。
2. **三组运行**（其余参数完全一致，`--seed-time=0` 做完即停，开 `--log-level=debug`）：
   - A 组：`--disk-cache=0`
   - B 组：默认 `--disk-cache`（16M）
   - C 组：`--disk-cache=64M`
3. **采集三个指标**：
   - `grep -c "Cache flush" <log>`：cell 级冲刷次数；
   - `strace -c` 或方式 B 的插桩：`write/pwrite` 调用总次数、`pread` 调用总次数（免回读效果）、总耗时；
   - `grep -c "Force flush" <log>`：容量淘汰是否发生（B 组应比 C 组更频繁）。
4. **分析与产出**：写一份简短报告，回答三个问题：
   - B/C 组相对 A 组，写调用次数下降了多少？单次写出平均多大（总写出字节 ÷ 调用次数）？
   - C 组的 `Cache flush goff=` 序列是否基本单调？结合 `DataCellSet` 按 `goff` 排序与 `usedPieces_` 按索引排序两层机制解释。
   - piece 完成校验时的 `pread` 次数在 A/B 两组的差异，对应 `getDigestWithWrCache` 的哪段代码？
5. **风险提示**：BT 下载速度受 peer 状况影响，三组实验应间隔进行并各跑两次取稳定的一组；若结果波动大，可改用 `--bt-max-peers` 固定 peer 数。全部数值**待本地验证**。

## 6. 本讲小结

- `--disk-cache`（默认 16M、0 禁用）创建**进程级唯一**的 `WrDiskCache`，由 `RequestGroupMan` 持有、所有下载共享；禁用时对象根本不创建，全链路以空指针感知。
- `WrDiskCacheEntry` 是「一个在途 piece 的写缓冲」：数据装在按 `goff` 升序的 `DataCell` 集合里，尾部连续的数据可原地追加（HTTP 路径预留 ≥4KiB 余量吸收小数据），entry 拥有全部 cell 缓冲的内存。
- `WrDiskCache` 用 `std::set` + 反向比较器实现「缓存字节最大者优先、并列取最久未更新」的淘汰；`set` 键不可变约定逼出 `update()` 的「erase → 改键 → insert」舞步；被淘汰的条目数据落盘但**条目本身不移除**。
- flush 有五个时机：容量超限（F1）、piece/segment 完成（F2）、segment 换手防交叠（F3）、自动保存前保位图一致（F4，保留条目）、收尾关文件（F5，释放条目）；cell 级与 piece 级双重升序让写出按文件偏移有序，减少寻道。
- 写盘失败被 `writeToDisk` 就地吞下并记入 `error_/errorCode_`，由三个调用方检查点统一转为「清块 + 抛错」，piece 重下、哈希兜底；`getDigestWithWrCache` 让 piece 校验直接吃内存 cell、只为空洞读磁盘。
- 代价有两处：BT 路径每块一次额外 `memcpy`；HTTP 路径小数据要 `new[]` 拷贝进 cell——缓存用拷贝换聚合，这在两处源码注释里都被作者明确承认。

## 7. 下一步学习建议

本讲是磁盘 I/O 单元（u5）的收官。接下来建议：

1. **进入 BitTorrent 单元（u6）**：本讲反复出现的 `BtPieceMessage`、`getPieceStorage()->getMissingPiece`、end-game 交叠写等概念将在 u6-l4（DefaultBtInteractive）、u6-l5（消息分发）、u6-l6（请求流水线）中展开完整图景；带着「缓存为什么必须在 segment 换手时冲刷」的问题去读 end-game 逻辑会非常顺畅。
2. **若想继续深挖存储层**：对照阅读 `test/WrDiskCacheTest.cc`、`test/WrDiskCacheEntryTest.cc` 所在的 `test/` 目录，`DefaultPieceStorageTest.cc` 中有 `checkOutPiece` 创建缓存条目的直接用例，可作为 u5-l2 内容的补充验证。
3. **性能视角的延伸**：u9-l5（测试体系）会讲解如何用 CppUnit 过滤运行单个测试；u9-l6（二次开发）的「选项驱动功能开关」模式，恰好可以拿本讲的 `--disk-cache`（选项 → `initWrDiskCache` → 条件启用子系统）作为现成范例。
