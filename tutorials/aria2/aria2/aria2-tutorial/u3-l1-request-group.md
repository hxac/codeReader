# RequestGroup：一次下载任务的容器

## 1. 本讲目标

上一单元（u2-l3）我们学习了 Command 体系：引擎里的每个"工人"都是一条命令，执行完就销毁或重排。但一条命令显然不足以描述"下载这个 4GB 的 ISO 文件"这样一件大事——它没有地方存放"这个任务有哪些镜像地址、允许开几条连接、下载到什么程度了、完成后要派生什么新任务"。本讲就把视角从**命令层**上升到**任务层**，学完后你应当能够：

1. 说出 RequestGroup 聚合了哪些资源（Option、DownloadContext、SegmentMan、PieceStorage、URISelector、Dependency……），以及它如何用 `numConcurrentCommand_`（`-s/--split`）控制并发命令数的扩张。
2. 跟踪 RequestGroupMan 的 `fillRequestGroupFromReserver`：任务如何从等待队列（`reservedGroups_`）被提升到活动队列（`requestGroups_`），`-j/--max-concurrent-downloads` 与 `requestQueueCheck` 脏标记如何配合。
3. 解释 URISelector 三种策略（inorder/feedback/adaptive）在哪一行被真正调用，`-x/--max-connection-per-server` 为何是在 FileEntry 层而不是 RequestGroup 层生效。
4. 理解 RequestGroup 之间的两类关联：一类是"停车"关系（校验/预分配阶段通过 RequestGroupEntry + SequentialPicker 挂起），一类是"父子依赖"关系（Metalink 任务依赖 torrent 元数据任务，由 BtDependency 连接）。

## 2. 前置知识

### 2.1 任务、命令、连接三层概念

| 层 | 代表类 | 生命周期 | 数量关系 |
|---|---|---|---|
| 任务（下载什么） | `RequestGroup` | 从创建到出结果（DownloadResult），可能被暂停/恢复 | 一个任务 |
| 命令（现在做什么） | `Command`/`AbstractCommand` | 极短，一两步即结束，靠重排接力 | 每任务多条，受 `-s` 约束 |
| 连接（走哪条线） | `SocketCore` | 一次 TCP/TLS 会话 | 每主机受 `-x` 约束 |

一个 RequestGroup 内部同时活跃的命令数有上限（`numConcurrentCommand_`，来自 `-s`）；而"对同一台服务器最多开几条连接"（`-x`）记录在 FileEntry 上，在挑选 URI 时才检查。这两个约束作用在不同层，是初学者最容易混淆的点，也是本讲综合实践要验证的核心。

### 2.2 GID：任务的全局身份证

每个 RequestGroup 拥有一个 64 位 GID（`GroupId`），RPC 接口、控制台输出、`.aria2` 会话文件都用它指代任务。任务之间还有三个基于 GID 的关联字段（见 [src/RequestGroup.h:L89-L94](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.h#L89-L94) 与 [src/RequestGroup.h:L126-L134](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.h#L126-L134)）：

- `belongsToGID_`：我是谁的派生任务（如 Metalink 派生出的 torrent 元数据任务）；
- `followedByGIDs_`：我完成后派生出了哪些任务；
- `followingGID_`：`followedByGIDs_` 的反向链接。

### 2.3 IndexedList：带索引的双向队列

`RequestGroupMan` 用 `IndexedList<a2_gid_t, std::shared_ptr<RequestGroup>>`（别名 `RequestGroupList`，见 [src/RequestGroupMan.h:L65-L67](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.h#L65-L67)）存放任务：它既是一个按 GID 索引的字典（`get(gid)` 快速查找），又是一个保序队列（`push_front`/`push_back`/`move` 支持排队与插队）。RPC 的 `aria2.changePosition` 就靠它的 `move` 能力实现。

### 2.4 本讲涉及的命令行选项

| 选项 | 默认值 | 含义 | 定义处 |
|---|---|---|---|
| `-s, --split` | 5 | 每任务并发命令数上限 | [src/OptionHandlerFactory.cc:L972](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L972) |
| `-x, --max-connection-per-server` | 1（1..16） | 每主机最大连接数 | [src/OptionHandlerFactory.cc:L439-L441](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L439-L441) |
| `-j, --max-concurrent-downloads` | 5 | 全局同时进行的任务数 | [src/OptionHandlerFactory.cc:L431-L433](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L431-L433) |
| `--uri-selector` | feedback | 镜像挑选策略 | [src/OptionHandlerFactory.cc:L1008-L1017](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L1008-L1017) |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/RequestGroup.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.h) / [src/RequestGroup.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc) | 任务容器：聚合元数据与运行时资源，制造初始命令，控制并发扩张 |
| [src/RequestGroupMan.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc)（[.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.h)） | 任务队列管理器：等待/活动两级队列、并发上限、结果收集 |
| [src/RequestGroupEntry.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupEntry.cc) | 任务在"停车场"里的座位条：持有被扣住的命令并维持计数 |
| [src/SequentialPicker.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SequentialPicker.h) | 一次只取一个的通用挑选器，是文件预分配/校验两个停车场的骨架 |
| [src/FillRequestGroupCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FillRequestGroupCommand.cc) | 周期性触发队列维护的例程命令 |
| [src/CreateRequestCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CreateRequestCommand.cc) | 并发扩张的产物：挑选 URI、发起协议命令链的引导命令 |
| [src/FileEntry.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FileEntry.cc) | URI 池的真正持有者，`-x` 在这里生效 |
| [src/InorderURISelector.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InorderURISelector.cc)、[src/FeedbackURISelector.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FeedbackURISelector.cc)、[src/AdaptiveURISelector.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AdaptiveURISelector.cc) | 三种镜像挑选策略 |
| [src/BtDependency.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtDependency.cc)（[Dependency.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Dependency.h)） | 任务间依赖：等元数据任务完成再激活正式下载 |

## 4. 核心概念与源码讲解

### 4.1 RequestGroup：任务的状态与资源容器

#### 4.1.1 概念说明

RequestGroup 是 aria2 对"一个下载任务"的抽象。注意"一个任务"不等于"一个文件"：一个 torrent 里的几十个文件、一个 Metalink 里的一批条目，都各自构成一个 RequestGroup。它解决三个问题：

1. **资源聚合**：任务的选项快照（`option_`）、元数据（`downloadContext_`）、分段表（`segmentMan_`）、落盘器（`pieceStorage_`）、断点文件（`progressInfoFile_`）、URI 挑选器（`uriSelector_`）都挂在一个对象上，命令链上的任何命令都能通过裸指针 `requestGroup_` 回溯到这些共享资源。
2. **并发预算**：`numConcurrentCommand_` 是这个任务允许同时存在的"流式命令"数量上限，直接取自 `-s`。
3. **生命周期状态**：只有 `STATE_WAITING`（在等待队列里）和 `STATE_ACTIVE`（已开跑）两个状态，加上暂停/重启/停止等请求标志，足以表达排队、运行、暂停、做种分离等所有场景。

#### 4.1.2 核心流程

一个 RequestGroup 的一生：

```text
创建（download_helper / Metalink2RequestGroup / RPC addUri）
  │  numConcurrentCommand_ = -s；state = STATE_WAITING；默认 InorderURISelector
  ▼
进入 reservedGroups_（等待队列）
  │  被 RequestGroupMan::fillRequestGroupFromReserver 选中
  ▼
STATE_ACTIVE：createInitialCommand 制造第一批命令
  │  ├─ BT 任务 → BtRegistry 注册 + BtCheckIntegrityEntry
  │  ├─ 单文件流式(HTTP/FTP/SFTP) → 可能先进校验/预分配停车场，再扩张并发
  │  └─ 多文件 → 去重检查 + setupFileFilter + StreamCheckIntegrityEntry
  ▼
运行中：命令接力；文件大小已知后 createNextCommandWithAdj 扩到 min(-s, piece数)
  │  每条命令构造时 numCommand_++，析构时 numCommand_--
  ▼
numCommand_ 归零 → requestQueueCheck → removeStoppedGroup 收割
  ├─ 暂停 → 存控制文件，push_front 回等待队列，STATE_WAITING
  ├─ 完成 → postDownloadProcessing 派生新任务（insertReservedGroup(0, ...)）
  └─ 结束 → createDownloadResult 存入 downloadResults_，打印 Download Results
```

并发扩张的数学关系：文件总长度已知时，同时存在的流式命令数满足

\[ N_{\text{命令}} \le \min(s,\; P) \]

其中 \(s\) 是 `--split`，\(P\) 是该任务的 piece 总数（`downloadContext_->getNumPieces()`）。文件长度未知时退化为 1（无从分段）。而对任意一台主机，连接数另有约束 \( N_{\text{host}} \le x\)（见 4.3 节）。

#### 4.1.3 源码精读

**构造函数：选项即初值。** 任务创建时并没有传入任何 URI——URI 在 DownloadContext 的 FileEntry 里。构造函数只做三件事：抄选项、装默认 URI 选择器、登记前后处理器：

[src/RequestGroup.cc:L122-L162](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L122-L162) —— 构造函数把 `-s` 写入 `numConcurrentCommand_`，把超时、限速抄进成员，初始状态 `STATE_WAITING`：

```cpp
RequestGroup::RequestGroup(const std::shared_ptr<GroupId>& gid,
                           const std::shared_ptr<Option>& option)
    : belongsToGID_(0),
      ...
      uriSelector_(make_unique<InorderURISelector>()),
      ...
      state_(STATE_WAITING),
      numConcurrentCommand_(option->getAsInt(PREF_SPLIT)),
      numStreamConnection_(0),
      numStreamCommand_(0),
      numCommand_(0),
```

注意两点：`uriSelector_` 的默认值是 InorderURISelector，但任务被激活时会被 `RequestGroupMan::configureRequestGroup` 按 `--uri-selector` 覆盖（见 4.3 节）；`--dry-run` 时不安装任何 Pre/Post 处理器（[L158-L161](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L158-L161)）。

而 `-x` 在任务创建流水线里被写进 FileEntry，而不是 RequestGroup：[src/download_helper.cc:L130-L162](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L130-L162) 中 `dctx->getFirstFileEntry()->setMaxConnectionPerServer(option->getAsInt(PREF_MAX_CONNECTION_PER_SERVER))`——因为"每主机连接上限"约束的是 URI 池，而 URI 池属于 FileEntry。

**createInitialCommand：按任务类型分流。** [src/RequestGroup.cc:L271-L527](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L271-L527) 是任务激活后的第一跳，三大分支：

- **BitTorrent 分支**（[L280-L462](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L280-L462)）：检查 infohash 是否已注册（[L288-L295](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L288-L295)，重复则报 `DUPLICATE_INFO_HASH`）；magnet 的 metadataGetMode 用 UnknownLengthPieceStorage；随后一次性装配 BtRuntime、PeerStorage、DefaultBtAnnounce 并放入 BtRegistry（[L319-L348](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L319-L348)），必要时启动 DHT（[L350-L398](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L350-L398)），最后交给 BtCheckIntegrityEntry。这是单元 6 的主战场，本讲只需知道"BT 任务在这里注册进全局注册表"。
- **单文件流式分支**（[L465-L482](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L465-L482)）：`--dry-run` 或总长度未知时只造 1 条命令（[L470](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L470)）——因为不知道大小就无法分段；否则 `adjustFilename`（重名自动改名）→ `initPieceStorage` → `createCheckIntegrityEntry`。
- **多文件分支**（[L493-L526](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L493-L526)）：先调 `isSameFileBeingDownloaded` 防止同一文件被两个任务同时下载（报 `DUPLICATE_DOWNLOAD`），再 `setupFileFilter` 应用 `--select-file`。

[initPieceStorage（L555-L650）](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L555-L650) 保证 `pieceStorage_` 与 `segmentMan_` 要么都初始化要么都没有（[L618-L620](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L618-L620)）：

```cpp
segmentMan_ = std::make_shared<SegmentMan>(downloadContext_, tempPieceStorage);
pieceStorage_ = tempPieceStorage;
```

**createNextCommand 家族：并发扩张的阀门。** 三个重载各司其职：

[src/RequestGroup.cc:L827-L844](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L827-L844) —— 带修正量的版本（预分配/校验完成后调用）：

```cpp
int numCommand;
if (getTotalLength() == 0) {
  numCommand = 1 + numAdj;              // 长度未知：单命令
}
else {
  numCommand = std::min(downloadContext_->getNumPieces(),
                        static_cast<size_t>(numConcurrentCommand_));
  numCommand += numAdj;                 // numAdj 通常为 0 或 -1（当前已有1条在跑）
}
```

[src/RequestGroup.cc:L846-L870](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L846-L870) —— 无参版本（重试/换 URI 时调用），用 `numStreamCommand_`（当前活着的流式命令数）做差值补偿：

```cpp
else if (numStreamCommand_ >= numConcurrentCommand_) {
  numCommand = 0;                       // 已满员，不再扩张
}
else {
  numCommand = std::min(downloadContext_->getNumPieces(),
      static_cast<size_t>(numConcurrentCommand_ - numStreamCommand_));
}
```

[src/RequestGroup.cc:L872-L883](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L872-L883) —— 最终落地：每条命令都是一个 CreateRequestCommand，并要求引擎别休眠（`setNoWait`）：

```cpp
for (; numCommand > 0; --numCommand) {
  commands.push_back(make_unique<CreateRequestCommand>(e->newCUID(), this, e));
}
if (!commands.empty()) {
  e->setNoWait(true);
}
```

谁在调用它们？记住四个调用点即可拼出完整时间线：

| 调用点 | 时机 | 数量 |
|---|---|---|
| [src/RequestGroup.cc:L470](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L470) | 任务启动且长度未知 | 1 |
| [src/StreamFileAllocationEntry.cc:L78-L100](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/StreamFileAllocationEntry.cc#L78-L100) | 预分配完成、长度已知 | `min(-s, piece数) + (-1 或 0)` |
| [src/AbstractCommand.cc:L415-L436](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L415-L436)（tryReserved） | 连接失败换下一个 URI | 1 |
| [src/RpcMethodImpl.cc:L1346](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodImpl.cc#L1346) | RPC unpause 恢复任务 | 按差值 |

**计数器与 RAII 记账。** 三个计数器 `numStreamCommand_`/`numStreamConnection_`/`numCommand_` 的增减全部由 AbstractCommand 的构造/析构自动完成（[src/AbstractCommand.cc:L99-L125](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractCommand.cc#L99-L125)）：

```cpp
if (incNumConnection_) {
  requestGroup->increaseStreamConnection();
}
requestGroup_->increaseStreamCommand();
requestGroup_->increaseNumCommand();
```

这就是 u2-l3 所说"命令返回 true 即销毁、清理都在析构"的收益之一——不需要任何手动注销代码。而 [decreaseNumNumber 的关键副作用在 src/RequestGroup.cc:L979-L986](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L979-L986)：

```cpp
void RequestGroup::decreaseNumCommand()
{
  --numCommand_;
  if (!numCommand_ && requestGroupMan_) {
    A2_LOG_DEBUG(fmt("GID#%s - Request queue check", gid_->toHex().c_str()));
    requestGroupMan_->requestQueueCheck();   // 通知调度器：我可能完事了
  }
}
```

任务是否"结束"的判定标准就是 `getNumCommand() == 0`——这个标志在 4.2 和 4.4 节还会反复出现。

#### 4.1.4 代码实践

**实践目标**：亲眼看到"一个 RequestGroup 里的命令数从 1 扩张到 `-s`"的过程。

**操作步骤**（需要网络或本地可分段的 HTTP 服务）：

1. 准备一个 ≥ 8MB 的测试文件。本地方案：任选一个目录执行 `python3 -m RangeHTTPServer 8001`（需 `pip install rangehttpserver`；python 自带的 `http.server` 不支持 Range 请求，无法触发分段，这是一个常见的坑）。
2. 运行：
   ```bash
   ./src/aria2c -s3 -x2 -k1M --min-split-size=1M \
     --log=rg.log --log-level=debug -d /tmp/rg-test \
     http://127.0.0.1:8001/bigfile.bin
   ```
3. 打开 rg.log，搜索 `CUID#`，统计不同 CUID 的个数；再搜索 `Request queue check`，看它出现的时间点。

**需要观察的现象**：下载初期只有 1 个 CUID 在活动（长度未知，单命令试探）；拿到 Content-Length 之后，日志里短时间新增多个 CUID；下载全部结束后出现一条 `GID#... - Request queue check`。

**预期结果**：活跃 CUID 总数不超过 3（`-s3`），且因为三个连接打向同一主机 127.0.0.1，同时建立的连接不超过 2（`-x2`）——控制台进度行里的 `CN:` 字段会显示当前连接数。CUID 数与连接数是两个概念：一次重试会换新 CUID 但可能复用连接。实际数值「待本地验证」（取决于服务器是否支持 Range、文件大小是否够切 3 段）。

#### 4.1.5 小练习与答案

**练习 1**：`-s10` 下载一个 3MB 的文件（默认 `--piece-length=1M`），最多会有几条并发命令？

答案：`min(10, piece数) = min(10, 3) = 3`。依据是 [createNextCommandWithAdj](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L827-L844) 中的 `std::min(downloadContext_->getNumPieces(), numConcurrentCommand_)`。piece 数不足时 `-s` 再大也没用。

**练习 2**：为什么 `numStreamCommand_` 与 `numCommand_` 要分开计数？

答案：`numCommand_` 统计任务下**所有**命令（包括 BT 交互命令、校验命令等），归零是"任务结束"的判定条件；`numStreamCommand_` 只统计 HTTP/FTP/SFTP 这类流式协议命令，用于和 `numConcurrentCommand_`（`-s`）比较来决定是否继续扩张并发。两者职责不同，参考 [src/RequestGroup.h:L149-L157](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.h#L149-L157) 的注释（"This is the number of connections used in streaming protocol(http/ftp)"）。

**练习 3**：一个任务下载完成后，aria2 控制台最后的 Download Results 表格数据从哪来？

答案：`ProcessStoppedRequestGroup` 收割任务时调用 `group->createDownloadResult()`（[src/RequestGroup.cc:L1171-L1210](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L1171-L1210)），把 GID、结果码、followedBy/following/belongsTo、总长度、已完成长度、bitfield 等打包成 DownloadResult 存入 `downloadResults_`，再由 `showDownloadResults`（[src/RequestGroupMan.cc:L696](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L696) 起）格式化输出。

### 4.2 RequestGroupMan：两级队列与任务调度

#### 4.2.1 概念说明

RequestGroupMan 是"任务调度台"，同时管三份名单：

- `requestGroups_`：活动队列，正在下载的任务（含做种中的）；
- `reservedGroups_`：等待队列，排队等待开始或被暂停的任务；
- `downloadResults_`：成绩单，已结束任务的 DownloadResult（上限 `--max-download-result`）。

`-j` 限制的是**活动队列的长度**（准确说是 `numActive_` 计数，做种分离 `--bt-detach-seed-only` 的任务不计入，见 [src/RequestGroupMan.h:L87-L92](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.h#L87-L92) 的注释）。调度不是每圈主循环都做，而是由脏标记 `queueCheck_` 驱动——只有当有任务"可能空出了名额"时才真正扫描队列，这是 aria2 单线程引擎里典型的"按需维护"优化。

#### 4.2.2 核心流程

`fillRequestGroupFromReserver` 的决策伪代码：

```text
function fillRequestGroupFromReserver(e):
    removeStoppedGroup(e)              # 先收割尸体，腾出名额
    上限 = optimize-concurrent-downloads ? 动态计算 : -j
    if 上限 <= numActive_: return      # 没名额，直接走

    while 已补充数 < (上限 - numActive_) 且 (还有等待任务 或 输入文件未读完):
        if 等待队列空 且 -i 输入文件还有内容:
            再解析一批 RequestGroup 进等待队列（惰性解析）
        group = 等待队列.pop_front()
        if (RPC 模式且 group 被暂停) 或 group.依赖未解决:
            pending.push(group); continue    # 跳过但保持队头相对顺序
        group.dropPieceStorage()             # 丢弃暂停期间持有的旧存储
        configureRequestGroup(group)         # 安装 URISelector
        group.setState(STATE_ACTIVE)
        ++numActive_
        活动队列.push_back(group)
        try: createInitialCommand(group, e)  # 制造第一批命令
        catch: 记录错误码，请求再次队列检查
        执行 on-download-start 钩子 + 通知事件
    把 pending 插回队头
```

`ProcessStoppedRequestGroup`（收割器）的判定伪代码：

```text
for group in 活动队列:
    if group.numCommand != 0: 保留           # 还有活命令，没结束
    else:
        if 是暂停: 存控制文件 → push_front 回等待队列 → STATE_WAITING
        elif 下载完成且校验通过:
            应用 Last-Modified → 上报完成 → postDownloadProcessing 派生新任务
            （派生任务 insertReservedGroup(0, ...) 插到等待队列最前面）
        else: createDownloadResult → 存成绩单 → 执行停止钩子
        releaseRuntimeResource（释放 BT 注册、保留进度供 RPC 查询）
```

#### 4.2.3 源码精读

**构造与初始装载。** [src/RequestGroupMan.cc:L105-L132](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L105-L132)：命令行给的所有任务一开始全部进等待队列（`appendReservedGroup(reservedGroups_, ...)`），活动队列是空的；`keepRunning_` 在启用 RPC 时为真——此时即使所有任务结束引擎也不退出（`downloadFinished()` 恒为假，[L153-L159](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L153-L159)），这是 RPC 守护进程模式的基石。

**fillRequestGroupFromReserver 主体。** [src/RequestGroupMan.cc:L515-L593](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L515-L593)。名额判断在 [L523-L525](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L523-L525)；取队头与"跳过但保序"在 [L546-L551](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L546-L551)：

```cpp
std::shared_ptr<RequestGroup> groupToAdd = *reservedGroups_.begin();
reservedGroups_.pop_front();
if ((keepRunning_ && groupToAdd->isPauseRequested()) ||
    !groupToAdd->isDependencyResolved()) {
  pending.push_back(groupToAdd);
  continue;
}
```

被跳过的任务最终插回队头（[L584-L587](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L584-L587)），所以"依赖未解决"不会饿死它后面的任务，也不会丢失它自己的队头优先级——这是 4.4 节依赖机制的另一半。激活五连击在 [L555-L560](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L555-L560)；`createInitialCommand` 抛异常（如重复下载、文件已存在）时不回滚入队，而是记下错误码让收割器走失败流程（[L571-L578](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L571-L578)）。

**收割器 ProcessStoppedRequestGroup。** [src/RequestGroupMan.cc:L289-L473](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L289-L473) 是一个仿函数，被 `removeStoppedGroup`（[L475-L484](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L475-L484)）用 `remove_if` 施加到活动队列。核心判定 `group->getNumCommand() == 0` 在 [L352](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L352)。三个出口中最有意思的是完成出口的派生逻辑（[L391-L398](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L391-L398)）：

```cpp
std::vector<std::shared_ptr<RequestGroup>> nextGroups;
group->postDownloadProcessing(nextGroups);      # Metalink/BT 后处理器
if (!nextGroups.empty()) {
  e_->getRequestGroupMan()->insertReservedGroup(0, nextGroups);  # 插队头
}
```

`postDownloadProcessing`（[src/RequestGroup.cc:L1064-L1082](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L1064-L1082)）遍历 PostDownloadHandler，谁能处理（比如下载到的是 .torrent/.metalink 文件）就由它产出下一批 RequestGroup。暂停出口（[L434-L455](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L434-L455)）把任务 `push_front` 回等待队列并释放运行时资源，但**故意不重置** segmentMan/pieceStorage（源码注释：为了 RPC 还能查进度）。

**谁驱动调度：脏标记 + 例程命令。** `requestQueueCheck()` 只是置位（[src/RequestGroupMan.h:L321-L328](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.h#L321-L328)）。真正干活的是 [FillRequestGroupCommand](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FillRequestGroupCommand.cc#L57-L97)，它在引擎装配时被注册为例程命令（[src/DownloadEngineFactory.cc:L163-L164](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngineFactory.cc#L163-L164)），每次执行先查脏标记、调 `fillRequestGroupFromReserver`，然后把自己重新排回 routineCommands_ 队尾：

```cpp
if (rgman->queueCheckRequested()) {
  while (rgman->queueCheckRequested()) {
    rgman->clearQueueCheck();
    rgman->fillRequestGroupFromReserver(e_);
    ...
  }
  if (rgman->downloadFinished()) { return true; }   # 全部结束，自杀
}
e_->addRoutineCommand(std::unique_ptr<Command>(this));  # 否则自循环
return false;
```

这正好复习了 u2-l3 的约定：返回 false 必须已自我重排。

**防重复下载。** [isSameFileBeingDownloaded（L862-L884）](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L862-L884) 收集活动队列中所有任务的文件路径，与 newcomers 的路径排序后做交集，命中即拒绝——这就是同一文件下载两次时报 `DUPLICATE_DOWNLOAD` 的出处。

#### 4.2.4 代码实践

**实践目标**：观察 `-j` 如何把多个任务排队串行化，并从日志确认"补充任务"的时机。

**操作步骤**：

1. 写一个三行的输入文件 `urls.txt`（三个不同文件名的 URL，可用 4.1.4 节的本地服务，或任意外网 URL）：
   ```text
   http://127.0.0.1:8001/a.bin
   http://127.0.0.1:8001/b.bin
   http://127.0.0.1:8001/c.bin
   ```
2. 运行 `./src/aria2c -j1 -i urls.txt -d /tmp/rg-test --log=rg2.log --log-level=info --summary-interval=1`。
3. 观察控制台进度行首部的 `[#gid 1.2MiB/3MiB(40%) CN:1 ...]`——同一时刻只有**一个** GID 在动。
4. 结束后看 Download Results 表：三行结果按完成顺序排列。

**需要观察的现象**：第一个文件下载完的瞬间，第二个 GID 立刻出现；日志（debug 级）里能找到 "1 RequestGroup(s) added." 与 "RequestGroup(s) deleted." 成对出现。

**预期结果**：`-j1` 下三个任务严格串行；若把 `-j` 改成 2，则前两个任务并行，第三个等名额。控制台输出与日志的对应关系「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`aria2.unpause` 之后任务是如何重新跑起来的？

答案：RPC 方法把 `pauseRequested_` 清掉后调用 `group->createNextCommand(commands, e)`（[src/RpcMethodImpl.cc:L1346](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodImpl.cc#L1346)）制造命令；任务本身还在等待队列队头，下一轮 `fillRequestGroupFromReserver` 发现它不再满足 `keepRunning_ && isPauseRequested()`，于是正常激活。

**练习 2**：为什么 `fillRequestGroupFromReserver` 开头要先调 `removeStoppedGroup`？

答案：名额以 `numActive_` 计，而任务结束（numCommand 归零）时 `numActive_` 并不立即减少；必须先收割尸体（`ProcessStoppedRequestGroup` 里 `decreaseNumActive()`）释放名额，后面的 while 循环才能拿到正确的 `num = maxConcurrentDownloads - numActive_`。顺序反了会导致新任务晚一轮才被补充。

**练习 3**：`--optimize-concurrent-downloads` 打开后 `-j` 还生效吗？

答案：生效但被动态化：[L519-L521](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L519-L521) 用 `optimizeConcurrentDownloads()`（[L1063 起](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L1063-L1086)）的返回值替代 `-j`，该函数按近期网速与系数 A/B 用二分法在 `-j` 附近浮动，且 FillRequestGroupCommand 会每秒强制重新请求队列检查以跟上速度变化。

### 4.3 URISelector：URI 从哪里被挑走

#### 4.3.1 概念说明

RequestGroup 自己不存 URI 列表——URI 池（`uris_`/`spentUris_`/`inFlightRequests_`）在 FileEntry 上。URISelector 回答一个问题："**现在要新开一条连接，该用哪个 URI？**" 它是一个只有三个虚函数的小接口（[src/URISelector.h:L48-L60](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/URISelector.h#L48-L60)）：

```cpp
class URISelector {
public:
  virtual std::string select(
      FileEntry* fileEntry,
      const std::vector<std::pair<size_t, std::string>>& usedHosts) = 0;
  virtual void tuneDownloadCommand(const std::deque<std::string>& uris,
                                   DownloadCommand* command){};
  virtual void resetCounters() { return; };
};
```

三种实现（`--uri-selector`，默认 feedback）：

| 实现 | 策略 | 依据 |
|---|---|---|
| InorderURISelector | 按输入顺序取队头，用完即止 | 无状态，最简单 |
| FeedbackURISelector | 优先挑"历史上快且当前不忙"的镜像 | ServerStatMan 保存的各主机历史速度/错误记录 |
| AdaptiveURISelector | 至少测 3 个镜像再择优，超时自动加倍重试 | ServerStat + 运行时连接数 |

ServerStatMan 的数据来自每次下载结束时的回填（`ProcessStoppedRequestGroup::collectStat`，[src/RequestGroupMan.cc:L313-L341](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L313-L341)），可用 `--server-stat-of`/`--server-stat-if` 跨会话保存。

#### 4.3.2 核心流程

挑选发生在 CreateRequestCommand 内部，完整链路：

```text
CreateRequestCommand::executeInternal
  └─ FileEntry::getRequest(selector, uriReuse, usedHosts, referer, method)
       └─ getRequestWithInFlightHosts(...)
            ├─ uri = selector->select(this, usedHosts)   # 策略点：选哪个 URI
            ├─ 统计 inFlightRequests_ 里各主机的在途数
            ├─ if 该主机在途数 >= maxConnectionPerServer_(-x):
            │     uri 挪进 pending，换下一个 URI          # 硬约束点：-x
            └─ 选定 → 移入 spentUris_，登记 inFlightRequests_
       （一轮没选中且允许 uriReuse → reuseUri 把用过的 URI 重新入池，再来一轮）
  └─ InitiateConnectionCommandFactory 按 URI scheme 造协议命令
     （http→HttpInitiate、ftp→FtpInitiate、magnet→Bt...、file→File...）
```

关键认知：**`-x` 的检查点在 FileEntry，不在 RequestGroup**；`usedHosts`（配合 `--select-least-used-host`）只是给选择器的参考信息，真正的硬限制是 `inFlightRequests_` 的主机计数。

#### 4.3.3 源码精读

**选择器的安装时机。** 任务构造时默认 Inorder（见 4.1.3）；被激活前由 [configureRequestGroup（L486-L502）](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L486-L502) 按选项覆盖：

```cpp
const std::string& uriSelectorValue =
    requestGroup->getOption()->get(PREF_URI_SELECTOR);
if (uriSelectorValue == V_FEEDBACK) {
  requestGroup->setURISelector(make_unique<FeedbackURISelector>(serverStatMan_));
}
else if (uriSelectorValue == V_INORDER) { ... }
else if (uriSelectorValue == V_ADAPTIVE) { ... }
```

**消费点：CreateRequestCommand。** [src/CreateRequestCommand.cc:L68-L127](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CreateRequestCommand.cc#L68-L127)。这条命令是并发扩张的直接产物（4.1.3 的 createNextCommand 造出来的就是它），它自己不碰网络，只做"选 URI + 移交"：

```cpp
setRequest(getFileEntry()->getRequest(
    getRequestGroup()->getURISelector().get(),
    getOption()->getAsBool(PREF_REUSE_URI), usedHosts, ...));
if (!getRequest()) {
  ... throw DL_ABORT_EX2("No URI available.", ...);   # URI 用尽的出路
}
getDownloadEngine()->addCommand(
    InitiateConnectionCommandFactory::createInitiateConnectionCommand(...));
```

拿到 URI 却发现对方主机连接已满、请求对象还在"冷却"（wake time 未到）时，它把请求放回池子并把自己重排（[L110-L119](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CreateRequestCommand.cc#L110-L119)）——又一次"返回 false 必先 addCommandSelf"的示范。

**`-x` 的真身。** [src/FileEntry.cc:L128-L179](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FileEntry.cc#L128-L179)：

```cpp
std::string uri = selector->select(this, usedHosts);
if (uri.empty()) { break; }
req = std::make_shared<Request>();
if (req->setUri(uri)) {
  if (std::count(std::begin(inFlightHosts), std::end(inFlightHosts),
                 req->getHost()) >= maxConnectionPerServer_) {
    pending.push_back(uri);          # 这台主机满了，URI 先存起来
    ignoreHost.push_back(req->getHost());
    req.reset();
    continue;                        # 换下一个 URI
  }
  ...
  spentUris_.push_back(uri);         # 用过的 URI 记账
  inFlightRequests_.insert(req);
```

注意 `req->getHost()` 只含主机名不含端口，所以 `http://127.0.0.1:8001/a` 与 `http://127.0.0.1:8002/b` 共享同一个 `-x` 配额。第一轮全军覆没且允许 `--reuse-uri` 时，[L170-L174](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FileEntry.cc#L170-L174) 调 `reuseUri` 把已用 URI 按配额重新灌回池子再选一轮。

**三种策略的实现差异。** [InorderURISelector::select（L45-L58）](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InorderURISelector.cc#L45-L58) 就是取队头；[FeedbackURISelector::select（L60-L86）](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FeedbackURISelector.cc#L60-L86) 先 `selectFaster`（历史快、当前用得少的主机）再退化为 `selectRarer`，并显式跳过 `ss->isError()` 的主机；[AdaptiveURISelector::selectOne（L126 起）](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AdaptiveURISelector.cc#L126-L174) 里有一条硬规则"至少测 3 个镜像"（[L146-L156](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AdaptiveURISelector.cc#L146-L156)），它还会在山穷水尽时把超时时间加倍、把超时失败的 URI 捞回来重试（[mayRetryWithIncreasedTimeout，L102-L124](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AdaptiveURISelector.cc#L102-L124)）。

#### 4.3.4 代码实践

**实践目标**：验证 `-x` 按主机名（而非端口）计数，对比两种 uri-selector 的挑选顺序。

**操作步骤**：

1. 用 4.1.4 的本地服务（或任一支持 Range 的服务），同一文件写三个 URL，其中两个同主机不同端口、一个换主机名：
   ```bash
   ./src/aria2c -s3 -x2 -k1M --min-split-size=1M --uri-selector=inorder \
     -d /tmp/rg-test --log=sel.log --log-level=info \
     http://127.0.0.1:8001/bigfile.bin \
     http://127.0.0.1:8002/bigfile.bin \
     http://localhost:8001/bigfile.bin
   ```
2. 观察进度行的 `CN:` 数值上限。
3. 把 `--uri-selector` 换成 `adaptive` 再跑一次，对比日志里 URI 被取用的先后顺序。

**需要观察的现象**：`127.0.0.1` 的两个端口合计只允许 2 条连接；`localhost` 是另一个主机字符串，单独享有 2 条配额，因此理论连接上限是 min(3, …) 内先打满 127.0.0.1 的 2 条。

**预期结果**：inorder 模式下三个 URI 按书写顺序被取用；同一主机第三条连接会被 FileEntry 挪进 pending，日志可看到连接数停在 2（若 localhost URI 被选中则可到 3）。具体数值「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么把 `-x` 从 1 改成 16 后，下载一个只提供 1 个 URI 的文件并没有变快？

答案：并发命令数受 `-s` 与 piece 数约束，连接数还受 URI 池约束：只有一个 URI 时，FileEntry 对同一主机的在途请求最多 1 条就无 URI 可选（[FileEntry.cc:L140-L152](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FileEntry.cc#L140-L152) 的循环会因 `uri.empty()` 退出），多余的 CreateRequestCommand 拿不到 Request 只能抛 "No URI available" 进入重试等待。多连接必须同时满足：多个 URI（或允许 reuse-uri 的大文件分段）、`-s` 足够、`-x` 足够。

**练习 2**：`spentUris_` 与 `uris_` 为什么要分开存？

答案：`uris_` 是待用队列（选择器从里面拿），`spentUris_` 是已用账本。分开存让 `--reuse-uri=true` 时能把用过的 URI 重新灌回（`reuseUri`），也方便 RPC `tellStatus` 汇报完整列表（`getUris` 拼接两者，[src/FileEntry.cc:L106-L111](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FileEntry.cc#L106-L111)）。

**练习 3**：FeedbackURISelector 的"反馈"数据从哪里来、存到哪里去？

答案：来自每次下载结束时 `ProcessStoppedRequestGroup::collectStat`（[src/RequestGroupMan.cc:L313-L341](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L313-L341)）对各 PeerStat 平均速度的回填，写入 RequestGroupMan 持有的 ServerStatMan；可用 `--server-stat-if/--server-stat-of` 跨进程持久化。

### 4.4 RequestGroupEntry、SequentialPicker 与任务依赖

#### 4.4.1 概念说明

本模块讲 RequestGroup 的两段"非主流"经历：

**（a）停车场：校验与预分配。** 任务激活后并不总是直奔网络——本地已有半截文件时要先做 hash 校验（`--check-integrity`），大文件可能要先预分配（`--file-allocation`）。这两件事由两个全局"停车场"串行处理：`FileAllocationMan` 与 `CheckIntegrityMan`。它们的类型定义惊人地简单（[src/FileAllocationMan.h:L45](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FileAllocationMan.h#L45)、[src/CheckIntegrityMan.h:L45](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CheckIntegrityMan.h#L45)）：

```cpp
typedef SequentialPicker<CheckIntegrityEntry> CheckIntegrityMan;
typedef SequentialPicker<FileAllocationEntry> FileAllocationMan;
```

SequentialPicker（[src/SequentialPicker.h:L46-L91](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SequentialPicker.h#L46-L91)）的语义是"队列 + 单座位"：`pickNext()` 把队头搬进 `pickedEntry_` 这个唯一座位，处理完 `dropPickedEntry()` 才能取下一个——同一时刻全引擎只对一个任务做校验/预分配，避免磁盘被打爆。

而 CheckIntegrityEntry/FileAllocationEntry 都继承自 **RequestGroupEntry**（[src/RequestGroupEntry.cc:L42-L49](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupEntry.cc#L42-L49)）：

```cpp
RequestGroupEntry::RequestGroupEntry(RequestGroup* requestGroup,
                                     std::unique_ptr<Command> nextCommand)
    : requestGroup_(requestGroup), nextCommand_(std::move(nextCommand))
{
  requestGroup_->increaseNumCommand();      # 入场：占一个命令名额
}

RequestGroupEntry::~RequestGroupEntry() { requestGroup_->decreaseNumCommand(); }
```

它解决两个问题：其一，任务进入停车场前手中那条"即将开工"的命令被扣在 `nextCommand_` 里随身携带（校验完直接放行，见 [StreamFileAllocationEntry.cc:L78-L96](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/StreamFileAllocationEntry.cc#L78-L96) 的 `popNextCommand`）；其二，用 RAII 维持 `numCommand_ > 0`——否则 4.2 节的收割器会误以为任务已结束而把它回收。

**（b）依赖：任务之间的父子链。** 有些任务在"前置任务完成"之前不具备开工条件。典型场景：Metalink 文件里除了 HTTP 镜像还内嵌了一个 `.torrent` 链接——aria2 会为 torrent 元数据单开一个任务（"先把种子文件下载下来"），正式的文件下载任务依赖它。抽象接口只有一行（[src/Dependency.h:L42-L47](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Dependency.h#L42-L47)）：

```cpp
class Dependency {
public:
  virtual bool resolve() = 0;  // throw()
};
```

`resolve()` 返回"现在能不能开工"。调用点正是 4.2 节看到的 `fillRequestGroupFromReserver` 跳过逻辑——依赖未解决的任务始终被挪回队头，每轮调度再问一次，直到某一天 `resolve()` 变为真。

#### 4.4.2 核心流程（依赖的生命周期）

```text
Metalink 解析（Metalink2RequestGroup）
  ├─ torrentRg = 下载 .torrent 的元数据任务（belongsTo = 正式任务的 GID）
  └─ 正式任务 rg.dependsOn(BtDependency(rg, torrentRg))
       │
       ▼ 每轮 fillRequestGroupFromReserver 询问 isDependencyResolved()
  ├─ torrentRg 还有命令在跑 → resolve() = false → rg 留在队头
  ├─ torrentRg 完成且成功 → 从其内存盘读出 torrent 内容，
  │     bittorrent::loadFromMemory 构建全新 DownloadContext，
  │     拷贝 URI/路径/每主机配额 → rg.setDownloadContext(context)
  │     → resolve() = true → rg 正常激活（此后走 BT 流程）
  └─ torrentRg 失败（numCommand==0 但没下完）→ 放弃 BT，
        rg 拿原有的 HTTP/FTP URI 继续 → resolve() = true
```

#### 4.4.3 源码精读

**任务如何进入停车场。** [processCheckIntegrityEntry（src/RequestGroup.cc:L529-L553）](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L529-L553) 在需要校验时把 entry 推进停车场并临时禁止保存控制文件：

```cpp
if ((option_->getAsBool(PREF_CHECK_INTEGRITY) ||
     downloadContext_->isChecksumVerificationNeeded()) &&
    entry->isValidationReady()) {
  entry->initValidator();
  disableSaveControlFile();          # 校验期间 Ctrl-C 不落盘，避免 0 字节控制文件
  e->getCheckIntegrityMan()->pushEntry(std::move(entry));
  return;
}
entry->onDownloadIncomplete(commands, e);   # 不需要校验 → 直接开工
```

**停车场的取车员。** 两个 dispatcher 例程命令在引擎装配时注册（[src/DownloadEngineFactory.cc:L165-L168](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngineFactory.cc#L165-L168)），共享同一个模板基类 [SequentialDispatcherCommand](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SequentialDispatcherCommand.h#L67-L80)：

```cpp
virtual bool execute() CXX11_OVERRIDE
{
  ...
  if (picker_->hasNext() && !picker_->isPicked()) {   # 座位空着才取下一个
    e_->addCommand(createCommand(picker_->pickNext()));
    e_->setNoWait(true);
  }
  e_->addRoutineCommand(std::unique_ptr<Command>(this));  # 自循环
  return false;
}
```

**取车之后的爆发。** [StreamFileAllocationEntry::prepareForNextAction（L61-L100）](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/StreamFileAllocationEntry.cc#L61-L100)：预分配完成后放行被扣住的命令，并立即调用 `createNextCommandWithAdj` 把并发补满——这就是 4.1 节"长度已知后扩张"的准确触发位置。

**依赖的接线与判定。** 接线在 Metalink 解析处（[src/Metalink2RequestGroup.cc:L333-L358](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Metalink2RequestGroup.cc#L333-L358)）：`rg->dependsOn(dep)`、`torrentRg->belongsTo(rg->getGID())`，并且当所有文件都有 HTTP URI 兜底时给元数据任务设 30 秒的 `--bt-stop-timeout`——"有退路就不为种子等太久"。判定在 [BtDependency::resolve（L87-L181）](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtDependency.cc#L87-L181)，三个分支的判据都是 `dependee_->getNumCommand() == 0`（前置任务是否已停）：

```cpp
if (dependee_->getNumCommand() == 0 && dependee_->downloadFinished()) {
  // 成功：读出 torrent → 构建 context → dependant_->setDownloadContext(context)
  return true;
}
else if (dependee_->getNumCommand() == 0) {
  // 前置任务失败：放弃 BT，仅用 HTTP/FTP URI
  return true;
}
else {
  return false;   // 前置任务还在跑，我继续等
}
```

注意成功分支里 `dependee_.reset()` 先切断引用、`copyValues` 把正式任务的 URI、路径、`maxConnectionPerServer` 灌进新 context——4.3 节的 `-x` 配额就这样跨任务传递。RequestGroup 侧的门面方法在 [src/RequestGroup.cc:L1118-L1129](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L1118-L1129)（`isDependencyResolved`/`dependsOn`，无依赖时恒真）。

#### 4.4.4 代码实践（源码阅读型）

**实践目标**：不运行程序，靠阅读证明"校验期间任务不会被收割器误杀"，并画出调用链。

**操作步骤**：

1. 从 `ProcessStoppedRequestGroup::operator()` 的判定 [src/RequestGroupMan.cc:L352](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupMan.cc#L352)（`group->getNumCommand() == 0`）出发。
2. 找到 CheckIntegrityEntry 的构造函数（[src/CheckIntegrityEntry.cc:L49](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CheckIntegrityEntry.cc#L49) 附近，它把 `RequestGroupEntry` 基类构造为 `{requestGroup, std::move(nextCommand)}`）。
3. 顺着 [RequestGroupEntry.cc:L42-L49](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroupEntry.cc#L42-L49) 的 `increaseNumCommand()` 闭合这条链。
4. 可选的实际验证（需已有半截文件）：`./src/aria2c --check-integrity=true -c <url>`，日志中观察校验阶段任务仍显示为活动状态。

**需要观察的现象 / 预期结果**：调用链为 `CheckIntegrityEntry 构造 → RequestGroupEntry 构造 → increaseNumCommand() → numCommand_ ≥ 1 → ProcessStoppedRequestGroup 判定不成立 → 任务保命`；校验 entry 析构时计数归零，收割器下一轮才接管。这条推理链完全由静态阅读得出，可自行在纸上画成时序图。

#### 4.4.5 小练习与答案

**练习 1**：SequentialPicker 为什么设计成"单座位"而不是并行处理多个 entry？

答案：校验与预分配都是磁盘 I/O 密集操作，并行化会让多个任务同时顺序读盘/写盘，磁头来回抖动反而更慢；单座位还天然简化了"同一时刻只做一个大校验"的进度显示。引擎侧用 `hasNext() && !isPicked()` 保证上一个没 drop 之前不取下一个（[SequentialDispatcherCommand.h:L72-L76](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SequentialDispatcherCommand.h#L72-L76)）。

**练习 2**：magnet 链接（`magnet:?xt=...`）下载也用到了本讲的哪个机制？

答案：也用"元数据任务 + 依赖"的两段式，但方向相反：magnet 先创建一个只下载 ut_metadata 的任务（metadataGetMode，用 UnknownLengthPieceStorage），真正的文件下载任务由 UTMetadataPostDownloadHandler 在其完成后派生，并通过 followedBy/belongsTo GID 关联——细节在 u6-l9 展开。本讲你要记住的是：无论 Metalink 还是 magnet，"先下元数据再下数据"都落在 RequestGroup 的 Dependency 与 PostDownloadHandler 两个扩展点上。

**练习 3**：如果把 `ProcessStoppedRequestGroup` 里 `getNumCommand() == 0` 改成 `!downloadFinished()` 就收割，会发生什么？

答案：所有正在校验/预分配/做种的任务会被立刻判死：做种任务 `downloadFinished()` 为真倒没事，但校验中的任务文件没下完，会被错误地记为失败并写 DownloadResult，且停车场的 entry 析构时 `decreaseNumCommand()` 会在任务已销毁后访问悬垂的 `requestGroup_` 指针（RequestGroupMan 持有 shared_ptr 所以对象还在，但状态机已被破坏，暂停/恢复不再工作）。这从反面说明了 `numCommand_` 作为"任务是否仍在运转"唯一判据的设计意义。

## 5. 综合实践

把本讲三个模块串起来，完成规格中的核心实验：**用 3 个 URL 下载同一文件并加 `-x2 -s3`，数一数同一个 RequestGroup 到底开了几条 HTTP 连接，并用源码解释**。

**环境准备**（本地可复现方案）：

```bash
mkdir -p /tmp/rg-www && cd /tmp/rg-www
head -c 10000000 /dev/urandom > bigfile.bin   # 10MB 测试文件
pip install rangehttpserver                    # 提供带 Range 支持的静态服务
python3 -m RangeHTTPServer 8001 &
python3 -m RangeHTTPServer 8002 &
```

（没有本地条件时，可改用任一发行版镜像站的同一文件的三台镜像，结论一致。）

**实验步骤**：

1. **三主机变体**（三个不同主机名：`127.0.0.1` 与 `localhost` 视为不同主机）：
   ```bash
   ./src/aria2c -s3 -x2 -k1M --min-split-size=1M -d /tmp/rg-out \
     --log=exp1.log --log-level=debug --summary-interval=1 \
     http://127.0.0.1:8001/bigfile.bin \
     http://localhost:8001/bigfile.bin \
     http://localhost:8002/bigfile.bin
   ```
2. **单主机变体**（三个 URL 全部是 `127.0.0.1`，仅端口/路径不同）：
   ```bash
   ./src/aria2c -s3 -x2 -k1M --min-split-size=1M -d /tmp/rg-out2 \
     --log=exp2.log --log-level=debug \
     http://127.0.0.1:8001/bigfile.bin \
     http://127.0.0.1:8001/bigfile.bin?mirror=2 \
     http://127.0.0.1:8002/bigfile.bin
   ```
3. 每次运行后：看进度行 `CN:` 的最大值；在日志里 grep `CUID#` 去重计数；grep `Download Results` 确认只有一个 GID（一个任务）。

**预期结果与源码对照**：

| 变体 | 预期最大连接数 | 决定它的源码 |
|---|---|---|
| 三主机 | 3（受 `-s3` 限制） | [createNextCommandWithAdj](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L827-L844)：`min(piece数, -s)`；每主机 1 条 < `-x2` |
| 单主机 | 2（受 `-x2` 限制） | [FileEntry.cc:L146-L152](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FileEntry.cc#L146-L152)：同主机在途数达到 2 后 URI 全部进 pending |

进一步把 `-x` 改成 1 重跑单主机变体：应当只观察到 1 条连接、其余命令反复进入 "No URI available" 的重试节奏——这条日志的抛出点正是 [CreateRequestCommand.cc:L94-L109](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CreateRequestCommand.cc#L94-L109)。所有具体数值均「待本地验证」（服务器对 Range 的支持情况会影响分段行为）。

## 6. 本讲小结

- **RequestGroup 是任务容器**：聚合 Option/DownloadContext/SegmentMan/PieceStorage/URISelector/Dependency，用 `numConcurrentCommand_`（`-s`）作为并发命令预算，命令构造/析构自动维护三个计数器，`numCommand_` 归零即宣告任务结束并触发 `requestQueueCheck`。
- **并发扩张有公式**：长度已知后由 `createNextCommandWithAdj` 扩张，同时存在的流式命令数 ≤ \(\min(s, P)\)；`-x` 不在 RequestGroup 层，而在 FileEntry 挑选 URI 时按"主机名在途计数"强制执行。
- **RequestGroupMan 两级队列调度**：等待队列→活动队列由 `fillRequestGroupFromReserver` 完成，名额由 `-j`（或动态优化值）与 `numActive_` 决定；脏标记 + FillRequestGroupCommand 例程命令实现按需维护；收割器按 `numCommand == 0` 处理暂停/完成/失败三条出口，完成出口经 PostDownloadHandler 派生新任务并插队到等待队列头。
- **URISelector 是可插拔策略**：inorder/feedback/adaptive 在 `configureRequestGroup` 安装、在 `CreateRequestCommand → FileEntry::getRequest` 消费，反馈数据由 ServerStatMan 沉淀。
- **两类跨任务机制**：校验/预分配通过 RequestGroupEntry + SequentialPicker 的"停车场"挂起（RAII 维持 numCommand_ 保命）；元数据依赖通过 Dependency/BtDependency 表达，未解决的任务被调度器反复挪回队头。

## 7. 下一步学习建议

- 下一讲 **u3-l2（DownloadContext）**：本讲反复出现的 `downloadContext_` 到底装了什么——文件列表、piece 长度、校验和、协议属性（TorrentAttribute 等）都在那里，是理解 BT/Metalink 挂载点的前提。
- 之后 **u3-l4（SegmentMan 与分段调度）**：本讲只回答"开几条命令"，"每条命令负责哪个字节区间"由 SegmentMan 与 BitfieldMan 回答，两者合起来才是多线程下载的全貌。
- 想先看调度全景的读者，可以回头重读 [src/DownloadEngineFactory.cc:L148-L168](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngineFactory.cc#L148-L168)：引擎装配时一次性注册的 FillRequestGroupCommand、FileAllocationDispatcherCommand、CheckIntegrityDispatcherCommand 正是本讲三个机制的启动按钮。
- 单元 6（BT）与 u6-l9（magnet）会再次用到本讲的 Dependency 与 followedBy/belongsTo GID 链，届时可回来对照 `BtDependency::resolve` 的成功分支。
