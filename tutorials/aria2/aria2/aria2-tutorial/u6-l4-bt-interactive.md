# u6-l4 DefaultBtInteractive：与单个 peer 的交互循环

## 1. 本讲目标

上一讲（u6-l3）我们走完了 peer 连接建立与 68 字节握手：TCP 连上、握手消息通过校验、`Peer` 对象拿到 peer_id 与能力位之后，控制权被移交给了常驻命令 `PeerInteractionCommand`。从那一刻起，这条连接上发生的所有 BT 协议交互，都由一个叫做 `DefaultBtInteractive` 的对象全权负责。

学完本讲，你应该能够：

1. 说出 `PeerInteractionCommand` 与 `DefaultBtInteractive` 的分工：谁是命令层的「宿主」，谁是协议层的「大脑」。
2. 按顺序默写 `doInteractionProcessing()` 一次循环内的处理步骤：收消息 → 洪泛检测 → 执行 choke → 决定 interest → 广播 have → 发 keepalive → 生成请求 → 发 PEX → 统一发送。
3. 解释 keepalive 的触发时机、消息洪泛（flooding）检测的计数规则，以及「不活跃连接」的三条超时裁定。
4. 描述 `PeerStorage` 用哪三张表管理 peer 候选池，choke/unchoke 的「决策」与「执行」为什么会分离成两个组件。
5. 分析 `ActivePeerConnectionCommand` 在什么条件下补充新连接、每次补几个、以及为什么 0 连接时要缩短 tracker 的 announce 间隔。

## 2. 前置知识

### 2.1 回顾：BT 会话的四状态词汇

BT 协议里，连接双方各自维护四个布尔状态，两两配对：

| 状态 | 含义 | 谁设置 |
| --- | --- | --- |
| `amChoking` / `amInterested` | **我**是否掐住对方 / **我**是否对对方的数据感兴趣 | 本端 |
| `peerChoking` / `peerInterested` | **对方**是否掐住我 / **对方**是否对我感兴趣 | 对端（通过消息告知） |

只有「对方没掐我（`!peerChoking`）且我感兴趣（`amInterested`）」时，我才能向对方发 `request` 要数据。choke/unchoke 消息改变的是掐与不掐，interested/not_interested 改变的是兴趣声明。

### 2.2 回顾：命令的「自循环」

在 u2-l3 中我们学过：`Command::execute()` 返回 `false` 时必须把自己重新排回队列（`addCommandSelf`）。本讲的主角 `PeerInteractionCommand` 就是这样一个**常驻自循环命令**——它每被调度一次，就驱动一轮 `DefaultBtInteractive` 的交互处理，然后把自己放回队列等待下次调度。这就是本讲标题中「每 tick」的含义：一次 tick 等于引擎主循环对这条 peer 连接的一次驱动。

### 2.3 本讲需要的三个「节律器」概念

`DefaultBtInteractive` 内部没有线程、没有独立的循环线程，它的所有周期性行为（keepalive、洪泛检测、PEX 广播）都是**用 `Timer` 时间戳实现的软节律**：每次被 tick 到时检查「距上次动作是否超过了间隔」。理解了这一点，就能看懂为什么一个被动调度的对象可以表现出持续的心跳。

### 2.4 一个术语澄清

本讲规格中原定的实践切入点叫 `doReceivedHandshakeHandshake`——这个函数在当前源码中**并不存在**（它是大纲草案阶段的笔误）。握手之后真正的交互主循环入口是 [`DefaultBtInteractive::doInteractionProcessing()`](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L531-L587)，本讲的实践任务会修正到这个真实函数上。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/BtInteractive.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtInteractive.h#L46-L72) | 交互对象的纯虚接口（12 个方法） |
| [src/DefaultBtInteractive.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.h#L66-L147) | 默认实现类声明；`FloodingStat` 洪泛计数器；五个计时器成员 |
| [src/DefaultBtInteractive.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L87-L110) | 本讲核心：握手后处理、交互主循环、choke/interest 决策、keepalive、洪泛检测、不活跃裁定、PEX |
| [src/PeerInteractionCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L83-L283) | 命令层宿主：装配整套 per-peer 对象图，用状态机驱动 BtInteractive |
| [src/PeerStorage.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerStorage.h#L55-L123) | peer 存储接口 |
| [src/DefaultPeerStorage.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPeerStorage.cc#L54-L348) | 候选池 / 在用集 / 去重表三张表的实现，choke 算法的执行入口 |
| [src/ActivePeerConnectionCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ActivePeerConnectionCommand.cc#L59-L148) | 种子级周期命令：按策略补充出站连接 |
| src/BtConstants.h | 流水线深度常量 `DEFAULT_MAX_OUTSTANDING_REQUEST` 等 |
| src/PeerSessionResource.cc | `shouldBeChoking()` 的真正判定（choke 标志位） |
| src/PeerChokeCommand.cc | 种子级周期命令：每 10 秒跑一次 choke 算法 |
| src/BtRuntime.h | 连接计数与 max/min peers 上下限 |

## 4. 核心概念与源码讲解

### 4.1 PeerInteractionCommand 与 BtInteractive 接口：会话的宿主与「心跳」来源

#### 4.1.1 概念说明

aria2 把「一条 peer 连接上的所有事」拆成两层：

- **命令层**：`PeerInteractionCommand`。它是一个标准的 aria2 Command，负责与引擎调度体系打交道——注册 socket 读写事件、处理超时、异常时善后、把自己重新排队。它对应 u2-l3 学过的命令生命周期。
- **协议层**：`BtInteractive` 接口及其唯一实现 `DefaultBtInteractive`。它不认识 Command、不认识 EventPoll，只认 BT 消息。它持有这条会话的全部协议资源：消息接收器、消息分发器、请求工厂、PeerConnection。

这样分层的好处是：协议逻辑可以被单元测试直接驱动（test 目录里有 `DefaultBtInteractiveTest`），而命令层只做薄薄的胶水。

#### 4.1.2 核心流程

`BtInteractive` 接口按会话阶段暴露方法，形成一条清晰的调用时间线：

```
出站连接:  initiateHandshake() ──► receiveHandshake() ──► doPostHandshakeProcessing()
                                                            │
入站连接:  receiveAndSendHandshake() ──────► doPostHandshakeProcessing()
                                                            │
                                                            ▼
                              ┌─── doInteractionProcessing()  ◄── 每 tick 一次，直到连接关闭
                              │           │
                              │           ├── sendPendingMessage()   （内部也会被反复调用）
                              │           └── cancelAllPiece()       （异常善后时）
                              └── countPendingMessage() / countOutstandingRequest() 等查询
```

关键点：`doInteractionProcessing()` 不是死循环，**它每次只做一小轮工作就返回**，由宿主命令反复驱动。「循环」是宿主命令的自循环 + 该方法内的固定步骤顺序共同构成的。

#### 4.1.3 源码精读

**接口全貌**。[src/BtInteractive.h:46-72](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtInteractive.h#L46-L72) 定义了 12 个纯虚方法，上面流程图中的每个阶段各对应一个；本讲的主角是 `doInteractionProcessing`。

**宿主的状态机**。[src/PeerInteractionCommand.cc:297-377](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L297-L377) 的 `executeInternal()` 用一个 `switch(sequence_)` 驱动四个状态：

- `INITIATOR_SEND_HANDSHAKE`（L303-314）：socket 可写时发送握手，切到等回礼；
- `INITIATOR_WAIT_HANDSHAKE`（L315-331）：先冲刷未发完的消息，再收对方握手，成功后调 `doPostHandshakeProcessing()`；
- `RECEIVER_WAIT_HANDSHAKE`（L332-341）：入站连接路径，收发一次握手；
- `WIRED`（L342-360）：**稳定态**。每次执行只做一件事：

```cpp
case WIRED:
  btInteractive_->doInteractionProcessing();     // 协议大脑跑一轮
  if (btInteractive_->countReceivedMessageInIteration() > 0) {
    updateKeepAlive();                            // 有消息进来就推迟超时死线
  }
  // 限速判定：超过下载限速 → 摘掉读事件注册，退化为每秒轮询
  if (...doesOverallDownloadSpeedExceed() || ...doesDownloadSpeedExceed()) {
    disableReadCheckSocket(); setNoCheck(true);
  } else {
    setReadCheckSocket(getSocket());
  }
  done = true;
```

这段代码体现了 u3-l5 讲过的限速机制在 BT 侧的落点：超限时不再注册读事件，命令退化为低频轮询，`setNoCheck(true)` 同时跳过超时检查。

**WIRED 之后的统一收尾**。[src/PeerInteractionCommand.cc:363-376](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L363-L376)：如果还有排队消息或发送进行中、且上传未超限，就注册**写事件**（下次 socket 可写时继续冲刷）；否则注销写事件。最后 `addCommandSelf()` 把自己放回队列——这就是常驻命令的自循环。

**`updateKeepAlive()` 的本质**。[src/PeerAbstractCommand.cc:184-187](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerAbstractCommand.cc#L184-L187) 只有一行：把 `checkPoint_` 刷成当前时间。命令基类用 `checkPoint_` 判定超时（`--bt-timeout`），所以「只要对端还在发消息，这条命令就不会因空闲超时被杀」——超时裁定与 4.3 节的 `inactiveTimer_` 是两套独立的时钟。

**对象图的装配**。`PeerInteractionCommand` 构造函数（[src/PeerInteractionCommand.cc:83-283](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L83-L283)）就是 u6-l2 说的「peer 级对象」诞生地，值得注意的细节：

- [L120-124](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L120-L124)：每条连接新建一个 `ExtensionMessageRegistry`，把 ut_pex 注册为扩展 id 8、ut_metadata 注册为 9（BEP 9 约定的常用值）——扩展 id 是**每条连接各自协商**的，所以注册表必须是 per-peer 的；
- [L173-175](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L173-L175)：按种子 piece 数预先扩容 `PeerConnection` 接收缓冲，防止大种子的 bitfield 消息放不下；
- [L215-231](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L215-L231)：把 receiver、dispatcher、request factory、message factory 等逐个注入 `DefaultBtInteractive`，其中 [L227-228](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L227-L228) 用隐藏选项 `--bt-keep-alive-interval`（默认 120 秒，取值范围 1~120，见 [src/OptionHandlerFactory.cc:1522-1523](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L1522-L1523)）覆盖构造函数里的初值；
- [L281-282](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L281-L282)：`btRuntime_->increaseConnections()`——连接计数在命令诞生时加一，析构时（[L285-295](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L285-L295)）减一并归还 peer 的位图统计。

**异常善后**。[src/PeerInteractionCommand.cc:397-401](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L397-L401) 的 `onAbort()` 在命令因异常退出前被调用：`cancelAllPiece()` 归还所有在途 piece（[src/DefaultBtInteractive.cc:434-448](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L434-L448)，magnet 模式还会逐个取消 ut_metadata 请求），`returnPeer()` 把 peer 还给存储（见 4.4 节）。

#### 4.1.4 代码实践

**实践目标**：用日志验证「一条 peer 连接 = 一个常驻 cuid + 反复的 tick」。

**操作步骤**：

1. 用任意种子启动下载并开启 info 级日志：
   ```bash
   ./src/aria2c --seed-time=0 -d /tmp/bt -l aria2.log --log-level=info <你的.torrent>
   ```
2. 在日志中随便挑一个建立成功的连接 cuid（形如 `CUID#42`）。
3. 统计该 cuid 相关日志行数随时间的增长：
   ```bash
   grep -c 'CUID#42' aria2.log
   sleep 10 && grep -c 'CUID#42' aria2.log
   ```

**需要观察的现象**：`CUID#42 - From: x.x.x.x:port piece index=...` 之类的行在持续增多，且时间跨度远大于一轮下载循环——说明同一个 cuid（同一条 `PeerInteractionCommand`）被反复调度。

**预期结果**：只要该 peer 还在交换数据，日志行数单调增长；连接断开后（出现 return/abort 相关行）该 cuid 不再出现新日志。具体增长速率取决于下载速度，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `DefaultBtInteractive` 不自己注册 socket 事件、自己循环，而要借宿主命令之手？

**答案**：aria2 是单线程 Reactor 模型（u2-l1/u2-l2），所有 I/O 等待必须统一交给 `EventPoll`，而注册事件、处理超时、重排队是 Command 体系的职责。让 `DefaultBtInteractive` 保持「纯协议逻辑」可使它脱离引擎被单元测试直接调用，也避免了协议层与调度层耦合。

**练习 2**：`PeerInteractionCommand::executeInternal()` 里，WIRED 状态每轮结束后为什么要判断「有无待发消息」来决定注册读事件还是写事件？

**答案**：读事件驱动「收对方的数据」（piece 等），写事件驱动「把队列里没发完的消息冲刷出去」（如 dispatcher 队列积压、大 bitfield 分段发送）。两个方向的事件需求每轮都变，所以每轮收尾都要重新裁定，避免注册无用事件浪费唤醒。

**练习 3**：连接计数 `btRuntime_->increaseConnections()` 放在构造函数、`decreaseConnections()` 放在析构函数，这意味着计数统计的是什么范围的对象？

**答案**：统计的是「存活着的 PeerInteractionCommand 实例数」，即当前已握手或正在握手的会话数（含出站与入站）。它不等同于 `PeerStorage` 的 `usedPeers_` 数量，但在稳定态两者一致；用 RAII 配对保证任何退出路径（异常、停机）都不会漏减。

### 4.2 doInteractionProcessing：一次循环的固定顺序

#### 4.2.1 概念说明

`doInteractionProcessing()` 是 `DefaultBtInteractive` 的主入口，也是整个 BT 数据交换的节拍器。它每被 tick 一次，就按**固定顺序**执行一组动作。这个顺序本身就是协议语义：

1. 先裁定这条连接还要不要保留（不活跃检测）；
2. 再把接收缓冲里的消息**尽量收干**并逐条分发；
3. 然后做一连串「本地决策」：要不要掐对方、要不要声明兴趣、要不要广播新完成的 piece、要不要发 keepalive；
4. 接着按流水线深度生成新的 piece 请求；
5. 最后把这一轮攒下的所有出站消息**一次性冲刷**出去。

先收后发、决策夹在中间、发送统一收尾——这让每轮 tick 的出站流量天然聚批，减少系统调用次数。

#### 4.2.2 核心流程

非 metadataGetMode（正常下载/做种）的主干顺序（对应源码 [src/DefaultBtInteractive.cc:564-587](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L564-L587)）：

```
doInteractionProcessing():
  ① checkActiveInteraction()        # 三条超时裁定，可能直接抛异常断开
  ② 若 perSecTimer_ 满 1 秒:
       dispatcher_->checkRequestSlotAndDoNecessaryThing()   # 每秒一次请求槽清理
  ③ numReceivedMessage_ = receiveMessages()   # 循环收干消息并分发（见下）
  ④ detectMessageFlooding()          # 每 5 秒检查洪泛计数
  ⑤ decideChoking()                  # 应用 choke 算法的裁定 → choke/unchoke 消息
  ⑥ decideInterest()                 # 对方还有我没有的 piece 吗 → interested/not_interested
  ⑦ checkHave()                      # 把新完成的 piece 用 have 广播给对方
  ⑧ sendKeepAlive()                  # 距上次 >= 120s 则排队 keepalive 并立即发送
  ⑨ btRequestFactory_->removeCompletedPiece()   # 清理已完成 piece 的目标记录
  ⑩ 若下载未完成: addRequests()      # 进入 end-game 判定 + 补满请求流水线
  ⑪ 若协商了 ut_pex 且本端启用: addPeerExchangeMessage()   # 每 1 分钟一次
  ⑫ sendPendingMessage()             # 统一冲刷出站队列
```

其中 ③ `receiveMessages()` 的内部循环（[src/DefaultBtInteractive.cc:301-350](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L301-L350)）：

```
receiveMessages():
  while true:
    若整体/本任务下载速度超限 → break        # 限速时不继续收，让内核缓冲扛住反压
    message = receiver->receiveMessage()     # 缓冲不够一条完整消息时返回空
    若 message 为空 → break
    日志记录（MSG_RECEIVE_PEER_MESSAGE）
    message->doReceivedAction()              # ★ 分发：消息对象自己执行副作用
    switch (message->getId()):
      choke/unchoke  → 洪泛计数
      request/piece  → inactiveTimer_ 重置为现在   # 有实质数据流动，不算不活跃
      keep alive     → 洪泛计数
  # 循环外：流水线深度自适应增长（见 4.2.3 末尾）
```

「分发」没有集中的 giant switch——每个 `BtMessage` 子类在 `doReceivedAction()` 里做自己该做的事（如 `BtPieceMessage` 写数据入 piece、`BtHaveMessage` 更新对方位图），这是 u6-l5 的主题，这里只需知道调用点在 ③。

#### 4.2.3 源码精读

**主函数本体**。[src/DefaultBtInteractive.cc:531-587](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L531-L587)：`doInteractionProcessing()` 先按 `metadataGetMode_` 分流。magnet 元数据获取模式（L533-563）走精简分支：只 keepalive、收消息、补充 ut_metadata 请求、每秒清理超时的元数据请求，收齐元数据后对任务发出强制停止（L558-561），由 BtDependency 派生真正的数据任务（u6-l9 详述）。正常模式的 L564-580 就是上面流程图的 ①~⑩，最后的 [L581-586](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L581-L586) 是 ⑪⑫。

**收消息与限速闸门**。[src/DefaultBtInteractive.cc:305-339](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L305-L339)：注意 while 循环**每条消息之前**都检查两级下载限速（`requestGroupMan_->doesOverallDownloadSpeedExceed()` 与本任务的 `doesDownloadSpeedExceed()`）。BT 的限速不是丢弃数据，而是停止从 socket 继续读取——数据滞留在内核接收缓冲，靠 TCP 反压传导给对端（呼应 u3-l5）。

**流水线深度自适应**。[src/DefaultBtInteractive.cc:341-348](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L341-L348)：一轮之内，如果在途请求（outstanding request）被快速消化（`本轮消化的请求数 × 4 ≥ 当前上限`），说明带宽还有余量，就把 `maxOutstandingRequest_` 翻倍，上限 [src/BtConstants.h:51-54](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtConstants.h#L51-L54) 的 `UB_MAX_OUTSTANDING_REQUEST = 256`；初值为同文件的 `DEFAULT_MAX_OUTSTANDING_REQUEST = 6`。用数学语言表达：设一轮内在途请求从 \(O_{old}\) 降到 \(O_{new}\)，当

\[(O_{old} - O_{new}) \times 4 \geq M\]

则 \(M \leftarrow \min(2M,\ 256)\)。这就是「每 peer 请求流水线从 6 起步、按消化能力指数增长到 256」的来历（end-game 阶段禁用增长）。

**choke 的「执行」**。[src/DefaultBtInteractive.cc:248-263](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L248-L263)：`decideChoking()` 读取 `peer_->shouldBeChoking()`，与当前 `amChoking` 状态不一致时才发 choke/unchoke 消息（省流量）。而 `shouldBeChoking()` 的真身在 [src/PeerSessionResource.cc:87-93](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerSessionResource.cc#L87-L93)：

```cpp
bool PeerSessionResource::shouldBeChoking() const
{
  if (optUnchoking_) {        // choke 算法钦点的「乐观放开」peer
    return false;
  }
  return chokingRequired_;    // 算法裁定是否应掐住
}
```

两个标志位都不是 `DefaultBtInteractive` 写的——它们由 choke 算法（4.4 节）周期性写入。**决策与执行分离**：算法每 10 秒全局重排一次，交互循环每 tick 只负责把裁定翻译成消息。

**interest 的「执行」**。[src/DefaultBtInteractive.cc:352-370](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L352-L370)：`decideInterest()` 用 `pieceStorage_->hasMissingPiece(peer_)` 询问「对方位图里还有我没有的 piece 吗」，答案与当前 `amInterested` 不一致时才发 interested/not_interested。这条决策完全本地可算（对方位图已随 bitfield/have 消息同步进 `Peer` 对象），无需通信。

**have 广播的批量优化**。[src/DefaultBtInteractive.cc:265-290](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L265-L290)：`checkHave()` 先用 `getAdvertisedPieceIndexes(haveIndexes, cuid_, lastHaveIndex_)` 取出「自从上次告知对方以来新完成的 piece 序号」，然后做一个成本比较：若 `5 + bitfieldLength ≤ 9 × 条数`（一条 have 消息固定 9 字节，整个 bitfield 只要 `5 + 位图字节数`），干脆改发一条完整的 bitfield（或 Fast 扩展下的 have all）——piece 很多时一次位图同步比几百条 have 便宜。

**请求生成**。[src/DefaultBtInteractive.cc:414-432](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L414-L432)：`addRequests()` 先做 end-game 判定（已无「未被任何人认领」的缺失 piece 时进入收尾模式，详见 u6-l6），再 `fillPiece(maxOutstandingRequest_)` 领取新 piece（[L372-412](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L372-L412)，注意对方掐着我时只有 Fast 扩展的 allowed-fast piece 可领），最后把「应补足的请求数」交给 `btRequestFactory_->createRequestMessages()` 生成 request 消息入队：

```cpp
size_t reqNumToCreate =
    maxOutstandingRequest_ <= dispatcher_->countOutstandingRequest()
        ? 0
        : maxOutstandingRequest_ - dispatcher_->countOutstandingRequest();
```

即「目标流水线深度 − 在途请求数 = 本轮要新发的请求数」，这是 u6-l6 请求流水线的入口。

#### 4.2.4 代码实践

**实践目标**：通过插桩观察主循环各步骤在真实下载中的执行频次差异。

**操作步骤**：

1. 在 [src/DefaultBtInteractive.cc:570](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L570)（`numReceivedMessage_ = receiveMessages();`）前后各加一行 `A2_LOG_DEBUG`，打印本轮收到的消息数与本轮结束时 `dispatcher_->countOutstandingRequest()` 的值。
2. 重新编译：`make -j$(nproc)`。
3. 跑一个种子下载，日志级别用 debug：
   ```bash
   ./src/aria2c --seed-time=0 -d /tmp/bt -l aria2.log --log-level=debug <你的.torrent>
   ```

**需要观察的现象**：每轮 tick 收到的消息数大多在 0 到十几条之间波动；做种阶段（对方在下载）`countOutstandingRequest` 常为 0（我们不发请求），下载阶段则多数时候贴着 `maxOutstandingRequest_` 的当前值。

**预期结果**：日志能看出「流水线深度从 6 逐步翻倍」的过程（在途数上限台阶式上升）。具体数值依赖网络环境，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 ④ 洪泛检测放在 ③ 收消息之后、⑤ choke 执行之前？换个位置行不行？

**答案**：洪泛计数（choke/unchoke/keepalive 次数）正是在 ③ 里累加的，所以检测必须在 ③ 之后才有本轮数据可查。放在 ⑤ 之前还有个好处：一旦判定洪泛立刻抛 `DL_ABORT_EX` 断连，不必再浪费后续步骤去生成消息。放到更后面则白做了 ⑤~⑪ 的工作；放到 ③ 之前则本轮洪泛要迟一轮才被发现。顺序是「先收集证据，再审判，后行动」。

**练习 2**：`receiveMessages()` 为什么每次循环都要检查下载限速，而不是收完缓冲里所有消息再统一检查？

**答案**：piece 消息每条可达 64KB（`MAX_BLOCK_LENGTH`，[src/BtConstants.h:49](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtConstants.h#L49)），若一口气收干，一条高速链路的缓冲可能带来几十 MB 数据，限速形同虚设。逐条检查能让「停止读取」及时生效，让 TCP 反压尽快传导。

**练习 3**：`decideChoking()` 和 `decideInterest()` 都只在状态**变化**时才发消息。这种「边沿触发」设计省了什么？

**答案**：BT 协议里 choke/interest 是**电平语义**——状态一旦声明就持续有效，直到下一条相反消息。每 tick 重复发送完全冗余。边沿触发既省上行带宽（尤其面对几十个 peer 时），也避免频繁的 choke/unchoke 消息触发对方的洪泛计数器（4.3 节），是协议与实现的双赢。

### 4.3 三个节律器：keepalive、洪泛检测与不活跃裁定

#### 4.3.1 概念说明

一条 BT 连接如果长时间没有任何消息，中间的 NAT、防火墙可能会悄悄丢掉它的映射；而对端也可能已经死掉（进程崩溃、断网）。BT 协议用两个机制对抗这两类问题：

- **keepalive**：我方周期性发一条零长度消息，既保活路径，也暗示「我还活着」；
- **超时裁定**：我方周期性检查「对方多久没给我发过有用消息了」，超时就主动断开，把连接名额让给别人。

第三个节律器**洪泛检测**防的是恶意或损坏的客户端：以远超正常的频率发 choke/unchoke 或 keepalive 消息，消耗我方 CPU（每条消息都要解析分发）。aria2 对此的对策简单粗暴——数条数，超标直接断连。

`DefaultBtInteractive` 用五个 `Timer` 成员实现全部节律（[src/DefaultBtInteractive.h:127-147](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.h#L127-L147)）：

| 计时器 | 周期 | 动作 |
| --- | --- | --- |
| `keepAliveTimer_` | `keepAliveInterval_`（`--bt-keep-alive-interval`，默认 120s） | 排队并发送 keepalive |
| `floodingTimer_` | 5 秒（`FLOODING_CHECK_INTERVAL`） | 检查洪泛计数并复位 |
| `inactiveTimer_` | 阈值 30s / 60s | 三条不活跃裁定 |
| `pexTimer_` | 1 分钟（`UTPexExtensionMessage::DEFAULT_INTERVAL`，[src/UTPexExtensionMessage.h:118](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTPexExtensionMessage.h#L118)） | 发一条 ut_pex |
| `perSecTimer_` | 1 秒 | 请求槽清理 / ut_metadata 超时清理 |

#### 4.3.2 核心流程

```
sendKeepAlive():
  if now - keepAliveTimer_ >= keepAliveInterval_:
      排队 keepalive; 立即 sendMessages(); keepAliveTimer_ = now

detectMessageFlooding():                      # 每 5 秒一轮
  if now - floodingTimer_ >= 5s:
      if chokeUnchokeCount >= 2 or keepAliveCount >= 2:
          throw EX_FLOODING_DETECTED           # DL_ABORT_EX → 断开这条连接
      else:
          floodingStat_.reset()                # 清零，重新数下个 5 秒
      floodingTimer_ = now

checkActiveInteraction():                      # 三条裁定，任一命中即断开
  ① 双方互不感兴趣持续 30s        → 优雅断开（名额让给感兴趣的 peer）
  ② 自上次 request/piece 起 60s 无数据流动 → 断开（EX_DROP_INACTIVE_CONNECTION）
  ③ 对方是做种者且我也下载完毕    → 断开（两个 seeder 互连毫无意义）
```

注意 ② 的时钟基准：`inactiveTimer_` 只在收到 **request 或 piece** 消息时重置（[src/DefaultBtInteractive.cc:331-334](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L331-L334)）——have、keepalive 之类的小消息不算「活跃」，因为它们不代表数据贡献。

#### 4.3.3 源码精读

**keepalive**。[src/DefaultBtInteractive.cc:292-299](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L292-L299)：与其他排队消息不同，keepalive 排队后**立即调用** `dispatcher_->sendMessages()` 冲刷，不等本轮 ⑫ 的统一发送——保活消息越早出门越好。

**洪泛检测**。[src/DefaultBtInteractive.cc:452-469](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L452-L469)：`FLOODING_CHECK_INTERVAL = 5_s` 是本文件匿名命名空间里的常量；阈值是「5 秒内 ≥2 次 choke/unchoke 切换，或 ≥2 条 keepalive」。计数器 `FloodingStat` 定义在 [src/DefaultBtInteractive.h:66-97](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.h#L66-L97)，只有两个 int 与自增/复位方法。抛出的 `EX_FLOODING_DETECTED`（[src/message.h:277](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h#L277)，文案 "Flooding detected."）是 `DL_ABORT_EX`，走 u2-l3 讲过的可放弃出口：`PeerInteractionCommand` 析构 → `onAbort()` 归还 piece 与 peer → 换下一个候选。

有一个细节值得体会：正常客户端的 keepalive 间隔是 120 秒，5 秒窗口内出现 2 条 keepalive 意味着对端发送频率是我们的约 50 倍——阈值看似宽松（只要 2 条），实际很难误伤。

**不活跃裁定**。[src/DefaultBtInteractive.cc:471-503](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L471-L503) 的 `checkActiveInteraction()`，三条裁定按注释逐段对应：

- L476-487：互不感兴趣 30 秒 → `setDisconnectedGracefully(true)` 后抛异常。注释点明了动机：**为了把连接名额腾给「互相感兴趣」的 peer**。标记 graceful 会让 `returnPeer` 把它放进 dropped 列表供 PEX 广播（4.4 节）；
- L491-498：60 秒无 request/piece → 抛 `EX_DROP_INACTIVE_CONNECTION`。注释解释：刚连上却毫无贡献的 peer 纯属浪费资源；
- L500-502：双方都是 seeder → 抛 `MSG_GOOD_BYE_SEEDER`（文案很有梗："Client is in seed state: Good Bye Seeder;)"）。

这三条都在主循环 ① 执行——**每轮 tick 的第一件事就是审判这条连接还配不配活着**，不合格立即抛异常终止本轮剩余步骤。

#### 4.3.4 代码实践

**实践目标**：验证 keepalive 的实际发送间隔与 `--bt-keep-alive-interval` 的关系。

**操作步骤**：

1. 该选项取值范围 1~120（[src/OptionHandlerFactory.cc:1522-1523](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L1522-L1523)），用最小值启动一次做种（做种时没有 piece 流动，最容易观察到 keepalive）：
   ```bash
   ./src/aria2c --seed-time=600 --bt-keep-alive-interval=1 \
       -d /tmp/bt -l aria2.log --log-level=info <已下完的.torrent>
   ```
2. 统计日志中的 keep alive 行：
   ```bash
   grep -c 'keep alive' aria2.log
   ```

**需要观察的现象**：当有 peer 连上且双方无数据交换时，每个 peer 每 1 秒左右产生一条 `CUID#N - From: x.x.x.x:p keep alive` 日志（我们发出去的 keepalive 不会以 From: 形式记录，看到的多为对方回礼；若对方也设了短间隔则双向都密）。

**预期结果**：keep alive 行数与连接数 × 时间的乘积同量级。若始终观察不到，多半是环境里没有真实 peer 连入（自建两个 aria2 互连即可），**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：洪泛检测为什么只数 choke/unchoke 和 keepalive，不数 have 或 piece？

**答案**：洪泛攻击的目的是让对方做无意义的重复工作。piece 消息带着真数据、have 更新真位图，数量大是正常且「有产出」的；而高频 choke/unchoke 会反复触发对方的 choke 状态机与请求作废（代价极高），高频 keepalive 则纯粹烧 CPU。检测器盯的是「单位价值为零却高频出现」的消息。另外 piece 本身受 `maxOutstandingRequest_` 流水线约束，想洪泛也快不起来。

**练习 2**：裁定 ① 里为什么要先 `peer_->setDisconnectedGracefully(true)` 再抛异常？去掉这行会怎样？

**答案**：这个标志位会被 `DefaultPeerStorage::onReturningPeer()` 检查：优雅断开的**出站**连接会进入 dropped 列表，随下一次 ut_pex 广播告诉其他 peer「我最近掉了个 peer」——这是 PEX 协议的 dropped 字段的语义来源。去掉它，PEX 消息就永远只有 fresh 没有 dropped，其他客户端无法及时清理失效 peer。

**练习 3**：`inactiveTimer_`、命令基类的 `checkPoint_`（超时死线）、`floodingTimer_` 是三个独立时钟。它们各自防什么？

**答案**：`checkPoint_`（`--bt-timeout`）防**对端完全沉默**——连 TCP 层面都没有任何字节到达时，由命令基类超时杀掉命令；`inactiveTimer_` 防**有消息但无数据贡献**——对方还在发 have/keepalive，但 60 秒没给过一条 request/piece；`floodingTimer_` 防**消息过多而非过少**。三者分别覆盖「死链」「懒链」「疯链」。

### 4.4 PeerStorage：三张表、坏 peer 封禁与 choke 算法落点

#### 4.4.1 概念说明

一个种子同时面对几十上百个 peer，需要一个集中的账房管理「谁还没连（候选）、谁正在连（在用）、谁最近掉了（dropped）」。这就是 `PeerStorage`。它与 4.2 的交互循环关系密切：

- 交互循环的 `receiveHandshake()` 要查 `getUsedPeers()` 做 peer_id 去重（[src/DefaultBtInteractive.cc:134-140](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L134-L140)）；
- PEX 广播要遍历 `getUsedPeers()` 与 `getDroppedPeers()` 组装消息（[src/DefaultBtInteractive.cc:505-529](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L505-L529)）；
- 连接断开时 `onAbort()` 调 `returnPeer()` 归还；
- 更关键的是：**choke 算法（choke 算法的「决策」）就挂在 PeerStorage 上**——它需要全局视野（所有在用 peer 的速度），所以不能放在任何单个 `DefaultBtInteractive` 里。

#### 4.4.2 核心流程

```
                addPeer()  (来自 tracker/DHT/PEX/LPD)
                    │ 去重(IP,origPort)、坏 peer 过滤、容量 512 上限
                    ▼
  unusedPeers_ (deque, 候选池, FIFO) ──checkoutPeer(cuid)──► usedPeers_ (set, 在用)
                    ▲                                    │
                    │            returnPeer(peer)        │
                    └────────────(从在用集移除)◄─────────┘
                    │ 优雅断开的出站连接 → droppedPeers_ (≤50, 供 PEX)
                    
  坏 peer: addBadPeer(ip) → badPeers_[ip] = now + rand[120,720]s   # 过期自动解禁
  choke:   PeerChokeCommand 每轮问 chokeRoundIntervalElapsed() (10s)
          → executeChoke() → seeder/leecher 两套算法改写各 peer 的
            chokingRequired_ / optUnchoking_ 标志 → 交互循环下轮生效
```

`uniqPeers_` 是第四张辅助结构：一个 `(IP, origPort)` 的 set，让去重判断 O(log n)。析构函数里有条断言 `uniqPeers_.size() == unusedPeers_.size() + usedPeers_.size()`（[src/DefaultPeerStorage.cc:68-71](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPeerStorage.cc#L68-L71)），说明它是纯冗余索引，与两张主表必须严格同步。

#### 4.4.3 源码精读

**接口**。[src/PeerStorage.h:55-123](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerStorage.h#L55-L123) 定义了 13 个纯虚方法；注释即文档，例如 `checkoutPeer` 的注释明确说了「从 unused 列表头取第一个移入 used 集合并调用 `Peer::usedBy(cuid)`」。

**候选池入库**。[src/DefaultPeerStorage.cc:89-119](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPeerStorage.cc#L89-L119)：`addPeer()` 三道闸门——容量（`MAX_PEER_LIST_SIZE = 512`，[L54-58](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPeerStorage.cc#L54-L58)）、已存在、坏 peer。批量版 [L121-161](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPeerStorage.cc#L121-L161) 逻辑相同但超限时从尾部淘汰旧的（`deleteUnusedPeer`）。`addAndCheckoutPeer()`（[L163-187](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPeerStorage.cc#L163-L187)）是「插入并立即借出」的原子操作，入站连接握手成功时用它——新 peer **插到队头**再 checkout，保证它立刻被自己这条连接使用，不会被别人抢走。

**借出与归还**。[src/DefaultPeerStorage.cc:271-289](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPeerStorage.cc#L271-L289)：`checkoutPeer()` 从 deque 头部弹出、打上 cuid、插入 used 集。归还 [L296-325](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPeerStorage.cc#L296-L325) 的 `onReturningPeer()` 有两个精妙副作用：

1. 优雅断开的**出站** peer 进 dropped 列表（上限 50，去重旧的，[L189-205](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPeerStorage.cc#L189-L205)）——供下一条 PEX 广播；
2. **若刚归还的 peer 处于「我没掐它且它对我感兴趣」的状态，立刻重跑 `executeChoke()`**——因为这个 peer 原本占着一个 unchoke 名额，它走了，名额要马上让给别人，等下个 10 秒周期太慢。

**坏 peer 封禁**。[src/DefaultPeerStorage.cc:236-258](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPeerStorage.cc#L236-L258)：`addBadPeer()` 给 IP 记一个随机 120~720 秒的封禁期（`max(random(601), 120)`），注释点明随机化是为了避免大量坏 peer 同一时刻集体复活；每小时顺手清一遍过期条目。查询 [L221-234](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPeerStorage.cc#L221-L234) 的 `isBadPeer()` 在到期时惰性删除。发坏数据（piece 哈希不过关）的 peer 会被 `BtPieceMessage` 校验失败路径拉黑——坏一次封 2~12 分钟，比封永久宽容。

**choke 算法落点**。[src/DefaultPeerStorage.cc:327-348](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPeerStorage.cc#L327-L348)：`chokeRoundIntervalElapsed()` 用常量 `CHOKE_ROUND_INTERVAL = 10_s` 判周期，按 `pieceStorage_->downloadFinished()` 选择 seeder（`BtSeederStateChoke`）或 leecher（`BtLeecherStateChoke`）两套算法对**整个 usedPeers_ 集合**执行 `executeChoke()`。驱动者是 [src/PeerChokeCommand.cc:49-59](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerChokeCommand.cc#L49-L59)——又一个常驻自循环命令，每轮问一句「到点了吗」，到点就跑算法。算法的输出不是消息，而是写进每个 `PeerSessionResource` 的 `chokingRequired_`/`optUnchoking_` 标志；4.2 节的 `decideChoking()` 每 tick 读标志翻译成 choke/unchoke 消息。**choke 算法本身（tit-for-tat + optimistic unchoke）是 u6-l5 之后的拓展话题，本讲只需记住这条「算法周期写标志、交互循环即时读标志」的流水线。**

#### 4.4.4 代码实践

**实践目标**：用 debug 日志观察一次完整的「checkout → 交互 → return」生命周期。

**操作步骤**：

1. 以 debug 级别跑一次种子下载：
   ```bash
   ./src/aria2c --seed-time=0 -d /tmp/bt -l aria2.log --log-level=debug <你的.torrent>
   ```
2. 提取借出与归还事件：
   ```bash
   grep -E 'Checkout peer|returned from CUID' aria2.log | head -20
   ```

**需要观察的现象**：每个 peer 先出现一条 `Checkout peer IP:port to CUID#N`（[src/DefaultPeerStorage.cc:285-287](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPeerStorage.cc#L285-L287)），隔一段时间（正常交换或超时断开）后出现 `Peer IP:port returned from CUID#N`（[L314-316](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPeerStorage.cc#L314-L316)）。

**预期结果**：借出与归还一一同 cuid 配对；被洪泛检测或超时裁定踢掉的 peer，其归还行紧跟着断开原因的异常日志。若还看到 `Added x.x.x.x as bad peer`，说明该 peer 发过校验失败的数据。具体日志量取决于 peer 数，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `unusedPeers_` 用 deque（FIFO）而 `usedPeers_` 用 set？

**答案**：候选池要「先来先服务」地公平消耗（tracker 早期返回的 peer 先被尝试），且需要从两端操作（尾部淘汰旧候选、头部借出、`addAndCheckoutPeer` 插队头），deque 全都 O(1)。在用集的核心操作是「按 peer 对象快速删除与遍历」，且断言要求精确的集合语义（析构时校验元素个数等式），set（有序、无重）最合适。

**练习 2**：坏 peer 封禁时长为什么是 `max(random(601), 120)` 秒这种「120~720 随机」，而不是固定值？

**答案**：两个目的。下限 120 秒保证惩罚有威慑力（坏 peer 不能靠立刻重连刷回来）；上限随机化（约 2~12 分钟均匀分布）避免大量同时被拉黑的 peer 在同一时刻解禁、同时回来捣乱——把重试压力在时间轴上摊平。这与 HTTP 侧退避加抖动的思想一致。

**练习 3**：choke 算法为什么放在 `PeerStorage`（种子级）而不是每个 `DefaultBtInteractive`（peer 级）里跑？

**答案**：choke 本质是**资源分配问题**：我有限的 unchoke 名额该分给几十个 peer 中的哪几个，需要横向比较所有人的上传速度与兴趣状态，天然是全局视角。放在单个 peer 的交互对象里既看不到全局，还会导致每个 peer 各自为政地频繁改判。挂在 PeerStorage 上、由独立的 `PeerChokeCommand` 每 10 秒统一跑一次，决策全局一致且节流。

### 4.5 ActivePeerConnectionCommand：补充新连接的策略

#### 4.5.1 概念说明

前四节解决了「一条连接如何活下去」，本节解决「该同时活多少条连接」。`ActivePeerConnectionCommand` 是 u6-l2 中 `BtSetup` 装配的种子级周期命令之一（[src/BtSetup.cc:126-135](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtSetup.cc#L126-L135)，每 2 秒（magnet 元数据模式）或 10 秒一跳）。它是出站连接的**泵**：定期检查「现在的连接数够不够、快不快」，不够就从 `PeerStorage` 候选池里 checkout 出新 peer，交给 `PeerInitiateConnectionCommand` 去拨号。

策略围绕两个选项展开：`--bt-max-peers`（上限，默认 55）与 `--bt-min-peers`（下限，默认 40），以及阈值速度 `--bt-request-peer-speed-limit`（见 [src/BtRuntime.h:100-101](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtRuntime.h#L100-L101) 的 `DEFAULT_MAX_PEERS`/`DEFAULT_MIN_PEERS`）。

#### 4.5.2 核心流程

```
ActivePeerConnectionCommand::execute()        # 每 interval_(10s 或 2s) 做一次
  若 btRuntime_->isHalt() → 退出（返回 true，命令销毁）
  ├─ 计算 thresholdSpeed:
  │     有元数据(真种子) → --bt-request-peer-speed-limit
  │     magnet 元数据模式 → 0（全力找 peer 拿元数据）
  │     再与 --max-download-limit 取 min
  ├─ 判断要不要补人（满足任一）:
  │     做种态: 下载完毕 && 连接数 < max-peers && 上传速度 < 0.8×上传限速
  │     下载态: 下载未完 && (当前下载速度 < thresholdSpeed || 连接数 < min-peers)
  ├─ 要补 → numConnection = 做种态 ? min(5, max-peers - 连接数) : 5
  │            makeNewConnections(numConnection)
  │              # 每次: peerStorage_->checkoutPeer(newCuid)
  │              #       → new PeerInitiateConnectionCommand 入引擎
  └─ 连接数 == 0 且未下载完 → btAnnounce_->overrideMinInterval(2_min)
                # 一个 peer 都没有！让 tracker 提前汇报，赶紧弄点 peer 回来
  最后 addCommandSelf() 重排队（返回 false）
```

注意「下载态」的补人条件是个**或**：要么速度不达标（说明现有 peer 不给力，多找人），要么连接数连下限都没到（不管速度先铺满基数）。而「做种态」的条件更保守——只有当有人真的在从我这里猛拉数据（上传带宽还有富余）时才值得多收连接。

#### 4.5.3 源码精读

**周期与构造**。[src/ActivePeerConnectionCommand.cc:59-69](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ActivePeerConnectionCommand.cc#L59-L69)：构造参数 `interval` 由 BtSetup 决定（10 秒，magnet 模式 2 秒），`numNewConnection_(5)` 是每跳最多发起的新连接数。与 `PeerInteractionCommand` 一样用 `increaseNumCommand`/`decreaseNumCommand` 配对维持任务计数（这是 RequestGroup 判断「任务是否结束」的依据，见 u3-l1）。

**判定主体**。[src/ActivePeerConnectionCommand.cc:81-126](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ActivePeerConnectionCommand.cc#L81-L126)：先取 `NetStat` 算当前上传/下载速度（u3-l5 的 SpeedCalc 在任务层的出口）；[L98-105](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ActivePeerConnectionCommand.cc#L98-L105) 的条件表达式就是流程图里那两行注释（`// for seeder state` 与 `// for leecher state`）。做种态补人数 [L108-114](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ActivePeerConnectionCommand.cc#L108-L114) 用 `min(numNewConnection_, maxPeers - connections)` 防止越过 `--bt-max-peers`；下载态则固定要 5 个（后面 `makeNewConnections` 内部的 `isPeerAvailable()` 与候选池容量 512 自然限流）。

**兜底：缩短 announce 间隔**。[src/ActivePeerConnectionCommand.cc:121-124](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ActivePeerConnectionCommand.cc#L121-L124)：连接数为零且还在下载（典型场景：刚启动、或所有 peer 都被踢光），调用 `btAnnounce_->overrideMinInterval(BtAnnounce::DEFAULT_ANNOUNCE_INTERVAL)`，把 tracker 汇报的最小间隔压到 2 分钟（常量见 [src/BtAnnounce.h:142](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtAnnounce.h#L142)）。这是「peer 荒」时的自救：让 u6-l7 的 announce 流程更频繁地向 tracker 要 peer 列表。

**拨号**。[src/ActivePeerConnectionCommand.cc:131-148](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ActivePeerConnectionCommand.cc#L131-L148)：`makeNewConnections()` 循环「checkout 一个 peer → 铸一个 `PeerInitiateConnectionCommand` → 交引擎」。checkout 同时把 peer 从候选池移入在用集、打上新 cuid，所以这里的并发安全由「单线程引擎 + 队列」天然保证（回顾 u2-l1：aria2 没有下载工作线程）。`PeerInitiateConnectionCommand` 负责剩下的 TCP 建连 + 主动握手，成功后诞生的正是 4.1 节的 `PeerInteractionCommand`。

**闭环**。至此 BT 连接管理形成完整循环：

```
tracker/DHT/PEX/LPD 提供 peer → addPeer 入候选池(4.4)
    → ActivePeerConnectionCommand checkout(4.5) → PeerInitiateConnectionCommand 拨号
    → 握手(u6-l3) → PeerInteractionCommand + DefaultBtInteractive 常驻交互(4.1/4.2)
    → 洪泛/不活跃/双 seeder 裁定断开(4.3) → returnPeer 回候选体系(4.4)
    → ActivePeerConnectionCommand 下一跳再补人(4.5) ……
```

#### 4.5.4 代码实践

**实践目标**：验证 `--bt-min-peers`/`--bt-max-peers` 对连接数爬升速度的影响。

**操作步骤**：

1. 选一个 peer 较多的种子（知名 Linux 发行版镜像站常提供），分别用两组参数各跑 2 分钟，观察控制台 SEED/LEECH 状态行中的连接数（`CN` 列）：
   ```bash
   # 第一组：默认(55/40)
   ./src/aria2c --seed-time=0 -d /tmp/bt1 <你的.torrent>
   # 第二组：压低上下限
   ./src/aria2c --seed-time=0 --bt-max-peers=10 --bt-min-peers=3 -d /tmp/bt2 <你的.torrent>
   ```

**需要观察的现象**：第二组的连接数爬到 10 附近就不再增长；第一组可以明显更高。下载速度通常第一组更快（更多 peer 意味着更多并行 piece 来源）。

**预期结果**：连接数上限被 `--bt-max-peers` 钳制。注意做种态下连接数还会受「上传速度 < 0.8×`--max-upload-limit`」条件约束，若想观察做种态行为可加 `--seed-time`。具体数值依赖种子的热度，**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`thresholdSpeed` 在 magnet 元数据获取模式下被置为 0，这意味着该模式下补人条件如何退化？

**答案**：`thresholdSpeed` 为 0 时，`stat.calculateDownloadSpeed() < thresholdSpeed` 即「速度 < 0」，永假；补人条件中的这一支失效，整个条件退化为只靠「连接数 < min-peers」驱动。但元数据模式下 interval 只有 2 秒（BtSetup 装配时传 `2_s`），检查频率是正常下载的 5 倍，加上 ut_metadata 需要尽量多的 peer 来源，整体行为是「高频、贪婪地铺连接」。

**练习 2**：为什么做种态的补人条件里有一个 `stat.calculateUploadSpeed() < maxUploadLimit * 0.8`，而下载态没有类似的上传带宽检查？

**答案**：做种时连接的**唯一用途**是上传数据，若上传带宽已被打满（超过限速的 80%），再多连接也分不到带宽，只会空耗对方连接名额；留 20% 余量是避免在限速临界点反复增减连接。下载态的连接主要用于下载数据，上传量小（ACK、have 之类），不受上传带宽约束，因此无此检查。

**练习 3**：`makeNewConnections(5)` 一次发起 5 个连接，为什么不由一个循环命令慢慢发起？

**答案**：握手是个耗时过程（TCP 建连 + RTT + 协议握手），串行发起会把后面 peer 的开始时间推迟好几倍。每跳并发 5 个、每 10 秒一跳，既能在 peer 荒时快速铺开（一分钟理论最多 30 个新连接），又以 `--bt-max-peers` 与候选池存量为界，不会瞬时打爆。这是「批处理 + 周期节流」的折中。

## 5. 综合实践：统计一次下载中各消息类型的收发分布

本实践对应本讲规格中的主任务（已把源码中不存在的 `doReceivedHandshakeHandshake` 修正为真实入口 `doInteractionProcessing` 的收发路径）。目标：拿到一份「消息类型 → 次数」排行榜，并用本讲知识解释前三名。

### 5.1 纯日志版（无需改代码）

aria2 对**每一条收到的消息**都有一条 INFO 日志（[src/DefaultBtInteractive.cc:315-317](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L315-L317) 处的 `A2_LOG_INFO(fmt(MSG_RECEIVE_PEER_MESSAGE, ...))`，格式为 `CUID#%d - From: ip:port <消息文本>`，消息文本以各消息类的 `NAME` 开头）。各 `NAME` 常量（如 [src/BtPieceMessage.cc:67](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtPieceMessage.cc#L67) 的 `"piece"`）已从源码确认。

```bash
./src/aria2c --seed-time=0 -d /tmp/bt -l aria2.log --log-level=info <你的.torrent>
# 下载进行中或结束后：
grep -- '- From:' aria2.log \
  | grep -oE 'keep alive|have none|have all|not interested|allowed fast|suggest piece|bitfield|handshake|extended|interested|unchoke|reject|cancel|request|piece|have|port' \
  | sort | uniq -c | sort -rn
```

注意 grep 交替分支必须**长词在前**（`have all` 在 `have` 前、`not interested` 在 `interested` 前），否则短词会先匹配截断。此法统计的是**接收方向**；发送方向的每条消息在 debug 级别另有日志，也可以把 `--log-level=debug` 打开后 grep `MSG_SEND` 相关行观察（具体文案待本地验证）。

### 5.2 插桩版（统计收发两个方向）

1. **接收侧**：在 [src/DefaultBtInteractive.cc:320-338](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L320-L338) 的 `switch (message->getId())` 处为 `default` 分支补一个计数（示例代码）：

   ```cpp
   // 示例代码：文件顶部匿名命名空间里
   namespace { unsigned long g_recvCount[256] = {}; }
   // switch 的 default 分支里：
   ++g_recvCount[message->getId()];
   ```

   现有 case（choke/unchoke/request/piece/keepalive）里也各加一行 `++g_recvCount[message->getId()];`。
2. **发送侧**：在 [src/DefaultBtInteractive.cc:450](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L450) 的 `sendPendingMessage()` 里，调用 `dispatcher_->sendMessages()` 前后各记一次 `dispatcher_->countMessageInQueue()`，差值即本轮发送量（按类型细分需进 `DefaultBtMessageDispatcher` 的发送循环插桩，属于 u6-l6 的练习范围）。
3. 在 `DefaultBtInteractive::~DefaultBtInteractive()`（[L112](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L112)）里把计数表打印出来（消息 id 与名称对照见下表）。
4. `make -j$(nproc)` 后跑一次完整下载。

### 5.3 消息 id 对照表（均出自各 `Bt*Message.h` 的 `static const uint8_t ID`）

| id | 消息 | 说明 |
| --- | --- | --- |
| 0 | choke | 掐住 |
| 1 | unchoke | 放开 |
| 2 | interested | 感兴趣 |
| 3 | not interested | 不感兴趣 |
| 4 | have | 我有了第 n 块 |
| 5 | bitfield | 位图同步 |
| 6 | request | 请给我某块某段 |
| 7 | piece | 数据本体 |
| 9 | port | DHT 端口通告 |
| 13/14/15/16/17 | suggest/have all/have none/reject/allowed fast | Fast 扩展族 |
| 20 | extended | 扩展协议（ut_pex/ut_metadata 等） |
| 99 | keep alive | **aria2 内部 id**；线上是 0 长度帧 |
| 127 | handshake | 协议前置 68 字节，不在长度前缀帧体系内 |

### 5.4 预期结果与分析框架

下载阶段的接收榜前三通常是 **piece（7）**、**request（6）**、**have（4）**（待本地验证）。解释框架：

- **piece 第一**：每块数据就是一条消息，`MAX_BLOCK_LENGTH = 64_k`（[src/BtConstants.h:49](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtConstants.h#L49)）意味着 1MB 的 piece 要拆约 16~64 条 piece 消息，数据流量天然映射为高频消息；
- **request 第二**：4.2 节的流水线机制——`maxOutstandingRequest_` 从 6 起步翻倍到最高 256，每个在途请求将来都要换回一条 piece，故 request 数量级与 piece 匹配（做种时我们收到的 request 会取代 piece 成为第一）；
- **have 第三**：每个 peer 每完成一个 piece 就向所有连接广播一条 have（4.2 的 `checkHave`），它随下载进度线性增长，但远低于块级消息。

如果榜上出现异常（如 choke/unchoke 远高于其他），对照 4.3 的洪泛检测窗口（5 秒 2 次）想想 aria2 为什么还没把它踢掉——那通常意味着对端在阈值边缘反复试探。

## 6. 本讲小结

- **宿主与大脑分工**：`PeerInteractionCommand`（命令层，状态机 `INITIATOR_SEND_HANDSHAKE → … → WIRED`）负责调度、事件注册与善后；`DefaultBtInteractive`（协议层）持有消息收发器、分发器、请求工厂，实现 `BtInteractive` 接口的 12 个方法。
- **一次 tick 的固定顺序**：`doInteractionProcessing()` = 不活跃裁定 → （每秒）请求槽清理 → `receiveMessages()` 收干分发 → 洪泛检测 → `decideChoking` → `decideInterest` → `checkHave` → `sendKeepAlive` → 清理已完成 piece → `addRequests` 补流水线 → （每分钟）PEX → `sendPendingMessage` 统一冲刷。
- **决策与执行分离**：choke 算法由 `PeerChokeCommand` 每 10 秒在 `PeerStorage::executeChoke()` 全局重排一次，只写 `chokingRequired_`/`optUnchoking_` 标志；交互循环每 tick 读标志、仅在状态变化时发消息（边沿触发）。
- **三个节律器**：keepalive（默认 120s，可调 1~120）、洪泛检测（5 秒窗口内 ≥2 次 choke/unchoke 或 keepalive 即断连）、不活跃裁定（互不感兴趣 30s / 无数据流动 60s / 双 seeder 即断）。
- **PeerStorage 四张结构**：候选池 deque（≤512、FIFO）、在用集 set、`(IP,origPort)` 去重索引、dropped 列表（≤50，喂给 PEX）；外加坏 peer 随机 120~720 秒封禁表。
- **补人泵**：`ActivePeerConnectionCommand` 每 10 秒（magnet 模式 2 秒）按「做种态 / 下载态」两套条件最多补 5 个出站连接，0 连接时把 tracker announce 最小间隔压到 2 分钟自救。

## 7. 下一步学习建议

本讲把「一条连接上的循环」讲完了，但循环里两个关键部件只是点到为止：

1. **u6-l5（BtMessage 家族）**：`message->doReceivedAction()` 那一行背后的世界——每种消息的类层次、`DefaultBtMessageReceiver` 如何从字节流构造消息、`DefaultBtMessageDispatcher` 的发送队列与 `onSendReply` 回调。建议顺手精读 [src/DefaultBtMessageDispatcher.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageDispatcher.cc)，弄清 4.2 节每秒一次的 `checkRequestSlotAndDoNecessaryThing()` 到底清理了什么。
2. **u6-l6（请求流水线）**：本讲 `addRequests()`/`fillPiece()` 的下游——`DefaultBtRequestFactory` 如何把 piece 切成 16KB block 生成 request、超时请求如何重排、end-game 模式的请求策略切换。
3. **u6-l7（Tracker 交互）**：本讲 4.5 节的 `overrideMinInterval` 已经为 tracker 埋了伏笔，下一讲看 `DefaultBtAnnounce` 与 `TrackerWatcherCommand` 如何兑现它。
4. 想先横向拓展的读者可以对比 `DefaultBtInteractiveTest`（test 目录）如何脱离引擎单测这套循环——那是 4.1 节「分层好处」的最好注脚。
