# get_peers 与 peer announce 存储

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `DHTGetPeersCommand` 如何为一个种子周期性地发起 DHT peer 查找，以及它为什么要区分 15 分钟、5 分钟、1 分钟、5 秒四档触发间隔。
2. 说出 `DHTPeerAnnounceStorage` 的两层记账结构：外层按 infohash 聚合（有序集合），内层按 (IP, 端口) 对 peer 去重（带时间戳的向量），并理解 30 分钟淘汰窗口的作用。
3. 推导 `DHTTokenTracker` 的 token 公式，解释「双秘密 + 定期轮换」如何既防伪造宣告、又允许轮换期间旧 token 继续有效。
4. 走通一条端到端闭环：本机发出 `get_peers` 拿到 token 与 peer 列表 → 收敛后向最近节点发 `announce_peer` → 对方校验 token 并把本机记入它的 announce 表 → 之后第三方查询同一 infohash 时，本机地址出现在应答的 `values` 字段里。

本讲是 DHT 单元（单元 7）的第 5 篇，承接 u7-l3 的任务系统（`DHTTaskQueue`、迭代查找）与 u7-l2 的报文体系（bencode、transaction id 配对），把视角从「路由表如何自举」转到「下载中的种子如何借助 DHT 找到 peer、又如何被别的节点找到」。

## 2. 前置知识

- **infohash**：种子文件中 info 字典的 SHA-1 摘要（20 字节），在 DHT 与 BT 中都充当「内容指纹」。DHT 里所有与某个种子相关的查询都以 infohash 为目标 ID。
- **get_peers / announce_peer（BEP 5）**：DHT 的两类核心 RPC。`get_peers` 问某节点「你认识的 peer 里谁有这个 infohash 的数据？应答里同时给我离这个 infohash 最近的 8 个节点」；`announce_peer` 则是向一个节点声明「我自己就在下载/提供这个 infohash，把我的地址记下来」。
- **token（令牌）**：announce_peer 报文必须携带的凭证。你想让节点 B 记录你的宣告，就必须先向 B 发一次 get_peers，B 会在应答里发给你一个与「你的地址 + 这个 infohash」绑定的 token。B 收到 announce_peer 时重算一遍并比对，防止第三者伪造「别人拥有某种子」的假情报污染 DHT。
- **compact 地址格式**：peer 地址在 DHT 报文里以「4 字节 IP + 2 字节端口」（IPv4，6 字节）或「16 字节 IP + 2 字节端口」（IPv6，18 字节）的网络字节序紧凑形式传输，不使用字符串。
- **PeerStorage（BT 侧）**：u6-l3/u6-l4 讲过的 BT peer 管理器（候选池 + 在用集）。DHT 查到的 peer 最终要喂给它，BT 的连接命令才会去连接。注意它与本讲的 `DHTPeerAnnounceStorage`（DHT 侧、服务别人）是两个不同的存储。
- **TimeBasedCommand 与 routineCommands_**：u7-l4 讲过，DHT 的周期命令默认 INACTIVE、每秒被事件循环唤醒一次，到达自身 interval 才执行 process()。
- **两套 DHT**：aria2 的 IPv4 与 IPv6 DHT 完全独立（u7-l1 的 `DHTRegistry` / `DHTRegistry6`），本讲所有组件每个地址族各有一份。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/DHTGetPeersCommand.cc/.h` | 需求侧：每个 BT 种子任务一个，按四档间隔发起 `DHTPeerLookupTask` |
| `src/DHTPeerLookupTask.cc` | 迭代查找任务（骨架见 u7-l3）；本讲关注它捕获 token、收纳 peer、收尾发 announce_peer |
| `src/DHTPeerAnnounceStorage.cc/.h` | 供给侧：`infohash → peer 地址表` 的进程级记账与淘汰 |
| `src/DHTPeerAnnounceEntry.cc/.h` | 单个 infohash 的条目：peer 地址去重与时间戳 |
| `src/PeerAddrEntry.cc/.h` | 最内层记录：IP + 端口 + 最后更新时间，`operator==` 只看地址 |
| `src/DHTTokenTracker.cc/.h` | token 的生成（SHA-1）、双秘密轮换与校验 |
| `src/DHTGetPeersMessage.cc` | 服务端收到 `get_peers` 请求：生成 token、查 announce 表、补 nodes、组应答 |
| `src/DHTGetPeersReplyMessage.cc` | `get_peers` 应答的 bencode 编码，含 `values` 上限 25 的推导 |
| `src/DHTAnnouncePeerMessage.cc` | 服务端收到 `announce_peer`：`validate()` 验 token，`doReceivedAction()` 入库 |
| `src/DHTPeerAnnounceCommand.cc` | 周期命令：每 5 分钟调用 `handleTimeout()` 清理过期宣告 |
| `src/DHTSetup.cc` | DHT 装配点：创建 `DHTPeerAnnounceStorage`、`DHTTokenTracker` 与两条周期命令 |
| `src/BtSetup.cc` | BT 装配点：为种子任务创建 `DHTGetPeersCommand`（私有种子除外） |
| `src/DHTConstants.h` | 本讲涉及的全部时间窗口常量 |
| `src/DHTMessageReceiver.cc` | 入站报文入口：`validate()` 抛异常即按未知报文丢弃 |
| `src/DHTTaskFactoryImpl.cc` | 任务工厂（含一个返回 `nullptr` 的未实现占位，见 4.2） |

## 4. 核心概念与源码讲解

### 4.1 DHTGetPeersCommand：为种子周期性发起 peer 查找

#### 4.1.1 概念说明

tracker 是 BT 世界里的「中央花名册」，但 DHT 的意义就在于不依赖它。当一个种子任务启动时，aria2 会为它挂一个常驻命令 `DHTGetPeersCommand`，职责只有一件事：**观察当前 peer 是否够用，不够就把一个 `DHTPeerLookupTask` 丢进任务队列，让 DHT 去网上搜罗新 peer**。

它不亲自收发任何 UDP 报文——报文收发属于 u7-l3 的任务系统（`DHTTaskQueue` 的 periodic2 泳道、`DHTMessageTracker` 配对）。它是「需求方」，任务系统是「执行方」。这个职责分离与 u6-l7 的 `TrackerWatcherCommand`（管调度）与 `DefaultBtAnnounce`（管决策）的分工如出一辙。

#### 4.1.2 核心流程

`execute()` 每轮做三件事（判断 → 收割 → 自循环）：

```text
若 BtRuntime 已请求停机 → 返回 true（销毁自己）
若当前没有进行中的查找任务 task_:
    elapsed = 距上次查找完成的时间
    满足以下任一条件即发起一轮新的 DHTPeerLookupTask:
      ① 常规节奏:      elapsed ≥ 15 分钟
      ② peer 少于下限:  已处重试态且 elapsed ≥ 5 秒，或 elapsed ≥ 5 分钟
      ③ 连接数为 0:     elapsed ≥ 1 分钟
若 task_ 存在且已结束:
    lastGetPeerTime_ = now
    若 peer 总数仍低于 --bt-max-peers 且重试次数 < 10:
        numRetry_++        （下一轮将以 5 秒间隔再次触发，见条件②）
    否则:
        numRetry_ = 0      （退出重试态，回到常规节奏）
    释放 task_
把自己重新排入命令队列（常驻自循环）
```

四档间隔对应四种饥渴程度：

| 间隔 | 常量 | 触发条件 |
| --- | --- | --- |
| 15 分钟 | `GET_PEER_INTERVAL` | 常规保活，哪怕 peer 够用也定期刷新 |
| 5 分钟 | `GET_PEER_INTERVAL_LOW` | peer 数低于 `BtRuntime` 下限（`lessThanMinPeers`） |
| 1 分钟 | `GET_PEER_INTERVAL_ZERO` | 当前 BT 连接数为 0，完全断粮，最激进的自救 |
| 5 秒 | `GET_PEER_INTERVAL_RETRY` | 重试态：上一轮查完了但 peer 还是不够 |

#### 4.1.3 源码精读

先看四档间隔与重试上限的定义：

[DHTGetPeersCommand.cc:L56-L68](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTGetPeersCommand.cc#L56-L68)

匿名命名空间里定义了 4 个 `constexpr` 时间常量与 `MAX_RETRIES = 10`。注意注释「Try more than 5 to drop bad node」——多次重试的另一个副作用是让连续查不到 peer 的坏节点在 `DHTMessageTracker` 超时判定下被标坏、移出路由表（u7-l3 讲过的健康度反哺）。

节流判断是本命令最核心的一段：

[DHTGetPeersCommand.cc:L88-L108](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTGetPeersCommand.cc#L88-L108)

`execute()` 开头先查 `btRuntime_->isHalt()` 决定是否退场；随后那个嵌套的长条件表达式就是 4.1.2 伪代码中条件①②③的直接翻译：`elapsed >= GET_PEER_INTERVAL` 是常规节奏，`lessThanMinPeers()` 分支里「`numRetry_` 非零则 5 秒、否则 5 分钟」实现了重试态的快速节奏，`getConnections() == 0` 则是 1 分钟的断仓自救。满足条件时调用 `taskFactory_->createPeerLookupTask(...)` 创建任务并 `addPeriodicTask2` 送进 periodic2 泳道（下载业务泳道，u7-l3 讲过它与其他泳道分开限流）。

任务收割与重试计数：

[DHTGetPeersCommand.cc:L109-L129](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTGetPeersCommand.cc#L109-L129)

任务结束后刷新 `lastGetPeerTime_`；若 peer 数（`peerStorage_->countAllPeer()`）仍低于 `--bt-max-peers` 且重试未超上限就 `++numRetry_`，否则清零。最后 `e_->addCommand(std::unique_ptr<Command>(this)); return false;` 是 u2-l3 讲过的「自我重排」——命令永不销毁，直到 `BtRuntime` 停机。构造/析构中的 `increaseNumCommand()`/`decreaseNumCommand()`（[DHTGetPeersCommand.cc:L70-L86](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTGetPeersCommand.cc#L70-L86)）维持 RequestGroup 的存活计数，这与 u6-l10 的 `SeedCheckCommand` 是同一手法。

谁创建这个命令？在 BT 装配阶段：

[BtSetup.cc:L137-L156](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtSetup.cc#L137-L156)

条件 `metadataGetMode || !torrentAttrs->privateTorrent` 说明 magnet 元数据任务也走 DHT（它必须靠 DHT 找 peer 换元数据），而**私有种子（`private: 1`）不创建此命令**——私有种子禁止 DHT/PEX 是协议要求。IPv4 与 IPv6 各建一个实例，分别接各自的 `DHTRegistry::getData()` / `getData6()` 任务队列。

`DHTPeerLookupTask` 的迭代查找骨架已在 u7-l3 讲过，这里补上它与本讲主题直接相关的两段。收到 `get_peers` 应答时：

[DHTPeerLookupTask.cc:L74-L83](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPeerLookupTask.cc#L74-L83)

两件事：把应答方的 token 以「应答方节点 ID 的十六进制」为键存入 `tokenStorage_`（供收尾宣告用）；把应答 `values` 里的 peer 交给 `peerStorage_->addPeer`，直接喂给 BT 连接机制。查找收敛时（`onFinish`）：

[DHTPeerLookupTask.cc:L96-L121](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPeerLookupTask.cc#L96-L121)

遍历查找过程中实际联系过（`used` 标志）的最近 8 个节点，从 `tokenStorage_` 取出各自的 token，构造 `announce_peer` 报文入队——没有 token 的节点跳过（日志「Token is empty」）。这段代码揭示了 token 的本质：**它是「每个节点 × 每个 infohash × 每个请求方地址」三元组各自的凭证，不能跨节点通用**，所以必须按节点 ID 存一张表。

#### 4.1.4 代码实践

**实践目标**：用日志验证四档触发间隔的存在，观察「peer 不足 → 5 秒重试」的行为。

**操作步骤**（源码阅读 + 本地运行型实践）：

1. 按 u1-l2 的方法编译 aria2（`autoreconf -i && ./configure && make -j$(nproc)`）。
2. 在 [DHTGetPeersCommand.cc:L100](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTGetPeersCommand.cc#L100) 的 `A2_LOG_DEBUG` 处与 [DHTGetPeersCommand.cc:L117](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTGetPeersCommand.cc#L117) 的重试日志处，把 `A2_LOG_DEBUG` 提升为 `A2_LOG_INFO`（或直接保持不动，用 `--log-level=debug`）。重新 `make`。
3. 运行：`./src/aria2c --enable-dht --enable-dht6=false -d /tmp/dht-test "<某个种子文件或magnet>" --log=/tmp/dht.log --log-level=debug`。
4. 在日志中检索 `Issuing PeerLookup` 与 `Too few peers` 两类行，抄录它们的时间戳。

**需要观察的现象**：

- 首次查找后若 peer 不够，`Too few peers ... Try again(N)` 应以约 5 秒间隔连续出现；
- `Try again` 的编号从 1 递增，接近 10 之后回到 0（重试态退出）；
- 稳定期（peer 充足）后，`Issuing PeerLookup` 的间隔拉长到分钟级。

**预期结果**：时间戳差值序列大致呈现「密集（5 秒级）→ 递增编号至 10 → 稀疏（分钟级）」的形态。若网络环境无法提供足够 peer（如内网沙箱），可能长期停留在重试态，这也是有效观察。具体数值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么「连接数为 0」时的间隔（1 分钟）要比「peer 略少」（5 分钟）激进得多？

**答案**：peer 略少只是效率问题，还有存量连接在传数据；连接数为 0 意味着下载完全停滞，此时尽快补充 peer 是唯一的自救手段。这与 u6-l4 讲过的 `ActivePeerConnectionCommand` 在零连接时主动缩短 tracker announce 间隔是同一种「断粮自救」设计。

**练习 2**：`numRetry_` 达到 `MAX_RETRIES`（10）之后会发生什么？

**答案**：见 [DHTGetPeersCommand.cc:L112-L124](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTGetPeersCommand.cc#L112-L124)：条件 `numRetry_ < MAX_RETRIES` 不再满足，走 `else` 分支被重置为 0，退出重试态，之后回到条件②的「5 分钟」节奏而非「5 秒」。连续 10 轮查找仍凑不够 peer，说明该 infohash 在网络里确实冷门，继续 5 秒轰炸只会浪费自己的带宽与路由表健康度。

**练习 3**：`DHTGetPeersCommand` 为什么要在构造函数里 `increaseNumCommand()`？

**答案**：u3-l1/u6-l10 讲过，RequestGroup 以 `numCommand` 归零为结束判据。这个命令与任务共存亡（直到 BT 做种结束、`BtRuntime::isHalt()` 为真才销毁），构造时 +1、析构时 -1 可以防止任务因「看起来没有命令了」而被提前收割。

### 4.2 DHTPeerAnnounceStorage 与 DHTPeerAnnounceEntry：infohash → peer 地址表的记账

#### 4.2.1 概念说明

当本机作为 DHT 网络的一个普通节点运行时，它也在**服务别人**：别的下载者会向本机发 `get_peers` 和 `announce_peer`。`DHTPeerAnnounceStorage` 就是本机替整个网络保管的一本账，回答的问题是：

> 「哪些 infohash 被哪些地址宣告过『我有这个种子』？」

注意它与 BT 自己的 `PeerStorage` 方向相反：`PeerStorage` 记「我要连的 peer」，`DHTPeerAnnounceStorage` 记「别人托我转告第三方的 peer」。这本账有两个与生俱来的特点：

1. **它是近似的、可过期的**。peer 随时可能退出网络，账本用时间戳让过期记录自动腐化，从不持久化（u7-l1 讲过 `dht.dat` 只存本地节点 ID 与好节点，不存这本账）。
2. **它是被动的**。只有别人向本机发 `announce_peer` 时才会记账；本机从不主动去「采集」。

结构上它是两层的：

```text
DHTPeerAnnounceStorage
  └─ entries_ : std::set<shared_ptr<DHTPeerAnnounceEntry>, InfoHashLess>
        按 infohash 的 20 字节 memcmp 字典序排序
        └─ DHTPeerAnnounceEntry
             ├─ infoHash_[20]
             ├─ peerAddrEntries_ : vector<PeerAddrEntry>
             │     每个 PeerAddrEntry = { ip, port, lastUpdated_ }
             │     按 (ip, port) 判重，重复宣告只刷新时间戳
             └─ lastUpdated_（条目级时间戳）
```

#### 4.2.2 核心流程

**写入路径**（收到合法 announce_peer 时）：

```text
DHTAnnouncePeerMessage::doReceivedAction
  └─ storage->addPeerAnnounce(infoHash, 对端IP, 报文里的TCP端口)
       └─ getPeerAnnounceEntry(infoHash)
            用 lower_bound 在有序集合中二分查找
            ├─ 命中 → 返回既有条目
            └─ 未命中 → 原地插入新条目（带提示的 insert）
       └─ entry->addPeerAddrEntry({ip, port})
            线性 find：operator== 只比较 (ip, port)
            ├─ 不存在 → push_back 新记录
            └─ 已存在 → 只 notifyUpdate() 刷新该记录时间戳
            最后无条件 notifyUpdate() 刷新条目级时间戳
```

**读取路径**（收到 get_peers 时）：

```text
DHTGetPeersMessage::doReceivedAction
  └─ storage->getPeers(peers, infoHash)
       entries_.find(infoHash) 命中 → 把每条 PeerAddrEntry
       转成 Peer 对象追加进出参向量（应答的 values 来源之一）
```

**清理路径**（每 5 分钟一次）：

```text
DHTPeerAnnounceCommand::process (每 DHT_PEER_ANNOUNCE_CHECK_INTERVAL = 5 分钟)
  └─ storage->handleTimeout()
       ├─ 每个条目 removeStalePeerAddrEntry(30 分钟)
       │    删除 lastUpdated_ 距今 ≥ 30 分钟的 peer 记录
       └─ 删除已变空的条目（erase-while-iterate 惯用法）
```

**时间窗口的语义**：一个 peer 想留在这本账上，必须至少每 30 分钟（`DHT_PEER_ANNOUNCE_PURGE_INTERVAL`）重新宣告一次。而下载侧 `DHTPeerLookupTask` 每轮查找结束都会向 8 个最近节点发 announce_peer，常规节奏 15 分钟一轮（见 4.1）——**宣告频率（约 15 分钟）高于淘汰门槛（30 分钟），正常节点不会掉出账本**；一旦真正下线，最多 30 分钟后它的记录自然消失。这是一种典型的「租约式」软状态（soft state）设计：不通知也能收敛，只是慢一点。

#### 4.2.3 源码精读

外层的排序准则与容器类型：

[DHTPeerAnnounceStorage.h:L54-L61](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPeerAnnounceStorage.h#L54-L61)

`InfoHashLess` 对两个条目的 infohash 做逐字节 `memcmp` 比较（实现在 [DHTPeerAnnounceStorage.cc:L60-L65](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPeerAnnounceStorage.cc#L60-L65)），于是 `entries_` 成为按 infohash 字典序排列的 `std::set`。

「找到或插入」一步完成：

[DHTPeerAnnounceStorage.cc:L67-L92](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPeerAnnounceStorage.cc#L67-L92)

`getPeerAnnounceEntry` 先造一个临时条目，`lower_bound` 定位后用 `memcmp` 确认真的是同一 infohash（20 字节全等）；命中则复用既有条目，未命中则 `entries_.insert(i, entry)` 以迭代器 `i` 为提示完成摊还 O(log n) 的插入。`addPeerAnnounce` 就是对它的薄封装，带一条 debug 日志（hex 化的 infohash + 对端地址）。

内层的去重与时间戳刷新：

[DHTPeerAnnounceEntry.cc:L52-L62](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPeerAnnounceEntry.cc#L52-L62)

配合 [PeerAddrEntry.cc:L62-L65](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerAddrEntry.cc#L62-L65) 的 `operator==`（**只比较 `ipaddr_` 与 `port_`，不含时间戳**），可以看到：同一 (IP, 端口) 反复宣告不会让列表膨胀，只会把 `lastUpdated_` 刷到当前时刻——这正是「去重」与「续租」合一的实现。条目自身的 `lastUpdated_` 也无条件刷新（[DHTPeerAnnounceEntry.cc:L91-L94](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPeerAnnounceEntry.cc#L91-L94) 的 `notifyUpdate`）。

读取路径（get_peers 命中本地账本的出口）：

[DHTPeerAnnounceStorage.cc:L101-L110](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPeerAnnounceStorage.cc#L101-L110)

`find` 命中后由 [DHTPeerAnnounceEntry.cc:L83-L89](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPeerAnnounceEntry.cc#L83-L89) 把每条记录物化成 `Peer` 对象（`std::make_shared<Peer>(ip, port)`）。注意与 `PeerStorage` 的 Peer 不同，这些对象只是「地址的信封」，没有任何会话状态。

清理路径：

[DHTPeerAnnounceStorage.cc:L112-L131](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPeerAnnounceStorage.cc#L112-L131)

`handleTimeout` 先对每个条目调用 `removeStalePeerAddrEntry`（其内部是 erase-remove 惯用法，见 [DHTPeerAnnounceEntry.cc:L69-L79](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPeerAnnounceEntry.cc#L69-L79)，谓词为 `lastUpdated_ 距今 ≥ 30 分钟`），随后用 `erase(i++)` 惯用法删掉变空的条目。它的调用者是每 5 分钟醒来一次的周期命令（[DHTPeerAnnounceCommand.cc:L63-L71](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPeerAnnounceCommand.cc#L63-L71)，装配于 [DHTSetup.cc:L235-L239](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L235-L239)，间隔常量 `DHT_PEER_ANNOUNCE_CHECK_INTERVAL` 定义于 [DHTConstants.h:L61-L67](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTConstants.h#L61-L67)）。

**必须如实指出的两处「未完成」**：

1. [DHTPeerAnnounceStorage.cc:L133-L147](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPeerAnnounceStorage.cc#L133-L147) 的 `announcePeer()` 意图是「对 15 分钟内未被本机重新宣告过的 infohash 补发一轮宣告任务」，但**全仓库没有任何调用者**，是保留的死代码；
2. 它依赖的 [DHTTaskFactoryImpl.cc:L100-L105](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTTaskFactoryImpl.cc#L100-L105) `createPeerAnnounceTask` 直接 `return nullptr`（源码中标注 `// TODO`）。

也就是说：**aria2 只被动记账与过期，不会主动替账上的 infohash 重新宣告**。读源码时不能想当然认为「有这个函数就有这个行为」。

#### 4.2.4 代码实践

**实践目标**：统计不同 infohash 收到的 announce 数量与 peer 去重效果，解释时间窗口的作用。（这是本讲指定的主实践，本小节先做观测，4.5 综合实践再做完整版。）

**操作步骤**：

1. 在 [DHTPeerAnnounceStorage.cc:L84-L92](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPeerAnnounceStorage.cc#L84-L92) 的 `addPeerAnnounce` 中，把已有的 `A2_LOG_DEBUG` 改为 `A2_LOG_INFO`，并追加一条「该 infohash 当前记录数」的输出。示例代码（非项目原有代码）：

   ```cpp
   // 示例代码：插在 getPeerAnnounceEntry(...) 调用之前
   A2_LOG_INFO(fmt("ANNOUNCE infoHash=%s from %s:%u, current entry peers=%lu",
                   util::toHex(infoHash, DHT_ID_LENGTH).c_str(),
                   ipaddr.c_str(), port,
                   static_cast<unsigned long>(
                       getPeerAnnounceEntry(infoHash)->countPeerAddrEntry())));
   ```

   （`countPeerAddrEntry` 声明于 [DHTPeerAnnounceEntry.h:L64-L68](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPeerAnnounceEntry.h#L64-L68)。为减少重复查找，可先取一次条目引用再分别打印。）
2. 重新 `make`，运行一个热门种子：`./src/aria2c --enable-dht --seed-time=0 "<种子>" --log=/tmp/ann.log --log-level=info`。
3. 用 `grep 'ANNOUNCE infoHash' /tmp/ann.log | awk '{print $2}' | sort | uniq -c | sort -rn` 统计各 infohash 的宣告次数。
4. 再对照 [DHTPeerAnnounceStorage.cc:L112-L131](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPeerAnnounceStorage.cc#L112-L131) 的两条 `Now purge ...` / `Currently %lu peer announce entries` 日志（同为 debug 级，可一并提为 info），观察每 5 分钟清理后条目数的衰减。

**需要观察的现象**：

- 同一 infohash 会反复出现宣告行，但 `current entry peers` 不随之线性增长——同一 (IP, port) 只刷新时间戳（去重生效）；
- 若下载持续超过 30 分钟，清理日志显示的条目数应大致稳定（新宣告续租、离线 peer 被剔除）；
- 热门种子的 infohash 排在 `uniq -c` 统计的首位。

**预期结果**：宣告次数 ≫ 去重后的 peer 记录数，比值即「续租率」。沙箱环境若无真实 BT 流量，`ANNOUNCE` 行可能很少甚至为零，此时可退回 `--log-level=debug` 观察 `Adding ... to peer announce list` 原生日志。具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么外层用按 infohash 排序的 `std::set`，而不是 `std::unordered_map`？

**答案**：本讲的用法里有三处依赖有序性：`getPeerAnnounceEntry` 的 `lower_bound` + 提示插入（[DHTPeerAnnounceStorage.cc:L72-L80](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPeerAnnounceStorage.cc#L72-L80)）、`contains` 的 `std::binary_search`（[DHTPeerAnnounceStorage.cc:L94-L99](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPeerAnnounceStorage.cc#L94-L99)）、以及 `handleTimeout`/`announcePeer` 的确定性遍历顺序。infohash 是 20 字节二进制，没有天然的哈希友好形态，而 C++03 时代的 aria2 用 `set + memcmp` 最为直白。

**练习 2**：`PeerAddrEntry::operator==` 为什么故意不比较 `lastUpdated_`？

**答案**：它服务于 `addPeerAddrEntry` 里的 `std::find`（[DHTPeerAnnounceEntry.cc:L54](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPeerAnnounceEntry.cc#L54)）：判等的目的是识别「同一个 peer」，地址相同即同一 peer；时间戳恰恰是要被更新的字段，若参与判等，重复宣告会被误判为新记录，账本将无限膨胀。

**练习 3**：一本账只增不减会发生什么？aria2 用哪两个机制避免？

**答案**：条目与内存无限增长，且离线 peer 的地址被不断发给查询方，污染 DHT。aria2 用两级机制：peer 级 30 分钟 `removeStalePeerAddrEntry` 淘汰 + 条目级「变空即删」，即 4.2.2 的租约式软状态；配合宣告方约 15 分钟一次的重宣告节奏，活跃 peer 永续租、离线 peer 自动出账。

### 4.3 DHTTokenTracker：token 的生成、双秘密窗口与校验

#### 4.3.1 概念说明

如果没有 token，任何人都可以向任意 DHT 节点发送伪造的 `announce_peer`，把不相干的 IP 塞进某个 infohash 的账本，实现「污染某个种子的 peer 列表」或「把流量引向受害者」的攻击。BEP 5 的对策是：

1. 每个节点自己持有一个**秘密**（secret），不对外披露；
2. 收到 `get_peers` 时，应答方用 `SHA-1(infohash || 请求方紧凑地址 || secret)` 生成 token 附在应答里发给请求方——**token 与「请求方的 IP + UDP 端口 + 该 infohash」三元组绑定**；
3. 之后收到 `announce_peer` 时，用报文声称的 infohash、报文真实的源地址、自己的 secret 重算一遍，与报文携带的 token 比对；不一致即丢弃。

攻击者没有 secret，就无法为「自己的地址 + 任意 infohash」预计算出合法 token；唯一取得 token 的途径是先老老实实发一次 `get_peers`——而这一来一回本身就限定了 token 只能用于宣告自己。即使窃听到别人 (IP, port) 的 token，也因为地址不匹配而无法从自己的地址使用。

还有一个工程细节：secret 若定期更换，轮换瞬间「旧 token 持有者」会被误杀；若永不更换，长期观测的攻击者可能积累大量 (地址, token) 对。aria2 的折中是**双秘密滑动窗口**：持有 `secret_[0]`（当前）与 `secret_[1]`（上一代），每 10 分钟把 `[0]` 降级到 `[1]`、再随机生成新的 `[0]`；校验时两代都接受。于是任何 token 的有效期落在 \(10 \sim 20\) 分钟之间，与 4.2 的宣告节奏（约 15 分钟一轮）匹配。

#### 4.3.2 核心流程

token 的数学定义：

\[
\text{token} = \mathrm{SHA\text{-}1}\big(\ \text{infohash}_{20} \ \|\ \text{compact}(ip, port) \ \|\ \text{secret}_{4}\ \big)
\]

即把 20 字节 infohash、紧凑地址（IPv4 6 字节 / IPv6 18 字节）、4 字节秘密顺序拼接后取 SHA-1，得到 20 字节（40 个十六进制字符）的 token。

三个动作：

```text
生成（收到 get_peers 请求时）:
  generateToken(infoHash, 请求方IP, 请求方UDP端口)        ← 用 secret_[0]

校验（收到 announce_peer 时）:
  validateToken(报文token, 报文infoHash, 源IP, 源UDP端口)
  对 secret_[0] 与 secret_[1] 各重算一次，任一相等 → 通过
  两个都不等 → 抛 DL_ABORT_EX → 上层按未知报文丢弃

轮换（DHTTokenUpdateCommand 每 10 分钟）:
  secret_[1] ← secret_[0]
  secret_[0] ← 新随机 4 字节
```

时间线示意（`T = 10` 分钟）：

```text
时刻      secret_[0]   secret_[1]   可通过校验的 token
0T        A            A            A
1T        B            A            A 或 B     ← 刚轮换，旧 token 仍有效
2T        C            B            B 或 C     ← A 代 token 全面失效
```

#### 4.3.3 源码精读

秘密的存储与初值：

[DHTTokenTracker.h:L43-L52](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTTokenTracker.h#L43-L52)

`SECRET_SIZE = 4`，`secret_[2][SECRET_SIZE]` 就是「当前 + 上一代」。两个构造函数（[DHTTokenTracker.cc:L49-L59](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTTokenTracker.cc#L49-L59)）都把两代初始化为同一个值：默认构造用随机数据，另一个用调用方给的初值（u7-l4 的 `DHTSetup` 在自举时可用它保持稳定）。

token 的生成：

[DHTTokenTracker.cc:L63-L88](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTTokenTracker.cc#L63-L88)

拼接缓冲区的大小是 `DHT_ID_LENGTH + COMPACT_LEN_IPV6 + SECRET_SIZE`（20 + 18 + 4 = 42 字节），先 `memset` 全零；紧凑地址写在偏移 20 处——IPv4 时 `packcompact` 只写前 6 字节，**其后 12 字节保持为零**；秘密固定写在偏移 38 处。这个「定长 IPv6 槽位 + 零填充」的选择意味着同一 (infohash, IPv4 地址, secret) 在两种地址族下生成的 token 也不同（IPv4 与 IPv6 的紧凑长度不同，且 IPv4 时不写满槽位）。随后调用 `message_digest::digest`（u5-l5 讲过的 MessageDigest 门面）取 SHA-1，以 `std::string` 原样承载 20 个二进制字节返回。地址无法打包（`packcompact` 返回 0，如非法 IP）则抛 `DL_ABORT_EX`。公有重载固定使用 `secret_[0]`。

校验与轮换：

[DHTTokenTracker.cc:L90-L107](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTTokenTracker.cc#L90-L107)

`validateToken` 对 `secret_` 数组逐个元素（恰好两代）调用私有版 `generateToken` 比对，任一命中即真。`updateTokenSecret` 就是 4.3.2 流程图的两行赋值：旧当前降为上一代，随机生成新当前。它的驱动者是 [DHTSetup.cc:L220-L225](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L220-L225) 创建的 `DHTTokenUpdateCommand`，周期为 [DHTConstants.h:L67](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTConstants.h#L67) 的 `DHT_TOKEN_UPDATE_INTERVAL = 10_min`（u7-l4 已介绍这类周期命令）。

校验失败如何变成「报文被丢弃」：

[DHTAnnouncePeerMessage.cc:L102-L111](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTAnnouncePeerMessage.cc#L102-L111)

`validate()` 用**报文真实的源地址**（`getRemoteNode()->getIPAddress()/getPort()`，这是 UDP 包的源 IP 与源 UDP 端口）重算 token 并比对 `token_`，失败抛 `DL_ABORT_EX`。这个异常的最终归宿在入站报文入口：

[DHTMessageReceiver.cc:L115-L129](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageReceiver.cc#L115-L129)

`onMessageReceived` 的调用被 `catch (RecoverableException&)` 包住，异常时走 `handleUnknownMessage`——报文被静默丢弃，`doReceivedAction()`（记账）、`markGood()`（路由表健康度回写）都不会执行。**校验失败不仅不入账，还会让对端错过一次「变好节点」的机会**。

最后一个精细区分（容易忽略）：**验 token 用 UDP 源端口，记账用报文里的 TCP 端口**。

[DHTAnnouncePeerMessage.cc:L77-L85](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTAnnouncePeerMessage.cc#L77-L85)

`doReceivedAction` 存入账本的是 `tcpPort_`——announce_peer 报文 `a` 字典里的 `port` 字段，即宣告方 BT 客户端监听的 **TCP** 端口（查询方将用它发起 BT 连接）；而 `validate()` 里的 `getRemoteNode()->getPort()` 是 DHT UDP 对话的源端口。两者数值通常不同，职责严格分离：token 绑定「对话地址」，账本记录「服务地址」。

#### 4.3.4 代码实践

**实践目标**：亲手复算一个 token，验证公式与「地址绑定」性质。

**操作步骤**（源码阅读 + 计算型实践，无需改源码）：

1. 开 debug 日志运行 aria2：`./src/aria2c --enable-dht "<种子>" --log=/tmp/tk.log --log-level=debug`。
2. 日志中检索 `Message received: get_peers`（入站请求，[DHTMessageReceiver.cc:L123](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageReceiver.cc#L123) 打印）与 `Invalid token`（[DHTAnnouncePeerMessage.cc:L107-L109](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTAnnouncePeerMessage.cc#L107-L109) 打印，出现即说明校验失败真实发生过）。若日志中有本机收到 announce_peer 的记录，摘出其中的 `token=<hex>`、`info_hash=<hex>`、来源 IP/端口。
3. 由于 secret 不落盘也不打日志，无法对线上 token 做完整复算——改为**验证绑定关系**：对照 [DHTTokenTracker.cc:L63-L81](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTTokenTracker.cc#L63-L81) 手工写出拼接布局：

   ```text
   偏移  0..19    infohash（20 字节）
   偏移 20..37    compact(ip, port)，IPv4 仅前 6 字节有效、其余为零
   偏移 38..41    secret（4 字节）
   → SHA-1 → 20 字节 token
   ```

   并用任意脚本语言对假想的 secret（如 4 个 `0x00`）复算一遍，确认算法理解无误。示例代码（非项目原有代码）：

   ```python
   # 示例代码：复算 token（secret 未知，仅验证算法）
   import hashlib, socket, struct
   infohash = bytes.fromhex("00" * 20)
   ip, port = "192.0.2.1", 6881
   compact = socket.inet_aton(ip) + struct.pack(">H", port)  # 6 字节
   secret = b"\x00" * 4
   src = infohash + compact.ljust(18, b"\x00") + secret      # 定长 IPv6 槽位
   print(hashlib.sha1(src).hexdigest())
   ```

**需要观察的现象**：debug 日志里 `Invalid token` 出现的频率远低于合法 announce；同一来源对同一 infohash 的宣告在 10~20 分钟后可能突然开始失败（token 随双秘密窗口滑出有效期，直到对方下一轮 get_peers 拿到新 token）。

**预期结果**：理解「token 有效期 = 一至两个轮换周期」；脚本报文与源码布局逐字节对应。线上具体日志内容**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么校验时要同时接受 `secret_[0]` 与 `secret_[1]` 两代秘密？

**答案**：轮换瞬间（`updateTokenSecret` 刚执行完），网络上仍散布着用上一代秘密签发的 token，且 DHT 查找一轮迭代可能持续数十秒，请求方从拿到 token 到发出 announce_peer 也有延迟。若只认当前秘密，轮换后的第一个窗口期内所有合法宣告都会被误杀。双秘密让旧 token 在整整一个周期内继续有效，实现平滑过渡。

**练习 2**：攻击者从网络窃听到节点 A 向节点 B 请求到的 token，能用来向 B 宣告吗？

**答案**：不能。`validateToken` 用**报文真实源地址**（B 侧看到的 IP + UDP 端口）重算（[DHTAnnouncePeerMessage.cc:L104-L106](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTAnnouncePeerMessage.cc#L104-L106)），该 token 是用 A 的地址算出的，攻击者从自己的地址发报文，重算结果必然不同，校验失败被丢弃。

**练习 3**：把 `SECRET_SIZE` 从 4 提到 32 会更安全吗？有什么代价？

**答案**：安全性收益有限——token 本体是 20 字节的 SHA-1 摘要，输出长度才是抗暴力枚举的上界；秘密再长，攻击者对固定输出的碰撞/枚举难度不变。而代价是每条 get_peers 应答的 token 仍为 20 字节（不随秘密变长），但每个节点需要保密的熵更高、且 4.3.1 所述「长期收集 (地址, token) 对」的攻击依然受 10 分钟轮换窗口限制而非秘密长度。实际瓶颈在于攻击者根本拿不到秘密，4 字节随机数已提供 \(2^{32}\) 种可能。

### 4.4 端到端闭环：一次 get_peers → announce_peer 的完整对话

#### 4.4.1 概念说明

前三个模块分别讲了需求侧、账本侧、凭证侧。本模块把它们拼成一条真实的网络对话，回答学习目标中的最后一问：**get_peers 命中本地存储时，values 字段是怎么来的**。视角是两个节点 A（下载者）与 B（网络中的普通节点），外加一个后来者 C：

```text
A（下载者，运行 DHTGetPeersCommand）          B（普通 DHT 节点，持有账本）
──────────────────────────────────────────────────────────────────
DHTGetPeersCommand 触发（四档间隔之一）
 └─ DHTPeerLookupTask 迭代查找（u7-l3 的收敛循环）
     ├─ get_peers(infohash) ────────────────► DHTGetPeersMessage::doReceivedAction
     │                                          ① token = SHA1(infohash+B地址+secret)
     │                                          ② peers ← DHTPeerAnnounceStorage::getPeers
     │                                          ③ addLocalPeer：本机若有此内容则自荐
     │                                          ④ nodes  ← 路由表 getClosestKNodes(8个)
     │  ◄──────────────────────────────────── get_peers 应答 { token, values, nodes }
     ├─ token 存入 tokenStorage_[B的节点ID]
     ├─ values 里的 peer → peerStorage_->addPeer（喂给 BT 连接）
 └─ 查找收敛 onFinish：向最近 8 个节点
     announce_peer(infohash, 我的TCP端口, token) ─► validate()：按 B 地址重算比对
                                                    ├─ 失败 → 异常 → 按未知报文丢弃
                                                    └─ 成功 → addPeerAnnounce 入账
                                                              + markGood 回写路由表
                                       C（后来的查询者）
                                       get_peers(同一infohash) ──► B 的应答 values
                                                                   里从此包含 A
```

#### 4.4.2 核心流程

服务端 B 处理 `get_peers` 的四个步骤（对应上图①~④）：生成 token → 查账本 → 可选自荐 → 补最近节点。应答报文里 `values`（peer 列表）与 `nodes`（节点列表）二选一或并存——账本没有记录时至少还能给 `nodes` 让查询继续收敛。

#### 4.4.3 源码精读

服务端处理 get_peers 的完整动作：

[DHTGetPeersMessage.cc:L112-L128](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTGetPeersMessage.cc#L112-L128)

`doReceivedAction` 依次：用请求方地址生成 token（4.3 的公式）；`peerAnnounceStorage_->getPeers` 查账本（4.2 的读路径）；`addLocalPeer` 判断本机是否也在下载/持有该 infohash；`getClosestKNodes` 取路由表中离 infohash 最近的 8 个节点；组装 `DHTGetPeersReplyMessage` 入队。**这就是「get_peers 命中本地存储返回 values」的起点**。

本机自荐的条件：

[DHTGetPeersMessage.cc:L74-L110](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTGetPeersMessage.cc#L74-L110)

`addLocalPeer` 用 `BtRegistry` 按 infohash 反查本机是否有对应任务（u6-l2 讲过的按内容反查），且仅当用户配置了 `--bt-external-ip`（NAT 环境下公网可达地址）、该地址能按当前地址族解析、且尚不在列表中时，才把自己的 (externalIP, TCP 端口) 追加进 values——否则自荐一个不可达的私网地址毫无意义。

应答报文的编码与 values 上限：

[DHTGetPeersReplyMessage.cc:L78-L145](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTGetPeersReplyMessage.cc#L78-L145)

`getResponse` 组装 `r` 字典：`id`、`token`、可选 `nodes`（紧凑节点列表，最多 K=8 个）与可选 `values`。[L105-L143](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTGetPeersReplyMessage.cc#L105-L143) 的 values 分支把每个 Peer 打包成紧凑地址串。`MAX_VALUES_SIZE = 25` 的推导写在注释里，值得细读：UDP 数据报上限 65535 字节，但为避免 IP 分片（Path MTU 发现的 ICMP 可能被过滤），aria2 把报文预算压到 1024 字节；扣除 IP 头 20 + UDP 头 8 + 应答模板约 395（含 token、nodes 上限 38×8），剩余约 601 字节，每个 IPv6 紧凑地址 18 字节加 bencode 长度前缀约 3 字节，601 ÷ 21 ≈ 28，保守取 25。

入站应答的解析（客户端 A 这侧）：

[DHTMessageFactoryImpl.cc:L419-L452](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageFactoryImpl.cc#L419-L452)

`createGetPeersReplyMessage` 从收到的 `r` 字典里解析 `nodes`/`nodes6`（按地址族选择键）、逐项 `unpackcompact` 还原 values 列表中的 peer（长度不匹配或解析失败的项直接跳过）、取出 `token`。产物随后经 u7-l2 的 transaction id 配对送到 `DHTPeerLookupTask::onReceivedInternal`（4.1.3 精读过的收纳逻辑）。发往对端的 announce_peer 报文由 [DHTMessageFactoryImpl.cc:L469-L481](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageFactoryImpl.cc#L469-L481) 创建，注入账本与 token 追踪器这两个依赖（服务端收到时才用得上）。

#### 4.4.4 代码实践

**实践目标**：在一次真实下载的日志里，按时间顺序摘出闭环的每一环。

**操作步骤**：

1. 运行 `./src/aria2c --enable-dht "<热门种子>" --log=/tmp/loop.log --log-level=debug`。
2. 在日志中依次检索并抄录（均为 aria2 原生日志，无需改码）：
   - `Issuing PeerLookup for infoHash=`（4.1，查找发起）；
   - `Received N peers.`（[DHTPeerLookupTask.cc:L81-L82](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPeerLookupTask.cc#L81-L82)，values 收纳）；
   - `Message received: get_peers`（本机作为 B 角色服务他人）；
   - `Message received: announce_peer` 与 `Adding ... to peer announce list: infoHash=`（[DHTPeerAnnounceStorage.cc:L88-L90](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPeerAnnounceStorage.cc#L88-L90)，入账）；
   - `Invalid token`（校验失败，正常情况下应为空或极少）。
3. 把摘出的行按时间排列，对照 4.4.1 的对话图逐环勾销。

**需要观察的现象**：`Issuing PeerLookup` → 短延迟 → 多条 `Received N peers.` → 若本机节点 ID 恰好落在别人的查找路径上，还会看到本机收到 `get_peers`/`announce_peer`（B 角色日志）。`Received` 的 N 最大不会超过 25（values 上限）。

**预期结果**：一份能同时看到 A 角色（查 peer）与 B 角色（记账）双面行为的日志时间线。沙箱无公网 UDP 时 B 角色日志可能缺失，属正常。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：客户端收到 get_peers 应答后，`values` 与 `nodes` 分别流向哪里？

**答案**：见 [DHTPeerLookupTask.cc:L66-L83](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPeerLookupTask.cc#L66-L83)：`values` 中的 peer 交给 `peerStorage_->addPeer` 进入 BT 候选池；`nodes` 经 `getNodesFromMessage` 并入迭代查找的候选列表，驱动查找继续收敛（u7-l3 的循环）。

**练习 2**：如果账本里某 infohash 已有 100 个 peer，B 会怎么应答？

**答案**：只发前 25 个。[DHTGetPeersReplyMessage.cc:L128-L143](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTGetPeersReplyMessage.cc#L128-L143) 的循环条件 `valuesList->size() < MAX_VALUES_SIZE` 截断了列表——这是 UDP 分片风险下的刻意取舍，查询方多问几个节点即可凑齐更多 peer。

**练习 3**：`addLocalPeer` 为什么要求用户配置 `--bt-external-ip` 才自荐？

**答案**：见 [DHTGetPeersMessage.cc:L87-L98](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTGetPeersMessage.cc#L87-L98)：NAT 后的本机只能看到私网地址，把它写进 values 对外网查询者毫无连接价值；`--bt-external-ip` 是用户对「我的公网可达地址」的显式声明，配置了它自荐才有意义（且还需通过 `inetPton` 的地址族校验）。

## 5. 综合实践

把本讲三个模块串成一个观测实验——**给你的 aria2 装上「DHT 宣告仪表盘」**：

1. **改造三处日志**（均在已精读的代码点上）：
   - [DHTGetPeersCommand.cc:L100-L103](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTGetPeersCommand.cc#L100-L103)：发起查找时打印 `infoHash`、当前 peer 总数；
   - [DHTPeerAnnounceStorage.cc:L84-L92](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPeerAnnounceStorage.cc#L84-L92)：入账时打印 infohash、对端地址、去重后条目内 peer 数（4.2.4 的示例代码）；
   - [DHTPeerAnnounceStorage.cc:L129-L130](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPeerAnnounceStorage.cc#L129-L130)：清理后打印存活条目数。
   统一用 `A2_LOG_INFO`，重新编译。
2. **跑一个热门种子并做种 40 分钟以上**（`--seed-time=0` 表示不做种、仅下载也可，但做种能保证持续的announce 流量）：`./src/aria2c --enable-dht --seed-time=60 "<种子>" --log=/tmp/dash.log --log-level=info`。
3. **产出三个统计**：
   - 各 infohash 的宣告次数分布（`awk`/`sort`/`uniq -c`）；
   - 同一 infohash 的「宣告次数 vs 去重后 peer 数」比值，验证续租率；
   - 每 5 分钟清理点的条目数曲线，验证 30 分钟窗口下账本规模的稳态。
4. **回答一个问题**：把观察到的宣告平均间隔与 [DHTConstants.h:L61-L67](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTConstants.h#L61-L67) 的三个窗口（宣告 15 分钟、清理检查 5 分钟、淘汰 30 分钟）对照，解释「为什么正常下载者的记录不会掉出账本」以及「停止下载多久后它会消失」。

预期结论：宣告间隔 ≈ 15 分钟 < 淘汰阈值 30 分钟，活跃 peer 永续租；停止宣告后 30 分钟内（最迟在下一次 5 分钟周期的清理中）记录被剔除。具体数值**待本地验证**。

## 6. 本讲小结

- `DHTGetPeersCommand` 是每个（非私有）种子任务的常驻自循环命令，按 15 分钟/5 分钟/1 分钟/5 秒四档间隔把 `DHTPeerLookupTask` 送进 periodic2 泳道，peer 不足时最多连续重试 10 次，靠 `increaseNumCommand` 维持任务存活。
- `DHTPeerLookupTask` 收到应答时做两件本讲关键的事：把 token 按「应答方节点 ID」存入 `tokenStorage_`，把 values 里的 peer 喂给 BT 的 `PeerStorage`；收敛后向最近 8 个节点各发一个 announce_peer（无 token 的跳过）。
- `DHTPeerAnnounceStorage` 是两层账本：外层 `std::set` 按 20 字节 infohash 的 `memcmp` 序聚合，内层 `vector<PeerAddrEntry>` 按 (IP, 端口) 判重去重；peer 记录 30 分钟未续租即被每 5 分钟一次的 `handleTimeout` 淘汰，条目变空即删——租约式软状态，从不持久化。
- `announcePeer()` 与 `createPeerAnnounceTask` 是未完成的保留代码（后者返回 `nullptr`、前者无调用者）：aria2 只被动记账，不主动重宣告。
- token \(= \mathrm{SHA\text{-}1}(\text{infohash} \| \text{compact}(ip, port) \| \text{secret})\)，与请求方地址三元组绑定；双秘密每 10 分钟轮换、两代并存，token 有效期介于 10~20 分钟，既防伪造宣告又避免轮换误杀。
- 验 token 用报文的 UDP 源地址，入账记的是报文 `port` 字段的 TCP 端口；校验失败抛出的异常在 `DHTMessageReceiver` 被捕获、按未知报文丢弃，且不回写路由表健康度。get_peers 应答的 `values` 上限 25，由 1024 字节的防分片预算推导而来。

## 7. 下一步学习建议

本讲补完了 DHT 单元的最后一块拼图。建议：

1. **回看闭环**：把 u7-l3（任务队列与迭代查找）、u7-l4（DHTSetup 装配与本讲的周期命令）、本讲连起来重读 `DHTSetup.cc`，画出 DHT 组件的完整依赖图——这三讲合起来就是 aria2 DHT 的全貌。
2. **对比 tracker 体系**：重读 u6-l7 的 `DefaultBtAnnounce`，列出 tracker announce 与 DHT announce 在「间隔决策、凭证机制、peer 来源」上的异同（提示：tracker 无 token，但 HTTP tracker 有自己的防滥用语义；DHT 的 announcePeer 重宣告在 aria2 中未实现，而 tracker 的周期 announce 是实现的）。
3. **测试佐证**：阅读 `test/DHTPeerAnnounceStorageTest.cc` 与 `test/DHTTokenTrackerTest.cc`（u9-l5 将系统讲解测试体系），对照本讲的去重、过期、token 轮换行为，确认源码结论。
4. **继续主线**：按大纲进入单元 8（Metalink 与 RPC），或先做 u9-l6 的二次开发实践，把本讲综合实践里的「仪表盘」升级为一个新的 `--dht-stats` 选项。
