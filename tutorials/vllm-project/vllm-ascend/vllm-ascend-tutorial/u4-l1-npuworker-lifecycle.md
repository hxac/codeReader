# NPUWorker 生命周期

## 1. 本讲目标

本讲聚焦 vllm-ascend 执行主链路上**最贴近硬件的一层**——`NPUWorker`。学完后你应该能够：

- 说出 `NPUWorker` 在 vLLM 进程模型中的位置（它是引擎核心 spawn 出的「单卡 worker 子进程」的主角）。
- 梳理出一个 worker 从「构造 → 设备初始化 → 模型加载 → 编译预热 → KV 缓存就绪 → 推理循环 → 睡眠/唤醒/在线更新 → 健康检查 → 关闭」的完整方法调用顺序，并能标注每一步的关键副作用。
- 理解 `execute_model` 如何把调度器的输出（`SchedulerOutput`）转交给 `NPUModelRunner` 完成一次前向，以及流水线并行（PP）下中间张量如何在 rank 间收发。
- 理解睡眠模式（`sleep`/`wake_up`）如何释放并恢复显存，以及在线权重更新（`start_weight_update`/`update_weights`/`finish_weight_update`）的状态机。

本讲承接 u2-l1 的 `NPUPlatform`：平台层负责「告诉 vLLM 这是 NPU、该走哪些后端」，而 `NPUWorker` 负责「在每一张 NPU 上真正把一次推理跑起来」。

## 2. 前置知识

- **进程模型**：vLLM v1 把推理拆成「前端 / 引擎核心（EngineCore）/ worker 子进程」。开启张量并行（TP）时，每张卡对应一个 worker 子进程；这些子进程由执行器（multiproc/ray）用 `spawn` 拉起。`NPUWorker` 就是每个子进程里干活的那个对象。
- **WorkerBase 契约**：vLLM 上游的 `WorkerBase` 定义了一组「生命周期钩子方法」（`init_device`、`load_model`、`compile_or_warm_up_model`、`execute_model`、`check_health` 等）。执行器会按固定顺序调用它们，`NPUWorker` 继承后重写这些钩子，把 CUDA 路径改道到 Ascend 路径。
- **适配手段回顾（来自 u3）**：因为 worker 子进程是 `spawn` 出来的全新解释器，**不继承父进程的 monkey-patch**，所以每个 worker 必须在自己的初始化里重新打一次 worker 级补丁。这是理解 `__init__` 里 `adapt_patch()` 调用的关键。
- **TP/PP/EP/CP**：张量并行、流水线并行、专家并行、上下文并行。本讲会涉及 PP 的中间张量收发（`irecv_tensor_dict`/`isend_tensor_dict`）和序列并行（SP）开关。
- **KV 缓存**：推理时缓存注意力的 Key/Value，按 block 分配在 NPU 显存里。睡眠模式会把权重/KV 在显存与内存间搬运。

> 一个心智模型：`NPUPlatform` 是「身份证 + 总调度」，`NPUWorker` 是「派到某张卡上的操作员」，`NPUModelRunner`（下一讲 u4-l2）是「操作员手里的前向计算引擎」。本讲只到 worker 层，前向细节留到下一讲。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [vllm_ascend/worker/worker.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/worker.py) | **本讲主角**。定义 `NPUWorker`，是每个 worker 子进程的核心对象，串联设备初始化、分布式环境、模型加载、推理执行、睡眠唤醒、在线更新与健康检查。 |
| [vllm_ascend/device_allocator/camem.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/device_allocator/camem.py) | `CaMemAllocator` 单例，基于 CANN 可插拔分配器实现睡眠模式的显存卸载/丢弃/恢复。被 `sleep`/`wake_up`/`load_model` 调用。 |
| [vllm_ascend/device_allocator/sleep_mem_optimized.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/device_allocator/sleep_mem_optimized.py) | `SleepWakeupManager`，睡眠时额外清理 HCCL 进程组与 ACL Graph 工作区，唤醒时重建。 |
| [vllm_ascend/utils.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/utils.py) | 提供 `adapt_patch()`（重打 worker 补丁）、`register_ascend_customop()`（注册自定义算子）、`setup_ascend_local_comm_res()`（A5 本地通信资源）等初始化帮手。 |
| [vllm_ascend/distributed/parallel_state.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/distributed/parallel_state.py) | `init_ascend_model_parallel()`，建立 Ascend 的 TP/EP/DP/PP/PCP 并行分组。 |

## 4. 核心概念与源码讲解

### 4.1 NPUWorker 定位与生命周期总览

#### 4.1.1 概念说明

`NPUWorker` 继承自上游 vLLM v1 的 `WorkerBase`：

[vllm_ascend/worker/worker.py:89-89](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/worker.py#L89-L89) —— 声明 `class NPUWorker(WorkerBase)`。

它的职责可以一句话概括：**在单张 NPU 上，按执行器要求的方法调用顺序，把一次推理所需的「设备、分布式、模型、显存、图」全部准备好，并在每次请求时执行一次前向**。它本身不做前向计算——前向委托给 `self.model_runner`（`NPUModelRunner`），worker 层主要负责「资源管理与生命周期编排」。

`NPUWorker` 和上游 GPU 版 `Worker` 的关系，与整个项目的策略一致：**继承 + 重写**，而非 fork。凡涉及 CUDA 假设的地方（设备设置、显存探测、图捕获、采样器）都被改写为 NPU 版。

#### 4.1.2 核心流程

执行器在 worker 子进程里按下面这条主线依次调用 worker 的方法（具体编排由上游 vLLM 驱动，本讲标注每步的关键副作用）：

```text
构造阶段
  __init__             打 worker 补丁、注册算子、建 AscendConfig、建 SleepWakeupManager
        │
设备/分布式阶段
  init_device          设置 NPU 设备、建 workspace、建 ModelRunner、建分布式环境、设随机种子
        │
模型阶段
  load_model           （在 weights 内存池里）加载模型权重；按需创建权重传输引擎
        │
  compile_or_warm_up_model   预热各 batch size、（非 eager 时）捕获 ACL Graph、ATB 预热、CPU 绑核
        │
  determine_available_memory profile_run 探测显存，算出 KV 缓存可用额度
        │
  initialize_from_config    （在 kv_cache 内存池里）分配 KV 缓存
        │
推理循环（反复）
  execute_model        收 PP 中间张量 → 委托 model_runner 前向 → 发 PP 中间张量
        │
后台 / 按需
  check_health         调 npu-smi 查询硬件健康
  sleep / wake_up      显存卸载/恢复（睡眠模式）
  start_weight_update / update_weights / finish_weight_update   在线权重更新
  shutdown             关闭 KV 连接器、profiler、权重引擎、model_runner
```

#### 4.1.3 源码精读

`__init__` 的开头有一句很能体现项目整体策略的话——**每个 worker 进程都要重新打补丁**：

[vllm_ascend/worker/worker.py:107-118](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/worker.py#L107-L118) 说明：先 `adapt_patch()`（默认 `is_global_patch=False`，触发 worker 级补丁），再注册 dummy fusion op、注册 ATB 扩展与 Ascend 自定义算子。这正是 u3-l3 讲过的「spawn 子进程不继承父进程补丁，每个 worker 必须重打」的落点。

[vllm_ascend/utils.py:533-537](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/utils.py#L533-L537) 说明：`adapt_patch(is_global_patch)` 的分发逻辑——True 走 platform 补丁，False 走 worker 补丁。

随后 `__init__` 建立睡眠管理器与权重引擎占位：

[vllm_ascend/worker/worker.py:143-151](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/worker.py#L143-L151) 说明：开睡眠模式时建 `_sleep_saved_buffers` 与 `SleepWakeupManager`；`weight_transfer_engine` 先置 `None`，要等 `load_model` 拿到模型后才能创建。

#### 4.1.4 代码实践

**实践目标**：建立「worker 子进程里方法被调用的先后」直觉，区分哪些是构造期一次性副作用、哪些是每请求重复副作用。

**操作步骤**：

1. 打开 `vllm_ascend/worker/worker.py`，定位 `class NPUWorker`。
2. 用上方流程图给每个方法标注「一次性」或「每请求/周期」。
3. 思考：为什么 `adapt_patch()` 写在 `__init__` 而不是写在 `init_device`？

**预期结果**：`__init__` / `init_device` / `load_model` / `compile_or_warm_up_model` / `initialize_from_config` 都是一次性；`execute_model` 是每请求；`check_health` 是周期；`sleep`/`wake_up`/`update_weights` 是按需。补丁必须早于任何可能用到被替换符号的代码，所以放在构造最早处（`__init__`）。

#### 4.1.5 小练习与答案

**练习 1**：`NPUWorker` 继承的是 `WorkerBase`，它为什么不直接继承上游的 GPU 版 `Worker`？
**参考答案**：为了保持「解耦 + 不 fork」。直接继承 GPU Worker 会把大量 CUDA 假设（显存探测 API、CUDA Graph、NCCL）一并带进来，需要在更多地方打补丁；继承抽象基类 `WorkerBase` 只需实现/重写必要的钩子，更干净。

**练习 2**：`weight_transfer_engine` 为什么不能在 `__init__` 里直接创建？
**参考答案**：它需要持有「已加载的模型对象」引用（见 4.5），而模型要到 `load_model` 阶段才存在，所以 `__init__` 先置 `None`。

---

### 4.2 构造与设备/分布式初始化

#### 4.2.1 概念说明

「设备初始化」要解决两件事：**让这个进程绑定到正确的 NPU**，以及**把这个进程接入分布式通信世界（HCCL）**。听起来简单，但 vllm-ascend 在这里要处理数据并行（DP）下的设备号偏移、`--device-ids` 预分配、A5（如 300I Duo）的本地通信资源、Triton 图模式所需的 `_inductor` 导入等细节。

关键概念：

- **local_rank**：进程在本机的卡序号；TP/PP 会让多进程共用一组卡，DP 会把卡分组。
- **HCCL**：华为集合通信库（Huawei Collective Communication Library），相当于 NCCL 的 NPU 版。
- **内存快照（MemorySnapshot）**：初始化时记录显存总量与空闲量，后续 KV 缓存额度计算以此为基准。

#### 4.2.2 核心流程

`init_device()`（对外入口）内部委托 `_init_device()`（实现体），分两段：

```text
_init_device()
  ├── 1. DP local_rank 偏移：按 dp_local_rank * tp_pp_world_size 平移 self.local_rank
  ├── 2. 逻辑→物理设备映射：set_assigned_physical_gpu_ids / device_count 断言
  ├── 3. torch.npu.set_device(device) 绑定当前卡
  ├── 4. 惰性 import torch_npu._inductor（供 Triton 图模式，须在 set_device 之后）
  ├── 5. gc.collect() + torch.npu.empty_cache()
  ├── 6. A5 芯片：setup_ascend_local_comm_res 加载本地通信端点
  ├── 7. 记录 init_snapshot / requested_memory，校验空闲显存 ≥ 预期用量
  ├── 8. _init_worker_distributed_environment()  建 HCCL 世界与并行组
  ├── 9. set_random_seed(model_config.seed)
  └── 10. init_device_properties_triton()  注入 Triton 内核所需的设备属性
init_device()
  ├── self.device = _init_device()
  ├── init_workspace_manager(device)  建 workspace
  └── 建造 ModelRunner（v1 或 v2，按 use_v2_model_runner 选）
```

`_init_worker_distributed_environment()` 的内部步骤见源码精读，核心是「建世界 → 建并行组 → 建 Ascend 专属分组 → 建 EC 传输」。

#### 4.2.3 源码精读

设备绑定的核心两句：

[vllm_ascend/worker/worker.py:409-412](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/worker.py#L409-L412) 说明：用平台层 `current_platform.logical_device_id_to_visible_device_id` 把逻辑 local_rank 映射为物理卡号，再 `torch.npu.set_device(device)` 绑定。这里体现了与 `NPUPlatform` 的协作——平台提供映射规则，worker 执行绑定。

DP 设备号偏移：

[vllm_ascend/worker/worker.py:363-385](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/worker.py#L363-L385) 说明：注释解释上游 vLLM PR #45026 取消了 DP worker 的自动设备隔离，vllm-ascend 镜像 `gpu_worker.py` 的做法，把 `local_rank` 按 `dp_local_rank * tp_pp_world_size` 平移，使每个 DP 组绑定到不同 NPU；但若用户用 `--device-ids` 预分配（`assigned_physical_gpu_ids` 非 None）则跳过平移，否则会越界断言。

内存基准与校验：

[vllm_ascend/worker/worker.py:430-442](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/worker.py#L430-L442) 说明：记录 `init_snapshot`，按 `gpu_memory_utilization` 算出 `requested_memory`；若启动时空闲显存已不足预期用量，直接抛错并给出「调低 utilization」的建议。

分布式环境建立：

[vllm_ascend/worker/worker.py:947-960](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/worker.py#L947-L960) 说明：`_init_worker_distributed_environment` 先 `init_batch_invariance()`，再用后端字符串 `"hccl"` 调上游 `init_distributed_environment`，随后 `ensure_model_parallel_initialized` 建立 TP/PP/PCP/DCP 分组，`init_ascend_model_parallel` 建立 Ascend 专属细粒度组（如 MLP_TP/OTP），最后 `ensure_ec_transfer_initialized` 建立专家通信传输。

对外入口与 ModelRunner 建造：

[vllm_ascend/worker/worker.py:467-486](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/worker.py#L467-L486) 说明：`init_device` 调 `_init_device` 拿到 device，建 workspace 管理器，再按 `use_v2_model_runner` 选择 v1（`NPUModelRunner`）或 v2（开发中的 `NPUModelRunnerV2`）建造 model_runner；只有 `rank==0` 上报 usage 统计。

#### 4.2.4 代码实践

**实践目标**：理解「DP 偏移」与「设备绑定」两个副作用如何决定一个 worker 到底落在哪张卡。

**操作步骤**：

1. 阅读第 363–385 行的注释链，回答：在 TP=2、DP=2、单机 4 卡、不使用 `--device-ids`、非 ray 的场景下，4 个 worker 的 `local_rank` 在偏移后分别变成几号？
2. 找到第 459 行 `_init_worker_distributed_environment` 调用，确认它发生在「设备绑定之后、模型加载之前」。

**需要观察的现象**：偏移公式为 `self.local_rank += dp_local_rank * tp_pp_world_size`。TP=2、PP=1 时 `tp_pp_world_size=2`；DP0 的两个 worker 为 0、1，DP1 的两个 worker 偏移为 2、3——正好覆盖 4 张卡，互不重叠。

**预期结果**：能画出 4 个 worker 各自绑定的物理卡号，并解释为何 `--device-ids` 场景要跳过偏移（因为子进程已各自绑卡，再偏移会越界）。

#### 4.2.5 小练习与答案

**练习 1**：为什么第 418–421 行的 `import torch_npu._inductor` 要在 `torch.npu.set_device` 之后？
**参考答案**：注释写明这是「惰性导入」，避免在补丁流程里触发 `torch_npu` 重复初始化 / 重复 `set_device`；放在 set_device 之后能确保图模式依赖的设备上下文已就绪。

**练习 2**：`_init_worker_distributed_environment` 用的通信后端字符串是什么？为什么不是 `"nccl"`？
**参考答案**：是 `"hccl"`。NPU 上集合通信走 HCCL 而非 NCCL，这是把 CUDA/NCCL 路径改道到 Ascend/HCCL 的典型改写。

---

### 4.3 模型加载、编译预热与 KV 缓存就绪

#### 4.3.1 概念说明

设备与分布式就绪后，三个方法接力把模型「装上卡、调好图、留好 KV 空间」：

- **`load_model`**：把权重从磁盘/远端加载到 NPU 显存。开睡眠模式时，权重会被放进带 `"weights"` 标签的内存池，方便睡眠时整体卸载。
- **`compile_or_warm_up_model`**：对若干 batch size 跑 dummy 前向做编译预热；非 eager 模式下捕获 ACL Graph（NPU 版的 CUDA Graph）；额外做 ATB 矩阵乘预热与 CPU 绑核；最后建议一个最优 `--kv-cache-memory` 值。
- **`determine_available_memory`** + **`initialize_from_config`**：前者跑一次 `profile_run` 探测显存，算出 KV 缓存可用额度；后者据此在带 `"kv_cache"` 标签的池子里真正分配 KV 缓存。

关键概念：

- **CaMemAllocator 内存池 + tag**：睡眠模式依赖的可插拔分配器，分配时打 tag（`weights`/`kv_cache`/`sleep_persistent`），睡眠时按 tag 决定「卸载到 CPU」还是「丢弃」。
- **ACL Graph**：昇腾的图捕获/回放机制，对应 CUDA Graph，用于消除每次前向的算子下发开销（详见 u8-l3）。
- **profile_run**：用 dummy 输入跑一次前向，让框架把真实激活显存测出来。

#### 4.3.2 核心流程

```text
load_model()
  ├── enable_sleep_mode? → allocator.use_memory_pool(tag="weights") 包裹
  ├── model_runner.load_model()   真正加载权重
  └── weight_transfer_config 非 None? → WeightTransferEngineFactory.create_engine(...) 建权重引擎

compile_or_warm_up_model()
  ├── 计算 warmup_sizes（compile_sizes ⊖ cudagraph_capture_sizes）
  ├── 对每个 size 跑 model_runner._dummy_run(size)  编译预热
  ├── 非 eager: model_runner.capture_model()  捕获 ACL Graph，返回 npugraph_memory_bytes
  ├── 算并日志「建议 --kv-cache-memory」
  ├── 非 A5: _warm_up_atb()  预热 ATB matmul
  ├── enable_cpu_binding: bind_cpus(local_rank)
  └── set_random_seed 重置随机状态（消除预热污染）

determine_available_memory()
  ├── 快路径：用户已指定 kv_cache_memory_bytes → 只跑 profile_run 编译，直接返回
  └── 慢路径：memory_profiling 上下文里 profile_run，扣除非 KV 内存，得到可用额度

initialize_from_config(kv_cache_config)
  ├── ensure_kv_transfer_initialized(...)
  ├── enable_sleep_mode? → allocator.use_memory_pool(tag="kv_cache")
  ├── model_runner.initialize_kv_cache(kv_cache_config)
  └── eagle3 + mamba 混合 + num_speculative_tokens>1 → _init_kv_zero_meta()  防 NaN
```

#### 4.3.3 源码精读

`load_model` 的睡眠池包裹与权重引擎创建：

[vllm_ascend/worker/worker.py:636-659](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/worker.py#L636-L659) 说明：开睡眠模式时用 `CaMemAllocator.use_memory_pool(tag="weights")` 包裹加载，使权重分配进可卸载池；加载后若配置了 `weight_transfer_config`，用工厂创建权重传输引擎，把模型对象传进去（这正是 4.1 说的「要等模型存在才能建引擎」）。

编译预热的图捕获与显存建议：

[vllm_ascend/worker/worker.py:681-688](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/worker.py#L681-L688) 说明：对 `warmup_sizes` 倒序跑 `_dummy_run` 编译预热；非 eager 时调 `capture_model()` 捕获 ACL Graph 并记下其显存占用 `npugraph_memory_bytes`。

[vllm_ascend/worker/worker.py:696-728](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/worker.py#L696-L728) 说明：未预设 `kv-cache-memory` 且探测过激活显存时，把权重/激活/非 torch/图显存相加并留 150 MiB 余量，算出「适配 requested」与「吃满 free」两个建议值，写进日志供用户下次直接用 `--kv-cache-memory`。

显存探测：

[vllm_ascend/worker/worker.py:488-559](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/worker.py#L488-L559) 说明：`determine_available_memory`。快路径直接返回用户指定值；慢路径在 `memory_profiling` 上下文里跑 `profile_run()`，记录「图捕获前」的 torch peak（避免把图池算成激活），最后 `available_kv_cache_memory_bytes = requested_memory - non_kv_cache_memory`。

KV 缓存分配与 eagle3 零初始化保护：

[vllm_ascend/worker/worker.py:865-896](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/worker.py#L865-L896) 说明：`initialize_from_config` 先初始化 KV 传输，再在 `kv_cache` 池里分配 KV 缓存；针对 eagle3 + mamba 混合且 `num_speculative_tokens>1` 的特殊情形调 `_init_kv_zero_meta()`——注释解释了为何此处要对 KV 块做零初始化以规避 NaN（草稿模型复用脏块 + stale seq_lens 导致的问题）。

#### 4.3.4 代码实践

**实践目标**：理解 `requested_memory` 如何一路传递到 KV 缓存额度的计算。

**操作步骤**：

1. 在 `worker.py` 里搜索 `requested_memory`、`non_kv_cache_memory`、`available_kv_cache_memory_bytes` 三个量的赋值点。
2. 画出它们的数据流：`init_snapshot.total_memory * gpu_memory_utilization → requested_memory`；`requested_memory - non_kv_cache_memory → available_kv_cache_memory_bytes`。

**预期结果**：能解释「为什么先要 `init_snapshot`（设备初始化时记），再在 profile 后扣除非 KV 部分」。`non_kv_cache_memory` = 非 torch 增量 + torch peak 增量 + 权重显存（第 534–536 行）。

> 若无法运行真实 NPU，这是「源码阅读型实践」：跟踪数据流即可，不必跑通。

#### 4.3.5 小练习与答案

**练习 1**：`determine_available_memory` 的「快路径」何时触发？快路径下还会跑 `profile_run` 吗？
**参考答案**：用户显式指定了 `--kv-cache-memory`（`kv_cache_memory_bytes` 非 None）时触发。快路径仍会跑 `profile_run`（为了让模型完成编译），但**跳过**显存探测计算，直接返回用户指定值（见第 502–515 行）。

**练习 2**：为什么 `compile_or_warm_up_model` 末尾要再次 `set_random_seed`？
**参考答案**：预热/探测会消费随机数、改变全局随机状态，末尾重置种子能保证「初始化与探测不污染正式推理的随机性」（第 742–744 行）。

---

### 4.4 推理主循环：execute_model 的衔接

#### 4.4.1 概念说明

进入稳态后，引擎核心每调度出一个 batch，就向各 worker 发一次 `execute_model(scheduler_output)` 调用。`NPUWorker.execute_model` **本身不做前向**，它做的是「上下游衔接」：

- **PP 衔接**：流水线并行下，非首 rank 要先 `irecv_tensor_dict` 收上一阶段的中间张量，前向后再 `isend_tensor_dict` 发给下一阶段；首 rank 只发，末 rank 只收。
- **SP 衔接**：序列并行开启时，收发的 all-gather 组要置为 `None`，避免与 flashcomm1 的 all-gather 冲突。
- **前向委托**：把 `scheduler_output`（和收到的 `intermediate_tensors`）交给 `self.model_runner.execute_model`。
- **投机解码草稿**：若是 PP 中间阶段产出的 `IntermediateTensors`，按是否 v2 runner 分支处理 KV 连接器输出。
- **profiler / msmonitor 步进**、**PP 异步发送句柄回收**。

关键概念：

- **SchedulerOutput**：调度器一次调度结果的容器，含本步要跑哪些请求、各请求多少 token。
- **IntermediateTensors / AsyncIntermediateTensors**：PP 阶段间传递的隐藏状态张量；`Async*` 版本附带通信句柄，实现计算与通信重叠。
- **`_pp_send_work`**：暂存上一步 PP 异步发送的句柄列表，下一步开头 `wait()` 收尾。

#### 4.4.2 核心流程

```text
execute_model(scheduler_output)
  ├── msmonitor 开启? → dp.step()  上报性能
  ├── 回收上一步 PP 异步发送句柄 _pp_send_work
  ├── forward_pass = total_num_scheduled_tokens > 0
  ├── forward_pass 且非 PP 首 rank?
  │     ├── enable_sp()? all_gather_group = None : get_tp_group()
  │     └── tensor_dict,... = get_pp_group().irecv_tensor_dict(...)   收中间张量
  │         intermediate_tensors = AsyncIntermediateTensors(tensor_dict, ...)
  ├── profiler.step()
  ├── output = model_runner.execute_model(scheduler_output, intermediate_tensors)   ★前向
  ├── output 是 ModelRunnerOutput/Async/None? → 直接返回（末 rank）
  ├── 否则 output 是 IntermediateTensors（PP 中间阶段）
  │     ├── isend_tensor_dict(output.tensors, all_gather_group=...) → _pp_send_work  发中间张量
  │     ├── use_v2_model_runner? → return None
  │     └── 处理 kv_connector_output 的透传（v1 路径）
```

#### 4.4.3 源码精读

PP 中间张量的接收（带 SP 分支）：

[vllm_ascend/worker/worker.py:574-591](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/worker.py#L574-L591) 说明：`forward_pass` 判定是否真有 token 要算；非首 rank 时，按 `enable_sp()` 决定 `all_gather_group`（SP 开则置 `None` 以兼容 flashcomm1），调 `irecv_tensor_dict` 异步收上一阶段中间张量，包成 `AsyncIntermediateTensors`。

前向委托与返回值分流：

[vllm_ascend/worker/worker.py:596-619](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/worker.py#L596-L619) 说明：第 596 行把前向真正交给 `model_runner.execute_model`；若返回 `ModelRunnerOutput/Async/None`（末 rank 的最终结果）直接返回；若是 `IntermediateTensors`（PP 中间阶段），则 `isend_tensor_dict` 把张量异步发给下一阶段并存入 `_pp_send_work`，v2 runner 直接 return None，v1 路径继续处理 KV 连接器透传。

上一步异步发送的回收：

[vllm_ascend/worker/worker.py:569-572](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/worker.py#L569-L572) 说明：每次 `execute_model` 开头先 `wait()` 上一轮存下的 PP 发送句柄，保证「发送完成」先于「下一轮复用缓冲」。

#### 4.4.4 代码实践

**实践目标**：理解 PP 下「首 rank / 中间 rank / 末 rank」三者在 `execute_model` 里走的不同分支。

**操作步骤**：

1. 假设 PP=3（rank0 首、rank1 中、rank2 末），分别追踪它们在本方法里：是否调 `irecv_tensor_dict`？是否调 `isend_tensor_dict`？返回值是 `IntermediateTensors` 还是 `ModelRunnerOutput`？
2. 对照第 576 行 `not get_pp_group().is_first_rank` 与第 602 行 `not get_pp_group().is_last_rank`。

**预期结果**：

| PP 位置 | irecv（收） | 前向委托 | isend（发） | 返回 |
| --- | --- | --- | --- | --- |
| 首 rank | 否 | 是 | 是 | IntermediateTensors（v1）/None（v2） |
| 中间 rank | 是 | 是 | 是 | IntermediateTensors / None |
| 末 rank | 是 | 是 | 否 | ModelRunnerOutput |

#### 4.4.5 小练习与答案

**练习 1**：开启序列并行（SP）时，为什么 `irecv_tensor_dict`/`isend_tensor_dict` 的 `all_gather_group` 要置 `None`？
**参考答案**：SP 用 flashcomm1 时，其内部已有 all-gather 操作；若再传 TP 组作 all_gather_group，会与 flashcomm1 的 all-gather 冲突（注释见第 577–579、603–605 行），故 SP 开启时传 `None` 关掉这层 all-gather。

**练习 2**：`_pp_send_work` 为什么是个 list 且在 `execute_model` 开头统一 wait？
**参考答案**：PP 发送是异步的，句柄可能跨多轮积累；在每轮开头统一 `wait()` 既能保证上一批发送完成、释放缓冲，又能在睡眠/健康检查前确保没有悬挂的异步通信（`HcclSleepWakeupManager.sleep` 也会先 wait 它，见 sleep_mem_optimized.py）。

---

### 4.5 睡眠/唤醒、在线权重更新与健康检查

#### 4.5.1 概念说明

这三个机制让 worker 在「长跑」中具备运维与弹性能力：

- **睡眠模式（sleep/wake_up）**：分时复用显存——睡觉时把权重（level 1）甚至更多（level 2）卸载到 CPU 释放显存，醒来再恢复。基于 `CaMemAllocator` 的可插拔分配器实现：睡觉按 tag 把 NPU 显存 unmap 释放（权重搬 CPU，其余丢弃），醒来再 remap 回来。`SleepWakeupManager` 额外处理 HCCL 进程组的销毁/重建与 ACL Graph 工作区清理/重捕获。
- **在线权重更新**：RL/在线学习场景下，训练侧把新权重逐层推给推理 worker。`NPUWorker` 暴露 `init_weight_transfer_engine` / `start_weight_update` / `update_weights` / `finish_weight_update` 四步，用 `_weight_update_active` 布尔量维护一个简易状态机，防止乱序调用。
- **健康检查（check_health）**：周期性调外部命令 `npu-smi info -i <rank> -t health` 查询 NPU 卡健康状态，解析输出里 `Health: OK`；超时/工具缺失/异常都降级为 warning，只有健康状态非 OK 才抛错。

#### 4.5.2 核心流程

睡眠/唤醒（以 level 1 卸载权重为例）：

```text
sleep(level=1)
  ├── 记 free_bytes_before
  ├── level==2? 先把 model.named_buffers() 搬 CPU 保存
  ├── enable_sleep_mode_extra_cleanup? sleep_wakeup_manager.sleep()  清 HCCL + ACL Graph 工作区
  ├── CaMemAllocator.sleep(offload_tags=("weights",))   权重搬 CPU、其余丢弃、unmap 释放
  └── 日志：释放了多少 GiB

wake_up(tags=None)
  ├── weight_nz_mode 开启? 抛错（RL 下 NZ 会精度受损）
  ├── CaMemAllocator.wake_up(tags)  remap 显存、权重搬回 NPU
  ├── 非 quant 且含 weights? 对 w2/w13 权重做 transpose(1,2) 恢复布局
  ├── 恢复 level 2 保存的 buffers
  ├── 含 kv_cache? model_runner.post_kv_cache_wake_up()
  └── extra_cleanup 开启? sleep_wakeup_manager.wakeup(tags)  重建 HCCL + 重捕获图
```

在线权重更新状态机：

```text
init_weight_transfer_engine(init_info)   解析并初始化引擎（前置：load_model 已建引擎）
        │
start_weight_update()   _weight_update_active=False→True，校验 NZ 关闭，引擎进入更新态
        │  （若已 active 则报错）
update_weights(update_info)   可多次调用，逐块接收权重；异常时回退 _weight_update_active=False
        │  （未 start 则报错）
finish_weight_update()   _weight_update_active=True→False，引擎做逐层后处理
```

健康检查：

```text
check_health()
  └── subprocess.run(["npu-smi","info","-i",local_rank,"-t","health"], timeout=10)
        ├── returncode==0 → parse_text_output，非 "OK" 抛 RuntimeError
        └── 超时/工具缺失/异常 → warning，不抛错
```

#### 4.5.3 源码精读

`sleep` 的两级卸载：

[vllm_ascend/worker/worker.py:212-235](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/worker.py#L212-L235) 说明：记录睡前后空闲显存；level 2 先把 `named_buffers()` 搬 CPU；可选触发 `SleepWakeupManager.sleep()`（清 HCCL 与 ACL Graph 工作区）；调 `CaMemAllocator.sleep(offload_tags=("weights",) if level==1 else tuple())`——level 1 只卸载权重，tuple() 表示全卸载。

`wake_up` 的恢复与权重布局修正：

[vllm_ascend/worker/worker.py:237-280](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/worker.py#L237-L280) 说明：先校验 `weight_nz_mode`（NZ 分形在 RL 下有精度风险，禁止）；`CaMemAllocator.wake_up` remap 显存并把权重从 CPU 搬回 NPU；对非量化模型的 `w2_weight`/`w13_weight` 做 `transpose(1,2)` 恢复权重布局；恢复 level 2 的 buffers；按 tag 决定是否 `post_kv_cache_wake_up`；可选重建 HCCL 与重捕获图。

> 对照 `CaMemAllocator.sleep`/`wake_up` 的实现 [vllm_ascend/device_allocator/camem.py:180-249](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/device_allocator/camem.py#L180-L249)：sleep 时对 `offload_tags` 内的分配创建 CPU 备份并 `memcpy` 搬走、其余直接 unmap 释放；wake_up 时 `create_and_map` 重新映射、有备份的再 `memcpy` 搬回。`sleep_persistent_tag` 的分配跨睡醒周期不被释放。

在线权重更新状态机：

[vllm_ascend/worker/worker.py:303-342](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/worker.py#L303-L342) 说明：`start_weight_update` 把 `_weight_update_active` 置 True 并校验 NZ 关闭；`update_weights` 要求必须先 start，异常时回退 active 标志；`finish_weight_update` 置回 False 并触发逐层后处理。三者都先 `_check_weight_transfer_engine`（第 282–286 行），引擎未配置则报错。

健康检查与外部命令解析：

[vllm_ascend/worker/worker.py:977-1010](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/worker.py#L977-L1010) 说明：`check_health` 用 `subprocess.run` 调 `npu-smi`，10 秒超时；超时/`FileNotFoundError`/异常都只 warning，唯解析出 `Health` 非 `OK` 才抛 `RuntimeError`（`parse_text_output`）。

优雅关闭：

[vllm_ascend/worker/worker.py:344-357](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/worker.py#L344-L357) 说明：`shutdown` 依次关闭 KV 传输连接器、profiler、权重传输引擎、model_runner（若各自存在），收尾资源。

#### 4.5.4 代码实践

**实践目标**：把「睡眠显存释放」与「在线权重更新状态机」两条机制对应到具体方法副作用。

**操作步骤**：

1. 在 `worker.py` 与 `camem.py` 里追踪一次 `sleep(level=1)`：worker 记 free → `CaMemAllocator.sleep(("weights",))` → 对每个权重分配建 CPU 备份 + memcpy + unmap。问：level 1 时 KV 缓存（tag=`kv_cache`）会被怎样处理？
2. 阅读 `start_weight_update`/`update_weights`/`finish_weight_update`，回答：如果直接调 `update_weights` 而没先 `start_weight_update` 会发生什么？

**需要观察的现象**：level 1 的 `offload_tags=("weights",)` 只搬权重；KV 缓存 tag 不在卸载集合内，会走「丢弃」（unmap 不备份）分支，醒来后 KV 数据为空——这正是睡眠后 KV 需要「重算或重新填充」的原因（参考 u10-l3）。直接调 `update_weights` 不 start 会在第 324–325 行抛 `RuntimeError("start_weight_update must be called before update_weights.")`。

**预期结果**：能说出 level 1 vs level 2 的差别（level 2 还保存 buffers、`offload_tags=()` 表示全卸载），并解释状态机为何用单一布尔量 + 前置校验来防乱序。

#### 4.5.5 小练习与答案

**练习 1**：`wake_up` 开头为什么要检查 `weight_nz_mode` 并在开启时抛错？
**参考答案**：FRACTAL_NZ 分形布局在 RL 在线更新场景会导致权重精度问题（第 238–243 行），所以睡眠唤醒（常伴随 RL 热更新）时若检测到 NZ 开启就主动报错，提示用户设 `weight_nz_mode=0`。

**练习 2**：`check_health` 的设计为什么对「超时/工具缺失」只 warning，而对「健康非 OK」抛错？
**参考答案**：前者属于环境/工具问题，不一定代表硬件坏，降级为 warning 可避免误杀正常进程；后者是明确的硬件不健康信号，必须上报让引擎判定 worker 失效（第 989–1009 行）。

## 5. 综合实践

**任务**：完整画出 `NPUWorker` 的生命周期，并补全每一步的「输入 / 关键副作用 / 输出或状态变化」。

请按下面的表格逐行填写（不写代码，只读源码）：

| 阶段 | 方法 | 输入 | 关键副作用（写 1–2 条） | 输出 / 状态变化 |
| --- | --- | --- | --- | --- |
| 构造 | `__init__` | `vllm_config`, `local_rank`, `rank`, … | 重打 worker 补丁；建 `SleepWakeupManager` | `self.weight_transfer_engine=None` |
| 设备 | `init_device` | — | ? | `self.device`, `self.model_runner` |
| 模型 | `load_model` | — | ? | 模型上卡；权重引擎就绪 |
| 编译 | `compile_or_warm_up_model` | — | ? | `CompilationTimes` |
| 探测 | `determine_available_memory` | — | ? | KV 可用字节数 |
| KV 分配 | `initialize_from_config` | `kv_cache_config` | ? | KV 缓存已分配 |
| 推理 | `execute_model` | `scheduler_output` | ? | `ModelRunnerOutput` / `IntermediateTensors` |
| 健康检查 | `check_health` | — | 调 `npu-smi` | 无返回（异常即抛错） |
| 睡眠 | `sleep` | `level` | ? | 显存释放 |
| 唤醒 | `wake_up` | `tags` | ? | 显存恢复 |
| 在线更新 | `start_weight_update` → `update_weights` → `finish_weight_update` | `init_info`/`update_info` | ? | `_weight_update_active` 状态翻转 |
| 关闭 | `shutdown` | — | 关闭各子系统 | — |

**验收标准**：能不看源码填出「?」处至少各一条副作用，并能解释「为什么补丁在 `__init__`、为什么权重引擎在 `load_model`、为什么 KV 分配在 `determine_available_memory` 之后」这三个时序问题。

## 6. 本讲小结

- `NPUWorker(WorkerBase)` 是每个 worker 子进程的核心对象，负责单卡资源管理与生命周期编排，前向本身委托给 `NPUModelRunner`。
- 构造期 `__init__` 必须先 `adapt_patch()` 重打 worker 级补丁（spawn 子进程不继承父进程补丁），再注册算子、建 `AscendConfig` 与 `SleepWakeupManager`。
- 设备初始化 `_init_device`/`init_device` 处理 DP 设备号偏移、NPU 绑定、HCCL 分布式世界与并行组建立、随机种子与 Triton 设备属性，并建造 v1/v2 ModelRunner。
- `load_model` → `compile_or_warm_up_model`（含 ACL Graph 捕获、ATB 预热、CPU 绑核）→ `determine_available_memory` → `initialize_from_config` 四步接力把模型装上卡、调好图、留好 KV 空间；睡眠模式下权重与 KV 分别进 `weights`/`kv_cache` 内存池。
- `execute_model` 是 PP/SP 衔接层：非首 rank 收中间张量、委托前向、非末 rank 发中间张量，异步发送句柄靠 `_pp_send_work` 跨轮回收。
- 睡眠/唤醒靠 `CaMemAllocator`（按 tag 卸载/丢弃/恢复显存）与 `SleepWakeupManager`（清/重建 HCCL 与 ACL Graph）；在线权重更新是 `_weight_update_active` 驱动的三步状态机；`check_health` 调 `npu-smi` 查硬件。

## 7. 下一步学习建议

- **下一讲 u4-l2 NPUModelRunner v1 主链路**：本讲的 `execute_model` 把前向交给了 `model_runner.execute_model`，下一讲深入 `NPUModelRunner` 的 `_prepare_inputs`、`_build_attention_metadata` 与采样衔接，看一次前向的输入如何被准备好。
- **u4-l3 NPUModelRunner v2 新架构**：本讲已遇到 `use_v2_model_runner` 分支，下一讲对比 v1/v2 状态管理差异。
- **u8-l3 ACL Graph**：本讲的 `capture_model`、`npugraph_memory_bytes`、睡眠唤醒里的图重捕获，都依赖 ACL Graph，建议在读 v2 前后结合 u8-l3 理解图捕获机制。
- **u9-l4 在线权重传输**：本讲的权重传输引擎只是 worker 侧的「调用方」，其 HCCL/NPU IPC 实现在 u9-l4 详讲。
- **建议继续阅读的源码**：`vllm_ascend/device_allocator/camem.py`（可插拔分配器）、`vllm_ascend/device_allocator/sleep_mem_optimized.py`（HCCL/Graph 睡眠管理），以补全睡眠机制的实现细节。
