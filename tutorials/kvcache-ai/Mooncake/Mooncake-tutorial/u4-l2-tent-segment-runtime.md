# TENT 运行时：Segment 管理与注册表

## 1. 本讲目标

本讲是 TENT 系列的第二篇，承接 [u4-l1（TENT 概述与设计动机）](u4-l1-tent-overview.md)。u4-l1 讲清了「TENT 是什么、为什么这样设计」，并点到了运行时 `TransferEngineImpl` 这个总入口。本讲钻进运行时内部，把**段（segment）相关的基础设施**讲透。读完后你应当能够：

1. 说清楚 TENT 运行时里 `SegmentManager`、`SegmentRegistry`、`SegmentTracker`、`ProxyManager`、`ProgressWorker` 这五个组件各自的职责，以及它们为什么必须分开。
2. 理解「段描述符（SegmentDesc）」如何在本机与远端之间流转：本地登记 → 写入注册表 → 远端缓存 → 失效与刷新。
3. 掌握远端段缓存的「版本号 + TTL + 订阅推送」三重失效机制，并知道它为什么是「尽力而为」。
4. 读懂代理暂存（ProxyManager）的事件循环状态机，理解「没有 GPU Direct 时如何用主机内存中转」。
5. 读懂异步进度 worker（ProgressWorker）如何把故障切换（failover）从调用方的轮询循环里解耦出来。
6. 能够对照源码，画出一次 TENT 传输的运行时组件交互图。

> 本讲面向已经读过 u4-l1、知道 `TransferEngine` 门面会转发给 `TransferEngineImpl` 的读者。我们会从运行时的**装配（construct）**开始，顺着「段」这条主线一路讲到底层数据结构与后台线程。

## 2. 前置知识

### 2.1 什么是「段（segment）」

在 TENT 里，一次传输就是「把本地一段内存，搬到某个**目标段**的某个偏移上」。所以「段」是数据存放位置的抽象。一个段由一个 `SegmentDesc`（段描述符）描述，它包含：

- 段的名字（`name`）、类型（`SegmentType::Memory` 或 `File`）、所在机器 ID（`machine_id`）、RPC 地址（`rpc_server_addr`）；
- 一组**缓冲区描述符** `BufferDesc`（地址、长度、所在位置 location、支持的传输类型 transports）。

每个节点启动时都会创建一个**本地段**（local segment），把自己注册过的内存区域登记进去；远端节点的段则叫**远端段**（remote segment）。要往远端搬数据，本地必须先拿到远端段的 `SegmentDesc`——这正是本讲的「段注册表」与「段缓存」要解决的问题。

### 2.2 为什么要分这么多组件

一个朴素的设计是：运行时直接持有一张「所有段」的全局表。但 TENT 面对的是分布式集群，这会带来三个问题：

1. **元数据存哪？** 可能是 etcd 这类中心化存储，也可能是 peer-to-peer 直接 RPC 互查。存储后端应当可插拔。
2. **远端段信息会不会过期？** peer 可能随时注册/注销内存。本地缓存必须能被失效。
3. **本地段的缓冲区会频繁增删**（`registerLocalMemory` / `unregisterLocalMemory`），需要一个专门的登记簿，并在每次变更后同步到注册表。

TENT 用「职责分离」回答了这三个问题，于是有了五个组件。本讲后面会逐一拆解，先用一张表建立直觉：

| 组件 | 一句话职责 | 是否后台线程 |
|------|-----------|-------------|
| `SegmentManager` | 管理本地段、远端段句柄与**线程级远端缓存** | 否 |
| `SegmentRegistry` | 段元数据的**存储后端**抽象（中心化 / p2p） | 否 |
| `SegmentTracker` | 本地段里**已注册内存区域**的登记簿（引用计数） | 否 |
| `ProxyManager` | 主机内存**暂存中转**（GPU Direct 不可用时） | 是（8 个分片线程） |
| `ProgressWorker` | 异步**推进传输进度**、驱动 failover | 是（1 个线程） |

### 2.3 几个会反复出现的术语

| 术语 | 含义 |
|------|------|
| `SegmentID` | 段的句柄，本地是个递增整数；`LOCAL_SEGMENT_ID == 0` 表示本地段 |
| `SegmentDesc` / `SegmentDescRef` | 段描述符 / 其 `shared_ptr` |
| `BufferDesc` | 段内一段已注册内存的描述（地址、长度、location、transports） |
| stage buffer（暂存缓冲） | 代理传输时用于中转的主机内存块 |
| failover（故障切换） | 某条传输失败后，自动换一条传输路径重试 |

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [`mooncake-transfer-engine/tent/src/runtime/segment_manager.cpp`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/segment_manager.cpp) | `SegmentManager` 实现：远端段句柄、线程级缓存、本地段同步 |
| [`mooncake-transfer-engine/tent/include/tent/runtime/segment_manager.h`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/include/tent/runtime/segment_manager.h) | `SegmentManager` 声明，含 `withCachedSegment` 模板与缓存结构 |
| [`mooncake-transfer-engine/tent/src/runtime/segment_registry.cpp`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/segment_registry.cpp) | `CentralSegmentRegistry` / `PeerSegmentRegistry` 实现 |
| [`mooncake-transfer-engine/tent/include/tent/runtime/segment_registry.h`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/include/tent/runtime/segment_registry.h) | `SegmentRegistry` 抽象基类与两个子类声明 |
| [`mooncake-transfer-engine/tent/src/runtime/segment_tracker.cpp`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/segment_tracker.cpp) | `SegmentTracker` 实现：本地段缓冲区的增删查 |
| [`mooncake-transfer-engine/tent/src/runtime/proxy_manager.cpp`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/proxy_manager.cpp) | `ProxyManager` 实现：暂存缓冲与多阶段事件循环 |
| [`mooncake-transfer-engine/tent/src/runtime/progress_worker.cpp`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/progress_worker.cpp) | `ProgressWorker` 实现：事件驱动进度推进 |
| [`mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp) | 运行时总控：装配上述组件、串联一次传输 |
| [`mooncake-transfer-engine/tent/src/runtime/control_plane.cpp`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/control_plane.cpp) | `ControlService`：创建 `SegmentManager`+`Registry`，提供 RPC |
| [`mooncake-transfer-engine/tent/include/tent/runtime/segment.h`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/include/tent/runtime/segment.h) | `SegmentDesc` / `BufferDesc` 等核心数据结构 |

记忆线索：**`TransferEngineImpl`（装配者）→ `ControlService` 持有 `SegmentManager` → `SegmentManager` 委托 `SegmentRegistry` 存元数据、用线程级缓存加速 → `SegmentTracker` 管本地缓冲 → `ProxyManager` / `ProgressWorker` 是两个后台线程**。

## 4. 核心概念与源码讲解

### 4.1 运行时装配：五个组件何时被创建

#### 4.1.1 概念说明

在深入每个组件之前，先回答一个全局问题：**这五个组件是谁、在什么时候创建出来的？** 答案是运行时的 `construct()` 阶段——也就是 `TransferEngineImpl` 构造时一次性装配。理解这条装配链，就理解了它们的拥有关系与生命周期：

- `SegmentRegistry` 由 `ControlService` 在自己的构造函数里创建（根据 `metadata_type` 选中心化或 p2p）；
- `SegmentManager` 由 `ControlService` 创建，并**持有**那个 `SegmentRegistry`；
- `SegmentTracker` 由 `TransferEngineImpl` 创建，绑定到本地段；
- `ProxyManager` 与 `ProgressWorker` 由 `TransferEngineImpl` 在 `construct()` 末尾创建，前者总是创建，后者受配置开关控制。

#### 4.1.2 核心流程

```
TransferEngineImpl(conf)
   └─ construct()
        ├─ 1. 读配置（metadata_type/servers、enable_progress_worker ...）
        ├─ 2. ControlService(type, servers, this)        ← 内部创建 Registry + SegmentManager
        │       └─ metadata->start(port)                 ← 启动 RPC 服务端
        ├─ 3. setupLocalSegment()                         ← 创建 SegmentTracker、写本地段
        ├─ 4. TransportSelector + loadTransports() + install
        ├─ 5. staging_proxy_ = new ProxyManager(this)     ← 暂存代理（总是建）
        └─ 6. if (enable_progress_worker_) ProgressWorker ← 异步进度（可选）
```

#### 4.1.3 源码精读

**`ControlService` 根据元数据类型选择不同的 `SegmentRegistry`。** 这是「存储后端可插拔」的落点：`p2p` 模式用 `PeerSegmentRegistry`（peer 间直接 RPC 互查），其它模式（etcd、redis 等）用 `CentralSegmentRegistry`（中心化存储）：

[`mooncake-transfer-engine/tent/src/runtime/control_plane.cpp:160-166`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/control_plane.cpp#L160-L166) —— `SegmentManager` 始终被一个 `SegmentRegistry` 装进去，二选一：

```cpp
if (type == "p2p") {
    auto agent = std::make_unique<PeerSegmentRegistry>();
    manager_ = std::make_unique<SegmentManager>(std::move(agent));
} else {
    auto agent = std::make_unique<CentralSegmentRegistry>(type, servers);
    manager_ = std::make_unique<SegmentManager>(std::move(agent));
}
```

**`TransferEngineImpl::construct()` 在末尾装配两个后台组件。** 注意 `ProxyManager` 无条件创建，而 `ProgressWorker` 受 `enable_progress_worker` 控制（默认 `false`，见 4.6）：

[`mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp:339-344`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L339-L344) —— 暂存代理总是建，进度 worker 按开关建：

```cpp
staging_proxy_ = std::make_unique<ProxyManager>(this);

if (enable_progress_worker_) {
    progress_worker_ = std::make_unique<ProgressWorker>(this);
    progress_worker_->start();
}
```

**`setupLocalSegment()` 把拓扑信息塞进本地段，并创建 `SegmentTracker`。** 本地段的 `SegmentDesc` 在此刻被填充名字、类型、机器 ID、RPC 地址与拓扑，随后交给 `SegmentTracker`：

[`mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp:263-274`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L263-L274) —— 填充本地段并创建 tracker，最后同步到注册表：

```cpp
auto& manager = metadata_->segmentManager();
auto segment = manager.getLocal();
segment->name = local_segment_name_;
segment->type = SegmentType::Memory;
segment->machine_id = getMachineID();
segment->rpc_server_addr = buildIpAddrWithPort(hostname_, port_, ipv6_);
auto& detail = std::get<MemorySegmentDesc>(segment->detail);
detail.topology = *(topology_.get());
local_segment_tracker_ = std::make_unique<SegmentTracker>(segment);
return manager.synchronizeLocal();
```

注意最后一行 `synchronizeLocal()`——它会把本地段写进注册表，让远端能查到「我」。

#### 4.1.4 代码实践

**实践目标**：阅读 `construct()` 全文，把五个组件的创建顺序排成一条时间线。

**操作步骤**：

1. 打开 [`transfer_engine_impl.cpp`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp)，定位 `construct()`（第 276 行起）。
2. 在纸上画出「谁创建了谁」的依赖箭头：`TransferEngineImpl` → `ControlService` → (`SegmentRegistry`, `SegmentManager`)；`TransferEngineImpl` → (`SegmentTracker`, `ProxyManager`, `ProgressWorker`)。
3. 找到 `deconstruct()`（第 385 行起），对比**销毁顺序**与创建顺序——注意 `progress_worker_` 先停、`staging_proxy_` 先析构。

**需要观察的现象**：销毁顺序与创建顺序大致相反，且 `deconstruct()` 里有注释解释「为什么 staging_proxy_ 必须在 local_segment_tracker_ 之前析构」。

**预期结果**：你能复述「`ProgressWorker` 默认不建、`ProxyManager` 默认建、`SegmentRegistry` 的具体类型由 `metadata_type` 决定」这三条结论，并指出各自在源码中的行号。

**待本地验证**：若已编译 TENT，可在构造 `TransferEngineImpl` 后断点观察 `metadata_->segmentManager()` 是否非空、`staging_proxy_` 是否非空、`progress_worker_` 是否为 `nullptr`（默认配置下应为空）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ProxyManager` 和 `ProgressWorker` 不像 `SegmentManager` 那样放在 `ControlService` 里，而是直接由 `TransferEngineImpl` 持有？

> **参考答案**：`SegmentManager` 解决的是「元数据在哪里、怎么缓存」，属于控制面（control plane），与 RPC 服务端同生共死，所以放在 `ControlService` 里。而 `ProxyManager` 要回调 `TransferEngineImpl::submitTransfer` / `progressBatch` 来搬数据，`ProgressWorker` 要回调 `progressBatch` 来推进进度——它们都是**数据面**组件，强依赖 `TransferEngineImpl` 自身的数据通路，因此由运行时直接持有更自然，生命周期也与运行时一致。

**练习 2**：`deconstruct()` 里为什么先 `progress_worker_->stop()` 再销毁 batch？

> **参考答案**：`ProgressWorker` 的后台线程会通过 `progressBatch` 解引用 `BatchID` 到 `Batch*`。若先销毁 batch，worker 线程可能正持有过期指针导致 UAF。先 stop 并 join worker，保证它不再触碰任何 batch，再做 batch 的回收（见 `transfer_engine_impl.cpp:388-393` 的注释）。

---

### 4.2 SegmentManager：段句柄与远端缓存

#### 4.2.1 概念说明

`SegmentManager` 是段管理的「门面」，它解决三件事：

1. **本地段**：持有并返回本机的 `SegmentDesc`（`getLocal()`），并在内存变更后把它同步出去（`synchronizeLocal()`）。
2. **远端段句柄**：用户用 `openSegment(name)` 拿到一个 `SegmentID`，内部维护「名字 ↔ ID」的双向映射（`openRemote` / `closeRemote`）。这个 ID 是**本地递增整数**，只在当前进程内有有意义。
3. **远端段缓存**：真正去注册表拉取远端 `SegmentDesc` 很贵（RPC / etcd 访问），所以每个线程维护一份缓存（`getRemoteCached`），并配合失效机制保证不过期。

#### 4.2.2 核心流程

远端段描述符的获取与失效，是一个典型的「**缓存 + 版本号 + TTL + 推送失效**」组合：

```
getRemoteCached(handle):
    取本线程的 tl_remote_cache_
    若 (距上次刷新 > TTL) 或 (本地 version_ 变了):
        清空本线程缓存              ← 粗粒度失效
    若缓存命中: 直接返回
    若缓存未命中:
        走 registry_->getSegmentDesc() 拉取   ← 慢路径
        存入缓存
        异步向 peer 订阅「段更新推送」         ← 尽力而为的主动失效

synchronizeLocal():                    ← 本地段变更后调用
    registry_->putSegmentDesc(local_desc_)  ← 写注册表
    向所有 subscriber 异步发 NotifySegmentUpdated
```

三重失效机制的设计意图：

- **TTL（默认 1 小时）**：兜底，防止推送丢失导致永久陈旧；
- **版本号 `version_`**：本地主动失效（`invalidateAllCacheForRemote`）时自增，让所有线程下次访问时整体刷新；
- **订阅推送**：peer 间互相订阅，谁更新了段就主动通知对方失效缓存，把陈旧窗口压到最小。

#### 4.2.3 源码精读

**远端段句柄：`openRemote` / `closeRemote`。** 名字到 ID 的映射用读写自旋锁 `RWSpinlock` 保护，ID 由原子计数器 `next_id_` 自增分配。每次增删都会自增 `version_`，触发缓存刷新：

[`mooncake-transfer-engine/tent/src/runtime/segment_manager.cpp:37-50`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/segment_manager.cpp#L37-L50) —— 已开过的段复用旧 ID，新段分配新 ID 并自增版本号：

```cpp
RWSpinlock::WriteGuard guard(lock_);
if (name_to_id_map_.count(segment_name)) {
    handle = name_to_id_map_.at(segment_name);
} else {
    handle = next_id_.fetch_add(1, std::memory_order_relaxed);
    name_to_id_map_[segment_name] = handle;
    id_to_name_map_[handle] = segment_name;
    version_.fetch_add(1, std::memory_order_relaxed);
}
```

**远端段缓存：`getRemoteCached`。** 这是热路径。它先检查 TTL 与版本号决定是否清空本线程缓存，未命中再走 `getRemote`（进而调用 `registry_`）：

[`mooncake-transfer-engine/tent/src/runtime/segment_manager.cpp:63-98`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/segment_manager.cpp#L63-L98) —— 命中即返回；未命中拉取并异步订阅推送：

```cpp
auto &cache = tl_remote_cache_.get();
auto current_ts = getCurrentTimeInNano();
auto current_version = version_.load(std::memory_order_relaxed);
if (current_ts - cache.last_refresh >
        static_cast<uint64_t>(TENT_SEGMENT_DESC_TTL_MS) * 1000000 ||
    cache.version != current_version) {
    cache.id_to_desc_map.clear();
    cache.last_refresh = current_ts;
    cache.version = current_version;
}
if (!cache.id_to_desc_map.count(handle)) {
    SegmentDescRef desc_ref;
    auto status = getRemote(desc_ref, handle);   // 慢路径
    if (!status.ok()) return status;
    cache.id_to_desc_map[handle] = desc_ref;
    // ... 异步向 peer 订阅段更新推送（best-effort）
    ControlClient::subscribeSegmentUpdateAsync(peer_rpc_addr, local_rpc_addr);
}
desc = cache.id_to_desc_map[handle].get();
```

注意 TTL 宏定义在头文件里，默认 1 小时，注释说明它只是「推送丢失时的兜底」：

[`mooncake-transfer-engine/tent/include/tent/runtime/segment_manager.h:34-37`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/include/tent/runtime/segment_manager.h#L34-L37) —— TTL 是陈旧上界，不是正常失效手段：

```cpp
// Fallback refresh interval for cached remote SegmentDesc. Invalidation
// normally comes from the best-effort SegmentUpdate push; this TTL only bounds
// staleness if a push is lost.
#define TENT_SEGMENT_DESC_TTL_MS (60 * 60 * 1000)  // 1h
```

**`withCachedSegment`：带自动重试的缓存访问模板。** 很多调用点（如选传输类型、解析请求边界）需要「拿到段描述符 → 做某操作」，而操作可能因为缓存陈旧返回 `NeedsRefreshCache`。这个模板把「失效 → 重新拉取 → 重试一次」封装起来：

[`mooncake-transfer-engine/tent/include/tent/runtime/segment_manager.h:78-114`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/include/tent/runtime/segment_manager.h#L78-L114) —— 本地段直通；远端段命中失败则失效重试一次：

```cpp
template <typename Func>
Status withCachedSegment(SegmentID segment_id, Func operation) {
    // Local segment: no cache lookup or retry required.
    if (segment_id == LOCAL_SEGMENT_ID) {
        return operation(getLocal().get());
    }
    SegmentDesc *desc = nullptr;
    CHECK_STATUS(getRemoteCached(desc, segment_id));
    Status res = operation(desc);
    if (!res.IsNeedsRefreshCache()) {
        return res;
    }
    // 缓存陈旧：失效后重新拉取，再试一次
    invalidateRemote(segment_id);
    CHECK_STATUS(getRemoteCached(desc, segment_id));
    res = operation(desc);
    if (res.IsNeedsRefreshCache()) {
        res = Status::InvalidEntry("Segment refetched ... but still invalid");
    }
    return res;
}
```

**本地段同步：`synchronizeLocal`。** 每次本地内存增删后调用。它先清掉 JSON 序列化缓存，再把本地段写进注册表，最后向所有订阅者异步推送「我更新了」：

[`mooncake-transfer-engine/tent/src/runtime/segment_manager.cpp:193-225`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/segment_manager.cpp#L193-L225) —— 写注册表 + 快照订阅者 + 逐个异步通知（失败则剔除该订阅者）：

```cpp
{
    std::lock_guard<std::mutex> g(local_json_cache_mu_);
    local_json_cache_.reset();
}
CHECK_STATUS(registry_->putSegmentDesc(local_desc_));

std::vector<std::string> subscribers_snapshot;
{ /* 读快照 subscribers_ */ }

for (const auto &subscriber : subscribers_snapshot) {
    ControlClient::notifySegmentUpdatedAsync(
        subscriber, local_desc_->name,
        /* on_failure */ [subscribers, lock, subscriber] {
            RWSpinlock::WriteGuard guard(*lock);
            subscribers->erase(subscriber);   // peer 已下线则剔除
        });
}
```

这里有个值得品味的工程细节：**先拷一份订阅者快照再发 RPC**，避免持锁发 RPC 导致死锁（注释指出协程回调可能在当前线程同步执行）。

**JSON 序列化缓存：`getLocalDumpedJson`。** peer 通过 RPC 拉取本地段时，服务端要把 `SegmentDesc` 序列化成 JSON。为避免每次都重新 dump，结果被缓存，只在 `synchronizeLocal` 时失效：

[`mooncake-transfer-engine/tent/src/runtime/segment_manager.cpp:178-191`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/segment_manager.cpp#L178-L191) —— 双检锁缓存 JSON dump：

```cpp
std::shared_ptr<const std::string> SegmentManager::getLocalDumpedJson() {
    {
        std::lock_guard<std::mutex> g(local_json_cache_mu_);
        if (local_json_cache_) return local_json_cache_;
    }
    json j = *local_desc_;
    auto computed = std::make_shared<const std::string>(j.dump());
    std::lock_guard<std::mutex> g(local_json_cache_mu_);
    if (!local_json_cache_) {
        local_json_cache_ = computed;
    }
    return local_json_cache_;
}
```

`ControlService::onGetSegmentDesc` 直接返回这个缓存，于是并发 peer 拉取共享同一份 JSON：

[`mooncake-transfer-engine/tent/src/runtime/control_plane.cpp:227-232`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/control_plane.cpp#L227-L232) —— RPC 处理函数复用缓存：

```cpp
void ControlService::onGetSegmentDesc(const std::string_view& request,
                                      std::string& response) {
    auto cached = manager_->getLocalDumpedJson();
    response = *cached;
}
```

#### 4.2.4 代码实践

**实践目标**：跟踪一次远端段描述符的拉取与缓存命中。

**操作步骤**：

1. 在 `getRemoteCached`（`segment_manager.cpp:63`）入口处，设想给它加一行日志（**仅作阅读练习，不要真的改源码**）：`LOG(INFO) << "getRemoteCached handle=" << handle << " hit=" << cache.id_to_desc_map.count(handle);`
2. 阅读调用方 `TransferEngineImpl::getSegmentInfo`（`transfer_engine_impl.cpp:486`），它对远端段就是调 `getRemoteCached`。
3. 推演：同一个 `target_id` 连续两次 `getSegmentInfo`，第一次未命中走 `getRemote`（拉取 + 订阅），第二次命中直接返回。两次之间若收到 peer 的 `NotifySegmentUpdated`，`invalidateAllCacheForRemote` 会自增 `version_`，导致下次访问整体刷新。

**需要观察的现象**：缓存命中时**完全不接触注册表**，也不发 RPC；未命中时才触发拉取与订阅。

**预期结果**：你能回答「为什么 TENT 在高频轮询 `getTransferStatus` 时不会每次都打爆 etcd / peer RPC」——因为选传输类型用的 `getRemoteCached` 走的是线程级缓存。

**待本地验证**：若启用 `verbose` 配置，可在日志里观察 `Opened segment #N: <name>`（`segment_manager.cpp:46`）来确认远端段首次打开的时机。

#### 4.2.5 小练习与答案

**练习 1**：`invalidateAllCacheForRemote(name)` 收到的参数 `segment_name` 在函数体里被 `(void)name;` 忽略了，转而自增全局 `version_`。为什么不做「按段精确失效」？

> **参考答案**：源码注释（`segment_manager.cpp:131-139`）写得很直白：精确的按段版本会引入额外复杂度，且可能拖慢 `getRemoteCached` 热路径。由于段更新本就罕见，整体失效（自增 `version_`，让所有线程下次整体刷新）的代价可以接受，换来热路径的极简（只比对一个原子版本号）。这是典型的「用粗粒度换低延迟」的取舍。

**练习 2**：`getRemoteCached` 里，缓存未命中时会异步调用 `subscribeSegmentUpdateAsync`，但注释说「correctness should not depend on this」。如果这个订阅 RPC 永远失败，系统还能正确工作吗？

> **参考答案**：能。订阅推送只是把「陈旧窗口」压小的优化手段；真正的正确性兜底是 TTL（1 小时后强制刷新）以及 `withCachedSegment` 的「失效重试一次」逻辑。推送丢失最坏只会让某个 peer 在最多 1 小时内用到陈旧描述符，而一旦该陈旧导致操作返回 `NeedsRefreshCache`，模板会立即失效并重新拉取。

---

### 4.3 SegmentRegistry：段元数据的两种存储后端

#### 4.3.1 概念说明

`SegmentRegistry` 是段元数据存储的**抽象接口**，只定义三个纯虚方法：

- `getSegmentDesc`：按名字取一个段的描述符；
- `putSegmentDesc`：写入/更新一个段的描述符；
- `deleteSegmentDesc`：删除一个段。

它有两个具体实现，对应两种部署形态：

- **`CentralSegmentRegistry`**：中心化存储。底层是一个 `MetaStore` 插件（可以是 etcd、redis 等），所有节点把段描述符写到同一个地方，互查时都读这里。
- **`PeerSegmentRegistry`**：点对点。没有中心存储，要查别人的段就直接向对方的 RPC 服务端发 `GetSegmentDesc` 请求。

#### 4.3.2 核心流程

两种实现的 `getSegmentDesc` 数据来源不同，但对外都返回反序列化后的 `SegmentDesc`：

```
CentralSegmentRegistry::getSegmentDesc(name):
    key = "mooncake/tent/" + name
    plugin_->get(key, jstr)            ← 从 etcd/redis 读 JSON 字符串
    desc = json::parse(jstr)           ← 反序列化

PeerSegmentRegistry::getSegmentDesc(name):
    ControlClient::getSegmentDesc(name, response)  ← RPC 问 peer 要 JSON
    desc = json::parse(response)
```

注意中心化实现给所有 key 加了统一前缀 `mooncake/tent/`，相当于一个命名空间，避免和别的系统存在同一个 etcd 里的 key 冲突。

#### 4.3.3 源码精读

**抽象接口与统一 key 前缀。**

[`mooncake-transfer-engine/tent/include/tent/runtime/segment_registry.h:34-50`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/include/tent/runtime/segment_registry.h#L34-L50) —— 三个纯虚方法构成存储后端契约：

```cpp
class SegmentRegistry {
   public:
    virtual Status getSegmentDesc(SegmentDescRef &desc,
                                  const std::string &segment_name) = 0;
    virtual Status putSegmentDesc(SegmentDescRef &desc) = 0;
    virtual Status deleteSegmentDesc(const std::string &segment_name) = 0;
};
```

[`mooncake-transfer-engine/tent/src/runtime/segment_registry.cpp:27-30`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/segment_registry.cpp#L27-L30) —— 统一的 key 前缀：

```cpp
static inline std::string getFullMetadataKey(const std::string &segment_name) {
    const static std::string kCommonKeyPrefix = "mooncake/tent/";
    return kCommonKeyPrefix + segment_name;
}
```

**`CentralSegmentRegistry`：委托给 `MetaStore` 插件。** `MetaStore::Create(type, servers)` 根据类型（如 etcd、redis）创建具体客户端。get/put/remove 都是把 JSON 字符串塞进/取出存储：

[`mooncake-transfer-engine/tent/src/runtime/segment_registry.cpp:37-58`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/segment_registry.cpp#L37-L58) —— 读时反序列化、写时序列化：

```cpp
Status CentralSegmentRegistry::getSegmentDesc(SegmentDescRef &desc,
                                              const std::string &segment_name) {
    std::string jstr;
    desc = nullptr;
    auto status = plugin_->get(getFullMetadataKey(segment_name), jstr);
    if (!status.ok()) return status;
    desc = std::make_shared<SegmentDesc>();
    *desc = json::parse(jstr).get<SegmentDesc>();
    return Status::OK();
}

Status CentralSegmentRegistry::putSegmentDesc(SegmentDescRef &desc) {
    json j = *desc;
    return plugin_->set(getFullMetadataKey(desc->name), j.dump());
}
```

**`PeerSegmentRegistry`：直接 RPC 问 peer。** 它的 `putSegmentDesc` / `deleteSegmentDesc` 是空操作（p2p 模式下「本地段」直接通过 RPC 被别人拉取，无需主动写中心存储）：

[`mooncake-transfer-engine/tent/src/runtime/segment_registry.cpp:68-81`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/segment_registry.cpp#L68-L81) —— p2p 拉取走 `ControlClient::getSegmentDesc`：

```cpp
Status PeerSegmentRegistry::getSegmentDesc(SegmentDescRef &desc,
                                           const std::string &segment_name) {
    std::string response;
    CHECK_STATUS(ControlClient::getSegmentDesc(segment_name, response));
    if (response.empty()) {
        return Status::InvalidEntry(std::string("Segment ") + segment_name +
                                    "not found" LOC_MARK);
    }
    desc = std::make_shared<SegmentDesc>();
    *desc = json::parse(response).get<SegmentDesc>();
    return Status::OK();
}
```

两种实现的对比：

| 维度 | `CentralSegmentRegistry` | `PeerSegmentRegistry` |
|------|--------------------------|-----------------------|
| 触发条件 | `metadata_type != "p2p"` | `metadata_type == "p2p"` |
| 数据来源 | `MetaStore` 插件（etcd/redis…） | peer 的 RPC 服务端 |
| key 命名空间 | `mooncake/tent/<name>` | 无（名字即段名，p2p 下段名常为 `ip:port`） |
| `putSegmentDesc` | 写入存储 | 空操作（peer 被动响应拉取） |

#### 4.3.4 代码实践

**实践目标**：搞清楚一次 `registerLocalMemory` 之后，本地段描述符是如何落到存储后端的。

**操作步骤**：

1. 从 `TransferEngineImpl::registerLocalMemory`（`transfer_engine_impl.cpp:621`）出发，看它在末尾调用 `metadata_->segmentManager().synchronizeLocal()`（第 680 行）。
2. 跟进 `SegmentManager::synchronizeLocal`（`segment_manager.cpp:193`），它调用 `registry_->putSegmentDesc(local_desc_)`。
3. 此时 `registry_` 的具体类型取决于你的 `metadata_type`：若是 `etcd`，就是 `CentralSegmentRegistry::putSegmentDesc`（写 etcd）；若是 `p2p`，就是 `PeerSegmentRegistry::putSegmentDesc`（空操作，因为本地段会被 peer 主动拉取）。
4. 画出这条调用链：`registerLocalMemory → synchronizeLocal → putSegmentDesc → (MetaStore::set | no-op)`。

**需要观察的现象**：中心化模式下，每次注册内存都会产生一次 etcd/redis 写；p2p 模式下则完全不写中心存储。

**预期结果**：你能解释「为什么 p2p 模式下 `putSegmentDesc` 是空操作」——因为 p2p 没有「中心写」的概念，peer 需要时直接 RPC 拉取对方的本地段（由 `onGetSegmentDesc` 返回缓存好的 JSON）。

**待本地验证**：若用 etcd 模式，可在 etcd 里 `etcdctl get mooncake/tent/<segment_name>` 直接看到序列化后的 `SegmentDesc` JSON。

#### 4.3.5 小练习与答案

**练习 1**：`SegmentManager` 持有的是 `std::unique_ptr<SegmentRegistry>`（基类指针）。这种设计带来的好处是什么？

> **参考答案**：多态 + 实现可替换。`SegmentManager` 只依赖 `SegmentRegistry` 的抽象接口（get/put/delete），不关心底层是 etcd、redis 还是 p2p RPC。新增一种存储后端只需再写一个 `SegmentRegistry` 子类，`SegmentManager` 的代码完全不用改——这就是面向接口编程的好处（见 `segment_manager.h:159` 的成员声明）。

**练习 2**：为什么 `PeerSegmentRegistry::putSegmentDesc` 直接返回 `Status::OK()` 而不报错？本地段的更新难道不需要让别人知道吗？

> **参考答案**：p2p 模式下，本地段始终以「被拉取」的方式被别人看到——peer 调 `GetSegmentDesc` RPC，本地 `onGetSegmentDesc` 返回最新缓存。所以「写」这个动作对 p2p 没有意义。而让别人「知道更新了」靠的是 `synchronizeLocal` 里的 `notifySegmentUpdatedAsync` 推送（失效缓存），与存储后端无关。中心化模式才需要把新描述符写到共享存储。

---

### 4.4 SegmentTracker：本地段的缓冲区登记簿

#### 4.4.1 概念说明

`SegmentTracker` 管的是**本地段里那些具体的内存区域**（`BufferDesc`）。它直接绑定到一个 `SegmentDescRef`（就是本地段），维护其 `MemorySegmentDesc::buffers` 列表。每当用户调用 `registerLocalMemory`，就往这个列表里加一个 `BufferDesc`；`unregisterLocalMemory` 就移除一个。

它的核心特色是**引用计数**：同一段地址可以重复注册，只有引用计数归零时才真正从各 transport 里注销。同时，缓冲区列表始终按地址排序，方便快速查找。

#### 4.4.2 核心流程

```
SegmentTracker::addInBatch(desc_list, callback):
    对每个 desc:
        若 (addr, length) 已存在 → ref_count++，标记 found
        否则 → 放入 new_desc_list
    callback(new_desc_list)        ← 让调用方把它注册进各 transport
    把 new_desc_list 追加进 buffers
    按 addr 重新排序 buffers       ← 保持有序，便于二分/线性查找
```

排序规则有个小细节：地址相同时，**长度大的排前面**（`lhs.length > rhs.length`，注释 `prefer large interval`），这样查找时优先命中更大的区间。

#### 4.4.3 源码精读

**批量添加与回调。** `registerLocalMemory` 会把「真正注册到 transport」的逻辑作为回调 `callback` 传进来，`SegmentTracker` 负责去重和引用计数，回调负责实际的 transport 注册：

[`mooncake-transfer-engine/tent/src/runtime/segment_tracker.cpp:86-123`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/segment_tracker.cpp#L86-L123) —— 先去重（已存在则引用计数 +1），新条目交给回调注册，最后排序：

```cpp
Status SegmentTracker::addInBatch(
    std::vector<BufferDesc>& desc_list,
    std::function<Status(std::vector<BufferDesc>&)> callback) {
    std::vector<BufferDesc> new_desc_list;
    for (auto& desc : desc_list) {
        bool found = false;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            auto& detail = std::get<MemorySegmentDesc>(local_desc_->detail);
            for (auto& buf : detail.buffers) {
                if (buf.addr == desc.addr && buf.length == desc.length) {
                    buf.ref_count++;        // 重复注册：引用计数 +1
                    found = true;
                    break;
                }
            }
        }
        if (!found) new_desc_list.push_back(std::move(desc));
    }
    auto status = callback(new_desc_list);   // 让调用方注册进 transport
    if (!status.ok()) return status;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        auto& detail = std::get<MemorySegmentDesc>(local_desc_->detail);
        for (auto& new_desc : new_desc_list) detail.buffers.push_back(new_desc);
        std::sort(detail.buffers.begin(), detail.buffers.end(),
                  [](const BufferDesc& lhs, BufferDesc& rhs) -> bool {
                      if (lhs.addr < rhs.addr) return true;
                      if (lhs.addr > rhs.addr) return false;
                      return lhs.length > rhs.length;  // prefer large interval
                  });
    }
    return Status::OK();
}
```

**移除与引用计数归零。** `remove` 在引用计数归零时，克隆一份 `BufferDesc`（因为要边遍历边 erase），释放锁后回调注销：

[`mooncake-transfer-engine/tent/src/runtime/segment_tracker.cpp:125-147`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/segment_tracker.cpp#L125-L147) —— 引用计数归零才真正 erase 并回调：

```cpp
Status SegmentTracker::remove(uint64_t base, size_t length,
                              std::function<Status(BufferDesc&)> callback) {
    auto& detail = std::get<MemorySegmentDesc>(local_desc_->detail);
    mutex_.lock();
    for (auto it = detail.buffers.begin(); it != detail.buffers.end(); ++it) {
        if (it->addr == base && (!length || it->length == length)) {
            it->ref_count--;
            Status status = Status::OK();
            if (it->ref_count == 0) {
                BufferDesc clone = *it;
                detail.buffers.erase(it);
                mutex_.unlock();
                status = callback(clone);    // 注销出 transport
            } else {
                mutex_.unlock();
            }
            return status;
        }
    }
    mutex_.unlock();
    return Status::OK();
}
```

**上游：`registerLocalMemory` 如何用 `SegmentTracker`。** 运行时先构造 `BufferDesc`（做 NUMA 探测、warm-up 钉页），再调用 `addInBatch`，回调里把它注册进所有支持的 transport，最后 `synchronizeLocal`：

[`mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp:669-681`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L669-L681) —— 回调里向各 transport 注册内存，之后同步到注册表：

```cpp
auto status = local_segment_tracker_->addInBatch(
    desc_list, [&](std::vector<BufferDesc>& descs) -> Status {
        for (auto type : transports) {
            auto s = transport_list_[type]->addMemoryBuffer(descs, options);
            if (!s.ok()) LOG(WARNING) << s.ToString();
        }
        return Status::OK();
    });
if (!status.ok()) return status;
// Synchronize local segment to metadata server so remote peers can see the
// new buffers
return metadata_->segmentManager().synchronizeLocal();
```

#### 4.4.4 代码实践

**实践目标**：验证「同一段内存注册两次，引用计数为 2，注销一次不会真正移除」。

**操作步骤**：

1. 阅读 `transfer_engine_impl.cpp` 的 `registerLocalMemory`（621 行起）与 `unregisterLocalMemory`（685 行起），确认二者都通过 `local_segment_tracker_` 操作。
2. 对照 `segment_tracker.cpp` 的 `addInBatch`（86 行）与 `remove`（125 行）的引用计数逻辑。
3. 推演：对地址 `A`、长度 `L` 调两次 `registerLocalMemory`——第一次加入 buffers、`ref_count=1`；第二次命中相同 `(addr,length)`、`ref_count=2`。调一次 `unregisterLocalMemory(A, L)` → `ref_count=1`，buffers 里仍保留。再调一次 → `ref_count=0`，erase 并回调注销。

**需要观察的现象**：引用计数归零前，`SegmentDesc::buffers` 里始终保留该条目；只有归零才会 erase 并触发 transport 注销。

**预期结果**：你能解释「为什么 `deconstruct()` 里要 `local_segment_tracker_->forEach(...)` 对每个 buffer 调 `removeMemoryBuffer`」——保证析构时所有 transport 都释放了对内存的引用（否则 CUDA 可能卡死，见 `transfer_engine_impl.cpp:683-684` 的 WARNING 注释）。

**待本地验证**：可在测试里对同一段内存连续 `registerLocalMemory` 两次，再观察 `getSegmentInfo` 返回的 buffers 数量与重复注册前是否一致。

#### 4.4.5 小练习与答案

**练习 1**：`remove` 在 `ref_count` 归零时，为什么要先 `BufferDesc clone = *it;` 再 `erase`，然后释放锁后才执行 `callback`？

> **参考答案**：两个原因。其一，`erase(it)` 会让迭代器失效，必须在 erase 前把需要的信息（`BufferDesc`）拷出来。其二，`callback` 会调用 `transport->removeMemoryBuffer`，可能耗时较长或重入；先 `mutex_.unlock()` 再回调，避免长时间持锁阻塞其它注册/注销操作。

**练习 2**：缓冲区列表排序时，地址相同为什么让「长度大的排前面」？

> **参考答案**：查找一个地址落在哪个 buffer 时，通常希望优先命中「覆盖范围更大」的区间。地址相同、长度更大的排在前面，线性查找或区间查找时能先遇到更宽的区间，减少误匹配和小窗口拼接，注释称之为 `prefer large interval`。

---

### 4.5 ProxyManager：暂存中转与事件循环

#### 4.5.1 概念说明

`ProxyManager`（源码里也叫 staging proxy）解决一个具体痛点：**当两端的传输后端不支持 GPU Direct 时，如何用主机内存做中转**。典型场景是「两端都是 GPU 内存，但 RDMA 后端的 `gpu_to_gpu` 能力不可用」——这时不能直接 GPU→GPU，需要：

- 先把数据从源 GPU 搬到**本地主机内存**（local stage）；
- 再跨网络搬到**远端主机内存**（cross stage，走 TCP）；
- 最后从远端主机内存搬到目标 GPU（remote stage，由远端代为执行）。

`ProxyManager` 内部有一组「暂存缓冲」（stage buffer），按 location（NUMA/GPU）分组，用位图分配。它用 8 个分片工作线程，每个线程跑一个**事件循环状态机**，把一次大传输切成多个 chunk 流水线推进。

#### 4.5.2 核心流程

提交与推进分两层：

```
提交层（调用方线程）:
    submitTransfer 发现 task.type == TCP 且需要暂存
        → findStagingPolicy() 算出 [server_addr, local_loc, remote_loc]
        → staging_proxy_->submit(task, params)   ← 投入某个分片队列
    之后 getTransferStatus 对该 task 走 staging 分支（getStatus）

工作线程层（ProxyManager::runner）:
    从分片队列取 StagingTask
        → transferEventLoop(task):
            把请求按 chunk_size 切成多个 Chunk
            每个 Chunk 在状态机里流转:
              PRE → (本地暂存) → CROSS → (远端暂存) → POST → FINISH
            INFLIGHT / INFLIGHT_REMOTE 表示该阶段在途
```

一次 WRITE 暂存传输的完整阶段（本地 + 远端都需要 stage 时）：

```
PRE:   源 → 本地 stage buffer      (submitLocalStage)
CROSS: 本地 stage → 远端 stage     (submitCrossStage，跨网络)
POST:  远端 stage → 目标            (submitRemoteStage，RPC 委托远端)
```

READ 方向相反：先从远端拉到本地 stage，再拷回源。

#### 4.5.3 源码精读

**提交入口与分片。** `submit` 把任务塞进某个分片队列（thread_local 选一个分片，负载均衡），并通知对应工作线程：

[`mooncake-transfer-engine/tent/src/runtime/proxy_manager.cpp:125-139`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/proxy_manager.cpp#L125-L139) —— 投入分片队列并唤醒一个 worker：

```cpp
Status ProxyManager::submit(TaskInfo* task,
                            const std::vector<std::string>& params) {
    StagingTask staging_task;
    staging_task.native = task;
    staging_task.params = params;
    task->staging_status = PENDING;
    static std::atomic<size_t> next_queue_index(0);
    thread_local size_t id = next_queue_index.fetch_add(1) % kShards;
    {
        std::lock_guard<std::mutex> lk(shards_[id].mu);
        shards_[id].queue.push(staging_task);
    }
    shards_[id].cv.notify_one();
    return Status::OK();
}
```

分片数固定为 8：

[`mooncake-transfer-engine/tent/include/tent/runtime/proxy_manager.h:109`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/include/tent/runtime/proxy_manager.h#L109) —— 8 个工作分片：

```cpp
const static size_t kShards = 8;
```

**暂存缓冲分配：位图。** 每个 location 有一组 chunk（默认 chunk_size=4MB、chunk_count=64），用 `std::atomic_flag` 位图记录哪些 chunk 被占用。`pinStageBuffer` 用 test_and_set 抢一个空闲 chunk：

[`mooncake-transfer-engine/tent/src/runtime/proxy_manager.cpp:557-574`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/proxy_manager.cpp#L557-L574) —— 抢到第一个 `test_and_set` 成功的 chunk：

```cpp
Status ProxyManager::pinStageBuffer(const std::string& location,
                                    uint64_t& addr) {
    auto it = stage_buffers_.find(location);
    if (it == stage_buffers_.end()) {
        CHECK_STATUS(allocateStageBuffers(location));
        it = stage_buffers_.find(location);
    }
    auto& buf = it->second;
    for (size_t i = 0; i < chunk_count_; ++i) {
        if (!buf.bitmap[i].test_and_set(std::memory_order_acquire)) {
            addr = reinterpret_cast<uint64_t>(static_cast<char*>(buf.chunks) +
                                              i * chunk_size_);
            return Status::OK();
        }
    }
    return Status::TooManyRequests("No available stage buffer in " + location);
}
```

**事件循环状态机：`transferEventLoop`。** 这是暂存传输的核心。先把请求切成 chunk（每个 chunk 默认 4MB），再让每个 chunk 在状态机里流转。chunk 数为：

\[
N_{\text{chunk}} = \left\lceil \frac{\text{length}}{\text{chunk\_size}} \right\rceil
\]

为支持流水线，它会预分配 `kStageBuffers = min(chunk_count_, 16)` 个 stage buffer 槽位循环复用：

[`mooncake-transfer-engine/tent/src/runtime/proxy_manager.cpp:289-303`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/proxy_manager.cpp#L289-L303) —— 按固定大小切块，stage buffer 按 `id % kStageBuffers` 循环复用：

```cpp
for (size_t offset = 0; offset < request.length; offset += chunk_size_) {
    size_t id = chunks.size();
    Chunk chunk{offset,
                std::min(chunk_size_, request.length - offset),
                local_staging ? local_stage_buffer[id % kStageBuffers]
                              : (uint64_t)request.source + offset,
                remote_staging ? remote_stage_buffer[id % kStageBuffers]
                               : request.target_offset + offset,
                StageState::PRE,
                StageState::PRE,
                0};
    chunks.push_back(chunk);
}
for (size_t i = 0; i < chunks.size(); ++i) event_queue.push(i);
```

状态机的 `INFLIGHT` 分支展示了一个 chunk 如何在阶段间推进、并在完成后释放占用的 stage buffer：

[`mooncake-transfer-engine/tent/src/runtime/proxy_manager.cpp:386-418`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/proxy_manager.cpp#L386-L418) —— 推进该 chunk 的子 batch，完成后按来源阶段转到下一阶段并释放锁：

```cpp
case StageState::INFLIGHT: {
    TransferStatus xfer_status;
    CHECK_STATUS(impl_->progressBatch(chunk.batch, xfer_status));
    if (xfer_status.s == PENDING) {
        event_queue.push(id);
        break;
    }
    if (xfer_status.s == COMPLETED) {
        if (chunk.prev_state == StageState::PRE)
            chunk.state = StageState::CROSS;
        else if (chunk.prev_state == StageState::CROSS) {
            chunk.state = StageState::POST;
            if (request.opcode == Request::WRITE && local_staging)
                local_locked.erase(chunk.local_buf);   // 释放本地 stage
            if (request.opcode == Request::READ && remote_staging)
                remote_locked.erase(chunk.remote_buf);
        } else if (chunk.prev_state == StageState::POST) {
            chunk.state = StageState::FINISH;
            ...
        }
        impl_->freeBatch(chunk.batch);
        chunk.batch = 0;
    } else if (xfer_status.s != PENDING) {
        chunk.state = StageState::FAILED;
        ...
    }
    event_queue.push(id);
    break;
}
```

`local_locked` / `remote_locked` 这两个集合是关键的**流控**机制：同一个 stage buffer 槽位同时只能被一个 chunk 在某个方向占用，从而让多个 chunk 在有限的 buffer 槽位上流水线推进而不互相覆盖。

**状态查询：`getStatus`。** 暂存任务的状态由原子量 `staging_status` 承载，工作线程完成后用 `__atomic_store` 写回：

[`mooncake-transfer-engine/tent/src/runtime/proxy_manager.cpp:141-148`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/proxy_manager.cpp#L141-L148) —— 直接读 task 的原子状态：

```cpp
Status ProxyManager::getStatus(TaskInfo* task, TransferStatus& task_status) {
    if (!task || !task->staging) return Status::InvalidArgument("Invalid task");
    task_status.s = task->staging_status;
    if (task_status.s == COMPLETED) {
        task_status.transferred_bytes = task->request.length;
    }
    return Status::OK();
}
```

**上游：何时走暂存？** `submitTransfer` 在选到 TCP 且 `findStagingPolicy` 给出非空策略时，把 task 标记为 staging 并交给 `staging_proxy_`：

[`mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp:1275-1283`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L1275-L1283) —— TCP 任务在需要中转时改走暂存代理：

```cpp
if (task.type == TCP) {
    std::vector<std::string> staging_params;
    findStagingPolicy(merged_request, staging_params);
    if (!staging_params.empty() && staging_proxy_) {
        task.staging = true;
        staging_proxy_->submit(&task, staging_params);
        continue;
    }
}
```

相应地，轮询状态时对 staging 任务走 `staging_proxy_->getStatus` 而非 transport：

[`mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp:1429-1431`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L1429-L1431) —— 暂存任务的状态走代理：

```cpp
if (task.staging) {
    return staging_proxy_->getStatus(&task, task_status);
}
```

#### 4.5.4 代码实践

**实践目标**：理解一次 GPU→GPU（无 GPU Direct）暂存 WRITE 的三阶段数据流。

**操作步骤**：

1. 读 `findStagingPolicy`（`transfer_engine_impl.cpp:1143`），看它在「两端都是 CUDA 且 `gpu_to_gpu` 不可用」时给出的策略：`[server_addr, 本地近端内存 location, 远端近端内存 location]`。
2. 跟进 `transferEventLoop` 的 `PRE → CROSS → POST` 三段（`proxy_manager.cpp:311-384`），分别对应 `submitLocalStage`、`submitCrossStage`、`submitRemoteStage`。
3. 画出数据流（见下方「预期结果」）。

**需要观察的现象**：`submitCrossStage` 构造的子请求 `target_id` 仍是远端段、`target_offset` 是远端 stage buffer 地址；`submitRemoteStage` 则通过 `ControlClient::delegate` RPC 把「远端 stage → 目标」这一步**委托给远端节点执行**。

**预期结果**（数据流图）：

```
本地 GPU ──PRE──▶ 本地主机stage ──CROSS(TCP)──▶ 远端主机stage ──POST(delegate RPC)──▶ 远端 GPU
         submitLocalStage            submitCrossStage               submitRemoteStage
```

**待本地验证**：ProxyManager 头文件标注 `Beta version -- use with own risk`，且暂存仅在 TCP + 特定内存类型组合下触发。无 RDMA/GPU 环境下难以真实触发，建议以源码阅读为主，标注「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：`local_locked` / `remote_locked` 这两个 `unordered_set` 在事件循环里起什么作用？没有它们会怎样？

> **参考答案**：stage buffer 槽位数量有限（`kStageBuffers`，最多 16），但一次大传输的 chunk 数可能远多于槽位，所以多个 chunk 会**循环复用**同一个槽位。`local_locked` / `remote_locked` 记录「当前哪些 stage buffer 正被某个 chunk 在某个方向占用」，防止两个 chunk 同时往同一个槽位写、互相覆盖。没有它们，流水线复用就会产生数据竞争。

**练习 2**：为什么 `submitRemoteStage` 用 `ControlClient::delegate`（RPC）而不是直接由本地 transport 搬？

> **参考答案**：POST 阶段是「远端主机 stage buffer → 远端目标 GPU」，这段数据**完全在远端节点内部**，本地节点够不着。所以本地只能通过 RPC 请远端代为执行这次本地拷贝（`delegate` 即「委托」）。`onDelegate` 在远端会调 `impl_->transferSync({user_request})`（`control_plane.cpp:308-313`），由远端的运行时完成这最后一段。

---

### 4.6 ProgressWorker：异步进度推进

#### 4.6.1 概念说明

默认情况下，TENT 的传输进度推进是**同步的**：调用方自己 `getTransferStatus` / `progressBatch` 时，顺带做一次 poll，并（若开启 `enable_auto_failover_on_poll`）触发 failover。这在「调用方会主动轮询」时没问题。

但有些集成方（比如 `mooncake-pg`）只「提交 + 观察状态」，从不主动 poll。这时如果某条传输失败、需要 failover，没人推进就会卡住。`ProgressWorker` 就是为这种场景设计的：它是一个**可选的后台线程**（配置 `enable_progress_worker=true` 时启用），**事件驱动**地推进进度——谁通知「某个 batch 可能就绪了」，它就对该 batch 做一次 `progressBatch`。

#### 4.6.2 核心流程

```
notifyBatchMaybeReady(batch_id):     ← transport 或测试钩子调用
    若 batch 已在队列 → 去重，直接返回
    否则入队 order_，notify_one

ProgressWorker::runner():
    循环:
        等 cv，直到 order_ 非空或要退出
        取一个 batch_id 出队
        impl_->progressBatch(batch_id, s)   ← 推进一步
            （PENDING = 下次再来；终态 = 不再碰，由用户线程 freeBatch 回收）
```

关键设计：**一次 notify 只推进一步**，worker 不会自己循环到完成。要继续推进，需要下一次 notify。这让 worker 的行为可预测、不会抢占式地空转。

#### 4.6.3 源码精读

**通知与去重。** `queued_` 是个 set，保证同一个 batch 不会在队列里重复排多次；`order_` 是 deque，保持入队顺序：

[`mooncake-transfer-engine/tent/src/runtime/progress_worker.cpp:45-54`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/progress_worker.cpp#L45-L54) —— 去重后入队并唤醒：

```cpp
void ProgressWorker::notifyBatchMaybeReady(BatchID batch_id) {
    if (!batch_id) return;
    if (!running_.load(std::memory_order_acquire)) return;
    {
        std::lock_guard<std::mutex> lk(mu_);
        if (!queued_.insert(batch_id).second) return;   // 已在队列，去重
        order_.push_back(batch_id);
    }
    cv_.notify_one();
}
```

**runner：取一个、推进一步。** 注释清楚地点明了「PENDING 就等下次 notify、终态就交给用户线程回收」的契约：

[`mooncake-transfer-engine/tent/src/runtime/progress_worker.cpp:56-78`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/progress_worker.cpp#L56-L78) —— 每个 notify 对应一次 progressBatch：

```cpp
void ProgressWorker::runner() {
    while (true) {
        BatchID batch_id = 0;
        {
            std::unique_lock<std::mutex> lk(mu_);
            cv_.wait(lk, [&] {
                return !running_.load(std::memory_order_acquire) ||
                       !order_.empty();
            });
            if (!running_.load(std::memory_order_acquire)) return;
            batch_id = order_.front();
            order_.pop_front();
            queued_.erase(batch_id);
        }
        // progressBatch acquires the engine's progress_mutex_ and silently
        // returns InvalidArgument if the batch was freed before we got here.
        TransferStatus s;
        (void)impl_->progressBatch(batch_id, s);
    }
}
```

**与 freeBatch 的竞态安全。** worker 可能在 batch 已被用户线程 free 之后才拿到通知。为此 `progressBatch` → `getBatchStatus` 会先检查 `alive_batches_`，若 batch 已不在则静默返回 `InvalidArgument`：

[`mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp:1540-1547`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L1540-L1547) —— 先校验 batch 仍存活，否则静默失败：

```cpp
Status TransferEngineImpl::getBatchStatus(BatchID batch_id,
                                          TransferStatus& overall_status,
                                          bool allow_failover) {
    if (!batch_id) return Status::InvalidArgument("Invalid batch ID" LOC_MARK);
    std::lock_guard<std::recursive_mutex> lk(progress_mutex_);
    if (!alive_batches_.count(batch_id))
        return Status::InvalidArgument("Batch is not alive" LOC_MARK);
    ...
```

而 `notifyBatchMaybeReady` 在运行时层面只是转发：

[`mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp:1618-1620`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L1618-L1620) —— 运行时把通知转给 worker（若存在）：

```cpp
void TransferEngineImpl::notifyBatchMaybeReady(BatchID batch_id) {
    if (progress_worker_) progress_worker_->notifyBatchMaybeReady(batch_id);
}
```

`progress_mutex_` 是 `std::recursive_mutex`（可重入），因为 `freeBatch → lazyFreeBatch → getTransferStatus` 会在同一线程上重入这把锁，头文件注释专门记录了这一点：

[`mooncake-transfer-engine/tent/include/tent/runtime/transfer_engine_impl.h:267-272`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/include/tent/runtime/transfer_engine_impl.h#L267-L272) —— 递归锁保护 alive 集合并串行化 poll：

```cpp
// Guards alive_batches_ and serializes pollTaskStatus /
// updateTaskStatusAfterPoll / lazyFreeBatch against the optional
// ProgressWorker thread. Recursive because freeBatch -> lazyFreeBatch ->
// getTransferStatus can re-enter on the same thread. See issue #2116.
std::recursive_mutex progress_mutex_;
std::unordered_set<BatchID> alive_batches_;
std::unique_ptr<ProgressWorker> progress_worker_;
```

#### 4.6.4 代码实践

**实践目标**：用测试断言反推 `ProgressWorker` 的「一次 notify 推进一步」契约。

**操作步骤**：

1. 打开 [`mooncake-transfer-engine/tent/tests/progress_worker_test.cpp`](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/tent/tests/progress_worker_test.cpp)。
2. 读测试 `SingleNotifyAdvancesOneStep`（第 325 行起）：它用一个「poll 前 3 次返回 PENDING、第 4 次返回 COMPLETED」的 FakeTransport，然后只 `notifyBatchMaybeReady` 一次，断言 `status_calls` 增到 1 后**不再继续增长**。
3. 读测试 `ProgressesWithoutPollAutoFailover`（第 258 行起）：关闭 `enable_auto_failover_on_poll`、开启 `enable_progress_worker`，在调用方从不 poll 的情况下，靠反复 `notifyBatchMaybeReady` 让传输走完 failover（RDMA 失败 → TCP 成功）。

**需要观察的现象**：

- `SingleNotifyAdvancesOneStep` 里，单次 notify 后 `fake_rdma->status_calls` 恰好为 1，睡眠 50ms 后仍是 1——证明 worker 不会自循环。
- `ProgressesWithoutPollAutoFailover` 里，`fake_rdma->submit_calls == 1`（首次提交后失败）、`fake_tcp->submit_calls >= 1`（worker 驱动了 failover 重交到 TCP）。

**预期结果**：你能用一句话总结 `ProgressWorker` 的契约——「**事件驱动、单步推进、去重、与 freeBatch 竞态安全**」。

**待本地验证**：该测试使用 `FakeTransport` + `FaultProxyTransport`，不依赖真实 RDMA，可在普通 CI/本机编译后用 `ctest -R ProgressWorker` 运行。

#### 4.6.5 小练习与答案

**练习 1**：`notifyBatchMaybeReady` 为什么要用 `queued_`（set）做去重？如果不去重会怎样？

> **参考答案**：transport 完成事件可能高频触发，若每次都无条件入队，同一个 batch 会在 `order_` 里堆积大量重复项，worker 会重复对同一个 batch 做无意义的 `progressBatch`，浪费 CPU 并放大锁竞争。用 set 去重保证「同一个 batch 在队列里至多一份」，等 worker 取出处理完，下一次 notify 才会重新入队。

**练习 2**：为什么 worker 对 `progressBatch` 的返回值用 `(void)` 忽略，连 PENDING 都不处理？

> **参考答案**：worker 的职责是「被唤醒就推进一步」，而不是「负责把传输做到完成」。PENDING 意味着「暂时没完成，下次再说」——而「下次」由谁触发？由下一次 `notifyBatchMaybeReady`。这种设计让 worker 永远不会空转抢占 CPU，进度推进的节奏完全由真实事件（transport 就绪）驱动。终态则交给用户线程的 `freeBatch` 回收，职责清晰。

---

## 5. 综合实践

**任务**：对照源码，画出一次 TENT 传输（远端段、需暂存、开启 progress worker）的**运行时组件交互图**，并用源码行号标注每一步发生在哪里。

这是本讲代码实践任务的核心：把 `TransferEngineImpl` → `SegmentManager`/`Registry` → `TransportSelector` → `ProxyManager` 暂存 → `ProgressWorker` 追踪完成 这条链画清楚。

**建议产出形式**：一张 ASCII 时序图 + 一张「步骤 → 源码位置」对照表。

**操作步骤**：

1. **准备阶段（一次性）**：定位每个组件的装配点（见 4.1）。确认 `SegmentManager` 持有一个 `SegmentRegistry`（`control_plane.cpp:160-166`），`TransferEngineImpl` 持有 `staging_proxy_` 和可选的 `progress_worker_`（`transfer_engine_impl.cpp:339-344`）。

2. **注册内存**：`registerLocalMemory` → `SegmentTracker::addInBatch`（`transfer_engine_impl.cpp:669`）→ `synchronizeLocal`（680 行）→ `registry_->putSegmentDesc`（`segment_manager.cpp:198`）+ 推送订阅者。

3. **打开远端段**：`openSegment` → `SegmentManager::openRemote`（`transfer_engine_impl.cpp:478` → `segment_manager.cpp:37`），拿到本地 `SegmentID`。

4. **提交传输**：`submitTransfer`（`transfer_engine_impl.cpp:1218`）内部：
   - `resolveTransport` → `getTransportType`（825 行）→ 对远端段调 `getRemoteCached`（`segment_manager.cpp:63`，走线程级缓存，未命中才问 `registry_`）；
   - `TransportSelector::select`（914 行）选出 transport 类型与 `device_mask`；
   - 若选到 TCP 且 `findStagingPolicy` 非空（1275 行）→ `staging_proxy_->submit`（`proxy_manager.cpp:125`），task 标记 `staging=true`。

5. **暂存执行**：`ProxyManager` 工作线程跑 `transferEventLoop`（`proxy_manager.cpp:235`），chunk 在 `PRE→CROSS→POST` 状态机里流转，每段的子 batch 用 `impl_->progressBatch` 推进。

6. **进度追踪**：
   - 若未开 progress worker：调用方 `getTransferStatus` → `pollTaskStatus`，对 staging task 走 `staging_proxy_->getStatus`（`transfer_engine_impl.cpp:1429`）；
   - 若开了 progress worker：transport 完成时 `notifyBatchMaybeReady`（1618 行）→ worker 线程 `progressBatch`（`progress_worker.cpp:76`），驱动 failover。

7. **画出时序图**（示例骨架，请补全源码行号）：

```
调用方              TransferEngineImpl        SegmentManager/Registry     TransportSelector     ProxyManager       ProgressWorker
  │  registerLocalMemory │                           │                         │                    │                     │
  │─────────────────────▶│ SegmentTracker::addInBatch│                         │                    │                     │
  │                       │ synchronizeLocal ────────▶│ putSegmentDesc ──▶ Registry                  │                     │
  │  openSegment          │                           │                         │                    │                     │
  │─────────────────────▶│ openRemote (分配 SegmentID)│                         │                    │                     │
  │  submitTransfer       │                           │                         │                    │                     │
  │─────────────────────▶│ resolveTransport ────────▶│ getRemoteCached(缓存)   │                    │                     │
  │                       │ getTransportType ─────────────────────────────────▶│ select()           │                     │
  │                       │ (TCP+staging) ───────────────────────────────────────────────────────▶│ submit(task)        │
  │                       │                           │                         │            runner/transferEventLoop       │
  │                       │                           │                         │            PRE→CROSS→POST (progressBatch) │
  │  (transport 就绪)     │                           │                         │                    │                     │
  │                       │ notifyBatchMaybeReady ───────────────────────────────────────────────────────────────────────▶│ progressBatch
  │  getTransferStatus    │                           │                         │                    │                     │
  │─────────────────────▶│ pollTaskStatus (staging→getStatus)                  │                     │                     │
```

8. **填写对照表**（示例骨架，请补全行号）：

| 步骤 | 入口函数 | 源码位置 |
|------|----------|----------|
| 注册内存并同步 | `registerLocalMemory` → `synchronizeLocal` | `transfer_engine_impl.cpp:680` / `segment_manager.cpp:193` |
| 打开远端段 | `openRemote` | `segment_manager.cpp:37` |
| 拉取远端描述符（缓存） | `getRemoteCached` | `segment_manager.cpp:63` |
| 选传输类型 | `getTransportType` → `TransportSelector::select` | `transfer_engine_impl.cpp:825,914` |
| 投递暂存任务 | `ProxyManager::submit` | `proxy_manager.cpp:125` |
| 暂存状态机推进 | `transferEventLoop` | `proxy_manager.cpp:235` |
| 异步进度推进 | `ProgressWorker::runner` | `progress_worker.cpp:56` |

**需要观察的现象**：画完后你会发现，`SegmentManager`/`Registry` 主要在「提交前」被访问（拿远端描述符），`ProxyManager` 在「提交时」接管暂存任务，`ProgressWorker` 在「传输中」异步推进——三者分别对应传输生命周期的**准备、提交、执行**三个阶段，职责几乎不重叠。

**预期结果**：你的交互图里至少包含 6 个带行号的源码引用，并能回答「为什么远端段描述符的拉取不会成为每次 poll 的瓶颈」（线程级缓存）、「为什么暂存任务的状态查询走 `staging_proxy_->getStatus` 而非 transport」（task.staging 分支）。

## 6. 本讲小结

- TENT 运行时把段管理拆成五个组件：`SegmentManager`（段句柄+缓存）、`SegmentRegistry`（存储后端）、`SegmentTracker`（本地缓冲登记簿）、`ProxyManager`（暂存中转）、`ProgressWorker`（异步进度），它们在 `TransferEngineImpl::construct()` 里一次性装配。
- `SegmentRegistry` 是可插拔的存储抽象：`CentralSegmentRegistry` 走 etcd/redis（key 前缀 `mooncake/tent/`），`PeerSegmentRegistry` 走 peer 间 RPC，由 `metadata_type` 决定（`control_plane.cpp:160-166`）。
- `SegmentManager` 用**线程级缓存**加速远端段描述符的访问，靠「版本号 + TTL(1h) + 订阅推送」三重机制保证不过期；`withCachedSegment` 模板封装了「失效→重试」逻辑，且正确性不依赖尽力而为的推送。
- `SegmentTracker` 用引用计数管理本地段缓冲区，列表按地址排序（地址相同时大区间优先），`registerLocalMemory`/`unregisterLocalMemory` 都经它中转，变更后 `synchronizeLocal` 同步到注册表。
- `ProxyManager` 在 GPU Direct 不可用时用主机内存做三段中转（PRE/CROSS/POST），8 个分片线程跑事件循环，靠 `local_locked`/`remote_locked` 在有限 stage buffer 槽位上做流水线流控。
- `ProgressWorker` 是可选的事件驱动后台线程（`enable_progress_worker`），一次 notify 推进一步、去重、与 `freeBatch` 竞态安全（靠 `alive_batches_` + `progress_mutex_` 递归锁），把 failover 从调用方轮询循环里解耦。

## 7. 下一步学习建议

学完本讲「段管理与运行时组件」之后，建议按下面的顺序继续：

1. **传输选择细节**：本讲只点到 `TransportSelector::select`，下一讲 [u4-l3（TENT 传输选择器）](u4-l3-tent-transport-selector.md) 会精读策略匹配（`matchesPolicy`、`isTransportAvailable`）与 `device_mask` 如何一路透传到 RDMA 后端。
2. **准入与 QoS**：[u4-l4（TENT 准入与 QoS）](u4-l4-tent-admission-qos.md) 讲 `AdmissionQueue` 与优先级如何影响调度，与本讲的 `ProgressWorker` 推进节奏相关。
3. **控制面与元数据存储**：[u4-l5（TENT 控制面与 MetaStore）](u4-l5-tent-control-plane-metastore.md) 会展开本讲提到的 `ControlService` RPC（`onGetSegmentDesc`、`onDelegate`、`onPinStageBuffer`、`onSegmentUpdated`）与 `MetaStore` 插件体系。
4. **动手读测试**：`mooncake-transfer-engine/tent/tests/progress_worker_test.cpp`（本讲 4.6 已用）、`endpoint_lifecycle_test.cpp`、`request_merge_test.cpp`，用测试断言反推运行时行为。
5. **延伸阅读源码**：`segment.cpp` 里的 `SegmentDesc::findBuffer`（本讲多次提到它被 `withCachedSegment` 内的操作调用），以及 `slab.cpp`（`Batch` / `SubBatch` 的内存分配，解释了 `allocateBatch`/`freeBatch` 背后的对象池）。
