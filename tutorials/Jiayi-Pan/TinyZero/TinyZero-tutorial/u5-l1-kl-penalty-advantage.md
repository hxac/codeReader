# KL 惩罚与优势函数计算

## 1. 本讲目标

上一篇 `u4-l3` 我们顺着 `fit()` 把一步训练的「骨架」走了一遍：generate → reward → **apply_kl_penalty** → **compute_advantage** → update_critic / update_actor。本讲把骨架里这两个最关键的「大脑步骤」单独拆开精读。学完本讲你应该能够：

1. 说清 **分数（score）** 和 **奖励（reward）** 的区别，并能默写 `token_level_rewards = token_level_scores - beta * KL` 的逐 token 公式。
2. 解释 `apply_kl_penalty` 如何从整条 `attention_mask` 里切出 `response_mask`、如何对 KL 在序列上求平均、如何回传给 KL 控制器。
3. 看懂 `kl_penalty` 函数族里 `kl / abs / mse / low_var_kl` 四种估计的公式与取舍。
4. 画出 `compute_advantage` 对 `gae`（需要 critic）与 `grpo`（不需要 critic）两条分支的调度，并理解 GAE 的反向递推与 GRPO 的组内归一化。
5. 回答一个高频面试题：**为什么 `use_kl_loss=True` 时要跳过 KL penalty？**

本讲是后续 `u5-l2`（策略损失）、`u5-l3`（价值损失）、`u5-l5`（GRPO）的数学地基。

## 2. 前置知识

在进入源码前，先用大白话把几个术语立起来。

- **score（分数）vs reward（奖励）**：上一讲 `u4-l4` 里 `RewardManager` 算出来、挂在回答最后一个有效 token 上的那张量叫 `token_level_scores`，它是「任务打分」（countdown 答对给 1.0）。而真正喂给 RL 算优势函数的叫 `token_level_rewards`，它在 score 基础上**扣除了 KL 惩罚**。一句话：reward = score - KL 罚款。
- **KL 散度**：衡量两个概率分布的差异。在 RLHF/R1 Zero 里，我们希望训练中的策略 $\pi_\theta$ 不要跑得离「参考策略」$\pi_{\text{ref}}$（通常是冻结的基座模型）太远，否则模型可能为了刷分而退化成乱码。KL 就是这根「缰绳」。
- **token 级 vs 序列级**：模型的输出是一条 token 序列。我们把每个 token 位置都看成一个时间步 $t$，于是 reward、value、advantage 都是 `[batch, response_length]` 形状的张量，最后一维是序列。
- **优势函数 $A_t$**：衡量「在状态 $s_t$ 采取动作 $a_t$ 比平均水平好多少」。PPO 用它给策略梯度定方向：$A_t>0$ 鼓励这个动作，$A_t<0$ 抑制。
- **回报 $R_t$**：从 $t$ 往后的累积奖励，用来监督 critic。关系是 $R_t = A_t + V(s_t)$。
- **GAE**（Generalized Advantage Estimation）：用 critic 的价值估计 + 一个参数 $\lambda$ 平滑地估计 $A_t$，是 PPO 的标准优势估计法。
- **GRPO**（Group Relative Policy Optimization）：DeepSeek R1 用的方法，**不需要 critic**，而是对同一个 prompt 的多次采样做组内归一化得到优势。TinyZero 同时支持这两条路。

如果你对 PPO 的 importance ratio、clipping 还不熟，没关系——那些是 `u5-l2` 的内容，本讲只到「得到 advantage」为止。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [verl/trainer/ppo/ray_trainer.py:84-113](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L84-L113) | 模块级函数 `apply_kl_penalty`：从 score 扣 KL 得到 reward，并更新 KL 控制器。 |
| [verl/trainer/ppo/ray_trainer.py:116-147](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L116-L147) | 模块级函数 `compute_advantage`：按 `gae`/`grpo` 调度优势函数计算。 |
| [verl/trainer/ppo/core_algos.py:242-274](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L242-L274) | `kl_penalty`：四种 KL 估计（`kl/abs/mse/low_var_kl`）。 |
| [verl/trainer/ppo/core_algos.py:70-107](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L70-L107) | `compute_gae_advantage_return`：GAE 反向递推。 |
| [verl/trainer/ppo/core_algos.py:111-155](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L111-L155) | `compute_grpo_outcome_advantage`：GRPO 组内归一化。 |
| [verl/utils/torch_functional.py:107-109](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/torch_functional.py#L107-L109) | `masked_mean`：在 mask 有效位上求平均。 |
| [verl/utils/torch_functional.py:130-136](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/torch_functional.py#L130-L136) | `masked_whiten`：对 advantage 做零均值、单位方差归一化。 |
| [verl/trainer/ppo/ray_trainer.py:630-644](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L630-L644) | `fit()` 里调用这两步的真实位置（含 `use_kl_loss` 分支）。 |
| [verl/workers/actor/dp_actor.py:259-269](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L259-L269) | actor 端 `use_kl_loss=True` 时把 KL 直接加进 loss 的代码。 |

配置默认值见 [verl/trainer/config/ppo_trainer.yaml:138-146](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L138-L146)（`algorithm` 分组）与 [verl/trainer/config/ppo_trainer.yaml:29-32](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L29-L32)（actor 的 `use_kl_loss` 等）。

---

## 4. 核心概念与源码讲解

### 4.1 从分数到奖励：apply_kl_penalty 与 token 级 KL 惩罚

#### 4.1.1 概念说明

`apply_kl_penalty` 解决的问题是：**把「任务分」翻译成「RL 奖励」**。

任务分 `token_level_scores` 只反映「答得对不对」，它不知道模型有没有偏离基座模型。如果我们只拿 score 去训练，模型很可能为了刷高分而在输出分布上乱漂（例如一直输出固定模板），最终退化。所以在 score 上**扣除一个与 KL 成正比的罚项**：

\[
r_t = s_t - \beta \cdot \widehat{\mathrm{KL}}_t \cdot m_t
\]

其中：

- $s_t$ 是 `token_level_scores` 在 token $t$ 的值（绝大多数位置是 0，只有末位有效 token 非零——见 `u4-l4` 的稀疏放置）。
- $\widehat{\mathrm{KL}}_t$ 是 token $t$ 处的 KL 估计（由 `kl_penalty` 算出，4.2 节详解）。
- $m_t$ 是 `response_mask`，把 prompt 段和 response 的右填充位清零。
- $\beta$ 是 KL 系数（TinyZero 默认 `kl_coef=0.001`），由 KL 控制器提供，可固定也可自适应。

注意一个工程不变量：KL 罚项**只作用在 response 段的有效 token 上**，prompt 和 padding 一律不罚——这是 `response_mask` 的职责。

#### 4.1.2 核心流程

`apply_kl_penalty` 在 driver 进程上跑（不算梯度，只做轻量张量运算），流程是：

```text
输入 data(DataProto)
  ├─ responses, token_level_scores, attention_mask, old_log_probs, ref_log_prob
 1. response_mask = attention_mask[:, -response_length:]     # 切出 response 段
 2. 若 batch 里有 ref_log_prob:
       kld = kl_penalty(old_log_probs, ref_log_prob)          # (B, L_resp)
       kld = kld * response_mask                              # 只罚有效 response token
       beta = kl_ctrl.value
    否则:
       beta = 0; kld = zeros                                   # 没有参考策略就不罚
 3. token_level_rewards = token_level_scores - beta * kld     # ★核心公式
 4. current_kl = masked_mean(kld, response_mask, axis=-1)     # 每条序列平均
       current_kl = mean over batch                            # 整个 batch 平均
 5. kl_ctrl.update(current_kl, batch_size)                    # 更新 beta（固定型为空操作）
 6. 写回 data.batch['token_level_rewards']; 返回 metrics
```

第 4 步算出的 `current_kl` 是一个**标量监控量**，会被记录成指标 `critic/kl`，同时喂给 KL 控制器决定下一步的 $\beta$。

#### 4.1.3 源码精读

下面这段就是 [verl/trainer/ppo/ray_trainer.py:84-113](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L84-L113) 的核心，逐行标注：

```python
def apply_kl_penalty(data: DataProto, kl_ctrl: core_algos.AdaptiveKLController, kl_penalty='kl'):
    responses = data.batch['responses']
    response_length = responses.size(1)
    token_level_scores = data.batch['token_level_scores']
    batch_size = data.batch.batch_size[0]
    attention_mask = data.batch['attention_mask']
    response_mask = attention_mask[:, -response_length:]      # 切最后 response_length 列

    if 'ref_log_prob' in data.batch.keys():
        kld = core_algos.kl_penalty(data.batch['old_log_probs'], data.batch['ref_log_prob'],
                                    kl_penalty=kl_penalty)    # (B, response_length)
        kld = kld * response_mask                             # 屏蔽 prompt 与 padding
        beta = kl_ctrl.value
    else:
        beta = 0
        kld = torch.zeros_like(response_mask, dtype=torch.float32)

    token_level_rewards = token_level_scores - beta * kld     # ★ reward = score - beta*KL

    current_kl = masked_mean(kld, mask=response_mask, axis=-1)  # 序列内平均 → (B,)
    current_kl = torch.mean(current_kl, dim=0).item()           # batch 平均 → 标量

    kl_ctrl.update(current_kl=current_kl, n_steps=batch_size)   # 更新 beta
    data.batch['token_level_rewards'] = token_level_rewards

    metrics = {'critic/kl': current_kl, 'critic/kl_coeff': beta}
    return data, metrics
```

几个要点：

- **`response_mask = attention_mask[:, -response_length:]`**：回忆 `u2-l3`，prompt 左填充、response 在右端追加。整条 `attention_mask` 形如 `[0..0 1..1]`，**最后 `response_length` 列**正好覆盖 response 槽位；其中有效 token 为 1、被右填充的尾部位为 0。这一行是后续所有「只在 response 上算」操作的基础。
- **`old_log_probs`** 是 rollout 时**旧策略**的对数概率（`generate_sequences` 末尾 recompute 得到，见 `u6-l1`），`ref_log_prob` 是**冻结参考策略**的对数概率。两者都来自上一阶段的 worker 前向。
- **`masked_mean`**（[torch_functional.py:107-109](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/torch_functional.py#L107-L109)）实现是 `(values * mask).sum(axis) / mask.sum(axis)`，即在有效位上求和再除以有效位数，自动忽略 padding。
- **没有 ref policy 时 $\beta=0$**：等价于「不约束漂移」，`token_level_rewards == token_level_scores`。`RayPPOTrainer.__init__` 在 `use_reference_policy=False` 时会创建一个 `FixedKLController(kl_coef=0.)`（见 [ray_trainer.py:327-338](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L327-L338)），所以这条else分支其实很少触发，是双保险。

调用点在 `fit()` 的 `adv` 计时段（[ray_trainer.py:630-637](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L630-L637)）：

```python
if not self.config.actor_rollout_ref.actor.use_kl_loss:
    batch, kl_metrics = apply_kl_penalty(batch, kl_ctrl=self.kl_ctrl,
                                         kl_penalty=self.config.algorithm.kl_penalty)
    metrics.update(kl_metrics)
else:
    batch.batch['token_level_rewards'] = batch.batch['token_level_scores']
```

这就是本讲要重点回答的问题的入口：**`use_kl_loss=True` 时跳过 `apply_kl_penalty`，直接让 `token_level_rewards = token_level_scores`**。为什么？答案放在 4.1.5 和综合实践里。

#### 4.1.4 代码实践

> 实践目标：用一个小张量手算 `apply_kl_penalty`，把逐元素公式落实成具体数字。

给定 `batch_size=1, response_length=3`，`beta=0.001`，`kl_penalty='kl'`：

| 张量 | 值 |
| --- | --- |
| `token_level_scores` | `[[0, 0, 1.0]]`（末位放 1.0 分） |
| `old_log_probs` | `[[-1.0, -0.8, -1.2]]` |
| `ref_log_prob` | `[[-1.1, -0.7, -1.0]]` |
| `response_mask` | `[[1, 1, 1]]`（三个 token 都有效） |

**操作步骤（请你在纸上跟着算一遍）：**

1. 用默认 `kl_penalty='kl'` 算 `kld = old_log_probs - ref_log_prob`：

   \[
   \widehat{\mathrm{KL}} = [-1.0-(-1.1),\ -0.8-(-0.7),\ -1.2-(-1.0)] = [0.1,\ -0.1,\ -0.2]
   \]

2. 乘 mask（全 1，不变），`token_level_rewards = scores - 0.001 * kld`：

   \[
   [0,\ 0,\ 1.0] - 0.001 \times [0.1,\ -0.1,\ -0.2]
   = [-0.0001,\ 0.0001,\ 1.0002]
   \]

3. `current_kl = masked_mean(kld, mask)`：$(0.1 + (-0.1) + (-0.2))/3 = -0.0667$。

**需要观察的现象：**

- KL 罚项把末位的 1.0 抬到了 1.0002（因为该位 $\widehat{\mathrm{KL}}$ 是负的，减负等于加）；而前两个本该是 0 的位置出现了 $-0.0001$、$0.0001$ 的**渗漏**。这说明 KL 罚会把「稀疏的 score」改造成「每个有效 token 都带一点信号」的稠密 reward。
- `current_kl` 是**负数**（$-0.0667$）。这是因为朴素的 `kl = logp_old - logp_ref` 是 KL 的**无偏估计但单样本可为负**。这正是 4.2 节 `low_var_kl` 想缓解的问题。

**预期结果：**`token_level_rewards = [[-0.0001, 0.0001, 1.0002]]`，`critic/kl ≈ -0.0667`。

**为什么 `use_kl_loss=True` 时跳过 KL penalty？**（思考 30 秒再看答案）因为当 `use_kl_loss=True` 时，KL 已经在 actor 端**直接加进了策略损失**：

```python
# verl/workers/actor/dp_actor.py:259-269
if self.config.use_kl_loss:
    kld = core_algos.kl_penalty(logprob=log_prob, ref_logprob=ref_log_prob,
                                kl_penalty=self.config.kl_loss_type)   # 默认 low_var_kl
    kl_loss = masked_mean(kld, response_mask)
    policy_loss = policy_loss - kl_loss * self.config.kl_loss_coef     # ★ KL 进 loss
```

如果这里**还**在 reward 上扣一次 KL，那么 KL 会被算两遍（一次经 reward→advantage→pg_loss，一次直接进 loss），等价于把 KL 系数翻倍、约束过强。所以 GRPO 走 `use_kl_loss=True` 这条路时，`token_level_rewards` 保持为纯 score，KL 改由 loss 端的 `kl_loss_coef` 控制。这就是 `u4-l3` 里「GRPO 通过 use_kl_loss 直接在 loss 中加 KL」的代码落点。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `beta` 从 0.001 调到 0.1，对训练有什么影响？
> **答案**：KL 罚变强 100 倍，策略更难偏离 ref policy。好处是输出更稳、更接近基座；坏处是约束过强时模型「不敢探索」，score 上升变慢甚至停滞。这正是 `u7-l6` 调参时 `kl_coef` 的核心权衡。

**练习 2**：为什么 `kld = kld * response_mask` 这一行不能省？
> **答案**：prompt 段和 response 的右填充位上，`old_log_probs`/`ref_log_prob` 的值是「填充出来的」，不对应真实生成动作。若不乘 mask，这些伪位置会向 reward 注入噪声。乘 mask 把它们清零，保证罚项只计真实 response token。

**练习 3**：`apply_kl_penalty` 为什么放在 driver 进程而不是 worker 上？
> **答案**：它只做逐元素减法和一次 `masked_mean`，计算量极小，无需 GPU；而它要读写完整 `DataProto` 并更新 `kl_ctrl` 这个全局状态。放在 driver 便于集中管理状态、避免跨 worker 同步（见 `u3-2` 单控制器思想）。

---

### 4.2 KL 的多种估计：kl_penalty 函数族

#### 4.2.1 概念说明

真实的 KL 散度 $\mathrm{KL}(\pi_\theta \| \pi_{\text{ref}})$ 需要对整个词表求和，开销大。实践中我们只有每个生成 token 的对数概率，于是用**采样估计**。`kl_penalty` 提供四种估计子，对应 `algorithm.kl_penalty` 配置（TinyZero 默认 `'kl'`）：

\[
\mathrm{KL}(\pi \| \pi_{\text{ref}}) = \mathbb{E}_{a\sim\pi}\bigl[\log\tfrac{\pi(a)}{\pi_{\text{ref}}(a)}\bigr]
\]

因为我们采到的 token 恰好来自 $\pi$（旧策略），所以 $\log\pi(a) - \log\pi_{\text{ref}}(a)$ 就是 KL 的**无偏单样本估计**——这就是默认的 `'kl'`。

#### 4.2.2 核心流程

四种估计子的公式对照（令 $d = \log\pi - \log\pi_{\text{ref}}$）：

| 取值 | 公式 | 特点 |
| --- | --- | --- |
| `kl`（默认） | $d$ | 无偏，但**单样本方差大、可为负** |
| `abs` | $|d|$ | 恒非负，但有偏 |
| `mse` | $0.5\,d^2$ | 平滑、恒非负，对大偏差更敏感 |
| `low_var_kl` | $\bigl[\,\mathrm{ratio} - (\log\pi_{\text{ref}}-\log\pi) - 1\,\bigger],\ \mathrm{ratio}=e^{\log\pi_{\text{ref}}-\log\pi}$ | **低方差**无偏估计（Schulman 2020），裁剪到 $[-10,10]$ |

`low_var_kl` 的推导来自 John Schulman 的博客 *Approximating KL divergence*：通过控制变量法把方差降下来，是 GRPO loss 端默认用的估计子（`actor.kl_loss_type: low_var_kl`）。

#### 4.2.3 源码精读

[verl/trainer/ppo/core_algos.py:242-274](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L242-L274)：

```python
def kl_penalty(logprob, ref_logprob, kl_penalty):
    if kl_penalty == "kl":
        return logprob - ref_logprob                         # 无偏，可为负
    if kl_penalty == "abs":
        return (logprob - ref_logprob).abs()
    if kl_penalty == "mse":
        return 0.5 * (logprob - ref_logprob).square()
    if kl_penalty == 'low_var_kl':                            # Schulman 低方差估计
        kl = ref_logprob - logprob
        ratio = torch.exp(kl)
        kld = (ratio - kl - 1).contiguous()
        return torch.clamp(kld, min=-10, max=10)             # 防数值爆炸
    if kl_penalty == "full":
        raise NotImplementedError                            # 需要全词表 logits，未实现
    raise NotImplementedError
```

注意：

- 参数名是 `logprob`（当前/旧策略）和 `ref_logprob`（参考策略）。在 `apply_kl_penalty` 里传入的是 `old_log_probs` 与 `ref_log_prob`；在 actor loss 里（`use_kl_loss=True`）传入的是当前步重算的 `log_prob` 与 `ref_log_prob`——**同一函数复用于两处**。
- `low_var_kl` 的 `clamp(-10, 10)` 是数值稳定保护：当 $\pi$ 与 $\pi_{\text{ref}}$ 差异极大时 `ratio = exp(...)` 会爆炸。
- `full` 分支需要每个 token 在**整个词表**上的 logits 才能算精确 KL，TinyZero 没有实现，直接 `NotImplementedError`。

配置位置：[ppo_trainer.yaml:142](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L142) `kl_penalty: kl`，以及 actor 端 [ppo_trainer.yaml:32](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L32) `kl_loss_type: low_var_kl`。

#### 4.2.4 代码实践

> 实践目标：用一段可运行脚本对比四种估计在同一组对数概率上的输出差异。

操作步骤（需已按 `u1-l2` 安装 verl）：

```python
# 示例代码：可直接 python 运行（需 verl 可 import）
import torch
from verl.trainer.ppo import core_algos

logprob    = torch.tensor([[-1.0, -0.8, -1.2]])
ref_logprob= torch.tensor([[-1.1, -0.7, -1.0]])
for mode in ["kl", "abs", "mse", "low_var_kl"]:
    print(mode, core_algos.kl_penalty(logprob, ref_logprob, mode).tolist())
```

需要观察的现象：

- `kl` 会出现负值（与 4.1.4 的 $-0.0667$ 对应）。
- `abs` 把负值翻正。
- `mse` 的量纲和前两者不同（是平方量级），所以它一般要配更小的系数。
- `low_var_kl` 的值与 `kl` 接近但更稳定、且被 clamp 过。

预期结果：四个估计数值不同、符号行为不同；**待本地验证**具体取值。

#### 4.2.5 小练习与答案

**练习 1**：为什么 GRPO 在 loss 端默认用 `low_var_kl` 而不是 `kl`？
> **答案**：loss 端的 KL 要参与反向传播，方差大、可为负的 `kl` 会让梯度剧烈抖动。`low_var_kl` 通过控制变量法降低方差，训练更稳。

**练习 2**：把 `algorithm.kl_penalty` 从 `kl` 改成 `mse`，需要同步改什么吗？
> **答案**：需要重新调 `kl_coef`。因为 `mse=0.5*d^2` 与 `kl=d` 量纲不同，沿用同一个 `kl_coef` 会导致罚项量级骤变，训练行为完全不同。这是配置耦合点。

---

### 4.3 优势函数与回报：compute_advantage 的两条分支

#### 4.3.1 概念说明

有了 `token_level_rewards`，下一步是估计每个 token 的**优势** $A_t$ 和**回报** $R_t$。`compute_advantage` 是一个分发器：按 `algorithm.adv_estimator`（默认 `gae`）选两条互斥的路：

- **`gae`**：需要 critic。用价值函数 $V$ 做 GAE 反向递推。这也是 PPO 的经典做法。
- **`grpo`**：**不需要 critic**。对同一 prompt 的 $n$ 次采样（`rollout.n`）做组内归一化。这是 R1 Zero / DeepSeek 的做法，TinyZero 的「招牌」。

回忆 `u4-l2`：`adv_estimator` 决定了 `init_workers` 里 `use_critic` 的取值——`gae` 时为 True（建 critic），`grpo` 时为 False。

#### 4.3.2 核心流程

**GAE 分支**（需要 `values`）。对每条序列从右往左递推：

\[
\delta_t = r_t + \gamma\, V_{t+1} - V_t
\]
\[
A_t = \delta_t + \gamma\lambda\, A_{t+1}
\]

边界条件：序列末尾 $A_{T}=0$（代码里 `lastgaelam` 初值 0），$V_{T+1}=0$（`nextvalues` 在最后一步取 0）。然后：

\[
R_t = A_t + V_t,\qquad A \leftarrow \mathrm{masked\_whiten}(A,\ \text{eos\_mask})
\]

注意 `returns` 用的是**归一化之前**的 $A_t$，而写回 batch 的 `advantages` 是**归一化之后**的（见源码顺序）。

**GRPO 分支**（不需要 `values`，但需要 `uid` 分组）。把 token 级 reward 压成每条样本的标量分，再按组归一化：

\[
\text{score}_i = \sum_t r_{i,t}
\]
\[
\hat{A}_i = \frac{\text{score}_i - \mu_g}{\sigma_g + \epsilon}
\]

其中 $\mu_g,\sigma_g$ 是「同一 uid 组内」各样本分的均值与标准差，$\epsilon=10^{-6}$。最后把这个标量 advantage 广播回序列每个有效 token：`tile([1, response_length]) * eos_mask`。GRPO 里 `returns` 直接等于 `advantages`（因为没有 $V_t$ 可加）。

> **为什么 GRPO 能省掉 critic？** critic 的作用是给「比平均好多少」提供基线 $V_t$。GRPO 换了个思路：既然对同一 prompt 采了 $n$ 条回答，那这 $n$ 条分数的**组内均值**就是天然基线，组内标准差负责归一化。等价于「同一个题，答得比同组平均好就鼓励、差就抑制」，完全不需要再训一个价值网络。

#### 4.3.3 源码精读

先看分发器 [verl/trainer/ppo/ray_trainer.py:116-147](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L116-L147)：

```python
def compute_advantage(data: DataProto, adv_estimator, gamma=1.0, lam=1.0, num_repeat=1):
    if adv_estimator == 'gae':
        values = data.batch['values']
        responses = data.batch['responses']
        response_length = responses.size(-1)
        attention_mask = data.batch['attention_mask']
        response_mask = attention_mask[:, -response_length:]      # 同样切 response 段
        token_level_rewards = data.batch['token_level_rewards']
        advantages, returns = core_algos.compute_gae_advantage_return(
            token_level_rewards=token_level_rewards, values=values,
            eos_mask=response_mask, gamma=gamma, lam=lam)
        data.batch['advantages'] = advantages
        data.batch['returns'] = returns
    elif adv_estimator == 'grpo':
        token_level_rewards = data.batch['token_level_rewards']
        index = data.non_tensor_batch['uid']                       # ★ 用 uid 分组
        ...
        advantages, returns = core_algos.compute_grpo_outcome_advantage(
            token_level_rewards=token_level_rewards, eos_mask=response_mask, index=index)
        data.batch['advantages'] = advantages
        data.batch['returns'] = returns
    else:
        raise NotImplementedError
    return data
```

两个分支都从 `attention_mask` 切出 `response_mask` 当 `eos_mask`（有效 response token 为 1）。`grpo` 分支关键的 `index = data.non_tensor_batch['uid']` 正是 `u4-l3` 里在 `repeat` 之前赋的那个 uuid——**同一 prompt 复制 n 份后 uid 相同，从而能被识别为同一组**。

**GAE 实现** [core_algos.py:70-107](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L70-L107)：

```python
with torch.no_grad():
    lastgaelam = 0
    advantages_reversed = []
    gen_len = token_level_rewards.shape[-1]
    for t in reversed(range(gen_len)):
        nextvalues = values[:, t + 1] if t < gen_len - 1 else 0.0          # V_{t+1}，末尾取 0
        delta = token_level_rewards[:, t] + gamma * nextvalues - values[:, t]
        lastgaelam = delta + gamma * lam * lastgaelam                      # A_t = δ_t + γλ A_{t+1}
        advantages_reversed.append(lastgaelam)
    advantages = torch.stack(advantages_reversed[::-1], dim=1)             # 反转回正序
    returns = advantages + values                                          # ★ 用未归一化的 A
    advantages = verl_F.masked_whiten(advantages, eos_mask)               # 再归一化
return advantages, returns
```

`masked_whiten`（[torch_functional.py:130-136](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/torch_functional.py#L130-L136)）做 $(A-\mu)/\sqrt{\sigma^2+10^{-8}}$，把 advantage 拉到零均值单位方差，稳定策略梯度。注意它用 `unbiased=True` 的 `masked_var`（带 Bessel 校正，见 [torch_functional.py:112-127](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/torch_functional.py#L112-L127)）。

**GRPO 实现** [core_algos.py:111-155](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L111-L155)：

```python
non_zero_mask = (token_level_rewards != 0)
scores = (token_level_rewards * non_zero_mask).sum(dim=-1)     # 压成每条样本的标量分
# 按 index(uid) 分组求 mean/std
for idx in id2score:
    if len(id2score[idx]) == 1:                                # 组内只有 1 条
        id2mean[idx] = 0.0; id2std[idx] = 1.0                  # 退化：adv = score 本身
    elif len(id2score[idx]) > 1:
        id2mean[idx] = mean(...); id2std[idx] = std(...)
for i in range(bsz):
    scores[i] = (scores[i] - id2mean[index[i]]) / (id2std[index[i]] + epsilon)
scores = scores.unsqueeze(-1).tile([1, response_length]) * eos_mask   # 广播回 token 级
return scores, scores
```

要点：

- `scores` 先 `sum(dim=-1)` 把 token 级 reward 压成标量——因为 GRPO 只看 **outcome**（最终结果分），不看过程。
- 组内只有 1 条时（`rollout.n=1`）设 mean=0、std=1，于是 $\hat{A} = \text{score}$，等于不归一化。这是 `u5-l5` 会再强调的退化情形。
- 最后 `tile * eos_mask` 把标量 advantage **均匀涂到序列每个有效 token**——与 GAE「每个 token 不同 advantage」形成对比。

`compute_advantage` 的调用点在 `fit()` 的 [ray_trainer.py:640-644](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L640-L644)，紧接 `apply_kl_penalty` 之后。

#### 4.3.4 代码实践

> 实践目标：手算一个长度 4 的 GAE，验证你对反向递推与 `returns` 的理解。

给定单条序列，`gamma=1, lam=1`：

| $t$ | 0 | 1 | 2 | 3 |
| --- | --- | --- | --- | --- |
| $r_t$ (`token_level_rewards`) | 0 | 0 | 0 | 1.0 |
| $V_t$ (`values`) | 0.2 | 0.3 | 0.5 | 0.4 |

**操作步骤（从右往左算）：**

1. $t=3$（末尾）：$V_4=0$，$\delta_3 = 1.0 + 1\cdot0 - 0.4 = 0.6$；$A_3 = 0.6 + 1\cdot1\cdot0 = 0.6$。
2. $t=2$：$\delta_2 = 0 + 1\cdot V_3 - V_2 = 0.4 - 0.5 = -0.1$；$A_2 = -0.1 + 1\cdot1\cdot0.6 = 0.5$。
3. $t=1$：$\delta_1 = 0 + V_2 - V_1 = 0.5 - 0.3 = 0.2$；$A_1 = 0.2 + 0.5 = 0.7$。
4. $t=0$：$\delta_0 = 0 + V_1 - V_0 = 0.3 - 0.2 = 0.1$；$A_0 = 0.1 + 0.7 = 0.8$。

**需要观察的现象：**

- 原始 advantage = $[0.8, 0.7, 0.5, 0.6]$，奖励只在末位给出，但通过 $V$ 的递推**反向「渗透」到前面每个 token**——这就是 GAE 把稀疏 outcome 信用分配（credit assignment）到过程 token 的机制。
- `returns = advantages + values`（用未归一化的 A）$= [1.0, 1.0, 1.0, 1.0]$，恰好等于从各步起算的真实累积回报（末位 reward=1，其余 0）。这验证了 $R_t = A_t + V_t$ 的恒等关系。
- 写回 batch 的 `advantages` 还要再过一次 `masked_whiten`，变成零均值单位方差。

**预期结果：**原始 $A=[0.8,0.7,0.5,0.6]$，$R=[1.0,1.0,1.0,1.0]$；归一化后的 advantage 数值随 mask 上有效位数而定（**待本地验证**精确值）。

可运行验证脚本（示例代码）：

```python
import torch
from verl.trainer.ppo import core_algos
r = torch.tensor([[0., 0., 0., 1.0]])
v = torch.tensor([[0.2, 0.3, 0.5, 0.4]])
mask = torch.tensor([[1., 1., 1., 1.]])
adv, ret = core_algos.compute_gae_advantage_return(r, v, mask, gamma=1.0, lam=1.0)
print("returns", ret.tolist())   # 预期 [[1.0, 1.0, 1.0, 1.0]]
```

#### 4.3.5 小练习与答案

**练习 1**：`gae` 分支里，`returns` 用的是归一化前还是归一化后的 advantage？为什么？
> **答案**：归一化前。代码顺序是 `returns = advantages + values` 在前，`advantages = masked_whiten(...)` 在后。因为 $R_t = A_t + V_t$ 是物理恒等式（回报 = 优势 + 基线），必须用未失真的 $A_t$；归一化是只为稳定策略梯度的工程手段，不该污染用来监督 critic 的 returns。

**练习 2**：GRPO 里 `rollout.n=1` 时 advantage 会变成什么？
> **答案**：组内只有 1 条样本，代码设 `mean=0, std=1`，于是 $\hat{A} = (\text{score}-0)/(1+\epsilon) \approx \text{score}$，等于不做组内归一化。此时 GRPO 退化为「直接拿原始分当优势」，失去相对比较的意义——所以用 GRPO 一定要 `rollout.n>1`。

**练习 3**：为什么 `compute_advantage` 的 `eos_mask` 就是 `response_mask`？
> **答案**：变量名 `eos_mask` 沿用自 TRL，含义是「有效 response token 标志位」（EOS 之后的位为 0）。在本仓库里它直接复用 `attention_mask[:, -response_length:]`，即 response 段的有效位。归一化、求平均都只在这些位上进行，padding 不参与。

---

## 5. 综合实践

> **任务：把一条 mini-batch 从 score 一路推到 advantage，串起本讲三个模块。**

设 `batch_size=2, response_length=3`，两个样本**属于同一个 prompt**（GRPO 视角下 uid 相同），用以同时观察 KL penalty 与两种 advantage：

| 量 | 样本 0 | 样本 1 |
| --- | --- | --- |
| `token_level_scores` | `[0, 0, 1.0]`（答对） | `[0, 0, 0.0]`（答错） |
| `old_log_probs` | `[-1.0, -0.8, -1.2]` | `[-1.5, -1.4, -1.6]` |
| `ref_log_prob` | `[-1.1, -0.7, -1.0]` | `[-1.3, -1.3, -1.4]` |
| `values`（仅 GAE 用） | `[0.3, 0.4, 0.5]` | `[0.2, 0.2, 0.2]` |

取 `beta=0.001, kl_penalty='kl', gamma=1, lam=1, response_mask` 全 1。

**操作步骤：**

1. **KL penalty**（4.1）：对每个样本算 `kld = old_log_probs - ref_log_prob`，再 `reward = score - 0.001*kld`。
   - 样本 0：`kld=[0.1,-0.1,-0.2]` → `reward=[-0.0001, 0.0001, 1.0002]`（与 4.1.4 一致）。
   - 样本 1：`kld=[-0.2,-0.1,-0.2]` → `reward=[0.0002, 0.0001, 0.0002]`（答错但 KL 为负，reward 略正）。
2. **GAE advantage**（4.3）：对每个样本用各自 `values` 做反向递推（仿 4.3.4）。观察答对的样本 0 优势显著为正、答错的样本 1 为负。
3. **GRPO advantage**（4.3）：把两条 reward 各自 `sum` 成标量分（样本 0 ≈ 1.0002，样本 1 ≈ 0.0005），再组内归一化：$\mu,\sigma$ 由这两条算出，$\hat{A}_0>0, \hat{A}_1<0$。
4. **切换对比**：把 `algorithm.adv_estimator` 在 `gae` 与 `grpo` 间切换（改 `train_tiny_zero.sh` 的 Hydra 覆盖），同时把 `actor_rollout_ref.actor.use_kl_loss` 设为 `True`（GRPO 配套）。

**需要观察的现象与预期结果：**

- 同一组分数，**GAE 的 advantage 沿序列逐 token 不同**（信用分配），**GRPO 的 advantage 在序列内是常数**（涂满有效位）。
- 切到 GRPO + `use_kl_loss=True` 后，`apply_kl_penalty` 被跳过，`token_level_rewards` 直接等于 score；KL 改由 `actor/pg_loss` 之外多出的 `actor/kl_loss` 项承担。可在日志里对比 `critic/kl_coeff` 是否还出现、`actor/kl_loss` 是否出现。
- 数值结果**待本地验证**（受 `masked_whiten` 的 Bessel 校正影响），但符号与上述定性结论应当一致。

这个任务把「score → reward（扣 KL）→ advantage（GAE 或 GRPO）」完整走了一遍，正是 `fit()` 里 `adv` 计时段做的工作。

## 6. 本讲小结

- **reward ≠ score**：`apply_kl_penalty` 在任务分上扣除 token 级 KL 罚，公式 `token_level_rewards = token_level_scores - beta * kld * response_mask`，只罚 response 有效 token。
- **response_mask 的切法**：`attention_mask[:, -response_length:]`，是所有「只在 response 上算」操作的统一入口。
- **KL 有四种估计**：`kl`（默认，无偏可为负）、`abs`、`mse`、`low_var_kl`（低方差，GRPO loss 端默认），由 `kl_penalty` 函数统一提供，reward 端与 loss 端复用。
- **`use_kl_loss=True` 跳过 KL penalty**：因为 KL 已经在 actor loss 里直接加了，避免重复计数；此时 `token_level_rewards = token_level_scores`。这是 GAE 与 GRPO 路线的分水岭。
- **`compute_advantage` 是分发器**：`gae` 用 critic 做 GAE 反向递推（每 token 不同优势），`grpo` 用 uid 组内归一化（序列内常数优势，无需 critic）。`returns = advantages + values` 必须用归一化前的 A。
- **KL 控制器**：`current_kl` 经 `masked_mean` 在序列与 batch 上平均后回传给 `kl_ctrl.update`，固定型为空操作，自适应型据此调 $\beta$（`u5-l4` 详讲）。

## 7. 下一步学习建议

本讲产出了 `advantages` 与 `returns`，接下来它们会被送进两处反传：

- 下一讲 **`u5-l2` PPO 策略损失与 clipping**：看 `advantages` 如何与 importance ratio、cliprange 组合成 `compute_policy_loss`，理解 `pg_loss / pg_clipfrac / ppo_kl` 三个监控量。
- **`u5-l3` GAE 优势与价值损失**：看 `returns` 如何监督 critic 的 `compute_value_loss`，以及 `vf_explained_var` 的含义。
- **`u5-l4` KL 控制器与 KL 估计变体**：深入 `AdaptiveKLController` 的比例误差调节公式，以及 `low_var_kl` 的推导。
- **`u5-l5` GRPO 算法实现**：从组内归一化一路看到 `use_kl_loss` 在 actor 端的完整配合，把本讲的 GRPO 分支讲透。

建议阅读顺序：先 `u5-l2`（消费 advantage）→ `u5-l3`（消费 returns）→ `u5-l4`（回到 KL 控制器）→ `u5-l5`（GRPO 收尾）。每篇都依赖本讲建立的「reward 与 advantage 从哪来」。
