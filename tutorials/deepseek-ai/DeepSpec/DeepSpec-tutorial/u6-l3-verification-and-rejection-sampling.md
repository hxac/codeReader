# u6-l3 验证与拒绝采样：verify_draft_tokens 的数学与实现

## 1. 本讲目标

上一讲（u6-l2）我们精读了投机解码主循环 `generate_decoding_sample`，把「骨架固化、差异下沉」的结构看清楚了，但当时刻意把两件事留作黑盒：

1. **验证**到底怎么做——目标模型一次前向之后，凭什么样的规则决定草稿 token 的生死？
2. 为什么这样做之后，最终输出**在数学上与目标模型自己的采样完全不可区分**（无损）？

本讲打开这个黑盒，逐行精读 `verify_draft_tokens` 与它依赖的采样工具 `deepspec/utils/sampling.py`。学完后你应该能够：

- 推导接受概率 \( \min(1,\; p_{\text{target}}(x)/p_{\text{draft}}(x)) \) 配合 residual 采样为什么能保证每个提交 token 的边缘分布严格等于目标分布——这是投机解码「无损」的全部数学。
- 解释 `accept_prefix_mask` 为什么用 `cumprod`（连乘）而不是 `cumsum`，以及它如何一步算出「最长被接受前缀」。
- 说明 residual 采样如何在首个被拒位置修正分布，停止 token 如何截断已接受前缀，`committed_tokens` 如何拼装。
- 用一个玩具蒙特卡洛实验（10000 次模拟）验证理论接受长度与 token 分布。

## 2. 前置知识

本讲需要的数学基础不深，但要求你理解以下几个概念：

- **拒绝采样（rejection sampling）**：想从一个「难采样」的目标分布 \( p \) 抽样时，可以先从一个「易采样」的提议分布 \( q \) 抽出 \( x \)，再以某个概率决定「接受」还是「拒绝重采」。经典拒绝采样要求存在常数 \( M \) 使 \( p(x) \le M q(x) \)；投机解码用的是它的一个变体——**不需要 M、拒绝时也不重采整条链，而是在拒绝位做一次修正采样**。
- **边缘分布**：一个随机变量经过多步随机过程（先以概率接受、否则按残差采样）后，最终「落在每个 token 上」的总概率。证明无损性就是计算这个边缘分布并验证它等于 \( p \)。
- **总变差距离（TV distance）**：两个分布的差异度量 \( \mathrm{TV}(p,q) = \frac{1}{2}\sum_x |p(x)-q(x)| \)。本讲会推出一个漂亮等式：单位置接受率 \( \alpha = 1 - \mathrm{TV}(p,q) \)，它正好接上第 4 单元讲过的 L1 蒸馏损失（\( L_1 = 2\,(1-\alpha) \)）。
- **张量工具**：`gather`（按下标取概率）、`clamp`（截断）、`cumprod`（累积连乘）。这三个操作构成本讲的实现骨架。

承接 u6-l2 已建立的两个事实，本讲直接复用不再重证：主循环每轮把 `DraftProposal`（含草稿 token 与草稿分布）交给 `verify_draft_tokens`，拿回 `VerificationResult`；循环不变量是「目标 KV cache 长度恒等于游标 `start`，对应已提交前缀」。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [deepspec/utils/sampling.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/sampling.py) | 概率与采样工具箱：`logits_to_probs`（温度归一）、`sample_from_probs`（多项采样）、`gather_token_probs`（按下标取概率）、`sample_residual`（残差修正采样） |
| [deepspec/eval/base_evaluator.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py) | 本讲主角 `verify_draft_tokens`（L186-L304），加上 `DraftProposal`/`VerificationResult` 两个数据合同与主循环的调用点 |
| [deepspec/eval/dspark/draft_ops.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/draft_ops.py) | `build_dspark_proposal` 展示 `draft_probs` 是如何用**同一个** `logits_to_probs`、同一温度产生的——这是 p 与 q 可比较的前提 |
| [deepspec/eval/dspark/confidence_head.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/confidence_head.py) | `accept_prefix_mask` 的下游消费者（置信度校准，u6-l5 的主角，本讲只看它怎么用这个 mask） |

一句话概括分工：`sampling.py` 提供「一个分布上的操作」，`verify_draft_tokens` 把它们编排成「一次验证的完整决策」。

## 4. 核心概念与源码讲解

### 4.1 从 logits 到概率：logits_to_probs 与温度

#### 4.1.1 概念说明

模型前向输出的永远是 **logits**（未归一化的分数，形状 `[B, S, V]`），而拒绝采样比较的是**概率分布**。所以验证的第一步是把两侧 logits 变成同一词表上的分布：

- \( p \)：目标模型在验证前向后的 `target_probs`；
- \( q \)：草稿模型提议时自己算好的 `draft_probs`。

温度 \( T \) 控制分布锐度：\( T \to 0 \) 收敛到 greedy（one-hot），\( T \) 越大分布越平。关键工程约束是：**p 和 q 必须用同一个温度、同一个归一化函数产生**，否则两者不在同一个分布语义下，后面所有数学都不成立。

#### 4.1.2 核心流程

```
logits --softmax(logits / T)--> probs          (T >= 1e-5)
logits --argmax scatter one-hot--> probs       (T < 1e-5，greedy 分支)
```

数学定义：

\[ p(x) = \frac{\exp(z_x / T)}{\sum_{x'} \exp(z_{x'} / T)} \]

#### 4.1.3 源码精读

先看 `logits_to_probs` 的两条分支：

[deepspec/utils/sampling.py:L6-L11](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/sampling.py#L6-L11) 把 logits 变概率：温度低于 1e-5 时走 L7-L10 的 greedy 分支——`argmax` 后用 `scatter_` 摆出一个 one-hot 分布（float32）；否则 L11 做标准的温度 softmax。注意 `logits.float()` 先升精度再除温度，避免 bf16 logits 除以小温度时精度崩坏。

[deepspec/utils/sampling.py:L14-L17](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/sampling.py#L14-L17) 在概率上采样：`torch.multinomial` 按概率抽一个 token，先 `reshape` 拍平 batch 维再抽，保证形状合同 `[B, S, V] → [B, S]`。

[deepspec/utils/sampling.py:L30-L31](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/sampling.py#L30-L31) 按下标取概率：`gather` 把「这批 token 在这个分布下的概率」抽出来，返回 `[B, S]`。它是 4.2 节接受概率计算的核心零件。

再看两侧是怎么用**同一个函数**的。目标侧：

[deepspec/eval/base_evaluator.py:L229](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L229) 验证前向一结束，立刻把目标 logits 用本次评测的 `temperature` 归一成 `target_probs`。

草稿侧：

[deepspec/eval/dspark/draft_ops.py:L140-L143](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/draft_ops.py#L140-L143) DSpark 在构造提议时，把草稿 logits 用**同一个** `logits_to_probs` 和**同一个** `temperature` 归一成 `draft_probs` 塞进 `DraftProposal`。温度来自 [deepspec/eval/dspark/evaluator.py:L130](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L130) 传入的 `self.args.temperature`，与验证侧同源。

此外 [deepspec/eval/base_evaluator.py:L230-L238](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L230-L238) 显式校验 `draft_probs` 与 `target_probs` 词表维度一致——p、q 必须定义在同一个离散空间上（草稿模型与目标模型共用冻结 `lm_head` 的合同可回看 u4-l2）。

顺带一提，[deepspec/utils/sampling.py:L20-L27](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/sampling.py#L20-L27) 的 `sample_tokens` 是「logits 直接采样」的便捷封装，草稿模型提议侧采样 token 时用它（如 [deepspec/eval/eagle3/evaluator.py:L19](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py#L19)、[deepspec/modeling/dspark/qwen3/modeling.py:L327](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L327)）；验证路径则拆成 `logits_to_probs` + `sample_from_probs` 两步，因为验证需要**先拿到完整分布做接受判定，再在合适的行上采样**。

#### 4.1.4 代码实践

**实践目标**：直观感受温度对分布的影响，并确认 greedy 分支产出 one-hot。

**操作步骤**（示例代码，只需 torch 与本仓库在 `PYTHONPATH` 上）：

```python
import torch
from deepspec.utils.sampling import logits_to_probs

logits = torch.tensor([[[2.0, 1.0, 0.5, -1.0]]])   # [1, 1, V]，示例代码
for T in (2.0, 1.0, 0.5, 1e-6):
    probs = logits_to_probs(logits, T)
    print(f"T={T:>7}: {[round(x, 4) for x in probs[0, 0].tolist()]}")
```

**需要观察的现象**：T 从 2.0 降到 0.5，最大分量的概率单调上升、其余分量下降；T=1e-6 时输出 `[1.0, 0.0, 0.0, 0.0]`。

**预期结果**：T=1.0 时 softmax 值约为 `[0.6095, 0.2242, 0.1360, 0.0303]`（可用手算 \(\exp(z)/\sum\exp(z)\) 核对：\(\exp\) 值依次约 7.389、2.718、1.649、0.368，和约 12.124）。T=1e-6 触发 L7-L10 greedy 分支，概率质量全部落在 argmax 上。

#### 4.1.5 小练习与答案

**练习 1**：如果目标侧用 T=1.0、草稿侧用 T=0.5 各自归一化再比较，投机解码还是无损的吗？

**答案**：不是。无损性证明要求 p 与 q 是**同一个**目标分布语义下的两个近似（见 4.2 的推导——残差分布 \( \max(p-q,0) \) 的构造依赖 p、q 之和都为 1 且指向同一分布）。温度不同意味着草稿在逼近「另一个分布」，接受概率 \( \min(1, p/q) \) 的推导前提被破坏，输出会系统性偏离目标。这正是 draft_ops.py L140-L143 与 base_evaluator.py L229 必须共用一个 temperature 参数的原因。

**练习 2**：greedy（T<1e-5）时草稿分布是 one-hot，此时接受概率会是什么形态？验证还工作吗？

**答案**：设草稿 one-hot 在 \( x^* \)，则 \( q(x^*)=1 \)，接受概率 \( \min(1, p(x^*)/1) = p(x^*) \)（p 同为 one-hot 时非 0 即 1）。工作正常：若目标 greedy token 与草稿一致则以概率 1 接受，否则以概率 \( p(x^*)\in\{0\} \)（目标 one-hot 下为 0）被拒，随后 residual 采样把修正 token 拉回目标 argmax。也就是说温度为 0 时投机解码退化为「猜中就收、猜不中就换」，输出仍等于目标 greedy 序列。

### 4.2 拒绝采样验证：接受概率、随机数比较与前缀 cumprod

#### 4.2.1 概念说明

这是本讲的心脏。朴素想法是「目标 argmax 与草稿 token 一致就算对」——但这只对 greedy 成立，采样解码下会系统性偏向高频 token，破坏输出分布。

正确做法是**逐位置条件分布的假设检验式接受**：草稿 token \( x \sim q \) 已经采出来了，目标模型在同一条件下给出分布 \( p \)。规则：

- 以概率 \( r(x) = \min\left(1,\; \dfrac{p(x)}{q(x)}\right) \) **接受**这个 token；
- 否则在第一个被拒位置改从残差分布采样（4.3 节）。

为什么这样输出分布恰好是 \( p \)？推导见 4.2.2。先记住直觉：**草稿在目标「高看」的地方（\( p > q \)）永远被接受；在目标「低看」的地方（\( p < q \)）按比例打折；打折扣掉的概率质量由 residual 补回**。两者加起来不多不少正好是 \( p \)。

#### 4.2.2 核心流程

一次验证（n 个草稿 token）的决策流水线：

```
verify_input_ids = [a, d1, d2, ..., dn]        # a 是锚点 token（当前已接受 token）
目标一次前向 → target_probs: [1, n+1, V]        # 第 j 行预测窗口第 j+1 个 token
proposed_tokens = [d1, ..., dn]
对每个槽位 i：
    tp_i = p_i(d_i)          # gather 目标分布
    dp_i = q_i(d_i)          # gather 草稿分布（下限 1e-8 防除零）
    r_i  = min(1, tp_i/dp_i) # 接受概率
    accept_mask_i = 1[u_i < r_i]   # u_i ~ Uniform[0,1)
accept_prefix_mask = cumprod(accept_mask)       # 连乘：一旦拒绝，之后全归零
accepted_draft_tokens = sum(accept_prefix_mask) # 最长被接受前缀长度
```

**无损性的证明**（单位置版本）。设草稿采样 \( x \sim q \)，接受概率 \( r(x)=\min(1,p(x)/q(x)) \)。定义边际接受率

\[ \alpha = \sum_x q(x)\,r(x) = \sum_x \min\bigl(q(x),\,p(x)\bigr) = 1 - \mathrm{TV}(p,q) \]

（最后一步用 \( \sum_x \min(p,q) = 1 - \frac{1}{2}\sum_x|p-q| \)。）拒绝时从残差分布 \( p_{\text{res}}(x) = \max(p(x)-q(x),\,0)\,/\,Z \) 采样，其中 \( Z = \sum_x \max(p(x)-q(x),0) = 1-\alpha \)。于是提交 token 的边缘分布为

\[ P(x) \;=\; \underbrace{q(x)\,r(x)}_{\text{接受路径}} \;+\; \underbrace{(1-\alpha)\,p_{\text{res}}(x)}_{\text{拒绝后修正}} \]

分两种情况：

- 若 \( p(x) \ge q(x) \)：\( q(x)r(x)=q(x) \)，\( p_{\text{res}}(x) = \frac{p(x)-q(x)}{Z} \)，代入得 \( q(x) + Z\cdot\frac{p(x)-q(x)}{Z} = p(x) \)；
- 若 \( p(x) < q(x) \)：\( q(x)r(x) = p(x) \)，残差项为 0，同样得 \( p(x) \)。

所以 \( P(x) \equiv p(x) \)：**输出分布严格等于目标分布，与草稿好坏无关**。草稿质量只影响 \( \alpha \)（速度），不影响正确性。把这个结论沿序列做归纳（每个位置的提交分布都是目标的自回归条件分布）即得整条序列无损。

**为什么用 cumprod**：\( n \) 个槽位各自独立抽随机数 \( u_i \)，我们只取**连续被接受的前缀**——第 k 个 token 被提交当且仅当前 k 个全部被接受，概率为 \( \prod_{i=1}^{k} \alpha_i \)。0/1 指示变量的累积连乘恰好实现这个语义：任何一位出现 0，其后全部归零。`cumprod` 的第 k 项期望正是 \( \prod_{i\le k}\alpha_i \)，这也是期望接受长度公式

\[ \mathbb{E}[\text{接受草稿数}] = \sum_{k=1}^{n} \prod_{i=1}^{k} \alpha_i \]

的蒙特卡洛来源——它同时预告了 u6-l1 的指标 `accept_rate@k`（第 k 槽位的接受概率）与 u4-l4 的 `tau_probabilistic`（用 cumprod 解析估计期望接受长度）。

最后把 \( \alpha = 1-\mathrm{TV}(p,q) \) 与第 4 单元接上：\( L_1(p,q) = \sum_x|p-q| = 2\,\mathrm{TV}(p,q) = 2(1-\alpha) \)。**训练时最小化 L1 蒸馏损失，就是最大化每个槽位的接受概率**——损失函数与推理指标在这里严丝合缝。

#### 4.2.3 源码精读

验证函数的入口与两道护栏：

[deepspec/eval/base_evaluator.py:L199-L212](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L199-L212) 做两件前置校验：草稿数不得超过 `max_proposal_tokens`（L199-L204）；`verify_input_ids` 的首位必须是主循环传入的当前已接受 token（L205-L212）——这正是 u6-l2 讲过的「锚点一致性」合同，锚点 token 必须来自目标分布（prefill 直采或上一轮的 residual/bonus），草稿链才挂得在正确的前缀上。

然后是接受概率的五步核心，全部在 [deepspec/eval/base_evaluator.py:L240-L258](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L240-L258)：

- [deepspec/eval/base_evaluator.py:L243-L247](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L243-L247) 取草稿 token 并 gather 目标概率。**对齐细节**：验证窗口长 \( n{+}1 \)（锚点 + n 个草稿），第 j 行 logits 预测第 j+1 个 token，所以用 `target_probs[:, :-1, :]`（前 n 行）配上 `proposed_tokens = verify_input_ids[:, 1:]`（后 n 个 token）——第 i 个草稿 token 的目标概率来自它**前面一格**的预测行，与草稿侧 `draft_probs` 第 i 行（草稿对同一槽位的预测）严格同格。
- [deepspec/eval/base_evaluator.py:L248-L251](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L248-L251) gather 草稿概率并 `clamp_min(1e-8)`。数学上被采出的 token 必有 \( q(x)>0 \)，但 float32 softmax 可能下溢成 0，不设下限会出现除零 NaN。
- [deepspec/eval/base_evaluator.py:L252-L255](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L252-L255) 计算 \( \min(1, p/q) \)：相除后 `clamp(max=1.0)` 截断——目标比草稿更看好的 token（比率 > 1）以概率 1 接受。
- [deepspec/eval/base_evaluator.py:L256](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L256) 随机数比较：`torch.rand_like` 抽 \( u\in[0,1) \)，`u < r` 的概率恰为 \( r \)。这是拒绝采样的标准实现。
- [deepspec/eval/base_evaluator.py:L257-L258](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L257-L258) `cumprod(dim=1)` 得到前缀接受 mask，`sum` 得到最长被接受前缀长度 `accepted_draft_tokens`。

#### 4.2.4 代码实践

**实践目标**：手工走一遍 L244-L258 的决策流水线，确认 cumprod 的「归零」语义。

**操作步骤**（示例代码，纯 torch 即可运行）：

```python
import torch

# 模拟 4 个槽位：预先「抽好」的目标概率、草稿概率与均匀随机数（示例代码）
tp = torch.tensor([[0.90, 0.95, 0.20, 0.70]])
dp = torch.tensor([[0.80, 0.90, 0.80, 0.60]])
u  = torch.tensor([[0.42, 0.99, 0.55, 0.10]])

r = torch.clamp(tp / dp.clamp_min(1e-8), max=1.0)     # min(1, p/q)
accept_mask = (u < r).to(torch.int64)                 # 随机数比较
accept_prefix_mask = accept_mask.cumprod(dim=1)       # 前缀连乘
print("accept_prob =", r.tolist())
print("accept_mask =", accept_mask.tolist())
print("prefix_mask =", accept_prefix_mask.tolist())
print("accepted    =", int(accept_prefix_mask.sum()))
```

**需要观察的现象**：第 3 槽位 \( r=0.25 \)、\( u=0.55 \) 被拒；尽管第 4 槽位本身会被接受（\( u=0.10 < 1.0 \)），它在 `prefix_mask` 里也是 0。

**预期结果**：`accept_prob = [[1.0, 1.0, 0.25, 1.0]]`（前两槽位 p>q 被截断到 1），`accept_mask = [[1, 1, 0, 1]]`，`prefix_mask = [[1, 1, 0, 0]]`，`accepted = 2`。若把 `cumprod` 换成 `cumsum`，会得到 `[1,2,2,3]`——拒绝之后的 token 又被数进来，语义完全错误。

#### 4.2.5 小练习与答案

**练习 1**：草稿模型与目标模型完全相同（\( q \equiv p \)）时，接受概率是多少？每轮提交几个 token？

**答案**：\( r(x) = \min(1, p/p) = 1 \)，所有草稿 token 必被接受，`accepted_draft_tokens = n`，加上最后一行的 bonus token 每轮提交 \( n{+}1 \) 个。这是接受长度的理论天花板（\( \alpha=1 \)，\( \mathbb{E}=n \)），也说明加速上限由提议块大小决定。

**练习 2**：草稿与目标毫不相干（\( \mathrm{TV}(p,q) \to 1 \)，比如支撑集不相交）时会怎样？

**答案**：\( \alpha \to 0 \)，第一个草稿 token 几乎必被拒，每轮只提交 1 个 residual 修正 token——回到普通自回归解码的速度，还多付一次草稿前向的开销。输出仍然无损（4.2.2 的证明不依赖草稿质量），只是白费算力。这正是「把草稿训得像目标」是 DeepSpec 全部意义的原因（回看 u1-l1）。

**练习 3**：为什么接受概率比较用 `rand_like < accept_prob`，而不是「`rand_like <= accept_prob` 或直接比较 argmax」？

**答案**：\( u\sim\mathrm{Uniform}[0,1) \) 时 \( \mathbb{P}(u < r) = r \) 对任意 \( r\in[0,1] \) 精确成立（`<=` 在连续分布下差异为零，两者都正确，工程上任选）。而「比较 argmax」只对 greedy 正确：采样解码下它会丢掉所有非众数 token，使输出分布坍缩、不再等于 \( p \)。

### 4.3 residual 修正采样与 committed_tokens 的拼装

#### 4.3.1 概念说明

拒绝发生之后，「这一轮已经花掉一次目标前向」的成本不可回收，必须在这个位置产出一个**分布正确**的 token 兜底。直接从 \( p \) 重采会破坏无损性——因为被拒绝这个事件本身携带信息（它说明被拒位置上 \( p(x)<q(x) \) 的可能性大，剩余概率质量需要重新分配）。

修正方法是残差分布：把 \( p \) 减去 \( q \) 的正部归一化后采样。直觉：草稿提议「占用」的概率质量 \( \min(p,q) \) 已经在接受路径里用掉了，剩下的 \( \max(p-q,0) \) 才是拒绝路径应得的质量。4.2.2 的推导已验证这个分配恰好补齐到 \( p \)。

#### 4.3.2 核心流程

验证收尾的三段逻辑：

```
若 停止 token 出现在已接受前缀中:
    accepted_draft_tokens 截断到首个停止 token（含）
    effective_proposal_length 同步截断；标记 terminated

next_token 的三选一:
    ① 0 < accepted < n  → sample_residual(p[a], q[a])   # 首个被拒位修正
    ② accepted == n     → 从 target_probs 最后一行直采   # bonus token
    ③ n == 0（空提议）   → 同②                          # 兜底，最差也提交 1 个

committed_tokens = cat(已接受草稿前缀, next_token)       # 长度 accepted+1
```

残差分布的数学定义（承接 4.2.2 的 \( Z = 1-\alpha \)）：

\[ p_{\text{res}}(x) = \frac{\max\bigl(p(x) - q(x),\, 0\bigr)}{Z}, \qquad Z = \sum_{x'} \max\bigl(p(x')-q(x'),\,0\bigr) \]

#### 4.3.3 源码精读

[deepspec/utils/sampling.py:L34-L44](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/sampling.py#L34-L44) 实现 residual 采样：L38 取 `clamp(target - draft, min=0)` 正部；L39-L42 处理退化情形——若残差质量 \( Z \le 10^{-8} \)（意味着 \( p \le q \) 处处成立，而两者又都归一，只能是 \( p \approx q \)，即 4.2 练习 1 的情形），残差分布没有定义，直接退回从 `target_probs` 采样，分布仍是正确的 \( p \)；L43 归一化后经 `sample_from_probs` 抽一个 token。注意它的形状合同是 `[B, V]`（L44 的 `unsqueeze(1)` 把它临时抬回 `[B,1,V]` 喂给多项采样）。

回到验证函数。停止 token 截断：

[deepspec/eval/base_evaluator.py:L262-L276](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L262-L276) 若已接受前缀（L265 的切片跳过锚点、只看草稿 token）里出现停止 token，就把 `accepted_draft_tokens` 截到首个停止 token（含），并把 `effective_proposal_length` 同步截短、置 `terminated_by_stop_token=True`。`effective_proposal_length` 的意义在下游：置信度头只统计 `[ : effective_length]`——见 [deepspec/eval/dspark/confidence_head.py:L357-L375](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/confidence_head.py#L357-L375)，它把置信度的 cumprod 预测与 `accept_prefix_mask` 截断到同一长度对齐（u6-l5 展开）。

next_token 的分支：

[deepspec/eval/base_evaluator.py:L278-L285](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L278-L285) 中途被拒时（L278），用 `target_probs` 与 `draft_probs` 的**第 `accepted_draft_tokens` 行**做 residual 采样——这一行预测的正是首个被拒槽位的 token，p、q 严格同格（对照 4.2.3 的对齐讨论）；全部接受或空提议时（L284-L285），从 `target_probs[:, -1:, :]` 最后一行直采「bonus token」。空提议（DSpark 置信度阈值砍光提议时会出现，见 draft_ops.py 的 `_empty_dspark_proposal`）也走这条路，保证**最差每轮也提交 1 个 token**（u1-l1 的承诺在此兑现）。

committed_tokens 的拼装与去向：

[deepspec/eval/base_evaluator.py:L287-L293](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L287-L293) 把已接受草稿前缀（切片 `[1 : accepted+1]` 跳过锚点）与 `next_token` 拼接，长度恒为 `accepted_draft_tokens + 1`。

[deepspec/eval/base_evaluator.py:L411-L425](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L411-L425) 主循环的消费端：L411-L413 把 `verify_input_ids[:, :accepted+1]` 写回 `output_ids`（锚点 + 接受前缀）；正常路径 L421-L425 再写入 bonus token、推进游标 `start += accepted+1`、`crop` 目标 KV cache（丢弃被拒草稿的废料 KV，u6-l2 已讲）并调用 `update` 钩子。注意 L415-L419 的 terminated 分支：序列已在停止 token 处终结，`start` 只推进 `accepted_draft_tokens`（停止 token 是被接受草稿的一员，已含在内），residual 采出的 `next_token` 在这条路径上被丢弃；同时 `acceptance_lengths` 记的是 `accepted_draft_tokens` 而非 `+1`——对照正常路径的 `+1` 会发现两条路径记录的其实都是「本轮实际提交的 token 数」（terminated 时提交的全部来自草稿，正常时含 bonus），这正是 u6-l1 指标 `accept_len` 的分子口径。

#### 4.3.4 代码实践

**实践目标**：亲手验证 residual 分布的形状与退化兜底分支。

**操作步骤**（示例代码）：

```python
import torch
from deepspec.utils.sampling import sample_residual

p = torch.tensor([[0.5, 0.3, 0.2]])
q = torch.tensor([[0.2, 0.3, 0.5]])

# 正常分支：residual = clamp(p-q, 0) = [0.3, 0, 0]，归一化后 one-hot 在 token 0
for _ in range(5):
    print(int(sample_residual(p, q)), end=" ")
print()

# 退化分支：p == q，残差质量为 0，兜底改为直接从 p 采样
q2 = torch.tensor([[0.5, 0.3, 0.2]])
from collections import Counter
c = Counter(int(sample_residual(p, q2)) for _ in range(2000))
print("退化分支采样频率:", {k: v / 2000 for k, v in sorted(c.items())})
```

**需要观察的现象**：第一段 5 次输出全是 0；第二段 2000 次里 token 0/1/2 的频率分别接近 0.5/0.3/0.2。

**预期结果**：手算 \( \max(p-q,0) = [0.3, 0, 0] \)，\( Z=0.3 \)，归一化后 \( p_{\text{res}} = [1,0,0] \)——因为 token 1 上 \( p=q \)（质量已被接受路径用尽）、token 2 上 \( p<q \)（草稿已「超额提议」），两者都不该再从残差里出现。退化分支触发 sampling.py L40-L42 的 `torch.where` 兜底，等价于直接按 \( p \) 采样；频率与 \( p \) 的偏差应在抽样噪声内（2000 次下约 ±0.02）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `sample_residual` 不能直接从 `target_probs` 采样？

**答案**：边缘分布 \( P(x) = q(x)r(x) + (1-\alpha)p_{\text{res}}(x) \) 中接受路径已经贡献了 \( \min(p,q) \) 的质量；若拒绝路径再从完整 \( p \) 采样，总分布为 \( \min(p,q) + (1-\alpha)p \ne p \)（多出了 \( (1-\alpha)\min(p,q) \) 的质量）。只有残差 \( \max(p-q,0)/Z \) 恰好补上缺口（4.2.2 的两情况证明）。

**练习 2**：一轮验证中 `accepted_draft_tokens = 3`、`draft_token_count = 7`（无停止 token）。这轮提交几个 token？分别是什么来源？目标 KV cache 会被 crop 到多长？

**答案**：提交 4 个 = 3 个被接受草稿 token + 1 个在第 3 行（首个被拒位）residual 采出的修正 token（`committed_tokens` 长 `accepted+1`）。游标推进 `start += 4`，`past_key_values_target.crop(start)` 把验证前向多算的 4 个草稿位 KV 裁掉，恢复「cache 长度 == start」的循环不变量。

**练习 3**：停止 token 截断发生在 L264 时为什么要求 `accepted_draft_tokens > 0`？

**答案**：已接受前缀为空时说明第一个草稿 token 就被拒了，本轮根本没有草稿 token 进入输出，停止与否由 residual/bonus 的 `next_token` 决定——主循环在 L428-L429 统一用 `has_stop_token` 检查新提交的 token 并退出，验证函数内无需（也不能）在此截断。

## 5. 综合实践：玩具验证器的蒙特卡洛验证

把 4.1-4.3 串成一个完整实验：**不加载任何模型**，自造 target/draft 概率分布，按 `verify_draft_tokens` 的公式（L244-L258 + `sample_residual`）模拟 10000 轮验证，回答两个问题：

1. 模拟的平均接受草稿数是否逼近理论值 \( \sum_{k=1}^{n}\prod_{i\le k}\alpha_i \)（本玩具中各槽位同分布，即 \( \alpha + \alpha^2 + \cdots + \alpha^n \)，其中 \( \alpha = \sum_x \min(p,q) \)）？
2. 第一个提交位的 token 频率是否收敛到目标分布 \( p \)？（无损性的直接检验）

**操作步骤**（示例代码，在仓库根目录运行，需 `pip install -r requirements.txt` 后的 torch 环境）：

```python
import torch
from collections import Counter
from deepspec.utils.sampling import (
    logits_to_probs, gather_token_probs, sample_residual,
)

torch.manual_seed(0)

V, n, T = 6, 4, 10000                      # 词表 / 每轮草稿数 / 模拟轮数
target_logits = torch.tensor([[[2.0, 1.5, 0.5, 0.2, 0.1, 0.05]]])   # [1,1,V]
draft_logits  = torch.tensor([[[1.0, 2.0, 0.8, 0.3, 0.1, 0.02]]])
p = logits_to_probs(target_logits, 1.0)
q = logits_to_probs(draft_logits, 1.0)

# 理论：alpha = sum_x min(p,q)；期望接受数 = alpha + alpha^2 + ... + alpha^n
alpha = torch.minimum(p, q).sum()
theory = sum((alpha ** k).item() for k in range(1, n + 1))

accepted_counts = []
first_slot_tokens = []
for _ in range(T):
    # ① 草稿提议：从 q 逐槽位采样 n 个 token（玩具设定：槽位间独立同分布）
    draft_tokens = torch.multinomial(q[0, 0].repeat(n, 1), 1).reshape(1, n)
    # ② 复刻 verify_draft_tokens L244-L258 的拒绝采样
    tp = gather_token_probs(p.expand(1, n, V), draft_tokens)
    dp = gather_token_probs(q.expand(1, n, V), draft_tokens).clamp_min(1e-8)
    accept_prob = torch.clamp(tp / dp, max=1.0)
    accept_mask = (torch.rand_like(accept_prob) < accept_prob).to(torch.int64)
    a = int(accept_mask.cumprod(dim=1).sum())
    accepted_counts.append(a)
    # ③ 第一个提交位：a>0 时是被接受的 d1，否则在首个被拒位 residual 采样
    first_slot_tokens.append(
        int(draft_tokens[0, 0]) if a > 0 else int(sample_residual(p[0], q[0]))
    )

print(f"理论 alpha           = {alpha.item():.4f}")
print(f"理论期望接受草稿数   = {theory:.4f}")
print(f"模拟平均接受草稿数   = {sum(accepted_counts) / T:.4f}")

counter = Counter(first_slot_tokens)
emp = torch.tensor([counter.get(v, 0) for v in range(V)], dtype=torch.float) / T
print("目标分布 p           =", [round(x, 4) for x in p[0, 0].tolist()])
print("模拟第一提交位分布   =", [round(x, 4) for x in emp.tolist()])
```

**需要观察的现象**：

- 模拟平均接受数与理论值 `sum(cumprod(accept_prob))` 的解析表达（玩具里即 `α+α²+α³+α⁴`）之差随轮数增加而缩小；
- 模拟的第一提交位分布逐分量贴近目标分布 p。

**预期结果**：两条曲线/两组数字在抽样噪声内一致（10000 轮下接受数均值偏差通常在 ±0.03 量级，分布分量偏差约 ±0.01）。第二项一旦吻合，就是 4.2.2 无损性推导的实验版证明：**无论草稿多差（本例中 q 与 p 差异明显），提交 token 的分布始终等于目标分布**。具体数值以本地实际输出为准（随机种子不同结果略有浮动）。

进阶改法：把 `draft_logits` 改成与 `target_logits` 完全相同再跑一遍，`alpha` 应为 1.0、接受数应为 `n`；再改成与目标「互补」（互换支撑集），`alpha` 趋于 0、接受数趋于 0——亲手复现 4.2.5 练习 1 与练习 2 的两个极端。

## 6. 本讲小结

- 接受规则是 \( r(x)=\min(1, p(x)/q(x)) \) 配合 residual 采样，两情况分类讨论可证明提交 token 的边缘分布**严格等于目标分布**——投机解码无损，草稿质量只决定速度（\( \alpha = 1-\mathrm{TV}(p,q) \)），不影响正确性。
- `cumprod` 是「只取连续被接受前缀」的精确实现：任一槽位拒绝即归零其后所有位；`sum(prefix_mask)` 就是最长被接受前缀长度，其第 k 项的期望 \( \prod_{i\le k}\alpha_i \) 直接对应 `accept_rate@k` 与期望接受长度。
- residual 采样从 \( \max(p-q,0)/Z \) 中抽修正 token，与接受路径的质量 \( \min(p,q) \) 严丝合缝拼成 \( p \)；\( Z\approx 0 \)（\( p\approx q \)）时退回直采目标分布。
- 工程细节全为数学正确性服务：p/q 同温度同函数归一、词表维度校验、`clamp_min(1e-8)` 防下溢除零、`target_probs[:, :-1]` 与 `proposed_tokens` 的错位对齐、停止 token 截断与 `effective_proposal_length` 同步。
- `committed_tokens` 恒为「接受前缀 + 1 个修正/bonus token」，主循环据此推进游标并 crop 目标 KV cache；terminated 路径丢弃 residual token，两条路径的 `acceptance_lengths` 记录的都是本轮实际提交数。
- 训练侧的 L1 蒸馏损失与推理侧的接受率是一枚硬币的两面：\( L_1 = 2(1-\alpha) \)，最小化蒸馏损失就是最大化接受长度。

## 7. 下一步学习建议

本讲补完了 u6-l2 留下的验证黑盒，验证系统的通用部分至此全部讲完。接下来两讲分别看两种草稿算法如何「喂」这个验证器：

- **u6-l4（DSpark 评估器）**：`_propose` 如何用 mask token 块一次前向产出整块提议与 `draft_probs`（本讲反复引用的 draft_ops.py 将被完整精读），`_update` 如何从 `target_output.hidden_states` 提取被接受前缀的目标隐状态。
- **u6-l5（置信度校准）**：本讲埋下的 `accept_prefix_mask` 如何成为 `ConfidenceHeadRecorder` 的监督标签（置信度的 cumprod 预测 vs 真实前缀接受），以及 `effective_proposal_length` 截断的完整动机。

建议按 u6-l4 → u6-l5 顺序阅读；若想巩固本讲的数学，可先做第 5 节的综合实践再继续。
