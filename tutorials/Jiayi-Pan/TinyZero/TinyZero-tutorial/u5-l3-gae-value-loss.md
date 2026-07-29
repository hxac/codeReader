# GAE 优势与价值损失

## 1. 本讲目标

本讲承接 [u5-l1 KL 惩罚与优势函数计算](./u5-l1-kl-penalty-advantage.md)：在那里我们已经得到 `token_level_rewards`（含 KL 罚的奖励）并知道 `compute_advantage` 会按 `adv_estimator` 把任务分派到 GAE 或 GRPO。本讲专门拆解 **GAE 这条分支**，读者学完后应该能够：

- 看懂 `compute_gae_advantage_return` 的反向递推，能手算每一步的 `delta` 与 `lastgaelam`；
- 理解 `masked_whiten` 如何对优势做归一化，以及它为什么在整个 batch 上统计而非逐序列统计；
- 看懂 `compute_value_loss` 的 clipped 双侧损失，说清 `cliprange_value`、`vf_clipfrac`、`vf_explained_var` 三个量的含义；
- 把握 GAE 分支下 `returns = advantages + values` 这个关键关系，以及它与价值网络监督信号的连接。

## 2. 前置知识

在进入源码前，先用直白的话建立几个直觉。

**为什么需要优势函数？** 强化学习的目标是对「比平均水平好多少」的动作加强化、对「比平均水平差多少」的动作减强化。这个「好/差多少」就是**优势（advantage）** \(\hat{A}\)。直接用奖励 \(r\) 当梯度信号会方差很大；用优势能把信号中心化，让训练更稳。

**什么是价值函数 \(V(s)\)？** 它是 critic 网络对「从当前状态出发，未来能拿到多少累计奖励」的估计。有了 \(V\)，就能把「实际拿到的回报」减去「原本的估计」，得到优势。所以 GAE 必须依赖 critic（这也解释了 u4-l2 里讲的 `adv_estimator=gae` 时 `use_critic=True`）。

**什么是 GAE？** Generalized Advantage Estimation（[Schulman et al. 2015](https://arxiv.org/abs/1506.02438)）。它用一个参数 \(\lambda \in [0,1]\) 在「高偏差低方差的 TD」和「低偏差高方差的 Monte Carlo」之间插值：

\[
\delta_t = r_t + \gamma V(s_{t+1}) - V(s_t)
\]

\[
\hat{A}_t^{\text{GAE}(\gamma,\lambda)} = \sum_{l=0}^{\infty}(\gamma\lambda)^l \delta_{t+l}
\]

\(\delta_t\) 叫 **TD 误差**，衡量「这一步实际收益 + 下一步估计」与「这一步估计」的差距。GAE 把未来所有 \(\delta\) 按衰减系数 \(\gamma\lambda\) 加权求和，得到当前 token 的优势。它有一个非常方便的**反向递推**形式：

\[
\hat{A}_t = \delta_t + \gamma\lambda\,\hat{A}_{t+1},\qquad \hat{A}_T = 0
\]

这正是源码循环计算的方式——从序列最后一个 token 往前推。

**为什么 TinyZero 选 gamma=1、lam=1？** 这两个值（见 [ppo_trainer.yaml](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L139-L141) 默认 `gamma: 1.0`、`lam: 1.0`）让 \(\gamma\lambda=1\)，GAE 退化成**纯 Monte Carlo 回报**：把未来所有奖励原样累加。配合 TinyZero「奖励只放在回答最后一个有效 token」的稀疏奖励设定（见 u4-l4），GAE 的作用就是把这份**末位奖励反向摊还**到序列里每一个 token 上，让每个 token 都拿到信用（credit）。

## 3. 本讲源码地图

| 文件 | 关键符号 | 作用 |
|---|---|---|
| `verl/trainer/ppo/core_algos.py` | `compute_gae_advantage_return` | 反向递推算 GAE 优势与 returns |
| `verl/trainer/ppo/core_algos.py` | `compute_value_loss` | 价值网络的 clipped MSE 损失 |
| `verl/utils/torch_functional.py` | `masked_whiten` / `masked_mean` / `masked_var` / `clip_by_value` | 在有效 token 上的归一化、均值、方差、裁剪工具 |
| `verl/trainer/ppo/ray_trainer.py` | `compute_advantage`（gae 分支）、`compute_data_metrics`（`vf_explained_var`） | 把 GAE 接进训练循环，并产出监控指标 |
| `verl/workers/critic/dp_critic.py` | `compute_values` / `update_critic` | critic 前向取 value、反向用 `compute_value_loss` 拟合 returns |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：GAE 反向递推、优势归一化、价值损失。三者顺序正是 GAE 分支在训练里的执行顺序。

### 4.1 GAE 优势反推：compute_gae_advantage_return

#### 4.1.1 概念说明

`compute_gae_advantage_return` 解决的问题是：给定每个 token 的奖励 \(r_t\)（即 `token_level_rewards`）和 critic 对每个 token 的价值估计 \(V_t\)（即 `values`），计算每个 token 的优势 \(\hat{A}_t\) 与回报 \(\hat{R}_t\)。它只做前向数值计算，不建图（包在 `torch.no_grad()` 里），因此「算 advantage」这一步本身不产生梯度——梯度来自后续 actor 与 critic 的更新。

关键术语：

- **TD 误差 \(\delta_t\)**：`delta = r_t + gamma * V_{t+1} - V_t`，单步的「惊喜」。
- **lastgaelam**：递推累加器，存的就是当前 token 的 GAE 优势 \(\hat{A}_t\)，从序列末尾往前滚。
- **returns**：\(\hat{R}_t = \hat{A}_t + V_t\)。注意它用的是**归一化之前**的优势加上 values，是给 critic 监督用的「目标回报」。

#### 4.1.2 核心流程

```text
输入: token_level_rewards (bs, L), values (bs, L), eos_mask (bs, L), gamma, lam
lastgaelam = 0
for t 从 L-1 反向到 0:
    nextvalues = values[t+1] 若 t < L-1，否则 0       # 序列末端没有「下一步」
    delta_t     = rewards[t] + gamma * nextvalues - values[t]
    lastgaelam  = delta_t + gamma * lam * lastgaelam   # 递推公式
    记录 lastgaelam
advantages = 把记录反序拼回正向
returns    = advantages + values                       # 用未归一化的 advantages
advantages = masked_whiten(advantages, eos_mask)       # 最后才归一化
return advantages, returns
```

注意一个**顺序陷阱**：`returns` 必须在 `masked_whiten` **之前**由「原始 advantages + values」得到。如果先归一化再算 returns，目标值就被污染了——归一化只作用于送进 actor 的 `advantages`，不作用于送进 critic 的 `returns`。

#### 4.1.3 源码精读

完整函数定义在此（注释说明输入输出形状都是 `(bs, response_length)`）：

[verl/trainer/ppo/core_algos.py:70-107](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L70-L107) — GAE 优势与 returns 的总入口。

反向递推的核心三行是本模块的灵魂：

[verl/trainer/ppo/core_algos.py:98-103](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L98-L103) — 从末位 token 向前推：`nextvalues` 在最后一步取 0（无后续状态），`delta` 是 TD 误差，`lastgaelam` 按 \(\hat{A}_t=\delta_t+\gamma\lambda\hat{A}_{t+1}\) 滚动累加，`advantages_reversed[::-1]` 把反序记录翻回时间正序。

随后两行收尾：

[verl/trainer/ppo/core_algos.py:105-106](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L105-L106) — `returns = advantages + values` 用的是**归一化前**的优势；紧接着才对 `advantages` 调 `masked_whiten`。

再看它的调用点，确认参数从哪来：

[verl/trainer/ppo/ray_trainer.py:119-132](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L119-L132) — `compute_advantage` 的 gae 分支：从 `data.batch` 取出 `values`、`token_level_rewards`，用 `response_mask` 当 `eos_mask`，把结果写回 `data.batch['advantages']` 与 `data.batch['returns']`。

> **命名提醒**：参数虽叫 `eos_mask`，实际传入的是 `response_mask`（见上方调用点 L128），即「哪些是有效 response token」的掩码。`masked_whiten` 用它排除 padding。名字容易误导，读源码时以传入值为准。

#### 4.1.4 代码实践

**实践目标**：用一个长度为 4 的玩具序列，手算 GAE 的每一步，再用脚本验证。

**给定**：`token_level_rewards = [0, 0, 0, 1]`（奖励只在末位，模拟 TinyZero 稀疏奖励），`values = [0.5, 0.6, 0.7, 0.8]`，`gamma=1`，`lam=1`。

**手算（反向 t=3→0）**：

| t | nextvalues | \(\delta_t = r_t+\gamma V_{t+1}-V_t\) | lastgaelam \(=\delta_t+\gamma\lambda\hat A_{t+1}\) | returns \(=\hat A_t+V_t\) |
|---|---|---|---|---|
| 3 | 0（末端） | \(1+0-0.8=0.2\) | \(0.2+1\cdot1\cdot0=0.2\) | \(0.2+0.8=1.0\) |
| 2 | \(V_3=0.8\) | \(0+0.8-0.7=0.1\) | \(0.1+1\cdot1\cdot0.2=0.3\) | \(0.3+0.7=1.0\) |
| 1 | \(V_2=0.7\) | \(0+0.7-0.6=0.1\) | \(0.1+1\cdot1\cdot0.3=0.4\) | \(0.4+0.6=1.0\) |
| 0 | \(V_1=0.6\) | \(0+0.6-0.5=0.1\) | \(0.1+1\cdot1\cdot0.4=0.5\) | \(0.5+0.5=1.0\) |

得到：原始 advantages（未归一化）\(=[0.5,0.4,0.3,0.2]\)，returns \(=[1.0,1.0,1.0,1.0]\)。

**预期观察**：returns 四个位置全是 1.0——因为 `gamma=lam=1` 让 GAE 退化成 Monte Carlo，而唯一奖励（1.0）在末端，所以**每个 token 看到的未来总回报都等于 1.0**。这正是「末位奖励被均摊」的直观体现。

**验证脚本（示例代码，非项目原文件）**：下面这段直接复刻 `compute_gae_advantage_return` 的循环，便于把每步打印出来与上表对照。仅依赖 `torch`。

```python
# 示例代码：复刻 GAE 反向递推，逐 token 打印
import torch

def gae_step_by_step(rewards, values, gamma=1.0, lam=1.0):
    L = rewards.shape[-1]
    lastgaelam = 0.0
    adv_rev = []
    print(f"{'t':>2} {'nextV':>6} {'delta':>6} {'A_t':>6} {'return':>7}")
    for t in reversed(range(L)):
        nextvalues = values[t + 1] if t < L - 1 else 0.0
        delta = rewards[t] + gamma * nextvalues - values[t]
        lastgaelam = delta + gamma * lam * lastgaelam
        adv_rev.append(lastgaelam)
        print(f"{t:>2} {nextvalues:>6.2f} {delta:>6.2f} {lastgaelam:>6.2f} {lastgaelam + values[t]:>7.2f}")
    return torch.stack(adv_rev[::-1])

rewards = torch.tensor([0., 0., 0., 1.])
values  = torch.tensor([0.5, 0.6, 0.7, 0.8])
raw_adv = gae_step_by_step(rewards, values)
print("returns =", (raw_adv + values).tolist())   # 期望 [1.0, 1.0, 1.0, 1.0]
```

**预期结果**：脚本打印的 `delta`、`A_t`、`return` 列应与上表完全一致，最后一行 `returns = [1.0, 1.0, 1.0, 1.0]`。

> 想直接调真实函数也行（需已 `pip install -e .` 装好 verl 环境）：把 `rewards`、`values` 升成 `(1,4)` 形状、`eos_mask=torch.ones(1,4)`，`from verl.trainer.ppo.core_algos import compute_gae_advantage_return` 即可；注意返回的 `advantages` 已被 `masked_whiten` 归一化，而 `returns` 未归一化（值为 `[1,1,1,1]`）。结果是否在真实函数里也成立，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：把上例的 `lam` 改成 0（gamma 仍为 1），returns 会变成什么？

**答案**：\(\lambda=0\) 时 \(\hat A_t=\delta_t\)，于是 returns \(=\delta_t+V_t=r_t+V_{t+1}\)（末端 \(=r_T+0=r_T\)）。代入：returns \(=[0+0.6,\;0+0.7,\;0+0.8,\;1+0]=[0.6,0.7,0.8,1.0]\)。对比可见 \(\lambda\) 越小，前面 token 的回报越接近「单步 TD」估计，信用往回摊得越弱。

**练习 2**：为什么序列末位（t=L-1）的 `nextvalues` 取 0 而不是取 `values[L]`？

**答案**：因为回答序列没有「第 L 个 token 的状态」——episode 在生成完就结束，终止状态的价值约定为 0。代码里用 `if t < gen_len - 1 else 0.0` 表达这一边界条件。

---

### 4.2 优势归一化：masked_whiten

#### 4.2.1 概念说明

算完原始优势后，`compute_gae_advantage_return` 立刻调 `masked_whiten` 把优势做 **白化（whiten）**：减均值、除标准差，使优势大致分布在 0 附近、尺度稳定。这一步能显著降低策略梯度的方差，是 PPO 训练稳定性的关键技巧之一。

「masked」的含义是：只对有效 response token（`eos_mask=1`）统计均值方差、也只对这些位置归一化，padding 位不参与、也不被破坏。

#### 4.2.2 核心流程

白化的数学定义（\(\epsilon=10^{-8}\) 防除零）：

\[
\tilde{A} = (A - \mu)\cdot\frac{1}{\sqrt{\text{Var}(A)+\epsilon}}
\]

其中 \(\mu\)、\(\text{Var}\) 在**所有被 mask 选中的元素**上统计——注意是「整个 batch × response_length 展平后的全体有效 token」，**不是逐条序列**单独统计。这意味着不同样本的优势会互相参照、共用同一组均值方差。

#### 4.2.3 源码精读

[verl/utils/torch_functional.py:130-136](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/torch_functional.py#L130-L136) — `masked_whiten`：`shift_mean` 默认 True 时减均值；若传 `False` 则把均值加回去（仅缩放不中心化）。GAE 调用处没传该参数，走默认减均值。

它依赖两个更底层的工具：

[verl/utils/torch_functional.py:107-109](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/torch_functional.py#L107-L109) — `masked_mean`：加权求和除以 mask 总数。

[verl/utils/torch_functional.py:112-127](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/torch_functional.py#L112-L127) — `masked_var`：用 Bessel 校正（\(n/(n-1)\)）的无偏方差；当 `mask_sum<=1` 会显式报错——这解释了为什么 micro batch 不能太小。

#### 4.2.4 代码实践

**实践目标**：用 4.1 手算出的原始优势 `[0.5,0.4,0.3,0.2]`，验证 `masked_whiten` 的输出，并理解统计范围。

**操作**：对这 4 个值求均值 \(\mu=0.35\)；无偏方差（n=4，Bessel 校正 \(4/3\)）\(\approx 0.01667\)；\(\tilde A_i=(A_i-0.35)/\sqrt{0.01667}\approx(A_i-0.35)/0.1291\)。

**预期结果**：

```
whitened ≈ [1.162, 0.387, -0.387, -1.162]
```

**需观察的现象**：白化后均值约为 0、量级被压到 ±1 附近。注意真实训练里这个统计是跨整个 batch 算的，单条序列的结果会与上面略有差异——上面只是用一条序列演示公式本身。

**验证脚本（示例代码）**：

```python
import torch
from verl.utils.torch_functional import masked_whiten   # 需 verl 环境
adv = torch.tensor([[0.5, 0.4, 0.3, 0.2]])
mask = torch.ones(1, 4)
print(masked_whiten(adv, mask))   # 期望约 [[ 1.162, 0.387, -0.387, -1.162]]
```

#### 4.2.5 小练习与答案

**练习 1**：如果把 `compute_gae_advantage_return` 里的 `masked_whiten` 注释掉（直接返回原始优势），训练会怎样？

**答案**：优势尺度随任务奖励幅度漂移（比如 countdown 的 1.0 奖励会给出量级偏大的优势），策略梯度方差变大，ratio 更易冲出 clip 区间，训练通常更不稳定、更易发散。白化正是为了让 advantage 量级与学习率、cliprange 匹配。

**练习 2**：为什么 `masked_var` 在 `mask_sum==1` 时主动抛错？

**答案**：Bessel 校正的分母是 \(n-1\)，\(n=1\) 时除零。代码宁可报错也不静默返回错误值，提示你把 micro batch 调大，使每个统计组里至少有 2 个有效 token。

---

### 4.3 价值损失：compute_value_loss

#### 4.3.1 概念说明

critic 网络的任务是拟合 returns（目标回报）。`compute_value_loss` 衡量「critic 的新预测 `vpreds`」与「目标 `returns`」的差距，并用 PPO 风格的 **clipped MSE** 控制单步更新幅度。

关键术语：

- **vpreds**：critic 这一轮前向算出的**新**价值预测。
- **values**：rollout 时记录的**旧**价值预测（`compute_values` 算出来、冻结带回的）。
- **cliprange_value**：把新预测限制在 `[old_value - c, old_value + c]` 内，防止价值头一步跳太远。注意它的默认值是 **0.5**（见 [ppo_trainer.yaml:119](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L119)），比策略侧的 `cliprange=0.2` 宽得多。
- **vf_clipfrac**：被裁剪影响到的 token 占比（监控量）。
- **vf_explained_var**：价值预测对 returns 方差的解释比例，衡量 critic 拟合得好不好。

#### 4.3.2 核心流程

价值损失的计算步骤：

\[
v_{\text{clip}} = \mathrm{clip}(v_{\text{pred}},\;V_{\text{old}}-c,\;V_{\text{old}}+c)
\]

\[
L_1 = (v_{\text{pred}} - \hat{R})^2,\qquad L_2 = (v_{\text{clip}} - \hat{R})^2
\]

\[
L_{\text{vf}} = \tfrac{1}{2}\,\text{masked\_mean}\big(\max(L_1, L_2)\big)
\]

取 `max` 是**悲观**策略：当裁剪会让损失变小（即新预测已跑出裁剪窗、且裁剪反而更接近 returns）时，仍保留较大的未裁剪损失，保证梯度继续把预测拉向 returns；同时裁剪窗又限制了「即使要大幅修正，单次也不能超过 c」。这与 PPO 策略损失取 `max` 的精神一致（详见 [u5-l2](./u5-l2-policy-loss-clip.md)）。前面的 \(\tfrac{1}{2}\) 是把 MSE 写成半化形式，使梯度恰好为误差本身。

监控指标 `vf_explained_var`（在 `compute_data_metrics` 里算，不在 `compute_value_loss` 内）：

\[
R^2_{\text{vf}} = 1 - \frac{\mathrm{Var}(\hat{R}-V_{\text{old}})}{\mathrm{Var}(\hat{R})+\epsilon}
\]

接近 1 说明 critic 几乎解释了 returns 的全部波动；接近 0 甚至为负，说明 critic 几乎没用或比均值还差。

#### 4.3.3 源码精读

[verl/trainer/ppo/core_algos.py:216-239](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L216-L239) — `compute_value_loss` 全函数，返回 `(vf_loss, vf_clipfrac)`。

逐行拆解关键四行：

[verl/trainer/ppo/core_algos.py:234-238](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L234-L238) — `vpredclipped` 把新预测夹在旧值 ±`cliprange_value` 内；`vf_losses1/2` 是未裁剪/裁剪后的平方误差；`vf_loss` 取两者最大值再 `masked_mean` 并乘 0.5；`vf_clipfrac` 统计裁剪生效的比例。

裁剪工具的实现：

[verl/utils/torch_functional.py:86-92](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/torch_functional.py#L86-L92) — `clip_by_value`：`max(min(x, max), min)`，先压上界再抬下界，等价于 `torch.clamp` 但支持张量上下界。

调用点（critic 如何用 returns 监督）：

[verl/workers/critic/dp_critic.py:184-190](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/critic/dp_critic.py#L184-L190) — `update_critic` 用本轮 `vpreds`、冻结的 `values`、目标 `returns` 与 `eos_mask` 调 `compute_value_loss`，再把 loss 除以 `gradient_accumulation` 做梯度累积反传。

而 `values`（旧预测）的来源，注意它乘了 mask：

[verl/workers/critic/dp_critic.py:136](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/critic/dp_critic.py#L136) — `compute_values` 末尾 `values = values * attention_mask[:, -response_length-1:-1]`，把 padding 位的预测清零，保证送进 GAE 与 value loss 的 values 在无效位置恒为 0。

监控指标 `vf_explained_var` 的计算位置：

[verl/trainer/ppo/ray_trainer.py:194-198](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L194-L198) — 在 `compute_data_metrics` 里用 `valid_returns - valid_values` 的方差除以 `valid_returns` 的方差，得到解释方差；仅 `use_critic=True`（即 GAE 路线）时才计算。

[verl/trainer/ppo/ray_trainer.py:235](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L235) — 最终写入 metrics 字典的 `critic/vf_explained_var`。

#### 4.3.4 代码实践

**实践目标**：用一组小数字看 `cliprange_value` 如何「稳住」价值更新。

**操作**：设旧值 `values=0.5`，目标 `returns=1.0`，`cliprange_value=0.5`（默认）。critic 两个不同预测场景：

- 场景 A：`vpreds=0.7`（在窗 `[0.0, 1.0]` 内）→ `vpredclipped=0.7`，`vf_loss=0.5*(0.7-1.0)^2=0.045`，未被裁剪。
- 场景 B：`vpreds=1.8`（冲出窗，裁到 `1.0`）→ `vf_losses1=0.5*(1.8-1.0)^2=0.32`，`vf_losses2=0.5*(1.0-1.0)^2=0`，取 `max=0.32`。此时 `vf_clipfrac=1`（裁剪生效）。

**需观察的现象**：场景 B 里即使裁剪把预测拉近了 returns，损失仍保留较大的未裁剪值 0.32——梯度继续把预测往下拉，但「单步内」预测被裁在 1.0 以内，避免了一次跳到 1.8 这种剧烈震荡。这就是 clipped value loss「既允许修正、又限制步幅」的稳定化作用。

**预期结果**：场景 A 的 `vf_clipfrac=0`，场景 B 的 `vf_clipfrac=1`；二者 `vf_loss` 分别约为 0.045、0.32。

**验证脚本（示例代码）**：

```python
import torch
from verl.trainer.ppo.core_algos import compute_value_loss   # 需 verl 环境

eos_mask = torch.ones(1, 1)
values   = torch.tensor([[0.5]])
returns  = torch.tensor([[1.0]])

for vpreds in [torch.tensor([[0.7]]), torch.tensor([[1.8]])]:
    loss, clipfrac = compute_value_loss(vpreds, returns, values, eos_mask, cliprange_value=0.5)
    print(f"vpreds={vpreds.item():.1f} -> vf_loss={loss.item():.3f}, vf_clipfrac={clipfrac.item():.1f}")
# 预期:
# vpreds=0.7 -> vf_loss=0.045, vf_clipfrac=0.0
# vpreds=1.8 -> vf_loss=0.320, vf_clipfrac=1.0
```

> 若本地暂无 verl 环境，可把 `compute_value_loss` 的 5 行核心逻辑（见 4.3.3）用纯 `torch` 复刻验证。脚本输出是否符合预期，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `compute_value_loss` 不像策略损失那样需要 importance ratio（重要性采样比）？

**答案**：策略损失用旧策略采的样本去估新策略的梯度，必须用 ratio 校正分布偏差；而价值函数是状态的函数，对「当前状态的价值」的回归目标（returns）不依赖于「是谁采的这个状态」，因此不需要 ratio。这也是 critic 更新通常比 actor 更稳定的结构性原因之一。

**练习 2**：`vf_explained_var` 在训练初期可能是负的，正常吗？

**答案**：正常。训练初期 critic 尚未学到东西，其预测可能比「直接用 returns 均值」还差，此时 \(\mathrm{Var}(\hat R - V)>\mathrm{Var}(\hat R)\)，解释方差为负。随着 critic 拟合，该指标应逐步上升趋向 0 以上、接近 1。它是诊断 critic 健康度的关键指标。

## 5. 综合实践

把三个模块串起来，做一次「端到端的 GAE + 价值损失」纸上推演，验证你对整条数据链的理解。

**任务**：给定一条 4-token 回答（全部有效，`eos_mask=[1,1,1,1]`），`token_level_rewards=[0,0,0,1]`，初始 `values=[0.5,0.6,0.7,0.8]`，`gamma=lam=1`，`cliprange_value=0.5`。请：

1. 反向递推写出每步 `delta`、原始 advantages、returns（参考 4.1.4 答案：原始 adv `[0.5,0.4,0.3,0.2]`，returns `[1,1,1,1]`）。
2. 对原始 advantages 做 `masked_whiten`（参考 4.2.4，得约 `[1.162,0.387,-0.387,-1.162]`）——这是送进 actor 的 `advantages`；而 returns 仍是未归一化的 `[1,1,1,1]`，送进 critic。
3. 假设 critic 更新后新预测 `vpreds=[0.6,0.7,0.8,0.95]`，手算四个 token 的 `vf_losses1`（未裁剪）与是否触发裁剪（`values±0.5 = [0,0.1,0.2,0.3]~[1.0,1.1,1.2,1.3]`，四个预测都落在窗内，故 `vf_clipfrac=0`），再用 `0.5*masked_mean` 汇总。

**验收**：

- 你应当能解释清楚「为什么 returns 不归一化、advantages 要归一化」——因为 critic 学的是真实回报尺度，actor 只关心相对好坏。
- 你应当能指出 `compute_gae_advantage_return` 里 `returns` 的赋值在 `masked_whiten` 之前这条不可改动的顺序。
- 推演完后，把 4.1.4、4.2.4、4.3.4 三个验证脚本依次在本地跑通（需 verl 环境），对照手算与程序输出是否一致；若环境不具备，至少把纯 `torch` 复刻版跑通。

## 6. 本讲小结

- `compute_gae_advantage_return` 用反向递推 \(\hat A_t=\delta_t+\gamma\lambda\hat A_{t+1}\) 把末端稀疏奖励摊还给每个 token；TinyZero 默认 `gamma=lam=1`，等价于 Monte Carlo 回报。
- `returns = advantages + values` 必须在归一化**之前**计算——returns 给 critic 当监督、advantages 给 actor 当梯度信号，二者尺度要求不同。
- `masked_whiten` 在「全 batch 展平后的有效 token」上统计均值方差并白化优势，降低策略梯度方差；依赖 `masked_var` 的 Bessel 校正，因此 micro batch 不能小于 2 个有效 token。
- `compute_value_loss` 是 clipped MSE：把新预测夹在 `values±cliprange_value` 内、取未裁剪/裁剪损失的 `max`，既允许修正又限制单步步幅；默认 `cliprange_value=0.5`。
- `vf_clipfrac` 监控裁剪触发比例，`vf_explained_var` 监控 critic 对 returns 的解释力，二者只在 GAE（`use_critic=True`）路线下才有意义。
- GAE 与 GRPO 的分水岭：GAE 需要价值网络、沿序列逐 token 给出不同优势；GRPO 用组内归一化省掉 critic、序列内优势为常数（详见 [u5-l1](./u5-l1-kl-penalty-advantage.md) 与 [u5-l5](./u5-l5-grpo-algorithm.md)）。

## 7. 下一步学习建议

- 接着读 **u5-l4 KL 控制器与 KL 估计变体**：本讲的 KL 只出现在 reward 端（`apply_kl_penalty`），u5-l4 会展开 `FixedKLController`/`AdaptiveKLController` 如何动态调节 \(\beta\)，以及四种 KL 估计公式。
- 再读 **u5-l5 GRPO 算法实现**：与本讲对照「GAE 用 critic、GRPO 用组内归一化省 critic」的全貌，并理解 `compute_grpo_outcome_advantage` 为何让单样本组 `mean=0, std=1`。
- 若想看本讲的 returns 如何真正驱动 critic 反向更新，读 **u6-l3 Critic 价值估计与更新**，它精读 `dp_critic.py` 的 `compute_values`/`update_critic`/`_forward_micro_batch`，补齐「价值头前向—取 response 段—乘 mask—算 loss—梯度累积」的完整闭环。
