# DHT 基础：Kademlia 路由表与分桶

## 1. 本讲目标

本讲是 DHT 单元（单元 7）的第一篇。学完后你应该能够：

1. 说出一个 DHT 节点在 aria2 中由哪些字段表示（`DHTNode`），以及"好/可疑/坏"三种健康度如何判定与流转。
2. 解释 160 位节点 ID、XOR 距离与"共享前缀"三者的关系，说明 `DHTBucket` 的 `[min, max]` 区间如何实现 Kademlia 的 K 桶划分。
3. 跟踪一次 bucket 分裂（`split`）的位翻转过程，以及坏节点被替换/驱逐的全部触发条件。
4. 读懂 `DHTBucketTree` 上的 K 近邻查找算法（先向下、后向上），并理解它为什么不排序。
5. 理解 `DHTRegistry` 为什么保存 IPv4/IPv6 两套完全独立的路由表，以及 `dht.dat` 文件的二进制格式。

## 2. 前置知识

### 2.1 DHT 是什么：没有 tracker 的 peer 发现

在第 6 单元中你已经知道，BT 客户端靠 tracker（u6-l7）告诉它"还有谁在下载同一个种子"。DHT（Distributed Hash Table，分布式哈希表，BEP 5 规范）提供第二条路：**完全不依赖 tracker，直接向其他 peer 询问"谁有这个 infohash"**。要加入 DHT 网络，只需要知道网络中任意一个节点（entry point），然后通过层层询问逐步认识更多节点——这些"认识的节点"就记录在**路由表**里，即本讲主角。

### 2.2 Kademlia：用 ID 距离组织网络

Kademlia 协议的核心约定：

- 每个节点有一个 **160 位（20 字节）的随机 ID**。
- 每个种子也用一个 160 位的 infohash 标识（u6-l1 中你已经见过 infohash 的计算）。
- 两个 ID 之间的"距离"不用减法，而用**按位异或**：

\[
d(x, y) = x \oplus y
\]

XOR 距离的好处是对称（\(d(x,y)=d(y,x)\)）且把 ID 空间当成一棵二叉前缀树。直觉上：**两个 ID 从最高位开始相同的位数（共享前缀）越多，XOR 值越小，距离越近**。例如 `0b1011...` 和 `0b1010...` 共享前 3 位，XOR 结果的最高非零位只出现在第 4 位，距离量级是 \(2^{156}\)；而 `0b1011...` 和 `0b0111...` 第一位就不同，距离量级是 \(2^{159}\)。

### 2.3 K 桶：越近记得越细

Kademlia 把整个 160 位 ID 空间按前缀划分成若干区间，每个区间对应一个 **K 桶（K-bucket）**：

- 每个桶最多容纳 \(K = 8\) 个节点；
- 包含"自己"的桶可以继续一分为二（分裂），所以**离本地节点 ID 越近的区域，桶划分得越细**；远离自己的区域则可能整段空间只有一个桶。

为什么这样设计？因为查找任何一个目标 ID（比如某个 infohash）时，我们只需要知道"离目标最近的几个节点"，把它们当作下一跳去问。离自己近的方向记得细，远的方向记个大概，恰好够用又省内存。

aria2 的实现有一个务实的简化：**不显式计算 XOR 距离**，而是用"字典序区间 `[min, max]` + 二分分裂"来表达同一套前缀划分——一个前 p 位固定的 ID 集合，恰好是字典序上一段连续区间；反之，min 与 max 前 p 位相同的闭区间，其中的所有 ID 也共享这 p 位前缀。你在 4.2 节会看到这个等价性如何落到代码。

### 2.4 与前面讲义的衔接

- `DHTSetup`、`DHTEntryPointNameResolveCommand` 等都是挂在 `DownloadEngine` 主循环里的周期命令（u2-l1、u2-l3 的 TimeBasedCommand 家族）。
- DHT 与 BT 共享同一个 UDP 端口，`BtRegistry`（u6-l2）托管了 `UDPTrackerClient`；DHT 自己的"注册表"则是本讲的 `DHTRegistry`。
- u7-l2 将讲 DHT 报文，u7-l3 讲任务系统——本讲的路由表是它们的查询基础。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/DHTNode.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTNode.h) / [src/DHTNode.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTNode.cc) | 一个远端节点的表示：20 字节 ID、地址、RTT、健康度计数器、最近联系时间 |
| [src/DHTConstants.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTConstants.h) | DHT 全部常量：ID 长度 20、桶刷新间隔 15 分钟等 |
| [src/DHTBucket.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.h) / [src/DHTBucket.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.cc) | K 桶：ID 区间 `[min,max]` + 最多 8 个节点 + 2 个替补缓存；分裂逻辑 |
| [src/bitfield.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bitfield.cc) | `flipBit`：分裂时翻转指定位 |
| [src/DHTBucketTree.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.h) / [src/DHTBucketTree.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.cc) | 路由二叉树（叶子持桶）+ K 近邻查找算法 |
| [src/DHTRoutingTable.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTable.h) / [src/DHTRoutingTable.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTable.cc) | 路由表门面：addNode 分裂循环、K 近邻、桶枚举 |
| [src/DHTRegistry.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRegistry.h) / [src/DHTRegistry.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRegistry.cc) | 进程级静态注册表，IPv4/IPv6 各一套（`data_` / `data6_`） |
| [src/DHTSetup.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc) | 装配整套 DHT 组件、从 dht.dat 恢复路由表 |
| [src/DHTAutoSaveCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTAutoSaveCommand.cc) / [src/DHTRoutingTableSerializer.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTableSerializer.cc) | 每 30 分钟及停机时把路由表序列化到 dht.dat |
| [src/DHTBucketRefreshCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketRefreshCommand.cc) / [src/DHTBucketRefreshTask.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketRefreshTask.cc) | 周期性桶刷新 |
| [src/DHTMessageTracker.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageTracker.cc) | 报文超时 → 节点健康度惩罚 → 坏节点出表 |
| 测试 | [test/DHTNodeTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DHTNodeTest.cc)、[test/DHTBucketTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DHTBucketTest.cc)、[test/DHTBucketTreeTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DHTBucketTreeTest.cc)、[test/DHTRoutingTableTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DHTRoutingTableTest.cc)、[test/DHTRoutingTableSerializerTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DHTRoutingTableSerializerTest.cc) |

## 4. 核心概念与源码讲解

本讲按"节点 → 桶 → 树 → 表与注册表"四层拆解，自底向上。

### 4.1 DHTNode：一个远端节点在本地的心智模型

#### 4.1.1 概念说明

路由表里存的是"我认识的节点"。对一个节点，aria2 关心四件事：

1. **你是谁**——20 字节节点 ID；
2. **你在哪**——IP 地址与 UDP 端口；
3. **你健康吗**——一个整数计数器 `condition_`，值越大越不可信；
4. **你最近响应多快**——RTT（往返时延）与最近一次联系时间。

`DHTNode` 就是这四件事的载体。注意它是一个**纯数据对象**：不做网络 I/O，健康度由外部（消息收发组件）维护。

#### 4.1.2 核心流程

健康度是一个单调计数器加两个时间判定的三态机：

```text
                  收到有效响应: markGood() -> condition_ = 0
                  +------------------------------------------+
                  |                                          |
   [good] ---消息超时 timeout(): condition_++---> [可疑] ---condition_ >= 5---> [bad]
                                                             |
                                             15 分钟未联系 lastContact_ 超时 -> [questionable]
```

- `isBad()`：`condition_ >= 5`（连续 5 次消息超时）；
- `isQuestionable()`：不坏，且距离上次联系超过 15 分钟（`DHT_NODE_CONTACT_INTERVAL`）；
- `isGood()`：既不坏也不可疑。

本地节点自己的 ID 在首次启动时随机生成；ID 用"40 字节随机数做 SHA-1，取 20 字节摘要"的方式产生，保证均匀散布在 ID 空间中。

#### 4.1.3 源码精读

先看字段定义：节点 ID 是 20 字节数组（`DHT_ID_LENGTH` 常量为 20），健康度是 `int condition_`，最近联系时间用 `Timer lastContact_`。

- [src/DHTNode.h:L49-L59](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTNode.h#L49-L59)：`id_`、`ipaddr_`、`port_`、`rtt_`、`condition_`、`lastContact_` 六个字段——这就是节点的全部状态。
- [src/DHTConstants.h:L48](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTConstants.h#L48)：`constexpr size_t DHT_ID_LENGTH = 20;`，ID 空间是 \(2^{160}\)。同文件 [L55](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTConstants.h#L55) 定义 `DHT_NODE_CONTACT_INTERVAL = 15_min`，是"可疑"判定的时间阈值。

两个构造函数的语义差别值得注意：无参构造**生成随机 ID**（用于本地节点），带 ID 构造用于远端节点，且初始 `condition_` 为 1 而不是 0——一个"只听说、未验证"的节点从第一天起就带着一分嫌疑。

- [src/DHTNode.cc:L46-L60](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTNode.cc#L46-L60)：无参构造 `condition_(0)` 后调用 `generateID()`；带 ID 构造 `condition_(1)` 并 `memcpy` ID。
- [src/util.cc:L2130-L2136](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L2130-L2136)：`generateRandomKey` —— 生成 40 字节随机数，做 SHA-1，摘要写入 20 字节 key。这是本地节点 ID 的出生地。

三态判定与健康度操作：

- [src/DHTNode.cc:L82-L92](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTNode.cc#L82-L92)：`isGood` = 不坏且不可疑；`isBad` 判定 `condition_ >= BAD_CONDITION`（常量 5 定义在 [L84](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTNode.cc#L84-L86)）；`isQuestionable` 要求不坏且 `lastContact_` 距今超过 15 分钟。
- [src/DHTNode.cc:L94-L100](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTNode.cc#L94-L100)：`markGood()` 清零计数器、`markBad()` 直接置 5、`updateLastContact()` 刷新联系时钟、`timeout()` 计数加一。

谁在调用这些方法（这一步很重要，说明健康度的数据来源）：

- [src/DHTMessageReceiver.cc:L121-L129](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageReceiver.cc#L121-L129)：每收到一条**有效** DHT 报文（无论对方是主动查询还是响应），就 `markGood()` + `updateLastContact()` 并尝试 `addGoodNode` 入表。
- [src/DHTMessageTracker.cc:L119-L141](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageTracker.cc#L119-L141)：发出的查询超时后 `node->timeout()` 计数；一旦 `isBad()` 成立，立刻 `routingTable_->dropNode(node)` 把它从路由表摘除（4.4 节展开）。

另一个细节：`operator<` 按 ID 逐字节比较，但注意它在"完全相等"时返回 `true`——这是一个"小于等于"语义的非常规实现，阅读时留意（[src/DHTNode.cc:L69-L80](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTNode.cc#L69-L80)）。

#### 4.1.4 代码实践

**实践目标**：亲眼看到本地节点 ID 的生成与日志输出位置。

**操作步骤**：

1. 确认已按 u1-l2 编译出 `src/aria2c`。
2. 删除旧的 dht.dat（若存在）：`rm -f ~/.aria2/dht.dat ~/.cache/aria2/dht.dat`。
3. 找一个 torrent 文件或 magnet 链接，运行：
   ```console
   $ ./src/aria2c --enable-dht --dht-entry-point=<某个公共DHT节点HOST:PORT> \
       -l=dht-debug.log --log-level=debug your.torrent
   ```
   等待约 1 分钟后按一次 Ctrl-C 优雅停止（u2-l1 讲过首按是优雅停机）。
4. 在日志中搜索 `Initialized local node ID=`。注意这行是 `A2_LOG_DEBUG` 级别（见 4.1.3 的引用行），必须用 `--log-level=debug` 才会出现；`--log-level=info` 只能看到 `No DHT entry point specified.` 与 `Saving DHT routing table to ...` 这类 INFO 行。

**需要观察的现象**：日志里出现一行 `Initialized local node ID=<40 个十六进制字符>`，它由 [src/DHTSetup.cc:L140-L141](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L140-L141) 打印。由于我们删除了 dht.dat，这个 ID 一定来自 `DHTNode()` 无参构造的随机生成路径；再运行一次并对比，两次 ID 不同。

**预期结果**：能定位到该日志行，且能对应到 4.1.3 中的 `generateID()` 调用链。若不指定 `--dht-entry-point`，日志会出现 `No DHT entry point specified.`（[src/DHTSetup.cc:L207-L209](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L207-L209)），此时路由表只能靠种子文件 `nodes` 字段或空表起步。本实践需要真实网络环境，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么"远端节点"构造时 `condition_` 初始为 1，而本地节点是 0？

**答案**：远端节点的 ID 是对方自报的，未经任何响应验证，天然带一分嫌疑；`condition_` 只会被 `markGood()`（收到有效响应）清零，因此一个从未响应过的远端节点只要再积累 4 次超时就会变坏。本地节点 ID 是自己生成的，不存在可信度问题。

**练习 2**：`isQuestionable()` 的两个条件（`!isBad()` 与超时 15 分钟）为什么要先排除 bad？

**答案**：健康度三态是互斥分级：bad（≥5 次超时）比 questionable（只是久未联系）更严重，判定顺序必须先查更重的状态，否则坏节点会被降级归类成"可疑"，导致本应立即出表的节点继续留在桶里占位。

**练习 3**：`DHTNode` 没有 `isGood()` 的调用方（可用 `grep -rn 'isGood()' src/` 验证），这是不是死代码？

**答案**：`isGood()` 在 src/ 下确实没有调用方（仅 `isBad`/`isQuestionable` 被用到），属于保留的语义接口；但注意 `getGoodNodes()`（4.2 节）名字里的 "good" 实际实现是"非 bad"，并不调用 `isGood()`。读源码时不能只看函数名，要看实现。

### 4.2 DHTBucket：K 桶、ID 区间与分裂

#### 4.2.1 概念说明

`DHTBucket`（K 桶）回答一个问题：**"落在某段 ID 区间里的节点归我管"**。它的三个核心构件：

1. **区间边界**：`min_[20]` 与 `max_[20]` 两个闭区间端点。新来一个节点，ID 落在 `[min, max]` 内（字典序）就归这个桶。
2. **节点队列** `nodes_`：最多 `K = 8` 个节点，按"最久未见在前"的升序维护。
3. **替补缓存** `cachedNodes_`：最多 `CACHE_SIZE = 2` 个节点的候补名单，桶满时收留新节点，正式节点被摘除时优先从缓存递补。

`prefixLength_` 记录本桶区间前多少位是固定前缀——它既是"分裂深度"的度量，也是 4.4 节桶刷新时生成随机目标 ID 的依据。

Kademlia 的 XOR 距离在这里被翻译成了字典序区间：桶分裂永远在 `prefixLength_` 指向的那一位上把区间一分为二，因此**同一桶内所有 ID 共享相同的前缀**；前缀越长，桶内节点在 XOR 意义上离得越近。aria2 全程不需要计算任何 XOR 值。

#### 4.2.2 核心流程

**addNode 的四条出路**（新节点入桶）：

```text
addNode(node):
  在 nodes_ 中按 ID 查找
  ├─ 已存在 -> 移到队尾(最近见过), 返回 true
  ├─ 不存在且 nodes_.size() < K=8 -> 直接入队, 返回 true
  ├─ 桶已满, 但队首(最旧)节点 isBad() -> 驱逐队首, 新节点入队, 返回 true
  └─ 桶已满且队首不坏 -> 返回 false  (由上层决定: 分裂? 还是进替补缓存?)
```

**split 分裂**（把一个桶变成两个）：

```text
设当前区间 [min, max], 前缀长 p:
  rMax = max 的第 p 位翻转为 0  (即 max ∩ ~bit_p)
  rMin = min                      (原样)
  min  = min 的第 p 位翻转为 1   (this 变成上半区)
  p    = p + 1
  返回新桶 rBucket = [rMin, rMax] (下半区, 同样的 p)
  原 nodes_ 按是否落入 rBucket 分给两个桶
```

由于分裂总是在"最高未定位位"上按 0/1 切开，上半区与下半区各共享一个长 1 位的前缀。**分裂只允许发生在包含本地节点的桶上**（`splitAllowed`），这正是"离自己越近分得越细"的实现机关。

#### 4.2.3 源码精读

- [src/DHTBucket.h:L54-L70](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.h#L54-L70)：`prefixLength_`、`min_`/`max_`、`localNode_`、`nodes_`（注释明确"sorted in ascending order"）、`cachedNodes_`（注释说明按最近见到排序、上限 `CACHE_SIZE`）。
- [src/DHTBucket.h:L84-L86](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.h#L84-L86)：`static const size_t K = 8;` 与 `CACHE_SIZE = 2;`——Kademlia 的 K 参数就定义在这里。
- [src/DHTBucket.cc:L65-L70](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.cc#L65-L70)：根桶构造——`min` 全 0、`max` 全 0xff、`prefixLength` 0，即最初一个桶覆盖**整个** \(2^{160}\) 空间（不只是包含本地节点的那部分）。
- [src/DHTBucket.cc:L96-L104](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.cc#L96-L104)：`isInRange` 用两次 `std::lexicographical_compare` 表达闭区间判定 `min <= nodeID <= max`——把 20 字节 ID 当作 20 元组的字典序，这就是"XOR 距离 → 区间划分"等价性的代码落点。
- [src/DHTBucket.cc:L106-L131](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.cc#L106-L131)：`addNode` 四条出路。注意 [L116-L120](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.cc#L116-L120)：只有**队首**（最久未见者）恰好是坏节点时才驱逐；这是"坏节点替换"的第一条触发路径。
- [src/DHTBucket.cc:L133-L140](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.cc#L133-L140)：`cacheNode` 把进不了正式名单的节点推入替补缓存头部，超过 2 个就截断。
- [src/DHTBucket.cc:L142-L152](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.cc#L142-L152)：`dropNode`——正式节点被摘除时，若替补缓存非空，立刻把缓存队首递补进 `nodes_`。这是"坏节点替换"的第二条路径：**先递补、不留空位**。
- [src/DHTBucket.cc:L172-L175](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.cc#L172-L175)：`splitAllowed` 的两个条件——前缀长度未达 159（即还能再切一位），且**本地节点落在本桶区间内**。后者保证只有"自己所在的桶"才会越分越细。
- [src/DHTBucket.cc:L177-L212](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.cc#L177-L212)：`split` 全过程。位翻转用 [src/bitfield.cc:L41-L47](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bitfield.cc#L41-L47) 的 `flipBit`：`data[bitIndex/8] ^= 128 >> (bitIndex%8)`，即最高位算 bit 0（MSB-first），与前缀语义一致。分裂后原桶变上半区 `[翻转后的min, 原max]`，新桶是下半区 `[原min, 翻转后的max]`；两行 `A2_LOG_DEBUG` 打印了新桶/旧桶的区间，是实践 4.2.4 的观察点。
- [src/DHTBucket.cc:L214-L221](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.cc#L214-L221)：`getGoodNodes` 只过滤 `isBad()` 的节点——名字叫 good，实现是"非坏"，可疑节点也会被返回。
- [src/DHTBucket.cc:L246-L250](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.cc#L246-L250)：`needsRefresh`——桶未满，或距上次更新超过 `DHT_BUCKET_REFRESH_INTERVAL`（15 分钟，[src/DHTConstants.h:L57](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTConstants.h#L57)）。
- [src/DHTBucket.cc:L74-L84](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.cc#L74-L84)：`getRandomNodeID` 生成一个落在本桶区间内的随机 ID：先全随机，再把前 `lastByteIndex+1` 字节覆盖成本桶 min 的对应字节。桶刷新任务用它产生"往哪个方向找"的目标。

#### 4.2.4 代码实践

**实践目标**：用单元测试验证一次分裂的区间算术。

**操作步骤**：

1. 打开 [test/DHTBucketTest.cc:L182-L231](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DHTBucketTest.cc#L182-L231)（`testSplit`，断言主体集中在 L210-L231）。
2. 阅读其断言：本地节点 ID 全 0，根桶 `[0x00..00, 0xff..ff]` 分裂后，返回的新桶（右/下半区）应为 `[0x00..00, 0x7f..ff]` 且 `prefixLength == 1`；原桶（左/上半区）变为 `[0x80..00, 0xff..ff]`。
3. 手算验证：`max = 0xff...` 第 0 位（MSB）是 1，翻转为 0 得 `0x7f...`，即 `rMax`；`min = 0x00...` 第 0 位翻转为 1 得 `0x80...`，即新 `min_`。
4. 若已编译测试（u9-l5 会详述测试构建），可运行 `test/aria2c` 测试程序的对应用例验证。

**需要观察的现象**：断言中的 `expectedRMax = 0x7f..ff`、`expectedLMin = 0x80..00` 与 4.2.2 的伪代码逐行对应。

**预期结果**：能独立解释"为什么返回的是下半区而 this 保留上半区"（因为 `min_` 原地翻转、`rMin` 用未翻转的副本）。本实践的运行结果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `nodes_` 要维护"升序（最近见过在队尾）"的顺序？

**答案**：Kademlia 的经验法则是"老节点比新节点更可能稳定在线"，因此驱逐时永远优先赶走队首（最久未见者）。`addNode` 重插到队尾、`moveToTail`（响应验证后调用）都是维持这一顺序的操作；只有 `isInRange(localNode_)` 的桶能分裂，加上"驱逐只看队首"，共同实现"近处细粒度、远处粗粒度"的路由表形态。

**练习 2**：替补缓存 `cachedNodes_` 为什么只有 2 个名额？

**答案**：替补缓存的作用是"桶满时先收留、空位出现时快速递补"，避免为每个被拒节点立刻发起质询。2 个名额在"记住候选"与"内存/维护成本"间取折中；超过名额时按"最近见过优先"截断（[src/DHTBucket.cc:L137-L139](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.cc#L137-L139)）。

**练习 3**：`splitAllowed` 为什么要限制 `prefixLength_ < DHT_ID_LENGTH * 8 - 1`（即 < 159）而不是 < 160？

**答案**：前缀 159 的桶区间只剩最后 1 位自由度（两种取值），再分裂一次就会得到前缀 160 的桶——区间里只有一个 ID，没有继续分裂的意义；极限情况下若分裂到 160 位，`getRandomNodeID` 中 `(prefixLength-1)/8` 的字节计算也会越过最后一个字节。所以允许的最后一次分裂发生在前缀 159 → 上限 160 之前，用 `< 159` 的判断留出这一位。此处边界推理基于常量计算，如需实证可在本地对 `prefixLength_ = 159` 的桶调用 `split()` 观察 `assert` 行为，**待确认**。

### 4.3 DHTBucketTree：路由二叉树与 K 近邻查找

#### 4.3.1 概念说明

桶会越裂越多，需要一个结构把所有桶组织起来并能快速定位——这就是 `DHTBucketTreeNode` 构成的**二叉路由树**：

- **叶子节点持有桶**（`bucket_` 非空），**内部节点只持有区间**（`minId_`/`maxId_`，桶指针置空）；
- 左子树管小 ID、右子树管大 ID（4.2 节分裂返回的下半区成为左孩子）；
- 树"高度不平衡"：包含本地节点的路径不断加深，其余方向长期是浅叶子。

类注释原文说得很清楚：*"This class represents Kademlia DHT routing tree. The leaf nodes have bucket which contains DHT node. The tree is binary tree but highly unbalanced."*（[src/DHTBucketTree.h:L50-L53](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.h#L50-L53)）。

在这棵树上，本模块要讲清三个操作：`findTreeNodeFor`/`findBucketFor`（一个 ID 属于哪个桶）、`findClosestKNodes`（一个 ID 附近最近的 K 个好节点——DHT 查询的元操作）、`enumerateBucket`（遍历所有桶）。

#### 4.3.2 核心流程

**定位（dig）**：从根开始，每个内部节点比较 key 与左孩子区间，落在左边走左、否则走右，直到叶子：

```text
findTreeNodeFor(root, key):
  while root 不是叶子:
    root = (key 在左孩子区间内) ? left : right
  return root          // key 所属的桶所在叶子
```

**K 近邻查找（findClosestKNodes）**，注释声明结果"顺序任意"、可能不足 K 个：

```text
findClosestKNodes(nodes, root, key):
  leaf = findTreeNodeFor(root, key)
  if leaf == root: 收集该桶的好节点, 结束
  parent = leaf 的父节点
  # 第一阶段: 向下, 以 parent 为子树, 同侧优先
  if leaf 是 parent 的左孩子: collectDownwardLeftFirst(parent)
  else:                        collectDownwardRightFirst(parent)
  #    (先收 leaf 所在桶, 不足 K 再收兄弟子树最贴近的一侧)
  # 第二阶段: 向上, 凑不满 K 时沿父链逐层收"叔叔子树"
  if nodes.size() < K: collectUpward(parent)
  截断到 K 个
```

"同侧优先"和"先下后上"共同保证：**先收区间离 key 最近的桶，逐步向远处扩展**——这正是 Kademlia 查找中"返回离目标最近的 K 个节点"的近似实现，且不用对候选节点做任何距离排序（调用方自行按需处理）。

**遍历（enumerateBucket）**：普通的中序递归，把所有叶子的桶收集进 vector，供保存与刷新使用。

#### 4.3.3 源码精读

- [src/DHTBucketTree.cc:L53-L58](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.cc#L53-L58)：叶子构造函数——从桶复制 `minId_`/`maxId_`。
- [src/DHTBucketTree.cc:L62-L68](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.cc#L62-L68)：`resetRelation`——内部节点接管两个孩子：设父指针，并令 `minId_ = 左孩子的 min`、`maxId_ = 右孩子的 max`。左右不变式在此确立。
- [src/DHTBucketTree.cc:L70-L81](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.cc#L70-L81)：`dig`——对叶子返回空，对内部节点按"key 是否在左孩子区间"二选一下降。
- [src/DHTBucketTree.cc:L91-L97](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.cc#L91-L97)：`DHTBucketTreeNode::split`——调用 4.2 节的 `DHTBucket::split()`，**返回的下半区新桶成为左孩子**、原桶（上半区）成为右孩子，然后 `bucket_.reset()` 使自己"退化"为内部节点。这是桶与树唯一的接缝。
- [src/DHTBucketTree.cc:L101-L110](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.cc#L101-L110)：`findTreeNodeFor` 递归下降（叶子即返回）。
- [src/DHTBucketTree.cc:L112-L117](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.cc#L112-L117)：`findBucketFor` = `findTreeNodeFor(...)->getBucket()`。
- [src/DHTBucketTree.cc:L119-L127](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.cc#L119-L127)：`collectNodes`——对一个桶调用 `getGoodNodes` 后追加进结果（再次强调：过滤的是 bad，不是 questionable）。
- [src/DHTBucketTree.cc:L130-L143](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.cc#L130-L143) 与 [L146-L159](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.cc#L146-L159)：左优先/右优先两种向下收集——先走目标所在一侧，凑不满 K 才走另一侧，且对内部节点正确递归。
- [src/DHTBucketTree.cc:L161-L182](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.cc#L161-L182)：`collectUpward`——沿父链向上，每层收集"来自那一层的叔叔"的桶，凑满 K 即停。注意它对叔叔直接调 `getBucket()` 而没有先检查 `leaf()`（对照向下方向的两个函数都有内部节点递归）——若某层的叔叔恰好是内部节点，这里拿到的是空 `shared_ptr`。现有测试（见 4.3.4）未覆盖该形态，是否可在真实路由表中触发值得思考，**待确认**。
- [src/DHTBucketTree.cc:L184-L210](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.cc#L184-L210)：`findClosestKNodes` 主体——先下后上两阶段，最后截断到 K。头注释（[src/DHTBucketTree.h:L108-L113](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.h#L108-L113)）明确 "The order of nodes is arbitrary"。
- [src/DHTBucketTree.cc:L212-L222](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.cc#L212-L222)：`enumerateBucket` 中序遍历。

调用方（谁在用 K 近邻）：

- [src/DHTFindNodeMessage.cc:L65](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTFindNodeMessage.cc#L65) 与 [src/DHTGetPeersMessage.cc:L123](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTGetPeersMessage.cc#L123)：收到远端的 find_node / get_peers **查询**时，用 `getClosestKNodes` 找出本地所知最近节点放进应答——路由表的价值出口。
- [src/DHTNodeLookupTask.cc:L57](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTNodeLookupTask.cc#L57)、[src/DHTPeerLookupTask.cc:L70](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPeerLookupTask.cc#L70)：迭代查找任务用**应答**里返回的节点继续下一轮查询（u7-l3 展开）。

#### 4.3.4 代码实践

**实践目标**：在一张手工构造的树上核对 `findClosestKNodes` 的"先下后上"收集顺序。

**操作步骤**：

1. 打开 [test/DHTBucketTreeTest.cc:L109-L175](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DHTBucketTreeTest.cc#L109-L175)（`testFindClosestKNodes`）。
2. 照注释画出这棵树（本地节点 ID = `0xaa..`）：连续 4 次 `split` 得到 5 个桶，再手工用 `make_unique<DHTBucketTreeNode>(left, right)` 拼成 4 层的树；每个桶用 `getRandomNodeID` 生成 2 个节点填入。
3. 跟踪第一组断言：`targetID = 0x80...` 应依次从 bucket4、bucket5、bucket3、bucket1 各收 2 个节点，共 8 个。
4. 对照 4.3.2 的两阶段伪代码解释这个顺序：target 落在 bucket4 → 向下（bucket4 → bucket5 → bucket3）只凑到 6 个 → 向上收叔叔 bucket1 的 2 个，恰好 8 个截断。

**需要观察的现象**：`nodes[0..1] ∈ bucket4`、`nodes[2..3] ∈ bucket5`、`nodes[4..5] ∈ bucket3`、`nodes[6..7] ∈ bucket1` 的断言顺序与"同侧优先、先下后上"一一对应；第二组 `targetID = 0xf0...` 的断言则验证从另一侧进入时的对称行为。

**预期结果**：能不看解析地说出 8 个节点各自的来源桶。运行结果**待本地验证**（测试构建方法见 u9-l5）。

#### 4.3.5 小练习与答案

**练习 1**：`findClosestKNodes` 为什么不按距离排序就返回？

**答案**：协议场景只需要"返回一组足够近的候选"：应答方（find_node/get_peers 应答）把节点列表原样发回，由查询方（NodeLookupTask）在本地比较目标距离、挑选下一跳。省掉排序既降低应答路径开销，也与头注释"The order of nodes is arbitrary"一致。

**练习 2**：`dig` 在叶子上返回 `nullptr`，而 `findTreeNodeFor` 为什么不会对叶子调 `dig`？

**答案**：`findTreeNodeFor` 先判断 `root->leaf()`，是叶子就直接返回，只在内部节点上调用 `dig`。`dig` 的空返回值语义是"已到底"，供其他调用方区分节点类型使用（`testDig` 用例专门验证了这一点）。

**练习 3**：为什么说这棵树"高度不平衡"是特性而不是缺陷？

**答案**：不平衡恰好对应 Kademlia 的需求：包含本地节点 ID 的路径因 `splitAllowed` 的限制而不断加深（近处精细），其余子树只要桶没被填满就永远是浅叶子（远处粗略）。查找的时间复杂度与树的深度成正比，而深度只沿着"近"方向增长，与"只需要在近处精确路由"的使用模式匹配。

### 4.4 DHTRoutingTable 与 DHTRegistry：门面、双栈与持久化

#### 4.4.1 概念说明

**DHTRoutingTable** 是路由树的拥有者与对外门面：它持有根节点 `root_` 和本地节点引用，把 4.2/4.3 的原语组合成三组上层能力——`addNode`（含"满则分裂再入"循环）、`getClosestKNodes`/`getBucketFor`（查找）、`getBuckets`（遍历，供保存与刷新）。桶数量计数 `numBucket_` 也在这里。

**DHTRegistry** 则是**进程级静态注册表**：它用两个静态 `Data` 结构（`data_` 与 `data6_`）分别保存 IPv4 与 IPv6 各一整套 DHT 组件——本地节点、路由表、任务队列/工厂、peer 宣告存储、token 跟踪器、消息分发/接收/工厂。之所以要两套完全独立的副本，是因为 **IPv4 与 IPv6 是两个物理隔离的 DHT 网络**：同一个节点在两张网里的 ID、邻居、可达性都不同，混在一张表里会导致向"IPv6-only 节点"发 IPv4 查询这类根本无法送达的路由。

最后，路由表要能跨进程存活：aria2 每 30 分钟及停机时把它序列化到 `dht.dat`（IPv6 版是 `dht6.dat`），下次启动时反序列化重建，避免每次都从 entry point 白手起家。

#### 4.4.2 核心流程

**addNode 的分裂循环**（门面层最核心的逻辑）：

```text
addNode(node, good):
  if node 的 ID == 本地 ID: 拒绝
  treeNode = findTreeNodeFor(root, node.ID)
  loop:
    bucket = treeNode 的桶
    if bucket.addNode(node) 成功: return true
    elif bucket.splitAllowed():            # 桶满但可分裂(包含本地节点)
      treeNode.split(); numBucket_++
      treeNode = 新节点 ID 所落的那一侧孩子   # 下降一层再试
    else:                                  # 桶满且不可分裂
      if good: bucket.cacheNode(node)      # 好节点进替补缓存
      return false
```

**桶刷新**（防止"远桶"里的节点悄悄失联）：

```text
DHTBucketRefreshCommand (每 5 分钟, TimeBasedCommand)
  └─> 创建 DHTBucketRefreshTask 入任务队列
        └─> 遍历所有桶: needsRefresh()?(未满 或 超过15分钟未更新)
              ├─ notifyUpdate() 重置时钟
              ├─ getRandomNodeID(targetID)   # 桶区间内随机目标
              └─ 创建 DHTNodeLookupTask(targetID)  # 迭代查找, 顺路认识新节点
```

**坏节点出表的完整触发链**（本讲学习目标的重点）：

```text
路径A(主动): 发出的查询超时(DHTMessageTracker) -> node.timeout() 计数+1
             -> 累计>=5 次 isBad() -> routingTable.dropNode(node)
                -> bucket.dropNode: 从 nodes_ 摘除 + 替补缓存队首递补
路径B(被动): 桶满时新节点入桶(addNode)
             -> 队首(最旧)节点 isBad()? -> 驱逐队首, 新节点顶上
路径C(登记): 收到有效报文(DHTMessageReceiver)
             -> markGood()+updateLastContact()+addGoodNode()
             -> 桶满且不可分裂时进替补缓存等机会
遗留路径:   DHTReplaceNodeTask(质询可疑节点, 两次超时则 markBad 并顶替)
             -- 其工厂方法 createReplaceNodeTask 目前在 src/ 下无调用方
```

**持久化**：

```text
保存: DHTAutoSaveCommand(每30分钟) 或 停机(preProcess 检测 halt)
        -> getBuckets() 枚举全部桶 -> 每桶 getGoodNodes() 汇总
        -> DHTRoutingTableSerializer.serialize(dht.dat)
        -> 先写 dht.dat__temp 再原子改名

加载: DHTSetup(首个 BT 任务启动时)
        -> DHTRoutingTableDeserializer 反序列化出 localNode + 节点列表
        -> 新建空路由表, 逐个 addNode 重建分桶形状
        -> 若有旧节点: 创建 forceRefresh 的桶刷新任务立即验证
```

#### 4.4.3 源码精读

**DHTRoutingTable**：

- [src/DHTRoutingTable.cc:L52-L60](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTable.cc#L52-L60)：构造函数——根即一个覆盖全 ID 空间的根桶（4.2 节的全 0/全 ff 桶），`numBucket_` 从 1 起步。
- [src/DHTRoutingTable.cc:L74-L110](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTable.cc#L74-L110)：`addNode` 私有重载——先拒绝与本地同 ID 的节点（[L77-L80](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTable.cc#L77-L80)），然后进入"入桶失败→分裂→下降"的 while 循环；`good` 为 true 时失败兜底进替补缓存（[L101-L107](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTable.cc#L101-L107)）。公开入口 `addNode`/`addGoodNode` 在 [L64-L72](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTable.cc#L64-L72) 只是给 `good` 标志赋默认值。
- [src/DHTRoutingTable.cc:L112-L117](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTable.cc#L112-L117)：`getClosestKNodes` 一行转调 4.3 节的 `dht::findClosestKNodes`。
- [src/DHTRoutingTable.cc:L132-L150](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTable.cc#L132-L150)：`getBucketFor`/`getNode`——按 ID 定位桶、再按 (ID, IP, 端口) 三元组找正式节点。
- [src/DHTRoutingTable.cc:L167-L171](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTable.cc#L167-L171)：`getBuckets` 中序枚举全部桶，保存与刷新都用它。

**DHTRegistry**：

- [src/DHTRegistry.h:L56-L81](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRegistry.h#L56-L81)：内部结构 `Data`（localNode、routingTable、taskQueue、taskFactory、peerAnnounceStorage、tokenTracker、messageDispatcher/Receiver/Factory 九件套）与两个静态实例 `data_`、`data6_`。
- [src/DHTRegistry.h:L88-L106](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRegistry.h#L88-L106)：IPv4 用 `getData`/`isInitialized`，IPv6 用 `getData6`/`isInitialized6`，两套 API 完全镜像。
- [src/DHTRegistry.cc:L49-L69](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRegistry.cc#L49-L69)：两个静态实例的定义与 `clear` 的逐字段 reset（初始化失败时整体回滚，见下）。

**谁创建这一切**：

- [src/RequestGroup.cc:L350-L373](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L350-L373)：DHT **并非进程启动即初始化**，而是在第一个 BT 任务（`--enable-dht` 默认 true；`--enable-dht6` 默认 false，见 [src/OptionHandlerFactory.cc:L1768-L1782](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L1768-L1782)）创建初始命令时调用 `DHTSetup().setup(e, AF_INET)` / `(e, AF_INET6)`。纯 HTTP 下载不会启动 DHT。
- [src/DHTSetup.cc:L85-L93](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L85-L93)：幂等闸门——family 非 AF_INET/AF_INET6，或对应栈已初始化，直接返回。
- [src/DHTSetup.cc:L96-L113](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L96-L113)：先反序列化 dht.dat 拿 localNode（保留旧 ID，路由历史得以延续）；失败则新建随机 ID 节点。
- [src/DHTSetup.cc:L143](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L143)：`auto routingTable = make_unique<DHTRoutingTable>(localNode);`——每栈一张表。
- [src/DHTSetup.cc:L155-L183](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L155-L183)：九件套之间的手工依赖注入（互设指针）。
- [src/DHTSetup.cc:L247-L257](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L247-L257)：把反序列化出的旧节点逐个 `addNode` 重建分桶形状；只要旧表非空，就入队一个 `setForceRefresh(true)` 的桶刷新任务立即验证这些"隔夜节点"是否还活着。
- [src/DHTSetup.cc:L258-L285](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L258-L285)：按 family 把整套对象搬进 `DHTRegistry::getMutableData()`（IPv4）或 `getMutableData6()`（IPv6）并置 initialized——双栈设计在这里落地。
- [src/DHTSetup.cc:L291-L305](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L291-L305)：任一环节抛异常则清空临时命令并 `clearData()`/`clearData6()` 整体回滚——DHT 初始化失败只禁用 DHT，不影响下载本身。

**刷新与超时（4.4.2 两条链的代码落点）**：

- [src/DHTBucketRefreshCommand.cc:L64-L67](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketRefreshCommand.cc#L64-L67)：每 5 分钟（`DHT_BUCKET_REFRESH_CHECK_INTERVAL`，[src/DHTConstants.h:L59](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTConstants.h#L59)）创建一个桶刷新任务。
- [src/DHTBucketRefreshTask.cc:L54-L77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketRefreshTask.cc#L54-L77)：`needsRefresh` 过滤 → `notifyUpdate` 重置时钟 → `getRandomNodeID` 生成桶内随机目标 → 创建 `DHTNodeLookupTask`。
- [src/DHTMessageTracker.cc:L127-L132](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageTracker.cc#L127-L132)：超时计数与 `isBad → dropNode` 的路径 A 出口。
- [src/DHTReplaceNodeTask.cc:L63-L103](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTReplaceNodeTask.cc#L63-L103)：遗留路径——对桶内 LRU 可疑节点发 ping，两次超时则 `markBad()` 并 `addNode` 顶替；但 `createReplaceNodeTask`（[src/DHTTaskFactoryImpl.cc:L107-L115](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTTaskFactoryImpl.cc#L107-L115)）在 src/ 下没有调用方，现行代码中"可疑质询"不发生，可疑节点仅靠路径 A/B/刷新自然淘汰。

**dht.dat 的二进制格式**（综合实践的依据）：

- [src/DHTAutoSaveCommand.cc:L70-L79](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTAutoSaveCommand.cc#L70-L79)：`preProcess` 检测到全部下载完成或停机请求就保存——这就是"Ctr-C 也会落盘"的原因；[L81-L116](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTAutoSaveCommand.cc#L81-L116) 的 `save()`：枚举全部桶 → 每桶 `getGoodNodes`（坏节点不保存）→ 交给序列化器。
- [src/DHTRoutingTableSerializer.cc:L90-L99](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTableSerializer.cc#L90-L99)：8 字节头——魔数 `a1 a2`、格式 ID `02`、3 字节保留、版本 `00 03`（版本即 `DHT_VERSION`，[src/DHTConstants.h:L46](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTConstants.h#L46)）。
- [src/DHTRoutingTableSerializer.cc:L104-L121](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTableSerializer.cc#L104-L121)：8 字节保存时间（epoch 秒，网络字节序）→ 8 字节保留 + 20 字节本地节点 ID + 4 字节保留 → 4 字节节点数（大端）+ 4 字节保留。
- [src/DHTRoutingTableSerializer.cc:L123-L149](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTableSerializer.cc#L123-L149)：每个节点记录定长 56 字节——1 字节紧凑地址长度（IPv4 为 6、IPv6 为 18）+ 7 字节保留 + 紧凑地址（4 字节 IP + 2 字节大端端口）+ 补足 24 字节的保留区 + 20 字节节点 ID + 4 字节保留。注意 **dht.dat 不存桶结构**，只存扁平节点列表，桶形状在加载时由 `addNode` 重新长出来（[src/DHTRoutingTableDeserializer.cc:L77-L156](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTableDeserializer.cc#L77-L156) 按相同格式读回）。
- [src/DHTRoutingTableSerializer.cc:L150-L157](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTableSerializer.cc#L150-L157)：先写 `dht.dat__temp` 再 `renameTo` 原子替换——崩溃不会留下半截文件。
- 文件路径默认值：[src/OptionHandlerFactory.cc:L1725-L1738](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L1725-L1738) 注册 `--dht-file-path`，默认值来自 [src/util.cc:L1816-L1825](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L1816-L1825) 的 `getDHTFile`：`$HOME/.aria2/dht.dat`，若不存在则用 `$XDG_CACHE_HOME/aria2/dht.dat`（手册 [doc/manual-src/en/aria2c.rst](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/doc/manual-src/en/aria2c.rst) 同义）。

#### 4.4.4 代码实践

**实践目标**：核对序列化器的字节布局断言，为综合实践的手工解析做准备。

**操作步骤**：

1. 打开 [test/DHTRoutingTableSerializerTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DHTRoutingTableSerializerTest.cc)，阅读 `testSerialize`：它把 1 个本地节点 + 3 个远端节点序列化到临时文件，再用 `checkToLocalnode`（逐字段核对魔数、保留区、本地 ID）与 `checkNumNodes` 把文件**按字段读回**比对。注意用例里第二个节点被故意设成 `setIPAddress("non-numerical-name")`——紧凑地址打包失败时会写入全零地址（对应序列化器 [src/DHTRoutingTableSerializer.cc:L130-L135](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTableSerializer.cc#L130-L135) 的 `memset` 分支），且该节点仍占 56 字节记录。
2. 列出每段字节与 4.4.3 中 offset 的对应关系（头 8B、时间 8B、本地 ID 32B 段、计数 8B 段、每节点 56B）。
3. 回答一个问题：一个包含 100 个节点、IPv4 栈的 dht.dat 应该多大？

**需要观察的现象**：期望缓冲区的总长度 = 8 + 8 + (8+20+4) + (4+4) + 100 × 56 = 5656 字节。

**预期结果**：能推导出通用公式 `文件大小 = 56 × (节点数 + 1)`——头部与本地节点段合计恰好也是 56 字节（8+8+8+20+4+4+4 = 56）。这一不变式是综合实践的校验手段。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 dht.dat 里不保存桶（区间）信息，只保存扁平节点列表？

**答案**：桶形状是确定性的：由本地节点 ID 与节点 ID 集合完全决定——只要按相同顺序语义 `addNode`，分裂路径必然重现（`splitAllowed` 只依赖"本地节点是否在区间内"，`addNode` 驱逐只依赖健康度）。保存派生数据反而要处理"加载后节点变化导致桶对不上"的一致性问题。这是典型的"存状态、不存结构"。

**练习 2**：DHTRegistry 用静态成员保存两套 `Data`，而不是像 BtRegistry 那样挂在 DownloadEngine 上，有什么取舍？

**答案**：DHT 是进程级单例资源：UDP socket、端口绑定、路由表跨任务共享，生命周期长于任何一个 RequestGroup；而 BtRegistry 管理的六件套（u6-l2）是每种子一份、随引擎生灭。静态存储让 DHTSetup 的幂等闸门（isInitialized/isInitialized6）可以在任意 BT 任务触发时检查"是否已经装好"。代价是静态状态要靠 `clearData` 手工清理（u6-l2 也见过 BtRegistry 的类似处理）。

**练习 3**：`addNode(node, good=true)` 与 `good=false` 的区别何时体现？

**答案**：只有当目标桶已满且不允许分裂（不含本地节点）时才体现：`good=true` 的节点（来自有效报文的发送者，`addGoodNode` 路径）进替补缓存等待递补；`good=false`（如 find_node 应答里"听说"的节点）直接丢弃。换句话说，"验证过的好节点"才值得留替补。

## 5. 综合实践

**任务**：运行一次真实的 DHT 会话，拿到 `dht.dat`，手工解析它的二进制内容，并用节点 ID 与本地 ID 的共享前缀长度验证"离自己越近、桶分得越细"的路由表形态。

### 步骤 1：产生 dht.dat

```console
$ rm -f ~/.aria2/dht.dat ~/.cache/aria2/dht.dat
$ ./src/aria2c --enable-dht --dht-entry-point=<HOST:PORT> \
    --dht-listen-port=6881-6999 -l=dht.log --log-level=debug some.torrent
# 等待几分钟让路由表积累节点，然后按一次 Ctrl-C 优雅停机
$ ls -l ~/.aria2/dht.dat ~/.cache/aria2/dht.dat 2>/dev/null
```

要点（都有 4.4 节的代码依据）：

- 必须有 **BT 任务**才触发 DHTSetup（RequestGroup.cc 的 BT 分支）；
- 停机时 `DHTAutoSaveCommand::preProcess` 会立刻保存，不必等 30 分钟周期；
- 若不指定 entry point 且种子没有 `nodes` 字段，路由表很难积累节点。

### 步骤 2：手工核对头部

```console
$ xxd -l 64 ~/.aria2/dht.dat
```

按 [DHTRoutingTableSerializer.cc:L90-L121](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTableSerializer.cc#L90-L121) 的布局核对：

| 偏移 | 长度 | 含义 | 预期 |
| --- | --- | --- | --- |
| 0x00 | 8 | 头 | `a1 a2 02 00 00 00 00 03` |
| 0x08 | 8 | 保存时间（epoch 秒，大端） | 与文件 mtime 接近 |
| 0x10 | 8 | 保留 | 全 0 |
| 0x18 | 20 | **本地节点 ID** | 与日志 `Initialized local node ID=` 一致 |
| 0x2C | 4 | 保留 | 全 0 |
| 0x30 | 4 | **节点数 N**（大端） | 与文件大小满足 56×(N+1) |
| 0x34 | 4 | 保留 | 全 0 |
| 0x38 | 56×N | 节点记录 | 见步骤 3 |

### 步骤 3：脚本化解析（示例代码）

以下为**示例代码**（非 aria2 源码），建议保存到教程目录之外（如 `/tmp/parse_dht.py`）运行：

```python
#!/usr/bin/env python3
# 示例代码：解析 aria2 dht.dat（IPv4 栈，格式见 DHTRoutingTableSerializer.cc）
import struct, sys

data = open(sys.argv[1], "rb").read()
assert data[0:3] == b"\xa1\xa2\x02", "bad magic"
mtime, = struct.unpack_from(">Q", data, 8)
local_id = data[0x18:0x18 + 20]
n, = struct.unpack_from(">I", data, 0x30)
assert len(data) == 56 * (n + 1), f"size {len(data)} != 56*{n+1}"

print(f"mtime={mtime} nodes={n}")
print(f"local  ID={local_id.hex()}")

def common_prefix_bits(a, b):          # 共享前缀位数（MSB 起）
    total = 0
    for x, y in zip(a, b):
        d = x ^ y
        if d:
            return total + 7 - d.bit_length() + 1
        total += 8
    return total

hist = {}
for i in range(n):
    off = 0x38 + 56 * i
    clen = data[off]                   # IPv4 应为 6
    ip = ".".join(str(b) for b in data[off+8 : off+8+4])
    port, = struct.unpack_from(">H", data, off+12)
    nid = data[off+32 : off+52]
    p = common_prefix_bits(local_id, nid)
    hist[p] = hist.get(p, 0) + 1
    if i < 5:                          # 打印前几个节点
        print(f"node[{i}] ID={nid.hex()} prefix={p:>3}b {ip}:{port}")

print("共享前缀位数直方图:", dict(sorted(hist.items())))
```

### 步骤 4：验证桶覆盖范围

对直方图做两件事：

1. **存在性**：多数已存节点的共享前缀位数较高（集中在 4~10 位附近为正常），因为包含本地 ID 的桶分裂得最深、节点最密。
2. **深度上限**：找出直方图中的最大前缀位数 \(p_{\max}\)。由于每个桶最多 8 个节点、只有包含本地节点的路径才会分裂，理论上最深的桶前缀长度约为 \(\log_2(\text{节点总数})\) 量级——把 \(p_{\max}\) 与 N 对照，应大致满足 \(2^{p_{\max}} \lesssim 8 \times \text{桶数}\) 的量级关系（精确形状受网络拓扑影响，量级吻合即可）。

同时抽查前几个节点：用 4.2 节 `isInRange` 的语义手工确认——与本地 ID 共享 \(p\) 位前缀的节点，必然落在某个前缀长度 \(\le p\) 的桶区间内。

### 预期结果与验收

- 头 8 字节、`56×(N+1)` 尺寸不变式、本地 ID 与日志一致——三项全部通过即证明你已读懂序列化格式；
- 直方图呈"前缀位数高的一侧更密"的形态，验证分桶的空间不均匀性。

本实践依赖真实 DHT 网络可达性（UDP 出网、entry point 可用），所有观察值**待本地验证**；若网络受限，可退化为纯阅读实践：对照 `test/DHTRoutingTableSerializerTest.cc` 与 `test/DHTRoutingTableDeserializerTest.cc`，在纸上完成同样的字节布局核对。

## 6. 本讲小结

- **DHTNode** 是节点的纯数据表示：20 字节 ID + 地址 + RTT + 健康度计数器 `condition_`（≥5 次超时即 bad）+ 最近联系时间（超 15 分钟未联系为 questionable）；`markGood/timeout` 分别由有效报文与消息超时驱动。
- **DHTBucket** 用字典序闭区间 `[min,max]` 实现 Kademlia 的前缀 K 桶：每桶至多 K=8 个正式节点 + 2 个替补缓存；`split` 在 `prefixLength` 位上翻转切开区间，且只有包含本地节点的桶允许分裂——"离自己越近分得越细"的全部机关就在 `splitAllowed` 的一个条件里。
- **DHTBucketTree** 是叶子持桶的高度不平衡二叉树；`findClosestKNodes` 用"先同侧向下、后沿父链向上"两阶段收集至多 8 个非坏节点，结果不排序，排序责任留给调用方。
- **DHTRoutingTable** 的 `addNode` 循环是"入桶失败→分裂→下降重试"；坏节点出表有三条现实路径（超时计数出表、桶满驱逐坏队首、有效节点进替补缓存递补），`DHTReplaceNodeTask` 是无调用方的遗留路径。
- **DHTRegistry** 以两个静态 `Data` 保存 IPv4/IPv6 完全独立的九件套；两栈两张表，因为它们是物理隔离的网络。
- **dht.dat** 只存"本地 ID + 扁平的好节点列表"（定长 56 字节/节点，文件大小恒为 56×(N+1)），桶形状在加载时由 `addNode` 确定性重建，保存走临时文件加原子改名。

## 7. 下一步学习建议

本讲只回答了"路由表长什么样、怎么维护"；DHT 真正的运转靠三件事，正好是本单元接下来的三讲：

1. **u7-l2 DHT 报文**：bencode 编码的 ping/find_node/get_peers/announce_peer 查询与响应——你会看到 `DHTMessageTracker`（本讲健康度惩罚的数据源）如何用 transaction id 配对请求响应。
2. **u7-l3 DHT 任务系统**：`DHTTaskQueue`/`DHTTaskExecutor` 如何调度本讲反复出现的 `DHTNodeLookupTask`、桶刷新任务，以及迭代查找的收敛过程。
3. **u7-l4 DHT 启动与周期命令**：`DHTSetup` 的完整装配清单、entry point 解析与自举、`DHTBucketRefreshCommand`/`DHTAutoSaveCommand` 两个周期命令的协作全貌。

建议阅读顺序：先 u7-l2（有了报文语义，任务系统才看得懂），再 u7-l3、u7-l4。如果你对路由表本身还想深挖，可以读 `DHTRoutingTableTest.cc` 里针对 addNode 分裂循环的用例，以及 Kademlia 原论文（Maymounkov & Mazières, 2002）的第 2~4 节——对照本讲的 `splitAllowed` 实现体会工程化取舍。
