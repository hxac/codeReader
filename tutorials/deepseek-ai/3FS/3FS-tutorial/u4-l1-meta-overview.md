# meta 服务总览与无状态架构

## 1. 本讲目标

本讲是「元数据服务 meta」单元（u4）的第一篇。读完本讲，你应当能够：

- 说清 **meta 服务为什么是无状态的**，以及「无状态」在 3FS 里到底意味着什么（元数据存在哪里、进程重启后靠什么恢复）。
- 画出一次元数据 RPC 的**完整分层调用路径**：`MetaSerdeService → MetaOperator → MetaStore → Operation → OperationDriver → FoundationDB 事务`，并理解每一层的职责边界。
- 理解 **Distributor（分片转发）** 如何让多个 meta 实例协同分担负载，以及它如何与「无状态」设计自洽。
- 自己对照源码，追踪一次 `create file` RPC 从入口到 FDB 事务提交的全过程。

本讲只讲「骨架与分层」，不深入具体操作的内部逻辑（如 inode 的 KV 编码、rename 的冲突范围）——那是 u4-l2、u4-l3 的任务。

## 2. 前置知识

在进入本讲前，请确认你已经理解以下内容（来自前置讲义）：

- **u1-l4 端到端链路**：meta 不在数据热路径。`open` 时 client 从 meta 取得 inode 与 layout（含 chunkSize、stripe size、chain table、shuffle seed），此后 read/write 由 client 自算 chunk id 直连 storage。也就是说，meta 是一个**控制面**服务，负责「这个文件长什么样、放在哪」，而不是「搬运数据」。
- **u2-l1 服务骨架**：meta/storage/mgmtd 三服务共用 `TwoPhaseApplication` 两阶段启动骨架，`main` 里只有一行 `TwoPhaseApplication<Server>().run(argc, argv)`，差异仅在模板参数；`beforeStart` 是建客户端、刷路由、注册 RPC 服务的标准位置。
- **u2-l2 RPC 与 serde**：3FS 用 `(serviceId, methodId)` 两个整数做 RPC「门牌号」，服务端按门牌号把请求派发给 `serde::Service` 的方法。
- **u2-l6 FoundationDB 与事务**：FDB 是 meta 元数据的**唯一事实来源**；`IKVEngine` → `IReadOnlyTransaction`/`IReadWriteTransaction` 是上层使用的抽象接口，`FDBRetryStrategy` 负责冲突重试。

如果对 FDB 的「乐观并发控制 + 冲突范围 + 自动重试」还不太熟，建议先回顾 u2-l6，因为本讲的「无状态」本质就是建立在 FDB 事务语义之上的。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [`src/meta/meta.cpp`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/meta.cpp) | meta 服务的 `main` 入口，仅一行启动骨架。 |
| [`src/meta/service/MetaServer.h`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/service/MetaServer.h) | `MetaServer` 类：继承 `net::Server`，声明 NodeType、配置与服务成员。 |
| [`src/meta/service/MetaServer.cc`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/service/MetaServer.cc) | `MetaServer::beforeStart`：组装客户端、KV 引擎、`MetaOperator`，注册 RPC 服务。 |
| [`src/meta/service/MetaSerdeService.h`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/service/MetaSerdeService.h) | RPC 服务：用宏把每个 RPC 方法转发给 `MetaOperator`。 |
| [`src/meta/service/MetaOperator.h`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/service/MetaOperator.h) / [`MetaOperator.cc`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/service/MetaOperator.cc) | 核心调度器：鉴权、分片转发、批处理、调用 `runOp`/`runInBatch` 跑事务。 |
| [`src/meta/store/MetaStore.h`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/MetaStore.h) | 元数据操作入口：持有各组件，对外暴露 `open`/`stat`/`rename` 等 Operation 工厂方法。 |
| [`src/meta/store/Operation.h`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Operation.h) | `Operation` 模板基类与 `OperationDriver`（事务执行 + 重试主循环）。 |
| [`src/common/kv/WithTransaction.h`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/kv/WithTransaction.h) | 通用「跑一个事务并在失败时按策略重试」的工具模板。 |
| [`src/meta/components/Distributor.cc`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/Distributor.cc) | 分片路由：决定一个 inode 该由哪个 meta 实例处理。 |

一句话记住分层：

```
RPC 入口      MetaSerdeService        （按 methodId 派发）
   ↓
调度层        MetaOperator            （鉴权 / 分片转发 / 批处理 / runOp）
   ↓
操作入口      MetaStore               （持有组件，产出 Operation 对象）
   ↓
执行驱动      OperationDriver         （开事务 / 跑 Operation / 提交 / 重试）
   ↓
存储          FoundationDB（IKVEngine）（唯一事实来源）
```

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**无状态架构**、**操作分发**、**存储入口**。

### 4.1 无状态架构

#### 4.1.1 概念说明

很多分布式系统文档都会写「无状态（stateless）」，但在 3FS 里这个词有非常具体的含义，值得先讲清楚：

> **meta 进程本身不持久化任何业务状态。所有的文件元数据（inode、目录项、文件 session、布局 layout 等）都存在 FoundationDB 里。meta 进程内存里只有缓存和运行期组件。**

这意味着：

1. **任意一个 meta 实例都能处理任意一个元数据请求**——因为数据不在它本地，而在共享的 FDB 里。这是后面「多实例分担负载」的前提。
2. **meta 进程崩溃/重启不丢任何元数据**——重启后重新连上 FDB，状态自然恢复。升级时可以逐个重启实例而不停服。
3. **「无状态」不等于「没有内存数据」**——meta 内存里其实有不少东西（AclCache、Distributor 的 server map、inode id 分配器等），但这些都是**可重建的缓存或临时簿记**，丢了能从 FDB 重新拉起来，所以不影响「无状态」的定性。

为什么能这么做？因为 FDB 是一个**强一致、支持事务、能水平扩展读**的 KV 存储。把「并发控制、持久化、故障恢复」这三件最难的事全交给 FDB，meta 服务就只剩「业务逻辑编排」这一层，复杂度大幅降低。这正是 3FS 选择「无状态元 + 事务型 KV」的核心动机（见 u1-l1）。

#### 4.1.2 核心流程

一个 meta 实例的「无状态」生命线可以概括为三步：

1. **启动时重建**：进程起来后，连上 mgmtd 拿路由信息，连上 FDB（通过 `IKVEngine`），把可重建的组件（inode id 分配器、GC 管理器等）初始化好——这些组件的状态最终都指向 FDB。
2. **运行期只做编排**：每个请求都临时开一个 FDB 事务，在事务里读、改、提交；事务提交成功，状态就落在 FDB 了。
3. **崩溃后无损恢复**：进程挂了，FDB 里已提交的数据都在；新实例（或重启后的实例）重新走第 1 步即可。

注意一个关键点：**inode id 的分配**这类「自增计数器」也放在 FDB 里（`InodeIdAllocator` 基于 FDB 的事务型计数器），而不是放在某个 meta 实例的内存里——否则实例一挂就会丢计数器、产生重复 id。本讲只需记住这个结论，具体编码留到 u4-l2。

#### 4.1.3 源码精读

先看入口。meta 的 `main` 和 storage/mgmtd 完全一样的写法，唯一区别是模板参数换成 `MetaServer`：

[`src/meta/meta.cpp`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/meta.cpp#L5-L8) —— `main` 只有一行，把控制权交给两阶段启动骨架（详见 u2-l1）：

```cpp
int main(int argc, char *argv[]) {
  using namespace hf3fs;
  return TwoPhaseApplication<meta::server::MetaServer>().run(argc, argv);
}
```

`MetaServer` 继承自 `net::Server`，并声明了自己的身份：

[`MetaServer.h`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/service/MetaServer.h#L22-L25) —— `kName = "Meta"`、`kNodeType = flat::NodeType::META`，这两个常量决定了它在 mgmtd 里被登记成哪类节点、能拉到哪份运行时配置：

```cpp
class MetaServer : public net::Server {
 public:
  static constexpr auto kName = "Meta";
  static constexpr auto kNodeType = flat::NodeType::META;
```

「无状态」最直接的代码证据在 `beforeStart` 里——看它是如何把 KV 引擎搭起来的：

[`MetaServer.cc`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/service/MetaServer.cc#L46-L48) —— 如果上层没注入 `kvEngine_`，就从配置创建一个 `HybridKvEngine`。生产环境它背后是 FoundationDB；`use_memkv` 分支是给单元测试用的内存 KV：

```cpp
  if (!kvEngine_) {
    kvEngine_ = kv::HybridKvEngine::from(config_.kv_engine(), config_.use_memkv(), config_.fdb());
  }
```

这个 `kvEngine_` 就是 meta 与「外部状态存储」之间的唯一桥梁，类型是 `std::shared_ptr<kv::IKVEngine>`（接口）。把 KV 抽象成 `IKVEngine`，意味着 meta 的业务代码不直接依赖 FDB 的 C API，测试时可以换成 `MemKV`（这也解释了 `use_memkv` 配置项的存在）。

再看 `MetaServer` 持有的成员，注意**没有任何「文件元数据」字段**：

[`MetaServer.h`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/service/MetaServer.h#L88-L94) —— 只有配置引用、KV 引擎、几个客户端、和一个 `MetaOperator`：

```cpp
  const Config &config_;
  std::shared_ptr<kv::IKVEngine> kvEngine_;
  std::unique_ptr<net::Client> backgroundClient_;
  std::shared_ptr<::hf3fs::client::MgmtdClientForServer> mgmtdClient_;
  std::unique_ptr<MetaOperator> metaOperator_;
```

没有 `inodeTable`、没有 `dirCache` 之类的持久结构——这正是「无状态」在类型层面的体现：所有能丢的东西，要么是客户端句柄，要么是 `MetaOperator` 内部那些可重建的组件。

最后看服务监听的配置，体会「业务面走 RDMA、控制面走 TCP」的设计（和 mgmtd 一致，见 u3-l1）：

[`MetaServer.h`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/service/MetaServer.h#L48-L57) —— 第 0 组承载 `MetaSerde`（业务 RPC，默认 RDMA、端口 8000），第 1 组承载 `Core`（控制面，强制 TCP、独立线程池、端口 9000），保证 IB 不可用时控制面仍可达：

```cpp
    CONFIG_OBJ(base, net::Server::Config, [](net::Server::Config &c) {
      c.set_groups_length(2);
      c.groups(0).listener().set_listen_port(8000);
      c.groups(0).set_services({"MetaSerde"});
      c.groups(1).set_network_type(net::Address::TCP);
      c.groups(1).listener().set_listen_port(9000);
      c.groups(1).set_use_independent_thread_pool(true);
      c.groups(1).set_services({"Core"});
    });
```

#### 4.1.4 代码实践

**实践目标**：用眼睛「证明」meta 服务是无状态的。

**操作步骤**：

1. 打开 [`MetaServer.cc`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/service/MetaServer.cc#L25-L81) 的 `beforeStart`，列出它在启动时创建的全部对象。
2. 对每个对象问一个问题：「如果这个 meta 进程现在被 `kill -9`，哪些数据会丢失？丢失后能不能从 FDB 重建？」
3. 重点看 `kvEngine_`、`mgmtdClient_`、`storageClient`、`metaOperator_` 这四个。

**需要观察的现象**：

- 你会发现 `beforeStart` 里**没有任何「从本地磁盘加载 inode 表」之类的步骤**；它只是创建客户端 + 创建 `MetaOperator` + `init()`。
- `metaOperator_->init(rootLayout)` 里只有当 `use_memkv` 为真（测试场景）时才会写入一个空 layout（[`MetaServer.cc:68-72`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/service/MetaServer.cc#L68-L72)）；生产环境下 `rootLayout` 为空，连「初始化文件系统」都不做——因为根目录 layout 早在 `admin_cli init-cluster` 时就写进 FDB 了（见 u1-l3）。

**预期结果**：你能用一句话回答「meta 进程被杀后，已创建的文件元数据会丢吗？」——答案是「不会，全在 FDB 里」。

#### 4.1.5 小练习与答案

**练习 1**：既然 meta 是无状态的，为什么 `MetaServer` 里还要持有 `mgmtdClient_` 和 `storageClient`？这两个客户端难道不是「状态」吗？

**参考答案**：它们是**通信句柄**（持有连接、路由缓存），不是**业务状态**。`mgmtdClient_` 用来向 mgmtd 续租、拉路由和配置（控制面）；`storageClient` 用来在需要时联系 storage（比如 GC 删除数据时）。进程重启后重新建这两个客户端即可，不依赖任何本地持久数据，所以不破坏「无状态」。

**练习 2**：`use_memkv` 配置项为什么默认是 `false`？打开它会怎样？

**参考答案**：`use_memkv=true` 时 KV 引擎换成纯内存实现，元数据**不落盘、进程退出即丢**，只用于单元测试（快、隔离）。生产环境必须用 FoundationDB，所以默认 `false`。源码里它甚至被标注为 `deprecated`（[`MetaServer.h:46`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/service/MetaServer.h#L46)），真正的后端选择由 `kv_engine`（`HybridKvEngineConfig`）决定。

---

### 4.2 操作分发

#### 4.2.1 概念说明

一个 RPC 请求到达 meta 进程后，要经历两层分发才能真正开始干活：

- **第一层：RPC 派发（MetaSerdeService）**。网络层按 `(serviceId, methodId)` 把请求交给 `MetaSerdeService` 的某个方法（`create`/`open`/`stat`/...）。这一层只做「转发」，几乎是透明的。
- **第二层：业务调度（MetaOperator）**。`MetaOperator` 收到请求后做三件事：**鉴权**、**分片判断（该不该我处理）**、**选择执行方式（普通事务 or 批处理）**。

其中最值得关注的是**分片判断**。虽然 meta 无状态、任何实例都能处理任何请求，但 3FS 仍然用 `Distributor` 把请求按 inode「粘」到固定实例上。原因有两点：

1. **批处理收益**：对同一个目录/文件的写操作（create、sync、close）集中到一个实例，可以合并成单个事务（见 4.2.3 的 `runInBatch`），大幅减少 FDB 事务数。
2. **串行化简化**：同一 inode 的并发写在同一个实例上更容易排队，避免跨实例的复杂协调。

`Distributor::getServer(inodeId)` 用一个一致性哈希（`Weight::select`）把 inode id 映射到当前活跃的 meta 节点列表中的一个。如果算出来「不是我自己」，就把请求 `forward` 给目标实例。

#### 4.2.2 核心流程

以 `create` 为例的调度流程（伪代码）：

```
MetaSerdeService::create(ctx, req)
  └─ meta_.create(req)                         # 透传
       MetaOperator::create(req)
         ├─ AUTHENTICATE(req.user)             # 鉴权
         ├─ runOp(&MetaStore::tryOpen, req)    # 先尝试打开已存在文件（O_CREAT|O_EXCL 语义）
         ├─ node = distributor_->getServer(req.path.parent)   # 该父目录归谁？
         ├─ if node == me:
         │     runInBatch<CreateReq,CreateRsp>(parentId, req) # 本地批处理执行
         └─ else:
               forward_->forward(node, req)    # 转发给正确的 meta 实例
```

注意 `runInBatch` 是「create/sync/close/setAttr」这类**对同一 inode 写**操作的统一入口；而 `stat/mkdirs/list/rename` 等则走更简单的 `runOp`（见 4.3）。

#### 4.2.3 源码精读

先看第一层派发。`MetaSerdeService` 用一个宏把「方法名 → 转发给 `MetaOperator`」批量生成：

[`MetaSerdeService.h`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/service/MetaSerdeService.h#L14-L19) —— 宏 `META_SERVICE_METHOD` 展开后，每个 RPC 方法都只是 `return meta_.NAME(req)`，一行透传：

```cpp
#define META_SERVICE_METHOD(NAME, REQ, RESP) \
  CoTryTask<RESP> NAME(serde::CallContext &, const REQ &req) { return meta_.NAME(req); }
  ...
  META_SERVICE_METHOD(create, CreateReq, CreateRsp);
```

这就是「服务面薄、逻辑都在 Operator」的典型写法。`MetaSerdeService` 本身被 `addSerdeService` 注册到 `net::Server`（[`MetaServer.cc:64`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/service/MetaServer.cc#L64)）。

再看第二层调度。`MetaOperator::create` 是理解「分发」最好的样本，它完整展示了「鉴权 → tryOpen → 分片判断 → 本地批处理 or 转发」四步：

[`MetaOperator.cc`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/service/MetaOperator.cc#L341-L369) —— 注意 `distributor_->getServer(req.path.parent)` 决定路由，`forward_->forward` 处理「不归我管」的情况：

```cpp
CoTryTask<CreateRsp> MetaOperator::create(CreateReq req) {
  AUTHENTICATE(req.user);
  CO_RETURN_ON_ERROR(req.valid());
  ...
  if (req.path.path->has_parent_path()) {
    auto result = co_await runOp(&MetaStore::tryOpen, req);   // 先尝试打开已存在文件
    ...
  }
  auto node = distributor_->getServer(req.path.parent);       // 该父目录归哪个 meta？
  if (node == distributor_->nodeId()) {
    auto parentId = req.path.parent;
    co_return co_await runInBatch<CreateReq, CreateRsp>(parentId, std::move(req));  // 本地批处理
  } else {
    co_return co_await forward_->forward<CreateReq, CreateRsp>(node, std::move(req)); // 转发
  }
}
```

`Distributor::getServer` 的实现非常短，就是一次一致性哈希选择：

[`Distributor.cc`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/Distributor.cc#L88-L91) —— `Weight::select(guard->active, inodeId)` 在当前活跃 meta 节点列表里选一个：

```cpp
flat::NodeId Distributor::getServer(InodeId inodeId) {
  auto guard = latest_.rlock();
  return Weight::select(guard->active, inodeId);
}
```

这份「活跃节点列表」存在 FDB 里（`Distributor` 周期性地把自己的心跳写进 FDB、并拉取别人的心跳，超时的节点被标记为 dead），所以即使 meta 无状态，多个实例对「谁是 active」也能达成一致。这又一次体现了「共享状态放 FDB」的设计。

批处理执行入口 `runInBatch`：它把请求塞进按 inode 分桶的等待队列，第一个请求负责真正跑事务，后续同 inode 的请求搭便车：

[`MetaOperator.cc`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/service/MetaOperator.cc#L116-L132) —— `addBatchReq` 决定「我是队首（要跑事务）还是跟随者（只等结果）」：

```cpp
template <typename Req, typename Rsp>
CoTryTask<Rsp> MetaOperator::runInBatch(InodeId inodeId, Req req) {
  ...
  BatchedOp::Waiter<Req, Rsp> waiter(std::move(req));
  auto op = addBatchReq(inodeId, waiter);
  co_await waiter.baton;                 // 等待被队首唤醒
  if (op) {
    co_await runBatch(inodeId, std::move(op), deadline);  // 队首才走到这里
  }
  auto result = waiter.getResult();
  ...
}
```

`runBatch` 再交给 `OperationDriver` 去开事务、提交（见 4.3）。

最后补一张「哪些操作走 `runOp`、哪些走 `runInBatch`」的对照表，方便你扫源码：

| 走 `runOp`（单请求单事务） | 走 `runInBatch`（按 inode 合并） |
| --- | --- |
| `statFs` / `stat` / `batchStat` / `getRealPath` / `mkdirs` / `symlink` / `remove` / `rename` / `list` / `hardLink` / `lockDirectory` | `create` / `sync` / `close` / `setAttr`（路径为 inode 时） |

规律是：**只读或操作「整棵子树/路径」的走 `runOp`；操作「单个文件 inode」且高频并发的写走 `runInBatch`**。

#### 4.2.4 代码实践

**实践目标**：验证「转发」真的会发生，并理解它为什么不影响正确性。

**操作步骤**：

1. 在 [`MetaOperator.cc`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/service/MetaOperator.cc#L330-L339) 的 `close` 和 `sync` 方法里，找到与 `create` 完全相同的「`getServer` → 本地 or forward」分支结构。
2. 思考：假设有 3 个 meta 实例 M1/M2/M3，client 把一个 `create` 请求发到了 M1，但 `Distributor` 算出这个父目录归 M3。M1 会怎么处理？
3. 追踪 `forward_->forward<CreateReq, CreateRsp>(node, req)` —— 它通过 `Forward` 组件（[`MetaOperator.h:36`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/components/Forward.h)）把请求当成普通 RPC 再发给 M3。

**需要观察的现象**：转发对 client 是**完全透明**的——client 只发了一次请求、只等一个回包，它不知道中途被转发了。

**预期结果**：你能解释「为什么转发不会破坏无状态语义」——因为转发后，最终处理请求的 M3 也是开一个 FDB 事务来干活，FDB 才是事实来源；M1 只是个中转。**待本地验证**：若你有测试集群，可以只起一个 meta 实例，观察此时 `distributor_->nodeId()` 恒等于 `getServer` 的结果，转发分支永远不走。

#### 4.2.5 小练习与答案

**练习 1**：`create` 里为什么先要 `runOp(&MetaStore::tryOpen, req)`，而不是直接进入批处理？

**参考答案**：为了正确实现 `O_CREAT` 的语义——如果文件已存在就直接返回（不报错），只有不存在时才真正创建。`tryOpen` 复用了 `OpenOp`（[`Open.cc:304-306`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/Open.cc#L304-L306)），先尝试打开；若已存在就早返回，省去批处理的开销。

**练习 2**：`sync` 和 `close` 方法里有一行注释 `// don't auth user for sync` / `// Note: don't auth user here`（[`MetaOperator.cc:320`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/service/MetaOperator.cc#L319-L339)）。猜猜为什么这两个操作不做用户鉴权？

**参考答案**：`sync`/`close` 是**已经打开文件的后续操作**，client 在 `open` 时已经鉴过权并拿到了 session。这两个操作主要用来上报写位置、更新文件长度、清理 session（见 u4-l5），属于「打开之后的管理动作」，因此不再重复鉴权，以降低热路径开销。

---

### 4.3 存储入口

#### 4.3.1 概念说明

请求经过 `MetaOperator` 调度后，最终要落到「在 FDB 事务里真正读写元数据」。这一层有三个角色：

- **`MetaStore`（操作入口）**：持有所有元数据组件（ChainAllocator、SessionManager、GcManager、InodeIdAllocator 等），对外提供 `open`/`stat`/`rename` 等工厂方法，每个方法返回一个 `Operation` 对象。它本身**不开事务**，只负责「造操作对象」。
- **`Operation`（操作对象）**：一个请求对应一个 `Operation`，它的核心方法是 `run(IReadWriteTransaction &txn)`——把业务逻辑写在这个协程里，参数就是 `MetaOperator` 替它开好的事务。`Operation` 还携带「我是只读的吗」「要不要幂等」「重试时清掉事件」等元信息。
- **`OperationDriver`（执行驱动）**：拿一个 `Operation` 和一个事务，跑「执行 → 出错就按 `FDBRetryStrategy` 重试 → 成功就提交」的主循环。它是重试逻辑的真正归宿（u2-l6 讲过 `FDBRetryStrategy`，这里是它的调用方）。

这三者用「**接口 + 模板**」解耦：`MetaStore` 不知道具体 `Operation` 类型，只通过 `IOperation<Rsp>` 接口调用；`OperationDriver` 也是模板，能驱动任意 `Rsp` 类型的操作。这种设计让「新增一个元数据操作」非常容易（见 u8-l4）。

#### 4.3.2 核心流程

`OperationDriver::run` 的重试主循环（伪代码）：

```
OperationDriver::run(txn, retryConfig, readonly, grvCache):
  strategy = FDBRetryStrategy(retryConfig)
  strategy.init(txn)
  result = kOperationTimeout
  while True:
    if deadline 到了: break           # 超时保护
    result = runAndCommit(txn, operation)   # 跑一次：执行 + 提交
    if success: break
    operation.retry(error)            # 让 Operation 清理本地的临时状态（事件/trace）
    strategy.onError(txn, error)      # 判断能否重试 + 退避
  operation.finish(result)            # 成功才记事件/trace
  return result
```

两个关键点：

1. **重试是「整段重跑」**：冲突后不是只重做失败的子步骤，而是把整个 `operation.run(txn)` 重新跑一遍（FDB 事务的乐观并发模型要求如此）。所以 `Operation::retry` 的职责是**清掉上一轮积累的临时状态**（如待记录的 Event），避免重复记录。
2. **提交时机统一**：只读操作不提交；读写操作在 `runAndCommit` 里 `co_await txn.commit()`。幂等操作还会额外存一份去重记录（见 `IDEMPOTENT_CHECK`）。

#### 4.3.3 源码精读

先看操作接口。`IOperation<Rsp>` 定义在 `MetaStore.h` 里，它规定了所有操作必须实现的契约：

[`MetaStore.h`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/MetaStore.h#L46-L70) —— 注意 `run(IReadWriteTransaction &)` 是纯虚函数，`operator()` 直接转发给 `run`：

```cpp
template <typename Rsp>
class IOperation {
 public:
  using RspT = Rsp;
  virtual bool isReadOnly() = 0;
  virtual bool retryMaybeCommitted() { return true; }
  virtual bool needIdempotent(Uuid &clientId, Uuid &requestId) const { ... return false; }
  virtual CoTryTask<Rsp> run(IReadWriteTransaction &) = 0;   // 核心业务逻辑
  virtual void retry(const Status &) = 0;                     // 重试前清理
  virtual void finish(const Result<Rsp> &) = 0;               // 成功后记事件
  CoTryTask<Rsp> operator()(IReadWriteTransaction &txn) { co_return co_await run(txn); }
};
```

`MetaStore` 本身就是一堆「工厂方法」，每个方法 `make_unique` 一个具体的 `Operation`。以 `open`/`tryOpen` 为例：

[`Open.cc`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/Open.cc#L300-L306) —— `MetaStore::open` 造一个 `OpenOp` 返回，自己不开事务：

```cpp
MetaStore::OpPtr<OpenRsp> MetaStore::open(OpenReq &req) {
  return std::make_unique<OpenOp<OpenReq, OpenRsp>>(*this, req);
}
MetaStore::OpPtr<CreateRsp> MetaStore::tryOpen(CreateReq &req) {
  return std::make_unique<OpenOp<CreateReq, CreateRsp>>(*this, req);   // create 复用 OpenOp
}
```

`MetaStore` 持有的组件清单（这些就是「可重建的运行期组件」）：

[`MetaStore.h`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/MetaStore.h#L147-L156) —— 全是 `shared_ptr`，状态最终都指向 FDB：

```cpp
  std::shared_ptr<Distributor> distributor_;
  std::shared_ptr<InodeIdAllocator> inodeAlloc_;
  std::shared_ptr<ChainAllocator> chainAlloc_;
  std::shared_ptr<FileHelper> fileHelper_;
  std::shared_ptr<SessionManager> sessionManager_;
  std::shared_ptr<GcManager> gcManager_;
  AclCache aclCache_;   // 2M 项的 ACL 缓存，纯内存、可重建
```

接着看 `MetaOperator::runOp`——这是「开事务 → 造操作 → 交给驱动」的三行核心：

[`MetaOperator.cc`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/service/MetaOperator.cc#L70-L87) —— 注意三步：`createReadWriteTransaction`、`((*metaStore_).*func)(arg)` 造操作、`OperationDriver(...).run(...)` 驱动：

```cpp
template <typename Func, typename Arg>
auto MetaOperator::runOp(Func &&func, Arg &&arg)
    -> CoTryTask<...> {
  ...
  auto txn = kvEngine_->createReadWriteTransaction();          // ① 开事务
  auto op = ((*metaStore_).*func)(std::forward<Arg>(arg));     // ② 造 Operation
  auto driver = OperationDriver(*op, arg, deadline);
  co_return co_await driver.run(std::move(txn), createRetryConfig(),
                                config_.readonly(), config_.grv_cache());  // ③ 驱动执行
}
```

最后看重试主循环本体——这是整个 meta 操作执行的「心脏」：

[`Operation.h`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Operation.h#L176-L198) —— `while(true)` 里「跑 → 检查超时 → 出错就 retry + 退避」，成功才 `break`：

```cpp
    Result<Rsp> result = makeError(MetaCode::kOperationTimeout);
    auto duplicate = false;
    while (true) {
      if (deadline_ && deadline_.value() <= SteadyClock::now()) {     // 超时保护
        XLOGF(ERR, "Request {} timeout, return error {}", describe(), result);
        break;
      }
      result = co_await runAndCommit(*txn, operation_, duplicate);     // 执行 + 提交
      if (ErrorHandling::success(result)) {
        break;                                                         // 成功，退出
      }
      XLOGF(WARN, "Request {} failed, error {}", describe(), result.error());
      operation_.retry(result.error());                                // 让 Operation 清理
      auto retry = co_await strategy.onError(txn.get(), result.error()); // 判断能否重试
      if (retry.hasError()) {
        result = makeError(retry.error());
        break;                                                         // 不可重试，退出
      }
      recorder.retry()++;
    }
```

`runAndCommit` 决定「什么时候提交」：

[`Operation.h`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Operation.h#L243-L250) —— 非只读操作成功后 `txn.commit()`：

```cpp
    } else {
      auto result = co_await handler(txn);
      if (!result.hasError() && !readonly) {
        CO_RETURN_ON_ERROR(co_await txn.commit());      // 读写操作才提交
      }
      co_return result;
    }
```

值得一提的是，meta 里还有个更通用的重试工具 `kv::WithTransaction`（用于不需要「操作对象」的简单事务，比如 `MetaOperator::start` 里清理幂等记录的后台任务）：

[`WithTransaction.h`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/kv/WithTransaction.h#L49-L62) —— 同样是「跑 handler → 失败按策略重试」的循环，是 `OperationDriver` 的轻量版：

```cpp
  template <typename Handler>
  std::invoke_result_t<...> run(IReadWriteTransaction &txn, Handler &&handler) {
    auto result = strategy_.init(&txn);
    CO_RETURN_ON_ERROR(result);
    while (true) {
      auto result = co_await runAndCommit(txn, std::forward<Handler>(handler));
      if (!result.hasError()) { co_return std::move(result); }
      auto retryResult = co_await strategy_.onError(&txn, std::move(result.error()));
      CO_RETURN_ON_ERROR(retryResult);
    }
  }
```

对比两者：`WithTransaction` 只管「跑 handler + 重试」，适合后台任务；`OperationDriver` 多了「只读判断、幂等去重、事件/trace 记录、操作命名埋点」等业务能力，适合 RPC 操作。

#### 4.3.4 代码实践

**实践目标**：把「存储入口」三件套（`MetaStore` → `Operation` → `OperationDriver`）在源码里串起来，亲手走一遍 `stat`（最简单的只读操作）。

**操作步骤**：

1. 从 [`MetaOperator.cc:289-292`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/service/MetaOperator.cc#L289-L292) 的 `stat` 进入，看到它调用 `runOp(&MetaStore::stat, req)`。
2. 跳到 `runOp`（[`MetaOperator.cc:70-87`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/service/MetaOperator.cc#L70-L87)），确认它造的 `op` 来自 `MetaStore::stat`。
3. 打开 [`MetaStore.h`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/MetaStore.h#L106) 的 `OpPtr<StatRsp> stat(const StatReq &req)`，再去看 `src/meta/store/ops/Stat.cc` 里 `StatOp` 的 `run(IReadWriteTransaction &txn)`——确认它只在事务里 `get` inode，不 `commit`（因为 `ReadOnlyOperation::isReadOnly()` 为 true）。
4. 回到 `OperationDriver::run`（[`Operation.h:178-198`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Operation.h#L178-L198)），确认只读路径里 `runAndCommit` 不会调用 `txn.commit()`。

**需要观察的现象**：

- `stat` 全程不写 FDB，只读。`OperationDriver` 对只读操作的处理与读写操作在「提交」这一步上分叉。
- 如果发生 `kConflict`/`kTooOld`（别的请求改了同一 key），`strategy.onError` 会决定重试，整个 `run` 重跑——但因为 `stat` 没有副作用，重跑是安全的。

**预期结果**：你能画出 `stat` 的最小调用链：

```
MetaSerdeService::stat → MetaOperator::stat → runOp
  → MetaStore::stat (造 StatOp)
  → OperationDriver::run (开事务 / 跑 StatOp::run / 不提交 / 可能重试)
```

#### 4.3.5 小练习与答案

**练习 1**：`OperationDriver::run` 里为什么要有 `deadline_` 超时保护？`FDBRetryStrategy` 不是已经会退避重试了吗？

**参考答案**：`FDBRetryStrategy` 决定「**能不能**重试」和「退避多久」，但它不会主动放弃。如果两个请求持续冲突（活锁），没有 `deadline_` 就会无限重试。`deadline_`（由 `config_.operation_timeout()` 设置，[`MetaOperator.cc:79-81`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/service/MetaOperator.cc#L79-L81)）是兜底，到点就返回 `kOperationTimeout`，把错误抛给 client 让它决定下一步。

**练习 2**：`Operation::finish` 里有一段 `if (!result.hasError()) { ... 记录 events_/traces_ ... }`（[`Operation.h:57-69`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Operation.h#L57-L69)）。为什么事件要等操作**成功**后才记录，而 `retry()` 又要先清空事件？

**参考答案**：一个操作可能因冲突被重试多次。如果每轮都记事件，同一次请求会被记多遍、污染监控数据。所以设计成「重试时清空（`retry` 调 `clearEvents`）、最终成功后再统一记录（`finish`）」——保证一次请求只产出一份事件/trace。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面的「**create file RPC 全链路追踪 + 分层图**」任务。

**任务背景**：client 调用 `open(path, O_CREAT|O_WRONLY)` 创建一个新文件。这次 RPC 会一路穿过 meta 的所有分层，最终把一个新 inode 和目录项写进 FoundationDB。

**请你完成**：

1. **画出分层时序图**。从「client 发出 create RPC」开始，到「FDB 事务 commit 成功」结束，标出每一跳所在的文件与关键函数。至少应包含以下节点（按顺序）：
   - `net::Server` 收到请求，按 methodId 派发
   - `MetaSerdeService::create`（[`MetaSerdeService.h:19`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/service/MetaSerdeService.h#L19)）
   - `MetaOperator::create`：鉴权 → `tryOpen` → `distributor_->getServer`（[`MetaOperator.cc:341-369`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/service/MetaOperator.cc#L341-L369)）
   - `runInBatch` → `runBatch`（[`MetaOperator.cc:116-132`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/service/MetaOperator.cc#L116-L132)）
   - `OperationDriver::run` 的重试循环（[`Operation.h:178-198`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Operation.h#L178-L198)）
   - `BatchedOp::run` / `OpenOp` 在事务里写入 inode + DirEntry（参考 [`Open.cc:212-233`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/Open.cc#L212-L233) 的 `createInodeAndEntry`）
   - `txn.commit()` 落 FDB（[`Operation.h:243-250`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Operation.h#L243-L250)）

2. **回答三个串联问题**：
   - 这次 create 操作是走 `runOp` 还是 `runInBatch`？为什么选这条路径？
   - 如果在 `OperationDriver::run` 里第一次 `runAndCommit` 返回了 `kConflict`（比如另一个 client 同时在同一个目录创建文件），会发生什么？最终 client 会感知到冲突吗？
   - 假设处理这个请求的 meta 实例在 `txn.commit()` **之后**、回包**之前**崩溃了，FDB 里已经有这个新文件了吗？client 会怎样？（提示：结合 u2-l6 的 `maybe_committed`。）

3. **进阶（可选）**：用 `git grep -n "runInBatch" src/meta` 找出所有使用批处理的操作，验证 4.2.3 给出的对照表。

**预期产出**：一张分层图 + 三段文字回答。如果某些行为你无法从源码确定（比如 client 侧重试策略），明确标注「待本地验证」。

## 6. 本讲小结

- **meta 是无状态服务**：进程不持久化任何业务元数据，inode/目录项/session/布局全在 FoundationDB；`MetaServer` 的成员里没有任何「文件表」，只有 KV 引擎和客户端句柄。崩溃/重启不丢数据，升级可逐实例重启。
- **`IKVEngine` 是状态边界**：meta 通过 `kv::IKVEngine`（生产环境为 `HybridKvEngine`→FDB）访问外部存储，业务代码不直接耦合 FDB C API，测试可换 `MemKV`。
- **两层分发**：`MetaSerdeService` 把 RPC 透传给 `MetaOperator`；`MetaOperator` 做鉴权、`Distributor` 分片判断（归我就本地跑，不归我就 `forward` 转发，转发对 client 透明）。
- **两种执行路径**：只读/路径型操作走 `runOp`（单请求单事务）；对单 inode 的高频写（create/sync/close/setAttr）走 `runInBatch`（按 inode 合并事务，减少 FDB 压力）。
- **存储入口三件套**：`MetaStore`（持有组件、造 Operation）→ `Operation`（在事务里跑业务逻辑、声明只读/幂等）→ `OperationDriver`（开事务、跑、提交、按 `FDBRetryStrategy` 整段重试、超时兜底、成功后记事件）。
- **重试是整段重跑**：冲突后整个 `Operation::run` 重新执行，所以 `retry()` 负责清空临时状态、`finish()` 只在最终成功后记事件。

## 7. 下一步学习建议

本讲建立了 meta 的「骨架与分层」，接下来按依赖关系深入血肉：

- **u4-l2 Inode 与 DirEntry 的 KV 编码**：本讲只说「元数据存在 FDB」，但没说**怎么存**。下一讲讲 `INOD`/`DENT`/`INOS` 前缀、字节序选择与范围查询，是理解所有 Operation 内部行为的基础。
- **u4-l3 用 FoundationDB 事务实现元数据操作**：把本讲的 `OperationDriver` 和具体操作（rename/remove）的冲突范围、重试结合起来，是 u2-l6 在 meta 侧的落地。
- **u4-l4 文件数据布局与链分配**：本讲提到的 `ChainAllocator`（在 `MetaStore` 里）如何为新文件选链，串联起 meta 与 storage/u3-l4。
- **u4-l5 动态文件长度、FileSession 与 GC**：解释 `sync`/`close` 为什么走 `runInBatch`、`SessionManager`/`GcManager` 如何工作。

建议在进入 u4-l2 前，先把本讲「综合实践」的分层图画出来——它会是你阅读后续讲义时反复回查的地图。
