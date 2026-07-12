# 奖励信号：Reward Model 与奖励塑造

## 1. 本讲目标

本讲是强化学习后训练单元（u7）的第三讲，承接 u7-l1（DPO 与 PO 统一视角）和 u7-l2（Rollout 引擎）。学完后你应当能够：

- 说清「奖励稀疏（Reward Sparsity）」为何是超小模型做 RL 的致命瓶颈，以及 MiniMind 为什么选择 **model-based 连续奖励** 而非二元规则奖励来缓解它。
- 读懂 `RLAIFDataset` 如何把 assistant 答案留空、把回答权完全交给 policy 在线采样。
- 读懂 `LMForRewardModel` 如何把第三方 `InternLM2-1.8B-Reward` 包装成一个返回 \([-3,3]\) 连续分数的打分器。
- 读懂 `train_grpo.calculate_rewards` 如何把「长度 / 思考格式 / 重复惩罚 / RM 分数」四种信号加权成一个标量奖励，并理解它与 GRPO 组内优势的关系。

## 2. 前置知识

在进入源码前，先用三段话把必要概念补齐。

**奖励（reward）是什么。** 在 RL 里，奖励 \(r(x,y)\) 是一个标量，衡量「在输入 \(x\) 下生成回答 \(y\) 的好坏」。RL 的目标就是调整策略 \(\pi_\theta\)，让高奖励的回答更容易出现、低奖励的回答更难出现。本讲只关心「这个分数从哪来、怎么算」；至于分数拿到之后怎么驱动梯度（PPO clip / GRPO 组内归一化 / CISPO），那是 u7-l4、u7-l5 的事。

**奖励从哪来（三大来源）。** README 把奖励来源分成三类：**Model-based**（专门训练的奖励模型，如 InternLM2-Reward）、**Rule-based**（规则函数，如数学答案对错、JSON 是否合法）、**Environment-based**（环境反馈，如代码是否跑通）。本讲的 `calculate_rewards` 是一个**混合（hybrid）**实现：以 Model-based 为主，叠加若干 Rule-based 的格式/长度规则。

**优势（advantage）与奖励的关系。** 这是理解本讲「为什么用连续奖励」的关键。策略梯度并不直接用 \(r\)，而是用优势 \(A\)。GRPO 用组内归一化计算优势：

\[
A_i = \frac{r_i - \mu_{\text{group}}}{\sigma_{\text{group}} + \epsilon}
\]

其中 \(\mu_{\text{group}}, \sigma_{\text{group}}\) 是同一个 prompt 下 N 个采样的均值与标准差。注意：**起作用的不是 \(r\) 的绝对大小，而是 N 个 \(r\) 之间的差异**。如果 N 个回答的奖励几乎一样（二元奖励下全 0 或全 1），\(\sigma_{\text{group}}\approx 0\)，则 \(A_i\approx 0\)，梯度消失。这就是本讲要解决的核心矛盾。

## 3. 本讲源码地图

| 文件 | 关键符号 | 作用 |
|---|---|---|
| `dataset/lm_dataset.py` | `RLAIFDataset` | RLAIF 数据集：丢弃最后一轮答案、返回空 answer，把生成交给 rollout |
| `trainer/trainer_utils.py` | `LMForRewardModel` | 包装 InternLM2-Reward，提供 `get_score(messages, response)` 返回 \([-3,3]\) 分数 |
| `trainer/train_grpo.py` | `calculate_rewards` / `rep_penalty` | 把长度、思考格式、重复惩罚、RM 分数加权成单个奖励 |
| `README.md` | 奖励机制选择与稀疏问题章节 | 解释设计动机（连续奖励缓解稀疏） |

把数据流串起来是：

\[
\text{RLAIFDataset}(\text{prompt},\ \text{answer}="") \xrightarrow{\text{rollout}} \text{responses} \xrightarrow{\text{calculate\_rewards}} r_i \in \mathbb{R}
\]

## 4. 核心概念与源码讲解

### 4.1 奖励稀疏：为什么小模型必须用连续奖励

#### 4.1.1 概念说明

「奖励稀疏（Reward Sparsity）」指：策略在绝大多数采样上拿到的奖励几乎相同（通常是都拿 0 分），导致无法从分数差异里学到任何东西。README 用了一个贴切的比喻——让小学生做高考数学题，无论怎么尝试都是零分，自然无法通过「分数高低」来改进答题策略。

对 MiniMind 这种 0.1B 级别、能力较弱的模型，如果在 R1 风格的数学题上用**二元规则奖励**（答对 +1、答错 0），几乎必然陷入稀疏：模型生成的候选回答几乎全部错误，所有 \(r(x,y)\approx 0\)。

#### 4.1.2 核心流程

把它放进 GRPO 的优势公式里看更清楚。设某 prompt 下 N=6 个采样的奖励序列为 \(r_1,\dots,r_6\)：

- **二元奖励、全错**：\(r=(0,0,0,0,0,0)\)，则 \(\mu=0,\ \sigma=0\)，\(A_i=0\)，梯度为 0，训练停滞。
- **连续奖励**：\(r=(-2.5,-2.8,-3.0,-2.6,-2.7,-2.4)\)，虽然答得都不好，但 RM 仍能区分「没那么差」和「更差」，\(\sigma>0\)，\(A_i\) 有正有负，策略得以渐进式优化。

这正是 README 反复强调的：连续奖励即使绝对分都低，只要**有方差**，就能为优势函数提供非零梯度。MiniMind 因此选择 model-based 连续奖励作为主线，并明确「避免直接使用 rule-based 二元奖励 + 超纲难度数据」。

#### 4.1.3 源码精读

设计动机写在 README 的折叠章节里，本节是后续 `calculate_rewards` 的总纲：

[README.md:1042-1059](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L1042-L1059) —— 说明奖励稀疏的现象（\(r\approx 0\)）、后果（\(A\approx 0\)、梯度消失），以及缓解方案：RM 输出连续分数（如 \(-2.5\sim+3.0\)），并支持混合奖励 \(r_{\text{total}}=\alpha r_{\text{model}}+\beta r_{\text{rule}}\)；同时建议训练时监控 \(\text{Var}(r)\)，持续接近 0 即说明信号消失。

#### 4.1.4 代码实践

这是一个「数值推演」型实践（无需下载模型）。

1. 实践目标：亲手验证「方差」才是 GRPO 学习信号的来源，而非绝对奖励值。
2. 操作步骤：打开 Python，模拟两组奖励向量：
   ```python
   import torch
   # 组 A：二元奖励、全错
   rA = torch.zeros(6)
   # 组 B：连续奖励、都差但有差异
   rB = torch.tensor([-2.5, -2.8, -3.0, -2.6, -2.7, -2.4])
   for name, r in [('A', rA), ('B', rB)]:
       adv = (r - r.mean()) / (r.std(unbiased=False) + 1e-4)
       print(name, 'sigma=%.3f' % r.std(unbiased=False), 'adv=', adv.tolist())
   ```
3. 观察现象：组 A 的优势全为 0（或因 1e-4 抖动成极小值）；组 B 的优势有正有负、数值合理。
4. 预期结果：组 A 无任何梯度方向；组 B 给出了「该鼓励谁、该抑制谁」的明确信号。这解释了为什么绝对分都差的组 B 反而能学，而全零的组 A 不能。

#### 4.1.5 小练习与答案

**练习 1**：若把 RM 换成「答对 1、答错 0」的二元奖励，但数据集换成模型有能力答对的简单题，稀疏问题还会出现吗？

> 参考答案：会缓解但未必消失。只要 N 个采样里既有答对也有答错，\(\sigma>0\) 就有信号；但二元奖励信息量低（只有两档），相比连续奖励区分度差，仍容易出现「退化组」（某 prompt 下 N 个采样全对或全错，\(\sigma=0\)）。

**练习 2**：为什么 README 建议训练时监控 \(\text{Var}(r)\)？

> 参考答案：因为 GRPO 的优势由组内方差归一化驱动，\(\text{Var}(r)\) 持续接近 0 意味着优势恒为 0、训练停滞。监控它相当于监控「还有没有学习信号」。

---

### 4.2 RLAIFDataset：把答案留空，交给 policy 实时采样

#### 4.2.1 概念说明

回顾数据集的演化（u2-l2）：`PretrainDataset` 整段算 loss，`SFTDataset` 用锚点夹出 assistant 段算 loss，`DPODataset` 给 chosen/rejected 各标 mask。越往后的阶段，数据集越「懒」——`RLAIFDataset` 是最懒的一个：它**根本不提供答案**。

为什么？因为 RL 的回答不是「背」出来的，而是 policy 当前**自己生成**的，然后由奖励函数打分。数据集只需要给出 prompt（问题 + 模板），答案字段固定留空字符串。真正的回答由 u7-l2 的 RolloutEngine 在训练循环里现场采样。

#### 4.2.2 核心流程

`RLAIFDataset.__getitem__` 的产出极其简单：

1. 读一条样本的 `conversations`（含若干轮对话，最后一轮通常是 assistant 答案）。
2. `create_chat_prompt` 调 `apply_chat_template`，但传入的是 `conversations[:-1]`——**显式丢掉最后一轮 assistant 答案**，并以 `add_generation_prompt=True` 结尾，相当于把话筒递给模型。
3. 按 `thinking_ratio` 概率决定是否开思考模式（注入 `<think>` 起始标签），这会直接影响 4.4 里「思考格式分」能否触发。
4. 返回 `{'prompt': prompt, 'answer': ""}`。

#### 4.2.3 源码精读

[dataset/lm_dataset.py:195-203](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L195-L203) —— `RLAIFDataset.__init__`。它像 SFTDataset 一样缓存了 `bos_id`/`eos_id`，但实际并没用于构造 labels（RL 不需要标签）。`thinking_ratio` 控制思考开关概率。

[dataset/lm_dataset.py:208-216](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L208-L216) —— `create_chat_prompt`。关键三处：`conversations[:-1]` 丢弃 gold 答案；`open_thinking=use_thinking` 随机注入思考开关；`add_generation_prompt=True` 让模板以「该 assistant 说话了」结尾。

[dataset/lm_dataset.py:217-224](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L217-L224) —— `__getitem__` 返回 `answer` 恒为 `""`。这个空字符串在训练脚本里**不会被使用**——回答来自 rollout。

#### 4.2.4 代码实践

1. 实践目标：直观看到 RLAIFDataset 吐出的 prompt 长什么样、答案确实是空的。
2. 操作步骤：在仓库根目录写一段临时脚本（示例代码，非项目原有代码）：
   ```python
   from transformers import AutoTokenizer
   from dataset.lm_dataset import RLAIFDataset
   tok = AutoTokenizer.from_pretrained('./model')
   ds = RLAIFDataset('./dataset/rlaif.jsonl', tok, max_length=1024, thinking_ratio=1.0)
   item = ds[0]
   print(repr(item['prompt'][-200:]))   # 看模板结尾
   print('answer =', repr(item['answer']))
   ```
3. 观察现象：prompt 结尾应是 `<|im_start|>assistant\n`（`thinking_ratio=1.0` 时其后还跟半个 `<think>\n`）；`answer` 是空串。
4. 预期结果：确认「数据集只给上联、不给下联」，下联由 policy 现场对。
5. 若本地无 `rlaif.jsonl` 或 `./model`，标注「待本地验证」，可改为阅读 `__getitem__` 源码确认 answer 为空字符串。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `conversations[:-1]` 要丢掉最后一轮？保留它（连同 gold 答案）会怎样？

> 参考答案：保留 gold 答案就退化成 SFT 了——模型会去模仿标准答案而非探索自己的策略。RL 的核心是 policy 自己生成、自己被打分，所以必须把答案抹掉，只留问题作为采样起点。

**练习 2**：`thinking_ratio=0.9`（train_grpo 默认）意味着什么？

> 参考答案：约 90% 的 prompt 以「开思考」模式渲染（注入 `<think>` 起始标签），10% 直答。这与奖励里「思考格式分」配合：开了思考的采样若格式合格会拿格式奖励，没开的则不触发该子项。

---

### 4.3 LMForRewardModel：把 InternLM2-Reward 包装成连续打分器

#### 4.3.1 概念说明

`LMForRewardModel` 是一个非常薄的包装类，把第三方的 `InternLM2-1.8B-Reward` 包成一个统一接口 `get_score(messages, response) -> float`。它解决两件事：

- **统一接口**：屏蔽 InternLM2 特有的 `model.get_score(tokenizer, messages)` 调用方式，对外只暴露「给对话历史 + 一个候选回答，返回一个分数」。
- **数值截断**：把原始分数 clamp 到 \([-3,3]\)，防止极端值在后续加权求和时压垮其它奖励项。

它和被训练的 MiniMind policy **完全独立**——是另一个模型（InternLM2 架构、约 1.8B 参数），只做推理打分、不参与梯度。

#### 4.3.2 核心流程

`get_score` 的处理很有意思——它不是简单地把 messages 喂给 RM，而是先做一次「上下文折叠」：

1. 把 `messages[:-1]`（历史轮次）拼成一段纯文本 `history_text`。
2. 取 `messages[-1]`（最后一轮 user 提问）。
3. 拼成一句 `"{history}\n以上是对话历史。我的新问题是：\n{last_query}"`，把多轮历史压成单轮 user 消息。
4. 构造 `[{user: 上面那句}, {assistant: response}]` 二元列表，调 InternLM2 的 `get_score`。
5. `return max(min(score, 3.0), -3.0)`。

这种「折叠」是为了适配 InternLM2-Reward 偏好单轮 user+assistant 对的打分格式。

#### 4.3.3 源码精读

[trainer/trainer_utils.py:160-165](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/trainer_utils.py#L160-L165) —— 构造函数。`trust_remote_code=True` 是因为 InternLM2-Reward 带自定义建模代码；`.eval()` 确保只推理不训练。

[trainer/trainer_utils.py:167-177](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/trainer_utils.py#L167-L177) —— `get_score`。注意 `@torch.no_grad()` 装饰器：RM 不参与反向传播。第 169-171 行做上下文折叠；第 176 行调 InternLM2 原生 `get_score`；第 177 行 `max(min(score, 3.0), -3.0)` 把分数 clamp 到 \([-3,3]\)。

#### 4.3.4 代码实践

这是本讲的核心实践任务。

1. 实践目标：亲手拿到 RM 的连续分数，感受它的取值范围与区分度。
2. 操作步骤：先按 README 把 `internlm2-1_8b-reward` 放在仓库同级目录，然后运行（示例代码）：
   ```python
   from trainer.trainer_utils import LMForRewardModel
   rm = LMForRewardModel('../../internlm2-1_8b-reward', device='cuda', dtype=__import__('torch').float16)
   messages = [{"role": "user", "content": "用一句话解释什么是梯度下降。"}]
   for resp in ["梯度下降是一种沿损失函数负梯度方向迭代更新参数以最小化损失的优化算法。",
                "不知道。",
                "啊啊啊啊啊啊啊啊啊啊啊啊啊啊。"]:
       print(f"{rm.get_score(messages, resp):+.3f}  <-  {resp[:18]}")
   ```
3. 观察现象：三条回答应得到明显不同的分数（规范回答 > 「不知道」 > 乱码），且都在 \([-3,3]\) 内。
4. 预期结果：即便三条回答都不算「完美」，RM 仍给出有区分度的连续分数，而非 0/1。这正是 4.1 所说的「连续奖励缓解稀疏」的直观证据。
5. 若无 GPU 或未下载 RM，标注「待本地验证」，可改为阅读 `get_score` 源码，重点理解 clamp 与上下文折叠两处设计。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `get_score` 要把多轮历史折叠成单轮 user 消息？

> 参考答案：InternLM2-Reward 的原生 `get_score` 针对单轮 (user, assistant) 对训练/标定。直接喂多轮可能偏离其训练分布导致分数失真。折叠成「这是历史 + 这是新问题」的单轮形式，让 RM 在其熟悉的输入形态下打分。

**练习 2**：把 clamp 范围从 \([-3,3]\) 改成 \([-1,1]\) 会带来什么影响？

> 参考答案：RM 分数在 `calculate_rewards` 里和若干 ±0.5 的规则项相加。clamp 到 \([-1,1]\) 会让 RM 信号被规则项淹没，弱化「连续奖励为主」的设计初衷；\([-3,3]\) 则让 RM 主导、规则项只做微调。

---

### 4.4 calculate_rewards：多维奖励的加权和

#### 4.4.1 概念说明

有了「空答案的数据集」和「连续打分的 RM」，还差一步：把一条回答映射成**一个标量奖励**。`train_grpo.calculate_rewards` 就是这个映射函数。它是一个**混合（hybrid）奖励**：

\[
r = \underbrace{r_{\text{len}}}_{\text{长度}} + \underbrace{r_{\text{think}}}_{\text{思考格式}} + \underbrace{r_{\text{rep}}}_{\text{重复惩罚}} + \underbrace{r_{\text{RM}}}_{\text{模型分数}}
\]

设计哲学是：RM 提供主要的质量信号（连续、范围大），几个轻量规则项做格式约束（鼓励长度适中、思考闭合、不重复）。这也对应 README 的混合奖励公式 \(r_{\text{total}}=\alpha r_{\text{model}}+\beta r_{\text{rule}}\)。

#### 4.4.2 核心流程

对每个 prompt \(i\) 的每个采样 \(j\)（共 \(B\times\text{num\_generations}\) 条回答），逐项累加进 `rewards[response_idx]`：

1. 用正则从 prompt 文本里解析回 messages 列表（匹配 `<|im_start|>role content<|im_end|>`）。
2. **长度项**：回答 strip 后长度落在 \([20,800]\) 给 +0.5，否则 −0.5（惩罚过短/过长）。
3. **思考格式项**（仅当回答含 `</think>`）：
   - `</think>` 之前的内容（思考段）长度落在 \([20,300]\) 给 +1.0，否则 −0.5；
   - `</think>` 恰好出现 1 次给 +0.25，否则 −0.25；
   - 随后把 `answer` 切成 `</think>` 之后的部分。
4. **重复惩罚项**：`rewards -= rep_penalty(answer)`，`rep_penalty` 衡量三元组重复率，上限 0.5。
5. **RM 项**：调 `reward_model.get_score(messages, answer)`，得到 \([-3,3]\) 分数，存进列表。
6. 循环结束后，把所有 RM 分数一次性加到 rewards 上。

#### 4.4.3 源码精读

[trainer/train_grpo.py:31-34](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_grpo.py#L31-L34) —— `rep_penalty`：用正则切词、构三元组，重复三元组数量乘以系数、上限 `cap=0.5`。返回值越大表示越重复。

[trainer/train_grpo.py:37-68](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_grpo.py#L37-L68) —— `calculate_rewards` 主体：

- 第 38 行 `rewards = torch.zeros(len(responses))`，每个采样一个槽位。
- 第 50-52 行正则解析 messages，供 RM 使用。
- 第 54 行长度项（±0.5）。
- 第 55-59 行思考格式项（长度 +1.0/−0.5、闭合 +0.25/−0.25），并把 `answer` 重置为 `</think>` 之后的内容。
- 第 60 行重复惩罚。
- 第 62-63 行 RM 打分。
- 第 65-66 行把 RM 分数一次性加总。

注意第 60 行用的是 `answer`（若含思考，已是 `</think>` 之后的内容），意味着重复惩罚只针对最终答案、不针对思考过程。

#### 4.4.4 代码实践

1. 实践目标：复现 `calculate_rewards` 的逐项加和，理解各权重比例。
2. 操作步骤：阅读 [trainer/train_grpo.py:54-66](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_grpo.py#L54-L66) 后，手算一条样例的奖励。设某回答为：长度 100、含恰好 1 个合法 `</think>`、`</think>` 之前思考段 150 字、最终答案无三元组重复、RM 给 +1.2。则：

   | 子项 | 计算 | 得分 |
   |---|---|---|
   | 长度 | 100 ∈ [20,800] | +0.5 |
   | 思考段长度 | 150 ∈ [20,300] | +1.0 |
   | `</think>` 闭合 | 恰好 1 个 | +0.25 |
   | 重复惩罚 | 无重复 | −0.0 |
   | RM 分数 | clamp(1.2) | +1.2 |
   | **合计** | | **+2.95** |

3. 观察现象：本例中 RM 分数（1.2）是单项最大贡献之一，但与规则项数量级可比；而在极端情况（RM=−3）下 RM 会主导。
4. 预期结果：直观感受「RM 为主、规则为辅」的加权结构，以及为何要把 RM clamp 到一个比规则项大的范围。
5. 「待本地验证」：实际数值需跑通训练脚本才能确认，本处为按源码逻辑手算。

#### 4.4.5 小练习与答案

**练习 1**：为什么重复惩罚只算 `</think>` 之后的 `answer`，不算思考段？

> 参考答案：思考过程（CoT）里允许甚至鼓励反复斟酌、复述条件，重复三元组是正常的；而最终答案应当精炼，重复通常意味着模型卡住或胡编。所以只惩罚答案部分的重复。

**练习 2**：若某 prompt 下 6 个采样的最终 rewards 完全相同（比如都是 2.0），GRPO 还能学到东西吗？

> 参考答案：不能。GRPO 的优势 \(A=(r-\mu)/(\sigma+\epsilon)\)，\(\sigma=0\) 时 \(A=0\)、梯度消失。这正是「退化组（Degenerate Groups）」，也是 4.1 强调连续奖励要保证组内**有方差**的根本原因。

---

## 5. 综合实践

设计一个小实验，把本讲三块内容（数据集留空、RM 连续打分、混合奖励）串起来。

**任务**：对比「纯 RM 连续奖励」与「二元规则奖励」在 GRPO 优势上的差异。

**步骤**：

1. 用 4.3 的脚本，对同一个 prompt 让 policy 生成 6 条质量参差的回答，调用 `LMForRewardModel.get_score` 得到 6 个连续分数 \(r_{\text{RM}}\)（若无法生成，可手动构造 6 条长短/质量不同的回答代替）。
2. 另构造一个二元奖励：以 0 为阈值，\(r_{\text{bin}}=\mathbf{1}[r_{\text{RM}}>0]\)。
3. 对两组分别算 GRPO 优势 \(A=(r-\mu)/(\sigma+10^{-4})\)，并记录各自的 \(\sigma\)。
4. 汇报：连续组的 \(\sigma\) 与优势分布 vs 二元组的 \(\sigma\) 与优势分布。

**预期结论**：连续奖励的优势有更丰富的梯度方向（6 个不同值、\(\sigma\) 较大）；二元奖励往往退化为少数几档甚至全同（\(\sigma\) 小），从而验证「小模型要用连续奖励而非二元奖励」。

若无法运行 RM，可改为：阅读 `calculate_rewards` 全文，画一张「输入回答 → 五个子项 → 总奖励」的数据流图，标注每个子项的取值范围与权重，并解释 RM 分数为何要占主导。

## 6. 本讲小结

- 奖励稀疏是超小模型做 RL 的致命瓶颈：二元奖励下全错则 \(r\approx0\)、\(\sigma\approx0\)、优势为 0、梯度消失。
- GRPO 的优势是组内归一化的，**起作用的是 N 个采样的奖励差异，而非绝对值**——这是连续奖励有效的根本原因。
- `RLAIFDataset` 是最「懒」的数据集：用 `conversations[:-1]` 丢弃 gold 答案、返回空 `answer`，把回答完全交给 policy 在线采样。
- `LMForRewardModel` 把 InternLM2-Reward 包成 `get_score`，做上下文折叠并 clamp 到 \([-3,3]\)，对外提供连续分数。
- `calculate_rewards` 是混合奖励：长度 ±0.5 + 思考格式（+1.0/−0.5、+0.25/−0.25）+ 重复惩罚（≤0.5）+ RM \([-3,3]\)，RM 为主、规则为辅。

## 7. 下一步学习建议

本讲只解决了「奖励从哪来、怎么算」。拿到标量奖励 \(r\) 之后，GRPO/CISPO 如何把它变成策略梯度，是下一讲 **u7-l4（GRPO 与 CISPO）** 的主题，重点看 `grpo_train_epoch` 里 `grouped_rewards → mean_r/std_r → advantages → ratio/clip` 这条链。若想看「带价值网络」的 PPO 如何用奖励估计优势，继续看 **u7-l5（PPO 与 GAE）**。若对多轮工具调用下的「延迟整轮奖励」感兴趣，看 **u7-l6（Agentic RL）** 的 `calculate_rewards` 是如何在整条轨迹结算的。
