# u2-l1 配置层：OffloadConfig、HostConfig 与 P2pConfig

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐个说出 `OffloadConfig` 七个字段的语义、默认值、可见性和设置途径（构造参数 / builder 方法 / 直接字段赋值）。
2. 解释 `namespace` 作为**内容寻址域**的双重作用：相同才可能互相命中，不同则互相完全不可见；并说出为什么 qwen3 把模型几何编进 namespace。
3. 说出 `P2pConfig` 三个字段（`metaserver_addr`、`advertise_addr`、`rdma_nics`）各自的约束，以及三道 fail-fast 校验分别发生在 `host.rs` 的哪个位置。
4. 理解 `OffloadConfig`（公开、实例级）与 `HostConfig`（`pub(crate)`、宿主级）的公私拆分设计，以及 `host()` 这个拆分函数在整个 crate 里唯一的调用点。

## 2. 前置知识

本讲假设你已读过 u1-l1（crate 定位）和 u1-l2（心智模型）。在此基础上补充几个 Rust 与系统层面的概念：

- **Consuming builder（消费式 builder）**：Rust 常见的配置构造惯用法——`with_xxx(mut self, ...) -> Self` 取得所有权、改一个字段、把 `self` 还给你，可以链式调用 `OffloadConfig::new(..).with_namespace(..).with_p2p(..)`。`#[must_use]` 属性让「调了 builder 却丢掉返回值」变成编译警告。
- **字段可见性**：Rust 的结构体字段可以比结构体本身更私有。`pub` 字段谁都能读写；`pub(crate)` 字段只有本 crate 内可读；无修饰的私有字段只有定义模块（及其子模块）能碰。本讲的 config.rs 把这三种可见性全都用上了——**可见性本身就是 API 设计**。
- **锁页内存（pinned memory）**：普通内存可被操作系统换页/搬移，CUDA 无法从它发起稳定的高速 DMA。锁页内存是「钉死」在物理地址上的宿主内存，是 GPU↔CPU 大块拷贝的中转站。`pinned_pool_bytes` 就是这块中转池的大小。
- **Hugepages（大页）**：用 2 MiB/1 GiB 大页替代 4 KiB 小页，减少 TLB miss，常用于大容量 pinned pool。代价见 4.3.3 的注释——机器重启后预留可能被回收。
- **tokio 多线程 runtime**：一组操作系统线程驱动异步任务。`runtime_threads` 决定这组线程的数量。为什么 2 个就够？因为 save 是 fire-and-forget、query 只是一次内存查表（源码注释原话）。
- **RDMA NIC**：远程 DMA 网卡（如 Mellanox `mlx5_0`）。P2P 模式下对端用单边 RDMA READ 直接从本机的 pinned pool 拉块，不经 CPU。
- **DP-rank 共享模型**：数据并行（DP）下多个 rank 各有一份相同的权重，它们的 KV 在数值上可互换；让它们共享一个宿主层（一个 pinned pool），任何 rank 存的块其他 rank 都能恢复。这是 `OffloadHost` 存在的意义（u2-l3 专题）。

## 3. 本讲源码地图

| 文件 | 行数 | 在本讲中的角色 |
| --- | --- | --- |
| `pegainfer-kv-offload/src/config.rs` | 99 | **本讲主角**：三个配置结构体与 builder 全部在此 |
| `pegainfer-kv-offload/src/lib.rs` | 34 | 导出面：只有 `OffloadConfig` 与 `P2pConfig` 两个配置名是公开的，`HostConfig` 不导出 |
| `pegainfer-kv-offload/src/host.rs` | 156 | `HostConfig` 的唯一消费方：`OffloadHost::new` 用它建 runtime、pinned pool 与 P2P 服务面 |
| `pegainfer-kv-offload/src/engine.rs`（构造半边） | — | `OffloadConfig` 的唯一消费方：`OffloadEngine::new` 调 `config.host()` 拆分后再吃掉实例字段 |
| `pegainfer-qwen3/src/executor.rs`（build_offload） | — | 真实调用方：展示 namespace 怎么推导、`use_hugepages` 怎么直接赋值 |
| `pegainfer-kv-offload/Cargo.toml` | 34 | `rdma` feature 背景：P2pConfig 的三个字段最终依赖 pegaflow 的 RDMA + MetaServer 能力 |

## 4. 核心概念与源码讲解

### 4.1 OffloadConfig 与 builder 方法

#### 4.1.1 概念说明

`OffloadConfig` 是创建一个 `OffloadEngine` 的「调参清单」。它回答三类问题：

1. **我是谁**（实例身份）：`instance_id`（本引擎终身不变的标识）、`device_id`（哪块 GPU）、`namespace`（我的块按什么域内容寻址）。
2. **CPU 层多大、怎么跑**（宿主容量与运行时）：`pinned_pool_bytes`（CPU KV 层容量）、`use_hugepages`、`runtime_threads`。
3. **要不要跨实例联网**：`p2p: Option<P2pConfig>`。

回忆 u1-l2 的三层分工：scheduler 出策略、本 crate 出机制、pegaflow 出运力。`OffloadConfig` 就是把「机制」拧到具体部署上的那组旋钮——它**不含任何调度策略**（哪些块值得 save 由 scheduler 决定），只含拓扑与容量。

#### 4.1.2 核心流程

一个 `OffloadConfig` 的一生：

```text
调用方（如 qwen3 的 build_offload）
  │
  ├─ OffloadConfig::new(instance_id, device_id, pinned_pool_bytes)   ← 三个必填项
  │      默认: namespace="pegainfer", use_hugepages=false,
  │           runtime_threads=2, p2p=None
  ├─ (可选) .with_namespace(ns)     ← 链式，消费 self 再返回 self
  ├─ (可选) .with_p2p(P2pConfig{..}) ← 链式
  ├─ (可选) cfg.use_hugepages = true ← 唯一的 pub 字段，直接赋值
  ▼
OffloadEngine::new(config, &kv_buffer, &stream)      ← engine.rs
  ├─ config.host()  → 拆出 HostConfig（宿主级四字段）
  ├─ OffloadHost::new(HostConfig) → 私有 host
  └─ register(host, config.instance_id, &config.namespace,
             config.device_id, reg, /*page_first=*/ false)
```

要点：构造函数只收**每个部署必须自己决定**的三个参数，其余都有合理默认；两个 builder 方法各管一个「不常用但一用就必须显式」的字段。

#### 4.1.3 源码精读

先看结构体定义——注意每个字段的注释都写了「为什么」：

[pegainfer-kv-offload/src/config.rs:L25-L47](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/config.rs#L25-L47)

```rust
/// Tuning knobs for a new `OffloadEngine`.
pub struct OffloadConfig {
    /// Stable identifier shared across this engine's lifetime so prefix blocks
    /// saved by one request are query-visible to the next.
    pub(crate) instance_id: String,
    /// Content-addressing domain shared with P2P peers: two engines see each
    /// other's blocks iff their namespaces match. ...
    pub(crate) namespace: String,
    /// CUDA device ordinal whose KV buffer this engine offloads.
    pub(crate) device_id: i32,
    /// Host pinned-memory pool size in bytes (the CPU KV tier capacity).
    pinned_pool_bytes: usize,
    pub use_hugepages: bool,
    runtime_threads: usize,
    p2p: Option<P2pConfig>,
}
```

（片段有删节，逐字段说明见下表。）这段代码的信息密度全在注释里：

- `instance_id`（L27-L29）：**稳定性**是它的唯一要求。pegaflow 按 `(instance_id, 层名)` 记住每个实例的 GPU 地址注册（engine.rs 的 `register` 把裸设备地址「烤」进去）；请求 A 存的块要能被请求 B 查到，靠的就是这个 id 跨请求不变。
- `namespace`（L30-L34）：**内容寻址域**。注意注释里的「iff」（当且仅当）：两个引擎的 namespace 相同**才有可能**互相看到对方的块；不同则**保证**互相不可见——这是一个互斥语义，不只是「前缀」。
- `device_id`（L35-L36）：CUDA 设备序号，最终原样传给 pegaflow 的注册接口。
- `pinned_pool_bytes`（L37-L38）：CPU KV 层的总容量（字节）。
- `use_hugepages`（L39-L40）：唯一 `pub` 字段——设计上允许调用方事后直接改。
- `runtime_threads`（L41-L44）：注释给出默认值 2 的理由——save 是 fire-and-forget，query 只是短暂的内存查表，「两个就够了」。
- `p2p`（L45-L46）：`None` = 单机模式；`Some` = 加入跨实例 P2P mesh（4.2 详述）。

再看构造函数的默认值：

[pegainfer-kv-offload/src/config.rs:L49-L60](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/config.rs#L49-L60)

`new(instance_id, device_id, pinned_pool_bytes)` 三个参数对应「每个部署必须拍板」的三件事；`namespace` 默认字符串 `"pegainfer"`（单机随便用的常数——正好呼应注释里「Single-node offload can use any constant」）、`use_hugepages = false`、`runtime_threads = 2`、`p2p = None`。

两个 builder 方法：

[pegainfer-kv-offload/src/config.rs:L73-L83](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/config.rs#L73-L83)

```rust
#[must_use]
pub fn with_namespace(mut self, namespace: impl Into<String>) -> Self { ... }
#[must_use]
pub fn with_p2p(mut self, p2p: P2pConfig) -> Self { ... }
```

注意两个细节：`impl Into<String>` 让调用方直接传 `&str` 或 `String` 都行；`#[must_use]` 防止写出 `cfg.with_namespace("x");` 却丢掉返回值（那会静默地什么都没改）。

最后看**真实调用方**——qwen3 的 `build_offload`，这是本 crate 唯一的下游构造点：

[pegainfer-qwen3/src/executor.rs:L2342-L2355](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L2342-L2355)

```rust
let mut config = OffloadConfig::new(
    format!("qwen3-dev{device_id}"),
    device_id,
    opts.pinned_pool_bytes,
)
.with_namespace(namespace);
config.use_hugepages = opts.use_hugepages;      // 唯一 pub 字段：直接赋值
if let Some(p2p) = &opts.p2p {
    config = config.with_p2p(pegainfer_kv_offload::P2pConfig { ... });
}
```

`instance_id` 用 `qwen3-dev{device_id}` 保证稳定且带可读性；`use_hugepages` 走直接赋值；`with_p2p` 只在配置了 P2P 时链上——教科书式的三种设置途径混用。

而 `namespace` 的推导（同文件 [L2318-L2341](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L2318-L2341)）是理解「内容寻址域」最好的教材，值得整段读：`pegainfer-qwen3-hs{hidden_size}-is{intermediate_size}-v{vocab_size}-l{layers}h{kv_heads}d{head_dim}p{page_size}`。注释解释了为什么**只编几何不够**——Qwen3-4B 与 8B 的层数/头数/head_dim/分词器完全相同，若 namespace 只含几何，一个混合集群会让 8B 静默吃到 4B 的 KV（块哈希相同、字节布局相同，但内容属于另一个模型！），所以还要编上 `hidden_size`/`intermediate_size`/`vocab_size` 这三个能区分模型规格的维度。

#### 4.1.4 代码实践

**实践 A：可见性之墙（不需要 GPU，约 3 分钟）**

1. **实践目标**：用编译器亲身体验 `OffloadConfig` 的字段可见性分层，理解「外部能碰什么」。
2. **操作步骤**：
   - 在 `pegainfer-kv-offload/tests/` 下新建 `config_visibility.rs`（这是你的本地练习文件，不要提交）：

     ```rust
     // 示例代码：仅用于观察编译错误，不要提交
     use pegainfer_kv_offload::{OffloadConfig, P2pConfig};

     #[test]
     fn visibility_wall() {
         let cfg = OffloadConfig::new("demo", 0, 64 << 20).with_namespace("ns-demo");
         assert!(!cfg.use_hugepages);          // pub 字段：OK
         let _ = &cfg.namespace;               // pub(crate) 字段：外部 crate 不可见
         let _ = cfg.runtime_threads;          // 私有字段：更不可见
     }
     ```

   - 运行 `cargo test -p pegainfer-kv-offload --test config_visibility --release 2>&1 | head -40`。
3. **需要观察的现象**：编译失败，报错指向 `cfg.namespace` 与 `cfg.runtime_threads` 两行，错误码 E0616（field is private）以及可见性说明；`use_hugepages` 那行**不报错**。
4. **预期结果**：两处 E0616，分别提示 `namespace` 至少要在本 crate 内可见、`runtime_threads` 是私有字段。把后两行注释掉后测试通过（`use_hugepages` 默认 `false` 断言成立）。具体报错文本与测试计数**待本地验证**。
5. 删除练习文件，恢复原状。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `runtime_threads` 连一个 setter 都没有，而 `use_hugepages` 却是公开字段？

**参考答案**：`runtime_threads` 的注释说明默认值 2 已经覆盖真实负载特征——save 是 fire-and-forget、query 是一次内存查表，线程数不是部署方需要调的参数（至少现在不是），所以干脆锁死默认值，避免暴露一个没人知道怎么调的旋钮。`use_hugepages` 则依赖机器环境（内核大页预留），是部署方按机器实情决定的开关，且语义简单（开/关），直接暴露字段最省事。

**练习 2**：两个引擎 `namespace` 相同就一定能互相看到对方的块吗？

**参考答案**：不一定。namespace 是「可见」的**必要条件**而非充分条件：同进程内还必须共享同一个 `OffloadHost`（见 4.3 与综合实践），跨进程还必须都配置了指向同一 MetaServer 的 P2pConfig。反之 namespace 不同则**一定**看不见——这是充分条件的否定方向，即「iff」注释里互斥的那一半。

**练习 3**：`new()` 为什么把 `pinned_pool_bytes` 收进构造函数，而不是给个默认值？

**参考答案**：CPU KV 层容量是每个部署必须自己算的物理量（可用宿主内存减去其他占用），不存在普适默认值——给个错误默认值反而诱导用户漏配。而 namespace（单机可用任意常数）、线程数（2 够用）、hugepages（默认关）、p2p（默认单机）都有安全默认。

### 4.2 P2pConfig 三个字段

#### 4.2.1 概念说明

`P2pConfig` 是「跨实例 P2P 共享」的开关包：设了它，本引擎就加入 pegaflow 的 MetaServer + RDMA 数据面。这是 **P/D 分离（prefill/decode 分离）的数据面**：decode 节点本地 CPU 层 miss 时，凭内容哈希问 MetaServer「谁有这块」，然后单边 RDMA READ 把缺的前缀拉进本地 pinned pool，再走普通 H2D load——**没有 handle 协议**，块按内容找到（u1-l2 讲过免协议的原理，u3-l2 会走完整时序）。

三个字段各管一段：

| 字段 | 管什么 | 约束 |
| --- | --- | --- |
| `metaserver_addr` | 去哪查询块归属（MetaServer 的 gRPC 地址） | 形如 `http://10.0.0.100:50056` |
| `advertise_addr` | 别人怎么找到我（本引擎的可路由 `IP:port`） | 必须是**字面 socket 地址**（它同时充当传输服务的 bind 地址，主机名会在启动期被拒绝）；跨节点不能用 `0.0.0.0`/`127.0.0.1` |
| `rdma_nics` | 在哪些 RDMA 网卡上注册 pinned pool | 设备名列表（如 `mlx5_0`），**至少一个**，否则启动报错 |

#### 4.2.2 核心流程

`p2p: Some(...)` 之后，`OffloadHost::new` 里发生的事（细节在 u2-l3 展开，这里只看配置的流动）：

```text
OffloadConfig.p2p ──host()──▶ HostConfig.p2p ──▶ OffloadHost::new(config)
   │
   ├─ 校验 1: rdma_nics 为空 → EngineError::InvalidArgument     (host.rs L49-L53)
   ├─ 三个字段注入 pegaflow 的 StorageConfig                     (host.rs L54-L56)
   │     rdma_nic_names / metaserver_addr / advertise_addr
   ├─ 校验 2: advertise_addr 解析为 SocketAddr 失败 → InvalidArgument
   │     （拒绝主机名；host.rs L74-L79）
   ├─ 校验 3: TcpListener 先 bind 再 serve，端口被占 → 启动即失败
   │     （fail-fast，host.rs L86-L96）
   └─ 全部通过 → 后台 GC 任务 + 正式加入 mesh
```

三道校验全是 **fail-fast**：宁可在启动时大声失败，也不要 P2P「静默地从未生效」。

#### 4.2.3 源码精读

结构体与文档（整段文档注释就是一份 P2P 数据面的微缩说明书）：

[pegainfer-kv-offload/src/config.rs:L3-L23](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/config.rs#L3-L23)

```rust
/// Cross-instance P2P sharing over pegaflow's MetaServer + RDMA data plane.
#[derive(Clone, Debug)]
pub struct P2pConfig {
    /// MetaServer gRPC address, e.g. `http://10.0.0.100:50056`.
    pub metaserver_addr: String,
    /// This engine's routable `IP:port` (a literal socket address — it doubles
    /// as the embedded transfer service's bind address, so hostnames are
    /// rejected at startup). ... Must not be 0.0.0.0/127.0.0.1 for cross-node use.
    pub advertise_addr: String,
    /// RDMA NIC device names to register the pinned pool on (e.g. `mlx5_0`).
    pub rdma_nics: Vec<String>,
}
```

三个字段全是 `pub`——`P2pConfig` 是普通数据包，没有 builder；调用方（qwen3 executor.rs L2350-L2354）直接结构体字面量构造。注意 `advertise_addr` 的双重身份：**既是给别人看的广告地址（MetaServer 把它记成块属主），又是自己传输服务的监听地址**。这就是它必须是字面 `IP:port` 的根因——`std::net::TcpListener` bind 需要具体地址，而且若允许主机名，不同机器对同一主机名的解析可能不同，会造成「广告出去的地址」与「实际监听的地址」不一致。

消费端的三道校验。第一道，NIC 非空：

[pegainfer-kv-offload/src/host.rs:L47-L57](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/host.rs#L47-L57)

```rust
let mut storage_config = StorageConfig::default();
if let Some(p2p) = &config.p2p {
    if p2p.rdma_nics.is_empty() {
        return Err(EngineError::InvalidArgument(
            "P2P requires at least one RDMA NIC".into(),
        ));
    }
    storage_config.rdma_nic_names = Some(p2p.rdma_nics.clone());
    storage_config.metaserver_addr = Some(p2p.metaserver_addr.clone());
    storage_config.advertise_addr = Some(p2p.advertise_addr.clone());
}
```

第二道，地址必须是 socket 地址：

[pegainfer-kv-offload/src/host.rs:L72-L79](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/host.rs#L72-L79)

```rust
let p2p_shutdown = match config.p2p {
    Some(p2p) => {
        let listen: std::net::SocketAddr = p2p.advertise_addr.parse().map_err(|e| {
            EngineError::InvalidArgument(format!(
                "P2P advertise_addr {:?} is not a socket address: {e}", ...
```

第三道，先 bind 再服务（注释原话：Bind eagerly so startup fails loud on a taken port instead of P2P silently never serving）：

[pegainfer-kv-offload/src/host.rs:L83-L96](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/host.rs#L83-L96)

```rust
runtime.spawn(async move {
    // Bind eagerly so startup fails loud on a taken port
    // instead of P2P silently never serving.
    let bound = tokio::net::TcpListener::bind(listen).await;
    ...
```

bind 的结果通过一个 ready channel 同步回构造线程（L107-L110 的 `ready_rx.recv()`），所以端口冲突在 `OffloadHost::new` 返回 `Err` 时就已暴露，而不是等第一个对端连接失败才发现。

#### 4.2.4 代码实践

**实践 B：错误注入走读（纯阅读，不需要 GPU）**

1. **实践目标**：不看运行结果，仅凭源码推断三种错误配置各自的失败点与错误类型，训练「沿调用链查证」的习惯。
2. **操作步骤**：对下面三种配置，在源码中找到失败发生的**具体行**，填一张表：

   | 配置错误 | 失败位置（文件:行） | 错误类型/消息关键词 |
   | --- | --- | --- |
   | `rdma_nics: vec![]` | ? | ? |
   | `advertise_addr: "node-7:51000"`（主机名） | ? | ? |
   | `advertise_addr` 端口已被占用 | ? | ? |
3. **需要观察的现象**：三行失败点都在 `host.rs`，且都在 `OffloadEngine::new` 返回 `Err` 时就能被调用方感知。
4. **预期结果**：分别是 host.rs L49-L53（`InvalidArgument("P2P requires at least one RDMA NIC")`）、L74-L79（`InvalidArgument("... is not a socket address")`）、L86-L96 bind 失败经 ready channel 传回后转为 `EngineError::Storage`（L107-L110）。你可以用 `grep -n "InvalidArgument\|ready_rx\|TcpListener::bind" src/host.rs` 自查。
5. 运行层面的实际报错文本**待本地验证**（需要一个带 CUDA 与 pegaflow git 依赖的构建环境）。

#### 4.2.5 小练习与答案

**练习 1**：`advertise_addr` 填 `0.0.0.0:51000` 能通过 host.rs 的三道校验吗？为什么文档仍然禁止它用于跨节点？

**参考答案**：能。`0.0.0.0:51000` 是合法 `SocketAddr`，bind 也会成功——三道校验都不检查这个具体值。但 `0.0.0.0` 是「所有接口」的通配地址，不是可路由的单播地址；MetaServer 会把它记成块属主，对端拿它去做 RDMA 握手时要么拨不通、要么拨到错误机器。所以这条约束写在**文档注释**里，靠配置评审保证——校验管住了「语法错误」，管不住「语义错误」。

**练习 2**：为什么 `rdma_nics` 是列表而不是单个字符串？

**参考答案**：一台多网卡机器可能有几张 RDMA NIC（多轨道组网、冗余或带宽聚合）。pinned pool 需要在**每张**要被对端直读的 NIC 上完成内存注册（`ibv_reg_mr`），只注册一张就意味着经另一张 NIC 到达的对端无法 READ。`StorageConfig.rdma_nic_names` 接收列表，host.rs 把整个列表克隆注入。

**练习 3**：`P2pConfig` 为什么不做任何校验就把字段交给 `OffloadHost`？

**参考答案**：这是「数据包 vs 使用点」的分工：`P2pConfig` 只描述意图，校验发生在**拥有启动上下文**的地方（`OffloadHost::new`——那里才能 bind 端口、才能给出带上下文的错误消息）。把校验塞进结构体构造函数会让错误发生在远离使用场景的位置，且无法做「bind 端口」这类有副作用的检查。

### 4.3 HostConfig 与 host() 拆分

#### 4.3.1 概念说明

`HostConfig` 是 `OffloadConfig` 的「宿主层一半」，两者是**公私镜像**关系：

| | OffloadConfig | HostConfig |
| --- | --- | --- |
| 可见性 | `pub`（lib.rs 导出） | `pub(crate)`（**不导出**） |
| 归属 | 一个引擎实例（rank 级） | 一个宿主（可服务多个引擎） |
| 字段 | 实例身份 3 个 + 宿主 4 个 | 只有宿主 4 个 |
| 谁构造 | 外部调用方 | 只能由 `config.host()` 拆出 |

这条公私线背后是所有权模型：**一个 `OffloadHost` 服务任意多个 rank 级 `OffloadEngine`**（DP-rank 共享模型）。实例身份（`instance_id`/`namespace`/`device_id`）每个 rank 各有各的，不能进宿主配置；而 pinned pool、runtime、P2P 服务面是全体 rank 共用的，天然属于宿主。`host()` 就是把公开配置「切」成私有宿主配置的那一刀。

#### 4.3.2 核心流程

`host()` 拆分发生在**私有宿主构造路径**上，且全 crate 只有这一个调用点：

```text
路径 1（私有宿主）: OffloadEngine::new(config, buffer, stream)
    config ──host()──▶ HostConfig ──▶ OffloadHost::new   ← 建新宿主
    config 的 instance_id/namespace/device_id ──▶ register

路径 2（共享宿主）: OffloadEngine::with_arenas_on(host, instance_id,
                    namespace, device_id, arenas, page_first)
    完全不经过 OffloadConfig！宿主级参数在「建 host 那一刻」已被消费，
    这里只补实例身份。
```

也就是说：想共享宿主的调用方根本**不写 OffloadConfig**——宿主早就建好了，缺的只是每个 rank 自己的身份三元组。

#### 4.3.3 源码精读

拆分函数本体（注意 doc 注释点名了它的唯一用途）：

[pegainfer-kv-offload/src/config.rs:L62-L71](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/config.rs#L62-L71)

```rust
/// The host-tier half of this config (private-host constructors split it
/// off before consuming the instance fields).
pub(crate) fn host(&self) -> HostConfig {
    HostConfig {
        pinned_pool_bytes: self.pinned_pool_bytes,
        use_hugepages: self.use_hugepages,
        runtime_threads: self.runtime_threads,
        p2p: self.p2p.clone(),
    }
}
```

四个宿主字段被拷贝/克隆过去，三个身份字段被留下（马上交给 `register`）。私有结构体定义：

[pegainfer-kv-offload/src/config.rs:L86-L98](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/config.rs#L86-L98)

```rust
/// Host-tier knobs for a shared `OffloadHost`.
pub(crate) struct HostConfig {
    pub(crate) pinned_pool_bytes: usize,
    /// Back the pinned pool with hugepages (pegaflow supports it natively).
    /// Verify the box actually holds a reservation (`HugePages_Total`) —
    /// some cluster platforms re-claim it across reboots.
    pub(crate) use_hugepages: bool,
    pub(crate) runtime_threads: usize,
    pub(crate) p2p: Option<P2pConfig>,
}
```

`use_hugepages` 的注释是一条真实运维教训：大页预留可能在集群平台重启后被回收，启用前应核对 `/proc/meminfo` 的 `HugePages_Total`——文档写进字段注释，配置层替运维把关。

消费点二连看。`host()` 的唯一调用方（`OffloadEngine::new`）：

[pegainfer-kv-offload/src/engine.rs:L101-L116](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L101-L116)

```rust
pub fn new(config: OffloadConfig, buffer: &KvBuffer, stream: &CudaStream)
    -> Result<Self, EngineError>
{
    let reg = Registration::from_buffer(buffer, stream);
    let host = OffloadHost::new(config.host())?;   // ← 拆分发生在这里
    Self::register(host, config.instance_id, &config.namespace,
                   config.device_id, reg, false)
}
```

执行顺序值得注意：先用掉宿主一半（`config.host()`），再消费实例字段——与 `host()` 的 doc 注释「split it off **before** consuming the instance fields」逐字对应。而共享宿主路径：

[pegainfer-kv-offload/src/engine.rs:L130-L146](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L130-L146)

`with_arenas_on(host: Arc<OffloadHost>, instance_id, namespace: &str, device_id: i32, arenas: &[KvArena], page_first: bool)` 的参数表就是「路径 2 不需要 OffloadConfig」的直接证据：宿主已经存在（`Arc` 传来），身份三元组逐个传入，KV 布局来自显式 `arenas` 而非 `KvBuffer`。顺带一提，目前本仓库内还没有 `with_arenas_on` 的调用者——它是为 DP-rank 共享与 vLLM MLA namespace 预留的公开扩展点（其 doc 注释解释了 `page_first` 对接 vLLM MLA 布局的规则）。

最后看宿主怎么消费 `HostConfig`：

[pegainfer-kv-offload/src/host.rs:L40-L67](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/host.rs#L40-L67)

`OffloadHost::new(config: HostConfig)` 依次：用 `config.runtime_threads.max(1)` 建多线程 runtime（L41-L45，`max(1)` 是对「手滑配 0」的防御）；按 4.2 的流程注入 P2P；然后在 `runtime.enter()` 守卫内用 `config.pinned_pool_bytes` 与 `config.use_hugepages` 建 `PegaEngine`（L60-L67）——为什么必须在守卫内，是 u2-l3 的主题。

#### 4.3.4 代码实践

**实践 C：三方对照表（纯阅读 + 手写表格）**

1. **实践目标**：把三条配置通路（`OffloadConfig` 字段 → `with_arenas_on` 参数 → `HostConfig` 字段）对齐，验证你对拆分设计的理解。
2. **操作步骤**：对照 [config.rs:L26-L47](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/config.rs#L26-L47)（注意通配规则）与 [engine.rs:L130-L146](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L130-L146)，填写下表：

   | OffloadConfig 字段 | 进 HostConfig？ | with_arenas_on 里的对应参数 | 最终去向 |
   | --- | --- | --- | --- |
   | instance_id | | | |
   | namespace | | | |
   | device_id | | | |
   | pinned_pool_bytes | | | |
   | use_hugepages | | | |
   | runtime_threads | | | |
   | p2p | | | |
3. **需要观察的现象**：身份三字段与宿主四字段恰好互补，没有重叠也没有遗漏。
4. **预期结果**：身份三字段（instance_id/namespace/device_id）不进 HostConfig、在 `with_arenas_on` 中对应同名参数、去向是 `register_context_layer_batch_strided`（engine.rs L156 起）；宿主四字段全部进 HostConfig、在 `with_arenas_on` 中**没有对应参数**（因为 host 早已建好）、去向是 `OffloadHost::new`。另外两行额外发现：`with_arenas_on` 有 OffloadConfig 没有的 `arenas`/`page_first`（布局来源不同，u2-l2 主题）。
5. 本实践为源码阅读型，无需运行验证。

#### 4.3.5 小练习与答案

**练习 1**：`HostConfig` 为什么不导出（`pub(crate)`），而 `OffloadHost` 类型本身却导出？

**参考答案**：`OffloadHost` 出现在公开签名里（`with_arenas_on(host: Arc<OffloadHost>, ...)`），外部必须能命名这个类型、持有它的 `Arc`。但外部**不应**自己构造宿主——`OffloadHost::new` 是 `pub(crate)`，宿主的建立（建 runtime、bind 端口、起 GC）是本 crate 的职责；导出 `HostConfig` 等于把「怎么建宿主」的所有内部细节（字段含义、校验时机）变成对外承诺的 API，未来一改就破坏兼容。

**练习 2**：如果把 `namespace` 挪进 `HostConfig` 会怎样？

**参考答案**：会破坏 DP-rank 共享模型的一个核心前提。host.rs L15-L23 的文档说：各 rank 注册成各自的 pegaflow instance、块按 `(namespace, hash)` 落在共享池——「共享 namespace」是**每个 rank 的选择**（注释同时警告：只应在 KV 数值可互换时共享，例如复制权重的 DP rank）。namespace 属于实例身份，与「这台宿主跑哪些 rank、池子多大」无关；挪进宿主配置后，同宿主的所有 rank 被强制同域，想分域的部署无法表达。

**练习 3**：`host()` 里 `p2p: self.p2p.clone()` 为什么是 clone 而 `pinned_pool_bytes` 是 copy？

**参考答案**：`host()` 接收 `&self`（只借用），但返回一个独立的 `HostConfig` 值；`usize`/`bool` 是 `Copy` 类型直接复制，而 `Option<P2pConfig>` 内含 `String` 与 `Vec<String>`（堆数据，非 `Copy`），只能 clone。代价可忽略——`host()` 在每个引擎的生命周期里只调用一次。

## 5. 综合实践

**综合实践：亲手构造一份完整配置并回答「共享什么」（约 20 分钟）**

这是本讲规格指定的主实践：用 `OffloadConfig::new` 构造配置，链式调用 `with_namespace` 与 `with_p2p`，断言字段值；再回答两个引擎互相可见还缺什么。

### 第一步：外部视图测试（不需要 GPU）

在 `pegainfer-kv-offload/tests/` 下新建 `config_builder.rs`（本地练习文件，不要提交）：

```rust
// 示例代码：综合实践，验证 OffloadConfig 的构造与公开可观察的默认值
use pegainfer_kv_offload::{OffloadConfig, P2pConfig};

#[test]
fn config_builder_public_view() {
    let p2p = P2pConfig {
        metaserver_addr: "http://10.0.0.100:50056".to_string(),
        advertise_addr: "10.0.0.2:51000".to_string(),
        rdma_nics: vec!["mlx5_0".to_string()],
    };
    // P2pConfig 是 Clone + Debug：留一份克隆用于断言（with_p2p 会拿走所有权）
    let probe = p2p.clone();
    let cfg = OffloadConfig::new("inst-7", 3, 128 << 20)
        .with_namespace("ns-demo")
        .with_p2p(p2p);

    // —— 外部 crate 能直接断言的部分 ——
    assert!(!cfg.use_hugepages); // 唯一 pub 字段，默认 false

    // —— 以下断言在 crate 外无法直接写（字段不可见），见第二步 ——
    // assert_eq!(cfg.namespace, "ns-demo");      // pub(crate)
    // assert_eq!(cfg.runtime_threads, 2);        // 私有
    // assert!(cfg.p2p.is_some());                // 私有

    // P2pConfig 本体的字段是 pub 的，断言我们留下的克隆
    assert_eq!(probe.metaserver_addr, "http://10.0.0.100:50056");
    assert_eq!(probe.rdma_nics, vec!["mlx5_0".to_string()]);
}

// 问题答案（3-5 句，规格要求写成注释）：
// 两个引擎想互相看到对方的块，除了共享 namespace，同进程内还必须共享
// 同一个 OffloadHost：save 的块落在宿主层唯一的 pinned pool 里，按
// (namespace, hash) 寻址；而 OffloadEngine::new 每次都经 config.host()
// 建一个私有宿主，两个私有宿主就是两个互不相通的池，namespace 相同也
// 看不见对方。共享的做法是把同一个 Arc<OffloadHost> 传给每个引擎的
// with_arenas_on（engine.rs L130 起）——这正是 host.rs L15-L23 文档描述的
// DP-rank 共享模型。跨进程/跨机时不必共享宿主，但双方都要配置指向同一
// MetaServer 的 P2pConfig，经 RDMA READ 互拉（u3-l2 专题）。
```

运行：`cargo test -p pegainfer-kv-offload --test config_builder --release`。

- **需要观察的现象**：测试通过；把三行注释掉的断言解开则编译失败（E0616），与 4.1.4 的可见性之墙一致。
- **预期结果**：`config_builder_public_view ... ok`，1 passed。构建需要拉取 pegaflow-core git 依赖，**待本地验证**。

### 第二步：临时内联测试（全字段断言，跑完必须还原）

`instance_id`/`namespace`/`device_id` 是 `pub(crate)`、`pinned_pool_bytes`/`runtime_threads`/`p2p` 是私有字段，外部测试碰不到；要断言全部字段，唯一的办法是在**定义模块内**做单元测试。在**你本地的克隆**里，把下面的代码临时粘贴到 `src/config.rs` 文件末尾（这会临时修改源码——仅作练习，跑完立刻还原，绝不提交）：

```rust
// 示例代码：临时内联测试，验证后用 git restore src/config.rs 还原
#[cfg(test)]
mod tutorial_field_probe {
    use super::*;

    #[test]
    fn builder_fills_every_field() {
        let cfg = OffloadConfig::new("inst-7", 3, 128 << 20)
            .with_namespace("ns-demo")
            .with_p2p(P2pConfig {
                metaserver_addr: "http://10.0.0.100:50056".into(),
                advertise_addr: "10.0.0.2:51000".into(),
                rdma_nics: vec!["mlx5_0".into()],
            });
        assert_eq!(cfg.instance_id, "inst-7");      // pub(crate)：模块内可见
        assert_eq!(cfg.namespace, "ns-demo");
        assert_eq!(cfg.device_id, 3);
        assert_eq!(cfg.pinned_pool_bytes, 128 << 20); // 私有：子模块可见
        assert!(!cfg.use_hugepages);
        assert_eq!(cfg.runtime_threads, 2);           // 锁死的默认值
        let p2p = cfg.p2p.as_ref().unwrap();
        assert_eq!(p2p.advertise_addr, "10.0.0.2:51000");
        assert_eq!(p2p.rdma_nics.len(), 1);
    }
}
```

运行与还原：

```bash
cargo test -p pegainfer-kv-offload --lib tutorial_field_probe --release -- --nocapture
git restore src/config.rs    # 或 git checkout -- src/config.rs
```

- **需要观察的现象**：`builder_fills_every_field ... ok`——同一份字段在模块内全部可断言，证明「外部看不到」是可见性设计而非字段不存在。
- **预期结果**：1 passed；还原后 `git status` 显示 src/config.rs 无改动。**待本地验证**。

### 第三步：清理

删除 `tests/config_builder.rs`（与 4.1.4 的 `tests/config_visibility.rs` 若保留），确认 `git status` 干净。

## 6. 本讲小结

- `OffloadConfig` 是实例级调参清单：身份三字段（`instance_id` 稳定标识、`namespace` 内容寻址域、`device_id` GPU 序号）+ 宿主四字段（`pinned_pool_bytes` CPU 层容量、`use_hugepages`、`runtime_threads` 默认锁死 2、`p2p`）。
- 字段可见性就是 API 设计：`use_hugepages` 是唯一 `pub` 字段（直接赋值），身份字段 `pub(crate)`（crate 内 register 消费），容量与线程字段私有（只能经 `new`/builder 设置）；qwen3 的 `build_offload` 混用了全部三种途径。
- `namespace` 是内容寻址域：相同是互相命中的必要条件（还要同宿主或同 MetaServer 的 P2P mesh），不同保证互不可见；qwen3 把 hidden_size/intermediate_size/vocab_size 连同 KV 几何编进 namespace，防住「4B 与 8B 几何相同导致静默交叉命中」。
- `P2pConfig` 三字段（MetaServer 地址、可路由的字面 `IP:port`、RDMA NIC 列表）交给 `OffloadHost::new` 做三道 fail-fast 校验：NIC 非空、地址可解析为 `SocketAddr`、端口先 bind——宁可启动失败，不让 P2P 静默失效。
- `host()` 把公开配置拆成 `pub(crate)` 的 `HostConfig`：宿主字段给私有宿主构造，身份字段留给 `register`；想共享宿主的调用方走 `with_arenas_on`，完全不经过 `OffloadConfig`。

## 7. 下一步学习建议

- **下一讲 u2-l2（GPU 布局映射）**：`OffloadEngine::new` 里与配置同场出现的 `Registration::from_buffer` 是本 crate 最核心的数学——page-first KvBuffer 如何拆成 per-layer KvArena。理解它之后你会明白 `with_arenas_on` 的 `arenas`/`page_first` 参数为什么必须显式存在。
- **u2-l3（共享宿主）**：本讲两次路过 `OffloadHost::new` 都只看了配置流动；runtime、`PegaEngine`、P2P 生命周期与 drop 语义值得整讲精读，尤其 `runtime.enter()` 守卫与 GC 双清扫。
- **延伸阅读**：`docs/subsystems/runtime/pegaflow-offload-integration.md`（配置背后的整体集成图）与 `docs/models/qwen3/pd-disaggregation-m2.md`（P2pConfig 三个字段撑起来的 P/D 数据面实测）。
