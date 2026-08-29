# 传输层：etcd 服务发现 + NATS/ZMQ 事件面

## 1. 本讲目标

前几讲我们已经知道：Dynamo 把 endpoint 注册进「发现面」，客户端按路径订阅（u3-l2）；worker 之间用回拨模式收发请求（u3-l4）。但还有一个一直被推迟的问题：**这些注册记录和事件消息，物理上到底放在哪里、走什么协议？**

本讲深入 `lib/runtime/src/transports/` 与 `lib/runtime/src/storage/kv/`，学完后你应该能够：

1. 解释 etcd lease（租约）如何实现 worker 的死亡检测，以及为什么「租约丢失 = 进程主动关停」。
2. 读懂 etcd 客户端的 KV 事务（Txn）与 prefix watch 机制，包括断线重连后的 Resync 快照。
3. 说出事件面（event plane）的统一抽象：`EventEnvelope` + `EventTransportTx/Rx` trait，以及 NATS 与 ZMQ 两种实现各自的拓扑（直连 / broker）与取舍。
4. 说出 `storage/kv` 的 `Store`/`Bucket` 抽象与 etcd / file / mem / nats 四个后端各自的适用场景。
5. 用 `etcdctl` 亲手观察一个运行中的 Dynamo 集群在 etcd 里写了什么。

## 2. 前置知识

- **控制面 / 事件面 / 请求面**：回顾 u1-l1 的三面架构。本讲的主角是两个「面」的物理载体——服务发现（控制面）落在 etcd（或 file/mem）这类 **KV 存储** 上；事件面（如 KV 路由器的 KvEvent）落在 **PubSub 消息系统** 上（NATS 或 ZMQ）。
- **etcd**：一个分布式、强一致的 KV 存储（基于 Raft），Kubernetes 的元数据就存在里面。对本讲最重要的三个特性：
  - **lease（租约）**：带 TTL 的「租约凭证」，key 可以挂在租约下；租约到期，其下所有 key 会被 etcd 集群自动删除。客户端必须周期性发送 keep-alive 续租。
  - **Txn（事务）**：一组「当条件成立则执行 A，否则执行 B」的原子操作，是实现分布式锁和无竞争创建（create-if-not-exists）的基础。
  - **watch**：客户端可以订阅某个 key 前缀的变更流（Put/Delete 事件），这是服务发现「客户端感知 worker 上下线」的来源。
- **NATS**：轻量级消息中间件，Core 模式提供 subject（点分主题，如 `namespace.blr.component.backend`）上的 at-most-once PubSub。Dynamo 同时用它做请求面传输（u3-l4 讲过 nats 请求面）。
- **ZMQ（ZeroMQ）**：一个**库**而不是守护进程——PUB/SUB 套接字直接嵌在应用进程里，无 broker 也能广播，延迟极低。代价是拓扑管理（谁连谁）要自己做，而 NATS 的拓扑天然是星型（大家都连 server）。
- **HWM（High Water Mark）**：ZMQ 套接字的收/发缓冲上限，满了之后 PUB 套接字会**丢弃**消息（at-most-once），这是 ZMQ 与 NATS 行为差异的关键参数。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `lib/runtime/src/transports/etcd.rs` | etcd 客户端 `Client`：KV 增删查改、Txn、prefix watch、`KvCache` 本地缓存、连接重试 |
| `lib/runtime/src/transports/etcd/lease.rs` | etcd 租约的创建与 keep-alive 后台任务；租约丢失时关停整个 runtime |
| `lib/runtime/src/transports/etcd/lock.rs` | 基于 etcd Txn 的分布式读写锁 `DistributedRWLock` |
| `lib/runtime/src/transports/etcd/connector.rs` | 底层连接管理与断线重连（被 lease/watch 复用） |
| `lib/runtime/src/transports/event_plane/mod.rs` | 事件面门面：`EventPublisher` / `EventSubscriber`、broker 解析、去重流 |
| `lib/runtime/src/transports/event_plane/traits.rs` | 统一信封 `EventEnvelope` 定义与序列化 |
| `lib/runtime/src/transports/event_plane/transport.rs` | 传输无关 trait：`EventTransportTx` / `EventTransportRx` |
| `lib/runtime/src/transports/event_plane/nats_transport.rs` | NATS 实现（薄封装，复用 runtime 的 NATS 连接） |
| `lib/runtime/src/transports/event_plane/zmq_transport.rs` | ZMQ 实现：PUB/SUB 套接字、四帧消息格式、直连与 broker 两种模式 |
| `lib/runtime/src/transports/event_plane/frame.rs` | 事件二进制帧格式（5 字节头 + 负载） |
| `lib/runtime/src/transports/event_plane/codec.rs` | MessagePack 编解码器（信封与业务负载） |
| `lib/runtime/src/storage/kv.rs` | KV 存储抽象 `Store`/`Bucket`、后端选择器 `Selector`、`Manager` |
| `lib/runtime/src/storage/kv/etcd.rs` | `EtcdStore`：把「bucket」映射为 etcd key 前缀 |
| `lib/runtime/src/storage/kv/file.rs` | `FileStore`：用文件 mtime 模拟租约 TTL 的本地后端 |
| `lib/runtime/src/storage/kv/mem.rs` | `MemoryStore`：进程内后端（测试用） |
| `lib/runtime/src/storage/kv/nats.rs` | `NATSStore`：基于 NATS JetStream KV（目前未在主链路使用） |
| `lib/runtime/src/distributed.rs` | 把三个正交开关（发现后端 / 事件面 / 请求面）装配起来的地方 |

一个小提示：`transports/etcd/kv.rs` 是一个只有 SPDX 版权头的空占位文件，真正的 etcd KV 逻辑全部在 `transports/etcd.rs` 里——阅读时别被文件名骗了。

## 4. 核心概念与源码讲解

### 4.1 etcd 传输：Client、租约与 worker 存活检测

#### 4.1.1 概念说明

etcd 传输是控制面的物理载体。上一讲（u3-l2）说过：endpoint 注册要写进「发现面」，key 布局是 `v1/instances/{ns}/{comp}/{ep}/{instance_id}`。但当时刻意略过了一个问题：**如果 worker 进程被 `kill -9`，谁来删除这条注册记录？**

答案是：没人删——也不需要删。每条注册 key 都挂在一个 **lease（租约）** 下面，租约有 10 秒 TTL；worker 存活期间由后台任务持续续租，worker 死亡后续租停止，10 秒后 etcd 集群自动删除该租约下的所有 key，watch 这个前缀的客户端随即收到 Delete 事件。这就是「崩溃检测」的全部原理，不需要心跳服务器。

注意一个常被忽略的细节：这个设计的另一半是**活着的 worker 必须自杀**。如果 worker 还活着但与 etcd 失联（网络分区），它既无法续租也收不到请求，成为一个「半死」节点。Dynamo 的契约是：租约保不住，进程就整体关停（graceful shutdown），把位置让出来。

#### 4.1.2 核心流程

etcd 客户端的启动与存活闭环：

```text
Client::new(config, runtime)
  ├─ 连接 etcd（失败则指数退避重试，总时限 120s）
  ├─ grant(ttl) 创建租约 ──► 拿到 lease_id
  └─ spawn 后台 keep-alive 任务（挂在 runtime 取消令牌的子令牌上）
        │
        ▼   每 (剩余 TTL / 2) 发送一次 keep-alive
      ┌─────────────────────────────────────────────┐
      │ 收到续租响应 TTL > 0  → 刷新本地 deadline     │
      │ 收到 TTL <= 0（已过期/被撤销）→ 返回 Err      │
      │ 流断开 / 出错 → 重连 etcd 再建流（带 deadline）│
      └─────────────────────────────────────────────┘
        │ 不可恢复错误
        ▼
   runtime.shutdown()  ← 分阶段关停（本进程让位）
```

租约续期节奏的数学很简单：设 TTL 为 \( T \) 秒，keep-alive 任务在距离 deadline 还剩一半时间时发送心跳，即每 \( T/2 \) 一次；这样即使一次心跳丢失，剩余 \( T/2 \) 的时间也足够重连并补发（见 [lease.rs:156-158](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/etcd/lease.rs#L156-L158)）。默认 \( T = 10 \)，所以 worker 死亡后注册记录最多残留 10 秒——这与 u1-l2 讲过的 file 后端行为一致，并非巧合（file 后端注释明说「10s 与 etcd 租约相同」）。

#### 4.1.3 源码精读

**① Client 结构：一份连接、一个主租约、一个专属小运行时**

[lib/runtime/src/transports/etcd.rs:43-53](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/etcd.rs#L43-L53) 定义了 etcd `Client`：它持有 `Connector`（底层连接，断线可换新）、`primary_lease`（本连接的「主租约」id）和 `Runtime`。特别注意 `rt` 字段——一个**专属的单线程 tokio 运行时**，专门跑 lease keep-alive 和 watch 任务，注释写明动机：避免这些关键任务在主运行繁忙时被饿死（那会导致假性「租约丢失」）。⚠️ 注释同时警告：不要在这个运行时里再 await 主运行时的东西，否则可能死锁。

**② 连接重试：启动期最多等 etcd 两分钟**

[lib/runtime/src/transports/etcd.rs:98-144](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/etcd.rs#L98-L144) 的 `connect_with_startup_retry`：初始退避 1 秒、上限 30 秒（常量见 [etcd.rs:36-38](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/etcd.rs#L36-L38)），总时限 `STARTUP_CONNECT_TIMEOUT` = 120 秒。这个设计是为 Kubernetes 部署准备的：Pod 起动时 etcd Service 可能还没就绪，与其立刻崩溃进重启循环，不如边退避边等。

**③ 创建租约并绑定 runtime 生命周期**

[lib/runtime/src/transports/etcd/lease.rs:17-61](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/etcd/lease.rs#L17-L61) 的 `create_lease`：`grant(ttl)` 拿到租约后，spawn 一个 keep-alive 任务，其错误分支直接调用 `runtime.shutdown()`——这正是本节开头说的「租约保不住就自杀」契约的落点。注意注释强调关停是**分阶段的**（endpoint 排空 → 后端拆除），而不是粗暴地取消主令牌。

**④ keep-alive 循环：半周期心跳 + 断线重建**

[lib/runtime/src/transports/etcd/lease.rs:147-211](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/etcd/lease.rs#L147-L211) 的 `keep_alive_with_stream` 是一个 `tokio::select!` 三路循环：

- `receiver.message()`：收到续租响应，用响应里的 TTL 刷新 deadline；**TTL ≤ 0 说明租约已过期或被撤销，返回 Err**（这是唯一不可恢复、必须关停的分支）；
- `token.cancelled()`：进程正常退出，主动 `revoke`（撤销）租约，让注册立即消失而不是等 10 秒；
- `sleep(next_renewal)`：到心跳时间点发送 keep-alive。

流断开等可恢复错误返回 `Ok(true)`，由外层 [keep_alive](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/etcd/lease.rs#L66-L96) 先重连 etcd 再重建 keep-alive 流。

**⑤ 配置来源：环境变量**

[lib/runtime/src/transports/etcd.rs:873-905](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/etcd.rs#L873-L905)：`default_servers()` 读 `ETCD_ENDPOINTS`（缺省 `http://localhost:2379`），`default_lease_ttl()` 读 `ETCD_LEASE_TTL`（缺省 10，非法值回退并告警）。认证支持用户名/密码或双向 TLS，见 [ClientOptions::default](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/etcd.rs#L839-L871)。

**⑥ 顺带一提：分布式读写锁**

同目录的 [lock.rs](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/etcd/lock.rs#L17-L27) 用 etcd Txn 实现了 `DistributedRWLock`（写锁 key 为 `v1/{prefix}/writer`，读锁为 `v1/{prefix}/readers/{id}`），锁同样挂在租约下，持有者死亡时锁自动释放。这是「租约 = 存活凭证」这一模式的第二次复用。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「租约续期」和「租约死亡 → 注册消失」。

**操作步骤**：

1. 启动一个本地 etcd（示例命令，待本地验证）：

   ```bash
   docker run -d --name etcd -p 2379:2379 quay.io/coreos/etcd:v3.5.16 \
     etcd --advertise-client-urls http://127.0.0.1:2379 --listen-client-urls http://0.0.0.0:2379
   ```

2. 编译并启动 hello_world 的 server（来自 u3-l1 的 Rust 示例）：

   ```bash
   cargo build -p dynamo-hello-world
   ETCD_LEASE_TTL=5 ./target/debug/server
   ```

3. 另开终端，列出所有租约并持续观察：

   ```bash
   ETCDCTL_API=3 etcdctl lease list
   ETCDCTL_API=3 etcdctl lease timetolive <lease_id>   # 多执行几次，间隔 2 秒
   ```

4. `kill -9` 掉 server 进程，然后每秒执行一次：

   ```bash
   ETCDCTL_API=3 etcdctl get --prefix "v1/" --keys-only
   ```

**需要观察的现象**：`lease timetolive` 输出的剩余 TTL 在约 \( T/2 \) 周期被拉回满值（续租成功）；`kill -9` 后不再有续租，约 5 秒后 `v1/instances/...` 与 `v1/event_channels/...` 下的 key 全部消失。

**预期结果**：与 4.1.2 的流程图完全对应。若你的 etcd 版本较老，命令可能是 `etcdctl lease timetolive --keys <id>`，可顺带看到租约挂了哪些 key。

#### 4.1.5 小练习与答案

**练习 1**：为什么 keep-alive 任务跑在专属的 `rt` 而不是主运行时上？如果跑在主运行时会出什么问题？

> **答案**：keep-alive 是时限性任务，晚发一次可能直接导致租约过期、整个 worker 被剔除。主运行时承载请求处理，高负载下任务调度延迟增大，可能把 keep-alive「饿」过 deadline，造成活节点被误判死亡。专属运行时隔离了这种干扰；代价是要警惕两个运行时互相 await 造成死锁（源码注释明确警告）。

**练习 2**：worker 与 etcd 之间网线被拔了，但 worker 还能从别的网卡收请求。按本节源码，接下来会发生什么？这是好事还是坏事？

> **答案**：keep-alive 无法送达，重连在 deadline 内失败后 `keep_alive` 返回 Err，触发 `runtime.shutdown()`——worker 主动分阶段关停。这被认为是好事：该 worker 的注册即将从发现面消失，继续收请求只会造成「路由认为它可用、它却交不出注册状态」的脑裂；宁可让它下线，由调度系统拉起替代副本。

---

### 4.2 etcd KV 事务与 Watch：注册、发现与 KvCache

#### 4.2.1 概念说明

租约解决了「死亡检测」，本模块解决另外两件事：

1. **原子写入**。多个进程可能同时尝试创建同一个 key（例如两个 worker 抢注同一个 endpoint 名字），必须保证恰好一个成功。etcd 的 Txn（事务）用「Compare → then/else」结构在一次往返里完成「检查 + 写入」。
2. **变更监听**。客户端（比如路由器）需要知道 worker 何时上线/下线，etcd 的 prefix watch 提供了这样的推送流。

`transports/etcd.rs` 把这些原语包装成 `kv_create` / `kv_put` / `kv_compare_and_put` / `kv_get_and_watch_prefix` 等方法，并在其上再叠一层 `KvCache`——一个本地 `HashMap` 缓存，靠 watch 流保持与 etcd 同步，让高频读取不必每次都打网络。

#### 4.2.2 核心流程

watch 的建立与自愈（简化伪代码）：

```text
kv_get_and_watch_prefix(prefix, include_existing=true):
  1. get(prefix)           → 读到当前快照 + etcd 集群 revision
  2. start_revision = revision + 1        # 从「现在之后」开始订阅
  3. 把已有 key 作为 WatchEvent::Put 先发给消费者   # 先给快照再给增量，不丢事件
  4. spawn 监视任务:
       loop:
         建立 watch 流(start_revision)
         转发 Put/Delete 事件，滚动更新 start_revision
         流断开 → resync: 重新 get 全量快照
                → 发送 WatchEvent::Resync(kvs)   # 消费者应整体替换本地状态
                → 从新 revision 继续订阅
```

关键设计是 **Resync 事件**：断线期间发生的变化无法追回（revision 可能已被 etcd 压缩），所以重连后不做「补日志」，而是让消费者**丢弃本地状态、以最新快照为准重建**。消费者侧（如 `KvCache`）收到 `Resync` 就直接整体替换 `HashMap`。

#### 4.2.3 源码精读

**① create-if-not-exists：一条 Txn 搞定**

[lib/runtime/src/transports/etcd.rs:196-237](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/etcd.rs#L196-L237) 的 `kv_create`：`Compare::version(key, Equal, 0)` 表示「key 的版本号为 0（即不存在）」时执行 `put`（挂到租约下），否则 `get` 现有值并返回其版本。返回值设计成 `Ok(None)`=我创建成功 / `Ok(Some(version))`=已存在——幂等语义，多进程竞争时不算错误（代码注释提到这是 PR #4212 引入的 `StoreOutcome` 模式对齐）。

**② 乐观并发：compare-and-put**

[lib/runtime/src/transports/etcd.rs:324-375](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/etcd.rs#L324-L375) 的 `kv_compare_and_put`：先 get 拿到当前值与 `mod_revision`（该 key 最后一次被修改的全局版本号），值匹配后用 `Compare::mod_revision(key, Equal, 期望值)` 的事务完成「没被别人改过才写入」，返回 `Updated / Missing / Conflict` 三态。这是实现乐观锁更新（如模型卡片的 taints 修改）的基础。

**③ watch 的建立：先快照、后增量**

[lib/runtime/src/transports/etcd.rs:462-564](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/etcd.rs#L462-L564) 的 `watch_internal`：先 `get_start_revision`（[etcd.rs:567-593](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/etcd.rs#L567-L593)）拿快照与起始 revision，把已有 KV **先同步发给消费者**（通道容量特意开到 `existing_count + 32` 防死锁），再在专属运行时上 spawn 监视循环。断线恢复路径 [resync_watch_prefix](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/etcd.rs#L596-L634) 带 10 秒超时，成功后发送 `WatchEvent::Resync(kvs)`。

**④ 消费者视角的三态事件**

[lib/runtime/src/transports/etcd.rs:810-819](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/etcd.rs#L810-L819)：

```rust
pub enum WatchEvent {
    Put(KeyValue),
    Delete(KeyValue),
    /// 断线重连后的全量权威快照：消费者应整体替换本地状态
    Resync(Vec<KeyValue>),
}
```

**⑤ KvCache：读缓存 + 写穿透**

[lib/runtime/src/transports/etcd.rs:908-960](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/etcd.rs#L908-L960) 的 `KvCache::new`：先拉前缀全量、补写 `initial_values` 里缺的 key、再开 watch；后台任务（[etcd.rs:963-1012](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/etcd.rs#L963-L1012)）把三种 `WatchEvent` 应用到 `Arc<RwLock<HashMap>>` 上——`Resync` 分支直接 `*cache_write = replacement`。写路径 `put`/`delete` 是「先写 etcd，再改本地」。

#### 4.2.4 代码实践

**实践目标**：用 10 行 Rust 直接驱动 etcd 客户端，体会 Txn 与 watch（不依赖完整 runtime）。

**操作步骤**（示例代码，基于本仓库 `dynamo-runtime` crate 的公开 API 编写）：

1. 新建 `examples/etcd_probe.rs`（可放进 `lib/runtime/examples/`，或复制到自己的 bin 里）：

   ```rust
   use dynamo_runtime::{Runtime, transports::etcd::{Client, ClientOptions}};

   #[tokio::main]
   async fn main() -> anyhow::Result<()> {
       let runtime = Runtime::single_threaded()?;
       let client = Client::new(ClientOptions::default(), runtime.clone()).await?;
       // 1. create-if-not-exists
       println!("first:  {:?}", client.kv_create("demo/key", b"v1".to_vec(), None).await?);
       println!("second: {:?}", client.kv_create("demo/key", b"v2".to_vec(), None).await?);
       // 2. watch 前缀，随后另开终端用 etcdctl 改这个 key
       let watcher = client.kv_get_and_watch_prefix("demo/").await?;
       let (_, mut rx) = watcher.dissolve();
       while let Some(event) = rx.recv().await {
           println!("event: {event:?}");
       }
       Ok(())
   }
   ```

2. 运行后在另一终端执行 `etcdctl put demo/key v3`，再 `etcdctl del demo/key`。

**需要观察的现象**：第一次 `kv_create` 打印 `Ok(None)`（创建成功），第二次打印 `Ok(Some(version))`（已存在，版本号为 1）；watch 侧依次打印 Put 与 Delete 事件。

**预期结果**：与 ① 的 Txn 语义一致。若把 etcd 容器暂停 `docker pause etcd` 约 15 秒再恢复，还能看到 `Resync` 事件（consumer 收到全量快照）——**待本地验证**（取决于 etcd 的 revision 压缩窗口）。

#### 4.2.5 小练习与答案

**练习 1**：`kv_get_and_watch_prefix` 为什么要「先同步发送已有 KV，再返回 watcher」？如果反过来（先返回、后发存量）会怎样？

> **答案**：先返回会让消费者立刻开始处理增量事件，但存量快照还在通道里排队，消费者可能先看到后续增量、再看到旧快照，把新状态覆盖回旧状态（乱序）。先同步发快照再发增量，保证消费者看到的事件顺序与 etcd revision 顺序一致。通道容量按存量大小 + 32 开也是为了这批同步发送不阻塞。

**练习 2**：`KvCache` 的 `put` 是「先写 etcd 再改本地缓存」。为什么不能只改本地、让 watch 事件回来时再更新？

> **答案**：技术上可行（watch 事件确实会回来），但那样写操作的返回时机与可见性分离：本进程刚写入后立刻 `get` 可能读到旧值，且写失败的错误被吞掉（watch 只通知成功写入）。先写 etcd 能把错误立刻抛给调用者，并在成功后再更新本地，保证「写返回成功 ⇒ 本进程立即可见」。watch 路径则服务于**其他进程**的写入同步。

---

### 4.3 事件面抽象与 NATS/ZMQ 双实现

#### 4.3.1 概念说明

事件面承载的是**高频、单向、可容忍丢失**的广播——最典型的就是 KV 路由器依赖的 KvEvent（「我这个 worker 新缓存了哪些 token 前缀」）。它与请求面（u3-l4，一问一答、不可丢）的要求截然不同，所以 Dynamo 为它单独建了一套抽象：

- **`EventEnvelope`（信封）**：任何事件先包进统一信封——`publisher_id`（发布者身份）、`sequence`（每个发布者单调递增的序号）、`published_at`（毫秒时间戳）、`topic`、`payload`（业务负载）。信封是**传输无关**的：同一份字节流既能在 NATS subject 上跑，也能在 ZMQ topic 上跑。
- **`EventTransportTx` / `EventTransportRx`（传输 trait）**：发布就是 `publish(subject, bytes)`，订阅就是 `subscribe(subject) -> WireStream`。NATS 和 ZMQ 各实现一遍，上层（`EventPublisher`/`EventSubscriber`）完全感知不到差异。

为什么要有两个实现？这是本讲最重要的取舍题：

| 维度 | NATS | ZMQ（默认） |
|------|------|-------------|
| 形态 | 独立 server，星型拓扑 | 嵌在进程里的库，无 daemon |
| 部署依赖 | 需要 NATS_SERVER | 无（本地开发零依赖） |
| 拓扑 | 天然集中，发布订阅都连 server | 默认每个 publisher 独立 bind 端口，订阅者直连；或选 broker 模式 |
| 语义 | Core PubSub at-most-once | PUB/SUB at-most-once，HWM 满即丢 |
| 延迟/吞吐 | 受限于 server 跳数 | 点对点直连，跳数最少 |

Dynamo 的默认是 **ZMQ**：`EventTransportKind` 的 `#[default]` 就是 `Zmq`（[discovery/mod.rs:43-51](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/discovery/mod.rs#L43-L51)），`DYN_EVENT_PLANE=nats` 是显式 opt-in（[from_env](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/discovery/mod.rs#L63-L74)）。这也解释了 u2-l1 的结论：本地开发不需要 NATS。

#### 4.3.2 核心流程

事件面在 ZMQ 下有**两种拓扑**，由环境变量决定（`resolve_zmq_broker`，优先级：`DYN_ZMQ_BROKER_URL` > `DYN_ZMQ_BROKER_ENABLED`（走发现面找 broker）> 都不设 = 直连模式）：

```text
直连模式（默认，每个 publisher 一个端口）:
   PublisherA(PUB bind :P1) ──┐
   PublisherB(PUB bind :P2) ──┼──► Subscriber 逐个 connect，fan-in 后本地 broadcast 分发
   PublisherC(PUB bind :P3) ──┘
   ※ publisher 的 endpoint 地址通过发现面注册（v1/event_channels/...），
     订阅者用 DynamicSubscriber 监听注册变化、动态增连

broker 模式（集群规模大 / 跨网段时）:
   PublisherA ─┐                    ┌─► Subscriber1
   PublisherB ─┼─► XSUB [Broker] XPUB ─┼─► Subscriber2
   PublisherC ─┘   （转发，无业务逻辑） └─► ...
   ※ 多 broker（HA）时订阅者连多个 XPUB，靠 (publisher_id, sequence) 去重
```

NATS 模式则简单得多：所有 publisher/subscriber 都用 `namespace.{ns}.component.{comp}.endpoint.{ep}.{topic}` 这样的点分 subject 收发（[EventScope::subject](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/discovery/mod.rs#L287-L313)），NATS server 负责匹配。

#### 4.3.3 源码精读

**① 统一信封：`EventEnvelope`**

[lib/runtime/src/transports/event_plane/traits.rs:12-25](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/event_plane/traits.rs#L12-L25)：五个字段即上述信封。`(publisher_id, sequence)` 二元组是事件的**全局唯一标识**——ZMQ 的四帧格式把它单独提到帧级别（见 ⑤），多 broker 去重也靠它。

**② 传输 trait：一收一发两个接口**

[lib/runtime/src/transports/event_plane/transport.rs:22-37](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/event_plane/transport.rs#L22-L37)：`EventTransportTx::publish(&self, subject, envelope_bytes)` 与 `EventTransportRx::subscribe(&self, subject) -> WireStream`。注意 trait 只搬**原始字节**，编解码完全在上层，这让传输实现可以保持极薄。

**③ NATS 实现：65 行的薄封装**

[lib/runtime/src/transports/event_plane/nats_transport.rs:36-69](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/event_plane/nats_transport.rs#L36-L69)：`publish` 委托给 `DistributedRuntime::kv_router_nats_publish_subject`，`subscribe` 委托给 `kv_router_nats_subscribe`——即复用 runtime 已建好的那条 NATS 连接（[distributed.rs:509-532](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/distributed.rs#L509-L532)）。值得一提的是 publish 的降级：若 runtime 没建 NATS 连接，`publish` 静默返回 `Ok(())`——因为 KV 路由存在「approximate 模式」，事件本就是可有可无的。

**④ ZMQ 发布端：bind 随机端口 + 地址注册进发现面**

- 消息格式在文件头注释里写得明明白白（[zmq_transport.rs:4-15](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/event_plane/zmq_transport.rs#L4-L15)）：

  ```text
  Frame 0: topic 字符串（ZMQ 原生订阅过滤用）
  Frame 1: publisher_id（8 字节，u64 大端）
  Frame 2: sequence  （8 字节，u64 大端）
  Frame 3: 5 字节帧头(版本+长度) + EventEnvelope 的 MessagePack 字节
  ```

  publisher_id/sequence 被提到独立帧是为了**免解包的快速去重**（多 broker 场景）。帧头格式见 [frame.rs:13-42](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/event_plane/frame.rs#L13-L42)：1 字节版本 + 4 字节负载长度。
- HWM 从默认 1000 提到 **100 000**（[zmq_transport.rs:41-47](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/event_plane/zmq_transport.rs#L41-L47)），注释直言「默认值限制可扩展性」；接收超时 100ms 防永久阻塞。
- `ZmqPubTransport::bind`（[zmq_transport.rs:105-134](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/event_plane/zmq_transport.rs#L105-L134)）有个小技巧：先让 OS 分配一个空闲 TCP 端口（bind `0.0.0.0:0` 的 TcpListener 拿端口号再关掉），再把 ZMQ 绑到这个口上。`publish` 实现（[zmq_transport.rs:187-213](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/event_plane/zmq_transport.rs#L187-L213)）从信封字节里解出身份二元组，拼出四帧发出。
- 直连模式的地址分发：`EventPublisher::new_internal` 在直连分支把 `tcp://{本机IP}:{port}` 作为 `EventTransport::zmq(endpoint)` **注册进发现面**（[event_plane/mod.rs:443-476](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/event_plane/mod.rs#L443-L476) 绑定与换算，[mod.rs:501-518](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/event_plane/mod.rs#L501-L518) 注册）——也就是说，「事件面拓扑」本身也是通过「服务发现」来广告的。broker 模式则跳过注册（地址就是 broker，全局已知）。

**⑤ ZMQ 订阅端：socket pump + 本地广播**

`ZmqSubTransport`（[zmq_transport.rs:218-221](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/event_plane/zmq_transport.rs#L218-L221)）用「后台 pump 任务读套接字 → `tokio::broadcast` 通道扇出给任意多个本地订阅流」的结构（[EventTransportRx 实现](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/event_plane/zmq_transport.rs#L548-L579)），订阅流落后时打 `Lagged` 警告并跳过——又一处 at-most-once 的明示。对延迟敏感的单消费者可走 [connect_single_consumer_with_rcvhwm](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/event_plane/zmq_transport.rs#L379-L418)：直接轮询套接字，没有有损的 broadcast 一跳。四帧解码与校验在 [decode_multipart](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/event_plane/zmq_transport.rs#L508-L546)：帧数必须是 4、topic 必须与订阅串**完全相等**（防前缀碰撞）、两个 8 字节帧按大端解析。

**⑥ 订阅端的上层装配与去重**

[EventSubscriber::new_internal](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/event_plane/mod.rs#L736-L867) 按 transport_kind 三分支：NATS 直接订阅 subject（[mod.rs:747-752](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/event_plane/mod.rs#L747-L752)）；ZMQ broker 模式连 XPUB，**多 broker 时**套上 [DeduplicatingStream](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/event_plane/mod.rs#L206-L260)——一个容量 10 万的 LRU 表按 `(publisher_id, sequence)` 过滤重复事件；ZMQ 直连模式用 `DiscoveryQuery::EventChannels` 查出所有该 topic 的 publisher 端点，交给 `DynamicSubscriber` 动态增连。最后统一做 topic 过滤 + 信封解码（[mod.rs:831-853](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/event_plane/mod.rs#L831-L853)）。

**⑦ 发布端的身份与生命周期**

`EventPublisher::new_internal`（[mod.rs:386-442](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/event_plane/mod.rs#L386-L442)）用 OsRng 生成随机 `publisher_id`，并做 2^53-1 掩码（[mod.rs:263-266](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/event_plane/mod.rs#L263-L266)）——因为发现面元数据走 JSON，u64 超过浮点安全整数会被四舍五入。发布时 `sequence` 用原子自增（[publish_bytes_ref](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/event_plane/mod.rs#L556-L566)）。`Drop` 实现（[mod.rs:584-629](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/event_plane/mod.rs#L584-L629)）负责从发现面反注册，并用 `GracefulShutdownTracker` 保证反注册在优雅关停第二阶段内完成——publisher 死了，它的 ZMQ 地址要尽快从发现面消失，否则订阅者会对着一个死端口重试。

#### 4.3.4 代码实践

**实践目标**：不启动任何集群，验证 ZMQ 事件面的发布/订阅闭环，并观察 at-most-once 行为。

**操作步骤**：

仓库自带现成的单测（[zmq_transport.rs:628-676](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/event_plane/zmq_transport.rs#L628-L676) 的 `test_zmq_pubsub_basic` 就是一份最小发布/订阅示例）：

1. 运行它并打开追踪日志：

   ```bash
   RUST_LOG=dynamo_runtime=trace cargo test -p dynamo-runtime \
     --lib transports::event_plane::zmq_transport::tests::test_zmq_pubsub_basic -- --nocapture
   ```

2. 再跑多消息用例与 malformed 容错用例：

   ```bash
   cargo test -p dynamo-runtime --lib zmq_transport \
     single_consumer_preserves_wire_identity_and_exact_topic -- --nocapture
   cargo test -p dynamo-runtime --lib zmq_transport \
     test_zmq_socket_pump_continues_after_malformed_messages -- --nocapture
   ```

3. 阅读这两个测试的断言，回答：畸形消息（帧数不对 / 8 字节帧长度不对 / topic 前缀碰撞）分别被 `decode_multipart` 的哪个分支拒绝？拒绝后 pump 是退出还是继续？

**需要观察的现象**：测试通过；trace 日志里能看到 `ZMQ PUB transport bound ...`、`Socket pump received ZMQ message` 等语句；malformed 用例显示「坏消息被丢弃、后续合法 sentinel 正常送达」。

**预期结果**：pump 遇到解不开的帧只 `warn` 并继续，事件流不中断——这是广播语义下的正确选择（一条坏消息不该杀死整条情报线）。**待本地验证**：具体日志措辞以运行输出为准。

#### 4.3.5 小练习与答案

**练习 1**：ZMQ 直连模式下，一个新的 publisher 上线，订阅者是怎么知道要连它的？broker 模式下呢？

> **答案**：直连模式：publisher 把自己的 `tcp://ip:port` 作为 `DiscoverySpec::EventChannel` 注册进发现面（`v1/event_channels/` 前缀），订阅者用 `DiscoveryQuery::EventChannels` 查询并用 `DynamicSubscriber` watch 注册变化、对新端点发起 connect——**发现面是事件面拓扑的广告牌**。broker 模式：publisher 与订阅者都只连 broker 的 XSUB/XPUB，彼此地址无需交换，所以连发现面注册都跳过了。

**练习 2**：为什么 `DeduplicatingStream` 只在多 broker（HA）场景启用，单 broker 或 NATS 不需要？

> **答案**：单 broker / NATS 中，每个事件从 publisher 到订阅者只有一条路径，天然不重复。多 broker 时订阅者同时连多个 XPUB 做容错，同一条事件会被收到多份，必须按 `(publisher_id, sequence)` 去重——这正是把这两个字段放进独立 ZMQ 帧的原因：去重路径不需要反序列化整个信封。

**练习 3**：把 `ZMQ_SNDHWM` 调回默认 1000 会发生什么？什么负载形态下这会成为问题？

> **答案**：PUB 套接字发送缓冲排队到 1000 条后开始丢消息，订阅者无感知（PUB/SUB 没有重传）。KV 事件洪峰（大量请求 → 大量 KvEvent）叠加慢订阅者（HWM 同样受限）时，路由器的 KV 索引会缺失块记录，导致 KV 感知路由选点失真。所以代码把两级 HWM 都提到 10 万，本质是用内存换丢失率。

---

### 4.4 storage/kv：同一抽象，四个后端

#### 4.4.1 概念说明

前面三节分别讲了「etcd 客户端」和「事件面」，现在把它们放进一个更大的图景：`storage/kv.rs` 定义了一对 trait——`Store`（建 bucket）与 `Bucket`（增删查改 + watch），**发现面（u3-l2 的 `KVStoreDiscovery`）就建立在这对 trait 之上**。也就是说，etcd 只是发现面的**其中一个后端**：

```text
                 ┌──────────── storage::kv::Store / Bucket trait ────────────┐
                 │  get_or_create_bucket / get / insert / compare_and_replace │
                 │  delete / watch / entries                                   │
                 └───────┬───────────┬──────────────┬───────────────┬────────┘
                         │           │              │               │
                   EtcdStore    FileStore     MemoryStore     NATSStore
                  (生产/集群)  (本地零依赖)   (单测/临时)   (JetStream KV,
                                                               未在主链路使用)
```

`Store` 的语义是「传统 KV 存储」——模块头注释特意拼出全称并自嘲：*"'key_value_store' spelt out because in AI land 'KV' means something else"*（在 AI 领域 KV 通常指 KV cache，这里刻意区分）。

**四个后端的适用场景**（结合 [Selector::FromStr](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/storage/kv.rs#L154-L170) 与 [distributed.rs:700-718](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/distributed.rs#L700-L718)，由 `DYN_DISCOVERY_BACKEND` 选择，缺省 `etcd`）：

| 后端 | 选值 | 场景 | 存活检测机制 |
|------|------|------|--------------|
| etcd | `etcd` | 多机部署、K8s Operator 环境 | 真 etcd 租约（4.1） |
| file | `file` | 单机本地开发，零外部依赖 | 文件 mtime + 后台线程续期（模拟租约） |
| memory | `mem` | 单元测试、单进程 | 无（进程退出即消失） |
| nats | —（不在 Selector 列表里） | 历史遗留 | JetStream KV 的 max_age |

#### 4.4.2 核心流程

后端选择的完整链路（把 u3-l1 的「三个正交开关」落到代码）：

```text
DYN_DISCOVERY_BACKEND=file
  └► DistributedConfig::from_settings (distributed.rs:700-718)
       └► Selector::from_str("file") (kv.rs:154-170)
            root = $DYN_FILE_KV 或 /tmp/dynamo_store_kv
       └► DistributedRuntime::new (distributed.rs:173-191)
            └► kv::Manager::file(token, root) → KVStoreDiscovery
                 bucket 名 = "v1/instances" / "v1/mdc" / "v1/event_channels" ...
                 （discovery/kv_store.rs:22-25）
```

注意 bucket 在 etcd 后端下就是 **key 前缀**：`EtcdBucket` 的 `make_key` 直接 `bucket_name + "/" + key` 拼接（[storage/kv/etcd.rs:282-284](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/storage/kv/etcd.rs#L282-L284)），所以你用 `etcdctl get --prefix v1/` 看到的目录结构，与 file 后端磁盘上的目录结构是同构的——这就是综合实践要验证的事。

#### 4.4.3 源码精读

**① trait 定义：bucket 是一等公民**

[lib/runtime/src/storage/kv.rs:113-129](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/storage/kv.rs#L113-L129)：

```rust
pub trait Store: Send + Sync {
    type Bucket: Bucket + Send + Sync + 'static;
    async fn get_or_create_bucket(&self, name: &str, ttl: Option<Duration>) -> Result<Self::Bucket, StoreError>;
    async fn get_bucket(&self, name: &str) -> Result<Option<Self::Bucket>, StoreError>;
    fn connection_id(&self) -> u64;
    fn shutdown(&self);
}
```

`Bucket` 的关键方法：`insert(key, value, revision)`（revision=0 表示创建，>0 表示带版本更新）、`compare_and_replace`（乐观并发）、`watch()`、`entries()`。`WatchEvent` 与 etcd 传输层的三态一模一样（[kv.rs:106-111](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/storage/kv.rs#L106-L111)）——`Resync(HashMap)` 同样要求消费者整体替换状态。

**② EtcdStore：bucket = 前缀，get_bucket 零网络调用**

[lib/runtime/src/storage/kv/etcd.rs:26-58](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/storage/kv/etcd.rs#L26-L58)：`get_or_create_bucket` 与 `get_bucket` 都只是拼一个 `EtcdBucket` 结构体返回——etcd 没有显式 bucket 概念，前缀就是 bucket，「创建」无需任何网络请求。`insert`（[etcd.rs:65-99](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/storage/kv/etcd.rs#L65-L99)）按 revision 分派到 create/update，`compare_and_replace` 把 4.2 的 `CompareAndPutOutcome::Conflict` 翻译成 `StoreError::Retry`——调用方（如模型 taints 更新，最多重试 8 次）据此自旋重试。`watch`（[etcd.rs:128-184](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/storage/kv/etcd.rs#L128-L184)）包一层 `kv_get_and_watch_prefix` 并把 etcd 事件翻译成 `kv::WatchEvent`。

**③ FileStore：用文件系统模拟租约**

[lib/runtime/src/storage/kv/file.rs:28-33](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/storage/kv/file.rs#L28-L33)：`DEFAULT_TTL` 10 秒——注释明说「与我们的 etcd 租约过期相同」；这就是 u1-l2 里「file 模式 worker 死亡后注册至多 10 秒消失」的出处。`FileStore::new`（[file.rs:53-64](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/storage/kv/file.rs#L53-L64)）起一个**真线程**跑 `expiry_thread`（[file.rs:71-92](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/storage/kv/file.rs#L71-L92)）：每 `max(TTL/3, 1s)` 醒一次，对本进程持有的文件做 keep-alive（**touch 更新 mtime**），再扫描删除 mtime 过期的文件。key 的存活 = 文件 mtime 距今小于 TTL，写入用 `.tmp_` 前缀临时文件 + 原子 rename。用真线程而非 tokio 任务的动机与 etcd 专属运行时如出一辙：不能被繁忙的异步运行时饿死，否则活 worker 的注册会被误删。

**④ NATSStore：JetStream KV（遗留）**

[lib/runtime/src/storage/kv/nats.rs:66-95](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/storage/kv/nats.rs#L66-L95)：bucket 映射为 NATS JetStream 的 KV bucket（名字 slug 化），TTL 映射为 `max_age`。但 [kv.rs:131-139](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/storage/kv.rs#L131-L139) 的 `Selector` 枚举里根本没有 Nats 变体，注释写着「可能想移除该实现，目前未使用且测试不足」。读它的价值在于对比：同一个 trait 如何映射到完全不同的存储模型。

**⑤ Manager：统一入口与背压 watch**

[lib/runtime/src/storage/kv.rs:248-273](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/storage/kv.rs#L248-L273) 的 `Manager` 是四个后端的统一句柄；`Manager::watch`（[kv.rs:339-378](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/storage/kv.rs#L339-L378)）为发现面提供「先存量后增量」的订阅，通道容量 16384 且注释强调**故意保留背压**：discovery 状态事件绝不能丢（与事件面 at-most-once 形成鲜明对比——又一次印证「面不同，语义不同」）。

#### 4.4.4 代码实践

**实践目标**：确认「同一 bucket/key 布局在 file 后端下就是磁盘目录结构」。

**操作步骤**：

1. 用 file 后端跑 hello_world（无需 etcd，事件面默认 ZMQ 也无需 NATS）：

   ```bash
   DYN_DISCOVERY_BACKEND=file DYN_FILE_KV=/tmp/dynamo_store_kv ./target/debug/server &
   ./target/debug/client
   ```

2. 观察目录：

   ```bash
   find /tmp/dynamo_store_kv -type f | head -20
   ls -la --time-style=full-iso /tmp/dynamo_store_kv/v1/instances/*/*/* | head
   ```

3. 每 2 秒重复第 2 步，观察文件 mtime 是否被周期性 touch（keep-alive）。

**需要观察的现象**：目录里出现 `v1/instances/{namespace}/{component}/{endpoint}/{instance_id}` 与 `v1/event_channels/...` 结构的文件；活跃文件的 mtime 约每 3 秒（10/3）被刷新一次。

**预期结果**：file 后端的目录树与 4.4.2 所述 bucket 前缀布局一致；`kill -9` server 后约 10 秒内这些文件被后台扫描删除。**待本地验证**：具体文件名与删除时机以实际观察为准。

#### 4.4.5 小练习与答案

**练习 1**：`Bucket::insert(key, value, revision)` 的 `revision` 参数为什么设计成「0 = 创建，>0 = 更新」而不是分成两个方法？

> **答案**：让调用方（`Manager::publish`）可以统一走一条路径：对象带 `revision` 字段（`Versioned` trait），首次发布时 revision 为 0 触发 create-if-not-exists，后续发布带上次返回的 revision 触发带版本更新。这样 `publish` 的代码不用区分新建/更新两种调用形态，且返回的 `StoreOutcome::Created(revision)` 可以回写到对象上供下次使用——一次接口完成乐观并发的完整闭环。

**练习 2**：假设你要新增一个「redis」后端，需要实现哪些东西？租约/存活检测打算怎么映射？

> **答案**：实现 `Store`（关联一个 `RedisBucket` 类型、get_or_create_bucket/get_bucket/connection_id/shutdown）和 `Bucket`（insert/compare_and_replace/get/delete/watch/entries），然后在 `Selector` 枚举加 `Redis(url)` 变体、`FromStr` 加 `"redis"` 分支、`distributed.rs` 的 KvStore 分支加 `Manager::redis(...)`。存活检测可映射为：每个进程持有一个 Redis key 挂 `EXPIRE ttl`，后台任务周期 `EXPIRE` 续期（等价于 etcd keep-alive），注册 key 写进一个 Redis Stream/Set，watch 用 keyspace notifications 或轮询 SUNION+版本比对模拟。关键点：三态 `WatchEvent`（含 Resync）语义必须保真，否则发现面上层的 reconcile 逻辑会出错。

---

## 5. 综合实践

**任务：etcd 后端 vs file 后端对照实验，产出一份实验记录。**

这个任务把本讲四个模块串起来：租约与存活检测（4.1）、注册 key 布局（4.2）、事件面在发现面上的注册（4.3）、后端互换性（4.4）。

**步骤**：

1. **准备**：编译 hello_world（`cargo build -p dynamo-hello-world`），启动本地 etcd（见 4.1.4 步骤 1），设 `export ETCDCTL_API=3`。
2. **实验 A（etcd 后端）**：默认配置启动 server + client。用以下命令采集证据：

   ```bash
   etcdctl get --prefix "" --keys-only          # 全部 key
   etcdctl get --prefix "v1/"                    # 带值的完整记录（JSON 注册体）
   etcdctl lease list                            # 租约
   etcdctl lease timetolive --keys <id>          # 租约剩余 TTL 与挂载的 key
   ```

3. **实验 B（file 后端）**：`kill` 掉 server，`DYN_DISCOVERY_BACKEND=file DYN_FILE_KV=/tmp/dynamo_store_kv ./target/debug/server` 重新启动，采集 `find /tmp/dynamo_store_kv` 的目录树与若干文件内容。
4. **对照分析**，在实验记录中回答：
   - 两种后端的「bucket → key」布局是否同构？列出至少两个共同的顶层 bucket（提示：`v1/instances`、`v1/event_channels`，定义于 [discovery/kv_store.rs:22-25](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/discovery/kv_store.rs#L22-L25)）。
   - `v1/event_channels` 下记录的值里能看到什么？它为什么存在？（提示：ZMQ 直连模式 publisher 的 endpoint 广告，见 4.3.3 ④。）
   - 存活检测机制对照：etcd 的 lease TTL（`ETCD_LEASE_TTL`，默认 10）vs file 的 mtime 续期（`DEFAULT_TTL` = 10s，刷新周期 TTL/3）——两者数值为什么刻意保持一致？
   - `kill -9` server 后，两种后端各用多久把注册清掉？机制分别是「etcd 服务端删 key」和「本地后台线程删文件」。
5. **加分项**：在实验 A 中 `docker pause etcd` 15 秒再恢复，观察 server 日志中 keep-alive 重连与 watch resync 的输出，对照 4.1.2/4.2.2 的流程图标注每条日志属于哪个阶段。

**预期结果**：一份记录表，含 key 布局截图/文本、租约 TTL 观测、两种后端故障清理时间对比。无法在本机跑 etcd 时，实验 A 标注「待本地验证」，实验 B 单独完成（它零依赖）。

## 6. 本讲小结

- **租约是控制面的心跳**：etcd lease（TTL 默认 10s，`ETCD_LEASE_TTL` 可调）+ 半周期 keep-alive 后台任务实现 worker 死亡检测；worker 死则 etcd 自动删 key，worker 活但失联则 `runtime.shutdown()` 主动让位——「丢失租约 = 关停进程」是双向契约。
- **watch 的三态事件**：`Put / Delete / Resync`；断线重连后不做补日志，而是发送权威全量快照让消费者整体替换状态（`KvCache` 与 `KVStoreDiscovery` 都遵循这一纪律）。
- **事件面是传输无关的**：统一 `EventEnvelope`（`publisher_id + sequence + published_at + topic + payload`，MessagePack 编码）+ `EventTransportTx/Rx` 两个 trait；NATS 是 65 行薄封装，ZMQ 是四帧格式（topic / id / seq / Frame）的库级实现。
- **ZMQ 有两种拓扑**：默认直连模式（每个 publisher 独立 bind 端口、地址经发现面广告、订阅者动态连接）与 broker 模式（`DYN_ZMQ_BROKER_URL`/`DYN_ZMQ_BROKER_ENABLED`，多 broker 时按 `(publisher_id, sequence)` LRU 去重）。HWM 提到 10 万、丢帧不重传——事件面是 at-most-once，与请求面/发现面的不可丢语义形成对照。
- **发现面建立在 `Store`/`Bucket` 抽象上**：etcd（bucket=key 前缀）、file（目录树 + mtime 模拟租约，TTL 同样 10s）、mem（测试）、nats（遗留未用）四后端由 `DYN_DISCOVERY_BACKEND` 一键切换，key 布局跨后端同构。
- **调度隔离反复出现**：etcd keep-alive/watch 用专属 tokio 运行时，FileStore 续期用真线程，动机相同——时限性任务不能被繁忙的异步运行时饿死。

## 7. 下一步学习建议

本讲补齐了 u3 系列（Rust 运行时核心）的最后一块基石。接下来两条路：

1. **横向进入 LLM 层（u4）**：推荐先读 `lib/llm/src/entrypoint.rs`，看 `DistributedRuntime` 之上如何装配 HttpService 与引擎——你会大量用到本讲的 `default_event_transport_kind`、`KvCache` 等概念。
2. **纵深进入 KV 事件流（u6-l3）**：本讲的 `EventPublisher/EventSubscriber` 是 KV 路由的情报管道。`lib/llm/src/kv_router/publisher/` 下的 `event_processor.rs`、`batching.rs`、`zmq_listener.rs` 正是用 `ZmqSubTransport::connect_single_consumer` 这类直连接口消费 worker 的 KvEvent——届时回头对照 4.3.3 ⑤ 会非常有感觉。

建议同时精读的源码：`lib/runtime/src/transports/etcd/connector.rs`（重连状态机）、`lib/runtime/src/transports/event_plane/dynamic_subscriber.rs`（直连模式的动态增连逻辑，本讲只从上层视角带过）。
