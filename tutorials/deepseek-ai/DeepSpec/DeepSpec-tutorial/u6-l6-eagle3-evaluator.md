# Eagle3 评估器：链式提议与草稿缓存扩展

## 1. 本讲目标

本讲是评估系统单元的收官篇。学完本讲，你应该能够：

1. 解释 Eagle3 草稿 cache **预填充时的 token 错位对齐**：为什么 `shifted_prompt_ids` 要丢掉首 token、拼上首 token 之后的那个 token，而 position 却保持不动。
2. 读懂 `_propose` 的**逐 token 链式提议循环**：一次提议最多做 `ttt_length - 1` 次单 token 前向，滚动隐状态如何一步步往下传。
3. 说清 `assert draft_num_hidden_layers == 1` 背后的**缓存安全隐患**：为什么 `_update` 一次向 cache 追加多个 token 且不带因果掩码，只在单层草稿下是安全的。
4. 对比 DSpark 与 Eagle3 两个评估器在 **context 状态结构**上的差异，理解「同一个投机解码主循环、两套完全不同的状态机」。

## 2. 前置知识

本讲默认你已读完 u6-l2（主循环与四钩子）和 u5-l1（Eagle3 模型结构）。这里只补三个本讲反复用到的基础件：

- **DynamicCache 的三个原语**。`update(k, v, layer_idx)` 追加一组 K/V；`get_seq_length()` 返回当前 cache 长度（token 数）；`crop(n)` 把 cache 截断回前 `n` 个 token。投机解码的全部缓存维护就是这三个原语的组合。
- **Eagle3 的错位约定**（u5-l1 已建立）：草稿的每个「槽位 \(i\)」消费一对输入 \((h_i,\; x_{i+1})\) 并预测 \(x_{i+2}\)。其中 \(h_i\) 在 prompt 区是目标模型 5 层隐状态拼接后投影的特征，在生成区则是草稿自己上一步的输出；RoPE 位置编号保持 \(i\)。相对距离自洽，所以绝对编号错一位并不破坏注意力。
- **四钩子合同**（u6-l2 已建立）：`generate_decoding_sample` 只做算法无关的事（目标 prefill、验证前向、游标 `start` 推进、统计收集），`init_context` / `propose` / `update` 三个钩子携带算法私有状态。钩子间以两个数据类为合同：`DraftProposal(draft_token_count, verify_input_ids, draft_probs)` 与 `VerificationResult`（含 `committed_tokens`，即「被接受的草稿前缀 + 1 个兜底 token」，**不含锚点 token**）。

一个后面反复用到的不变式：主循环每轮结束时 `past_key_values_target.crop(start)`，所以**目标 KV cache 长度恒等于 `start`**。本讲会证明 Eagle3 的草稿 cache 在每轮提议开始时也恰好等于 `start`——两套缓存长度同步，这是理解 `_update` 的钥匙。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [deepspec/eval/eagle3/evaluator.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py) | 本讲主角。`Qwen3Eagle3Evaluator` 实现三个钩子；`Gemma4Eagle3Evaluator` 只换模型类 |
| [deepspec/modeling/eagle3/qwen3/modeling.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py) | 草稿模型本体。`extend_draft_cache`、`forward` 的双路输入与掩码准备都在这里 |
| [deepspec/modeling/eagle3/common.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/common.py) | `extract_eagle3_context_feature`（5 层拼接）与训练侧 flex 块掩码 |
| [deepspec/eval/base_evaluator.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py) | 主循环 `generate_decoding_sample`、`committed_tokens` 的拼装、四钩子调用点 |
| [deepspec/eval/dspark/evaluator.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py) | 对照组：DSpark 的 `_init_context` / `_update` 状态结构 |
| [deepspec/eval/dspark/draft_ops.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/draft_ops.py) | 对照组：`forward_dspark_draft_block` 内的 `crop(start)` |
| [deepspec/modeling/eagle3/qwen3/config.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/config.py) | `build_draft_config`：`draft_num_hidden_layers` 如何写进草稿 config |
| [eval.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py) | `EVALUATORS` 分发表，`Qwen3Eagle3Model` 键的来源 |

另有两份轻量参考：[config/eagle3/eagle3_qwen3_4b.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/eagle3/eagle3_qwen3_4b.py)（`ttt_length=7`、`draft_num_hidden_layers=1` 的出厂值）与 [deepspec/utils/sampling.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/sampling.py)（`sample_tokens` / `logits_to_probs`）。

## 4. 核心概念与源码讲解

### 4.1 extend_draft_cache 预填充：token 错位对齐

#### 4.1.1 概念说明

主循环 prefill 完目标模型后，会从目标分布直采出第一个 token（锚点），然后调用 `init_context` 让草稿算法建立自己的初始状态。Eagle3 的初始状态是一份「已经消化完整个 prompt」的草稿 KV cache，外加一个**滚动隐状态** `draft_hidden`。

难点在于对齐。目标 prefill 给出的是 prompt 各位置的隐状态 \(h_0,\dots,h_{n-1}\)（\(n\) 为 prompt 长度），但按 Eagle3 约定，草稿槽位 \(i\) 要吃的是 \((h_i, x_{i+1})\)——token 要**右移一位**，RoPE 位置却**保持 \(i\)**。如果直接把 `input_ids` 原样喂给草稿，槽位 \(i\) 就会变成 \((h_i, x_i)\)，与训练时的输入分布错位，草稿头第一步就会给出系统性偏移的分布。

所以预填充要手工构造一份「右移一位」的 token 序列：丢掉 prompt 首 token，把刚采出的锚点 token 补到末尾。

#### 4.1.2 核心流程

```
主循环 prefill:
  output = target(prompt)                    # hidden_states[0]=embedding, [l+1]=第 l 层输出
  output_ids[:, n] = 从目标分布直采的锚点 a0
  start = n

_init_context:
  target_hidden[t] = concat_5层(hidden_states[layer_id+1])   # 形状 [1, n, 5H]
  shifted[t]       = output_ids[t+1]          # t = 0..n-1（右移一位）
  draft_cache      = DynamicCache()
  draft_hidden     = extend_draft_cache(target_hidden, shifted, pos=[0..n-1])
                    └── 返回 output[:, -1:, :]  # 只留最后一个槽位的输出
  context = {draft_cache, draft_hidden, position_ids, current_pos=n, cache_len_before=0}
```

用符号写：预填充后草稿 cache 槽位 \(i\) 存放由 \((h_i,\; x_{i+1})\) 算出的 K/V，其中 \(x_n = a_0\) 是锚点。特别地，最后一个槽位 \(n-1\) 已经把锚点 \(a_0\) 吃进去了，它的输出（即 `draft_hidden`）恰好用来预测 \(x_{n+1}\)——也就是下一轮第一个草稿 token。锚点因此无缝成为链式提议的第一环。

#### 4.1.3 源码精读

先看钩子本体 [deepspec/eval/eagle3/evaluator.py:61-96](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py#L61-L96)。这段就是 `_init_context`：先取 5 层拼接特征，再手工构造右移 token 序列，最后用 `extend_draft_cache` 一次填充整份草稿 cache。

其中三行切片是错位对齐的全部秘密，见 [deepspec/eval/eagle3/evaluator.py:76-82](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py#L76-L82)：`shifted_prompt_ids` 取 `output_ids[:, 1:n]` 再拼上 `output_ids[:, n:n+1]`（锚点）。即 `shifted[t] = output_ids[t+1]`，长度仍为 \(n\)，首 token（通常是 BOS）被丢弃。而 [deepspec/eval/eagle3/evaluator.py:87](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py#L87) 传入的 `position_ids` 却是 `[:, :n]`，即 \([0,\dots,n-1]\)——位置不右移，这正是「隐状态 \(i\) 配 token \(i+1\)、RoPE 位置保持 \(i\)」的约定在评估侧的复刻。函数开头的注释也直接点明了这一点，见 [deepspec/eval/eagle3/evaluator.py:69-71](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py#L69-L71)。

5 层特征来自 [deepspec/modeling/eagle3/common.py:38-41](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/common.py#L38-L41)：`extract_eagle3_context_feature` 对 `hidden_states[layer_id + 1]` 逐层取张量沿末维拼接。`+1` 是因为 `output_hidden_states=True` 时元组第 0 项是 embedding 输出、第 \(l+1\) 项才是第 \(l\) 个 decoder 层的输出；Eagle3 只消费 decoder 层，注释明确说不支持 DSpark 的 `-1`（embedding）哨兵层。

`extend_draft_cache` 本体在 [deepspec/modeling/eagle3/qwen3/modeling.py:307-322](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py#L307-L322)：它断言至少一个 token，调一次普通 `forward`（`use_cache=True`），然后 **只返回最后一个位置的输出** `output[:, -1:, :]`。这个「丢弃中间、只留末位」的返回约定是后面安全性的伏笔之一。

进入 `forward` 后还有一处双路输入值得注意，见 [deepspec/modeling/eagle3/qwen3/modeling.py:344-346](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py#L344-L346)：若 `hidden_states` 末维等于 \(5H\) 就先过 `self.fc` 投影回 \(H\)。预填充传的是 5 层拼接特征（\(5H\)），走投影；下一节链式提议传的是草稿自己上一步的输出（\(H\)），跳过投影。同一个 `forward` 因此能同时服务「吃目标特征」和「吃自己的滚动隐状态」两种调用。

顺带确认分发链路：草稿 checkpoint 的 `architectures[0]` 是 `build_draft_config` 写入的 `"Qwen3Eagle3Model"`（[deepspec/modeling/eagle3/qwen3/config.py:28](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/config.py#L28)），在 [eval.py:10-16](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py#L10-L16) 的 `EVALUATORS` 表里命中 `Qwen3Eagle3Evaluator`（遗留别名 `Eagle3DraftModel` 也映射到它）。`max_proposal_tokens` 直接取 `ttt_length`，见 [deepspec/eval/eagle3/evaluator.py:39-41](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py#L39-L41)。

#### 4.1.4 代码实践

**实践目标**：不用任何模型，只用张量切片验证「右移一位、长度不变」确实成立。

操作步骤（以下为**示例代码**，只需安装 PyTorch 即可运行）：

1. 新建 `shift_check.py`，粘贴下面内容；
2. 运行 `python shift_check.py`；
3. 对照输出检查三件事：`shifted` 长度是否等于 `n`；`shifted[t]` 是否等于 `output_ids[t+1]`；首 token `x0` 是否已不在 `shifted` 中。

```python
import torch

n = 6                                        # 假设 prompt 长 6
output_ids = torch.arange(2 * n).unsqueeze(0)  # [x0..x11]，前 6 个视为 prompt，第 7 个是锚点
shifted = torch.cat([
    output_ids[:, 1:n],
    output_ids[:, n:n + 1],
], dim=1)
print("output_ids:", output_ids[0].tolist())
print("shifted    :", shifted[0].tolist())
print("长度       :", shifted.shape[1])
```

需要观察的现象与预期结果：输出为 `output_ids: [0,1,2,3,4,5,6,...]`、`shifted: [1,2,3,4,5,6]`、`长度: 6`。即 token 右移一位、锚点（6 号）补尾、长度不变——这正是 `_init_context` 喂给草稿的 token 序列，而 RoPE 位置仍是 `[0,1,2,3,4,5]`。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `shifted_prompt_ids` 换成直接用 `output_ids[:, :n]`（不右移），草稿槽位 \(i\) 实际消费的输入对是什么？会破坏什么？

**答案**：会变成 \((h_i, x_i)\)，即隐状态和**同一位置**的 token 配对；而训练时草稿学到的是 \((h_i, x_{i+1})\) 配对。输入分布与训练分布错位一步，`draft_hidden` 起点的 logits 会系统性偏移，首轮接受率显著下降（输出仍无损，因为拒绝采样兜底，但加速比受损）。

**练习 2**：预填充一共向草稿 cache 写入了多少个槽位？`draft_hidden` 是第几个槽位的输出？

**答案**：恰好 \(n\) 个槽位（长度等于 prompt 长度，因为 `q_len = n`、`extend_draft_cache` 一次吃完）。`draft_hidden` 是第 \(n-1\) 个（最后一个）槽位的输出，该槽位消费 \((h_{n-1}, a_0)\)，其 logits 用于预测 \(x_{n+1}\)。

**练习 3**：为什么锚点 token \(a_0\) 必须拼进 `shifted_prompt_ids` 末尾，而不是留给 `_propose` 再处理？

**答案**：因为草稿槽位的输入是「隐状态 + 下一个 token」的**成对**结构，锚点是位置 \(n-1\) 那一对的 token 半边，缺了它最后一个槽位就残缺。预填充把它吃进去后，`draft_hidden` 天然就是「已看到锚点」的状态，`_propose` 的第一次 `compute_logits` 可以直接产出对 \(x_{n+1}\) 的预测，链式循环无需特判首步。

### 4.2 逐 token 提议循环：_propose 的链式前向

#### 4.2.1 概念说明

DSpark 的提议是**块式**的：一个 mask token 噪声块做一次长度为 \(B\) 的并行前向，一次吐出整块 logits。Eagle3 相反，是**链式**的：每采样一个 token，就要把这个 token 喂回草稿做一次单 token 前向，得到新的滚动隐状态，再采下一个。这本质上是把训练时的 TTT（train-time test，链式展开 `ttt_length` 步）原样搬到推理期。

结构上带来两个直接后果：

- **前向次数**：提议 \(k\) 个 token（\(k \le\) `ttt_length`）需要 \(k-1\) 次长度为 1 的草稿前向（最后一个 token 采样后不再前向）；DSpark 只要 1 次长度为 \(B\) 的前向。
- **状态副作用**：链式前向会**真的把投机 token 写进草稿 cache**。这些槽位用的是草稿自己的预测 token 和自己的滚动隐状态，一旦验证拒绝就必须整体作废——这就是 `cache_len_before` 存在的理由。

#### 4.2.2 核心流程

```
_propose(context, output_ids, position_ids, start):
  cache_len_before = draft_cache.get_seq_length()   # 副作用：记下回退点（= start）
  candidates = [output_ids[start]]                  # 锚点打头（DraftProposal 合同）
  hidden = context.draft_hidden                     # 预填充留下的滚动状态
  pos = start
  重复 max_proposal_tokens 次:
      logits = lm_head(norm(hidden))                # 预测「再下一个」token
      tok = sample_tokens(logits, temperature)
      candidates.append(tok)
      若 tok 是停止 token: break                     # 未前向、未写 cache
      hidden = draft(hidden_states=hidden,          # 注意 hidden 是 H 维 → 跳过 fc
                     input_ids=tok, position_ids=pos,
                     past_key_values=draft_cache)    # cache 追加 1 个投机槽位
      pos += 1
  return DraftProposal(draft_token_count=k,
                       verify_input_ids=candidates,   # [锚点, p1..pk]
                       draft_probs=softmax(logits 拼接))
```

三条对齐线必须同时成立（可对照 u6-l2 的验证合同）：`verify_input_ids` 首位必须是当前 token（锚点）；`draft_probs[:, j]` 是第 \(j+1\) 个草稿 token 的采样分布；`draft_token_count ≤ max_proposal_tokens`。

#### 4.2.3 源码精读

先看回退点与初始化，[deepspec/eval/eagle3/evaluator.py:107-114](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py#L107-L114)：`cache_len_before` 在循环开始前记录当前 cache 长度，注释（[deepspec/eval/eagle3/evaluator.py:108-109](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py#L108-L109)）说明它是给 `_update` 回退用的。`candidate_ids` 以锚点 `output_ids[:, start:start+1]` 打头。

链式循环本体在 [deepspec/eval/eagle3/evaluator.py:116-136](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py#L116-L136)：每轮先 `compute_logits`（就是 `lm_head(norm(h))`，见 [deepspec/modeling/eagle3/qwen3/modeling.py:268-269](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py#L268-L269)），`sample_tokens` 采样（温度 ≤ 1e-5 时退化为 argmax，见 [deepspec/utils/sampling.py:20-27](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/sampling.py#L20-L27)）；若采到停止 token 就 `break`（此时该 token 已进 `candidate_ids` 但**没有**写 cache）；否则把 `(hidden, tok)` 喂回草稿——注意 `hidden_states=proposal_hidden` 是 \(H\) 维的草稿自身输出，因此在 `forward` 里跳过 `fc` 投影，且 `position_ids` 取 `[next_position, next_position+1)`，从 `start` 起逐位递增。

停止 token 的早停检查在 [deepspec/eval/eagle3/evaluator.py:124-125](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py#L124-L125)：`has_stop_token` 对刚采出的 token 做成员判断。所以 `draft_token_count` 可以小于 `ttt_length`，主循环统计的 `#propose` 列因此是 `2.00+1` 这类「均值+1」形式。

返回值组装在 [deepspec/eval/eagle3/evaluator.py:138-146](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py#L138-L146)：logits 沿序列维拼接后经 `logits_to_probs` 变分布，`verify_input_ids` 是 `[锚点, p1..pk]`。注意这里**没有**置信度截断逻辑，也没有 `post_verify` 钩子（对照 `generate_one_sample`，[deepspec/eval/eagle3/evaluator.py:172-188](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py#L172-L188) 没有传 `post_verify` 参数）——Eagle3 没有置信度头，`--confidence-threshold` 对它是无效参数。

最后看一次完整的数字例子（后面实践还要用它）。设 \(n=6\)、`ttt_length=7`、第一轮采满 7 个草稿 token、验证接受 3 个（\(p_4\) 被拒、残差采出兜底 \(r\)）：

| 时刻 | 草稿 cache 长度 | 说明 |
| --- | --- | --- |
| 预填充后 | 6 | 槽位 0..5，`draft_hidden` 来自槽位 5 |
| `_propose` 循环中 | 6 → 12 | 7 次采样做 6 次前向，槽位 6..11 全是投机槽位 |
| `_update` crop 后 | 12 → 6 | 裁回 `cache_len_before`，投机槽位全弃 |
| `_update` extend 后 | 6 → 10 | 用已验证 token 重建槽位 6..9 |

主循环那边 `start` 从 6 推进到 \(6+3+1=10\)——`_update` 结束时草稿 cache 长度（10）恰好等于新的 `start`，两套缓存重新同步。

#### 4.2.4 代码实践

**实践目标**：源码阅读型实践——手工执行一轮 `_propose`，验证「\(k\) 个草稿 token 只需 \(k-1\) 次前向」以及 cache 长度变化。

操作步骤：

1. 打开 [deepspec/eval/eagle3/evaluator.py:98-146](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py#L98-L146)，在纸上画一张 5 列表格：`迭代号 i`、`compute_logits 的输入 hidden 来自哪个槽位`、`采出的 token（预测的是哪个下标）`、`本次是否执行 forward`、`forward 用的 position_ids`。
2. 按 \(n=6\)、`ttt_length=7`、无停止 token 的设定逐行填表。
3. 填完后数一下「执行 forward」列中 `是` 的个数，并写出循环结束时 `draft_cache.get_seq_length()`。

需要观察的现象与预期结果（即参考答案）：

| i | hidden 来源 | 采样 token（预测下标） | forward? | position |
| --- | --- | --- | --- | --- |
| 0 | 预填充槽位 5 | \(p_1\)（下标 7） | 是 | 6 |
| 1 | 槽位 6 | \(p_2\)（下标 8） | 是 | 7 |
| 2 | 槽位 7 | \(p_3\)（下标 9） | 是 | 8 |
| 3 | 槽位 8 | \(p_4\)（下标 10） | 是 | 9 |
| 4 | 槽位 9 | \(p_5\)（下标 11） | 是 | 10 |
| 5 | 槽位 10 | \(p_6\)（下标 12） | 是 | 11 |
| 6 | 槽位 11 | \(p_7\)（下标 13） | **否**（循环耗尽） | — |

7 次采样、6 次前向；结束时 cache 长度 \(6+6=12\)。若第 3 轮迭代采到停止 token，则前向只发生 2 次（迭代 0、1），cache 长度为 8，且 `draft_token_count=3`。

#### 4.2.5 小练习与答案

**练习 1**：链式循环里传给草稿的 `hidden_states=proposal_hidden` 是 \(H\) 维的。如果有人在 `forward` 里删掉那个 5H 判断、无条件做 `fc` 投影，会发生什么？

**答案**：预填充路径（5H 输入）需要投影、链式路径（\(H\) 输入）不能投影。无条件投影会让链式路径把 \(H\) 维特征再乘一次 `fc` 权重（且维度 \(5H \times H\) 的矩阵根本无法乘 \(H\) 维输入，直接抛形状错误）。双路判断见 [deepspec/modeling/eagle3/qwen3/modeling.py:344-346](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py#L344-L346)。

**练习 2**：为什么 `_propose` 写进 cache 的投机槽位即使对应「碰巧被接受」的 token 也不能保留，必须由 `_update` 重建？

**答案**：投机槽位的 K/V 是由（草稿自己的滚动隐状态、草稿预测的 token）算出的；验证接受后，正确的状态应该由（**目标模型验证前向得到的**隐状态、被接受的 token）重建。两者输入不同、数值不同。DSSpark 同理——这也是两个评估器都要 crop 的根本原因。

**练习 3**：`draft_probs` 为什么必须用采样时实际使用的那份 logits 构造？

**答案**：拒绝采样的无损性要求接受概率 \(\min(1, p/q)\) 里的 \(q\) 是**草稿真实采样分布**（u6-l3 的证明前提）。若 `draft_probs` 与采样 logits 不一致（例如忘了温度），接受概率计算就失真，输出分布不再与目标分布一致。

### 4.3 draft_num_hidden_layers == 1 约束：多 token cache 扩展的安全性

#### 4.3.1 概念说明

评估器构造函数里有一条全仓库最「有故事」的断言。先看原文，见 [deepspec/eval/eagle3/evaluator.py:28-37](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py#L28-L37)：注释说 `_update` 会**不带因果掩码**地一次向草稿 cache 追加多个已提交 token，这只有在草稿头是**单层**时才安全——因为此时被缓存的 K/V 直接由逐 token 输入投影得到，而不是由上一层「双向混合过」的注意力输出投影得到。

问题的根源是一个矛盾：

- `_update` 想一次吃 \(a{+}1\) 个已提交 token（吞吐考虑，等价于「批量重放」）；
- 但这批 token 之间**未来不该看到过去**之外的东西——如果不加掩码，块内注意力是双向的。

单层结构恰好让这个「不严谨」变得无害。证明分两步：

1. **cache 无污染**。单层时每个位置的 K/V 只由该位置自己的输入对 \((\mathrm{LN}(e_{x}), \mathrm{LN}(h))\) 经 `k_proj/v_proj` 算出（见 [deepspec/modeling/eagle3/qwen3/modeling.py:184-196](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py#L184-L196)：decoder 层先各自 norm 再 cat、送注意力投影），**与块内其他位置无关**。所以无论一次喂 1 个还是 \(a{+}1\) 个，写进 cache 的 K/V 逐位相同。
2. **返回值无污染**。`extend_draft_cache` 只返回 `output[:, -1:, :]`。对最后一个位置而言，「双向看整块」与「因果只看之前」是同一件事——块内其他位置都在它之前。因此返回的滚动隐状态也与逐 token 扩展完全一致。

写成形式：设块长 \(m\)，位置 \(m-1\) 的注意力集合在两种模式下都是 \(\{0,\dots,m-1\}\)，故

\[
\mathrm{out}_{m-1}^{\text{无掩码}} = \mathrm{Attn}(q_{m-1},\{k_j\}_{j\le m-1},\{v_j\}_{j\le m-1}) = \mathrm{out}_{m-1}^{\text{因果}}.
\]

若草稿有第二层，第 2 层的 K/V 要从第 1 层输出投影，而第 1 层对非末位位置已经混入了块内未来 token——这些位置的 K/V 被永久写进 cache，后续每一轮的注意力都会读到「被污染的 key」。错误不会立刻崩，只会让接受率悄悄下降，这正是注释所谓的 cache 安全隐患。

#### 4.3.2 核心流程

先弄清三次「多 token / 单 token」调用在掩码上的差异，这是理解安全边界的钥匙：

| 调用点 | q_len | past_seen_tokens | 掩码路径 | 块内因果性 |
| --- | --- | --- | --- | --- |
| `_init_context` 预填充 | \(n\) | 0 | `attention_mask=None` 且 q_len>1 且 past=0 → `is_causal=True` 快路径 | 因果 ✓ |
| `_propose` 链式步 | 1 | start | 单 query 看全部历史 | 天然安全 ✓ |
| `_update` 重建 | \(a{+}1\) | start>0 | `attention_mask=None` 且 past>0 → `is_causal=False` | **双向**，靠单层约束兜底 |

`_update` 的流程：

```
_update(context, verification):
  m = committed_tokens.shape[1]                 # = 接受数 a + 1，不含锚点
  draft_cache.crop(cache_len_before)            # 丢弃全部投机槽位
  committed_hidden = 5层拼接(验证前向的 hidden_states)[:, :m, :]
      # 第 j 个隐状态对应验证输入的第 j 个 token = [锚点, p1..pa] 中第 j 个
  draft_hidden = extend_draft_cache(
      hidden_states=committed_hidden,           # 5H → fc 投影
      input_ids=committed_tokens,               # [p1..pa, r]，不含锚点
      position_ids=position_ids[current_pos : current_pos+m])
  current_pos += m
```

这里藏着本讲最漂亮的一处对齐技巧：`committed_tokens` **不含锚点**（u6-l2 已说明，拼装处见 [deepspec/eval/base_evaluator.py:287-293](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L287-L293)），而 `committed_hidden` 的第 0 个是**锚点的隐状态**。两者一配对，槽位恰好又是 \((h_{\text{start}}, p_1), (h_{p_1}, p_2), \dots\)——\((h_i, x_{i+1})\) 的错位约定**不需要任何显式移位**就自动成立。也就是说：预填充用「token 右移」实现错位，`_update` 用「hidden 多含一个锚点」实现同样的错位，两种手法殊途同归。

#### 4.3.3 源码精读

断言本体见 [deepspec/eval/eagle3/evaluator.py:33-37](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py#L33-L37)：从草稿 config 读 `draft_num_hidden_layers` 并断言等于 1。该字段在训练配置里出厂就是 1（[config/eagle3/eagle3_qwen3_4b.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/eagle3/eagle3_qwen3_4b.py) 的 `model` 字典），由 [deepspec/modeling/eagle3/qwen3/config.py:21-36](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/config.py#L21-L36) 写入草稿 config（注意 `build_draft_config` 只断言 `>= 1`，允许训练多于一层；把「必须单层」收紧到评估侧，是评估器的职责）。

`_update` 本体见 [deepspec/eval/eagle3/evaluator.py:148-170](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py#L148-L170)。crop 在 [evaluator.py:156](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py#L156)：`context.draft_cache.crop(int(context.cache_len_before))`，回退点正是 `_propose` 开头记录的那个值。隐状态切片在 [evaluator.py:157-160](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py#L157-L160)：从验证前向（`output_hidden_states=True`，[deepspec/eval/base_evaluator.py:217-223](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L217-L223)）的 hidden_states 取 5 层拼接后裁前 \(m\) 个——验证输入是 `[锚点, p1..pk]`，所以前 \(m=a{+}1\) 个隐状态对应 `[锚点, p1..pa]`。重建与推进在 [evaluator.py:161-170](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py#L161-L170)：新槽位的 RoPE 位置是 `[current_pos, current_pos+m)`，最后 `current_pos += m`。

掩码路径的「分流开关」在注意力层里，见 [deepspec/modeling/eagle3/qwen3/modeling.py:129-140](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py#L129-L140)：非 flex 实现下，`attn_is_causal` 的默认值是 `attention_mask is None and q_len > 1 and past_seen_tokens == 0`。三次调用恰好落进三种组合：预填充 `q_len>1, past=0` → 因果快路径；`_update` `q_len>1, past>0` → 非因果；`_propose` `q_len==1` → 单 query 无所谓因果。掩码准备的上游入口是 [modeling.py:274-305](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py#L274-L305)：`attention_mask=None` 时直接原样返回 `None`，flex 分支（要求固定大小 TTT 块、`past_seen_tokens % q_len == 0`）只在训练侧走到——评估器把 attention 实现固定为 `sdpa`（[evaluator.py:23](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py#L23)），训练的那套块稀疏掩码（[deepspec/modeling/eagle3/common.py:103-139](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/common.py#L103-L139)）在评估期整体旁路。

最后补一个不变式的证明（归纳）：每轮 `_propose` 开始时 `cache_len_before == start`。初始 `cache_len_before` 未用、cache 长度 \(=n=\) `start`；每轮结束 cache 长度 \(=\) `cache_len_before` \(+ m =\) `start` \(+ a{+}1 =\) 新 `start`（主循环推进见 [deepspec/eval/base_evaluator.py:421-426](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L421-L426)）。所以**草稿 cache 与目标 KV cache 在每轮起点长度恒等**，DSpark 用 `crop(start)`、Eagle3 用 `crop(cache_len_before)` 维护的是同一个量，只是回退值的记法不同。

#### 4.3.4 代码实践

**实践目标**：用一个纯 PyTorch 玩具复现「单层时 K/V 逐 token 无关、多层时被污染」的断言依据。

操作步骤（以下为**示例代码**，无需 GPU、无需模型权重）：

1. 新建 `single_layer_kv.py` 粘贴代码；
2. 运行 `python single_layer_kv.py`，确认打印 `True`；
3. 按练习 2 的提示把它扩成两层，重新运行并观察结果。

```python
import torch
import torch.nn as nn

torch.manual_seed(0)
d = 8

class SingleLayerKV(nn.Module):
    """仿 Qwen3Eagle3DecoderLayer 的单层 K/V 投影：只看本位置输入。"""
    def __init__(self):
        super().__init__()
        self.norm_e = nn.LayerNorm(d)
        self.norm_h = nn.LayerNorm(d)
        self.k_proj = nn.Linear(2 * d, d, bias=False)
        self.emb = nn.Embedding(10, d)

    def keys(self, token_ids, hidden):
        x = torch.cat([self.norm_e(self.emb(token_ids)), self.norm_h(hidden)], dim=-1)
        return self.k_proj(x)          # [B, T, d]

model = SingleLayerKV()
tokens = torch.randint(0, 10, (1, 4))
hidden = torch.randn(1, 4, d)

kv_block = model.keys(tokens, hidden)                                   # 整块一次算
kv_one_by_one = torch.cat(                               # 逐 token 分开算
    [model.keys(tokens[:, i:i + 1], hidden[:, i:i + 1]) for i in range(4)], dim=1)
print("单层 K 是否逐 token 无关:", torch.allclose(kv_block, kv_one_by_one))
```

需要观察的现象与预期结果：打印 `True`——整块计算与逐 token 计算的 K 完全一致，即「批量重放」不改变写入 cache 的内容。这就是 `_update` 敢于不带掩码批量扩展的第一根支柱。若改为两层（练习 2），预期打印 `False`（待本地验证：具体数值取决于随机权重，但两层版本两者必然不一致）。

#### 4.3.5 小练习与答案

**练习 1**：既然 `_update` 的双向注意力在非末位位置是「错」的，为什么最终结果仍然正确？

**答案**：两步。（a）写入 cache 的 K/V 与注意力输出无关（单层、逐 token 输入投影），所以 cache 逐位正确；（b）唯一被保留的输出是最后一位（`extend_draft_cache` 的 `[:, -1:, :]`），而最后一位的双向可见集与因果可见集相同。两个「恰好」叠加，投机取巧零误差。

**练习 2**：把上面的玩具改成两层（第 1 层的输出作为第 2 层的 hidden 输入，第 2 层再算 keys），预期 `allclose` 结果如何？为什么？

**答案**：`False`。第 2 层某位置的 keys 依赖第 1 层对该位置的输出，而第 1 层整块计算时每个位置已混合块内其他位置的信息（无掩码双向注意力），逐 token 计算时则看不到。若这种两层结构真的把「整块版 K」写进 cache，后续轮次的注意力就会读到与逐步生成不一致的 key——正是断言注释描述的隐患。

**练习 3**：`_init_context` 也是一次多 token 前向，为什么不依赖这个单层约束？

**答案**：因为预填充时 `past_seen_tokens == 0` 且 `q_len > 1`，注意力层走 `is_causal=True` 的因果快路径（[modeling.py:133-138](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py#L133-L138)），块内本来就被因果掩码管住了。`_update` 时 `past_seen_tokens > 0`，同一行代码落到 `is_causal=False` 分支——同一份默认值表达式，在两次调用里给出相反的掩码语义，这是读这段代码最容易漏掉的细节。

### 4.4 状态结构对比：DSpark 块提议 vs Eagle3 链式提议

#### 4.4.1 概念说明

两个评估器跑在**同一个** `generate_decoding_sample` 里，差异全部封装在 `context` 这个 `SimpleNamespace` 携带的状态里。对比状态结构是理解「模板方法模式」收益的最佳方式：主循环对 `context` 里有什么一无所知，只负责在正确的时机把它递给钩子。

本质区别可以一句话概括：**DSpark 的记忆放在「目标特征窗口」里，Eagle3 的记忆放在「草稿 cache + 一个滚动隐状态」里**。DSpark 每轮把整段保留窗口显式喂给块前向；Eagle3 则把历史全部压进草稿 KV cache，只额外携带一个 token 的滚动隐状态，链式前向自产自销。

#### 4.4.2 核心流程

一图看懂一轮验证中两者各自维护什么（`a` 为接受数，`r` 为兜底 token）：

```
                 DSpark（块提议）                    Eagle3（链式提议）
─────────────────────────────────────────────────────────────────────────
propose 前      draft_cache.len == start             draft_cache.len == start
                target_hidden = 已验证窗口           draft_hidden = 上轮末滚动状态
propose 中      1 次长度 B 的块前向（mask 噪声块）    k-1 次长度 1 的链式前向
                cache 临时长到 start+B 后 crop(start) cache 临时长到 start+k-1
verify 后       committed = [p1..pa, r]（同左）      committed = [p1..pa, r]
update 中       只替换 target_hidden ← 前 a+1 个      crop(cache_len_before) 后
                已验证目标特征；cache 已在            用已验证隐状态重建 a+1 个槽位，
                propose 内部 crop 完毕               并取回新 draft_hidden
update 后       draft_cache.len == start             draft_cache.len == start'
                （start' = start+a+1）               current_pos == start'
```

#### 4.4.3 源码精读

先看 DSpark 的 `_init_context`，[deepspec/eval/dspark/evaluator.py:85-97](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L85-L97)：context 只有两样东西——空的 `past_key_values_draft` 和**整段 prompt 的目标特征** `target_hidden_states`。没有滚动隐状态，因为 DSpark 的块前向每个槽位的输入特征都由 `target_hidden_states` 显式提供（mask token 只贡献 embedding 半边）。

DSpark 的草稿 cache 裁剪不在 `_update` 里，而在块前向内部：[deepspec/eval/dspark/draft_ops.py:32-45](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/draft_ops.py#L32-L45) 先用「cache 长度到 start+block_size」的位置切片做一次 `is_causal=False` 的块前向，随后 `past_key_values_draft.crop(start)` 把噪声块的废料裁掉。对照 Eagle3：crop 的时机从「propose 内部」挪到了「update 开头」，回退值从字面量 `start` 换成记录值 `cache_len_before`——语义等价，工程上 Eagle3 必须延后裁剪，因为它的 `_update` 需要 cache 保持「propose 前长度」这个锚点来重建。

DSSpark 的 `_update` 见 [deepspec/eval/dspark/evaluator.py:134-147](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L134-L147)：只把 `target_hidden_states` 换成验证输出前 `a+1` 个特征（含锚点，正好是下一轮块前向需要的上下文窗口），**完全不碰草稿 cache**。Eagle3 的 `_update`（[evaluator.py:148-170](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py#L148-L170)）则要「裁剪 + 重建 + 取回滚动状态」三步走。

汇总成表：

| 维度 | DSpark | Eagle3 |
| --- | --- | --- |
| `max_proposal_tokens` | `block_size`（[dspark/evaluator.py:40-42](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L40-L42)） | `ttt_length`（[eagle3/evaluator.py:39-41](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py#L39-L41)） |
| context 字段 | `past_key_values_draft`、`target_hidden_states` | `draft_cache`、`draft_hidden`、`position_ids`、`current_pos`、`cache_len_before` |
| 每轮草稿前向 | 1 次长度 \(B\) | 至多 \(k-1\) 次长度 1 |
| 草稿输入特征 | 显式传目标特征窗口 | 滚动隐状态（\(H\) 维，跳过 `fc`） |
| crop 位置 | propose 内部，`crop(start)` | update 开头，`crop(cache_len_before)` |
| 提议提前截断 | 置信度阈值截前缀 | 仅停止 token 早停 |
| `post_verify` 钩子 | 有（置信度校准记录） | 无（未传入） |
| 结构性断言 | 置信度阈值 ∈ [0,1] | `draft_num_hidden_layers == 1` |

共同点同样重要：两者在每轮起点草稿 cache 长度都等于 `start`，与目标 KV cache 同步；都遵循 `DraftProposal`/`VerificationResult` 合同；`Gemma4` 变体都只换 `draft_model_cls` 一行（[eagle3/evaluator.py:191-192](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py#L191-L192)）。

#### 4.4.4 代码实践

**实践目标**：画出「同一轮验证、两套状态更新」的对照时序图（即本讲规格里的实践任务前半部分）。

操作步骤：

1. 取 4.2.4 的场景：\(n=6\)、DSpark `block_size=7`、Eagle3 `ttt_length=7`、两者都接受 3 个草稿 token、兜底 \(r\)。
2. 画两条平行时间轴，每条轴标 5 个节点：`propose 前`、`propose 中`、`verify`、`update`、`update 后`。
3. 在每个节点标注四元组 `(start, 目标cache长, 草稿cache长, 关键状态变量)`。
4. 完成后在两条轴的 `update 后` 节点处检查两个不变式：草稿 cache 长 == 目标 cache 长 == 新 start。

需要观察的现象与预期结果（参考答案，两轴在 `update 后` 完全同步）：

```
DSpark 轴:
  propose 前  (start=6,  目标=6, 草稿=6,  target_hidden=前6个prompt特征)
  propose 中  (start=6,  目标=6, 草稿=6→13→6, 噪声块前向后 crop(start))
  verify      (start=6,  目标=6→14, 验证输入长 8；committed=[p1,p2,p3,r])
  update      (start=6,  目标=14→10, 草稿=6,  target_hidden ← 前4个已验证特征)
  update 后   (start=10, 目标=10, 草稿=10)

Eagle3 轴:
  propose 前  (start=6,  目标=6, 草稿=6,  draft_hidden=槽位5输出, cache_len_before=6)
  propose 中  (start=6,  目标=6, 草稿=6→12, 7采6前向，槽位6..11为投机)
  verify      (start=6,  目标=6→14, 验证输入长 8；committed=[p1,p2,p3,r])
  update      (start=6,  目标=14→10, 草稿=12→6→10, 重建槽位6..9, 取回新draft_hidden)
  update 后   (start=10, 目标=10, 草稿=10, current_pos=10)
```

（目标 cache 在 verify 时先长到 \(6+8=14\)、随后主循环 `crop(start')=crop(10)` 收回，见 [base_evaluator.py:421-425](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L421-L425)；DSpark 草稿 cache 在块前向中先到 \(6+7=13\) 再被裁回 6。）注意 DSpark 的 verify 里草稿 cache 长度不动、Eagle3 的 verify 里草稿 cache 还带着 6 个投机槽位——这正是两者 `_update` 职责不同的直观体现。

#### 4.4.5 小练习与答案

**练习 1**：如果把 Eagle3 的 `draft_hidden` 从 context 里删掉、每次提议都从头重算，会发生什么？

**答案**：不可行——`draft_hidden` 是链式提议的「传动轴」，它编码了「吃掉锚点（或上一轮兜底 token）之后」的状态；下一轮第一个 logits 必须由它算出。从头重算等价于丢掉 \((h_{\text{start}-1}, x_{\text{start}})\) 这一步的消化结果，链就断了。DSpark 不需要它，因为块前向的每个槽位特征都来自 `target_hidden_states`，没有跨轮滚动状态。

**练习 2**：两个评估器都在维护「草稿 cache == start」这个不变式，但为什么 DSpark 可以在 propose 里顺手裁掉、Eagle3 必须拖到 update 里裁？

**答案**：DSpark 的裁剪对象（噪声块废料）在块前向后就已确定作废，且 `_update` 不再依赖 cache 的任何中间长度；Eagle3 的投机槽位是在链式前向中逐个产生的，`_update` 需要一个「提议前长度」作为回退锚点（`cache_len_before`），并且要在裁剪后立刻重建——裁与建是一对原子动作，拆到 propose 里反而要传递更多状态。

**练习 3**：Eagle3 每轮做 \(k-1\) 次小前向，DSpark 只做 1 次大前向。这是否意味着 DSpark 提议一定更快？

**答案**：不一定，只能说过规約更少。大前向的长度 \(B\) 与小前向的次数都受同一批 GPU kernel 启动开销与并行度影响：长度 1 的前向几乎吃不满 GPU，\(k-1\) 次串行的 launches 可能比一次长度 \(B\) 的前向更慢；但 DSpark 的块前向还要构造噪声块、双源 K/V 拼接与置信度头。真实结论取决于硬件与实现，仓库并未给出两者的提议耗时对比，需要实测（待本地验证）。

## 5. 综合实践

**端到端双轮追踪 + 单层约束设计题**（纸笔即可完成，是本讲三块的串联）：

设 prompt 长 \(n=10\)，`ttt_length=7`，温度 0（贪心）。

1. **第一轮**：锚点 \(a_0\) 已采出（下标 10）。草稿贪心采出 7 个 token，验证接受 5 个，第 6 个被拒，残差采出兜底 \(r_1\)。写出：预填充时 `shifted_prompt_ids` 的长度与内容下标；`_propose` 结束时草稿 cache 长度；`_update` 后 `current_pos`、草稿 cache 长度、目标 cache 长度。
2. **第二轮**：以 \(r_1\) 已入列为前提，草稿采出的第 3 个 token 是停止 token（提前 break）。写出：本轮实际 `draft_token_count`；链式前向执行了几次；`committed_tokens` 的构成（设接受 2 个）。
3. **设计题**：假设要支持 `draft_num_hidden_layers=2` 的 Eagle3 草稿，`_update` 的批量重建需要补什么才能保证 cache 正确？给出修改要点（提示：回顾训练侧的 flex 块掩码如何处理「块内可见性」）。

参考答案：

1. `shifted_prompt_ids` 长度 10，内容为 `output_ids[1..10]`（9 个 prompt token + 锚点 \(a_0\)），RoPE 位置 `[0..9]`。`_propose` 结束时 cache 长度 \(10+6=16\)（7 采 6 前向）。`_update`：crop 回 10，重建 6 个槽位（\(a=5\)，\(m=6\)），`current_pos=10+6=16`，草稿 cache 长 16，目标 cache 长 \(10+6=16\)，三者相等。
2. `draft_token_count=3`；链式前向 2 次（采出停止 token 的那次迭代 break，不做前向）；`committed_tokens = [p1, p2, r2]`（接受 2 个 + 1 个兜底；若停止 token 在被接受前缀内则由验证侧截断逻辑另行处理）。
3. 必须给批量重建补**块内因果掩码**：让第 \(j\) 个新 token 只看到 cache 前缀与块内前 \(j\) 个 token（等价于把训练侧 flex 掩码的「首块因果」模式搬到 sdpa/4D 掩码上），这样第 1 层输出不再混入块内未来，第 2 层的 K/V 才与逐 token 扩展一致；同时 `extend_draft_cache` 只返回末位的约定可保持不变。另一条路是干脆放弃批量重建，退回逐 token 扩展（正确但更慢）——两条路之外，任何「不带掩码的批量扩展」在两层结构下都会污染 cache。

## 6. 本讲小结

- Eagle3 评估器用**三个钩子**接进通用投机解码主循环：`_init_context` 预填充草稿 cache、`_propose` 链式提议、`_update` 重建已验证状态；`post_verify` 不传（无置信度头）。
- **错位对齐有两条实现路径但同一约定** \((h_i, x_{i+1})\)、RoPE 位置 \(i\)：预填充靠「token 右移一位、锚点补尾」，`_update` 靠「committed_hidden 含锚点而 committed_tokens 不含锚点」的天然错位。
- `_propose` 是**链式**循环：\(k\) 个草稿 token 对应 \(k-1\) 次单 token 前向，滚动隐状态 `draft_hidden` 逐环下传；投机槽位写入 cache，由 `cache_len_before` 标记回退点。
- `assert draft_num_hidden_layers == 1` 的安全论证是两步：单层时 K/V 只由本位置输入投影（cache 无污染），且只保留末位输出而末位的双向可见集等于因果可见集（返回值无污染）；两层则第 2 层 K/V 会被双向混合的第 1 层输出污染。
- 掩码语义由 `attention_mask is None and q_len > 1 and past_seen_tokens == 0` 这一个默认表达式分流：预填充因果、`_update` 双向（靠单层兜底）、`_propose` 单 query 天然安全；训练侧的 flex 固定块掩码在评估期整体旁路（`sdpa`）。
- 与 DSpark 的状态结构对照：DSpark 记「目标特征窗口」，Eagle3 记「草稿 cache + 滚动隐状态」；但两者共享 `draft cache == start == target cache` 的每轮起点不变式，DSpark 在 propose 内 `crop(start)`、Eagle3 在 update 开头 `crop(cache_len_before)`。

## 7. 下一步学习建议

本讲完成后，第 6 单元（评估系统）全部讲义已读完，你已掌握从入口分发、主循环、拒绝采样到两个算法评估器的完整链路。建议：

1. **进入第 7 单元扩展实战**：先读 u7-l1（接入新目标模型族）——你会发现本讲的对照表正是「新增一个 Eagle3 风格评估器要写什么」的清单：`draft_model_cls` 一行加上三个钩子。
2. **性能视角回看**：u7-l2 会讨论 flex_attention 与 kernel 效率，届时回头重做 4.4.5 练习 3 的提议耗时问题，把「\(k-1\) 次小前向 vs 1 次大前向」变成可测的消融实验。
3. **源码延伸阅读**：`deepspec/eval/eagle3/gemma4/` 下的 Gemma4 变体与 [deepspec/eval/eagle3/evaluator.py:191-192](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py#L191-L192) 的单行子类对照着读，体会「模型族差异全部下沉到 modeling 层」的分层收益；再对照 [deepspec/modeling/eagle3/common.py:103-139](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/common.py#L103-L139) 想清楚训练侧块掩码若用于 `_update` 需要哪些改动（即综合实践第 3 题）。
