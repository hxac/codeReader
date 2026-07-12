# GRPO 与 CISPO：分组相对优势与无梯度截断

## 1. 本讲目标

本讲是强化学习后训练单元（u7）的第四讲。在 [u7-l1](#) 我们立起了 PO（Policy Optimization）的统一视角——所有 xxPO 都在优化「策略项 \(f(r_t)\) × 优势项 \(g(A_t)\) − 正则项 \(h(\text{KL}_t)\)」；在 [u7-l2](#) 我们搭好了训推分离的 Rollout 引擎；在 [u7-l3](#) 我们解决了「奖励从哪来、连续奖励为何必要」。

本讲把这三块拼成一条完整的训练链路：**当前策略采样 → 打奖励 → 算优势 → 算损失 → 反向更新**。学完后你应当能够：

1. 说清 GRPO 如何用「同一问题的 N 个回答的组内均值/方差」当作 baseline，从而**完全不需要 Critic 价值网络**（这是它相对 PPO 最大的工程简化）。
2. 读懂 `train_grpo.py` 里 `ratio = exp(logp − old_logp)`、PPO clip、token 级 KL 的逐行实现，并理解 `completion_mask` 如何把「EOS 之后」与「padding」从 loss 里剔除。
3. 说清 CISPO 为什么把策略项改写成 `clamp(ratio)·A·log π`，从而**避免 clip 把梯度路径一起截断**，并能把它当作 GRPO 的一个 `loss_type` 变体直接切换。
4. 独立用 `rlaif.jsonl` 跑通一次 GRPO/CISPO 训练，并看懂日志里的 `Reward / KL_ref / Adv Std / Actor Loss` 各代表什么。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。如果你已读过 u7-l1 ~ u7-l3，可以跳到第 3 节。

**为什么需要「优势（Advantage）」。** 强化学习不只关心某个回答「好不好」（奖励 \(R\)），而关心它「比平常好多少」。如果一个回答的奖励是 5，但这个问题的所有回答平均都能拿 4，那它其实只比预期好 1；这个「扣除 baseline 后的净收益」就是优势 \(A\)。用 \(A\) 而不是 \(R\) 去乘以梯度，能极大降低方差、稳定训练。

**PPO/GRPO 为什么需要 ratio 和 clip。** 训练用的回答是**旧策略**采样出来的（叫 behavior policy），而我们要更新的是**新策略**。直接用旧样本的梯度去更新新策略会有偏差，所以引入重要性权重 \(r_t = \pi_{\text{new}}(a_t)/\pi_{\text{old}}(a_t)\) 来校正。但 \(r_t\) 太大会一步走太远、把策略搞崩，PPO 用 `clip(r, 1-ε, 1+ε)` 把它钳制住。

**CISPO 要解决的痛点。** PPO/GRPO 的 clip 有个副作用：一旦 \(r\) 被 clip 成常数，`min(r·A, clip(r)·A)` 整项就变成常数，**梯度直接归零**——也就是说，越是需要被约束的大步更新，反而越学不动。CISPO 的洞察是：把 `ratio·A` 改写成 `clamp(ratio)·A·log π`，让被 clip 的 `clamp(ratio)` 只当**权重**（detach），梯度从 `log π` 这条路继续流。

> 关键术语速查：
> - **rollout**：用当前策略采样一批回答，并记录采样时刻的对数概率 `old_logps`。
> - **ratio**：\(r_t = \exp(\log\pi_\theta - \log\pi_{\text{old}})\)，新旧策略对同一 token 的概率比。
> - **参考模型（ref model）**：与策略同结构、同初始、被冻结的副本，用于 KL 正则，防止策略跑离 SFT 分布太远。
> - **退化组（Degenerate Group）**：同一问题的 N 个回答奖励几乎相同 → 组内方差≈0 → 优势≈0 → 学不到东西。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的地方 |
|------|------|----------------|
| `trainer/train_grpo.py` | GRPO/CISPO 训练主脚本，本讲主角 | `calculate_rewards`、`grpo_train_epoch`、`loss_type` 分支、`__main__` 装配 |
| `trainer/rollout_engine.py` | 训推分离的采样引擎（u7-l2 详讲） | `RolloutResult`、`TorchRolloutEngine.rollout`、`compute_per_token_logps`、`update_policy` |
| `dataset/lm_dataset.py` | 数据集定义（u2-l2 详讲） | `RLAIFDataset`：返回 prompt 与空 answer |
| `trainer/trainer_utils.py` | 公共工具（u4-l1、u7-l3 详讲） | `LMForRewardModel.get_score`、`init_model`、`lm_checkpoint` |

一句话串起来：`RLAIFDataset` 吐出 prompt → `RolloutEngine.rollout` 采样并回算 old logps → `calculate_rewards` 打分 → 组内归一化得优势 → `loss_type` 分支算 policy loss + token 级 KL → 反向更新 → `update_policy` 把新权重同步回 rollout 引擎。

## 4. 核心概念与源码讲解

### 4.1 calculate_rewards：把一条回答打成标量奖励

#### 4.1.1 概念说明

[u7-l3](#) 已经论证过：小模型做 RL 必须用**连续、混合**的奖励，否则奖励稀疏会让组内方差塌缩、梯度消失。本模块不再重复「为什么」，而是落到 `train_grpo.py` 的具体实现——看看这个「混合奖励」到底由哪几项相加而成。

`calculate_rewards` 把每一条回答的奖励 \(R\) 定义为四个分量的加和：

\[
R = \underbrace{r_{\text{len}}}_{\text{长度}} + \underbrace{r_{\text{think}}}_{\text{思考格式}} + \underbrace{r_{\text{fmt}}}_{\text{闭合}} - \underbrace{p_{\text{rep}}}_{\text{重复惩罚}} + \underbrace{s_{\text{RM}}}_{\text{奖励模型}}
\]

其中规则项（长度/格式/重复）是「辅助 shaping」，奖励模型分数 \(s_{\text{RM}}\) 是「主信号」。规则项的作用是在 RM 信号噪声大时给出一个可微的方向（例如鼓励 20~800 字、鼓励恰好一个 `</think>`），RM 的作用是给出连续的、语义层面的好坏判断。

#### 4.1.2 核心流程

```
对每个 prompt i (共 B 个):
    对该 prompt 的第 j 条采样 (共 num_generations 条):
        idx = i * num_generations + j          # 与 rollout 的 repeat_interleave 顺序对齐
        从 prompt 用正则解析出 messages(角色序列)
        answer = response
        r_len   = +0.5 if 20 <= len(answer) <= 800 else -0.5
        if '</think>' in response:
            切出 thinking_content / answer_content
            r_think = +1.0 if 20<=len(thinking)<=300 else -0.5
            r_fmt   = +0.25 if 恰好1个</think> else -0.25
            answer  = answer_content           # 后续 RM 只评分正式回答
        r_rep = rep_penalty(answer)            # n-gram 重复度，∈[0, 0.5]
        s_RM  = reward_model.get_score(messages, answer)
        R[idx] = r_len + (r_think + r_fmt) - r_rep + s_RM
返回 R ∈ ℝ^(B*num_gen)
```

注意 `idx = i * num_generations + j` 这一行——它必须与 `TorchRolloutEngine.rollout` 里的 `prompt_ids.repeat_interleave(num_generations, dim=0)`（见 [rollout_engine.py:L76](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/rollout_engine.py#L76)）保持同样的「prompt-外层、generation-内层」排列，否则奖励会错配到错误的回答上。

#### 4.1.3 源码精读

完整的奖励函数定义在这里：

[trainer/train_grpo.py:L37-L68](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_grpo.py#L37-L68) — 逐条回答累加四类奖励，全程在 `torch.no_grad()` 下进行（奖励是数据，不参与求导）。

几个关键实现点：

- **从 prompt 反解 messages**（[L50-L52](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_grpo.py#L50-L52)）：用正则 `<\|im_start\|>(system|user|assistant)\s+(.*?)<\|im_end\|>` 直接从已经渲染好的 prompt 字符串里把对话角色抠出来，交给 RM。这比单独维护一份原始 messages 更省事，但依赖 chat_template 严格使用 `<|im_start|>`/`<|im_end|>` 包裹（这正是 u2-l1 引入的核心控制标记）。
- **长度项**（[L54](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_grpo.py#L54)）：`+= 0.5 if 20 <= len(strip) <= 800 else -0.5`，鼓励「不太短也不太长」。
- **思考格式项**（[L55-L59](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_grpo.py#L55-L59)）：若回答含 `</think>`，则分别奖励「思考段长度适中（+1.0/-0.5）」与「恰好一个闭合标签（+0.25/-0.25）」，并把 `answer` 切成 `</think>` 之后的部分交给 RM。
- **重复惩罚**（[L60](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_grpo.py#L60)）：调用 `rep_penalty(answer)`。

重复惩罚的实现很短，值得单独一看：

[trainer/train_grpo.py:L31-L34](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_grpo.py#L31-L34) — 对回答做 3-gram，用「(总 gram 数 − 去重后 gram 数) × 2 / 总数」衡量重复比例，并 `min(cap, ...)` 封顶在 0.5。它是个被**减去**的惩罚项（`rewards[idx] -= rep_penalty(answer)`），所以越重复、奖励越低，且最大扣 0.5。

最后把所有 RM 分数拼成 tensor 一次性加上（[L65-L66](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_grpo.py#L65-L66)），向量化收尾。

#### 4.1.4 代码实践（源码阅读型）

**目标**：在不启动训练的前提下，理解「同一回答在含/不含 `</think>` 时奖励项的差异。

**步骤**：

1. 打开 [train_grpo.py:L37-L68](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_grpo.py#L37-L68)。
2. 假设两条回答都 300 字、RM 都给 0.2 分：
   - 回答 A：直接回答，无 `</think>`。
   - 回答 B：`<think>…（200 字思考）…</think>正式回答（100 字）`，恰好一个 `</think>`。
3. 手算两条各自的规则分（忽略重复惩罚）：
   - A：`r_len=+0.5`，无 think 项，规则合计 `+0.5`。
   - B：`r_len=+0.5`、`r_think=+1.0`（20≤200≤300）、`r_fmt=+0.25`，规则合计 `+1.75`。

**预期结果**：B 比 A 在规则项上高 1.25 分，这说明 `calculate_rewards` 明显**偏好「先思考再回答、且思考长度适中、标签恰好闭合」**的回答——这正好呼应 u2-l1/u6 的「思考能力由模板+标签+数据共同塑造」。

**待本地验证**：把 `rep_penalty` 对一段高度重复的文本（如同一句话重复 10 遍）跑一下，确认返回值接近上限 0.5。

#### 4.1.5 小练习与答案

**Q1**：为什么 `calculate_rewards` 全程包在 `torch.no_grad()` 里？
**答**：奖励是「数据标签」，是用来给优势项提供标量信号的，本身不需要对奖励求梯度；不进计算图可以省显存、避免无意义的反向。

**Q2**：把长度上界从 800 改成 100，训练曲线最可能先出现什么异常？
**答**：模型会更快收敛到「凑够 100 字就停」的短回答，`Avg Response Len` 会迅速逼近 100，长回答被持续负奖励抑制。

---

### 4.2 grpo_train_epoch（上）：分组采样、分组相对优势与 completion_mask

#### 4.2.1 概念说明

GRPO（Group Relative Policy Optimization）相对 PPO 最关键的简化是：**用一个问题的 N 个回答互相当 baseline，扔掉 Critic 价值网络。**

PPO 里优势是 \(A = R - V(s)\)，需要专门训一个价值网络 \(V\) 去估计 baseline；而价值网络在小模型上很难收敛（见 u7-l5）。GRPO 的做法是：对同一个 prompt 采样 \(N\) 条回答，各自拿到奖励 \(R_1,\dots,R_N\)，用组内统计量做归一化：

\[
A_i = \frac{R_i - \mu_{\text{group}}}{\sigma_{\text{group}} + \epsilon}, \quad \mu_{\text{group}}=\frac{1}{N}\sum_i R_i,\quad \sigma_{\text{group}}=\sqrt{\frac{1}{N}\sum_i (R_i-\mu)^2}
\]

直观地说：高于组内均值的回答被鼓励（\(A>0\)），低于均值的被抑制（\(A<0\)）。这就把「绝对好不好」转化成「相对组内基线好不好」，而基线完全来自**同一批采样本身**，不需要任何额外网络。代价是前面提过的退化组问题——当 \(N\) 个回答奖励几乎一样时 \(\sigma\approx 0\)、\(A\approx 0\)，学不到东西（u7-l3 已说明为何要用连续奖励来缓解）。

#### 4.2.2 核心流程

`grpo_train_epoch` 是一个标准的「每 step 一轮 rollout + 一次更新」的循环，本模块先讲它前半段（采样到优势）：

```
for step, batch in loader:
    prompts = batch['prompt']                         # B 条 prompt
    # 1. 采样：每条 prompt 生成 num_generations 条回答
    rollout_result = rollout_engine.rollout(prompt_ids, attn_mask,
                                            num_generations=N,
                                            max_new_tokens=R, temperature=0.8)
    outputs          = rollout_result.output_ids       # [B*N, P+R]
    completion_ids   = rollout_result.completion_ids   # [B*N, R]
    completions      = rollout_result.completions      # B*N 条文本
    old_per_token_logps = rollout_result.per_token_logps.detach()  # 采样时刻的 logp
    prompt_lens      = rollout_result.prompt_lens      # [B*N]

    # 2. 当前策略与参考策略的逐 token logp
    per_token_logps     = log_softmax(model(outputs))  # 当前策略（带梯度）
    ref_per_token_logps = log_softmax(ref(outputs))    # 冻结参考（无梯度）

    # 3. 打奖励 + 组内归一化优势
    rewards = calculate_rewards(prompts, completions, reward_model)  # [B*N]
    grouped = rewards.view(B, N)
    mean_r  = grouped.mean(1).repeat_interleave(N)
    std_r   = grouped.std(1, unbiased=False).repeat_interleave(N)
    advantages = (rewards - mean_r) / (std_r + 1e-4)   # [B*N]

    # 4. 构造 completion_mask（下一步 loss 用）
    ...
```

后半段（ratio、loss、更新）放到 4.3、4.4 讲。

#### 4.2.3 源码精读

**采样调用**：[trainer/train_grpo.py:L80-L91](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_grpo.py#L80-L91) — 把 prompt 喂给 rollout 引擎，每条 prompt 出 `num_generations`（默认 6）条回答。注意 `old_per_token_logps` 取自 rollout 结果并 `.detach()`，因为它是「行为策略」的固定参照点，不应回传梯度。

`full_mask` 与 `logp_pos` 的构造很关键（[L92-L93](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_grpo.py#L92-L93)）：

```python
full_mask = (outputs != tokenizer.pad_token_id).long()
logp_pos = prompt_lens.unsqueeze(1) - 1 + torch.arange(completion_ids.size(1)).unsqueeze(0)
```

- `full_mask`：把 padding 位置从注意力里排除。
- `logp_pos`：回答中第 \(k\) 个 token（全局位置 \(P+k\)）是由**前一位**的 logit 预测的（位置 \(P+k-1\)），所以取 `prompt_len - 1 + k`。这一行就是「logit 预测下一个 token」的位移在 gather 索引上的体现（u3-l5 讲过位移交叉熵，这里是同样的原理，只是要精确夹出回答段）。

**当前/参考策略的逐 token logp**（[L97-L104](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_grpo.py#L97-L104)）：

```python
with autocast_ctx:
    res = model_unwrapped(outputs, attention_mask=full_mask)
    per_token_logps = F.log_softmax(res.logits[:, :-1, :], dim=-1) \
        .gather(2, outputs[:, 1:].unsqueeze(-1)).squeeze(-1).gather(1, logp_pos)
with torch.no_grad():
    ref_per_token_logps = F.log_softmax(ref_model(outputs, ...).logits[:, :-1, :], dim=-1) \
        .gather(2, outputs[:, 1:].unsqueeze(-1)).squeeze(-1).gather(1, logp_pos)
```

两次 `gather`：第一次沿词表维取「真实下一 token」的 log 概率，第二次沿序列维用 `logp_pos` 只保留回答段的 \(R\) 个位置。`ref_model` 在 `no_grad` 下前向——参考策略只提供 KL 锚点，不更新。

**组内归一化优势**：[trainer/train_grpo.py:L121-L124](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_grpo.py#L121-L124)

```python
grouped_rewards = rewards.view(-1, args.num_generations)            # [B, N]
mean_r = grouped_rewards.mean(dim=1).repeat_interleave(args.num_generations)   # [B*N]
std_r  = grouped_rewards.std(dim=1, unbiased=False).repeat_interleave(args.num_generations)
advantages = (rewards - mean_r) / (std_r + 1e-4)                    # [B*N]
```

`view(-1, N)` 之所以成立，正得益于 4.1 强调的「prompt-外层、generation-内层」排列——同一 prompt 的 \(N\) 条回答在 `rewards` 里相邻。`repeat_interleave(N)` 把每个组内标量广播回 \(N\) 个位置，使 `advantages` 与 `rewards` 形状对齐。注意分母用 `std + 1e-4`（不是 u7-l3 概括里的 `σ+ε` 取较大值），这里只做轻微平滑——这也意味着一旦 \(\sigma\) 真的塌缩，优势仍会很小，退化组风险依旧存在，需靠 `Adv Std` 监控。

**completion_mask 构造**：[trainer/train_grpo.py:L126-L130](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_grpo.py#L126-L130)

```python
completion_pad_mask = rollout_result.completion_mask.to(args.device).bool()
is_eos = (completion_ids == tokenizer.eos_token_id) & completion_pad_mask
eos_idx = torch.full((is_eos.size(0),), is_eos.size(1) - 1, ...)
eos_idx[is_eos.any(dim=1)] = is_eos.int().argmax(dim=1)[is_eos.any(dim=1)]
completion_mask = ((torch.arange(R).expand(B*N, -1) <= eos_idx.unsqueeze(1)) & completion_pad_mask).int()
```

这段做了两件事：① 找到每条回答**第一个 EOS** 的位置 `eos_idx`（没找到则回退到序列末尾）；② 生成 mask，只保留「非 padding」且「不晚于第一个 EOS（含 EOS 本身）」的位置。也就是说：**EOS 之后的 token（往往是续写的废话或 padding）一律不计入 loss。** 这是 GRPO 里非常容易出错的一处工程细节，少了它会把噪声梯度灌进模型。

> 关于 `old_per_token_logps` 的来源：它在 `TorchRolloutEngine.rollout` 里由 [rollout_engine.py:L88](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/rollout_engine.py#L88) 调用 `compute_per_token_logps` 当场算好（u7-l2 详讲）。因此在本实现里，`old_logps` 与本轮 `per_token_logps` 来自**同一份权重**，二者几乎相等——这一点会直接影响 4.3 对 `ratio` 的理解。

#### 4.2.4 代码实践（源码阅读型）

**目标**：亲手验证 `logp_pos` 的位移逻辑，理解「为什么是 `prompt_len - 1 + k`」。

**步骤**：

1. 阅读并对比两处 gather：
   - 采样侧回算：[rollout_engine.py:L24-L36](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/rollout_engine.py#L24-L36)（`compute_per_token_logps`，用 `logits_to_keep` 切片）
   - 训练侧前向：[train_grpo.py:L101](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_grpo.py#L101)（全量 logits 后 gather）
2. 回答：假设 `prompt_len=5`、`completion_len=3`，回答 token 在全局位置 5、6、7。它们分别由哪几个位置的 logit 预测？`logp_pos` 应等于哪三个值？

**预期结果**：位置 5/6/7 的 token 分别由位置 4/5/6 的 logit 预测，故 `logp_pos = [5-1+0, 5-1+1, 5-1+2] = [4, 5, 6]`。这正是「预测下一位」的位移。

#### 4.2.5 小练习与答案

**Q1**：把 `num_generations` 从 6 调到 2，优势估计会有什么变化？
**答**：组内只有 2 个样本，\(\mu,\sigma\) 估计方差变大，优势噪声显著增大；极端情况下两条回答奖励相同会让 \(\sigma=0\) 直接退化。增大 \(N\) 能让 baseline 更稳，代价是采样开销线性增长。

**Q2**：`std` 用了 `unbiased=False`（有偏），为什么？
**答**：组内样本量 \(N\) 较小，无偏估计会除以 \(N-1\)，在 \(N\) 小时偏低；这里更想要一个稳定的「尺度」，用有偏的 \(1/N\) 更符合工程直觉，且与大多数 GRPO 开源实现一致。

---

### 4.3 loss_type 分支：GRPO 的 PPO-clip 与 CISPO 的无梯度截断

#### 4.3.1 概念说明

这是本讲的核心模块。两种 loss 都建立在三个共同量之上：

- **ratio（重要性比）**：\(r_t = \exp(\log\pi_\theta(a_t) - \log\pi_{\text{old}}(a_t))\)，衡量「当前策略」相对「采样时策略」对该 token 的概率放大倍数。
- **per-token KL（正则项）**：用 Schulman 的 k3 估计 \(\widehat{\text{KL}}(\pi_{\text{ref}}\|\pi_\theta)\)，始终非负：

\[
\widehat{\text{KL}}_t = \exp(\log\pi_{\text{ref}} - \log\pi_\theta) - (\log\pi_{\text{ref}} - \log\pi_\theta) - 1
\]

- **advantages**：4.2 算出的组内归一化优势 \(A\)。

**GRPO 分支**采用经典 PPO clip：

\[
\mathcal{L}_{\text{GRPO}} = -\mathbb{E}\Big[\min\big(r_t A,\ \text{clip}(r_t,1-\varepsilon,1+\varepsilon)\,A\big) - \beta\,\widehat{\text{KL}}_t\Big]
\]

`min` 起到「悲观」作用：当 \(A>0\) 且 \(r\) 已经很大时，不再奖励继续放大；当 \(A<0\) 且 \(r\) 很小时，不再奖励继续缩小。问题在于：一旦 `clip` 把 \(r\) 钳成常数，整个 `min` 项失去对 \(\theta\) 的依赖，**该 token 的策略梯度被硬截断为 0**。

**CISPO 分支**改写策略项：

\[
\mathcal{L}_{\text{CISPO}} = -\mathbb{E}\Big[\underbrace{\min(r_t,\varepsilon_{\max})}_{\text{detach，仅作权重}}\cdot A \cdot \log\pi_\theta(a_t) \;-\; \beta\,\widehat{\text{KL}}_t\Big]
\]

关键差别：① `clamp(ratio)` 只设**上界** \(\varepsilon_{\max}\)（默认 5.0），不设下界；② 它被 `.detach()`，当作固定的逐 token 权重；③ 真正带梯度的是 \(\log\pi_\theta\)。这样一来，即使 `ratio` 被夹住，梯度仍沿 \(\log\pi_\theta\) 流动——**权重被约束，但学习不被切断**。这正是论文标题「Clipped Importance Sampling」的精髓，也对应 README 里 CISPO 的策略项 \(f(r_t)=\mathrm{clip}(r,0,\varepsilon_{\max})\cdot A\cdot\log\pi_\theta\)。

> **统一视角对照（承接 u7-l1）**：GRPO 与 CISPO 的优势项 \(g(A)\) 完全相同（都是组内归一化），正则项 \(h(\text{KL})\) 也相同（都是 \(\beta\cdot\text{KL}_t\)），**唯一区别在策略项 \(f(r)\)**：GRPO 是 `min(r, clip(r))·A`（梯度可能被 clip 切断），CISPO 是 `clamp(r).detach()·A·log π`（梯度永不断）。

#### 4.3.2 核心流程

```
kl_div      = ref_per_token_logps - per_token_logps
per_token_kl= exp(kl_div) - kl_div - 1                 # k3 估计，≥0，[B*N, R]
ratio       = exp(per_token_logps - old_per_token_logps)  # [B*N, R]

if loss_type == "cispo":
    clamped_ratio = clamp(ratio, max=epsilon_high).detach()      # 仅上界、作权重
    per_token_loss = -(clamped_ratio * A * per_token_logps - beta * per_token_kl)
else:  # grpo
    clipped_ratio  = clamp(ratio, 1-epsilon, 1+epsilon)
    per_token_loss = -(min(ratio*A, clipped_ratio*A) - beta * per_token_kl)

policy_loss = mean_over_batch( sum(per_token_loss * completion_mask, dim=1)
                               / sum(completion_mask, dim=1).clamp(min=1) )
loss = (policy_loss + aux_loss) / accumulation_steps
```

#### 4.3.3 源码精读

三种量的计算与分支判定：[trainer/train_grpo.py:L132-L143](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_grpo.py#L132-L143)

```python
kl_div = ref_per_token_logps - per_token_logps
per_token_kl = torch.exp(kl_div) - kl_div - 1                 # k3 KL 估计
ratio = torch.exp(per_token_logps - old_per_token_logps)
if args.loss_type == "cispo":
    clamped_ratio = torch.clamp(ratio, max=args.epsilon_high).detach()
    per_token_loss = -(clamped_ratio * advantages.unsqueeze(1) * per_token_logps - args.beta * per_token_kl)
else:
    clipped_ratio = torch.clamp(ratio, 1 - args.epsilon, 1 + args.epsilon)
    per_token_loss1 = ratio * advantages.unsqueeze(1)
    per_token_loss2 = clipped_ratio * advantages.unsqueeze(1)
    per_token_loss = -(torch.min(per_token_loss1, per_token_loss2) - args.beta * per_token_kl)
policy_loss = ((per_token_loss * completion_mask).sum(dim=1) / completion_mask.sum(dim=1).clamp(min=1)).mean()
loss = (policy_loss + aux_loss) / args.accumulation_steps
```

逐行对照概念：

- **`per_token_kl`**（L133）：对应公式 \(e^x - x - 1\)，其中 \(x = \log\pi_{\text{ref}} - \log\pi_\theta\)。函数 \(g(x)=e^x-x-1\) 在 \(x=0\) 时为 0、始终 \(\geq 0\)，且在 0 附近可微，是 KL 的无偏、非负估计（Schulman k3）。注意参数顺序是「ref − policy」，即估计 \(\text{KL}(\pi_{\text{ref}}\|\pi_\theta)\)。
- **`ratio`**（L134）：注意 `old_per_token_logps` 已 detach，所以 ratio 的梯度只来自 `per_token_logps`。
- **CISPO 分支**（L135-L137）：`clamp(ratio, max=epsilon_high)` 只夹上界，且 `.detach()` 让它成为纯权重；带梯度的是 `per_token_logps`（即 \(\log\pi_\theta\)）。
- **GRPO 分支**（L138-L142）：标准 PPO clip，`min(ratio·A, clipped_ratio·A)`。
- **`policy_loss`**（L143）：先按序列对有效 token 求和再除以该序列有效长度（`completion_mask.sum`），得到「每条回答的 token 平均损失」，再对 batch 求均值。这是一种**按序列归一化**的 reduction，避免长回答主导 loss。

对应的命令行参数：[trainer/train_grpo.py:L226-L230](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_grpo.py#L226-L230)

```python
parser.add_argument("--num_generations", type=int, default=6, ...)   # 组大小 N
parser.add_argument("--beta", type=float, default=0.1, ...)           # KL 系数 β
parser.add_argument("--loss_type", type=str, default="cispo", choices=["grpo", "cispo"], ...)
parser.add_argument("--epsilon", type=float, default=0.2, ...)        # GRPO 的 clip ε
parser.add_argument("--epsilon_high", type=float, default=5.0, ...)   # CISPO 的上界 ε_max
```

**一个值得注意的默认值**：`--loss_type` 默认是 `"cispo"`，即 MiniMind 当前推荐的 RLAIF 主线算法是 CISPO，而非 GRPO。README 也明确说「把 CISPO 视作 GRPO 的 loss 变体来实现，而不是单独维护一套独立脚本」。

> **深入思考（高级）**：在本实现里，每个 step 先 `rollout`（用权重 \(W_t\) 采样并当场回算 `old_logps`），随后立刻用**同一份** \(W_t\) 前向得到 `per_token_logps`，二者之间没有任何权重更新。因此 \(r_t = \exp(\log\pi_{W_t} - \log\pi_{W_t}) \approx 1\)，`clip(1, 0.8, 1.2) = 1`，GRPO 的 `min` 项几乎不激活。也就是说，**在「每步一次 rollout、一次更新」的设计下，GRPO 与 CISPO 的数值表现会非常接近**；ratio/clip 机制的价值体现在「对同一批 rollout 做多轮更新」或「梯度累积中途权重已变」的场景——那时 ratio 才会真正偏离 1、clip 才会真正起作用。这是阅读本段代码必须建立的现实预期，也解释了为什么 4.3.4 的对比实验可能看到「两条曲线高度相似」。

#### 4.3.4 代码实践（可运行 · 本讲主实践）

**目标**：在同样的数据与初始权重下，分别跑 GRPO 与 CISPO 若干步，对比 `Reward / Actor Loss / KL_ref` 曲线，并验证「ratio≈1 → 二者接近」的判断。

**前置准备**：

1. 已按 u5-l2 训练好 `full_sft_768.pth`（GRPO 的 `--from_weight full_sft`）。
2. 下载 InternLM2-1.8B-Reward 到 `../../internlm2-1_8b-reward`（`--reward_model_path` 默认值），详见 u7-l3。
3. 确认 `../dataset/rlaif.jsonl` 就位。

**操作步骤**（在 `trainer/` 目录下执行）：

```bash
# 1) CISPO（默认）
python train_grpo.py \
    --loss_type cispo --from_weight full_sft \
    --epochs 1 --batch_size 2 --num_generations 6 \
    --save_interval 20 --log_interval 1 --debug_mode

# 2) GRPO
python train_grpo.py \
    --loss_type grpo --from_weight full_sft \
    --epochs 1 --batch_size 2 --num_generations 6 \
    --save_interval 20 --log_interval 1 --debug_mode
```

**需要观察的现象**（日志格式见 [train_grpo.py:L164-L167](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_grpo.py#L164-L167)）：

- `Reward` 是否随 step 上升（RL 有效的首要信号）。
- `KL_ref` 是否维持在较小正数（说明策略没跑离 SFT 太远；若快速膨胀，应调大 `--beta`）。
- `Adv Std` 是否显著大于 0（若趋近 0，说明出现退化组，参考 u7-l3 的对策）。
- `Actor Loss`（即 `policy_loss`）的整体走势。

**预期结果**：

- 两种 loss 的 `Reward` 曲线都应缓慢上升，CISPO 略稳；`KL_ref` 在 0 附近小幅波动。
- 鉴于 4.3.3 末尾的分析，两条曲线**可能高度相似**——这本身就是一个有价值的发现，说明在单次 rollout 设计下 clip 未激活。
- 若你把 `--accumulation_steps` 调大或修改代码让一次 rollout 被多次更新（制造 ratio 漂移），则会看到 GRPO 在 clip 激活处梯度被截断、CISPO 仍持续学习的差异。

**待本地验证**：实际 reward 绝对值与上升斜率取决于 RM 与算力；若显存不足，先调小 `--batch_size` / `--num_generations` / `--max_gen_len`。

#### 4.3.5 小练习与答案

**Q1**：`per_token_kl` 为什么用 \(e^x - x - 1\) 而不是直接用 \((\log\pi_{\text{ref}} - \log\pi_\theta)\)？
**答**：后者（对数概率差）可正可负、不是真正的 KL，无法保证非负、不能稳定地惩罚偏离。\(e^x-x-1\) 是 \(g(x)\) 在 \(x=\log(p/q)\) 时的取值，是 \(\text{KL}(p\|q)\) 的非负、无偏估计（k3），更适合作正则项。

**Q2**：CISPO 的 `clamped_ratio` 为什么要 `.detach()`？不 detach 会怎样？
**答**：CISPO 的设计意图是「ratio 只当权重、梯度走 \(\log\pi\)」。若不 detach，梯度会同时流过 `clamp(ratio)`，把 ratio 的导数 \(r_t\cdot\partial\log\pi/\partial\theta\) 也叠进去，偏离 CISPO 的原意，行为退回接近 PPO 的形式。

**Q3**：GRPO 与 CISPO 的优势项、正则项分别是什么？为何说它们只是策略项不同？
**答**：优势项都是组内归一化 \((R-\mu)/(\sigma+\epsilon)\)，正则项都是 \(\beta\cdot\widehat{\text{KL}}_t\)。区别只在策略项：GRPO 是 `min(r, clip(r))·A`，CISPO 是 `clamp(r).detach()·A·logπ`——前者 clip 会切断梯度，后者不会。

---

### 4.4 grpo_train_epoch（下）：梯度更新、日志、保存与权重同步

#### 4.4.1 概念说明

算出 `loss` 只是「半步」，完整的训练 step 还要：反向传播 → 梯度累积 → 梯度裁剪 → 优化器步进 → 学习率调度 → 清零梯度。这一套与 u4-l3 的训练底座**几乎逐行同构**，区别只在三处：① loss 来源是 policy loss + aux loss；② 每个 save 节点要把新权重 `update_policy` 同步回 rollout 引擎；③ GRPO 用的是 `CosineAnnealingLR`（而 pretrain/SFT 用手写的 `get_lr`）。

另外要理解日志里 `KL_ref` 的定义——它与 loss 里的 `per_token_kl` **不是同一个量**：日志里的 `KL_ref` 只是「ref 与 policy 对数概率差的均值」\(\overline{\log\pi_{\text{ref}}-\log\pi_\theta}\)，可正可负，仅用于监控策略相对 ref 的整体偏移方向；loss 里的 `per_token_kl` 才是非负的 k3 估计。

#### 4.4.2 核心流程

```
loss.backward()                                       # 反向，梯度累积到 .grad
if step % accumulation_steps == 0:                    # 攒够一个有效 batch
    if grad_clip > 0: clip_grad_norm_(model.parameters(), grad_clip)
    optimizer.step(); scheduler.step(); optimizer.zero_grad()

# 日志（每 log_interval 步）
打印 Reward / KL_ref / Adv Std / Adv Mean / Actor Loss / Avg Response Len / LR
wandb.log(...)                                        # 仅主进程

# 保存（每 save_interval 步，仅主进程）
保存 grpo_{hidden_size}{_moe?}.pth + lm_checkpoint 续训点
rollout_engine.update_policy(model)                   # 把新权重同步给采样引擎
```

#### 4.4.3 源码精读

**反向与优化器步进**：[trainer/train_grpo.py:L145-L152](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_grpo.py#L145-L152)

```python
loss.backward()
if step % args.accumulation_steps == 0:
    if args.grad_clip > 0:
        torch.nn.utils.clip_grad_norm_(model.parameters(), args.grad_clip)
    optimizer.step()
    scheduler.step()
    optimizer.zero_grad()
```

更新顺序 `clip → step → scheduler.step → zero_grad` 与 u4-l3 一致（GRPO 用了正式的 `scheduler`，所以多一步 `scheduler.step()`）。

**日志与监控量**：[trainer/train_grpo.py:L154-L167](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_grpo.py#L154-L167)，重点看 `KL_ref` 的算法（[L159](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_grpo.py#L159)）：

```python
kl_ref_val = ((ref_per_token_logps - per_token_logps) * completion_mask).sum().item() \
             / max(completion_mask.sum().item(), 1)
```

这就是「对有效 token 取 \(\log\pi_{\text{ref}}-\log\pi_\theta\) 的均值」——一个**朴素、可正可负**的监控量，用来快速判断策略整体是偏向 ref（负）还是偏离 ref（正）。别把它与 loss 里的 k3 `per_token_kl` 混淆。

**保存与权重同步**：[trainer/train_grpo.py:L180-L193](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_grpo.py#L180-L193)。这里有两件事：

1. 主进程把模型存成 `grpo_{hidden_size}{_moe?}.pth`（推理权重，fp16）+ 调 `lm_checkpoint` 存续训点（u4-l2）。
2. **`rollout_engine.update_policy(model)`**（[L193](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_grpo.py#L193)）：这是训推分离的关键一步。对 `TorchRolloutEngine`，它只是把 `self.policy_model` 换成新引用（[rollout_engine.py:L94-L95](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/rollout_engine.py#L94-L95)，零成本）；对 `SGLangRolloutEngine`，它要把权重落盘并 HTTP 通知推理服务热加载（[rollout_engine.py:L175-L194](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/rollout_engine.py#L175-L194)）。注意 `update_policy` 在 `if save_interval` 分支里——也就是说**并非每步都同步**，而是每隔 `save_interval` 步同步一次，期间 rollout 用的是「略微滞后」的策略。这在本实现里安全，因为 ratio≈1（见 4.3.3）。

**`__main__` 装配要点**（[L271-L298](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_grpo.py#L271-L298)）：

```python
model, tokenizer   = init_model(lm_config, base_weight, device=...)   # 策略模型
ref_model, _       = init_model(lm_config, base_weight, device=...)   # 参考模型（同初始权重）
ref_model          = ref_model.eval().requires_grad_(False)            # 冻结
reward_model       = LMForRewardModel(args.reward_model_path, ...)     # 奖励模型
rollout_engine     = create_rollout_engine(engine_type=args.rollout_engine, ...)
...
scheduler = CosineAnnealingLR(optimizer, T_max=total_optimizer_steps, eta_min=args.learning_rate/10)
```

三个模型各司其职：策略模型（待更新）、参考模型（冻结、提供 KL 锚点）、奖励模型（打分）。注意 `ref_model` 与 `model` 用**同一个** `base_weight` 初始化——RL 的起点就是「不破坏 SFT 成果」，KL 正则保证策略不会跑离这个起点太远。学习率默认 `3e-7`（[L212](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_grpo.py#L212)），远小于 SFT 的 `1e-5`，因为 RL 阶段只做「微调对齐」，不能大步重写已学到的能力。

#### 4.4.4 代码实践（源码阅读型）

**目标**：理解 `update_policy` 的两种实现差异，搞清「换后端只改一个参数」是如何做到的。

**步骤**：

1. 读 [rollout_engine.py:L94-L95](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/rollout_engine.py#L94-L95)（Torch 后端的 `update_policy`）与 [rollout_engine.py:L175-L194](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/rollout_engine.py#L175-L194)（SGLang 后端的 `update_policy`）。
2. 回答：为什么 `grpo_train_epoch` 里调用 `rollout_engine.update_policy(model)` 时不需要写 `if engine == 'torch': ... else: ...`？

**预期结果**：因为 `RolloutEngine` 是抽象基类（[rollout_engine.py:L51-L60](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/rollout_engine.py#L51-L60)），`TorchRolloutEngine` 与 `SGLangRolloutEngine` 都实现了同名 `update_policy`。训练脚本面向接口编程，具体差异被多态封装——这正是 u7-l2「训推分离、后端可插拔」的收益。切换只需 `--rollout_engine sglang`。

#### 4.4.5 小练习与答案

**Q1**：日志里的 `KL_ref` 与 loss 里的 `per_token_kl` 有何区别？
**答**：`KL_ref` 是 \(\overline{\log\pi_{\text{ref}}-\log\pi_\theta}\)，可正可负，仅作监控；`per_token_kl` 是 \(e^x-x-1\)，非负、用于 loss 的正则项。

**Q2**：为什么 GRPO 的学习率（3e-7）比 SFT（1e-5）小这么多？
**答**：RL 阶段是基于 SFT 成果做「偏好对齐」，目的是小幅修正行为而非重学语言能力；学习率过大会破坏已有能力（灾难性遗忘），同时 KL 正则也要求策略不能偏离 ref 太远，两者都要求小步更新。

---

## 5. 综合实践

把本讲四块知识串起来，完成一次「**带监控的 GRPO/CISPO 最小复现 + 曲线解读**」：

1. **准备**：确保 `full_sft_768.pth`、`rlaif.jsonl`、InternLM2-Reward 就位。
2. **跑两组实验**（在 `trainer/` 下）：
   - A 组：`python train_grpo.py --loss_type grpo --debug_mode --log_interval 1 --epochs 1`
   - B 组：`python train_grpo.py --loss_type cispo --debug_mode --log_interval 1 --epochs 1`
3. **边跑边对照源码标注**：在 [train_grpo.py:L71-L203](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_grpo.py#L71-L203) 上，用四种颜色分别标出「采样→奖励→优势→loss」四段，确认你能把日志里每个数字对应到代码里的某个变量。
4. **解读**（写一段小结）：
   - `Reward` 是否上升？`Adv Std` 是否健康（没有塌到 0）？
   - `KL_ref` 量级如何？是否需要调 `--beta`？
   - A、B 两组曲线是否如 4.3.3 预测的那样「高度相似」？若相似，请用「ratio≈1、clip 未激活」解释；若不同，排查是否 `accumulation_steps>1` 制造了 ratio 漂移。
5. **产物**：用 `eval_llm.py --weight grpo`（CISPO 训出的权重默认名也是 `grpo_*`）对比 SFT 模型，主观感受 RL 后回答在「格式/长度/思考闭合」上的变化。

> 提示：若全程无法跑训练（缺卡或缺 RM），可降级为「源码阅读型综合实践」——只做第 3、4 步，把日志格式的预期数值范围用注释写在源码旁，作为后续真跑时的对照表。

## 6. 本讲小结

- **GRPO 的核心简化**是用同一 prompt 的 \(N\) 条采样的组内均值/方差作 baseline（\(A=(R-\mu)/(\sigma+\epsilon)\)），**完全不用 Critic 价值网络**，代价是存在退化组风险（需监控 `Adv Std`）。
- **`completion_mask`** 把「第一个 EOS 之后」与「padding」位置剔除出 loss，是 GRPO 工程实现里最容易出错、也最关键的一步。
- **ratio = exp(logp − old_logp)**、**PPO clip（GRPO 分支）**、**k3 估计的 token 级 KL** 三者共同构成 GRPO 损失；其中 `per_token_kl = exp(x) − x − 1` 非负，是 \(\text{KL}(\pi_{\text{ref}}\|\pi_\theta)\) 的无偏估计。
- **CISPO** 把策略项改写成 `clamp(ratio).detach()·A·logπ`——被 clip 的 ratio 只当权重，梯度从 \(\log\pi\) 持续流过，**避免 PPO clip 切断梯度**；它与 GRPO 仅策略项不同，故能作为 `loss_type` 变体一键切换（且是默认值）。
- 在本实现「每步一次 rollout、一次更新」的设计下，`old_logps` 与本轮 `per_token_logps` 同源，**ratio≈1、clip 基本不激活**，因此 GRPO 与 CISPO 数值表现接近；ratio/clip 的真正价值在「rollout 复用 / 梯度累积中途权重已变」的场景。
- 三模型分工：**策略模型**（待更新）、**参考模型**（冻结、同初始、供 KL 锚点）、**奖励模型**（打分）；学习率 3e-7 远小于 SFT，体现「RL 只做小幅对齐」。

## 7. 下一步学习建议

- **继续 RL 主线**：进入 [u7-l5 PPO 与 GAE](#)，对比 GRPO「无 Critic」与 PPO「Actor-Critic + GAE 优势估计」的差异，理解 PPO 为何 reward 提升更慢、显存约为 1.5–2 倍。
- **多轮与延迟奖励**：阅读 [u7-l6 Agentic RL](#)，看 `train_agent.py` 如何把单轮 rollout 扩展成「生成 tool_call → 执行工具 → 拼回 observation → 续写」的多轮循环，并把整轮延迟奖励复用本讲的 GRPO/CISPO loss。
- **源码延伸**：若想进一步理解 ratio/clip 真正起作用的场景，可尝试修改 `train_grpo.py`，让一次 rollout 的结果被多次 mini-batch 更新（类似 PPO 的 `ppo_update_iters`），观察 `ratio` 如何随更新轮次偏离 1、GRPO 与 CISPO 的梯度差异如何显现。
