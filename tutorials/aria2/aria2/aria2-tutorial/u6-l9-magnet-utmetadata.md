# magnet 链接：先下元数据再下数据

## 1. 本讲目标

学完本讲，你应该能够：

1. 解析 magnet URI 中 `xt`、`dn`、`tr` 三类参数的含义，并说出 aria2 如何把它们变成一个「只有 infohash 的极简下载任务」。
2. 解释 metadataGetMode 下 `UnknownLengthPieceStorage` 如何在总长度未知时充当过渡存储，以及 `metadata_size` 到达后 PieceStorage 如何被原地「换心」。
3. 逐行读懂 ut_metadata 的 request/data/reject 三种消息与请求账本（每 peer 同时 1 个在途请求、20 秒超时）。
4. 描述 metadata 收齐后的收官动作：整体 SHA-1 终检、`UTMetadataPostDownloadHandler` 派生真正的文件下载任务（followedBy/following 链），并能对照 `BtDependency` 说清两种「两阶段任务」机制的差别。

一个需要先澄清的事实：学习大纲中把「派生数据任务」归给了 `BtDependency`，但源码中 magnet 场景的派生其实由 `UTMetadataPostDownloadHandler`（PostDownloadHandler 机制）完成；`BtDependency` 只在 Metalink 条目内嵌 .torrent 的场景中使用。两者共享同一套「metadata → torrent → DownloadContext」的转换函数，本讲 4.4 节会把这对孪生机制放在一起对照讲解。

## 2. 前置知识

本讲默认你已读过的前序内容（若印象模糊建议先复习）：

- **u3-l1/u3-l3（任务模型）**：`RequestGroup` 是一次下载任务的容器；任务完成后可由 PostDownloadHandler 派生新任务，新任务插到等待队列头部；任务之间用 GID 的 `followedBy`/`following` 链表达派生关系，用 `belongsTo` 表达从属关系。
- **u6-l1（torrent 元数据解析）**：.torrent 文件是 bencode 字典；infohash 定义为 info 字典重编码后的 SHA-1，即 \( \text{infohash} = \text{SHA-1}(\text{info 字典原文}) \)；`TorrentAttribute` 挂在 DownloadContext 的 `CTX_ATTR_BT` 属性槽上。
- **u6-l3（握手）**：68 字节握手之后，保留位第 20 字节的 0x10 比特表示支持扩展协议（BEP10），扩展能力经 extended handshake 的 `m` 字典协商，每对连接、每个方向各有一张 id 表。
- **u6-l8（扩展协议）**：所有扩展消息共用 20 号 `BtExtendedMessage` 外壳（1 字节扩展 id + bencode 载荷）；本端固定通告 `ut_metadata = 9`。
- **u5-l2（PieceStorage）**：`DefaultPieceStorage` 用位图管理 piece；`UnknownLengthPieceStorage` 是总长度未知时的退化实现，只有一个「生长型」piece。

两个本讲新引入的术语：

- **magnet URI**：一种「内容寻址」的种子引用（不是种子本身）。它只包含内容的指纹（infohash），不包含文件名、文件大小、分块哈希等任何实际元数据——这些都要从 BT 网络中的其他 peer 处获取。
- **metadataGetMode（元数据获取模式）**：aria2 内部对「metadata 为空」的 BT 任务打上的标记。此模式下下载的不是文件数据，而是种子的 info 字典本身。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/magnet.cc` | 极简的 magnet URI 语法解析：query string → `Dict` |
| `src/bittorrent_helper.cc` | `parseMagnet`（Dict → TorrentAttribute）、`metadata2Torrent`（info 字典 → 完整 torrent）、`torrent2Magnet`（反向转换） |
| `src/download_helper.cc` | `createBtMagnetRequestGroup`：把 magnet 变成「只下元数据」的 RequestGroup |
| `src/BtConstants.h` | `METADATA_PIECE_SIZE = 16 KiB` 常量 |
| `src/RequestGroup.cc` | `createInitialCommand` 的 BT 分支判定 metadataGetMode；`initPieceStorage` 按是否已知总长选择存储实现；`postDownloadProcessing` 遍历后处理器 |
| `src/UnknownLengthPieceStorage.cc` | 总长度未知时的过渡 PieceStorage |
| `src/HandshakeExtensionMessage.cc` | extended handshake 应答处理：`metadata_size` 触发 PieceStorage 重建 |
| `src/DefaultBtInteractive.cc` | metadataGetMode 下与单个 peer 的交互循环 |
| `src/UTMetadataRequestFactory.cc` | 生成 ut_metadata request 消息 |
| `src/UTMetadataRequestTracker.cc` | 在途 metadata 请求的账本（超时、容量） |
| `src/UTMetadataDataExtensionMessage.cc` | 收到 data 消息：落盘、记账、整体 SHA-1 终检 |
| `src/UTMetadataRejectExtensionMessage.cc` | 收到 reject 消息：断连 |
| `src/UTMetadataPostDownloadHandler.cc` | metadata 收齐后的收官：拼 torrent、保存、派生数据任务 |
| `src/RequestGroupMan.cc` | 任务完成后的 `postDownloadProcessing` 调用点与依赖检查 |
| `src/BtDependency.cc` | （对照）Metalink 场景的任务依赖机制 |
| `src/Metalink2RequestGroup.cc` | `BtDependency` 的唯一创建点 |
| `test/UTMetadataPostDownloadHandlerTest.cc` | 派生逻辑的单元测试，本讲实践的依据 |

## 4. 核心概念与源码讲解

### 4.1 magnet 解析：从 URI 到极简 TorrentAttribute

#### 4.1.1 概念说明

一条 magnet 链接长这样：

```text
magnet:?xt=urn:btih:cd41c7fdddfd034a15a04d7ff881216e01c4ceaf&dn=aria2&tr=http%3A%2F%2Ftracker.example%2Fannounce
```

它本质是一个 query string，常用参数有三类：

| 参数 | 含义 | 示例 |
| --- | --- | --- |
| `xt` | exact topic，内容指纹，`urn:btih:` 后跟 40 位十六进制（v1 infohash）或 32 位 base32 编码 | `xt=urn:btih:cd41...ceaf` |
| `dn` | display name，仅供展示的文件名 | `dn=aria2` |
| `tr` | tracker 地址，可重复出现多个 | `tr=http://...` |

注意 magnet 里**没有**文件大小、piece 长度、分块哈希——这些恰恰是下载数据所必需的。所以 aria2 的策略是：先创建一个「特殊的下载任务」，它要下载的文件就是种子的 info 字典本身；等这份元数据到手，再派生真正的下载任务。这就是标题所说的「先下元数据再下数据」。

#### 4.1.2 核心流程

```text
magnet 字符串
  │  magnet::parse（切 & 、按 = 分键值、百分号解码、同名参数聚成列表）
  ▼
Dict: { "xt": ["urn:btih:..."], "tr": [...], "dn": [...] }
  │  bittorrent::parseMagnet（提取 xt/tr/dn）
  ▼
TorrentAttribute { infoHash(20字节), name="[METADATA]xxx", announceList=[[tr1],[tr2]...] }
  │  download_helper::createBtMagnetRequestGroup
  ▼
RequestGroup（piece 长度 16KiB，总长度未知，内存盘，挂 UTMetadataPostDownloadHandler）
```

#### 4.1.3 源码精读

**第一步：`magnet::parse` 把 query string 变成通用 Dict。**

[magnet.cc:L43-L69](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/magnet.cc#L43-L69) 是整个解析器：先检查 `magnet:?` 前缀（L45-47），用 `util::splitIter` 按 `&` 切分（L50-51），再对每段用 `util::divide` 按 `=` 分离键值并做百分号解码（L55-57）。同名参数（如多个 `tr`）被聚合进同一个 `List`（L58-66）——这就是为什么后续读取时总是把值当作列表处理。

**第二步：`parseMagnet` 从 Dict 中提炼出 TorrentAttribute。**

[magnet.cc:L45-L57](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/magnet.cc#L45-L57) 旁边这几行说明解析层对参数名完全不设白名单，任何 `名=值` 都收。真正的筛选在 [bittorrent_helper.cc:L915-L939](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L915-L939)：遍历所有 `xt`，只认 `urn:btih:` 前缀，其后长度为 40 时按十六进制解码、为 32 时按 base32 解码（两种都要求解出恰好 20 字节）；一个合法 infohash 都没有就抛 `MAGNET_PARSE_ERROR`（L935-939）。

接着 [bittorrent_helper.cc:L940-L956](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L940-L956) 处理 `tr` 与 `dn`：每个 `tr` 单独成为一个 tracker 层级（tier）；显示名取第一个 `dn`，缺省时用 infohash 的十六进制。任务名统一加 `[METADATA]` 前缀（L948），这样在控制台的任务列表里一眼就能认出这是元数据任务。最终 `loadMagnet` 把这份属性挂到 DownloadContext 上：[bittorrent_helper.cc:L962-L966](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L962-L966)。

**第三步：`createBtMagnetRequestGroup` 把属性变成任务。**

[download_helper.cc:L236-L302](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L236-L302) 是 magnet 任务的装配车间，几个关键决定：

- L240-243：`DownloadContext(METADATA_PIECE_SIZE, 0)` —— piece 长度取 [BtConstants.h:L56](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtConstants.h#L56) 定义的 `16_k`（16 KiB，BEP 9 规定的 metadata 分片大小），总长度填 0 并 `markTotalLengthIsUnknown()`（要等 peer 告知 `metadata_size`）。
- L248-274：`--bt-load-saved-metadata` 快路径——若下载目录里已存有 `<infohash>.torrent`（由上次运行的 `--bt-save-metadata` 落盘）且其 infohash 相符，直接按普通种子建任务，跳过整个元数据获取阶段。
- L286-294 的连续装配：`setFileAllocationEnabled(false)`（内存文件无需预分配）、`setPreLocalFileCheckEnabled(false)`（不做本地重名检查）、`clearPostDownloadHandler()` 后挂上 `UTMetadataPostDownloadHandler`（元数据任务唯一的后处理器）、注入 `ByteArrayDiskWriterFactory`（数据写到内存而非磁盘）、`markInMemoryDownload()`（标记为内存下载，完成后不删文件）、`setMetadataInfo`（记录「本任务源自哪条 magnet」，供 RPC 展示）。

顺带一提，aria2 也实现了反向转换 `torrent2Magnet`（[bittorrent_helper.cc:L993-L1014](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L993-L1014)）：把 `--show-files` 或种子文件里的 infohash/name/announce 拼回 magnet 字符串。它正是我们实践中手工拼 magnet 链接的「官方公式」。

#### 4.1.4 代码实践

1. **实践目标**：验证你已理解 magnet 参数与种子的对应关系。
2. **操作步骤**：
   - 找一个已有的 .torrent 文件，运行 `./src/aria2c --show-files=true your.torrent`，从输出中记下文件名（作为 `dn`）；tracker 地址可从种子文件开头（文本编辑器可见的 `announce` 字段）或 `-l debug` 日志获取。
   - 用 `sha1sum` 无法直接得到 infohash（它是对 info 字典而非全文件算的），改用现成工具（如 `transmission-show`、`mktorrent -s`）或直接使用任一公开种子的 infohash。
   - 按 `torrent2Magnet` 的公式手工拼出：`magnet:?xt=urn:btih:<40位大写HEX>&dn=<percentEncode(名字)>&tr=<percentEncode(tracker)>`。
   - 运行 `./src/aria2c "<你拼的magnet>" --bt-metadata-only=true --bt-save-metadata=true -d /tmp/magnet-test`。
3. **需要观察的现象**：日志出现 `BitTorrent metadata was saved`（或类似）消息；`/tmp/magnet-test/` 下生成 `<infohash小写>.torrent` 文件。
4. **预期结果**：生成的 .torrent 能被 `--show-files=true` 正常解析，文件列表与原种子一致。若 `dn` 拼错不影响下载成功，只影响任务名显示；若 `xt` 拼错则永远连不到正确 peer，任务会一直等待。
5. 若本地无编译产物或无可用 BT 网络，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `magnet::parse` 要把同名参数聚成列表，而不是覆盖前值？
**答案**：magnet 规范允许同名参数多次出现（最典型是多个 `tr` tracker）。聚成列表后 `parseMagnet` 才能把每个 `tr` 变成一个独立的 tracker 层级；若覆盖前值，只会保留最后一个 tracker，丢失冗余的 tracker 来源（参见 magnet.cc L58-66 与 bittorrent_helper.cc L940-947 的循环）。

**练习 2**：`xt=urn:btih:` 后面跟 32 个字符时 aria2 如何处理？
**答案**：按 base32 解码（bittorrent_helper.cc L920-926）。base32 恰好能把 20 字节编码成 32 个字符，解码后校验长度为 20 才接受；40 个字符则走十六进制解码路径（L927-932）。

**练习 3**：元数据任务为什么不落盘、不做本地文件检查？
**答案**：它下载的「文件」是 info 字典，最终只作为派生任务的输入（及可选的 `<infohash>.torrent` 保存），不存在与用户文件冲突的问题；写入用 `ByteArrayDiskWriterFactory` 全内存完成，`setPreLocalFileCheckEnabled(false)` 跳过重名检查、`setFileAllocationEnabled(false)` 跳过预分配停车场（download_helper.cc L286-288）。

### 4.2 metadataGetMode：UnknownLengthPieceStorage 与「虚拟种子文件」

#### 4.2.1 概念说明

magnet 任务创建后进入 BT 装配流程。`RequestGroup::createInitialCommand` 的 BT 分支用「metadata 是否为空」判定元数据获取模式。此时任务处于一个尴尬状态：**总长度未知**（info 字典多大要等 peer 说），但 BT 装配又需要一个 PieceStorage。

aria2 的解法分两步：

1. 先用 `UnknownLengthPieceStorage` 顶上——它是为「长度未知的流式下载」准备的退化实现，只有一个从 0 开始生长的 piece。这个阶段的它**不能**服务任何 BT 选块请求。
2. 等 extended handshake 带来 `metadata_size`，任务的总长度变为已知，**原地重新调用 `initPieceStorage()`**，换成 `DefaultPieceStorage`——从此 info 字典就被当成一个按 16 KiB 分片的「虚拟文件」用标准位图管理。

#### 4.2.2 核心流程

```text
createInitialCommand（BT 分支）
  │ metadata 为空 → metadataGetMode
  ▼
initPieceStorage() ── knowsTotalLength()==false ──► UnknownLengthPieceStorage
  │                                                    （生长型单 piece，BT 接口全部 abort）
  │ 与 peer 完成 BT 握手 + extended handshake
  ▼
HandshakeExtensionMessage::doReceivedAction 收到 metadata_size
  │ setLength(metadata_size); markTotalLengthIsKnown()
  ▼
initPieceStorage() ── knowsTotalLength()==true ──► DefaultPieceStorage
  │ （16KiB 分片位图）+ enterEndGame() + peer->setAllBitfield()
  ▼
DefaultBtInteractive 每轮 tick 重新取 pieceStorage_，开始请求分片
```

piece 数量的计算与本讲相关的一个小数学：分片数 \( n = \lceil \text{metadata\_size} / 16\,\text{KiB} \rceil \)。例如 info 字典 100 KiB 时 \( n = \lceil 102400/16384 \rceil = 7 \)，最后一个分片只有 4 KiB。

#### 4.2.3 源码精读

**模式判定**：[RequestGroup.cc:L280-L299](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L280-L299) 中，L282 用 `torrentAttrs->metadata.empty()` 定义 `metadataGetMode`；L296-299 显示该模式同样调用 `initPieceStorage()`，区别在于此刻总长度未知，会走另一个分支。另外 L313-317 表明元数据任务**不创建** `.aria2` 控制文件（`progressInfoFile` 仅在非 metadataGetMode 时构造）——它没有断点续传的意义。

**两套存储的选择**：[RequestGroup.cc:L555-L612](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L555-L612) 是 `initPieceStorage` 的完整逻辑：`knowsTotalLength()` 为真且（总长 > 0 或带 BT 属性）时建 `DefaultPieceStorage`（L558-565、L567-568），否则建 `UnknownLengthPieceStorage`（L606-611）。注意这是同一个函数被调用两次、产生不同实现——这就是「原地换心」的实现方式。

**过渡存储的能力边界**：[UnknownLengthPieceStorage.cc:L76-L127](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UnknownLengthPieceStorage.cc#L76-L127) 中所有面向 BT 选块的接口（`hasMissingPiece`、按 peer 的 `getMissingPiece` 等）直接 `abort()`——一旦被调用就是程序 bug；而按索引取块的 [UnknownLengthPieceStorage.cc:L146-L155](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UnknownLengthPieceStorage.cc#L146-L155) 只认 index 0，其余返回 `nullptr`。它的 `initStorage`（[UnknownLengthPieceStorage.cc:L61-L72](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UnknownLengthPieceStorage.cc#L61-L72)）建的是 `DirectDiskAdaptor`，写入器由注入的工厂决定——magnet 场景注入的是 `ByteArrayDiskWriterFactory`，于是「虚拟文件」整个驻留内存。

**换心触发点**：[HandshakeExtensionMessage.cc:L109-L140](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HandshakeExtensionMessage.cc#L109-L140) 是收到对端 extended handshake 的处理。当本任务 metadata 为空（L110）时：对端若不支持 ut_metadata 扩展，直接断连（L111-116）；对端报告的 `metadata_size` 首次被记录时（L117-131），依次执行 `attrs->metadataSize = metadataSize_`、`getFirstFileEntry()->setLength(metadataSize_)`（总长度从 0 变为真实值）、`markTotalLengthIsKnown()`、`getOwnerRequestGroup()->initPieceStorage()`（重建 PieceStorage，这次走 DefaultPieceStorage 分支）、`enterEndGame()`（L128-130 注释解释：从头就进入 end game，任意缺失分片都可向任何 peer 要，加快元数据获取）；最后 `peer_->setAllBitfield()`（L134）让对方侧的位图判定认为本 peer 「拥有全部分片」。若两个不同 peer 报告了不一致的 `metadata_size`，L119-121 会抛异常断连。

**每轮重新取存储**：[DefaultBtInteractive.cc:L531-L538](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L531-L538) 中 L536-538 的注释点明了一个精妙细节：因为 `HandshakeExtensionMessage::doReceivedAction` 可能刚刚替换了 PieceStorage 对象，交互循环每轮都要从 RequestGroup 重新取一次 `pieceStorage_`，而不是缓存旧指针。

**握手后的差异行为**：[DefaultBtInteractive.cc:L172-L191](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L172-L191) 显示 metadataGetMode 下握手完成后只发 extended handshake（可能加 DHT port），**不发** bitfield 与 HaveAll/AllowedFast（L181-189 的两个 `if (!metadataGetMode_)`）——本 peer 此时没有任何数据可宣称。

#### 4.2.4 代码实践

1. **实践目标**：用现成单元测试验证「metadata_size → PieceStorage 重建」链路中各对象的赋值关系。
2. **操作步骤**：
   - 打开 [test/UTMetadataPostDownloadHandlerTest.cc:L67-L102](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/UTMetadataPostDownloadHandlerTest.cc#L67-L102)。注意 L82 `dctx_->getFirstFileEntry()->setLength(metadata.size())` 与 L95 `requestGroup_->initPieceStorage()`——测试手动复现了 `HandshakeExtensionMessage` 所做的两步（设置长度、初始化存储）。
   - 跟踪 `metadata` 字符串（L74-77）：它是一个最小合法 info 字典（`length`/`name`/`piece length`/`pieces` 四键）。
3. **需要观察的现象**：测试没有直接断言 PieceStorage 类型，而是通过 L96-98 直接向 `getDiskAdaptor()->writeData(..., 0)` 偏移 0 写入整段 metadata 模拟「分片全部收齐」。
4. **预期结果**：理解这段测试后你能回答：为什么测试必须先 `setLength` 再 `initPieceStorage`（否则会得到 UnknownLengthPieceStorage，其 DiskAdaptor 总长为 0，写不下数据）。
5. 本实践为纯源码阅读，无需编译运行即可完成。

#### 4.2.5 小练习与答案

**练习 1**：为什么元数据任务要 `enterEndGame()`？
**答案**：end game 模式下选块不再按顺序、也不再避让「别的连接正在下载的块」，任何缺失分片都可以立刻向任何 peer 请求。元数据总量通常只有几十 KiB、分片很少，顺序与避让没有意义，尽快收齐才是目标（HandshakeExtensionMessage.cc L128-130 的注释原文即 "We enter 'end game' mode from the start to get metadata quickly"）。

**练习 2**：`peer_->setAllBitfield()` 把本端位图置为全 1，会不会导致别的 peer 向我们请求数据？
**答案**：对其他「也在下载数据」的 peer 而言不会——它们依据自己的 PieceStorage 缺失情况发请求。但这个全 1 位图确实用于向对端声明「我有完整元数据」，使 ut_metadata 的 request 能被对端应答；同时注意 aria2 自己作为已拥有种子的 peer 也会用 `m` 字典通告 ut_metadata 并在 `metadata_size` 中报告大小（HandshakeExtensionMessage.cc L76-77），这正是 magnet 用户与种子持有者能互通的基础。

**练习 3**：如果第一个 peer 报 `metadata_size=32768`、第二个 peer 报 `metadata_size=40000`，会发生什么？
**答案**：第一个值被记录后（`attrs->metadataSize` 非零），第二个值在 L118-121 与已存值不相等，抛出 `"Wrong metadata_size. Which one is correct!?"` 并断开与第二个 peer 的连接。已初始化的 PieceStorage 不会被推翻。

### 4.3 UTMetadata 扩展族：request/data/reject 与请求账本

#### 4.3.1 概念说明

BEP 9 定义了三种 ut_metadata 消息，都封装在 20 号扩展消息的载荷里（bencode 字典 + 可选原始数据）：

| msg_type | 消息 | 方向 | 载荷 |
| --- | --- | --- | --- |
| 0 | request | 我 → peer | `{"msg_type":0,"piece":N}` |
| 1 | data | peer → 我 | `{"msg_type":1,"piece":N,"total_size":T}` + 第 N 片原始字节 |
| 2 | reject | peer → 我 | `{"msg_type":2,"piece":N}` |

aria2 的实现由三个对象协作：

- `UTMetadataRequestTracker`：账本。记录「我向这个 peer 发出但尚未应答的分片索引」，容量上限 1、超时 20 秒。
- `UTMetadataRequestFactory`：生产者。从 PieceStorage 领取缺失分片（排除账本中在途的），构造 request 消息并记账。
- `UTMetadataDataExtensionMessage`：消费者。核对账本、把数据写进内存盘、核销账目、必要时做整体终检。

「每 peer 同时只允许 1 个在途请求」是这个扩展族最重要的设计约束：metadata 分片极小，串行请求换取的是实现简单与精确的超时归因。

#### 4.3.2 核心流程

metadataGetMode 下 [DefaultBtInteractive.cc:L531-L563](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L531-L563) 的每轮循环：

```text
每轮 doInteractionProcessing（metadataGetMode 分支）
  1. sendKeepLive + receiveMessages（收干并分发消息）
  2. 重新取 pieceStorage_（可能刚被换心）
  3. 若对端支持 ut_metadata 且总长度已知：
     a. tracker->avail() 有空位（=1-在途数）
        → factory->create(num, pieceStorage) 生成 request 消息入队
     b. 每秒一次：tracker->removeTimeoutEntry()
        超过 20 秒的条目移出账本并 cancelPiece 归还位图
     c. pieceStorage->downloadFinished()
        → setForceHaltRequested 强制结束任务 → 进入后处理
```

#### 4.3.3 源码精读

**生产者**：[UTMetadataRequestFactory.cc:L61-L87](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTMetadataRequestFactory.cc#L61-L87) 的 `create`。L66 先取账本中全部在途索引作为排除表；L67 调 `pieceStorage->getMissingPiece(peer_, metadataRequests, cuid_)` 领一块「缺失且不在途」的分片（end-game 语义下任何缺失块都行）；领不到（L68-71）说明本 peer 视角没有可要的了，提前收工；领到则构造 `UTMetadataRequestExtensionMessage`（对端协商的扩展 id 在 L76 取得）、注入依赖（L77-81）、经 `createBtExtendedMessage` 包上 20 号外壳（L83），最后 L84 `tracker_->add(p->getIndex())` 记账——注意「领块即置在用位、请求即入账」，两本账严格同步。

**账本**：[UTMetadataRequestTracker.cc:L47-L65](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTMetadataRequestTracker.cc#L47-L65) 是朴素的 vector 线性查找（add/tracks/remove），规模为 1 时毫无性能压力；容量接口 [UTMetadataRequestTracker.cc:L90-L99](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTMetadataRequestTracker.cc#L90-L99) 中 `MAX_OUTSTANDING_REQUEST = 1` 写死为常量；超时清理 [UTMetadataRequestTracker.cc:L67-L88](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTMetadataRequestTracker.cc#L67-L88) 以 20 秒（L68 `TIMEOUT = 20_s`）为界，用 remove-if 把超时索引收集后返回。

**消费者**：[UTMetadataDataExtensionMessage.cc:L80-L113](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTMetadataDataExtensionMessage.cc#L80-L113) 的 `doReceivedAction` 分四步：

1. L82-85：`tracker_->tracks(getIndex())` 核对「这块确实是我要的」——防止对端塞来未请求的数据（未在途的数据一律丢弃，L109-112），核对通过即从账本移除。
2. L86-89：`getDiskAdaptor()->writeData(data, len, getIndex() * METADATA_PIECE_SIZE)` 把分片写到内存盘的对应偏移（第 N 片的偏移恰为 \( N \times 16384 \)），随后 `completePiece` 置完成位。
3. L90-107：若这是最后一块（`downloadFinished()`），把内存盘整体读出、对**完整 metadata** 计算 SHA-1 并与 magnet 带来的 infohash 逐字节比对（L92-97）。比对失败的后果很重：L102-104 把所有 piece 标回缺失（`markPieceMissing`，任务重头再要）并抛异常断开这个 peer——因为对端给了错误元数据，不可再信。
4. 比对成功仅记日志 `"Got ut_metadata"`（L98）。

**超时与拒绝的善后**：超时分片的归还链在 [DefaultBtInteractive.cc:L549-L557](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L549-L557)（L551-552 的注释解释了先排队新消息再清理超时的顺序：给其他连接留出请求窗口）；`cancelPiece` 归还后该分片重新可被任何连接领取。连接中断时的批量归还见 [DefaultBtInteractive.cc:L434-L448](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L434-L448) 的 `cancelAllPiece`。收到 reject 消息则干脆利落：[UTMetadataRejectExtensionMessage.cc:L63-L66](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTMetadataRejectExtensionMessage.cc#L63-L66) 直接抛异常断连——对端明确表示没有元数据，留之无用。

**任务终结**：最后一块落盘后 [DefaultBtInteractive.cc:L558-L561](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L558-L561) 对任务设置 `setForceHaltRequested(true, NONE)`，所有连接命令收到停机请求后逐个退出，任务结束，控制权移交 4.4 节的后处理。

#### 4.3.4 代码实践

1. **实践目标**：在真实运行中观察 ut_metadata 的请求-应答节奏与终检日志。
2. **操作步骤**：
   - 在 [UTMetadataDataExtensionMessage.cc:L98](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTMetadataDataExtensionMessage.cc#L98) 的 `A2_LOG_INFO("Got ut_metadata")` 前后已有日志；无需改码，直接运行：`./src/aria2c "<magnet>" --bt-metadata-only=true --bt-save-metadata=true -l=log.txt --log-level=info -d /tmp/magnet-test`。
   - 若想看得更细，可在 `UTMetadataRequestFactory::create`（L73-74 处已有 `Creating ut_metadata request index=%lu` 的 DEBUG 日志）不改码，把 `--log-level=debug` 打开即可。
3. **需要观察的现象**：日志中 `Creating ut_metadata request index=N` 与 `ut_metadata index=N found in tracking list`（Data 消息的 DEBUG 日志）成对交替出现；由于在途上限为 1，同一 peer 的相邻两次请求之间必有一次应答（或 20 秒后超时）。
4. **预期结果**：info 字典有多少分片（\(\lceil\text{metadata\_size}/16384\rceil\)），就出现多少对请求-应答；最后一对应答后紧接 `Got ut_metadata`（INFO 级）。收齐后任务立即结束，`/tmp/magnet-test` 下出现 `.torrent` 文件。
5. 依赖真实 BT 网络与可连的 peer，无法保证一定能观察到（_tracker 与 peer 可用性不可控_），标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么收到 data 消息时要先 `tracker_->tracks(index)` 核对，而不是直接写盘？
**答案**：两个原因。其一，防「未请求的数据」：账本中没有这个索引说明它要么从未被请求、要么已超时被取消，来源不可信；其二，超时后被取消的分片可能已经由其他连接补齐，盲目写盘会与内存盘中的正确数据冲突。核对通过才写，保证盘内数据全部来自受控请求（UTMetadataDataExtensionMessage.cc L82-89、L109-112）。

**练习 2**：终检为什么用「对完整 metadata 做 SHA-1 与 infohash 比对」，而不是逐分片校验？
**答案**：magnet 里唯一可信的指纹就是 infohash，而 \( \text{infohash} = \text{SHA-1}(\text{info 字典原文}) \) 是它的定义（u6-l1）。分片层面没有任何独立校验数据（真实种子里 piece 哈希本身就在 info 字典内部），所以只能等收齐后整体比对；失败即全部作废重下。

**练习 3**：假设某 peer 很慢，一个分片 25 秒才应答，会发生什么？
**答案**：20 秒超时（UTMetadataRequestTracker.cc L68）先把该条目移出账本并 `cancelPiece` 归还分片（DefaultBtInteractive.cc L553-556），该分片可能被同一 peer 的下一轮请求或别的 peer 领走；25 秒时迟到的 data 到达，但账本中已无此索引，数据被静默丢弃（`is not tracked` 分支）。

### 4.4 收官与派生：UTMetadataPostDownloadHandler 与两阶段任务衔接

#### 4.4.1 概念说明

metadata 收齐、任务被强制停机后，流程进入 RequestGroupMan 的完成处理：调用任务挂着的 PostDownloadHandler。`UTMetadataPostDownloadHandler` 做三件事：

1. 从内存盘读出 info 字典，用 `metadata2Torrent` 包一层 `announce-list` 拼成完整 torrent 文本。
2. （可选）按 `--bt-save-metadata` 把它保存为 `<infohash>.torrent`。
3. （默认）把它交给 `createRequestGroupForBitTorrent` 派生真正的文件下载任务，并用 `followedBy`/`following` 把两个任务的 GID 串成链；`--bt-metadata-only` 为真时跳过这一步。

而 `BtDependency` 是**另一条**两阶段路径，专用于 Metalink：Metalink 文件的某个条目描述的文件同时附带一个 .torrent 元数据（`<metaurl>`），aria2 为元数据建一个任务（dependee）、为文件本体建一个任务（dependant），dependant 通过 `BtDependency` 挂起等待；`resolve()` 在 dependee 完成后被反复调用，一旦成功就把 torrent 内容灌进 dependant 的 DownloadContext 并放行。两者的转换核心完全一致（`metadata2Torrent` + `loadFromMemory`），区别在连接方式：

| 维度 | magnet 场景 | Metalink 场景 |
| --- | --- | --- |
| 机制 | PostDownloadHandler 派生新任务 | Dependency 挂起既有任务 |
| 数据任务何时存在 | 元数据完成后才创建 | 一开始就创建（在等待队列里 pending） |
| 转换后去向 | `insertReservedGroup(0, ...)` 插到等待队列头 | `dependant->setDownloadContext(context)` 原地替换 |
| 失败后果 | 元数据任务失败即整体失败（无 URI 可退） | 退化为普通 HTTP/FTP 下载（Go without BT） |

#### 4.4.2 核心流程

```text
metadata 任务 downloadFinished + 强制停机
  ▼
RequestGroupMan::downloadFinished 处理
  └─ group->postDownloadProcessing(nextGroups)          [RequestGroupMan.cc:391-398]
       └─ UTMetadataPostDownloadHandler::getNextRequestGroups
            ├─ util::toString(diskAdaptor)   ← 从内存盘读出 info 字典
            ├─ metadata2Torrent(metadata, attrs)         ← 拼 announce-list + info
            ├─ --bt-save-metadata → 保存 <infohash>.torrent
            └─ createRequestGroupForBitTorrent(newRgs, option, {}, "", torrent, false)
                 → followedBy / following 串 GID 链
  ▼
insertReservedGroup(0, nextGroups)  ← 数据任务插到等待队列最前
  ▼
fillRequestGroupFromReserver 逐个检查 isDependencyResolved()   [RequestGroupMan.cc:546-552]
  ├─ magnet 派生任务无 Dependency → 立即放行进入下载
  └─ Metalink 的 dependant → BtDependency::resolve()
        ├─ dependee 未完成 → false（继续 pending）
        ├─ dependee 完成   → 读盘 + loadFromMemory + setDownloadContext → true
        └─ dependee 失败   → 放弃 BT，true（按普通下载继续）
```

#### 4.4.3 源码精读

**触发点**：[RequestGroupMan.cc:L391-L398](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L391-L398)——任务完成后收集 `nextGroups` 并 `insertReservedGroup(0, nextGroups)` 插到等待队列**头部**（0 号位），派生任务获得最高调度优先级。PostDownloadHandler 的遍历入口在 [RequestGroup.cc:L1064-L1082](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L1064-L1082)：第一个 `canHandle` 的处理器被调用。

**匹配条件**：[UTMetadataPostDownloadHandler.cc:L58-L71](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTMetadataPostDownloadHandler.cc#L58-L71) 的 `Criteria`：任务带 BT 属性**且** `metadata` 为空——即「这个任务的 metadata 曾经是空的」；普通种子任务的 metadata 非空，不会被误接手。

**派生主体**：[UTMetadataPostDownloadHandler.cc:L78-L125](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTMetadataPostDownloadHandler.cc#L78-L125) 的 `getNextRequestGroups`：

- L84-86：`util::toString(diskAdaptor)` 把内存盘整体读出为 `metadata`（正是上一节终检过的字节），再 `bittorrent::metadata2Torrent(metadata, attrs)` 拼装。拼装逻辑见 [bittorrent_helper.cc:L968-L991](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L968-L991)：把 magnet 里的 `tr` 参数（已存于 `attrs->announceList`）编码为 `announce-list`，再接上 `4:info` + metadata 字典原文 + `e`——**info 字典原样嵌入，infohash 不变**，这正是 magnet 机制能自洽的根基。
- L88-98：`--bt-save-metadata` 为真时以 `<infohash小写>.torrent` 为名保存（L92），成功失败都有 NOTICE 日志。
- L99-124：非 `--bt-metadata-only` 时派生。L103-105 调 `createRequestGroupForBitTorrent`，注意第 6 个参数 `adjustAnnounceUri=false`——注释（L101-102）说明 announce 调整在建 magnet 任务时已做过，不能重复。L106-109 双向建立 GID 链：`requestGroup->followedBy(newRgs)` 与每个新任务 `following(老GID)`。L110-113 把 magnet 的 `MetadataInfo`（记录「源自哪条 magnet 链接」）传递给后代。L116-121 处理 `--pause-metadata`：仅当 RPC 模式且 keepRunning 时，把派生任务标记为暂停，等客户端主动唤起。

**依赖检查与 BtDependency**：等待队列中的任务在提升为活动前要过 [RequestGroupMan.cc:L546-L552](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L546-L552) 的关卡——`isDependencyResolved()` 返回 false 的任务被推入 `pending` 暂存（下一轮再试）。该函数在 [RequestGroup.cc:L1118-L1124](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L1118-L1124) 转发给 `dependency_->resolve()`。

`BtDependency::resolve` 全文见 [BtDependency.cc:L87-L181](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtDependency.cc#L87-L181)，三条出口：

- L92-168（dependee 已完成且成功）：L98-101 打开 dependee 的磁盘适配器（元数据任务的内存盘）读出内容；L102-109 走与 magnet 完全相同的转换（`metadata2Torrent` + `loadFromMemory`，且注释说明不再重复 `adjustAnnounceUri`）；L117-157 把新 context 的文件条目与 dependant 原有条目按 `originalName` 对齐（`copyValues` 在 L65-74 复制路径/URI/单主机连接数等用户意图）；任何异常都被 L159-164 捕获并**降级**——记日志 `"Go without BT"` 后返回 true，dependant 按普通 HTTP/FTP 下载继续。L167-168 成功路径 `dependant_->setDownloadContext(context)` 原地替换元数据模型。
- L170-177（dependee 结束但失败）：同样 `Go without BT` 降级放行。
- L178-180（dependee 还在跑）：返回 false，继续等待。

**BtDependency 的创建点**：[Metalink2RequestGroup.cc:L333-L339](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalink2RequestGroup.cc#L333-L339) 是全仓库唯一构造 `BtDependency` 的地方——为 Metalink 条目建 `rg`（文件本体）与 `torrentRg`（元数据），`rg->dependsOn(dep)`、`torrentRg->belongsTo(rg->getGID())`。后面 L340-355 还有一个贴心细节：若文件本体有可用 HTTP/FTP 镜像，元数据任务的 `bt-stop-timeout` 被压到最多 30 秒——元数据等不起就降级。

#### 4.4.4 代码实践（本讲主实践）

1. **实践目标**：亲眼看一次「元数据任务 → 数据任务」的派生，并画出两阶段任务依赖图。
2. **操作步骤**：
   - 准备一个信息完整的 magnet（4.1.4 实践的产物即可）。
   - 在 [UTMetadataPostDownloadHandler.cc:L106](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTMetadataPostDownloadHandler.cc#L106) 的 `followedBy` 调用前加一行日志（示例代码，非项目原有）：
     ```cpp
     A2_LOG_INFO(fmt("magnet metadata done: %lu follow-up group(s) created",
                     static_cast<unsigned long>(newRgs.size())));
     ```
   - 重新编译（`make -j$(nproc)`），运行 `./src/aria2c "<magnet>" -l=log.txt --log-level=info -d /tmp/magnet-test --seed-time=0`。
   - 观察日志中两个任务的 GID：用 `--enable-rpc=true` + `curl` 调 `aria2.tellActive`/`aria2.tellWaiting` 也可以（元数据任务先 active，结束后消失，同 GID 或新 GID 的数据任务出现在 waiting 队列头部）。
3. **需要观察的现象**：元数据阶段任务名带 `[METADATA]` 前缀；`Got ut_metadata` 之后出现你加的日志与（默认开启的）`bt-save-metadata` 相关消息；随后控制台进入正常的数据下载进度。
4. **预期结果**：数据任务的 GID 出现在元数据任务 `followedBy` 列表中、其 `following` 指回元数据任务 GID（可用 RPC `aria2.tellStatus` 的 `followedBy`/`following` 字段直接验证）。据此画出依赖图：
   ```text
   GID#A (magnet 元数据任务, [METADATA]xxx, 内存盘, 无控制文件)
     │  downloadFinished + forceHalt
     │  UTMetadataPostDownloadHandler::getNextRequestGroups
     │  metadata2Torrent(info字典, attrs)
     ▼  followedBy / following
   GID#B (数据任务, 完整 DownloadContext, 磁盘盘, .aria2 控制文件)
   ```
5. 需要 BT 网络与持有该种子的 peer，结果不可控时标注「待本地验证」；退路是纯阅读 [test/UTMetadataPostDownloadHandlerTest.cc:L67-L127](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/UTMetadataPostDownloadHandlerTest.cc#L67-L127)：L116-120 的断言正是 `followedBy`/`following` 链的机器验证。

#### 4.4.5 小练习与答案

**练习 1**：magnet 派生出的数据任务为什么不重新执行 `adjustAnnounceUri`？
**答案**：magnet 的 `tr` 参数在建任务时已经过 `adjustAnnounceUri`（download_helper.cc L277）写入 `attrs->announceList`；派生时 `metadata2Torrent` 直接使用这份已调整的列表，若再调一次会把 `--bt-tracker`、`--bt-exclude-tracker` 的效果重复应用。UTMetadataPostDownloadHandler.cc L101-102 的注释与 BtDependency.cc L107-108 的注释都明确说明了这一点。

**练习 2**：`--bt-metadata-only=true` 与 `--pause-metadata=true`（RPC 场景）分别改变流程的哪一步？
**答案**：`--bt-metadata-only` 让 `getNextRequestGroups` 跳过 L99-124 的派生整段，只做（可选的）保存 `.torrent`，进程在元数据收齐后即结束；`--pause-metadata` 照常派生但把新任务标记 `setPauseRequested(true)`（L116-121），数据任务停在等待队列里等 RPC 客户端 `aria2.unpause` 唤起——常用于「先探元数据再决定是否下载」的交互式客户端。

**练习 3**：若元数据任务因网络原因最终失败（一个分片都拿不到），magnet 场景和 Metalink 场景分别是什么结局？
**答案**：magnet 场景没有 URI 可退，元数据任务以错误结束，整个下载失败；Metalink 场景的 dependant 在 `BtDependency::resolve` 的 L170-177 检测到 dependee 失败，记 `"Go without BT"` 后返回 true，dependant 放弃 BT 路径、按 Metalink 提供的 HTTP/FTP 镜像继续下载（且镜像齐全时元数据任务最多只等 30 秒，见 Metalink2RequestGroup.cc L340-355）。

## 5. 综合实践

**任务：手工复盘一次 magnet 下载的完整生命周期，并产出三份工件。**

1. **准备**：用 4.1.4 的方法获得一条可用的 magnet 链接（infohash 必须准确）。
2. **运行三组对照实验**（每组都开 `-l=log.txt --log-level=debug`，`-d` 指向独立目录）：
   - A：`--bt-metadata-only=true --bt-save-metadata=true`：只取元数据并落盘。
   - B：默认参数完整下载 `--seed-time=0`。
   - C：`--bt-load-saved-metadata=true`：先删掉 A 存的 `.torrent` 跑一次 B，再保留它跑一次 B，对比第二次是否出现 `BitTorrent metadata was loaded from ...` 日志（download_helper.cc L264 的输出）。
3. **日志取证**：从 B 的日志中按时间顺序摘出并标注以下事件（若日志级别不够则补充 `--log-level=debug`）：
   - `[METADATA]` 任务名的出现（magnet 任务创建）；
   - extended handshake 携带的 `metadataSize`（HandshakeExtensionMessage 的 toString 日志）；
   - 若干对 `Creating ut_metadata request index=N` / `ut_metadata index=N found in tracking list`；
   - `Got ut_metadata`；
   - 元数据任务完成、数据任务接棒的边界（followedBy 相关行或进度行重启）。
4. **产出三份工件**：
   - 工件一：两阶段任务依赖图（4.4.4 的预期图样，填上真实 GID）。
   - 工件二：一张「分片请求时序表」，列出每个分片的请求时刻与应答时刻，验证在途上限 1 与 20 秒超时。
   - 工件三：一篇 200 字以内的说明，回答「为什么 C 实验第二次运行跳过了 DHT/peer 元数据获取阶段」，引用 `createBtMagnetRequestGroup` 的 L248-274 说明快路径的判定条件（infohash 相符）。
5. **验证与兜底**：以上全部依赖真实 BT 网络。若环境不可用，以 [test/UTMetadataPostDownloadHandlerTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/UTMetadataPostDownloadHandlerTest.cc)、[test/UTMetadataRequestFactoryTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/UTMetadataRequestFactoryTest.cc)、[test/UTMetadataRequestTrackerTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/UTMetadataRequestTrackerTest.cc) 三个测试的断言为「已验证行为」，逐一翻译成中文事实清单代替日志取证，并在文档开头注明「网络实践部分待本地验证」。

## 6. 本讲小结

- magnet URI 只含 `xt`（infohash，40 位 hex 或 32 位 base32）、`dn`（显示名）、`tr`（tracker，可多个）；`magnet::parse` 把它解析成通用 Dict，`parseMagnet` 提炼出极简 `TorrentAttribute`（任务名带 `[METADATA]` 前缀）。
- magnet 任务是一个「下载 info 字典本身」的特殊任务：piece 长度固定 16 KiB（`METADATA_PIECE_SIZE`）、总长度未知、数据全内存（`ByteArrayDiskWriterFactory`）、无 `.aria2` 控制文件、唯一后处理器是 `UTMetadataPostDownloadHandler`。
- 总长度未知期间用 `UnknownLengthPieceStorage` 过渡（BT 选块接口全部 `abort`）；extended handshake 带来 `metadata_size` 后 `initPieceStorage()` 原地换心为 `DefaultPieceStorage`，从头进入 end game，交互循环每轮重新取存储指针。
- ut_metadata 族遵循「每 peer 同时 1 个在途请求、20 秒超时」的账本纪律；data 应答须通过账本核对才落盘，收齐后对完整 metadata 做 SHA-1 与 infohash 终检，失败全部作废重下；reject 直接断连。
- 收官派生：`metadata2Torrent` 把 info 字典原样嵌入（infohash 不变），`createRequestGroupForBitTorrent` 生成数据任务并以 `followedBy`/`following` 串链，新任务插到等待队列头；`--bt-metadata-only` 只存不派生，`--pause-metadata` 派生但暂停。
- `BtDependency` 是 Metalink 场景的孪生机制：数据任务一开始就存在、以 Dependency 挂起等待元数据任务，`resolve()` 成功则原地替换 DownloadContext，失败则降级为普通下载（Go without BT）——与 magnet 的「完成后派生」互为镜像。

## 7. 下一步学习建议

- **u6-l10（做种与退出）**：本讲派生出的数据任务下载完成后如何转入做种、`ShareRatioSeedCriteria` 如何判定退出，是 BT 单元的收官话题。
- **u6-l11（会话恢复与 .aria2 控制文件）**：本讲提到元数据任务没有控制文件；对照学习数据任务的 `DefaultBtProgressInfoFile` 如何保存位图，理解两种任务的持久化差异。
- **u9-l6（二次开发）**：`UTMetadataPostDownloadHandler` 是「实现 `PostDownloadHandler` 接口即可挂入任务生命周期」的范例，`BtDependency` 则示范了 `Dependency` 扩展点；做自己的扩展时可回到这两处参照。
- 继续精读的源码：`src/UTMetadataRequestExtensionMessage.cc`（request 消息的 payload 与排队钩子）、`src/ExtensionMessageRegistry.cc`（ut_metadata = 9 的通告路径），以及 `test/UTMetadataRequestFactoryTest.cc` 里对「排除在途索引」行为的断言。
