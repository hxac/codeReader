# Worker 抽象与构建器

## 1. 本讲目标

本讲正式进入第 3 单元「控制面：Worker 生命周期」。第 1、2 单元我们一直从外部看网关（怎么构建、怎么配置、怎么启动），从本讲开始，我们要钻进控制面，看它如何管理「一群后端推理进程」。

本讲只聚焦一个核心问题：**代码里用什么来表示「一个后端推理进程」？** 学完本讲，你应该能做到：

- 说出 `Worker` trait 是什么、为什么用 trait 对象 `Arc<dyn Worker>` 来表示一个 worker。
- 把 `Worker` 的几十个方法归类成「身份 / 健康 / 负载计数 / 熔断 / 模型能力 / DP / gRPC」几组，并知道每组解决什么问题。
- 区分 `WorkerType` 的三种取值（`Regular` / `Prefill` / `Decode`），理解为什么只有 `Prefill` 带着一个 `bootstrap_port`。
- 用 `BasicWorkerBuilder`（以及 `DPAwareWorkerBuilder`）以链式调用构造一个 worker，并说清楚 `build()` 内部到底造出了哪些原子计数器和熔断器。
- 读懂 `ModelCard` 这张「模型能力卡片」，知道它如何从「散落在 labels 里的字符串」演进而来。

本讲是后续 `WorkerRegistry`（u3-l2）、`WorkerManager`（u3-l3）、注册工作流（u3-l5）的地基——那些模块全都建立在 `Worker` 这个抽象之上。

## 2. 前置知识

在开始之前，请确认你理解下面几个 Rust 概念。如果某个概念陌生，先花几分钟看懂它的直觉即可，不必深究语法。

- **trait 与 trait 对象**：trait 是一组方法的「契约」。任何实现了 `Worker` trait 的类型（比如 `BasicWorker`、`DPAwareWorker`），都可以被当作「一个 Worker」来用。用 `Arc<dyn Worker>` 这种「trait 对象」，我们就能把**不同具体类型**的 worker 放进同一个集合里统一管理——这是控制面「注册表」能存在的前提。
- **`#[async_trait]`**：Rust 的 trait 原生不支持 `async fn` 方法，`async_trait` 这个宏把每个 async 方法改写成「返回一个 `Future` 的普通方法」。你只要知道：trait 里出现 `async fn`，上面就一定有 `#[async_trait]`。
- **`Arc` 与原子类型**：网关是高并发的，一个 worker 同时被很多请求读写。`Arc<T>` 提供「线程安全的共享引用」（克隆只增加一个引用计数，几乎免费）；`AtomicUsize` / `AtomicBool` 提供「不加锁也能安全读写的计数器」，这是负载计数、健康标志用原子类型的原因。
- **RAII 与 `Drop`**：Rust 用「对象销毁时自动执行清理」来管理资源（称为 RAII）。本讲的 `WorkerLoadGuard` 就靠实现 `Drop` trait，在「请求结束、guard 被销毁」时自动把负载计数减一——这是理解流式请求负载统计的关键。
- **builder（建造者）模式**：当一个对象字段很多，直接用结构体字面量构造会很难读。builder 模式提供一个链式 API：`.worker_type(...).label(...).build()`，每调用一个方法返回 `self`，最后 `build()` 收口。本讲的 `BasicWorkerBuilder` 就是典型例子。
- **PD 分离部署（Prefill-Decode Disaggregation）**：这是大模型推理的一种部署方式——把「预填充（处理 prompt）」和「解码（逐 token 生成）」拆到两批不同的 worker 上，各自独立扩缩容。两批 worker 之间需要一个「bootstrap」通道传递中间状态（KV cache）。本讲的 `WorkerType::Prefill { bootstrap_port }` 就是为它准备的。

> 一句话回顾 u1-l4：`core` 是被控制面、数据面、可靠性层共同依赖的「抽象底座」。本讲的 `Worker` trait、`WorkerType`、`BasicWorkerBuilder`、`ModelCard` 全都在 `src/core/` 下。它们正是 `core` 作为「底座」最核心的几个抽象。

## 3. 本讲源码地图

本讲涉及的关键文件如下（全部在 `src/core/` 下）：

| 文件 | 作用 |
| --- | --- |
| `src/core/worker.rs` | 定义 `Worker` trait、`WorkerType`、`ConnectionMode`、`HealthConfig`、`WorkerMetadata`，以及两个具体实现 `BasicWorker` / `DPAwareWorker`。是本讲的主战场。 |
| `src/core/worker_builder.rs` | 定义 `BasicWorkerBuilder` 和 `DPAwareWorkerBuilder`，用流式 API 构造上面的两个实现。 |
| `src/core/model_card.rs` | 定义 `ModelCard`（模型能力卡片）和 `ProviderType`（外部厂商类型）。 |
| `src/core/model_type.rs` | 定义 `ModelType`（用 bitflags 表示「支持哪些端点」）和 `Endpoint` 枚举。是 `ModelCard` 依赖的基础类型。 |
| `src/core/circuit_breaker.rs` | 定义 `CircuitBreaker`（熔断器）。本讲只看它如何被「嵌入」每个 worker，熔断器本身的详讲在 u6-l2。 |

阅读建议：先看 `worker.rs` 顶部的 `Worker` trait（看方法分类），再看 `WorkerType` / `ConnectionMode` / `HealthConfig` / `WorkerMetadata` 这几个「数据类型」，最后用 `worker_builder.rs` 的 `build()` 把它们串起来。`ModelCard` 可以单独看，它是相对独立的一块。

## 4. 核心概念与源码讲解

### 4.1 Worker trait：一个后端推理进程的统一抽象

#### 4.1.1 概念说明

在网关眼里，每一个「后端推理进程」（一个 SGLang server、一个 vLLM server、一个 OpenAI 兼容服务……）都被抽象成一个 **Worker**。worker 有一个 URL（去哪里访问它）、能服务某些模型、有自己的健康状态、当前负载、以及一个属于它的熔断器。

为什么要定义一个 **trait**，而不是直接用一个具体结构体？因为 worker 的形态会变化：

- 普通的 worker 用 `BasicWorker` 表示。
- 数据并行（Data Parallel，DP）场景下，同一个基址下挂多个 rank，用 `DPAwareWorker` 表示——它在普通 worker 之上多包了一层 DP 信息。

控制面的「注册表」要能把这些**不同具体类型**的 worker 一视同仁地存进同一个 `HashMap`、交给策略层挑选。这只有用 trait 对象 `Arc<dyn Worker>` 才能做到。所以 `Worker` trait 的第一要务是：**定义一个稳定、统一的接口，让所有 worker 形态都能实现它。**

#### 4.1.2 核心流程

`Worker` trait 有大约 30 个方法，但别被数量吓到——它们可以清晰地归成 7 组。理解了这 7 组，就理解了整个 trait：

| 分组 | 代表方法 | 解决什么问题 |
| --- | --- | --- |
| 身份 | `url()` / `api_key()` / `worker_type()` / `connection_mode()` | 「这个 worker 是谁、怎么连」 |
| 健康 | `is_healthy()` / `set_healthy()` / `check_health_async()` | 「它现在能不能用」 |
| 负载计数 | `load()` / `increment_load()` / `decrement_load()` / `processed_requests()` | 「它现在背了多少请求」 |
| 熔断 | `circuit_breaker()` / `is_available()` / `record_outcome()` | 「连续失败时先别用它」 |
| 模型能力 | `model_id()` / `supports_model()` / `models()` / `tokenizer_path()` | 「它能服务哪些模型」 |
| DP（数据并行） | `is_dp_aware()` / `dp_rank()` / `dp_size()` / `prepare_request()` | 「它是不是数据并行的一份子」 |
| gRPC 客户端 | `get_grpc_client()` / `reset_grpc_client()` | 「gRPC worker 惰性建一条连接」 |

trait 的设计有一个重要特点：**大量方法带默认实现**。也就是说，实现一个新 worker 不用把 30 个方法全写一遍——trait 已经帮你填好了「合理的默认行为」，你只需实现少数几个「没有默认、必须自己提供」的方法（比如 `url()`、`is_healthy()`、`load()`、`circuit_breaker()`、`metadata()` 等）。

> 这种「trait + 大量默认实现」的手法，和 u1-l4 讲过的 `RouterTrait`（数据面）如出一辙：用 trait 统一接口，用默认实现降低实现成本。这是整个网关贯穿始终的设计风格。

#### 4.1.3 源码精读

先看 trait 本身的声明：

[src/core/worker.rs#L141-L150](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker.rs#L141-L150) — `Worker` trait 的定义开头。注意 `#[async_trait]`（因为有 async 方法），以及 `: Send + Sync + fmt::Debug` 这三个约束（要在多线程 + trait 对象里用，必须满足）。

```rust
#[async_trait]
pub trait Worker: Send + Sync + fmt::Debug {
    fn url(&self) -> &str;
    fn api_key(&self) -> &Option<String>;
    /// Get the worker's type (Regular, Prefill, or Decode)
    fn worker_type(&self) -> &WorkerType;
    /// Get the worker's connection mode (HTTP or gRPC)
    fn connection_mode(&self) -> &ConnectionMode;
    ...
```

负载计数是高频操作，所以用原子类型实现，且方法签名都带默认实现：

[src/core/worker.rs#L201-L211](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker.rs#L201-L211) — 负载计数三件套：`load()` 读、`increment_load()` 加、`decrement_load()` 减，外加一个「重置」的默认空实现。

```rust
fn load(&self) -> usize;
fn increment_load(&self);
fn decrement_load(&self);
/// Reset the load counter to 0 (for sync/recovery)
fn reset_load(&self) {}
```

最值得记住的是「可用性」和「记录结果」这两个默认方法——它们把「健康」和「熔断」两条线索拼在一起：

[src/core/worker.rs#L228-L236](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker.rs#L228-L236) — `is_available()` = 健康 **且** 熔断器允许放行；`record_outcome()` 把一次请求的成功/失败转交给熔断器统计。

```rust
/// Check if the worker is available (healthy + circuit closed/half-open)
fn is_available(&self) -> bool {
    self.is_healthy() && self.circuit_breaker().can_execute()
}

/// Record the outcome of a request to this worker
fn record_outcome(&self, success: bool) {
    self.circuit_breaker().record_outcome(success);
}
```

> 这两行是本讲最重要的「集成点」：**熔断器被嵌入到了每个 worker 内部**。一个 worker 是否「可用」，不只看它是否健康，还要看它的熔断器是否还在 `Open`（熔断中）状态。`can_execute()` 的语义是：`Closed`（正常）和 `HalfOpen`（试探中）放行，`Open`（熔断中）拒绝——详见 [src/core/circuit_breaker.rs#L148-L155](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/circuit_breaker.rs#L148-L155)。

模型能力相关的默认方法大多只是「从 `metadata()` 里取东西」，例如取 `model_id`：

[src/core/worker.rs#L273-L286](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker.rs#L273-L286) — `model_id()` 先查 `ModelCard`，找不到再退回 labels，最后退回 `UNKNOWN_MODEL_ID` 常量。

```rust
fn model_id(&self) -> &str {
    self.metadata()
        .models
        .first()
        .map(|m| m.id.as_str())
        .or_else(|| {
            self.metadata().labels.get("model_id").map(|s| s.as_str())
        })
        .unwrap_or(UNKNOWN_MODEL_ID)
}
```

DP（数据并行）相关的方法在 trait 里给了「不是 DP worker」的默认返回值，只有 `DPAwareWorker` 会覆盖它们：

[src/core/worker.rs#L238-L261](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker.rs#L238-L261) — DP 方法的默认实现：`is_dp_aware()` 默认 `false`，`dp_rank()` / `dp_size()` 默认 `None`，`prepare_request()` 默认原样返回请求。

```rust
fn is_dp_aware(&self) -> bool { false }
fn dp_rank(&self) -> Option<usize> { None }
fn dp_size(&self) -> Option<usize> { None }
async fn prepare_request(&self, req: serde_json::Value) -> WorkerResult<serde_json::Value> {
    Ok(req)
}
```

最后，trait 里唯一几个**没有默认实现、必须由具体类型提供**的方法是这些（以分号 `;` 结尾）：

[src/core/worker.rs#L168-L226](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker.rs#L168-L226) — `is_healthy` / `set_healthy` / `check_health_async` / `load` / `increment_load` / `decrement_load` / `metadata` / `circuit_breaker` 等核心方法没有默认体，任何 `Worker` 实现都必须给出它们。

```rust
fn is_healthy(&self) -> bool;
fn set_healthy(&self, healthy: bool);
async fn check_health_async(&self) -> WorkerResult<()>;
...
fn metadata(&self) -> &WorkerMetadata;
fn circuit_breaker(&self) -> &CircuitBreaker;
```

#### 4.1.4 代码实践

这是一个「阅读 + 运行现有测试」型实践，目标是亲手感受 trait 对象的多态。

1. **实践目标**：把三种不同形态的 worker 都装进 `Vec<Box<dyn Worker>>`，验证它们「被当作同一个类型」处理。
2. **操作步骤**：
   - 打开 [src/core/worker.rs](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker.rs)，定位 `test_mixed_worker_types` 这个测试（L1934 起）。
   - 在仓库根目录运行：`cargo test --lib -p smg test_mixed_worker_types`（若 crate 名解析有差异，可用 `cargo test test_mixed_worker_types`）。
3. **需要观察的现象**：测试会把 `Regular`、`Prefill`、`Decode`、以及三个 `DPAwareWorker` 装进同一个 `Vec<Box<dyn Worker>>`，然后统一断言 `is_healthy()`、`is_dp_aware()`、`worker_type()`。
4. **预期结果**：测试通过。前三个普通 worker 的 `is_dp_aware()` 为 `false`，后三个 DP worker 为 `true`，但它们都活在**同一个集合、同一个循环**里——这就是 trait 对象的多态价值。
5. 如果只关心阅读：直接读 L1984-L2012 的循环体即可，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `is_available()` 不直接等于 `is_healthy()`？两者什么关系？

**答案**：`is_available()` = `is_healthy()` **且** `circuit_breaker().can_execute()`。一个 worker 可能「健康检查通过」（`is_healthy == true`），但它最近转发请求连续失败、熔断器进入 `Open`，此时它虽然「活着」但「不该再被选中」。`is_available()` 把健康和熔断两条线索合一，是策略层真正用来筛 worker 的依据。

**练习 2**：trait 里 `prepare_request` 有默认实现（原样返回请求）。`DPAwareWorker` 为什么要覆盖它？

**答案**：因为数据并行时，请求必须带上「该交给哪个 rank」的信息。`DPAwareWorker::prepare_request` 会在请求体里插入 `data_parallel_rank` 字段（见 4.4 节）。普通 worker 不需要这个字段，所以默认原样返回即可——这正是默认实现降低实现成本的体现。

---

### 4.2 WorkerType：Regular / Prefill / Decode 与 PD 信息

#### 4.2.1 概念说明

`WorkerType` 回答一个更细的问题：**这个 worker 在推理流水线里扮演什么角色？** 它只有三个取值：

- `Regular`：普通 worker，能独立完成「接收请求 → 生成响应」全流程。
- `Prefill`：PD 分离部署里的「预填充」worker，专门处理 prompt 阶段。
- `Decode`：PD 分离部署里的「解码」worker，专门做逐 token 生成。

注意 `Prefill` 是一个**带数据的变体**——它带着一个 `bootstrap_port`，用来告诉 decode worker「到哪个端口来取我算好的 KV cache」。这是 PD 分离部署在 worker 抽象上留下的唯一「特殊痕迹」。

#### 4.2.2 核心流程

`WorkerType` 的信息会在构造 worker 时被存进 `WorkerMetadata`，并衍生出两个派生字段：

1. `worker_type` 本身：被存入 `WorkerMetadata.worker_type`，决定这个 worker 在注册表里被分到「普通池 / prefill 池 / decode 池」。
2. `bootstrap_port`：只有 `Prefill` 有值，会被单独「提」出来存进 `WorkerMetadata.bootstrap_port`，方便 PD 路由器快速取用（不必每次都 `match` 一遍 `WorkerType`）。

这套设计的好处是：**类型即配置**。你给一个 worker 标上 `WorkerType::Decode`，注册表和 PD 路由器就自动知道它属于解码池，不需要额外的字符串或标签。

#### 4.2.3 源码精读

[src/core/worker.rs#L513-L525](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker.rs#L513-L525) — `WorkerType` 枚举定义。只有 `Prefill` 带数据（`bootstrap_port`）。

```rust
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum WorkerType {
    /// Regular worker for standard routing
    Regular,
    /// Prefill worker for PD disaggregated mode
    Prefill {
        /// Bootstrap port for communication with decode workers
        bootstrap_port: Option<u16>,
    },
    /// Decode worker for PD disaggregated mode
    Decode,
}
```

它的 `Display` 实现让日志可读：

[src/core/worker.rs#L527-L538](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker.rs#L527-L538) — 把三种类型格式化成字符串，`Prefill` 还会带出 bootstrap 端口。

```rust
impl fmt::Display for WorkerType {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            WorkerType::Regular => write!(f, "Regular"),
            WorkerType::Prefill { bootstrap_port } => match bootstrap_port {
                Some(port) => write!(f, "Prefill(bootstrap:{})", port),
                None => write!(f, "Prefill"),
            },
            WorkerType::Decode => write!(f, "Decode"),
        }
    }
}
```

还有一个把类型转成「指标标签」的方法，供 Prometheus 区分：

[src/core/worker.rs#L540-L549](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker.rs#L540-L549) — `as_metric_label()` 把类型映射成固定的标签常量（`regular` / `prefill` / `decode`），用于指标维度。

```rust
pub fn as_metric_label(&self) -> &'static str {
    match self {
        WorkerType::Regular => metrics_labels::WORKER_REGULAR,
        WorkerType::Prefill { .. } => metrics_labels::WORKER_PREFILL,
        WorkerType::Decode => metrics_labels::WORKER_DECODE,
    }
}
```

> 注意 `as_metric_label` 对 `Prefill` 用了 `{ .. }` 通配——无论 `bootstrap_port` 是 `Some` 还是 `None`，都归到 `prefill` 这一个指标维度。这是刻意的：指标关心「角色」，不关心具体端口。

#### 4.2.4 代码实践

1. **实践目标**：用 builder 构造三种类型的 worker，观察 `worker_type()` 与 `bootstrap_port()` 的取值。
2. **操作步骤**：
   - 阅读 [src/core/worker.rs](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker.rs) 里的 `test_create_prefill_worker`（L1648 起）和 `test_create_decode_worker`（L1681 起）。
   - 运行：`cargo test test_create_prefill_worker test_create_decode_worker`。
3. **需要观察的现象**：`Prefill { bootstrap_port: Some(9090) }` 的 worker，`bootstrap_port()` 返回 `Some(9090)`；而 `Regular` 和 `Decode` 的 worker 返回 `None`。
4. **预期结果**：两个测试通过，印证「只有 `Prefill` 携带 bootstrap 端口」。
5. 这是阅读型实践，运行是可选的验证手段。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Decode` 变体不带任何数据，而 `Prefill` 要带 `bootstrap_port`？

**答案**：在 PD 分离部署里，**prefill worker 是 KV cache 的生产者**，decode worker 是消费者。生产者需要把自己的「bootstrap 服务端口」告诉消费者，消费者才知道去哪里取 cache。所以信息只存在于 prefill 一侧，decode 一侧不需要携带端口（它只是去连别人给的端口）。把数据放在真正需要它的那一侧，是简洁的类型设计。

**练习 2**：`WorkerType` 派生了 `Hash`。想一想注册表或一致性哈希为什么需要它可 `Hash`？

**答案**：控制面经常要「按 worker 类型分组」（普通池 / prefill 池 / decode 池），用 `WorkerType` 作为 `HashMap` 的 key 时就需要 `Hash + Eq`。一致性哈希（`HashRing`）也会用到这些 trait。类型派生 `Hash` 让它能直接当 key 用。

---

### 4.3 WorkerMetadata、HealthConfig 与 ConnectionMode：把配置聚合成元数据

#### 4.3.1 概念说明

`Worker` trait 的很多方法最终都在读一份「元数据」。这份元数据就是 `WorkerMetadata`——它把一个 worker 的所有「静态属性」打包在一起：

- 它的 `url`、`worker_type`、`connection_mode`（HTTP 还是 gRPC）、`runtime_type`（sglang / vllm / external）。
- 一组 `labels`（自由键值对，给策略和指标用）。
- 一份 `health_config`（健康检查怎么查）。
- 一个可选 `api_key`、缓存的 `bootstrap_host` / `bootstrap_port`。
- 一组 `models`（它能服务的模型卡片，本讲 4.5 详讲）。

把所有静态属性集中到一个结构体有两个好处：**一是读起来一目了然，二是 `metadata()` 只需要返回一个引用**（`&WorkerMetadata`），trait 的其它默认方法都能基于它工作，避免了大量重复参数。

`HealthConfig` 是 `WorkerMetadata` 里专门管「健康检查」的一块：查哪个路径、多久查一次、连续失败几次判不健康、连续成功几次恢复健康。`ConnectionMode` 则表示「用什么协议连 worker」——`Http` 或 `Grpc { port }`。

#### 4.3.2 核心流程

- **`ConnectionMode`** 有两个变体：`Http`（默认）和 `Grpc { port: Option<u16> }`。它带一个特别的 `matches()` 方法：当过滤器是 `Grpc { port: None }` 时，相当于「匹配任意 gRPC worker」的通配符——这在注册表按连接模式查询时很有用。
- **`HealthConfig`** 的默认值是「查 `/health`，每 30 秒一次，超时 5 秒，连续 3 次失败判不健康、连续 2 次成功恢复」。
- **`WorkerMetadata`** 有几个查询方法：`find_model(id)` 按 id 或别名找模型卡片；`supports_model(id)` 判断能否服务某模型（**models 为空时表示「接受任意模型」**，这是向后兼容的通配规则）；`supports_endpoint(model, endpoint)` 判断能否服务某端点。

> 「models 为空 = 接受任意模型」这条规则非常关键。它意味着一个新注册、还没做模型发现的 worker 会被视为「什么都能服务」，直到懒加载的模型列表（4.1 节提到的 `set_models`）被填上才会收紧——这个收紧逻辑在 `BasicWorker::supports_model` 里，见下面的源码精读。

#### 4.3.3 源码精读

先看 `ConnectionMode`：

[src/core/worker.rs#L422-L436](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker.rs#L422-L436) — `ConnectionMode` 枚举，`Http` 为默认值，`Grpc` 带可选端口。

```rust
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize, Default)]
#[serde(tag = "type", rename_all = "lowercase")]
pub enum ConnectionMode {
    #[default]
    Http,
    Grpc {
        #[serde(skip_serializing_if = "Option::is_none")]
        #[serde(default)]
        port: Option<u16>,
    },
}
```

[src/core/worker.rs#L442-L449](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker.rs#L442-L449) — `matches()` 的通配逻辑：`Grpc { port: None }` 当作「任意 gRPC」通配。

```rust
pub fn matches(&self, filter: &ConnectionMode) -> bool {
    match (self, filter) {
        (ConnectionMode::Http, ConnectionMode::Http) => true,
        (ConnectionMode::Grpc { .. }, ConnectionMode::Grpc { port: None }) => true,
        (ConnectionMode::Grpc { port: p1 }, ConnectionMode::Grpc { port: p2 }) => p1 == p2,
        _ => false,
    }
}
```

再看 `HealthConfig` 及其默认值：

[src/core/worker.rs#L551-L579](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker.rs#L551-L579) — `HealthConfig` 字段与默认实现（`/health`、30s、5s 超时、3 次失败、2 次成功）。

```rust
pub struct HealthConfig {
    pub timeout_secs: u64,
    pub check_interval_secs: u64,
    pub endpoint: String,
    pub failure_threshold: u32,
    pub success_threshold: u32,
    pub disable_health_check: bool,
}

impl Default for HealthConfig {
    fn default() -> Self {
        Self {
            timeout_secs: 5,
            check_interval_secs: 30,
            endpoint: "/health".to_string(),
            failure_threshold: 3,
            success_threshold: 2,
            disable_health_check: false,
        }
    }
}
```

最后是 `WorkerMetadata` 的关键查询方法：

[src/core/worker.rs#L612-L632](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker.rs#L612-L632) — `find_model` / `supports_model`（空列表 = 通配）/ `supports_endpoint`（找不到模型时退回 `default_model_type`）。

```rust
pub fn find_model(&self, model_id: &str) -> Option<&ModelCard> {
    self.models.iter().find(|m| m.matches(model_id))
}

pub fn supports_model(&self, model_id: &str) -> bool {
    self.models.is_empty() || self.find_model(model_id).is_some()
}

pub fn supports_endpoint(&self, model_id: &str, endpoint: Endpoint) -> bool {
    if let Some(model) = self.find_model(model_id) {
        model.supports_endpoint(endpoint)
    } else {
        self.default_model_type.supports_endpoint(endpoint)
    }
}
```

一个值得留意的细节：`BasicWorker` 会覆盖 `supports_model`，优先看「懒加载的 override」：

[src/core/worker.rs#L834-L844](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker.rs#L834-L844) — `BasicWorker::supports_model` 先查 `models_override`；一旦懒加载填了 override，就**不再**走「空列表 = 通配」的老规则，而是严格匹配已发现的模型。

```rust
fn supports_model(&self, model_id: &str) -> bool {
    if let Ok(guard) = self.models_override.read() {
        if let Some(ref models) = *guard {
            return models.iter().any(|m| m.matches(model_id));
        }
    }
    self.metadata.supports_model(model_id)
}
```

#### 4.3.4 代码实践

1. **实践目标**：验证「空 models = 接受任意模型」这条通配规则。
2. **操作步骤**：
   - 阅读 `test_worker_metadata_empty_models_accepts_all`（[src/core/worker.rs#L2017-L2038](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker.rs#L2017-L2038)）。
   - 运行：`cargo test test_worker_metadata_empty_models_accepts_all`。
3. **需要观察的现象**：一个 `models: Vec::new()` 的 metadata，对 `"any-model"`、`"gpt-4"`、`"llama-3.1"` 都返回 `true`。
4. **预期结果**：测试通过，三条断言全真。
5. 思考题（不运行）：如果之后调用了 `set_models(vec![...])`，同一份 metadata 会不会还接受任意模型？结合上面的 `BasicWorker::supports_model` 得出结论。

#### 4.3.5 小练习与答案

**练习 1**：`ConnectionMode::matches` 里，为什么要把 `Grpc { port: None }` 当通配符？

**答案**：注册表按连接模式查询 worker 时，调用方经常只关心「是不是 gRPC」，不关心具体端口。用 `Grpc { port: None }` 作为「任意 gRPC」的过滤器，就能一次性匹配所有 gRPC worker；若需要精确到端口，再传 `Grpc { port: Some(x) }`。一个方法覆盖两种语义。

**练习 2**：`WorkerMetadata` 的字段大多是普通值类型，但 `BasicWorker` 里维护了一个 `models_override: Arc<RwLock<Option<Vec<ModelCard>>>>`。为什么不直接改 `metadata.models`？

**答案**：因为 `metadata()` 返回的是 `&WorkerMetadata`（不可变借用），而模型发现在运行期异步发生、需要可变。用一把独立的 `RwLock` 把「可变的 override」和「不可变的 metadata」分开，既保留了 `metadata()` 返回稳定引用的便利，又能安全地做懒加载。找不到 override 时再退回 metadata 的默认规则。

---

### 4.4 BasicWorkerBuilder 与 DPAwareWorkerBuilder：流式构建 Worker

#### 4.4.1 概念说明

`BasicWorker` 内部字段很多（元数据 + 一堆原子计数器 + 熔断器 + gRPC 客户端占位 + models override 锁），直接用结构体字面量构造既冗长又容易漏字段。`BasicWorkerBuilder` 用**建造者模式**解决这个问题：

- `BasicWorkerBuilder::new(url)` 创建一个带「合理默认值」的 builder。
- 一串链式 setter（`.worker_type(...)`、`.label(...)`、`.health_config(...)`、`.circuit_breaker_config(...)`、`.model(...)` 等）按需覆盖默认值。
- `.build()` 收口：把字段打包成 `WorkerMetadata`，创建所有原子计数器，实例化一个带该 worker URL 标签的熔断器，产出 `BasicWorker`。

> 「builder 持有默认值、setter 只覆盖关心的字段、`build()` 统一收口」——这和 u2-l1 讲过的 `RouterConfig::builder` 是同一套套路。整个网关在「复杂对象构造」上一致地使用 builder 模式。

`DPAwareWorkerBuilder` 是 `BasicWorkerBuilder` 的「数据并行特化版」：它额外要 `dp_rank` 和 `dp_size`，`build()` 时先把 URL 改写成 `base@rank` 的形式，复用 `BasicWorkerBuilder` 造出一个 `BasicWorker`，再用 `DPAwareWorker::with_base_worker` 把它包成 `DPAwareWorker`。

#### 4.4.2 核心流程

`BasicWorkerBuilder::build()` 内部做这几件事（顺序很重要）：

1. **解析 bootstrap host**：从 URL 里解析出主机名（顺手剥掉 `@rank` 后缀），缓存进 `WorkerMetadata.bootstrap_host`，避免每次 PD 路由都重新解析。
2. **提取 bootstrap port**：若 `worker_type` 是 `Prefill { bootstrap_port }`，把它单独存进 `WorkerMetadata.bootstrap_port`。
3. **组装 `WorkerMetadata`**：填入所有字段，`default_provider` 默认 `None`（原生/透传），`default_model_type` 默认 `ModelType::LLM`。
4. **创建原子计数器**：`load_counter`、`processed_counter`、`healthy`、`consecutive_failures/successes` 全部初始为 0 / true。
5. **实例化熔断器**：`CircuitBreaker::with_config_and_label(config, url)`——**熔断器的指标标签就是 worker 的 URL**，所以每个 worker 有自己独立的熔断器和独立的指标维度。
6. **准备 gRPC 客户端占位**：用 `OnceCell` 包起来，若 builder 传了 client 就预填，否则留空等运行期惰性建连。
7. **打初始健康指标**：`Metrics::set_worker_health(url, true)`。

`DPAwareWorkerBuilder::build()` 则更简洁：先改写 URL，再用一个 `BasicWorkerBuilder` 把公共字段全转过去，最后包一层 `DPAwareWorker`。

#### 4.4.3 源码精读

先看 `BasicWorkerBuilder` 的字段和构造：

[src/core/worker_builder.rs#L14-L43](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_builder.rs#L14-L43) — builder 的字段与 `new()`：每个字段都给了一个默认值（`Regular` / `Http` / 默认 health 与熔断配置）。

```rust
pub struct BasicWorkerBuilder {
    url: String,
    api_key: Option<String>,
    worker_type: WorkerType,
    connection_mode: ConnectionMode,
    runtime_type: RuntimeType,
    labels: HashMap<String, String>,
    models: Vec<ModelCard>,
    health_config: HealthConfig,
    circuit_breaker_config: CircuitBreakerConfig,
    grpc_client: Option<GrpcClient>,
}

impl BasicWorkerBuilder {
    pub fn new(url: impl Into<String>) -> Self {
        Self {
            url: url.into(),
            worker_type: WorkerType::Regular,
            connection_mode: ConnectionMode::Http,
            health_config: HealthConfig::default(),
            circuit_breaker_config: CircuitBreakerConfig::default(),
            ... // 其余默认值
        }
    }
}
```

核心在 `build()` 的收口逻辑：

[src/core/worker_builder.rs#L128-L156](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_builder.rs#L128-L156) — `build()` 第一步：解析 bootstrap host、提取 bootstrap port、组装 `WorkerMetadata`。

```rust
pub fn build(self) -> BasicWorker {
    let bootstrap_host = parse_bootstrap_host_from_url(&self.url);
    let bootstrap_port = match self.worker_type {
        WorkerType::Prefill { bootstrap_port } => bootstrap_port,
        _ => None,
    };
    let metadata = WorkerMetadata {
        url: self.url.clone(),
        api_key: self.api_key,
        worker_type: self.worker_type,
        connection_mode: self.connection_mode,
        runtime_type: self.runtime_type,
        labels: self.labels,
        health_config: self.health_config,
        bootstrap_host,
        bootstrap_port,
        models: self.models,                // Empty = accepts any model
        default_provider: None,             // Native/passthrough
        default_model_type: ModelType::LLM, // Standard LLM capabilities
    };
    ...
```

接着创建所有运行期状态（原子计数器 + 熔断器 + gRPC 占位）：

[src/core/worker_builder.rs#L169-L187](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_builder.rs#L169-L187) — `build()` 第二步：所有计数器初始为 0 / true，熔断器以 worker URL 为标签实例化。

```rust
let healthy = true;
Metrics::set_worker_health(&self.url, healthy);

BasicWorker {
    metadata,
    load_counter: Arc::new(AtomicUsize::new(0)),
    worker_routing_key_load: Arc::new(WorkerRoutingKeyLoad::new(&self.url)),
    processed_counter: Arc::new(AtomicUsize::new(0)),
    healthy: Arc::new(AtomicBool::new(healthy)),
    consecutive_failures: Arc::new(AtomicUsize::new(0)),
    consecutive_successes: Arc::new(AtomicUsize::new(0)),
    circuit_breaker: CircuitBreaker::with_config_and_label(
        self.circuit_breaker_config,
        self.url.clone(),
    ),
    grpc_client,
    models_override: Arc::new(StdRwLock::new(None)),
}
```

再看 `DPAwareWorkerBuilder::build()` 如何复用 `BasicWorkerBuilder`：

[src/core/worker_builder.rs#L314-L335](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_builder.rs#L314-L335) — DP builder：URL 改写成 `base@rank`，复用 `BasicWorkerBuilder` 造出 base worker，再包成 `DPAwareWorker`。

```rust
pub fn build(self) -> DPAwareWorker {
    let worker_url = format!("{}@{}", self.base_url, self.dp_rank);
    let mut builder = BasicWorkerBuilder::new(worker_url)
        .models(self.models)
        .worker_type(self.worker_type)
        .connection_mode(self.connection_mode)
        ...;
    // 可选地补上 grpc_client / api_key
    let base_worker = builder.build();
    DPAwareWorker::with_base_worker(base_worker, self.base_url, self.dp_rank, self.dp_size)
}
```

> 这里的设计精髓是**复用**：`DPAwareWorkerBuilder` 不重新实现一遍造 worker 的逻辑，而是把公共字段转交给 `BasicWorkerBuilder`，自己只负责「URL 改写 + DP 包装」。这样两条构造路径共享同一套收口逻辑，未来 `build()` 改了，两者同步生效。

#### 4.4.4 代码实践

这是本讲的核心实践之一，目标是用 builder 造一个带熔断器的 Regular worker，并观察 `build()` 产出的初始状态。

1. **实践目标**：用 `BasicWorkerBuilder` 构造一个自定义熔断阈值的 Regular worker，断言它刚造出来时「可用、负载为 0、熔断器 Closed」。
2. **操作步骤**：
   - 阅读 `test_basic_worker_builder_full`（[src/core/worker_builder.rs#L367-L431](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_builder.rs#L367-L431)）和 `test_worker_with_circuit_breaker_config`（[src/core/worker.rs#L1889-L1916](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker.rs#L1889-L1916)），它们演示了完整 builder 链与熔断行为。
   - 运行：`cargo test test_worker_with_circuit_breaker_config`。
3. **需要观察的现象**：配置 `failure_threshold: 2` 后，连续两次 `record_outcome(false)` 就让 `is_available()` 变 `false`；等 `timeout_duration` 过去后，状态自动进入 `HalfOpen`，再一次成功即回到 `Closed`。
4. **预期结果**：测试通过。这印证 `build()` 产出的熔断器确实「按你给的配置」工作，且和健康状态联动决定 `is_available()`。
5. 如果你愿意自己写一个最小断言，可参考下面的「示例代码」（**非项目原有代码**），把它放进你自己的测试模块：

```rust
// 示例代码：演示 build() 产出的初始状态（非项目原有）
use std::time::Duration;
use smg::core::{
    BasicWorkerBuilder, CircuitBreakerConfig, Worker, WorkerType,
    circuit_breaker::CircuitState,
};

let cb = CircuitBreakerConfig {
    failure_threshold: 3,
    success_threshold: 2,
    timeout_duration: Duration::from_millis(200),
    window_duration: Duration::from_secs(60),
};
let worker = BasicWorkerBuilder::new("http://test:8080")
    .worker_type(WorkerType::Regular)
    .circuit_breaker_config(cb)
    .build();

assert!(worker.is_healthy());                       // 初始健康
assert_eq!(worker.load(), 0);                       // 初始负载 0
assert_eq!(worker.circuit_breaker().state(), CircuitState::Closed); // 熔断器初始 Closed
assert!(worker.is_available());                     // 健康 + Closed => 可用
```

6. 该示例若直接放入仓库需引入正确的 `use` 路径与 `#[cfg(test)]` 模块；具体能否编译通过**待本地验证**（取决于你把它放在哪个 crate / 模块）。

#### 4.4.5 小练习与答案

**练习 1**：`build()` 里为什么用 `Metrics::set_worker_health(&self.url, true)` 而不是用一个数字常量当健康指标？

**答案**：因为指标的维度（label）是 **worker 的 URL**。每个 worker 在 Prometheus 里都有一条独立的 `smg_worker_health` 指标，靠 URL 区分。在构造期就打一次 `true`，保证「worker 一注册就有指标」，不会出现「指标时有时无」的缺口。

**练习 2**：`DPAwareWorkerBuilder::build()` 把 URL 改写成 `base@rank`。这个 `@rank` 后缀在哪些地方会被「剥掉」？

**答案**：至少两处会剥掉它——`parse_bootstrap_host_from_url`（[src/core/worker.rs#L42-L70](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker.rs#L42-L70)）解析主机名时会先去掉 `@rank`；`BasicWorker::normalised_url`（[src/core/worker.rs#L680-L698](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker.rs#L680-L698)）做健康检查时会还原成真实基址。`@rank` 只是个「让注册表区分不同 rank」的虚拟后缀，真正发请求时要还原成 `base_url`。

---

### 4.5 ModelCard：模型能力卡片

#### 4.5.1 概念说明

一个 worker 能服务一个或多个模型。每个模型有自己的一堆属性：叫什么名字（主 id）、有哪些别名、支持哪些端点（chat / embeddings / rerank…）、上下文多长、tokenizer 在哪、用什么 chat template、用哪种 reasoning parser / tool parser、是不是分类模型……这些属性如果全塞进 `WorkerMetadata.labels` 这个自由 `HashMap<String, String>`，既没有类型保证也很难查询。

`ModelCard` 就是为了把这些**模型相关属性**从 labels 里「拎出来」、聚合成一个有类型的结构体。源码注释里写得很直白：「Consolidates fields previously scattered in `WorkerMetadata.labels`」（整合了过去散落在 labels 里的字段）。它是网关里「一个模型」的正式表达。

和 `ModelCard` 配套的有两个基础类型（在 `model_type.rs` 里）：

- `ModelType`：用 **bitflags**（位标志）表示「支持哪些端点」。比如 `ModelType::LLM` = chat | completions | responses | tools；`ModelType::VISION_LLM` = LLM | vision。用位运算可以组合和判断。
- `ProviderType`：表示「这个模型走哪个外部厂商的 API 格式」（OpenAI / xAI / Anthropic / Gemini / 自定义）。`None` 表示「原生/透传」（本地 SGLang 后端）。

#### 4.5.2 核心流程

`ModelCard` 的典型用法是「builder 式构造」：`ModelCard::new(id)` 创建一张默认卡片（`model_type = LLM`、无 provider），再用 `.with_alias(...)`、`.with_model_type(...)`、`.with_tokenizer_path(...)`、`.with_reasoning_parser(...)` 等方法按需补全。构造好后：

- 把若干张卡片传给 `BasicWorkerBuilder::models(vec![...])` 或 `.model(card)`，存进 `WorkerMetadata.models`。
- 运行期，`Worker` trait 的 `model_id()`、`supports_model()`、`tokenizer_path(model_id)` 等方法会通过 `metadata.find_model(model_id)` 找到对应卡片并返回属性。
- 匹配时**同时认主 id 和别名**：`matches(id)` 检查 `self.id == id` 或别名列表里有没有。

`ModelType` 的位运算是核心能力判断的底层：

\[ \text{supports\_chat}(m) \iff (m \,\&\, \text{CHAT}) \neq 0 \]

即「某模型支持某端点」等价于「它的能力位与该端点的位相与不为零」。预定义的 `LLM`、`VISION_LLM`、`REASONING_LLM` 等只是常用组合的快捷写法。

#### 4.5.3 源码精读

先看 `ModelCard` 的结构（字段分了「身份 / 能力 / 分词与解析 / 分类」几组）：

[src/core/model_card.rs#L111-L184](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/model_card.rs#L111-L184) — `ModelCard` 结构体，注释里标注了每个字段「过去对应 labels 里的哪个 key」。

```rust
pub struct ModelCard {
    pub id: String,                          // 原 labels["model_id"]
    pub display_name: Option<String>,
    pub aliases: Vec<String>,
    pub model_type: ModelType,               // 能力位标志，默认 LLM
    pub provider: Option<ProviderType>,      // None = 原生/透传
    pub context_length: Option<u32>,
    pub tokenizer_path: Option<String>,      // 原 labels["tokenizer_path"]
    pub chat_template: Option<String>,       // 原 labels["chat_template"]
    pub reasoning_parser: Option<String>,    // 原 labels["reasoning_parser"]
    pub tool_parser: Option<String>,         // 原 labels["tool_parser"]
    pub id2label: HashMap<u32, String>,      // 分类模型用
    pub num_labels: u32,
    // ... 还有 hf_model_type / architectures / metadata 等
}
```

构造方法与匹配方法：

[src/core/model_card.rs#L194-L216](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/model_card.rs#L194-L216) — `ModelCard::new(id)` 给出默认值（`model_type = LLM`、无 provider）。

```rust
pub fn new(id: impl Into<String>) -> Self {
    Self {
        id: id.into(),
        aliases: Vec::new(),
        model_type: ModelType::LLM,
        provider: None,
        ... // 其余 None / 空
    }
}
```

[src/core/model_card.rs#L313-L321](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/model_card.rs#L313-L321) — `matches` 同时认主 id 与别名；`supports_endpoint` 委托给 `model_type`。

```rust
pub fn matches(&self, model_id: &str) -> bool {
    self.id == model_id || self.aliases.iter().any(|a| a == model_id)
}

pub fn supports_endpoint(&self, endpoint: Endpoint) -> bool {
    self.model_type.supports_endpoint(endpoint)
}
```

再看 `ModelType` 的 bitflags 定义（节选），看清楚「组合」是怎么用位或拼出来的：

[src/core/model_type.rs#L12-L52](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/model_type.rs#L12-L52) — `ModelType` 位标志：每个端点是一个 bit，`LLM` 等是若干 bit 的或运算组合。

```rust
bitflags! {
    pub struct ModelType: u16 {
        const CHAT        = 1 << 0;
        const COMPLETIONS = 1 << 1;
        const RESPONSES   = 1 << 2;
        const EMBEDDINGS  = 1 << 3;
        const VISION      = 1 << 6;
        const TOOLS       = 1 << 7;
        const REASONING   = 1 << 8;
        // ... AUDIO / IMAGE_GEN / MODERATION 等

        /// Standard LLM: chat + completions + responses + tools
        const LLM = Self::CHAT.bits() | Self::COMPLETIONS.bits()
                  | Self::RESPONSES.bits() | Self::TOOLS.bits();

        /// Vision-capable LLM: LLM + vision
        const VISION_LLM = Self::LLM.bits() | Self::VISION.bits();
        // ... REASONING_LLM / FULL_LLM 等
    }
}
```

> 为什么用 bitflags 而不是一个 `Vec<Endpoint>`？因为「能力判断」是高频操作（每个请求都要判），用一个 `u16` 的位与运算比遍历数组快得多，且占用内存极小。`ModelCard` 里很多 `supports_*` 方法最终都是一行 `self.contains(Self::CHAT)`（见 [src/core/model_type.rs#L88-L158](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/model_type.rs#L88-L158)）。

最后看一个完整用法——这是源码自带的文档示例：

[src/core/model_card.rs#L96-L110](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/model_card.rs#L96-L110) — `ModelCard` 的文档级用法示例：链式构造、按别名匹配、能力判断。

```rust
let card = ModelCard::new("meta-llama/Llama-3.1-8B-Instruct")
    .with_display_name("Llama 3.1 8B Instruct")
    .with_alias("llama-3.1-8b")
    .with_model_type(ModelType::VISION_LLM)
    .with_context_length(128_000)
    .with_tokenizer_path("meta-llama/Llama-3.1-8B-Instruct");

assert!(card.matches("llama-3.1-8b"));      // 别名也能匹配
assert!(card.model_type.supports_vision()); // VISION_LLM 带视觉能力
assert!(card.provider.is_none());           // 本地模型，无外部 provider
```

#### 4.5.4 代码实践

1. **实践目标**：用 `ModelCard` 给 worker 配一个带别名的模型，验证按别名也能匹配上。
2. **操作步骤**：
   - 阅读 `test_worker_metadata_find_model`（[src/core/worker.rs#L2040-L2074](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker.rs#L2040-L2074)），看它如何构造带别名的 `ModelCard` 并按主 id / 别名查找。
   - 运行：`cargo test test_worker_metadata_find_model`。
3. **需要观察的现象**：`find_model("meta-llama/Llama-3.1-8B")`（主 id）、`find_model("llama-3.1-8b")`（别名）、`find_model("llama3.1")`（别名）都返回 `Some`，而 `find_model("unknown-model")` 返回 `None`。
4. **预期结果**：测试通过，印证「主 id + 多别名」都能命中同一张卡片。
5. 这是阅读型实践，运行可选。

#### 4.5.5 小练习与答案

**练习 1**：`ModelCard::new("gpt-4o").with_provider(ProviderType::OpenAI)` 与不设 provider 的卡片，在路由上有什么行为差异？

**答案**：设了 `provider = OpenAI` 表示这个模型走 OpenAI 厂商的 API 格式，路由时需要做「厂商专用的请求/响应转换」（剥掉 SGLang 专有字段等）；不设 provider（`None`）表示「原生/透传」，请求原样转发给本地后端，不做转换。`Worker::provider_for_model(model_id)` 会优先取卡片上的 provider，卡片没设再退回 worker 的 `default_provider`（见 [src/core/worker.rs#L342-L344](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker.rs#L342-L344) 与 [src/core/worker.rs#L636-L640](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker.rs#L636-L640)）。

**练习 2**：为什么 `ModelType` 用 `u16` 位标志，而不是给 `ModelCard` 一个 `Vec<Endpoint>`？

**答案**：能力判断是每个请求都要做的高频操作。用 `u16` 位与运算判断（`self & CHAT != 0`）是 O(1) 且无内存分配的；用 `Vec` 则要遍历、且每张卡片都要在堆上分配。对一个可能同时管理成百上千模型、每秒处理上万请求的网关，这个差异值得用 bitflags。

---

## 5. 综合实践

现在把本讲的四个核心模块（`Worker` trait、`WorkerType`、`BasicWorkerBuilder`、`ModelCard`）串起来，完成下面这个**编写单元测试**的任务（对应规格里的实践任务：用 builder 构造带熔断器的 Regular worker，调用 load guard 与 is_healthy，编写测试断言状态）。

**任务**：写一个测试，构造一个「带自定义熔断器 + 一张带别名的 ModelCard」的 Regular worker，验证下面三件事：

1. 刚构造完：`is_healthy() == true`、`load() == 0`、熔断器 `Closed`、`is_available() == true`、按别名能匹配到模型。
2. 用 `WorkerLoadGuard` 模拟一次「请求在途」：guard 存在期间 `load() == 1`、routing-key 计数为 1；guard 离开作用域后自动回到 0。
3. 连续记录失败直到熔断：`record_outcome(false)` 若干次后 `is_available()` 变 `false`（因为熔断器 Open），但 `is_healthy()` 仍是 `true`（健康和熔断是两回事）。

**操作步骤**：

1. 在 `src/core/worker.rs` 已有的 `#[cfg(test)] mod tests` 里（或你自己的测试模块），新增一个测试函数。下面是**示例代码**（非项目原有代码），综合参考了已有的 `test_worker_with_circuit_breaker_config`、`test_worker_load_guard_with_routing_key` 和 `test_worker_metadata_find_model` 三个真实测试：

```rust
// 示例代码：综合实践（非项目原有代码）
use std::{collections::HashMap, sync::Arc, time::Duration};

use crate::core::{
    circuit_breaker::{CircuitBreakerConfig, CircuitState},
    model_card::ModelCard,
    model_type::ModelType,
    BasicWorkerBuilder, Worker, WorkerLoadGuard, WorkerType,
};

#[test]
fn practice_regular_worker_full_lifecycle() {
    // 1. 一张带别名的模型卡片
    let card = ModelCard::new("meta-llama/Llama-3.1-8B")
        .with_alias("llama-3.1-8b")
        .with_model_type(ModelType::LLM);

    // 2. 自定义熔断配置：失败 2 次即熔断
    let cb = CircuitBreakerConfig {
        failure_threshold: 2,
        success_threshold: 1,
        timeout_duration: Duration::from_millis(100),
        window_duration: Duration::from_secs(60),
    };

    let worker = BasicWorkerBuilder::new("http://test:8080")
        .worker_type(WorkerType::Regular)
        .model(card)
        .circuit_breaker_config(cb)
        .build();

    // (1) 初始状态断言
    assert!(worker.is_healthy());
    assert_eq!(worker.load(), 0);
    assert_eq!(worker.circuit_breaker().state(), CircuitState::Closed);
    assert!(worker.is_available());
    assert!(worker.supports_model("llama-3.1-8b")); // 按别名匹配

    // (2) RAII load guard：在作用域内负载 +1，离开后自动 -1
    let worker: Arc<dyn Worker> = Arc::new(worker);
    let mut headers = http::HeaderMap::new();
    headers.insert("x-smg-routing-key", "sess-42".parse().unwrap());
    {
        let _guard = WorkerLoadGuard::new(worker.clone(), Some(&headers));
        assert_eq!(worker.load(), 1);
        assert_eq!(worker.worker_routing_key_load().value(), 1);
    }
    assert_eq!(worker.load(), 0);
    assert_eq!(worker.worker_routing_key_load().value(), 0);

    // (3) 连续失败触发熔断：健康依旧 true，但 is_available 变 false
    worker.record_outcome(false);
    assert!(worker.is_available()); // 失败 1 次，还未到阈值
    worker.record_outcome(false);
    assert!(!worker.is_available()); // 失败 2 次，熔断器 Open
    assert!(worker.is_healthy());    // 健康状态不受熔断影响
    assert_eq!(worker.circuit_breaker().state(), CircuitState::Open);
}
```

2. 运行你新增的测试：`cargo test practice_regular_worker_full_lifecycle`。

**需要观察的现象 / 检查清单**：

- [ ] 第 (1) 段断言全过：builder 产出的 worker 初始即「健康 + 负载 0 + 熔断 Closed + 可用 + 别名可匹配」。
- [ ] 第 (2) 段：`WorkerLoadGuard` 创建后 `load()` 立刻为 1、routing-key 计数为 1；离开 `{ }` 作用域后两者都回到 0——**没有显式调用 `decrement_load`，是 `Drop` 自动做的**。
- [ ] 第 (3) 段：两次失败后 `is_available()` 变 `false`，但 `is_healthy()` 仍是 `true`——这证明「健康」和「熔断」是两条独立线索，`is_available` 才是它们的合取。

**预期结果**：测试通过。通过这一个测试，你同时验证了：builder 收口逻辑（`build()` 造出的初始状态）、RAII 负载守卫（`WorkerLoadGuard` + `Drop`）、熔断器嵌入 worker（`record_outcome` → `is_available`）、以及 `ModelCard` 的别名匹配。

> 若编译报错，最可能的原因是 `use` 路径或测试模块位置。上述 `use crate::core::{...}` 假设测试写在 `smg` crate 内部；如果你在别处写，需要相应调整。具体能否一次编译通过**待本地验证**。你也可以不新增测试，直接运行本讲引用的三个真实测试（`test_worker_with_circuit_breaker_config`、`test_worker_load_guard_with_routing_key`、`test_worker_metadata_find_model`）来分别验证上述三件事。

## 6. 本讲小结

- **`Worker` trait** 是「一个后端推理进程」的统一抽象，约 30 个方法可分为「身份 / 健康 / 负载计数 / 熔断 / 模型能力 / DP / gRPC」七组；大量方法带默认实现，实现者只需提供少数核心方法（`url` / `is_healthy` / `load` / `metadata` / `circuit_breaker` 等）。
- 一个 worker 是否「可用」由 `is_available()` 决定，它是 `is_healthy()` **与** `circuit_breaker().can_execute()` 的合取——**熔断器被嵌入到了每个 worker 内部**，且以 worker URL 作为指标标签。
- **`WorkerType`** 有三值：`Regular` / `Prefill { bootstrap_port }` / `Decode`；只有 `Prefill` 携带 bootstrap 端口，这是 PD 分离部署在 worker 抽象上留下的唯一特殊痕迹。
- **`WorkerMetadata`** 把所有静态属性（含 `HealthConfig`、`ConnectionMode`、`models`）打包，`Worker` trait 的默认方法都基于 `metadata()` 工作；「models 为空 = 接受任意模型」是默认通配规则，但 `BasicWorker` 在懒加载 `models_override` 后会收紧为严格匹配。
- **`BasicWorkerBuilder`** 用链式 API + `build()` 收口构造 `BasicWorker`：组装 `WorkerMetadata`、创建原子计数器、实例化带 URL 标签的熔断器；**`DPAwareWorkerBuilder`** 复用同一套收口逻辑，只额外做 URL 改写与 DP 包装。
- **`ModelCard`** 把过去散落在 labels 里的模型字段聚合成有类型的「能力卡片」，配套的 `ModelType`（bitflags）让高频的端点能力判断退化为一次位与运算。

## 7. 下一步学习建议

本讲建立的是「单个 worker」的抽象。控制面真正要做的是「管理一群 worker」。建议按以下顺序继续：

1. **u3-l2（WorkerRegistry 与 HashRing）**：本讲造出的 worker 会被存进 `WorkerRegistry`，并支持按 model / type / connection 查询，以及一致性哈希环选路。这是「单个 worker」到「一群 worker」的第一步。
2. **u3-l3（WorkerManager 与 LoadMonitor）**：本讲的 `load()` 计数会被 `LoadMonitor` 周期采样，驱动 `power_of_two` 等负载均衡策略。
3. **u3-l7（健康检查与启动探活）**：本讲只讲了 `HealthConfig` 的字段；u3-l7 会讲 `start_health_checker` 如何周期性调用 `check_health_async`，并把结果回写 `set_healthy` 与熔断器。
4. **u6-l2（熔断器 CircuitBreaker）**：本讲把熔断器当作「嵌入 worker 的黑盒」用了；u6-l2 会拆开这个黑盒，讲清 `Closed` / `Open` / `HalfOpen` 三态机的转换条件。

如果你想在读 u3-l2 之前热身，可以先打开 [src/core/worker_registry.rs](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_registry.rs)，扫一眼它的 `pub fn` 清单，猜猜它会怎么用本讲的 `Arc<dyn Worker>` 和 `WorkerType`。
