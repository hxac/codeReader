# 四大组件与端到端请求链路总览

## 1. 本讲目标

前三讲我们已经分别认识了 3FS 是什么（u1-l1）、仓库如何构建（u1-l2）、如何部署一个测试集群（u1-l3）。本讲要把这些「零件」串成一台能运转的机器。

读完本讲，你应该能够：

- 说清楚一次 `open / read / write` 操作依次穿过哪些组件、各发了一次什么 RPC。
- 解释为什么 **meta 服务不在数据热路径上**——这是 3FS 高吞吐的根本前提。
- 理解 mgmtd 如何用一个「版本号 + 拉取」的机制，让 client / meta / storage 看到同一份集群视图。
- 看懂四个服务的 `main` 入口，知道后续深入任何一个组件时该从哪里下手。

本讲是一张「全局地图」，细节会被后续单元填充，目标是建立方向感。

## 2. 前置知识

在开始之前，请确认你已经理解下面这些来自前几讲（u1-l1、u1-l3）的概念：

- **四大组件**：cluster manager（mgmtd）、metadata service（meta）、storage service（storage）、client（FUSE 守护进程或原生客户端）。
- **mgmtd 是「发现服务」**：所有进程加入集群都要先找到它，从它那里拿到路由信息。
- **配置托管**：运行时配置存在 FoundationDB 里，由 mgmtd 统一下发。
- **CRAQ**：storage 采用的链式复制协议，特点是「写全读任何（write-all-read-any）」。

另外补充一个本讲会反复用到的关键直觉：

> **数据布局（layout）**。一个 3FS 文件被切成等大的 chunk，每个 chunk 由 `chunk id` 唯一标识。`chunk id` 由「文件的 inode id」和「chunk 在文件中的序号（chunk index）」拼接得到。文件创建时，meta 会按 stripe size 把若干条复制链（chain）轮询分配给这个文件，并用一个随机种子打乱。这些信息——chain table、chunk size、stripe size、shuffle seed、已经分配到的链范围——就构成了文件的 **layout**，它被存在该文件的 inode 里。

理解了 layout，就能理解本讲最核心的一句话：**只要拿到 layout，client 就能自己算出任意一段数据落在哪个 chunk、哪条链上，从而直接去找 storage，不再需要打扰 meta。**

## 3. 本讲源码地图

本讲涉及的文件分为三组，对应三个最小模块：

| 文件 | 作用 | 所属模块 |
| --- | --- | --- |
| `src/mgmtd/mgmtd.cpp` | mgmtd 服务入口 | 组件入口 |
| `src/meta/meta.cpp` | meta 服务入口 | 组件入口 |
| `src/storage/storage.cpp` | storage 服务入口 | 组件入口 |
| `src/fuse/hf3fs_fuse.cpp` | FUSE 客户端入口（结构与前三者不同） | 组件入口 |
| `src/mgmtd/ops/GetRoutingInfoOperation.h` | 「拉取路由信息」RPC 定义 | 路由分发 |
| `src/mgmtd/background/MgmtdRoutingInfoVersionUpdater.cc` | 后台推进路由版本号 | 路由分发 |
| `src/fuse/FuseClients.h` | 客户端聚合 mgmtd/meta/storage 三类子客户端 | 请求链路 |
| `src/fuse/FuseOps.cc` | FUSE 请求分发（`open` / `read` 等回调） | 请求链路 |
| `src/meta/service/MetaOperator.h` | meta 侧 RPC handler 集合 | 请求链路 |
| `src/storage/service/StorageOperator.h` | storage 侧 RPC handler 集合 | 请求链路 |
| `docs/design_notes.md` | 官方设计说明，权威依据 | 全讲 |

## 4. 核心概念与源码讲解

### 4.1 组件入口：四个服务的 main 函数

#### 4.1.1 概念说明

3FS 是一个由多个独立进程组成的分布式系统。mgmtd、meta、storage 各自是一个可执行文件，client（这里以 FUSE 守护进程为代表）又是一个可执行文件。要理解「请求怎么流动」，第一步是看清楚每个进程是怎么启动起来的、启动后手里握着哪些对象。

关键观察：**四个入口里，三个长得几乎一模一样，只有 FUSE 客户端是特殊的。** 这种「高度一致」不是巧合，而是因为它们共用同一套服务骨架（这套骨架会在 u2-l1「服务骨架」中深入讲解）。

#### 4.1.2 核心流程

三个服务（mgmtd / meta / storage）的启动流程是统一的「两阶段」模型：

1. **launcher 阶段**：解析命令行与本地配置、初始化日志、拉起 RDMA/网络等基础设施。
2. **app 阶段**：执行具体服务自己的启动逻辑（注册 RPC、连 mgmtd、加载数据……），随后进入运行循环。

这三者都被压成了一行代码，差别只在尖括号里的服务类型。

FUSE 客户端则不同，它必须和内核的 FUSE 模块打交道，启动顺序更细致，所以没有套用统一骨架，而是自己写了一个 `main`：

1. 初始化配置 → 启动 InfiniBand 管理器（`IBManager::start`）。
2. 初始化日志 → 启动监控（`monitor::Monitor::start`）。
3. 解析物理机/容器主机名，构造 `AppInfo`（集群身份）。
4. **构造三类子客户端**（mgmtd / storage / meta 客户端）——这是它能发出后续所有 RPC 的根基。
5. 进入 `fuseMainLoop`，挂载文件系统、监听内核请求。

#### 4.1.3 源码精读

三个服务入口，结构完全一致，只差服务类型：

[meta.cpp:5-8](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/meta.cpp#L5-L8) —— meta 服务入口，把启动委托给 `TwoPhaseApplication<MetaServer>`。

```cpp
int main(int argc, char *argv[]) {
  using namespace hf3fs;
  return TwoPhaseApplication<meta::server::MetaServer>().run(argc, argv);
}
```

[storage.cpp:5-8](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/storage.cpp#L5-L8) 把 `MetaServer` 换成 `StorageServer`；[mgmtd.cpp:5-8](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/mgmtd.cpp#L5-L8) 换成 `MgmtdServer`。三处代码加起来不到 10 行，却定义了整个系统的「启动契约」。

FUSE 客户端则是另一番景象。[hf3fs_fuse.cpp:39-81](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/hf3fs_fuse.cpp#L39-L81) 是它完整的 `main`，手动完成了 IB、日志、监控的初始化。其中最关键的一步是构造并初始化三类子客户端：

[hf3fs_fuse.cpp:70-73](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/hf3fs_fuse.cpp#L70-L73) —— 初始化 `FuseClients`，内部会建好 mgmtd / storage / meta 三类客户端。

```cpp
auto &d = getFuseClientsInstance();
if (auto res = d.init(appInfo, hf3fsConfig.mountpoint(), hf3fsConfig.token_file(), hf3fsConfig); !res) {
  XLOGF(FATAL, "Init fuse clients failed: {}", res.error());
}
```

这三类客户端分别定义在 `FuseClients` 里：

[FuseClients.h:196-198](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseClients.h#L196-L198) —— 一个 FUSE 守护进程同时持有与三个服务通信的客户端。

```cpp
std::shared_ptr<client::MgmtdClientForClient> mgmtdClient;
std::shared_ptr<storage::client::StorageClient> storageClient;
std::shared_ptr<meta::client::MetaClient> metaClient;
```

> 记住这三个成员：本讲后面的每一次请求，都要先想清楚「这次调用走的是 `mgmtdClient`、`metaClient` 还是 `storageClient`」。

#### 4.1.4 代码实践

**实践目标**：用对比的方式建立「四个入口」的直觉。

**操作步骤**：

1. 打开上面三个服务入口链接，确认它们只有模板参数不同。
2. 打开 FUSE 入口链接，对照本节列出的 5 步启动流程，在源码里逐一找到对应的代码行（如 `IBManager::start` 在第 44 行，`monitor::Monitor::start` 在第 53 行）。

**需要观察的现象**：三个服务入口几乎「无事可做」，全部委托给统一骨架；FUSE 入口则手写了一整套初始化。

**预期结果**：你能用一句话回答「为什么 FUSE 客户端不套用 `TwoPhaseApplication`」——因为它需要和 FUSE 内核模块、IB 设备强耦合，初始化顺序特殊。这是真实源码可验证的结论。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `TwoPhaseApplication` 看作一个模板，meta / storage / mgmtd 三者「不同」的部分被抽到了哪里？
**答案**：被抽到了各自的 `Server` 类（`MetaServer` / `StorageServer` / `MgmtdServer`）里，作为模板参数注入。`main` 本身完全一致。

**练习 2**：FUSE 客户端为什么需要同时持有 `mgmtdClient`、`metaClient`、`storageClient` 三个客户端？
**答案**：因为它要分别完成「发现集群路由（mgmtd）」「读写文件元数据（meta）」「读写文件数据（storage）」三类工作，三类 RPC 对应三个不同服务。

---

### 4.2 路由分发：mgmtd 如何让所有人看到同一份集群视图

#### 4.2.1 概念说明

集群里的节点会上下线、SSD 会故障、链的成员会变。每一次这样的变化，都必须让 client / meta / storage **都看得到**，否则请求就会发到错误的节点。mgmtd 就是负责维护和分发这份「集群视图」（即 RoutingInfo）的角色。

RoutingInfo 里大致包含四张表：节点表（NodeMap）、链表（ChainMap）、链表集合（ChainTableMap）、存储目标表（TargetMap）。这些概念会在 u3「集群管理服务 mgmtd」中详解，本讲只需知道：**它是一份描述「谁在集群里、数据该怎么放」的全局信息。**

mgmtd 用一个朴素但高效的机制做分发：**版本号 + 拉取（pull）**。

- mgmtd 维护一个单调递增的 `routingInfoVersion`。
- 任何一方（client / meta / storage）手里都缓存着「自己上次看到的版本号」。
- 它们周期性地用 `GetRoutingInfo(我的版本号)` 去问 mgmtd。如果版本号一致，mgmtd 可以很便宜地告诉它「你已经是最新了」；不一致才返回完整的最新 RoutingInfo。

这样就把「主动推送给所有人」的复杂度，降成了「各方按需拉取」。

#### 4.2.2 核心流程

那么 `routingInfoVersion` 是怎么被「推高」的？这靠 mgmtd 内部的一个后台任务 `MgmtdRoutingInfoVersionUpdater`，而且 **只有 primary mgmtd 才能做这件事**（关于 primary 选举见 u3-l3）。流程是：

1. 某个事件（节点心跳超时、管理员下线 target 等）把内存里的 `routingInfoChanged` 标志置为 true。
2. 后台任务周期性加写锁，检查这个标志。
3. 若需要变更：
   - `updateStoredRoutingInfo`：把新的 RoutingInfo **持久化到 FoundationDB**（这样 primary 切换也不丢）。
   - `updateMemoryRoutingInfo`：更新内存里的 RoutingInfo 并 **推高版本号**。
4. 此后各方下一次 `GetRoutingInfo` 时就会发现版本号变了，从而拉取新视图。

用伪代码概括：

```
loop (background, primary only):
    加写锁
    if routingInfoChanged:
        持久化到 FDB
        更新内存视图，version += 1
        清除 changed 标志
```

#### 4.2.3 源码精读

后台任务的核心逻辑非常短：

[MgmtdRoutingInfoVersionUpdater.cc:13-26](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdRoutingInfoVersionUpdater.cc#L13-L26) —— 检查 `routingInfoChanged`，若变更则先持久化、再更新内存视图。

```cpp
auto handle(MgmtdState &state) -> CoTryTask<void> {
  auto writerLock = co_await state.coScopedLock<"BumpRoutingInfoVersion">();
  bool needChange = co_await [&]() -> CoTask<bool> {
    auto dataPtr = co_await state.data_.coSharedLock();
    co_return dataPtr->routingInfo.routingInfoChanged;
  }();

  if (needChange) {
    CO_RETURN_ON_ERROR(co_await updateStoredRoutingInfo(state, *this));
    co_await updateMemoryRoutingInfo(state, *this);
  }
  co_return Void{};
}
```

注意「先持久化、再更新内存」的顺序——它保证了任何能看到新版本号的方，都一定能从 FDB 读到对应的数据，不会出现「版本号进了、数据还没落盘」的窗口。

而「只有 primary 才能做」这件事，体现在调用入口：

[MgmtdRoutingInfoVersionUpdater.cc:32-43](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/background/MgmtdRoutingInfoVersionUpdater.cc#L32-L43) —— 用 `doAsPrimary` 包裹整个操作；非 primary 实例会收到 `kNotPrimary` 并跳过。

```cpp
auto res = co_await doAsPrimary(state_, std::move(handler));
if (res.hasError()) {
  if (res.error().code() == MgmtdCode::kNotPrimary)
    LOG_OP_INFO(op, "self is not primary, skip");
  ...
}
```

各方拉取路由信息则走这个 RPC：

[GetRoutingInfoOperation.h:9-18](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/GetRoutingInfoOperation.h#L9-L18) —— 请求里带着调用方已知的版本号 `req.routingInfoVersion`，服务端据此决定是返回「已最新」还是返回完整 RoutingInfo。

```cpp
struct GetRoutingInfoOperation ... {
  GetRoutingInfoReq req;
  explicit GetRoutingInfoOperation(GetRoutingInfoReq r) : req(std::move(r)) {}
  String toStringImpl() const final { return fmt::format("GetRoutingInfo for {}", req.routingInfoVersion); }
  CoTryTask<GetRoutingInfoRsp> handle(MgmtdState &state);
};
```

#### 4.2.4 代码实践

**实践目标**：理解版本号拉取的「便宜之处」。

**操作步骤**：

1. 在仓库中搜索 `GetRoutingInfoRsp` 的定义（提示：在 `src/fbs/mgmtd/` 下），观察它的字段里是否同时包含「版本号」和「完整路由信息」以及一个表示「是否已是最新」的标志位。
2. 搜索客户端侧调用 `getRoutingInfo` 的地方（如 `src/client/mgmtd/`），看客户端是如何把本地缓存的版本号传上去、又如何在收到「已最新」时避免无谓刷新的。

**需要观察的现象**：当集群稳定时，`GetRoutingInfo` 的响应体可以非常小（只回一个版本号 + 「up to date」），不会反复搬运整张路由表。

**预期结果**：你能解释为什么这种「版本号 + 拉取」设计在大集群下也不会成为瓶颈——稳态开销接近于一次心跳。具体字段命名以本地源码为准，若与上述描述不符请以源码为准（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么「先 `updateStoredRoutingInfo` 再 `updateMemoryRoutingInfo`」的顺序不能反过来？
**答案**：反过来会出现「内存版本号已是 N，但 FDB 里还是 N-1」的窗口。此时若 primary 切换，新 primary 从 FDB 只能加载到 N-1，已经看到 N 的方会拿到过期数据。先落盘可保证「版本号可见 ⇒ 数据可读」。

**练习 2**：多个 mgmtd 实例同时运行时，谁有权力修改 RoutingInfo？
**答案**：只有被选为 primary 的那一个。代码用 `doAsPrimary` 保证非 primary 实例直接跳过，避免多写冲突。

---

### 4.3 请求链路：一次 open / read / write 的完整旅程

#### 4.3.1 概念说明

这是本讲最重要的一节。我们把前面两节拼起来，回答一个具体问题：**应用程序调用 `open` / `read` / `write` 时，到底发生了什么？**

核心设计原则来自官方设计文档：

[design_notes.md:59](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L59) —— 「应用打开文件时，client 向 meta 请求文件的数据布局；此后 client 可以自行计算 chunk id 和所属链，从而把 meta 排除在关键路径之外。」

这句话直接决定了 3FS 的性能模型：

- **`open`（以及 `create`）走 meta**：client 需要从 meta 拿到 inode 和 layout。
- **`read` / `write` 走 storage**：拿到 layout 后，client 自己算出数据在哪条链的哪个 target 上，直接和 storage 通信。
- **mgmtd 只在「视图变更」时介入**，不在每次请求里出现。

简单说：**meta 管命名与布局，storage 管数据本身，mgmtd 管谁是谁。三者分工，使读写吞吐可以随 SSD 数量与网络带宽线性扩展。**

#### 4.3.2 核心流程

下面用三条主线描述一次完整的读写。

**主线一：`open`（打开文件）**

```
应用 open()
  → Linux 内核 FUSE 模块
  → 用户态 FUSE 守护进程 hf3fs_open()
  → MetaClient.open(inode)            [RPC: MetaService.open]
  → meta 服务 MetaOperator.open()
  → FoundationDB 只读/读写事务（读 inode、必要时建 FileSession）
  → 返回 inode + layout 给 client
  → client 缓存 inode，回复内核 open 成功
```

注意：若是写打开，client 还会生成一个 `session`（FileSession）并在 meta 登记，用于后续长度上报与延迟删除（详见 u4-l5）。

**主线二：`read`（读数据）**

```
应用 read(fd, buf, off, size)
  → 内核 FUSE → hf3fs_read()
  → 用 inode 的 layout 把 [off, off+size) 拆成若干 chunk
     （chunk index = off / chunkSize；chunk id = (inodeId, chunk index)）
  → 用 layout 里的链信息选出某个 target
  → StorageClient 批量发起读 [RPC: StorageService.batchRead]
  → storage 服务 StorageOperator.batchRead()
  → 分配 RDMA buffer、AIO 从 SSD 读、经 RDMA 回传
  → client 把数据拷进应用 buffer，回复内核
```

关键：**这一整条链路里没有 meta，也没有 mgmtd。** client 完全靠 `open` 时拿到的 layout 自己导航。

**主线三：`write`（写数据）**

写路径与读类似地绕开 meta，但要遵循 CRAQ 的链式规则（详见 u5-l3）：

```
应用 write()
  → 内核 FUSE → hf3fs_write() → StorageClient
  → 写请求发给 chain 的 head target  [RPC: StorageService.update]
  → head 加锁、生成 pending 版本，沿链转发到 successor
  → 到达 tail 后提交，ACK 沿链反向传回
  → client 收到 ACK，写完成
```

CRAQ 的「写全读任何」特性意味着：读可以打到链上任意 target，写则必须在 head 串行化后沿链传播。这正是 [design_notes.md:105](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L105) 所述。

#### 4.3.3 源码精读

先看 client 侧 FUSE 守护进程如何分发 `open`：

[FuseOps.cc:1418-1471](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseOps.cc#L1418-L1471) —— `hf3fs_open` 的关键调用，写/读写打开时走 `metaClient->open`。

```cpp
Uuid session;
if ((fi->flags & O_ACCMODE) == O_WRONLY || (fi->flags & O_ACCMODE) == O_RDWR) {
  session = meta::client::SessionId::random();
  auto res = withRequestInfo(req, d.metaClient->open(userInfo, ino, std::nullopt, session, fi->flags));
  ...
}
```

可以看到：**只有写/读写打开才会调用 meta 的 `open`**，纯读打开连这一步 RPC 都可以省（这也呼应了 design_notes 里「不为只读 fd 跟踪会话」的设计）。返回的 inode 里就带着 layout。

再看 `read`，最能体现「绕开 meta、直连 storage」：

[FuseOps.cc:1473-1542](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fuse/FuseOps.cc#L1473-L1542) —— `hf3fs_read` 用 `PioV` 把读请求交给 `storageClient`，全程不碰 metaClient。

```cpp
PioV ioExec(*d.storageClient, config.chunk_size_limit(), res);
auto retAdd = ioExec.addRead(0, inode, 0, off, size, memh.data(), memh);
...
auto retExec = withRequestInfo(req, ioExec.executeRead(userInfo, d.config->storage_io().read()));
```

`PioV`（Parallel IO Vector）把 `[off, off+size)` 按 inode 的 layout 切成多个 chunk 读，再并行交给 `storageClient` 发往对应 target。这就是「client 自己算 chunk id、自己选 target」的落点。

服务端这边，meta 和 storage 各有一个「RPC handler 集合」负责接住这些请求：

[MetaOperator.h:64-90](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/service/MetaOperator.h#L64-L90) —— meta 侧 handler，每一个方法对应一个 meta RPC：`stat` / `open` / `close` / `create` / `remove` / `rename` / `list` / `truncate` / `sync` …

```cpp
CoTryTask<StatRsp> stat(StatReq req);
CoTryTask<OpenRsp> open(OpenReq req);
CoTryTask<CreateRsp> create(CreateReq req);
CoTryTask<RenameRsp> rename(RenameReq req);
...
```

[StorageOperator.h:70-98](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/StorageOperator.h#L70-L98) —— storage 侧 handler，对应数据读写与维护类 RPC。

```cpp
CoTryTask<BatchReadRsp> batchRead(...);
CoTryTask<WriteRsp> write(...);
CoTryTask<UpdateRsp> update(...);
CoTryTask<QueryLastChunkRsp> queryLastChunk(...);
...
```

把两侧 handler 名字和前面三条主线对照，就能精确说出每条 RPC 落到哪个函数：`open` → `MetaOperator.open`，`read` → `StorageOperator.batchRead`，`write` → `StorageOperator.update`。

#### 4.3.4 代码实践

**实践目标**：把「client 调用 → 服务端 handler」的对应关系在源码里亲手对一遍。

**操作步骤**：

1. 在 `src/fuse/FuseOps.cc` 中找到 `hf3fs_open`、`hf3fs_read`，确认它们分别调用的是 `d.metaClient->...` 还是 `d.storageClient->...`（提示：`open` 在 1452 行用 `metaClient`，`read` 在 1525 行用 `storageClient`）。
2. 在 `src/meta/service/MetaOperator.h` 与 `src/storage/service/StorageOperator.h` 中找到同名/对应的 handler 方法。
3. 列一张三列表格：**FUSE 回调 → 用到的 client → 服务端 handler**。

**需要观察的现象**：`read` 回调里出现的是 `storageClient` 和 `PioV`，**不出现** `metaClient`；`open` 回调里出现的是 `metaClient`。

**预期结果**：你会得到一张类似下表的对应关系（这是从真实源码归纳的结论）：

| 应用调用 | FUSE 回调 | 使用的 client | 服务端 handler | 是否在数据热路径 |
| --- | --- | --- | --- | --- |
| `open`（写打开） | `hf3fs_open` | `metaClient` | `MetaOperator.open` | 否（仅打开时） |
| `read` | `hf3fs_read` | `storageClient` | `StorageOperator.batchRead` | 是 |
| `write` | `hf3fs_write` | `storageClient` | `StorageOperator.update` | 是 |

#### 4.3.5 小练习与答案

**练习 1**：假设 meta 服务全部下线，已经 `open` 过的文件还能不能继续 `read`？为什么？
**答案**：能。`read` 完全依赖 `open` 时已经拿到并缓存的 inode/layout，由 `storageClient` 直连 storage。meta 下线只影响「新打开文件」「创建/重命名」等元数据操作。

**练习 2**：为什么 `read` 请求可以发给链上任意一个 target，而 `write` 必须发给 head？
**答案**：CRAQ 的 read-any 利用所有副本的读带宽；而写需要严格串行化以保证一致，所以只能在 head 加锁、生成 pending 版本后沿链传播，由 tail 提交。见 [design_notes.md:105](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L105)。

**练习 3**：`chunk id` 由哪两部分组成？为什么 client 拿到 layout 后就能自己算出来？
**答案**：由「inode id」和「chunk index（= offset / chunkSize）」组成。因为 layout 里含有 chunkSize 和文件起始的链分配信息，给定 offset 就能算出 chunk index，进而算出 chunk id 与所属链。这正是把 meta 排除在热路径之外的关键。

## 5. 综合实践

**任务：绘制一张端到端时序图，串起本讲全部内容。**

请你结合本讲引用的所有 `main` 入口与 handler，画一张覆盖 `open / read / write` 的时序图，要求：

1. **参与者**至少包含：应用进程、FUSE 内核模块、FUSE 守护进程（含 `metaClient` / `storageClient`）、meta 服务、storage 服务、mgmtd。
2. **标出每条箭头对应的 RPC**，例如 `MetaService.open`、`StorageService.batchRead`、`StorageService.update`、`GetRoutingInfo`。
3. **用虚线标出 mgmtd 的角色**：它在「启动时拉取路由」和「集群视图变更」时出现，但**不在**单次 `read/write` 的路径上。
4. 在图上用一句话标注「数据热路径」（即 `read/write` 经过的链路），说明它为什么能绕开 meta。

参考画法（ASCII 草稿，你可改成更清晰的图）：

```
应用            FUSE内核        FUSE守护(metaClient/storageClient)      meta        storage      mgmtd
 |  open()        |                   |                                  |            |           |
 |--------------->|------------------>|                                  |            |           |
 |                |            metaClient.open() ──────────────────────>|            |           |
 |                |                   | <──── inode + layout ───────────|            |           |
 |<── fd ─────────|<──────────────────|                                  |            |           |
 |                |                   |                                  |            |           |
 |  read()        |                   |                                  |            |           |
 |--------------->|------------------>|                                  |            |           |
 |                |          storageClient.batchRead() ────────────────────────────->|           |
 |                |                   | <──── RDMA 数据 ────────────────────────────|           |
 |<── data ───────|<──────────────────|                                  |            |           |
 |                |                   |                                  |            |           |
 |  write()       |                   |                                  |            |           |
 |--------------->|------------------>|                                  |            |           |
 |                |          storageClient.update() (→ chain head) ─────────────────>|           |
 |                |                   | <──── ACK ──────────────────────────────────|           |
 |<── ok ─────────|<──────────────────|                                              |           |
 |                |                   |                                              |           |
 (启动 / 视图变更时: GetRoutingInfo ─────────────────────────────────────────────────────────----->|)
```

**完成后请自检**：你的图里，`read` 和 `write` 的箭头是不是都直接指向 storage、没有经过 meta？如果是，你就抓住了 3FS 端到端链路的核心。

## 6. 本讲小结

- 3FS 由 mgmtd / meta / storage / client 四类进程组成；前三者共用 `TwoPhaseApplication` 骨架，入口几乎一样，FUSE 客户端因需耦合内核与 IB 而单独写 `main`。
- 一个 FUSE 守护进程同时持有 `mgmtdClient` / `metaClient` / `storageClient` 三类子客户端，分别对应三种通信对象。
- mgmtd 用「单调递增版本号 + 各方按需 `GetRoutingInfo` 拉取」分发集群视图；只有 primary mgmtd 会「先落盘 FDB、再更新内存并推高版本号」。
- **meta 不在数据热路径上**：`open` 时从 meta 拿到 inode + layout，之后 `read/write` 由 client 自己算出 chunk id 与所属链，直连 storage。
- 三类核心 RPC 的落点：`open → MetaOperator.open`、`read → StorageOperator.batchRead`、`write → StorageOperator.update`。
- 这张全局地图是后续单元的导航：深入任何一个组件时，先回到本讲定位它在链路中的位置。

## 7. 下一步学习建议

本讲建立的是「宏观地图」，后续单元会逐层放大细节。建议按依赖关系继续：

- **想搞懂「服务怎么启动」**：进入 u2「公共基础设施」，先读 u2-l1「服务骨架：TwoPhaseApplication 与 ServerLauncher」，把本讲里一行带过的 `TwoPhaseApplication` 展开成完整的两阶段启动流程。
- **想搞懂「路由信息长什么样、怎么变」**：进入 u3「集群管理服务 mgmtd」，从 u3-l1「mgmtd 服务总览与 RoutingInfo 数据模型」开始。
- **想搞懂「open 在 meta 内部怎么跑」**：进入 u4「元数据服务 meta」，从 u4-l1「meta 服务总览与无状态架构」开始，再看 u4-l3「用 FoundationDB 事务实现元数据操作」。
- **想搞懂「read/write 在 storage 内部怎么跑」**：进入 u5「存储服务 storage」，先读 u5-l1 总览，再读 u5-l2「读路径」与 u5-l3「写路径与 CRAQ 链式复制」。

无论选择哪条线，都可以随时回到本讲的时序图，确认自己当前所在的位置。
