# PPO：Actor-Critic 与 GAE 优势估计

## 1. 本讲目标

本讲是强化学习后训练单元的第五讲，承接 [u7-l4 GRPO 与 CISPO](u7-l4-grpo-and-cispo.md)。GRPO 用「同一问题的 N 条回答做组内归一化」来代替价值网络，本讲则回到强化学习最经典的基线方法——**PPO（Proximal Policy Optimization）**，它走的是另一条路：**显式训练一个 Critic 价值网络来估计每个状态的价值，用 GAE 计算优势**。

学完本讲你应该能够：

1. 说清 PPO 的 **Actor-Critic 双网络**结构：Actor 生成回答、Critic 给每个 token 打价值分，以及 MiniMind 如何用一个 `CriticModel` 复用主干、把 `lm_head` 换成 `value_head`。
2. 手推 **GAE（Generalized Advantage Estimation）**的递推公式，并能在源码里逐行对应：终局奖励如何被 `γ`、`λ` 倒序传播到每一个 token。
3. 读懂 `ppo_train_epoch` 的 **多轮更新（`ppo_update_iters`）+ 小批量（`mini_batch`）+ `early_stop_kl` 早停**机制，以及策略 / 价值**双损失 + 双优化器**的写法。
4. 对照 README 解释 PPO 的两个工程现象：**为什么 reward 提升缓慢**（Actor 依赖 Critic，两者相互耦合）、**为什么显存约为单网络方法的 1.5–2 倍**（多了一个 Critic + 一个冻结的 ref）。

---

## 2. 前置知识

在进入源码前，先用直觉建立几个关键概念。本讲默认你已读完 u7-l1 ~ u7-l4，熟悉 rollout 引擎、奖励信号与 GRPO 的统一 PO 视角。

### 2.1 状态、动作与价值函数

把大模型生成回答的过程看成「下棋」：

- **状态 \(s_t\)**：到第 \(t\) 个 token 为止的上下文（prompt + 已生成部分）。
- **动作 \(a_t\)**：在这一步选择哪个 token。
- **奖励 \(r_t\)**：这一步拿到的外部打分。RLHF 里奖励通常是**稀疏**的——只有回答结束时才由奖励模型给一个总分，中间每步 \(r_t=0\)。
- **价值函数 \(V(s_t)\)**：Critic 网络对「从状态 \(s_t\) 往后走，平均能拿多少累计奖励」的**估计值**。

### 2.2 优势（Advantage）：好多少还是差多少

**优势 \(A_t\)** 衡量「动作 \(a_t\) 比平均水平好多少」：

\[
A_t = R_t - V(s_t)
\]

其中 \(R_t\) 是实际拿到的（折扣）累计回报，\(V(s_t)\) 是 Critic 估计的基线。

- \(A_t > 0\)：这一步比预期好 → **增大**这个动作的概率。
- \(A_t < 0\)：比预期差 → **减小**它的概率。

GRPO 用「组内均值」当基线（不需要 V），PPO 则用 Critic 给出的 \(V(s_t)\) 当基线。这是两条路线的根本差异。

### 2.3 GAE：在偏差与方差之间插值

优势 \(A_t = R_t - V(s_t)\) 里的 \(R_t\) 怎么算？最朴素的两种：

- **蒙特卡洛（MC）**：把后面所有实际奖励加起来。无偏但**方差大**（噪声多）。
- **一步 TD**：用 \(r_t + \gamma V(s_{t+1})\) 代替真实回报，方差小但**有偏**（依赖 Critic 估得准不准）。

GAE 用一个参数 \(\lambda \in [0,1]\) 在两者之间插值，\(\gamma\) 是折扣因子（对未来奖励打几折）：

\[
\delta_t = r_t + \gamma V(s_{t+1}) - V(s_t) \qquad \text{（TD 误差，一步优势估计）}
\]

\[
A_t^{\text{GAE}(\gamma,\lambda)} = \sum_{l=0}^{\infty} (\gamma\lambda)^l \, \delta_{t+l}
\]

它的等价递推形式（代码里用的就是这条）：

\[
A_t = \delta_t + \gamma\lambda \cdot A_{t+1}, \qquad A_{\text{末尾}} = 0
\]

- \(\lambda=0\)：退化为一步 TD，\(A_t=\delta_t\)。
- \(\lambda=1\)：退化为蒙特卡洛。

MiniMind 默认 \(\gamma=1.0\)、\(\lambda=0.95\)，即几乎不打折未来奖励、\(\lambda\) 接近 1，更偏向蒙特卡洛。

### 2.4 PPO 的「近端」二字

PPO 在更新策略时，会用「重要性比率」\(r_t = \exp(\log\pi_\theta - \log\pi_{\text{old}})\) 衡量新旧策略的偏离程度，并用 **clip** 把它限制在 \([1-\varepsilon, 1+\varepsilon]\) 内，防止一次更新把策略改得太猛（这正是 [u7-l4](u7-l4-grpo-and-cispo.md) 提到 CISPO 想绕开的「clip 截断梯度」问题）。这部分逻辑 PPO 与 GRPO 共用，本讲重点放在 **Critic + GAE** 这条 PPO 独有的支线上。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [trainer/train_ppo.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_ppo.py) | PPO 训练全流程：`CriticModel`、`calculate_rewards`、`ppo_train_epoch`、`__main__` 装配 |
| [trainer/rollout_engine.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/rollout_engine.py) | 训推分离引擎：`rollout` 在线采样、`compute_per_token_logps` 回算 old logp（u7-l2 已讲） |
| [model/model_minimind.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py) | `MiniMindModel`（躯干）、`MiniMindForCausalLM`（外壳），`CriticModel` 继承自后者 |
| [trainer/trainer_utils.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/trainer_utils.py) | `init_model`、`LMForRewardModel`（u7-l3 已讲） |
| [README.md](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md) | PPO 损失公式与「reward 提升缓慢 / 显存 1.5–2 倍」的工程讨论 |

PPO 的整体数据流（一张图先记住）：

```
prompt ──► rollout 引擎 ──► response + old_logp
                                  │
                          calculate_rewards (RM + 规则)
                                  │  稀疏终局奖励 R
                                  ▼
        Critic ──► V(s) ──► GAE ──► 优势 A、回报 returns
                                  │
        ref 模型 ──► ref_logp       │
                                  ▼
   多轮 PPO 更新（policy clip 损失 + clipped value 损失 + KL）
   双优化器：actor_optimizer + critic_optimizer
```

---

## 4. 核心概念与源码讲解

### 4.1 CriticModel：Actor-Critic 双网络与价值头

#### 4.1.1 概念说明

PPO 是 **Actor-Critic** 架构：两个网络协同工作。

- **Actor（策略网络）**：就是我们的语言模型本身（`MiniMindForCausalLM`），输入上下文，输出「下一个 token 的概率分布」\(\pi_\theta\)。它负责**生成回答**。
- **Critic（价值网络）**：输入同一个上下文，输出一个**标量**——对这个状态的价值估计 \(V(s)\)。它负责**评价**「从这里开始，预期能拿多少奖励」。

GRPO 把 Critic 砍掉、用组内均值代替基线；PPO 则保留它。代价是要多训练一个网络、多占显存，收益是**每一步都能拿到一个基线**，不必凑齐 N 条回答才能算优势。

MiniMind 的做法很省事：Critic 不从零设计，而是**复用语言模型的主干**，只把输出端的 `lm_head`（维度 `[hidden, vocab=6400]`，投影到词表）换成一个 `value_head`（维度 `[hidden, 1]`，投影到单个标量）。两者共享几乎全部参数结构，只是最后一步投影不同。

#### 4.1.2 核心流程

1. `CriticModel` 继承自 `MiniMindForCausalLM`，构造时调用 `super().__init__()`，于是它天然拥有 `self.model`（Transformer 主干）和 `self.lm_head`。
2. 额外加一个 `self.value_head = nn.Linear(hidden_size, 1)`。
3. `forward` 只跑主干 + 末层 norm + `value_head`，输出形状 `[B, seq_len]`——**序列里每个位置一个价值估计**。
4. 由于加载的是 `full_sft` 权重（含 `lm_head` 不含 `value_head`），用 `strict=False` 加载：主干权重复用，`value_head` 随机初始化、需要从头训练。

#### 4.1.3 源码精读

CriticModel 的完整定义（替换 lm_head 为单值输出层）见 [trainer/train_ppo.py:36-49](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_ppo.py#L36-L49)：

```python
class CriticModel(MiniMindForCausalLM):
    def __init__(self, params):
        super().__init__(params)
        # 替换lm_head为输出单一价值的线性层
        self.value_head = nn.Linear(params.hidden_size, 1)

    def forward(self, input_ids=None, attention_mask=None, **kwargs):
        outputs = self.model(input_ids=input_ids, attention_mask=attention_mask, **kwargs)
        hidden_states = self.model.norm(outputs[0])
        values = self.value_head(hidden_states).squeeze(-1)
        return values
```

对应的主干定义（`MiniMindModel`，注意 `forward` 末尾已有一次 `self.norm`）见 [model/model_minimind.py:196-232](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L196-L232)，外壳（`self.model` + `self.lm_head` + tie）见 [model/model_minimind.py:234-243](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L234-L243)。

> **细节提示（不展开为练习，但要知道）**：`MiniMindModel.forward` 在第 230 行已经对 `hidden_states` 做过一次 `self.norm`，而 `CriticModel.forward` 又对 `outputs[0]` 再做了一次 `self.model.norm`。这相当于对主干输出做了两次 RMSNorm。这不影响训练跑通（RMSNorm 只是缩放），但属于阅读时值得留意的实现细节。

`__main__` 里三模型 + 奖励模型的装配见 [trainer/train_ppo.py:366-377](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_ppo.py#L366-L377)：

```python
actor_model, tokenizer = init_model(lm_config, base_weight, device=args.device)   # 1. Actor
ref_model, _ = init_model(lm_config, base_weight, device=args.device)             # 2. ref（冻结）
ref_model = ref_model.eval().requires_grad_(False)
...
critic_model = CriticModel(lm_config)                                             # 3. Critic
critic_model.load_state_dict(state_dict, strict=False)                            #    复用 full_sft 主干
reward_model = LMForRewardModel(args.reward_model_path, ...)                      # 4. 奖励模型
```

四个模型各司其职：**Actor 被更新**、**ref 冻结只提供 KL 锚点**、**Critic 被更新**、**奖励模型冻结只打分**。注意 `critic_model.load_state_dict(..., strict=False)`——`full_sft_*.pth` 里有 `lm_head.*` 但没有 `value_head.*`，`strict=False` 容许这种「键不完全匹配」，主干权重复用、`value_head` 用随机值起步。这正是 PPO 显存约为「单网络方法 1.5–2 倍」的直接来源：同时驻留 Actor + Critic（可训练）+ ref（冻结）+ 奖励模型。

#### 4.1.4 代码实践

**实践目标**：直观感受 Critic 与 Actor 的输出差异，并验证 `strict=False` 加载后 `value_head` 确实是随机初始化。

**操作步骤**（「待本地验证」——需在装好 torch 的环境里、`cd trainer` 后运行）：

1. 写一段最小脚本，构造一个 `CriticModel`，喂入一段假 `input_ids`，打印输出形状。
2. 对比 `CriticModel` 和普通 `MiniMindForCausalLM` 在同一输入下输出张量的形状区别。

```python
# 示例代码：仅作形状验证，非项目原有脚本
import torch
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM
from trainer.train_ppo import CriticModel

cfg = MiniMindConfig(hidden_size=768, num_hidden_layers=8)
critic = CriticModel(cfg).eval()
ids = torch.randint(0, cfg.vocab_size, (2, 16))          # [B=2, T=16]
with torch.no_grad():
    values = critic(input_ids=ids)                        # Critic 输出
print("Critic 输出形状:", values.shape)                   # 期望: torch.Size([2, 16])
print("value_head 是否随机:", critic.value_head.weight.std().item())  # 应为一个非零随机值

lm = MiniMindForCausalLM(cfg).eval()
with torch.no_grad():
    logits = lm(input_ids=ids).logits
print("Actor logits 形状:", logits.shape)                 # 期望: torch.Size([2, 16, 6400])
```

**需要观察的现象**：Critic 输出是 `[2, 16]`（每个位置一个标量价值），Actor 的 logits 是 `[2, 16, 6400]`（每个位置一个词表分布）。两者主干完全同构，只差最后一层投影。

**预期结果**：形状分别符合 `[B, T]` 与 `[B, T, vocab]`；`value_head.weight` 的标准差非零（随机初始化），证明它没从 `full_sft` 继承任何权重。

#### 4.1.5 小练习与答案

**练习 1**：既然 Critic 复用了语言模型主干，为什么不能直接用 Actor 自己的前向输出去估价值，非要再开一个网络？

**答案**：因为两者的**训练目标不同**。Actor 的末端是 `lm_head`，被训练去预测下一个 token（输出词表分布）；Critic 的末端是 `value_head`，被训练去回归累计奖励（输出标量）。共用主干会导致梯度互相打架，且价值估计需要独立的表示空间，所以必须用两个独立的「输出头」（甚至各自的主干权重）分别优化。

**练习 2**：`CriticModel.load_state_dict(state_dict, strict=False)` 中，为什么必须用 `strict=False`？

**答案**：`state_dict` 来自 `full_sft_*.pth`，它的键是语言模型的（含 `lm_head.weight`，不含 `value_head.*`）。`CriticModel` 多了 `value_head`、且不再使用 `lm_head`。若 `strict=True`，PyTorch 会因「键缺失（value_head）」和「键多余（lm_head）」直接报错；`strict=False` 容许这种不匹配，让主干权重照常加载、`value_head` 保持随机初始化。

---

### 4.2 GAE 优势估计：把终局奖励分摊到每个 token

#### 4.2.1 概念说明

RLHF 的奖励是**稀疏**的：一条回答无论多长，奖励模型只在结尾给一个总分 \(R\)，中间每个 token 的 \(r_t=0\)。但 PPO 要对**每个 token** 算一个优势 \(A_t\) 来指导更新——否则没法做 token 级的策略梯度。

这就引出两个问题：
1. **怎么把一个终局总分分摊到每个 token？** —— 靠 GAE 的倒序递推。
2. **每个 token 的「基线」从哪来？** —— 靠 Critic 给每个位置输出一个 \(V(s_t)\)。

GAE 的核心是第 2.3 节的递推式 \(A_t = \delta_t + \gamma\lambda \cdot A_{t+1}\)，它把「TD 误差」沿时间倒序传播：末尾拿到真实奖励的 token 把信号一步步往前递减地推给更早的 token。MiniMind 把外部奖励 \(R\) 只放在回答的**最后一个有效 token** 上，其余位置 \(r_t=0\)，然后让 GAE 自动把它摊开。

#### 4.2.2 核心流程

设回答长度为 `gen_len`，Critic 给出每个位置的 `old_resp_values`（即 \(V(s_t)\)），奖励 \(R\) 放在末位 `token_rewards`：

1. 倒序遍历 \(t = \text{gen_len}-1, \dots, 0\)。
2. 取下一状态价值 \(V(s_{t+1})\)：非末位取 `old_resp_values[:, t+1]`，末位取 0（终态无未来）。
3. 算 TD 误差：\(\delta_t = r_t + \gamma V(s_{t+1}) - V(s_t)\)。
4. 递推优势：\(A_t = \delta_t + \gamma\lambda \cdot A_{t+1}\)，其中 \(A_{t+1}\) 初始为 0。
5. 全部算完后得到 `advantages`（形状 `[B, gen_len]`）。
6. **价值回归目标**：`returns = advantages + old_resp_values`（即 \(R_t = A_t + V(s_t)\)），它就是 Critic 要拟合的标签。
7. 对优势做**按 batch 归一化**（减均值、除标准差），这是 PPO 工程上的稳定技巧。

数学上，第 3、4 步对应：

\[
\delta_t = r_t + \gamma V(s_{t+1}) - V(s_t)
\]

\[
A_t = \delta_t + \gamma\lambda \cdot A_{t+1}, \qquad A_{\text{gen\_len}} = 0
\]

#### 4.2.3 源码精读

GAE 全程在一个 `torch.no_grad()` 块里完成（[trainer/train_ppo.py:130-151](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_ppo.py#L130-L151)），因为 rollout 阶段的 `old_logp`、`old_values` 都是**固定参照量**，不需要梯度。

先看稀疏奖励的放置（只在最后一个有效 token 加外部奖励）见 [trainer/train_ppo.py:136-138](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_ppo.py#L136-L138)：

```python
token_rewards = torch.zeros_like(old_resp_logp)
last_idx = resp_lengths - 1  # [B]
token_rewards[torch.arange(B, device=args.device)[valid_resp], last_idx[valid_resp]] += rewards[valid_resp]  # 末尾加外部奖励
```

GAE 递推主循环见 [trainer/train_ppo.py:140-147](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_ppo.py#L140-L147)：

```python
gen_len = old_resp_values.size(1); lastgaelam = torch.zeros(B, device=args.device); advs_rev = []
for t in reversed(range(gen_len)):
    nv = old_resp_values[:, t + 1] if t < gen_len - 1 else 0.0                    # V(s_{t+1})，末位为 0
    delta = token_rewards[:, t] + args.gamma * nv - old_resp_values[:, t]         # δ_t
    lastgaelam = delta + args.gamma * args.lam * lastgaelam                       # A_t = δ_t + γλ·A_{t+1}
    advs_rev.append(lastgaelam)
advantages = torch.stack(advs_rev[::-1], dim=1)  # [B, R]（倒序收集后再翻正）
returns = advantages + old_resp_values            # [B, R] 价值回归目标
```

注意 `advs_rev[::-1]`：循环是倒序 append 的（先算末位），最后要翻转回正常时间顺序。

紧接其后的优势归一化见 [trainer/train_ppo.py:149-151](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_ppo.py#L149-L151)：

```python
adv_mean = (advantages * resp_policy_mask).sum() / resp_policy_mask.sum().clamp(min=1)
adv_var  = ((advantages - adv_mean) ** 2 * resp_policy_mask).sum() / resp_policy_mask.sum().clamp(min=1)
advantages = (advantages - adv_mean) * torch.rsqrt(adv_var + 1e-8) * resp_policy_mask
```

`resp_policy_mask` 用来只在有效 token 上统计均值和方差（排除 padding 与首 EOS 之后的位置），归一化后再乘回 mask 把无效位置清零。

#### 4.2.4 代码实践

**实践目标**：脱离大模型，用一个最小例子手算 GAE，验证自己对递推式的理解与代码一致。

**操作步骤**：把下面这段「示例代码」跑一遍，它复刻了源码 GAE 循环的语义（单条序列、固定 V）：

```python
# 示例代码：最小 GAE 验证，非项目原有脚本
import torch

gamma, lam = 1.0, 0.95
values = torch.tensor([1.0, 1.2, 0.8, 0.5])   # V(s_0..s_3)，长度 4
rewards = torch.tensor([0.0, 0.0, 0.0, 2.0])  # 只在末位放奖励 R=2

gen_len = values.size(0)
lastgaelam = 0.0
advs_rev = []
for t in reversed(range(gen_len)):
    nv = values[t + 1] if t < gen_len - 1 else 0.0
    delta = rewards[t] + gamma * nv - values[t]
    lastgaelam = delta + gamma * lam * lastgaelam
    advs_rev.append(lastgaelam)
advantages = torch.stack(advs_rev[::-1])
returns = advantages + values
print("advantages:", advantages)   # 末位 = R - V(s_3) = 2 - 0.5 = 1.5；往前逐级递推
print("returns:", returns)         # = advantages + values
```

**需要观察的现象**：末位优势 \(A_3 = 2.0 - 0.5 = 1.5\)；倒数第二位 \(A_2 = (0.5 - 0.8) + 1.0\times0.95\times1.5\)，以此类推，越靠前的 token 优势越被 \(\lambda\) 衰减地传递信号。

**预期结果**：`advantages` 末位约为 `1.5`，且整体随位置前移而变化；`returns` 每个位置都接近一个围绕真实奖励的值。若手算与程序一致，说明你已掌握 GAE 递推。

**（可选进阶）**：把 `lam` 改成 `0.0` 再跑一次，应观察到 `advantages` 恰好等于一步 TD 误差 \(\delta_t\)，印证「\(\lambda=0\) 退化为一步 TD」。

#### 4.2.5 小练习与答案

**练习 1**：为什么奖励只放在最后一个 token，而不是均匀分给每个 token？

**答案**：因为奖励模型对**整条回答**打一个总分，它并不知道哪个 token 贡献了多少。把总分均匀分配是人为假设、不一定正确；GAE 的做法是先放在末位、再由 Critic 的价值函数 \(V(s_t)\) 配合递推**让模型自己学**如何把功劳分摊回每个 token——这比手工分配更合理，且随 Critic 训练得越来越好而自适应。

**练习 2**：`returns = advantages + old_resp_values` 这个 `returns` 给谁用？

**答案**：给 **Critic** 当回归标签。Critic 的价值损失就是 \( (V_\theta(s_t) - \text{returns}_t)^2 \)。因为 \(A_t = R_t - V_{\text{old}}(s_t)\)，所以 \(R_t = A_t + V_{\text{old}}(s_t) = \text{returns}_t\)，即「优势 + 旧价值」正好是估计的真实回报，正是 Critic 要拟合的目标。

---

### 4.3 ppo_train_epoch：多轮更新、双损失与 early_stop_kl

#### 4.3.1 概念说明

GAE 算出优势后，进入 PPO 的**优化阶段**。PPO 与 GRPO 在这一步有一个关键不同：**同一批 rollout 数据会被重复使用多次**（`ppo_update_iters` 轮，每轮内再切小批量 `mini_batch`）。这是因为 Critic 训练较慢、需要多次梯度步才能逼近准确价值。

但「复用旧数据」有风险：更新太多会让新策略偏离采样时的旧策略太远，破坏 on-policy 假设。PPO 用两个机制兜底：

1. **PPO clip**：把重要性比率 \(r_t = \exp(\log\pi_\theta - \log\pi_{\text{old}})\) 裁剪在 \([1-\varepsilon, 1+\varepsilon]\)，限制单步更新幅度。
2. **early_stop_kl**：监控新旧策略的近似 KL，一旦超过阈值（默认 0.25）就**提前终止**本轮多轮更新，防止策略崩溃。

同时，因为有 Actor 和 Critic 两个网络、两个目标，所以是**双损失 + 双优化器**：策略损失更新 Actor、价值损失更新 Critic，各自有独立学习率（Actor 默认 `3e-7`，Critic 默认 `5e-7`，Critic 学得更快）。

#### 4.3.2 核心流程

每个训练 step：

1. **rollout + 奖励**：rollout 引擎在线采样得到 `completion_ids`、`old_resp_logp`、`per_token_logps`；`calculate_rewards` 算出标量奖励 `[B]`（见 [trainer/train_ppo.py:89-101](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_ppo.py#L89-L101)）。
2. **掩码构造**：根据 padding 与首个 EOS 位置，构造 `resp_policy_mask` / `resp_value_mask`，剔除无效 token（见 [trainer/train_ppo.py:117-128](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_ppo.py#L117-L128)）。
3. **Critic / ref 前向 + GAE**（no_grad，4.2 节）：得到 `old_resp_values`、`ref_resp_logp`、`advantages`、`returns`。
4. **多轮更新**：`for ppo_epoch in range(args.ppo_update_iters)`，每轮把 batch 打乱切成 `mini_batch`：
   - 重新前向 Actor、Critic，得到新的 `mb_resp_logp`、`mb_resp_values`。
   - 算 `log_ratio`、`approx_kl`，跨卡 all_reduce 同步后判断是否 `stop_ppo`。
   - 算 `policy_loss`（clip 代理 + KL 惩罚）、`value_loss`（clipped MSE）。
   - `loss = policy_loss + vf_coef*value_loss + aux_loss`，反向；每 `accumulation_steps` 步更新双优化器。
5. **日志 / 保存**：主进程打印并写 wandb；每隔 `save_interval` 保存 `ppo_actor_*.pth` 与续训检查点。

数学上，策略损失（clip 代理）为：

\[
\mathcal{L}^{\text{policy}} = \mathbb{E}\Big[\max\big(-A_t r_t,\; -A_t\cdot\mathrm{clip}(r_t,\,1-\varepsilon,\,1+\varepsilon)\big)\Big] + \beta\cdot\mathrm{KL}(\pi_{\text{ref}}\|\pi_\theta)
\]

价值损失（clipped，防止价值头一次跳太远）：

\[
\mathcal{L}^{\text{value}} = \tfrac{1}{2}\,\mathbb{E}\Big[\max\big((V_\theta - R)^2,\;(\mathrm{clip}(V_\theta,\,V_{\text{old}}-c,\,V_{\text{old}}+c)-R)^2\big)\Big]
\]

其中 ref-KL 用 **k3 估计** \(\mathrm{KL}_{k3} = e^{\Delta} - \Delta - 1,\ \Delta=\log\pi_{\text{ref}}-\log\pi_\theta\)；早停用的近似 KL 为 \(\widehat{\mathrm{KL}} = \tfrac{1}{2}(\log r_t)^2\)。

#### 4.3.3 源码精读

多轮更新主循环骨架见 [trainer/train_ppo.py:164-234](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_ppo.py#L164-L234)。其中近似 KL 与早停判断（注意跨卡同步防 DDP 死锁）见 [trainer/train_ppo.py:180-190](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_ppo.py#L180-L190)：

```python
log_ratio = mb_resp_logp - old_resp_logp[inds]
approx_kl = (0.5 * (log_ratio ** 2) * resp_policy_mask[inds]).sum() / resp_policy_mask[inds].sum().clamp(min=1)
# 同步各卡的 approx_kl，防止某卡 break 而其它卡继续导致 DDP 死锁
approx_kl_val = approx_kl.detach().clone()
if dist.is_initialized():
    dist.all_reduce(approx_kl_val, op=dist.ReduceOp.AVG)
if approx_kl_val > args.early_stop_kl:
    stop_ppo = True
```

策略损失与价值损失（clip 代理 + clipped value，对照上面公式阅读）见 [trainer/train_ppo.py:191-203](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_ppo.py#L191-L203)：

```python
ratio = torch.exp(log_ratio)
kl_ref_penalty = ((torch.exp(ref_resp_logp[inds] - mb_resp_logp) - (ref_resp_logp[inds] - mb_resp_logp) - 1.0)
                  * resp_policy_mask[inds]).sum() / resp_policy_mask[inds].sum().clamp(min=1)   # k3 KL 估计
policy_loss = ((torch.max(-advantages[inds] * ratio,
                          -advantages[inds] * torch.clamp(ratio, 1.0 - args.clip_epsilon, 1.0 + args.clip_epsilon))
               * resp_policy_mask[inds]).sum() / resp_policy_mask[inds].sum().clamp(min=1)
               + args.kl_coef * kl_ref_penalty)
value_loss = 0.5 * (torch.max((mb_resp_values - returns[inds]) ** 2,
                              (torch.clamp(mb_resp_values, old_resp_values[inds] - args.cliprange_value,
                                           old_resp_values[inds] + args.cliprange_value) - returns[inds]) ** 2)
                    * resp_value_mask[inds]).sum() / resp_value_mask[inds].sum().clamp(min=1)
```

早停时「loss × 0」保证 forward-backward 闭环（不中断 DDP 通信）见 [trainer/train_ppo.py:208-214](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_ppo.py#L208-L214)：

```python
# 早停时必须保证 forward-backward 闭环，故只截断 loss 不中断 DDP 通信
if stop_ppo:
    loss = (policy_loss + args.vf_coef * value_loss + aux_loss) * 0.0
else:
    loss = (policy_loss + args.vf_coef * value_loss + aux_loss) / args.accumulation_steps
loss.backward()
```

> **关键工程细节**：早停不是用 `break` 跳过反向，而是把 `loss` 乘 0 再 `backward()`。这是为了在 DDP 下保持每张卡「前向 + 反向」的对称——若某张卡 `break` 而其它卡继续，`DistributedDataParallel` 在反向时 all-reduce 梯度会死锁。

双优化器更新（Actor、Critic 各自 clip + step + scheduler + zero_grad）见 [trainer/train_ppo.py:226-234](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_ppo.py#L226-L234)：

```python
if grad_accum_step % args.accumulation_steps == 0:
    clip_grad_norm_(actor_model.parameters(), args.grad_clip)
    clip_grad_norm_(critic_model.parameters(), args.grad_clip)
    actor_optimizer.step();  critic_optimizer.step()
    actor_scheduler.step();  critic_scheduler.step()
    actor_optimizer.zero_grad();  critic_optimizer.zero_grad()
```

对应的 `__main__` 装配——双优化器 + 双余弦调度（注意 PPO 这里用了 `CosineAnnealingLR`，不同于其它脚本手动 `get_lr`）见 [trainer/train_ppo.py:391-398](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_ppo.py#L391-L398)：

```python
actor_optimizer  = optim.AdamW(actor_model.parameters(),  lr=args.learning_rate)        # 3e-7
critic_optimizer = optim.AdamW(critic_model.parameters(), lr=args.critic_learning_rate) # 5e-7
...
actor_scheduler  = CosineAnnealingLR(actor_optimizer,  T_max=total_optimizer_steps, eta_min=args.learning_rate/10)
critic_scheduler = CosineAnnealingLR(critic_optimizer, T_max=total_optimizer_steps, eta_min=args.critic_learning_rate/10)
```

日志打印（注意：当前实现只打印 `Critic Loss`，**Actor/策略 loss 没有直接打印**——它累加在 `policy_loss_sum` 里但未输出，这正是下一节实践的切入点）见 [trainer/train_ppo.py:269-272](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_ppo.py#L269-L272)。

#### 4.3.4 代码实践

**实践目标**：跑通一次 PPO，监控 reward / critic_loss / KL 三条曲线；同时**自己补一行 Actor loss 日志**（当前源码没打印它），并对照 README 解释 PPO 的两个现象。

**操作步骤**：

1. 准备依赖：`../out/full_sft_768.pth`（u5-l2 产物）、`../dataset/rlaif.jsonl`、以及 InternLM2 奖励模型（默认路径 `../../internlm2-1_8b-reward`，需自行下载，详见 u7-l3）。
2. 进入目录并启动（单卡即可，待本地验证）：
   ```bash
   cd trainer
   python train_ppo.py --batch_size 2 --mini_batch_size 2 --ppo_update_iters 2 --epochs 1
   ```
3. **补 Actor loss 日志**：在 [trainer/train_ppo.py:248-254](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_ppo.py#L248-L254) 的主进程统计区，仿照 `critic_loss_val` 加一行：
   ```python
   policy_loss_val = policy_loss_sum / max(log_count, 1)
   ```
   并在 [Logger 行](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_ppo.py#L269-L272) 里补上 `Policy Loss: {policy_loss_val:.4f}`（若开 wandb，可同步加入 `wandb.log`）。
4. 记录若干步的：reward、Policy Loss、Critic Loss、Approx KL、KL_ref。

**需要观察的现象**（对照 README [§7.1](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L1118) 的 PPO 曲线说明）：

- **reward 提升缓慢**：前几十步 reward 几乎不动，甚至抖动。原因是 Actor 依赖 Critic 提供的优势、Critic 又需要逐步收敛才能估准价值，两者相互耦合，初期 Critic 估不准会带偏 Actor 梯度方向。这与 GRPO（单网络、reward 稳定上升）形成对比。
- **显存约为单网络方法 1.5–2 倍**：同时驻留可训练的 Actor + Critic、冻结的 ref、外加奖励模型，显存开销显著高于 GRPO 的单网络方案。
- **Approx KL 触发早停**：偶见某步 `Approx KL` 接近或越过 `early_stop_kl=0.25`，此时 `stop_ppo` 置真，后续小批量的 loss 被乘 0，多轮更新提前结束。

**预期结果**：能打印出 Policy Loss 与 Critic Loss 两条曲线；reward 缓慢爬升；KL 受 clip 与 early_stop 约束维持在低位。若 reward 长期不涨，属 PPO 在超小模型上的已知现象（README 已说明），可尝试调大 `critic_learning_rate` 或延长 `epochs`。

> **若无法运行完整训练**（缺权重或奖励模型），可退化为「源码阅读型实践」：通读 [trainer/train_ppo.py:79-293](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_ppo.py#L79-L293) 的 `ppo_train_epoch`，画出「rollout → 奖励 → 掩码 → GAE → 多轮更新 → 双优化器」的调用链，并标注哪些量在 `no_grad` 块内、哪些参与梯度。仍标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么早停用 `loss * 0.0` 而不是直接 `break`？

**答案**：DDP 在反向传播时会跨卡 all-reduce 梯度，要求每张卡「前向 + 反向」严格对称。若某张卡 `break` 跳过反向、其它卡继续，等待 all-reduce 的卡会永远阻塞，造成死锁。把 loss 乘 0 再 `backward()`，既让该步梯度为 0（等同于不更新）、又保持了通信闭环，从而安全地「停」下本轮更新。

**练习 2**：PPO 的策略损失里，`kl_ref_penalty` 用的是 ref 模型（`full_sft` 冻结副本），而 `approx_kl` 用的是 `old_resp_logp`（采样时刻的策略）。这两个 KL 各管什么事？

**答案**：
- `kl_ref_penalty`（锚定 ref / SFT 模型）是**正则项**，防止 Actor 在追求 reward 时偏离初始 SFT 策略太远、遗忘语言能力（这是 RLHF 的「KL 锚点」思想），系数 `kl_coef=0.02`。
- `approx_kl`（锚定采样时刻的 old 策略）是**早停监控量**，衡量「这一轮多轮更新把策略推离采样策略多远」，一旦超阈值就停，保证 on-policy 复用安全。
- 两者参照系不同：一个管「别离 SFT 太远」，一个管「别离刚才采样的策略太远」。

**练习 3**：对照 README 的统一 PO 表（[README.md:1269-1274](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L1269-L1274)），填出 PPO 的三项与训练模型数。

**答案**：
- 策略项 \(f(r_t) = \min(r,\,\mathrm{clip}(r))\)。
- 优势项 \(g(A_t) = R - V(s)\)（由 Critic + GAE 提供，这是 PPO 区别于 GRPO 的关键）。
- 正则项 \(h(\mathrm{KL}_t) = \beta\cdot\mathbb{E}[\mathrm{KL}]\)（锚定 ref）。
- 训练模型数 = 2（Actor + Critic，外加冻结的 ref 与奖励模型参与前向）。

---

## 5. 综合实践

把本讲三个最小模块串成一个完整的小任务：**从一次 PPO step 里把「奖励 → GAE → 双损失」整条数值链路抠出来**。

任务步骤（源码阅读 + 局部插桩，待本地验证）：

1. **开 debug 模式跑一步**：`python train_ppo.py --debug_mode --debug_interval 1 --batch_size 2`，观察 [trainer/train_ppo.py:103-115](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_ppo.py#L103-L115) 打印的 prompt、response、reward。
2. **在 GAE 处插桩**：在 [trainer/train_ppo.py:147](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_ppo.py#L147) 之后，主进程下打印一次 `advantages[0]`、`returns[0]`、`old_resp_values[0]`，验证 `returns ≈ advantages + old_resp_values`，并观察奖励如何集中在末位、经 GAE 向前衰减传播。
3. **在双损失处插桩**：在 [trainer/train_ppo.py:203](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_ppo.py#L203) 之后打印 `policy_loss.item()`、`value_loss.item()`、`kl_ref_penalty.item()`，记录它们随 step 的变化。
4. **写一段小结**：用一句话回答——「PPO 比 GRPO 多了 Critic，这笔开销换来的是什么？又付出了什么代价？」（参考答案：换来每一步都有价值基线、优势估计更细粒度；付出显存翻倍与 Actor-Critic 耦合导致的收敛变慢。）

完成这个任务后，你应当能在不看源码的情况下，讲清「奖励怎么变成每个 token 的优势、优势怎么同时驱动 Actor 和 Critic 两个网络更新」。

---

## 6. 本讲小结

- **PPO = Actor-Critic 双网络**：Actor 是语言模型本体负责生成；Critic 复用主干、把 `lm_head` 换成 `value_head`（`hidden→1`），输出每个位置的 \(V(s)\)，靠 `strict=False` 加载 `full_sft` 主干、`value_head` 随机起步。
- **GAE 把稀疏终局奖励分摊到每个 token**：奖励只放在最后一个有效 token，靠递推 \(A_t = \delta_t + \gamma\lambda A_{t+1}\) 倒序传播；MiniMind 默认 \(\gamma=1.0,\lambda=0.95\)；`returns = advantages + old_resp_values` 作为 Critic 的回归标签。
- **多轮更新 + early_stop_kl**：同一批 rollout 被 `ppo_update_iters` 轮复用、每轮切 `mini_batch`，靠 PPO clip 与近似 KL 早停（阈值 0.25）兜底；早停用 `loss*0` 而非 `break`，保 DDP 前向-反向闭环。
- **双损失 + 双优化器**：策略损失（clip 代理 + ref 的 k3-KL 惩罚）更新 Actor、价值损失（clipped MSE）更新 Critic，各有独立学习率（Actor 3e-7、Critic 5e-7）与独立 `CosineAnnealingLR`。
- **两个工程现象**：reward 提升缓慢（Actor 依赖 Critic、相互耦合）；显存约为单网络方法 1.5–2 倍（Actor + Critic + 冻结 ref + 奖励模型同驻）。
- **与 GRPO 的关键差异**：PPO 的优势项是 \(R - V(s)\)（需 Critic）、训练 2 个模型；GRPO 的优势项是组内归一化 \((R-\mu)/\sigma\)（无 Critic）、训练 1 个模型——这是统一 PO 框架下两者最本质的填法区别。

---

## 7. 下一步学习建议

- **横向对比**：回头重读 [README.md §7.1–7.3](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L1079-L1135) 与统一 PO 表，把 PPO / GRPO / CISPO 的「策略项 / 优势项 / 正则项」三列对齐，体会 CISPO 为何要改写策略项以避免 clip 截断梯度。
- **下一讲 u7-l6 Agentic RL**：把单轮 rollout 扩展为「生成 tool_call → 执行工具 → 拼回 observation → 续写」的多轮循环，奖励从即时变为延迟整轮奖励 \(R(\text{answer})+R(\text{tool})+R(\text{format})\)，并复用本讲的 GRPO/CISPO 更新机制。
- **继续阅读的源码**：
  - [trainer/train_grpo.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_grpo.py)：对比 GRPO 的单网络优势计算与无 Critic 写法。
  - [trainer/train_agent.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_agent.py)：多轮工具调用 rollout（u7-l6）。
- **想深入理论**：阅读 PPO 原论文（Schulman et al., 2017）与 GAE 原论文（Schulman et al., 2015），重点看 clip 代理的动机与 \(\lambda\) 对偏差-方差权衡的推导。
