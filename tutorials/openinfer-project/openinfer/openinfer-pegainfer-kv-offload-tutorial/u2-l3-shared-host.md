# 共享宿主 OffloadHost：tokio runtime、PegaEngine 与 P2P 生命周期

## 1. 本讲目标

上一讲（u2-l1）我们读完了配置层：`OffloadConfig` 用 `host()` 方法把「宿主半边」拆成了 crate 私有的 `HostConfig`。本讲就顺着这条线，精读 `host.rs`（全文件仅 157 行），搞清楚 `HostConfig` 最终喂给了谁、发生了什么。学完本讲，你应该能够：

1. 解释 OffloadHost 的三个字段（`engine` / `runtime` / `p2p_shutdown`）各自的职责，以及「一个 host 服务任意多个 rank 级引擎」的 DP-rank 共享模型。
2. 说出 `PegaEngine` 为什么必须在 `runtime.enter()` 守卫内构建。
3. 逐一列举 `OffloadHost::new` 返回 `Err` 的全部位置，特别是 P2P 启动路径的三个 fail-fast 校验（RDMA NIC 非空、`advertise_addr` 可解析为 `SocketAddr`、端口 bind 先行）。
4. 描述 1 分钟一次的 GC 双清扫各自回收哪类垃圾、分别对应哪种对端故障。
5. 复述 drop 语义：最后一个句柄 drop 时，runtime、在途 save、P2P 服务与 flush 屏障各自发生什么。

## 2. 前置知识

本讲涉及的 Rust / tokio 概念都不深，但先用两句话把直觉建立起来：

- **tokio runtime（运行时）**：一个自带线程池的任务调度器。`runtime.spawn(async move {...})` 把一个异步任务交给它后台执行；`Builder::new_multi_thread()` 创建带 worker 线程池的运行时，`enable_all()` 打开它的 IO 与定时器驱动。
- **`tokio::spawn` 的上下文要求**：`tokio::spawn` 并非随处可调——它从当前线程的线程局部变量里取「我正处在哪个 runtime 里」。若调用线程不在任何 runtime 上下文中，它会直接 panic。`runtime.enter()` 返回一个守卫（guard），守卫存活期间当前线程被登记为「在该 runtime 内」，spawn 即可正常工作；守卫 drop 时线程脱离上下文。
- **oneshot 与 mpsc channel**：oneshot 是「只能发一次」的通道，常被用作取消信号——发送端被 drop 时，接收端的 `await` 立刻返回，等于一声「停」。`std::sync::mpsc` 是标准库的同步多生产者通道，它的 `recv()` 是阻塞调用，适合在同步代码里等一个异步任务的结果。
- **fail-fast（快速失败）**：把「将来某天静默出错」提前成「启动时大声报错」。配置错误的代价随发现时间指数增长，这是本讲 P2P 启动路径反复出现的主题。
- **TTL 与 GC**：TTL（time-to-live）是「这条记录最多活多久」的期限；GC（garbage collection）在这里指周期性巡检并清掉过期记录的循环。分布式系统里，锁和缓存条目必须有 TTL 兜底，因为持有它们的对端可能随时崩溃、永远不再回来。

另外请回忆 u2-l1 的结论：`HostConfig` 是 crate 私有结构体，只含宿主字段（pinned 池容量、hugepages、线程数、p2p）；身份字段（`instance_id` / `namespace` / `device_id`）留在 `OffloadConfig` 里给注册用。本讲要讲的，就是 `HostConfig` 的归宿。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲的用法 |
|------|------|-----------|
| [src/host.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/host.rs) | `OffloadHost` 的全部实现：runtime、PegaEngine、P2P serving、GC、drop | 本讲核心，逐段精读 |
| [src/config.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/config.rs) | `P2pConfig` / `HostConfig` 定义与 `host()` 拆分 | 引用 P2P 三字段的约束说明 |
| [src/engine.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs) | `OffloadEngine` 的两条构造入口 | 只看 `new`（私有 host）与 `with_arenas_on`（共享 host）如何触碰 OffloadHost，注册细节留给下一讲 |

`OffloadHost` 类型本身在 [src/lib.rs:L31](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/lib.rs#L31) 公开导出，但它的 `new` 是 `pub(crate)`——外部代码只能「持有和传递 `Arc<OffloadHost>`」，不能自己造一个。这个可见性设计在 4.1 里解释。

## 4. 核心概念与源码讲解

### 4.1 OffloadHost：结构体与 DP-rank 共享模型

#### 4.1.1 概念说明

`OffloadEngine`（下一讲的主角）是「一个 rank 的 GPU KV 缓存与 pegaflow 深层之间的桥」。那么 runtime、pinned pool、P2P 服务这些**进程级**资源为什么要跟着每个引擎走一遍？——不需要。`OffloadHost` 就是把这些进程级资源抽出来单独持有：

- 一个 `PegaEngine`：pegaflow 的引擎实例，CPU pinned 池的实际所有者；
- 一个多线程 tokio `Runtime`：驱动 pegaflow 所有异步操作（save / query / load / P2P serving / GC）的调度器；
- 一个可选的 `p2p_shutdown` oneshot 发送端：drop 时叫停 P2P 服务任务。

「共享」的含义见结构体文档注释里的 **DP-rank 共享模型**：数据并行（DP）下多个 rank 通常持有相同的模型权重副本，各自处理不同请求。每个 rank 各建一个 `OffloadEngine`、注册成**自己的** pegaflow instance（有自己的 `instance_id` 和 GPU arena），但它们可以搭在**同一个** `OffloadHost` 上——所有块都落进同一个 host 池，按 `(namespace, hash)` 寻址。只要 namespace 相同，任何 rank 都能恢复任何 rank 存过的块：rank 0 prefill 出来的前缀，rank 1 接到同前缀的新请求时直接命中。

这个「共享」为什么是安全的？文档注释给了一个精确的论证：调用方只应在「KV 跨 instance 可互换」时共享 namespace。对复制权重的 DP rank，这成立的容忍度与复用本 rank 自己的前缀缓存相同——不同 batch 形状下 FP 归约顺序不同会导致字节级差异，但这就像「同一段前缀本地重算两次也会逐字节不同」一样，是浮点推理的固有属性，不是新的风险。

#### 4.1.2 核心流程

```text
                 ┌───────────────────────────────┐
                 │      一个 OffloadHost          │
                 │  PegaEngine + Runtime (+P2P)   │
                 └──────────────┬────────────────┘
                                │ host 池按 (namespace, hash) 寻址
          ┌─────────────────────┼─────────────────────┐
          │                     │                     │
   OffloadEngine(rank0)   OffloadEngine(rank1)   OffloadEngine(rank2)
   instance_id = "r0"     instance_id = "r1"     instance_id = "r2"
   namespace  = "qwen3"   namespace  = "qwen3"   namespace  = "qwen3"
   各自的 GPU arena        各自的 GPU arena        各自的 GPU arena
```

- 引擎是「rank 级」的：一个 rank 的注册（GPU 布局、layer 名、device）只描述自己那块卡。
- 宿主是「进程级」的：pinned 池、runtime、对外服务只有一份。
- 命中判定与 instance 无关，只看 `(namespace, hash)`——这正是 u1-l2 讲过的内容寻址在共享模型里的落点。

#### 4.1.3 源码精读

结构体定义与完整的设计说明在 [src/host.rs:L15-L37](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/host.rs#L15-L37)——这段文档注释值得整段读完，它把共享模型与 drop 语义（4.4 展开）都写清了：

```rust
/// One host serves any number of rank-level `OffloadEngine`s. That is the
/// DP-rank sharing model: each rank registers its own GPU arenas as its own
/// pegaflow *instance*, but blocks land in the one host tier keyed by
/// `(namespace, hash)` — with a shared namespace, any rank restores what any
/// rank saved. ...
pub struct OffloadHost {
    pub(crate) engine: Arc<PegaEngine>,
    pub(crate) runtime: Runtime,
    /// `Some` when P2P is on: resolves the P2P serving tasks (gRPC transfer
    /// service + transfer-lock GC) on drop.
    #[allow(dead_code)]
    p2p_shutdown: Option<oneshot::Sender<()>>,
}
```

三个字段三个角色：`engine` 用 `Arc` 包着（P2P 服务任务和 GC 任务要各 clone 一份进去）；`runtime` 按 tokio 约定不能被 clone，随宿主生灭；`p2p_shutdown` 上的 `#[allow(dead_code)]` 是因为该字段**从不被读取、只在 drop 时起作用**——oneshot 发送端被 drop 本身就是信号（接收端的 `shutdown_rx.await` 因此返回），Rust 编译器不知道这种「drop 即用途」的惯用法，需要手动消音。

两条构造入口怎么用它，见 [src/engine.rs:L101-L116](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L101-L116) 与 [src/engine.rs:L130-L146](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L130-L146)：

```rust
// 私有 host 路径：OffloadEngine::new 内部自建宿主，随后随引擎一起被持有
let host = OffloadHost::new(config.host())?;

// 共享 host 路径：调用方先拿到 Arc<OffloadHost>，再往上面挂一个引擎
pub fn with_arenas_on(
    host: Arc<OffloadHost>,
    instance_id: impl Into<String>,
    namespace: &str, ...
```

私有路径下宿主与引擎同生共死；共享路径下宿主的寿命由所有引擎的 `Arc` 共同决定——最后一个 `Arc` 被 drop 时宿主才消失。这也解释了可见性设计：`OffloadHost` 类型公开（外部要能写 `Arc<OffloadHost>` 参数类型），`new` 却是 `pub(crate)`，而 `HostConfig` 本身 crate 私有——于是**外部无法绕过 `OffloadEngine` 的构造函数凭空造宿主**，宿主的配置流转（`OffloadConfig::host()` 拆分）始终在本 crate 掌控之中。

#### 4.1.4 代码实践

本模块的实践是纯阅读型的对照练习：

1. **实践目标**：确认「共享宿主」不是文档口号，而是被类型系统真实支撑的。
2. **操作步骤**：
   - 读 [src/engine.rs:L101-L116](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L101-L116)（私有路径），数一数从 `OffloadConfig` 到 `OffloadHost::new` 之间经过了几次所有权转移（答案线索：`config.host()` 消费了配置的哪半边？`config.instance_id` 为什么还能在下一行用？——`host()` 只按值拷贝宿主字段、`instance_id` 是 `String` 字段被 `register` 拿走）。
   - 读 [src/engine.rs:L130-L146](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L130-L146)（共享路径），注意 `host: Arc<OffloadHost>` 是按值传入并被存进引擎的字段（[src/engine.rs:L71](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L71)）。
3. **需要观察的现象**：两条路径最终都汇入同一个私有函数 `Self::register(...)`（[src/engine.rs:L148](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L148)），差异只在「宿主从哪来」和「布局从哪来」。
4. **预期结果**：你能在不看讲义的情况下说出——私有路径 = 自建 host + `Registration::from_buffer`；共享路径 = 外来 host + `Registration::from_arenas`。

#### 4.1.5 小练习与答案

**练习 1**：两个部署在同一进程里的 DP rank 想互相恢复对方存的块，除了搭同一个 `OffloadHost`，还必须满足什么条件？这个条件为什么对「复制权重的 DP rank」成立、对「两个不同模型」不成立？

**答案**：必须共享同一个 `namespace`（且各自 arena 的几何在 namespace 内一致）。namespace 是内容寻址域（u2-l1）：相同才互相命中，不同则互不可见。对复制权重的 DP rank，同一前缀的 KV 在浮点容忍度下可互换——文档注释指出其字节差异（FP 归约顺序随 batch 形状变化）与「本地重算同一前缀两次」的差异同级，属推理固有属性；两个不同模型的 KV 语义完全不同，共享 namespace 会造成静默串块，这正是 namespace 要把模型几何编进去的原因。

**练习 2**：`OffloadHost` 在 `lib.rs` 公开导出，`new` 却是 `pub(crate)`。这种「类型公开、构造器私有」的组合达到什么效果？

**答案**：外部代码可以持有、传递、跨引擎共享 `Arc<OffloadHost>`（`with_arenas_on` 需要这 ability），但无法凭空构造宿主——创建宿主的唯一入口是 `OffloadEngine` 的构造函数，而它们只接受 `OffloadConfig`（公开类型）。配合 `HostConfig` 的 crate 私有，宿主配置的拆分与流转完全收在 crate 边界内，外部既不能注入非法宿主配置，也不能绕过引擎注册流程拿到「没有注册任何 arena 的空宿主」之外的怪状态。

### 4.2 宿主构造：多线程 runtime 与 enter() 守卫内的 PegaEngine

#### 4.2.1 概念说明

`OffloadHost::new` 的前半段做两件事：建 runtime、建 PegaEngine。第二件事有一个不显眼但致命的时序要求——**PegaEngine 必须在 runtime.enter() 守卫内构建**。

原因：pegaflow 的 `MetaServerClient`（P2P 模式下负责向 MetaServer 注册块哈希的客户端）在**构造时**就用 `tokio::spawn` 启动后台注册循环。而 `tokio::spawn` 只在「当前线程处于某个 runtime 上下文」时才能调用。`OffloadHost::new` 是一个普通同步函数，跑在调用方的线程上——此刻 runtime 刚建好，但这个线程并没有「进入」它。若直接构造 PegaEngine，`MetaServerClient` 内部的 `tokio::spawn` 会 panic（找不到当前 runtime）。`runtime.enter()` 的守卫把这个线程临时登记进 runtime，让构造期间的 spawn 全部落到**我们自己的** runtime 上。

注意一个容易混淆的点：**不开 P2P 也需要 enter()**。守卫包住的是 `PegaEngine::new_with_config` 整体，与 `config.p2p` 是否为 `Some` 无关——源码注释只以 MetaServerClient 为例说明动机，但守卫对两条路径统一生效，这也让代码无需为 P2P 与非 P2P 维护两套构建方式。

#### 4.2.2 核心流程

```text
OffloadHost::new(HostConfig)
  │
  ├─ 1. Builder::new_multi_thread()
  │       .worker_threads( max(runtime_threads, 1) )   // 0 会被钳到 1
  │       .enable_all()                                 // IO + 定时器驱动
  │       .build()
  │       └─ 失败 → Err#1（EngineError::Storage）
  │
  ├─ 2. StorageConfig::default()
  │       若 config.p2p = Some(p2p):
  │         ├─ rdma_nics 为空 → Err#2（EngineError::InvalidArgument）
  │         └─ 注入 rdma_nic_names / metaserver_addr / advertise_addr
  │
  └─ 3. { let _guard = runtime.enter();
             PegaEngine::new_with_config(pinned_pool_bytes,
                                         use_hugepages,
                                         storage_config)?   // Err 原样上抛
         }  // 守卫在此 drop，线程脱离 runtime 上下文
```

线程数的默认值来自配置层：[src/config.rs:L41-L44](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/config.rs#L41-L44) 的注释说「两个就够了：save 是 fire-and-forget，query 只是一次短暂的内存缓存查找」。但开了 P2P 后这个 runtime 还要同时承载 gRPC serving 与 GC 循环（4.3 / 4.4），所以它是多线程 runtime 而非单线程的 `new_current_thread`。

#### 4.2.3 源码精读

runtime 构建在 [src/host.rs:L40-L45](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/host.rs#L40-L45)——这是第一个 Err 点，runtime 建不起来（例如线程资源耗尽）被包装成 `EngineError::Storage`：

```rust
let runtime = tokio::runtime::Builder::new_multi_thread()
    .worker_threads(config.runtime_threads.max(1))
    .enable_all()
    .build()
    .map_err(|e| EngineError::Storage(format!("offload runtime build: {e}")))?;
```

StorageConfig 注入与 NIC 校验在 [src/host.rs:L47-L57](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/host.rs#L47-L57)——不开 P2P 时保持 `StorageConfig::default()`（纯本机 host 层）；开了 P2P 才把三个字段搬进去，且搬之前先做「RDMA NIC 非空」校验（Err#2，fail-fast 之一）：

```rust
let mut storage_config = StorageConfig::default();
if let Some(p2p) = &config.p2p {
    if p2p.rdma_nics.is_empty() {
        return Err(EngineError::InvalidArgument(
            "P2P requires at least one RDMA NIC".into(),
        ));
    }
    storage_config.rdma_nic_names = Some(p2p.rdma_nics.clone());
    ...
}
```

enter() 守卫在 [src/host.rs:L58-L67](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/host.rs#L58-L67)，注释原文把动机钉死了：

```rust
// pegaflow's MetaServerClient spawns its background registration loop
// with tokio::spawn, so the engine must be built inside our runtime.
let engine = {
    let _guard = runtime.enter();
    Arc::new(PegaEngine::new_with_config(
        config.pinned_pool_bytes,
        config.use_hugepages,
        storage_config,
    )?)
};
```

注意 `?` 直接上抛 `PegaEngine` 自己的错误（这是本讲流程图里唯一的「隐式 Err 点」，不改变错误类型也不加上下文）；块表达式结束后守卫 `_guard` 先于 `engine` 绑定 drop，线程随即脱离 runtime 上下文——之后的 `OffloadHost::new` 代码又是普通同步世界，直到显式 `runtime.spawn`。

#### 4.2.4 代码实践

用一个最小例程亲眼看见「enter() 缺失会发生什么」（**示例代码**，非项目源码，需本地有任意 tokio 依赖的 Rust 工程验证）：

1. **实践目标**：验证 `tokio::spawn` 在 runtime 上下文外调用会 panic、在 `enter()` 守卫内正常工作。
2. **操作步骤**：在一个临时 crate 的 `fn main()`（注意：不要在 `#[tokio::main]` 里）中写入：

   ```rust
   // 示例代码：演示 enter() 守卫的作用，与仓库代码无关
   fn main() {
       let rt = tokio::runtime::Builder::new_multi_thread()
           .worker_threads(2)
           .enable_all()
           .build()
           .unwrap();

       // 情形 A：去掉下行的 enter 守卫直接 spawn —— 观察 panic 信息
       // 情形 B：保留守卫 —— 观察任务正常被调度执行
       let _guard = rt.enter();
       tokio::spawn(async {
           println!("spawned task ran: inside runtime context");
       });
       drop(_guard);

       std::thread::sleep(std::time::Duration::from_millis(100));
   }
   ```

3. **需要观察的现象**：情形 A panic，报错形如 `must be called from the context of a Tokio 1.x runtime`；情形 B 正常打印。
4. **预期结果**：与 [src/host.rs:L58-L59](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/host.rs#L58-L59) 的注释互证——若把 `PegaEngine::new_with_config` 挪出守卫，pegaflow 内部那个 `tokio::spawn` 的遭遇与情形 A 相同。待本地验证（本仓库环境下不运行无关示例）。

#### 4.2.5 小练习与答案

**练习 1**：`OffloadConfig` 默认 `runtime_threads = 2`（[src/config.rs:L57](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/config.rs#L57)）。若调用方显式传 0，`OffloadHost::new` 会怎样？

**答案**：`.max(1)` 把它钳到 1（[src/host.rs:L42](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/host.rs#L42)），得到单 worker 的多线程 runtime，不会 panic 也不会是非法状态——这是把「调用方传了不合法值」就地规范化的防御式写法，比报错更合适，因为 0 只是无意义而非危险。

**练习 2**：为什么这里必须用 `new_multi_thread`，而不能用更轻量的 `new_current_thread`？

**答案**：这个 runtime 上同时挂着四类常驻工作——fire-and-forget 的 save D2H、短促的 query 查找、P2P 模式下的 gRPC serving 循环、1 分钟一次的 GC 循环（4.3、4.4 都 `runtime.spawn` 在它上面）。`new_current_thread` 的任务只在 `block_on` 驱动期间执行，任何一个长期 `await` 的循环都会饿死其它任务；多线程 runtime 让 worker 池并行调度这些任务，`runtime_threads` 才有调参意义。

### 4.3 P2P serving 启动路径：三个 fail-fast 与 ready channel

#### 4.3.1 概念说明

P2P 模式（`config.p2p = Some`）意味着本实例加入跨实例共享网格：把存的块哈希注册到 MetaServer、在 `advertise_addr` 上服务对端的 RDMA 握手与块查询。启动这段服务面的代码面对一个经典问题：**异步任务里的同步错误如何报告给同步的构造函数**——`TcpListener::bind` 发生在 spawn 出去的 async 任务里，可「端口被占」必须在 `OffloadHost::new` 返回前就变成 `Err`，否则部署者要到第一个 peer 连不上时才发现。

解法是 fail-fast 三件套加一条 ready channel：

1. **RDMA NIC 非空**（4.2 已见）：没有网卡却开 P2P 是配置矛盾，当场拒绝。
2. **`advertise_addr` 必须可解析为 `SocketAddr`**：它在 [src/config.rs:L15-L20](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/config.rs#L15-L20) 的文档里写明「字面 socket 地址——它兼作内嵌传输服务的 bind 地址，所以 hostname 在启动时被拒」，且跨节点使用不得是 `0.0.0.0`/`127.0.0.1`。用一个既能通告又能 bind 的值，免去配置两个字段的口径问题。
3. **端口 bind 先行**：serve 任务第一件事就是 bind，成功失败都经 ready channel 回话；`new` 阻塞在 `recv()` 上等回话，把异步世界的结果折算成本次调用的 `Ok`/`Err`。

ready channel 用的是 `std::sync::mpsc` 而非 tokio 通道：接收端是同步的 `new()`（此刻我们不在任何 runtime 上下文里），标准库的阻塞 `recv()` 恰好是要的语义，不必动用 `Handle::block_on`。

#### 4.3.2 核心流程

```text
match config.p2p
  ├─ None → p2p_shutdown = None（进程只做本机 host 层，不起服务面）
  └─ Some(p2p):
       ① listen = advertise_addr.parse::<SocketAddr>()?
            └─ 解析失败 → Err#3（InvalidArgument）
       ② 建 shutdown oneshot（drop 即停机信号）
          建 ready mpsc（std 同步通道）
       ③ runtime.spawn(serve 任务):
            TcpListener::bind(listen).await
              ├─ Ok(l)  → ready_tx.send(Ok(()))
              │           → TcpListenerStream 包装
              │           → P2pTransferService::serve_with_incoming(
              │                 engine, incoming, async { shutdown_rx.await })
              └─ Err(e) → ready_tx.send(Err("bind …")) → 任务返回（不服务）
       ④ ready_rx.recv() 阻塞等回话
            ├─ 任务没回话就死   → Err#4（Storage: "P2P serve task died at startup"）
            └─ 回话内容是 Err   → Err#5（Storage: bind 失败详情）
       ⑤ runtime.spawn(GC 循环)        —— 见 4.4
       ⑥ p2p_shutdown = Some(shutdown_tx)
```

`serve_with_incoming` 的第三个参数是 shutdown future（`shutdown_rx.await`）：它完成时 serving 优雅收尾。而 oneshot 的特性保证了**发送端无论主动发送还是被 drop，接收端的 `await` 都会返回**——所以「drop 宿主」天然成为「停服务」。

#### 4.3.3 源码精读

地址解析校验在 [src/host.rs:L72-L79](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/host.rs#L72-L79)，这是 Err#3：

```rust
let listen: std::net::SocketAddr = p2p.advertise_addr.parse().map_err(|e| {
    EngineError::InvalidArgument(format!(
        "P2P advertise_addr {:?} is not a socket address: {e}",
        p2p.advertise_addr
    ))
})?;
```

serve 任务的 spawn 与 bind-first 在 [src/host.rs:L80-L106](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/host.rs#L80-L106)。注意两件小事：`serve_engine` 是 `Arc::clone(&engine)` 进任务（与宿主共享所有权）；bind 的注释明说动机——「启动时大声失败，而不是 P2P 静默地永不服务」：

```rust
let (shutdown_tx, shutdown_rx) = oneshot::channel::<()>();
let serve_engine = Arc::clone(&engine);
let (ready_tx, ready_rx) = std::sync::mpsc::channel::<Result<(), String>>();
runtime.spawn(async move {
    // Bind eagerly so startup fails loud on a taken port
    // instead of P2P silently never serving.
    let bound = tokio::net::TcpListener::bind(listen).await;
    ...
});
```

`new` 这边阻塞收账在 [src/host.rs:L107-L110](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/host.rs#L107-L110)——两层 `map_err` 分别对应 Err#4（连回话都没有：任务在 send 之前就死了）与 Err#5（回话本身是 bind 失败）：

```rust
ready_rx
    .recv()
    .map_err(|_| EngineError::Storage("P2P serve task died at startup".into()))?
    .map_err(EngineError::Storage)?;
```

成功路径的收尾在 [src/host.rs:L141-L145](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/host.rs#L141-L145)：打一条 `log::info`（服务地址 + MetaServer 地址，部署排障第一线索），返回 `Some(shutdown_tx)` 存进宿主字段。

#### 4.3.4 代码实践

1. **实践目标**：体验三个 fail-fast 各自拦截哪类坏配置，理解「错误配置的发现时机」如何被提前。
2. **操作步骤**（纯源码阅读 + 推演，不改仓库代码）：对下面三组假想配置，先自己判定 `OffloadHost::new` 的返回结果与错误信息，再到源码里核对行号：
   - A：`P2pConfig { metaserver_addr: "http://10.0.0.100:50056", advertise_addr: "10.0.0.7:50057", rdma_nics: [] }`
   - B：`advertise_addr: "node7.example.com:50057"`（其余正常）
   - C：`advertise_addr: "10.0.0.7:22"`（端口已被 sshd 占用，其余正常）
3. **需要观察的现象**：A 在进入 P2P 分支后立刻 Err#2；B 在 parse 处 Err#3；C 通过所有同步校验，直到 serve 任务 bind 失败、ready channel 传回 Err#5。
4. **预期结果**：A → `InvalidArgument("P2P requires at least one RDMA NIC")`；B → `InvalidArgument("P2P advertise_addr \"node7.example.com:50057\" is not a socket address: …")`；C → `Storage("bind 10.0.0.7:22: …")`。三者都发生在 `OffloadHost::new` 返回之前——这就是 fail-fast 的全部含义：**坏配置永远活不过构造函数**。

#### 4.3.5 小练习与答案

**练习 1**：假如去掉 ready channel、bind 失败只记一条日志（`log::error!` 后任务返回），部署者会在什么时候、以什么症状发现问题？

**答案**：构造成功、服务静默缺席。对端从 MetaServer 查到本实例是某块的 owner、拨 `advertise_addr` 却永远连不上，表现为跨节点命中率异常与对端反复重试——这类「运行期才显形的启动错误」排障成本远高于一条启动 Err。这正是 bind 注释里 "silently never serving" 指向的故障形态。

**练习 2**：shutdown 信号为什么选 oneshot 的 `Sender<()>` 而不是 `Arc<AtomicBool>` 轮询或 `CancellationToken`？

**答案**：本仓库的用法只需要「一次性、无需携带数据、drop 即触发」——oneshot 的 `Receiver::await` 在 Sender drop（或发送）时立即完成，天然映射 `serve_with_incoming` 接受的 shutdown future，零轮询、零额外依赖；宿主 drop 时 `p2p_shutdown` 字段被 drop，信号自动发出，不需要任何显式 close 调用（这就是字段上 `#[allow(dead_code)]` 的底气：从不读取，drop 即用途）。

### 4.4 GC 双清扫与 drop 语义

#### 4.4.1 概念说明

P2P 一旦开起来，本实例的 host 池里会积累两类「别人留下的」状态，它们的共同点是：**合法产生、无人负责清理**。

- **过期 transfer lock（传输锁）**：对端 peer 发起单边 RDMA READ 之前，会先在本端给目标块上锁，防止读到一半块被驱逐改写。正常流程是读完解锁；但 peer 若在读取中途崩溃，锁的持有者永远消失，谁也不会来解锁。不清扫的后果：被锁的块无限期钉在 host 池，等效内存泄漏。
- **陈旧 in-flight（预取状态）**：本端 query miss 时会发起后台远端拉取（Loading 态，见 u2-l7）。若请求在拉取中途被放弃——请求被 drop，或 executor 的 re-query deadline 触发重查——已完成（或已失败）的拉取任务留下孤儿条目，把拉来的块占在 pinned pool 里。

GC 循环每 1 分钟 tick 一次，对这两类垃圾各扫一遍。两类垃圾对应两类故障方向：**前者是对端崩溃在我处留下的锁，后者是本端请求被遗弃后替对端拉来的货**。

drop 语义则是宿主的生命周期终局。因为 `OffloadHost` 只以 `Arc` 流通，最后一个 `Arc` 被 drop 时结构体字段按声明顺序销毁（`engine` → `runtime` → `p2p_shutdown`），runtime 的关闭随即带走它上面的一切。文档注释（[src/host.rs:L25-L29](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/host.rs#L25-L29)）逐项写了后果，4.4.3 精读里照录。

#### 4.4.2 核心流程

GC 循环的节奏与扫描对象：

```text
每 60s 一次 tick（MissedTickBehavior::Skip：错过了不补发，防追帧风暴）
  ├─ gc_expired_transfer_locks()          // 同步调用，返回释放条数
  │     释放：TTL 已过的对端传输锁
  └─ gc_stale_inflight(5min, 5min).await  // 异步调用，返回 (stale, failed)
        释放：年龄超过 5 分钟的预取条目 + 失败远端标记
  任何一项 > 0 都打日志（warn / info）
```

被锁块的实际最长滞留时间是一个区间而不是定值：锁自身带 TTL，GC 每 60 秒巡一次，所以一个被崩溃 peer 遗留的锁最长把块钉到「锁 TTL + 最多一个 GC 周期」，即滞留 \( \in [\text{TTL},\ \text{TTL} + 60\text{s}) \)（TTL 数值定义在 pegaflow-core 内，此处不展开）。

drop 语义的因果链：

```text
最后一个 Arc<OffloadHost> drop
  → 结构体字段销毁：engine(Arc) → runtime → p2p_shutdown(oneshot::Sender)
  → Runtime 关闭：其上所有任务终止
      ├─ 在途 fire-and-forget save 被抛弃        // 可接受：host 层只是缓存
      ├─ P2P serving 任务与 GC 循环停止
      │    （oneshot Sender 的销毁同样让 shutdown_rx.await 返回——双保险）
      └─ 在途 flush_saves_then 屏障被取消
           → 其 then 回调不运行、直接 drop       // 见 u2-l8 的屏障语义
  → 对端 peer 视角：本实例从 MetaServer 消失/不可达 → 退化为各自本地 prefill
```

#### 4.4.3 源码精读

GC 循环本体在 [src/host.rs:L112-L140](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/host.rs#L112-L140)。注释先声明它「镜像 pegaflow-server 的同名任务」，然后解释双清扫各自防什么：

```rust
// Background GC, mirroring pegaflow-server's task. Two sweeps:
// expired transfer locks (a crashed peer must not pin our
// blocks past the lock timeout) and stale prefetch state — an
// abandoned remote fetch (request dropped mid-RemoteFetch, or
// the executor's re-query deadline fired) leaves an orphaned
// entry whose completed task pins its fetched blocks in the
// pinned pool until this sweep drops it.
```

循环骨架（节选）：

```rust
const STALE_MAX_AGE: std::time::Duration = std::time::Duration::from_mins(5);
let mut tick = tokio::time::interval(std::time::Duration::from_mins(1));
tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
loop {
    tick.tick().await;
    let expired = gc_engine.gc_expired_transfer_locks();
    ...
    let (stale, failed) = gc_engine.gc_stale_inflight(STALE_MAX_AGE, STALE_MAX_AGE).await;
    ...
}
```

两个可留意的细节：`MissedTickBehavior::Skip` 让被任务拥延误点的 tick 直接跳过而不补发，GC 永远不会为「赶进度」连环扫描；两个常量（1 分钟 tick、5 分钟最大年龄）都是**清垃圾的保守上限**而非正确性参数——扫晚一点只是垃圾多占一会儿内存，不损任何推理正确性。

drop 语义的权威表述在 [src/host.rs:L25-L29](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/host.rs#L25-L29) 的结构体文档注释：

> Dropping the last handle drops the [`Runtime`], which abandons any in-flight fire-and-forget saves (acceptable — the host tier is a cache) and stops the P2P serving tasks; peers degrade to their own local prefill. In-flight `OffloadEngine::flush_saves_then` barriers are cancelled too, dropping their `then` callbacks unrun.

每一项都承接前面讲义立过的原则：丢 save 只损命中机会（u1-l2 的「save 尽力而为」红线）；对端退化到本地重算是 P/D 故障语义的标准出口（u3-l2 展开）；屏障取消则意味着**正在 withhold 的 Finished 事件就此悬置**——调用方必须把「宿主可能整体消失」纳入容错考量。

#### 4.4.4 代码实践

1. **实践目标**：把「双清扫 × 两种故障」的对应关系落到能向别人复述的程度。
2. **操作步骤**：阅读 [src/host.rs:L112-L140](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/host.rs#L112-L140) 后，在笔记里画一张 2×2 表格：行是 `gc_expired_transfer_locks` / `gc_stale_inflight`，列是「垃圾是谁造成的」「谁消失/被放弃后垃圾遗留」「不扫会怎样」，填完后与源码注释逐句核对。
3. **需要观察的现象**：两行的「责任人」完全不同——一行在对端（peer 崩溃），一行在本端（自己的请求被放弃）。
4. **预期结果**：表格第一行 = 对端上锁后崩溃 / 锁无人释放 / 块被永久钉住（内存泄漏）；第二行 = 本端请求 drop 或 re-query deadline 触发 / 孤儿预取条目 / pinned pool 被完成的拉取占用。另外补一句话：为什么这两类垃圾都**不能**在产生点同步清理？（提示：产生点分别是「对端进程」与「已被 drop 的请求」——两者在清理时刻都已经不在场，只有周期性兜底扫除可行。）

#### 4.4.5 小练习与答案

**练习 1**：GC 的 1 分钟 tick 会不会与 save/load 抢 worker 线程导致推理抖动？

**答案**：会占用但影响可忽略。GC 与推理不在同一世界：D2H/H2D 数据搬运走 CUDA 侧，GC 只动 host 内存里的元数据与 pinned pool 条目；且每 60 秒一次的两次调用（一个同步、一个短暂 `await`）在 `runtime_threads` 默认 2 个 worker 上是毫秒级的工作量。真正要紧的是它**必须在某个 runtime 上常驻**——这正是它与 serving 同住宿主 runtime 的原因。

**练习 2**：最后一个 `Arc<OffloadHost>` 被 drop 时，恰好有一个 `flush_saves_then` 屏障在等一个在途 save。屏障里排队的 `then` 回调（比如「发出 Finished 事件」）会执行吗？这正确吗？

**答案**：不执行——文档明说屏障被取消、`then` 回调 unrun 直接 drop。这是**有意的**正确取舍：宿主消失意味着整个进程的 offload 能力退场，此刻若还去发「KV 已就绪」信号反而是错误信号。代价是该请求的 Finished 事件悬置、客户端侧等不到响应，由上层（HTTP 连接断开/超时）兜底——与「对端退化为本地 prefill」同属一个故障语义家族：宁可显式失败，不可谎报成功。

## 5. 综合实践

把本讲三个最小模块串起来，完成规格里的综合任务：**画出 `OffloadHost::new` 的完整流程图（含带/不带 P2P 两个分支），标出每个可能返回 `Err` 的点；再用注释回答两个理解题**。

**第一步：流程图。** 下面是参考答案（请先自己画一遍再对照）——共 5 个显式 `return Err`/`map_err` 点，外加 1 个 `?` 原样上抛的隐式错误点：

```text
OffloadHost::new(HostConfig)
│
├─① 构建多线程 runtime（worker_threads = max(runtime_threads,1)，enable_all）
│    └─ Err#1 EngineError::Storage("offload runtime build: …")
│
├─② StorageConfig::default()
│    └─ config.p2p = Some 时：
│         rdma_nics 为空？ ──是──→ Err#2 InvalidArgument("P2P requires at least one RDMA NIC")
│         否则注入 rdma_nic_names / metaserver_addr / advertise_addr
│
├─③ { runtime.enter() 守卫 } 内构建 PegaEngine::new_with_config(pinned, hugepages, storage)
│    └─ Err(隐式) PegaEngine 错误经 `?` 原样上抛
│
├─④ match config.p2p
│    ├─ None ──────────────→ p2p_shutdown = None（无服务面、无 GC）
│    └─ Some(p2p):
│         ├─ advertise_addr.parse::<SocketAddr>()
│         │    └─ Err#3 InvalidArgument("… is not a socket address: …")
│         ├─ 建 shutdown oneshot + ready mpsc(std)
│         ├─ runtime.spawn(serve 任务): TcpListener::bind 先行
│         │    ├─ bind Ok  → ready_tx.send(Ok) → serve_with_incoming(…, shutdown_rx.await)
│         │    └─ bind Err → ready_tx.send(Err("bind …")) → 任务退出
│         ├─ ready_rx.recv() 阻塞等回话
│         │    ├─ 任务未回话即死 → Err#4 Storage("P2P serve task died at startup")
│         │    └─ 回话为 Err    → Err#5 Storage(bind 失败详情)
│         ├─ runtime.spawn(GC 循环: 1min tick, Skip 误点, 双清扫)
│         └─ p2p_shutdown = Some(shutdown_tx)
│
└─ Ok(Arc<OffloadHost { engine, runtime, p2p_shutdown }>)
```

**第二步：两个问题，把答案写成注释**（示例答案如下，建议手写一遍加深记忆）：

```rust
// 问题 1：为什么 PegaEngine 必须在 runtime.enter() 守卫内构建？
//
// 答：pegaflow 的 MetaServerClient 在构造时用 tokio::spawn 启动后台注册循环
// （host.rs L58-59 注释原文）。tokio::spawn 从线程局部上下文取当前 runtime，
// 调用线程不在任何 runtime 内时直接 panic。OffloadHost::new 是同步函数，
// 运行在调用方线程上——runtime 虽已 build，但本线程尚未"进入"它。
// runtime.enter() 的守卫把本线程登记进 runtime 上下文，守卫存活期间
// PegaEngine 构造内部的所有 tokio::spawn 都落到我们自己的 runtime 上；
// 块表达式结束守卫 drop，线程回到同步世界。守卫与 config.p2p 无关：
// 两条路径统一在守卫内构建，不为 P2P 单独维护一套。
//
// 问题 2：GC 为什么同时清扫 expired transfer locks 和 stale in-flight 两类垃圾，
// 各自对应哪种对端故障？
//
// 答：两类垃圾的"责任人"不同：
// (a) expired transfer locks —— 对端崩溃。peer 发起 RDMA READ 前在本端块上
//     加锁防驱逐，peer 中途 crash 后锁无人释放；不清扫则块被永久钉在
//     host 池（内存泄漏）。TTL + 每分钟巡检把滞留封顶在
//     [TTL, TTL + 60s)。
// (b) stale in-flight —— 本端请求被放弃（对端慢或请求中途 drop）。后台远端
//     拉取完成后，被遗弃的请求留下孤儿条目，把拉来的块占在 pinned pool；
//     5 分钟最大年龄的 sweep 将其 drop、归还池容量。
// 一句话：一类是"别人在我这里留下的锁"，一类是"我替别人拉来的货"。
// 两者都无法在产生点同步清理——清理时刻责任人（对端进程/已 drop 的请求）
// 都已不在场，周期性兜底扫除是唯一可行解。
```

**验证方式**：流程图中每个 Err 点都应能在 host.rs 里指出行号（Err#1: L45、Err#2: L50、Err#3: L75、Err#4: L109、Err#5: L110、隐式: L66 的 `?`）。若你自己的图漏了某个点，回到对应小节重读。

## 6. 本讲小结

- **OffloadHost 是进程级资源的三合一容器**：一个 `PegaEngine`（host 池真正的主人）、一个多线程 tokio `Runtime`、一个可选的 P2P 停机信号；引擎（rank 级）可搭多个，宿主只有一份，块按 `(namespace, hash)` 跨引擎共享——这就是 DP-rank 共享模型。
- **可见性即架构**：类型公开、`new` 私有、`HostConfig` crate 私有，三者合力保证宿主只能经 `OffloadEngine` 的两条构造入口（`new` 私有 / `with_arenas_on` 共享）诞生。
- **enter() 守卫是硬约束**：PegaEngine 构造内部有 `tokio::spawn`（MetaServerClient 注册循环），必须让调用线程处在 runtime 上下文，否则 panic。
- **P2P 启动路径五个 Err 点、三道 fail-fast**：NIC 非空、地址必须是字面 `SocketAddr`（兼作 bind 地址）、端口 bind 先行 + ready channel 回话——坏配置永远活不过构造函数。
- **GC 双清扫按责任人分两类**：对端崩溃遗留的传输锁（TTL 兜底）与本端被弃请求的孤儿预取条目（5 分钟年龄兜底），1 分钟一拍、误点跳过不补发。
- **drop 是有意的损失设计**：最后一个句柄消失 → runtime 关闭 → 在途 save 抛弃（缓存语义）、P2P 停服（对端退化本地 prefill）、flush 屏障取消（then 回调 unrun）——宁可显式失败，不可谎报就绪。

## 7. 下一步学习建议

本讲把「宿主从哪来」讲完了，下一讲 **u2-l4（引擎构造与注册）** 接棒讲「引擎怎么长在宿主上」：`register` 如何把 `Registration` 喂给 `register_context_layer_batch_strided` 的十几个参数、`PEGAINFER_KV_TRANSFER_MODE` 的 Direct/Kernel 取舍，以及 `with_arenas_on` 共享路径的完整语义。建议在进入下一讲前：

1. 重读 [src/engine.rs:L148-L203](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L148-L203) 的 `register`，数一数它消费了本讲宿主的哪个字段（答案：`host.engine`，且只用了这一个）。
2. 若你想更早看到 P2P 的完整图景，可跳读 `docs/models/qwen3/pd-disaggregation-m2.md` 的时序部分再回来——u3-l2 会正式展开。
3. 有余力的话翻一下 Cargo.toml 里 pegaflow-core 的 rev pin，对照 u1-l3 的依赖治理结论，理解「P2pTransferService / MetaServerClient 这些外部类型为何必须版本钉死」。
