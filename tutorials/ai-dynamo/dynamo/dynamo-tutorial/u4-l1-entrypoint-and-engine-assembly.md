# 引擎装配：entrypoint 与 EngineConfig

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `EngineConfig` 三个变体（`Dynamic` / `InProcessText` / `InProcessTokens`）各自的含义与适用场景，以及 `RouterConfig` 如何与之正交组合出不同拓扑。
2. 完整追踪一次 `make_engine` 调用：从 Python 侧的 `EntrypointArgs(EngineType.Dynamic, ...)` 出发，跨过 PyO3 边界，到达 Rust 侧的 `LocalModelBuilder` → `select_engine` → `EngineConfig`。
3. 解释 `EngineType.Dynamic` 如何通过 `chat_engine_factory` 回调把「引擎逻辑留在 Python、路由与发现留在 Rust」这一分工落地。
4. 看懂 `EngineDispatcher` / `StreamingEngineAdapter` 这对适配器，以及 `Input` 枚举如何决定引擎就位之后接什么（HTTP / gRPC / 终端 / Endpoint）。

本讲是第 4 单元（lib/llm 引擎层）的第一篇：先把「装配」这条骨架立起来，后续讲义（HttpService、preprocessor、discovery）都是挂在这条骨架上的器官。

## 2. 前置知识

本讲假设你已读过以下两讲（依赖：u3-l3、u2-l1），这里只做最小回顾：

- **AsyncEngine 抽象（u3-l3）**：Rust 侧一切引擎都实现 `AsyncEngine<Req, Resp, E>`，只有一个 `generate` 方法；配合 `SingleIn` / `ManyOut` 别名可判读引擎签名；`Context<T>` 信封携带取消信号。本讲的 `EngineDispatcher` 就是在这层抽象之上做的「多端点分发」适配器。
- **dynamo.runtime 三对象（u2-l1）**：Python 侧的 `DistributedRuntime` / `Endpoint` / `Client`，以及 namespace → component → endpoint 的服务目录。本讲的 `make_engine` 就是从 Python 世界进入这个分布式世界的正门。
- **PyO3 桥接（u2-l2，可选但推荐）**：`#[pyclass]` 薄壳 + inner 持有 Rust 结构体的模式；`future_into_py` 把 Rust async 函数变成 Python awaitable。本讲会大量遇到这两招。

另外补充两个本讲反复出现的名词：

- **worker**：真正执行推理的进程（vLLM / SGLang / TRT-LLM / mocker / sample）。frontend 不执行推理，只做 HTTP 接入、预处理和路由。
- **frontend**：对外暴露 OpenAI 兼容 API 的进程。它自己没有引擎，靠服务发现找到 worker——这正是 `EngineConfig::Dynamic` 存在的理由。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [lib/llm/src/entrypoint.rs](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/entrypoint.rs) | 装配层的「图纸定义」：`RouterConfig` 与 `EngineConfig` 两个核心类型，以及 `ChatEngineFactoryCallback` 回调类型 |
| [lib/llm/src/entrypoint/input.rs](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/entrypoint/input.rs) | `Input` 枚举与 `run_input`：引擎就位后接什么输入源 |
| [lib/llm/src/entrypoint/input/common.rs](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/entrypoint/input/common.rs) | 装配的「施工队」：`build_preprocessed_routing`、`prepare_engine`、`PreprocessedRouting::build_pipeline` |
| [lib/llm/src/entrypoint/input/http.rs](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/entrypoint/input/http.rs) | `HttpFrontend`：把 `EngineConfig` 三分支分别接成 HTTP 服务 |
| [lib/llm/src/engines.rs](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/engines.rs) | `StreamingEngine` trait、`EngineDispatcher` / `StreamingEngineAdapter` 适配器、echo 引擎 |
| [lib/bindings/python/rust/llm/entrypoint.rs](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/llm/entrypoint.rs) | PyO3 侧：`EngineType`、`EntrypointArgs`、`make_engine`、`run_input` |
| [lib/llm/src/discovery/watcher.rs](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/discovery/watcher.rs) | 模型发现后的组件生成：何时建 KvRouter / PrefillRouter、何时调用 `chat_engine_factory` |
| [components/src/dynamo/frontend/main.py](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py) | Python 前端主流程，`make_engine` 的头号调用方 |
| [examples/backends/sample/launch/agg.sh](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/examples/backends/sample/launch/agg.sh) / [disagg.sh](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/examples/backends/sample/launch/disagg.sh) | 综合实践用的两种启动拓扑 |

## 4. 核心概念与源码讲解

先建立一个直觉：**装配一个 Dynamo runner 需要回答两个正交的问题**。

1. **引擎在哪？**（`EngineConfig`）——推理引擎是在远端 worker 进程里（`Dynamic`），还是就在本进程里（`InProcessText` / `InProcessTokens`）？
2. **请求怎么走？**（`RouterConfig`）——frontend 内部用哪种 `RouterMode` 把请求分给 worker？

这两个决策互相独立：同一个远端 worker 集合，可以配 round-robin，也可以配 KV 感知路由。装配层（entrypoint）的全部工作，就是把这两个决策连同杂项参数（HTTP 端口、tokenizer、TLS……）收集起来，在合适的时机把组件「长」出来。

### 4.1 EngineConfig 与 RouterConfig：装配的两张图纸

#### 4.1.1 概念说明

`EngineConfig` 是一个枚举，描述「执行推理的东西」在哪里：

| 变体 | 引擎位置 | 谁负责分词/模板 | 典型使用者 |
|------|----------|------------------|------------|
| `Dynamic` | 远端 worker，经服务发现 | 可选：框架（Rust tokenizer）或 Python 回调 | `dynamo.frontend`（`EngineType.Dynamic`） |
| `InProcessText` | 本进程 | 引擎自己（收文本，自己分词） | echo 引擎（`EngineType.Echo`） |
| `InProcessTokens` | 本进程 | 框架（收 token，外面包 pre/post processor） | mocker 引擎（`EngineType.Mocker`） |

`Dynamic` 变体还携带两个可选「插件槽」：

- `chat_engine_factory`：一个异步回调，签名是 `(ModelCardInstanceId, ModelDeploymentCard, PrefillRoutedEngine) -> OpenAIChatCompletionsStreamingEngine`。frontend 用 `--dyn-chat-processor vllm|sglang` 时由 Python 侧提供——**引擎的业务逻辑留在 Python，路由与发现由 Rust 代劳**，回调收到的 `PrefillRoutedEngine` 就是 Rust 建好的路由管线。
- `prefill_load_estimator`：基于 AIConfigurator 的 prefill 负载估计器（配合 `--router-prefill-load-model aic`）。

`RouterConfig` 则是路由侧的配置包：`router_mode`（七种模式）+ `kv_router_config`（KV 路由的几十个细调参数）+ `load_threshold_config`（过载检测阈值）+ `session_affinity_ttl_secs`。注意其中 `enforce_disagg` 已被标记废弃——**拓扑与就绪判断现在来自 worker 注册的类型**，不再由 frontend 强制。

#### 4.1.2 核心流程

`EngineConfig` 的消费发生在两个时机：

```text
时机一：进程内引擎（InProcessText / InProcessTokens）
  make_engine → select_engine 直接构造引擎对象
  → HttpFrontend 的对应分支立即 build_pipeline 并注册进 ModelManager

时机二：远端引擎（Dynamic）
  make_engine → select_engine 只装好回调与 LocalModel（此时没有任何 worker）
  → HttpFrontend 启动 run_watcher 监听服务发现
  → 某个 worker 注册出现 → watcher 决定生成哪些路由组件
       ├─ router_mode == KV 且需要路由 → KvRouter（kv_chooser）
       ├─ worker 类型是 Decode → PrefillRouter（prefill_chooser）
       └─ 总是 → build_preprocessed_routing 组装 RoutingHost
  → 若有 chat_engine_factory：调用它，把 Rust 路由管线交给 Python 引擎
```

`RouterConfig` 的旅程则更简单：它被塞进 `LocalModelBuilder`，随 `LocalModel` 一路传到 watcher，在那里决定 `kv_chooser` / `prefill_chooser` 的构造参数。

#### 4.1.3 源码精读

先看 `RouterConfig` 的定义——注意第 50-52 行对 `enforce_disagg` 的注释：

[lib/llm/src/entrypoint.rs:L44-L55](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/entrypoint.rs#L44-L55)

```rust
pub struct RouterConfig {
    pub router_mode: RouterMode,
    pub kv_router_config: KvRouterConfig,
    /// Load threshold configuration for overload detection
    pub load_threshold_config: LoadThresholdConfig,
    /// Deprecated compatibility field. Routing and readiness ignore this value.
    #[serde(default)]
    pub enforce_disagg: bool,
    #[serde(default)]
    pub session_affinity_ttl_secs: Option<u64>,
}
```

这段定义了路由侧的全部顶层旋钮：`router_mode` 是七种模式之一（`RoundRobin` / `Random` / `KV` / `Direct` / `PowerOfTwoChoices` / `LeastLoaded` / `DeviceAwareWeighted`，见 u3-l4 的 RouterMode 分组）；注释明确说明 `enforce_disagg` 只是兼容字段，路由和就绪判断都忽略它。

再看 `EngineConfig` 枚举本体：

[lib/llm/src/entrypoint.rs:L87-L109](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/entrypoint.rs#L87-L109)

```rust
pub enum EngineConfig {
    /// Remote networked engines that we discover via etcd
    Dynamic {
        model: Box<LocalModel>,
        chat_engine_factory: Option<ChatEngineFactoryCallback>,
        prefill_load_estimator: Option<Arc<dyn PrefillLoadEstimator>>,
    },

    /// A Text engine receives text, does it's own tokenization and prompt formatting.
    InProcessText {
        engine: Arc<dyn StreamingEngine>,
        model: Box<LocalModel>,
    },

    /// A Tokens engine receives tokens, expects to be wrapped with pre/post processors that handle tokenization.
    InProcessTokens {
        engine: ExecutionContext,
        model: Box<LocalModel>,
        is_prefill: bool,
        is_decode: bool,
    },
}
```

三个变体共享 `model: Box<LocalModel>`——所以枚举提供了统一访问器 `local_model()`（[L111-L119](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/entrypoint.rs#L111-L119)），后续所有消费方（HTTP builder、watcher）都从这里取模型与路由配置。`InProcessTokens` 额外带 `is_prefill` / `is_decode` 两个布尔——这就是 mocker 模拟 P/D 分离角色的开关。

回调类型 `ChatEngineFactoryCallback` 是一个 `Arc<dyn Fn(...) -> Pin<Box<dyn Future>>>`：

[lib/llm/src/entrypoint.rs:L33-L42](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/entrypoint.rs#L33-L42)

```rust
pub type ChatEngineFactoryCallback = Arc<
    dyn Fn(
            ModelCardInstanceId,
            ModelDeploymentCard,
            PrefillRoutedEngine,
        ) -> Pin<Box<dyn Future<Output = anyhow::Result<OpenAIChatCompletionsStreamingEngine>> + Send>,
        > + Send
        + Sync,
>;
```

读法：给定「哪个 worker 实例（`ModelCardInstanceId`）+ 它的模型部署卡（`ModelDeploymentCard`）+ 一条 Rust 建好的路由引擎（`PrefillRoutedEngine`）」，异步返回一个可服务的 chat 引擎。第三个参数是关键——**Rust 把路由管线递给 Python，Python 只需要在上面包一层预处理/后处理**。

PyO3 侧还有一个 Python 包装版 `RouterConfig`，构造时做参数校验，再经 `From` 转换为 Rust 版本：

[lib/bindings/python/rust/llm/entrypoint.rs:L488-L526](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/llm/entrypoint.rs#L488-L526) — `RouterConfig::new` 在 `enforce_disagg=true` 时打印一次性告警、校验 `session_affinity_ttl_secs` 范围（1..=31536000）、验证 `LoadThresholdConfig`。

[lib/bindings/python/rust/llm/entrypoint.rs:L529-L543](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/llm/entrypoint.rs#L529-L543)

```rust
impl From<RouterConfig> for RsRouterConfig {
    fn from(rc: RouterConfig) -> RsRouterConfig {
        RsRouterConfig {
            router_mode: rc.router_mode.into(),
            kv_router_config: rc.kv_router_config.inner,
            load_threshold_config: RsLoadThresholdConfig { ... },
            enforce_disagg: false,          // 永远置 false：拓扑由 worker 类型决定
            session_affinity_ttl_secs: rc.session_affinity_ttl_secs,
        }
    }
}
```

注意 `enforce_disagg: false` 是硬编码的——即使 Python 侧传了 `true`，跨过桥之后也会被丢弃，与 Rust 侧的废弃标注呼应。

#### 4.1.4 代码实践

**实践目标**：亲手构造一次 `RouterConfig`，观察它的参数校验行为，建立「Python 侧配置 → Rust 侧图纸」的手感。

**操作步骤**：

1. 在装好 `ai-dynamo` 的环境里（u1-l2 / u1-l4 的任一安装方式）执行以下脚本（示例代码，非项目原有文件）：

```python
# demo_router_config.py —— 示例代码
from dynamo.llm import KvRouterConfig, RouterConfig, RouterMode

# 1) 合法构造
rc = RouterConfig(RouterMode.RoundRobin, KvRouterConfig())
print("mode:", rc.router_mode)

# 2) 非法 ttl：低于 1
try:
    RouterConfig(RouterMode.KV, KvRouterConfig(), session_affinity_ttl_secs=0)
except ValueError as e:
    print("ttl=0 被拒绝:", e)

# 3) 废弃参数：enforce_disagg
import logging
logging.basicConfig(level=logging.WARNING)
rc3 = RouterConfig(RouterMode.KV, KvRouterConfig(), enforce_disagg=True)
print("enforce_disagg 构造成功（但会被 Rust 侧丢弃）")
```

2. 对照源码确认三处行为的出处：ttl 校验在 [lib/bindings/python/rust/llm/entrypoint.rs:L506-L510](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/llm/entrypoint.rs#L506-L510)，`enforce_disagg` 告警在 [L498-L505](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/llm/entrypoint.rs#L498-L505)。

**需要观察的现象**：步骤 2 抛出 `ValueError`，报错文案含 "session_affinity_ttl_secs must be between 1 and 31536000"；步骤 3 打印一条 "enforce_disagg is deprecated and ignored..." 的 WARNING。

**预期结果**：三步全部按上述通过/失败。若本机尚未安装 ai-dynamo，此实践可改为纯源码阅读：在 `_core.pyi`（[lib/bindings/python/src/dynamo/_core.pyi](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/src/dynamo/_core.pyi)）中找到 `RouterConfig` 的类型标注，与上述 Rust 源码逐字段对照。运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `EngineConfig` 的三个变体都要携带 `LocalModel`，而不是把模型信息放在枚举外面？

**答案**：因为三个变体对模型的用法不同（`Dynamic` 用它拿 router_config 与 namespace 过滤器，`InProcessTokens` 用它的 tokenizer 和 card 建 pipeline），但所有下游消费方都需要「无论哪个变体都能取到模型」。枚举内嵌 + 统一访问器 `local_model()` 让消费方（如 [http.rs:L148](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/entrypoint/input/http.rs#L148) 第一行就是 `engine_config.local_model()`）无须 match 三次。

**练习 2**：`InProcessText` 和 `InProcessTokens` 都是进程内引擎，本质区别是什么？

**答案**：分词与模板的责任归属。`InProcessText` 的引擎收到的直接是文本（OpenAI 请求对象），自己做 tokenization 和 prompt formatting（echo 引擎甚至原样回显字符）；`InProcessTokens` 的引擎收到的已经是 `PreprocessedRequest`（token 块），框架在外面套 `OpenAIPreprocessor` / `Backend` 前后处理层——这正是 [common.rs:L435-L465](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/entrypoint/input/common.rs#L435-L465) `build_pipeline` 里 `frontend.link(preprocessor).link(backend).link(engine)...` 那条链存在的意义。

**练习 3**：如果一个部署里 frontend 配了 `--router-mode kv` 但所有 worker 都是聚合（Aggregated）角色，会发生什么？

**答案**：frontend 仍会为该 worker 集构造 `KvRouter`（条件是 `router_mode == KV && needs_preprocessed_routing`，见 [watcher.rs:L572-L600](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/discovery/watcher.rs#L572-L600)），但不会构造 `PrefillRouter`——它的构造条件是 worker 类型为 `Decode`（[watcher.rs:L633-L658](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/discovery/watcher.rs#L633-L658)），聚合 worker 走 `PrefillRouter::disabled_with_selector` 的默认分支（[common.rs:L290-L296](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/entrypoint/input/common.rs#L290-L296)）。KV 路由照常打分，只是没有 P/D 二级路由。

### 4.2 EntrypointArgs 与 make_engine：从 Python 到 Rust 的装配路径

#### 4.2.1 概念说明

`EngineConfig` 是 Rust 内部类型，Python 侧无法直接构造。跨界的信封是 `EntrypointArgs`——一个 PyO3 类，把「引擎类型 + 模型信息 + 路由配置 + HTTP/TLS/tokenizer 等约三十个参数」打包成一个对象送过桥。`make_engine(distributed_runtime, args)` 则是装配总入口：**解析信封 → 构建 LocalModel → 按 EngineType 选出引擎形态 → 返回包装好的 `EngineConfig`**。

`EngineType` 是用户在 Python 侧真正写的枚举，只有三个值：

[lib/bindings/python/rust/llm/entrypoint.rs:L66-L73](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/llm/entrypoint.rs#L66-L73)

```rust
pub enum EngineType {
    Echo = 1,
    Dynamic = 2,
    Mocker = 3,
}
```

它与 `EngineConfig` 变体的对应关系**不是一对一**：`Echo` → `InProcessText`，`Dynamic` → `Dynamic`，`Mocker` → `InProcessTokens`。vLLM / SGLang / TRT-LLM 等真实后端不在这三个值里——它们是独立进程的 worker，对 frontend 而言统统表现为 `EngineType.Dynamic`。

#### 4.2.2 核心流程

`make_engine` 的完整装配路径（以 frontend 为例）：

```text
Python:  python -m dynamo.frontend
  └─ main.py async_main()
       ├─ build_router_config(config)              # FrontendConfig → PyO3 RouterConfig
       ├─ EntrypointArgs(EngineType.Dynamic, **kwargs)   # 装信封
       └─ engine = await make_engine(runtime, e)   # 过桥
            └─ Rust: make_engine()
                 ├─ ① LocalModelBuilder 链式填充（名称/端口/路由/TLS/命名空间…）
                 ├─ ② 异步体（future_into_py）：
                 │     ├─ model_path 存在 → LocalModel::fetch()
                 │     │     （Mocker 只拉 tokenizer，ignore_weights=true）
                 │     ├─ builder.build().await → LocalModel
                 │     └─ select_engine(drt, args, local_model)
                 │          ├─ Echo   → InProcessText { make_echo_engine() }
                 │          ├─ Dynamic→ Dynamic { chat_engine_factory,
                 │          │                       prefill_load_estimator }
                 │          └─ Mocker → InProcessTokens { make_mocker_engine(...) }
                 └─ 返回 PyO3 包装的 EngineConfig
  └─ run_input(runtime, "http", engine, ...)       # 引擎就位，接输入源（见 4.3）
```

其中 `chat_engine_factory` 的桥接值得单独看：Python 传进来的是一个普通 async 函数，Rust 把它包成 `ChatEngineFactoryCallback`，等 watcher 发现新模型时才调用。

#### 4.2.3 源码精读

先看信封本体——`EntrypointArgs` 的字段：

[lib/bindings/python/rust/llm/entrypoint.rs:L560-L588](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/llm/entrypoint.rs#L560-L588)

```rust
pub(crate) struct EntrypointArgs {
    engine_type: EngineType,
    model_path: Option<PathBuf>,
    model_name: Option<String>,
    endpoint_id: Option<EndpointId>,
    template_file: Option<PathBuf>,
    router_config: Option<RouterConfig>,
    kv_cache_block_size: Option<u32>,
    http_host: Option<String>,
    http_port: u16,
    ...
    is_prefill: bool,
    is_decode: bool,
    chat_engine_factory: Option<PyEngineFactory>,
    aic_perf_config: Option<AicPerfConfig>,
}
```

注意 `chat_engine_factory` 的类型是 `PyEngineFactory` 而不是裸 `PyObject`——这是为了在注册时捕获 `TaskLocals`（Python 事件循环上下文），见 [L638-L652](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/llm/entrypoint.rs#L638-L652)：如果晚到调用时才取 locals，回调可能跑在另一个不同的循环上下文里。

`make_engine` 的同步前半段是纯 builder 链：

[lib/bindings/python/rust/llm/entrypoint.rs:L732-L761](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/llm/entrypoint.rs#L732-L761)

```rust
let mut builder = LocalModelBuilder::default();
builder
    .model_name(args.model_name.clone()
        .or_else(|| args.model_path.clone().map(|p| p.display().to_string())))
    .endpoint_id(args.endpoint_id.clone())
    .request_template(args.template_file.clone())
    .kv_cache_block_size(args.kv_cache_block_size)
    .router_config(args.router_config.clone().map(|rc| rc.into()))
    ...
    .namespace(args.namespace.clone())
    .namespace_prefix(args.namespace_prefix.clone());
```

`router_config` 在这里完成了 `From` 转换进 builder——4.1 练习 1 里说的「图纸随 LocalModel 旅行」就是从这一行开始的。

异步后半段做模型下载与引擎选择：

[lib/bindings/python/rust/llm/entrypoint.rs:L762-L786](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/llm/entrypoint.rs#L762-L786)

```rust
pyo3_async_runtimes::tokio::future_into_py(py, async move {
    if let Some(model_path) = args.model_path.clone() {
        let local_path = if model_path.exists() {
            model_path
        } else {
            // Mocker only needs tokenizer, not weights
            let ignore_weights = matches!(args.engine_type, EngineType::Mocker);
            builder.source_path(model_path.clone());
            LocalModel::fetch(&model_path.display().to_string(), ignore_weights)
                .await.map_err(to_pyerr)?
        };
        builder.model_path(local_path);
    }
    let local_model = builder.build().await.map_err(to_pyerr)?;
    let inner = select_engine(distributed_runtime, args, local_model)
        .await.map_err(to_pyerr)?;
    Ok(EngineConfig { inner })
})
```

两个细节：`future_into_py` 让整个装配对 Python 呈现为一个 awaitable（所以 main.py 里是 `await make_engine(...)`）；`ignore_weights` 让 mocker 只下载 tokenizer 配置而不拉几个 GB 的权重——这是 mocker 能做无 GPU 全链路的前提之一。

`select_engine` 的 Dynamic 分支是本讲主角：

[lib/bindings/python/rust/llm/entrypoint.rs:L863-L897](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/llm/entrypoint.rs#L863-L897)

```rust
EngineType::Dynamic => {
    //  Convert Python chat engine factory to Rust callback
    let chat_engine_factory = args.chat_engine_factory.map(py_engine_factory_to_callback);
    let prefill_load_estimator = args.aic_perf_config.as_ref().map(|config| {
        Python::with_gil(|py| { create_aic_prefill_load_estimator(py, ...) })
    }).transpose()?;
    RsEngineConfig::Dynamic {
        model: Box::new(local_model),
        chat_engine_factory,
        prefill_load_estimator,
    }
}
```

注意此刻**什么网络组件都没建**——没有 Client、没有 KvRouter、没有 HTTP 服务。`Dynamic` 的装配是惰性的：真正的组件要等到 watcher 在服务发现里看到第一个 worker 才生成（见 4.3.3）。

回调桥接函数 `py_engine_factory_to_callback` 展示了「Python 异步函数 → Rust 闭包」的标准姿势：

[lib/bindings/python/rust/llm/entrypoint.rs:L790-L848](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/llm/entrypoint.rs#L790-L848) — 核心三步：`Python::with_gil` 中把三个参数包成 PyO3 对象并 `callback.call1(...)` 得到 coroutine；用注册时捕获的 `locals` 把 coroutine 转成 future（`into_future_with_locals`）；await 之后把结果 `extract` 成 `PythonAsyncEngine`（u2-l2 讲过它实现了 Rust 的 `AsyncEngine` trait）。

最后看 Python 调用方——frontend 主流程的三行关键代码：

[components/src/dynamo/frontend/main.py:L468-L469](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L468-L469)

```python
e = EntrypointArgs(EngineType.Dynamic, **kwargs)
engine = await make_engine(runtime, e)
```

`kwargs` 的组装在 [L418-L463](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L418-L463)：基础项（http_host/http_port/kv_cache_block_size/router_config/migration_limit/...）无条件放入；`chat_engine_factory` 只在 `--dyn-chat-processor vllm|sglang` 时注入（[L451-L463](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L451-L463)）。装配完成后交给 `run_input`（[L482-L488](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L482-L488)）。

#### 4.2.4 代码实践

**实践目标**：不动手改代码，纯靠阅读把「frontend 启动 → EngineConfig::Dynamic 诞生」这条链上的每一跳写出来，练成能追踪任意 PyO3 边界调用的能力。

**操作步骤**：

1. 打开 [components/src/dynamo/frontend/main.py:L355-L469](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/frontend/main.py#L355-L469)，从 `async_main()` 开始，列出到达 `make_engine` 之前的每一步函数调用。
2. 过桥后，在 [lib/bindings/python/rust/llm/entrypoint.rs](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/llm/entrypoint.rs) 中按顺序找到 `make_engine` → `LocalModelBuilder` → `select_engine` 的行号。
3. 为每一跳记一行笔记，格式：`文件:函数(关键参数) → 产物`。参考答案骨架（自己补全行号）：

```text
main.py:async_main()
  → build_router_config(config)                 → PyO3 RouterConfig
  → EntrypointArgs(EngineType.Dynamic, kwargs)  → PyO3 信封
  → make_engine(runtime, e)                     → 过桥
    → entrypoint.rs:make_engine  builder 链     → LocalModelBuilder
    → future_into_py: LocalModel::fetch         → 本地模型目录
    → builder.build()                           → LocalModel
    → select_engine (Dynamic 分支)               → RsEngineConfig::Dynamic
  → run_input(runtime, "http", engine, exts)    → 进入 4.3 的 Input 分派
```

4. 自查一个细节：frontend 从不传 `is_prefill` / `is_decode`（这两个默认 `false`）——想清楚为什么（提示：这两个标志属于 `InProcessTokens`，也就是 worker 侧的引擎形态；frontend 用的 `Dynamic` 根本用不到它们）。

**需要观察的现象**：无需运行，产物是你的笔记文本。

**预期结果**：链上不少于 8 跳，且每跳都能给出 `文件:行号`。行号以当前 HEAD 为准（本讲链接即该版本）。

#### 4.2.5 小练习与答案

**练习 1**：`make_engine` 为什么把模型下载放在 `future_into_py` 的异步体里，而不是在 Python 侧先下载好再传路径进来？

**答案**：下载可能耗时几十分钟（大模型权重），必须不阻塞 Python 事件循环；同时下载策略依赖 `engine_type`（Mocker 要 `ignore_weights=true` 只拉 tokenizer，见 [L766-L775](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/llm/entrypoint.rs#L766-L775)），这个知识只有 `select_engine` 一侧有。放在一起，Python 侧只需 `await` 一个结果。

**练习 2**：为什么 `chat_engine_factory` 必须在 `EntrypointArgs::new` 注册时就捕获 `TaskLocals`，而不能等回调被调用时再取？

**答案**：回调的真正调用时机在 watcher 发现新模型时（可能晚于注册很久，且跑在 Rust tokio 线程上）。那时 Python 事件循环的上下文未必能从当前线程拿到；`into_future_with_locals` 需要正确的 locals 才能把 coroutine 挂到原来的循环。源码在 [L638-L652](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/llm/entrypoint.rs#L638-L652) 捕获、[L827](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/bindings/python/rust/llm/entrypoint.rs#L827) 使用。

**练习 3**：sample 后端（`python3 -m dynamo.common.backend.sample_main`）有没有调用 `make_engine`？

**答案**：没有。sample worker 走的是另一条路：`sample_main.py` → `run(SampleLLMEngine)`（[components/src/dynamo/common/backend/sample_main.py:L10-L15](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/common/backend/sample_main.py#L10-L15)）→ 统一的 Rust `Worker`（[components/src/dynamo/common/backend/run.py:L26-L34](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/components/src/dynamo/common/backend/run.py#L26-L34)）。`make_engine` 是 frontend/独立引擎进程的装配入口；worker 只负责把自己注册进服务发现，等 frontend 用 `Dynamic` 引擎来发现它。**一个集群里通常只有一个进程跑 make_engine（frontend），而有 N 个 worker 进程各走各的注册路径。**

### 4.3 EngineDispatcher 与 Input：引擎的另一端接什么

#### 4.3.1 概念说明

`EngineConfig` 解决了「引擎在哪」，还剩两个问题：

1. **HTTP 服务有两种请求端点**（`/v1/completions` 与 `/v1/chat/completions`，请求/响应类型各不相同）。u3-l3 的 `AsyncEngine` 是单形态的——一个引擎类型只对应一对 Req/Resp。`StreamingEngine` trait 加上 `EngineDispatcher<E>` 适配器就是解法：让一个对象同时暴露 `handle_completion` 和 `handle_chat` 两个方法。
2. **引擎就位后接什么输入源？** `Input` 枚举给出五种选择：HTTP 服务器、KServe gRPC、交互式终端、stdin 单条 prompt、或从远端 endpoint 拉取（`dyn://namespace.component.endpoint`）。`run_input` 按枚举分派。

`EngineDispatcher<E>` 与 `StreamingEngineAdapter` 是一对方向相反的适配器：前者把「实现了多个 `AsyncEngine` impl 的 E」升格成 `StreamingEngine`；后者把 `dyn StreamingEngine` 拆回单个 `AsyncEngine` 以便塞进类型明确的管线。这是 Rust 里典型的「trait 对象与泛型边界互相翻译」手法。

#### 4.3.2 核心流程

`run_input` 的分派逻辑：

```text
run_input(drt, in_opt, engine_config)
  ├─ in_opt == Http   → http::run*  → HttpFrontend::run
  │     └─ match engine_config:
  │          Dynamic        → run_watcher（惰性：发现 worker 才装配管线）
  │          InProcessText  → StreamingEngineAdapter + 立即注册进 ModelManager
  │          InProcessTokens→ build_pipeline（preprocessor→backend→engine）+ 立即注册
  ├─ in_opt == Grpc   → grpc::run
  ├─ in_opt == Text   → text::run（交互终端）
  ├─ in_opt == Stdin  → text::run(drt, Some(prompt), ...)（读一条 prompt）
  └─ in_opt == Endpoint(path) → endpoint::run（作为 worker 从远端拉请求）
```

其中 `Dynamic` 分支的 watcher 发现模型后，组件生成顺序（对照 [watcher.rs:L560-L697](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/discovery/watcher.rs#L560-L697)）：

```text
worker 的 ModelDeploymentCard 出现
  ├─ needs_preprocessed_routing?（有 factory chat 管线 / 本地 tokenizer / generate 能力）
  ├─ 是且 router_mode == KV → 构造 KvRouter（kv_chooser）
  ├─ 是且 worker 类型 == Decode → 构造 PrefillRouter（prefill_chooser）
  ├─ 是 → 构造 EncoderRouter
  └─ build_preprocessed_routing(...)
       ├─ 等 min_initial_workers 个实例（默认 1；DYN_ROUTER_MIN_INITIAL_WORKERS 可调）
       ├─ LlmPushRouter::from_client_with_state(...)
       ├─ KV 模式 → RoutingHost::new_with_coordinator(router, chooser, affinity)
       │  否则    → RoutingHost::new_builtin_with_capabilities(router, affinity, lora)
       └─ 返回 PreprocessedRouting { backend_engine, prefill_router, encoder_router }
之后组装 chat 引擎：
  ├─ 有 chat_engine_factory → build_preprocessed_pipeline(...) 交给 Python 回调
  └─ 有本地 tokenizer      → build_pipeline(preprocessor, tk, ...)
```

#### 4.3.3 源码精读

先看 trait 与适配器：

[lib/llm/src/engines.rs:L109-L120](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/engines.rs#L109-L120)

```rust
pub trait StreamingEngine: Send + Sync {
    async fn handle_completion(
        &self, req: SingleIn<NvCreateCompletionRequest>,
    ) -> Result<ManyOut<Annotated<NvCreateCompletionResponse>>, Error>;

    async fn handle_chat(
        &self, req: SingleIn<NvCreateChatCompletionRequest>,
    ) -> Result<ManyOut<Annotated<NvCreateChatCompletionStreamResponse>>, Error>;
}
```

这正是 u3-l3 讲过的 `SingleIn<T> = Context<T>`、`ManyOut<T> = ResponseStream<T>` 判读法的直接应用——两个方法签名读作「一个进、流出一个」。

`EngineDispatcher` 的实现只是把两个方法分别转发给内层引擎对应的 `generate`：

[lib/llm/src/engines.rs:L450-L481](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/engines.rs#L450-L481) — `where` 子句要求 `E` 同时实现三个 `AsyncEngine`（completion / chat / embedding），`handle_completion` 与 `handle_chat` 都是一行 `self.inner.generate(req).await`。echo 引擎的出厂函数就是这两层的组合（[L131-L135](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/engines.rs#L131-L135)）：`EngineDispatcher::new(EchoEngine{})` 包成 `Arc<dyn StreamingEngine>`——也就是 `EngineConfig::InProcessText` 需要的类型。

再看 `Input` 枚举与分派：

[lib/llm/src/entrypoint/input.rs:L29-L45](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/entrypoint/input.rs#L29-L45)

```rust
pub enum Input {
    /// Run an OpenAI compatible HTTP server
    Http,
    /// Single prompt on stdin
    Stdin,
    /// Interactive chat
    Text,
    /// Pull requests from a namespace/component/endpoint path.
    Endpoint(String),
    // Run an KServe compatible gRPC server
    Grpc,
}
```

字符串解析规则在 [L55-L70](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/entrypoint/input.rs#L55-L70)：`"http"` / `"grpc"` / `"text"` / `"stdin"` 四个字面量，加上以 `dyn://` 开头的 endpoint 路径；`Default` 实现按「stdin 是不是终端」选 Text 或 Stdin（[L85-L93](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/entrypoint/input.rs#L85-L93)）——所以裸跑一个 Rust 二进制引擎时，管道输入走 Stdin、交互 shell 走 Text。

分派主体 `run_input_with_frontend_route_extensions`：

[lib/llm/src/entrypoint/input.rs:L124-L145](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/entrypoint/input.rs#L124-L145) — 五个 match 臂各自调用 `http::run*` / `grpc::run` / `text::run` / `endpoint::run`。非 HTTP 输入先做一次 `initialize_input`（请求追踪初始化，失败只告警不中断，见 [L147-L162](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/entrypoint/input.rs#L147-L162)）。

HTTP 这条臂最终落到 `HttpFrontend::run`，它对 `EngineConfig` 三分支的处理是本模块的收口：

[lib/llm/src/entrypoint/input/http.rs:L196-L239](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/entrypoint/input/http.rs#L196-L239) — `Dynamic` 分支先把 discovery client 塞给 builder（供 `/health` 查活跃实例），`build()` 出 `HttpService`，然后带着 `chat_engine_factory` / `prefill_load_estimator` / tokenizer 设置调 `run_watcher`。**注意此时 HTTP 服务里一个模型都没有**——`/v1/chat/completions` 要等 watcher 发现模型并回填。

[lib/llm/src/entrypoint/input/http.rs:L240-L276](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/entrypoint/input/http.rs#L240-L276) — 另两个分支是「立即注册」：`InProcessText` 用 `StreamingEngineAdapter::new(engine)` 包一层后 `manager.add_completions_model(...)` + `add_chat_completions_model(...)`；`InProcessTokens` 则先 `build_pipeline`（preprocessor → token backend → engine 的完整链）再注册。

watcher 里 chat 引擎的最终组装，就是 4.2 讲的回调被调用的地方：

[lib/llm/src/discovery/watcher.rs:L699-L717](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/discovery/watcher.rs#L699-L717)

```rust
let chat_engine = if let Some(ref factory) = self.chat_engine_factory {
    let routed_engine = routing
        .build_preprocessed_pipeline(card, self.migration_limit,
            self.migration_max_seq_len, self.metrics.clone())
        .context("PreprocessedRouting::build_preprocessed_pipeline")?;
    Some(factory(mcid.clone(), card.clone(), routed_engine)
        .await.context("python chat_engine_factory")?)
} else if let Some(tk) = tokenizer.clone() {
    // Rust 本地 tokenizer 路径：OpenAIPreprocessor + routing.build_pipeline(...)
    ...
};
```

两条路径二选一：**有 Python factory 就把 Rust 管线递过去；没有就用 Rust 自己的 preprocessor 建全链**。这就是「vllm/sglang processor」与「默认 frontend」两种部署形态在源码里的分岔点。

最后看两条管线的差别——`build_pipeline` 的完整算子链：

[lib/llm/src/entrypoint/input/common.rs:L500-L512](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/entrypoint/input/common.rs#L500-L512)

```rust
let engine = frontend
    .link(preprocessor_op.forward_edge())?
    .link(migration.forward_edge())?
    .link(token_backend.forward_edge())?
    .link(encoder_op.forward_edge())?
    .link(prefill_op.forward_edge())?
    .link(backend)?
    .link(prefill_op.backward_edge())?
    .link(encoder_op.backward_edge())?
    .link(token_backend.backward_edge())?
    .link(migration.backward_edge())?
    .link(preprocessor_op.backward_edge())?
    .link_terminal(frontend)?;
```

前向（请求）方向依次经过：preprocessor（模板+分词）→ migration（请求迁移）→ token backend → encoder router → prefill router → backend（真正的路由与推送）；反向（响应）沿原路回来做后处理。而 `build_preprocessed_pipeline`（[L538-L546](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/entrypoint/input/common.rs#L538-L546)）**没有 preprocessor、token_backend**——因为收到的已经是 `PreprocessedRequest`（token 块），预处理由 Python 侧的 factory 逻辑自己完成（u5-l2 的 prepost.py 就是那部分代码）。

#### 4.3.4 代码实践

**实践目标**：通过对比两条 pipeline 的算子链，弄清「谁负责分词」如何决定管线形状。

**操作步骤**：

1. 打开 [lib/llm/src/entrypoint/input/common.rs:L472-L549](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/entrypoint/input/common.rs#L472-L549)，把 `build_pipeline` 与 `build_preprocessed_pipeline` 的 `link(...)` 序列各抄成一列。
2. 逐个标注每个算子的方向（forward = 处理请求 / backward = 处理响应）与职责。
3. 回答：为什么 `build_preprocessed_pipeline` 里 `migration` 仍在、`preprocessor` 却没了？（提示：请求迁移发生在 token 层面，与文本格式无关；而分词只能做一次。）
4. 延伸一步：在 [watcher.rs:L759-L779](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/discovery/watcher.rs#L759-L779) 确认 `/v1/completions`（非 chat）端点**永远走 Rust preprocessor 路径**——即使 chat 用了 Python factory。想一条理由解释这个不对称（提示：completions 的 prompt 是纯文本，没有 chat 模板逻辑，Python 侧没有增值）。

**需要观察的现象**：无需运行；产物是两张算子链列表与两段文字解释。

**预期结果**：两条链共有的算子是 `migration / encoder / prefill / backend`，差异只在 `preprocessor + token_backend` 是否出现。第 4 步的结论应能在 watcher 源码注释 "completions always uses the Rust preprocessor" 处对上。

#### 4.3.5 小练习与答案

**练习 1**：为什么需要 `EngineDispatcher<E>` 和 `StreamingEngineAdapter` 两个方向相反的适配器，而不是只留一个？

**答案**：生产端（如 `make_echo_engine`）手里的 `EchoEngine` 天然是「多个 AsyncEngine impl 的同一结构体」，要变成能塞进 `EngineConfig::InProcessText` 的 `Arc<dyn StreamingEngine>`，需要 dispatcher 向上聚合；消费端（如 HttpFrontend 的 `InProcessText` 分支）的 ModelManager 按端点分别要 `AsyncEngine<Completion...>` 和 `AsyncEngine<Chat...>` 两种具体类型，需要 adapter 向下拆解。Rust 的 trait 对象与泛型各有边界，两个适配器就是边界的翻译器。

**练习 2**：`Input::Endpoint("dyn://...")` 和其他四个 Input 的本质区别是什么？

**答案**：方向相反。Http/Grpc/Text/Stdin 都是「本进程当服务端/入口，把 prompt 推进引擎」；Endpoint 是「本进程当 worker，从远端 endpoint 拉请求」（[input.rs:L40-L41](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/entrypoint/input.rs#L40-L41) 的注释 "Pull requests from a namespace/component/endpoint path"）。它对应 u3-l4 讲过的推送语义：worker 注册 ingress，等远端 frontend 把请求推过来。

**练习 3**：`HttpFrontend::run` 的 `Dynamic` 分支为什么不能像 `InProcessText` 那样立即 `add_chat_completions_model`？

**答案**：因为 `Dynamic` 的引擎在远端，此刻集群里可能一个 worker 都没有，连「模型存在」这个事实都还未知。它必须先起 `run_watcher` 订阅服务发现，等某个 worker 的 `ModelDeploymentCard` 出现、`build_preprocessed_routing` 等 `min_initial_workers` 个实例就绪后，才能构造 chat 引擎并注册（[watcher.rs:L679-L755](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/discovery/watcher.rs#L679-L755)）。所以 Dynamic frontend 的启动日志会先出现 "Waiting for remote model"，这就是那条路径。

## 5. 综合实践：『参数 → 生成组件』对照表

本讲的综合实践把三个模块串起来：**用不同参数组合启动 sample 后端，预测并验证 frontend 内部各生成了哪些组件**。

### 5.1 准备

环境要求与 u1-l2 相同：装好 `ai-dynamo`（容器或 PyPI），本地无 etcd 时给 frontend 和 worker 都加 `--discovery-backend file`。sample 后端无需 GPU。

先明确一个容易踩的坑：**agg.sh / disagg.sh 只把 `--model-name` 之外的多余参数转发给 worker，不会传给 frontend**。看 [examples/backends/sample/launch/agg.sh:L17-L49](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/examples/backends/sample/launch/agg.sh#L17-L49)：`EXTRA_ARGS` 只出现在 `sample_main` 的命令行上，frontend 是裸起的 `python3 -m dynamo.frontend &`。所以要改 frontend 的 router-mode，必须手动分进程启动。

### 5.2 操作步骤

依次跑三种组合（每种做完 `Ctrl-C` 清理干净再换下一种；无 etcd 时每条 python 命令都加 `--discovery-backend file`）：

**组合 A —— agg + 默认 round-robin**（直接用脚本）：

```bash
examples/backends/sample/launch/agg.sh
# 验证：curl http://localhost:8000/v1/chat/completions -H "Content-Type: application/json" \
#   -d '{"model":"sample-model","messages":[{"role":"user","content":"hi"}],"stream":true}'
```

**组合 B —— agg + kv 路由**（手动分进程，frontend 换模式）：

```bash
python3 -m dynamo.frontend --router-mode kv &        # 步骤 1：frontend 用 KV 模式
python3 -m dynamo.common.backend.sample_main --model-name sample-model &   # 步骤 2：聚合 worker
```

**组合 C —— disagg（prefill + decode）**（直接用脚本）：

```bash
examples/backends/sample/launch/disagg.sh
```

对照源码：disagg.sh 的两个 worker 分别带 `--component sample-prefill --disaggregation-mode prefill` 和 `--component sample-decode --disaggregation-mode decode`（[examples/backends/sample/launch/disagg.sh:L61-L74](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/examples/backends/sample/launch/disagg.sh#L61-L74)）。

### 5.3 需要观察的现象

每种组合下记录：

1. frontend 日志中 `Connected to ...` 与 `Chat completions is ready` 出现的时机（组合 A/B 在 worker 起来后；组合 C 要等 decode worker 也就位）。
2. 组合 B 的 frontend 是否多出 KV 路由相关日志/指标（`dynamo_router_*`）。
3. 组合 C 中请求的响应 token 是否先出现 prefill 侧的一条、再由 decode 侧续出（sample 引擎的合成 `disaggregated_params` 交接，见 disagg.sh 头部注释）。

### 5.4 预期结果：对照表

以下「生成的组件」列是**从源码推导的预测**（依据 [watcher.rs:L560-L697](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/discovery/watcher.rs#L560-L697) 与 [common.rs:L227-L315](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/entrypoint/input/common.rs#L227-L315)），具体日志表现**待本地验证**：

| 组合 | 关键参数 | frontend 侧生成的组件 | worker 侧 |
|------|----------|------------------------|-----------|
| A | frontend 默认（round-robin）；worker 无特殊参数 | `LlmPushRouter`（RoundRobin 模式）、`RoutingHost::new_builtin_with_capabilities`、`PrefillRouter`（disabled 分支）、无 `KvRouter` | 1 个 Aggregated worker（component `sample`） |
| B | frontend `--router-mode kv`；worker 同 A | `KvRouter`（kv_chooser，需要 `set_teardown_task_guard`）、`RoutingHost::new_with_coordinator`、`PrefillRouter` 仍为 disabled（worker 是聚合型，非 Decode） | 同 A |
| C | worker 分别 `--disaggregation-mode prefill` / `decode` | 对 decode worker 集：`PrefillRouter`（启用，构造条件 `WorkerType::Decode`）、`KvWorkerMonitor`；`EncoderRouter`；对 prefill worker 集另有独立的一套 routing | `sample-prefill` + `sample-decode` 两个 component |

填表时的自查问题（答案在源码注释里）：

- 组合 C 里为什么 monitor 必须与 KvRouter 共用同一个 `Client`？（[watcher.rs:L610-L614](https://github.com/ai-dynamo/dynamo/blob/7feb2b81be941e184314b029c7a43b8a221b5de8/lib/llm/src/discovery/watcher.rs#L610-L614)：每个 `Client::new()` 有独立 ArcSwap 状态，用不同的 client 会让 PushRouter 永远看不到过载状态。）
- 组合 A 的 frontend 能不能带 `--router-mode direct`？能，但 `Direct` 模式要求请求显式指定目标 worker，行为属于 u3-l4 讲过的「外部决策」组。

### 5.5 交付物

一张填完的对照表 + 三段日志摘录（每组合一段，标出证明组件生成的那几行）。

## 6. 本讲小结

- 装配 Dynamo runner 的两个正交决策：`EngineConfig`（引擎在本进程还是远端）与 `RouterConfig`（请求怎么分发）；`enforce_disagg` 已废弃，拓扑由 worker 注册的类型决定。
- `EngineConfig` 三变体：`Dynamic`（远端 + 可选 Python `chat_engine_factory` 回调 + AIC 负载估计器）、`InProcessText`（引擎自己分词，echo 用）、`InProcessTokens`（框架分词，mocker 用，带 `is_prefill/is_decode`）。
- Python 到 Rust 的装配路径：`EntrypointArgs(EngineType.Dynamic, ...)` → `make_engine` → `LocalModelBuilder` 链 → `LocalModel::fetch`（mocker 忽略权重）→ `select_engine` → `EngineConfig`；`chat_engine_factory` 靠注册时捕获的 `TaskLocals` 完成异步回调桥接。
- `Dynamic` 是惰性装配：`make_engine` 阶段不建任何网络组件；组件在 watcher 发现 worker 后生成——KV 模式建 `KvRouter`，Decode worker 集建 `PrefillRouter`，最后 `build_preprocessed_routing` 组出 `RoutingHost`。
- `EngineDispatcher` / `StreamingEngineAdapter` 是一对方向相反的适配器，解决「一个引擎同时服务 completions 与 chat 两种端点」；`Input` 枚举（Http/Grpc/Text/Stdin/Endpoint）决定引擎就位后接什么，其中 `Endpoint` 方向相反（本进程当 worker 被拉取）。
- 有 Python factory 时 chat 管线走 `build_preprocessed_pipeline`（无 preprocessor/token_backend，预处理在 Python）；否则走全 Rust 的 `build_pipeline`；completions 端点永远走 Rust preprocessor。

## 7. 下一步学习建议

- **u4-l2（HttpService）**：本讲只到「HttpFrontend 把引擎接给 HTTP 服务」为止；下一讲深入 `service_v2.rs` 的 `HttpService` 内部——路由表、`InflightPermit` 并发控制、OpenAI 端点处理器。
- **u4-l3（preprocessor 与 LocalModel）**：本讲反复出现的 `LocalModel`、`ModelDeploymentCard`、`OpenAIPreprocessor` 与 token 块化，在那里展开。
- **u4-l4（worker 类型与 discovery）**：本讲站在 frontend 视角看「watcher 发现了 worker」；下一面镜子从 worker 侧看 `WorkerType` / `WorkerSet` / `ModelManager` 如何维护动态集合。
- 若你想先看 Python 侧的另一半：**u5-l1（frontend main 全流程）** 与 **u5-l2（prepost.py）** 讲的就是 `chat_engine_factory` 在 Python 端的实现（`EngineFactory.chat_engine_factory`）。
