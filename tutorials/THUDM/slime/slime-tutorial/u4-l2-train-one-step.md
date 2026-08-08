# train_one_step 与 pipeline 前后向

## 1. 本讲目标

本讲是「训练后端（Megatron）层」的第二讲。上一讲（u4-l1）我们已经看清 `MegatronTrainRayActor.train_actor` 的三段式骨架：先用 `forward_only` 算 ref/teacher/actor 的对数概率，再用 `compute_advantages_and_returns` 算优势，最后调 `train()` 反向更新权重。本讲要钻进这三段背后真正的引擎——`slime/backends/megatron_utils/model.py` 里的三个函数。

读完本讲，你应当能够：

1. 说清 `train_one_step` 的六段式流程：清梯度 → 自定义 hook → 前后向 → 梯度检查 → `optimizer.step` → 学习率调度 → 释放梯度。
2. 理解 slime 如何把 Megatron 的流水线前后向引擎（`get_forward_backward_func`）当成「执行器」，把 RL 损失当「回调」注入，从而在一个 micro-batch 内完成前后向并跨 micro-batch 累积梯度。
3. 掌握 `forward_only` 这条「只读」路径：它只前向、不算梯度，用来产出 logprob/entropy/value，供优势估计与评估使用。
4. 认识 NaN/Inf 梯度检测、跳过 `optimizer.step` 的容错，以及损失为何要被 `num_microbatches / step_global_batch_size * dp_world_size` 这样奇怪的因子缩放。

## 2. 前置知识

在进入源码前，先用通俗语言铺几个概念。

### 2.1 为什么要拆 micro-batch：显存与流水线

RL 训练的一条样本可能长达数万 token（长 CoT、多轮 agent 轨迹）。把一个大批次一次性塞进 GPU 前向，激活值会撑爆显存。Megatron 的做法是把一个训练步的数据切成若干 **micro-batch（微批）**，一个一个前向，梯度**累加**到同一份 `.grad` 上，等所有微批跑完再统一 `optimizer.step()`。这就是「梯度累积（gradient accumulation）」。

当模型大到一张卡放不下，还要把层切到多张卡上，形成 **流水线并行（Pipeline Parallel, PP）**：卡 0 算前几层，把中间激活传给卡 1，卡 1 算后几层……反向时倒序回传梯度。为了让多张卡都不闲着，Megatron 用 1F1B（一个前向一个反向）等调度策略，让微批像流水线上的产品一样交错流动。本讲我们把这套机制统称为「流水线前后向引擎」。

### 2.2 slime 的「执行器 + 回调」设计

Megatron-LM 原生是为「预训练交叉熵」设计的。slime 要算的是 RL 损失（PPO clip、KL、entropy 等），但 slime **没有重写**流水线引擎，而是复用 Megatron 的 `get_forward_backward_func()`，只替换掉其中「算损失」的那一步。

这套设计的关键是一个 **forward_step 闭包**：它接收一个数据迭代器和一个模型，返回 `(output_tensor, 回调函数)`。其中：

- `output_tensor` 是模型最后一层吐出的 logits（actor）或 value（critic）。
- 回调函数接收这份 logits，返回真正的 loss 和指标。

Megatron 引擎拿到 `(output_tensor, 回调)` 后，自己负责流水线调度、反向、梯度归约；slime 只需要在回调里把 RL 损失算清楚。下面三个函数都是这个模式的不同变体。

### 2.3 关键术语速查

| 术语 | 含义 |
|------|------|
| micro-batch（微批） | 一次前向处理的最小数据片，多个微批梯度累积成一个训练步 |
| `num_microbatches` | 一个训练步要跑几个微批 |
| forward_step 闭包 | 返回 `(logits, 回调)` 的函数，是 slime 与 Megatron 引擎的接口 |
| `forward_only` | True 表示只前向不算梯度（读路径），False 表示前后向都做（写路径） |
| `step_global_batch_size` | 当前训练步跨 DP 的样本（rollout）总数，用于 loss 归一与学习率步进 |
| normalizer | Megatron 用来除 loss 的归一因子（token 数或 1） |

## 3. 本讲源码地图

本讲主要围绕一个文件，辅以三个支撑文件：

| 文件 | 作用 |
|------|------|
| [slime/backends/megatron_utils/model.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py) | **本讲主角**：定义 `forward_only`、`train_one_step`、`train` 三个核心函数 |
| [slime/backends/megatron_utils/loss.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py) | 回调实现：`loss_function`（训练）、`get_log_probs_and_entropy`/`get_values`（forward_only） |
| [slime/backends/megatron_utils/data.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/data.py) | `get_batch`：把变长样本切片、padding 成 CP-ready 微批；`DataIterator` 按既定下标取微批 |
| [slime/backends/megatron_utils/cp_utils.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/cp_utils.py) | `reduce_train_step_metrics`（指标归约）、`get_sum_of_sample_mean`（每样本平均归约） |

## 4. 核心概念与源码讲解

本讲按「先建引擎心智模型 → 单步训练 → 只读前向 → 多步驱动」的顺序展开，共四个最小模块。

### 4.1 流水线前后向引擎：slime 与 Megatron 的接口契约

#### 4.1.1 概念说明

无论是训练（`train_one_step`）还是只读推断（`forward_only`），slime 都走同一条引擎入口：

```python
from megatron.core.pipeline_parallel import get_forward_backward_func
forward_backward_func = get_forward_backward_func()
```

`get_forward_backward_func()` 会根据当前是否开了流水线并行，返回不同的调度实现（无 PP 时是简单的顺序前后向，有 PP 时是 1F1B 等）。**这对 slime 是一个黑盒执行器**：你给它 `forward_step_func`、`model`、`num_microbatches`、`forward_only` 标志，它就负责把所有微批调度好、把前后向跑完、把梯度归约好。

slime 与这个执行器之间只通过两个东西对话：

1. **`forward_step_func`**：一个闭包，引擎对每个微批调用它一次，拿到 `(output_tensor, 回调)`。
2. **`forward_only` 标志**：`True` 只跑前向并把回调返回值收集起来；`False` 跑完前向后接着反向，并返回每个微批的 reduced 指标。

这个统一接口是 slime 能「无损复用 Megatron 全部并行能力」（TP/PP/CP/EP/DP）的关键——slime 不碰流水线调度的任何细节。

#### 4.1.2 核心流程

```
get_forward_backward_func()  →  选定调度策略（顺序 / 1F1B / ...）
        │
        ▼
对每个 micro-batch（共 num_microbatches 个）：
    output_tensor, callback = forward_step_func(data_iterator, model)
    │
    ├─ forward_only=True：  收集 callback(output_tensor) 的结果 → forward_data_store
    └─ forward_only=False： callback(output_tensor) 返回 (loss, normalizer, 指标)
                            引擎用 loss/normalizer 做反向与梯度归约
                            指标 → losses_reduced
```

`forward_step_func` 内部做两件事：(a) 用 `get_batch` 从 `DataIterator` 取一个微批并切成 CP-ready 张量；(b) 调 `model(**forward_kwargs)` 得到 `output_tensor`，并把损失/指标计算打包成一个 `partial(...)` 回调返回。

#### 4.1.3 源码精读

引擎调用点在训练路径中（`forward_only=False`）：

[model.py:641-651](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L641-L651) — 训练时的引擎调用，`forward_only=False`，返回每个微批的 reduced 指标列表 `losses_reduced`：

```python
forward_backward_func = get_forward_backward_func()
losses_reduced = forward_backward_func(
    forward_step_func=_wrap_forward_step_with_microbatch_pbar(forward_step, microbatch_pbar),
    data_iterator=data_iterator,
    model=model,
    num_microbatches=num_microbatches,
    ...
    forward_only=False,
)
```

只读路径的调用点（`forward_only=True`）：

[model.py:471-480](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L471-L480) — 收集回调返回的 logprob/value 字典：

```python
forward_data_store += forward_backward_func(
    forward_step_func=forward_step_with_progress,
    ...
    forward_only=True,
)
```

两条路径都把 `data_iterator` 传给引擎，引擎在跑每个微批时调用 `forward_step` 取数。

#### 4.1.4 代码实践

**实践目标**：在源码里确认「执行器 + 回调」契约，看清 `output_tensor` 与回调分别从哪来。

**操作步骤**：

1. 打开 [model.py 的 train_one_step 内部 forward_step](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L560-L638)，找到 `output_tensor = model(**forward_kwargs)` 这一行（约 633 行）。
2. 看它的 return 语句（约 638 行）：`return output_tensor, partial(loss_function, args, batch, num_microbatches, step_global_batch_size)`。
3. 再打开 [forward_only 内部 forward_step](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L394-L445)，对比它的 return：`return output_tensor, partial(f, **output_kwargs)`。

**需要观察的现象**：两个 `forward_step` 结构几乎一样——都是「取 batch → 喂模型 → 返回 `(output_tensor, 回调)`」，区别只在回调内容：训练用 `loss_function`，只读用 `f`（即 `get_log_probs_and_entropy` 或 `get_values`）。

**预期结果**：你会确认 slime 没有重写任何流水线调度代码，所有差异都收敛在「回调」这一个接入点。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `forward_only=False` 误写成 `forward_only=True`，训练会发生什么？

**答案**：引擎只跑前向、不跑反向，因此不会产生梯度，`optimizer.step()` 更新的是零梯度（参数几乎不动），同时 `forward_backward_func` 返回的是回调收集值而非 reduced 指标，下游 `reduce_train_step_metrics` 会因取不到 `"keys"` 字段而报错。这正是为什么两条路径必须严格区分 `forward_only` 标志。

**练习 2**：为什么 `forward_step` 要返回一个 `partial(...)` 而不是直接调用损失函数？

**答案**：因为流水线并行下，`output_tensor` 在中间流水级是「占位」的——真正的 logits 只有最后一个流水级（`pipeline_last_stage`）才完整。引擎需要等激活沿流水线传到最后一 stage 后，再用回调消费这份完整 logits。把损失计算延后成回调，让引擎能在正确的时机、正确的 rank 上调用它。

---

### 4.2 train_one_step：单个训练步的六段式

#### 4.2.1 概念说明

`train_one_step` 是整个训练后端最核心的函数，它执行**一个优化器步**：跑完当前步的全部微批（前后向 + 梯度累积），检查梯度是否健康，然后更新参数、推进学习率。上一讲的 `train_actor` 最终就是通过外层 `train()` 反复调用它。

它要协调三件事：Megatron 的梯度累积（多个微批的梯度加在一起）、DistributedOptimizer 的梯度归约（跨 DP/TP 求和）、以及 RL 特有的损失缩放（保证累积后的有效梯度等于「整批一起前向」的梯度）。

#### 4.2.2 核心流程

`train_one_step` 可以拆成六段：

```
① 清梯度
   model_chunk.zero_grad_buffer() + optimizer.zero_grad()
② 自定义 before_train_step hook（可选）
   custom_before_train_step_hook(args, rollout_id, step_id, ...)
③ 定义 forward_step 闭包（不立即执行）
   闭包内：get_batch → model(**kwargs) → return (logits, partial(loss_function,...))
④ 流水线前后向（引擎跑完所有微批，梯度已累积 + 归约）
   losses_reduced = forward_backward_func(..., forward_only=False)
⑤ 梯度健康检查
   optimizer.prepare_grads() 查 Inf；optimizer.get_grad_norm() 查 NaN/Inf
   → valid_step 决定是否真的 step
⑥ 若 valid_step：
   optimizer.step()  +  opt_param_scheduler.step(increment=step_global_batch_size)
   最后再清一遍梯度（释放激活）
```

损失缩放是理解第 ④ 步的关键。Megatron 引擎内部会对一个训练步的所有微批 loss 做「求和后除以微批数」之类的归约（再除以 normalizer）。为了让「分微批累积」等价于「整批一次前向」，slime 在 `loss_function` 里把 loss 预先乘上一个补偿因子：

\[
\text{scale} = \frac{\text{num\_microbatches}}{\text{step\_global\_batch\_size}} \times \text{dp\_world\_size}
\]

这样无论数据被切成多少微批、分布在多少 DP 卡上，最终回传到优化器的有效梯度都等于「整批样本平均梯度」。具体见 [loss.py:1290-1298](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L1290-L1298)。

#### 4.2.3 源码精读

**第 ① 段 清梯度**——[model.py:549-552](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L549-L552)：

```python
# Set grad to zero.
for model_chunk in model:
    model_chunk.zero_grad_buffer()
optimizer.zero_grad()
```

`zero_grad_buffer()` 清的是 DDP 的梯度缓冲（distributed optimizer 用持久缓冲存梯度），`optimizer.zero_grad()` 清优化器侧状态。两者都要，因为梯度累积依赖「干净起步」。

**第 ③ 段 forward_step 闭包**——[model.py:577-638](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L577-L638)：先 `get_batch` 把当前微批切成 CP-ready 张量，再喂模型，最后把损失打包成 `partial(loss_function, ...)` 返回。注意它一次只处理「一个微批」，引擎会调用它 `num_microbatches` 次。

**第 ④ 段 引擎执行**——见 4.1.3 已引用的 [model.py:641-651](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L641-L651)。跑完后梯度已累积到 `.grad`。

**第 ⑤ 段 梯度检查**——[model.py:653-664](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L653-L664)：

```python
valid_step = True
grad_norm = float("nan")
if not getattr(args, "check_for_nan_in_loss_and_grad", True):
    found_inf_flag = optimizer.prepare_grads()
    if found_inf_flag:
        valid_step = False
    else:
        grad_norm = optimizer.get_grad_norm()
        ...  # 检查 grad_norm 是否 NaN/Inf
```

这里有一个容易误读的点：判断条件是 `if not args.check_for_nan_in_loss_and_grad`。也就是说，**只有当用户显式关掉 `--check-for-nan-in-loss-and-grad` 时，slime 才走这条「在 step 前检查梯度」的路径**；默认开启时，NaN/Inf 检查由 Megatron 在 `forward_backward_func` 内部更早地完成（一旦 loss 出现 NaN/Inf 就把该微批的梯度置零），`valid_step` 保持 `True`。这是一种「双重保险」：要么 Megatron 在 loss 层就拦住，要么 slime 在 grad_norm 层拦住。

**第 ⑥ 段 step + 学习率**——[model.py:673-685](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L673-L685)：

```python
if valid_step:
    update_successful, grad_norm, num_zeros_in_grad = optimizer.step()
    assert update_successful
    opt_param_scheduler.step(increment=step_global_batch_size)
# release grad
for model_chunk in model:
    model_chunk.zero_grad_buffer()
optimizer.zero_grad()
```

若梯度不健康（`valid_step=False`），直接跳过 `optimizer.step()`——参数原封不动，这一步「白跑」但不崩溃，是 RL 训练里常见的容错（某些 rollout 步 reward 极端导致梯度爆炸）。学习率调度用 `increment=step_global_batch_size` 推进，意味着调度器按「已见样本数」而非「迭代数」前进，这样动态采样导致的步数波动不会打乱学习率曲线。

最后返回 reduced 指标——[model.py:687-696](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L687-L696)：只在最后一个流水级做 `reduce_train_step_metrics`，把多个微批的指标聚合成一个字典。

#### 4.2.4 代码实践

**实践目标**：对照 Megatron 的 `get_forward_backward_func`，说清 slime 如何在一个 micro-batch 内完成前后向、并跨 micro-batch 累积梯度，最后手算一次损失缩放因子。

**操作步骤**：

1. 阅读本模块引用的 `forward_step` 闭包（[model.py:560-638](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L560-L638)）与 `loss_function` 的缩放段（[loss.py:1290-1298](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L1290-L1298)）。
2. 在笔记里画一张表，三列：`一个训练步内发生了什么`、`由 Megatron 引擎负责`、`由 slime 负责的接入点`。
3. 做一次手算：假设 `num_microbatches=4`、`step_global_batch_size=64`、`dp_world_size=8`、`calculate_per_token_loss=False`，求 `loss_function` 里给 loss 乘的缩放因子，并解释这个因子如何抵消 Megatron 的「除以微批数」与「除以 DP」。

**需要观察的现象**：缩放因子 = `4 / 64 * 8 = 0.5`。Megatron 内部会把 4 个微批的 loss 求和后除以 4（微批数），再由 DistributedOptimizer 把 DP=8 的梯度求平均（相当于除以 8）。slime 预乘 `num_microbatches` 抵消「除以微批数」，预除 `step_global_batch_size` 把 loss 归一到「每样本」，预乘 `dp_world_size` 抵消「除以 DP」。

**预期结果**：净效果是「每个样本对梯度的贡献权重为 `1/step_global_batch_size`」，等价于把 64 个样本作为一个整批做一次平均前向——这就是梯度累积数值等价性的来源。

**待本地验证**：上述因子抵消关系依赖 Megatron 内部「按微批数除、按 DP 平均」的具体实现，不同 Megatron 版本可能细节不同；若要严格验证，需对照你实际使用的 Megatron-LM 版本的 `forward_backward_func` 源码。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `train_one_step` 末尾要再调一次 `zero_grad`？

**答案**：为了释放反向传播产生的激活值与中间梯度缓冲。梯度累积只在「一个训练步内」发生，步与步之间必须彻底清零，否则上一步的梯度会污染下一步。同时清缓冲也能及时归还显存，配合 `clear_memory` 降低碎片。

**练习 2**：如果某一步梯度出现 NaN，默认配置下会发生什么？

**答案**：默认 `check_for_nan_in_loss_and_grad=True`，Megatron 在 `forward_backward_func` 内部检测到 loss 含 NaN/Inf 时会把对应微批梯度置零，从而 `optimizer.step()` 仍会执行但用的是「被清洗过的」梯度。只有当用户显式关掉该选项时，才会走到 slime 的 `prepare_grads()` → `valid_step=False` → 跳过 step 的分支。两种策略都保证训练不崩溃，只是「跳过整步」与「置零该微批」的粒度不同。

---

### 4.3 forward_only：只前向的「读」路径

#### 4.3.1 概念说明

`forward_only` 与 `train_one_step` 共享同一个引擎，但目的完全不同：它**只跑前向、不产生梯度**，用来「读取」当前模型对一批数据的 logprob/entropy/value。上一讲我们已经看到它的用途——`train_actor` 用它分别算 ref 模型、teacher 模型、actor 模型自身的对数概率，再喂给 `compute_advantages_and_returns` 算优势。评估（eval）阶段也用它。

因为它不需要梯度，整个函数被 `@torch.no_grad()` 装饰，且模型被切到 `eval()` 模式（关掉 dropout）。

#### 4.3.2 核心流程

```
@torch.no_grad()
forward_only(f, args, model, data_iterator, num_microbatches, store_prefix):
  ① 重置迭代器；模型切 eval()
  ② 定义 forward_step：get_batch → model(**kwargs) → return (logits, partial(f, **kwargs))
  ③ for step_id in num_steps_per_rollout:
         forward_data_store += 引擎(forward_only=True)   # 只收集回调返回的字典
  ④ 模型切回 train()
  ⑤ 在最后一个流水级把 forward_data_store 聚合成 {prefix+key: [tensor,...]}
```

`f` 是一个回调工厂，实参通常是 `get_log_probs_and_entropy`（算 actor/ref 的 logprob 与 entropy）或 `get_values`（算 critic 的 value）。`store_prefix` 用来给结果加前缀（如 `"ref_"`），便于在一次 `train_actor` 里区分不同模型的输出。

#### 4.3.3 源码精读

函数签名与装饰器——[model.py:344-353](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L344-L353)：

```python
@torch.no_grad()
def forward_only(f, args, model, data_iterator, num_microbatches, store_prefix="", ...):
```

`forward_step` 闭包——[model.py:394-445](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L394-L445)。与训练版的关键差异：它 `return output_tensor, partial(f, **output_kwargs)`，且 `f` 的产出是「非 loss 数据」。以 `get_log_probs_and_entropy` 为例，它返回 `(空张量, {"log_probs": [...], "entropy": [...]})`——见 [loss.py:470-561](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L470-L561)，最后一个 return 是：

```python
return torch.empty((0,), device=device), res
```

空张量表示「没有 loss 要反向」，`res` 是要收集的 logprob/entropy 字典。

模型模式切换——[model.py:448-449](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L448-L449) 切 `eval()`，[model.py:484-485](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L484-L485) 切回 `train()`。务必切回，否则下一步真正训练时 dropout 仍是关的。

结果聚合——[model.py:487-506](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L487-L506)：只在 `is_pipeline_last_stage()` 聚合（只有最后一 stage 有完整 logits），把多个微批的列表拼起来；若开了动态批，还要按原始下标重排回输入顺序。

#### 4.3.4 代码实践

**实践目标**：对比 `forward_only` 与 `train_one_step` 的 `forward_step`，找出三条「只读」与「训练」的本质差异。

**操作步骤**：

1. 并排打开 [forward_only 的 forward_step](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L394-L445) 与 [train_one_step 的 forward_step](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L560-L638)。
2. 逐项对比：装饰器、`forward_only` 标志、回调类型、模型模式、batch_keys。

**需要观察的现象与预期结果**：

| 维度 | forward_only | train_one_step |
|------|--------------|----------------|
| `@torch.no_grad()` | 有（函数级） | 无 |
| `forward_only=` | `True` | `False` |
| 回调 | `get_log_probs_and_entropy` / `get_values` | `loss_function` |
| 模型模式 | `eval()` 后切回 `train()` | `train()` |
| batch_keys | 只需 tokens/lengths 等 | 还要 advantages/returns/log_probs 等 |
| 返回 | dict（logprob/value 列表） | (指标 dict, grad_norm) |

**待本地验证**：`forward_only` 切回 `train()` 的时机——确认它在返回前一定执行，否则下游训练会静默地带着关掉的 dropout 跑。

#### 4.3.5 小练习与答案

**练习 1**：`store_prefix` 有什么用？为什么 ref 和 actor 都用同一个 `get_log_probs_and_entropy`？

**答案**：`store_prefix` 给结果键加前缀（如 `"ref_"`），让一次 `train_actor` 调用里 ref 模型的 logprob 存成 `ref_log_probs`、actor 的存成 `log_probs`，互不覆盖。复用同一个 `get_log_probs_and_entropy` 是因为「算对数概率」的逻辑与用哪个模型无关——区别只在于当前 GPU 骨架上加载的是哪份权重（由 `_switch_model` 切换，见上一讲）。

**练习 2**：为什么 `forward_only` 必须在 `is_pipeline_last_stage()` 才聚合结果？

**答案**：流水线并行下，只有最后一个流水级持有完整的最终 logits，中间级只有激活的片段。回调 `get_log_probs_and_entropy` 需要完整 logits 才能算每个 token 的 logprob，因此聚合只在最后一 stage 做，中间 stage 的 `forward_data_store` 为空、直接跳过。

---

### 4.4 train：多步驱动与训练配置

#### 4.4.1 概念说明

`train` 是 `train_one_step` 的外层驱动。一个 rollout 产生的数据往往够训练**多个优化器步**（`num_steps_per_rollout`），`train` 负责：把模型切到训练模式、配置 DDP 的梯度归约/参数收集策略、可选地重置优化器状态、按步循环调 `train_one_step`、并为每步打印日志。

它还处理两个工程细节：(1) forward pre-hook 的延迟启用（防止 checkpoint 加载错误在首次 all-gather 时扩散到所有 rank）；(2) 手动垃圾回收对齐各 rank 的 GC 时机。

#### 4.4.2 核心流程

```
train(rollout_id, model, optimizer, opt_param_scheduler, data_iterator, num_microbatches, global_batch_sizes):
  ① 断言 num_microbatches 与 global_batch_sizes 等长；重置迭代器；模型切 train()
  ② 配置 Megatron ModelConfig：grad_scale_func / no_sync_func / grad_sync_func / finalize_model_grads_func
  ③ 可选：reset_optimizer_states / manual_gc
  ④ 若用 distributed optimizer + overlap_param_gather：先禁用 forward pre-hook（首步后再启用）
  ⑤ for step_id in range(num_steps_per_rollout):
         loss_dict, grad_norm = train_one_step(..., num_microbatches[step_id], global_batch_sizes[step_id])
         step_id==0 后启用 forward pre-hook
         末 stage 打印每步日志（lr / grad_norm / loss 各项 / global_batch_size）
```

注意 `num_microbatches` 和 `global_batch_sizes` 都是**列表**，长度等于 `num_steps_per_rollout`，每个元素对应一个训练步——这允许动态批（每步样本数不同）。

#### 4.4.3 源码精读

入参与断言——[model.py:732-740](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L732-L740)：

```python
assert len(num_microbatches) == len(global_batch_sizes), ...
for iterator in data_iterator:
    iterator.reset()
for model_module in model:
    model_module.train()
```

ModelConfig 配置（梯度归约策略）——[model.py:747-766](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L747-L766)。这里把 Megatron 的 `config.grad_scale_func` 绑到 `optimizer.scale_loss`，把 `finalize_model_grads_func` 绑到 `finalize_model_grads`（跨所有模型块做最终梯度归约）。`overlap_grad_reduce` / `overlap_param_gather` 时还要绑 `no_sync_func`、`grad_sync_func`、`param_sync_func`，让引擎在前后向过程中穿插通信。

forward pre-hook 的「先禁后启」——[model.py:802-808](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L802-L808) 与 [model.py:837-844](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L837-L844)：

```python
# 训练开始前先禁用，避免 checkpoint 加载错误在首次 all-gather 扩散
if should_disable_forward_pre_hook(args):
    disable_forward_pre_hook(model, param_sync=False)
    ...
# 第一个 step 成功跑完后才启用
if step_id == 0:
    if should_disable_forward_pre_hook(args):
        enable_forward_pre_hook(model)
```

核心循环——[model.py:821-835](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L821-L835)：每个 step 把 `num_microbatches[step_id]` 和 `global_batch_sizes[step_id]` 传给 `train_one_step`。

每步日志——[model.py:867-890](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L867-L890)：只在 `dp_rank==0 and tp_rank==0 and pp_last_stage` 打印，记录每个参数组的学习率、grad_norm、各项 loss、以及「每步 global_batch_size」（注释指出「不均的步大小容易被忽略」，所以专门打这一项）。

#### 4.4.4 代码实践

**实践目标**：读 `train` 的配置段，画出「一次 rollout 内多步训练」的数据分配，并定位学习率曲线的初始化点。

**操作步骤**：

1. 阅读 [get_optimizer_param_scheduler](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L182-L235)，找到 `args.train_iters` 的估算公式，理解学习率 decay 长度如何由 `num_rollout * rollout_batch_size * n_samples_per_prompt / global_batch_size` 决定。
2. 假设 `num_steps_per_rollout=2`、`rollout_batch_size=32`、一个 rollout 产出 64 条样本，说明 `num_microbatches` 和 `global_batch_sizes` 这两个列表长度为何是 2、每步的样本数大致如何分。

**需要观察的现象**：`train_iters` 只是「估算值」用于安排学习率 decay；真正的进度跟踪靠 `opt_param_scheduler.num_steps`（按已消费样本数累计），所以动态采样导致的步数漂移只让 cosine/linear 曲线「略早或略晚到达平台」，不会错乱。

**预期结果**：列表长度 = `num_steps_per_rollout` = 2；每步的 `global_batch_sizes[step_id]` 之和应等于该 rollout 实际产出的样本总数。

**待本地验证**：具体每步切几个微批由数据打包层（下一讲 u4-l3 的 `dp_schedule`）决定，本讲只需确认「外层按步循环、内层按微批累积」的两层结构。

#### 4.4.5 小练习与答案

**练习 1**：为什么 forward pre-hook 要在第一个训练步成功后再启用，而不是一开始就开？

**答案**：分布式 optimizer 的 forward pre-hook 会在每次前向前做一次参数 all-gather。如果某个 rank 的 checkpoint 加载出错或随机初始化异常，一开始就开 hook 会让错误在首次 all-gather 时扩散到所有 rank，难以定位。先禁用、让首步在「无 hook」下跑通（此时 all-gather 是 no-op），确认无误后再启用，是一种「首步自检」的保护机制。

**练习 2**：`manual_gc`（手动垃圾回收）为什么要把 `gc.disable()` 后在各 rank 对齐时机手动 `gc.collect()`？

**答案**：Python 自动 GC 的触发时机不确定，不同 rank 会在不同时刻停顿回收内存，导致流水线/集合通信出现「有的 rank 在跑、有的 rank 在 GC」的错峰等待，拖慢整体。手动禁用自动 GC 并在固定位置统一回收，让所有 rank 的 GC 停顿对齐，避免随机卡顿。

---

## 5. 综合实践

把本讲三个函数串起来，完成一次「源码追踪 + 数值验证」综合任务。

### 任务

追踪一条数据从进入 `train()` 到梯度更新完成的完整路径，并用 slime 自带的 CPU 单测验证 `train_one_step` 的指标归约数学。

### 步骤

1. **画调用链**。从上一讲的 `MegatronTrainRayActor.train_actor` 出发，画出：
   - `train_actor` → `compute_log_prob`（包装 `forward_only` + `get_log_probs_and_entropy`）算 logprob；
   - `compute_advantages_and_returns` 算优势；
   - `train_actor` → `train`（[model.py:704](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L704)）→ 循环调 `train_one_step`（[model.py:509](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L509)）→ 引擎 `forward_backward_func` → 回调 `loss_function`（[loss.py:1220](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L1220)）→ `policy_loss_function`。
   
   在图上标注每个箭头传递的数据载体（Sample 字段 / logits / loss / 梯度）。

2. **标注六段式**。在你画的 `train_one_step` 节点上，标出 4.2.2 的六段，并指出哪一段调用了 Megatron、哪一段是 slime 自己的逻辑。

3. **运行 CPU 单测验证归约数学**。`train_one_step` 末尾用的 `reduce_train_step_metrics`（[cp_utils.py:127](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/cp_utils.py#L127)）有一组**单进程 CPU 单测**，专门验证「同批样本无论怎么分微批、分 DP、是否开 CP，报告值都一致」：

   ```bash
   pytest tests/test_metric_report.py -v
   ```

   阅读该测试文件头部注释（[tests/test_metric_report.py:1-16](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/test_metric_report.py#L1-L16)），它用 mock 的 `dp_with_cp_group` + no-op 的 `dist.all_reduce` 在单进程里复现生产调用形状。

4. **观察与预期**：

   - 调用链图应清楚显示：logits 在 `forward_step` 里产生，loss 在回调 `loss_function` 里产生，梯度由 Megatron 引擎在 `forward_backward_func` 里算并归约，参数更新在 `optimizer.step()` 里完成。
   - 单测应全部通过，证明 per-rollout-mean 与 per-token-loss 两种模式下，`train_one_step` 报告的指标与「样本如何分布」无关——这正是 4.2 里损失缩放因子的设计目标。

5. **若无法运行**：明确标注「待本地验证」。该测试依赖 `megatron` 的 stub（见测试目录的 `_cp_dist_helpers.py`），若环境未装 slime 的纯 Python 依赖，可仅做源码阅读部分（步骤 1、2）。

## 6. 本讲小结

- slime 不重写流水线引擎，而是把 Megatron 的 `get_forward_backward_func()` 当「执行器」，通过 `forward_step` 闭包 + 回调注入 RL 损失，所有 TP/PP/CP/EP/DP 并行能力都是「免费」复用的。
- `train_one_step` 是六段式：清梯度 → before hook → 定义闭包 → 流水线前后向（梯度累积 + 归约）→ 梯度健康检查 → `optimizer.step` + 学习率调度 → 释放梯度。
- 损失缩放因子 `num_microbatches / step_global_batch_size * dp_world_size` 是梯度累积数值等价性的核心：它抵消 Megatron 的「除以微批数」「除以 DP」，使每个样本的梯度权重恰为 `1/step_global_batch_size`。
- `forward_only` 是只读路径：`@torch.no_grad()` + `eval()` + `forward_only=True`，回调返回 logprob/entropy/value 字典而非 loss，供优势估计与评估使用。
- NaN/Inf 容错是双重的：默认由 Megatron 在 loss 层拦截（置零该微批梯度），关掉 `--check-for-nan-in-loss-and-grad` 时改由 slime 在 grad_norm 层拦截（跳过整步）。
- `train` 是多步外层驱动，负责 ModelConfig 配置、forward pre-hook 的「先禁后启」首步自检、手动 GC 对齐，以及按步打印学习率/grad_norm/loss 日志。

## 7. 下一步学习建议

- 本讲只讲了「一个微批如何被前向」，但**微批是怎么从变长 Sample 切出来的**尚未展开——这正是下一讲 **u4-l3「数据打包、微批调度与 loss mask」** 的主题，重点读 `data.py` 的 `get_batch` 与 `dp_schedule`。
- 想深入损失本身（PPO clip、KL、entropy、优势如何变成 per-token advantage），请读 **u4-l4「RL 损失与优势估计」**，它会精读 `loss.py` 的 `policy_loss_function` 与 `compute_advantages_and_returns`。
- 若想理解 `optimizer.step()` 之后权重如何同步给 SGLang 推理引擎，进入 **U5「权重同步与推理后端」**，从 `u5-l1` 开始。
