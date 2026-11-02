# 扩展协议：extended handshake、PEX 与 ut_metadata

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 BEP10 扩展协议的分层设计：为什么所有扩展消息都要装进 `id=20` 的 BtExtendedMessage 外壳里传输。
2. 解释扩展消息 id 的「每对连接独立协商」机制：本端 id 表（ExtensionMessageRegistry）与对端 id 表（PeerSessionResource）各自的作用，以及 extended handshake 里 `m` 字典的协商过程。
3. 读懂 ut_pex 的消息负载结构（`added`/`added.f`/`dropped`/`added6` 等），并说明 PEX 如何带来 tracker 之外的 peer。
4. 描述 ut_metadata（BEP 9）的 request/data/reject 三种消息与 16KiB 分片传输、SHA-1 终检的完整流程，为下一讲 magnet 链接做好铺垫。
5. 通过加日志的实践，亲眼观察到「与每个 peer 协商得到的扩展 id 表」以及 PEX 开关对 peer 来源数量的影响。

一个诚实的更正：本讲任务书中提到的选项 `--bt-enable-pex` 在当前源码中并不存在，真实的选项名是 `--enable-peer-exchange`（映射到 pref 键 `enable-peer-exchange`，默认 `true`）；任务书提到的 `lt_tex` 扩展 aria2 也未实现——aria2 只实现了 uTorrent 兼容的二进制 PEX（`ut_pex`）。本讲全部以真实源码为准。

## 2. 前置知识

- **BEP10 扩展协议（Extension Protocol）**：BitTorrent 主协议只定义了 0～19 号消息。BEP10 约定：主握手（handshake）保留字节的第 5 字节（`reserved_[5]`）的 `0x10` 比特置 1，表示「我支持扩展协议」；双方随后交换一条**扩展握手（extended handshake）**，用一个 bencode 字典 `m` 声明「我支持哪些扩展、每个扩展用哪个数字 id」。此后所有扩展消息都走 20 号消息，消息体第一个字节是扩展 id。
- **为什么 id 要协商**：BEP10 规定扩展的数字 id **由每个客户端自行分配**、只在「我发的消息」里有效。也就是说，同一个 `ut_pex` 扩展，aria2 用 id 8，对端可能用 id 5。于是产生两张表：
  - **本端 id 表**：「我」通告给对端的 id，对端给我发消息时必须用这张表里的 id；
  - **对端 id 表**：「对端」通告的 id，我给对端发消息时必须用它。
- **bencode**：BT 世界的序列化格式（字符串/整数/列表/字典），在 u6-l1 已精读。扩展消息的 payload 全部是 bencode 字典，ut_metadata 的 data 消息例外——bencode 字典后面直接拼接原始字节。
- **保留位回顾**（u6-l3）：68 字节握手中的 8 字节保留位，`reserved_[7] & 0x01` 是 DHT、`reserved_[7] & 0x04` 是 Fast Extension、`reserved_[5] & 0x10` 就是本讲的扩展协议。
- **交互循环回顾**（u6-l4）：每个 peer 连接由 PeerInteractionCommand 常驻托管，进入稳定态后每 tick 驱动一次 `DefaultBtInteractive::doInteractionProcessing()`。本讲的扩展消息收发都挂在这个循环里。
- **消息三件套回顾**（u6-l5）：接收器读满「4 字节长度前缀 + 载荷」，工厂按消息 id 实例化对象，派发器管发送队列。本讲的 BtExtendedMessage 就是在工厂的 `case 20` 分支被创建的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/BtExtendedMessage.cc/.h` | 20 号扩展消息的统一外壳：分帧、装卸扩展子消息 |
| `src/DefaultBtMessageFactory.cc` | 消息工厂：`case 20` 分派到 BtExtendedMessage |
| `src/DefaultExtensionMessageFactory.cc` | 扩展消息工厂：按扩展 id / 扩展名实例化 handshake、ut_pex、ut_metadata 族 |
| `src/ExtensionMessageRegistry.cc/.h` | 「扩展名 ↔ 本端 id」注册表，只登记 aria2 支持的扩展 |
| `src/HandshakeExtensionMessage.cc/.h` | extended handshake：构造/解析 `m` 字典，落地协商结果 |
| `src/UTPexExtensionMessage.cc/.h` | ut_pex 消息：紧凑 peer 列表的编解码与收发动作 |
| `src/UTMetadataExtensionMessage.cc/.h` 及 `UTMetadata{Request,Data,Reject}ExtensionMessage.cc` | ut_metadata 消息族基类与三种 msg_type |
| `src/UTMetadataRequestFactory.cc` / `src/UTMetadataRequestTracker.cc` | metadata 分片请求的生成与在途记账（每 peer 最多 1 个在途、20 秒超时） |
| `src/DefaultBtInteractive.cc` | 挂接点：发送 extended handshake、周期发送 PEX、metadataGetMode 循环 |
| `src/PeerInteractionCommand.cc` | 装配点：为每条连接创建注册表（本端 id 固定 UT_PEX=8、UT_METADATA=9）并决定是否启用 PEX |
| `src/Peer.cc` / `src/PeerSessionResource.cc` | 对端 id 表的真正存放处 |
| `test/HandshakeExtensionMessageTest.cc` 等 | 各扩展消息的单元测试，实践的参照物 |

## 4. 核心概念与源码讲解

### 4.1 BtExtendedMessage：所有扩展消息的统一外壳

#### 4.1.1 概念说明

BEP10 没有为每个扩展定义新的消息 id，而是「借用」20 号消息当集装箱：集装箱上贴一个扩展 id（1 字节），箱子里装具体扩展自己的 payload。这样主协议的消息解析逻辑完全不用改——对不认识扩展 id 的一端，最多丢弃一个箱子，而不是解析失败。

aria2 中这个集装箱就是 `BtExtendedMessage`，它内部持有一个 `ExtensionMessage` 指针（真正的货物），自己只负责贴标签和装卸：

- **扩展 id 0** 被保留给 extended handshake 本身（协商用的第一条消息）；
- 其他扩展 id 由双方在 extended handshake 中协商（见 4.2）。

#### 4.1.2 核心流程

线上字节格式（承接 u6-l5 的长度前缀分帧）：

```text
+-------------+----------+------------------+---------------------------+
| len (4字节)  | id=20    | extmsgid (1字节)  | extpayload (len-2 字节)    |
| = 2+payload | (1字节)  | 0=handshake      | bencode 字典(ut_metadata   |
|             |          | 其他=协商出的 id   | 的 data 另拼原始字节)       |
+-------------+----------+------------------+---------------------------+
```

发送侧：`createMessage()` 先向货物要 payload，再拼出 6 + payload 长度的报文；接收侧：主工厂按 `id=20` 创建 BtExtendedMessage，把 `extmsgid + payload` 整体转交给 `ExtensionMessageFactory` 去识别货物。

#### 4.1.3 源码精读

先看外层分帧——`createMessage` 拼装完整报文，注释里就是上面的字节布局图：

- [src/BtExtendedMessage.cc:61-79](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtExtendedMessage.cc#L61-L79) — 先调用货物的 `getPayload()`，`createPeerMessageString` 写入 4 字节长度（`2 + payload.size()`）与 id=20，随后写入 1 字节扩展 id，最后拷贝 payload。
- [src/BtExtendedMessage.h:54](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtExtendedMessage.h#L54) — `static const uint8_t ID = 20;`，扩展协议在主协议中的固定消息号。

发送前提——只有对端在主握手里置了扩展协议保留位，才允许发 20 号消息：

- [src/BtExtendedMessage.cc:81-84](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtExtendedMessage.cc#L81-L84) — `sendPredicate()` 返回 `getPeer()->isExtendedMessagingEnabled()`，为假时派发器不会真正发送这条消息。

接收侧入口——主消息工厂的 `case 20` 分支，同样有保留位闸门：

- [src/DefaultBtMessageFactory.cc:218-226](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageFactory.cc#L218-L226) — 若对端没声明扩展能力却发来 20 号消息，直接抛 `DL_ABORT_EX` 断连（协议违约防御）；否则调用 `BtExtendedMessage::create`。
- [src/BtExtendedMessage.cc:94-104](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtExtendedMessage.cc#L94-L104) — `create` 校验长度与 id 后，把去掉 1 字节消息 id 的剩余部分（即 `extmsgid + payload`）整体交给 `factory->createMessage()` 识别货物。
- [src/BtExtendedMessage.cc:106-111](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtExtendedMessage.cc#L106-L111) — `doReceivedAction()` 只是转发给内部的 ExtensionMessage，外壳本身无协议动作——典型的组合模式。

最后是「识别货物」的扩展消息工厂，按第一个字节分流：

- [src/DefaultExtensionMessageFactory.cc:83-107](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultExtensionMessageFactory.cc#L83-L107) — `extensionMessageID == 0` 走 handshake；非 0 则用注册表 `getExtensionName(id)` 反查扩展名（用的是**本端 id 表**——对端按我们通告的 id 发消息），查不到就抛「No extension registered」断连；`ut_pex` 分支在此实例化。
- [src/DefaultExtensionMessageFactory.cc:108-168](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultExtensionMessageFactory.cc#L108-L168) — `ut_metadata` 分支：先解出 bencode 字典里的 `msg_type` 与 `piece`，再按 `msg_type` 0/1/2 分别实例化 request/data/reject 三种消息（data 分支还要切出字典后拼接的原始字节，见 4.4）。

#### 4.1.4 代码实践

1. **实践目标**：亲眼在日志里看到「20 号集装箱」里装了什么货物。
2. **操作步骤**：
   - 在 [src/BtExtendedMessage.cc:106-111](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtExtendedMessage.cc#L106-L111) 的 `doReceivedAction()` 开头加一行日志（示例代码）：
     ```cpp
     if (extensionMessage_) {
       A2_LOG_INFO(extensionMessage_->toString());
     }
     ```
     `toString` 已包含扩展名与参数摘要（如 `extended handshake client=...`、`ut_pex added=3, dropped=0`），无需自行拼格式。注意该文件需要 `#include "LogFactory.h"` 与 `#include "Logger.h"`（若编译报错则补上）。
   - 重新编译：在项目根目录执行 `make -j$(nproc)`（前置条件见 u1-l2）。
   - 下载任一种子并开日志：`./src/aria2c --enable-dht=true -l pex.log --log-level=info "<torrent 文件或 magnet>"`。
3. **需要观察的现象**：日志中应出现 `extended handshake client=...`（每个 peer 连接建立后一条）与周期性的 `ut_pex added=N, dropped=M`。
4. **预期结果**：handshake 在前、ut_pex 以约 1 分钟为周期重复出现；`toString` 的输出格式分别来自 4.2、4.3 要精读的两个函数。
5. 具体日志条数依种子热度而异，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `BtExtendedMessage::createMessage` 计算的长度字段是 `2 + payload.size()`，而整个报文却是 `6 + payload.size()` 字节？

**答案**：长度前缀只统计它后面的内容：1 字节消息 id + 1 字节扩展 id + payload = `2 + payload`；再加上长度字段自身的 4 字节，总共 `6 + payload` 字节。见 [src/BtExtendedMessage.cc:70-78](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtExtendedMessage.cc#L70-L78)。

**练习 2**：如果一个不支持扩展协议的 peer 发来 20 号消息，aria2 会怎样？

**答案**：`DefaultBtMessageFactory` 的 `case 20` 分支检查 `peer_->isExtendedMessagingEnabled()`，为假时抛 `DL_ABORT_EX`，该连接被放弃（走 u6-l3 讲过的可放弃异常出口）。见 [src/DefaultBtMessageFactory.cc:218-225](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtMessageFactory.cc#L218-L225)。

**练习 3**：`sendPredicate()` 返回 false 时这条消息去哪了？

**答案**：它不会被发送。`sendPredicate` 是 u6-l5 讲过的发送前置谓词，派发器在冲刷队列时对谓词为假的消息做放弃处理（如自我作废），保证「对端不具备能力就不发对应消息」。

### 4.2 HandshakeExtensionMessage 与 ExtensionMessageRegistry：id 协商机制

#### 4.2.1 概念说明

这条「扩展握手」解决的问题是：**两条方向上的 id 表怎么建立起来**。

aria2 的实现里有三处关键设计：

1. **注册表极小化**：`ExtensionMessageRegistry` 只登记 aria2 自己支持的扩展——枚举 `InterestingExtension` 只有两个成员 `UT_METADATA`、`UT_PEX`。对端通告的其他扩展（如 `lt_tex`、各类私有扩展）一律忽略并打 debug 日志。
2. **本端 id 固定**：每条连接创建注册表时写死 `UT_PEX=8`、`UT_METADATA=9`（BEP 只要求「本端内唯一」，数值随意）。
3. **两张表分开存**：本端表放在 `DefaultBtInteractive::extensionMessageRegistry_`（生成 `m` 字典、接收时反查扩展名都用它）；对端表放进 `PeerSessionResource`（给对端发消息时取 id 用）。`Peer::setExtension/getExtensionMessageID` 只是对后者的转发。

一个容易被忽略的细节：**「是否通告 PEX」和「是否启用 PEX」是同一个开关的两面**。若 `--enable-peer-exchange=false`（或私有种子、本地 peer），协商前会把 `UT_PEX` 从本端表里**移除**——于是我们发出的 `m` 字典里没有 `ut_pex`，对端自然不会给我们发 PEX；即便有对端硬用 id 8 发来，工厂反查本端表也会失败断连。

#### 4.2.2 核心流程

一次完整的协商时序（嵌在 u6-l3/u6-l4 的握手流程之后）：

```text
TCP + 主握手（reserved_[5] & 0x10 双方置位）
        │
        ▼
doPostHandshakeProcessing()
        │  本端：若 !utPexEnabled_ → 注册表 removeExtension(UT_PEX)
        ▼
本端发送 extended handshake（extmsgid=0）
  payload = {v: 客户端版本, p: 监听端口, m: {ut_metadata:9, ut_pex:8*}, metadata_size*}
        │                        (* 视开关/是否有 metadata 而定)
        ▼
对端也发来 extended handshake
        │  HandshakeExtensionMessage::create 解析 m 字典：
        │    只认 ut_metadata / ut_pex 两个名字，id 越界(>255)或未知名字均跳过
        ▼
doReceivedAction()
        ├─ peer_->setPort(p)、setExtension(key, id)  → 对端 id 表落位
        └─ metadataGetMode（我们还没有种子 metadata）时：
             ├─ 对端不支持 ut_metadata → 抛异常断连（"Goodbye"）
             ├─ metadata_size 首次到达 → 定长、initPieceStorage、enterEndGame
             └─ metadata_size 与已知不符 / 为 0 → 断连
```

此后给该 peer 发 PEX 时用 `peer_->getExtensionMessageID(UT_PEX)`（对端 id），发 ut_metadata 请求时同理。

#### 4.2.3 源码精读

**注册表本身**——一张只有两个格子的表：

- [src/ExtensionMessageRegistry.h:52-59](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ExtensionMessageRegistry.h#L52-L59) — 类注释明确说明它遵循 BEP10、只存 aria2 支持的扩展；`InterestingExtension` 枚举定义 `UT_METADATA`、`UT_PEX`。
- [src/ExtensionMessageRegistry.cc:49-51](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ExtensionMessageRegistry.cc#L49-L51) — 名字表 `EXTENSION_NAMES[] = {"ut_metadata", "ut_pex", nullptr}`，枚举序号即数组下标。
- [src/ExtensionMessageRegistry.cc:53-83](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ExtensionMessageRegistry.cc#L53-L83) — `getExtensionMessageID`（key→id）、`getExtensionName`（id→名字，线性扫描，id=0 返回空）、`setExtensionMessageID`、`removeExtension`（置 0 即「不支持」）。
- [src/ExtensionMessageRegistry.cc:90-109](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ExtensionMessageRegistry.cc#L90-L109) — `strBtExtension`/`keyBtExtension` 在「枚举 key ↔ 扩展名字符串」之间互转，是解析 `m` 字典时的翻译器；未知名字返回 `MAX_EXTENSION`。

**装配点**——PeerInteractionCommand 为每条连接配好整套对象（u6-l2 的 BtSetup 装配的延续）：

- [src/PeerInteractionCommand.cc:120-127](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L120-L127) — 创建注册表并写死本端 id：`UT_PEX=8`、`UT_METADATA=9`（注释指向 BEP 9）；同一个注册表对象同时交给 `DefaultExtensionMessageFactory`（接收时反查名字用）。
- [src/PeerInteractionCommand.cc:232-236](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L232-L236) — PEX 启用三条件：非私有种子（或 metadataGetMode）、对端不是本地 peer、`--enable-peer-exchange` 为真；满足才 `setUTPexEnabled(true)`。
- [src/PeerInteractionCommand.cc:225-226](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L225-L226) — 注册表与扩展工厂分别注入 `DefaultBtInteractive`（工厂持裸指针、interactive 接管所有权，二者指向同一张表）。

**发送侧**——主握手完成后立刻发出 extended handshake：

- [src/DefaultBtInteractive.cc:148-155](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L148-L155) — 收到主握手、确认扩展协议保留位后：`utPexEnabled_` 为假则 `removeExtension(UT_PEX)`——这就是「不通告即不支持」的实现处。
- [src/DefaultBtInteractive.cc:172-191](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L172-L191) — `doPostHandshakeProcessing()`：置零 `pexTimer_`（让 4.3 的首个 PEX 立即可发），随后按能力依次排队 extended handshake、bitfield、port(DHT) 等首波消息。
- [src/DefaultBtInteractive.cc:199-211](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L199-L211) — `addHandshakeExtendedMessageToQueue()`：填入客户端版本、监听端口、`setExtensions(本端表)`，若本端已持有 metadata 再填 `metadata_size`，最后包一层 `createBtExtendedMessage` 入队。

**payload 生成**——`m` 字典就是本端表的投影：

- [src/HandshakeExtensionMessage.cc:59-80](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HandshakeExtensionMessage.cc#L59-L80) — `getPayload()`：可选的 `v`（版本）、`p`（端口，>0 才写）、`m`（遍历注册表，id 非 0 的才写进字典）、`metadata_size`（非 0 才写），整体 bencode 编码。
- [src/HandshakeExtensionMessage.cc:82-95](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HandshakeExtensionMessage.cc#L82-L95) — `toString()`：日志里那句 `extended handshake client=..., tcpPort=..., metadataSize=..., ut_metadata=9, ut_pex=8` 的出处（版本串做了 percent-encode）。

**接收与解析**：

- [src/HandshakeExtensionMessage.cc:163-207](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HandshakeExtensionMessage.cc#L163-L207) — `create()`：跳过 1 字节扩展 id 后 bencode 解码；`p` 必须在 (0, 65536) 开区间；`m` 字典里 id 超出 [0,255] 打日志跳过，名字不在 `EXTENSION_NAMES` 里只记 debug 不报错（对端可能通告一堆我们不认识扩展，属正常情况）。
- [src/HandshakeExtensionMessage.cc:208-224](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HandshakeExtensionMessage.cc#L208-L224) — `metadata_size` 防御：负数直接抛异常；上限 `8_m`（8 MiB），防止恶意超大值。

**协商结果落地**——`doReceivedAction`：

- [src/HandshakeExtensionMessage.cc:97-108](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HandshakeExtensionMessage.cc#L97-L108) — `p>0` 时修正 peer 的监听端口并把 `incomingPeer` 置 false（此后该 peer 才适合被 PEX 广播，见 4.3）；随后把对端通告的每个扩展 id 写入 `peer_->setExtension(i, id)`。
- [src/HandshakeExtensionMessage.cc:109-141](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HandshakeExtensionMessage.cc#L109-L141) — metadataGetMode 分支（本端还没有种子元数据时）：对端不支持 ut_metadata 即抛 `"Peer doesn't support ut_metadata extension. Goodbye."` 断连；`metadata_size` 首次到达则设定文件长度、`markTotalLengthIsKnown`、`initPieceStorage` 并**从一开始就进入 end-game 模式**（metadata 分片无优先级可言，尽快凑齐）；两个防御出口：size 不一致、size 为 0（对端没有完整 metadata）。
- [src/Peer.cc:320-336](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Peer.cc#L320-L336) 与 [src/PeerSessionResource.cc:177-190](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerSessionResource.cc#L177-L190) — `Peer::setExtension/getExtensionMessageID` 只是转发给会话资源里的另一个注册表实例——**对端 id 表**。

**扩展能力对 peer 选择的间接影响**（本讲学习目标之三）：aria2 从不主动「按扩展挑 peer」，但两个间接通道真实存在——其一，metadataGetMode 下不支持 ut_metadata 的 peer 会被直接断开（如上）；其二，PEX 协商失败只是少了 peer 来源，普通数据传输不受影响，peer 候选池的丰俭由 4.3 的机制间接决定连接建立速度。

#### 4.2.4 代码实践

1. **实践目标**：打印「与每个 peer 协商得到的扩展 id 表」，验证 4.2.2 的时序。
2. **操作步骤**：
   - 在 [src/HandshakeExtensionMessage.cc:103-108](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HandshakeExtensionMessage.cc#L103-L108) 的 for 循环后加一行（示例代码）：
     ```cpp
     A2_LOG_INFO(fmt("Peer %s:%u negotiated: ut_metadata=%u, ut_pex=%u",
                     peer_->getIPAddress().c_str(), peer_->getPort(),
                     peer_->getExtensionMessageID(ExtensionMessageRegistry::UT_METADATA),
                     peer_->getExtensionMessageID(ExtensionMessageRegistry::UT_PEX)));
     ```
   - 再在 [src/PeerInteractionCommand.cc:120-124](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L120-L124) 之后加一行打印本端固定 id（`local offer: ut_pex=8, ut_metadata=9`），确认「每条连接一张新表」。
   - 重新编译并下载种子：`./src/aria2c -l negotiate.log --log-level=info "<torrent>"`。
3. **需要观察的现象**：每个完成扩展握手的 peer 都有一条 negotiated 日志；不同 peer 的 `ut_pex` id 可能不同（对端自选），但 `ut_metadata`/`ut_pex` 为 0 表示对端未通告该扩展。
4. **预期结果**：热门种子的多数 peer 两个 id 都非 0；个别客户端只支持其中之一。若想看「不通告 PEX」，可加 `--enable-peer-exchange=false` 对比 negotiated 行里对端 ut_pex 是否依旧非 0（对端仍会通告，但我们不启用——发送侧闸门在 4.3）。
5. 日志条数与取值依 peer 而异，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `DefaultBtInteractive` 里的注册表和 `DefaultExtensionMessageFactory` 里的注册表是同一张？各自用它做什么？

**答案**：见 [src/PeerInteractionCommand.cc:120-127](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L120-L127) 与 [src/PeerInteractionCommand.cc:225-226](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L225-L226)。二者必须一致，因为 BEP10 规定「对端按我们通告的 id 给我们发消息」：工厂用它把收到的扩展 id 反查成扩展名；interactive 用它生成 `m` 字典。若是两张不同的表，通告的 id 和解码的 id 就对不上了。

**练习 2**：`--enable-peer-exchange=false` 时，对端给我们发来 ut_pex 消息（用了我们的 id 8）会发生什么？

**答案**：协商前 `UT_PEX` 已被 `removeExtension` 置 0（[src/DefaultBtInteractive.cc:148-155](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L148-L155)），且我们的 `m` 字典里本就没有 ut_pex，正常对端不会发；若强发，[src/DefaultExtensionMessageFactory.cc:96-101](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultExtensionMessageFactory.cc#L96-L101) 反查不到 id=8 对应的名字，抛异常断连。

**练习 3**：extended handshake 里的 `p` 字段有什么用？为什么 aria2 在收到它之后要把 `incomingPeer` 置为 false？

**答案**：`p` 是对端的监听端口。入站连接的对端端口是临时端口，直接广播出去别人也连不上；拿到 `p` 后 aria2 修正端口并把该 peer 标记为「可广播」（[src/HandshakeExtensionMessage.cc:99-102](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HandshakeExtensionMessage.cc#L99-L102)），4.3 的 `addFreshPeer` 才会接纳它。

### 4.3 ut_pex：peer 交换（PEX）

#### 4.3.1 概念说明

PEX（Peer Exchange）让两个已连接的 peer 互相告知「我还知道哪些 peer」，从而**绕过 tracker 扩散 peer 列表**——tracker 挂了、限流了，peer 网络照样滚雪球。aria2 实现的是 uTorrent 兼容的 `ut_pex`（二进制紧凑格式）；任务书里提到的 `lt_tex`（文本格式 PEX）aria2 并未实现，注册表里只有 `ut_pex` 一个名字。

负载结构的要点：

- peer 不用字典逐个描述，而是**紧凑格式拼接**：IPv4 每peer 6 字节（4 字节 IP + 2 字节大端端口），IPv6 每-peer 18 字节；
- `added`（新增）/`dropped`（掉线）两个语义桶，v4、v6 各一套；
- `added.f` 是与 `added` 等长的 flag 串，aria2 只用一个比特：`0x02` 表示 seeder（已完成者）；
- 发送频率：默认每 1 分钟一次（`DEFAULT_INTERVAL = 1_min`），且握手后第一轮立即发一次。

#### 4.3.2 核心流程

发送侧（挂在 [src/DefaultBtInteractive.cc:581-584](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L581-L584) 的交互循环末尾）：

```text
每轮 doInteractionProcessing：
  if 对端协商了 UT_PEX 且本端 utPexEnabled_：
      if 距上次发送 >= 1 分钟（pexTimer_ 握手后被置零，首轮立即满足）：
          遍历 PeerStorage 的在用 peer：
              活跃、IP 不同于接收方、非入站（或端口已修正）、
              首次接触在 1 分钟内 → addFreshPeer（fresh 满 50 个为止）
          遍历 dropped 列表：掉线开始于 1 分钟内 → addDroppedPeer（至多 50 个）
          包成 BtExtendedMessage（用对端的 UT_PEX id）入队
```

接收侧：工厂识别 `ut_pex` → `create()` 解码四个字节串 → `doReceivedAction()` 把 fresh 与 dropped 两组 peer 全部交给 `PeerStorage::addPeer` 进入候选池。注意 **dropped 也被加入**：对端与某个 peer 断开，不代表本机也连不上它，值得一试。

#### 4.3.3 源码精读

- [src/UTPexExtensionMessage.h:118](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTPexExtensionMessage.h#L118) — `DEFAULT_INTERVAL = 1_min`，PEX 的固有节拍。
- [src/UTPexExtensionMessage.cc:49-56](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTPexExtensionMessage.cc#L49-L56) — 硬上限：fresh 与 dropped 各 50（v4+v6 合并计数），防止消息膨胀。
- [src/UTPexExtensionMessage.cc:71-92](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTPexExtensionMessage.cc#L71-L92) — `getPayload()`：只为非空列表写字段，四个键 `added`/`added.f`/`dropped`/`added6`/`added6.f`/`dropped6` 按需出现（`dropped` 无 flag 串）。
- [src/UTPexExtensionMessage.cc:94-119](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTPexExtensionMessage.cc#L94-L119) — `createCompactPeerListAndFlag()`：用 `bittorrent::packcompact` 把每个 peer 压成 6/18 字节，按地址族分拣到 v4/v6 两个桶；flag 位 `0x02` 标 seeder。
- [src/UTPexExtensionMessage.cc:128-132](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTPexExtensionMessage.cc#L128-L132) — `doReceivedAction()`：fresh 与 dropped 两组统统 `peerStorage_->addPeer`，进入 u6-l4 讲过的候选池。
- [src/UTPexExtensionMessage.cc:134-167](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTPexExtensionMessage.cc#L134-L167) — 入桶时间窗：fresh 要求「非入站 peer 且首次接触距今 < 1 分钟」（入站 peer 在对端通告 `p` 修正端口前不广播，呼应 4.2 练习 3）；dropped 要求「掉线开始距今 < 1 分钟」。两函数返回 bool，配合 `freshPeersAreFull`/`droppedPeersAreFull` 提前停止收集。
- [src/UTPexExtensionMessage.cc:195-229](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTPexExtensionMessage.cc#L195-L229) — `create()`：首字节是对端给 ut_pex 分配的 id（原样保存供回执使用），其后 bencode 字典四个键分别用 `bittorrent::extractPeer` 按 AF_INET/AF_INET6 解出 Peer 对象列表。
- [src/DefaultBtInteractive.cc:505-529](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L505-L529) — `addPeerExchangeMessage()`：发送侧收集逻辑，两个细节值得注意——排除与接收方同 IP 的 peer（对方多半已经认识它们），满 50 即停；构造消息用的 id 取自 `peer_->getExtensionMessageID(UT_PEX)`（**对端**的 id 表）。
- [src/DefaultBtInteractive.cc:581-584](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L581-L584) — 发送总闸门：`对端协商了 UT_PEX && utPexEnabled_`，两个条件缺一不可（前者来自 4.2 的协商，后者来自选项与私有种子判定）。
- [src/PeerInteractionCommand.cc:232-236](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L232-L236) — `utPexEnabled_` 的来源：`--enable-peer-exchange`（[src/prefs.cc:489](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/prefs.cc#L489) 定义键名，[src/OptionHandlerFactory.cc:1784-1786](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L1784-L1786) 定义默认值 `true`）且非私有种子且非本地 peer。

#### 4.3.4 代码实践

1. **实践目标**：量化「PEX 是否真的带来了 tracker 之外的额外 peer」。
2. **操作步骤**：
   - 延续 4.2.4 的日志补丁，另在 [src/UTPexExtensionMessage.cc:128-132](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTPexExtensionMessage.cc#L128-L132) 加一行：
     ```cpp
     A2_LOG_INFO(fmt("ut_pex received: added=%lu, dropped=%lu",
                     static_cast<unsigned long>(freshPeers_.size()),
                     static_cast<unsigned long>(droppedPeers_.size())));
     ```
   - 选一个 announce 返回 peer 数较少的种子，跑两次：
     ```bash
     ./src/aria2c --enable-peer-exchange=true  -l with-pex.log  --log-level=info "<种子>"
     ./src/aria2c --enable-peer-exchange=false -l no-pex.log   --log-level=info "<种子>"
     ```
   - 各运行 3～5 分钟后 Ctrl-C，统计两份日志：`grep -c "ut_pex received" with-pex.log`，以及连接过的不同 peer 数（可用 4.2.4 的 negotiated 行去重：`grep "negotiated:" with-pex.log | sort -u | wc -l`）。
3. **需要观察的现象**：`no-pex.log` 中没有 `ut_pex received` 行；`with-pex.log` 中该行以约 1 分钟周期出现，且 `added` 数字在前期递增。
4. **预期结果**：开 PEX 的一轮连接到的不同 peer 数不少于（通常明显多于）关闭的一轮；差值即可视为「PEX 贡献的 peer」。若种子本身 tracker 返回很少，效果更明显。
5. 具体数值高度依赖种子热度与网络环境，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`added.f` 中的 `0x02` 表示什么？aria2 在发送侧如何决定某个 peer 的 flag？

**答案**：`0x02` 表示该 peer 是 seeder（已完成下载）。发送侧在 [src/UTPexExtensionMessage.cc:109-113](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTPexExtensionMessage.cc#L109-L113) 用 `(*itr)->isSeeder() ? 0x02u : 0x00u` 生成。

**练习 2**：为什么 aria2 把收到的 `dropped` 列表也加进 PeerStorage，而不是避开这些 peer？

**答案**：见 [src/UTPexExtensionMessage.cc:128-132](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTPexExtensionMessage.cc#L128-L132)。「对端与 X 断开」只说明这一对连接不合适（满员、choke 策略、网络路径差异），不代表本机也连不上 X；多一个候选只赚不赔，真正的接纳与否由 PeerStorage 的去重与容量判定把关（u6-l4）。

**练习 3**：握手刚完成时 `pexTimer_` 被置为 `Timer::zero()`，这会导致什么？

**答案**：零时刻与当前 wallclock 的差远大于 1 分钟，所以 [src/DefaultBtInteractive.cc:507-508](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L507-L508) 的间隔判断首轮即成立——握手后第一波交互就立刻发送一次 PEX，让对端尽早拿到 peer 列表。

### 4.4 ut_metadata：种子元数据传输（BEP 9）

#### 4.4.1 概念说明

magnet 链接手里只有一个 infohash，没有种子文件。BEP 9（ut_metadata）解决「**从 peer 处下载种子元数据**」：把 info 字典（即 metadata，其 SHA-1 就是 infohash）当作一个虚拟文件，按 **16KiB 分片**（`METADATA_PIECE_SIZE = 16_k`）向 peer 逐片请求，收齐后整体做 SHA-1 与 infohash 比对，即「下载的内容天然自带校验」。

消息族一父三子，共用载荷里的 `msg_type` 区分：

| msg_type | 消息类 | 载荷（bencode 字典） | 语义 |
| --- | --- | --- | --- |
| 0 | UTMetadataRequestExtensionMessage | `{msg_type:0, piece:N}` | 请求第 N 片 |
| 1 | UTMetadataDataExtensionMessage | `{msg_type:1, piece:N, total_size:S}` + 原始字节 | 交付第 N 片（字典后直接拼数据） |
| 2 | UTMetadataRejectExtensionMessage | `{msg_type:2, piece:N}` | 拒绝（我没有元数据） |

两个设计点值得体会：

- **复用 piece 基础设施**：metadata 分片被当成普通 piece 装进 PieceStorage（元数据任务的 PieceStorage 由 `metadata_size` 初始化），于是「缺哪片、谁在途、超时回收」全部复用 u5-l2/u6-l6 已有的位图与 piece 机制，甚至从一开始就 `enterEndGame`（分片无优先级，多源抢同一片反而快）。
- **节流激进的在途控制**：每个 peer 同一时刻**最多 1 个**在途 metadata 请求（`avail()` 上限 1），20 秒超时归还重排——元数据很小，广撒网比深流水线更稳。

#### 4.4.2 核心流程

元数据获取模式（metadataGetMode）的单 peer 循环：

```text
扩展握手带来 metadata_size（4.2）
    → 设定文件长度、initPieceStorage、enterEndGame
每轮 doInteractionProcessing（metadataGetMode 分支）：
    ├─ avail() = 1 - 在途数（每 peer 最多 1 个在途）
    ├─ UTMetadataRequestFactory::create(num)
    │     pieceStorage->getMissingPiece(排除已跟踪分片) → 生成 request 入队
    ├─ 每秒：tracker.removeTimeoutEntry() → 超 20 秒的在途分片 cancelPiece 归还
    └─ pieceStorage->downloadFinished()？
          → 对整个 metadata 做 SHA-1，与 infohash 比对
              一致 → "Got ut_metadata"，任务强制停止（交给 u6-l9 的后处理器）
              不一致 → 全部 markPieceMissing 重来，并断开该 peer

对端发来 request 时（我方已有 metadata 的普通场景）：
    无 metadata → 回 reject；index 合法 → 切出 [index*16K, min((index+1)*16K, 末尾))
    的字节回 data；index 越界 → 抛异常断连
对端发来 reject 时：直接断连换 peer（DL_ABORT_EX）
```

#### 4.4.3 源码精读

**消息族基类与三种子消息**：

- [src/UTMetadataExtensionMessage.h:42-66](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTMetadataExtensionMessage.h#L42-L66) — 基类只持有「对端给 ut_metadata 分配的扩展 id」与分片号 `index_`，`EXTENSION_NAME` 为 `ut_metadata`（[src/UTMetadataExtensionMessage.cc:39](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTMetadataExtensionMessage.cc#L39)）。
- [src/UTMetadataRequestExtensionMessage.cc:65-71](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTMetadataRequestExtensionMessage.cc#L65-L71) — request 的 payload：`msg_type=0` 加分片号，两行搞定。
- [src/UTMetadataRequestExtensionMessage.cc:79-107](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTMetadataRequestExtensionMessage.cc#L79-L107) — 收到 request 的三种应答：自己没有 metadata → 排队回 reject；分片合法（`index * 16K < metadataSize`）→ 从 `attrs->metadata` 切出该段回 data（末片切到结尾）；越界 → 抛异常断连。
- [src/UTMetadataDataExtensionMessage.cc:65-72](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTMetadataDataExtensionMessage.cc#L65-L72) — data 的 payload：**bencode 字典序列化结果与原始分片数据直接字符串相加**——这是 ut_metadata 唯一不走纯 bencode 的地方，也是接收侧要用「字典消费了多少字节」来切数据的原因。
- [src/UTMetadataDataExtensionMessage.cc:80-113](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTMetadataDataExtensionMessage.cc#L80-L113) — 收到 data 的核心动作：只有 `tracker_->tracks(index)`（确是我们发出的在途请求）才受理，防串台；把数据经 DiskAdaptor 写到 `index * METADATA_PIECE_SIZE` 偏移，`completePiece` 记账；全部收齐后把 metadata 从盘上读回、算 SHA-1 与 infohash 比对——一致打 `Got ut_metadata`，不一致把所有 piece 标回 missing 并抛异常（换 peer 重来）。
- [src/UTMetadataRejectExtensionMessage.cc:49-66](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTMetadataRejectExtensionMessage.cc#L49-L66) — reject 的 payload 是 `msg_type=2`；`doReceivedAction` 直接抛 `DL_ABORT_EX` 断开该连接——对方没有元数据，留着无益。

**工厂解析**（呼应 4.1.3 的 msg_type 分派）：

- [src/DefaultExtensionMessageFactory.cc:108-168](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultExtensionMessageFactory.cc#L108-L168) — `bencode2::decode` 的第三个参数 `end` 返回字典消费的字节数，data 分支用 `&data[1 + end]` 到 `&data[length]` 切出原始分片——与发送侧「字典 + 数据」的拼装方式严格互逆；`total_size` 缺失或为负、`piece` 缺失或为负均有独立报错。

**请求生成与在途记账**：

- [src/UTMetadataRequestFactory.cc:61-87](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTMetadataRequestFactory.cc#L61-L87) — `create(num)`：循环 `num` 次，`getMissingPiece(peer, 已跟踪分片列表, cuid)` 取缺失分片（复用 u5-l2 的位图机制，天然排除别的连接已在途的分片），构造 request（id 取自**对端** id 表）并入队，同时 `tracker_->add` 记账。
- [src/UTMetadataRequestTracker.cc:90-99](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTMetadataRequestTracker.cc#L90-L99) — `avail()`：`MAX_OUTSTANDING_REQUEST = 1`，每 peer 同时最多 1 个在途 metadata 请求。
- [src/UTMetadataRequestTracker.cc:67-88](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTMetadataRequestTracker.cc#L67-L88) — 超时 20 秒（`TIMEOUT = 20_s`）的在途分片被移出账本，索引返回给调用方去 `cancelPiece` 归还 PieceStorage。
- [src/BtConstants.h:56](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtConstants.h#L56) — `METADATA_PIECE_SIZE = 16_k`，分片大小常量。

**循环挂接与模式装配**：

- [src/DefaultBtInteractive.cc:531-563](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L531-L563) — `doInteractionProcessing()` 的 metadataGetMode 分支：先收干消息，再刷新 `pieceStorage_`（它可能刚被扩展握手里的 metadata_size 重新初始化，注释明确指向 4.2 的 `doReceivedAction`），然后依次：按 `avail()` 生成请求、每秒清理超时、收齐后 `setForceHaltRequested` 停任务（后续交给 u6-l9 的 UTMetadataPostDownloadHandler）。
- [src/PeerInteractionCommand.cc:253-278](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L253-L278) — metadataGetMode（判定条件是 `torrentAttrs->metadata.empty()`，见 [src/PeerInteractionCommand.cc:116-118](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PeerInteractionCommand.cc#L116-L118)）时额外装配 RequestFactory 与 Tracker，并调用 `enableMetadataGetMode()`（[src/DefaultBtInteractive.h:240](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.h#L240)）。

#### 4.4.4 代码实践

1. **实践目标**：观察一个 magnet 下载中 ut_metadata 分片的逐片到达与最终 SHA-1 校验。
2. **操作步骤**：
   - 在 [src/UTMetadataDataExtensionMessage.cc:82-99](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTMetadataDataExtensionMessage.cc#L82-L99) 的 `if (tracker_->tracks(...))` 体内加一行（示例代码）：
     ```cpp
     A2_LOG_INFO(fmt("ut_metadata data piece=%lu bytes=%lu",
                     static_cast<unsigned long>(getIndex()),
                     static_cast<unsigned long>(data_.size())));
     ```
   - 重新编译后用 magnet 下载（任一公网种子的 magnet 均可）：
     ```bash
     ./src/aria2c -l utmeta.log --log-level=info "magnet:?xt=urn:btih:<infohash>"
     ```
3. **需要观察的现象**：`ut_metadata data piece=... bytes=...` 逐片出现（末片字节数小于 16384）；最后一条相关日志是 `Got ut_metadata`，随后任务停止、真正的文件下载任务被派生出来（下一讲的主题）。
4. **预期结果**：分片数 ≈ ⌈metadata_size / 16384⌉（`metadata_size` 可在 4.2.4 的扩展握手日志中看到）；若中途换 peer，可能看到同一 piece 号重复到达但只有受跟踪的那次生效。
5. 需要真实网络环境与可用 magnet，**待本地验证**。无网络时的替代方案（源码阅读型）：打开 [test/UTMetadataDataExtensionMessageTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/UTMetadataDataExtensionMessageTest.cc) 与 [test/HandshakeExtensionMessageTest.cc:19-27](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/HandshakeExtensionMessageTest.cc#L19-L27)，阅读测试如何构造 payload 与断言 `doReceivedAction` 的写盘行为。

#### 4.4.5 小练习与答案

**练习 1**：收到 data 消息时为什么必须先检查 `tracker_->tracks(index)`？

**答案**：多个 peer 可能同时被请求同一分片（end-game 式抢片），或对端迟到回执；不跟踪就写盘会破坏 PieceStorage 的在途记账与完成判定。见 [src/UTMetadataDataExtensionMessage.cc:82-85](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTMetadataDataExtensionMessage.cc#L82-L85)，未跟踪的分片只打 debug 日志直接丢弃。

**练习 2**：aria2 为什么对 metadata 请求限制「每 peer 1 个在途」，而普通 piece 请求的流水线深度可以到 256（u6-l6）？

**答案**：见 [src/UTMetadataRequestTracker.cc:90-99](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTMetadataRequestTracker.cc#L90-L99)。元数据总量小（上限 8 MiB，见 4.2 的 `8_m` 检查）、分片少，瓶颈在对端是否真的持有完整元数据而非吞吐；每 peer 单在途 + 20 秒超时可以让失效 peer 快速暴露并换源，深流水线反而会把请求浪费在注定失败的 peer 上。

**练习 3**：假设某恶意 peer 回发了错误的 metadata 分片，aria2 如何自保？

**答案**：双层防线。分片层：只有被跟踪的在途分片会被写盘；终局层：收齐后对整个 metadata 计算 SHA-1 与 infohash 比对（[src/UTMetadataDataExtensionMessage.cc:90-106](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTMetadataDataExtensionMessage.cc#L90-L106)），不一致则把全部 piece 标回 missing 并抛异常断开该 peer，从其他 peer 重来。infohash 本身就是元数据的摘要，无法伪造通过校验。

## 5. 综合实践

**任务：绘制一份「单 peer 扩展协商与 PEX 贡献」观测报告。**

把 4.2.4、4.3.4、4.4.4 的三处日志补丁一起打上，然后完成下表（同一热门种子、各运行 5 分钟）：

| 观测项 | 获取方式 | --enable-peer-exchange=true | --enable-peer-exchange=false |
| --- | --- | --- | --- |
| 完成扩展握手的 peer 数 | negotiated 日志去重 | 填写 | 填写 |
| 对端 ut_pex id 的取值分布 | negotiated 行汇总 | 填写 | 填写 |
| 收到的 PEX 消息数 / 累计 added | ut_pex received 行求和 | 填写 | 应为 0 |
| （magnet 场景）metadata 分片到达序列 | ut_metadata data 行 | 填写 | 填写 |

随后回答三个分析题：

1. 关闭 PEX 后 negotiated 行里对端仍可能通告 ut_pex id（对端不知道我们关了），但为什么收不到任何 PEX 消息？（对照 [src/DefaultBtInteractive.cc:581-584](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L581-L584) 与 [src/DefaultBtInteractive.cc:148-155](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtInteractive.cc#L148-L155)：我们不通告 ut_pex，对端按 BEP10 就不会用它给我们发。）
2. magnet 场景下若某 peer 协商结果 `ut_metadata=0`，这条连接的命运如何？（对照 [src/HandshakeExtensionMessage.cc:110-116](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HandshakeExtensionMessage.cc#L110-L116)：被 "Goodbye" 断连——这就是扩展能力间接影响 peer 存活的实例。）
3. 两个方向的 id 表如果在实现里混用（发消息时误用本端 id），会发生什么？（对端工厂反查不到名字，抛 "No extension registered" 断连，见 [src/DefaultExtensionMessageFactory.cc:96-101](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultExtensionMessageFactory.cc#L96-L101)。）

产物：一份 Markdown 报告，包含两列对比数据、三条分析题答案，以及一张手工绘制的「握手 → 扩展握手 → PEX 周期 → （magnet）metadata 分片」时序图。所有结论注明对应日志行与源码链接。

## 6. 本讲小结

- 所有扩展消息共用 20 号 BtExtendedMessage 外壳：4 字节长度 + id=20 + 1 字节扩展 id + payload；扩展 id 0 保留给 extended handshake。
- 扩展 id 是**每对连接、每个方向独立协商**的：本端表（ExtensionMessageRegistry，aria2 固定 ut_pex=8、ut_metadata=9）用于通告与接收反查；对端表（PeerSessionResource）用于发送时取 id。两张表在 `m` 字典里完成交换。
- 「不通告即不支持」：关闭 PEX（或私有种子）会把 UT_PEX 从本端表移除，`m` 字典里没有它，对端就不会发——单开关同时管住发送与接收两个方向。
- ut_pex 用 6/18 字节紧凑格式分 v4/v6 双列表（`added`/`dropped` 及 flag 串），每分钟广播一次、fresh/dropped 各限 50 个，收到的 peer 直接进 PeerStorage 候选池，是 tracker 之外的第二大 peer 来源。
- ut_metadata 把 info 字典当 16KiB 分片的虚拟文件下载：request/data/reject 三种 msg_type、每 peer 1 个在途、20 秒超时、收齐后 SHA-1 对 infohash 终检——为下一讲 magnet 的两阶段任务打好了地基。
- 扩展能力不直接决定 peer 选择，但通过两条间接通道起作用：metadataGetMode 下无 ut_metadata 的 peer 被断连；PEX 的丰俭决定候选池大小。

## 7. 下一步学习建议

- **下一讲 u6-l9（magnet 链接：先下元数据再下数据）**：本讲的 ut_metadata 只是「取回元数据」这一半；收齐后如何经 UTMetadataPostDownloadHandler 解析出真正的 DownloadContext、并由 BtDependency 派生文件下载任务，是下一讲的主线。建议先读 [src/UTMetadataPostDownloadHandler.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/UTMetadataPostDownloadHandler.cc) 与 [src/BtDependency.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtDependency.cc)。
- **横向延伸**：对照阅读 [test/UTPexExtensionMessageTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/UTPexExtensionMessageTest.cc) 中对 `added`/`added6` 字节串的断言，可以最快速度内化紧凑格式。
- **协议原文**：BEP 9（ut_metadata）、BEP 10（扩展协议）与 BEP 11（PEX 家族，含 aria2 未实现的 lt_tex）值得一读，读完后回头再看 `ExtensionMessageRegistry` 的极小化设计会更有体会。
