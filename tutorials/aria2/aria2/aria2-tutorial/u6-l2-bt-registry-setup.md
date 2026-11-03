# u6-l2 BtRegistry 与 BtSetup：BT 运行时的装配

## 1. 本讲目标

u6-l1 解决了「种子文件怎么变成任务元数据」，本讲回答紧接着的下一个问题：**一个 BT 任务从"纸面元数据"变成"活的下载"，引擎到底创建了哪些运行时对象？这些对象放在哪里、谁能找到它们、什么时候销毁？**

学完本讲你应该能够：

1. 解释 `BtRegistry` 的注册表设计：以 GID 为键的 `BtObject` 六件套、以 infohash 反查任务的线性扫描，以及它挂载的四个进程级全局槽位（TCP 端口、UDP 端口、LPD 接收器、UDP Tracker 客户端）。
2. **纠正一个常见误解**：在当前代码里，「整套数据对象」并不是 `BtSetup` 创建的——六件套在 `RequestGroup::createInitialCommand` 里装配并登记进注册表；`BtSetup::setup` 装配的是**种子级周期命令**和**一次性的全局初始化**（监听端口、LPD），最后扳下 `btRuntime->setReady(true)` 这个放行闸门。
3. 跟踪完整的装配时序：`createInitialCommand` → 校验停车场（u5-l5）→ 预分配停车场（u5-l4）→ `BtFileAllocationEntry::prepareForNextAction` → `BtSetup::setup`。
4. 说出「进程级 / 种子级 / peer 级」三层对象的边界：peer id 与 DHT 是进程级，BtObject 六件套是种子级，BtInteractive 一族是 peer 级（不入注册表）。
5. 列出一次种子下载被创建的 10 个以上关键对象，并注明每个对象在后续哪讲中被展开。

## 2. 前置知识

### 2.1 为什么需要"装配"和"注册表"

BT 下载与 HTTP 下载有一个结构性差异：HTTP 任务的运行时状态基本都挂在命令链自己身上（u4-l1 的接力棒模型），而 BT 任务的许多组件要**跨命令、跨连接共享**：

- 来了一个**主动连入**的 peer（别人连你），接盘的命令必须能根据握手里的 infohash 找到"这个种子对应哪些共享对象"；
- DHT 节点收到 `get_peers` 询问"你有 xx 种子的 peer 吗"，也要能按 infohash 找到本进程内对应的任务；
- 控制台要显示某个种子的 seeder 数，RPC 要查询某个 GID 的 peer 列表。

这些"按 key 找任务运行时"的需求，就要求有一个进程级的地方集中登记每个种子的共享对象——这就是 `BtRegistry`（注册表模式，Registry Pattern）存在的理由。

### 2.2 GID 与 infohash：两把不同的钥匙

- **GID**（u3-l1）：每个下载任务的 16 字节全局唯一编号，`RequestGroup` 的主键，注册表 `pool_` 的键就是它的数值形式 `a2_gid_t`。
- **infohash**（u6-l1）：种子 `info` 字典的 SHA-1，**内容的**指纹。同一个 infohash 在一个进程里只允许注册一次（否则两个任务会互相打架）。

注册表支持两把钥匙查同一个条目：按 GID 查是 \( O(\log n) \)（`std::map`），按 infohash 查是 \( O(n) \) 线性扫描（n 为并发种子数，通常个位数到几十，性能无虞）。

### 2.3 承接前讲

- u3-l1：`RequestGroup` 是任务容器；本讲会看到它的 `createInitialCommand` 兼任 BT 数据对象的"总装车间"。
- u5-l4/u5-l5：校验（CheckIntegrityMan）与预分配（FileAllocationMan）都是"单槽停车场"；本讲的 `BtSetup` 恰好挂在预分配停车场的出口 `prepareForNextAction` 上——理解这个衔接是本讲时序的关键。
- u6-l1：种子解析产物挂在 `DownloadContext` 的 `CTX_ATTR_BT` 属性槽（`TorrentAttribute`）；本讲的一切装配都以 `hasAttribute(CTX_ATTR_BT)` 为入口判定。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/BtRegistry.h/.cc` | 本讲主角一：注册表本体，`BtObject` 六件套 + 全局槽位 |
| `src/RequestGroup.cc` | **真正的数据对象装配点**：`createInitialCommand` 的 BT 分支（L271-L462） |
| `src/BtSetup.h/.cc` | 本讲主角二：种子级周期命令的装配 + 一次性全局初始化 |
| `src/BtFileAllocationEntry.cc` | 调用 `BtSetup().setup` 的唯一位置（预分配出口） |
| `src/BtCheckIntegrityEntry.cc` | 校验完成后的分流：`onDownloadIncomplete`/`onDownloadFinished` 都通向 `BtFileAllocationEntry` |
| `src/bittorrent_helper.cc` | 进程级全局状态：静态 peer id / peer agent |
| `src/Context.cc` | 静态 peer id 的初始化时机（构造函数） |
| `src/BtRuntime.h` | 种子级运行时开关与计数器（六件套之一） |
| `src/DownloadEngine.cc` | 注册表的所有权：引擎构造时创建 |
| `src/PeerReceiveHandshakeCommand.cc` | 消费方示例①：被动连接按 infohash 反查 |
| `src/LpdReceiveMessageCommand.cc`、`src/DHTGetPeersMessage.cc` | 消费方示例②③：LPD 与 DHT 按_infohash 反查 |
| `src/ConsoleStatCalc.cc`、`src/RpcMethodImpl.cc` | 消费方示例④⑤：按 GID 查（进度条 SD 数、tellStatus/getPeers/changeOption） |
| `src/PeerInteractionCommand.cc` | 对照组：peer 级对象（BtInteractive 等）在这里创建，**不入注册表** |
| `test/*.torrent` | 仓库自带迷你种子（single.torrent、test.torrent 等），实践材料 |

## 4. 核心概念与源码讲解

### 4.1 BtRegistry：以 GID 为键的 BT 运行时注册表

#### 4.1.1 概念说明

`BtRegistry` 是 `DownloadEngine` 拥有的一个普通对象（不是全局单例），内部只有一张 map 和四个全局槽位。它的价值不在复杂逻辑——全部实现不到 130 行——而在**所有权设计**：

- 每个种子任务的六个共享对象被打包成一个 `BtObject`，以 GID 为键存进 `pool_`（`std::map<a2_gid_t, std::unique_ptr<BtObject>>`）；
- 注册表**独占**持有 `BtObject`（`unique_ptr`），而 `BtObject` 内部对六个成员用 `shared_ptr` 共享——这样任务结束后命令们还能安全地用残余引用收尾，注册表删条目即宣告"新客人不得入内"；
- 除了 per-种子 的池子，注册表还顺手保管了四个**进程级**单件：BT 监听 TCP 端口、DHT/UDP-Tracker 用的 UDP 端口、LPD 多播接收器、UDP Tracker 客户端。它们与具体种子无关，放在这里是"就近托管"。

#### 4.1.2 核心流程

一个 `BtObject` 条目的生命周期：

```
创建   RequestGroup::createInitialCommand（成为活动任务时）
         ├─ 六件套逐个 new（见 4.2.3）
         └─ btRegistry->put(gid, BtObject{...})          ← 唯一的写入点
存活   引擎主循环期间，多类消费者按键查询：
         ├─ 按 GID：BtSetup、RPC(tellStatus/getPeers/changeOption)、进度条
         └─ 按 infohash：被动握手、LPD、DHT get_peers 应答
销毁   RequestGroup::releaseRuntimeResource（任务完成/被移除/暂停释放资源时）
         └─ btRegistry->remove(gid)                       ← 此后同 infohash 的
                                                            连入会被拒绝
```

注意两条查询路径的返回约定：按 GID 查不到返回 `nullptr`；按 infohash 查不到返回一个**空 shared_ptr 的常引用**（`getNull<T>()` 技巧，避免构造临时对象），调用方一律以"查无此任务"处理。

#### 4.1.3 源码精读

先看数据结构。`BtObject` 就是六个 `shared_ptr` 的打包：

- [src/BtRegistry.h:L56-L72](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtRegistry.h#L56-L72)：`BtObject` 六件套——`downloadContext`（元数据）、`pieceStorage`（分块存储）、`peerStorage`（peer 池）、`btAnnounce`（tracker 交互）、`btRuntime`（运行时开关）、`btProgressInfoFile`（.aria2 控制文件）。
- [src/BtRegistry.h:L74-L83](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtRegistry.h#L74-L83)：注册表私有成员——`pool_`（GID → BtObject）、`tcpPort_`（BT 监听端口，两代监听命令共用）、`udpPort_`（DHT 与 UDP tracker 共用，注释说明 IPv6 上暂不支持 UDP tracker）、`lpdMessageReceiver_` 与 `udpTrackerClient_`（两个全局单件的托管槽位）。
- [src/BtRegistry.h:L87-L127](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtRegistry.h#L87-L127)：公有接口全家福——两个 `getDownloadContext` 重载、`put/get/remove/removeAll`、端口与全局单件的存取器，以及模板方法 `getAllDownloadContext`（把池子里所有任务的 `DownloadContext` 收集到输出迭代器）。

再看实现，重点体会"薄"：

- [src/BtRegistry.cc:L52-L62](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtRegistry.cc#L52-L62)：按 GID 取 `DownloadContext`；查无时返回 `getNull<DownloadContext>()`。
- [src/BtRegistry.cc:L64-L74](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtRegistry.cc#L64-L74)：按 infohash 查——**遍历整个池子**，逐个取任务的 `TorrentAttribute` 比对 `infoHash` 字符串。这就是被动连接能"按内容找到任务"的落点。
- [src/BtRegistry.cc:L76-L94](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtRegistry.cc#L76-L94)：`put`（`pool_[gid] = move(obj)`，注意同 GID 重复 put 会直接覆盖，所以写入方有 `assert(!get(gid))` 防御）、`get`、`remove`、`removeAll`。
- [src/NullHandle.h:L44-L50](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/NullHandle.h#L44-L50)：`getNull<T>()`——函数模板里的静态空 `shared_ptr`，让"查无"可以返回常引用而不是值。

写入点只有一处，在 `RequestGroup::createInitialCommand`：

- [src/RequestGroup.cc:L343-L348](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L343-L348)：`assert` 确认该 GID 尚未注册，然后 `btRegistry->put(gid, make_unique<BtObject>(六件套))`。注意最后一个参数的三目表达式：magnet 元数据模式下没有 `DefaultBtProgressInfoFile`，退化传入成员 `progressInfoFile_`（空对象）。

注销点也只有一处：

- [src/RequestGroup.cc:L1024-L1030](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L1024-L1030)：`releaseRuntimeResource` 里 `remove(gid)` 并清空 `btRuntime_`/`peerStorage_` 裸指针。注释特意说明**不**重置 `segmentMan_` 和 `pieceStorage_`——RPC 还要靠它们汇报已完成任务的进度信息（注册表条目已删，但 RequestGroup 自己还留着只读快照）。

三个典型消费方（后续讲义的主角，这里只看它们如何"查表"）：

- [src/PeerReceiveHandshakeCommand.cc:L99-L123](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerReceiveHandshakeCommand.cc#L99-L123)：被动连接收到 48 字节后切出 infohash，先 `getDownloadContext(infoHash)` 查任务（查不到抛 "Unknown info hash"），再 `get(ownerGid)` 取整个 `BtObject` 拿 `btRuntime`/`pieceStorage`/`peerStorage`；`!btRuntime->ready()` 同样拒绝——这解释了为什么 `setReady` 之前来的连接会被丢弃。
- [src/LpdReceiveMessageCommand.cc:L77-L99](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LpdReceiveMessageCommand.cc#L77-L99)：LPD 多播消息的接收循环，同样按 infohash 找 `DownloadContext`，私有种子直接忽略，找到后把宣告的 peer 塞进该任务的 `peerStorage`。
- [src/DHTGetPeersMessage.cc:L74-L110](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTGetPeersMessage.cc#L74-L110)：本机作为 DHT 节点应答 `get_peers` 时，`addLocalPeer` 用 `btRegistry->getDownloadContext(infoHash)` 判断"我自己有没有这个种子"，有且配置了 `--bt-external-ip` 就把自己也加进应答的 peer 列表。

按 GID 查的消费方：

- [src/ConsoleStatCalc.cc:L183-L188](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ConsoleStatCalc.cc#L183-L188)：BT 进度行里的 `SD:`（seeder 数）就是 `getBtRegistry()->get(gid)->peerStorage->getUsedPeers()` 数出来的。
- [src/RpcMethodImpl.cc:L793-L797](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodImpl.cc#L793-L797)：`aria2.tellStatus` 的 BT 附加字段（seeder/leecher 等）经由 `gatherProgressBitTorrent` 从注册表取 `BtObject`。
- [src/RpcMethodImpl.cc:L1638-L1645](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodImpl.cc#L1638-L1645)：`aria2.changeOption` 运行期修改 `--bt-max-peers`，直接改注册表里 `btObject->btRuntime` 的 maxPeers——运行时对象可以被 RPC 热改，这是注册表集中托管的直接红利。

所有权归属：

- [src/DownloadEngine.cc:L104-L106](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L104-L106)：注册表在引擎构造函数里创建，随引擎共存亡；因此 `--enable-rpc` 常驻进程里它可以活得很久，而单次命令行进程里它与主循环同寿。

#### 4.1.4 代码实践：亲眼看到 infohash 去重与条目注销

**实践目标**：验证注册表"同 infohash 只注册一次"的守门逻辑，并理解条目注销后任务即"失联"。

**操作步骤**：

1. 用仓库自带的种子文件，把**同一个**种子在命令行里传两遍：
   ```bash
   ./src/aria2c -d /tmp/bt-test1 test/single.torrent test/single.torrent
   ```
2. 观察下载结果摘要（`Download Results` 一节）里两个任务各自的状态与错误信息。
3. 对照源码：第二个任务的 `createInitialCommand` 在 [src/RequestGroup.cc:L287-L295](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L287-L295) 处被 `getDownloadContext(infoHash)` 拦下，抛出 `DUPLICATE_INFO_HASH`（错误码 12，见 [src/error_code.h:L57-L58](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/error_code.h#L57-L58)）。
4. 再做一个对照实验：去掉一个参数，只下载一次但 Ctrl-C 中断后立即重跑同一命令——这次不会报重复，因为进程退出时旧注册表随之消失，新进程从空表开始。

**需要观察的现象**：第二个任务报 `InfoHash xxx... is already registered.`；第一个任务则照常进入后续流程（tracker 连不上是另一回事，与本实验无关）。

**预期结果**：摘要里一个任务显示重复注册错误；进程退出码非 0。由于 `MultiUrlRequestInfo::getResult` 取的是"最后一个错误结果"（[src/MultiUrlRequestInfo.cc:L320-L330](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiUrlRequestInfo.cc#L320-L330)），两个任务都失败时退出码取决于谁最后结束，具体数值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `pool_` 的值类型是 `unique_ptr<BtObject>`，而 `BtObject` 里的六个成员却是 `shared_ptr`？

答案：注册表是 `BtObject` 的唯一所有者，条目生死由 `put/remove` 一手掌控，用 `unique_ptr` 表达独占；而六个成员对象在任务运行期间被大量命令（PeerInteractionCommand、TrackerWatcherCommand、RPC 处理器等）按引用共享，任务结束时这些命令可能还活着并持有残留引用，`shared_ptr` 保证最后一个使用者析构时对象才释放。

**练习 2**：按 infohash 查找是 O(n) 线性扫描，为什么 aria2 不建一张 infohash → GID 的哈希表？

答案：n 是**并发注册的种子数**，受 `-j`（默认 5）与做种任务数约束，量级是个位到几十；线性扫描一次仅做字符串比较，成本可忽略，而维护第二张索引表反而要处理两表一致性。这是典型的"按实际数据规模选数据结构"的取舍。

**练习 3**：一个种子的下载完成、任务从注册表移除后，又有 peer 拿着该 infohash 连进来，会发生什么？

答案：[PeerReceiveHandshakeCommand.cc:L104-L109](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerReceiveHandshakeCommand.cc#L104-L109) 的 `getDownloadContext(infoHash)` 返回空，命令抛出 `DL_ABORT_EX("Unknown info hash ...")`，该连接被丢弃。

### 4.2 BtSetup：装配的是"命令"，不是"数据"

#### 4.2.1 概念说明

本讲的标题容易让人以为 `BtSetup` 负责"创建一切"——**这是对当前代码最常见的误解**。真实的分工是三段式：

| 阶段 | 装配者 | 产物 | 时机 |
|------|--------|------|------|
| ① 数据对象 | `RequestGroup::createInitialCommand` | 六件套 + `put` 进注册表 | 任务从等待队列提升为活动任务时 |
| ② 种子级命令 | `BtSetup::setup` | TrackerWatcher、PeerChoke、ActivePeerConnection、DHTGetPeers、SeedCheck、BtStopDownload 等周期命令；顺带做一次性的端口绑定与 LPD 初始化；最后 `setReady(true)` | 校验与预分配完成之后 |
| ③ peer 级对象 | `PeerInteractionCommand` | DefaultBtInteractive、消息收发三件套、扩展注册表等 | 每个 peer 连接建立时，**不入注册表** |

为什么 ② 要等校验/预分配完成才做？因为 `BtSetup` 创建的命令会立刻开始对外活动（announce、接受连入），而此时本地的 piece 状态必须已经定型——否则刚连入的 peer 问"你有哪些块"时位图还是空的。`btRuntime->ready()` 标志（[src/BtRuntime.h:L92-L94](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtRuntime.h#L92-L94)）就是这个"可以营业了"的闸门：`BtSetup` 最后一行才置 true，而被动握手命令在接纳 peer 前必查它。

`BtRuntime`（六件套之一）本身值得单独认识：它是种子级的"计数器+开关盒"——当前连接数 `connections_`、上限 `maxPeers_`（`--bt-max-peers`）、下限 `minPeers_`、停机标志 `halt_`、营业标志 `ready_`，见 [src/BtRuntime.h:L42-L102](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtRuntime.h#L42-L102)。choke 算法、连接补充、优雅停机都读它。

#### 4.2.2 核心流程

一个普通 .torrent 文件任务的完整装配时序（magnet 的差异见下）：

```
RequestGroup::createInitialCommand            [数据装配 + 注册]
 ├─ 重复 infohash 检查（查注册表）
 ├─ initPieceStorage()（多文件再 setupFileFilter）
 ├─ new DefaultBtProgressInfoFile（.aria2 控制文件代理）
 ├─ new BtRuntime（读 --bt-max-peers）
 ├─ new DefaultPeerStorage
 ├─ new DefaultBtAnnounce（读 --bt-tracker-interval，shuffle tracker）
 ├─ btRegistry->put(gid, BtObject{六件套})    ← 注册！
 ├─ （首个 BT 任务且开 DHT）DHTSetup().setup(e, AF_INET/6)  ← 进程级，一次性
 ├─ 种子带 nodes 且非私有 → DHTEntryPointNameResolveCommand（DHT 自举）
 ├─ .aria2 存在则 load / 目标文件存在则按选项处置
 └─ processCheckIntegrityEntry(BtCheckIntegrityEntry)        → 校验停车场
        ↓ （位图校验完成）
BtCheckIntegrityEntry::onDownloadIncomplete
 └─ proceedFileAllocation(make_unique<BtFileAllocationEntry>) → 预分配停车场
        ↓ （预分配完成或按阈值跳过）
BtFileAllocationEntry::prepareForNextAction
 └─ BtSetup().setup(commands, rg, e, option)                 [命令装配]
        ↓
BtSetup::setup
 ├─ 从注册表 get(gid) 取回六件套引用
 ├─ TrackerWatcherCommand（tracker announce 守护）
 ├─ PeerChokeCommand（choke 周期，magnet 模式跳过）
 ├─ ActivePeerConnectionCommand（补连接，magnet 2s / 普通 10s）
 ├─ DHTGetPeersCommand ×（IPv4/IPv6，私有种子跳过）
 ├─ SeedCheckCommand（Union: --seed-time / --seed-ratio）
 ├─ 绑定 BT 监听端口（进程级只绑一次，IPv4→IPv6 同端口）
 ├─ LPD 接收器 + 派发器（进程级只建一次，--bt-enable-lpd）
 ├─ BtStopDownloadCommand（--bt-stop-time > 0 时）
 └─ btRuntime->setReady(true)                                ← 开门营业
```

magnet（元数据获取模式，`torrentAttrs->metadata.empty()`，u6-l9 展开）的差异：无 `DefaultBtProgressInfoFile`、无 PeerChoke/SeedCheck、DHT 照常（magnet 更依赖 DHT）、`ActivePeerConnectionCommand` 轮询间隔缩短为 2 秒。已经完整下载、纯做种的任务（如 `--bt-seed-unverified=true` 且文件齐全）则经 `onDownloadFinished` 分支同样汇入 `BtFileAllocationEntry` → `BtSetup`。

#### 4.2.3 源码精读

**第一段：数据对象的真正出生地**（在 `RequestGroup.cc`，不在 `BtSetup.cc`）：

- [src/RequestGroup.cc:L271-L299](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L271-L299)：`createInitialCommand` 开头——重置会话计时器后，`hasAttribute(CTX_ATTR_BT)` 判定进入 BT 分支；`--dry-run` 直接拒绝（BT 没有"只看看不下载"）；按 infohash 查注册表防重复；magnet 模式走 `initPieceStorage()`（内部创建 `UnknownLengthPieceStorage`，u5-l2 讲过"长度未知退化单生长 piece"）。
- [src/RequestGroup.cc:L300-L311](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L300-L311)：普通模式——先查"同文件是否已在下载"，然后 `initPieceStorage()` + 多文件时 `setupFileFilter()`（`--select-file` 落成的过滤位图，u5-l3）。
- [src/RequestGroup.cc:L313-L317](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L313-L317)：六件套之六——`DefaultBtProgressInfoFile`（.aria2 控制文件的加载/保存代理，u6-l11）；magnet 模式不创建。
- [src/RequestGroup.cc:L319-L324](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L319-L324)：六件套之五——`BtRuntime`，`setMaxPeers(--bt-max-peers)`；RequestGroup 同时记一个裸指针 `btRuntime_`，用于暂停/停止时广播 `setHalt`（[src/RequestGroup.cc:L1000-L1012](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L1000-L1012)）。
- [src/RequestGroup.cc:L326-L332](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L326-L332)：六件套之三——`DefaultPeerStorage`，接线 BtRuntime 与 PieceStorage，并注册进进度信息文件（恢复时还原 peer 列表）。
- [src/RequestGroup.cc:L334-L341](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L334-L341)：六件套之四——`DefaultBtAnnounce`，接线三件，应用 `--bt-tracker-interval` 用户覆盖，`shuffleAnnounce()` 打乱 tier 内 tracker 顺序（u6-l1 讲过的 AnnounceList 游标在这里拿到底层数据）。
- 注册动作见 4.1.3 引用的 L343-L348。

**第二段：DHT 的进程级一次性装配**（挂在首个 BT 任务的初始化里）：

- [src/RequestGroup.cc:L350-L398](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L350-L398)：`--enable-dht`/`--enable-dht6` 开启时调用 `DHTSetup().setup(e, family)`，产出的命令一半进普通队列、一半进例程队列；种子 `nodes` 字段非空且非私有时，再创建 `DHTEntryPointNameResolveCommand` 做 DHT 自举。`DHTSetup` 内部有 `DHTRegistry::isInitialized()` 守门（u7-l4 展开），所以多个种子同时启动也只装配一次 DHT——这是"进程级对象"的第二个例子。
- [src/RequestGroup.cc:L399-L407](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L399-L407)：magnet 却没开 DHT 时打 NOTICE 强烈建议开启；随后 magnet 任务直接走 `BtCheckIntegrityEntry::onDownloadIncomplete` 提前返回（没有文件校验/预分配可做）。

**第三段：控制文件处置与汇入校验停车场**：

- [src/RequestGroup.cc:L409-L460](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L409-L460)：文件大小与总长一致则只读模式打开（支持只读介质做种）；`.aria2` 存在则 `load()` 断点恢复；目标文件已存在按 `--check-integrity`/`--allow-overwrite`/`--bt-seed-unverified` 处置（都不满足则报 FILE_ALREADY_EXISTS）；最后 `processCheckIntegrityEntry(BtCheckIntegrityEntry)` 把任务送进校验停车场（u5-l5 的单槽串行化）。
- [src/BtCheckIntegrityEntry.cc:L56-L95](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtCheckIntegrityEntry.cc#L56-L95)：校验完成后的两个出口——`onDownloadIncomplete`（还有缺块 → `proceedFileAllocation` 造 `BtFileAllocationEntry`）与 `onDownloadFinished`（文件本就完整；`--bt-hash-check-seed=true` 时同样进预分配阶段以便做种，还会触发 `--on-bt-download-complete` 钩子）。两条路都汇入 `BtFileAllocationEntry`。

**第四段：`BtSetup::setup` 本体**（预分配停车场的出口）：

- [src/BtFileAllocationEntry.cc:L60-L93](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtFileAllocationEntry.cc#L60-L93)：`prepareForNextAction` 第一行就是 `BtSetup().setup(...)`（L69），之后才处理 mmap、补 HTTP 源命令、首次保存控制文件等——这解释了本讲时序图中"② 为什么在停车场之后"。
- [src/BtSetup.cc:L93-L108](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtSetup.cc#L93-L108)：入口——无 `CTX_ATTR_BT` 属性直接返回（HTTP/FTP 任务不会走到装配）；按 `metadata.empty()` 判定 magnet 模式；**从注册表 `get(gid)` 取回六件套引用**（此刻它们早已存在），后续命令统一从这里拿引用。
- [src/BtSetup.cc:L110-L135](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtSetup.cc#L110-L135)：三个种子级周期命令——`TrackerWatcherCommand`（守护 HTTP announce，u6-l7）、`PeerChokeCommand`（每轮自排队，节拍由 `PeerStorage::chokeRoundIntervalElapsed` 判定，到点执行 choke/unchoke 算法；magnet 模式跳过，u6-l4）、`ActivePeerConnectionCommand`（维持连接数：主动去 PeerStorage 领新 peer 发起连接，magnet 模式 2 秒一拍、普通 10 秒一拍，u6-l4）。
- [src/BtSetup.cc:L137-L156](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtSetup.cc#L137-L156)：DHT 取 peer——非私有种子且 DHT 已初始化时，为 IPv4/IPv6 各建一个 `DHTGetPeersCommand`（u7-l5），taskQueue/taskFactory 从进程级的 `DHTRegistry` 领取（又一次"全局对象"引用）。
- [src/BtSetup.cc:L157-L183](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtSetup.cc#L157-L183)：做种退出判定——`UnionSeedCriteria` 组合 `--seed-time`（注意乘 60：选项单位是分钟）的 `TimeSeedCriteria` 与 `--seed-ratio` 的 `ShareRatioSeedCriteria`，非空则造 `SeedCheckCommand` 周期评估（u6-l10）。
- [src/BtSetup.cc:L184-L215](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtSetup.cc#L184-L215)：**进程级一次性**的监听端口绑定——`btReg->getTcpPort() == 0` 才做（第二个种子直接复用）；先试 IPv4 再试 IPv6（`--disable-ipv6` 时跳过 v6），IPv4 成功后 IPv6 必须绑**同一端口**（用 `SegList` 限定 `usedPort`）；端口候选来自 `--listen-port`；两个族都失败则抛 `DL_ABORT_EX("Errors occurred while binding port.")`。成功后 `btAnnounce->setTcpPort()` 把端口告诉 announce（tracker 要把这个端口宣告给其他 peer）。
- [src/BtSetup.cc:L217-L276](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtSetup.cc#L217-L276)：**进程级一次性**的 LPD（本地 peer 发现）——`--bt-enable-lpd` 且非私有种子时：接收器不存在才 `init`（多播组地址+端口，可指定 `--bt-lpd-interface`），成功则存进注册表全局槽位并挂 `LpdReceiveMessageCommand`；随后为本种子创建 `LpdMessageDispatcher` 与 `LpdDispatchMessageCommand`（每种子一个派发器，接收器全局共享）。初始化过程全程 INFO 日志可观察。
- [src/BtSetup.cc:L277-L285](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtSetup.cc#L277-L285)：`--bt-stop-time > 0` 时挂 `BtStopDownloadCommand`（下载停滞超时整体停机）；最后一行 `btRuntime->setReady(true)`——放行闸门落下，被动连接从此可被接纳。

**对照组：peer 级对象不入注册表**：

- [src/PeerInteractionCommand.cc:L177-L231](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L177-L231)：每条 peer 连接现场创建 `DefaultBtMessageDispatcher`/`DefaultBtMessageReceiver`/`DefaultBtRequestFactory`/`DefaultBtInteractive` 等（u6-l4/u6-l5 的主角），它们只活一条连接的寿命，由命令自己持有，注册表对此一无所知。区分"什么该共享、什么该独占"正是本讲想传递的设计感。

#### 4.2.4 代码实践：装配清单（本讲核心实践）

**实践目标**：通读 `RequestGroup::createInitialCommand` 与 `BtSetup::setup`，产出一张「对象 → 创建位置 → 后续哪讲使用」对照表，把 BT 单元的学习地图提前铺好。

**操作步骤**：

1. 打开 [src/RequestGroup.cc:L271-L462](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L271-L462) 与 [src/BtSetup.cc:L93-L286](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtSetup.cc#L93-L286)，逐行找出所有 `make_unique`/`make_shared`/`new`。
2. 为每个对象记录：类名、创建行号、进注册表与否、生命周期层级（进程级/种子级/peer 级）。
3. 用下表核对（参考答案，共 15 项）：

| # | 对象 | 创建位置 | 层级 | 后续哪讲使用 |
|---|------|----------|------|--------------|
| 1 | `DefaultPieceStorage` / `UnknownLengthPieceStorage` | RequestGroup.cc L298/L307（经 `initPieceStorage`） | 种子级 | u5-l2（piece 记账）、u6-l9（magnet 的未知长度） |
| 2 | `DefaultBtProgressInfoFile` | RequestGroup.cc L315 | 种子级 | u6-l11（.aria2 断点恢复） |
| 3 | `BtRuntime` | RequestGroup.cc L319 | 种子级 | u6-l3/u6-l4（maxPeers 判定、ready 闸门） |
| 4 | `DefaultPeerStorage` | RequestGroup.cc L326 | 种子级 | u6-l3（peer 池）、u6-l4（choke 候选） |
| 5 | `DefaultBtAnnounce` | RequestGroup.cc L334 | 种子级 | u6-l7（HTTP/UDP tracker announce） |
| 6 | `BtObject`（打包上述+Context） | RequestGroup.cc L346 | 种子级 | 本讲（注册表条目） |
| 7 | DHT 全家（经 `DHTSetup`） | RequestGroup.cc L356/L367 | **进程级** | u7-l1~u7-l5 |
| 8 | `DHTEntryPointNameResolveCommand` | RequestGroup.cc L377/L388 | 种子级触发、进程级效果 | u7-l4（DHT 自举） |
| 9 | `TrackerWatcherCommand` | BtSetup.cc L111 | 种子级命令 | u6-l7 |
| 10 | `PeerChokeCommand` | BtSetup.cc L120 | 种子级命令 | u6-l4（choke 算法） |
| 11 | `ActivePeerConnectionCommand` | BtSetup.cc L127 | 种子级命令 | u6-l4（补充连接） |
| 12 | `DHTGetPeersCommand`（v4/v6 最多两个） | BtSetup.cc L140/L149 | 种子级命令 | u7-l5 |
| 13 | `SeedCheckCommand` + `UnionSeedCriteria` | BtSetup.cc L158-L181 | 种子级命令 | u6-l10（做种退出） |
| 14 | `PeerListenCommand` + 监听端口 | BtSetup.cc L190 | **进程级**（端口）/命令常驻 | u6-l3（被动握手入口） |
| 15 | `LpdMessageReceiver`（全局）/`LpdMessageDispatcher`（每种子）/`BtStopDownloadCommand` | BtSetup.cc L221/L261/L279 | 混合 | LPD 散见 u6；stop-time 见文档 |

4. 补一个观察题：表中哪些对象在 magnet 模式下**不会**被创建？（答案：#2、#10、#13；magnet 只要元数据，不做种也不 choke。）

**需要观察的现象**：纯源码阅读，无需运行；重点体会"同一行代码里混着三种生命周期"（如 BtSetup 里 TrackerWatcherCommand 是种子级、PeerListenCommand 绑的端口是进程级）。

**预期结果**：一张 15 行的对照表，能回答"XX 对象在哪创建、归谁管、哪讲讲它"。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `BtSetup::setup` 不自己创建 PieceStorage 等六件套，而要提前在 `createInitialCommand` 里创建并注册？

答案：六件套在命令装配之前就有人要用——校验阶段（`BtCheckIntegrityEntry` 校验的就是 piece 哈希）、预分配阶段（`BtFileAllocationEntry` 要对 pieceStorage 的 DiskAdaptor 预分配）、以及 `DefaultBtProgressInfoFile` 的加载恢复都发生在停车场里，早于 `BtSetup`。提前创建并放进注册表，才能让"数据先行、命令后到"的流水线各阶段共享同一套对象。

**练习 2**：`btRuntime->setReady(true)` 如果被去掉，最直接的症状是什么？

答案：被动连接全部被拒。[PeerReceiveHandshakeCommand.cc:L115-L118](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerReceiveHandshakeCommand.cc#L115-L118) 在接纳连入 peer 前检查 `btRuntime->ready()`，未就绪一律抛 "Unknown info hash"——主动连接（tracker 拿到的 peer 由 `ActivePeerConnectionCommand` 发起）不受影响，表现为"只能连出、不能连入"。

**练习 3**：两个 BT 任务先后启动，第二个任务执行到 `BtSetup` 时还会再绑一次监听端口吗？

答案：不会。[BtSetup.cc:L184](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtSetup.cc#L184) 以 `btReg->getTcpPort() == 0` 为闸门，端口是注册表上的进程级状态，首个任务绑定后后续任务直接复用，只把端口号 `setTcpPort` 给自己的 announce。

### 4.3 bittorrent_helper 的进程级全局状态：peer id 与"三级边界"

#### 4.3.1 概念说明

`bittorrent_helper` 命名空间里藏着一小块**进程级可变状态**：匿名命名空间中的两个静态字符串 `peerId` 与 `peerAgent`（[src/bittorrent_helper.cc:L697-L700](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L697-L700)）。

- **peer id**：BT 协议规定每方在握手中出示 20 字节 id。aria2 的策略是**整个进程共用一个**：前缀默认 `aria2-`（[src/bittorrent_helper.cc:L90-L91](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L90-L91)），可由 `--peer-id-prefix` 覆盖，其余字节随机补齐。
- **peer agent**：扩展握手里报告的客户端描述串，默认 `aria2/<版本>`，可用 `--peer-agent` 覆盖。

它们与 `BtRegistry` 的四个全局槽位、`DHTRegistry` 的全局组件一起，构成理解 BT 子系统对象模型的**三级边界**：

| 层级 | 例子 | 创建时机 | 存放位置 |
|------|------|----------|----------|
| 进程级 | peer id/agent、BT 监听端口、UDP 端口、LPD 接收器、UDP Tracker 客户端、DHT 全家 | `Context` 构造 / 首个 BT 任务装配时，一次性 | 静态变量 / `BtRegistry` / `DHTRegistry` |
| 种子级 | `BtObject` 六件套 + `BtSetup` 的周期命令 | 任务激活时 | `BtRegistry::pool_`（按 GID） |
| peer 级 | `DefaultBtInteractive`、消息收发三件套、`PeerConnection` | 每条连接建立时 | `PeerInteractionCommand` 自持 |

#### 4.3.2 核心流程

```
进程启动
 └─ Context 构造函数（在选项解析之后、日志配置之前）
     ├─ generateStaticPeerId(--peer-id-prefix)   ← 仅当静态串为空才生成（幂等）
     └─ generateStaticPeerAgent(--peer-agent)
下载期间（所有种子、所有 peer 连接共享同一 id）
 ├─ DefaultBtAnnounce 构造 announce URL：peer_id=<percentEncode(静态id)>
 ├─ DefaultBtInteractive 发起/回应握手：写入静态 id；
 |    收到对方 id 与自己相同 ⇒ 判定"连到自己"，断开
 └─ 扩展握手 client version 报告静态 agent
```

注意生成时机的细节：`Context` 构造里 peer id 的生成（L168-169）排在 `LogFactory` 配置（L171 起）**之前**——它属于"极早期初始化"，连日志系统都还没就绪。

#### 4.3.3 源码精读

- [src/bittorrent_helper.cc:L682-L695](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L682-L695)：`generatePeerId`——前缀 + 随机字节补到 20，超过 20 截断。
- [src/bittorrent_helper.cc:L702-L716](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L702-L716)：`generateStaticPeerId/Agent`——"若静态串为空才生成"，天然幂等；配合 `setStaticPeerId/Agent`（L718-L722）供 libaria2 嵌入方注入自定义 id。
- [src/bittorrent_helper.cc:L726-L745](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L726-L745)：`getStaticPeerId/Agent`——懒生成兜底：即使没人显式生成过，首次取用时用默认前缀补造，保证任何路径取到的都是非空 id。
- [src/Context.cc:L167-L170](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L167-L170)：初始化时机——`Context` 构造函数中、`ENABLE_BITTORRENT` 编译开关内，选项解析完成后立刻执行。
- [src/DefaultBtAnnounce.cc:L166-L181](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtAnnounce.cc#L166-L181)：消费方①——announce URL 的 `peer_id=` 参数直接 percent-encode 静态 id；tracker 由此把"你"告知其他 peer。
- [src/DefaultBtInteractive.cc:L118-L129](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L118-L129)：消费方②——握手消息写入静态 id；收到侧若对方 id 与自己完全相同则判定自连并拒绝（这也解释了为何进程内共享一个 id 不会造成歧义：不同 aria2 进程的随机后缀几乎不可能撞车）。
- 对照全局槽位的托管方式：UDP Tracker 客户端由 `DHTSetup` 创建后存进 `BtRegistry::setUDPTrackerClient`（[src/BtRegistry.cc:L102-L106](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtRegistry.cc#L102-L106)），随后所有种子的 `TrackerWatcherCommand`（[src/TrackerWatcherCommand.cc:L193](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TrackerWatcherCommand.cc#L193)）共享这一个客户端收发 UDP tracker 报文——"全局一份、注册表托管、按需领取"的又一实例。

#### 4.3.4 代码实践：三级边界分类（源码阅读型）

**实践目标**：把 BT 子系统的对象按"进程级/种子级/peer 级"归类，建立后续 u6-l3~u6-l11、u7 各讲阅读时的"归属感"。

**操作步骤**：

1. 用 grep 在 `src/` 下搜索三类引用并逐一归类：
   - `getStaticPeerId()` / `getStaticPeerAgent()`（进程级静态状态的所有消费方）；
   - `getBtRegistry()->get(`（按 GID 取种子级对象的消费方）；
   - `DHTRegistry::getData()`（进程级 DHT 组件的领取点）。
2. 对每个命中点记下：文件、行号、它拿到对象后做什么、对象创建于哪一行（反向追溯）。
3. 核对参考答案：`getStaticPeerId` 的消费方应至少含 `DefaultBtAnnounce.cc`（announce URL）、`DefaultBtInteractive.cc`（握手与自连判定）、`DefaultBtMessageReceiver.cc`（接收握手）三处；`getBtRegistry()->get(` 的消费方应含 `BtSetup.cc`、`ConsoleStatCalc.cc`、`RpcMethodImpl.cc`、`PeerReceiveHandshakeCommand.cc` 等。

**需要观察的现象**：同一份静态 id 被 announce、握手、接收三条互不相识的代码路径引用，而它们无需任何参数传递就能拿到——这就是"进程级全局状态"的形态与代价（隐式耦合）。

**预期结果**：一张三级分类表。可选本地验证（需可用 tracker/对端，**待本地验证**）：运行 `./src/aria2c --peer-id-prefix=-AB1234- <种子>`，用抓包工具观察 announce 请求中的 `peer_id=` 前缀是否为 `-AB1234-`。

#### 4.3.5 小练习与答案

**练习 1**：两个 aria2 进程在同一台机器上同时下载同一个种子，它们的 peer id 会相同吗？

答案：不会。peer id 是**进程级**静态变量，每个进程独立生成；默认前缀 `aria2-` 相同，但剩余 14 字节是随机数，碰撞概率约 \( 2^{-112} \)，可忽略。`DefaultBtInteractive` 的自连检测正是依赖"同进程内 id 唯一"这一性质。

**练习 2**：`getStaticPeerId` 为什么要做成"懒生成"而不是在 `main()` 一开始就生成？

答案：`bittorrent_helper` 同时被命令行与 libaria2 库两种形态复用（u1-l3 的 standalone 概念）。库形态没有 `main`，由宿主调用 `Context`/`aria2::libraryInit`；懒生成保证任何调用路径、任何时机取用都能拿到合法 id，同时 `setStaticPeerId` 又给宿主留了注入自定义 id 的口子——生成逻辑对初始化顺序不敏感。

**练习 3**：UDP Tracker 客户端为什么放在 `BtRegistry` 而不像 peer id 一样做成 `bittorrent_helper` 的静态变量？

答案：两者都是进程级单件，差别在**类型与生命周期管理**。peer id 是裸字符串，无需析构协调；UDP Tracker 客户端是带 socket 的对象，需要明确的所有者负责销毁与引擎生命周期对齐——挂在使用 `shared_ptr` 的注册表槽位上，所有权和释放顺序都清晰。这也体现了 aria2 "简单数据做静态、资源对象进注册表"的惯用法。

## 5. 综合实践：一次种子下载的装配全程追踪

把本讲三个模块串起来，完成一份「装配档案」。材料：仓库自带种子 `test/single.torrent`（tracker 已失效不影响本实践——装配发生在任何网络活动之前）。

1. **跑起来**：
   ```bash
   ./src/aria2c -d /tmp/bt-lab --enable-dht=false --bt-enable-lpd=false \
       --log=/tmp/bt-lab/aria2.log --log-level=info test/single.torrent
   ```
2. **对照时序**：在日志里找到引擎启动横幅（`<<--- --- --- --->>` 等，由 `Context` 构造打出），结合本讲 4.2.2 的时序图，标注以下里程碑各自的"此刻注册表里有没有这个 GID"：选项解析完成（peer id 已生成）→ 任务激活（六件套已 put）→ 校验停车场 → 预分配停车场 → `BtSetup`（端口绑定、命令创建、setReady）→ tracker 失败。
3. **制造一次装配失败**：改用 `--listen-port=1` 重跑（非 root 无法绑定 1 号端口），对照 [BtSetup.cc:L184-L214](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtSetup.cc#L184-L214)，确认错误信息 `Errors occurred while binding port.` 出现在**校验之后**（证明 `BtSetup` 确实排在停车场后面），并观察此时进程是否还残留监听 socket（预期：两个地址族都绑定失败才抛错，应无残留）。
4. **交付**：装配清单表（4.2.4 的 15 项）+ 时序图（手绘即可）+ 三级边界表（4.3.4）各一份，并在时序图上用三种颜色区分进程级/种子级/peer 级对象的出生点。

预期结论一句话：**数据先注册（`createInitialCommand`），命令后装配（`BtSetup`），端口与 LPD 全局只来一次，`setReady` 之后才开门迎客。**若第 3 步的现象与预期不符（例如错误出现在校验前），请回头核对源码行号——那是本实践最重要的纠错时刻。

## 6. 本讲小结

- `BtRegistry` 是 `DownloadEngine` 拥有的注册表：`map<GID, unique_ptr<BtObject>>`，`BtObject` 打包种子级六件套（Context/PieceStorage/PeerStorage/BtAnnounce/BtRuntime/ProgressInfoFile）；另托管 TCP/UDP 端口、LPD 接收器、UDP Tracker 客户端四个进程级槽位。
- **数据与命令分工装配**：六件套在 `RequestGroup::createInitialCommand` 创建并 `put` 注册；`BtSetup::setup`（经校验→预分配两个停车场之后，由 `BtFileAllocationEntry::prepareForNextAction` 调用）只创建种子级周期命令、做一次性的端口绑定与 LPD 初始化，最后 `btRuntime->setReady(true)` 放行被动连接。
- 按 GID 查是 map 对数查找，按 infohash 查是线性扫描——被动握手、LPD、DHT get_peers 应答都靠后者"按内容找任务"；条目在 `releaseRuntimeResource` 中移除，之后同 infohash 连入被拒。
- magnet（元数据获取）模式裁剪装配：无进度文件、无 PeerChoke/SeedCheck，DHT 保留且补连接轮询加密到 2 秒。
- `bittorrent_helper` 的静态 peer id/peer agent 是进程级状态，在 `Context` 构造早期生成、全进程共享；与注册表槽位、DHTRegistry 一起划出「进程级 / 种子级 / peer 级」三层对象边界，peer 级对象（BtInteractive 等）不入注册表。

## 7. 下一步学习建议

本讲铺好了 BT 运行时的"骨架与地址簿"，下一讲 **u6-l3（Peer 连接与握手协议）** 将使用其中两件：`PeerListenCommand` 绑定的监听端口（被动连接入口）和 `DefaultPeerStorage`（peer 的接纳与淘汰）。之后 u6-l4（DefaultBtInteractive 交互循环）会看到 `ActivePeerConnectionCommand`/`PeerChokeCommand` 如何驱动每个 peer 级对象，u6-l7（Tracker 交互）展开 `TrackerWatcherCommand` 与 `DefaultBtAnnounce`，u7-l4（DHT 启动）则解释本讲一笔带过的 `DHTSetup` 全家。建议在进入 u6-l3 前，重跑综合实践第 2 步，确认你能不看讲义说出时序图上每个里程碑的注册表状态。
