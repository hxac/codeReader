# server::startup 启动编排

## 1. 本讲目标

上一篇（u2-l1）我们看清了「命令行参数 → `RouterConfig`」这一翻译链，但那个 `RouterConfig` 对象究竟被谁消费、各子系统按什么顺序启动起来，还没有展开。本讲就负责补上这一段：跟踪从 `main()` 到 `server::startup()` 的**完整启动流程**。

学完本讲你应该能够：

- 说清 `startup` 里「日志/追踪 → 指标 → Mesh → AppContext → JobQueue → WorkflowEngines → 启动作业 → RouterManager → 健康检查 → 限流 → 服务发现」这条初始化时间线，并解释**为什么是这个顺序**。
- 理解启动时三类后台作业（tokenizer / worker / MCP）**何时、按什么次序**被提交，以及为什么提交后启动并不阻塞。
- 弄懂 `OnceLock` 在 `AppContext` 三个字段上的「写一次、后填入」惰性初始化模式，以及它为什么能解开 `JobQueue` 与 `AppContext` 的循环依赖。

## 2. 前置知识

- **进程启动与异步运行时**：本项目的入口函数先构造一个 `tokio::runtime::Runtime`，再用 `block_on` 驱动一个异步的 `startup`。理解「同步的 `main` 把控制权交给异步世界」这一点即可。
- **`Arc` 与循环依赖**：`Arc<T>` 是引用计数的共享指针。若 `A` 持有 `Arc<B>`、`B` 又持有 `Arc<A>`，两者都不会被释放（内存泄漏）。解决办法之一是其中一方改用「弱引用」`Weak<T>`。
- **`OnceLock<T>`（一次性可变单元）**：标准库提供的「只能写入一次」的同步原语。它可以在不可变引用（`&OnceLock`）下被「填入」一次值，之后所有人都能读到同一个值。你可以把它理解成一个**运行期才赋值、赋值后只读的 `const`**。
- **控制面 vs 数据面（来自 u1-l1）**：`AppContext` / `WorkerRegistry` / `JobQueue` 属于控制面底座；`RouterManager` 与各个路由器属于数据面。本讲会看到这两面在 `startup` 里是如何被装配到一起的。
- **异步作业队列（预告 u3-l4）**：`JobQueue` 用一个 `mpsc` 通道接收作业、用一个信号量限制并发，在后台逐个执行。本讲只关注「谁在启动时往里提交了什么」，执行细节留到 u3-l4。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/main.rs` | 进程入口，命令行解析 | `main()` 如何把控制权交给 `server::startup` |
| `src/server.rs` | Axum 应用装配与启动编排 | `pub async fn startup()` 整段初始化顺序 |
| `src/app_context.rs` | 聚合各层组件的共享状态 | `AppContext` 的三个 `OnceLock` 字段与 `from_config` |
| `src/core/job_queue.rs` | 异步作业队列 | `Job` 枚举、`submit`、`JobQueue::new` 的分发模型 |
| `src/core/steps/workflow_engines.rs` | 类型化工作流引擎集合 | `WorkflowEngines::new` / `subscribe_all` |
| `src/routers/router_manager.rs` | 多路由器协调（IGW） | `RouterManager::from_config` 的两种装配模式 |

> 本讲重点是「顺序与依赖」，因此会频繁出现行号引用。链接均指向当前 HEAD，方便你逐行对照。

## 4. 核心概念与源码讲解

### 4.1 启动交接：从 main 到 startup

#### 4.1.1 概念说明

网关进程的入口 `main()` 本身是同步的、且刻意保持极薄。它只做三件事：拦截版本参数、解析命令行、构造配置，然后把舞台让给异步的 `server::startup`。`startup` 才是真正「把一堆零件组装成一台跑得起来的网关」的函数。把启动逻辑放在 `async fn startup` 而不是 `main`，是因为启动过程里大量使用 `.await`（建客户端、连数据库、起 mesh、装路由器）。

#### 4.1.2 核心流程

```text
main()                                  # 同步入口
 ├─ 命中 --version/-V/--version-verbose ? → 打印版本后直接 return
 ├─ parse_prefill_args()                # 预扫描 --prefill 可选第二值
 ├─ 过滤 argv 后交给 clap 解析 → CliArgs
 ├─ cli_args.to_router_config() → router_config
 ├─ router_config.validate()            # 集中校验（见 u2-l2）
 ├─ cli_args.to_server_config(router_config) → ServerConfig
 ├─ tokio::Runtime::new()               # 创建异步运行时
 └─ runtime.block_on(server::startup(server_config))  # 进入异步启动
```

关键点：`validate()` 在交给 `startup` **之前**就被调用一次（见下方源码），所以「非法配置」在进 `startup` 前就被拦下；`startup` 内部不会再重复校验整份配置，但 `builder.build()` 这类局部封口仍可能再做局部校验。

#### 4.1.3 源码精读

版本参数拦截，先于 clap 解析，避免 clap 把 `--version` 当成普通参数报错：[src/main.rs:1192-L1201](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L1192-L1201) —— 这段先扫描 `args`，命中即打印版本字符串后 `return Ok(())`。

把配置校验与运行时交接串起来：[src/main.rs:1271-L1276](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L1271-L1276) —— 这段先 `to_router_config` 得到 `RouterConfig`，再 `router_config.validate()?`，然后 `to_server_config` 装成 `ServerConfig`，最后 `runtime.block_on(async move { server::startup(server_config).await })`。注意 `block_on` 的闭包把 `server_config` move 进去，此后同步代码再也无法访问它——所有后续消费都在异步 `startup` 内完成。

`ServerConfig` 是 `startup` 的唯一入参，相当于「启动这一台网关所需的外部参数总表」：[src/server.rs:518-L534](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L518-L534) —— 它既包含 `router_config`（路由形态、策略、可靠性），也包含 `host/port`、日志、Prometheus、服务发现、mesh 等运行期参数。

#### 4.1.4 代码实践（源码阅读型）

1. 实践目标：确认 `main` 真的把全部启动责任交给 `startup`，自身不再持有 `ServerConfig`。
2. 操作步骤：在 [src/main.rs:1271-L1276](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L1271-L1276) 之后，尝试在脑中（或临时本地分支上）插入一行使用 `server_config` 的语句，看编译器报「value moved」。
3. 需要观察的现象：`block_on` 的闭包以 `move` 捕获，`server_config` 的所有权已经进入异步世界。
4. 预期结果：你会理解「`main` 极薄」是靠所有权移交强制的，而非靠纪律。
5. 运行结果：待本地验证（需要本地编译环境）。

#### 4.1.5 小练习与答案

- 练习 1：为什么 `--version` 要在 clap 解析之前手工拦截，而不是用 clap 内置的版本支持？
  - 答案：项目需要支持 `--version-verbose` 这种自定义输出（含 git 状态、编译器版本，见 u1-l2 的 `build.rs` 注入），clap 内置版本标志无法承载这种自定义逻辑，因此在解析前手工拦截、直接打印并退出。
- 练习 2：`router_config.validate()` 在 `main` 里已经调用过一次，`startup` 里还会再整体校验吗？
  - 答案：不会整体再校验。`startup` 假定进来的 `ServerConfig` 已通过校验，仅在局部装配（如 `AppContext::from_config`、`RouterManager::from_config`）处可能产生装配期错误。

---

### 4.2 server::startup 的初始化全景

#### 4.2.1 概念说明

`startup` 是一条「有序的装配流水线」。它的难点不在于某个零件有多复杂，而在于**顺序敏感**：把后置步骤前置就会 panic 或拿不到依赖。这一节先给出全景图与每个阶段的一句话职责，后续 4.3–4.5 再放大三个关键阶段。

#### 4.2.2 核心流程

`startup` 的阶段顺序（按源码出现先后）：

```text
server::startup(config)
 1. otel_tracing_init        # OpenTelemetry 追踪（可选）
 2. init_logging             # 结构化日志（一次性守卫）
 3. start_prometheus         # 指标端点（可选）
 4. mesh 构建 + spawn         # HA/CRDT 同步服务（可选）
 5. AppContext::from_config  # ★ 控制面共享状态装配
 6. inflight_sampler         # 在途请求采样（依赖 prometheus）
 7. JobQueue::new + set      # ★ 把作业队列填入 OnceLock
 8. WorkflowEngines::new + set + subscribe_all  # ★ 工作流引擎填入 OnceLock
 9. submit(AddTokenizer)     # 启动 tokenizer 作业（若有 --tokenizer-path/--model-path）
10. submit(InitializeWorkersFromConfig)         # ★ 启动 worker 注册作业
11. submit(InitializeMcpServers)                # 启动 MCP 作业（若有 mcp_config）
12. MCP 后台刷新 spawn
13. RouterManager::from_config                  # ★ 数据面路由器装配
14. start_health_checker / LoadMonitor::start   # 健康与负载探针
15. ConcurrencyLimiter（限流/队列）spawn
16. set_mesh_sync 到两个 registry（若 mesh 开启）
17. 组装 AppState
18. start_service_discovery（若开启）
19. build_app（axum 路由表 + 中间件）
20. bind + graceful_shutdown + serve
```

「★」标注的就是本讲要放大的最小模块（AppContext/OnceLock 见 4.3、JobQueue 提交见 4.4、RouterManager 见 4.5）。三个核心结论先记下来：

1. **日志必须最先初始化**：后面所有阶段都依赖 `tracing`，先有日志才能记录任何问题。
2. **控制面（5–8）必须先于启动作业（9–11）就绪**：作业执行时要读取 `worker_job_queue` 与 `workflow_engines` 这两个 `OnceLock`，未填入就会报错。
3. **作业是「提交后即返回」**：第 9–11 步只把作业塞进队列，并不等待完成；因此网关能在 worker 还没注册完时就 bind 端口开始服务，真正的就绪判定交给 `/readiness`。

#### 4.2.3 源码精读

`startup` 函数签名与日志守卫：[src/server.rs:696-L730](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L696-L730) —— 注意顶部 `static LOGGING_INITIALIZED: AtomicBool`。这是一个**进程级一次性守卫**：`swap(true, SeqCst)` 返回旧值，只有第一个进入者返回 `false` 并真正初始化日志，其余调用（比如被嵌入库时可能重复触发）拿到的 `_log_guard` 是 `None`。被赋值给 `_log_guard` 的返回值（`WorkerGuard`）必须活到函数末尾——一旦 drop，日志缓冲会被刷新并关闭，所以它一直被持有到 `startup` 结束。

可选子系统（Prometheus、Mesh）紧随其后，二者都是「配了才起」：[src/server.rs:732-L790](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L732-L790) —— Prometheus 仅在 `prometheus_config.is_some()` 时 `start_prometheus`；Mesh 仅在 `mesh_server_config` 存在时构建 `StateStores` / `MeshSyncManager` / `MeshServer` 并 `spawn` 到后台。Mesh 块把 `mesh_handler`、`mesh_sync_manager` 两个值返回出来，供后面第 16 步与 `AppState` 使用。

控制面装配的入口与一条醒目的「启动横幅」：[src/server.rs:792-L807](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L792-L807) —— 这里 `AppContext::from_config(...).await?` 创建控制面共享状态；随后 `app_context.inflight_tracker.start_sampler(20)` 必须在 `prometheus_config.is_some()` 为真时才调，因为它喂给指标。

数据面装配与后续探针、限流、mesh 注入：[src/server.rs:914-L974](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L914-L974) —— `RouterManager::from_config`（4.5 详述）之后，才是健康检查（`start_health_checker`，受 `disable_health_check` 控制）、`LoadMonitor::start`、`ConcurrencyLimiter`、以及把 `mesh_sync_manager` 设到两个 registry 上。注意第 16 步「set mesh sync」是**可选附加**：不开启 mesh 时 registry 照常独立工作，开启后才有跨实例同步。

最后的 bind + serve：[src/server.rs:1050-L1091](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L1050-L1091) —— 根据 `server_cert`/`server_key` 是否同时存在，选择 `bind_rustls`（TLS）或 `bind`（明文），并挂上 `axum_server::Handle` 实现优雅关闭（`shutdown_signal` 在 [src/server.rs:1099-L1125](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L1099-L1125) 监听 Ctrl-C / SIGTERM）。

#### 4.2.4 代码实践（源码阅读型）

1. 实践目标：体会「顺序敏感」。
2. 操作步骤：对照上面的全景图，在 [src/server.rs](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs) 里找到「第 6 步 inflight sampler」与「第 3 步 prometheus」之间是否真有依赖（提示：`start_sampler` 只在 `prometheus_config.is_some()` 分支里调用）。
3. 需要观察的现象：如果理论上把 prometheus 初始化挪到 sampler 之后会怎样。
4. 预期结果：sampler 无端点可上报，或空跑——这就是顺序的必要性。
5. 运行结果：待本地验证。

#### 4.2.5 小练习与答案

- 练习 1：为什么 `_log_guard` 名字带下划线却不能删掉？
  - 答案：下划线前缀只是「告诉编译器我故意不读这个变量」，并不会提前 drop。`_log_guard` 作为局部变量持有 `WorkerGuard`，它**必须存活到 `startup` 末尾**；一旦提前 drop，日志会在程序还没结束时被关闭。它的生命周期即「日志生效期」。
- 练习 2：mesh 块返回的 `mesh_handler` / `mesh_sync_manager` 在后续哪两处被消费？
  - 答案：`mesh_sync_manager` 在第 16 步被 `set_mesh_sync` 到 `worker_registry` 与 `policy_registry`（[src/server.rs:964-L974](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L964-L974)）；两者随后都被装入 `AppState`（[src/server.rs:983-L990](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L983-L990)）。

---

### 4.3 OnceLock 与 AppContext 装配（惰性初始化）

#### 4.3.1 概念说明

`AppContext` 是控制面的「共享状态大对象」，几乎所有子系统都通过 `Arc<AppContext>` 共享它。但里面有三个字段无法在构造 `AppContext` 时就给出最终值——`worker_job_queue`、`workflow_engines`、`mcp_manager`——它们被设计成 `Arc<OnceLock<...>>`。**`OnceLock` 让我们先用一个「空盒子」占位，等依赖凑齐后再把真值「填入」一次**；填入之后，所有共享同一个 `Arc<OnceLock>` 的代码（包括 `WorkerService`、各路由器）都能读到。

为什么要绕这一圈？核心是一个**鸡生蛋问题**：`JobQueue` 的构造需要一个指向 `AppContext` 的弱引用 `Weak<AppContext>`（为了避免循环引用，见 4.3.2）；但 `AppContext` 自己还没构造完时，根本拿不到 `Arc<AppContext>`。于是顺序只能是：

```text
先建 AppContext（盒子先空着）  →  拿到 Arc<AppContext>  →  用它的弱引用建 JobQueue  →  把 JobQueue 填进盒子
```

`OnceLock` 正是这条「先占位、后填值」链路上的那只盒子。

#### 4.3.2 核心流程

`AppContext` 的三个 `OnceLock` 字段：

```rust
pub worker_job_queue:  Arc<OnceLock<Arc<JobQueue>>>,    // 启动期在 startup 里填入
pub workflow_engines:  Arc<OnceLock<WorkflowEngines>>,  // 启动期在 startup 里填入
pub mcp_manager:       Arc<OnceLock<Arc<McpManager>>>,  // from_config 内部就已填入
```

注意三者的「填值时机不同」：

| 字段 | 盒子何时创建 | 真值何时填入 | 谁填入 |
| --- | --- | --- | --- |
| `worker_job_queue` | `from_config`（`with_worker_job_queue`） | `startup` 第 7 步 | `server::startup` |
| `workflow_engines` | `from_config`（`with_workflow_engines`） | `startup` 第 8 步 | `server::startup` |
| `mcp_manager` | `from_config`（`with_mcp_manager`） | `from_config` 内部即刻 | `AppContextBuilder` |

引用关系与循环依赖的解除可以形式化地看：

\[ \text{AppContext} \xrightarrow{\text{Arc<OnceLock<Arc<JobQueue>>>}} \text{JobQueue} \xrightarrow{\text{Weak<AppContext>}} \text{AppContext} \]

如果 `JobQueue` 持有的是 `Arc<AppContext>` 而非 `Weak<AppContext>`，就形成强引用环 \(\text{AppContext} \to \text{JobQueue} \to \text{AppContext}\)，二者永不释放。用 `Weak` 断开反向强边后，环被打破，`AppContext` 可以被正常回收，`JobQueue` 在 `submit` 时通过 `upgrade().is_none()` 判断「上下文已销毁、队列正在关闭」。

#### 4.3.3 源码精读

`AppContext` 的三个 `OnceLock` 字段声明：[src/app_context.rs:56-L58](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L56-L58) —— `worker_job_queue` 与 `workflow_engines` 外层都是 `Arc<OnceLock<...>>`，因此能被 `WorkerService`、路由器等多处廉价 `clone` 共享。

`AppContext::from_config` 是装配入口：[src/app_context.rs:99-L107](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L99-L107) —— 它委托给 `AppContextBuilder::from_config` 再 `.build()`，注释里写明「replaces ~194 lines of initialization in server.rs」，即这一大块装配逻辑原本散落在 `startup` 里，后被抽到 builder。

builder 链式装配（注意顺序：先建 registry/policy，再建空的 job_queue/engines 盒子，最后建 mcp 并立刻填值）：[src/app_context.rs:289-L309](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L289-L309)。

两个「只创建空盒子」的步骤：[src/app_context.rs:471-L480](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L471-L480) —— `with_worker_job_queue` / `with_workflow_engines` 都只做 `Arc::new(OnceLock::new())`，盒子是空的。

`mcp_manager` 则在 builder 内部「建盒即填值」：[src/app_context.rs:486-L512](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L486-L512) —— 它用空配置创建 `McpManager`，然后 `mcp_manager_lock.set(Arc::new(manager))`；真正的 MCP server 注册要等到 `startup` 第 11 步的 `InitializeMcpServers` 作业（见 4.4）。

真正的「后填值」发生在 `startup`：[src/server.rs:809-L825](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L809-L825) ——
- 第 810 行 `Arc::downgrade(&app_context)` 拿到弱引用，**这一步只有在拿到 `Arc<AppContext>` 之后才能做**，正对应了 4.3.1 的鸡生蛋问题；
- `JobQueue::new(JobQueueConfig::default(), weak_context)` 构造队列；
- `app_context.worker_job_queue.set(...).expect("...only be initialized once")` 把它填入盒子——`.expect` 的字符串点明了 `OnceLock` 的「只能写一次」语义，二次写入会 panic。

`workflow_engines` 同理在第 817–825 行：先 `WorkflowEngines::new(&config.router_config)` 构造引擎集合，`subscribe_all` 挂上日志订阅，再 `set` 进盒子。

#### 4.3.4 代码实践（源码阅读型）

1. 实践目标：验证 `WorkerService` 持有的是「盒子」而非「最终值」。
2. 操作步骤：阅读 [src/app_context.rs:242-L246](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L242-L246)，确认 `WorkerService::new` 接收的是 `worker_job_queue.clone()`（即 `Arc<OnceLock<Arc<JobQueue>>>`）。再去 `src/core/worker_service.rs` 看它内部如何 `.get()` 取出真值。
3. 需要观察的现象：`WorkerService` 构造时 `JobQueue` 尚未填入，但它在每次处理请求时才 `.get()`，因此总能拿到最新填入的队列。
4. 预期结果：你会看到「构造时解耦、使用时解析」的惰性模式。
5. 运行结果：待本地验证。

#### 4.3.5 小练习与答案

- 练习 1：如果 `worker_job_queue.set(...)` 被误调了两次，会发生什么？
  - 答案：`OnceLock::set` 在已写入时返回 `Err(已存在的值)`；本处用了 `.expect("...only be initialized once")`，所以第二次调用会直接 panic。这是把「一次性」用类型系统强制的体现。
- 练习 2：为什么 `JobQueue` 用 `Weak<AppContext>` 而不是 `Arc<AppContext>`？
  - 答案：因为 `AppContext` 经由 `worker_job_queue` 字段（间接通过 `WorkerService`）强持有 `JobQueue`；若 `JobQueue` 再强持有 `AppContext`，就构成强引用环导致泄漏，且队列无法感知「上下文已销毁」。`Weak` 既打破环，又让 `submit` 能用 `upgrade().is_none()` 判定关闭。

---

### 4.4 JobQueue：启动作业（tokenizer / worker / MCP）的提交时机

#### 4.4.1 概念说明

`JobQueue` 是控制面的异步作业队列：提交者把 `Job` 丢进队列立刻返回，后台任务在信号量限流下逐个执行。启动期往里提交了三类作业——加载 tokenizer、注册 worker、注册 MCP server——它们决定了网关「开服后能力是否齐备」。理解「提交即返回、后台串行执行」这一语义，就能解释一个常被初学者误判的现象：**进程日志打印「Router ready」时，worker 很可能还没全部注册完**。

#### 4.4.2 核心流程

`JobQueue` 的并发模型：

```text
submit(job) ──send──▶ [mpsc channel(cap=1000)] ──▶ dispatcher task
                                                         │ acquire semaphore permit (max 200)
                                                         ▼
                                                  spawn(process_job) ──▶ execute_job ──▶ workflow engine
status_map: worker_url ─▶ Pending → Processing → (移除 | Failed)
```

启动期三类作业的提交顺序（来自 `startup` 第 9–11 步）：

```text
9.  if tokenizer_path/model_path 存在: submit(Job::AddTokenizer)        # 先于 worker
10. submit(Job::InitializeWorkersFromConfig)                            # 注册所有 worker
11. if mcp_config 存在:        submit(Job::InitializeMcpServers)         # 注册所有 MCP server
```

注意第 10 步的 `InitializeWorkersFromConfig` **本身不直接注册**，它在 `execute_job` 里按 `RoutingMode` 再把每个 URL 拆成若干 `Job::AddWorker` 重新 `submit`（见源码精读）。这是一种「扇出（fan-out）」模式：一个外层作业 → 多个内层 `AddWorker` 作业。

#### 4.4.3 源码精读

`Job` 枚举覆盖控制面全部作业类型：[src/core/job_queue.rs:34-L66](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/job_queue.rs#L34-L66) —— 启动期用到的是 `InitializeWorkersFromConfig`、`InitializeMcpServers`、`AddTokenizer` 三种；运行期还会用到 `AddWorker`/`UpdateWorker`/`RemoveWorker`（来自 `/workers` API，见 u3-l6）、`AddWasmModule` 等。

`JobQueue::new` 的分发模型：[src/core/job_queue.rs:145-L194](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/job_queue.rs#L145-L194) —— 它建立 `mpsc::channel(queue_capacity)`，`spawn` 一个 dispatcher 任务：每收到一个 job，先 `sem.acquire_owned()` 拿许可（拿不到就阻塞等待，从而把并发限制在 `max_concurrent_jobs`），拿到后再 `spawn` 一个独立任务跑 `process_job`。默认配置（[src/core/job_queue.rs:111-L118](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/job_queue.rs#L111-L118)）是「队列容量 1000、最大并发 200」。另外还 `spawn` 了一个 5 分钟 TTL 的状态清理任务（[src/core/job_queue.rs:187-L191](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/job_queue.rs#L187-L191)）。

`submit` 提交即返回：[src/core/job_queue.rs:204-L239](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/job_queue.rs#L204-L239) —— 先 `context.upgrade().is_none()` 判定关闭，再写一条 `Pending` 状态，然后 `tx.send(job)`；`send` 成功就 `Ok(())` 返回。**它不会等待作业执行完**——这正是启动不阻塞的关键。

启动期的三类提交：[src/server.rs:831-L898](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L831-L898) ——
- tokenizer 作业（第 855 行 `Job::AddTokenizer`，第 860 行 `.submit(job)`）：源码注释明确写道「This runs before worker initialization to ensure tokenizer is available」（[src/server.rs:831-L865](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L831-L865)），即有意排在 worker 之前。
- worker 作业（第 877 行 `Job::InitializeWorkersFromConfig`，第 881 行 `.submit(job)`）。
- MCP 作业（第 889 行 `Job::InitializeMcpServers`，第 893 行 `.submit(mcp_job)`）：仅当 `mcp_config` 存在时提交，否则打印「skipping MCP server initialization」。

「外层作业扇出成内层 AddWorker」的细节：[src/core/job_queue.rs:496-L640](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/job_queue.rs#L496-L640) —— `execute_job` 对 `InitializeWorkersFromConfig` 按 `RoutingMode`（Regular / PrefillDecode / OpenAI）把每个 URL 构造成 `WorkerConfigRequest`，再 `context.worker_job_queue.get()?.submit(Job::AddWorker{...})`。注意它通过 `worker_job_queue.get()` 拿队列——这正是 4.3 里 `OnceLock` 必须先填好值的根本原因：**如果第 7 步没把队列填进盒子，这里 `.get()` 返回 `None`，作业直接失败并报 "JobQueue not available"**。

> 提交完作业后，[src/server.rs:908-L912](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L908-L912) 紧接着打印 `worker_registry.stats()` 的「total/healthy」。由于 worker 注册是异步后台进行的，这条早期日志很可能显示 healthy=0，这是正常现象，并非故障。

#### 4.4.4 代码实践（源码阅读型）

1. 实践目标：亲眼确认「提交即返回、后台执行」的语义。
2. 操作步骤：在 [src/core/job_queue.rs:204-L239](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/job_queue.rs#L204-L239) 的 `submit` 与 [src/core/job_queue.rs:252-L291](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/job_queue.rs#L252-L291) 的 `process_job` 之间，对照 `status_map` 的状态迁移：`Pending`（submit 时）→ `Processing`（process_job 起始）→ 完成后移除或置 `Failed`（[src/core/job_queue.rs:748-L774](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/job_queue.rs#L748-L774)）。
3. 需要观察的现象：`submit` 路径上没有任何 `await` 等待 workflow 完成，只有 `tx.send(job).await`（等通道空位）。
4. 预期结果：你会理解为什么「Router ready」打印时 worker 可能尚未就绪，以及为什么 `/readiness` 才是真正判定（见 u2-l5）。
5. 运行结果：待本地验证。

#### 4.4.5 小练习与答案

- 练习 1：为什么 tokenizer 作业要在 worker 作业之前提交？
  - 答案：tokenizer 加载（可能从 HuggingFace 下载）耗时且是 worker 注册流程之外的能力。先提交可让它在后台尽早开始；worker 注册工作流在需要 tokenizer 时（如按 `model_id` 注册 tokenizer）能更可能命中已就绪状态。这只是提交次序，由于执行是并发后台的，并不保证 tokenizer 一定先完成。
- 练习 2：`InitializeWorkersFromConfig` 与 `AddWorker` 是什么关系？
  - 答案：前者是「批量入口作业」，后者是「单个 worker 注册作业」。前者在 `execute_job` 内根据 `RoutingMode` 把配置里的每个 URL 拆成多个 `AddWorker` 再 `submit`，是一种扇出；每个 `AddWorker` 再由 dispatcher 调度执行。

---

### 4.5 RouterManager::from_config：数据面路由器装配

#### 4.5.1 概念说明

控制面就绪后，`startup` 第 13 步装配数据面：`RouterManager::from_config`。`RouterManager` 是一个「多路由器协调者」——在 IGW（Inference Gateway，多模型网关）模式下它会同时持有 HTTP/gRPC、Regular/PD、OpenAI 等多个路由器，按请求里的 `model` 分发；在单路由器模式下它只持有一个。无论哪种模式，它最终对外暴露成 `Arc<dyn RouterTrait>`，成为 `AppState.router`，承担每一次推理请求的转发。

#### 4.5.2 核心流程

`from_config` 的两种装配路径：

```text
enable_igw == true ? ─┬─ 多路由器模式（IGW）
                      │    register HTTP_REGULAR / GRPC_REGULAR / HTTP_PD / GRPC_PD / HTTP_OPENAI
                      │    （每个用 RouterFactory::create_* 创建，失败仅 warn 不中断）
                      │
                      └─ 单路由器模式
                           RouterFactory::create_router(app_context)  # 按 mode+connection_mode 选唯一一个
                           determine_router_id → 注册为默认
```

关键点：

1. **多路由器模式下「容错」**：每个路由器创建用 `match`，失败只 `warn!` 不 `return Err`；因此某一种路由器建不出来（例如 gRPC 依赖缺失）不会拖垮整个网关。
2. **单路由器模式下「必成」**：`create_router(...).await?` 用 `?`，建不出来就直接让 `startup` 报错退出。
3. **不依赖 worker 已注册**：路由器在构造时只持有 `worker_registry` 的引用，**真正选 worker 发生在每次请求时**，所以 `from_config` 可以在 0 个 worker 的情况下成功（呼应 4.4「提交即返回」的异步设计）。

#### 4.5.3 源码精读

`RouterManager` 的结构：[src/routers/router_manager.rs:62-L68](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/routers/router_manager.rs#L62-L68) —— 它持有 `worker_registry`、一个 `DashMap<RouterId, Arc<dyn RouterTrait>>`（路由表）、一个 `ArcSwap<Vec<...>>`（无锁快照，供请求路径快速读取）、一个可写的 `default_router`，以及 `enable_igw` 标志。

`from_config` 的入口与分支：[src/routers/router_manager.rs:81-L89](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/routers/router_manager.rs#L81-L89) —— 先 `Self::new(worker_registry)` 建空壳，把 `enable_igw` 从配置赋上，包成 `Arc`；随后用 `if config.router_config.enable_igw { ... } else { ... }` 走两条装配路径。

多路由器（IGW）模式的容错装配：[src/routers/router_manager.rs:91-L168](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/routers/router_manager.rs#L91-L168) —— 逐个 `RouterFactory::create_regular_router` / `create_grpc_router` / `create_pd_router` / `create_grpc_pd_router` / `create_openai_router`，每个用 `match` 把 `Ok` 的 `register_router`、`Err` 的只 `warn!`；末尾打印「initialized with N routers」。

单路由器模式：[src/routers/router_manager.rs:168-L172](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/routers/router_manager.rs#L168-L172) —— `RouterFactory::create_router(app_context).await?`（注意 `?`：失败即终止启动），再用 `determine_router_id` 根据 `mode` + `connection_mode` 决定它是哪种 `RouterId`。

回到 `startup` 里把它接上：[src/server.rs:914-L915](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L914-L915) —— `let router_manager = RouterManager::from_config(&config, &app_context).await?;`，随后 `let router: Arc<dyn RouterTrait> = router_manager.clone();`，这个 `router` 最终进入 `AppState.router`（[src/server.rs:983-L990](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L983-L990)），成为每个 HTTP 处理函数调用的对象。

#### 4.5.4 代码实践（源码阅读型）

1. 实践目标：对比「容错装配」与「必成装配」两种风格。
2. 操作步骤：在 [src/routers/router_manager.rs:91-L168](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/routers/router_manager.rs#L91-L168) 数一数有多少处 `warn!("Failed to create ...")`，再对比单路由器分支 [src/routers/router_manager.rs:168-L172](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/routers/router_manager.rs#L168-L172) 的 `?`。
3. 需要观察的现象：IGW 模式下某一种路由器建不出时，网关仍能起来并提供其余路由能力；单路由器模式下任何失败都会让进程退出。
4. 预期结果：你会理解为什么生产多模型部署偏好 IGW 模式——它的启动更具弹性。
5. 运行结果：待本地验证。

#### 4.5.5 小练习与答案

- 练习 1：`from_config` 在 0 个 worker 注册成功时会不会失败？
  - 答案：不会。路由器构造只持有 `worker_registry` 引用，选 worker 延后到请求时；`from_config` 不查询「当前有没有 worker」。就绪判定由 `/readiness` 在请求时做（[src/server.rs:102-L144](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L102-L144)）。
- 练习 2：`ArcSwap<Vec<Arc<dyn RouterTrait>>>`（`routers_snapshot`）存在的意义是什么？
  - 答案：给请求热路径提供无锁读取。路由表注册/移除时更新 `DashMap`，再原子地刷新快照；请求转发只读快照，避免每次请求都加锁查 `DashMap`。

---

## 5. 综合实践

> 本实践对应任务规格：在 `startup` 中插入两行 `tracing::info`，分别在 `AppContext` 创建前后输出，运行后对比日志时间线，解释初始化顺序的必要性。

**实践目标**：用日志时间戳亲手验证 4.2 的初始化顺序，并回答「为什么 `JobQueue` 与 `WorkflowEngines` 必须在 `AppContext` 之后、启动作业之前就绪」。

**操作步骤**：

1. 在 [src/server.rs:792-L807](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L792-L807) 这段里，在 `let app_context = Arc::new(AppContext::from_config(...))` **之前**插入：

   ```rust
   info!("[startup-timeline] BEFORE AppContext::from_config");
   ```

   在它**之后**（[src/server.rs:809](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L809) 那行 `let weak_context = ...` 之前）插入：

   ```rust
   info!("[startup-timeline] AFTER  AppContext::from_config");
   ```

2. 用 release profile 编译运行（参考 u1-l2）：`cargo run --release -- launch --worker-urls http://127.0.0.1:9000 --port 30000`（用一个本地占位 worker 地址即可，目的是观察启动日志，不必真的连上）。
3. 保留默认 `--log-level info`，让两行 info 都能输出。

**需要观察的现象**：

- 日志里应能看到 `BEFORE` 出现在 `AFTER` 之前，且二者之间夹着 `AppContext::from_config` 内部各 builder 步骤的 `debug!`（若临时把日志级别调到 debug 可见）。
- `AFTER` 之后才会依次出现「Loading startup tokenizer」「Startup tokenizer job submitted」「Worker initialization job submitted」等（对应 [src/server.rs:839](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L839)、[src/server.rs:864](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L864)、[src/server.rs:885](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L885)）。
- 「Router ready」([src/server.rs:1019-L1022](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L1019-L1022)) 出现时，紧邻的 `worker_registry.stats()` 行很可能显示 `healthy=0`——因为 worker 注册在后台异步进行。

**预期结果（用顺序解释必要性）**：

1. **为什么 AppContext 必须先建**：`JobQueue::new` 需要 `Arc::downgrade(&app_context)`（[src/server.rs:810](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L810)），没有 `Arc<AppContext>` 就拿不到弱引用——这是 4.3 的鸡生蛋问题。
2. **为什么 JobQueue 与 WorkflowEngines 必须在提交作业前 set 进 OnceLock**：`execute_job` 在执行 `AddTokenizer` / `AddWorker` 时会 `context.workflow_engines.get().ok_or("Workflow engines not initialized")`（[src/core/job_queue.rs:297-L300](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/job_queue.rs#L297-L300)），在 `InitializeWorkersFromConfig` 里会 `context.worker_job_queue.get()` 来扇出 `AddWorker`（[src/core/job_queue.rs:626-L636](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/job_queue.rs#L626-L636)）。两个盒子若未填值，作业就会失败。
3. **为什么「Router ready」时 worker 可能未就绪不是 bug**：worker 注册是「提交即返回」的后台作业（4.4），`/readiness` 才在请求时真正判定（[src/server.rs:102-L144](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L102-L144)）。

> 说明：以上涉及运行的步骤均**待本地验证**；若你不想改动源码，也可以仅阅读日志字符串出现顺序，得到同样的时间线结论。

## 6. 本讲小结

- `main()` 极薄，靠所有权移交把启动责任全部交给异步的 `server::startup()`；`validate()` 在进 `startup` 前已完成。
- `startup` 是一条顺序敏感的装配流水线：日志/追踪 → 指标 → Mesh → **AppContext** → **JobQueue/WorkflowEngines 填入 OnceLock** → **三类启动作业提交** → **RouterManager** → 健康/负载/限流 → 服务发现 → bind+serve。
- `AppContext` 的 `worker_job_queue` / `workflow_engines` / `mcp_manager` 三个 `OnceLock` 字段实现了「先占位、后填值」的惰性初始化，解开了「`JobQueue` 需要 `Weak<AppContext>` 而 `AppContext` 又要先存在」的循环依赖。
- `JobQueue` 用 `mpsc + 信号量（默认 1000/200）` 的分发模型，`submit` 只 `send` 到通道即返回；启动期提交 tokenizer → worker → MCP 三类作业，且 `InitializeWorkersFromConfig` 会扇出为多个 `AddWorker`。
- 因此网关能在 worker 尚未全部注册时就对外 bind 服务，真正的就绪判定在 `/readiness`。
- `RouterManager::from_config` 按 `enable_igw` 走「多路由器容错装配」或「单路由器必成装配」，且不依赖 worker 已注册。

## 7. 下一步学习建议

- **u2-l4（AppContext 共享状态）**：本讲只点了 `AppContext` 的三个 `OnceLock` 字段，下一篇会完整展开它的全部 `Arc` 字段、克隆语义与 builder 装配，建议紧接阅读。
- **u2-l5（Axum 应用与路由表）**：本讲的 `build_app` 被一笔带过，下一篇会拆解 public/protected/admin/worker/mesh 路由组与三层中间件，以及 `/readiness` 的就绪判定。
- **u3-l4（JobQueue 异步作业队列）**：本讲只关注「启动期提交了什么」，执行侧的并发模型、状态机、TTL 清理留到控制面单元深入。
- **u3-l5（Workflow 引擎与注册步骤）**：`WorkflowEngines::new` 在本讲只是「被 set 进盒子」，下一篇会拆解 worker 注册工作流的多步链路。
- 建议带着本讲的「初始化时间线」去读 u2-l4/u2-l5，能把「装配顺序」和「路由分层」两层视图拼成完整的启动图景。
