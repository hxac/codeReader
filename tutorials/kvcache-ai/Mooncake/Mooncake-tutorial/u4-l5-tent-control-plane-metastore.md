# TENT 控制面与元数据存储（ControlPlane / MetaStore）

> 本讲是 Mooncake 学习手册「传输引擎 TENT」单元（Unit 4）的第 5 讲。
> 在继续之前，请先完成 [u4-l1]（TENT 总体架构与传输引擎入门）。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清 **ControlPlane** 在 TENT 里扮演的角色：它是每台主机上的一个 RPC 服务，承担「段（segment）发现」「RDMA bootstrap」「数据中转」「段更新通知」等协调职责，与具体的传输（RDMA/TCP/SHM…）解耦。
2. 理解 **MetaStore** 的多后端抽象：它用一个极简的 `connect / get / set / remove` 接口，统一了 `etcd`、`redis`、`http` 三种外部存储，并通过 `CentralSegmentRegistry` 把段描述写进这些存储里。
3. 区分 TENT 的两种 **failover（故障转移）**：跨传输层的 `resubmitTransferTask`，以及 RDMA 内部的 `RailMonitor` 链路恢复；并知道哪些故障会被自动恢复、哪些不会。
4. 会用 Python 绑定（`pybind.cpp` 暴露的 `TransferEngine` 接口）通过一份 `transfer-engine.json` 切换不同的元数据后端，启动一次传输。

## 2. 前置知识

如果你对以下概念还不熟悉，建议先了解：

- **段（segment）**：TENT 把一台主机上一块已注册的内存区域抽象成一个 segment。别的机器要读这块内存，必须先拿到它的段描述（地址、长度、注册到哪些传输上等）。本讲的核心就是「这些段描述放在哪里、怎么被发现、传输失败后怎么办」。
- **RPC（远程过程调用）**：调用远端函数就像调用本地函数一样。TENT 的控制面基于协程 RPC 框架 `coro_rpc`（yalang/ylt）实现，每个 RPC 函数有一个数字 ID。
- **KV 存储与 etcd / redis / HTTP**：这三者都可以当成一个分布式 / 远程的「键值字典」。`etcd` 一致性最强、适合做元数据中心；`redis` 是内存数据库、快但持久化弱；`http` 后端则假设你自己起了一个支持 `GET/PUT/DELETE` 的 HTTP 服务。
- **pybind11**：把 C++ 类/函数包装成 Python 可调用模块的工具。TENT 的 Python 模块名叫 `tent`。
- **故障转移（failover）**：主路径出问题时，自动切到备用路径，尽量让上层应用感知不到故障。

## 3. 本讲源码地图

本讲涉及的关键文件（路径均相对仓库根目录）：

| 文件 | 作用 |
|------|------|
| `mooncake-transfer-engine/tent/include/tent/runtime/control_plane.h` | ControlPlane 的接口：`ControlService`（服务端）与 `ControlClient`（静态客户端），以及 `BootstrapDesc` 等结构。 |
| `mooncake-transfer-engine/tent/src/runtime/control_plane.cpp` | 上述接口的实现，注册每个 RPC 函数到 `CoroRpcAgent`。 |
| `mooncake-transfer-engine/tent/include/tent/rpc/rpc.h` | RPC 函数 ID 枚举 `RpcFuncID` 与协程 RPC 代理 `CoroRpcAgent`。 |
| `mooncake-transfer-engine/tent/include/tent/runtime/metastore.h` | `MetaStore` 抽象基类，只有 4 个纯虚方法。 |
| `mooncake-transfer-engine/tent/src/runtime/metastore.cpp` | `MetaStore::Create` 工厂：按 `type` 串选择后端。 |
| `mooncake-transfer-engine/tent/src/metastore/etcd.cpp` / `redis.cpp` / `http.cpp` | 三个后端实现。 |
| `mooncake-transfer-engine/tent/include/tent/runtime/segment_registry.h` | `CentralSegmentRegistry`（用 MetaStore）与 `PeerSegmentRegistry`（用 RPC 直连）。 |
| `mooncake-transfer-engine/tent/src/runtime/segment_registry.cpp` | 段描述如何被序列化、加 key 前缀后存进 MetaStore。 |
| `mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp` | 引擎构造：读取 `metadata_type`、`max_failover_attempts` 等配置；以及 failover 状态机 `resubmitTransferTask`。 |
| `mooncake-transfer-engine/tent/src/transport/rdma/rail_monitor.cpp` | RDMA 链路（rail）级别的恢复逻辑。 |
| `mooncake-transfer-engine/tent/src/python/pybind.cpp` | Python 绑定：`TransferEngine(config_path)` 等。 |
| `docs/source/design/tent/failover.md` | failover 的官方设计文档（本讲 4.3 的依据）。 |
| `mooncake-transfer-engine/tent/tests/engine_failover_e2e_test.cpp` | failover 的端到端测试，给出了真实可用的最小配置示例。 |

## 4. 核心概念与源码讲解

### 4.1 ControlPlane：基于 RPC 的分布式协调

#### 4.1.1 概念说明

在一台机器上，TENT 会启动一个**控制面服务** `ControlService`。它对外是一个 RPC 服务器，对内持有 `SegmentManager`（管理本机段描述、缓存远端段描述）。任何一台机器上的 TENT 进程，都可以作为 `ControlClient` 去调用另一台机器的 `ControlService`。

可以把 ControlPlane 理解成「段信息的客服中心」：传输层（RDMA、TCP 等）只管把字节搬过去，但在搬运之前，必须先问控制面「对方那块内存到底是什么、注册在哪些传输上、怎么 bootstrap」。这套问询完全走 RPC，不依赖某个中心化数据库（除非你主动选用 `etcd/redis/http`，那是 4.2 的事）。

关键点：

- ControlPlane 与传输层解耦。它解决的是「发现与协调」，不解决「搬数据」。当然它也提供了 `SendData/RecvData` 这类 RPC，用于在没有专用传输时直接把数据塞进 RPC 报文里（小数据 / fallback）。
- `ControlService` 在构造时根据 `type` 决定段注册表是 **p2p 模式**（`PeerSegmentRegistry`，去对方 RPC 拉段描述）还是 **中心化模式**（`CentralSegmentRegistry`，去 etcd/redis/http 读段描述）。

#### 4.1.2 核心流程

`ControlService` 的生命周期：

```text
ControlService(type, servers, impl)
   │
   ├─ type == "p2p"  → manager 用 PeerSegmentRegistry（段描述走 RPC 直连对端）
   ├─ 其他（etcd/redis/http）→ manager 用 CentralSegmentRegistry(type, servers)
   │
   ├─ 创建 CoroRpcAgent，注册 11 个 RPC 函数：
   │    GetSegmentDesc / BootstrapRdma / SendData / RecvData / Notify /
   │    Probe / Delegate / Pin / Unpin / SubscribeSegmentUpdate / NotifySegmentUpdated
   │
   └─ start(port, ipv6) → RPC 服务器开始监听
```

11 个 RPC 函数各司其职（见 [rpc.h:38-50](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/include/tent/rpc/rpc.h#L38-L50)）：

| 函数 ID | 作用 |
|---------|------|
| `GetSegmentDesc` | 拉取本机段描述的 JSON（p2p 模式的核心入口）。 |
| `BootstrapRdma` | 交换 RDMA 连接所需信息（QP 号、LID、GID 等）。 |
| `SendData` / `RecvData` | 通过 RPC 报文直接搬运数据（fallback / 小数据）。 |
| `Notify` | 向对端发送一条通知（`Notification{name, msg}`）。 |
| `Probe` | 探活：对端是否还活着（failover 会用到）。 |
| `Delegate` | 把一个 `Request` 委托给对端在本机执行（用于 staging 代理）。 |
| `Pin` / `Unpin` | 锁定/解锁一个 staging buffer。 |
| `SubscribeSegmentUpdate` / `NotifySegmentUpdated` | 段更新订阅与失效通知，用于远端缓存一致性。 |

#### 4.1.3 源码精读

**（1）`ControlClient` 全是静态方法，每个方法就是一次 RPC 调用。** 以 `bootstrap`（交换 RDMA 信息）为例，它把 `BootstrapDesc` 序列化成 JSON 发出去，再把对端返回的 JSON 反序列化回来：

[mooncake-transfer-engine/tent/src/runtime/control_plane.cpp:36-46](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/control_plane.cpp#L36-L46) —— 把 `BootstrapDesc` 序列化成 JSON，通过 thread-local 的 `tl_rpc_agent` 发起 `BootstrapRdma` RPC，再把回包解析回 `BootstrapDesc`。

注意第 28 行的 `thread_local CoroRpcAgent tl_rpc_agent;`：每个线程一个 RPC 客户端代理，避免多线程争用同一个连接池。

**（2）`ControlService` 构造函数：决定走 p2p 还是中心化注册表，并注册所有 RPC 处理器。**

[mooncake-transfer-engine/tent/src/runtime/control_plane.cpp:156-167](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/control_plane.cpp#L156-L167) —— 关键分支：`type == "p2p"` 时用 `PeerSegmentRegistry`（段描述去对端 RPC 拉），否则用 `CentralSegmentRegistry(type, servers)`（段描述去外部存储读）。

紧接着 168–218 行把 11 个 `onXxx` 回调逐一 `registerFunction` 到 `CoroRpcAgent`，于是收到的每个 RPC ID 都会被分发到对应的处理函数。

**（3）`onGetSegmentDesc` 的实现非常巧妙：直接返回缓存好的 JSON。**

[mooncake-transfer-engine/tent/src/runtime/control_plane.cpp:227-232](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/control_plane.cpp#L227-L232) —— 不重新序列化，而是复用 `SegmentManager::getLocalDumpedJson()` 共享出来的缓存 JSON，所以多个并发对端来拉段描述时，读的是同一份只读快照。

**（4）段更新通知（缓存一致性）。** 当某台机器修改了自己的段（比如新注册了一块内存），它会异步通知所有订阅者，让对方失效本地缓存：

[mooncake-transfer-engine/tent/src/runtime/control_plane.cpp:336-345](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/control_plane.cpp#L336-L345) —— `onSegmentUpdated` 收到段名后调用 `manager_->invalidateAllCacheForRemote(segment_name)`，把该段的远端缓存清掉。这是 p2p 模式下保证「对方看到的段描述是最新的」的关键。

> 一个重要细节：`SendData` / `RecvData` 这两个 RPC 处理函数里有明确的越界校验（`findBuffer`）和长度上限（`kMaxTransferSize = 1GiB`），见 [control_plane.cpp:245-294](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/control_plane.cpp#L245-L294)。这说明 ControlPlane 不只是「能跑通」，还考虑了恶意 / 越界请求的防护。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：搞清楚 `BootstrapRdma` 这条 RPC 从「客户端发起」到「服务端处理」的完整路径。
2. **操作步骤**：
   - 打开 [control_plane.h:36-46](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/include/tent/runtime/control_plane.h#L36-L46)，找到 `ControlClient::bootstrap` 的声明。
   - 跟到 [control_plane.cpp:36-46](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/control_plane.cpp#L36-L46) 的实现。
   - 再看服务端的 [control_plane.cpp:234-243](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/control_plane.cpp#L234-L243)（`onBootstrapRdma`），注意它最终调用了 `bootstrap_callback_`——这个回调是传输层（RDMA）在 `setBootstrapRdmaCallback` 里注册的。
3. **需要观察的现象**：客户端 `bootstrap` 把请求交给 `tl_rpc_agent.call(...)`；服务端 `onBootstrapRdma` 把 `string_view` 解析成 `BootstrapDesc`，调用回调后把 `response_desc` 序列化回去。
4. **预期结果**：你能画出「ControlClient → CoroRpcAgent → 对端 ControlService::onBootstrapRdma → bootstrap_callback_（RDMA 层）」这条调用链。
5. 运行结果：**待本地验证**（本实践为源码阅读型，无需运行）。

#### 4.1.5 小练习与答案

**练习 1**：`ControlService` 构造函数里 `type` 参数可以是哪些值？分别会创建哪种 `SegmentRegistry`？

> **答案**：`type == "p2p"` 创建 `PeerSegmentRegistry`（段描述通过对端 RPC 直接拉取）；`type` 为 `etcd`/`redis`/`http`（以及其他任何非 p2p 的值）创建 `CentralSegmentRegistry(type, servers)`，后者会去对应的外部存储读写段描述。

**练习 2**：为什么 `ControlClient` 用 `thread_local` 的 `tl_rpc_agent` 而不是全局单例？

> **答案**：RPC 客户端代理内部有连接池和调用状态，全局单例会在多线程并发调用时成为争用点；改成 thread-local 后每个线程持有独立代理，互不阻塞。

**练习 3**：`onGetSegmentDesc` 为什么不直接 `json::dump` 本地段，而是用 `getLocalDumpedJson()`？

> **答案**：为了在高并发（多个对端同时拉段描述）时复用同一份已经序列化好的只读快照，避免每次请求都重新做 JSON 序列化，也避免在序列化过程中段被并发修改导致的读写竞争。

---

### 4.2 MetaStore：多后端元数据抽象

#### 4.2.1 概念说明

p2p 模式下，每台机器要去对端 RPC 拉段描述，规模一大就扛不住（N 台机器两两拉取）。于是 TENT 提供了**中心化模式**：把所有段描述统一存到一个外部 KV 存储里。但 `etcd`、`redis`、HTTP 这三者的 API 截然不同，TENT 不想被某一种绑死。

`MetaStore` 就是这层抽象。它的接口小到不能再小：

```cpp
virtual Status connect(const std::string &endpoint) = 0;  // 建立连接
virtual Status get(const std::string &key, std::string &value) = 0;
virtual Status set(const std::string &key, const std::string &value) = 0;
virtual Status remove(const std::string &key) = 0;
```

只要某个后端实现了这 4 个方法，就能被 TENT 当作元数据存储用。上层（`CentralSegmentRegistry`）完全不知道底下是 etcd 还是 redis，它只管 `get/set/remove`。

为什么要这样设计？

- **可插拔**：运维可以选择已有基础设施（集群里已经有 redis 就用 redis）。
- **测试友好**：测试里可以用最轻的 HTTP mock 服务，甚至 `p2p` 模式完全跳过外部存储。
- **编译期裁剪**：每个后端用宏（`USE_ETCD` / `USE_REDIS` / `USE_HTTP`）单独开关，没装 hiredis 的环境可以不编译 redis 后端。

#### 4.2.2 核心流程

```text
配置 metadata_type = "etcd" / "redis" / "http"
        │
        ▼
TransferEngineImpl::construct()
        │  读 metadata_type / metadata_servers
        ▼
ControlService(type, servers, this)
        │  非 p2p → CentralSegmentRegistry(type, servers)
        ▼
CentralSegmentRegistry 构造 → MetaStore::Create(type, servers)
        │
        ├─ type=="etcd"  → EtcdMetaStore
        ├─ type=="redis" → RedisMetaStore（并从环境变量取账号/库号）
        └─ type=="http"  → HttpMetaStore

之后每次 putSegmentDesc / getSegmentDesc / deleteSegmentDesc
   → plugin_->set/get/remove("mooncake/tent/" + segment_name, JSON)
```

注意段描述在存储里的 key 都带统一前缀 `mooncake/tent/`，见 [segment_registry.cpp:27-30](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/segment_registry.cpp#L27-L30)，这样多个系统共用同一个 etcd/redis 也不会撞 key。

#### 4.2.3 源码精读

**（1）抽象基类只有 4 个虚方法。**

[mooncake-transfer-engine/tent/include/tent/runtime/metastore.h:25-40](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/include/tent/runtime/metastore.h#L25-L40) —— `MetaStore` 结构体，`Create` 是静态工厂，其余 4 个 `connect/get/set/remove` 都是纯虚函数。

**（2）`MetaStore::Create` 是一个被编译宏包裹的工厂。**

[mooncake-transfer-engine/tent/src/runtime/metastore.cpp:31-107](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/metastore.cpp#L31-L107) —— 这是最值得精读的一段。要点：

- 每个 `if (type == "...")` 都被 `#ifdef USE_XXX ... #endif` 包住。也就是说，如果你编译时没开 `USE_REDIS`，那么 `type == "redis"` 这段代码在预处理阶段就被删掉了，运行时若传 redis 会落到末尾的 `LOG(FATAL) "Protocol ... not installed. Please rebuild the package."`。
- **redis 分支特别长**（第 39–87 行），因为它要从环境变量读取敏感信息：
  - `MC_REDIS_PASSWORD` / `MC_REDIS_USERNAME`：账号密码，避免写进配置文件泄露。
  - `MC_REDIS_DB_INDEX`：选择哪个 redis 库（0–255），并做了范围校验（第 68–76 行）。
  - 而且 redis 分支是「连接成功立即返回，连接失败 `LOG(FATAL)`」，与 etcd/http 走到末尾统一 `connect` 的路径不同。

**（3）三个后端的实现风格对比。**

| 后端 | 客户端库 | `get` 映射 | `set` 映射 | `remove` 映射 | 「键不存在」语义 |
|------|----------|-----------|-----------|--------------|-----------------|
| etcd | C 封装 (`NewEtcdClient` 等) | `EtcdGetWrapper` | `EtcdPutWrapper` | `EtcdDeleteWrapper` | `raw_value == null` → `Status::InvalidEntry` |
| redis | hiredis | `GET %b` | `SET %b %b` | `DEL %b` | `REDIS_REPLY_NIL` → `Status::InvalidEntry` |
| http | libcurl | `GET` (3s 超时) | `PUT` | `DELETE` | HTTP `404` → `Status::InvalidEntry` |

- etcd 后端见 [etcd.cpp:50-67](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/metastore/etcd.cpp#L50-L67)（`get`），实现最朴素，包了一层 C API。
- redis 后端见 [redis.cpp:173-194](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/metastore/redis.cpp#L173-L194)（`get`）。注意它用 `%b`（二进制安全格式）而不是 `%s`，并在第 188 行把 `REDIS_REPLY_NIL` 翻译成 `Status::InvalidEntry`——这是「key 不存在」和「连接错误」区分的关键。认证逻辑在 [redis.cpp:93-112](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/metastore/redis.cpp#L93-L112)。
- http 后端见 [http.cpp:51-86](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/metastore/http.cpp#L51-L86)（`get`）。它把 key 拼成 URL，`CURLOPT_TIMEOUT_MS = 3000` 设了 3 秒超时，并把 HTTP `404` 翻译成 `Status::InvalidEntry`（第 76 行），其它非 200 算 `MetadataError`。

> 三种后端都把「key 不存在」统一映射成 `Status::InvalidEntry`，这正是上层能透明切换的原因——上层只看 `Status`，不关心底层协议。

**（4）`CentralSegmentRegistry` 如何用 MetaStore。**

[mooncake-transfer-engine/tent/src/runtime/segment_registry.cpp:37-66](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/segment_registry.cpp#L37-L66) —— `getSegmentDesc` 调 `plugin_->get(带前缀的key)`，再把 JSON 反序列化成 `SegmentDesc`；`putSegmentDesc` 反过来把 `SegmentDesc` 序列化后 `plugin_->set(...)`。它对底层是 etcd/redis/http 完全无感。

而 `PeerSegmentRegistry::getSegmentDesc`（[segment_registry.cpp:68-81](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/segment_registry.cpp#L68-L81)）则完全绕开 MetaStore，直接调 `ControlClient::getSegmentDesc(segment_name, response)`——这就是 p2p 模式。

**（5）配置如何驱动这一切。** 引擎构造时读两个配置项：

[mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp:277-301](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L277-L301) —— 第 277–278 行读 `metadata_type`（默认 `"p2p"`）和 `metadata_servers`（默认空串）；第 298–299 行据此构造 `ControlService`。第 303 行还能看到：p2p 模式下段名直接用 `ip:port`，非 p2p 模式则用一个随机段名。

#### 4.2.4 代码实践（可运行型 / 源码阅读型结合）

> 本任务对应规格里的实践：**用 TENT 的 Python 绑定切换不同 MetaStore 后端（http 与 redis/etcd）启动一次传输**。

Python 绑定里，引擎用配置文件构造：

[mooncake-transfer-engine/tent/src/python/pybind.cpp:406-409](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/python/pybind.cpp#L406-L409) —— `TransferEngine` 暴露了 `TransferEngine(config_path)` 这个构造函数。

而 `TransferEngine(config_path)` 的 C++ 实现就是把文件读进 `Config` 再交给 impl：

[mooncake-transfer-engine/tent/src/transfer_engine.cpp:26-33](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transfer_engine.cpp#L26-L33) —— `conf->loadFile(config_path)`，然后 `TransferEngineImpl(conf)`。

**步骤 1：准备两份配置文件**（路径、端口请按你的环境调整）。

`http` 后端配置（`tent_http.json`）——示例代码，假定你已经起了一个支持 `GET/PUT/DELETE` 的 HTTP 元数据服务在 `127.0.0.1:8080`：

```json
{
  "metadata_type": "http",
  "metadata_servers": "http://127.0.0.1:8080",
  "rpc_server_hostname": "127.0.0.1",
  "rpc_server_port": "0",
  "log_level": "info",
  "verbose": true
}
```

`redis` 后端配置（`tent_redis.json`）——示例代码，假定 redis 在 `127.0.0.1:6379`：

```json
{
  "metadata_type": "redis",
  "metadata_servers": "127.0.0.1:6379",
  "rpc_server_hostname": "127.0.0.1",
  "rpc_server_port": "0",
  "log_level": "info",
  "verbose": true
}
```

> redis 的密码 / 用户名 / 库号请通过环境变量 `MC_REDIS_PASSWORD`、`MC_REDIS_USERNAME`、`MC_REDIS_DB_INDEX` 提供（见 [metastore.cpp:43-66](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/metastore.cpp#L43-L66)），不要写进 JSON。

**步骤 2：用 Python 绑定启动一次传输**（示例代码）。

```python
# 示例代码：启动一个 TENT 引擎，注册一块内存，开一个 batch 并提交一次传输
import tent

engine = tent.TransferEngine("tent_http.json")   # 想测 redis 就换成 "tent_redis.json"
assert engine.available()

# 在本地分配并注册一段内存（演示用，真实跨机传输需要第二个引擎 import 对方 segment）
addr = engine.allocate_local_memory(4096, "cpu:0")
engine.register_local_memory(addr, 4096)

batch_id = engine.allocate_transfer_batch(4)
req = tent.Request(tent.OpCode.WRITE, source=addr, target_id=tent.LOCAL_SEGMENT_ID,
                   target_offset=0, length=4096)
engine.submit_transfer(batch_id, [req])

st = engine.get_transfer_status_overall(batch_id)
print("overall:", st.state, st.bytes)
```

绑定里 `submit_transfer` / `get_transfer_status_overall` 的定义见 [pybind.cpp:648-656](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/python/pybind.cpp#L648-L656) 与 [pybind.cpp:734-743](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/python/pybind.cpp#L734-L743)。

**步骤 3：需要观察的现象。**

- 引擎启动日志里会打印 `- Metadata Type: http`（或 `redis`），对应 [transfer_engine_impl.cpp:373](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L373) 那行 `LOG(INFO)`。
- 把 `metadata_type` 设成编译时未启用的后端（例如没开 `USE_REDIS` 却用 redis），程序会 `LOG(FATAL)` 退出，提示 "Protocol redis not installed. Please rebuild the package."（见 [metastore.cpp:95-98](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/metastore.cpp#L95-L98)）。
- 若用 redis，去 redis 里执行 `KEYS "mooncake/tent/*"` 应能看到本机段描述被写进去了（key 前缀来自 [segment_registry.cpp:27-30](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/segment_registry.cpp#L27-L30)）。

**预期结果**：换不同的 `metadata_type`，上层 Python 代码一行都不用改，传输照样完成（前提是后端服务可达）。运行结果：**待本地验证**（需要真实 redis / HTTP 服务与编译好的 `tent` Python 扩展）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 redis 后端把账号密码放在环境变量，而不是配置文件？

> **答案**：配置文件往往会被打进镜像、提交到仓库，明文密码容易泄露；环境变量由运维在部署时注入，不落盘。同时 [redis.cpp:148-171](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/metastore/redis.cpp#L148-L171) 的 `handleRedisReply` 也刻意把底层错误信息脱敏成通用文案，避免在错误返回里泄露敏感细节。

**练习 2**：http 后端的 `get` 收到 HTTP `404` 时返回什么 `Status`？为什么要这样设计？

> **答案**：返回 `Status::InvalidEntry(key)`（[http.cpp:76-77](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/metastore/http.cpp#L76-L77)）。这样上层能区分「key 不存在（正常的首次访问）」和「服务出错（`MetadataError`）」，与 etcd / redis 后端的语义保持一致，从而可以透明替换。

**练习 3**：如果你编译 TENT 时既没开 `USE_ETCD` 也没开 `USE_REDIS`，`metadata_type` 传 `"etcd"` 会怎样？

> **答案**：所有后端分支都因 `#ifdef` 被裁掉，`plugin` 保持为空，最终命中 [metastore.cpp:95-98](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/metastore.cpp#L95-L98) 的 `LOG(FATAL)`，进程终止并提示重新编译。

---

### 4.3 Failover：把传输失败对应用透明化

#### 4.3.1 概念说明

「failover」的目标是：**只要还有任何一条可用路径，且重试预算没用完，应用就永远看不到 `FAILED`。** 设计文档 [failover.md](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/tent/failover.md) 把它分成两层：

1. **跨传输层 failover（Cross-transport failover）**：在 `TransferEngineImpl` 里。当一个传输在**完成阶段**（completion stage）报告某个任务失败，引擎就把这个任务挪到下一个可用传输（例如 RDMA → TCP）。
2. **RDMA 链路恢复（Intra-RDMA rail recovery）**：在 `RailMonitor` 里。某条具体的（本机 NIC, 对端 NIC）链路反复失败时，把它暂停一段时间，冷却到期或下一次成功后再恢复。

需要特别记住的边界（来自 failover.md 的「Known Gaps」）：**提交阶段（submit stage）的失败今天不会被 failover**。也就是 `submitTransferTasks` 返回非 OK 时，任务会被标记成 `UNSPEC` 并直接上报 `FAILED`。原因有两个：开了 `merge_requests` 后一个逻辑传输对应多个任务 id，盲目重投会重复；某些传输在返回错误前已经把前面几个请求 enqueue 了，无法判断哪些已部分成功。

#### 4.3.2 核心流程

**跨传输层 failover 的状态机**（对应 [failover.md](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/tent/failover.md) 的 "State Machine" 一节）：

```text
应用调用 getTransferStatus(batch, task)
   │
   ▼ pollTaskStatus → 拿到任务当前状态
   │
   ▼ updateTaskStatusAfterPoll(... allow_failover = enable_auto_failover_on_poll)
   │
   │  如果状态 == FAILED 且允许 failover：
   │     ┌──────────────────────────────────────────────┐
   │     │ resubmitTransferTask(batch, task_id):        │
   │     │   ++task.failover_count                       │
   │     │   if failover_count > max_failover_attempts  │
   │     │       → 返回 "Failover limit exceeded"（预算耗尽）│
   │     │   task.xport_priority = failover_count        │
   │     │   type = resolveTransport(req, xport_priority)│
   │     │   if type == UNSPEC                           │
   │     │       → 返回 "All available transports failed"│
   │     │   transport_list_[type]->submitTransferTasks  │
   │     │   成功 → 把任务状态改回 PENDING（不再 FAILED）  │
   │     └──────────────────────────────────────────────┘
   │
   ▼ 返回给应用：只要成功 resubmit，应用就只看到 PENDING/COMPLETED，看不到 FAILED
```

**RDMA 链路恢复（RailMonitor）**：

- 每次完成（completion）都会驱动 rail 监视器：
  - 坏完成 → `markFailed(local_nic, remote_nic)`
  - 好完成 → `markRecovered(local_nic, remote_nic)`
- `markFailed` 在滑动窗口 `error_window_` 里累加 `error_count`；一旦达到 `error_threshold_`，该链路被暂停到 `now + cooldown_`。
- 冷却时间随重复失败**指数翻倍**，封顶 300 秒：

\[
  \text{cooldown}_n = \min\left(\text{cooldown}_0 \cdot 2^{\,n-1},\ 300\text{s}\right)
\]

- 恢复信号有两个：① 冷却到期，`available()` 自己重置退避状态；② 链路上一次真正成功的传输，`markRecovered` 直接把它恢复。两个信号相互独立，保证一条抖动的链路既不会因没人投递而永远卡死，也不会在第一次成功后还要傻等满冷却。

#### 4.3.3 源码精读

**（1）`TaskInfo` 里有 failover 专用的两个字段。**

[mooncake-transfer-engine/tent/include/tent/runtime/transfer_engine_impl.h:51-63](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/include/tent/runtime/transfer_engine_impl.h#L51-L63) —— `xport_priority`（在排序好的传输回退列表里的下标）和 `failover_count`（已重试次数）。类成员里的 `max_failover_attempts_{3}` 与 `enable_auto_failover_on_poll_{true}` 见 [transfer_engine_impl.h:263-264](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/include/tent/runtime/transfer_engine_impl.h#L263-L264)。

**（2）`resubmitTransferTask` 是整个跨传输层 failover 的唯一入口。**

[mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp:1386-1424](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L1386-L1424) —— 逐行对应上面的状态机：

- 第 1390 行 `++task.failover_count > max_failover_attempts_` 判断预算是否耗尽，耗尽返回 `InvalidEntry("Failover limit exceeded, all transports exhausted")`。
- 第 1401 行 `task.xport_priority = task.failover_count`——用重试次数当优先级下标，每次往后退一格。
- 第 1403 行 `resolveTransport(...)` 选下一个传输；返回 `UNSPEC`（第 1405 行）说明没传输可用了。
- 第 1411–1413 行打 `Transport failover: X -> Y (attempt N/M)` 日志，第 1414 行 `TENT_RECORD_TRANSPORT_FAILOVER()` 计一次指标（`tent_transport_failover_total`）。
- 第 1423 行在新的传输上重新提交这个任务。

**（3）谁调用 `resubmitTransferTask`？——`updateTaskStatusAfterPoll`。**

[mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp:1448-1460](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L1448-L1460) —— 关键三行：

- 第 1453 行：`!allow_failover || task_status.s != FAILED || task.type == UNSPEC` 时直接 return（`UNSPEC` 是提交阶段失败的标记，这里正是 Known Gaps 说的「不重试」）。
- 第 1456 行：`resubmitTransferTask(...).ok()` 成功的话，第 1457–1458 行把任务状态**改回 `PENDING`**，于是聚合后的 batch 状态不会因为这个正在重试的任务而锁死成 `FAILED`。

**（4）`getTransferStatus` 把 `enable_auto_failover_on_poll_` 透传下去。**

[mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp:1501-1522](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L1501-L1522) —— 第 1513–1514 行调用 `updateTaskStatusAfterPoll(..., enable_auto_failover_on_poll_)`。所以把 `enable_auto_failover_on_poll` 设成 `false` 后，`getTransferStatus` 就只观察、不主动重试。

**（5）配置项从哪读。**

[mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp:285-287](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L285-L287) —— `max_failover_attempts`（默认 3）和 `enable_auto_failover_on_poll`（默认 true）。

**（6）RDMA 链路恢复（RailMonitor）。**

- 加载阈值与冷却：[rail_monitor.cpp:20-33](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/rail_monitor.cpp#L20-L33)（`RailMonitor::load`，读 `rail_error_threshold`、`rail_cooldown_secs`）。
- 投递前的「闸门」：[rail_monitor.cpp:43-58](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/rail_monitor.cpp#L43-L58)（`available`，冷却到期会自愈并打 `Rail recovered: ... (cooldown expired)`）。
- 失败计数 + 暂停：[rail_monitor.cpp:60-81](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/rail_monitor.cpp#L60-L81)（`markFailed`，第 75 行 `if (st.cooldown > kMaxCooldown) st.cooldown = kMaxCooldown;` 就是 300s 封顶）。
- 成功即恢复：[rail_monitor.cpp:83-98](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/rail_monitor.cpp#L83-L98)（`markRecovered`，打 `Rail recovered: ... (un-paused by successful transfer)`）。

**（7）怎么测试 failover？** TENT 用「装饰器式故障注入」：`FaultProxyTransport` 包裹任意 `Transport`，按概率/策略把完成结果翻成 `FAILED`。引擎看到的就是普通传输，failover 路径完全没被绕过。注入的钩子是 `swapTransportForTest`（[transfer_engine_impl.h:171-176](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/include/tent/runtime/transfer_engine_impl.h#L171-L176)，仅测试用）。端到端测试的最小配置 `makeMinimalP2PConfig` 用 `127.0.0.1` 上的 p2p 元数据后端，完全自包含，见 [engine_failover_e2e_test.cpp:198-222](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/tests/engine_failover_e2e_test.cpp#L198-L222)。

#### 4.3.4 代码实践（源码阅读型）

对照 failover 文档，说明「当某节点失联时 TENT 的处理路径」：

1. **目标**：用 `probePeerAliveByID` 与 failover 文档，串起「节点失联」的检测与处理路径。
2. **操作步骤**：
   - 读 `ControlClient::probe` 的实现 [control_plane.cpp:95-98](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/control_plane.cpp#L95-L98)（一个 `Probe` RPC），再看引擎里的封装 [transfer_engine_impl.cpp:1472-1489](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L1472-L1489)：`probe` 失败若是 `RpcServiceError`，返回 `NeedsRefreshCache`——这表示「该节点的 RPC 地址可能变了/缓存失效了」，提示上层刷新远端段缓存。
   - 对照 [failover.md](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/tent/failover.md) 的 Fault Model 表：**「Peer disconnect mid-transfer → `getTransferStatus` 返回 `FAILED` → 跨传输层 failover」**这一行，就是失联在数据路径上的表现。
3. **需要观察的现象 / 处理路径**（结合源码）：
   - 失联首先在**完成阶段**被发现：`pollTaskStatus` 通过 `transport->getTransferStatus(...)`（[transfer_engine_impl.cpp:1444](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L1444)）拿到 `FAILED`。
   - `updateTaskStatusAfterPoll` 判定 `FAILED` 且允许 failover，进入 `resubmitTransferTask`（[transfer_engine_impl.cpp:1456](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L1456)），切到下一个传输（如 TCP）重投。
   - 若是 RDMA 链路层面的问题，`RailMonitor::markFailed` 把对应 rail 暂停（[rail_monitor.cpp:60-81](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/rail_monitor.cpp#L60-L81)），调度器经 `findBestRemoteDevice` 换一条 rail。
   - 若**所有传输**都用尽或预算耗尽，任务才真正上报 `FAILED`（[transfer_engine_impl.cpp:1390-1395](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L1390-L1395) 与 [1448-1460](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L1448-L1460)）。
4. **预期结果**：你能用一句话描述「节点失联 → 完成阶段 FAILED → 跨传输层重投 / rail 暂停换轨 → 预算耗尽才真正 FAILED」这条路径，并能指出每一步对应的源码位置。
5. 运行结果：**待本地验证**（端到端 failover 测试需 `cmake -DUSE_TENT=ON -DUSE_CUDA=OFF` 单独编译并本地运行，TENT 测试目前不在上游 CI 内，见 failover.md「Running manually」一节）。

#### 4.3.5 小练习与答案

**练习 1**：把 `max_failover_attempts` 设成 0 会怎样？设成 1 呢？

> **答案**：设成 0 等于完全关闭跨传输层 failover——第一次完成阶段失败就永久 `FAILED`，备用传输（如 TCP）永远不会被触碰；设成 1 表示允许恰好一次切换（RDMA 失败 → TCP）。对应测试用例见 [engine_failover_e2e_test.cpp](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/tests/engine_failover_e2e_test.cpp) 里的 `MaxFailoverAttemptsZeroDisablesFailover` 与 `MaxFailoverAttemptsOneAllowsSingleFailover`。

**练习 2**：为什么 `resubmitTransferTask` 成功后要把任务状态改回 `PENDING`？

> **答案**：因为 batch 的聚合状态（`getBatchStatus`）取所有任务里的「最坏」状态。如果正在重试的任务保留 `FAILED`，整个 batch 会被这个其实还能救的任务拖成 `FAILED`。改回 `PENDING`（[transfer_engine_impl.cpp:1457-1458](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L1457-L1458)）后，应用看到的就是「还在传」，符合「对应用透明」的设计目标。

**练习 3**：RDMA 链路恢复里，为什么需要「冷却到期」和「成功即恢复」两个独立信号？

> **答案**：只有一个信号会出问题。只靠冷却到期：如果一条恢复了的链路一直没人往它投递任务，它要傻等满冷却才能重新服役；只靠成功即恢复：如果链路抖动但恰好没有成功完成，它永远等不到恢复信号。两个信号互补——`available()` 在冷却到期时自愈（[rail_monitor.cpp:43-58](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/rail_monitor.cpp#L43-L58)），`markRecovered` 在真实成功时提前恢复（[rail_monitor.cpp:83-98](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/transport/rdma/rail_monitor.cpp#L83-L98)）。

## 5. 综合实践

把本讲三个模块串起来，做一个完整的「**换后端 + 走一遍 failover 路径**」练习。

**背景**：你要验证「换一个元数据后端，传输照常工作；传输过程中某条路径出问题，应用感知不到」。

**任务 A：对照式换后端（对应 4.2）**

1. 按本机环境起一个最简单的 HTTP 元数据服务（或一个 redis 实例）。
2. 写 `tent_http.json` 与 `tent_redis.json` 两份配置（见 4.2.4 步骤 1）。
3. 用 4.2.4 步骤 2 的 Python 脚本分别用两份配置启动，确认 `engine.available()` 为真、`get_transfer_status_overall` 返回 `COMPLETED`。
4. 在后端存储里观察 key 前缀（应带 `mooncake/tent/`），印证段描述确实被写进了你选的后端。

**任务 B：描述失联时的处理路径（对应 4.3）**

1. 假设任务 A 跑通后，在传输进行中「杀掉」对端 TENT 进程（模拟节点失联）。
2. 不运行，仅凭源码与 [failover.md](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/tent/failover.md) 写出：应用下一次 `get_transfer_status` 时，引擎内部会经过哪些函数、哪些判断，最终返回什么。要求每一步标注源码行号（提示：`pollTaskStatus` → `updateTaskStatusAfterPoll` → `resubmitTransferTask` → `resolveTransport`）。
3. 思考：因为本场景里跨传输层 failover 也救不了「整个对端没了」，所以最终任务会耗尽 `max_failover_attempts` 上报 `FAILED`——请在源码里指出是哪一行决定了这个结局（[transfer_engine_impl.cpp:1390-1395](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/tent/src/runtime/transfer_engine_impl.cpp#L1390-L1395)）。

**预期结果**：

- 任务 A：换后端不改 Python 代码，传输都能完成。
- 任务 B：你能画出失联后「完成阶段 FAILED → 重投 → 预算耗尽 → 真正 FAILED」的完整路径，并把 failover 文档、`ControlClient::probe`、`RailMonitor` 三者与这条路径对应起来。

运行结果：**待本地验证**（任务 A 需要真实的 HTTP/redis 服务与编译好的 `tent` Python 扩展；任务 B 为源码阅读与推演）。

## 6. 本讲小结

- **ControlPlane** 是每台主机上的 RPC 服务（`ControlService` + `ControlClient`），承担段发现、RDMA bootstrap、数据中转、段更新通知等协调职责，与传输层解耦；它在 `type == "p2p"` 时用 `PeerSegmentRegistry`，否则用 `CentralSegmentRegistry`。
- **MetaStore** 用 4 个纯虚方法（`connect/get/set/remove`）抽象出元数据存储；`MetaStore::Create` 是受编译宏控制的工厂，支持 `etcd`/`redis`/`http` 三种后端，且三者都把「key 不存在」统一翻译成 `Status::InvalidEntry`，从而对上层透明可替换。
- **配置驱动**：`metadata_type` / `metadata_servers` 决定走哪个后端，`max_failover_attempts` / `enable_auto_failover_on_poll` 控制 failover 行为，全部在 `TransferEngineImpl::construct()` 里读取。
- **跨传输层 failover** 的唯一入口是 `resubmitTransferTask`，由 `updateTaskStatusAfterPoll` 在完成阶段 `FAILED` 时触发；成功重投后会把任务改回 `PENDING`，对应用透明。
- **RDMA 链路恢复** 在 `RailMonitor` 里，用滑动窗口计数 + 指数退避冷却（封顶 300s），靠「冷却到期」与「成功即恢复」两个独立信号自愈。
- **提交阶段的失败今天不做 failover**（Known Gaps），会直接上报 `FAILED`；failover 通过 `FaultProxyTransport` + `swapTransportForTest` 做端到端测试，但 TENT 测试目前不在上游 CI 内。

## 7. 下一步学习建议

- 想深入 ControlPlane 的并发与连接管理，建议读 `CoroRpcAgent` 的实现 `mooncake-transfer-engine/tent/src/rpc/rpc.cpp`，理解 `ClientPool` 与协程调度的关系。
- 想搞清楚段描述如何被序列化、缓存、失效，建议顺着 `SegmentManager`（`mooncake-transfer-engine/tent/src/runtime/segment_manager.cpp`）和本讲的 `CentralSegmentRegistry` / `PeerSegmentRegistry` 一起读。
- 想真正动手做 failover 实验，按 [failover.md](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/docs/source/design/tent/failover.md) 的「Running manually」一节，用 `cmake -DUSE_TENT=ON -DUSE_CUDA=OFF` 编译并运行 `tent_failover_test`、`tent_engine_failover_e2e_test`、`tent_rail_monitor_test`。
- 下一讲可以转向**传输层内部**（如 `transport_selector` 如何排序回退列表、RDMA rail 与 `RailMonitor` 的完整生命周期），把本讲的 failover 与具体传输实现联系起来。
