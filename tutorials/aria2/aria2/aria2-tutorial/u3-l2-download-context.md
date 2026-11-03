# DownloadContext：下载任务的元数据模型

## 1. 本讲目标

上一讲（u3-l1）我们看到：RequestGroup 是 aria2 对「一次下载任务」的容器抽象，它聚合了 Option、DownloadContext、SegmentMan、PieceStorage、URISelector 等一大堆对象。本讲下探其中最基础的一个成员——DownloadContext，学完后你应该能够：

1. 说出 DownloadContext 承载哪些元数据（文件列表、piece 长度、piece 哈希、整文件校验和、协议属性槽），以及它**不**承载哪些东西（下载进度）。
2. 解释多文件任务（如 BitTorrent 种子）如何用一套「逻辑偏移空间」把所有文件串在一条地址线上，并能手算 `getTotalLength`、`getNumPieces`、`findFileEntryByOffset` 的结果。
3. 说明 ContextAttribute/TorrentAttribute 如何把协议专属信息（infohash、announce 列表等）挂载到同一个对象上；同时澄清一个常见误解：Metalink 并没有独立的属性槽，它是通过公共字段进入 DownloadContext 的。
4. 区分 DownloadContext（任务进行中的元数据）与 DownloadResult（任务结束后的只读快照），并追踪后者是如何从前者派生、被谁消费的。

## 2. 前置知识

- **元数据 vs 运行时状态**：本讲反复强调这条分界线。元数据是「这个任务*是*什么」——文件叫什么名字、多大、分几块、每块的哈希是什么；运行时状态是「这个任务*进行到哪了*」——哪些块已完成、当前连了几个 peer。前者放在 DownloadContext，后者放在 PieceStorage / SegmentMan / PeerStorage（PieceStorage 将在 u5-l2 详讲）。
- **piece（块）**：aria2 内部把下载内容切成固定长度的 piece（默认 1 MiB，可用 `--min-split-size` / `--piece-length` 影响），校验、断点续存、BT 协议消息都以 piece 为单位。piece 长度记录在 DownloadContext 里，但「哪些 piece 完成了」不在。
- **FileEntry（文件条目）**：一个待下载文件的描述符，核心三元组是 `(path 路径, length 长度, offset 起始偏移)`。单文件下载只有一个 FileEntry；多文件种子有多个，它们首尾相接拼成一条逻辑字节流。
- **GID**：u3-l1 引入过的全局任务编号，16 位十六进制。它属于 RequestGroup/DownloadResult，不属于 DownloadContext——记住「谁拥有什么」正是本讲的主题。
- **`std::shared_ptr`**：C++ 的引用计数智能指针。DownloadContext 内部大量用 `std::shared_ptr<FileEntry>`、`std::shared_ptr<ContextAttribute>` 存成员，好处是任务结束后 DownloadResult 可以直接拷贝这些指针继续持有数据。
- **槽位（slot）+ 枚举**：用一个「以枚举值为下标的数组」存放可扩展的属性集合，这是 C++ 里极简的类型擦除手法，本讲 4.3 会看到。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/DownloadContext.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadContext.h) | DownloadContext 类声明：全部成员变量与接口签名，是本讲的「地图」本身 |
| [src/DownloadContext.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadContext.cc) | DownloadContext 的实现：偏移查找、文件过滤、属性存取、piece 计数 |
| [src/ContextAttribute.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ContextAttribute.h) / [src/ContextAttribute.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ContextAttribute.cc) | 属性空基类与属性类型枚举（目前只有 `CTX_ATTR_BT` 一个槽） |
| [src/TorrentAttribute.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TorrentAttribute.h) / [src/TorrentAttribute.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TorrentAttribute.cc) | BitTorrent 专属属性结构体：infohash、announce 列表、metadata 等 |
| [src/DownloadResult.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadResult.h) / [src/DownloadResult.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadResult.cc) | 任务结束后的只读快照结构体 |
| [src/RequestGroup.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc) | `createDownloadResult()`：从 RequestGroup + DownloadContext 抽取快照 |
| [src/RequestGroupMan.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc) | `showDownloadResults()`：DownloadResult 的最终消费方之一（控制台摘要） |
| [src/download_helper.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc) | 三条「创建 DownloadContext」的典型路径：HTTP URI / .torrent / magnet |
| [src/bittorrent_helper.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc) | 种子字典 → DownloadContext + TorrentAttribute 的完整赋值过程 |
| [src/Metalink2RequestGroup.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalink2RequestGroup.cc) | Metalink entry → DownloadContext 公共字段（无属性槽）的对照样本 |
| [test/DownloadContextTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DownloadContextTest.cc) | 本讲实践任务的模板：官方单测覆盖了 5 个核心方法 |

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：**DownloadContext 聚合根** → **逻辑偏移空间** → **属性挂载点** → **DownloadResult 快照**。

### 4.1 DownloadContext：任务元数据的聚合根

#### 4.1.1 概念说明

一个 DownloadContext 回答的问题是：「我们到底在下载什么？」

- 下载**哪些文件**？（`fileEntries_`：FileEntry 列表）
- 内容**总共多长**？（由 FileEntry 拼接推得，见 4.2）
- **按多大一块**切分、**每块的哈希**是什么？（`pieceLength_`、`pieceHashes_`、`pieceHashType_`）
- **整文件校验和**是什么？（`digest_` + `hashType_`，即 `--checksum` 传入的那个值）
- 有没有**协议专属附件**？（`attrs_` 属性槽，见 4.3）
- 有没有**签名文件**？（`signature_`，来自 Metalink）

它**不**回答：「下到哪了？」——完成进度由 PieceStorage 的 bitfield 记录；也**不**回答：「用哪些 URI？」——URI 挂在每个 FileEntry 上（`FileEntry::setUris`），因为多文件种子里不同文件可以有不同的 web-seed URI。

另外它还带一小块「会话统计」：`netStat_`（本次会话的下载/上传速度与流量）与 `downloadStopTime_`，以及一个反向裸指针 `ownerRequestGroup_` 指回宿主 RequestGroup——注意这是**观察指针而非拥有关系**，方向上 RequestGroup 才是拥有者。

#### 4.1.2 核心流程

DownloadContext 的生命周期与宿主 RequestGroup 同步：

```text
download_helper.cc 按输入类型创建 DownloadContext
   ├─ HTTP/FTP URI → 便捷构造函数（单 FileEntry，长度先填 0）
   ├─ .torrent     → 默认构造 + bittorrent::loadFromMemory 逐字段填充
   └─ magnet       → 便捷构造（piece 长度 = METADATA_PIECE_SIZE）+ markTotalLengthIsUnknown
        ↓
RequestGroup 持有它，贯穿整个下载过程
   ├─ 长度未知时（服务器不给 Content-Length）→ markTotalLengthIsUnknown()
   ├─ 每读到一批数据 → updateDownload(bytes)，顺带更新全局统计
   └─ 校验完成后 → setChecksumVerified(true)
        ↓
任务结束 → RequestGroup::createDownloadResult() 把元数据抽进 DownloadResult
        ↓
DownloadContext 随 RequestGroup 一起销毁（DownloadResult 用拷贝的指针续命数据）
```

#### 4.1.3 源码精读

先看成员变量全景，这一段就是「任务元数据」的完整清单：

- [src/DownloadContext.h:L58-L91](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadContext.h#L58-L91) — DownloadContext 类声明：`fileEntries_`（文件列表）、`pieceHashes_`/`pieceHashType_`（分块哈希）、`digest_`/`hashType_`（整文件校验和）、`attrs_`（属性槽）、`pieceLength_`、`basePath_`、`knowsTotalLength_` 等成员一字排开。注意 `ownerRequestGroup_` 是裸指针（L62），`attrs_` 是以枚举为下标的 vector（L64）。

两个构造函数，对应「单文件任务」与「待填充任务」两种起点：

- [src/DownloadContext.cc:L50-L63](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadContext.cc#L50-L63) — 默认构造：`attrs_(MAX_CTX_ATTR)` 把属性槽数组预分配出来；`knowsTotalLength_` 默认为 **true**（乐观假设长度已知，HTTP 场景若服务器不给 Content-Length 再显式改回未知）；`acceptMetalink_` 由编译宏 `ENABLE_METALINK` 决定初值。
- [src/DownloadContext.cc:L65-L81](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadContext.cc#L65-L81) — 便捷构造函数：直接创建**单个** FileEntry，`offset` 固定为 0。这就是普通 HTTP/FTP 下载的起点。

看三处典型创建点（download_helper.cc）：

- [src/download_helper.cc:L137-L154](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L137-L154) — HTTP/FTP 路径：`pieceLength` 取自 `--piece-length` 选项，总长度暂填 0（等响应头）；URI 挂到第一个 FileEntry 上；`--checksum` 选项被拆成 hashType 与原始字节 digest 后经 `setDigest` 存入。
- [src/download_helper.cc:L194-L221](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L194-L221) — 种子路径：先默认构造空 dctx，再交给 `bittorrent::loadFromMemory` 填充（详见 4.3.3），随后 `--select-file` 经 `setFileFilter`、`--index-out` 经 `setFilePathWithIndex` 继续改写它——同一个对象被多道工序加工。
- [src/download_helper.cc:L240-L245](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L240-L245) — magnet 路径：便捷构造时 piece 长度用常量 `METADATA_PIECE_SIZE`（因为这个任务下载的「文件」就是种子元数据本身），并立刻 `markTotalLengthIsUnknown()`——此刻只知 infohash，长度确实未知。

两个派生量（不存储、每次现算）：

- [src/DownloadContext.cc:L218-L226](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadContext.cc#L218-L226) — `getTotalLength()` 直接返回**最后一个** FileEntry 的 `getLastOffset()`（= `offset + length`）。这隐含一条约定：fileEntries_ 必须按 offset 升序且首尾相接，总长才能由尾元素代表。
- [src/DownloadContext.cc:L206-L216](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadContext.cc#L206-L216) — `getNumPieces()` 是向上取整除法：

\[ \text{numPieces} = \left\lceil \frac{L}{p} \right\rceil = \frac{L + p - 1}{p} \quad (L=\text{总长度},\ p=\text{piece 长度}) \]

分块哈希与校验判定的三个布尔函数：

- [src/DownloadContext.cc:L277-L285](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadContext.cc#L277-L285) — `getPieceHash(index)` 越界时返回空串 `A2STR::NIL` 而不是抛异常（BT 校验时允许哈希缺失）。
- [src/DownloadContext.cc:L260-L275](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadContext.cc#L260-L275) — 三个判定函数值得一并读：`isChecksumVerificationNeeded`（没有分块哈希、有整文件摘要、且尚未校验过——注意条件里 `pieceHashType_.empty()`，说明**分块校验优先于整文件校验**）；`isPieceHashVerificationAvailable` 要求哈希条数**恰好等于** piece 总数，多了少了都算不可用。

最后一个容易忽略的联动：

- [src/DownloadContext.cc:L304-L311](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadContext.cc#L304-L311) — `updateDownload(bytes)` 先更新自己的 `netStat_`，再经 `ownerRequestGroup_` 反向指针把字节数累加到 RequestGroupMan 的全局统计上。这就是 u3-l1 讲过的「任务层 → 全局层」流量上报链，钩子点就在这里。

#### 4.1.4 代码实践（源码阅读 + 手算验证）

1. **实践目标**：不动手编译也能验证你读懂了 `getNumPieces` 与 `getTotalLength` 的公式。
2. **操作步骤**：
   - 打开 [test/DownloadContextTest.cc:L63-L67](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DownloadContextTest.cc#L63-L67) 的 `testGetNumPieces`：用例构造 `DownloadContext ctx(345, 9889, "")`，断言 piece 数为 29。
   - 自己代入公式：\( \lceil 9889 / 345 \rceil = \lceil 28.66 \rceil = 29 \)。
   - 再看 [test/DownloadContextTest.cc:L69-L75](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DownloadContextTest.cc#L69-L75) 的 `testGetBasePath`：空路径时 `getBasePath()` 返回空串，`setPath` 之后返回新路径——验证了 basePath 未显式设置时「退化为第一个文件路径」的回退逻辑（见 4.2.3）。
3. **需要观察的现象**：你的手算结果与断言值一致；`testGetBasePath` 说明 basePath 是**惰性回退**而非独立维护的副本。
4. **预期结果**：29 与 29 吻合；能口述「piece 数 = 尾元素 lastOffset 向上取整除以 pieceLength」。
5. 想跑真机的话：完成 `./configure && make` 后执行 `make check`（或直接运行 `test/aria2ctest`，用 `--test` 过滤用例），观察 `DownloadContextTest` 全绿。具体运行输出待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `updateDownload` 里要访问 `ownerRequestGroup_`？把这段联动删掉会发生什么？

**答案**：它是任务级统计上报到全局统计（RequestGroupMan::getNetStat）的桥。删掉后 `aria2c` 控制台上的总下载速度/总流量（所有任务之和）将不再增长，单任务速度仍正常，因为每条链路各自维护净统计。

**练习 2**：HTTP 下载创建 DownloadContext 时总长度填 0 且 `knowsTotalLength_` 为 true，这不是自相矛盾吗？

**答案**：不矛盾。`knowsTotalLength_` 表示「**最终**能否知道长度」，默认乐观为 true；当响应到达后发现没有 Content-Length（如 chunked 编码且无描述长度），HTTP 响应处理代码会调用 `markTotalLengthIsUnknown()` 改回 false，下游 PieceStorage 层据此切换到 UnknownLengthPieceStorage（u5-l2 会讲）。也就是说初值只是「先按已知处理，发现未知再降级」的默认策略。

### 4.2 逻辑偏移空间：FileEntry 拼接与多文件定位

#### 4.2.1 概念说明

多文件任务（一个种子包含目录树）的关键设计：把所有文件**首尾相接**拼成一条从 0 开始的逻辑字节流。

```text
逻辑偏移:   0 ──────────────► 1000        1000 ──────► 3000     3000 ────► 6000
            ┌─ file1 (1000) ─┐┌─ file4 (2000) ─────────┐┌─ file5 (3000) ─┐
            （file2/file3 是 0 字节占位，不占地址线）
piece 切分与 segment 切分都在这条线上进行，与物理文件边界无关
```

好处：piece 编号、segment 分配、BT 协议里的 index 字段都可以用「逻辑偏移」一个坐标系描述；要把逻辑区间落到磁盘，再由 DiskAdaptor 层（u5-l3）换算成「哪个文件的第几个字节」。`getBasePath`、`findFileEntryByOffset`、`setFileFilter` 都围绕这条地址线工作。

#### 4.2.2 核心流程

- `findFileEntryByOffset(offset)`：给定逻辑偏移，二分定位所属 FileEntry——写盘与校验时高频调用。
- `setFileFilter(SegList)`：把 `--select-file=2,5-8` 之类的选择表达式翻译成每个 FileEntry 的 `requested_` 布尔位；只有 requested 的文件才会真正下载。
- `getFirstRequestedFileEntry()` / `countRequestedFileEntry()`：跳过未选中的文件找「有效首文件」、统计选中数。
- `setFilePathWithIndex(index, path)`：`--index-out=2=renamed.bin` 的落点，按 1 起始的序号改写某个文件的保存路径。

#### 4.2.3 源码精读

- [src/DownloadContext.cc:L103-L121](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadContext.cc#L103-L121) — `findFileEntryByOffset`：先做边界快筛（空列表、或 `offset>0` 且尾元素 `getLastOffset() <= offset` 直接判越界返回空指针）；然后构造一个**只设置 offset 的临时 FileEntry** 作为键，用 `std::upper_bound` 在按 offset 升序的列表中二分，命中起始偏移返回该元素，否则返回前一个。注意 0 字节文件（file2/file3）会被二分自然跳过——任何落在区间内的偏移都归属前一个非空文件。
- [src/DownloadContext.cc:L136-L161](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadContext.cc#L136-L161) — `setFileFilter`：两个特例先处理——选择列表为空或**只有一个文件**时全部置为 requested（单文件任务无所谓选择）；一般情况是一个双指针扫描：`i` 走文件下标、`sgl` 走已归一化的选择段，命中则 `setRequested(true)` 并推进选择段，否则置 false，扫描完毕后剩余文件统一置 false。
- [src/DownloadContext.cc:L228-L237](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadContext.cc#L228-L237) — `getBasePath`：`basePath_` 非空则返回之，否则回退到**第一个 FileEntry 的路径**。头文件注释（[src/DownloadContext.h:L163-L167](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadContext.h#L163-L167)）说明它的用途：作为 `.aria2` 控制文件名的代表路径。对多文件种子，种子根目录名会被设为 basePath，于是控制文件名是「目录名.aria2」而不是「第一个文件名.aria2」——这就是它存在的意义。
- [src/DownloadContext.cc:L123-L134](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadContext.cc#L123-L134) — `setFilePathWithIndex`：索引 1 起始、越界抛 `DL_ABORT_EX`；注释特意说明**不做** `escapePath` 转义，因为路径来自用户。
- [test/DownloadContextTest.cc:L30-L52](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DownloadContextTest.cc#L30-L52) — 官方单测用 6 个 FileEntry（含两个 0 字节文件）验证了上述全部行为，是理解二分定位的最佳教具。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：亲手验证逻辑偏移空间的拼接与二分定位规则。
2. **操作步骤**：
   - 抄下 [test/DownloadContextTest.cc:L36-L42](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DownloadContextTest.cc#L36-L42) 的 6 个 FileEntry：`file1(1000,0)`、`file2(0,1000)`、`file3(0,1000)`、`file4(2000,1000)`、`file5(3000,3000)`、`file6(0,6000)`（构造参数是 `path, length, offset`）。
   - 画一条地址线，标出每个文件覆盖的区间。
   - 预测 `findFileEntryByOffset(0 / 1500 / 5999 / 6000)` 各返回谁，再对照 L45-L51 的断言。
3. **需要观察的现象**：0→file1；1500 落在 file4 的 [1000,3000) 区间→file4；5999 落在 file5 的 [3000,6000)→file5；6000 触发 `back()->getLastOffset() <= offset` 的越界快筛→空指针。
4. **预期结果**：与断言完全一致；并且能解释为什么 0 字节的 file2/file3 永远不会被返回（任何偏移都落在它前面或后面的非空文件里）。
5. 进阶（可选）：阅读 [src/bittorrent_helper.cc:L178-L184](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L178-L184) 的 `extractFileEntries` 签名与调用点（[L496-L497](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L496-L497)），确认种子解析时 offset 是逐文件累加出来的——这正是「首尾相接」约定的生产端。

#### 4.2.5 小练习与答案

**练习 1**：`--select-file=1` 只选第一个文件时，`getTotalLength()` 返回的是选中文件的长度吗？

**答案**：不是。`getTotalLength()` 恒等于整条逻辑流的长度（尾元素 lastOffset），与 requested 与否无关。「选中文件的总大小」要用 `countRequestedFileEntry` 加总每个选中 FileEntry 的 length，或看 PieceStorage 的按需过滤结果（`--select-file` 对 piece 的影响在 u5-l2/l3 展开）。

**练习 2**：若某个下载的 fileEntries_ 没有按 offset 排序，`findFileEntryByOffset` 会怎样？

**答案**：`std::upper_bound` 的前提是序列有序，乱序时行为未定义（可能返回错误文件）。这是 DownloadContext 对构造方（torrent/metalink 解析器）的一条隐含契约，类型系统不强制，靠调用方保证。

### 4.3 ContextAttribute 与 TorrentAttribute：协议专属信息的挂载点

#### 4.3.1 概念说明

DownloadContext 的公共字段（文件、piece、校验和）覆盖了所有协议的共性需求，但 BitTorrent 还有一堆专属信息：infohash、announce 列表、DHT nodes、是否私有种子、magnet 抓到的 metadata……这些如果都塞进 DownloadContext，HTTP 下载就背着一堆永远为空的字段。

aria2 的解法是**属性槽**：DownloadContext 持有一个「以枚举为下标的数组」`attrs_`，每个协议认领一个槽位，槽里放任意继承自 ContextAttribute 的结构体。HTTP 下载一个槽都不用；BT 下载填 `CTX_ATTR_BT` 槽，里面是 TorrentAttribute。

**一个必须澄清的事实**：枚举里目前**只有** `CTX_ATTR_BT` 一个真实槽位（外加哨兵 `MAX_CTX_ATTR`），并不存在 `CTX_ATTR_METALINK`。Metalink 的信息（校验和、分块哈希、签名、文件路径）在创建 RequestGroup 时被**拆解进公共字段**——setDigest / setPieceHashes / setSignature / FileEntry，而不是作为属性挂载。两种集成方式的对比是本模块最有价值的观察点。

#### 4.3.2 核心流程

```text
写入（BT）：
  bittorrent::load/loadFromMemory/loadMagnet
    └─ 解析 bencode 字典
         ├─ 公共部分 → ctx->setPieceLength / setPieceHashes / fileEntries
         └─ 专属部分 → 组装 TorrentAttribute
                          └─ ctx->setAttribute(CTX_ATTR_BT, torrent)

读取：
  ctx->hasAttribute(CTX_ATTR_BT)     // 判断是否 BT 任务
  bittorrent::getTorrentAttrs(dctx)  // 取 TorrentAttribute*（内部即 getAttribute + cast）

Metalink（对照）：
  Metalink2RequestGroup
    ├─ dctx->setDigest(...)          // 整文件校验和 → 公共字段
    ├─ dctx->setPieceHashes(...)     // 分块哈希 → 公共字段
    ├─ dctx->setSignature(...)       // 签名 → 公共字段
    └─ （没有 Metalink 属性槽）
```

#### 4.3.3 源码精读

先看挂载机制本身，总共不到 40 行：

- [src/ContextAttribute.h:L42-L52](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ContextAttribute.h#L42-L52) — `ContextAttribute` 是只有虚析构的空基类；枚举 `ContextAttributeType` 目前仅有 `CTX_ATTR_BT`，`MAX_CTX_ATTR` 作为哨兵兼做 `attrs_` 数组长度。
- [src/DownloadContext.cc:L163-L187](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadContext.cc#L163-L187) — `setAttribute`（按下标覆盖写入）、`getAttribute`（槽为空时**抛异常**，错误信息经 `strContextAttributeType` 给出可读名字）、`hasAttribute`（只查非空）。读属性失败是异常而非返回空指针，因为「BT 代码读 BT 属性」若失败说明程序逻辑已错，属于不该被静默吞掉的错误。
- [src/ContextAttribute.cc:L39-L47](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ContextAttribute.cc#L39-L47) — `strContextAttributeType`：把枚举翻译成 "BitTorrent" / "UNKNOWN"，仅用于错误信息。

再看 TorrentAttribute 装了什么：

- [src/TorrentAttribute.h:L48-L69](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TorrentAttribute.h#L48-L69) — 字段一览：`infoHash`（**原始 20 字节**，非十六进制文本，见 L53-L54 注释）、`metadata`/`metadataSize`（info 字典的 bencode 原文，magnet 场景的核心载荷）、`announceList`（tier 层级的 tracker 列表）、`nodes`（DHT 引导节点）、`mode`（单文件/多文件）、`privateTorrent`、`creationDate`、`comment`/`createdBy`、`urlList`（HTTP web-seed）。禁止拷贝（`= delete`），只能经 shared_ptr 共享。
- [src/TorrentAttribute.cc:L39-L45](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TorrentAttribute.cc#L39-L45) — 构造函数只做零值初始化，所有字段由解析器逐一填充。

写入端（.torrent → DownloadContext 的完整赋值过程，本讲只看骨架，bencode 细节留给 u6-l1）：

- [src/bittorrent_helper.cc:L413-L440](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L413-L440) — `processRootDictionary` 开头：new 一个 TorrentAttribute，把 info 字典重新 bencode 后做 SHA-1，得到 20 字节 infohash 存入 `torrent->infoHash`，同时把 info 字典原文存入 `metadata`/`metadataSize`。
- [src/bittorrent_helper.cc:L472-L501](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L472-L501) — 公共字段这边：`ctx->setPieceLength(pieceLength)`、`extractPieceHash` 把 `pieces` 字符串按每 20 字节切成哈希表；随后 `extractFileEntries` 填文件列表；最后交叉校验 `(totalLength + pieceLength - 1) / pieceLength == numPieces`，对不上直接抛「Too few/many piece hash」——又一次出现 4.1 的取整公式。
- [src/bittorrent_helper.cc:L527](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L527) — 最后一行 `ctx->setAttribute(CTX_ATTR_BT, std::move(torrent))`： TorrentAttribute 正式入槽。magnet 路径同理，见 [src/bittorrent_helper.cc:L965](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L965)（`parseMagnet` 的返回值入槽）。
- [src/bittorrent_helper.h:L158-L169](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.h#L158-L169) — 读取端惯用法：`getTorrentAttrs(dctx)` 返回 `TorrentAttribute*`，`getInfoHash(dctx)` 直接取 infohash 指针。BT 子系统代码里到处是这两个函数。

Metalink 的对照样本（没有属性槽）：

- [src/Metalink2RequestGroup.cc:L261-L283](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalink2RequestGroup.cc#L261-L283) — 单 entry 分支：`make_shared<DownloadContext>(pieceLength, entry->getLength(), path)` 之后，`setDigest`（L274-277）、`setPieceHashes`（L278-282，且若有分块哈希则 piece 长度直接采用哈希块的长度，见 L252-L260）、`setSignature`（L283）——全部走公共字段，自始至终没有 `setAttribute`。

#### 4.3.4 代码实践（加日志观察赋值过程）

这是本讲规格给出的第二种实践方式：在 download_helper.cc 加日志，观察从 .torrent 到 DownloadContext 的赋值过程。

1. **实践目标**：亲眼看到「一个 DownloadContext 被逐步填充」的顺序，验证 4.3.2 的流程图。
2. **操作步骤**：
   - 在 [src/download_helper.cc:L197](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L197)（`loadFromMemory` 调用之后）插入一行示例代码：
     ```cpp
     // 示例代码：观察种子填充结果
     A2_LOG_INFO(fmt("BT dctx: files=%zu totalLength=%" PRId64
                     " pieceLength=%d numPieces=%zu hasBTAttr=%d",
                     dctx->getFileEntries().size(), dctx->getTotalLength(),
                     dctx->getPieceLength(), dctx->getNumPieces(),
                     dctx->hasAttribute(CTX_ATTR_BT)));
     ```
   - 重新 `make`，然后运行 `./src/aria2c --show-files <某个.torrent>`（u1-l3 讲过 show-files 会提前退出，正好只走解析不走下载）。
3. **需要观察的现象**：日志里 files 等于种子文件数、totalLength 等于各文件长度之和、numPieces 等于总长除以 piece 长度向上取整、hasBTAttr 为 1；且这行日志出现在 `--show-files` 的文件列表**之前**（说明解析先于展示）。
4. **预期结果**：与 `--show-files` 打印的文件清单逐项对得上。若无种子文件在手，可先用 `aria2c --bt-save-metadata=true <magnet:...>` 生成一个（待本地验证）。
5. 注意：这属于「改一行日志」的实验性修改，验证完请还原，不要提交。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `getAttribute` 在槽为空时抛异常，而不是返回 nullptr 让调用方判空？

**答案**：语义分层。调用方想知道「是不是 BT 任务」应该用 `hasAttribute`（正常分支）；而 BT 代码路径里取 BT 属性时，槽居然为空意味着上游流程被破坏（比如忘了 load 就用），这是程序错误，应当立刻炸出来而不是让空指针在深层崩溃。异常信息里的属性名（来自 `strContextAttributeType`）也便于定位。

**练习 2**：如果让你给某个新协议（假设叫 FooProtocol）加专属元数据，按 aria2 的现有风格要改哪几处？

**答案**：在 `ContextAttributeType` 枚举里加 `CTX_ATTR_FOO`（放在 `MAX_CTX_ATTR` 之前，`attrs_` 数组长度自动扩一）；新建 `FooAttribute : public ContextAttribute` 结构体；在 `strContextAttributeType` 补一个 case；解析器里 `ctx->setAttribute(CTX_ATTR_FOO, ...)`。公共字段（路径、长度、哈希）仍走 FileEntry/setDigest 等既有通道——Metalink 就是只走公共通道、一个槽都不占的先例。

**练习 3**：`TorrentAttribute::infoHash` 存的是 40 个字符的十六进制字符串吗？

**答案**：不是，是 20 字节原始 SHA-1 值（[src/TorrentAttribute.h:L53-L54](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TorrentAttribute.h#L53-L54) 注释明确写了 "raw hash value 20 bytes"）。要展示给人看时才用 `util::toHex` 转成 40 字符，例如 download_helper.cc 里拼 `.torrent` 元数据文件名时（[src/download_helper.cc:L251-L253](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L251-L253)）。

### 4.4 DownloadResult：任务结束后的只读快照

#### 4.4.1 概念说明

任务结束（完成、失败、被删除）后，RequestGroup 会被销毁，但「这次跑出了什么结果」必须留下来：控制台结束时的 Download Results 摘要、RPC 的 `aria2.tellStatus`/`aria2.getDownloadResult`、`--save-session` 的恢复清单，都依赖它。DownloadResult 就是这份**只读快照**：一张扁平的结构体，把 RequestGroup 与 DownloadContext 里散落的关键值一次性抄录下来。

它与 DownloadContext 的分工：DownloadContext 是**活的**元数据，随任务演化（长度可从未知变已知、路径可被改写）；DownloadResult 是**定稿的**结果记录，任务一结束就不再变化，连 `error_code::IN_PROGRESS` 这种「结果」都可能是中间态（强制退出时任务的快照即为此值）。

#### 4.4.2 核心流程

```text
任务结束
  └─ RequestGroup::createDownloadResult()
       ├─ 元数据：attrs（属性槽数组）、fileEntries、pieceLength、numPieces
       ├─ 进度定稿：totalLength、completedLength、bitfield（完成位图字符串）
       ├─ 身份链：gid、belongsTo、followedBy、following（u3-l1 的 GID 关系链）
       ├─ 统计：sessionDownloadLength、sessionTime、uploadLength
       └─ 结果：result（error_code 枚举）+ resultMessage
  └─ RequestGroupMan::addDownloadResult() 存入 downloadResults_（有上限，FIFO 淘汰）
       ├─ 控制台摘要：showDownloadResults()
       └─ RPC / 会话保存按 gid 查询
```

#### 4.4.3 源码精读

- [src/DownloadResult.h:L56-L110](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadResult.h#L56-L110) — 全部字段一目了然，几组值得注意：`belongsTo`/`followedBy`/`following` 是 u3-l1 讲过的 GID 关系链（注释直接指向 RequestGroup.cc 的对应成员）；`attrs` 与 `fileEntries` 是 `shared_ptr` 容器的**浅拷贝**，共享底层对象；`bitfield` 是完成位图的原始字节串；`infoHash` 独立成字段（BT 任务专用）。
- [src/DownloadResult.cc:L42-L55](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadResult.cc#L42-L55) — 构造函数把所有数值清零、`result` 初始化为 `error_code::UNDEFINED`：快照先是一张白纸，再由下面这个函数逐项填写。
- [src/RequestGroup.cc:L1171-L1210](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L1171-L1210) — `createDownloadResult()` 是快照的**唯一生产者**，建议逐行读：L1177 `res->attrs = downloadContext_->getAttributes()` 直接拷贝属性槽数组（ TorrentAttribute 因此在任务结束后仍可被 RPC 读取）；L1195-1199 有 pieceStorage 时抄录 bitfield；L1200-1205 **仅当** `hasAttribute(CTX_ATTR_BT)` 才抄 infohash——属性槽在这里第二次出场（第一次是 4.3 的挂载）；L1206-1207 pieceLength/numPieces 来自 DownloadContext，L1192-1193 的 totalLength/completedLength 则来自 PieceStorage——元数据与进度在快照里合流。
- [src/RequestGroupMan.cc:L696-L751](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L696-L751) — `showDownloadResults`：`aria2c` 退出前打印的 "Download Results:" 表格（gid/stat/avg speed/path）就是遍历 `downloadResults_`（L721）把每个 DownloadResult 的 `result` 映射成 OK/ERR/INPR/RM 状态列（L727-L743）。
- [src/RequestGroupMan.cc:L921-L940](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L921-L940) — `addDownloadResult`：快照入队 `downloadResults_`，超过 `maxDownloadResult` 上限时从队头淘汰——保留最近的 N 条结果，而不是全部。

#### 4.4.4 代码实践（观察快照的投影）

1. **实践目标**：把控制台输出与 DownloadResult 字段一一对应起来。
2. **操作步骤**：
   - 运行任意一个小下载，例如 `./src/aria2c https://example.com/ -o example.html`，任其完成。
   - 观察退出前打印的 "Download Results:" 表格（由 `showDownloadResults` 生成）。
   - 对照 [src/RequestGroupMan.cc:L698-L713](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L698-L713) 的表头代码（`gid | stat | avg speed | path/URI`），把每一列反推到 DownloadResult 的具体字段。
3. **需要观察的现象**：gid 列来自 `dr->gid`；stat 列是 `dr->result`（成功时 FINISHED 映射为 OK）；path 列来自 `dr->fileEntries` 里的路径；平均值由 sessionDownloadLength/sessionTime 算出。
4. **预期结果**：你能对每列写出「字段名 → 来源（RequestGroup / DownloadContext / PieceStorage）」。表格是否带颜色取决于终端能力与 `--enable-color`（L716）。具体输出待本地验证。
5. 延伸（源码阅读）：思考被 `belongsTo != 0` 跳过的行（[L723-L725](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L723-L725)）——magnet 的元数据任务作为「子任务」不单独占一行，它通过 followedBy 关系并入主任务的结果行。

#### 4.4.5 小练习与答案

**练习 1**：DownloadResult 里为什么既有 `totalLength` 又有 `pieceLength`/`numPieces`，它们和 DownloadContext 的关系是什么？

**答案**：`pieceLength`/`numPieces` 是从 DownloadContext 原样抄来的元数据（RequestGroup.cc L1206-1207）；`totalLength` 虽然也能从 DownloadContext 算出，但快照抄的是 `getTotalLength()` 定稿值（L1192）。快照的意义就在于任务结束后 DownloadContext 已随 RequestGroup 销毁，消费者（RPC、摘要打印、会话保存）只能依赖这些抄录值。

**练习 2**：magnet 下载的第一阶段（元数据任务）的 DownloadResult 有什么特别之处？

**答案**：它被 `markInMemoryDownload()` 标记，快照里 `inMemoryDownload=true`（[src/download_helper.cc:L294](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L294) 设置，RequestGroup.cc L1179 抄录），数据落在内存（ByteArrayDiskWriterFactory）而非磁盘；同时它属于子任务（belongsTo 非零），在摘要表中被跳过，由 followedBy 指向的真正文件任务接棒展示。

## 5. 综合实践

把本讲四个模块串成一个动手任务：**仿照官方单测，构造一个含两个文件的 DownloadContext，并打印它的完整元数据画像**。

1. **实践目标**：用代码证明你掌握了 FileEntry 偏移拼接、getTotalLength/getNumPieces 公式、piece 哈希存取与属性槽读写。
2. **操作步骤**：
   - 复制 [test/DownloadContextTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DownloadContextTest.cc) 的写法，在该文件里临时加一个测试方法（示例代码，验证后请还原）：
     ```cpp
     // 示例代码：构造两个文件的 DownloadContext 并打印元数据画像
     void DownloadContextTest::testMyTwoFileContext()
     {
       std::vector<std::shared_ptr<FileEntry>> files = {
           std::make_shared<FileEntry>("dir/fileA", 1048576, 0),
           std::make_shared<FileEntry>("dir/fileB", 500000, 1048576)};
       DownloadContext ctx;
       ctx.setFileEntries(files.begin(), files.end());
       ctx.setPieceLength(262144); // 256K
       for (auto& fe : ctx.getFileEntries()) {
         printf("file: %s offset=%" PRId64 " length=%" PRId64 "\n",
                fe->getPath().c_str(), fe->getOffset(), fe->getLength());
       }
       printf("totalLength=%" PRId64 "\n", ctx.getTotalLength());
       printf("numPieces=%zu\n", ctx.getNumPieces());
       // 属性槽：挂一个 TorrentAttribute 再读回来
       // （getTorrentAttrs 有 DownloadContext* 重载，见 bittorrent_helper.h:L158）
       auto attr = std::make_shared<TorrentAttribute>();
       attr->name = "my-torrent";
       ctx.setAttribute(CTX_ATTR_BT, attr);
       printf("hasBTAttr=%d name=%s\n", ctx.hasAttribute(CTX_ATTR_BT),
              bittorrent::getTorrentAttrs(&ctx)->name.c_str());
     }
     ```
     （记得在类声明与 `CPPUNIT_TEST_SUITE` 宏里同步登记；`setFileEntries` 的用法可对照 [test/DownloadContextTest.cc:L43](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DownloadContextTest.cc#L43)；示例用到 `TorrentAttribute`/`getTorrentAttrs` 时需 `#include "TorrentAttribute.h"` 与 `#include "bittorrent_helper.h"`。）
   - `./configure --enable...`（若尚未配置）后 `make -j$(nproc)`，再运行 `make check` 或直接执行测试二进制并过滤本用例。
3. **需要观察的现象**：打印出两个文件的路径与偏移；`totalLength` 为 1548576（= 1048576 + 500000，即尾元素 lastOffset）；`numPieces` 为 6（\( \lceil 1548576/262144 \rceil = 6 \)）；`hasBTAttr=1` 且能读回 `name=my-torrent`。
4. **预期结果**：输出与上述数值完全一致；若把 fileB 的 offset 故意写成 0，`getTotalLength()` 会变成 500000——直观体会「总长 = 尾元素 lastOffset」这条约定的另一面。编译与运行输出待本地验证。
5. **思考题（顺带回答）**：为什么这个用例里不必调用 `markTotalLengthIsKnown()`？——因为默认构造已把 `knowsTotalLength_` 置 true（见 4.1.5 练习 2）。

## 6. 本讲小结

- DownloadContext 是任务元数据的聚合根：文件列表（`fileEntries_`）、piece 长度与哈希（`pieceLength_`/`pieceHashes_`）、整文件校验和（`digest_`/`hashType_`）、属性槽（`attrs_`）；下载进度不在它这里（在 PieceStorage）。
- 多文件任务共享一条逻辑偏移空间：FileEntry 按 `(offset, length)` 首尾相接，`getTotalLength()` = 尾元素 lastOffset，`getNumPieces()` = \( \lceil L/p \rceil \)，`findFileEntryByOffset` 用 upper_bound 二分定位。
- ContextAttribute 是「空基类 + 枚举槽位」的极简扩展点，目前唯一真实槽位是 `CTX_ATTR_BT`（TorrentAttribute：infohash、metadata、announceList、urlList 等）；Metalink 走的是公共字段（setDigest/setPieceHashes/setSignature），没有属性槽——同一个对象、两种集成方式。
- `getBasePath` 惰性回退到第一个文件路径，多文件种子用它派生 `.aria2` 控制文件名；`setFileFilter` 把 `--select-file` 翻译成 FileEntry 的 requested 位。
- DownloadResult 是任务定稿后的只读快照，由 `RequestGroup::createDownloadResult()` 唯一生产，属性槽数组与 FileEntry 以 shared_ptr 浅拷贝续命，供控制台摘要、RPC 与会话保存消费。
- `updateDownload` 经 `ownerRequestGroup_` 反向指针把任务流量上报到 RequestGroupMan 全局统计——元数据对象上也有一处关键的运行时联动。

## 7. 下一步学习建议

- **u3-l3（任务创建流水线）**：本讲多次出现 download_helper.cc 的三个创建点，下一讲系统梳理 UriListParser、paramed_string 与协议识别分派的完整流水线。
- **u3-l4（SegmentMan 与分段调度）**：本讲的「逻辑偏移空间」在 SegmentMan 里被进一步切成分给各连接的 segment，两讲合读正好打通「piece ↔ segment」两套切分的关系。
- **u5-l2（PieceStorage）**：看「哪些 piece 完成了」如何在运行时状态层落位，以及 DownloadContext 的 piece 哈希如何被 IteratableChunkChecksumValidator 消费（u5-l5）。
- **u6-l1（torrent 元数据解析）**：本讲只引用了 `processRootDictionary` 的骨架，bencode 解析、AnnounceList 层级等细节在 BT 单元展开。
- **u8-l4（RPC 方法分发）**：回去看 `aria2.tellStatus` 如何把 DownloadResult/DownloadContext 的字段序列化成 JSON，检验你对「谁拥有什么数据」的理解。
