# 主选举与故障切换

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚多个 mgmtd 实例**如何用「FDB 单 key + 时间租约」选出唯一的 primary**，而不需要 Paxos/Raft 那样独立的共识协议。
- 读懂 `MgmtdLeaseExtender` 这一后台协程的完整编排：它在每个实例上周期运行、用一次读写事务调用 `MgmtdStore::extendLease` 仲裁出当前持约者，并在「自己刚成为/重新成为 primary」时触发 `onNewLease` 全量重建内存路由。
- 复述一次 **primary 进程崩溃后的故障切换全过程**：旧租约如何自然过期、谁在竞争中胜出、新 primary 如何从 FoundationDB 把全部路由信息加载回内存，以及为什么这个设计**天然不会脑裂（split-brain）**。
- 理解 `suspicious_lease_interval`、`bootstrapping`、`ensureLeaseValid` 这几个看似零散的概念如何共同构成「同一时刻只有一个可信 primary」的正确性保证。

本讲只讲 **mgmtd 自己的主选举与切换**，不讲 meta/storage 节点的心跳租约（那是上一讲 u3-l2），也不讲切换后链表/ target 状态如何推进数据恢复（那是 u3-l5、u5-l5）。

## 2. 前置知识

进入源码前，先建立四个直觉。

### 2.1 mgmtd 是无状态的，路由信息的「唯一事实来源」是 FoundationDB

回顾 u3-l1 与 u2-l6：mgmtd 内存里那份 `RoutingInfo`（`nodeMap`/`chainMap`/`chainTableMap`/`targetMap`）只是 FDB 里数据的**内存投影**。真正持久的、跨重启不丢的，是 FDB 中按 key 前缀分门别类存放的若干条记录（节点、链表、链、配置、target……）。这意味着：

- 任何一个 mgmtd 实例崩溃，**路由信息本身不会丢**——它躺在 FDB 里。
- 一个新当选的 primary 要做的第一件事，就是把这些记录**重新读回内存**，重建出 `RoutingInfo`。本讲把这个过程叫做「**路由加载**」。

这是理解故障切换的基石：**切换 = 重新选一个持约者 + 它把 FDB 的数据重新加载进内存**，不需要做任何数据搬迁。

### 2.2 用「租约」表达「谁是 primary」

mgmtd 用一个极其朴素的想法表达「现在谁是 primary」：在 FDB 里存**一条**记录，里面写着

```
当前 primary 是谁(primary.nodeId) + 这张租约何时开始(leaseStart) + 何时到期(leaseEnd)
```

这条记录就是 `flat::MgmtdLeaseInfo`。**谁的名字写在这条记录的 `primary` 字段里，谁就是 primary。** 「到期」则用墙钟时间表达：一旦 `leaseEnd < now`，这张租约作废，别人可以申请新租约。

primary 必须在租约到期前**不断续约**（把 `leaseEnd` 往后推），一旦它停止续约（崩溃、卡死、被禁用），租约就会自然过期，从而把 primary 身份「让」出来。这就是「**心跳/续约即租约**」思想在 mgmtd 自身上的应用——和 u3-l2 讲的节点心跳续约是同一个模型，只不过这里的「租约」对象是 mgmtd 自己的 primary 身份。

### 2.3 为什么不需要独立的共识算法？

关键洞察：**仲裁发生在一次 FoundationDB 读写事务内部**。

- 「申请/续约租约」是一次对**同一个 key**（`getMgmtdLeaseKey()`）的读—改—写。
- FDB 提供严格的可串行化（SSI），对同一个 key 的并发写事务会被**冲突检测**强制排队，最终只有一个会 commit 成功。
- 因此即使多个 mgmtd 实例**同时**尝试接管，FDB 也只允许其中一个真正改写租约记录。

换句话说，3FS 把「选举」这件原本需要 Paxos/Raft 的复杂事情，**外包给了 FDB 的事务原子性**。mgmtd 实例之间不需要互相通信、不需要多数派投票，只要各自去 FDB 抢着写那一个 key 即可。代价是 mgmtd 的可用性依赖于 FDB 集群的可用性——而 FDB 本身就是为高可用设计的（u2-l6）。

### 2.4 两层锁与 doAsPrimary 守卫

和 u3-l1/u3-l2 一致，本讲会反复遇到：

- **外层 `writerMu_`**（`state.coScopedLock<...>()`）：串行化一次「读—改—写」的整个过程。
- **内层 `CoroSynchronized<MgmtdData> data_`**：保护内存 `MgmtdData` 的读写一致性。
- **`doAsPrimary(state, handler)`**：所有「只有 primary 才能处理」的写操作 RPC 都被它包裹；它先调 `ensureSelfIsPrimary`，若自己不是可信 primary 就直接返回 `kNotPrimary` 拒绝。

本讲要回答的核心问题之一就是：**`ensureSelfIsPrimary` 依据什么判定「自己是可信 primary」？** 答案会和 §4.1 的租约状态机紧密相关。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/mgmtd/store/MgmtdStore.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/store/MgmtdStore.cc) | **选举核心**：`extendLease` 在一次事务里仲裁出当前持约者；`ensureLeaseValid` 供写操作校验租约；`loadMgmtdLeaseInfo`/`storeMgmtdLeaseInfo` 读写那一条租约记录。 |
| [src/mgmtd/background/MgmtdLeaseExtender.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdLeaseExtender.cc) | **续约编排**：每个实例周期运行的 `extend()`；判定「自己是否刚成为/重新成为 primary」并触发 `onNewLease` 全量加载。 |
| [src/mgmtd/background/MgmtdLeaseExtender.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdLeaseExtender.h) | 仅有 `extend()` 一个协程接口，逻辑全在 `.cc`。 |
| [src/mgmtd/background/MgmtdBackgroundRunner.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdBackgroundRunner.cc) | 把 `extendLease` 注册为周期后台任务（**所有实例都注册**，无论是否 primary）。 |
| [src/mgmtd/service/MgmtdState.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdState.cc) | `currentLease`（判定「可信 primary」的真正实现）、`utcNow`、`selfId`。 |
| [src/mgmtd/service/helpers.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/helpers.h) / [helpers.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/helpers.cc) | `withReadWriteTxn`（带租约校验的写事务封装）、`doAsPrimary`、`ensureSelfIsPrimary`。 |
| [src/fbs/mgmtd/MgmtdLeaseInfo.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/MgmtdLeaseInfo.h) | 租约记录的扁平结构定义：`primary` / `leaseStart` / `leaseEnd` / `releaseVersion`。 |
| [src/mgmtd/service/LeaseInfo.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/LeaseInfo.h) | 内存中的租约状态：`lease`（可选）+ `bootstrapping` 标志位。 |
| [src/mgmtd/service/MgmtdConfig.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdConfig.h) | 关键参数：`lease_length`、`extend_lease_interval`、`suspicious_lease_interval`、`bootstrapping_length`、`validate_lease_on_write`、`extend_lease_check_release_version`。 |
| [src/mgmtd/service/MgmtdData.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdData.cc) | `reset`（加载后重建内存数据并清 `bootstrapping`）、`bootstrapping(config)`（基于时间的「新主」窗口，对外暴露给客户端）。 |
| [src/mgmtd/background/MgmtdHeartbeater.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdHeartbeater.cc) | **非主 mgmtd** 作为客户端向 primary 发心跳（mgmtd 自己也是集群成员，要被 primary 续命）。 |

---

## 4. 核心概念与源码讲解

### 4.1 租约选举：用 FDB 单 key + 时间租约选出 primary

#### 4.1.1 概念说明

先看选举要解决的问题：集群里部署了多个 mgmtd 实例（典型 3 个），但同一时刻只能有**一个** primary 对外提供写服务，否则会出现两个 primary 各自改写路由信息、互相覆盖的灾难。我们需要一个机制来：

1. **选定**唯一的 primary；
2. 在 primary 正常时**持续确认**它的身份；
3. 在 primary 失效后**自动**让出身份、选出新 primary；
4. 全程**不脑裂**——绝不能有两个实例同时认为自己是可信 primary。

3FS 的解法是「**竞争式租约续约（competitive lease renewal）**」：

- FDB 中有一条专门的租约记录，key 形如 `Single|MgmtdLease`（`Single` 是 key 前缀，表示全局唯一的一条记录）。
- **每一个** mgmtd 实例都周期性地（默认每 10s）跑一次 `extend()`，在一次读写事务里去「申请/续约」这条租约。
- `extendLease` 这一个函数内部用租约的**到期时间**做仲裁，决定到底是「续约当前持约者」还是「因为旧租约已过期，改写给当前调用者」。
- 由于所有实例争抢的是**同一个 key**，FDB 的事务冲突检测会把并发竞争序列化，最终只有一个实例能成功写入。

这个设计里没有「投票」「多数派」「leader election round」这些概念——**FDB 的事务原子性就是共识层**。

#### 4.1.2 核心流程

先看承载租约信息的数据结构。`flat::MgmtdLeaseInfo` 只有四个字段：

```cpp
// src/fbs/mgmtd/MgmtdLeaseInfo.h
struct MgmtdLeaseInfo : public serde::SerdeHelper<MgmtdLeaseInfo> {
  SERDE_STRUCT_FIELD(primary, PersistentNodeInfo{});   // 当前 primary 的节点信息(含 nodeId、地址)
  SERDE_STRUCT_FIELD(leaseStart, UtcTime{});            // 这张租约开始时间
  SERDE_STRUCT_FIELD(leaseEnd, UtcTime{});              // 这张租约到期时间
  SERDE_STRUCT_FIELD(releaseVersion, ReleaseVersion()); // 发放租约的二进制版本号
};
```

> 见 [src/fbs/mgmtd/MgmtdLeaseInfo.h:7-24](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/MgmtdLeaseInfo.h#L7-L24)：`MgmtdLeaseInfo` 的四个字段。`primary` 是 `PersistentNodeInfo`，里面带着 nodeId 与各 service group 地址——这正是非主 mgmtd 用来「找到 primary 并向它发心跳」的依据（§4.3）。

而内存侧用一个 `LeaseInfo` 把这条租约和一个 `bootstrapping` 标志位包在一起：

```cpp
// src/mgmtd/service/LeaseInfo.h
struct LeaseInfo {
  std::optional<flat::MgmtdLeaseInfo> lease;   // 最近一次从 FDB 读回的租约；nullopt 表示尚无/已释放
  bool bootstrapping = false;                  // 自己是否「刚成为 primary、路由还没加载完」
};
```

> 见 [src/mgmtd/service/LeaseInfo.h:6-9](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/LeaseInfo.h#L6-L9)。`bootstrapping` 这个标志位是本讲的「主角」之一，它决定了实例对外是否表现得像个可信 primary。

整个选举—续约的循环可以用下面的时序概括（`P1` 为现 primary，`P2/P3` 为备机）：

```
        每 10s                          每 10s                         每 10s
   P1: extend() ──► extendLease ──► primary==P1,续约成功(leaseStart不变,leaseEnd 后推)
   P2: extend() ──► extendLease ──► P1 租约未过期 & primary!=P2 ──► 返回 P1 的租约(P2 仍非主)
   P3: extend() ──► extendLease ──► 同上，返回 P1 的租约

   ───── P1 崩溃，停止 extend() ─────
   ... P1 的 leaseEnd 在 50~60s 后到来 ...
   ... 在那之前 P2/P3 每次都拿到「未过期的 P1 租约」，继续当备机 ...

   某一刻 now > P1.leaseEnd:
   P2: extend() ──► extendLease ──► leaseEnd < now ──► 「租约已过期，开新租约」
                    └─ FDB 事务 commit：写入 primary=P2, leaseStart=now, leaseEnd=now+60s
                    └─ P2 成为新 primary，触发 onNewLease 加载路由(§4.2)
   P3: extend() ──► extendLease ──► P2 的新租约未过期 & primary!=P3 ──► 返回 P2 的租约
                    └─ P3 仍是备机(且下次心跳目标切到 P2)
```

#### 4.1.3 源码精读

**(a) 那一条租约记录的 key 与读写**

租约记录存放在一个全局唯一的 key 下，前缀是 `KeyPrefix::Single`：

```cpp
// src/mgmtd/store/MgmtdStore.cc
std::string_view getMgmtdLeaseKey() {
  static const std::string key = fmt::format("{}MgmtdLease", kv::toStr(kv::KeyPrefix::Single));
  return key;
}
```

> 见 [src/mgmtd/store/MgmtdStore.cc:16-19](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/store/MgmtdStore.cc#L16-L19)。注意是「单条」记录——这正是「大家争抢同一个 key」的前提。`loadMgmtdLeaseInfo`/`storeMgmtdLeaseInfo` 就是 `load`/`store` 这一个 key（见 [MgmtdStore.cc:226-235](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/store/MgmtdStore.cc#L226-L235)）。

**(b) 选举核心：`extendLease`**

这是整篇讲义最关键的函数。它在一次读写事务内被调用，输入是「我（调用者）是谁」「我想要的租约时长」「现在几点」，输出是「仲裁后的当前租约」。逻辑是一棵 if/else 决策树：

```cpp
// src/mgmtd/store/MgmtdStore.cc  (精简，保留分支语义)
CoTryTask<flat::MgmtdLeaseInfo> MgmtdStore::extendLease(
    kv::IReadWriteTransaction &txn, const flat::PersistentNodeInfo &nodeInfo,
    std::chrono::microseconds leaseLength, UtcTime now,
    flat::ReleaseVersion rv, bool checkReleaseVersion) {
  auto fetchResult = co_await loadMgmtdLeaseInfo(txn);      // 读当前租约
  std::optional<flat::MgmtdLeaseInfo> storedLeaseInfo = std::move(*fetchResult);
  flat::MgmtdLeaseInfo newLeaseInfo(nodeInfo, now, now + leaseLength, rv);  // 以「我」为 primary 的候选租约

  if (storedLeaseInfo.has_value()) {
    // 分支①：禁止版本号回滚——旧二进制不能抢走 primary
    if (checkReleaseVersion && newLeaseInfo.releaseVersion < storedLeaseInfo->releaseVersion)
      co_return *storedLeaseInfo;
    auto leaseEnd = storedLeaseInfo->leaseEnd;
    // 分支②：旧租约还有效且剩余时间 ≥ 一整张租约长度 → 啥也不做，返回旧租约
    if (leaseEnd >= now + leaseLength) co_return *storedLeaseInfo;
    // 分支③：旧租约已过期 → 开新租约(写入自己)   ★接管发生在这里★
    if (leaseEnd < now) co_return co_await storeMgmtdLeaseInfo(txn, newLeaseInfo);
    // 分支④：旧租约快到期、且持约者就是自己 → 续约(沿用 leaseStart)
    if (storedLeaseInfo->primary.nodeId == nodeInfo.nodeId) {
      newLeaseInfo.leaseStart = storedLeaseInfo->leaseStart;
      co_return co_await storeMgmtdLeaseInfo(txn, newLeaseInfo);
    }
    // 分支⑤：旧租约快到期、但持约者是别人 → 不抢，返回旧租约
    co_return *storedLeaseInfo;
  } else {
    // 分支⑥：史上第一条租约 → 直接写入自己
    co_return co_await storeMgmtdLeaseInfo(txn, newLeaseInfo);
  }
}
```

> 见 [src/mgmtd/store/MgmtdStore.cc:154-189](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/store/MgmtdStore.cc#L154-L189)。逐分支对照上文时序图：分支②是「健康 primary 持续独占」、分支④是「健康 primary 续约」、分支③是「故障切换接管」、分支⑤是「备机不抢」。

几个关键点务必理解到位：

- **接管只在分支③发生**，且条件是 `leaseEnd < now`——必须等旧租约**完全过期**。这保证了「新 primary 上位」与「旧 primary 仍以为自己是 primary」在时间上不可能重叠（更严格的论证见 §4.3）。
- **续约（分支④）刻意沿用旧的 `leaseStart`**。这非常重要：`leaseStart` 是「这张租约从何时起归我」的身份凭证，只要续约就保持不变；只有**换主**时 `leaseStart` 才会变成新的 `now`。后面 `MgmtdLeaseExtender` 正是靠「`leaseStart` 是否变化」来判断「是不是换主了」。
- **分支①的版本号保护**：如果当前调用者编译出来的二进制 `releaseVersion` 比 FDB 里现存租约的版本号**更低**（旧版），直接返回旧租约、不抢。这防止一次意外的版本回退部署把 primary 抢走。由配置 `extend_lease_check_release_version`（默认 `true`）开关。
- **为什么这是「竞争式」而非「协商式」**：函数内部没有任何「询问其他实例是否同意」的逻辑。它只看 FDB 里那一条记录的当前值。并发正确性完全依赖 FDB 对 `getMgmtdLeaseKey()` 这个 key 的写冲突检测——两个同时想开新租约（分支③）的事务，只有一个能 commit。

**(c) 写操作的租约守卫：`ensureLeaseValid`**

光有选举还不够，还得保证**只有 primary 能处理写**。每个写 RPC 走的 `withReadWriteTxn` 封装，在事务开头会先校验租约：

```cpp
// src/mgmtd/service/helpers.h
template <typename Handler, ...>
inline Result withReadWriteTxn(MgmtdState &state, Handler &&handler, bool expectSelfPrimary = true) {
  ...
  [&](kv::IReadWriteTransaction &txn) -> Result {
    if (expectSelfPrimary && state.config_.validate_lease_on_write()) {
      CO_RETURN_ON_ERROR(co_await state.store_.ensureLeaseValid(txn, state.selfId(), state.utcNow()));
    }
    co_return co_await handler(txn);
  }
  ...
}
```

> 见 [src/mgmtd/service/helpers.h:43-62](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/helpers.h#L43-L62)，重点在 [helpers.h:52-54](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/helpers.h#L52-L54)。`ensureLeaseValid` 在事务里读租约，若 `primary.nodeId != 自己` 或 `leaseEnd < now`，立即返回 `kNotPrimary`。

`ensureLeaseValid` 本身很短，就是把「持约者是不是我、租约有没有过期」翻译成错误码：

```cpp
// src/mgmtd/store/MgmtdStore.cc
CoTryTask<void> MgmtdStore::ensureLeaseValid(kv::IReadOnlyTransaction &txn, flat::NodeId nodeId, UtcTime now) {
  auto fetchResult = co_await loadMgmtdLeaseInfo(txn);
  std::optional<flat::MgmtdLeaseInfo> storedLeaseInfo = std::move(*fetchResult);
  if (storedLeaseInfo.has_value()) {
    if (storedLeaseInfo->primary.nodeId != nodeId)
      co_return makeError(MgmtdCode::kNotPrimary, fmt::format("{}", storedLeaseInfo->primary.nodeId));
    if (storedLeaseInfo->leaseEnd < now) co_return makeError(MgmtdCode::kNotPrimary);
    co_return Void{};
  } else {
    co_return makeError(MgmtdCode::kNotPrimary);
  }
}
```

> 见 [src/mgmtd/store/MgmtdStore.cc:191-204](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/store/MgmtdStore.cc#L191-L204)。注意它是在**写事务里实时读** FDB 的租约——而不是读内存缓存。所以即便内存里 `lease` 还没更新，只要 FDB 里租约已不属于自己，写操作也会被当场拒绝。这是「写路径不会脑裂」的最后一道闸门。

**(d) `doAsPrimary` 与 `currentLease`：什么算「可信 primary」**

对外暴露的写 RPC 通常用 `doAsPrimary` 包裹，它调用 `ensureSelfIsPrimary`：

```cpp
// src/mgmtd/service/helpers.cc
CoTryTask<Void> ensureSelfIsPrimary(MgmtdState &state) {
  auto lease = co_await state.currentLease(state.utcNow());
  if (lease.has_value()) {
    if (lease->primary.nodeId == state.selfId()) co_return Void{};
    else co_return makeError(MgmtdCode::kNotPrimary, ...);
  }
  co_return makeError(MgmtdCode::kNotPrimary);
}
```

> 见 [src/mgmtd/service/helpers.cc:84-94](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/helpers.cc#L84-L94)，模板 `doAsPrimary` 在 [helpers.h:24-29](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/helpers.h#L24-L29)。

这里出现了一个本讲的关键判定函数 `currentLease`。它**不是**简单地返回内存里的 `lease`，而是带了两道「不信任」条件：

```cpp
// src/mgmtd/service/MgmtdState.cc
CoTask<std::optional<flat::MgmtdLeaseInfo>> MgmtdState::currentLease(UtcTime now) {
  auto dataPtr = co_await data_.coSharedLock();
  const auto &lease = dataPtr->lease;
  bool canTrustLease =
      lease.lease.has_value() && now + config_.suspicious_lease_interval().asUs() < lease.lease->leaseEnd;
  if (canTrustLease && !lease.bootstrapping) co_return lease.lease;
  co_return std::nullopt;
}
```

> 见 [src/mgmtd/service/MgmtdState.cc:66-75](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdState.cc#L66-L75)。`utcNow` 与 `selfId` 的实现在 [MgmtdState.cc:45](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdState.cc#L45) 与 [MgmtdState.cc:77-80](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdState.cc#L77-L80)。

`currentLease` 只在**同时满足**以下两个条件时才承认「我有个可信租约」：

1. `now + suspicious_lease_interval < leaseEnd`：租约剩余时间**超过** `suspicious_lease_interval`（默认 20s）。也就是说，一旦租约将在 20s 内到期，就**主动不再信任自己**——即便名义上租约还没过期。
2. `!bootstrapping`：自己不是「刚成为 primary、路由还没加载完」的状态（§4.2）。

条件 1 是**脑裂避免的灵魂**，留到 §4.3 详细论证。这里先记住结论：`currentLease` 返回 `nullopt` ⇒ `ensureSelfIsPrimary` 返回 `kNotPrimary` ⇒ 所有写 RPC 被拒。

#### 4.1.4 代码实践：追踪 `extendLease` 的六个分支

**实践目标**：不看答案，把 `extendLease` 的六条分支与「集群处于什么状态」一一对应，训练自己读决策树的能力。

**操作步骤**：

1. 打开 [src/mgmtd/store/MgmtdStore.cc:154-189](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/store/MgmtdStore.cc#L154-L189)。
2. 准备一张表，三列分别是：`storedLeaseInfo` 的情况（有/无、是否过期、primary 是谁）、命中的分支编号、调用者最终是否成为/保持 primary。
3. 依次代入这五个场景，填表：
   - **S1**：集群刚 `init-cluster` 完，FDB 里还没有租约记录；P1 第一次 `extend()`。
   - **S2**：P1 是健康 primary，10s 前刚续过约（`leaseEnd = now + 60s`）；P1 再次 `extend()`。
   - **S3**：P1 是健康 primary；备机 P2 `extend()`。
   - **S4**：P1 已崩溃，且距它最后一次续约已过 65s（`leaseEnd < now`）；备机 P2 `extend()`。
   - **S5**：P1 是 primary，但运营误部署了一个**更旧**版本的 P1'（`releaseVersion` 更小）来抢，此时 `extend_lease_check_release_version = true`。

**需要观察的现象 / 预期结果**（请自己先填，再对照）：

| 场景 | stored 情况 | 分支 | 调用者是否持约 |
| --- | --- | --- | --- |
| S1 | 无 | ⑥ 写入 P1 | 是（首次成为 primary） |
| S2 | 有，`leaseEnd ≥ now+leaseLength`，primary==P1 | ② 返回旧租约 | 是（保持，未触发实际写入） |
| S3 | 有，未过期，primary==P1≠P2 | ② 或 ⑤（取决于剩余时长） | 否（拿到 P1 的租约） |
| S4 | 有，`leaseEnd < now` | ③ 开新租约写入 P2 | 是（接管！） |
| S5 | 有，`newLeaseInfo.releaseVersion < stored` | ① 返回旧租约 | 否（版本保护阻止回退） |

> 说明：S3 究竟命中②还是⑤，取决于调用时 `leaseEnd` 与 `now + leaseLength` 的关系。由于 `extend_lease_interval(10s)` 远小于 `lease_length(60s)`，健康 primary 自己续约时通常落在②（剩余还很足）；备机调用时如果离过期还远也落②，临近过期但 primary 不是自己则落⑤。核心是：**只要不是自己持约且未过期，就一定不会写入**。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `lease_length` 从 60s 调小到 5s（`extend_lease_interval` 仍是 10s），会发生什么？

**参考答案**：会出大问题。`extend_lease_interval`(10s) > `lease_length`(5s)，意味着两次续约之间租约就已经过期。健康 primary 自己续约时，分支②/④的前提（`leaseEnd ≥ now+leaseLength` 或 `primary==self`）虽仍可能在同一次事务里把租约续上，但只要续约稍有延迟，备机就会反复命中分支③开新租约，导致 primary 频繁易主、`onNewLease` 反复全量加载。工程上必须保证 `lease_length ≫ extend_lease_interval`，默认 60s vs 10s 留了 6 倍余量。

**练习 2**：分支④续约时为什么要把 `newLeaseInfo.leaseStart` 设成旧的 `storedLeaseInfo->leaseStart`，而不是用 `now`？

**参考答案**：`leaseStart` 是「这张租约自何时起归当前 primary」的**身份锚点**。续约只是把到期时间往后推，primary 没换，锚点就不该变。`MgmtdLeaseExtender` 正是靠比对 `leaseStart` 是否变化来判断「是否发生了换主」（见 §4.2）。若续约也刷新 `leaseStart`，每个 10s 都会被误判为「换主」，触发不必要的全量加载。

**练习 3**：`ensureLeaseValid` 在事务里实时读 FDB 租约，而不是读内存 `lease` 字段，为什么？

**参考答案**：内存 `lease` 的更新滞后于 FDB（它由后台 `extend()` 周期刷新）。如果写操作只看内存，可能在一个 primary 实际已丢约、但内存还没刷新的窗口里放行写操作，造成两个 primary 同时写。实时读 FDB 让校验与写入落在同一个可串行化事务里，确保「校验通过」与「实际写入」之间租约状态不可能被别人改掉。

---

### 4.2 路由加载：新 primary 的 `onNewLease` 全量重建

#### 4.2.1 概念说明

`extendLease` 只解决了「谁的名字写进租约」的问题，但一个新 primary 真正能对外服务，还差一步：它的**内存里还没有 `RoutingInfo`**。

为什么？因为 mgmtd 是无状态的（u3-l1、§2.1）。一个备机实例平时只持有「我读到的那张租约」，并不会持续维护完整的节点/链/链表/target 视图——这些只有 primary 才需要用来响应 `GetRoutingInfo`、`Heartbeat`、`updateChain` 等 RPC。所以新 primary 上任后，必须**立刻从 FDB 把全部路由信息读回内存**，这整个过程由 `onNewLease` 完成。

这引出本模块要搞清楚的两件事：

1. **何时触发** `onNewLease`？——`MgmtdLeaseExtender` 如何判断「自己刚刚成为 primary（或重新成为 primary），需要加载」。
2. **加载什么、加载后做什么**？——读哪些 FDB key、如何重建 `MgmtdData`、`routingInfoVersion` 如何推进、`bootstrapping` 标志如何翻转。

#### 4.2.2 核心流程

`MgmtdLeaseExtender::extend()` 的每一次执行（无论 primary 还是备机）都走相同的骨架：

```
extend()
 └─ Op::handle(state)
     ├─ withReadWriteTxn(handler, expectSelfPrimary=false)   ← 注意：false！
     │     ├─ loadNodeInfo(self)                              ← 若自己被 disable 标记 → 返回 nullopt(主动让位)
     │     └─ store.extendLease(...)  → newLease             ← §4.1 的仲裁
     ├─ coScopedLock<"ExtendLease">()                          ← 外层写锁
     ├─ 若 newLease 为 nullopt → lease.lease.reset()（让位，见 §4.3）
     ├─ 计算 leaseChangedReason（比对旧 lease 与 newLease）
     ├─ lease.lease = newLease
     ├─ bootstrapping = (newLease.primary==self && reason 非空)
     └─ 若 bootstrapping → onNewLease()                        ← 全量加载在这里
```

判断「是否需要加载」的核心，是这段比较新旧租约的 lambda：

```cpp
// src/mgmtd/background/MgmtdLeaseExtender.cc
String leaseChangedReason = [&] {
  if (!leaseInfo.lease.has_value()) return "prev no lease";
  if (leaseInfo.lease->primary.nodeId != newLease.primary.nodeId) return "primary id changed";
  if (leaseInfo.lease->leaseStart != newLease.leaseStart) return "lease start changed";
  if (leaseInfo.bootstrapping) return "still bootstrapping";
  return "";
}();
```

> 见 [src/mgmtd/background/MgmtdLeaseExtender.cc:230-236](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdLeaseExtender.cc#L230-L236)。

只要「primary 是自己」且 `reason` 非空（即：之前没租约、或换了 primary、或 `leaseStart` 变了、或上一轮还在 bootstrapping），就把自己标记为 `bootstrapping = true` 并触发 `onNewLease`：

```cpp
leaseInfo.lease = newLease;
leaseInfo.bootstrapping = newLease.primary.nodeId == state.selfId() && !leaseChangedReason.empty();
...
if (leaseInfo.bootstrapping) {
  LOG_OP_INFO(*this, "self got lease, loading ...");
  co_return co_await onNewLease(state, *this, *dataPtr, start);
}
```

> 见 [src/mgmtd/background/MgmtdLeaseExtender.cc:238-255](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdLeaseExtender.cc#L238-L255)。

注意一个精妙的自洽设计：`onNewLease` 内部最终会调用 `MgmtdData::reset`，而 `reset` 会把 `lease.bootstrapping` 置回 `false`（见本模块末尾）。所以稳态下，一次接管只触发**一次**全量加载：接管那轮 `reason="primary id changed"`、`bootstrapping=true`、加载完成后 `reset` 把它清成 `false`；下一轮续约 `leaseStart` 不变、`bootstrapping` 已是 `false`、`reason` 为空，于是不再加载。

#### 4.2.3 源码精读

**(a) `extend()` 的入口与后台注册**

`MgmtdLeaseExtender::extend()` 本身极薄，真正逻辑在内部的 `Op::handle`：

```cpp
// src/mgmtd/background/MgmtdLeaseExtender.cc
CoTask<void> MgmtdLeaseExtender::extend() {
  Op op;
  co_await [&]() -> CoTryTask<void> { CO_INVOKE_OP_INFO(op, "background", state_); }();
}
```

> 见 [src/mgmtd/background/MgmtdLeaseExtender.cc:264-268](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdLeaseExtender.cc#L264-L268)。头文件只有一个声明，见 [MgmtdLeaseExtender.h:7-15](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdLeaseExtender.h#L7-L15)。

而「每个实例都周期跑它」是在 `MgmtdBackgroundRunner::start` 里注册的——注意它对 primary 与备机一视同仁：

```cpp
// src/mgmtd/background/MgmtdBackgroundRunner.cc
void MgmtdBackgroundRunner::start() {
  if (backgroundRunner_) {
    backgroundRunner_->start(
        "extendLease",
        [this] { return leaseExtender_->extend(); },
        state_.config_.extend_lease_interval_getter());   // 默认 10s
    backgroundRunner_->start("checkClientSessions", ...);
    ...
  }
}
```

> 见 [src/mgmtd/background/MgmtdBackgroundRunner.cc:37-42](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdBackgroundRunner.cc#L37-L42)（`extendLease` 注册），其余后台任务见 [MgmtdBackgroundRunner.cc:43-79](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdBackgroundRunner.cc#L43-L79)。理解这点很关键：**竞争式选举之所以能工作，正是因为所有备机都在持续尝试 `extend()`**——它们平时拿到的是「别人的未过期租约」而安静退出，一旦租约过期就立刻有机会接管。

**(b) `Op::handle` 的事务与 disable 让位**

```cpp
// src/mgmtd/background/MgmtdLeaseExtender.cc
auto handle(MgmtdState &state) -> CoTryTask<void> {
  auto handler = [&](kv::IReadWriteTransaction &txn) -> CoTryTask<std::optional<flat::MgmtdLeaseInfo>> {
    auto checkSelfRes = co_await state.store_.loadNodeInfo(txn, state.selfId());
    CO_RETURN_ON_ERROR(checkSelfRes);
    // 若自己被打了 disabled 标签 → 不续约，返回 nullopt
    if (checkSelfRes->has_value() &&
        flat::findTag(checkSelfRes->value().tags, flat::kDisabledTagKey) != -1)
      co_return std::nullopt;
    auto extendRes = co_await state.store_.extendLease(txn, state.selfPersistentNodeInfo_,
                         state.config_.lease_length().asUs(), state.utcNow(),
                         flat::ReleaseVersion::fromVersionInfo(),
                         state.config_.extend_lease_check_release_version());
    CO_RETURN_ON_ERROR(extendRes);
    co_return *extendRes;
  };
  // ★ 注意第三个参数 expectSelfPrimary=false ★
  auto commitRes = co_await withReadWriteTxn(state, std::move(handler), /*expectSelfPrimary=*/false);
  ...
}
```

> 见 [src/mgmtd/background/MgmtdLeaseExtender.cc:193-215](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdLeaseExtender.cc#L193-L215)。

这里有两个关键细节：

- **`expectSelfPrimary=false`**：`withReadWriteTxn` 默认会在事务开头调 `ensureLeaseValid` 拒绝非 primary 的写（§4.1.c）。但续约恰恰是「可能由非 primary 发起、且目的就是让自己成为 primary」的操作，所以必须跳过这道校验。这是整个文件里最容易看漏、却最点题的一行。
- **disable 让位**：如果管理员给本节点打上 `kDisabledTagKey` 标签，这里返回 `nullopt`，外层就会 `lease.lease.reset()`（[MgmtdLeaseExtender.cc:222-226](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdLeaseExtender.cc#L222-L226)），主动放弃 primary 身份。这是受控切换/维护下线的官方手段（§4.3 详述）。

**(c) `onNewLease`：从 FDB 全量加载**

这是「路由加载」的主体。它在一次读写事务里并发读取六类数据，然后推进 `routingInfoVersion` 并落盘自己的节点信息：

```cpp
// src/mgmtd/background/MgmtdLeaseExtender.cc (onNewLease 内的 handler)
auto handler = [&](kv::IReadWriteTransaction &txn) -> CoTryTask<void> {
  auto [rivRes, nodesRes, configsRes, chainTablesRes, chainsRes, utagsRes] =
      co_await folly::coro::collectAll(
          loadRoutingInfoVersion(ctx, state.store_, txn, newRoutingInfoVersion),  // 读并 +1
          loadAllNodes(ctx, state.store_, txn, allNodes),
          loadAllConfigs(ctx, state.store_, txn, allConfigs),
          loadAllChainTables(ctx, state.store_, txn, chainTables),
          loadAllChains(ctx, state.store_, txn, chains, targetMap),
          loadAllUniversalTags(ctx, state.store_, txn, universalTagsMap));
  ... // 任一失败则整体回滚
  co_return co_await persistNewVersionAndSelfInfo(ctx, state.store_, txn,
              state.selfNodeInfo_, state.selfPersistentNodeInfo_,
              newRoutingInfoVersion, allNodes);
};
auto loadRes = co_await withReadWriteTxn(state, std::move(handler));
```

> 见 [src/mgmtd/background/MgmtdLeaseExtender.cc:128-164](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdLeaseExtender.cc#L128-L164)。注意 `onNewLease` 这一组 helper（`loadRoutingInfoVersion`/`loadAllNodes`/`loadAllConfigs`/`loadAllChainTables`/`loadAllChains`/`loadAllUniversalTags`/`persistNewVersionAndSelfInfo`）都定义在同一文件开头，见 [MgmtdLeaseExtender.cc:20-126](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdLeaseExtender.cc#L20-L126)。`loadRoutingInfoVersion` 把读到的版本号 `+1`（[L20-29](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdLeaseExtender.cc#L20-L29)），`persistNewVersionAndSelfInfo` 把新版本号连同自身节点信息一起写回（[L31-58](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdLeaseExtender.cc#L31-L58)）。

加载成功后，用读到的数据**整体替换**内存 `MgmtdData`：

```cpp
// src/mgmtd/background/MgmtdLeaseExtender.cc (onNewLease 事务成功之后)
if (!loadRes.hasError()) {
  ... // 用读到的 MGMTD 配置更新自身配置
  data.reset(newRoutingInfoVersion, std::move(allNodes), std::move(allConfigs),
             std::move(chainTables), std::move(chains), std::move(targetMap),
             std::move(universalTagsMap));
  auto clientSessionMap = co_await state.clientSessionMap_.coLock();
  clientSessionMap->clear();          // 清空 client 会话(旧 primary 的会话不再有效)
  co_return Void{};
} else {
  CO_RETURN_ERROR(loadRes);           // 失败则下一轮再试
}
```

> 见 [src/mgmtd/background/MgmtdLeaseExtender.cc:166-187](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdLeaseExtender.cc#L166-L187)。`data.reset(...)` 是「重建内存视图」的总入口。

**(d) `MgmtdData::reset`：重建内存并清 `bootstrapping`**

`reset` 把刚读到的全部数据搬进内存，并翻转两个关键状态：

```cpp
// src/mgmtd/service/MgmtdData.cc
void MgmtdData::reset(...) {
  routingInfo.reset(routingInfoVersion, std::move(allNodes), std::move(allChainTables),
                    std::move(allChains), std::move(allTargets));
  { auto cachePtr = routingInfoCache.wlock(); cachePtr->reset(); }  // 清路由缓存
  configMap = std::move(allConfigs);
  lease.bootstrapping = false;        // ★ 加载完成，不再是 bootstrapping ★
  leaseStartTs = SteadyClock::now();  // ★ 记一个单调时钟起点(供时间型 bootstrapping 判定)
  universalTagsMap = std::move(allUniversalTags);
}
```

> 见 [src/mgmtd/service/MgmtdData.cc:146-167](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdData.cc#L146-L167)，重点是 [L164-L165](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdData.cc#L164-L165)。

这里出现了**两种 `bootstrapping`**，不要混淆：

| 名称 | 定义位置 | 含义 | 谁用它 |
| --- | --- | --- | --- |
| `LeaseInfo::bootstrapping`（布尔字段） | [LeaseInfo.h:8](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/LeaseInfo.h#L8) | 「我是否刚成为 primary 且路由还没加载完」 | `currentLease` 用来决定是否对外当 primary；`reset` 会把它清成 `false` |
| `MgmtdData::bootstrapping(config)`（基于时间的方法） | [MgmtdData.cc:169-171](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdData.cc#L169-L171) | `leaseStartTs + bootstrapping_length(2min) > now`，即「新主上任还没满 2 分钟」 | `getRoutingInfo` 把它写进下发给客户端的 `RoutingInfo.bootstrapping` 字段，提示客户端「新主刚上任、视图可能仍在恢复」 |

```cpp
// src/mgmtd/service/MgmtdData.cc
bool MgmtdData::bootstrapping(const MgmtdConfig &config) const {
  return leaseStartTs + config.bootstrapping_length().asUs() > SteadyClock::now();
}
```

> 见 [src/mgmtd/service/MgmtdData.cc:169-171](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdData.cc#L169-L171)。它在 `getRoutingInfo` 里被填进返回结构（[MgmtdData.cc:97](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdData.cc#L97)）。这个时间窗口与 u3-l5 的 target 状态恢复衔接：新主上任的头 2 分钟里，部分 target 可能仍在 syncing，客户端据此可以采取更谨慎的策略（例如重试）。

#### 4.2.4 代码实践：画出 `onNewLease` 一次加载的「读—写」清单

**实践目标**：把新 primary 上任时**到底从 FDB 读/写了哪些 key 前缀**梳理成一张清单，验证「路由信息完整可重建」。

**操作步骤**：

1. 从 [MgmtdLeaseExtender.cc:139-163](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdLeaseExtender.cc#L139-L163) 的 `collectAll` 列出六个 `load*` 调用。
2. 逐一跳到 [MgmtdStore.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/store/MgmtdStore.cc) 里对应的 `loadAllNodes`(L260)/`loadAllConfigs`(L289)/`loadAllChainTables`(L335)/`loadAllChains`(L353)/`loadAllUniversalTags`(L409)/`loadRoutingInfoVersion`(L237)，记录它们各自按哪个 `KeyPrefix` 做范围扫描。
3. 再看 `persistNewVersionAndSelfInfo`（[MgmtdLeaseExtender.cc:31-58](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdLeaseExtender.cc#L31-L58)）写了哪两个 key。

**预期结果**（读 / 写清单）：

| 方向 | key 前缀 / 内容 | 来源 |
| --- | --- | --- |
| 读 | `Single|RoutingInfoVersion` | `loadRoutingInfoVersion` |
| 读 | `NodeTable|*`（全部节点） | `loadAllNodes` |
| 读 | `Config|*`（全部配置） | `loadAllConfigs` |
| 读 | `ChainTable|*`（全部链表） | `loadAllChainTables` |
| 读 | `ChainInfo|*`（全部链） | `loadAllChains` |
| 读 | `UniversalTags|*`（全部标签） | `loadAllUniversalTags` |
| 写 | `Single|RoutingInfoVersion`（版本号 +1） | `persistNewVersionAndSelfInfo` |
| 写 | `NodeTable|<selfNodeId>`（自身节点信息，仅在变化时） | `persistNewVersionAndSelfInfo` |

> 结论：这六读两写覆盖了 `RoutingInfo` 的全部实体。由于它们全部在**同一个读写事务**里，要么整体可见、要么整体回滚——这就保证了「新 primary 要么拿到一份完整一致的视图，要么加载失败下一轮重试」，绝不会出现半新半旧的视图。`routingInfoVersion` 被 +1 并落盘，正是为了让客户端在 `GetRoutingInfo` 时感知到「换主了、需要刷新」（详见 u3-l6）。

> 注：以上「读/写了哪些 key」的结论是直接从源码得出的，可静态验证；但「实际运行中 FDB 里这些 range 的大小」取决于集群规模，属「待本地验证」的运行期数据。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `onNewLease` 在加载成功后要 `clientSessionMap->clear()`？

**参考答案**：`clientSessionMap` 记录的是「哪些客户端持有写打开的文件会话」（见 u4-l5）。这些会话是由**旧 primary** 在内存里维护的、并未持久化到 FDB。新 primary 上任后，旧 primary 内存里的会话表随之消失，新 primary 必须从空开始重新积累。清空它意味着旧会话在新主看来都不存在，客户端下次写时会重新建立会话。这体现了 mgmtd「无状态」的彻底性：凡是没有进 FDB 的内存状态，换主后一律丢弃重建。

**练习 2**：假设 `onNewLease` 这次加载事务因为 FDB 冲突而 commit 失败，会发生什么？会不会出现「内存 `lease` 已更新成自己、但路由没加载」的危险中间态？

**参考答案**：不会。注意 `Op::handle` 的顺序：它先用 `withReadWriteTxn` 跑 `extendLease`（得到 `commitRes`，即 `newLease`），**这时 `extendLease` 已经 commit 成功**（自己已是 FDB 里的 primary）；然后才在 `coScopedLock` 里更新内存 `lease` 并按需调用 `onNewLease`。若 `onNewLease` 的加载事务失败，函数走 `else` 分支 `CO_RETURN_ERROR`，但此时内存里 `lease` **已经被设成了自己、`bootstrapping` 仍为 `true`**。下一轮 `extend()` 时，由于自己已是持约者，`extendLease` 走分支④续约（`leaseStart` 不变），`leaseChangedReason` 因 `bootstrapping==true` 而得 `"still bootstrapping"`，于是**再次触发 `onNewLease` 重试加载**。也就是说：加载失败会自动在后续轮次重试，期间 `currentLease` 因 `bootstrapping==true` 返回 `nullopt`，对外表现为「尚无可信 primary」，不会用半成品视图服务请求。这就是 `leaseChangedReason` 里特意保留 `"still bootstrapping"` 这一项的作用。

**练习 3**：`MgmtdData::bootstrapping(config)`（时间型）与 `LeaseInfo::bootstrapping`（布尔型）既然都叫 bootstrapping，能否合并成一个？

**参考答案**：不能，它们服务不同对象。布尔型面向**服务内部**：决定 `currentLease`/`ensureSelfIsPrimary` 是否承认自己是 primary——必须在「内存路由真的加载完成」那一刻立即翻 `false`（由 `reset` 同步完成），否则会卡住无法服务。时间型面向**客户端**：在 `getRoutingInfo` 里随路由信息下发，告诉客户端「新主上任未满 2 分钟，target 可能仍在恢复」——这是一个**软提示**，需要持续一段时间（2 分钟）让数据恢复跑完（u3-l5/u5-l5），与内存加载是否完成无关。两者维度不同，故分别用「事件触发」和「时间窗口」实现。

---

### 4.3 故障切换：崩溃后自动接管与脑裂避免

#### 4.3.1 概念说明

把 §4.1、§4.2 串起来，就能回答本讲开头的核心问题：**primary 进程崩溃后，集群如何在不丢路由信息的前提下选出新 primary？** 整个过程不需要任何人工干预，由三个机制自动完成：

1. **路由信息不丢**：它本来就在 FDB 里，不在崩溃进程的内存里（§2.1）。崩溃只丢失进程内一些未持久化的辅助状态（如 client session），这些会在新主重建时被合理清空（§4.2 练习 1）。
2. **自动选出新主**：所有备机一直在周期跑 `extend()`；旧租约一旦过期，第一个抢到 FDB 事务的备机成为新主，并立即 `onNewLease` 全量加载。
3. **不脑裂**：通过「租约必须完全过期才能被接管」+「primary 在租约剩 20s 内就主动不再信任自己」这两条规则，保证任一时刻至多只有一个「可信 primary」。

本模块重点把第 3 点讲透，并补全两种切换场景：**崩溃式被动切换**与**disable 式主动让位**。

#### 4.3.2 核心流程：一次崩溃切换的完整时间线

设 P1 为现 primary，P2/P3 为备机；默认配置 `lease_length=60s`、`extend_lease_interval=10s`、`suspicious_lease_interval=20s`。

```
t0      P1 健康：每 10s 续约，leaseEnd ≈ now+60s，内存 lease=P1、bootstrapping=false
        P2/P3 每 10s extend() → 拿到 P1 的未过期租约 → 安静当备机 → 向 P1 发心跳

t1      P1 进程崩溃(sudo kill -9 / OOM / 宕机)，停止 extend()
        此刻 FDB 里 leaseEnd ≈ t1 + (50~60)s

t1+Δ    P2/P3 仍在 extend()，但每次都命中「未过期、primary≠自己」→ 仍当备机
        (Δ ∈ [0, lease_length] 之内 P2/P3 都不会接管)

t_exp = leaseEnd   旧租约在这一刻真正过期

t_exp ~ t_exp+10s  P2(或 P3) 某次 extend() 命中分支③(leaseEnd<now)
                  → FDB 写入新租约 primary=P2, leaseStart=now, leaseEnd=now+60s
                  → P2 内存 lease=P2、bootstrapping=true
                  → onNewLease：六读两写、reset 内存视图、bootstrapping=false
        同一轮里 P3 的 extend() 命中「未过期、primary=P2≠P3」→ 当 P2 的备机
        P3 下一次心跳目标切到 P2(依据 lease.primary 的地址)

t_exp+20s 之后     P2 内存 lease 剩余 <20s 时 currentLease 返回 nullopt，
                  P2 在续约把 leaseEnd 推远后又恢复可信 —— 稳态运行
```

**接管延迟**主要由 `lease_length` 决定。崩溃发生在两次续约之间，旧 `leaseEnd` 距崩溃时刻还剩 50~60s；备机每 10s 轮询一次，故从崩溃到新主上任：

\[
T_{\text{failover}} \in [\,50\text{s},\ 70\text{s}\,] \quad(\text{默认配置})
\]

想缩短切换延迟，可以调小 `lease_length`（同时务必保持 `lease_length ≫ extend_lease_interval`，见 §4.1 练习 1）。

#### 4.3.3 源码精读：为什么不会脑裂

脑裂的定义是：**两个 mgmtd 实例在同一时间窗口内都认为自己是可信 primary 并处理写**。本设计用两条规则把它排除。

**规则一：接管要求旧租约完全过期（分支③）。**

回顾 [MgmtdStore.cc:176-179](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/store/MgmtdStore.cc#L176-L179)：

```cpp
if (leaseEnd < now) {
  // lease already retired, try to start a new lease
  co_return co_await storeMgmtdLeaseInfo(txn, newLeaseInfo);
}
```

新主写入新租约的最早时刻是 `t_exp = 旧 leaseEnd`。

**规则二：旧主在租约剩余 < `suspicious_lease_interval` 时就主动不信任自己。**

回顾 [MgmtdState.cc:69-71](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdState.cc#L69-L71)：

```cpp
bool canTrustLease =
    lease.lease.has_value() && now + config_.suspicious_lease_interval().asUs() < lease.lease->leaseEnd;
```

即旧主认为自己可信的最后时刻是 `leaseEnd - suspicious_lease_interval = t_exp - 20s`。

把两条规则画在同一根时间轴上：

\[
T_{\text{旧主不再可信}} = t_{\text{exp}} - 20\text{s}, \qquad
T_{\text{新主可能上任}} \geq t_{\text{exp}}
\]

\[
\Rightarrow\ \text{无主空窗} = T_{\text{新主上任}} - T_{\text{旧主不再可信}} \geq 20\text{s}
\]

在这段 **≥20s 的空窗**里，**没有任何实例**是可信 primary：

- 旧主 P1 的内存 `leaseEnd` 已逼近 `t_exp`，`currentLease` 返回 `nullopt`，`doAsPrimary`/`ensureSelfIsPrimary` 全部返回 `kNotPrimary`，拒绝一切写 RPC。
- 备机 P2/P3 在 FDB 里的租约 primary 仍是 P1、且未过期，`extendLease` 不会让它们写入，`ensureLeaseValid` 也会因为 `primary.nodeId != 自己` 拒绝。

直到 `t_exp` 之后新主才可能出现。因此「旧主可信」与「新主可信」在时间上被至少 20s 的空窗隔开，**绝无重叠**。这就是 3FS mgmtd 不脑裂的数学保证。`suspicious_lease_interval`(20s) 必须显著大于一次 FDB 事务 + 网络往返的耗时，给「旧主停止服务 → 新主加载完成」留出安全垫。

> 配置位置：`lease_length`/`extend_lease_interval`/`suspicious_lease_interval`/`bootstrapping_length`/`validate_lease_on_write`/`extend_lease_check_release_version` 全部在 [src/mgmtd/service/MgmtdConfig.h:10-34](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdConfig.h#L10-L34)，且都是 `CONFIG_HOT_UPDATED_ITEM`（可热更新，见 u2-l5）。

**规则三（兜底）：写操作在事务里实时校验租约。**

即便上述时间推理有误差，写路径还有 `ensureLeaseValid` 这道在 FDB 事务内的实时校验（§4.1.c）。它读的是 FDB 的**实时**租约，而非内存。两个实例不可能在同一时刻都通过「primary==自己 且 leaseEnd>now」的实时校验，因为那需要两条租约记录同时存在于 FDB，而租约记录是单 key、唯一的。

**受控让位（disable）**：除了崩溃这种被动切换，管理员还可以主动让某个 primary 下线维护。给该节点打 `kDisabledTagKey` 标签后，`Op::handle` 会返回 `nullopt`，外层执行：

```cpp
// src/mgmtd/background/MgmtdLeaseExtender.cc
if (!commitRes->has_value()) {
  LOG_OP_INFO(*this, "self is disabled, release lease");
  dataPtr->lease.lease.reset();   // 主动清空内存租约
  co_return Void{};
}
```

> 见 [src/mgmtd/background/MgmtdLeaseExtender.cc:195-200](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdLeaseExtender.cc#L195-L200)（disable 判定）与 [MgmtdLeaseExtender.cc:222-226](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdLeaseExtender.cc#L222-L226)（释放租约）。被 disable 的实例不再续约，其租约会在 `lease_length` 内自然过期，随后备机正常接管。注意它「主动不续约」而不是「直接删租约」——仍走「过期→接管」的标准路径，因此同样不脑裂。

**备机如何感知新主、并改投门庭**：非主 mgmtd 通过 `MgmtdHeartbeater` 向 primary 发心跳（mgmtd 自己也是集群成员）。它每次发送前先读 `currentLease`，并依据 `lease.leaseStart` 是否变化来决定是否重建到 primary 的连接：

```cpp
// src/mgmtd/background/MgmtdHeartbeater.cc
auto lease = co_await state_.currentLease(start);
if (!lease.has_value()) { ...; co_return; }                 // 没有可信 primary，跳过本轮
else if (lease->primary.nodeId == state_.selfId()) { ...; co_return; }  // 自己就是 primary，不发心跳
if (!sendHeartbeatCtx_ || sendHeartbeatCtx_->lease.leaseStart != lease->leaseStart) {
  auto addrs = flat::extractAddresses(lease->primary.serviceGroups, "Mgmtd");  // 从新租约取 primary 地址
  ...
  sendHeartbeatCtx_->stub = state_.env_->mgmtdStubFactory()->create(addrs[0]); // 重建到新 primary 的客户端
}
```

> 见 [src/mgmtd/background/MgmtdHeartbeater.cc:58-92](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdHeartbeater.cc#L58-L92)，重点 [L66-76](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdHeartbeater.cc#L66-L76)（自己是不是 primary 的判定）与 [L78-89](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdHeartbeater.cc#L78-L89)（依据 `leaseStart` 变化重建连接）。注意它又是用 `leaseStart` 作为「是否换主」的锚点——与 §4.1 续约保留 `leaseStart` 的设计一脉相承。

#### 4.3.4 代码实践：手推一次崩溃切换的「状态变迁表」

**实践目标**：把「P1 崩溃 → P2 接管」过程中，P1/P2/P3 各自内存 `lease`、`bootstrapping`、`currentLease()` 返回值随时间的变化，以及 FDB 租约记录，填成一张表，亲手验证「不脑裂」。

**操作步骤**：

1. 准备一张表，列为：时刻 t、事件、FDB 租约(primary, leaseStart, leaseEnd)、P1 内存(currentLease)、P2 内存(currentLease)、P3 内存(currentLease)。
2. 沿用 §4.3.2 的时间线，依次填入 t0、t1(崩溃)、t1+30s、t_exp、t_exp+5s(P2 接管瞬间)、t_exp+5s 之后稳态。
3. 重点检查：是否存在某个时刻，P1 与 P2 的 `currentLease` 同时非空且都指向各自？

**预期结果**（关键行，时间取相对值，`lease_length=60s`、`suspicious=20s`、假设 t1 时 leaseEnd=t1+55s）：

| 时刻 | 事件 | FDB 租约 | P1 currentLease | P2 currentLease | P3 currentLease |
| --- | --- | --- | --- | --- | --- |
| t0 | 稳态 | P1, end=t1+55s | P1（可信） | nullopt（备机 lease 也是 P1，但 currentLease 仅当 primary==self 才算可信；这里 P2 self≠P1 → nullopt） | nullopt |
| t1 | P1 崩溃 | P1, end=t1+55s | 进程已死 | nullopt | nullopt |
| t1+35s | 旧租约剩余 20s | P1, end=t1+55s | — | nullopt | nullopt |
| t1+40s | 旧租约剩余 15s<20s | P1, end=t1+55s | （若 P1 还活着也会 currentLease=nullopt） | nullopt | nullopt |
| t_exp=t1+55s | 旧租约过期 | P1, end=t1+55s | — | nullopt（命中未过期前仍是 P1） | nullopt |
| t_exp+5s | P2 extend 命中分支③ | **P2**, start=t_exp+5s, end=t_exp+65s | — | nullopt（bootstrapping=true，onNewLease 中） | nullopt |
| t_exp+5s+ε | P2 加载完成 reset | P2, ... | — | **P2（可信）** | nullopt（备机，尚未刷新到 P2） |
| t_exp+15s | 稳态 | P2 | — | P2（可信） | nullopt（已刷新，向 P2 发心跳） |

> **关键观察**：在「P1 currentLease 可信」的最后一列（t1+35s 之前）与「P2 currentLease 可信」的第一列（t_exp+5s+ε）之间，存在约 25s 的空窗，期间**无任何实例可信**。全表没有任何一行出现「P1 与 P2 同时可信」。这就手工验证了不脑裂。
>
> 说明：上表中 `currentLease` 对备机的取值遵循 §4.1 的定义——`currentLease` 仅在「lease 指向自己」时才被 `ensureSelfIsPrimary` 视为可信 primary；备机读到的租约虽指向 P1，但 `primary.nodeId != self`，故对自身而言等同于「不是可信 primary」。P1 崩溃后其内存状态不再可观测，表中以「—」表示。

#### 4.3.5 小练习与答案

**练习 1**：把 `suspicious_lease_interval` 调成 0（即只要 `leaseEnd>now` 就可信），系统还能正常工作吗？

**参考答案**：能选主，但**有脑裂风险**。`suspicious_lease_interval=0` 意味着旧主直到 `leaseEnd` 那一刻才停止信任自己；而新主恰好在 `leaseEnd` 之后就可能上任。考虑到时钟漂移、FDB 事务耗时、网络延迟，旧主的「最后可信瞬间」与新主的「上任瞬间」可能贴在一起甚至重叠，从而出现短暂的「双 primary」。`suspicious_lease_interval`(20s) 就是为了在两者之间硬塞一个安全空窗，它的取值必须覆盖「一次切换全流程」的耗时。

**练习 2**：如果整个集群的 FDB 挂了（不是 mgmtd 挂），mgmtd 还能选出 primary 吗？现有 primary 还能服务吗？

**参考答案**：都不能正常工作。选举依赖 FDB 事务（`extendLease` 无法 commit），加载依赖 FDB 读取（`onNewLease` 全部失败），写操作的 `ensureLeaseValid` 也读不了 FDB。现有 primary 的内存 `lease` 还在，但随着时间推移 `leaseEnd - now < suspicious_lease_interval` 后 `currentLease` 返回 `nullopt`，它也会停止服务。这正是 §2.3 所说「mgmtd 的可用性依赖于 FDB」的体现——FDB 是整个控制面的共识层与事实来源。运维上 FDB 必须以高可用方式部署（多副本）。

**练习 3**：`extendLease` 分支①的版本号保护（禁止 `releaseVersion` 回滚）有什么实际意义？能不能去掉？

**参考答案**：它防止一次**版本回退部署**意外抢走 primary。设想集群正运行新版 mgmtd，运维误把一台旧版二进制起起来：若没有分支①，这台旧机一旦在租约过期窗口里抢到 primary，就会用一个缺少新特性的旧二进制来服务整个集群，可能写入旧版不认识的数据结构或丢失新版字段的语义。有了分支①，旧版 `releaseVersion` 更小，直接被拒绝接管，primary 只会在「同版本或更新版本」的实例之间传递。它是「升级安全性」的护栏，不应去掉。可由 `extend_lease_check_release_version`(默认 true) 在特殊回退场景下临时关闭。

---

## 5. 综合实践

把本讲三个模块合在一起，完成下面这个贯穿性任务。

**任务**：撰写一份《3FS mgmtd primary 崩溃切换事后分析报告》。假设你刚目睹了这样一次故障：集群有 3 个 mgmtd 实例 `mgmtd-1`(primary)、`mgmtd-2`、`mgmtd-3`，某时刻 `mgmtd-1` 因宿主机内核 panic 突然死亡，约一分钟后集群自动恢复，`mgmtd-2` 成为新 primary。请基于源码回答：

1. **路由信息有没有丢？为什么？** 至少引用 `onNewLease` 中六读两写的 key 清单（[MgmtdLeaseExtender.cc:139-163](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdLeaseExtender.cc#L139-L163)）说明新主如何把视图完整重建。
2. **大约多少秒后 `mgmtd-2` 才真正接管？** 给出基于 `lease_length`(60s)、`extend_lease_interval`(10s) 的延迟区间推导（参考 §4.3.2 的公式）。
3. **从 `mgmtd-1` 死亡到 `mgmtd-2` 上任，期间有没有可能出现两个 primary 同时服务？** 用 `suspicious_lease_interval`(20s) 与分支③的「完全过期」条件，给出不脑裂的时间轴论证（参考 §4.3.3）。
4. **`mgmtd-2` 上任后，`mgmtd-3` 如何知道要改向它发心跳？** 引用 `MgmtdHeartbeater` 依据 `leaseStart` 变化重建连接的逻辑（[MgmtdHeartbeater.cc:66-89](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdHeartbeater.cc#L66-L89)）。
5. **如果当时 `mgmtd-1` 不是崩溃，而是管理员想主动下线它，应该怎么做？** 说明 `kDisabledTagKey` 的受控让位路径（[MgmtdLeaseExtender.cc:195-200](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdLeaseExtender.cc#L195-L200)、[L222-226](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdLeaseExtender.cc#L222-L226)）。

**交付物**：一份不超过一页的分析报告，每一论断都要附上对应的源码永久链接与行号。若你在本地有测试集群，可进一步用 `admin_cli list-nodes` 观察 `mgmtd-1` 的 status、用 `set-config mgmtd lease_length=...` 验证切换延迟随 `lease_length` 的变化（运行期数值属「待本地验证」）。

## 6. 本讲小结

- mgmtd 的 primary 选举**不需要独立的共识协议**：所有实例竞争写 FDB 中同一条租约记录（`Single|MgmtdLease`），仲裁完全发生在 `MgmtdStore::extendLease` 的一次读写事务里，FDB 的可串行化就是共识层（§4.1）。
- 选举核心是 `extendLease` 的六分支决策树：**只有旧租约完全过期（`leaseEnd < now`）时，备机才能开新租约接管**（分支③）；健康 primary 续约时**沿用 `leaseStart`**，作为「是否换主」的身份锚点（§4.1）。
- 新主上任后由 `onNewLease` 在一次事务里**六读两写**全量重建内存 `RoutingInfo`，`routingInfoVersion` +1 落盘，并清空 client session；`reset` 把 `LeaseInfo::bootstrapping` 翻回 `false`，使一次接管只加载一次（§4.2）。
- 故障切换**不丢路由**（数据在 FDB）、**自动接管**（备机持续 `extend()`）、延迟约 `lease_length ± extend_lease_interval`（默认 50–70s）（§4.3）。
- **不脑裂**由两条规则保证：接管要求旧租约完全过期 + primary 在租约剩 `suspicious_lease_interval`(20s) 时就主动不再信任自己（`currentLease` 返回 `nullopt`），两者之间留出 ≥20s 的无主空窗；写路径还有 `ensureLeaseValid` 在事务内实时读 FDB 兜底（§4.3）。
- 还存在 `disable` 标签触发的**受控让位**路径，与崩溃切换走相同的「过期→接管」机制；以及对外暴露的**时间型 `bootstrapping`**（上任未满 `bootstrapping_length`=2min）提示客户端新主仍在恢复（§4.2、§4.3）。

## 7. 下一步学习建议

- **u3-l4（ChainTable/Chain/Target 数据模型）**：本讲的 `onNewLease` 会加载这些实体，接下来应深入它们各自的 flat 结构与版本号语义，理解「加载回来的到底是些什么」。
- **u3-l5（Target 状态机与故障检测）**：本讲提到新主任任头 2 分钟 `bootstrapping` 为真、部分 target 可能仍在恢复——这正好衔接 target 的 `serving/syncing/...` 状态转换表与数据恢复触发。
- **u3-l6（路由信息分发与配置管理）**：本讲把 `routingInfoVersion` +1，下一讲讲这个版本号如何被客户端感知并触发刷新，把「换主」的信号传递到整个集群。
- **u5-l5（数据恢复与同步）**：mgmtd 侧的切换只负责视图重建；storage 侧 target 从 offline 恢复到 up-to-date 的真正数据回放，要到存储服务篇才展开。
- **延伸阅读**：对照 `docs/design_notes.md` 中关于 mgmtd 多实例与 primary 选举的描述，验证本讲从源码推出的模型与官方设计意图一致；并可思考「为什么 3FS 选择把共识外包给 FDB，而不是像 etcd/Chubby 那样自建租约/选举」。
