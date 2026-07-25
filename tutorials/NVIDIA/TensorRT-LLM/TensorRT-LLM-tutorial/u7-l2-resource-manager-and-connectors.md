# ResourceManager 与 KV Cache 连接器

## 1. 本讲目标

在 u7-l1 中，我们把「KV Cache」当作一种**显存资源**，看清了它分页的结构、`KVCacheManager` 的三段式生命周期（prepare / update / free），以及 V1/V2 两套实现。

但 PyTorch 后端在单步循环里要管理的不止是 KV Cache：序列槽（seq slot）、PEFT/LoRA 适配器缓存、投机解码草稿模型缓存、KV cache 压缩……这些都是「随请求生灭的资源」。如果每种资源各写一套驱动代码，`PyExecutor` 的主循环会膨胀到无法维护。

本讲要回答两个问题：

1. **资源容器模式**：`PyExecutor` 如何用统一接口驱动一群异构资源？——答案是一个叫 `BaseResourceManager` 的抽象基类 + 一个叫 `ResourceManager` 的容器。
2. **远程 KV 搬运**：当请求的 KV Cache 要跨 GPU、跨节点移动时（分离式服务、KV 卸载、KV 共享），框架提供了什么抽象？——答案是两条容易混淆但完全不同的路径：**KV Cache Connector**（可插拔的 worker/scheduler 接口）与 **KV Cache Transceiver**（分离式传输器）。

学完后你应当能：

- 说出 `BaseResourceManager` 的契约接口（容量接口 + 三段式生命周期）。
- 在源码里找出所有继承 `BaseResourceManager` 的资源类型，并解释 `ResourceManager` 如何把单步循环扇出（fan-out）给它们。
- 区分 **Connector** 与 **Transceiver** 的职责边界，并描述它们在分离式服务中各自处理哪一段 KV 搬运。
- 读懂 `KvCacheConnectorManager` 的异步请求状态机。

## 2. 前置知识

- **三段式生命周期**（u7-l1）：`prepare_resources`（为调度到的请求**预分配**资源）→ `update_resources`（前向后**回写/回收**资源）→ `free_resources`（请求结束**释放**资源）。本讲会把这套三段式从「KV Cache 专用」泛化成「所有资源通用」。
- **in-flight batching 的「单步」**（u3-l2）：`PyExecutor` 每跑一圈主循环，就把所有活跃请求推进一个 token。资源管理器的三个接口就插在这「一步」的不同阶段。
- **分离式服务（disaggregated serving）**（u1-l1 高空视图）：把 prefill（算力密集）和 decode（带宽密集）拆到不同 GPU 上，KV Cache 必须从 prefill 节点搬到 decode 节点。搬运的「物理层」是 NIXL / UCX / MPI / Mooncake。
- **ABC（抽象基类）**：Python `abc.ABC` + `@abstractmethod`，用来定义「子类必须实现」的接口契约。`BaseResourceManager` 正是这样一个 ABC。
- **leader-worker 模式**：多卡训练/推理里常见，只有 rank 0（leader）真正做编排决策，其余 rank（worker）执行，结果通过广播同步。Connector 的 scheduler 只在 rank 0 存在。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tensorrt_llm/_torch/pyexecutor/resource_manager.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/resource_manager.py) | 定义 `BaseResourceManager` 抽象契约、`ResourceManager` 容器、`KVCacheManager`/`PeftCacheManager` 等多个具体资源管理器，以及 `KvCacheConnectorManager` 在 `KVCacheManager` 内部的接入点。**本讲主战场。** |
| [tensorrt_llm/_torch/pyexecutor/connectors/kv_cache_connector.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/connectors/kv_cache_connector.py) | KV Cache Connector 的分层接口（`KvCacheConnectorWorker` / `KvCacheConnectorScheduler` 两个 ABC）以及把它们粘起来的 `KvCacheConnectorManager` 状态机。 |
| [tensorrt_llm/_torch/pyexecutor/connectors/registry.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/connectors/registry.py) | Connector 预设名（`lmcache` / `lmcache-mp` / `kvbm`）到「模块路径 + scheduler 类 + worker 类」的注册表。 |
| [tensorrt_llm/_torch/pyexecutor/kv_cache_transceiver.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/kv_cache_transceiver.py) | 分离式传输器抽象 `KvCacheTransceiver`、默认 C++ 绑定实现 `BindKvCacheTransceiver`，以及工厂 `create_kv_cache_transceiver`（选择 NIXL/UCX/MPI、C++/Python 运行时）。 |

辅助（用于看清装配点，非本讲精读对象）：

| 文件 | 作用 |
|------|------|
| [tensorrt_llm/_torch/pyexecutor/seq_slot_manager.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/seq_slot_manager.py) | `SeqSlotManager`，另一个继承 `BaseResourceManager` 的资源类型（管序列槽）。 |
| [tensorrt_llm/_torch/pyexecutor/kv_cache_manager_v2.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/kv_cache_manager_v2.py) | `KVCacheManagerV2`，V2 实现，同样继承 `BaseResourceManager`。 |
| [tensorrt_llm/_torch/pyexecutor/py_executor_creator.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor_creator.py) | Executor 的装配车间：动态导入 connector、构造 `KvCacheConnectorManager`、把所有资源塞进容器。 |
| [tensorrt_llm/_torch/pyexecutor/py_executor.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py) | 单步循环：在固定阶段调用 `ResourceManager` 的 prepare/update/free，以及 connector/transceiver 的回调。 |

## 4. 核心概念与源码讲解

### 4.1 BaseResourceManager：资源管理器的统一契约

#### 4.1.1 概念说明

先想一个设计问题：`PyExecutor` 的单步循环要驱动 KV Cache、序列槽、PEFT 缓存、投机解码缓存……这些资源**结构完全不同**（KV Cache 是一池分页显存，序列槽是一组整数编号，PEFT 缓存是 host/device 两级 LoRA 权重表），却都要在「同三个时刻」被驱动：

- 一步开始前，为**新调度到的请求**预分配资源；
- 前向后，根据刚跑完的结果**回写/回收**资源（比如回写 KV Cache、回退被投机解码拒绝的 token）；
- 请求彻底结束时，**释放**它占的全部资源。

「接口相同、实现各异」——这正是抽象基类的用武之地。`BaseResourceManager` 把这套三段式生命周期固定成方法签名，任何想被 `PyExecutor` 自动驱动的资源，只要继承它、实现这几个方法即可。这样主循环里就只有一句「对所有资源管理器依次调 prepare/update/free」，新增一种资源完全不用改 `PyExecutor`。

此外，调度器（scheduler）在决定「还能不能塞新请求」时，需要知道每种资源的**容量上限**和「这个请求跑到结束还需要多少」。于是 `BaseResourceManager` 还规定了两个**容量接口**。

#### 4.1.2 核心流程

`BaseResourceManager` 规定的契约分两组：

**容量接口（抽象，必须实现）**——给调度器用：

- `get_max_resource_count()`：该资源最多能容纳多少个真实请求。
- `get_needed_resource_to_completion(request)`：把 `request` 一路跑到结束，还需要多少单位的资源。

**三段式生命周期（带默认空实现）**——给 `PyExecutor` 单步循环用：

- `add_dummy_requests(request_ids)`：注册 warmup/CUDA Graph 用的假请求。
- `prepare_resources(scheduled_batch)`：为「本步调度到的请求」预分配资源。
- `update_resources(scheduled_batch, ...)`：前向后回写/回收资源。
- `free_resources(request)`：请求结束时释放资源。
- `shutdown()`：整个 Executor 关闭时清理。

用伪代码概括 `PyExecutor` 一步中资源管理器的参与点：

```
一步开始
  scheduler.schedule()          # 调度时读 get_max_resource_count / get_needed_resource_to_completion
  resource_manager.prepare_resources(scheduled_batch)   # 预分配
  model_engine.forward(...)     # 前向
  sampling(...)
  resource_manager.update_resources(scheduled_batch, attn_metadata)  # 回写/回收
一步结束
请求完成时
  resource_manager.free_resources(request)   # 释放
```

> 注意一个关键细节：容量接口是**抽象方法**（必须实现），而三段式生命周期是**带默认空实现的普通方法**（可选覆盖）。这意味着一个资源管理器至少要告诉调度器「我有多少容量」，但不一定要参与每一步的资源预分配——例如 PEFT 缓存和压缩管理器对容量接口直接返回 0，明确表示「不要因为我卡住调度」。

#### 4.1.3 源码精读

`BaseResourceManager` 的定义非常短，但它是整个资源管理体系的「宪法」：

```python
# resource_manager.py
class BaseResourceManager(ABC):

    @abstractmethod
    def get_max_resource_count(self) -> int:
        """Return the maximum number of real requests this manager can admit."""
        raise NotImplementedError

    @abstractmethod
    def get_needed_resource_to_completion(self, request: LlmRequest) -> int:
        raise NotImplementedError

    def add_dummy_requests(self, request_ids: List[int]):
        pass

    def prepare_resources(self, scheduled_batch: ScheduledRequests):
        pass

    def update_resources(self, scheduled_batch: ScheduledRequests):
        pass

    def free_resources(self, request: LlmRequest):
        pass

    def shutdown(self):
        pass
```

- 抽象基类与两个容量接口：[tensorrt_llm/_torch/pyexecutor/resource_manager.py:138-162](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/resource_manager.py#L138-L162)。注意 `get_max_resource_count` 的文档串「the maximum number of real requests this manager can admit」——admit（准入）这个词直接点明它服务于调度器的准入控制。

那「跑完一个请求需要多少资源」具体怎么算？以 KV Cache 为例（u7-l1 讲过分页），就是把 prompt + max_new_tokens 的总 token 数换算成 block 数：

```python
# KVCacheManager.get_needed_resource_to_completion
def get_needed_resource_to_completion(self, request: LlmRequest) -> int:
    context_token_count = request.orig_prompt_len
    num_context_blocks = context_token_count // self.tokens_per_block
    remaining_tokens = context_token_count + request.max_new_tokens \
                       - num_context_blocks * self.tokens_per_block
    need_blocks = num_context_blocks + math.ceil(remaining_tokens / self.tokens_per_block)
    return need_blocks
```

- 容量计算：[tensorrt_llm/_torch/pyexecutor/resource_manager.py:740-753](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/resource_manager.py#L740-L753)。源码里的 TODO 注释很重要：C++ 版的同名方法「扣除已分配的 block」，Python 版「包含已分配的 block」，两边语义不一致，所以这里暂时用 Python 自己的实现而没调 C++。

不同资源对同一组接口的差异化实现，恰好体现「契约统一、语义各异」：

- KV Cache 的 `get_needed_resource_to_completion` 返回**需要的 block 数**：[resource_manager.py:740-753](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/resource_manager.py#L740-L753)。
- 序列槽的同一方法直接返回 `1`（每个请求恒占一个槽）：[seq_slot_manager.py:14-15](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/seq_slot_manager.py#L14-L15)。
- 压缩管理器明确返回 `0`，表示「我不拥有物理资源、别因为我卡调度」：[resource_manager.py:2520-2530](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/resource_manager.py#L2520-L2530)。

#### 4.1.4 代码实践

**实践目标**：亲手确认「容量接口是抽象方法、生命周期接口有默认实现」这一契约设计。

**操作步骤**：

1. 打开 `tensorrt_llm/_torch/pyexecutor/resource_manager.py`，定位 `class BaseResourceManager(ABC)`。
2. 数一数哪些方法带 `@abstractmethod`，哪些没带。
3. 用编辑器跳转（或 `grep`）找出 `class .* (BaseResourceManager)` 的所有子类，列一张表。

**需要观察的现象**：

- 只有 `get_max_resource_count` 和 `get_needed_resource_to_completion` 带 `@abstractmethod`。
- 其余五个方法（`add_dummy_requests` / `prepare_resources` / `update_resources` / `free_resources` / `shutdown`）都没有，方法体只是 `pass`。

**预期结果**：你应该能找到至少 5 个直接子类——`KVCacheManager`、`KVCacheManagerV2`（V2 实现）、`SeqSlotManager`、`PeftCacheManager`、`BaseKVCacheCompressionManager`。（精确清单见 4.2.3。）

> 本实践为源码阅读型，不需要 GPU，不运行命令。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `prepare_resources` 等生命周期方法**不**设成 `@abstractmethod`，而容量接口要设成？

**参考答案**：并非每种资源都需要在每个阶段做事。例如 PEFT 缓存的 `update_resources` 是空操作（权重表在 prepare 阶段一次建好），压缩管理器干脆不参与调度（容量返回 0）。把它们设成抽象方法会强迫每个子类写一堆空实现。而容量接口是调度器做准入控制**必须**知道的信息，没有默认值可言，因此设成抽象方法、强制子类表态。

**练习 2**：`KVCacheManager.get_needed_resource_to_completion` 为什么不直接调用 C++ 的同名实现？（提示：看源码里的 TODO 注释。）

**参考答案**：Python 调度器与 C++ 调度器对「needed resource to completion」的口径不一致——C++ 版扣除已分配的 block，Python 版包含已分配的 block。在两边统一之前，强行调 C++ 会得到与 Python 调度器预期不符的数字，所以暂时保留 Python 实现。

---

### 4.2 ResourceManager：资源容器与扇出（fan-out）

#### 4.2.1 概念说明

有了统一的 `BaseResourceManager` 契约，下一个问题：`PyExecutor` 手里攥着好几个资源管理器，怎么管？答案是再套一层——`ResourceManager`（注意首字母大写，和 `BaseResourceManager` 区分）。

`ResourceManager` 本身**不实现任何资源逻辑**，它只是一个**有序容器**：内部用一个 `OrderedDict` 把「资源类型 → 该类型的资源管理器实例」存起来，然后提供三个扇出方法，把一次调用广播给所有注册的资源。对 `PyExecutor` 而言，它面对的始终是这**一个** `ResourceManager`，不用关心里面装了几个、装了什么。

这个设计的好处是**开放-封闭**：加一种新资源，只要造一个继承 `BaseResourceManager` 的类、注册进容器，主循环一行都不用改。资源类型用一个枚举 `ResourceManagerType` 标识，避免用字符串当 key 出现拼写错误。

#### 4.2.2 核心流程

容器扇出的关键约定（来自源码）：

- `prepare_resources`：**按注册顺序**遍历所有资源管理器，依次调它们的 `prepare_resources`。顺序很重要——例如 KV Cache 必须先于依赖它的资源分配。
- `update_resources`：同样按顺序，但**特判** `KV_CACHE_MANAGER` 这一种类型，给它多传 `attn_metadata` 和 `kv_cache_dtype_byte_size` 两个参数（KV Cache 回写时需要注意力元数据来判断哪些草稿 token 被接受/拒绝）。
- `free_resources`：**逆序**遍历（`reversed`），先释放后注册的资源，符合「后申请者先释放」的资源回收直觉。
- `reorder_pipeline(list)`：允许把资源管理器的执行顺序重排（用于投机解码等需要把 draft KV 调到 target KV 之前的场景）。

扇出循环的伪代码：

```
prepare_resources(batch):
    for rm in resource_managers.values():      # 正序
        rm.prepare_resources(batch)

update_resources(batch, attn_md, dtype_bytes):
    for type, rm in resource_managers.items():  # 正序
        if type == KV_CACHE_MANAGER:
            rm.update_resources(batch, attn_md, dtype_bytes)   # 多传两个参数
        else:
            rm.update_resources(batch)

free_resources(request):
    for rm in reversed(resource_managers.values()):  # 逆序
        rm.free_resources(request)
```

#### 4.2.3 源码精读

枚举 `ResourceManagerType` 列出了系统已知的资源种类：

```python
# resource_manager.py
class ResourceManagerType(enum.Enum):
    KV_CACHE_MANAGER = "KV_CACHE_MANAGER"
    DRAFT_KV_CACHE_MANAGER = "DRAFT_KV_CACHE_MANAGER"
    CROSS_KV_CACHE_MANAGER = "CROSS_KV_CACHE_MANAGER"
    PEFT_CACHE_MANAGER = "PEFT_CACHE_MANAGER"
    SEQ_SLOT_MANAGER = "SEQ_SLOT_MANAGER"
    SPEC_RESOURCE_MANAGER = "SPEC_RESOURCE_MANAGER"
    KV_CACHE_COMPRESSION_MANAGER = "KV_CACHE_COMPRESSION_MANAGER"
```

- 资源类型枚举：[tensorrt_llm/_torch/pyexecutor/resource_manager.py:94-101](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/resource_manager.py#L94-L101)。包含 self KV、draft KV（草稿模型）、cross KV（编码器-解码器的编码器 KV）、PEFT 缓存、序列槽、投机解码资源、KV 压缩共 7 类。

容器类本体（构造、注册、扇出）：

```python
# resource_manager.py
class ResourceManager:

    def __init__(self, resource_managers: dict[ResourceManagerType, BaseResourceManager]):
        self.resource_managers = OrderedDict(resource_managers)

    def __call__(self, type: ResourceManagerType):
        return self.resource_managers[type]

    @nvtx_range("prepare_resources")
    def prepare_resources(self, scheduled_batch: ScheduledRequests):
        for _, resource_manager in self.resource_managers.items():
            if hasattr(resource_manager, "prepare_resources"):
                resource_manager.prepare_resources(scheduled_batch)

    def free_resources(self, request: LlmRequest):
        for resource_type, resource_manager in reversed(self.resource_managers.items()):
            if hasattr(resource_manager, "free_resources"):
                resource_manager.free_resources(request)
```

- 容器类与扇出：[tensorrt_llm/_torch/pyexecutor/resource_manager.py:2570-2619](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/resource_manager.py#L2570-L2619)。`__call__` 让你可以用 `resource_manager(ResourceManagerType.KV_CACHE_MANAGER)` 这样「调用」语法取回某个具体资源管理器；`@nvtx_range` 给 Nsight Systems 打时间轴标记，方便性能分析。

`update_resources` 里对 KV_CACHE_MANAGER 的特判：

```python
# resource_manager.py:2593-2607（update_resources 片段）
for resource_type, resource_manager in self.resource_managers.items():
    if hasattr(resource_manager, "update_resources"):
        if resource_type == ResourceManagerType.KV_CACHE_MANAGER:
            resource_manager.update_resources(scheduled_batch,
                                              attn_metadata,
                                              kv_cache_dtype_byte_size)
        else:
            resource_manager.update_resources(scheduled_batch)
```

- KV_CACHE_MANAGER 的参数特判：[tensorrt_llm/_torch/pyexecutor/resource_manager.py:2593-2607](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/resource_manager.py#L2593-L2607)。这是容器为「KV Cache 回写需要额外信息」开的口子。

那么这个容器在 `PyExecutor` 单步循环里到底在哪被调？三处典型调用点：

- `self.resource_manager.prepare_resources(scheduled_batch)`：[py_executor.py:2628](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L2628) 与重叠调度路径 [py_executor.py:4055](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L4055)。
- `self.resource_manager.update_resources(...)`：[py_executor.py:3152](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L3152) 与 [py_executor.py:4187](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L4187)。
- 请求终止时的 `free_resources`：[py_executor.py:444-447](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L444-L447)。

**哪些类继承了 `BaseResourceManager`**（本讲实践题要求的清单）：

| 子类 | 文件:行 | 管的资源 | 容量接口返回 |
|------|---------|---------|-------------|
| `KVCacheManager`（V1） | resource_manager.py:266 | self KV Cache 分页池 | 需要的 block 数 |
| `KVCacheManagerV2` | kv_cache_manager_v2.py:740 | V2 分页池（多级缓存） | **桩**：`get_max_resource_count` 返回 1、`get_needed_resource_to_completion` 是 TODO 空实现 |
| `SeqSlotManager` | seq_slot_manager.py:6 | 序列槽（请求编号→槽位映射） | 恒为 1 |
| `PeftCacheManager` | resource_manager.py:2622 | host/device 两级 LoRA 权重 | 0（不卡调度） |
| `BaseKVCacheCompressionManager` | resource_manager.py:2418 | KV Cache 压缩（逐层驱逐/重写） | 0（不拥有物理资源） |

V2 的容量接口目前是桩，可佐证 u7-l1 提到的「V2 的容量接口目前为桩」：[kv_cache_manager_v2.py:3318-3329](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/kv_cache_manager_v2.py#L3318-L3329)。

#### 4.2.4 代码实践

**实践目标**：在源码中枚举所有 `BaseResourceManager` 子类，验证容器扇出对它们一视同仁。

**操作步骤**：

1. 在仓库根目录运行（只读检索，不修改任何源码）：

   ```bash
   grep -rn "BaseResourceManager)" tensorrt_llm/_torch/pyexecutor/ \
     | grep -E "class .*\(.*BaseResourceManager"
   ```

2. 对找到的每个子类，打开它对应的 `get_needed_resource_to_completion`，记录返回值。
3. 打开 `ResourceManager.update_resources`（resource_manager.py:2593），确认只有 `KV_CACHE_MANAGER` 这一种类型被特判多传了参数。

**需要观察的现象**：

- 命中的子类与 4.2.3 表格一致。
- 不同子类对同一接口返回截然不同的值（block 数 / 1 / 0）。
- 扇出循环用一个 `if resource_type == KV_CACHE_MANAGER` 完成了对所有资源的差异化传参。

**预期结果**：你会直观看到「契约统一、实现各异、容器扇出」三件事如何在同一段代码里成立。

> 本实践为源码阅读型，可在无 GPU 环境执行 `grep`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `free_resources` 要**逆序**遍历，而 `prepare_resources` 要**正序**？

**参考答案**：资源之间有依赖，通常「先分配的资源被后分配的资源依赖」（如 KV Cache 先建好，投机解码资源才好挂上去）。释放时遵循栈式「后进先出」更安全：先释放依赖方，再释放被依赖方，避免悬挂引用。`prepare` 正序、`free` 逆序正是这层依赖顺序的镜像。

**练习 2**：如果你要新增一种「请求级计数器」资源（每个请求维护一个 int 计数），最少要写哪些方法？

**参考答案**：必须实现两个抽象容量接口（`get_max_resource_count`、`get_needed_resource_to_completion`，哪怕分别返回一个大数和 0），其余三段式方法按需覆盖：在 `prepare_resources` 里初始化计数、`free_resources` 里清理。然后构造一个 `ResourceManagerType` 枚举项，把实例注册进 `ResourceManager` 即可——`PyExecutor` 主循环无需改动。

---

### 4.3 KV Cache Connector：可插拔的远程缓存访问接口

#### 4.3.1 概念说明

到目前为止，KV Cache 都被当作「本机显存里的一池 block」。但在很多场景下，KV Cache 需要被**远程**访问：

- **分离式服务**：prefill 节点算完 KV，要送到 decode 节点。
- **KV 卸载/回载（offload/onboard）**：显存放不下，把冷 KV 挪到 host 内存或远端。
- **KV 共享**：多个副本共用同一份前缀 KV。
- **P2P 传输**：GPU 之间直接搬 KV。

TensorRT-LLM 借鉴了 vLLM 的设计，把这些需求抽象成一套**可插拔的 Connector API**。它把职责**一分为二**：

- **Scheduler（编排者）**：只在一个 rank（leader，rank 0）上运行，负责「决定搬哪些 block、去哪搬」，产出一份元数据（metadata）。
- **Worker（执行者）**：每个 rank 都有一个，根据 scheduler 给的 metadata，真正发起/等待数据传输。

为什么要分两层？因为「决策」只需做一次（且要全局一致），而「搬运」每个 rank 都得各自做。用 leader 做决策、广播给所有 worker，既避免重复决策、又保证一致。这跟 u2-l3 讲过的「接口在 Python、实现可选 C++」同源，只是这里切的是「编排 vs 执行」。

Connector 是**可插拔**的：具体的 scheduler/worker 实现由外部包提供（如 `lmcache`、`kvbm`），框架只定义接口与把它们粘起来的「管家」`KvCacheConnectorManager`。

#### 4.3.2 核心流程

Connector 的生命周期围绕「**异步保存/加载**」展开。一次典型的远程 KV 访问分两个方向：

**加载方向（远端 → 本机）**，发生在请求**进入 context 前**：

```
1. scheduler.get_num_new_matched_tokens(req, num_computed)
   → 返回「能从远端加载多少 token」、以及「是否异步加载」
2. 若异步加载：请求状态置为 DISAGG_GENERATION_TRANS_IN_PROGRESS，暂时移出调度
3. KVCacheManager 给请求分配本地 block_id
4. scheduler.update_state_after_alloc(req, block_ids)   ← 把 block_id 交给 scheduler
5. build_scheduler_output(...)   ← 汇总本步所有请求的数据（token、block_id、hash）
6. scheduler.build_connector_meta(scheduler_output)     ← leader 生成 metadata
7. worker.bind_connector_meta(metadata)                 ← 广播给每个 worker
8. worker.start_load_kv(stream)                         ← 每个 worker 开始拉数据
   每层前向后：worker.wait_for_layer_load(layer_idx, stream)
```

**保存方向（本机 → 远端）**，发生在请求**生成结束后**：

```
1. scheduler.request_finished(req, cache_block_ids)
   → 返回「是否异步保存」；若异步，请求状态置为 DISAGG_CONTEXT_TRANS_IN_PROGRESS
   （此时不能立刻 free KV，要等保存完成）
2. 每层前向后：worker.save_kv_layer(layer_idx, stream)
3. 前向结束：worker.wait_for_save(stream)
4. worker.get_finished(...)  → 返回「哪些请求已保存/加载完成」
   （只有所有 worker 都报告完成的请求，才算真完成）
```

异步状态机有四个桶（`AsyncRequests`）：

- `new_async_requests`：刚发起、还没问过 `get_finished`。
- `pending_async_requests`：问过 `get_finished`、但本 rank 还没完成。
- `local_finished_async_requests`：本 rank 完成、但还没被所有 rank 确认。
- `finished_async_loading_requests`：加载完成、等待重新加入调度的请求。

「真完成」的判定用集合**交集**：`get_finished` 会把所有 rank 的结果 `mpi_allgather` 上来，只有**所有 worker 都报告完成**的请求 id 才进入交集，才会触发后续动作。这保证了一个请求不会因为「某个 worker 慢」而被提前释放。

#### 4.3.3 源码精读

模块顶部把 Connector 的职责讲得很清楚：

> The KV Cache Connector … It is responsible for:
> - Orchestrating the loading and saving of KV cache blocks.
> - Managing asynchronous block tx/rx.

- 模块文档与职责说明：[connectors/kv_cache_connector.py:15-35](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/connectors/kv_cache_connector.py#L15-L35)。

**Worker 接口**（执行者）核心抽象方法：

```python
# kv_cache_connector.py
class KvCacheConnectorWorker(ABC):
    @abstractmethod
    def register_kv_caches(self, kv_cache_tensor: torch.Tensor): ...   # 注册 KV 张量（如 NIXL 注册）
    @abstractmethod
    def start_load_kv(self, stream): ...                               # 开始加载
    @abstractmethod
    def wait_for_layer_load(self, layer_idx, stream): ...              # 等某层加载完
    @abstractmethod
    def save_kv_layer(self, layer_idx, stream): ...                    # 保存某层 KV
    @abstractmethod
    def wait_for_save(self, stream): ...                               # 等所有保存完
    @abstractmethod
    def get_finished(self, finished_gen_req_ids, started_loading_req_ids) -> Tuple[...]: ...
```

- Worker ABC：[connectors/kv_cache_connector.py:101-196](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/connectors/kv_cache_connector.py#L101-L196)。注意 `register_forward_pass_callable` 的注释：它让 connector 在前向 stream 末尾插一个 CUDA event，作为「可以开始卸载 KV」的信号——这是 KV 卸载能和前向重叠的关键。

**Scheduler 接口**（编排者）核心抽象方法：

```python
# kv_cache_connector.py
class KvCacheConnectorScheduler(ABC):
    @abstractmethod
    def build_connector_meta(self, scheduler_output): ...              # 生成给 worker 的 metadata
    @abstractmethod
    def get_num_new_matched_tokens(self, request, num_computed_tokens) -> Tuple[int, bool]: ...
    @abstractmethod
    def request_finished(self, request, cache_block_ids) -> bool: ...  # 是否异步保存
    @abstractmethod
    def update_state_after_alloc(self, request, block_ids): ...        # 回传 block_id
```

- Scheduler ABC：[connectors/kv_cache_connector.py:199-263](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/connectors/kv_cache_connector.py#L199-L263)。

**`KvCacheConnectorManager`**——把 scheduler 和 worker 粘起来的「管家」，文档明说它是实现细节、不属于 connector 接口本身：

```python
# kv_cache_connector.py
class KvCacheConnectorManager(KvCacheConnectorManagerCpp):
    """
    1. Managing the state of async requests (both offload and onboard)
    2. Handling MPI communication. We only run the leader on one rank,
       but need the results of the leader API on all ranks.
    Note: This class is solely an implementation detail …
    """
    def __init__(self, worker, scheduler):
        assert (scheduler is not None) == (mpi_rank() == 0), "The scheduler may only exist at rank 0!"
```

- 管家类与 leader 约束：[connectors/kv_cache_connector.py:415-453](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/connectors/kv_cache_connector.py#L415-L453)。断言 `(scheduler is not None) == (mpi_rank() == 0)` 强制 scheduler 只在 rank 0 存在。

leader 模式的核心模式——`_run_on_leader`：在 rank 0 跑函数、把结果广播给所有 rank：

```python
# kv_cache_connector.py
def _run_on_leader(self, f: Callable[[], Any]) -> Any:
    if self.scheduler is not None:
        assert mpi_rank() == 0
        res = f()
    else:
        res = None
    return mpi_broadcast(res, root=0)
```

- leader 广播：[connectors/kv_cache_connector.py:455-464](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/connectors/kv_cache_connector.py#L455-L464)。

`get_finished` 用「所有 worker 交集」判定真完成（这是异步状态机收敛的关键）：

```python
# kv_cache_connector.py（get_finished 片段）
(finished_saving, finished_loading) = self.worker.get_finished(finished_gen_req_ids, started_loading_req_ids)
...
all_results = mpi_allgather((finished_saving, finished_loading))
intersect_finished_saving = set.intersection(*[set(res[0]) for res in all_results])
intersect_finished_loading = set.intersection(*[set(res[1]) for res in all_results])
```

- 交集判定完成：[connectors/kv_cache_connector.py:571-621](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/connectors/kv_cache_connector.py#L571-L621)。

**Connector 预设注册表**——把短名映射到「外部模块 + scheduler 类 + worker 类」：

```python
# registry.py
CONNECTOR_REGISTRY = {
    "lmcache":    {"connector_module": "lmcache.integration.tensorrt_llm.tensorrt_adapter", ...},
    "lmcache-mp": {"connector_module": "lmcache.integration.tensorrt_llm.tensorrt_mp_adapter", ...},
    "kvbm":       {"connector_module": "kvbm.trtllm_integration.connector", ...},
}
```

- 注册表：[connectors/registry.py:22-38](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/connectors/registry.py#L22-L38)。注释强调：这里**不导入** connector 模块，运行时才用 `importlib` 解析，避免把可选依赖（lmcache/kvbm）变成硬依赖。

装配车间 `py_executor_creator.py` 用 `importlib` 动态加载、并在线程池里**并发**实例化 worker 与 scheduler（因为二者可能互相依赖）：

```python
# py_executor_creator.py
module = importlib.import_module(kv_connector_config.connector_module)
worker_cls = getattr(module, kv_connector_config.connector_worker_class)
scheduler_cls = getattr(module, kv_connector_config.connector_scheduler_class)
with ThreadPoolExecutor(max_workers=2) as executor:
    connector_worker_task = executor.submit(worker_cls, llm_args)
    if scheduler_cls is not None and rank == 0:
        connector_scheduler_task = executor.submit(scheduler_cls, llm_args)
        connector_scheduler = connector_scheduler_task.result()
    else:
        connector_scheduler = None
    connector_worker = connector_worker_task.result()
...
kv_connector_manager = KvCacheConnectorManager(connector_worker, connector_scheduler)
```

- 动态导入与并发实例化：[py_executor_creator.py:857-888](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor_creator.py#L857-L888)。

**Connector 如何接入 `KVCacheManager` 的生命周期**：`KVCacheManager.__init__` 收一个 `kv_connector_manager` 参数（[resource_manager.py:327](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/resource_manager.py#L327)），并把它透传给 C++ KVCacheManager（[resource_manager.py:614](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/resource_manager.py#L614)）。在 `prepare_resources` 末尾，若 connector 存在，就调它的 `build_scheduler_output`：

```python
# KVCacheManager.prepare_resources 结尾
if self.kv_connector_manager is not None:
    self.kv_connector_manager.build_scheduler_output(scheduled_batch, self)
```

- prepare_resources 内对 connector 的回调：[resource_manager.py:838-840](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/resource_manager.py#L838-L840)。而在分配新 sequence 时也会回调 `update_state_after_alloc`：[resource_manager.py:808-811](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/resource_manager.py#L808-L811)。

最后，`PyExecutor` 在单步循环里驱动 connector 的三个回调：

```python
# py_executor.py
def _kv_connector_start_batch(self, scheduled_batch):
    if self.kv_connector_manager:
        self.kv_connector_manager.take_scheduled_requests_pending_load(scheduled_batch)
        self.kv_connector_manager.handle_metadata()                          # build_connector_meta + bind
        self.kv_connector_manager.worker.start_load_kv(torch.cuda.current_stream())

def _kv_connector_terminate_requests(self):
    if self.kv_connector_manager:
        reqs_to_terminate = self.kv_connector_manager.get_finished()         # 交集判定
        for req in reqs_to_terminate:
            self._end_transfer_and_maybe_terminate(req)
```

- 单步循环里的 connector 驱动：[py_executor.py:3733-3745](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L3733-L3745)。`handle_metadata` 即 leader 生成 metadata 后广播给 worker；`get_finished` 收割异步完成的请求。

#### 4.3.4 代码实践

**实践目标**：在无 GPU 环境下，理清 Connector 从「配置名」到「被 PyExecutor 调用」的完整装配链。

**操作步骤**：

1. 打开 `connectors/registry.py`，记下 `lmcache` 预设对应的三个字段（`connector_module` / `connector_worker_class` / `connector_scheduler_class`）。
2. 在 `py_executor_creator.py` 搜索 `importlib.import_module(kv_connector_config.connector_module)`，确认它用 registry 里的模块路径动态加载 worker/scheduler 类。
3. 在 `py_executor.py` 搜索 `self.kv_connector_manager.`，把所有调用点列出来，标注它们发生在单步循环的哪个阶段（批开始 / 每层前后 / 批结束 / 请求终止）。
4. 在 `KVCacheManager.prepare_resources`（resource_manager.py:780）里找到对 `self.kv_connector_manager` 的两处调用，说明它们为什么必须发生在「分配 block 之后」。

**需要观察的现象**：

- registry 只存「类名字符串」，不导入任何外部包。
- `KvCacheConnectorManager` 的方法在 `PyExecutor` 里出现在固定的 4 个时机。
- `update_state_after_alloc` 和 `build_scheduler_output` 都在 `KVCacheManager.prepare_resources` 内、且在 `add_sequence_batch` 之后调用——因为只有先分配出本地 block_id，scheduler 才知道「要把远端 KV 搬到哪些 block」。

**预期结果**：你能画出一张「registry 名 → 动态导入 → KvCacheConnectorManager → PyExecutor 4 个回调时机」的装配时序。

> 本实践为源码阅读型，不运行命令。

#### 4.3.5 小练习与答案

**练习 1**：为什么 connector 要把 scheduler 和 worker 分成两个对象，而不是合并成一个？

**参考答案**：scheduler 做「决策」，只需要在 leader（rank 0）上跑一份；worker 做「搬运」，每个 rank 都需要一份。分开后，决策只需做一次再广播，既省算力又保证全局一致。合并的话要么每个 rank 重复决策（可能不一致），要么要在对象内部再分 leader/worker 角色，徒增复杂度。

**练习 2**：`get_finished` 为什么要用「所有 rank 的交集」而不是「任意 rank 报告完成」？

**参考答案**：KV 搬运是跨 rank 的协作——一个请求的 KV 可能在不同 rank 上分布、且各 rank 完成时间不同。只有**所有** worker 都确认「这个请求的保存/加载完成」，才能安全释放本机 KV 或重新调度该请求。用「任意一个」会因某个慢 rank 还没搬完就提前释放，导致数据损坏或读取悬挂。

---

### 4.4 Connector vs Transceiver：两条 KV 搬运路径的区别与配合

#### 4.4.1 概念说明

这是本讲最容易混淆、也最值得讲清楚的一点：TensorRT-LLM 里有**两个**名字相近、都和「搬 KV」有关、但完全不同的组件。

| 维度 | **KV Cache Connector** | **KV Cache Transceiver** |
|------|------------------------|--------------------------|
| 文件 | `connectors/kv_cache_connector.py` | `kv_cache_transceiver.py` |
| 抽象 | worker/scheduler 双 ABC + `KvCacheConnectorManager` | `KvCacheTransceiver` ABC |
| 设计来源 | 借鉴 vLLM 的可插拔 connector API | TRT-LLM 原生分离式传输 |
| 主要场景 | KV 卸载/回载、KV 共享、P2P、第三方存储（lmcache/kvbm） | **分离式服务**（prefill→decode 跨节点搬 KV） |
| 是否 BaseResourceManager | 否（它是 `KVCacheManager` 内部持有的一块状态） | 否（它是 `PyExecutor` 上的独立对象 `self.kv_cache_transceiver`） |
| 传输后端 | 由具体 worker 实现决定（如 lmcache） | NIXL（默认）/ UCX / MPI / Mooncake |
| 接入方式 | 在 `KVCacheManager.prepare_resources` 里被回调 | 在 `PyExecutor` 单步循环的 context/响应阶段被直接调用 |

一句话记忆：**Connector 是「可插拔的缓存访问插件」，Transceiver 是「分离式服务的专用运输车」。**

它们也能配合：分离式服务的 decode 节点收到从 prefill 节点经 Transceiver 搬来的 KV 后，若还接了 Connector（比如 lmcache 做二级缓存），Connector 可以继续把这些 KV 卸载到远端存储。但二者各管一段、互不替代。

#### 4.4.2 核心流程

**Transceiver 的工厂选择**（`create_kv_cache_transceiver`）按两个维度路由：

1. **传输后端**（`cache_transceiver_config.backend`）：`DEFAULT` → 由环境变量解析（NIXL 为非混合模型的默认）；`MPI`（已废弃）/ `UCX`（推荐用于跨域）/ `NIXL` / `MOONCAKE`。
2. **运行时**（`transceiver_runtime`）：`None` 或 `"CPP"` → 用 C++ 绑定的 `BindKvCacheTransceiver`（默认、推荐）；`"PYTHON"` → 用纯 Python 的 `KvCacheTransceiverV2`（仅支持 NIXL/DEFAULT）。

分离式 prefill→decode 的简化时序：

```
【prefill 节点（context 侧）】
  请求生成结束
    transceiver.respond_and_send_async(req)     # 把本请求的 KV 异步发给 decode 节点
    → 请求状态进入「异步保存」，暂不释放 KV
    get_finished() 确认所有 rank 发送完毕 → 才 free

【decode 节点（generation 侧）】
  收到请求
    transceiver.request_and_receive_async(req)  # 异步接收 KV 到本地 block
    或 request_and_receive_sync(req)            # 同步接收
    check_context_transfer_status(...)          # 轮询接收是否完成
    接收完成后，请求进入正常 generation
```

**运行时选择的一个关键护栏**：`MixedMambaHybridCacheManager`（混合线性注意力模型）**必须**用 Python transceiver，否则报错——因为 C++ transceiver 不支持这种混合缓存管理器的搬运语义。

**inflight 取消**（实验特性）：当 `TRTLLM_DISAGG_ENABLE_INFLIGHT_CANCEL=1` 且配置满足一组严格条件（C++ 运行时 + NIXL + UCX 子后端 + 有限超时 + 非分层 + 非零拷贝）时，transceiver 才支持「传输途中取消」，用于客户端断开时及时回收资源。

#### 4.4.3 源码精读

`KvCacheTransceiver` 抽象基类，规定了分离式传输的全部接口：

```python
# kv_cache_transceiver.py
class KvCacheTransceiver(ABC):
    @abstractmethod
    def respond_and_send_async(self, req): ...       # prefill 侧：算完即异步发出
    @abstractmethod
    def request_and_receive_sync(self, req): ...     # decode 侧：同步接收
    @abstractmethod
    def request_and_receive_async(self, req): ...    # decode 侧：异步接收
    @abstractmethod
    def check_context_transfer_status(self, n): ...  # 轮询接收进度
    @abstractmethod
    def check_gen_transfer_status(self, n): ...
    @abstractmethod
    def check_gen_transfer_complete(self): ...
    @abstractmethod
    def cancel_request(self, req): ...
    @abstractmethod
    def prepare_context_requests(self, requests): ...  # generation-first 模式准备 context
    @abstractmethod
    def get_disaggregated_params(self) -> Dict: ...
    def supports_inflight_request_cancellation(self) -> bool: return False
```

- Transceiver ABC：[kv_cache_transceiver.py:183-245](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/kv_cache_transceiver.py#L183-L245)。注意 `respond_and_send_async`（prefill 侧发送）与 `request_and_receive_*`（decode 侧接收）的命名对称——这正是分离式服务「一端发、一端收」的镜像。

工厂 `create_kv_cache_transceiver` 的路由逻辑（核心分支）：

```python
# kv_cache_transceiver.py
if cache_transceiver_config.transceiver_runtime == "PYTHON":
    if cache_transceiver_config.backend not in ("DEFAULT", "NIXL"):
        raise ValueError("Python transceiver currently only supports NIXL or DEFAULT backend ...")
    from tensorrt_llm._torch.disaggregation.transceiver import KvCacheTransceiverV2
    return KvCacheTransceiverV2(mapping, dist, kv_cache_manager, cache_transceiver_config)

# Default: use C++ transceiver
return BindKvCacheTransceiver(mapping, dist, kv_cache_manager,
                              attention_type, cache_transceiver_config, mamba_cache_manager)
```

- 工厂与运行时选择：[kv_cache_transceiver.py:113-180](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/kv_cache_transceiver.py#L113-L180)。`"auto"` 会被先置为 `None`（注释指出 auto 正常在配置加载期对齐模型偏好，跳过该步的路径如 AutoDeploy 退回 C++）。

`BindKvCacheTransceiver` 是默认实现——一个薄薄的 Python 包装，真正干活的是 C++ `CacheTransceiver`：

```python
# kv_cache_transceiver.py
self.impl = CacheTransceiverCpp(kv_cache_manager.impl,
                                total_num_kv_heads_per_layer, head_dim,
                                tokens_per_block, world_config,
                                pp_layer_num_per_pp_rank, dtype,
                                attention_type,
                                cache_transceiver_config._to_pybind(),
                                rnn_layer_num_per_pp_rank)
```

- C++ 绑定构造：[kv_cache_transceiver.py:301-307](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/kv_cache_transceiver.py#L301-L307)。注意它直接读 `kv_cache_manager.impl`（C++ KVCacheManager）和 `kv_cache_manager.dtype/head_dim/...`——transceiver 与 KVCacheManager 是**紧耦合**的，它要知道每一层的 KV 头数、head_dim、block 大小才能正确切分传输缓冲。这也是为什么 transceiver 不是 `BaseResourceManager`：它不「管理资源」，而是「在 KVCacheManager 已有资源上做搬运」。

**Transceiver 在 `PyExecutor` 里的直接调用**（区别于 connector 经由 `prepare_resources` 间接驱动）：

- prefill 侧发送 / decode 侧接收的轮询：见 [py_executor.py:2439](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L2439) 起、以及 [py_executor.py:2496](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L2496)、[py_executor.py:2548-2621](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L2548-L2621)、[py_executor.py:3135-3158](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L3135-L3158)。
- inflight 取消与 poisoned buffer 检查：[py_executor.py:3896-3900](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L3896-L3900)。

#### 4.4.4 代码实践

**实践目标**：用一句话+一张时序图说清「分离式服务中，connector 与 transceiver 各自搬运哪一段 KV」。这是本讲实践任务的后半部分。

**操作步骤**：

1. 在 `kv_cache_transceiver.py` 里找到 `create_kv_cache_transceiver`，确认：当 `cache_transceiver_config.backend is None` 时返回 `None`（即非分离式部署时根本没有 transceiver）。定位行号。

2. 在 `py_executor.py` 里分别搜索 `self.kv_cache_transceiver` 和 `self.kv_connector_manager`，统计二者的出现次数和调用阶段，填入下表：

   | 组件 | 调用阶段（批开始/每层/批结束/请求终止） | 触发的方法 |
   |------|----------------------------------------|-----------|
   | transceiver | ? | `respond_and_send_async` / `request_and_receive_*` / `check_*_transfer_status` … |
   | connector manager | ? | `handle_metadata` / `get_finished` / `take_scheduled_requests_pending_load` … |

3. 画一张时序图：两个节点（prefill GPU、decode GPU），画出
   - prefill 节点用 **transceiver** `respond_and_send_async` 把 KV 发出；
   - decode 节点用 **transceiver** `request_and_receive_async` 接收；
   - （可选）decode 节点接收后，若装了 **connector**（如 lmcache），再由 connector 把 KV 卸载到远端存储。

**需要观察的现象**：

- 非分离式部署里，`kv_cache_transceiver` 为 `None`，整条 transceiver 路径被跳过；而 connector 可以独立存在（用于纯 KV 卸载）。
- transceiver 的调用散布在 context 接收、响应发送、终止等多个阶段；connector 的调用集中在批开始（`_kv_connector_start_batch`）和终止（`_kv_connector_terminate_requests`）。

**预期结果**：你能用一句话概括——**Transceiver 负责分离式 prefill→decode 的跨节点 KV 传输（基于 NIXL/UCX/MPI），Connector 负责本地的 KV 卸载/回载/共享/第三方存储（基于可插拔的 worker/scheduler）；二者在 decode 节点可以串联：transceiver 收进来、connector 再卸出去。**

> 本实践为源码阅读型，无 GPU 即可完成。

#### 4.4.5 小练习与答案

**练习 1**：一个用户既没配分离式服务、也没装 lmcache/kvbm，那么他的 `PyExecutor` 里同时存在 connector 和 transceiver 吗？

**参考答案**：都不存在。`kv_connector_config` 未配置时 `kv_connector_manager = None`（py_executor_creator.py:893-894）；`cache_transceiver_config.backend is None` 时 `create_kv_cache_transceiver` 返回 `None`（kv_cache_transceiver.py:120-122）。此时 `KVCacheManager` 的 connector 回调分支（`if self.kv_connector_manager is not None`）和 `PyExecutor` 里所有 `if self.kv_cache_transceiver` 分支都会被跳过——纯本地推理零开销。

**练习 2**：为什么 `BindKvCacheTransceiver` 要直接读 `kv_cache_manager.impl`（C++ KVCacheManager），而不是像 connector 那样只通过抽象接口交互？

**参考答案**：transceiver 要按层切分传输缓冲，必须知道每层的 KV 头数、head_dim、tokens_per_block、dtype 这些与物理 KV 布局强相关的细节，而这些最权威的来源就是 C++ KVCacheManager 本身。connector 的抽象层级更高（按 block_id 操作），不需要深入物理布局，因此可以保持松耦合。这是「紧耦合换性能/正确性、松耦合换可插拔性」的典型取舍。

---

## 5. 综合实践

把本讲三块知识串起来，完成一次「**给 PyExecutor 加一个最小资源管理器**」的设计练习（纸面设计，无需 GPU）。

**背景**：假设你要统计每个请求从被调度到生成第一个 token 的「等待步数」，做成一个随请求生灭的资源。

**任务**：

1. **契约实现**：写一个 `WaitStepCounter(BaseResourceManager)`，说明你会实现哪些方法、各自做什么。至少覆盖两个抽象容量接口（返回什么值合适？），以及 `prepare_resources`（在请求首次被调度时初始化计数）、`update_resources`（每步自增）、`free_resources`（清理）。

2. **容器接入**：在 `ResourceManagerType` 里新增一个枚举项（如 `WAIT_STEP_COUNTER = "WAIT_STEP_COUNTER"`），并在装配阶段（参考 py_executor_creator.py:976-981 处 `SPEC_RESOURCE_MANAGER` 的注册写法）把你的实例塞进 `resources` 字典。说明为什么这一步不用改 `PyExecutor` 主循环。

3. **与 connector/transceiver 共存**：解释你的计数器与（可能存在的）connector、transceiver 互不干扰的原因——提示：从「容器扇出对每个资源独立调用」「你的资源不碰 KV block」两点作答。

4. **验证设计**：写出你会用 `grep` 检索的命令，确认 `ResourceManager.update_resources`（resource_manager.py:2593）会自动把你的计数器纳入扇出，且不会给它误传 `attn_metadata`（因为只有 `KV_CACHE_MANAGER` 类型会被特判）。

**参考要点**：

- 容量接口：`get_max_resource_count` 可返回一个大数（如 `max_batch_size`），`get_needed_resource_to_completion` 返回 `1`（每个请求占一个计数槽，类似 `SeqSlotManager`）。
- 不改主循环：这正是容器模式的价值——`ResourceManager` 用 `OrderedDict` 扇出，新资源注册即生效。
- 共存：扇出循环对每个资源独立调用；特判只认 `KV_CACHE_MANAGER`，你的新类型走 `else` 分支只收 `scheduled_batch`。
- 检索命令：`grep -n "WAIT_STEP_COUNTER\|KV_CACHE_MANAGER" tensorrt_llm/_torch/pyexecutor/resource_manager.py`，确认你的类型不在特判分支里。

> 本综合实践为纸面设计 + 源码检索，全程只读、不修改任何源码。

## 6. 本讲小结

- **`BaseResourceManager`** 是资源管理的统一契约：两个**抽象**容量接口（`get_max_resource_count` / `get_needed_resource_to_completion`）给调度器做准入控制，五个**带默认实现**的三段式生命周期方法（`add_dummy_requests` / `prepare_resources` / `update_resources` / `free_resources` / `shutdown`）给 `PyExecutor` 单步循环驱动。
- **`ResourceManager`** 是一个**有序容器**，用 `OrderedDict[ResourceManagerType, BaseResourceManager]` 装多个异构资源，靠 `prepare`（正序）/ `update`（正序，特判 KV_CACHE_MANAGER 多传参）/ `free`（逆序）扇出，使主循环与具体资源解耦——加新资源不改主循环。
- 直接继承 `BaseResourceManager` 的子类至少有 5 个：`KVCacheManager`、`KVCacheManagerV2`（容量接口为桩）、`SeqSlotManager`（恒返回 1）、`PeftCacheManager`（返回 0）、`BaseKVCacheCompressionManager`（返回 0）。
- **KV Cache Connector** 是借鉴 vLLM 的可插拔远程缓存访问 API，**worker/scheduler 双 ABC** + 粘合层 `KvCacheConnectorManager`；scheduler 只在 rank 0 做「搬哪些 block」的决策，广播给每个 rank 的 worker 执行；异步完成用「所有 rank 交集」判定。
- **KV Cache Transceiver** 是分离式服务的**专用传输器**（NIXL/UCX/MPI/Mooncake，C++ 或 Python 运行时），与 KVCacheManager 紧耦合，负责 prefill→decode 跨节点的 KV 发送/接收。
- **易混点澄清**：Connector（可插拔缓存插件，经 `KVCacheManager.prepare_resources` 回调）≠ Transceiver（分离式运输车，由 `PyExecutor` 直接调用）；二者可在 decode 节点串联（transceiver 收进来、connector 再卸出去），非分离式部署时均可为 `None`、零开销跳过。

## 7. 下一步学习建议

- **调度细节**：本讲的容量接口（`get_max_resource_count` / `get_needed_resource_to_completion`）究竟如何被调度器消费，将在 **u8-l1（调度器与 inflight batching）** 展开——那里会讲 `CapacityScheduler` + `MicroBatchScheduler` 的两步调度，以及 `NoEvictScheduledBlocksManager` / `MaxUtilizationScheduledBlocksManager` 如何读 KVCacheManager 的容量统计。
- **请求状态机**：connector 的异步加载/保存如何改写请求状态（`DISAGG_GENERATION_TRANS_IN_PROGRESS` / `DISAGG_CONTEXT_TRANS_IN_PROGRESS`），将在 **u8-l2（请求生命周期与状态机）** 系统讲解。
- **分离式部署全景**：本讲只讲了 transceiver 这条「运输车」，分离式服务的协调器、router、NIXL 细节将在 **u11-l2（分离式服务）** 完整展开，届时你会看到 prefill/decode 节点如何经协调服务配对、KV 如何按 pool 解析 attention cache dtype 传输。
- **进阶源码**：若你对 connector 的具体实现感兴趣，可阅读外部包 `lmcache` 的 `tensorrt_adapter`（registry 里 `lmcache` 预设指向的模块）；对 Python transceiver 感兴趣可读 `tensorrt_llm/_torch/disaggregation/transceiver.py` 里的 `KvCacheTransceiverV2`。
