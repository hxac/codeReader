# 文件数据布局与链分配

## 1. 本讲目标

本讲解决一个问题：**一个新文件被创建时，它的数据该落在哪些复制链（chain）上？**

读完本讲你应当能够：

1. 说清楚一个 3FS 文件是如何被切成 chunk、又如何按 stripe 跨多条链「条带化（striping）」的，并能解释 `chunkSize`、`stripeSize`、`tableId` 这些布局字段各自的作用。
2. 读懂 `Layout` 数据结构的三种形态（`Empty` / `ChainRange` / `ChainList`），并理解 `ChainAllocator` 是如何用「轮询（round-robin）」从链表里为每个新文件挑出一段连续链的。
3. 解释为什么挑出来的连续链还要再用一个 `seed` 做 `shuffle`（打乱），以及为什么 3FS 要费力寻找一个「安全种子（safe seed）」。
4. 给定一张链表和 `stripeSize`，手工（或用小脚本）模拟 `ChainAllocator` 为若干个新文件分配链，并验证负载均衡效果。

## 2. 前置知识

本讲建立在 u4-l2（inode/目录项的 KV 编码）和 u3-l4（ChainTable / Chain / Target 数据模型）之上。开始前请确认你已了解：

- **chunk 与 ChunkId**：3FS 把一个文件切成等长的 chunk。每个 chunk 有一个全局唯一的 `ChunkId`，由 `inode id + chunk 序号` 拼成。本讲会看到它的精确编码。
- **chain（复制链）与 CRAQ**：一个 chunk 被复制在一条 chain 上，chain 由多个 target（分布在不同节点的 SSD 上）串成。写从链头进、读可打链上任一 target。详见 u1-l4、u5-l3。
- **chain table（链表）**：一张有序的「链清单」，里面是一串 `ChainId`。`ChainAllocator` 就是从链表里挑链。详见 u3-l4。
- **ChainRef 与版本钉住**：`ChainRef = (tableId, tableVersion, chainIndex)`，靠 `tableVersion` 把文件创建那一刻的链表历史版本「钉住」，使链表后续演进不影响老文件的放置。详见 u3-l4。
- **`SHUFFLE_METHOD`**：构建期必填参数，锁死 `std::shuffle` 的实现，保证全集群数据放置一致。详见 u1-l2。本讲会看到它与 `seed` 的微妙关系。

如果以上概念已清楚，我们直接进入源码。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [src/fbs/meta/Schema.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/meta/Schema.h) | 定义 `Layout` 数据结构（三态）、`ChunkId` 编码、以及文件→chunk→chain 的核心计算函数声明。 |
| [src/fbs/meta/Schema.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/meta/Schema.cc) | `Layout` 的方法实现：`getChunkId`、`getChainId`、`getChainIndexList`、`getChainOfChunk`、`valid`。 |
| [src/meta/components/ChainAllocator.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/ChainAllocator.h) | 创建文件时为布局选链的核心组件：校验布局、轮询挑 `baseIndex`、生成 `seed`。 |
| [src/common/utils/Shuffle.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/Shuffle.h) | 可移植的 `hf3fs_shuffle` 实现，以及 `find_safe_seed` / `safe_shuffle_seed`。 |
| [src/fbs/mgmtd/RoutingInfo.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/RoutingInfo.cc) | `getChainId(ref)`：把布局里的「链序号」经 1-based 取模解析成真实 `ChainId`。 |
| [src/meta/components/InodeIdAllocator.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/InodeIdAllocator.h) | 新文件还要分配 `inode id`（它会成为每个 `ChunkId` 的高位）。批量分配以减少 FDB 访问。 |
| [src/meta/store/ops/BatchOperation.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/BatchOperation.cc) | 创建文件的 RPC 落点：决定「用父目录的布局」还是「现场分配链」。 |
| [tests/meta/components/TestChainAllocator.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/tests/meta/components/TestChainAllocator.cc) | 覆盖轮询推进、每目录计数器、负载均衡三条不变量的单元测试，是本讲实践的依据。 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**文件布局**、**轮询选链**、**shuffle 打乱**。

### 4.1 文件布局：chunk、stripe 与 Layout 数据结构

#### 4.1.1 概念说明

一个 3FS 文件在物理上不是「一整块」，而是被拆成两类映射：

1. **文件 → chunk**：文件按 `chunkSize`（必须是 2 的幂）等长切片。第 `k` 个 chunk 由 `(inode, k)` 唯一确定，打包成 `ChunkId`。
2. **chunk → chain**：chunk 不直接落在某个 SSD，而是落在一条复制链上。多个 chunk 按 `stripeSize` 条为一组，跨**不同**的链条带化（striping）。

为什么要条带化？如果整文件只放一条链，那么该文件的所有读写都压在链上的几个 target 上，既无法横向扩展吞吐，也无法分摊热点。条带化让一个文件的连续 chunk 轮流落到不同的链上，从而把单文件的负载摊到链表里的多条链（进而多个 SSD）上。

> 直觉一句话：**chunkSize 决定「切多细」，stripeSize 决定「跨几条链摊开」**。

`Layout` 就是把这两个参数、加上「具体用哪些链」打包在一起的元数据，存在文件的 inode 里。客户端 `open` 时拿到 `Layout`，之后读写就能**自己**算出每个 chunk 该去哪条链，无需再问 meta（这正是 meta 不在数据热路径的关键，见 u1-l4、u4-l1）。

#### 4.1.2 核心流程

给定一个文件偏移 `offset`，定位到 chain 的完整流程（客户端侧）：

```text
offset
  │  chunk = offset / chunkSize
  ▼
ChunkId(inode, track=0, chunk)              ← 文件→chunk
  │  chunkIndex = chunk (+ track*7)
  ▼
stripe = chunkIndex % stripeSize            ← chunk→stripe 内位置
  │  chains[stripe]   （chains 是 shuffle 后的链序号列表）
  ▼
ChainRef(tableId, tableVersion, chainIndex) ← 布局内的链序号
  │  getChainId: chainId = table.chains[(chainIndex-1) % N]
  ▼
ChainId                                       ← 真实复制链
```

其中 `chains` 这张「链序号列表」长度恰为 `stripeSize`，它由 `ChainAllocator` 在建文件时一次性确定（见 4.2、4.3）。之后该文件每个 chunk 的去向，都只是对这张表做取模查表——极其廉价。

注意最后一步 `getChainId` 的 `(chainIndex - 1) % N`：链表里共有 `N` 条链，而 `chainIndex` 是个可能大于 `N` 的「逻辑序号」，靠 1-based 取模绕回链表。这一点会在 4.2 解释轮询时用到。

#### 4.1.3 源码精读

`Layout` 结构定义在 [src/fbs/meta/Schema.h:71-169](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/meta/Schema.h#L71-L169)，关键字段：

```cpp
SERDE_STRUCT_FIELD(tableId, ChainTableId());        // 用哪张链表
SERDE_STRUCT_FIELD(tableVersion, ChainTableVersion()); // 钉住链表的哪一版
SERDE_STRUCT_FIELD(chunkSize, ChunkSize(0));        // 切多细（须为 2 的幂）
SERDE_STRUCT_FIELD(stripeSize, uint32_t(0));        // 跨几条链
SERDE_STRUCT_FIELD(chains, (std::variant<Empty, ChainRange, ChainList>{}));
```

`chains` 是个三态 variant，对应文件生命周期的不同阶段：

- `Empty`：还没分配链（新建文件的初始态，或空文件）。
- `ChainRange`：**最常见**。用一个「起点 `baseIndex` + shuffle 方式 + `seed`」紧凑地表示一段链，不存全表，节省空间。
- `ChainList`：显式存一个链序号数组，用于需要精确指定链的特殊场景（如某些工具）。

`ChainRange` 的三个字段（[Schema.h:85-112](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/meta/Schema.h#L85-L112)）：

```cpp
struct ChainRange {
  enum Shuffle : uint8_t { NO_SHUFFLE = 0, STD_SHUFFLE_MT19937 };
  SERDE_STRUCT_FIELD(baseIndex, uint32_t(0));   // 起始链序号（1-based）
  SERDE_STRUCT_FIELD(shuffle, Shuffle(0));       // 是否打乱
  SERDE_STRUCT_FIELD(seed, uint64_t(0));         // 打乱用的种子
  mutable folly::DelayedInit<std::vector<uint32_t>> chains; // 展开后的链序号（惰性缓存）
  std::span<const uint32_t> getChainIndexList(size_t stripe) const;
};
```

`getChainIndexList` 是把「起点」展开成实际链序号列表的地方（[Schema.cc:160-181](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/meta/Schema.cc#L160-L181)），它先构造 `[baseIndex, baseIndex+1, ..., baseIndex+stripe-1]`，再按 `shuffle` 决定是否打乱，结果用 `DelayedInit` 缓存（只算一次）：

```cpp
std::vector<uint32_t> chains(stripe);
for (uint32_t i = 0; i < stripe; i++) { chains[i] = baseIndex + i; }
switch (shuffle) {
  case NO_SHUFFLE: break;
  case STD_SHUFFLE_MT19937: hf3fs_shuffle(chains, seed); break;  // 见 4.3
}
```

有了链序号列表，`getChainOfChunk` 把 chunk 映射到列表中的一项（[Schema.cc:192-197](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/meta/Schema.cc#L192-L197)）：

```cpp
ChainRef Layout::getChainOfChunk(const Inode &inode, size_t chunkIndex) const {
  const auto chains = getChainIndexList();
  auto stripe = chunkIndex % stripeSize;                 // 在 stripe 内的位置
  return ChainRef{tableId, tableVersion, chains[stripe]}; // 钉住创建时的 tableVersion
}
```

注意返回的 `ChainRef` 带上了文件创建时的 `tableVersion`，所以即使链表后来演进，本文件的 chunk 仍按「老版本链表」解析——这就是 u3-l4 说的版本钉住。

外层入口 `File::getChainId` 把 `offset` 串起来（[Schema.cc:75-91](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/meta/Schema.cc#L75-L91)），它先算 `chunkIndex = offset / chunkSize + track * 7`（`track` 默认 0，`7` 是为未来多轨道文件预留的素数偏移），再取 `ChainRef`，最后交给 `routingInfo.getChainId` 解析成真实 `ChainId`：

```cpp
auto ref = layout.getChainOfChunk(inode, offset / layout.chunkSize + track * TRACK_OFFSET_FOR_CHAIN);
auto cid = routingInfo.getChainId(ref);
```

`getChunkId` 则更简单，就是除法（[Schema.cc:62-73](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/meta/Schema.cc#L62-L73)）：`chunk = offset / chunkSize`。`ChunkId` 的字节布局用大端，保证字典序与 chunk 序号一致（[Schema.h:177-218](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/meta/Schema.h#L177-L218)）。

最后看 1-based 取模解析（[RoutingInfo.cc:22-35](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/RoutingInfo.cc#L22-L35)）：

```cpp
std::optional<ChainId> RoutingInfo::getChainId(ChainRef ref) const {
  auto [tid, tv, index] = ref.decode();
  if (tid == 0 && tv == 0) return ChainId(index);   // ChainList 直存 ChainId 的捷径
  const auto *table = getChainTable(tid, tv);
  if (!table) return std::nullopt;
  if (index == 0) return std::nullopt;
  index = (index - 1) % table->chains.size();       // 1-based 取模绕回
  return table->chains[index];
}
```

`tableVersion == 0` 表示「最新版」（[RoutingInfo.cc:6-14](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/RoutingInfo.cc#L6-L14)）；而建文件时 `ChainAllocator` 会把它**钉成具体版本**（见 4.2.3）。

**关于 `inode id` 从哪来**：`ChunkId` 的高位是 inode id，它是另一个独立分配的资源。`InodeIdAllocator`（[InodeIdAllocator.h:45-96](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/InodeIdAllocator.h#L45-L96)）采用「高 52 位从 FDB 批量取 + 低 12 位本地发」的批量策略，每访问一次 FDB 能发 4096（\(2^{12}\)）个 id，并分 32 个 shard 以避免事务冲突。换句话说，建一个文件要做两件「分配」：`InodeIdAllocator` 发 inode id、`ChainAllocator` 填布局——本讲聚焦后者。

#### 4.1.4 代码实践：源码阅读型——画出 chunk 到 chain 的解析表

1. **实践目标**：用一个具体的小文件，验证「偏移 → chunk → stripe → chainIndex → ChainId」全链路，确认你读懂了取模逻辑。
2. **操作步骤**：
   - 假设参数：`chunkSize = 1 MiB`，`stripeSize = 4`，链表 `tableId=1` 共 `N=30` 条链（对应 design_notes 的 6 节点 × 5 target 示例），`baseIndex = 1`，`shuffle = NO_SHUFFLE`（便于手算）。
   - 则 `getChainIndexList` 展开为 `[1, 2, 3, 4]`。
   - 对偏移 `0, 1MiB, 2MiB, 3MiB, 4MiB, 5MiB`，分别算 `chunk`、`stripe = chunk % 4`、`chainIndex = chains[stripe]`、`ChainId = table.chains[(chainIndex-1) % 30]`。
3. **需要观察的现象**：连续 4 个 chunk（chunk 0~3）应分别落到链序号 1、2、3、4；第 5 个 chunk（chunk 4）绕回链序号 1，体现条带化循环。
4. **预期结果**（待本地用脚本复核，见 4.2.4 的 Python 模拟）：

   | offset | chunk | stripe | chainIndex | 实际 ChainId 在表中的下标 |
   | --- | --- | --- | --- | --- |
   | 0 | 0 | 0 | 1 | (1-1)%30 = 0 |
   | 1 MiB | 1 | 1 | 2 | (2-1)%30 = 1 |
   | 2 MiB | 2 | 2 | 3 | 2 |
   | 3 MiB | 3 | 3 | 4 | 3 |
   | 4 MiB | 4 | 0 | 1 | 0（绕回） |

5. 若想看真实链表，可用 `admin_cli stat <file>` 观察某文件的 layout 与各 chunk 的 chainId（见 [src/client/cli/admin/Stat.cc:55-85](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/Stat.cc#L55-L85)），该命令正是调用 `routingInfo.getChainId({tableId, tableVersion, index})` 来解析的。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `chunkSize` 必须是 2 的幂？

> **答案**：见 `valid()` 的校验（[Schema.cc:129-158](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/meta/Schema.cc#L129-L158)）：`folly::isPowTwo(chunkSize)`。除法 `offset / chunkSize` 在 `chunkSize` 为 2 的幂时可被编译器优化成位移，定位 chunk 更快；同时与存储侧 chunk engine 的物理块分级（2 的幂，见 u6-l2）天然对齐。

**练习 2**：`getChainId` 里为什么要 `(index - 1) % size` 而不是 `index % size`？

> **答案**：因为 `baseIndex` 与链序号都是 **1-based**（`valid()` 要求 `baseIndex != 0`，[Schema.cc:142](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/meta/Schema.cc#L142)）。1-based 让「序号 0」可以充当 `getChainId` 里的非法哨兵（`if (index == 0) return nullopt`）。`-1` 把 1-based 搬回 0-based 数组下标，`% size` 处理「逻辑序号超过链表长度」的绕回。

---

### 4.2 轮询选链：ChainAllocator 的 round-robin

#### 4.2.1 概念说明

新文件初始的 `Layout` 是 `Empty`（见 [BatchOperation.cc:463-477](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/BatchOperation.cc#L463-L477)）。创建文件时，meta 要把它「填上链」。这件事由 `ChainAllocator` 完成。

最朴素的想法是「随机挑 `stripeSize` 条链」，但随机挑会让某些链被频繁命中、某些闲置，长期看负载不均。3FS 选择**轮询（round-robin）**：维护一个游标，每建一个文件，游标向前走 `stripeSize` 步，挑出从游标开始的连续 `stripeSize` 条链。这样链表里每条链被均匀地轮到，整体负载均衡。

> 直觉一句话：**轮询 = 「这次从第 c 条开始挑 S 条，下次从第 c+S 条开始」。**

`ChainAllocator` 提供两个轮询变体（[ChainAllocator.h:49-80](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/ChainAllocator.h#L49-L80)）：

- **默认（全局）**：按 `(tableId, stripeSize)` 维护一个 `meta` 进程级的游标，所有文件共用。
- **每目录（perDir）**：当父目录带 `FS_CHAIN_ALLOCATION_FL` 标志时，用该目录自己的计数器，使「同一目录下的文件」按可预测的顺序轮流用链（便于按目录隔离/分析）。

#### 4.2.2 核心流程

设链表共有 `N` 条链，`stripeSize = S`，游标为 `c`。每个新文件：

```text
baseIndex = (c % N) + 1              # 1-based 起始序号
c_next    = (c + S) % N              # 游标前进 S
```

即第 \(k\) 个文件（从随机初始游标 \(c_0\) 起）的起始序号为：

\[
\text{baseIndex}_k = \bigl((c_0 + k \cdot S) \bmod N\bigr) + 1
\]

每个文件「占用」连续 `S` 条逻辑序号 \([\text{baseIndex}_k,\ \text{baseIndex}_k + S - 1]\)。当 \(N\) 被 \(S\) 整除时，连续文件恰好「无缝拼接」铺满链表，循环往复，每条链被命中的次数严格相等——这正是负载均衡的数学保证。

校验上还要求链表够大：`chainCnt >= stripeSize` 且非 0（[ChainAllocator.h:103-112](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/ChainAllocator.h#L103-L112)），否则分配失败。

#### 4.2.3 源码精读

核心实现是模板化的第三重载（[ChainAllocator.h:82-127](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/ChainAllocator.h#L82-L127)），两个具体重载只是注入不同的 `roundRobin` 闭包。主流程：

```cpp
CO_RETURN_ON_ERROR(co_await checkLayoutValid(layout));  // 1. 先校验
if (!layout.empty()) co_return Void{};                  // 2. 已有链则不动

auto routing = getRoutingInfo();
const auto *table = routing->raw()->getChainTable(tableId, tableVersion);  // 3. 取链表(tv=0→最新)
// ... 校验 table 存在、版本合法、chainCnt >= stripeSize ...
auto chainBegin = roundRobin(chainCnt);                 // 4. 轮询得 baseIndex
auto seed = find_safe_seed(layout.stripeSize);          // 5. 选安全种子（见 4.3）
layout.tableVersion = table->chainTableVersion;         // 6. 钉住真实版本
layout.chains = Layout::ChainRange(chainBegin, STD_SHUFFLE_MT19937, *seed); // 7. 写回
```

**第 6 步是关键细节**：传入的 `tableVersion` 通常是 0（意为「最新」），分配完成后被改写成链表当时的真实版本 `table->chainTableVersion`。从此该文件的 `ChainRef` 永远指向这一版链表——后续链表演进不影响它（呼应 u3-l4）。

默认（全局）轮询闭包（[ChainAllocator.h:49-65](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/ChainAllocator.h#L49-L65)），游标存在 `roundRobin_` 这个以 `(tableId, stripeSize)` 为键的 map 里：

```cpp
auto key = AllocType(tableId, stripeSize);
auto guard = roundRobin_.lock();
auto iter = guard->find(key);
if (iter == guard->end()) {
  // 首次：随机起点，并对齐到 stripeSize 的倍数
  auto initial = folly::Random::rand32(chainCnt) / stripeSize * stripeSize;
  iter = guard->insert({key, initial}).first;
}
auto res = (iter->second % chainCnt) + 1;                 // baseIndex
iter->second = (iter->second + stripeSize) % chainCnt;    // 游标前进
return res;
```

`roundRobin_` 的类型见 [ChainAllocator.h:132-133](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/ChainAllocator.h#L132-L133)，用 `folly::Synchronized<map>` 加锁保护并发。

每目录变体（[ChainAllocator.h:67-80](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/ChainAllocator.h#L67-L80)）逻辑相同，区别是游标由调用方传入的 `chainAllocCounter`（一个 `folly::Synchronized<uint32_t>`）持有——它绑定到目录，于是「同目录文件按序轮询」。是否启用由父目录的 `FS_CHAIN_ALLOCATION_FL` 决定（[BatchOperation.cc:472-476](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/BatchOperation.cc#L472-L476)）。

`checkLayoutValid`（[ChainAllocator.h:28-47](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/ChainAllocator.h#L28-L47)）会展开 `getChainIndexList()`，逐个确认每个链序号在路由表里都能解析到一条「有 target」的真实链——这是防止写入「指向空链」的布局的护栏。

#### 4.2.4 代码实践：用 Python 模拟轮询分配

这是本讲的主实践。给定一张 `N=30` 的链表和 `stripeSize=5`，模拟连续建若干文件，观察游标推进与负载均衡。

1. **实践目标**：复现「游标每次前进 S、baseIndex 循环递增、链被均匀命中」。
2. **操作步骤**：运行下面这段示例脚本（**示例代码**，非项目源码；只模拟轮询，不含 shuffle）：

   ```python
   # 示例代码：模拟 ChainAllocator 的全局轮询（不含 shuffle）
   N = 30          # 链表大小（对应 design_notes 6 节点示例）
   S = 5           # stripeSize
   c0 = 0          # 假设随机初始游标对齐后为 0（真实为随机）

   counter = c0
   from collections import Counter
   hit = Counter()
   for f in range(12):                      # 连续建 12 个文件
       base_index = (counter % N) + 1        # 1-based
       counter = (counter + S) % N           # 游标前进 S
       picked = [base_index + i for i in range(S)]  # shuffle 前的连续 5 条
       for ci in picked:
           real = ((ci - 1) % N)             # 解析到 0-based 链下标
           hit[real] += 1
       print(f"file {f}: baseIndex={base_index}, picked={picked}")
   print("每条链被命中次数:", dict(sorted(hit.items())))
   ```

3. **需要观察的现象**：
   - `baseIndex` 序列：`1, 6, 11, 16, 21, 26, 1, 6, ...`（每次 +5，对 30 取模绕回）。
   - 12 个文件后，每条链被命中的次数应大致相等（\(12 \times 5 / 30 = 2\) 次）。
4. **预期结果**：`baseIndex` 严格按 `+S` 递增并绕回；由于 \(N=30\) 被 \(S=5\) 整除，每条链恰好被命中 2 次，完全均衡。这与官方测试 `balance` 的断言一致（见 4.2.5）。
5. **对比试验**：把 `S` 改成不能整除 `N` 的值（如 `S=8`），观察命中次数会出现 ±1 的不均——这正是 `ChainAllocator` 要求「`N` 最好被 `S` 整除」以获得完美均衡的原因（链表生成脚本会据此设计，见 u8-l1）。
6. 真实游标的「随机初始值」会让起点不可预测，但**相对推进规律不变**，因此均衡性不受影响。

> 注：`shuffle` 会改变「picked 这 5 条」到真实 chunk 的映射顺序，但**不改变这 5 条链是哪几条**，因此不影响本实践的均衡性结论。shuffle 的作用见 4.3。

#### 4.2.5 小练习与答案

**练习 1**：官方测试 `perDirCounter` 断言 `chain % chainCount == (*prevChain + layout.stripeSize) % chainCount`（[TestChainAllocator.cc:59-61](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/tests/meta/components/TestChainAllocator.cc#L59-L61)）。请用游标公式解释它。

> **答案**：第 \(k\) 个文件 `baseIndex_k = ((c0 + k·S) % N) + 1`。相邻两文件满足 `baseIndex_{k+1} % N == (baseIndex_k + S) % N`（注意 1-based 在模 N 下可还原），这正是测试断言。它验证了「游标每次确实前进 S」。

**练习 2**：为什么默认轮询的 key 是 `(tableId, stripeSize)` 而不是单一全局游标？

> **答案**：不同链表（`tableId`）和不同条带宽度（`stripeSize`）的「轮询节拍」不同。若共用一个游标，一个 `stripeSize=4` 的文件会把游标推 4 步，从而干扰 `stripeSize=8` 的轮询节拍，破坏各自的均衡性。按 `(tableId, stripeSize)` 分桶，每类配置有独立游标，互不干扰。

---

### 4.3 shuffle 打乱：为什么需要打乱与 safe seed

#### 4.3.1 概念说明

轮询挑出的是**连续**的链序号 \([\text{baseIndex}, \text{baseIndex}+S-1]\)。但 3FS 的链表在构造时，为了让「单节点故障时的恢复流量」被多个 SSD 分摊，**相邻的链往往共享部分 target/SSD**（见 design_notes 第 132 行附近的链表设计：A 与其它每个 SSD 都配对）。如果文件的 stripe 总是落在一段连续链上，那么这条 stripe 永远集中读写同一小组 SSD，既容易热点，也与「故障时分摊」的设计相冲突。

解决办法：对挑出来的 `S` 个连续序号，用一个随机 `seed` 做一次**置换（shuffle）**，打散它们到链表里更分散的位置。置换是确定性的（同 `seed` 同结果），所以只需把 `seed` 存进 `Layout`，任何节点任何时候都能复算出同一张映射表。

> 直觉一句话：**轮询保证「长期每条链被均匀挑到」，shuffle 保证「单次挑到的几条链彼此分散、不扎堆」。**

但这里藏着一个跨版本的坑（见 4.3.2），3FS 用 `find_safe_seed` 解决它。

#### 4.3.2 核心流程

shuffle 的输入是 `[baseIndex, baseIndex+1, ..., baseIndex+S-1]`，输出是这 `S` 个数的一个排列，由 `seed` 决定：

\[
\text{chains} = \text{shuffle}\bigl([\text{baseIndex},\ldots,\text{baseIndex}+S-1],\ \text{seed}\bigr)
\]

置换后，`getChainOfChunk` 用 `chunkIndex % S` 在这个排列里查位置，于是文件的连续 chunk 被映射到链表里**分散**的几条链上。

**为什么需要「安全种子」**：C++ 标准只规定 `std::shuffle` 的**算法**（Fisher-Yates），却没规定底层随机数「缩放（downscaling）」的具体做法。libstdc++ 在 g++10 与 g++11 之间改了这个缩放实现，导致**同一个 `seed`、同一组数，不同编译器版本会洗出不同排列**。对 3FS 这是致命的：布局一旦写入 inode，全集群所有节点、所有未来版本都必须算出**同一张**映射表，否则 chunk 会被写到错误的链上、数据错乱。

3FS 的两道防线（呼应 u1-l2）：

1. **构建期锁死 `SHUFFLE_METHOD`**（`g++10` / `g++11` / `stdshuffle`，见 [Shuffle.h:26-34](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/Shuffle.h#L26-L34)）：全集群用同一种实现，这是一致性的主保证，一旦部署不可更换。
2. **`find_safe_seed`**：即便如此，再挑一个「**三种实现都恰好洗出相同排列**」的种子作为双保险，杜绝任何混用风险。

#### 4.3.3 源码精读

shuffle 的实际调用在 4.1.3 已见：`getChainIndexList` 里 `hf3fs_shuffle(chains, seed)`。`hf3fs_shuffle` 按 `SHUFFLE_METHOD` 分派到三种实现之一（[Shuffle.h:161-172](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/Shuffle.h#L161-L172)）：

```cpp
template <typename T>
void hf3fs_shuffle(std::vector<T> &vec, uint64_t mt19937_64_seed) {
#if defined(USE_STD_SHUFFLE)
  std_shuffle(vec, mt19937_64_seed);
#elif defined(USE_GCC10_SHUFFLE)
  gcc10_shuffle(vec, mt19937_64_seed);
#elif defined(USE_GCC11_SHUFFLE)
  gcc11_shuffle(vec, mt19937_64_seed);
#endif
}
```

三种实现（`std_shuffle` / `gcc10_shuffle` / `gcc11_shuffle`，[Shuffle.h:145-159](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/Shuffle.h#L145-L159)）都从同一个 `mt19937_64(seed)` 出发，差别仅在「把 64 位随机数缩放成 `[0, k)` 下标」的算法（`fast_range` vs 旧的 scaling 法，[Shuffle.h:41-96](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/Shuffle.h#L41-L96)）——而这正是 g++10/11 不兼容之处。作者甚至把 libstdc++ 对应源码的链接注释在函数上方，说明这是「刻意复刻」各版本行为。

`safe_shuffle_seed` 是「三实现一致性」的判定器（[Shuffle.h:174-194](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/Shuffle.h#L174-L194)）：用同一 `seed` 跑三种实现，比较结果是否完全相等：

```cpp
inline bool safe_shuffle_seed(uint32_t vec_len, uint64_t mt19937_64_seed) {
  // vec1 = std_shuffle, vec2 = gcc11, vec3 = gcc10
  ...
  return vec1 == vec2 && vec1 == vec3;
}
```

`find_safe_seed` 则暴搜一个安全种子（[Shuffle.h:196-206](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/Shuffle.h#L196-L206)）：

```cpp
inline std::optional<uint64_t> find_safe_seed(uint32_t vec_len) {
  for (size_t i = 0; i < 1000; i++) {
    auto seed = folly::Random::rand64();
    if (safe_shuffle_seed(vec_len, seed)) return seed;
  }
  XLOGF(DFATAL, "can't find safe shuffle seed for vec size {}", vec_len);
  return std::nullopt;
}
```

它最多试 1000 个随机种子，找不到才报错（注释说「概率应该极低」）。`ChainAllocator` 在分配时调用它（[ChainAllocator.h:115-118](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/ChainAllocator.h#L115-L118)），若失败整个建文件失败——一致性宁可拒绝服务也不冒错乱风险。

注意 `find_safe_seed` 的参数是 `stripeSize`（即被洗向量的长度），所以「安全」是针对**这个具体的 stripe 宽度**判定的；不同 `stripeSize` 的文件各自有自己的安全种子。

#### 4.3.4 代码实践：源码阅读型——验证 shuffle 不改变「选了哪几条链」

1. **实践目标**：确认你理解 shuffle 只重排「stripe 内 chunk→chainIndex 的映射」，而**不改变**轮询挑出的那 `S` 条链的集合。
2. **操作步骤**：
   - 复用 4.2.4 的脚本，在 `picked` 这 5 个连续序号上**手动**做一个置换，例如交换成 `[base+2, base+4, base+0, base+1, base+3]`（模拟某 `seed` 的结果）。
   - 对文件的 chunk 0~7，按 `stripe = chunk % 5` 查这张置换表得到 `chainIndex`，再 `(chainIndex-1) % 30` 得真实链下标。
   - 统计这 8 个 chunk 命中的链下标集合。
3. **需要观察的现象**：无论怎么置换，文件用到的链**始终是 picked 那 5 条**对应的真实链，只是 chunk 到链的配对顺序变了。
4. **预期结果**：链的**集合**不变（仍是轮询挑的那 5 条），仅 chunk↔chain 的对应被打散——这正是 shuffle 的全部作用：把热点从「连续 chunk 集中读某条链」打散开。
5. 真实的精确置换结果依赖 `mt19937_64(seed)` 与构建期的 `SHUFFLE_METHOD`，Python 难以精确复刻，故具体排列**待本地验证**；但「集合不变」这一结论与 `SHUFFLE_METHOD` 无关，可放心得出。

#### 4.3.5 小练习与答案

**练习 1**：既然构建期已用 `SHUFFLE_METHOD` 锁死实现，为什么还要 `find_safe_seed`？

> **答案**：`SHUFFLE_METHOD` 是主保证，但它依赖「全集群都用同一编译产物」这一前提。`find_safe_seed` 是**纵深防御**：即便出现新旧二进制混部、或未来升级路径出现意外，只要种子是「三实现一致」的，洗出的排列就相同，数据不会错放。代价只是建文件时多试几个随机种子（几乎总能立即找到）。

**练习 2**：如果 `find_safe_seed` 返回 `nullopt`，`ChainAllocator` 会怎样？

> **答案**：见 [ChainAllocator.h:115-118](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/ChainAllocator.h#L115-L118)：直接 `co_return` 一个 `kInvalidArg` 错误，建文件失败。3FS 选择「拒绝建文件」而非「用不安全种子」，宁可牺牲可用性也要保数据一致性。

---

## 5. 综合实践

把三个模块串起来，完成一次「端到端」的布局推演。

**场景**：链表 `tableId=1` 共 `N=30` 条链（沿用 design_notes 的 6 节点示例），某目录配置 `chunkSize = 1 MiB`、`stripeSize = 5`。连续在该目录下创建文件 `f0, f1, f2, f3`。

**任务**：

1. **轮询**：假设全局游标初始对齐值为 0，写出 `f0~f3` 各自的 `baseIndex` 与 shuffle **前**的 picked 链序号集合。
2. **钉版本**：说明分配完成后，4 个文件的 `Layout.tableVersion` 会变成什么、为什么。
3. **shuffle**：解释为什么每个文件的 5 条链「集合相同、内部顺序不同」这件事在这里不成立——即指出「集合」是由谁决定的、「顺序」是由谁决定的。
4. **读路径**：客户端打开 `f0` 后，要读偏移 `7 MiB` 处的数据，逐步算出它落在哪条 chainIndex、再经 `getChainId` 得到真实 `ChainId` 的下标（写出 `(chainIndex-1) % 30` 的结果）。
5. **故障联想**：若 `f0` 的某条链上有 target 故障（u3-l5 的状态机），`Layout.tableVersion` 的「钉住」特性如何保护 `f0` 已有 chunk 的放置不被错误改写？

**参考要点**：

1. `baseIndex`: 1, 6, 11, 16；picked 集合分别为 `{1..5}`、`{6..10}`、`{11..15}`、`{16..20}`。
2. 都被钉成创建那一刻链表的真实版本 `table->chainTableVersion`（传入是 0=最新，写回是具体值）。原因：保证 `ChainRef` 解析稳定，链表后续演进不影响老文件。
3. 「集合」由轮询的 `baseIndex` 决定（连续 5 条），「顺序」由 `seed` 决定的 shuffle 排列决定。不同文件的 `seed` 不同，所以顺序不同；但因游标每次前进 5，相邻文件的集合天然不重叠（\(N\) 被 \(S\) 整除时）。
4. `chunk = 7MiB/1MiB = 7`，`stripe = 7 % 5 = 2`，`chainIndex = chains[2]`（shuffle 后的第 3 个数，shuffle 前为 `baseIndex+2 = 3`），真实下标 `(3-1) % 30 = 2`（shuffle 后的具体值待本地验证）。
5. `tableVersion` 钉住历史链表版本，故障导致的状态变迁（chainVersion 推进、target 移到链尾等）发生在**新版本**链表里；`f0` 的 chunk 仍按老版本解析，不会被新状态误导，数据恢复由 storage 侧的同步流程（u5-l5）在新旧版本间完成。

## 6. 本讲小结

- 文件按 `chunkSize` 切成 chunk，再按 `stripeSize` 跨多条链条带化；`Layout`（三态 `Empty/ChainRange/ChainList`）把这些参数与「用哪些链」打包存进 inode，客户端 `open` 后即可自算 chunk→chain，meta 不在数据热路径。
- `getChainOfChunk` 用 `chunkIndex % stripeSize` 在链序号列表里查位置，`getChainId` 再用 `(index-1) % N` 把 1-based 逻辑序号绕回真实链表下标。
- `ChainAllocator` 用**轮询**为新文件从链表里挑连续 `stripeSize` 条链：`baseIndex = (c % N) + 1`，游标 `c` 每次前进 `stripeSize`；按 `(tableId, stripeSize)` 分桶，并有「每目录」变体。
- 分配完成后 `Layout.tableVersion` 被钉成链表的真实版本，使文件的 `ChainRef` 永远指向创建时的历史链表版本。
- 挑出的连续链再用 `seed` 做 `shuffle` 打散，避免单文件 stripe 总扎堆在共享 SSD 的相邻链上；轮询管「长期均衡」，shuffle 管「单次分散」。
- 由于 `std::shuffle` 在 g++10/11 间不兼容，3FS 既在构建期锁死 `SHUFFLE_METHOD`，又用 `find_safe_seed` 挑「三实现一致」的种子双保险，宁可建文件失败也不冒数据错放风险。

## 7. 下一步学习建议

- **链表是怎么构造的**：本讲的链表是「给定」的。要理解为何相邻链要共享 SSD、如何让故障恢复流量均衡，请读 u8-l1（数据放置算法与链表生成），那里的脚本正是为配合本讲的轮询+shuffle 而设计链表的。
- **故障后链与 target 的状态如何变迁**：本讲只讲「正常分配」。当一个 target 故障，chainVersion 如何推进、target 如何被移到链尾、新文件如何避开故障链，见 u3-l5（Target 状态机）。
- **数据真正落盘的细节**：本讲到 `ChainId` 为止。chain 内的 CRAQ 写传播、读任意 target、双版本一致性，见 u5-l3；chunk 在单个 target 上的物理存储与 RocksDB 元数据见 u6（chunk engine）。
- **建文件的完整 RPC**：本讲聚焦布局分配这一环。完整的 create/open/remove 在 FDB 事务里如何走，见 u4-l3；文件长度、session、GC 见 u4-l5。
