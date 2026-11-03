# DHT 基础：Kademlia 路由表与分桶

## 1. 本讲目标

本讲是 DHT 单元（单元 7）的第一篇。DHT（Distributed Hash Table，分布式哈希表）让 BT 摆脱 tracker 也能找到 peer——u6-l7 里你已经见过 tracker 这条路，本单元讲第二条路。学完本讲你应该能够：

1. 说出一个 DHT 节点在 aria2 中由哪些字段表示（`DHTNode`），以及「好 / 可疑 / 坏」三种健康度的判定与流转。
2. 解释 160 位节点 ID、XOR 距离与「共享前缀」三者的关系，说明 `DHTBucket` 的 `[min, max]` 闭区间如何实现 Kademlia 的 K 桶划分。
3. 跟踪一次 bucket 分裂（`split`）的位翻转算术，以及坏节点被驱逐 / 递补的全部触发条件。
4. 读懂 `DHTBucketTree` 路由二叉树上的 K 近邻查找（先向下、后向上），并理解生产环境里这棵树为什么必然是「梳子形」。
5. 理解 `DHTRoutingTable` 的「入桶失败→分裂→下降重试」循环，以及 `DHTRegistry` 为何保存 IPv4/IPv6 两套完全独立的路由表。

## 2. 前置知识

### 2.1 DHT 是什么：没有 tracker 的 peer 发现

第 6 单元里，BT 客户端靠 tracker 告诉它「还有谁在下载同一个种子」（u6-l7）。DHT（BEP 5 规范）提供第二条路：**不依赖 tracker，直接向其他节点询问「谁有这个 infohash」**。要加入 DHT 网络，只需认识网络中任意一个节点（entry point），然后层层打听逐步认识更多节点——这些「认识的节点」记录在**路由表**里，就是本讲的主角。

### 2.2 Kademlia：用 ID 距离组织网络

Kademlia 协议的核心约定：

- 每个节点有一个 **160 位（20 字节）随机 ID**；
- 每个种子用 160 位 infohash 标识（u6-l1 中你已见过 infohash 的计算）；
- 两个 ID 的「距离」不用减法，用**按位异或**：

\[ d(x, y) = x \oplus y \]

XOR 距离对称（\(d(x,y)=d(y,x)\)），且把 ID 空间组织成一棵二叉前缀树。直觉：**两个 ID 从最高位起相同的位数（共享前缀）越多，XOR 值越小，距离越近**。例如 `0b1011…` 与 `0b1010…` 共享前 3 位，XOR 的最高非零位在第 4 位，距离量级是 \(2^{156}\)；而 `0b1011…` 与 `0b0111…` 第一位就不同，距离量级 \(2^{159}\)。

### 2.3 K 桶：离自己越近，记得越细

Kademlia 把 160 位 ID 空间按前缀切成若干区间，每个区间一个 **K 桶（K-bucket）**：

- 每桶至多 \(K = 8\) 个节点；
- **包含本地节点 ID 的桶**可以继续一分为二（分裂），所以离自己近的区域桶越分越细，远的区域可能整段空间只有一个桶。

为什么够用？查找任意目标 ID（如某 infohash）时只需知道「离目标最近的几个节点」当下一跳去问。近处记得细、远处记个大概，恰好匹配这种使用模式。

aria2 有一个务实的简化：**全程不显式计算 XOR 距离**，改用「字典序闭区间 `[min, max]` + 二分分裂」表达同一套前缀划分——前 p 位固定的 ID 集合恰好是字典序上一段连续区间；反之 min 与 max 共享 p 位前缀的闭区间，其中所有 ID 也共享这 p 位。4.2 节会看到这个等价性如何落到代码。

### 2.4 与前面讲义的衔接

- `DHTBucketRefreshCommand`、`DHTAutoSaveCommand` 等都是挂在 `DownloadEngine` 主循环里的 `TimeBasedCommand` 周期命令（u2-l1、u2-l3 讲过这个家族）。
- DHT 与 BT 共享 UDP 端口，`BtRegistry`（u6-l2）托管 `UDPTrackerClient`；DHT 自己的进程级注册表是本讲的 `DHTRegistry`，与 `BtRegistry` 形成「进程级 / 种子级」的对照。
- u7-l2 讲 DHT 报文，u7-l3 讲任务系统——本讲的路由表是它们的查询基础。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/DHTNode.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTNode.h) / [src/DHTNode.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTNode.cc) | 一个远端节点的表示：20 字节 ID、地址、RTT、健康度计数器、最近联系时间 |
| [src/DHTConstants.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTConstants.h) | DHT 常量：ID 长 20、节点联系间隔 15 分钟、桶刷新间隔 15 分钟等 |
| [src/DHTBucket.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.h) / [src/DHTBucket.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.cc) | K 桶：区间 `[min,max]` + 至多 8 个节点 + 2 个替补缓存；分裂逻辑 |
| [src/bitfield.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bitfield.cc) | `flipBit`：分裂时翻转指定位 |
| [src/DHTBucketTree.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.h) / [src/DHTBucketTree.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.cc) | 路由二叉树（叶子持桶）+ K 近邻查找算法 |
| [src/DHTRoutingTable.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTable.h) / [src/DHTRoutingTable.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTable.cc) | 路由表门面：addNode 分裂循环、K 近邻、桶枚举 |
| [src/DHTRegistry.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRegistry.h) / [src/DHTRegistry.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRegistry.cc) | 进程级静态注册表，IPv4/IPv6 各一套（`data_` / `data6_`） |
| [src/DHTSetup.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc) | 装配整套 DHT 组件、从 dht.dat 恢复路由表 |
| [src/DHTAutoSaveCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTAutoSaveCommand.cc) / [src/DHTRoutingTableSerializer.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTableSerializer.cc) | 每 30 分钟及停机时把路由表序列化到 dht.dat |
| [src/DHTRoutingTableDeserializer.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTableDeserializer.cc) | 启动时按相同格式读回 dht.dat |
| [src/DHTBucketRefreshCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketRefreshCommand.cc) / [src/DHTBucketRefreshTask.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketRefreshTask.cc) | 周期性桶刷新 |
| [src/DHTMessageTracker.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageTracker.cc) / [src/DHTMessageReceiver.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageReceiver.cc) | 健康度的数据源：消息超时惩罚、有效报文加分 |
| 测试 | [test/DHTBucketTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DHTBucketTest.cc)、[test/DHTBucketTreeTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DHTBucketTreeTest.cc)、[test/DHTRoutingTableTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DHTRoutingTableTest.cc)、[test/DHTRoutingTableSerializerTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DHTRoutingTableSerializerTest.cc) |

## 4. 核心概念与源码讲解

按「节点 → 桶 → 树 → 表与注册表」四层自底向上拆解。

### 4.1 DHTNode：一个远端节点在本地的心智模型

#### 4.1.1 概念说明

路由表里存的是「我认识的节点」。对一个节点，aria2 关心四件事：

1. **你是谁**——20 字节节点 ID；
2. **你在哪**——IP 地址与 UDP 端口；
3. **你健康吗**——整数计数器 `condition_`，越大越不可信；
4. **你最近响应多快**——RTT（往返时延）与最近一次联系时间。

`DHTNode` 就是这四件事的载体。它是**纯数据对象**：不做网络 I/O，健康度由外部组件（消息收发，u7-l2 详讲）维护。

#### 4.1.2 核心流程

健康度是一个单调计数器加一个时间判定的三态机：

```text
                     收到有效报文: markGood() -> condition_ = 0
                    +--------------------------------------------+
                    |                                            |
  [good] ---查询超时 timeout(): condition_++--> [可疑] --- condition_ >= 5 ---> [bad]
                                                    |
                                  距上次联系超过 15 分钟 lastContact_ 超时 -> [questionable]
```

- `isBad()`：`condition_ >= 5`（累计 5 次消息超时）；
- `isQuestionable()`：不坏，且距上次联系超过 15 分钟（`DHT_NODE_CONTACT_INTERVAL`）；
- `isGood()`：既不坏也不可疑。

本地节点自己的 ID 首次启动时随机生成：**40 字节随机数做 SHA-1，取 20 字节摘要**，保证均匀散布在 ID 空间中。

#### 4.1.3 源码精读

字段定义——一个节点的全部状态：

- [src/DHTNode.h:L49-L59](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTNode.h#L49-L59)：`id_[20]`、`ipaddr_`、`port_`、`rtt_`、`condition_`、`lastContact_` 六个字段。
- [src/DHTConstants.h:L48](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTConstants.h#L48)：`constexpr size_t DHT_ID_LENGTH = 20;`——ID 空间是 \(2^{160}\)。同一行附近 [L55](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTConstants.h#L55) 定义 `DHT_NODE_CONTACT_INTERVAL = 15_min`，即「可疑」的时间阈值。

两个构造函数语义不同：无参构造**生成随机 ID**（本地节点用），带 ID 构造用于远端节点，且初始 `condition_` 为 **1** 而不是 0——「只听说、未验证」的节点从第一天起就带一分嫌疑。

- [src/DHTNode.cc:L46-L56](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTNode.cc#L46-L56)：无参构造 `condition_(0)` 后调 `generateID()`；带 ID 构造 `condition_(1)` 并 `memcpy` ID。
- [src/DHTNode.cc:L60](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTNode.cc#L60)：`generateID` 转调 `util::generateRandomKey`。
- [src/util.cc:L2130-L2136](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L2130-L2136)：`generateRandomKey`——40 字节随机数经 SHA-1 得 20 字节 key。本地节点 ID 的出生地。

三态判定与健康度操作：

- [src/DHTNode.cc:L82-L92](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTNode.cc#L82-L92)：`isGood` / `isBad`（阈值常量 `BAD_CONDITION = 5` 定义在 [L84-L86](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTNode.cc#L84-L86)）/ `isQuestionable`。
- [src/DHTNode.cc:L94-L100](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTNode.cc#L94-L100)：`markGood()` 清零、`markBad()` 直接置 5、`updateLastContact()` 刷新联系时钟、`timeout()` 计数加一。

谁在调用这些方法（健康度的数据来源，很重要）：

- [src/DHTMessageReceiver.cc:L121-L129](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageReceiver.cc#L121-L129)：每收到一条**有效** DHT 报文（无论查询还是响应），`markGood()` + `updateLastContact()` 并尝试 `addGoodNode` 入表。
- [src/DHTMessageTracker.cc:L119-L141](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageTracker.cc#L119-L141)：发出的查询超时后 `node->timeout()` 计数；一旦 `isBad()` 成立立刻 `routingTable_->dropNode(node)` 出表（4.4 节展开）。同文件 [L91-L105](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageTracker.cc#L91-L105) 顺带用应答耗时更新 RTT，并在对方换了节点 ID 时把旧 ID 的节点出表。

一个阅读陷阱：[src/DHTNode.cc:L69-L80](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTNode.cc#L69-L80) 的 `operator<` 在两 ID 完全相等时返回 `true`——是「小于等于」语义的非常规实现，读代码时留意。

#### 4.1.4 代码实践

**实践目标**：亲眼看到本地节点 ID 的生成与日志输出位置。

**操作步骤**：

1. 确认已按 u1-l2 编译出 `src/aria2c`。
2. 删除旧的路由表文件（若存在）：`rm -f ~/.aria2/dht.dat ~/.cache/aria2/dht.dat`。
3. 准备一个 torrent 文件，运行（`--enable-dht` 默认已是 true，这里显式写出便于观察）：
   ```console
   $ ./src/aria2c --enable-dht --dht-entry-point=<某个公共DHT节点HOST:PORT> \
       -l=dht-debug.log --log-level=debug your.torrent
   ```
   注意：**必须有一个 BT 任务**才会触发 DHT 装配（见 4.4.3 的 `RequestGroup::createInitialCommand`），纯 HTTP 下载不会启动 DHT。等待约 1 分钟后按一次 Ctrl-C 优雅停止（u2-l1 讲过首按是优雅停机）。
4. 在日志中搜索 `Initialized local node ID=`。这行是 `A2_LOG_DEBUG` 级别，必须 `--log-level=debug` 才会出现。

**需要观察的现象**：日志出现一行 `Initialized local node ID=<40 个十六进制字符>`。

**预期结果**：该行由 [src/DHTSetup.cc:L140-L141](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L140-L141) 打印。因为删除了 dht.dat，这个 ID 一定走了 `DHTNode()` 无参构造的随机生成路径；再跑一次对比，两次 ID 不同。若不指定 `--dht-entry-point`，日志会出现 `No DHT entry point specified.`（[src/DHTSetup.cc:L207-L209](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L207-L209)），此时路由表只能靠种子 `nodes` 字段或空表起步。本实践需要真实网络环境，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么远端节点构造时 `condition_` 初始为 1，本地节点是 0？

**答案**：远端节点的 ID 是对方自报的，未经响应验证，天然带一分嫌疑；`condition_` 只会被 `markGood()`（收到有效报文）清零，因此一个从未响应过的远端节点只要再积累 4 次超时就会变坏。本地节点 ID 自己生成，不存在可信度问题。

**练习 2**：`isQuestionable()` 为什么要先排除 bad 再判时间？

**答案**：三态是互斥分级，bad（≥5 次超时）比 questionable（只是久未联系）更严重。先查重态再查轻态，否则坏节点会被降级归类成「可疑」，本应立即出表的节点继续留在桶里占位。

**练习 3**：`DHTNode` 的 `isGood()` 在 src/ 下没有调用方（可用 `grep -rn 'isGood()' src/` 验证），这是死代码吗？

**答案**：`isGood()` 确实没有直接调用方，属于保留的语义接口；但注意 4.2 节 `getGoodNodes()` 名字里的 "good" 实际实现是「非 bad」，并不调用 `isGood()`。读源码不能只看函数名，要看实现。

### 4.2 DHTBucket：K 桶、ID 区间与分裂

#### 4.2.1 概念说明

`DHTBucket`（K 桶）回答一个问题：**「落在某段 ID 区间里的节点归我管」**。三个核心构件：

1. **区间边界**：`min_[20]` 与 `max_[20]` 两个闭区间端点。新节点 ID 落在 `[min, max]`（字典序）内就归这个桶。
2. **节点队列** `nodes_`：至多 `K = 8` 个正式节点，按「最久未见在前」升序维护（头部注释 *sorted in ascending order*）。
3. **替补缓存** `cachedNodes_`：至多 `CACHE_SIZE = 2` 个候补，桶满时收留新节点，正式节点被摘除时优先递补。

`prefixLength_` 记录本桶区间前多少位是固定前缀——既是「分裂深度」的度量，也是 4.4 节桶刷新时生成随机目标 ID 的依据。

Kademlia 的 XOR 距离在这里被翻译成字典序区间：分裂永远在 `prefixLength_` 指向的那一位上把区间一分为二，因此**同桶内所有 ID 共享相同前缀**；前缀越长，桶内节点在 XOR 意义上离本地越近。全程无需计算任何 XOR 值。

#### 4.2.2 核心流程

**addNode 的四条出路**（新节点入桶）：

```text
addNode(node):
  在 nodes_ 中按 ID 查找
  ├─ 已存在 -------------------> 移到队尾(最近见过), 返回 true
  ├─ 不存在且 nodes_.size() < 8 -> 直接入队, 返回 true
  ├─ 桶满, 队首(最旧) isBad() --> 驱逐队首, 新节点入队, 返回 true
  └─ 桶满且队首不坏 -----------> 返回 false (上层决定: 分裂? 还是进替补缓存?)
```

**split 分裂**（一个桶变两个）：

```text
设当前区间 [min, max], 前缀长 p:
  rMax = max 的第 p 位翻转为 0     (下半区上界)
  rMin = min 原样复制               (下半区下界)
  min  = min 的第 p 位翻转为 1     (this 原地变上半区)
  p    = p + 1
  返回新桶 rBucket = [rMin, rMax]  (下半区, 同样的 p)
  原 nodes_ 按是否落入 rBucket 分给两桶
```

分裂在「最高未定位位」（MSB-first 的第 p 位）上按 0/1 切开，上下两半各共享一个长 1 位的前缀。**分裂只允许发生在包含本地节点的桶上**（`splitAllowed`）——这正是「离自己越近分得越细」的全部机关。

#### 4.2.3 源码精读

- [src/DHTBucket.h:L54-L70](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.h#L54-L70)：`prefixLength_`、`min_`/`max_`、`localNode_`、`nodes_`（注释 *sorted in ascending order*）、`cachedNodes_`（替换缓存，按最近见到排序）、`lastUpdated_`。
- [src/DHTBucket.h:L84-L86](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.h#L84-L86)：`static const size_t K = 8;` 与 `CACHE_SIZE = 2;`——Kademlia 的 K 参数就定义在这里。
- [src/DHTBucket.cc:L65-L70](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.cc#L65-L70)：根桶构造——min 全 0、max 全 `0xff`、`prefixLength_` 0，即最初一个桶覆盖**整个** \(2^{160}\) 空间。
- [src/DHTBucket.cc:L96-L104](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.cc#L96-L104)：`isInRange` 用两次 `std::lexicographical_compare` 表达闭区间 `min <= nodeID <= max`——把 20 字节 ID 当 20 元组比字典序，「XOR 距离 → 区间划分」等价性的代码落点。
- [src/DHTBucket.cc:L106-L131](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.cc#L106-L131)：`addNode` 四条出路。注意 [L116-L120](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.cc#L116-L120)：只有**队首**（最久未见者）恰好是坏节点时才驱逐——「坏节点替换」的路径一。
- [src/DHTBucket.cc:L133-L140](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.cc#L133-L140)：`cacheNode` 把进不了正式名单的节点推入缓存头部，超 2 个截断。
- [src/DHTBucket.cc:L142-L152](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.cc#L142-L152)：`dropNode`——正式节点被摘除时若缓存非空，立刻把缓存队首递补进 `nodes_`。「坏节点替换」的路径二：**先递补、不留空位**。
- [src/DHTBucket.cc:L172-L175](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.cc#L172-L175)：`splitAllowed` 的两个条件——前缀长未达 159，且**本地节点落在本桶区间内**。
- [src/DHTBucket.cc:L177-L212](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.cc#L177-L212)：`split` 全过程。位翻转用 [src/bitfield.cc:L41-L47](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bitfield.cc#L41-L47) 的 `flipBit`：`data[bitIndex/8] ^= 128 >> (bitIndex%8)`，最高位算 bit 0（MSB-first），与前缀语义一致。分裂后原桶变上半区 `[翻转后的 min, 原 max]`，新桶是下半区 `[原 min, 翻转后的 max]`；两行 `A2_LOG_DEBUG` 打印新旧桶区间，是本节实践的观察点。
- [src/DHTBucket.cc:L214-L221](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.cc#L214-L221)：`getGoodNodes` 只过滤 `isBad()`——名字叫 good，实现是「非坏」，可疑节点照样返回。
- [src/DHTBucket.cc:L223-L238](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.cc#L223-L238)：`getNode` 按 (ID, IP, 端口) 三元组精确找节点——ID 相同但地址变了不算同一个「已登记」节点。
- [src/DHTBucket.cc:L246-L250](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.cc#L246-L250)：`needsRefresh`——桶未满，或距上次更新超过 `DHT_BUCKET_REFRESH_INTERVAL`（15 分钟，[src/DHTConstants.h:L57](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTConstants.h#L57)）。
- [src/DHTBucket.cc:L74-L84](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.cc#L74-L84)：`getRandomNodeID`——先全随机，再把前 `lastByteIndex+1` 字节覆盖成本桶 min 的对应字节（`lastByteIndex = (prefixLength_-1)/8`），得到一个落在本桶区间内的随机 ID。桶刷新靠它产生「往哪个方向找」的目标。

#### 4.2.4 代码实践

**实践目标**：用单元测试核对一次分裂的区间算术。

**操作步骤**：

1. 打开 [test/DHTBucketTest.cc:L182-L235](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DHTBucketTest.cc#L182-L235)（`testSplit`）。
2. 阅读第一组断言（L188-L214）：本地节点 ID 全 0，根桶 `[0x00…00, 0xff…ff]` 分裂后，返回的新桶应为 `[0x00…00, 0x7f…ff]` 且 `prefixLength == 1`；原桶变为 `[0x80…00, 0xff…ff]`。
3. 手算验证：`max = 0xff…` 第 0 位（MSB）是 1，翻转为 0 得 `0x7f…` 即 `rMax`；`min = 0x00…` 第 0 位翻转为 1 得 `0x80…` 即新 `min_`。
4. 再看第二组断言（L216-L235）：循环 159 次「断言可分裂 → 分裂」，循环外断言**不可再分裂**且最终桶区间为 `[0x00…00, 0x00…01]`、前缀 159。本地节点 ID 全 0，每次分裂后都落在返回的下半区桶里，所以能一路裂到底。

**需要观察的现象**：`expectedRMax = 0x7f…ff`、`expectedLMin = 0x80…00` 与 4.2.2 的伪代码逐行对应；159 次循环与 `splitAllowed` 的 `< 159` 上限精确吻合。

**预期结果**：能独立解释「为什么返回的是下半区而 this 保留上半区」（`min_` 原地翻转，`rMin` 用未翻转的副本），以及「为什么到前缀 159 就停」（再裂一次就是前缀 160 的单点区间，`prefixLength_ < 159` 的判断恰好挡住）。运行结果**待本地验证**（测试构建方法见 u9-l5）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `nodes_` 要维护「升序（最近见过在队尾）」？

**答案**：Kademlia 的经验法则是「老节点比新节点更可能稳定在线」，驱逐时永远优先赶走队首（最久未见者）。`addNode` 重插到队尾、`moveToTail`（响应验证后调用）都在维持这一顺序；配合「驱逐只看队首」和「只有含本地节点的桶能分裂」，共同实现「近处细粒度、远处粗粒度」的路由表形态。

**练习 2**：替补缓存为什么只有 2 个名额？

**答案**：替补缓存的作用是「桶满时先收留、空位出现时快速递补」，避免为每个被拒节点立刻发起质询。2 个名额在「记住候选」与「内存 / 维护成本」间取折中；超额时按「最近见过优先」截断（[src/DHTBucket.cc:L137-L139](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucket.cc#L137-L139)）。

**练习 3**：桶的区间是 `[min, max]` 闭区间且边界值可达，这与「前缀划分」等价吗？

**答案**：等价。前 p 位固定、其余位任取的 ID 集合，最小值是「前缀 + 全 0」、最大值是「前缀 + 全 1」，闭区间恰好覆盖且两端可达；反过来，min 与 max 共享 p 位前缀时，区间内任何 ID 的前 p 位都只能取该前缀（否则字典序会越界）。`isInRange` 的两次字典序比较就是这个边界的实现。

### 4.3 DHTBucketTree：路由二叉树与 K 近邻查找

#### 4.3.1 概念说明

桶会越裂越多，需要一个结构把它们组织起来并支持快速定位——`DHTBucketTreeNode` 构成的**二叉路由树**：

- **叶子持桶**（`bucket_` 非空），**内部节点只持区间**（`minId_`/`maxId_`，桶指针置空）；
- 左子树管小 ID、右子树管大 ID（分裂返回的下半区成为左孩子）；
- 树「高度不平衡」（类注释原文：*"The tree is binary tree but highly unbalanced"*，[src/DHTBucketTree.h:L50-L53](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.h#L50-L53)）。

更进一步，生产环境的路由树必然是**梳子形**：`splitAllowed` 要求桶包含本地节点，而树上的分裂只发生在 `DHTRoutingTable::addNode` 的循环里（`DHTBucketTreeNode::split` 在 src/ 下仅此一个调用点，[src/DHTRoutingTable.cc:L92](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTable.cc#L92)）。所以内部节点全部落在「根 → 本地节点所在叶子」这一条脊柱上，脊柱上每个内部节点的另一个孩子永远是没分裂过的叶子。这个不变式是理解 `collectUpward`（见下）正确性的钥匙。

本模块讲三个操作：`findTreeNodeFor`/`findBucketFor`（一个 ID 属于哪个桶）、`findClosestKNodes`（离一个 ID 最近的至多 K 个非坏节点——DHT 查询的元操作）、`enumerateBucket`（遍历所有桶）。

#### 4.3.2 核心流程

**定位（dig）**：从根开始，每个内部节点比较 key 与左孩子区间，落左走左、否则走右，直到叶子：

```text
findTreeNodeFor(root, key):
  while root 不是叶子:
    root = (key 在左孩子区间内) ? left : right
  return root        // key 所属桶所在的叶子
```

**K 近邻查找（findClosestKNodes）**，头注释声明结果「顺序任意」、可能不足 K 个：

```text
findClosestKNodes(nodes, root, key):
  leaf = findTreeNodeFor(root, key)
  if leaf == root: 收集该桶非坏节点, 结束
  parent = leaf 的父节点
  # 第一阶段: 向下。以 parent 为子树, 同侧优先
  if leaf 是 parent 的左孩子: collectDownwardLeftFirst(parent)
  else:                       collectDownwardRightFirst(parent)
  #   (先收 leaf 所在桶, 凑不满 K 再收兄弟子树更近的一侧)
  # 第二阶段: 向上。仍不满 K 时沿父链逐层收"叔叔子树"
  if nodes.size() < K: collectUpward(parent)
  截断到 K 个
```

「同侧优先」+「先下后上」保证**先收区间离 key 最近的桶，逐步向远处扩展**——Kademlia「返回离目标最近的 K 个节点」的近似实现，且不对候选做任何距离排序（排序责任留给调用方）。

**遍历（enumerateBucket）**：中序递归收集所有叶子的桶，供保存与刷新使用。

#### 4.3.3 源码精读

- [src/DHTBucketTree.cc:L53-L58](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.cc#L53-L58)：叶子构造——从桶复制 `minId_`/`maxId_`。
- [src/DHTBucketTree.cc:L62-L68](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.cc#L62-L68)：`resetRelation`——内部节点接管两个孩子：设父指针，令 `minId_ = 左孩子 min`、`maxId_ = 右孩子 max`。左右不变式在此确立。
- [src/DHTBucketTree.cc:L70-L81](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.cc#L70-L81)：`dig`——叶子返回空，内部节点按「key 是否在左孩子区间」二选一下降。
- [src/DHTBucketTree.cc:L83-L89](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.cc#L83-L89)：节点级 `isInRange`，同样是两次字典序比较。
- [src/DHTBucketTree.cc:L91-L97](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.cc#L91-L97)：`DHTBucketTreeNode::split`——调 `DHTBucket::split()`，**返回的下半区新桶成为左孩子**、原桶（上半区）成为右孩子，然后 `bucket_.reset()` 使自己退化为内部节点。桶与树唯一的接缝。
- [src/DHTBucketTree.cc:L101-L110](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.cc#L101-L110)：`findTreeNodeFor` 递归下降。
- [src/DHTBucketTree.cc:L112-L117](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.cc#L112-L117)：`findBucketFor` = `findTreeNodeFor(...)->getBucket()`。
- [src/DHTBucketTree.cc:L119-L127](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.cc#L119-L127)：`collectNodes`——对桶调 `getGoodNodes` 后追加（再次强调：过滤的是 bad，不是 questionable）。
- [src/DHTBucketTree.cc:L130-L143](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.cc#L130-L143) 与 [L146-L159](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.cc#L146-L159)：左优先 / 右优先两种向下收集——先走目标所在一侧，凑不满 K 再走另一侧，对内部节点正确递归。
- [src/DHTBucketTree.cc:L161-L182](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.cc#L161-L182)：`collectUpward`——沿父链向上，每层收集「叔叔」的桶，凑满 K 即停。注意它对叔叔直接 `getBucket()` 而没有先检查 `leaf()`——**能这样写靠的正是梳子不变式**：生产树上内部节点全在脊柱上，脊柱节点的兄弟必是叶子，所以叔叔永远是叶子、`getBucket()` 不会拿到空指针。（测试会手工拼出非梳子树，见 4.3.4。）
- [src/DHTBucketTree.cc:L184-L210](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.cc#L184-L210)：`findClosestKNodes` 主体——先下后上两阶段，最后截断到 K。头注释（[src/DHTBucketTree.h:L108-L113](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.h#L108-L113)）明确 *"The order of nodes is arbitrary"*。
- [src/DHTBucketTree.cc:L212-L222](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketTree.cc#L212-L222)：`enumerateBucket` 中序遍历。

调用方（谁在用 K 近邻）：

- [src/DHTFindNodeMessage.cc:L65](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTFindNodeMessage.cc#L65) 与 [src/DHTGetPeersMessage.cc:L123](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTGetPeersMessage.cc#L123)：收到远端 find_node / get_peers **查询**时，用 `getClosestKNodes` 找出本地所知最近节点放进应答——路由表的价值出口。
- [src/DHTNodeLookupTask.cc:L57](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTNodeLookupTask.cc#L57)、[src/DHTPeerLookupTask.cc:L70](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTPeerLookupTask.cc#L70)：迭代查找任务消费**应答**里返回的节点，继续下一轮查询（u7-l3 展开）。

#### 4.3.4 代码实践

**实践目标**：在一张手工构造的树上核对 `findClosestKNodes` 的「先下后上」收集顺序。

**操作步骤**：

1. 打开 [test/DHTBucketTreeTest.cc:L109-L176](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DHTBucketTreeTest.cc#L109-L176)（`testFindClosestKNodes`）。
2. 照 L116-L120 的连续分裂画出五个桶的区间（本地节点 ID = `0xaa…`）：
   `bucket2=[00…,7f…]`、`bucket4=[80…,af…]`、`bucket5=[b0…,b7…]`、`bucket3=[b8…,bf…]`、`bucket1=[c0…,ff…]`；再按 L124-L133 用 `make_unique<DHTBucketTreeNode>(left, right)` 拼出 4 层的树（注意这是**手工拼的非梳子树**，生产树长不出来）。每个桶用 `getRandomNodeID` 生成 2 个节点填入（L135-L146）。
3. 跟踪第一组断言（L147-L161）：`targetID = 0x80…` 应依次从 bucket4、bucket5、bucket3、bucket1 各收 2 个，共 8 个。
4. 对照 4.3.2 的两阶段伪代码解释顺序：target 落在 bucket4 → 向下（bucket4 → bucket5 → bucket3）只凑到 6 个 → 向上收叔叔 bucket1 的 2 个，恰好 8 个截断。

**需要观察的现象**：`nodes[0..1] ∈ bucket4`、`nodes[2..3] ∈ bucket5`、`nodes[4..5] ∈ bucket3`、`nodes[6..7] ∈ bucket1` 的断言顺序与「同侧优先、先下后上」一一对应；第二组 `targetID = 0xf0…`（L162-L176）验证从另一侧进入时的对称行为。

**预期结果**：能不看解析说出 8 个节点各自的来源桶。运行结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`findClosestKNodes` 为什么不按距离排序就返回？

**答案**：协议场景只需要「一组足够近的候选」：应答方把节点列表原样发回，查询方（NodeLookupTask）在本地按目标距离挑下一跳。省掉排序降低应答路径开销，与头注释 *The order of nodes is arbitrary* 一致。

**练习 2**：`dig` 在叶子上返回 `nullptr`，`findTreeNodeFor` 为什么不会踩到？

**答案**：`findTreeNodeFor` 先判 `root->leaf()`，是叶子直接返回，只在内部节点上调 `dig`。`dig` 的空返回值语义是「已到底」，供其他调用方区分节点类型（测试 `testDig` 专门验证这一点）。

**练习 3**：既然 `collectUpward` 依赖「叔叔必是叶子」，这个前提在什么情况下会被打破？

**答案**：只有在树上出现「不在本地节点脊柱上的内部节点」时才会打破，而生产代码里 `DHTBucketTreeNode::split` 只被 `DHTRoutingTable::addNode` 调用、且仅当桶包含本地节点（`splitAllowed`）才裂，所以打破不了；单元测试手工拼树（4.3.4）就是唯一的例外场景。这是「不变式由调用方保证」的典型例子——单独读 `DHTBucketTree.cc` 推不出正确性，必须连同 `DHTRoutingTable` 一起读。

### 4.4 DHTRoutingTable 与 DHTRegistry：门面、双栈与持久化

#### 4.4.1 概念说明

**DHTRoutingTable** 是路由树的拥有者与对外门面：持根节点 `root_` 与本地节点引用，把 4.2/4.3 的原语组合成三组上层能力——`addNode`（含「满则分裂再入」循环）、`getClosestKNodes`/`getBucketFor`（查找）、`getBuckets`（遍历，供保存与刷新）。桶计数 `numBucket_` 也在这里。

**DHTRegistry** 是**进程级静态注册表**：两个静态 `Data`（`data_` 与 `data6_`）分别保存 IPv4 与 IPv6 各一整套 DHT 组件——本地节点、路由表、任务队列 / 工厂、peer 宣告存储、token 跟踪器、消息分发 / 接收 / 工厂，共九件。两套完全独立，因为 **IPv4 与 IPv6 是两个物理隔离的 DHT 网络**：同一节点在两张网里的 ID、邻居、可达性都不同，混在一张表里会出现「向 IPv6-only 节点发 IPv4 查询」这种根本无法送达的路由。

路由表还要跨进程存活：aria2 每 30 分钟及停机时把它序列化到 `dht.dat`（IPv6 版 `dht6.dat`），下次启动反序列化重建，避免每次从 entry point 白手起家。

#### 4.4.2 核心流程

**addNode 的分裂循环**（门面层最核心的逻辑）：

```text
addNode(node, good):
  if node.ID == 本地 ID: 拒绝
  treeNode = findTreeNodeFor(root, node.ID)
  loop:
    bucket = treeNode 的桶
    if bucket.addNode(node) 成功: return true
    elif bucket.splitAllowed():          # 桶满但含本地节点, 可裂
      treeNode.split(); numBucket_++
      treeNode = node.ID 所落的那一侧孩子  # 下降一层再试
    else:                                # 桶满且不可裂
      if good: bucket.cacheNode(node)   # 验证过的好节点进替补缓存
      return false
```

**坏节点出表的完整触发链**（本讲学习目标的重点）：

```text
路径A(主动): 发出的查询超时(DHTMessageTracker) -> node.timeout() 计数+1
             -> 累计 >= 5 次 isBad() -> routingTable.dropNode(node)
                -> bucket.dropNode: 从 nodes_ 摘除 + 替补缓存队首递补
路径B(被动): 桶满时新节点入桶(bucket->addNode)
             -> 队首(最旧)节点 isBad()? -> 驱逐队首, 新节点顶上
路径C(登记): 收到有效报文(DHTMessageReceiver)
             -> markGood() + updateLastContact() + addGoodNode()
             -> 桶满且不可裂时进替补缓存, 等空位出现递补
遗留路径:   DHTReplaceNodeTask -- 对桶内 LRU 可疑节点发 ping,
             两次超时则 markBad() 并让新节点顶替; 但其工厂方法
             createReplaceNodeTask 在 src/ 下没有调用点, 现行
             版本不会触发, 可疑节点靠路径 A/B 与桶刷新自然淘汰
```

**桶刷新**（防止桶里节点悄悄失联）：

```text
DHTBucketRefreshCommand (每 5 分钟, TimeBasedCommand)
  └─> 创建 DHTBucketRefreshTask 入任务队列
        └─> 遍历所有桶: needsRefresh()? (未满 或 超 15 分钟未更新)
              ├─ notifyUpdate() 重置时钟
              ├─ getRandomNodeID(targetID)   # 桶区间内随机目标
              └─ 创建 DHTNodeLookupTask(targetID)  # 迭代查找, 顺路认识新节点
```

**持久化**：

```text
保存: DHTAutoSaveCommand(每 30 分钟) 或 停机(preProcess 检测到停机/完成)
        -> getBuckets() 枚举全部桶 -> 每桶 getGoodNodes() 汇总(坏节点不存)
        -> DHTRoutingTableSerializer.serialize(dht.dat)
        -> 先写 dht.dat__temp 再原子改名

加载: DHTSetup(第一个 BT 任务创建初始命令时)
        -> DHTRoutingTableDeserializer 反序列化出 localNode + 节点列表
        -> 新建空路由表, 逐个 addNode 重新长出分桶形状
        -> 若有旧节点: 创建 forceRefresh 的桶刷新任务立即验证
```

#### 4.4.3 源码精读

**DHTRoutingTable**：

- [src/DHTRoutingTable.cc:L52-L60](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTable.cc#L52-L60)：构造——根即一个覆盖全 ID 空间的根桶（4.2 节的全 0 / 全 ff 桶），`numBucket_` 从 1 起步。
- [src/DHTRoutingTable.cc:L64-L72](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTable.cc#L64-L72)：公开入口 `addNode` / `addGoodNode` 只是给 `good` 标志赋默认值的薄封装。
- [src/DHTRoutingTable.cc:L74-L110](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTable.cc#L74-L110)：私有 `addNode`——先拒绝与本地同 ID 的节点（[L77-L80](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTable.cc#L77-L80)），然后进「入桶失败 → 分裂 → 下降」的 while 循环；`good` 为 true 时失败兜底进替补缓存（[L101-L107](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTable.cc#L101-L107)）。
- [src/DHTRoutingTable.cc:L112-L117](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTable.cc#L112-L117)：`getClosestKNodes` 一行转调 4.3 节的 `dht::findClosestKNodes`。
- [src/DHTRoutingTable.cc:L132-L150](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTable.cc#L132-L150)：`getBucketFor` / `getNode`——按 ID 定位桶，再按 (ID, IP, 端口) 三元组找节点。
- [src/DHTRoutingTable.cc:L152-L155](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTable.cc#L152-L155)：`dropNode` 转调桶的摘除加递补。
- [src/DHTRoutingTable.cc:L167-L171](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTable.cc#L167-L171)：`getBuckets` 中序枚举全部桶，保存与刷新都用它。

**DHTRegistry**：

- [src/DHTRegistry.h:L56-L81](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRegistry.h#L56-L81)：内部结构 `Data`（localNode、routingTable、taskQueue、taskFactory、peerAnnounceStorage、tokenTracker、messageDispatcher / Receiver / Factory 九件套）与两个静态实例 `data_`、`data6_`。
- [src/DHTRegistry.h:L88-L106](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRegistry.h#L88-L106)：IPv4 用 `getData`/`isInitialized`，IPv6 用 `getData6`/`isInitialized6`，两套 API 完全镜像。
- [src/DHTRegistry.cc:L49-L69](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRegistry.cc#L49-L69)：两个静态实例的定义与 `clear` 的逐字段 reset（初始化失败时整体回滚用）。

**谁创建这一切**：

- [src/RequestGroup.cc:L350-L373](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L350-L373)：DHT **并非进程启动即初始化**，而是在第一个 BT 任务的 `createInitialCommand`（函数起于 [L271](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L271)）里按 `--enable-dht`（默认 true）/ `--enable-dht6`（默认 false，选项定义见 [src/OptionHandlerFactory.cc:L1768-L1782](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L1768-L1782)）分别调 `DHTSetup().setup(e, AF_INET)` / `(e, AF_INET6)`。纯 HTTP 下载不会启动 DHT。
- [src/DHTSetup.cc:L85-L93](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L85-L93)：幂等闸门——family 非法，或对应栈已初始化，直接返回。
- [src/DHTSetup.cc:L96-L113](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L96-L113)：先反序列化 dht.dat 拿 localNode（**保留旧 ID，路由历史得以延续**）；失败则新建随机 ID 节点。
- [src/DHTSetup.cc:L143](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L143)：`make_unique<DHTRoutingTable>(localNode)`——每栈一张表。
- [src/DHTSetup.cc:L155-L183](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L155-L183)：九件套之间的手工依赖注入（互设指针）。
- [src/DHTSetup.cc:L226-L246](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L226-L246)：注册 `DHTBucketRefreshCommand`（每 `DHT_BUCKET_REFRESH_CHECK_INTERVAL` = 5 分钟，[src/DHTConstants.h:L59](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTConstants.h#L59)）与 `DHTAutoSaveCommand`（30 分钟）。
- [src/DHTSetup.cc:L247-L257](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L247-L257)：把反序列化出的旧节点逐个 `addNode` **重新长出**分桶形状；只要旧表非空，就入队一个 `setForceRefresh(true)` 的桶刷新任务，立即验证这些「隔夜节点」是否还活着。
- [src/DHTSetup.cc:L258-L285](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L258-L285)：按 family 把整套对象搬进 `DHTRegistry::getMutableData()`（IPv4）或 `getMutableData6()`（IPv6）并置 initialized——双栈设计在这里落地。
- [src/DHTSetup.cc:L291-L305](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTSetup.cc#L291-L305)：任一环节抛异常则清空临时命令并 `clearData()`/`clearData6()` 整体回滚——**DHT 初始化失败只禁用 DHT，不影响下载本身**。

**刷新与超时（4.4.2 两条链的代码落点）**：

- [src/DHTBucketRefreshCommand.cc:L64-L67](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketRefreshCommand.cc#L64-L67)：每次到点创建一个桶刷新任务。
- [src/DHTBucketRefreshTask.cc:L54-L77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTBucketRefreshTask.cc#L54-L77)：`needsRefresh` 过滤 → `notifyUpdate` 重置时钟 → `getRandomNodeID` 生成桶内随机目标 → 创建 `DHTNodeLookupTask`。
- [src/DHTMessageTracker.cc:L127-L132](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageTracker.cc#L127-L132)：路径 A 的出口——超时计数与 `isBad → dropNode`。
- [src/DHTReplaceNodeTask.cc:L63-L103](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTReplaceNodeTask.cc#L63-L103)：遗留路径——对 LRU 可疑节点发 ping，两次超时（`MAX_RETRY = 2`）则 `markBad()` 并 `addNode` 顶替；但 `createReplaceNodeTask`（[src/DHTTaskFactoryImpl.cc:L107](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTTaskFactoryImpl.cc#L107)）在 src/ 下没有调用点。

**dht.dat 的二进制格式**（综合实践的依据）：

- [src/DHTAutoSaveCommand.cc:L70-L79](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTAutoSaveCommand.cc#L70-L79)：`preProcess` 检测到全部下载完成或停机请求就保存——这就是「Ctrl-C 也会落盘」的原因；[L81-L116](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTAutoSaveCommand.cc#L81-L116) 的 `save()`：枚举全部桶 → 每桶 `getGoodNodes` → 交序列化器。
- [src/DHTRoutingTableSerializer.cc:L90-L99](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTableSerializer.cc#L90-L99)：8 字节头——魔数 `a1 a2`、格式 ID `02`、3 字节保留、版本 `00 03`（即 `DHT_VERSION`，[src/DHTConstants.h:L46](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTConstants.h#L46)）。
- [src/DHTRoutingTableSerializer.cc:L104-L121](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTableSerializer.cc#L104-L121)：8 字节保存时间（epoch 秒，网络字节序）→ 8 字节保留 + 20 字节本地节点 ID + 4 字节保留 → 4 字节节点数（大端）+ 4 字节保留。
- [src/DHTRoutingTableSerializer.cc:L123-L149](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTableSerializer.cc#L123-L149)：每个节点定长 56 字节——1 字节紧凑地址长度（`bittorrent::getCompactLength`：IPv4 为 6、IPv6 为 18，[src/bittorrent_helper.cc:L1016-L1025](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L1016-L1025)，常量 `COMPACT_LEN_IPV4/6` 见 [src/BtConstants.h:L62-L64](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtConstants.h#L62-L64)）+ 7 字节保留 + 紧凑地址（IP + 2 字节大端端口）+ 补足 24 字节的保留区 + 20 字节节点 ID + 4 字节保留。注意 **dht.dat 不存桶结构**，只存扁平节点列表；桶形状在加载时由 `addNode` 重新长出（[src/DHTRoutingTableDeserializer.cc:L77-L202](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTableDeserializer.cc#L77-L202) 按相同格式读回，并兼容旧版本 2 的头，地址打包失败的条目直接跳过）。
- [src/DHTRoutingTableSerializer.cc:L150-L157](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTRoutingTableSerializer.cc#L150-L157)：先写 `dht.dat__temp` 再 `renameTo` 原子替换——崩溃不会留下半截文件。
- 文件路径默认值：[src/OptionHandlerFactory.cc:L1725-L1738](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L1725-L1738) 注册 `--dht-file-path` / `--dht-file-path6`，默认值来自 [src/util.cc:L1816-L1825](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L1816-L1825) 的 `getDHTFile`：`$HOME/.aria2/dht.dat`，不存在则用 `$XDG_CACHE_HOME/aria2/dht.dat`。

#### 4.4.4 代码实践

**实践目标**：核对序列化器的字节布局，为综合实践的手工解析做准备。

**操作步骤**：

1. 打开 [test/DHTRoutingTableSerializerTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DHTRoutingTableSerializerTest.cc) 阅读 `testSerialize`：它把本地节点 + 若干远端节点序列化到临时文件，再按字段把文件**读回**逐段比对（魔数、保留区、本地 ID、节点数）。
2. 列出每段字节与 4.4.3 布局的对应关系（头 8B、时间 8B、本地 ID 段 8+20+4=32B、计数段 4+4=8B、每节点 56B）。
3. 回答：一个含 100 个好节点、IPv4 栈的 dht.dat 应该多大？

**需要观察的现象**：总长度 = 8 + 8 + 32 + 8 + 100 × 56 = 5656 字节。

**预期结果**：能推出通用公式 `文件大小 = 56 × (节点数 + 1)`——头部与本地节点段合计恰好也是 56 字节（8+8+8+20+4+4+4 = 56）。这一不变式是综合实践的校验手段。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 dht.dat 不保存桶（区间）信息，只存扁平节点列表？

**答案**：桶形状是确定性的：由本地节点 ID 与节点 ID 集合完全决定——按相同语义 `addNode`，分裂路径必然重现（`splitAllowed` 只依赖「本地节点是否在区间内」，驱逐只依赖健康度）。保存派生数据反而要处理「加载后桶对不上」的一致性问题。典型的「存状态、不存结构」。

**练习 2**：DHTRegistry 用静态成员保存两套 `Data`，而不像 BtRegistry 那样挂在 DownloadEngine 上，有什么取舍？

**答案**：DHT 是进程级单例资源：UDP socket、端口绑定、路由表跨任务共享，生命周期长于任何 RequestGroup；BtRegistry 的六件套（u6-l2）是每种子一份、随引擎生灭。静态存储让 `DHTSetup` 的幂等闸门（`isInitialized`/`isInitialized6`）可在任意 BT 任务触发时检查「是否已装好」。代价是静态状态要靠 `clearData` 手工清理。

**练习 3**：`addNode(node, good=true)` 与 `good=false` 的区别何时体现？

**答案**：只在目标桶已满且不允许分裂（不含本地节点）时体现：`good=true`（来自有效报文的发送者，`addGoodNode` 路径）进替补缓存等待递补；`good=false` 直接丢弃。「验证过的好节点」才值得留替补。

## 5. 综合实践

**任务**：跑一次真实 DHT 会话拿到 `dht.dat`，对照 `DHTRoutingTableSerializer` 的格式手工解析出节点数量与前几个节点的 ID，再用「与本地 ID 的共享前缀位数」验证分桶的空间不均匀性。

### 步骤 1：产生 dht.dat

```console
$ rm -f ~/.aria2/dht.dat ~/.cache/aria2/dht.dat
$ ./src/aria2c --enable-dht --dht-entry-point=<HOST:PORT> \
    --dht-listen-port=6881-6999 -l=dht.log --log-level=debug some.torrent
# 等待几分钟让路由表积累节点，然后按一次 Ctrl-C 优雅停机
$ ls -l ~/.aria2/dht.dat ~/.cache/aria2/dht.dat 2>/dev/null
```

要点（都有 4.4 节的代码依据）：

- 必须有 **BT 任务**才触发 DHTSetup（`RequestGroup::createInitialCommand` 的 BT 分支）；
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

1. **节点数与尺寸互证**：步骤 2 的 N 与 `56×(N+1)` 尺寸不变式必须同时成立——这是「你已读懂序列化格式」的硬校验。
2. **空间不均匀性**：统计各节点的共享前缀位数分布。由于只有包含本地 ID 的桶才会分裂、每桶至多 8 个节点，前缀位数高的桶越深，说明那一侧分得越细。分布规律有个可验证的结构特征：**不含本地 ID 的桶，其全部节点与本地 ID 的共享前缀位数完全相同**（等于该桶的前缀长度，因为它们正是在脊柱的某次分裂处与本地 ID 分道扬镳），且每档至多 8 个；唯一的例外是包含本地 ID 的那个桶——桶内节点与本地 ID 的共享前缀可能超过桶前缀、彼此参差。抽查前几个节点：按 4.2 节 `isInRange` 的语义手工确认，与本地 ID 共享 \(p\) 位前缀的节点，必然落在某个前缀长度 \(\le p\) 的桶区间内。

### 预期结果与验收

- 头 8 字节、`56×(N+1)` 尺寸不变式、本地 ID 与日志一致——三项全部通过即证明格式读懂了；
- 直方图中「同前缀档位 ≤ 8 个节点」且高前缀档位有节点，验证 K 桶划分与「离自己越近分得越细」。

本实践依赖真实 DHT 网络可达性（UDP 出网、entry point 可用），所有观察值**待本地验证**；若网络受限，可退化为纯阅读实践：对照 `test/DHTRoutingTableSerializerTest.cc` 与 `test/DHTRoutingTableDeserializerTest.cc` 在纸上完成同样的字节布局核对。

## 6. 本讲小结

- **DHTNode** 是节点的纯数据表示：20 字节 ID + 地址 + RTT + 健康度计数器 `condition_`（≥5 次超时即 bad）+ 最近联系时间（超 15 分钟未联系为 questionable）；`markGood`/`timeout` 分别由有效报文与消息超时驱动。
- **DHTBucket** 用字典序闭区间 `[min,max]` 实现 Kademlia 的前缀 K 桶：每桶至多 K=8 个正式节点 + 2 个替补缓存；`split` 在 `prefixLength` 位上翻转切开区间，且只有包含本地节点的桶允许分裂（上限前缀 159）——「离自己越近分得越细」的全部机关在 `splitAllowed` 的一个条件里。
- **DHTBucketTree** 是叶子持桶的高度不平衡二叉树，生产环境必然呈「梳子形」（内部节点全在本地节点脊柱上）；`findClosestKNodes` 用「先同侧向下、后沿父链向上」两阶段收集至多 8 个非坏节点，结果不排序，排序责任留给调用方。
- **DHTRoutingTable** 的 `addNode` 循环是「入桶失败→分裂→下降重试」；坏节点出表有三条现实路径（超时计数出表、桶满驱逐坏队首、替补缓存递补），`DHTReplaceNodeTask` 是无调用点的遗留路径。
- **DHTRegistry** 以两个静态 `Data` 保存 IPv4/IPv6 完全独立的九件套；两栈两张表，因为它们是物理隔离的两个 DHT 网络。
- **dht.dat** 只存「本地 ID + 扁平的好节点列表」（定长 56 字节/节点，文件大小恒为 56×(N+1)），桶形状在加载时由 `addNode` 确定性重建，保存走临时文件加原子改名。

## 7. 下一步学习建议

本讲只回答了「路由表长什么样、怎么维护」；DHT 真正转起来靠三件事，正好是本单元接下来的三讲：

1. **u7-l2 DHT 报文**：bencode 编码的 ping / find_node / get_peers / announce_peer 查询与响应——你会看到 `DHTMessageTracker`（本讲健康度惩罚的数据源）如何用 transaction id 配对请求响应。
2. **u7-l3 DHT 任务系统**：`DHTTaskQueue`/`DHTTaskExecutor` 如何调度本讲反复出现的 `DHTNodeLookupTask` 与桶刷新任务，以及迭代查找的收敛过程。
3. **u7-l4 DHT 启动与周期命令**：`DHTSetup` 的完整装配清单、entry point 解析与自举、`DHTBucketRefreshCommand`/`DHTAutoSaveCommand` 两个周期命令的协作全貌。

建议顺序：先 u7-l2（有了报文语义，任务系统才看得懂），再 u7-l3、u7-l4。若想继续深挖路由表本身，可读 `test/DHTRoutingTableTest.cc` 中针对 addNode 分裂循环的用例，以及 Kademlia 原论文（Maymounkov & Mazières, 2002）第 2~4 节——对照本讲的 `splitAllowed` 实现体会工程化取舍。
