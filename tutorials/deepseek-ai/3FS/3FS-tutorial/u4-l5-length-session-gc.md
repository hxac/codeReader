# 动态文件长度、FileSession 与 GC

## 1. 本讲目标

本讲回答 meta 服务里三个「不在数据热路径、却决定正确性」的问题：

1. **文件的长度到底存在哪？** 客户端在并发写一个文件时，meta 里的 `length` 字段是「权威值」吗？什么时候才更新成真实长度？
2. **「某个客户端把文件打开来写」这件事，meta 怎么记住？** 如果那个客户端进程崩溃了、来不及关闭，meta 又怎么把这个「烂尾」记录清掉？
3. **一个文件被 `unlink` 删除后，它占的物理 chunk 立刻释放吗？** 如果立刻释放，会不会删掉某个还打开着该文件的客户端正在写的数据？

读完本讲你应当能够：

1. 说清 `VersionedLength`（`length` + `truncateVer`）的含义，解释 `sync` 的「周期上报最大写位置（lengthHint）」与 `close/fsync` 的「精确查 storage」这两套更新长度的机制为什么并存。
2. 读懂 `FileSession` 的 KV 编码与生命周期（create 时建、close 时删、崩溃后由 `SessionManager` 周期扫描回收），并能指出 session 与 GC 之间的互锁关系。
3. 描述 `unlink` 后「不立刻删 chunk、而是写一条带时间戳的 GC 条目、由后台扫描延迟回收」的完整流程，并解释延迟时间、空闲空间、优先级如何影响回收节奏。
4. 把「多客户端并发写 → 某客户端崩溃 → 文件被删」这一完整生命周期里「长度如何演进、session 如何维护与清理、chunk 何时真正释放」串成一条线。

## 2. 前置知识

本讲建立在 u4-l1（meta 无状态架构）、u4-l2（inode/目录项 KV 编码）、u4-l3（FDB 事务）和 u4-l4（文件布局与链分配）之上。开始前请确认你已了解：

- **meta 无状态**：meta 进程不持久化业务数据，inode、目录项、session 全存在 FoundationDB（FDB）里，崩溃/重启不丢。meta 经 `IKVEngine`→FDB 访问存储（见 u4-l1）。
- **KV 前缀编码**：每类对象用固定 4 字节前缀区分 key，如 inode 是 `INOD`、目录项是 `DENT`。本讲会新增一个 `INOS` 前缀给 session（见 u4-l2）。
- **FDB 事务与冲突范围**：快照读不进冲突范围、`get`/`addReadConflictRange` 进读冲突范围、`set`/`clear` 进写冲突范围；读写事务冲突会自动重试（见 u4-l3）。本讲会看到 session 用读冲突范围来「卡住并发 GC」。
- **chunk / stripe / chain**：文件按 `chunkSize` 切成 chunk，再按 `stripeSize` 跨多条链条带化；每个 chunk 落在一条 chain（CRAQ 复制链）上（见 u4-l4）。本讲会看到「查文件长度」就是「并行查每条 stripe 链上各自的最后一个 chunk」。
- **Distributor（一致性哈希转发）**：meta 用 `Weight::select(activeMetaServers, inodeId)` 把每个 inode 粘到固定的一个 meta 实例上，归我则本地跑、否则 `forward`（见 u4-l1）。本讲会确认 `Weight::select` 其实就是 **rendezvous hashing（最高随机权重哈希）**，它让不同 inode 的批量操作天然分摊到不同 meta 实例上。

如果以上概念已清楚，我们直接进入源码。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [src/fbs/meta/Schema.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/meta/Schema.h) | 定义 `VersionedLength`（`length`+`truncateVer`）与 `File::getVersionedLength/setVersionedLength`，是「长度」的数据模型。 |
| [src/fbs/meta/Service.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/meta/Service.h) | 定义 `SyncReq`/`CloseReq`，含 `updateLength`、`lengthHint`、`session`、`pruneSession` 等关键字段。 |
| [src/meta/store/ops/BatchOperation.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/BatchOperation.cc) | 长度更新的主战场：`syncAndClose` 合并请求、`queryLength` 决定是否查 storage；也是建/删 session 的落点。 |
| [src/meta/components/FileHelper.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/FileHelper.cc) | `queryLength`：把「查文件长度」转成对 storage 的 chunk 查询。 |
| [src/fbs/meta/FileOperation.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/meta/FileOperation.cc) | `queryChunksByChain`：跨 stripeSize 条链并行查「各自最后一个 chunk」，取最大者还原文件长度。 |
| [src/meta/store/FileSession.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/FileSession.h) / [.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/FileSession.cc) | `FileSession` 数据结构与 KV 编码（`INOS` 前缀）、`store/remove/scan/checkExists` 等操作。 |
| [src/meta/components/SessionManager.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/SessionManager.h) / [.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/SessionManager.cc) | 周期扫描全量 session、回收「死客户端」遗留 session 的后台管理器。 |
| [src/meta/store/ops/PruneSession.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/PruneSession.cc) | 客户端主动发起的 `pruneSession` RPC：把待清理 session 写到特殊 `InodeId(-1)` 下，由后台扫描优先处理。 |
| [src/meta/components/GcManager.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/GcManager.h) / [.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/GcManager.cc) | 延迟删除核心：`removeEntry` 建 GC 条目、`GcDirectory` 周期扫描、`gcFile` 真正删 chunk。 |
| [src/meta/store/ops/Remove.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/Remove.cc) | `unlink` RPC：nlink 归零时不立刻删 inode，而是交给 `gcManager().removeEntry` 走延迟回收。 |
| [src/fbs/meta/Utils.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/meta/Utils.h) | `Weight::select`：rendezvous 哈希，把 inode 粘到固定 meta 实例，使批量操作可聚合、负载可分摊。 |

## 4. 核心概念与源码讲解

本讲三个最小模块：**长度更新**、**会话管理**、**延迟删除与 GC**。三者环环相扣：session 是「文件还在被写」的标记，GC 删 chunk 前要先确认没有 session；长度则在 close/fsync 时才向 storage 查精确值。

### 4.1 长度更新：从 lengthHint 到精确查询

#### 4.1.1 概念说明

很多人会以为文件长度像传统文件系统那样，写多少就立刻更新多少。3FS **不是**这样。原因在于 3FS 的写数据根本不经过 meta：客户端拿到布局后直接把数据写到 storage 链上（见 u1-l4、u4-l1），meta 对「此刻磁盘上到底写了多长」并没有即时视图。如果每次 `write` 都让 meta 去查 storage 求长度，meta 立刻成为瓶颈。

所以 3FS 对 inode 里的 `length` 采用**最终一致性**策略，分两档：

1. **轻量档（sync / lengthHint）**：客户端周期性地把自己记录的「最大写位置」作为 `lengthHint` 上报给 meta。meta 只把多个 hint「取最大」合并，**不查 storage**，廉价地把 `length` 往大推。这个值可能偏小（还有数据在路上），但足够让 `stat`、目录列举等给出一个合理的「当前可见长度」。
2. **权威档（close / fsync，`updateLength=true`）**：文件关闭或显式刷盘时，meta 才真正去 storage 查「每条链上最后一个 chunk」，还原出权威长度，写回 inode。此后该长度才算准。

`length` 还配一个 `truncateVer`（截断版本号）：每次 `truncate`（缩短文件）让 `truncateVer + 1`。它的作用是**挡住并发场景下「旧长度」覆盖「新长度」**——稍后会看到 hint 比较时如何用到它。

> 直觉一句话：**`length` 平时是「乐观估计」，只有 close/fsync 才「向 storage 对账」变成真值。**

#### 4.1.2 核心流程

`VersionedLength` 的定义（[Schema.h:228-243](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/meta/Schema.h#L228-L243)）就是 `(length, truncateVer)` 一对。多个客户端的 hint 合并规则是「取 length 更大者」：

\[
\text{mergeHint}(h_1, h_2) = \arg\max_{h \in \{h_1,h_2\}} h.\text{length}
\]

一次 `sync`/`close` 批量操作的长度决策流程：

```text
sync/close 请求里 updateLength=true ?
   │ 否 → 不碰 length
   │ 是
   ▼
有 truncate？ ──是──► 忽略所有 hint，强制 updateLength（必须查 storage，因为截断了）
   │ 否
   ▼
合并所有请求的 lengthHint（取最大）→ hintLength
   ▼
queryLength(inode, hintLength, truncate)：
   ├─ 当前 length 已 ≥ hint？ ──是──► 不更新（hint 是旧消息）
   ├─ hint 的 truncateVer == 当前 且 hint.length > 当前？ ──是──► 用 hint
   └─ 否则 ──► 向 storage 查权威长度（FileOperation::queryChunks）
   ▼
新长度 != 旧长度？ → setVersionedLength + 更新 mtime/ctime，标记 inode dirty
```

关键不变量：**长度只会单调增长（除非 truncate）**。源码里对「新长度反而比旧长度小」直接 `FATAL`（除非是 truncate），见 [BatchOperation.cc:159-167](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/BatchOperation.cc#L159-L167)。

#### 4.1.3 源码精读

**入口：合并请求并决定是否更新长度**在 `BatchedOp::syncAndClose`（[BatchOperation.cc:107-187](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/BatchOperation.cc#L107-L187)）。它遍历本批所有 `sync`/`close` 请求，把各自的 `updateLength`、`truncated`、`lengthHint` 汇总：

```cpp
hintLength = meta::VersionedLength{0, 0};          // 初始 hint
for (auto &waiter : syncs_)   co_await sync(inode, ..., updateLength, truncate, hintLength);
for (auto &waiter : closes_)  co_await close(inode, ..., updateLength, hintLength, sessions);
if (truncate) { hintLength = std::nullopt; updateLength = true; }   // 截断→必须查
...
auto newLength = co_await queryLength(inode, hintLength, truncate);
```

`sync`/`close` 用 `VersionedLength::mergeHint` 把多个 hint 合并（[BatchOperation.cc:222](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/BatchOperation.cc#L222) 与 [:250](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/BatchOperation.cc#L250)），`mergeHint` 的实现就是「取 length 更大者」（[Schema.h:233-240](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/meta/Schema.h#L233-L240)）：

```cpp
static std::optional<VersionedLength> mergeHint(std::optional<VersionedLength> h1, std::optional<VersionedLength> h2) {
  if (!h1 || !h2) return std::nullopt;
  return h1->length >= h2->length ? h1 : h2;
}
```

**决策是否真查 storage**在 `BatchedOp::queryLength`（[BatchOperation.cc:260-303](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/BatchOperation.cc#L260-L303)）。注意它开头有一个「本批缓存」优化：若本批已经查过一次（`nextLength_` 有值），后续请求直接复用，避免一批里反复查 storage：

```cpp
if (nextLength_) co_return *nextLength_;                  // 本批已查过，复用
auto currLength = inode.asFile().getVersionedLength();
if (hintLength && !config().ignore_length_hint()) {
  if (currLength.truncateVer >= hintLength->truncateVer && currLength.length >= hintLength->length)
    co_return currLength;                                 // 当前已不小于 hint，无需更新
  if (hintLength->truncateVer == currLength.truncateVer && hintLength->length > currLength.truncateVer)
    co_return *hintLength;                                // 直接采信 hint（注意源码这里的比较）
}
// 否则查 storage
auto length = co_await fileHelper().queryLength(flat::UserInfo(user_), inode);
```

> 注：上面第二处 `hintLength->length > currLength.truncateVer` 是源码原样（[BatchOperation.cc:276](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/BatchOperation.cc#L276)），把 `length` 与 `truncateVer` 比较——读源码时请照原样理解，其语义仍是「hint 有效且领先时采信 hint」。精确的采信边界以本机运行日志为准（待本地验证）。

**真正的「向 storage 查权威长度」**在 `FileHelper::queryLength`（[FileHelper.cc:88-111](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/FileHelper.cc#L88-L111)），它把任务交给 `FileOperation::queryChunks`：

```cpp
FileOperation fop(*storageClient_, *rawRoutingInfo, userInfo, inode, recorder);
auto queryResult = co_await fop.queryChunks(hasHole != nullptr, config_.dynamic_stripe());
...
co_return queryResult->length;
```

`queryChunks` 的精髓是**把「查文件长度」拆成「查每条 stripe 链上最后一个 chunk」**（[FileOperation.cc:45-76](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/meta/FileOperation.cc#L45-L76)）：因为文件按 stripeSize 条带化，第 `s` 条链上只存放 chunk 序号 `s, s+stripeSize, s+2*stripeSize, ...` 的数据。所以只要并行问每条 stripe 链「你这儿最后一个 chunk 是第几个、多长」，再**取最大值**就得到全局最后一个 chunk，进而得到文件长度：

```cpp
for (auto &[chain, cresult] : *chains) {
  if (result.length < cresult.length) { result.length = cresult.length; ... }
}
auto length = result.lastChunk * inode_.asFile().layout.chunkSize + result.lastChunkLen;
```

「分摊」就体现在这里：查询压力被**分摊到 stripeSize 条链（进而多个 target/SSD）**上并行执行，而不是压在单条链上。`queryChunksByChain`（[FileOperation.cc:78-179](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/meta/FileOperation.cc#L78-L179)）为每条 stripe 构造一个 `QueryLastChunkOp`，再 `storage_.queryLastChunk(queries, ...)` 一次性并发发出。

**并发一致性兜底**：`BatchedOp::run` 在事务里先做一道 sanity check——如果本批处理过程中 inode 的 versionstamp 变了（说明别的 meta 实例改了这个 inode），就刷新本地缓存的长度并要求重试（[BatchOperation.cc:69-84](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/BatchOperation.cc#L69-L84)）：

```cpp
if (versionstamp != versionstamp_) { currLength_ = inode->asFile().getVersionedLength(); nextLength_ = std::nullopt; ... }
if (currLength_ != inode->asFile().getVersionedLength() && nextLength_ != inode->asFile().getVersionedLength())
  co_return makeError(MetaCode::kBusy, "length updated during operation, retry");
```

这就是为什么长度更新最终是安全的：乐观估计可能短暂偏小，但 close/fsync 会向 storage 对账，且并发改写会被 versionstamp 检测并重试。

#### 4.1.4 代码实践：源码阅读型——追踪一次 fsync 的长度对账

1. **实践目标**：把「客户端 fsync → meta 查 storage → 取每条链最大值 → 写回 inode」这条链走一遍，确认你理解「取最大值」与「striping」的关系。
2. **操作步骤**：
   - 假设某文件 `chunkSize = 1 MiB`、`stripeSize = 4`，已写满 10 个 chunk（即 chunk 0~9，长度 10 MiB）。
   - 根据 u4-l4，chunk `c` 落在第 `c % 4` 条 stripe 链。列出 chunk 0~9 各自落在哪条链（链 0/1/2/3）。
   - 模拟 `queryChunksByChain` 并行查 4 条链：每条链返回「我这里最后一个 chunk 的序号和长度」。
3. **需要观察的现象**：4 条链返回的 `lastChunk` 应分别是 8、9、6、7（每条链上最后那个 chunk 的全局序号），`lastChunkLen` 都是 1 MiB（满块）。`queryChunks` 取其中最大的 `lastChunk=9`，于是 `length = 9 * 1MiB + 1MiB = 10 MiB`。
4. **预期结果**：最终长度 10 MiB，等于实际写入量。这验证了「取最大值 + 乘 chunkSize + 加尾块长度」的正确性。若 stripe 链返回的 lastChunk 出现重复非零值，源码会用 `DFATAL` 告警（[FileOperation.cc:51-56](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/meta/FileOperation.cc#L51-L56)）。
5. 具体的 `lastChunkLen`、`hasHole`（空洞检测，[FileHelper.cc:98-106](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/FileHelper.cc#L98-L106)）在真实集群上的取值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `sync` 用 `lengthHint`（客户端上报）而不是每次都查 storage？

> **答案**：因为写数据不经过 meta，meta 没有磁盘实时视图；每次 `sync` 都查 storage 会让 meta 成为吞吐瓶颈。`lengthHint` 是廉价的「乐观上推」，把昂贵的 storage 查询推迟到 close/fsync（`updateLength=true`）一次性对账。代价是 `length` 在 close 前可能短暂偏小。

**练习 2**：`truncateVer` 在并发场景下保护什么？

> **答案**：保护「截断后的短文件」不被「截断前发出的旧长度/hint」覆盖。源码里若新长度小于旧长度且非 truncate，直接 `FATAL`（[BatchOperation.cc:159-167](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/BatchOperation.cc#L159-L167)）；queryLength 也拒绝 `hintLength->truncateVer > currLength.truncateVer` 这种「来自未来截断版本」的 hint（[:280-285](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/BatchOperation.cc#L280-L285)）。

---

### 4.2 会话管理：FileSession 的建、删与回收

#### 4.2.1 概念说明

「文件长度可以延迟对账」带来一个副作用：在长度还没对账之前，meta 怎么知道这个文件**此刻还有人在写**？更关键的——如果有人正在写一个文件，而另一个进程把该文件 `unlink` 了，meta 能不能立刻删它的物理 chunk？

答案是不能，否则正在写的数据会凭空消失。3FS 用 `FileSession` 记录「该文件被某客户端以写方式打开」这一事实：只要文件还有任意一个写 session 存在，GC 就**不能**删它的 chunk（详见 4.3）。

`FileSession` 是一条很轻的记录，关键字段（[FileSession.h:25-30](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/FileSession.h#L25-L30)）：`inodeId`、`clientId`、`sessionId`（UUID）、`timestamp`、`payload`（占位）。它存在 FDB 里，key 形如 `INOS + inodeId + sessionId`（前缀 `INOS`，见 [KeyPrefix-def.h:15](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/kv/KeyPrefix-def.h#L15) 的 `DEFINE_PREFIX(InodeSession, "INOS")`）。注意只对**写打开**（`accessType != READ`）建 session。

> 直觉一句话：**`FileSession` 是写打开文件的「占位符」，挡住 GC 别误删正在写的数据。**

但 session 有个天然问题：客户端可能崩溃、断电、被 kill，来不及发 `close`。于是 session 会**泄漏**——文件已经没人写了，但 FDB 里还留着一个写 session，导致该文件的 chunk 永远 GC 不掉。3FS 用 `SessionManager` 周期扫描全量 session、回收「死客户端」遗留的 session 来解决。

#### 4.2.2 核心流程

session 的完整生命周期：

```text
create/open(写) ──► FileSession::create(inode, session).store(txn)   建 session
     │
     │ （客户端正常）close ──► BatchedOp::close 收集 session ──► syncAndClose 里 remove
     │
     │ （客户端崩溃，没 close）
     ▼
SessionManager 周期扫描（每 scan_interval，默认 5min）：
   ├─ 只在「第一个 meta」上跑（isFirstMeta），避免重复
   ├─ 分 256 个 shard 并行 scan
   ├─ 对每个 session：查 mgmtd listClientSessions 判断其 clientId 是否还活着
   │     ├─ 活着 → 跳过
   │     ├─ 刚建（timestamp+1min > now）→ 跳过（防与并发建 session 冲突）
   │     └─ 死了 → 标记待清理
   ├─ sync_on_prune_session=true → 先走 close+sync 对账长度，再删 session
   └─ sync_on_prune_session=false → 直接删 session KV
```

此外还有一条**客户端主动清理**的旁路：客户端在某些错误后（如发现 session 泄漏）会调 `pruneSession` RPC，把要清理的 session 写到一个**特殊 inode `InodeId(-1)`** 下（`createPrune`，[FileSession.h:66-68](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/FileSession.h#L66-L68)）。`SessionManager` 每轮扫描开头会先 `loadPrune` 把这些「客户端点名要删的 session」读出来，扫描时优先删它们（[SessionManager.cc:109-113](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/SessionManager.cc#L109-L113)）。`InodeId(-1)` 在这里纯粹当一个「全局待清理队列」的特殊键，不和任何真实文件冲突。

#### 4.2.3 源码精读

**session 的建与删（在批量操作里）**：建文件时，若以写方式打开且带 session，则在同一事务里建 session（[BatchOperation.cc:516-519](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/BatchOperation.cc#L516-L519)）；打开已存在文件同理（[:600-603](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/BatchOperation.cc#L600-L603)）：

```cpp
if (req.session && req.flags.accessType() != AccessType::READ) {
  openWrite.addSample(1);
  CO_RETURN_ON_ERROR(co_await FileSession::create(inode.id, req.session.value()).store(txn));
}
```

`close` 时把要删的 session 收集起来（[BatchOperation.cc:253-255](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/BatchOperation.cc#L253-L255)），由 `syncAndClose` 统一 `session.remove(txn)`（[:142-144](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/BatchOperation.cc#L142-L144)）。`store`/`remove` 就是写/清 `INOS` 那个 key（[FileSession.cc:207-228](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/FileSession.cc#L207-L228)）。

**session 的 KV 编码**（[FileSession.cc:22-41](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/FileSession.cc#L22-L41)）：`SessionByInode` 把 key 编成 `keyPrefix(INOS) + inodeId.packKey() + sessionId`。这样**同一个 inode 的所有 session 在 key 空间里连续**，可以用一次范围查询列出（`FileSession::list`），也能用 `snapshotCheckExists` 只取一个判断「该文件是否有 session」（[FileSession.cc:172-193](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/FileSession.cc#L172-L193)）。

**session 与 GC 的互锁**：GC 删文件前会 `snapshotCheckExists` 看有没有 session（见 4.3.3）。而在写事务里还有个 `checkExists`（[FileSession.cc:158-170](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/FileSession.cc#L158-L170)）：当发现「此刻没有 session」时，它**额外把 session 的 key 区间加进读冲突范围**。这保证了「GC 判定无 session 准备删」与「并发地有客户端正在建 session」必然在 FDB 层面冲突、其中一方重试，不会出现「GC 删了 chunk，session 才建上」的竞态。

**死 session 回收（SessionManager）**：周期任务入口 `scanTask`（[SessionManager.cc:252-272](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/SessionManager.cc#L252-L272)）只在第一个 meta 上跑，先 `loadPrune`（[:274-288](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/SessionManager.cc#L274-L288)，读 `InodeId(-1)` 下的待清理 session），再为 256 个 shard 各投一个 `ScanTask`。

`ScanTask::run`（[SessionManager.cc:78-186](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/SessionManager.cc#L78-L186)）是核心，判断「死活」靠 `getActiveClients`（[:43-73](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/SessionManager.cc#L43-L73)）——它向 mgmtd 的 `listClientSessions` 要当前所有活着的 client，并叠加一个 `session_timeout`（默认 5min）的超时过滤：

```cpp
for (auto &session : *sessions) {
  if (prune_->sessions.rlock()->contains(session.sessionId)) { ... }   // 客户端点名要删的
  else if (active->contains(session.clientId)) continue;              // 还活着
  else if (session.timestamp + 1_min > ts) continue;                  // 刚建，给 1min 宽限
  deadSessions.push_back(session);                                    // 判定死亡
}
```

回收分两路（[:131-151](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/SessionManager.cc#L131-L151)）：`sync_on_prune_session=true` 时投 `CloseTask`（先 close+sync 对账长度再删，[CloseTask::run](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/SessionManager.cc#L189-L215)），否则直接在一个事务里批量 `remove`。

**客户端主动 prune**：`MetaClient::pruneSession`（[MetaClient.cc:671-679](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/meta/MetaClient.cc#L671-L679)）把 session 攒批后发 `pruneSession` RPC；服务端 `PruneSessionOp`（[PruneSession.cc:62-66](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/PruneSession.cc#L62-L66)）用 `createPrune` 把它们存到 `InodeId(-1)` 下，等后台扫描处理。

#### 4.2.4 代码实践：源码阅读型——画出 session 的「建-锁-清」三态

1. **实践目标**：把 session 在三种情形下的命运对应到具体源码位置，确认你理解「为什么 GC 删 chunk 前必须查 session」。
2. **操作步骤**：
   - 在仓库里检索 `FileSession::create(...).store` 的所有调用点，确认它们都满足 `accessType != READ`。
   - 检索 `snapshotCheckExists` 与 `checkExists` 的调用点，分别在 GC 路径（`GcManager::gcFile`）和写路径里各找一个。
   - 检索 `SessionManager` 的 `isFirstMeta` 判断（[SessionManager.cc:257-260](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/SessionManager.cc#L257-L260)），思考为什么扫描只在一个 meta 上跑。
3. **需要观察的现象**：session 的「建」永远在写打开的事务里、与 inode 同事务提交；「删」在 close 事务或后台扫描事务里；GC 删 chunk 前必有一次 `snapshotCheckExists`。
4. **预期结果**：三者形成闭环——只要还有写 session，GC 就被挡住（返回 `kBusy`）；session 一旦清掉，GC 才能真正删 chunk。具体日志中 `Delay gc file ..., still has session` 的触发频率**待本地验证**。
5. 若想观察真实 session，可用 `admin_cli` 的 session 相关命令（`SessionManager` 暴露了 `listSessions` / `listSessions(inodeId)` / `pruneManually`，[SessionManager.h:81-83](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/SessionManager.h#L81-L83)）查看集群里当前有哪些写 session。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `FileSession::checkExists`（写事务版）在「发现没有 session」时要额外加读冲突范围？

> **答案**：见 [FileSession.cc:162-167](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/FileSession.cc#L162-L167)。`snapshotCheckExists` 是快照读，不进冲突范围；如果调用方据此决定「无 session、可以删」，而此刻恰好有客户端正在建 session，两者就会并发。`checkExists` 在「无 session」时补一段读冲突范围，使得「建 session 的写」与「判定无 session 的删」在 FDB 层冲突，迫使一方重试，消除竞态。

**练习 2**：`SessionManager` 的扫描为什么用 256 个 shard 并行？

> **答案**：因为 `FileSession::scan` 按 `inodeId` 的高位分桶（`kShard = 256`，[FileSession.h:56-57](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/FileSession.h#L56-L57)），每个 shard 是一段连续的 key 区间（[FileSession.cc:230-248](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/FileSession.cc#L230-L248)）。分 shard 后可投到 `scanWorkers`（默认 8 协程）并行扫描与回收，把一次全量扫描的延迟摊开，也避免单个超大事务。

---

### 4.3 延迟删除与 GC：不立刻删 chunk 的艺术

#### 4.3.1 概念说明

传统文件系统 `unlink` 一个文件，若 nlink 归零且没人打开，就立刻释放数据块。3FS **不立刻删 chunk**，而是把「待删 inode」写进一个特殊的 **GC 目录**，由后台扫描按节奏回收。这样设计有两个动机：

1. **避免误删正在写的数据**：如 4.2 所述，文件可能还有写 session。立即删 chunk 会破坏正在写的客户端。
2. **削峰与可控回收**：删大量 chunk 是重 I/O 操作。延迟回收让删除流量可调度——可以等集群空闲、或按优先级慢慢删，避免 `unlink` 一个大目录瞬间压垮 storage。

具体做法：`unlink` 时，若 nlink 归零，meta 不删 inode，而是在 GC 目录下建一条**目录项（DirEntry）**，它的**名字**编码了「类型前缀 + 时间戳 + inodeId」。后台 `GcDirectory` 周期扫描这些条目，凡是时间戳早于「现在 − 延迟」的，才取出执行真正的删 chunk。

> 直觉一句话：**`unlink` 只是「把文件搬进一个带时间戳的回收站」，真正删 chunk 由后台 GC 按延迟节奏慢慢做。**

名字里的**类型前缀**还顺便承担了「按文件大小分级、定优先级」的作用：大文件优先级高（`HI_PRI`）、小文件优先级低（`LO_PRI`）、中等与目录走中优先级。

#### 4.3.2 核心流程

GC 条目名字格式（[GcManager.h:70-72](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/GcManager.h#L70-L72)）：

\[
\text{name} = \underbrace{\text{prefix}}_{\text{类型}} + \text{"-"} + \underbrace{\text{timestamp(20 位补零)}}_{\text{决定何时可被回收}} + \text{"-"} + \text{inodeIdHex}
\]

例如 `L-0000000001717...-0x1234` 表示一个大文件（`L`）的待删条目。时间戳用 20 位补零的微秒数，使得**按名字字典序排列 ≈ 按时间排列**——这正是延迟 GC 能用「范围扫描 `name < endkey`」实现的关键（`endkey` 用「现在 − 延迟」生成）。

类型与优先级（[GcManager.h:124-130](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/GcManager.h#L124-L130) 与 [:166-194](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/GcManager.h#L166-L194)）：

| 类型 | 前缀 | 含义 | 优先级 |
| --- | --- | --- | --- |
| `DIRECTORY` | `d` | 目录 | MID |
| `FILE_MEDIUM` | `f` | 中等文件 | MID |
| `FILE_LARGE` | `L` | 大文件（chunk 数 ≥ large_file_chunks） | HI |
| `FILE_SMALL` | `S` | 小文件（chunk 数 < small_file_chunks） | LO |

完整回收流程：

```text
unlink(文件) → nlink-1 → nlink==0?
   │ 否 → 只更新 nlink
   │ 是
   ▼
GcManager::removeEntry → gcDirectory->add(inode)
   → addFile: 按 chunk 数选前缀(L/f/S)，建 DirEntry(name=prefix-ts-inode)，存入 GC 目录
       （此时 chunk 仍在 storage 上，没删）
   ▼
GcDirectory 周期扫描（每 scan_interval）：
   endkey = formatGcEntry(prefix, now - delay, root)
   范围扫描 name < endkey 的条目（即「足够老」的）→ 投 GcTask（带优先级）
   ▼
GcTask::gcFile:
   ├─ 若 check_session：snapshotCheckExists，还有 session → 返回 kBusy，moveToTail 延后再试
   ├─ fileHelper->remove(...) 删掉该文件所有 chunk
   └─ 删 GC 条目 + 删 inode
```

**延迟的开关**：`enableGcDelay()`（[GcManager.cc:863-879](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/GcManager.cc#L863-L879)）——当集群**空闲空间不足**（低于 `gc_delay_free_space_threshold`）时，自动**取消延迟**，让 GC 赶紧追上以腾出空间；空间充裕时才启用延迟，慢慢删。

**失败重试**：`GcTask` 失败时，若错误「关键」（critical）或 `kBusy`，调用 `moveToTail`（[GcManager.cc:225-238](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/GcManager.cc#L225-L238)）把条目的时间戳**往后推一个 retry_delay**，相当于「挪到队尾」下次再试，而不是死循环重试。

#### 4.3.3 源码精读

**建 GC 条目**：`unlink` 的 RPC 落点 `RemoveOp` 在 nlink 归零时调用 `gcManager().removeEntry`（[Remove.cc:185](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/Remove.cc#L185)）。`GcManager::removeEntry`（[GcManager.cc:754-807](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/GcManager.cc#L754-L807)）把 nlink 减一，归零后 `gcDirectory->add(txn, inode, ...)`，而 `addFile`（[:201-214](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/GcManager.cc#L201-L214)）按 chunk 数选前缀、用 `formatGcEntry` 生成名字并建目录项：

```cpp
auto chunks = inode.asFile().length / inode.asFile().layout.chunkSize;
if (chunks >= config.large_file_chunks()) prefix = prefixOf(GcEntryType::FILE_LARGE);     // 'L'
if (chunks <  config.small_file_chunks()) prefix = prefixOf(GcEntryType::FILE_SMALL);     // 'S'
auto entry = DirEntry::newFile(dirId(), formatGcEntry(prefix, UtcClock::now(), inode.id), inode.id);
co_await entry.store(txn);
```

**扫描与调度**：每个 `GcDirectory` 为四种类型各起一个扫描协程（[GcManager.cc:73-83](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/GcManager.cc#L73-L83)）。`scan` 的实现（[:121-178](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/GcManager.cc#L121-L178)）用「`now - delay`」生成 `endkey`，范围扫描 `name < endkey`，对未在队列里的条目投 `GcTask`（带 `priorityOf(type)` 优先级）：

```cpp
auto endtime = UtcTime::fromMicroseconds(UtcClock::now().toMicroseconds() - delay.asUs().count());
std::string endkey = formatGcEntry(prefix, endtime, InodeId::root());
... // 范围扫描 [beginkey, endkey) → manager.gcWorkers_->enqueue(task, priorityOf(type));
```

`queued`/`finished` 两个集合防止重复投递与重复扫描（[:141-175](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/GcManager.cc#L141-L175)）。

**真正删 chunk（gcFile）**：[GcManager.cc:371-457](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/GcManager.cc#L371-L457)。先（若 `check_session` 开启）`snapshotCheckExists` 看有没有 session，有就 `kBusy` 延后：

```cpp
auto [inode, session] = co_await collectAll(Inode::snapshotLoad(txn, id), FileSession::snapshotCheckExists(txn, id));
if (*session) { XLOGF(CRITICAL, "Delay gc file {}, still has session {}.", id, ...); co_return kBusy; }
```

然后 `fileHelper_->remove(...)` 删该文件所有 chunk（内部按 stripe 分链删除，类似 queryChunks 的分摊），最后删 GC 条目与 inode（`removeGcEntryAndInode`，[:614-618](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/GcManager.cc#L614-L618)）。

**GC 目录的组织**：每个 meta 实例有 `kNumGcDirectoryPerServer = 5`（4+1）个 GC 目录（[GcManager.cc:60](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/GcManager.cc#L60)），名字如 `GC-Node-{nodeId}`（主）、`GC-Node-{nodeId}.1`~`.4`。`distributed_gc` 开启时，`pickGcDirectory`（[GcManager.h:240-253](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/GcManager.h#L240-L253)）会从**所有活跃 meta 的 GC 目录**里随机挑一个写入，把删除负载在 meta 之间分摊（`scanAllGcDirectories` 维护这张活跃目录表，[:809-861](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/GcManager.cc#L809-L861)）。

**并发度与低优先级事务**：GC 用两个信号量（`concurrentGcDirSemaphore_`/`concurrentGcFileSemaphore_`）限制目录/文件回收的并发（[GcManager.h:92-93](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/GcManager.h#L92-L93)），并发度可热更新（[:95-108](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/GcManager.h#L95-L108)）；且 GC 事务可设为 `FDB_TR_OPTION_PRIORITY_BATCH`（低优先级，[:309-312](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/GcManager.cc#L309-L312)），避免和用户请求抢资源——这些都是「让删除尽量不碍事」的工程取舍。

#### 4.3.4 代码实践：源码阅读型——还原一个 GC 条目的「时间线」

1. **实践目标**：把一个被 `unlink` 的大文件，从「搬进回收站」到「chunk 真正被删」的完整时间线对上源码。
2. **操作步骤**：
   - 假设某文件 `length = 1 GiB`、`chunkSize = 1 MiB`，故 `chunks = 1024`；设 `large_file_chunks = 64`、`small_file_chunks = 4`。判断 `addFile` 给它选哪个前缀。
   - 设 `gc_file_delay = 10 min`、当前空间充足（`enableGcDelay() = true`）。一条 `formatGcEntry('L', T0, inodeId)` 的名字在 `T0` 时刻建好。
   - 推算：扫描器在 `T0`、`T0+5min`、`T0+10min`、`T0+11min` 各次扫描时，这条目是否会被取出（`name < formatGcEntry('L', now-10min, root)` 是否成立）。
3. **需要观察的现象**：`T0` 和 `T0+5min` 时 `now-10min < T0`，条目不在扫描范围（被延迟）；`T0+10min` 起进入范围，被投成 `GcTask`（`HI_PRI`）。
4. **预期结果**：延迟期内 chunk 不被删；延迟期满且无 session 时，`gcFile` 经 `fileHelper_->remove` 删掉 1024 个 chunk，再删条目与 inode。若期间恰好有写 session，会被 `Delay gc file ... still has session` 挡住（kBusy）并 `moveToTail` 延后。真实 `gc_file_delay`/`large_file_chunks` 等默认值**待本地验证**（见 [GcManager.h:124-130](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/GcManager.h#L124-L130) 及配置）。
5. 进阶：对比 `enableGcDelay()` 在「空闲空间充足」与「不足」下的返回值（[GcManager.cc:863-879](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/GcManager.cc#L863-L879)），解释「空间紧张时为何要取消延迟」。

#### 4.3.5 小练习与答案

**练习 1**：GC 条目的名字为什么要把时间戳放在中间、且补零到 20 位？

> **答案**：因为 FDB 与目录项都按 key/名字的字典序排列。把时间戳补零到固定位数（20 位微秒），字典序就等于时间序；于是「延迟回收」只需用 `now - delay` 生成一个 `endkey`，范围扫描所有 `name < endkey` 的条目，天然就是「足够老、可以删」的那批。把 inodeId 放最后，是为了让同一时刻多个条目仍能区分（[GcManager.h:70-72](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/GcManager.h#L70-L72)）。

**练习 2**：`unlink` 一个还在被写的大文件，chunk 会立刻被删吗？请用 session 解释。

> **答案**：不会。`unlink` 只把它搬进 GC 目录（`removeEntry`→`addFile`），chunk 仍在。后台 `gcFile` 执行前会 `snapshotCheckExists`，只要该文件还有写 session，就返回 `kBusy` 并 `moveToTail` 延后（[GcManager.cc:386-394](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/GcManager.cc#L386-L394)）。只有等所有写 session 都清掉（客户端正常 close 或 SessionManager 回收死 session），GC 才真正删 chunk。

---

## 5. 综合实践

把三个模块串起来，推演「多客户端并发写 → 某客户端崩溃 → 文件被删」的完整生命周期。这正是本讲规格里要求的实践任务。

**场景**：客户端 C1、C2 同时以写方式打开同一个文件 `F`（各自有独立 `sessionId`），交替写入；写到一半 C2 崩溃；随后 C1 正常 close；最后另一进程把 `F` 删除。

**任务**：逐步回答以下问题，并指出每一步对应的源码位置。

1. **建 session**：C1、C2 的 open 各自建了几个 session？key 长什么样？为什么同一个 `F` 的两个 session 在 key 空间里连续？
2. **长度演进（写阶段）**：写过程中 C1/C2 周期发 `sync`（带 `lengthHint`）。meta 的 `length` 会怎么变？meta 此时查 storage 了吗？为什么 `length` 可能短暂偏小？
3. **C2 崩溃**：C2 的 session 谁来清？`SessionManager` 怎么判定 C2 已死（依赖哪个 mgmtd 接口、用什么时钟、给多长宽限）？若 `sync_on_prune_session=true`，清 session 前还多做了一步什么、为什么？
4. **C1 正常 close**：C1 的 close 触发 `updateLength=true`，meta 走 `queryLength`。请说明此时是否一定查 storage、查 storage 时如何「分摊到 stripeSize 条链」、最终长度怎么取。C1 的 session 何时被删？
5. **删除 `F`**：`unlink` 后 nlink 归零，meta 做了什么（建什么样的 GC 条目）？此时 C2 的死 session 若还没被 SessionManager 清掉，`gcFile` 会怎样？直到什么条件满足，`F` 的 chunk 才被真正删除？
6. **兜底**：整个过程中，若 C1 close 对账长度与并发改写撞上，`BatchedOp::run` 的 versionstamp sanity check 如何触发重试？

**参考要点**：

1. 各建 1 个写 session（[BatchOperation.cc:516-519](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/BatchOperation.cc#L516-L519)）。key = `INOS + F.inodeId + sessionId`；两个 session 共享 `INOS+F.inodeId` 前缀，故连续（[FileSession.cc:22-41](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/FileSession.cc#L22-L41)）。
2. `length` 按 `mergeHint` 取最大上推，**不查 storage**（[BatchOperation.cc:260-286](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/BatchOperation.cc#L260-L286)）；因 hint 是「已发出写位置」的乐观上报，可能有数据在途，故 `length` 可能偏小。
3. 由 `SessionManager` 周期扫描回收：经 `getActiveClients` 问 mgmtd `listClientSessions`，叠加 `session_timeout`（默认 5min）超时判定 C2 死亡，并给新建 session 1min 宽限（[SessionManager.cc:43-73](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/SessionManager.cc#L43-L73)、[:107-127](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/SessionManager.cc#L107-L127)）。`sync_on_prune_session=true` 时先投 `CloseTask` 走 close+sync 对账 `F` 的长度再删 session（[:189-215](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/SessionManager.cc#L189-L215)），避免长度丢失。
4. close 带 `updateLength=true` 触发对账：若 hint 已领先则采信 hint，否则 `fileHelper().queryLength` 向 storage 查；查时 `queryChunksByChain` 为每条 stripe 链并发查最后 chunk、取最大值（[FileOperation.cc:78-179](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/meta/FileOperation.cc#L78-L179)）。C1 的 session 在 `syncAndClose` 里 `remove`（[BatchOperation.cc:142-144](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/BatchOperation.cc#L142-L144)、[:253-255](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/BatchOperation.cc#L253-L255)）。
5. `unlink` 后 nlink 归零，`removeEntry`→`addFile` 建一条 `prefix-ts-inodeId` 的 GC 条目，chunk 暂不删（[GcManager.cc:201-214](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/GcManager.cc#L201-L214)）。若 C2 死 session 还在，`gcFile` 的 `snapshotCheckExists` 命中，返回 `kBusy` 并 `moveToTail` 延后（[:386-394](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/GcManager.cc#L386-L394)）。直到 C2 session 被清、延迟期满，`gcFile` 才 `fileHelper_->remove` 删 chunk 并删条目与 inode（[:413-446](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/GcManager.cc#L413-L446)）。
6. 并发改写使 versionstamp 变化，sanity check 发现 `currLength_`/`nextLength_` 都与 inode 当前长度不符，返回 `kBusy` 要求重试（[BatchOperation.cc:69-84](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/BatchOperation.cc#L69-L84)），由 `OperationDriver` 重跑（见 u4-l3）。

## 6. 本讲小结

- 文件长度采用**最终一致性**：`VersionedLength = (length, truncateVer)`；平时 `sync` 用客户端上报的 `lengthHint`（`mergeHint` 取最大）廉价上推、不查 storage，只有 `close/fsync`（`updateLength=true`）才向 storage 对账权威长度。
- 对账时 `queryChunksByChain` 把「查长度」拆成「并行查每条 stripe 链上最后一个 chunk」，取最大值还原长度，查询压力分摊到 `stripeSize` 条链；并发改写由 `BatchedOp::run` 的 versionstamp sanity check 检测并重试。
- `FileSession`（`INOS` 前缀，仅写打开建立）是「文件还在被写」的占位符；正常 close 时删，客户端崩溃则由 `SessionManager` 周期扫描（256 shard、只在第一个 meta、靠 mgmtd `listClientSessions`+超时判死活）回收，客户端也可主动 `pruneSession` 把待清 session 写到 `InodeId(-1)` 优先处理。
- `unlink` 不立刻删 chunk：nlink 归零时在 GC 目录建一条 `prefix-ts-inodeId` 的目录项（前缀按大小分级并定优先级），后台 `GcDirectory` 按「`now − delay`」范围扫描延迟回收；`gcFile` 删 chunk 前必查 session，有 session 则 `kBusy` 并 `moveToTail` 延后。
- 延迟 GC 受空闲空间调控（`enableGcDelay`：空间紧张时取消延迟加速回收），并发度可热更新，事务可降为低优先级——一切为了「让删除尽量不误删、不碍事」。
- 三个机制闭环：session 挡住 GC 误删正在写的数据，GC 的延迟回收又给 session 清理（尤其崩溃回收）留出时间窗口；长度对账与 session 删除都在 `BatchedOp` 的同一批事务里完成，保证一致性。

## 7. 下一步学习建议

- **batch 操作与重试**：本讲的长度/session/GC 都落在 `BatchedOp` 与 `OperationDriver` 之上。批量如何聚合、整段重试如何由 `FDBRetryStrategy` 退避，见 u4-l3（用 FDB 事务实现元数据操作）。
- **chunk 在 storage 上怎么被删**：本讲到 `fileHelper_->remove` 为止。它最终向 storage 发 remove 请求，背后是 chunk engine 的物理块回收，见 u6（Chunk Engine，Rust 实现），尤其是物理块分配器与 RocksDB 元数据的写批。
- **数据恢复期的 session/GC 交互**：当一个 target 离线重启做全量回放（u5-l5），文件可能短暂处于「链不全」状态，此时 close/fsync 的 `queryChunks`、GC 的 `removeChunks` 行为值得结合 u5 一并读。
- **客户端侧如何使用 session/length**：本讲讲的是 meta 侧。客户端 `MetaClient` 怎么发 sync/close、怎么在错误后 `pruneSession`、怎么本地缓存 `hintLength`（[FuseOps.cc:2666](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseOps.cc#L2666)），见 u7（客户端）。
