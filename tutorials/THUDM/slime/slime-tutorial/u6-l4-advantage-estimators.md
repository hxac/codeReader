# 优势估计器与 RL 算法选择

## 1. 本讲目标

本讲是 RL 数学核心的「精读课」。在 [u4-l4](u4-l4-rl-loss-and-advantage.md) 里我们已经建立了 `reward → advantage → loss` 的整条链条，知道「advantage 是 reward 经过某种变换后、用来给 policy gradient 加权的信号」。但「某种变换」到底有几种、各自数学含义是什么、什么场景该选哪种——这正是本讲要讲透的问题。

学完本讲你应该能够：

1. 说清楚 slime 支持的 6 种 `advantage_estimator`（GRPO / GSPO / CISPO / PPO / REINFORCE++ / REINFORCE++-baseline）在数学上的差别，以及哪些需要 critic。
2. 读懂 `compute_advantages_and_returns` 这个总分发函数，知道一行 `--advantage-estimator` 参数是如何路由到完全不同的代码分支的。
3. 理解 GAE（Generalized Advantage Estimation）的递归定义，以及 slime 为何提供 `vanilla_gae`（反向递归）与 `chunked_gae`（前缀扫描）两种实现。
4. 认识 `compute_approx_kl` 的 `k1` / `k2` / `k3`（`low_var_kl`）三种 KL 估计式的偏差与方差取舍。

## 2. 前置知识

### 2.1 什么是 advantage（优势）

策略梯度定理告诉我们，沿着「让好动作概率变大、坏动作概率变小」的方向更新参数。最朴素的信号是「这条轨迹的累计回报」，但它的方差极大。**优势函数（advantage）** 的直觉是：

\[
A(s,a) = Q(s,a) - V(s)
\]

即「采取动作 \(a\) 相比平均水平好多少」。\(V(s)\) 充当**基线（baseline）**，减去它不改变期望但显著降低梯度估计的方差。RL 框架里大量工作都是在「如何把 reward 变成一个低方差、有学习信号的 advantage」。

### 2.2 GRPO 的核心思想：组内相对

GRPO（Group Relative Policy Optimization）不学一个价值网络 \(V\)，而是**对同一个 prompt 采样多条回复**，用组内其他回复的平均 reward 当作 baseline：

\[
A_i = \frac{r_i - \mathrm{mean}(r_1,\dots,r_n)}{\mathrm{std}(r_1,\dots,r_n)}
\]

这样无需 critic 也能得到零均值的优势。代价是：组内 reward 方差为 0 时（全对或全错），\(A_i\) 全为 0、无梯度——这正是 [u3-l5](u3-l5-dynamic-sampling-filters.md)「动态采样丢弃零方差组」的动机。

### 2.3 on-policy 与重要性采样比（IS ratio）

RL 训练严格要求「用当前策略采样、用当前策略更新」。但实际训练时，采样用的是 rollout 时的旧策略 \(\pi_{old}\)，而我们要更新当前策略 \(\pi_\theta\)，二者有偏差。**重要性采样（Importance Sampling）**用比值修正：

\[
r = \frac{\pi_\theta(a\mid s)}{\pi_{old}(a\mid s)} = \exp\bigl(\log\pi_\theta - \log\pi_{old}\bigr)
\]

slime 里 `ppo_kl = old_log_probs - log_probs`，于是 `ratio = (-ppo_kl).exp() = exp(log_probs - old_log_probs)` 正是这个 IS 比。

### 2.4 KL 散度在 RL 里的两个作用

在 slime 中 KL 有两条**互斥**通道（回顾 u4-l4）：

- **reward shaping**：把 `−kl_coef × KL` 加进 per-token reward，从而进入 advantage。用 `--kl-coef` 控制。
- **独立 loss 项**：把 KL 作为损失的一项单独加上去。用 `--use-kl-loss` / `--kl-loss-coef` 控制。

GRPO 通常 kl_coef=0 而用 `--use-kl-loss`；PPO 通常用 kl_coef 做 reward shaping。本讲的 KL 估计器同时服务于这两条通道。

> 约定：本讲数学公式里，\(\pi_\theta\) 是当前（带梯度的）策略，\(\pi_{ref}\) 是冻结的参考策略，\(\pi_{old}\) 是 rollout 时的行为策略，\(\pi_b\) 是 KL 的「基准分布」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `slime/utils/ppo_utils.py` | 优势/回报计算的全部纯函数：KL 估计、GAE、各 estimator 的 returns。 |
| `slime/backends/megatron_utils/loss.py` | `compute_advantages_and_returns` 总分发、policy/value loss 组合。 |
| `slime/utils/distributed_utils.py` | `distributed_masked_whiten`：跨 DP 组的白化（归一化）。 |
| `slime/utils/arguments.py` | `--advantage-estimator` 参数定义与校验逻辑。 |
| `slime/ray/rollout.py` | `_post_process_rewards`：GRPO 的组内归一化（rollout 侧完成）。 |

读源码的顺序建议：先看 `loss.py:compute_advantages_and_returns` 看清分发骨架，再逐个 estimator 点进 `ppo_utils.py` 看实现，最后回到 `loss.py` 看 normalization 与下游 loss 如何衔接。

## 4. 核心概念与源码讲解

### 4.1 优势估计器分发：compute_advantages_and_returns 总入口

#### 4.1.1 概念说明

「优势估计器」就是回答「给定 reward、log_probs、values，如何算出 advantage」的具体算法。slime 把它做成一个**可插拔的字符串开关** `--advantage-estimator`，支持 6 种取值，外加一个 `--custom-advantage-function-path` 完全自定义的逃生口。这种设计的好处是：数学实现（ppo_utils.py）与调用流程（loss.py）解耦，加一种新算法只需在 dispatch 表里加一个分支并实现对应函数。

#### 4.1.2 核心流程

`compute_advantages_and_returns(args, rollout_data)` 的执行流程是：

1. 从 `rollout_data` 取出 `rollout_log_probs` / `log_probs` / `ref_log_probs` / `rewards` / `values` / `loss_masks` 等字段。
2. **早退**：若不是流水线最后一段（`mpu.is_pipeline_last_stage()` 为假），直接返回——因为 advantage 只在 PP 末段计算。
3. **算 KL**：若 `kl_coef == 0` 则 KL 置零（省去 ref 前向）；否则用 `compute_approx_kl(log_probs[i], ref_log_probs[i], ...)` 算 per-token KL。
4. **分发**：按 `args.advantage_estimator` 走不同分支算出 `(advantages, returns)`。
5. **可选 OPD**：若 `use_opd`，给 advantage 追加蒸馏 KL 惩罚。
6. **可选白化**：若 `normalize_advantages`，跨 DP 组做 masked whiten。
7. 把结果写回 `rollout_data["advantages"]` 和 `rollout_data["returns"]`（in-place）。

#### 4.1.3 源码精读

先看函数签名与文档，它列出了全部支持的 estimator：

[slime/backends/megatron_utils/loss.py:661-685](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L661-L685) — 这是总入口的 docstring，明确支持 `grpo / gspo / cispo / ppo / reinforce_plus_plus / reinforce_plus_plus_baseline` 六种，并说明 `normalize_advantages` 会跨 DP 组白化、`custom_advantage_function_path` 可整体替换。

KL 计算与零优化的关键判断：

[slime/backends/megatron_utils/loss.py:700-713](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L700-L713) — 当 `kl_coef == 0` 时直接构造全零 KL（因为此时根本不会算 ref_log_prob，省一次前向）；否则才真正调用 `compute_approx_kl`。这里的 `log_probs` 已经根据 `use_rollout_logprobs` 在前面决定是取训练侧还是 rollout 侧的对数概率。

各 estimator 的分发分支（GRPO 系与 PPO）：

[slime/backends/megatron_utils/loss.py:720-724](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L720-L724) — **GRPO 系**（`grpo`/`gspo`/`cispo`）：把组归一化后的标量 reward 直接广播到每个 token 作为 advantage，`returns` 同值。注意这里 GRPO 的 advantage **不包含 KL 项**——它的 KL 走的是 `--use-kl-loss` 独立 loss 通道，而非 reward shaping。

[slime/backends/megatron_utils/loss.py:726-738](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c4730197e/slime/backends/megatron_utils/loss.py#L726-L738) — **PPO**：先把 per-token KL 乘上 `-kl_coef` 作为负向 per-token reward，再把标量 reward 加到序列**最后一个 token**（`if cp_rank == 0: k[-1] += reward`，只在 CP 第 0 段加避免重复），然后交给 `get_advantages_and_returns_batch` 跑 GAE。PPO 是唯一需要 `values`（critic 输出）的 estimator。

REINFORCE++ 的两个分支：

[slime/backends/megatron_utils/loss.py:740-761](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L740-L761) — `reinforce_plus_plus` 用带折扣的 returns（`get_reinforce_plus_plus_returns`），`reinforce_plus_plus_baseline` 用基线减法（`get_reinforce_plus_plus_baseline_advantages`）。

> 注意 GRPO/GSPO/CISPO 的「组内归一化」并不在这里做——它在 rollout 侧的 `_post_process_rewards` 提前完成（见 4.1.3 末尾引用），传到这里时 `rewards` 已经是减过组均值的值。这是 slime 把「需要组内信息的步骤」前置到 rollout、把「只需要张量运算的步骤」放在训练侧的一个清晰切分。

[slime/ray/rollout.py:690-710](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L690-L710) — GRPO 系组归一化的真正位置：把 reward 重排成 `[rollout_batch_size, n_samples_per_prompt]`，减组均值；若 `grpo_std_normalization` 再除以组标准差。返回 `(raw_rewards, rewards)`，前者供日志、后者供训练。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：看清 dispatch 表的全貌，理解一个参数字符串如何决定走完全不同的计算路径。

**操作步骤**：

1. 打开 `slime/backends/megatron_utils/loss.py`，定位 `compute_advantages_and_returns`。
2. 用一张表记录 6 个 estimator 各自进入哪个分支、调用了 `ppo_utils.py` 的哪个函数、是否使用 `values`。
3. 重点对照 GRPO 分支（720-724）与 PPO 分支（726-738），注意 PPO 分支多了「把 reward 摊到 per-token + 末位加标量」的预处理。

**需要观察的现象**：只有 PPO 分支调用了 `get_advantages_and_returns_batch`（GAE）；其余要么直接广播标量，要么走 REINFORCE++ 的折扣 returns。

**预期结果**：你能用一句话说出「GRPO 把归一化后的标量 reward 当 advantage；PPO 把 reward 拆成 per-token 再做 GAE」。

#### 4.1.5 小练习与答案

**练习 1**：如果想让 slime 支持一种全新的优势算法（比如 RLOO），最省事的接入点是哪里？

> **参考答案**：用 `--custom-advantage-function-path` 指向自己写的函数，签名 `def fn(args, rollout_data) -> None`，在其中就地写入 `rollout_data["advantages"]` 与 `rollout_data["returns"]`。完全不必改框架源码。

**练习 2**：为什么 `compute_advantages_and_returns` 开头要判断 `mpu.is_pipeline_last_stage()`？

> **参考答案**：advantage 的计算需要模型在 response 上的完整 log_probs/values，这些只在流水线（PP）最后一段才有意义；中间 PP 段没有最终输出，提前返回避免无意义的计算与跨段同步。

---

### 4.2 KL 估计器：compute_approx_kl 的 k1 / k2 / k3 / low_var_kl

#### 4.2.1 概念说明

精确 KL 散度 \( KL(p\|q) = \sum_x p(x)\log\frac{p(x)}{q(x)} \) 需要对整个词表求和，开销巨大。在实践中我们只有**采样到的 token**，因此需要用单个样本（per-token）来**估计** KL。John Schulman 的经典博客《Approximating KL Divergence》给出了三种估计式，slime 的 `compute_approx_kl` 正是它的实现。

设 \( x = \log\pi_\theta - \log\pi_b \)（当前策略对数概率减基准策略对数概率），三种估计式为：

| 类型 | 表达式 | 性质 |
|------|--------|------|
| k1 | \( \hat{D}_{k1} = x \) | 无偏，但**可负**，方差较大 |
| k2 | \( \hat{D}_{k2} = \tfrac{1}{2}x^2 \) | 非负，但**有偏**（它是 f-散度的上界），方差更大 |
| k3 / low_var_kl | \( \hat{D}_{k3} = e^{-x} - 1 + x \) | 非负、低方差、接近无偏 |

k3 是工程上最常用的：它形式为 \( e^y - 1 - y \)（其中 \(y=-x\)），由 \( e^y \geq 1+y \) 恒非负，且方差比 k1 小。

#### 4.2.2 核心流程

`compute_approx_kl(log_probs, log_probs_base, kl_loss_type, importance_ratio=None)`：

1. 算对数比 `log_ratio = log_probs - log_probs_base`。
2. 按 `kl_loss_type` 选 k1 / k2 / k3(low_var_kl) 三套公式之一。
3. 若传了 `importance_ratio`（无偏 KL，DeepSeek-V3.2 风格），把估计值乘上该比值。
4. 仅 `low_var_kl` 做数值 clamp（裁到 [-10, 10]）防爆炸。

整个函数用 `@torch.compile(dynamic=True)` 编译加速。

#### 4.2.3 源码精读

[slime/utils/ppo_utils.py:11-51](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/ppo_utils.py#L11-L51) — `compute_approx_kl` 完整实现。关键三段：

```python
log_ratio = log_probs.float() - log_probs_base.float()
if kl_loss_type == "k1":
    kl = log_ratio                              # 无偏、可负
elif kl_loss_type == "k2":
    kl = log_ratio**2 / 2.0                     # 非负、有偏
elif kl_loss_type in ["k3", "low_var_kl"]:
    log_ratio = -log_ratio
    kl = log_ratio.exp() - 1 - log_ratio        # exp(y)-1-y，非负、低方差
```

末尾两步——无偏化加权与 clamp：

```python
if importance_ratio is not None:          # DeepSeek-V3.2 unbiased KL
    kl = importance_ratio * kl
if kl_loss_type == "low_var_kl":          # 仅 low_var_kl 做数值稳定
    kl = torch.clamp(kl, min=-10, max=10)
```

注意 `k3` 与 `low_var_kl` 用的是**同一套公式**，唯一差别是 `low_var_kl` 多了 clamp——这解释了为什么参数校验里它们常并列出现。

这个函数在两条 KL 通道里都被用到：

- **reward shaping**：在 `compute_advantages_and_returns` 里，`compute_approx_kl(log_probs[i], ref_log_probs[i], kl_loss_type=...)` 算出 KL 后乘 `-kl_coef` 进入 PPO 的 per-token reward（见 [4.1.3](#413-源码精读) 引用的 PPO 分支）。
- **独立 loss 项**：在 `policy_loss_function` 里，当 `--use-kl-loss` 开启时，再次调用 `compute_approx_kl(log_probs, ref_log_probs, ..., importance_ratio=...)` 算 `kl_loss`，见 [slime/backends/megatron_utils/loss.py:1059-1067](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L1059-L1067)；当 `use_unbiased_kl` 时还会传入 `importance_ratio = exp(log_probs - old_log_probs)` 走无偏路径。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：用数值例子直观感受三种 KL 估计的偏差与方差差别。

**操作步骤**：

1. 在一个 Python REPL（无需 GPU）里复制 `compute_approx_kl` 的核心逻辑（去掉 `@torch.compile` 与 importance_ratio 分支，写成纯函数）。
2. 固定 `log_probs_base = 0`，让 `log_probs` 取一组小值如 `[-2, -1, -0.5, 0, 0.5, 1, 2]`。
3. 分别用 `k1` / `k2` / `k3` 算出估计值，打印成表。

**需要观察的现象**：当 `log_probs == log_probs_base`（即 \(x=0\)）时三种估计都应为 0；当 \(x\neq 0\) 时 k1 可正可负、k2 恒正且偏大、k3 恒正且最接近「真值」。

**预期结果**：你会看到 k3 在小 \(x\) 时近似 \( \tfrac{1}{2}x^2 \)（与 k2 接近），但在大 \(|x|\) 时比 k2 增长得慢、更稳定。这正是 `low_var_kl`（k3）成为默认推荐的原因。

> 说明：以上为「源码阅读 + 本地小实验」，不涉及 slime 的分布式训练流程，可在普通 CPU 上完成。

#### 4.2.5 小练习与答案

**练习 1**：`k3` 和 `low_var_kl` 公式完全一样，为什么 slime 要分成两个名字？

> **参考答案**：它们的数学式相同，但 `low_var_kl` 多了一道 `torch.clamp(min=-10, max=10)` 的数值稳定保护，而纯 `k3` 不做 clamp。命名上 `low_var_kl` 强调「带数值稳定的低方差版」。

**练习 2**：什么场景下应该给 `compute_approx_kl` 传 `importance_ratio`？

> **参考答案**：当希望得到**无偏**的 KL 估计（如 DeepSeek-V3.2 的 unbiased KL loss）时。此时 KL 估计值乘以 IS 比 \( \pi_\theta/\pi_{old} \) 进行重要性采样修正，把「用采样分布估计的 KL」矫正为对真实 KL 的无偏估计。slime 中由 `--use-unbiased-kl` 触发。

---

### 4.3 GAE 两种实现：vanilla_gae 与 chunked_gae

#### 4.3.1 概念说明

PPO 用一个价值网络 \(V(s)\) 估计「从状态 \(s\) 出发的期望回报」，再用 **GAE（广义优势估计）** 平衡偏差与方差。GAE 引入一个参数 \(\lambda\in[0,1]\)：

先定义时序差分（TD）误差：

\[
\delta_t = r_t + \gamma V(s_{t+1}) - V(s_t)
\]

GAE 优势是 TD 误差的指数加权和：

\[
\hat{A}_t^{GAE(\gamma,\lambda)} = \sum_{l=0}^{\infty}(\gamma\lambda)^l \delta_{t+l}
\]

它满足一个优雅的**反向递归**（从序列末尾往前算）：

\[
\hat{A}_t = \delta_t + \gamma\lambda \cdot \hat{A}_{t+1}, \qquad \hat{A}_T = 0
\]

\(\lambda=0\) 时退化为单步 TD（低方差高偏差），\(\lambda=1\) 时退化为 Monte-Carlo（高方差低偏差）。returns 定义为 \( \hat{R}_t = \hat{A}_t + V(s_t) \)，用来训练 critic。

slime 提供两种实现：

- **`vanilla_gae`**：直接做上述反向递归，串行依赖长度 \(O(T)\)。
- **`chunked_gae`**：受 FlashLinearAttention 启发，把序列切成 chunk，**chunk 内用并行前缀扫描、chunk 间用递归传递状态**，把串行依赖降到 \(O(T/\text{chunk\_size})\)，更适合长序列与 GPU 并行。

#### 4.3.2 核心流程

`get_advantages_and_returns_batch` 是 PPO 专属的入口（被 [4.1.3](#413-源码精读) 的 PPO 分支调用），它负责：

1. **CP 拼接**：若开了 context parallel（CP），先用 `all_gather_with_cp` 把本 rank 持有的局部 value/reward 拼成完整序列。
2. **padding 对齐**：把变长样本 pad 到 `max_len`，组成 `[B, max_len]` 的批量张量。
3. **选 GAE 实现**：按 `chunked` 标志调 `vanilla_gae` 或 `chunked_gae`。
4. **CP 切回**：若开了 CP，把结果按 `slice_log_prob_with_cp` 切回本 rank 的局部片段。

**vanilla_gae** 的反向递归（核心 4 行）：

```
for t in reversed(range(T)):
    next_value = values[:, t+1] if t < T-1 else 0.0
    delta = rewards[:, t] + gamma * next_value - values[:, t]
    lastgaelam = delta + gamma*lambd * lastgaelam
```

**chunked_gae** 的关键是把反向递归改写成「在反转序列上的前向扫描」：

\[
S[i] = \Delta[i] + w \cdot S[i-1], \qquad w = \gamma\lambda
\]

其闭式解（chunk 内、初始状态为 0）：

\[
S_{local}[t] = \sum_{k=0}^{t} w^{t-k}\,\Delta[k]
\]

这可以用矩阵乘 \( S_{local} = \Delta \cdot M \) 一次算出，其中 \( M[i,j] = w^{j-i} \)（当 \(j\ge i\)）否则 0。chunk 之间则用一个标量状态 `s_prev` 串起来：

\[
S_{global}[t] = S_{local}[t] + w^{t+1}\cdot s_{prev},\qquad s_{prev} \leftarrow S_{global}[\text{末位}]
\]

#### 4.3.3 源码精读

先看 PPO 的 GAE 入口与 CP/padding 处理：

[slime/utils/ppo_utils.py:471-546](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/ppo_utils.py#L471-L546) — `get_advantages_and_returns_batch`。其中 [533-546](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/ppo_utils.py#L533-L546) 是 GAE 实现的选择：

```python
if not chunked:
    full_advantages, full_returns = vanilla_gae(rewards, values, gamma, lambd)
else:
    full_advantages, full_returns = chunked_gae(rewards, values, gamma, lambd)
```

vanilla 实现（直观、串行）：

[slime/utils/ppo_utils.py:579-600](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/ppo_utils.py#L579-L600) — `vanilla_gae`，逐时间步反向递归 `lastgaelam = delta + gamma*lambd*lastgaelam`，最后 `full_returns = full_advantages + values`。

chunked 实现（并行扫描 + chunk 间递归）：

[slime/utils/ppo_utils.py:603-743](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/ppo_utils.py#L603-L743) — `chunked_gae`。几个关键代码点：

- [644-648](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/ppo_utils.py#L644-L648)：构造 TD 误差 `deltas = rewards + gamma*next_values - values`。
- [650-653](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/ppo_utils.py#L650-L653)：把反向递归改写为「反转序列上的前向扫描」，权重 `w = gamma*lambd`。
- [682-693](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/ppo_utils.py#L682-L693)：构造 chunk 内并行扫描核 `M`，`M[i,j] = w^(j-i)`。
- [704-706](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/ppo_utils.py#L704-L706)：chunk 内一次性矩阵乘 `S_local = deltas @ M`。
- [725-734](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/ppo_utils.py#L725-L734)：chunk 间递归，用 `pow_vec = w^(t+1)` 把上一 chunk 末状态注入当前 chunk。

#### 4.3.4 代码实践（源码阅读型 + 手算）

**实践目标**：用一个小例子验证 `vanilla_gae` 与 `chunked_gae` 数值等价，并理解 chunk 边界如何传递状态。

**操作步骤**：

1. 在 REPL 里 `import torch`，复制 `vanilla_gae` 与 `chunked_gae` 两个函数（它们是纯 tensor 运算，不依赖 megatron，可直接用）。
2. 构造一个 toy 输入：`rewards = torch.tensor([[1.0, 0.0, 0.0, 1.0]])`，`values = torch.tensor([[0.5, 0.5, 0.5, 0.5]])`，`gamma=1.0, lambd=0.9`。
3. 分别调用 `vanilla_gae(...)` 与 `chunked_gae(..., chunk_size=2)`。
4. 手算 \( \delta_t \) 序列，再用反向递归手算 vanilla 结果对照。

**需要观察的现象**：两种实现返回的 advantages 应当几乎完全一致（浮点误差内）；改变 `chunk_size` 不应改变 `chunked_gae` 的结果，只改变内部并行度。

**预期结果**：你会确认 chunked 版本是 vanilla 版式的等价加速实现——数学上完全相同，只是把 \(O(T)\) 的串行循环换成了 \(O(T/\text{chunk\_size})\) 的串行 + chunk 内并行。

> 说明：本实践为本地 CPU 小实验，待本地验证 chunk_size 改变时数值不变。

#### 4.3.5 小练习与答案

**练习 1**：`chunked_gae` 把串行依赖从 \(O(T)\) 降到 \(O(T/\text{chunk\_size})\)，代价是什么？

> **参考答案**：代价是 chunk 内要构造并乘一个 \([\text{chunk\_size}, \text{chunk\_size}]\) 的扫描核矩阵 \(M\)，计算量为 \(O(C^2)\)（\(C\) 为 chunk_size）。因此 chunk_size 是「并行度」与「单 chunk 计算量」的折中，默认 128。

**练习 2**：PPO 里 `returns = advantages + values`，这个 returns 用来做什么？

> **参考答案**：returns 是 critic（价值网络）的训练目标——它让 critic 学习预测 \( \hat{R}_t \)。在 [u4-l4](u4-l4-rl-loss-and-advantage.md) 的 `value_loss_function` 里，critic 的输出与 returns 做 clipped MSE。GRPO 没有 critic，所以它的 returns 只是 advantage 的副本、不参与训练。

---

### 4.4 GRPO 系的 returns 与 REINFORCE++ 变体（补充模块）

为了让 6 种 estimator 的全貌完整，本节补充 GRPO 系与 REINFORCE++ 系的 returns 实现。

#### 4.4.1 概念说明

- **GRPO returns**：advantage 就是（组归一化后的）标量 reward 广播到每个 token，returns 同值。
- **REINFORCE++**：带折扣的回报，把 KL 当作 per-token 负 reward、标量 reward 放在最后一个 token，再做反向折扣求和（类似 GAE 但 \(\lambda=0\)）。
- **REINFORCE++-baseline**：把「reward − 基线」广播到每个 token，再减去 KL 惩罚，不做折扣。

#### 4.4.2 源码精读

[slime/utils/ppo_utils.py:361-368](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/ppo_utils.py#L361-L368) — `get_grpo_returns`：`returns[i] = torch.ones_like(kl[i]) * rewards[i]`，纯广播，KL 仅用于取形状。

[slime/utils/ppo_utils.py:371-438](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/ppo_utils.py#L371-L438) — `get_reinforce_plus_plus_returns`：把 `−kl_coef×KL` 作为 per-token reward，标量 reward 加在 mask 最后一个有效 token，再反向递推 \( G_t = r_t + \gamma G_{t+1} \)。CP 下需先 all-gather 拼完整序列再算、最后切回局部。

[slime/utils/ppo_utils.py:441-468](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/ppo_utils.py#L441-L468) — `get_reinforce_plus_plus_baseline_advantages`：广播 `(reward − baseline)` 到每个 token 再减 `kl_coef×KL`，不折扣。

> 校验约束（[slime/utils/arguments.py:1789-1793](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1789-L1793)）：两种 REINFORCE++ 都强制要求 `--normalize-advantages`，因为它们的 advantage 没有内置组归一化，需要靠后续的白化来稳定。

---

### 4.5 白化与 OPD：advantage 的最后两道工序（补充模块）

#### 4.5.1 概念说明

- **白化（whitening）**：把 advantage 归一化成「全局均值 0、方差 1」，进一步降低方差、稳定训练。`distributed_masked_whiten` 跨整个 DP 组统计均值方差（用 Bessel 校正），保证各 rank 用同一组统计量。
- **OPD（On-Policy Distillation）**：在线策略蒸馏，给 advantage 追加一项 `−opd_kl_coef × reverse_kl`（学生 logp − 教师 logp），与 estimator 正交，可叠加在任意 estimator 上。

#### 4.5.2 源码精读

[slime/backends/megatron_utils/loss.py:776-825](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L776-L825) — `compute_advantages_and_returns` 的白化段：把所有 advantage 拼平，构造与 advantage 对齐的 mask（CP 下需把全局 response mask 切回本 rank 局部），调用 `distributed_masked_whiten(all_advs, all_masks, process_group=dp_group, shift_mean=True)`。

[slime/utils/distributed_utils.py:111-167](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/distributed_utils.py#L111-L167) — `distributed_masked_whiten`：本地累加 `sum / sum_sq / mask_sum` 三项，`all_reduce` 聚合到全局，算全局均值方差并做 Bessel 校正，最后 `(values − mean) * rsqrt(var + eps)`。

[slime/backends/megatron_utils/loss.py:620-658](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L620-L658) — `apply_opd_kl_to_advantages`：在 estimator 算完 advantage 之后，就地 `advantages[i] = adv − opd_kl_coef × (student_logp − teacher_logp)`，并把 reverse_kl 存入 rollout_data 供日志。

---

## 5. 综合实践

**任务**：对比 GRPO 与 PPO（带 critic）在 `compute_advantages_and_returns` 中的代码分支，写出两者**所需输入字段**的差异，并据此推断各自的资源开销。

**目标**：把本讲的 dispatch、KL、GAE 三个模块串起来，形成「选 estimator = 选一组数据依赖与计算路径」的整体认知。

**操作步骤**：

1. 打开 `slime/backends/megatron_utils/loss.py`，分别精读 GRPO 分支（[720-724](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L720-L724)）与 PPO 分支（[726-738](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L726-L738)）。
2. 列出每个分支从 `rollout_data` 取用了哪些字段、调用了 `ppo_utils.py` 的哪些函数。
3. 查 [slime/utils/arguments.py:1847](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1847) 的 `args.use_critic = args.advantage_estimator == "ppo"`，确认 critic 是否启用。
4. 填写下面的对照表。

**对照表（参考答案）**：

| 维度 | GRPO（grpo/gspo/cispo） | PPO |
|------|------------------------|-----|
| 需要 `values`（critic 输出） | ❌ 不需要 | ✅ 必须 |
| `args.use_critic` | False（不建 critic） | True（建 critic，复用 actor 同卡） |
| 需要 `ref_log_probs` 用于 advantage | ❌（kl_coef 通常为 0） | ✅ 若 kl_coef≠0 需要 |
| 组内归一化 | ✅ rollout 侧 `_post_process_rewards` 完成 | ❌ 无组归一化 |
| KL 如何进入 | `--use-kl-loss` 独立 loss 项 | `--kl-coef` reward shaping 进 advantage |
| reward 如何变 advantage | 标量 reward 直接广播到 token | reward 摊到 per-token + 末位加标量 → GAE |
| 调用的核心函数 | `get_grpo_returns` | `get_advantages_and_returns_batch` → `vanilla_gae`/`chunked_gae` |
| 额外前向开销 | 只需 actor 一次前向算 logp | 多一次 critic 前向算 values，且 GAE 有计算量 |

**预期结论**：GRPO 用「组内相对」省掉了 critic，代价是依赖 `n_samples_per_prompt` 多采样与零方差丢弃；PPO 用 critic + GAE 得到更平滑的优势，代价是多一个价值网络的训练与显存。这正是「选 estimator」本质上是「在方差/偏差/资源开销之间做权衡」。

**延伸（可选）**：在本表基础上再加 GSPO 与 CISPO 两列。提示——它们的 advantage 计算与 GRPO 完全相同（都走 [720-724](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L720-L724)），差别在下游 `policy_loss_function` 的 loss 形式（GSPO 用 sequence-level KL，CISPO 用 stop-gradient 的 clipped ratio），可对照 [slime/utils/ppo_utils.py:95-121](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/ppo_utils.py#L95-L121) 与 [slime/utils/ppo_utils.py:151-171](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/ppo_utils.py#L151-L171) 阅读。

## 6. 本讲小结

- slime 用一个 `--advantage-estimator` 字符串把 6 种算法（GRPO/GSPO/CISPO/PPO/REINFORCE++/REINFORCE++-baseline）分发到完全不同的计算分支，并有 `--custom-advantage-function-path` 作为逃生口。
- **是否需要 critic 是最大的分水岭**：只有 PPO 需要 `values` 并启用 critic（`use_critic = advantage_estimator == "ppo"`）；GRPO 系用组内相对、REINFORCE++ 系用基线/折扣，都不需要价值网络。
- GRPO 系的组内归一化在 **rollout 侧** `_post_process_rewards` 完成，训练侧只做标量广播；这是「需要组内信息的步骤前置、张量运算留在训练侧」的清晰切分。
- `compute_approx_kl` 实现 Schulman 的三种 KL 估计：k1（无偏可负）、k2（非负有偏）、k3/low_var_kl（非负低方差，最常用），并支持 `importance_ratio` 无偏化（DeepSeek-V3.2 风格）。
- GAE 有两种数值等价的实现：`vanilla_gae` 反向递归 \(O(T)\)，`chunked_gae` 用「chunk 内并行扫描矩阵 + chunk 间递归状态」把串行依赖降到 \(O(T/\text{chunk\_size})\)，更适合长序列。
- advantage 计算后还可能经过两道工序：跨 DP 组的 `distributed_masked_whiten` 白化，以及与 estimator 正交的 OPD 蒸馏 KL 惩罚。

## 7. 下一步学习建议

- **继续读 loss 组合**：本讲的 advantage 只到「写回 rollout_data」，下一步应精读 [u6-l5](u6-l5-custom-loss-offpolicy.md)，看 advantage 如何与 IS ratio、clip、熵正则、OPSM 拼成最终 policy loss，以及 GSPO/CISPO 在 loss 侧的具体差异。
- **读 GSPO/CISPO 的 loss 实现**：`compute_gspo_kl`（[ppo_utils.py:95-121](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/ppo_utils.py#L95-L121)）与 `compute_cispo_loss`（[ppo_utils.py:151-171](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/ppo_utils.py#L151-L171)），理解它们如何在 advantage 相同的情况下改变梯度流。
- **回到参数体系**：结合 [u8-l3](u8-l3-argument-system.md) 看 `--advantage-estimator`、`--kl-coef`、`--use-kl-loss`、`--normalize-advantages` 这一组参数是如何被解析与互相校验的（如 [arguments.py:1787](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1787) 的「kl_coef 与 kl_loss_coef 互斥」断言）。
- **动手扩展**：尝试用 `--custom-advantage-function-path` 实现一个 RLOO（Reinforce with Leave-One-Out）优势函数，体会 slime 把数学实现与流程解耦的设计带来的扩展便利。
