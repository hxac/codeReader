# TargetMap 与链路由

## 1. 本讲目标

本讲聚焦 storage 服务里一个「看起来很小、实则掌管所有数据面流量走向」的组件——**TargetMap**。学完本讲，读者应该能够：

- 说清楚 storage 进程收到一个带 `(chainId, chainVer)` 的读写请求后，如何在本机找到对应的本地 target，并判断自己该不该处理。
- 理解 **chain 版本校验**为什么能拒绝过期的写请求，以及 `allowOutdatedChainVer` 这个「后门」在什么场景下才被打开。
- 掌握 `AtomicallyTargetMap` 的「写时复制 + CAS 重试」原子快照机制，明白为什么读路径在并发更新下不会读到「半个」路由表。
- 说清楚 `isHead / isTail / successor` 三个字段是如何在 `updateRouting` 里从 mgmtd 下发的链结构中**重新推导**出来的，以及为什么 3FS 只记录「后继」而不记录「前驱」。

本讲依赖 u5-l1（storage 服务总览与启动）和 u3-l4（ChainTable/Chain/Target 数据模型与版本号语义），请先确认你已了解：一条 Chain 是一串 `ChainTargetInfo`、`chainVersion` 在成员变更时单调递增、storage 在数据面逐请求校验 `chainVersion`。

## 2. 前置知识

在进入源码前，先用三段话把直觉建立起来。

**「一条链」在单机上长什么样？** 集群里有成千上万条 Chain（CRAQ 复制链），每条链横跨多个 storage 节点。但对**单个** storage 节点而言，它在一指定的 Chain 上最多只持有**一个**本地 target（落盘的那块 SSD 目录）。所以 storage 本地不需要保存「整条链长什么样」，只需要回答两个问题：

1. 「这条 chain 在我这里有 target 吗？是哪个？」——这是 `chainToTarget_` 这张映射表干的事。
2. 「我这个 target 在链里排第几？前面是谁、后面是谁？」——这是 `isHead / isTail / successor` 这几个字段干的事。

**为什么路由查询必须原子？** mgmtd 会不定期推送新的 RoutingInfo（比如某节点宕机、某 target 被移到链尾），storage 收到后要重写整张路由表。如果读请求恰好「读到一半」（旧的 head 还没清、新的 successor 已经写进去），就可能把写请求转发给错误的节点。所以 TargetMap 的查询必须基于一个**一致的全局快照**——`AtomicallyTargetMap` 就是为这个设计的。

**为什么需要版本号？** 链的拓扑会变。一条原本是 `A→B→C` 的链，可能因为 B 宕机变成 `A→C`。这个「变成」不是瞬时的：mgmtd 先 bump `chainVersion` 再下发，client 和 storage 陆续感知。在过渡窗口里，旧版本和新版本会同时存在。版本号就是让每个节点能判断「我手上的链拓扑，和这个请求所期待的链拓扑，是不是同一版」——不一样就拒绝，从而避免写到一个已经不在链里的副本上。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `src/storage/service/TargetMap.h` | 定义 `TargetMap`（可变路由表）与 `AtomicallyTargetMap`（原子包装层），是本讲的核心。 |
| `src/storage/service/TargetMap.cc` | 路由查询 `getByChainId`、版本校验、`updateRouting` 重推导 head/tail/successor、原子 `updateTargetMap` 的实现。 |
| `src/fbs/storage/Common.h` | 定义 `VersionedChainId`、`Successor`、`Target` 三个核心数据结构。 |
| `src/storage/service/StorageOperator.cc` | 读路径 `batchRead` 与写路径 `handleUpdate` 如何调用 TargetMap，包括「加锁后重新校验版本」的关键设计。 |
| `src/storage/service/ReliableForwarding.cc` | `forward` 如何依据 `successor` 决定转发还是就地提交（链尾返回 `kNoSuccessorTarget`）。 |
| `src/storage/service/Components.cc` | 路由信息如何从 mgmtd 流入 storage 并刷新 TargetMap（`refreshRoutingInfo`、`addRoutingInfoListener`）。 |
| `src/storage/store/StorageTargets.h` | `StorageTargets` 负责「物理上」创建/加载本机所有 target，是 `storageTarget` 字段背后对象的来源（本讲作背景理解）。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**链路由**（链 id 如何定位到本机 target）、**版本校验**（为何拒绝过期请求）、**前驱后继**（head/tail/successor 如何推导）。三者层层递进，共同回答「我这个节点，该不该、如何处理这个请求」。

### 4.1 链路由：从 chainId 到本机 target

#### 4.1.1 概念说明

「链路由」要解决的问题是：storage 收到一个请求（读或写），请求里只带了 `VersionedChainId{chainId, chainVer}` 和一个 `chunkId`，storage 必须先判断「这条 chain 在本机有没有 target」。如果有，拿到那个 target 落盘对象（`StorageTarget`）才能真正去读写 SSD；如果没有，就直接回路由错误。

关键认知是：**storage 本地把「chain → target」当作一张扁平哈希表 `chainToTarget_`**。因为单个节点在一条链上至多持有一个 target，所以这张表的 value 就是「本机那个 target 的 id」。这种设计把「链拓扑」这个集群级概念，降维成了「本机有一张 chain→target 的查表」。

读路径还有一个特殊点：CRAQ 是「写全读任何」（见 u5-l3），所以**读可以打链上任意一个 up-to-date 的 target**，不需要像写那样只能打 head。这意味着读路由只需查到本机 target 并校验其状态，全程单机闭环、不跨网络。

#### 4.1.2 核心流程

storage 处理一次请求时，「查本机 target」这步在 `TargetMap::getByChainId` 中完成，伪代码如下：

```
getByChainId(VersionedChainId vChainId, allowOutdatedChainVer):
    1. targetId = chainToTarget_.find(vChainId.chainId)   // 查 chain→target 映射
       找不到 -> kRoutingError "chain not found"
    2. target = targets_.find(targetId)                   // 查 target→Target 对象
       找不到 -> kRoutingError "target not found"
    3. 版本校验（见 4.2，此处先跳过）
    4. 状态校验：
       target.localState == OFFLINE  -> kTargetOffline
       target.storageTarget == nullptr -> kTargetOffline（已卸载）
    5. return target
```

读路径与写路径都用同一个入口，区别只在调用点：读在 `batchRead` 里、写在 `handleUpdate` 与 `processUpdate` 里。

#### 4.1.3 源码精读

`TargetMap` 内部维护两张表：`targets_`（targetId → Target）和 `chainToTarget_`（chainId → targetId）。

[src/storage/service/TargetMap.h:35](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/TargetMap.h#L35) 声明了核心查询接口 `getByChainId`，它带一个 `allowOutdatedChainVer` 开关，是版本校验的「后门」（见 4.2）：

```cpp
// [observers] get target by versioned chain id.
Result<const Target *> getByChainId(VersionedChainId vChainId, bool allowOutdatedChainVer) const;
```

`chainToTarget_` 与 `targets_` 两张私有表：

```cpp
robin_hood::unordered_map<TargetId, Target> targets_;
robin_hood::unordered_map<ChainId, TargetId> chainToTarget_;
```

[src/storage/service/TargetMap.cc:55-77](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/TargetMap.cc#L55-L77) 是「链路由 + 版本校验 + 状态校验」三合一的实现，本模块先看链路由部分（前两步）：

```cpp
Result<const Target *> TargetMap::getByChainId(VersionedChainId vChainId, bool allowOutdatedChainVer) const {
  CHECK_RESULT(targetId, getTargetId(vChainId.chainId));   // chain -> targetId
  CHECK_RESULT(target, getTarget(targetId));               // targetId -> Target*
  // 版本校验、状态校验见 4.1.4 后的 4.2 节
  ...
  return target;
}
```

其中 `getTargetId` 就是查 `chainToTarget_` 这张表（[src/storage/service/TargetMap.cc:35-43](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/TargetMap.cc#L35-L43)），找不到返回 `StorageClientCode::kRoutingError`。

读路径 `batchRead` 如何使用它：**整个批次只取一次快照**，然后用这一个快照遍历所有 `ReadIO`：

[src/storage/service/StorageOperator.cc:90-117](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L90-L117)

```cpp
auto snapshot = components_.targetMap.snapshot();        // 取一次原子快照
...
for (AioReadJobIterator it(&batch); it; it++) {
  auto targetResult = ... snapshot->getByChainId(it->readIO().key.vChainId,
                                                  config_.batch_read_ignore_chain_version());
  ...
  if (UNLIKELY(!target->upToDate())) {                    // public=SERVING 且 local=UPTODATE 才能读
    ... co_return makeError(StorageCode::kTargetStateInvalid, ...);
  }
  it->state().storageTarget = target->storageTarget.get();
  ...
}
```

注意 `upToDate()` 的定义（[src/fbs/storage/Common.h:707](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/storage/Common.h#L707)）：`localState == UPTODATE`。读不仅要求 target 在本机存在，还要求它的本地状态是「数据已追平」，这正是 CRAQ「读任何」下保证读到已提交数据的关卡（脏 target 的 `commitVer != updateVer`，详见 u5-l3）。

#### 4.1.4 代码实践

**实践目标**：用一个真实读请求，把「chainId → 本机 target → 落盘对象」这条链路走一遍，理解为什么读不跨网络。

**操作步骤（源码阅读型实践）**：

1. 打开 `src/storage/service/StorageOperator.cc`，定位 `StorageOperator::batchRead`（第 82 行起）。
2. 跟踪第 90 行 `components_.targetMap.snapshot()`：这是 `AtomicallyTargetMap` 的方法，返回一个 `shared_ptr<const TargetMap>`（见 4.2）。
3. 跟踪第 103–106 行 `snapshot->getByChainId(...)`，进入 `TargetMap.cc:55` 的实现。
4. 注意第 113 行的 `target->upToDate()` 判断——只有本机 target 处于 UPTODATE 才继续，否则整个批次直接回 `kTargetStateInvalid`。
5. 第 118 行 `it->state().storageTarget = target->storageTarget.get()`：拿到了真实的落盘对象指针，后续 AIO 读就靠它。

**需要观察的现象**：整段 `batchRead` 里**没有任何向其他 storage 节点发 RPC 的调用**。读请求一旦在本机 `chainToTarget_` 命中并通过 `upToDate` 校验，就完全是本地 SSD 读取（经 AIO + RDMA 回传给 client，见 u5-l2）。

**预期结果**：你能画出一张图——请求带 `(chainId, chainVer)` 进来，本机用 `chainId` 查表得到 targetId，再查到 `Target`，校验版本与状态后取出 `storageTarget` 去读盘。如果本机不在该链上，立即返回 `kRoutingError`。

#### 4.1.5 小练习与答案

**练习 1**：如果一条 Chain 在本机持有 target，但该 target 刚被 `offlineTarget` 置为 OFFLINE，`getByChainId` 会返回什么？为什么这样设计？

**参考答案**：返回 `StorageCode::kTargetOffline`（见 `TargetMap.cc:66-70`，`localState == OFFLINE` 分支）。这样设计是为了让 client 收到明确错误后换链上其他副本重试（读可换任意 up-to-date 副本；写则要重新向 mgmtd 拿新路由），而不是把请求挂死在一个已经不可用的本地 target 上。

**练习 2**：为什么 `batchRead` 对整个批次只调用一次 `snapshot()`，而不是每个 `ReadIO` 各取一次？

**参考答案**：两个原因。一是性能（取快照要走原子指针加载）；二是一致性——一次批次应该基于「同一时刻」的路由视图处理，若逐 IO 取快照，可能批前半部分基于旧视图、后半部分基于新视图，导致同一批读到的数据来自不一致的拓扑版本。取一次快照保证整批原子。

---

### 4.2 版本校验：拒绝过期请求的守门员

#### 4.2.1 概念说明

「版本校验」是 TargetMap 最关键的防线。每个数据面请求都带 `VersionedChainId{chainId, chainVer}`，其中 `chainVer` 是该请求发起时所依据的链版本号。storage 本机每个 target 也记着自己当前所属的版本 `vChainId.chainVer`。**两者不等（且不允许放宽时），storage 直接拒绝该请求**，返回 `kRoutingVersionMismatch`。

为什么要这么严？因为链拓扑会变：成员增删、target 上下线、target 被移到链尾，都会让 mgmtd 把 `chainVersion` 加 1。如果 storage 不校验，就可能：
- 把写请求转发给一个已经不在链里的节点（按旧 successor 转发）。
- 让一个已经被踢出链的 target 继续接受写，产生「孤儿副本」。
- 让链头判定失效：旧版本的 head 可能已经不是新版本的 head。

版本号让「拓扑变了」这件事变成「一个整数变了」，校验变成一次整数比较，O(1) 且确定性。

但版本校验不是铁板一块。有些场景**必须放宽**：比如读路径在配置 `batch_read_ignore_chain_version` 打开时、比如转发重试时主动用「容忍过期版本」去等新拓扑就绪。这就是 `allowOutdatedChainVer` 这个布尔参数存在的原因——它是「后门」，默认关，只在确有必要时打开。

#### 4.2.2 核心流程

版本校验的完整决策（对应 `getByChainId` 第 3 步）：

```
if target.vChainId != 请求.vChainId  且  not allowOutdatedChainVer:
    return kRoutingVersionMismatch   // 拒绝过期请求
```

在写路径里，版本校验发生了**两次**，这是 3FS 一个容易被忽略但极其重要的设计：

1. **第一次**：进入 `handleUpdate` 前，`processUpdate` 用请求的 `vChainId` 查 target（`StorageOperator.cc:303`）。
2. **第二次**：`handleUpdate` 在拿到 chunk 锁**之后**，**重新**用同一个 `vChainId` 查 target（`StorageOperator.cc:383`）。

为什么要查两次？因为**等锁期间路由可能变了**。chunk 锁是阻塞的（见 u5-l3），一个写请求可能在 `lockChunk` 上挂很久。在这段时间里，mgmtd 推送的新 RoutingInfo 已经把本机 target 的 `vChainId.chainVer` 改了（甚至把本机 target 移出了链头）。如果不二次校验，这个写就会带着旧版本号继续执行，落到错误的拓扑上。二次校验失败会直接返回错误，触发上层（client 或前驱节点）重试拿新路由。

#### 4.2.3 源码精读

`VersionedChainId` 的定义（[src/fbs/storage/Common.h:252-256](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/storage/Common.h#L252-L256)），就是 chainId + chainVer 两个字段，自带 `operator==`：

```cpp
struct VersionedChainId {
  bool operator==(const VersionedChainId &) const = default;
  SERDE_STRUCT_FIELD(chainId, ChainId{});
  SERDE_STRUCT_FIELD(chainVer, ChainVer{});
};
```

版本校验的核心代码在 `getByChainId` 中段（[src/storage/service/TargetMap.cc:58-65](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/TargetMap.cc#L58-L65)）：

```cpp
if (target->vChainId != vChainId && !allowOutdatedChainVer) {
  auto msg = fmt::format("chain {} version mismatch request {} != local {}",
                         vChainId.chainId, vChainId.chainVer, target->vChainId.chainVer);
  XLOG(ERR, msg);
  return makeError(StorageClientCode::kRoutingVersionMismatch, std::move(msg));
}
```

写路径的「二次校验」在 `handleUpdate` 里。先看第一次（进入时由 `processUpdate` 完成，见 [src/storage/service/StorageOperator.cc:301-308](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L301-L308)），再看关键的第二次——**加锁后重查**（[src/storage/service/StorageOperator.cc:368-387](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L368-L387)）：

```cpp
// 2. lock chunk.
folly::coro::Baton baton;
auto lockGuard = target->storageTarget->lockChunk(baton, req.payload.key.chunkId, ...);
if (!lockGuard.locked()) {
  co_await lockGuard.lock();          // ← 可能阻塞很久
}
...
// re-check chain version after acquiring the lock.
auto targetResult = components_.targetMap.getByChainId(req.payload.key.vChainId);  // 第二次校验
if (UNLIKELY(!targetResult)) {
  co_return makeError(std::move(targetResult.error()));   // 版本/状态变了，直接失败
}
target = std::move(*targetResult);    // 用最新拓扑覆盖
```

配置开关 `batch_read_ignore_chain_version`（[src/storage/service/StorageOperator.h:38](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.h#L38)），默认 `false`（严格校验）：

```cpp
CONFIG_HOT_UPDATED_ITEM(batch_read_ignore_chain_version, false);
```

转发路径还会出现「对端版本比我高」的反向情况（[src/storage/service/ReliableForwarding.cc:125-133](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/ReliableForwarding.cc#L125-L133)）：后继节点回包带了更高的 `commitChainVer`，说明对端已经看到更新的拓扑，本节点返回 `kChainVersionMismatch`，触发自己重试去拉新路由。

#### 4.2.4 代码实践

**实践目标**：体会「等锁期间拓扑变化」这个竞态，理解二次校验为何不可省。

**操作步骤（源码阅读型实践，待本地验证）**：

1. 在 `StorageOperator::handleUpdate` 中定位第 383 行的二次校验。
2. 设想如下并发时序（**示例场景，非真实运行**）：
   - 时刻 t0：链 `A→B→C`，`chainVer=5`，client 向 head A 发写请求，`vChainId={chainX, 5}`。
   - 时刻 t1：A 的 `handleUpdate` 第一次校验通过（版本匹配 5）。
   - 时刻 t2：A 调 `lockChunk`，但该 chunk 正被另一个写持有，A 挂起等待。
   - 时刻 t3：B 宕机，mgmtd 把链改成 `A→C`，`chainVer=6`，下发 RoutingInfo，A 的 target `vChainId.chainVer` 变成 6，且 A 的 successor 从 B 变成 C。
   - 时刻 t4：A 拿到 chunk 锁，执行第 383 行二次校验。
3. 推导此时若**没有**二次校验会发生什么：A 会用旧的 `successor=B` 转发写请求给已经宕机的 B，写永远无法完成。有了二次校验，A 立即返回 `kRoutingVersionMismatch`，client 重试后拿到 `chainVer=6` 的新路由，重新发给 head A，A 用新 successor C 完成转发。

**需要观察的现象**：二次校验失败时返回的错误码是 `kRoutingVersionMismatch`，与首次校验失败完全相同——上层无法（也不需要）区分是「进来就过期」还是「等锁时过期」，统一走重试。

**预期结果**：你能用自己的话说明——二次校验是「把阻塞窗口里的拓扑变化」转化为一次可重试的版本错误，从而避免把写转发到失效拓扑上。**待本地验证**：在测试环境用 fault injection（`debugFlags.injectServerError` 注入 `kChainVersionMismatch`）可观察 client 的重试行为。

#### 4.2.5 小练习与答案

**练习 1**：`allowOutdatedChainVer=true` 时，`getByChainId` 还会做哪些校验？版本不匹配会怎样？

**参考答案**：仍会做状态校验——`localState == OFFLINE` 或 `storageTarget == nullptr` 时照常返回 `kTargetOffline`（见 `TargetMap.cc:66-75`），这两个与版本无关。但版本不匹配这一条被跳过：即使请求的 `chainVer` 与本机 `vChainId.chainVer` 不等，也照样返回 target。这用于「明知拓扑在变，但仍要拿到本机 target 去等/轮询」的场景（如转发重试里等待 successor 就绪，见 4.3.3 引用的 `ReliableForwarding.cc:90`）。

**练习 2**：为什么 `batch_read_ignore_chain_version` 默认是 `false`，却被设计成「热更新配置」？

**参考答案**：默认 `false` 是为了正确性——读严格校验版本，保证读到的是请求所期望拓扑下的已提交数据，避免在过渡窗口读到「半新半旧」链上的数据。设计成热更新（`CONFIG_HOT_UPDATED_ITEM`，见 u2-l5）是为了运维灵活：当集群遇到因版本切换风暴导致大量读被误拒、且业务能容忍短暂弱一致时，可以在线把它打开以保吞吐，事后再关回去，无需重启 storage。

---

### 4.3 前驱后继：head / tail / successor 如何推导

#### 4.3.1 概念说明

CRAQ 写是「从链头沿链向下串行化传播」（见 u5-l3）。所以一个 storage 节点在收到写后，必须知道自己「在链里排第几、后面是谁」：

- **`isHead`**：我是不是链头？只有链头能接收 client 的写请求（非 head 收到 client 写直接报错）。
- **`successor`**：我的后继是谁？写完本地后要转发给它。`successor` 同时携带后继节点地址（`nodeInfo`）和后继 target 信息（`targetInfo`），转发时直接用。
- **`isTail`**：我是不是链尾？链尾没有后继，写完本地就直接提交，不需要转发——这是链尾判定提交点的依据。

注意一个微妙的设计选择：**3FS 只显式记录「后继」，不记录「前驱」**。原因有二：
1. 写流向是 head→tail 单向，每个节点只需知道「往哪转发」，不需要知道「从哪来」（请求里自带来源）。
2. `isHead` 本质就是「没有前驱」——排在 `chain.targets` 第一个的 serving target 就是 head。所以「前驱」这个信息被 `isHead` 这个布尔量压缩表达了。

更重要的认知：**`isHead / isTail / successor` 都不是持久化的，也不由本机自行决定，而是每次收到新 RoutingInfo 时从 mgmtd 下发的链结构里「重新推导」出来**。这一点是本讲实践任务的核心——因为只要 mgmtd 下发新拓扑、storage 重算一遍，任何「target 被移到链尾」之类的重排都会自动得到正确的 head/successor/tail，无需任何手工同步。

#### 4.3.2 核心流程

`updateRouting` 的推导逻辑（核心是三步重算）：

```
updateRouting(newRoutingInfo):
  1. 版本单调性：若本地 routingInfoVersion_ > 新版，拒绝（过期）
  2. RESET：把所有 target 的 isHead/isTail/vChainId/publicState/successor 清零
  3. 对每条 chain（其中含本机某 target）：
       a. 在 chain.targets 数组里找到属于本机的那个 target（至多一个）
       b. isHead = (该 target 是 SERVING/SYNCING) 且 (它是 chain.targets[0])
       c. vChainId = {chain.chainId, chain.chainVersion}   ← 版本随拓扑更新
       d. 更新 localState / publicState
       e. 后继推导：从当前位置往后扫，第一个 SERVING 或 SYNCING 的 target 即 successor
          - 若后继是 SYNCING，本链记入 syncingChains_（恢复中）
       f. isTail = (该 target 是 SERVING/SYNCING) 且 (没有 successor)
```

关键不变量：
- `isHead` 一定是 `chain.targets[0]` 且为 SERVING/SYNCING。clients 只往 head 写。
- `isTail` 一定是某个 serving target 且无后继。链尾提交（返回 `kNoSuccessorTarget`）。
- 中间节点有且仅有一个 SERVING/SYNCING 后继。

#### 4.3.3 源码精读

先看 `Target` 与 `Successor` 的字段定义。`Successor`（[src/fbs/storage/Common.h:679-682](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/storage/Common.h#L679-L682)）打包了后继的节点信息（含地址）和 target 信息：

```cpp
struct Successor {
  SERDE_STRUCT_FIELD(nodeInfo, flat::NodeInfo{});     // 含 serviceGroups/endpoints，转发用它取地址
  SERDE_STRUCT_FIELD(targetInfo, flat::TargetInfo{}); // 后继 targetId、publicState 等
};
```

`Target` 的关键字段（[src/fbs/storage/Common.h:685-710](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/storage/Common.h#L685-L710)）：

```cpp
struct Target {
  std::shared_ptr<StorageTarget> storageTarget;   // 落盘对象（offline 时为 nullptr）
  SERDE_STRUCT_FIELD(isHead, false);
  SERDE_STRUCT_FIELD(isTail, false);
  SERDE_STRUCT_FIELD(vChainId, VersionedChainId{});     // 当前所属版本
  SERDE_STRUCT_FIELD(localState, flat::LocalTargetState::INVALID);
  SERDE_STRUCT_FIELD(publicState, flat::PublicTargetState::INVALID);
  SERDE_STRUCT_FIELD(successor, std::optional<Successor>{});   // 后继（无则 nullopt）
  ...
};
```

推导逻辑在 `updateRouting` 中。先看 RESET 阶段（[src/storage/service/TargetMap.cc:146-161](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/TargetMap.cc#L146-L161)）——所有 target 先被「抹平」，确保推导基于干净起点：

```cpp
for (auto &[targetId, target] : targets_) {
  ...  // 记录旧 head/tail/lastSrv 用于变更日志
  target.isHead = false;
  target.isTail = false;
  target.vChainId = VersionedChainId{};
  target.publicState = flat::PublicTargetState::INVALID;
  target.successor = std::nullopt;
}
```

`isHead` 的判定（[src/storage/service/TargetMap.cc:193](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/TargetMap.cc#L193)）：本机 target 必须是 SERVING/SYNCING 且位于链数组首位：

```cpp
bool targetIsServing = targetInfo->publicState == flat::PublicTargetState::SERVING ||
                       targetInfo->publicState == flat::PublicTargetState::SYNCING;
target->isHead = (targetIsServing && it == chain.targets.begin());
```

后继推导（[src/storage/service/TargetMap.cc:221-254](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/TargetMap.cc#L221-L254)）：从本机 target 位置向后扫，找到第一个 SERVING/SYNCING 的 target 作为 successor，并把后继的节点信息解析进来：

```cpp
// 6. update successor.
while (targetIsServing && ++it != chain.targets.end()) {
  auto targetInfo = routingInfo->getTarget(it->targetId);
  if (targetInfo->publicState == flat::PublicTargetState::SERVING) {
    target->successor = Successor{{}, *targetInfo};
  } else if (targetInfo->publicState == flat::PublicTargetState::SYNCING) {
    target->successor = Successor{{}, *targetInfo};
    syncingChains_.push_back(VersionedChainId{chain.chainId, chain.chainVersion});  // 链在恢复
  }
  if (target->successor) {
    auto node = routingInfo->getNode(*targetInfo->nodeId);
    target->successor->nodeInfo = *node;     // 装入后继节点地址
  }
  break;   // 只取紧邻的第一个后继
}
target->isTail = (targetIsServing && !target->successor.has_value());  // 无后继即链尾
```

这三个字段如何被消费：

- **`isHead`** 守门（[src/storage/service/StorageOperator.cc:338-341](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L338-L341)）：client 写只能发给 head。
  ```cpp
  if (UNLIKELY(req.options.fromClient && !target->isHead)) {
    XLOGF(ERR, "non-head node receive a client update request");
    co_return makeError(StorageClientCode::kRoutingError, ...);
  }
  ```
- **`successor`** 决定转发还是提交（[src/storage/service/ReliableForwarding.cc:113-117](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/ReliableForwarding.cc#L113-L117)）：链尾无后继，直接提交。
  ```cpp
  if (!target->successor.has_value()) {
    commitIO.commitChainVer = target->vChainId.chainVer;
    co_return makeError(StorageCode::kNoSuccessorTarget);  // 链尾：就地提交
  }
  ```
- **`isTail`** 的语义已被「无 successor」覆盖，代码里更多用于日志与诊断（如 `StorageOperator.cc:1093` 的 `target.isHead && target.isTail` 表示单副本链）。

#### 4.3.4 代码实践（本讲主实践任务）

**实践目标**：结合链表更新场景，说明当一个 target 被移到链尾时，TargetMap 如何保证后续写请求路由正确。

**场景设定（示例场景）**：一条 CRAQ 链初始为 `A→B→C`（A=head，C=tail），`chainVer=5`。因运维操作（如 `rotateLastSrv` 或重新均衡，见 u3-l5），mgmtd 把 **B 移到链尾**，新链为 `A→C→B`，`chainVer=6`。

**操作步骤（跟踪推导型实践）**：

1. **mgmtd 侧**：bump `chainVersion` 5→6，重新排布 `chain.targets = [A, C, B]`，下发新 RoutingInfo。
2. **storage A 侧**（执行 `updateRouting`，对照 `TargetMap.cc:124-270`）：
   - RESET：A 的 `isHead/isTail/successor/vChainId` 全部清零。
   - 遍历链 `[A, C, B]`，定位本机 target = A（`chain.targets[0]`）。
   - `isHead = true`（A 是 SERVING 且是首位）。
   - `vChainId.chainVer = 6`（关键！版本已更新）。
   - 后继推导：从 A 往后扫，第一个 SERVING 的是 **C**（不再是 B），故 `successor = C`，且装入 C 的节点地址。
   - A 有后继，故 `isTail = false`。
3. **storage C 侧**：
   - RESET 后定位本机 target = C（现在是 `chain.targets[1]`）。
   - `isHead = false`。
   - `vChainId.chainVer = 6`。
   - 后继推导：C 往后扫到 **B**（B 此刻若为 SYNCING 则链记入 `syncingChains_`），`successor = B`。
   - C 有后继，`isTail = false`。
4. **storage B 侧**（被移到链尾）：
   - 定位本机 target = B（`chain.targets[2]`）。
   - `isHead = false`。
   - `vChainId.chainVer = 6`。
   - 后继推导：B 往后已无 target，`successor = nullopt`。
   - **`isTail = true`**——B 现在是链尾，后续收到转发写会就地提交（返回 `kNoSuccessorTarget`）。

**需要观察的现象与「正确性」来源**：上述正确性不靠任何手工同步，而是由三层机制联合保证：

| 机制 | 作用 | 关键代码 |
| --- | --- | --- |
| **重推导** | head/successor/tail 每次都从权威 RoutingInfo 重算，B 移尾后自动得到 `isTail=true`、`successor=nullopt` | `TargetMap.cc:221-254` |
| **版本号 bump** | `chainVer` 5→6 写进每个 target 的 `vChainId`，使任何带旧 `chainVer=5` 的写立即被拒 | `TargetMap.cc:194` + `TargetMap.cc:58-65` |
| **原子快照** | 整张表重写后原子替换，读请求要么看到全旧的 `[A→B→C]`、要么看到全新的 `[A→C→B]`，不会看到「A 转发给 B、但 C 已是 head」的中间态 | `TargetMap.cc:370-382`（见 4.2/4.3 边界） |

5. **写请求在过渡窗口的行为**：若 client 仍持有旧路由（`chainVer=5`）把写发给 head A，A 第一次校验时若已切到 6 则直接 `kRoutingVersionMismatch`；若 A 还没切，写会按旧 successor B 转发，但 B 即将/已经变为链尾——此时**写路径二次校验**（`StorageOperator.cc:383`）和**转发后继版本比对**（`ReliableForwarding.cc:125-133`）会兜住：要么 B 因版本不符拒绝，要么 A 发现对端版本更高而自我失败重试。最终 client 重拉路由拿到 `chainVer=6`，重新发给 head A，A 按新链 `A→C→B` 转发，B 作为新链尾提交。

**预期结果**：你能画出「target 移尾」前后三个节点各自的 `isHead/successor/isTail/vChainId.chainVer` 表格，并说清楚「为什么没有任何一个写会因此落到错误拓扑」——答案是重推导 + 版本号 + 原子快照 + 二次校验四重保险。

> 待本地验证：若有测试集群，可用 `admin_cli` 触发一次链重排（如 `rotate-lastsrv` 或 `update-chain`），观察三个 storage 节点日志中 `target {} becomes head/tail` 与 `version mismatch` 的出现时序（对应 `TargetMap.cc:256-269` 的变更日志）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Target` 里有 `successor` 却没有 `predecessor` 字段？

**参考答案**：因为 CRAQ 写是 head→tail 单向串行，每个节点只需知道「写完转发给谁」，不需要知道「写从哪来」（来源信息在请求里）。而「前驱」信息被 `isHead` 压缩了——`isHead=true` 即「没有前驱」。多记一个 `predecessor` 既无消费方，又增加 `updateRouting` 的维护成本，所以省略。

**练习 2**：如果一条链只有一个 target（单副本），它的 `isHead` 和 `isTail` 分别是什么？写请求会怎么走？

**参考答案**：`isHead=true`（首位且 SERVING）且 `isTail=true`（无后继）。client 写发给它，它 `handleUpdate` 通过 `isHead` 校验，本地落盘后转发——因 `successor` 为空，`forward` 返回 `kNoSuccessorTarget`（`ReliableForwarding.cc:113-117`）即视为链尾提交，写完成。`StorageOperator.cc:1093` 的 `target.isHead && target.isTail` 正是判定单副本链的写法。

**练习 3**：`updateRouting` 末尾对 `LASTSRV` 副本调用了 `resetUncommitted`（`TargetMap.cc:272-279`），这与本讲的「路由」有什么关系？

**参考答案**：当一个 LASTSRV 副本（链上所有 serving 副本全死时临时顶上的最后服务者，见 u3-l5）在新拓扑里重新变为 SERVING/SYNCING/WAITING 时，它身上可能残留「未确认的 pending 写」（因为它曾在脱离正常 ACK 链的状态下被写过）。`resetUncommitted` 把这些 chunk 的 pending 版本回退到已提交版本，避免它在重新加入链后用脏的 `updateVer` 干扰后续 CRAQ 版本不变量。这是路由更新顺带做的「状态清理」，属于路由与数据一致性交接的一部分。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个端到端的路由推演任务。

**任务**：给定一条 4 节点链 `N1→N2→N3→N4`（`chainVer=10`，全部 SERVING），模拟「N2 磁盘故障被 mgmtd 判 offline 并移出链」的完整过程，回答下列问题：

1. mgmtd 把链改成什么？新 `chainVer` 是多少？
2. 四个节点各自执行 `updateRouting` 后，`isHead / successor / isTail / vChainId.chainVer` 分别是什么？（N2 的 `storageTarget` 会变成什么？）
3. 一个在拓扑变更前已发出、带 `chainVer=10` 的 client 写请求到达 head N1，会发生什么？请说明它会经过本讲的哪些校验点（首次校验、二次校验、后继版本比对），最终结果是什么。
4. client 要怎样、经过哪几步才能让下一次写成功落到新拓扑？

**参考答案要点**：

1. 新链 `N1→N3→N4`，`chainVer=11`。N2 的 target publicState 翻为 OFFLINE（由 u3-l5 的状态机），`updateRouting` 中 `updateLocalState` 把 N2 的 localState 从 UPTODATE 翻为 OFFLINE，进而执行 `TargetMap.cc:214-219`：`weakStorageTarget = storageTarget->aliveWeakPtr(); storageTarget = nullptr;`——即 N2 的 `storageTarget` 被置空（弱引用保留以便复活），后续任何对 N2 的 `getByChainId` 会命中 `storageTarget == nullptr` 分支返回 `kTargetOffline`。
2. N1：`isHead=true, successor=N3, isTail=false, chainVer=11`。N3：`isHead=false, successor=N4, isTail=false, chainVer=11`。N4：`isHead=false, successor=nullopt, isTail=true, chainVer=11`。N2：不在新链的 SERVING 集合里，`isHead/isTail=false`，`storageTarget=nullptr`，`chainVer=11`（vChainId 仍被刷新，但它已 offline）。
3. N1 首次校验：若 N1 已切到 11，请求带 10 → `kRoutingVersionMismatch`，直接失败；若 N1 仍为 10，通过首次校验。N1 加 chunk 锁后二次校验（`StorageOperator.cc:383`）：若期间已切到 11，二次校验失败 → `kRoutingVersionMismatch`。即使侥幸两次都通过（N1 全程未切），N1 转发给 successor——若 successor 已变 N3，则用旧 successor=N2 转发会失败（N2 返回 `kTargetOffline` 或网络不通），或 N1 在 `ReliableForwarding.cc:125-133` 发现对端 commitChainVer 更高而自我失败。最终结果：该写以版本错误失败，**不会落到错误拓扑**。
4. client 收到 `kRoutingVersionMismatch` 后，向 mgmtd 重新拉取 RoutingInfo（u3-l6 的「304 式」按需拉取），获得 `chainVer=11` 与新链 `[N1,N3,N4]`，重新把写发给 head N1；N1 全程校验通过后沿 `N1→N3→N4` 转发，N4 作为链尾提交，写成功。

完成本任务后，你应当能独立解释：**TargetMap 用「重推导 + 版本号 + 原子快照 + 写路径二次校验」四件套，把集群拓扑变化安全地传导到数据面，使任何过渡窗口里的写要么成功落到正确拓扑、要么以可重试错误失败，绝不静默落到错误副本。**

## 6. 本讲小结

- **链路由**：storage 用 `chainToTarget_`（chainId→targetId）和 `targets_`（targetId→Target）两张表，把「我是否在某条链上」降维成两次哈希查表；读路径整批取一次快照、命中且 `upToDate()` 即本地读盘，不跨网络。
- **版本校验**：每个请求带 `VersionedChainId`，`getByChainId` 比对请求版本与本机 `vChainId.chainVer`，不等即 `kRoutingVersionMismatch`；写路径在加 chunk 锁前后**各校验一次**，化解「等锁期间拓扑变化」的竞态；`allowOutdatedChainVer` 是受控后门。
- **前驱后继**：`isHead/successor/isTail` 不持久化，每次 `updateRouting` 从 mgmtd 下发的链结构**重推导**；只记后继不记前驱，因写单向流动且 `isHead` 已隐含「无前驱」。
- **原子性**：`AtomicallyTargetMap` 用 `atomic_shared_ptr` + 写时复制 + CAS 重试（`updateTargetMap`），让读永不阻塞、且永远看到一份完整一致的路由快照。
- **正确性闭环**：拓扑变化的安全传导 = 重推导（拿到正确 head/successor/tail）+ 版本号 bump（拒绝过期请求）+ 原子快照（无中间态）+ 写路径二次校验（兜住阻塞窗口），四者缺一不可。
- **链尾语义**：`successor` 为空即链尾，`forward` 返回 `kNoSuccessorTarget` 触发就地提交；单副本链 `isHead && isTail` 同为真。

## 7. 下一步学习建议

本讲把「路由与版本」讲透了，接下来建议：

1. **u5-l5 数据恢复与同步：ResyncWorker**：本讲反复提到的 `SYNCING` 状态、`syncingChains_`、`resetUncommitted` 都指向数据恢复。当一个 offline target 重启后如何追平数据、期间写为何一律走 `full-chunk-replace`，是 TargetMap 状态流转的下半场。
2. **回头精读 u3-l5 Target 状态机与故障检测**：本讲的 `publicState/localState` 与 `updateLocalState` 的转换规则全由 u3-l5 的状态转换表决定；结合本讲，你能完整理解「故障 → mgmtd 重算 publicState → 下发 → storage 重推导 head/successor/tail」的端到端链路。
3. **u5-l3 写路径与 CRAQ 链式复制**：本讲的 `successor/isTail` 与二次校验是 CRAQ 写流水线的前置；结合 u5-l3 的双版本（`updateVer/commitVer`）机制，你能看到「版本号」在链层（chainVer）和 chunk 层（ChunkVer）两个层面的协同。
4. **源码延伸阅读**：通读 `TargetMap.cc:124-283` 的 `updateRouting` 全文，关注其中对 `lastSrvTargets`、`resetUncommitted`、`syncingChains_` 的处理——这些是把「路由更新」与「数据一致性」缝合在一起的关键细节，是理解 3FS 故障切换不丢数据的精髓。
