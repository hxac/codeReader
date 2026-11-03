# DHT 任务系统：队列、执行器与消息追踪

## 1. 本讲目标

前两讲里，我们已经知道了 DHT 的「账本」（u7-l1 路由表与 K 桶）和「语言」（u7-l2 bencode 报文与 transaction id）。但还有一个关键问题没有回答：

- 一次 Kademlia 查找需要**多轮网络往返**（问一个节点、等回复、再问更近的节点……），而 u2-l1 讲过 DownloadEngine 是单线程事件循环，任何一步都不能阻塞等待——那么这「一场跨多轮的对话」由谁驱动、如何推进？
- 我们通过无连接的 UDP 发出几十个 query，收到的响应包里只有一个 4 字节 transaction id——**怎么知道哪个响应对应哪个请求、该通知谁**？
- 发出去石沉大海的请求，**多久判定超时、超时后又发生什么**？

本讲回答这三个问题。学完后你应该能够：

1. 读懂 `DHTTaskExecutor::update()`，说出它每轮「先回收、再补位」的调度逻辑，以及三条任务泳道（periodic1/periodic2/immediate）各自的用途。
2. 画出 `DHTAbstractNodeLookupTask` 的迭代收敛循环：候选列表如何一轮轮向目标 ID 收缩，终止条件为什么是「在途消息归零」。
3. 解释 `DHTMessageTracker` 用 (transaction id, IP, port) 三元组配对请求与响应的机制，以及超时清理如何反哺路由表的健康度统计。
4. 对比 `DHTNodeLookupTask` 与 `DHTPeerLookupTask` 这对孪生任务的目标差异。

## 2. 前置知识

### 2.1 迭代式查找（iterative lookup）

Kademlia 论文定义了两种查找方式：

- **递归式**：客户端问最近的一个节点「帮我把这事查到底」，对方替你一路问下去，最后把结果带回来。
- **迭代式**：客户端自己当发动机——每轮向当前最近的几个节点发 query，从响应里得知更近的节点，再向它们发 query，直到没有更近的信息为止。

BitTorrent DHT（BEP 5）采用**迭代式**。这带来了一个工程问题：迭代过程天然要「发出→等待→再发出」循环多次，而每次等待都可能长达数秒。如果把这套循环写成一个阻塞函数，整个 aria2 的事件循环就会卡死。于是 aria2 把「一次查找」建模成一个**有状态的任务对象**，让事件循环每转一圈就推进它一小步。

### 2.2 本讲需要的关键概念

| 术语 | 含义 | 出处 |
|------|------|------|
| DHTTask | 「一次跨越多轮 RPC 的目标」的抽象，只有 `startup()` 和 `finished()` 两个行为 | 本讲 |
| TaskQueue / TaskExecutor | 任务的排队与调度器，DHT 的「CPU」 | 本讲 |
| α（ALPHA） | 每轮同时发出的查找消息数上限，Kademlia 论文推荐 3 | 本讲 |
| K | 候选列表容量与桶容量，aria2 中为 8 | u7-l1 |
| transaction id | 4 字节随机串，区分同一节点发出的多个未决 query | u7-l2 |
| RTT | round-trip time，一次 RPC 的往返耗时，用于节点排序 | u7-l1 |
| 在途消息（in-flight message） | 已发出但还没收到响应（也没超时）的 query 数量 | 本讲 |
| XOR 距离 | 两个 160 位节点 ID 按位异或后的整数大小，衡量「远近」 | u7-l1 |

### 2.3 与事件循环的衔接

回忆 u2-l1：DownloadEngine 每圈执行到期命令。DHT 子系统里有一个常驻的 `DHTInteractionCommand`（u7-l4 会详讲它的装配），它每圈做四件事：**驱动任务队列 → 收 UDP 包并配对分发 → 清理超时 → 把待发消息真正写出**。本讲的三个模块分别对应其中「驱动」「配对分发」「清理」三步。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/DHTTask.h` | 任务的最小接口：`startup()` + `finished()` |
| `src/DHTTaskQueue.h` / `src/DHTTaskQueueImpl.h` / `src/DHTTaskQueueImpl.cc` | 任务队列接口与实现：三条泳道 |
| `src/DHTTaskExecutor.h` / `src/DHTTaskExecutor.cc` | 单条泳道的调度器：回收已完成、启动新任务 |
| `src/DHTAbstractNodeLookupTask.h` | 迭代查找的模板基类：**整个收敛循环都在这里**（头文件实现） |
| `src/DHTNodeLookupTask.cc` | find_node 特化：目标是节点 ID，用于刷新/填充路由表 |
| `src/DHTPeerLookupTask.cc` | get_peers 特化：目标是 infohash，用于找 peer 并宣告自己 |
| `src/DHTBucketRefreshTask.cc` | 桶刷新任务：为每个过期桶派生一个 NodeLookupTask |
| `src/DHTTaskFactoryImpl.cc` | 任务工厂：创建各任务并统一注入依赖 |
| `src/DHTMessageTracker.cc` / `src/DHTMessageTrackerEntry.cc` | 请求-响应配对账本与超时清理 |
| `src/DHTMessageDispatcherImpl.cc` | 消息发送器：query 发出（或发送失败）时向 tracker 登记 |
| `src/DHTInteractionCommand.cc` | 每圈驱动上述一切的常驻命令 |
| `src/DHTConstants.h` | DHT 相关常量（transaction id 长度、超时时长等） |

## 4. 核心概念与源码讲解

### 4.1 DHTTaskQueue 与 DHTTaskExecutor：三条泳道、统一节拍

#### 4.1.1 概念说明

DHT 里的「任务」不是 u2-l3 里那种 Command。Command 是事件循环的调度单元（每圈执行一次 `execute()`）；而 DHTTask 是**协议层的长期目标**——「查清谁离这个 ID 最近」「找到拥有这个种子的 peer」。一个 DHTTask 的生命周期里会发出很多条 DHT 报文、跨越很多圈事件循环，它的推进靠的是「响应到达时的回调」，而不是被反复调用。

任务队列要解决的问题有两个：

1. **限流**：迭代查找会像滚雪球一样产生大量并发 RPC，必须限制同时在执行的任务数，防止 UDP 发包洪水。
2. **分类**：不同来源的任务有不同的紧迫度——桶刷新可以慢慢做，收到 peer 的 DHT port 消息后回 ping 应该尽快做。

aria2 的方案是把任务分进**三条泳道**，每条泳道是一个独立的 `DHTTaskExecutor`，各自排队、各自限流、互不挤占名额。

#### 4.1.2 核心流程

每圈事件循环中，`DHTInteractionCommand::execute()` 调用一次 `taskQueue_->executeTask()`，它依次驱动三条泳道：

```text
DHTInteractionCommand::execute()   （每圈一次，常驻命令自循环）
  └─ DHTTaskQueueImpl::executeTask()
       ├─ periodicTaskQueue1_.update()   // 泳道 1：路由表维护类任务
       ├─ periodicTaskQueue2_.update()   // 泳道 2：下载服务类任务
       └─ immediateTaskQueue_.update()   // 泳道 3：即时任务（插队）
```

每个 `DHTTaskExecutor::update()` 固定三步：

```text
1. 回收：把 execTasks_ 中 finished() == true 的任务删掉
2. 算空位：r = numConcurrent_ - execTasks_.size()（下限 0）
3. 补位：只要 r > 0 且 queue_ 非空，取出队首任务
       调 task->startup()（同步执行，任务在这里发出第一波消息）
       若任务没立刻结束，放入 execTasks_，占用一个并发名额
```

注意一个关键分工：**executor 只负责「启动」和「回收」，不负责「推进」**。任务启动后，后续每一步推进都由 DHTMessageTracker 的响应回调（下一小节及 4.3）触发——响应到达 → 回调 `task->onReceived()` → 任务内部再发下一波消息。executor 每圈所做的只是扫一眼有没有任务已经悄悄完成，好把名额让给排队者。

#### 4.1.3 源码精读

**任务的最小契约**只有两个函数——启动与完成判定：

- [src/DHTTask.h:L42-L49](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTTask.h#L42-L49)：`DHTTask` 纯虚接口定义 `startup()` 与 `finished()`。这个极简接口让 executor 无需了解任务内部状态就能调度它。

**队列接口与三条泳道**：

- [src/DHTTaskQueue.h:L46-L57](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTTaskQueue.h#L46-L57)：抽象接口声明 `executeTask()` 与三个 add 方法（periodic1、periodic2、immediate）。
- [src/DHTTaskQueueImpl.cc:L42-L51](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTTaskQueueImpl.cc#L42-L51)：匿名命名空间定义 `NUM_CONCURRENT_TASK = 15`，构造函数用它初始化三个 `DHTTaskExecutor`——即每条泳道最多同时执行 15 个任务，三条泳道全局上限 45。
- [src/DHTTaskQueueImpl.cc:L55-L63](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTTaskQueueImpl.cc#L55-L63)：`executeTask()` 依次更新三条泳道，每条前面有一句 DEBUG 日志，方便在 `--log-level=debug` 下区分。

**执行器的数据结构与调度核心**：

- [src/DHTTaskExecutor.h:L48-L67](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTTaskExecutor.h#L48-L67)：`execTasks_` 是 `vector`（正在执行的任务，需要随机遍历与删除），`queue_` 是 `deque`（等待队列，只需队首弹出、队尾压入）；`addTask()` 就是简单的 `push_back`。
- [src/DHTTaskExecutor.cc:L54-L78](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTTaskExecutor.cc#L54-L78)：`update()` 全文不到 25 行，是本模块的心脏。第 56-58 行先用 `remove_if` + `mem_fn(&DHTTask::finished)` 把已完成任务从 `execTasks_` 抹掉；第 60-65 行计算剩余空位 `r`；第 66-74 行的 while 循环逐个启动队首任务——注意第 70 行的判断：如果 `startup()` 之后任务立刻 `finished()`（例如路由表为空、无事可做），它**不进入** `execTasks_`，也不消耗空位；第 75-77 行输出 DEBUG 日志「Executing N Task(s). Queue has M task(s).」，这是本讲实践任务要抓的主要观测点。

**节拍的来源**：

- [src/DHTInteractionCommand.cc:L95-L117](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTInteractionCommand.cc#L95-L117)：常驻命令 `execute()` 的开头——先判断退出条件（下载全部结束或停机），第 110 行调用 `taskQueue_->executeTask()` 驱动三条泳道。
- [src/DHTInteractionCommand.cc:L167-L168](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTInteractionCommand.cc#L167-L168)：把 `this` 重新塞回 routineCommands 队列并返回 false——即**自循环**，下一圈事件循环再来一遍。这就是 DHT 子系统的「心跳」。

**谁在往三条泳道里塞任务？**（这是理解泳道分工的最好材料）

| 调用点 | 泳道 | 任务 | 场景 |
|--------|------|------|------|
| [src/DHTEntryPointNameResolveCommand.cc:L140-L144](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTEntryPointNameResolveCommand.cc#L140-L144) | periodic1 | 自举 NodeLookup + BucketRefresh | 启动时从 entry point 引导路由表 |
| [src/DHTEntryPointNameResolveCommand.cc:L152-L160](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTEntryPointNameResolveCommand.cc#L152-L160) | periodic1 | Ping entry point 节点 | 引导节点探活 |
| [src/DHTBucketRefreshCommand.cc:L64-L67](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketRefreshCommand.cc#L64-L67) | periodic1 | BucketRefresh | 每 15 分钟检查一次过期桶（u7-l4 详讲） |
| [src/DHTSetup.cc:L252-L257](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L252-L257) | periodic1 | 强制 BucketRefresh（forceRefresh） | 从 dht.dat 恢复出节点后立即全量刷新 |
| [src/DHTGetPeersCommand.cc:L100-L108](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTGetPeersCommand.cc#L100-L108) | periodic2 | PeerLookup | 下载任务缺 peer 时按 infohash 查找（u7-l5 详讲） |
| [src/BtPortMessage.cc:L86-L95](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtPortMessage.cc#L86-L95) | immediate | Ping / NodeLookup | 收到 BT peer 的 port 消息，把对方纳入 DHT（桶太少时顺手自举） |

可以看出泳道语义：**periodic1 = 让 DHT 网络拓扑保持健康的「内务」任务；periodic2 = 为具体下载服务的「业务」任务；immediate = 对外来事件即时回应**。两者分开限流，保证业务高峰不会饿死内务、内务突发也不会拖慢业务。

顺带一提，periodic2 的另一个潜在用户 `DHTPeerAnnounceStorage::announcePeer()`（[src/DHTPeerAnnounceStorage.cc:L133-L147](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPeerAnnounceStorage.cc#L133-L147)）目前是死代码——它依赖的 `createPeerAnnounceTask` 是一个返回 `nullptr` 的 TODO 桩（见 [src/DHTTaskFactoryImpl.cc:L100-L105](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTTaskFactoryImpl.cc#L100-L105)），且 `announcePeer()` 在源码中没有任何调用者。阅读时不要把它当成活跃路径。

**任务的出生地——工厂统一注入依赖**：

- [src/DHTTaskFactoryImpl.cc:L117-L125](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTTaskFactoryImpl.cc#L117-L125)：`setCommonProperty()` 给每个新建任务注入五项依赖：路由表、消息分发器、消息工厂、任务队列（供任务再派生子任务，如 BucketRefresh 派生 NodeLookup）、本地节点。这与 u6-l5 的 `setCommonProperty` 是同一套装配思想。
- [src/DHTTaskFactoryImpl.cc:L53-L60](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTTaskFactoryImpl.cc#L53-L60)：工厂默认超时取 `DHT_MESSAGE_TIMEOUT`（10 秒，见 [src/DHTConstants.h:L52-L59](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTConstants.h#L52-L59)，对应 `--dht-message-timeout` 选项），Ping/ReplaceNode 任务会显式 `setTimeout(timeout_)`。

#### 4.1.4 代码实践

**实践目标**：亲眼看到三条泳道每圈「执行中 / 排队中」的任务数量变化，理解调度节拍。

**操作步骤**：

1. 按 u1-l2 的方法编译好 aria2。
2. 打开 `src/DHTTaskExecutor.cc`，把第 75-77 行的 `A2_LOG_DEBUG` 改成 `A2_LOG_INFO`（只改日志级别这一处），并在 `DHTTaskQueueImpl::executeTask()`（[src/DHTTaskQueueImpl.cc:L55-L63](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTTaskQueueImpl.cc#L55-L63)）的三个 update 调用前各加一行带泳道编号的 `A2_LOG_INFO`（示例代码，实验后请还原）。这样无需开 debug 级别就能看到每条泳道的负载。
3. 重新编译，运行：

   ```bash
   ./src/aria2c --enable-dht=true --dht-listen-port=6881 \
     -l dht.log --log-level=info \
     "magnet:?xt=urn:btih:<任一真实 infohash>"
   ```

4. 观察日志中 `Executing N Task(s). Queue has M task(s).` 行的 N 与 M。

**需要观察的现象**：启动初期 periodic1 泳道的 M（排队数）先冲高（ping 各 entry point + 自举 NodeLookup 同时入队），随后 N 稳定在 15 以内；随着任务完成，M 回落。

**预期结果**：任一时刻 N ≤ 15；任务队列呈现「入队突发 → 逐步消化 → 归零等待」的锯齿形态。若网络不通，任务会挂满约 10 秒（消息超时）后才被回收。实际数值待本地验证（取决于 entry point 可达性与网络环境）。

#### 4.1.5 小练习与答案

**练习 1**：`update()` 为什么先 erase 已完成任务、再计算 `r` 补位，而不是反过来？

**答案**：先回收再补位，才能保证 `execTasks_.size()` 反映的是「扣除已死后」的真实占用。若先补位，已完成但尚未清理的任务会虚占名额，导致新任务明明有空位却进不去，并发上限被隐性压低。

**练习 2**：一个任务 `startup()` 之后立刻 `finished()` 返回 true，它会不会占用 `execTasks_` 名额？

**答案**：不会。[src/DHTTaskExecutor.cc:L70-L72](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTTaskExecutor.cc#L70-L72) 只把 `!finished()` 的任务 push 进 `execTasks_`；「秒完成」的任务（如路由表为空时的 NodeLookup）启动即出队，不消耗并发额度，while 循环可以继续启动下一个。

**练习 3**：如果把 `NUM_CONCURRENT_TASK` 从 15 改成 1，最直接的影响是什么？

**答案**：每条泳道每轮最多只有 1 个任务在执行，其余全部排队。自举阶段（要 ping 多个 entry point + 首次 NodeLookup）会被严重拉长；但不会死锁——因为任务的推进靠响应回调而非 executor 反复调用，单个任务仍能正常收敛，只是任务间变成了串行。

### 4.2 DHTNodeLookupTask：迭代查找的收敛循环

#### 4.2.1 概念说明

`DHTNodeLookupTask` 要完成的事，用一句话说：**从路由表出发，一轮轮地问「谁离目标 ID 更近」，直到问不出更新的信息为止**。

它解决的问题是 Kademlia 的核心操作——lookup。两个典型用途：

- 目标是**某个节点 ID**（通常是本地 ID 或桶内随机 ID）：目的是「骚扰」目标附近的节点，让它们认识我们、把我们加入候选，从而**填充和刷新路由表**（桶刷新就是这么实现的）。
- 目标是**某个 infohash**：目的是找到**正在分享该种子的 peer**（这就是 `DHTPeerLookupTask`，见 4.2.6）。

aria2 的精妙之处在于：这两件事的**收敛循环完全相同**，只有「发什么消息」「响应里取什么」不同。于是把这个循环提炼成模板基类 `DHTAbstractNodeLookupTask<ResponseMessage>`，用 C++ 模板做静态多态，子类只需实现四个钩子。

#### 4.2.2 核心流程

整个循环维护三个状态：目标 ID `targetID_`、候选条目列表 `entries_`（按到目标的 XOR 距离升序）、在途消息计数 `inFlightMessage_`。

```text
startup():
    candidates ← 路由表中最接近 target 的 K 个节点
    若为空 → 立即 finished
    否则 sendMessage()

sendMessage():                        # α 并发窗口
    从队头扫描 entries_，取前 α 个「未询问过」的节点
    每个节点：inFlight++，标记 used，把 query 消息压入 dispatcher 队列

响应到达（tracker 配对成功后回调）onReceived(msg):
    inFlight--
    用响应节点的新 ID 替换本地旧记录          # 节点 ID 会变
    从响应提取「更近的节点」列表，插入 entries_ 头部
    stable_sort（按到 target 的 XOR 距离）→ 去重 → 截断到 K 个
    sendMessageAndCheckFinish()

onTimeout(node):                       # 对应 query 超时
    inFlight--
    从 entries_ 摘除该节点
    sendMessageAndCheckFinish()

sendMessageAndCheckFinish():
    若还能发 → sendMessage()            # 补满 α 窗口
    若 inFlight == 0 → onFinish()，任务 finished   # 终止不变量
```

终止不变量值得强调：任务结束的条件**不是**「候选列表全被问过」，而是「**没有在途消息且发不出新消息**」。当列表头部剩下的全是 used 节点时，`sendMessage()` 发不出任何消息，`inFlightMessage_` 一旦归零，任务即告完成——此时列表里恰是离目标最近的（≤ K 个）节点，这正是我们要的结果。

用数学语言描述收敛：设第 \( i \) 轮后候选列表中最远节点与目标的 XOR 距离为 \( d_i \)。每轮向最近的至多 \( \alpha \) 个节点询问，每个响应带来至多 \( K \) 个新候选，经排序截断后 \( d_{i+1} \le d_i \) 单调不增；由于节点总数有限，数列最终收敛到局部最小，查找停止。α 与 K 的乘积 \( \alpha \times K = 3 \times 8 = 24 \) 是每轮最多涌入的新候选规模上限。

#### 4.2.3 源码精读：模板基类的收敛循环

整个算法写在头文件里（模板的惯例）：

- [src/DHTAbstractNodeLookupTask.h:L65-L72](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTAbstractNodeLookupTask.h#L65-L72)：模板类的三个核心成员——`targetID_`（20 字节目标 ID）、`entries_`（候选条目双端队列，`DHTNodeLookupEntry` = 节点 + `used` 标志）、`inFlightMessage_`（在途计数）。
- [src/DHTAbstractNodeLookupTask.h:L145](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTAbstractNodeLookupTask.h#L145)：`ALPHA = 3`，与 [src/DHTBucket.h:L84](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.h#L84) 的 `K = 8` 呼应。
- [src/DHTAbstractNodeLookupTask.h:L83-L94](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTAbstractNodeLookupTask.h#L83-L94)：`sendMessage()` 的 α 窗口控制——遍历候选列表，跳过 `used` 的条目，凑够 `inFlightMessage_ < ALPHA` 就停。消息经 `getMessageDispatcher()->addMessageToQueue()` 压入发送队列（注意：不是直接发 UDP，真正发送在 `DHTInteractionCommand` 每圈末尾的 `sendMessages()`）。
- [src/DHTAbstractNodeLookupTask.h:L147-L165](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTAbstractNodeLookupTask.h#L147-L165)：`startup()`——用 `getRoutingTable()->getClosestKNodes()` 取种子候选（u7-l1 讲过的「先向下后向上」收集）；路由表为空则立即 `setFinished(true)`，这就是 4.1 习题 2 里「秒完成任务」的实例。
- [src/DHTAbstractNodeLookupTask.h:L167-L209](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTAbstractNodeLookupTask.h#L167-L209)：`onReceived()` 是收敛的核心，值得逐段读：
  - 第 169 行先 `--inFlightMessage_`；
  - 第 171-177 行用「IP + 端口」匹配，把响应方带回的**新节点 ID** 替换本地旧条目（DHT 节点重启后 ID 会变）；
  - 第 179-196 行调子类钩子 `getNodesFromMessage()` 从响应里取出更近节点（排除本地节点自己），插入列表头部；
  - 第 197-202 行 `stable_sort` + `unique`——按 `DHTIDCloser(targetID_)` 排序（u7-l1 的 XOR 距离比较器），去重防同一节点被多个响应重复报告；
  - 第 205-207 行截断到 `DHTBucket::K`（8 个）；
  - 第 208 行进入 `sendMessageAndCheckFinish()`。
- [src/DHTAbstractNodeLookupTask.h:L96-L113](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTAbstractNodeLookupTask.h#L96-L113)：`sendMessageAndCheckFinish()`——先尝试补发，然后检查终止不变量 `inFlightMessage_ == 0`；满足则 `onFinish()`（子类收尾钩子）+ `setFinished(true)`，executor 下一圈就会把它回收。
- [src/DHTAbstractNodeLookupTask.h:L211-L224](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTAbstractNodeLookupTask.h#L211-L224)：`onTimeout()`——在途计数减一、把失联节点从候选列表摘除（不重试），然后同样走 `sendMessageAndCheckFinish()`。**超时不会让查找失败**，只是候选池里少了一个选项。

#### 4.2.4 源码精读：find_node 特化

`DHTNodeLookupTask` 本体薄得惊人，四个钩子各有归宿：

- [src/DHTNodeLookupTask.cc:L48-L51](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTNodeLookupTask.cc#L48-L51)：构造函数只做一件事——把目标节点 ID 交给基类。
- [src/DHTNodeLookupTask.cc:L53-L59](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTNodeLookupTask.cc#L53-L59)：`getNodesFromMessage()` 从 `find_node` 响应（u7-l2 讲过的 `DHTFindNodeReplyMessage`）里取出 `nodes` 字段的紧凑节点列表。
- [src/DHTNodeLookupTask.cc:L61-L65](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTNodeLookupTask.cc#L61-L65)：`createMessage()` 用消息工厂造 `find_node` query，询问目标是 `getTargetID()`。
- [src/DHTNodeLookupTask.cc:L67-L70](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTNodeLookupTask.cc#L67-L70)：`createCallback()` 返回一个访问者对象（[src/DHTNodeLookupTaskCallback.h:L44-L61](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTNodeLookupTaskCallback.h#L44-L61)），它在 tracker 配对成功后被回调，最终转调 `task->onReceived()`；超时则转调 `task->onTimeout()`。**这就是 4.1 说的「任务推进靠响应回调」的接线方式**。

桶刷新是 NodeLookup 最大的「客户」：

- [src/DHTBucketRefreshTask.cc:L54-L77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketRefreshTask.cc#L54-L77)：`DHTBucketRefreshTask::startup()` 遍历所有桶，挑出 `needsRefresh()` 为真的（15 分钟未更新，见 [src/DHTConstants.h:L55-L59](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTConstants.h#L55-L59)），为每个桶在桶的 ID 范围内**随机**取一个目标 ID，造一个 `DHTNodeLookupTask` 投入 periodic1 泳道（第 74 行）。注意第 76 行：派生任务入队后，刷新任务自己立即 `setFinished(true)`——它是个一次性的「分裂繁殖者」，本体活不过一圈。

#### 4.2.5 源码精读：get_peers 特化（PeerLookup）与两者的目标差异

`DHTPeerLookupTask` 继承**同一个**模板基类，收敛循环一字不改，差异全在钩子里：

- [src/DHTPeerLookupTask.cc:L58-L64](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPeerLookupTask.cc#L58-L64)：构造函数把**种子的 infohash** 当作目标 ID（`getInfoHash`）——对 DHT 来说，infohash 与节点 ID 同为 160 位，距离度量通用。
- [src/DHTPeerLookupTask.cc:L74-L83](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPeerLookupTask.cc#L74-L83)：`onReceivedInternal()` 是 NodeLookup 没有的动作——get_peers 响应除了 `nodes`（继续收敛）还带 `values`（拥有该种子的 peer 地址列表），这里直接 `peerStorage_->addPeer()` 喂给 BT 的 PeerStorage（u6-l3 讲过的候选池），同时把对方发放的 **token** 存进 `tokenStorage_`（announce 时要用，见 u7-l2 的 token 防伪机制）。
- [src/DHTPeerLookupTask.cc:L85-L89](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPeerLookupTask.cc#L85-L89)：`createMessage()` 造的是 `get_peers` query 而非 `find_node`。
- [src/DHTPeerLookupTask.cc:L96-L121](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPeerLookupTask.cc#L96-L121)：`onFinish()` 在收敛结束后，向候选列表中最接近的至多 K 个**已响应**节点发送 `announce_peer`（携带各自 token 与本机 BT 监听端口）——宣告「我有/想要这个种子」，之后这些节点会把这个 infohash 记入各自的 PeerAnnounceStorage（u7-l5 详讲）。

两者的目标差异总结成表：

| 维度 | DHTNodeLookupTask | DHTPeerLookupTask |
|------|-------------------|-------------------|
| 目标 ID | 节点 ID（本地 ID 或桶内随机 ID） | 种子 infohash |
| 发出的 query | `find_node` | `get_peers` |
| 响应中取什么 | 只取 `nodes`（更近节点） | 取 `nodes` + `values`（peer）+ token |
| 副作用 | 沿途节点认识我们（刷新路由表） | peer 直接入 PeerStorage |
| 收尾动作 | 无（`onFinish` 为空） | 向 K 个最近节点 announce_peer |
| 泳道 | periodic1（内务） | periodic2（业务） |
| 触发者 | 桶刷新 / 自举 / BtPortMessage | DHTGetPeersCommand（下载缺 peer 时） |

#### 4.2.6 代码实践

**实践目标**：在真实运行中追踪一次 NodeLookup 的收敛过程，验证候选列表「膨胀→排序去重截断→归零」的生命周期。

**操作步骤**：

1. 用 debug 级别日志启动 DHT（无需真的下载，跑一个磁力链接或加 `--enable-dht=true` 的任意任务均可让 DHT 启动）：

   ```bash
   ./src/aria2c --enable-dht=true --dht-listen-port=6881 \
     -l dht-debug.log --log-level=debug "magnet:?xt=urn:btih:<任一真实 infohash>"
   ```

2. 在日志里依次抓三类行（均出自本讲读过的源码）：
   - `Dispating bucket refresh. targetID=...`（注意源码里就拼作 **Dispating**，少了个 t，见 [src/DHTBucketRefreshTask.cc:L72-L73](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketRefreshTask.cc#L72-L73)，grep 时照抄才能命中）——每次出现意味着一个桶派生了一个 NodeLookupTask；
   - `N node lookup entries added.` / `N node lookup entries are unique.`（[src/DHTAbstractNodeLookupTask.h:L195-L204](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTAbstractNodeLookupTask.h#L195-L204)）——观察 added 数通常大于 unique 数（去重生效），unique 稳定在 ≤ 8（截断生效）；
   - `Finished node_lookup for node ID ...`（[src/DHTAbstractNodeLookupTask.h:L102-L103](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTAbstractNodeLookupTask.h#L102-L103)）——一次收敛完成。
3. 把同一个 targetID 的三类行按时间排列，数一数从 `Dispating` 到 `Finished` 经历了几轮 `entries added`。

**需要观察的现象**：added 数逐轮递减（近处节点问过之后就问不出新面孔了）；unique 始终不超过 8；几轮之后出现 Finished。

**预期结果**：一次成功的桶刷新在 2~6 轮内收敛，全程秒级。轮数与网络规模相关，待本地验证。

#### 4.2.7 小练习与答案

**练习 1**：任一时刻，一个 lookup 任务在途的 query 最多有几条？依据是哪行代码？

**答案**：最多 α = 3 条。`sendMessage()` 的循环条件是 `inFlightMessage_ < ALPHA`（[src/DHTAbstractNodeLookupTask.h:L85-L86](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTAbstractNodeLookupTask.h#L85-L86)），每补发一条 `inFlightMessage_` 加一，到达 3 即停止；只有收到响应或超时（都会减一）后才可能再补。这是「α 并发」的严格上限实现。

**练习 2**：候选列表排序为什么用 `stable_sort` 而不是普通 `sort`？

**答案**：两个节点到同一目标的 XOR 距离几乎不会相等，但 `stable_sort` 保证**相等元素保持原有相对顺序**——即保持「先被响应 discovered 的在前」。这让 α 窗口每次选中的是「距离相同时更早发现」的节点，行为确定、可复现，也优先信任先响应（通常网络更近）的来源。普通 `sort` 对相等元素的顺序无保证，行为会因实现而异。

**练习 3**：为什么 `onTimeout()` 只是摘除候选节点，而不重发一次 query？

**答案**：Kademlia 的迭代查找天然容错——丢一个节点不影响收敛，剩余候选照样能推进到底；超时本身还是信号，tracker 会顺带对该节点做健康度记账（见 4.3），多次超时的节点会在路由表层被判坏淘汰。重发反而占用 α 窗口、拖慢整体收敛。这种「用冗余换简单」的设计贯穿 DHT 全部代码。

### 4.3 DHTMessageTracker：请求-响应配对与超时清理

#### 4.3.1 概念说明

UDP 是无连接的：发出十个 query，可能先回后到、只回一半、甚至混进不相干的包。u7-l2 讲过每个 query 自带 4 字节 transaction id（t），响应会原样带回——这就是配对的钥匙。`DHTMessageTracker` 就是那本「未决请求账本」：

- **登记**：每个 query 真正发出时，登记一条记录（transaction id + 目标地址 + 消息类型 + 回调 + 超时时限）。
- **配对**：收到一个响应包，用 (t, IP, port) 三元组查账，命中则取出回调并把响应交给它（最终落到某个 DHTTask 的 `onReceived`）。
- **清算**：每圈事件循环扫一遍账本，超期的条目作废，通知回调 `onTimeout`（落到任务的 `onTimeout`），并给目标节点的健康度记一笔「查询超时」。

为什么要三元组而不只用 transaction id？因为 tid 只有 4 字节，全网随机碰撞不可避免，还存在恶意伪造；加上「响应必须来自我们发往的那个 IP 和端口」这道地址门槛，等于给配对加了一层源验证。

#### 4.3.2 核心流程

```text
发送侧（每圈末尾 dispatcher_->sendMessages()）:
    对发送队列中每条消息:
        send() 成功 且 是 query → tracker_->addMessage(msg, timeout, callback)
        send() 抛异常(如 ICMP 不可达) 且 是 query
            → tracker_->addMessage(msg, 0秒, callback)   # 以 0 超时登记，下一圈必超时

接收侧（每圈收到 UDP 包后）:
    messageArrived(dict, ip, port):
        取 t 字段 → 遍历账本 entries_
        命中 (tid, ip, port) → 摘除该条目
            → factory_->createResponseMessage(...)       # 字典 → 响应消息对象(u7-l2)
            → 用条目的耗时更新节点 RTT
            → 若节点 ID 变了 → 从路由表摘除旧 ID
            → 返回 (响应消息, 回调)，上层执行 callback->visit(响应)

清理侧（每圈 receiver_->handleTimeout()）:
    遍历账本，isTimeout() 的条目:
        node->updateRTT(耗时); node->timeout()           # 健康度记账
        若 node->isBad() → routingTable_->dropNode(node) # 判坏出桶(u7-l1)
        callback->onTimeout(node)                        # 通知任务
        条目出账
```

#### 4.3.3 源码精读

**账本条目——配对与超时判定的最小单元**：

- [src/DHTMessageTrackerEntry.cc:L45-L56](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageTrackerEntry.cc#L45-L56)：构造时记下目标节点、transaction id、消息类型、回调，并把 `dispatchedTime_` 定为当前时钟——超时判定全靠这个时间戳。
- [src/DHTMessageTrackerEntry.cc:L58-L61](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageTrackerEntry.cc#L58-L61)：`isTimeout()` 就是「已过去时长 ≥ timeout」。
- [src/DHTMessageTrackerEntry.cc:L65-L82](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageTrackerEntry.cc#L65-L82)：`match()` 实现三元组比对——先比 tid 与端口，再比 IP；第 75-80 行的 `::ffff:` 分支是 IPv4-mapped IPv6 地址的兼容处理（同一个节点可能以两种文本形式出现，u2-l4 的双栈知识在这里落地）。

**登记点——dispatcher 在发送时向账本记账**：

- [src/DHTMessageDispatcherImpl.cc:L71-L99](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageDispatcherImpl.cc#L71-L99)：`sendMessage()` 对每条待发消息，第 74-79 行——`send()` 成功且**不是响应**（`isReply()` 为 false，即 query）时调 `tracker_->addMessage()` 登记；第 85-97 行——发送抛异常时以 `0_s` 超时登记，注释写得很清楚：不这么做，未记账的消息会让 `DHTAbstractNodeLookupTask` 这类任务**永远等不到归零**而无法结束。这是一个很好的防御性设计范例。
- [src/DHTMessageDispatcherImpl.cc:L101-L112](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageDispatcherImpl.cc#L101-L112)：`sendMessages()` 逐条发送并从队列头部清除已处理项，它由 [src/DHTInteractionCommand.cc:L149](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTInteractionCommand.cc#L149) 在每圈末尾调用——4.2 里 lookup 任务「压入 dispatcher 队列」的消息就是在这里真正变成 UDP 包的。

**配对——账本的查询路径**：

- [src/DHTMessageTracker.cc:L59-L66](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageTracker.cc#L59-L66)：`addMessage()` 造一个 entry 压入 `entries_`，仅此而已——登记是 O(1) 的尾部追加。
- [src/DHTMessageTracker.cc:L68-L117](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageTracker.cc#L68-L117)：`messageArrived()`——第 73-77 行先取 `t` 字段，缺失即视为畸形报文抛异常；第 80-112 行线性扫描账本调用 `entry->match(tid, ipaddr, port)`，命中后：摘除条目 → 用 `factory_->createResponseMessage()` 把 bencode 字典造成响应消息对象（u7-l2 的工厂）→ 第 91-95 行用条目存活时长算 RTT 并 `updateRTT` 回写节点 → 第 96-105 行若响应方节点 ID 与登记时不同，从路由表**摘除旧节点**（节点重启换了 ID，旧条目已无意义）→ 第 106 行连同回调一起返回。没命中则返回空对——迟到的响应（原条目已超时出账）会安静地走到这里，第 114 行的日志「Tracker entry not found.」就是证据。

**清算——超时的善后**：

- [src/DHTMessageTracker.cc:L119-L141](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageTracker.cc#L119-L141)：`handleTimeoutEntry()` 对单个超时条目依次做四件事：给节点记一次真实耗时 RTT、调 `node->timeout()` 记一次查询失败（u7-l1 讲过：连续 5 次即判坏）、`isBad()` 为真则 `routingTable_->dropNode()` 出桶、最后 `callback->onTimeout(node)` 通知等待方（最终落到 lookup 任务的 `onTimeout`）。整个函数包在 try/catch 里，单个条目的善后异常不影响其他条目。
- [src/DHTMessageTracker.cc:L143-L157](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageTracker.cc#L143-L157)：`handleTimeout()` 用 `remove_if` 扫全账，对每个 `isTimeout()` 条目调 `handleTimeoutEntry` 后移除。它的调用点在 [src/DHTInteractionCommand.cc:L147](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTInteractionCommand.cc#L147)——每圈事件循环必扫一次，所以超时精度就是事件循环的节拍（通常远小于 1 秒）。
- 超时时限的默认值来自 [src/DHTConstants.h:L52-L53](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTConstants.h#L52-L53)：`DHT_MESSAGE_TIMEOUT = 10_s`，注释指明可由 `--dht-message-timeout` 选项调整（u1-l5 的选项系统在此生效）。

至此可以回答本讲开篇的第二、三问：**配对靠 (t, IP, port) 三元组查账，超时靠每圈扫账**；而「任务推进靠回调」的完整闭环是——tracker 命中 → `popCallback` → `callback->visit(response)` → `task->onReceived()` → 任务发下一波消息。DHTTask、DHTMessageTracker、DHTTaskExecutor 三个模块正是靠这条闭环咬合成一台完整的「查找机器」。

#### 4.3.4 代码实践

**实践目标**：亲眼观察一次 DHT 消息超时的完整清理路径：账本出账 → 节点健康度记账 → （累计 5 次后）判坏出桶。

**操作步骤**：

1. 把超时窗口调小，让失联更容易观察：

   ```bash
   ./src/aria2c --enable-dht=true --dht-listen-port=6881 \
     --dht-message-timeout=3 -l dht-timeout.log --log-level=debug \
     "magnet:?xt=urn:btih:<任一真实 infohash>"
   ```

2. 在日志中抓两类行：
   - `Message timeout: To:<ip>:<port>`（[src/DHTMessageTracker.cc:L123-L124](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageTracker.cc#L123-L124)）——单条 query 超时；
   - `Marked bad: <ip>:<port>`（[src/DHTMessageTracker.cc:L129-L130](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageTracker.cc#L129-L130)）——某节点累计超时达到判坏阈值被出桶（阈值 5，u7-l1 已从 DHTNode 源码核实）。
3. 统计同一个 IP 出现 `Message timeout` 的次数，验证第 5 次之后紧跟 `Marked bad`。
4. 对照日志时间戳，验证 `Message timeout` 与对应 query 的 `Message sent`（[src/DHTMessageDispatcherImpl.cc:L79](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageDispatcherImpl.cc#L79)）之间恰好相差约 3 秒（我们设的 `--dht-message-timeout`）。

**需要观察的现象**：网络正常的节点不会出现 timeout 行；只有真正无响应的地址（不可达的 entry point、已下线的节点）才会连续出现，且同一 IP 的第 5 次超时后出现 `Marked bad`。

**预期结果**：超时清理的时间差符合选项设定，判坏计数为 5。在完全阻断 UDP 的环境下（防火墙 DROP），几乎每条 query 都会走 timeout 路径。具体日志形态待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么我们自己发出的**响应**（reply）不需要登记到 tracker？

**答案**：tracker 的职责是「等待配对的未决请求账本」。query 发出后我们要等对端的响应，所以要记账以便配对和超时管理；reply 是我们对别人 query 的回答，发出后不期待任何回报，没有「未决」可言。见 [src/DHTMessageDispatcherImpl.cc:L75-L78](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageDispatcherImpl.cc#L75-L78) 与 [L93-L96](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageDispatcherImpl.cc#L93-L96)，两处登记都以 `!isReply()` 为前提。

**练习 2**：发送失败的 query 为什么还要以 0 秒超时登记进 tracker，而不是直接丢弃？

**答案**：lookup 任务在发出消息时已经把 `inFlightMessage_` 加了 1，只有收到「响应或超时」通知才会减回。若消息因发送失败而凭空消失，任务永远收不到通知，`inFlightMessage_` 永不归零，任务无法 finished，executor 的并发名额被永久占用。以 0 秒超时登记，让它在下一圈 `handleTimeout()` 中走正常超时路径触发 `onTimeout` 回调——用一次「善意的谎言」保住状态机的闭环。源码注释（[src/DHTMessageDispatcherImpl.cc:L89-L93](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageDispatcherImpl.cc#L89-L93)）明确写了这个动机。

**练习 3**：一个响应包到达，但它在账本里找不到条目（"Tracker entry not found"），可能发生了什么？

**答案**：最常见的是**迟到**——原条目已在此前某圈因超时被 `handleTimeout()` 清算出账，响应随后才到；也可能是网络中的串扰包或伪造包（tid 碰巧撞上但 IP/port 不匹配）。aria2 的处理是安静丢弃（[src/DHTMessageTracker.cc:L114-L117](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageTracker.cc#L114-L117)），不产生任何副作用——DHT 的最终一致性容许这种浪费。

## 5. 综合实践

**任务：给三条泳道装上「心电图」，观察桶刷新的任务突发与超时回收。**

本实践把本讲三个模块串起来：改一行日志级别 + 补几行日志，观测 executor 的调度节奏，再用日志证据解释「任务从哪来、怎么消化、怎么死」。

**操作步骤**：

1. **加观测点**（以下均为示例代码，实验完成后请还原，不要提交）：
   - [src/DHTTaskExecutor.cc:L75-L77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTTaskExecutor.cc#L75-L77)：把 `A2_LOG_DEBUG` 升级为 `A2_LOG_INFO`，并在格式串里加上 `getNumConcurrent()`，改成类似：

     ```cpp
     // 示例代码：观测泳道负载
     A2_LOG_INFO(fmt("TaskExecutor: executing=%u, queued=%u, limit=%d",
                     static_cast<unsigned int>(getExecutingTaskSize()),
                     static_cast<unsigned int>(getQueueSize()),
                     getNumConcurrent()));
     ```

   - [src/DHTTaskQueueImpl.cc:L55-L63](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTTaskQueueImpl.cc#L55-L63)：把三行 DEBUG 升级为 INFO，并把文本改成 `Updating periodicTaskQueue1/2/immediate`——这样每行泳道日志能对上号。
2. **重新编译**：`make -j$(nproc)`（只需重编这两个 .cc，增量编译很快）。
3. **运行 40 分钟以上**（覆盖至少一次桶刷新检查周期）：

   ```bash
   ./src/aria2c --enable-dht=true --dht-listen-port=6881 \
     --dht-message-timeout=10 \
     -l dht-observe.log --log-level=info \
     "magnet:?xt=urn:btih:<任一真实 infohash>"
   ```

4. **画曲线**：从日志提取每分钟的 `executing` / `queued` 数值（`grep 'periodicTaskQueue1' -A1 dht-observe.log` 之类），画或手抄成时间序列。
5. **对照源码解释三个现象**：
   - **启动突发**：t≈0 时 periodic1 的 queued 冲高——对应 [src/DHTEntryPointNameResolveCommand.cc:L140-L144](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTEntryPointNameResolveCommand.cc#L140-L144) 的自举任务（NodeLookup + BucketRefresh）与若干 entry point ping 同时入队。
   - **周期性回潮**：每 15 分钟左右出现一次 queued 小峰——对应 [src/DHTBucketRefreshCommand.cc:L64-L67](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketRefreshCommand.cc#L64-L67) 每 5 分钟检查、[src/DHTBucketRefreshTask.cc:L54-L77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketRefreshTask.cc#L54-L77) 为每个 `needsRefresh()` 的桶各派生一个 NodeLookupTask（突发宽度 = 过期桶数量，上限 15 个并发消化）。
   - **超时回收**：突发后 executing 维持高位约 10 秒再回落——对应 `--dht-message-timeout=10` 内部分 query 等待超时，[src/DHTMessageTracker.cc:L143-L157](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageTracker.cc#L143-L157) 逐圈清算触发各任务的 `onTimeout`，`inFlightMessage_` 归零后任务 finished，被下一圈 `update()` 的第 56-58 行回收。

**预期结果**：三条泳道中 immediate 与 periodic2 大多数时间空转（executing=0, queued=0），periodic1 呈「平静—突发—消化」的周期形态；全部任务在每次突发后的几十秒内归零。曲线的具体峰值取决于网络环境与 dht.dat 状态，待本地验证。

**交付物**：一张时间-队列长度曲线图 + 一份不少于 300 字的现象解释，要求每条解释都能指到本讲引用过的具体源码行。

## 6. 本讲小结

- **DHTTask 是跨多轮 RPC 的长期目标，不是 Command**。executor 只做「启动（startup）+ 回收（finished）」，任务的日常推进靠响应回调驱动；`DHTTaskQueueImpl` 用 periodic1（路由表内务）/periodic2（下载业务）/immediate（即时回应）三条泳道分开限流，每条泳道并发上限 15。
- **`DHTTaskExecutor::update()` 三步走**：先 erase 已完成任务、再算空位、然后逐个 startup 队首任务；秒完成的任务不占并发名额。所有泳道由常驻的 `DHTInteractionCommand` 每圈驱动一次。
- **迭代查找的收敛循环全部写在模板基类 `DHTAbstractNodeLookupTask`**：候选列表以 XOR 距离排序、去重、截断到 K=8，α=3 控制在途消息窗口，终止不变量是「在途消息归零」；超时只是摘除候选，不重试。`DHTNodeLookupTask`（find_node，刷新路由表）与 `DHTPeerLookupTask`（get_peers，找 peer + announce）复用同一循环，只差四个钩子。
- **DHTMessageTracker 是未决请求的账本**：query 真正发出时以 (transaction id, IP, port) 三元组登记；响应到达时查账配对、造响应消息、回写 RTT、处理节点 ID 变更；发送失败的 query 以 0 秒超时补登记，防止任务永不结束。
- **超时清理与路由表健康度是同一套机制的两面**：每圈扫账，超时条目给节点记一次失败（连续 5 次判坏出桶），并回调任务的 `onTimeout` 维持查找循环的状态闭环。

## 7. 下一步学习建议

本讲补齐了 DHT 的「动力系统」，至此路由表（u7-l1）、报文（u7-l2）、任务（本讲）三块底盘已经齐了。接下来：

- **u7-l4（DHT 启动与周期命令）**：看 DHTSetup 如何把本讲的 taskQueue、tracker、dispatcher、factory 装配成完整子系统，DHTEntryPointNameResolveCommand 如何自举、DHTBucketRefreshCommand 与 DHTAutoSaveCommand 如何维持周期节律——本讲表格里那些「入队调用点」会在那一讲里展开成完整生命周期。
- **u7-l5（get_peers 与 peer announce 存储）**：从 DHTGetPeersCommand 的视角看 DHTPeerLookupTask 如何为下载任务服务，以及查到 peer 之后 announce 信息如何被存储与分发。
- 若想横向对比，可回到 u6-l5（BtMessage 分发三件套）：BT 的 Receiver/Factory/Dispatcher 与 DHT 的 Tracker/Factory/Dispatcher 解决的是同构问题（配对、构造、收发），对照阅读能加深对「协议框架设计」的理解。
