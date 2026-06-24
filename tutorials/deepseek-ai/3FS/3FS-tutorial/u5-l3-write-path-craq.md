# 写路径与 CRAQ 链式复制

## 1. 本讲目标

本讲深入 3FS storage 服务的**写数据路径**，拆解它如何用 CRAQ（Chain Replication with Apportioned Queries）在一条复制链上实现强一致的多副本写入。读完本讲你应该能够：

- 说清一次客户端写请求从「到达链头」到「全部副本落盘」所经过的 **五步流水线**，以及为什么只有链头能接收客户端写。
- 理解每个 chunk 上同时维护的**两个版本号**——`updateVer`（pending，待确认）与 `commitVer`（committed，已确认）——以及它们必须满足的不变量 \( \text{commitVer} \le \text{updateVer} \le \text{commitVer}+1 \)。
- 读懂写请求如何**沿链向下转发**（head→…→tail），提交确认又如何**作为 ACK 沿链向上回传**（tail→…→head），并理解 `ReliableForwarding` 的重试与 `ReliableUpdate` 的幂等缓存为什么是必不可少的。

本讲承接 [u5-l1（storage 服务总览与启动）](u5-l1-storage-overview.md) 引入的 `StorageOperator`、`TargetMap`、`StorageTarget`、协程池分流等概念；链与 target 的数据结构、版本号语义请回顾 [u3-l4（ChainTable/Chain/Target 数据模型）](u3-l4-chain-target-model.md) 与 [u3-l5（Target 状态机）](u3-l5-target-state-machine.md)。

## 2. 前置知识

### 2.1 什么是 CRAQ

CRAQ 是链式复制（Chain Replication）的强一致变体，核心口诀是「**写全读任何**（write-all, read-any）」：

- **写全**：一次写请求必须写入链上的**所有**副本才算完成。具体做法是把请求从链头（head）开始，沿链一个接一个地传递到链尾（tail），每个副本都先写本地、再转发给下一个。
- **读任何**：读请求可以打到链上**任意**一个副本，而不必固定走链尾，从而把读吞吐摊到所有副本上。

为了保证「读任何」还能读到强一致的数据，CRAQ 给每个对象（在本讲里就是一个 chunk）引入**两个版本号**：

| 名称 | 含义 | 别名 |
|---|---|---|
| `commitVer`（committed version） | 已经被链尾确认、对所有副本都一致可见的版本 | 已提交版本 |
| `updateVer`（pending/dirty version） | 本副本已经写入、但还没拿到链尾确认的版本 | 待确认版本 |

不变量是 \( \text{commitVer} \le \text{updateVer} \le \text{commitVer}+1 \)，即一个 chunk 在任一副本上**最多只有一个未确认的写**。本讲后面会反复回到这个不变量。

> 3FS 的具体实现做了一点务实简化：当读到一个「脏」chunk（`commitVer != updateVer`）时，并不去链尾拉取最新版本，而是直接返回「未提交」错误，由客户端换一个副本重试。这样做的前提是写提交速度极快（见 4.3 的 ACK 回传），脏窗口非常短。细节属于读路径，见 [u5-l2（读路径：批量读与 AIO）](u5-l2-read-path-aio.md)。

### 2.2 链、target 与 chunk 的关系速览

- 一条 **Chain** 由若干 **Target** 有序排列（head 在前、tail 在后），每个 target 住在一个 storage 节点的一块 SSD 上。
- 一个 **chunk** 是文件数据按 `chunkSize` 切出来的基本单位，用 `ChunkId` 标识；它会被复制到一条链的**每一个** target 上。
- 写一个 chunk = 把这段数据依次写到链上所有 target。本讲的主角就是「写一个 chunk」这条路径。

### 2.3 你需要记住的版本号字段

源码里反复出现三个版本号，先认全：

- `ChunkVer updateVer` / `ChunkVer commitVer`：chunk 的待确认 / 已提交版本（存在 chunk 元数据里）。
- `ChainVer chainVer`：**整条链**的版本号。链成员一变（加副本、摘副本），`chainVer` 就单调递增，用来拒绝「针对旧链」的过期写请求（见 [u3-l4](u3-l4-chain-target-model.md)）。
- `ChainVer commitChainVer`：提交时携带的链版本，用来让提交也带上链的身份校验。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| `src/storage/service/StorageOperator.h/.cc` | 写路径的**总协调者**。`update()`/`write()` 是 RPC 入口，`handleUpdate()` 是 CRAQ 五步流水线的主体。 |
| `src/storage/service/ReliableUpdate.h/.cc` | 在 `handleUpdate` 之上包一层**幂等 + 通道去重**，让网络重试不会造成重复写入。 |
| `src/storage/service/ReliableForwarding.h/.cc` | 负责把写请求**转发给后继 target**，并处理重试、`SYNCING` 全量回放等复杂情况。 |
| `src/storage/update/UpdateWorker.h/.cc` | 落盘的**线程池**：按磁盘分队列，串行化同一磁盘上的 chunk 写入。 |
| `src/storage/update/UpdateJob.h` | 一次「写」或「提交」任务的载体，封装 `UpdateIO`/`CommitIO` 与完成信号。 |
| `src/storage/store/ChunkReplica.cc` | chunk 元数据上**版本号的真正赋值与校验**逻辑（`update()` / `commit()`），是双版本不变量的执行点。 |
| `src/fbs/storage/Common.h` | `Target`、`UpdateIO`、`CommitIO`、`IOResult`、`UpdateType`/`ChunkState` 等数据结构定义。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**链头加锁**（4.1）、**双版本**（4.2）、**转发与 ACK**（4.3）。开篇 4.0 先给一张全局流水线图，把三者串起来。

### 4.0 全局视角：一次写入的五步流水线

`StorageOperator::handleUpdate` 是整条写路径的主干，它把 CRAQ 的「先写本地、再转发、收到确认后再提交」固化成清晰的五步：

```text
                  ┌─────────────────────────────────────────────────┐
   客户端写请求 ──▶│ 1. 校验：必须是链头、非只读、chunkId 非空         │
   (fromClient)   │ 2. lockChunk：按 chunk 加锁，串行化同一 chunk 的写 │
                  │ 3. doUpdate：本地落盘，分配/校验 updateVer(pending)│
                  │ 4. forwardWithRetry：把写转发给后继 target         │
                  │ 5. doCommit：后继确认后，本地把 commitVer 提到 v+1  │
                  └─────────────────────────────────────────────────┘
```

先记住这五步的顺序，下面三节分别展开「第 2 步的锁、第 3 步的版本、第 4 步的转发」。

#### 4.0.1 入口：write 与 update 两个 RPC

storage 服务对外暴露两个写相关 RPC，差别只在「谁来」：

- [`write()`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L233-L282)：客户端直连链头的入口。它把 `WriteReq` 改写成 `UpdateReq`，并打上 `options.fromClient = true`，然后交给 `reliableUpdate.update()`。注意它在改写时**显式标记来自客户端**——这是后面「只有链头能收客户端写」的依据。
- [`update()`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L284-L331)：**链上前驱转发给本节点**的入口。`fromClient` 默认是 `false`。

二者最终都汇入 [`components_.reliableUpdate.update(...)`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L310-L314)（REMOVE 且 `channel.id==0` 的特殊情况才直连 `handleUpdate`）。也就是说，无论客户端写还是服务间转发，都要先过 ReliableUpdate 的幂等层，再进 `handleUpdate` 的五步流水线。

> 关键结论：`fromClient` 这个布尔位把「客户端写」和「链内转发」区分开。它不是性能开关，而是一致性开关——用来在 `handleUpdate` 第 1 步强制校验「客户端写只能落在链头」。

### 4.1 链头加锁：串行化写入的两道关卡

「链头加锁」其实包含**两道不同层次的锁**，理解它们的分工是本节的核心。

#### 4.1.1 概念说明

CRAQ 要求所有客户端写都从链头进入，沿链串行传播。这意味着同一 chunk 上的写**必须被串行化**，否则两个并发写会让 `updateVer` 错乱、破坏双版本不变量。3FS 用两层机制实现串行化与幂等：

1. **chunk 锁（阻塞式）**：`handleUpdate` 在写本地 chunk 前，对**该 chunk** 加一把可 `co_await` 的锁。同一 chunk 的写排队；不同 chunk 的写并发。这是 CRAQ 流水线内部的串行化。
2. **通道锁 + 结果缓存（非阻塞式）**：`ReliableUpdate` 在 `handleUpdate` **之外**再加一层，按「客户端 + 通道」去重，并缓存上次的执行结果，使网络重试变成幂等的「查缓存」。

两者是叠加关系：先过 ReliableUpdate 的幂等层（决定要不要真的执行），再进 `handleUpdate` 的 chunk 锁（决定执行的先后）。

#### 4.1.2 核心流程

`handleUpdate` 的前两步——校验链头身份、加 chunk 锁——代码非常短：

```cpp
// 第 1 步：只有链头能接收客户端写
if (UNLIKELY(req.options.fromClient && !target->isHead)) {
  co_return makeError(StorageClientCode::kRoutingError, "non-head node receive a client update request");
}
// ... 只读 / 空 chunkId / syncing 下禁止 truncate 等 校验 ...

// 第 2 步：按 chunk 加锁
folly::coro::Baton baton;
auto lockGuard = target->storageTarget->lockChunk(baton, req.payload.key.chunkId, fmt::to_string(req.tag));
if (!lockGuard.locked()) {
  co_await lockGuard.lock();   // 没抢到就挂起协程，等前任写完
}
// 拿到锁后，重新查一次 TargetMap，防止链版本在等锁期间发生了变化
auto targetResult = components_.targetMap.getByChainId(req.payload.key.vChainId);
```

这里有三个值得注意的设计：

- **`isHead` 字段**：来自 [`Target` 结构体](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/storage/Common.h#L685-L710)，由 `TargetMap` 根据当前链的成员顺序算出（链上第一个 `SERVING` 副本即 head）。链成员一变，`isHead`/`isTail`/`successor` 会随新版路由信息更新。
- **锁是协程友好的**：`lockChunk` 内部用 `CoLockManager`（见 `StorageTarget.h`），抢不到锁时 `co_await lockGuard.lock()` 只挂起协程、不阻塞线程，正好契合 u2-l3 讲过的「数万协程配十几线程」模型。
- **拿锁后重查 TargetMap**：等锁期间，链可能发生了重排（比如 head 换人）。所以拿到锁后**立刻重查** `getByChainId` 拿到最新的 target 与 `chainVer`，避免基于过期路由继续写。

#### 4.1.3 源码精读

- [`handleUpdate` 的链头校验与 chunk 加锁](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L333-L388)：这段同时包含「fromClient 必须是 head」「只读拒绝」「syncing 拒绝 truncate/extend」三道校验，以及 `lockChunk` + 拿锁后重查 `TargetMap`。这是五步流水线的第 1、2 步。
- [`StorageTarget::lockChunk` 声明](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/StorageTarget.h#L60-L66)：一行转调 `CoLockManager::lock`，按 `chunkId` 作 key；旁边的 `tryLockChannel` 是给 ReliableUpdate 用的非阻塞通道锁。
- [`ReliableUpdate::update` 的幂等层](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/ReliableUpdate.cc#L16-L127)：这是「第二道关卡」。下面拆开看。

`ReliableUpdate` 用一个按 `clientId` 分片（`Shards<ClientMap, 1024>`）的缓存，key 是 `(chainId, channelId)`，value 是上次执行的结果（`ReqResult`，含 `channelSeqnum`、`requestId`、`updateResult`、`succUpdateVer`、`generationId`）。它的判重逻辑：

```cpp
// 同一通道并发：非阻塞试锁，抢不到就让客户端重试（kChannelIsLocked）
auto lock = target->storageTarget->tryLockChannel(baton, ...);
if (!lock.locked()) { co_return makeError(StorageCode::kChannelIsLocked); }

// 序号倒退：客户端发了一个比已执行序号更老的请求 → 重复，拒绝
if (req.tag.channel.seqnum < reqResult->channelSeqnum)
  co_return makeError(StorageClientCode::kDuplicateUpdate);

// 序号相同且 generationId 未变 → 命中缓存，直接回放上次结果（幂等）
if (req.tag.channel.seqnum == reqResult->channelSeqnum &&
    target->storageTarget->generationId() == reqResult->generationId) {
  // ...校验 requestId、updateVer 后，co_return 缓存的 updateResult...
}
// 否则：这是一次新写入，真正调用 handleUpdate，并把结果写回缓存
```

> 为什么需要 `generationId`？它是一个「目标实例的全局递增编号」。若本地 chunk engine 被重建（generationId 变了），缓存里的历史结果就失效了，必须重新执行。这是防止「缓存命中却对应了另一份物理数据」的安全阀。

#### 4.1.4 代码实践：阅读型——跟踪两道锁的差别

1. **实践目标**：分清 `chunk 锁`（阻塞、按 chunk）与 `通道锁`（非阻塞、按 client+channel）的用途差异。
2. **操作步骤**：
   - 在 `ReliableUpdate.cc` 中找到 `tryLockChannel` 与 `kChannelIsLocked`，确认它是**非阻塞**的（抢不到立即返回错误，而不是 `co_await`）。
   - 在 `StorageOperator.cc` 的 `handleUpdate` 中找到 `lockChunk` 与 `co_await lockGuard.lock()`，确认它是**阻塞**的。
   - 在 `StorageTarget.h` 中确认两者都来自同一个 `CoLockManager` 模板，只是分别用 `lock`（阻塞）和 `tryLock`（非阻塞）两个接口。
3. **需要观察的现象**：通道锁用的是 `tryLock`，因此同一个通道的**并发**写会被立即拒绝（客户端重试），而不是排队等待；而 chunk 锁用 `lock`，保证同一 chunk 的写**一定**按顺序执行。
4. **预期结果**：你能用一句话说清——通道锁挡「重复/并发请求」，chunk 锁挡「乱序执行」。
5. **运行结果**：待本地验证（本实践为源码阅读型，无需运行集群）。

#### 4.1.5 小练习与答案

**练习 1**：如果客户端把同一个写请求（相同 `MessageTag`）因为网络超时重发了三次，会发生什么？

**参考答案**：第一次真正执行，结果写入 `ReliableUpdate` 的 `ReqResult` 缓存。后两次因为 `channel.seqnum == channelSeqnum` 且 `generationId` 未变、`requestId` 相同，直接命中缓存分支，回放第一次的结果，不会重复落盘。这就是 at-most-once 的幂等保证。

**练习 2**：为什么 `lockChunk` 是阻塞的，而 `tryLockChannel` 是非阻塞的？反过来设计会怎样？

**参考答案**：chunk 锁必须阻塞，因为 CRAQ 要求同一 chunk 的写严格串行，否则版本号会错乱；而通道锁若也阻塞，会让「重试的重复请求」也排队，白白占用协程。非阻塞 + 立即返回错误，让客户端自行退避重试，更轻量。反过来会让系统要么失去串行化保证，要么浪费资源。

---

### 4.2 双版本：committed/pending 与版本号不变量

第二道关卡过了，进入五步流水线的**第 3 步 `doUpdate`**——这是版本号真正被赋值与校验的地方。

#### 4.2.1 概念说明

每个 chunk 的元数据（`ChunkMetadata`）上同时挂着两个版本号：

- `updateVer`：**pending（待确认）版本**。本副本已经把这段数据写到磁盘了，但还没拿到「整条链都写完」的确认。
- `commitVer`：**committed（已确认）版本**。链尾已经确认、可以安全暴露给读者的版本。

它们必须满足不变量：

\[
\text{commitVer} \le \text{updateVer} \le \text{commitVer} + 1
\]

也就是说，一个 chunk 在任一副本上**最多只有一个尚未确认的写**（pending 比 committed 最多领先 1）。这个不变量由两处共同保证：

- `update()` 阶段：写本地时校验/分配 `updateVer`，若违反「领先不超过 1」就拒绝。
- 链头 chunk 锁：保证上一个写走完「转发 + 提交」、`commitVer` 追上来之后，下一个写才能开始。

`ChunkState` 配合版本号表达 chunk 的「干净程度」：`COMMIT`（已提交，`commitVer==updateVer`）、`CLEAN`（写完待提交）、`DIRTY`（写了一半，异常状态）。定义见 [`enum class ChunkState`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/storage/Common.h#L60-L64)。

#### 4.2.2 核心流程：update() 如何决定 updateVer

写本地 chunk 时，版本号的赋值有**三条分支**（取决于这是客户端首写、链内转发，还是数据同步）：

```text
isSyncing（数据恢复/同步）?
  ├─ 是 → meta.updateVer = 请求带来的 updateVer；meta.commitVer = updateVer - 1   # 直接进入 pending 态
  └─ 否 → 请求带 updateVer > 0（链内转发，版本由 head 分配）?
            ├─ 是 → 校验 updateVer 与本地 meta 的关系：
            │       updateVer <= commitVer        → kChunkCommittedUpdate（已提交过，幂等成功）
            │       updateVer <= updateVer_local  → kChunkStaleUpdate    （已写过这个 pending，幂等成功）
            │       updateVer >  updateVer_local+1→ kChunkMissingUpdate  （中间漏了版本，真错误，阻断）
            │       否则                           → meta.updateVer = updateVer
            └─ 否（客户端首写，updateVer==0）→ meta.updateVer += 1（head 负责分配新版本号）
                                          并校验 updateVer <= commitVer+1，否则 kChunkAdvanceUpdate
```

四种「版本异常」错误码的含义一览（这是面试与排障的高频考点）：

| 错误码 | 触发条件 | 处理方式 |
|---|---|---|
| `kChunkCommittedUpdate` | `updateVer <= commitVer`（这段写早已提交） | **视为成功**，幂等返回 |
| `kChunkStaleUpdate` | `updateVer <= 本地 updateVer`（这段 pending 已写过） | **视为成功**，幂等返回 |
| `kChunkMissingUpdate` | `updateVer > 本地 updateVer + 1`（中间有版本缺口） | **真错误，阻断** |
| `kChunkAdvanceUpdate` | 客户端首写自增后 `updateVer > commitVer+1`（违反不变量） | **真错误，阻断** |

`handleUpdate` 拿到 `doUpdate` 的结果后，正是按这张表把「幂等成功」和「真错误」分流——见 [4.2.3](#423-源码精读)。

#### 4.2.3 源码精读

版本号赋值与校验的真正执行点在 [`ChunkReplica::update`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/ChunkReplica.cc#L132-L317)，关键片段：

```cpp
if (options.isSyncing) {
  meta.updateVer = writeIO.updateVer;
  meta.commitVer = ChunkVer{writeIO.updateVer - 1};   // 同步写入：直接 pending
} else if (writeIO.updateVer > 0) {
  // 链内转发：版本号由 head 分配好带过来，这里只做校验
  if (writeIO.updateVer <= meta.commitVer)        return kChunkCommittedUpdate;
  else if (writeIO.updateVer <= meta.updateVer)   return kChunkStaleUpdate;
  else if (writeIO.updateVer > meta.updateVer + 1)return kChunkMissingUpdate;
  meta.updateVer = writeIO.updateVer;
} else {
  // 客户端首写：head 负责分配新版本号
  meta.updateVer += 1;
  if (meta.updateVer > meta.commitVer + 1) return kChunkAdvanceUpdate;   // 守护不变量
}
meta.chunkState = ChunkState::DIRTY;   // 标记为「写过、待提交」
```

这段同时回答了一个关键问题：**版本号是谁分配的？** 答案是**链头**。客户端首写带 `updateVer==0`，head 的 `update()` 走第三分支自增 `updateVer`；随后 `handleUpdate` 把这个分配好的版本号回填到请求里（`req.payload.updateVer = updateResult.updateVer`），转发给后继时后继就走第二分支（`updateVer > 0`）只做校验。于是**整条链上同一次写用的是同一个 `updateVer`**。

`handleUpdate` 收到 `doUpdate` 结果后的分流在 [`StorageOperator.cc` 的 406-439 行](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L406-L439)，把四种错误码翻译成「成功/阻断」：

- `kChunkCommittedUpdate` / `kChunkStaleUpdate` → 把 `lengthInfo`/`updateVer` 补成成功，**继续走后续转发与提交**（幂等）。
- `kChunkMissingUpdate` / `kChunkAdvanceUpdate` → 直接 `co_return` 错误，**阻断**这次写。

提交阶段（第 5 步 `doCommit`）则把 `commitVer` 推上去，在 [`ChunkReplica::commit`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/ChunkReplica.cc#L397-L467)：

```cpp
if (commitIO.commitVer > meta.updateVer) return kChunkVersionMismatch;   // 不能提交到没写过的版本
...
if (meta.commitVer < commitIO.commitVer) {
  meta.commitVer = commitIO.commitVer;                                    // 推高 committed 版本
} else {
  return kChunkStaleCommit;                                               // 已提交过，幂等
}
if (meta.commitVer == meta.updateVer) {
  meta.chunkState = ChunkState::COMMIT;                                   // pending 全部确认 → 进入 COMMIT 态
}
```

提交完成后 `commitVer == updateVer`，chunk 从 `CLEAN` 翻为 `COMMIT`，对读者完全可见。

> 把 `update()` 与 `commit()` 合起来看，不变量 \( \text{commitVer} \le \text{updateVer} \le \text{commitVer}+1 \) 在三处被守护：写时（`kChunkAdvanceUpdate`）、提交时（`commitVer > updateVer` 拒绝）、以及靠链头 chunk 锁保证两次写不重叠。

#### 4.2.4 代码实践：给一个 chunk 画出版本号时间线

1. **实践目标**：用一个具体例子验证双版本不变量在写-提交周期内的变化。
2. **操作步骤**：
   - 假设某 chunk 初始 `commitVer = updateVer = 4`，`chunkState = COMMIT`。
   - 模拟一次客户端首写：按第三分支，`updateVer` 自增到 5，`chunkState = DIRTY`。此时 `commitVer=4, updateVer=5`，满足 \( \le \) 关系。
   - 模拟提交成功：`commitVer` 推到 5，`chunkState = COMMIT`。
   - 打开 [`ChunkReplica.cc` 的版本分支](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/ChunkReplica.cc#L211-L242) 逐行对照。
3. **需要观察的现象**：在「pending 但未提交」的窗口里，`updateVer == commitVer + 1` 恰好成立；提交后二者相等。
4. **预期结果**：你能写出这个 chunk 从 `(4,4,COMMIT)` → `(5,4,DIRTY)` → `(5,5,COMMIT)` 的完整三元组序列（按 `(updateVer, commitVer, chunkState)` 记）。
5. **运行结果**：待本地验证（源码阅读 + 手动推演型）。

#### 4.2.5 小练习与答案

**练习 1**：`kChunkMissingUpdate` 为什么必须当成「真错误」阻断，而不能像 `kChunkStaleUpdate` 那样幂等成功？

**参考答案**：`kChunkMissingUpdate` 意味着 `updateVer > 本地 updateVer + 1`，即请求声称要写的版本号比本地「领先超过 1」，中间存在版本缺口——说明链上某个更早的写丢失了。这不是重复请求，而是真正的数据不一致，必须阻断并上报，否则会让副本之间版本错位。`kChunkStaleUpdate` 则是 `updateVer` 落在已写范围内，纯粹是重试，幂等成功即可。

**练习 2**：为什么版本号要由链头分配，而不是各副本自己加 1？

**参考答案**：CRAQ 要求同一次写在所有副本上使用同一个版本号，才能在提交时让 `commitVer` 全链一致、让「读任何」看到一致结果。若各副本各自加 1，不同副本上同一次写会得到不同版本号，校验与提交都会错乱。由 head 统一分配、随转发请求带给后继，是最简单可靠的做法。

---

### 4.3 转发与 ACK：沿链向下写、沿链向上确认

五步流水线的**第 4 步**是 CRAQ 与普通主从复制最大的区别：写完本地后，要把请求**转发给后继 target**；而「提交确认」不是凭空产生的，它是链尾提交后、**作为 ACK 一级级回传**上来的。

#### 4.3.1 概念说明

- **向下转发（forward）**：每个副本写完本地后，调用 `ReliableForwarding::forwardWithRetry`，把同一个 `UpdateReq`（此时 `fromClient=false`，且带上 head 分配好的 `updateVer`）发给自己的**后继 target**。后继收到后走的是同一个 `update()` RPC，于是它会再写本地、再转发自己的后继——如此递归直到链尾。
- **链尾提交**：链尾（tail）没有后继，`forward()` 返回特殊错误 `kNoSuccessorTarget`，于是 tail **就地提交**（`doCommit` 把 `commitVer` 推到 `updateVer`）。
- **向上 ACK**：tail 提交后，它的 `update()` RPC 响应（`UpdateRsp`）里带着 `commitVer`；这个响应回到前驱，前驱据此提交自己的 `commitVer`，再把新的 `commitVer` 放进自己的响应……于是「已提交版本」从 tail 一路传回 head。

一句话概括：**写请求沿链向下传播（带 `updateVer`），提交确认沿链向上回传（带 `commitVer`）**。这是 CRAQ 强一致的核心机制。

#### 4.3.2 核心流程

`ReliableForwarding` 是三层调用：`forwardWithRetry`（重试外壳）→ `forward`（处理「有没有后继」）→ `doForward`（真正发 RPC）。

```text
forwardWithRetry(req, target, commitIO):           # 带指数退避的重试循环
  └─ forward(req, target, commitIO):
       ├─ target 没有 successor?  
       │    └─ 是（本节点是 tail）→ commitIO.commitChainVer = 本地 chainVer
       │                              return kNoSuccessorTarget      # 特殊「错误」，handleUpdate 视为成功
       ├─ doForward(req, successor) → 发 update RPC 给后继
       └─ 用后继响应里的 commitVer / commitChainVer 回填 commitIO
            若后继的 commitChainVer > 本地 chainVer → kChainVersionMismatch（触发本地刷新路由重试）
```

`handleUpdate` 第 4、5 步如何消费转发结果（[StorageOperator.cc 443-508](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L443-L508)）：

```cpp
// 第 4 步：转发给后继（commitIO.commitVer 初值 = 本地 updateResult.updateVer）
CommitIO commitIO; commitIO.commitVer = updateResult.updateVer;
auto forwardResult = co_await components_.reliableForwarding
                         .forwardWithRetry(requestCtx, req, remoteBuf, chunkEngineJob, target, commitIO);

if (forwardResult 是普通错误 且 != kNoSuccessorTarget) co_return 错误;   // tail 的 kNoSuccessorTarget 放行
// （可选）校验后继生成的 checksum 与本地一致，否则 kChecksumMismatch

// 第 5 步：用 commitIO.commitVer（已被后继 ACK 回填，或 tail 用本地值）提交本地
auto commitResult = co_await doCommit(requestCtx, commitIO, ...);
```

注意 `commitIO.commitVer` 的两种来源，恰好对应「中间节点」与「链尾」：

- **中间节点**：`forward()` 用后继响应里的 `commitVer` 回填 `commitIO.commitVer`（「后继提交到了几，我也提交到几」）。
- **链尾**：`forward()` 直接返回 `kNoSuccessorTarget`，`commitIO.commitVer` 保持初值（即本地 `updateVer`），于是 `doCommit` 把自己的 `commitVer` 推到 `updateVer`，chunk 翻为 `COMMIT`。

#### 4.3.3 源码精读

- [`forwardWithRetry` 重试外壳](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/ReliableForwarding.cc#L33-L104)：用 `ExponentialBackoffRetry`（默认 100ms→1000ms，总时长 60s）循环重试。两个关键点：① `kNoSuccessorTarget` 被当作**成功**（说明本节点是 tail）；② 转发失败时，它会周期性重查 `TargetMap`，等待后继 target 上线（链正在重排时很常见）。
- [`forward` 的 tail 判定与 ACK 回填](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/ReliableForwarding.cc#L106-L136)：`!target->successor.has_value()` 即为 tail；否则用 `doForward` 的结果里的 `commitVer`/`commitChainVer` 回填 `commitIO`。这里还有一道一致性保护：若后继回了一个**比本地更高**的 `chainVer`，说明本地路由过期，返回 `kChainVersionMismatch` 逼上层刷新路由后重试。
- [`doForward` 真正发 RPC](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/ReliableForwarding.cc#L138-L280)：构造转发的 `UpdateReq`（`fromClient=false`、带上 head 分配的 `updateVer`、把本地已注册的 `RDMARemoteBuf` 一起带过去，让后继用 RDMA Read 零拷贝取数据），最后 `messenger.update(successorAddr, updateReq)` 发出 RPC。后继的处理又回到 [`StorageOperator::update`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L284-L331)，形成递归。

> 一个常被忽略的细节：`doForward` 里有大量针对 **`SYNCING` 状态后继**的特殊处理（[ReliableForwarding.cc 152-223](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/ReliableForwarding.cc#L152-L223)）。当后继 target 还在数据恢复期（`publicState == SYNCING`），普通增量写无法直接套用，于是转发方会**整 chunk 读出来再以全量写（full-chunk-replace）的方式发给后继**。这块属于数据恢复范畴，详见 [u5-l5（数据恢复与同步）](u5-l5-data-recovery.md)。

落盘的线程池在 [`UpdateWorker`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/update/UpdateWorker.h#L11-L46)：默认 32 个写线程，**按磁盘分队列**（`queueVec_[diskIndex]`），保证同一块 SSD 上的写串行、不同 SSD 并行；`enqueue` 把 `UpdateJob` 投到对应磁盘队列，工作线程 `run()` 取出后调用 [`target->updateChunk(*job, bgExecutors_)`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/update/UpdateWorker.cc#L34-L44)，再由 `StorageTarget::updateChunk` 分派到 `ChunkReplica::update`（写）或 `ChunkReplica::commit`（提交）。`UpdateJob` 用 `Baton` 在 `complete()` 处挂起协程、`setResult` 时唤醒，把「异步落盘」对接回协程模型（见 [`UpdateJob::complete/setResult`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/update/UpdateJob.h#L85-L99)）。

#### 4.3.4 代码实践：阅读型——画一张转发与 ACK 的时序图

1. **实践目标**：用一张时序图把「向下转发 + 向上 ACK」画清楚。
2. **操作步骤**：
   - 读 [`ReliableForwarding::forward`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/ReliableForwarding.cc#L106-L136)，标注 tail 分支返回 `kNoSuccessorTarget`、中间节点用后继响应回填 `commitIO.commitVer`。
   - 读 [`handleUpdate` 第 4、5 步](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L443-L508)，确认 `commitIO.commitVer` 的两种来源。
   - 画一条竖向时序：client → A → B → C（向下转发 updateVer），再 C → B → A → client（向上回传 commitVer）。
3. **需要观察的现象**：`updateVer` 只在向下方向流动（head 分配、固定不变）；`commitVer` 只在向上方向流动（tail 先有，逐级回填）。
4. **预期结果**：图中能清楚看到「写向下、确认向上」两条方向相反的数据流。
5. **运行结果**：待本地验证（源码阅读型）。

#### 4.3.5 小练习与答案

**练习 1**：链尾提交后，前驱是怎么「知道」可以提交、并且提交到哪个版本号的？

**参考答案**：链尾提交后，它对 `update()` RPC 的响应（`UpdateRsp.result`）里带着 `commitVer`。前驱的 `forward()` 拿到这个响应，用 `commitIO.commitVer = ioResult.commitVer` 把它回填，然后 `handleUpdate` 第 5 步用这个值调 `doCommit`。所以前驱「提交到几」完全由后继的 ACK 决定。

**练习 2**：`kNoSuccessorTarget` 是一个「错误码」，为什么 `forwardWithRetry` 和 `handleUpdate` 都把它当成功处理？

**参考答案**：它表达的语义是「本节点没有后继」，即本节点就是链尾。链尾本来就不需要转发，它要做的就是就地提交。把它设计成错误码是为了让 `forward()` 能用一个统一的返回值表达「没有转发这件事」，而调用方据此跳过转发相关校验、直接进入提交步骤。这是一种用错误码做控制流的惯用写法。

---

## 5. 综合实践：三节点链 A→B→C 的完整一次写入

把三节的知识串起来，完成本讲的综合任务：**用一个三节点链 A→B→C 模拟一次写入，逐步说明每节点的 pending/committed 版本变化与 ACK 传播过程。**

### 5.1 初始条件

- 链 `A(head) → B → C(tail)`，三个 target 都 `SERVING` 且 `UPTODATE`。
- 某个 chunk `X` 在三处的初始状态都是：`updateVer = 10, commitVer = 10, chunkState = COMMIT`（即三元组 `(u,v,state) = (10,10,COMMIT)`）。
- 客户端要写入 chunk `X` 的一段新数据，请求里 `updateVer = 0`（首写，让 head 分配版本号）。

### 5.2 操作步骤（逐步推演）

**第 1 步：客户端 → A（head）**
- A 的 `write()` RPC → `ReliableUpdate`（通道锁/缓存，未命中）→ `handleUpdate`。
- A 校验 `fromClient && isHead` 通过 → `lockChunk(X)` → `doUpdate`：因 `updateVer==0` 走第三分支，`meta.updateVer += 1` → **A: (11, 10, DIRTY)**。A 把分配好的 `updateVer=11` 回填请求。
- A `forwardWithRetry` → 转发给 B（带 `updateVer=11`）。

**第 2 步：A → B**
- B 的 `update()` RPC（`fromClient=false`）→ `ReliableUpdate` → `handleUpdate`。
- B `lockChunk(X)` → `doUpdate`：`updateVer=11 > 0` 走第二分支，校验 `11 <= commitVer_B(10)`? 否；`11 <= updateVer_B(10)`? 否；`11 > 10+1`? 否 → `meta.updateVer = 11` → **B: (11, 10, DIRTY)**。
- B `forwardWithRetry` → 转发给 C。

**第 3 步：B → C（tail）**
- C 的 `update()` RPC → `handleUpdate` → `doUpdate` → **C: (11, 10, DIRTY)**。
- C `forwardWithRetry` → `forward` 发现 `!successor.has_value()` → 返回 `kNoSuccessorTarget`，`commitIO.commitVer` 保持 11。
- C 进入第 5 步 `doCommit`：`commitVer = 11`，因 `commitVer==updateVer` → `chunkState=COMMIT` → **C: (11, 11, COMMIT)**。
- C 的 `update()` 响应带着 `commitVer=11` 返回给 B（**第一段 ACK**）。

**第 4 步：ACK C → B**
- B 的 `forward()` 收到响应，`commitIO.commitVer = 11`（来自 C）。
- B `doCommit` → **B: (11, 11, COMMIT)**。B 的响应带 `commitVer=11` 返回给 A（**第二段 ACK**）。

**第 5 步：ACK B → A → 客户端**
- A 的 `forward()` 收到响应，`commitIO.commitVer = 11`（来自 B）。
- A `doCommit` → **A: (11, 11, COMMIT)**。A 返回成功给客户端。

### 5.3 需要观察的现象

1. **向下**：`updateVer=11` 这个数字从 A 一路带到 C，全程不变（head 分配一次，全员复用）。
2. **向上**：`commitVer=11` 这个「确认」从 C 最先产生，逐级回传到 A。
3. **状态翻转顺序**：三处的 `chunkState` 翻成 `COMMIT` 的顺序是 **C → B → A**（与链尾→链尾相反，即从 tail 向 head）。
4. **不变量全程成立**：每处都满足 `commitVer ≤ updateVer ≤ commitVer+1`；pending 窗口里 `(11,10,DIRTY)`，提交后 `(11,11,COMMIT)`。
5. **读一致性**：在第 3 步 C 已 `COMMIT` 但 A/B 还是 `DIRTY` 的瞬间，若读者打到 A 或 B，会因 `commitVer(10) != updateVer(11)` 命中读路径的 `kChunkNotCommit`（见 [u5-l2 的 aioPrepareRead 校验](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/ChunkReplica.cc#L54-L66)），客户端换副本重试；这段窗口极短，因为 ACK 几跳内就回来了。

### 5.4 预期结果

把上述五步整理成一张表（每行写「A / B / C」三处的 `(updateVer, commitVer, chunkState)`）：

| 时刻 | A (head) | B | C (tail) | 说明 |
|---|---|---|---|---|
| 初始 | (10,10,COMMIT) | (10,10,COMMIT) | (10,10,COMMIT) | 三处一致 |
| A 写本地 | (11,10,DIRTY) | (10,10,COMMIT) | (10,10,COMMIT) | head 分配 updateVer=11 |
| B 写本地 | (11,10,DIRTY) | (11,10,DIRTY) | (10,10,COMMIT) | 转发带 updateVer=11 |
| C 写本地 | (11,10,DIRTY) | (11,10,DIRTY) | (11,10,DIRTY) | 写到达 tail |
| C 提交 | (11,10,DIRTY) | (11,10,DIRTY) | (11,11,COMMIT) | tail 就地提交，ACK 起点为 11 |
| B 提交 | (11,10,DIRTY) | (11,11,COMMIT) | (11,11,COMMIT) | ACK 回填 commitVer=11 |
| A 提交 | (11,11,COMMIT) | (11,11,COMMIT) | (11,11,COMMIT) | 写完成，全链一致 |

### 5.5 运行结果

待本地验证。若你有测试集群，可借助本讲的 [`StorageEventTrace`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/storage/Common.h#L726-L735)（`handleUpdate` 在每个节点都会记录 `updateReq/updateRes/forwardRes/commitIO/commitRes` 五段）配合 [u8-l3 的结构化 trace 日志](u8-l3-monitor-and-analytics.md) 把这张表实际打印出来核对；否则按上表手工推演即可。

## 6. 本讲小结

- 写路径的总协调者是 [`StorageOperator::handleUpdate`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L333-L519)，它把 CRAQ 写入固化成**五步流水线**：校验链头 → 加 chunk 锁 → 写本地（`doUpdate`）→ 转发后继（`forwardWithRetry`）→ 提交（`doCommit`）。
- **链头加锁**有两层：`handleUpdate` 的 chunk 锁（阻塞、按 chunk，保证 CRAQ 串行化）与 `ReliableUpdate` 的通道锁 + 结果缓存（非阻塞、按 client+channel，保证网络重试幂等）；客户端写只能落在 `isHead` 的 target。
- **双版本** `updateVer`（pending）/`commitVer`（committed）满足 \( \text{commitVer} \le \text{updateVer} \le \text{commitVer}+1 \)；版本号由链头在首写时分配，随转发请求带给全链；四种版本异常码里 `Committed/Stale` 幂等成功、`Missing/Advance` 阻断。
- **转发与 ACK**：写请求沿链向下（带 `updateVer`），链尾因无后继返回 `kNoSuccessorTarget` 并就地提交；提交确认作为 `commitVer` 在 RPC 响应里沿链向上回传，每个前驱据此 `doCommit`，于是 `chunkState` 从 tail 向 head 依次翻为 `COMMIT`。
- 落盘由 [`UpdateWorker`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/update/UpdateWorker.h#L11-L46) 按**磁盘分队列**串行化，经 `UpdateJob` 的 `Baton` 把异步 AIO 对接回协程。
- 「读任何」靠双版本守护：脏 chunk（`commitVer != updateVer`）会被读路径拒绝，由客户端换副本重试；脏窗口因 ACK 快速回传而极短。

## 7. 下一步学习建议

- **继续 storage 单元**：读 [u5-l4（TargetMap 与链路由）](u5-l4-targetmap-routing.md)，看 `getByChainId` 如何解析 `successor`/`isHead`/`isTail`、用 `chainVer` 拒绝过期写；再读 [u5-l5（数据恢复与同步：ResyncWorker）](u5-l5-data-recovery.md)，理解本讲多次提到的 `SYNCING` 全量回放（`doForward` 里的 `readForSyncing`）到底怎么把一个离线 target 救回来。
- **横向对照**：回到 [u4-l4（文件数据布局与链分配）](u4-l4-layout-and-chain-alloc.md) 与 [u3-l4（ChainTable/Chain/Target）](u3-l4-chain-target-model.md)，把「客户端 open 拿到 layout → 自算 chunk 所属链 → 写打到链头 → 沿链 CRAQ 复制」的端到端链路在脑子里走通。
- **深入 chunk engine**：本讲的 `doUpdate`/`doCommit` 最终落到 `ChunkReplica`（旧 C++ 实现）；若想看新版 Rust chunk engine 如何原子地完成「写新 chunk 元数据 + 更新物理块状态」，进入 [u6-l1（Chunk Engine 总览与 C++/Rust FFI）](u6-l1-chunk-engine-overview.md) 与 [u6-l3（Chunk 元数据与 RocksDB）](u6-l3-chunk-meta-rocksdb.md)。
