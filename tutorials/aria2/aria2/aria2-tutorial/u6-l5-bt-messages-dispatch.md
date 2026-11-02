# BtMessage 家族：消息定义与分发执行

## 1. 本讲目标

上一讲（u6-l4）我们看到 `DefaultBtInteractive::doInteractionProcessing()` 每个 tick 都会「收干消息并分发」，但当时把消息层当黑盒。本讲打开这个黑盒，学完后你应该能够：

1. 解释 BT 消息「4 字节长度前缀 + 载荷」的线上格式，以及长度为 0 的 keepalive 特例是如何被解析状态机识别的。
2. 说出 `BtMessage` → `AbstractBtMessage` → `SimpleBtMessage`/`RangeBtMessage`/`ZeroBtMessage` → 各具体消息的类层次分工，并对比 request/piece/cancel/have/bitfield 等核心消息收到后各自做了什么。
3. 理解 `DefaultBtMessageReceiver`（收）→ `DefaultBtMessageFactory`（造）→ `DefaultBtMessageDispatcher`（发）三件套如何协作：工厂按消息 id 实例化对象并注入依赖，dispatcher 管理发送队列与在途请求账本（RequestSlot），并通过事件回调把「本方 choke 了」「对方 cancel 了」广播给队列中的消息。
4. 独立梳理任意一条消息（以 have none 为例）从 TCP 字节到达到回调触发的完整时序。

## 2. 前置知识

- **peer 消息线格式**：握手完成后，BT 协议的所有消息都是 `长度(4 字节大端) + 载荷`，其中载荷第一个字节是消息 id，其后是消息专属参数。长度是「载荷长度」（不含这 4 字节本身）。
- **keepalive 特例**：长度为 0 的消息没有载荷、没有 id，就是 keepalive（保活）。它在线上只有 4 个 0 字节。
- **choke/unchoke 语义**（回顾 u6-l4）：peer choking 表示对方不再给我发数据；am choking 表示我不再给对方发数据。interest 表示谁想要谁的数据。
- **RequestSlot（回顾 u3-l4/u6-l4）**：在途请求的记账条目，键是 `(index, begin, length)` 三元组。发出 request 消息时登记，收到对应 piece 消息时对账核销。
- **Fast Extension**：BEP-6 扩展，握手保留位协商。启用后才有 have all/have none/suggest/reject/allowed fast 这批消息（id 13~17）。
- **上传限速（回顾 u3-l5）**：BT 上传限速的超额判定在 `DefaultBtMessageDispatcher::sendMessagesInternal` 中执行——这正是本讲要精读的代码之一。
- **factory 模式**：把「根据输入构造正确子类对象」的逻辑集中到一个类，调用方只拿到基类指针。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/BtMessage.h` | 消息纯虚接口：`send`/`doReceivedAction`/`validate`/各事件回调 |
| `src/AbstractBtMessage.h` / `.cc` | 消息骨架基类：持有 Peer、PieceStorage、dispatcher、factory 等依赖指针 |
| `src/SimpleBtMessage.cc` | 通用 `send()` 实现：序列化 + 推入发送缓冲 |
| `src/PeerConnection.cc` | 「4 字节长度 + 载荷」三态接收状态机（线上格式的唯一裁判） |
| `src/bittorrent_helper.cc` | 消息编解码原语：`getId`/`getIntParam`/`createPeerMessageString`/`assertID` |
| `src/RangeBtMessage.h` / `src/ZeroBtMessage.h` | 两类消息模板：带 index+begin+length 的 13 字节载荷消息 / 只有 id 的 1 字节载荷消息 |
| `src/BtKeepAliveMessage.cc` | keepalive：序列化为 4 个 0 字节 |
| `src/DefaultBtMessageReceiver.cc` | 接收器：从 PeerConnection 拿载荷 → 交给工厂造消息对象 |
| `src/DefaultBtMessageFactory.cc` | 工厂：按消息 id switch 实例化具体消息类并注入依赖 |
| `src/DefaultBtMessageDispatcher.cc` / `.h` | 分发器：发送队列、RequestSlot 账本、事件广播 |
| `src/BtRequestMessage.cc` / `src/BtPieceMessage.cc` | 两个最繁忙的消息：下载数据的请求与交付 |
| `src/BtChokeMessage.cc` / `src/BtHaveMessage.cc` / `src/BtCancelMessage.cc` / `src/BtHaveNoneMessage.cc` | 几个代表性小消息 |
| `src/DefaultBtInteractive.cc` | 调用方视角：`receiveMessages()` 收干循环与各消息的入队时机 |
| `test/DefaultBtMessageDispatcherTest.cc` | dispatcher 单元测试（本讲实践的参照物） |

## 4. 核心概念与源码讲解

### 4.1 线上格式：4 字节长度前缀与 keepalive 特例

#### 4.1.1 概念说明

BT 的消息是自定界的：每条消息前面有 4 字节大端整数标明「后面的载荷有多长」。接收方不需要理解消息内容就能把字节流正确切成一条条消息——这就是 **长度前缀分帧（length-prefixed framing）**。它和 HTTP 的 `Content-Length`、u4-l6 chunked 编码解决的是同一个问题：TCP 是字节流，没有「消息边界」。

keepalive 是唯一没有载荷的消息：4 字节长度为 0，后面什么都没有。所以解析器必须在读满 4 字节长度时就地判定「这条消息到头了」，而不能等载荷。

#### 4.1.2 核心流程

接收状态机有三个状态，进度全部记在 `PeerConnection` 的成员变量里（`msgState_`、`msgOffset_`、`currentPayloadLength_`），因此跨多次 `receiveMessage()` 调用、跨粘包半包都安全：

```text
BT_MSG_PREV_READ_LENGTH  --读到第 1 个字节--> 记 msgOffset_, 进入读长度
BT_MSG_READ_LENGTH       --逐字节左移累加-->  读满 4 字节时:
                              长度+4 > 缓冲上限(64K+128) → 抛异常断连
                              长度 == 0 → keepalive! 立即返回(dataLength=0)
                              否则 → 进入读载荷
BT_MSG_READ_PAYLOAD      --缓冲中攒够 4+长度 字节--> 返回一条完整消息
```

发送方向的对称操作由 `bittorrent::createPeerMessageString` 完成：先写 4 字节载荷长度，再写 1 字节消息 id。

#### 4.1.3 源码精读

三态枚举定义（这就是整个分帧器的状态集合）：

- [src/PeerConnection.cc:L55-L64](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerConnection.cc#L55-L64) 定义 `BT_MSG_PREV_READ_LENGTH`、`BT_MSG_READ_LENGTH`、`BT_MSG_READ_PAYLOAD` 三个状态，分别对应「还没开始读长度」「正在读 4 字节长度」「在读载荷」。

长度字段的逐字节解析与 keepalive 特例：

- [src/PeerConnection.cc:L108-L124](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerConnection.cc#L108-L124) 每读一个字节就 `currentPayloadLength_ <<= 8; += c`（大端逐字节拼数）；凑满 4 字节时做两件事：若 `currentPayloadLength_ + 4 > bufferCapacity_` 则抛「载荷过长」异常（上限来自 [src/PeerConnection.h:L57](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerConnection.h#L57) 的 `MAX_BUFFER_CAPACITY = MAX_BLOCK_LENGTH + 128`，而 `MAX_BLOCK_LENGTH = 64KiB` 定义于 [src/BtConstants.h:L49](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtConstants.h#L49)）；若长度为 0，就地置 `done = true` 并回到初始状态——keepalive 在这里被拦截，根本不会进入载荷阶段。

载荷攒齐判定与「零拷贝」返回：

- [src/PeerConnection.cc:L126-L139](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerConnection.cc#L126-L139) 检查缓冲里从 `msgOffset_` 起是否已有 `4 + currentPayloadLength_` 字节；不足则继续读 socket。
- [src/PeerConnection.cc:L143-L149](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerConnection.cc#L143-L149) 完成时若调用方传了输出缓冲才 `copy_n`，`dataLength` 恒被设置为载荷长度——即使长度为 0 也返回 `true`。
- [src/PeerConnection.cc:L274-L277](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerConnection.cc#L274-L277) `getMsgPayloadBuffer()` 返回接收缓冲内部「跳过 4 字节长度」的位置，让上层（receiver）可以不拷贝就把载荷交给工厂解析。

发送侧的编码原语（与上面的解码严格对称）：

- [src/bittorrent_helper.cc:L838-L845](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L838-L845) `createPeerMessageString` 先 memset 清零，再用 `setIntParam` 写 4 字节载荷长度、`msg[4]` 写消息 id——所有消息的序列化都从这一个函数起步。
- [src/bittorrent_helper.cc:L756-L761](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L756-L761) `getIntParam` 用 `memcpy + ntohl` 做网络序到主机序转换，是所有「index/begin/length」参数的统一读法；`getId`（[src/bittorrent_helper.cc:L747](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L747)）就是取载荷第 0 字节。
- [src/bittorrent_helper.cc:L893-L899](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L893-L899) `assertID` 在工厂构造消息时做「载荷里的 id 真的是这个类的 id 吗」的兜底校验，不符即抛 `DL_ABORT_EX`。

三个「形状」模板，覆盖了几乎全部消息的编解码：

- [src/RangeBtMessage.h:L49-L60](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RangeBtMessage.h#L49-L60) `RangeBtMessage` 描述 `id + index(4) + begin(4) + length(4)` 共 13 字节载荷的消息（request/cancel/reject），整条消息 17 字节；`create` 模板统一断言载荷长度等于 13、id 匹配。
- [src/ZeroBtMessage.h:L43-L62](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ZeroBtMessage.h#L43-L62) `ZeroBtMessage` 描述只有 id、载荷长 1 字节的消息（choke/unchoke/interested/not interested/have all/have none），整条 5 字节。
- [src/BtKeepAliveMessage.cc:L42-L49](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtKeepAliveMessage.cc#L42-L49) keepalive 的 `createMessage` 直接返回长度为 `MESSAGE_LENGTH`（4）的全零 vector——完美对应解析侧「长度 0 无载荷」的特例。

#### 4.1.4 代码实践

**实践：手工编解码一条 request 消息（源码阅读 + 手算，无需编译）**

1. **实践目标**：不看运行结果，仅凭源码推出一条具体消息的完整字节序列，验证你真正掌握了长度前缀分帧。
2. **操作步骤**：
   - 假设我们要请求「piece 3 内偏移 16384 处的 16384 字节」，即 request 消息 `index=3, begin=16384, length=16384`，消息 id 为 6（[src/BtRequestMessage.h:L50](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtRequestMessage.h#L50)）。
   - 对照 `RangeBtMessage::createMessage`（[src/RangeBtMessage.h:L49](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RangeBtMessage.h#L49)，总长 17 字节）与 `createPeerMessageString`/`setIntParam`，手写 17 个字节：前 4 字节是载荷长度 13（`00 00 00 0D`），第 5 字节是 id（`06`），随后 index（`00 00 00 03`）、begin（`00 00 40 00`）、length（`00 00 40 00`）各 4 字节大端。
   - 再手写一条 keepalive：4 个 `00`，共 4 字节。
   - 把两组字节当作输入，人肉走一遍 [src/PeerConnection.cc:L108-L139](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerConnection.cc#L108-L139) 的状态机，确认：request 会在「读载荷」状态攒满 17 字节后返回；keepalive 在读满第 4 个长度字节时立刻返回且 `dataLength=0`。
3. **需要观察的现象**：状态机的游标推进逻辑——尤其 keepalive 分支不进入 `BT_MSG_READ_PAYLOAD`。
4. **预期结果**：request 序列化为 `00 00 00 0D 06 00 00 00 03 00 00 40 00 00 00 40 00`；keepalive 为 `00 00 00 00`。（可再用 `printf` 或 xxd 生成对照，属可本地验证项。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `receiveMessage` 的长度检查是 `currentPayloadLength_ + 4 > bufferCapacity_` 而不是 `currentPayloadLength_ > bufferCapacity_`？
**答案**：缓冲里必须同时装下「4 字节长度 + 载荷」才能完成分帧（参见 [src/PeerConnection.cc:L113](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerConnection.cc#L113) 与 L130 的 `4 + currentPayloadLength_` 判定）。留不出这 4 字节余量的消息永远凑不齐，与其半途卡死不如立即报「载荷过长」断连。

**练习 2**：aria2 单条 BT 消息载荷最大能是多少？由谁决定？
**答案**：约 64KiB + 124 字节。`MAX_BUFFER_CAPACITY = MAX_BLOCK_LENGTH + 128`（[src/PeerConnection.h:L57](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerConnection.h#L57)），其中 `MAX_BLOCK_LENGTH = 64KiB`（[src/BtConstants.h:L49](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtConstants.h#L49)）。而最大的消息 piece 载荷是 `9 + 16KiB`（块上限 16KiB），远小于此上限。

**练习 3**：keepalive 在解析侧不进入载荷状态，那它在工厂里会被造成什么对象？
**答案**：`dataLength == 0` 时工厂直接造 `BtKeepAliveMessage`（见 4.3 的 switch 前特判）。它的 `ID = 99` 只是进程内部的哨兵值（[src/BtKeepAliveMessage.h:L49](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtKeepAliveMessage.h#L49)），线上根本不存在这个 id。

### 4.2 BtMessage 家族：接口、骨架与类层次

#### 4.2.1 概念说明

aria2 为每种线上消息定义一个 C++ 类，全部实现 `BtMessage` 纯虚接口。类层次是：

```text
BtMessage                        纯虚接口：send / doReceivedAction / validate / 事件回调
└─ AbstractBtMessage             骨架：持有 peer、pieceStorage、dispatcher、factory 等依赖
   ├─ SimpleBtMessage            通用 send()：createMessage() 序列化 → pushBytes
   │  ├─ ZeroBtMessage           载荷只有 id 的消息（choke/unchoke/haveAll/haveNone…）
   │  │  └─ BtChokeMessage / BtUnchokeMessage / …
   │  ├─ RangeBtMessage          载荷 = id + index + begin + length（request/cancel/reject）
   │  │  └─ BtRequestMessage / BtCancelMessage / BtRejectMessage
   │  ├─ BtHaveMessage / BtBitfieldMessage / BtPortMessage / BtKeepAliveMessage …
   │  └─ BtPieceMessage          特殊：头部 + 大块数据，单独优化了序列化
   └─ BtHandshakeMessage / BtExtendedMessage   不走 SimpleBtMessage 的特例
```

消息的「行为」集中在四个钩子，分别在不同时机被调：

| 钩子 | 调用时机 | 调用者 |
| --- | --- | --- |
| `onQueued()` | 消息被排入发送队列的瞬间 | dispatcher 的 `addMessageToQueue` |
| `validate()` | 收到的消息构造完成后、`doReceivedAction` 前 | receiver |
| `doReceivedAction()` | 收到消息、本方要执行其语义 | `DefaultBtInteractive::receiveMessages` |
| `send()` | 队列冲刷、真正要写 socket 时 | dispatcher 的 `sendMessagesInternal` |
| `onChokingEvent` / `onCancelSendingPieceEvent` / `onAbortOutstandingRequestEvent` | 本方状态变化时向队列内消息广播 | dispatcher 的 `doChokingAction` 等 |

> **勘误（相对本讲大纲）**：大纲里提到的 `onSendReply` 回调在当前源码中**并不存在**（全 src 目录检索无此符号）。真实的回调族就是上表所列的 `onQueued`/`doReceivedAction`/`onChokingEvent`/`onCancelSendingPieceEvent`/`onAbortOutstandingRequestEvent`，请以本表为准。

各消息的线上 id（定义在各头文件的 `static const ... ID`）：

| id | 消息 | 载荷形状 |
| --- | --- | --- |
| 0/1 | choke / unchoke | 仅 id（ZeroBtMessage） |
| 2/3 | interested / not interested | 仅 id |
| 4 | have | id + index(4) |
| 5 | bitfield | id + 位图（长度可变） |
| 6/7/8 | request / piece / cancel | id + index(4) + begin(4) + length(4)（Range 系） |
| 9 | port | id + 端口(2)，DHT 用 |
| 13~17 | suggest / haveAll / haveNone / reject / allowedFast | Fast Extension（BEP-6） |
| 20 | extended | 扩展协议（BEP-10，下一讲主角） |
| （无） | keepalive | 空载荷 |

#### 4.2.2 核心流程

一条「发送」消息的生命周期：

```text
业务代码(如 DefaultBtInteractive::decideChoking)
  → factory.createXxxMessage(...)        构造 + setCommonProperty 注入依赖
  → dispatcher->addMessageToQueue(msg)   先回调 onQueued()（request 在这里登记 RequestSlot）
  → dispatcher->sendMessages()           每 tick 统一冲刷
       → msg->send()                     createMessage() 序列化
       → peerConnection->pushBytes()     进入 SocketBuffer 部分写
```

一条「接收」消息则相反：工厂造好后由 `DefaultBtInteractive` 调 `doReceivedAction()`，消息在钩子里通过基类保存的依赖指针反手调用 dispatcher/factory，产生新消息入队（详见 4.4）。

#### 4.2.3 源码精读

接口契约：

- [src/BtMessage.h:L50-L82](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtMessage.h#L50-L82) `BtMessage` 声明了全部行为钩子：`isInvalidate`/`isUploading`（dispatcher 冲刷时用来过滤）、`doReceivedAction`/`send`/`validate`、三个事件回调与 `onQueued`。注意 `getId` 是非虚的——id 在构造时写死。

骨架基类的「依赖注入面板」：

- [src/AbstractBtMessage.h:L53-L73](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractBtMessage.h#L53-L73) 成员一览：`invalidate_`（作废标记）、`uploading_`（是否上传类消息，限速用）、`cuid_`、`name_`，以及六个裸指针依赖 `pieceStorage_`/`peer_`/`dispatcher_`/`messageFactory_`/`requestFactory_`/`peerConnection_`，还有可选的 `validator_` 与 `metadataGetMode_` 标志。消息对象不拥有这些资源，只借用。
- [src/AbstractBtMessage.h:L109-L126](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractBtMessage.h#L109-L126) 五个回调的默认实现全为空——绝大多数消息不关心大多数事件，只有需要的类才覆写（如 `BtPieceMessage` 覆写 `onChokingEvent`）。
- [src/AbstractBtMessage.cc:L64-L69](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractBtMessage.cc#L64-L69) `validate()` 把校验委托给可选的 `validator_`（策略对象），没有就跳过。validator 家族（`IndexBtMessageValidator`/`RangeBtMessageValidator`/`BtBitfieldMessageValidator`…）负责「index 是否越界、begin+length 是否落在 piece 内」这类合法性检查。

通用发送实现：

- [src/SimpleBtMessage.cc:L51-L63](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SimpleBtMessage.cc#L51-L63) `send()` 三步：`isInvalidate()` 或 `sendPredicate()` 不满足则直接返回（消息被取消后的「无声退场」）；否则 `createMessage()` 序列化并打 INFO 日志；最后 `pushBytes` 推入 socket 发送缓冲。小消息的序列化都在各自 `createMessage()` 里完成。

两个「重量级」消息的特殊处理：

- [src/BtPieceMessage.cc:L69-L79](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtPieceMessage.cc#L69-L79) 构造函数调用 `setUploading(true)`——piece 是唯一的上传类消息，dispatcher 限速判定靠这个标志。它不走 `createMessage()` 整体序列化，而是「13 字节头 + 数据」分开处理（见 4.4.3）。
- [src/BtPieceMessage.cc:L158-L171](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtPieceMessage.cc#L158-L171) `createMessageHeader` 只序列化前 13 字节（长度=9+块长、id=7、index、begin），真正的数据在 `send()` 时从磁盘读出拼接——避免为 16KiB 块做多一次完整拷贝。

#### 4.2.4 代码实践

**实践：用日志观察一次真实会话中的消息种类（需可运行环境；不可运行则改为阅读测试）**

1. **实践目标**：把「类层次表」和真实流量对上号。
2. **操作步骤**：编译 aria2 后运行任一种子下载，加 `./src/aria2c --enable-rpc=false -l log.txt --log-level=info <torrent>`（info 级即可，消息收发都有日志）。在日志里 grep `receive peer message` 与 `send peer message`（[src/SimpleBtMessage.cc:L56](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SimpleBtMessage.cc#L56) 的 `MSG_SEND_PEER_MESSAGE` 与 [src/DefaultBtInteractive.cc:L315](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L315) 的 `MSG_RECEIVE_PEER_MESSAGE`）。
3. **需要观察的现象**：每条日志都带 `message->toString()` 的输出，如 `request index=0, begin=0, length=16384`、`keep alive`。
4. **预期结果**：一次正常下载中 request/piece 数量最多，have/bitfield 握手期出现，choke/unchoke 每 10 秒左右一次（PeerChokeCommand 周期）。**待本地验证**（无种子环境时可改为阅读 `test/DefaultBtMessageDispatcherTest.cc` 的用例清单想象消息流）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `BtPieceMessage` 要覆盖 `onChokingEvent`，而 `BtChokeMessage` 不需要？
**答案**：`onChokingEvent` 在「本方刚决定 choke 对方」时广播给**发送队列里**的消息。piece 消息是上传数据，对方被 choke 后原则上不该再发（除非在 allowed fast 集合内），所以它要在回调里自我作废或改发 reject（见 4.4.3）；choke 消息本身在队列里瞬间即发，不关心这个事件，用基类空实现即可。

**练习 2**：`metadataGetMode_` 标志是干什么的？
**答案**：magnet 元数据获取模式下（u6-l9），本地还没有真实 piece 布局，多数消息的 `doReceivedAction` 与工厂的 validator 都不能按正常逻辑跑（如 [src/BtRequestMessage.cc:L62-L64](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtRequestMessage.cc#L62-L64) 直接 return，工厂侧跳过 validator 装配）。工厂统一经 [src/DefaultBtMessageFactory.cc:L246-L248](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageFactory.cc#L246-L248) 给消息打上该标志。

**练习 3**：`isInvalidate()` 为 true 的消息还在队列里，会发生什么？
**答案**：`send()` 开头直接 return（[src/SimpleBtMessage.cc:L53-L55](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SimpleBtMessage.cc#L53-L55)），消息被「无声跳过」，随后随队列析构销毁。作废而不删除的原因是消息对象可能仍被 dispatcher 的迭代器引用，标记位是最安全的退出方式。

### 4.3 DefaultBtMessageReceiver 与 DefaultBtMessageFactory：从字节到对象

#### 4.3.1 概念说明

`DefaultBtMessageReceiver` 是「字节 → 消息对象」的搬运工：它调用 4.1 的分帧器拿到一条完整载荷，再交给工厂。自己几乎不做协议判断——**「这是什么消息、参数是什么、合不合法」全部由工厂与消息类自己决定**。这样 receiver 的逻辑极薄，也方便单元测试时用 Mock 工厂替换。

`DefaultBtMessageFactory` 则是唯一的「消息 id → C++ 类」映射点。它有两类入口：

- **解析入口** `createBtMessage(data, dataLength)`：处理收到的字节，按 id switch 实例化。收到的消息只填参数，不预置发送所需的一切。
- **构造入口** `createXxxMessage(参数...)`：给业务代码造「待发送」的消息，参数直接来自业务对象（如 piece、index）。

两类入口最后都调 `setCommonProperty`，把 cuid、peer、pieceStorage、dispatcher、requestFactory、peerConnection、factory 自身这组依赖一次性注入——这就是消息对象在钩子里能「反手」调用 dispatcher 的原因。

#### 4.3.2 核心流程

接收一条消息的完整流水（在 `DefaultBtInteractive::receiveMessages` 的循环里反复执行）：

```text
peerConnection_->receiveMessage(nullptr, dataLength)
   false → 无完整消息，退出循环
   true  → dataLength = 载荷长度（0 表示 keepalive）
messageFactory_->createBtMessage(getMsgPayloadBuffer(), dataLength)
   dataLength==0 → BtKeepAliveMessage
   否则 getId(data) 按下表 switch → 各类 XxxMessage::create(data, len)
   （create 内部 assertPayloadLength/assertID + 解析参数）
   → 装配 validator（可被 metadataGetMode 跳过）→ setCommonProperty
msg->validate()          边界校验，失败抛 DL_ABORT_EX
（piece 消息特例：setMsgPayload 让消息直接引用接收缓冲里的数据）
```

#### 4.3.3 源码精读

receiver 主函数只有十几行：

- [src/DefaultBtMessageReceiver.cc:L120-L135](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageReceiver.cc#L120-L135) `receiveMessage` 的四个动作：(1) 给 `receiveMessage` 传 `nullptr` 输出缓冲，迫使分帧器**不拷贝**载荷（L123-125 注释写明就是为了省 memcpy）；(2) 用 `getMsgPayloadBuffer()` 把缓冲内部指针交给工厂；(3) `msg->validate()`；(4) 若是 piece 消息，再 `setMsgPayload` 保存数据指针（L130-133）——所以 `BtPieceMessage::data_` 指向的是 PeerConnection 的接收缓冲，消息对象存活期间缓冲不能被复用。
- [src/DefaultBtMessageReceiver.cc:L65-L104](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageReceiver.cc#L65-L104) 握手期专用的 `receiveHandshake`：正常路径（L73-80）收满 68 字节交工厂并校验；`quickReply` 分支（L81-102）处理 tracker 的 NAT-checking——tracker 只发握手不收应答，aria2 检查对方 info_hash 匹配后**抢先回握手**。这是 receiver 里唯一带策略的逻辑。

工厂的解析入口（本讲的核心 switch）：

- [src/DefaultBtMessageFactory.cc:L91-L99](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageFactory.cc#L91-L99) `dataLength == 0` 直接造 keepalive——4.1 解析侧「长度 0 无 id」特例在这里落地为具体类型。
- [src/DefaultBtMessageFactory.cc:L100-L147](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageFactory.cc#L100-L147) `uint8_t id = bittorrent::getId(data)` 后逐 case 分派。几个有代表性的分支：have（L121-128）装配 `IndexBtMessageValidator` 防越界；request（L137-147）装配 `RangeBtMessageValidator`，用「piece 总数 + 该 piece 长度」确保 `index/begin/length` 落在合法区间——**先取参数再建 validator** 的顺序保证校验用的就是消息自己的值。
- [src/DefaultBtMessageFactory.cc:L148-L160](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageFactory.cc#L148-L160) piece 分支额外注入 `downloadContext_` 与 `peerStorage_`（写盘与坏 peer 记录要用）；注意此时**不拷贝数据**，数据指针由 receiver 随后补上。
- [src/DefaultBtMessageFactory.cc:L194-L196](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageFactory.cc#L194-L196) `BtHaveNoneMessage::ID`（15）分支——最简单的 case，两行完事，因为 have none 没有参数、也没有 validator。
- [src/DefaultBtMessageFactory.cc:L218-L230](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageFactory.cc#L218-L230) extended 分支（id=20）：必须 `peer_->isExtendedMessagingEnabled()` 才解析，否则抛异常；未知 id 走 default 抛 `Invalid message ID` 断连——工厂是协议合法性的第一道关卡。
- [src/DefaultBtMessageFactory.cc:L237-L249](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageFactory.cc#L237-L249) `setCommonProperty`：七个 setter 把依赖一次性注入消息。注意 L244 `msg->setBtMessageFactory(this)`——把工厂自己也交给消息，消息在钩子里就能随手再造新消息入队。

构造入口的两个例子：

- [src/DefaultBtMessageFactory.cc:L272-L280](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageFactory.cc#L272-L280) `createRequestMessage(piece, blockIndex)` 把「块号」换算成线上参数：`begin = blockIndex * 块长`、`length = 该块实际长度`（尾块可能不足），并保留 `blockIndex` 供 `onQueued` 建 RequestSlot。
- [src/DefaultBtMessageFactory.cc:L340-L347](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageFactory.cc#L340-L347) `createBitfieldMessage` 直接从 `pieceStorage_->getBitfield()` 拷贝本方位图——发送侧 bitfield 永远反映当前真实进度。

调用方（收干循环）在 `DefaultBtInteractive`：

- [src/DefaultBtInteractive.cc:L301-L339](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L301-L339) `receiveMessages` 是 receiver 的唯一生产调用点：循环直到分帧器返回空或限速；每条消息打 INFO 日志后立即 `doReceivedAction()`（L318），然后 switch 做**洪泛统计**（choke/unchoke/keepalive 频次，L320-338）与不活跃计时刷新（request/piece 重置 inactiveTimer）。也就是说：消息语义在消息类里，会话级策略在 interactive 里，职责清晰分离。

#### 4.3.4 代码实践

**实践：给工厂的 switch 加一条「消息统计」日志（源码修改型，不破坏行为）**

1. **实践目标**：亲眼确认「id → 类」映射的执行路径与命中频率。
2. **操作步骤**：
   - 在 [src/DefaultBtMessageFactory.cc:L100-L102](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageFactory.cc#L100-L102) 的 switch 之前加一行（示例代码，非项目原有）：
     ```cpp
     A2_LOG_DEBUG(fmt("Factory dispatching message id=%u, len=%lu", id, static_cast<unsigned long>(dataLength)));
     ```
   - 重新 `make`，用 `--log-level=debug` 跑一个种子下载几分钟。
   - （替代方案，不编译）直接阅读 [test/DefaultBtMessageDispatcherTest.cc:L27-L39](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DefaultBtMessageDispatcherTest.cc#L27-L39) 的用例清单，对照本节 switch 找出哪些 case 有测试覆盖。
3. **需要观察的现象**：日志中 id 的分布——预期 6/7（request/piece）占绝对多数，4（have）次之，0/1（choke/unchoke）呈约 10 秒周期。
4. **预期结果**：能按 id 频次排出「最繁忙消息 Top3」，并解释：request/piece 是数据面的往返单元，have 是控制面的进度广播。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `receiveMessage` 宁可传 `nullptr` 也不传一个输出缓冲？
**答案**：分帧器对 `data != nullptr` 的情况会 `copy_n` 整个载荷（[src/PeerConnection.cc:L143-L147](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerConnection.cc#L143-L147)）。piece 消息载荷可达 16KiB，绕过这次拷贝、让消息对象直接引用接收缓冲（`getMsgPayloadBuffer` + `setMsgPayload`），是 BT 数据面的重要省拷贝优化。

**练习 2**：收到一条 id=42 的消息会怎样？
**答案**：switch 落到 default，抛 `DL_ABORT_EX("Invalid message ID. id=42")`（[src/DefaultBtMessageFactory.cc:L229-L230](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageFactory.cc#L229-L230)）。该异常沿 `doReceivedAction` 的调用栈上传，被 `PeerInteractionCommand`/`PeerAbstractCommand` 捕获后断开该 peer 连接（不影响其他 peer）。

**练习 3**：收到的 `BtHaveMessage` 与发送用的 `BtHaveMessage` 是同一个类吗？构造路径有何不同？
**答案**：同一个类，但走工厂的两类入口。收到的走 `BtHaveMessage::create(data, len)`（校验长度 9、解析 index），发送的走 `createHaveMessage(index)`（[src/DefaultBtMessageFactory.cc:L301-L307](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageFactory.cc#L301-L307)，参数直接给定）。

### 4.4 DefaultBtMessageDispatcher：发送队列、在途账本与事件广播

#### 4.4.1 概念说明

dispatcher 是每个 peer 会话的「消息中枢」，持有两条队列（见 [src/DefaultBtMessageDispatcher.h:L57-L65](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageDispatcher.h#L57-L65)）：

- `messageQueue_`：待发送消息队列（`deque<unique_ptr<BtMessage>>`）。业务代码不直接写 socket，只把消息塞进来，每个 tick 由 `sendMessages` 统一冲刷。
- `requestSlots_`：在途请求账本（`deque<unique_ptr<RequestSlot>>`）。发出 request 时登记，收到 piece 时核销——**request 与 piece 不是请求响应对象，靠这个账本对账**。

它还提供三类「动作」方法（`doChokedAction`/`doChokingAction`/`doCancelSendingPieceAction`/`doAbortOutstandingRequestAction`），供消息的 `doReceivedAction` 或 interactive 调用，把本方状态变化**广播回发送队列**——比如本方刚 choke 对方时，队列里还没发出去的 piece 消息应该作废。这是典型的「观察者」模式：队列中的消息是观察者，事件回调是通知。

#### 4.4.2 核心流程

一个 tick 内 dispatcher 参与的三件事（对应 u6-l4 的 `doInteractionProcessing` 顺序）：

```text
① checkRequestSlotAndDoNecessaryThing()（每秒一次）
     扫 requestSlots_：超时 → cancelBlock + peer 标记 snubbing
                       块已被别处补齐 → 补发 cancel 消息（end-game 去重）
② receiveMessages() → 各消息 doReceivedAction()
     choke   → peerChoking(true) + dispatcher->doChokedAction()（清不受宠请求槽）
     request → 有货且未 choke → piece 消息入队；Fast 扩展且无货/被 choke → reject 入队
     piece   → 账本核销 → 写盘 → 块完成；piece 完成 → 哈希校验 → completePiece
     cancel  → dispatcher->doCancelSendingPieceAction()（广播给队列作废同参数 piece）
③ sendMessages()
     遍历 messageQueue_：uploading 消息遇上传限速 → 挪回队首等下轮
     其余 msg->send() → 序列化进 SocketBuffer → sendPendingData 写 socket
```

#### 4.4.3 源码精读

入队与「先回调后入队」的次序：

- [src/DefaultBtMessageDispatcher.cc:L79-L84](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageDispatcher.cc#L79-L84) `addMessageToQueue` 先调 `onQueued()` 再 push_back。这个次序对 request 至关重要：[src/BtRequestMessage.cc:L81-L86](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtRequestMessage.cc#L81-L86) 的 `onQueued` 用消息自带的 `(index, begin, length, blockIndex, piece)` 构造 RequestSlot 登记进账本——**请求一入队就被记账**，哪怕还没真正发出去，这样 piece 回来时一定能对上账。

冲刷与上传限速：

- [src/DefaultBtMessageDispatcher.cc:L86-L106](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageDispatcher.cc#L86-L106) `sendMessagesInternal` 逐条取出：若 `isUploading()`（基本只有 piece 消息）且「全局超限或本任务超限」，塞进 `tempQueue` 暂存并 `continue`；其余直接 `send()`。最后把被扣下的消息**插回队首**（L101-105），保序且下轮优先重试。这就是 u3-l5 讲过的「BT 上传限速三处执行点」之一。
- [src/DefaultBtMessageDispatcher.cc:L108-L114](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageDispatcher.cc#L108-L114) `sendMessages` 外层门槛：SocketBuffer 里排队的数据块数已达 `A2_IOV_MAX` 就先不产生新数据，只调 `sendPendingData()` 把存量写出去（writev 一次能挂的块数有上限，攒多了没意义）。

账本的核销与查询：

- [src/DefaultBtMessageDispatcher.cc:L260-L271](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageDispatcher.cc#L260-L271) `getOutstandingRequest(index, begin, length)` 线性查找三元组匹配的槽位——piece 消息到达时拿它对账。
- [src/DefaultBtMessageDispatcher.cc:L273-L284](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageDispatcher.cc#L273-L284) `removeOutstandingRequest` 核销槽位：先调 `abortOutstandingRequest`（即 `piece->cancelBlock`，把该块标记回「未持有」，L149-157），再从 deque 删除。

每秒巡检（超时与 end-game 去重）：

- [src/DefaultBtMessageDispatcher.cc:L215-L242](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageDispatcher.cc#L215-L242) `checkRequestSlotAndDoNecessaryThing`：槽位超时（`--bt-request-timeout`，默认 60 秒）→ 取消块并 `peer_->snubbing(true)`（u6-l4 讲过：被 snub 的 peer 上传不再计入 choke 决策）；块的 piece 若已从别处凑齐（end-game 多源竞速的常态）→ **主动补发 cancel**（L231-233），告诉对方别再发了。两类槽位随后统一从账本 erase。

事件广播三连（把本方状态变化通知队列）：

- [src/DefaultBtMessageDispatcher.cc:L205-L213](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageDispatcher.cc#L205-L213) `doChokingAction`：本方刚决定 choke（由 `DefaultBtInteractive::decideChoking` 在发 choke 消息的同时调用，见 [src/DefaultBtInteractive.cc:L248-L263](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L248-L263)），向队列广播 `onChokingEvent`。
- [src/DefaultBtMessageDispatcher.cc:L130-L139](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageDispatcher.cc#L130-L139) `doCancelSendingPieceAction`：对方发来 cancel（[src/BtCancelMessage.cc:L53-L60](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtCancelMessage.cc#L53-L60) 的 `doReceivedAction` 就是转调它），向队列广播 `onCancelSendingPieceEvent`。
- [src/DefaultBtMessageDispatcher.cc:L161-L182](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageDispatcher.cc#L161-L182) `doAbortOutstandingRequestAction`：某个 piece 被判定坏块重来时，清掉该 piece 的全部在途槽位（`cancelBlock` 归还块），再广播 `onAbortOutstandingRequestEvent` 让对应的待发 request 消息作废。
- 注意三者都先用 `toRawPointers`（L116-127）把队列拍成裸指针快照再遍历——回调里消息可能调 `addMessageToQueue` 产生新消息，快照迭代避免迭代器失效。

广播的接收端（piece 消息的两个事件回调）：

- [src/BtPieceMessage.cc:L295-L307](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtPieceMessage.cc#L295-L307) `onChokingEvent`：对方被本方 choke 后，若该 piece 不在「allowed fast 白名单」里，Fast 扩展开启时改发 reject（礼貌拒绝），然后 `setInvalidate(true)` 让自己被跳过。
- [src/BtPieceMessage.cc:L309-L323](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtPieceMessage.cc#L309-L323) `onCancelSendingPieceEvent`：参数完全匹配（index/begin/length 三者相等）才作废——粗粒度匹配会误杀别的请求。

两个最繁忙消息的 `doReceivedAction`（数据面核心）：

- [src/BtRequestMessage.cc:L60-L79](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtRequestMessage.cc#L60-L79) 收到对方的 request：本地有这块数据且（未 choke 对方 或 该 index 在 allowed fast 集合内）→ 造 piece 消息入队；否则若 Fast 扩展开启 → 造 reject 入队。**非 Fast 模式下无法拒绝，只能沉默**——这正是 Fast 扩展要解决的问题。
- [src/BtPieceMessage.cc:L95-L152](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtPieceMessage.cc#L95-L152) 收到 piece 是整个数据面最长的钩子，逐段看：L100-105 先 `getOutstandingRequest` 对账（没账就只记流量不写盘，L147-151——防对方硬塞数据）；对上账后 `updateDownload` 记流量、`snubbing(false)` 解除慢速标记；L114-117 块已凑齐则丢弃（end-game 竞速的常态）；L118-129 写盘两路——有 WrDiskCache 走缓存（多一次拷贝），否则 `DiskAdaptor::writeData` 直写，`data_ + 9` 正是「跳过 id+index+begin 共 9 字节头」后的纯数据；L130-135 `completeBlock` + `removeOutstandingRequest` 核销 + 增量哈希；L136-145 piece 集齐后立刻验哈希，通过则 `onNewPiece`（冲缓存、`completePiece`、广播 have 的准备），失败则清块重来并把对方记为坏 peer。
- [src/BtPieceMessage.cc:L201-L234](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtPieceMessage.cc#L201-L234) 发送侧 `send`/`pushPieceData`：分配「13 字节头 + 块长」的缓冲，写完头后从磁盘 `readData` 读出块数据，连同 `PieceSendUpdate`（按实际写出字节数更新双方上传统计，L179-198）一起 `pushBytes`。读不满（文件异常）直接抛异常断连。

流水线自适应（调用方侧，衔接 u6-l6）：

- [src/DefaultBtInteractive.cc:L341-L348](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L341-L348) 一轮收消息后，若非 end-game 且核销的请求数占比够大（说明对方响应快），就把 `maxOutstandingRequest_` 翻倍（上限 `UB_MAX_OUTSTANDING_REQUEST`）——「账本核销量」直接驱动流水线深度调优，这正是账本存在的第二个价值。

#### 4.4.4 代码实践

**实践：读懂 dispatcher 的单元测试桩件（纯阅读型实践）**

1. **实践目标**：理解「事件广播」如何被测试捕获，体会 dispatcher 与消息的解耦。
2. **操作步骤**：
   - 阅读 [test/DefaultBtMessageDispatcherTest.cc:L189-L203](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DefaultBtMessageDispatcherTest.cc#L189-L203) `testDoCancelSendingPieceAction`：构造两个 `MockBtMessage2`（测试目录的桩件，内部用 `EventCheck` 记录「哪个回调被调过」），都 `addMessageToQueue` 入队，然后调 `doCancelSendingPieceAction(0,0,0)`，断言两个桩的 `doCancelActionCalled` 都为 true。
   - 再看 [test/DefaultBtMessageDispatcherTest.cc:L209-L219](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DefaultBtMessageDispatcherTest.cc#L209-L219) `testCheckRequestSlotAndDoNecessaryThing`：手工构造 `Piece(0, 16K)` 并 `addOutstandingRequest` 一个 RequestSlot——演示了如何在没有真实 peer 的情况下驱动账本逻辑。
3. **需要观察的现象**：测试完全没有 socket、没有 PeerConnection，dispatcher 的广播逻辑照常运行。
4. **预期结果**：你能说出「dispatcher 只依赖 BtMessage 接口，不依赖任何具体消息类」这一解耦设计的可测性收益；并且明白为什么生产代码里广播前要先做 `toRawPointers` 快照。

#### 4.4.5 小练习与答案

**练习 1**：对方发来一条我们没有请求过的 piece 消息，aria2 会把它写盘吗？
**答案**：不会。`doReceivedAction` 先对账（[src/BtPieceMessage.cc:L100-L105](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtPieceMessage.cc#L100-L105)），`slot` 为空时只记流量统计并打一条 "RequestSlot not found" 日志（L147-151）。这防止恶意 peer 硬塞垃圾数据污染文件。

**练习 2**：为什么被上传限速扣下的 piece 消息要「插回队首」而不是队尾？
**答案**：`sendMessagesInternal` 用 `while` 从队首逐条取（[src/DefaultBtMessageDispatcher.cc:L89-L105](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageDispatcher.cc#L89-L105)），若插到队尾，中间新入队的消息（如 have、keepalive 等小消息）会先发，piece 的到达顺序无法保证接近请求顺序；插回队首保证限速解除后数据面消息优先、尽量按请求序发送。

**练习 3**：request 的 `onQueued` 里登记了 RequestSlot，那 cancel 消息发出后账本会同步清理吗？
**答案**：不会由 cancel 消息自己清理。cancel 的语义是「通知对方别发了」，本方账本的清理发生在三个地方：收到 piece 对账核销（`removeOutstandingRequest`）、每秒巡检发现超时或块已凑齐（`checkRequestSlotAndDoNecessaryThing` 的 erase）、以及坏块重来时的 `doAbortOutstandingRequestAction`。cancel 消息本身正是「块已凑齐」巡检的**产物**（L231-233），先有清理后有 cancel。

## 5. 综合实践

**任务：完整梳理 `BtHaveNoneMessage`（have none，id=15）的双向路径，画出从字节到回调的时序图。**

have none 是 Fast Extension 的「我一块都没有」速记消息——语义上等价于发送一张全 0 的 bitfield，但只需 5 字节。选它做综合练习是因为它同时经过本讲的每一个模块且路径最短，适合亲手把全链路走一遍。

**建议步骤**（找一个空闲时段完成，全程源码阅读 + 手绘时序图，可选做日志验证）：

1. **发送侧起点**：阅读 [src/DefaultBtInteractive.cc:L213-L231](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L213-L231) `addBitfieldMessageToQueue`，找到 have none 的发送条件：Fast 扩展开启、非全部完成、且 `getCompletedLength() == 0`（一块都没有才用 have none 代替 bitfield）。
2. **工厂构造**：[src/DefaultBtMessageFactory.cc:L365-L371](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageFactory.cc#L365-L371) `createHaveNoneMessage` → [src/BtHaveNoneMessage.cc:L44](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtHaveNoneMessage.cc#L44) 构造为 `ZeroBtMessage`。确认 `setCommonProperty` 注入了哪七项依赖。
3. **入队与序列化**：`addMessageToQueue`（先回调空的 `onQueued`）→ 下个冲刷点 `sendMessages` → `SimpleBtMessage::send` → `ZeroBtMessage` 的 `createMessage`（5 字节：`00 00 00 01 0F`）→ `pushBytes`。
4. **对端接收**（把你自己当作对端）：TCP 字节到达 → `PeerConnection::receiveMessage` 状态机读出 `00 00 00 01`（长度 1，非 keepalive）→ 攒 1 字节载荷 `0F` → `DefaultBtMessageReceiver::receiveMessage` 把 `getMsgPayloadBuffer()`（指向 `0F`）交给工厂。
5. **工厂按 id 实例化**：switch 命中 `BtHaveNoneMessage::ID`（[src/DefaultBtMessageFactory.cc:L194-L196](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageFactory.cc#L194-L196)）→ `ZeroBtMessage::create<BtHaveNoneMessage>`（[src/BtHaveNoneMessage.cc:L46-L50](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtHaveNoneMessage.cc#L46-L50)）：断言载荷长 1、id 为 15，然后造一个无参对象 + `setCommonProperty`。
6. **校验与执行**：`validate()` 无 validator 直接通过 → `DefaultBtInteractive::receiveMessages` 打日志并调 `doReceivedAction` → [src/BtHaveNoneMessage.cc:L52-L58](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtHaveNoneMessage.cc#L52-L58)：若对方握手保留位没开 Fast 扩展则抛 `DL_ABORT_EX` 断连；开了则**什么都不做**——peer 的空位图已是默认状态，have none 只是显式确认。
7. **画出时序图**，把上面 6 步整理成两泳道（本方 / 对端）的序列，标出每一步经过的函数与文件。参考骨架：

```text
本方                                 对端(peer)
 │ addBitfieldMessageToQueue           │
 │  └ createHaveNoneMessage(factory)   │
 │  └ addMessageToQueue → onQueued()   │
 │  └ sendMessages → send()            │
 │     └ createMessage: 00 00 00 01 0F │
 │ ──────────── 5 bytes ─────────────► │ receiveMessage 状态机(4+1)
 │                                     │  └ factory: switch id=15 → create
 │                                     │  └ validate()（无校验器，通过）
 │                                     │  └ receiveMessages → doReceivedAction()
 │                                     │     └ Fast 未开 → DL_ABORT_EX 断连
 │                                     │     └ Fast 已开 → no-op（默认空位图）
```

8. **可选验证**（需可运行环境）：在步骤 5 的 case 处加一行 `A2_LOG_INFO`，连一个支持 Fast 扩展的客户端（多数现代客户端都支持），从零开始下载，确认握手后立即看到 have none 的收发；再对照日志里的 5 字节与你手算的序列是否一致。**待本地验证**。

**验收标准**：不看讲义能复述时序图；能回答「如果对端没开 Fast 扩展却发来 have none 会怎样」（答：`doReceivedAction` 抛异常，该 peer 连接被断开）。

## 6. 本讲小结

- BT 消息以「4 字节大端载荷长度 + 载荷」自定界；长度 0 是 keepalive 特例，在 `PeerConnection::receiveMessage` 的三态状态机里于读完长度字段时就地拦截，不进入载荷阶段。消息缓冲上限 64KiB+128，超限即断连。
- `BtMessage` 接口定义五个行为钩子（`onQueued`/`validate`/`doReceivedAction`/`send`/三个事件回调）；`AbstractBtMessage` 用七个依赖指针（peer、pieceStorage、dispatcher、两个 factory、peerConnection、cuid）把消息武装成「能自我行动」的对象；大纲中的 `onSendReply` 在源码中不存在，真实回调族即上述五个。
- `DefaultBtMessageReceiver` 是薄搬运工：零拷贝拿载荷 → 交工厂 → `validate` → piece 消息补挂数据指针。`DefaultBtMessageFactory` 是唯一的「id → 类」映射点，解析入口（switch id）与构造入口（createXxx）共用 `setCommonProperty` 依赖注入，未知 id 在此即被拒。
- `DefaultBtMessageDispatcher` 持两条队列：`messageQueue_`（发送队列，冲刷时对 uploading 消息做上传限速、扣下者插回队首）与 `requestSlots_`（在途请求账本，request 入队即记账，piece 到达按三元组对账核销，每秒巡检处理超时与 end-game 补发 cancel）。
- request/piece 是数据面主角：request 收到后按「有货且未 choke」决定回 piece 或 reject；piece 收到后对账→写盘（WrDiskCache 或直写）→核销→piece 集齐即验哈希，坏块清零重来并记坏 peer。
- 「事件广播」是 dispatcher 的反向通道：本方 choke（`doChokingAction`）、对方 cancel（`doCancelSendingPieceAction`）、坏块重来（`doAbortOutstandingRequestAction`）都会通知队列中的消息自我作废或改发 reject，广播前先做裸指针快照防迭代器失效。

## 7. 下一步学习建议

本讲搞定了「单条消息」的编解码与收发执行，下一讲 **u6-l8（扩展协议）** 将研究 id=20 的 `BtExtendedMessage` 如何在这个框架上再叠一层「扩展消息注册表」——`HandshakeExtensionMessage` 协商各扩展的 id、ut_pex 交换 peer 列表，工厂里的 `BtExtendedMessage::create` 分支（本讲 4.3.3 已路过）就是入口。如果想先补齐数据面的「生产节奏」，建议先读 **u6-l6（请求流水线）**：`DefaultBtRequestFactory` 决定「向谁要哪块」，本讲的 `requestSlots_` 账本则是它的运行时反馈（4.4.3 末尾的流水线自适应就是两者的接缝）。源码方面，推荐顺手对照 `src/BtBitfieldMessage.cc`（载荷可变长的消息）与 `src/BtPortMessage.h`（id=9，DHT 端口宣告），它们分别通向 u6-l1 的位图机制和 u7 单元的 DHT 子系统。
