# DDP 分布式量化

## 1. 本讲目标

本讲解决一个问题：**llm-compressor 在多卡（DDP）环境下如何量化，才能让每张卡最终得到完全一致的量化结果？**

学完本讲你应当能够：

- 说清「数据并行校准」与「权重并行压缩」的分工，以及为什么单卡代码几乎不用改就能跑多卡。
- 看懂 observer 的 `_act_sync_dict` 声明式同步表，理解不同 observer 为什么用不同的 reduce 操作（MIN/MAX/AVG/SUM）。
- 读懂 `GPTQModifier` 在 DDP 下的「贪心装箱分配 → Hessian 求和归约 → 拥有者压缩 → 广播回写」四步流程。
- 逐行讲清 `broadcast_qparams_and_cleanup` 的通信与清理逻辑，并画出它的通信时序图。

本讲是 [u3-l5 SequentialPipeline](./u3-l5-sequential-pipeline.md) 与 [u4-l1 GPTQ 算法](./u4-l1-gptq-algorithm.md) 的进阶延伸，聚焦它们在分布式场景下新增的同步环节。

## 2. 前置知识

### 2.1 校准为什么依赖数据

量化的核心是算 `scale` 与 `zero_point`。**权重量化**只需要权重本身（各卡权重相同），而**激活量化**依赖校准数据：observer 在前向时统计激活的 min/max、均方等。统计结果直接决定 scale，所以「喂了什么数据」就决定「得到什么量化参数」。

### 2.2 什么是 DDP / torch.distributed

DDP（Distributed Data Parallel）是 PyTorch 的多进程并行范式：每张 GPU 跑一个独立进程（称为一个 **rank**），进程间通过 `torch.distributed` 通信。本讲会用到的三个原语：

| 操作 | 含义 | 本讲用途 |
|------|------|----------|
| `dist.all_reduce(t, op)` | 所有 rank 的 `t` 做归约（MIN/MAX/SUM/AVG），结果写回每个 rank | 同步激活统计 |
| `dist.reduce(t, dst, op)` | 归约结果只写到目标 rank `dst` | 把各 rank 的 Hessian 汇总到拥有者 rank |
| `dist.broadcast(t, src)` | 把 `src` rank 的 `t` 广播给所有 rank | 把拥有者算出的量化参数发回所有 rank |

`async_op=True` 表示异步发起、返回一个 handle，攒一批后用 `wait_for_comms` 统一等待，能减少同步开销。

### 2.3 distributed onloading 与 offload 存储

在 [u6-l2 大模型内存策略](./u6-l2-big-model-memory.md) 会详讲：超大模型量化时，权重常被 offload 到 CPU/磁盘，按需 onload 到 GPU。这带来一个关键约束——**offload 存储是各 rank 共享的「权威副本」，不能让多个 rank 同时写**。本讲的 `only_update_onload` 参数就是为避免重复写 offload 而设计的（见 4.4）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/llmcompressor/utils/dist.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/utils/dist.py) | **本讲核心**：`broadcast_qparams_and_cleanup`（广播量化参数并清理统计）、以及 `greedy_bin_packing`/`wait_for_comms` 的薄封装（已标注为废弃，真正实现在 compressed_tensors） |
| [src/llmcompressor/modifiers/gptq/base.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py) | **本讲核心**：`GPTQModifier` 的 `compress_modules`（DDP 四步流程）、`_reduce_hessian_to_target_rank` |
| [src/llmcompressor/modifiers/quantization/quantization/base.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/base.py) | `QuantizationModifier.on_sequential_epoch_end`：RTN 权重的装箱分配与广播 |
| [src/llmcompressor/observers/base.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/base.py) | `Observer.sync_activation_stats` 与 `_act_sync_dict` 声明式同步表 |
| [src/llmcompressor/modifiers/quantization/quantization/mixin.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py) | `QuantizationMixin.sync_obs_act_stats`：在子图边界调用 observer 同步 |
| [examples/quantization_w4a16/llama3_ddp_example.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/examples/quantization_w4a16/llama3_ddp_example.py) | 可运行的 DDP GPTQ 完整示例 |

## 4. 核心概念与源码讲解

### 4.1 DDP 量化的总体设计：数据并行校准 + 权重并行压缩

#### 4.1.1 概念说明

一句话概括 llm-compressor 的 DDP 哲学（见 [distributed_oneshot.md:174-176](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/guides/big_models_and_distributed/distributed_oneshot.md#L174-L176)）：

> **尽可能少地侵入抽象层**。sequential pipeline 完全「无感」DDP；只有 modifier 与 observer 需要 DDP 感知，其余（保存、数据准备）只在少数地方加了防呆。

它把多卡加速拆成两个正交的维度：

- **数据并行校准（data-parallel calibration）**：把 N 条校准样本切成 R 份，每张卡只过自己那 N/R 条。由于激活统计依赖数据，各卡统计不同 → 需要在子图边界 **all-reduce 汇总**。
- **权重并行压缩（weight-parallel compression）**：压缩权重本身（如 GPTQ 求逆、逐列量化）是计算密集的，可以把不同模块**分配给不同 rank 并行算**，再广播回全部 rank。

#### 4.1.2 核心流程

按压缩算法的不同，llm-compressor 把 DDP 支持归纳为三种范式（见 [distributed_oneshot.md:83-155](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/guides/big_models_and_distributed/distributed_oneshot.md#L83-L155)）：

| 范式 | 代表 modifier | 权重压缩在哪做 | 本讲是否精读 |
|------|--------------|---------------|-------------|
| 各卡相同压缩 | `QuantizationModifier`（RTN） | 权重各卡相同，装箱分配仅为并行加速，算完广播 | ✅ 4.2 / 4.4 |
| 权重并行压缩 | `GPTQModifier` | 先 reduce Hessian 到拥有者，拥有者压缩，再广播 | ✅ 4.3 / 4.4 |
| 数据并行同步压缩 | `AutoRoundModifier` / `AWQModifier` | 迭代优化中 all-reduce 梯度/MSE | 仅作对比，本讲不讲 |

注意一个关键区分：**RTN（就近取整）的权重量化不依赖数据**——权重各卡相同，所以任意一张卡算某个模块得到的量化参数都一样。因此 `QuantizationModifier` 的装箱分配纯粹是为了**避免各卡重复算同一个模块**（省算力），算完后用广播补齐各卡缺失的模块。而 **GPTQ 的权重量化依赖 Hessian，Hessian 依赖数据**，所以必须先把各卡的「部分 Hessian」求和归约到拥有者，拥有者才能算出正确的全量 Hessian。

#### 4.1.3 源码精读

`QuantizationModifier` 的类文档直接点明了同步策略：

[src/llmcompressor/modifiers/quantization/quantization/base.py:31-33](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/base.py#L31-L33) 说明：在 DDP 模式下，激活 observer 的统计会在 sequential 的层边界做 all-reduce，使所有 rank 共享相同的量化参数。

#### 4.1.4 代码实践

**实践目标**：建立「单卡脚本 → 多卡脚本」只需三处改动的直觉。

**操作步骤**：打开单卡示例 [examples/quantization_w4a16/llama3_example.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/examples/quantization_w4a16/) 与多卡示例 [llama3_ddp_example.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/examples/quantization_w4a16/llama3_ddp_example.py)，用 diff 工具对比，找出三处改动：

1. `init_dist()` + `load_context()` 包裹模型加载。
2. `get_rank_partition` 切分数据集。
3. 用 `torchrun --nproc_per_node=2` 启动。

**需要观察的现象**：除这三处外，`oneshot(model=..., recipe=GPTQModifier(...))` 调用本身一字未改——这正印证了「pipeline 无感、只 modifier/observer 感知」的设计。

**预期结果**：三处改动分别对应「初始化分布式 / 数据并行 / 启动方式」，`oneshot` 与 recipe 完全一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 sequential pipeline 本身可以「无感」DDP？

> **答案**：因为 pipeline 只负责「切子图、喂数据、触发生命周期事件」。它在每个子图边界调用 modifier 的钩子；真正需要同步的是 modifier 内部对 observer/Hessian 的处理。pipeline 不关心数据是来自全量还是某个分片，也不关心有几个 rank——同步代码全写在 modifier/observer 里。

**练习 2**：RTN 权重量化（如 W4A16）各卡权重相同，为什么 `QuantizationModifier` 还要装箱分配？

> **答案**：仅为并行加速、避免 R 张卡重复算同一批模块。每个模块只由一个拥有者 rank 算，算完广播给其余 rank，既省时间又保证最终各卡一致。

---

### 4.2 激活统计的 all-reduce 同步

#### 4.2.1 概念说明

数据并行校准下，每张卡只看到一部分校准数据，因此它统计出的激活 min/max 只反映**自己那份数据**。要得到「全量数据」的统计，必须把各卡的局部统计合并。问题是：**不同 observer 的统计语义不同，合并操作（reduce op）也不同**。llm-compressor 用一个声明式字典 `_act_sync_dict` 让每个 observer 自己声明「哪些字段、用什么操作」来同步，做到零侵入。

#### 4.2.2 核心流程

同步发生在每个子图边界（`on_sequential_epoch_end` 钩子内），流程为：

1. `QuantizationMixin.sync_obs_act_stats(modules)` 遍历当前子图所有模块上的 observer。
2. 对每个 observer 调 `observer.sync_activation_stats()`，它按 `_act_sync_dict` 逐字段发起**异步 `dist.all_reduce`**。
3. 收集所有通信 handle，用 `wait_for_comms` 统一等待完成。
4. 之后各卡的激活统计就一致了，再算 `update_qparams` 得到相同的 scale/zero_point。

reduce 操作的数学含义（设第 r 张卡的局部统计为 \( s_r \)，共 R 张卡）：

- **static_minmax**：全局 min/max。全局最小值是各卡最小值的最小，全局最大值是各卡最大值的最大：
  \[ \min_{\text{global}} = \min_{r}(\min_r), \qquad \max_{\text{global}} = \max_{r}(\max_r) \]
  故 `min_vals` 用 `MIN`、`max_vals` 用 `MAX`。
- **EMA minmax / MSE**：滑动平均，跨卡取平均 → `AVG`。
- **imatrix_mse**：重要性 \( E[x^2] = \text{sum}/\text{count} \)，分子分母都要跨卡累加 → 两者都用 `SUM`。
- **memoryless_\***：每批覆盖、不累积，跨卡无需同步 → 空字典。

权重统计**永远不同步**：权重在各卡相同（加载时广播），只有激活因数据分片而不同。

#### 4.2.3 源码精读

**声明式同步表**（基类默认空字典）：[src/llmcompressor/observers/base.py:39-40](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/base.py#L39-L40) 定义了 `_act_sync_dict: Dict[str, dist.ReduceOp] = {}`，把「统计属性名 → reduce 操作」的映射作为类属性。

**同步实现**：[src/llmcompressor/observers/base.py:147-162](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/base.py#L147-L162) 的 `sync_activation_stats` 遍历 `_act_sync_dict`，对每个非空字段发起异步 all-reduce，返回 handle 列表。其文档串明确点出「权重统计无需同步，因为权重已同步，只有 data（激活）各卡不同」。

**各 observer 的声明**：

| observer | 代码位置 | `_act_sync_dict` |
|----------|---------|------------------|
| `MemorylessMinMaxObserver` | [min_max.py:16](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/min_max.py#L16) | `{}` |
| `StaticMinMaxObserver` | [min_max.py:28-31](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/min_max.py#L28-L31) | `min_vals: MIN, max_vals: MAX` |
| `MinMaxObserver`（EMA） | [min_max.py:50-53](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/min_max.py#L50-L53) | `min_vals: AVG, max_vals: AVG` |
| `MovingAverageMSEObserver` | [mse.py:58-61](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/mse.py#L58-L61) | `min_vals: AVG, max_vals: AVG` |
| `IMatrixMSEObserver` | [imatrix.py:37-40](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/imatrix.py#L37-L40) | `_imatrix_sum: SUM, _imatrix_count: SUM` |

**触发入口**：[src/llmcompressor/modifiers/quantization/quantization/mixin.py:272-294](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/mixin.py#L272-L294) 的 `sync_obs_act_stats` 用 `synced_obs` 集合去重（融合层共享同一 observer），遍历 `ACTIVATION_OBS + ("weight",)` 所有 base_name，非分布式时直接 return（no-op）。

#### 4.2.4 代码实践

**实践目标**：验证「不同 observer 用不同 reduce 操作」是必要的，而非随意。

**操作步骤**：

1. 阅读 [base.py:147-162](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/observers/base.py#L147-L162)，确认 `sync_activation_stats` 只是「按声明表逐字段 all-reduce」。
2. 做一个思维实验：假设把 `StaticMinMaxObserver` 的 `max_vals` 错写成 `MIN`，会发生什么？

**需要观察的现象 / 预期结果**：全局最大值会被错误地取成「各卡最大值的最小」，导致 scale 偏小、激活被饱和截断，量化误差显著增大。这说明 reduce 操作必须与统计语义严格对应。

#### 4.2.5 小练习与答案

**练习 1**：`IMatrixMSEObserver` 为什么同步 `_imatrix_sum` 和 `_imatrix_count` 两个字段都用 `SUM`，而不是直接同步算好的 \( E[x^2] \)？

> **答案**：因为 \( E[x^2] = \text{sum}/\text{count} \)。各卡的 sum 和 count 都是全量的一个子集，两者分别 SUM 归约后再相除，才等价于「全量数据的 \( E[x^2] \)」。若直接对各卡算好的比值取平均，会丢失样本数加权（样本多的卡和样本少的卡被等权），结果错误。

**练习 2**：为什么 `memoryless_minmax` 的 `_act_sync_dict` 是空字典？

> **答案**：memoryless observer 每次观测都覆盖上一批的统计，不跨 batch 累积；它用于权重（各卡相同）或单批即取整的场景，不存在「跨卡合并多批统计」的需求，所以无需同步。

---

### 4.3 GPTQ 的权重并行压缩：greedy_bin_packing 与 Hessian reduce

#### 4.3.1 概念说明

GPTQ 比 RTN 贵得多：它要先累积 Hessian 矩阵 \( H = 2X^\top X \)，再做带阻尼的 Cholesky 求逆与逐列误差补偿（回顾 [u4-l1](./u4-l1-gptq-algorithm.md)）。如果每张卡都把全部模块全算一遍，R 张卡就浪费了 R 倍算力。llm-compressor 的做法是：**各卡先各自累积自己数据分片上的「部分 Hessian」，再用一次 `reduce(SUM)` 汇总到模块的拥有者 rank，由拥有者独占完成昂贵的压缩，最后广播结果**。这样压缩这一步也被并行化了。

「拥有者 rank」怎么定？用 **贪心装箱（greedy bin-packing）**：按模块大小（Hessian 的行数）降序排，依次把每个模块丢给当前总负载最小的 rank，近似做到各 rank 负载均衡。

#### 4.3.2 核心流程

`GPTQModifier.compress_modules`（[gptq/base.py:292-318](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L292-L318)）的 DDP 分支四步：

```text
若非分布式：直接压缩全部模块，返回          # 单卡快路径
─────────── 以下为 DDP ───────────
1. greedy_bin_packing(模块, world_size, 权重=H.shape[0])
     → module_list（降序）, rank_to_modules, module_to_rank
2. _reduce_hessian_to_target_rank(...)
     → 对每个模块，各卡把部分 Hessian 与 num_samples 用 reduce(SUM) 发给拥有者
     → 非拥有者删掉自己的 Hessian（省内存）
3. compress_module_list(rank_to_modules[本rank])
     → 本 rank 只压缩分给自己的模块（quantize_weight 逐列量化）
4. broadcast_qparams_and_cleanup(module_list, module_to_rank, _GPTQ_Q_PARAMS)
     → 拥有者把 weight/weight_scale/weight_zero_point/weight_g_idx 广播给所有卡
     → 顺带删除 observer 统计
```

部分 Hessian 的归约数学：第 r 张卡在自己数据分片上累积的部分 Hessian 为
\[ H_r = 2\sum_{b \in \text{partition}_r} X_b^\top X_b \]
全量 Hessian 是各卡之和：
\[ H = \sum_{r=0}^{R-1} H_r \]
这正是 `dist.reduce(op=ReduceOp.SUM, dst=拥有者)` 所做的。`num_samples`（样本计数）同样需要 SUM，因为压缩时要对 Hessian 做平均（`hessian / num_samples`，见 [gptq/base.py:336](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L336)）。

#### 4.3.3 源码精读

**装箱分配**：[src/llmcompressor/modifiers/gptq/base.py:306-310](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L306-L310) 调用 `greedy_bin_packing`，`item_weight_fn=lambda mod: self._hessians[mod].shape[0]` 以 Hessian 行数（即模块输入维度）作为模块「重量」来均衡各 rank 负载。

**Hessian 归约**：[src/llmcompressor/modifiers/gptq/base.py:345-370](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L345-L370) 的 `_reduce_hessian_to_target_rank`：对每个模块发起两个异步 `dist.reduce(SUM)`（一个给 `_hessians[module]`、一个给 `_num_samples[module]`，目标都是拥有者 rank），非拥有者在归约后 `pop` 掉自己的 Hessian 以释放显存，最后 `wait_for_comms` 等待全部完成。

**拥有者独占压缩**：[gptq/base.py:315](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L315) 只压缩 `rank_to_modules[rank]`（本 rank 拥有的模块）。`compress_module_list`（[gptq/base.py:320-343](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L320-L343)）对每个模块调 `quantize_weight` 并用 `update_offload_parameter` 写回。

**广播回写**：[gptq/base.py:318](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L318) 广播 `_GPTQ_Q_PARAMS = ["weight", "weight_scale", "weight_zero_point", "weight_g_idx"]`（定义在 [gptq/base.py:44](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L44)）。注意这里广播了 `weight` 本身——因为只有拥有者改写过权重（量化后的整数权重），其余 rank 的权重还是原值。

#### 4.3.4 代码实践

**实践目标**：验证「各卡只压缩分到的模块、其余模块靠广播补齐」。

**操作步骤**：在 [gptq/base.py:315](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L315) 的 `compress_module_list(rank_to_modules[rank])` 调用前临时加一行日志（**仅阅读/本地实验，勿提交**）：

```python
# 示例代码：临时调试，验证分片
logger.info(f"[rank {rank}] owns {len(rank_to_modules[rank])}/{len(module_list)} modules")
```

然后用 `torchrun --nproc_per_node=2` 跑 [llama3_ddp_example.py](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/examples/quantization_w4a16/llama3_ddp_example.py)。

**需要观察的现象**：两个 rank 各自打印「owns 约一半模块」，且两卡日志的模块名互补（合起来等于全部 Linear）。

**预期结果**：2 卡时每卡约owns一半模块（装箱近似均衡）；最终两卡保存的 checkpoint 完全一致（广播保证）。

**待本地验证**：本实践需要多卡 GPU 环境才能运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么 GPTQ 要广播 `weight` 本身，而 `QuantizationModifier`（RTN）只广播 `weight_scale`/`weight_zero_point`？

> **答案**：RTN 把权重就地舍入为整数，各卡权重本就相同（即便分到不同 rank 算，结果也一样，拥有者算完后非拥有者手里其实也已是正确值，但为保险仍广播 scale/zp）。而 GPTQ 在压缩时用逆 Hessian 对权重做了逐列的最优补偿重写，**只有拥有者真正改写了权重**，非拥有者的权重仍是压缩前的原值，所以必须把改写后的 `weight` 广播过去。两者的广播列表正反映了这一差别（[gptq/base.py:44](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L44) vs [quantization/base.py:21](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/base.py#L21)）。

**练习 2**：`_reduce_hessian_to_target_rank` 里非拥有者为什么要 `pop` 掉自己的 Hessian？

> **答案**：归约完成后，非拥有者既不需要、也不拥有全量 Hessian（结果只写到拥有者），保留局部 Hessian 纯属浪费显存。删除它能在压缩阶段显著降内存，这也是大模型量化的关键优化之一。

---

### 4.4 量化参数广播与清理：broadcast_qparams_and_cleanup

#### 4.4.1 概念说明

无论是 GPTQ 还是 RTN，权重压缩都被分片到了各 rank。压缩完必须有一个「**收集 + 广播 + 清理**」的收尾动作，让所有 rank 对每个模块都拿到最终量化参数，并顺手释放 observer 统计占用的内存。这个动作就是 `llmcompressor.utils.dist.broadcast_qparams_and_cleanup`——本讲 dist.py 的核心函数。

#### 4.4.2 核心流程

函数对「跨所有 rank 的全部模块」逐一处理（注意它接收的 `module_list` 是**所有**模块，不是只有本 rank 的）：

```text
对 module_list 中每个 module：
  1. 若 skip_cpu 且模块在 CPU 上 → 跳过广播（offload 共享存储，无需 GPU 广播）
  2. 否则对该模块每个 qparam_name（如 weight_scale）：
       若模块确有该属性 → 异步 broadcast，源 = module_to_rank[module]（拥有者）
       把 handle 攒进 pending_comms
  3. 若模块挂有 weight_observer 且仍有统计 → 删除统计（obs.delete_statistics）
最后 wait_for_comms(pending_comms) 统一等待所有广播完成
```

通信时序示意（2 卡、4 个模块，假设装箱后 rank0 拥有 M1/M4、rank1 拥有 M2/M3）：

```text
rank0(拥有 M1,M4)          rank1(拥有 M2,M3)
  broadcast(M1.qp,src=0) ──> 收 M1.qp
  收 M2.qp              <── broadcast(M2.qp,src=1)
  broadcast(M4.qp,src=0) ──> 收 M4.qp
  收 M3.qp              <── broadcast(M3.qp,src=1)
  delete M1,M4 obs stats    delete M2,M3 obs stats
            └── wait_for_comms(全部 handle) ──┘
```

关键点：每个模块的广播源（`src`）是该模块的拥有者 rank（`module_to_rank[module]`），所以不同模块的广播来自不同 rank；用 `async_op=True` + `wait_for_comms` 把它们批量化，避免逐个同步的开销。

#### 4.4.3 源码精读

**函数全貌**：[src/llmcompressor/utils/dist.py:55-88](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/utils/dist.py#L55-L88) 的 `broadcast_qparams_and_cleanup`。

逐段说明：

- [dist.py:68-82](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/utils/dist.py#L68-L82)：遍历所有模块，`should_broadcast` 判定跳过 CPU offload 模块（`skip_cpu=True` 默认）；对每个存在的 qparam 属性发起 `dist.broadcast(as_broadcastable(param), src=module_to_rank[module], async_op=True)`，`as_broadcastable` 把张量整理成可广播的连续形态。
- [dist.py:84-86](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/utils/dist.py#L84-L86)：若模块挂了 `weight_observer` 且 `has_statistics`，调 `obs.delete_statistics(check_fused=True)` 删除统计（`check_fused` 让融合组协作式删除，见 [u3-l3](./u3-l3-calibration-observers-hooks.md)）。
- [dist.py:88](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/utils/dist.py#L88)：`_wait_for_comms(pending_comms)` 等待本批所有异步通信完成并清空列表。

**废弃薄封装**：[dist.py:19-52](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/utils/dist.py#L19-L52) 的 `greedy_bin_packing` 与 `wait_for_comms` 都带 `@deprecated` 装饰器，仅转发到 `compressed_tensors.distributed`。新代码应直接从 compressed_tensors 导入（modifier 内部已经这么做，见 [gptq/base.py:4](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L4)）。

**RTN 侧的对称调用**：[quantization/base.py:96-108](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/base.py#L96-L108) 中 `QuantizationModifier` 走同样的「装箱 → 各卡算分到的模块 → 广播」结构，区别仅是 `item_weight_fn=lambda mod: mod.weight.numel()`（按权重元素数装箱）且广播列表是 `_WEIGHT_Q_PARAMS`（不含 `weight` 本身，见 [quantization/base.py:21](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/quantization/base.py#L21)）。

**only_update_onload：避免重复写 offload**：GPTQ 在更新激活量化参数时用 `update_qparams(modules, ACTIVATION_OBS, only_update_onload=not is_src())`（[gptq/base.py:246](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L246)）。`only_update_onload` 的含义见 [calibration.py:125-128](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/quantization/calibration.py#L125-L128) 的文档串：在 DDP 下，只让一个 rank（源进程）同时写 offload+onload，其余 rank 只更新内存里的 onload 副本，从而避免多卡并发写同一份共享 offload 存储。由于各卡激活统计已同步、算出的值相同，只更新 onload 也能让每卡拿到正确数值。

#### 4.4.4 代码实践

**实践目标**：逐行读懂 `broadcast_qparams_and_cleanup` 并画出通信时序。

**操作步骤**：

1. 打开 [src/llmcompressor/utils/dist.py:55-88](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/utils/dist.py#L55-L88)。
2. 在纸上画出本节 4.4.2 的时序图，并标注：哪些模块由哪个 rank 发起广播、`pending_comms` 何时被 `wait_for_comms` 消费、observer 统计何时被删。
3. 回答：为什么广播和删统计可以放在同一个循环里、最后才统一 `wait_for_comms`？

**需要观察的现象 / 预期结果**：广播是异步发起的，删统计是本地操作、不依赖广播完成；把等待放到循环外，能让多个模块的广播在网络层面重叠执行，缩短总耗时。删统计用 `check_fused=True` 保证融合组（如 Q/K/V 共享 global_scale）的协作式删除安全。

#### 4.4.5 小练习与答案

**练习 1**：`skip_cpu=True` 时，CPU 上的模块为什么被跳过广播？

> **答案**：CPU offload 的权重存在主机内存里，是各 rank 共享访问的「权威副本」，不像 GPU 张量那样需要跨卡 broadcast。跳过它们既省通信又避免对共享存储的并发写。

**练习 2**：如果删统计时不传 `check_fused=True`，会发生什么风险？

> **答案**：融合组里多个 observer（如 q/k/v 的 observer）共享同一份 global_scale 统计。若一个 observer 用完就立即硬删，可能把同组伙伴还需要的统计也清掉，导致 global_scale 计算失败。`check_fused=True` 走协作式删除，等全组都 ready 才真正清除。

---

### 4.5 运行时启动：torchrun、init_dist 与数据分片

#### 4.5.1 概念说明

前面四节都是「分布式代码怎么写」，本节解决「怎么把它跑起来」。要把单卡脚本变成多卡，只需三处改动：初始化分布式上下文、按 rank 切分数据、用 `torchrun` 启动。模型加载和保存也有 DDP 专属的注意事项。

#### 4.5.2 核心流程

```text
torchrun --nproc_per_node=R script.py     # 启动 R 个进程，每个一张 GPU
  │
  ├─ init_dist()                          # 每个进程初始化 torch.distributed
  ├─ with load_context():                 # 避免每进程重复加载整个模型
  │     model = from_pretrained(..., device_map="auto_offload")
  ├─ ds = load_dataset(split=get_rank_partition(split, N))  # 各 rank 取 N/R 条
  ├─ oneshot(model, dataset=ds, recipe=...)                 # modifier 内部自动同步
  └─ model.save_pretrained(SAVE_DIR, save_compressed=True)  # 仅 rank0 写盘
       dist.destroy_process_group()                         # 收尾
```

`get_rank_partition(split, num_samples)` 对 S 个样本、D 个设备：每设备至少 S//D 条，余数尽量均摊，返回形如 `"train[start:end]"` 的切片字符串（[datasets/utils.py:398-431](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/datasets/utils.py#L398-L431)）。

#### 4.5.3 源码精读

**初始化分布式 + 加载模型**：[examples/quantization_w4a16/llama3_ddp_example.py:22-26](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/examples/quantization_w4a16/llama3_ddp_example.py#L22-L26) 调 `init_dist()` 并在 `load_context()` 里用 `device_map="auto_offload"` 加载，避免每进程重复占满内存。

**数据分片**：[llama3_ddp_example.py:36-38](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/examples/quantization_w4a16/llama3_ddp_example.py#L36-L38) 用 `get_rank_partition(DATASET_SPLIT, NUM_CALIBRATION_SAMPLES)` 让每 rank 只取自己那份。

**oneshot 调用不变**：[llama3_ddp_example.py:76-82](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/examples/quantization_w4a16/llama3_ddp_example.py#L76-L82) 的 `oneshot(...)` 与单卡写法完全相同——同步逻辑全藏在 modifier 内部。

**保存与收尾**：[llama3_ddp_example.py:101-111](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/examples/quantization_w4a16/llama3_ddp_example.py#L101-L111) 中 `save_pretrained` 自动只让 rank0 写盘（但所有 rank 都要调用），最后 `destroy_process_group()`。

官方基准（见 [distributed_oneshot.md:107-116](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/guides/big_models_and_distributed/distributed_oneshot.md#L107-L116)）：Llama-3-8B 单卡约 745s，2 卡约 372s，4 卡约 264s，近乎线性加速，而精度（flex_extract）几乎无损。

#### 4.5.4 代码实践

**实践目标**：亲手跑一次 DDP GPTQ 并与单卡对比一致性。

**操作步骤**：

1. 确保有 ≥2 张 GPU 的环境，安装好 `llmcompressor`。
2. 运行：`torchrun --nproc_per_node=2 examples/quantization_w4a16/llama3_ddp_example.py`。
3. 把保存目录里的某个 `weight_scale` 张量，与单卡跑同一脚本（`nproc_per_node=1`）的产物逐元素比较。

**需要观察的现象**：两个 rank 日志显示各自 owns 约一半模块；量化时间约为单卡的一半；终端打印正常的采样生成。

**预期结果**：多卡与单卡产出的量化参数在数值上**位级一致**（或仅浮点误差量级），证明「同步统计 + 广播参数」确实让各 rank 收敛到相同结果。

**待本地验证**：本实践需要多卡 GPU 环境与可访问的模型权重。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `save_pretrained` 要在所有 rank 都调用，却只有 rank0 写盘？

> **答案**：DDP 下保存涉及各 rank 持有的分片数据需要先聚合，框架内部用集合通信协调。只有 rank0 实际写文件可避免多进程并发写坏文件；但通信协调要求所有 rank 都参与调用，否则会挂起等待。这是典型的「集体通信必须全员参与」约束。

**练习 2**：如果不调用 `get_rank_partition`、让每张卡都加载全部 512 条校准样本，会怎样？

> **答案**：校准仍能跑完、结果仍一致（因为最终会 all-reduce），但失去了「数据并行」的加速与省内存收益——每卡都过全量数据，相当于校准阶段没有并行。极端情况下还可能 OOM。`get_rank_partition` 是把数据并行收益兑现的关键一步。

---

## 5. 综合实践

**任务**：把本讲的三条主线——激活统计同步、Hessian 归约、量化参数广播——串成一张完整的 DDP GPTQ 时序图，并对照源码验证每一步。

**步骤**：

1. 准备一张大白纸，横轴是时间，纵轴是 rank0 / rank1（两卡）。
2. 画出 sequential pipeline 在**一个子图**内的完整 DDP 事件流（参考 [gptq/base.py:240-247](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/gptq/base.py#L240-L247) 与 4.3、4.4 两节）：

   ```text
   a. 前向：各卡 calibrate_module hook 累积本卡部分 Hessian
   b. on_sequential_epoch_end：
      ├─ sync_obs_act_stats：all-reduce 激活统计（若有权重量化外的激活 observer）
      ├─ update_qparams(ACTIVATION_OBS, only_update_onload=not is_src())
      └─ compress_modules：
         i.   greedy_bin_packing 分配模块
         ii.  reduce(SUM) 部分Hessian + num_samples → 拥有者
         iii. compress_module_list(本rank拥有的)
         iv.  broadcast_qparams_and_cleanup 广播 weight/scale/zp/g_idx + 删统计
   ```

3. 在图上用不同颜色标注三类集合通信：all_reduce（紫）、reduce（红）、broadcast（蓝）。
4. 对照 [distributed_oneshot.md](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/guides/big_models_and_distributed/distributed_oneshot.md) 的 `DDP_GPTQ.png` 描述，检查你的时序是否覆盖了「部分 Hessian → 汇总 → 拥有者压缩 → 广播回写」四步。

**验收标准**：能不看源码，向别人讲清「为什么校准要并行、压缩也要并行、最后还要广播」这三个问题，并能指出每一步对应的源码行号。

## 6. 本讲小结

- **设计哲学**：DDP 尽量不侵入抽象层——sequential pipeline 完全无感，只有 modifier 与 observer 需要 DDP 感知；单卡脚本改三处（`init_dist` / `get_rank_partition` / `torchrun`）即可多卡运行。
- **两条并行维度**：数据并行校准（各卡切数据）解决「校准慢」，权重并行压缩（装箱分配模块）解决「压缩慢」。
- **激活统计同步**：observer 用声明式 `_act_sync_dict` 自己声明「字段 → reduce 操作」，`sync_obs_act_stats` 在每个子图边界异步 all-reduce；MIN/MAX/AVG/SUM 各对应一种统计语义，权重统计因各卡相同而不同步。
- **GPTQ 权重并行**：四步流程「贪心装箱 → reduce(SUM) 部分Hessian 到拥有者 → 拥有者独占压缩 → 广播回写」，广播列表含 `weight` 本身因为只有拥有者改写了权重。
- **广播与清理**：`broadcast_qparams_and_cleanup` 把广播（异步）与删 observer 统计放进同一循环、最后统一 `wait_for_comms`，兼顾通信重叠与内存释放。
- **offload 写保护**：`only_update_onload=not is_src()` 让非源进程只更新内存副本，避免多卡并发写共享 offload 存储。

## 7. 下一步学习建议

- **[u6-l2 大模型内存策略](./u6-l2-big-model-memory.md)**：本讲的 `only_update_onload`、`skip_cpu`、`load_context`/`auto_offload` 都依赖 distributed onloading 与磁盘 offloading 机制，下一讲会从内存角度深入这些设计。
- **[u6-l5 自定义 Observer](./u6-l5-custom-observer.md)**：若你要写自己的 observer，必须正确设置 `_act_sync_dict`——本讲的同步表就是你要实现的接口。
- **[u6-l4 自定义 Modifier](./u6-l4-custom-modifier.md)**：若你的自定义 modifier 需要在 DDP 下工作，本讲的「同步统计 + 装箱 + 广播」三段式是参考范式（对比 AutoRound/AWQ 的「数据并行同步压缩」第三种范式见 [distributed_oneshot.md:118-155](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/guides/big_models_and_distributed/distributed_oneshot.md#L118-L155)）。
