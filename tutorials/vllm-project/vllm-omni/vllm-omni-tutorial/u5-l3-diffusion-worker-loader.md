# Diffusion Worker 与模型加载

## 1. 本讲目标

在上一篇（u5-l1）里，我们把 `DiffusionEngine` 看成「只编排、不下场」的指挥官——它把真正的前向算子全部委托给多进程执行器 `MultiprocDiffusionExecutor`，再由执行器分发到若干 **worker 子进程**。本讲就钻进这些 worker 子进程，回答三个问题：

1. 一个 diffusion worker 进程是**怎么被拉起来**的？它内部的类是怎么分层的？
2. worker 进程**怎么初始化设备与分布式环境**（NCCL / HCCL、模型并行组）？模型**权重怎么加载**？
3. worker 进程**收到一条 ZMQ 消息后，如何一步步走到 `pipeline.forward`**？中间的 `cache_backend.refresh` 和 `set_forward_context` 各自出现在哪一步？

学完本讲，你应该能够：

- 画出「进程入口 `worker_main` → `WorkerProc` → `WorkerWrapperBase` → `DiffusionWorker` → `DiffusionModelRunner`」这条分层链路，并说清每一层的职责。
- 说清 `init_device` 如何用 `current_omni_platform` 完成跨硬件（CUDA/NPU/…）的设备选择、`init_distributed_environment` 与 `initialize_model_parallel` 的调用顺序。
- 跟踪一条 generation 消息从 `broadcast_mq` 进、到 `result_mq` 出的完整路径，并在路径上准确标注 `cache_backend.refresh` 与 `set_forward_context` 的位置。
- 说清 `DiffusersPipelineLoader` 如何下载 / 校验 / 迭代 safetensors 权重，以及量化与 HSDP 两条特殊加载分支。

本讲是 u5-l4（Diffusion Pipeline 与去噪数据流）的前置——只有先搞懂「权重如何被加载、forward 在哪个上下文里执行」，才能进一步读懂 pipeline 内部的去噪循环。

## 2. 前置知识

阅读本讲前，建议你已经掌握：

- **进程间通信（IPC）与共享内存（SHM）**：vLLM 的 `MessageQueue` 是一个基于共享内存的广播/收集队列，1 个写者可以同时让 N 个读者读到同一条消息。本讲会反复出现两类 `MessageQueue`：执行器→worker 的 `broadcast_mq`（1 写 N 读）与 worker→执行器的 `result_mq`（N 写 1 读）。
- **torch.distributed 与模型并行**：多 GPU 推理需要先用 `init_distributed_environment` 建立通信域（后端通常是 NCCL，华为 NPU 上是 HCCL），再用 `initialize_model_parallel` 把全局 rank 划分成若干正交的并行组（TP/SP/CFG/DP/PP 等）。本讲只关心 worker 进程如何「加入」这些组，具体并行策略的细节留到 u7-l4。
- **monkey-patch / 平台抽象**：u2-l1 讲过 vLLM-Omni 用 `current_omni_platform` 屏蔽 CUDA/ROCm/NPU/XPU 的差异。本讲会大量看到 `current_omni_platform.get_torch_device()`、`current_omni_platform.synchronize()` 这类调用。
- **u5-l1 的两种执行模式**：`REQUEST_BATCH`（一次 `execute_batch` 跑完整条 `pipeline.forward`）与 `STEP_BATCH`（`execute_step` 按去噪步推进）。本讲的 worker 代码同时为这两种模式服务。

> 名词速查
> - **worker 进程**：一个独立的 OS 进程，绑定一张 GPU，负责在该 GPU 上执行 diffusion 前向。
> - **EngineCore / Executor**：留在主进程里的编排者，负责调度与 IPC，不直接碰 GPU 算子。
> - **Runner**：进程内真正持有模型权重、调用 `pipeline.forward` 的对象。
> - **forward context**：一个进程级全局上下文，存放当前前向所需的 vLLM 配置、注意力元数据等，供底层算子在执行时读取。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [vllm_omni/diffusion/worker/diffusion_worker.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/worker/diffusion_worker.py) | worker 进程的全部实体：进程入口 `worker_main`、IPC 包装 `WorkerProc`、扩展包装 `WorkerWrapperBase`、基础设施类 `DiffusionWorker`。 |
| [vllm_omni/diffusion/worker/diffusion_model_runner.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/worker/diffusion_model_runner.py) | 模型运行器 `DiffusionModelRunner`：负责模型加载、编译、缓存、offload 与前向执行。 |
| [vllm_omni/diffusion/model_loader/diffusers_loader.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/model_loader/diffusers_loader.py) | 权重加载器 `DiffusersPipelineLoader`：下载、校验、迭代 safetensors 权重并实例化 pipeline。 |
| [vllm_omni/diffusion/forward_context.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/forward_context.py) | `ForwardContext` 数据类与 `set_forward_context` 上下文管理器（进程级全局）。 |
| [vllm_omni/diffusion/cache/selector.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/selector.py) / [cache/base.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/base.py) | 缓存后端选择器 `get_cache_backend` 与抽象基类 `CacheBackend`（`enable/refresh/is_enabled` 契约）。 |
| [vllm_omni/diffusion/executor/multiproc_executor.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/executor/multiproc_executor.py) | 执行器侧：`_launch_workers` 拉起 worker 进程、`collective_rpc` 向 worker 发 RPC。 |

## 4. 核心概念与源码讲解

### 4.1 Worker 进程的分层架构与启动生命周期

#### 4.1.1 概念说明

一个 diffusion worker 进程里其实**叠了四层对象**，从外到内分别是：

1. **进程入口 `worker_main`（静态方法）**：信号处理、死亡信号、插件加载、创建 `WorkerProc`、握手、进入 busy loop。
2. **`WorkerProc`**：进程的「IPC 大脑」。它持有 ZMQ 上下文、广播队列 `mq`（从执行器收命令）、结果队列 `result_mq`（向执行器回结果），以及一个被包装的 worker。
3. **`WorkerWrapperBase`**：一层「动态继承」包装，支持用 `worker_extension_cls` 给 worker 类临时「贴」上额外方法（例如 `CustomPipelineWorkerExtension`），并通过 `__getattr__` 把属性访问透明地转发给内层真正的 worker。
4. **`DiffusionWorker`**：基础设施类，只管设备、分布式环境、显存 sleep/wake，**所有模型相关操作都委托给 `DiffusionModelRunner`**。

这套分层的关键设计哲学写在 `DiffusionWorker` 的类文档里：

> This class handles infrastructure initialization only … All model-related operations (loading, compilation, execution) are delegated to DiffusionModelRunner.

也就是说，**Worker 管基础设施，Runner 管模型**。这与 AR 模块（u4-l1）的 Worker/Runner 分工完全一致，是一种刻意对齐的代码组织。

#### 4.1.2 核心流程

一个 worker 进程从被 `spawn` 到「就绪（ready）」的生命周期：

```text
MultiprocDiffusionExecutor._launch_workers        # 主进程
  └─ mp.Process(target=WorkerProc.worker_main)    # spawn 一个新进程
       │
       ▼  （以下都在 worker 子进程内）
WorkerProc.worker_main(rank, od_config, pipe_writer, broadcast_handle, ...)
  ├─ load_omni_general_plugins()                  # 加载 general plugins
  ├─ WorkerProc(od_config, gpu_id=rank, broadcast_handle, ...)
  │     ├─ MessageQueue.create_from_handle(...)   # 接上执行器的广播队列 mq
  │     ├─ MessageQueue(n_reader=1, ...)          # 自建结果队列 result_mq
  │     └─ self._create_worker(...)
  │           ├─ current_omni_platform.get_diffusion_worker_cls()  # 跨硬件选类
  │           └─ WorkerWrapperBase(...)            # 包装层
  │                 ├─ self._prepare_worker_class()  # 按需动态继承 extension
  │                 └─ DiffusionWorker(...)          # ← 这里触发 init_device + load_model
  │                       ├─ self.init_device()
  │                       ├─ 创建 DiffusionModelRunner
  │                       └─ self.load_model(...)   # 委托给 runner
  ├─ pipe_writer.send({"status": "ready", "result_handle": ...})  # 握手回执
  └─ worker_proc._worker_busy_loop()              # 进入主循环
```

注意第 4 步：`DiffusionWorker.__init__` 在构造阶段就会**同步完成 `init_device` 与 `load_model`**。所以当 `pipe_writer.send({"status": "ready"})` 发出时，模型权重已经躺在 GPU 上了——这就是「ready」的真正含义。

#### 4.1.3 源码精读

进程入口 `worker_main` 是一个静态方法，负责把一个普通 Python 进程「打扮」成 diffusion worker：注册 SIGTERM/SIGINT 处理、设置死亡信号（父进程死时把自己也杀掉）、加载插件，然后创建 `WorkerProc` 并进入 busy loop：

```python
# diffusion_worker.py:1164-1192（节选）
set_death_signal(signal.SIGTERM)
load_omni_general_plugins()
worker_proc = WorkerProc(
    od_config, gpu_id=rank, broadcast_handle=broadcast_handle,
    wake_event=wake_event, worker_extension_cls=worker_extension_cls,
    custom_pipeline_args=custom_pipeline_args,
)
pipe_writer.send({"status": "ready", "result_handle": worker_proc.result_mq_handle})
worker_proc._worker_busy_loop()
```

`WorkerProc._create_worker` 决定「用哪个 worker 类」——它问 `current_omni_platform` 要一个限定的类名（CUDA 和 NPU 可能返回不同的类），再用 `WorkerWrapperBase` 把它包起来：

```python
# diffusion_worker.py:842-859（节选）
worker_cls_path = current_omni_platform.get_diffusion_worker_cls()
base_worker_class = resolve_obj_by_qualname(worker_cls_path)
wrapper = WorkerWrapperBase(
    gpu_id=gpu_id, od_config=od_config,
    worker_extension_cls=worker_extension_cls,
    custom_pipeline_args=custom_pipeline_args,
    base_worker_class=base_worker_class,
)
```

`WorkerWrapperBase._prepare_worker_class` 是「动态继承」的魔法所在：如果传了 `worker_extension_cls`，它会用 `type(...)` 在运行时合成一个新类，让 extension 的方法贴到 worker 上，并发出冲突警告：

```python
# diffusion_worker.py:1296-1305（节选）
class_name = f"{worker_class.__name__}With{worker_extension_cls.__name__}"
worker_class = type(class_name, (worker_extension_cls, worker_class), {})
```

最后，`DiffusionWorker.__init__` 是「构造即就绪」的总入口。它先 `init_device()`，再按「用户覆盖 → 引擎声明 → 平台默认」三级优先级选出 `DiffusionModelRunner` 的具体子类，最后 `load_model`：

```python
# diffusion_worker.py:221-257（节选）
self.init_device()
# 1. 用户显式覆盖  2. 引擎类声明的 runner  3. 平台默认
runner_override = getattr(self.od_config, "diffusion_model_runner_cls", None)
...
model_runner_cls = resolve_obj_by_qualname(model_runner_cls_path)
self.model_runner = model_runner_cls(vllm_config=..., od_config=..., device=self.device)
self.profiler = self._create_profiler()
if not skip_load_model:
    self.load_model(load_format=self.od_config.diffusion_load_format)
    self.init_lora_manager()
```

> 这个三级优先级（`diffusion_worker.py:231-248`）值得留意：例如 `ARDiffusionEngine` 会声明 `default_diffusion_model_runner_cls = ...ARDiffusionModelRunner`，于是同一份 worker 代码可以服务于不同的实验引擎（见 u9-l4）。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认「ready 握手 = 模型已加载」这一结论。

**步骤**：

1. 打开 [multiproc_executor.py:268-313](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/executor/multiproc_executor.py#L268-L313)，看执行器如何为每张 GPU `spawn` 一个进程，并等待 `reader.recv()` 回送的 `{"status": "ready"}`。
2. 打开 [diffusion_worker.py:1176-1192](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/worker/diffusion_worker.py#L1176-L1192)，确认 `pipe_writer.send` 在 `_worker_busy_loop()` **之前**。
3. 沿着 `WorkerProc(...)` → `_create_worker` → `WorkerWrapperBase(...)` → `DiffusionWorker(__init__)` 追到 `self.load_model(...)`（`diffusion_worker.py:255-257`）。

**需要观察的现象**：`load_model` 出现在握手 send 之前，因此执行器收到 ready 时，权重一定已经就位。

**预期结果**：你能用一句话回答「执行器收到 ready 回执意味着什么？」——「意味着该 rank 对应的 GPU 上 pipeline 已加载完毕、分布式环境已初始化、可以接收前向请求」。

#### 4.1.5 小练习与答案

**练习 1**：如果用户同时在配置里给了 `diffusion_model_runner_cls`，又用一个引擎（如 `ARDiffusionEngine`）声明了 `default_diffusion_model_runner_cls`，最终用哪个？

**参考答案**：用用户的显式覆盖。代码（`diffusion_worker.py:231-247`）先检查 `runner_override`，只要它是非空字符串就用它；否则才看 `engine_runner`；两者都空才回落到 `current_omni_platform.get_diffusion_model_runner_cls()`。

**练习 2**：`WorkerWrapperBase.__getattr__`（`diffusion_worker.py:1402-1404`）的作用是什么？为什么 RPC 调用 `execute_method` 能调用到 `DiffusionWorker` 上未显式转发的方法？

**参考答案**：`__getattr__` 只在常规属性查找失败时触发，它把访问透明转发给 `self.worker`（内层 `DiffusionWorker`）。因此 `execute_method` 里 `getattr(self.worker, method)`（`diffusion_worker.py:1394`）能拿到任意 worker 方法（如 `sleep`、`add_lora`），无需在 wrapper 里逐一声明。

---

### 4.2 init_device：设备与分布式环境初始化

#### 4.2.1 概念说明

`init_device` 是 worker 进程的「开机自检」：它要在一个空白进程里把 GPU、torch.distributed 通信域、模型并行组全部建立起来。这一步对多 GPU 推理至关重要——没有正确的并行组，后续 `pipeline.forward` 里的集合通信（AllReduce、AllGather、all-to-all）就会卡死或报错。

这里有一个跨硬件的关键设计：vLLM-Omni 不直接写 `torch.cuda.set_device(...)`，而是统一走 `current_omni_platform`。这样同一份 worker 代码可以在 CUDA（NCCL 后端）和 NPU（HCCL 后端）上运行，差异被吸收在平台类里。

#### 4.2.2 核心流程

`init_device` 分四步：

1. **设环境变量**：`MASTER_ADDR/MASTER_PORT/LOCAL_RANK/RANK/WORLD_SIZE`，为 `torch.distributed` 的 rendezvous 做准备。
2. **选设备 + 构造 worker-local `VllmConfig`**：通过平台拿到 `torch.device`，再手工拼一个只含并行/量化/模型配置的精简 `VllmConfig`（因为 diffusion worker 并不运行完整 vLLM，只复用它的部分机制）。
3. **`init_distributed_environment`**：建立 torch.distributed 通信域（NCCL/HCCL）。
4. **`initialize_model_parallel`**：用 `RankGenerator` 把全局 rank 切成 TP/SP/CFG/DP/PP 等正交并行组，最后 `init_workspace_manager`。

后三步都被包在同一个 `set_forward_context(...) + set_current_vllm_config(...)` 双重上下文里——这是因为初始化期间可能有 GPU 操作需要读取全局配置。

#### 4.2.3 源码精读

环境变量与设备选择（注意 `MASTER_PORT` 来自 `od_config.master_port`，它在 `OmniDiffusionConfig.__post_init__` 里已由 `_resolve_master_port` 解析，优先级是 `MASTER_PORT` 环境变量 → 显式 `master_port` → OS 分配的临时端口）：

```python
# diffusion_worker.py:266-274（节选）
os.environ["MASTER_ADDR"] = "localhost"
os.environ["MASTER_PORT"] = str(self.od_config.master_port)
os.environ["WORLD_SIZE"] = str(world_size)
self.device = current_omni_platform.get_torch_device(rank)
current_omni_platform.set_device(self.device)
```

接着手工组装 worker-local `VllmConfig`，把 `od_config.parallel_config` 里的各并行度搬到 vLLM 的 `parallel_config` 上。注意 MoE 专家并行的特殊重映射（把 DP×CFG 折叠进 vLLM 的 `data_parallel_size`）：

```python
# diffusion_worker.py:278-296（节选）
vllm_config = _create_diffusion_worker_vllm_config(self.device, self.od_config)
parallel_config = self.od_config.parallel_config
vllm_config.parallel_config.tensor_parallel_size = parallel_config.tensor_parallel_size
vllm_config.parallel_config.data_parallel_size = parallel_config.data_parallel_size
if parallel_config.enable_expert_parallel and self.od_config.is_moe:
    vllm_config.parallel_config.data_parallel_size = (
        parallel_config.data_parallel_size * parallel_config.cfg_parallel_size)
...
vllm_config.kernel_config.ir_op_priority = _resolve_ir_op_priority(self.od_config, vllm_config)
```

> 这里的 `_make_diffusion_vllm_model_config`（`diffusion_worker.py:95-109`）构造的是一个**精简的占位 `_DiffusionVllmModelConfig`**，而不是真正的 vLLM `ModelConfig`——因为 diffusion 模型不走 vLLM 的文本模型路径，只需要 dtype/quantization 等少量字段被下游算子读到。

分布式环境与模型并行组的建立，整个包在双重上下文里。`initialize_model_parallel` 接收 TP/SP/CFG/DP/PP/Ulysses/Ring/HSDP 全套并行度参数：

```python
# diffusion_worker.py:310-331（节选）
with (set_forward_context(vllm_config=self.vllm_config, omni_diffusion_config=self.od_config),
      set_current_vllm_config(self.vllm_config)):
    init_distributed_environment(world_size=world_size, rank=rank)
    initialize_model_parallel(
        data_parallel_size=parallel_config.data_parallel_size,
        cfg_parallel_size=parallel_config.cfg_parallel_size,
        sequence_parallel_size=parallel_config.sequence_parallel_size,
        ulysses_degree=parallel_config.ulysses_degree,
        ring_degree=parallel_config.ring_degree,
        tensor_parallel_size=parallel_config.tensor_parallel_size,
        pipeline_parallel_size=parallel_config.pipeline_parallel_size,
        fully_shard_degree=parallel_config.hsdp_shard_size if parallel_config.use_hsdp else 1,
        ...
    )
    init_workspace_manager(self.device)
```

`num_gpus` 与 `world_size` 的关系在 `OmniDiffusionConfig.__post_init__` 里被强制：若未显式指定，`num_gpus` 取 `parallel_config.world_size`，且 `num_gpus < world_size` 会直接抛错（`data.py:941-950`）。这保证「spawn 的进程数」与「并行组需要的 rank 数」永远一致。

#### 4.2.4 代码实践（源码阅读型）

**目标**：理解「为什么 `MASTER_PORT` 不能写死」。

**步骤**：

1. 读 [data.py:855-872](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/data.py#L855-L872) 的 `_resolve_master_port`，注意三级优先级。
2. 读 `settle_port`（`data.py:874-907`）的端口冲突重试逻辑（`port_inc=37`、冲突时递增、60000 以上回绕）。
3. 联系 u3-l5 的多副本场景：当 Orchestrator 启动多个 stage 副本时，每个副本的 worker 进程组都需要独立的 rendezvous 端口。

**需要观察的现象**：若两个副本用了同一个端口，第二个进程组会在 `init_distributed_environment` 处卡住或失败。

**预期结果**：你能解释「分布式多副本部署时，为什么 orchestrator 要为每个副本传不同的 `master_port`」——因为 `torch.distributed` 的 store rendezvous 依赖端口隔离，`_resolve_master_port` 通过环境变量 / 显式参数 / 临时端口三级来避免冲突。

#### 4.2.5 小练习与答案

**练习 1**：`init_device` 为什么要把 `init_distributed_environment` 和 `initialize_model_parallel` 放进 `set_forward_context(...)` 上下文里？这一步还没有模型，forward context 有什么用？

**参考答案**：`set_forward_context`（见 4.4.3）不只是为「模型 forward」服务——它把 `vllm_config` 与 `omni_diffusion_config` 注入进程级全局，并同时挂上 `set_current_vllm_config` 与 IR op 优先级。分布式初始化与并行组建组过程中可能触发平台相关的 GPU 操作或 IR 包装，这些操作需要读到正确的全局配置，因此必须置于该上下文内。

**练习 2**：MoE 模型且开启专家并行时，vLLM 的 `data_parallel_size` 被改写成了什么？为什么？

**参考答案**：被改写成 `data_parallel_size * cfg_parallel_size`（`diffusion_worker.py:286-288`）。因为 diffusion 通常用自己的 DP/CFG/SP 组，只有当 MoE 运行时要消费 vLLM 的 FusedMoE/EP 语义时，才把 CFG 维折叠进 DP，让 vLLM 看到一个合并后的「数据并行」维度。

---

### 4.3 busy_loop 与消息路由：从 ZMQ 收消息到执行

#### 4.3.1 概念说明

worker 进程就绪后，整个生命周期都在 `_worker_busy_loop` 里循环：从广播队列 `mq` 取一条消息，按类型路由，执行，再把结果塞回 `result_mq`。这是 worker 的「主循环」，也是执行器与 worker 之间唯一的运行期交互通道。

消息类型有五类，前四类是**控制类**，最后是**直接生成类**：

| 消息 `type` | 处理方式 |
| --- | --- |
| `"sleep"` | `handle_sleep_task` → 把权重卸载到 CPU（显存让出） |
| `"wake_up"` | `handle_wake_task` → 重新加载权重 |
| `"rpc"` | `_execute_rpc` → 在 worker 上调用任意方法（`execute_model`、`add_lora` 等） |
| `"shutdown"` | 退出主循环 |
| 其它（直接是 request） | `worker.execute_model(msg, od_config)` → 直接前向 |

绝大多数前向请求走 **`"rpc"`** 路径——因为执行器的 `collective_rpc` 会把方法名包装成 `{"type": "rpc", "method": "execute_model", ...}` 再广播。

#### 4.3.2 核心流程

一条 `execute_model` RPC 的往返：

```text
执行器 collective_rpc("execute_model", args=(req, od_config, kv_prefetch_job))
  └─ broadcast_mq.enqueue({"type":"rpc", "method":"execute_model", "rpc_id":..., ...})
        │ （共享内存广播，所有 worker rank 都收到）
        ▼
worker _worker_busy_loop
  └─ msg = self.mq.dequeue(timeout=1.0)
  └─ 分支：msg["type"]=="rpc" → _execute_rpc(msg)
        └─ result, should_reply = self._execute_rpc(msg)
              └─ self.worker.execute_method("execute_model", req, od_config, kv_prefetch_job)
                    └─ WorkerWrapperBase.execute_method → getattr(self.worker, "execute_model")
                          └─ DiffusionWorker.execute_model(req, od_config, kv_prefetch_job)
                                └─ self.model_runner.execute_model(req, kv_prefetch_job=...)   # ← 4.4 详讲
        └─ if should_reply: self._return_result(result, rpc_id=rpc_id)
              └─ 异步路径：enqueue COMPUTE_DONE；后台线程做 D2H + SHM；再 enqueue OUTPUT_READY
              └─ 同步路径：pack_diffusion_output_shm(output) → result_mq.enqueue(output)
        │
        ▼
执行器 result_pump / _dequeue_one_with_failure_polling 从 result_mq 取回结果
```

异步输出（async output）是性能优化的关键：在 request 模式（`step_execution=False`）下，worker 一算完就立刻回一个轻量的 `COMPUTE_DONE`（只带 `async_output_id`），把昂贵的「GPU→CPU 拷贝 + 共享内存打包」丢给后台线程在 side stream 上做，默认流立刻能开始下一次前向。这就是 `_async_output_loop` 存在的理由。

#### 4.3.3 源码精读

主循环的骨架——`dequeue` 带超时，超时时检查是否有 OOB「唤醒」事件（sleep 后用特殊方式唤醒）：

```python
# diffusion_worker.py:1033-1058（节选）
while self._running:
    msg = None
    try:
        msg = self.mq.dequeue(timeout=1.0)
    except Exception:
        if self.wake_event and self.wake_event.is_set():
            self.wake_event.clear()
            msg = {"type": "wake_up", "task_id": "recovery-task", "tags": None}
        else:
            continue
    ...
    if isinstance(msg, dict) and msg.get("type") == "sleep":
        ack = self.worker.handle_sleep_task(task); self._return_result(ack)
    elif isinstance(msg, dict) and msg.get("type") == "wake_up":
        ack = self.worker.handle_wake_task(task); self._return_result(ack)
```

RPC 路由分支调用 `_execute_rpc`，它内部用 `execute_method` 解析方法名并执行，并按 `output_rank` / `exec_all_ranks` 决定「哪些 rank 要回结果」：

```python
# diffusion_worker.py:1060-1065 与 990-992（节选）
elif isinstance(msg, dict) and msg.get("type") == "rpc":
    rpc_id = msg.get("rpc_id")
    result, should_reply = self._execute_rpc(msg)
    if should_reply:
        self._return_result(result, rpc_id=rpc_id)
...
result = self.worker.execute_method(method, *args, **kwargs)
```

`should_reply` 的判定（`diffusion_worker.py:953-974`）很精细：在 DP 多并发场景下，只有每个 DP 副本内的「主 rank」（SP/TP/CFG/PP rank 全为 0）才回复，避免 SP/TP 等非主 rank 往 `result_mq` 塞多余响应把执行器搞乱。

`_return_result` 区分三条回传路径——`OmniACK`（sleep/wake 回执）、异步 `DiffusionOutput`（先 COMPUTE_DONE 后 OUTPUT_READY）、同步兜底（直接 SHM 打包）：

```python
# diffusion_worker.py:861-888（节选）
def _return_result(self, output, rpc_id=None):
    if isinstance(output, OmniACK):
        self.result_mq.enqueue(output); return
    if not self.od_config.step_execution and isinstance(output, (DiffusionOutput, BatchRunnerOutput)):
        async_output_id = WorkerProc._generate_async_output_id()
        gpu_event = current_omni_platform.record_device_event()
        self._async_output_queue.put((output, async_output_id, gpu_event))
        self.result_mq.enqueue(AsyncDiffusionOutput(
            kind=AsyncOutputKind.COMPUTE_DONE, rpc_id=rpc_id, async_output_id=async_output_id))
        return
    pack_diffusion_output_shm(output)
    self.result_mq.enqueue(output)
```

异步后台线程用独立的 `d2h_stream`，并通过 `wait_event(gpu_event)` 等待默认流写完输出张量后再读，保证跨流顺序正确：

```python
# diffusion_worker.py:898-914（节选）
d2h_stream = torch.Stream(device=device)
output, async_output_id, gpu_event = self._async_output_queue.get()
if gpu_event is not None:
    d2h_stream.wait_event(gpu_event)
pack_diffusion_output_shm(output, d2h_stream=d2h_stream)
d2h_stream.synchronize()
self.result_mq.enqueue(AsyncDiffusionOutput(
    kind=AsyncOutputKind.OUTPUT_READY, async_output_id=async_output_id, output=output))
```

> 直接生成分支（`diffusion_worker.py:1121-1136`）处理「裸 request」消息（非 RPC 包装），直接 `self.worker.execute_model(msg, self.od_config)`。它与 RPC 分支最终都汇合到同一个 `DiffusionWorker.execute_model`。

#### 4.3.4 代码实践（源码阅读型）

**目标**：搞清「一条 `execute_model` RPC，哪些 rank 会回复」。

**步骤**：

1. 打开执行器侧 [multiproc_executor.py:571-678](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/executor/multiproc_executor.py#L571-L678) 的 `collective_rpc`，注意普通前向调用时 `unique_reply_rank=0, exec_all_ranks=True`，意味着所有 rank 都执行，但只有 rank 0 回复。
2. 对比 worker 侧 [diffusion_worker.py:953-974](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/worker/diffusion_worker.py#L953-L974)，确认 worker 的 `should_reply` 判定与执行器期望的回复数一致。
3. 再看 DP 多并发特例（`multiproc_executor.py:631-660`）：`unique_reply_rank=None, exec_all_ranks=True` 时执行器会收集 `dp_size` 个回复并按 `dp_rank` 排序。

**需要观察的现象**：如果某个 SP/TP 非主 rank 误发了回复，执行器的 `_validate_wave_id` / 响应计数就会错乱。

**预期结果**：你能填出下表——

| 场景 | exec_all_ranks | 回复的 rank |
| --- | --- | --- |
| 普通前向（TP>1） | True | 仅 rank 0 |
| DP 多并发 | True | 每个 DP 副本的主 rank（共 dp_size 个） |

#### 4.3.5 小练习与答案

**练习 1**：为什么 request 模式下要把「计算完成」与「输出就绪」拆成两条消息？

**参考答案**：为了让昂贵的 D2H 拷贝与 SHM 打包在后台 side stream 上与下一次前向重叠（`diffusion_worker.py:890-926`）。`COMPUTE_DONE` 立刻返回，默认流马上能开始下一个 `pipeline.forward`；真正的输出张量稍后由 `OUTPUT_READY` 投递，执行器在 `step_streaming` 里等它。

**练习 2**：`_async_output_loop` 里的 `d2h_stream.wait_event(gpu_event)` 如果删掉会怎样？

**参考答案**：side stream 可能读到默认流还没写完的输出张量（跨流无序），导致 D2H 拷贝出读到半成品数据或形状错乱。`gpu_event` 由 `record_device_event()` 在默认流上记录，`wait_event` 强制 side stream 等到默认流那一点完成，保证读时序正确。

---

### 4.4 DiffusionModelRunner：模型加载、cache 刷新与 forward 执行

#### 4.4.1 概念说明

`DiffusionModelRunner` 是 worker 进程里「真正持有模型」的对象。它的职责可以用一句话概括：**加载 pipeline、给它装配各种加速外挂（offload / compile / cache / prompt-embed cache），然后在正确的上下文里调用 `pipeline.forward`**。

这里有几个关键概念：

- **pipeline**：一个 diffusion 模型对象（如 `ZImagePipeline`、`QwenImagePipeline`），内部含 transformer、VAE、text encoder 等组件。`self.pipeline` 就是它。
- **cache_backend**：缓存加速后端（TeaCache / Cache-DiT / MagCache / StepCache），通过统一的 `enable / refresh / is_enabled` 契约接入。它的作用是「跳过部分 transformer 层或步」来加速去噪，详见 u7-l3。
- **forward context**：一个进程级全局（`_forward_context`），存放当前前向所需的 vLLM 配置、注意力元数据、序列并行状态等。底层 attention 层在执行时会通过 `get_forward_context()` 读它。`set_forward_context(...)` 是把数据「放进」这个全局的上下文管理器。

#### 4.4.2 核心流程

模型加载（`load_model`）顺序：

```text
load_model
  ├─ 平台 runtime 初始化 (init_diffusion_model_runner_runtime)
  ├─ 决定加载设备 (cpu / gpu，受 cpu_offload / layerwise_offload 影响)
  ├─ DiffusersPipelineLoader(...).load_model(...)            # ← 4.5 详讲，产出 self.pipeline
  ├─ get_offload_backend(...).enable(pipeline)               # CPU/层间 offload
  ├─ torch.compile (enforce_eager=False 时，regionally_compile 或全量 compile)
  ├─ get_cache_backend(...).enable(pipeline)                 # 缓存加速挂载
  └─ install_prompt_embed_cache(pipeline)                    # 文本编码缓存（可选）
```

单请求前向（`execute_model` → `_execute_request_list`）顺序：

```text
execute_model(req)
  └─ _execute_request_list([req], allow_single_output=True, ...)
        ├─ _prepare_request_for_forward(req)
        │     ├─ KV 接收 (receive_multi_kv_cache_distributed 或 consume_and_distribute)
        │     ├─ KV 预取 (start_prefetch，与本次 forward 重叠)
        │     └─ _initialize_generator (按 seed 建 torch.Generator)
        ├─ _refresh_cache_for_requests(reqs)                 # ← cache_backend.refresh 在这里
        ├─ reset_peak_memory_stats (主 rank)
        ├─ with set_forward_context(...):                    # ← forward context 在这里
        │     raw_outputs = self.pipeline.forward(batch)     # ← 真正的去噪前向
        ├─ _normalize_pipeline_outputs (校验返回结构)
        └─ 采样峰值显存、记录 prompt-embed cache 统计
```

注意三个关键点的**相对顺序**：先 KV 接收/预取 → 再 cache refresh → 最后进 `set_forward_context` 跑 `pipeline.forward`。`cache_backend.refresh` 必须在 forward **之前**调用（它要按本次的 `num_inference_steps` 重置缓存状态）；`set_forward_context` 必须包住 forward（底层 attention 要读它）。

#### 4.4.3 源码精读

`load_model` 用 `DeviceMemoryProfiler` 量化加载耗时与显存，并把加载放进 `_maybe_get_memory_pool_context`（sleep 模式下用 `CuMemAllocator` 标记 weights 池）：

```python
# diffusion_model_runner.py:228-247（节选）
load_config = LoadConfig()
model_loader = DiffusersPipelineLoader(load_config, od_config=self.od_config)
with get_memory_context():
    with DeviceMemoryProfiler() as m:
        self.pipeline = model_loader.load_model(
            load_device=load_device, load_format=load_format,
            custom_pipeline_name=custom_pipeline_name, device=self.device)
logger.info("Model loading took %.4f GiB and %.6f seconds",
            m.consumed_memory / GiB_bytes, time_after_load - time_before_load)
```

缓存后端的选择与挂载——`get_cache_backend` 按名字路由到具体后端实例，再 `.enable(pipeline)` 把策略注入 transformer；对声明不兼容缓存的模型（`_NO_CACHE_ACCELERATION`）会强制关闭：

```python
# diffusion_model_runner.py:292-304（节选）
self.cache_backend = get_cache_backend(self.od_config.cache_backend, self.od_config.cache_config)
if self.cache_backend is not None:
    if self.od_config.model_class_name in _NO_CACHE_ACCELERATION:
        self.cache_backend = None
    else:
        self.cache_backend.enable(self.pipeline)
```

`get_cache_backend`（[cache/selector.py:11-49](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/selector.py#L11-L49)）是个纯查表函数：`"tea_cache"→TeaCacheBackend`、`"cache_dit"→CacheDiTBackend`、`"mag_cache"→MagCacheBackend`、`"step_cache"→StepCacheBackend`，`None/"none"→None`。所有后端都继承自 `CacheBackend`，实现统一的 `enable(pipeline)` / `refresh(pipeline, num_inference_steps)` / `is_enabled()` 三件套（[cache/base.py:61-101](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/base.py#L61-L101)）。

`_refresh_cache_for_requests` 是前向前**刷新缓存**的入口——它取批次里第一个请求的 `num_inference_steps`（批准入按 `RequestBatchSamplingParamsKey` 分组，所以组内一致），调 `cache_backend.refresh`：

```python
# diffusion_model_runner.py:411-443（节选）
def _refresh_cache_for_requests(self, reqs, *, od_config):
    if self.cache_backend is None or not self.cache_backend.is_enabled():
        return
    num_inference_steps = reqs[0].sampling_params.num_inference_steps
    ...
    if num_inference_steps is not None:
        self.cache_backend.refresh(self.pipeline, num_inference_steps)
```

真正的前向发生在 `_execute_request_list` 里，被 `set_forward_context` 包住——这是本讲最关键的一行：

```python
# diffusion_model_runner.py:496-511（节选）
self._refresh_cache_for_requests(reqs, od_config=od_config)   # ① 先刷新缓存
batch = DiffusionRequestBatch(requests=reqs)
...
with set_forward_context(vllm_config=self.vllm_config, omni_diffusion_config=od_config):  # ② 进 forward 上下文
    with record_function(record_name):
        raw_outputs = self.pipeline.forward(batch)            # ③ 真正的去噪前向
        outputs = _normalize_pipeline_outputs(raw_outputs, expected_count=len(reqs), ...)
```

`set_forward_context`（[forward_context.py:175-209](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/forward_context.py#L175-L209)）做了两件事：把一个新 `ForwardContext` 塞进进程级全局 `_forward_context`；同时挂上 `set_current_vllm_config` 与 IR op 优先级，让 vLLM 的 CustomOp（如 `QKVParallelLinear`）和 IR 算子（如 RMSNorm）能在 diffusion 模型里正确派发：

```python
# forward_context.py:197-209（节选）
with override_forward_context(forward_context):
    if vllm_config is None:
        yield
    else:
        with (set_current_vllm_config(vllm_config),
              vllm_config.kernel_config.ir_op_priority.set_priority(),
              vllm.ir.enable_torch_wrap(vllm_config.compilation_config.ir_enable_torch_wrap)):
            yield
```

> 这解释了为什么 `init_device` 和 `load_model` 也要套 `set_forward_context`：底层算子在任何 GPU 操作（建组、加载、前向）时都可能读取这个全局上下文。`ForwardContext` 的字段（`forward_context.py:20-72`）还包括序列并行 padding 信息、当前去噪步索引 `denoise_step_idx`、参考 latent 等，供 attention 层按需读取。

`DiffusionWorker.execute_model` 只是 Runner 的薄封装：先按 DP rank 选请求、激活 LoRA、套 profiler 上下文，再委托给 runner：

```python
# diffusion_worker.py:440-469（节选）
is_batch = isinstance(req, list)
if is_batch:
    dp_rank = get_data_parallel_rank(); req = req[dp_rank % len(req)]
if self.lora_manager is not None:
    self.lora_manager.set_active_adapter(req.sampling_params.lora_request, req.sampling_params.lora_scale)
with ctx:
    output = self.model_runner.execute_model(req, kv_prefetch_job=kv_prefetch_job)
```

#### 4.4.4 代码实践（源码阅读型，对应总体实践任务的核心段）

**目标**：在源码上定位 `cache_backend.refresh` 与 `set_forward_context` 相对 `pipeline.forward` 的位置。

**步骤**：

1. 打开 [diffusion_model_runner.py:462-531](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/worker/diffusion_model_runner.py#L462-L531) 的 `_execute_request_list`。
2. 在 `_refresh_cache_for_requests(reqs, ...)`（第 496 行）处标「① 缓存刷新」。
3. 在 `with set_forward_context(...)`（第 503 行）处标「② forward 上下文开始」。
4. 在 `raw_outputs = self.pipeline.forward(batch)`（第 505 行）处标「③ 真正去噪前向」。

**需要观察的现象**：三者的缩进与顺序——`refresh` 在 `set_forward_context` **之外**且**之前**；`pipeline.forward` 在 `set_forward_context` **之内**。

**预期结果**：你能回答「为什么 refresh 不能放进 set_forward_context 里？」——`refresh` 是为本次生成重置缓存状态（注册/清理 hook、重置计数器），属于「准备」阶段；而 `set_forward_context` 是为了让 transformer 内部的 attention 算子在执行时读到正确的元数据，属于「执行」阶段。两者职责不同，自然分开。如果反过来在上下文里 refresh，倒也不会立刻出错，但语义上把「准备」和「执行」混在一层会增加耦合。

#### 4.4.5 小练习与答案

**练习 1**：`execute_model_batch`（`diffusion_model_runner.py:576-594`）与 `execute_model` 有什么区别？它们分别对应 u5-l1 的哪种执行模式？

**参考答案**：`execute_model` 处理单条请求（`allow_single_output=True`，允许 pipeline 只返回单个 `DiffusionOutput`），对应把每个请求当独立逻辑请求的串行路径；`execute_model_batch` 把多条兼容请求拼成 `DiffusionRequestBatch` 一次喂给 `pipeline.forward(batch)`（`require_request_batch_support=True`，要求 pipeline 声明 `supports_request_batch`），对应 request-level batching（u7-l5）。两者最终都走 `_execute_request_list`。

**练习 2**：步模式（`execute_stepwise`）下的前向与 request 模式有何不同？

**参考答案**：步模式不调 `pipeline.forward`，而是按去噪步推进：`prepare_encode` → `denoise_step` → `step_scheduler` → `post_decode`（`diffusion_model_runner.py:691-819`），每一步都跨多次 `_worker_busy_loop` 调用，用 `state_cache` 保存每请求的跨步状态。它同样用 `set_forward_context` 包住 `denoise_step`，但目前**不支持** `cache_backend`（`execute_stepwise` 开头显式校验 `cache_backend in (None, "none")`）。

---

### 4.5 DiffusersPipelineLoader：权重下载与加载

#### 4.5.1 概念说明

`DiffusersPipelineLoader` 是 `load_model` 链条的最底端——它负责「把磁盘/HuggingFace 上的权重变成一个躺在 GPU 上的 pipeline 对象」。它的核心难点不是数学，而是工程：

- **权重在哪**：本地路径 or HuggingFace 仓库（可能还要走 ModelScope 镜像）。
- **权重格式**：safetensors（分片或不分片）、`.bin`、`.pt`、GGUF。
- **分片索引**：大模型权重常被切成多个 shard，需要读 `*.index.json` 知道每个参数在哪片。
- **量化**：量化权重可能需要在线/离线处理，meta device 上的「懒加载」参数需要在加载后 materialize。
- **HSDP**：权重先加载到 CPU，再按 HSDP 切分到各 GPU。

#### 4.5.2 核心流程

`load_model`（加载器侧，与 runner 的 `load_model` 同名但不同物）的决策树：

```text
DiffusersPipelineLoader.load_model(load_device, load_format, custom_pipeline_name, device)
  ├─ CPU offload + 在线量化？ → 临时在 GPU 上加载、量化后再 offload 回 CPU
  ├─ set_default_torch_dtype(od_config.dtype)
  ├─ use_hsdp?
  │    ├─ 是 → _load_model_with_hsdp(...)            # CPU 加载后用 apply_hsdp_to_model 切分
  │    └─ 否 → _init_from_load_format(...)
  │              ├─ "default"  → initialize_model(od_config)         # 走模型 registry
  │              ├─ "diffusers"→ DiffusersAdapterPipeline(...)
  │              └─ "custom_pipeline" → 用户类(od_config=...)
  │           └─ load_weights(model)                  # 迭代 safetensors 填参
  │           └─ _process_weights_after_loading(...)  # 量化后处理
  └─ _apply_skip_softmax_calibration(model)
  └─ return model.eval()
```

权重迭代的核心是 `_prepare_weights` → `_get_weights_iterator` → `get_all_weights`：先定位文件、过滤分片、再产出一个 `(name, tensor)` 迭代器交给模型的 `load_weights`。

#### 4.5.3 源码精读

`_prepare_weights` 负责把「模型路径」解析成「本地权重文件列表」。它会：走 ModelScope 镜像 → 判断本地/远端 → 按 `allow_patterns`（`*.safetensors`/`*.bin`/`*.pt`）匹配 → 若远端则 `download_weights_from_hf` 下载 → 用 `filter_duplicate_safetensors_files` 依据 index 去重：

```python
# diffusers_loader.py:150-227（节选）
model_name_or_path = maybe_download_from_modelscope(model_name_or_path, revision) or model_name_or_path
is_local = os.path.isdir(model_name_or_path)
...
if load_format == "hf":
    allow_patterns = ["*.safetensors", "*.bin"]
if fall_back_to_pt:
    allow_patterns += ["*.pt"]
if not is_local:
    hf_folder = download_weights_from_hf(model_name_or_path, ..., allow_patterns, revision, subfolder=subfolder, ...)
...
if use_safetensors:
    hf_weights_files = filter_duplicate_safetensors_files(hf_weights_files, hf_folder, index_file)
```

`_get_weights_iterator` 决定单线程还是多线程读 safetensors（受 `enable_multithread_weight_load` 与线程数控制），并给每个参数名加上 source 前缀；若模型声明了 checkpoint adapter（如 ModelOpt 的权重重排），再让 adapter 改写迭代器：

```python
# diffusers_loader.py:243-272（节选）
use_multithread = (use_safetensors
    and getattr(self.od_config, "enable_multithread_weight_load", False)
    and self.load_config.safetensors_load_strategy != "torchao")
if use_multithread:
    sorted_hf_weights_files = sorted(hf_weights_files, key=_natural_sort_key)
    weights_iterator = multi_thread_safetensors_weights_iterator(sorted_hf_weights_files, ..., max_workers=num_threads)
else:
    weights_iterator = safetensors_weights_iterator(hf_weights_files, ...)
prefixed_weights_iterator = ((source.prefix + name, tensor) for (name, tensor) in weights_iterator)
```

> `_natural_sort_key`（`diffusers_loader.py:77-80`）保证分片按 `model-00001, model-00002, ...` 的自然顺序而不是字典序（字典序会把 `00010` 排在 `0002` 前面）读入，避免参数错位。

`load_model`（加载器侧）处理「在线量化 + CPU offload」的特殊情况：离线量化（如 AutoRound MXFP8）的权重已是量化态，直接在 CPU 加载；在线量化则要在加速器上跑量化 kernel，所以临时把 `load_device` 切到 GPU，量化后再 `model.to("cpu")`：

```python
# diffusers_loader.py:341-356（节选）
if load_device == "cpu" and self.quant_config is not None and device is not None:
    is_offline = getattr(quant_cfg, "data_type", None) == "mx_fp" or getattr(quant_cfg, "is_checkpoint_quantized", False)
    if not is_offline:
        load_device = device.type
        offload_after_quant = True
```

模型实例化由 `_init_from_load_format` 按 `load_format` 分派：`"default"` 走 `initialize_model(od_config)`（从 `DiffusionModelRegistry` 查模型类、配置量化、应用序列并行 hook，见 [registry.py:350-407](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/registry.py#L350-L407)）；`"custom_pipeline"` 解析用户给的限定类名并 `model_cls(od_config=...)`：

```python
# diffusers_loader.py:614-621（节选）
with device_ctx:
    if load_format == "default":
        model = initialize_model(self.od_config)
    elif load_format == "diffusers":
        model = DiffusersAdapterPipeline(od_config=self.od_config, device=target_device)
    else:
        raise ValueError(f"Unknown load_format: {load_format}")
```

填参与校验在 `load_weights`：调用模型的 `load_weights(iterator)`，拿到「已加载参数集合」，与「期望参数集合」做差，对未加载参数按是否为量化专属后缀（`.g_idx`/`.weight_scale`/`.input_scale` 等）区分——量化专属的告警，其余的报错：

```python
# diffusers_loader.py:501-529（节选）
weights_to_load = self._get_expected_parameter_names(model)
loaded_weights = model.load_weights(self.get_all_weights(model))
if loaded_weights is not None:
    weights_not_loaded = weights_to_load - loaded_weights
    weights_scale_not_loaded = {name for name in weights_not_loaded if name.endswith(("weight_scale", "input_scale"))}
    weights_not_loaded = weights_not_loaded - weights_scale_not_loaded
    if weights_not_loaded:
        self._check_unloaded_weights(weights_not_loaded)
```

HSDP 分支 `_load_model_with_hsdp`（`diffusers_loader.py:623-709`）是另一条加载路径：先在 CPU 加载完整权重 → `_process_weights_after_loading` → 用 `ModuleDiscovery` 找到最外层 DiT → 对每个 DiT `apply_hsdp_to_model` 切分 → 把 VAE / encoder 等非切分模块显式 `.to(target_device)`。这条路径只在 `parallel_config.use_hsdp=True` 时启用，详细机制留到 u7-l4。

#### 4.5.4 代码实践（源码阅读型）

**目标**：确认「同一个模型仓库，safetensors 分片是怎么被按正确顺序读进来的」。

**步骤**：

1. 在 [diffusers_loader.py:77-80](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/model_loader/diffusers_loader.py#L77-L80) 读 `_natural_sort_key`，注意它用 `re.split(r"(\d+)", ...)` 把数字段转成 `int`。
2. 在 [diffusers_loader.py:243-262](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/model_loader/diffusers_loader.py#L243-L262) 看多线程路径如何先 `sorted(..., key=_natural_sort_key)` 再交给 `multi_thread_safetensors_weights_iterator`。
3. 思考：如果不排序，`model-00010-of-00020` 会在 `model-00002-of-00020` 之前被处理，但 `load_weights` 内部通常按参数名装载而非按文件顺序，所以分片乱序一般不会出错——**除非**模型实现依赖文件顺序（例如 streaming loader 累积状态）。

**需要观察的现象**：排序是「保险措施」，确保即使下游对顺序敏感也不会踩坑。

**预期结果**：你能解释 `_natural_sort_key` 解决了「字典序排序下 `00010 < 0002`」的问题，并说出它在多线程权重加载路径（`enable_multithread_weight_load=True`）里被显式使用。

> 待本地验证：若你本机有一个分片 safetensors 模型，可在 Python 里 `sorted(["model-00001-...","model-00010-...","model-00002-..."], key=_natural_sort_key)` 直接观察顺序差异（与不传 key 时对比）。

#### 4.5.5 小练习与答案

**练习 1**：`load_format` 有哪些取值？分别走哪条实例化路径？

**参考答案**：主要有 `"default"`（`initialize_model` 走 registry）、`"diffusers"`（`DiffusersAdapterPipeline` 通用适配器）、`"custom_pipeline"`（用户给的限定类名）。此外 runner 侧还可能传 `"dummy"`（在 runner 的 `load_model` 开头直接 return，不加载权重，用于测试）。

**练习 2**：为什么 HSDP 路径要「先在 CPU 加载完整权重，再切分」，而不是像普通路径那样直接 `with target_device:` 在 GPU 上创建权重？

**参考答案**：因为 HSDP 需要把权重按 `hsdp_shard_size`/`hsdp_replicate_size` 在各 GPU 间重新分布（`apply_hsdp_to_model`），这要求权重先以完整的普通 CPU 张量形式存在；若直接在单卡 GPU 上创建，就无法跨 GPU 重分布了。注释（`diffusers_loader.py:644-648`）明确指出 HSDP 需要「weights on CPU first so they can be redistributed」。

---

## 5. 综合实践

**任务**：画出一条 diffusion 请求在 worker 进程内的完整调用链，并在链上准确标注 `cache_backend.refresh` 与 `set_forward_context` 的位置。

**背景**：这是本讲的总体实践任务，把 4.1–4.5 串起来。你需要结合执行器侧（主进程）与 worker 侧（子进程）两个视角。

**操作步骤**：

1. **执行器侧起点**。打开 [multiproc_executor.py:465-503](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/executor/multiproc_executor.py#L465-L503) 的 `execute_request`，看它如何对每条新请求调 `collective_rpc("execute_model", args=(req, od_config, kv_prefetch_job), unique_reply_rank=0, exec_all_ranks=True)`。
2. **进入 broadcast_mq**。在 [multiproc_executor.py:602-614](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/executor/multiproc_executor.py#L602-L614) 确认 request 模式下 RPC 被赋了 `rpc_id` 并 `broadcast_mq.enqueue(rpc_request)`。
3. **worker 侧收消息**。在 [diffusion_worker.py:1033-1065](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/worker/diffusion_worker.py#L1033-L1065) 的 `_worker_busy_loop` 找到 `msg = self.mq.dequeue(...)` 与 `type=="rpc"` 分支 → `_execute_rpc(msg)`。
4. **方法派发**。在 [diffusion_worker.py:990-992](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/worker/diffusion_worker.py#L990-L992) 与 [1375-1395](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/worker/diffusion_worker.py#L1375-L1395) 看 `execute_method("execute_model")` 如何经 `WorkerWrapperBase.__getattr__` 落到 `DiffusionWorker.execute_model`。
5. **Worker → Runner**。在 [diffusion_worker.py:440-469](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/worker/diffusion_worker.py#L440-L469) 看 DP rank 选请求、LoRA 激活、`self.model_runner.execute_model(req, kv_prefetch_job=...)`。
6. **Runner 内部三段**。在 [diffusion_model_runner.py:462-531](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/worker/diffusion_model_runner.py#L462-L531) 的 `_execute_request_list` 标出三处关键点：
   - `_prepare_request_for_forward`（KV 接收/预取）；
   - `_refresh_cache_for_requests`（第 496 行）→ `cache_backend.refresh(pipeline, num_inference_steps)`；
   - `with set_forward_context(...)`（第 503 行）包住 `self.pipeline.forward(batch)`（第 505 行）。
7. **结果回传**。在 [diffusion_worker.py:861-888](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/worker/diffusion_worker.py#L861-L888) 看 `_return_result` 的异步 `COMPUTE_DONE` → 后台线程 `OUTPUT_READY` 两段式回传。

**交付物**：一张调用链图，形如：

```text
executor.execute_request
  → collective_rpc("execute_model") → broadcast_mq.enqueue(rpc)
       ┃ (SHM 广播)
  worker._worker_busy_loop → mq.dequeue → _execute_rpc
    → WorkerWrapperBase.execute_method → DiffusionWorker.execute_model
      → (DP 选 req / LoRA) → DiffusionModelRunner.execute_model
        → _execute_request_list
            ├─ _prepare_request_for_forward      (KV recv + prefetch)
            ├─ _refresh_cache_for_requests ───────► cache_backend.refresh(pipeline, n_steps)  ★
            └─ with set_forward_context(...): ────► (全局 ForwardContext 注入)               ★
                  pipeline.forward(batch) ────────► (transformer 去噪，attention 读 ctx)
  worker._return_result → (async) COMPUTE_DONE → 后台 D2H+SHM → OUTPUT_READY → result_mq
       ┃
executor.result_pump / _dequeue_one_with_failure_polling 取回结果
```

**验收标准**：图中 `★` 两处标注正确——`cache_backend.refresh` 在 `set_forward_context` 之外、`pipeline.forward` 之前；`set_forward_context` 包住 `pipeline.forward`。

> 待本地验证：若本机有 GPU 且装好了 vllm-omni，可启动一个 Z-Image-Turbo 的离线推理（参考 u1-l4），在 `diffusion_model_runner.py:503` 与 `:505` 各加一行 `logger.info`（仅用于学习，勿提交），观察日志确认 refresh 先于 forward 打印。若无法运行，本任务作为纯源码阅读完成即可。

## 6. 本讲小结

- worker 进程分四层：进程入口 `worker_main` → IPC 大脑 `WorkerProc` → 动态继承包装 `WorkerWrapperBase` → 基础设施类 `DiffusionWorker`，模型相关操作全部委托给 `DiffusionModelRunner`。
- `init_device` 通过 `current_omni_platform` 完成跨硬件设备选择，再用 `init_distributed_environment` + `initialize_model_parallel` 建立 NCCL/HCCL 通信域与 TP/SP/CFG/DP/PP 并行组；`MASTER_PORT` 由 `OmniDiffusionConfig._resolve_master_port` 三级解析，避免多副本冲突。
- 「ready 握手 = 模型已加载」：`DiffusionWorker.__init__` 在构造期同步完成 `init_device` 与 `load_model`，所以执行器收到 ready 时权重已就位。
- worker 主循环 `_worker_busy_loop` 按 `type` 路由 sleep/wake/rpc/shutdown/直接生成五类消息；前向请求主要走 `rpc` → `execute_method` → `execute_model`；request 模式下用 `COMPUTE_DONE`/`OUTPUT_READY` 两段式异步回传，让 D2H 与下一次前向重叠。
- `DiffusionModelRunner.load_model` 给 pipeline 装配 offload / torch.compile / cache_backend / prompt-embed cache 四类外挂；前向时先 `_refresh_cache_for_requests`（`cache_backend.refresh`），再在 `set_forward_context` 上下文内调 `pipeline.forward`。
- `DiffusersPipelineLoader` 负责权重下载、分片过滤、`_natural_sort_key` 排序、safetensors 迭代与量化后处理；HSDP 走单独的「CPU 加载后切分」路径。

## 7. 下一步学习建议

- **u5-l4 Diffusion Pipeline 与去噪数据流**：本讲止步于 `pipeline.forward(batch)` 这一行；下一讲会钻进 pipeline 内部，讲 CFG 双前向、`scheduler.step`、`vae.decode` 与完整的去噪数据流。
- **u7-l3 缓存加速**：本讲只提到 `cache_backend.refresh` 的调用点；TeaCache / Cache-DiT / MagCache 的内部机制（hook 注入、缓存命中率）在 u7-l3 详述。
- **u7-l4 并行策略**：本讲的 `initialize_model_parallel` 只列出了参数；TP/SP/CFG/DP/HSDP 各并行组的 rank 划分与适用场景在 u7-l4 展开。
- **延伸阅读**：若对 worker 的 sleep/wake 显存管理感兴趣，可继续读 [diffusion_worker.py:565-752](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/worker/diffusion_worker.py#L565-L752) 的 `sleep`/`wake_up`/`handle_sleep_task`/`handle_wake_task`，理解 `CuMemAllocator` 如何按 tag 卸载/恢复权重。
