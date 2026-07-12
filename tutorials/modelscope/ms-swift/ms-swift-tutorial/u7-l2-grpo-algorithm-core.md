# GRPO 算法核心

## 1. 本讲目标

本讲深入 ms-swift 的强化学习算法核心层 `swift/rl_core/` 与 `swift/rlhf_trainers/grpo_trainer.py`，讲清 GRPO（Group Relative Policy Optimization）算法的三大支柱：**数据结构**、**奖励聚合**、**训练循环**。读完本讲你应当能够：

- 说出 `GRPOSample` / `GRPOBatch` 承载了哪些 RL 信号，以及 advantage（优势）是如何从奖励归一化得到的。
- 看懂一个 batch 的 completions 如何被多个奖励函数评分、聚合成 `[N, n_funcs]` 张量，再被加权融合为标量奖励。
- 描述 `GRPOTrainer` 的 rollout → score → batch → advantage → loss 五阶段训练循环，以及 PPO 风格的 clip 损失是怎么算出来的。

本讲承接 [u7-l1 RLHF 训练流程](./u7-l1-rlhf-training-pipeline.md)：上一讲搭好了 `SwiftRLHF` 管道与 `rlhf_type='grpo'` 的派发脚手架，本讲拆开 `GRPOTrainer` 内部的算法齿轮。

## 2. 前置知识

### 2.1 什么是 GRPO，为什么不用 PPO 的 critic

强化学习微调大模型（RLHF）的经典算法 PPO 需要训练一个**价值网络（critic / value model）**来估计每个状态的价值，从而计算 advantage \(A = r - V(s)\)。但大模型上 critic 几乎和策略模型一样大，显存翻倍、训练不稳。

GRPO（论文 [2402.03300](https://arxiv.org/abs/2402.03300)）的巧思是：**对同一个 prompt 采样 K 条不同的回答，用这 K 条奖励的组内均值/方差代替 critic**。于是 advantage 退化为组内相对优劣：

\[
A_i = \frac{r_i - \mathrm{mean}(r_{\text{group}})}{\mathrm{std}(r_{\text{group}}) + \epsilon}
\]

不需要 critic，显存省一半，这是 GRPO 能流行的根本原因。

### 2.2 三个张量维度的约定

本讲会反复出现三个维度，先约定清楚：

- `N`：一个 generation batch 里的**样本总数** = prompt 数 × K（每个 prompt 采 K 条）。
- `n_funcs`：**奖励函数个数**（可能同时有 accuracy、format 等多个打分函数，外加奖励模型）。
- `[B, T]`：collate 后的**batch × 序列长度**，用于模型前向。

奖励矩阵是 `[N, n_funcs]`，advantage 先算成 `[N]`（每条序列一个标量），再 expand 成 `[B, T]`（每个 token 一个值）。

### 2.3 关键术语速查

| 术语 | 含义 |
|------|------|
| rollout | 让当前策略对 prompt 生成回答（采样） |
| on-policy | 用当前策略自己采的样本训练自己 |
| reward function | 给一条回答打分的函数（规则/模型） |
| advantage | 「这条回答比同组平均水平好多少」，是策略梯度的权重 |
| importance sampling ratio | 新旧策略概率之比，用于多步复用同一批 rollout |
| KL penalty | 惩罚策略偏离参考模型，防止训飞 |

## 3. 本讲源码地图

本讲涉及四个核心文件，外加一个 mixin：

| 文件 | 作用 |
|------|------|
| [swift/rl_core/data.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rl_core/data.py) | 定义 `OnPolicySample`/`GRPOSample`/`GRPOBatch` 三大数据结构，是 RL 信号在流水线中的载体 |
| [swift/rl_core/grpo_algorithm.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rl_core/grpo_algorithm.py) | 纯算法层：`compute_rewards_per_func` 把 completions 评成 `[N, n_funcs]`，`score_completions` 聚合 |
| [swift/rl_core/advantage.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rl_core/advantage.py) | 纯张量层：`compute_advantages` 做组内归一化得到 advantage，以及 per-token 展开 |
| [swift/rlhf_trainers/grpo_trainer.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rlhf_trainers/grpo_trainer.py) | `GRPOTrainer`：编排 rollout/score/advantage/loss 的训练器主体 |
| [swift/rlhf_trainers/rollout_mixin.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rlhf_trainers/rollout_mixin.py) | `RolloutTrainerMixin`：提供 `_generate_and_score_completions` 五阶段骨架与 vLLM rollout 基础设施 |

一个关键设计原则：**算法（`rl_core/`）与训练器（`rlhf_trainers/`）解耦**。`rl_core/` 里都是不依赖 accelerate/transformers 的纯 PyTorch 函数，方便 HF/Megatron/Ray 三套后端复用；`GRPOTrainer` 只负责调度和数据搬运。

---

## 4. 核心概念与源码讲解

### 4.1 GRPOSample 与 advantage

#### 4.1.1 概念说明

GRPO 训练的一条「样本」远不止一问一答的文本，它还要携带：采样出的 token id、rollout 时的 logprob、是否被截断、奖励、advantage……ms-swift 用 `GRPOSample` 这个 dataclass 把这些字段打包成一个对象，沿着流水线流转。

`GRPOSample` 继承自更基础的 `OnPolicySample`。后者描述「一条 on-policy rollout 轨迹」的通用生命周期，前者只额外加了两个 GRPO 专属字段：`rewards` 和 `advantages`。这种「基类装通用信号、子类装算法专属信号」的设计，让 GKD 等其他算法可以复用同一个 `OnPolicySample`（见 `GKDSample`）。

到 batch 层面，`GRPOBatch` 收集的是模型前向需要的张量：`completion_mask`、`old_per_token_logps`、`ref_per_token_logps`、`advantages`……这些是从一堆 sample collate 出来的。

#### 4.1.2 核心流程

一条样本的生命周期（见 `OnPolicySample` 类文档字符串）：

```
1. 数据集 row        → messages + extra
2. rollout          → response_token_ids / rollout_logprobs / finish_reason
3. 重建 messages     → replace_assistant_response_with_ids
4. encode           → encoded = template.encode(self)
5. reward           → rewards_per_func [n_funcs]
6. advantage        → advantages [标量]
```

而 advantage 的计算（`compute_advantages`）是一段纯张量运算，流程为：

1. **加权融合奖励**：把 `[N, n_funcs]` 的每函数奖励按 `reward_weights` 加权求和，得到 `[N]` 的标量奖励。
2. **（可选）减去 KL 惩罚**：若 `kl_in_reward=True`，从奖励里减去 \(\beta \cdot \mathrm{KL}\)。
3. **组内去均值**：把 `[N]` reshape 成 `[num_prompts, K]`，每行减去该行均值——这就是 GRPO 的灵魂。
4. **（可选）除以组内 std**：`scale_rewards='group'` 时做尺度归一化。
5. 返回 `(advantages, rewards)` 两个 `[N]` 张量。

GRPO 的核心数学：

\[
\tilde{r}_i = r_i - \overline{r}_{\text{group}(i)}, \qquad
A_i = \frac{\tilde{r}_i}{\mathrm{std}(r_{\text{group}(i)}) + \epsilon}
\]

其中 \(\overline{r}_{\text{group}}\) 是同一 prompt 的 K 条回答的奖励均值。

#### 4.1.3 源码精读

**`GRPOSample` 只在 `OnPolicySample` 上加了两个字段**——`rewards`（可选镜像）与 `advantages`（advantage 计算后填入的 0 维张量）：

[swift/rl_core/data.py:297-301](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rl_core/data.py#L297-L301) — `GRPOSample` 定义，注释说明主路径用的是 `rewards_per_func` 张量而非 `rewards` 列表。

**`compute_advantages` 是 advantage 计算的总入口**，先做加权奖励融合：

[swift/rl_core/advantage.py:48-55](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rl_core/advantage.py#L48-L55) — `nansum` 忽略 NaN（某奖励函数对该样本不打分时），`view(-1, K)` 把奖励按 prompt 分组，`group_mean` 用 `repeat_interleave(K)` 广播回每条样本。

接着按 `advantage_estimator` 分支：GRPO 直接 `rewards - group_mean`，RLOO 用留一法（leave-one-out）的修正系数 \(K/(K-1)\)：

[swift/rl_core/advantage.py:57-60](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rl_core/advantage.py#L57-L60) — `rloo` 与默认（grpo/reinforce_plus_plus）两条分支。

随后是 std 归一化，`scale_rewards` 决定按 group、batch、none 还是 gdpo 计算尺度：

[swift/rl_core/advantage.py:68-87](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rl_core/advantage.py#L68-L87) — 注意 `grouped.std(dim=1)` 是按 prompt 组算标准差，`+1e-4` 防止除零；`scale_rewards='none'` 时跳过除法，advantage 只去均值不归一化。

> **设计要点**：`compute_advantages` 是个**纯函数**，docstring 明确「Input tensors should already be gathered across all processes」——跨进程的 all-gather 由调用方（`GRPOTrainer._compute_advantages`）提前做好。这让算法函数对分布式无感。

最后，advantage 还要从 per-sequence `[B]` 展开成 per-token `[B, T]` 才能进 loss。展开发生在 `expand_advantage_to_per_token`：

[swift/rl_core/advantage.py:251-279](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rl_core/advantage.py#L251-L279) — `unsqueeze(1).expand_as(completion_mask)` 把每条序列的标量 advantage 广播到所有 token；若有 teacher（OPD-RL 蒸馏），还会逐 token 叠加 teacher 的 signed log-ratio。

#### 4.1.4 代码实践

**实践目标**：手算一个最小例子的 advantage，验证对 `compute_advantages` 的理解。

**操作步骤**（源码阅读 + 手算型实践，无需 GPU）：

1. 打开 [swift/rl_core/advantage.py:10-89](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rl_core/advantage.py#L10-L89)。
2. 设想：1 个 prompt，K=4 条回答，单奖励函数（`n_funcs=1`），`reward_weights=[1.0]`，奖励 `r = [0.2, 0.8, 0.5, 0.5]`。
3. 手算：
   - 加权奖励 `rewards = [0.2, 0.8, 0.5, 0.5]`。
   - 组均值 `mean = 0.5`，去均值后 `[−0.3, 0.3, 0.0, 0.0]`。
   - 组 std（无偏估计，torch 默认）≈ `0.2646`，归一化后约 `[−1.13, 1.13, 0, 0]`。

**需要观察的现象**：组内奖励相同的两条回答（0.5, 0.5）advantage 恰为 0——这正是 GRPO「组内相对」的本质，绝对奖励高没用，要比同组平均好才有正 advantage。

**预期结果**：手算值与下面这段最小脚本（**示例代码**，非项目原有）的输出一致：

```python
import torch
from swift.rl_core.advantage import compute_advantages

rewards_per_func = torch.tensor([[0.2], [0.8], [0.5], [0.5]])  # [N=4, n_funcs=1]
reward_weights = torch.tensor([1.0])
advantages, rewards = compute_advantages(rewards_per_func, reward_weights,
                                         num_generations=4, scale_rewards='group')
print(advantages)   # 约 tensor([-1.133, 1.133, 0., 0.])
```

> 待本地验证：std 的有偏/无偏会带来约 3% 的数值差异（torch `.std()` 默认无偏）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `GRPOSample.rewards` 字段标注为「optional mirror」，主路径用张量而不用它？

**答案**：奖励在 batch 层面是 `[N, n_funcs]` 的稠密张量（`self._rewards_per_func`），需要跨进程 all-gather 后才能算 advantage；逐 sample 存一份 list 既冗余又不便做分布式归约，故只作可选镜像方便调试。

**练习 2**：把 `scale_rewards` 从 `'group'` 改成 `'none'`，advantage 会怎样变化？训练行为会如何不同？

**答案**：`'none'` 跳过 `advantages / (std + 1e-4)` 这一步，advantage 只去均值（如 `[−0.3, 0.3, 0, 0]`）。量纲更接近原始奖励绝对值，学习率的有效作用强度会随奖励尺度变化；`'group'` 归一化后 advantage 量纲稳定，对学习率更鲁棒，是 GRPO 默认推荐。

---

### 4.2 多奖励函数聚合

#### 4.2.1 概念说明

真实 RL 训练往往不只一个奖励信号：既想要「答案对」（accuracy），又想要「格式规整」（format），可能还接一个奖励模型（reward model）。ms-swift 把这些异质的打分者统一抽象成「奖励函数列表 `reward_funcs`」，用 `reward_weights` 控制各自权重，最终融合成一个标量奖励。

关键难点有三个：

1. **异构**：有的奖励函数是普通 Python 函数（同步），有的是 `async` 协程（如调外部 LLM-as-judge），有的是 `nn.Module`（判别式奖励模型）——执行方式完全不同。
2. **数据传递**：奖励函数需要的输入不仅是 completion 文本，往往还要 `solution`（标准答案）、`prompt_id` 等数据集透传列。
3. **聚合**：多个函数各打一分，要按权重合成。

`compute_rewards_per_func` 解决前两个，产出 `[N, n_funcs]` 矩阵；权重融合留给 advantage 阶段（`rewards = (rewards_per_func * reward_weights).nansum(dim=1)`）。

#### 4.2.2 核心流程

`compute_rewards_per_func` 的执行流程：

```
输入: samples[N], reward_funcs[n_funcs], reward_model_plugins[n_funcs]
1. 预分配 rewards_per_func = zeros([N, n_funcs])
2. 抽取 completions = [s.messages[-1]['content'] for s in samples]
3. 构造 reward_kwargs: trainer_state + 把 reward_rows 批量化(RowPreprocessor.rows_to_batched)
4. 逐函数遍历:
   - nn.Module  → 走 reward_model_plugin(inputs=reward_rows)
   - async 函数  → 先记录 index, 稍后 asyncio.gather 并发执行
   - 普通函数    → reward_func(completions, **reward_kwargs)
   每个返回值中 None 替换为 NaN
5. async 函数用 asyncio.run 并发跑完, 填回对应列
6. 检查: 若某行所有函数都返回 None, 打 warning
输出: rewards_per_func [N, n_funcs]
```

外层 `score_completions` 在此基础上，若开启了 gym 环境（多轮工具调用），会把环境返回的 `total_reward` 作为额外一列拼上去。

#### 4.2.3 源码精读

**`compute_rewards_per_func` 的签名与张量预分配**：

[swift/rl_core/grpo_algorithm.py:17-46](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rl_core/grpo_algorithm.py#L17-L46) — 注意 `completions` 取自每条样本 `messages[-1]['content']`（即最后一条 assistant 回答）；`rewards_per_func` 形状 `[len(samples), len(reward_funcs)]`。

**reward_kwargs 的构造是数据透传的关键**——它把每个 sample 的 `solution`/`target` 等列展平成 batched kwargs，让奖励函数能按名取用：

[swift/rl_core/grpo_algorithm.py:48-54](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rl_core/grpo_algorithm.py#L48-L54) — `to_reward_row()` 把 sample 的 `extra`（数据集透传列）拍平到顶层，再由 `RowPreprocessor.rows_to_batched` 转成每个值是长度 N 的列表。

**三类奖励函数的分发循环**：

[swift/rl_core/grpo_algorithm.py:56-78](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rl_core/grpo_algorithm.py#L56-L78) — 模型走 plugin、async 函数走 `asyncio.gather`、普通函数直接调用；`None → torch.nan` 保证不打分的位置在后续 `nansum` 中被忽略。

> **关键约束**：第 80-85 行的 NaN 全空检查——如果某条样本所有奖励函数都返回 None，advantage 会因 NaN 污染整个组。ms-swift 打 warning 提示「至少要有一个函数返回有效奖励」。

**`score_completions` 在 gym 场景下追加环境奖励列**：

[swift/rl_core/grpo_algorithm.py:104-118](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rl_core/grpo_algorithm.py#L104-L118) — `gym_reward` 从 `sample.rollout_infos['total_reward']` 取，`unsqueeze(1)` 后与函数奖励 `torch.cat` 拼成新的 `[N, n_funcs+1]`，从而 gym 奖励也能参与 `reward_weights` 加权。

**reward_weights 的初始化**在 trainer 构造期：

[swift/rlhf_trainers/grpo_trainer.py:2173-2179](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rlhf_trainers/grpo_trainer.py#L2173-L2179) — `_prepare_rewards` 里，若用户未指定 `reward_weights` 则默认全 1（等权）；并校验权重数必须等于奖励函数数（含 gym 列）。

#### 4.2.4 代码实践

**实践目标**：跟踪一个 batch 的 completions 如何被多个奖励函数评分并聚合成 `[N, n_funcs]`，再追踪 advantage 如何归一化。（本讲指定的实践任务）

**操作步骤**（源码阅读 + 跟踪型实践）：

1. 从训练器入口 `_compute_rewards_per_func` 读起：[swift/rlhf_trainers/grpo_trainer.py:322-365](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rlhf_trainers/grpo_trainer.py#L322-L365)。注意它在 `score_completions` 外包了一层分布式 `gather`（第 356-363 行），把各 rank 的局部奖励拼成全局 `[N, n_funcs]`。
2. 跟进 `score_completions` → `compute_rewards_per_func`，画出「completions → reward_kwargs → 各函数评分 → `[N, n_funcs]`」的数据流。
3. 再看 `_compute_advantages` 如何把这个矩阵喂给 `compute_advantages`：[swift/rlhf_trainers/grpo_trainer.py:441-460](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rlhf_trainers/grpo_trainer.py#L441-L460)。

**需要观察的现象**：

- `reward_kwargs` 经过 `rows_to_batched` 后，`solution` 这类列变成「长度 N 的列表」，奖励函数能像 `reward_func(completions, solution=[...], trainer_state=...)` 这样按名取参。
- 多个奖励函数的输出被逐列填入同一个 `[N, n_funcs]` 矩阵，列序与 `reward_funcs` 列表一致。
- advantage 阶段 `(rewards_per_func * reward_weights.unsqueeze(0)).nansum(dim=1)` 把 `[N, n_funcs]` 压成 `[N]`。

**预期结果**：能口述「一条 completion 先被 n_funcs 个函数各打一分填进矩阵的一行，再按权重融合成一个标量奖励，最后在同 prompt 组内去均值归一化成 advantage」。

#### 4.2.5 小练习与答案

**练习 1**：如果某奖励函数对部分样本返回 `None`（比如 format 函数只检查特定格式），训练会崩溃吗？

**答案**：不会。第 59/66/77 行把 `None` 替换为 `torch.nan`，随后 advantage 阶段用 `nansum` 聚合、`nanstd`/`nanmean` 算统计量，NaN 被自动忽略。仅当**一行全部为 NaN** 时才打 warning。

**练习 2**：gym 奖励为什么作为「额外一列」拼进矩阵，而不是单独处理？

**答案**：拼成同一矩阵后，gym 奖励和规则/模型奖励一样受 `reward_weights` 调节（`_prepare_rewards` 在第 2170 行为它追加名字 `'gym_reward'`），可以灵活配比（如 0.5×accuracy + 1.0×gym），统一走同一套 advantage 归一化路径，避免特例分支。

---

### 4.3 GRPOTrainer 训练循环

#### 4.3.1 概念说明

前两节讲清了「奖励怎么来」「advantage 怎么算」，本节把它们装进真正的训练循环。`GRPOTrainer` 继承自 `RolloutTrainerMixin + SwiftMixin + HFGRPOTrainer`（TRL 的 GRPOTrainer），ms-swift 重写了绝大部分核心逻辑。

整个循环的灵魂是一个**固定顺序的五阶段骨架** `_generate_and_score_completions`，它定义在 mixin 里、被 GRPO 和 GKD 共享。每个阶段都是可覆写的钩子，GRPO 通过覆写这些钩子注入自己的行为。这种「骨架固定 + 钩子可覆写」是 ms-swift RL 训练器复用的核心模式。

五阶段：

1. **rollout**：原始数据 → 样本 + 生成回答。
2. **score**：奖励函数打分（+ DAPO 动态采样）。
3. **prepare batch**：编码 + collate 成模型前向输入。
4. **postprocess**：算 advantage 并写进 batch。
5. **log**：记录 prompt/completion/指标。

最后由 HF Trainer 的 `training_step` 调用 `compute_loss` 完成 PPO 风格的 clipped 策略梯度更新。

#### 4.3.2 核心流程

一轮训练（一个 generation batch）的时序：

```
HF Trainer.training_step
  └─ _prepare_inputs(inputs)                      # 缓冲 + 切片
       └─ (每 generate_every 步触发一次)
          _generate_and_score_completions(inputs)  # 五阶段骨架
            1. _rollout_samples     → samples (含生成回答)
            2. _score_completions   → self._rewards_per_func [N, n_funcs]
                 └─ _compute_rewards_per_func → score_completions
                 └─ (可选) _dynamic_sampling   # DAPO 重采样 std=0 的组
            3. _prepare_batch_inputs → batch_encoded_inputs
                 ├─ encode_sample + collate_to_grpo_micro_batch
                 └─ 算 old/ref/teacher per_token_logps (no_grad)
            4. _postprocess_batch   → 算 advantage 写入 grpo_batch.advantages
                 ├─ _compute_advantages (grouped / request-aware)
                 └─ expand_advantage_to_per_token  # [B] → [B, T]
            5. _log_rollout
       返回当前 accumulation step 的 micro_batch
  └─ compute_loss(model, inputs)
       └─ _compute_loss_and_metrics
            ├─ per_token_logps (当前策略, 有梯度)
            ├─ log_ratio = per_token_logps - old_per_token_logps
            ├─ coef_1 = exp(log_ratio)            # 重要性采样比
            ├─ coef_2 = clamp(coef_1, 1-ε, 1+ε)   # PPO clip
            ├─ per_token_loss = -min(coef_1*A, coef_2*A)
            └─ (+ β·KL)  (+ rollout IS 修正)  ...
```

PPO clip 损失的数学（逐 token）：

\[
\rho_t = \frac{\pi_\theta(y_t)}{\pi_{\theta_{\text{old}}}(y_t)}, \qquad
L_t = -\min\big(\rho_t A_t,\ \mathrm{clip}(\rho_t,\, 1-\epsilon,\, 1+\epsilon)\, A_t\big)
\]

其中 \(\rho_t\) 由 `coef_1 = exp(log_ratio)` 实现，`A_t` 是 per-token advantage。

#### 4.3.3 源码精读

**五阶段骨架**（这是理解整个训练循环的「目录」）：

[swift/rlhf_trainers/rollout_mixin.py:122-145](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rlhf_trainers/rollout_mixin.py#L122-L145) — `_generate_and_score_completions` 固定五步顺序，每步对应一个可覆写钩子；docstring 明确「per-algorithm behavior lives in the hooks」。

**`_prepare_inputs` 负责缓冲复用**——同一批 rollout 会被多个 gradient accumulation step 复用，避免重复生成：

[swift/rlhf_trainers/grpo_trainer.py:204-213](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rlhf_trainers/grpo_trainer.py#L204-L213) — `_step % generate_every == 0` 时才重新生成，否则从 `_buffered_inputs` 取第 `_step % num_rollout_samples` 个 micro_batch。

**score 阶段：`_score_completions` 把奖励暂存到 `self._rewards_per_func`**（因为 advantage 依赖后续编码出的 batch，要延迟到 postprocess 才算）：

[swift/rlhf_trainers/grpo_trainer.py:223-237](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rlhf_trainers/grpo_trainer.py#L223-L237) — 动态采样（DAPO）开启时，会用 `_dynamic_sampling` 替换奖励方差为 0 的组（std=0 的组 advantage 全 0，学不到东西）。

**postprocess 阶段把 advantage 展开 per-token 写入 batch**：

[swift/rlhf_trainers/grpo_trainer.py:244-275](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rlhf_trainers/grpo_trainer.py#L244-L275) — 先 `_compute_advantages` 得 `[N]`，再 `expand_advantage_to_per_token` 展开成 `[B, T]` 赋给 `grpo_batch.advantages`。

**loss 计算的核心——重要性采样比与 clip**：

[swift/rlhf_trainers/grpo_trainer.py:996-1046](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rlhf_trainers/grpo_trainer.py#L996-L1046) — `log_ratio = per_token_logps - old_per_token_logps`；`importance_sampling_level` 控制 token 级还是 sequence 级（GSPO）；`coef_1 = exp(log_ratio)` 即 \(\rho_t\)；`coef_2 = clamp(coef_1, 1-ε_low, 1+ε_high)`；`per_token_loss = -min(coef_1*A, coef_2*A)` 正是 PPO clip。

**多种 loss 归一化策略**（grpo/bnpo/dr_grpo/dapo/cispo/sapo/real/fipo）：

[swift/rlhf_trainers/grpo_trainer.py:1070-1077](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rlhf_trainers/grpo_trainer.py#L1070-L1077) — `grpo` 按「每条序列 token 平均」再 batch 平均；`bnpo` 直接按总 token 平均（更省、更适合长序列）；`dapo` 用全局 `num_items_in_batch` 归一。

#### 4.3.4 代码实践

**实践目标**：跑通一个真实 GRPO 训练，观察 reward / advantage / KL 指标随训练的变化。

**操作步骤**：

1. 参考 [examples/train/grpo/external/grpo_7b.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/train/grpo/external/grpo_7b.sh)。该脚本用 `Qwen2.5-7B-Instruct` + `accuracy` 奖励 + vLLM server rollout，`num_generations=8`、`beta=0.04`。
2. 资源不足时缩成最小版（**示例命令**，需按你的显存调整）：

   ```bash
   NPROC_PER_NODE=1 swift rlhf \
     --rlhf_type grpo --model Qwen/Qwen2.5-1.5B-Instruct \
     --reward_funcs accuracy --use_vllm true --vllm_mode colocate \
     --dataset AI-MO/NuminaMath-TIR#200 \
     --max_completion_length 1024 --num_generations 4 \
     --per_device_train_batch_size 2 --gradient_accumulation_steps 2 \
     --learning_rate 1e-6 --num_train_epochs 1 \
     --log_completions true --beta 0.04
   ```

3. 训练时另开终端 `tail -f` 输出目录下的 `completions.jsonl`。

**需要观察的现象**：

- `completions.jsonl` 每行包含 `prompt`/`completion`/各奖励函数分/`advantages`——直接印证「同一 prompt 的 K 条 completion 各自带 advantage」。
- tensorboard/wandb 上 `reward`、`reward_std`、`kl`、`clip_ratio/region_mean`、`completions/mean_length` 曲线。
- `frac_reward_zero_std`：奖励方差为 0 的组占比，偏高说明奖励函数区分度不足（此时 DAPO 的动态采样会介入）。

**预期结果**：随训练推进 `reward` 均值上升、`kl` 缓慢上升（受 `beta` 约束不会爆）、`clip_ratio` 维持个位数百分比。

> 待本地验证：单卡 + colocate vLLM 至少需要 ~24G 显存跑 1.5B 模型；若显存不足可改 `--use_vllm false`（回退到 TransformersEngine rollout，慢但省显存）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_prepare_inputs` 要用 `_buffered_inputs` 缓存，而不是每个 gradient accumulation step 都重新 rollout？

**答案**：rollout（采样 K 条回答）是 GRPO 最贵的操作。当 `num_iterations > 1` 或 `gradient_accumulation_steps` 不能整除 `steps_per_generation` 时，同一批 rollout 会被复用多次做策略更新（这正是 `old_per_token_logps` 存在的原因——它锚定采样时刻的策略，重要性采样比据此计算）。缓存避免重复生成。

**练习 2**：`coef_1` 和 `coef_2` 在 loss 里分别起什么作用？为什么取 `min`？

**答案**：`coef_1 = exp(log_ratio)` 是真实的重要性采样比 \(\rho_t\)，`coef_2` 是把它 clip 到 \([1-\epsilon, 1+\epsilon]\) 后的值。取 `min` 是 PPO 的 pessimistic bound：当 advantage 为正时取较小的 clip 值（限制正向增益），为负时取较小的原始值（限制负向增益），从而把策略更新约束在信任域内，防止一两步就把策略改崩。

---

## 5. 综合实践

把三节知识串起来：**手画一张 GRPO 一步训练的数据流图，并标注每段张量形状与对应的源码函数**。

任务：

1. 画一条从「dataset row」到「`loss.backward()`」的完整数据流，至少包含以下节点，并标出张量形状：
   - `OnPolicySample` / `GRPOSample`（per-sample）
   - `_compute_rewards_per_func` 产出的 `rewards_per_func [N, n_funcs]`
   - `gather` 后的全局 `rewards_per_func`
   - `compute_advantages` 产出的 `advantages [N]`
   - `GRPOBatch.advantages [B, T]`
   - `compute_loss` 里的 `log_ratio`、`coef_1`、`per_token_loss [B, T]`、`loss [标量]`
2. 在每个节点旁标注它由哪个文件的哪个函数产生（引用本讲给出的永久链接）。
3. 用一句话解释「为什么 advantage 在 score 阶段算不出来、必须延迟到 postprocess 阶段」。

参考答案要点：

- advantage 依赖组内统计（mean/std），而组是跨进程的，必须先 `_compute_rewards_per_func` 做 `gather` 得到全局 `[N, n_funcs]`；同时 advantage 要展开成 per-token 写进 `grpo_batch`，而 `grpo_batch` 是在 prepare batch 阶段编码 collate 后才存在的——所以 score 阶段只能把奖励暂存到 `self._rewards_per_func`，延迟到 postprocess 再算。
- 关键形状变化：`[N, n_funcs]` →（加权 nansum）→ `[N]` →（组内归一化）→ `[N]` →（按 micro_batch 切分 + expand）→ `[B, T]`。

> 这是纯源码阅读型实践，无需 GPU，重点检验你是否能把「数据结构—奖励聚合—advantage—loss」四段拼成一条连贯的链路。

## 6. 本讲小结

- **GRPOSample/GRPOBatch 是 RL 信号的载体**：`OnPolicySample` 装通用 rollout 信号，`GRPOSample` 加 `rewards`/`advantages`；`GRPOBatch` 收集 collate 后的 batch 级张量（`completion_mask`、`old/ref_per_token_logps`、`advantages`）。
- **advantage = 组内相对优劣**：`compute_advantages` 先加权融合 `[N, n_funcs]` → `[N]`，再按 prompt 组（K 条）去均值、除 std，是 GRPO 免 critic 的关键；它是个纯张量函数，分布式 gather 由调用方提前做。
- **多奖励聚合产出 `[N, n_funcs]`**：`compute_rewards_per_func` 统一处理同步函数、async 函数、判别式奖励模型三类异质打分者，`None→NaN` + `nansum` 实现缺省容错；gym 奖励作为额外一列拼接以共享 `reward_weights` 机制。
- **训练循环是固定五阶段骨架**：`_generate_and_score_completions`（rollout→score→prepare batch→postprocess→log），per-algorithm 行为藏在可覆写钩子里，advantage 因依赖全局奖励和编码 batch 而延迟到 postprocess 计算。
- **loss 是 PPO clip**：`coef_1=exp(log_ratio)` 是重要性采样比，`coef_2` 是 clip，`-min(coef_1·A, coef_2·A)` 构成悲观信任域约束，叠加可选的 KL 惩罚与 rollout IS 修正。
- **算法与训练器解耦**：`rl_core/` 是纯 PyTorch 可被 HF/Megatron/Ray 三后端复用，`GRPOTrainer` 只做调度与数据搬运。

## 7. 下一步学习建议

- 继续向「奖励侧」深入，读 [u7-l3 奖励函数与 RM 插件](./u7-l3-reward-functions-and-rm-plugin.md)，理解 `swift/rewards/orm.py`/`prm.py` 如何实现具体的 accuracy/format 等奖励，以及 `DefaultRMPlugin` 如何把判别式奖励模型接入本讲的 `compute_rewards_per_func`。
- 若关心多轮工具调用训练，进入 [u7-l4 多轮 Rollout 与环境交互](./u7-l4-multi-turn-rollout-and-env.md)，看 `RolloutScheduler` 如何把 `gym_env` 的 `total_reward` 回填成本讲的 gym 奖励列。
- 想理解 advantage 之外的高级算法变体（DAPO 动态采样、RLOO、GSPO 序列级 IS、OPD-RL teacher 蒸馏），精读 [swift/rl_core/advantage.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rl_core/advantage.py) 与 [grpo_trainer.py 的 `_prepare_algorithm_params`](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rlhf_trainers/grpo_trainer.py#L2064-L2113)，每个变体都对应论文链接与一组参数开关。
