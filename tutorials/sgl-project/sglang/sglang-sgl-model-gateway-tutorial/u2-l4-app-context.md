# AppContext 共享状态

## 1. 本讲目标

本讲聚焦 `src/app_context.rs` 这一个文件，回答一个问题：

> 网关启动后，遍布各处的路由器、处理器、后台任务，是怎样拿到同一份「注册表 / 策略表 / 存储后端 / 作业队列」的？

读完本讲，你应当能够：

- 说清 `AppContext` 是什么、它聚合了哪 22 个字段、为什么它 `Clone` 起来几乎免费。
- 区分「启动即就绪」的 `Arc` 字段、「按配置可选」的 `Option<Arc>` 字段，以及「靠 `OnceLock` 延迟初始化」的字段。
- 解释三个 `OnceLock` 字段存在的根本原因——破解 `JobQueue` 与 `AppContext` 之间的循环依赖。
- 看懂 `AppContextBuilder` 的链式装配，以及 `from_config` 如何用 14 个 `with_*` 步骤把一个配置对象变成完整的运行时状态。

本讲承接 [u2-l3](u2-l3-server-startup.md)：u2-l3 讲了 `server::startup` 这条装配流水线的宏观顺序，本讲把其中最关键的一个环节——`AppContext` 的构造——单独放大讲透。

## 2. 前置知识

阅读本讲前，建议你已经了解（来自前序讲义）：

- **网关的四区域架构**：控制面、数据面、可靠性、可观测性（u1-l1）。
- **`RouterConfig`**：网关的「参数总表」，由 CLI 翻译而来（u2-l1、u2-l2）。
- **`server::startup` 的初始化顺序**：日志 → 指标 → Mesh → `AppContext` → OnceLock 填值 → RouterManager → 服务发现（u2-l3）。

此外需要一点 Rust 基础：

- **`Arc<T>`**：线程安全的引用计数指针。`Arc::clone(&x)` 只是增加一个计数，不复制内部数据，所以「克隆 `Arc`」很廉价。多个所有者共享同一份数据时就用它。
- **`Weak<T>`**：`Arc` 的「弱引用」，不增加强引用计数，不会阻止被指向的对象被释放，常用来打破循环引用（A 持有 B、B 又想引用 A 时，其中一侧用 `Weak`）。
- **`OnceLock<T>`**：标准库提供的一次性写入容器。创建时为空，生命周期内最多 `set` 一次，之后可以反复 `get`。适合「先占位、晚一点才有值」的字段。
- **Builder 模式**：用一个中间对象（Builder）逐步收集字段，最后 `build()` 一次性构造目标对象，常配合 `Option<T>` 字段做「必填校验」。

如果你对这些概念还不熟，本讲会在用到时再次点明。

## 3. 本讲源码地图

本讲只涉及一个源码文件，外加 `server.rs` 中消费它的一小段：

| 文件 | 作用 |
| --- | --- |
| [src/app_context.rs](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs) | 定义 `AppContext`、`AppContextBuilder`、`AppContextBuildError`，以及 `from_config` 装配链 |
| [src/server.rs](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs) | `startup` 中调用 `AppContext::from_config`，并在其后填充三个 `OnceLock` |

`AppContext` 是横跨控制面、数据面、可观测性的「共享底座」，它本身不实现业务逻辑，只负责把各层组件捏在一起，供全程序克隆共享。文件作者在注释里直白地说明了它的来历：它把原本散落在 `server.rs` 里约 194 行的初始化代码集中到了一处（见下文 [src/app_context.rs:97-98](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L97-L98) 与 [src/app_context.rs:287-288](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L287-L288)）。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **4.1 AppContext：聚合 22 个共享字段的「神经中枢」**——字段构成与克隆语义。
2. **4.2 OnceLock 惰性字段与循环依赖破解**——三个延迟字段为什么必须延迟。
3. **4.3 AppContextBuilder：链式装配器与 build() 校验**——构造器与必填校验。
4. **4.4 from_config：从配置一键装配全部组件**——14 步装配链的顺序与依赖。

### 4.1 AppContext：聚合 22 个共享字段的「神经中枢」

#### 4.1.1 概念说明

设想一个网关进程里有几十个地方都需要同一批东西：所有路由器都要查 `WorkerRegistry` 选 worker；所有策略都要读写 `PolicyRegistry`；OpenAI 路由器要访问 `ResponseStorage`；后台任务要把作业塞进 `JobQueue`……如果让每个使用者各自持有这些对象，要么要在函数间层层传参，要么要造一堆全局变量。

`AppContext` 的做法是：**把这些「需要被全程序共享」的组件打包进一个结构体**，再把整个结构体放进一个 `Arc`，到处克隆这个 `Arc`。于是任意一处拿到 `AppContext`，就等于同时拿到了全部共享组件。你可以把它理解成网关运行时的「神经中枢」或「共享背包」。

关键性质有三条：

- **克隆廉价**：`AppContext` 上标了 `#[derive(Clone)]`，克隆它只是给内部每个 `Arc` 的计数加一，不复制任何实际数据。
- **单一来源**：组件在 `AppContext` 里只构造一份，所有使用者通过克隆共享同一份实例，状态天然一致。
- **横切关注点集中**：可观测性（`inflight_tracker`）、限流（`rate_limiter`）、存储（三个 storage）等横切组件也被收纳进来，路由器无需各自构造。

#### 4.1.2 核心流程

`AppContext` 在生命周期里大致经历三个阶段：

```text
阶段 A：from_config 装配        阶段 B：server::startup 填充 OnceLock    阶段 C：运行期到处克隆
┌─────────────────────┐        ┌──────────────────────────┐          ┌──────────────────────┐
│ 大部分字段就绪        │   →    │ worker_job_queue.set(..)  │   →      │ handler.clone_ctx()  │
│ 3 个 OnceLock 创建   │        │ workflow_engines.set(..)  │          │ router 持有 Arc<AppContext>│
│ (mcp_manager 已填)   │        │ 提交启动作业              │          │ 后台任务 clone        │
└─────────────────────┘        └──────────────────────────┘          └──────────────────────┘
```

克隆发生在最后阶段：每个进来的 HTTP 请求处理器、每个路由器、每个后台轮询任务，都会克隆一份 `AppContext`（其实是克隆外层 `Arc`），从而零成本地访问全部共享状态。

#### 4.1.3 源码精读

结构体定义在 [src/app_context.rs:39-62](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L39-L62)：

```rust
#[derive(Clone)]
pub struct AppContext {
    pub client: Client,
    pub router_config: RouterConfig,
    pub rate_limiter: Option<Arc<TokenBucket>>,
    pub tokenizer_registry: Arc<TokenizerRegistry>,
    pub reasoning_parser_factory: Option<ReasoningParserFactory>,
    pub tool_parser_factory: Option<ToolParserFactory>,
    pub worker_registry: Arc<WorkerRegistry>,
    pub policy_registry: Arc<PolicyRegistry>,
    pub router_manager: Option<Arc<RouterManager>>,
    pub response_storage: Arc<dyn ResponseStorage>,
    pub conversation_storage: Arc<dyn ConversationStorage>,
    pub conversation_item_storage: Arc<dyn ConversationItemStorage>,
    pub load_monitor: Option<Arc<LoadMonitor>>,
    pub configured_reasoning_parser: Option<String>,
    pub configured_tool_parser: Option<String>,
    pub worker_job_queue: Arc<OnceLock<Arc<JobQueue>>>,
    pub workflow_engines: Arc<OnceLock<WorkflowEngines>>,
    pub mcp_manager: Arc<OnceLock<Arc<McpManager>>>,
    pub wasm_manager: Option<Arc<WasmModuleManager>>,
    pub worker_service: Arc<WorkerService>,
    pub inflight_tracker: Arc<InFlightRequestTracker>,
}
```

逐字段的语义如下表（这是本讲最重要的一张表，后续实践会用到）：

| 字段 | 类型形态 | 何时有值 | 作用 |
| --- | --- | --- | --- |
| `client` | `Client`（reqwest，内部即 `Arc`） | 装配时 | 所有对 worker 的 HTTP 请求共用这一个客户端（连接池复用） |
| `router_config` | `RouterConfig`（按值克隆） | 装配时 | 运行期仍需读取的配置快照 |
| `rate_limiter` | `Option<Arc<TokenBucket>>` | 按配置可选 | `max_concurrent_requests<=0` 时为 `None`，即不限流 |
| `tokenizer_registry` | `Arc<TokenizerRegistry>` | 装配时（空表） | tokenizer 异步注册表，启动后才逐步填充 |
| `reasoning_parser_factory` | `Option<ReasoningParserFactory>` | 装配时 | gRPC / IGW 模式下解析思维链 |
| `tool_parser_factory` | `Option<ToolParserFactory>` | 装配时 | gRPC / IGW 模式下解析工具调用 |
| `worker_registry` | `Arc<WorkerRegistry>` | 装配时（空表） | 全部 worker 的注册表，启动后才注册 |
| `policy_registry` | `Arc<PolicyRegistry>` | 装配时 | per-model 负载均衡策略表 |
| `router_manager` | `Option<Arc<RouterManager>>` | **标准路径下保持 `None`** | 见下方说明 |
| `response_storage` / `conversation_storage` / `conversation_item_storage` | `Arc<dyn ...>`（trait 对象） | 装配时 | 历史/会话后端，由 `history_backend` 决定实现 |
| `load_monitor` | `Option<Arc<LoadMonitor>>` | 装配时 | 给 power-of-two 策略喂负载采样 |
| `configured_reasoning_parser` / `configured_tool_parser` | `Option<String>` | 在 `build()` 中派生 | 记录用户在配置里指定的解析器名 |
| `worker_job_queue` | `Arc<OnceLock<Arc<JobQueue>>>` | **延迟** | 控制面作业队列，破解循环依赖 |
| `workflow_engines` | `Arc<OnceLock<WorkflowEngines>>` | **延迟** | 类型化工作流引擎 |
| `mcp_manager` | `Arc<OnceLock<Arc<McpManager>>>` | 装配时即填（用 OnceLock 形态） | MCP 工具管理器 |
| `wasm_manager` | `Option<Arc<WasmModuleManager>>` | 按配置可选 | `enable_wasm` 时才有 |
| `worker_service` | `Arc<WorkerService>` | `build()` 中构造 | 把 registry+queue+config 封装成服务 |
| `inflight_tracker` | `Arc<InFlightRequestTracker>` | `build()` 中构造 | 在途请求采样（可观测性） |

> 关于 `router_manager`：它虽是 `AppContext` 的字段，但 `from_config` 装配链并不调用对应的 `with_router_manager`（见 [src/app_context.rs:289-309](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L289-L309)），因此标准启动路径下它保持 `None`。真正在用的 `RouterManager` 是在 `server::startup` 里单独创建，并以 `Arc<dyn RouterTrait>` 形态存进 `AppState.router`（见 [src/server.rs:914-915](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L914-L915)）。这个字段更多是给 builder 留的扩展位。

注意 `AppContext` 还**手写了 `Debug`**，见 [src/app_context.rs:64-70](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L64-L70)：

```rust
impl std::fmt::Debug for AppContext {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("AppContext")
            .field("router_config", &self.router_config)
            .finish_non_exhaustive()
    }
}
```

为什么手写？因为字段里有 `Arc<dyn ResponseStorage>` 这类 trait 对象，未必都实现了 `Debug`；用 `finish_non_exhaustive()` 既给出最关键的 `router_config`，又避免为每一个组件都实现 `Debug` 的麻烦。

#### 4.1.4 代码实践

**实践目标**：亲手把 `AppContext` 的字段按「初始化时机」分类，建立对共享状态的全局认知。

**操作步骤**：

1. 打开 [src/app_context.rs:39-62](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L39-L62)。
2. 用一张表，把 22 个字段分到三列：
   - **A. 启动即就绪（构造时就有值）**
   - **B. 按配置可选（可能为 `None`）**
   - **C. `OnceLock` 延迟（结构体里有占位，值后填）**
3. 对照上方语义表核对答案。

**需要观察的现象**：你会发现绝大多数字段属于 A 类（这些是真正的「共享底座」）；少数属于 B 类（功能开关）；只有 `worker_job_queue`、`workflow_engines` 属于「结构体已建、值未填」的纯延迟字段，而 `mcp_manager` 虽用 OnceLock 形态，但在装配阶段就已经 `set` 了。

**预期结果**：A 类约 13 个（含 `worker_service`、`inflight_tracker`），B 类约 5 个（`rate_limiter`、`load_monitor`、`wasm_manager`、`router_manager`、以及两个 parser factory 严格说也是 `Option` 但装配时必填），C 类 3 个。具体归类见本讲末尾「综合实践」的参考答案。

#### 4.1.5 小练习与答案

**练习 1**：`AppContext` 标了 `#[derive(Clone)]`，但里面有 `Client`、`RouterConfig` 这种看似「大」的字段，克隆会不会很贵？

> **答案**：不贵。`reqwest::Client` 内部本身就是 `Arc`，克隆只增计数、共享连接池；`RouterConfig` 虽按值持有，但它是启动期就固定、几乎不再变的配置，克隆开销可忽略；其余关键字段全是 `Arc<...>`，克隆即增计数。所以「到处克隆 `AppContext`」是设计上鼓励的低成本操作。

**练习 2**：为什么 `response_storage` 等三个 storage 字段类型是 `Arc<dyn ResponseStorage>`，而不是 `Arc<ResponseStorage>`？

> **答案**：因为存储后端有多种实现（memory / none / oracle / postgres / redis，见 u2-l2），具体用哪种要到运行期由 `history_backend` 决定。用 trait 对象 `dyn ResponseStorage` 才能在同一字段类型下容纳不同实现，这是典型的「依赖注入」做法。

---

### 4.2 OnceLock 惰性字段与循环依赖破解

#### 4.2.1 概念说明

`AppContext` 里有三个字段长这样：

```rust
pub worker_job_queue: Arc<OnceLock<Arc<JobQueue>>>,
pub workflow_engines: Arc<OnceLock<WorkflowEngines>>,
pub mcp_manager: Arc<OnceLock<Arc<McpManager>>>,
```

最外层是 `Arc<OnceLock<...>>`：`Arc` 让它能被克隆共享，`OnceLock` 让里面的值「先空着，之后再填一次」。

为什么要「先空着」？最核心的原因是**循环依赖**：

- `JobQueue` 在处理作业时，需要回调 `AppContext` 上的其它组件（比如注册 worker 要写 `worker_registry`、要触发 `workflow_engines`），因此 `JobQueue` 内部要持有一个指向 `AppContext` 的引用。
- 但 `AppContext` 又得**包含** `JobQueue` 字段。
- 这就成了「A 里面要有 B，B 里面要有 A」的鸡生蛋问题。

破解方法是经典的「弱引用 + 延迟填充」：

1. 先把 `AppContext` 整体 `Arc::new` 出来（此时 `worker_job_queue` 这个 `OnceLock` 是空的）。
2. 再用 `Arc::downgrade` 得到一个 `Weak<AppContext>`。
3. 用这个 `Weak` 去构造 `JobQueue`——`Weak` 不会增加强计数，因此不构成真正的循环引用，不会泄漏内存。
4. 最后把造好的 `JobQueue` 塞回 `AppContext.worker_job_queue`（`OnceLock::set`）。

`OnceLock` 正是为「构造到一半、某个字段晚点才有值」这种场景量身定做的：它允许结构体先以「空槽」形态存在，等依赖就绪再一次性写入，之后所有人用 `.get()` 读到的就是同一个真实值。

#### 4.2.2 核心流程

`server::startup` 中填充 OnceLock 的顺序如下（承接 u2-l3）：

```text
1. AppContext::from_config(...)   → 产出 Arc<AppContext>，其中 worker_job_queue / workflow_engines 是空 OnceLock
2. weak_context = Arc::downgrade(&app_context)
3. JobQueue::new(config, weak_context)        ← 用 Weak 构造，破解循环
4. app_context.worker_job_queue.set(job_queue) ← 一次性填入
5. engines = WorkflowEngines::new(...)
6. app_context.workflow_engines.set(engines)   ← 一次性填入
7. 提交 tokenizer / worker / MCP 启动作业到 job_queue
```

注意第 3 步是关键：`JobQueue` 拿到的是 `Weak<AppContext>`，处理作业时再 `upgrade()` 成 `Arc`；而 `AppContext` 持有的是 `Arc<OnceLock<Arc<JobQueue>>>`，两边都没有形成「强计数互相指」的死锁链。

#### 4.2.3 源码精读

装配阶段只创建「空槽」，见 [src/app_context.rs:470-480](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L470-L480)：

```rust
/// Create worker job queue OnceLock container
fn with_worker_job_queue(mut self) -> Self {
    self.worker_job_queue = Some(Arc::new(OnceLock::new()));  // 空槽
    self
}

/// Create workflow engines OnceLock container
fn with_workflow_engines(mut self) -> Self {
    self.workflow_engines = Some(Arc::new(OnceLock::new()));  // 空槽
    self
}
```

真正的填值发生在 `server.rs` 中。先 `downgrade` 再 `set`，见 [src/server.rs:809-814](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L809-L814)：

```rust
let weak_context = Arc::downgrade(&app_context);
let worker_job_queue = JobQueue::new(JobQueueConfig::default(), weak_context);
app_context
    .worker_job_queue
    .set(worker_job_queue)
    .expect("JobQueue should only be initialized once");
```

`workflow_engines` 的填充紧随其后，见 [src/server.rs:822-825](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L822-L825)：

```rust
app_context
    .workflow_engines
    .set(engines)
    .expect("WorkflowEngines should only be initialized once");
```

`.expect("... should only be initialized once")` 这句注释很关键：它说明设计者预期 `set` 只会成功一次，如果意外被二次 `set`（返回 `Err`）就是程序错误，应直接 panic 暴露。

读端则是 `.get()`。例如提交启动作业前先取出队列，见 [src/server.rs:841-844](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L841-L844)：

```rust
let job_queue = app_context
    .worker_job_queue
    .get()                          // 拿到 Arc<JobQueue>
    .expect("JobQueue should be initialized");
```

第三个 OnceLock 字段 `mcp_manager` 比较特殊：它在 `from_config` 内部**就已经 `set` 好了**（没有循环依赖问题），但仍保留 OnceLock 形态，见 [src/app_context.rs:486-512](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L486-L512)：

```rust
async fn with_mcp_manager(mut self, _router_config: &RouterConfig) -> Result<Self, String> {
    let mcp_manager_lock = Arc::new(OnceLock::new());
    // ... 用空配置构造 manager ...
    let manager = McpManager::with_defaults(empty_config).await?;
    mcp_manager_lock
        .set(Arc::new(manager))                     // 装配阶段就填
        .map_err(|_| "Failed to set MCP manager in OnceLock".to_string())?;
    self.mcp_manager = Some(mcp_manager_lock);
    Ok(self)
}
```

为什么 `mcp_manager` 能填却不直接用 `Arc<McpManager>`？因为它是 `async` 构造、且后续还可能被「重新装载」（MCP 服务器列表会通过作业动态变化），统一用 `OnceLock` 形态可以让读写两端都用 `.get()`，保持访问方式一致。

#### 4.2.4 代码实践

**实践目标**：追踪 `JobQueue` 与 `AppContext` 之间那条「弱引用」链，确认循环依赖确实被破解。

**操作步骤**：

1. 读 [src/server.rs:801-814](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L801-L814)，注意 `app_context` 先 `Arc::new` 出来，**之后**才 `downgrade`。
2. 打开 `src/core/job_queue.rs`，找到 `JobQueue::new` 的签名，确认它接收的是 `Weak<AppContext>`（而不是 `Arc`）。
3. 在 `job_queue.rs` 中找到处理作业时调用 `weak_context.upgrade()` 的位置，确认它把 `Weak` 升级回 `Arc<AppContext>` 才去访问 registry 等。

**需要观察的现象**：`JobQueue` 字段里存的是 `Weak`，因此即使它「指向」`AppContext`，也不会增加后者的强引用计数；`AppContext` 持有 `Arc<OnceLock<Arc<JobQueue>>>` 指向 `JobQueue`，这是唯一的强方向。整条链只有一个方向的强引用，不会形成引用环。

**预期结果**：确认 `JobQueue` 不持有 `Arc<AppContext>`，而是持有 `Weak<AppContext>`。这正是三个字段非用 `OnceLock` 不可的根因。若你的本地环境能编译，可尝试把 `JobQueue::new` 的参数从 `Weak` 改成 `Arc`（仅做思想实验，不要提交），观察编译器是否会因构造顺序而报错——**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `worker_job_queue` 直接声明成 `Arc<JobQueue>`（不用 OnceLock），会出什么问题？

> **答案**：构造 `JobQueue` 需要 `Weak<AppContext>`，而 `AppContext` 必须先 `Arc::new` 出来才能 `downgrade`。也就是说 `JobQueue` 必须在 `AppContext` **之后**构造，可它又是 `AppContext` 的字段——字段必须在结构体构造时就提供。这就成了「构造 AppContext 需要 JobQueue、构造 JobQueue 又需要 AppContext」的死结。`OnceLock` 用「先占空槽、后填值」解开了这个结。

**练习 2**：为什么 `mcp_manager` 没有循环依赖，却仍用 `OnceLock`？

> **答案**：为访问方式统一，且 MCP manager 的内容（服务器列表）会在运行期通过 `InitializeMcpServers` 等作业动态变化。保留 `OnceLock<Arc<McpManager>>` 形态，使得「读端 `.get()`」的代码与另外两个 OnceLock 字段写法一致，降低心智负担。

---

### 4.3 AppContextBuilder：链式装配器与 build() 校验

#### 4.3.1 概念说明

`AppContext` 有 22 个字段，直接用结构体字面量构造很容易漏填或填错顺序。项目用 **Builder 模式** 来缓解：

- `AppContextBuilder` 把每个字段都存成 `Option<T>`（初始全 `None`）。
- 每个字段配一个返回 `self` 的链式 setter，调用者用「`.field(v).field2(v2)...`」的方式逐步填值。
- 最后调用 `build()`，在这里统一做「必填字段是否已提供」的校验，并产出真正的 `AppContext`。

Builder 的好处是：必填与可选的边界集中在 `build()` 里一次性表达清楚，缺字段时给出明确的错误（哪个字段没给），而不是让程序带着半成品状态跑起来。

#### 4.3.2 核心流程

```text
AppContextBuilder::new()              // 全部 None
  .client(..)
  .router_config(..)
  ...
  .build()                             // 校验必填 + 派生字段 + 构造 AppContext
    ├─ 缺必填 → Err(AppContextBuildError("字段名"))
    └─ 齐全   → Ok(AppContext{ .. })，并在其中：
          · 用 registry+queue+config 构造 WorkerService
          · 用 InFlightRequestTracker::new() 构造 inflight_tracker
          · 从 router_config 派生 configured_reasoning_parser / configured_tool_parser
```

错误类型 `AppContextBuildError` 很朴素，就是把缺失的字段名包起来，见 [src/app_context.rs:28-37](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L28-L37)：

```rust
#[derive(Debug)]
pub struct AppContextBuildError(&'static str);   // 缺失字段名

impl std::fmt::Display for AppContextBuildError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Missing required field: {}", self.0)
    }
}
```

#### 4.3.3 源码精读

Builder 结构体所有字段都是 `Option<T>`，见 [src/app_context.rs:72-90](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L72-L90)：

```rust
pub struct AppContextBuilder {
    client: Option<Client>,
    router_config: Option<RouterConfig>,
    // ... 其余字段同样为 Option<...> ...
    wasm_manager: Option<Arc<WasmModuleManager>>,
}
```

链式 setter 形如（节选自 [src/app_context.rs:133-225](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L133-L225)）：

```rust
pub fn client(mut self, client: Client) -> Self {
    self.client = Some(client);
    self
}
pub fn router_config(mut self, router_config: RouterConfig) -> Self {
    self.router_config = Some(router_config);
    self
}
```

`build()` 的核心是「对每个必填字段 `ok_or(AppContextBuildError("字段名"))`」，见 [src/app_context.rs:227-285](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L227-L285)。关键片段：

```rust
pub fn build(self) -> Result<AppContext, AppContextBuildError> {
    let router_config = self
        .router_config
        .ok_or(AppContextBuildError("router_config"))?;
    let configured_reasoning_parser = router_config.reasoning_parser.clone();
    let configured_tool_parser = router_config.tool_call_parser.clone();

    let worker_registry = self
        .worker_registry
        .ok_or(AppContextBuildError("worker_registry"))?;
    let worker_job_queue = self
        .worker_job_queue
        .ok_or(AppContextBuildError("worker_job_queue"))?;

    // 用已构造的组件组装 WorkerService
    let worker_service = Arc::new(WorkerService::new(
        worker_registry.clone(),
        worker_job_queue.clone(),
        router_config.clone(),
    ));

    Ok(AppContext {
        client: self.client.ok_or(AppContextBuildError("client"))?,
        router_config,
        // ... 其余字段 ...
        worker_service,
        inflight_tracker: InFlightRequestTracker::new(),   // 在此构造
    })
}
```

注意几个细节：

- **必填字段**：`router_config`、`worker_registry`、`worker_job_queue`、`client`、`tokenizer_registry`、`policy_registry`、三个 storage、`workflow_engines`、`mcp_manager` 都用 `ok_or` 校验，缺了就 `Err`。
- **派生字段**：`configured_reasoning_parser` / `configured_tool_parser` 不是调用者传进来的，而是从 `router_config` 里 `.clone()` 派生（[src/app_context.rs:231-232](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L231-L232)）。
- **就地构造**：`worker_service` 与 `inflight_tracker` 不走 setter，而是在 `build()` 里由已有组件组装出来。这体现了「`build()` 既是校验器，也是最后的装配点」。

外层 `AppContext::from_config` 只是把 builder 串起来再 `build`，见 [src/app_context.rs:99-107](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L99-L107)：

```rust
pub async fn from_config(
    router_config: RouterConfig,
    request_timeout_secs: u64,
) -> Result<Self, String> {
    AppContextBuilder::from_config(router_config, request_timeout_secs)
        .await?
        .build()
        .map_err(|e| e.to_string())
}
```

#### 4.3.4 代码实践

**实践目标**：理解 `build()` 如何区分必填与可选字段，并尝试用 Builder 手动装配（绕过 `from_config`）。

**操作步骤**：

1. 读 [src/app_context.rs:227-285](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L227-L285)，数一数共有多少处 `ok_or(AppContextBuildError(...))`——这就是必填字段的数量。
2. 对照确认：`rate_limiter`、`reasoning_parser_factory`、`tool_parser_factory`、`router_manager`、`load_monitor`、`wasm_manager` 这些字段**没有** `ok_or`，说明它们是可选的（直接透传 `Option`）。
3. （可选，源码阅读型）设想你要为测试构造一个最小 `AppContext`：列出你必须手动提供给 Builder 的字段清单。

**需要观察的现象**：必填字段恰好是那些「没有它网关就无法转发请求」的核心组件（client、registry、queue、storage、policy）；可选字段都是「功能开关型」组件。

**预期结果**：约 12 处 `ok_or`，对应必填字段数。其余字段直接 `self.xxx` 透传，缺省即 `None`。

#### 4.3.5 小练习与答案

**练习 1**：`build()` 返回 `Result<AppContext, AppContextBuildError>`，但 `AppContext::from_config` 返回 `Result<Self, String>`，为什么类型不一样？

> **答案**：外层 `from_config` 把 `AppContextBuildError` 用 `.map_err(|e| e.to_string())` 转成了 `String`（见 [src/app_context.rs:106](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L106)）。这是为了让上层 `server::startup` 用统一的 `String` 错误链（`?` 直接传播），不必为每种内部错误类型分别处理。

**练习 2**：`worker_service` 和 `inflight_tracker` 没有 setter，调用者无法直接设置。这样设计合理吗？

> **答案**：合理。这两个组件完全由「已构造的其它字段」决定：`WorkerService` 是 `registry + queue + config` 的组合封装，`InFlightRequestTracker` 无需外部输入。把它们的构造收在 `build()` 里，避免调用者传入不一致的实例，保证「单一来源」。

---

### 4.4 from_config：从配置一键装配全部组件

#### 4.4.1 概念说明

`AppContextBuilder::from_config` 是真正干活的入口：给它一个 `RouterConfig` 和请求超时秒数，它就能装配出「几乎完整」的 `AppContext`（只差三个 OnceLock 的填值，那步在 `server::startup`）。

它的写法是一条很长的链式调用，每一步是一个私有的 `with_*` 方法，负责「从 config 读参数 → 构造某个组件 → 塞进 builder」。这种写法把原本一大段过程式初始化代码（注释里说约 194 行）拆成了 14 个命名清晰的小步骤，可读性和可测试性都更好。

#### 4.4.2 核心流程

装配链顺序如下（来自 [src/app_context.rs:289-309](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L289-L309)）：

```text
Self::new()
  .with_client(..)                  // 1. HTTP 客户端（TLS/mTLS）      ——后续多步要用
  .maybe_rate_limiter(..)           // 2. 限流器（可选）
  .with_tokenizer_registry(..)      // 3. tokenizer 注册表（空表）
  .with_reasoning_parser_factory()  // 4. 思维链解析器工厂
  .with_tool_parser_factory()       // 5. 工具调用解析器工厂
  .with_worker_registry()           // 6. worker 注册表（空表）
  .with_policy_registry(..)         // 7. 策略注册表
  .with_storage(..)                 // 8. 三个存储后端
  .with_load_monitor(..)            // 9. 负载监控（依赖 1 的 client、6 的 registry、7 的 policy）
  .with_worker_job_queue()          // 10. 空 OnceLock
  .with_workflow_engines()          // 11. 空 OnceLock
  .with_mcp_manager(..)             // 12. MCP 管理器（async，已填 OnceLock）
  .with_wasm_manager(..)            // 13. WASM 管理器（可选）
  .router_config(..)                // 14. 最后塞回 config 本身
```

注意步骤之间有**隐含的数据依赖**：`with_load_monitor` 需要用到前面 `with_client` 产出的 `client`、`with_worker_registry` 产出的 `registry`、`with_policy_registry` 产出的 `policy_registry`，所以它必须排在它们之后。这种依赖是通过方法体内 `self.client.as_ref().expect(...)` 来读取已填字段实现的（见 4.4.3）。

#### 4.4.3 源码精读

装配链本体，见 [src/app_context.rs:289-309](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L289-L309)：

```rust
pub async fn from_config(
    router_config: RouterConfig,
    request_timeout_secs: u64,
) -> Result<Self, String> {
    Ok(Self::new()
        .with_client(&router_config, request_timeout_secs)?
        .maybe_rate_limiter(&router_config)
        .with_tokenizer_registry(&router_config)?
        .with_reasoning_parser_factory()
        .with_tool_parser_factory()
        .with_worker_registry()
        .with_policy_registry(&router_config)
        .with_storage(&router_config)?
        .with_load_monitor(&router_config)
        .with_worker_job_queue()
        .with_workflow_engines()
        .with_mcp_manager(&router_config)
        .await?
        .with_wasm_manager(&router_config)?
        .router_config(router_config))
}
```

挑几个有代表性的步骤细看。

**`with_client`**——根据配置决定是否启用 TLS/mTLS，见 [src/app_context.rs:312-372](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L312-L372)。它先判断「是否配置了 TLS」，据此选择 rustls 后端，再注入 mTLS 身份与 CA 证书：

```rust
let has_tls_config = config.client_identity.is_some() || !config.ca_certificates.is_empty();
let mut client_builder = Client::builder()
    .pool_idle_timeout(Some(Duration::from_secs(config.pool_idle_timeout_secs)))
    .timeout(Duration::from_secs(timeout_secs))
    .tcp_nodelay(true)
    .tcp_keepalive(Some(Duration::from_secs(config.tcp_keepalive_secs)));
if has_tls_config {
    client_builder = client_builder.use_rustls_tls();
}
// ... 注入 identity / CA ...
```

> 代码里还留了一条重要的设计 FIXME（[src/app_context.rs:313-324](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L313-L324)）：当前是「所有 worker 共用一个 client」，适用于单一安全域；若要支持多 CA 多模型族部署，需把 client 下沉到 per-worker。这是后续扩展点。

**`maybe_rate_limiter`**——按 `max_concurrent_requests` 决定要不要限流器，见 [src/app_context.rs:375-390](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L375-L390)：

```rust
self.rate_limiter = match config.max_concurrent_requests {
    n if n <= 0 => None,                 // -1 或 0 → 不限流
    n => {
        let rate_limit_tokens = config
            .rate_limit_tokens_per_second
            .filter(|&t| t > 0)
            .unwrap_or(n);               // 未显式给令牌速率时，用并发数兜底
        Some(Arc::new(TokenBucket::new(n as usize, rate_limit_tokens as usize)))
    }
};
```

这与 u2-l2 讲的「`max_concurrent_requests` 默认 `-1` 即不限流」对上了：默认配置下这里产出 `None`，网关不限流。

**`with_load_monitor`**——展示步骤间数据依赖，见 [src/app_context.rs:450-468](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L450-L468)：

```rust
fn with_load_monitor(mut self, config: &RouterConfig) -> Self {
    let client = self.client.as_ref().expect("client must be set before load monitor");
    self.load_monitor = Some(Arc::new(LoadMonitor::new(
        self.worker_registry.as_ref().expect("worker_registry must be set").clone(),
        self.policy_registry.as_ref().expect("policy_registry must be set").clone(),
        client.clone(),
        config.worker_startup_check_interval_secs,
    )));
    self
}
```

这里的 `.expect("... must be set")` 就是在**断言链上顺序正确**：如果有人把 `with_load_monitor` 挪到 `with_client` 之前，运行时就会 panic。链式装配的顺序约定因此是「被代码强制」的。

**`with_storage`**——典型的工厂调用，见 [src/app_context.rs:432-447](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L432-L447)，把 `history_backend` 与各后端凭据打包成 `StorageFactoryConfig`，由外部 crate `data_connector` 的 `create_storage` 一次性产出三个存储：

```rust
let storage_config = StorageFactoryConfig {
    backend: &config.history_backend,
    oracle: config.oracle.as_ref(),
    postgres: config.postgres.as_ref(),
    redis: config.redis.as_ref(),
};
let (response_storage, conversation_storage, conversation_item_storage) =
    create_storage(storage_config)?;
```

这呼应 u2-l2 讲的「选了某个 `HistoryBackend` 就必须带对应凭据」——校验在 config 层，而真正按凭据建连接就在这里。

#### 4.4.4 代码实践

**实践目标**：通过阅读装配链，理解「config → 组件」的映射关系，并定位每个组件由哪些配置字段驱动。

**操作步骤**：

1. 打开 [src/app_context.rs:289-309](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L289-L309)。
2. 逐个 `with_*` 跳进去，记录「这一步读了 `RouterConfig` 的哪些字段」。例如：
   - `with_client` 读 `client_identity` / `ca_certificates` / `pool_idle_timeout_secs` / `connect_timeout_secs` / `tcp_keepalive_secs`；
   - `maybe_rate_limiter` 读 `max_concurrent_requests` / `rate_limit_tokens_per_second`；
   - `with_policy_registry` 读 `policy`；
   - `with_storage` 读 `history_backend` / `oracle` / `postgres` / `redis`；
   - `with_wasm_manager` 读 `enable_wasm`。
3. 画出一张「config 字段 → 组件」的对应表。

**需要观察的现象**：你会发现 `RouterConfig`（u2-l2 讲的「参数总表」）里的字段，几乎都在这条装配链上被消费。这印证了 `AppContext` 是「config 的运行期具象化」——配置是静态的参数表，`AppContext` 是这些参数实例化出的活组件。

**预期结果**：得到一张覆盖大多数 `RouterConfig` 字段的映射表。若某字段在装配链里找不到消费者，可能是它在 `server::startup` 的后续阶段（如健康检查、服务发现）才被读取。

#### 4.4.5 小练习与答案

**练习 1**：装配链里只有 `with_mcp_manager` 带 `.await`，为什么？

> **答案**：因为 `McpManager::with_defaults(..)` 是 `async` 的（[src/app_context.rs:501](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L501)），它内部可能涉及异步资源初始化。其余 `with_*` 都是同步构造。因此整个 `from_config` 也是 `async`，且调用方 `AppContext::from_config` 同样标了 `async`（[src/app_context.rs:99](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L99)）。

**练习 2**：如果 `with_load_monitor` 被错误地放到 `with_client` 之前，会怎样？

> **答案**：编译能过（因为 `self.client` 是 `Option`，`as_ref()` 总能调），但运行到 `self.client.as_ref().expect(...)` 会 panic，因为此时 `client` 还是 `None`。这说明链上步骤间的顺序依赖是靠 `.expect()` 在运行时强制，而非编译期保证——这也是为什么 `from_config` 里步骤顺序不能随意调换。

---

## 5. 综合实践

把本讲内容串起来，完成这道「分类 + 解释」的综合任务（即本讲规格里指定的实践）：

**任务**：列出 `AppContext` 的全部 `Arc` 相关字段，按下表分类，并解释「为什么有的字段必须延迟初始化」。

**步骤**：

1. 阅读 [src/app_context.rs:39-62](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L39-L62)。
2. 完成下表（参考答案附后，先自己填再看）。

| 字段 | 类型 | 类别（A 启动即就绪 / B 可选 / C OnceLock 延迟） | 说明 |
| --- | --- | --- | --- |
| `client` | `Client` | | |
| `worker_registry` | `Arc<WorkerRegistry>` | | |
| `policy_registry` | `Arc<PolicyRegistry>` | | |
| `rate_limiter` | `Option<Arc<TokenBucket>>` | | |
| `worker_job_queue` | `Arc<OnceLock<Arc<JobQueue>>>` | | |
| `workflow_engines` | `Arc<OnceLock<WorkflowEngines>>` | | |
| `mcp_manager` | `Arc<OnceLock<Arc<McpManager>>>` | | |
| `load_monitor` | `Option<Arc<LoadMonitor>>` | | |
| `wasm_manager` | `Option<Arc<WasmModuleManager>>` | | |
| `router_manager` | `Option<Arc<RouterManager>>` | | |

3. **回答关键问题**：`worker_job_queue` 与 `workflow_engines` 为什么不能在 `from_config` 阶段就填好值？请用「循环依赖」和「`Weak<AppContext>`」这两个词写出一句解释。

**参考答案**：

| 字段 | 类别 | 说明 |
| --- | --- | --- |
| `client` | A | 装配阶段由 `with_client` 构造，全程序共享一个连接池 |
| `worker_registry` / `policy_registry` | A | 装配阶段构造（空表），启动后逐步填充 |
| `rate_limiter` | B | `max_concurrent_requests <= 0` 时为 `None`（默认即不限流） |
| `worker_job_queue` | C | 结构体构造时只有空 `OnceLock`，真正值在 `server::startup` 里 `set`（破解循环依赖） |
| `workflow_engines` | C | 同上，在 `server::startup` 里 `set` |
| `mcp_manager` | C（但装配期已填） | 用 `OnceLock` 形态，但在 `with_mcp_manager` 内就已 `set`，为访问方式统一 |
| `load_monitor` | B | 字段是 `Option`，实际装配时设为 `Some`，是否真正启用看后续 `start()` |
| `wasm_manager` | B | 仅 `enable_wasm` 时为 `Some` |
| `router_manager` | B | 标准路径下保持 `None`，真正在用的 router 存于 `AppState.router` |

**关键问题答案**：因为 `JobQueue` 处理作业时要回调 `AppContext` 上的组件，需要持有指向 `AppContext` 的 `Weak<AppContext>`；而 `AppContext` 又必须先 `Arc::new` 出来才能 `downgrade` 得到 `Weak`。这种「构造 A 需要 B、构造 B 又需要 A」的**循环依赖**，只能靠「先用 `OnceLock` 占空槽、等 `AppContext` 建好后再 `set`」来破解。

## 6. 本讲小结

- `AppContext` 是网关运行时的「共享神经中枢」，把 22 个需要全程序共享的组件（registry、policy、storage、queue、tracker 等）打包进一个可廉价克隆的结构体。
- 克隆廉价的关键：关键字段都是 `Arc<...>`，`reqwest::Client` 内部也是 `Arc`，所以「到处 `clone` 一份 `AppContext`」只是增计数，不复制数据。
- 三个 `OnceLock` 字段（`worker_job_queue`、`workflow_engines`、`mcp_manager`）用于「先占位、后填值」；其中前两者必须延迟，是为了破解 `JobQueue` 与 `AppContext` 的循环依赖（用 `Weak<AppContext>`）。
- `AppContextBuilder` 用链式 setter + `build()` 集中校验，必填字段用 `ok_or(AppContextBuildError("字段名"))` 兜底，`worker_service` 与 `inflight_tracker` 在 `build()` 中就地构造。
- `from_config` 是一条 14 步的 `with_*` 装配链，把 `RouterConfig` 实例化成活组件；步骤间有隐含数据依赖，靠 `.expect("... must be set")` 在运行时强制顺序。
- `AppContext` 是 `RouterConfig` 的「运行期具象化」：config 是静态参数表，`AppContext` 是这些参数造出来的、可被到处共享的活状态。

## 7. 下一步学习建议

本讲把「共享状态怎么组装」讲完了，接下来建议：

- **进入控制面**：学习 [u3-l2 WorkerRegistry 与 HashRing](u3-l2-worker-registry.md)，看 `AppContext.worker_registry` 这个空表启动后如何被填充、如何被查询。
- **看作业队列细节**：学习 [u3-l4 JobQueue 异步作业队列](u3-l4-job-queue.md)，理解 `AppContext.worker_job_queue` 这个 OnceLock 里的 `JobQueue` 如何用 `Weak<AppContext>` 处理作业、如何限并发。
- **看路由器如何消费 AppContext**：学习 [u4-l2 RouterManager 与 IGW 多模型](u4-l2-router-manager-and-igw.md)，理解 `RouterManager::from_config(&config, &app_context)` 怎样从共享状态里取 registry/policy 来构造路由器。
- **延伸阅读**：直接对照 [src/app_context.rs](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs) 与 [src/server.rs](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs) 的 `startup` 函数，把「装配 → 填 OnceLock → 提交启动作业」三段在源码里完整走一遍。
