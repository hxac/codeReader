# DHT 启动与周期命令：自举、刷新与保存

## 1. 本讲目标

前两讲（u7-l1～u7-l3）已经把 DHT 的静态结构（路由表）、线上字节（报文）和动力系统（任务队列）讲完了，但一直留着一个问题没有回答：**这套东西是何时、由谁、按什么顺序装配起来并开始运转的？进程退出后又如何把状态留下来？**

学完本讲，你应该能够：

1. 说出 `DHTSetup::setup` 的完整装配清单：它创建了哪些组件、如何互相接线、失败时如何整体回滚。
2. 跟踪 `dht.dat` 路由表文件的加载与失效重建流程：加载失败时本地节点 ID 如何产生、冷启动如何自举（bootstrap）。
3. 解释 `DHTEntryPointNameResolveCommand` 的「解析 → ping → 节点查找」三步曲，以及 entry point 解析失败时的兜底策略。
4. 读懂 `DHTInteractionCommand` 这个常驻命令的收发循环，理解它为什么同时服务 DHT 和 UDP tracker。
5. 区分 aria2 中两类周期命令的调度方式（routine/REALTIME 与 INACTIVE 每秒唤醒），并说出 BucketRefresh、AutoSave、TokenUpdate、PeerAnnounce 四个周期命令各自的节律与职责。
6. 手工解析 `dht.dat` 的二进制格式（56 字节文件头 + 每节点 56 字节记录）。

## 2. 前置知识

本讲默认你已读过 u7-l1～u7-l3。用到的几个概念先统一口径：

- **自举（bootstrap）**：Kademlia 网络没有中心服务器，一个全新节点要加入网络，必须先联系至少一个「已在网内」的节点，再通过迭代查找逐步认识更多节点。这个「第一个联系人」就是 entry point（入口节点）。冷启动的鸡生蛋问题全靠它解决。
- **dht.dat**：aria2 把路由表里的「好节点」列表和本地节点 ID 序列化成的二进制快照文件。下次启动时直接加载，免去重新自举（前提是这些节点还活着）。
- **Command 的两类去处**（承接 u2-l1/u2-l3）：
  - `DownloadEngine::addCommand` 进 `commands_` 队列，命令默认 `STATUS_INACTIVE`，只在主循环「每秒一次的全量刷新闸门」处被执行；
  - `addRoutineCommand` 进 `routineCommands_` 队列，每轮循环无条件执行。
- **TimeBasedCommand**：带时间间隔的命令骨架，每轮检查「距上次 process 是否已过 interval」，没到就什么都不做、把自己重新排队。
- **原子写**：先写 `<目标文件>__temp` 临时文件，写完 `rename` 覆盖原文件。读者要么看到旧文件、要么看到完整新文件，永远不会看到写了一半的文件。
- **好节点（good node）**：u7-l1 讲过 DHTNode 的好/可疑/坏三态。只有好节点才有资格被写进 dht.dat。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/DHTSetup.cc` | DHT 子系统总装车间：反序列化路由表、绑定 UDP 端口、创建并接线全部组件、产出 6 条命令、失败回滚 |
| `src/RequestGroup.cc`（节选） | DHTSetup 的唯一调用点：BT 任务创建初始命令时按 `--enable-dht`/`--enable-dht6` 触发装配 |
| `src/DHTEntryPointNameResolveCommand.cc` | 自举命令：解析 entry point 主机名 → 发 ping → 触发首次节点查找 |
| `src/DHTInteractionCommand.cc` | 常驻收发循环：UDP socket 读事件驱动，处理 DHT 报文与 UDP tracker 报文 |
| `src/TimeBasedCommand.cc` / `.h` | 周期命令骨架（preProcess/process/postProcess + interval） |
| `src/DHTBucketRefreshCommand.cc` | 周期命令：每 5 分钟检查并派发桶刷新任务 |
| `src/DHTAutoSaveCommand.cc` | 周期命令：每 30 分钟及退出时保存路由表 |
| `src/DHTTokenUpdateCommand.cc` / `src/DHTPeerAnnounceCommand.cc` | 另外两个周期命令：更新 token 秘密、清理过期 peer 宣告 |
| `src/DHTRoutingTableSerializer.cc` | dht.dat 的写入门面（本讲模块四） |
| `src/DHTRoutingTableDeserializer.cc` | dht.dat 的读入门面 |
| `src/DHTConstants.h` | 各周期常量（5 分钟、10 分钟、30 分钟等） |
| `src/DHTRegistry.h` | 进程级注册表：IPv4/IPv6 两套 Data 槽位 |
| `src/util.cc`（节选） | `getDHTFile`：dht.dat 默认路径的推导规则 |
| `src/OptionHandlerFactory.cc`（节选） | `--dht-entry-point`、`--dht-file-path`、`--dht-listen-port` 等选项定义 |

## 4. 核心概念与源码讲解

### 4.1 DHTSetup：DHT 子系统的总装车间

#### 4.1.1 概念说明

前几讲看到的路由表、报文工厂、任务队列都是「零件」。`DHTSetup::setup` 是唯一把它们组装成一台能运转机器的地方，它一次性完成：加载旧路由表 → 绑定 UDP 端口 → 创建 12 个组件并互相接线 → 产出 6 条命令 → 把组件挂进 `DHTRegistry`。任何一个环节抛异常，整个 DHT 功能就被干净地禁用（命令清单清空、注册表清空），下载本身不受影响——这是典型的「可选子系统降级」设计。

要注意它的触发时机：**DHTSetup 只在第一个 BitTorrent 任务的初始命令创建时被调用**（普通 HTTP 下载不会启动 DHT），且以 `DHTRegistry` 的 initialized 标志保证整个进程只装配一次。

#### 4.1.2 核心流程

```text
RequestGroup::createInitialCommand（BT 分支）
  └─ 检查 --enable-dht / --enable-dht6
      └─ DHTSetup().setup(e, AF_INET / AF_INET6)
          ① 已初始化？是 → 直接返回空
          ② 反序列化 dht.dat → 拿到 localNode + 旧节点列表
             失败 → localNode = 随机 ID 的新节点
          ③ 绑定 UDP 端口（复用 BtRegistry 已绑端口或按 --dht-listen-port）
          ④ 创建 12 个组件并互相 set 注入
          ⑤ 按 family 产出最多 6 条命令：
             - DHTEntryPointNameResolveCommand（有 entry point 才建）
             - DHTInteractionCommand          （唯一进 routine 队列的）
             - DHTTokenUpdateCommand          （10 分钟）
             - DHTBucketRefreshCommand        （5 分钟）
             - DHTPeerAnnounceCommand         （5 分钟）
             - DHTAutoSaveCommand             （30 分钟）
          ⑥ 旧节点灌入路由表 + 派发强制桶刷新任务
          ⑦ 组件 move 进 DHTRegistry（IPv4 或 IPv6 槽位）→ setInitialized(true)
          ⑧ 任何一步抛 RecoverableException → 清空命令 + clearData → DHT 禁用
```

#### 4.1.3 源码精读

**调用点：谁触发装配。** 在 `RequestGroup::createInitialCommand` 的 BT 分支里，把六件套注册进 BtRegistry 之后，按两个布尔选项分别对 IPv4/IPv6 各调一次 `setup`。返回的 `c`（普通命令）进 `commands_`，`rc`（routine 命令）进 `routineCommands_`：

[src/RequestGroup.cc:L350-L373](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L350-L373) —— `--enable-dht`（默认 true）触发 IPv4 装配；`--enable-dht6` 且未禁 IPv6 时触发 IPv6 装配。两次调用互不干扰，各自拥有独立的一套路由表与命令。

**入口守卫。** [src/DHTSetup.cc:L85-L93](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L85-L93) —— family 只接受 `AF_INET`/`AF_INET6`；对应注册表槽位已初始化则直接返回空 pair。这就是「整个进程只装配一次」的保证：第二个 torrent 到来时这里空手而归，后面所有命令复用 DHTRegistry 里的同一套组件。

**第一步：加载 dht.dat。** [src/DHTSetup.cc:98-L113](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L98-L113) —— 用 `DHTRoutingTableDeserializer` 读取 `--dht-file-path`（IPv6 用 `--dht-file-path6`）指向的文件。文件不存在或头部损坏会抛 `RecoverableException`，被就地捕获只记一条 ERROR 日志——**加载失败不是错误，只是「冷启动」**：`localNode` 为空时 new 一个随机 ID 的 `DHTNode`。你的节点 ID 就在这一刻诞生（或从 dht.dat 恢复）。

**第二步：绑定 UDP 端口。** [src/DHTSetup.cc:115-L139](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L115-L139) —— `DHTConnectionImpl` 封装了 UDP socket。端口选择遵循两条规则：若 `BtRegistry` 已记录 UDP 端口（比如 IPv4 先装配过，IPv6 紧随其后）就直接复用同号端口；否则解析 `--dht-listen-port`（默认 `6881-6999`）逐个尝试绑定。绑定失败抛 `DL_ABORT_EX`，进入最外层的「禁用 DHT」分支。[src/DHTConnectionImpl.cc:57-L86](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTConnectionImpl.cc#L57-L86) 是两个 `bind` 重载的实现。

**第三步：创建与接线。** [src/DHTSetup.cc:142-L183](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L142-L183) —— 12 个组件在 142-152 行创建：`DHTMessageTracker`（未决请求账本，u7-l3）、`DHTRoutingTable`（u7-l1）、`DHTMessageFactoryImpl`（u7-l2）、`DHTMessageDispatcherImpl`/`DHTMessageReceiver`（收发）、`DHTTaskQueueImpl`/`DHTTaskFactoryImpl`（u7-l3）、`DHTPeerAnnounceStorage`/`DHTTokenTracker`（u7-l5 预告）、`UDPTrackerClient`（u6-l7 讲过它随 DHT 一起启用）。156-183 行是一长串 `setXxx` 互相注入——注意 153 行的 `--dht-message-timeout`（默认 10 秒）同时喂给了 dispatcher 和 taskFactory，作为所有 RPC 的超时基准。

**第五步：产出 6 条命令。**

- [src/DHTSetup.cc:185-L209](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L185-L209) —— 仅当 `--dht-entry-point` 非空才创建 `DHTEntryPointNameResolveCommand`，并打上 `setBootstrapEnabled(true)` 标记（4.2 节详述）；否则只留一条 INFO 日志 "No DHT entry point specified."。
- [src/DHTSetup.cc:210-L219](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L210-L219) —— `DHTInteractionCommand` 是六条中唯一进 **routine 队列**的命令，它拿走了 `connection` 的所有权（`std::move`），此后 UDP socket 的生命周期由它托管。
- [src/DHTSetup.cc:220-L246](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L220-L246) —— 依次创建 TokenUpdate（`DHT_TOKEN_UPDATE_INTERVAL`）、BucketRefresh（`DHT_BUCKET_REFRESH_CHECK_INTERVAL`）、PeerAnnounce（`DHT_PEER_ANNOUNCE_CHECK_INTERVAL`）、AutoSave（硬编码 `30_min`）四条周期命令，全部进普通 `commands_` 队列。各间隔常量见 [src/DHTConstants.h:53-L67](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTConstants.h#L53-L67)。

**第六步：旧节点灌入 + 强制刷新。** [src/DHTSetup.cc:247-L257](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L247-L257) —— 把 deserializer 读出的节点逐个 `routingTable->addNode`（桶会按 u7-l1 的规则自动分裂重建），只要列表非空就创建一个 `DHTBucketRefreshTask` 并 `setForceRefresh(true)` 塞进 periodic1 泳道。强制刷新的意思是：不看「桶是否 15 分钟未刷新」的门槛，对所有桶一律发起节点查找（见 [src/DHTBucketRefreshTask.cc:54-L77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketRefreshTask.cc#L54-L77) 的 `forceRefresh_` 判断）。这很必要：dht.dat 里的地址可能早已失效，必须挨个 ping 验活。

**第七步：挂进注册表。** [src/DHTSetup.cc:259-L289](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L259-L289) —— 按 family 把 9 个组件 move 进 `DHTRegistry` 的对应 Data 槽（结构见 [src/DHTRegistry.h:54-L107](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRegistry.h#L54-L107)），IPv4 分支额外把 `UDPTrackerClient` 交给 BtRegistry，最后才 `setInitialized(true)`——顺序刻意放在最后，「避免异常打断初始化」（注释原话），并且此时才把实际绑定的端口号回写 BtRegistry。

**失败回滚。** [src/DHTSetup.cc:291-L305](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L291-L305) —— 上述任何一步抛 `RecoverableException`：两个命令容器 `clear()`（unique_ptr 析构连带销毁已创建命令），`DHTRegistry::clearData()` 清空槽位，UDP tracker 客户端置空。用户只会在日志里看到一条 "DHT is disabled"，下载继续。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：把 DHTSetup 的装配清单画成一张依赖图，验证「12 个组件 + 6 条命令」的数字。
2. **操作步骤**：
   - 打开 [src/DHTSetup.cc:142-L183](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L142-L183)，逐个列出 `make_unique`/`make_shared` 创建的对象；
   - 对每个 `setXxx` 画一条「谁注入谁」的箭头；
   - 用 `grep -n "make_unique<DHT\|make_shared<DHT" src/DHTSetup.cc` 核对总数。
3. **需要观察的现象**：接线图里 `routingTable` 被注入了几个对象（tracker、receiver、taskFactory、factory、entryPoint 命令、bucketRefresh 命令……）；`localNode` 又被注入了几个。
4. **预期结果**：能解释为什么 `taskFactory` 需要同时拿到 localNode、routingTable、dispatcher、factory、taskQueue 五样东西（造任务时都要塞进任务里，见 u7-l3 的 `setCommonProperty` 同款思路）。待本地验证：若你数出的组件数与本讲不符，以源码为准并回查是哪个版本差异。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `DHTSetup::setup` 的返回值是「两个命令列表」的 pair，而不是让 setup 自己调用 `e->addCommand`？

**答案**：让调用方（RequestGroup）决定命令的去向——事实上调用方也只是原样搬运（普通命令 `addCommand`、routine 命令 `addRoutineCommand`）。但这种「产出资源、由调用方注入」的写法与 DHTSetup「纯装配、无副作用于引擎」的定位一致，便于测试与复用；更重要的是失败回滚时只需 `clear()` 本地容器，未进引擎的命令不会被执行。

**练习 2**：两个 torrent 同时下载，第二个 torrent 的初始命令创建时会发生什么？

**答案**：`DHTSetup::setup` 在入口守卫处发现 `DHTRegistry::isInitialized()` 已为 true，直接返回空 pair（[src/DHTSetup.cc:L89-L93](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L89-L93)），不重复绑定端口、不重复建命令。两个种子共享同一张路由表、同一个 taskQueue；种子专属的部分（get_peers 任务）在各自下载时才向队列里加。

**练习 3**：IPv6 DHT 装配时如何避免与 IPv4 抢端口？

**答案**：`e->getBtRegistry()->getUdpPort()` 若非 0 则直接用同号端口绑定（[src/DHTSetup.cc:L118-L134](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L118-L134)）。IPv4 与 IPv6 是不同协议族，同号端口互不冲突；源码注释还类比了 BtSetup.cc 中 TCP 侧的同样做法。首次装配完成后才把实际端口回写 BtRegistry（[src/DHTSetup.cc:L286-L289](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L286-L289)）。

### 4.2 自举三步曲：DHTEntryPointNameResolveCommand

#### 4.2.1 概念说明

新节点的路由表是空的，唯一能写的联系人就是 `--dht-entry-point=HOST:PORT` 指定的入口节点。`DHTEntryPointNameResolveCommand` 是一条**一次性**命令（不是周期命令），生命周期通常只有几百毫秒，干三件事：

1. **解析**：把 HOST 解析成 IP（可同步可异步）；
2. **ping**：对解析成功的主机各发一个 ping 任务（超时 10 秒）；
3. **节点查找**：若至少一个主机解析成功且 `bootstrapEnabled_`，就派发两个任务——以**自己节点 ID 为目标**的 node lookup（find_node），以及一次桶刷新。前者是经典的 Kademlia 自举：向入口节点问「谁离我这个 ID 最近」，然后滚雪球。

注意它持有的 `taskQueue_`/`taskFactory_` 等指针来自 DHTSetup 刚创建的组件，任务真正执行要等 4.3 节的 InteractionCommand 开始转起来——装配期只是「下单」，收发循环才是「厨房」。

#### 4.2.2 核心流程

```text
execute()
  ├─ 下载已结束/已请求停止？→ return true（退出）
  ├─ 异步 DNS 分支（ENABLE_ASYNC_DNS 且 --async-dns=true）：
  │    对每个 entry point：
  │      resolveHostname → -1 失败 / 0 进行中 / 1 成功
  │      0 → addCommand(self) 重排自己，return false（下轮再看）
  │      1 → numSuccess++ → addPingTask(IP, PORT)
  │     同步分支：NameResolver 阻塞解析，失败仅记日志
  └─ bootstrapEnabled_ && numSuccess > 0？
       → addPeriodicTask1(createNodeLookupTask(自己的ID))
       → addPeriodicTask1(createBucketRefreshTask())
  return true（使命完成，退出）
```

#### 4.2.3 源码精读

**构造与异步解析准备。** [src/DHTEntryPointNameResolveCommand.cc:59-L80](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTEntryPointNameResolveCommand.cc#L59-L80) —— 构造时按 family 配置 `AsyncNameResolverMan`（u2-l4 讲过的双栈异步解析器），IPv4 模式只开 v4、IPv6 模式只开 v6——DHT 报文永远走单一协议族。

**异步解析的三值轮询。** [src/DHTEntryPointNameResolveCommand.cc:95-L116](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTEntryPointNameResolveCommand.cc#L95-L116) —— 这是 u2-l4「自旋加三值状态」模式的又一次落地：`rv == 0` 表示还没解析完，命令把自己重新 `addCommand` 排队、`return false` 保命；`rv == 1` 成功则记数并 `addPingTask`；`rv == -1` 失败只记 INFO 日志（`MSG_NAME_RESOLUTION_FAILED`），**继续处理下一个 entry point，绝不抛异常**。[src/DHTEntryPointNameResolveCommand.cc:164-L194](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTEntryPointNameResolveCommand.cc#L164-L194) 是 `resolveHostname` 对三值的判定细节。

**同步退路。** [src/DHTEntryPointNameResolveCommand.cc:119-L139](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTEntryPointNameResolveCommand.cc#L119-L139) —— 未编译 c-ares 或关闭 `--async-dns` 时用阻塞的 `NameResolver`（socket 类型限定为 `SOCK_DGRAM`，即只要 UDP 能用的地址），单个失败同样只记日志。

**自举任务派发。** [src/DHTEntryPointNameResolveCommand.cc:140-L149](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTEntryPointNameResolveCommand.cc#L140-L149) —— 关键门槛：`bootstrapEnabled_ && numSuccess_` 两个条件同时成立才发起 node lookup + 桶刷新。注意外层的 try/catch：即使派发时抛异常也只记日志，命令照样 `return true` 退场——自举是「尽力而为」。

**ping 任务的构造。** [src/DHTEntryPointNameResolveCommand.cc:152-L160](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTEntryPointNameResolveCommand.cc#L152-L160) —— 为入口地址造一个临时 `DHTNode`（随机 ID，仅当联系人用），`createPingTask(entryNode, 10)` 的第二个参数是 10 秒超时。ping 的意义不只是「打招呼」：u7-l3 讲过 MessageTracker 收到响应会喂路由表健康度，入口节点由此进入候选池，成为后续 node lookup 的起点。

**第二来源：种子自带的 nodes。** [src/RequestGroup.cc:374-L397](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L374-L397) —— `.torrent` 文件根字典里的 `nodes` 列表（u6-l1 提过）会被包装成**另一个** `DHTEntryPointNameResolveCommand`（每个 BT 任务创建时都会发一次），但它**没有** `setBootstrapEnabled(true)`——只 ping、不触发 node lookup。因为这批节点只是「补充联系人」，真正的自举（如果需要）已由 DHTSetup 那条命令完成。

**兜底策略总结**（对应学习目标 3）：entry point 解析失败时的回退链是——

1. dht.dat 里的旧节点仍在（4.1 第六步的强制桶刷新会验活它们）→ 网络照常可用；
2. 种子 `nodes` 列表 → ping 后进入路由表；
3. 两者皆无且解析全失败 → **aria2 不报错**，DHT 空转（没有任何已知节点，get_peers 无从发起），BT 下载仍可走 tracker/PEX。这就是「No DHT entry point specified.」之后系统的真实状态。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看到 entry point 解析成功与失败两种走向的差异。
2. **操作步骤**（需要一个能启动 BT 任务的 aria2，任选一个你有权下载的 torrent/magnet）：
   - 先把路由表指到干净路径：`--dht-file-path=/tmp/dht-test.dat`（确保冷启动）；
   - 运行 A：`./src/aria2c --enable-dht=true --dht-entry-point=nonexistent.invalid:6881 -l dht-a.log --log-level=info <你的torrent>`；
   - 运行 B：把 entry point 换成一个可解析的主机名（公共 DHT 路由节点，例如 `router.bittorrent.com:6881`，其当前可用性需自行验证），其余参数相同，日志存 `dht-b.log`；
   - 对比两份日志中的 `Name resolution` 相关行，以及是否出现 bucket refresh / node lookup 引发的 "Message received" 行。
3. **需要观察的现象**：A 中出现 name resolution failed 且此后没有任何 DHT 报文往来；B 中解析完成后很快出现成串的 "Message received: ..."（[src/DHTMessageReceiver.cc:123](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageReceiver.cc#L123)，INFO 级即可见）。
4. **预期结果**：验证 4.2.3 的兜底结论——解析失败不致命，只是 DHT 保持空转。若你的 aria2 未编译异步 DNS，日志形态略有不同（同步分支）。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 node lookup 的目标是「自己的节点 ID」而不是随机 ID？

**答案**：Kademlia 自举的目的是让路由表尽快覆盖**自己附近**的 ID 空间——路由表按 XOR 距离分桶，离自己越近的桶分得越细（u7-l1）。查找自己的 ID 会把「离我最近的 K 个节点」逐轮挖出来，正好填满最细的那些桶；而这些桶也是将来别人找到我、我被 announce 的路径。随机 ID 则只会填充远处某个粗桶。（桶刷新任务倒是故意用桶内随机 ID，见 [src/DHTBucketRefreshTask.cc:63-L65](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketRefreshTask.cc#L63-L65)。）

**练习 2**：`bootstrapEnabled_` 为 false 的实例（种子 nodes 那条命令）如果解析全部成功，路由表会有什么变化？

**答案**：只多了几个被 ping 过的入口节点（进入候选池/桶），不会触发以自身 ID 为目标的迭代查找；网络扩张只能靠后续周期性桶刷新和正常报文往来慢慢完成。

**练习 3**：这条命令是周期命令吗？它如何处理「DNS 还没解析完」的情况？

**答案**：不是。它是普通 Command，靠 `e_->addCommand(std::unique_ptr<Command>(this)); return false;` 自我重排（[src/DHTEntryPointNameResolveCommand.cc:100-L104](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTEntryPointNameResolveCommand.cc#L100-L104)）在主循环里自旋等待，符合 u2-l3「返回 false 必须已重排」的约定。

### 4.3 DHTInteractionCommand：常驻收发循环

#### 4.3.1 概念说明

DHTSetup 创建的六条命令里，只有这一条是**常驻**的：它持有 UDP socket 的所有权，把自己注册为该 socket 的**读事件回调**，然后每轮循环做「驱动任务 → 收包 → 分流 → 超时清理 → 发包」五件事。源码开头的 TODO 注释坦白了这个名字已经名不副实——它现在同时是 **DHT 收发循环**和 **UDP tracker 收发循环**（u6-l7 讲过两者共享同一个 UDP socket）。

它的调度身份也很特别：构造时 `setStatusRealtime()` 并被放进 routine 队列——即「每轮循环无条件执行、且不被状态过滤降级」的最高优先级常驻命令（对照 [src/Command.h:46-L52](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Command.h#L46-L52) 的状态枚举与 [src/DownloadEngine.cc:161-L178](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L161-L178) 的双队列执行点）。没有 UDP 数据时它跑得也很快：收包循环立即空转退出。

#### 4.3.2 核心流程

```text
execute()（每轮主循环都被调用）
  ├─ 退出判定：
  │    全部下载完成 / (停止请求 且 UDP tracker 无观察者) → return true
  │    强制停止 → udpTrackerClient_->failAll() → return true
  ├─ taskQueue_->executeTask()      ← 驱动三条泳道的任务执行器（u7-l3）
  ├─ 收包循环：while receiveMessage() > 0
  │    data[0] == 'd' ？  ← bencode 字典必以 'd' 开头
  │      是 → DHTMessageReceiver::receiveMessage()（解析+分发）
  │      否 → UDPTrackerClient::receiveReply()
  │            若是 announce 应答 → 唤醒对应的 TrackerWatcherCommand
  ├─ receiver_->handleTimeout()           ← DHT 未决请求超时
  ├─ udpTrackerClient_->handleTimeout()   ← UDP tracker 未决请求超时
  ├─ dispatcher_->sendMessages()          ← 发出队首到期的 DHT 报文
  ├─ while UDP tracker 有待发请求 → createRequest + sendMessage
  └─ e_->addRoutineCommand(this); return false   ← 自我重排，永不销毁（直到退出判定）
```

#### 4.3.3 源码精读

**构造与读事件注册。** [src/DHTInteractionCommand.cc:63-L93](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTInteractionCommand.cc#L63-L93) —— 构造即 `setStatusRealtime()`；`setReadCheckSocket` 把 UDP socket 与本命令一起交给引擎的读检查表（`e_->addSocketForReadCheck`），于是 EventPoll 一旦报告该 socket 可读，本命令的状态就被置 ACTIVE——不过对一条 REALTIME 例行命令来说，这只是让主循环的 `noWait` 判断更精确。析构函数对称地注销（RAII，见 75-76 行）。

**退出判定。** [src/DHTInteractionCommand.cc:95-L108](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTInteractionCommand.cc#L95-L108) —— 三种退场：全部下载完成；收到停止请求**且** UDP tracker 已无观察者（`getNumWatchers() == 0`）——这个附加条件就是「名字虽叫 DHT、命却是 UDP tracker 的」的直接证据：哪怕 DHT 该退了，只要还有 tracker announce 在等应答，它就得留任；强制停止时先把所有 UDP tracker 请求标记失败再走人。

**收包与一分之二。** [src/DHTInteractionCommand.cc:110-L148](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTInteractionCommand.cc#L110-L148) —— `receiveMessage` 非阻塞地收 UDP 报文（64 KiB 栈缓冲），收尽为止。分流的依据极其朴素：bencode 字典的第一个字节必是 `'d'`，而 UDP tracker 的应答（action 开头的二进制结构）不可能以 0x64 开头，于是 `data[0] == 'd'` 一刀切进 DHT、否则进 UDP tracker。两个分支的注释都标了 `nothrow`。收包之后依次做两个 `handleTimeout`（u7-l3：DHT 超时反哺路由表健康度）再 `sendMessages` 把到期报文发出去——**先收后发**，让响应能赶在同一轮发出。

**UDP tracker 发送与自排。** [src/DHTInteractionCommand.cc:150-L168](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTInteractionCommand.cc#L150-L168) —— 把 UDP tracker 的待发请求循环取出、编码、经同一个 socket 发出，发送失败按 `UDPT_ERR_NETWORK` 记账。最后一行 `addRoutineCommand(this)` + `return false` 是常驻命令的标准自排写法。

#### 4.3.4 代码实践（源码阅读型 + 日志观察）

1. **实践目标**：跟踪「一条 ping 响应从 UDP 字节到路由表健康度清零」的完整路径，体会本命令在链路中的位置。
2. **操作步骤**：
   - 从 [src/DHTInteractionCommand.cc:122-L126](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTInteractionCommand.cc#L122-L126) 的 `'d'` 分支进入 `DHTMessageReceiver::receiveMessage`（u7-l2/l3 讲过它调 factory 造消息、MessageTracker 配对 transaction id）；
   - 继续追到响应消息的 `doReceivedAction` → 路由表 `markGood`/`notifyUpdate`；
   - 运行期用 `--log-level=info -l dht.log` 数 "Message received:" 行（INFO 级），感受节律。
3. **需要观察的现象**：每条 "Message received" 行的 `toString()` 里带 transaction id 与远端地址；ping 响应的到达紧跟在一次 ping 发送之后。
4. **预期结果**：能口头复述「EventPoll 读事件 → 本命令 execute → receiveMessage → MessageTracker 配对 → 消息对象回调 → 路由表更新」这条链，并指出每一环所在的源码文件。待本地验证（依赖真实网络的 DHT 流量）。

#### 4.3.5 小练习与答案

**练习 1**：为什么用首字节 `'d'` 而不是端口号或报文长度来区分 DHT 与 UDP tracker？

**答案**：两者共享同一个 UDP socket、来自同样的远端地址端口组合，无法按地址区分；长度上也没有可靠界线。bencode 字典首字节恒为 `'d'`（0x64），而 UDP tracker 应答前 4 字节是 action 整数（0/1/2/3），恰好构成一个零成本的判别位。这是「协议巧合当分发器」的经典例子——源码注释也明说了这一假设。

**练习 2**：如果去掉 `taskQueue_->executeTask()` 这一行，u7-l3 里哪些机制会停摆？

**答案**：三条泳道（periodic1/periodic2/immediate）的 `DHTTaskExecutor::update()` 都不会被驱动——任务不会被启动（`startup()` 不被调）、完成的任务不会被回收；于是自举 node lookup、桶刷新、get_peers、announce_peer 以及对收到的 get_peers/announce_peer 的即时回应（immediate 泳道）全部停摆。只剩无任务的裸收发。参见 [src/DHTTaskQueueImpl.cc:55-L62](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTTaskQueueImpl.cc#L55-L62)。

**练习 3**：这条命令与 4.4 节的周期命令在调度上的两个本质区别是什么？

**答案**：① 队列与过滤：本命令在 `routineCommands_` 队列且 REALTIME，每轮无条件执行；周期命令在 `commands_` 队列且默认 INACTIVE，只在每秒一次的全量刷新闸门被唤醒（对照 [src/DownloadEngine.cc:172-L177](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L172-L177)）。② 驱动来源：本命令由 UDP 读事件与任务回调驱动、节奏无上限；周期命令由 `TimeBasedCommand` 的 interval 时钟驱动，节奏封顶为每 interval 一次。

### 4.4 周期命令家族：TimeBasedCommand 模式与两个代表

#### 4.4.1 概念说明

DHT 有四件「不急但必须定期做」的事，aria2 全部套进 `TimeBasedCommand` 骨架：

| 命令 | 间隔常量 | 默认间隔 | process() 干什么 |
| --- | --- | --- | --- |
| `DHTTokenUpdateCommand` | `DHT_TOKEN_UPDATE_INTERVAL` | 10 分钟 | 轮换 token 秘密（防伪造 announce，u7-l5） |
| `DHTBucketRefreshCommand` | `DHT_BUCKET_REFRESH_CHECK_INTERVAL` | 5 分钟 | 派发桶刷新任务 |
| `DHTPeerAnnounceCommand` | `DHT_PEER_ANNOUNCE_CHECK_INTERVAL` | 5 分钟 | 清理过期的 peer 宣告存储 |
| `DHTAutoSaveCommand` | 构造参数 `30_min` | 30 分钟 | 序列化路由表到 dht.dat |

常量定义集中在 [src/DHTConstants.h:53-L67](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTConstants.h#L53-L67)。

`TimeBasedCommand` 是模板方法模式的又一实例（u2-l3 的 AbstractCommand 同款思路）：基类的 `execute()` 固定骨架，子类只覆写三个钩子——`preProcess()`（每轮都调）、`process()`（到点才调）、`postProcess()`（每轮都调）。四条命令的共同节律是：`preProcess` 里检查「下载是否结束/停止」，是则 `enableExit()` 让 `execute` 返回 true 自我销毁。

#### 4.4.2 核心流程

```text
TimeBasedCommand::execute()
  ├─ preProcess()                    ← 子类：该退场了吗？
  ├─ exit_？ → return true（销毁）
  ├─ 距 checkPoint_ 已过 interval_？
  │    是 → checkPoint_ = now; process()   ← 子类的正事
  │         exit_？ → return true
  ├─ postProcess()
  └─ routineCommand_ ? addRoutineCommand(self) : addCommand(self)
       return false（自排）
（四条 DHT 周期命令都用默认 routineCommand=false → 进 commands_ 队列，
  状态默认 INACTIVE → 每秒只被唤醒一次，平时不占主循环）
```

#### 4.4.3 源码精读

**骨架。** [src/TimeBasedCommand.cc:55-L79](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TimeBasedCommand.cc#L55-L79) —— 注意 61 行的到点判断和 62 行「先刷新 checkPoint 再干活」，以及 72-77 行按 `routineCommand_` 选择队列。三个钩子的语义契约写在 [src/TimeBasedCommand.h:71-L90](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TimeBasedCommand.h#L71-L90)。命令默认 `STATUS_INACTIVE`（[src/Command.cc:40-L48](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Command.cc#L40-L48)），因此它们挂在 `commands_` 队列里时，只会在主循环每秒一次的 `executeCommand(commands_, STATUS_ALL)` 分支被真正执行——这正是 u2-l1 讲过的「睡眠者唤醒」机制，周期命令是它最重要的客户。

**BucketRefresh。** [src/DHTBucketRefreshCommand.cc:56-L67](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketRefreshCommand.cc#L56-L67) —— `process()` 只有一行：向 periodic1 泳道加一个 `DHTBucketRefreshTask`。注意它**不是**每 5 分钟刷一次桶：任务是「检查所有桶，对 15 分钟（`DHT_BUCKET_REFRESH_INTERVAL`）没动静的桶发起节点查找」（[src/DHTBucketRefreshTask.cc:54-L77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketRefreshTask.cc#L54-L77)，59 行的 `needsRefresh()` 门槛 + 63 行桶内随机目标 ID）。5 分钟是「检查频率」，15 分钟是「桶的新鲜度阈值」，两个数字别混。

**AutoSave。** [src/DHTAutoSaveCommand.cc:70-L79](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTAutoSaveCommand.cc#L70-L79) —— 与另外三条不同，它的 `preProcess` 在退场前**先保存再退出**：这就是「停机保存」路径。加上 `process()` 的每 30 分钟一次，保存共有两个触发时机。

**save() 的三部曲。** [src/DHTAutoSaveCommand.cc:81-L116](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTAutoSaveCommand.cc#L81-L116) —— ① 先删掉历史上一个 bug（1.10.4 曾把 `dht.dat__temp` 建成目录）可能遗留的临时路径，并 `mkdirs` 父目录；② 遍历所有桶，只收集 `getGoodNodes`——**可疑/坏节点一律不写盘**，文件里永远是「验证过的优质联系人」；③ 交给 serializer 落盘，异常只记 ERROR 不上抛（保存失败不影响下载）。

**另外两条（速览）。** [src/DHTTokenUpdateCommand.cc:62-L70](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTTokenUpdateCommand.cc#L62-L70) 每 10 分钟轮换 token 秘密；[src/DHTPeerAnnounceCommand.cc:63-L71](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPeerAnnounceCommand.cc#L63-L71) 每 5 分钟调 `handleTimeout` 清掉超过 `DHT_PEER_ANNOUNCE_PURGE_INTERVAL`（30 分钟）没人续期的 infohash 宣告（细节在 u7-l5）。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：验证「周期命令每秒被唤醒一次、但 process 只按 interval 触发」的调度模型。
2. **操作步骤**：
   - 在 [src/DownloadEngine.cc:168-L177](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L168-L177) 找到 `lastRefresh_` 闸门：满足条件走 `STATUS_ALL`，否则只跑 `STATUS_ACTIVE`；四条 DHT 周期命令默认 INACTIVE，所以只在闸门打开时执行；
   - 推算：BucketRefresh 两次 `process()` 之间的最小与典型间隔分别由哪两个数字决定；
   - （可选，需改源码重编译）在 `DHTBucketRefreshCommand::preProcess` 加一行计数日志，统计 60 秒内它被调用的次数。
3. **需要观察的现象**：理论上每秒一次 `execute`、每 300 秒一次 `process`。
4. **预期结果**：若做了可选步骤，60 秒内 preProcess 计数应约为 60（主循环节律），process 计数为 0 或 1。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 AutoSave 间隔是 30 分钟这么久？保存太频繁有什么坏处？

**答案**：路由表变化的主要受益者是「下次启动少走自举弯路」，分钟级的新鲜度足够；而每次保存都要遍历全部桶、写整份文件并 rename。更关键的是 dht.dat 并不要求精确——写入的都是好节点，30 分钟前的好节点现在大概率还是好的（DHT_NODE_CONTACT_INTERVAL 15 分钟才判可疑）。成本收益权衡下 30 分钟是保守选择。

**练习 2**：四条周期命令里哪条「多做一次」会造成网络流量？哪条不会？

**答案**：BucketRefresh 会——它派发的节点查找任务产生真实 UDP 报文（还占 periodic1 泳道的并发名额）；TokenUpdate、PeerAnnounce、AutoSave 都是纯本地操作（换秘密、清内存、写文件），不产生任何报文。

**练习 3**：`enableExit()` 之后命令还要等几次自身排队才会真正消失？

**答案**：当次 `execute()` 里 preProcess 置位后立即在 58-60 行返回 true，命令被引擎销毁，**不再**重新排队。若 exit_ 是在 `process()` 里置位的（64-66 行），同样当次返回 true。这也是四条命令统一在 preProcess 检查退出条件的原因——赶在干活前先看该不该走。

### 4.5 路由表序列化：dht.dat 的二进制格式

#### 4.5.1 概念说明

dht.dat 是「内存路由表」的磁盘投影，但**不是**逐字节拷贝：桶的树形结构不落盘，只写「本地节点 ID + 好节点扁平列表」。桶树在加载时由 `addNode` 的分裂规则确定性重建（u7-l1 讲过为什么重建是无损的：桶划分只由本地 ID 决定）。文件格式为固定头 + 定长节点记录，全部多字节整数使用网络字节序（大端），并对未来留了大量保留字节。

**默认路径的推导**（[src/util.cc:L1816-L1825](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L1816-L1825)）：先看 `~/.aria2/dht.dat`（IPv6 为 `dht6.dat`）是否存在——存在（旧版本遗留）就用它；否则用 XDG 缓存目录 `$XDG_CACHE_HOME/aria2/dht.dat`（未设置时为 `~/.cache/aria2/dht.dat`）。可用 `--dht-file-path` 覆盖（选项定义见 [src/OptionHandlerFactory.cc:1725-L1738](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L1725-L1738)）。

#### 4.5.2 核心流程

文件布局（v3 格式，IPv4 与 IPv6 通用，节点记录定长 56 字节）：

| 偏移 | 长度 | 含义 |
| --- | --- | --- |
| 0 | 8 | 头：魔数 `a1 a2`、格式 ID `02`、3 字节保留、版本 `00 03`（v2 兼容头为 `00 02`） |
| 8 | 8 | 保存时刻的 epoch 秒（大端 64 位；v2 为 32 位 + 4 字节保留） |
| 16 | 8 | 保留 |
| 24 | 20 | 本地节点 ID |
| 44 | 4 | 保留 |
| 48 | 4 | 节点数 N（大端 32 位） |
| 52 | 4 | 保留 |
| 56 | 56×N | N 条节点记录 |

每条节点记录（`clen` = 6 或 18，紧凑地址长度）：`1 字节 clen` + `7 字节保留` + `clen 字节紧凑 IP+端口` + `24−clen 字节保留` + `20 字节节点 ID` + `4 字节保留`——两种 family 下总长都恰为 56 字节。

```text
写盘（serialize）：
  写 <file>__temp → close → renameTo(<file>)     ← 原子替换
读盘（deserialize）：
  校验 8 字节头（v3 或 v2 兼容）→ 读时间/本地ID/节点数
  → 逐条读节点，三种脏数据就地跳过该条（继续读下一条）：
      clen 与本 family 不符 / 紧凑地址全零 / unpack 失败
```

#### 4.5.3 源码精读

**写文件头。** [src/DHTRoutingTableSerializer.cc:80-L104](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTableSerializer.cc#L80-L104) —— 先写 `filename + "__temp"`；8 字节头里魔数 `a1 a2`（aria2 的谐音）、格式 ID `02`、版本 `00 03`。`WRITE_CHECK` 宏（74-78 行定义）保证任何一次短写都抛异常。

**写本地节点与计数。** [src/DHTRoutingTableSerializer.cc:105-L121](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTableSerializer.cc#L105-L121) —— 时间戳用 `hton64` 转大端；本地节点 ID 占 20 字节（`DHT_ID_LENGTH`）；节点数 `htonl`。每段之间都垫保留字节，给未来格式演进留空间。

**写节点记录。** [src/DHTRoutingTableSerializer.cc:123-L149](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTableSerializer.cc#L123-L149) —— 地址用 BT 世界通用的紧凑格式（`packcompact`，u6 系列见过：IPv4 6 字节、IPv6 18 字节）。注意 133-135 行的容错：pack 失败（比如节点还没有合法地址）就写一段全零——读盘方会把它当脏数据跳过，等于「占位但无效」。

**原子收尾。** [src/DHTRoutingTableSerializer.cc:150-L158](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTableSerializer.cc#L150-L158) —— `close` 检查 EOF 后 `renameTo` 原子替换；成功打 INFO 日志 "DHT routing table was saved successfully"。

**读文件头与版本分叉。** [src/DHTRoutingTableDeserializer.cc:77-L127](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTableDeserializer.cc#L77-L127) —— 同时准备 v3 与 v2 两个样板头逐字节比对：匹配 v3 读 64 位时间戳、匹配 v2 读 32 位；都不匹配抛 "bad header"——这正是 4.1 节「加载失败→冷启动」的异常来源之一。

**逐条读节点与三种跳过。** [src/DHTRoutingTableDeserializer.cc:158-L201](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTableDeserializer.cc#L158-L201) —— 165-169 行：记录里的 clen 与当前 family 的紧凑长度不符（例如把 IPv4 的 dht.dat 当 IPv6 读）→ 按定长跳过整条；174-178 行：全零地址（写盘时的 pack 失败占位）→ 跳过；181-185 行：unpack 失败 → 跳过。**跳过不等于失败**：只要有一条合法记录文件就继续可用。最后 199-200 行把 localNode 与节点列表交还调用方（DHTSetup）。

#### 4.5.4 代码实践

1. **实践目标**：不借助 aria2，手工解析一份 dht.dat 的文件头，验证格式表。
2. **操作步骤**：
   - 先让 aria2 生成一份文件（任意一次启用了 DHT 的 BT 下载结束后，默认在 `~/.cache/aria2/dht.dat`，或你用 `--dht-file-path` 指定的路径）；
   - `xxd -l 56 <dht文件路径>` 查看前 56 字节；
   - 按格式表核对：字节 0-1 是否 `a1 a2`、字节 2 是否 `02`、字节 6-7 是否 `00 03`；偏移 48 起的 4 字节大端读出节点数 N（可用 `od -A d -t x1` 辅助）；
   - 用 `stat -c %s <文件>` 取文件大小，验证等于 `56 + 56×N`。
3. **需要观察的现象**：文件头各字段与格式表一一对应；字节数恰好是 56 的整数倍。
4. **预期结果**：节点数与你下载时的活跃程度相关（冷启动可能只有个位数）。若文件不存在，说明该次运行既没到 30 分钟也未触发停机保存路径（对照 4.4.3），可检查日志中是否有 "Saving DHT routing table" 行。待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么序列化只存「好节点扁平列表」，而不存桶的边界？

**答案**：桶边界不是独立数据——它由本地节点 ID 与插入序列经分裂规则唯一确定（u7-l1）。存边界既冗余又容易与节点列表不一致；只存节点 + 本地 ID，加载时重放 addNode 即可确定性重建整棵桶树。这是「存最小充分信息」的典型取舍。

**练习 2**：dht.dat 写到一半进程崩溃，下次启动会发生什么？

**答案**：几乎不会发生「写一半」：写的是 `__temp` 文件，最后一步才 rename 原子替换（[src/DHTRoutingTableSerializer.cc:150-L157](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTableSerializer.cc#L150-L157)）。最坏情形是 rename 前崩溃——旧文件完好，磁盘上多一个残留的 `__temp`（AutoSave 下次保存前会先删它，见 [src/DHTAutoSaveCommand.cc:87-L94](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTAutoSaveCommand.cc#L87-L94) 的注释）。

**练习 3**：把 IPv4 的 dht.dat 复制成 IPv6 的路径会怎样？

**答案**：Deserializer 会逐条发现 clen（6）与 IPv6 期望（18）不符而跳过所有节点记录，最终得到一个「只有本地 ID、节点为空」的空表（[src/DHTRoutingTableDeserializer.cc:164-L169](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTableDeserializer.cc#L164-L169)），DHTSetup 后续的强制桶刷新因 `desnodes` 为空而不派发——等于 IPv6 冷启动，且本地 ID 沿用了 IPv4 的（两个 family 共用 ID 没有正确性问题，但属巧合而非设计保证）。

## 5. 综合实践：亲手完成一次冷启动并画出增长曲线

本实践把四个模块串成一条时间线：**冷启动 → 自举 → 自我填充 → 保存**。

**准备**：

- 已按 u1-l2 编译好的 `./src/aria2c`；
- 一个你有权下载的 torrent 或 magnet（自建种子 + 任意公开 tracker 亦可）；
- 把路由表指到干净路径，排除旧数据干扰：`--dht-file-path=/tmp/dht-lab.dat`，并确认该文件不存在；
- 指定一个入口节点：`--dht-entry-point=<HOST>:6881`（公共 DHT 路由节点，可用性需自行验证；填错也能观察兜底行为）。

**步骤**：

1. 启动并记录完整日志：
   `./src/aria2c --enable-dht=true --dht-entry-point=<HOST>:6881 --dht-file-path=/tmp/dht-lab.dat --seed-time=0.01 -l /tmp/dht-lab.log --log-level=debug <你的torrent>`
   （`--seed-time` 设小是为了让进程在数十秒内自然结束，触发停机保存路径。）
2. 在日志里按时间顺序找出这些里程碑行，标注时间戳：
   - `Loading DHT routing table from /tmp/dht-lab.dat`（[src/DHTRoutingTableDeserializer.cc:79](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTableDeserializer.cc#L79)）及其随后的失败信息；
   - `Initialized local node ID=...`（[src/DHTSetup.cc:140-L141](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L140-L141)，DEBUG 级）——这就是你的随机节点 ID；
   - name resolution 的结果行（成功/失败，INFO 级）；
   - 第一条 `Message received:`（[src/DHTMessageReceiver.cc:123](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageReceiver.cc#L123)）——自举 ping 的应答到了；
   - `Dispating bucket refresh. targetID=...`（[src/DHTBucketRefreshTask.cc:72-L73](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketRefreshTask.cc#L72-L73)，源码原文如此拼写）；
   - `Saving DHT routing table to /tmp/dht-lab.dat.`（[src/DHTAutoSaveCommand.cc:85](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTAutoSaveCommand.cc#L85)）与 `DHT routing table was saved successfully`——**注意它出现在进程收尾阶段而不是整 30 分钟**，因为它走的是 4.4.3 的 preProcess 停机保存路径。
3. 绘制前 30 秒节点增长曲线：统计 DEBUG 行 `Added DHTNode.`（[src/DHTRoutingTable.cc:85](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTable.cc#L85)）的累积数量随时间的变化。可用一条命令取计数：
   `grep -n "Added DHTNode" /tmp/dht-lab.log | head -40`
   日志行自带时间戳前缀，逐行累积即得曲线；横轴时间、纵轴累计节点数。
4. 验证保存产物：用 4.5.4 的 `xxd -l 56` 方法解析 `/tmp/dht-lab.dat`，核对节点数 N 与你曲线的终值是否一致（理论上应当相等——只有「好节点」入盘，若曲线值更大，差额就是尚未验活的可疑节点，这正是本实践最有价值的观察点）。
5. 再跑一次相同命令（这次不删 dht.dat），对照日志差异：应看到 `DHT routing table was loaded successfully`、无自举三步曲、以及一次强制桶刷新。

**预期结果**：一条「先快后慢」的 S 形增长曲线（自举后的前几秒节点查找回报最密集），`Added DHTNode.` 增速随后衰减；停机时刻出现保存日志；第二次启动跳过自举。若网络环境限制了 UDP 出站，曲线可能为平线——此时把观察重点转向兜底路径的日志证据。待本地验证。

## 6. 本讲小结

- `DHTSetup::setup` 是 DHT 唯一的装配点，只在第一个 BT 任务初始化时执行一次：加载 dht.dat（失败即随机 ID 冷启动）→ 绑 UDP 端口 → 创建 12 个组件互相接线 → 产出 6 条命令 → 挂进 DHTRegistry；任何一步失败整体回滚、DHT 静默禁用。
- 自举是「解析 → ping → 以自身 ID 做 node lookup」三步曲，由一次性的 `DHTEntryPointNameResolveCommand` 完成；解析失败不致命，兜底链是「dht.dat 旧节点 → 种子 nodes 列表 → DHT 空转但下载不受影响」。
- `DHTInteractionCommand` 是六条命令中唯一的常驻 routine/REALTIME 命令，独占 UDP socket：每轮驱动任务队列 → 收包（首字节 `'d'` 分流 DHT 与 UDP tracker）→ 双超时清理 → 发包；它要等 UDP tracker 的观察者清零才肯退场。
- 四条周期命令（TokenUpdate 10 分钟 / BucketRefresh 检查 5 分钟 / PeerAnnounce 检查 5 分钟 / AutoSave 30 分钟）共用 `TimeBasedCommand` 骨架：preProcess 管退场、process 按间隔干活；它们默认 INACTIVE，靠主循环每秒一次的闸门唤醒。
- dht.dat 是「本地 ID + 好节点扁平列表」的最小充分快照：56 字节文件头 + 每节点 56 字节定长记录，写临时文件后原子 rename；桶树由 addNode 规则确定性重建，脏记录逐条跳过不算失败。

## 7. 下一步学习建议

本讲补完了 DHT 的「生命周期」一环，至此路由表（u7-l1）、报文（u7-l2）、任务（u7-l3）、装配与节律（本讲）四块拼图齐了。下一讲 **u7-l5《get_peers 与 peer announce 存储》** 讲 DHT 真正服务下载的部分：`DHTGetPeersCommand` 如何为一个种子发起 get_peers 查找、`DHTPeerAnnounceStorage` 如何记录「哪个 infohash 有哪些 peer」（本讲 4.4 出现过的 `DHTPeerAnnounceCommand` 的清理对象就在那里展开）、以及 `DHTTokenTracker` 的 token 校验（`DHTTokenUpdateCommand` 每 10 分钟轮换的秘密如何被验证）。

延伸阅读建议：把 [src/DHTSetup.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc) 与 [src/BtSetup.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtSetup.cc) 对照读——两套 Setup 的「创建组件 + set 注入 + 失败回滚」手法如出一辙，理解其一即可举一反三；再回头看 u2-l1 的主循环，确认你能指出 DHT 的每条命令分别挂在哪条队列、按什么频率被执行。
