# GRPO 改进一：Clip Ratio 裁剪

## 1. 本讲目标

上一讲（u8-l1）我们学会了用 CSV 日志、优势统计和熵来「观测」GRPO 训练，并看到真实日志中熵从 1.33 崩到 0.2、又飙到 10+ 的失稳过程。本讲动手解决「更新过猛」问题的第一件工具：**PPO 式裁剪（clipping）**。

读完本讲，你应该能够：

1. 解释**新旧策略概率比 ratio** 的构造：为什么要在 rollout 时冻结一份旧模型、记录旧 logprob，训练时再带梯度重算新 logprob；
2. 说明 `torch.clamp` + `torch.minimum` 组合如何按**优势的正负分别约束上下界**——为什么好结果「封顶不封底」、坏结果「封底不封顶」；
3. 理解 **eval/训练间 logp 漂移（ratio ≠ 1）** 问题：`inner_epochs=2` 之后，rollout 数据对当前权重来说变成了 off-policy，ratio 正是对这个漂移的度量与修正；
4. 对照第 6 章/7_3 的基线实现，逐项说出 `7_4_plus_clip_ratio.py` 在 `compute_grpo_loss` 上的全部改动点。

## 2. 前置知识

本讲建立在 u7-l3（GRPO 损失函数）与 u8-l1（训练跟踪与熵）之上，先把三个旧结论摆出来，再补一个新概念。

**（1）基线损失长什么样。** 第 6 章的 `compute_grpo_loss` 最终一行是：

\[ \mathcal{L}_{\text{pg}} = -\frac{1}{G}\sum_{i=1}^{G} A_i \cdot \log p_\theta(o_i \mid q) \]

即「优势 × 序列 logprob 取负平均」（见 [reasoning_from_scratch/ch06.py:L176-L178](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L176-L178)）。它隐含一个假设：**采样 rollout 时的权重与计算 logprob 时的权重是同一份**——严格 on-policy。这在「采样一次、更新一次」时成立。

**（2）优势是 detach 的学习信号。** \( A_i = (r_i - \mu) / (\sigma + 10^{-4}) \) 组内标准化后 `detach()`，只当常数系数用，不回传梯度。

**（3）更新过猛会失稳。** u8-l1 的真实日志显示：奖励先升、熵骤降（策略过度自信），随后熵飙升逼近均匀分布、奖励归零。梯度裁剪 `clip_grad_norm_` 限制的是**梯度范数**，而本讲的裁剪限制的是**策略移动幅度本身**——两者互补。

**（4）新概念：on-policy → off-policy 漂移。** 一旦同一批 rollout 被用于**多次**参数更新（本脚本的 `inner_epochs=2` 就是如此），第二次更新时权重已经变了：数据仍是旧策略采的，模型已是新策略——这批数据对新权重而言是 off-policy 的。直接沿用 on-policy 的策略梯度估计会有偏且方差失控。标准解法是**重要性采样比**：

\[ r_i(\theta) = \frac{\pi_\theta(o_i \mid q)}{\pi_{\theta_{\text{old}}}(o_i \mid q)} \]

它度量「同一条回答，新策略给它的概率是旧策略的几倍」。\( r_i = 1 \) 表示没有漂移；偏离 1 越远，新旧策略差距越大。

> 术语提示：**信任区间（trust region）**——PPO 一族方法的思想是：只在旧策略附近的一个小邻域内信任「策略梯度」这个近似，越界就停止奖励继续移动。裁剪 ratio 就是在损失函数层面实现这个邻域。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [ch07/03_rlvr_grpo_scripts_advanced/7_4_plus_clip_ratio.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_4_plus_clip_ratio.py#L1-L582) | 本讲主角。独立训练脚本：在 7_3（跟踪指标版）基础上加入旧模型冻结、ratio 裁剪损失、inner_epochs 与 policy_ratio 监控 |
| [reasoning_from_scratch/ch07.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch07.py#L1-L156) | 库模块：`sequence_logprob_and_entropy`（旧/新 logprob 的计算引擎）、`compute_advantage_stats`、绘图工具。注意裁剪逻辑**不在**库里，只在独立脚本中 |
| [reasoning_from_scratch/ch06.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L120-L187) | 第 6 章基线 `compute_grpo_loss`，用于对照改动点 |
| [ch07/02_logs/7_4_plus_clip_ratio_metrics.csv](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/02_logs/7_4_plus_clip_ratio_metrics.csv#L1-L6) | 作者发布的 500 步真实训练日志，含新增的 `policy_ratio` 列 |
| [ch07/03_rlvr_grpo_scripts_advanced/README.md](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/README.md#L12-L15) | 脚本定位说明：7.4 节「用裁剪的概率比稳定**序列级** GRPO」 |

一个容易混淆的点：`7_4_plus_clip_ratio.py` 顶部 import 的是 ch02/ch03/ch04/ch06 与 qwen3（见 [7_4_plus_clip_ratio.py:L12-L27](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_4_plus_clip_ratio.py#L12-L27)），**并不 import ch07**。`sequence_logprob_and_entropy` 在脚本里本地复制了一份（L82-L103），与库版 [ch07.py:L118-L140](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch07.py#L118-L140) 逻辑完全一致。这是本书「独立脚本自包含、库供 notebook 使用」的一贯组织方式。

## 4. 核心概念与源码讲解

本讲的三个最小模块：**ratio 计算** → **clamp 裁剪** → **compute_grpo_loss 改动点与 policy_ratio 监控**。

### 4.1 模块一：ratio 计算——新旧策略分离

#### 4.1.1 概念说明

要度量「策略移动了多远」，前提是有一个**不动参照物**。基线实现里采样和求 logprob 用的是同一个 `model`，根本没有「旧策略」可言。7_4 的做法是：每一步训练开始时，把当前模型完整复制一份、冻结，作为旧策略 \(\pi_{\theta_{\text{old}}}\)；rollout 从旧策略采，旧 logprob 在旧策略上算（无梯度），新 logprob 在正在被更新的模型上算（带梯度）。两者相减再取指数就是 ratio：

\[ r_i(\theta) = \exp\big(\log p_\theta(o_i \mid q) - \log p_{\theta_{\text{old}}}(o_i \mid q)\big) \]

注意一个关键细节：本实现是**序列级** ratio。`sequence_logprob_and_entropy` 返回的是答案区逐 token logprob 的**求和**（[7_4_plus_clip_ratio.py:L89-L91](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_4_plus_clip_ratio.py#L89-L91)），所以：

\[ r_i(\theta) = \exp\Big(\sum_t \big[\log \pi_\theta(o_{i,t}) - \log \pi_{\theta_{\text{old}}}(o_{i,t})\big]\Big) = \prod_{t} \frac{\pi_\theta(o_{i,t} \mid q, o_{i,<t})}{\pi_{\theta_{\text{old}}}(o_{i,t} \mid q, o_{i,<t})} \]

即**逐 token 概率比的连乘**。一条几百 token 的回答，哪怕每个 token 的概率只变动百分之几，连乘之后序列级 ratio 也会显著偏离 1——这正是 README 把本脚本描述为「sequence-level … clipped policy ratios」的原因，也是后面 ε 取得很大的伏笔（对比：原始 PPO/GRPO 论文是**逐 token** 裁剪，每 token 一个 ratio）。

#### 4.1.2 核心流程

```
每个训练 step：
  old_model = deepcopy(model)        # 冻结参照物
  old_model.eval(); requires_grad=False
  重复 inner_epochs 次（默认 2）：
    compute_grpo_loss(model, old_model, ...)：
      ① model.eval()
      ② 循环 num_rollouts 次：
           rollout 从 old_model 采样          ← 采样策略
           old_logp = f(old_model, rollout)    ← no_grad，冻结
           reward = reward_rlvr(...)
      ③ 恢复 model.train()
      ④ 优势 = 组内标准化(rewards)，detach
      ⑤ new_logp = f(model, rollout)           ← 带梯度，当前权重
      ⑥ log_ratio = new_logp − old_logp
         ratio = exp(log_ratio)
      backward + optimizer.step()               ← 更新的是 model
```

第一个 inner epoch 里 `model` 与 `old_model` 权重相同（刚复制的），所以 ratio ≈ 1；第一次 `optimizer.step()` 之后，第二个 inner epoch 的 ratio 就开始偏离 1——**ratio 是策略漂移的直接读数**。

#### 4.1.3 源码精读

**训练循环：每步冻结一份旧模型。**

[7_4_plus_clip_ratio.py:L330-L334](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_4_plus_clip_ratio.py#L330-L334)：`copy.deepcopy(model)` 后立即 `eval()` 并把所有参数 `requires_grad=False`——旧策略既不更新也不产生梯度。

**损失函数：新增 `old_model` 参数与回退分支。**

[7_4_plus_clip_ratio.py:L116-L127](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_4_plus_clip_ratio.py#L116-L127) 签名多了 `old_model`、`clip_eps`、`skip_zero_adv`；[L135-L136](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_4_plus_clip_ratio.py#L135-L136) 中 `old_model is None` 时回退为用 `model` 自身——保留旧行为的兼容开关。

**rollout 一律从旧模型采样，旧 logprob 无梯度记录。**

[7_4_plus_clip_ratio.py:L138-L151](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_4_plus_clip_ratio.py#L138-L151)：`sample_response(model=old_model, ...)` 采样；随后在 `torch.no_grad()` 下用 `sequence_logprob_and_entropy(old_model, ...)` 记录旧 logprob。源码注释写明意图：「Treat the rollout policy as the “old policy” for this update (detach)」。注意第二个 inner epoch 仍从**同一份冻结的** `old_model` 重新采样——数据相对当前权重是 off-policy 的，这正是 ratio 要修正的对象。

**当前权重下带梯度重算新 logprob。**

[7_4_plus_clip_ratio.py:L193-L198](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_4_plus_clip_ratio.py#L193-L198)：对每条 rollout 用 `model`（此时已恢复 train 模式）再算一遍 logprob，这次有 `grad_fn`。teacher-forcing 前向 + `gather` 错位抽取的细节与 u7-l3 相同，此处不赘。

**log 域相减、指数还原成概率比。**

[7_4_plus_clip_ratio.py:L200-L203](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_4_plus_clip_ratio.py#L200-L203)：

```python
old_logps = torch.stack(roll_old_logps).detach()
log_ratio = new_logps - old_logps
ratio = torch.exp(log_ratio)
```

在 log 域做减法、再一次性 `exp`，等价于概率域的除法（连乘），且数值上远比直接连乘稳定——这与 u6-l1「化乘为加」的动机一脉相承。

#### 4.1.4 代码实践

**实践目标**：建立 ratio 的数值直觉——logprob 差值如何映射为概率比。

**操作步骤**（以下为示例代码，只需安装 torch，无需下载模型）：

```python
import torch

old_logp = torch.tensor([-5.0, -5.0, -5.0, -5.0])
new_logp = torch.tensor([-4.9, -4.5, -3.0, -7.0])   # 差值 +0.1 / +0.5 / +2.0 / -2.0

log_ratio = new_logp - old_logp
ratio = torch.exp(log_ratio)
print(log_ratio)   # tensor([ 0.1000,  0.5000,  2.0000, -2.0000])
print(ratio)       # tensor([1.1052, 1.6487, 7.3891, 0.1353])
```

**需要观察的现象**：logprob 只差 0.5，概率比就放大到 1.65 倍；差 2.0 就是 7.39 倍。回忆 4.1.1 的序列级连乘——一条 400 token 的回答平均每 token 偏 0.005，序列级 log_ratio 就是 2.0、ratio 7.4。

**预期结果**：如注释所示（手算值：\(e^{0.1}\approx1.105\)、\(e^{0.5}\approx1.649\)、\(e^{2}\approx7.389\)、\(e^{-2}\approx0.135\)）。具体打印位数待本地验证。

**附加观察（可选，需加载模型）**：在 7_4 脚本里把 `inner_epochs` 临时设为 1 跑两步，观察 CSV 的 `policy_ratio` 是否贴近 1.0（权重未更新前新旧 logprob 应几乎一致；本架构无 dropout，train/eval 数值相同）。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么旧 logprob 必须在 `torch.no_grad()` 下计算，而新 logprob 必须带梯度？

**答案**：旧策略是冻结的参照物，只是 ratio 的分母常数——no_grad 确保它不进入计算图、不产生梯度（`detach()` 是双保险，见 L200）。新 logprob 是唯一与参数 θ 相连的量，策略梯度和裁剪全都通过它反传；若它也无梯度，`backward()` 将无事可做。

**练习 2**：某 rollout 的答案区有 500 个 token，新旧策略每个 token 的 logprob 平均只差 +0.004。序列级 ratio 是多少？

**答案**：log_ratio = 500 × 0.004 = 2.0，ratio = \(e^{2.0} \approx 7.39\)。这就是序列级 ratio「天然漂得远」的量化感受。

**练习 3**：脚本为什么每个 inner epoch 都从 `old_model` 重新采样，而不是复用第一批 rollout？

**答案**：两者的 off-policy 修正逻辑其实都成立（ratio 本就是为此设计的）。重新采样让每个 inner epoch 拿到旧策略下的新鲜样本、降低对单批数据的过拟合；代价是 rollout 是全流程最贵的环节，inner_epochs=2 意味着 rollout 开销翻倍。这是「样本新鲜度 vs 计算成本」的取舍。

### 4.2 模块二：clamp 裁剪——PPO 式信任区间

#### 4.2.1 概念说明

有了 ratio，PPO 的经典目标函数（GRPO 沿用）是把基线损失中的 \(\log p_\theta\) 换成裁剪后的 ratio 加权优势，再取**悲观下界**：

\[ \mathcal{J}(\theta) = \frac{1}{G}\sum_{i=1}^{G} \min\Big( r_i(\theta)\, A_i,\ \operatorname{clip}\big(r_i(\theta),\, 1-\varepsilon,\, 1+\varepsilon\big) \cdot A_i \Big), \qquad \mathcal{L} = -\mathcal{J} \]

在 \(\theta = \theta_{\text{old}}\)（即 \(r_i = 1\)）处，\(\operatorname{clip}\) 不起作用，该目标的梯度与策略梯度完全一致；一旦参数更新让 \(r_i\) 越出 \([1-\varepsilon,\ 1+\varepsilon]\)，**朝「继续变好」方向的梯度被截断，朝「回到区间」方向的梯度保留**。分四种情形看（记 \(\varepsilon\) 为 clip_eps）：

- **A > 0（好答案）且 r 太大（> 1+ε）**：概率已经提得够多了，`min` 选中常数项 \((1+\varepsilon)A\)——继续增大概率不再有奖励，**封顶**；但概率若跌破 1−ε，梯度仍在，允许拉回——**不封底**。
- **A < 0（坏答案）且 r 太小（< 1−ε）**：概率已经压得够低了，`min` 选中常数项 \((1-\varepsilon)A\)——继续压小不再有奖励，**封底**；但概率若冲破 1+ε，梯度仍在，允许压回——**不封顶**。

为什么用 `min` 而不是 `max` 或平均？`min` 永远取两个候选中**更保守（更小）**的目标值：任何裁剪只会让目标变差不会变好，模型无法从「冲出区间」中获得额外收益——这就是「悲观下界」。梯度层面：`torch.minimum` 把梯度路由到取到最小值的那一支，而 `torch.clamp` 在界外的梯度为 0，于是「被封顶/封底」的样本对 θ 的梯度恰好为零。

**ε 取多大？** 经典 PPO 用 0.2。但本脚本默认 `clip_eps=10.0`，argparse 的帮助文字直接写明出处：「GRPO/PPO clip range epsilon (DeepSeek uses 10)」（[7_4_plus_clip_ratio.py:L495-L500](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_4_plus_clip_ratio.py#L495-L500)）。目录 README 的「Tips & Tricks」清单也收录了 DAPO 的「Clip higher」经验（[README.md:L28](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/README.md#L28)）。结合 4.1.1 的序列级连乘不难理解：几百 token 连乘出来的 ratio 动辄数倍，若仍按 [0.8, 1.2] 裁剪，几乎每条样本都会被截断、训练信号被大面积冻结。ε=10 时裁剪区间是 [−9, 11]，而 \(r = e^{x} > 0\) 恒成立，**下界 −9 在数学上永不触发**，上界 11 也极少触发——默认配置下裁剪是一道「安全护栏」，不是主动约束。

#### 4.2.2 核心流程

```
unclipped  = ratio * adv                       # 未裁剪的重要性加权目标
clipped    = clamp(ratio, 1-ε, 1+ε) * adv      # 裁剪版
obj        = minimum(unclipped, clipped)       # 逐样本取悲观下界
loss       = -obj.mean()                       # 最大化目标 ⇔ 最小化损失
```

对比基线 `pg_loss = -(adv * logps).mean()`：`logps` 换成了 `ratio`，外面套了一层 `min·clip` 保护。

#### 4.2.3 源码精读

裁剪核心只有五行，位于 [7_4_plus_clip_ratio.py:L204-L211](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_4_plus_clip_ratio.py#L204-L211)：

```python
clipped_ratio = torch.clamp(ratio, 1.0 - clip_eps, 1.0 + clip_eps)

# PPO-style clipping
unclipped = ratio * adv
clipped = clipped_ratio * adv
obj = torch.minimum(unclipped, clipped)

loss = -obj.mean()
```

- `torch.clamp(ratio, 1-ε, 1+ε)`：把 ratio 限制进信任区间，界外梯度为 0；
- `torch.minimum`：逐样本取更小者（悲观下界），梯度流向被选中的分支；
- `-obj.mean()`：与基线一致，把「最大化期望优势」翻译成最小化问题。

`adv` 来自 [L171-L173](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_4_plus_clip_ratio.py#L171-L173)：组内标准化后 `detach()`，在裁剪公式中是常数系数。**注意 ratio 的每个分量是一条 rollout 的序列级比值**（`new_logps`/`old_logps` 都是 per-rollout 的标量张量，shape `(G,)`），不是 per-token 矩阵。

零优势捷径继承自 7_3（[L175-L191](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_4_plus_clip_ratio.py#L175-L191)）：全对/全错时提前返回 `loss=0.0`、`policy_ratio=1.0`、`loss_tensor=None`，训练循环据此跳过 backward（[L352](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_4_plus_clip_ratio.py#L352)）——没有学习信号时 ratio 也无从谈起，故占位为 1。

#### 4.2.4 代码实践

**实践目标**：验证 4.2.1 的方向性结论——哪些样本梯度被冻结、哪些仍在流动。

**操作步骤**（示例代码，接 4.1.4 的数据，取 ε=0.2）：

```python
import torch

old_logp = torch.tensor([-5.0, -5.0, -5.0, -5.0])
new_logp = torch.tensor([-4.9, -4.5, -3.0, -7.0], requires_grad=True)
adv      = torch.tensor([1.0, 1.0, -1.0, -1.0])   # detach 的优势
eps      = 0.2

ratio = torch.exp(new_logp - old_logp)
clipped_ratio = torch.clamp(ratio, 1.0 - eps, 1.0 + eps)
obj = torch.minimum(ratio * adv, clipped_ratio * adv)
loss = -obj.mean()
loss.backward()
print("ratio:", ratio.detach())       # [1.105, 1.649, 7.389, 0.135]
print("clipped:", clipped_ratio)      # [1.105, 1.200, 1.200, 0.800]
print("obj:", obj.detach())           # [1.105, 1.200, -7.389, -0.800]
print("loss:", loss.item())           # 1.471
print("grad d(loss)/d(new_logp):", new_logp.grad)
```

**需要观察的现象**：重点看 `new_logp.grad` 的四个分量。手算预期（\(\partial \text{obj}_i/\partial \log p_i = r_i A_i\)，再乘 \(-1/4\)）：

| i | r | A | 梯度 | 解读 |
|---|---|---|---|---|
| 1 | 1.105（区间内） | +1 | −0.276（≠0） | 正常：增大好答案概率 |
| 2 | 1.649（> 1.2，A>0） | +1 | **0** | 封顶：继续增大概率无奖励 |
| 3 | 7.389（> 1.2，A<0） | −1 | +1.847（≠0） | 不封顶：仍允许压低坏答案概率 |
| 4 | 0.135（< 0.8，A<0） | −1 | **0** | 封底：继续压小无奖励 |

**预期结果**：梯度恰好只在「A>0 且越上界」与「A<0 且越下界」两种情形为零，且梯度符号方向正确（负号 ⇒ 下降法增大该 logprob；正号 ⇒ 减小）。以上数值为手算结果，待本地运行验证。

#### 4.2.5 小练习与答案

**练习 1**：把 `torch.minimum` 换成 `torch.maximum` 会发生什么？

**答案**：目标变成「乐观上界」：样本可以靠冲出信任区间获得比未裁剪 surrogate 更高的目标值，裁剪反而奖励大幅漂移——信任区间彻底失效，失去加入裁剪的意义。

**练习 2**：默认 `clip_eps=10.0` 时，`clamp` 的两个界分别是多少？哪个在数学上不可能触发？

**答案**：区间 [1−10, 1+10] = [−9, 11]。ratio = exp(·) 恒正，故下界 −9 **永不**触发；上界 11 对应 log_ratio > ln 11 ≈ 2.40，只有单步更新把整条序列的概率拉升 11 倍以上才会触发。

**练习 3**：既然默认 ε=10 几乎不裁剪，作者为什么还要实现裁剪？

**答案**：① 它是可控参数——`--clip_eps 0.2` 即可切换到激进约束做对比实验（这正是实验脚本的价值）；② 护栏意义——即使几乎不触发，也能防极端漂移下的破坏性大更新；③ 教学意义——展示 PPO 家族的标准组件如何嫁接到 GRPO 上，并引出「序列级 ratio 天然漂得远 ⇒ 需要 clip higher」的洞察（DAPO 经验，见目录 README）。

### 4.3 模块三：compute_grpo_loss 改动点与 policy_ratio 监控

#### 4.3.1 概念说明

把 7_4 与 7_3/第 6 章基线逐项对照（u8-l1 已讲过的跟踪指标不再重复）：

| 维度 | ch06 / 7_3 基线 | 7_4（本讲） |
|---|---|---|
| rollout 采样自 | `model` 自身 | `old_model`（deepcopy 冻结副本） |
| logprob 计算次数 | 1 次：同模型、带梯度 | 2 次：旧策略 no_grad + 当前策略带梯度 |
| 损失公式 | `-(A · logp).mean()` | `-min(r·A, clip(r)·A).mean()` |
| 每个 step 的更新次数 | 1 | `inner_epochs`（默认 2），backward/step 移入内层循环 |
| 新增监控指标 | — | `policy_ratio`（组内 ratio 均值，进 txt 日志与 CSV） |
| 新增命令行参数 | — | `--clip_eps`（默认 10.0）、`--inner_epochs`（默认 2） |

**policy_ratio 是「eval/训练间 logp 漂移」的直接读数。** 它回答的问题是：这个 step 里，被更新后的模型给这批 rollout 的概率，相对采样时旧模型的概率，平均变了多少倍？理想健康状态是略大于 1——太小（≈1）说明没学到东西，太大说明单步更新过猛（u8-l1 观察到的失稳前兆就可以用它提前捕捉）。

还有一个工程细节值得点出：训练循环里 `stats` 被 inner 循环**覆盖**（[L338-L351](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_4_plus_clip_ratio.py#L338-L351)），随后写入 CSV 的 `policy_ratio`（[L411-L423](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_4_plus_clip_ratio.py#L411-L423)）是**最后一个** inner epoch 的值——即「经过 inner_epochs−1 次更新后的漂移量」。

#### 4.3.2 核心流程

完整的 `compute_grpo_loss` 六阶段（相对 u7-l3 的五阶段，新增「记录旧 logprob」）：

```
① 采样：num_rollouts 条 rollout，全部来自 old_model（eval + no_grad）
② 记录：old_logp（旧策略、no_grad）+ reward + entropy
③ 优势：组内标准化 rewards → advantages（detach）
   └─ 全零优势且 skip_zero_adv ⇒ 提前返回（loss=0, ratio=1）
④ 重算：new_logp（当前 model、带梯度）
⑤ 比值：log_ratio = new − old；ratio = exp(log_ratio)
⑥ 损失：obj = min(ratio·A, clamp(ratio,1−ε,1+ε)·A)；loss = −obj.mean()
返回：loss 标量 + loss_tensor（带梯度）+ policy_ratio + rewards/entropies/advantages/samples
```

#### 4.3.3 源码精读

**返回字典与 policy_ratio 的诞生。** [7_4_plus_clip_ratio.py:L213-L222](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_4_plus_clip_ratio.py#L213-L222)：`"policy_ratio": ratio.mean().item()`——组内所有 rollout 序列级比值的均值；`loss_tensor` 供训练循环 backward，`loss` 是其浮点快照。

**policy_ratio 进入双日志。** 文本日志 [L253-L262](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_4_plus_clip_ratio.py#L253-L262) 末尾追加 `policy_ratio={...:.3f}`；CSV 表头与数据行 [L263-L276](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_4_plus_clip_ratio.py#L263-L276) 新增第 10 列 `policy_ratio`，排在 `entropy_avg` 之后、`eval_acc` 之前。

**inner_epochs 内层循环。** [7_4_plus_clip_ratio.py:L337-L357](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_4_plus_clip_ratio.py#L337-L357)：`for _ in range(inner_epochs):` 内部依次调用 `compute_grpo_loss` → `zero_grad` → `backward` → `clip_grad_norm_` → `optimizer.step`。与第 6 章不同，**优化器步进被搬进了内层**——同一批（重新采样的）数据上连续更新两次，这是 ratio 产生的根本原因。

**真实日志证据。** 作者发布的训练日志 [7_4_plus_clip_ratio_metrics.csv:L2-L6](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/02_logs/7_4_plus_clip_ratio_metrics.csv#L2-L6) 前 5 步的 `policy_ratio` 为 1.032、2.145、1.849、3.141、0.950：第一步贴近 1（第一次更新幅度尚小），随后在 1~3 倍间波动——单步更新让整条序列的概率变动 1~3 倍，正呼应了 4.1.5 练习 2 的连乘效应；也解释了为什么 ε 要取 10 而不是 0.2（0.2 会把这批样本全部冻结在界外）。

#### 4.3.4 代码实践

**实践目标**：用真实训练日志验证 policy_ratio 的行为，并把它与 u8-l1 的指标并排解读。

**操作步骤**：

1. 用 `pandas` 读取 `ch07/02_logs/7_4_plus_clip_ratio_metrics.csv`；
2. 打印 `policy_ratio` 列的 `describe()`（均值/最小/最大）；
3. 仿照 u8-l1 的 `plot_grpo_metrics`（[reasoning_from_scratch/ch07.py:L55-L103](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch07.py#L55-L103)），把 `policy_ratio` 与 `reward_avg`、`entropy_avg` 画在同一 x 轴上（该函数为 2×2 网格，可取 `columns=["reward_avg", "entropy_avg", "policy_ratio", "avg_response_len"]`）。

```python
import pandas as pd
from reasoning_from_scratch.ch07 import plot_grpo_metrics

df = pd.read_csv("ch07/02_logs/7_4_plus_clip_ratio_metrics.csv")
print(df["policy_ratio"].describe())
plot_grpo_metrics("ch07/02_logs/7_4_plus_clip_ratio_metrics.csv",
                  columns=["reward_avg", "entropy_avg", "policy_ratio", "avg_response_len"])
```

**需要观察的现象**：`policy_ratio` 始终为正、围绕 1~3 波动（前 14 行抽样最小 0.95、最大 3.14），远离 ε=10 的上界 11；对照 `entropy_avg` 的骤降段，看 ratio 是否同步异常放大。

**预期结果**：按前 14 行抽样推断，500 步内 policy_ratio 均值约在 1~2 之间、最大不超过个位数——即默认配置下裁剪从未（或极罕见）真正触发；完整 500 步统计量待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：既然每个 inner epoch 都会重新计算 `old_logp`（old_model 没变，rollout 却重新采了），这些 old_logp 值和第一个 epoch 的一样吗？

**答案**：不一样。`old_model` 权重没变，但 rollout 是重新采样的**不同序列**——每条序列有自己的旧 logprob。ratio 比较的永远是「同一条序列在新旧两套权重下的概率」，序列变了，分子分母就都换了。

**练习 2**：如果只看 CSV 的 `policy_ratio` 列，能否判断某个 step 是否真的发生了裁剪？

**答案**：不能直接判断。CSV 记录的是组内均值，均值未越界不代表个别 rollout 未越界；且脚本没有落盘逐样本 ratio。要精确判断需在 `compute_grpo_loss` 里补一条日志（如 `ratio.max().item()`、被裁剪样本计数）——这本身就是很好的二次开发练习。

**练习 3**：`--clip_eps 0.2` 会让训练发生什么？先推演再（若有 GPU）实验。

**答案**：推演——由 4.3.3 的日志证据，序列级 ratio 常态在 1~3，0.2 的界 [0.8, 1.2] 会把大量样本推出区间；其中 A>0 且 r>1.2、A<0 且 r<0.8 的样本梯度被冻结，有效学习信号显著减少，训练变慢甚至停滞（「梯度饥饿」现象，DAPO 提出 clip higher 正是为此）。实验属 GPU 任务，待本地验证。

## 5. 综合实践

**任务：亲手复现一遍裁剪损失，并用五类情形表总结裁剪方向。**

**实践目标**：把本讲三个模块串起来——ratio 计算 → clamp/minimum 裁剪 → 梯度冻结方向，全部用可运行代码验证，再与 7_4 源码逐行对照。

**操作步骤**：

1. **手算**。五条 rollout 的数据如下（ε=0.2），先在纸上算出每条的 ratio、裁剪后 ratio、obj 与「梯度是否为 0」：

   | i | logp_old | logp_new | A | log_ratio | ratio | clip 后 |
   |---|---|---|---|---|---|---|
   | 1 | −5.0 | −4.9 | +1 | +0.1 | 1.105 | 1.105 |
   | 2 | −5.0 | −4.5 | +1 | +0.5 | 1.649 | 1.200 |
   | 3 | −5.0 | −3.0 | −1 | +2.0 | 7.389 | 1.200 |
   | 4 | −5.0 | −7.0 | −1 | −2.0 | 0.135 | 0.800 |
   | 5 | −5.0 | −5.1 | +1 | −0.1 | 0.905 | 0.905 |

2. **代码验证**。运行 4.2.4 的示例脚本（五条数据各加一行即可），核对 `obj`、`loss = -mean(obj)` 与 `new_logp.grad`。手算参考值：obj = [1.105, 1.200, −7.389, −0.800, 0.905]，均值 = −4.979/5 ≈ −0.996，loss ≈ +0.996；梯度为零的只有第 2、4 条。
3. **阅读源码对照**。打开 [7_4_plus_clip_ratio.py:L200-L211](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_4_plus_clip_ratio.py#L200-L211)，确认你的实现与源码在 `exp`、`clamp`、`minimum`、`mean` 四步上逐一对应。
4. **完成五情形方向表**（本讲最重要的产出）：

   | # | 优势 | ratio 位置 | obj 取值 | 梯度 | 裁剪方向 |
   |---|---|---|---|---|---|
   | 1 | A > 0 | r > 1+ε | (1+ε)·A（常数） | 0 | **封顶**：不再奖励继续增大概率 |
   | 2 | A > 0 | 1−ε ≤ r ≤ 1+ε | r·A | ≠0 | 不裁：正常鼓励增大概率 |
   | 3 | A > 0 | r < 1−ε | r·A | ≠0 | 不封底：允许概率回升进区间 |
   | 4 | A < 0 | r > 1+ε | r·A | ≠0 | 不封顶：允许继续压低概率 |
   | 5 | A < 0 | r < 1−ε | (1−ε)·A（常数） | 0 | **封底**：不再奖励继续压小概率 |

5. **直觉解释**（写成 3~5 句话，示例）：裁剪的本质是**只封锁「顺优势方向越界后继续加码」的梯度，永远不封锁「回到信任区间」的梯度**。好答案的概率提得太多就停止奖励，坏答案的概率压得太低也停止奖励——但无论哪种情况，把 ratio 拉回 [1−ε, 1+ε] 的路径始终敞开。于是单步更新被限制在旧策略的一个邻域内，策略只能「小步走」，不能一个梯度步跳出信任区间。

**预期结果**：代码输出与手算表一致（数值待本地验证）；第 2、4 条（表中第 2 行与第 5 行方向）梯度严格为 0，其余非零且符号方向正确。

## 6. 本讲小结

- **ratio 是策略漂移的读数**：rollout 从 deepcopy 冻结的 `old_model` 采样并记录旧 logprob（no_grad），当前模型带梯度重算新 logprob，`ratio = exp(new − old)`；它是逐 token 概率比的**序列级连乘**，天然容易偏离 1。
- **裁剪 = 悲观下界的信任区间**：`obj = min(r·A, clamp(r, 1−ε, 1+ε)·A)`；A>0 封顶不封底、A<0 封底不封顶——只冻结「越界后继续加码」的梯度，不冻结「回家」的梯度。
- **inner_epochs 是 ratio 存在的原因**：优化器步进移入内层循环，同一 step 内第二次更新时数据已成 off-policy，ratio 完成重要性修正。
- **默认 ε=10 是宽松护栏**：DeepSeek 风格的大 ε 应对序列级连乘的大漂移（实测 policy_ratio 常态 1~3），裁剪几乎不触发；`--clip_eps 0.2` 可切换为激进约束做对比。
- **新增可观测指标 policy_ratio** 进 txt 日志与 CSV 第 10 列，与 reward/entropy 并排监控，是「单步更新是否过猛」的预警表。

## 7. 下一步学习建议

裁剪是对策略移动幅度的**硬约束**（越界即冻结梯度）。下一讲 **u8-l3（KL 散度正则）** 介绍另一种**软约束**：在损失中加一项当前策略与参考策略的逐 token KL 惩罚，越界不是被冻结而是被「罚款」，两者的权衡（clip 的硬边界 vs KL 的连续惩罚）是第 7 章的核心张力。随后 **u8-l4（格式奖励）** 转向奖励设计，**u8-l5（训练变体）** 里的 Olmo3/DeepSeek-V3.2 风格脚本会再次遇到本讲的主题——DAPO 的 clip higher、GSPO 的序列级重要性采样正是「序列级 ratio + 裁剪」这条路线的后继演化（见目录 README 的 Tips & Tricks 清单）。建议阅读顺序：先精读 [7_5_plus_kl.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_5_plus_kl.py#L1-L1) 与 7_4 的 diff，体会「同一损失上叠加约束」的演进方式。
