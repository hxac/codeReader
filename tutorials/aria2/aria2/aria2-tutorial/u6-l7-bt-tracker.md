# Tracker 交互：HTTP/UDP announce

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 tracker announce 的四种时机，并解释 announce 间隔为什么会动态变化（tracker 下发值、用户选项、零连接自救三个来源）。
2. 跟踪一次 HTTP announce 的完整链路：`TrackerWatcherCommand` 如何把 announce 伪装成一个普通的 HTTP 下载任务，再从内存里读出 bencode 应答。
3. 解释 AnnounceList 的 tier 轮换语义：成功者升为队首、失败者降级到下一个 tracker、全部失败后重置再来。
4. 对比 UDP tracker（BEP 15）的 connect/announce 两步握手与 HTTP tracker 的差异，理解 connection_id 伪会话、transaction_id 配对、5 秒重发/10 秒超时三件套。

## 2. 前置知识

### 2.1 tracker 是什么

BitTorrent 网络里，下载者（peer）彼此并不知道对方的存在，需要一个「电话簿」：tracker 就是一台 HTTP（或 UDP）服务器，客户端定期向它汇报自己的进度，并换取其他正在下载同一文件的人的地址列表。这个过程叫 **announce**（宣告）。

peer 的来源不止 tracker 一个：DHT（单元 7）和 PEX（下一讲 u6-l8）都能补充 peer。但 tracker 仍是大多数种子的第一来源——下载开始的第一个 peer 列表几乎总是 tracker 给的。

### 2.2 announce 的四种时机

接口注释写得很清楚（本讲源码精读会引用原文）：

| 时机 | 含义 |
|---|---|
| `started` | 下载刚开始，向 tracker 报到 |
| `stopped` | 客户端退出，告诉 tracker 我走了 |
| `completed` | 下载刚完成，从 leecher 变成 seeder |
| 周期性 | 距上次 announce 已过 tracker 规定的间隔 |

### 2.3 HTTP tracker 与 UDP tracker

- **HTTP tracker**：标准做法（BEP 3），就是一次普通的 HTTP GET，URL 里带上 `info_hash`、`peer_id` 等查询参数，响应体是一个 bencode 字典。
- **UDP tracker**（BEP 15）：用 UDP 报文完成同样的事。UDP 没有连接概念，协议用一对报文模拟「会话」：先发 CONNECT 拿一个 64 位 connection_id，再带着它发 ANNOUNCE。开销远小于 TCP 握手 + HTTP 头。

### 2.4 承接前面几讲

- u6-l1 讲过 `.torrent` 的 announce-list 如何解析成 `AnnounceList` 的「tier 层级 + tier 内 URL 队列」结构，本讲关注它在运行期如何被轮换。
- u6-l2 讲过 `BtRegistry` 托管四个进程级槽位，其中就包括本讲的 UDPTrackerClient；`BtSetup` 为每个种子装配种子级命令。
- u6-l4 讲过种子级周期命令（PeerChoke、ActivePeerConnection 等），`TrackerWatcherCommand` 是其中之一，且与 ActivePeerConnectionCommand 的「零连接自救」有直接联动。
- u2-l1/u2-l3 的结论在这里直接适用：命令默认 INACTIVE，被主循环每秒一次的全量刷新闸门唤醒；`execute()` 返回 false 必须自我重排。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/BtAnnounce.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtAnnounce.h) | announce 抽象接口：四种时机的注释、`DEFAULT_ANNOUNCE_INTERVAL` 常量 |
| [src/DefaultBtAnnounce.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtAnnounce.cc) | 默认实现：时机判定、HTTP URL 构造、应答字典消化 |
| [src/TrackerWatcherCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TrackerWatcherCommand.cc) | 守护命令：驱动一次 announce 的完整状态机，含 `HTTPAnnRequest`/`UDPAnnRequest` |
| [src/UDPTrackerClient.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UDPTrackerClient.cc) | UDP tracker 协议引擎：三队列状态机、报文编解码、超时处理 |
| [src/UDPTrackerRequest.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UDPTrackerRequest.h) | 请求/应答结构体与 action/event/error 枚举 |
| [src/AnnounceList.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceList.cc) / [src/AnnounceTier.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceTier.cc) | tier 轮换与事件状态机 |
| [src/NameResolveCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/NameResolveCommand.cc) | UDP tracker 主机名的 DNS 解析前置步骤 |
| [src/DHTInteractionCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTInteractionCommand.cc) | DHT 与 UDP tracker 共享 UDP socket 的收发驱动者 |
| [src/DHTSetup.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc) | 创建进程级 UDPTrackerClient 并注入 BtRegistry |
| [src/BtSetup.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtSetup.cc) | 为每个种子注册 TrackerWatcherCommand |
| [src/RequestGroup.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc) | 创建 DefaultBtAnnounce 并注入 `--bt-tracker-interval` |

## 4. 核心概念与源码讲解

### 4.1 DefaultBtAnnounce：何时宣告、宣告什么、如何消化应答

#### 4.1.1 概念说明

每个种子下载任务拥有一个 `DefaultBtAnnounce`（在 `RequestGroup::createInitialCommand` 的 BT 分支里创建，随六件套一起注册进 BtRegistry）。它回答三个问题：

1. **现在该不该 announce？**（时机判定）
2. **announce 的请求长什么样？**（URL 参数 / UDP 报文字段）
3. **应答怎么消化？**（interval 更新、peer 入库）

它不管「怎么把请求发出去」——那是下一节 TrackerWatcherCommand 的职责。这种分离让 HTTP 与 UDP 两种传输方式能共用同一套时机与应答逻辑。

#### 4.1.2 核心流程

**时机判定**：`isAnnounceReady()` 是三个判定的或：

```text
isStoppedAnnounceReady:    正在停机 && 存在允许发 stopped 事件的 tier
isCompletedAnnounceReady:  全部下载完成 && 存在允许发 completed 事件的 tier
isDefaultAnnounceReady:    无在途请求 && 距上次 announce 已超过等待间隔 && 尚有未失败的 tier
```

其中等待间隔的取值优先级是本讲实践任务的关键：

\[
\text{等待间隔} =
\begin{cases}
\text{userDefinedInterval} & (\texttt{--bt-tracker-interval} > 0 \text{ 时})\\
\text{minInterval} & (\text{否则})
\end{cases}
\]

**间隔动态变化的三个来源**：

1. **tracker 下发**：每次成功 announce 后，HTTP 应答里的 `interval`/`min interval` 字段、UDP 应答里的 `interval` 字段会覆盖本地值；
2. **用户选项**：`--bt-tracker-interval=N`（N>0）时固定为 N 秒，tracker 的值不再生效；
3. **零连接自救**：ActivePeerConnectionCommand 发现连接数为 0 且尚未下载完成时，把 minInterval 强制改回默认 2 分钟（若 tracker 给的间隔更长则缩短，尽快再要一批 peer）。

首次 announce 之前没有任何 tracker 下发值，用的是编译期默认 2 分钟（`DEFAULT_ANNOUNCE_INTERVAL = 2_min`）。

**URL 参数**（HTTP）：在 tracker 基础 URL 后追加查询串。

**应答消化**（HTTP）：bencode 字典逐字段处理，peers 解包进 PeerStorage。

#### 4.1.3 源码精读

先看接口注释，它就是四种时机的权威定义：

- [src/BtAnnounce.h:54-65](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtAnnounce.h#L54-L65) — `isAnnounceReady()` 的文档注释列出四种 announce 时机：started、stopped、completed、间隔到期。
- [src/BtAnnounce.h:142](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtAnnounce.h#L142) — `DEFAULT_ANNOUNCE_INTERVAL = 2_min`，构造时的初始间隔。

构造函数初始化三个间隔变量，注意 `userDefinedInterval_` 初始为 0（表示「未定义」）：

- [src/DefaultBtAnnounce.cc:60-75](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtAnnounce.cc#L60-L75) — 构造函数：`interval_` 与 `minInterval_` 都取默认 2 分钟；`announceList_` 直接引用 TorrentAttribute 里解析好的层级（u6-l1 的产物）。

三个时机判定，`isDefaultAnnounceReady` 里藏着间隔优先级公式：

- [src/DefaultBtAnnounce.cc:79-86](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtAnnounce.cc#L79-L86) — `trackers_ == 0`（无在途请求）且距上次 announce 超过 `userDefinedInterval_`（仅当非 0）或 `minInterval_`，且 tier 未全失败。
- [src/DefaultBtAnnounce.cc:88-98](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtAnnounce.cc#L88-L98) — stopped/completed 两个事件型判定：分别要求 BtRuntime 处于停机状态、PieceStorage 全部完成，且存在「允许接收该事件」的 tier。

`getAnnounceUrl` 把 HTTP announce 请求拼成完整 URL：

- [src/DefaultBtAnnounce.cc:148-183](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtAnnounce.cc#L148-L183) — 参数一览：`info_hash`（百分号编码的 20 字节）、`peer_id`、`uploaded`/`downloaded`（会话累计）、`left`（总长减已完成，即还差多少字节）、`compact=1`（要求紧凑格式 peer 列表）、`key`（peer_id 末 8 字节，tracker 用于跨 IP 识别同一客户端）、`numwant`、`no_peer_id=1`。注意 URL 原有查询串时用 `&` 拼接（`uriHasQuery` 判断）。
- [src/DefaultBtAnnounce.cc:153-156](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtAnnounce.cc#L153-L156) — `numWant` 默认 50；peer 够多（`!lessThanMinPeers()`）或正在停机时置 0——「我不要 peer 了，只是来汇报的」。
- [src/DefaultBtAnnounce.cc:184-206](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtAnnounce.cc#L184-L206) — 追加参数：`port`（BT 监听端口）、`event`（started/stopped/completed，空则周期性）、`trackerid`（tracker 上次下发的 ID，回传让它认得你）、`requirecrypto=1`（强制加密时）或 `supportcrypto=1`（声明支持）、`ip`（`--bt-external-ip` 指定时）。

应答消化，interval 的覆盖逻辑在这里：

- [src/DefaultBtAnnounce.cc:291-329](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtAnnounce.cc#L291-L329) — 先 bencode 解码；`failure reason` 存在则抛 `DL_ABORT_EX`（本次 announce 失败）；`interval` > 0 时更新 `interval_`；`min interval` 存在时取 `min(min_interval, interval)`，不存在时直接用 `interval`——所以本地计时器永不短于 tracker 认可的下限。
- [src/DefaultBtAnnounce.cc:340-361](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtAnnounce.cc#L340-L361) — `peers`/`peers6` 字段经 `bittorrent::extractPeer` 解包成 Peer 对象加入 PeerStorage，受 `lessThanMinPeers()` 闸门控制（peer 够多就不加）。

UDP 应答的消化更简单（结构化字段，无需 bencode）：

- [src/DefaultBtAnnounce.cc:364-384](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtAnnounce.cc#L364-L384) — `processUDPTrackerResponse`：interval 同时赋给 `minInterval_` 和 `interval_`；seeders/leechers 记账；peers 逐个 `addPeer`。

间隔的第二个来源——用户选项的注入点：

- [src/RequestGroup.cc:334-341](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L334-L341) — 创建 `DefaultBtAnnounce`，`setUserDefinedInterval(--bt-tracker-interval 秒)`，并 `shuffleAnnounce()` 打乱各 tier 内 tracker 顺序（负载分散）。
- [src/OptionHandlerFactory.cc:1693-1701](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L1693-L1701) — `bt-tracker-interval` 默认 "0"（即不覆盖），下限 0。

间隔的第三个来源——零连接自救（u6-l4 提过的联动，这里给出准确位置）：

- [src/ActivePeerConnectionCommand.cc:121-124](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ActivePeerConnectionCommand.cc#L121-L124) — 补完连接后若 `getConnections() == 0` 且未下载完成，`overrideMinInterval(DEFAULT_ANNOUNCE_INTERVAL)` 把等待间隔改回 2 分钟。注意这是无条件的 2 分钟：tracker 给的间隔更长时会缩短（尽快自救），但若 tracker 给的间隔本来短于 2 分钟反而会被拉长。
- [src/DefaultBtAnnounce.cc:417-420](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtAnnounce.cc#L417-L420) — `overrideMinInterval` 仅覆盖 `minInterval_`；用户自定义间隔非 0 时它不生效（优先级公式里 userDefinedInterval 短路）。

另外留意 `adjustAnnounceList` 里一个防重复的细节：

- [src/DefaultBtAnnounce.cc:133-141](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtAnnounce.cc#L133-L141) — 若下载在「started 事件还没来得及发」时就已完成，事件改标为 `STARTED_AFTER_COMPLETION`，防止后续把 started 误升级成 completed。
- [src/AnnounceTier.cc:46-64](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceTier.cc#L46-L64) — tier 的事件状态机：`STARTED→DOWNLOADING`、`STARTED_AFTER_COMPLETION→SEEDING`、`COMPLETED→SEEDING`、`STOPPED→HALTED`。announce 成功后 tier 会 `nextEvent()` 前进一格。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「用户选项覆盖 tracker 下发间隔」。

1. 准备一个可用的种子（HTTP tracker 即可），开启日志：
   `./src/aria2c --bt-tracker-interval=5 -l tracker.log --console-log-level=info file.torrent`
2. 在日志里检索 `Creating tracker request group`（HTTP announce 每次都会打这一行），记录每次出现的时间戳。
3. 观察前几次 announce 的时间差是否约为 5 秒（而不是 tracker 应答里下发的 interval，通常几百秒以上）。
4. 再去掉 `--bt-tracker-interval` 跑一次，此时间隔应变成 tracker 下发的值（首次 announce 前最长等 2 分钟默认值）。

**需要观察的现象**：加选项后 announce 频率显著变高；日志中每次 announce 后紧跟下载活动。
**预期结果**：两次 `Creating tracker request group` 的时间差 ≈ 5 秒 ± 调度误差。
**若网络/tracker 不可用**：本实践依赖真实 tracker 响应，具体数字待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么周期性 announce 用 `minInterval_` 而不是 `interval_` 做计时器？

**答案**：BT 规范里 `interval` 是 tracker 建议的「理想间隔」，`min interval` 是「最短允许间隔」；客户端频繁 announce 会被 tracker 封禁。aria2 保守地用 min_interval 计时（无该字段时退化为 interval，且取 `min(min_interval, interval)` 防止下发烧过 interval），见 [src/DefaultBtAnnounce.cc:319-329](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtAnnounce.cc#L319-L329)。

**练习 2**：`numwant=0` 在哪两种情况下出现？语义是什么？

**答案**：peer 数已不低于 `--bt-max-peers`（`!lessThanMinPeers()`）或正在停机（`isHalt()`）。语义是「我只要汇报进度，不需要新 peer」，见 [src/DefaultBtAnnounce.cc:153-156](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtAnnounce.cc#L153-L156)。

**练习 3**：`left` 参数的值怎么算？做种阶段它是多少？

**答案**：`getTotalLength() - getCompletedLength()`（[src/DefaultBtAnnounce.cc:157-159](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtAnnounce.cc#L157-L159)）；下载完成后恒为 0，tracker 据此把你标记为 seeder。

### 4.2 TrackerWatcherCommand：announce 的守护状态机

#### 4.2.1 概念说明

`DefaultBtAnnounce` 只做决策，真正把 announce 发出去、等结果、善后的，是 BtSetup 为每个种子装配的常驻自循环命令 `TrackerWatcherCommand`（u6-l4 所讲「种子级周期命令」家族的一员）。

它的核心设计是**决策与传输解耦**：抽象类 `AnnRequest` 定义了 `stopped/success/issue/stop/processResponse` 五个契约，两个实现分别封装 HTTP 与 UDP 传输。TrackerWatcherCommand 的状态机只面向 `AnnRequest` 接口编程，不关心报文怎么发。

#### 4.2.2 核心流程

`execute()` 每次被调度时走一遍如下状态机（默认 INACTIVE 状态，由主循环每秒的全量刷新唤醒；UDP 应答到达时会被实时唤醒，见 4.3）：

```text
execute():
  若任务被强制停止:
      有在途请求 → stop() 并稍后再来; 无 → 退出（返回 true）
  若 noMoreAnnounce（已停机且无 tier 可发 stopped）: 退出
  若无在途请求:
      createAnnounce():
          只要「有未失败 tier」且「时机已到」:
              取下一个 tracker URL
              scheme == "udp" 且 UDP 客户端可用 → UDPAnnRequest
              否则                                          → HTTPAnnRequest
              announceStart()（在途计数 +1）
          否则返回空 → 本次空转，自我重排等待下轮
      创建成功 → issue() 把请求交给引擎
  否则若在途请求已停止（stopped）:
      成功 → processResponse 消化应答
             → announceSuccess()（tier 游标复位到成功者）
             → resetAnnounce()（重置计时器）
             → addConnection()（用新 peer 发起出站连接）
      失败 → announceFailure()（游标移到下一个 tracker）
      全部 tier 失败 → resetAnnounce()（等待一个完整间隔后从头再来）
  自我重排（addCommandSelf），返回 false
```

#### 4.2.3 源码精读

注册点（每个种子一个）：

- [src/BtSetup.cc:110-118](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtSetup.cc#L110-L118) — BtSetup::setup 第一个装配的命令就是 TrackerWatcherCommand，注入 PeerStorage、PieceStorage、BtRuntime、BtAnnounce 四件依赖。

主状态机：

- [src/TrackerWatcherCommand.cc:209-272](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TrackerWatcherCommand.cc#L209-L272) — `execute()` 主体：强制停止的两段式退出（211-224）、`noMoreAnnounce` 终局（225-228）、创建请求（229-235）、结果结算（236-263）、尾部自我重排（270-271）。
- [src/TrackerWatcherCommand.cc:236-254](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TrackerWatcherCommand.cc#L236-L254) — 成功路径三连：`processResponse` → `announceSuccess` + `resetAnnounce` → `addConnection`。失败路径调用 `announceFailure`，且仅当 `isAllAnnounceFailed()` 才 `resetAnnounce`——失败的 tier 游标停在原地，下一次立即试下一个 tracker，不必等间隔。

传输方式的选择：

- [src/TrackerWatcherCommand.cc:296-330](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TrackerWatcherCommand.cc#L296-L330) — `createAnnounce`：循环内先 `getAnnounceUrl()`（内部会调 `adjustAnnounceList` 定事件），用 `uri_split` 解析；`udpTrackerClient_` 存在且 scheme 为 `udp` 走 UDP 请求，否则走 HTTP。注意 305-306 行的注释：UDP tracker 支持不可用时，udp:// URL 仍交给 HTTP 流程，让它在 HTTP 层报错失败（然后 tier 轮换）。URI 解析失败也会 `announceFailure` 换下一个。

HTTP announce 的实现最妙——它把 announce 伪装成一个普通的迷你下载任务：

- [src/TrackerWatcherCommand.cc:361-404](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TrackerWatcherCommand.cc#L361-L404) — `createHTTPAnnRequest`：new 一个独立 GID 的 `RequestGroup`，单 URI、单命令；`PREF_MAX_TRIES=2`（有备份 tracker 时每个 tracker 试 2 次就换，见 375-377 行注释）；超时取 `--bt-tracker-timeout`（默认 60 秒），连接超时取 `--bt-tracker-connect-timeout`；文件名固定为 `[tracker.announce]`；`ByteArrayDiskWriterFactory` 让应答体写进内存而非磁盘；关闭文件分配与本地文件检查、清空 pre/post 处理器（这个任务不归 RequestGroupMan 管）。
- [src/TrackerWatcherCommand.cc:89-103](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TrackerWatcherCommand.cc#L89-L103) — `issue()` 调 `createInitialCommand` 生成下载命令并投入引擎——于是 u4-l1 整套 HTTP 命令链（Initiate→Request→Response→Download）原封不动地为 announce 服务。
- [src/TrackerWatcherCommand.cc:80-82](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TrackerWatcherCommand.cc#L80-L82) — `stopped()` 判据是 `rg_->getNumCommand() == 0`：迷你任务的所有命令都跑完才算停止；`success()` 是 `downloadFinished()`。236-240 行的注释解释了为什么必须等命令彻底结束：命令还活着时销毁 RequestGroup 会悬空引用导致段错误。
- [src/TrackerWatcherCommand.cc:105-135](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TrackerWatcherCommand.cc#L105-L135) — `processResponse` 打开内存 DiskAdaptor，循环 `readData` 把 bencode 应答读进字符串，交给 `btAnnounce->processAnnounceResponse`（4.1.3 分析过的那个函数）。

announce 成功后的第一件事——用换来的 peer 发起连接：

- [src/TrackerWatcherCommand.cc:274-294](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TrackerWatcherCommand.cc#L274-L294) — `addConnection`：只要未停机且 peer 少于上限，就从 PeerStorage `checkoutPeer` 并创建 `PeerInitiateConnectionCommand`（u6-l3 讲过的出站握手第一棒）。

tier 轮换的语义在 AnnounceList 里：

- [src/AnnounceList.cc:107-117](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceList.cc#L107-L117) — `announceSuccess`：当前 tier 事件前进一格，成功的 tracker URL 挪到本 tier 队首，游标整体回到第一层第一个——下次 announce 优先找「刚刚成功过」的 tracker（稳定且省一轮探测）。
- [src/AnnounceList.cc:119-135](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceList.cc#L119-L135) — `announceFailure`：游标移到同 tier 下一个 tracker；tier 内耗尽则 tier 事件按需前进并移到下一 tier；所有 tier 耗尽时 `currentTrackerInitialized_ = false`。
- [src/AnnounceList.cc:252-257](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceList.cc#L252-L257) — `allTiersFailed` 就是「游标越过了最后一层」；`resetTier` 把游标拨回第一层第一个，配合 `DefaultBtAnnounce::resetAnnounce`（[src/DefaultBtAnnounce.cc:285-289](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtAnnounce.cc#L285-L289)，同时重置计时器）实现「全败后冷却一个间隔再从头来」。

构造与析构里的引用计数：

- [src/TrackerWatcherCommand.cc:187-207](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TrackerWatcherCommand.cc#L187-L207) — 构造时 `increaseWatchers()`、析构时 `decreaseWatchers()`。这个计数决定 DHTInteractionCommand 是否可以退出（见 4.3.3）——UDP tracker 的收发引擎比 watcher 活得久一点才行。

#### 4.2.4 代码实践

**实践目标**：观察 tier 失败降级——tracker 依次失败时游标如何移动。

1. 用 `--bt-tracker` 给种子追加几个必然失败的 tracker（aria2 支持给任意种子追加 tracker 列表，见 [src/OptionHandlerFactory.cc:1674-1682](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L1674-L1682) 的 `bt-tracker` 选项）：
   `./src/aria2c --bt-tracker="http://127.0.0.1:1/a,http://127.0.0.1:2/b" --bt-tracker-timeout=1 -l fail.log --console-log-level=info file.torrent`
2. 在 `fail.log` 里检索 `Creating tracker request group`，确认每个失败 tracker 各被尝试了 2 次（`PREF_MAX_TRIES=2`），然后游标移向下一个。
3. 对照 [src/AnnounceList.cc:119-135](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceList.cc#L119-L135)（announceFailure）画出游标移动轨迹。

**需要观察的现象**：日志中同一 tracker 的请求恰好连续出现 2 次后换下一个；全部失败后经过一段冷却（间隔到期）又从第一个重来。
**预期结果**：失败序列 127.0.0.1:1 ×2 → 127.0.0.1:2 ×2 →（若种子自带 tracker 则继续）→ 冷却后循环。
**说明**：`127.0.0.1` 的 1/2 端口必然连接拒绝，无需真实坏 tracker；具体重试节奏待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 HTTPAnnRequest 的 `stopped()` 用「命令数归零」而不是「下载完成」？

**答案**：下载完成时命令可能还在收尾（如连接回池）。若此时重置 `trackerRequest_`（销毁 RequestGroup），仍在执行的命令会引用已释放的对象，造成段错误。源码注释 [src/TrackerWatcherCommand.cc:237-240](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TrackerWatcherCommand.cc#L237-L240) 明确写了这一点。

**练习 2**：为什么把 announce 做成一个独立的 RequestGroup，而不是直接开个 socket 发 GET？

**答案**：复用。RequestGroup 意味着 HTTP 命令链、代理、TLS、异步 DNS、超时与重试体系全部免费获得，announce 请求与普通下载走完全相同的代码路径（u4-l1），只需把落盘目的地换成内存（ByteArrayDiskWriterFactory）。

**练习 3**：announce 成功后 tier 游标停在哪里？为什么？

**答案**：成功的 tracker 被移到其 tier 队首，且整体游标回到第一层第一个（[src/AnnounceList.cc:107-117](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceList.cc#L107-L117)）。下次周期 announce 优先访问「已知可用」的 tracker，避免每次都从可能失效的队头开始试错。

### 4.3 UDPTrackerClient：BEP 15 的 connect/announce 两步握手

#### 4.3.1 概念说明

UDP tracker 协议（BEP 15）用两个报文对完成一次 announce：

1. **CONNECT**：客户端发送魔法初始连接 ID `0x41727101980` + action(0) + transaction_id，tracker 回 64 位 connection_id。这个 ID 相当于 UDP 上的「伪会话票据」。
2. **ANNOUNCE**：带着 connection_id + action(1) + transaction_id + info_hash + peer_id 等字段，tracker 回 interval/leechers/seeders/紧凑 peer 列表。

两个关键 ID 分工不同：**connection_id 标识「与这个 tracker 的会话」**（有效期 1 分钟，缓存复用）；**transaction_id 标识「这一个请求」**（随机生成，应答必须回显，用于把应答与在途请求配对——UDP 里应答可能乱序、迟到甚至伪造）。

还有两个工程事实必须知道：

- **UDPTrackerClient 是进程级单例**，由 DHTSetup 创建、挂在 BtRegistry 槽位上（u6-l2 的四个进程级槽位之一）。源码注释直言「目前 UDPTrackerClient 随 DHT 一起启用」——因为驱动它的正是 DHT 的 UDP 收发命令。**DHT 未启用时 udp:// tracker 不可用**。
- **DHT 与 UDP tracker 共用一个 UDP socket**：两者都是 UDP 报文，区分方法是首字节——bencode 字典必以 `d` 开头，UDP tracker 应答是二进制不会是 `d`。

#### 4.3.2 核心流程

UDPTrackerClient 内部用三条队列管理所有（可能属于不同种子、不同 tracker 的）请求：

```text
addRequest(req)          → 进入 pendingRequests_（状态 PENDING）
createRequest()          → 每次为队首生成一个待发送报文：
    队首是 CONNECT            → 直接生成 16 字节 CONNECT 报文
    队首是 ANNOUNCE 且无连接ID → 插队生成一个 CONNECT（对同 tracker）
    队首是 ANNOUNCE 且 CONNECTING → 挪到 connectRequests_ 挂起等待
    队首是 ANNOUNCE 且有连接ID   → 生成 100 字节 ANNOUNCE 报文
发送成功 requestSent()    → 移入 inflightRequests_，记录派发时刻
收到应答 receiveReply()   → 按 (地址, 端口, transaction_id) 在 inflight 中找回请求
    CONNECT 应答  → 缓存 connection_id；把 connectRequests_ 里同 tracker 的挂起请求
                    全部放回 pending 队首
    ANNOUNCE 应答 → 填 reply 结构（interval/leechers/seeders/peers），标记 COMPLETE，
                    唤醒对应的 TrackerWatcherCommand
    ERROR 应答    → 标记失败；若是 CONNECT 的错则连坐失败同 tracker 的所有挂起请求
超时 handleTimeout()     → 在途超过 5 秒：重发一次（回 pending）
                          重发后超过 10 秒：彻底失败（COMPLETE + TIMEOUT）
连接 ID 缓存             → (地址, 端口) → connection_id，超过 1 分钟未更新即作废
```

ANNOUNCE 请求体是定长 100 字节，字段按偏移布局：

| 偏移 | 长度 | 字段 |
|---|---|---|
| 0 | 8 | connection_id |
| 8 | 4 | action（1 = announce） |
| 12 | 4 | transaction_id |
| 16 | 20 | info_hash |
| 36 | 20 | peer_id |
| 56 | 8 | downloaded |
| 64 | 8 | left |
| 72 | 8 | uploaded |
| 80 | 4 | event |
| 84 | 4 | ip |
| 88 | 4 | key |
| 92 | 4 | numwant |
| 96 | 2 | port |
| 98 | 2 | extensions（恒 0） |

#### 4.3.3 源码精读

常量与数据结构：

- [src/UDPTrackerClient.h:49](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UDPTrackerClient.h#L49) — `UDPT_INITIAL_CONNECTION_ID 0x41727101980`，协议规定的 CONNECT 魔数。
- [src/UDPTrackerClient.h:53-70](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UDPTrackerClient.h#L53-L70) — `UDPTrackerConnection`：连接 ID 缓存条目，带 CONNECTING/CONNECTED 状态与更新时间。
- [src/UDPTrackerRequest.h:48-70](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UDPTrackerRequest.h#L48-L70) — 四个枚举：action（CONNECT=0/ANNOUNCE=1/SCRAPE=2/ERROR=3）、error、state（PENDING/COMPLETE）、event（与 AnnounceTier 事件一一对应）。
- [src/UDPTrackerRequest.h:82-106](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UDPTrackerRequest.h#L82-L106) — 请求结构体：除了协议字段，还有运行时字段 `state`/`error`/`dispatched`/`failCount`/`reply`/`user_data`（回指创建它的 TrackerWatcherCommand，用于应答到达时唤醒）。

报文编码：

- [src/UDPTrackerClient.cc:555-566](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UDPTrackerClient.cc#L555-L566) — `createUDPTrackerConnect`：16 字节 = 初始连接 ID + action + transaction_id。
- [src/UDPTrackerClient.cc:568-592](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UDPTrackerClient.cc#L568-L592) — `createUDPTrackerAnnounce`：按上表逐字段写入 100 字节。

请求生成与队列流转：

- [src/UDPTrackerClient.cc:262-301](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UDPTrackerClient.cc#L262-L301) — `createRequest` 的四分支：CONNECT 优先；无连接 ID 时为 ANNOUNCE 插队生成 CONNECT（278-287 行）；CONNECTING 期间挂起到 connectRequests_（289-292 行）；有 ID 才发 ANNOUNCE。
- [src/UDPTrackerClient.cc:303-338](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UDPTrackerClient.cc#L303-L338) — `requestSent`：记录派发时刻、CONNECT 发出时在缓存写入 CONNECTING 占位条目（330-334 行）、移入 inflight 队列。
- [src/UDPTrackerClient.cc:371-376](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UDPTrackerClient.cc#L371-L376) — `addRequest`：状态置 PENDING、错误清零、入 pending 队列。

应答解析：

- [src/UDPTrackerClient.cc:135-174](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UDPTrackerClient.cc#L135-L174) — CONNECT 应答分支：长度必须恰 16 字节；按 transaction_id 找回在途请求；连接 ID 写入缓存（160-161 行）；把 connectRequests_ 里同 tracker 的挂起请求全部放回 pending 队首（164-169 行）——「会话建立了，排队的都能走了」。
- [src/UDPTrackerClient.cc:175-219](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UDPTrackerClient.cc#L175-L219) — ANNOUNCE 应答分支：长度至少 20 字节；解析 interval/leechers/seeders；从偏移 20 起每 6 字节（4 字节 IPv4 + 2 字节端口）解包一个 peer；打一条包含全部关键信息的 INFO 日志（实践任务会用到）。
- [src/UDPTrackerClient.cc:220-249](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UDPTrackerClient.cc#L220-L249) — ERROR 应答分支：请求标记 TRACKER 错误；若出错的是 CONNECT，`failConnect` 连坐失败同 tracker 的所有挂起 ANNOUNCE。

超时两段式：

- [src/UDPTrackerClient.cc:378-462](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UDPTrackerClient.cc#L378-L462) — `TimeoutCheck`：`failCount == 0` 且派发超过 5 秒 → 重发一次（回 pending 队列，385-406 行）；`failCount > 0` 且超过 10 秒 → 标记 TIMEOUT 完成（412-437 行）。UDP 不可靠，一次丢包就判死太激进，重发一次才合算。

连接 ID 缓存过期：

- [src/UDPTrackerClient.cc:484-500](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UDPTrackerClient.cc#L484-L500) — `getConnectionId`：CONNECTED 状态且超过 1 分钟未更新则删除缓存返回空——下次 announce 得重新 CONNECT（协议允许 tracker 让 ID 提前失效，过期重取最稳妥）。

DNS 前置与 I/O 驱动（UDP 路径的接力）：

- [src/TrackerWatcherCommand.cc:163-173](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TrackerWatcherCommand.cc#L163-L173) — `UDPAnnRequest::issue` 创建 `NameResolveCommand` 解析 tracker 主机名（UDP tracker 目前只用 IPv4）。
- [src/NameResolveCommand.cc:140-145](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/NameResolveCommand.cc#L140-L145) — 解析成功 `onSuccess`：把地址写回请求并 `getUDPTrackerClient()->addRequest(req_)`——请求从此归协议引擎管。
- [src/DHTSetup.cc:151-152](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L151-L152) — 注释与创建：「For now, UDPTrackerClient was enabled along with DHT」——进程级单例随 DHT 初始化而诞生。
- [src/DHTSetup.cc:210-218](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L210-L218) / [src/DHTSetup.cc:270](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L270) — `DHTInteractionCommand`（DHT 的 UDP 收发命令）被注入 UDPTrackerClient；后者再挂到 BtRegistry。DHT 初始化失败时槽位清空（[src/DHTSetup.cc:296-301](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L296-L301)），UDP tracker 随之不可用。
- [src/DHTInteractionCommand.cc:116-142](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTInteractionCommand.cc#L116-L142) — 收包判别：首字节 `d` 交给 DHT，否则交给 `udpTrackerClient_->receiveReply`；ANNOUNCE 应答完成时通过 `req->user_data` 找回 TrackerWatcherCommand，`setStatus(STATUS_ONESHOT_REALTIME)` + `setNoWait(true)` 立即唤醒它去结算结果（133-138 行）。
- [src/DHTInteractionCommand.cc:147-166](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTInteractionCommand.cc#L147-L166) — 每轮先 `handleTimeout`（超时重发/失败），再把 pending 队列逐个 `createRequest` + `sendMessage` 发出去。
- [src/DHTInteractionCommand.cc:99-100](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTInteractionCommand.cc#L99-L100) — DHTInteractionCommand 的退出条件与 `getNumWatchers()` 挂钩：只要有 TrackerWatcherCommand 活着（watchers > 0），即使全部下载完成它也得继续服务 stopped 事件的发送。

#### 4.3.4 代码实践

**实践目标**：看到 CONNECT 只在「首次」与「缓存过期后」出现，验证两步握手与连接 ID 缓存。

1. 给种子注入一个公共 UDP tracker（无需种子本身带 UDP tracker）：
   `./src/aria2c --enable-dht --bt-tracker="udp://tracker.opentrackr.org:1337/announce" --bt-tracker-interval=70 -l udp.log --console-log-level=info file.torrent`
   （`--bt-tracker-interval=70` 使周期 announce 略超 60 秒，制造缓存过期。）
2. 在 `udp.log` 中按时间检索这三类行：
   - `UDPT sent CONNECT to ...`
   - `UDPT received CONNECT reply from ... connection_id=...`
   - `UDPT sent ANNOUNCE to ...` 与 `UDPT received ANNOUNCE reply from ... interval=...`
3. 统计：第 1 次 announce 前有 CONNECT+ANNOUNCE 两对报文；第 2 次（60 秒内）应只有 ANNOUNCE 一对；第 3 次（距上次超 60 秒）又出现 CONNECT。

**需要观察的现象**：CONNECT 出现次数 ≈ announce 次数的一半或略多；`received ANNOUNCE reply` 行尾的 `interval=` 值即 tracker 下发的间隔（4.1 的间隔来源一）。
**预期结果**：时间线呈「CONNECT+ANNOUNCE → ANNOUNCE → CONNECT+ANNOUNCE → …」的节奏。
**说明**：依赖公共 tracker 的可达性，具体报文序列待本地验证；也可用 `tcpdump udp port 1337` 抓包对照。

#### 4.3.5 小练习与答案

**练习 1**：一次成功的 UDP announce 最少需要几对 UDP 报文？HTTP tracker 呢？

**答案**：UDP：首次 2 对（CONNECT 往返 + ANNOUNCE 往返），连接 ID 缓存命中时 1 对；HTTP：至少 1 次 TCP 三次握手 + 1 个 GET + 1 个响应（还需 TCP 慢启动与 HTTP 头开销），若算上 TLS 更多。这正是 BEP 15 的存在理由。

**练习 2**：transaction_id 与 connection_id 各解决什么问题？

**答案**：connection_id 是「会话票据」，向 tracker 证明我已完成握手，1 分钟有效，缓存复用（[src/UDPTrackerClient.cc:484-500](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UDPTrackerClient.cc#L484-L500)）；transaction_id 是「请求编号」，随机生成且应答必须回显，用于在无连接的 UDP 上把应答与在途请求配对并防伪造（[src/UDPTrackerClient.cc:465-482](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UDPTrackerClient.cc#L465-L482) 的 `findInflightRequest`）。

**练习 3**：为什么超时处理是「5 秒重发一次、再等 10 秒才判死」的两段式？

**答案**：UDP 不保证送达，单次未响应可能是丢包而非 tracker 失效；立即判死会造成无谓的 tier 降级。先重发一次对冲丢包，仍无应答才标记 TIMEOUT（[src/UDPTrackerClient.cc:378-437](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UDPTrackerClient.cc#L378-L437)）。

## 5. 综合实践

**任务**：为同一个种子同时准备 HTTP 与 UDP 两类 tracker，整理一条完整的「announce 时间线」，用日志验证本讲三个模块的全部结论。

**准备**：

```bash
./src/aria2c \
  --enable-dht \
  --bt-tracker="udp://tracker.opentrackr.org:1337/announce,http://example.invalid/announce" \
  --bt-tracker-interval=0 \
  -l timeline.log --console-log-level=info \
  file.torrent
```

（`bt-tracker-interval=0` 表示不覆盖，让 tracker 下发值主导间隔，便于观察动态变化。）

**步骤**：

1. 从 `timeline.log` 中提取四类日志行（均为源码中的 INFO 级日志）：
   - HTTP 路径：`Creating tracker request group GID#...`（[src/TrackerWatcherCommand.cc:401-402](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TrackerWatcherCommand.cc#L401-L402)）
   - UDP 路径：`UDPT sent/received ...`（[src/UDPTrackerClient.cc:312-322](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UDPTrackerClient.cc#L312-L322)、[src/UDPTrackerClient.cc:206-214](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UDPTrackerClient.cc#L206-L214)）
2. 填写下面这张时间线表：

| 时刻 | tracker | 协议 | 事件 | tracker 下发 interval | 下次 announce 预计时刻（实测） |
|---|---|---|---|---|---|
| t0 | opentrackr | UDP | started | 例 1800 | ... |
| t0' | example.invalid | HTTP | started（失败） | - | 立即降级下一个 tracker |
| t1 | ... | ... | ... | ... | ... |

3. 验证三件事：
   - **间隔动态变化**：t1 - t0 是否等于 tracker 在 t0 下发的 interval？若中途断网导致零连接，间隔是否被拉回 2 分钟（[src/ActivePeerConnectionCommand.cc:121-124](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ActivePeerConnectionCommand.cc#L121-L124)）？
   - **tier 轮换**：HTTP tracker 失败 2 次后是否换下一个 tracker（[src/TrackerWatcherCommand.cc:375-377](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TrackerWatcherCommand.cc#L375-L377)）？成功后是否总是同一个 tracker 被优先 announce？
   - **协议对比**：统计两类 tracker 各自完成 N 次 announce 消耗的「应用层往返」次数：UDP 首次 2N+缓存命中修正，HTTP 恰好 N 次 GET（但每次都要完整 HTTP 事务）。
4. 结束时观察 `event=stopped` 的最后一次 announce（Ctrl-C 优雅停止），对照 [src/DefaultBtAnnounce.cc:88-98](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtAnnounce.cc#L88-L98) 与 `noMoreAnnounce` 的退出条件。

**预期结果**：一张能自洽解释每个 announce 时刻的时间线表；UDP 与 HTTP 的交互次数差异清晰可见。
**说明**：公共 tracker 行为、网络状况因时因地而异，表中数值待本地验证；`example.invalid` 是保留的必然解析失败域名，用于制造 HTTP 失败样本。

## 6. 本讲小结

- **DefaultBtAnnounce 管决策**：四种 announce 时机；HTTP URL 的全部参数（info_hash/peer_id/left/key/numwant/event…）；应答消化时用 tracker 下发的 min_interval/interval 更新本地计时器，且用户 `--bt-tracker-interval` 与零连接自救都会改写间隔——这就是 announce 间隔动态变化的全部三个来源。
- **TrackerWatcherCommand 管调度**：常驻自循环状态机「创建→等待→结算→重排」，通过 `AnnRequest` 抽象把 HTTP 与 UDP 传输解耦；HTTP announce 是一个用 ByteArrayDiskWriterFactory 落盘到内存的迷你 RequestGroup，复用整条 HTTP 命令链；tier 轮换语义为「成功者升队首、失败者降级、全败冷却一个间隔重头再来」。
- **UDPTrackerClient 管协议**：BEP 15 两步握手（CONNECT 换 connection_id、ANNOUNCE 带票据请求），transaction_id 配对在途请求；三队列（pending/connect/inflight）+ 连接 ID 一分钟缓存 + 5 秒重发/10 秒超时的两段式容错。
- **UDP tracker 与 DHT 共生**：进程级单例由 DHTSetup 创建、由 DHTInteractionCommand 驱动收发，两者共享一个 UDP socket，靠首字节 `d` 区分报文；DHT 关闭时 udp:// tracker 不可用。

## 7. 下一步学习建议

- **u6-l8（扩展协议）**：tracker 给的 peer 列表只是起点，ut_pex 让已连接的 peer 互相扩散 peer 列表，与本讲的 announce 形成互补的 peer 来源；ut_metadata 则是 magnet 流程（u6-l9）的基石。
- **u7（DHT 单元）**：本讲已经见到 DHTInteractionCommand 的收发循环与 UDP socket 共享，u7-l4 会完整拆解 DHTSetup 装配的整套周期命令；u7-l5 的 DHTPeerAnnounceStorage 是「tracker 化的 DHT」——get_peers/announce_peer 与本讲的 announce 在概念上同构，对照阅读会非常顺畅。
- **源码延伸**：把 [src/UDPTrackerClient.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UDPTrackerClient.cc) 的 `receiveReply` 与 [src/DefaultBtAnnounce.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtAnnounce.cc) 的 `processAnnounceResponse` 并排读一遍，体会「同一份 announce 语义在两种编码（bencode 字典 vs 定长二进制）上的双实现」。
