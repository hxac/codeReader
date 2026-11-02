# u6-l11 会话恢复与 .aria2 控制文件

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `.aria2` 控制文件的确切二进制格式：版本号、扩展位、infohash、piece 长度、总长度、上传量、完成位图、在途 piece 明细各占多少字节、按什么顺序排列。
2. 解释 `DefaultBtProgressInfoFile` 的保存策略：为什么先用 SHA1 做内容去重、为什么先写临时文件再改名。
3. 跟踪一条完整的断点续传链路：`createInitialCommand` 读控制文件 → `CheckIntegrityEntry` 家族裁定 → `BtFileAllocationEntry` 收尾 → 下载/做种命令启动。
4. 区分恢复时的三种策略——直接信任位图、全量分块校验、`--bt-seed-unverified` 全部标记完成——并说出各自的触发条件与代价。
5. 列出控制文件在整个生命周期中被保存与被删除的全部时机，理解 `--force-save` 对做种任务的意义以及 `--bt-detach-seed-only` 的切入位置。

## 2. 前置知识

本讲是 BitTorrent 单元（单元 6）与磁盘 I/O 单元（单元 5）的汇合点，需要以下已建立的概念：

- **piece 与位图（u3-l4、u5-l2）**：文件总字节流按 piece 切分，piece 内再切 16KiB 的 block；`BitfieldMan` 用「完成位图」记录哪些 piece 已落盘，`PieceStorage` 是位图的持有者。控制文件保存的核心内容就是这张位图。
- **在途 piece（u5-l2）**：`DefaultPieceStorage` 的 `usedPieces_` 集合中那些「领出去了但还没写满」的 `Piece` 对象。每个在途 piece 自带一张 block 级小位图，记录 piece 内部已写入哪些块。控制文件会把它们逐个序列化。
- **可迭代校验器（u5-l5）**：`IteratableValidator` 契约（`init` → 反复 `validateChunk` → `finished`），由 `CheckIntegrityCommand` 每轮推进一小步，避免阻塞事件循环；`CheckIntegrityMan` 是保证全局同时只有一个任务在校验的「单槽停车场」。
- **文件预分配停车场（u5-l4）**：`FileAllocationMan` + `FileAllocationEntry`，校验结束后任务还要经过这道闸门才正式开始下载。
- **BT 运行时装配（u6-l2）**：`BtRegistry` 六件套（DownloadContext、PieceStorage、PeerStorage、BtAnnounce、BtRuntime、BtProgressInfoFile）——注意控制文件对象本来就是六件套之一。
- **BtRuntime 与做种（u6-l10）**：`BtRuntime` 记录会话级运行时状态；`RequestGroup::enableSeedOnly` 由 `--bt-detach-seed-only` 控制，把做种任务摘出 `-j` 并发计数。
- **写盘缓存（u5-l6）**：`--disk-cache` 开启时，未写满的 piece 数据滞留在 `WrDiskCache`，保存控制文件前必须先落盘，否则位图会说谎。

一个容易误解的事实先澄清：**`DefaultBtProgressInfoFile` 名字里带 Bt，但它服务所有协议**。HTTP/FTP 单文件下载（`RequestGroup.cc` 的非 BT 分支）同样创建它来写 `.aria2` 文件，只是不保存 infohash 和上传量这两个 BT 专属字段。这个名字是历史遗留。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/DefaultBtProgressInfoFile.cc/.h` | `.aria2` 控制文件的格式定义：save/load/removeFile/exists 的全部实现 |
| `src/RequestGroup.cc` | 恢复链路的入口：`createInitialCommand` 的 BT 分支、`processCheckIntegrityEntry` 的三条件闸门、`saveControlFile`/`removeControlFile` |
| `src/CheckIntegrityEntry.cc/.h` | 校验条目基类：持有 validator，提供 `proceedFileAllocation` |
| `src/PieceHashCheckIntegrityEntry.cc` | 中间层：绑定 `IteratableChunkChecksumValidator`（分块哈希校验） |
| `src/BtCheckIntegrityEntry.cc` | BT 专属校验条目：校验后转入 `BtFileAllocationEntry` |
| `src/StreamCheckIntegrityEntry.cc` | HTTP/FTP 专属校验条目：校验后转入 `StreamFileAllocationEntry` |
| `src/BtFileAllocationEntry.cc` | BT 收尾棒：装配 `BtSetup`、创建下载命令或切入做种 |
| `src/CheckIntegrityCommand.cc` | 事件循环里推进校验的命令，保存开关在这里恢复 |
| `src/RequestGroupMan.cc` | 控制文件的周期保存（autosave）与完成后的删除 |
| `src/AutoSaveCommand.cc` | 周期触发 `RequestGroupMan::save()` 的例程命令 |
| `src/IteratableChunkChecksumValidator.cc` | 逐 piece 读盘比对的校验器实现 |
| `src/DefaultPieceStorage.cc` | `setBitfield`/`addInFlightPiece`/`onDownloadIncomplete` 等被恢复链路调用的出口 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**4.1 控制文件格式与读写**、**4.2 恢复链路与 CheckIntegrityEntry 家族**、**4.3 BtFileAllocationEntry 收尾棒**、**4.4 控制文件的生命周期**。

### 4.1 DefaultBtProgressInfoFile：控制文件的磁盘格式

#### 4.1.1 概念说明

aria2 断点续传的前提是：**下载进度必须独立于进程生命周期持久化**。内存里的进度只存在于 `BitfieldMan` 的位图和 `usedPieces_` 集合中，进程一退就没了。`.aria2` 控制文件（aria2 文档里也叫 segment file，源码日志沿用这个旧称）就是这份内存进度的磁盘快照。

它要回答四个问题：

1. 哪些 piece 已经完整落盘？（完成位图）
2. 哪些 piece 写了一半、各自写到哪个块？（在途 piece 明细）
3. 这次恢复的是不是同一个任务？（infohash、piece 长度、总长度三项一致性检查）
4. 做种分享率要跨会话累计，上次会话之前总共传了多少字节？（历史上传量）

文件名是「数据文件路径 + `.aria2` 后缀」：构造函数用 [DefaultBtProgressInfoFile.cc:70-77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtProgressInfoFile.cc#L70-L77) 的 `createFilename` 拼出——单文件任务就是 `file.ext.aria2`，多文件种子则是种子根目录名的 `.aria2`。后缀常量定义在头文件里：[DefaultBtProgressInfoFile.h:96-100](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtProgressInfoFile.h#L96-L100)，就是字符串 `".aria2"`。`updateFilename()` 则在自动改名（`--auto-file-renaming`）后重算路径。

是否为 BT 下载由 `isTorrentDownload()` 判定——它的实现很轻：只看有没有注入 `BtRuntime` 指针，见 [DefaultBtProgressInfoFile.cc:96-103](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtProgressInfoFile.cc#L96-L103)。

#### 4.1.2 核心流程

`save(IOFile&)` 按下表顺序写入字段（所有多字节整数自版本 0001 起为**网络字节序**，即大端）：

| 偏移 | 字段 | 长度 | 说明 |
| --- | --- | --- | --- |
| 0 | version | 2 字节 | 固定 `{0x00, 0x01}`，即 `0x0001` |
| 2 | extension | 4 字节 | BT 下载置 `extension[3] = 1`，否则全零 |
| 6 | infoHashLength | 4 字节 | BT 为 20；非 BT 为 0（此时后面不写 infohash） |
| 10 | infoHash | 20 字节（仅 BT） | 种子 infohash，恢复时用于「同一任务」校验 |
| 10/30 | pieceLength | 4 字节 | piece 长度 |
| +4 | totalLength | 8 字节 | 总长度 |
| +8 | uploadLength | 8 字节 | `历史上传量 + 本次会话上传量`（做种分享率的分子跨会话累计） |
| +8 | bitfieldLength | 4 字节 | 完成位图字节数 |
| +4 | bitfield | bitfieldLength 字节 | 全局完成位图，1 个 bit 代表 1 个 piece |
| 尾部 | numInFlightPiece | 4 字节 | 在途 piece 个数 |
| 尾部 | 每个在途 piece | 4+4+4+变长 | index、length、块位图长度、块位图 |

位图长度不是随口写的，load 时会用公式反推验证。设总长度为 \( L \)、piece 长度为 \( P \)，则 piece 数为 \( \lceil L/P \rceil \)，位图字节数为：

\[ \left\lceil \frac{\lceil L/P \rceil}{8} \right\rceil \]

保存路径的两级结构值得注意：

```text
save() 无参版本
  ├── 1. 把同样的字节流写进 SHA1IOFile（只算哈希不落盘）
  ├── 2. digest 与上次相同 → 直接返回（磁盘无写入）
  ├── 3. 不同 → 写 <filename>.aria2__temp
  └── 4. renameTo 原子替换正式文件
```

#### 4.1.3 源码精读

**格式写入**。版本与扩展位在 [DefaultBtProgressInfoFile.cc:118-130](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtProgressInfoFile.cc#L118-L130)：版本写死 `0x0001`，BT 下载把 `extension[3]` 置 1。注释说明「自版本 0001 起整数以二进制网络字节序保存」——版本 0000（host 字节序）只存在于很老的 aria2 中，load 仍兼容。

**infohash 与上传量**。BT 分支写入 20 字节 infohash，见 [DefaultBtProgressInfoFile.cc:131-147](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtProgressInfoFile.cc#L131-L147)；上传量在 [DefaultBtProgressInfoFile.cc:154-162](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtProgressInfoFile.cc#L154-L162)，公式是 `btRuntime_->getUploadLengthAtStartup() + 本次会话上传量`——上次启动时恢复进来的历史值加上本会话新传的值，滚雪球式累计。这正是 u6-l10 讲过的 `--seed-ratio` 分子能跨进程累计的数据来源。

**位图与在途 piece**。[DefaultBtProgressInfoFile.cc:163-187](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtProgressInfoFile.cc#L163-L187) 先写全局完成位图，再写 `countInFlightPiece()` 个在途 piece，每个 piece 序列化四元组 `(index, length, 块位图长度, 块位图)`。有了块位图，恢复后一个写到一半的 piece 能精确到 16KiB 块继续，而不是整个 piece 重下。

**SHA1 去重与原子替换**。[DefaultBtProgressInfoFile.cc:193-224](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtProgressInfoFile.cc#L193-L224) 是无参 `save()`：先把序列化流喂给 `SHA1IOFile`（一个只更新 MessageDigest、不做任何文件 I/O 的 `IOFile` 装饰器，见 [SHA1IOFile.h:45-51](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SHA1IOFile.h#L45-L51)），摘要与 `lastDigest_` 相同就跳过写盘——头文件注释直言这是为了不唤醒可能休眠的磁盘（[DefaultBtProgressInfoFile.h:61-64](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtProgressInfoFile.h#L61-L64)）。内容有变化时写 `__temp` 后缀的临时文件再 `renameTo`，改名在 POSIX 上是原子操作，保证任何时刻磁盘上的正式控制文件都是完整版本，不会出现写一半崩溃导致控制文件损坏。

**读取与四道一致性检查**。`load()` 在 [DefaultBtProgressInfoFile.cc:234-403](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtProgressInfoFile.cc#L234-L403)，依次做：

1. 版本号只认 `0000`/`0001`（L241-254），否则抛「Unsupported ctrl file version」；
2. infohash 逐字节比对当前种子的 infohash（L263-286），不匹配说明这个 `.aria2` 属于别的任务；
3. piece 长度非零、总长度必须与当前 `DownloadContext` 完全一致（L288-307）；
4. 位图长度必须等于按公式反推的期望值（L319-329），防止文件被截断或篡改。

全部通过后，[DefaultBtProgressInfoFile.cc:331-381](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtProgressInfoFile.cc#L331-L381) 执行恢复的核心三步：`pieceStorage_->setBitfield()` 把磁盘位图直接灌回内存；逐个重建 `Piece(index, length)` 对象并用 `setBitfield` 恢复块位图、`setHashType` 恢复哈希算法；最后 `addInFlightPiece` 一次性塞回 `usedPieces_` 集合（该函数在 [DefaultPieceStorage.cc:790-802](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L790-L802)）。上传量则在 [DefaultBtProgressInfoFile.cc:308-317](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtProgressInfoFile.cc#L308-L317) 写回 `btRuntime_->setUploadLengthAtStartup()`。

**piece 长度变了的兜底**。若保存时的 piece 长度与当前不同（例如换了制作工具重新发布同内容种子），走 [DefaultBtProgressInfoFile.cc:383-401](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtProgressInfoFile.cc#L383-L401)：默认直接抛 `PIECE_LENGTH_CHANGED` 错误；用户显式给 `--allow-piece-length-change=true` 时，用 `util::convertBitfield` 把旧粒度位图换算成新粒度位图再灌回（换算按字节覆盖关系推导，边界处可能损失进度，报错信息里也这么警告）。

#### 4.1.4 代码实践

**实践目标**：亲眼验证控制文件的二进制格式。

**操作步骤**：

1. 找（或自建）一个小型种子开始下载，下载中或中断后确认数据文件旁存在 `xxx.aria2`。
2. 用 `xxd` 查看前 64 字节（示例命令，路径按实际替换）：
   ```console
   $ xxd -l 64 ubuntu-xx.iso.aria2
   ```
3. 手工解码：第 0-1 字节应为 `0001`；第 5 字节（extension 末字节）BT 下载应为 `01`；第 6-9 字节大端读出应为 20；随后 20 字节是 infohash——拿 `xxd` 输出与 aria2 日志里的 infohash（或 `aria2c --show-files` / magnet 的 `xt` 值）比对。
4. 用 `stat -c %s` 取文件总大小 \( S \)，按格式表推算：\( S = 30 + 4 + 8 + 8 + 4 + B + 4 + \sum(\text{在途 piece}) \)，其中 \( B \) 是位图字节数，解出 \( B \) 后与 \( \lceil\lceil L/P\rceil/8\rceil \) 对照。

**需要观察的现象**：xxd 前几行的字节与上表逐字段吻合；infohash 十六进制串与种子一致；推算出的位图长度吻合。

**预期结果**：字段全部对上，说明你已经能脱离源码读懂这个文件。**具体数值待本地验证**（我没有运行环境，无法替你产生真实的 xxd 输出）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `save()` 要先写 `__temp` 文件再改名，而不是直接覆写原文件？

**答案**：直接覆写过程中若进程崩溃或断电，控制文件会是半新半旧的混合体，恢复时位图与实际磁盘数据不一致，可能把坏数据当已下载数据。`rename` 在同一文件系统上是原子的，任意时刻正式文件要么是旧完整版、要么是新完整版。

**练习 2**：`uploadLength` 字段保存的是「本次会话上传量」吗？

**答案**：不是。它保存 `getUploadLengthAtStartup() + 本次会话上传量`，即从第一次下载起累计的历史总上传量。恢复时经 `setUploadLengthAtStartup` 写回 BtRuntime，下次保存再叠加，形成跨会话滚雪球。这是 `--seed-ratio` 分子跨重启不丢的机制。

**练习 3**：恢复时发现 totalLength 与当前种子不一致会发生什么？为什么必须失败而不是重新换算？

**答案**：抛 `DL_ABORT_EX("total length mismatch...")`，任务以错误终止。位图的每个 bit 映射一段固定字节区间，总长度变了意味着 piece 数、位图长度全变，旧位图与新文件的字节区间无法对应，语义上不可换算。（对比：piece 长度变化尚可按字节覆盖关系换算，因为总字节流没变。）

### 4.2 恢复链路：从 createInitialCommand 到 CheckIntegrityEntry 家族

#### 4.2.1 概念说明

控制文件只是一份静态快照；把它变成「接着上次下」的，是一条由 `RequestGroup::createInitialCommand` 发起的三棒接力：

```text
createInitialCommand（BT 分支）
  ├── 第 0 棒：清理失效控制文件 + 决定打开模式 + load() 位图
  ├── 第 1 棒：BtCheckIntegrityEntry —— 要不要校验？校验后是完成还是未完成？
  └── 第 2 棒：BtFileAllocationEntry —— 装配 BtSetup、发下载命令或切入做种
```

`CheckIntegrityEntry` 家族是第 1 棒的主角，类层次如下：

```text
CheckIntegrityEntry                    （持有 IteratableValidator；定义两个纯虚出口）
├── ChecksumCheckIntegrityEntry        （整文件摘要校验 → IteratableChecksumValidator）
└── PieceHashCheckIntegrityEntry       （分块哈希校验 → IteratableChunkChecksumValidator）
    ├── StreamCheckIntegrityEntry      （HTTP/FTP；下一棒 StreamFileAllocationEntry）
    └── BtCheckIntegrityEntry          （BT；下一棒 BtFileAllocationEntry）
```

三个分支的差异本质上是两个问题的答案：**用什么校验**（整文件一个哈希 vs 逐 piece 哈希）与**校验完去哪**（下一棒是谁）。

#### 4.2.2 核心流程

BT 任务启动时第 0 棒的判定树：

```text
removeDefunctControlFile：控制文件在、数据文件不在 → 删控制文件（失效）
实际文件大小 == 总长度 → DiskAdaptor 开只读模式（支持只读介质做种）
否则 → 可写模式打开（允许截断/补写）

if 控制文件存在:
    progressInfoFile->load()          # 位图 + 在途 piece + 上传量灌回内存
    openFile()
elif 数据文件存在:
    若无 --check-integrity 且无 --allow-overwrite 且无 --bt-seed-unverified
        → 抛 FILE_ALREADY_EXISTS
    openFile()
    若 --bt-seed-unverified → markAllPiecesDone()   # 全部标记完成，跳过一切校验
else:
    openFile()                        # 全新下载

BtCheckIntegrityEntry 上场：
    if --bt-seed-unverified 且 downloadFinished():
        onDownloadFinished()          # 直接进完成分支
    else:
        processCheckIntegrityEntry()  # 三条件闸门
```

`processCheckIntegrityEntry` 的闸门（对 BT 与流式下载通用）：

```text
实际文件大小 > 总长度 → cutTrailingGarbage() 截掉尾部垃圾

if (--check-integrity=true 或 整文件校验尚未做) 且 isValidationReady():
    initValidator()                  # 构造 IteratableChunkChecksumValidator
    disableSaveControlFile()         # 校验期间不保存控制文件
    push 进 CheckIntegrityMan 停车场  # CheckIntegrityCommand 逐轮推进
else:
    entry->onDownloadIncomplete()    # 不校验，直接信任刚 load 进来的位图
```

校验器推进与出闸由 `CheckIntegrityCommand::executeInternal` 驱动：每轮 `validateChunk()` 校验若干 piece，`finished()` 为真时**恢复控制文件保存开关**，然后按 `downloadFinished()` 分流到 `onDownloadFinished` 或 `onDownloadIncomplete`。

#### 4.2.3 源码精读

**第 0 棒**。BT 分支里控制文件对象的创建与装配在 [RequestGroup.cc:313-332](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L313-L332)：只有非 metadataGetMode（即不是 magnet 拉元数据的临时任务，u6-l9）才创建，并把新生的 `BtRuntime`、`PeerStorage` 注入进去——`isTorrentDownload()` 由此为真。清理失效控制文件的 `removeDefunctControlFile` 在 [RequestGroup.cc:409](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L409) 调用，实现见 [RequestGroup.cc:718-729](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L718-L729)：数据文件没了、控制文件还在，说明用户删了数据文件，留着只会误导下次恢复，于是删除并打日志 `Removed the defunct control file...`。

只读判定在 [RequestGroup.cc:410-424](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L410-L424)：实际大小与总长相等时先 `enableReadOnly`，让 aria2 能对只读介质（刻录盘、只读挂载）里的完整数据直接做种。若恢复后判定还要继续下载，`BtCheckIntegrityEntry::onDownloadIncomplete` 会先关文件、关只读、重开（见下）。

三分支加载在 [RequestGroup.cc:426-448](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L426-L448)：控制文件在就 `load()`；只有数据文件在时，`--check-integrity`、`--allow-overwrite`、`--bt-seed-unverified` 三者一个都没有就抛 `FILE_ALREADY_EXISTS`（错误码 13），给了其中之一则打开文件，且 `--bt-seed-unverified` 直接 `markAllPiecesDone()`——这正是该选项文档所述「不做哈希校验直接做种」的落点。最后 [RequestGroup.cc:449-460](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L449-L460) 创建 `BtCheckIntegrityEntry`：`--bt-seed-unverified` 且已完成则直呼 `onDownloadFinished` 跳过校验，否则交 `processCheckIntegrityEntry`。

对照流式下载：HTTP/FTP 单文件走 [RequestGroup.cc:465-481](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L465-L481) 的 `createCheckIntegrityEntry`（[RequestGroup.cc:221-269](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L221-L269)），它比 BT 分支多一种出口：控制文件显示已完成且整文件校验未做过时返回 `ChecksumCheckIntegrityEntry`（置 `redownload_`）；多文件 Metalink 分支则在 [RequestGroup.cc:503-526](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L503-L526) 以几乎相同的三分支逻辑挂 `StreamCheckIntegrityEntry`。注意流式路径若控制文件不存在但数据文件在且给了 `-c`，会 `markPiecesDone(outfile.size())` 把已下载前缀标记完成（[RequestGroup.cc:744-750](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L744-L750)）——**BT 分支没有这个 `-c` 后备**，BT 的进度完全以控制文件位图为准。

**闸门与停车场**。[RequestGroup.cc:529-553](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L529-L553) 是 `processCheckIntegrityEntry` 全文：先 `cutTrailingGarbage`；进入校验的条件是「`--check-integrity` 为真 **或** 整文件校验尚需进行」**且** `isValidationReady()`。对普通 BT 恢复（不带 `--check-integrity`）：整文件校验通常不可用（torrent 没有 md5/sha1 整文件摘要）→ 条件为假 → **直接走 `onDownloadIncomplete`，即无条件信任刚 load 进来的位图，一个字节的哈希都不算**。这是默认恢复快的原因。进入校验分支时的 `disableSaveControlFile()` 有段长注释（L541-547）：校验期间用户按 Ctrl-C 的话，位图还没攒齐，若把半截位图存进控制文件，用户下次会看到进度归零，不如不存；保存开关在校验完成后由 `CheckIntegrityCommand` 恢复（[CheckIntegrityCommand.cc:70-74](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CheckIntegrityCommand.cc#L70-L74)，注释明确指向这里）。

**家族三个分支**。校验什么：`PieceHashCheckIntegrityEntry::isValidationReady` 要求分块哈希可用（[PieceHashCheckIntegrityEntry.cc:52-66](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PieceHashCheckIntegrityEntry.cc#L52-L66)），`initValidator` 构造 `IteratableChunkChecksumValidator`；`ChecksumCheckIntegrityEntry` 则绑定整文件的 `IteratableChecksumValidator`（[ChecksumCheckIntegrityEntry.cc:56-70](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ChecksumCheckIntegrityEntry.cc#L56-L70)）。校验完去哪：`StreamCheckIntegrityEntry::onDownloadIncomplete` 下一棒是 `StreamFileAllocationEntry` 并捎带 `nextCommand`（[StreamCheckIntegrityEntry.cc:53-65](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/StreamCheckIntegrityEntry.cc#L53-L65)）；`BtCheckIntegrityEntry::onDownloadIncomplete` 下一棒是 `BtFileAllocationEntry`（[BtCheckIntegrityEntry.cc:56-73](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtCheckIntegrityEntry.cc#L56-L73)），且多了三件 BT 专属事务：调 `ps->onDownloadIncomplete()` 通知选择器位图已初始化（实现是重置流式 piece 选择器的缓存状态，[DefaultPieceStorage.cc:844-847](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L844-L847)）、`--hash-check-only` 时到此为止不再下载、以及若第 0 棒开了只读模式则关文件→关只读→重开（L64-70）。完成分支 [BtCheckIntegrityEntry.cc:75-95](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtCheckIntegrityEntry.cc#L75-L95) 在 `--bt-enable-hook-after-hash-check` 时触发 `--on-bt-download-complete` 钩子与 `EVENT_ON_BT_DOWNLOAD_COMPLETE` 通知，且 `--bt-hash-check-seed` 时也走文件分配棒——为的是让完整校验通过的已有文件直接进入做种。

**校验器如何对待已下载 piece**。这是本讲实践任务的核心问题，答案在 [IteratableChunkChecksumValidator.cc:70-104](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/IteratableChunkChecksumValidator.cc#L70-L104)：`init()` 把**本地**位图整体清零、下标归零（L120-125），随后 `validateChunk()` 从 piece 0 开始**逐个**读盘算哈希与种子里的 piece hash 比对——也就是说它并不「跳过」位图中已完成的 piece，而是不信任旧位图、全部重算；比对结果攒在本地位图里，直到最后一个 piece 校验完才一次性 `setBitfield` 写回（L99-102）。读文件越界（文件比期望短）会抛异常被捕获、该 piece 清位（L89-96），所以短缺的 piece 自动标记为待重下。三策略总结：

| 恢复策略 | 触发条件 | 已下载 piece 的命运 | 代价 |
| --- | --- | --- | --- |
| 直接复用位图 | 默认（无 `--check-integrity`），控制文件有效 | `load()` 灌回，**零读盘零校验**，续传从位图缺口继续 | 信任磁盘；数据若被外部损坏不会被察觉 |
| 全量分块校验 | `--check-integrity=true` | 全部 piece 重算哈希；好块保留、坏块清位重下 | 读盘一遍 + 全量哈希 |
| 全部标记完成 | `--bt-seed-unverified=true` 且数据文件大小匹配 | `markAllPiecesDone`，无任何读盘校验 | 完全信任，坏块靠 peer 侧校验兜底 |

#### 4.2.4 代码实践

**实践目标**：用日志区分「直接复用位图」与「全量校验」两条恢复路径。

**操作步骤**：

1. 正常下载一个种子，进度过半后按一次 Ctrl-C 优雅停止（u2-l1 讲过首按优雅、再按强杀——务必只按一次，让 aria2 走完停机保存）。
2. 确认数据文件旁存在 `.aria2` 文件。
3. **第一次恢复**：原命令重跑，加 `-l resume1.log --log-level=info`。在日志中找 `Loading the segment file`（message.h L114）与紧随其后的下载进度起始百分比。
4. **第二次恢复**：再次中断后，改用 `aria2c --check-integrity=true -l resume2.log --log-level=info <同一个种子>` 重跑，观察控制台出现的 `[#hash-check]` 进度行与日志中的 `Verification finished successfully`（message.h L143）。

**需要观察的现象**：第一次恢复几乎瞬间进入下载且起始进度等于中断前进度（没有 hash check 阶段）；第二次恢复先出现 hash check 进度行，从 0% 涨到 100% 后才转入下载，且已验证完好的 piece 不再重下。

**预期结果**：两条日志轨迹分别对应 `processCheckIntegrityEntry` 的 else 分支（直接 `onDownloadIncomplete`）与停车场分支（`initValidator` + `disableSaveControlFile` + CheckIntegrityMan）。**具体日志行待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `disableSaveControlFile` 必须配对地在 `CheckIntegrityCommand` 里恢复，而不是校验器自己恢复？

**答案**：保存开关 `saveControlFile_` 是 `RequestGroup` 的状态（[RequestGroup.h:459-461](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.h#L459-L461)），而校验器只认识 DownloadContext 与 PieceStorage，看不到 RequestGroup；驱动校验的 `CheckIntegrityCommand` 恰好持有 RequestGroup，且它能在 `finished()` 成立的那一刻（校验结果已写回位图）恢复开关，时序最准。

**练习 2**：magnet 链接的元数据任务（u6-l9）会写 `.aria2` 控制文件吗？

**答案**：不会。`createInitialCommand` 的 BT 分支只在 `!metadataGetMode` 时才创建 `DefaultBtProgressInfoFile`（[RequestGroup.cc:313-317](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L313-L317)）。元数据任务数据全内存、总长未知，本就没有可持久化的位图；它派生出的真正数据任务才有控制文件。

**练习 3**：`--check-integrity=true` 恢复时，控制文件位图里标记完成、但磁盘数据实际被篡改的 piece 会怎样？

**答案**：被重算哈希后发现不匹配，本地新位图中清零，最终一次性写回后该 piece 回到待下载集合，aria2 会重新下载它（若再失败可被 peer 侧哈希再兜底）。旧位图在校验期间完全不参与判定，只在校验完成后被整体替换。

### 4.3 BtFileAllocationEntry：从恢复裁定到下载/做种的最后一棒

#### 4.3.1 概念说明

无论第 1 棒的结论是「未完成」还是「已完成」，BT 任务的下一棒都是 `BtFileAllocationEntry`。它的职责不是分配文件本身（那是 u5-l4 讲的停车场机制），而是充当**恢复链路的总装出口**：把校验阶段拿到的结论变成真正跑起来的命令——未完成就发下载命令，已完成就切入做种。

`CheckIntegrityEntry::proceedFileAllocation` 是转交接力的枢纽（[CheckIntegrityEntry.cc:84-94](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CheckIntegrityEntry.cc#L84-L94)）：`needsFileAllocation()` 为真（选项非 none、总长达到 `--no-file-allocation-limit` 阈值、分配迭代器未完成，见 [RequestGroup.cc:1163-1169](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L1163-L1169)）就进 `FileAllocationMan` 停车场排队（u5-l4：dispatcher 铸造 worker 推进分配，完成后调 `prepareForNextAction`）；为假就直接调 `prepareForNextAction`，一棒到位。

#### 4.3.2 核心流程

`BtFileAllocationEntry::prepareForNextAction` 的判定树：

```text
BtSetup().setup(commands, rg, e, option)      # 装配种子级周期命令（u6-l2）
若 --enable-mmap 且分配方式非 none 且大小未超 --max-mmap-limit
    → diskAdaptor->enableMmap()

if 下载未完成:
    resetDownloadStartTime()                  # 校验/分配耗时不应计入会话计时
    if 任一选中 FileEntry 带外链 URI（web-seeding）
        → createNextCommandWithAdj(...)       # 同时发 HTTP 镜像命令
    if --auto-save-interval != 0
        → saveControlFile()                   # 写下第一份控制文件
else:
    （Windows：关文件→开只读→重开，避免做种锁住文件）
    enableSeedOnly()                          # --bt-detach-seed-only 的切入点
```

#### 4.3.3 源码精读

总装与 mmap 在 [BtFileAllocationEntry.cc:60-74](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtFileAllocationEntry.cc#L60-L74)：`BtSetup().setup` 装配 TrackerWatcher、PeerChoke、ActivePeerConnection、SeedCheck 等种子级命令并绑定端口（细节在 u6-l2 已拆解）。未完成分支在 [BtFileAllocationEntry.cc:75-93](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtFileAllocationEntry.cc#L75-L93)：`resetDownloadStartTime` 的注释解释了为什么要在这里重置——`createInitialCommand` 里先起过一次会话计时，而哈希校验和文件分配可能耗时很久，不应计入超时；`isUriSuppliedForRequsetFileEntry`（定义在 [FileEntry.h:290-293](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FileEntry.h#L290-L293)，函数名里的拼写错误 Requset 是源码原貌）判定种子是否带 web-seeding URI，带则额外发 HTTP 命令实现 HTTP+BT 混合下载；`--auto-save-interval != 0` 才写**初始控制文件**——刚分配完、一个字节没下也要存，这样哪怕进程在下载头几秒就崩溃，下次恢复也有一份合法的控制文件可续。

完成分支在 [BtFileAllocationEntry.cc:94-109](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtFileAllocationEntry.cc#L94-L109)：Windows 下关文件、开只读、重开（注释解释：以写权限打开的文件会让别的程序在做种期间无法打开）；然后 `rg->enableSeedOnly()`。`enableSeedOnly` 的实现（[RequestGroup.cc:1325-1337](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L1325-L1337)）先看 `--bt-detach-seed-only`：选项没开就直接返回（做种任务仍占 `-j` 名额）；开了才置 `seedOnly_`、`decreaseNumActive()` 归还并发名额并触发 `requestQueueCheck` 让等待队列里的下一个任务顶上——这正是 u6-l10 所说「做种任务摘出并发计数」的代码落点。注意本讲视角下它的另一层意义：**控制文件恢复出一个已完成的任务时，最后一棒不是下载而是做种**。

#### 4.3.4 代码实践

**实践目标**：观察「恢复已完成种子 → 直接做种」的路径。

**操作步骤**：

1. 完整下载一个小种子（校验通过、进入做种）后 Ctrl-C 停止，此时数据完整、`.aria2` 是否还在取决于 `--force-save`（见 4.4）。
2. 用原命令加 `--seed-time=0.01 -l seed.log --log-level=info` 重跑：因为 `--check-integrity` 未开、位图/文件大小判定为完成，任务应直接进入做种并在约 36 秒后退出。
3. 在日志中确认没有出现 hash check 进度行、没有产生下载流量，`enableSeedOnly` 相关效果为控制台立即显示做种（SEED）状态。
4. 改加 `--bt-detach-seed-only=true` 再跑一次，同时再加一个 `-j1` 的第二个下载任务，观察做种任务是否立即让出并发名额。

**需要观察的现象**：恢复即做种、无校验阶段；`--bt-detach-seed-only` 下第二个任务无需等做种结束就开始下载。

**预期结果**：与 `BtFileAllocationEntry.cc:94-109` 的 else 分支行为一致。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `prepareForNextAction` 里保存初始控制文件要以 `--auto-save-interval != 0` 为前提？`--auto-save-interval=0` 语义是什么？

**答案**：文档（`doc/manual-src/en/aria2c.rst` L1222-1228）明确：该选项为 0 表示「下载期间不保存控制文件，但停止时仍会保存」。既然用户明确表示下载期间不要周期性控制文件，初始保存也一并跳过；停止时的保存不受影响。

**练习 2**：`--bt-hash-check-seed=true`（配合 `--check-integrity=true`）对「磁盘上已有完整文件但从未用 aria2 记录过」的场景有什么用？

**答案**：全量校验通过后 `downloadFinished()` 为真，`BtCheckIntegrityEntry::onDownloadFinished`（[BtCheckIntegrityEntry.cc:90-94](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtCheckIntegrityEntry.cc#L90-L94)）会照样走文件分配棒，`prepareForNextAction` 的完成分支切入做种。合起来即「把别处下好的文件校验一遍后直接做种」。

**练习 3**：`StreamFileAllocationEntry` 与 `BtFileAllocationEntry` 同为 FileAllocationEntry 子类，为什么 BT 版要做 `BtSetup().setup` 而流式版不需要？

**答案**：BT 下载的执行者是 peer 交互命令、tracker 命令等一整套种子级周期命令，它们只有任务进入下载前才被装配（u6-l2 的 `BtSetup::setup`），恢复链路把这一步安排在文件分配完成后；HTTP/FTP 的执行者只是普通的协议命令链（u4-l1），由 `createNextCommand` 直接生成，无需额外装配。

### 4.4 控制文件的生命周期：谁在保存、谁在删除

#### 4.4.1 概念说明

前面三个模块讲的是「一次保存与一次恢复」。把视角拉到整个进程生命周期，控制文件经历「创建 → 周期性更新 → 终局（删除或最后保存）」三个阶段，由四类角色协作：

- **初始保存**：文件分配完成后（4.3 已述）。
- **周期保存**：`AutoSaveCommand`（TimeBasedCommand，间隔即 `--auto-save-interval`，默认 60 秒）每 tick 调 `RequestGroupMan::save()`。
- **终局保存/删除**：任务被收割（`numCommand` 归零）时，按暂停 / 完成 / 未完成三种结局处理。
- **保存开关**：`saveControlFile_` 布尔闸门，哈希校验期间关闭（4.2 已述）。

#### 4.4.2 核心流程

```text
saveControlFile() 的前置冲洗（保证位图不说谎）:
    flushWrDiskCacheEntry(false)   # 把写盘缓存中该任务的数据落盘（u5-l6）
    flushOSBuffers()               # 冲洗 OS 页缓存
    progressInfoFile_->save()      # 再写控制文件

RequestGroupMan::save()（周期保存，对每个活动任务）:
    if allDownloadFinished 且 无待做整文件校验 且 未开 --force-save:
        removeControlFile()        # 完成 = 控制文件使命结束
    else:
        saveControlFile()

收割时（numCommand == 0）:
    closeFile()
    暂停   → saveControlFile()，任务回等待队列
    完成   → allDownloadFinished 且 !--force-save → removeControlFile() + 保存 .sig 签名
             否则 → saveControlFile()
    未完成 → saveControlFile()     # Ctrl-C / 出错都走这里，这就是断点续传的来源
```

#### 4.4.3 源码精读

**前置冲洗**。[RequestGroup.cc:1291-1300](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L1291-L1300)：保存控制文件前先把写盘缓存里属于该任务的数据 flush 落盘（参数 `false` 表示保留缓存条目，见 u5-l6 的五时机清单），再 `flushOSBuffers`。顺序至关重要——若先写控制文件后落盘数据，崩溃窗口内位图声称「piece N 已完成」而数据还在内存里，恢复后会跳过一个实际缺失的 piece。`removeControlFile` 则是一行删除（L1302-1305）。

**周期保存与删除**。`AutoSaveCommand::process` 每 tick 调 `RequestGroupMan::save()`（[AutoSaveCommand.cc:49-60](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AutoSaveCommand.cc#L49-L60)），其 `preProcess` 在全部任务结束或引擎停机时让自己退场。`save()` 全文在 [RequestGroupMan.cc:595-612](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L595-L612)：完成的任务删控制文件，否则保存。注意推论：**做种中的 BT 任务处于「已完成」状态（位图全满），若未开 `--force-save`，第一个 autosave tick 就会删掉它的控制文件**——手册对 `--force-save` 的描述（[doc/manual-src/en/aria2c.rst:1389-1395](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/doc/manual-src/en/aria2c.rst#L1389-L1395)）点名了这一点：「保存被识别为已完成状态的 BitTorrent 做种可能有用」。想在做种中断后继续累计 `--seed-ratio` 分子（uploadLength），就要靠它保住控制文件。

**收割三分支**。[RequestGroupMan.cc:370-429](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L370-L429)：先 `closeFile()`（内部同样先 flush 写盘缓存再关文件，[RequestGroup.cc:210-217](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L210-L217)）；暂停（RPC `aria2.pause`）→ 保存控制文件、任务回等待队列；完成且无待校验 → `applyLastModifiedTime`、报告完成，`allDownloadFinished` 且未开 `--force-save` 时 `removeControlFile` 并尝试保存 `.sig` 签名文件（`--save-session` 体系也在同条件下不再记录该任务）；否则保存。**未完成分支（L423-429）就是断点续传的诞生地**：Ctrl-C 优雅停机或普通出错都会走到这里，留下（或更新）控制文件。

**顺带一提的失效清理**。除了收割时删除，启动时的 `removeDefunctControlFile`（4.2 已述）负责清掉「数据文件已被用户删除」的孤儿控制文件，两种删除的语义不同：前者是「使命完成」，后者是「数据源没了、留着有害」。

#### 4.4.4 代码实践

**实践目标**：亲眼看到控制文件周期性更新与完成后的删除。

**操作步骤**：

1. 用 `--auto-save-interval=10 -l cycle.log --log-level=info` 下载一个大种子。
2. 下载期间 `watch -n 12 'ls -l --time-style=+%T *.aria2'`（示例命令，shell 按环境调整），观察控制文件修改时间每约 10 秒跳动一次；同时在 `cycle.log` 里找 `Saving the segment file`（message.h L112）与 `The segment file was saved successfully`（L113）成对出现。
3. 下载自然完成后，确认 `.aria2` 文件已消失。
4. 换一个 `--force-save=true` 重新下载同一种子到别的目录，完成后与做种期间观察 `.aria2` 是否一直存在。
5. 中断一次做种再重启，观察 `--seed-ratio` 的累计上传量是否从日志/做种统计中恢复。

**需要观察的现象**：修改时间周期跳动、完成即删除、`--force-save` 下保留。

**预期结果**：与 `RequestGroupMan::save()` 的分支一一对应。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`saveControlFile()` 为什么必须在 `save()` 之前 flush 写盘缓存与 OS 缓冲？

**答案**：控制文件位图是对数据落盘情况的断言。若数据还滞留在 `WrDiskCache` 或 OS 页缓存就先写位图，崩溃后可能出现「位图说已完成、磁盘上没有」的假阳性，恢复后该 piece 被跳过，最终产出损坏文件。先冲数据再写断言，两者才一致（u5-l6 的自动保存前 flush 是同一时机）。

**练习 2**：做种任务没开 `--force-save`，控制文件何时消失？丢失了什么能力？

**答案**：第一个 autosave tick（或收割时）即被 `RequestGroupMan::save()`/收割分支删除，因为 `allDownloadFinished()` 为真。丢失的是 `uploadLength` 跨会话累计——重启做种后 `--seed-ratio` 分子从零重新累计，做种时长也更难衔接（`--seed-time` 同理）。

**练习 3**：用户手动删掉了数据文件但留下 `.aria2`，下次启动会发生什么？

**答案**：`createInitialCommand` 调 `removeDefunctControlFile` 检测到「控制文件在、数据文件不在」，删除控制文件并打日志 `Removed the defunct control file ... because the download file ... doesn't exist`，任务按全新下载从零开始。

## 5. 综合实践

把本讲四条线串成一个完整的断点续传实验（即大纲指定的实践任务）：

**任务**：下载种子到一半 Ctrl-C 中断 → 解剖控制文件 → 三种方式恢复，对照源码解释差异。

1. **中断与解剖**：任选一个种子开始下载，进度过半后按一次 Ctrl-C。用 4.1.4 的方法 `xxd` 解剖留下的 `.aria2`：核对版本 `0001`、extension 位、infohash，并用总长度与 piece 长度推算位图字节数，数一数 `xxd` 输出里位图区置位的字节个数，估算已完成 piece 比例，与中断时的控制台进度对照。
2. **默认恢复（复用位图）**：原命令重跑并开 `-l r1.log --log-level=info`。在日志找到 `Loading the segment file`；确认没有 hash check 阶段、起始进度等于中断值。对照源码解释：这对应 `RequestGroup.cc:426-430` 的 `load()`（位图与在途 piece 直接灌回）+ `processCheckIntegrityEntry` 条件为假直奔 `onDownloadIncomplete`——**已下载 piece 一个都没被读盘校验，纯复用**。
3. **校验恢复（全量重算）**：再次中断，改用 `--check-integrity=true -l r2.log` 重跑。观察 hash check 进度行从 0 到 100%，随后只补下坏块/缺块。对照源码解释：`IteratableChunkChecksumValidator::init` 清零本地位图后**逐 piece 重算**（已完成的不跳过、只是算完确认），结果一次性 `setBitfield` 写回，坏块自动回到待下载集合；并指出校验期间 `disableSaveControlFile` 生效、校验完成那一刻由 `CheckIntegrityCommand` 恢复。
4. **免校验恢复（全部信任）**：删掉 `.aria2` 但保留数据文件，分别试跑（a）原命令——应报 `FILE_ALREADY_EXISTS` 退出；（b）`--allow-overwrite=true`——从零重下（位图全空）；（c）`--bt-seed-unverified=true`——`markAllPiecesDone` 直接做种。三者差异对应 `RequestGroup.cc:431-445` 的三条件判定。
5. **收官观察**：任一恢复方式下载完成后，确认 `.aria2` 被删除；再用 `--force-save=true` 走一遍，观察做种期间控制文件保留。

最后写一份简短实验报告：每种恢复方式对应的源码分支（文件:行号）、观察到的日志证据、控制文件字节数推算表。

## 6. 本讲小结

- `.aria2` 控制文件是内存进度（完成位图 + 在途 piece 块位图 + 历史上传量）的磁盘快照，格式为「版本 2B + 扩展 4B + infohash 20B（BT）+ piece 长度 4B + 总长 8B + 上传量 8B + 位图长度 4B + 位图 + 在途 piece 明细」，版本 0001 起全大端。
- 保存走「SHA1 内容去重 → 写 `__temp` → 原子改名」三步，既不唤醒休眠磁盘，也保证任意时刻正式文件完整；`DefaultBtProgressInfoFile` 虽以 Bt 命名，但 HTTP/FTP 下载同样使用它。
- 恢复是三棒接力：`createInitialCommand` 清失效控制文件并 `load()` 位图 → `CheckIntegrityEntry` 家族裁定（BT 用 `BtCheckIntegrityEntry`，分块哈希；HTTP/FTP 用 `StreamCheckIntegrityEntry`；整文件摘要则用 `ChecksumCheckIntegrityEntry`）→ `BtFileAllocationEntry::prepareForNextAction` 装配 `BtSetup`、发下载命令或 `enableSeedOnly` 切入做种。
- 已下载 piece 的三种待遇：默认恢复零读盘纯复用位图；`--check-integrity=true` 全量逐 piece 重算、坏块清位重下（校验期间禁止保存控制文件）；`--bt-seed-unverified` 直接 `markAllPiecesDone` 全部信任。
- 控制文件保存时机：文件分配后初始保存、`--auto-save-interval` 周期保存（保存前必须先冲写盘缓存与 OS 缓冲）、暂停/停止时保存；删除时机：任务完成后（`--force-save` 可保，做种任务尤其需要它保住 uploadLength 累计）与启动时发现数据文件已删的失效清理。

## 7. 下一步学习建议

本讲是单元 6（BitTorrent）的收官。建议：

1. **复盘依赖链**：回到 u5-l5（校验器）与 u5-l4（文件预分配停车场）重看 `proceedFileAllocation` 的闸门，体会校验与分配两个「停车场」如何串联；再到 u6-l2（BtSetup）确认本讲反复出现的装配点全貌。
2. **对比流式路径**：沿 `StreamCheckIntegrityEntry → StreamFileAllocationEntry → CreateRequestCommand` 把 HTTP/FTP 的恢复链走一遍（结合 u4-l1 命令链），列表对比与 BT 链的同构与差异（如 `-c` 后备 `markPiecesDone`、无 infohash 检查）。
3. **进入单元 7（DHT）**：BT 数据面已闭环，下一讲 u7-l1 从 `DHTNode`/`DHTBucket` 开始讲 Kademlia 路由表——注意 DHT 的 `dht.dat` 路由表持久化与本讲控制文件在「原子写、temp 改名」策略上的异曲同工。
4. **延伸阅读源码**：`src/StreamFileAllocationEntry.cc`（流式收尾棒的初始保存）、`src/SHA1IOFile.cc`（去重哈希的 IOFile 实现）、`src/RequestGroupMan.cc` 的收割函数（控制文件终局），以及 `doc/manual-src/en/aria2c.rst` 中 `--force-save`、`--auto-save-interval`、`--allow-piece-length-change` 三个选项的官方表述。
