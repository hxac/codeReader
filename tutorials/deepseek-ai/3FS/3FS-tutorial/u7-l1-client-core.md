# 客户端核心：Meta / Storage / Mgmtd Client

> 本讲属于「客户端」单元的第一篇。前置讲义：u1-l4（端到端请求链路总览）、u2-l4（网络层 TCP/RDMA 传输）。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 3FS 客户端里的 `MgmtdClient`、`MetaClient`、`StorageClient`、`CoreClient` 各自和谁说话、各管什么事，以及它们如何被组装在一起。
- 解释 `StorageClient` 在一次批量读里如何把用户传入的若干 `ReadIO` **拆分 → 选 target → 按节点合并成批次 → 并发发送**，以及为什么这么做。
- 理解读流量在一条 CRAQ 复制链内是如何均衡到多个 target 的（target 选择策略）。
- 掌握客户端的 **RDMA buffer 零拷贝**：为什么用户要事先「注册」一段内存、注册后发生了什么、数据是怎么不经过内核拷贝就回到用户缓冲区的。
- 读懂客户端 `open` 之后发起一次 `read` 的完整路径，并说清楚「偏移量 → chunk id → 所在链 → target」是如何算出来的。

## 2. 前置知识

在进入源码前，先用三段话把背景补齐（细节都来自前面的讲义，这里只做最小回顾）。

**（1）四大组件与「meta 不在数据热路径」。** 3FS 由 mgmtd、meta、storage、client 四大组件构成。`open` 时客户端从 meta 取回 inode 与 **layout（布局）**——里面有 `chunkSize`、`stripeSize`、`chainTableId`、`shuffle seed` 等；此后 `read`/`write` 完全由客户端自己计算数据落在哪条复制链上，**直接连 storage 读写**，meta 不再参与。这就是本讲要展开的「客户端核心」。

**（2）CRAQ 的「写全读任何」。** 一条 chain 上有多个 target（副本）。写必须从链头沿链串行传播到链尾（见 u5-l3）；但**读可以打到链上任意一个 `SERVING` 状态的 target**。这给客户端留下一个问题：一条链上有好几个能读的 target，客户端该选哪一个？这正是本讲「target 选择」要回答的。

**（3）客户端 RPC 的底座。** 客户端发出的每个 RPC，底层都走 u2-l2 讲过的 serde/RPC 框架（`ClientContext` 打包请求、`(serviceId, methodId)` 派发）和 u2-l4 讲过的 `net::Client`/`IBSocket`/`RDMABuf`。本讲只关心「客户端这一层如何用这些底座」，不再重复底座细节。

> 一个贯穿全讲的心智模型：**3FS 的「客户端」不是一个对象，而是一捆子客户端**。FUSE 守护进程或原生应用会同时持有「管路由/配置的 mgmtd 子客户端」「管元数据的 meta 子客户端」「管数据的 storage 子客户端」，三者协作完成一次文件操作。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/client/mgmtd/MgmtdClientForClient.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/MgmtdClientForClient.h) | 面向「客户端进程」的 mgmtd 子客户端，负责自动刷新路由信息、续客户端会话、接收配置热更新。 |
| [src/client/mgmtd/ICommonMgmtdClient.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/ICommonMgmtdClient.h) | 各类 mgmtd 子客户端的公共接口：`getRoutingInfo`、`refreshRoutingInfo`、`addRoutingInfoListener`。 |
| [src/client/mgmtd/RoutingInfo.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/RoutingInfo.h) | 客户端侧的 `RoutingInfo` 包装，提供 `getChain`/`getTarget`/`getNode` 查询。 |
| [src/client/meta/MetaClient.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/meta/MetaClient.h) | meta 子客户端，封装 stat/open/create/close/rename 等元数据 RPC，内含重试与服务器选择。 |
| [src/client/core/CoreClient.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/core/CoreClient.h) | 最底层的 CoreService 子客户端，用一行宏展开所有 RPC 方法。 |
| [src/client/storage/StorageClient.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClient.h) | storage 子客户端的**抽象基类**：定义 `ReadIO`/`WriteIO`/`IOBuffer`/`RoutingTarget`、并发配置与 `batchRead` 等纯虚接口。 |
| [src/client/storage/StorageClient.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClient.cc) | 工厂 `create()`、`createReadIO()`、`registerIOBuffer()` 的实现。 |
| [src/client/storage/StorageClientImpl.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClientImpl.h) | RPC 形态的 storage 客户端：持有 `currentRoutingInfo_`、并发限流器。 |
| [src/client/storage/StorageClientImpl.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClientImpl.cc) | 批量读/写的全部真实逻辑：选 target、按节点合并、并发发送、重试。 |
| [src/client/storage/TargetSelection.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/TargetSelection.h) / [.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/TargetSelection.cc) | target 选择策略（负载均衡/轮询/随机/链头/链尾/手动）。 |
| [src/fbs/meta/Schema.cc](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/meta/Schema.cc) | `getChunkId`/`getChainId`：把文件内偏移换算成 chunk id 与所属链。 |

## 4. 核心概念与源码讲解

本讲拆为四个最小模块：

1. **4.1 三类客户端的分工与组装**（Mgmtd / Meta / Storage / Core）
2. **4.2 StorageClient 的路由信息与 target 选择**（含读流量在链内均衡）
3. **4.3 IO 合并、拆分与批量发送**
4. **4.4 RDMA buffer 零拷贝管理**

### 4.1 三类客户端的分工与组装

#### 4.1.1 概念说明

3FS 的客户端进程（FUSE 守护进程 `hf3fs_fuse`、原生 USRBIO 程序、或 admin_cli）内部都不是「一个 client」，而是一组分工明确的**子客户端**：

| 子客户端 | 对端服务 | 主要职责 |
| --- | --- | --- |
| `MgmtdClientForClient` | mgmtd（控制面） | 拉取并自动刷新 `RoutingInfo`（集群视图）、续客户端会话 `extendClientSession`、接收运行时配置热更新 |
| `MetaClient` | meta（元数据） | `stat`/`open`/`create`/`close`/`rename`/`unlink`/`truncate` 等元数据 RPC，内含重试与 meta 服务器选择 |
| `StorageClient` | storage（数据） | `batchRead`/`batchWrite`/`queryLastChunk`/`removeChunks` 等数据 RPC，处理路由、合并、限流、RDMA buffer |
| `CoreClient` | CoreService（最底层） | 用一行宏展开 CoreService 的全部 RPC，属于最底层的通用调用入口 |

它们的组装关系是**单向依赖**：

- mgmtd 子客户端最先被创建（它只需要 mgmtd 的引导地址）。
- `StorageClient` 创建时**接收 mgmtd 子客户端的引用**（`create(clientId, config, mgmtdClient)`），并向它注册为「路由信息监听者」。
- `MetaClient` 创建时**同时持有 mgmtd 子客户端和 storage 子客户端的 `shared_ptr`**——因为 meta 的一些操作（如 GC 删 chunk、查文件真实长度）需要反过来调 storage。

#### 4.1.2 核心流程

```
            ┌─────────────────────────────────────────────┐
            │            客户端进程 (fuse / 原生)            │
            │                                             │
   mgmtd 引导地址 ──▶ MgmtdClientForClient                 │
            │            │  (后台自动 refreshRoutingInfo)   │
            │            │  addRoutingInfoListener ──┐     │
            │            ▼                            │     │
            │      ICommonMgmtdClient                │     │
            │            │                            ▼     │
            │            └──────────▶ StorageClient (持有 currentRoutingInfo_)
            │            │
            │            └──shared_ptr──▶ MetaClient (同时持有 storage_ shared_ptr)
            │
            └──────────────────────────────────────────────┘
                  │              │              │
            mgmtd RPC        meta RPC      storage RPC
```

要点：

1. **mgmtd 子客户端是「路由信息的事实来源」**。它通过后台任务周期性 `refreshRoutingInfo`，并维护一份最新的 `RoutingInfo`。
2. **路由信息以「监听者」模式分发**。`StorageClient` 通过 `addRoutingInfoListener` 把自己挂上去；每次路由刷新，mgmtd 子客户端回调 `StorageClient`，后者用 `atomic_shared_ptr` 无锁整体替换本地视图（见 4.2）。
3. **三类子客户端都是「带重试的 RPC stub 持有者」**。它们内部都用 u2-l2 的 serde stub 框架发出请求，外面包一层重试。

#### 4.1.3 源码精读

**（a）mgmtd 子客户端默认开启「自动刷新路由」。** [`MgmtdClientForClient`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/MgmtdClientForClient.h#L7-L15) 的 `Config` 构造函数里三行默认值说明了它在客户端场景下的定位：

```cpp
struct Config : MgmtdClient::Config {
  Config() {
    set_enable_auto_refresh(true);            // 后台自动拉路由
    set_enable_auto_heartbeat(false);         // 客户端不发心跳（那是 server 的事）
    set_enable_auto_extend_client_session(true); // 自动续客户端会话
  }
};
```

它额外提供 [`extendClientSession`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/MgmtdClientForClient.h#L25)（续会话，让 mgmtd 知道这个客户端还活着）和 [`setConfigListener`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/MgmtdClientForClient.h#L29)（接收运行时配置热更新回调）。

**（b）公共接口 `ICommonMgmtdClient` 把「路由信息」抽象成四个动作。** 见 [ICommonMgmtdClient.h:20-27](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/ICommonMgmtdClient.h#L20-L27)：

```cpp
virtual std::shared_ptr<RoutingInfo> getRoutingInfo() = 0;          // 取当前视图
virtual CoTryTask<void> refreshRoutingInfo(bool force) = 0;         // 主动刷新
using RoutingInfoListener = std::function<void(std::shared_ptr<RoutingInfo>)>;
virtual bool addRoutingInfoListener(String name, RoutingInfoListener) = 0;   // 订阅
virtual bool removeRoutingInfoListener(std::string_view name) = 0;
```

注意 `getRoutingInfo` 返回的是 `shared_ptr<RoutingInfo>` 而非裸指针——因为路由视图会被多线程并发读，必须靠引用计数管理生命周期（见 4.2 的 `atomic_shared_ptr`）。

**（c）`MetaClient` 同时持有 mgmtd 与 storage。** 看 [MetaClient.h:315-320](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/meta/MetaClient.h#L315-L320) 的成员声明：

```cpp
[[maybe_unused]] ClientId clientId_;
const Config &config_;
const bool dynStripe_;                                   // 仅 fuse client 支持动态 stripe
std::unique_ptr<StubFactory> factory_;                   // meta RPC stub 工厂
std::shared_ptr<ICommonMgmtdClient> mgmtd_;              // mgmtd 子客户端
std::shared_ptr<storage::client::StorageClient> storage_; // storage 子客户端
```

这就是「meta 子客户端内部还能调 storage」的物理证据（meta 的 GC 删 chunk、查真实长度都会用到 `storage_`，见 u4-l5）。它的 [`open`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/meta/MetaClient.h#L123-L127) 返回的 `Inode` 里就带着 layout，是后续数据读写的起点。

**（d）`CoreClient`：一行宏展开整个服务。** [CoreClient.h:16-32](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/core/CoreClient.h#L16-L32) 用 u2-l2 讲过的 `DEFINE_SERDE_SERVICE_METHOD_FULL` 宏，把 CoreService 的每个方法都展开成「单地址直发」和「多地址依次尝试」两个重载：

```cpp
CoTryTask<core::rsptype> name(const std::vector<net::Address> &addrs, const core::reqtype &req) {
  for (auto addr : addrs) {
    auto stub = stubFactory_->create(addr);
    auto res = co_await stub->name(req);
    if (res || StatusCode::typeOf(res.error().code()) != StatusCodeType::RPC) co_return res;
  }
  co_return makeError(RPCCode::kConnectFailed, ...);  // 全部地址都连不上才报错
}
#include "fbs/core/service/CoreServiceDef.h"   // X-macro 方法清单，重复包含生成全部方法
```

这段代码体现了一个客户端子客户端的典型形态：**持有一个 stub 工厂，按地址现造 stub、发请求、遇 RPC 错误换下一个地址**。`MetaClient`/`StorageClient` 的重试逻辑更复杂，但骨架相同。

#### 4.1.4 代码实践

**实践目标：** 在源码中确认「三类子客户端的组装关系」，并验证 `MetaClient` 确实同时持有另外两个。

**操作步骤：**

1. 在 `src/fuse/` 下找到 FUSE 守护进程构造 `MetaClient`、`StorageClient`、`MgmtdClientForClient` 的位置（提示：搜索 `std::make_shared<MetaClient>` 与 `StorageClient::create`）。
2. 对照本节 4.1.2 的依赖图，标出三个对象创建的先后顺序。
3. 打开 [MetaClient.h:315-320](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/meta/MetaClient.h#L315-L320)，确认 `mgmtd_` 与 `storage_` 都是 `shared_ptr`。

**需要观察的现象 / 预期结果：** 你应当看到 mgmtd 子客户端先于 storage 子客户端创建，而 storage 子客户端又被传给 meta 子客户端——即依赖链 `mgmtd → storage → meta`。**待本地验证**（具体构造代码在 FUSE/client 初始化路径中，本讲不深入 FUSE 层，留到 u7-l2）。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 `StorageClient` 创建时接收的是 mgmtd 子客户端的**引用**（`ICommonMgmtdClient &`），而 `MetaClient` 持有 mgmtd 子客户端的 **`shared_ptr`**？

> **参考答案：** `StorageClient` 只需要在启动时注册一次「路由信息监听者」并偶尔取路由视图，不关心 mgmtd 子客户端的生命周期，所以用引用即可；`MetaClient` 则要在多个异步 RPC 的整个生命周期内随时访问 mgmtd（取路由、查会话），需要共享所有权以保证 mgmtd 子客户端不被提前析构，故用 `shared_ptr`。

**练习 2：** `MgmtdClientForClient::Config` 里 `enable_auto_heartbeat(false)`，但 `enable_auto_extend_client_session(true)`。这说明了「心跳」和「客户端会话」的区别是什么？

> **参考答案：** 心跳（heartbeat）是 **storage/meta/mgmtd 服务节点**向 primary mgmtd 续「节点租约」的机制（见 u3-l2），客户端进程不是服务节点、不发心跳；而「客户端会话」是客户端进程向 mgmtd 登记「我还活着」的轻量机制，用 `extendClientSession` 周期续约，用于客户端配置下发与存活判定，两者是不同的控制面通道。

---

### 4.2 StorageClient 的路由信息与 target 选择

#### 4.2.1 概念说明

`StorageClient` 是数据面的核心。它要解决两个问题：

1. **「这一条链现在有哪些 target 能读？」** —— 由路由信息（`RoutingInfo`）回答。客户端从 mgmtd 拿到集群视图后，能查出每条 chain 上的 target 列表及其 `publicState`（SERVING/SYNCING/…）。
2. **「能读的 target 有好几个，我该选哪个？」** —— 由 **target 选择策略**（`TargetSelectionStrategy`）回答。CRAQ「读任何」的特性赋予了客户端这个自由度，客户端用它来**把读流量在链内多个副本之间均衡**，避免所有读都打到同一个 target。

这里有两个数据结构需要区分（都在 [TargetSelection.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/TargetSelection.h) 里）：

- [`SlimTargetInfo`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/TargetSelection.h#L14-L19)：`{targetId, nodeId}`，一个 target 的最精简视图（只够用来路由）。
- [`SlimChainInfo`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/TargetSelection.h#L21-L27)：一条链的精简视图，含 `chainId`、`version`、`routingInfoVer`、`totalNumTargets` 和 **`servingTargets`（只含能服务的 target）**。策略就是从 `servingTargets` 里挑一个。

#### 4.2.2 核心流程

一次批量读里，target 选择的完整流程在 [`selectRoutingTargetForOps`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClientImpl.cc#L490-L631) 中：

```
对批里每个 IO（它的 routingTarget.chainId 已知）:
  ├─ 若该 chain 还没处理过:
  │    ├─ 从 RoutingInfo 查出完整 ChainInfo
  │    ├─ selectServingTargets(): 遍历链上 target，只留 publicState==SERVING 且通过流量区间(trafficZone)过滤的
  │    │      └─ 并剔除「失败次数已达阈值」的 target（failover）
  │    └─ 组装 SlimChainInfo（含 servingTargets）
  ├─ 用策略 strategy.selectTarget(slimChain) 从 servingTargets 挑一个
  └─ 把选中的 SlimTargetInfo 写进 op->routingTarget.targetInfo
```

关键点：

- **`servingTargets` 是「读任何」的候选集**：只含 SERVING 且可达的 target，SYNCING/WAITING 等会被排除。
- **失败感知的 failover**：若某 target 所在节点连续失败次数 ≥ `max_failures_before_failover`（默认 1），会被尽量跳过（见 [StorageClientImpl.cc:552-565](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClientImpl.cc#L552-L565)）。
- **版本钉死**：选 target 时同时记下 `chainVer` 与 `routingInfoVer`，发送前还要再校验一次（见 4.3），防止「选完到发送之间拓扑变了」。

#### 4.2.3 源码精读

**（a）默认策略是负载均衡。** 在批量读入口，若用户没指定模式，会被强制改成 `LoadBalance`（[StorageClientImpl.cc:1647-1650](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClientImpl.cc#L1647-L1650)）。所有模式枚举见 [TargetSelection.h:29-38](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/TargetSelection.h#L29-L38)：

```cpp
enum TargetSelectionMode {
  Default = 0, LoadBalance, RoundRobin, RandomTarget,
  TailTarget, HeadTarget, ManualMode, EndOfMode
};
```

**（b）六种策略的实现都很短。** 见 [TargetSelection.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/TargetSelection.cc)：

| 策略 | 选法 | 典型用途 |
| --- | --- | --- |
| [`LoadBalanceStrategy`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/TargetSelection.cc#L14-L53)（默认） | 随机起一个候选，再在链内挑「累计 IO 数最少」的节点 | 生产读，把读流量在链内副本间摊平 |
| [`RoundRobinStrategy`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/TargetSelection.cc#L56-L74) | 按 chain 维护一个递增索引，`index % N` 轮询 | 均匀且确定 |
| [`RandomTargetStrategy`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/TargetSelection.cc#L77-L88) | 随机选一个 | 简单随机 |
| [`TailTargetStrategy`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/TargetSelection.cc#L91-L97) / [`HeadTargetStrategy`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/TargetSelection.cc#L100-L106) | 固定读链尾 / 链头 | 调试、强一致读链尾已提交版本 |
| [`ManualModeStrategy`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/TargetSelection.cc#L109-L125) | 用户指定 `targetIndex` | 测试/定位问题 |

负载均衡策略的核心是「累计 IO 计数」——它维护一张 `NodeId → 累计 IO 数` 的表，挑计数最少的节点，从而把读流量在链内多个节点间均衡：

```cpp
// TargetSelection.cc:20-41（节选）
Result<SlimTargetInfo> selectTarget(const SlimChainInfo &chain) override {
  uint32_t targetIndex = folly::Random::rand32(0, chain.servingTargets.size());
  SlimTargetInfo preferredTarget = chain.servingTargets[targetIndex];
  ...
  for (const auto &target : chain.servingTargets) {
    if (numIOs[target.nodeId] < numIOs[preferredTarget.nodeId]) preferredTarget = target;
    ...                                   // 计数相等时再用全局计数器 tie-break
  }
  numIOs[preferredTarget.nodeId]++;       // 选中后累计 +1
  return preferredTarget;
}
```

注意两个计数器：`numIOs` 是**本批次内**的本地计数（每个 batch read 新建一个策略实例，见 [TargetSelection.h:48](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/TargetSelection.h#L48) 的注释「created for each batch read」），而 `numAccumIOs`（[TargetSelection.cc:8](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/TargetSelection.cc#L8)）是**进程级**的原子计数，仅在计数相等时用来跨批次打破平局。

**（c）`servingTargets` 是怎么筛出来的。** [`selectServingTargets`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClientImpl.cc#L418-L487) 遍历链上 target，**遇到第一个非 SERVING 的就 `break`**，只保留前缀连续的 SERVING target，并按 trafficZone 过滤节点：

```cpp
// StorageClientImpl.cc:426-475（节选）
for (const auto &target : chainInfo.targets) {
  if (target.publicState != flat::PublicTargetState::SERVING) break;   // 遇非 SERVING 即止
  auto targetInfo = getTargetInfo(routingInfo, target.targetId);
  auto nodeInfo   = getNodeInfo(routingInfo, *targetInfo->nodeId);
  if (!nodeSelector(*nodeInfo)) continue;                              // trafficZone 过滤
  servingTargets.push_back({TargetId(targetInfo->targetId), NodeId(*targetInfo->nodeId)});
}
```

这呼应了 u3-l5 讲的「链内 public 状态呈固定有序布局」：链头的副本先恢复成 SERVING，链尾最后，所以「前缀连续 SERVING」恰好就是当前可安全读取的副本集合。

**（d）路由信息是无锁热更新的。** `StorageClient` 用 `folly::atomic_shared_ptr` 持有当前路由视图（[StorageClientImpl.h:230](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClientImpl.h#L230)）：

```cpp
folly::atomic_shared_ptr<hf3fs::client::RoutingInfo const> currentRoutingInfo_;
```

启动时它向 mgmtd 子客户端注册监听者，每次路由刷新回调 [`setCurrentRoutingInfo`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClientImpl.cc#L1494-L1559) 就 `store` 整份新视图；读路径用 `getCurrentRoutingInfo().load()` 拿到一个 `shared_ptr` 快照，整个批次都用这一份，既无锁又一致。

#### 4.2.4 代码实践

**实践目标：** 通过修改 target 选择模式，直观感受「读流量在链内如何分布」。

**操作步骤：**

1. 阅读 [`selectRoutingTargetForOps`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClientImpl.cc#L490-L631)，找到 `strategy->selectTarget(slimChain)` 的调用点（[第 608 行](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClientImpl.cc#L608)）。
2. 阅读 [TargetSelection.cc:127-150](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/TargetSelection.cc#L127-L150) 的 `create()` 工厂，确认 `mode` 与策略类的对应关系。
3. 在客户端配置里把读的 `targetSelection.mode` 分别设成 `RoundRobin`、`TailTarget`、`LoadBalance`，对同一文件发起多次读。

**需要观察的现象 / 预期结果：**

- `RoundRobin`：同一链上的读会按 `index % N` 在 N 个 serving target 间严格轮换。
- `TailTarget`：所有读都打到链尾那一个 target（可在 storage 侧监控看到单一 target 读流量飙升）。
- `LoadBalance`（默认）：读流量在链内多个 serving target 间大致均衡。

**待本地验证**（需要真实集群与监控；若仅阅读源码，可通过在 `selectTarget` 各策略返回前加一行 `XLOGF(INFO, ...)` 日志，观察被选中的 `nodeId` 分布来验证）。

#### 4.2.5 小练习与答案

**练习 1：** 为什么 `selectServingTargets` 遇到第一个非 SERVING 的 target 就 `break`，而不是 `continue` 跳过它继续找后面的 SERVING target？

> **参考答案：** 因为 CRAQ 链内 public 状态是**有序布局**的（见 u3-l5）：链头到链尾按 SERVING → … → 非SERVING 排列，可安全读取的副本是一个**前缀连续**的集合。一旦遇到非 SERVING，后面不会再有 SERVING，`break` 既正确又高效；若用 `continue` 反而可能把处于过渡态、尚不可读的副本纳入候选，破坏一致性。

**练习 2：** 假设一条链有 3 个 target 都在 SERVING，分别属于节点 A、B、C。用默认 `LoadBalance` 策略发 300 次单 IO 的读，A/B/C 的流量一定完全相等吗？

> **参考答案：** 不一定完全相等。`LoadBalance` 先随机起一个候选、再挑「本批次累计 IO 数最少」的节点；当多个节点计数相等时，用进程级原子计数器 `numAccumIOs` 打破平局。因此长期看会趋于均衡，但单次分布带随机性，且会受历史累计计数影响，不会是严格的 100/100/100。

---

### 4.3 IO 合并、拆分与批量发送

#### 4.3.1 概念说明

用户调 `batchRead` 时传入的往往是一大批 `ReadIO`（比如训练 Dataloader 一次取几百条样本）。`StorageClient` 不会把它们一条条发出去，而是做一套**「拆分 → 选 target → 按节点合并 → 并发发送」**的流水线，目的是：

- **减少 RPC 数量**：把落到同一 storage 节点的多个小 IO 合并成一个大请求（`max_batch_size` / `max_batch_bytes`）。
- **避免单 IO 过大**：把超过 `max_read_io_bytes` 的巨型 IO 拆成若干对齐的小 IO。
- **控制并发**：用「全局 + 单服务器」两级信号量限流，既榨干带宽又不把单个 storage 节点压垮。
- **跨节点并行**：不同节点的批次可以并发发送（`process_batches_in_parallel`）。

这里要先认识 [`RoutingTarget`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClient.h#L21-L42)——它挂在每个 IO 上，记录「这个 IO 要路由到哪」：

```cpp
class RoutingTarget {
 public:
  ChainId chainId;                          // 属于哪条链（由 layout 算出，见 4.3 综合实践）
  ChainVer chainVer;                        // 链版本（发送前校验）
  flat::RoutingInfoVersion routingInfoVer;  // 选 target 时的路由版本
  SlimTargetInfo targetInfo;                // 选中的 {targetId, nodeId}
  UpdateChannel channel;                    // 写专用的更新通道（见 update 路径）
};
```

#### 4.3.2 核心流程

`batchRead` 的完整流水线（[StorageClientImpl.cc:1563-1744](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClientImpl.cc#L1563-L1744)）：

```
batchRead(span<ReadIO>)
  └─ batchReadWithRetry
       ├─ validateDataRange:           校验 buffer 不重叠、范围合法
       ├─ (若开启) splitReadIOs:        把超过 max_read_io_bytes 的 IO 按 maxIOLen 对齐拆分
       └─ sendOpsWithRetry:            带退避重试的外层循环
            └─ batchReadWithoutRetry:   单次尝试
                 ├─ selectRoutingTargetForOps:  给每个 IO 选 target（见 4.2）
                 ├─ groupOpsByNodeId:           按 nodeId 分组、切成受 max_batch_size/bytes 约束的批次
                 └─ processBatches(parallel):   对每个(nodeId, 批次)并发执行 sendReq
                      ├─ 取 per-server 信号量 + 全局信号量（限流）
                      ├─ isLatestRoutingInfo:   发送前再校验路由版本
                      ├─ buildBatchRequest:     打包成 BatchReadReq
                      └─ sendBatchRequest -> messenger_.batchRead(addr, req)  // 真正发 RPC
```

四个阶段各有侧重：**拆分**针对单 IO 大小、**合并**针对 RPC 数量、**限流**针对并发度、**校验**针对一致性。

#### 4.3.3 源码精读

**（a）巨型 IO 的拆分：`splitReadIOs`。** 若配置了 `max_read_io_bytes > 0`，一个跨越很大的读会被切成多片，每片按 `maxIOLen` 对齐（[StorageClientImpl.cc:953-992](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClientImpl.cc#L953-L992)）：

```cpp
for (uint32_t offset = parentIO->offset, length = parentIO->length; length > 0;) {
  uint32_t ioEnd = ALIGN_LOWER(offset, maxIOLen) + maxIOLen;
  uint32_t ioLen = std::min(ioEnd - offset, length);
  parentIO->splittedIOs.push_back(
      client.createReadIO(parentIO->routingTarget.chainId, parentIO->chunkId,
                          offset, ioLen,
                          parentIO->data + (offset - parentIO->offset),  // 数据指针随切片前移
                          parentIO->buffer));
  offset += ioLen; length -= ioLen;
}
```

拆出的子 IO 复用父 IO 的 `buffer`（同一段已注册内存的不同 subrange），全部成功后再把各片 `lengthInfo` 累加回父 IO（见 [batchReadWithRetry 第 1607-1633 行](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClientImpl.cc#L1607-L1633)）。

**（b）按节点合并成批次：`groupOpsByNodeId`。** 选完 target 后，每个 IO 都有了 `targetInfo.nodeId`。[`groupOpsByNodeId`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClientImpl.cc#L1029-L1122) 把它们**按 nodeId 分桶**，再在每桶内切成不超过 `max_batch_size`（条数）和 `max_batch_bytes`（字节数）的批次：

```cpp
// StorageClientImpl.cc:1059-1091（节选）
for (const auto &[nodeId, opsGroup] : opsGroupedByNode) {
  size_t avgBatchSize  = calcAvgSize(remainingOps,   maxBatchSize);
  size_t avgBatchBytes = calcAvgSize(remainingBytes, maxBatchBytes);
  for (size_t i = 0; i < opsGroup.size(); i++) {
    if ((batchOps.size() >= avgBatchSize && ...) ||
        (batchBytes + op->dataLen() > avgBatchBytes && ...)) {
      batches.emplace_back(nodeId, std::move(batchOps));   // 凑满一批就切出
      batchOps.clear(); batchBytes = 0;
    }
    batchOps.push_back(op); batchBytes += op->dataLen();
  }
  if (!batchOps.empty()) batches.emplace_back(nodeId, batchOps);  // 最后一批
}
```

`calcAvgSize` 的作用是**尽量让各批大小均匀**（用总条数/总字节数除以「向上取整的批数」算平均，而非每批都顶到 `maxBatchSize`）。批切完后若开启 `random_shuffle_requests`，还会随机打乱批次顺序（[第 1102-1105 行](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClientImpl.cc#L1102-L1105)），避免多客户端同步把请求压到同一节点。

**（c）两级信号量限流。** [`OperationConcurrencyLimit`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClientImpl.h#L150-L196) 提供两级限流：全局 `concurrencySemaphore_`（`max_concurrent_requests`，默认 32）和**每个 nodeId 一个**的 `perServerSemaphore_`（`max_concurrent_requests_per_server`，默认 8）。发送每个批次前要先拿到这两把令牌（[StorageClientImpl.cc:1667-1671](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClientImpl.cc#L1667-L1671)）：

```cpp
SemaphoreGuard perServerReq(*readConcurrencyLimit_.getPerServerSemaphore(nodeId));
co_await perServerReq.coWait();
SemaphoreGuard concurrentReq(readConcurrencyLimit_.getConcurrencySemaphore());
co_await concurrentReq.coWait();
```

这样既能并发打满多条链（全局 32），又不让单一 storage 节点吃下超过 8 个并发请求（per-server 8）。配置见 [StorageClient.h:378-385](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClient.h#L378-L385)。

**（d）发送前的版本校验。** 拿到令牌、即将发包前，还要再查一次路由是不是最新的（[StorageClientImpl.cc:1673-1676](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClientImpl.cc#L1673-L1676)，宏 `isLatestRoutingInfo` 定义在 [第 633-645 行](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClientImpl.cc#L633-L645)）：若期间路由版本变了且相关链版本也变了，就以 `kRoutingVersionMismatch` 失败、让外层重试重选。这是「等令牌期间拓扑变化」竞态的兜底。

> 与 u5-l4 呼应：客户端这边做「选 target 时记版本 + 发送前再校验」，storage 服务端那边做「收到请求时按 `VersionedChainId` 校验」，两端共同保证写请求落到正确拓扑。

#### 4.3.4 代码实践

**实践目标：** 通过阅读源码与配置，定量理解「合并」与「限流」。

**操作步骤：**

1. 打开 [StorageClient.h:378-394](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClient.h#L378-L394)，记录 `OperationConcurrency` 的默认值：`max_batch_size=128`、`max_batch_bytes=4MB`、`max_concurrent_requests=32`、`max_concurrent_requests_per_server=8`。
2. 跟踪 [`groupOpsByNodeId`](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClientImpl.cc#L1029-L1122)，回答下面的练习 2。
3. 在 [`batchReadWithoutRetry`](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClientImpl.cc#L1642-L1744) 的 `sendReq` lambda 里找到 `perServerReq` 与 `concurrentReq` 两个 `co_wait` 点，确认限流发生在「打包请求之前」。

**需要观察的现象 / 预期结果：** 你应当能说清楚一次 1000 条小 IO 的 `batchRead`，在最坏情况下会被切成多少个 RPC（取决于它们落到几个节点、`max_batch_size=128`）。**预期结果**：若全部落到同一节点，最多 `ceil(1000/128)=8` 个批次；若均匀分散到多个节点，每个节点最多 128 条。

#### 4.3.5 小练习与答案

**练习 1：** 为什么要有「全局」和「单服务器」两级并发限制，只用一个全局限制不行吗？

> **参考答案：** 只用全局限制的话，若某次批量的 IO 恰好全部路由到同一个 storage 节点，全部并发令牌都会压到这一台机器上，把它打满甚至打挂，而其他节点却闲置。单服务器限制（默认 8）给每个节点设上限，保证无论路由如何分布，单节点都不会被压垮；全局限制（默认 32）则保证总并发不会无限膨胀。两者配合既榨带宽又保护对端。

**练习 2：** `groupOpsByNodeId` 里用 `calcAvgSize` 算「平均批大小」，而不是简单地「每批顶到 `maxBatchSize` 再切」。这样做有什么好处？

> **参考答案：** 用平均值切批能让同一节点的多个批次**大小更均匀**，避免出现「一批接近 128 条、最后一批只有 1 条」的尾巴。均匀的批次有利于各批次耗时接近、并发调度更平滑，也更容易让 RDMA 批量读把数据摊到大小相近的请求上，减少长尾。

---

### 4.4 RDMA buffer 零拷贝管理

#### 4.4.1 概念说明

要让数据「不经过内核拷贝」地从 storage 节点的 SSD 直达客户端用户缓冲区，客户端必须提前把目标内存**注册**给 RDMA 网卡（NIC）：把用户内存「钉住」（pin，禁止换页）并调用 `ibv_reg_mr` 登记一段「远程可写」的内存区域（MR），拿到一个 `rkey`/`lkey`。这些是 u2-l4 讲过的 `RDMABuf`/`RDMABufPool` 的职责。

在客户端这一层，`StorageClient` 把这个过程封装成两个东西：

- [`IOBuffer`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClient.h#L46-L66)：对 `net::RDMABuf`（已注册内存）的轻量包装，**销毁即注销**。
- [`registerIOBuffer`](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClient.h#L517)：用户调它来注册一段自己的内存，拿到 `IOBuffer`。

之后用户构造 `ReadIO`/`WriteIO` 时，要把 `data` 指针指向**这段已注册 buffer 内部**的某个位置，并把 `buffer` 指针传进去。这样 storage 服务回数据时，可以直接用 RDMA Write 单边写进 `data` 所在的 MR，全程零拷贝。

#### 4.4.2 核心流程

```
用户侧:
  uint8_t *mem = malloc(N);                       // 1. 分配一块内存
  auto iobuf = storageClient.registerIOBuffer(mem, N);   // 2. 注册（pin + ibv_reg_mr），拿到 IOBuffer
  auto rio   = storageClient.createReadIO(chainId, chunkId,
                                          offset, length,
                                          mem + off, &iobuf);  // 3. data 落在 iobuf 内部
  co_await storageClient.read(rio, userInfo);     // 4. 发请求；storage 用 RDMA Write 把数据写进 mem+off
  // 5. 读到的数据已在 mem+off，直接用；rio.data() 即指向它
  // iobuf 析构时自动 deregister MR
```

核心不变量：**`data` 指针必须落在 `buffer`（`IOBuffer`）所注册的内存范围内**，且同一批次内不同 IO 的 `data` 区间不能重叠（可由 `check_overlapping_read_buffers` 校验，见 [StorageClient.h:420-421](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClient.h#L420-L421)）。违反这一点会导致 RDMA 写冲突或注册失败。

#### 4.4.3 源码精读

**（a）`IOBuffer` 就是 `RDMABuf` 的壳。** [StorageClient.h:46-66](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClient.h#L46-L66)：

```cpp
class IOBuffer : public folly::MoveOnly {
 public:
  uint8_t *data() const { return const_cast<uint8_t *>(rdmabuf.ptr()); }
  size_t size() const { return rdmabuf.size(); }
  bool contains(const uint8_t *data, uint32_t len) const { return rdmabuf.contains(data, len); }
  net::RDMABuf subrange(size_t offset, size_t length) const { return rdmabuf.subrange(offset, length); }
  ...
 private:
  const hf3fs::net::RDMABuf rdmabuf;   // 真正持有已注册内存的对象
};
```

`contains` 用来校验「`data` 是否落在注册范围内」，`subrange` 用来在拆分 IO 时切出子区域——呼应 4.3 里 `splitReadIOs` 复用同一 `buffer` 的设计。

**（b）`registerIOBuffer` 调 `createFromUserBuffer` 完成注册。** [StorageClient.cc:97-110](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClient.cc#L97-L110)：

```cpp
Result<IOBuffer> StorageClient::registerIOBuffer(uint8_t *buf, size_t len) {
  monitor::ScopedLatencyWriter latencyWriter(iobuf_reg_latency);
  iobuf_reg_size.addSample(len);
  auto rdmabuf = hf3fs::net::RDMABuf::createFromUserBuffer(buf, len);  // pin + ibv_reg_mr
  if (rdmabuf.valid()) {
    iobuf_reg_success_ops.addSample(1);
    return IOBuffer{rdmabuf};
  } else {
    iobuf_reg_failed_ops.addSample(1);
    return makeError(StorageClientCode::kMemoryError);
  }
}
```

`RDMABuf::createFromUserBuffer` 是 u2-l4 讲过的「用户内存零拷贝注册」入口。注意这里**不带 buffer pool**——用户自带内存，注册一次、用完随 `IOBuffer` 析构而注销。注册失败（如内存无法 pin）返回 `kMemoryError`。

**（c）`createReadIO` 只是组装，不发请求。** [StorageClient.cc:48-56](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClient.cc#L48-L56) 把传入参数原样塞进 `ReadIO`：

```cpp
ReadIO StorageClient::createReadIO(ChainId chainId, const ChunkId &chunkId,
                                   uint32_t offset, uint32_t length,
                                   uint8_t *data, IOBuffer *buffer, void *userCtx) {
  return ReadIO{chainId, chunkId, offset, length, data, buffer, userCtx};
}
```

真正发包是 `read`/`batchRead`（协程）。`ReadIO` 的字段含义见 [StorageClient.h:112-130](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClient.h#L112-L130)：`routingTarget`（路由）、`chunkId`/`offset`/`length`（读哪一段）、`data`/`buffer`（数据落在哪个已注册 MR）、`result`（回填的读结果）。

**（d）回包时数据已经在用户内存里。** 在 [`batchReadWithoutRetry` 的 sendReq](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClientImpl.cc#L1697-L1737) 里，正常 RDMA 路径下回包只含 `IOResult` 元数据（长度、校验和），数据已由 storage 单边 RDMA Write 写进客户端预留 buffer；随后客户端**本地重算校验和**与 server 报回的校验和比对（[第 1720-1736 行](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClientImpl.cc#L1720-L1736)），不一致则报 `kChecksumMismatch`。只有在小数据/调试（`SEND_DATA_INLINE`）模式下，数据才随回包内联返回、由客户端 `memcpy` 落位（[第 1697-1718 行](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClientImpl.cc#L1697-L1718)）。

> 这呼应 u5-l2 的零拷贝结论：数据从 SSD → storage 端已注册 RDMA buffer → RDMA Write → 客户端预留 buffer，全程不经内核拷贝；客户端侧的「预留 buffer」正是本节讲的 `registerIOBuffer` 注册出来的 `IOBuffer`。

#### 4.4.4 代码实践

**实践目标：** 理解「注册 buffer → 构造 IO → 读后数据就位」的零拷贝链路，并搞清 `data` 与 `buffer` 的关系。

**操作步骤：**

1. 阅读 [StorageClient.h:448-459](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClient.h#L448-L459) `createReadIO` 的注释，特别注意这句：*"The memory pointed by `data` ... fall in the range of the registered `buffer`"*。
2. 阅读 [`registerIOBuffer`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClient.cc#L97-L110)，确认它返回的 `IOBuffer` 持有 `RDMABuf`。
3. 在 [`IOBuffer::contains`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/storage/StorageClient.h#L52) 处思考：为什么客户端要校验 `data` 落在 `buffer` 范围内？

**需要观察的现象 / 预期结果：** 你应当能解释——若把一个**未注册**的 `data` 指针传给 `createReadIO`（或 `data` 在 `buffer` 范围外），storage 即便想 RDMA Write 也写不进来（该内存没有有效的 MR/`rkey`），读会失败或数据落不到位。**预期结果**：正确的用法是先 `registerIOBuffer` 一块大内存，再在其中切出多个不重叠的 `data` 区间给同一批的不同 IO。

#### 4.4.5 小练习与答案

**练习 1：** 为什么「注册 buffer」是一次性的、而读请求可以反复发？

> **参考答案：** RDMA 内存注册（pin + `ibv_reg_mr`）代价不低（要锁页、改页表、向 NIC 登记 MR），不适合每次读都做。3FS 的做法是让用户**注册一次**大块内存拿到 `IOBuffer`，之后所有落在该范围内的读都复用这个 MR——storage 用它的 `rkey` 单边写数据进来，无需每次重新注册。这正是「零拷贝」得以低成本成立的前提。

**练习 2：** `IOBuffer` 是 `folly::MoveOnly`（只能移动不能拷贝）。为什么要禁止拷贝？

> **参考答案：** `IOBuffer` 内部的 `RDMABuf` 持有真实的 MR 资源，且其析构会 deregister。若允许拷贝，就会出现多个 `IOBuffer` 指向同一 MR，其中一个析构就把 MR 注销了，另一个还在用——导致 use-after-deregister。禁拷贝、只允许移动，保证 MR 的所有权唯一、销毁即注销的语义清晰。

---

## 5. 综合实践

**任务：** 梳理「客户端 `open` 之后发起一次 `read`」的完整流程，重点说清楚**偏移量如何换算成 chunk id 与所属链，再如何选出 target**。这是把本讲四个模块串起来的总练习。

**背景：** `open` 由 `MetaClient` 完成，返回的 `Inode`（文件类型）里带着 `layout`（含 `chunkSize`、`stripeSize`、`chainTableId` 等）。此后的读完全在 `StorageClient` 一侧完成，`MetaClient` 不再参与。

**操作步骤：**

1. **偏移 → chunk id。** 阅读 [`File::getChunkId`](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/meta/Schema.cc#L62-L73)：

   ```cpp
   auto chunk = offset / layout.chunkSize;   // 第几个 chunk
   return ChunkId(id, 0, chunk);             // ChunkId = (inodeId, ?, chunkIndex)
   ```

   即：文件内偏移 `offset` 除以 `chunkSize` 得到 chunk 序号，拼成 `ChunkId(inodeId, 0, chunkIndex)`。这一步**纯算术、不查任何表**。

2. **偏移 → 所属链。** 阅读 [`File::getChainId`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/meta/Schema.cc#L75-L91)：

   ```cpp
   auto ref = layout.getChainOfChunk(inode, offset / layout.chunkSize + track * TRACK_OFFSET_FOR_CHAIN);
   auto cid = routingInfo.getChainId(ref);   // 用 ChainRef 在路由视图里解出真实 ChainId
   ```

   即：由 layout（`ChainRange`/`ChainList` + shuffle seed，见 u4-l4）算出一个 `ChainRef`（含 chainTableId、chainTableVersion、chainIndex），再在**客户端本地的 `RoutingInfo`** 里解出 `ChainId`。这一步依赖 4.2 讲的、由 mgmtd 子客户端热更新的路由视图。

3. **链 → target。** 拿着 `ChainId` 构造 `ReadIO`（`chainId` 字段就位），交给 `batchRead`。`StorageClient` 内部按 4.2 的流程：查 `servingTargets` → 用 `LoadBalance` 策略选一个 target → 记下 `chainVer`/`routingInfoVer`。一个真实的「偏移 → (chain, chunkId) → 切分」循环范例见 [PioV.cc:104-128](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/PioV.cc#L104-L128)（FUSE 侧 USRBIO 的并行读，把一次大读按 `chunkSize` 切成多段，每段算出 `(chain, chunkId)` 后 `consumeChunk`）：

   ```cpp
   for (...; l < len + chunkSize; ...) {
     auto chain  = f.getChainId(inode, opOff, *routingInfo_, track);   // 偏移 -> ChainId
     auto fchunk = f.getChunkId(inode.id, opOff);                      // 偏移 -> ChunkId
     ...
     consumeChunk(*chain, chunk, chunkSize, chunkOff + co, ...);       // 交给 StorageClient
   }
   ```

4. **数据就位。** 按 4.3 的流水线合并/限流/发送，按 4.4 的方式由 storage 单边 RDMA Write 把数据写进预先 `registerIOBuffer` 的内存。

**需要观察的现象 / 预期结果：** 你应当能画出下面这张时序，并标注每一步用到的源码：

```
MetaClient.open ─▶ Inode(含 layout)
                        │
   读路径(StorageClient 侧，meta 不再参与):
   offset ──getChunkId──▶ ChunkId          (Schema.cc:62)   纯算术
   offset ──getChainId──▶ ChainId          (Schema.cc:75)   查本地 RoutingInfo
              │
   createReadIO(chainId, chunkId, data, &iobuf)             (StorageClient.cc:48)
              │
   batchRead ─▶ selectRoutingTargetForOps ─▶ 选 target      (StorageClientImpl.cc:490)
              └▶ groupOpsByNodeId ─▶ 切批次                  (StorageClientImpl.cc:1029)
              └▶ sendBatchRequest (RDMA Write 回填 iobuf)    (StorageClientImpl.cc:1697)
```

**待本地验证**：若能在测试集群挂载 FUSE、对一个已知 layout 的文件做一次 `read`，可用 `strace`/storage 侧监控观察「read 不产生对 meta 的额外 RPC、只打到 storage」，从而验证「meta 不在数据热路径」。无集群时，本实践作为**源码阅读型实践**完成即可——重点是读懂上述四个函数的调用链。

## 6. 本讲小结

- **客户端是一捆子客户端**：`MgmtdClientForClient`（路由/配置/会话）、`MetaClient`（元数据，且内部持有 mgmtd 与 storage 的 `shared_ptr`）、`StorageClient`（数据）、`CoreClient`（底层 CoreService）。依赖链为 `mgmtd → storage → meta`。
- **路由信息走「监听者 + atomic_shared_ptr」热更新**：mgmtd 子客户端后台刷新，`StorageClient` 注册监听、无锁整体替换本地 `currentRoutingInfo_`，读路径取快照使用。
- **target 选择实现 CRAQ「读任何」**：从链上筛出前缀连续的 SERVING target，默认用 `LoadBalance` 策略把读流量在链内副本间均衡，并支持失败感知的 failover。
- **批量读走四段流水线**：`validateDataRange → splitReadIOs（拆分）→ selectRoutingTargetForOps（选 target）→ groupOpsByNodeId（按节点合并成受 `max_batch_size/bytes` 约束的批次）→ processBatches（并发发送）`，配「全局 + 单服务器」两级信号量限流，发送前再做一次路由版本校验。
- **RDMA 零拷贝靠 `registerIOBuffer`**：用户预先注册内存拿到 `IOBuffer`（pin + `ibv_reg_mr`），`ReadIO.data` 必须落在其范围内；storage 用 RDMA Write 单边把数据写进该 MR，回包只带元数据与校验和，客户端本地重算校验和比对。
- **偏移到数据的换算链**：`offset → getChunkId`（纯算术）`→ getChainId`（查本地路由）`→ 选 target → RDMA 回填`，全程 meta 不参与。

## 7. 下一步学习建议

- **u7-l2 FUSE 守护进程与请求分发**：本讲的子客户端最终被 FUSE 守护进程组装起来。下一讲看内核 FUSE 请求如何经 `FuseOps` 分发到 `MetaClient`/`StorageClient`，以及为什么 FUSE 共享队列自旋锁会成为吞吐瓶颈。
- **u7-l3 异步零拷贝 USRBIO API**：本讲的 `registerIOBuffer`/`ReadIO` 是 USRBIO `Iov`/`Ior` 的底层原料。下一讲看 `IoRing` 如何把成百上千个 `ReadIO` 组织成环形批处理，进一步榨干吞吐。
- **回看 u5-l2 读路径**：本讲是「客户端发起」的读，u5-l2 是「storage 服务端接收」的读。两边对读（`BatchReadReq`/`IOResult`、RDMA buffer、chunk 版本校验）的概念是一一对应的，对照阅读能看清整条零拷贝链路。
- **若对 layout 的链分配细节感兴趣**：可回看 u4-l4（`ChainAllocator` 的 round-robin 选链与 shuffle 打乱），理解本讲 `getChainId` 所依赖的 `ChainRef` 是如何在文件创建时就钉死版本的。
