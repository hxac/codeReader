# GRPO 算法实现

## 1. 本讲目标

本讲聚焦 veRL 中 PPO 训练循环的另一条算法分支——**GRPO（Group Relative Policy Optimization，组相对策略优化）**。学完后你应当能够：

- 说清 GRPO 用「**同一 prompt 的多次采样做组内归一化**」来替代 critic（价值网络）的核心思想，以及它为什么能让 TinyZero 在 3B 规模下省掉一整个价值模型。
- 读懂 `compute_grpo_outcome_advantage` 中按 `uid` 分组、用 `id2mean / id2std` 归一化、再把标量优势广播到所有 response token 的完整流程，并解释「组内只有 1 个样本」时 `mean=0、std=1` 的兜底处理。
- 看懂 GRPO 如何通过 `use_kl_loss=True` 把 KL 约束从「reward 端扣分」搬到「loss 端直接加项」，并与 `kl_loss_coef`、`kl_loss_type=low_var_kl` 配合。
- 能够对照 `examples/grpo_trainer` 的脚本，列出启用 GRPO 必须改动的几个配置项。

本讲承接 [u5-l1](./u5-l1-kl-penalty-advantage.md)（`apply_kl_penalty` 与 `compute_advantage` 的分发）与 [u5-l2](./u5-l2-policy-loss-clip.md)（策略损失与 importance ratio）。如果这两篇里的 `compute_advantage`、`use_kl_loss` 开关你还印象模糊，建议先回顾。

## 2. 前置知识

在进入源码前，先用直觉理解三件事。

**① 为什么 PPO/GAE 需要 critic。** GAE（Generalized Advantage Estimation）通过一个价值网络 \(V_\phi(s_t)\) 给每个 token 打「基线」，再用 TD 误差 \(\delta_t = r_t + \gamma V(s_{t+1}) - V(s_t)\) 反向递推，得到**逐 token**的优势 \(\hat{A}_t\)。优势减去基线后方差更小，训练更稳。代价是：要多训练、多存一个价值网络，显存和工程复杂度都翻倍。

**② GRPO 的替代思路。** 既然「减基线」是为了降方差，那是不是只能用学出来的 critic？GRPO 的回答是：**对同一个 prompt 采样多次**，用这组回答各自结果的均值和标准差作为基线。某个回答比同组平均好，优势为正；比平均差，优势为负。这样一来：

- 不需要价值网络（省掉 critic）。
- 优势是**逐序列的常数**（同一个回答里每个 token 的优势相同），而不是逐 token 不同。

代价是：每个 prompt 必须采样 \(n>1\) 条回答（`rollout.n`），生成开销变大。

**③ KL 的两条路径。** 在 veRL 里，约束「训练策略不要跑离参考策略太远」有两种实现，二者**互斥**（详见 [u5-l1](./u5-l1-kl-penalty-advantage.md)）：

- **reward 端（`use_kl_loss=False`，PPO/GAE 路线）**：在奖励里扣一项 \( -\beta \cdot \mathrm{KL}\)，即 `apply_kl_penalty`。
- **loss 端（`use_kl_loss=True`，GRPO 路线）**：不在 reward 上扣分，而是直接在 actor 损失里加一项 `kl_loss * kl_loss_coef`。

GRPO 走第二条路。本讲会解释为什么。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `verl/trainer/ppo/core_algos.py` | GRPO 优势计算的核心函数 `compute_grpo_outcome_advantage`，以及 KL 估计函数 `kl_penalty`。 |
| `verl/trainer/ppo/ray_trainer.py` | 训练主循环里把 GRPO 接进来的三处：`compute_advantage` 分发器、`init_workers` 里 `use_critic=False`、`fit()` 里 `uid` 赋值与 `use_kl_loss` 分支。 |
| `verl/workers/actor/dp_actor.py` | actor 的 `update_policy`，其中 `use_kl_loss=True` 分支把 KL 加进策略损失。 |
| `verl/protocol.py` | `DataProto.repeat(interleave=True)`，保证 `uid` 与多次采样的 response 对齐，是 GRPO 分组的前提。 |
| `examples/grpo_trainer/run_qwen2-7b.sh` | 一个真实可参考的 GRPO 启动脚本。 |
| `verl/trainer/config/ppo_trainer.yaml` | GRPO 相关默认配置（`use_kl_loss`、`kl_loss_coef`、`kl_loss_type`、`rollout.n`、`adv_estimator`）。 |

## 4. 核心概念与源码讲解

GRPO 的实现可以拆成三个最小模块：①组内归一化的优势函数；②`compute_advantage` 如何把 GRPO 接进训练循环（含 `uid` 分组机制）；③actor 端的 loss 端 KL。

### 4.1 GRPO 的核心思想：用组内归一化替代 critic

#### 4.1.1 概念说明

GRPO 把「优势 = 回报 − 基线」里的基线，从「学出来的 critic」换成「**同组回答的平均回报**」，并且用「同组回报的标准差」做归一化。设一个 prompt 采样了 \(n\) 条回答，第 \(i\) 条的（outcome）回报为 \(r_i\)，则该回答的优势为：

\[
A_i = \frac{r_i - \mathrm{mean}(r_{1..n})}{\mathrm{std}(r_{1..n}) + \epsilon}
\]

这就是「组相对」的含义。它本质是把一组回答的分数做 **z-score 标准化**：比平均好的为正、差的为负，尺度归一到「几个标准差」。因为 GRPO 只用 outcome（每个回答一个标量分），所以这条优势是**序列级常数**，会被原样广播到该回答的所有 response token 上——这与 GAE 的逐 token 优势形成鲜明对比。

> 注：DeepSeek 的 GRPO 论文用的就是这个「组均值 + 组标准差」形式；它和 RLOO（leave-one-out 均值、不含标准差）是不同的变体。本仓库实现的是前者。

#### 4.1.2 核心流程

`compute_grpo_outcome_advantage` 的执行过程可以用下面伪代码概括：

```
输入: token_level_rewards (bs, response_length), eos_mask (bs, response_length), index=uid (bs,)
1. 把每条回答的 token 级奖励求和 → 得到每条回答的标量 outcome 分数 scores (bs,)
   (奖励本就稀疏地放在最后一个有效 token，求和即还原标量)
2. 按 uid 把 scores 分组: id2score[uid] = [r_1, r_2, ..., r_n]
3. 对每个组算 mean / std:
   - 组内样本数 == 1: mean=0, std=1   (兜底，避免除零)
   - 组内样本数  > 1:  mean=组均值, std=组标准差
4. 对每条回答做组内归一化: scores[i] = (scores[i] - mean) / (std + eps)
5. 把标量广播到所有 response token: (bs,) → (bs, response_length)，再乘 eos_mask 抹掉 pad 位
6. 返回 (scores, scores)  —— advantages 与 returns 相同（GRPO 无 critic，returns 不再被使用）
```

关键点有两个：**分组靠 `uid`**（同一个 prompt 的 \(n\) 条回答共享一个 uid）；**广播后所有 token 优势相同**（这是 outcome 监督的必然结果）。

#### 4.1.3 源码精读

函数入口与文档注释明确说明它只处理 outcome（标量）奖励：

[verl/trainer/ppo/core_algos.py:L110-L129](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L110-L129) —— 注释 `only consider outcome supervision, where the reward is a scalar`。

第一步：用 `non_zero_mask` 把 token 级奖励压成每条回答一个标量分数：

[verl/trainer/ppo/core_algos.py:L130-L132](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L130-L132) —— `scores = (token_level_rewards * non_zero_mask).sum(dim=-1)`。奖励在 u2-l4 / u4-l4 里被挂在回答最后一个有效 token 上，其余位置为 0，沿序列求和即还原出标量 outcome 分数。`non_zero_mask` 在这里主要是显式表达意图（0 乘任何数仍是 0，结果一致）。

第二步：按 `uid` 分组并算组内均值/标准差：

[verl/trainer/ppo/core_algos.py:L138-L150](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L138-L150) —— 逐样本把 `scores[i]` 追加到 `id2score[index[i]]`（`index` 就是 `uid`）。随后：

- 组内只有 1 个样本：`id2mean[idx]=0, id2std[idx]=1`，于是归一化后退化为 `scores[i] - 0 = scores[i]`（不归一化，见 4.1.4 解释）。
- 组内有多个样本：`id2mean` 用 `torch.mean`，`id2std` 用 `torch.std`。

> ⚠️ 读源码时注意一个**不影响结果但容易让人困惑的写法**：均值分支写的是 `torch.tensor(id2score[idx])`（一维），而标准差分支写的是 `torch.tensor([id2score[idx]])`（多套了一层 list，变成形状 `(1, n)` 的二维张量）。由于 `torch.std` 默认对所有元素求，多出来的那一维不影响数值，最终标准差与一维写法相同。这是个历史遗留的小不一致，知道即可。

第三步：组内归一化，并把标量广播到整个 response 段：

[verl/trainer/ppo/core_algos.py:L151-L155](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L151-L155) —— `scores[i] = (scores[i] - mean) / (std + eps)`，再 `scores.unsqueeze(-1).tile([1, response_length]) * eos_mask`。这就是「序列级常数广播到每个 token」的实现：广播后再乘 `eos_mask`，把 padding 位置清零。最后 `return scores, scores`——**advantages 与 returns 是同一个张量**，因为 GRPO 没有价值网络，`returns` 在后续 `update_critic` 中根本不会被消费（`use_critic=False`），这里写一份只是为了和 GAE 分支的接口保持一致。

#### 4.1.4 代码实践：手算组内归一化

**实践目标**：用一个最小例子验证你对「分组 + 归一化 + 广播」的理解。

**操作步骤**（示例代码，非项目原有代码）：

```python
import torch
from collections import defaultdict

# 模拟 1 个 prompt 采样 3 条回答，outcome 分数分别为 1.0, 0.0, 0.1
scores = torch.tensor([1.0, 0.0, 0.1])
uid = ["p0", "p0", "p0"]
eps = 1e-6

id2score = defaultdict(list)
for i in range(len(scores)):
    id2score[uid[i]].append(scores[i])

mean = torch.mean(torch.tensor(id2score["p0"]))
std = torch.std(torch.tensor(id2score["p0"]))
adv = (scores - mean) / (std + eps)
print("mean:", mean.item(), "std:", std.item())
print("advantages:", adv.tolist())
```

**需要观察的现象**：分数最高的 1.0 得到正优势、最低的 0.0 得到负优势、中间的 0.1 接近 0。

**预期结果**：mean≈0.367、std≈0.479；advantages 约为 `[1.322, -0.764, -0.558]`（浮点近似）。可见「比平均好」为正、「比平均差」为负。

**关于「组内只有 1 个样本」的解释**：把上面的 `scores` 改成只有 1 个元素（对应 `rollout.n=1`）。此时代码走 `len==1` 分支，令 `mean=0, std=1`，于是优势 `= (score - 0)/(1 + eps) = score`，**完全不做归一化**，直接把原始分数当作优势。这是为了避免「标准差为 0 → 除零」的保护：单样本谈不上「组内相对」，只能原样使用。这也正是 GRPO 必须 `rollout.n>1` 的根本原因——`n=1` 时每组都退化成单样本，归一化机制完全失效。

> 待本地验证：上述数值取决于 `torch.std` 的 Bessel 校正（默认 `correction=1`，分母为 \(n-1\)）。

#### 4.1.5 小练习与答案

**练习 1**：如果一组 3 条回答的分数完全相同（例如 `[0.5, 0.5, 0.5]`），GRPO 给出的优势是什么？这说明什么？

**参考答案**：mean=0.5，std=0，但代码里组内样本数 >1 时 `std` 由 `torch.std` 算得为 0（注意：`torch.std` 对常数序列返回 0），于是分母 `0 + eps`，优势约为 `(0.5-0.5)/eps = 0`。即组内没有区分度时优势为 0，模型从这组学不到梯度信号——这迫使同一 prompt 的多次采样必须产生**有差异**的结果，GRPO 才能起作用。

**练习 2**：为什么 GRPO 把标量优势「广播到所有 response token」是合理的？

**参考答案**：因为 GRPO 只用 outcome 奖励（整段回答一个总分），没有更细粒度的 token 级信用分配。同一段回答里每个 token 对最终分数的贡献无法区分，于是只能把同一个序列级优势平均地分给所有 token。GAE 则用 critic 的逐 token 价值差来实现细粒度信用分配——这是两者最核心的区别。

---

### 4.2 `compute_advantage` 分发器与 GRPO 的接线

#### 4.2.1 概念说明

`compute_advantage` 是个**分发器**：它根据 `adv_estimator`（`gae` 或 `grpo`）把工作派给 `compute_gae_advantage_return` 或 `compute_grpo_outcome_advantage`。GRPO 分支有两个特征：**不需要 critic 的 values**、**需要 `uid` 作为分组键**。`uid` 在训练主循环 `fit()` 里被赋值，并通过 `DataProto.repeat(interleave=True)` 与多次采样的回答对齐——这一步是 GRPO 分组能否正确工作的前提，必须单独说清。

#### 4.2.2 核心流程

GRPO 的数据准备链条（发生在 `fit()` 的每个 step）：

```
1. gen_batch_output = generate_sequences(gen_batch)   # 每个 prompt 生成 n 条回答，顺序为 [p0的n条, p1的n条, ...]
2. batch['uid'] = 给每个 prompt 随机一个 uuid          # 此时 batch 还是一行一个 prompt
3. batch = batch.repeat(n, interleave=True)            # uid 变成 [u0,u0,..(n个),u1,u1,..]，与生成顺序对齐
4. batch = batch.union(gen_batch_output)               # 合并：每行 = (uid, 该 prompt 的某条回答)
   ⇒ 同一个 uid 恰好对应同一 prompt 的 n 条回答 = 一个 GRPO 组
5. compute_advantage(batch, adv_estimator='grpo', ...) # 用 uid 分组算优势
```

关键洞察：`interleave=True` 用的是 `torch.repeat_interleave`（模式 `[A,A,A,B,B,B]`）和 `np.repeat`（同模式），这正好匹配 `generate_sequences` 输出的「连续 \(n\) 条」顺序。如果误用 `interleave=False`（`np.tile`，模式 `[A,B,A,B,...]`），`uid` 与回答就会错位，分组彻底错乱。

#### 4.2.3 源码精读

**`compute_advantage` 的 GRPO 分支**——注意它取的是 `data.non_tensor_batch['uid']` 作为 `index`，且只读 `token_level_rewards`，不读 `values`：

[verl/trainer/ppo/ray_trainer.py:L133-L144](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L133-L144) —— `index = data.non_tensor_batch['uid']`，然后把 `(token_level_rewards, response_mask, index)` 交给 `compute_grpo_outcome_advantage`。对比上面 `gae` 分支（[L119-L132](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L119-L132)）需要 `values`，GRPO 分支完全不需要——这就是「省掉 critic」在算法分发层的体现。

**`init_workers` 里关闭 critic**：

[verl/trainer/ppo/ray_trainer.py:L466-L467](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L466-L467) —— `elif self.config.algorithm.adv_estimator == 'grpo': self.use_critic = False`。于是后续不会创建 `critic_wg`、不调用 `compute_values` / `update_critic`，显存与算力都省下一大块。

**`fit()` 里 `uid` 的赋值与对齐 repeat**：

[verl/trainer/ppo/ray_trainer.py:L591-L595](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L591-L595) —— 先给每个 prompt 生成一个 `str(uuid.uuid4())` 作为 `uid`，**注意必须在 `repeat` 之前赋值**（否则每个 prompt 会被赋成不同 uuid，分组失败）；随后 `batch.repeat(repeat_times=rollout.n, interleave=True)` 把 prompt 侧（含 uid）复制 \(n\) 份，与 `generate_sequences` 输出的 \(n\) 条回答一一拼接。

`repeat` 内部的对齐逻辑：

[verl/protocol.py:L558-L583](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/protocol.py#L558-L583) —— 张量用 `repeat_interleave`、非张量用 `np.repeat`，二者都是 `[A,A,A,B,B,B]` 模式，确保 uid 与 rollout 输出的排列顺序一致。

#### 4.2.4 代码实践：追踪 uid 对齐

**实践目标**：验证 `repeat(interleave=True)` 是 GRPO 分组正确的前提。

**操作步骤**：

1. 打开 [verl/trainer/ppo/ray_trainer.py:L586-L600](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L586-L600)，确认 `uid` 赋值（L591）发生在 `generate_sequences`（L589）之后、`repeat`（L594）之前。
2. 阅读注释 L598-L599：`Note that this breaks the order of data inside the batch. Please take care when you implement group based adv computation such as GRPO and rloo`。
3. 思考：`_balance_batch`（L600）会用 Karmarkar-Karp 重排各 DP rank 的数据（见 [u7-l2](./u7-l2-seqlen-balancing-dynamic-bsz.md)），这会不会破坏 GRPO 分组？

**需要观察的现象 / 思考结论**：

- `uid` 是随 prompt 走的「身份标签」，`_balance_batch` 只是在不同 DP rank 之间搬运整行（含 uid），同一个 uid 的 \(n\) 条回答可能被搬到同一 rank 也可能跨 rank。
- 但 `compute_advantage` 在 driver 进程上对**全 batch**统一按 uid 分组（`id2score` 是全局字典），所以只要每行的 uid 正确，rank 间的物理分布不影响分组结果——这正是「单控制器在 driver 上算优势」的好处。

**预期结果**：你能解释「为什么 `_balance_batch` 重排了行序，却不会破坏 GRPO 的组内归一化」。这是 GRPO 在分布式下的一个非平凡正确性保证。

> 待本地验证：若你有多卡环境，可打印各 rank 收到的 uid 列表，确认同一 uid 的 \(n\) 条回答会被任意切分到不同 rank，而最终优势仍由 driver 统一算出。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `batch.repeat(repeat_times=n, interleave=True)` 改成 `interleave=False`，会发生什么？

**参考答案**：`interleave=False` 用 `np.tile`，uid 会变成 `[u0,u1,...,u0,u1,...]`，而 `generate_sequences` 输出的回答顺序是 `[p0的n条,p1的n条,...]`。两者错位后，`uid` 不再能正确标识「哪几条回答来自同一 prompt」，GRPO 分组（`id2score`）会把不同 prompt 的回答混进同一组、把同一 prompt 的回答拆散到不同组，优势计算彻底错误。

**练习 2**：为什么 `uid` 必须在 `repeat` **之前**赋值，而不能在之后？

**参考答案**：在 `repeat` 之前，batch 是「一行一个 prompt」，给每个 prompt 赋一个 uuid 后，repeat 会把这个 uuid 复制 \(n\) 份分给该 prompt 的 \(n\) 条回答，从而形成正确的组。若在 `repeat` 之后赋值，此时 batch 已经是「一行一条回答」（\(n \times\) 行数），每行会被赋一个**不同**的 uuid，每个「组」就只剩 1 条回答，全部退化成单样本（走 4.1.4 的兜底分支），GRPO 失效。

---

### 4.3 loss 端 KL：`use_kl_loss` 与 `kl_loss_coef`

#### 4.3.1 概念说明

GRPO 把 KL 约束从 reward 端搬到 loss 端。回顾两条路径（见 [u5-l1](./u5-l1-kl-penalty-advantage.md)、[u5-l4](./u5-l4-kl-controller-variants.md)）：

| 维度 | reward 端（PPO/GAE） | loss 端（GRPO） |
| --- | --- | --- |
| 开关 | `use_kl_loss=False` | `use_kl_loss=True` |
| KL 在哪扣 | 在奖励里 `reward = score - beta*KL` | 在 actor 损失里 `loss = pg_loss - kl_loss*kl_loss_coef` |
| KL 系数 | `kl_ctrl.kl_coef`（可由控制器调度） | `kl_loss_coef`（固定常数） |
| 默认 KL 估计 | `kl`（朴素） | `low_var_kl`（低方差） |
| 需要 ref policy | 是 | 是 |

为什么 GRPO 选 loss 端？因为 GRPO 的优势是**组内归一化**后的相对值（尺度被 std 归一了），如果再在 reward 端按某个绝对系数 \(\beta\) 扣 KL，量纲上会和归一化后的优势打架、难以平衡。直接在 loss 端加一个固定的 `kl_loss_coef` 项，和策略损失 `pg_loss`、熵正则 `entropy_loss` 同处一个尺度，调参更直观。

#### 4.3.2 核心流程

GRPO 一步训练中，reward 与 loss 的分工：

```
fit():
  if use_kl_loss:                                     # GRPO 走这里
      token_level_rewards = token_level_scores        # ★ reward 端不扣 KL，直接等于原始分数
  else:
      token_level_rewards = apply_kl_penalty(...)     # PPO/GAE 在 reward 端扣 KL

  compute_advantage(..., adv_estimator='grpo')        # 用 (无 KL 的) reward 算组内归一化优势

dp_actor.update_policy():
  policy_loss = pg_loss - entropy_loss*entropy_coeff
  if use_kl_loss:
      kld = kl_penalty(log_prob, ref_log_prob, 'low_var_kl')
      kl_loss = masked_mean(kld, response_mask)
      policy_loss = policy_loss - kl_loss*kl_loss_coef   # ★ KL 在 loss 端加上
```

两处带 ★ 的就是 GRPO 的关键接线：reward 端「放过」KL，loss 端「补上」KL。

#### 4.3.3 源码精读

**`fit()` 里的 `use_kl_loss` 分支**：

[verl/trainer/ppo/ray_trainer.py:L631-L637](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L631-L637) —— `if not self.config.actor_rollout_ref.actor.use_kl_loss:` 才调用 `apply_kl_penalty`；`else` 分支直接 `token_level_rewards = token_level_scores`。GRPO（`use_kl_loss=True`）走 else，于是 reward 不含任何 KL 成分，组内归一化用的就是纯 outcome 分数。

**`dp_actor.update_policy` 里的 KL 加项**。先看策略损失的组合：

[verl/workers/actor/dp_actor.py:L256-L257](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L256-L257) —— `policy_loss = pg_loss - entropy_loss * entropy_coeff`（`pg_loss` 来自 `compute_policy_loss`，详见 [u5-l2](./u5-l2-policy-loss-clip.md)）。

再看 `use_kl_loss` 分支如何补上 KL：

[verl/workers/actor/dp_actor.py:L259-L269](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L259-L269) —— `kld = core_algos.kl_penalty(log_prob, ref_log_prob, kl_penalty=self.config.kl_loss_type)`，用 `masked_mean` 在有效 response token 上平均得到 `kl_loss`，然后 `policy_loss = policy_loss - kl_loss * self.config.kl_loss_coef`，并记录 `actor/kl_loss`、`actor/kl_coef` 两个监控指标。这里 `kl_loss_type` 默认 `low_var_kl`（见 4.3.4 配置），即 \(r-1-\log r\) 形式，方差比朴素 `kl` 小、恒非负（详见 [u5-l4](./u5-l4-kl-controller-variants.md)）。

为了让 `ref_log_prob` 可被取到，`update_policy` 在选字段时根据 `use_kl_loss` 动态加入 `ref_log_prob`：

[verl/workers/actor/dp_actor.py:L211-L214](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L211-L214) —— `if self.config.use_kl_loss: select_keys.append('ref_log_prob')`。最后损失除以 `gradient_accumulation`（[L271](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L271)）做梯度累积，这部分与 PPO 一致。

#### 4.3.4 代码实践：对照 GRPO 启动脚本读配置

**实践目标**：把「启用 GRPO 要改哪几个参数」落到具体配置行。

**操作步骤**：

1. 打开默认配置 [verl/trainer/config/ppo_trainer.yaml](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml)，找到这四处默认值（默认都是 PPO/GAE 设置）：
   - [L30-L32](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L30-L32)：`use_kl_loss: False`、`kl_loss_coef: 0.001`、`kl_loss_type: low_var_kl`（注释直写 `# True for GRPO`、`# for grpo`）。
   - [L84](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L84)：`n: 1`（注释 `# > 1 for grpo`）。
   - [L141](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L141)：`adv_estimator: gae`。
2. 打开真实 GRPO 脚本 [examples/grpo_trainer/run_qwen2-7b.sh](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/grpo_trainer/run_qwen2-7b.sh)，对照它如何用 Hydra 覆盖上述默认值：
   - [L6](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/grpo_trainer/run_qwen2-7b.sh#L6)：`algorithm.adv_estimator=grpo`。
   - [L18-L20](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/grpo_trainer/run_qwen2-7b.sh#L18-L20)：`actor_rollout_ref.actor.use_kl_loss=True`、`kl_loss_coef=0.001`、`kl_loss_type=low_var_kl`。
   - [L29](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/grpo_trainer/run_qwen2-7b.sh#L29)：`actor_rollout_ref.rollout.n=5`。

**需要观察的现象**：脚本显式设了 `adv_estimator=grpo`、`use_kl_loss=True`、`kl_loss_coef`、`kl_loss_type`、`rollout.n=5` 这五项；其中 `kl_loss_coef`/`kl_loss_type` 其实与默认值相同，写出来是为了可读与明确。

**预期结果**：你能总结出「启用 GRPO 的最小必改项」为 `algorithm.adv_estimator=grpo` + `actor_rollout_ref.actor.use_kl_loss=True` + `actor_rollout_ref.rollout.n>1`（`kl_loss_coef`、`kl_loss_type` 用默认值即可，但建议显式写出便于调参）。

#### 4.3.5 小练习与答案

**练习 1**：如果同时设 `adv_estimator=grpo` 却忘了把 `use_kl_loss` 改成 `True`（即保持默认 `False`），训练还能跑吗？会有什么隐患？

**参考答案**：能跑，但有隐患。此时 reward 端会走 `apply_kl_penalty`，从 `token_level_scores` 里扣掉 `beta*KL` 得到 `token_level_rewards`，GRPO 组内归一化就在「含 KL 扣分」的 reward 上做；同时因为没有 critic，`use_critic=False`，`apply_kl_penalty` 用的 `kl_ctrl` 仍是默认 fixed 的 `kl_coef=0.001`。这与 GRPO 论文的设计（KL 在 loss 端、reward 为纯 outcome）不一致，KL 会被「重复或错位」计入，量纲上和归一化优势不平衡。所以 `adv_estimator=grpo` 与 `use_kl_loss=True` 应当成对出现。

**练习 2**：GRPO 的 `kl_loss_coef` 是固定常数，而 PPO 的 `kl_coef` 可以接 `AdaptiveKLController` 自适应。为什么 GRPO 不需要自适应？

**参考答案**：自适应 KL 控制器是根据「当前 KL 与目标 KL 的比例误差」调节 \(\beta\)，本质是为了在 reward 端把 KL 拉回目标值。GRPO 把 KL 放在 loss 端，与 `pg_loss`、`entropy_loss` 同尺度，直接作为一个固定权重的正则项即可，调参时和其他损失系数一起权衡，不需要单独的反馈式控制器。这也让 GRPO 的超参更少、更易复现。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这份「GRPO 启动检查清单」。

**任务**：假设你要把一个原本用 PPO（`adv_estimator=gae`）跑的任务切换成 GRPO，请：

1. **列出必改参数**：从 [ppo_trainer.yaml](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml) 默认值出发，给出 Hydra 覆盖命令，至少包含 `adv_estimator`、`rollout.n`、`use_kl_loss`、`kl_loss_coef`，并说明每项的作用与你的取值依据（可参考 [run_qwen2-7b.sh](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/examples/grpo_trainer/run_qwen2-7b.sh)）。
2. **解释组内单样本兜底**：用你自己的话说清「组内只有 1 个样本时 `mean=0、std=1`」的代码（[core_algos.py:L143-L145](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L143-L145)）为什么这么处理，以及它和「为什么 GRPO 必须 `n>1`」之间的因果关系。
3. **画一条数据流**：从 `generate_sequences` 产出 \(n\) 条回答开始，依次标注 `uid` 赋值、`repeat(interleave=True)`、`token_level_rewards = token_level_scores`（`use_kl_loss=True` 分支）、`compute_grpo_outcome_advantage` 分组归一化、`update_policy` 里 `policy_loss - kl_loss*kl_loss_coef`，画出 reward 与 loss 两条线在 GRPO 下如何分工。

**参考要点**：

- 必改参数：`algorithm.adv_estimator=grpo`、`actor_rollout_ref.rollout.n=5`（或更大）、`actor_rollout_ref.actor.use_kl_loss=True`；`kl_loss_coef=0.001`、`kl_loss_type=low_var_kl` 建议显式写出。注意 `data.train_batch_size` 要能被 `n` 整除相关的约束，且 `n` 增大会按比例增加生成开销。
- 单样本兜底：std 对单样本未定义（或为 0），为避免除零，令 `mean=0、std=1`，使优势退化为原始分数——这保护了正确性，但也意味着 `n=1` 时归一化完全失效，所以 GRPO 要求 `n>1` 来保证每组有多个样本可比较。
- 数据流：reward 线「不扣 KL」→ 组内归一化得优势；loss 线「加 KL 项」→ 与 pg_loss、entropy_loss 组合成最终 policy_loss。两条线在 GRPO 下严格分离。

> 待本地验证：若你有 GPU 环境，可用 `examples/grpo_trainer/run_qwen2-7b.sh`（或缩小为更小模型）跑几个 step，在日志里确认 `actor/kl_loss`、`actor/pg_loss`、`critic/...`（GRPO 下应无 critic 指标）的出现与缺失情况。

## 6. 本讲小结

- **GRPO 用组内归一化替代 critic**：对同一 prompt 的 \(n\) 条回答，用组均值做基线、组标准差做归一化得到序列级常数优势，省掉整个价值网络（`use_critic=False`）。
- **优势是序列级常数**：`compute_grpo_outcome_advantage` 把标量优势广播到所有 response token，与 GAE 的逐 token 优势形成对照；`returns` 与 `advantages` 相同且在 GRPO 下不被使用。
- **分组靠 `uid` + `repeat(interleave=True)`**：uid 必须在 repeat 之前赋值，`interleave=True` 保证 uid 与 `generate_sequences` 的连续 \(n\) 条回答对齐，否则分组错乱。
- **单样本兜底 `mean=0、std=1`**：避免除零，但也意味着 `n=1` 时归一化失效——这是 GRPO 必须 `rollout.n>1` 的根因。
- **KL 走 loss 端**：`use_kl_loss=True` 让 reward 不扣 KL（`token_level_rewards = token_level_scores`），改在 `update_policy` 里以 `kl_loss * kl_loss_coef`（默认 `low_var_kl`）加进策略损失，与组内归一化的相对优势尺度更协调。
- **启用 GRPO 的最小必改项**：`adv_estimator=grpo` + `use_kl_loss=True` + `rollout.n>1`，`kl_loss_coef`/`kl_loss_type` 用默认值即可。

## 7. 下一步学习建议

- 阅读 [u6-l1](./u6-l1-hybrid-actor-rollout-ref-worker.md) 与 [u6-l2](./u6-l2-actor-update.md)，看 `update_policy` 里这段 GRPO 损失是如何在 FSDP actor worker 上做 mini/micro batch 切分与梯度累积的。
- 回到 [u5-l4](./u5-l4-kl-controller-variants.md) 对比 `low_var_kl` 与朴素 `kl` 的方差差异，理解 GRPO 为何默认选低方差估计。
- 若想动手扩展，可尝试在 [u7-l3](./u7-l3-add-new-task.md) 的「自定义新任务」基础上，把自己新增的任务分别用 `gae` 与 `grpo` 跑一遍，对比「有 critic」与「无 critic、组内归一化」在收敛曲线和显存占用上的差异。
- 进阶思考：GRPO 的组内归一化在「同组所有回答分数都相同」时梯度为零（见 4.1.5 练习 1），可进一步研究 RLOO（leave-one-out）等变体如何缓解，但注意本仓库并未实现 RLOO，需参考上游 veRL 的更新版本。
