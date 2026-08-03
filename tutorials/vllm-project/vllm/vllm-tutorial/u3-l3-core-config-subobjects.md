# 核心子配置：Cache / Scheduler / Parallel

## 1. 本讲目标

在 [u3-l2](u3-l2-vllm-config-system.md) 中，我们知道了 vLLM 把所有配置打包进一个全局对象 `VllmConfig`，各层对象各取所需。但 `VllmConfig` 只是「容器」，真正决定「能装多少请求、占多少显存、用几张卡」的，是它里面的三块子配置：`CacheConfig`、`SchedulerConfig`、`ParallelConfig`。

本讲要回答一个直击性能的问题：**这三块子配置分别掌管什么？它们之间如何互相制约、共同决定了 vLLM 的吞吐与延迟？**

读完本讲，你应当能够：

- 说清 `CacheConfig` 如何用 `gpu_memory_utilization`、`block_size` 等字段控制 KV 缓存的显存预算与组织方式。
- 解释 `SchedulerConfig` 中的 `max_num_batched_tokens`、`max_num_seqs`、`enable_chunked_prefill` 等参数如何塑造每一步的批处理行为。
- 看懂 `ParallelConfig` 如何用 `tensor_parallel_size` / `pipeline_parallel_size` / `data_parallel_size` 声明并行拓扑，并理解它如何与执行器后端联动。
- 把三者串起来，说明 `gpu_memory_utilization`、`max_num_seqs`、`max_model_len` 之间的三角取舍关系。

## 2. 前置知识

- **子配置（sub-config）**：`VllmConfig` 聚合的十几块配置之一，每块用 `@config` 装饰成 Pydantic dataclass，构造时自动校验类型并禁止多余字段（`extra="forbid"`）。本讲只看其中三块。
- **KV 缓存 / block / 块表**：承接 u1-l1、u4。PagedAttention 把每个序列的 KV 缓存切成固定大小（`block_size` 个 token）的块，用块表把「逻辑序列位置」映射到「物理块」。`num_gpu_blocks` 就是一块 GPU 上能分出多少个这样的物理块。
- **prefill / decode / 连续批处理**：承接 u1-l1。prefill 是处理整段 prompt 的首步（计算密集、token 多）；decode 是逐个生成新 token（每步每序列 1 个 token）。连续批处理让每一步都能动态地把 prefill 和 decode 混在一起算。
- **TP / PP / DP**：张量并行（把单层权重切到多卡）、流水并行（把不同层放到不同卡、像流水线一样接力）、数据并行（每张卡跑一份完整模型副本，各自处理不同请求）。
- **`InitVar`**：Pydantic/dataclass 里的一种「只在构造时传入、用完不作为字段保留」的参数。`SchedulerConfig` 用它接收 `max_model_len` 之类的「外来值」用于校验，但并不把它存为自己的字段（因为这些值真正的归属是 `ModelConfig`）。

术语承接：本讲延续 u3-l1 的进程架构、u3-l2 的 `VllmConfig` / `ModelConfig` 概念。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [vllm/config/cache.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/cache.py) | 定义 `CacheConfig`：KV 缓存的块大小、显存利用率、缓存精度、前缀缓存开关、以及 profiling 后才填入的 `num_gpu_blocks`。 |
| [vllm/config/scheduler.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/scheduler.py) | 定义 `SchedulerConfig`：单步 token 上限、并发序列上限、分块预填充、调度策略、水位线等批处理参数。 |
| [vllm/config/parallel.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/parallel.py) | 定义 `ParallelConfig`：TP/PP/DP 规模、`world_size`、执行器后端选择、专家并行与 all2all 后端等分布式拓扑。 |
| [vllm/config/vllm.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/vllm.py) | `VllmConfig` 在此把三块子配置聚合为字段，并在 `__post_init__` 里做大量跨配置校验（async scheduling、cudagraph 等），是三者联动的「上层裁判」。 |
| [vllm/v1/worker/gpu_worker.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/worker/gpu_worker.py) | `determine_available_memory` 真正消费 `gpu_memory_utilization`，做 profiling、算出可用 KV 缓存字节数并回填 `num_gpu_blocks`。 |
| [vllm/v1/worker/utils.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/worker/utils.py) | `request_memory` 把 `gpu_memory_utilization × 总显存` 转成「vLLM 申请的字节数」，是显存预算的第一步换算。 |
| [vllm/engine/arg_utils.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/engine/arg_utils.py) | `EngineArgs` 把命令行参数（`--max-num-seqs`、`--gpu-memory-utilization` 等）翻译进这三块子配置，并按 `usage_context` 给默认值。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块推进：先看管「显存与 KV 缓存」的 `CacheConfig`，再看管「批处理节奏」的 `SchedulerConfig`，最后看管「并行拓扑」的 `ParallelConfig`。三者最后会在「综合实践」里汇成一个三角取舍。

### 4.1 CacheConfig：KV 缓存与显存预算

#### 4.1.1 概念说明

推理时，模型每处理一个 token，都要为它保存一份 Key/Value 向量（即 KV 缓存），供后续 token 做 attention 时复用。KV 缓存是显存大头：上下文越长、并发请求越多，KV 缓存占的显存越大。`CacheConfig` 就是专门管「这块显存怎么切、切多大、用什么精度存、要不要复用」的子配置。

它要回答几个核心问题：

1. **显存预算多少？** —— `gpu_memory_utilization` 声明 vLLM 这一个实例最多吃掉 GPU 总显存的多少比例（默认 0.92）。
2. **KV 缓存按多大块切？** —— `block_size` 决定一个物理块容纳多少个 token（默认 16）。
3. **KV 缓存用什么精度？** —— `cache_dtype`，默认 `"auto"`（跟模型权重同精度），也可选 `fp8` 等量化精度以省显存。
4. **相同前缀要不要复用？** —— `enable_prefix_caching`（默认开启）。
5. **到底能分出多少个块？** —— `num_gpu_blocks`，这个值**不在构造时确定**，而是引擎启动时做一次显存 profiling 之后才填入。

#### 4.1.2 核心流程

`CacheConfig` 在引擎启动中的关键节点：

1. **声明与构造**：作为 `VllmConfig` 的一个字段，带默认工厂 `Field(default_factory=CacheConfig)`；`block_size` 默认 `None`，由校验器在构造后补成 `DEFAULT_BLOCK_SIZE = 16`。
2. **显存预算换算**：worker 启动时，先把「总显存 × `gpu_memory_utilization`」算成「vLLM 申请使用的字节数」：

   \[ \text{requested\_memory} = \lceil \text{total\_memory} \times \text{gpu\_memory\_utilization} \rceil \]

3. **profiling**：worker 跑一次 dummy forward，测出模型权重 + 激活的峰值显存（`non_kv_cache_memory`）。剩下的才是 KV 缓存可用字节：

   \[ \text{available\_kv\_bytes} = \text{requested\_memory} - \text{non\_kv\_cache\_memory} \]

4. **回填 `num_gpu_blocks`**：把可用字节按「每块字节数」折算成块数，写回 `cache_config.num_gpu_blocks`。注意这是 `init=False` 字段——构造时不传、profiling 后才赋值。

   \[ \text{num\_gpu\_blocks} = \left\lfloor \frac{\text{available\_kv\_bytes}}{\text{block\_bytes}} \right\rfloor \]

5. **派生 token 容量**：`kv_cache_size_tokens = num_gpu_blocks × block_size`（混合注意力模型里按 KV cache group 感知计算，否则二者乘积就是总 token 容量）。

#### 4.1.3 源码精读

先看 `CacheConfig` 的类声明与几个最关键字段：

[CacheConfig 类声明与默认块大小](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/cache.py#L43-L54) —— `DEFAULT_BLOCK_SIZE` 是类级常量 16；`block_size` 字段默认 `None`（意为「用默认」），并有 `user_specified_block_size` 这个派生标志记录用户是否显式传过。

```python
# vllm/config/cache.py:47-49
DEFAULT_BLOCK_SIZE: ClassVar[int] = 16
block_size: int = Field(default=None, gt=0)  # None 表示用默认
```

[gpu_memory_utilization 字段](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/cache.py#L68-L75) —— 默认 0.92，范围 `(0, 1]`。文档明确这是「按实例计算的上限」，只约束当前 vLLM 实例：若同一张卡上跑两个 vLLM 实例，各自设 0.5 即可。

```python
# vllm/config/cache.py:68
gpu_memory_utilization: float = Field(default=0.92, gt=0, le=1)
```

[profiling 后才填入的字段](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/cache.py#L155-L167) —— 注意 `num_gpu_blocks`、`num_cpu_blocks`、`kv_cache_size_tokens`、`kv_cache_max_concurrency` 都标了 `init=False`，即「构造时不传，运行时由 profiling / KV 缓存初始化回填」。这正是 `CacheConfig` 「先声明预算、后测出实际块数」的体现。

```python
# vllm/config/cache.py:156-162
num_gpu_blocks: int | None = field(default=None, init=False)   # profiling 后回填
num_cpu_blocks: int | None = field(default=None, init=False)
kv_cache_size_tokens: int | None = field(default=None, init=False)  # KV 初始化后回填
```

[block_size 默认值的补齐校验器](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/cache.py#L253-L266) —— `_block_size_resolved` 守卫防止 Pydantic 在 `CacheConfig` 被嵌套进 `VllmConfig` 时重复跑这段逻辑；若 `block_size is None` 就补成 16，否则把 `user_specified_block_size` 置真。

接着看 `gpu_memory_utilization` 是如何被消费的——真正的换算发生在 worker 里：

[request_memory：把利用率换成字节数](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/worker/utils.py#L409-L429) —— 注意它是乘 `total_memory`（**总显存**，不是剩余显存），并在「剩余显存 < 申请量」时报错提示调低 `gpu_memory_utilization`。

```python
# vllm/v1/worker/utils.py:414-416
requested_memory = math.ceil(
    init_snapshot.total_memory * cache_config.gpu_memory_utilization
)
```

[available_kv_cache_memory_bytes 的计算](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/cache.py#L186-L195) 与 [determine_available_memory 的 profiling 主体](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/worker/gpu_worker.py#L459-L548) —— worker 先跑 `profile_run()` 测峰值，再用 `requested_memory - non_kv_cache_memory` 得到 KV 缓存可用字节，最终折算成 `num_gpu_blocks`。`kv_cache_memory_bytes`（非 None 时）可绕过 `gpu_memory_utilization` 手动指定 KV 字节数，适合精控显存的场景。

补充：`CacheConfig` 还持有大量「KV 缓存形态」开关——`cache_dtype`（KV 量化精度，如 `fp8`）、`enable_prefix_caching`（默认 `True`）、`sliding_window`、`is_attention_free`、mamba 相关字段等。它们大多由 `ModelConfig` 在 `VllmConfig.__post_init__` 里「手动复制」过来（字段注释明确写了 `primarily set in ModelConfig and that value should be manually duplicated here`），因为底层 KV 管理器只看 `CacheConfig`。

#### 4.1.4 代码实践

**实践目标**：用源码确认 `gpu_memory_utilization` 的默认值，并验证「`num_gpu_blocks` 是 profiling 后才填的、构造时为 `None`」。

**操作步骤**（可在装有 vLLM 的环境运行；若不可用则纯阅读源码）：

1. 写一段脚本，直接构造一个默认 `CacheConfig`：

   ```python
   # 示例代码
   from vllm.config import CacheConfig
   cc = CacheConfig()
   print("gpu_memory_utilization =", cc.gpu_memory_utilization)
   print("block_size =", cc.block_size)
   print("user_specified_block_size =", cc.user_specified_block_size)
   print("num_gpu_blocks =", cc.num_gpu_blocks)   # 构造时应为 None
   print("enable_prefix_caching =", cc.enable_prefix_caching)
   ```

2. 再构造一个显式传 `block_size` 的实例，对比 `user_specified_block_size`。

**需要观察的现象**：

- `gpu_memory_utilization` 为 `0.92`；`block_size` 默认补成了 `16`；`user_specified_block_size` 默认 `False`。
- `num_gpu_blocks` 在「只构造配置、还没启动引擎」时是 `None`——这印证了它要等 profiling。

**预期结果**：

```
gpu_memory_utilization = 0.92
block_size = 16
user_specified_block_size = False
num_gpu_blocks = None
enable_prefix_caching = True
```

若手头有 GPU 且已安装 vLLM，可进一步用 `vllm.LLM(model=<小模型>)` 启动引擎，在日志里搜索 `Maximum concurrency` / `KV Cache` 相关行，观察 `num_gpu_blocks`、`kv_cache_size_tokens` 被 profiling 回填后的真实数值。无法运行时，这一步标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `gpu_memory_utilization` 乘的是「总显存」而非「剩余显存」？若同卡上已有一个进程占了 30% 显存，vLLM 默认 0.92 会发生什么？

> **答案**：因为 0.92 是「本实例对自己占用的硬上限」，按总显存计费才能让用户用「总显存的百分比」这种稳定、与瞬时波动无关的方式表达预算。若同卡已有进程占 30%，则启动时剩余 < 92%，`request_memory` 会在 [utils.py:418](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/worker/utils.py#L418-L427) 报错，提示调低 `gpu_memory_utilization`。

**练习 2**：把 `block_size` 从 16 调到 8，`num_gpu_blocks` 会怎样变化？对前缀缓存命中率又可能有什么影响？

> **答案**：块变小后，同样字节数能分出更多但更小的块，`num_gpu_blocks` 数值变大但 `kv_cache_size_tokens`（= 块数 × 块大小）基本不变。更小的块让前缀缓存的命中粒度更细（能在更精确的 token 边界复用），但块表更大、管理开销略增。

### 4.2 SchedulerConfig：批处理与调度参数

#### 4.2.1 概念说明

调度器（Scheduler，见 u4-l2）每一步都要决定「这一步算哪些请求、算多少 token」。`SchedulerConfig` 就是给调度器定「节奏上限」的子配置。它不关心 KV 缓存具体怎么放（那是 `CacheConfig` 的事），只规定每步的 token 预算、并发请求数上限、是否分块预填充、按什么策略排队等。

它最关键的几个参数：

- **`max_num_batched_tokens`**：单步（一次 forward）最多处理多少个 token。这是 prefill 阶段的「嘴巴大小」。
- **`max_num_seqs`**：单步最多同时处理多少条序列（请求）。这是并发上限的硬帽子。
- **`enable_chunked_prefill`**：是否把超长 prefill 切成多块、和 decode 混在同一步算。
- **`policy`**：排队策略，`fcfs`（先来先服务）或 `priority`（按优先级）。
- **`watermark`**：预留多少比例的 KV 块空闲，避免频繁抢占。

#### 4.2.2 核心流程

调度每一步时，这几个参数如何协作：

1. **算 token 预算**：调度器为当前步能用的 token 数以 `max_num_batched_tokens` 为上限（推测解码等场景实际下发量受 `max_num_scheduled_tokens` 约束，后者默认等于前者）。
2. **选请求**：在不超过 `max_num_seqs` 的前提下，从 `waiting` 队列里按 `policy` 挑请求加入 `running`。
3. **分块预填充**：若 `enable_chunked_prefill=True`，一条很长的 prefill 不会一次性吃掉整步预算，而是切成「不超过剩余预算」的块；剩余预算可以塞 decode 请求，让 GPU 在 prefill 和 decode 间都满载。
4. **尊重水位线**：`watermark` 保留一定比例 KV 块空闲，显存紧张时少接请求、避免反复抢占。
5. **校验长度**：`verify_max_model_len` 检查 `max_num_batched_tokens` 与 `max_model_len`、`max_num_seqs` 的相容性。

`max_model_len` 本身**不属于** `SchedulerConfig`（它属于 `ModelConfig`），调度配置只是通过 `InitVar` 借用它来做校验。

#### 4.2.3 源码精读

[SchedulerConfig 的类级默认常量](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/scheduler.py#L42-L44) —— 三个默认值：单步 token 上限 2048、批量 DP 场景更小的 256、并发序列上限 128。

```python
# vllm/config/scheduler.py:42-44
DEFAULT_MAX_NUM_BATCHED_TOKENS: ClassVar[int] = 2048
DEFAULT_MAX_NUM_BATCHED_TOKENS_FOR_BATCHED_DP: ClassVar[int] = 256
DEFAULT_MAX_NUM_SEQS: ClassVar[int] = 128
```

> 注意：这些是「直接 new 出 `SchedulerConfig` 时的兜底默认」。真实运行时，`EngineArgs` 会按硬件型号与 `usage_context` 覆盖它们（例如 A100 上默认更大），见 [arg_utils.py:2529-2584](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/engine/arg_utils.py#L2509-L2584) 的注释 `Setting large max_num_batched_tokens for A100`。

[max_num_batched_tokens 与 max_num_seqs](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/scheduler.py#L49-L68) —— 前者是「单步最多算多少 token」，后者是「单步最多多少条序列」。

```python
# vllm/config/scheduler.py:49-50
max_num_batched_tokens: int = Field(default=DEFAULT_MAX_NUM_BATCHED_TOKENS, ge=1)
# vllm/config/scheduler.py:63-64
max_num_seqs: int = Field(default=DEFAULT_MAX_NUM_SEQS, ge=1)
```

[enable_chunked_prefill 默认 True](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/scheduler.py#L74-L80) —— V1 默认开启分块预填充，这是连续批处理能跨 prefill/decode 满载的前提。

[policy：排队策略](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/scheduler.py#L99-L105) —— `fcfs` 按到达顺序，`priority` 按给定优先级（值小先处理，相同则按到达时间）。

[watermark：KV 块水位线](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/scheduler.py#L136-L141) —— 默认 0.0（不预留）；显存吃紧时调高它能减少「频繁抢占 → 重算」的抖动。

[default_factory：补齐 InitVar 默认值](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/scheduler.py#L159-L168) —— 因为 `max_model_len`、`is_encoder_decoder` 是 `InitVar`（必须传），这个工厂在缺省时给 `max_model_len=8192`、`is_encoder_decoder=False`。`VllmConfig` 正是用它做 `scheduler_config` 的默认工厂。

[verify_max_model_len：跨参数校验](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/scheduler.py#L249-L285) —— 这段最能体现三个参数的制约关系：

```python
# vllm/config/scheduler.py:250-261（节选）
if self.max_num_batched_tokens < max_model_len \
   and not self.enable_chunked_prefill:
    raise ValueError(... "max_num_batched_tokens ... smaller than max_model_len ...")
if self.max_num_batched_tokens < self.max_num_seqs:
    raise ValueError(... "must be greater than or equal to max_num_seqs ...")
```

- 若不开分块预填充，`max_num_batched_tokens` 必须 ≥ `max_model_len`，否则长 prompt 一次装不下。
- `max_num_batched_tokens` 必须 ≥ `max_num_seqs`（每条序列每步至少 1 个 token，否则并发数无意义）。

[get_scheduler_cls：选择调度器实现](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/scheduler.py#L170-L191) —— 根据 `async_scheduling` 选 `AsyncScheduler` 还是普通 `Scheduler`，也支持用 `scheduler_cls` 字符串路径加载自定义调度器。

#### 4.2.4 代码实践

**实践目标**：用 `SchedulerConfig.default_factory()` 构造一份默认配置，确认关键默认值，并触发一次「故意的」校验错误，直观体会三个参数的制约。

**操作步骤**：

1. 运行下面这段示例代码：

   ```python
   # 示例代码
   from vllm.config import SchedulerConfig
   sc = SchedulerConfig.default_factory()   # max_model_len=8192, is_encoder_decoder=False
   print("max_num_batched_tokens =", sc.max_num_batched_tokens)
   print("max_num_seqs =", sc.max_num_seqs)
   print("enable_chunked_prefill =", sc.enable_chunked_prefill)
   print("policy =", sc.policy)
   print("max_num_encoder_input_tokens =", sc.max_num_encoder_input_tokens)

   # 故意制造 max_num_batched_tokens < max_num_seqs 的非法配置
   bad = SchedulerConfig.default_factory(
       max_model_len=8192,
       max_num_batched_tokens=4,
       max_num_seqs=128,
   )
   ```

**需要观察的现象**：

- 默认 `max_num_batched_tokens=2048`、`max_num_seqs=128`、`enable_chunked_prefill=True`、`policy="fcfs"`。
- `max_num_encoder_input_tokens` 在 `__post_init__` 里被设成 `max_num_batched_tokens`（多模态 encoder 预算跟它对齐）。
- 构造 `bad` 时应抛出 `ValueError`，提示 `max_num_batched_tokens (4) must be greater than or equal to max_num_seqs (128)`。

**预期结果**：前 5 行打印如上数值；`bad` 的构造抛出含 "must be greater than or equal to max_num_seqs" 的 `ValueError`。若环境无法运行，请阅读 [scheduler.py:263-268](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/scheduler.py#L263-L268) 复述该校验逻辑，并标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：把 `max_num_batched_tokens` 设得很大（比如 32768）一定能让吞吐更高吗？

> **答案**：不一定。更大的 token 预算确实允许更多 prefill/decode 混算，提升吞吐，但同时：① 增大单步激活显存峰值，可能挤压 KV 缓存可用字节、减少 `num_gpu_blocks`；② 让 torch.compile / cudagraph 需要覆盖更大 shape 区间，增加捕获开销。它和显存预算是此消彼长的。

**练习 2**：`watermark` 默认 0.0，在什么情况下你会想调高它？

> **答案**：当显存紧张、调度器频繁因 KV 块不足而抢占（preempt）正在运行的请求、导致大量重算和延迟抖动时。调高 `watermark` 预留空闲块做缓冲，能减少抢占频率，代价是峰值并发略降。

### 4.3 ParallelConfig：TP/PP/DP 并行声明

#### 4.3.1 概念说明

当单卡装不下一个模型，或单卡吞吐不够时，就要用多卡。`ParallelConfig` 是声明「怎么并行」的子配置：用几张卡做张量并行（TP）、几段做流水并行（PP）、几份做数据并行（DP），以及用哪种执行器后端把这些 worker 进程拉起来。

它最核心的字段：

- **`tensor_parallel_size`（TP）** / **`pipeline_parallel_size`（PP）** / **`data_parallel_size`（DP）**：三种并行规模。
- **`world_size`**：派生量，`= TP × PP × PCP`（prefill context parallel），决定要拉起多少个 worker。
- **`distributed_executor_backend`**：执行器后端，`mp`（多进程）/ `uni`（单进程）/ `ray` / `external_launcher`。
- **`enable_expert_parallel`** / **`all2all_backend`**：MoE 专家并行与 all-to-all 通信后端。
- **`decode_context_parallel_size`（DCP）/ `prefill_context_parallel_size`（PCP）**：上下文并行（长上下文场景把序列本身切开）。

#### 4.3.2 核心流程

`ParallelConfig` 在启动中的关键节点：

1. **构造**：用户通过 `--tensor-parallel-size` 等参数传入 TP/PP/DP，`EngineArgs` 翻译进 `ParallelConfig`。
2. **算 world_size**：在 `__post_init__` 里 `world_size = PP × TP × PCP`（`world_size` 是 `init=False` 字段）。若用 `external_launcher`，再乘上 DP。
3. **跨 DP 的总规模**：`world_size_across_dp = world_size × DP`，是整个部署的进程规模。
4. **选执行器后端**：若用户没指定，按「world_size 能否塞进单机」「是否要用 Ray」等规则在 `mp`/`uni`/`ray` 间自动选择。
5. **校验拓扑合法性**：`_validate_parallel_config` 检查 PCP/DCP/TP 的整除关系、EPLB 与专家并行的相容性等。
6. **校验执行器**：`_verify_args` 检查后端类型合法、Ray 可用、平台是否支持 custom all-reduce 等。

#### 4.3.3 源码精读

[ParallelConfig 类声明与三种并行规模](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/parallel.py#L118-L131) —— TP/PP/DP 都默认 1（单卡单副本）。注意 DP 的注释：MoE 层会按 `TP × PCP × DP` 的乘积来分片。

```python
# vllm/config/parallel.py:122-129
pipeline_parallel_size: int = Field(default=1, ge=1)
tensor_parallel_size: int = Field(default=1, ge=1)
prefill_context_parallel_size: int = Field(default=1, ge=1)
data_parallel_size: int = Field(default=1, ge=1)
```

[world_size：派生的进程规模](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/parallel.py#L327-L328) 与 [world_size_across_dp 属性](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/parallel.py#L548-L551) —— `world_size = PP×TP×PCP`，跨 DP 再乘 DP。

```python
# vllm/config/parallel.py:327-328
world_size: int = Field(init=False)
"""world_size is TPxPP, it affects the number of workers we create."""
# vllm/config/parallel.py:549-551
@property
def world_size_across_dp(self) -> int:
    return self.world_size * self.data_parallel_size
```

[__post_init__：计算 world_size 并选执行器后端](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/parallel.py#L831-L956) —— 先算 `world_size`，再决定 `distributed_executor_backend`：

```python
# vllm/config/parallel.py:833-837
self.world_size = (
    self.pipeline_parallel_size
    * self.tensor_parallel_size
    * self.prefill_context_parallel_size
)
```

[自动选择执行器后端的规则](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/parallel.py#L911-L956) —— 当 `distributed_executor_backend is None` 且 `world_size_across_dp > 1` 时：若单机 GPU 数 < world_size 直接报错（提示要么用 ray、要么设 nnodes）；同机够用则默认 `mp`；TPU + SPMD 用 `uni`；DP 后端为 ray 或处于 ray placement group 则用 `ray`。world_size==1 时落到 `uni`。

[distributed_executor_backend 字段](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/parallel.py#L243-L257) —— 文档写明：`mp` 用于单机（TP×PP ≤ 单机 GPU 数），`ray` 用于跨机，TPU 平台只支持 ray。

[_validate_parallel_config：拓扑合法性校验](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/parallel.py#L451-L546) —— 例如检查 `data_parallel_size_local ≤ data_parallel_size`、`PCP 不支持 DP`、`PCP 关闭时 tp 必须被 dcp 整除`、EPLB 需要 TP/PCP/DP 之一 > 1 等。

```python
# vllm/config/parallel.py:527-538（节选）
if pcp > 1 and self.data_parallel_size > 1:
    raise ValueError("PCP does not support data parallelism yet.")
if pcp == 1:
    if tp % dcp != 0:
        raise ValueError(f"tp_size={tp} must be divisible by dcp_size={dcp}.")
```

> 三种并行的取舍直觉：**TP** 降单层延迟、但通信开销大（每层都要 all-reduce），通常限 8 卡以内；**PP** 能装下超大模型、但引入流水气泡（bubble）降低吞吐；**DP** 几乎线性扩吞吐、对延迟无帮助，且每副本要完整复制权重。三者常组合使用。

#### 4.3.4 代码实践

**实践目标**：构造几份不同并行规模的 `ParallelConfig`，观察 `world_size` 与 `world_size_across_dp` 如何派生，并触发一次非法拓扑校验。

**操作步骤**：

1. 运行示例代码：

   ```python
   # 示例代码
   from vllm.config import ParallelConfig
   for tp, pp, dp in [(1,1,1), (4,1,1), (2,2,1), (2,1,4)]:
       pc = ParallelConfig(
           tensor_parallel_size=tp,
           pipeline_parallel_size=pp,
           data_parallel_size=dp,
       )
       print(f"TP={tp} PP={pp} DP={dp} -> world_size={pc.world_size}, "
             f"world_size_across_dp={pc.world_size_across_dp}, "
             f"backend={pc.distributed_executor_backend}")
   ```

**需要观察的现象**：

- `TP=1,PP=1,DP=1`：`world_size=1`、`backend='uni'`（单卡单进程）。
- `TP=4`：`world_size=4`，`backend` 视本机 GPU 数与 ray 是否可用而定（同机 4 卡则 `mp`）。
- `TP=2,PP=2`：`world_size=4`。
- `DP=4`：`world_size_across_dp = world_size × 4`。

2. （可选）尝试构造一份非法拓扑，例如把 `decode_context_parallel_size` 设成不被 TP 整除的值，观察 `_validate_parallel_config` 抛出的具体错误信息。

**预期结果**：打印出如上派生数值。第 2 步若环境无法运行（无多卡/无 ray），`backend` 的自动选择结果可能不同，请结合 [parallel.py:911-956](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/parallel.py#L911-L956) 的规则解释你机器上会落到哪个后端，并标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`world_size` 和 `world_size_across_dp` 有什么区别？为什么 DP 不计入 `world_size`？

> **答案**：`world_size = TP × PP × PCP`，是「单个模型副本」需要的 worker 数；`world_size_across_dp = world_size × DP` 是整个部署的总 worker 数。DP 的每个副本是独立进程组、各自跑完整模型，副本间不通过 NCCL 做张量/流水通信（只做轻量的负载/状态同步），所以不计入「单个模型副本」的 world_size。

**练习 2**：在一张 8 卡机器上跑一个单卡就能装下的模型，但希望尽量提高吞吐，你会怎么设 `ParallelConfig`？为什么不用 PP？

> **答案**：优先用 **DP=8**（或 TP 不变、开 DP）：每副本独立处理不同请求，扩吞吐近线性且无通信开销。若单请求延迟也重要，可考虑 TP=2/4 降单步延迟。一般不用 PP：单卡能装下时 PP 只会引入流水气泡、降低吞吐，PP 主要用于「单副本装不下超大模型」。

## 5. 综合实践

把三块子配置串成一个「三角取舍」任务。

**任务背景**：你接到一个部署需求——给定一张 80GB 显存的 GPU，要服务一个上下文最长 32k token 的模型。请基于本讲三块配置，分析下面三个参数各自影响什么、并说明它们如何互相制约：

- `cache_config.gpu_memory_utilization`（CacheConfig）
- `scheduler_config.max_num_seqs`（SchedulerConfig）
- `model_config.max_model_len`（ModelConfig，被 SchedulerConfig 借用校验）

**要求你完成**：

1. **画一张关系图**：把「总显存 → `gpu_memory_utilization` → profiling → `num_gpu_blocks` → KV token 容量」这条链画出来，并标出 `max_num_seqs` 和 `max_model_len` 分别从哪里「卡」住并发。
2. **写出取舍公式**：最坏情况下，要同时跑 N 条最长上下文的请求，需要的 KV token 数约为：

   \[ \text{kv\_tokens\_needed} \approx N \times \text{max\_model\_len} \]

   实际能容纳的并发数受 KV 容量限制：

   \[ N_{\text{by KV}} \approx \left\lfloor \frac{\text{num\_gpu\_blocks} \times \text{block\_size}}{\text{max\_model\_len}} \right\rfloor \]

   而调度器允许的真实并发是：

   \[ N_{\text{actual}} = \min\bigl(\text{max\_num\_seqs},\; N_{\text{by KV}}\bigr) \]

3. **解释三个方向调节的后果**：
   - 调高 `gpu_memory_utilization`（如 0.92 → 0.95）：KV 容量变大、并发可提升，但留给其他进程/激活的余量变小，OOM 风险上升。
   - 调高 `max_num_seqs`（如 128 → 256）：并发上限抬升，但只有在 KV 容量跟得上时才有效；否则只会让调度器更频繁抢占。
   - 调高 `max_model_len`（如 32k → 64k）：支持更长上下文，但每条请求 KV 占用翻倍，同等显存下可并发数约减半。
4. **给出你的推荐**：在「吞吐优先」与「长上下文优先」两种目标下，你会分别怎么权衡这三个参数，并指出哪个参数是「主旋钮」、哪个是「从旋钮」。

**参考要点**：`gpu_memory_utilization` 是显存主旋钮（决定 KV 容量池大小）；`max_model_len` 决定每请求的 KV 单价；`max_num_seqs` 是并发硬上限、只有在 KV 容量充裕时才是有效约束。三者必须联调——单独拉高 `max_num_seqs` 而 KV 不够，只会换来抢占与抖动。

## 6. 本讲小结

- `CacheConfig` 管「显存与 KV 缓存」：`gpu_memory_utilization`（默认 0.92）按**总显存**定预算，profiling 后回填 `num_gpu_blocks`；`block_size`（默认 16）决定切块粒度；`cache_dtype`/`enable_prefix_caching` 等决定缓存形态。
- `SchedulerConfig` 管「批处理节奏」：`max_num_batched_tokens`（默认 2048）是单步 token 预算，`max_num_seqs`（默认 128）是并发硬上限，`enable_chunked_prefill`（默认 True）让 prefill/decode 混算，`watermark` 防 KV 抖动。
- `ParallelConfig` 管「并行拓扑」：TP/PP/DP 声明规模，`world_size = TP×PP×PCP` 派生 worker 数，`distributed_executor_backend` 在 mp/uni/ray 间自动选择，并有 PCP/DCP/EPLB 等拓扑校验。
- 三者通过 `VllmConfig.__post_init__` 联动校验（如 `max_num_batched_tokens ≥ max_num_seqs`、async scheduling 与执行器后端相容）。
- 三角取舍：`gpu_memory_utilization`（KV 容量池）× `max_model_len`（每请求 KV 单价）共同决定「KV 能容纳多少并发」，而 `max_num_seqs` 只是并发的硬上限——实际并发取两者最小值。

## 7. 下一步学习建议

- 顺着「KV 缓存」继续：阅读 [u4-l4 PagedAttention 与 KV 缓存管理](u4-l4-pagedattention-kv-cache.md)、[u4-l5 前缀缓存](u4-l5-prefix-caching.md)，看 `num_gpu_blocks`、`block_size` 如何被 `KVCacheManager` / `BlockPool` 真正消费。
- 顺着「调度」继续：阅读 [u4-l2 Scheduler 调度器核心](u4-l2-scheduler-core.md)，看 `max_num_batched_tokens`、`max_num_seqs`、`watermark` 如何在 `schedule()` 里落地为每步的 `SchedulerOutput`。
- 顺着「并行」继续：阅读 [u9-l1 张量/流水/数据并行](u9-l1-parallelism.md) 与 [u9-l2 执行器与多进程部署](u9-l2-executors.md)，看 `world_size`、`distributed_executor_backend` 如何驱动 worker 进程拉起与进程组初始化。
- 想看「上层裁判」如何联动这三块：重读 [vllm/config/vllm.py 的 `__post_init__`](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/vllm.py#L972-L1492)，那是三者（乃至更多子配置）一致性检查的中枢。
