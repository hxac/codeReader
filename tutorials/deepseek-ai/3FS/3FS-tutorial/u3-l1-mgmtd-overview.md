# mgmtd 服务总览与 RoutingInfo 数据模型

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 `mgmtd` 进程的分层结构：从 `main` 入口，到 `MgmtdServer`（网络服务），到 `MgmtdOperator`（RPC 分发 + 后台任务），再到 `MgmtdState` / `MgmtdData`（共享状态），最终落到 `RoutingInfo`（全局路由视图）这一条主线。
- 画出 `RoutingInfo` 里 `node` / `chain` / `chain_table` / `target` 四类实体的 ER（实体关系）图，并解释它们之间的数量关系与版本号语义。
- 读懂 mgmtd 用「双层锁」保护共享状态的并发模型：外层逻辑写锁 `writerMu_` 串行化整个「读—改—写」操作，内层 `CoroSynchronized<MgmtdData>` 用读写锁保护内存数据结构。

本讲是「集群管理服务 mgmtd」单元（u3）的第一篇，承接 [u2-l1](u2-l1-service-skeleton.md)（服务骨架）建立的认知——那一讲指出 meta/storage/mgmtd 三服务共用 `TwoPhaseApplication` 两阶段启动骨架；本讲就深入 mgmtd 这个「集群发现中枢」的内部结构。同时本讲为后续 [u3-l2](u3-l2-registration-heartbeat.md)（心跳）、[u3-l4](u3-l4-chain-target-model.md)（链表数据模型）、[u3-l5](u3-l5-target-state-machine.md)（target 状态机）铺路。

## 2. 前置知识

### 2.1 mgmtd 是什么：集群的「发现服务」

在 [u1-l3](u1-l3-deploy-and-admin-cli.md) 和 [u1-l4](u1-l4-end-to-end-flow.md) 里我们已经建立了两个关键认知：

- mgmtd 是全集群的「发现服务」：**所有进程（meta、storage、client）加入集群的第一步，都是先找到 mgmtd**。
- mgmtd 维护一份「全局路由视图（RoutingInfo）」，并向各方分发。

本讲要回答的核心问题是：**这份「全局路由视图」在 mgmtd 进程内部到底长什么样、由哪些数据结构组成、又如何在并发请求下被安全地读写？**

### 2.2 几个关键术语

- **Node（节点）**：一个加入集群的进程（mgmtd/meta/storage/client/fuse）。每个 node 有一个全局唯一的 `NodeId`、一个类型（`NodeType`）、一组监听地址和若干 tag。
- **Target（存储目标）**：一个存储副本。一条复制链由多个 target 组成，每个 target 落在某个 node 的某块磁盘上。target 同时有两套状态：`publicState`（对外可见的聚合状态）和 `localState`（节点本地上报的原始状态）。
- **Chain（链）**：一条 CRAQ 复制链，由一串有序的 target 组成，带一个随成员变更单调递增的 `chainVersion`。
- **ChainTable（链表）**：一组 chain 的有序集合，是数据放置的「地址空间」。文件按 stripe 切块后，用 `chunkId % 链数` 落到链表中的某一条链。链表自身也有版本号 `chainTableVersion`。
- **RoutingInfoVersion（路由版本号）**：单调递增的整数。每次 mgmtd 的路由视图发生变更，这个版本号就被推高，client/storage 据此判断「我手上的路由是否过期，要不要重新拉取」。

### 2.3 协程读写锁的直觉

mgmtd 的并发模型大量使用「协程读写锁」。如果你读过 [u2-l3](u2-l3-coroutine-and-pools.md)，应该记得：3FS 基于 folly 的 C++20 协程，加锁不是「阻塞线程」，而是 `co_await` 一个锁、**把当前协程挂起**，让出执行线程去跑别的协程。本讲用到的 `CoroSynchronized<T>` 就是把「一把协程读写锁 + 一个被保护的 `T`」打包在一起。你只要记住：`co_await xxx.coSharedLock()` 拿读锁（可多人并发读），`co_await xxx.coLock()` 拿写锁（独占）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/mgmtd/mgmtd.cpp` | mgmtd 进程的 `main` 入口，只有 8 行，证明它复用了通用启动骨架。 |
| `src/mgmtd/MgmtdServer.h` / `.cc` | 网络服务层：继承 `net::Server`，定义监听端口与 RPC 服务注册、生命周期钩子。 |
| `src/mgmtd/service/MgmtdOperator.h` | RPC 分发层：持有 `MgmtdState` 和 `MgmtdBackgroundRunner`，把每个 RPC 方法映射到一个 Operation。 |
| `src/mgmtd/service/MgmtdState.h` / `.cc` | 共享状态层：持有 `env_`、`store_`、`data_`、`clientSessionMap_`，以及外层逻辑写锁 `writerMu_`。 |
| `src/mgmtd/service/MgmtdData.h` / `.cc` | 真正的数据：`routingInfo`、`configMap`、`lease`、配置/路由的版本校验与序列化缓存。 |
| `src/mgmtd/service/RoutingInfo.h` / `.cc` | mgmtd 内部的工作版路由视图：`nodeMap` / `chainTables` / `chains` / `targets` 及其维护方法。 |
| `src/fbs/mgmtd/RoutingInfo.h` | 「线上版」路由视图 `flat::RoutingInfo`：序列化后发给 client/storage/meta 的扁平结构。 |
| `src/fbs/mgmtd/ChainInfo.h` / `ChainTable.h` / `TargetInfo.h` / `ChainTargetInfo.h` | 四类实体的字段定义。 |
| `src/fbs/mgmtd/MgmtdTypes.h` | `PublicTargetState`、`LocalTargetState`、`NodeId`/`ChainId` 等强类型枚举与 typedef。 |
| `src/common/utils/CoroSynchronized.h` | 协程读写锁包装器，是并发保护的核心工具。 |
| `src/mgmtd/service/helpers.h` | 读—改—写操作的辅助函数（`doAsPrimary`、`updateStoredRoutingInfo`、`updateMemoryRoutingInfo`）。 |
| `src/mgmtd/ops/GetRoutingInfoOperation.cc` / `RegisterNodeOperation.cc` | 两个典型 Operation，分别示范「只读」与「写」两条路径如何加锁。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**服务结构**、**路由信息数据模型**、**并发状态保护**。

### 4.1 服务结构

#### 4.1.1 概念说明

mgmtd 进程在代码里是一条清晰的「四层洋葱」：

```text
mgmtd.cpp (main, 两阶段骨架)
   └── MgmtdServer        ← 网络服务层：监听端口、注册 RPC 服务、生命周期钩子
         └── MgmtdOperator  ← 分发层：每个 RPC 方法 → 一个 Operation；同时跑后台任务
               └── MgmtdState  ← 共享状态层：env/store/userStore/data + 写锁
                     └── MgmtdData → RoutingInfo  ← 真正的全局路由视图
```

理解这条主线后，你看任意一个 mgmtd 的 RPC（比如 `GetRoutingInfo`、`RegisterNode`），都能按「请求进入 `MgmtdService` → 委托给 `MgmtdOperator` 的对应方法 → 构造一个 `XxxOperation` → 在 `Operation::handle(state)` 里加锁、读/写 `state.data_`」的固定套路去读。

#### 4.1.2 核心流程

mgmtd 进程的启动严格遵循 [u2-l1](u2-l1-service-skeleton.md) 讲过的两阶段骨架，只是模板参数换成 `MgmtdServer`：

1. `main` 调用 `TwoPhaseApplication<mgmtd::MgmtdServer>().run(argc, argv)`。
2. 引导阶段（`ServerLauncher`）：加载本地 `mgmtd_app.toml` / `mgmtd_launcher.toml`、起 IB、拼 `AppInfo`、拉运行时配置模板。
3. 运行阶段：`MgmtdServer::start()` 绑定端口，进入 `beforeStart` 钩子。
4. `beforeStart` 里：构造 `ServerEnv`、创建 `MgmtdOperator`、注册两个 RPC 服务（`MgmtdService` 与 `CoreService`）、`operator->start()` 启动后台任务。
5. 进入信号驱动的 `mainLoop()` 阻塞，直到收到停止信号。
6. 停止阶段：`afterStop` 销毁 `MgmtdOperator`，后台任务随之停止。

#### 4.1.3 源码精读

**入口确认复用骨架**——整个 `main` 只有一行有效代码，和 meta/storage 的入口几乎一字不差，差别仅在模板参数：

[mgmtd.cpp:5-8](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/mgmtd.cpp#L5-L8) 证实 mgmtd 就是 `TwoPhaseApplication<MgmtdServer>` 的实例化，没有任何额外逻辑。

**网络服务层 `MgmtdServer`** 继承自 `net::Server`，并用几个常量声明自己的身份，供骨架的 `RemoteConfigFetcher`、`AppInfo` 等机制使用：

[MgmtdServer.h:14-22](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/MgmtdServer.h#L14-L22) 声明了 `kNodeType = flat::NodeType::MGMTD`，以及三个供骨架识别的类型别名（`AppConfig` / `LauncherConfig` / `RemoteConfigFetcher` / `Launcher`）。

监听端口的配置在 `MgmtdServer::Config` 里。mgmtd 同时开了**两个 ServiceGroup**（参见 [u2-l4](u2-l4-network-rdma.md) 讲过的多 group 分流）：

[MgmtdServer.h:25-37](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/MgmtdServer.h#L25-L37) 显示：group 0 走默认网络（RDMA）、监听 8000 端口、承载 `"Mgmtd"` 业务服务；group 1 强制走 TCP、监听 9000 端口、用独立线程池承载 `"Core"` 服务。之所以 Core 服务单独走 TCP，是为了让集群管理控制面在 RDMA 不可用或 client 还没起 IB 时也能联通。

**`beforeStart` 钩子**是理解 mgmtd 运行期组装的关键，它把依赖注入进 `ServerEnv`、创建核心对象、注册 RPC 服务并启动后台任务：

[MgmtdServer.cc:22-38](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/MgmtdServer.cc#L22-L38) 依次完成：构造 `ServerEnv` → 注入 `kvEngine`（即 FoundationDB 引擎，mgmtd 用它持久化路由与配置）→ 注入 mgmtd stub 工厂、后台执行器、配置更新回调 → 创建 `MgmtdOperator` → `addSerdeService(MgmtdService)` 和 `addSerdeService(CoreService)` 注册两个服务 → `operator->start()` 启动后台任务。

**分发层 `MgmtdOperator`** 内部只持有两样东西：共享状态 `state_` 和后台任务总管 `backgroundRunner_`。它的 RPC 方法并非手写一个个函数声明，而是用一个 X-macro 批量生成（参见 [u2-l2](u2-l2-rpc-and-serde.md) 讲过的 fbs 代码生成）：

[MgmtdOperator.h:23-26](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdOperator.h#L23-L26) 用 `#include "fbs/mgmtd/MgmtdServiceDef.h"` 配合 `DEFINE_SERDE_SERVICE_METHOD_FULL` 宏，为 `MgmtdServiceDef.h` 里列出的每个方法自动生成 `CoTryTask<Rsp> method(Req, PeerInfo)` 声明。每个这样的方法通常只是构造一个对应的 `XxxOperation` 并调用其 `handle(state_)`。

**共享状态层 `MgmtdState`** 把跨 RPC、跨后台任务共享的对象集中存放：

[MgmtdState.h:32-38](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdState.h#L32-L38) 列出关键字段：`env_`（运行时依赖容器）、`selfNodeInfo_`（自身节点信息）、`config_`（运行时配置引用）、`store_`（FDB 存储封装，负责把路由/配置落盘）、`userStore_`（鉴权用）。

[MgmtdBackgroundRunner.h:23-46](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdBackgroundRunner.h#L23-L46) 展示 mgmtd 后台跑了约 10 个周期任务（心跳处理、租约续期、链更新、target 持久化/加载、路由版本推进等），它们和 RPC handler 一起读写同一份 `MgmtdState`，这也是为什么并发保护如此重要——后续 4.3 节详述。

#### 4.1.4 代码实践

**实践目标**：用「调用链追踪法」验证 mgmtd 的四层洋葱结构。

**操作步骤**：

1. 打开 `src/mgmtd/service/MgmtdService.cc`（或 `MgmtdOperator.cc`），找到 `GetRoutingInfo` 方法的实现，确认它构造了一个 `GetRoutingInfoOperation` 并调用 `handle(state_)`。
2. 打开 `src/mgmtd/ops/GetRoutingInfoOperation.h` 与 `.cc`，确认 `handle(MgmtdState &state)` 是业务逻辑落点。
3. 对照本节列出的四层，标注每一层的文件名。

**需要观察的现象**：从 RPC 入口到 `handle`，中间没有任何「业务逻辑」，全是转发/委托——这验证了分层是「纯转发 + 逻辑下沉到 Operation」的设计。

**预期结果**：你能画出 `MgmtdService::GetRoutingInfo → MgmtdOperator → GetRoutingInfoOperation::handle(MgmtdState&)` 这条直线，并指出 `state.data_` 是最终数据落点。若本地未编译运行，这部分结论标注为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：mgmtd 的 `main` 和 meta 的 `main` 看起来一模一样，唯一的区别是什么？

**答案**：模板参数不同——mgmtd 是 `TwoPhaseApplication<mgmtd::MgmtdServer>`，meta 是 `TwoPhaseApplication<meta::MetaServer>`。骨架完全复用，差异全在各自 `Server` 类的 `kNodeType`、`Config`、`beforeStart` 等定义里。

**练习 2**：为什么 `CoreService` 要单独开一个走 TCP 的 ServiceGroup，而不是和 `MgmtdService` 合并？

**答案**：`CoreService` 是集群管理控制面（含节点基本信息查询等），需要在「RDMA 尚未就绪、或 client 未启用 IB」的早期阶段也能被访问；强制 TCP + 独立线程池保证了它在任何环境下都可达，且不被 `MgmtdService` 的业务流量挤占。

### 4.2 路由信息数据模型

#### 4.2.1 概念说明

mgmtd 最核心的资产就是那份全局路由视图。代码里这份视图有**两个版本**，务必区分清楚：

- **`mgmtd::RoutingInfo`（工作版，内部用）**：定义在 `src/mgmtd/service/RoutingInfo.h`，是 mgmtd 内存里维护的、带额外「临时簿记」字段的完整工作副本。
- **`flat::RoutingInfo`（线上版，对外用）**：定义在 `src/fbs/mgmtd/RoutingInfo.h`，是去掉临时字段、可被序列化后通过网络发给 client/storage/meta 的扁平结构。

之所以分两份，是因为工作版里有些字段（如「刚出生还没稳定的新链 `newBornChains`」「找不到归属的孤儿 target」）只是 mgmtd 内部调度的临时簿记，不该也不需要发给下游。每次 client 拉取路由时，mgmtd 会现场把工作版「投影」成线上版（见 4.2.3 的 `getRoutingInfo`）。

#### 4.2.2 核心流程

先看四类实体的 ER 关系：

```text
┌──────────────┐   包含 N 条   ┌────────┐   包含 N 个   ┌─────────┐
│ ChainTable   │──────────────▶│ Chain  │─────────────▶│ Target  │
│ chainTableId │               │chainId │               │targetId │
│ tableVersion │               │chainVer│               │chainId  │
│ chains[]     │               │targets[]              │nodeId   │
└──────────────┘               │preferredOrder         │diskIndex│
        ▲                      └────────┘               │publicSt │
        │                         │                     │localSt  │
        │ 顺序: chainId 列表       │ targetId 引用         │usedSize │
        │                         │                     └────┬────┘
        │                         ▼                          │
        │               (chainVersion 随成员变更              │ nodeId 引用
        │                单调递增)                            ▼
        │                                                ┌────────┐
        │                                                │  Node  │
        │                                                │NodeId  │
        │                                                │type    │
        │                                                │status  │
        │                                                │tags[]  │
        │                                                └────────┘
        │
  Node 与 ChainTable/Chain/Target 是「正交」的两张表：
  Node 表记录「进程」，Target 通过 nodeId 关联回某个进程。
```

关系要点（记数量关系即可）：

- `ChainTable 1 — N Chain`：一个链表包含多条链，链表里只存 `chainId` 的有序列表。
- `Chain 1 — N Target`：一条链包含多个 target（CRAQ 复制组），链里存的是 `ChainTargetInfo`（`targetId` + `publicState`）。
- `Target N — 1 Node`：每个 target 落在唯一一个 node 的某块磁盘上，通过 `nodeId` 反查。
- `Target N — 1 Chain`：每个 target 属于唯一一条链（`chainId`）。

版本号的语义是这个模型的关键设计：

- **`routingInfoVersion`**：整个路由视图的全局版本号，单调递增。任何一处变更都会在后台周期性地把它推高（`routingInfoChanged` 标志位触发，见 [RoutingInfo.h:32-33](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/RoutingInfo.h#L32-L33)）。client 拿着自己手里的版本号去比对，发现落后就重新拉取。
- **`chainVersion`**（`uint32`）：单条链的版本号，随该链成员变更单调递增。
- **`chainTableVersion`**：链表的版本号；注意 `ChainTableMap` 是 `ChainTableId → (std::map<ChainTableVersion, ChainTable>)`，即**同一个链表 id 下保留了多个历史版本**，用有序 `std::map` 存放，方便按版本回溯。

#### 4.2.3 源码精读

**工作版 RoutingInfo 的字段与「持久 vs 临时」标注**：

[RoutingInfo.h:31-39](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/RoutingInfo.h#L31-L39) 列出全部字段。注意注释里的分类：`nodeMap`/`chainTables`/`chains` 标注为 `persistent`（重启后从 FDB 重新加载），`newBornChains`/`orphanTargets*` 标注为 `temporal`（仅在内存、重启即丢）。版本号 `routingInfoVersion` 初值是 1 而非 0，注释解释「确保版本 0 比任何合法版本都小」——因为 client 用 0 表示「强制全量刷新」。

四类核心容器的类型别名定义紧挨在结构体上方，值得逐行看：

[RoutingInfo.h:22-29](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/RoutingInfo.h#L22-L29) 给出容器类型：`NodeMap = unordered_map<NodeId, NodeInfoWrapper>`、`ChainTableMap = unordered_map<ChainTableId, map<ChainTableVersion, ChainTable>>`（内层用有序 `std::map` 保多版本）、`ChainMap = unordered_map<ChainId, ChainInfo>`、`TargetMap = unordered_map<TargetId, TargetInfo>`。

一个容易忽略的细节：**`targets` 是「派生（derived）」字段**，被刻意设为 `private`：

[RoutingInfo.h:66-75](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/RoutingInfo.h#L66-L75) 显示 `reset(...)` 接收外部传入的 `allTargets`，而 `targets` 本身是私有成员。这是因为 target 信息其实已经「内嵌」在每条 chain 的 `targets` 向量里了，单独再建一张 `TargetId → TargetInfo` 的索引表只是为了 O(1) 快速查找，属于「可由 chain 重建的派生数据」。

**四类实体的字段定义**逐一对应 ER 图：

- [ChainTable.h:7-14](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/ChainTable.h#L7-L14)：`chainTableId`、`chainTableVersion`、`chains`（`vector<ChainId>`）、`desc`。
- [ChainInfo.h:6-14](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/ChainInfo.h#L6-L14)：`chainId`、`chainVersion`、`targets`（`vector<ChainTargetInfo>`）、`preferredTargetOrder`。
- [ChainTargetInfo.h:8-13](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/ChainTargetInfo.h#L8-L13)：链内对单个 target 的引用，只有 `targetId` + `publicState` 两项。
- [TargetInfo.h:8-18](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/TargetInfo.h#L8-L18)：target 的完整信息，`publicState`（对外聚合状态）、`localState`（节点上报的原始状态）、`chainId`、`nodeId`、`diskIndex`、`usedSize`。

注意 `TargetInfo` 同时持有 `publicState` 和 `localState` 两套状态——这正是 [u3-l5](u3-l5-target-state-machine.md) target 状态机的输入与输出。两套枚举的定义在：

[MgmtdTypes.h:10-28](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/MgmtdTypes.h#L10-L28)：`PublicTargetState ∈ {INVALID, SERVING, LASTSRV, SYNCING, WAITING, OFFLINE}`，`LocalTargetState ∈ {INVALID, UPTODATE, ONLINE, OFFLINE}`。注意这些枚举值被刻意设成 2 的幂（1/2/4/8/16），方便做位运算组合。

**工作版 ↔ 线上版的投影**发生在 `MgmtdData::getRoutingInfo`，这是 client 拉路由时的核心路径：

[MgmtdData.cc:80-114](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdData.cc#L80-L114) 做三件事：(1) 版本校验，请求版本等于当前版本就直接返回 `nullopt`（「你已经最新，无需下发」）；(2) 若开启缓存且非强制刷新，先查 `routingInfoCache`；(3) 现场构造一个 `flat::RoutingInfo`——只拷贝 `nodeMap` 的 `base()`、`chainTables`、`chains` 和 targets 的 `base()`，**刻意丢弃了工作版的 `newBornChains`、orphan target 等临时字段**，这正是「投影」的体现。

**线上版 `flat::RoutingInfo`** 的字段比工作版「干净」，且自带一组查询方法：

[fbs/mgmtd/RoutingInfo.h:11-48](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/RoutingInfo.h#L11-L48) 提供 `getChainTable`/`getChain`/`getNode`/`getTarget` 等便捷查找，字段就是 `routingInfoVersion`、`bootstrapping`、`nodes`、`chainTables`、`chains`、`targets`——下游（client/storage）拿到它后，用这套方法在本机路由。

**视图重建**：当 primary mgmtd 重启或换主后，需要从 FDB 把整份 RoutingInfo 重新载入内存，入口是 `RoutingInfo::reset`：

[RoutingInfo.cc:145-172](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/RoutingInfo.cc#L145-L172) 先清空所有容器，再依次填回 nodes / chainTables / chains / targets，并刷新 `routingInfoVersion`；末尾还把所有现存链都塞进 `newBornChains`（注释解释：避免重启后过早更新链导致状态不稳）。

#### 4.2.4 代码实践

**实践目标**：画出 `node` / `chain` / `chain_table` / `target` 的 ER 关系图（本讲要求的综合实践前置练习）。

**操作步骤**：

1. 阅读上文 4.2.1 的两个 `RoutingInfo` 结构与 4.2.2 的 ER 图。
2. 在纸上（或任意画图工具）画出四类实体框，用线标出关系，并在每条线上标注数量（1—N）和「通过哪个字段关联」（如 chain→target 用 `ChainTargetInfo.targetId`）。
3. 在 `target` 实体框里，明确标出 `publicState` 与 `localState` 两套状态字段。

**需要观察的现象**：你会发现自己画的图里，`Node` 表与「ChainTable/Chain/Target」三件套是**正交**的两张子图——它们唯一的连接点是 `Target.nodeId`。

**预期结果**：得到一张与本文 4.2.2 流程图一致的 ER 图。若想用真实数据验证，可在已部署集群上用 `admin_cli list-nodes` / `list-chains` / `list-chain-tables` 的输出核对实体关系；未部署则标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ChainTableMap` 的内层用 `std::map<ChainTableVersion, ChainTable>`（有序）而不是 `unordered_map`？

**答案**：同一个链表 id 下保留了多个历史版本，业务上经常需要「取最新版本」（`rbegin()`）或按特定版本回溯；有序 `std::map` 让「最新版本」就是 `rbegin()`，O(log n) 取到，且能按版本范围遍历。`unordered_map` 无法保证版本顺序，做不到这一点。

**练习 2**：工作版 `RoutingInfo` 的哪些字段不会出现在发给 client 的 `flat::RoutingInfo` 里？为什么？

**答案**：`newBornChains`、`orphanTargetsByTargetId`、`orphanTargetsByNodeId`、`routingInfoChanged` 都不会下发。前两者是 mgmtd 内部调度的临时簿记（刚出生待稳定的新链、找不到归属的孤儿 target），对下游路由无用甚至有害；`routingInfoChanged` 只是内部「该推高版本号了」的脏标记。`MgmtdData::getRoutingInfo` 在投影时只拷贝 `nodeMap`/`chainTables`/`chains`/`targets` 四项。

### 4.3 并发状态保护

#### 4.3.1 概念说明

mgmtd 同时承受两类并发访问：(1) 大量 RPC（注册节点、心跳、拉路由、改配置……）；(2) 约 10 个后台周期任务。它们都要读写同一份 `MgmtdState.data_`。3FS 的解法是**双层锁**：

| 层 | 锁 | 类型 | 粒度 | 作用 |
| --- | --- | --- | --- | --- |
| 外层 | `writerMu_`（`folly::coro::Mutex`） | 互斥锁 | 粗，按「写操作」串行 | 保护整个「读—改—写」流程的逻辑原子性 |
| 内层 | `data_`（`CoroSynchronized<MgmtdData>`） | 读写锁 | 细，按「数据结构访问」 | 保护内存数据结构的一致性 |

初学者常问：既然有了内层读写锁，为什么还要外层互斥锁？关键在于 mgmtd 的写操作是**跨 FDB 事务的读—改—写**：

```text
写操作典型流程（以 RegisterNode 为例）：
  1. 读：查 nodeMap 里 nodeId 是否已存在        ← 需要 data_ 共享/写锁
  2. 改：写 FoundationDB（慢，毫秒~几十毫秒）   ← 期间会释放 data_ 锁，否则阻塞所有读者
  3. 写：更新内存 nodeMap                       ← 需要 data_ 写锁
```

如果**只**用内层锁：两个并发 `RegisterNode` 可能都读到「nodeId 不存在」→ 都写 FDB → 都插内存，产生重复节点。

如果**全程**持内层写锁：FDB 写事务期间锁不释放，会阻塞所有 `GetRoutingInfo`、心跳等读者，吞吐崩塌。

外层 `writerMu_` 的妙处在于：它**只挡其他写者**（写操作相对稀疏），**不挡读者**；并且在慢 FDB I/O 期间也持有，从而把「读检查 + 写」在逻辑上缝合成原子操作。这就是「逻辑锁管操作、物理锁管数据」的经典分层。

#### 4.3.2 核心流程

只读路径（如 `GetRoutingInfo`）：

```text
doAsPrimary(state, handler):           ← 先确认自己是 primary，否则拒绝
  ensureSelfIsPrimary()
  handler():
    dataPtr = co_await state.data_.coSharedLock()   ← 只取共享读锁
    dataPtr->checkRoutingInfoVersion(...)
    dataPtr->getRoutingInfo(...)                     ← 直接读，无外层锁
```

写路径（如 `RegisterNode`）：

```text
doAsPrimary(state, handler):
  ensureSelfIsPrimary()
  handler():
    writerLock = co_await state.coScopedLock<"RegisterNode">()   ← 外层逻辑写锁（贯穿全程）
    { dataPtr = co_await state.data_.coSharedLock()              ← 短暂读，检查 nodeId 是否存在
      if exists: error }
    updateStoredRoutingInfo(...):                                 ← 写 FDB（期间 data_ 锁已释放）
      withReadWriteTxn → store_.storeNodeInfo(txn, ...)
    updateMemoryRoutingInfo(...):                                 ← 更新内存
      dataPtr = co_await state.data_.coLock()                    ← 取内层写锁，插入 nodeMap
```

注意 `updateMemoryRoutingInfo`（[helpers.h:33-39](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/helpers.h#L33-L39)）取的是 `coLock()`（写锁），但它之所以敢在 `writerMu_` 保护下「先 coSharedLock 检查、后 coLock 改」，正是因为外层锁保证了没有别的写者插队。

此外还有一个**第三个、独立的锁**——`routingInfoCache`：它是 `folly::Synchronized<std::optional<flat::RoutingInfo>>`（[MgmtdData.h:31](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdData.h#L31)），一把**普通同步互斥锁**（`rlock()`/`wlock()`），专用于缓存序列化好的 `flat::RoutingInfo`，避免每次 `GetRoutingInfo` 都重新投影+序列化。它和协程锁无关，因为序列化操作很快、不值得挂起协程。

#### 4.3.3 源码精读

**协程读写锁工具 `CoroSynchronized`** 是内层锁的实现：

[CoroSynchronized.h:32-55](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/CoroSynchronized.h#L32-L55) 把一把 `folly::coro::SharedMutex` 和被保护对象 `obj_` 绑在一起：`coSharedLock()` 返回 `CoSharedLockGuard<T>`（持共享锁，可多人并发读，`operator->` 返回 `const T*`），`coLock()` 返回 `CoLockGuard<T>`（持独占锁，`operator->` 返回 `T*`）。两者都是 `CoTask`，必须 `co_await`——挂起协程而非阻塞线程。

**外层逻辑写锁与耗时统计**定义在 `MgmtdState`：

[MgmtdState.h:40-58](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdState.h#L40-L58) 给出 `WriterMutexGuard`（RAII 守卫，析构时记录该写操作的耗时到监控）和 `coScopedLock<method>()` 模板——注意它接收一个**编译期字符串**作为 `method` 名（如 `"RegisterNode"`），既用作日志/监控标签，也体现了「每个写操作类型各自加锁」的设计。

两个被保护对象的声明：

[MgmtdState.h:60-66](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdState.h#L60-L66)：`data_`（`CoroSynchronized<MgmtdData>`，主数据）和 `clientSessionMap_`（`CoroSynchronized<ClientSessionMap>`，客户端会话）各用一把独立读写锁；外层 `writerMu_` 是 `folly::coro::Mutex`（纯互斥）。注释明确说明 `writerMu_` 的用途：**「logical lock for protecting the whole processing of a writer operation during which read-modify-write will be performed on `data_`」**。

**只读路径实证**——`GetRoutingInfoOperation::handle` 全程不碰 `writerMu_`：

[GetRoutingInfoOperation.cc:6-21](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/GetRoutingInfoOperation.cc#L6-L21) 第 10 行 `co_await state.data_.coSharedLock()` 只取共享读锁，外层用 `doAsPrimary` 确认主身份。这意味着并发的多个 `GetRoutingInfo` 可以同时进行，互不阻塞。

**写路径实证**——`RegisterNodeOperation::handle` 双锁齐全：

[RegisterNodeOperation.cc:13-43](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/RegisterNodeOperation.cc#L13-L43) 清晰展示了 4.3.2 的三段式：第 18 行先取外层 `coScopedLock<"RegisterNode">()`；第 20-24 行短暂 `coSharedLock` 检查重复；第 30-34 行 `updateStoredRoutingInfo` 写 FDB；第 36-40 行 `updateMemoryRoutingInfo` 更新内存。注意 `writerLock` 的生命周期贯穿到 `co_return`，覆盖了中间的 FDB I/O。

**辅助函数**把「先写 FDB、再写内存 + 推高版本」封装好：

- [helpers.h:72-81](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/helpers.h#L72-L81) `updateStoredRoutingInfo`：取共享锁读出当前版本号、算出 `nextVersion`，再开一个读写事务把 `nextVersion` 和具体变更一起原子写进 FDB。
- [helpers.h:33-39](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/helpers.h#L33-L39) `updateMemoryRoutingInfo`：取 `coLock()`（写锁）更新内存并标记 `routingInfoChanged`。

#### 4.3.4 代码实践

**实践目标**：通过「删锁假设法」理解双层锁各自不可或缺的作用。

**操作步骤**：

1. 重读 [RegisterNodeOperation.cc:13-43](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/RegisterNodeOperation.cc#L13-L43)，对照 4.3.2 的三段式流程。
2. 做两个思想实验：
   - **实验 A**：假设删掉第 18 行的 `coScopedLock`，仅保留 `data_` 的锁。两个并发 `RegisterNode(nodeId=5)` 同时执行，各自会读到什么？最终内存与 FDB 会是什么结果？
   - **实验 B**：假设把第 20 行的 `coSharedLock` 改成在第 18 行就取 `data_.coLock()` 并一直持有到 `co_return`（即用内层写锁替代外层逻辑锁）。这会对并发的 `GetRoutingInfo` 造成什么影响？
3. 把结论写成两三句话。

**需要观察的现象**：

- 实验 A：两个请求都读到「nodeId=5 不存在」，都写 FDB、都插内存 → 产生重复节点（数据不一致）。
- 实验 B：由于 `coLock()` 是写锁，与所有 `coSharedLock()` 互斥，`RegisterNode` 期间 FDB 慢 I/O 时整段持有写锁，所有 `GetRoutingInfo` / 心跳读路径全部被挂起 → 吞吐骤降。

**预期结果**：得出结论「外层逻辑锁保证写操作的逻辑原子性（防重复），内层读写锁保证数据结构一致性且不牺牲读吞吐，二者职责正交、缺一不可」。这是源码阅读型实践，不需要编译运行；若想在真实集群观察写操作耗时，可参考 `WriterMutexGuard` 记录的 `MgmtdService.WriterLatency` 监控指标，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`CoroSynchronized::coSharedLock()` 和 `coLock()` 返回的 guard，`operator->` 分别返回 `const T*` 还是 `T*`？为什么这样设计？

**答案**：`coSharedLock()` 返回 `CoSharedLockGuard<T>`，`operator->` 返回 `const T*`（只读）；`coLock()` 返回 `CoLockGuard<T>`，`operator->` 返回 `T*`（可写）。这是用 C++ 类型系统在编译期保证「拿共享锁就只能读、拿写锁才能改」，防止误用——你拿到的锁权限和引用的可变性是绑定的。

**练习 2**：为什么 `routingInfoCache` 用 `folly::Synchronized`（同步锁）而不是 `CoroSynchronized`（协程锁）？

**答案**：`routingInfoCache` 缓存的是已经序列化好的 `flat::RoutingInfo`，读写它的操作（拷贝/比较/赋值一个 optional）非常快，不值得为了它挂起协程、付出协程调度的开销；用普通同步锁 `rlock()/wlock()` 直接阻塞极短时间即可。协程锁主要用在「持锁期间可能 `co_await` 慢 I/O（如 FDB）」的场景。

## 5. 综合实践

把三个最小模块串起来：**完整追踪一次「storage 节点注册」请求，画出它穿越的分层、触及的数据实体、以及加锁时间线。**

具体任务：

1. **分层穿透**：从 `MgmtdService::RegisterNode`（RPC 入口）出发，经 `MgmtdOperator`，到达 `RegisterNodeOperation::handle`，标注每一层所在文件与本讲 4.1 的「四层洋葱」对应关系。
2. **数据实体**：这次注册会向 `RoutingInfo` 的哪个容器（`nodeMap`）插入新条目？引用了哪些字段？如果该 storage 节点后续上报 target，又会触及哪些容器（`chains` / `targets` / orphan 表）？结合 4.2 的 ER 图回答。
3. **加锁时间线**：画一条时间轴，标出 `writerMu_`、`data_` 共享锁、`data_` 写锁各自从哪一行获取、在哪一行释放，并指出 FDB 写事务发生在「哪两把锁之间」。结合 4.3 的源码行号。
4. **对比只读**：对照 `GetRoutingInfoOperation`，说明它为什么不需要 `writerMu_`、为什么不会阻塞其他读者。

**产出**：一张图（分层 + ER + 时间线三合一）+ 一段 150 字说明「双层锁如何让 mgmtd 在高并发下既不丢一致性又不牺牲读吞吐」。若手头有部署好的集群，可用 `admin_cli register-node`（或观察真实 storage 起来时的注册）配合 `MgmtdService.WriterLatency` 指标佐证；否则标注「待本地验证」。

## 6. 本讲小结

- mgmtd 是一条清晰的四层洋葱：`main`（两阶段骨架）→ `MgmtdServer`（网络服务/端口/生命周期）→ `MgmtdOperator`（RPC 分发 + 后台任务）→ `MgmtdState`/`MgmtdData`/`RoutingInfo`（共享状态与全局路由视图）。
- 全局路由视图有「工作版 `mgmtd::RoutingInfo`」与「线上版 `flat::RoutingInfo`」两份：前者带临时簿记字段、内部用；后者是投影后的扁平结构、发给下游；`getRoutingInfo` 负责投影。
- 四类实体构成正交两张子图：`ChainTable 1—N Chain 1—N Target`，`Target` 通过 `nodeId` 反查 `Node`；`routingInfoVersion`/`chainVersion`/`chainTableVersion` 三级版本号单调递增，是「按需拉取、过期检测」的基础。
- 并发保护采用「外层逻辑写锁 `writerMu_` + 内层读写锁 `CoroSynchronized<MgmtdData>`」的双层设计：外层串行化整个读—改—写（防重复、允许中途慢 FDB I/O），内层保证内存一致性且让只读路径（`coSharedLock`）互不阻塞。
- 只有 primary mgmtd 服务请求（`doAsPrimary` / `ensureSelfIsPrimary`），非 primary 直接拒绝；这与 [u3-l3](u3-l3-primary-election.md) 将讲的主选举机制衔接。
- `routingInfoCache` 是第三把、独立的同步锁，仅用于缓存序列化结果，与协程双锁体系正交。

## 7. 下一步学习建议

接下来按 mgmtd 单元的依赖关系继续：

1. **[u3-l2 节点注册、心跳与租约续期](u3-l2-registration-heartbeat.md)**：本讲已见 `RegisterNode` 与双层锁，下一讲深入「注册 + 心跳即租约续期 + 超时检测」的完整生命周期，以及心跳如何把 target 的 `localState` 上报进 `RoutingInfo`（即本讲的 `localUpdateTargets`）。
2. **[u3-l4 ChainTable / Chain / Target 数据模型](u3-l4-chain-target-model.md)**：本讲只看了字段定义，下一讲深入链表生成、版本号推进与多链表隔离不同工作负载。
3. **[u3-l5 Target 状态机与故障检测](u3-l5-target-state-machine.md)**：本讲出现的 `publicState`/`localState` 两套枚举将在下一讲组成完整状态转换表，是故障切换的核心。
4. **横向延伸**：想看「下游如何使用 `flat::RoutingInfo`」可跳读 `src/storage/service/TargetMap.h`（storage 侧）与 `src/client/mgmtd/MgmtdClientForClient.h`（client 侧拉路由），把本讲的「生产端」与「消费端」连成闭环。
