# PPO 策略损失与 clipping

## 1. 本讲目标

上一讲（u5-l1）我们把「任务分数」加工成了 `token_level_rewards`，并计算出 `advantages` 与 `returns`。本讲专注回答一个问题：**拿到 advantage 之后，Actor（策略网络）的损失到底长什么样、梯度从哪里来？**

读完本讲你应该能够：

1. 说清 PPO 的 **importance ratio（重要性采样比）** 是怎么由 `old_log_prob` 与 `log_prob` 构造出来的，以及为什么要用它。
2. 看懂 `compute_policy_loss` 里 `pg_losses` 与 `pg_losses2` 的双侧裁剪（clip），并解释代码为什么写成 `torch.max` 而不是 `torch.min`。
3. 掌握 `masked_mean` 如何「只在有效 response token 上求平均」，以及 `entropy_from_logits` 如何给出熵。
4. 理解 `pg_loss`、`pg_clipfrac`、`ppo_kl` 这三个返回值分别作为**训练损失**和**监控指标**的不同角色，并能在 `update_policy` 里看到它们被如何组装、记录。

## 2. 前置知识

在进入源码前，先用直觉建立三个概念。

### 2.1 为什么需要 importance ratio（重要性采样比）

强化学习的策略梯度要求「用当前策略 \(\pi_\theta\) 采样的数据」来估计梯度。但 PPO 是**同策略（on-policy）的近似**：rollout（生成回答）用的是稍旧的策略 \(\pi_{\theta_{old}}\)，等到真正反传更新时，参数已经（在 mini-batch 内）被改动过，变成了 \(\pi_\theta\)。

同一个动作 \(a_t\)，在新旧两个策略下的概率不一样，直接用旧数据估计新策略的梯度会有偏差。**重要性采样比**就是用来纠偏的系数：

\[
r_t(\theta)=\frac{\pi_\theta(a_t\mid s_t)}{\pi_{\theta_{old}}(a_t\mid s_t)}
\]

工程上我们手上只有对数概率，所以用减法代替除法、再用指数还原：

\[
r_t(\theta)=\exp\bigl(\log\pi_\theta(a_t\mid s_t)-\log\pi_{\theta_{old}}(a_t\mid s_t)\bigr)
\]

\(r_t=1\) 表示新旧策略对这个 token 的看法没变；偏离 1 越多，说明策略已经「跑偏」，这时 PPO 会用 clipping 把它拉回来。

### 2.2 clipping（双侧裁剪）的直觉

PPO 不让 ratio 随意变大变小，而是把它限制在一个「信任区间」\([1-\epsilon,\,1+\epsilon]\) 内（代码里 \(\epsilon\) 就是 `cliprange`，默认 `0.2`）。

- 如果某个 token 的 advantage 为**正**（这是个好动作），我们想**增大**它的概率，也就是让 ratio 往大走。但最多只允许到 \(1+\epsilon\)，超出部分不给奖励——防止一步走太远、把策略搞坏。
- 如果 advantage 为**负**（这是个差动作），我们想**减小**它的概率，让 ratio 往小走，但最多只允许到 \(1-\epsilon\)。

这就是「裁剪」：**允许策略朝有利方向更新，但不允许更新幅度失控**。

### 2.3 为什么要「带掩码」的平均

response 是右填充（pad）的：真实回答后面挂着一串 pad token。这些 pad 位置不应该参与损失计算，否则模型会去优化一堆无意义的填充 token。`masked_mean` 的作用就是：**只在 `eos_mask`（实际是有效 response 掩码）为 1 的位置上求平均**。

> 命名提醒：`compute_policy_loss` 的参数叫 `eos_mask`，但在本讲的调用场景里，传入的其实是 `response_mask`（由 `attention_mask` 末尾切出），它就是「这一位是不是真实 token」的 0/1 掩码，并不是字面上的「EOS 之后置零」掩码。这个名字继承自上游 TRL，读源码时按「有效 token 掩码」理解即可。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [verl/trainer/ppo/core_algos.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py) | PPO 核心数学。本讲精读其中的 `compute_policy_loss`（构造 ratio、双侧裁剪、返回三个值）。 |
| [verl/utils/torch_functional.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/torch_functional.py) | 小工具集合。本讲用到 `masked_mean`（带掩码平均）与 `entropy_from_logits`（熵计算）。 |
| [verl/workers/actor/dp_actor.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py) | Actor 的前向与更新。`update_policy` 调用 `compute_policy_loss`，并把 `pg_loss` 与熵损失、KL 损失组合成最终 loss。 |
| [verl/trainer/config/ppo_trainer.yaml](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml) | 配置默认值：`clip_ratio`、`entropy_coeff`、`kl_loss_type` 等。 |

## 4. 核心概念与源码讲解

### 4.1 masked_mean：只在有效 token 上求平均

#### 4.1.1 概念说明

`masked_mean` 是本讲里出现频率最高的工具。策略损失、熵损失、KL 损失都要在「整个 batch 的所有 response token」上求平均，但 pad 位置必须排除。`masked_mean(values, mask)` 就是带权平均：用 mask 当权重，先求加权和，再除以 mask 的总和对长度做归一。

#### 4.1.2 核心流程

给定逐 token 的值 \(v_t\) 与 0/1 掩码 \(m_t\)：

\[
\text{masked\_mean}(v,m)=\frac{\sum_t v_t\,m_t}{\sum_t m_t}
\]

- 分子：只累加有效位置（\(m_t=1\)）的值。
- 分母：有效位置的总数，用来把「求和」折算回「平均」。
- 如果 `axis=None`（默认），就是对所有维度一起求和，返回一个标量——这正是损失函数想要的形状。

#### 4.1.3 源码精读

[verl/utils/torch_functional.py:L107-L109](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/torch_functional.py#L107-L109) —— 用一行实现带掩码平均：

```python
def masked_mean(values, mask, axis=None):
    """Compute mean of tensor with a masked values."""
    return (values * mask).sum(axis=axis) / mask.sum(axis=axis)
```

`values * mask` 把 pad 位置（mask=0）直接清零，`.sum()` 得到加权和；除以 `mask.sum()` 得到有效 token 上的平均。

同文件里还有两个相关工具，便于对比记忆：

- [masked_sum](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/torch_functional.py#L102-L104)：只求和、不除以长度。
- [masked_whiten](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/torch_functional.py#L130-L136)：带掩码的「减均值、除标准差」归一化，上一讲 GAE 优势就是用它白化的。

#### 4.1.4 代码实践

**目标**：直观确认 `masked_mean` 会忽略 pad 位置。

把下面这段当**示例代码**在本地跑（只需 torch）：

```python
# 示例代码
import torch
from verl.utils.torch_functional import masked_mean

values = torch.tensor([[2.0, 4.0, 100.0, 100.0]])   # 末两位是 pad
mask   = torch.tensor([[1.0, 1.0, 0.0,   0.0]])
print(masked_mean(values, mask))   # 期望 (2+4)/2 = 3.0，而不是 (2+4+100+100)/4
```

**预期结果**：打印 `tensor(3.)`。第三、四位 100 被忽略。如果改成全 1 掩码，结果才是 51.5。

#### 4.1.5 小练习与答案

**练习 1**：若 `mask` 全为 0，`masked_mean` 会返回什么？训练里为什么绝不能出现这种情况？

**参考答案**：分母 `mask.sum()=0`，会发生除以零，得到 `nan`。所以 batch 里必须保证至少有一个有效 response token；这也是 GAE 计算、reward 末位放置等环节都要保证回答非空的原因。

**练习 2**：`masked_mean(values, mask, axis=None)` 和 `masked_mean(values, mask, axis=1)` 在形状 (bs, response_length) 上分别返回什么？

**参考答案**：`axis=None` 对所有元素求平均，返回标量（形状为 ()），用作 loss；`axis=1` 沿序列维平均，返回形状 (bs,)，即「每条样本内部的平均」，常用于按样本统计指标。

---

### 4.2 entropy_from_logits：熵的计算（用于探索奖励）

#### 4.2.1 概念说明

熵 \(H\) 衡量策略在每个位置上的「不确定度」。熵高表示模型还在多种 token 之间犹豫（探索性强），熵低表示模型非常确定（容易陷入重复、失去多样性）。PPO 通常在损失里减去一项「熵奖励」\(\beta_e\cdot H\)，鼓励模型保持一定探索，避免过早坍缩。这项奖励就由 `entropy_from_logits` 逐 token 算出来。

#### 4.2.2 核心流程

对每个位置，模型输出 logits \(l_i\)（词表维度的未归一化分数）。先归一化得到概率 \(p_i=\mathrm{softmax}(l_i)\)，分类熵为：

\[
H(p)=-\sum_i p_i\log p_i
\]

直接按定义算要算两次 log/softmax。工程上有个等价且更稳的写法。令 \(Z=\sum_i e^{l_i}\)，则 \(\log p_i = l_i-\log Z\)，代入展开：

\[
H(p)=-\sum_i p_i(l_i-\log Z)=\log Z\cdot\underbrace{\sum_i p_i}_{=1}-\sum_i p_i l_i=\log Z-\sum_i p_i l_i
\]

其中 \(\log Z=\mathrm{logsumexp}(l)\)、\(\sum_i p_i l_i=\sum_i \mathrm{softmax}(l)_i\cdot l_i\)。这就是源码用的形式。

#### 4.2.3 源码精读

[verl/utils/torch_functional.py:L95-L99](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/torch_functional.py#L95-L99) —— 与上面推导一一对应：

```python
def entropy_from_logits(logits: torch.Tensor):
    """Calculate entropy from logits."""
    pd = torch.nn.functional.softmax(logits, dim=-1)
    entropy = torch.logsumexp(logits, dim=-1) - torch.sum(pd * logits, dim=-1)
    return entropy
```

- `pd`：每个位置在词表上的概率分布。
- `torch.logsumexp(logits, dim=-1)`：\(\log Z\)，数值稳定的 \(\log\sum e^{l_i}\)。
- `torch.sum(pd * logits, dim=-1)`：\(\sum_i p_i l_i\)。
- 两者相减得到每个位置的熵，形状 `(bs, response_length)`。

它在 Actor 里被这样调用（注意 rmpad 路径下被 `torch.compile` 包了一层以加速）：

- 非去填充路径：[dp_actor.py:L139](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L139) 直接 `entropy = verl_F.entropy_from_logits(logits)`。
- 去填充路径：`__init__` 里 `self.compute_entropy_from_logits = torch.compile(verl_F.entropy_from_logits, dynamic=True)`（[dp_actor.py:L56](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L56)），在 [dp_actor.py:L103](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L103) 调用。

> 小贴士：core_algos 里其实还有一个 [compute_entropy_loss](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L197-L213)，它内部也是 `entropy_from_logits` + `masked_mean`。但 Actor 的 `update_policy` 因为前向时已经顺手算好了逐 token 的 `entropy`，所以没有调用它，而是直接 `masked_mean(entropy, response_mask)`（见 4.4）。两者公式一致。

#### 4.2.4 代码实践

**目标**：验证熵公式的两种写法等价。

```python
# 示例代码
import torch
import torch.nn.functional as F
from verl.utils.torch_functional import entropy_from_logits

torch.manual_seed(0)
logits = torch.randn(2, 5)                 # 2 个位置，词表大小 5
# 写法 A：直接按定义 H = -sum p log p
logp = F.log_softmax(logits, dim=-1)
p = logp.exp()
entropy_def = -(p * logp).sum(dim=-1)
# 写法 B：源码写法
entropy_code = entropy_from_logits(logits)
print(torch.allclose(entropy_def, entropy_code, atol=1e-5))
```

**预期结果**：打印 `True`。

#### 4.2.5 小练习与答案

**练习 1**：当某个位置的 logits 近似 one-hot（一个分量极大、其余极小）时，`entropy_from_logits` 的输出接近多少？

**参考答案**：接近 0。因为概率几乎全部集中在一个 token 上，分布近乎确定，熵最小。反之 logits 各分量相近时熵最大。

**练习 2**：为什么损失里是「减去」熵而不是「加上」熵？

**参考答案**：训练是在**最小化** loss。我们希望**鼓励探索**，即希望熵**变大**，所以把 \(-\beta_e H\) 放进 loss——最小化 loss 等价于最大化 \(H\)，于是梯度会把策略往更高熵的方向推。\(\beta_e\) 就是配置里的 `entropy_coeff`（默认 `0.001`）。

---

### 4.3 compute_policy_loss：importance ratio 与双侧裁剪

#### 4.3.1 概念说明

这是本讲的主角。`compute_policy_loss` 把「上一讲算好的 advantage」和「新旧策略的对数概率」合在一起，输出 PPO 的策略损失，外加两个监控量。

原始 PPO 论文（[arXiv:1707.06347](https://arxiv.org/abs/1707.06347)）里**要最大化**的 clipped surrogate 目标是：

\[
L^{CLIP}(\theta)=\hat{E}_t\Bigl[\min\bigl(r_t\hat{A}_t,\;\mathrm{clip}(r_t,1-\epsilon,1+\epsilon)\hat{A}_t\bigr)\Bigr]
\]

深度学习框架做的是**最小化** loss，所以要取负号：\(\text{pg\_loss}=-L^{CLIP}\)。利用恒等式 \(\min(a,b)=-\max(-a,-b)\)，取负后的形式变成：

\[
\text{pg\_loss}=\hat{E}_t\Bigl[\max\bigl(-r_t\hat{A}_t,\;-\mathrm{clip}(r_t,1-\epsilon,1+\epsilon)\hat{A}_t\bigr)\Bigr]
\]

**这正是代码用 `torch.max` 而非 `torch.min` 的原因**——「最大化目标里的 min」等价于「最小化损失里的 max」。这是读懂这段源码最关键的一步。

#### 4.3.2 核心流程

`compute_policy_loss(old_log_prob, log_prob, advantages, eos_mask, cliprange)` 的执行步骤：

1. **算对数比**：`negative_approx_kl = log_prob - old_log_prob`，即 \(\log\pi_\theta-\log\pi_{old}\)。
2. **还原 ratio**：`ratio = exp(negative_approx_kl)`，即重要性采样比 \(r_t\)。
3. **两支损失**：
   - `pg_losses  = -advantages * ratio`（不裁剪支）
   - `pg_losses2 = -advantages * clamp(ratio, 1-cliprange, 1+cliprange)`（裁剪支）
4. **取 max 后做 masked_mean**：`pg_loss = masked_mean(torch.max(pg_losses, pg_losses2), eos_mask)`。
5. **三个返回值**：
   - `pg_loss`：策略损失（要反传）。
   - `pg_clipfrac = masked_mean(pg_losses2 > pg_losses, eos_mask)`：被裁剪的 token 占比（监控）。
   - `ppo_kl = masked_mean(old_log_prob - log_prob, eos_mask)`：新旧策略的近似 KL（监控）。

裁剪到底在什么时候「起作用」（即 `pg_losses2` 被选中）？把 \(\hat A\) 的正负和 ratio 是否越界列成表就清楚了：

| advantage | ratio 范围 | 是否裁剪 | 含义 |
| --- | --- | --- | --- |
| 正 | \(r\in[1-\epsilon,1+\epsilon]\) | 否 | 好动作、且更新幅度合理，正常鼓励 |
| 正 | \(r>1+\epsilon\) | **是** | 好动作，但概率已经涨太多了，封顶到 \(1+\epsilon\) |
| 正 | \(r<1-\epsilon\) | 否 | 好动作却在掉概率，这种「错误方向」裁剪不拦（让损失如实反映） |
| 负 | \(r\in[1-\epsilon,1+\epsilon]\) | 否 | 差动作、更新幅度合理，正常抑制 |
| 负 | \(r<1-\epsilon\) | **是** | 差动作，概率已经跌太多了，封底到 \(1-\epsilon\) |
| 负 | \(r>1+\epsilon\) | 否 | 差动作却在涨概率，裁剪不拦 |

一句话：**裁剪只限制「有利方向上走得太远」，不限制「不利方向」**，从而既防止策略剧烈漂移，又不掩盖真实的劣势信号。

#### 4.3.3 源码精读

[verl/trainer/ppo/core_algos.py:L163-L194](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L163-L194) —— 完整函数：

```python
def compute_policy_loss(old_log_prob, log_prob, advantages, eos_mask, cliprange):
    ...
    negative_approx_kl = log_prob - old_log_prob
    ratio = torch.exp(negative_approx_kl)
    ppo_kl = verl_F.masked_mean(-negative_approx_kl, eos_mask)

    pg_losses = -advantages * ratio
    pg_losses2 = -advantages * torch.clamp(ratio, 1.0 - cliprange, 1.0 + cliprange)

    pg_loss = verl_F.masked_mean(torch.max(pg_losses, pg_losses2), eos_mask)
    pg_clipfrac = verl_F.masked_mean(torch.gt(pg_losses2, pg_losses).float(), eos_mask)
    return pg_loss, pg_clipfrac, ppo_kl
```

逐行对照：

- [L185-L187](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L185-L187)：构造 ratio 与 `ppo_kl`。注意变量名 `negative_approx_kl = log_prob - old_log_prob`，取负后 `old_log_prob - log_prob` 就是 Schulman 的 KL 近似估计 \( \hat{KL}\approx \mathbb{E}[\log\pi_{old}-\log\pi_\theta]\)（无偏但可能为负）。
- [L189-L190](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L189-L190)：两条损失支。`torch.clamp(ratio, 1-cliprange, 1+cliprange)` 把 ratio 双侧夹住。
- [L192](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L192)：取 `max`（对应「最大化目标里的 min」），再 `masked_mean` 得到标量损失。
- [L193](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L193)：`pg_clipfrac` 用 `torch.gt(pg_losses2, pg_losses)` 判断「裁剪支是否被选中」，求平均得到被裁剪比例。

配置侧默认值见 [ppo_trainer.yaml:L28-L29](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L28-L29)：`clip_ratio: 0.2`、`entropy_coeff: 0.001`。

#### 4.3.4 代码实践

**目标**：用一组精心设计的小张量，手算 ratio、两条损失支、max 选择，再和 `compute_policy_loss` 的输出对照。

设计一个 1×4 的样本，**四个 token 各命中一种典型情形**（adv 依次为 `+1, +1, -1, -1`）：

- token 0：adv=+1，log_prob=0.1 → ratio=exp(0.1)≈1.105（落在区间内，不裁剪）
- token 1：adv=+1，log_prob=0.5 → ratio=exp(0.5)≈1.649（>1.2，**正 adv 触发裁剪**）
- token 2：adv=-1，log_prob=0.05 → ratio=exp(0.05)≈1.051（落在区间内，不裁剪）
- token 3：adv=-1，log_prob=-0.5 → ratio=exp(-0.5)≈0.607（<0.8，**负 adv 触发裁剪**）

设 `old_log_prob` 全 0、`eos_mask` 全 1、`cliprange=0.2`。把下面作为**示例代码**运行：

```python
# 示例代码
import torch
from verl.trainer.ppo.core_algos import compute_policy_loss

old_log_prob = torch.tensor([[0.0, 0.0,   0.0,  0.0]])
log_prob     = torch.tensor([[0.1, 0.5,   0.05,-0.5]])
advantages   = torch.tensor([[1.0, 1.0,  -1.0, -1.0]])
eos_mask     = torch.tensor([[1.0, 1.0,   1.0,  1.0]])

# 手算 ratio（exp(log_prob - old_log_prob) = exp(log_prob)）
ratio = torch.exp(log_prob - old_log_prob)
print("ratio   =", ratio.tolist())
# 手算两支
pg_losses  = -advantages * ratio
pg_losses2 = -advantages * torch.clamp(ratio, 0.8, 1.2)
print("branch1 =", pg_losses.tolist())
print("branch2 =", pg_losses2.tolist())

# 对照官方实现
pg_loss, pg_clipfrac, ppo_kl = compute_policy_loss(
    old_log_prob, log_prob, advantages, eos_mask, cliprange=0.2)
print("pg_loss    =", pg_loss.item())
print("pg_clipfrac=", pg_clipfrac.item())
print("ppo_kl     =", ppo_kl.item())
```

**手算预期**（精确浮点以本地运行为准）：

- token 0：branch1=branch2≈-1.105，max≈-1.105，未裁剪（clipfrac 贡献 0）
- token 1：branch1≈-1.649，branch2=-1.2，max=-1.2，**裁剪**（clipfrac 贡献 1）
- token 2：branch1=branch2≈1.051，max≈1.051，未裁剪（贡献 0）
- token 3：branch1≈0.607，branch2=0.8，max=0.8，**裁剪**（贡献 1）

汇总：

- `pg_loss = (-1.105 - 1.2 + 1.051 + 0.8)/4 ≈ -0.1135`
- `pg_clipfrac = (0+1+0+1)/4 = 0.5`
- `ppo_kl = mean(0-0.1, 0-0.5, 0-0.05, 0-(-0.5)) = -0.15/4 = -0.0375`

**观察重点**：token 1 与 token 3 正好对应上表里「触发裁剪」的两行；`pg_clipfrac=0.5` 反映了四个 token 里有两个被裁。`ppo_kl` 为负表示新策略整体比旧策略更「自信」（log_prob 更高）。

#### 4.3.5 小练习与答案

**练习 1**：把 `cliprange` 从 0.2 调到 0.05，重跑上面的例子，`pg_clipfrac` 和 `pg_loss` 会怎么变？为什么？

**参考答案**：区间收窄到 \([0.95,1.05]\)，原本落在区间内的 token 0（ratio≈1.105）和 token 2（ratio≈1.051）现在也会越界、可能触发裁剪，`pg_clipfrac` 变大；`pg_loss` 因为更多位置被封顶/封底而改变。这说明 `cliprange` 越小，策略更新越保守、越「贴近」旧策略。

**练习 2**：为什么 `ppo_kl` 是「监控指标」而不是直接进 loss 的项？

**参考答案**：PPO 的 KL 约束已经通过 **reward 端的 KL penalty**（上一讲 `apply_kl_penalty`，对应 GAE 路线）或 **loss 端的 kl_loss**（GRPO 路线，见 4.4）施加。这里的 `ppo_kl` 只是观测「这一步 mini-batch 之后，新旧策略实际差了多少」，用来判断 ratio 是否还可信、是否该提前停止或调 `cliprange`。它返回给上层记录为 `actor/ppo_kl` 供人看。

---

### 4.4 update_policy：把 pg_loss、entropy、kl 组装成最终 loss

#### 4.4.1 概念说明

`compute_policy_loss` 只给出「策略损失」这一项。Actor 真正反传的 `loss` 是三项之和：策略损失 + 熵奖励 + （可选）KL 损失。`update_policy` 就是这个组装车间，同时负责 mini/micro batch 的梯度累积。这一节把前三节的零件接上线，并指出三个返回指标最终被记录到哪里。

#### 4.4.2 核心流程

`DataParallelPPOActor.update_policy`（[dp_actor.py:L203-L286](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L203-L286)）的骨架：

1. **梯度累积系数**：`gradient_accumulation = ppo_mini_batch_size // ppo_micro_batch_size`（[L208](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L208)）。
2. **拆 mini-batch → micro-batch**：mini-batch 做一次优化器更新，micro-batch 是为了塞进显存而做的前向分片。
3. 对每个 micro-batch：前向拿到 `(entropy, log_prob)`，调用 `compute_policy_loss` 得到 `(pg_loss, pg_clipfrac, ppo_kl)`。
4. 组装：`policy_loss = pg_loss - entropy_loss * entropy_coeff`；若 `use_kl_loss`（GRPO），再 `policy_loss = policy_loss - kl_loss * kl_loss_coef`。
5. `loss = policy_loss / gradient_accumulation` 后 `loss.backward()`，把若干 micro-batch 的梯度累加起来再统一 `optimizer.step()`。

最终 loss（GRPO 时三项齐全）：

\[
\text{loss}=\frac{1}{G}\Bigl(\underbrace{\text{pg\_loss}}_{\text{策略损失}}-\underbrace{\beta_e\,H}_{\text{熵奖励}}-\underbrace{\beta_{kl}\,\widehat{KL}_{\text{low\_var}}}_{\text{KL 损失}}\Bigr)
\]

其中 \(G\) 是 `gradient_accumulation`。除以 \(G\) 是为了保证「多个 micro-batch 累加后的梯度」等价于「一次性算整个 mini-batch 的平均梯度」。

#### 4.4.3 源码精读

调用与组装的关键几行（[dp_actor.py:L248-L272](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L248-L272)）：

```python
pg_loss, pg_clipfrac, ppo_kl = core_algos.compute_policy_loss(
    old_log_prob=old_log_prob, log_prob=log_prob,
    advantages=advantages, eos_mask=response_mask, cliprange=clip_ratio)
# compute entropy loss from entropy
entropy_loss = verl_F.masked_mean(entropy, response_mask)

# compute policy loss
policy_loss = pg_loss - entropy_loss * entropy_coeff

if self.config.use_kl_loss:
    ref_log_prob = data['ref_log_prob']
    kld = core_algos.kl_penalty(logprob=log_prob, ref_logprob=ref_log_prob,
                                kl_penalty=self.config.kl_loss_type)
    kl_loss = masked_mean(kld, response_mask)
    policy_loss = policy_loss - kl_loss * self.config.kl_loss_coef
    ...

loss = policy_loss / self.gradient_accumulation
loss.backward()
```

要点：

- `eos_mask` 实参传的是 `response_mask`（[dp_actor.py:L238](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L238) 由 `attention_mask[:, -response_length:]` 切出），再次印证 2.3 节的命名说明。
- `clip_ratio`、`entropy_coeff` 来自配置（默认 `0.2`、`0.001`）。
- **GRPO 与 GAE 的分水岭**：`use_kl_loss=True` 时（GRPO 默认走这条，`kl_loss_type=low_var_kl`），KL 直接进 loss；而上一讲讲过，此时 `apply_kl_penalty` 会被跳过、reward 直接等于 score，避免 KL 被算两遍。
- 三个监控量被写进 metrics（[dp_actor.py:L274-L279](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L274-L279)）：`actor/entropy_loss`、`actor/pg_loss`、`actor/pg_clipfrac`、`actor/ppo_kl`，最终经 Tracking 写到 wandb/console。

#### 4.4.4 代码实践

**目标**：理解梯度累积缩放，而无需跑训练。

阅读 [dp_actor.py:L207-L272](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L207-L272)，回答两个问题（源码阅读型实践）：

1. 设 `ppo_mini_batch_size=32`、`ppo_micro_batch_size=8`，则 `gradient_accumulation` 是多少？为什么 `loss` 要除以它？
2. 假如把 `entropy_coeff` 从 `0.001` 调到 `0.1`，训练曲线的 `actor/entropy_loss` 和生成多样性会怎么变？

**参考答案**：

1. `gradient_accumulation = 32/8 = 4`。一个 mini-batch 被切成 4 个 micro-batch 依次前向反传，若不除以 4，累加的梯度会是「整 mini-batch 梯度的 4 倍」（因为 `masked_mean` 本身已对 token 数做过平均，但 4 次反传会累加 4 份）。除以 4 让最终累加梯度等价于一次性处理整个 mini-batch 的平均梯度。
2. `entropy_coeff` 增大，熵项权重变大，优化器会更努力把策略推向高熵，`actor/entropy_loss` 会上升、生成文本更多样、更难出现重复；但过大也会让模型「为探索而探索」，拖慢奖励收敛。

#### 4.4.5 小练习与答案

**练习 1**：GRPO 路线下，`compute_policy_loss` 之外又加了 `kl_loss`，但 `apply_kl_penalty` 那一步被跳过。如果两者都保留会发生什么？

**参考答案**：KL 会被「双计」——reward 端扣一次、loss 端又扣一次，等价于把 KL 约束强度翻倍甚至更复杂，策略会被过度拽回参考策略，训练变慢甚至无法学习。所以代码用 `use_kl_loss` 做互斥开关（详见上一讲 u5-l1）。

**练习 2**：`update_policy` 里每个 mini-batch 做一次 `optimizer.step()`，但没看到 PPO 常说的「多个 epoch 复用同一批数据」。这是否意味着本项目每个 batch 只更新一次策略？

**参考答案**：是的。在这份 `update_policy` 实现里，`dataloader = batch.split(ppo_mini_batch_size)` 只是把整批切成若干 mini-batch 各做一次更新，没有对同一批数据多次遍历的 epoch 循环。这是工程上的简化（也利于 on-policy 的正确性），与传统 PPO「K 个 epoch」不同，读源码时注意不要假设有多轮复用。

## 5. 综合实践

把本讲四块内容串起来，完成一次「**纸上 PPO 单步**」：

给定一个 2 条样本、每条 3 个 response token 的小批次（`old_log_prob`、`log_prob`、`advantages`、`eos_mask` 如下），`cliprange=0.2`、`entropy_coeff=0.001`、`use_kl_loss=False`：

```
old_log_prob = [[-1.0, -1.0, -1.0], [-1.0, -1.0, -1.0]]
log_prob     = [[-0.5, -0.9, -2.0], [-1.5, -0.8, -0.6]]
advantages   = [[ 1.0,  1.0,  1.0], [-1.0, -1.0, -1.0]]
eos_mask     = [[ 1.0,  1.0,  1.0], [ 1.0,  1.0,  1.0]]
```

请：

1. 逐 token 算出 `ratio`，判断哪些位置触发裁剪。
2. 算出 `pg_loss`、`pg_clipfrac`、`ppo_kl`，并与 `compute_policy_loss` 的真实输出对照。
3. 用 [entropy_from_logits](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/torch_functional.py#L95-L99) 另行喂一组 logits（自造即可），算出 `entropy_loss`，再按 `policy_loss = pg_loss - entropy_loss*entropy_coeff` 得到最终 loss。

提示：可把 4.3.4 的脚本扩展为 2×3 输入直接运行验证；若无法运行，手算结果标注「待本地验证」亦可。重点不在数值精度，而在于你能解释**每个位置为什么被裁或不裁**、**三个返回值各自的物理含义**。

## 6. 本讲小结

- PPO 用 **importance ratio** \(r_t=\exp(\log\pi_\theta-\log\pi_{old})\) 来纠正「用旧策略数据估计新策略梯度」的偏差；代码里就是 `ratio = torch.exp(log_prob - old_log_prob)`。
- **双侧裁剪** 把 ratio 限制在 \([1-\epsilon,1+\epsilon]\)（默认 \(\epsilon=0.2\)）；由于框架做的是最小化损失，源码写成 `torch.max(pg_losses, pg_losses2)`，对应论文「最大化目标里的 min」。
- `masked_mean` 用 `eos_mask`（实为有效 response 掩码）排除 pad 位置，所有损失项都建立在它之上。
- `entropy_from_logits` 用 \(\log Z-\sum_i p_i l_i\) 给出逐 token 熵，作为探索奖励的来源。
- `compute_policy_loss` 返回 **损失** `pg_loss` 和 **两个监控量** `pg_clipfrac`（裁剪比例）、`ppo_kl`（新旧策略近似 KL）。
- `update_policy` 把 `pg_loss - entropy_loss*entropy_coeff (- kl_loss*kl_loss_coef)` 除以 `gradient_accumulation` 后反传；GRPO 走 `use_kl_loss=True` 把 KL 放进 loss，与 reward 端的 KL penalty 互斥。

## 7. 下一步学习建议

- 本讲的 `pg_loss` 只用到 `advantages`，而 advantage 的 GAE 反向递推与 value loss 在下一讲 **u5-l3 GAE 优势与价值损失** 中详细展开，建议顺次阅读 `compute_gae_advantage_return` 与 `compute_value_loss`，把「actor 损失」与「critic 损失」配成一对。
- 若想看清 KL 的多种估计（`kl/abs/mse/low_var_kl`）与自适应系数调节，可继续读 **u5-l4 KL 控制器与 KL 估计变体**，理解 `AdaptiveKLController` 如何按比例误差调 `beta`。
- 想把策略损失放回训练循环看全景，可回顾 **u4-l3 fit() 训练主循环全流程** 中 `update_actor` 这一步的计时与 RPC 调用。
