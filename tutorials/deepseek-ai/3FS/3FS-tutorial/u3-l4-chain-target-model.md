# ChainTable / Chain / Target 数据模型

## 1. 本讲目标

本讲聚焦 mgmtd（集群管理服务）中最核心的一组数据结构：**链表（ChainTable）、链（Chain）、存储目标（Target）**。学完本讲，你应当能够：

- 说清 ChainTable、Chain、Target 三者的层级关系与各自的字段含义。
- 看懂 RoutingInfo 是如何用四张映射表（NodeMap / ChainTableMap / ChainMap / TargetMap）把它们组织起来的。
- 解释三级单调递增的版本号（`routingInfoVersion` / `chainTableVersion` / `chainVersion`）各自管什么、为什么需要它们。
- 理解一个文件如何通过 `ChainRef` 钉住某张链表的某个历史版本，从而在链表变更期间仍能正确定位数据。
- 说明为什么要支持「多张链表」，以及多表如何用互斥的节点/SSD 隔离不同工作负载。

本讲只讲「数据结构与版本语义」，**不讲**状态机转换（那是 u3-l5）、**不讲**建链命令与放置算法（那是 u8-l1）。我们只回答一个问题：mgmtd 眼中的集群拓扑，到底长什么样。

## 2. 前置知识

在进入本讲前，你需要已经建立以下认知（来自 u1 与 u2、u3-l1）：

- **CRAQ 链式复制**：3FS 把每个文件切成等长 chunk，每个 chunk 复制成一条「链」，链头负责写、沿链传播，链上任意 target 都可读。详见 `docs/design_notes.md`。
- **四大组件与端到端链路**：mgmtd 是「集群发现中枢」，维护全局拓扑并下发给所有进程；client 向 meta 取得文件布局后，自己算出 chunk id 与所属链，直连 storage 读写。
- **flat 命名空间**：本讲的 `ChainTable`、`ChainInfo`、`TargetInfo` 都在 `hf3fs::flat` 命名空间下。`flat` 指的是「投影后下发到线上的扁平结构」，区别于 mgmtd 内部内存中 richer 的工作版 `mgmtd::RoutingInfo`（见 u3-l1）。
- **serde 序列化**：这些结构都继承 `serde::SerdeHelper<...>`，用 `SERDE_STRUCT_FIELD` 一行声明字段、默认值与反射元信息（见 u2-l2）。
- **StrongType**：下面大量出现的 `ChainId`、`TargetId` 等都是 `STRONG_TYPEDEF` 生成的强类型整数，避免不同 id 之间互相赋值出错。

一个直觉性的比喻：把整个集群想象成一个大仓库。

- **Node（节点）**= 一台物理机（一台 storage 服务进程）。
- **Target（存储目标）**= 一台机器上的一块 SSD 上划出来的一段存储资源（一个 storage target）。一台机器有多块 SSD，每块 SSD 上又能建多个 target。
- **Chain（链）**= 由若干 target 串成的一条复制链（CRAQ），同一条链上的 target 互为副本。
- **ChainTable（链表）**= 一张「可选链的清单 + 顺序」，元数据服务从中按 round-robin 给新文件挑链。

本讲就把这四个概念在源码里的样子看清楚。

## 3. 本讲源码地图

本讲涉及的关键文件全部位于 `src/fbs/mgmtd/`，这是一个由 X-macro 风格手写 C++ + 宏构成的「schema」目录（详见 u2-l2）。

| 文件 | 作用 |
| :--- | :--- |
| `src/fbs/mgmtd/MgmtdTypes.h` | 定义全部强类型 id 与两个状态枚举（`PublicTargetState` / `LocalTargetState`），是理解本讲所有结构的「字典」。 |
| `src/fbs/mgmtd/ChainTable.h` | 链表结构 `ChainTable`（链表 id、链表版本、链 id 列表、描述）。 |
| `src/fbs/mgmtd/ChainInfo.h` | 链结构 `ChainInfo`（链 id、链版本、target 列表、偏好读顺序）。 |
| `src/fbs/mgmtd/ChainTargetInfo.h` | 链内 target 的精简视图 `ChainTargetInfo`（target id + public 状态）。 |
| `src/fbs/mgmtd/TargetInfo.h` | 完整 target 结构 `TargetInfo`（含 public/local 双状态、所属节点、磁盘、已用空间）。 |
| `src/fbs/mgmtd/ChainRef.h` | 「链引用」`ChainRef`，把（表 id、表版本、链序号）三元组编码成一个引用，是文件布局定位链的关键。 |
| `src/fbs/mgmtd/RoutingInfo.h` / `RoutingInfo.cc` | 线上路由信息 `flat::RoutingInfo`，用四张映射表把上述结构装在一起，并提供一组 getter。 |
| `docs/design_notes.md` | 6 节点示例链表、public 状态语义表，是本讲实践的依据。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**链表结构**、**版本语义**、**多表隔离**。

### 4.1 链表结构：ChainTable / Chain / Target 的三层关系

#### 4.1.1 概念说明

3FS 的集群拓扑可以抽象成一个三层图：

```
ChainTable（链表：一张"可选链清单 + 顺序"）
   │  chains: [chainId_1, chainId_2, ..., chainId_N]
   │
   ├──► Chain（链：一条 CRAQ 复制链）
   │      │  targets: [ChainTargetInfo(head), ..., ChainTargetInfo(tail)]
   │      │
   │      └──► ChainTargetInfo（链内 target 精简视图）
   │              │  targetId + publicState
   │              ▼
   │           TargetInfo（完整 target）
   │              │  nodeId 反查 ──► NodeInfo（所在机器）
   │              │  diskIndex     ──► 所在 SSD
   │              │  publicState / localState
   └──► （同一条 Chain 可被多张 ChainTable 引用）
```

需要特别强调几个「反直觉」的点，它们是后续理解的基础：

1. **ChainTable 不直接持有 target，只持有一串 `ChainId`**。链表只回答「这张表里有哪些链、按什么顺序排」，至于每条链内部有几个 target、状态如何，要再去 `ChainMap` 里查 `ChainInfo`。这是一种「间接引用」设计。
2. **Chain 与 ChainTable 是多对多关系**。一条链可以被多张链表引用（见 `docs/design_notes.md` 所述 "Each chain can be included in multiple chain tables"）。所以 `ChainTable::chains` 存的是 `ChainId` 而不是 `Chain` 本体。
3. **Target 在两个地方出现，详略不同**。在 `ChainInfo::targets` 里，每个 target 只用 `ChainTargetInfo`（targetId + publicState）这种精简视图；而完整的 `TargetInfo`（含 nodeId、diskIndex、双状态、usedSize）单独存在 `TargetMap` 里，靠 `targetId` 关联。这样链信息紧凑、节点定位信息按需查。
4. **Target 经 `nodeId` 反查 Node**。`TargetInfo` 里的 `nodeId` 指向 `NodeInfo`，告诉你这块 target 物理上在哪台机器、能从哪些地址连上去。

#### 4.1.2 核心流程

给定一个 `targetId`，要在 `RoutingInfo` 里定位它「属于哪台机器、在哪条链、链上排第几」，大致流程是：

1. `RoutingInfo::getTarget(targetId)` → 在 `TargetMap` 中拿到 `TargetInfo`，读出 `nodeId`、`chainId`、`publicState`。
2. 由 `chainId` 调 `getChain(chainId)` → 在 `ChainMap` 中拿到 `ChainInfo`，遍历其 `targets` 即可知该 target 在链中的位置（head/successor/tail）。
3. 由 `nodeId` 调 `getNode(nodeId)` → 在 `NodeMap` 中拿到 `NodeInfo`，得到机器地址。

反过来，给定一个 `ChainRef`（文件布局里存的就是它），定位到具体链：

1. `ChainRef` 解码出 `(chainTableId, chainTableVersion, chainIndex)`。
2. `getChainTable(tableId, tableVersion)` → 在 `ChainTableMap` 中拿到那张（特定版本的）`ChainTable`。
3. 用 `chainIndex` 在 `ChainTable::chains` 里取下标 → 得到 `ChainId`（见 4.2 的取模规则）。
4. `getChain(chainId)` → 拿到 `ChainInfo`，即可读写。

#### 4.1.3 源码精读

先看「字典」——所有 id 与状态枚举都在 `MgmtdTypes.h`：

[src/fbs/mgmtd/MgmtdTypes.h:10-28](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/MgmtdTypes.h#L10-L28) 定义了 target 的两套状态。`PublicTargetState` 是「对外公开、随链表下发」的状态（SERVING/LASTSRV/SYNCING/WAITING/OFFLINE），决定能否读/写；`LocalTargetState` 是「只有 storage 与 mgmtd 知道、存于 mgmtd 内存」的本机状态（UPTODATE/ONLINE/OFFLINE），充当状态机的触发事件。本讲只需要记住：**`ChainTargetInfo` 里携带的就是 `PublicTargetState`**，它是会随链表一起被分发给 client/storage/meta 的。

注意枚举值是 `1, 2, 4, 8, 16`（位掩码风格），方便做集合运算。

[src/fbs/mgmtd/MgmtdTypes.h:44-55](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/MgmtdTypes.h#L44-L55) 定义了全部强类型 id。这里有个关键的设计取舍值得读注释：

```cpp
// ChainVersion is space sensitive and uint32 is enough for changes of one chain
STRONG_TYPEDEF(uint32_t, ChainVersion);

STRONG_TYPEDEF(uint32_t, ChainTableId);
STRONG_TYPEDEF(uint32_t, ChainTableVersion);
STRONG_TYPEDEF(uint32_t, ChainId);
```

`TargetId`、`RoutingInfoVersion` 是 `uint64_t`（target 数量多、全局视图版本变化频繁），而 `ChainId`、`ChainTableId`、`ChainVersion`、`ChainTableVersion` 都压成 `uint32_t` 以省空间——因为线上要下发整张链表给每个进程，每个字段省 4 字节、乘以成千上万个 target/chain，就是可观的带宽。

接着看三个核心结构。**链表**：

[src/fbs/mgmtd/ChainTable.h:7-14](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/ChainTable.h#L7-L14) `ChainTable` 只有四个字段：`chainTableId`（表 id）、`chainTableVersion`（表版本）、`chains`（一串 `ChainId`，顺序就是元数据服务 round-robin 选链的顺序）、`desc`（人类可读描述）。注意它**只存 ChainId 列表**，不存 target。

**链**：

[src/fbs/mgmtd/ChainInfo.h:6-13](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/ChainInfo.h#L6-L13) `ChainInfo` 含 `chainId`、`chainVersion`、`targets`（一串 `ChainTargetInfo`，head 在前、tail 在后）、`preferredTargetOrder`（读流量的偏好顺序，用于把读均匀分摊到链上各 target）。

**链内 target 精简视图**：

[src/fbs/mgmtd/ChainTargetInfo.h:8-13](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/ChainTargetInfo.h#L8-L13) `ChainTargetInfo` 只有两个字段：`targetId` 与 `publicState`。这就是链表下发时每个 target 携带的全部信息——够 client/storage 决定能不能读/写，又足够紧凑。

**完整 target**：

[src/fbs/mgmtd/TargetInfo.h:8-18](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/TargetInfo.h#L8-L18) `TargetInfo` 是最完整的 target 描述：`publicState` + `localState` 双状态、`chainId`（所属链）、`nodeId`（所在机器，`optional` 表示孤儿 target 可能暂无归属）、`diskIndex`（所在 SSD）、`usedSize`（已用空间）。它独立存于 `TargetMap`。

最后看「容器」——`RoutingInfo` 如何把它们装起来：

[src/fbs/mgmtd/RoutingInfo.h:35-48](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/RoutingInfo.h#L35-L48) 定义了四张映射表与两个顶层字段：

```cpp
using NodeMap          = robin_hood::unordered_map<NodeId, NodeInfo>;
using ChainTableVersionMap = std::map<ChainTableVersion, ChainTable>;   // ordered!
using ChainTableMap    = robin_hood::unordered_map<ChainTableId, ChainTableVersionMap>;
using ChainMap         = robin_hood::unordered_map<ChainId, ChainInfo>;
using TargetMap        = robin_hood::unordered_map<TargetId, TargetInfo>;
```

注意一个细节：`ChainTableMap` 不是 `unordered_map<ChainTableId, ChainTable>`，而是嵌了一层 `std::map<ChainTableVersion, ChainTable>`。这张内层 map 是**按版本号有序**的（`std::map` 按 key 排序），这是「同一张表保留多个历史版本」的关键（见 4.2）。其它三张表都是 `robin_hood::unordered_map`（高性能哈希表，按 id 直接 O(1) 查）。`routingInfoVersion` 是整个视图的版本号，`bootstrapping` 标记集群是否处于引导期。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：用一个具体的 `targetId` 走通「从 target 找到机器与链位置」的查询路径，验证你对三层关系的理解。

**操作步骤**：

1. 打开 [src/fbs/mgmtd/RoutingInfo.cc:72-82](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/RoutingInfo.cc#L72-L82)，阅读 `RoutingInfo::getTarget`，确认它就是在 `targets` 这张 `unordered_map` 里做一次哈希查找。
2. 假设拿到一个 `TargetInfo`，其 `chainId = 3`、`nodeId = 5`。阅读 `getChain`（[L37-47](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/RoutingInfo.cc#L37-L47)）与 `getNode`（[L60-70](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/RoutingInfo.cc#L60-L70)），确认它们同样是 O(1) 哈希查找。
3. 在 `ChainInfo::targets` 里线性扫描 `ChainTargetInfo::targetId`，即可判断本 target 是 head（下标 0）、tail（末位）还是中间的 successor。

**需要观察的现象**：四张表之间**没有任何指针互连**，完全靠 `targetId` / `chainId` / `nodeId` 这些 id 做「逻辑外键」关联。这是序列化友好的设计——整个 `RoutingInfo` 可以整体打包成 FlatBuffers/二进制一次性下发给客户端。

**预期结果**：你能画出一张「targetId → TargetInfo →（chainId）→ ChainInfo →（targetId 列表）→ 位置；（nodeId）→ NodeInfo → 地址」的关系图。

**待本地验证**：若要真实跑一遍，可在单测中构造一个 `flat::RoutingInfo` 并调用上述 getter（仓库内 meta/storage 单测有大量此类构造，可自行检索 `RoutingInfo{` 的用法）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ChainTable::chains` 存的是 `std::vector<ChainId>` 而不是 `std::vector<Chain>`？

**参考答案**：因为链与链表是多对多关系——同一条链可被多张链表引用。若内联存储 `Chain`，同一条链会在多张表里重复存多份，既浪费空间又会在链变更时产生多份不一致的副本。存 `ChainId` 则让 `Chain` 只在 `ChainMap` 里存一份，链表只是「引用集合」。

**练习 2**：`ChainTargetInfo` 与 `TargetInfo` 都有 `publicState`，它们会不一致吗？

**参考答案**：正常情况下二者一致——`ChainTargetInfo` 是 `TargetInfo` 在「链内视图」里的投影，只保留 `targetId + publicState`。`ChainTargetInfo` 故意省略 `localState`、`nodeId`、`diskIndex` 等字段，因为 client/storage 在处理某条链的读写时只需要知道「这个 target 能不能读/写」，机器地址等信息按需从 `TargetMap`/`NodeMap` 另查。

---

### 4.2 版本语义：三级单调递增的版本号

#### 4.2.1 概念说明

分布式系统里「拓扑会变」是常态：节点会宕机、SSD 会坏、target 会被移到链尾做恢复。3FS 用**三级单调递增的版本号**来让所有进程对「我看到的拓扑是不是最新的」达成一致，并在变更期间仍能正确读写。

| 版本号 | 作用域 | 类型 | 何时递增 | 谁来比对 |
| :--- | :--- | :--- | :--- | :--- |
| `routingInfoVersion` | 整个 `RoutingInfo` 视图 | `uint64` | 任何影响路由的变更（节点上下线、链/链表变更、配置变更） | client/storage/meta 拉取路由时按需增量获取 |
| `chainTableVersion` | 单张链表 | `uint32` | 链表的链清单/顺序发生变化（增删链、调整顺序） | `ChainRef` 钉住某个历史版本 |
| `chainVersion` | 单条链 | `uint32` | 链的成员发生变化（target 下线、被移到链尾） | storage 在每次写请求里校验，拒绝过期请求 |

这里有一个**容易混淆、但极其重要**的区分：

- `chainTableVersion` 解决的是「**这张表里有哪些链、什么顺序**」的版本问题，由 mgmtd 维护、在 meta/client 侧通过 `ChainRef` 解析。
- `chainVersion` 解决的是「**这条链内部有哪些 target**」的版本问题，由 storage 在数据面**逐请求**校验。

两者层次不同、校验位置不同，共同保证「链表/链在变更期间，旧请求不会写到错误的地方」。

#### 4.2.2 核心流程

**链表版本与「同表多版本」**。`ChainTableMap` 是 `unordered_map<ChainTableId, std::map<ChainTableVersion, ChainTable>>`。内层 `std::map` 按 `chainTableVersion` 有序，意味着**同一张表 id 下可以同时保留多个历史版本**。`getChainTable(tableId, tableVersion)` 的语义是：

- `tableVersion == 0`：取该表**最新**版本（`--end()`，因为 `std::map` 有序，末尾即最大版本）。
- `tableVersion != 0`：精确取**那个历史版本**。

这让 `ChainRef` 能「钉住」文件创建时的链表版本，即使后来链表更新了，老文件仍按它出生时的版本定位链。

**ChainRef 的取模 round-robin**。`ChainRef` 把 `(chainTableId, chainTableVersion, chainIndex)` 打包成一个引用。`getChainId` 把 1-based 的 `chainIndex` 对链数取模，得到实际链：

\[
\text{chainIndex}' = \big((\text{chainIndex} - 1) \bmod N\big) + 1,\quad N = \text{table.chains.size()}
\]

这个取模让元数据服务可以无限地 round-robin 轮询链表里的链，给新文件均匀分配，而不需要在文件布局里存一个会不断增长的绝对序号。

**chainVersion 的数据面校验**。每次写请求（来自 client 或链上前驱）都会带上它认知的 `chainVersion`；storage 收到后与本机最新已知的 `chainVersion` 比对，**不一致就拒绝**（见 `docs/design_notes.md` 数据复制一节："The service checks if the chain version in write request matches with the latest known version; reject the request if it's not"）。这保证了一条链在被 mgmtd 改动（比如某 target 下线被移到链尾）后，旧版本的写请求不会被错误地发给已经不在原位置的 target。

#### 4.2.3 源码精读

先看 `ChainRef` 的编码——它就是三元组的简单封装：

[src/fbs/mgmtd/ChainRef.h:11-35](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/ChainRef.h#L11-L35) `ChainRef` 由 `chainTableId`、`chainTableVersion`、`chainIndex` 三字段组成，`decode()` 还原成三元组。注意**它不带 `chainVersion`**——链成员版本的校验是 storage 侧另用请求里携带的 `chainVersion` 做的，与 `ChainRef` 无关。这是两层版本号互不混淆的直接体现。

再看「按版本取链表」与「ChainRef 取模定位链」的实现，这是本模块最核心的两段代码：

[src/fbs/mgmtd/RoutingInfo.cc:6-14](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/RoutingInfo.cc#L6-L14) `getChainTable`：

```cpp
// tv == 0 means latest version
auto vit = tableVersion != 0 ? tit->second.find(tableVersion)
                             : (--tit->second.end());
```

`--tit->second.end()` 取有序 `std::map` 的最后一个元素，即最大版本号对应的 `ChainTable`。这正是「`tableVersion == 0` 表示取最新」的实现。

[src/fbs/mgmtd/RoutingInfo.cc:22-35](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/RoutingInfo.cc#L22-L35) `getChainId`（ChainRef → ChainId）：

```cpp
std::optional<ChainId> RoutingInfo::getChainId(ChainRef ref) const {
  auto [tid, tv, index] = ref.decode();
  if (tid == 0 && tv == 0) {
    return ChainId(index);          // 退化情形：直接当 ChainId 用
  }
  const auto *table = getChainTable(tid, tv);   // 钉住历史版本
  if (!table) return std::nullopt;
  if (index == 0) return std::nullopt;
  index = (index - 1) % table->chains.size();   // 1-based 取模 round-robin
  return table->chains[index];
}
```

读这段代码要抓住三点：① `getChainTable(tid, tv)` 用的是 `ChainRef` 自带的 `tv`（非 0），所以**定位的是创建文件时那张历史版本的表**，不是最新表；② `(index - 1) % size` 是 1-based 取模，让序号可以无限增长而自动在链表内循环；③ `tid == 0 && tv == 0` 是一种「裸 ChainId」退化用法，绕过链表直接指定链。

`docs/design_notes.md` 对链版本的描述：

[docs/design_notes.md:122](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L122) "Each chain has a version number. The version number is incremented if the chain is changed (e.g. a storage target is offline). Only the primary cluster manager makes changes to chain tables." —— 点明 `chainVersion` 的递增时机（链成员变更）与唯一修改者（primary mgmtd）。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：用一个具体的 `ChainRef` 手工推演 `getChainId`，验证「版本钉扎 + 取模 round-robin」两个机制。

**操作步骤**：

1. 假设某张链表 `chainTableId=1`，当前最新 `chainTableVersion=3`，`chains = [10, 20, 30, 40]`（共 4 条链）。
2. 假设文件 A 在 `chainTableVersion=2` 时创建，布局里存的 `ChainRef = (1, 2, 7)`。
3. 按 [RoutingInfo.cc:22-35](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/RoutingInfo.cc#L22-L35) 推演：
   - `tid=1, tv=2, index=7`，非退化分支。
   - `getChainTable(1, 2)` 返回的是 **v=2 那张历史表**（注意不是 v=3），假设 v=2 的 `chains = [10, 20, 30]`（3 条链）。
   - `index = (7-1) % 3 = 6 % 3 = 0` → 返回 `chains[0] = ChainId(10)`。
4. 再假设文件 B 在最新版本 v=3 创建，`ChainRef = (1, 3, 7)`：`getChainTable(1,3)` 取 v=3 表，`index = (7-1) % 4 = 6 % 4 = 2` → `chains[2] = ChainId(30)`。

**需要观察的现象**：**同样的 `chainIndex=7`，因为钉住的表版本不同、表的链数不同，最终落到不同的链**。这就是版本钉扎的意义——老文件不会被新链表版本「带跑偏」。

**预期结果**：你能解释「为什么文件布局里要存 `chainTableVersion` 而不是只存 `chainTableId`」——因为只存 id 会总是取最新表，老文件的数据定位就会在链表变更后被破坏。

**待本地验证**：可在单测里构造两个版本的 `ChainTable` 塞进 `RoutingInfo.chainTables`，调用 `getChainId` 验证上述取模结果。

#### 4.2.5 小练习与答案

**练习 1**：`getChainTable(id, 0)` 与 `getChainTable(id, v)`（v 为某具体非零版本）行为有何不同？为什么需要两种？

**参考答案**：`tableVersion=0` 取该表最新版本（有序 `std::map` 的末尾），用于「我就要当前最新拓扑」的场景（如 mgmtd 向新加入节点下发、admin_cli 查询）；指定非零 v 则精确取历史版本，用于 `ChainRef` 钉住文件创建时的拓扑。两种语义对应「拉最新」与「按引用还原历史」两类需求。

**练习 2**：`chainVersion` 与 `chainTableVersion` 都叫「版本号」，它们校验的是同一件事吗？

**参考答案**：不是。`chainTableVersion` 校验的是「**链清单/顺序**」的版本，决定一个 `ChainRef` 解析到哪条链，在 meta/client 侧由 `getChainTable` 解析；`chainVersion` 校验的是「**某条链内部的 target 成员**」的版本，决定一次写请求会不会被发给位置已变动的 target，在 storage 数据面逐请求比对。前者管「挑哪条链」，后者管「链内怎么转发」。

**练习 3**：`ChainRef` 里为什么**不**包含 `chainVersion`？

**参考答案**：`ChainRef` 的职责是在「链表层」把文件布局解析到某条 `ChainId`，这一层只关心链表的链清单版本（`chainTableVersion`）。链内部 target 成员的版本（`chainVersion`）是另一层关注点，由 storage 在收到具体读写请求时、用请求自带 `chainVersion` 字段单独校验。两层解耦，`ChainRef` 自然不需要也无法承载 `chainVersion`。

---

### 4.3 多表隔离：用多张链表服务不同工作负载

#### 4.3.1 概念说明

一个集群里可以同时存在**多张链表**，每张用不同的 `chainTableId` 标识。为什么要多张？因为不同的工作负载对「数据放在哪些节点/SSD 上」有不同、甚至相互冲突的要求。

`docs/design_notes.md` 给了一个典型例子：建两张链表，一张给批处理/离线作业，一张给在线服务，两张表**建立在互斥的节点和 SSD 上**。这样离线任务的大流量扫描绝不会抢占在线服务的 SSD 带宽——物理隔离带来了性能隔离。

关键认知：

1. **链表是「视图/选择」，链与 target 是「资源」**。多张链表是对底层同一批 chain/target 资源的不同编排。
2. **文件在创建时通过目录布局指定 `chainTableId`**，此后该文件的所有 chunk 都从这张表里选链。换表需要新文件（或新目录布局），不会影响已有文件。
3. **互斥隔离是「构造时保证」的，不是结构强制**。`ChainTable` 结构本身不校验两张表是否用了互斥的 target——互斥性由建链表的人（`deploy/data_placement` 脚本 + admin_cli `upload-chain-table`，见 u8-l1）在设计时保证。结构只提供「多张表共存」的能力。

#### 4.3.2 核心流程

一个集群典型的多表使用流程：

1. **规划**：运维用 `deploy/data_placement` 脚本（基于平衡不完全区组设计 / 整数规划，见 u8-l1）为不同 workload 各生成一张链表，约束它们使用互斥的节点/SSD。
2. **上传**：用 `admin_cli upload-chains` 上传链、`upload-chain-table` 上传链表（每张表一个 `chainTableId`），mgmtd 把它们写入 `RoutingInfo.chainTables`。
3. **绑定**：在某个目录上设置布局（`SetDirLayout`，指定 `chainTableId` / `chunkSize` / `stripeSize`），该目录下新建的文件就都用这张表。
4. **下发**：mgmtd 把 `RoutingInfo`（含全部链表）下发给所有进程；client 打开文件时拿到布局里的 `chainTableId`，从对应链表里 round-robin 选链。
5. **隔离生效**：因为两张表用了互斥 target，离线作业的读写流量只打在「离线表」的 target 上，物理上碰不到「在线表」的 SSD。

#### 4.3.3 源码精读

多表共存的结构基础，就是 `RoutingInfo.chainTables` 的类型：

[src/fbs/mgmtd/RoutingInfo.h:37-38](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/RoutingInfo.h#L37-L38)

```cpp
using ChainTableVersionMap = std::map<ChainTableVersion, ChainTable>;
using ChainTableMap = robin_hood::unordered_map<ChainTableId, ChainTableVersionMap>;
```

外层以 `ChainTableId` 为 key——**每个 id 对应一张独立的链表**（外加它自己的多版本历史）。要几张表，就往这个 map 里塞几个 entry，互不干扰。读一张表只需 `getChainTable(tableId, ...)`，天然隔离。

设计意图的直接出处：

[docs/design_notes.md:124-126](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L124-L126) "A few chain tables can be constructed to support different data placement requirements. ... The two tables consist of storage targets on mutually exclusive nodes and SSDs." 以及 "The concept of chain table is created to let metadata service pick a table for each file and stripe file chunks across chains in the table." —— 明确了「多表 = 多种放置需求」「互斥节点/SSD」「meta 为每个文件挑一张表」三件事。

`ChainTable::desc` 字段（[ChainTable.h:13](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/ChainTable.h#L13)）就是给人区分「这张表是给谁的」用的可读描述（如 `"batch"` / `"online"`），是运维管理多表时的人肉标识。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：在源码与文档中确认「多表隔离是构造时约定、结构本身不强制互斥」，并理解其代价。

**操作步骤**：

1. 阅读 [RoutingInfo.h:35-48](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/RoutingInfo.h#L35-L48)，确认 `ChainTableMap` 只按 `ChainTableId` 索引，**没有任何字段记录「这张表与那一张表是否互斥」**。
2. 用检索工具搜索 `chainTableId` 在仓库中的使用点（尤其 meta 侧的布局结构与 `ChainAllocator`，见 u4-l4），观察文件布局如何携带 `chainTableId` 来选定一张表。
3. 阅读 `deploy/data_placement` 相关脚本与 `deploy/README.md`（见 u8-l1），确认「互斥」是建链表脚本在生成阶段保证的，而非运行时结构保证。

**需要观察的现象**：结构层只提供「多表共存与按 id 取表」的能力；隔离的正确性责任在建链表的人身上。这意味着**如果你手工把同一个 target 塞进两张表，系统不会拦你**——但隔离就被破坏了。

**预期结果**：你能说清「为什么 3FS 不在 `ChainTable` 里加一个互斥校验」——因为互斥是一种 placement 策略，策略多变（有时是节点互斥、有时是 SSD 互斥、有时是 traffic zone 互斥），硬编码进结构反而限制了灵活性。把它交给建表脚本（u8-l1 的整数规划模型）是更干净的分层。

**待本地验证**：若有测试集群，可用 `admin_cli list-chain-tables` 观察多张表共存，并用 `list-chains` 比对两张表的 target 是否真的互斥。

#### 4.3.5 小练习与答案

**练习 1**：假设运维失误，把同一个 target 同时放进「在线表」和「离线表」两张链表，系统会报错吗？会有什么后果？

**参考答案**：结构层不会报错（`ChainTableMap` 不校验互斥）。后果是隔离被破坏——离线作业的大流量读写会打在这个共享 target 上，从而影响在线服务的延迟与带宽。这正是 4.3.4 实践要确认的「互斥是构造时约定」。

**练习 2**：一个已经存在的文件，能不能通过改链表把它「搬到」另一张表？

**参考答案**：不能直接搬。文件的 `ChainRef` 已经钉死了它创建时的 `(chainTableId, chainTableVersion, chainIndex)`，对应的数据也落在那些链的 target 上。要让文件换表，需要新建文件（在新表/新布局下）并拷贝数据，或通过目录级别的布局变更影响**后续新建**的文件。已有文件的布局不会因链表变更而改动。

---

## 5. 综合实践：手工构造一张 6 节点链表并标注 public 状态

本实践把三个最小模块串起来。依据 `docs/design_notes.md` 的 6 节点示例，手工构造一张完整的链表，并用本讲学过的结构去标注每个 target 的 public 状态。

### 5.1 场景设定

- 6 个 storage 节点：A、B、C、D、E、F，每个节点 1 块 SSD。
- 每块 SSD 上建 5 个 target：编号 1~5。所以全部 target 为 A1…A5、B1…B5、…、F5，共 30 个。
- 每个 chunk 3 副本，即每条链含 3 个 target（head、中间、tail）。

### 5.2 操作步骤

**第 1 步：选定链表设计。** 选用 `design_notes.md` 给出的「恢复期流量均衡」链表（它让任一节点故障时，其读流量被均匀分摊到其余 5 个 SSD，优于简单链表）：

| Chain | chainVersion | Target 1 (head) | Target 2 | Target 3 (tail) |
| :---: | :---: | :---: | :---: | :---: |
| 1 | 1 | B1 | E1 | F1 |
| 2 | 1 | A1 | B2 | D1 |
| 3 | 1 | A2 | D2 | F2 |
| 4 | 1 | C1 | D3 | E2 |
| 5 | 1 | A3 | C2 | F3 |
| 6 | 1 | A4 | B3 | E3 |
| 7 | 1 | B4 | C3 | F4 |
| 8 | 1 | B5 | C4 | E4 |
| 9 | 1 | A5 | C5 | D4 |
| 10 | 1 | D5 | E5 | F5 |

（出处：[docs/design_notes.md:134-145](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L134-L145)）

**第 2 步：用本讲的结构「翻译」这张表。**

- 这张表对应一个 `ChainTable`：`chainTableId = 1`、`chainTableVersion = 1`、`chains = [1,2,3,...,10]`、`desc = "balanced-6nodes"`。
- 每一行对应一个 `ChainInfo`：如 `chainId=2, chainVersion=1, targets = [ChainTargetInfo{targetId=A1, SERVING}, ChainTargetInfo{B2, SERVING}, ChainTargetInfo{D1, SERVING}]`。
- 每个 target 同时在 `TargetMap` 里有一条 `TargetInfo`，如 `targetId=A1, publicState=SERVING, localState=UPTODATE, chainId=2, nodeId=A, diskIndex=0`。

**第 3 步：标注初始 public 状态。** 集群刚建成、一切正常时，全部 30 个 target 的 public 状态都是 `SERVING`（可读可写）。参照 public 状态语义表 [docs/design_notes.md:185-191](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L185-L191)：serving = 服务存活且服务客户端请求。

**第 4 步：注入一次故障并推演状态。** 假设节点 A 宕机（A1~A5 全部失联）。结合本讲对「public 状态随链表下发」的理解，做**高层**推演（**精确的状态机转换属于 u3-l5，这里只做定性**）：

- A 上的 5 个 target（A1~A5）会被 mgmtd 标为 `OFFLINE`，并被**移到各自链的链尾**（见 design_notes 所述 "If a storage target is marked offline, it's moved to the end of chain"）。
- 由于 A 的 target 离线，相关链的成员发生变化，这些链的 `chainVersion` 递增（如链 2/3/5/6/9 各自 +1）。
- 因为 A 的读流量原本可由链上的其他 target 承接（CRAQ 读可打链上任意 target），且这张表设计成 A 与其余 5 个 SSD 都配对过，所以 A 的读流量被均匀分摊到 B~F，没有单点被打爆——这就是「均衡链表」的价值。
- 此后 storage 在收到带旧 `chainVersion` 的写请求时会拒绝，迫使前驱/client 拿到新链表后重发（见 4.2）。

**第 5 步：验证多表隔离理解。** 假设再建第二张链表 `chainTableId = 2`，**只用节点 A、B、C 上的 target**（与上表 D、E、F 互斥）。把它绑定到「在线服务」目录，把 `chainTableId = 1` 绑定到「离线批处理」目录。回答：离线作业的流量会碰到在线表的 target 吗？——不会，因为两张表用了互斥 target（4.3）。

### 5.3 需要观察的现象与预期结果

- 你应当能用 `ChainTable / ChainInfo / ChainTargetInfo / TargetInfo` 四个结构**逐字段**描述这张表里的任意一行。
- 你应当能解释：A 故障后，为什么老文件的 `ChainRef` 仍能正确定位（因为 `chainTableVersion` 没变，链清单没变；变的是各链内部的 `chainVersion` 与 target 顺序）。
- 你应当能区分：本次故障改的是 `chainVersion`（链成员），**没有**改 `chainTableVersion`（链清单），所以 `ChainRef` 解析不受影响。

### 5.4 待本地验证

若想在真实集群观察上述结构，可：用 `admin_cli upload-chains` / `upload-chain-table` 导入一张表（具体命令见 u8-l1），再用 `admin_cli list-chain-tables` / `list-chains` 打印 mgmtd 实际持有的 `ChainTable` 与 `ChainInfo`，与上面的手工构造逐字段比对。注入故障（停一个 storage 进程）后，再次 `list-chains` 观察 `chainVersion` 递增与 target 移到链尾的现象。**精确的 public 状态转换请对照 u3-l5 的状态转换表。**

## 6. 本讲小结

- **三层结构**：ChainTable（链清单）→ Chain（一条 CRAQ 复制链）→ ChainTargetInfo/TargetInfo（链内/完整 target）。四者通过 `RoutingInfo` 的四张映射表（NodeMap / ChainTableMap / ChainMap / TargetMap）用 id 做「逻辑外键」组织，整体可一次性序列化下发。
- **间接引用**：ChainTable 只存 `ChainId` 列表，Chain 与链表是多对多；同一条链可在多张表复用，避免重复与不一致。
- **三级版本号**：`routingInfoVersion`（整视图）、`chainTableVersion`（链清单/顺序）、`chainVersion`（链内成员）。前两者由 mgmtd 维护、在 meta/client 侧解析；`chainVersion` 在 storage 数据面逐请求校验，拒绝过期写。
- **版本钉扎**：`ChainRef = (chainTableId, chainTableVersion, chainIndex)` 把文件布局钉在创建时的历史链表版本上；`getChainTable(id, v)` 借助按版本有序的 `std::map` 还原历史版本，`getChainId` 用 1-based 取模实现 round-robin 选链。
- **多表隔离**：一个集群可有多张 `ChainTable`（按 `ChainTableId` 索引），服务于不同放置需求；互斥隔离由建表脚本在生成阶段保证，结构层只提供「多表共存」能力，不强制互斥。
- **紧凑性取舍**：`ChainId`/`ChainTableId`/`ChainVersion`/`ChainTableVersion` 压成 `uint32_t` 以省下发带宽，`TargetId`/`RoutingInfoVersion` 用 `uint64_t` 容纳大数量与高频变更。

## 7. 下一步学习建议

- **u3-l5（Target 状态机与故障检测）**：本讲只点到 public 状态的「含义」，状态之间如何转换（serving↔syncing↔waiting↔lastsrv↔offline，由 local state 触发）是下一讲的核心，建议紧接着学，并把本讲的「6 节点 A 故障」场景用完整状态转换表重新推演一遍。
- **u3-l6（路由信息分发与配置管理）**：本讲的 `RoutingInfo` 是如何被 `routingInfoVersion` 推进并下发给 client/storage/meta 的，下一讲会讲清楚分发机制。
- **u4-l4（文件数据布局与链分配）**：想看 `ChainRef` 是怎么被 meta 的 `ChainAllocator` 用 round-robin + shuffle seed 实际「制造」出来的，去读这一讲，它把本讲的链表结构与文件布局真正接起来。
- **u8-l1（数据放置算法与链表生成）**：想了解本讲「恢复期均衡链表」是怎么用平衡不完全区组设计 + 整数规划算出来的，以及 `upload-chains`/`upload-chain-table` 命令如何把结果灌进 mgmtd，去读这一讲。
