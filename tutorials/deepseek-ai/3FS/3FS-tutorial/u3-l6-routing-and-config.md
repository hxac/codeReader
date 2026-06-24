# 路由信息分发与配置管理

## 1. 本讲目标

在 [u3-l1](u3-l1-mgmtd-overview.md) 中我们已经知道：mgmtd 是整个 3FS 集群的「发现中枢」，它在内存里维护一份 `RoutingInfo`（节点、链表、链、target 的全局视图），并把它分发给 client / storage / meta。本讲要回答的核心问题是：

> **这份视图是怎么「变」的，又是怎么「传」到每个使用者的？集群的运行时配置又是怎么被托管和分发的？**

学完本讲，你应当能够：

1. 说清 `GetRoutingInfo` 的版本号拉取机制：客户端带着「我手上的版本」去问，mgmtd 只在「有更新」时才返回完整视图，否则返回空——这是一种类似 HTTP `304 Not Modified` 的省带宽设计。
2. 说清 `routingInfoVersion` 与 `ConfigVersion` 两条独立的版本号是如何被「推进」的，以及为什么 mgmtd 要用一个「脏标记 + 周期 bump」的机制把多次变更合并成一次下发。
3. 掌握配置托管流程：`set-config` 如何在 FoundationDB 里写入新版本、配置如何随心跳回到各个节点。
4. 理解节点的 tag（尤其是 `Disable`、`TrafficZone`）与 `enable/disable` 操作如何改变路由视图、并影响客户端选 target 的决策。

---

## 2. 前置知识

本讲默认你已经掌握以下内容（若没有，建议先读对应讲义）：

- **[u2-l5 配置系统与热更新](u2-l5-config-system.md)**：`ConfigBase`、`CONFIG_HOT_UPDATED_ITEM`、`atomicallyUpdate` 门禁、`ConfigVersion`、心跳拉取等概念。本讲讲的「配置管理」是 mgmtd **服务端**如何托管这些配置，与 u2-l5 的「客户端如何应用配置」是一对配对关系。
- **[u2-l6 FoundationDB 客户端与事务封装](u2-l6-fdb-and-transactions.md)**：`IReadWriteTransaction`、读写冲突范围、`withReadWriteTxn` 重试。本讲里 mgmtd 写路由版本号、写配置，都是 FDB 事务。
- **[u3-l1 mgmtd 服务总览与 RoutingInfo 数据模型](u3-l1-mgmtd-overview.md)**：`MgmtdState` / `MgmtdData`、工作版 `mgmtd::RoutingInfo` 与下发版 `flat::RoutingInfo` 的区别、`CoroSynchronized<MgmtdData>` 的双层锁、`doAsPrimary`。
- **[u3-l4 ChainTable / Chain / Target 数据模型](u3-l4-chain-target-model.md)**：三级版本号 `routingInfoVersion` / `chainTableVersion` / `chainVersion`。

两个贯穿全讲的关键词先点出来：

- **RoutingInfo（路由信息）**：集群拓扑的完整快照——哪些节点在线、每条链上有哪几个 target、各 target 的公开状态。客户端靠它把「chunk id」翻译成「去哪个 storage 节点的哪个 target 读写」。
- **Config（运行时配置）**：每个服务类型（`NodeType`：MGMTD / META / STORAGE / CLIENT / FUSE 等）的一份 TOML 配置文本，由 mgmtd 集中托管，可热更新。

两者都是「mgmtd 单点写入 FoundationDB + 多点拉取」的模式，所以本讲会把它们放在一起对照讲。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [src/mgmtd/ops/GetRoutingInfoOperation.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/GetRoutingInfoOperation.cc) | `GetRoutingInfo` RPC 的服务端处理：版本校验 + 投影下发 |
| [src/mgmtd/service/MgmtdData.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdData.cc) | `checkRoutingInfoVersion` / `getRoutingInfo`（含缓存）与配置查询 |
| [src/mgmtd/background/MgmtdRoutingInfoVersionUpdater.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdRoutingInfoVersionUpdater.cc) | 周期性「推进 routingInfoVersion」的后台任务 |
| [src/mgmtd/service/helpers.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/helpers.h) / [helpers.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/helpers.cc) | `nextVersion` / `updateStoredRoutingInfo` / `updateMemoryRoutingInfo` 等公共逻辑 |
| [src/mgmtd/ops/SetConfigOperation.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/SetConfigOperation.cc) | `SetConfig` RPC：写入新版本配置 |
| [src/mgmtd/ops/GetConfigOperation.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/GetConfigOperation.cc) | `GetConfig` RPC：按版本拉取配置 |
| [src/fbs/mgmtd/ConfigInfo.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/ConfigInfo.h) | 配置数据结构：版本 + TOML 内容 + 描述 |
| [src/mgmtd/store/MgmtdStore.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/store/MgmtdStore.cc) | FDB 存取：路由版本号 key、配置 key（巧妙的「反转版本」编码） |
| [src/mgmtd/ops/EnableDisableNodeOperation.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/EnableDisableNodeOperation.cc) | `enable/disable node`：改节点状态 + `Disable` tag |
| [src/mgmtd/ops/SetNodeTagsOperation.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/SetNodeTagsOperation.cc) | `set-node-tags`：增删改节点标签 |
| [src/common/app/AppInfo.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/AppInfo.h) | 两个特殊 tag 键：`Disable`、`TrafficZone` |
| [src/fbs/mgmtd/NodeInfo.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/NodeInfo.h) | `selectNodeByTrafficZone`：客户端按 traffic zone 过滤节点 |
| [src/client/mgmtd/MgmtdClient.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/MgmtdClient.cc) | **客户端侧**：周期 `AutoRefresh` 拉取、`updateRoutingInfo` 通知监听者、心跳回包带配置 |
| [src/client/storage/StorageClientImpl.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClientImpl.cc) | 客户端如何用 RoutingInfo 选 target（受 tag/状态影响） |
| [src/mgmtd/background/MgmtdBackgroundRunner.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdBackgroundRunner.cc) | 把「推进版本」等周期任务挂到 `BackgroundRunner` |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1 路由信息分发**：`GetRoutingInfo` 与客户端按需拉取。
- **4.2 节点 tag 与 enable/disable**：管理操作如何改变路由视图、影响客户端选 target。
- **4.3 配置托管与管理**：`SetConfig` / `GetConfig` 与 `ConfigVersion`。
- **4.4 版本号推进**：从「某处发生变更」到「客户端切到新视图」的完整时序。

### 4.1 路由信息分发：GetRoutingInfo 与客户端按需拉取

#### 4.1.1 概念说明

集群里每一个需要发数据的进程（FUSE 客户端、storage、meta）内部都持有一个 `MgmtdClient`（见 [u1-l4](u1-l4-end-to-end-flow.md)）。它们各自缓存了一份 `flat::RoutingInfo`，用来：

- 客户端：把文件布局里的 chunk → 链 → target → storage 节点地址。
- storage：判断自己是不是某条链的 head/tail，决定写请求是就地处理还是要沿链转发。
- meta：选链分配时知道当前有哪些可用链。

如果每个进程都「高频全量拉取」整份视图，mgmtd 的带宽和 CPU 会被序列化/反序列化吃光。3FS 的做法是**带版本号的增量通知**：

> 客户端请求时带上「我当前持有的 `routingInfoVersion`」；mgmtd 比对：若客户端版本 == 服务端版本，说明没有更新，**回包里 `info` 字段留空（`std::nullopt`）**；否则才把完整的 `flat::RoutingInfo` 塞进回包。

这等价于 HTTP 的 `ETag` + `304 Not Modified`：绝大多数轮询都是「无变化」的轻量回包，只有真变了才传整份数据。

#### 4.1.2 核心流程

服务端 `GetRoutingInfoOperation::handle` 的伪代码：

```
validateClusterId(req.clusterId)                 // 防止跨集群误连
doAsPrimary(state, {                             // 只有 primary 才服务
    dataPtr = state.data_.coSharedLock()          // 读锁，拿共享状态
    checkRoutingInfoVersion(req.routingInfoVersion) // 客户端版本不能比我新
    rsp.info = dataPtr->getRoutingInfo(req.routingInfoVersion, config)
    return rsp
})
```

其中 `getRoutingInfo(version)` 的判定是：

```
if version == info.routingInfoVersion:   // 一样 → 无更新
    return std::nullopt
// 否则投影出一份 flat::RoutingInfo（拷贝 nodeMap/chains/...）
res.routingInfoVersion = info.routingInfoVersion
res.bootstrapping = ...
return res
```

客户端侧 `MgmtdClient::refreshRoutingInfoImpl(force)`：

```
currentVersion = 当前缓存的版本 (没有则 0)
req.routingInfoVersion = force ? 0 : currentVersion   // force=0 表示「强制全量刷新」
rsp = stub.getRoutingInfo(req)
if rsp.info 非空:
    新建 shared_ptr<flat::RoutingInfo>，原子替换 routingInfo_
    逐个通知已注册的 routingInfoListener（如 StorageClient）
```

> 注意 `force ? 0 : currentVersion`：传 `0` 会被服务端当作「强制刷新」——因为没有任何真实版本会是 0（`routingInfoVersion` 初值为 1，见 4.4.3）。这是客户端强制重新拉取整份视图的逃生通道。

#### 4.1.3 源码精读

**服务端入口**——[GetRoutingInfoOperation.cc:6-21](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/GetRoutingInfoOperation.cc#L6-L21)：先校验 `clusterId`，再在 `doAsPrimary` 包裹下（确保只有 primary 处理）取共享读锁，调用 `checkRoutingInfoVersion` + `getRoutingInfo`。

**版本校验**——[MgmtdData.cc:68-78](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdData.cc#L68-L78)：如果客户端带来的版本号**大于**服务端版本，返回 `kInvalidRoutingInfoVersion` 错误。这只在「客户端版本来自一个更新的 primary、而当前 primary 还没追上」时发生，客户端会重试或重新探测 primary。

**投影 + 缓存**——[MgmtdData.cc:80-114](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdData.cc#L80-L114)。这一段有几个要点：

- `version == info.routingInfoVersion` 时直接返回 `std::nullopt`（即「无更新」）。
- 投影过程把工作版 `mgmtd::RoutingInfo` 里的 `NodeInfoWrapper`、`TargetInfo` 等「带包装」的结构剥成扁平的 `flat::NodeInfo` / `flat::TargetInfo`（对应 [u3-l1](u3-l1-mgmtd-overview.md) 讲的「工作版 → 下发版投影」）。
- **`routingInfoCache`**（[MgmtdData.h:31](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdData.h#L31)）：因为同一版本会被大量客户端反复请求，mgmtd 把最近一次投影结果缓存起来，命中就直接返回缓存，避免每次都重新拷贝整张图。`version == 0`（强制刷新）时不读缓存，但仍然会把结果写进缓存供后续请求复用。

**客户端拉取与切换**——[MgmtdClient.cc:601-620](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/MgmtdClient.cc#L601-L620) 是 `refreshRoutingInfoImpl`，构造请求时根据 `force` 决定带当前版本还是 `0`；回包后调用 `updateRoutingInfo`。

**原子替换 + 监听者通知**——[MgmtdClient.cc:622-658](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/MgmtdClient.cc#L622-L658)：

```cpp
auto ri = std::make_shared<RoutingInfo>(newRoutingInfo ? newRoutingInfo : curRoutingInfo, SteadyClock::now());
routingInfo_.store(ri, std::memory_order_release);   // 原子指针替换，读写无锁
if (newRoutingInfo) {
  for (const auto &[_, listener] : *listenersPtr) listener(ri);  // 通知 StorageClient 等
}
```

`routingInfo_` 是 `folly::atomic_shared_ptr<RoutingInfo>`（[MgmtdClient.cc:750](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/MgmtdClient.cc#L750)）：读路径（任意线程随时 `getRoutingInfo()`）完全无锁，更新路径整块替换指针，读者要么看到旧快照、要么看到新快照，不会读到半新半旧。这和 [u2-l3](u2-l3-coroutine-and-pools.md) 里讲的「无锁读取」思路一致。

**周期拉取**——[MgmtdClient.cc:240-261](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/MgmtdClient.cc#L240-L261) 的 `startBackgroundTasksWithLock` 用 `BackgroundRunner` 注册了 `AutoRefresh`，周期由配置 `auto_refresh_interval` 控制，默认 **10 秒**（[MgmtdClient.h:20](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/MgmtdClient.h#L20)）。客户端的 `getRoutingInfo()`（[MgmtdClient.cc:440](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/MgmtdClient.cc#L440)）直接返回这份原子指针。

#### 4.1.4 代码实践：观察客户端拉取日志

**实践目标**：亲眼看到客户端周期性拉取，并在「无更新」时收到空回包。

**操作步骤**：

1. 在已部署的集群里，把某个 storage 节点或 FUSE 客户端的日志级别调到 `DBG`（参考 [u1-l3](u1-l3-deploy-and-admin-cli.md) 中 admin_cli 用法）或直接看进程 stdout。
2. 在该进程日志里过滤关键字 `RefreshRoutingInfo` 与 `get new routing info`。

**需要观察的现象**：

- 每隔约 10 秒出现一次 `RefreshRoutingInfo` 的处理记录。
- 在集群**没有变更**时，不会出现 `get new routing info version ...`（因为回包 `info` 为空，[MgmtdClient.cc:611-613](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/MgmtdClient.cc#L611-L613) 只在 `res->info` 非空时才打这条 INFO）。

**预期结果**：稳定状态下日志很安静（证明「304 式」省带宽生效）；一旦你在 mgmtd 侧做了一次拓扑变更，下一次轮询就会出现 `get new routing info version N`。

> 待本地验证：具体日志级别开关与路径以你部署版本为准；若 `DBG` 仍看不到，可临时调到 `DBG3`。

#### 4.1.5 小练习与答案

**Q1**：如果客户端把自己的 `routingInfoVersion` 改成 `0` 发给 mgmtd，会发生什么？

> **答**：mgmtd 把 `0` 视为「强制刷新」，忽略缓存、返回当前完整视图（见 [MgmtdData.cc:88-93](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdData.cc#L88-L93)）。这正是客户端 `force=true` 路径做的事。

**Q2**：为什么 `routingInfo_` 用 `atomic_shared_ptr` 而不是 `CoroSynchronized`？

> **答**：读路径是任意线程、极高频的同步调用（每次发 IO 都要查路由），必须无锁且不能阻塞；`atomic_shared_ptr` 的「整体替换指针」正好满足「读到一致快照」而无须加锁。`CoroSynchronized` 适合协程里偶尔的共享状态，不适合这种热点只读路径。

---

### 4.2 节点 tag 与 enable/disable：管理操作如何影响路由

#### 4.2.1 概念说明

`NodeInfo` 上挂着一组 `tags`（键值对），mgmtd 定义了两个**有特殊语义**的 tag 键（[AppInfo.h:129-130](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/AppInfo.h#L129-L130)）：

```cpp
inline constexpr auto kDisabledTagKey  = "Disable";     // 节点被禁用
inline constexpr auto kTrafficZoneTagKey = "TrafficZone"; // 节点所属的流量分区
```

- **`Disable` tag**：与节点的 `status == DISABLED` 是**一体两面**。`disable-node` 操作会同时把状态置为 `DISABLED` 并加上 `Disable` tag；`enable-node` 则反过来。因为 tag 是 `NodeInfo` 的一部分，而 `NodeInfo` 进了 `RoutingInfo`，所以「禁用一个节点」本质上是**一次路由信息变更**，会走 4.4 的版本推进流程，让所有客户端都看到。
- **`TrafficZone` tag**：用于**客户端侧**的读流量隔离。客户端可以声明「我只从标记了某些 zone 的节点读」，从而把读流量限制在特定机架/机房。它不改变 mgmtd 的拓扑，只改变客户端选 target 时的过滤逻辑。

`enable/disable` 与 `set-node-tags` 都是**管理员操作**（需要 `validateAdmin` 校验 `user`），并且都只能在 primary 上执行。

#### 4.2.2 核心流程

**禁用/启用节点**（[EnableDisableNodeOperation.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/EnableDisableNodeOperation.cc)）：

```
doAsPrimary + coScopedLock<"EnableNode"/"DisableNode"> 写锁
读取旧 NodeInfo
若 enable 且当前 DISABLED：onNodeEnabled → status=HEARTBEAT_CONNECTING，移除 Disable tag
若 disable 且当前非 DISABLED：onNodeDisabled → status=DISABLED，添加 Disable tag
否则：无变化，直接返回（幂等）
updateStoredRoutingInfo：把新 NodeInfo 落 FDB + 预先 bump 存储版本号
updateMemoryRoutingInfo：写回内存 nodeMap，标记 routingInfoChanged=true
```

注意一个保护：**不能禁用 primary mgmtd 自己**（[EnableDisableNodeOperation.cc:68-70](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/EnableDisableNodeOperation.cc#L68-L70)、[L81-83](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/EnableDisableNodeOperation.cc#L81-L83)），否则会把自己踢下线导致集群无主。

**改 tag**（[SetNodeTagsOperation.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/SetNodeTagsOperation.cc)）支持三种模式（`updateTags`，[helpers.cc:39-75](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/helpers.cc#L39-L75)）：`REPLACE`（整体替换）、`UPSERT`（增量添加，对已存在的键由旧值兜底——实际是 `try_emplace`）、`REMOVE`（删除，要求 value 为空）。它还会**联动状态**：手动加 `Disable` tag 会让状态变 `DISABLED`，手动移除 `Disable` tag 会让状态变回 `HEARTBEAT_CONNECTING`（[SetNodeTagsOperation.cc:44-49](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/SetNodeTagsOperation.cc#L44-L49)）。

**客户端侧过滤**（[StorageClientImpl.cc:418-487](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClientImpl.cc#L418-L487)）：`selectServingTargets` 遍历链上的 target，对每个 target 反查其所在 `NodeInfo`，再用一个 `nodeSelector` 谓词过滤。这个谓词默认就是 `selectNodeByTrafficZone(options.trafficZone())`——按 traffic zone 过滤。

#### 4.2.3 源码精读

**状态翻转的两个函数**——[EnableDisableNodeOperation.cc:8-22](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/EnableDisableNodeOperation.cc#L8-L22)：

```cpp
flat::NodeInfo onNodeEnabled(...)   { sn.status = HEARTBEAT_CONNECTING; removeTag(sn.tags, kDisabledTagKey); }
flat::NodeInfo onNodeDisabled(...)  { sn.status = DISABLED; sn.tags.emplace_back(kDisabledTagKey, ""); }
```

两行就讲清了「状态 + tag 同步」的核心。`changeNodeStatus` 模板（[L24-60](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/EnableDisableNodeOperation.cc#L24-L60)）把「读旧值→判定是否真要变→落 FDB→改内存」串起来，复用了 4.4 要讲的 `updateStoredRoutingInfo` / `updateMemoryRoutingInfo`。

**TrafficZone 过滤谓词**——[NodeInfo.h:54-69](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/NodeInfo.h#L54-L69)：

```cpp
auto p = [zones](const flat::NodeInfo &node) {
  if (zones.empty()) return true;                 // 客户端没设 zone → 任意节点都可
  for (const auto &tp : node.tags)
    if (tp.key == kTrafficZoneTagKey && zones 包含 tp.value) return true;
  return false;
};
```

**客户端选 target 时如何用**——[StorageClientImpl.cc:460-472](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClientImpl.cc#L460-L472)：拿到 target 的 `nodeId`，反查 `NodeInfo`，若 `!nodeSelector(*nodeInfo)` 就 `continue`（跳过这个 target，不计入可读列表）。于是「被过滤的节点上的 target」对客户端就「不可见」了。

> 关键认知：`disable-node` 之所以能让客户端「绕开」某节点，并不是 mgmtd 主动改了 target 的 public 状态，而是 **(a)** 节点 `status` 变 `DISABLED` 进入路由视图，**之后** 该节点停止心跳、其 target 的 local 状态翻为 `OFFLINE`，进而 public 状态在 [u3-l5](u3-l5-target-state-machine.md) 的状态机里被重算为非 `SERVING`；同时 **(b)** 客户端选 target 时只挑 `publicState == SERVING` 的 target（[StorageClientImpl.cc:435-444](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClientImpl.cc#L435-L444)）。两步叠加，被禁用节点最终不再承接新流量。

#### 4.2.4 代码实践：禁用一个节点并观察客户端路由变化

**实践目标**：验证 `disable-node` → 路由版本推进 → 客户端停止向该节点发读流量。

**操作步骤**：

1. 用 `admin_cli` 执行 `disable-node -n <nodeId>`（参考 [u1-l3](u1-l3-deploy-and-admin-cli.md) admin_cli 用法）。
2. 在 mgmtd 日志里找 `DisableNode` 与随后的 `RoutingInfo: bump ... version`（见 4.4.4）。
3. 在 FUSE 客户端日志里等待 `get new routing info version N`，然后观察该节点上的 storage 读请求是否归零（可通过 storage 侧的 `num_completed_ops` 指标或 RDMA 流量判断）。
4. 用 `admin_cli enable-node -n <nodeId>` 恢复。

**需要观察的现象**：禁用后经过一两个刷新周期（mgmtd 5s bump + 客户端 10s refresh，最坏约 15s），该节点读流量下降；恢复后流量回归。

**预期结果**：流量迁移平滑，没有报错风暴（因为 CRAQ 链上仍有其他 `SERVING` 副本可读，见 [u5-l3](u5-l3-write-path-craq.md)）。

> 待本地验证：具体迁移时长取决于心跳超时与状态机重算节奏（[u3-l5](u3-l5-target-state-machine.md)），本实践只追踪「版本推进 + 客户端切换」这一段。

#### 4.2.5 小练习与答案

**Q1**：为什么不直接在客户端配置里写「不要用某节点」，而要走 mgmtd 的 disable？

> **答**：集群节点多、客户端多，逐个客户端配置无法一致更新。disable 是「写一次 FDB、全员可见」的集中式控制；而且 disable 会驱动 target 状态机，让数据恢复/迁移流程也正确进行，单纯客户端过滤做不到这一点。

**Q2**：`set-node-tags` 用 `REPLACE` 模式清空所有 tag 后，原本 `DISABLED` 的节点会怎样？

> **答**：`Disable` tag 被移除 → 联动逻辑把状态从 `DISABLED` 翻回 `HEARTBEAT_CONNECTING`（[SetNodeTagsOperation.cc:44-46](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/SetNodeTagsOperation.cc#L44-L46)），等价于一次 `enable-node`。

---

### 4.3 配置托管与管理：SetConfig / GetConfig / ConfigVersion

#### 4.3.1 概念说明

[u2-l5](u2-l5-config-system.md) 讲的是「一个进程内部如何声明、热更新配置」。本节讲的是这些配置**从哪来**：它们由 mgmtd 统一托管在 FoundationDB 里，按 `NodeType` 分桶，每个桶内是一串**单调递增的版本**。

数据结构 `flat::ConfigInfo`（[ConfigInfo.h:7-14](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/ConfigInfo.h#L7-L14)）只有三个字段：

```cpp
SERDE_STRUCT_FIELD(configVersion, ConfigVersion(0));  // 版本号
SERDE_STRUCT_FIELD(content, String{});                // TOML 配置正文
SERDE_STRUCT_FIELD(desc, String{});                   // 人类可读的变更说明
```

内存里用两层 map 组织（[MgmtdData.h:18-19](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdData.h#L18-L19)）：

```cpp
using VersionedConfigMap = std::map<flat::ConfigVersion, flat::ConfigInfo>;  // 按 version 有序
using ConfigMap = std::map<flat::NodeType, VersionedConfigMap>;              // 按 NodeType 分桶
```

`std::map` 按 key 有序，所以「最新版本」永远是 `rbegin()->first`，这在后面多处用到。

**和 RoutingInfo 的关键区别**：配置版本号 `ConfigVersion` 是**按 NodeType 独立**计数的（MGMTD 的版本和 STORAGE 的版本互不相干），而 `routingInfoVersion` 是**整张路由视图共用**一个版本号。另外，配置**不进** RoutingInfo，它有自己独立的分发通道。

#### 4.3.2 核心流程

**写配置 `SetConfig`**（[SetConfigOperation.cc:6-59](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/SetConfigOperation.cc#L6-L59)）：

```
validateClusterId + validateAdmin           // 仅管理员
coScopedLock<"SetConfig"> 写锁
读出该 NodeType 的最新版本 oldVersion
newVersion = nextVersion(oldVersion) = oldVersion + 1
withReadWriteTxn：storeConfig(txn, nodeType, ConfigInfo{newVersion, content, desc})  // 落 FDB
写回内存：configMap[nodeType][newVersion] = newConfigInfo
若 nodeType == MGMTD：
    调用本进程的 configUpdater 应用到自己（primary 也要吃自己的配置）
    更新 selfNodeInfo_.configVersion 与 nodeMap 里自己的版本
返回 newVersion
```

**读配置 `GetConfig`**（[GetConfigOperation.cc:6-17](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/GetConfigOperation.cc#L6-L17)）：调用 `getConfig(nodeType, version, !exactVersion)`。当 `exactVersion=false`（默认）时，语义是「我要比 `version` 更新的配置；若已是最新就返回空」——和 `GetRoutingInfo` 一模一样的「304 语义」（[MgmtdData.cc:30-58](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdData.cc#L30-L58)）。

**配置如何到达节点**：节点并不主动轮询配置，而是**搭心跳的便车**。心跳回包 `HeartbeatRsp` / `ExtendClientSessionRsp` 里带一个可选的 `config` 字段：当 mgmtd 发现该节点的 `configVersion` 落后于最新时，就把新配置塞进回包；节点收到后调用本地的 config listener 应用，并把本地版本推进到回包里的版本（[MgmtdClient.cc:731-740](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/MgmtdClient.cc#L731-L740) 心跳路径、[L364-372](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/MgmtdClient.cc#L364-L372) client session 路径）。这正是 [u2-l5](u2-l5-config-system.md) 里说的「心跳拉取」落地处。

#### 4.3.3 源码精读

**SetConfig 的版本推进**——[SetConfigOperation.cc:30-38](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/SetConfigOperation.cc#L30-L38)：

```cpp
auto newVersion = nextVersion(*oldVersionRes);
auto newConfigInfo = flat::ConfigInfo::create(newVersion, content, req.desc);
// ... 落 FDB ...
dataPtr->configMap[nodeType][newVersion] = std::move(newConfigInfo);
```

`nextVersion` 就是简单的 `+1`（[helpers.h:16-20](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/helpers.h#L16-L20)）。

**FDB 里的配置 key——一个值得细品的编码技巧**——[MgmtdStore.cc:85-95](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/store/MgmtdStore.cc#L85-L95)：

```cpp
String getConfigKey(flat::NodeType nodeType, flat::ConfigVersion version) {
  Serializer s(buf);
  s.put(kv::KeyPrefix::Config);
  s.putShortString(toStringView(nodeType));
  // let the latest version be the first
  auto reversedBigVer = folly::Endian::big64(UINT64_MAX - version.toUnderType());
  s.put(reversedBigVer);
  return buf;
}
```

为什么要把版本号做 `UINT64_MAX - version` 再用大端序？因为 FDB（LSM-tree，按 key 字典序排列）的 `listByPrefix` 返回的是**升序**结果。要让「最新版本排在最前」，就把版本反转：版本越大 → 反转值越小 → 排越前。于是 `loadLatestConfig` 只需 `listByPrefix(prefix, limit=1)` 取第一条（[MgmtdStore.cc:303-321](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/store/MgmtdStore.cc#L303-L321)），一次前缀扫描就能拿到最新配置，无需把所有历史版本都读出来再排序。

对比一下：[u4-l2](u4-l2-inode-direntry-encoding.md) 会讲 meta 用 **little-endian** 让 inode id 在 FDB 里**均匀分布**（为了均衡热点）；这里 mgmtd 用 **大端 + 反转** 让版本**有序且最新在前**。同一个 FDB，不同的字节序选择服务于不同目的，这是很典型的 KV 编码设计取舍。

**写配置**——[MgmtdStore.cc:282-287](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/store/MgmtdStore.cc#L282-L287)：`storeConfig` 就是 `info.store(txn, getConfigKey(...))`，value 是整个 `ConfigInfo` 序列化。注意配置是**只追加不覆盖**——每个版本一个独立 key，历史版本永久保留（便于审计与回退排查）。

**心跳回包带配置**——[MgmtdClient.cc:728-741](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/MgmtdClient.cc#L728-L741)：

```cpp
if (res->config) {
  auto listener = serverConfigListener_.load(...);
  if (!listener || (*listener)(res->config->content, ...))   // 应用配置（u2-l5 的 atomicallyUpdate）
    heartbeatInfo_->configVersion = res->config->configVersion;  // 成功才推进本地版本
}
```

注意「listener 返回 true 才推进版本」——如果配置应用失败（门禁不过、校验失败），本地版本不前进，下次心跳 mgmtd 还会再下发，形成自然重试。这与 [u2-l5](u2-l5-config-system.md) 的 `ConfigStatus`（NORMAL/DIRTY/FAILED）联动：应用失败会留下 DIRTY/FAILED 标记。

#### 4.3.4 代码实践：set-config 与版本号观察

**实践目标**：改一个运行时配置，观察它在 mgmtd 落库、版本号 +1、并被节点应用。

**操作步骤**：

1. 选一个可热更新的配置项（例如某个超时阈值，确认它标了 `CONFIG_HOT_UPDATED_ITEM`，见 [u2-l5](u2-l5-config-system.md)）。
2. 用 `admin_cli get-config -t <NodeType>` 导出当前配置与版本号。
3. 编辑该 TOML，`admin_cli set-config -t <NodeType> --desc "bump timeout"` 上传。
4. 再次 `get-config` 确认版本号 +1、`content` 已更新。
5. 在目标节点日志里找配置应用记录（如 `Config: atomicallyUpdate` 或对应的 `ConfigStatus` 变化）。

**需要观察的现象**：`set-config` 回包里带 `configVersion`；目标节点在**下一次心跳**（约 10s）后应用新配置。

**预期结果**：配置平滑生效，无需重启进程。

> 待本地验证：具体配置项名与日志关键字以你的版本为准；可先用 `get-config` 看 `desc` 字段确认上一次变更记录。

#### 4.3.5 小练习与答案

**Q1**：为什么配置按 `NodeType` 而不是按节点（NodeId）分桶？

> **答**：同一类服务（如所有 storage）共享同一份配置模板，按类型分发能让一次 `set-config` 同时推给该类所有节点，避免逐节点配置漂移。个别节点的差异（如监听地址）放在本地 `*_app.toml`，不进托管配置（见 [u1-l3](u1-l3-deploy-and-admin-cli.md) 三类配置）。

**Q2**：如果心跳回包里带了新配置但 listener 应用失败，本地 `configVersion` 会前进吗？

> **答**：不会（[MgmtdClient.cc:733-739](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/MgmtdClient.cc#L733-L739)）。下次心跳 mgmtd 仍认为该节点落后，会再次下发，直到应用成功。这是「失败可重试」的安全设计。

---

### 4.4 版本号推进：从变更落地到客户端切换

#### 4.4.1 概念说明

前面三个模块都反复出现「版本号推进」这个动作，本节把它单独拎出来讲透。mgmtd 用了两种版本号，推进机制不同：

| 版本号 | 作用域 | 推进时机 | 推进者 |
| --- | --- | --- | --- |
| `routingInfoVersion` | 整张路由视图 | 「脏标记」置位后，**周期性**批量推进 | `MgmtdRoutingInfoVersionUpdater`（后台任务） |
| `ConfigVersion` | 单个 NodeType | `SetConfig` 时**立即**推进 | `SetConfigOperation` |

为什么路由版本要「周期批量」而配置版本可以「立即」？因为**路由变更高频且往往成簇发生**：一次节点故障会让一连串 chain 的 target 状态变化（见 [u3-l5](u3-l5-target-state-machine.md) 的 `MgmtdChainsUpdater`），如果每变一次就 bump 一次版本、写一次 FDB、让所有客户端重拉一次，会造成「版本号风暴」和 FDB 写放大。于是 mgmtd 用一个**脏标记解耦**：

> 任何修改路由的 Operation（registerNode / heartbeat 改 target 状态 / disableNode / setChainTable / setChains / updateChain …）都只做两件事：**改内存数据** + **把 `routingInfo.routingInfoChanged = true`**。真正的「bump 版本号 + 落 FDB + 通知」由后台任务周期性（默认 5s）统一做，把这一窗口内的所有变更**合并成一次版本递增**。

这是一个典型的「**写时打标、周期合并**」（write-mark / periodic-flush）模式。

#### 4.4.2 核心流程

**变更侧**（任一改路由的 Operation）：

```
coScopedLock<...> 写锁
改 routingInfo 的某部分（nodeMap / chains / ...）
（或调用 updateMemoryRoutingInfo 的带 handler 版本，在同一个事务里改）
→ 隐含/显式置 routingInfoChanged = true
```

**推进侧**（后台 `BumpRoutingInfoVersion`，每 5s）：

```
doAsPrimary + coScopedLock<"BumpRoutingInfoVersion"> 写锁
读 routingInfoChanged
if routingInfoChanged:
    updateStoredRoutingInfo:                          // 先落 FDB
        nextv = routingInfoVersion + 1
        withReadWriteTxn: storeRoutingInfoVersion(txn, nextv)
    updateMemoryRoutingInfo:                           // 再改内存
        ++routingInfo.routingInfoVersion
        routingInfoChanged = false
（否则什么都不做，本轮空转）
```

**客户端感知侧**（已在 4.1 讲）：

```
（每 10s）refreshRoutingInfoImpl 带 currentVersion 询问
mgmtd 发现 currentVersion < 服务端版本 → 返回新视图
updateRoutingInfo 原子替换 atomic_shared_ptr → 通知 listener
```

**端到端时序**（一次 chain table 更新）：

```
t=0    admin_cli set-chain-table → 改内存 + changed=true
t≤5s   BumpRoutingInfoVersion 周期到达 → 写 FDB + 内存版本 N→N+1 + changed=false
t≤15s  客户端 AutoRefresh 周期到达 → 拿到版本 N+1 → 切换视图 → StorageClient 重建 target 选择
```

最坏感知延迟 ≈ bump 间隔(5s) + refresh 间隔(10s) ≈ **15 秒**。

#### 4.4.3 源码精读

**脏标记与版本初值**——[RoutingInfo.h:31-39](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/RoutingInfo.h#L31-L39)（mgmtd 工作版）：

```cpp
bool routingInfoChanged = false;                // periodically promote routingInfoVersion
flat::RoutingInfoVersion routingInfoVersion{1}; // ensure version 0 is less than any valid version
```

注释直接点明设计意图：`routingInfoVersion` 初值是 **1** 而非 0，正是为了让「0」能被 4.1 里的强制刷新当作特殊哨兵复用。

**后台推进任务**——[MgmtdRoutingInfoVersionUpdater.cc:10-27](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdRoutingInfoVersionUpdater.cc#L10-L27)：

```cpp
auto handle(MgmtdState &state) -> CoTryTask<void> {
  auto writerLock = co_await state.coScopedLock<"BumpRoutingInfoVersion">();
  bool needChange = co_await [...]{ co_return dataPtr->routingInfo.routingInfoChanged; }();
  if (needChange) {
    co_await updateStoredRoutingInfo(state, *this);   // 先 FDB
    co_await updateMemoryRoutingInfo(state, *this);   // 再内存
  }
}
```

它被 `MgmtdRoutingInfoVersionUpdater::update()`（[L32-43](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdRoutingInfoVersionUpdater.cc#L32-L43)）包在 `doAsPrimary` 里——**只有 primary 推进版本**，备机直接 `skip`（收到 `kNotPrimary` 时只打 INFO 日志）。

**「先落盘再改内存」的顺序很重要**——`updateStoredRoutingInfo`（[helpers.h:72-81](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/helpers.h#L72-L81)）先把 `nextv` 写进 FDB 的 `RoutingInfoVersion` key（[MgmtdStore.cc:252-258](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/store/MgmtdStore.cc#L252-L258)），成功后才调用 `updateMemoryRoutingInfo` 把内存版本 +1（[helpers.cc:77-82](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/helpers.cc#L77-L82)）：

```cpp
void updateMemoryRoutingInfo(RoutingInfo &ri, ...) {
  ++ri.routingInfoVersion.toUnderType();
  ri.routingInfoChanged = false;
}
```

这个顺序保证了「内存里可见的版本号，FDB 里一定已经写过了」——这是 [u3-l3](u3-l3-primary-election.md) 里 primary 切换时能从 FDB 重建路由、且 `routingInfoVersion` 单调不退化的前提。注意 `RoutingInfoVersion` 在 FDB 里是一个**单独的 key**（`Single` 前缀，[MgmtdStore.cc:21-24](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/store/MgmtdStore.cc#L21-L24)），而不是把整份路由信息序列化成一个 value——具体每类实体（node/chain/chainTable/target）各有自己的 key 前缀，版本号只是它们的「逻辑检查点」。

**任务注册**——[MgmtdBackgroundRunner.cc:63-66](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdBackgroundRunner.cc#L63-L66)：`bumpRoutingInfoVersion` 挂在 `BackgroundRunner` 上，周期 `bump_routing_info_version_interval`，默认 **5 秒**（[MgmtdConfig.h:24](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdConfig.h#L24)）。这正是 [u3-l1](u3-l1-mgmtd-overview.md) 提到的「mgmtd 用 BackgroundRunner 跑约 10 个周期任务」之一。

**客户端版本不回退断言**——[MgmtdClient.cc:626-630](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/MgmtdClient.cc#L626-L630)：客户端拿到新视图时，若发现 `currentVersion > newRoutingInfo->routingInfoVersion`（版本回退）会直接 `FATAL`。这是「版本号全局单调递增」不变量的运行期守卫。

#### 4.4.4 代码实践：追踪一次 chain table 更新的版本推进

**实践目标**：把第 4.4.2 节的时序图在真实集群里走一遍，记录每一步的日志与时间戳。

**操作步骤**：

1. mgmtd 日志级别调到能看见 `INFO`，过滤 `bumpRoutingInfoVersion` / `RoutingInfo: bump` / `updateChains`。
2. 客户端（FUSE）日志过滤 `get new routing info version`。
3. 用 `admin_cli upload-chain-table`（或 `set-chain-table`）更新一张链表（参考 [u8-l1](u8-l1-data-placement.md) 的建链流程）。
4. 记录三个时间点：\(t_1\) 下发命令、\(t_2\) mgmtd 打出 `RoutingInfo: bump storage version to N` 与 `bump memory version to N`、\(t_3\) 客户端打出 `get new routing info version N`。

**需要观察的现象**：

- \(t_2 - t_1 \le 5\text{s}\)（受 bump 周期约束）。
- \(t_3 - t_2 \le 10\text{s}\)（受客户端 refresh 周期约束）。
- 若在 \(t_1\) 之后、\(t_2\) 之前又有别的路由变更，它们会**合并进同一个** \(N\)（只 bump 一次）——可在 mgmtd 日志里确认 `routingInfoChanged` 被多次置位但版本只 +1。

**预期结果**：客户端在约 15s 内切到新链表视图，`list-chains` / `list-chain-tables` 反映新结构。

> 待本地验证：实际延迟受日志刷新与调度抖动影响；若想观察「合并」效果，可在 5s 窗口内连续做两次 `set-node-tags` 等轻量变更。

#### 4.4.5 小练习与答案

**Q1**：如果 `bump_routing_info_version_interval` 调大到 60s，会有什么利弊？

> **答**：好处是 FDB 写放大更小、客户端拉取更稀疏（省带宽）；坏处是路由变更的「可见延迟」拉长到最多 70s，故障切换、disable 节点的生效变慢。这是一个**延迟 vs 写放大**的权衡旋钮。

**Q2**：为什么 `updateStoredRoutingInfo` 要先写 FDB、`updateMemoryRoutingInfo` 后改内存，顺序不能反？

> **答**：若先改内存版本、再写 FDB，万一写 FDB 失败，内存版本会高于 FDB——此时若 primary 崩溃、新 primary 从 FDB 重建，会得到一个**更小**的版本号，违反单调递增，甚至让已经「看到」高版本的客户端 FATAL。先落盘保证了「内存可见的版本一定已持久化」，崩溃切换后版本不会回退。

---

## 5. 综合实践：端到端追踪「chain table 更新 → 客户端切换」

把本讲四个模块串起来，完成一次完整的端到端追踪。这是本讲规格里指定的实践任务。

**背景**：你刚用数据放置脚本（[u8-l1](u8-l1-data-placement.md)）生成了一张新链表，现在要让集群用上它。请回答：从你敲下上传命令，到 FUSE 客户端真正按新链表读写，中间发生了什么？

**任务**：

1. **画出时序图**，横轴为时间，纵轴为四个角色：`admin_cli`、`mgmtd(primary)`、`FoundationDB`、`FUSE 客户端`。标出下列事件及其所在文件/行号：
   - `SetChainTable` 操作改内存 + 置 `routingInfoChanged=true`；
   - `BumpRoutingInfoVersion` 周期到达 → `updateStoredRoutingInfo`（写 FDB `RoutingInfoVersion` key）→ `updateMemoryRoutingInfo`（版本 +1、清脏标记）；
   - 客户端 `AutoRefresh` → `refreshRoutingInfoImpl`（带旧版本询问）→ mgmtd `getRoutingInfo` 返回新视图 → `updateRoutingInfo` 原子替换 + 通知 listener；
   - `StorageClient` 收到 listener 回调，重建 target 选择（[StorageClientImpl.cc:490-512](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClientImpl.cc#L490-L512)）。
2. **验证版本号链路**：用 `admin_cli list-chain-tables` 看到新结构后，对照 mgmtd 日志里的 `bump ... version` 与客户端日志里的 `get new routing info version`，确认三者版本号一致。
3. **回答两个追问**：
   - 如果更新后立刻（< 5s）kill 掉 primary mgmtd，新链表会丢吗？为什么？（提示：结合 [u3-l3](u3-l3-primary-election.md) 与本节「先落盘再改内存」。）
   - 如果某个 storage 节点恰好在这 15s 窗口内重启，它会用旧版本还是新版本的路由？（提示：storage 也是 `MgmtdClient`，启动时带 `version=0` 强制全量拉取。）

**参考答案要点**：

- 新链表**不会丢**：`updateStoredRoutingInfo` 先把新版本号与各实体（chain/chainTable 各自的 key）写进 FDB，内存版本是在 FDB 写成功之后才 +1 的。primary 崩溃后新 primary 从 FDB 全量重建（[u3-l3](u3-l3-primary-election.md) 的 `onNewLease`），能读到最新结构。
- 重启的 storage 节点启动时 `routingInfoVersion=0`，等价于强制刷新，会直接拿到当前最新视图，不存在「用旧版本」的问题——这正是 4.1.1 里 `0` 作为哨兵的价值。

---

## 6. 本讲小结

- **路由分发是「带版本号的 304 式拉取」**：客户端带 `routingInfoVersion` 询问，mgmtd 只在「有更新」时返回完整 `flat::RoutingInfo`，否则回包 `info` 为空；客户端用 `atomic_shared_ptr` 无锁整体替换视图，并通知 `StorageClient` 等监听者。
- **两条独立版本号**：`routingInfoVersion`（整张路由视图共用）与 `ConfigVersion`（按 `NodeType` 独立计数）；前者周期批量推进，后者 `SetConfig` 时立即推进。
- **脏标记 + 周期 bump 解耦写与发**：路由变更只改内存 + 置 `routingInfoChanged=true`，由 5s 周期的 `BumpRoutingInfoVersion` 合并推进，避免版本号风暴与 FDB 写放大；且严格「先落 FDB 再改内存」，保证崩溃切换后版本单调不退。
- **配置托管在 FDB，按 NodeType 分桶，版本只追加**：用「反转版本 + 大端序」的 key 编码让最新版本排在前缀扫描首位；配置不主动轮询，而是搭心跳 / client session 回包便车下发，应用成功才推进本地版本。
- **tag 与 enable/disable 是路由视图的一部分**：`Disable` tag 与 `DISABLED` 状态一体两面，`TrafficZone` 用于客户端读流量隔离；它们随 `NodeInfo` 进 `RoutingInfo`，经版本推进被所有客户端感知，并影响 `selectServingTargets` 的 target 选择。

---

## 7. 下一步学习建议

本讲把「mgmtd 如何把视图与配置分发出去」讲完了。接下来建议：

- **[u4-l1 meta 服务总览](u4-l1-meta-overview.md)**：看消费方之一——meta 如何用拉到的 RoutingInfo / chain table 做链分配。
- **[u5-l4 TargetMap 与链路由](u5-l4-targetmap-routing.md)**：看 storage 侧如何用 RoutingInfo 判断自己在链中的位置、用 `chainVersion` 拒绝过期写请求——这是本讲「版本号」在数据面的落地。
- **[u3-l5 Target 状态机与故障检测](u3-l5-target-state-machine.md)**（若尚未读）：理解 `routingInfoChanged` 被置位的**最主要来源**——target public 状态的重算。
- **进阶阅读**：直接对照源码 `src/mgmtd/ops/` 下所有 Operation，会发现它们几乎都遵循同一套「`coScopedLock` 写锁 → 改内存 → `updateStoredRoutingInfo` + `updateMemoryRoutingInfo`」模板，本讲的套路可以举一反三地套用到每一个。
