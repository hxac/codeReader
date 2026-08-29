# 一切的起点：Runtime 与 DistributedRuntime

## 1. 本讲目标

从本讲开始，我们离开 Python 包装层，直接进入 Dynamo 的 Rust 核心 `lib/runtime`。本讲结束后，你应该能够：

1. 说清 `Runtime` 与 `DistributedRuntime` 的职责边界：一个管**本机资源**（tokio 线程池、取消令牌、计算池），一个管**集群资源**（服务发现、NATS/ZMQ 连接、请求面网络、组件注册表）。
2. 亲手配置不同的 `DiscoveryBackend`（kubernetes / etcd / file / mem）与 `RequestPlaneMode`（tcp / nats），理解这两个开关是**正交**的。
3. 通过 `DYN_*` 环境变量覆盖 `RuntimeConfig` 的默认值，并能说出配置合并的优先级顺序。
4. 解释 `RuntimeConfig::tokio_builder()` 这个新拆出的公开方法为什么存在：**同一份 builder 既喂给本地 `create_runtime()`，也喂给 pyo3 异步桥**，保证 `DYN_RUNTIME_*` 对两条构建路径同时生效。
5. 描述「进程级唯一 Tokio 运行时」的保证机制（`Worker` 的 `RT` / `RTCONFIG` / `COMPUTE_CLAIMED` 三件套），以及为什么计算池一个进程只附挂一次。
6. 独立运行 Rust 版 `hello_world` 示例（server + client），并观察「client 找不到服务」时的真实代码路径。

## 2. 前置知识

本讲需要一点 Rust 异步编程的基础概念，不熟悉也没关系，先建立直觉：

- **tokio 异步运行时**：Rust 的异步代码（`async fn`）本身不会跑起来，需要一个「运行时」来调度执行。`tokio::runtime::Runtime` 就是那个调度器，内部管理着一组操作系统线程。
- **多线程 vs 单线程运行时**：多线程运行时（`new_multi_thread`）有 N 个工作线程并行调度任务；单线程运行时只有一个线程。`Handle` 是指向某个运行时的「遥控器」，拿到 Handle 就能往那个运行时上提交任务，即使运行时是在别处创建的。`tokio::runtime::Builder` 是「还没 build 的运行时图纸」——可以先调参数，之后再 `build()` 成真运行时。
- **CancellationToken（取消令牌）**：一个可克隆的通知开关。调用 `cancel()` 后，所有持有该令牌（或其子令牌）的代码都能感知「该停下来了」。Dynamo 用它实现优雅关停和请求取消（u2-l3 已见过它在请求链路上的用法）。
- **服务发现（Service Discovery）**：分布式系统的「电话簿」。worker 启动时把自己的地址登记上去，client 查这本簿子找到 worker。etcd、Kubernetes API、甚至一个本地目录，都可以充当这本簿子。
- **配置分层**：Dynamo 用 [figment](https://docs.rs/figment) 库合并配置：代码默认值 → TOML 文件 → 环境变量，后层覆盖前层。这和常见的十二要素应用（12-factor app）配置理念一致。
- **承接前讲**：u1-l3 讲过仓库三层结构（`lib/` Rust 核心 / `components/` Python / `deploy/` K8s），u1-l4 留过一个伏笔——`import dynamo._core` 即初始化、并按 `DYN_RUNTIME_*` 预设 pyo3 桥的 Tokio 运行时规模；u2-l2 讲过 Python 里的 `dynamo.runtime.DistributedRuntime` 其实是 PyO3 暴露的 Rust 结构体。本讲下到 Rust 侧看这些结构体本来的样子，并回收 u1-l4 的伏笔。

一个先记住的比喻：**`Runtime` 是「这台机器上的发动机」，`DistributedRuntime` 是「装上发动机的整车」**——发动机只管提供动力（线程），整车才连得上公路网络（服务发现与消息传输）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [lib/runtime/src/runtime.rs](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/runtime.rs) | `Runtime` 结构体：包装 tokio 运行时、取消令牌、计算线程池与优雅关停跟踪；新增 `from_handle_with_config` |
| [lib/runtime/src/distributed.rs](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/distributed.rs) | `DistributedRuntime` 与 `DistributedConfig`：集群通信、发现后端选择、请求面模式 |
| [lib/runtime/src/config.rs](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/config.rs) | `RuntimeConfig`：线程数、系统状态服务端口、健康检查等本机配置，figment 分层加载；新增公开的 `tokio_builder()` |
| [lib/runtime/src/worker.rs](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/worker.rs) | `Worker`：`main()` 与 `Runtime` 之间的便捷封装；持有进程级唯一运行时（`RT`/`RTCONFIG`/`COMPUTE_CLAIMED`）、SIGINT/SIGTERM 与优雅关停超时 |
| [lib/runtime/tests/process_runtime.rs](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/tests/process_runtime.rs) | 新增集成测试：验证「第一个 Runtime wrapper 拿计算池、后续 wrapper 复用同一 Tokio 运行时且不再建池」 |
| [lib/runtime/src/storage/kv.rs](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/storage/kv.rs) | `kv::Selector`：etcd / file / mem 三种 KV 存储的选择器（发现后端的底层载体） |
| [lib/runtime/examples/hello_world/src/bin/server.rs](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/examples/hello_world/src/bin/server.rs) | Rust 版最小 server：注册 `dynamo.backend.generate` 端点 |
| [lib/runtime/examples/hello_world/src/bin/client.rs](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/examples/hello_world/src/bin/client.rs) | Rust 版最小 client：等实例上线、逐帧收流 |
| [lib/runtime/examples/Cargo.toml](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/examples/Cargo.toml) | 注意：examples 目录是**独立的 Cargo workspace**，不属于仓库根 workspace |

另外会顺带引用 `lib/runtime/src/component/client.rs`（`wait_for_instances` 的等待逻辑）和 `lib/bindings/python/rust/lib.rs`（Python 侧入口，本讲重点看它如何消费 `tokio_builder()`），用于打通前两讲。

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：**Runtime** → **RuntimeConfig（含 tokio_builder）** → **DistributedRuntime** → **DiscoveryBackend 与 RequestPlaneMode** → **hello_world 启动链（含进程级运行时）**。

### 4.1 Runtime：节点本地的共享资源容器

#### 4.1.1 概念说明

`Runtime` 解决的问题是：**一个进程内的所有组件需要共享同一批本机资源**——线程池、取消令牌、CPU 密集计算池。如果每个组件各自建线程池，进程会迅速失控。所以 Dynamo 把这些资源收敛到一个对象里，谁需要谁克隆（`Runtime` 是 `Clone` 的，内部全是 `Arc`）。

文件头部的文档注释说得很直白：`Runtime` 是 `Component` 访问共享资源（线程池、内存分配器等）的接口，并持有用于终止所有挂载组件的主 `CancellationToken`（[runtime.rs:4-11](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/runtime.rs#L4-L11)）。

关键设计：`Runtime` 有**主（primary）**和**副（secondary）**两个线程池引用：

- **primary**：跑应用主逻辑（HTTP 服务、引擎生成）。
- **secondary**：跑后台任务（etcd 保活、NATS 订阅等），避免后台任务的阻塞拖慢主逻辑。

#### 4.1.2 核心流程

`Runtime` 的构造有四条入口，形成如下关系：

```text
Runtime::from_settings()          ← 最常用：读环境变量，新建 tokio 多线程运行时
Runtime::from_current()           ← 复用当前线程已存在的 tokio 运行时
Runtime::from_handle(h)           ← 只用 Handle 包装外部运行时（不带配置附属物）
Runtime::from_handle_with_config(h, &config) ← 同上，但按 config 附挂计算池与许可（新增）
Runtime::single_threaded()        ← 测试用：单线程运行时

所有入口最终汇聚到 Runtime::new(primary, Option<secondary>)
from_settings 与 from_handle_with_config 再经 new_with_config 附挂配置派生资源
```

`Runtime::new` 的执行步骤：

1. 初始化 NVTX（NVIDIA 性能标注，非 GPU 环境是空操作）。
2. 生成一个 UUID 作为本 Runtime 的 `id`。
3. 创建主取消令牌 `cancellation_token`，再派生一个子令牌 `endpoint_shutdown_token`——**先停端点收新请求，再停全局**，这是优雅关停的基础。
4. 若调用方没给 secondary，就新建一个单线程运行时充当 secondary。
5. 装配 `GracefulShutdownTracker`（统计还有多少端点没收完尾）。

`Runtime::new_with_config` 在 `new` 的基础上再做两件事（[runtime.rs:108-158](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/runtime.rs#L108-L158)）：

1. 按配置的 `compute_threads`/`compute_stack_size`/`compute_thread_prefix` 创建 **Rayon 计算池**（`compute_threads == Some(0)` 显式禁用；创建失败只告警、退化为 `spawn_blocking`）。
2. 按工作线程数初始化 **`block_in_place` 许可信号量**：`permits = max(num_workers - 1, 1)`，即至少给异步工作留一个线程。

优雅关停 `Runtime::shutdown()` 分三个阶段：

```text
Phase 1: 取消 endpoint_shutdown_token → 各端点停止接收新请求
Phase 2: 等待所有登记在册的端点处理完在途请求
         （上限 DYN_RUNTIME_GRACEFUL_SHUTDOWN_TIMEOUT_SECS，默认 15 分钟）
Phase 3: 取消主 cancellation_token → 断开 NATS/etcd 等后端连接
```

#### 4.1.3 源码精读

先看结构体定义——注意字段里没有「集群」相关的任何东西，全是本机资源：

[runtime.rs:53-64](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/runtime.rs#L53-L64) 定义 `Runtime`：持有 UUID 形式的 `id`、主/副两个 `RuntimeType`、两个取消令牌、优雅关停跟踪器和可选的计算线程池与 `block_in_place` 许可信号量。

其中 `RuntimeType` 是个两变体枚举，用来区分「我自己拥有的运行时」和「借来的外部运行时」：

[runtime.rs:46-51](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/runtime.rs#L46-L51) 定义 `RuntimeType::Shared`（包在 `Arc<ManuallyDrop<...>>` 里自己持有）与 `RuntimeType::External`（只保存别人运行时的 `Handle`）。

最常用的构造入口是 `from_settings`：

[runtime.rs:286-292](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/runtime.rs#L286-L292)：`Runtime::from_settings()` 先用 `RuntimeConfig::from_settings()` 读环境变量得到配置，再 `config.create_runtime()` 建出 tokio 运行时；注意此处 **primary 是 Shared（拥有所有权），secondary 是 External（同一运行时的 Handle 克隆）**——也就是说这条路径下主副池实际是同一个 tokio 运行时；最后走的是 `Runtime::new_with_config(..., &config)`，把计算池和 `block_in_place` 许可一并附挂上。

本讲更新新增的第四条入口，专供「运行时在别处持有、只能借 Handle，但其配置已知」的场景：

[runtime.rs:270-282](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/runtime.rs#L270-L282)：`Runtime::from_handle_with_config(handle, config)` 与 `from_handle` 一样把 primary/secondary 都设为 `External`，但最后调用 `Runtime::new_with_config` 附挂 `config` 所隐含的计算池与 `block_in_place` 许可。它的文档注释点明动机：Tokio 运行时可能被进程级 `OnceCell` 持有（见 4.5），此时只能借 Handle。这个方法正是 `Worker::runtime_from_existing()` 给「第一个 wrapper」附挂计算池的途径。

只有当调用方不提供 secondary 时，才会新建一个独立的单线程运行时：

[runtime.rs:81-89](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/runtime.rs#L81-L89)：`Runtime::new` 中 `secondary` 为 `None` 的分支，用 `RuntimeConfig::single_threaded().create_runtime()` 创建单线程运行时并打出日志 "Created secondary runtime with single thread"。目前这条分支主要由 `Runtime::single_threaded()`（[runtime.rs:295-299](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/runtime.rs#L295-L299)，测试场景）触达。

关停逻辑的三阶段实现：

[runtime.rs:339-379](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/runtime.rs#L339-L379)：`shutdown()` 在取消任何令牌**之前**先往 primary 上 spawn 一个协调任务；任务里 Phase 1 取消 endpoint 令牌（[runtime.rs:352](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/runtime.rs#L352)），Phase 2 在超时上限内等待 `GracefulShutdownTracker` 清零（[runtime.rs:360-373](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/runtime.rs#L360-L373)），Phase 3 才取消主令牌断开后端连接（[runtime.rs:377](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/runtime.rs#L377)）。超时上限来自环境变量 `DYN_RUNTIME_GRACEFUL_SHUTDOWN_TIMEOUT_SECS`，默认 15 分钟（[runtime.rs:33-44](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/runtime.rs#L33-L44)）。

最后一个值得精读的点是 `Drop` 实现，它解释了 u2-l2 提过的 Python 交互怪象：

[runtime.rs:392-430](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/runtime.rs#L392-L430)：tokio 不允许在异步上下文里 drop 一个运行时（会 panic，且 panic 吞掉最后的日志）。因此 `RuntimeType` 用 `ManuallyDrop` 包住运行时；drop 时若检测到当前正处于异步上下文（`Handle::try_current()` 成功），就改用 `shutdown_background()` 让运行时在线程池上自行回收，否则才真正 drop。注释坦言这是因为 pyo3/Python 生命周期的问题。

#### 4.1.4 代码实践

**实践目标**：直观感受「Runtime = 线程池的集合 + 配置派生资源」，并用环境变量改变它。

**操作步骤**（示例命令，待本地验证）：

1. 进入示例的独立 workspace（原因见 4.5.3）：

   ```bash
   cd lib/runtime/examples
   cargo build -p hello_world
   ```

2. 用单工作线程启动 server，并打开 debug 日志观察 Runtime 初始化信息：

   ```bash
   DYN_DISCOVERY_BACKEND=file \
   DYN_RUNTIME_NUM_WORKER_THREADS=1 \
   RUST_LOG=dynamo_runtime=debug \
   cargo run -p hello_world --bin server
   ```

3. 另开一个终端，只看启动阶段日志（`Ctrl+C` 退出即可）。

**需要观察的现象**：

- 日志中是否出现 `Initialized block_in_place permits: 1 (from 1 worker threads)`——线程数被环境变量改成了 1。
- 相比之下，不加 `DYN_RUNTIME_NUM_WORKER_THREADS` 再跑一次，该数字应变成机器核数减 1（保底 1）。

**预期结果**：`DYN_RUNTIME_NUM_WORKER_THREADS=1` 时 permits 为 1；默认时等于 CPU 核数减 1（保底 1，见 [runtime.rs:144-155](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/runtime.rs#L144-L155)）。注意 `hello_world` 走 `Worker::from_settings → from_config` 路径，计算池**不会**被附挂（见 4.5.3），所以这条日志来自 `from_config` 内部的 `Runtime::new` 默认值……如果看不到该日志，说明路径判断需要修正——这本身就是一次很好的源码追踪练习。具体日志文本**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Runtime` 要区分 `Shared` 和 `External` 两种 `RuntimeType`？

**参考答案**：`External` 只保存外部运行时的 `Handle`，不负责生命周期，适合「复用已有运行时」的场景（比如 Python 进程里 pyo3 已经建好了 tokio 运行时，或测试里复用 `#[tokio::test]` 的运行时）；`Shared` 拥有运行时的所有权，需要在 Drop 时正确回收，而回收方式又受异步上下文限制（见上面的 `Drop` 实现）。

**练习 2**：`Runtime::from_settings()` 路径下，primary 和 secondary 是两个不同的 tokio 运行时吗？

**参考答案**：不是。`from_settings` 里 secondary 是 `RuntimeType::External(runtime.handle().clone())`，与 primary 指向**同一个**多线程运行时（[runtime.rs:286-292](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/runtime.rs#L286-L292)）。只有 `Runtime::new(_, None)` 不传 secondary 时才会另建一个单线程运行时（例如 `single_threaded()`）。

**练习 3**：Phase 2 等待的「优雅端点」如果永远不结束，会发生什么？

**参考答案**：超过 `DYN_RUNTIME_GRACEFUL_SHUTDOWN_TIMEOUT_SECS`（默认 900 秒）后打 error 日志 "Graceful endpoint shutdown timed out; proceeding with runtime teardown"，然后**不再等待**，直接进入 Phase 3 取消主令牌、断开 NATS/etcd 连接（[runtime.rs:360-377](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/runtime.rs#L360-L377)）。该行为有单测覆盖：`shutdown_cancels_main_token_after_graceful_timeout`（[runtime.rs:437-472](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/runtime.rs#L437-L472)，用 `start_paused = true` 的虚拟时钟推进 5 秒验证主令牌恰好在该时刻被取消）。

### 4.2 RuntimeConfig：环境变量驱动的配置层与 tokio_builder

#### 4.2.1 概念说明

`RuntimeConfig` 是 `Runtime` 的「图纸」：线程数、系统状态服务（健康检查 + 指标）的地址端口、健康检查参数、计算池参数。它最大的特点是**完全由环境驱动**——`from_settings()` 不接受任何参数，一切从 figment 分层里读：

```text
优先级从高到低（高层覆盖低层）：
1. 环境变量   DYN_RUNTIME_* / DYN_SYSTEM_* / DYN_COMPUTE_* / DYN_HEALTH_CHECK_* / DYN_CANARY_*
2. TOML 文件 /opt/dynamo/etc/runtime.toml
3. TOML 文件 /opt/dynamo/defaults/runtime.toml
4. 代码内默认值 RuntimeConfig::default()
```

为什么要有两层 TOML？容器镜像里 `/opt/dynamo/defaults/` 放「镜像出厂值」，`/opt/dynamo/etc/` 放「部署时可改的值」——这也是 u1-l4 讲过的容器模板体系的一环。

本讲更新为这份图纸增加了一个公开出口：`tokio_builder()`。它回答一个此前没有好答案的问题——**「配置除了能 build 出一个本地运行时，还能不能约束别人自己 build 的运行时？」**答案是：把「还没 build 的图纸」本身交出去（详见 4.2.3）。

#### 4.2.2 核心流程

figment 合并有两个容易忽略的细节：

1. **空环境变量被过滤**。每个 `Env::prefixed(...)` 都套了 `filter_map`：读到的值为空字符串就当没设置。这是为了照顾 K8s 里 `env:` 置空等场景，避免空串把合法默认值冲掉。
2. **键名重映射**。环境变量名和字段名并不一致（如 `DYN_SYSTEM_HOST` → 字段 `system_host`），映射表就写在 `filter_map` 里。

伪代码：

```text
figment() =
    defaults(RuntimeConfig::default())
  + toml("/opt/dynamo/defaults/runtime.toml")
  + toml("/opt/dynamo/etc/runtime.toml")
  + env("DYN_RUNTIME_")   # 键名与字段基本同名，如 NUM_WORKER_THREADS → num_worker_threads
  + env("DYN_SYSTEM_")    # HOST→system_host, PORT→system_port, ...
  + env("DYN_COMPUTE_")   # THREADS→compute_threads, ...
  + env("DYN_HEALTH_CHECK_") / env("DYN_CANARY_")

from_settings() = figment().extract() 后再 validate()

构建运行时的两条消费路径（本讲更新的核心）：
  路径 A（本地）:   config.create_runtime()     = config.tokio_builder().build()
  路径 B（pyo3 桥）: pyo3_async_runtimes::tokio::init(config.tokio_builder())
                     —— 桥拿到 builder，稍后自己 build()
两条路径都经过 tokio_builder()，配置语义不会漂移
```

#### 4.2.3 源码精读

`RuntimeConfig` 的字段清单（节选关键部分）——每个字段的文档注释都写明了对应的环境变量，是查配置名的权威位置：

[config.rs:75-96](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/config.rs#L75-L96)：`RuntimeConfig` 定义 `num_worker_threads`（`DYN_RUNTIME_NUM_WORKER_THREADS`，默认取核数）、`max_blocking_threads`（`DYN_RUNTIME_MAX_BLOCKING_THREADS`）。后者在本次更新中**重写了文档注释**，明确三层含义：(1) `impl Default` 的默认值是 **CPU 核数**；(2) 字段上 `#[builder(default = "512")]` 的 512 **只**在通过 `RuntimeConfigBuilder` 构建且未设置该字段时生效；(3) 它是**上限而非预分配**——tokio 按需生成阻塞线程、空闲时回收，测量应在稳态负载下进行。其余字段（`system_host`/`system_port` 等）延续至 [config.rs:98-185](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/config.rs#L98-L185)。

> 旧版讲义曾把「两条构造路径默认值不同」当作源码阅读的坑（512 vs 核数）；现在源码注释已把这个坑写明白，`Default` 实现也可直接对照：[config.rs:403-425](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/config.rs#L403-L425) 中 `max_blocking_threads: num_cores`（第 408 行）。

figment 的分层合并与空值过滤：

[config.rs:227-239](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/config.rs#L227-L239)：`figment()` 先合并 `RuntimeConfig::default()`，再依序合并两个 `/opt/dynamo/` 下的 TOML，随后合并 `DYN_RUNTIME_` 前缀的环境变量；`filter_map` 中 `Ok(v) if !v.is_empty()` 的分支保证空字符串环境变量不会参与合并。

`DYN_SYSTEM_*` 的键名映射表：

[config.rs:240-260](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/config.rs#L240-L260)：把 `HOST`、`PORT`、`ENABLED`、`USE_ENDPOINT_HEALTH_STATUS` 等环境变量段映射到 `system_host`、`system_port` 等结构体字段名。

入口函数与废弃变量告警：

[config.rs:319-341](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/config.rs#L319-L341)：`from_settings()` 先对两个已废弃变量（`DYN_SYSTEM_USE_ENDPOINT_HEALTH_STATUS`、`DYN_SYSTEM_ENABLED`）打 warning，再 `extract()` 并 `validate()`（例如线程数必须 ≥ 1，见单测 [config.rs:665](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/config.rs#L665) 起的 `test_runtime_config_rejects_invalid_thread_count`）。

**配置如何变成 tokio 运行时——本讲更新的核心**。旧版这里只有一个 `create_runtime()`；现在拆成了「出图纸」和「按图纸施工」两步：

[config.rs:372-395](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/config.rs#L372-L395)：`tokio_builder()` 是新拆出的**公开**方法，返回尚未 build 的 `tokio::runtime::Builder`：`new_multi_thread()` + `worker_threads`（未设置时取 `available_parallelism()` 即 CPU 核数）+ `max_blocking_threads` + `enable_all()`；若 `DYN_ENABLE_POLL_HISTOGRAM` 为真还会开启任务轮询耗时直方图（并提示约两倍 `Instant::now()` 开销）。文档注释直接解释了拆分原因：**pyo3 桥自建运行时**——`pyo3_async_runtimes::tokio::init` 接收的是 builder、稍后自己调 `build()`，把这份 builder 递过去是**约束那个运行时规模的唯一途径**；两条路径都经过这里，所以不会漂移。

[config.rs:397-400](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/config.rs#L397-L400)：`create_runtime()` 退化为一行 `self.tokio_builder().build()`，仍是 `pub(crate)`——crate 外想「按 Dynamo 配置施工」只能拿 builder，不能拿现成 runtime。

本次更新还配套补了一组单测，把「配置真的传到运行时」钉死：

- [config.rs:555](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/config.rs#L555) `test_from_settings_reads_both_thread_env_vars`：`DYN_RUNTIME_NUM_WORKER_THREADS=7`、`DYN_RUNTIME_MAX_BLOCKING_THREADS=11` 必须同时穿过 `from_settings()`——注释点明：若某个 frontend 的线程数不理会后者，病因在接线而非解析。
- [config.rs:572](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/config.rs#L572) `test_tokio_builder_applies_configured_worker_threads`：`tokio_builder().build()` 出来的运行时 `num_workers()` 必须等于配置值——注释还原了原始 bug：桥自建的运行时会悄悄退回「每 CPU 一个 worker」，而这是其他测试都看不到的角落。
- [config.rs:585](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/config.rs#L585) 与 [config.rs:611](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/config.rs#L611)：分别验证「未设置时回落核数」与「`max_blocking_threads` 真的会封顶并发阻塞任务」（后者用 `spawn_blocking` + 原子计数器测峰值并发，因为阻塞线程数在稳定版 tokio 上无法直接读取）。
- [config.rs:657](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/config.rs#L657) `test_default_max_blocking_threads_is_core_count`：把「Default 是核数、512 只属于 builder 链」写进断言。

#### 4.2.4 代码实践

**实践目标**：用 `DYN_SYSTEM_PORT` 打开系统状态服务，并用新增单测验证线程环境变量确实生效。

**操作步骤**（待本地验证）：

```bash
cd lib/runtime/examples
DYN_DISCOVERY_BACKEND=file DYN_SYSTEM_PORT=8081 \
  cargo run -p hello_world --bin server
# 另一终端
curl http://127.0.0.1:8081/health
```

再在仓库根跑（不需要任何外部服务）：

```bash
cargo test -p dynamo-runtime --lib config::tests
```

**需要观察的现象**：启动日志出现 "System status server started successfully"；`curl` 返回健康状态；单测输出包含 `test_from_settings_reads_both_thread_env_vars`、`test_tokio_builder_applies_configured_worker_threads` 等新增用例且全部通过。

**预期结果**：`system_server_enabled()` 判定为端口 ≥ 0（[config.rs:347-349](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/config.rs#L347-L349)），于是 `DistributedRuntime::new` 会拉起系统状态服务（见 4.3.3）。健康响应的具体 JSON 结构**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：把 `DYN_SYSTEM_PORT=""`（空字符串）和 `DYN_SYSTEM_PORT=-1` 分别设置，行为有区别吗？

**参考答案**：有。空字符串会被 `filter_map` 过滤掉（等于没设置，回落到默认 -1 禁用）；而 `-1` 是显式设置的合法值，同样禁用服务。两者最终效果相同但路径不同——前者根本没进 figment，后者进去了。若设成 `abc` 这种非法值，`extract()` 反序列化会报错。

**练习 2**：为什么 `tokio_builder()` 是 `pub` 而 `create_runtime()` 仍是 `pub(crate)`？

**参考答案**：消费方分两类。crate 内部（`Runtime::from_settings`、`Worker`）要的是「建好的运行时」，走 `create_runtime()` 即可；crate 外部（pyo3 绑定 crate dynamo-py3）面对的是**不接受现成 runtime 的 API**——`pyo3_async_runtimes::tokio::init` 只收 builder、自己延迟 build。所以公开的必须是「图纸」而不是「成品」；同时 `create_runtime` 内部也改为 `tokio_builder().build()`，两条路径共用同一份图纸，配置语义不可能漂移（[config.rs:372-400](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/config.rs#L372-L400)）。

**练习 3**：想给生产镜像统一改线程数，应该改环境变量还是改 `/opt/dynamo/etc/runtime.toml`？

**参考答案**：改 TOML 适合「镜像级默认」，改环境变量适合「每个部署覆盖」。因为环境变量优先级最高，TOML 改完后仍可被单实例的环境变量临时覆盖；两者可以配合使用。

### 4.3 DistributedRuntime：跨节点的通信与发现容器

#### 4.3.1 概念说明

`DistributedRuntime`（下称 DRT）在 `Runtime` 之上解决**集群维度**的问题：我是谁（连接 ID）、谁在线（服务发现）、消息走哪条路（请求面/事件面）、本进程有哪些组件（组件注册表）、我健康吗（SystemHealth）。

两个重要认知：

1. **DRT 不是进程单例**。源码注释明确说明：多次 `DistributedRuntime::new` 会创建彼此独立的实例，各有各的发现连接 ID；克隆（`Clone`）则共享原实例。生产上「一进程一 DRT」只是软约定，单进程多 DRT 主要为单进程测试拓扑和 mocker 服务（[distributed.rs:43-51](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/distributed.rs#L43-L51)）。
2. **DRT 持有 `Runtime` 但不暴露线程细节**。上层代码通过 `drt.primary_token()`、`drt.child_token()` 拿取消信号，通过 `drt.namespace("...")` 进入服务目录层级（namespace → component → endpoint，下一讲的主角）。

注意区分两个同名的 `new`：Rust 侧的 `rs::DistributedRuntime::new`（本节的装配函数）与 pyo3 绑定里 Python 调用的 `DistributedRuntime::new`（4.3.3 末尾，负责运行时交接）。本讲更新主要改了后者。

#### 4.3.2 核心流程

`DistributedRuntime::new(runtime, config)` 是一个大型装配函数，流程如下：

```text
输入: Runtime（本机资源） + DistributedConfig（发现后端/请求面/事件面/NATS）

1. dissolve 拆出四个配置分量
2. 若启用 NATS → 建立连接 nats_client
3. 再读一次 RuntimeConfig（系统服务是否开启、健康参数）
4. 按 DiscoveryBackend 建立发现客户端：
     Kubernetes        → KubeDiscoveryClient（K8s API，无需 KV 存储）
     KvStore(Etcd)     → etcd 客户端 + KVStoreDiscovery（默认，连不上直接报错）
     KvStore(File)     → 本地目录充当 KV（u1-l2 的零依赖模式）
     KvStore(Memory)   → 进程内存（process_local / 测试）
5. 建 component::Registry（本进程组件注册表，复用 watcher）
6. 建 NetworkManager（请求面收发）+ EndpointRegistrationManager（端点注册与租约）
7. 组装 Self（含 metrics_registry、system_health、local_endpoint_registry 等）
8. 若 DYN_SYSTEM_PORT ≥ 0 → 启动系统状态 HTTP 服务（/health、/live、指标）
9. 若开启主动健康检查 → 启动 health check manager
```

#### 4.3.3 源码精读

结构体字段全景——每一个字段对应一块集群能力：

[distributed.rs:52-99](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/distributed.rs#L52-L99)：`DistributedRuntime` 持有本机 `runtime`、可选 `nats_client`、请求面 `NetworkManager`、惰性初始化的 `tcp_server` 与 `system_status_server`、发现客户端 `discovery_client`（`Arc<dyn Discovery>` trait 对象）、端点注册管理器、K8s 专用 `discovery_metadata`、本进程 `component_registry`（注释解释了它的价值：两个指向同一远端组件的客户端共享同一个 etcd watcher，控制后台任务数量）、健康状态、进程内端点注册表、指标注册表等。

装配的第一步——拆配置、连 NATS：

[distributed.rs:126-133](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/distributed.rs#L126-L133)：`new` 用 `config.dissolve()` 拆出发现后端、NATS 配置、请求面模式和事件面类型；`nats_config` 为 `Some` 时立刻 `connect()`。

发现后端的选择逻辑（本讲最重要的 match 之一）：

[distributed.rs:156-192](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/distributed.rs#L156-L192)：`Kubernetes` 分支创建 `KubeDiscoveryClient`；`KvStore` 分支再按 `kv::Selector` 细分——`Etcd` 时新建 etcd 客户端，失败会打出关键报错 "Could not connect to etcd. Pass `--discovery-backend ..` to use a different backend or start etcd."（[distributed.rs:177-181](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/distributed.rs#L177-L181)），`File`/`Memory` 则分别建目录型与内存型 KV manager，最后统一包成 `KVStoreDiscovery`。

随后的三个组件注册/网络管理器：

[distributed.rs:194-208](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/distributed.rs#L194-L208)：创建 `component::Registry`、`NetworkManager`（用 `runtime.child_token()` 绑定生命周期）与 `EndpointRegistrationManager`（跑在 secondary 线程池上，负责端点注册与租约保活）。

系统状态服务的按需启动：

[distributed.rs:248-289](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/distributed.rs#L248-L289)：只有前面拿到 `cancel_token`（即 `system_server_enabled()` 为真）时才 `spawn_system_status_server`；成功则记录 "System status server started successfully"，失败只打 error 不中断启动。

进入服务目录层级的入口：

[distributed.rs:373-375](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/distributed.rs#L373-L375)：`namespace(name)` 只是简单地 `Namespace::new(self.clone(), name)`，把 DRT 自身塞进 Namespace——服务目录的每层都反向持有 DRT，随时能回到集群资源。

与 Python 世界的连接点（本讲更新改动最大的地方，回收 u1-l4 的伏笔）。分两段看：

**第一段：模块初始化时就给桥递图纸**。

[lib/bindings/python/rust/lib.rs:181-198](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/lib.rs#L181-L198)：`register_core`（即 `import dynamo._core` 时执行）在读到日志初始化后，立刻 `RuntimeConfig::from_settings()` 并调用 `pyo3_async_runtimes::tokio::init(config.tokio_builder())`——把 4.2 的那份 builder 交给异步桥。注释把动机讲得很透：DRT 的构造函数会把配置好的运行时交给桥，**但只有它先到才行**，而 `dynamo.sglang` 往往更早触达桥；那时桥的 `get_runtime()` 会按 tokio 自己的默认值自建运行时（每 CPU 一个 worker、512 阻塞线程上限），`DYN_RUNTIME_*` 完全被无视。模块初始化是我们的代码最早运行的时刻，在这里设置 builder 就能保证**无论谁构建、运行时尺寸都正确**。配置读取失败只 warn 不 fatal，把报错留给有上下文的 `Worker::ensure_process_runtime`。

**第二段：DRT 构造时的运行时交接**。

[lib/bindings/python/rust/lib.rs:1180-1227](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/lib.rs#L1180-L1227)：Python 侧 `DistributedRuntime.__new__` 解析完三个字符串参数后，先 `Worker::ensure_process_runtime()` 拿到进程级 `&'static` 运行时（[L1183](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/lib.rs#L1183)）；随后在 `INIT.get_or_init` 里尝试 `pyo3_async_runtimes::tokio::init_with_runtime(primary)` 把这个运行时交给桥（[L1184-L1201](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/lib.rs#L1184-L1201)）——若桥已经持有别的运行时（它从不交还），只要不是同一个就打 warn「进程里现在有两个运行时；两者都按 `DYN_RUNTIME_*` 定尺寸，所以配置描述的线程数翻倍」，**接受现状而非报错**（注释坦言：在这里拒绝会弄挂所有 sglang 测试）；再 `Worker::runtime_from_existing()` 把同一个 tokio 运行时包成 dynamo `Runtime`（[L1204](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/lib.rs#L1204)），最后在 secondary 线程池上 `block_on(rs::DistributedRuntime::new(...))` 走 4.3.2 的装配流程。u2-l1 里你写过的 `dynamo.runtime.DistributedRuntime(...)`，最终就是这一段。

#### 4.3.4 代码实践

**实践目标**：验证「DRT 非单例、克隆共享」这条注释，并观察模块初始化时的配置日志。

**操作步骤**（示例代码，基于 u2-l1 的环境，待本地验证）：

```python
# 文件: /tmp/drt_ids.py（示例代码，非仓库文件）
import asyncio
from dynamo.runtime import DistributedRuntime

async def main(loop):
    drt1 = DistributedRuntime(loop, "file", "tcp")
    drt2 = DistributedRuntime(loop, "file", "tcp")
    drt1_clone = drt1  # Python 侧克隆语义即共享

loop = asyncio.new_event_loop()
loop.run_until_complete(main(loop))
```

配合环境变量观察：

```bash
RUST_LOG=dynamo_runtime=info DYN_RUNTIME_NUM_WORKER_THREADS=4 \
  python /tmp/drt_ids.py
```

**需要观察的现象**：`import dynamo._core` 后、构造任何 DRT 之前，是否已出现一行 `dynamo runtime configuration: ...`（来自 4.5.3 的 `ensure_process_runtime`）；进程内构造两个 DRT 时初始化日志出现两遍（连接 ID 各自独立）。

**预期结果**：连接 ID 的概念在 Rust 侧由 `connection_id()` 提供（[distributed.rs:363-365](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/distributed.rs#L363-L365)，返回发现客户端的 `instance_id()`）。具体可观测的 Python 输出**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`component_registry` 的注释说它能让「两个指向同一远端组件的客户端共享同一个 watcher」，为什么这很重要？

**参考答案**：如果不共享，每个客户端都会各自起一个后台任务去 etcd/K8s watch 同一条路径，客户端数量增长时后台任务和 watch 连接数线性膨胀。注册表按服务名去重，把 watcher 数量压到与服务数同阶（[distributed.rs:71-76](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/distributed.rs#L71-L76)）。

**练习 2**：`DistributedRuntime::new` 里第 136 行又调用了一次 `RuntimeConfig::from_settings()`，这与外层 `Runtime` 的配置是什么关系？

**参考答案**：DRT 复用同一份环境变量配置来决定「系统状态服务是否启动、健康检查参数」等分布式层的可观测性行为（[distributed.rs:135-154](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/distributed.rs#L135-L154)）。注意它在 `unwrap_or_default()` 兜底——即使配置读取失败也能以默认值继续建 DRT。

**练习 3**：既然模块初始化已经把 builder 交给了桥，为什么 DRT 构造时还要再 `init_with_runtime` 一次？

**参考答案**：`pyo3_async_runtimes::tokio::init(builder)` 只提供「桥自建运行时时的图纸」；`init_with_runtime(&'static Runtime)` 才是「让桥直接用这个现成运行时」。理想情况是 DRT 先到：进程级运行时既服务 Dynamo 也服务桥，一份线程池干两份活。若桥已经抢先按图纸自建了，`init_with_runtime` 会失败，此时两个运行时都已按 `DYN_RUNTIME_*` 定尺寸——功能正确、代价是线程数翻倍，所以只 warn 不报错（[lib.rs:1184-1201](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/lib.rs#L1184-L1201)）。

### 4.4 DiscoveryBackend 与 RequestPlaneMode：两个正交的开关

#### 4.4.1 概念说明

初学者最容易把三件事混为一谈，这里明确区分——它们各自由独立的环境变量控制：

| 维度 | 问题 | 开关（环境变量） | 可选值 |
|------|------|------------------|--------|
| **发现后端** | 「谁在线」记在哪里？ | `DYN_DISCOVERY_BACKEND` | `kubernetes` / `etcd`（默认）/ `file` / `mem` |
| **请求面** | 请求报文走什么传输？ | `DYN_REQUEST_PLANE` | `tcp`（默认）/ `nats`（旧式） |
| **事件面** | KV 事件等消息走什么传输？ | `DYN_EVENT_PLANE` | `zmq`（默认）/ `nats` |

`DiscoveryBackend` 枚举只有两个变体：`Kubernetes`（直接用 K8s API，不需要额外 KV 存储）和 `KvStore(kv::Selector)`（用某种 KV 存储充当注册表，`Selector` 再细分 etcd/file/mem）。所谓 `etcd` 后端、`file` 后端只是 `KvStore` 的三种底层选择。

`RequestPlaneMode` 决定 router 到 worker 的请求分发传输：`Tcp` 是带 msgpack 支持的裸 TCP（默认，性能更好），`Nats` 是旧式方案，源码中 NATS 请求面相关方法已标注 DEPRECATED。

#### 4.4.2 核心流程

`DistributedConfig::from_settings()` 的解析流程：

```text
读 DYN_DISCOVERY_BACKEND（未设置则默认 "etcd"）
  ├─ "kubernetes" → DiscoveryBackend::Kubernetes
  └─ 其他字符串 → parse 成 kv::Selector
        ├─ "etcd" → Selector::Etcd(默认连接选项)   # 默认地址 localhost:2379，可用 ETCD_ENDPOINTS 覆盖
        ├─ "file"  → Selector::File(DYN_FILE_KV 或 $TMPDIR/dynamo_store_kv)
        ├─ "mem"   → Selector::Memory
        └─ 不认识  → panic!("Unknown DYN_DISCOVERY_BACKEND value ...")

解析事件面 DYN_EVENT_PLANE → EventTransportKind（"nats"/"zmq"，未设或非法值都回落 Zmq）

决定是否启用 NATS 客户端（三者满足其一）:
  请求面是 NATS || 环境里设置了 NATS_SERVER || 事件面是 NATS

读 DYN_REQUEST_PLANE → RequestPlaneMode（解析失败回落默认 Tcp）
```

要点：**用 `file` 后端 + 默认 tcp 请求面 + 默认 zmq 事件面时，NATS 完全不需要**——这正是 u1-l2「本地开发零依赖」结论的源码依据。

#### 4.4.3 源码精读

`DiscoveryBackend` 枚举与「本地后端」判定：

[distributed.rs:634-653](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/distributed.rs#L634-L653)：枚举只有 `Kubernetes` 与 `KvStore(kv::Selector)` 两个变体；`is_local()` 判断 file/mem 这两种**不需要任何外部服务**的本地后端。

事件面的唯一权威解析：

[distributed.rs:663-680](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/distributed.rs#L663-L680)：`resolve_event_transport_kind()` 读取 `DYN_EVENT_PLANE`——`"nats"` 返回 Nats，`"zmq"` 返回 Zmq，**未设置或空值一律默认 Zmq**，非法值打 warning 后同样回落 Zmq。注释强调这是 `DYN_EVENT_PLANE` → `EventTransportKind` 的唯一映射，应启动时调用一次并缓存。

后端字符串的解析与 panic 路径：

[distributed.rs:700-718](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/distributed.rs#L700-L718)：`from_settings` 读 `DYN_DISCOVERY_BACKEND`（默认 `"etcd"`）；`"kubernetes"` 走 K8s 分支；其余交给 `Selector::from_str`，解析失败直接 `panic!`，报错信息列出全部合法值——这是「配置写错立刻死」的设计选择，宁可启动失败也不带错误配置运行。

NATS 启用的三条件：

[distributed.rs:729-749](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/distributed.rs#L729-L749)：`nats_enabled = request_plane.is_nats() || NATS_SERVER 已设置 || 事件面为 Nats`；三者都不满足时 `nats_config` 为 `None`，DRT 构造时就不会建 NATS 连接（见 4.3.3 第 2 步）。注释还说明了 NATS 的两个请求面之外的用途：KV 路由事件与 router 副本间同步。

`kv::Selector` 的三种 KV 底层：

[storage/kv.rs:132-139](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/storage/kv.rs#L132-L139)：`Selector` 枚举定义 `Etcd`（Box 装箱因为其配置体大）、`File`、`Memory` 三个变体，默认值是 `Memory`。

[storage/kv.rs:154-170](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/storage/kv.rs#L154-L170)：`FromStr` 实现里 `"file"` 分支读取 `DYN_FILE_KV` 作为根目录，未设置时用系统临时目录下的 `dynamo_store_kv`；未知值报 "Unknown key-value store type"。file 后端的注册文件默认 10 秒 TTL、以 TTL/3（至少 1 秒）为间隔保活（[storage/kv/file.rs:30-33](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/storage/kv/file.rs#L30-L33)、[file.rs:73-74](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/storage/kv/file.rs#L73-L74)）——这就是 u1-l2 说「worker 死后注册至多 10 秒消失」的出处。

etcd 的默认地址：

[transports/etcd.rs:874-880](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/transports/etcd.rs#L874-L880)：默认 etcd 地址是 `http://localhost:2379`，可用 `ETCD_ENDPOINTS` 环境变量（逗号分隔多地址）覆盖。

`RequestPlaneMode` 与其环境变量读取：

[distributed.rs:798-805](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/distributed.rs#L798-L805)：枚举定义 `Nats` 与 `Tcp`（标注 `#[default]`，是默认值）。

[distributed.rs:831-844](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/distributed.rs#L831-L844)：`from_env()` 读 `DYN_REQUEST_PLANE`，解析失败静默回落默认值（Tcp）——与发现后端的 panic 策略形成对比。

两个特殊预设也值得一看：`for_cli()`（etcd 后端但不挂 lease，供 CLI 工具短连使用）在 [distributed.rs:752-777](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/distributed.rs#L752-L777)；`process_local()`（mem 后端 + 无 NATS，前后端同进程）在 [distributed.rs:781-790](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/distributed.rs#L781-L790)。

#### 4.4.4 代码实践

**实践目标**：体验「发现后端决定注册表载体」——同一个 hello_world，换后端后观察注册介质的形态变化。

**操作步骤**（待本地验证）：

```bash
cd lib/runtime/examples

# 终端 1：file 后端，指定自定义根目录
DYN_DISCOVERY_BACKEND=file DYN_FILE_KV=/tmp/dynamo_store_a \
  cargo run -p hello_world --bin server

# 终端 2：看看注册介质长什么样
find /tmp/dynamo_store_a -type f | head
cat $(find /tmp/dynamo_store_a -type f | head -1)   # 观察注册内容（可能是二进制/JSON）

# 终端 3：client 用同一个根目录即可连通
DYN_DISCOVERY_BACKEND=file DYN_FILE_KV=/tmp/dynamo_store_a \
  cargo run -p hello_world --bin client
```

**需要观察的现象**：`/tmp/dynamo_store_a` 下出现以 namespace/component 组织的注册文件；server 存活期间文件持续保活，`Ctrl+C` 杀掉 server 后约 10 秒内文件消失。

**预期结果**：文件层级反映 `dynamo.backend.generate` 这条三段式路径；杀 server 后 TTL 过期清理。目录布局细节**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`DYN_DISCOVERY_BACKEND=file` 但忘记在 client 上设置同样的 `DYN_FILE_KV`，会发生什么？

**参考答案**：client 会用默认根目录（系统临时目录下的 `dynamo_store_kv`），与 server 的 `/tmp/dynamo_store_a` 互不可见——client 的 `wait_for_instances` 永远等不到实例（见 4.5.3）。这是模拟「namespace/注册表不匹配」的最干净方式，不需要改任何代码。

**练习 2**：为什么 `DYN_REQUEST_PLANE=bogus` 只是静默回落 Tcp，而 `DYN_DISCOVERY_BACKEND=bogus` 却直接 panic？

**参考答案**：见 [distributed.rs:834-839](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/distributed.rs#L834-L839) 与 [distributed.rs:710-715](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/distributed.rs#L710-L715)。请求面选错传输只是性能/兼容问题且 Tcp 是安全默认；而发现后端写错意味着系统连错了「电话簿」，继续跑只会得到莫名其妙的行为，宁可早死早暴露。

**练习 3**：`DiscoveryBackend::is_local()` 什么时候会被用到？

**参考答案**：用于判断当前后端是否不需要 etcd/NATS 等外部服务（file/mem 为真），从而决定能否在无基础设施的环境（本地开发、单测）运行。

### 4.5 hello_world 启动链：Worker、进程级运行时、server 与 client

#### 4.5.1 概念说明

`lib/runtime/examples/hello_world` 是 Rust 版的「最小 worker」——与 u2-l1 的 Python hello_world 一一对应，但少了 PyO3 这层皮。它引入了最后一个角色 `Worker`：`main()` 与 `Runtime` 之间的便捷封装，负责四件事：

1. 持有**进程级唯一的 Tokio 运行时**（`RT` 单元格里放的是运行时本体而非 Handle——pyo3 桥的 `init_with_runtime` 需要一个 `&'static Runtime`），并保证谁先来谁建、后来者复用。
2. 安装 SIGINT/SIGTERM 信号处理器，收到信号触发优雅关停。
3. 给应用一个优雅关停期限（`DYN_WORKER_GRACEFUL_SHUTDOWN_TIMEOUT`，debug 构建默认 5 秒、release 默认 30 秒），超时则以退出码 **911** 强杀。
4. 通过 `COMPUTE_CLAIMED` 原子标志保证**一个进程只附挂一次计算池**：无论后面包出多少个 `Runtime` wrapper，Rayon 池只有一个。

**重要工程细节**：`lib/runtime/examples/` 是一个**独立的 Cargo workspace**（自己的 `[workspace]` 节和 `Cargo.lock`），不属于仓库根 workspace。所以必须进入该目录执行 `cargo run`，在仓库根目录 `cargo run -p hello_world` 是找不到这个包的。

#### 4.5.2 核心流程

server 侧启动链（[server.rs:16-25](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/examples/hello_world/src/bin/server.rs#L16-L25)）：

```text
main()
  ├─ logging::init()                     # 初始化 tracing 日志
  ├─ Worker::from_settings()             # 读 RuntimeConfig → create_runtime → 存入 RT 单元格
  └─ worker.execute(app)                 # 在运行时上跑 app，阻塞直到结束
        └─ app(runtime)
              ├─ DistributedRuntime::from_settings(runtime)   # 读 DYN_* 建 DRT（4.3 的流程）
              └─ backend(drt)
                    ├─ Ingress::for_engine(RequestHandler)    # 把引擎包装成网络入口
                    └─ drt.namespace("dynamo")               # namespace 是常量 DEFAULT_NAMESPACE
                          .component("backend")?
                          .endpoint("generate")
                          .endpoint_builder()
                          .handler(ingress)
                          .start().await                      # 注册进发现后端，开始收请求
```

client 侧（[client.rs:10-38](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/examples/hello_world/src/bin/client.rs#L10-L38)）：

```text
main()
  ├─ logging::init() + Worker::from_settings() + worker.execute(app)   # client 也是 worker！
  └─ app(runtime)
        ├─ DistributedRuntime::from_settings(runtime)
        ├─ namespace("dynamo").component("backend").endpoint("generate").client().await
        ├─ client.wait_for_instances().await?          # ★ 阻塞等至少一个实例上线
        ├─ PushRouter::from_client(client, Default::default())
        ├─ router.random("hello world".into()).await?   # random 路由策略发一条
        ├─ while let Some(resp) = stream.next().await   # 逐帧打印
        └─ runtime.shutdown()
```

#### 4.5.3 源码精读

**进程级运行时的三件套静态变量**（本讲更新重构的核心）：

[worker.rs:32-52](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/worker.rs#L32-L52)：

- `RT: OnceCell<tokio::runtime::Runtime>`——**本进程唯一的 Tokio 运行时**。注意存的是运行时本体而不是 `Handle`，文档注释说明这是因为 `pyo3_async_runtimes::tokio::init_with_runtime` 需要 `&'static Runtime`；由 `from_config` 或 `ensure_process_runtime` 谁先到谁设置，所有路径都经过这个单元格，进程不可能出现两个运行时。（旧实现还有一个平行的 `RTHANDLE` 单元格，本次更新已删除。）
- `RTCONFIG: OnceCell<RuntimeConfig>`——`RT` 建成时所用的配置，让 `runtime_from_existing` 不必重读环境就能附挂匹配的计算池；只有 `ensure_process_runtime` 会填它。
- `COMPUTE_CLAIMED: AtomicBool`——计算池是否已被某个 wrapper 领走；**一进程一池**，不管之后包多少个 wrapper。

四个入口的分工：

[worker.rs:81-96](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/worker.rs#L81-L96)：`from_config` 检查 `RT` 已有则报 "Worker already initialized"；否则 `config.create_runtime()` 用 `RT.try_insert` 存入单元格（并发竞争下只有一个线程插入成功），再用 `Runtime::from_handle` 包一层——注意它**带自己的 config、不附挂计算池**。

[worker.rs:125-139](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/worker.rs#L125-L139)：`ensure_process_runtime()`（新增公开方法）幂等地返回进程级运行时的 `&'static` 引用：已存在直接返回（快路径），否则 `get_or_try_init` 里 `RuntimeConfig::from_settings()` → 打 info 日志 "dynamo runtime configuration: {config}" → `create_runtime()` → 把 config 存入 `RTCONFIG`。它存在的理由：pyo3 桥需要 `&'static` 运行时，而 `from_config` 在运行时已存在时只会报错、给不了这个引用。

[worker.rs:103-119](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/worker.rs#L103-L119)：`runtime_from_existing()` 先 `ensure_process_runtime()` 确保 `RT` 存在，然后做**计算池归属裁决**：若 `RTCONFIG` 有值且当前线程是第一个完成 `COMPUTE_CLAIMED.swap(true)` 的（原子交换同时解决双线程竞态），就走 `Runtime::from_handle_with_config` 附挂计算池与 `block_in_place` 许可；否则只 `Runtime::from_handle`。注释解释了为什么不能问「是不是我刚建的 RT」——调用方可能先调过 `ensure_process_runtime`（pyo3 侧的 `DistributedRuntime::new` 正是如此），那样这个问题永远答「不是」，池就丢了。

[worker.rs:145-147](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/worker.rs#L145-L147)：`has_existing_runtime()` 相应简化为只查 `RT`，且**从不创建**——调用方可以用它判断自己会不会是运行时的主人。

**新增集成测试**把上述裁决规则钉死：

[tests/process_runtime.rs:19-43](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/tests/process_runtime.rs#L19-L43)：`first_runtime_wrapper_owns_the_compute_pool` 完整镜像 pyo3 侧的调用顺序——先 `ensure_process_runtime`（像桥要求的那样提前拿运行时），再第一次 `runtime_from_existing`（断言 `compute_pool().is_some()`：即使运行时不是它亲手建的也该拿到池），再第二次 `runtime_from_existing`（断言 `compute_pool().is_none()`：后续 wrapper 复用运行时、不再建 Rayon 池），最后断言两个 wrapper 的 `primary().id()` 相同。文件头注释说明为什么它是**独立测试二进制**：`RT`/`RTCONFIG`/`COMPUTE_CLAIMED` 是进程全局的，首调行为每进程只能观察一次，所以这个文件故意只放一个测试。

**运行前提——独立 workspace**：

[lib/runtime/examples/Cargo.toml:4-9](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/examples/Cargo.toml#L4-L9)：examples 目录自成 workspace，成员只有 `hello_world`、`service_metrics`、`system_metrics`，其中 `dynamo-runtime = { path = "../" }` 以相对路径依赖旁边的 runtime crate。

**server 的 main 与引擎**：

[server.rs:16-25](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/examples/hello_world/src/bin/server.rs#L16-L25)：`main` 三行式——`logging::init()`、`Worker::from_settings()?`、`worker.execute(app)`；`app` 拿到 `Runtime` 后构造 DRT。

[server.rs:35-52](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/examples/hello_world/src/bin/server.rs#L35-L52)：`RequestHandler` 实现 `AsyncEngine<SingleIn<String>, ManyOut<Annotated<String>>>`，把输入字符串逐字符包成 `Annotated` 再组成流——引擎抽象本身是下一讲（u3-l3）的主题，这里只需知道它产出一个流。

[server.rs:54-65](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/examples/hello_world/src/bin/server.rs#L54-L65)：`backend()` 用 `Ingress::for_engine` 把引擎变成可服务的入口，然后沿 `namespace("dynamo") → component("backend") → endpoint("generate")` 三段路径用 builder 模式 `handler(...).start()` 完成注册。namespace 常量定义在 [examples/hello_world/src/lib.rs:4](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/examples/hello_world/src/lib.rs#L4)（值为 `"dynamo"`）。

**client 的关键一行**：

[client.rs:19-27](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/examples/hello_world/src/bin/client.rs#L19-L27)：client 沿同样的三段路径拿到 endpoint 的 `client()`，然后 `wait_for_instances()` 等实例，再用 `PushRouter::from_client` + `random` 策略发送。**注意 client 进程也完整地走了 `Worker::from_settings` + DRT 构造**——在 Dynamo 里「客户端」和「服务端」共享同一套运行时基础设施，区别只在于注册端点还是查找端点（u2-l1 在 Python 侧见过同样的对称性）。

**「找不到服务」的真实路径**——`wait_for_instances` 的等待循环：

[component/client.rs:644-666](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/component/client.rs#L644-L666)：函数先打 trace 日志 "wait_for_instances: Starting wait for endpoint"，然后进入 `loop`：从 `tokio::sync::watch` 通道读当前实例快照，**列表为空就 `rx.changed().await?` 挂起等待**，直到有变化再查；列表非空才 break 并打 info 日志 "Found N instance(s)"。这解释了两种失败形态：

- 服务不在线/namespace 不匹配/注册表不互通 → 循环永远等下去（client **挂起**而非报错退出），只有 trace 级日志可见；
- watch 通道的发送端被关闭（如 DRT 被关停）→ `rx.changed().await?` 返回 Err，`?` 把错误抛出。

**Worker 的执行与信号处理**：

[worker.rs:158-167](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/worker.rs#L158-L167)：`execute` 在 **secondary** 线程池上 `block_on` 整个应用生命周期，结束后调用 `runtime.shutdown()`。

[worker.rs:192-201](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/worker.rs#L192-L201)：优雅关停超时读 `DYN_WORKER_GRACEFUL_SHUTDOWN_TIMEOUT`，未设置时 debug 构建取 5 秒、release 取 30 秒（常量在 [worker.rs:61-65](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/worker.rs#L61-L65)）。

[worker.rs:226-235](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/worker.rs#L226-L235)：应用若在超时内没退出，`std::process::exit(911)` 强制终止。

[worker.rs:269-298](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/worker.rs#L269-L298)：`signal_handler` 同时监听 Ctrl+C（`signal::ctrl_c`）与 SIGTERM（unix `SignalKind::terminate`），任一到达即取消取消令牌、触发上面的关停流程——这正是一进程里 u2-l3 讲过的那些 Context 取消信号的最终源头。

#### 4.5.4 代码实践（本讲主实践）

**实践目标**：跑通 Rust 版 hello_world，然后系统性地制造三种「client 找不到服务」的场景，把报错路径对应到源码行；顺带跑一遍新增的进程级运行时测试。

**操作步骤**（全部待本地验证；需要 Rust 工具链，无需 GPU/etcd/NATS）：

1. **基线：正常跑通**

   ```bash
   cd lib/runtime/examples
   # 终端 1
   DYN_DISCOVERY_BACKEND=file cargo run -p hello_world --bin server
   # 终端 2
   DYN_DISCOVERY_BACKEND=file cargo run -p hello_world --bin client
   ```

   预期：client 每行打印一个 `Ok(Data(Annotated { data: "h" }))` 之类的帧（"hello world" 的逐字符流，帧格式**待本地验证**），随后进程退出。

2. **场景 A：注册表隔离（等价于 namespace 不匹配）**

   ```bash
   # 终端 1：server 用目录 a
   DYN_DISCOVERY_BACKEND=file DYN_FILE_KV=/tmp/store_a cargo run -p hello_world --bin server
   # 终端 2：client 用目录 b —— 双方都在 namespace "dynamo"，但注册表互不可见
   DYN_DISCOVERY_BACKEND=file DYN_FILE_KV=/tmp/store_b \
   RUST_LOG=dynamo_runtime=trace \
   timeout 10 cargo run -p hello_world --bin client
   ```

   预期：client 卡住 10 秒后被 timeout 杀掉。trace 日志里应能看到 `wait_for_instances: Starting wait for endpoint`（对应 [component/client.rs:645-648](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/component/client.rs#L645-L648)），且**永远等不到** "Found N instance(s)"。

3. **场景 B：完全不起 server**

   只跑 client（`DYN_DISCOVERY_BACKEND=file`）。现象与场景 A 相同：无限等待。这说明 Dynamo client 的默认语义是「等服务出现」而不是「服务不存在就报错」。

4. **场景 C：etcd 后端但本机没有 etcd**

   ```bash
   # 不设置 DYN_DISCOVERY_BACKEND（默认 etcd，默认地址 localhost:2379）
   cargo run -p hello_world --bin server
   ```

   预期：server 在 `DistributedRuntime::from_settings` 阶段就报错退出，错误信息包含 "Could not connect to etcd. Pass `--discovery-backend ..` to use a different backend or start etcd."——对应 [distributed.rs:177-181](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/distributed.rs#L177-L181) 的 `inspect_err`。

5. **附加：验证进程级运行时契约**

   ```bash
   # 仓库根目录
   cargo test -p dynamo-runtime --test process_runtime
   ```

   预期：`first_runtime_wrapper_owns_the_compute_pool` 通过，证明「先 ensure 再两次 wrap」时第一个 wrapper 拿池、第二个不重复建池。

**需要观察的现象**：三种失败场景分别呈现「挂起等待」「挂起等待」「启动即报错」三种不同形态；附加测试单绿。

**预期结果**：整理成一张表：

| 场景 | 失败位置（源码） | 表现 |
|------|------------------|------|
| A/B：实例列表为空 | [client.rs:652-655](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/component/client.rs#L652-L655) 的 watch 等待循环 | 无限挂起，trace 日志可见 |
| C：etcd 连不上 | [distributed.rs:177-181](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/distributed.rs#L177-L181) | 进程带错误信息退出 |
| 附加：双 wrapper 契约 | [process_runtime.rs:19-43](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/tests/process_runtime.rs#L19-L43) | 测试通过 |

#### 4.5.5 小练习与答案

**练习 1**：client 侧代码里 `worker.execute(app)` 与 server 完全相同，为什么「客户端」也需要 Worker？

**参考答案**：因为 client 同样需要 Runtime（线程池、取消令牌）与 DRT（连接发现后端、订阅 endpoint 变化的 watch）。Dynamo 的服务发现是双向的：查找方也要先「连上电话簿」。此外 Worker 带来的信号处理让 client 也能被 Ctrl+C 优雅打断（[client.rs:10-14](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/examples/hello_world/src/bin/client.rs#L10-L14)）。

**练习 2**：`hello_world` 的 namespace 是写死的常量 `"dynamo"`。如果让你支持用环境变量覆盖 namespace（比如 `DYN_NAMESPACE`），改动应放在哪？

**参考答案**：`DistributedConfig::from_settings()`（[distributed.rs:696-750](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/distributed.rs#L696-L750)）只管后端/请求面，不含 namespace；namespace 是 `drt.namespace("...")` 的调用参数。最小改动是在示例的 `server.rs`/`client.rs` 里把 `DEFAULT_NAMESPACE` 换成 `std::env::var("DYN_NAMESPACE").unwrap_or(DEFAULT_NAMESPACE.into())`（示例思路，仓库当前无此变量）。

**练习 3**：`Worker::from_config` 建的 Runtime 没有计算池，而 `runtime_from_existing` 建的第一个 wrapper 有。为什么 hello_world（走 `from_config`）不需要池，而 pyo3 路径需要？

**参考答案**：`from_config` 的注释写明「带自己的 config、不带池」（[worker.rs:44-45](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/worker.rs#L44-L45)、[worker.rs:90-95](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/worker.rs#L90-L95)）：它服务于 Rust 原生进程（如 hello_world），这类进程的计算池按需另行配置；而 pyo3 路径上 `RT` 由 `ensure_process_runtime` 用环境配置建成、`RTCONFIG` 在旁边记着同一份配置，第一个 wrapper 就应该把这份配置隐含的池领走（`from_handle_with_config`），否则 Python 进程里永远没人附挂池——CPU 密集操作会全部退化为 `spawn_blocking`（对照 [runtime.rs:331-336](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/runtime.rs#L331-L336) 的 `compute_pool()` 返回 `Option` 的语义）。

## 5. 综合实践

把本讲五个模块串起来，做一次「配置矩阵实验」：

**任务**：对 hello_world（server + client），按下表逐组运行并记录结果，最后写一份 200 字左右的「故障排查速查」：

| 组 | DYN_DISCOVERY_BACKEND | 其他变量 | 预期结果 |
|----|----------------------|----------|----------|
| 1 | `file`（双方一致） | 无 | 正常收发逐字符流 |
| 2 | `file`（双方一致） | `DYN_SYSTEM_PORT=8081`（server） | 同上，且 8081 出现 /health |
| 3 | `file`（目录不同） | server: `DYN_FILE_KV=/tmp/a`；client: `/tmp/b` | client 无限挂起 |
| 4 | 不设（默认 etcd） | 无 etcd 环境 | server 启动报 "Could not connect to etcd..." |
| 5 | `file` | client: `DYN_REQUEST_PLANE=nats`（server 默认 tcp） | 思考题：请求面不一致时哪一侧先失败？**待本地验证** |
| 6 | `file`（双方一致） | `DYN_RUNTIME_NUM_WORKER_THREADS=2`（双方） | 正常收发；日志中 permits/线程数体现为 2（见 4.1.4，**待本地验证**） |

要求：

1. 每组把关键日志行（含出错的源码位置）抄录下来；
2. 组 3 用 `RUST_LOG=dynamo_runtime=trace` 找到 `wait_for_instances` 的日志证据；
3. 组 6 观察配置是否「穿过」了 figment 与 `tokio_builder()` 两层（对照 [config.rs:555](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/config.rs#L555) 的新单测想清楚：解析层已由测试保证，你要观察的是运行时行为层）；
4. 组 5 先凭本讲知识预测，再实验验证——提示：请求面模式同时影响 server 的网络入口与 client 的发送路径（[distributed.rs:196-202](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/runtime/src/distributed.rs#L196-L202) 把 `request_plane` 交给 `NetworkManager`）。

## 6. 本讲小结

- **`Runtime` 管本机**：tokio 线程池（primary/secondary）、双层取消令牌（主令牌 + 端点关停子令牌）、计算池；`new_with_config` 负责附挂配置派生的计算池与 `block_in_place` 许可，新增的 `from_handle_with_config` 让「借来的 Handle」也能带上这些附属物。关停走「停收新请求 → 等在途完成（超时 `DYN_RUNTIME_GRACEFUL_SHUTDOWN_TIMEOUT_SECS`）→ 断后端连接」三阶段。
- **`RuntimeConfig` 全靠环境，且有一份公开的「图纸」**：figment 按「默认值 → `/opt/dynamo/defaults/runtime.toml` → `/opt/dynamo/etc/runtime.toml` → `DYN_*` 环境变量」逐层覆盖，空环境变量会被过滤，`DYN_SYSTEM_*` 有专门的键名映射表；新拆出的 `tokio_builder()` 把「带配置的 Builder」暴露出去，`create_runtime()` 只是它的一行 `build()`。
- **两条运行时构建路径共用一份图纸**：本地 `create_runtime()` 与 pyo3 桥的 `pyo3_async_runtimes::tokio::init(config.tokio_builder())`（模块初始化时执行）都经过 `tokio_builder()`，所以 `DYN_RUNTIME_*` 对两者同时生效——这是修复「桥按 tokio 默认值自建运行时、无视环境变量」的钥匙。
- **`DistributedRuntime` 管集群**：装配发现客户端、NATS、请求面 NetworkManager、组件注册表与系统状态服务；它不是单例，克隆共享实例。
- **三个正交开关**：`DYN_DISCOVERY_BACKEND`（kubernetes/etcd/file/mem，默认 etcd，写错即 panic）、`DYN_REQUEST_PLANE`（tcp 默认/nats）、`DYN_EVENT_PLANE`（zmq 默认/nats）；file 后端 + tcp + zmq 组合实现零外部依赖的本地运行。
- **进程级运行时契约**：`Worker` 的 `RT`（存运行时本体）/`RTCONFIG`/`COMPUTE_CLAIMED` 三件套保证一进程一个 Tokio 运行时、一个 Rayon 计算池；第一个 `runtime_from_existing` wrapper 拿池、后续复用（有专门测试二进制钉死）。
- **client 找不到服务的两种形态**：实例列表为空时 `wait_for_instances` 在 watch 循环里无限挂起；etcd 连不上则 server 启动即报错退出。**`lib/runtime/examples/` 是独立 workspace**，运行示例必须先 `cd` 进该目录。

## 7. 下一步学习建议

本讲止步于 `drt.namespace("dynamo")` 这一行——namespace → component → endpoint 的服务目录层级正是下一讲 **u3-l2「服务注册模型：Component / Service / Endpoint」**的主题：endpoint 注册如何写进 etcd、客户端如何按 tag 查找并订阅实例变化、`wait_for_instances` 背后的 watch 通道是谁在喂。建议先精读 `lib/runtime/src/component/` 目录下的 `component.rs` 与 `endpoint.rs`，再对照本讲的场景 A 想一个问题：**client 是怎么「看见」server 的注册文件的？**（答案在 file KV 的目录 watcher 里。）若想继续深挖本讲的运行时话题，可以读 `pyo3_async_runtimes` 的 `init`/`init_with_runtime`/`get_runtime` 三个 API 的文档，理解「builder 交接」与「运行时交接」的区别。
