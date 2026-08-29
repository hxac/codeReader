# 引擎装配：entrypoint 与 EngineConfig

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `EngineConfig` 三个变体（`Dynamic` / `InProcessText` / `InProcessTokens`）各自的含义与适用场景，以及 `RouterConfig` 如何与之正交组合出不同拓扑。
2. 完整追踪一次 `make_engine` 调用：从 Python 侧的 `EntrypointArgs(EngineType.Dynamic, ...)` 出发，跨过 PyO3 边界，到达 Rust 侧的 `LocalModelBuilder` → `select_engine` → `EngineConfig`。
3. 解释 `EngineType.Dynamic` 如何通过 `chat_engine_factory` 回调把「引擎逻辑留在 Python、路由与发现留在 Rust」这一分工落地。
4. 看懂 `EngineDispatcher` / `StreamingEngineAdapter` 这对适配器，以及 `Input` 枚举如何决定引擎就位之后接什么（HTTP / gRPC / 终端 / Endpoint）。
5. 说出装配层如何与 **`RoutingLoadContext`** 衔接：本轮 `#13861`（own load state per routing context）重构之后，`build_preprocessed_routing` 不再接收 `Option<KvWorkerMonitor>`，而是接收一个把「共享 Client、过载阈值、调度负载通道、取消子树」全部收拢的自持有负载上下文。

本讲是第 4 单元（lib/llm 引擎层）的第一篇：先把「装配」这条骨架立起来，后续讲义（HttpService、preprocessor、discovery）都是挂在这条骨架上的器官。

## 2. 前置知识

本讲假设你已读过以下两讲（依赖：u3-l3、u2-l1），这里只做最小回顾：

- **AsyncEngine 抽象（u3-l3）**：Rust 侧一切引擎都实现 `AsyncEngine<Req, Resp, E>`，只有一个 `generate` 方法；配合 `SingleIn` / `ManyOut` 别名可判读引擎签名；`Context<T>` 信封携带取消信号。本讲的 `EngineDispatcher` 就是在这层抽象之上做的「多端点分发」适配器。
- **dynamo.runtime 三对象（u2-l1）**：Python 侧的 `DistributedRuntime` / `Endpoint` / `Client`，以及 namespace → component → endpoint 的服务目录。本讲的 `make_engine` 就是从 Python 世界进入这个分布式世界的正门。
- **PyO3 桥接（u2-l2，可选但推荐）**：`#[pyclass]` 薄壳 + inner 持有 Rust 结构体的模式；`future_into_py` 把 Rust async 函数变成 Python awaitable。本讲会大量遇到这两招。
- **请求面 Pipeline（u3-l4，建议）**：`PushRouter` 的选点/过载检查语义。本讲 4.4 会用到其中的 `OverloadCheck` 概念。

另外补充几个本讲反复出现的名词：

- **worker**：真正执行推理的进程（vLLM / SGLang / TRT-LLM / mocker / sample）。frontend 不执行推理，只做 HTTP 接入、预处理和路由。
- **frontend**：对外暴露 OpenAI 兼容 API 的进程。它自己没有引擎，靠服务发现找到 worker——这正是 `EngineConfig::Dynamic` 存在的理由。
- **路由上下文（routing context）**：本讲新引入的术语，指「一个有类型的目标端点（typed endpoint）所对应的一整套选点与推送设施」。一个 frontend 同时服务 decode 池、prefill 池、encode 池时，就有多个互相独立的路由上下文。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [lib/llm/src/entrypoint.rs](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/entrypoint.rs) | 装配层的「图纸定义」：`RouterConfig` 与 `EngineConfig` 两个核心类型，以及 `ChatEngineFactoryCallback` 回调类型 |
| [lib/llm/src/entrypoint/input.rs](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/entrypoint/input.rs) | `Input` 枚举与 `run_input`：引擎就位后接什么输入源 |
| [lib/llm/src/entrypoint/input/common.rs](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/entrypoint/input/common.rs) | 装配的「施工队」：`build_preprocessed_routing`、`prepare_engine`、`PreprocessedRouting::build_pipeline`。本轮重构把它的 `worker_monitor` 参数换成了 `load_context` |
| [lib/llm/src/entrypoint/input/http.rs](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/entrypoint/input/http.rs) | `HttpFrontend`：把 `EngineConfig` 三分支分别接成 HTTP 服务 |
| [lib/llm/src/engines.rs](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/engines.rs) | `StreamingEngine` trait、`EngineDispatcher` / `StreamingEngineAdapter` 适配器、echo 引擎 |
| [lib/bindings/python/rust/llm/entrypoint.rs](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/bindings/python/rust/llm/entrypoint.rs) | PyO3 侧：`EngineType`、`EntrypointArgs`、`make_engine`、`select_engine` |
| [lib/llm/src/discovery/watcher.rs](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/watcher.rs) | 模型发现后的组件生成：何时建 KvRouter / PrefillRouter、何时调用 `chat_engine_factory`，以及**在哪里创建 `RoutingLoadContext`** |
| [lib/llm/src/kv_router/routing_load.rs](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/kv_router/routing_load.rs) | **本轮新增（470 行）**：`RoutingLoadContext` / `RouterLoadSource` / `SchedulerLoadSender`，把每个路由上下文的负载生命周期收拢成一个自持有对象 |
| [lib/llm/src/kv_router/routing_host.rs](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/kv_router/routing_host.rs) | `RoutingHost`：新增 `new_with_load_context*` 构造器族，用 `routing_context` 字段保活负载上下文 |
| [lib/llm/src/discovery/worker_monitor.rs](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/worker_monitor.rs) | `LoadThresholdConfig` / `LoadThresholdHandle` 与简化后的 `KvWorkerMonitor` |
| [lib/bindings/python/rust/llm/kv.rs](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/bindings/python/rust/llm/kv.rs) | PyO3 侧 `LoadThresholdConfig`（`dynamo.llm` 可直接构造） |
| [components/src/dynamo/frontend/main.py](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/components/src/dynamo/frontend/main.py) | Python 前端主流程，`make_engine` 的头号调用方 |
| [examples/backends/sample/launch/agg.sh](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/examples/backends/sample/launch/agg.sh) / [disagg.sh](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/examples/backends/sample/launch/disagg.sh) | 综合实践用的两种启动拓扑 |

## 4. 核心概念与源码讲解

先建立一个直觉：**装配一个 Dynamo runner 需要回答两个正交的问题**。

1. **引擎在哪？**（`EngineConfig`）——推理引擎是在远端 worker 进程里（`Dynamic`），还是就在本进程里（`InProcessText` / `InProcessTokens`）？
2. **请求怎么走？**（`RouterConfig`）——frontend 内部用哪种 `RouterMode` 把请求分给 worker？

这两个决策互相独立：同一个远端 worker 集合，可以配 round-robin，也可以配 KV 感知路由。装配层（entrypoint）的全部工作，就是把这两个决策连同杂项参数（HTTP 端口、tokenizer、TLS……）收集起来，在合适的时机把组件「长」出来。

本轮 `#13861` 又给这条骨架加了一根新的「血管」：每个被装配出来的路由管线，现在都挂着一个 `RoutingLoadContext`，负责该管线的负载状态生命周期（见 4.4）。

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

`RouterConfig` 则是路由侧的配置包：`router_mode`（七种模式）+ `kv_router_config`（KV 路由的几十个细调参数）+ `load_threshold_config`（**过载检测阈值，本轮重构的主角之一**）+ `session_affinity_ttl_secs`。注意其中 `enforce_disagg` 已被标记废弃——**拓扑与就绪判断现在来自 worker 注册的类型**，不再由 frontend 强制。

`load_threshold_config` 是三个可选阈值：`active_decode_blocks_threshold`（KV 块利用率 0.0–1.0）、`active_prefill_tokens_threshold`（字面 token 数）、`active_prefill_tokens_threshold_frac`（占 `max_num_batched_tokens` 的比例）。三个全空表示过载拒绝完全关闭。

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
       ├─ 需要预处理路由？→ 先建 RoutingLoadContext（本轮新增，见 4.4）
       ├─ router_mode == KV 且需要路由 → KvRouter（kv_chooser）
       ├─ worker 类型是 Decode → PrefillRouter（prefill_chooser）
       └─ 总是 → build_preprocessed_routing 组装 RoutingHost
  → 若有 chat_engine_factory：调用它，把 Rust 路由管线交给 Python 引擎
```

`RouterConfig` 的旅程则更简单：它被塞进 `LocalModelBuilder`，随 `LocalModel` 一路传到 watcher，在那里既决定 `kv_chooser` / `prefill_chooser` 的构造参数，也决定 `RoutingLoadContext` 拿到的负载阈值。

#### 4.1.3 源码精读

先看 `RouterConfig` 的定义——注意第 50-52 行对 `enforce_disagg` 的注释：

[lib/llm/src/entrypoint.rs#L44-L55](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/entrypoint.rs#L44-L55)

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

`LoadThresholdConfig` 本体定义在 discovery 模块——三个字段全部 `Option`，且注释写明「全空 = 过载拒绝完全关闭」：

[lib/llm/src/discovery/worker_monitor.rs#L92-L113](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/worker_monitor.rs#L92-L113) — `active_decode_blocks_threshold`（0.0–1.0）、`active_prefill_tokens_threshold`（绝对 token 数）、`active_prefill_tokens_threshold_frac`（占 `max_num_batched_tokens` 比例）。

再看 `EngineConfig` 枚举本体：

[lib/llm/src/entrypoint.rs#L87-L109](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/entrypoint.rs#L87-L109)

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

三个变体共享 `model: Box<LocalModel>`——所以枚举提供了统一访问器 `local_model()`（[L111-L119](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/entrypoint.rs#L111-L119)），后续所有消费方（HTTP builder、watcher）都从这里取模型与路由配置。`InProcessTokens` 额外带 `is_prefill` / `is_decode` 两个布尔——这就是 mocker 模拟 P/D 分离角色的开关。

回调类型 `ChatEngineFactoryCallback` 是一个 `Arc<dyn Fn(...) -> Pin<Box<dyn Future>>>`：

[lib/llm/src/entrypoint.rs#L33-L42](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/entrypoint.rs#L33-L42)

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

PyO3 侧还有一个 Python 包装版 `RouterConfig`，构造时做参数校验，再经 `From` 转换为 Rust 版本。注意它的签名：**三个负载阈值是直接作为构造参数传入的**（而不是一个 `LoadThresholdConfig` 对象）：

[lib/bindings/python/rust/llm/entrypoint.rs#L489-L526](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/bindings/python/rust/llm/entrypoint.rs#L489-L526) — `RouterConfig::new` 在 `enforce_disagg=true` 时打印一次性告警（[L498-L505](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/bindings/python/rust/llm/entrypoint.rs#L498-L505)）、校验 `session_affinity_ttl_secs` 范围 1..=31536000（[L506-L510](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/bindings/python/rust/llm/entrypoint.rs#L506-L510)），最后把三个阈值字段拼成 `RsLoadThresholdConfig` 并 `.validate()`。

[lib/bindings/python/rust/llm/entrypoint.rs#L529-L543](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/bindings/python/rust/llm/entrypoint.rs#L529-L543)

```rust
impl From<RouterConfig> for RsRouterConfig {
    fn from(rc: RouterConfig) -> RsRouterConfig {
        RsRouterConfig {
            router_mode: rc.router_mode.into(),
            kv_router_config: rc.kv_router_config.inner,
            load_threshold_config: RsLoadThresholdConfig {
                active_decode_blocks_threshold: rc.active_decode_blocks_threshold,
                active_prefill_tokens_threshold: rc.active_prefill_tokens_threshold,
                active_prefill_tokens_threshold_frac: rc.active_prefill_tokens_threshold_frac,
            },
            enforce_disagg: false,          // 永远置 false：拓扑由 worker 类型决定
            session_affinity_ttl_secs: rc.session_affinity_ttl_secs,
        }
    }
}
```

注意 `enforce_disagg: false` 是硬编码的——即使 Python 侧传了 `true`，跨过桥之后也会被丢弃，与 Rust 侧的废弃标注呼应。

#### 4.1.4 代码实践

**实践目标**：亲手构造一次 `RouterConfig` 与 `LoadThresholdConfig`，观察参数校验行为，建立「Python 侧配置 → Rust 侧图纸」的手感。

**操作步骤**：

1. 在装好 `ai-dynamo` 的环境里（u1-l2 / u1-l4 的任一安装方式）执行以下脚本（示例代码，非项目原有文件）：

```python
# demo_router_config.py —— 示例代码
from dynamo.llm import KvRouterConfig, LoadThresholdConfig, RouterConfig, RouterMode

# 1) 合法构造：负载阈值直接作为 kwargs 传入 RouterConfig
rc = RouterConfig(RouterMode.KV, KvRouterConfig(),
                  active_decode_blocks_threshold=0.8)
print("mode:", rc.router_mode)

# 2) 独立的 LoadThresholdConfig：构造即校验
lt = LoadThresholdConfig(active_decode_blocks_threshold=0.75,
                         active_prefill_tokens_threshold=512)
print("thresholds:", lt.active_decode_blocks_threshold, lt.active_prefill_tokens_threshold)

# 3) 非法阈值：> 1.0
try:
    LoadThresholdConfig(active_decode_blocks_threshold=1.1)
except ValueError as e:
    print("被拒绝:", e)

# 4) 非法 ttl：低于 1
try:
    RouterConfig(RouterMode.KV, KvRouterConfig(), session_affinity_ttl_secs=0)
except ValueError as e:
    print("ttl=0 被拒绝:", e)

# 5) 废弃参数：enforce_disagg
import logging; logging.basicConfig(level=logging.WARNING)
RouterConfig(RouterMode.KV, KvRouterConfig(), enforce_disagg=True)
print("enforce_disagg 构造成功（但会被 Rust 侧丢弃）")
```

2. 对照源码确认行为出处：`LoadThresholdConfig` 的 PyO3 定义与校验在 [lib/bindings/python/rust/llm/kv.rs#L70-L101](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/bindings/python/rust/llm/kv.rs#L70-L101)（`invalid load threshold config: ...` 的报错前缀来自 [L114-L128](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/bindings/python/rust/llm/kv.rs#L114-L128)）；ttl 校验与 `enforce_disagg` 告警在 4.1.3 引用的 entrypoint.rs 两段。仓库自带等价断言：[lib/bindings/python/tests/test_load_threshold_config.py#L23-L43](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/bindings/python/tests/test_load_threshold_config.py#L23-L43)。

**需要观察的现象**：步骤 3 抛出 `ValueError`（文案以 "invalid load threshold config: active_decode_blocks_threshold must be between 0.0 and 1.0" 开头）；步骤 4 抛出 `ValueError`（"session_affinity_ttl_secs must be between 1 and 31536000"）；步骤 5 打印一条 "enforce_disagg is deprecated and ignored..." 的 WARNING。

**预期结果**：五步全部按上述通过/失败。若本机尚未安装 ai-dynamo，此实践可改为纯源码阅读：在 `_core.pyi`（[lib/bindings/python/src/dynamo/_core.pyi#L1702-L1745](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/bindings/python/src/dynamo/_core.pyi#L1702-L1745)）中找到 `RouterConfig` 与 `LoadThresholdConfig` 的类型标注，与上述 Rust 源码逐字段对照。运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `EngineConfig` 的三个变体都要携带 `LocalModel`，而不是把模型信息放在枚举外面？

**答案**：因为三个变体对模型的用法不同（`Dynamic` 用它拿 router_config 与 namespace 过滤器，`InProcessTokens` 用它的 tokenizer 和 card 建 pipeline），但所有下游消费方都需要「无论哪个变体都能取到模型」。枚举内嵌 + 统一访问器 `local_model()` 让消费方（如 [http.rs#L148](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/entrypoint/input/http.rs#L148) 第一行就是 `engine_config.local_model()`）无须 match 三次。

**练习 2**：`InProcessText` 和 `InProcessTokens` 都是进程内引擎，本质区别是什么？

**答案**：分词与模板的责任归属。`InProcessText` 的引擎收到的直接是文本（OpenAI 请求对象），自己做 tokenization 和 prompt formatting（echo 引擎甚至原样回显字符）；`InProcessTokens` 的引擎收到的已经是 `PreprocessedRequest`（token 块），框架在外面套 `OpenAIPreprocessor` / `Backend` 前后处理层——这正是 [common.rs#L508-L520](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/entrypoint/input/common.rs#L508-L520) `build_pipeline` 里 `frontend.link(preprocessor).link(backend).link(engine)...` 那条链存在的意义。

**练习 3**：如果一个部署里 frontend 配了 `--router-mode kv` 但所有 worker 都是聚合（Aggregated）角色，会发生什么？`RouterLoadSource` 会是哪个值？

**答案**：frontend 仍会为该 worker 集构造 `KvRouter`（条件是 `router_mode == KV && needs_preprocessed_routing`，见 [watcher.rs#L594-L633](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/watcher.rs#L594-L633)），但不会构造 `PrefillRouter`——它的构造条件是 worker 类型为 `Decode`（[watcher.rs#L639-L665](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/watcher.rs#L639-L665)），聚合 worker 走 `PrefillRouter::disabled_with_selector` 的默认分支（[common.rs#L297-L303](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/entrypoint/input/common.rs#L297-L303)）。KV 路由照常打分，只是没有 P/D 二级路由。负载上下文的 `RouterLoadSource` 由 `from_worker_type(Aggregated)` 得到 `Aggregated`（[routing_load.rs#L41-L48](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/kv_router/routing_load.rs#L41-L48)）——注意它和 `Decode` 共用同一个 metric 标签（都算「解码侧负载」）。

### 4.2 EntrypointArgs 与 make_engine：从 Python 到 Rust 的装配路径

#### 4.2.1 概念说明

`EngineConfig` 是 Rust 内部类型，Python 侧无法直接构造。跨界的信封是 `EntrypointArgs`——一个 PyO3 类，把「引擎类型 + 模型信息 + 路由配置 + HTTP/TLS/tokenizer 等约三十个参数」打包成一个对象送过桥。`make_engine(distributed_runtime, args)` 则是装配总入口：**解析信封 → 构建 LocalModel → 按 EngineType 选出引擎形态 → 返回包装好的 `EngineConfig`**。

`EngineType` 是用户在 Python 侧真正写的枚举，只有三个值：

[lib/bindings/python/rust/llm/entrypoint.rs#L69-L73](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/bindings/python/rust/llm/entrypoint.rs#L69-L73)

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

[lib/bindings/python/rust/llm/entrypoint.rs#L562-L588](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/bindings/python/rust/llm/entrypoint.rs#L562-L588)

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

注意 `chat_engine_factory` 的类型是 `PyEngineFactory` 而不是裸 `PyObject`——这是为了在注册时捕获 `TaskLocals`（Python 事件循环上下文），见 [L638-L652](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/bindings/python/rust/llm/entrypoint.rs#L638-L652)：如果晚到调用时才取 locals，回调可能跑在另一个不同的循环上下文里。

`make_engine` 的同步前半段是纯 builder 链：

[lib/bindings/python/rust/llm/entrypoint.rs#L732-L761](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/bindings/python/rust/llm/entrypoint.rs#L732-L761)

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

`router_config` 在这里完成了 `From` 转换进 builder——4.1 练习 1 里说的「图纸随 LocalModel 旅行」就是从这一行开始的。**负载阈值也在这一刻进入 `LocalModel`，最终被 watcher 用来造 `LoadThresholdHandle`（见 4.4）。**

异步后半段做模型下载与引擎选择：

[lib/bindings/python/rust/llm/entrypoint.rs#L762-L786](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/bindings/python/rust/llm/entrypoint.rs#L762-L786)

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

两个细节：`future_into_py` 让整个装配对 Python 呈现为一个 awaitable（所以 main.py 里是 `await make_engine(...)`）；`ignore_weights`（[L767-L777](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/bindings/python/rust/llm/entrypoint.rs#L767-L777)）让 mocker 只下载 tokenizer 配置而不拉几个 GB 的权重——这是 mocker 能做无 GPU 全链路的前提之一。

`select_engine` 的 Dynamic 分支是本讲主角：

[lib/bindings/python/rust/llm/entrypoint.rs#L863-L897](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/bindings/python/rust/llm/entrypoint.rs#L863-L897)

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

注意此刻**什么网络组件都没建**——没有 Client、没有 KvRouter、没有 HTTP 服务，也没有 `RoutingLoadContext`。`Dynamic` 的装配是惰性的：真正的组件要等到 watcher 在服务发现里看到第一个 worker 才生成（见 4.3.3 与 4.4）。

回调桥接函数 `py_engine_factory_to_callback` 展示了「Python 异步函数 → Rust 闭包」的标准姿势：

[lib/bindings/python/rust/llm/entrypoint.rs#L790-L848](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/bindings/python/rust/llm/entrypoint.rs#L790-L848) — 核心三步：`Python::with_gil` 中把三个参数包成 PyO3 对象并 `callback.call1(...)` 得到 coroutine；用注册时捕获的 `locals` 把 coroutine 转成 future（`into_future_with_locals`，[L826-L828](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/bindings/python/rust/llm/entrypoint.rs#L826-L828)）；await 之后把结果 `extract` 成 `PythonAsyncEngine`（u2-l2 讲过它实现了 Rust 的 `AsyncEngine` trait）。

最后看 Python 调用方——frontend 主流程的三行关键代码：

[components/src/dynamo/frontend/main.py#L468-L469](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/components/src/dynamo/frontend/main.py#L468-L469)

```python
e = EntrypointArgs(EngineType.Dynamic, **kwargs)
engine = await make_engine(runtime, e)
```

`kwargs` 的组装在 [L418-L432](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/components/src/dynamo/frontend/main.py#L418-L432)：基础项（http_host/http_port/kv_cache_block_size/router_config/migration_limit/...）无条件放入；`chat_engine_factory` 只在 `--dyn-chat-processor vllm|sglang` 时注入（[L451-L463](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/components/src/dynamo/frontend/main.py#L451-L463)）。注意 `router_config` 本身在 [L411](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/components/src/dynamo/frontend/main.py#L411) 由 `build_router_config(config)` 生成，而它内部会原样转发四个字段——其中就包括三个负载阈值（[components/src/dynamo/common/configuration/groups/router_args.py#L34-L40](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/components/src/dynamo/common/configuration/groups/router_args.py#L34-L40)）。装配完成后交给 `run_input`（[L482-L488](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/components/src/dynamo/frontend/main.py#L482-L488)）。

#### 4.2.4 代码实践

**实践目标**：不动手改代码，纯靠阅读把「frontend 启动 → EngineConfig::Dynamic 诞生」这条链上的每一跳写出来，练成能追踪任意 PyO3 边界调用的能力。

**操作步骤**：

1. 打开 [components/src/dynamo/frontend/main.py#L405-L469](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/components/src/dynamo/frontend/main.py#L405-L469)，从 `async_main()` 的后半段开始，列出到达 `make_engine` 之前的每一步函数调用。
2. 过桥后，在 [lib/bindings/python/rust/llm/entrypoint.rs](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/bindings/python/rust/llm/entrypoint.rs) 中按顺序找到 `make_engine` → `LocalModelBuilder` → `select_engine` 的行号。
3. 为每一跳记一行笔记，格式：`文件:函数(关键参数) → 产物`。参考答案骨架（自己补全行号）：

```text
main.py:async_main()
  → build_router_config(config)                 → PyO3 RouterConfig（含三个负载阈值）
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

**答案**：下载可能耗时几十分钟（大模型权重），必须不阻塞 Python 事件循环；同时下载策略依赖 `engine_type`（Mocker 要 `ignore_weights=true` 只拉 tokenizer，见 [L767-L777](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/bindings/python/rust/llm/entrypoint.rs#L767-L777)），这个知识只有 `select_engine` 一侧有。放在一起，Python 侧只需 `await` 一个结果。

**练习 2**：为什么 `chat_engine_factory` 必须在 `EntrypointArgs::new` 注册时就捕获 `TaskLocals`，而不能等回调被调用时再取？

**答案**：回调的真正调用时机在 watcher 发现新模型时（可能晚于注册很久，且跑在 Rust tokio 线程上）。那时 Python 事件循环的上下文未必能从当前线程拿到；`into_future_with_locals` 需要正确的 locals 才能把 coroutine 挂到原来的循环。源码在 [L638-L652](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/bindings/python/rust/llm/entrypoint.rs#L638-L652) 捕获、[L826-L828](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/bindings/python/rust/llm/entrypoint.rs#L826-L828) 使用。

**练习 3**：sample 后端（`python3 -m dynamo.common.backend.sample_main`）有没有调用 `make_engine`？

**答案**：没有。sample worker 走的是另一条路：`sample_main.py` → `run(SampleLLMEngine)`（[components/src/dynamo/common/backend/sample_main.py#L10-L15](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/components/src/dynamo/common/backend/sample_main.py#L10-L15)）→ 统一的 Rust `Worker`（[components/src/dynamo/common/backend/run.py#L37-L52](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/components/src/dynamo/common/backend/run.py#L37-L52)）。`make_engine` 是 frontend/独立引擎进程的装配入口；worker 只负责把自己注册进服务发现，等 frontend 用 `Dynamic` 引擎来发现它。**一个集群里通常只有一个进程跑 make_engine（frontend），而有 N 个 worker 进程各走各的注册路径。**

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

其中 `Dynamic` 分支的 watcher 发现模型后，组件生成顺序（对照 [watcher.rs#L563-L704](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/watcher.rs#L563-L704)）：

```text
worker 的 ModelDeploymentCard 出现
  ├─ needs_preprocessed_routing?（有 factory chat 管线 / 本地 tokenizer / generate 能力）
  ├─ 是 → 先建 RoutingLoadContext（★ 本轮新增，见 4.4）
  ├─ 是且 router_mode == KV → 构造 KvRouter（kv_chooser，复用 load_context 的 client）
  ├─ 是且 worker 类型 == Decode → 构造 PrefillRouter（prefill_chooser）
  ├─ 是 → 构造 EncoderRouter
  └─ build_preprocessed_routing_with_selector(..., load_context, ...)
       ├─ 等 min_initial_workers 个实例（默认 1；DYN_ROUTER_MIN_INITIAL_WORKERS 可调）
       ├─ LlmPushRouter::from_client_with_state(...)
       ├─ KV 模式 → RoutingHost::new_with_load_context_and_coordinator(router, chooser, load_context, affinity)
       │  否则    → RoutingHost::new_builtin_with_capabilities(router, load_context, affinity, lora)
       └─ 返回 PreprocessedRouting { backend_engine, prefill_router, encoder_router }
之后组装 chat 引擎：
  ├─ 有 chat_engine_factory → build_preprocessed_pipeline(...) 交给 Python 回调
  └─ 有本地 tokenizer      → build_pipeline(preprocessor, tk, ...)
```

#### 4.3.3 源码精读

先看 trait 与适配器：

[lib/llm/src/engines.rs#L108-L120](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/engines.rs#L108-L120)

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

[lib/llm/src/engines.rs#L450-L481](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/engines.rs#L450-L481) — `where` 子句要求 `E` 同时实现三个 `AsyncEngine`（completion / chat / embedding），`handle_completion` 与 `handle_chat` 都是一行 `self.inner.generate(req).await`。echo 引擎的出厂函数就是这两层的组合（[L131-L135](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/engines.rs#L131-L135)）：`EngineDispatcher::new(EchoEngine{})` 包成 `Arc<dyn StreamingEngine>`——也就是 `EngineConfig::InProcessText` 需要的类型。

再看 `Input` 枚举与分派：

[lib/llm/src/entrypoint/input.rs#L28-L45](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/entrypoint/input.rs#L28-L45)

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

字符串解析规则在 [L55-L70](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/entrypoint/input.rs#L55-L70)：`"http"` / `"grpc"` / `"text"` / `"stdin"` 四个字面量，加上以 `dyn://` 开头的 endpoint 路径；`Default` 实现按「stdin 是不是终端」选 Text 或 Stdin（[L85-L93](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/entrypoint/input.rs#L85-L93)）——所以裸跑一个 Rust 二进制引擎时，管道输入走 Stdin、交互 shell 走 Text。

分派主体 `run_input_with_frontend_route_extensions` 的五个 match 臂：

[lib/llm/src/entrypoint/input.rs#L124-L143](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/entrypoint/input.rs#L124-L143) — 五个 match 臂各自调用 `http::run*` / `grpc::run` / `text::run` / `endpoint::run`。非 HTTP 输入先做一次 `initialize_input`（请求追踪初始化，失败只告警不中断，见 [L147-L162](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/entrypoint/input.rs#L147-L162)）。

HTTP 这条臂最终落到 `HttpFrontend::run`，它对 `EngineConfig` 三分支的处理是本模块的收口：

[lib/llm/src/entrypoint/input/http.rs#L196-L239](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/entrypoint/input/http.rs#L196-L239) — `Dynamic` 分支先把 discovery client 塞给 builder（供 `/health` 查活跃实例），`build()` 出 `HttpService`，然后带着 `chat_engine_factory` / `prefill_load_estimator` / tokenizer 设置调 `run_watcher`。**注意此时 HTTP 服务里一个模型都没有**——`/v1/chat/completions` 要等 watcher 发现模型并回填。

[lib/llm/src/entrypoint/input/http.rs#L240-L276](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/entrypoint/input/http.rs#L240-L276) — 另两个分支是「立即注册」：`InProcessText` 用 `StreamingEngineAdapter::new(engine)` 包一层后 `manager.add_completions_model(...)` + `add_chat_completions_model(...)`；`InProcessTokens` 则先 `build_pipeline`（preprocessor → token backend → engine 的完整链）再注册。

watcher 里 chat 引擎的最终组装，就是 4.2 讲的回调被调用的地方：

[lib/llm/src/discovery/watcher.rs#L711-L724](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/watcher.rs#L711-L724)

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

两条路径二选一：**有 Python factory 就把 Rust 管线递过去；没有就用 Rust 自己的 preprocessor 建全链**（本地 tokenizer 分支在 [L725-L745](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/watcher.rs#L725-L745)）。这就是「vllm/sglang processor」与「默认 frontend」两种部署形态在源码里的分岔点。

最后看两条管线的差别——`build_pipeline` 的完整算子链：

[lib/llm/src/entrypoint/input/common.rs#L499-L520](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/entrypoint/input/common.rs#L499-L520)

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

前向（请求）方向依次经过：preprocessor（模板+分词）→ migration（请求迁移）→ token backend → encoder router → prefill router → backend（真正的路由与推送）；反向（响应）沿原路回来做后处理。而 `build_preprocessed_pipeline`（[L546-L554](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/entrypoint/input/common.rs#L546-L554)）**没有 preprocessor、token_backend**——因为收到的已经是 `PreprocessedRequest`（token 块），预处理由 Python 侧的 factory 逻辑自己完成（u5-l2 的 prepost.py 就是那部分代码）。

#### 4.3.4 代码实践

**实践目标**：通过对比两条 pipeline 的算子链，弄清「谁负责分词」如何决定管线形状。

**操作步骤**：

1. 打开 [lib/llm/src/entrypoint/input/common.rs#L479-L557](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/entrypoint/input/common.rs#L479-L557)，把 `build_pipeline` 与 `build_preprocessed_pipeline` 的 `link(...)` 序列各抄成一列。
2. 逐个标注每个算子的方向（forward = 处理请求 / backward = 处理响应）与职责。
3. 回答：为什么 `build_preprocessed_pipeline` 里 `migration` 仍在、`preprocessor` 却没了？（提示：请求迁移发生在 token 层面，与文本格式无关；而分词只能做一次。）
4. 延伸一步：在 [watcher.rs#L764-L787](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/watcher.rs#L764-L787) 确认 `/v1/completions`（非 chat）端点**永远走 Rust preprocessor 路径**——即使 chat 用了 Python factory。想一条理由解释这个不对称（提示：completions 的 prompt 是纯文本，没有 chat 模板逻辑，Python 侧没有增值）。

**需要观察的现象**：无需运行；产物是两张算子链列表与两段文字解释。

**预期结果**：两条链共有的算子是 `migration / encoder / prefill / backend`，差异只在 `preprocessor + token_backend` 是否出现。第 4 步的结论应能在 watcher 源码注释 "completions always uses the Rust preprocessor"（[L765](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/watcher.rs#L765)）处对上。

#### 4.3.5 小练习与答案

**练习 1**：为什么需要 `EngineDispatcher<E>` 和 `StreamingEngineAdapter` 两个方向相反的适配器，而不是只留一个？

**答案**：生产端（如 `make_echo_engine`）手里的 `EchoEngine` 天然是「多个 AsyncEngine impl 的同一结构体」，要变成能塞进 `EngineConfig::InProcessText` 的 `Arc<dyn StreamingEngine>`，需要 dispatcher 向上聚合；消费端（如 HttpFrontend 的 `InProcessText` 分支）的 ModelManager 按端点分别要 `AsyncEngine<Completion...>` 和 `AsyncEngine<Chat...>` 两种具体类型（[engines.rs#L525](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/engines.rs#L525) 的 adapter），需要 adapter 向下拆解。Rust 的 trait 对象与泛型各有边界，两个适配器就是边界的翻译器。

**练习 2**：`Input::Endpoint("dyn://...")` 和其他四个 Input 的本质区别是什么？

**答案**：方向相反。Http/Grpc/Text/Stdin 都是「本进程当服务端/入口，把 prompt 推进引擎」；Endpoint 是「本进程当 worker，从远端 endpoint 拉请求」（[input.rs#L40-L41](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/entrypoint/input.rs#L40-L41) 的注释 "Pull requests from a namespace/component/endpoint path"）。它对应 u3-l4 讲过的推送语义：worker 注册 ingress，等远端 frontend 把请求推过来。

**练习 3**：`HttpFrontend::run` 的 `Dynamic` 分支为什么不能像 `InProcessText` 那样立即 `add_chat_completions_model`？

**答案**：因为 `Dynamic` 的引擎在远端，此刻集群里可能一个 worker 都没有，连「模型存在」这个事实都还未知。它必须先起 `run_watcher` 订阅服务发现，等某个 worker 的 `ModelDeploymentCard` 出现、`build_preprocessed_routing` 等 `min_initial_workers` 个实例就绪后，才能构造 chat 引擎并注册（[watcher.rs#L684-L757](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/watcher.rs#L684-L757)）。所以 Dynamic frontend 的启动日志会先出现 "Waiting for remote model"，这就是那条路径。

### 4.4 装配层 × RoutingLoadContext：每个路由上下文自持负载状态（#13861）

#### 4.4.1 概念说明

这是本轮更新（PR #13861，"own load state per routing context"）带来的最大结构变化，直接改写了 `build_preprocessed_routing` 的签名。

**重构前的问题**：过载状态是「共享但靠约定」的。watcher 先造一个 `Option<KvWorkerMonitor>`，把它同时交给 prefill router 和 `build_preprocessed_routing`，后者再把它转成 `Arc<dyn WorkerLoadMonitor>` 塞进 `LlmPushRouter::from_client_with_state`。要让 PushRouter 能看到 KvRouter 记下的过载实例，**双方必须碰巧用同一个 `Client`**——而每个 `Client::new()` 都有独立的 ArcSwap 状态，用错一个就静默失效。旧源码里那段「IMPORTANT: monitor 必须用 KvRouter 的 Client」的长注释，就是在防这个坑。

**重构后的方案**：把「一个有类型的目标端点所需要的全部负载状态」收进一个自持有对象 `RoutingLoadContext`，它拥有：

| 成员 | 作用 |
|------|------|
| `client: Client` | 该上下文的**唯一** Client；所有选点与推送面都拿它的 clone（共享由构造保证，不再靠约定） |
| `source: RouterLoadSource` | 端点角色：`Decode` / `Aggregated` / `Prefill` / `Encode` |
| `scheduler_load: SchedulerLoadSender` | 非阻塞的调度负载发布通道（容量 256，满了就合并为「每个 worker 最新一份」） |
| `thresholds: LoadThresholdHandle` | 共享可变的过载阈值（`Arc<RwLock<LoadThresholdConfig>>`） |
| `cancellation_token` | 取消子树；`Drop` 时自动 cancel |
| `monitor: Option<KvWorkerMonitor>` | 仅当 `source.monitors_sequence_load()`（即非 Encode）时存在 |
| `_task_guard` | 引擎上下文守卫，防止任务被提前回收 |

配套的两个关键设计：

- **`RouterLoadSource::monitors_sequence_load()`**：Encode 端点不做序列负载监控（它只编码媒体，不持有 KV 序列），所以 Encode 上下文的 `monitor` 是 `None`、`scheduler_load` 是禁用态——发布调用变成 no-op。
- **`OverloadCheck::AlreadyAdmitted`**：KV 选点已经做过载准入的请求，直发时跳过共享 client 的过载复查。原因写在 `dispatch_kv_admitted` 的文档注释里：**准入可能同步发布本请求自身的负载**，若再查一次共享过载状态，路由器会被自己刚记下的负载自误拒。

#### 4.4.2 核心流程

`RoutingLoadContext` 的诞生与分发（以 watcher 的 Tokens 分支为例）：

```text
watcher 发现模型，needs_preprocessed_routing = true
  ├─ effective_worker_type(card.worker_type, card.model_type)   → WorkerType
  ├─ RouterLoadSource::from_worker_type(...)                    → Decode/Agg/Prefill/Encode
  ├─ LoadThresholdHandle::new(router_config.load_threshold_config)   ← 阈值来自 RouterConfig
  └─ RoutingLoadContext::start(client, source, thresholds, &cancellation, Some(allocator_trim))
       ├─ cancellation_token = parent_token.child_token()       → 取消子树
       ├─ source.monitors_sequence_load()?
       │    是 → scheduler_load_channel(...) + KvWorkerMonitor::new(...) + start_monitoring()
       │    否（Encode）→ SchedulerLoadSender::disabled(...)，monitor = None
       └─ 返回 Arc<RoutingLoadContext>

随后三路分发（全部从同一个 context 取）：
  ├─ KvRouter     ← client() / scheduler_load_sender() / cancellation_token()
  ├─ PrefillRouter← load_thresholds() 的 clone + cancellation.child_token()
  └─ build_preprocessed_routing_with_selector(..., load_context, ...)
       └─ preprocessed_backend_engine(...)
            ├─ KV 模式 → RoutingHost::new_with_load_context_and_coordinator(router, chooser, load_context, affinity)
            └─ 其他    → RoutingHost::new_builtin_with_capabilities(router, load_context, affinity, lora)
                 └─ routing_context 字段持有 load_context（RAII 保活到 host 死亡）

请求期间的闭环：
  scheduler 发布负载快照 → SchedulerLoadSender（满了 coalesce）→ KvWorkerMonitor 消费
    → 超阈值者 set_overloaded_instances 写到共享 Client
  KV 选点做过载准入 → dispatch_kv_admitted（OverloadCheck::AlreadyAdmitted，免复查直发）
  RoutingHost 被 drop → RoutingLoadContext 引用计数归零 → Drop 取消子树 → 监控任务收尾
```

注意 watcher 里还有**第二条**创建路径：`ModelInput::Text` 的 worker（在 backend 内分词、可同时声明 embedding/classify/pooling 多个面）也会 `RoutingLoadContext::start`，然后把 `load_context.client()` 的 clone 用作所有 push router 的共享 client（[watcher.rs#L829-L842](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/watcher.rs#L829-L842)），最后 `worker_set.set_load_context(load_context)` 存档（[watcher.rs#L952-L953](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/watcher.rs#L952-L953)）。

#### 4.4.3 源码精读

先看新文件里的两个核心类型。`RouterLoadSource` 是端点角色的枚举：

[lib/llm/src/kv_router/routing_load.rs#L23-L30](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/kv_router/routing_load.rs#L23-L30)

```rust
/// Endpoint role whose scheduler and remote metrics feed one load context.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RouterLoadSource {
    Decode,
    Aggregated,
    Prefill,
    Encode,
}
```

它由 worker 类型直接映射（[L41-L48](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/kv_router/routing_load.rs#L41-L48) 的 `from_worker_type`），并决定是否监控序列负载（[L63-L65](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/kv_router/routing_load.rs#L63-L65)：`!matches!(self, Self::Encode)`）。注意 `Decode` 与 `Aggregated` 共用同一个 metric 标签（[L33-L39](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/kv_router/routing_load.rs#L33-L39)）——两者都算解码侧负载。

`RoutingLoadContext` 本体，结构体文档注释就是这次重构的宣言：

[lib/llm/src/kv_router/routing_load.rs#L253-L266](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/kv_router/routing_load.rs#L253-L266)

```rust
/// Owns the load lifecycle for one typed routing endpoint.
///
/// Every selection and dispatch plane receives a clone of this context's
/// single endpoint [`Client`]. Decode, aggregated, and prefill contexts
/// are intentionally independent.
pub struct RoutingLoadContext {
    client: Client,
    source: RouterLoadSource,
    scheduler_load: SchedulerLoadSender,
    thresholds: LoadThresholdHandle,
    cancellation_token: CancellationToken,
    monitor: Option<KvWorkerMonitor>,
    _task_guard: Option<EngineContextGuard>,
}
```

「Every selection and dispatch plane receives a clone of this context's single endpoint Client」——共享从「祈祷大家传同一个」变成了「只能从这里拿」。`start()` 是唯一构造入口：

[lib/llm/src/kv_router/routing_load.rs#L269-L306](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/kv_router/routing_load.rs#L269-L306)

```rust
pub async fn start(
    client: Client,
    source: RouterLoadSource,
    thresholds: LoadThresholdHandle,
    parent_token: &CancellationToken,
    task_guard: Option<EngineContextGuard>,
) -> anyhow::Result<Arc<Self>> {
    let cancellation_token = parent_token.child_token();
    let (scheduler_load, monitor) = if source.monitors_sequence_load() {
        let (scheduler_load, scheduler_load_rx) =
            scheduler_load_channel(source, cancellation_token.child_token());
        let monitor = KvWorkerMonitor::new(
            client.clone(), source, scheduler_load_rx,
            thresholds.clone(), cancellation_token.child_token(), task_guard.clone(),
        );
        monitor.start_monitoring().await?;
        (scheduler_load, Some(monitor))
    } else {
        (SchedulerLoadSender::disabled(source, cancellation_token.child_token()), None)
    };
    Ok(Arc::new(Self { client, source, scheduler_load, thresholds,
                       cancellation_token, monitor, _task_guard: task_guard }))
}
```

三个要点：取消令牌是**三级子树**（父 token → context token → monitor/channel token），任何一层 drop 都只影响自己的子树；monitor 与 channel 在 `start` 里成对创建，外部无法只造一个；`Drop` 实现只有一行 `self.cancellation_token.cancel()`（[L333-L337](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/kv_router/routing_load.rs#L333-L337)），生命周期完全交给 RAII。

调度负载通道的「非阻塞 + 合并」语义在 `SchedulerLoadSender`：

[lib/llm/src/kv_router/routing_load.rs#L167-L195](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/kv_router/routing_load.rs#L167-L195) — `publish` / `publish_batch` 都走 `try_publish`，用 `tx.try_send` 永不阻塞请求路径；通道满时进 `coalesce`：把快照写进一张 `DashMap<WorkerWithDpRank, Snapshot>`（**每个 worker 只留最新一份**），再 `notify_one` 唤醒接收方排水。接收侧 `SchedulerLoadReceiver::recv` 在 `tokio::select!` 里同时等通道与唤醒信号（[L203-L221](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/kv_router/routing_load.rs#L203-L221)）。因为快照是**绝对值**而非增量，丢弃中间值是安全的——最后一次写入总会收敛。

现在看 watcher 侧的创建点（这是 4.3.2 流程图里 ★ 那一步）：

[lib/llm/src/discovery/watcher.rs#L567-L589](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/watcher.rs#L567-L589)

```rust
let needs_preprocessed_routing =
    needs_factory_chat_pipeline || tokenizer.is_some() || needs_generate_pipeline;

let load_thresholds =
    LoadThresholdHandle::new(router_config.load_threshold_config.clone());
let load_context = if needs_preprocessed_routing {
    let source = RouterLoadSource::from_worker_type(effective_worker_type(
        card.worker_type, card.model_type,
    ));
    Some(RoutingLoadContext::start(
        client.clone(), source, load_thresholds.clone(),
        &cancellation, Some(allocator_trim.clone()),
    ).await?)
} else {
    None
};
```

**负载上下文建在一切路由组件之前**——后面的 KvRouter、PrefillRouter、`build_preprocessed_routing` 全部从它取零件。KvRouter 的三处取用：

[lib/llm/src/discovery/watcher.rs#L601-L626](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/watcher.rs#L601-L626)

```rust
let mut chooser = self.manager.kv_chooser_for_with_selector_and_client(
    load_context.as_ref().expect("routing load context must exist")
        .client().clone(),          // ① 共享 client：由构造保证
    card.kv_cache_block_size,
    selector,
    ...
    load_context.as_ref().expect("routing load context must exist")
        .scheduler_load_sender(),   // ② 调度负载发布端
    load_context.as_ref().expect("routing load context must exist")
        .cancellation_token(),      // ③ 取消子树的 child
).await?;
```

PrefillRouter 拿的则是阈值句柄与一个 child token（[watcher.rs#L647-L662](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/watcher.rs#L647-L662)，注意 `load_thresholds.clone()` 与 `cancellation.child_token()` 两个新参数）。`WorkerSet` 侧同时记录两样东西：`worker_set.load_thresholds`（[L677-L678](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/watcher.rs#L677-L678)）供运行时改阈值，`set_load_context`（[worker_set.rs#L231-L237](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/worker_set.rs#L231-L237)）保活整个上下文。

接着是本讲规格里点名要标注的那一步——`build_preprocessed_routing` 的**新签名**（对照旧版的 `worker_monitor: Option<KvWorkerMonitor>`）：

[lib/llm/src/entrypoint/input/common.rs#L210-L234](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/entrypoint/input/common.rs#L210-L234)

```rust
pub async fn build_preprocessed_routing(
    client: &Client,
    model_manager: Arc<crate::discovery::ModelManager>,
    router_mode: RouterMode,
    load_context: Arc<RoutingLoadContext>,     // ← 原来这里是 Option<KvWorkerMonitor>
    chooser: Option<Arc<KvRouter>>,
    prefill_chooser: Option<Arc<PrefillRouter>>,
    encoder_chooser: Option<Arc<EncoderRouter>>,
    enable_multimodal_cache_indexer: bool,
    session_affinity_ttl_secs: Option<u64>,
) -> anyhow::Result<PreprocessedRouting> { ... }
```

它的内部实现（`build_preprocessed_routing_with_selector`）里，原来「把 monitor 转成 `Arc<dyn WorkerLoadMonitor>` 塞给 push router」的三行没了，取而代之的是显式的 `None`：

[lib/llm/src/entrypoint/input/common.rs#L283-L290](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/entrypoint/input/common.rs#L283-L290)

```rust
let router = LlmPushRouter::from_client_with_state(
    router_client,
    router_mode,
    None,                      // 过载状态不再由外部注入，由 load_context 侧维护
    embedding_cache_indexer,
    cache_key_extractor,
).await?;
```

最后一步接线在 `preprocessed_backend_engine`——`load_context` 作为新参数传入，并在两个分支里分别进入 `RoutingHost`：

[lib/llm/src/entrypoint/input/common.rs#L182-L205](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/entrypoint/input/common.rs#L182-L205)

```rust
let engine: ServiceEngine<_, _> = match router_mode {
    RouterMode::KV => {
        let Some(chooser) = chooser else {
            anyhow::bail!("RouterMode::KV requires KVRouter to not be null");
        };
        Arc::new(RoutingHost::new_with_load_context_and_coordinator(
            router, chooser, load_context, affinity,
        ))
    }
    _ => {
        let lora = ...;
        Arc::new(RoutingHost::<Sel>::new_builtin_with_capabilities(
            router, load_context, affinity, lora,
        )?)
    }
};
```

`RoutingHost` 用一个字段把它保活（[routing_host.rs#L188-L203](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/kv_router/routing_host.rs#L188-L203)，字段注释写明 "Retains the shared client, overload state, and cancellation subtree for this host"；旧构造器 `new_with_coordinator` 现在内部传 `None`，是给「早于负载所有权重构的兼容路径」留的，见 [L245-L251](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/kv_router/routing_host.rs#L245-L251) 的注释）。新构造器族在 [L227-L265](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/kv_router/routing_host.rs#L227-L265)。

请求侧的另一半——`dispatch_kv_admitted` 与 `OverloadCheck`：

[lib/runtime/src/pipeline/network/egress/push_router.rs#L1159-L1180](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L1159-L1180)

```rust
/// Dispatch exactly to a worker whose KV selection step already performed
/// overload admission.
///
/// Discovery and fault detection are still enforced. The shared client
/// overload state is not rechecked because admission may synchronously
/// publish this request's own load before dispatch begins.
pub async fn dispatch_kv_admitted(
    &self, request: SingleIn<T>, instance_id: u64,
) -> anyhow::Result<ManyOut<U>> {
    if !self.router_mode.is_kv_routing() {
        anyhow::bail!("admitted dispatch is only valid in KV routing mode");
    }
    self.generate_with_fault_detection_inner(
        instance_id, request, TransportFallback::Deny,
        OverloadCheck::AlreadyAdmitted,
    ).await
}
```

`OverloadCheck` 只有两个值（[L208-L212](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L208-L212)）：`Required`（普通直发，仍查共享过载状态）与 `AlreadyAdmitted`（KV 准入后直发，跳过复查）。原来的 `generate_with_fault_detection` 被重构为带 `overload_check` 参数的 `_inner` 版本，两条安全网（发现面解析、响应流故障检测）都保留。

#### 4.4.4 代码实践

**实践目标**：把 `RoutingLoadContext` 的数据流画出来，并回答规格里的那个问题——**`build_preprocessed_routing` 现在把负载上下文接到哪一步？**

**操作步骤**：

1. 打开 [lib/llm/src/discovery/watcher.rs#L567-L704](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/watcher.rs#L567-L704)，从 `needs_preprocessed_routing` 到 `build_preprocessed_routing_with_selector` 调用，抄下 `load_context` 出现的每一行（共 6 处左右）。
2. 打开 [lib/llm/src/entrypoint/input/common.rs#L161-L323](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/entrypoint/input/common.rs#L161-L323)，追 `load_context` 参数从函数入口到 `RoutingHost` 字段的完整路径。
3. 画一张图，节点至少包括：`RouterConfig.load_threshold_config`、`LoadThresholdHandle`、`RoutingLoadContext`、`KvRouter`、`PrefillRouter`、`LlmPushRouter`、`RoutingHost.routing_context`、`KvWorkerMonitor`、`SchedulerLoadSender/Receiver`、共享 `Client`。用三种线：构造（谁创建谁）、引用（谁持有谁）、数据流（负载快照怎么流）。
4. 在图上用 ★ 标出你读到的「负载上下文被接入装配」的确切代码行——参考答案：**[common.rs#L309-L317](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/entrypoint/input/common.rs#L309-L317) 的 `preprocessed_backend_engine(...)` 调用把 `load_context` 作为最后一个参数传入，并在 [common.rs#L187-L203](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/entrypoint/input/common.rs#L187-L203) 进入 `RoutingHost` 的 `routing_context` 字段**。也就是说：它接在「等完 min_initial_workers、建好 LlmPushRouter 之后，构造最终 backend engine」这一步，而不是更早的 push router 构造（那里现在显式传 `None`）。
5. （可选，需本地装好 ai-dynamo）跑一遍仓库自带的 Python 断言，确认阈值配置能从 Python 侧干净地构造与校验：

```bash
pytest lib/bindings/python/tests/test_load_threshold_config.py -v
```

**需要观察的现象**：步骤 1-4 是纯阅读，产物是一张图与一个带行号的 ★ 标注。步骤 5 若可运行，应看到三个测试全部通过（`defaults_are_disabled` / `preserves_valid_values` / `rejects_invalid_values`）。

**预期结果**：图中 `RoutingLoadContext` 应表现为「扇出中心」——它被 watcher 创建，向 KvRouter（client/sender/token）、PrefillRouter（thresholds/token）、RoutingHost（整体持有）三个方向供零件；`SchedulerLoadSender → KvWorkerMonitor → 共享 Client` 构成一条独立的数据回流。步骤 5 的运行结果**待本地验证**（该测试带 `gpu_0` marker，在 GPU 分池 CI 里执行；本地无 GPU 时通常也能直接跑，因为它只构造配置对象）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `RouterLoadSource::Encode` 的上下文 `monitor` 是 `None`，却仍然保留 `client`？

**答案**：Encode 端点是「多模态编码上游」，不持有 KV 序列，也就没有 `active_decode_blocks` / `active_prefill_tokens` 这类序列负载可言，所以不需要监控（`monitors_sequence_load()` 对 Encode 返回 false，[routing_load.rs#L63-L65](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/kv_router/routing_load.rs#L63-L65)）。但选点与推送仍然需要 Client（发现、故障检测、直发都走它）。仓库自带单测固定了这个行为：[routing_load.rs#L432-L469](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/kv_router/routing_load.rs#L432-L469) 断言 Encode 上下文 `monitor().is_none()`、`scheduler_load_sender().is_enabled() == false`、且发布快照后 `client.overloaded_instance_ids()` 仍为 `None`。

**练习 2**：旧设计里「monitor 与 KvRouter 必须共用一个 Client」的坑，新设计是怎么从结构上消灭的？

**答案**：把 Client 的所有权移进 `RoutingLoadContext`：它只暴露 `client()` 访问器，KvRouter、push router、monitor 拿到的全是同一个 context 的 clone（结构体文档注释明说 "Every selection and dispatch plane receives a clone of this context's single endpoint Client"）。调用方想造第二个 Client 也无从插手——共享从运行时约定变成了类型系统保证。同时 `LlmPushRouter::from_client_with_state` 的 monitor 参数现在固定传 `None`（[common.rs#L283-L290](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/entrypoint/input/common.rs#L283-L290)），外部注入过载状态的旧通道被关闭。

**练习 3**：`dispatch_kv_admitted` 跳过的是哪一层检查？哪些检查**没有**被跳过？

**答案**：跳过的只有「共享 client 过载状态复查」这一层（`OverloadCheck::AlreadyAdmitted` vs `Required`，[push_router.rs#L208-L212](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/runtime/src/pipeline/network/egress/push_router.rs#L208-L212)）。没有被跳过的：目标实例的发现面解析（实例必须仍在路由表中）、响应流的故障检测（`generate_with_fault_detection_inner` 的主体），以及传输回退被显式设为 `TransportFallback::Deny`（KV 选点已定，不允许换目标）。此外它入口处还守卫了 `router_mode.is_kv_routing()`——非 KV 模式调用直接 bail。

## 5. 综合实践：『参数 → 生成组件』对照表

本讲的综合实践把四个模块串起来：**用不同参数组合启动 sample 后端，预测并验证 frontend 内部各生成了哪些组件，并标注负载上下文被接到哪一步**。

### 5.1 准备

环境要求与 u1-l2 相同：装好 `ai-dynamo`（容器或 PyPI），本地无 etcd 时给 frontend 和 worker 都加 `--discovery-backend file`。sample 后端无需 GPU。

先明确一个容易踩的坑：**agg.sh / disagg.sh 只把 `--model-name` 之外的多余参数转发给 worker，不会传给 frontend**。看 [examples/backends/sample/launch/agg.sh#L33-L49](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/examples/backends/sample/launch/agg.sh#L33-L49)：`EXTRA_ARGS` 只出现在 `sample_main` 的命令行上（`*)` 通配分支收集，[L47-L49](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/examples/backends/sample/launch/agg.sh#L47-L49) 使用），frontend 是裸起的 `python3 -m dynamo.frontend &`（[L44](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/examples/backends/sample/launch/agg.sh#L44)）。所以要改 frontend 的 router-mode 或负载阈值，必须手动分进程启动。

### 5.2 操作步骤

依次跑三种组合（每种做完 `Ctrl-C` 清理干净再换下一种；无 etcd 时每条 python 命令都加 `--discovery-backend file`）：

**组合 A —— agg + 默认 round-robin**（直接用脚本）：

```bash
examples/backends/sample/launch/agg.sh
# 验证：curl http://localhost:8000/v1/chat/completions -H "Content-Type: application/json" \
#   -d '{"model":"sample-model","messages":[{"role":"user","content":"hi"}],"stream":true}'
```

**组合 B —— agg + kv 路由 + 过载阈值**（手动分进程，frontend 换模式并配阈值）：

```bash
python3 -m dynamo.frontend --router-mode kv \
  --active-decode-blocks-threshold 0.8 &          # 步骤 1：frontend 用 KV 模式 + 负载阈值
python3 -m dynamo.common.backend.sample_main --model-name sample-model &   # 步骤 2：聚合 worker
```

**组合 C —— disagg（prefill + decode）**（直接用脚本）：

```bash
examples/backends/sample/launch/disagg.sh
```

对照源码：disagg.sh 的两个 worker 分别带 `--component sample-prefill --disaggregation-mode prefill` 和 `--component sample-decode --disaggregation-mode decode`（[examples/backends/sample/launch/disagg.sh#L62-L74](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/examples/backends/sample/launch/disagg.sh#L62-L74)）。

### 5.3 需要观察的现象

每种组合下记录：

1. frontend 日志中 `Connected to ...` 与 `Chat completions is ready` 出现的时机（组合 A/B 在 worker 起来后；组合 C 要等 decode worker 也就位）。
2. 组合 B 的 frontend 是否多出 KV 路由相关日志/指标（`dynamo_router_*`），以及启动时是否打印 `busy-worker rejection enabled by --active-decode-blocks-threshold=0.8`（这条日志来自 [router_args.py#L110-L138](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/components/src/dynamo/common/configuration/groups/router_args.py#L110-L138) 的 `log_rejection_thresholds`——它证明阈值确实进了 `RouterConfig` 并将随 `LocalModel` 到达 watcher）。
3. 组合 C 中请求的响应 token 是否先出现 prefill 侧的一条、再由 decode 侧续出（sample 引擎的合成 `disaggregated_params` 交接，见 disagg.sh 头部注释）。

### 5.4 预期结果：对照表

以下「生成的组件」列是**从源码推导的预测**（依据 [watcher.rs#L563-L704](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/watcher.rs#L563-L704) 与 [common.rs#L161-L323](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/entrypoint/input/common.rs#L161-L323)），具体日志表现**待本地验证**：

| 组合 | 关键参数 | frontend 侧生成的组件 | worker 侧 |
|------|----------|------------------------|-----------|
| A | frontend 默认（round-robin）；worker 无特殊参数 | `RoutingLoadContext`（source = Aggregated，含 `KvWorkerMonitor`）、`LlmPushRouter`（RoundRobin 模式）、`RoutingHost::new_builtin_with_capabilities`、`PrefillRouter`（disabled 分支）、无 `KvRouter` | 1 个 Aggregated worker（component `sample`） |
| B | frontend `--router-mode kv --active-decode-blocks-threshold 0.8`；worker 同 A | 同 A 的负载上下文（阈值句柄里现在有 0.8）、`KvRouter`（kv_chooser，需要 `set_teardown_task_guard`，并从 context 拿 client/sender/token）、`RoutingHost::new_with_load_context_and_coordinator`、`PrefillRouter` 仍为 disabled（worker 是聚合型，非 Decode） | 同 A |
| C | worker 分别 `--disaggregation-mode prefill` / `decode` | **两个独立的负载上下文**：decode 集一个（source = Decode）、prefill 集一个（source = Prefill）。decode 侧：`PrefillRouter`（启用，构造条件 `WorkerType::Decode`）+ 若 frontend 是 kv 模式再加 `KvRouter`；`EncoderRouter`；各自一套 `build_preprocessed_routing` → `RoutingHost` | `sample-prefill` + `sample-decode` 两个 component |

填表时的自查问题（答案在源码里）：

- **负载上下文接到哪一步？** 在 `build_preprocessed_routing_with_selector` 内部、`LlmPushRouter::from_client_with_state` 建好之后、`preprocessed_backend_engine` 构造最终 backend engine 时（[common.rs#L309-L317](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/entrypoint/input/common.rs#L309-L317) → [L187-L203](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/entrypoint/input/common.rs#L187-L203)），以 `RoutingHost.routing_context` 字段的形式被持有。注意它**不进** push router 的 monitor 槽（那里固定 `None`）。
- **组合 C 为什么会有两个而不是一个负载上下文？** 因为 watcher 按「有类型的目标端点」分组处理：decode 集与 prefill 集是两张不同的 `ModelDeploymentCard`，各自走一遍 `RoutingLoadContext::start`；`Decode / Aggregated / Prefill` 三类上下文「intentionally independent」（[routing_load.rs#L253-L257](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/kv_router/routing_load.rs#L253-L257) 的文档注释）。这也正是 PR 标题 "own load state per routing context" 的含义。
- 组合 A 的 frontend 能不能带 `--router-mode direct`？能，但 `Direct` 模式要求请求显式指定目标 worker，行为属于 u3-l4 讲过的「外部决策」组。

### 5.5 交付物

一张填完的对照表（含「负载上下文接入点」那一行的行号标注）+ 三段日志摘录（每组合一段，标出证明组件生成的那几行）。

## 6. 本讲小结

- 装配 Dynamo runner 的两个正交决策：`EngineConfig`（引擎在本进程还是远端）与 `RouterConfig`（请求怎么分发）；`enforce_disagg` 已废弃，拓扑由 worker 注册的类型决定。
- `EngineConfig` 三变体：`Dynamic`（远端 + 可选 Python `chat_engine_factory` 回调 + AIC 负载估计器）、`InProcessText`（引擎自己分词，echo 用）、`InProcessTokens`（框架分词，mocker 用，带 `is_prefill/is_decode`）。
- Python 到 Rust 的装配路径：`EntrypointArgs(EngineType.Dynamic, ...)` → `make_engine` → `LocalModelBuilder` 链 → `LocalModel::fetch`（mocker 忽略权重）→ `select_engine` → `EngineConfig`；`chat_engine_factory` 靠注册时捕获的 `TaskLocals` 完成异步回调桥接。
- `Dynamic` 是惰性装配：`make_engine` 阶段不建任何网络组件；组件在 watcher 发现 worker 后生成——**先建 `RoutingLoadContext`**，再按需建 `KvRouter`（Decode 集另建 `PrefillRouter`），最后 `build_preprocessed_routing` 组出 `RoutingHost`。
- `EngineDispatcher` / `StreamingEngineAdapter` 是一对方向相反的适配器，解决「一个引擎同时服务 completions 与 chat 两种端点」；`Input` 枚举（Http/Grpc/Text/Stdin/Endpoint）决定引擎就位后接什么，其中 `Endpoint` 方向相反（本进程当 worker 被拉取）。
- 有 Python factory 时 chat 管线走 `build_preprocessed_pipeline`（无 preprocessor/token_backend，预处理在 Python）；否则走全 Rust 的 `build_pipeline`；completions 端点永远走 Rust preprocessor。
- **本轮 #13861**：`build_preprocessed_routing` 的 `worker_monitor` 参数被 `Arc<RoutingLoadContext>` 取代。负载上下文把共享 Client、`RouterLoadSource`、调度负载通道（满则按 worker 合并最新值）、`LoadThresholdHandle`、取消子树和可选 `KvWorkerMonitor` 收进一个 RAII 对象；`RoutingHost` 用 `routing_context` 字段保活它；KV 准入后的直发走 `dispatch_kv_admitted`（`OverloadCheck::AlreadyAdmitted`）以免被自己刚发布的负载自误拒。

## 7. 下一步学习建议

- **u4-l2（HttpService）**：本讲只到「HttpFrontend 把引擎接给 HTTP 服务」为止；下一讲深入 `service_v2.rs` 的 `HttpService` 内部——路由表、`InflightPermit` 并发控制、OpenAI 端点处理器（含本次新增的 `POST /v1/responses/input_tokens`）。
- **u4-l3（preprocessor 与 LocalModel）**：本讲反复出现的 `LocalModel`、`ModelDeploymentCard`、`OpenAIPreprocessor` 与 token 块化，在那里展开。
- **u4-l4（worker 类型与 discovery）**：本讲站在 frontend 视角看「watcher 发现了 worker」；下一面镜子从 worker 侧看 `WorkerType` / `WorkerSet` / `ModelManager` 如何维护动态集合，以及 `LoadThresholdHandle` 如何挂进发现层。
- **u6-l2（Rust 路由核心）**：本讲 4.4 只讲了 `RoutingLoadContext` 的装配侧；它的消费侧（打分如何用过载状态、`dispatch_kv_admitted` 在 `RoutingHost` 里何时被调用、`RouterLoadSource` 四个值的完整数据流）在那一讲展开。
- 若你想先看 Python 侧的另一半：**u5-l1（frontend main 全流程）** 与 **u5-l2（prepost.py）** 讲的就是 `chat_engine_factory` 在 Python 端的实现（`EngineFactory.chat_engine_factory`）。
