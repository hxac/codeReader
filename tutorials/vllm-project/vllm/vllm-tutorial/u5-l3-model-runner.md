# ModelRunner 模型运行器

## 1. 本讲目标

本讲承接 u5-l2（GPU Worker），把视线从「worker 进程」下钻到它持有的**模型运行器** `GPUModelRunner`。读完本讲，你应当能够：

1. 说清 `GPUModelRunner` 在一次 step 里扮演的角色——它是 worker 上「把调度结果翻译成张量、跑一次前向、再产出 logits」的中枢。
2. 画出 `execute_model` 的主线流程：更新批状态 → 准备输入张量 → CPU 到 GPU 拷贝 → 执行前向 → 暂存中间态 →（下一阶段）采样。
3. 理解 `InputBatch`（代码中的实际类名，本讲义按大纲约定记作 `GPUInputBatch`）如何用一个**持久化的二维 token 表**承载一个 step 内多个请求的 token。
4. 建立对 CUDA Graph「按固定 shape 预捕获、运行时重放」的高层认识。
5. 了解本版本里 routed experts（路由专家）捕获逻辑被独立成 `bind_routed_experts_capturer` 与 `get_routed_experts` 两个辅助接口的重构。

## 2. 前置知识

- **请求与调度**（u4-l1、u4-l2）：调度器 `Scheduler` 每步产出一个 `SchedulerOutput`，告诉 worker「算哪些请求、各算几个 token」。本讲不关心调度器如何决策，只关心它**已经决策完**之后，运行器怎么执行。
- **KV 缓存按 block 管理**（u4-l4）：每个请求持有 `block_ids`，注意力层据此读写 KV 缓存。运行器要把这些 block id 装配成 attention metadata。
- **持久缓冲区（persistent buffer）**：为了能用 CUDA Graph 捕获固定 shape 的执行图，运行器**预先分配**好最大尺寸的输入张量，每步只改写其内容而不重新分配。这是理解本讲所有张量操作的关键。
- **CPU pinned memory**：锁页内存，可被 GPU 直接 DMA 访问，用于实现非阻塞的 H2D（Host→Device）/ D2H 拷贝。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [vllm/v1/worker/gpu_model_runner.py](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py) | `GPUModelRunner` 所在。负责初始化设备/缓冲区、准备输入、执行前向、捕获 cudagraph、产出 logits。是本讲核心文件。 |
| [vllm/v1/worker/gpu_input_batch.py](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_input_batch.py) | `InputBatch` 类（本讲记作 `GPUInputBatch`）。维护一个 step 内多个请求的持久化 token 表与每请求采样参数。 |
| [vllm/v1/utils.py](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/utils.py) | `CpuGpuBuffer`：CPU/GPU 双端缓冲，配合 pinned memory 做非阻塞拷贝。 |
| [vllm/model_executor/layers/fused_moe/routed_experts_capturer.py](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/layers/fused_moe/routed_experts_capturer.py) | `RoutedExpertsCapturer` 及本版本新增的模块级辅助函数 `bind_routed_experts_capturer` / `get_routed_experts_attn_gid`。 |

## 4. 核心概念与源码讲解

### 4.1 GPUModelRunner 的定位与持久缓冲区

#### 4.1.1 概念说明

回顾 u5-l1：EngineCore 每轮 step 的执行环节会调用 worker 的 `execute_model`，而 worker 把真正的「跑模型」工作委托给 **model runner**。`GPUModelRunner` 就是 GPU worker 上的那个 runner。它处于调度器与模型本体之间：

```
SchedulerOutput ──► GPUModelRunner.execute_model ──► self.model(...) ──► logits
                       (准备张量/attention meta)        (真正的 Transformer)
```

`GPUModelRunner` 自己**不含**模型权重（权重是 `self.model`），它负责的是「把 `SchedulerOutput` 这个纯 CPU 的、面向请求的决策，翻译成模型 forward 需要的、面向 token 的扁平张量」，并在前向后做 logits/采样前的收尾。

它最重要的设计取舍是**持久缓冲区**：为了适配 CUDA Graph（见 4.4），所有输入张量在 `__init__` 时就按**最大尺寸**一次性分配，运行期只覆写、不重建。这也意味着每步的张量布局是**确定且可复用**的。

#### 4.1.2 核心流程

1. 构造时从 `VllmConfig` 取出关键上限：`max_num_tokens = scheduler_config.max_num_batched_tokens`（单步 token 预算上限）、`max_num_reqs = scheduler_config.max_num_seqs`（并发请求数上限）。
2. 按这两个上限**预分配**输入张量（`input_ids`、`positions`、`query_start_loc`、`seq_lens` 等），形态固定。
3. 每步 `execute_model` 只向这些固定张量里写新数据，再交给模型前向。

#### 4.1.3 源码精读

构造函数保存配置并推算两个核心上限：

[vllm/v1/worker/gpu_model_runner.py:505-506](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L505-L506) — `max_num_tokens` 与 `max_num_reqs` 决定了所有持久张量的尺寸。

随后预分配输入张量。注意它们都用 `_make_buffer`（返回 `CpuGpuBuffer`，同时持有 CPU 与 GPU 两端）：

[vllm/v1/worker/gpu_model_runner.py:763-788](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L763-L788) — `input_ids`、`positions`、`query_start_loc`、`seq_lens` 等张量在此分配，尺寸即 `max_num_tokens` 或 `max_num_reqs`。

`CpuGpuBuffer` 是理解这套张量操作的基础——它一次分配 CPU（pinned）+ GPU 两份，并提供 `copy_to_gpu` / `copy_to_cpu` 的非阻塞拷贝：

[vllm/v1/utils.py:110-142](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/utils.py#L110-L142) — 同一份数据的 `.cpu`（可 `.numpy()` 给 CPU 改写）、`.gpu`（给模型读）两端；`copy_to_gpu(n)` 把前 `n` 个元素异步拷到 GPU。

`_make_buffer` 只是对 `CpuGpuBuffer` 的薄封装，集中管理「同尺寸 CPU/GPU 双端 + numpy 视图」：

[vllm/v1/worker/gpu_model_runner.py:1046-1054](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L1046-L1054)

#### 4.1.4 代码实践

1. **实践目标**：确认持久缓冲区的「最大尺寸」来源。
2. **操作步骤**：在 [gpu_model_runner.py:505-506](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L505-L506) 处定位 `max_num_tokens` / `max_num_reqs`，再回到 [763-788](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L763-L788) 行，数一数有多少个张量按这两个上限分配。
3. **需要观察的现象**：你会发现即使某一步只有 1 个请求、1 个 token，`input_ids` 也仍是长度 `max_num_batched_tokens` 的大数组——只是只用了前若干个位置。
4. **预期结果**：建立直觉——「持久缓冲区的容量是恒定的，步与步之间只换内容、不换容器」。这正是 CUDA Graph 可行的前提。

#### 4.1.5 小练习与答案

- **练习**：为什么 `input_ids` 用 `int32` 而 `positions` 用 `int64`？
- **答案**：词表大小通常远小于 \(2^{31}\)，`int32` 足够且更省带宽/显存；而位置索引可能配合 block 表寻址、需要更大的表示范围，故用 `int64`。这与 attention 的 `slot_mapping` 一致（后文 routed experts 的 `slot_mapping` 也固定为 `int64`）。

### 4.2 InputBatch：承载一个 step 内多请求的 token

#### 4.2.1 概念说明

大纲里把这个数据结构称作 `GPUInputBatch`；在代码里，它的实际类名是 `InputBatch`，定义在 `gpu_input_batch.py`。它是 **GPU runner 视角下「当前在算的这一批请求」的持久状态容器**。

关键直觉：模型一次前向只看**一维的、连续的 token 序列**，但调度器的输出是**按请求**组织的（「请求 A 算 2 个新 token、请求 B 算 5 个、请求 C 算 3 个」）。`InputBatch` 就是这两者之间的桥：它在 CPU 侧维护一张「每个请求一行、每行装该请求全部 token」的二维表，运行器据此把需要算的 token「摊平」成一维。

#### 4.2.2 核心流程

- **存储**：`token_ids_cpu` 是形状 `(max_num_reqs, max_model_len)` 的二维张量——每个请求占一行，该请求的 prompt + 已生成 token 顺序填入。这是「持久状态」，跨 step 保留。
- **增**：`add_request` 把一个请求塞进某一行，写入它的 prompt token、已生成 token、采样参数、block 表。
- **删+整理**：请求完成后对应行变空，`condense` 把还活着的请求「下移」填补空位，保证有效请求总是排在表的前若干行（连续紧凑），方便后续向量化处理。

#### 4.2.3 源码精读

二维 token 表与 numpy 视图（注意它直接在 CPU 上、可被 numpy 改写）：

[vllm/v1/worker/gpu_input_batch.py:134-140](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_input_batch.py#L134-L140) — `token_ids_cpu_tensor` 形状 `(max_num_reqs, max_model_len)`，`token_ids_cpu` 是它的 numpy 视图。

`add_request` 负责把请求写入指定行：拷贝 prompt token、已生成 token，登记采样参数（temperature / top_p / top_k 等），并 `block_table.add_row` 记录该请求的 KV block：

[vllm/v1/worker/gpu_input_batch.py:366-398](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_input_batch.py#L366-L398)

随后登记采样参数（贪婪/随机分流、top_p/top_k 等）：

[vllm/v1/worker/gpu_input_batch.py:400-417](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_input_batch.py#L400-L417) — 注意 `sampling_type == GREEDY` 时把 temperature 显式置 0（避免后续除零），并把请求归入 `greedy_reqs`/`random_reqs` 集合，便于采样器分批处理。

`condense` 把空洞填满——找出最大非空索引与最小空索引，把存活请求的整行（token、是否为 token id、prompt embeds 等）下移：

[vllm/v1/worker/gpu_input_batch.py:708-767](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_input_batch.py#L708-L767)

#### 4.2.4 代码实践

1. **实践目标**：理解「二维 token 表 → 一维 token 流」的映射。
2. **操作步骤**：阅读 [gpu_input_batch.py:134-140](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_input_batch.py#L134-L140) 与下文 4.3.3 中 `_prepare_inputs` 的 `np.repeat` / `index_select` 逻辑。
3. **需要观察的现象**：设请求 0 调度 2 个 token、请求 1 调度 5 个 token、请求 2 调度 3 个 token，`req_indices` 会变成 `[0,0,1,1,1,1,1,2,2,2]`——这正是把「每请求几行」拍平成「连续一维」的过程。
4. **预期结果**：能用自己的话讲清「为什么 `token_ids_cpu` 要做成二维、而模型前向只吃一维」——二维是状态存储（跨 step 保留），一维是计算视图（单步快照）。

#### 4.2.5 小练习与答案

- **练习 1**：`condense` 为什么必要？不调它会发生什么？
- **答案**：请求完成后会留下「空洞」，若不整理，有效请求会散落在表的各行，导致 `num_reqs` 之后的行混着已删请求的脏数据，后续向量化索引（如 `token_ids_cpu[:num_reqs]`）会读到错误内容。`condense` 把存活请求压实到前 `num_reqs` 行，保证「前 N 行即当前批次」。
- **练习 2**：`token_ids_cpu` 为什么是 CPU 张量而非 GPU？
- **答案**：调度与状态维护是 CPU 逻辑（numpy 索引/搬运成本低、且要在 CPU 上决定要算哪些 token），只有真正喂给模型的那一维快照才需要拷到 GPU。

### 4.3 execute_model：一次前向的总编排

#### 4.3.1 概念说明

`execute_model` 是 worker 之外世界与模型本体之间唯一的「正式入口」。它接收 `SchedulerOutput`，最终产出 logits（或中间张量）。理解它的主干，就理解了「运行器到底干了什么」。

注意一个**两阶段**设计：在本版本的异步调度（async scheduling）下，`execute_model` 只负责**前向 + 暂存中间态**（返回 `None`），真正的**采样**被挪到紧随其后的 `sample_tokens` 方法里。这样 CPU 调度（为下一步做准备）可以与 GPU 前向重叠，`sample_tokens` 在前向真正结束后才消费 logits。两个阶段通过实例属性 `execute_model_state` 传递中间数据。

#### 4.3.2 核心流程

`execute_model` 的主线可拆成五段（伪代码）：

```
def execute_model(scheduler_output):
    # 1) 更新批状态：把调度器对本步的增删改动落到 InputBatch
    deferred = self._update_states(scheduler_output)
    if 没有要算的 token: return EMPTY_MODEL_RUNNER_OUTPUT

    # 2) 准备输入张量：把"每请求几个 token"拍平成一维，登记 logits_indices
    logits_indices, spec_meta = self._prepare_inputs(scheduler_output, num_scheduled_tokens)

    # 3) 决定批执行方式与 padding（cudagraph 模式、是否分 ubatch）
    cudagraph_mode, batch_desc, ... = self._determine_batch_execution_and_padding(...)

    # 4) 预处理：embedding、CPU→GPU 拷贝，得到 input_ids/positions
    input_ids, embeds, positions, ... = self._preprocess(scheduler_output, num_tokens_padded, ...)

    # 5) 执行前向，算 logits，把中间态存进 execute_model_state
    with set_forward_context(attn_metadata, ...):
        hidden = self._model_forward(input_ids, positions, ...)
    logits = self.model.compute_logits(hidden[logits_indices])
    self.execute_model_state = ExecuteModelState(scheduler_output, logits, ...)
    return None     # 采样留给 sample_tokens()
```

#### 4.3.3 源码精读

**入口与状态校验**：`execute_model_state is not None` 表示上一轮的前向还没被 `sample_tokens` 消费，此时禁止再次前向——这是一个防呆断言：

[vllm/v1/worker/gpu_model_runner.py:4165-4174](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L4165-L4174)

**第一段：更新批状态 + 空步早退**。`_update_states` 把调度器的增删同步到 `InputBatch`（新请求 `add_request`、完成的请求移除等）；若本步无 token 要算，直接返回空输出：

[vllm/v1/worker/gpu_model_runner.py:4198-4249](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L4198-L4249) — 取出每请求调度 token 数 `num_scheduled_tokens_np`，调用 `_prepare_inputs`。

**第二段（核心）：准备输入张量**。`_prepare_inputs` 把二维表拍平成一维：`np.repeat` 把请求索引复制成每个 token 一份，`index_select` 从持久表里 gather 出实际 token id，并构建 attention 边界 `query_start_loc`：

[vllm/v1/worker/gpu_model_runner.py:1981-2024](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L1981-L2024) — `[2,5,3]` 的调度经 `np.repeat` 得 `req_indices=[0,0,1,1,1,1,1,2,2,2]`，再用 `torch.index_select` 从 `token_ids_cpu_tensor` 抽出对应 token 写入 `input_ids.cpu`。

[vllm/v1/worker/gpu_model_runner.py:2073-2079](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L2073-L2079) — `query_start_loc` 记录每个请求在扁平 token 流里的起止偏移（前缀和），供 attention kernel 切分 query。

**第四段：预处理与 H2D**。`_preprocess` 处理多模态 embedding/prompt embeds，并最终在 GPU 上给出 `input_ids` 与 `positions`（普通模型直接复用预分配的 `self.positions` 切片）：

[vllm/v1/worker/gpu_model_runner.py:3652-3663](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L3652-L3663)

**第五段：真正的前向**。在 `set_forward_context`（注入 attention metadata 等）上下文里调用 `_model_forward`，它最终调到 `self.model(...)`：

[vllm/v1/worker/gpu_model_runner.py:4438-4453](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L4438-L4453) — `model_output = self._model_forward(...)`。

[vllm/v1/worker/gpu_model_runner.py:3878-3908](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L3878-L3908) — `_model_forward` 仅是 `self.model(input_ids=..., positions=..., ...)` 的薄封装，独立成方法便于子类覆盖、也便于单独审视「真正的前向」这一步。

算完 logits 后把中间态打包存入 `execute_model_state` 并返回 `None`：

[vllm/v1/worker/gpu_model_runner.py:4504-4523](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L4504-L4523)

**第二阶段：采样**。`sample_tokens`（`@torch.inference_mode`）取出暂存的 logits，调 `_sample` → `self.sampler(...)`：

[vllm/v1/worker/gpu_model_runner.py:4540-4577](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L4540-L4577) — 解包 `execute_model_state`，调用 `self._sample(logits, spec_decode_metadata)`。

[vllm/v1/worker/gpu_model_runner.py:3691-3705](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L3691-L3705) — 无推测解码时直接 `self.sampler(logits=logits, sampling_metadata=...)`，采样参数从 `input_batch.sampling_metadata` 取（与 4.2 登记呼应）。

#### 4.3.4 代码实践

1. **实践目标**：定位「输入张量准备」与「forward 调用」两处，串成一条链。
2. **操作步骤**：
   - 在 [gpu_model_runner.py:4246](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L4246) 找到 `self._prepare_inputs(...)` 调用。
   - 顺着 `_prepare_inputs`（[1960](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L1960)）看 `np.repeat` 与 `index_select` 如何把二维表拍平成一维 `input_ids`。
   - 再到 [4438](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L4438) 看 `_model_forward` 真正调 `self.model`。
3. **需要观察的现象**：`InputBatch`（二维状态表）只在 CPU 被改写，进入模型的是由它「gather」出来的一维快照。
4. **预期结果**：能画出 `SchedulerOutput → _prepare_inputs(拍平) → _preprocess(H2D) → _model_forward → logits` 的链路，并说明 `InputBatch` 承担的是「状态存储」，而模型看到的是「单步快照」。

#### 4.3.5 小练习与答案

- **练习**：为什么把采样拆到 `sample_tokens`，而不是直接在 `execute_model` 末尾采样？
- **答案**：在异步调度下，`execute_model` 返回 `None` 表示「前向已发起」，CPU 立刻可以为**下一步**做调度与状态更新，让 CPU 准备与 GPU 前向重叠；真正的采样要等 GPU logit 就绪，故推迟到 `sample_tokens`。这种「前向/采样两阶段」是把调度与计算解耦、榨取重叠的关键（详见 u5-l1 的 busy loop）。

### 4.4 CUDA Graph 的捕获与重放

#### 4.4.1 概念说明

每次模型前向都要发射成百上千个 CUDA kernel，每个 kernel 的**启动开销**（launch overhead）累加起来在 decode 阶段（单步 token 少、计算轻）会成为主要瓶颈。CUDA Graph 把「一整段 kernel 序列」录制为一张图，之后只需一次 `replay` 即可整体重放，把大量 launch 合并成一次。

但 CUDA Graph 有一条硬约束：**捕获时输入 shape 固定，重放时 shape 也必须一致**。vLLM 的应对是——对一批**常见的 batch size**（确切地说是 token 数）逐一预捕获图，运行时按当前 step 的实际 token 数挑选最接近的已捕获图，把数据写进那张图对应的固定缓冲区再重放。这也解释了 4.1 为何所有缓冲区要「按最大尺寸预分配、内容可变」。

#### 4.4.2 核心流程

1. 启动期 `capture_model()` 被调用一次。
2. 它从 `cudagraph_dispatcher` 取得一批待捕获的「形状描述」（`BatchDescriptor`，含 token 数等），**先大后小**地捕获，让小图复用大图已分配的显存池。
3. 对每个形状：先做若干次 warmup（`_dummy_run`），再在 `torch.cuda.graph` 上下文里正式捕获（同样是一次 `_dummy_run`，但开启了 cudagraph 模式）。
4. 运行时，`_determine_batch_execution_and_padding` 据当前 token 数决定 `cudagraph_mode`（NONE / PIECEWISE / FULL）与 padding，把实际数据对齐到某张已捕获图。

#### 4.4.3 源码精读

`capture_model` 入口与「大形状优先」策略：

[vllm/v1/worker/gpu_model_runner.py:6786-6804](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L6786-L6804) — 若 `cudagraph_mode == NONE` 则跳过；否则开启捕获许可并记录起始显存。

[vllm/v1/worker/gpu_model_runner.py:6843-6864](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L6843-L6864) — 在 `graph_capture` 上下文里，按 dispatcher 给出的 `(runtime_mode, batch_descs)` 逐形状调用 `_capture_cudagraphs`；`start_free - end_free` 即为捕获占用的显存。

`_warmup_and_capture` 展示了「先热身、后录制」的两步：热身用 `CUDAGraphMode.NONE`（不捕获），正式捕获那次开启 `cudagraph_runtime_mode`：

[vllm/v1/worker/gpu_model_runner.py:6905-6935](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L6905-L6935)

运行时模式决策入口（每步都调用）：

[vllm/v1/worker/gpu_model_runner.py:3931-4038](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L3931-L4038) — `_determine_batch_execution_and_padding`：在 valid/invalid 模式集合与 DP 跨 rank 同步约束下，用 `dispatch_cudagraph` 选定本步的 `cudagraph_mode` 与 `batch_descriptor`。

#### 4.4.4 代码实践

1. **实践目标**：建立「固定 shape 才能捕获」的直觉，并理解 padding 的作用。
2. **操作步骤**：阅读 [gpu_model_runner.py:6848-6857](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L6848-L6857)，注意循环变量是「形状描述」而非具体请求；再看 4.3.3 中 `_determine_batch_execution_and_padding` 产出的 `num_tokens_padded`。
3. **需要观察的现象**：当本步实际 token 数（如 13）不等于任何已捕获形状时，运行器会把它 **padding** 到最近的已捕获形状（如 16），多余位置填 0，重放对应图后只取有效部分。
4. **预期结果**：能解释「为什么 cudagraph 只能捕获固定输入 shape，以及 vLLM 如何用 padding + 预捕获一组典型形状来覆盖动态的请求量」。

#### 4.4.5 小练习与答案

- **练习**：捕获「大形状优先」有什么好处？
- **答案**：CUDA Graph 重用同一块显存池，先捕获大形状可让池子一次性按最大需求分配，之后捕获的小形状直接复用该池，避免反复申请/释放造成的碎片与峰值浪费。注释 [6801-6804](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L6801-L6804) 即点明此意。
- **练习**：为什么 decode 阶段尤其受益于 cudagraph？
- **答案**：decode 单步 token 少、计算量小，相对而言 kernel launch 开销占比高；cudagraph 把大量 launch 合并成一次 replay，对 decode 的端到端延迟改善最显著。

### 4.5 routed experts 捕获的独立化重构（本版本重点）

#### 4.5.1 概念说明

`--enable-return-routed-experts` 是面向 MoE 模型的功能：在每个 transformer 层的 MoE 路由（router）选出 top-k 专家后，把这些「路由决策」（`topk_ids`）**捕获**下来，供上层（如专家负载均衡分析、EPLB 专家重排）使用。这件事需要在 forward 过程中「顺手记录」，因此 runner 会在模型初始化时给每个 `MoERunner` 的 router 挂上一个捕获回调。

本版本（f0de1a6 → c2881ce）对这部分做了一次**关注点分离**的重构：把原先散在 `GPUModelRunner` 里的几个方法，外移/合并到 `routed_experts_capturer.py` 模块，使 runner 更瘦、逻辑更内聚。

#### 4.5.2 核心流程（重构前 vs 重构后）

| 关注点 | 重构前（`GPUModelRunner` 的方法） | 重构后 |
| --- | --- | --- |
| 给每个 router 挂捕获回调 | `_bind_routed_experts_capturer(self, capturer)` | 模块级函数 `bind_routed_experts_capturer(model, capturer)` |
| 找 attention 所在的 KV cache 组 id | `_get_attention_kv_cache_gid()` | 模块级函数 `get_routed_experts_attn_gid(kv_cache_config)`，且在 `RoutedExpertsCapturer.__init__` 内算好存为 `self.attn_gid` |
| 取本步路由快照（异步路径） | 内联在 `execute_model` 里手写 `clone()` | 抽成 `GPUModelRunner.get_routed_experts(num_tokens)` 辅助方法 |
| 步首清空捕获缓冲 | `self.routed_experts_capturer.clear_buffer()` | **删除**——缓冲由下一步 forward 在默认流上整体覆写，无需显式清空 |

#### 4.5.3 源码精读

**导入新增的绑定函数**：

[vllm/v1/worker/gpu_model_runner.py:64-67](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L64-L67) — 从 `routed_experts_capturer` 同时导入 `RoutedExpertsCapturer` 与新增的 `bind_routed_experts_capturer`。

**初始化 capturer 时改用模块级函数绑定，且 capturer 自己持有 `attn_gid`**。`RoutedExpertsCapturer` 现在接收 `kv_cache_config`，在内部算出 `attn_gid`；`bind_routed_experts_capturer(self.model, ...)` 取代了原先的实例方法：

[vllm/v1/worker/gpu_model_runner.py:7668-7678](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L7668-L7678)

模块侧：`bind_routed_experts_capturer` 遍历模型的 `MoERunner`，按「monolithic kernel」或「BaseRouter」两种情况挂上 `capture_fn`（最终调 `capturer.capture(layer_id, topk_ids)`），并对不支持的情况抛错：

[vllm/model_executor/layers/fused_moe/routed_experts_capturer.py:248-294](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/layers/fused_moe/routed_experts_capturer.py#L248-L294)

`RoutedExpertsCapturer.__init__` 内部用 `get_routed_experts_attn_gid(kv_cache_config)` 计算 `attn_gid`（即第一个 `FullAttentionSpec` 组的下标，保证与调度侧一致）：

[vllm/model_executor/layers/fused_moe/routed_experts_capturer.py:86-123](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/layers/fused_moe/routed_experts_capturer.py#L86-L123)

**取路由快照抽成 `get_routed_experts` 辅助方法**。原先散在 `execute_model` 内联的 `clone()` 逻辑被收拢进这个方法，未初始化时返回 `None`：

[vllm/v1/worker/gpu_model_runner.py:7655-7666](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L7655-L7666)

**slot_mapping 引用从实例字段改为 capturer 的属性**。重构前用 `self.routed_experts_attn_gid` 间接取，现在直接读 `self.routed_experts_capturer.attn_gid`：

[vllm/v1/worker/gpu_model_runner.py:2353-2354](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L2353-L2354)

异步路径取快照的调用点也简化为一行：

[vllm/v1/worker/gpu_model_runner.py:4795-4798](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L4795-L4798) — `routed_experts_snapshot = self.get_routed_experts(scheduler_output.total_num_scheduled_tokens)`。

**步首的 `clear_buffer()` 已删除**。重构前每步开头会 `clear_buffer()`；现在缓冲在下一步 forward 时被默认流整体覆写，因此不再需要显式清空——少一次同步点。模块侧 `get_device_buffer` 的文档明确写出了这一约定：

[vllm/model_executor/layers/fused_moe/routed_experts_capturer.py:221-245](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/model_executor/layers/fused_moe/routed_experts_capturer.py#L221-L245) — 注释指出「the tensor is shared; callers must either clone or fully drain it before the next forward pass overwrites it」，印证了「不再需要 clear_buffer」。

#### 4.5.4 代码实践

1. **实践目标**：通过 diff 理解「方法外移」类重构的判读方法。
2. **操作步骤**：运行 `git log --oneline -3 -- vllm/model_executor/layers/fused_moe/routed_experts_capturer.py` 与 `git diff f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8..HEAD -- vllm/v1/worker/gpu_model_runner.py`，对照本节表格，逐一确认「哪些方法被删、哪些函数被新增」。
3. **需要观察的现象**：`gpu_model_runner.py` 里 `_get_attention_kv_cache_gid`、`_bind_routed_experts_capturer` 两个方法整体消失；`clear_buffer()` 调用消失；新增 `get_routed_experts` 方法。
4. **预期结果**：能用一句话总结这次重构的净效果——「把 routed experts 的捕获绑定与 attn-gid 推导从 runner 实例方法移到 capturer 模块，并移除冗余的步首清缓冲」。

#### 4.5.5 小练习与答案

- **练习**：为什么删除步首的 `clear_buffer()` 是安全的？
- **答案**：捕获缓冲是一个固定大小的 device 张量，下一步 forward 时每个位置都会被新的路由数据整体覆写；上层取快照时会 `clone()` 出私有副本（见 `get_routed_experts`）。既然每步都整体覆写，就没有「残留旧值」的风险，显式清空反而多一次无谓的内核与同步点。

## 5. 综合实践

把本讲三块主线串起来，完成一次「纸上跟踪」：

**场景**：某一步 `SchedulerOutput` 指示——请求 R0 调度 2 个新 token、请求 R1 调度 5 个、请求 R2 调度 3 个（共 10 个 token），三者共享一段 system prompt（前缀缓存已命中）。

请按顺序回答并标注对应源码位置：

1. **状态层**（4.2）：这 3 个请求在 `InputBatch` 的 `token_ids_cpu` 里分别占据哪几行？它们的 block_ids 如何登记？（[gpu_input_batch.py:366-398](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_input_batch.py#L366-L398)）
2. **拍平层**（4.3）：`_prepare_inputs` 会把这 10 个 token 的 `req_indices`、`positions`、`input_ids` 分别写成什么？（[gpu_model_runner.py:1981-2024](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L1981-L2024)）
3. **前向层**（4.3 / 4.4）：这 10 个 token 进入 `_model_forward` → `self.model(...)`；若 10 不是已捕获形状，`_determine_batch_execution_and_padding` 会如何 padding？（[gpu_model_runner.py:3931-4038](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L3931-L4038)）
4. **采样层**（4.3）：前向返回 `None` 后，`sample_tokens` 如何从 logits 采出 3 个请求各自的下一个 token？（[gpu_model_runner.py:4540-4577](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L4540-L4577)）

> 若有可运行环境，可用一个小模型（如 `facebook/opt-125m`）跑 `LLM.generate`，并在 `gpu_model_runner.py` 的 `_model_forward` 处加一行 `print(input_ids.shape, positions.shape)` 日志，观察每步输入张量长度随 batch 变化（注意：修改源码仅为本地观察，勿提交）。无 GPU 环境则按上表完成纯阅读型跟踪即可——**待本地验证**。

## 6. 本讲小结

- `GPUModelRunner` 是 GPU worker 上的模型运行中枢：把 `SchedulerOutput` 翻译成模型 forward 所需的扁平张量，跑前向，产出 logits。
- 所有输入张量在 `__init__` 按 `max_num_batched_tokens` / `max_num_seqs` **预分配**为持久缓冲区（`CpuGpuBuffer` 提供 CPU/GPU 双端 + 非阻塞拷贝），每步只改内容、不重建——这是 CUDA Graph 与高效 H2D 的共同前提。
- `InputBatch`（代码类名）维护一张 `(max_num_reqs, max_model_len)` 的二维 token 表作为**跨 step 的状态**；`add_request` 写入、`condense` 压实空洞，保证「前 N 行即当前批次」。
- `execute_model` 主线为「更新批状态 → `_prepare_inputs` 拍平 → `_preprocess` H2D → `_model_forward` 前向 → 暂存 `execute_model_state` → `sample_tokens` 采样」；异步调度下前向与采样分两阶段以重叠 CPU 调度与 GPU 计算。
- CUDA Graph 按**固定 shape 预捕获**一组典型 batch，运行时靠 padding 对齐到已捕获图后 replay，显著降低 decode 阶段的 kernel launch 开销。
- 本版本把 routed experts 捕获的绑定（`bind_routed_experts_capturer`）与 attn-gid 推导（`get_routed_experts_attn_gid`）从 runner 方法外移到 capturer 模块，新增 `get_routed_experts` 快照辅助方法，并删除冗余的步首 `clear_buffer()`。

## 7. 下一步学习建议

- **u6-l1 模型注册机制**：本讲里 `self.model` 是怎么被实例化出来的？下一讲讲 `_ModelRegistry` 如何把 HF 架构名映射到具体模型类。
- **u6-l5 注意力层抽象**：本讲只到「把 attention metadata 装配好」，注意力层如何据此选择 backend、写 KV 缓存，留待注意力层讲义展开。
- **u8-l2 CUDA Graphs 捕获与重放 / u8-l3 torch.compile**：本讲只讲 runner 视角的 cudagraph 高层逻辑，编译分段（piecewise）、partition rules 等深入机制在专家层讲义。
- **进阶阅读**：直接通读 [gpu_model_runner.py:4165](https://github.com/vllm-project/vllm/blob/c2881ce60302b5455867d2c29cdfae5fbeddecac/vllm/v1/worker/gpu_model_runner.py#L4165) 起的 `execute_model` 全文，对照本讲的五段划分逐行确认。
