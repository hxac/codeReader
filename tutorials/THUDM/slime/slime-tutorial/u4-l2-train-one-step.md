# train_one_step 与 pipeline 前后向

## 1. 本讲目标

本讲钻进 slime 训练工人真正「干活」的最内层——`slime/backends/megatron_utils/model.py` 里的三个核心函数：`forward_only`、`train_one_step`、`train`。

上一讲（u4-l1）我们看到 `MegatronTrainRayActor.train_actor` 把一轮训练拆成三步：先用 `forward_only` 算参考/教师/当前模型的对数概率，再用 `compute_advantages_and_returns` 算优势，最后调 `train` 反向更新。本讲要回答的是：这三步里每一步在 GPU 上到底发生了什么。

学完后你应该能够：

- 说清 slime 如何把 Megatron 的流水线引擎 `get_forward_backward_func()` 当成「执行器」，通过一个 `forward_step` 闭包把 RL 损失当「回调」注入，从而在一个 micro-batch 内完成前后向、跨 micro-batch 累积梯度。
- 说清 `forward_only` 这条「只读」路径：它只前向、不算梯度，用来产出 logprob / entropy / value，供优势估计与评估使用。
- 说清 `train_one_step` 的六拍节拍：`zero_grad → 自定义 hook → 流水线前后向 → 梯度 NaN/Inf 检查 → optimizer.step + LR scheduler.step → 释放梯度 + 归约指标`。
- 认识梯度异常时的容错：默认交给 `optimizer.step()` 内部检测，关闭开关时 slime 自己检测并跳过 step。
- 看懂 `train` 对一个 rollout 内多个训练步的编排，以及它对 DDP 同步回调、optimizer 状态、学习率调度所做的配置。

## 2. 前置知识

进入源码前，先用通俗语言建立三个基础概念。

### 2.1 为什么要拆 micro-batch：显存、流水线与梯度累积

RL 训练的一条样本可能长达数万 token（长 CoT、多轮 agent 轨迹）。把一个大批次一次性塞进 GPU 前向，激活值会撑爆显存。Megatron 的做法是把一个训练步的数据切成若干 **micro-batch（微批）**，一个一个前向，梯度**累加**到同一份 `.grad` 上，等所有微批跑完再统一 `optimizer.step()`——这就是「梯度累积（gradient accumulation）」。

当模型大到一张卡放不下，还要把层切到多张卡上，形成 **流水线并行（Pipeline Parallel, PP）**：卡 0 算前几层、把中间激活传给卡 1，卡 1 算后几层……反向时倒序回传梯度。为了让多张卡都不闲着，Megatron 用 1F1B（一个前向一个反向）等调度让微批像流水线上的产品一样交错流动。本讲把这套机制统称为「流水线前后向引擎」。

### 2.2 slime 的「执行器 + 回调」设计

Megatron-LM 原生是为「预训练交叉熵」设计的；slime 要算的是 RL 损失（PPO clip、KL、entropy 等），但 slime **没有重写**流水线引擎，而是复用 Megatron 的 `get_forward_backward_func()`，只替换掉「算损失」那一步。

这套设计的关键是一个 **forward_step 闭包**。它接收一个数据迭代器和一个模型，返回 `(output_tensor, 回调)`：

- `output_tensor` 是模型最后一层吐出的 logits（actor）或 value（critic）。
- 回调接收这份 logits，返回真正的 loss、归一化因子与日志。

Megatron 引擎拿到 `(output_tensor, 回调)` 后，自己负责流水线调度、反向、梯度归约；slime 只需在回调里把 RL 损失算清楚。本讲的三个函数都是这个模式的不同变体。

### 2.3 on-policy 训练里「前向」的双重用途

在 RL 训练里，前向 pass 有两种完全不同的用途：

- **算优势（advantage）**：用当前/参考/教师模型对 rollout 出来的 token 算对数概率。这只用前向、不需要梯度 → `forward_only`。
- **更新策略**：前向算 loss 再反向传播更新权重 → `train_one_step`。

这种「先 forward_only 算 logprob，再 train 反向」的两段式，是 RL 训练区别于普通 SFT 的关键结构，上一讲已在 `train_actor` 里见过。

### 2.4 关键术语速查

| 术语 | 含义 |
|------|------|
| micro-batch（微批） | 一次前向处理的最小数据片，多个微批梯度累积成一个训练步 |
| `num_microbatches` | 一个训练步要跑几个微批 |
| forward_step 闭包 | 返回 `(output_tensor, 回调)` 的函数，是 slime 与 Megatron 引擎的接口 |
| `forward_only` 标志 | `True` 只前向不算梯度（读路径），`False` 前后向都做（写路径） |
| `step_global_batch_size` | 当前训练步跨 DP 的样本（rollout）总数，用于 loss 归一与学习率步进 |
| normalizer | Megatron 用来除 loss 的归一因子（token 数或 1） |

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `slime/backends/megatron_utils/model.py` | **本讲主角**。定义 `forward_only`、`train_one_step`、`train`，以及 LR 调度 `get_optimizer_param_scheduler`、模型/优化器装配 `setup_model_and_optimizer` 等。 |
| `slime/backends/megatron_utils/actor.py` | 上层工人。`train_actor` / `train_critic` 调用本讲的三个函数，是它们的调用方。 |
| `slime/backends/megatron_utils/data.py` | `DataIterator` 与 `get_batch`：把变长样本切成 micro-batch、做 CP 切分与 loss mask 对齐。 |
| `slime/backends/megatron_utils/loss.py` | `loss_function`（训练回调）、`get_log_probs_and_entropy` / `get_values`（forward_only 回调）。 |
| `slime/backends/megatron_utils/cp_utils.py` | `reduce_train_step_metrics`：把各 micro-batch 的日志在 DP*CP 组上归约成单步指标。 |
| `tests/test_metric_report.py` | 单进程 CPU 单测，专门钉住 `reduce_train_step_metrics` 的归约数学。 |

> 提示：本讲聚焦 `model.py` 的执行机制；loss 的数学细节（PPO clip、优势估计）留给 u4-l4，数据打包细节留给 u4-l3。

## 4. 核心概念与源码讲解

按「引擎心智模型 → 只读前向 → 单步训练 → 多步驱动」的顺序展开，共四个最小模块。

### 4.1 共同基础：Megatron 流水线引擎与 forward_step 闭包

#### 4.1.1 概念说明

`forward_only` 和 `train_one_step` 看似不同，但骨架完全一样：

1. 定义一个局部闭包 `forward_step(data_iterator, model)`，负责「取数据 → 前向 → 返回 (output_tensor, 回调)」。
2. 调 `get_forward_backward_func()` 拿到 Megatron 流水线引擎。
3. 把闭包交给引擎，让引擎按调度跑完所有 micro-batch。
4. 在最后一个 pipeline stage 上收集引擎返回的结果。

`get_forward_backward_func()` 根据是否启用虚拟流水线（VPP）返回不同的调度实现，但这对 slime 是一个**黑盒执行器**：你给它闭包、模型、微批数、`forward_only` 标志，它就把前后向调度好、梯度归约好。这是 slime 能「无损复用 Megatron 全部并行能力」（TP/PP/CP/EP/DP）的关键——slime 不碰流水线调度的任何细节。

#### 4.1.2 核心流程

```
forward_step 闭包约定:
  入参: (data_iterator, model)
  1. batch = get_batch(data_iterator, keys, ...)        # 取一个微批，做 CP 切分
  2. output_tensor = model(input_ids=tokens, packed_seq_params=..., loss_mask=...)  # 前向
  3. return (output_tensor, partial(回调, **kwargs))     # 把回调参数预先绑好

引擎调用:
  结果 = forward_backward_func(
      forward_step_func=forward_step,
      data_iterator=..., model=...,
      num_microbatches=...,
      forward_only=True 或 False,   # 决定是否反向
  )
```

关键点：

- `partial(回调, **kwargs)` 用 `functools.partial` 把除 `output_tensor` 之外的参数预先绑好，引擎只需调用 `回调(output_tensor)`。
- `forward_only` 标志是开关：`True` 只跑前向、收集回调返回的字典；`False` 跑完整 1F1B、产出梯度，并返回每个微批的 reduced 日志字典。

#### 4.1.3 源码精读

引擎的取用与训练调用（`forward_only=False`）：

[model.py:641-L651](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L641-L651) —— 调用 Megatron 流水线引擎，`forward_only=False` 表示要执行反向传播；返回 `losses_reduced`（最后一个 pipeline stage 上每个 micro-batch 的日志字典列表）。

训练用的 `forward_step` 闭包：先 `get_batch` 取一个微批，再前向，最后用 `partial(loss_function, args, batch, num_microbatches, step_global_batch_size)` 绑定好回调：

[model.py:560-L638](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L560-L638) —— 训练用 `forward_step`。注意它把 `num_microbatches` 与 `step_global_batch_size` 一起绑进 `loss_function`，正是为了 2.3 节所说的损失缩放。

`get_batch` 是两个闭包的共同依赖，负责「取一个微批 + CP 切分 + loss mask 对齐」：

[data.py:28-L52](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/data.py#L28-L52) —— `get_batch` 的职责：取原始字段、保存 CP 切分前的 `unconcat_tokens`、按 CP 把序列切成两块再拼接、构造 `PackedSeqParams`（THD 布局）。

`DataIterator` 用预计算好的 `micro_batch_indices` 调度表按顺序吐出每个微批：

[data.py:201-L233](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/data.py#L201-L233) —— `DataIterator` 按 `offset` 在 `micro_batch_indices` 上推进，`get_next` 返回当前微批对应样本的字段子集；`reset` 把 `offset` 归零，供多轮 forward_only 复用同一份数据。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：把「闭包 + 引擎」的调用关系画成函数调用图，确认 slime 与 Megatron 的分工边界。

**操作步骤**：

1. 打开 `train_one_step` 的 `forward_step` 闭包（[model.py:560-L638](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L560-L638)），追踪三步：`get_batch(...)` → `model(**forward_kwargs)` → `partial(loss_function, ...)`。
2. 找到引擎调用（[model.py:642](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L642)），确认闭包、`num_microbatches`、`forward_only=False` 都传了进去。
3. 在纸上画出：`train_one_step` → `forward_backward_func` →（每 micro-batch）`forward_step` → `get_batch` + `model` + `loss_function`。

**需要观察的现象**：闭包本身**不**调 `backward()`、**不**调 `optimizer.step()`——这些全由引擎和 `train_one_step` 的后续代码负责。闭包只负责「出一个微批的前向结果 + 绑好的回调」。

**预期结果**：清楚看到分工——slime 提供「数据 + 前向 + loss」，Megatron 提供「流水线调度 + 梯度累积 + 反向」。

> 说明：此实践为源码阅读型，无需 GPU；若想观察真实 micro-batch 流转，需在多卡 + Megatron 环境运行，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`forward_step` 闭包为什么用 `partial(loss_function, ...)` 而不是直接在闭包里算好 loss 返回标量？

**参考答案**：流水线并行下，`output_tensor` 在中间流水级只是「占位」，真正的完整 logits 只有最后一个流水级才有。引擎需要等激活沿流水线传到最后一 stage 后，在正确时机、正确 rank 上调用回调消费这份完整 logits。把 loss 计算延后成 `partial` 回调，既让引擎掌控调用时机，又把 `args/batch/num_microbatches` 等上下文预先绑好。

**练习 2**：如果把 `forward_only=False` 误写成 `forward_only=True`，训练会发生什么？

**参考答案**：引擎只跑前向、不跑反向，不会产生梯度，`optimizer.step()` 几乎不动参数；同时引擎返回的是回调收集的字典而非 reduced 指标，下游 `reduce_train_step_metrics` 会因取不到 `"keys"` 字段而报错。这正是两条路径必须严格区分 `forward_only` 标志的原因。

---

### 4.2 forward_only：只前向算 logprob / entropy / value

#### 4.2.1 概念说明

`forward_only` 用于「只看、不改」的前向：用当前模型（或参考模型 ref、教师模型 teacher）对 rollout 出来的 token 算对数概率和熵，这些值随后喂给优势估计。它也被 `train_critic` 用来读 value head 的输出。

它名字里的「only」有两层含义：一是传给引擎的 `forward_only=True`（不反向、不产梯度）；二是函数本身打上 `@torch.no_grad()` 装饰器，确保整个前向不构建 autograd 计算图，省显存。

#### 4.2.2 核心流程

```
@torch.no_grad()
forward_only(f, args, model, data_iterator, num_microbatches, store_prefix, ...):
  ① data_iterator.reset(); 复位迭代器
  ② 定义 forward_step 闭包:
       batch = get_batch(...)
       output_tensor = model(...)
       return (output_tensor, partial(f, **output_kwargs))   # f = get_log_probs_and_entropy / get_values
  ③ model.eval()                              # 关掉 dropout
  ④ for step_id in range(num_steps_per_rollout):
         forward_data_store += 引擎(forward_only=True)        # 只前向、收集回调返回的字典
  ⑤ model.train()                             # 切回训练模式
  ⑥ 在最后一个 pipeline stage 上，把各微批结果按 key 拼成 rollout_data 并返回
```

`f` 是 post-forward 回调，实参通常是 `get_log_probs_and_entropy`（算 logprob/entropy）或 `get_values`（算 value）。`store_prefix` 给结果键加前缀（如 `"ref_"`），让一次 `train_actor` 调用里 ref 与 actor 的输出互不覆盖。

#### 4.2.3 源码精读

整个函数打 `@torch.no_grad()`，从源头杜绝反向：

[model.py:344-L353](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L344-L353) —— `forward_only` 签名。第一个参数 `f` 是 post-forward 回调，会被 `partial` 绑进闭包。

闭包里前向后用 `partial(f, **output_kwargs)` 绑回调；`output_kwargs` 携带 `unconcat_tokens / total_lengths / response_lengths` 等「从 logits 切出 response 段」所需的上下文：

[model.py:394-L445](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L394-L445) —— `forward_only` 内部的 `forward_step`。注意它 `assert not return_schedule_plan`，表明只读路径不参与组合式 1F1B 的 schedule plan 构建。

引擎调用循环：对每个 step 跑一次 `forward_only=True` 的流水线，结果累加进 `forward_data_store`：

[model.py:457-L480](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L457-L480) —— 取引擎并按 `num_steps_per_rollout` 循环；`forward_only=True` 让引擎只前向、收集回调输出。

结果聚合：在最后一个 stage 把各微批的字典按 key 展平，动态批模式下按原始索引重排以保证顺序与输入一致：

[model.py:487-L506](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L487-L506) —— 把 `forward_data_store` 聚合成 `rollout_data`，每个 key 加 `store_prefix`；动态批时因微批打乱原始顺序，需用 `micro_batch_indices` 还原。

回调 `f` 的返回很特别——它返回 `(torch.empty((0,)), res)`，一个空张量当「占位 loss」，真正的 logprob/entropy 放在 `res`：

[loss.py:548-L561](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L548-L561) —— `get_log_probs_and_entropy` 返回空张量作占位 loss、把真正的 logprob/entropy 列表放进 `res`。因为 `forward_only=True` 不会对空张量反向，引擎只把 `res` 收集起来交给 `forward_only` 聚合。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：理解为什么 `forward_only` 既要传 `forward_only=True` 又要打 `@torch.no_grad()`，以及空 loss 张量的作用。

**操作步骤**：

1. 阅读 [model.py:344](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L344) 的 `@torch.no_grad()` 装饰器。
2. 阅读 [model.py:479](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L479) 的 `forward_only=True`。
3. 阅读 [loss.py:561](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L561) 返回的 `torch.empty((0,), ...)`。
4. 在上层调用方 `actor.py` 的 `compute_log_prob` 里看 `store_prefix="ref_"` 如何调用 `forward_only`，结果又如何 `update` 回 `rollout_data`。

**需要观察的现象**：三处机制分别在不同层面保证「不反向」——`@torch.no_grad()` 阻止 autograd 建图、`forward_only=True` 阻止引擎调度反向 pass、空 loss 张量确保即便引擎调用回调也不会产生有意义的梯度。

**预期结果**：能解释「算 ref/teacher logprob 必须完全无梯度，否则既浪费显存又可能污染优势估计」这一设计动机。

> 说明：此实践为源码阅读型；若要在本地验证「无梯度」，可在单卡 Megatron 环境对 `forward_only` 输入假数据后检查参数 `.grad` 是否为 `None`，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`forward_only` 里 `model.eval()` 之后又 `model.train()`，为什么？

**参考答案**：`eval()` 关闭 dropout 等随机行为，让 logprob/entropy 计算确定（优势估计需要确定性）。但 `forward_only` 穿插在训练流程里，算完 logprob 后模型马上要进入反向训练，所以必须切回 `train()` 恢复训练态，否则后续 `train` 的前向会错误地保持 eval 行为。

**练习 2**：`store_prefix="ref_"` 时，最终 `rollout_data` 里会多出哪些 key？为什么 ref 和 actor 能复用同一个 `get_log_probs_and_entropy`？

**参考答案**：会多出带前缀的 `ref_log_probs`（以及 `args.use_rollout_entropy=True` 时的 `ref_entropy`）。这些 key 在 `actor.py` 的 `_switch_model("ref")` 后被 `rollout_data.update(...)` 合并，供 `compute_advantages_and_returns` 算 KL/优势。复用同一个回调是因为「算对数概率」的逻辑与用哪个模型无关——区别只在于当前 GPU 骨架上加载的是哪份权重（由 `_switch_model` 切换）。

---

### 4.3 train_one_step：单步训练的六拍节拍

#### 4.3.1 概念说明

`train_one_step` 是一个**完整的单步训练**：它接收若干 micro-batch，跑前向+反向，检查梯度，更新参数，推进学习率，并返回单步的 loss 指标和梯度范数。它是 `train` 循环体里每一步真正调用的函数。

它要协调三件事：Megatron 的梯度累积（多个微批的梯度加在一起）、DistributedOptimizer 的梯度归约（跨 DP/TP 求和）、以及 RL 特有的损失缩放（保证累积后的有效梯度等于「整批一起前向」的梯度）。

#### 4.3.2 核心流程

```
train_one_step(args, rollout_id, step_id, data_iterator, model, optimizer,
               opt_param_scheduler, num_microbatches, step_global_batch_size, ...):
  拍1  for chunk in model: chunk.zero_grad_buffer()
       optimizer.zero_grad()                         # 清梯度（含 DDP buffer）
  拍2  若有 custom_before_train_step_hook: 调用它      # 自定义注入点
  拍3  定义 forward_step 闭包（不立即执行）
  拍4  losses_reduced = 引擎(forward_step, forward_only=False)   # 流水线前后向，产梯度
  拍5  梯度检查:
         默认(check_for_nan_in_loss_and_grad=True): 跳过本段，交给 optimizer.step 内部检测
         否则: prepare_grads() 查 inf → get_grad_norm() → NaN/Inf 判定 → valid_step
  拍6  if valid_step:
         optimizer.step()                            # 更新参数
         opt_param_scheduler.step(increment=step_global_batch_size)  # 推进 LR/WD
       释放梯度 + 在最后 stage 用 reduce_train_step_metrics 归约指标，返回 (loss_dict, grad_norm)
```

**损失缩放**是理解拍 4 的关键。Megatron 引擎内部会对一个训练步的所有微批 loss 做归约（求和后除以微批数，再经 DDP all-reduce 除以 DP 世界规模）。为了让「分微批累积」等价于「整批一次前向」，slime 在 `loss_function` 里把 loss 预先乘上一个补偿因子：

\[
\text{scale} = \frac{\text{num\_microbatches}}{\text{step\_global\_batch\_size}} \times \text{dp\_world\_size}
\]

这样无论数据被切成多少微批、分布在多少 DP 卡上，最终回传到优化器的有效梯度都等于「整批样本平均梯度」。详见 [loss.py:1290-L1298](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L1290-L1298)（loss 的数学留给 u4-l4）。

#### 4.3.3 源码精读

**拍 1：清梯度**。同时清零 DDP 的 `zero_grad_buffer` 和 optimizer 的梯度：

[model.py:549-L552](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L549-L552) —— `zero_grad_buffer()` 清零 DDP 重叠通信用的梯度 buffer，`optimizer.zero_grad()` 清零参数 `.grad`。两步都做是为兼容 overlap_grad_reduce 等优化，且梯度累积依赖「干净起步」。

**拍 2：自定义 hook**。训练步开始前的注入点：

[model.py:554-L558](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L554-L558) —— 经 `--custom-megatron-before-train-step-hook-path` 注入的 hook，签名拿到 `(args, rollout_id, step_id, model, optimizer, opt_param_scheduler)`，可在前后向之前干预整个训练状态。

**拍 4：流水线前后向**。即 4.1 节的闭包 + 引擎调用，`forward_only=False`：

[model.py:640-L651](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L640-L651) —— 引擎调用，`forward_only=False` 触发反向；返回 `losses_reduced`。

**拍 5：梯度 NaN/Inf 检查（容错核心）**。这里有一个容易误读的细节：

[model.py:653-L664](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L653-L664) —— 关键是 `if not getattr(args, "check_for_nan_in_loss_and_grad", True):`。**默认该开关为 True**，于是整段被跳过：`valid_step` 保持 `True`、`grad_norm` 保持 `nan`，NaN/Inf 检测完全交给下一步 `optimizer.step()` 内部（Megatron 优化器自带 inf 检测与梯度缩放）。只有当用户**显式关闭**该开关时，slime 才主动 `prepare_grads()` + `get_grad_norm()` 做检测，并在发现 inf/nan 时把 `valid_step` 置 `False` 从而跳过这一步更新。

**拍 6：更新参数 + 推进 LR**。仅当 `valid_step` 为真才更新：

[model.py:673-L680](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L673-L680) —— `optimizer.step()` 返回 `(update_successful, grad_norm, num_zeros_in_grad)`；随后 `opt_param_scheduler.step(increment=step_global_batch_size)` 按本步样本数推进学习率/权重衰减。注意 `assert update_successful`——默认路径下若优化器内部检测到 inf 会返回 False，此处断言失败、显式报错。

LR 调度为何按 `step_global_batch_size` 增量推进？调度器内部用「已见样本数」追踪真实进度（见 [model.py:195-L208](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L195-L208) 注释），这样即便动态采样导致每步样本数波动，cosine/linear 衰减仍能跟随真实进度。

**拍 6 续：释放梯度 + 归约指标**：

[model.py:682-L696](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L682-L696) —— 再次清零梯度（避免泄漏到下一步），非最后 stage 返回空 dict 与 grad_norm，最后 stage 调 `reduce_train_step_metrics` 聚合。该函数把各微批 `values` 先本地求和、再 all-reduce 到 DP*CP 组，最后按模式除以分母：

[cp_utils.py:154-L168](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/cp_utils.py#L154-L168) —— 归约逻辑：per-token 模式除以 all-reduced 的 `num_tokens`（并乘 `cp_size` 抵消 CP 重复计数），per-rollout-mean 模式除以常量 `step_global_batch_size`。

#### 4.3.4 代码实践（含手算）

**实践目标**：对照 Megatron 的 `get_forward_backward_func`，说清 slime 如何在一个 micro-batch 内完成前后向、并跨 micro-batch 累积梯度；并手算损失缩放因子。

**操作步骤**：

1. 打开闭包 [model.py:560-L638](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L560-L638)，确认闭包只前向 + 绑 loss、**不**调 `backward`。
2. 看 [model.py:642-L651](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L642-L651)：`forward_backward_func(..., forward_only=False)` 是唯一触发反向的地方，引擎会在每个 micro-batch 上自动调用 `回调(output_tensor).backward()` 并把梯度累加进 `.grad`。
3. 看 [model.py:673-L675](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L673-L675)：累积完所有微批后，才一次性 `optimizer.step()`。
4. **手算缩放因子**：假设 `num_microbatches=4`、`step_global_batch_size=64`、DP（含 CP）世界规模 `dp_world_size=8`、`calculate_per_token_loss=False`。根据 [loss.py:1290-L1298](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L1290-L1298) 的缩放公式，算出系数。

**需要观察的现象**：反向与梯度累积发生在引擎内部（slime 不可见），slime 可见的只是 `optimizer.step()` 这一次集中更新。

**预期结果**：

- 调用链：`train_one_step` →（每微批）引擎 → `forward_step` → `get_batch` + `model` + `loss_function`；所有微批反向完 → `optimizer.step()`。
- 缩放系数 \( = 4 / 64 \times 8 = 0.5 \)。即每个微批的 loss 被乘 0.5 后反向，4 个微批累积 + DP all-reduce 平均后，净效果是每个样本对梯度的贡献权重为 \( 1/\text{step\_global\_batch\_size} \)，等价于把 64 个样本作为一个整批做一次平均前向。

**待本地验证**：上述因子抵消关系依赖 Megatron 内部「按微批数除、按 DP 平均」的具体实现，不同 Megatron 版本细节可能不同；若要严格验证，需对照你实际使用的 Megatron-LM 版本的 `forward_backward_func` 源码。

#### 4.3.5 小练习与答案

**练习 1**：默认配置下（`check_for_nan_in_loss_and_grad=True`），如果某一步出现 inf 梯度，会发生什么？

**参考答案**：拍 5 整段被跳过，`valid_step=True`、`grad_norm=nan`。于是进入拍 6 调 `optimizer.step()`，Megatron 优化器内部检测到 inf 会返回 `update_successful=False`，紧接着的 `assert update_successful` 触发断言失败、抛错中止。也就是说默认路径下 slime 选择「显式报错」而非静默跳过；只有显式关闭该开关，才会走 slime 自己的检测、在异常时把 `valid_step=False` 而静默跳过 step。

**练习 2**：为什么拍 1 和拍 6 都要 `zero_grad`？只保留一处行不行？

**参考答案**：拍 1 清零是为确保本步梯度干净（不含上一步残留）；拍 6 清零是在 `optimizer.step()` 之后立刻释放梯度 buffer 占用的显存（尤其 DDP 的 overlap 通信 buffer），避免在后续 `forward_only` 或权重同步期间 OOM。两处目的不同，不能合并。

**练习 3**：`opt_param_scheduler.step(increment=step_global_batch_size)` 为什么不固定增量为 1？

**参考答案**：因为 slime 的 LR 调度以「已消费的样本数」而非「步数」为进度轴。动态采样/过滤会让不同 step 的样本数不同，用样本数增量推进能让 cosine/linear 衰减跟随真实训练进度，避免因每步样本数波动导致衰减提前或滞后。

---

### 4.4 train：多步训练循环的编排与配置

#### 4.4.1 概念说明

`train` 是 `train_one_step` 的外层循环。一个 rollout 的训练数据往往够训练**多个优化器步**（`num_steps_per_rollout`），`train` 负责把这些步串起来，并在循环开始前一次性完成一系列**训练态配置**：设置 DDP 的梯度同步回调、处理 optimizer 状态重置、手动 GC、前向 pre-hook 的精细启停，以及每步结束后的指标日志。

#### 4.4.2 核心流程

```
train(rollout_id, model, optimizer, opt_param_scheduler,
      data_iterator, num_microbatches, global_batch_sizes):
  前置:
    ① assert len(num_microbatches) == len(global_batch_sizes)
    ② data_iterator.reset(); model.train()
    ③ 配置 model_config: grad_scale_func / no_sync_func / grad_sync_func / param_sync_func / finalize_model_grads_func
    ④ 若 reset_optimizer_states: 清零 optimizer 的 step / exp_avg / exp_avg_sq
    ⑤ 若 manual_gc: 关闭自动 GC，改为手动收集（对齐各 rank GC 时机）
    ⑥ 若 overlap_param_gather: 临时禁用前向 pre-hook，等第一步成功后再启用
  循环 for step_id in range(num_steps_per_rollout):
    ⑦ loss_dict, grad_norm = train_one_step(num_microbatches[step_id], global_batch_sizes[step_id], ...)
    ⑧ step_id==0 且需要时: 启用前向 pre-hook
    ⑨ MTP loss 处理（若 enable_mtp_training）
    ⑩ 在主 rank 上: 组装 train/* 日志（loss、grad_norm、各 param_group 的 lr、global_batch_size）并记录
    ⑪ CI 检查（若 ci_test）: KL 应近似 0、grad_norm 数值一致性等
  收尾:
    ⑫ 关闭前向 pre-hook（若曾启用）
```

注意 `num_microbatches` 和 `global_batch_sizes` 都是**列表**，长度等于 `num_steps_per_rollout`，每个元素对应一个训练步——这允许动态批（每步样本数不同）。

#### 4.4.3 源码精读

**前置 ①②：断言与复位**：

[model.py:732-L744](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L732-L744) —— 断言两表等长、复位迭代器、`model.train()` 开启训练态（dropout 等）。

**前置 ③：DDP 同步回调配置**。这是 `train` 最关键的配置段，把 slime 的 DDP 行为告诉 Megatron 引擎：

[model.py:746-L766](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L746-L766) —— 把 `optimizer.scale_loss` 设为 `grad_scale_func`；`overlap_grad_reduce` 时把各 chunk 的 `no_sync` / `start_grad_sync` 设为 `no_sync_func` / `grad_sync_func`；`overlap_param_gather` + `align_param_gather` 时设 `param_sync_func`；并把 `finalize_model_grads` 设为 `finalize_model_grads_func`。这些回调决定引擎在流水线各节点如何/何时触发梯度 all-reduce 与参数 gather。

**前置 ④：optimizer 状态重置**（可选）：

[model.py:770-L790](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L770-L790) —— 若 `reset_optimizer_states`，把每个 param group 的 `step` 与每个状态的 `exp_avg`/`exp_avg_sq` 清零，相当于「重置 Adam 动量但不重建优化器」。

**前置 ⑤：手动 GC**：

[model.py:792-L797](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L792-L797) —— 关闭 Python 自动 GC、改为手动收集，让各 rank 的 GC 时机对齐，避免某 rank 因 GC 停顿导致集合通信超时。

**前置 ⑥：前向 pre-hook 的「先禁后启」**。分布式优化器 + overlap_param_gather 时，首次 all-gather 若有 checkpoint 加载错误会扩散到所有 rank，所以首步前先禁用、首步成功后再启用：

[model.py:799-L808](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L799-L808) —— 临时禁用 pre-hook 并把 `param_sync_func` 置空，让首步的同步调用变 no-op。

**循环体：调 train_one_step + 首步后启用 pre-hook**：

[model.py:821-L844](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L821-L844) —— 每步调 `train_one_step`，传入该步自己的 `num_microbatches[step_id]` 与 `global_batch_sizes[step_id]`；`step_id==0` 且首步成功后恢复 pre-hook 与 `param_sync_func`。

**循环体：指标日志**。在主 rank（DP*CP rank 0 + TP rank 0 + 最后 PP stage）上组装并记录：

[model.py:867-L890](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L867-L890) —— 组装 `train/{role_tag}{key}` 形式的 loss 指标、`grad_norm`、每个 param group 的当前 lr、以及本步的 `global_batch_size`（注释专门指出「不等长 step 容易被忽略」，所以单独打这一项），连同累计 `train/step` 一起记录。

**循环体：CI 数值一致性检查**（仅 `ci_test`）：

[model.py:892-L930](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L892-L930) —— 一系列断言：如 `train_rollout_logprob_abs_diff <= 0.1`、首步 `ppo_kl < 1e-8`、KL 一致性，以及 `ci_save_grad_norm`/`ci_load_grad_norm` 之间的 grad_norm 数值对比（`rel_tol=0.01`），用于跨版本/跨配置回归测试。

#### 4.4.4 代码实践（源码阅读型）

**实践目标**：把 `train` 的「前置配置 → 循环 → 收尾」三段在源码里对上号，定位学习率曲线的初始化点。

**操作步骤**：

1. 阅读 [model.py:746-L766](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L746-L766)，列出 `model_config` 上被设置的 5 个回调字段。
2. 阅读 [model.py:821-L835](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L821-L835)，确认循环体每步传给 `train_one_step` 的是**该步自己的** `num_microbatches[step_id]` 与 `global_batch_sizes[step_id]`（而非整个列表）。
3. 阅读 [get_optimizer_param_scheduler](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L195-L208)，看 `args.train_iters` 的估算公式如何由 `num_rollout * rollout_batch_size * n_samples_per_prompt / global_batch_size` 决定学习率 decay 长度。
4. 在上层 `actor.py` 的 `train_actor` / `train_critic` 里找到对 `train(...)` 的调用（[actor.py:402-L410](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L402-L410) 与 [actor.py:511-L520](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L511-L520)），确认 `num_microbatches` 与 `global_batch_sizes` 来自 `rollout_data`。

**需要观察的现象**：`train` 不直接接触 loss 张量，它只编排 `train_one_step`；真正的 loss/梯度逻辑全在 `train_one_step` 与闭包里。`train` 的职责是「配置 + 循环 + 日志」。

**预期结果**：能口述 `train` 设置的 5 个回调（`grad_scale_func`、`no_sync_func`、`grad_sync_func`、`param_sync_func`、`finalize_model_grads_func`）分别控制 DDP 的哪一面；并理解 `train_iters` 只是估算值，真实进度靠调度器的「样本数」计数器跟踪。

> 说明：此实践为源码阅读型；要观察这些回调的真实触发，需在多卡 + overlap_grad_reduce 环境运行并用 nsys/nsight 抓通信轨迹，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么前向 pre-hook 要在第一步**之后**才启用，而不是循环开始前就启用？

**参考答案**：分布式优化器 + overlap_param_gather 时，第一次参数 all-gather 会把每个 rank 的参数同步起来。若此时某 rank 因 checkpoint 加载错误或随机初始化异常而持有错误参数，pre-hook 一启用错误就会经 all-gather 扩散到所有 rank，难以定位。先禁用 pre-hook、让第一步在前台显式跑通、确认参数正确后再启用，能把错误隔离在首步暴露——是一种「首步自检」保护。

**练习 2**：`manual_gc` 关闭自动 GC 后，GC 在哪里发生？为什么要这么做？

**参考答案**：关闭自动 GC 后由 slime 在训练流程的固定节点（如 step 之间、显存清理时）手动 `gc.collect()`。这样所有 rank 的 GC 停顿集中在同一时机，避免某 rank 在集合通信（all-reduce、pipeline P2P）期间触发 GC 而拖慢整条流水线甚至触发通信超时。

---

## 5. 综合实践

**任务**：把本讲三个函数串起来，完成一次「源码追踪 + 数值验证」综合任务。

### 步骤

1. **画调用链**。从上一讲的 `MegatronTrainRayActor.train_actor` 出发，画出：

   - `train_actor` → `compute_log_prob`（包装 `forward_only` + `get_log_probs_and_entropy`）算 logprob；
   - `compute_advantages_and_returns` 算优势；
   - `train_actor` → `train`（[model.py:704](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L704)）→ 循环调 `train_one_step`（[model.py:509](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L509)）→ 引擎 `forward_backward_func` → 回调 `loss_function`（[loss.py:1220](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L1220)）→ `policy_loss_function`。

   在图上标注每个箭头传递的数据载体（Sample 字段 / logits / loss / 梯度）。

2. **标注六拍**。在你画的 `train_one_step` 节点上标出 4.3.2 的六拍，并指出哪一拍调用 Megatron、哪几拍是 slime 自己的逻辑。

3. **运行 CPU 单测验证归约数学**。`train_one_step` 末尾用的 `reduce_train_step_metrics`（[cp_utils.py:127](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/cp_utils.py#L127)）有一组**单进程 CPU 单测**，专门钉住「同一批样本无论怎么分微批/分 DP、是否开 CP、是 per-rollout-mean 还是 per-token-loss，报告值都一致」：

   ```bash
   pytest tests/test_metric_report.py -v
   ```

   阅读该测试文件头部注释（[tests/test_metric_report.py:1-L16](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/test_metric_report.py#L1-L16)）：它用 mock 的 `dp_with_cp_group` + no-op 的 `dist.all_reduce` 在单进程里复现生产调用形状，无需多卡。

4. **观察与预期**：

   - 调用链图应清楚显示：logits 在 `forward_step` 里产生、loss 在回调 `loss_function` 里产生、梯度由 Megatron 引擎算并归约、参数更新在 `optimizer.step()` 里完成。
   - 单测应全部通过，证明 per-rollout-mean 与 per-token-loss 两种模式下，`train_one_step` 报告的指标与「样本如何分布」无关——这正是 4.3 损失缩放因子的设计目标。

5. **若无法运行**：明确标注「待本地验证」。该测试依赖 `megatron` 的 stub（测试目录的 `_cp_dist_helpers.py`，见 [test_metric_report.py:24](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/test_metric_report.py#L24)），若环境未装 slime 的纯 Python 依赖，可仅做源码阅读部分（步骤 1、2）。

## 6. 本讲小结

- slime 不重写流水线引擎，而是把 Megatron 的 `get_forward_backward_func()` 当「执行器」，通过 `forward_step` 闭包 + 回调注入 RL 损失，所有 TP/PP/CP/EP/DP 并行能力都是「免费」复用的。
- `forward_only` 是只读路径：`@torch.no_grad()` + `eval()` + `forward_only=True`，回调返回 logprob/entropy/value 字典（空张量占位 loss），供优势估计与评估使用。
- `train_one_step` 是六拍单步：`zero_grad → before_hook → 流水线前后向（梯度累积+归约）→ 梯度检查 → optimizer.step + scheduler.step → 释放梯度 + 归约指标`。
- 损失缩放因子 `num_microbatches / step_global_batch_size * dp_world_size` 是梯度累积数值等价性的核心：抵消 Megatron 的「除以微批数」「除以 DP」，使每个样本的梯度权重恰为 `1/step_global_batch_size`。
- 梯度容错有两条路：默认 `check_for_nan_in_loss_and_grad=True` 时交给 `optimizer.step()` 内部检测（异常时 `assert update_successful` 报错）；关闭时 slime 主动检测并可在 NaN/Inf 时跳过本步。
- `train` 是多步外层驱动，负责设置 DDP 的 5 个同步回调、optimizer 状态重置、手动 GC 对齐、前向 pre-hook 的「先禁后启」首步自检，以及按步记录 loss/grad_norm/lr/global_batch_size 指标与 CI 断言。

## 7. 下一步学习建议

- **u4-l3 数据打包、微批调度与 loss mask**：本讲把 `get_batch` / `DataIterator` 当黑盒，下一讲深入它们如何把变长 Sample 切成 micro-batch、做 CP 切分与 loss mask 对齐，是理解闭包取数细节的前提。
- **u4-l4 RL 损失与优势估计**：本讲把 `loss_function` 当黑盒（只用其缩放公式与返回结构），下一讲精读 PPO clip / KL / entropy / TIS 与优势估计的数学。
- **U5 权重同步与推理后端**：`train_one_step` 更新出的新权重，如何经 `update_weights` 单向同步给 SGLang 引擎，闭合 RL 训练循环。
- 阅读建议：对照 Megatron-LM 的 `get_forward_backward_func` 源码（`megatron/core/pipeline_parallel/pipelining.py`）看 1F1B 调度的真实实现，能加深对本讲「引擎负责反向」的理解。
