# 投机解码主循环：generate_decoding_sample 的钩子设计

## 1. 本讲目标

上一讲（u6-l1）我们看清了评估系统的骨架：`eval.py` 分发 Evaluator，`BaseEvaluator` 用模板方法模式固化「遍历数据集 → 逐卡生成 → 汇总 → 打表」的流程，而把真正的解码逻辑留给了 `generate_one_sample` 这个钩子。当时我们刻意把解码循环当成黑盒，本讲就打开这个黑盒。

学完本讲，你应该能够：

1. 解释为什么目标模型 prefill 之后，第一个 token 要直接从目标分布采样，而不是交给草稿模型提议。
2. 逐行读懂 `generate_decoding_sample` 的主循环：`init_context` / `propose` / `update` / `post_verify` 四个算法钩子各自何时被调用、承担什么职责。
3. 说出每一轮验证「提交了多少个 token、`start` 如何推进」，以及 `DynamicCache.crop` 为什么必须在每轮末尾把未被接受的草稿 token 的 KV 裁掉。
4. 理解 `acceptance_lengths`、`proposal_lengths`、`accepted_draft_lengths` 三个统计量是如何在循环里被收集、并流向上一讲见过的 `accept_len` / `verify_rate` 指标的。

本讲精读的核心是一个约 130 行的函数，但它是整个 DeepSpec 评估侧的「心脏」——DSpark 和 Eagle3 两种截然不同的草稿算法，都是往这同一个循环里插拔钩子实现的。

## 2. 前置知识

### 2.1 KV cache：自回归解码的「草稿纸」

Transformer 自回归生成时，每生成一个新 token 都要回头「看」之前所有 token 的 Key/Value 向量。如果每步都重算全部历史，代价是 \(O(n^2)\)。KV cache 把每层的 K、V 存下来，新 token 只需计算自己的 Q/K/V 并把 K/V 追加进 cache，单步代价降为 \(O(n)\)。

- **prefill（预填充）**：把整段 prompt 一次性喂进模型，填满 KV cache，同时得到「下一个 token」的分布。这一步是并行的大矩阵乘法，GPU 利用率高。
- **decode（逐 token 解码）**：每次只喂 1 个 token，读 cache、追加 cache。这一步是访存受限的，GPU 利用率低——这正是投机解码要优化的对象。

本讲使用的 `DynamicCache` 来自 `transformers`（[deepspec/eval/base_evaluator.py:13](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L13)），它按层维护 KV 张量，关键操作有两个：

- 前向时传 `past_key_values=cache, use_cache=True`，新 token 的 KV 会自动**追加**进去；
- `cache.crop(max_length)` 把 cache **截断**为只保留前 `max_length` 个位置的 KV——这是本讲第三模块的主角。

### 2.2 钩子（hook）：把算法差异从流程骨架里剥出去

「钩子」就是框架在固定时机回调的函数。`generate_decoding_sample` 自己只实现**与算法无关**的部分（目标模型前向、验证、推进、统计），把**与草稿算法相关**的部分留成四个回调参数。DSpark 传进来的钩子实现「块式提议」，Eagle3 传进来的实现「链式提议」，循环本体一行不用改。

这与训练侧 `BaseTrainer` 把 `_build_draft_model` / `run_batch` 留给子类（u3-l1）是同一个设计哲学：**骨架固化，差异下沉**。区别在于训练侧用「子类覆写」实现，评估侧用「传函数引用」实现（[deepspec/eval/dspark/evaluator.py:168-179](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L168-L179) 中传入的是 `self._init_context` 等绑定方法）。

### 2.3 上讲回顾：三个统计量的定义位

u6-l1 已经给出指标恒等式，本讲你会看到它们在循环里的**出生地**：每个验证轮次向 `acceptance_lengths` 追加「本轮提交的 token 数」、向 `proposal_lengths` 追加「本轮有效提议的草稿数」、向 `accepted_draft_lengths` 追加「本轮被接受的草稿数」。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [deepspec/eval/base_evaluator.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py) | **本讲主战场**。`generate_decoding_sample`（主循环）、`verify_draft_tokens`（验证）、`DraftProposal` / `VerificationResult`（两个数据合同）、`build_metrics_row`（统计量下游） |
| [deepspec/eval/dspark/evaluator.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py) | 钩子的一个具体实现方。`_init_context` / `_propose` / `_update` / `_post_verify`，用来说明钩子合同如何被满足（深入留给 u6-l4） |
| [deepspec/utils/sampling.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/sampling.py) | 采样工具箱：`logits_to_probs`（含温度与贪心退化）、`sample_from_probs`、`sample_residual`（拒绝采样数学留给 u6-l3） |
| [eval.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py) | 入口的默认参数：`--max-new-tokens 2048`、`--temperature 1.0`，即主循环 `max_new_tokens` / `temperature` 的实际来源 |

## 4. 核心概念与源码讲解

### 4.1 prefill 与首 token

#### 4.1.1 概念说明

投机解码的循环有一个「先有鸡还是先有蛋」的问题：草稿模型要提议候选 token，可它的提议必须以**已有上下文**为条件。具体到 DeepSpec 的两种算法：

- DSpark 的块式提议需要一个**锚点 token**——噪声块的第一个槽位放的是当前已接受的 token（u4-l1 讲过 `draft_input_ids[:, 0] = output_ids[:, start]`）；
- Eagle3 的链式提议需要**上一步的 token 和目标隐状态**来启动链条。

所以在 speculation 开始之前，必须先有「第一个非 prompt token」。这个 token 由目标模型在 prefill 时直接采样产生。它不需要验证——因为它本来就是从目标分布里采出来的，天然无损（回忆 u1-l1：验证的意义在于**修正**草稿分布与目标分布的偏差；没有草稿参与的 token 无从谈起偏差）。

#### 4.1.2 核心流程

```
输入: input_ids (形状 [1, N])
├─ 1. 分配输出缓冲区 output_ids (长度 N + max_new_tokens + K + 1)
├─ 2. prefill: 目标模型前向 N 个 token
│      ├─ logits_to_keep=1        → 只保留最后位置的 logits
│      ├─ output_hidden_states=True → 草稿模型要吃中间层隐状态
│      └─ KV cache 长度: 0 → N
├─ 3. 从 prefill logits 采样首 token，写入 output_ids[:, N]
├─ 4. start = N   （循环游标：指向「已写入但尚未进目标 cache」的待消费 token）
├─ 5. 若首 token 就是停止符 → 直接返回（verify_count=0）
└─ 6. init_context(...)  → 构建草稿算法专属状态，进入主循环
```

一个贯穿全讲的**不变量**在这里成立，并将在每轮循环末尾被恢复：

> 循环每一轮开始时，`past_key_values_target` 的长度恒等于 `start`，且这些 KV 恰好对应 `output_ids[:, :start]`；位置 `start` 上的 token（最近一次由目标模型采样或兜底得到）是「待消费」状态——它自己还没有进过目标模型的 cache。

prefill 后：cache 长度 = N = start，`output_ids[:, :N]` 是 prompt，位置 N 上是刚采样的首 token。不变量成立。

#### 4.1.3 源码精读

**输出缓冲区的一次性分配。** [deepspec/eval/base_evaluator.py:337-343](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L337-L343) 预分配了 `max_length + max_proposal_tokens + 1` 长度的缓冲区并一次性生成全套 `position_ids`：

```python
output_ids = torch.empty(
    (1, max_length + max_proposal_tokens + 1),
    dtype=torch.long, device=device,
)
position_ids = torch.arange(output_ids.shape[1], device=device).unsqueeze(0)
past_key_values_target = DynamicCache()
```

为什么多出 `max_proposal_tokens + 1` 个槽位？循环条件是 `start < max_length`，所以进入某轮时 `start` 最大为 `max_length - 1`；该轮最远会写到位置 `start + K + 1`（K 个草稿 + 1 个兜底 token），即 `max_length + K`。缓冲区索引到 `max_length + K`，恰好需要 `max_length + K + 1` 个槽位，一个不多一个不少。

**prefill 前向。** [deepspec/eval/base_evaluator.py:345-352](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L345-L352)：

```python
output = target_model(
    input_ids=input_ids,
    position_ids=position_ids[:, :num_input_tokens],
    past_key_values=past_key_values_target,
    use_cache=True,
    output_hidden_states=True,
    logits_to_keep=1,
)
```

两个参数值得注意：

- `logits_to_keep=1` 让 HF 模型只对最后一个位置计算 lm_head 投影，logits 形状是 `[1, 1, V]` 而非 `[1, N, V]`。prefill 唯一有用的输出就是「下一个 token」的分布，前面 N-1 个位置的 logits 纯属浪费。
- `output_hidden_states=True` 会返回全部层的隐状态元组。这不是给目标模型自己用的，而是给 `init_context` 用的——草稿模型的输入特征就是目标模型的中间层隐状态（u2-l5 讲过为什么这里可以用 `output_hidden_states`，但 `target_layer_ids` 不得包含末层：见 [deepspec/eval/base_evaluator.py:100-112](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L100-L112) 的断言及其注释）。

**首 token 采样。** [deepspec/eval/base_evaluator.py:354-359](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L354-L359)：

```python
output_ids[:, :num_input_tokens] = input_ids
output_ids[:, num_input_tokens : num_input_tokens + 1] = sample_from_probs(
    logits_to_probs(output.logits, float(temperature))
)
start = input_ids.shape[1]
```

`logits_to_probs`（[deepspec/utils/sampling.py:6-11](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/sampling.py#L6-L11)）先除温度再 softmax；温度小于 `1e-5` 时退化为 argmax 的 one-hot 分布，所以 `--temperature 0` 就是贪心解码。`sample_from_probs`（[deepspec/utils/sampling.py:14-17](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/sampling.py#L14-L17)）用 `torch.multinomial` 从分布里抽一个 token。注意这个 token 从目标分布直接采出，**不经任何验证**。

**首 token 即停止符的早退。** [deepspec/eval/base_evaluator.py:364-376](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L364-L376)：如果采出来的首 token 本身就是 eos，直接截断返回，此时三个统计量列表都是空、`verify_count=0`。这是一个容易忽略的边界：极端情况下一条样本可以一次验证都不做。

#### 4.1.4 代码实践

**实践目标**：在 CPU 上用一个小型 CausalLM 复现「prefill + `logits_to_keep=1` + 首 token 采样」这一段，亲眼确认 logits 形状、隐状态元组长度与 KV cache 长度。

**操作步骤**（下面是**示例代码**，不是仓库原有代码，保存为 `prefill_probe.py` 单独运行）：

```python
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
from deepspec.utils.sampling import logits_to_probs, sample_from_probs

name = "hf-internal-testing/tiny-random-gpt2"   # 仅 ~几 MB，用于观察形状
tok = AutoTokenizer.from_pretrained(name)
model = AutoModelForCausalLM.from_pretrained(name).eval()

input_ids = tok("The capital of France is", return_tensors="pt").input_ids
N = input_ids.shape[1]

out = model(
    input_ids=input_ids,
    use_cache=True,
    output_hidden_states=True,
    logits_to_keep=1,
)
print("logits.shape =", out.logits.shape)                    # 期望 [1, 1, V]
print("len(hidden_states) =", len(out.hidden_states))        # 期望 层数+1（含 embedding）
print("cache 长度 =", out.past_key_values.get_seq_length())  # 期望 N

first = sample_from_probs(logits_to_probs(out.logits, 1.0))
print("首 token =", first.item(), tok.decode(first[0]))
# 对照：去掉 logits_to_keep 再跑一次，观察 logits 形状变化与额外开销
```

**需要观察的现象**：

1. `logits.shape` 为 `[1, 1, vocab_size]`——只有最后一个位置；
2. `hidden_states` 是 `num_hidden_layers + 1` 个张量的元组（embedding 输出打头）；
3. cache 长度恰好等于 prompt 长度 N；
4. 去掉 `logits_to_keep=1` 后 logits 变为 `[1, N, vocab_size]`，而 decode 我们只关心最后一列。

**预期结果**：形状断言全部吻合；首 token 是模型随机初始化下的任意词（tiny 模型没有语义，这一步只看机制不看质量）。若运行环境无法联网下载模型，可退化为源码阅读实践：在 [deepspec/eval/base_evaluator.py:345-357](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L345-L357) 旁手写出 `output.logits`、`output.hidden_states`、`past_key_values_target` 三者在该行执行后的形状/长度。**待本地验证**（运行结果取决于本机环境）。

#### 4.1.5 小练习与答案

**练习 1**：把 prefill 里的 `logits_to_keep=1` 去掉，程序还能跑对吗？代价是什么？

**答案**：能跑对——`logits_to_probs` + `sample_from_probs` 会对 `[1, N, V]` 的每个位置都采一个样，但只有被写入 `output_ids[:, N:N+1]` 的那个（最后一个位置的采样）有效，其余 N-1 个采样结果被丢弃。代价是 N 倍的 lm_head 计算与采样开销，以及 N 倍的 logits 显存。

**练习 2**：为什么首 token 不需要走 `verify_draft_tokens`？

**答案**：验证与兜底采样的作用是「用目标分布修正草稿分布，保证最终输出分布与纯目标解码一致」。首 token 直接由 `sample_from_probs(logits_to_probs(target_logits))` 从目标分布采出，已经是无偏的，没有任何草稿偏差需要修正；反而它还是后续草稿提议的启动条件（DSpark 的锚点 / Eagle3 的链头）。

**练习 3**：`--temperature 0`（见 [eval.py:35](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py#L35)）传导到 prefill 首 token 时行为如何变化？

**答案**：`logits_to_probs` 在 `temperature < 1e-5` 时返回 one-hot 的 argmax 分布（[deepspec/utils/sampling.py:7-10](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/sampling.py#L7-L10)），`multinomial` 从 one-hot 里必然抽出最大概率词元，等价于贪心解码。循环内所有验证、兜底采样共用这一个温度。

### 4.2 propose / verify / update 钩子

#### 4.2.1 概念说明

主循环 `generate_decoding_sample` 把「草稿算法是什么」完全外包给四个回调，自己只保留「投机解码协议」本身。先看函数签名与文档（[deepspec/eval/base_evaluator.py:307-329](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L307-L329)，注意整个函数被 `@torch.inference_mode()` 包住）：

```python
def generate_decoding_sample(
    *,
    target_model, input_ids, max_new_tokens, max_proposal_tokens,
    temperature, stop_token_ids,
    init_context: Callable[..., Any],
    propose: Callable[..., DraftProposal],
    update: Callable[[Any, VerificationResult], None],
    post_verify: Callable[[DraftProposal, VerificationResult], None] | None = None,
) -> SimpleNamespace:
```

四个钩子的合同：

| 钩子 | 调用时机 | 签名要点 | 职责 |
| --- | --- | --- | --- |
| `init_context` | prefill 之后、进循环之前，恰好一次 | `(initial_output, output_ids, position_ids, num_input_tokens)` | 从 prefill 输出构建草稿算法的专属状态（如 DSpark 的目标隐状态 + 空草稿 cache），返回 `context` |
| `propose` | 每轮开头 | `(context, output_ids, position_ids, start, stop_token_ids)` | 基于 `output_ids[:, :start+1]` 提议至多 K 个草稿 token，返回 `DraftProposal` |
| `update` | 每轮验证通过、游标推进之后 | `(context, verification)` | 把被接受的目标侧信息（隐状态等）写回 `context`，供下一轮提议使用；无返回值 |
| `post_verify`（可选） | 每轮验证之后、统计之前 | `(proposal, verification)` | 纯诊断用途，DSpark 用它喂置信度校准记录器，不影响循环状态 |

两个数据类是钩子之间的「合同文本」（[deepspec/eval/base_evaluator.py:167-183](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L167-L183)）：

- **`DraftProposal`**：`draft_token_count`（本轮提议的草稿数 n，可为 0——DSpark 置信度早停时会出现短块）、`verify_input_ids`（形状 `[1, n+1]`，**必须以当前 token `output_ids[:, start]` 开头**）、`draft_probs`（草稿分布 `[1, n, V]`，供拒绝采样用；无草稿时可传 None）。
- **`VerificationResult`**：`target_output`（目标模型原始输出，含隐状态）、`target_probs`、`accept_prefix_mask`（前缀接受掩码）、`accepted_draft_tokens`（a）、`next_token`（兜底 token）、`effective_proposal_length`（有效提议长度）、`terminated_by_stop_token`、`committed_tokens`（被接受草稿前缀 + 兜底 token 拼成的一段 `[1, a+1]`）。

#### 4.2.2 核心流程

一轮循环的完整时序：

```
while start < max_length:
    ① proposal = propose(context, output_ids, position_ids, start, stop_token_ids)
          → 草稿算法产出 [cur, d1..dn]，cur == output_ids[:, start]
    ② verification = verify_draft_tokens(...)   ← 算法无关，本讲 4.2.3 精读
          → 目标模型一次前向 n+1 个 token，拒绝采样得 a、兜底 token
    ③ post_verify(proposal, verification)        ← 可选诊断
    ④ 收集统计: proposal_lengths ← n, accepted_draft_lengths ← a
    ⑤ output_ids[start : start+a+1] = [cur, d1..da]
    ⑥ 若命中停止符: acceptance_lengths ← a; start += a; crop(start); break
    ⑦ output_ids[start+a+1] = 兜底 token
       acceptance_lengths ← a+1
       start += a + 1
       crop(start)                                ← 4.3 的主角
       update(context, verification)
    ⑧ 若新写入的 token 含停止符: break
```

「每轮提交 `accepted + 1` 个 token」正是 ⑤+⑦ 的合成效果：a 个被接受的草稿加上 1 个兜底 token。最坏情况 a=0，每轮也稳赚 1 个 token——这是投机解码「最差不劣于普通解码」的结构保证。

`verify_draft_tokens` 内部对输入做两道前置校验（[deepspec/eval/base_evaluator.py:199-212](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L199-L212)）：`draft_token_count` 不得超过 `max_proposal_tokens`；`verify_input_ids[:, :1]` 必须与循环传入的 `current_token_ids`（即 `output_ids[:, start:start+1]`，见 [deepspec/eval/base_evaluator.py:401](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L401)）完全相等。第二道校验守护的正是 4.1 的不变量：verify 前向的「第一个被喂进目标模型的 token」就是那个待消费 token。

#### 4.2.3 源码精读

**目标侧验证前向。** [deepspec/eval/base_evaluator.py:214-229](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L214-L229)：

```python
draft_token_count = int(proposal.draft_token_count)
verify_length = draft_token_count + 1
verify_position_ids = position_ids[:, start : start + verify_length]
target_output = target_model(
    input_ids=proposal.verify_input_ids,
    position_ids=verify_position_ids,
    past_key_values=past_key_values_target,
    use_cache=True,
    output_hidden_states=True,
)
target_probs = logits_to_probs(target_output.logits, float(temperature))
```

这是投机解码加速的来源：一次前向**并行**处理 n+1 个 token（大 batch 的矩阵乘，GPU 吃得饱），替代 n+1 次串行 decode。注意 `output_hidden_states=True` 仍然打开——`update` 钩子需要从 `verification.target_output.hidden_states` 里提取草稿模型下一轮要吃的特征。前向结束后 cache 长度从 `start` 涨到 `start + n + 1`。

**拒绝采样（概览）。** [deepspec/eval/base_evaluator.py:240-258](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L240-L258) 计算每个草稿 token 的接受概率 \( \min(1,\; p_{\text{target}}(x)/p_{\text{draft}}(x)) \)，与均匀随机数比较后做 `cumprod` 得到**前缀**接受掩码——一旦某个位置被拒，后面全部作废。最终 `accepted_draft_tokens = accept_prefix_mask.sum()`。数学推导（为什么这样采样保持目标分布不变）是下一讲 u6-l3 的全部内容，本讲只需接受「a = 前缀长度」这个接口。

**停止符截断。** [deepspec/eval/base_evaluator.py:262-276](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L262-L276)：若被接受前缀里出现停止符，把 `accepted_draft_tokens` 与 `effective_proposal_length` 一并截到停止符处并置 `terminated_by_stop_token=True`。这解释了统计量用 `effective_proposal_length`（截断后的 n）而非原始提议数——指标才不会被停止符之后的「幽灵提议」污染。

**兜底 token。** [deepspec/eval/base_evaluator.py:278-285](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L278-L285)：

```python
if 0 < draft_token_count and accepted_draft_tokens < draft_token_count:
    next_token = sample_residual(
        target_probs[:, accepted_draft_tokens, :],
        proposal.draft_probs[:, accepted_draft_tokens, :],
    )
else:
    next_token = sample_from_probs(target_probs[:, -1:, :]).squeeze(1)
```

分支语义：若「有草稿且存在被拒位置」，在第一个被拒位置用 `sample_residual`（从 \( (p_{\text{target}} - p_{\text{draft}})^+ \) 归一化后采样，[deepspec/utils/sampling.py:34-44](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/sampling.py#L34-L44)）修正分布；否则（全部接受、或本轮根本没有草稿）直接从目标分布的最后一个位置采样。之后 [deepspec/eval/base_evaluator.py:287-293](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L287-L293) 把「被接受草稿前缀 + 兜底 token」拼成 `committed_tokens`——这个字段是给 Eagle3 的 `update` 用的（它需要逐 token 推进自己的草稿 cache，见 [deepspec/eval/eagle3/evaluator.py:154-163](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py#L154-L163)，细节留待 u6-l6）。

**主循环体。** [deepspec/eval/base_evaluator.py:385-405](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L385-L405) 把上述环节串起来：

```python
while start < max_length:
    proposal = propose(context=context, output_ids=output_ids,
                       position_ids=position_ids, start=start,
                       stop_token_ids=stop_token_ids)
    verification = verify_draft_tokens(
        target_model=target_model, proposal=proposal,
        position_ids=position_ids, start=start,
        past_key_values_target=past_key_values_target,
        temperature=temperature, max_proposal_tokens=max_proposal_tokens,
        current_token_ids=output_ids[:, start : start + 1],
        stop_token_ids=stop_token_ids,
    )
    if post_verify is not None:
        post_verify(proposal, verification)
```

**钩子的具体实现方（DSpark 侧）.** 以 [deepspec/eval/dspark/evaluator.py:85-97](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L85-L97) 的 `_init_context` 为例，它从 prefill 输出提取目标隐状态、开一个空的草稿 cache 打包成 `SimpleNamespace`；[deepspec/eval/dspark/evaluator.py:134-147](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L134-L147) 的 `_update` 则把**被接受前缀**（`[:, :accepted_draft_tokens+1]`，注意含兜底 token）的目标隐状态写回 context。本讲只需看出：钩子读写的 `context` 完全是算法私有状态，主循环对其一无所知——这就是 DSpark 与 Eagle3 能共用同一个循环的原因。

**统计量的出生地。** [deepspec/eval/base_evaluator.py:407-409](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L407-L409) 每轮追加三个数字，最终由 u6-l1 见过的 `build_metrics_row`（[deepspec/eval/base_evaluator.py:469-511](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L469-L511)）汇总成表格指标：

\[ \text{accept\_len} = \frac{\sum_t (a_t + 1)}{T}, \qquad \text{verify\_rate} = \frac{\sum_t (a_t + 1)}{\sum_t (n_t + 1)} = \frac{\text{accept\_len}}{\bar n + 1} \]

其中 \(\bar n\) 是表中 `#propose` 列显示的「平均有效草稿数」（表头渲染成 `n+1` 形式，见 [deepspec/eval/base_evaluator.py:154](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L154)）。直观地：`verify_rate` 是「每个候选槽位（n 个草稿位 + 1 个兜底位）平均有多少比例真正变成了提交 token」。单轮例子：n=7、a=3 时该轮贡献 acceptance_length=4、verify_rate 分子分母各加 4 和 8。

#### 4.2.4 代码实践

**实践目标**：把「一次两轮解码」中四个钩子的调用顺序、入参、出参整理成一张时序表，并核对 DSpark 实现对 `DraftProposal` 合同的两条硬约束的满足方式。

**操作步骤**（源码阅读型实践，无需 GPU）：

1. 打开 [deepspec/eval/base_evaluator.py:378-429](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L378-L429)，给每一次钩子调用按时间顺序编号（`init_context` ①，第 1 轮 `propose` ②、`post_verify` ③、`update` ④，第 2 轮 ⑤⑥⑦……）。
2. 对每个钩子抄下：调用处行号、传入的实参、消费/返回的数据类。
3. 打开 [deepspec/eval/dspark/evaluator.py:99-115](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L99-L115)，确认 `_propose` 构造的 `draft_input_ids[:, 0] = output_ids[:, start]`（第 115 行）如何满足「`verify_input_ids` 必须以当前 token 开头」的校验；再确认 `max_proposal_tokens` 属性（第 40-42 行，取 `draft_model.block_size`）如何保证不超上限。
4. 用伪代码写出如果想让 Eagle3 复用本循环，`propose` 返回的 `draft_token_count` 与 `draft_probs` 应该各是什么形状。

**需要观察的现象**：钩子调用严格满足「init 一次 → (propose → verify → post_verify → update)×T」的嵌套结构；`update` 永远发生在游标推进之后（[deepspec/eval/base_evaluator.py:421-426](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L421-L426)），所以 `update` 内部读 `verification.accepted_draft_tokens` 时 `output_ids` 与 `start` 已是推进后的值。

**预期结果**：得到一张 4 列表（序号 / 钩子 / 关键入参 / 关键产出）；第 3 步能明确指出「锚点 token 就是合同里的 current token」；第 4 步答案为 `[1, n+1]` 与 `[1, n, V]`。

#### 4.2.5 小练习与答案

**练习 1**：如果某个 `propose` 实现返回的 `verify_input_ids` 开头不是 `output_ids[:, start]`，会在哪里、以什么方式失败？

**答案**：在 `verify_draft_tokens` 的入参校验处立刻抛 `ValueError`（[deepspec/eval/base_evaluator.py:205-212](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L205-L212)）。这是「合同前置检查」的典型做法：宁可早失败，也不让错位的 KV 悄悄产生错误分布。

**练习 2**：`effective_proposal_length` 和 `accepted_draft_tokens` 在什么情况下相等、什么情况下不等？

**答案**：正常轮 `effective_proposal_length = n`（提议的草稿数），`accepted_draft_tokens = a ≤ n`，只要没有全接受就不等；当被接受前缀中出现停止符时，两者被同时截断为 `eos_pos + 1`（[deepspec/eval/base_evaluator.py:271-276](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L271-L276)），此时相等。

**练习 3**：为什么 `update` 钩子设计成「无返回值、原地改 context」，而不是让它返回新的状态？

**答案**：`context` 是算法私有对象（DSpark 里是 `SimpleNamespace`，含草稿 cache 和目标隐状态两个大张量），原地修改避免每轮重建对象与多余拷贝；同时主循环不关心 context 的内部结构，返回值反而会诱导框架去理解算法状态。这与 4.1 的不变量设计一致：主循环只管 `output_ids`、`start`、目标 cache 三样「公共财产」。

### 4.3 KV cache 裁剪与循环推进

#### 4.3.1 概念说明

这是本讲最精妙的一段。问题陈述：每轮 verify 前向把 **n+1** 个候选 token（当前 token + n 个草稿）的 KV 全部写进了目标 cache；但只有前 **a+1** 个（当前 token + 被接受的 a 个草稿）被真正提交，其余 \(n-a\) 个被拒草稿的 KV 是「废料」。若不清理，下一轮 verify 会把新候选接到这些废料后面——目标模型将在一条**从未被接受的虚构上下文**上做前向，输出分布完全错误，且 cache 随轮数无限膨胀。

解法是每轮末尾的 `past_key_values_target.crop(start)`：把 cache 截断回 `start` 个位置，只保留与 `output_ids[:, :start]` 对应的 KV。`DynamicCache.crop` 本身只是张量切片，但放在哪个时机、截到哪个长度，正是循环正确性的关键。

#### 4.3.2 核心流程

游标 `start` 的推进规则与 cache 的同步，用一轮（n 个草稿、接受 a 个、未触停止符）来演示：

```
轮开始:  cache 长度 = start（不变量成立），位置 start 是待消费 token c
propose: verify_input_ids = [c, d1, ..., dn]
verify:  目标前向 n+1 个 token, position_ids[start .. start+n]
         cache 长度: start → start + n + 1
         （fed token 第 i 个的 KV 落在 cache 位置 start+i）
接受:    d1..da 被接受，d_{a+1} 被拒；兜底采样得 r
写回:    output_ids[start .. start+a-1] = [c 已在, d1..da]，output_ids[start+a] = r
推进:    start_new = start + a + 1
crop:    crop(start_new) → cache 长度 start+n+1 → start+a+1
         丢掉 d_{a+1..n} 的 KV（cache 位置 start+a+1 .. start+n）
不变量:  cache 长度 == start_new，KV 对应 output_ids[:, :start_new]
         位置 start_new 上是新兜底 token r —— 新的待消费 token
```

注意一个容易被忽略的细节：兜底 token `r` 写进了 `output_ids`，但它的 KV **尚未计算**——它将是下一轮 verify 前向喂进去的第一个 token（也就是下一轮 `verify_input_ids[:, 0]`）。目标模型对它的 logits 会在下一轮才产生。这个「领先一步」的安排使得「每轮恰好提交 a+1 个 token」与「每轮目标前向恰好 n+1 个 token」严丝合缝。

两条出口规则：

- **正常出口**：`while start < max_length` 不再成立，或新写入 token 含停止符（[deepspec/eval/base_evaluator.py:428-429](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L428-L429)）。最终输出取 `output_ids[:, : min(start+1, max_length)]`——含位置 `start` 上的最后提交 token。
- **停止符出口**：被接受前缀内出现停止符（[deepspec/eval/base_evaluator.py:415-419](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L415-L419)）：`acceptance_lengths` 记 `a`（没有兜底 token，故不加 1），`start += a`，`crop(start)` 后 `break`。停止符 token 本身不进 cache 也无所谓——生成就此结束。

#### 4.3.3 源码精读

**接受前缀写回与统计。** [deepspec/eval/base_evaluator.py:407-413](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L407-L413)：

```python
proposal_lengths.append(int(verification.effective_proposal_length))
accepted_draft_tokens = int(verification.accepted_draft_tokens)
accepted_draft_lengths.append(accepted_draft_tokens)

output_ids[:, start : start + accepted_draft_tokens + 1] = (
    proposal.verify_input_ids[:, : accepted_draft_tokens + 1]
)
```

写回的是 `[c, d1..da]` 共 a+1 个 token（`c` 原地重写一次，值不变），其中 `c` 来自提议、`d1..da` 是被接受草稿。

**停止符分支。** [deepspec/eval/base_evaluator.py:415-419](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L415-L419)：

```python
if verification.terminated_by_stop_token:
    acceptance_lengths.append(accepted_draft_tokens)
    start += accepted_draft_tokens
    past_key_values_target.crop(start)
    break
```

对比正常分支少加 1：没有兜底 token。`crop` 之后 cache 对应 `output_ids[:, :start]`，最终输出又通过 `[:, :start+1]` 把停止符包含进来。

**正常推进：本讲的核心四行。** [deepspec/eval/base_evaluator.py:421-426](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L421-L426)：

```python
output_ids[:, start + accepted_draft_tokens + 1] = verification.next_token
new_token_ids = output_ids[:, start + 1 : start + accepted_draft_tokens + 2]
acceptance_lengths.append(accepted_draft_tokens + 1)
start += accepted_draft_tokens + 1
past_key_values_target.crop(start)
update(context, verification)
```

依次：写兜底 token 到位置 `start+a+1`；切出本轮新写入的 token（供停止符检查，第 428 行用）；统计提交数 `a+1`；游标与 cache 同步推进；最后才调用 `update` 让草稿算法状态跟上。**`crop(start)` 紧跟 `start +=`**，两行共同维护不变量。

**收尾截断。** [deepspec/eval/base_evaluator.py:431-441](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L431-L441) 把缓冲区裁到真实长度，再经 `trim_output_ids`（[deepspec/eval/base_evaluator.py:58-72](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L58-L72)）砍掉停止符之后可能已写入的多余 token，返回带四个统计字段的 `SimpleNamespace`（`verify_count = len(proposal_lengths)`）。

**旁证：草稿侧同样靠 crop。** 草稿模型自己的 KV cache 也用同一招回退：[deepspec/eval/dspark/draft_ops.py:44](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/draft_ops.py#L44) 中的 `past_key_values_draft.crop(start)`（其时序含义留待 u6-l4 精读）。目标侧与草稿侧的 crop 共同保证两个 cache 与 `output_ids[:, :start]` 三者永远对齐。

#### 4.3.4 代码实践

**实践目标**：在不跑模型的情况下，手工推演「crop 前后 cache 长度与 start 的联动」，把不变量算清楚。

**操作步骤**：设定 `num_input_tokens = 6`（记 prompt 为 `p0..p5`）、`max_proposal_tokens = 7`、`max_new_tokens` 足够大。第一轮接受 a=3、第二轮接受 a=0。填写下表（已在「需要观察的现象」处给出参考答案，请先遮住答案自己填）：

| 时刻 | start | cache 长度（变化） | output_ids 新写入位置 | 本轮提交数 |
| --- | --- | --- | --- | --- |
| prefill 后 | 6 | 6 | 位置 6 ← 首 token t6 | 1（目标直采） |
| 第 1 轮 verify 后、crop 前 | 6 | 6 → 14 | — | — |
| 第 1 轮 crop 后（a=3） | ? | 14 → ? | 位置 7,8,9 ← d1,d2,d3；位置 10 ← r1 | ? |
| 第 2 轮 verify 后、crop 前 | ? | ? → 18 | — | — |
| 第 2 轮 crop 后（a=0） | ? | 18 → ? | 位置 11 ← r2 | ? |

**需要观察的现象**：每轮「crop 后 cache 长度」是否都等于「新的 start」；每轮提交数是否都等于 `a+1`。

**预期结果**：第 1 轮 start=10、cache 14→10、提交 4；第 2 轮 start=11、cache 18→11、提交 1。两轮共提交 5 个生成 token（t6 之外新增 d1,d2,d3,r1,r2），`acceptance_lengths = [4, 1]`。此练习是第 5 节综合实践的铺垫，此处结论应与综合实践的模拟器输出一致。

#### 4.3.5 小练习与答案

**练习 1**：假如删掉主循环里的 `past_key_values_target.crop(start)`，第二轮会发生什么？

**答案**：第一轮 verify 后 cache 长度是 `start+n+1`，含被拒草稿的 KV。第二轮 verify 会把新候选接到位置 `start+n+1` 之后，但 `verify_position_ids` 仍从 `position_ids[:, start']` 切片（[deepspec/eval/base_evaluator.py:216](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L216)）——上下文内容与位置编号双双错位：目标模型实际在验证「被拒草稿续写的序列」，logits 与真实前缀不对应，输出分布错误；同时 cache 每轮净增 \(n-a\) 条废料，显存持续膨胀。

**练习 2**：把本讲的不变量用一句话说全。

**答案**：每轮循环顶部（以及 prefill 刚结束时），目标 KV cache 的长度等于 `start`，其内容恰好是 `output_ids[:, :start]` 中各 token 的 KV；位置 `start` 上的 token 是「待消费」的——已写入输出但尚未进入目标 cache，它将是本轮 `verify_input_ids` 的第一个元素。

**练习 3**：为什么输出缓冲区只需 `max_length + max_proposal_tokens + 1`，而不是 `max_length + 2 * max_proposal_tokens` 之类的更大值？

**答案**：进入循环的前提是 `start < max_length`，即 `start ≤ max_length - 1`；单轮最远写入位置是兜底 token 的 `start + a + 1 ≤ start + n + 1 ≤ max_length + K`。缓冲区需要覆盖索引 `max_length + K`，故长度 `max_length + K + 1` 恰好够用（[deepspec/eval/base_evaluator.py:337-341](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L337-L341)）。

## 5. 综合实践

**任务**：完成规格要求的时序推演——`max_proposal_tokens = 7`，两轮验证分别接受 3 个和 0 个草稿 token——画出（或用表格表示）`output_ids`、`past_key_values_target`、`start` 的逐轮变化，并**验证每轮恰好提交 accepted + 1 个 token**。

### 5.1 手工时序图

设定 prompt 长度 6（`p0..p5`），`max_new_tokens` 足够大，两轮后因达到演示需要而停止（真实循环会继续）：

```
prefill:
  output_ids   = [p0 p1 p2 p3 p4 p5 t6 _ _ _ _ ...]   ← t6 由目标分布直采
  cache(目标)  = KV(p0..p5)                 长度 6
  start        = 6          （位置 6 的 t6 待消费）

第 1 轮 (n=7, a=3):
  propose      → verify_input_ids = [t6 d1 d2 d3 d4 d5 d6 d7]      (8 个)
  verify       → 目标前向 8 token, position 6..13；cache 6 → 14
                  拒绝采样: d1 d2 d3 ✓, d4 ✗ → 兜底采样 r1
  写回         → output_ids[7..9] = d1 d2 d3;  output_ids[10] = r1
  统计         → acceptance_lengths += [4]   (a+1 = 4 ✓)
  推进+裁剪    → start: 6 → 10;  crop(10): cache 14 → 10
                  （丢弃 d4..d7 的 KV，即 cache 位置 10..13）
  update       → 草稿状态对齐到被接受前缀

第 2 轮 (n=7, a=0):
  propose      → verify_input_ids = [r1 e1 e2 e3 e4 e5 e6 e7]
  verify       → 目标前向 8 token, position 10..17；cache 10 → 18
                  拒绝采样: e1 ✗ → 兜底采样 r2
  写回         → output_ids[11] = r2
  统计         → acceptance_lengths += [1]   (a+1 = 1 ✓)
  推进+裁剪    → start: 10 → 11;  crop(11): cache 18 → 11
                  （丢弃 e1..e7 的 KV，即 cache 位置 11..17）

结果: output_ids[:, :12] = [p0..p5, t6, d1, d2, d3, r1, r2]
      acceptance_lengths = [4, 1] → accept_len = 2.5
      proposal_lengths   = [7, 7] → n̄ = 7, verify_rate = (4+1)/(8+8) = 0.3125
```

两个校验点：每轮提交数 = a+1（4 与 1）；每轮 crop 后 cache 长度 = 新 start（10 与 11），且位置 start 上的 token（r1、r2）恰是下一轮第一个被喂进目标模型的 token。

### 5.2 用模拟器验证（示例代码）

下面是**示例代码**（非仓库代码，纯 Python、无 torch/GPU 依赖），把主循环的簿记逻辑——`output_ids` 写入、`start` 推进、cache 长度变化——按源码语义复刻一遍，用断言验证「每轮提交 a+1」：

```python
"""模拟 generate_decoding_sample 的游标与 cache 簿记（示例代码）。"""

def simulate(num_input_tokens=6, max_proposal_tokens=7,
             max_new_tokens=64, accepted_per_round=(3, 0)):
    max_length = num_input_tokens + max_new_tokens
    # 缓冲区大小按源码分配: max_length + K + 1
    output_ids = [None] * (max_length + max_proposal_tokens + 1)
    output_ids[:num_input_tokens] = [f"p{i}" for i in range(num_input_tokens)]
    output_ids[num_input_tokens] = "t6"          # 首 token: 目标直采, 不经验证
    cache_len = num_input_tokens                  # prefill 后 cache 长度
    start = num_input_tokens
    assert cache_len == start                     # 不变量: prefill 后

    acceptance_lengths, proposal_lengths = [], []
    for a in accepted_per_round:
        assert start < max_length
        n = max_proposal_tokens
        fed = [output_ids[start]] + [f"d{start}_{j}" for j in range(1, n + 1)]
        cache_len += len(fed)                     # verify 前向: cache += n+1
        committed = fed[: a + 1] + [f"r{start}"]  # 接受前缀 + 兜底 token
        assert len(fed[:a+1]) == a + 1            # 合同: 提交 a+1 个
        for k, tok in enumerate(committed):
            output_ids[start + k] = tok           # 写回 [start, start+a+1]
        start += a + 1
        cache_len_crop = start                    # crop(start)
        assert cache_len_crop == start            # 不变量恢复
        acceptance_lengths.append(a + 1)
        proposal_lengths.append(n)
        print(f"轮次: n={n}, a={a} | 提交 {a+1} 个 {committed} | "
              f"start→{start} | cache {cache_len}→{cache_len_crop}")
        cache_len = cache_len_crop

    print("生成序列:", output_ids[: start + 1])
    print("acceptance_lengths:", acceptance_lengths)
    print("accept_len =", sum(acceptance_lengths) / len(acceptance_lengths))
    print("verify_rate =", sum(acceptance_lengths) / sum(p + 1 for p in proposal_lengths))

simulate()
```

**操作步骤**：保存为 `loop_sim.py` 并运行 `python loop_sim.py`。

**需要观察的现象**：第 1 轮打印 `提交 4 个`、`start→10`、`cache 14→10`；第 2 轮打印 `提交 1 个`、`start→11`、`cache 18→11`；两个断言全程通过。

**预期结果**：`acceptance_lengths: [4, 1]`、`accept_len = 2.5`、`verify_rate = 0.3125`，与 5.1 手工时序图逐项一致。若想进一步对拍真实实现，可在有 GPU 的环境下参照 4.1.4 的思路给 `verify_draft_tokens` 加两行 `print`（cache 前后长度、accepted 数），与模拟器输出对照（**待本地验证**，需要目标模型与草稿 checkpoint）。

## 6. 本讲小结

- **prefill 与首 token**：目标模型 prefill 时用 `logits_to_keep=1` 只取末位 logits、`output_hidden_states=True` 供草稿取特征；首 token 从目标分布直接采样、不经验证，它既是 DSpark 的锚点 / Eagle3 的链头，也保证「每轮至少推进 1 个 token」从第一步就成立。
- **钩子设计**：`generate_decoding_sample` 只实现算法无关的投机解码协议（验证前向、拒绝采样、游标推进、统计收集），`init_context` / `propose` / `update` / `post_verify` 四个钩子承载全部算法差异，DSpark 与 Eagle3 共用同一循环。
- **每轮提交规则**：正常轮提交 `accepted_draft_tokens + 1` 个 token（a 个被接受草稿 + 1 个兜底），`start += a + 1`；接受前缀命中停止符的轮提交 a 个、`start += a` 后直接 break，`acceptance_lengths` 不加 1。
- **crop 的作用**：verify 前向会把 n+1 个候选的 KV 全部写入目标 cache，`crop(start)` 把被拒草稿的废料 KV 裁掉，使 cache 恢复为「长度 == start，内容对应 `output_ids[:, :start]`」；兜底 token 是新一轮的待消费 token，其 KV 留待下一轮计算。
- **统计量出生地**：`acceptance_lengths`（a+1 / 停止轮 a）、`proposal_lengths`（截断后的有效 n）、`accepted_draft_lengths`（a）在每轮循环内收集，经 `allreduce` 汇总为 `accept_len = Σ(a+1)/T` 与 `verify_rate = Σ(a+1)/Σ(n+1)`。
- **防御性合同**：`verify_draft_tokens` 入口即校验「草稿数不超上限、`verify_input_ids` 以当前 token 开头」，把协议违规拦截在污染 cache 之前。

## 7. 下一步学习建议

本讲刻意把 `verify_draft_tokens` 里最核心的一段——接受概率 \( \min(1, p_{\text{target}}/p_{\text{draft}}) \)、`cumprod` 前缀掩码、`sample_residual` 兜底——当作黑盒接口（a 与 next_token）使用。下一讲 **u6-l3（验证与拒绝采样：verify_draft_tokens 的数学与实现）** 将专门证明：这套拒绝采样为何能保证最终输出分布与纯目标解码**逐 token 相同**，并用蒙特卡洛实验验证理论接受长度 \( \sum_t \prod_{s \le t} \min(1, p/q) \)。之后再进入 **u6-l4（DSpark 评估器）**，看 `_propose` / `_update` 两个钩子在块式提议下如何维护草稿侧自己的 KV cache（预告：[deepspec/eval/dspark/draft_ops.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/draft_ops.py) 里同样有一处 `crop`，其时机安排比目标侧更讲究）。建议在进入 u6-l3 前，先把本讲 5.2 的模拟器改出两个变体玩一玩：让第 2 轮全部接受（a=7），或让某轮 `draft_token_count` 小于 K（置信度早停的情形），观察提交数与 crop 长度如何随之变化。
