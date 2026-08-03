# Diffusion 引擎：执行模式与输出流

## 1. 本讲目标

本讲是进阶层 U5（Diffusion 模块：DiT 推理引擎）的第一篇，目标是把 `DiffusionEngine` 这个「编排者」讲透。读完本讲，你应该能够：

- 说清楚 `DiffusionEngine` 在整个 diffusion 子系统里扮演什么角色、它自己「不做推理」体现在哪里。
- 区分两种执行模式 `REQUEST_BATCH` 与 `STEP_BATCH` 的语义、选择条件，以及它们分别把一个请求交给了哪些执行函数（`execute_batch` vs `prepare_encode/denoise_step/step_scheduler/post_decode`）。
- 理解 `step_streaming()` 提供的「统一输出流」语义：流式与非流式调用方消费的是同一条 `asyncio.Queue`，区别只在「消费多少」。
- 识别 `OmniDiffusionConfig` 里决定执行行为的关键字段（`step_execution`、`streaming_output`、`max_num_seqs`、`request_batch_max_wait_ms`、`engine_backend`），以及 `DiffusionOutput` 这个跨进程输出容器的字段结构。
- 看懂 pre/post 处理函数（`pre_process_func` / `post_process_func`）的「注册表模式」是如何按模型架构名挂载的。

本讲承接 u3-l3「每个 stage 是独立 EngineCore 子进程」的认知——当一个 stage 的执行类型是 `DIFFUSION` 时，它内部运行的就是这里讲的 `DiffusionEngine`。

## 2. 前置知识

在进入源码前，先用通俗语言建立几个概念。

**扩散（Diffusion）模型与「步」的概念。** 扩散模型（DiT，Diffusion Transformer）生成一张图/一段视频，不是像大语言模型那样「一个 token 一个 token」自回归地蹦出来，而是从一团纯噪声出发，反复迭代去噪若干步（`num_inference_steps`），每一步都是一次完整的 Transformer 前向（`transformer.forward`）加一次调度器更新（`scheduler.step`），最后把潜变量（latents）交给 VAE 解码成像素。因此扩散推理天然有一个「步循环」结构。

**编排者（Orchestrator）的含义。** 一个推理引擎可以被拆成几件事：①接收请求；②决定谁先跑（调度）；③真正在 GPU 上算（执行）；④把结果交回调用方。`DiffusionEngine` 只做 ①②④ 这三件「编排」工作，而把 ③ 委托给独立的 worker 进程（多进程执行器 `MultiprocDiffusionExecutor`）。这种「自己不下场、只做裁判和快递员」的设计，就是本讲反复出现的「编排者」定位。

**请求级批处理 vs 步级批处理。** 这是本讲的核心区分点：

- **请求级（REQUEST_BATCH）**：把一整个请求（含全部去噪步）当作一个不可分割的单元，执行函数 `execute_batch` 一次跑完整个 `pipeline.forward(batch)`。多个「兼容」的请求可以融合成一个大 batch 一起前向。
- **步级（STEP_BATCH）**：把去噪循环拆开，按步推进。执行函数 `execute_step` 每次只推进一步，对应 pipeline 上的 `prepare_encode / denoise_step / step_scheduler / post_decode` 四个子步骤。步与步之间可以往批里加新请求、移除已完成的请求（连续批处理），也能产出中间块（流式输出）。

如果你已经读过 u3-l3，可以把 `DiffusionEngine` 类比成那个 stage 进程内部的「迷你 vLLM」：它有自己的 scheduler、executor、输出队列，只不过跑的不是 AR 推理而是扩散推理。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 |
| --- | --- |
| [`vllm_omni/diffusion/diffusion_engine.py`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py) | `DiffusionEngine` 编排者本体：执行模式选择、调度器/执行器装配、统一输出流、后台 busy loop、pre/post 处理。 |
| [`vllm_omni/diffusion/data.py`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/data.py) | 数据结构：`OmniDiffusionConfig`（引擎配置）、`DiffusionOutput`（输出容器）、`DiffusionParallelConfig` 等。 |
| [`docs/design/module/dit_module.md`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/module/dit_module.md) | 设计文档，本讲重点用其中的「1. Diffusion Engine」「Execution Modes」「Output Stream Lifecycle」「6. Data Flow」几节。 |
| [`vllm_omni/diffusion/executor/abstract.py`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/executor/abstract.py) | 执行器抽象基类，定义 `execute_batch` / `execute_step` 的契约。 |
| [`vllm_omni/diffusion/registry.py`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/registry.py) | 模型注册表与 pre/post 处理函数的「架构名 → 函数名」映射表。 |
| [`vllm_omni/diffusion/models/interface.py`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/interface.py) | step 执行协议 `SupportsStepExecution`，定义 `prepare_encode/denoise_step/step_scheduler/post_decode` 的签名。 |
| [`vllm_omni/diffusion/models/z_image/pipeline_z_image.py`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/z_image/pipeline_z_image.py) | 入门示例模型 Z-Image 的 pipeline，用来观察 `supports_request_batch` 与 `forward`。 |

## 4. 核心概念与源码讲解

### 4.1 DiffusionEngine 编排者：定位与初始化

#### 4.1.1 概念说明

`DiffusionEngine` 是扩散推理系统的**编排者**（orchestrator）。设计文档把它的职责概括为：「拥有请求准入、调度器/执行器协调、输出流投递、取消清理、启动预热」。换句话说，它自己**不直接调用 Transformer**，而是：

- 接收 `OmniDiffusionRequest`；
- 把请求交给 scheduler 决定何时跑；
- 把调度结果交给 executor（多进程执行器），由 executor 把工作分发到 worker 进程真正算；
- 把 worker 返回的 `DiffusionOutput` 投递回调用方的输出队列。

理解「编排者不下场」这一点，是理解后续所有代码的前提：你在 `diffusion_engine.py` 里**看不到** `transformer.forward`、`vae.decode` 这些真正的算子调用——它们都在 worker 进程的 pipeline 里。引擎里出现的 `execute_fn`、`self.executor.execute_batch(...)` 都只是「把活派出去」。

#### 4.1.2 核心流程

`DiffusionEngine` 的初始化严格按顺序装配五个部件，顺序见源码 [`__init__`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L163-L184)：

```text
__init__(od_config, scheduler=None)
  ├─ _init_process_hooks(od_config)     # 1. 装配 pre/post 处理函数（按模型架构名）
  ├─ execution_mode = _resolve_execution_mode(od_config)  # 2. 决定 REQUEST_BATCH / STEP_BATCH
  ├─ _init_executor(od_config)          # 3. 构造多进程执行器（启动 worker 进程）
  ├─ _init_scheduler(od_config, scheduler)  # 4. 按执行模式选 StepScheduler / RequestScheduler
  ├─ _init_runtime_state()              # 5. 建输出队列字典、锁、busy loop 状态
  ├─ _init_execute_fn()                 # 6. 把 execute_fn 绑到 execute_batch 或 execute_step
  └─ _log_execution_mode(od_config)     # 7. 打印当前模式与批参数
```

注意第 2 步必须早于第 3、4 步：执行器与调度器的选型都依赖 `execution_mode`。实际构造引擎时不应直接 `DiffusionEngine(...)`，而应走工厂 [`make_engine`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L669-L687)，它会先用 [`resolve_engine_class`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L634-L667) 解析 `engine_backend`（支持 `"default"`、子类、或导入路径字符串），再调用 `run_startup_warmup()` 做一次 dummy run 预热。`engine_backend` 字段定义在 [`OmniDiffusionConfig.engine_backend`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/data.py#L649-L655)。

#### 4.1.3 源码精读

引擎类声明与构造（标注每一步作用）：

[`diffusion_engine.py:L148-L184`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L148-L184) — `DiffusionEngine` 类与 `__init__`。`__init__` 严格按 `_init_process_hooks → _resolve_execution_mode → _init_executor → _init_scheduler → _init_runtime_state → _init_execute_fn → _log_execution_mode` 顺序装配。

工厂方法（带预热）：

[`diffusion_engine.py:L669-L687`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L669-L687) — `make_engine`：先 `resolve_engine_class` 解析后端类，再实例化，最后 `run_startup_warmup()`。预热失败会 `close()` 引擎并抛错（见 [`run_startup_warmup`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L815-L842)）。

pre/post 处理函数装配：

[`diffusion_engine.py:L186-L191`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L186-L191) — `_init_process_hooks`：通过 registry 拿到当前模型架构对应的 `pre_process_func` 与 `post_process_func`，并缓存 post 处理函数是否接受 `sampling_params` 参数。具体的注册表机制见 4.4。

#### 4.1.4 代码实践

**实践目标**：确认引擎初始化顺序与「编排者不下场」的边界。

**操作步骤（源码阅读型）**：

1. 打开 [`diffusion_engine.py:L163-L184`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L163-L184)，把 `__init__` 里七步的顺序抄下来。
2. 用搜索工具在整个 `diffusion_engine.py` 里查找 `transformer.forward`、`vae.decode`、`scheduler.step` 这类真正的算子调用。

**需要观察的现象**：第 2 步应当**找不到**这些算子调用——它们都在 worker 进程的 pipeline 里（如 u5-l3/u5-l4 会讲）。这印证了「编排者不下场」。

**预期结果**：你能说出「引擎里出现的 `execute_fn` / `self.executor.execute_batch(...)` 都只是把工作派给 worker 进程，引擎本身不持有 pipeline 权重也不算前向」。

> 运行类验证（如想确认 `make_engine` 的预热报错路径）需本地具备 GPU 与模型权重，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么构造引擎要走 `make_engine` 而不是直接 `DiffusionEngine(od_config)`？

**参考答案**：`make_engine` 多做了两件 `__init__` 没做的事——用 `resolve_engine_class` 解析 `engine_backend`（可能指向一个 `DiffusionEngine` 子类，如 AR-Diffusion 引擎），以及调用 `run_startup_warmup()` 做 dummy run 预热。直接构造会跳过预热，可能导致首请求很慢或显存状态未就绪。

**练习 2**：`_resolve_execution_mode` 必须在 `_init_executor`、`_init_scheduler` 之前调用，为什么？

**参考答案**：因为执行器内部行为与调度器选型（`StepScheduler` vs `RequestScheduler`）都依赖 `execution_mode`。这是「配置决定装配」的典型依赖顺序。

---

### 4.2 两种执行模式：REQUEST_BATCH 与 STEP_BATCH

#### 4.2.1 概念说明

`DiffusionExecutionMode` 只有两个枚举值，定义在 [`diffusion_engine.py:L143-L145`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L143-L145)：

```python
class DiffusionExecutionMode(str, Enum):
    REQUEST_BATCH = "request_batch"
    STEP_BATCH = "step_batch"
```

两者的本质区别在于**「一次 execute_fn 调用推进多少」**：

- **REQUEST_BATCH（请求级）**：一个请求 = 完整去噪循环。执行函数 `execute_batch` 一次性把请求（或一批兼容请求）跑完整个 `pipeline.forward(batch)`，包含全部 `num_inference_steps` 步。`max_num_seqs=1` 是串行单请求路径；当 pipeline 声明 `supports_request_batch=True` 且 `max_num_seqs>1` 时，多个兼容请求可融合成一个大 batch 共享一次前向。
- **STEP_BATCH（步级）**：一次 `execute_step` 只推进一步去噪。对应 pipeline 上的 `prepare_encode`（一次性请求初始化）→ `denoise_step`（一次去噪前向）→ `step_scheduler`（一次调度更新）→ `post_decode`（解码/出块）。`max_num_seqs` 控制单个 step wave 里最多容纳多少个兼容的活跃请求。

设计文档对两者的权威描述见 [`dit_module.md:L81-L99`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/module/dit_module.md#L81-L99)。

#### 4.2.2 核心流程

执行模式的判定逻辑在 [`_resolve_execution_mode`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L193-L211)，判定树如下：

```text
step_execution=True?  ──yes──>  STEP_BATCH（且 supports_request_batch=False）
        │no
        ▼
streaming_output=True 但 step_execution=False?
        │yes──> 强制开启 step_execution，走 STEP_BATCH（并打告警）
        │no
        ▼
supports_request_batch(od_config)?  （看 pipeline 类是否声明该属性）
        │yes──> REQUEST_BATCH（允许 max_num_seqs>1 融合批）
        │no
        ▼
max_num_seqs > 1?
        │yes──> 报错（不支持批处理却要批，配置矛盾）
        │no───> REQUEST_BATCH（串行单请求路径）
```

关键点：

1. **流式输出强制步执行**：`streaming_output=True` 必须搭配 `step_execution=True`，否则引擎会自动帮你打开并告警（[`L195-L198`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L195-L198)）。因为只有步级执行才能在去噪中途产出中间块。
2. **批处理能力来自 pipeline 声明**：`supports_request_batch(od_config)` 会去解析 pipeline 类的 `supports_request_batch` 属性（[`diffusion_engine.py:L103-L109`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L103-L109)）。例如 Z-Image 在 [`pipeline_z_image.py:L165`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/z_image/pipeline_z_image.py#L165) 声明 `supports_request_batch = False`，所以它只能走串行路径。
3. **调度器随之确定**：`STEP_BATCH → StepScheduler`，`REQUEST_BATCH → RequestScheduler`（[`_init_scheduler`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L217-L228)）。
4. **execute_fn 随之绑定**：步级绑定 `executor.execute_step`，请求级绑定 `executor.execute_batch`（[`_init_execute_fn`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L271-L275)）。

> 关于两种模式对应的 pipeline 内部子步骤，设计文档 Data Flow 给了权威对照（[`dit_module.md:L948-L961`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/module/dit_module.md#L948-L961)）：REQUEST_BATCH 走 `Pipeline.forward(req_or_batch)` 内部的 `encode_prompt → prepare_latents → diffuse(循环) → vae.decode`；STEP 走 `prepare_encode → denoise_step → step_scheduler → post_decode`。

请求级批处理还有一个「准入等待」机制：在调度一个 wave 之前，busy loop 会短暂等待兼容请求凑齐，以提升融合 batch 的大小。其等待逻辑见 [`_wait_for_request_batch_admission_locked`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L467-L523)，跳出等待的三个判据为：

- \( \text{waiting} \ge \text{max\_batch} \)（凑满批立即走）；
- \( \text{waiting} > 0 \;\land\; (t - t_{\text{stable}}) \ge \text{stable\_window\_s} \)（队列稳定超过窗口即走，避免无限等）；
- \( t \ge \text{deadline} \)（绝对截止时间到，`deadline = t_0 + \text{max\_wait\_s}`）。

#### 4.2.3 源码精读

模式判定：

[`diffusion_engine.py:L193-L211`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L193-L211) — `_resolve_execution_mode`：先看 `step_execution`，再强制流式必须步执行，最后用 `supports_request_batch` 决定能否批处理；不支持批处理却 `max_num_seqs>1` 时直接 `raise ValueError`。

批处理能力探测：

[`diffusion_engine.py:L103-L109`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L103-L109) — `supports_request_batch`：解析（或从 registry 加载）pipeline 类，读取其 `supports_request_batch` 属性。

调度器与 execute_fn 装配：

[`diffusion_engine.py:L217-L228`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L217-L228) — `_init_scheduler`：按执行模式选 `StepScheduler` / `RequestScheduler`。

[`diffusion_engine.py:L271-L275`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L271-L275) — `_init_execute_fn`：把 `self.execute_fn` 绑到 `execute_step` 或 `execute_batch`。这是「一个属性指针」决定整条执行路径的关键一行。

执行器契约（execute_batch / execute_step 的抽象定义）：

[`executor/abstract.py:L82-L90`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/executor/abstract.py#L82-L90) — `execute_batch`（请求级路径）与 `execute_step`（步级路径）的抽象方法签名。两者都吃一个 `DiffusionSchedulerOutput`、返回 `BaseRunnerOutput`，差异只在内部推进的「粒度」。

步级 pipeline 协议（让 pipeline 把 forward 拆成四步）：

[`models/interface.py:L47-L75`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/interface.py#L47-L75) — `SupportsStepExecution` 协议：要求 pipeline 提供 `prepare_encode`（一次性初始化）、`denoise_step`（一次去噪前向）、`step_scheduler`（一次调度更新）、`post_decode`（解码/出块）。

#### 4.2.4 代码实践

**实践目标**：给定若干 `od_config` 字段组合，预测引擎会落到哪种执行模式、绑定哪个 `execute_fn`。

**操作步骤（源码阅读型）**：

1. 阅读 [`_resolve_execution_mode`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L193-L211) 与 [`_init_execute_fn`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L271-L275)。
2. 用搜索工具查 `supports_request_batch = True` 出现在哪些 pipeline（提示：见 4.1.3 给出的多文件命中），确认 Z-Image 是 `False`。
3. 对下表三种配置，填出 `execution_mode` 与 `execute_fn`：

| 配置 | step_execution | streaming_output | pipeline.supports_request_batch | max_num_seqs | execution_mode | execute_fn |
| --- | --- | --- | --- | --- | --- | ? |
| A | False | False | False (Z-Image) | 1 | ? | ? |
| B | False | False | True | 4 | ? | ? |
| C | False | True | False | 1 | ? | ? |

**需要观察的现象**：配置 C 中，`streaming_output=True` 会被强制改为步执行并打告警。

**预期结果**：A→`REQUEST_BATCH` / `execute_batch`（串行）；B→`REQUEST_BATCH` / `execute_batch`（融合批）；C→`STEP_BATCH` / `execute_step`（并告警）。若把 A 的 `max_num_seqs` 改成 2，会触发 `raise ValueError`（不支持批却要批）。

> 真正跑模型验证这些分支需本地 GPU 与权重，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：一个 pipeline 既没声明 `supports_request_batch`，也没声明 step 执行能力，用户却设置了 `max_num_seqs=4`，会发生什么？

**参考答案**：`supports_request_batch(od_config)` 会返回 `False`，于是 `_resolve_execution_mode` 走到 `if not self.supports_request_batch and _max_num_seqs(od_config) > 1` 分支，`raise ValueError`，提示「要么用 `max_num_seqs=1` 串行，要么换一个 `supports_request_batch=True` 的 pipeline」。

**练习 2**：为什么 `streaming_output=True` 必须搭配步执行？

**参考答案**：流式输出需要在去噪循环中途产出中间块（partial chunk），只有步级执行（`prepare_encode/denoise_step/.../post_decode`）才能在步与步之间切出块边界；请求级执行是一次性跑完整条 `pipeline.forward`，没有中间产物可流式投递。

---

### 4.3 统一输出流：step_streaming 与 asyncio.Queue 投递

#### 4.3.1 概念说明

`DiffusionEngine` 对外提供一条**统一的异步输出流**：无论流式还是非流式调用方，消费的都是同一个 `asyncio.Queue[DiffusionOutput]`，区别只在「消费到第几个就停」——流式方把每个 `yield` 都转发出去，非流式方排干整条流只取最后一个。设计文档把这个原则称为「Unified Stream Semantics」（[`dit_module.md:L101-L115`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/module/dit_module.md#L101-L115)）。

这条流的三个关键角色：

- **`step_streaming(request)`**：面向调用方的公共异步入口，负责 pre 处理 → 驱动 `async_add_req_and_stream_response` → 对每个产出的 `DiffusionOutput` 做 `postprocess_output`。
- **`async_add_req_and_stream_response(request)`**：把「入队」和「读流」串起来——先 `add_request` 拿到 `request_id`，再返回 `get_output_stream(request_id)` 这个异步生成器。
- **后台 busy loop**：在独立线程里反复「调度 → 执行 → 把结果塞进对应请求的 queue」。它和主事件循环之间的线程安全靠 `loop.call_soon_threadsafe` 保证。

#### 4.3.2 核心流程

一次请求从进入到产出的完整链路：

```text
调用方: await engine.step_streaming(request)
  │
  ├─ pre_process_func(request)          # 模型专属预处理
  │
  ├─ async for output in async_add_req_and_stream_response(request):
  │     │
  │     ├─ add_request(request)         # 入队 + 建 asyncio.Queue + notify busy loop
  │     └─ get_output_stream(request_id):
  │           while True:
  │             output = await queue.get()   # 等 busy loop 投递
  │             yield output
  │             if output.finished: break    # 末块即停
  │
  ├─ (异步模式) 若 output.async_output_id: await executor.wait_output_ready(...)
  ├─ postprocess_output(request, output)    # DiffusionOutput → list[OmniRequestOutput]
  └─ yield formatted_outputs
```

后台线程的 busy loop（简化）：

```text
_busy_loop():
  while not stop:
    _process_aborts_queue(); _process_rpc_queue()      # 处理取消与 RPC
    with cv:
      while 无请求且无 RPC/abort: cv.wait(1.0)          # 闲则等待
      if supports_request_batch: _wait_for_request_batch_admission_locked()  # 凑批
      sched_output = scheduler.schedule()              # 调度
    if sched_output 空: _emit_finished_outputs(...); continue
    runner_output = execute_fn(sched_output)           # 派给 worker（execute_batch / execute_step）
    finished = scheduler.update_from_output(...)       # 回填状态
    _emit_outputs(finished, scheduled, runner_output)  # 投递到各请求 queue
```

`_emit_outputs` 对两种模式的投递语义不同（[`diffusion_engine.py:L591-L628`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L591-L628)）：

- **REQUEST_BATCH**：只对 `finished_ids` 投递（请求级一个请求一次跑完，没有中间块）。
- **STEP_BATCH**：对每个 `scheduled_request_id` 都投递——已完成者投末块（`finished=True`），未完成但已有中间产物者投非末块（`req_output.result`），从而实现「逐步出块」。

线程安全投递由 [`_put_queue_output`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L990-L999) 保证：若事件循环在跑，用 `loop.call_soon_threadsafe(queue.put_nowait, output)`，否则直接 `put_nowait`。

#### 4.3.3 源码精读

公共异步入口：

[`diffusion_engine.py:L304-L345`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L304-L345) — `step_streaming`：pre 处理 → 驱动 `async_add_req_and_stream_response` → 异步输出就绪等待 → `postprocess_output` → 给每个 `request_output.metrics` 填写 `preprocess_time_ms / diffusion_engine_exec_time_ms / postprocess_time_ms` 等分项计时。注意它是 `AsyncGenerator`，每个 `yield` 就是一批格式化后的 `OmniRequestOutput`。

入队 + 读流的组合：

[`diffusion_engine.py:L719-L721`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L719-L721) — `async_add_req_and_stream_response`：`add_request` 拿 id，再返回 `get_output_stream(id)`。

[`diffusion_engine.py:L689-L698`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L689-L698) — `add_request`：在 `cv` 锁内建一个 `asyncio.Queue`，注册到 `self._out_streams[request_id]`，并 `notify_all` 唤醒 busy loop。

[`diffusion_engine.py:L700-L717`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L700-L717) — `get_output_stream`：循环 `await queue.get()` 并 `yield`，遇到 `output.finished` 即 `break`；`finally` 里清理 `_out_streams` 中的 queue（取消即清理）。

后台 busy loop：

[`diffusion_engine.py:L411-L465`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L411-L465) — `_busy_loop`：处理 abort/RPC → 等请求 →（请求级）凑批 → 调度 → `execute_fn` 派工 → `update_from_output` 回填 → `_emit_outputs` 投递。执行异常会被包成 `DiffusionOutput.from_exception(exc)` 的失败结果（[`L443-L457`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L443-L457)），不会让 busy loop 崩掉。

分模式投递：

[`diffusion_engine.py:L591-L628`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L591-L628) — `_emit_outputs`：REQUEST_BATCH 只投 finished；STEP_BATCH 对每个 scheduled 请求投递（末块/中间块）。

> 历史接口 `step()` 与 `async_add_req_and_wait_for_response()` 现在都是 deprecated 包装：它们内部排干同一条流、只返回末块（[`L347-L360`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L347-L360)、[`L723-L739`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L723-L739)）。新代码应直接用 `step_streaming` / `async_add_req_and_stream_response`。同步路径 `add_req_and_wait_for_response`（[`L741-L791`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L741-L791)）主要给 `_dummy_run` 预热用。

#### 4.3.4 代码实践

**实践目标**：跟踪一个 `DiffusionOutput` 块从 worker 返回到调用方 `await queue.get()` 的完整路径。

**操作步骤（源码阅读型）**：

1. 从 [`_busy_loop`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L411-L465) 的 `runner_output = self.execute_fn(sched_output)` 开始，跟着 `_emit_outputs` → `_put_output` → `_put_queue_output`（[L990-L1006](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L990-L1006)）→ 调用方的 `get_output_stream` 里 `await queue.get()`。
2. 重点关注跨线程的那一行：`loop.call_soon_threadsafe(queue.put_nowait, output)`。

**需要观察的现象**：busy loop 跑在 `worker_thread`（普通线程），而 `get_output_stream` 的 `await queue.get()` 跑在主事件循环（`main_loop`）。两者靠 `call_soon_threadsafe` 跨线程把 `put_nowait` 调度回事件循环线程执行，保证 `asyncio.Queue` 的线程安全。

**预期结果**：你能画出「worker 进程 → executor 返回 `runner_output` → busy loop `_emit_outputs` → `call_soon_threadsafe` → 主循环 `queue.get()` 唤醒 → `step_streaming` 的 `postprocess_output`」这条完整链路。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_put_queue_output` 要判断 `loop is not None and loop.is_running()` 再决定用 `call_soon_threadsafe` 还是直接 `put_nowait`？

**参考答案**：因为 busy loop 在普通线程里投递，而 `asyncio.Queue` 不是线程安全的。当事件循环在跑时，必须用 `call_soon_threadsafe` 把 `put_nowait` 调度回事件循环所属线程；当循环没在跑（例如同步预热路径 `add_req_and_wait_for_response`，没有启动后台 busy loop 的协程）时，直接 `put_nowait` 即可。

**练习 2**：`step_streaming` 里对 `output.async_output_id` 的处理是做什么的？

**参考答案**：这是「异步输出」快路径——worker 已经算完（GPU 可接下一个请求），但 D2H/共享内存打包还没完成。引擎拿到 `async_output_id` 后用 `executor.wait_output_ready(...)` 等后台搬运完成，再交给 postprocess。这样 GPU 不必等数据搬回 CPU 就能继续干活。

---

### 4.4 数据结构：OmniDiffusionConfig 与 DiffusionOutput

#### 4.4.1 概念说明

引擎的行为几乎完全由 `OmniDiffusionConfig` 这个大配置对象驱动，而引擎产出的原始结果则封装在 `DiffusionOutput` 里。两者都是定义在 [`data.py`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/data.py) 的 dataclass。

**`OmniDiffusionConfig`** 是 diffusion stage 的「总配置口袋」，字段极多，本讲只关注**决定执行模式与输出流**的那一小撮；其余字段（并行、量化、缓存、注意力后端等）留给后续讲义（u5-l2/u5-l3/u7 系列）。

**`DiffusionOutput`** 是「pipeline 完成后」的输出容器，承载张量结果以及错误/中止/流式/计时等元信息，会在跨进程（worker → executor → 引擎）之间传递。

此外，引擎在 pre/post 阶段调用的 `pre_process_func` / `post_process_func` 并非硬编码，而是通过 registry 里的「架构名 → 函数名」映射表按模型挂载，这是典型的**注册表模式（registry pattern）**。

#### 4.4.2 核心流程

`OmniDiffusionConfig` 影响本讲行为的关键字段（均在 [`data.py:L597-L840`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/data.py#L597-L840)）：

| 字段 | 默认值 | 作用 |
| --- | --- | --- |
| `step_execution` | `False` | 是否步级执行；`True` 直接落 STEP_BATCH。 |
| `streaming_output` | `False` | 是否流式输出；`True` 会强制开启 `step_execution`。 |
| `max_num_seqs` | `1` | 单次调度最多容纳多少兼容请求；`=1` 即串行。 |
| `request_batch_max_wait_ms` | `0.0` | 请求级凑批等待窗口（毫秒），`0` 表示不等待。 |
| `engine_backend` | `"default"` | 引擎后端选择，可填 `"default"`、子类或导入路径字符串。 |
| `model_class_name` | `None` | pipeline 架构名，决定 pre/post 处理函数与批处理能力探测。 |

`DiffusionOutput` 的关键字段（[`data.py:L1289-L1361`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/data.py#L1289-L1361)）：

| 字段 | 作用 |
| --- | --- |
| `output` | 真正的结果张量/元组/字典（pipeline 产出）。 |
| `error` / `error_status_code` / `error_type` | 错误信息与分类（区分客户端错误）。 |
| `aborted` / `abort_message` | 是否被中止及原因。 |
| `async_output_id` | 异步快路径的输出句柄（见 4.3.5）。 |
| `finished` | 是否末块（流式语义）。 |
| `chunk_index` / `total_chunks` | 流式分块的索引与总数。 |
| `to_cpu` | 跨进程传递时是否把张量搬到 CPU（避免接收侧误建 CUDA 上下文）。 |

`DiffusionOutput` 还提供工厂 [`from_exception`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/data.py#L1354-L1361)，把异常转成带状态码的输出，busy loop 正是用它把执行异常包成失败结果（见 4.3.3）。

pre/post 处理函数的注册表：`registry.py` 维护两张映射表 `_DIFFUSION_PRE_PROCESS_FUNCS`（[`L561-L587`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/registry.py#L561-L587)）与 `_DIFFUSION_POST_PROCESS_FUNCS`（[`L493-L551`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/registry.py#L493-L551)），key 是架构名（如 `"ZImagePipeline"`），value 是函数名字符串。引擎 `_init_process_hooks` 调用 [`get_diffusion_pre_process_func`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/registry.py#L688-L692) / [`get_diffusion_post_process_func`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/registry.py#L674-L678)，由 [`_load_process_func`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/registry.py#L660-L671) `importlib.import_module` 真正加载并调用工厂返回具体函数。注意：并非每个架构都注册了 pre/post 函数，未注册时返回 `None`（向后兼容）。

#### 4.4.3 源码精读

配置关键字段：

[`data.py:L824-L837`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/data.py#L824-L837) — `step_execution`、`streaming_output`、`max_num_seqs`、`request_batch_max_wait_ms` 四个决定执行模式与凑批的字段。

[`data.py:L649-L655`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/data.py#L649-L655) — `engine_backend`：引擎后端选择，由 `DiffusionEngine.resolve_engine_class` 解析。

输出容器：

[`data.py:L1289-L1361`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/data.py#L1289-L1361) — `DiffusionOutput`：`output`（结果）、错误/中止三件套、`async_output_id`、流式 `finished/chunk_index/total_chunks`、跨进程 `to_cpu` 标志，以及 `from_exception` 工厂。

postprocess 在引擎里的消费：

[`diffusion_engine.py:L362-L409`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L362-L409) — `postprocess_output`：先把 `DiffusionOutput` 的错误/中止态翻译成异常（客户端错误走 `client_error_from_metadata`），再调用 `post_process_func`（若注册）把原始 `output` 转成最终格式（如 PIL 图），最后 `format_diffusion_outputs` 包装成 `OmniRequestOutput` 列表。CPU offload 开启时会先把输出张量搬到 CPU（`_move_tensor_tree_to_cpu`）。

注册表查询：

[`registry.py:L660-L692`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/registry.py#L660-L692) — `_load_process_func` / `get_diffusion_pre_process_func` / `get_diffusion_post_process_func`：按架构名查表、动态 import 模块、调用工厂返回处理函数；架构未注册返回 `None`。

#### 4.4.4 代码实践

**实践目标**：搞清楚一个具体模型（如 Z-Image）的 pre/post 处理函数是怎么被挂上的、输出怎么被格式化的。

**操作步骤（源码阅读型）**：

1. 在 [`_DIFFUSION_POST_PROCESS_FUNCS`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/registry.py#L493-L551) 里查 `"ZImagePipeline"` 对应的函数名（应为 `"get_post_process_func"`）。
2. 确认 Z-Image **没有**出现在 [`_DIFFUSION_PRE_PROCESS_FUNCS`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/registry.py#L561-L587) 里，因此 `pre_process_func` 为 `None`。
3. 跟着 [`postprocess_output`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L362-L409) 看一遍：错误态 → post_process_func → format_diffusion_outputs。

**需要观察的现象**：当 `post_process_func is None` 时（某些架构），`postprocess_output` 会直接把 `output_data` 当作已处理结果（[L398-L399](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L398-L399)）。

**预期结果**：你能说出「架构名是连接 pipeline、批处理能力、pre/post 处理函数的统一 key」——同一个 `model_class_name` 既被 `supports_request_batch()` 探测批处理能力，又被 registry 用来挂 pre/post 函数。

#### 4.4.5 小练习与答案

**练习 1**：`DiffusionOutput.to_cpu=True` 解决什么问题？

**参考答案**：跨进程（如 step-execution 模式）传递输出时，若张量留在 GPU 上，接收进程可能会因为反序列化 CUDA 张量而意外初始化一个 CUDA 上下文。`to_cpu=True` 在构造时就把张量 `.detach().cpu()`，避免接收侧建立多余的 CUDA 上下文。

**练习 2**：为什么 `postprocess_output` 要区分「客户端错误」和普通错误？

**参考答案**：客户端错误（如参数非法、输入不支持）应当以带 HTTP 状态码的形式抛给 API 层（`client_error_from_metadata`），让上层返回 4xx；而内部执行错误是 `RuntimeError`，应返回 5xx。区分两者让在线服务（u6）能给客户端正确的错误码。

---

## 5. 综合实践

**任务**：对比 `REQUEST_BATCH` 与 `STEP_BATCH` 两种模式下「一个请求」分别走过哪些执行函数，画出对照表。这是本讲的 headline 实践（practice_task）。

**操作步骤（源码阅读型）**：

1. 确定模式入口：阅读 [`_resolve_execution_mode`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L193-L211) 与 [`_init_execute_fn`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L271-L275)，确认两种模式分别绑定 `execute_batch` / `execute_step`。
2. 查执行器契约：阅读 [`executor/abstract.py:L82-L90`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/executor/abstract.py#L82-L90)，确认两者签名一致但语义不同。
3. 查 pipeline 内部分支：阅读设计文档 Data Flow 的两分支（[`dit_module.md:L948-L961`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/module/dit_module.md#L948-L961)），以及步级协议 [`models/interface.py:L47-L75`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/interface.py#L47-L75)。
4. 查一个真实 pipeline：看 Z-Image 的 [`supports_request_batch`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/z_image/pipeline_z_image.py#L165)（`False`）与 [`forward`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/z_image/pipeline_z_image.py#L413)（请求级单体入口）。

**需要观察的现象**：请求级模式把整条 `pipeline.forward` 当一个原子单元；步级模式把它拆成四步，且步与步之间由 busy loop 反复调度。

**预期结果**：填出下面的对照表。

| 维度 | REQUEST_BATCH | STEP_BATCH |
| --- | --- | --- |
| 触发条件 | `step_execution=False`（且流式关闭） | `step_execution=True`（或 `streaming_output=True` 被强制开启） |
| 引擎绑定的 `execute_fn` | `executor.execute_batch` | `executor.execute_step` |
| 调度器 | `RequestScheduler` | `StepScheduler` |
| 单次 execute 推进量 | 整个请求（全部去噪步） | 一步去噪 |
| pipeline 内部路径 | `forward(batch)`：`encode_prompt → prepare_latents → diffuse(循环) → vae.decode` | `prepare_encode → denoise_step → step_scheduler → post_decode` |
| 中间块输出 | 无（只在 finished 时投递） | 有（逐步投递，末块 `finished=True`） |
| 多请求融合 | 需 pipeline 声明 `supports_request_batch=True` + `max_num_seqs>1` | 步间可连续加/移请求，`max_num_seqs` 控 wave 大小 |
| 典型适用 | 普通文生图（Z-Image/Qwen-Image） | 视频流式生成、连续批处理 |

> 实际跑模型观察两种模式的日志（`_log_execution_mode` 会打印 `[RequestBatch] engine init ...`）需本地 GPU 与权重，**待本地验证**。

## 6. 本讲小结

- `DiffusionEngine` 是扩散推理的**编排者**：自己做请求准入、调度协调、输出投递、取消清理、启动预热，把真正的前向交给多进程 executor/worker，引擎里看不到 `transformer.forward`。
- 两种执行模式由 `step_execution`（与 `streaming_output`）决定：`REQUEST_BATCH` 用 `execute_batch` 一次跑完整条 `pipeline.forward`；`STEP_BATCH` 用 `execute_step` 按步推进 `prepare_encode/denoise_step/step_scheduler/post_decode`。
- 批处理能力来自 pipeline 类声明的 `supports_request_batch`；不支持批处理却设 `max_num_seqs>1` 会被 `_resolve_execution_mode` 直接拒绝。
- 统一输出流：`step_streaming` 是公共异步入口，流式与非流式消费同一条 `asyncio.Queue`；后台 busy loop 在普通线程投递，靠 `loop.call_soon_threadsafe` 跨线程安全；STEP_BATCH 会逐步出中间块，REQUEST_BATCH 只在 finished 时投递。
- `OmniDiffusionConfig` 的 `step_execution / streaming_output / max_num_seqs / request_batch_max_wait_ms / engine_backend` 决定执行行为；`DiffusionOutput` 是跨进程输出容器，带错误态、中止态、流式块信息与 `to_cpu` 标志。
- pre/post 处理函数走**注册表模式**：以架构名为 key 查两张映射表，动态 import 模块并返回函数；未注册返回 `None`，架构名同时是批处理能力探测的 key。

## 7. 下一步学习建议

本讲只讲了引擎的「外壳」——执行模式选择与输出流。接下来建议：

- **u5-l2 Diffusion 调度器与执行器**：深入 `BaseScheduler` / `RequestScheduler` / `StepScheduler` 的状态机，以及 `MultiprocDiffusionExecutor` 如何经 ZMQ/SHM 管理 worker 进程，补全 4.3 里被略过的「调度细节」。
- **u5-l3 Diffusion Worker 与模型加载**：进入 worker 进程，看 `execute_batch` / `execute_step` 最终如何调用 `pipeline.forward` 或四步函数、cache_backend 如何 refresh。
- **u5-l4 Diffusion Pipeline 与去噪数据流**：精读 `diffuse` 循环、CFG 双前向与 `scheduler.step`，对应本讲「pipeline 内部路径」那一列。
- 若对加速感兴趣，可跳读 u7 系列（注意力后端、并行注意力、缓存加速、并行策略、批处理），其中 u7-l5 会从更高视角重新审视本讲提到的请求级批处理与步级连续批处理的取舍。
