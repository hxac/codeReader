# RL 损失与优势估计（loss.py 核心）

## 1. 本讲目标

本讲精读 slime 训练后端中「把 reward 变成梯度」的那一段核心代码：`slime/backends/megatron_utils/loss.py` 与 `slime/utils/ppo_utils.py`。

学完后你应当能够：

- 说清一条样本的标量 `reward` 是如何变成每个 response token 上的 `advantage` 的，并能区分 GRPO / PPO / REINFORCE++ / GSPO / CISPO 这几条代码分支的差异。
- 读懂 `policy_loss_function`：PPO 截断式重要性采样（clipped surrogate）、熵正则、KL loss 是如何组合成最终 `loss` 的，以及 TIS、OPSM 这两个 off-policy 修正接在哪一步。
- 区分两个容易混淆的「对数概率」来源——参考策略 `ref_log_probs` 与行为/旧策略 `old_log_probs`，理解它们分别服务于「KL 惩罚」与「重要性采样比」。
- 理解 `sum_of_sample_mean` 归约：per-sample 均值与 per-token 均值的差别，以及它如何被 `loss_function` 重新缩放以兼容 Megatron 的梯度累积。

本讲承接 u4-l2（`train_one_step` 与流水线前后向）与 u3-l4（奖励模型 rm_hub）。u4-l2 告诉你 `forward_step` 把 `loss_function` 作为闭包交给 Megatron 执行器；u3-l4 告诉你 `sample.reward` 是怎么算出来的。本讲就补上中间那一段：reward → advantage → loss。

## 2. 前置知识

### 2.1 三个「策略」与三套对数概率

RL 微调里，「策略（policy）」就是语言模型在每个位置输出下一个 token 的概率分布。读 loss 源码前，必须先分清 slime 里同时存在的三个策略快照：

| 名称 | 来源 | 用途 |
|------|------|------|
| `ref_log_probs` | 冻结的参考模型（ref）对样本算的对数概率 | 计算 KL，把策略「拉回」参考分布，防止训歪 |
| `rollout_log_probs` / `old_log_probs` | 生成样本时（或训练步开始前）的行为策略对数概率 | 计算重要性采样比 \(r=\pi_{\text{new}}/\pi_{\text{old}}\) |
| `log_probs`（当前） | 本次 loss 前向时、当前权重下的对数概率 | 唯一带梯度的量，反向传播更新权重 |

一个直观记忆：**参考策略管「别跑太远」（KL），旧策略管「采样偏差有多大」（IS 比），当前策略才是「要优化的人」**。

### 2.2 PPO 截断式目标与重要性采样比

PPO 用重要性采样比 \(r_t=\dfrac{\pi_\theta(a_t\mid s_t)}{\pi_{\text{old}}(a_t\mid s_t)}\) 修正「样本来自旧策略、梯度要更新新策略」的偏差，再对 \(r\) 做 clip 防止单步更新过大：

\[
L^{\text{PG}}=-\,\mathbb{E}\!\left[\min\!\big(r_t A_t,\;\text{clip}(r_t,\,1-\epsilon,\,1+\epsilon_{\text{high}})\,A_t\big)\right]
\]

取 `min`（损失里取 `max`，因为损失带负号）是「悲观 bound」：当 \(A_t>0\)（好动作）时限制 \(r\) 上界，当 \(A_t<0\)（坏动作）时限制 \(r\) 下界，从而每步只敢小幅改进。slime 用 `ppo_kl = old_log_probs - log_probs` 表示这个 log-ratio，于是 \(r=\exp(-\text{ppo\_kl})\)。

### 2.3 GRPO 的组内相对优势

GRPO（Group Relative Policy Optimization）不需要价值网络（critic）。它对同一个 prompt 采样 \(n\) 条回答，用组内 reward 的均值/方差作为基线：

\[
A_i = \frac{r_i - \text{mean}(r_{1..n})}{\text{std}(r_{1..n})+\epsilon}
\]

再把每个样本的标量 \(A_i\) 广播到它所有 response token 上。关键性质：若一组样本 reward 全相同（全对或全错），则 \(A_i=0\)，没有梯度信号——这正是 u3-l5「动态采样丢零方差组」的动机。

### 2.4 KL 的近似估计（k1/k2/k3）

精确 KL 需要对整个词表求和，代价高。Schulman 博客给出几种廉价估计（`compute_approx_kl`），slime 用 `--kl-loss-type` 选择，默认 `k1`：

\[
\text{k1}=\log\frac{\pi_{\text{new}}}{\pi_{\text{ref}}}=\log r,\qquad
\text{k2}=\tfrac{1}{2}(\log r)^2,\qquad
\text{k3}/\text{low\_var\_kl}=e^{-\log r}-1+\log r
\]

其中 k3 非负且方差更低，但需要额外 clamp 防数值溢出。

> 名词提示：**advantage（优势）**衡量「这个动作比平均好多少」；**return（回报）**是用于训练价值网络的目标值；**surrogate（替代损失）**是 PPO 那个 clip 过的目标函数；**off-policy** 指样本来自旧策略但用来更新新策略。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [slime/backends/megatron_utils/loss.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py) | RL 损失主战场：对数概率/熵计算、优势与回报计算、policy/value/sft 损失、Megatron 适配 |
| [slime/utils/ppo_utils.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/ppo_utils.py) | 纯数学工具：KL 近似、PPO clip 损失、CISPO/GSPO/OPSM、GRPO/REINFORCE++/GAE 的优势函数 |
| [slime/backends/megatron_utils/cp_utils.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/cp_utils.py) | `get_sum_of_sample_mean` 归约器：per-sample 与 per-token 两种聚合，CP 感知 |
| [slime/backends/megatron_utils/actor.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py) | `train_actor`：算 ref/teacher/old logp → `compute_advantages_and_returns` → `train`（内含 policy loss） |
| [slime/utils/arguments.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py) | 参数定义：`--advantage-estimator`、`--kl-loss-type`、`--eps-clip`、`--use-kl-loss`、`--use-opsm` 等 |

## 4. 核心概念与源码讲解

按数据流顺序拆成四个最小模块：(1) 对数概率与熵的并行计算；(2) reward → advantage 的分发；(3) policy loss 的组合；(4) 归约与 Megatron 缩放。

### 4.1 对数概率与熵的并行计算：get_log_probs_and_entropy

#### 4.1.1 概念说明

无论是算优势（需要 old/ref logp）还是算 policy loss（需要当前 logp），都要把模型输出的 logits 转成「真实采样到的 token」的对数概率 \(\log \pi_\theta(a_t)\)，并可选地算熵 \(H=-\sum_v p_v\log p_v\)。难点在于 slime 跑在 Megatron 的张量并行（TP）+ 上下文并行（CP）下：每个 TP rank 只持有**一部分词表**的 logits，每个 CP rank 只持有**一段序列**。

`get_log_probs_and_entropy` 的核心设计是：**不在 per-sample 维度逐条切片，而是对整条 `[T, V]` 的 logits 一次性算 logprob/熵**，让反向传播只走一遍 `[T, V]`，然后再切回 per-sample。这样在大词表（如 15 万）+ 长序列下显著省显存与算力。

#### 4.1.2 核心流程

1. 构造「shifted token」：next-token 预测要把目标 token 左移一位对齐 logit 轴（`_build_shifted_tokens`）。
2. 对整条 `[T, V]` 调 `calculate_log_probs_and_entropy`，内部用自定义 autograd 算子 `_VocabParallelLogProbEntropy` 在 TP 组上做跨词表的 gather/max/sum，得到每行的 logprob 与熵，**只有一次 `[T,V]` 反向**。
3. `_extract_per_sample` 按每个样本的 `total_length`/`response_length` 把全长 logprob 切回 per-sample 的 response 段。
4. 若开启 allgather-CP，再用 `_allgather_cp_redistribute` 把 allgather 布局重排成 zigzag ring-attn 布局，供下游消费。

#### 4.1.3 源码精读

入口 `get_log_probs_and_entropy` 在 [slime/backends/megatron_utils/loss.py:470-561](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L470-L561)。关键两步：先对全长 logits 调并行算子，再切片：

```python
# loss.py:528-537  对整条 [T,V] 一次性算 logprob（含可选 top-p keep mask）
log_prob_full, entropy_full = calculate_log_probs_and_entropy(
    logits, full_tokens, tp_group,
    with_entropy=with_entropy,
    with_entropy_grad=with_entropy_grad,
    chunk_size=chunk_size,
    log_prob_keep_mask=top_p_keep_mask,
)
log_prob_full = log_prob_full.squeeze(-1)  # [T, 1] -> [T]
# loss.py:540-546  切回 per-sample response 段
log_probs_list, entropy_list = _extract_per_sample(
    log_prob_full, entropy_full, total_lengths, response_lengths, args.allgather_cp)
```

真正的 TP 感知 logprob 计算在 [slime/utils/ppo_utils.py:746-797](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/ppo_utils.py#L746-L797) 的 `calculate_log_probs_and_entropy`，它按 `chunk_size` 分块后委托给自定义 autograd 算子 `_VocabParallelLogProbEntropy`（[ppo_utils.py:187-336](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/ppo_utils.py#L187-L336)）。该算子的 forward 用两次 all-reduce（一次求全局 max 做数值稳定，一次求全局 sum_exp）把分散在各 TP rank 的 logits 归约成正确的 logprob，且**复用同一块 `[seq,vocab]` 显存**完成 softmax（注释 `single [seq_len, vocab] buffer instead of three`）。

> 一个性能细节：[loss.py:508](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L508) 的 `with_entropy_grad` 只在 `entropy_coef != 0` 时才保留熵的反向激活值——熵只用来做指标时不留梯度图，省显存。

#### 4.1.4 代码实践

**实践目标**：理解 `get_log_probs_and_entropy` 为何返回「空张量 + dict」这种略奇怪的签名。

**操作步骤**（源码阅读型）：
1. 打开 [loss.py:561](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L561)，注意返回 `torch.empty((0,), device=device), res`。
2. 对比 [loss.py:617](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L617) 的 `get_values`，签名同样是 `(callable_input, forward_kwargs) -> (output_tensor, loss_fn)` 风格。
3. 回到 [actor.py:358-359](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L358-L359)，看 `forward_only(get_log_probs_and_entropy, ...)` 如何把它们当 `forward_step_func` 喂给 Megatron。

**需要观察的现象**：`forward_only` 路径下 loss 是「空张量占位」（u4-l2 讲过），真正的 logprob/entropy 走 `res` 字典回传；而训练路径下 `policy_loss_function` 才会产生非空 loss。

**预期结果**：你能说清「为什么返回元组且第一个元素常为空」——它是 Megatron 流水线 `forward_step_func` 契约 `(output_tensor, loss_fn)` 的产物，`output_tensor` 是流水线要传递的张量，真正的损失由回调算。

#### 4.1.5 小练习与答案

**练习 1**：`_build_shifted_tokens` 在 cp_size=1 时为什么写 `full_tokens[offset:offset+total_length-1] = tokens[1:total_length]`（少一位）？
**答案**：next-token 预测里，位置 \(t\) 的 logit 预测的是 token \(t+1\)。所以要丢掉每个序列第一个 token、整体左移一位，使 `logits[t]` 对齐 `tokens[t+1]`；序列末尾的最后一个 logit 没有对应目标，故长度减一。

**练习 2**：为什么 entropy 永远从「未 mask 的 logits」算，而 logprob 可以套 top-p keep mask？
**答案**：熵衡量策略自身的随机性，必须基于完整分布；top-p replay 只是想复现 rollout 时的采样核，只该压窄 logprob 的取值范围，不应改变对策略不确定性的度量（见 [loss.py:489-490](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L489-L490) 注释）。

### 4.2 reward 如何变成 advantage：compute_advantages_and_returns

#### 4.2.1 概念说明

这是 slime 优势计算的**总调度**。它在 `train_actor` 里、真正训练步**之前**被调用（见 [actor.py:497](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L497)），因为 advantage 归一化可能需要看到整个 rollout 的全部样本，必须先把当前 rollout 的所有 logprob/values 凑齐。

它接收一个 `rollout_data` 字典（含 rewards、各路 log_probs、values、loss_masks 等），原地写入两个键：`advantages` 和 `returns`，每个是 per-sample 张量列表。

#### 4.2.2 核心流程

```
compute_advantages_and_returns(args, rollout_data):
  1. 取出 rollout_log_probs / log_probs / ref_log_probs / rewards / values / masks
     （非流水线最后一段直接 return）
  2. 算 kl = compute_approx_kl(log_probs[i], ref_log_probs[i], kl_loss_type)  # 当前 vs 参考
     若 kl_coef==0，kl 全置零（省掉 ref 前向）
  3. 按 advantage_estimator 分发：
       grpo/gspo/cispo → get_grpo_returns：把标量 reward 广播到每个 token
       ppo             → reward 减去 kl_coef*kl 当作 per-token reward，再 GAE
       reinforce_plus_plus        → 折扣回报（含 per-token kl 惩罚）
       reinforce_plus_plus_baseline → (reward - baseline) 广播 - kl_coef*kl
       custom_advantage_function_path → 全权交给用户函数
  4. （可选）use_opd：再叠加一个 on-policy 蒸馏 KL 惩罚
  5. （可选）normalize_advantages：跨 DP 组做 masked whiten（白化）
  6. 写回 rollout_data["advantages"]、["returns"]
```

一个关键区分：**GRPO 系把组内归一化放在 rollout 端做**（见 [rollout.py:685-710](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L685-L710) 的 `_post_process_rewards`，对 grpo/gspo/cispo 减均值、可选除标准差），所以 `compute_advantages_and_returns` 看到的 `rewards` 已经是组内相对值；它只需把这个标量广播到 token。而 PPO 的 reward shaping（`-kl_coef*kl`）发生在本函数内。

#### 4.2.3 源码精读

KL 计算（当前策略 vs 参考策略），见 [loss.py:700-713](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L700-L713)：

```python
if args.kl_coef == 0 or not log_probs:
    xs = log_probs or rollout_log_probs or values
    kl = [torch.zeros_like(x, ...) for x in xs]      # 省掉 ref 前向
else:
    kl = [compute_approx_kl(log_probs[i], ref_log_probs[i], kl_loss_type=args.kl_loss_type)
          for i in range(len(log_probs))]
rollout_data["kl"] = kl
```

GRPO 分支（最常用），见 [loss.py:720-724](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L720-L724)：

```python
elif args.advantage_estimator in ["grpo", "gspo", "cispo"]:
    rewards = torch.tensor(rewards, dtype=torch.float32, device=kl[0].device)
    returns = get_grpo_returns(rewards, kl)   # 把标量 reward 广播到每个 token
    advantages = [r for r in returns]
```

而 `get_grpo_returns` 本体极简，[ppo_utils.py:361-368](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/ppo_utils.py#L361-L368)：`returns.append(torch.ones_like(kl[i]) * rewards[i])`——直接把标量铺满该样本所有 response token，`kl` 在这里只用来取形状。

PPO 分支（需要 critic），见 [loss.py:726-738](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L726-L738)：先把 reward 整形为 per-token（reward 只加在序列最后一个有效 token、且只在 `cp_rank==0` 加，避免 CP 重复加），再交给 `get_advantages_and_returns_batch` 跑 GAE。

最后的白化，见 [loss.py:776-825](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L776-L825)：当 `normalize_advantages=True`，用 `distributed_masked_whiten` 在 DP 组上对 advantage 做带 mask 的均值/方差归一（`shift_mean=True`）。注意 CP>1 时要先把 mask 重切成本 rank 持有的 zigzag 两段，保证 `all_advs` 与 `all_masks` 形状对齐（[loss.py:781-810](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L781-L810)）。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：对照 `advantage_estimator=grpo` 的代码路径，写一段伪代码说明 reward 如何变成 per-token advantage。

**操作步骤**：
1. 假设一个 prompt 采样了 `n=4` 条回答，rollout 端原始 reward 为 `raw = [1.0, 0.0, 1.0, 0.0]`（两条对、两条错）。
2. 在 [rollout.py:694-706](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L694-L706) 做组内归一化（`grpo_std_normalization=True`）：减均值 0.5 得 `[0.5,-0.5,0.5,-0.5]`，除标准差 0.5 得 `[1,-1,1,-1]`。
3. 这 4 个标量进入 `compute_advantages_and_returns` 的 GRPO 分支，被 `get_grpo_returns` 广播：每条回答的所有 response token 都拿到同一个值。
4. 写出对应伪代码：

```
# rollout 端（_post_process_rewards）
raw = [r0, r1, r2, r3]
mean = mean(raw); std = std(raw)
reward[i] = (raw[i] - mean) / (std + 1e-6)        # 组内相对优势

# 训练端（compute_advantages_and_returns, grpo 分支）
kl[i] = compute_approx_kl(logp[i], ref_logp[i], "k1")   # 若 kl_coef!=0
returns[i] = ones(resp_len_i) * reward[i]               # 标量广播到每个 token
advantages[i] = returns[i]
# 若 normalize_advantages：跨 DP 组 whiten(advantages, loss_masks)
```

**需要观察的现象**：因为 `kl_coef` 默认 0，GRPO 路径里 `kl` 实际为全零、不参与 advantage；KL 控制改由 `--use-kl-loss` 在 loss 端另加一项（见 4.3）。

**预期结果**：你能解释「为什么全对/全错的一组没有梯度」——`std=0` 使归一后 reward 全为 0（或被 `1e-6` 顶住），advantage 全 0，policy loss 也全 0。这就是动态采样要丢弃零方差组的根本原因。

> 待本地验证：标准差加 `1e-6` 后，全相同 reward 的组 advantage 会得到一个很小的非零值还是精确 0，取决于浮点；建议本地构造 `torch.tensor([1.,1.,1.,1.])` 跑一遍归一化确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 PPO 分支里 `k[-1] += reward` 要加 `if cp_rank == 0` 保护？
**答案**：上下文并行（CP）下，同一条序列被切到多个 CP rank，每个 rank 都会执行这段代码。若不限制，序列最后那个 token 的 reward 会被每个 CP rank 各加一次，导致 reward 被 CP_size 倍放大。只让 `cp_rank==0` 加一次，保证 reward 注入正确。

**练习 2**：GRPO 和 PPO 在「需要哪些输入」上的最大差别是什么？
**答案**：GRPO 不需要 `values`（critic），advantage 完全来自组内 reward 归一；PPO 必须有 `values`（价值网络预测），用它做 GAE 的基线 `V(s)`。这也解释了 [arguments.py:1847](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1847) 的 `args.use_critic = args.advantage_estimator == "ppo"`——只有 PPO 会启动 critic。

### 4.3 策略损失的组合：policy_loss_function

#### 4.3.1 概念说明

`policy_loss_function` 是 `loss_type="policy_loss"` 时（默认）训练步真正反向传播的损失。它把多个零件拼成最终 `loss`：

\[
\text{loss} = \underbrace{L^{\text{PG}}_{\text{clip}}}_{\text{pg\_loss}}
\;-\;\beta\,\underbrace{H[\pi]}_{\text{entropy\_loss}}
\;+\;\underbrace{\beta_{\text{kl}}\,\text{KL}(\pi\Vert\pi_{\text{ref}})}_{\text{kl\_loss（可选）}}
\]

其中 pg_loss 还可能被 TIS（截断重要性采样）和 OPSM（off-policy 序列掩码）两个开关改写。不同 `advantage_estimator` 决定了 pg_loss 内部用 per-token log-ratio 还是 sequence-level KL：

- **GRPO / REINFORCE++ / vanilla PPO**：`ppo_kl = old_log_probs - log_probs`（per-token log-ratio），\(r=\exp(-\text{ppo\_kl})\)。
- **GSPO**：用 `compute_gspo_kl` 把整条序列的 KL 求平均再广播回每个 token（sequence-level KL），稳定性更好。
- **CISPO**：对 clip 后的 ratio 做 stop-gradient，梯度改走 `log_probs`，使被截断的 token 仍贡献梯度。

#### 4.3.2 核心流程

```
policy_loss_function(args, batch, logits, sum_of_sample_mean):
  1. old_log_probs = rollout_log_probs (若 use_rollout_logprobs) 否则 batch["log_probs"]
  2. 用当前 logits 调 get_log_probs_and_entropy 得到当前 log_probs + entropy
  3. 若 use_opsm 或 gspo：先 all_gather 把 log_probs 拼成全长
  4. 算 ppo_kl：
       gspo  → compute_gspo_kl（sequence-level）
       其他  → old_log_probs - log_probs（per-token log-ratio）
  5. 算 pg_loss：
       cispo → compute_cispo_loss（ratio 截断后 detach，梯度走 log_probs）
       其他  → compute_policy_loss（标准 PPO clip surrogate）
  6. 若 use_opsm：pg_loss *= opsm_mask（掩掉负优势且 KL 过大的序列）
  7. 若 use_tis/get_mismatch_metrics：pg_loss *= tis_weights（off-policy 修正）
  8. 归约：pg_loss = pg_loss_reducer(pg_loss)（sum_of_sample_mean 或自定义）
  9. entropy_loss = sum_of_sample_mean(entropy)
 10. loss = pg_loss - entropy_coef * entropy_loss
 11. 若 use_kl_loss：再算 kl = compute_approx_kl(log_probs, ref_log_probs)，loss += kl_loss_coef*kl
 12. 返回 (loss, reported_metrics)
```

#### 4.3.3 源码精读

取 old/当前 logprob 并区分两条来源，见 [loss.py:911-932](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L911-L932)：

```python
advantages = torch.cat(batch["advantages"], dim=0)
old_log_probs = batch["rollout_log_probs"] if args.use_rollout_logprobs else batch.get("log_probs")
_, log_probs_and_entropy = get_log_probs_and_entropy(logits, args=args, ..., with_entropy=True)
log_probs = log_probs_and_entropy["log_probs"]
```

KL 与 surrogate 的分发，见 [loss.py:964-981](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L964-L981)：

```python
if args.advantage_estimator == "gspo":
    ppo_kl = compute_gspo_kl(full_log_probs, full_old_log_probs, log_probs, batch["loss_masks"])
    ...
else:
    ppo_kl = old_log_probs - log_probs          # per-token log-ratio

if args.advantage_estimator == "cispo":
    pg_loss, pg_clipfrac = compute_cispo_loss(ppo_kl, log_probs, advantages, args.eps_clip, args.eps_clip_high)
else:
    pg_loss, pg_clipfrac = compute_policy_loss(ppo_kl, advantages, args.eps_clip, args.eps_clip_high)
```

标准 PPO clip 损失在 [ppo_utils.py:124-148](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/ppo_utils.py#L124-L148)，注意 `ratio = (-ppo_kl).exp()` 与「取 max」对应公式里的 `min`：

```python
ratio = (-ppo_kl).exp()
pg_losses1 = -ratio * advantages
pg_losses2 = -ratio.clamp(1 - eps_clip, 1 + eps_clip_high) * advantages
clip_pg_losses1 = torch.maximum(pg_losses1, pg_losses2)   # 悲观 bound
```

CISPO 的关键差异——ratio 截断后 `.detach()`，梯度只走 `log_probs`，见 [ppo_utils.py:167-169](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/ppo_utils.py#L167-L169)：

```python
ratio_truncated = torch.clamp(ratio, min=1.0 - eps_clip, max=1.0 + eps_clip_high)
pg_losses = -ratio_truncated.detach() * advantages * log_probs   # 梯度走 log_probs
```

最终的 loss 组合（entropy + 可选 kl_loss），见 [loss.py:1047-1067](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L1047-L1067)：

```python
entropy_loss = sum_of_sample_mean(entropy)
loss = pg_loss - args.entropy_coef * entropy_loss
if args.use_kl_loss:
    kl = compute_approx_kl(log_probs, ref_log_probs, kl_loss_type=args.kl_loss_type, ...)
    kl_loss = sum_of_sample_mean(kl)
    loss = loss + args.kl_loss_coef * kl_loss
```

> 关键区分：`kl_coef`（默认 0）在 advantage 端做 reward shaping，整形 PPO/REINFORCE++ 的 per-token reward；`kl_loss_coef`+`--use-kl-loss` 在 loss 端额外加一项 KL，是 GRPO 控制策略漂移的常用手段（测试里几乎所有 grpo 配方都带 `--use-kl-loss --kl-loss-coef 0.00 --kl-loss-type low_var_kl`，coef 为 0 时仍会算 kl_loss 用于监控）。两者互斥，由 [arguments.py:1787](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1787) 的 assert 保证。

#### 4.3.4 代码实践

**实践目标**：用 CPU 单测验证 CISPO「梯度只走 log_probs、不走 ratio」这一性质。

**操作步骤**：
1. 打开 [tests/test_cispo_loss.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/test_cispo_loss.py)，聚焦 `test_compute_cispo_loss_gradient_flows_only_through_log_probs`（[test_cispo_loss.py:34-48](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/test_cispo_loss.py#L34-L48)）。
2. 阅读断言：`log_ratios.grad is None or all zero`（ratio 不应有梯度），`log_probs.grad == -clamped * ADVANTAGES`（梯度只走 log_probs）。
3. 本地（CPU、无 GPU）运行：
   ```bash
   pytest tests/test_cispo_loss.py -q
   ```

**需要观察的现象**：对比标准 PPO 的 `compute_policy_loss`——它的 `ratio` 不 detach，梯度会同时流经 ratio 与（隐含的）log_ratio；而 CISPO 因为 `ratio_truncated.detach()`，被截断的 token 在标准 PPO 下梯度会「卡住」，在 CISPO 下仍能通过 `log_probs` 贡献梯度。

**预期结果**：两个测试通过。若失败，多半是 `compute_cispo_loss` 漏了 `.detach()`。

> 待本地验证：该测试标注 `NUM_GPUS = 0`，是纯 CPU 单测，无需分布式环境即可运行。

#### 4.3.5 小练习与答案

**练习 1**：GSPO 为什么要先用 `all_gather_with_cp` 把 log_probs 拼全长，再算 KL？
**答案**：GSPO 的 KL 定义在「整条序列」级别（`compute_gspo_kl` 对一条序列所有 token 的 log-ratio 求平均，再广播回每个 token，见 [ppo_utils.py:113-119](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/ppo_utils.py#L113-L119)）。CP 下每个 rank 只有序列的一段，必须 all-gather 拼出完整序列才能正确求这个序列级均值。

**练习 2**：`use_kl_loss` 和 `kl_coef` 都涉及 KL，为何设计成互斥的两条通道？
**答案**：它们作用在 RL 闭环的不同环节、解决同一问题（防止策略离参考太远）的两种学派。`kl_coef` 把 KL 折进 reward→advantage（影响价值判断）；`kl_loss_coef` 把 KL 作为独立 loss 项（直接进梯度）。同时启用会重复惩罚，故 assert 互斥。GRPO 系无 critic，常用后者；PPO 系两者皆可但二选一。

### 4.4 归约与 Megatron 缩放：sum_of_sample_mean 与 loss_function

#### 4.4.1 概念说明

slime 默认用 **per-sample 均值的和**（`sum_of_sample_mean`）作为损失归约方式：先对每条样本的 response token 求平均，再把一个 micro-batch 内所有样本的均值加起来。这与朴素的「所有 token 一起求平均」（per-token）不同：

- **per-sample mean**：长回答和短回答贡献同等权重，避免长回答主导梯度。这是 GRPO 系的默认。
- **per-token mean**：所有有效 token 平权，长序列天然权重大。由 `--calculate-per-token-loss` 开启。

`get_sum_of_sample_mean` 根据是否开 CP、是否 per-token，返回不同的闭包；`loss_function` 再把这个标量 loss 按 `num_microbatches / step_global_batch_size * dp_world_size` 缩放，使「分微批累积梯度」与「整批前向」等价（u4-l2 已建立这个概念，这里看它的落地）。

#### 4.4.2 核心流程

```
loss_function(args, batch, num_microbatches, step_global_batch_size, logits):
  1. num_tokens = Σ clamp_min(loss_mask.sum(), 1)
  2. sum_of_sample_mean = get_sum_of_sample_mean(total_lengths, response_lengths,
                                                 loss_masks, rollout_mask_sums,
                                                 calculate_per_token_loss)
  3. match loss_type: policy_loss / value_loss / sft_loss / custom_loss
  4. func(args, batch, logits, sum_of_sample_mean) -> (loss, log_metrics)
  5. 缩放：
       per-token  : loss *= cp_size
       per-sample : loss *= num_microbatches / step_global_batch_size * dp_world_size(with_cp)
  6. 返回 (loss, normalizer, logging_dict)
```

#### 4.4.3 源码精读

归约器构造，见 [cp_utils.py:47-124](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/cp_utils.py#L47-L124)。默认（`sample_denoms=None`）每个样本的分母是自己的 `loss_mask.sum()`：

```python
# cp_utils.py:73-81  per-sample mean（cp=1）
def sum_of_sample_mean(x):
    return sum((x_i * mask_i).sum() / clamp_min(denom, 1)
               for x_i, mask_i, denom in zip(x.split(response_lengths), loss_masks, sample_denoms))
```

而 `--calculate-per-token-loss` 时返回的是 `sum_of_token`（只求和、不除以样本长度，[cp_utils.py:83-89](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/cp_utils.py#L83-L89)），最终由 `loss_function` 的 normalizer=`num_tokens` 来做「除以全局 token 数」。

`loss_function` 的缩放，见 [loss.py:1290-1298](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L1290-L1298)：

```python
if not args.calculate_per_token_loss:
    loss = loss * num_microbatches / step_global_batch_size * mpu.get_data_parallel_world_size(with_context_parallel=True)
else:
    loss = loss * mpu.get_context_parallel_world_size()
```

`loss_type` 分发与可选的梯度检查点重算，见 [loss.py:1264-1279](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L1264-L1279)。`step_global_batch_size` 取代了旧的「每个 DP rank 持有相同 N 条」假设（见 [loss.py:1241-1244](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L1241-L1244) 注释），与动态批打包（u4-l3）配合。

#### 4.4.4 代码实践

**实践目标**：用 CPU 单测理解 per-sample 与 per-token 两种归约在同一批数据上的数值差异。

**操作步骤**：
1. 打开 [tests/test_metric_report.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/test_metric_report.py)，看 [test_metric_report.py:112-114](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/test_metric_report.py#L112-L114) 如何分别用 `calculate_per_token_loss=False/True` 构造归约器。
2. 本地运行：
   ```bash
   pytest tests/test_metric_report.py -q
   ```
3. 对照 [cp_utils.py:127-168](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/cp_utils.py#L127-L168) 的 `reduce_train_step_metrics`，阅读 per-token 与 per-rollout 两种模式下除数（`num_tokens` vs `step_global_batch_size`）与 `cp_factor` 的差别。

**需要观察的现象**：对一组长短不一的样本，per-sample mean 给短样本更大权重；per-token mean 则让长样本主导。这就是 `--calculate-per-token-loss` 开关能改变训练动力学的根源。

**预期结果**：测试通过；你能口头说清「为什么 per-rollout-mean 模式下 `cp_factor=1` 而 per-token-loss 模式下 `cp_factor=cp_size`」——因为每个 CP rank 都用**完整** mask 算 num_tokens，存在 cp_size 倍膨胀，需要 `cp_factor` 抵消（见 [cp_utils.py:140-148](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/cp_utils.py#L140-L148) 注释）。

#### 4.4.5 小练习与答案

**练习 1**：per-sample mean 模式下，`rollout_mask_sums`（`sample_denoms`）为什么要在 step 级别、而不是 micro-batch 级别预算？
**答案**：一个 rollout 的兄弟样本可能落在不同 micro-batch。若分母按 micro-batch 算，落进不同 mb 的同 rollout 样本会各自拿到「部分分母」，归约后这个 rollout 的贡献就被错误稀释。所以必须在 step 级别预算「该 rollout 所有兄弟样本 mask 总和」作为统一分母（[cp_utils.py:60-66](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/cp_utils.py#L60-L66) 注释）。

**练习 2**：`policy_loss_function` 末尾 `if log_probs.numel() == 0: loss += 0 * logits.sum()`（[loss.py:1070-1071](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L1070-L1071)）是干什么的？
**答案**：某些 CP rank 的本地 token 可能全被 mask 掉（`log_probs` 为空），此时 loss 与 logits 没有连边，反向时该 rank 不会调用对应的 reduce-scatter，导致其他 rank 死锁。加一个 `0 * logits.sum()` 强制 autograd 走完整个计算图，但不改变梯度值（类似 [loss.py:1286-1287](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L1286-L1287) allgather-CP 的处理）。

## 5. 综合实践

把本讲四个模块串起来，做一次「reward → 梯度」的端到端追踪。

**任务**：给定一段 GRPO 训练命令片段，画出从 reward 到反向梯度的完整数据流，并标注每个环节对应的源码函数。

**示例命令**（取自 [tests/test_qwen2.5_0.5B_short.py:46-52](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/test_qwen2.5_0.5B_short.py#L46-L52)）：

```bash
--advantage-estimator grpo \
--use-kl-loss --kl-loss-coef 0.00 --kl-loss-type low_var_kl \
--entropy-coef 0.00 --eps-clip 0.2 --eps-clip-high 0.28
```

**要求产出的追踪表**：

| 阶段 | 输入 | 处理函数（文件:行） | 输出 |
|------|------|----------------------|------|
| 算 reward | response | rm_hub（u3-l4） | `sample.reward` |
| 组内归一 | 4 条 reward | `_post_process_rewards` (rollout.py:694) | 归一后标量 reward |
| 算 ref logp | ref 模型 logits | `get_log_probs_and_entropy` (loss.py:470) | `ref_log_probs` |
| 算 old logp | actor 模型 logits | 同上 | `log_probs`（old） |
| 广播 advantage | 标量 reward | `get_grpo_returns` (ppo_utils.py:361) | per-token advantage |
| 算当前 logp | 当前 logits（带梯度） | `get_log_probs_and_entropy` (loss.py:917) | `log_probs`（current） |
| 算 log-ratio | old / current | `policy_loss_function` (loss.py:974-976) | `ppo_kl` |
| clip surrogate | ppo_kl, advantage | `compute_policy_loss` (ppo_utils.py:124) | `pg_loss` |
| 加 KL loss | current, ref | `compute_approx_kl` low_var_kl (ppo_utils.py:11) | `kl_loss`（coef=0，仅监控） |
| 组合 + 归约 | 各项 | `loss = pg_loss - 0*entropy` (loss.py:1051) | 标量 loss |
| Megatron 缩放 | loss | `loss_function` (loss.py:1290) | 缩放后 loss |

**进阶**：把 `--advantage-estimator` 换成 `ppo`，重画这张表——你会多出 critic 的 `get_values`（loss.py:564）、reward shaping（loss.py:731-735）、`get_advantages_and_returns_batch` 的 GAE（ppo_utils.py:471），以及 `value_loss_function`（loss.py:1113）这条额外的价值损失链路。

## 6. 本讲小结

- slime 的优势计算是**总调度式**的：`compute_advantages_and_returns` 按 `--advantage-estimator` 在 grpo/gspo/cispo/ppo/reinforce_plus_plus 之间分发，GRPO 系把组内归一化放在 rollout 端、训练端只做标量广播。
- 必须区分两套对数概率：`ref_log_probs` 服务于 KL 惩罚（管「别跑太远」），`old_log_probs`/`rollout_log_probs` 服务于重要性采样比 \(r=\exp(-\text{ppo\_kl})\)（管「采样偏差」），只有当前 `log_probs` 带梯度。
- `policy_loss_function` 把 PPO clip surrogate、熵正则、KL loss 拼成最终 loss；GSPO 用 sequence-level KL、CISPO 对 ratio 做 stop-gradient 让梯度走 log_probs，二者是稳定性增强变体。
- KL 有两条互斥通道：`kl_coef`（reward shaping，进 advantage）与 `--use-kl-loss`/`kl_loss_coef`（独立 loss 项，GRPO 常用）。
- 归约默认是 **per-sample mean 的和**（长/短回答等权），由 `get_sum_of_sample_mean` 实现；`--calculate-per-token-loss` 切到 per-token 均值。`loss_function` 再按 `num_microbatches/step_global_batch_size*dp_world_size` 缩放以兼容 Megatron 梯度累积。
- off-policy 修正（TIS、OPSM）作为可选乘子接在 pg_loss 上，处理异步/过期样本带来的分布偏差。

## 7. 下一步学习建议

- 接着读 **u6-l4（优势估计器与 RL 算法选择）**：那里会逐一精读 `get_advantages_and_returns_batch` 的 vanilla_gae 与 chunked_gae（前缀扫描）、REINFORCE++ 的折扣回报，以及 `compute_approx_kl` 的 k1/k2/k3 三种估计的数学推导。
- 读 **u6-l5（自定义损失、TIS 与 off-policy 修正）**：深入 `vanilla_tis_function`、`compute_opsm_mask` 与 CISPO 的 off-policy 理论，以及如何用 `--custom-tis-function-path`、`--custom-loss-function-path` 注入自定义逻辑。
- 读 **u8-l3（参数体系全景）**：理解 `--advantage-estimator`、`--eps-clip`、`--kl-loss-type` 等参数如何从命令行经 `parse_args` 流转到这些 loss 函数，以及它们之间的 assert 约束。
- 建议源码精读顺序：先通读 [ppo_utils.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/ppo_utils.py) 的纯数学函数（无并行干扰），再回到 [loss.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py) 看它们如何被 TP/CP 包裹后接入 Megatron。
