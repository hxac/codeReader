# storage 服务总览与启动

## 1. 本讲目标

本讲是「存储服务 storage」单元（u5）的开篇。学完本讲，你应当能够：

- 说清楚 storage 服务**从 `main` 一行代码到全部后台线程跑起来**的启动过程，以及这个过程中按顺序创建了哪些核心对象。
- 理解 storage 服务内部的**四层分层**：`main` → `StorageServer`（网络服务）→ `Components`（对象聚合容器）→ `StorageOperator`（请求处理器）+ 一组后台 Worker。
- 掌握 `StorageOperator` 作为「所有数据面 RPC 的中央处理器」的角色，能追踪一次 `update`（写）请求从网络层进入后被分发的路径。
- 认识 `aio` / `update` / `sync` / `worker` 四个子目录里各类后台 Worker 的分工，理解它们各自守护的是磁盘的哪一项职责。

本讲只做**总览与启动流程**，读路径细节见 u5-l2，CRAQ 链式复制的写路径细节见 u5-l3，链路由与版本校验见 u5-l4，数据恢复见 u5-l5。

---

## 2. 前置知识

在进入 storage 内部之前，你需要先建立下面三块认知（它们已在前序讲义中建立，这里只做最小回顾）：

1. **storage 是数据面的执行者（来自 u1-l4）**。3FS 的设计里，meta 不在数据热路径上：客户端 `open` 时从 meta 取得文件的 `Layout`（含 `chunkSize`、`stripeSize`、chain table、shuffle seed），此后 `read`/`write` 由客户端自行计算 chunk id 与所属链，**直接连 storage**。落到 storage 上的三类核心 RPC 是：
   - 读 → `StorageOperator::batchRead`
   - 写 → `StorageOperator::update`（及历史接口 `write`）
   - 元数据查询/管理 → `queryChunk` / `createTarget` / `syncStart` 等

2. **storage 节点上「target」是数据管理的单位（来自 u3-l1 / u3-l4）**。一个 storage 进程管理本地若干块 SSD，每块 SSD 上可以承载多个 **target**。每条 CRAQ 复制链（chain）由若干个 target 组成，分布在多个节点上。mgmtd 通过 `flat::RoutingInfo` 把「哪条链上有哪些 target、各自在哪个节点、public 状态如何」分发给所有 storage。storage 在内存里维护这份视图，称为 `TargetMap`。

3. **服务都跑在同一套两阶段启动骨架上（来自 u2-l1）**。`main` 一行 `TwoPhaseApplication<StorageServer>().run(argc, argv)` 把生命周期切成「引导（launcher）」与「运行（net::Server）」两段；业务初始化的标准位置是 `beforeStart()` 钩子。本讲你会看到 storage 如何在这个钩子里把整个 `Components` 拉起来。

> 名词速查：**target**（存储目标，一台机器上一块 SSD 上的一段空间，对应一条链上的一个副本位置）、**chunk**（文件被切成等大的数据块，是读写与复制的最小单位）、**CRAQ**（Chain Replication with Apportioned Queries，链式复制，写从链头沿链传播、读可打链上任意副本）、**版本号**（chainVersion / routingInfoVersion，用来拒绝过期请求、判定是否需要重拉路由）。

---

## 3. 本讲源码地图

本讲涉及的文件按职责分为四组：

| 文件 | 作用 |
| --- | --- |
| [src/storage/storage.cpp](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/storage.cpp) | 进程入口 `main`，一行调用两阶段启动骨架。 |
| [src/storage/service/StorageServer.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageServer.h) / [.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageServer.cc) | `StorageServer`，继承 `net::Server` 的网络服务，承载生命周期钩子与 RPC 服务注册。 |
| [src/storage/service/Components.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/Components.h) / [.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/Components.cc) | `Components`，storage 全部运行时对象的聚合容器，定义启动顺序与协程池分流。 |
| [src/storage/service/StorageOperator.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.h) / [.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc) | `StorageOperator`，所有数据面 RPC 的中央处理器。 |
| [src/storage/service/StorageService.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageService.h) | `StorageService`，把网络层 RPC 透传到 `StorageOperator` 的薄壳。 |
| [src/storage/store/StorageTarget.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/StorageTarget.h) / [StorageTargets.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/StorageTargets.h) | 单个 target 与 target 集合的本地管理（含 Rust chunk engine 句柄）。 |
| [src/storage/worker/*.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/worker) | 一组后台 Worker：`AllocateWorker`、`CheckWorker`、`DumpWorker`、`PunchHoleWorker`、`SyncMetaKvWorker`，以及 `sync/ResyncWorker`、`aio/AioReadWorker`、`update/UpdateWorker`。 |

---

## 4. 核心概念与源码讲解

### 4.1 服务分层：从 main 到 Components 的四层洋葱

#### 4.1.1 概念说明

storage 服务是 3FS 里**最「重」的进程**：它既要扛网络 RPC，又要直接驱动本地 SSD，还要在后台做空间分配、垃圾回收、数据恢复。把这么多职责塞进一个进程，3FS 的做法是把它们组织成**四层洋葱**：

```
main（进程入口）
 └─ StorageServer : public net::Server（网络服务 + 生命周期钩子）
     └─ Components（运行时对象聚合容器，全部成员都在这里）
         ├─ StorageOperator（请求处理器：batchRead / update / ...）
         ├─ StorageTargets（本地 target 集合 + Rust chunk engine）
         ├─ TargetMap（mgmtd 路由视图的本地缓存）
         ├─ BufferPool / AioReadWorker（读路径的 RDMA buffer 与异步 IO）
         ├─ ReliableForwarding / ReliableUpdate（CRAQ 链式转发）
         ├─ UpdateWorker（写路径的落盘执行器）
         └─ 一组后台 Worker（Allocate/Check/Dump/PunchHole/SyncMetaKv/Resync）
```

关键设计是：`StorageServer` 只负责「把网络服务跑起来 + 在合适的钩子里启动/停止 `Components`」；真正干活的全部对象都挂在 `Components` 这一个 `struct` 上。这样做的好处是——任何一个模块（比如 `StorageOperator`）需要用到别的模块（比如 `TargetMap`、`BufferPool`），都通过持有 `Components &` 这一个引用就能拿到，**避免对象之间互相持有指针形成网状依赖**。

#### 4.1.2 核心流程：分层启动顺序

`main` 触发两阶段启动骨架后，storage 的业务启动严格发生在 `beforeStart()` 钩子里，按下面顺序推进：

1. **注册 RPC 服务**：`StorageServer::beforeStart` 先把两个 `serde::Service` 挂到网络层——业务服务 `StorageService`（走 RDMA）和控制面服务 `CoreService`（心跳/配置，见 u2-l1）。
2. **设置协程池分流**：给业务服务组挂一个 `setCoroutinesPoolGetter`，按 RPC 的 `methodId` 把请求分流到读/写/同步/默认四个协程池。
3. **启动 Components**：调用 `components_.start(appInfo, tpg)`，这一步会**按固定顺序**把 RDMA buffer 池、4 个协程池、messenger、本地 target、AIO 读 worker、各后台 worker、路由信息等待、`StorageOperator` 依次拉起。

停止是对称的：`beforeStop` 先停下转发链路（`ReliableUpdate`/`ReliableForwarding`），`afterStop` 再 `stopAndJoin` 全部 worker 并释放 target。

#### 4.1.3 源码精读

进程入口只有一行——`StorageServer` 是模板参数，骨架（`TwoPhaseApplication`）来自 u2-l1：

[src/storage/storage.cpp:5-7](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/storage.cpp#L5-L7) —— `main` 仅触发两阶段启动骨架，真正的初始化全在 `StorageServer` 的钩子里。

`StorageServer` 声明自己是 `net::Server` 子类，并用常量声明节点类型为 `STORAGE`（这决定了它从 mgmtd 拉取哪一份运行时配置模板）：

[src/storage/service/StorageServer.h:22-32](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageServer.h#L22-L32) —— `class StorageServer : public net::Server`，`kNodeType = flat::NodeType::STORAGE`，并把 `Launcher`/`RemoteConfigFetcher` 类型别名交给骨架使用。

钩子只有三个，对应 u2-l1 讲过的生命周期点：

[src/storage/service/StorageServer.h:40-46](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageServer.h#L40-L46) —— `beforeStart`（建服务、启 Components）、`beforeStop`（停转发）、`afterStop`（停 worker + 释放 target）。

`beforeStart` 的全部业务：注册两个 RPC 服务，挂上协程池分流器，然后启动 `Components`：

[src/storage/service/StorageServer.cc:26-39](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageServer.cc#L26-L39) —— `addSerdeService(StorageService)` + `addSerdeService(CoreService)`，`setCoroutinesPoolGetter` 按 `serviceId`/`methodId` 选协程池，最后 `components_.start`。

> 这里有个值得记住的细节：storage 的网络层配了**两个 ServiceGroup**——第 0 组走 RDMA 承载 `StorageSerde`（数据面，8000 端口），第 1 组走 TCP、用独立线程池承载 `Core`（控制面，9000 端口）。这与 u3-l1 里 mgmtd「业务走 RDMA、Core 走 TCP」的思路完全一致：**即使 RDMA/IB 不可用，控制面（心跳、配置拉取）仍能经 TCP 到达**，保证节点不会被误判失联。见 [src/storage/service/Components.h:30-42](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/Components.h#L30-L42)，其中 `groups(0)` 是 RDMA+StorageSerde、`groups(1)` 是 TCP+Core，并设了 32 个 IO 线程、32 个 proc 线程。

`Components` 构造函数按**声明顺序**把全部成员初始化好（注意成员在 `.h` 里的声明顺序就是构造顺序）：

[src/storage/service/Components.cc:19-37](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/Components.cc#L19-L37) —— 构造 `rdmabufPool` → `storageTargets`（绑 `targetMap`）→ `aioReadWorker` → `messenger` → 各 worker → 协程池 → `storageOperator` → `reliableUpdate`。注意 `storageTargets` 构造时就把 `targetMap` 的引用传进去，二者是绑定的。

`Components::start` 是启动顺序的**权威定义**，建议逐行读一遍并记住这个顺序——后面的实践任务就建立在它之上：

[src/storage/service/Components.cc:39-69](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/Components.cc#L39-L69) —— 依次启动 `rdmabufPool` → 4 个协程池 → `messenger` → `reliableForwarding` → `storageTargets.load`（从磁盘加载 target）→ `aioReadWorker` → `dumpWorker` → `allocateWorker` → `punchHoleWorker` → `syncMetaKvWorker` → `waitRoutingInfo` → `resyncWorker` → `checkWorker` → `storageOperator.init`。

注意 `waitRoutingInfo` 排在 `resyncWorker`/`checkWorker`/`storageOperator` **之前**：服务必须先确认 mgmtd 已经「看见」自己上报的 target（且它们在路由视图里处于 OFFLINE），才能开始数据恢复和对外服务。`waitRoutingInfo` 里那个循环会一直等，直到自己名下的 target 在路由视图里都不再是 `SERVING/SYNCING/WAITING`（见 [src/storage/service/Components.cc:109-122](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/Components.cc#L109-L122)）——这一步与 u3-l5 的 target 状态机衔接：storage 先以 OFFLINE 入册，再通过后续心跳把 `localState` 翻成 `UPTODATE`，由 mgmtd 决定何时进入 `SYNCING`/`SERVING`。

#### 4.1.4 代码实践

**实践目标**：把 storage 的启动顺序画成一张时序/对象图，建立「谁先谁后、谁依赖谁」的全局观。

**操作步骤**：

1. 打开 [src/storage/service/Components.cc:39-69](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/Components.cc#L39-L69)，按行号抄下 `Components::start` 的启动顺序。
2. 对每一项，回答两个问题：（a）它依赖前面哪些对象？（b）为什么它必须排在那个位置？例如 `aioReadWorker.start` 的入参是 `storageTargets.fds()` 和 `rdmabufPool.iovecs()`（[L52-54](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/Components.cc#L52-L54)），所以它必然排在 `storageTargets.load` 与 `rdmabufPool.init` 之后。
3. 把停止顺序（[Components.cc:141-206](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/Components.cc#L141-L206)）与启动顺序并排对比，找出哪些是**逆序**、哪些不是。

**需要观察的现象**：停止顺序大致是启动顺序的逆序，但 `rdmabufPool` 的清理（`clear`）排在最后（[L199](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/Components.cc#L199)），因为释放 target 时仍可能用到 buffer。

**预期结果**：你得到一张「启动自上而下、停止自下而上」的对象生命周期图。**待本地验证**：若有测试集群，可在启动日志里按 `Start rdmabufPool` / `Start storageTargets` / `Start aioReadWorker` 等 `LOG` 行核对顺序。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `storageTargets.load` 要排在 `aioReadWorker.start` 之前？
**答案**：因为 `aioReadWorker.start` 需要 `storageTargets.fds()`（已打开的数据文件描述符）和 `rdmabufPool.iovecs()` 两项输入（[Components.cc:52-54](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/Components.cc#L52-L54)）。target 没加载，就没有 fd 可提交给 AIO。

**练习 2**：`StorageServer` 把 `components_` 作为成员直接持有（[StorageServer.h:56](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageServer.h#L56)），而不是用指针。这样做有什么好处和代价？
**答案**：好处是生命周期被 `StorageServer` 直接管辖，构造即建好、析构即回收，无需手动管理；代价是 `Components` 必须在 `StorageServer` 构造时就完整建好（它的配置来自 `Components::Config`），不能延后创建——但 storage 的需求正是「配置确定后一把全拉起」，所以这种强耦合是合理的。

---

### 4.2 请求处理器：StorageService → StorageOperator

#### 4.2.1 概念说明

`StorageOperator` 是 storage 进程里**所有数据面 RPC 的中央处理器**。无论客户端要读、要写、要查 chunk、要建 target，网络层最终都会把请求交给它。

为什么需要这一层抽象？因为一个数据面 RPC 的处理往往要**协调多个子系统**：

- 一次写（`update`）要先查 `TargetMap` 确认本节点在链中的角色（链头/中间/链尾），可能要经 `ReliableForwarding` 沿 CRAQ 链转发给后继，最终由 `UpdateWorker` 把数据落盘到对应 `StorageTarget`，期间还要从 `BufferPool` 领 RDMA buffer、用信号量限流并发 RDMA 写。
- 一次读（`batchRead`）要按链路由到任意 target、从 `BufferPool` 分配 buffer、经 `AioReadWorker` 异步从 SSD 读出、再经 RDMA 回传。

`StorageOperator` 就是把这些子系统串起来的「总指挥」，而它和具体业务（CRAQ 协议、落盘细节）之间还隔着一层 `StorageService`。

#### 4.2.2 核心流程：一次 update 请求的旅程

一个写请求（`update`）从网络到达 storage 进程后，依次经过：

```
IBSocket 收到 RPC（RDMA）
  → net::Server 的 Processor 协程按 methodId 分流到 updatePool 协程池
    → StorageService::update（薄壳：记录队列时延，构造请求上下文）
      → StorageOperator::update
        ├─ targetMap.getByChainId(vChainId)  // 解析本节点对应的 target，含版本校验
        └─ reliableUpdate.update(...)         // 进入 CRAQ 可靠写流程
              └─ handleUpdate(...)
                   ├─ 校验：非链头节点拒绝客户端直写、read_only 拒绝写
                   ├─ storageTarget->lockChunk(chunkId)  // 链头对 chunk 加锁，串行化
                   ├─ doUpdate(...)   // 领 buffer、从 RDMA 拉数据、写入 chunk engine
                   └─ ReliableForwarding 转发给后继，收齐 ACK
```

这里有几个贯穿后续讲义的关键点，先建立直觉即可：

- **版本校验**：`getByChainId(vChainId)` 用请求携带的 `chainVersion` 比对本地视图，**过期的写请求会被直接拒绝**（详见 u5-l4）。
- **链头加锁**：写请求只能在链头处理，`handleUpdate` 一上来就检查 `req.options.fromClient && !target->isHead` 拒绝非链头直写（见下文源码），再对 chunk 加锁保证链内串行化（CRAQ 的核心，详见 u5-l3）。
- **协程池分流**：读和写分别进不同的协程池（`readPool` / `updatePool`），互不阻塞——这是 u2-l3 讲过的「4 个隔离协程池按 RPC methodId 分流读写流量」在 storage 的具体落地。

#### 4.2.3 源码精读

先看 `StorageService` 这个**薄壳**——它对每个 RPC 几乎只做两件事：记一下队列时延，再把请求转给 `StorageOperator`：

[src/storage/service/StorageService.h:14-31](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageService.h#L14-L31) —— `batchRead` / `write` / `update` 都是先 `reportXxxQueueLatency(ctx)` 再 `co_await storageOperator_.xxx(...)`。注意 `update` 把 `ctx.transport()->ibSocket()` 一起传下去——因为写数据要走 RDMA 零拷贝，需要底层 socket 句柄。

`StorageOperator` 暴露的全部数据面方法都在头文件里一目了然：

[src/storage/service/StorageOperator.h:70-98](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.h#L70-L98) —— `batchRead`/`write`/`update`/`queryLastChunk`/`truncateChunks`/`removeChunks`/`syncStart`/`syncDone`/`spaceInfo`/`createTarget`/`offlineTarget`/`removeTarget`/`queryChunk`/`getAllChunkMetadata`。注意 `write` 与 `update` 的入参都带 `net::IBSocket *`，而只读类方法不带。

`update` 的入口逻辑很薄：查 target → 决定走 `handleUpdate`（REMOVE 特例）还是 `reliableUpdate.update`（正常 CRAQ 写）：

[src/storage/service/StorageOperator.cc:284-331](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L284-L331) —— 先 `targetMap.getByChainId(req.payload.key.vChainId)` 拿到本节点的 target；若 `updateType == REMOVE` 且无 channel 则直接 `handleUpdate`，否则交给 `reliableUpdate.update`。

`handleUpdate` 开头的几道校验，恰好揭示了 CRAQ 写路径的几条硬规则：

[src/storage/service/StorageOperator.cc:333-356](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L333-L356) —— （1）非链头节点收到客户端写直接报错；（2）`read_only` 模式拒绝写；（3）空 chunkId 非法；（4）syncing 期间拒绝 truncate/extend。随后对 chunk 加锁（[L371](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L371)）。

最后是构造函数里对 **RDMA 并发限流**的初始化——`StorageOperator` 为每块 IB 设备各建一对信号量，限制并发 RDMA 读/写数：

[src/storage/service/StorageOperator.h:46-64](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.h#L46-L64) —— 遍历所有 IB 设备，为每个 `ibdev->id()` 各建 `concurrentRdmaWriteSemaphore_` / `concurrentRdmaReadSemaphore_`，初始令牌数来自 `max_concurrent_rdma_writes`(256)/`max_concurrent_rdma_reads`(256)，并且挂了热更新回调——改配置后令牌数会实时调整。

#### 4.2.4 代码实践

**实践目标**：追踪一次 `update` 请求在 storage 进程内的方法调用链，标注每一跳经过了哪个对象。

**操作步骤**：

1. 从 [StorageService.h:27-31](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageService.h#L27-L31) 的 `update` 出发。
2. 跳到 [StorageOperator.cc:284-331](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L284-L331)，列出它调用了 `components_` 的哪些成员（至少 `targetMap`、`reliableUpdate`）。
3. 进入 `handleUpdate`（[L333-](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.cc#L333)），记录它调用了 `targetMap.getByChainId`、`target->storageTarget->lockChunk`、`components_.rdmabufPool.get`、`doUpdate` 等。
4. 用箭头画出 `StorageService → StorageOperator → TargetMap / ReliableUpdate → StorageTarget / BufferPool / UpdateWorker` 的调用图。

**需要观察的现象**：`StorageOperator` 几乎不直接碰磁盘，它总是经由 `StorageTarget`（落盘）或 `UpdateWorker`（排队）或 `BufferPool`（buffer）来间接操作；它本身是**协调者**而非执行者。

**预期结果**：得到一张以 `StorageOperator` 为中心、向外辐射到 `TargetMap`/`ReliableUpdate`/`ReliableForwarding`/`BufferPool`/`StorageTarget` 的调用图。CRAQ 沿链转发的细节（`ReliableForwarding`/`ReliableUpdate` 的可靠重试与幂等）留给 u5-l3 精读。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `write`/`update` 的签名带 `net::IBSocket *`，而 `batchRead` 不带？
**答案**：写数据量通常很大，3FS 用 RDMA Write/Read 把数据**零拷贝**搬进/搬出对端内存，这需要底层 `IBSocket` 句柄来发起 RDMA 操作（u2-l4 讲过 `IBSocket` 与 `RDMARemoteBuf`）。`batchRead` 虽然也用 RDMA 回传，但其回传 buffer 由 `BufferPool` 统一管理，调用链里另取 buffer，故入口签名不直接暴露 socket。

**练习 2**：`getCoroutinesPool(methodId)` 如何把不同 RPC 分到不同协程池？如果 `use_coroutines_pool_update` 被热更新成 `false` 会怎样？
**答案**：见 [Components.h:80-92](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/Components.h#L80-L92)：`batchRead` 走 `readPool`，`write`/`update` 走 `updatePool`，`syncStart`/`getAllChunkMetadata` 走 `syncPool`，其余走 `defaultPool`。若 `use_coroutines_pool_update` 置 `false`，`update` 请求会落到 `defaultPool`——这是一个**降级开关**，用于在写协程池出问题时把写流量并回默认池排查（u2-l3 讲过协程池可热更新）。

---

### 4.3 后台 Worker：磁盘管家与数据恢复

#### 4.3.1 概念说明

storage 进程除了被动响应 RPC，还有一整套**后台 Worker**主动维护本地 SSD 的健康。可以按职责把它们分成三类：

- **空间与回收类**：`AllocateWorker`（预分配物理文件组）、`PunchHoleWorker`（回收已删 chunk 的空间，打洞）、`CheckWorker`（巡检磁盘空间、可写性、触发紧急回收）、`SyncMetaKvWorker`（周期把 chunk 元数据落盘 RocksDB）。
- **诊断类**：`DumpWorker`（周期 dump chunk 元数据快照、CPU 高时启动 profiler）。
- **数据恢复类**：`ResyncWorker`（target 重启后从链上其他副本拉数据补齐，即 u5-l5 的恢复主力）。

这些 Worker 的共同特征是：每个都是「一个 `folly::CPUThreadPoolExecutor` + 一个 `loop()` 循环 + `start()/stopAndJoin()`」的模板，靠 `std::atomic<bool> stopping_` 与 `condition_variable` 协作优雅停止（与 u2-l3 的 `BackgroundRunner` 思路相通，但这里是更轻量的自循环实现）。

#### 4.3.2 核心流程：Worker 如何与请求路径协作

以**写**为例看后台 Worker 与请求路径的关系：

- 一次 `update` 写入新数据时，`StorageTarget` 会从预分配好的物理文件里切一块给这个 chunk。物理文件不是临时分配的，而是 `AllocateWorker` **提前批量 `fallocate` 好的一组「物理块组」**——它维持 `min_remain_groups`～`max_remain_groups` 个备用组，写请求只管「领用」，避免每次写都陷入系统调用。
- chunk 被删后，空间不会立刻归还给文件系统，而是先标记；`PunchHoleWorker` 周期性地对 `StorageTarget` 调 `punchHole()`（`fallocate(FALLOC_FL_PUNCH_HOLE)`）真正打洞回收。
- `CheckWorker` 每 `update_target_size_interval`（默认 10s）扫一遍磁盘水位：到 `disk_low_space_threshold`(0.96) 触发紧急回收，到 `disk_reject_create_chunk_threshold`(0.98) 直接拒绝建 chunk。
- `SyncMetaKvWorker` 每 `sync_meta_kv_interval`（默认 1min）把 chunk engine 的内存元数据刷盘，缩短崩溃恢复时间。

数据恢复路径（`ResyncWorker`）则在 target 从 offline 重启后启动，它周期扫描本地哪些链还「缺数据」，通过 `handleSync` 从链上其他副本拉取（详见 u5-l5）。

#### 4.3.3 源码精读

`Components` 把全部后台 Worker 作为成员集中声明，一眼可见 storage 的「全家福」：

[src/storage/service/Components.h:106-121](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/Components.h#L106-L121) —— `aioReadWorker`/`messenger`/`resyncWorker`/`checkWorker`/`dumpWorker`/`allocateWorker`/`punchHoleWorker`/`syncMetaKvWorker`/`reliableForwarding`/4 个协程池/`storageOperator`/`reliableUpdate` 全在这里。

各 Worker 的配置项直接揭示了它们的工作节奏与触发阈值：

[src/storage/worker/AllocateWorker.h:14-23](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/worker/AllocateWorker.h#L14-L23) —— 维持 `min_remain_groups`(4)～`max_remain_groups`(8) 个备用物理文件组、`max_reserved_chunks`(1GB)，以及针对大于 4MiB「超大块」的独立水位 `min/max_remain_ultra_groups`。

[src/storage/worker/CheckWorker.h:17-22](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/worker/CheckWorker.h#L17-L22) —— `update_target_size_interval`(10s) 巡检间隔、`emergency_recycling_ratio`(0.95) 紧急回收阈值、`disk_low_space_threshold`(0.96) 低水位、`disk_reject_create_chunk_threshold`(0.98) 拒建阈值。

[src/storage/worker/DumpWorker.h:17-21](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/worker/DumpWorker.h#L17-L21) —— `dump_interval`(1 天) 周期 dump、`high_cpu_usage_threshold`(100 核) 触发 profiler。它有 `dump` 与 `profilerStart` 两个动作（[L34-36](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/worker/DumpWorker.h#L34-L36)）。

[src/storage/worker/SyncMetaKvWorker.h:16-18](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/worker/SyncMetaKvWorker.h#L16-L18) —— `sync_meta_kv_interval`(1min) 刷元数据，对应 `syncAllMetaKVs()`（[L29](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/worker/SyncMetaKvWorker.h#L29)）。

[src/storage/worker/PunchHoleWorker.h:14-26](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/worker/PunchHoleWorker.h#L14-L26) —— 「recycle worker」，`loop()` 周期对 target 调 `punchHole()`（对应 [StorageTarget.h:84-90](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/StorageTarget.h#L84-L90)）。

[src/storage/sync/ResyncWorker.h:22-58](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/sync/ResyncWorker.h#L22-L58) —— 数据恢复主力：`handleSync(VersionedChainId)` 逐链补数据、`forward(...)` 把单个 chunk 的更新转发到本节点落盘；支持 `full_sync_chains`/`full_sync_level` 控制全量回放范围（u5-l5 精读）。

落到磁盘的最终执行者 `StorageTarget`，是「target」这一数据单位在单机上的化身，它的方法清单正好覆盖了读写回收四类操作：

[src/storage/store/StorageTarget.h:33-75](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/StorageTarget.h#L33-L75) —— `create`/`load`（建/加载 target）、`aioPrepareRead`/`aioFinishRead`（异步读）、`updateChunk`（写/删/截断的落盘）、`queryChunks`/`queryChunk`（元数据查询）、`punchHole`/`sync`（回收与刷盘）。

注意 `StorageTarget` 内部对每条操作都**分叉**为「chunk engine（Rust）」与「旧 ChunkStore（C++）」两条实现，由 `useChunkEngine()` 切换：

[src/storage/store/StorageTarget.h:84-99](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/StorageTarget.h#L84-L99) 与 [L162](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/StorageTarget.h#L162) —— `punchHole`/`sync`/`usedSize`/`uncommitted` 等都先判断 `useChunkEngine()`，是则走 Rust `chunk_engine::Engine`（经 FFI，见 u6 单元），否则走 C++ `chunkStore_`。`targetConfig_.only_chunk_engine` 决定走哪条路。生产部署默认用 Rust chunk engine。

而 `StorageTargets`（复数）则是「一块 SSD 上多个 target + 多个 Rust engine」的集合管理者：

[src/storage/store/StorageTargets.h:21-31](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/StorageTargets.h#L21-L31) —— 配置项 `target_paths`（SSD 路径列表）、`target_num_per_path`（每盘 target 数）；[L56](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/StorageTargets.h#L56) 的 `load` 把它们全部打开。成员 `engines_`（[L95](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/StorageTargets.h#L95)）是 `std::vector<rust::Box<chunk_engine::Engine>>`——**每块盘一个 Rust engine**。

#### 4.3.4 代码实践

**实践目标**：把后台 Worker 与「一次写请求的落盘回收」对应起来，理解它们各司其职。

**操作步骤**：

1. 列出 `Components` 里的全部后台 Worker（[Components.h:106-121](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/Components.h#L106-L121)）。
2. 对「写一个新 chunk → 之后删除它」这条时间线，标注每一步用到哪个 Worker：
   - 写新 chunk：从 `AllocateWorker` 预分配的物理文件组领用（`StorageTarget::updateChunk`）。
   - chunk 元数据：由 `SyncMetaKvWorker` 周期刷盘。
   - 删除 chunk：标记后由 `PunchHoleWorker` 周期 `punchHole` 回收。
   - 磁盘水位：`CheckWorker` 巡检，超阈值触发紧急回收或拒建。
3. 回答：如果一块 SSD 水位到达 0.96，哪个 Worker 会最先反应？触发什么动作？（提示：[CheckWorker.h:19-21](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/worker/CheckWorker.h#L19-L21)）

**需要观察的现象**：请求路径（`StorageOperator`）与后台 Worker（`AllocateWorker` 等）**通过 `StorageTarget` 这个共享对象间接协作**——前者调用 `updateChunk`/`aioPrepareRead`，后者调用 `punchHole`/`sync`，双方都作用在同一批本地文件上。

**预期结果**：你能说清楚「写请求只管领用预分配空间、回收交给后台」这一分工，以及 `CheckWorker` 的三档水位阈值各自触发的后果。**待本地验证**：可在监控里观察 `storage.target_state`（[Components.cc:15](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/Components.cc#L15)）与磁盘水位随写入的变化。

#### 4.3.5 小练习与答案

**练习 1**：`AllocateWorker` 为什么要预先 `fallocate` 一批物理文件组、维持 4～8 个备用，而不是写请求来了再当场分配？
**答案**：避免每次写都陷入 `fallocate` 系统调用与磁盘分配延迟，把「建文件」这种慢操作摊到后台异步做，让写请求只需「领用」现成的物理块，从而保证写路径的低延迟与高吞吐。`min/max_remain_groups` 是水位上下限：低于 `min` 就补、高于 `max` 就停，避免占用过多预留空间。

**练习 2**：`StorageTarget` 的多数方法都有 `if (useChunkEngine()) { ... } else { chunkStore_... }` 两条分支。这种「双实现」带来什么好处？
**答案**：允许同一套上层逻辑（`StorageOperator`/后台 Worker）在 Rust chunk engine（新、高性能，u6 精读）与旧 C++ `ChunkStore`（兼容/对照）之间切换，由 `only_chunk_engine` 配置决定。好处是可以平滑迁移、对照测试、在 chunk engine 出问题时回退。代价是每处都要写两条分支。

---

## 5. 综合实践

**任务**：把本讲三个最小模块串起来——画出「storage 服务从启动到处理一次写请求」的完整对象协作图。

**要求**：

1. **启动段**：照 [Components.cc:39-69](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/Components.cc#L39-L69) 的顺序，画出 `rdmabufPool → 4 协程池 → messenger → reliableForwarding → storageTargets.load → aioReadWorker → 5 个后台 Worker → waitRoutingInfo → resyncWorker → checkWorker → storageOperator.init` 的启动时序，并在每个对象旁标注它持有的「关键资源」（如 `storageTargets` 持有 fd 列表与 Rust engine、`rdmabufPool` 持有注册过的 RDMA 内存）。

2. **请求段**：画一次 `update` 写请求从 `IBSocket` 进入到落盘的调用链：`net::Server 协程（updatePool）→ StorageService::update → StorageOperator::update → TargetMap.getByChainId → ReliableUpdate → handleUpdate → StorageTarget.lockChunk → doUpdate（领 BufferPool buffer + 写 Rust chunk engine）`，并标注 CRAQ 转发（`ReliableForwarding`）发生在哪一跳（详见 u5-l3）。

3. **后台段**：在同一张图上，用虚线标出后台 Worker 如何异步作用在 `StorageTarget` 上：`AllocateWorker` 预分配、`SyncMetaKvWorker` 刷元数据、`PunchHoleWorker` 回收、`CheckWorker` 巡检水位、`ResyncWorker` 补数据。

**交付**：一张图 + 一段说明，解释「为什么请求路径与后台 Worker 必须共享同一批 `StorageTarget` 对象」。

**自检问题**（能在图上回答即过关）：
- `waitRoutingInfo` 为什么必须排在 `resyncWorker.start` 之前？
- `StorageOperator` 自己直接碰磁盘吗？如果不，它经由谁？
- 一个 chunk 被删后，空间是同步归还还是异步归还？由哪个 Worker 负责？

---

## 6. 本讲小结

- storage 服务是**四层洋葱**：`main` → `StorageServer : public net::Server` → `Components`（全部运行时对象的聚合容器）→ `StorageOperator`（请求处理器）+ 一组后台 Worker；`Components` 用「一个引用拿全部依赖」的方式避免对象网状耦合。
- 启动严格发生在 `StorageServer::beforeStart`：先注册 `StorageService`（RDMA，8000）与 `CoreService`（TCP，9000）两个服务并挂协程池分流器，再按 `Components::start` 的固定顺序逐个拉起对象——`waitRoutingInfo` 排在数据恢复与请求处理之前，确保 mgmtd 先「看见」本节点的 target。
- `StorageOperator` 是所有数据面 RPC 的**中央协调者**：它本身不直接碰磁盘，而是协调 `TargetMap`（路由/版本校验）、`ReliableUpdate`/`ReliableForwarding`（CRAQ 链式写）、`BufferPool`（RDMA buffer）、`StorageTarget`（落盘）、按 IB 设备限流的 RDMA 信号量。
- 读/写流量经 `getCoroutinesPool(methodId)` 分到 `readPool`/`updatePool`/`syncPool`/`defaultPool` 四个隔离协程池，互不阻塞；`use_coroutines_pool_*` 是热更新的降级开关。
- 后台 Worker 分三类：空间回收（`AllocateWorker`/`PunchHoleWorker`/`CheckWorker`/`SyncMetaKvWorker`）、诊断（`DumpWorker`）、数据恢复（`ResyncWorker`）；它们与请求路径通过共享的 `StorageTarget` 间接协作。
- `StorageTarget` 是「target」在单机的化身，多数操作在 Rust `chunk_engine::Engine`（经 FFI）与旧 C++ `ChunkStore` 之间二选一，由 `only_chunk_engine` 切换；每块 SSD 对应一个 Rust engine，由 `StorageTargets` 统管。

---

## 7. 下一步学习建议

本讲建立了 storage 服务的全景与启动地图。建议按真实请求路径继续深入：

1. **u5-l2 读路径**：精读 `batchRead` 如何解析 target、从 `BufferPool` 分配 RDMA buffer、经 `AioReadWorker`/`BatchReadJob` 异步落盘读取并回传。
2. **u5-l3 写路径与 CRAQ**：精读 `ReliableUpdate`/`ReliableForwarding` 的链头加锁、committed/pending 双版本、沿链转发与 ACK 回传，理解 `handleUpdate` 里加锁与 `doUpdate`/`doCommit` 的完整语义。
3. **u5-l4 TargetMap 与链路由**：精读 `getByChainId` 的版本校验如何拒绝过期写请求、如何确定自身链头/链尾身份。
4. **u5-l5 数据恢复**：精读 `ResyncWorker` 如何在 target 重启后做 dump-chunkmeta 对比与 full-chunk-replace 全量回放。
5. **u6 Chunk Engine**：跨过 FFI 边界，进入 Rust chunk engine 的物理块分配与 RocksDB 元数据，理解 `StorageTarget::updateChunk` 背后真正的执行者。
