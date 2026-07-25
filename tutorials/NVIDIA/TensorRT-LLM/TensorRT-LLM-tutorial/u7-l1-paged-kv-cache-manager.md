# 分页 KV Cache 与 KVCacheManager

## 1. 本讲目标

本讲聚焦 PyTorch 后端里「显存」这一核心资源——KV Cache。读完本讲，你应当能够：

- 说清楚**分页 KV Cache（Paged KV Cache）**为什么要把连续的 KV 切成等长的 block，以及一个 block 在显存里长什么样；
- 把 `KVCacheManager` 当作一个「资源管理器」，复述它对外的三类接口（生命周期类、`CapacityScheduler` 容量类、`ModelEngine` 读取类），并指出它们在 `PyExecutor` 单步循环里的调用时机；
- 区分 KV Cache 管理器的**两套实现**：基于 C++ 绑定的 V1（`KVCacheManager`）与纯 Python、可被 mypyc 编译的 V2（`KVCacheManagerV2`），并理解它们在「分配/回收」时机上的关键差异。

本讲承接 u3-l2（PyExecutor 单步循环）。在 u3-l2 里，单步循环的主干是「取请求 → 调度 → `prepare_resources` → 前向 → 采样 → `_handle_responses` → `update_resources`」。其中 `prepare_resources` / `update_resources` 两次「资源动作」的主角，正是本讲的 `KVCacheManager`。

## 2. 前置知识

### 2.1 什么是 KV Cache，为什么它要占显存

Transformer 在自回归生成时，每生成一个新 token，注意力层都要拿这个 token 去和**历史所有 token 的 Key/Value** 做点积。如果不缓存，每一步都要重算前面所有层的 K、V，代价随序列长度二次膨胀。

所以推理引擎会把每一层、每个历史 token 的 K 与 V 存下来，这就是 **KV Cache**。它是「以显存换算力」：有了它，单步前向的计算量与序列长度无关（只与本次新增的 token 数有关），但它会随着序列变长**持续吃显存**，是 LLM 服务里最关键的资源之一。

### 2.2 Prefill 与 Decode 两个阶段

- **Prefill（上下文阶段）**：一次性吃下整段 prompt，算力密集，KV Cache 从 0 增长到 prompt 长度；
- **Decode（生成阶段）**：每步只新增 1 个（加上投机解码的若干草稿）token，带宽密集，KV Cache 每步只长一点点。

这两种增长模式很不一样，`KVCacheManager` 的分配逻辑会按阶段区分对待。

### 2.3 资源管理器的三段式生命周期

在 u2-l3、u3-l2 里已经建立了「Python 调度、C++ 加速」的心智，以及 `BaseResourceManager` 的 `prepare / update / free` 三段式。本讲就是这个抽象在「显存」上的具体落地。如果这三段式的含义有些模糊，可以先回到 u3-l2 复习。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [docs/source/torch/kv_cache_manager.md](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/torch/kv_cache_manager.md) | 官方对 `KVCacheManager` 接口的说明，本讲最重要的「索引文档」 |
| `tensorrt_llm/_torch/pyexecutor/resource_manager.py` | 定义 `BaseResourceManager` 抽象基类，以及 **V1 版** `KVCacheManager`（封装 C++ 绑定） |
| `tensorrt_llm/_torch/pyexecutor/kv_cache_manager_v2.py` | **V2 版** `KVCacheManagerV2`，纯 Python 适配器，包装运行时纯 Python 实现 |
| `tensorrt_llm/runtime/kv_cache_manager_v2/__init__.py` | V2 运行时纯 Python 子包的入口，导出真正的 `KVCacheManager` / `_KVCache` 等 |
| `tensorrt_llm/runtime/kv_cache_manager_v2/AGENTS.md` | V2 子包自带的架构说明，分层非常清晰，强烈推荐先读 |
| `tensorrt_llm/_torch/pyexecutor/kv_cache_transceiver.py` | 分离式服务（disaggregated serving）里跨 worker 搬运 KV Cache 的收发器，本讲只做关联说明（详见 u11-l2） |
| `tensorrt_llm/_torch/pyexecutor/py_executor.py` | 单步循环的宿主，调用上述三段式接口的地方 |

> 一个容易混的名字陷阱：仓库里有**三个**都叫 `KVCacheManager` 的东西——
> 1. `tensorrt_llm._torch.pyexecutor.resource_manager.KVCacheManager`（V1 适配器，Python）；
> 2. `tensorrt_llm.bindings.internal.batch_manager.KVCacheManager`（V1 真正的 C++ 实现）；
> 3. `tensorrt_llm.runtime.kv_cache_manager_v2.KVCacheManager`（V2 纯 Python 实现，被 `KVCacheManagerV2` 包成 `self.impl`）。
>
> 加上 `KVCacheManagerV2`（V2 适配器），一共四层。本讲会始终带上模块前缀来区分。

---

## 4. 核心概念与源码讲解

### 4.1 分页 KV Cache：把连续显存切成等长 block

#### 4.1.1 概念说明

最朴素的 KV Cache 存储方式是「一个序列占一段连续显存」。这有两个致命问题：

1. **显存碎片**：序列长度千差万别，提前预留 `max_seq_len` 会大量浪费；不预留则要频繁搬运整理；
2. **无法共享前缀**：两个请求如果 prompt 前缀相同（比如同一个 system prompt），它们的 KV Cache 内容完全一样，连续存储下无法复用。

**分页 KV Cache** 借鉴了操作系统的虚拟内存分页思想：把整个 KV 显存池切成大量**等长的 block（页）**，每个 block 容纳 `tokens_per_block` 个 token 的 K（和 V）。一个序列不再要求连续显存，而是通过一张 **block table（页表）** 把「逻辑 token 位置」映射到「物理 block 编号」。空闲 block 由管理器统一调度分配/回收，于是：

- 请求按需逐块申请，无碎片、无浪费；
- 前缀相同的请求可以共享同一批物理 block（block reuse / radix tree）。

这套思想最早由 vLLM 的 PagedAttention 论文带入 LLM 推理社区，TensorRT-LLM 也采用类似设计。

#### 4.1.2 核心流程

一个 block 在显存里的形状（以 V2 的 NHD 布局为例）是：

\[ \text{shape} = [\,\text{num\_blocks},\ \text{kv\_factor},\ \text{tokens\_per\_block},\ \text{num\_kv\_heads},\ \text{head\_dim}\,] \]

其中：

- `num_blocks`：池子里总共有多少个 block（由显存配额 quota 决定）；
- `kv_factor`：标准 attention 下 K 与 V 分开存，`kv_factor = 2`；MLA 或 `SELFKONLY` 把 K/V 合体，`kv_factor = 1`（见 [kv_cache_manager_v2.py:809](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/kv_cache_manager_v2.py#L809)）；
- `tokens_per_block`：一页放几个 token，模型配置默认 `32`（见 [llm_args.py:3715](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L3715)）；
- `num_kv_heads`、`head_dim`：每层注意力头数与每头维度（GQA 下 KV 头数远小于 query 头数）。

一个请求的「页表」是一串物理 block id：`[b0, b1, b2, ...]`。token `i` 落在页表第 `i // tokens_per_block` 个 block 的第 `i % tokens_per_block` 个槽位。若某个 block 被驱逐（SWA 滑窗越界或被换出到 Host），其页表项会被标记为 `BAD_PAGE_INDEX`。

#### 4.1.3 源码精读

**池缓冲区的真实形状** —— `get_buffers` 返回某一层 KV 池的 torch 视图（零拷贝包裸显存地址）：

[tensorrt_llm/_torch/pyexecutor/kv_cache_manager_v2.py:1841-1869](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/kv_cache_manager_v2.py#L1841-L1869) —— 注意力后端正是从这里拿到 KV 池指针去读写。NHD 布局下 shape 为 `[num_blocks, kv_factor, tokens_per_block, num_kv_heads, head_dim]`，与上面的公式一致。

**页表的宿主缓冲** —— 每个请求每一步的页表存在 CPU pinned 内存里，再异步拷到 GPU 喂给注意力 kernel：

[tensorrt_llm/_torch/pyexecutor/kv_cache_manager_v2.py:1271-1279](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/kv_cache_manager_v2.py#L1271-L1279) —— `host_kv_cache_block_offsets` 的形状是 `[num_pools, capacity, 2 (key/value), max_blocks_per_seq]`，初始化为 0（未用的槽指向安全块 0）。

**池指针与层→池映射** —— 多层可能共享同一个物理池（按 `(生命周期, 单块大小)` 合并），所以还需要一张「层到池」的映射：

[tensorrt_llm/_torch/pyexecutor/kv_cache_manager_v2.py:1159-1282](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/kv_cache_manager_v2.py#L1159-L1282) —— `_prepare_page_table_tensor` 构造 `kv_cache_pool_pointers`（每池裸地址）与 `kv_cache_pool_mapping`（每层在池内的偏移）。

**每请求页表的读取** —— 注意力元数据需要「每个请求用了哪些 block」：

[tensorrt_llm/_torch/pyexecutor/kv_cache_manager_v2.py:3094-3112](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/kv_cache_manager_v2.py#L3094-L3112) —— `get_batch_cache_indices` 返回 `List[List[int]]`，即每个请求的物理 block id 列表；`BAD_PAGE_INDEX` 项原样保留。

#### 4.1.4 代码实践

**实践目标**：不跑模型，仅凭源码算出一个具体模型「一个 block 占多少字节」，建立对显存占用的量感。

**操作步骤**：

1. 选一个公开的小模型配置，例如 Llama-3 8B：`num_hidden_layers=32`、`num_key_value_heads=8`、`head_dim=128`、KV cache dtype 为 `BF16`（2 字节）。
2. 阅读 [get_layer_bytes_per_token](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/kv_cache_manager_v2.py#L3217-L3266) 的公式：标准 attention 下每 token 每层字节数 = `kv_factor * num_kv_heads * head_dim * dtype_bytes`。
3. 计算单 block 字节数 = 每层每 token 字节 × `tokens_per_block`（默认 32）× `num_layers`。

**预期结果**（按公式手算）：

- 单层单 token：`2 (K+V) × 8 (kv heads) × 128 (head_dim) × 2 (BF16) = 4096` 字节；
- 单层单 block：`4096 × 32 = 131072` 字节 = 128 KiB；
- 全 32 层单 block：`128 KiB × 32 = 4096 KiB = 4 MiB`。

即 Llama-3 8B 下，KV 池里**每 32 个 token 占约 4 MiB 显存**。如果一个序列长 4096 token，就需要 `4096/32 = 128` 个 block ≈ 512 MiB KV Cache。

**若无法确定运行结果**：上述为手算，标注「待本地验证」——可在有 GPU 的环境里真正起一个 LLM，从日志的 `KV cache manager v2 device quota set to ...GiB` 与 `max_num_tokens` 反推验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `kv_factor` 在 MLA（DeepSeek 系）下是 1 而不是 2？
**答**：MLA 把历史 K/V 压成了低秩的潜表示（latent），generation 路径只存「吸收后」的潜缓存，K 与 V 不再分开存，所以因子是 1。标准 attention 把 K、V 各存一份，因子是 2。见 [kv_cache_manager_v2.py:809](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/kv_cache_manager_v2.py#L809)。

**练习 2**：页表里出现 `BAD_PAGE_INDEX` 意味着什么？注意力 kernel 会怎么处理？
**答**：意味着该逻辑 block 当前没有有效的物理块（典型场景：滑动窗口注意力 SWA 把越界的旧 block 标记为无效，或 block 被换出）。`get_batch_cache_indices` 会把 `BAD_PAGE_INDEX` 原样返回，注意力后端据此跳过这些位置、不参与注意力计算。

---

### 4.2 KVCacheManager：资源管理器抽象与三段式生命周期

#### 4.2.1 概念说明

`KVCacheManager` 不是一个孤立的类，而是 **`BaseResourceManager`** 的一种实现。`BaseResourceManager` 是 PyTorch 后端里所有「按请求生命周期管理的资源」的统一抽象——KV Cache 是其中之一，此外还有序列槽（seq slot）、PEFT/LoRA 缓存、投机解码资源等（见 [ResourceManagerType 枚举](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/resource_manager.py#L94-L101)）。

把 KV Cache 包装成「资源管理器」的好处是：`PyExecutor` 的单步循环不需要知道 KV Cache 的细节，它只在固定的三个时机调用三个统一接口，具体怎么分配显存交给管理器自己决定。这就是 u3-l2 里 `ResourceManager 三段式（prepare/update/free）` 的真实落点。

#### 4.2.2 核心流程

`KVCacheManager` 对外的接口分三组：

| 接口组 | 方法 | 谁来调用 | 何时调用 |
|--------|------|----------|----------|
| **生命周期** | `prepare_resources` | PyExecutor 单步循环 | 每步**前向之前**，为本步 batch 分配/调整 KV |
| | `update_resources` | PyExecutor 单步循环 | 每步**响应处理之后**，更新已分配资源（如修正 capacity） |
| | `free_resources` | PyExecutor 请求完成时 | 请求结束，回收该请求占的 block |
| **容量（CapacityScheduler）** | `get_max_resource_count` | `CapacityScheduler` | 查询「最多还能容纳多少资源」（V1=最大 block 数） |
| | `get_needed_resource_to_completion` | `CapacityScheduler` | 估算「一个请求跑到完成还需要多少资源」，调度器求和判断能否接纳新请求 |
| **ModelEngine 读取** | `get_buffers` | 注意力后端 | 拿某一层的 KV 池张量去读写 |
| | `get_batch_cache_indices` | 注意力元数据构建 | 拿本步每个请求的 block id 列表 |
| | `get_num_free_blocks` | 预热/估算 | 空闲 block 数（仅管理器为空时调用） |

`PyExecutor` 调用三段式的典型位置（朴素单步循环）：

1. 调度出 `scheduled_batch` 后、前向之前，调用 `prepare_resources`：[py_executor.py:2628](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L2628)；
2. 前向 + 采样 + 处理响应之后，调用 `update_resources`：[py_executor.py:4851-4853](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L4851-L4853)；
3. 请求完成时调用 `free_resources`：[py_executor.py:6586](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/py_executor.py#L6586)。

#### 4.2.3 源码精读

**抽象基类 `BaseResourceManager`**：

[resource_manager.py:138-162](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/resource_manager.py#L138-L162) —— `get_max_resource_count` 与 `get_needed_resource_to_completion` 是 `@abstractmethod`（必须实现）；`prepare_resources` / `update_resources` / `free_resources` / `shutdown` 给了默认空实现，子类按需覆盖。

**官方文档对三段式语义的权威说明**（强烈建议读原文）：

[docs/source/torch/kv_cache_manager.md:20-30](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/torch/kv_cache_manager.md#L20-L30) —— 要点：
- `prepare_resources`：context 首次进入要为整段 prompt 分配；generation 阶段只为本步分配；若块内还有空位，可能不真正分配；
- `update_resources`：对 KV Cache 通常无操作（除非启用 Python 侧的 radix tree 复用管理）；
- `free_resources`：请求结束回收 block；C++ 绑定实现里对应调用 `remove_sequence`。

**容量类接口**：

[docs/source/torch/kv_cache_manager.md:33-37](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/torch/kv_cache_manager.md#L33-L37) —— `CapacityScheduler` 用这两个接口估算能否接纳新请求，是 inflight batching 容量控制的依据。

**V1 实现里 `free_resources` 如何映射到 C++**：

[resource_manager.py:1069-1071](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/resource_manager.py#L1069-L1071) —— 直接调用 `self.impl.remove_sequence(...)`，这里 `self.impl` 就是 C++ 绑定 `bindings.internal.batch_manager.KVCacheManager`。这正是 u2-l3 讲的「Python 接口、C++ 实现」。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：阅读官方文档，说明一个请求的 KV cache block 是如何被分配与释放的，并列举 `KVCacheManager` 在单步循环中被调用的三个接口及其调用时机。这是本讲 `practice_task` 的核心。

**操作步骤**：

1. 打开 [docs/source/torch/kv_cache_manager.md](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/torch/kv_cache_manager.md)，精读「Interfaces」一节。
2. 在 `py_executor.py` 里用搜索定位 `prepare_resources`、`update_resources`、`free_resources` 三处调用，记下它们各自出现在哪个方法里（`_executor_loop`？`_process_previous_batch`？`_handle_responses`？）。
3. 画一张时序图：横轴是单步循环的时间，纵轴是「调度 / prepare / 前向 / 采样 / 响应 / update」，标注 KV Cache 在每一步的状态变化。

**需要观察的现象 / 预期结果**：

- `prepare_resources` 出现在**调度之后、前向之前**；
- `update_resources` 出现在**前向与响应处理之后**（重叠调度模式下处理的是「上一步」的 batch）；
- `free_resources` 在请求判定为 `COMPLETE` 时触发，把该请求的 block 还回空闲池。
- 一个请求的 block 生命周期：`prepare_resources` 时按阶段分配 → 每步 `update_resources` 微调 capacity → 完成时 `free_resources` 回收。

**若无法确定运行结果**：本实践为「源码阅读型」，结论可从源码直接得出，无需 GPU；若要观察真实日志，可启用 `TLLM_LOG_LEVEL_BY_MODULE="debug:_torch"` 跑一个小模型，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `update_resources` 对 KV Cache「通常无操作」？
**答**：因为分配已经在 `prepare_resources` 完成，前向只是往已分配的 block 里**写**新的 K/V，不需要再分配或搬运。`update_resources` 主要用来修正 capacity（例如投机解码 rewind 后缩回 history_length）或做 radix tree 复用记账，没有这些需求时就是空操作。

**练习 2**：`CapacityScheduler` 用 `get_needed_resource_to_completion` 做什么决策？
**答**：调度器把每个等待中的请求「跑到完成还需要的 block 数」求和，与当前剩余空闲 block 比较，从而决定本轮能否把新请求从 waiting queue 提升进来。这是 inflight batching 控制并发、防止 OOM 的关键。

**练习 3**：`KVCacheManager` 与注意力后端之间唯一的正式数据通道是什么？
**答**：通过 `get_buffers`（KV 池张量）和 `get_batch_cache_indices`（每请求 block id 列表）把「池子在哪、我用了哪些块」告诉注意力后端；后端据此在池子里读写 K/V。这与 u6-l1 讲的「模块层与后端的唯一数据通道是 `(q,k,v,metadata,forward_args)`」相呼应。

---

### 4.3 V2 实现：纯 Python `KVCacheManagerV2`

#### 4.3.1 概念说明

TensorRT-LLM 目前有**两套** KV Cache 管理器实现：

- **V1（`resource_manager.KVCacheManager`）**：Python 适配器 + C++ 绑定实现（`self.impl = bindings.internal.batch_manager.KVCacheManager`）。成熟、性能由 C++ 保证，但扩展要改 C++。
- **V2（`kv_cache_manager_v2.KVCacheManagerV2`）**：Python 适配器 + **纯 Python** 运行时实现（`self.impl = runtime.kv_cache_manager_v2.KVCacheManager`），且纯 Python 部分被设计成可用 **mypyc** 编译成 C 以拿到接近原生的性能。

V2 的卖点是**可扩展、可调试**：分页、多级缓存（GPU/Host/Disk）、基于 radix tree 的前缀共享、为 `MAX_UTILIZATION` 调度器服务的 suspend/resume（换出/换入）等都用 Python 写成，便于演进。两者都实现同一套 `BaseResourceManager` 接口，对 `PyExecutor` 是「可互换」的（由 `use_kv_cache_manager_v2` 开关选择，该开关在 u4-l2 讲的 `llm_utils` 里按模型默认值解析）。

#### 4.3.2 核心流程

V2 的分层（来自 [runtime/kv_cache_manager_v2/AGENTS.md](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/runtime/kv_cache_manager_v2/AGENTS.md)，自底向上）：

1. `_common.py` —— 基础类型（`TokenId`、`CacheLevel`、`PageStatus` 等）；
2. `rawref/` —— C 扩展，提供 mypyc 兼容的弱引用；
3. `_storage/` —— 按槽位分配的底层显存池；
4. `_page.py` —— 页抽象（`CommittedPage` / `UncommittedPage` / `BlockPage`）；
5. `_block_radix_tree.py` —— 前缀共享用的基数树；
6. `_eviction_controller/` —— 内存不足时选谁驱逐；
7. `_copy_engine.py` —— GPU↔Host↔Disk 的批量拷贝；
8. `_core/_kv_cache.py`（`_KVCache`）—— **单序列**的缓存状态；
9. `_core/_kv_cache_manager.py`（`KVCacheManager`）—— 顶层管理器，拥有所有 `_KVCache`。

页的生命周期是一个状态机：

\[ \text{UncommittedPage} \xrightarrow{\text{commit}} \text{CommittedPage (GPU)} \xrightarrow{\text{evict}} \text{CommittedPage (Host/Disk)} \xrightarrow{\text{recall}} \text{CommittedPage (GPU)} \]

**V1 与 V2 在「分配时机」上的关键差异**：在 V1 里，`prepare_resources` 就是真正干活的入口（调 C++ 的 `add_sequence_batch` / `add_token`）；而在 V2 里，主管理器的 `prepare_resources` 几乎是空操作，真正的分配由一个**专门的 Python 调度器**（`KVCacheV2Scheduler`）在更早的时机调用 `try_allocate_generation` / `prepare_context` / `resize_context` 完成。也就是说，V2 把「分配策略」从 C++ 黑盒挪到了可读的 Python 代码里。

#### 4.3.3 源码精读

**V2 适配器类定义**：

[kv_cache_manager_v2.py:740](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/kv_cache_manager_v2.py#L740) —— `class KVCacheManagerV2(BaseResourceManager)`。

**包装纯 Python 实现**：

[kv_cache_manager_v2.py:1047](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/kv_cache_manager_v2.py#L1047) —— `self.impl = KVCacheManagerPy(config, ...)`，其中 `KVCacheManagerPy` 即 `runtime.kv_cache_manager_v2.KVCacheManager`（[导入见 :75-76](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/kv_cache_manager_v2.py#L75-L76)）。运行时子包导出这些符号见 [runtime/kv_cache_manager_v2/__init__.py:49-62](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/runtime/kv_cache_manager_v2/__init__.py#L49-L62)。

**多级缓存配置（GPU/Host/Disk）**：

[kv_cache_manager_v2.py:979-1033](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/kv_cache_manager_v2.py#L979-L1033) —— `cache_tiers` 先放 `GpuCacheTierConfig(quota=...)`；若没显式给 host 尺寸，会按「设备配额、可用内存、`RLIMIT_MEMLOCK`」三者取最小自动开一个 Host 层（因为 `MAX_UTILIZATION` 调度器的 suspend/resume 需要一个落脚点），还可选加 Disk 层。

**V2 的 `prepare_resources` —— 主管理器几乎空操作**：

[kv_cache_manager_v2.py:2362-2369](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/kv_cache_manager_v2.py#L2362-L2369) —— 只有 `is_draft` 时才真正干活（镜像主管理器的分配）。主管理器的分配由 V2 调度器另调下面的方法完成。

**V2 的「分配」真正入口**：

- 生成阶段每步 +1（加草稿）：[try_allocate_generation:2083-2100](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/kv_cache_manager_v2.py#L2083-L2100) —— 必要时先 `resume()` 从 Host 换回 GPU，再 `kv_cache.resize(capacity + 1 + draft_len)`；
- 上下文阶段按 chunk 扩容：[resize_context:2248-2271](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/kv_cache_manager_v2.py#L2248-L2271)；
- 首次进入上下文、建 `_KVCache`、查前缀复用：[prepare_context:2185](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/kv_cache_manager_v2.py#L2185) 与 [_create_kv_cache:3489-3530](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/kv_cache_manager_v2.py#L3489-L3530)。

**V2 的 `update_resources`**：

[kv_cache_manager_v2.py:3405-3442](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/kv_cache_manager_v2.py#L3405-L3442) —— 对每个 generation 请求，按 `rewind_len` 缩 capacity、按 `max_beam_num_tokens-1` 设 history_length（与投机解码接受的草稿回退有关，承接 u3-l2、u10-l3）。

**V2 的 `free_resources`**：

[kv_cache_manager_v2.py:3072-3086](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/kv_cache_manager_v2.py#L3072-L3086) —— 从 `kv_cache_map` 弹出该请求，`kv_cache.close()` 释放物理 block，再 `index_mapper.remove_sequence` 回收页表槽位。这对应文档说的「回收 block」。

**一个诚实的重要细节：V2 的容量接口仍是桩**：

[kv_cache_manager_v2.py:3318-3330](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/kv_cache_manager_v2.py#L3318-L3330) —— `get_max_resource_count` 返回 `1`、`get_needed_resource_to_completion` 返回 `0`，都标了 `TODO: implement this`。这是因为 V2 走的是独立的 `KVCacheV2Scheduler`，容量判断由它内部另行完成，不复用 V1 的 `CapacityScheduler` 路径。读源码时看到这两个桩不要误以为是 bug。

> 关联：分离式服务（disaggregated serving）需要在 worker 之间搬运这些 KV block，那是 `kv_cache_transceiver.py` 的职责（[KvCacheTransceiver:183](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/kv_cache_transceiver.py#L183)）。它消费的正是本讲管理器产出的 block id 与池指针。完整拆解见 u11-l2。

#### 4.3.4 代码实践

**实践目标**：对比 V1 与 V2 在「分配」这一步的代码组织差异，亲手把「V2 把策略挪到 Python」这个结论落到具体函数。

**操作步骤**：

1. 打开 V1 的 `prepare_resources`：[resource_manager.py:780](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/resource_manager.py#L780)，找到它对 context 请求调 `self.impl.add_sequence_batch(...)`、对 generation 请求调 `self.impl.add_token(...)` 的段落（[:799-806](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/resource_manager.py#L799-L806)）。注意这些 `impl.*` 都进入了 C++。
2. 打开 V2 的 `prepare_resources`：[:2362](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/kv_cache_manager_v2.py#L2362)，确认主管理器分支为空。
3. 在 `kv_cache_manager_v2.py` 内搜索 `try_allocate_generation` 的调用方（应在 `scheduler_v2.py` 一带的 V2 调度器里），确认 V2 的分配确实由专用调度器驱动。
4. 写两三句话总结：V1 的分配入口是 ______（C++ 方法），V2 的分配入口是 ______（Python 方法）。

**预期结果**：V1 分配入口 = `impl.add_sequence_batch` / `impl.add_token`（C++）；V2 分配入口 = `try_allocate_generation` / `resize_context` / `prepare_context`（纯 Python，由 `KVCacheV2Scheduler` 调用）。

**若无法确定运行结果**：本实践为源码追踪型，结论来自静态阅读，无需 GPU。

#### 4.3.5 小练习与答案

**练习 1**：V2 为什么要自动开一个 Host 缓存层，哪怕用户没配？
**答**：`MAX_UTILIZATION` 调度器靠 suspend/resume 在显存紧张时把页换出、稍后换回。若没有 Host 层，suspend 出去的页无处落脚，`resume()` 必然失败，会导致调度死锁——没有任何 generation 请求能继续推进。代码注释在 [:984-987](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/kv_cache_manager_v2.py#L984-L987) 明确写了这一点。

**练习 2**：为什么 V2 的 `get_max_resource_count` / `get_needed_resource_to_completion` 是返回常量的桩？
**答**：V2 不复用 V1 的 `CapacityScheduler`，而是用独立的 `KVCacheV2Scheduler` 做容量判断，所以这两个为 V1 调度器设计的接口在 V2 里无人调用，保持桩即可。这是「同一套抽象接口、不同实现路径」的典型代价。

**练习 3**：一个请求在 V2 里的「KV 身份」是什么对象？它什么时候被创建与销毁？
**答**：是 `_KVCache`（per-sequence），在 `_create_kv_cache` 里创建并放入 `kv_cache_map`（[:3519](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/kv_cache_manager_v2.py#L3519)），在 `free_resources` 里 `close()` 后从 map 弹出（[:3076-3081](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/kv_cache_manager_v2.py#L3076-L3081)）。

---

## 5. 综合实践

**任务**：用一个小模型，亲手「逼」KV Cache 走一遍「分配 → 容量吃紧 → 回收」的完整生命周期，把本讲三个模块串起来。

**操作步骤**：

1. 构造一个 `LLM`，故意把 KV cache 配得很小：在 `kv_cache_config` 里设一个偏小的 `max_tokens`（参考 [KvCacheConfig:3587](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L3587) 起的字段）。示例代码（非项目原有，标注为**示例代码**）：

   ```python
   # 示例代码：最小化 KV cache 以观察分配/回收行为
   from tensorrt_llm import LLM, SamplingParams
   llm = LLM(model="<本地小模型路径>",
             kv_cache_config={"max_tokens": 2048})  # 故意压小
   prompts = ["Hello, " * 200] * 8  # 多个长 prompt 抢显存
   for out in llm.generate(prompts, SamplingParams(max_tokens=64)):
       print(out.request_id, len(out.outputs[0].text))
   ```

2. 启用调试日志观察分配：`TLLM_LOG_LEVEL_BY_MODULE="debug:_torch"`，重点看启动时的 `KV cache manager v2 device quota set to ...GiB` 与 `max_num_tokens`，以及运行中是否有 suspend/resume 或排队。
3. 把观察到的现象与 4.1.4 算出的「每 block 字节数」对照：给定 `max_tokens`，池里能放几个 block？同时几个请求会触发互相挤占？

**预期结果**：当并发请求的 KV 需求超过 `max_tokens` 时，要么部分请求被挡在 waiting queue（V1 的 `CapacityScheduler` 路径），要么发生 suspend/resume 换页（V2 的 `MAX_UTILIZATION` 路径）；增大 `max_tokens` 后并发度明显提升。

**若无法确定运行结果**：本实践需要 GPU 与真实模型权重。若当前环境不具备，标注「待本地验证」，并改为纯阅读版：在 `kv_cache_manager_v2.py` 里追踪 `try_allocate_generation` 返回 `False` 时调度器如何处理（请求留在队列下一轮重试）。

## 6. 本讲小结

- **分页 KV Cache** 把连续显存切成等长 block（默认 32 token/块），用页表把逻辑位置映射到物理 block，既消除碎片又支持前缀共享；一个 block 的 shape 约为 `[num_blocks, kv_factor, tokens_per_block, num_kv_heads, head_dim]`。
- **`KVCacheManager` 是一种资源管理器**，实现 `BaseResourceManager` 的统一接口；`PyExecutor` 只在三个固定时机调用三段式 `prepare_resources` / `update_resources` / `free_resources`，KV 细节被封在管理器内部。
- 除生命周期三件套外，它还向 `CapacityScheduler` 暴露容量接口（`get_max_resource_count` / `get_needed_resource_to_completion`），向注意力后端暴露读取接口（`get_buffers` / `get_batch_cache_indices`）。
- 存在**两套实现**：V1 是 Python 适配器 + C++ 绑定，`prepare_resources` 直接调 `add_sequence_batch`/`add_token` 干活；V2 是 Python 适配器 + 纯 Python（mypyc 可编译）运行时，分配由专用 `KVCacheV2Scheduler` 调 `try_allocate_generation`/`resize_context` 完成。
- V2 支持 **GPU/Host/Disk 多级缓存**、radix tree 前缀共享、suspend/resume 换页，策略全部写在可读的 Python 里；其容量接口目前是桩，因为它走独立的调度路径。
- 分离式服务里跨 worker 搬运这些 block 的是 `kv_cache_transceiver.py`，消费的正是本讲管理器产出的 block id 与池指针（详见 u11-l2）。

## 7. 下一步学习建议

- **u7-l2（ResourceManager 与 KV Cache 连接器）**：把本讲的 `BaseResourceManager` 放回「多种资源容器」的全景，并展开分离式服务里的 `kv_cache_connector` 与 `transceiver` 如何配合搬运 KV。
- **u8-l1（调度器与 inflight batching）**：本讲反复提到的 `CapacityScheduler`、`get_needed_resource_to_completion` 在那里有完整拆解，是理解「为什么能动态并发」的下一块拼图。
- **继续阅读源码**：想深入 V2 的页状态机与前缀共享，优先读 [runtime/kv_cache_manager_v2/AGENTS.md](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/runtime/kv_cache_manager_v2/AGENTS.md)，再顺着它给的分层逐个文件看 `_KVCache` 与 `_block_radix_tree`。
