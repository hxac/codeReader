# u8-l1 vLLM 后端接入

## 1. 本讲目标

学完本讲，你应该能够：

1. 完整追踪一条请求在 vLLM 后端中的落点：`python -m dynamo.vllm` → `worker()` → `WorkerFactory.create()` → 某个 Handler → `engine_client.generate()`（vLLM 的 `AsyncLLM`）。
2. 解释 `setup_kv_event_publisher` 如何把 vLLM 引擎内部的 KV 块增删事实搬运到 Dynamo 事件面，从而「喂养」KV 感知路由器。
3. 说出 `kv_connector_protocols.py` 定义的三种 P/D 交接协议（NIXL 拉取式、Mooncake 推送式、MultiConnector 包装委托）及其判别规则。
4. 指出 `WorkerFactory` 如何按「任务类型（生成 / 嵌入 / 分类 / 实时 / 编码） × 分离角色（prefill / decode / aggregated）」两个正交维度选择 worker 装配路径。

## 2. 前置知识

本讲建立在 u4-l4（discovery 与 WorkerType）和 u5-l1（frontend 的 make_engine 注入链）之上，先用三句话把要用的旧概念复活：

- **AsyncLLM**：vLLM v1 引擎的异步入口。它是一个「引擎客户端」，真正的调度与推理跑在独立的 EngineCore 进程里。Dynamo 从不 fork 或修改 vLLM 的内部，只在进程外「包一层」。
- **ModelDeploymentCard（模型卡片）**：worker 在发现面上发布的自描述记录（u4-l4）。本讲的 `register_vllm_model` 就是往卡片里填 `worker_type`、`needs`（DNF 依赖）、`total_kv_blocks`、`max_num_seqs` 等字段，前端的就绪门按这些字段决定模型何时可服务。
- **KV 事件**（u6-l3）：引擎把「哪些块哈希进了缓存 / 被逐出了」编码成 ZMQ 消息广播。路由器订阅后回填基数树索引（u6-l4），才能算出 KV 重合度。vLLM 后端是这些事件的**生产端**。

两个本讲新引入的术语：

- **Handler**：挂在一个 Dynamo endpoint 上的异步生成器函数（u2-l1 的 `serve_endpoint` 模式），负责把 Dynamo 的请求 dict 翻译成 vLLM 的调用、再把 vLLM 的流式输出翻译回 Dynamo 的 chunk dict。
- **`setup_*` 系列函数**：`main.py` 里一组以 `setup_` 开头的装配函数（引擎、KV 事件、指标、FPM 中继……）。它们被打包注入 `WorkerFactory`，工厂再按 worker 形态决定「哪些 setup 被调用、以什么顺序」。

还有一个重要的认知校正：**Dynamo 对 vLLM 是「集成」而非「侵入」**。所有 `dynamo.vllm` 代码都跑在 vLLM 进程（或其父进程）里，通过 vLLM 的公开 API（`AsyncLLM.from_vllm_config`、`stat_loggers` 工厂、`kv_events_config`、`kv_transfer_config`）挂钩。理解了这一点，本讲的所有源码都可以读成同一个问题的不同侧面：*「vLLM 的哪个公开钩子能让我们拿到想要的信息？」*

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用它讲什么 |
|---|---|---|
| [components/src/dynamo/vllm/\_\_main\_\_.py](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/__main__.py) | `python -m dynamo.vllm` 的三行入口 | 入口与哈希种子固定 |
| [components/src/dynamo/vllm/main.py](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/main.py) | `worker()` 主编排 + 全部 `setup_*` 装配函数 + 模型注册 | 4.1、4.3 |
| [components/src/dynamo/vllm/worker_factory.py](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/worker_factory.py) | `WorkerFactory`：按配置分流到 6 条 worker 装配路径 | 4.2 |
| [components/src/dynamo/vllm/publisher.py](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/publisher.py) | `StatLoggerFactory` / `DynamoStatLoggerPublisher`：vLLM 统计回调 → Dynamo 指标 | 4.3 |
| [components/src/dynamo/vllm/kv_connector_protocols.py](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/kv_connector_protocols.py) | 每种 vLLM KV 连接器的 P/D 交接参数协议 | 4.4 |
| [components/src/dynamo/vllm/engine_generate.py](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/engine_generate.py) | 向模型卡片广告「vLLM 原生 generate 能力」 | 4.5 |
| [components/src/dynamo/vllm/handlers.py](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/handlers.py) | 各 Handler 与真正的生成桥接 `generate_tokens` | 4.5 |
| [components/src/dynamo/vllm/cache_info.py](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/cache_info.py) | 从引擎取 KV 块大小（事件块大小的来源） | 4.3 |

## 4. 核心概念与源码讲解

### 4.1 worker() 启动链：从 CLI 到 WorkerFactory

#### 4.1.1 概念说明

`dynamo.vllm` 是一个「worker 进程」：它启动 vLLM 引擎、把引擎包成 Handler、注册到 Dynamo 发现面，然后永久阻塞在 `serve_endpoint` 上服务请求。`main.py` 的 `worker()` 是这个进程的总编排函数，它自己**不做任何具体装配**——而是把六七个 `setup_*` 函数作为参数交给 `WorkerFactory`，由工厂按配置决定装配顺序与取舍。

这种「函数注入」的设计初看绕，实则解决一个真问题：同一份 `setup_vllm_engine` / `setup_kv_event_publisher` 逻辑，decode、prefill 两种 worker 都要用，但嵌入（embedding）worker 必须跳过 KV 发布、实时 worker 要换成双向端点。把 setup 抽成独立函数、把「何时调用」留给工厂，两条路径就能共享实现而各自裁剪。

#### 4.1.2 核心流程

`worker()` 的执行序列（按时序）：

```text
python -m dynamo.vllm
  └─ __main__.py: 固定 PYTHONHASHSEED=0（除非已设置）
  └─ main.py: worker(argv)
       1. parse_args(argv)                     → Config（运行时组 + vLLM 组的多继承聚合）
       2. 嵌入子进程判定 / dump_config          → 多进程嵌入池的父/子分叉（u8-l8）
       3. served_model_name 兜底               → 未指定时用 --model 原值
       4. fetch_model(config.model)            → 预下载权重，避免 vLLM 内部再拉 HF
       5. prepare_snapshot_engine(...)          → 快照模式：先建引擎再建 runtime（可选）
       6. config.headless? → run_dynamo_headless → 无 NATS/etcd 的裸 vLLM 模式，直接 return
       7. create_runtime(discovery_backend, request_plane, event_plane)
       8. install_signal_handlers(...)          → 优雅关停（u1-l4 的三阶段语义）
       9. WorkerFactory(setup_* 六件套, state_agent_lifecycle)
      10. await factory.create(runtime, config, shutdown_event, shutdown_endpoints, snapshot_engine)
```

第 4 步值得展开：Dynamo 用自己的通用取模型路径先把权重落到磁盘，**但故意不把返回的本地路径写回 `config.engine_args.model`**——因为 vLLM 会把这个名字发给它的 Ray 流水线并行 worker，那些进程未必看得到本机路径。vLLM 随后会自己再「下载」一次，然后从 HF 缓存里直接命中，两边各取所需。

#### 4.1.3 源码精读

入口三行，注意 `PYTHONHASHSEED` 的固定——KV 块哈希依赖跨进程一致的字符串哈希（u4-l3 的链式哈希），不固定种子会让同一段文本在不同进程算出不同块哈希，前缀复用直接失效：

[components/src/dynamo/vllm/\_\_main\_\_.py:L6-L16](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/__main__.py#L6-L16)
> 在导入任何 dynamo.vllm 模块之前固定 `PYTHONHASHSEED=0`；随后检查是否处于快照恢复的 standby 模式（是则不 import vLLM、原地等待），最后调用 `main()`。

`worker()` 前半段——参数、模型名、权重预取、快照与 headless 分叉：

[components/src/dynamo/vllm/main.py:L155-L194](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/main.py#L155-L194)
> 依序完成：`parse_args` 得到 `Config`；嵌入子进程与快照模式互斥校验（多进程嵌入池不能配合 CRIU 检查点）；`served_model_name` 未显式给出时兜底为 `config.model`；`should_prefetch_model` 为真时 `await fetch_model(config.model)` 预取权重（`--load-format modelexpress/mx` 时让 ModelExpress 插件自己接管获取，见 L97-L104 的两个判定函数）。

[components/src/dynamo/vllm/main.py:L196-L215](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/main.py#L196-L215)
> 快照模式：`prepare_snapshot_engine` 在创建 runtime **之前**构造引擎，保证 CRIU 捕获 GPU 状态时没有任何运行时连接；`config.headless` 为真则 `run_dynamo_headless` 直接返回——这是「裸 vLLM、无 NATS/etcd、无 dynamo endpoint」的旁路形态。

`worker()` 后半段——运行时与工厂：

[components/src/dynamo/vllm/main.py:L217-L253](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/main.py#L217-L253)
> `create_runtime` 三个参数（`discovery_backend` / `request_plane` / `event_plane`）正是 u3-l1 讲过的三个正交开关；随后安装信号处理，把六个 setup 函数与 `StateAgentLifecycle` 注入 `WorkerFactory`，`await factory.create(...)` 之后 worker 的命运就交给工厂了。注意源码里的 `[gluo FIXME]` 注释——作者自己也在怀疑信号安装的时点是否应该在 `init()` 之后。

`setup_vllm_engine` 中真正构造 `AsyncLLM` 的位置：

[components/src/dynamo/vllm/main.py:L641-L654](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/main.py#L641-L654)
> `configure_multimodal_embedding_cache` 必须发生在 `create_engine_config()` **之前**，vLLM 才能看到 `ec_transfer_config`；随后 `engine_args.create_engine_config(usage_context=OPENAI_API_SERVER)` 产出 `VllmConfig`，并立刻调用 `disable_hybrid_kv_cache_manager_for_incompatible_pd_connector`（4.4 节会回到这个函数）。

[components/src/dynamo/vllm/main.py:L697-L728](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/main.py#L697-L728)
> 二选一：嵌入多进程模式走 `create_shared_embedding_engine_client`（u8-l8），其余走 `AsyncLLM.from_vllm_config(vllm_config, usage_context, stat_loggers=factory, ...)`。注意 `stat_loggers=factory`——Dynamo 就是从这个参数把自己塞进 vLLM 的统计回调链的（4.3 节）。计时结束后 `set_model_load_time` 上报加载耗时，打出 `VllmWorker for X has been initialized`。

[components/src/dynamo/vllm/main.py:L563-L609](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/main.py#L563-L609)
> 两处细节：其一，自己设 `PROMETHEUS_MULTIPROC_DIR` 以绕开 vLLM v0.11.0 传 `TemporaryDirectory` 对象而非 `.name` 字符串的退出误报；其二，嵌入 worker（pooling 引擎）没有 KV cache，**整体跳过** chat 形状的 `LLMBackendMetrics` 构造，避免 `/metrics` 上永远挂着零值——`stat_logger.embedding_worker` 这个布尔从工厂一路传到这里。

#### 4.1.4 代码实践

**实践目标**：不看任何图，仅凭源码亲手画出 vLLM decode worker 的初始化时序图（本讲的主实践，无需 GPU、无需安装 vllm extra）。

**操作步骤**：

1. 打开 [components/src/dynamo/vllm/main.py:L155](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/main.py#L155) 的 `worker()`，从 L155 单步向下，每遇到一个函数调用就问：「这个调用之后，进程里多了什么？」
2. 走到 L245 的 `factory.create(...)` 时切到 [worker_factory.py:L640](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/worker_factory.py#L640)，假设 `config.disaggregation_mode` 既不是 ENCODE 也不是 PREFILL，确认自己会落到 `_run_decode_worker`（L1110 入口、L1134 实体）。
3. 在 `_run_decode_worker` 里按行号顺序抄下这 10 个里程碑（顺序打乱，请自己排好）：
   - `register_vllm_model(...)` —— L1352
   - `DecodeWorkerHandler(...)` 构造 —— L1239
   - `configure_kv_event_block_size(...)` —— L1221
   - `generate_endpoint.serve_endpoint(handler.generate, ...)` —— L1395
   - `self.setup_vllm_engine(config, factory, ...)` —— L1218
   - `self.setup_metrics_collection(...)` —— L1287
   - `runtime.endpoint(...)` 拿 generate/clear/rl 三个端点 —— L1145
   - `self._setup_kv_routing(...)` —— L1270
   - `handler._first_token_source = await generate_endpoint.first_token_source(...)` —— L1348
   - `self.register_engine_routes(...)` —— L1299
4. 用文本或纸笔画出时序图，参与者为四条竖线：`worker()`、`WorkerFactory`、`AsyncLLM(EngineCore)`、`发现面(etcd/file)`。
5. 对照下面的「参考骨架」自查（先自己做再对照）：

```text
worker()          WorkerFactory            AsyncLLM                发现面
  | parse_args       |                       |                      |
  | fetch_model      |                       |                      |
  | create_runtime   |                       |                      |
  | create() ───────►|                       |                      |
  |                  | endpoint(...) ×N ───────────────────────────►| (取端点对象)
  |                  | setup_vllm_engine ──►| from_vllm_config      |
  |                  |                       | ├─ create_engine_config
  |                  |                       | └─ EngineCore 子进程  |
  |                  | configure_kv_event_block_size ──►|           |
  |                  | DecodeWorkerHandler   |                      |
  |                  | _setup_kv_routing     |                      |
  |                  | setup_metrics_collection                    |
  |                  | register_engine_routes                     |
  |                  | register_vllm_model ──────────────────────►| (发布模型卡片)
  |                  | serve_endpoint ×N ─────────────────────────►| (开始服务)
```

**需要观察的现象**：排序过程会逼你回答两个容易混的问题——(a) `register_vllm_model`（发布卡片、模型可被发现）发生在 `serve_endpoint`（真正能接请求）**之前**；(b) KV 事件发布器的建立在 Handler 构造**之后**、卡片发布**之前**，因为卡片里的 `kv_event_publishing_enabled` 字段要与实际状态一致。

**预期结果**：一张包含 10 个里程碑、四条参与者竖线的时序图，且能回答「卡片发布早于端点服务」这一时序为什么不会造成请求丢失（提示：u4-l4 的就绪门按 `needs` 的 DNF 依赖判定，卡片先到只是让前端开始观察，端点未就绪时选点不会成功）。

### 4.2 WorkerFactory：按任务类型与分离角色分流

#### 4.2.1 概念说明

`WorkerFactory.create()` 是一个**两级分派器**。第一级按「任务类型」分（realtime / embedding / classify / encode / 生成），第二级（仅生成路径）按 `DisaggregationMode` 分（PREFILL / DECODE / AGGREGATED）。两级正交：嵌入 worker 永远是 Aggregated 角色（它没有 prefill/decode 之分），encode worker 是独立的多模态角色。

为什么 embedding 的判定要排在最前？源码注释给了答案：嵌入是「跨越 worker 形状」的差异（pooling 版 AsyncLLM、`ModelType.Embedding`），而不是 decode 的一个变体。若放在 disagg 分派之后，一个 `--disaggregation-mode decode --embedding-worker` 的矛盾配置就会先落进 decode 分支。

#### 4.2.2 核心流程

```text
WorkerFactory.create(config)
  ├─ config.realtime           → _create_realtime_worker      (双向端点, ModelType.Realtime)
  ├─ config.embedding_worker   → _create_embedding_worker     (pooling, ModelType.Embedding)
  ├─ config.classify_worker    → _create_classify_worker      (pooling, Classify|Pooling)
  ├─ disagg == ENCODE          → _create_multimodal_encode_worker
  ├─ disagg == PREFILL         → _create_prefill_worker
  └─ disagg ∈ {DECODE, AGGREGATED} → _create_decode_worker
                                    ├─ DECODE     → worker_type=Decode, needs=[[Prefill]]
                                    └─ AGGREGATED → worker_type=Aggregated, needs=[]
```

每条路径都遵循同一个五段式骨架：**取端点 → 建引擎 → 造 Handler → 注册卡片 → serve**。差异只在于每段的取舍。

#### 4.2.3 源码精读

分派主体：

[components/src/dynamo/vllm/worker_factory.py:L640-L700](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/worker_factory.py#L640-L700)
> 六路分派。注意 L660-L668 的注释：embedding 优先判定因为它「跨越 worker 形状而非 decode 的变体」；L676-L678 还注明 `--benchmark-mode` 只支持 prefill/decode，encode 路径不接基准等待与 `get_perf_metrics` 端点。

decode 路径的 `worker_type` / `needs` 推导——这是与 u4-l4 就绪门的直接对接点：

[components/src/dynamo/vllm/worker_factory.py:L1332-L1350](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/worker_factory.py#L1332-L1350)
> DECODE 模式声明 `needs=[[Prefill]]`（DNF 的单个 AND 集）；AGGREGATED 声明 `needs=[]`（无同伴依赖）；`--route-to-encoder` 会把 `Encode` 追加进 AND 集。这正是 u7-l1 讲的「双向依赖、成对可用」在 worker 侧的产出端。随后 `first_token_source(worker_type)` 绑定首 token 来源统计，供 Rust 侧观测 TTFT 归属。

prefill 路径有一个容易忽略的契约细节：

[components/src/dynamo/vllm/worker_factory.py:L1619-L1643](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/worker_factory.py#L1619-L1643)
> prefill worker **无条件**注册 `ModelInput.Tokens`——注释解释这是 worker 间契约而非引擎本地的分词偏好：prefill 只会从 decode 同伴处收到 token id，`use_vllm_tokenizer` 只影响 frontend↔decode 边界。同时它注册 `ModelType.Prefill` 这个「标记位而非 OpenAI surface」，让旧版前端在跨版本 rollout 期间仍能识别它。

encode worker 是本讲与多模态链路的接口：

[components/src/dynamo/vllm/worker_factory.py:L799-L849](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/worker_factory.py#L799-L849)
> `_create_multimodal_encode_worker` 构造 `EncodeWorkerHandler(config.engine_args, config.embedding_transfer_mode, enable_frontend_decoding=config.frontend_decoding)`——`#12004` 加入的这第三个参数让编码 worker 能消费前端解码好的像素（端到端见 u8-l9）。注册的卡片是 `ModelType.Empty`（无 OpenAI surface）加 DNF 依赖 `[[Prefill, Decode], [Aggregated]]`：要么 P+D 成对，要么单个聚合同伴。

embedding 路径的长注释是本模块最有教学价值的一段——它解释了「为什么跳过」而不是「怎么做的」：

[components/src/dynamo/vllm/worker_factory.py:L851-L916](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/worker_factory.py#L851-L916)
> docstring 逐条列出嵌入 worker 刻意跳过的四件事：KV 事件发布器（无 KV cache）、FPM 中继（无 decode 相位）、StatLoggerFactory 接线（pooling 引擎不发逐批统计）、InstrumentedScheduler（硬编码 `pooling_params=None` 会静默废掉 pooling pass）。还解释了为什么 deliberately **不**给嵌入加 `--benchmark-mode embed`：嵌入负载近似 `(batch × ISL → latency)` 的双轴函数，进程内自测绘的收益远低于外部 HTTP 压测。

工厂自身的函数注入面：

[components/src/dynamo/vllm/worker_factory.py:L584-L603](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/worker_factory.py#L584-L603)
> `WorkerFactory.__init__` 收的就是 4.1 节看到的六个 setup 函数引用。`main.py` 提供“做什么”，工厂决定“何时/是否做”。

engine 路由注册（`/engine/*` JSON 回调，呼应 u3-l3 的 `engine_routes.rs`）：

[components/src/dynamo/vllm/worker_factory.py:L1733-L1793](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/worker_factory.py#L1733-L1793)
> `register_engine_routes` 把 `control/sleep`、`control/wake_up`、`control/start_profile` 等 5 个控制路由加上一整套 RL 管理路由（`liveness_probe`、`pause_generation`、`update_weights_from_disk` 等 12 个，LoRA 启用时再加 2 个）挂到 runtime 上——这正是 u8-l7 RL 服务面的 Python 落点。

#### 4.2.4 代码实践

**实践目标**：验证你真的理解了分派规则，方法是**预测 + 反证**。

**操作步骤**：

1. 先写下你对这四个配置的预测（每格填路径名）：

| 配置 | 走哪条 `_create_*` | `worker_type` | `needs` |
|---|---|---|---|
| `--disaggregation-mode prefill --route-to-encoder` | ? | ? | ? |
| `--embedding-worker --runner pooling` | ? | ? | ? |
| （默认，无任何 disagg 参数） | ? | ? | ? |
| `--disaggregation-mode encode` | ? | ? | ? |

2. 回到 [worker_factory.py:L640-L700](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/worker_factory.py#L640-L700) 与 L1332-L1350、L1619-L1643、L824-L835 四处源码核对。
3. 找出一个「两级维度相互矛盾」的配置，并说明源码在哪一行防止了它（提示：embedding 分支为什么必须排在 disagg 分支之前？谁在更早的地方已经拒绝了矛盾组合？）。

**需要观察的现象**：核对时你会遇到两处「答案不在分派器里」——embedding 与 disagg 的互斥是在 `DynamoVllmConfig._validate_embedding_worker_exclusivity`（L660-L663 注释提到的）里更早拒绝的，分派器只是第二道防线。

**预期结果**：四行预测全对，并能指出「prefill + route-to-encoder」的 `needs` 是 `[[Decode, Encode]]`（两个同伴都在同一 AND 集里，必须同时在场）。

### 4.3 setup_kv_event_publisher 与指标发布：喂养路由器

#### 4.3.1 概念说明

KV 感知路由的情报来源在 worker 侧。`setup_kv_event_publisher` 建立的是一条**三段接力**：

```text
vLLM EngineCore ──(ZMQ PUB, 块哈希增删)──► Dynamo KvEventPublisher(Rust)
                                              ├─ 本地索引器(可选)
                                              └─(事件面 kv-events 主题)──► 路由器订阅(u6-l3)
```

关键认知：**vLLM 是发布方，Dynamo 是订阅方**。`kv_events_config.endpoint` 是 vLLM 的 `ZmqEventPublisher` 绑定的地址；Dynamo 侧的 `KvEventPublisher`（一个 PyO3 暴露的 Rust 对象，内部就是 u6-l3 读过的 zmq_listener + event_processor）连过去消费，归一化后再转发到 Dynamo 事件面。

指标是同构的第二条线：vLLM 的引擎统计（KV 使用率、调度器水位）通过 `stat_loggers` 工厂回调流入 `DynamoStatLoggerPublisher`，一边发到事件面给路由器当负载信号，一边写 Prometheus gauge 给运维看。

#### 4.3.2 核心流程

`setup_kv_event_publisher` 的闸门与产出：

```text
enable_prefix_caching == False          → return None（无前缀缓存，无事件可发）
kv_events_config is None                → return None
enable_kv_cache_events == False         → return None（显式关闭，打日志）
──────────────── 通过三道闸 ────────────────
dp_start, dp_size = get_dp_range_for_worker(vllm_config)
for dp_rank in [dp_start, dp_start+dp_size):
    endpoint = consolidator 启用 ? tcp://127.0.0.1:{consolidator_port}
                                : offset_endpoint_port(kv_events_config.endpoint, dp_rank)
    KvEventPublisher(endpoint=generate_endpoint, kv_block_size=…,
                     zmq_endpoint=endpoint, dp_rank=…, image_token_id=…, …)
```

每个 data-parallel rank 一个发布器、一个独立端口——不同 rank 的 KV cache 互不相干，必须分开记账。

#### 4.3.3 源码精读

三道闸门：

[components/src/dynamo/vllm/main.py:L443-L454](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/main.py#L443-L454)
> 前缀缓存关闭、`kv_events_config` 缺失、`enable_kv_cache_events=False` 三种情况各自返回 `None`。也就是说「开 KV 路由」需要同时满足：vLLM 开了 prefix caching、给了 kv events 配置、且没有显式关掉事件。

端口选择与发布器构造：

[components/src/dynamo/vllm/main.py:L456-L501](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/main.py#L456-L501)
> `get_dp_range_for_worker` 算出本进程管辖的 DP rank 区间；`get_configured_kv_event_block_size` 取块大小（默认等于 vLLM 的 `cache_config.block_size`，可被 `additional_config` 覆盖，见 [cache_info.py:L22-L28](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/cache_info.py#L22-L28)）。consolidator 启用时所有 rank 订阅同一个本地合并器端口（L469-L474 的 TODO 注明 KVBM 支持 DP 后要分端口）；否则每个 rank 用 `ZmqEventPublisher.offset_endpoint_port` 错开端口。最终 `KvEventPublisher(...)` 从 `dynamo.llm` 导入——它是 Rust 对象，这就是 u6-l3 那条管道的 Python 侧入口。

`image_token_id` 是多模态路由的一致性关键：

[components/src/dynamo/vllm/main.py:L376-L419](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/main.py#L376-L419)
> `_resolve_image_token_id` 调用与前端**同一个** Rust 函数（`dynamo._core.resolve_routing_image_token_id`）解析图像占位 token id，保证 worker 侧的事件归一化器按与前端完全相同的 pad_value 方案改写 `BlockStored` 事件——若两边各算各的，同一张图片会在路由器索引里裂成不同的块哈希前缀。模型目录的解析还有一段兜底：HF id 走 `try_to_load_from_cache`（带 vLLM 的 revision 以选中同一快照），本地路径直接用原值。

Rust 侧的对接点（只需确认存在，深入留在 u6-l3）：

[lib/bindings/python/rust/llm/kv.rs:L1113-L1152](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/lib/bindings/python/rust/llm/kv.rs#L1113-L1152)
> `KvEventPublisher` PyO3 包装类持有 `llm_rs::kv_router::publisher::KvEventPublisher` 的 `Arc`——Python 构造的「发布器」实为 Rust 侧 zmq_listener 管道的句柄。

指标侧的回调链：

[components/src/dynamo/vllm/publisher.py:L22-L85](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/publisher.py#L22-L85)
> `DynamoStatLoggerPublisher` 实现了 vLLM 的 `StatLoggerBase` 接口。`record()` 在每次引擎迭代被回调：`kv_used_blocks = num_gpu_block × kv_cache_usage` 经 `self.inner.publish(dp_rank, ...)`（`WorkerMetricsPublisher`，又一个 Rust 对象）发往事件面——这是路由器「负载感知」的原始数据；同时 `component_gauges.set_total_blocks / set_gpu_cache_usage` 写本地 Prometheus。注意 L68-L76 的注释特意解释 `kv_cache_usage` 的极小数值（如 8.34e-05）是**正确值**而非 bug。

[components/src/dynamo/vllm/publisher.py:L132-L176](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/publisher.py#L132-L176)
> `StatLoggerFactory` 是注入 vLLM 的工厂对象（4.1 节 `AsyncLLM.from_vllm_config(stat_loggers=factory)`）。`embedding_worker=True` 时短路返回 `NoopStatLogger`；否则断言 `component_gauges` 已被 `setup_vllm_engine` 设置——注释点明时序依赖：gauge 必须在 vLLM 于引擎初始化期间回调 `create_stat_logger` 之前就位。

`register_vllm_model` 把这些状态写进卡片：

[components/src/dynamo/vllm/main.py:L832-L856](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/main.py#L832-L856)
> `runtime_config.total_kv_blocks`、`max_num_seqs`、`max_num_batched_tokens`、`kv_event_publishing_enabled`、`kv_state_endpoint` 逐项写入。`num_gpu_blocks` 为 `None` 时（Ray DP 后端不回写主进程配置）以 0 为哨兵继续注册，KV 容量指标不可用——注释标了 `TODO(upstream-vllm)`。

[components/src/dynamo/vllm/main.py:L897-L926](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/main.py#L897-L926)
> `create_frontend_media_config(config.frontend_decoding)` 产出 `media_decoder`/`media_fetcher` 传给 `register_model`——前端图像解码（u4-l5）由 worker 卡片**反向**通告给前端；同处 `router_config=build_router_config(config.router_advertisement)` 让这组 worker 自带路由策略广告（u6-l1 讲过「worker 卡片整体覆盖 frontend 配置」）。

#### 4.3.4 代码实践

**实践目标**：搞清楚「KV 事件打开」的最小条件集，并用日志验证发布器确实建立。

**操作步骤**：

1. 静态推理：列出 `setup_kv_event_publisher` 返回非 `None` 的全部必要条件（对照 L443-L454），写成三元组合表。
2. 若本地装了 `vllm` extra 且有一块 GPU，运行聚合拓扑：

   ```bash
   cd examples/backends/vllm/launch
   ./agg.sh --model Qwen/Qwen3-0.6B \
       --enable-prefix-caching \
       --kv-events-config '{"enable_kv_cache_events": true, "endpoint": "zmq://127.0.0.1:5557"}'
   ```

   上面 `--kv-events-config` 的 JSON 字段名以你本地 vLLM 版本的 `KVEventsConfig` 定义为准（源码只保证消费 `enable_kv_cache_events` 与 `endpoint` 两个键，见 L450 与 L477-L480），**待本地验证**。
3. 在 worker 日志里 grep 这两行：
   - `KV event publisher for dp_rank=0 subscribing to vLLM at tcp://127.0.0.1:…`（[main.py:L481-L483](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/main.py#L481-L483)）
   - `Worker reading KV events for dp_rank=0 from tcp://127.0.0.1:…`（[main.py:L497-L499](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/main.py#L497-L499)）
4. 用相同前缀连发两条请求，观察第二条的 TTFT 是否下降（KV 重合带来的 prefill 抵扣，呼应 u6-l2 的成本模型）。
5. 若无 GPU：跳到步骤 3 的替代方案——在 `mocker` 后端上用 KV 路由观察同类事件流（u6-l3 的实践已搭好环境），并对照本节源码说明 mocker 与 vLLM 在「谁发布事件」上的差异（mocker 在引擎内直接合成事件；vLLM 要经 ZMQ 跨进程转发）。

**需要观察的现象**：两条日志中的端口号应与 `kv_events_config.endpoint`（加 dp_rank 偏移后）一致；关掉 `--enable-prefix-caching` 重跑，这两行日志应**消失**且函数返回 `None`。

**预期结果**：一份「条件 → 现象」对照记录：三个条件各自缺省时日志输出有何不同；有 GPU 时附两条请求的 TTFT 对比数字。无 GPU 环境下步骤 2-4 标注「待本地验证」，以步骤 1 与 5 的静态结论交付。

### 4.4 kv_connector_protocols：P/D KV 交接协议

#### 4.4.1 概念说明

P/D 分离（u7-l1）里，prefill 算完的 KV 必须交给 decode。vLLM 把「怎么交」开放给可插拔的 KV connector，而不同 connector 对 `kv_transfer_params` 这个字典的**形状**意见不一：

- **NIXL 是拉取式**（pull）：prefill 响应里直接带上块位置信息，decode 从响应里读出来自己去拉。
- **Mooncake 是推送式**（push）：prefill 先预分配一个 `transfer_id`，把块推到该 ID 下；decode 拿着同一个 ID 和 prefill 的 bootstrap 地址去取。

这个模块把这些差异隔离在 `KvConnectorProtocol` 抽象后面，让 `handlers.py` 的 prefill 处理器保持 connector 无关。加一个新 connector = 一个类 + 一条注册表记录。

#### 4.4.2 核心流程

```text
make_kv_connector_protocol(vllm_config)
  ├─ kv_transfer_config 缺失        → NixlConnectorProtocol（非 P/D 路径的默认）
  ├─ name ∈ {MultiConnector, PdConnector}
  │     └─ 在 extra_config["connectors"] 里找 PD-capable 子连接器
  │          ├─ 0 个  → ValueError（无 PD 子连接器）
  │          ├─ >1 个 → ValueError（vLLM 禁止多连接器同时产出 kv_transfer_params）
  │          └─ 恰 1 个 → 用「子连接器视角的 config」构造其协议
  └─ name ∈ KV_CONNECTOR_PROTOCOLS   → 对应协议类
       └─ 都不是 → ValueError（配置错误，不是可回退的场景）
```

每个协议实现两个方法：`prefill_request_kv_transfer_params()`（发给 vLLM 的 prefill 请求带什么）与 `decode_request_kv_transfer_params(prefill_response)`（从 prefill 响应推导 decode 侧参数）。

#### 4.4.3 源码精读

模块头注释直接给出了设计动机，值得整段读：

[components/src/dynamo/vllm/kv_connector_protocols.py:L4-L13](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/kv_connector_protocols.py#L4-L13)
> 「vLLM 的 KV connector 对 `kv_transfer_params` 的形状各执一词：NIXL 拉取式，Mooncake 推送式。本模块把每种协议隔离在 `KvConnectorProtocol` 之后，处理器保持 connector 无关，新连接器 = 一个类 + 一条注册表项。」

抽象基类：

[components/src/dynamo/vllm/kv_connector_protocols.py:L23-L38](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/kv_connector_protocols.py#L23-L38)
> 「每个 prefill 请求一个实例；承载任意逐请求状态」——这句话预告了 Mooncake 的 `transfer_id` 要存在实例字段里。

两种协议的对照：

[components/src/dynamo/vllm/kv_connector_protocols.py:L42-L58](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/kv_connector_protocols.py#L42-L58)
> `NixlConnectorProtocol`：prefill 侧发一个全 `None` 的骨架（`do_remote_decode=True, do_remote_prefill=False`），decode 侧参数**原样取自** `prefill_response.kv_transfer_params`——引擎自己填好了位置，Dynamo 只是搬运。

[components/src/dynamo/vllm/kv_connector_protocols.py:L61-L106](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/kv_connector_protocols.py#L61-L106)
> `MooncakeConnectorProtocol`：构造时就 `uuid.uuid4()` 预分配 `transfer_id` 并存为实例状态；构造期还 import vLLM 的 `get_mooncake_bootstrap_addr`（缺 mooncake 时**在请求建立阶段**就报错，而不是 prefill 跑完才炸）。decode 参数要带 `remote_bootstrap_addr = f"http://{host}:{port}"`——L103 的行内注释解释了 `http://` 前缀不可省，因为 decode 侧要做 `addr + "/query"` 拼接。选 `get_mooncake_bootstrap_addr` 而非随手 `get_ip()` 的理由写在 L69-L77：那个 helper 会正确处理 `local_engines_only` 与 `data_parallel_master_ip`，随便挑一块网卡只是「碰巧能对上」。

注册表与包装器：

[components/src/dynamo/vllm/kv_connector_protocols.py:L109-L121](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/kv_connector_protocols.py#L109-L121)
> 三个键映射两条协议（`NeuronNixlConnector` 复用 NIXL 协议）；`MULTI_CONNECTOR_WRAPPERS` 列出 `MultiConnector` 与 dynamo 自己的 `PdConnector`——后者继承 vLLM 的 `MultiConnector`、配置形状相同，都把 PD 协调委托给唯一的 PD 子连接器。

解析入口的「宁可报错不可静默回退」：

[components/src/dynamo/vllm/kv_connector_protocols.py:L140-L172](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/kv_connector_protocols.py#L140-L172)
> 未配置 connector 时默认 NIXL（非 P/D 路径）；但**已配置却查不到**的 connector 名直接 `ValueError`。L146-L147 的注释是这一节的思想纲领：「dynamo 与 vLLM 引擎的失配是配置错误而非良性默认；静默回退 NIXL 会发出错误的线格式，最终以晦涩的 decode 失败浮出水面。」错误信息还区分了「拼写/改名」与「新连接器」两种修复路径。

子连接器绑定要用「孩子的视角」：

[components/src/dynamo/vllm/kv_connector_protocols.py:L232-L253](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/kv_connector_protocols.py#L232-L253)
> `_child_vllm_config` 复刻 vLLM `MultiConnector` 构造子连接器的规则：子项覆盖同名键、`engine_id` 缺省回落到包装器的值。L240-L244 的注释说明若绑定到包装器 config，包装器的 `engine_id` 会泄漏进 `remote_engine_id`，decode 侧永远无法把传输匹配到真正持有它的子连接器。

处理器侧的消费点：

[components/src/dynamo/vllm/handlers.py:L874-L910](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/handlers.py#L874-L910)
> `_update_kv_transfer_params` 把协议产出的参数塞进 `sampling_params.extra_args["kv_transfer_params"]`，同时处理 `router_hint` 的保留：prefill 换新参数对象时保留请求带来的路由提示，decode 交接用 prefill 产出的参数、**不得**继承过期的 prefill 侧提示。

另外两个与本模块相关的入口：

[components/src/dynamo/vllm/main.py:L653](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/main.py#L653) 与 [components/src/dynamo/vllm/kv_connector_protocols.py:L124-L137](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/kv_connector_protocols.py#L124-L137)
> `disable_hybrid_kv_cache_manager_for_incompatible_pd_connector` 在 vLLM 构建 KV cache 分组**之前**被调用：`PdConnector` 的子连接器若不全部支持 HMA，就整体关掉 hybrid KV cache manager，避免引擎带着不兼容的分组假设启动。`args.py` 的 `_uses_dynamo_connector`（[args.py:L493-L510](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/args.py#L493-L510)）则识别直连或嵌在 `PdConnector` 里的 `DynamoConnector`（KVBM），用于启用 consolidator 端点（u9 的入口）。

#### 4.4.4 代码实践

**实践目标**：通过「写一个假协议」验证你理解了协议边界与注册表契约（纯静态 + 可选单测，不需要 GPU）。

**操作步骤**：

1. 在纸上（或一个不落盘的草稿里）为虚构的 `FakeHttpConnector` 写协议类：prefill 侧返回 `{"do_remote_decode": True, "session": "<uuid>"}`，decode 侧从 `prefill_response.fake_session` 取回。
2. 回答三个问题：
   - 它需要存实例状态吗？（对照 Mooncake 的 `transfer_id`）
   - 把它加进 `KV_CONNECTOR_PROTOCOLS` 后，`make_kv_connector_protocol` 对它走哪条分支？
   - 如果用户配了 `kv_connector="TypoConnector"`，异常在哪一行抛出、错误信息建议用户做什么？
3. （可选落盘验证）在仓库外建一个临时 venv，`pip install` 项目后用 `unittest.mock` 构造一个带假 `kv_transfer_config` 的对象，直接调用 `make_kv_connector_protocol`，断言三种行为：无 config → NIXL；`"PdConnector"` 带两个 PD 子连接器 → `ValueError`；`"TypoX"` → `ValueError`。不要修改仓库源码。
4. 对照 [kv_connector_protocols.py:L208-L229](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/kv_connector_protocols.py#L208-L229) 检查你的断言与源码的错误分支一致。

**需要观察的现象**：步骤 3 的三个断言中，「两个 PD 子连接器」那条的错误信息会列出两个冲突名字并引用 vLLM 的约束原文（"Only one connector can produce KV transfer params"）。

**预期结果**：假协议类的草稿 + 三问答案 + （可选）三个通过的断言。若未安装环境，步骤 3 标注「待本地验证」，以源码行号引用佐证三问答案。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `MooncakeConnectorProtocol` 在 `__init__` 里就分配 `transfer_id`，而不是等 prefill 请求到来再分？

**答案**：因为 `transfer_id` 必须同时出现在 prefill 请求参数与 decode 请求参数里（[L88-L106](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/kv_connector_protocols.py#L88-L106) 两处引用同一个 `self._transfer_id`）。协议对象本身是「每个 prefill 请求一个实例」（基类 docstring），把 id 放在构造期即建立了两端的共享标识；同理，import bootstrap helper 也放在构造期，让缺依赖的错误在请求建立时就暴露（L74-L84 的注释明确说了这个意图）。

**练习 2**：`make_kv_connector_protocol` 对「未知 connector 名」抛 `ValueError` 而不是回落 NIXL。假设改成静默回落，会在什么时刻、以什么形式让用户感知到？

**答案**：不会在启动时暴露，而是在 P/D 分离真正运行时：NIXL 协议会发出「拉取式」的参数骨架（`remote_block_ids: None` 等），而按推送式实现的连接器等着收 `transfer_id`，结果就是 decode 侧拿不到有效块位置、传输永远不完成，表现为晦涩的 decode 失败或超时——正是 L146-L147 注释所说「surface as opaque decode failures」。

**练习 3**：`_child_vllm_config` 为什么要清空 `kv_connector_extra_config` 再逐项 setattr？

**答案**：因为子连接器不继承包装器的 extra config——那里存放的恰恰是 `connectors` 列表本身（L247-L248 注释）。若不清空，子连接器的配置里会残留包装器的子列表；逐项 setattr 则复刻了 vLLM `MultiConnector`「子项覆盖同名键、其余继承」的构造规则，保证协议看到的 config 与该子连接器实际运行的 config 一致。

### 4.5 engine_generate 能力广告与 handlers 生成桥接

#### 4.5.1 概念说明

这一节澄清一个容易望文生义的地方：`engine_generate.py` **不是**生成流程本身——它只有 41 行，做的是「能力广告」：向模型卡片的 `engine_specific` 字段写入 `vllm_inference_v1_generate = true`，告诉前端「这个 worker 能直接接 vLLM 原生 `/inference/v1/generate` 请求」。真正的生成桥接在 `handlers.py`：`DecodeWorkerHandler.generate` → `generate_tokens` → `engine_client.generate()`。

为什么需要能力广告？因为 `/inference/v1/generate`（u4-l2 讲过的 generate 端点）要求请求以**不透明信封**直通引擎、绕过前端的分词与后处理管线。这只有在引擎真的是 vLLM、且 worker 接受 token 输入时才成立——能力位就是前端的判据。

#### 4.5.2 核心流程

```text
能力广告（注册期，一次性）:
  register_vllm_model(...)
    └─ publish_engine_generate_capability(runtime_config, model_input, model_type, worker_type, lora)
         ├─ model_input != Tokens            → False（文本输入模式不适用）
         ├─ worker_type == Prefill           → supported = (model_type == ModelType.Prefill)
         ├─ worker_type ∈ {Decode, Aggregated}
         │      → supported = model_type.supports_chat() or model_type == Completions
         └─ supported → set_engine_specific("vllm_inference_v1_generate", true)

生成桥接（每请求）:
  DecodeWorkerHandler.generate(request, context)
    ├─ request_id = context.id()
    ├─ first_token_source.bind(context, routing.dp_rank)
    ├─ 文本模式? _generate_text_mode : _generate_token_mode
    └─ async for chunk in _translate_vllm_client_errors(generator):
           首 token → decode_timer.stop_interval() / context.notify_first_token()
           yield chunk            # chunk 内含 token_ids、log_probs、finish_reason…
```

#### 4.5.3 源码精读

能力广告的判定矩阵：

[components/src/dynamo/vllm/engine_generate.py:L10-L41](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/engine_generate.py#L10-L41)
> 能力键名 `vllm_inference_v1_generate`；只有 `ModelInput.Tokens`（token 进 token 出）才有资格；prefill worker 单独按 `ModelType.Prefill` 标记位判定，decode/aggregated 按「支持 chat 或纯 completions」判定。同时把 tower-connector LoRA 的开关也写进 `engine_specific`，前端据此决定 LoRA 请求能否走原生路径。

生成桥接的入口——注意 context 的两个用法：

[components/src/dynamo/vllm/handlers.py:L3172-L3201](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/handlers.py#L3172-L3201)
> `request_id = context.id()` 用 Dynamo 上下文 id 做请求关联（呼应 u8-l3 将讲的 trtllm Engine ID map——那边要专门打日志才能把三种 id 对上，这边天然只有一种）；`first_token_source.bind(context, ...)` 把首 token 观测绑到上下文；token 模式下首次见到非空 `token_ids` 就 `context.notify_first_token()`——TTFT 的归一化信号从 worker 侧上报。

真正的引擎调用：

[components/src/dynamo/vllm/handlers.py:L2986-L3010](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/handlers.py#L2986-L3010)
> `self.engine_client.generate(prompt, sampling_params, request_id, lora_request=…, data_parallel_rank=…, trace_headers=…, priority=…)` 就是 Dynamo→vLLM 的最后一跳。注意外层的 `_generate_with_lora_admission_lock`：LoRA 准入要拿锁，避免适配器加载与请求并发冲突。`async for res in gen` 消费 vLLM 的 `RequestOutput` 流。

输出侧的翻译循环（节选）：

[components/src/dynamo/vllm/handlers.py:L3018-L3098](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/handlers.py#L3018-L3098)
> 逐 chunk 翻译成 Dynamo 的 dict：`token_ids`、`log_probs`/`top_logprobs`、`finish_reason`（经 `normalize_finish_reason` 归一）、`completion_usage`。两个值得注意的细节：(a) vLLM 只在 prefill 结束时给一次 `prompt_logprobs`、之后的 chunk 变 `None`，所以首个非 None 载荷要捕获下来挂到最后一个 chunk（L3013-L3026 注释）；(b) `routed_experts` 张量只在最后一个 chunk 序列化一次，逐 chunk base64 是纯浪费（L3069-L3075 注释）。引擎无输出时用 `"error: No outputs from vLLM engine"` 字符串——注释说明这是与 vLLM 字符串式 finish_reason 保持一致，由 Rust 侧解析成 `FinishReason::Error`（即 u4-l2 讲过的宽容读取）。

#### 4.5.4 代码实践

**实践目标**：把「能力广告」与「生成桥接」两个位置在源码里连起来，确认它们何时各跑一次。

**操作步骤**：

1. 在 [main.py:L819-L826](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/main.py#L819-L826) 找到 `publish_engine_generate_capability` 的调用点，确认它位于 `register_vllm_model` 内部——即**每个 worker 进程启动时执行一次**，写进卡片后随发现面广播。
2. 对照 [worker_factory.py:L1310-L1312](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/worker_factory.py#L1310-L1312)：`model_input = Text if use_vllm_tokenizer else Tokens`。据此回答：`--use-vllm-tokenizer` 开启时，能力广告会发生什么？
3. 追踪一条请求在 [handlers.py:L3172](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/handlers.py#L3172) 与 [L2974](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/handlers.py#L2974) 两函数之间的分工：`generate` 负责什么（提示：首 token、多模态校验、错误翻译），`generate_tokens` 负责什么（提示：引擎调用与 chunk 翻译）。
4. 写一段 5-8 行的总结，回答：「广告发生在 ______（注册期/每请求），桥接发生在 ______（注册期/每请求）；`/inference/v1/generate` 请求之所以能绕过前端分词，是因为 ______。」

**需要观察的现象**：步骤 2 的答案会导致 `publish_engine_generate_capability` 在 L23 就 `return False`——文本输入模式下不会写入能力键，前端也就不会把该 worker 视为原生 generate 可用。

**预期结果**：步骤 4 的填空能完整闭合；三个空分别是「注册期」「每请求」「worker 以 ModelInput.Tokens 注册并在卡片里带了 `vllm_inference_v1_generate` 能力位（前端因此以不透明信封透传 token_ids，见 u4-l2 的 generate 端点）」。

#### 4.5.5 小练习与答案

**练习 1**：为什么 prefill worker 的能力判定是 `model_type == ModelType.Prefill`，而 decode worker 用 `model_type.supports_chat() or model_type == Completions`？

**答案**：prefill worker 注册的 `ModelType.Prefill` 是一个**标记位而非 OpenAI surface**（[worker_factory.py:L1619-L1631](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/worker_factory.py#L1619-L1631) 注释），它没有 chat/completions 表面，判定只能按标记位做。decode/aggregated worker真正面向客户端，才需要按「是否暴露 chat 或 completions 表面」判定——generate 请求最终要从这些 worker 流式回给客户端。

**练习 2**：`generate_tokens` 里对空输出的处理（yield 一个 `finish_reason: "error: ..."` 的 chunk）与直接抛异常相比，好处是什么？

**答案**：流式协议里抛异常只能终止整条流且信息有限；而以「错误字符串式 finish_reason」收尾让 Rust 前端能按 u4-l2 讲过的 `FinishReason` 宽容解析规则把它转成带消息的终止帧，客户端拿到的是一个完整闭合的 SSE 流（带 finish_reason）而不是断流。源码注释（L3034-L3035）明说这是「与 vLLM 基于字符串的 finish_reason 保持一致，Rust 会解析成 `FinishReason::Error(message)`」。

**练习 3**：`context.notify_first_token()`（[handlers.py:L3200](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/handlers.py#L3200)）这个信号最终可能被谁消费？

**答案**：它沿 Dynamo 的 Context 机制（u2-l3 的取消/生命周期载体）向上一路传递：Rust 请求面把它与 `first_token_source`（[worker_factory.py:L1348-L1350](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/worker_factory.py#L1348-L1350) 绑定的 worker 类型/dp_rank 相结合），供前端观测 TTFT 归属（u4-l2 的 generate 端点指标里 `ttft_ms` 与 prefill/decode worker 字段正依赖这类信号）；同时也是 u3-l4 FirstResponseGuard「等首个响应后归还保留派发」的触发时机来源之一。

## 5. 综合实践

**贯穿任务：为一份假想的 `--disaggregation-mode decode --enable-lora --route-to-encoder` vLLM worker 配置写「启动审计清单」。**

假设你要向同事证明一份 decode worker 的启动配置会正确落地，请完成下面四件事，全部以本讲源码行号为证据：

1. **端点清单**：列出该 worker 会创建的全部 Dynamo endpoint（提示：从 [worker_factory.py:L1145-L1182](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/worker_factory.py#L1145-L1182) 数：generate、clear_kv_blocks、rl（若 enable_rl）、load/unload/list_loras（LoRA 启用）、get_perf_metrics），并标注每个端点serve 的是 Handler 的哪个方法（L1392-L1437）。
2. **卡片内容**：写出 `register_vllm_model` 会发布的 `worker_type`、`needs`、`ModelInput`、`total_kv_blocks` 来源、`router_config` 的来源（分散在 L1337-L1346、[main.py:L851-L856](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/main.py#L851-L856) 与 [main.py:L915-L925](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/main.py#L915-L925)）。
3. **旁路组件去留表**：对 KV 事件发布器、FPM 中继、consolidator、指标收集、LoRA 端点五项，判断该配置下各自是否建立，并给出判定代码行号。
4. **风险标注**：指出这份配置里最容易踩的一个坑（提示：`_maybe_get_encode_worker_client` 在 [worker_factory.py:L1718-L1731](https://github.com/ai-dynamo/dynamo/blob/d7f06b591f3af60270811b7e2d5d7d0a404b934a/components/src/dynamo/vllm/worker_factory.py#L1718-L1731) 会 `wait_for_instances()` 无限等待——若 encode worker 没起，worker 卡在哪一行？这与 u3-l1 讲过的 client watch 无限挂起是同一现象）。

**验收标准**：清单中每一项都有 `文件:行号` 支撑；第 4 项能同时说清「卡住的是哪条 await、日志上会看到什么（`Waiting for Encoder Worker Instances ...` 之后无输出）」。

## 6. 本讲小结

- `dynamo.vllm` 对 vLLM 是**集成不是侵入**：全部挂钩走 vLLM 公开 API（`AsyncLLM.from_vllm_config` 的 `stat_loggers`、`kv_events_config`、`kv_transfer_config`），`worker()` 只做编排，具体装配交给被注入六个 `setup_*` 函数的 `WorkerFactory`。
- `WorkerFactory.create()` 是两级分派器：任务类型（realtime/embedding/classify/encode/生成）优先，分离角色（prefill/decode/aggregated）其次；每条路径都是「取端点 → 建引擎 → 造 Handler → 注册卡片 → serve」五段式，差异在取舍（嵌入路径的 docstring 逐条解释了它跳过什么、为什么）。
- KV 事件是三段接力：vLLM EngineCore 经 ZMQ PUB 广播块哈希增删 → Dynamo 的 `KvEventPublisher`（Rust 对象，即 u6-l3 的 zmq_listener 管道）订阅并归一化 → 转发到事件面供路由器回填索引。打开它需要三道闸全过：prefix caching、`kv_events_config` 存在、事件未被显式关闭。
- `kv_connector_protocols.py` 把「P/D 交接参数的形状」差异（NIXL 拉取式 / Mooncake 推送式 / MultiConnector 委托唯一的 PD 子连接器）隔离在协议类后；解析失败宁可 `ValueError` 也不静默回退 NIXL——错配是配置错误，不是可默认的场景。
- `engine_generate.py` 是**能力广告**而非生成流程：向卡片写入 `vllm_inference_v1_generate` 能力位，让前端知道该 worker 可接原生 `/inference/v1/generate` 的不透明信封；真正的生成桥接是 `handlers.py` 的 `generate` → `generate_tokens` → `engine_client.generate()`。
- 指标双通道同构：引擎统计经 `StatLoggerFactory` 回调流入 `DynamoStatLoggerPublisher`，一支出事件面（路由器的负载信号），一支写 Prometheus gauge（运维观测）。

## 7. 下一步学习建议

本讲把「worker 进程内部」讲完了，接下来按依赖关系推荐三步：

1. **u8-l2（SGLang 后端接入）**：对照阅读 `dynamo.sglang` 的 `main.py` 与 `request_handlers/handler_base.py`，重点比较同一组问题（引擎构造、请求翻译、KV 交接）在另一个引擎上的答案——SGLang 的 bootstrap 寻址与 vLLM 的 connector 协议是两种风格的 P/D 交接。
2. **u8-l8（Embeddings 服务化）**：本讲多次遇到 `embedding_worker` 分支的「跳过」逻辑，那一讲展开 `embedding_worker_processes.py` 的多进程池、base64 字节线格式与跨语言分词 parity。
3. **u6-l3（KV 事件流）回头补深度**：本讲只讲到 `KvEventPublisher` 的 Python 构造点；那一讲的 normalizer 三道闸、`Cleared` 屏障语义是理解「事件洪峰不淹死路由器」的关键。
