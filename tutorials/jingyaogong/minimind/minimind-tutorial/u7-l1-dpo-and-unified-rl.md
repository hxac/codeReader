# DPO 偏好优化与 Policy Optimization 统一视角

## 1. 本讲目标

经过 SFT（[u5-l2](u5-l2-full-sft.md)）之后，模型已经「会按对话模板回答」，但它还不知道**哪种回答更好**——它只会接龙，缺少「正例 / 反例」的激励。本讲要解决的就是这个问题：如何用一份「好回答 vs 差回答」的偏好数据，把模型往人类更偏爱的方向再推一步。

本讲学完后你应该能够：

1. 说清 DPO（Direct Preference Optimization，直接偏好优化）想最大化什么——chosen 相对 rejected 的「对数几率」（log-odds）。
2. 读懂 MiniMind 从 0 实现的两个核心函数 `logits_to_log_probs` 与 `dpo_loss`，并能手算一个 batch 里 chosen/rejected 如何被拆分、如何与参考模型（ref model）做对数概率比。
3. 建立一个贯穿后续 PPO / GRPO / CISPO（[u7-l4](u7-l4-grpo-and-cispo.md)、[u7-l5](u7-l5-ppo-and-gae.md)）的**统一视角**：所有「xxPO」算法本质上都只是「策略项 + 优势项 + 正则项」三个组件的不同实例化。

本讲只讲 DPO 与统一框架的「损失这一层」，不涉及在线 rollout、奖励模型、Critic 网络——这些留待后续讲义。

---

## 2. 前置知识

阅读本讲前，请确认你已经掌握下面几个概念（前序讲义已建立）：

- **位移交叉熵（next-token prediction）**：模型在第 t 步用 `input_ids[:t]` 预测 `input_ids[t]`，训练时把 logits 与错位一位的 labels 做交叉熵。见 [u3-l5](u3-l5-forward-and-loss.md)。
- **logits 与 log_softmax**：logits 是词表上未归一化的分数；`log_softmax(logits)` 把它变成「对数概率」，所有 token 的对数概率之和为 0（概率和为 1）。
- **loss_mask / -100 掩码**：SFT 里只对 assistant 回答段算 loss，提问与 padding 被屏蔽。见 [u2-l2](u2-l2-datasets-and-labels.md)。
- **训练循环骨架**：前向 → `loss / accumulation_steps` → backward → 每 N 步 `unscale → clip → step → update → zero_grad`。见 [u4-l3](u4-l3-ddp-and-amp.md)、[u5-l1](u5-l1-pretrain.md)。
- **chat_template 的 `<|im_start|>assistant\n` / `<|im_end|>\n` 锚点**：见 [u2-l1](u2-l1-tokenizer-and-chat-template.md)。本讲 DPO 数据正是靠这两个锚点定位「回答段」。

还需要两个本讲用到、但属于强化学习常识的术语：

- **参考模型（reference model, ref / \(\pi_{\text{ref}}\)）**：一个被冻结、不更新的模型，通常就是 SFT 出来的 `full_sft` 权重。它的作用是充当「参照系」，防止策略模型 \(\pi_\theta\) 为了迎合偏好而跑得太偏。
- **概率比 / 对数概率比（ratio / log-ratio）**：\(r=\dfrac{\pi_\theta(y|x)}{\pi_{\text{ref}}(y|x)}\)，衡量「当前策略生成这个回答的概率，比参考模型高还是低」。取对数后 \(\log r=\log\pi_\theta(y|x)-\log\pi_{\text{ref}}(y|x)\)，这就是本讲反复要算的量。

> 关键直觉：DPO 不训练任何 Reward / Value 模型，它把「偏好」直接翻译成一个可微的损失——只要 chosen 的「对数概率比」比 rejected 的高，loss 就低。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到哪部分 |
|------|------|----------------|
| [trainer/train_dpo.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_dpo.py) | DPO 训练脚本，从 0 用 PyTorch 原生实现 | `logits_to_log_probs`、`dpo_loss`、`train_epoch`、main 中 ref_model 的初始化 |
| [dataset/lm_dataset.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py) | 各阶段 Dataset | `DPODataset`（chosen/rejected 双回答）与 `generate_loss_mask`（标出回答段） |
| [README.md](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md) | 项目说明 | 「PO 算法的统一视角」「6.1 Direct Preference Optimization」与末尾的「RL 小结」对照表 |

本讲围绕 4 个最小模块展开：偏好数据与参考模型、`logits_to_log_probs`、`dpo_loss`、PO 统一框架。

---

## 4. 核心概念与源码讲解

### 4.1 偏好数据与参考模型：DPO 的输入与「参照系」

#### 4.1.1 概念说明

DPO 的训练数据不再是「一问一答」，而是「一问 + 两个答」：同一个 prompt 下，有一个**更好**的回答 `chosen`（记 \(y_w\)，w = win）和一个**更差**的回答 `rejected`（记 \(y_l\)，l = lose）。

DPO 的训练目标，用一句话说就是：

> 让当前策略 \(\pi_\theta\) 生成 chosen 的相对概率，**比**生成 rejected 的相对概率**更高**。这里的「相对」是相对参考模型 \(\pi_{\text{ref}}\) 而言的。

为什么要有参考模型？因为如果只追求「让 chosen 概率高、rejected 概率低」，模型可以把 chosen 的概率推到 1、把其它所有回答的概率压到 0，变成一个只会背 chosen 的「过拟合复读机」。引入参考模型做参照系，相当于约定：**你可以在参考模型的基础上调整，但不能偏离太远**。这个「偏离程度」由超参 \(\beta\) 控制——\(\beta\) 越大约束越严、越保守。

工程上的关键点：参考模型和策略模型**结构完全相同、初始权重完全相同**（都从 `full_sft` 加载），唯一区别是参考模型被 `eval()` + `requires_grad_(False)` 冻结，前向时用 `torch.no_grad()`。

#### 4.1.2 核心流程

```text
dpo.jsonl 的一条样本
  ├── chosen:    [{role: user, ...}, {role: assistant, content: 好回答}]
  └── rejected:  [{role: user, ...}, {role: assistant, content: 差回答}]

DPODataset.__getitem__ 对每条样本产出 6 个张量：
  x_chosen   = chosen_input_ids[:-1]     # 模型输入（位移）
  y_chosen   = chosen_input_ids[1:]      # 预测目标（位移）
  mask_chosen= chosen_loss_mask[1:]      # 仅回答段为 1
  （rejected 同上，共 6 个）

train_epoch 里把 chosen 与 rejected 在 batch 维度拼接，一次前向算两组：
  x     = cat([x_chosen,   x_rejected  ])     # batch 维翻倍
  y     = cat([y_chosen,   y_rejected  ])
  mask  = cat([mask_chosen, mask_rejected])

  ref_logits   = ref_model(x)        # no_grad
  policy_logits= model(x)
  ref_log_probs   = logits_to_log_probs(ref_logits,   y)
  policy_log_probs= logits_to_log_probs(policy_logits,y)
  loss = dpo_loss(ref_log_probs, policy_log_probs, mask, beta)
       + outputs.aux_loss            # Dense 时 aux_loss=0
```

注意一个反直觉的工程细节：chosen 与 rejected **不在两个 batch 里**，而是被 `torch.cat` 拼成**一个翻倍的 batch** 一起前向。这样只需一次前向就同时拿到两组 logits，省一半 kernel 启动开销；代价是 `dpo_loss` 内部要靠「前一半是 chosen、后一半是 rejected」的约定把它们切回去（见 4.3）。

#### 4.1.3 源码精读

`DPODataset.__getitem__` 用 `apply_chat_template` 把 chosen / rejected 各自渲染成完整对话文本，再做位移与掩码：

[dataset/lm_dataset.py:135-174](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L135-L174) —— 对 chosen/rejected 各渲染模板、padding 到 `max_length`，并产出位移后的 `x = input_ids[:-1]`、`y = input_ids[1:]` 与 `mask = loss_mask[1:]`。

其中 `generate_loss_mask` 与 SFT 的 `generate_labels` 同构：用 `<|im_start|>assistant\n` 与 `<|im_end|>\n` 两个锚点夹出 assistant 段，段内（含 eos）置 1、其余置 0：

[dataset/lm_dataset.py:176-192](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L176-L192) —— 这里产出的是 0/1 的 `loss_mask`，而非 -100 的 labels；因为 DPO 不直接拿 labels 做交叉熵，而是要拿 mask 在 `dpo_loss` 里挑选需要累加的对数概率位置。

参考模型的初始化在 main 里，与策略模型共用同一份 `full_sft` 权重：

[trainer/train_dpo.py:182-189](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_dpo.py#L182-L189) —— `init_model` 被调用两次：第一次得到会训练的 `model`（策略模型），第二次得到 `ref_model`；随后 `ref_model.eval()` 与 `ref_model.requires_grad_(False)` 把它彻底冻结。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：确认 DPO 数据的 chosen/rejected 形态，以及 mask 是否只覆盖回答段。
2. **步骤**：写一段小脚本（CPU 即可），加载 `dataset/dpo.jsonl` 的一条样本，分别打印 chosen、rejected 的文本，再打印 `mask_chosen` 中 1 的位置对应的 token 文本。
3. **示例代码**（标注为「示例代码」，非项目原有）：

   ```python
   # 示例代码：在项目根目录运行
   from transformers import AutoTokenizer
   from dataset.lm_dataset import DPODataset
   ds = DPODataset('../dataset/dpo.jsonl',
                   AutoTokenizer.from_pretrained('./model'), max_length=512)
   item = ds[0]
   print("chosen 长度:", len(item['x_chosen']))
   print("mask_chosen 中为 1 的 token 数:", int(item['mask_chosen'].sum()))
   ```
4. **需要观察的现象**：`mask_chosen` 只有回答段（assistant 内容 + eos）为 1，提问与 padding 为 0；chosen 与 rejected 共享相同的前缀（同一个 user 提问），只在 assistant 内容处不同。
5. **预期结果**：若数据集为 `dpo.jsonl` 则可看到上述结构；若本地没有该文件，标注「待本地验证」，仅阅读 [dataset/lm_dataset.py:135-174](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L135-L174) 理解其构造逻辑即可。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 chosen 和 rejected 必须共享同一个 user 提问？
  - **答案**：DPO 比较的是「同一个问题下两个回答的偏好」。如果提问不同，比较的就是两个不同问题，偏好信号失去意义。
- **练习 2**：`loss_mask` 为什么要和 `x`、`y` 一起做 `[1:]` 位移？
  - **答案**：因为 `x=input_ids[:-1]`、`y=input_ids[1:]`，mask 必须与 `y` 对齐——位置 t 上的预测目标 `y[t]=input_ids[t+1]`，对应的回答段标记在 `loss_mask[t+1]`，所以 mask 也要整体后移一位。

---

### 4.2 logits_to_log_probs：把 logits 变成「每个 token 的对数概率」

#### 4.2.1 概念说明

DPO 要算的是「策略生成某个**完整回答**的对数概率」\(\log\pi_\theta(y|x)\)。一个回答由多个 token 组成，所以这等于「每个 token 对数概率之和」：

\[
\log\pi_\theta(y_{1:T}|x)=\sum_{t=1}^{T}\log\pi_\theta(y_t|x,y_{<t})
\]

而模型每次前向给出的 `logits` 形状是 `(batch, seq_len, vocab_size)`——对**每个位置**、**整个词表**都打分。我们需要做两件事：

1. 把 logits 沿词表维归一化成对数概率（`log_softmax`）。
2. 从词表维里**挑出「真实下一个 token」那一列**的对数概率（`gather`）。

这就是 `logits_to_log_probs` 的全部职责：输入 logits 与真实 labels，输出形状 `(batch, seq_len)` 的「每个位置真实 token 的对数概率」。

#### 4.2.2 核心流程

```text
logits: (B, L, V)        # B=batch, L=seq_len, V=vocab_size(=6400)
labels: (B, L)           # 真实 token id

1. log_probs = log_softmax(logits, dim=2)      # (B, L, V) 每个词的对数概率
2. log_probs_per_token = gather(log_probs, dim=2, index=labels.unsqueeze(2))
                                                # 按 labels 选列
3. squeeze(-1) → (B, L)                         # 每个 token 的对数概率
```

`gather` 是「按 index 取元素」的低级算子：`labels.unsqueeze(2)` 把 `(B,L)` 变成 `(B,L,1)`，在词表维 V 上按下标取那一列，得到 `(B,L,1)`，再 `squeeze(-1)` 回到 `(B,L)`。它等价于 `log_probs[b, t, labels[b,t]]`，但向量化、更高效。

#### 4.2.3 源码精读

[trainer/train_dpo.py:25-31](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_dpo.py#L25-L31) —— 先 `F.log_softmax(logits, dim=2)` 在词表维归一化，再 `torch.gather(..., index=labels.unsqueeze(2))` 按 labels 抽取真实 token 对应列，最后 `squeeze(-1)` 去掉末尾的尺寸为 1 的维度。

注意此处**不做位移**——因为传入的 `y` 已经在 Dataset 里做过 `[1:]` 位移（见 4.1.3）。也就是说，`logits[b,t]` 预测的就是 `y[b,t]`，二者天然对齐，函数内无需再错位。

在 `train_epoch` 中，它被分别调用两次：一次给参考模型，一次给策略模型：

[trainer/train_dpo.py:73-81](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_dpo.py#L73-L81) —— `ref_model` 在 `torch.no_grad()` 下前向得到 `ref_logits`（参考模型不参与反传）；`model` 正常前向得到策略 `logits`；两者都经 `logits_to_log_probs` 转成逐 token 对数概率。

#### 4.2.4 代码实践（可运行，CPU 即可）

1. **目标**：用随机 logits 验证 `logits_to_log_probs` 的输出形状与数值含义。
2. **步骤**（示例代码）：

   ```python
   # 示例代码
   import torch, torch.nn.functional as F
   from trainer.train_dpo import logits_to_log_probs
   torch.manual_seed(0)
   logits = torch.randn(2, 5, 8)          # batch=2, seq=5, vocab=8
   labels = torch.randint(0, 8, (2, 5))
   lp = logits_to_log_probs(logits, labels)
   print(lp.shape)                         # torch.Size([2, 5])
   # 手算一个位置验证：
   t = F.log_softmax(logits, dim=2)
   print(torch.allclose(lp, t.gather(2, labels.unsqueeze(2)).squeeze(-1)))  # True
   print(lp.min(), lp.max())               # 全部 <= 0（对数概率）
   ```
3. **现象**：输出 `(2,5)`；与手写 `gather` 结果 `allclose=True`；所有值 \(\le 0\)（因为概率 \(\le 1\)，对数概率 \(\le 0\)）。
4. **预期结果**：上述断言全部成立，说明函数就是把词表维上的「真实 token 那一列」抽出来。

#### 4.2.5 小练习与答案

- **练习 1**：为什么用 `log_softmax` 而不是先 `softmax` 再 `log`？
  - **答案**：数值稳定性。`log_softmax` 内部做了 log-sum-exp 重排，能避免大值 `exp` 溢出；先 softmax 再取 log 容易出现 0 取 log 得 \(-\infty\) 的问题。
- **练习 2**：`gather` 的 `index` 为什么要 `unsqueeze(2)`？
  - **答案**：`log_probs` 是 3 维 `(B,L,V)`，`gather` 要求 `index` 与被 gather 的张量**除目标维外其余维都相同**。`labels` 是 `(B,L)`，需扩成 `(B,L,1)` 才能在第 2 维（V）上按下标取一列。

---

### 4.3 dpo_loss：从对数概率到「偏好损失」

#### 4.3.1 概念说明

现在我们手上有了 chosen / rejected 各自的「逐 token 对数概率」，再加上 mask 把回答段圈出来。`dpo_loss` 要把它们组装成 DPO 论文里的损失：

\[
\mathcal{L}_{\text{DPO}}=-\mathbb{E}\left[\log\sigma\left(\beta\left[\log\frac{\pi_\theta(y_w|x)}{\pi_{\text{ref}}(y_w|x)}-\log\frac{\pi_\theta(y_l|x)}{\pi_{\text{ref}}(y_l|x)}\right]\right)\right]
\]

逐项翻译：

- \(\log\pi_\theta(y_w|x)\)：策略模型对 chosen **整段回答**的对数似然 = 「回答段每个 token 对数概率之和」。
- \(\log\pi_\theta(y_w|x)-\log\pi_{\text{ref}}(y_w|x)\)：chosen 的对数概率比 \(\log r_w\)，即相对参考模型，策略现在更爱 chosen 还是更不爱。
- 方括号里的整体 = \(\log r_w-\log r_l\)：chosen 的相对提升减去 rejected 的相对提升。当策略「相对参考模型」更偏爱 chosen 时，这个值为正。
- 外层 \(\log\sigma(\cdot)\)：把这个差值压成「chosen 比 rejected 更优的概率」。最小化负的它，就是在**最大化**这个概率。
- \(\beta\)：温度/约束强度，缩放括号内的差距，控制策略能偏离参考模型多远。

> 一句话：`dpo_loss` 直接最大化「chosen 优于 rejected 的对数几率」，无需训练任何 Reward / Value 模型。

#### 4.3.2 核心流程

`dpo_loss` 的实现把上面的数学**逐行**翻译成张量运算：

```text
输入：
  ref_log_probs    : (2B, L)   # 前一半 chosen、后一半 rejected（参考模型）
  policy_log_probs : (2B, L)   # 同上（策略模型）
  mask             : (2B, L)   # 仅回答段为 1
  beta             : float

1. 对每个序列，按 mask 把「回答段对数概率」求和：
   ref_log_probs    = (ref_log_probs    * mask).sum(dim=1)   # (2B,)  → log π_ref(y|x)
   policy_log_probs = (policy_log_probs * mask).sum(dim=1)   # (2B,)  → log π_θ(y|x)

2. 切开 chosen / rejected（前一半 / 后一半）：
   chosen_policy,  reject_policy  = policy[:B], policy[B:]
   chosen_ref,     reject_ref     = ref[:B],    ref[B:]

3. 算对数概率比之差：
   pi_logratios  = chosen_policy  - reject_policy     # log π_θ(y_w) - log π_θ(y_l)
   ref_logratios = chosen_ref     - reject_ref        # log π_ref(y_w) - log π_ref(y_l)
   logits        = pi_logratios - ref_logratios       # = (log r_w - log r_l)

4. 损失：
   loss = -F.logsigmoid(beta * logits)                # 标量
   return loss.mean()
```

注意第 1 步 `(x * mask).sum(dim=1)` 的含义：把序列里**回答段**（mask=1）每个 token 的对数概率加起来，得到「整段回答的对数似然」。这正是公式里的 \(\log\pi(y|x)\)。padding 与提问段（mask=0）被乘以 0 抹掉，不参与求和。

第 3 步得到的 `logits` 正是公式方括号里的内容：

\[
\text{logits}=(\log\pi_\theta(y_w)-\log\pi_\theta(y_l))-(\log\pi_{\text{ref}}(y_w)-\log\pi_{\text{ref}}(y_l))=\log r_w-\log r_l
\]

#### 4.3.3 源码精读

[trainer/train_dpo.py:34-50](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_dpo.py#L34-L50) —— 这是 DPO 的核心。`ref_log_probs = (ref_log_probs * mask).sum(dim=1)` 用 mask 选出回答段并求和；随后按 `batch_size // 2` 把 chosen（前半）与 rejected（后半）切开；`pi_logratios - ref_logratios` 得到「对数概率比之差」；最后 `-F.logsigmoid(beta * logits)` 即 DPO 损失。

这里 `batch_size = ref_log_probs.shape[0]` 实际上是 `2 × 数据 batch_size`，因为 `train_epoch` 已把 chosen / rejected 拼在了一起（见 4.1.2）。所以 `batch_size // 2` 才是真正的样本数。这是一个容易看错的点：函数内的 `batch_size` 是「chosen + rejected 总行数」。

最后在 `train_epoch` 里，把 `dpo_loss` 与 MoE 的 `aux_loss` 相加（Dense 模型 `aux_loss=0`），再除以梯度累积步数：

[trainer/train_dpo.py:83-85](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_dpo.py#L83-L85) —— `loss = dpo_loss_val + outputs.aux_loss`，随后 `loss = loss / args.accumulation_steps`，与预训练/SFT 完全同构。

#### 4.3.4 代码实践（可运行，CPU 即可）

1. **目标**：验证「当策略更偏爱 chosen 时，`dpo_loss` 更小」。
2. **步骤**（示例代码）：

   ```python
   # 示例代码
   import torch
   from trainer.train_dpo import dpo_loss
   B, L = 2, 4                      # B=2 表示 chosen+rejected 各 1 条
   mask = torch.ones(2, L)          # 简化：整段都算
   # 场景 A：策略对 chosen 给更高对数概率
   pol_A = torch.tensor([[ -0.1, -0.1, -0.1, -0.1],   # chosen（policy）
                         [ -2.0, -2.0, -2.0, -2.0]])  # rejected（policy）
   ref  = torch.tensor([[ -1.0, -1.0, -1.0, -1.0],
                        [ -1.0, -1.0, -1.0, -1.0]])
   # 场景 B：策略对 rejected 给更高对数概率（反偏好）
   pol_B = torch.tensor([[ -2.0, -2.0, -2.0, -2.0],
                         [ -0.1, -0.1, -0.1, -0.1]])
   print("偏好正确 dpo_loss:", dpo_loss(ref, pol_A, mask, beta=0.15).item())
   print("偏好相反 dpo_loss:", dpo_loss(ref, pol_B, mask, beta=0.15).item())
   ```
3. **现象**：「偏好正确」的 loss 明显小于「偏好相反」。
4. **预期结果**：偏好正确时 loss 接近 `−log σ(正数)` → 较小；偏好相反时 `−log σ(负数)` → 较大。说明损失确实在惩罚「把 rejected 排在 chosen 前面」。

#### 4.3.5 小练习与答案

- **练习 1**：`dpo_loss` 里的 `(log_probs * mask).sum(dim=1)` 为什么用 `sum` 而不是 `mean`？
  - **答案**：序列对数似然定义就是各 token 对数概率之「积」取对数 = 「和」。用 `mean` 会除以 token 数，破坏概率的乘法语义，导致长短回答不可比。
- **练习 2**：函数内 `batch_size = ref_log_probs.shape[0]`，这个 `batch_size` 和命令行 `--batch_size` 是同一个吗？
  - **答案**：不是。命令行 `--batch_size` 是「样本数（成对数）」；这里 `batch_size` 是 chosen+rejected 拼接后的「行数」，等于 `2 × --batch_size`，所以切分用 `batch_size // 2`。
- **练习 3**：把 `beta` 调大（比如 1.0）会怎样？
  - **答案**：括号内差值被放大，损失对 chosen/rejected 的概率差更敏感、梯度更大，策略更新更激进，但也更容易偏离参考模型、过拟合偏好数据；故 README 建议 `--beta` 取较小值（默认 0.15）。

---

### 4.4 PO 统一框架：策略项 / 优势项 / 正则项

#### 4.4.1 概念说明

DPO 只是 MiniMind 后训练「xxPO」家族的第一个成员。后续还有 PPO、GRPO、CISPO（[u7-l4](u7-l4-grpo-and-cispo.md)、[u7-l5](u7-l5-ppo-and-gae.md)）。这些算法看起来公式各异，但 README 用一个统一视角指出：**它们都在优化同一个期望目标，只是三个组件的「填法」不同**：

\[
\mathcal{J}_{\text{PO}}=\mathbb{E}_{q\sim P(Q),\,o\sim\pi(O|q)}\left[\underbrace{f(r_t)}_{\text{策略项}}\cdot\underbrace{g(A_t)}_{\text{优势项}}-\underbrace{h(\text{KL}_t)}_{\text{正则项}}\right]
\]

训练时最小化负目标 \(\mathcal{L}_{\text{PO}}=-\mathcal{J}_{\text{PO}}\)。三个组件的职责：

- **策略项 \(f(r_t)\)**：如何使用概率比 \(r_t=\pi_\theta/\pi_{\text{ref}}\)。回答「新旧策略偏离多少、是否探索到了更好的 token」。
- **优势项 \(g(A_t)\)**：如何计算优势 \(A_t\)（某个动作相比基线有多好）。回答「这一步到底好不好、好多少」。
- **正则项 \(h(\text{KL}_t)\)**：如何用 KL 散度约束策略变化幅度。回答「既防止跑偏、又防止管得太死」。

不同 xxPO 算法 = 这三个组件的不同实例化。把这个框架立起来，后续读 PPO/GRPO/CISPO 时就不再是被一堆公式淹没，而是带着「它三项分别怎么填」的问题去读。

#### 4.4.2 核心流程：把 DPO 填进框架

对照 README 的统一目标，DPO 的三项是：

| 组件 | DPO 的填法 | 直觉 |
|------|-----------|------|
| 策略项 \(f(r_t)\) | \(\log r_w-\log r_l\)（chosen 与 rejected 的对数概率比之差） | 不像 PPO 用 `clip(r)`，DPO 直接用 chosen/rejected 的对数比做对比 |
| 优势项 \(g(A_t)\) | 无显式优势项（通过偏好对比隐式体现） | DPO 没有 Critic、没有组内归一化，好坏信号全藏在 chosen vs rejected 里 |
| 正则项 \(h(\text{KL}_t)\) | 隐含在 \(\beta\) 中 | 不显式算 KL，而是用 \(\beta\) 缩放对参考模型的偏离 |

也就是说，DPO 把「优势」与「正则」都「折叠」进了一个紧凑的 sigmoid 损失里——这正是它**实现简单、显存占用低（只跑 actor + ref 两个模型）** 的原因，代价是**没有在线探索**（off-policy，反复用同一批静态偏好数据）。

README 末尾给出了一张贯穿全家族的对照表，建议你打开对照阅读：

[README.md:1269-1274](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L1269-L1274) —— 四种算法（DPO / PPO / GRPO / CISPO）的策略项、优势项、正则项与训练模型数一览。可以看到 PPO 的优势项是 \(R-V(s)\)（需要 Critic）、GRPO/CISPO 是组内归一化 \((R-\mu)/\sigma\)（无需 Critic），而 DPO 是「无显式优势项」。

DPO 的算法定位与局限也写得很清楚：

[README.md:948-961](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L948-L961) —— 明确 DPO 直接最大化「chosen 优于 rejected 的对数几率」，off-policy、ref 固定；局限是不做在线探索，更偏向「偏好/安全对齐」，对「能不能做对题」的智力提升有限。

统一目标函数本身的定义见：

[README.md:910-923](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L910-L923) —— 给出 \(\mathcal{J}_{\text{PO}}\) 的完整公式与三个组件的符号说明（\(r_t\)、\(A_t\)、\(\text{KL}_t\) 的含义与值域）。

#### 4.4.3 源码精读

本节主要是建立「读后续算法的视角」，源码映射如下：

- 统一目标 \(\mathcal{J}_{\text{PO}}\) 与符号表：[README.md:910-923](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L910-L923)。
- DPO 损失公式与三项填法：[README.md:948-961](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L948-L961)。
- 全家族对照表：[README.md:1269-1274](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L1269-L1274)。
- DPO 的损失**代码实现**就是上一节的 [trainer/train_dpo.py:34-50](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_dpo.py#L34-L50)：其中 `pi_logratios - ref_logratios` 对应策略项 \(\log r_w-\log r_l\)，`\beta` 对应正则项的隐含约束，优势项则以 chosen/rejected 对比隐式给出。

#### 4.4.4 代码实践（源码阅读型）

1. **目标**：把 DPO 的实现与 README 的统一 PO 表格逐项对应，建立「三项填法」的肌肉记忆。
2. **步骤**：
   - 打开 [README.md:1269-1274](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L1269-L1274) 的对照表。
   - 打开 [trainer/train_dpo.py:34-50](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_dpo.py#L34-L50) 的 `dpo_loss`。
   - 画一张三列表格：左列「三项组件」、中列「DPO 代码里哪一行」、右列「公式含义」。
3. **现象 / 预期结果**：策略项对应 `pi_logratios - ref_logratios`；正则项对应参数 `beta`（无显式 KL 行）；优势项找不到独立代码行——这正是「无显式优势项、隐含在 chosen/rejected 对比中」。能说出这一句即达标。

#### 4.4.5 小练习与答案

- **练习 1**：相比 DPO，PPO 的优势项 \(g(A_t)=R-V(s)\) 多出了什么？
  - **答案**：多出一个 Critic 价值网络来估计 \(V(s)\)，用 \(R-V(s)\) 作为优势。DPO 没有这一项，靠 chosen/rejected 对比隐式提供好坏信号，所以 DPO 不需要 Critic。
- **练习 2**：DPO 的正则项「隐含在 \(\beta\) 中」，而 GRPO 是显式 \(\beta\cdot\text{KL}_t\)。两者的差别在哪里？
  - **答案**：DPO 不显式计算 KL，而是用 \(\beta\) 缩放「相对参考模型的对数概率比」，间接约束偏离；GRPO 在 loss 里直接减去 token 级 KL 项。前者是隐式约束、实现简单，后者是显式约束、可控性更强。
- **练习 3**：用一句话概括「为什么所有 xxPO 是统一的」。
  - **答案**：因为它们都在最大化「策略项 × 优势项 − 正则项」的期望，区别只在于三项各自的实例化方式。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个**端到端小任务**（推荐先在 CPU 上跑通数值验证，再上 GPU 跑真实 DPO）。

### 任务：从一条偏好样本，手算并验证 DPO 损失，再上 GPU 跑若干步真实训练

**第 1 步：数值验证（CPU）**

写一个脚本，构造一个极小的「chosen + rejected」batch（seq_len=8，vocab=16），手工算出 `logits_to_log_probs` 与 `dpo_loss`，验证：

- `logits_to_log_probs` 输出全为非正数，且等于 `log_softmax + gather` 的结果。
- 当你把「策略对 chosen 的对数概率」调高、对 rejected 调低时，`dpo_loss` 下降；反过来则上升。

**第 2 步：阅读数据与 ref 模型（CPU 即可）**

加载 `dataset/dpo.jsonl` 一条样本，打印 chosen/rejected 文本与 `mask`，确认 mask 只覆盖 assistant 回答段；阅读 [trainer/train_dpo.py:182-189](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_dpo.py#L182-L189) 理解为何要加载两份相同权重、并把第二份冻结成参考模型。

**第 3 步：真实 DPO 训练（需 GPU + full_sft 权重）**

```bash
cd trainer
torchrun --nproc_per_node 1 train_dpo.py \
    --from_weight full_sft --hidden_size 768 \
    --epochs 1 --batch_size 2 --beta 0.15 \
    --data_path ../dataset/dpo.jsonl --log_interval 10
```

观察日志中 `dpo_loss` 的变化（参考 [trainer/train_dpo.py:96-106](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_dpo.py#L96-L106) 的日志字段 `loss / dpo_loss / aux_loss / learning_rate`）。Dense 模型 `aux_loss` 应恒为 0，故 `loss ≈ dpo_loss / accumulation_steps`。

**第 4 步：对照统一 PO 表格**

训练结束后，打开 [README.md:1269-1274](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L1269-L1274)，用你自己的话写出 DPO 的策略项、优势项、正则项分别是什么，并与代码里对应行一一指认。

> 说明：第 3 步需要本机具备 GPU 且已存在 `full_sft_768.pth` 权重与 `dpo.jsonl`；若条件不满足，标注「待本地验证」，仅完成第 1、2、4 步即可达成学习目标。

---

## 6. 本讲小结

- DPO 用「一问 + chosen + rejected」的偏好数据，直接最大化 chosen 优于 rejected 的对数几率，**无需训练 Reward / Value 模型**。
- `logits_to_log_probs` 做两件事：`log_softmax` 在词表维归一化、`gather` 抽出真实 token 那一列，得到 `(batch, seq_len)` 的逐 token 对数概率；它内部不做位移，因为输入 `y` 已在 Dataset 里位移过。
- `dpo_loss` 用 `(log_probs * mask).sum(dim=1)` 把回答段对数概率求和成序列对数似然，再按「前一半 chosen / 后一半 rejected」切开，算 `logits = (logπ_θ(y_w)-logπ_θ(y_l)) - (logπ_ref(y_w)-logπ_ref(y_l))`，损失为 `-logsigmoid(beta * logits)`。
- 参考模型与策略模型**同结构、同初始权重**，但 ref 被 `eval()` + `requires_grad_(False)` 冻结并在 `no_grad` 下前向，充当「参照系」；`\beta` 控制策略能偏离多远。
- 工程上 chosen/rejected 被 `torch.cat` 拼成翻倍 batch 一次前向，故 `dpo_loss` 内 `batch_size` 是 `2 × 数据 batch_size`，切分用 `batch_size // 2`。
- 统一 PO 视角：所有 xxPO 都在优化「策略项 \(f(r_t)\) × 优势项 \(g(A_t)\) − 正则项 \(h(\text{KL}_t)\)」；DPO 的策略项是 \(\log r_w-\log r_l\)、优势项无显式、正则隐含在 \(\beta\) 中。

---

## 7. 下一步学习建议

本讲建立的「三项组件统一视角」是后续所有 RL 算法的阅读地图，建议按以下顺序推进：

1. **[u7-l2 训推分离：Rollout 引擎](u7-l2-rollout-engine.md)** —— DPO 是 off-policy、用静态数据；而 PPO/GRPO/CISPO 是 on-policy，需要用当前策略**实时采样**回答。这就要先把「采样」从训练循环里抽出来，理解 `RolloutEngine`（本地 `TorchRolloutEngine` 与基于 HTTP 的 `SGLangRolloutEngine`）的抽象。
2. **[u7-l3 奖励信号：Reward Model 与奖励塑造](u7-l3-reward-model-and-shaping.md)** —— DPO 的好坏信号来自静态 chosen/rejected；RLAIF 则需要**实时给回答打分**，理解 `LMForRewardModel` 与多维奖励如何构造。
3. **[u7-l4 GRPO 与 CISPO](u7-l4-grpo-and-cispo.md)** —— 重点看 GRPO 如何把 DPO「无显式优势项」替换为**组内归一化优势** \((R-\mu)/\sigma\)，以及 CISPO 如何把策略项改写为 `clip(ratio)·A·logπ` 以避免 clip 截断梯度——届时回看本讲的对照表，会有「一以贯之」的感觉。
4. 继续精读源码：通读 [trainer/train_dpo.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_dpo.py) 的 `train_epoch` 与 main 装配，对照 [u4-l3](u4-l3-ddp-and-amp.md) 的训练骨架，体会「DPO 训练 = SFT 训练骨架 + 换损失函数 + 多一个冻结的 ref 模型」。
