# DHT 报文：bencode 编码的 RPC 协议

## 1. 本讲目标

上一讲（u7-l1）我们弄清了 DHT 的「通讯录」——Kademlia 路由表如何组织节点。这一讲下探到线上字节层，回答三个问题：

1. 一条 DHT 报文在网络上长什么样？——它是用 bencode 编码的一个字典，aria2 如何把它拼出来、又如何解析回去。
2. ping / find_node / get_peers / announce_peer 四类 RPC 各自携带哪些字段？收到 query 的一端如何处理并组织应答？
3. UDP 是无连接的，一个响应凭什么知道自己在回答哪个请求？——transaction id（事务 ID）的生成、登记与配对机制。

学完本讲，你应该能读懂任何一条 DHT 报文的原始字节流，并理解 `DHTMessageFactoryImpl` 从字典构造消息对象时的全部校验规则。

## 2. 前置知识

- **RPC（Remote Procedure Call，远程过程调用）**：一方向另一方发送「方法名 + 参数」的请求，对方回一个应答。DHT 的 RPC 跑在 UDP 上，方法名就是 `ping`、`find_node` 等字符串，参数装在 bencode 字典里。
- **transaction id（事务 ID）**：UDP 没有连接的概念，同一个节点可能同时发出几十个请求。请求方为每个请求生成一个随机短 ID，随报文一起发出；应答方原样抄回。请求方靠它把「应答」和「请求」配成一对。这相当于把 UDP 补成了「一问一答」的会话。
- **bencode**：BitTorrent 世界的序列化格式，只有四种类型——字符串（`长度:内容`）、整数（`i数字e`）、列表（`l...e`）、字典（`d 键 值 ... e`）。语法细节在 u6-l1 已详细讲过，本讲只回顾与 DHT 有关的部分。
- **ValueBase**：aria2 用 C++ 表达动态类型的树结构（`String` / `Integer` / `List` / `Dict`），bencode 解码的产物就是一棵 ValueBase 树（详见 u9-l2）。
- **紧凑地址格式（compact form）**：DHT 不用 `192.168.0.1:6881` 这样的文本地址，而是定长字节串——IPv4 为 4 字节 IP + 2 字节大端端口共 6 字节，IPv6 为 16 + 2 共 18 字节。**大端（big-endian）**指高位字节在前，是网络字节序的约定。
- **K 桶的 K**：路由表每个桶最多容纳 K=8 个节点（u7-l1），这决定了应答报文中节点列表的长度上限。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/bencode2.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bencode2.cc) | bencode 编解码门面：`encode`（ValueBase→字节流）与 `decode`（字节流→ValueBase） |
| [src/DHTMessage.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessage.h) / [src/DHTMessage.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessage.cc) | 消息基类：持有本地/远程节点、transaction id、版本串 |
| [src/DHTAbstractMessage.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTAbstractMessage.cc) | 组装 `t`/`y`/`v` 公共键并编码、经 UDP 连接发送 |
| [src/DHTQueryMessage.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTQueryMessage.cc) / [src/DHTResponseMessage.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTResponseMessage.cc) | query（`q`/`a` 键）与 response（`r` 键）两个分支骨架 |
| [src/DHTPingMessage.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPingMessage.cc)、[src/DHTFindNodeMessage.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTFindNodeMessage.cc)、[src/DHTGetPeersMessage.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTGetPeersMessage.cc)、[src/DHTAnnouncePeerMessage.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTAnnouncePeerMessage.cc) | 四类 query 消息：字段拼装 + 收到后的处理动作 |
| [src/DHTFindNodeReplyMessage.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTFindNodeReplyMessage.cc)、[src/DHTGetPeersReplyMessage.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTGetPeersReplyMessage.cc) | 两类带载荷的 response：nodes / values 紧凑打包 |
| [src/DHTMessageFactoryImpl.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageFactoryImpl.cc) | 从收到的字典构造消息对象，逐字段校验 |
| [src/DHTMessageReceiver.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageReceiver.cc) | 接收入口：解码、分流 query/response、驱动处理 |
| [src/DHTMessageTracker.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageTracker.cc) / [src/DHTMessageTrackerEntry.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageTrackerEntry.cc) | transaction id 登记簿：请求-响应配对、超时裁定 |
| [src/DHTMessageDispatcherImpl.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageDispatcherImpl.cc) | 发送队列：发出 query 的同时登记 tracker |
| [src/DHTTokenTracker.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTTokenTracker.cc) | announce_peer 的 token 生成与校验 |
| [src/bittorrent_helper.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc) | `getCompactLength` 等紧凑地址编解码（本讲只用长度函数） |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**bencode2 编解码** → **DHTAbstractMessage 报文骨架与四类消息字段** → **DHTMessageFactoryImpl 构造与校验** → **transaction id 的生命周期**。前两个模块讲「怎么发」，后两个讲「怎么收、怎么配对」。

### 4.1 bencode2：DHT 报文的编解码门面

#### 4.1.1 概念说明

DHT 协议规定所有报文都是一个 bencode 字典。aria2 把「ValueBase 树 ⇄ 字节流」的转换收敛在 `bencode2` 命名空间里，只有两个入口：

- `bencode2::encode(const ValueBase*)`：发送方向。把消息对象组装出的 `Dict` 序列化成字节流。
- `bencode2::decode(...)`：接收方向。把 UDP 收到的字节流解析成 ValueBase 树。

它是一层很薄的**门面（Facade）**：解码的实际工作交给 `ValueBaseBencodeParser`（一个增量状态机解析器，u9-l2 会详细拆解），编码则由一个内部访问者类完成。

#### 4.1.2 核心流程

编码方向按访问者模式遍历 ValueBase 树：

```
encode(Dict)
  └─ BencodeValueBaseVisitor 遍历
       ├─ String  → 输出 "长度:原始字节"
       ├─ Integer → 输出 "i数字e"
       ├─ List    → 输出 "l" 子元素... "e"
       └─ Dict    → 输出 "d" 键 值 ... "e"
```

两个对 DHT 报文影响深远的细节：

1. **键按字典序输出**。`Dict` 底层是 `std::map`，遍历天然有序。所以任何一条 DHT 报文的外层键都是 `a < q < r < t < v < y` 的字典序，与构造时的插入顺序无关——这保证了同一棵树的编码结果是确定的（这也是 u6-l1 讲过的 infohash 可重算的基础）。
2. **字符串是「长度前缀 + 原始字节」**，字节可以是任意二进制。node ID（20 字节）、transaction id（4 字节）、紧凑地址（6/18 字节）、版本串（含 `\0`）都直接塞在字符串里，不做任何转义。

解码方向则要求整段数据恰好构成一个完整的 bencode 值，任何语法错误都会抛出携带 `BENCODE_PARSE_ERROR` 错误码的异常。

#### 4.1.3 源码精读

先看解码入口（三个重载最终都落到第三个）：

- [src/bencode2.cc:L63-L76](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bencode2.cc#L63-L76)：`decode` 把数据交给 `ValueBaseBencodeParser::parseFinal` 一次解析完毕；返回负数表示语法错误，抛出 `BENCODE_PARSE_ERROR` 异常；`end` 参数带出实际消费的字节数。

  这段代码是 DHT 报文的第一道关卡：网络上收到的任何垃圾字节流，只要解不开 bencode，就在这里被拦下。

再看编码入口：

- [src/bencode2.cc:L78-L126](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bencode2.cc#L78-L126)：`encode` 定义局部类 `BencodeValueBaseVisitor`，对 ValueBase 树做一次遍历，结果写进 `ostringstream`。

  其中最关键的两个 visit：

- [src/bencode2.cc:L85-L90](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bencode2.cc#L85-L90)：字符串编码为 `长度:内容`。注意 `out_.write(s.data(), s.size())` 原样写出二进制字节——这就是 node ID、token 能直接放进报文的原因。
- [src/bencode2.cc:L109-L119](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bencode2.cc#L109-L119)：字典编码为 `d` + 若干「键(字符串) 值(任意)」+ `e`。遍历 `std::map` 的有序性就是外层键字典序的来源。

  顺带一提 [src/bencode2.cc:L97-L98](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bencode2.cc#L97-L98)：`Bool` 与 `Null` 的 visit 是空的——bencode 没有这两种类型，编码时静默丢弃。

一条 ping 请求编码后在网络上的形状（示例，`␀` 表示二进制零字节，实际是 4 字节随机 transaction id 和 20 字节二进制 ID）：

```text
d1:ad2:id20:<20字节本机ID>e1:q4:ping1:t4:<4字节tid>1:v4:A2␀␀1:y1:qe
```

可以逐字核对：`1:a` → `d2:id20:...e`；`1:q` → `4:ping`；`1:t` → `4:....`；`1:v` → `4:A2␀␀`；`1:y` → `1:q`。外层键顺序正是字典序 `a, q, t, v, y`。

#### 4.1.4 代码实践

源码阅读型实践：用测试用例验证你的编码推导。

1. **实践目标**：确认自己能手工推出一段 ValueBase 树的 bencode 编码。
2. **操作步骤**：
   1. 打开 [test/Bencode2Test.cc:L22](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/Bencode2Test.cc#L22) 的 `testEncode()`，挑一个含嵌套 Dict 的断言。
   2. 先不看期望值，自己在纸上写出你推导的编码结果。
   3. 与断言中的期望字符串逐字符比对。
   4. 再挑一个解码用例，反向练习。
3. **需要观察的现象**：字典键的输出顺序、整数与字符串的前缀格式是否与你的推导一致。
4. **预期结果**：对嵌套结构的编码能完全推对；若有出入，定位是对「长度前缀」还是「键排序」理解有误。
5. 待本地验证（用例断言值需打开文件确认）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 DHT 报文里可以直接放 20 字节二进制的 node ID，而不需要 Base64/Hex 之类的文本编码？

**答案**：bencode 字符串是「长度前缀 + 原始字节」，`visit(const String&)` 用 `out_.write` 原样写出任意字节（[src/bencode2.cc:L85-L90](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bencode2.cc#L85-L90)），长度由前缀给出，不依赖任何内容转义或终止符，二进制安全。

**练习 2**：一条报文外层字典的键实际输出顺序由什么决定？如果构造 `Dict` 时先 `put("y", ...)` 再 `put("a", ...)`，线上字节会怎样？

**答案**：由 `Dict` 底层 `std::map` 的键字典序决定（`a < q < r < t < v < y`），与插入顺序无关；先放 `y` 再放 `a` 编码结果不变（[src/bencode2.cc:L109-L119](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bencode2.cc#L109-L119)）。这种确定性让「重新编码同一棵树必然得到同一串字节」成立。

**练习 3**：`decode` 失败时抛出的异常携带什么错误码？

**答案**：`error_code::BENCODE_PARSE_ERROR`（[src/bencode2.cc:L70-L73](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bencode2.cc#L70-L73)）。

### 4.2 DHTAbstractMessage：报文骨架与四类消息字段

#### 4.2.1 概念说明

所有 DHT 消息共享同一个类层次：

```text
DHTMessage                     （transactionID、version、local/remote node）
  └─ DHTAbstractMessage        （connection/dispatcher/factory/routingTable 四依赖；编码与发送）
       ├─ DHTQueryMessage      （y="q"；fillMessage 放 q + a）
       │    ├─ DHTPingMessage / DHTFindNodeMessage / DHTGetPeersMessage / DHTAnnouncePeerMessage
       └─ DHTResponseMessage   （y="r"；fillMessage 放 r）
            ├─ DHTPingReplyMessage / DHTFindNodeReplyMessage / DHTGetPeersReplyMessage / DHTAnnouncePeerReplyMessage
```

报文外层的公共键在 [src/DHTMessage.cc:L41-L47](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessage.cc#L41-L47) 定义为四个静态常量：`t`（transaction id）、`y`（报文类型：`q`=query、`r`=response、`e`=error）、`v`（版本串）、`id`（发送方 node ID，嵌在 `a`/`r` 里）。这是典型的**模板方法模式**：`getBencodedMessage` 固定组装公共部分，子类只填自己的增量。

#### 4.2.2 核心流程

发送一条消息的流程：

```
消息对象（如 DHTGetPeersMessage）
  → getBencodedMessage()
      建 Dict：t=transactionID, y="q"/"r", v=版本串
      → fillMessage(&msgDict)          ← 子类钩子
           query:    put("q", 方法名); put("a", getArgument())
           response: put("r", getResponse())
      → bencode2::encode(&msgDict)
  → send()
      connection_->sendMessage(字节流, 远端 IP, 远端端口)   ← 一个 UDP 包
```

收到 query 的一端则在 `doReceivedAction()` 里组织应答：生成 token（get_peers）、查路由表取最近节点、查 peer 宣告存储取 peer，然后**复用同一个 transaction id** 创建 reply 消息并入队。

常量取值（[src/DHTConstants.h:L46-L50](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTConstants.h#L46-L50)）：`DHT_ID_LENGTH = 20`（node ID / infohash 字节数）、`DHT_TRANSACTION_ID_LENGTH = 4`、`DHT_VERSION = 3`（aria2 的 DHT 协议版本号）。

#### 4.2.3 源码精读

**（1）transaction id 与版本串的来历**

- [src/DHTMessage.cc:L49-L59](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessage.cc#L49-L59)：构造函数中若传入的 transactionID 为空串，就调用 `generateTransactionID()` 现场生成——本端主动发起的 query 走这条路；应答方构造 reply 时则显式传入要抄回的 ID。
- [src/DHTMessage.cc:L63-L68](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessage.cc#L63-L68)：`generateTransactionID` 生成 4 字节密码学随机数据。4 字节意味着同一远端同时只有 2³² 种 ID，足够区分在途请求。

**（2）骨架：公共键组装与发送**

- [src/DHTAbstractMessage.cc:L61-L69](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTAbstractMessage.cc#L61-L69)：`getBencodedMessage` 先放 `t`/`y`/`v` 三个公共键（`getType()` 在 query 子类返回 `"q"`、response 子类返回 `"r"`），再调纯虚函数 `fillMessage` 让子类补内容，最后交给 4.1 节的 `bencode2::encode`。
- [src/DHTAbstractMessage.cc:L71-L79](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTAbstractMessage.cc#L71-L79)：`send` 把编码结果经 `connection_`（对 `UDPConnection` 的抽象）发往 `remoteNode` 的 IP:port，返回是否完整发出。一条 DHT 报文就是一个 UDP 数据报。

**（3）两个分支的 fillMessage**

- [src/DHTQueryMessage.cc:L55-L61](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTQueryMessage.cc#L55-L61)：query 分支放 `q`（方法名字符串，如 `"get_peers"`）和 `a`（参数字典 `getArgument()` 的返回值）。常量 `Q("q")`、`A("a")` 定义在 [src/DHTQueryMessage.cc:L42-L44](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTQueryMessage.cc#L42-L44)。
- [src/DHTResponseMessage.cc:L54-L59](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTResponseMessage.cc#L54-L59)：response 分支只放 `r`（返回值字典 `getResponse()`）。注意：**应答报文里没有 `q` 键**——对方怎么知道这条应答属于哪个方法？答案在 4.4 节的 tracker。

**（4）四类 query 的 `a` 字典与处理动作**

| 消息 | `a` 字典内容 | 收到后的 `doReceivedAction` |
| --- | --- | --- |
| ping | `id` | 原样抄回 transaction id，回 ping reply |
| find_node | `id`, `target`(20字节) | 查路由表最近 K 节点，回带 `nodes` 的 reply |
| get_peers | `id`, `info_hash`(20字节) | 生成 token + 查本机已知 peer + 最近节点，回 reply |
| announce_peer | `id`, `info_hash`, `port`(整数), `token` | 校验 token 后登记宣告，回仅含 `id` 的 reply |

对应源码：

- [src/DHTPingMessage.cc:L54-L67](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPingMessage.cc#L54-L67)：ping 的应答直接复用 `getTransactionID()` 创建 reply——这就是「抄回」动作；`getArgument` 只放本机 `id`。字段常量见各消息文件开头（如 [src/DHTAnnouncePeerMessage.cc:L55-L61](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTAnnouncePeerMessage.cc#L55-L61) 的 `info_hash`/`port`/`token`）。
- [src/DHTFindNodeMessage.cc:L62-L77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTFindNodeMessage.cc#L62-L77)：`doReceivedAction` 用 `targetNodeID_` 在路由表里查最近 K 个节点（u7-l1 的 `getClosestKNodes`），把结果交给工厂的 `createFindNodeReplyMessage`；`getArgument` 放 `id` + `target`。
- [src/DHTGetPeersMessage.cc:L112-L136](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTGetPeersMessage.cc#L112-L136)：get_peers 是最丰富的一支——先用 `tokenTracker_->generateToken` 为对方生成 token（announce_peer 的入场券），再从 `peerAnnounceStorage_` 捞「本机已知的、拥有该 infohash 的 peer」，`addLocalPeer` 还会把本机也加进去（若本机正在下载/做种同 infohash 且配置了外部 IP），最后附上最近节点。请求侧 `getArgument` 只放 `id` + `info_hash`。
- [src/DHTAnnouncePeerMessage.cc:L87-L111](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTAnnouncePeerMessage.cc#L87-L111)：`getArgument` 放 `id`、`info_hash`、整数 `port`（**TCP 端口**，注意与 UDP 通讯端口区分）和 `token` 字符串；`validate()` 用 `tokenTracker_->validateToken` 校验 token，不合法直接抛异常拒绝。

token 的底细在 [src/DHTTokenTracker.cc:L63-L101](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTTokenTracker.cc#L63-L101)：token = SHA-1(`infohash ‖ 紧凑地址(对方IP,对方端口) ‖ 秘密`)，保存当前与上一代两个秘密（[src/DHTTokenTracker.cc:L90-L101](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTTokenTracker.cc#L90-L101)），`updateTokenSecret` 滚动更换。因为 token 绑定了请求方的 IP:port，别人截获也无法转用——这防止了伪造的第三方宣告（呼应 u7-l5）。

**（5）response 的 `r` 字典：紧凑格式打包**

- [src/DHTFindNodeReplyMessage.cc:L73-L97](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTFindNodeReplyMessage.cc#L73-L97)：find_node 应答把每个节点编码为 `20字节ID + 紧凑地址`，IPv4 每节点 26 字节、IPv6 每节点 38 字节，最多 K=8 个，整段塞进一个 bencode 字符串放 `r.nodes`（IPv6 时键名为 `nodes6`）。
- [src/DHTGetPeersReplyMessage.cc:L78-L145](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTGetPeersReplyMessage.cc#L78-L145)：get_peers 应答的 `r` 字典含 `id`、`token`（对方下次 announce_peer 要带的）、可选 `nodes`/`nodes6`（与 find_node 同格式）和可选 `values`——一个列表，每项是一个 6 字节（IPv4）/18 字节（IPv6）紧凑 peer 地址。键常量 `token`/`values`/`nodes`/`nodes6` 定义在 [src/DHTGetPeersReplyMessage.cc:L55-L61](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTGetPeersReplyMessage.cc#L55-L61)。
- [src/DHTGetPeersReplyMessage.cc:L106-L128](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTGetPeersReplyMessage.cc#L106-L128)：这段注释非常值得精读——作者逐项预算 UDP 报文体积（87 字节模板 + 8×38 节点 + IP/UDP 头），为避免 IP 分片（Path MTU），把 `values` 上限 `MAX_VALUES_SIZE` 定为 25。

**（6）版本串 `v`**

- [src/DHTMessageFactoryImpl.cc:L298-L311](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageFactoryImpl.cc#L298-L311)：`getDefaultVersion` 惰性构造 4 字节版本串 `'A','2'` + `htons(DHT_VERSION)`，即 `41 32 00 03`——"A2" 后跟大端的版本号 3。对端可据此做兼容判断。

#### 4.2.4 代码实践

日志观察型实践：从运行中的 aria2 里「听」到这些消息。

1. **实践目标**：在日志中辨认四类 query 与对应 reply，并把日志字段与本节讲的报文字段对上。
2. **操作步骤**：
   1. 运行一个 BT 下载并开启 info 级日志：`./src/aria2c --enable-dht -l dht.log --log-level=info <某个.torrent>`。
   2. 在日志中检索 `Message sent: dht query` 与 `Message received: dht response`（这两行分别出自 [src/DHTMessageDispatcherImpl.cc:L79](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageDispatcherImpl.cc#L79) 与 [src/DHTMessageReceiver.cc:L123](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageReceiver.cc#L123)，格式来自 [src/DHTQueryMessage.cc:L65-L74](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTQueryMessage.cc#L65-L74) 的 `toString()`）。
   3. 挑一条 `get_peers` 的发送记录，抄下它的 `TransactionID=`（8 个十六进制字符，即 4 字节）与 `Remote:` 地址。
   4. 再在 `Message received` 里找 `get_peers` 且 TransactionID 相同的行，注意末尾 `values=N, nodes=M`（出自 [src/DHTGetPeersReplyMessage.cc:L157-L162](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTGetPeersReplyMessage.cc#L157-L162)）。
3. **需要观察的现象**：`ping` 的 query 行没有 `toStringOptional` 附加字段；`get_peers` 的 query 行带 `info_hash=`；`announce_peer` 的 query 行带 `token=`、`tcpPort=`；response 与 query 的 TransactionID 一致。
4. **预期结果**：能按附加字段把日志行分类到四类消息，并确认「request 与 reply 抄回同一 TransactionID」。
5. 待本地验证（需要网络可达 DHT 网络）。

#### 4.2.5 小练习与答案

**练习 1**：announce_peer 报文里的 `port` 字段是 DHT 通讯用的 UDP 端口吗？

**答案**：不是。它是声明「我有这个 infohash 的数据、可以 BT 下载」的 **TCP** 端口。看 [src/DHTAnnouncePeerMessage.cc:L92](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTAnnouncePeerMessage.cc#L92) 成员名就是 `tcpPort_`，登记进 `peerAnnounceStorage` 后供对端发起 BT peer 连接（u6-l3）使用。

**练习 2**：为什么 get_peers 应答要把 peer 列表叫 `values`，而不是和节点列表一样叫 `nodes`？

**答案**：两者语义不同——`nodes` 是「更接近目标的 DHT 节点」（用于继续迭代查找，20 字节 ID + 紧凑地址）；`values` 是「宣称拥有该 infohash 数据的 BT peer」（仅紧凑地址，无 node ID，直接可用于 BT 连接）。DHT 规范（BEP 5）就用这两个键名区分「路由信息」与「数据持有者」，aria2 在 [src/DHTGetPeersReplyMessage.cc:L55-L61](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTGetPeersReplyMessage.cc#L55-L61) 照实定义。

**练习 3**：token 为什么要在 get_peers 应答里发，而不是让对端随便构造？

**答案**：token 由应答方计算：SHA-1(infohash ‖ 紧凑地址(请求方 IP:port) ‖ 本方秘密)（[src/DHTTokenTracker.cc:L63-L81](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTTokenTracker.cc#L63-L81)）。它绑定了请求方地址与 infohash，只有真正拿到过 token 的节点才能对该 infohash 向本方 announce，伪造或重放他人的 token 都过不了 `validateToken` 的双秘密比对。

### 4.3 DHTMessageFactoryImpl：从字典构造消息对象与校验

#### 4.3.1 概念说明

接收方向的问题：UDP 收到一包字节，解码成 `Dict` 之后，如何变成一个可执行的消息对象？`DHTMessageFactoryImpl` 就是「字典 → 消息对象」的唯一映射点，同时承担**结构校验**：任何一个必需键缺失、类型不对、长度不对，都抛出 `DL_ABORT_EX`（"Malformed DHT message..."），异常最终被 `DHTMessageReceiver` 捕获并把该报文归为 `DHTUnknownMessage` 丢弃——恶意或损坏的报文无法越过这一层。

工厂还有一个隐藏职责：**响应报文的方法名不是从报文里读的**。`createResponseMessage` 的第一个参数 `messageType` 来自调用方（tracker 里记住的原 query 类型），因为响应字典里根本没有 `q` 键。

#### 4.3.2 核心流程

```
接收字节流 → bencode2::decode → Dict
                                  │
              ┌─ y == "q" ────────┤
              │                   └─ y == "r" 或 "e" → DHTMessageTracker::messageArrived
              │                                                │ 命中 entry（含原 query 方法名）
              ▼                                                ▼
   createQueryMessage(dict, ip, port)              createResponseMessage(entry方法名, dict, ip, port)
     1. 取 q / t / y / a 四键，y 必须为 "q"            1. y=="e" → 记日志并抛异常（错误报文）
     2. 按 q 值分派 ping/find_node/                    2. y 必须为 "r"；取 r.id 建 remoteNode
        get_peers/announce_peer                        3. 按方法名分派四种 reply 构造
     3. 各自提取并校验字段（validateID/validatePort）    4. get_peers reply：解析 nodes/nodes6/values/token
     4. setVersion + setCommonProperty                 4'. setVersion + setCommonProperty
```

#### 4.3.3 源码精读

**（1）防弹的取值辅助函数**

- [src/DHTMessageFactoryImpl.cc:L90-L168](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageFactoryImpl.cc#L90-L168)：五个匿名命名空间的辅助函数 `getDictionary`/`getString`/`getInteger`/`getList`（字典版与列表版），统一做「按键取值 + `downcast` 类型检查」，任一不满足即抛 `"Malformed DHT message. Missing xxx"`。所有字段提取都经由它们，校验风格完全一致。
- [src/DHTMessageFactoryImpl.cc:L170-L186](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageFactoryImpl.cc#L170-L186)：`validateID` 要求恰好 20 字节；`validatePort` 要求 `0 < port < 65535`。

**（2）query 方向的分派**

- [src/DHTMessageFactoryImpl.cc:L201-L247](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageFactoryImpl.cc#L201-L247)：`createQueryMessage` 先取 `q`（方法名）、`t`（transaction id）、`y`、`a`（参数字典）四键，并检查 `y` 必须等于 `"q"`；随后 if-else 链按方法名分派：
  - `find_node` → 取 `target`，`validateID` 后构造（[src/DHTMessageFactoryImpl.cc:L218-L224](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageFactoryImpl.cc#L218-L224)）；
  - `get_peers` → 取 `info_hash` 并校验 20 字节（[src/DHTMessageFactoryImpl.cc:L225-L229](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageFactoryImpl.cc#L225-L229)）；
  - `announce_peer` → 取 `info_hash` + 整数 `port`（`validatePort`）+ 字符串 `token`（[src/DHTMessageFactoryImpl.cc:L230-L240](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageFactoryImpl.cc#L230-L240)）；
  - 其他方法名 → 抛 `"Unsupported message type"`（[src/DHTMessageFactoryImpl.cc:L241-L244](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageFactoryImpl.cc#L241-L244)）——aria2 只实现 BEP 5 的四种 RPC。
  
  注意所有分支都先 `getRemoteNode`：**remoteNode 的 IP 与端口来自 UDP 包的源地址**（函数入参 `ipaddr`/`port`），不信任报文内容里的自述地址。

- [src/DHTMessageFactoryImpl.cc:L78-L88](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageFactoryImpl.cc#L78-L88)：`getRemoteNode` 先按「ID + IP + 端口」向路由表查询，命中就复用已有的 `DHTNode` 对象（保留其健康度计数），查不到才新建——消息对象与路由表共享同一批节点对象。

**（3）response 方向的分派与错误报文**

- [src/DHTMessageFactoryImpl.cc:L249-L296](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageFactoryImpl.cc#L249-L296)：`createResponseMessage` 的 `messageType` 是外部传入的（见 4.4 节）。第一个分支处理 `y == "e"` 的错误报文：`e` 应为 `[错误码, 错误消息]` 两元素列表，aria2 记一条 INFO 日志（消息经 percentEncode 防止二进制污染日志）后抛异常放弃该报文（[src/DHTMessageFactoryImpl.cc:L257-L269](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageFactoryImpl.cc#L257-L269)）。随后要求 `y == "r"`，从 `r.id` 建立 remoteNode，再按方法名构造对应 reply。

**（4）nodes / values 的紧凑解码**

- [src/DHTMessageFactoryImpl.cc:L366-L385](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageFactoryImpl.cc#L366-L385)：`extractNodes` 是 4.2 节 `getResponse` 打包的逆过程——按 `紧凑长度 + 20` 为单元步进（IPv4 26、IPv6 38），总长度不是单元整数倍即抛异常；每个单元前 20 字节做 node ID，`unpackcompact` 解析剩余地址，解析失败（如非法 IP）就跳过该节点而不是整体失败。
- [src/DHTMessageFactoryImpl.cc:L419-L452](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageFactoryImpl.cc#L419-L452)：`createGetPeersReplyMessage`（字典版）按本工厂的 `family_` 决定读 `nodes` 还是 `nodes6`（IPv4 与 IPv6 两套 DHT 各有一个工厂实例，呼应 u7-l1 的 DHTRegistry）；`values` 列表逐项做 `unpackcompact` 还原成 `Peer`；最后 `token` 是**必需键**（没有 token 的 get_peers 应答无法用于后续 announce）。
- [src/bittorrent_helper.cc:L1016-L1027](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L1016-L1027)：`getCompactLength` 返回 IPv4=6（`COMPACT_LEN_IPV4`）、IPv6=18（`COMPACT_LEN_IPV6`）。

**（5）依赖注入**

- [src/DHTMessageFactoryImpl.cc:L313-L320](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageFactoryImpl.cc#L313-L320)：`setCommonProperty` 给每条新建消息注入 connection、dispatcher、routingTable、factory（指向自己）和默认版本串——消息发送与组织应答所需的全部环境，风格与 u6-l5 的 `setCommonProperty` 一致。各 `createXxx` 的差异部分（如 get_peers 消息额外注入 `peerAnnounceStorage_`、`tokenTracker_`、`btRegistry_`、`family_`，[src/DHTMessageFactoryImpl.cc:L404-L417](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageFactoryImpl.cc#L404-L417)）在各自工厂方法里完成。

#### 4.3.4 代码实践

源码阅读型实践：用测试用例当「报文构造器」。

1. **实践目标**：亲手拼一条 get_peers 应答字典，验证工厂能把它解析回节点与 peer 对象，体会「打包 ↔ 解析」的对称性。
2. **操作步骤**：
   1. 打开 [test/DHTMessageFactoryImplTest.cc:L270-L300](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DHTMessageFactoryImplTest.cc#L270-L300) 的 `testCreateGetPeersReplyMessage`。
   2. 注意它如何构造报文：外层 `t`/`y`，内层 `r` 字典放 `id`；`nodes` 用循环拼接「20 字节 ID + `packcompact` 出的 6 字节地址」共 K 个；`values` 每项 6 字节紧凑 peer。
   3. 对照本节 `extractNodes` 与 `createGetPeersReplyMessage` 的解析逻辑，逐字段确认测试数据能被正确还原。
   4. 思考变体：若把 `nodes` 长度故意改成 13（26 的非整数倍），`extractNodes` 会怎样？在测试里改一改跑 `make check` 验证（改测试文件属于练习行为，注意不要提交）。
3. **需要观察的现象**：解析出的 8 个节点 IP 是否等于 `192.168.0.1` ~ `192.168.0.8`、端口是否等于 `6881` 起递增。
4. **预期结果**：解析结果与构造数据一一对应；13 字节的 `nodes` 触发 `"Nodes length is not multiple of 26"` 异常路径。
5. 待本地验证（需要完成编译并运行测试）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `createResponseMessage` 需要外部传入 `messageType`，而 `createQueryMessage` 不需要？

**答案**：query 报文里有 `q` 键标明方法名，工厂可自行分派；而 response 报文（`y="r"`）只有 `t`/`r` 等键，**不含方法名**，方法名只能由 tracker 在登记请求时记下、配对成功后传给工厂（见 [src/DHTMessageFactoryImpl.cc:L249-L253](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageFactoryImpl.cc#L249-L253) 的签名与 4.4 节 tracker 的调用）。

**练习 2**：一条 get_peers 应答缺少 `token` 键会发生什么？

**答案**：`getString(rDict, TOKEN)` 抛出 `DL_ABORT_EX("Malformed DHT message. Missing token")`（[src/DHTMessageFactoryImpl.cc:L448](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageFactoryImpl.cc#L448) 调用 [src/DHTMessageFactoryImpl.cc:L104-L114](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageFactoryImpl.cc#L104-L114)），该异常被 `DHTMessageTracker::messageArrived` 捕获后按超时条目处理并向上抛，最终报文作废。

**练习 3**：`extractNodes` 遇到某节点的 IP 字节非法（`unpackcompact` 失败）时如何处理？为什么这样设计？

**答案**：跳过该单元继续解析下一个（[src/DHTMessageFactoryImpl.cc:L376-L380](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageFactoryImpl.cc#L376-L380) 的 `continue`），不整体失败。因为单个远端节点的地址损坏不应让整条应答（可能含 7 个好节点）作废；而整体长度不符单元则说明报文结构损坏，才抛异常。

### 4.4 transaction id 的生命周期：请求与响应如何配对

#### 4.4.1 概念说明

transaction id 是把 UDP 补成 RPC 的关键机制。它的生命周期是一条完整闭环：

```
① 生成：主动发 query 时随机生成 4 字节（DHTMessage 构造函数）
② 登记：query 实际发出后，(tid, 目标IP, 目标port, 方法名, 回调, 超时) 记入 DHTMessageTracker
③ 配对：收到 y=r/e 的报文，按 (tid, 源IP, 源port) 在登记簿里查找条目
   ├─ 命中 → 用条目里记的方法名构造 response 对象 → 触发回调 → 测 RTT
   └─ 未命中 → 迟到的应答或恶意报文 → 归为 UnknownMessage 丢弃
④ 超时：到点没有应答 → 节点 timeout() 计数 +1（累计判坏，呼应 u7-l1）
```

注意只有 query 才登记 tracker；reply 不登记（它本身不需要被配对）。

#### 4.4.2 核心流程

接收入口在 `DHTInteractionCommand`：UDP 包首字节为 `'d'`（bencode 字典）即判为 DHT 报文交给 `DHTMessageReceiver`（与 UDP tracker 响应分流，详见 u6-l7），随后：

```
DHTMessageReceiver::receiveMessage(addr, port, data, len)
  → bencode2::decode → 必须是 Dict 且含 y 键，否则归为 Unknown
  → y == "r" 或 "e"（应答方向）
       tracker_->messageArrived(dict, addr, port)
         逐条目比对 (tid, ip, port) → 命中:
           factory_->createResponseMessage(条目记的方法名, dict, ...)
           RTT = now - 条目发送时刻 → remoteNode->updateRTT
           若响应中 node ID 与条目记录不同 → dropNode 旧 ID
           返回 (response, 回调) → onMessageReceived + 回调->onReceived
  → 否则（query 方向）
       factory_->createQueryMessage(dict, addr, port)
       若 remoteNode == localNode（自发报文回环）→ 丢弃
       → onMessageReceived: toString 日志 → validate() → doReceivedAction()
         → markGood + addGoodNode（喂回路由表健康度）
```

#### 4.4.3 源码精读

**（1）登记：发送成功才记账**

- [src/DHTMessageDispatcherImpl.cc:L71-L99](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageDispatcherImpl.cc#L71-L99)：`sendMessage` 中，只有 `send()` 成功**且** `!isReply()`（即 query）才调 `tracker_->addMessage`，把远端节点、transaction id、方法名、超时与回调一并登记；发送抛异常时也以 `timeout=0` 登记（[src/DHTMessageDispatcherImpl.cc:L93-L96](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageDispatcherImpl.cc#L93-L96)），让依赖「条目必然终结」的任务（如节点查找）能正常收尾——一个防泄漏的细节。

**（2）配对：三元组匹配 + IPv4-mapped 兼容**

- [src/DHTMessageTracker.cc:L59-L66](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageTracker.cc#L59-L66)：`addMessage` 把消息的关键信息打包成 `DHTMessageTrackerEntry` 入列。
- [src/DHTMessageTracker.cc:L68-L117](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageTracker.cc#L68-L117)：`messageArrived` 从字典取 `t`，线性扫描条目逐个 `match`；命中即摘除条目、用**条目记住的方法名**（`entry->getMessageType()`）调用工厂构造 response 对象——这就是 4.3 节练习 1 的答案落点。随后两个高价值动作：
  - [src/DHTMessageTracker.cc:L91-L95](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageTracker.cc#L91-L95)：用条目存活时间算出往返时延（RTT）写回节点——u7-l1 提过的节点 RTT 统计就来自这里；
  - [src/DHTMessageTracker.cc:L96-L105](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageTracker.cc#L96-L105)：若响应里的 node ID 与登记时不同，说明对方换 ID 了，把旧节点从路由表剔除。
  未命中任何条目则返回空指针——迟到应答或伪造报文就此终结（由上层归为 Unknown）。
- [src/DHTMessageTrackerEntry.cc:L65-L77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageTrackerEntry.cc#L65-L77)：`match` 的判等规则：transaction id 与端口必须完全相等；IP 除直接相等外还接受 `::ffff:<IPv4>` 的映射形式（IPv6 套接字上收到 IPv4 来源时的表示差异）。**为什么必须三元组匹配？** 4 字节 tid 空间有限，不同节点可能撞车；加上 IP 与端口才能确定唯一在途请求。

**（3）接收入口与驱动处理**

- [src/DHTMessageReceiver.cc:L64-L119](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageReceiver.cc#L64-L119)：`receiveMessage` 是收包总入口：解码后先看 `y`——是 `r`/`e` 走 tracker 配对，是 `q` 走工厂构造 query；解码失败、缺 `y`、配对失败、构造抛异常，全部落入 `handleUnknownMessage`（记一条日志即丢弃，[src/DHTMessageReceiver.cc:L133-L140](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageReceiver.cc#L133-L140)）。query 分支还有一道防御：[src/DHTMessageReceiver.cc:L106-L110](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageReceiver.cc#L106-L110) 检测到报文发送者就是本机（多网卡/回环场景）时直接丢弃。
- [src/DHTMessageReceiver.cc:L121-L129](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageReceiver.cc#L121-L129)：`onMessageReceived` 是对**任何成功构造的消息**（query 与配对成功的 response）的统一处理：`toString()` 打日志 → `validate()`（如 announce_peer 的 token 校验）→ `doReceivedAction()`（4.2 节的应答组织）→ `markGood` + `updateLastContact` + `addGoodNode` 把对端喂回路由表。一次收包同时完成了「RPC 应答」与「路由表维护」两件事——这正是 DHT「通讯即保活」的设计。

**（4）超时：无应答的收尾**

- [src/DHTMessageTracker.cc:L119-L157](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageTracker.cc#L119-L157)：`handleTimeout`/`handleTimeoutEntry` 扫描超时条目：节点 `timeout()` 计数（u7-l1：累计 5 次判坏），判坏即 `dropNode`；若有回调则触发 `onTimeout` 通知任务层（u7-l3 的任务靠它感知失败并换目标）。

#### 4.4.4 代码实践

日志配对型实践。

1. **实践目标**：用日志亲手验证「一个 query 恰好配对一个 response」，并观察一次配对失败的例子。
2. **操作步骤**：
   1. 同 4.2.4 开启 info 日志做 BT 下载。
   2. 取一条 `Message sent: dht query ping TransactionID=abcd1234 Remote:1.2.3.4(6881)` 日志。
   3. 在其后（通常几毫秒内）寻找 `Message received: dht response ping TransactionID=abcd1234 Remote:1.2.3.4(6881)`——四项全同即配对成功。
   4. 统计：对所有 query 计算配对成功率；对没有 response 的 TransactionID，观察后续是否出现该节点的坏标记日志（`Marked bad`，[src/DHTMessageTracker.cc:L128-L132](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageTracker.cc#L128-L132)）。
   5. 进阶：把日志级别调到 debug，还能看到 `Searching tracker entry for TransactionID=...` 与 `Tracker entry found./not found.`（[src/DHTMessageTracker.cc:L78-L114](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageTracker.cc#L78-L114)）逐包呈现配对过程。
3. **需要观察的现象**：绝大多数 query 在数十毫秒内配对；未配对的 query 在超时后（`DHT_MESSAGE_TIMEOUT`）引发节点超时计数。
4. **预期结果**：能从纯日志层面还原 ②③④ 三个阶段。
5. 待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 reply 消息不需要登记 tracker？

**答案**：tracker 的目的是「等一个应答」。reply 本身就是应答，它发出后没有后续报文需要配对；`sendMessage` 里 `!entry->message->isReply()` 的判断（[src/DHTMessageDispatcherImpl.cc:L75-L78](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageDispatcherImpl.cc#L75-L78)）保证了只有 query 入册。

**练习 2**：假设节点 A 同时向节点 B 发出 100 个 query，B 的应答乱序到达。配对会出错吗？

**答案**：不会。每个 query 有独立随机 4 字节 tid，B 原样抄回；`messageArrived` 按 (tid, B 的 IP, B 的端口) 三元组精确匹配（[src/DHTMessageTrackerEntry.cc:L65-L77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageTrackerEntry.cc#L65-L77)），与到达顺序无关。只有当两个在途 query 的 tid 碰撞（概率约在途数²/2³²）才会错配，实践中可忽略。

**练习 3**：如果收到一条 `y="r"` 的报文，但在 tracker 里找不到对应条目，可能发生了什么？aria2 如何处理？

**答案**：最常见是**迟到**——应答在超时清理之后才到；也可能是对端重发或伪造报文。aria2 不报错，`messageArrived` 返回空对（[src/DHTMessageTracker.cc:L114-L117](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageTracker.cc#L114-L117)），`receiveMessage` 把它交给 `handleUnknownMessage` 记一条日志后丢弃（[src/DHTMessageReceiver.cc:L92-L97](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageReceiver.cc#L92-L97)）。

## 5. 综合实践

把四个模块串起来：**完整观察一次 get_peers 往返，并手工解码响应中的 `nodes` 字段**。

1. **实践目标**：从原始报文层面读懂一条真实的 get_peers 请求与应答，用手工解码证明你掌握了 bencode、紧凑格式与 transaction id 配对。

2. **操作步骤**：

   1. **准备**：编译 aria2，运行 `./src/aria2c --enable-dht --dht-listen-port=6881 -l dht.log --log-level=info <某个.torrent>`（固定监听端口便于抓包）。
   2. **拿到原始报文**，二选一：
      - *改源码加日志*（练完还原）：在 [src/DHTMessageReceiver.cc:L64-L70](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageReceiver.cc#L64-L70) 的 `receiveMessage` 开头加一行 `A2_LOG_INFO(fmt("RAW: %s", util::percentEncode(std::string(data, data+length)).c_str()));`，重新编译。percentEncode 会把二进制字节转义成可读形式。
      - *抓包*：`tcpdump -i any -w dht.pcap 'udp port 6881'`，用 Wireshark 打开（其 BitTorrent DHT 解析器能直接显示字典）。
   3. **在日志中锁定一对 query/response**：按 4.4.4 的方法找到同 TransactionID 的 `dht query get_peers` 与 `dht response get_peers`，记下该 tid。
   4. **手工解码请求**：对照 RAW 日志或抓包内容，写出请求字典 `d1:ad2:id20:<hex>20:info_hash20:<hex>e1:q9:get_peers1:t4:<你的tid>...`，并确认 `info_hash` 与你种子的 infohash（控制台或 `--show-files` 可得）一致。
   5. **手工解码响应的 `nodes`**：从响应字典中取出 `r.nodes`（bencode 字符串，长度应为 26 的倍数）。每个 26 字节单元：前 20 字节为节点 ID（转 hex），随后 4 字节按点分十进制读出 IP，最后 2 字节按大端读出端口。可用下面脚本辅助（**示例代码**）：

      ```python
      # 示例代码：解码 DHT get_peers/find_node 响应中的 nodes 字段（IPv4）
      import binascii, struct

      def decode_nodes(nodes: bytes):
          assert len(nodes) % 26 == 0, "长度必须是 26 的倍数"
          out = []
          for off in range(0, len(nodes), 26):
              nid = binascii.hexlify(nodes[off:off+20]).decode()
              ip = ".".join(str(b) for b in nodes[off+20:off+24])
              port = struct.unpack(">H", nodes[off+24:off+26])[0]  # 大端
              out.append((nid, ip, port))
          return out
      ```

   6. **交叉验证**：把解码出的 IP:port 列表与 dht.log 对比——aria2 随后会向其中部分节点发起新的 `get_peers`（可在日志中看到 `dht query get_peers ... Remote:<解码出的IP>(<解码出的端口>)`）。两边能对上，说明解码正确、你已读懂整条链路。

3. **需要观察的现象**：请求与响应外层键呈字典序；响应中 `token` 为可转义的二进制串；`nodes` 解码出的节点数 ≤ 8（K 桶上限）；若对方正好有该 infohash 的 peer，还会出现 `values` 列表（每项 6 字节）。

4. **预期结果**：产出一页笔记——一对报文的逐字段注释 + 一份解码节点表 + 日志交叉验证结论。

5. 待本地验证（依赖真实 DHT 网络连通性）。

## 6. 本讲小结

- DHT 报文就是一个 bencode 字典；`bencode2::encode/decode` 是唯一的编解码门面，字符串「长度前缀 + 原始字节」的二进制安全性与 `Dict` 键的字典序输出共同保证了编码的确定性。
- 报文骨架是模板方法：`DHTAbstractMessage::getBencodedMessage` 固定组装 `t`/`y`/`v`，query 子类补 `q`+`a`，response 子类补 `r`；四类 query（ping/find_node/get_peers/announce_peer）的差异全部体现在 `a` 字典与 `doReceivedAction`。
- 节点列表用紧凑格式（20 字节 ID + 6/18 字节地址）装进 `nodes`，peer 列表装进 `values`（上限 25，为避免 UDP 分片）；token 绑定「infohash + 请求方地址 + 秘密」防伪造宣告。
- `DHTMessageFactoryImpl` 是「字典 → 消息」的唯一映射点，逐字段做存在性、类型与长度校验，任何不合法都使报文被归为 Unknown 丢弃；响应的方法名来自 tracker 条目而非报文本身。
- transaction id 4 字节随机生成、只在 query 发出后登记、按 (tid, IP, port) 三元组配对；配对成功顺带测 RTT、维护路由表健康度，配对失败或超时则驱动节点超时计数——RPC 会话与路由表保活在此合二为一。

## 7. 下一步学习建议

本讲解决了「单条报文怎么读怎么写」；下一讲 **u7-l3（DHT 任务系统：队列、执行器与消息追踪）** 将看到谁在驱动这些报文：`DHTTaskQueue`/`DHTTaskExecutor` 如何调度，`DHTNodeLookupTask` 如何用本讲的 find_node 递归逼近目标，以及任务如何消费 tracker 的 `onReceived`/`onTimeout` 回调。建议先精读 [src/DHTMessageTracker.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageTracker.cc) 中回调的存取点，再带着「回调里能拿到什么」的问题进入下一讲。若想巩固 bencode 解析器的内部状态机，可提前翻阅 u9-l2。
