# u6-l3 Peer 连接与握手协议

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 BT 握手消息的 68 字节结构，以及保留位（reserved bytes）中三个比特各自代表的扩展能力（DHT / Fast Extension / Extension Protocol）。
2. 跟踪一条入站连接的完整旅程：`PeerListenCommand` accept → `ReceiverMSEHandshakeCommand` 识别明文/加密 → `PeerReceiveHandshakeCommand` 校验 info_hash 并准入 → `PeerInteractionCommand` 接管会话。
3. 对照出站方向理解「先发握手再等回礼」与「先看对方 info_hash 再回礼」两种时序的差异。
4. 解释 `PeerConnection` 如何以定长缓冲 + 状态机接收握手、以「4 字节长度前缀 + 载荷」状态机接收后续消息。
5. 描述 `DefaultPeerStorage` 中 peer 的三张表（候选池 / 在用集 / 去重集）与坏 peer 惩罚机制，理解一个 peer 从「被 tracker 报告」到「被淘汰」的完整生命周期。

## 2. 前置知识

- **TCP 握手 vs BT 握手**：TCP 的三次握手由内核完成，`accept()` 返回时连接已通。但 BitTorrent 是应用层协议，双方还必须交换一条 68 字节的**协议握手消息**，互相亮明「我要下哪个种子（info_hash）」和「我是谁（peer_id）」。本讲说的「两次握手」就是指这两个层次。
- **info_hash 与 peer_id**：info_hash 是种子文件中 info 字典的 SHA-1 摘要（20 字节，见 u6-l1），是种子的全局唯一 ID；peer_id 是每个下载客户端的 20 字节自报身份，aria2 的静态 peer id 在启动时由 `bittorrent_helper` 生成（见 u6-l2）。
- **cuid**：Command UID，贯穿一条命令链的日志追踪钥匙（见 u2-l3）。一条 peer 连接从 accept 到断开共用一个 cuid，所以你能用 `grep CUID#xxx` 在日志里还原这条连接的一生。
- **Command 接力**：aria2 的每个 Command 只推进一小步就退出并安排下一棒（见 u2-l3、u6-l2）。本讲的握手流程正是「一棒接一棒」的典型：监听命令、识别命令、接收握手命令都是一次性命令，会话建立后交给常驻的 `PeerInteractionCommand`。
- **保留位（reserved bytes）**：握手消息中 8 个保留字节。协议规范把这 64 个比特编号为 bit 0（字节 0 的最高位）到 bit 63（字节 7 的最低位），每个比特可以独立声明一项扩展能力，双方都置位才代表「可以聊这个扩展」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/Peer.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Peer.cc) | 单个 peer 的身份信息（IP、端口、peer_id）与会话资源的懒加载 |
| [src/DefaultPeerStorage.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPeerStorage.cc) | peer 的候选池、在用集、去重集与坏 peer 名单 |
| [src/PeerConnection.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerConnection.cc) | 单条 peer 连接的字节流收发器（定长握手 + 变长消息） |
| [src/PeerAbstractCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerAbstractCommand.cc) | 所有 peer 命令的公共骨架：事件检查、超时、异常出口 |
| [src/PeerReceiveHandshakeCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerReceiveHandshakeCommand.cc) | 入站方向的 BT 协议握手接收与准入判定 |
| [src/PeerListenCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerListenCommand.cc) | BT 监听端口，accept 入站连接 |
| [src/ReceiverMSEHandshakeCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ReceiverMSEHandshakeCommand.cc) | 入站第一棒：嗅探明文握手还是 MSE 加密握手 |
| [src/BtHandshakeMessage.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtHandshakeMessage.cc) | 68 字节握手消息的构造、解析与保留位判断 |
| [src/BtHandshakeMessageValidator.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtHandshakeMessageValidator.cc) | 收到的握手消息三重校验（pstrlen / pstr / info_hash） |
| [src/PeerInteractionCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc) | 握手完成后的常驻会话命令，出站侧握手也在这里发起 |
| [src/DefaultBtMessageReceiver.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageReceiver.cc) | 接收握手并回礼的细粒度控制（含 NAT-checking 应答） |
| [src/DefaultBtInteractive.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc) | 会话大脑：握手保留位落地为 peer 能力，握手后首条消息队列 |
| [src/DefaultBtMessageFactory.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageFactory.cc) | 消息工厂：创建握手消息并挂校验器 |

## 4. 核心概念与源码讲解

### 4.1 Peer 对象与 DefaultPeerStorage：peer 的接纳与淘汰

#### 4.1.1 概念说明

`Peer` 是 aria2 对「一个远端下载者」的抽象。注意它的两个端口字段：`port_` 是当前通信对端的端口，`origPort_` 是构造时传入的端口。对**入站**连接来说，`port_` 是对方的临时源端口（不是对方的监听端口，我们无法主动连它），而 `origPort_` 用来做 peer 身份去重。`Peer` 还刻意把「会话资源」（bitfield、choke 状态、速度统计等 `PeerSessionResource`）设计成**懒加载**——握手成功前只有一个轻量壳子，`isActive()` 为真才代表真正建立过会话。

`DefaultPeerStorage` 则是**单个种子**的 peer 仓库（挂在 BtObject 六件套的 `peerStorage` 槽上，见 u6-l2），用三张表管理 peer：

- `unusedPeers_`：候选池（deque），tracker/DHT/PEX 报告的 peer 先进这里排队等连接；
- `usedPeers_`：在用集（PeerSet），已被某个 cuid 领走的 peer；
- `uniqPeers_`：`(IP, origPort)` 去重集，保证同一 peer 不重复入库。

另有两张辅助表：`droppedPeers_`（近期断开且行为良好的 peer，可优先复连）与 `badPeers_`（行为恶劣的 IP 黑名单，带过期时间）。

#### 4.1.2 核心流程

一个 peer 的生命周期：

```
tracker/DHT/PEX 报告          入站连接（本讲 4.3）
        │                            │
        ▼                            ▼
  addPeer() ──拒绝──► 丢弃    addAndCheckoutPeer()
        │                     （跳过候选池直接领用）
        ▼                            │
  unusedPeers_（候选池，上限 512）    │
        │ checkoutPeer(cuid)         │
        ▼                            ▼
  usedPeers_（在用集）◄──────────────┘
        │ 会话结束 returnPeer()
        ├─ 曾活跃 + 优雅断开 + 非入站 → droppedPeers_（上限 50）
        └─ 从 uniqPeers_ 移除，peer 销毁
  行为恶劣（超时/发坏数据）→ addBadPeer(ip) → 120~720 秒内拒收
```

`addPeer` 的三道拒绝闸门依次是：候选池已满（512）、重复入库、坏 peer 名单命中。`addBadPeer` 的封禁时长是 `随机 120~720 秒`，故意加随机量是为了避免大量坏 peer 在同一时刻解禁、同时发起连接形成风暴。

#### 4.1.3 源码精读

Peer 的构造与身份字段——注意 `incoming=true` 时 `port_` 并非对方的监听端口：

- [src/Peer.cc:48-61](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Peer.cc#L48-L61)：构造函数保存 IP 与端口，`origPort_` 固定为构造值用于身份去重，20 字节 `peerId_` 先清零，握手成功后才由 `setPeerId` 填入。
- [src/Peer.h:59-65](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Peer.h#L59-L65)：头文件注释明确说明 `port_`（通信端口）与 `origPort_`（身份端口）的区别。

会话资源懒加载——`isActive()` 就是「`res_` 是否存在」：

- [src/Peer.cc:67-72](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Peer.cc#L67-L72)：`allocateSessionResource` 在握手完成后创建 bitfield 等会话资源；[src/Peer.h:112-113](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Peer.h#L112-L113) 定义 `isActive()`。

DefaultPeerStorage 的三张表与候选池上限：

- [src/DefaultPeerStorage.h:58-65](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPeerStorage.h#L58-L65)：`uniqPeers_` / `unusedPeers_` / `usedPeers_` 三张表的声明及注释；[src/DefaultPeerStorage.cc:56](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPeerStorage.cc#L56)：`MAX_PEER_LIST_SIZE = 512`。

`addPeer` 的三道闸门：

- [src/DefaultPeerStorage.cc:89-119](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPeerStorage.cc#L89-L119)：候选池满 → 拒绝；`isPeerAlreadyAdded`（[L78-L82](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPeerStorage.cc#L78-L82)，按 `(IP, origPort)` 查重）→ 拒绝；`isBadPeer` → 拒绝；全部通过才 `push_back` 进候选池并登记去重集。

坏 peer 黑名单的写入与随机封禁时长：

- [src/DefaultPeerStorage.cc:236-258](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPeerStorage.cc#L236-L258)：`addBadPeer` 先做每小时一次的过期清扫，再用 `max(随机数(0~600), 120)` 秒作为封禁期限写入 `badPeers_`；[src/DefaultPeerStorage.cc:221-234](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPeerStorage.cc#L221-L234)：`isBadPeer` 查表，过期即视为不坏并移除。

领用与归还：

- [src/DefaultPeerStorage.cc:271-289](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPeerStorage.cc#L271-L289)：`checkoutPeer` 从候选池头取出 peer，设置 `usedBy(cuid)` 并移入在用集——注意取的是「最早加入的」，体现 FIFO。
- [src/DefaultPeerStorage.cc:312-325](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPeerStorage.cc#L312-L325)：`returnPeer` 归还时经 `onReturningPeer`（[L296-L310](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPeerStorage.cc#L296-L310)）做善后：曾活跃、优雅断开且非入站的 peer 进 `droppedPeers_`（[L189-L205](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPeerStorage.cc#L189-L205)，上限 50）；若断开的是一个「被我们 unchoke 且对我们感兴趣」的 peer，还要立刻跑一次 choke 算法补位。

入站专用的「插队」入口：

- [src/DefaultPeerStorage.cc:163-187](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPeerStorage.cc#L163-L187)：`addAndCheckoutPeer` 供入站握手成功后调用——若该 `(IP, origPort)` 已在候选池，先删除旧条目；若在在用集（说明已有同身份连接），返回空指针让调用方丢弃；否则直接 `push_front` 后立即 `checkoutPeer`。它**不受**候选池 512 上限约束，因为入站连接意味着对方已经付出了一条 TCP 连接的代价，值得优先接待。

#### 4.1.4 代码实践

1. **实践目标**：用单元测试验证 `addPeer` 的三道拒绝闸门与去重行为。
2. **操作步骤**：
   - 打开 [test/DefaultPeerStorageTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DefaultPeerStorageTest.cc)，找到 `testAddPeer` 与 `testAddBadPeer` 两个用例（注册见 [test/DefaultPeerStorageTest.cc:19-27](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DefaultPeerStorageTest.cc#L19-L27)）。
   - 阅读断言：同一个 `(IP, port)` 第二次 `addPeer` 应返回 false；`addBadPeer` 之后再 `addPeer` 同 IP 应失败。
   - 若已按 u1-l2 编译过项目，可运行 `make check` 并观察该套件是否通过。
3. **需要观察的现象**：测试断言与 4.1.3 中三道闸门的源码一一对应。
4. **预期结果**：能指出「重复检测用的是 `origPort_` 而非 `port_`」这一细节——入站 peer 的 `port_` 是临时源端口，用它去重会误伤。
5. 运行结果**待本地验证**（取决于是否完成本地编译）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `droppedPeers_` 只收「优雅断开且非入站」的 peer？

**答案**：非优雅断开往往意味着网络异常或对端崩溃，立刻重连大概率再失败；入站 peer 的 `port_` 不是监听端口，主动重连根本连不上（见 [src/Peer.h:59-62](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Peer.h#L59-L62) 的注释）。`onReturningPeer` 中的判定 `peer->isDisconnectedGracefully() && !peer->isIncomingPeer()`（[src/DefaultPeerStorage.cc:298-302](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPeerStorage.cc#L298-L302)）正是把这两类排除在外。

**练习 2**：`addBadPeer` 的封禁时长为什么写成 `max(随机(0~600), 120)` 秒而不是固定值？

**答案**：见 [src/DefaultPeerStorage.cc:252-255](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPeerStorage.cc#L252-L255) 的注释「We use variable timeout to avoid many bad peers wake up at once」：固定时长会让同时被封的一批 peer 在同一秒集体解禁、集体重连，形成周期性风暴；随机化把唤醒时间摊开，下限 120 秒保证惩罚有意义。

**练习 3**：析构函数中的断言 `uniqPeers_.size() == unusedPeers_.size() + usedPeers_.size()`（[src/DefaultPeerStorage.cc:68-71](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPeerStorage.cc#L68-L71)）在防什么 bug？

**答案**：防止三张表失去同步——例如 peer 从 `unusedPeers_` 删除却忘了从 `uniqPeers_` 抹掉（或反之）。一旦失衡，后续去重判断就会出错，出现重复连接或永远拒绝某个 peer。断言在调试构建下能在仓库销毁时立即暴露这类不一致。

### 4.2 PeerConnection：定长字节流的收发器

#### 4.2.1 概念说明

`PeerConnection` 是一条 peer TCP 连接上的**字节流管家**。它要解决两个问题：

1. **TCP 是字节流不是消息流**：一次 `read` 可能读到半条消息，也可能读到一条半消息（粘包/半包）。握手是固定 68 字节，后续 BT 消息则是「4 字节大端长度 + 载荷」的变长格式，两种格式需要不同的接收策略。
2. **资源接力**：握手阶段和会话阶段由不同的 Command 负责，但缓冲区里可能还留着没收完的数据。所以 `PeerConnection` 被设计成可移动对象，作为「接力棒」在命令间传递（你在 4.3 会看到 `presetBuffer` 和 `std::move(peerConnection_)`）。

它同时管发送：数据先进 `SocketBuffer`（部分写缓冲），由事件循环在 socket 可写时推进；若 MSE 加密协商成功，收发都会先过一层 RC4（`enableEncryption`）。

#### 4.2.2 核心流程

接收侧的两条路径：

```
receiveHandshake(out, len, peek)        receiveMessage(out, len)
─────────────────────────────           ──────────────────────────
缓冲不足 68 字节?                        状态机（逐字节）:
  是 → readData 补读                    PREV_READ_LENGTH
       仍不足 → return false              └─ 记下 msgOffset，进入 ↓
  EOF → setDisconnectedGracefully       READ_LENGTH（攒 4 字节大端长度）
        抛 EX_EOF_FROM_PEER               ├─ 凑满且值=0 → keepalive，完成
缓冲满 68 → 拷给调用方                    └─ 凑满 → 进入 ↓
  peek=true → 数据留在缓冲区             READ_PAYLOAD（等 4+len 字节齐）
  peek=false → 清空缓冲区                  └─ 齐了 → 完成一条消息
```

发送侧：`pushBytes`（可选加密）→ `SocketBuffer::pushBytes` → 事件循环里 `sendPendingData` 推进。

#### 4.2.3 源码精读

缓冲容量上限（64KiB 块长 + 128 余量，超出即拒绝，防止恶意超长消息撑爆内存）：

- [src/PeerConnection.h:54-57](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerConnection.h#L54-L57)：`MAX_BUFFER_CAPACITY = MAX_BLOCK_LENGTH + 128`；`MAX_BLOCK_LENGTH = 64_k` 定义在 [src/BtConstants.h:49](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtConstants.h#L49)。

构造与成员布局（接收缓冲 `resbuf_`、游标 `resbufOffset_`/`msgOffset_`、发送缓冲 `socketBuffer_`）：

- [src/PeerConnection.cc:66-82](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerConnection.cc#L66-L82)：构造函数初始化状态机为 `BT_MSG_PREV_READ_LENGTH`、分配 `MAX_BUFFER_CAPACITY` 容量。

定长接收握手（补读 → EOF 判定 → peek 语义）：

- [src/PeerConnection.cc:198-230](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerConnection.cc#L198-L230)：`receiveHandshake` 先算 `remaining = 68 - 已缓冲`，`readData` 补读；读后仍不足则返回 false 让命令下次再来；`peek=true` 时（[L226-L228](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerConnection.cc#L226-L228)）数据保留在缓冲区不消费——这是「先偷看 info_hash 再决定是否回礼」的基础。
- 注意 [L210-L217](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerConnection.cc#L210-L217)：读到 EOF（既不想读也不想写）时标记 `setDisconnectedGracefully(true)` 再抛 `EX_EOF_FROM_PEER`，让上层把该 peer 归入「优雅断开」类别。

变长消息接收状态机（keepalive 特例、粘包免疫、缓冲整理）：

- [src/PeerConnection.cc:56-64](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerConnection.cc#L56-L64)：三态枚举定义。
- [src/PeerConnection.cc:108-124](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerConnection.cc#L108-L124)：`READ_LENGTH` 态逐字节左移拼出 32 位大端长度；攒满 4 字节时若长度超容量抛异常，若为 0 直接判定 keepalive 完成。
- [src/PeerConnection.cc:126-139](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerConnection.cc#L126-L139)：`READ_PAYLOAD` 态按 `msgOffset_ + 4 + payloadLength` 判断一条消息是否完整；不完整就消费掉缓冲区全部剩余字节等下次。
- [src/PeerConnection.cc:153-169](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerConnection.cc#L153-L169)：一轮读完仍未成帧时的缓冲整理：若缓冲恰好被完整消费则三游标清零；否则 `memmove` 把未完成消息挪到缓冲区头部。
- [src/PeerConnection.cc:171-179](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerConnection.cc#L171-L179)：大载荷（>4KiB）时只精确读 `所需 - 已有` 字节，减少后续 `memmove` 的搬运量——这是为 piece 消息（16KiB 块）做的优化。

发送与接力辅助：

- [src/PeerConnection.cc:86-93](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerConnection.cc#L86-L93)：`pushBytes` 发送前可选加密，再交给 `SocketBuffer`；[src/PeerConnection.cc:267-272](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerConnection.cc#L267-L272)：`sendPendingData` 在 socket 可写时推进部分写。
- [src/PeerConnection.cc:250-255](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerConnection.cc#L250-L255)：`presetBuffer` 把前一棒命令（MSE 嗅探）已经读进来的字节转交给自己——避免同一条连接出现两个读取者。
- [src/PeerConnection.cc:241-248](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerConnection.cc#L241-L248)：`enableEncryption` 挂上 RC4 加解密器，此后 `readData`（[L232-L239](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerConnection.cc#L232-L239)）与 `pushBytes` 都透明加解密。

#### 4.2.4 代码实践

1. **实践目标**：不用跑网络，手工推演 `receiveMessage` 状态机对「粘包 + 半包」输入的处理。
2. **操作步骤**：
   - 假设对端连发两条消息：`keepalive`（4 字节 `00 00 00 00`）和一条 `len=5` 的消息（`00 00 00 05 aa bb cc dd ee`），共 13 字节；再假设 TCP 第一次只送达前 7 字节 `00 00 00 00 00 00 00`。
   - 对照 [src/PeerConnection.cc:95-196](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerConnection.cc#L95-L196) 逐字节走一遍状态机：第 4 字节后 keepalive 成帧（`done=true`，`dataLength=0`）；第 7 字节时第二条消息处于 `READ_LENGTH` 态只攒到 3 字节长度。
   - 继续推演：此时函数如何走到 [L153-L169](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerConnection.cc#L153-L169) 的缓冲整理？`resbufOffset_`、`resbufLength_`、`msgOffset_` 各是多少？
3. **需要观察的现象**：keepalive 成帧后 `resbufOffset_` 停在第 4 字节，下一条消息的 `msgOffset_` 被记为 4；7 字节读完后 `memmove` 把字节 4~6（3 字节长度前缀）挪到缓冲头部，游标归位为 `msgOffset_=0, resbufLength_=3`。
4. **预期结果**：你能向别人解释「为什么这个状态机天然免疫粘包与半包」——因为所有进度（`msgState_`、`currentPayloadLength_`、`msgOffset_`）都外置于成员变量，函数可以在任意字节边界被打断再续跑。
5. 推演可完全在纸面完成，无需本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `receiveHandshake` 不像 `receiveMessage` 那样需要状态机？

**答案**：握手是**固定 68 字节**的定长消息，不存在「读到哪一段」的二义性——只需求「已缓冲字节数 < 68 则补读」这一个条件（[src/PeerConnection.cc:206-209](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerConnection.cc#L206-L209)）。变长消息必须先知道长度才能知道边界，才需要三态状态机。

**练习 2**：`receiveHandshake(peek=true)` 的「偷看」语义在什么场景下被使用？

**答案**：两处。一是 4.3 将讲的 tracker NAT-checking：tracker 代理只发握手的前 48 字节探测，接收方必须先偷看 info_hash 决定是否回礼、而不是把 68 字节等齐（[src/PeerReceiveHandshakeCommand.cc:93-98](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerReceiveHandshakeCommand.cc#L93-L98)）。二是出站方向的 quickReply 路径：对方握手可能先到、我们自己的握手还没发，需要先偷看校验 info_hash 再回礼（[src/DefaultBtMessageReceiver.cc:81-102](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageReceiver.cc#L81-L102)）。

**练习 3**：为什么 `receiveMessage` 对超过 `MAX_BUFFER_CAPACITY` 的消息直接抛异常而不是尽力接收？

**答案**：BT 协议单条消息理论上限是 piece 消息 ≈ 16KiB 块 + 头部，64KiB+128 的容量上限已远超合理范围；声称超长长度通常是对端 bug 或恶意行为，照单全收会让攻击者用一个 4 字节长度声明就迫使 aria2 分配 4GiB 内存。立即抛 `EX_TOO_LONG_PAYLOAD`（[src/PeerConnection.cc:113-115](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerConnection.cc#L113-L115)）并断开是最安全的回应。

### 4.3 PeerReceiveHandshakeCommand：入站连接的协议层握手

#### 4.3.1 概念说明

TCP 连接被 accept 之后、正式会话开始之前，一条入站连接要经过三棒命令：

1. **`PeerListenCommand`**（常驻）：在 `--listen-port` 上 accept 新连接，创建 `Peer(incoming=true)` 并交给下一棒。
2. **`ReceiverMSEHandshakeCommand`**（一次性）：嗅探前 20 字节，判断对端发来的是**明文 BT 握手**（首字节 19 + `"BitTorrent protocol"`）还是 **MSE 加密握手**（BEP 8 流加密）。明文则转交下一棒；加密则先完成整套密钥协商再转交。
3. **`PeerReceiveHandshakeCommand`**（一次性）：收满协议握手、校验 info_hash、按准入条件决定接收或丢弃；接收则把 peer 登记进 `PeerStorage` 并创建常驻的 `PeerInteractionCommand`（`RECEIVER_WAIT_HANDSHAKE` 状态）。

与之对照的**出站**方向只有两棒：`PeerInitiateConnectionCommand` 建连后直接创建 `PeerInteractionCommand`（`INITIATOR_SEND_HANDSHAKE` 状态）——因为我们主动连接时已知道要下哪个种子，不需要嗅探与反查。

#### 4.3.2 核心流程

入站完整时序：

```
PeerListenCommand::execute
  └─ accept → Peer(ip, port, incoming=true) → ReceiverMSEHandshakeCommand
       └─ executeInternal: identifyHandshakeType()
            ├─ 前 20 字节 = 19 | "BitTorrent protocol" → LEGACY
            │    ├─ --bt-require-crypto 开启 → 拒绝
            │    └─ new PeerConnection + presetBuffer(已读字节)
            │         → PeerReceiveHandshakeCommand ──────────────┐
            └─ 否则 → MSE 密钥协商(略) → enableEncryption        │
                 → PeerReceiveHandshakeCommand ───────────────────┤
                                                                  ▼
PeerReceiveHandshakeCommand::executeInternal
  ├─ 缓冲 < 48 字节: receiveHandshake(peek=true)   ← 兼容 NAT-checking
  ├─ 仍 < 48: addCommandSelf, return false          ← 下次事件继续等
  ├─ 取 data[28..47] 为 info_hash → BtRegistry::getDownloadContext(infoHash)
  │    └─ 找不到或 !btRuntime->ready() → "Unknown info hash" 抛弃
  ├─ 准入判定: (!downloadFinished && speed < 阈值) || lessThanMaxPeers()
  │    阈值 = min(--bt-request-peer-speed-limit, --max-download-limit)
  ├─ peerStorage->addAndCheckoutPeer(peer, cuid)   ← 重复则返回空,丢弃
  └─ new PeerInteractionCommand(RECEIVER_WAIT_HANDSHAKE, 连同 PeerConnection)
       └─ receiveAndSendHandshake(): 校验对方握手 → 回发自己的握手
            → doPostHandshakeProcessing() → 进入 WIRED 常驻循环(下一讲)
```

出站时序（对照）：

```
PeerInitiateConnectionCommand::executeInternal
  └─ establishConnection → PeerInteractionCommand(INITIATOR_SEND_HANDSHAKE)
       ├─ INITIATOR_SEND_HANDSHAKE: socket 可写 → initiateHandshake() 主动发出
       ├─ INITIATOR_WAIT_HANDSHAKE: receiveHandshake() 等对方回礼
       └─ WIRED
```

可见两条方向的差异本质是**谁先说话**：出站方先发握手再等回礼；入站方等对方先发、校验 info_hash 后才回礼。

#### 4.3.3 源码精读

监听命令 accept 入站连接（注意 `incoming=true`）：

- [src/PeerListenCommand.cc:100-127](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerListenCommand.cc#L100-L127)：`execute` 每轮最多处理 3 个就绪连接（[L105](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerListenCommand.cc#L105)），accept 后构造 `Peer(endpoint.addr, endpoint.port, true)` 并交给 `ReceiverMSEHandshakeCommand`。端口绑定与随机化在 [L62-L89](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerListenCommand.cc#L62-L89) 的 `bindPort`。

第一棒嗅探：明文还是加密？

- [src/MSEHandshake.cc:97-107](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MSEHandshake.cc#L97-L107)：`identifyHandshakeType` 读满 20 字节后判断：首字节等于 19 且后续 19 字节是 `BT_PSTR`（`"BitTorrent protocol"`）→ `HANDSHAKE_LEGACY`。
- [src/ReceiverMSEHandshakeCommand.cc:97-114](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ReceiverMSEHandshakeCommand.cc#L97-L114)：LEGACY 分支——若 `--bt-require-crypto`（旧名 `--bt-force-encryption`）开启则拒绝明文；否则 `new PeerConnection` 并用 `presetBuffer` 把嗅探阶段已读的字节无损转交，再创建 `PeerReceiveHandshakeCommand`。
- [src/ReceiverMSEHandshakeCommand.cc:214-232](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ReceiverMSEHandshakeCommand.cc#L214-L232)：MSE 分支协商完成后同样创建 `PeerReceiveHandshakeCommand`，若协商出 ARC4 则给 `PeerConnection` 挂上加密器，并把 MSE 的 IA（初始载荷，通常就是对方的 BT 握手）`presetBuffer` 进去。

第二棒主体：`PeerReceiveHandshakeCommand`。

- [src/PeerReceiveHandshakeCommand.cc:64-80](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerReceiveHandshakeCommand.cc#L64-L80)：构造函数若发现转交来的 `PeerConnection` 缓冲区已有数据，立即把自己升级为 `STATUS_ONESHOT_REALTIME` 并 `setNoWait(true)`——让本轮事件循环不等待 I/O 直接执行，减少一次空转。
- [src/PeerReceiveHandshakeCommand.cc:93-98](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerReceiveHandshakeCommand.cc#L93-L98)：缓冲不足 48 字节时用 `peek=true` 偷读——注释点明这是处理 **tracker NAT-checking**：tracker 会伪装成 peer 发来部分握手探测你是否可达/是否在下载该种子，等齐 68 字节反而会挂死。
- [src/PeerReceiveHandshakeCommand.cc:99-118](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerReceiveHandshakeCommand.cc#L99-L118)：凑满 48 字节后从 `data[28..47]` 取 info_hash，经 `BtRegistry::getDownloadContext(infoHash)` **按内容反查任务**（u6-l2 讲过这是注册表的线性扫描）；查不到或 `btRuntime` 未就绪即抛弃——入站连接不携带「想下哪个种子」之外的任何信息，只能靠 info_hash 匹配。
- [src/PeerReceiveHandshakeCommand.cc:119-123](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerReceiveHandshakeCommand.cc#L119-L123)：种子已结束（halt）则静默丢连接。
- [src/PeerReceiveHandshakeCommand.cc:124-136](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerReceiveHandshakeCommand.cc#L124-L136)：**准入闸门**——`(!downloadFinished && 当前下载速度 < 阈值) || btRuntime->lessThanMaxPeers()`。阈值取 `--bt-request-peer-speed-limit` 与 `--max-download-limit` 的较小值：下载未完且速度不达标时欢迎新 peer（它能带来带宽）；速度已达标时只有连接数低于 `--bt-max-peers`（默认 55，见 [src/OptionHandlerFactory.cc:1563-1564](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L1563-L1564)）才收。
- [src/PeerReceiveHandshakeCommand.cc:139-147](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerReceiveHandshakeCommand.cc#L139-L147)：`addAndCheckoutPeer` 成功后创建 `PeerInteractionCommand(RECEIVER_WAIT_HANDSHAKE)`，**连同 `std::move(peerConnection_)` 一起移交**（缓冲区接力）；随后 `return true` 自我销毁——它是纯一次性命令。
- [src/PeerReceiveHandshakeCommand.cc:151-154](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerReceiveHandshakeCommand.cc#L151-L154)：字节不够时 `addCommandSelf()` 重排自己，`return false` 等待下一个读事件。

公共骨架 `PeerAbstractCommand`：超时与异常出口。

- [src/PeerAbstractCommand.cc:52-70](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerAbstractCommand.cc#L52-L70)：构造时从 `--bt-timeout`（默认 180 秒，[src/OptionHandlerFactory.cc:1669-1670](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L1669-L1670)）取超时，并对已打开的 socket 注册读事件检查。
- [src/PeerAbstractCommand.cc:78-115](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerAbstractCommand.cc#L78-L115)：模板方法 `execute`——读/写/hup 事件刷新检查点（[L89-L92](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerAbstractCommand.cc#L89-L92)）；error 事件抛网络异常；超时抛 `EX_TIME_OUT`（[L97-L99](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerAbstractCommand.cc#L97-L99)）；`RecoverableException` 出口打日志 `MSG_PEER_BANNED`（对端进坏名单）后 `prepareForNextPeer` 换下一个 peer（[L108-L114](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerAbstractCommand.cc#L108-L114)）。
- 值得对比：嗅探命令 `ReceiverMSEHandshakeCommand` 用的是更短的 `--peer-connect-connection-timeout` 前身 `PREF_PEER_CONNECTION_TIMEOUT`（默认 20 秒，[src/ReceiverMSEHandshakeCommand.cc:65-66](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ReceiverMSEHandshakeCommand.cc#L65-L66)、[src/OptionHandlerFactory.cc:1849-1850](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L1849-L1850)）——嗅探阶段值得更快放弃。

第三棒入口与出站对照：

- [src/PeerInitiateConnectionCommand.cc:75-98](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInitiateConnectionCommand.cc#L75-L98)：出站第一棒 `establishConnection` 后按 `--bt-min-crypto-level` 等决定走 MSE 还是直接 `PeerInteractionCommand(INITIATOR_SEND_HANDSHAKE)`。
- [src/PeerInteractionCommand.cc:297-361](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L297-L361)：`executeInternal` 的四态状态机（枚举定义见 [src/PeerInteractionCommand.h:52-56](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.h#L52-L56)）：`INITIATOR_SEND_HANDSHAKE`（[L303-L314](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L303-L314)，socket 可写后 `initiateHandshake` 主动发握手）→ `INITIATOR_WAIT_HANDSHAKE`（[L315-L331](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L315-L331)，等回礼）→ `WIRED`；入站则从 `RECEIVER_WAIT_HANDSHAKE`（[L332-L341](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L332-L341)）起步，用 `receiveAndSendHandshake`（quickReply）校验后回礼。
- [src/DefaultBtInteractive.cc:114-120](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L114-L120)：`initiateHandshake` 用本地 info_hash 与静态 peer id 造握手消息入发送队列。
- [src/DefaultBtMessageReceiver.cc:65-104](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageReceiver.cc#L65-L104)：`receiveHandshake(quickReply)`——普通路径（[L73-L79](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageReceiver.cc#L73-L79)）等齐 68 字节、经工厂造消息并 `validate()`；quickReply 路径（[L81-L102](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageReceiver.cc#L81-L102)）在缓冲 ≥48 且尚未发过握手时先比对 `data[28..47]` 与本地 info_hash（[L85-L86](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageReceiver.cc#L85-L86)），匹配则立即 `sendHandshake` 回礼，再等剩余 20 字节 peer_id。

#### 4.3.4 代码实践

1. **实践目标**：在日志中区分入站与出站连接，并验证「同一 info_hash 的任务存在」是入站准入的前提。
2. **操作步骤**：
   - 准备一个合法的测试种子（建议用发行版官方 ISO 的 .torrent，如 Ubuntu/Debian），启动：
     ```bash
     ./src/aria2c --seed-time=0 -d /tmp/bt-test \
       --listen-port=6881-6889 --enable-dht=false \
       --log=/tmp/bt.log --log-level=debug <种子文件>
     ```
   - 在 `/tmp/bt.log` 中检索这些日志行（全部来自本讲源码）：
     - `Accepted the connection from`（[src/PeerListenCommand.cc:116-117](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerListenCommand.cc#L116-L117)）——入站起点；
     - `This is legacy BitTorrent handshake.`（[src/MSEHandshake.cc:105-106](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MSEHandshake.cc#L105-L106)）——明文嗅探命中；
     - `Unknown info hash`（[src/PeerReceiveHandshakeCommand.cc:106-109](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerReceiveHandshakeCommand.cc#L106-L109)）——不认识的对端被拒；
     - `Incoming connection, adding new command`（`MSG_INCOMING_PEER_CONNECTION`，[src/message.h:99-100](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h#L99-L100)，打印点在 [src/PeerReceiveHandshakeCommand.cc:145-146](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerReceiveHandshakeCommand.cc#L145-L146)）——入站握手成功交接。
   - 对照同一 `CUID#` 把一条入站连接的上述日志行按顺序串起来。
3. **需要观察的现象**：一条入站连接的 cuid 日志序列大致为 `Accepted → legacy handshake → Incoming connection`；若对端 info_hash 与本地任何任务都不匹配，序列终止于 `Unknown info hash` 且伴随 `banned` 字样的放弃日志。
4. **预期结果**：能画出「accept → 嗅探 → 反查注册表 → 准入 → 移交会话命令」五步时序，并指出每步对应的日志行。真实 peer 的到来取决于网络环境（做种人数），**待本地验证**。
5. 若短时间内没有入站连接，可用另一台机器/容器对同一种子运行 aria2 并指向本机 `--peer-id-prefix` 无关，只需保证两者端口可达。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `PeerReceiveHandshakeCommand` 凑满 48 字节就够反查 info_hash，而不用等 68 字节？

**答案**：info_hash 位于握手的偏移 28~47 字节，48 恰好是「pstrlen(1) + pstr(19) + reserved(8) + info_hash(20)」的总长。后面的 20 字节是 peer_id，反查任务用不到。提前判定还能兼容 NAT-checking 只发 48 字节的探测（[src/PeerReceiveHandshakeCommand.cc:93-102](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerReceiveHandshakeCommand.cc#L93-L102)）。

**练习 2**：出站方向的握手为什么不需要 `PeerReceiveHandshakeCommand` 这类「反查注册表」的命令？

**答案**：出站连接由 `ActivePeerConnectionCommand` 从某个种子的 `PeerStorage` 候选池发起（u6-l2 的 BtSetup 装配），命令创建时就携带了该种子的 `RequestGroup`、`pieceStorage` 等全部上下文，info_hash 天然已知。入站连接是「先有连接后有上下文」，只能靠读 info_hash 反查 `BtRegistry`（[src/PeerReceiveHandshakeCommand.cc:104-111](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerReceiveHandshakeCommand.cc#L104-L111)）。

**练习 3**：`PeerReceiveHandshakeCommand` 的准入闸门里，为什么「下载速度低于阈值」和「连接数未满」是**或**的关系？

**答案**：见 [src/PeerReceiveHandshakeCommand.cc:134-136](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerReceiveHandshakeCommand.cc#L134-L136)。下载未完成且速度不达标 → 现有 peer 供给不足，新 peer 很可能带来增量带宽，即使连接数已到 `--bt-max-peers` 也值得收（速度优先）；速度已达标 → 只在连接数低于上限时才收（上限优先）。两个条件覆盖「要带宽」与「要数量」两种诉求。

### 4.4 BtHandshakeMessage 与保留位：68 字节里的能力协商

#### 4.4.1 概念说明

`BtHandshakeMessage` 是 68 字节握手消息的对象表示。它的特殊之处在于：**它不走普通 BT 消息的「4 字节长度前缀」封帧**，而是裸发的定长报文——所以 `PeerConnection::receiveMessage` 的状态机对它不适用，才有 4.2 里独立的 `receiveHandshake`。

8 字节保留位是 BT 的**能力协商位图**。aria2 关心其中三个比特（按 BEP 惯例从字节 0 的最高位记为 bit 0）：

| 字节 | 掩码 | 比特号 | 含义 | BEP | aria2 判定函数 |
| --- | --- | --- | --- | --- | --- |
| `reserved[7]` | `0x01` | 63 | 对端支持 DHT（会应答 port 消息） | BEP 5 | `isDHTEnabled()` |
| `reserved[7]` | `0x04` | 61 | 对端支持 Fast Extension（HaveAll/SuggestPiece 等） | BEP 6 | `isFastExtensionSupported()` |
| `reserved[5]` | `0x10` | 43 | 对端支持扩展协议（extended 消息，ut_pex/ut_metadata 的载体） | BEP 10 | `isExtendedMessagingEnabled()` |

比特号的换算：字节 \(b\) 内掩码 \(m = 2^{k}\) 对应全局比特号

\[
\text{bit} = 8b + (7 - \log_2 m)
\]

例如 `reserved[7] & 0x04`：\(b=7, k=2\)，\(\text{bit} = 56 + (7-2) = 61\)。

一个提醒：这些掩码**没有**定义为 `BtConstants.h` 里的命名常量，而是直接写在 `BtHandshakeMessage` 的方法里（规划大纲中「对照 BtConstants/bittorrent_helper 的常量」的说法并不准确——`BtConstants.h` 提供的是 `INFO_HASH_LENGTH`/`PEER_ID_LENGTH`/`MAX_BLOCK_LENGTH` 这些长度常量；peer id 与 info_hash 的取值入口在 `bittorrent_helper` 的 `getStaticPeerId`/`getInfoHash`）。

#### 4.4.2 核心流程

**发送侧**（aria2 主动亮牌）：

```
DefaultBtMessageFactory::createHandshakeMessage(infoHash, peerId)
  └─ new BtHandshakeMessage(infoHash, peerId)
       └─ init(): reserved 清零
            reserved[7] |= 0x04   (Fast Extension, 恒置)
            reserved[5] |= 0x10   (Extension Protocol, 恒置)
       └─ factory: setDHTEnabled(dhtEnabled_)  (DHT 位, 视本机 DHT 是否已启动)
  → dispatcher 入队 → PeerConnection::pushBytes 发出 68 字节
```

**接收侧**（三重校验 + 能力落地）：

```
PeerConnection::receiveHandshake 收满 68 字节
  → DefaultBtMessageFactory::createHandshakeMessage(data, len)
       └─ BtHandshakeMessage::create() 按固定偏移切字段
       └─ 挂 BtHandshakeMessageValidator
  → msg->validate()
       ├─ pstrlen == 19 ?
       ├─ pstr  == "BitTorrent protocol" ?
       └─ info_hash == 本地任务的 info_hash ?
  → DefaultBtInteractive::receiveHandshake()
       ├─ peer_id == 自己的? → 自连接, 丢弃
       ├─ peer_id 已在其他在用 peer 上? → 重复, 丢弃
       ├─ setPeerId()
       ├─ 三个保留位检查 → peer->setFastExtensionEnabled / setExtendedMessagingEnabled / setDHTEnabled
       └─ 日志: "CUID#xx - From: ip:port handshake peerId=..., reserved=xxxx"
  → doPostHandshakeProcessing(): 按能力发首条消息
       (extended → 扩展握手; DHT 双开 → port; fast ext → HaveAll/HaveNone/AllowedFast)
```

#### 4.4.3 源码精读

68 字节结构与各字段偏移（常量 + 按偏移解析）：

- [src/BtHandshakeMessage.h:48-58](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtHandshakeMessage.h#L48-L58)：`PSTR_LENGTH=19`、`RESERVED_LENGTH=8`、`MESSAGE_LENGTH=68` 三个常量与四个字段的 `std::array` 布局。
- [src/BtHandshakeMessage.cc:71-82](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtHandshakeMessage.cc#L71-L82)：`create()` 按固定偏移解析收到的字节——`data[0]` 是 pstrlen、`data[1..19]` 协议名、`data[20..27]` 保留位、`data[28..47]` info_hash、`data[48..67]` peer_id。
- [src/BtHandshakeMessage.cc:84-94](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtHandshakeMessage.cc#L84-L94)：`createMessage()` 反向序列化为 68 字节。

aria2 自己亮出的能力位：

- [src/BtHandshakeMessage.cc:60-69](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtHandshakeMessage.cc#L60-L69)：`init()` 把保留位清零后置两个恒定位——`reserved_[7] |= 0x04`（Fast Extension）与 `reserved_[5] |= 0x10`（扩展协议）。
- [src/DefaultBtMessageFactory.cc:262-270](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageFactory.cc#L262-L270)：发送用的工厂重载在创建后补 `setDHTEnabled(dhtEnabled_)`——DHT 位取决于本机 DHT 节点是否已启动（`dhtEnabled_` 由 `PeerInteractionCommand` 构造时按 `DHTRegistry` 状态设置，见 [src/PeerInteractionCommand.cc:240-250](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L240-L250)）；`setDHTEnabled` 的位操作在 [src/BtHandshakeMessage.h:92-100](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtHandshakeMessage.h#L92-L100)。

接收侧三个能力判定（一行一个比特）：

- [src/BtHandshakeMessage.cc:103-113](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtHandshakeMessage.cc#L103-L113)：`isFastExtensionSupported()`（`reserved_[7] & 0x04`）、`isExtendedMessagingEnabled()`（`reserved_[5] & 0x10`）、`isDHTEnabled()`（`reserved_[7] & 0x01`）。

把保留位打印成十六进制的日志（实践观察的锚点）：

- [src/BtHandshakeMessage.cc:96-101](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtHandshakeMessage.cc#L96-L101)：`toString()` 输出 `handshake peerId=<percent-encoded>, reserved=<16 个 hex 字符>`；打印点在 [src/DefaultBtInteractive.cc:160-162](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L160-L162)（`MSG_RECEIVE_PEER_MESSAGE`，info 级日志）。

三重校验器：

- [src/BtHandshakeMessageValidator.cc:55-72](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtHandshakeMessageValidator.cc#L55-L72)：`validate()` 依次检查 pstrlen==19、pstr 与 `BT_PSTR` 前缀全等、info_hash 与构造时传入的本地期望值逐字节相等；任一失败抛 `DL_ABORT_EX`（可恢复异常，最终表现为断开该 peer）。
- [src/DefaultBtMessageFactory.cc:251-260](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageFactory.cc#L251-L260)：接收工厂把校验器挂到消息上（期望的 info_hash 来自 `bittorrent::getInfoHash(downloadContext_)`）；校验由 `AbstractBtMessage::validate()` 统一触发（[src/AbstractBtMessage.cc:64-69](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractBtMessage.cc#L64-L69)），调用点在 [src/DefaultBtMessageReceiver.cc:76-79](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageReceiver.cc#L76-L79)。

握手成功后的能力落地与首条消息：

- [src/DefaultBtInteractive.cc:129-140](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L129-L140)：两道防御——对方 peer_id 与自己的完全相同（自连接）则丢弃；与任一在用 peer 的 peer_id 重复也丢弃（同一客户端的多条连接只留一条）。
- [src/DefaultBtInteractive.cc:142-159](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L142-L159)：`setPeerId` 后依次检查三个保留位，各打一条 info 日志（`Fast extension enabled.` / `Extended Messaging enabled.` / `The peer is DHT-enabled.`，文案见 [src/message.h:89-90](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h#L89-L90)、[L104](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h#L104)），并把能力写进 `Peer` 的会话资源（[src/Peer.cc:263-267](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Peer.cc#L263-L267)、[L338-L342](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Peer.cc#L338-L342)、[L350-L354](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Peer.cc#L350-L354)）。
- [src/DefaultBtInteractive.cc:172-191](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L172-L191)：`doPostHandshakeProcessing` 按刚写入的能力决定握手后的第一批消息：支持扩展协议 → 发扩展握手（u6-l8 的 ut_pex/ut_metadata 协商入口）；双方 DHT 位都开 → 发 port 消息互通 DHT 端口；支持 Fast Extension → 用 HaveAll/HaveNone/AllowedFast 替代传统 bitfield（u6-l5 消息族）。

#### 4.4.4 代码实践

1. **实践目标**：不改 aria2，先用单元测试确认「手工构造的 68 字节 → 保留位判定」链路与你的理解一致。
2. **操作步骤**：
   - 打开 [test/BtHandshakeMessageTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/BtHandshakeMessageTest.cc)，重点看 `testCreate`（从字节解析）、`testCreateMessage`（序列化回字节）、`testSetDHTEnabled`（DHT 位翻转）三个用例（注册见 [L15-L18](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/BtHandshakeMessageTest.cc#L15-L18)）。
   - 纸面推演：一个 reserved 为 `00 00 00 00 00 00 00 05` 的 peer（`reserved[7]=0x05`，即 bit 63 + bit 61），三个判定函数分别返回什么？再算 `00 00 00 00 00 10 00 00`（`reserved[5]=0x10`，bit 43）呢？
3. **需要观察的现象**：`0x05` → `isDHTEnabled()=true`、`isFastExtensionSupported()=true`、`isExtendedMessagingEnabled()=false`；`reserved[5]=0x10` → 仅扩展协议为 true。
4. **预期结果**：与测试用例断言一致；aria2 自己（DHT 开启时）发出的 reserved 十六进制为 `0000000000100005`——`reserved[5]=0x10` 加 `reserved[7]=0x05`，可与日志里对端回报的值相互印证。
5. 测试断言部分**待本地验证**（需 `make check`）；十六进制推演可在纸面完成。

#### 4.4.5 小练习与答案

**练习 1**：为什么 aria2 把 Fast Extension 与扩展协议两位**恒置**，而 DHT 位要视情况置位？

**答案**：Fast Extension 与扩展协议是纯软件实现、不依赖外部状态，aria2 永远能陪对端玩（[src/BtHandshakeMessage.cc:64-68](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtHandshakeMessage.cc#L64-L68) 无条件置位）。DHT 位则承诺「我会监听 UDP 端口应答 get_peers」——若 `--enable-dht=false` 或 IPv6 DHT 未就绪，置位就是虚假承诺，对端发来的 port 消息也无从兑现，所以由工厂按 `dhtEnabled_` 现状决定（[src/DefaultBtMessageFactory.cc:266-268](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageFactory.cc#L266-L268)）。

**练习 2**：握手校验为什么把 info_hash 不匹配归为「可恢复异常」（`DL_ABORT_EX`）而不是致命错误？

**答案**：info_hash 不匹配最常见的原因是对端要下别的种子、恰好连到我们，属正常网络行为。`DL_ABORT_EX` 对应 `RecoverableException` 家族，在 `PeerAbstractCommand::execute` 中只导致**这一条连接**断开、命令链换下一个 peer（[src/PeerAbstractCommand.cc:108-114](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerAbstractCommand.cc#L108-L114)），不影响整个下载任务。若当致命错误处理，一次误连就会中止下载。

**练习 3**：日志里看到 `reserved=0000000000000000` 的对端，握手后会发生什么？

**答案**：三个判定全为 false：不会发扩展握手（ut_pex/ut_metadata 不可用，u6-l8）、不会发 port 消息、bitfield 用传统格式发送（`doPostHandshakeProcessing` 的三个分支都不触发，[src/DefaultBtInteractive.cc:178-190](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L178-L190)）。这是一个「只会基础协议」的老客户端，连接仍可用，只是没有增强能力。

## 5. 综合实践

**任务**：统计一次真实 BT 下载中各 peer 的保留位能力分布，验证你对 68 字节结构的理解。

1. **准备**：选一个健康的合法种子（推荐发行版 ISO 的官方 .torrent，做种者多且不涉版权）。按下面命令启动（`--bt-max-peers=5` 对应规格任务的设定）：

   ```bash
   ./src/aria2c --bt-max-peers=5 --seed-time=0 -d /tmp/bt-lab \
     --enable-dht=true --listen-port=6881-6889 \
     --log=/tmp/bt-lab.log --log-level=info <种子文件>
   ```

2. **收集**：等待若干 peer 接入后停止（Ctrl-C 一次即可优雅退出）。在日志中检索握手成功行：

   ```bash
   grep -E "handshake peerId=" /tmp/bt-lab.log
   ```

   每行形如 `CUID#12 - From: 203.0.113.7:51433 handshake peerId=%2DAR123..., reserved=0000000000100005`（该行由 [src/DefaultBtInteractive.cc:160-162](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L160-L162) 打印，格式定义在 [src/BtHandshakeMessage.cc:96-101](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtHandshakeMessage.cc#L96-L101)）。

3. **分析**：对每个 16 位十六进制串，取第 11、12 个字符（`reserved[5]`）与最后两个字符（`reserved[7]`），按下表判定三项能力并统计分布：

   | 字符 | 含义 |
   | --- | --- |
   | `reserved[5]` 含 `1`（即 `..10`） | 扩展协议（BEP 10，u6-l8 的前置） |
   | `reserved[7]` 二进制含 `100`（`0x04`） | Fast Extension（BEP 6） |
   | `reserved[7]` 二进制含 `001`（`0x01`） | DHT（BEP 5） |

   预期主流客户端（Transmission、qBittorrent、libtorrent 系）三位全开，即 `0000000000100005`。

4. **交叉验证**：把保留位判定的统计与这三行 info 日志的条数对账——`Fast extension enabled.`、`Extended Messaging enabled.`、`The peer is DHT-enabled.`（[src/DefaultBtInteractive.cc:144-159](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L144-L159)），两者应严格一致。再用 `grep -c "Incoming connection"` 与 `grep -c "Accepted the connection"` 对比，估算入站连接的握手成功率。

5. **扩展**（可选）：改用 `--enable-dht=false` 重跑，观察 aria2 自己发出的保留位是否从 `...0005` 变为 `...0004`，并解释原因（提示：[src/DefaultBtMessageFactory.cc:266-268](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageFactory.cc#L266-L268) 的 `setDHTEnabled(dhtEnabled_)`——注意本机发出的 reserved 值需用抓包或 debug 日志观察，info 日志只打印对端的）。

   实际能连到的 peer 数量与网络环境相关，统计结果**待本地验证**。

## 6. 本讲小结

- BT 的「两次握手」= TCP 三次握手（`PeerListenCommand` accept）+ 68 字节应用层握手；入站连接经 `PeerListenCommand → ReceiverMSEHandshakeCommand（嗅探明文/MSE）→ PeerReceiveHandshakeCommand（校验与准入）→ PeerInteractionCommand（常驻会话）` 四棒接力，出站则是 `PeerInitiateConnectionCommand → PeerInteractionCommand(INITIATOR_SEND_HANDSHAKE)` 两棒。
- 68 字节握手 = `1 字节 pstrlen(19) + 19 字节协议名 + 8 字节保留位 + 20 字节 info_hash + 20 字节 peer_id`；info_hash 用来反查 `BtRegistry` 找到任务上下文，peer_id 用来去重与防自连接。
- 保留位是能力协商位图：`reserved[7]&0x01`=DHT、`reserved[7]&0x04`=Fast Extension、`reserved[5]&0x10`=扩展协议；判定逻辑内联在 `BtHandshakeMessage` 的方法里，握手成功后写入 `Peer` 的会话资源并决定握手后的第一批消息。
- `PeerConnection` 是可移动的字节流管家：定长 `receiveHandshake`（支持 peek 偷看）与三态状态机 `receiveMessage`（4 字节大端长度 + 载荷，keepalive 是长度为 0 的特例）分别处理两种封帧，进度全部外置于成员变量因此免疫粘包/半包。
- `DefaultPeerStorage` 用候选池（512 上限）/在用集/`(IP,origPort)` 去重集三张表管理 peer，入站握手走 `addAndCheckoutPeer` 插队直达在用集；坏 peer 被随机封禁 120~720 秒，优雅断开的好 peer 进 `droppedPeers_` 待复连。
- 命令骨架 `PeerAbstractCommand` 提供 `--bt-timeout`（默认 180 秒）超时兜底与三类异常出口；嗅探阶段用更短的 `--peer-connection-timeout`（默认 20 秒），握手校验失败属可恢复异常，只断单条连接不伤任务。

## 7. 下一步学习建议

- **u6-l4 DefaultBtInteractive：与单个 peer 的交互循环**：本讲止步于 `PeerInteractionCommand` 进入 `WIRED` 状态的那一刻，下一讲讲 `WIRED` 里的收发循环、keepalive 与 choke 决策。
- **u6-l5 BtMessage 家族**：握手之后的 bitfield/request/piece 等消息如何经 `DefaultBtMessageReceiver/Factory/Dispatcher` 三件套流转，本讲的 `PeerConnection::receiveMessage` 状态机正是它们的接收地基。
- **u6-l8 扩展协议**：本讲看到「对端支持扩展协议」时 `doPostHandshakeProcessing` 会发出扩展握手，真正的 id 协商与 ut_pex/ut_metadata 在那一讲展开。
- 建议顺带阅读 [src/PeerInteractionCommand.cc:297-377](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L297-L377) 的完整状态机，它是本讲与下一讲之间的桥梁。
