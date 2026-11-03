# Rollout 采样：sample_response

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清「rollout」在 GRPO 中的角色：它是强化学习里「策略（模型）对一条 prompt 生成的一条完整回答」，`sample_response` 就是生产 rollout 的采样器。
2. 逐行读懂 [reasoning_from_scratch/ch06.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py) 中的 `sample_response` 函数：prefill、温度缩放、top-p 过滤、multinomial 采样、EOS 提前停止、跳过最后一次前向。
3. 准确说出它与第 4 章生成函数（`generate_text_top_p_stream_cache`）的关键差异——尤其是三个容易踩坑的点：`temperature=0` 并**不**退化为贪心、EOS **会**计入返回结果、函数内部**不**切换 eval 模式。
4. 解释它为什么返回「完整 `token_ids` + `prompt_len` + 文本」三件套，以及下游 `compute_grpo_loss` 如何分别消费这三样东西。
5. 独立完成实践：对同一 prompt 采样多个 rollout，统计其长度与内容的差异，并验证确定性的真正来源是随机种子而非 `temperature=0`。

## 2. 前置知识

本讲建立在前面几讲的概念之上，先做一次快速回顾，再补一个新知识点。

**Rollout 与策略（承接 u7-l1）**。在强化学习语境里，被训练的模型叫「策略」（policy）；策略对一条提示生成的一条完整回答叫一个 rollout（也可称 completion，二者在 LLM 语境下常互换）。GRPO 的做法是：对同一条 prompt 采样多条 rollout，用上一讲讲的 `reward_rlvr` 给每条打分，再在组内做相对比较得到优势。所以本讲的 `sample_response` 是 GRPO 的「原料车间」。

**采样流水线（承接 u5-l1 与 u5-l2）**。给定末位 logits \( z \)，采样分四步：

1. 温度缩放：\( z' = z / T \)；
2. 概率化：\[ p_i = \frac{\exp(z_i/T)}{\sum_j \exp(z_j/T)} \]
3. top-p（核采样）过滤：只保留按概率降序累积质量首次达到 `top_p` 的最小集合，核外置零后重归一化；
4. `torch.multinomial` 按概率有放回抽一个 token。

温度 \( T<1 \) 锐化分布、\( T>1 \) 平滑分布；top-p 负责砍掉长尾。

**KV cache 两阶段（承接 u2-l5）**。生成分 prefill（整段 prompt 一次前向，同时把全部位置的 K/V 写入缓存）与 decode（每步只送 1 个新 token）两阶段。`KVCache` 是「每层一个槽位」的列表容器；模型上有一个位置游标 `current_pos`，与缓存必须成对管理：新建缓存的同时要把游标 `reset_kv_cache()` 清零。

**新概念：`@torch.no_grad()` 与 `@torch.inference_mode()` 的区别**。两者都能关闭梯度追踪、节省显存，但 `inference_mode` 更激进：在它作用下创建的张量叫 inference tensor，之后**不允许**再参与任何被 autograd 追踪的计算，否则报错 `Inference tensors cannot be saved for backward`。本讲的 `sample_response` 用的是 `@torch.no_grad()`——原因见 4.1.3。

## 3. 本讲源码地图

| 文件 | 本讲关注点 | 关键位置 |
| --- | --- | --- |
| `reasoning_from_scratch/ch06.py` | 主角 `sample_response`；它的三个下游消费者 | L53-L98（本讲主角）、L111-L117（`sequence_logprob`）、L120-L187（`compute_grpo_loss`） |
| `reasoning_from_scratch/ch04.py` | 被复用的 `top_p_filter`；用于对比的生成函数 | L194-L222（`top_p_filter`）、L225-L267（`generate_text_top_p_stream_cache`） |
| `reasoning_from_scratch/qwen3.py` | `KVCache` 容器、`current_pos` 游标、`reset_kv_cache` | L422-L437（`KVCache`）、L56-L105（forward 与游标） |
| `ch06/01_main-chapter-code/ch06_main.ipynb` | 6.5 节教学版：解释 `no_grad` 选型、演示不同 seed 的 rollout | [notebook](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/01_main-chapter-code/ch06_main.ipynb) |
| `tests/test_ch06.py` | 用哑模型 + 哑分词器单测 `sample_response` | L12-L46（哑对象）、L104-L121（测试本体） |

## 4. 核心概念与源码讲解

### 4.1 sample_response：rollout 采样主循环

#### 4.1.1 概念说明

第 2 章和第 4 章的生成函数是为「聊天」设计的：接收 token 张量、`yield` 逐个吐 token、边生成边打印。GRPO 需要的则是一台「采样器」：不打印、不流式，但要返回**可以直接拿去重新计算对数概率的完整 token 序列**。`sample_response` 就是把第 2 章的 KV cache 生成骨架与第 4 章的温度 + top-p 采样「瘦身合并」后的产物（notebook 6.5 节原话：它只是把之前开发的组件组合成一个更精简的版本）。

它的返回值是一个三元组，每一元都有明确下游：

| 返回值 | 内容 | 下游消费者 |
| --- | --- | --- |
| `full_token_ids` | prompt + 生成拼接的一维张量 | `sequence_logprob(model, token_ids, prompt_len)`：带梯度重算序列对数概率 |
| `prompt_len` | prompt 的 token 数（Python int） | 同上，用于切出「只算答案区间」的起点 `prompt_len - 1` |
| 文本 `tokenizer.decode(generated)` | 生成部分的解码文本 | `reward_rlvr(text, ground_truth)`：提取 `\boxed{}` 并判分 |

另有派生指标 `gen_len = token_ids.numel() - prompt_len`（生成 token 数，含 EOS），用于训练日志的平均响应长度。

与第 4 章 `generate_text_top_p_stream_cache` 相比，除「输入 prompt 文本而非 token 张量、返回三件套而非 yield」这两处形态差异外，还有三处**行为**差异，是本讲最重要的考点：

| 差异点 | 第 4 章生成函数 | `sample_response` |
| --- | --- | --- |
| `temperature=0`/`None` | 显式 `argmax` 贪心分支 | **没有**该分支：跳过缩放，仍走 multinomial 随机采样 |
| EOS 处理 | 采样后先查 EOS 再 `yield`，EOS **不进**输出 | 先 append 再查 EOS 再 break，EOS **计入** `generated` |
| 模式管理 | 函数内第一行 `model.eval()` | 不切换模式，由调用方 `compute_grpo_loss` 统一管理 |

#### 4.1.2 核心流程

```text
sample_response(model, tokenizer, prompt, device,
                max_new_tokens=512, temperature=0.8, top_p=0.9):
    1. prompt --tokenizer.encode--> input_ids（一维张量）
    2. cache = 新 KVCache(层数); model.reset_kv_cache()   # 配对：新缓存 + 游标清零
    3. prefill：logits = model(input_ids, cache)[:, -1]    # 整段前向，取末位
    4. for step in range(max_new_tokens):
         a. 若 temperature 非零且 ≠ 1.0：logits /= temperature
         b. probas = softmax(logits)
         c. probas = top_p_filter(probas, top_p)
         d. next_token = multinomial(probas.cpu(), 1).to(device)
         e. generated.append(token_id)
         f. 若 token_id == eos_token_id：break             # EOS 计入后再停
         g. 若 step+1 < max_new_tokens：logits = model(next_token, cache)[:, -1]
                                              # 跳过最后一次（不再被使用的）前向
    5. return cat(input_ids, generated), len(input_ids), decode(generated)
```

注意采样分布的完整形式：在温度与核过滤双重作用下，第 \( t \) 步实际抽样的概率为

\[ \tilde p_i = \frac{p_i \cdot \mathbb{1}[i \in \text{nucleus}(p,\, \text{top\_p})]}{\sum_{j \in \text{nucleus}} p_j}, \qquad p_i = \frac{\exp(z_i/T)}{\sum_j \exp(z_j/T)} \]

#### 4.1.3 源码精读

**装饰器与签名**。[reasoning_from_scratch/ch06.py:L53-L62](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L53-L62) 用 `@torch.no_grad()` 关闭梯度，默认参数 `max_new_tokens=512, temperature=0.8, top_p=0.9` 正是 GRPO 训练的默认采样配置。为什么不用更「快」的 `@torch.inference_mode()`？notebook 6.5 节给出了原文解释：inference tensor 不能被保存用于反向传播，会报 `RuntimeError: Inference tensors cannot be saved for backward`；而 rollout 返回的 token 序列随后要交给**带梯度**的 `sequence_logprob` 使用，所以这里必须用 `no_grad`。

**编码与 prefill**。[reasoning_from_scratch/ch06.py:L63-L70](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L63-L70) 把 prompt 编码成一维张量，然后做两件 KV cache 配对动作——新建容量为 `n_layers` 的空缓存、把模型游标清零——再整段前向取末位 logits（prefill 阶段，详见 4.3）。

**采样循环**。[reasoning_from_scratch/ch06.py:L72-L92](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L72-L92) 是主体，四个细节值得逐一咀嚼：

- **温度判断 `if temperature and temperature != 1.0:`（L74-L75）**。当 `temperature=0` 或 `None` 时条件为假，**跳过缩放**——但注意后面仍然是 softmax + top-p + multinomial，也就是说此处 `temperature=0` 的效果约等于 `temperature=1`（不缩放的随机采样），而**不是**第 4 章那样的贪心 argmax。对比 [reasoning_from_scratch/ch04.py:L244-L245](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L244-L245)，那里的生成函数显式写了 `if temperature is None or temperature == 0.0: next_token = torch.argmax(...)` 贪心分支。这是两代函数最容易混淆的差异（综合实践会专门验证）。顺带一提，这里也**没有** `scale_logits_by_temperature` 的 `temperature <= 0` 防护（见 4.1.5 练习 3）。
- **采样与搬运（L77-L81）**：softmax 后接 `top_p_filter`（4.2 详解），再 `torch.multinomial(probas.cpu(), num_samples=1).to(device)`——先搬到 CPU 采样、再搬回模型设备，沿用第 4 章 [reasoning_from_scratch/ch04.py:L258](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L258) 的同一写法。
- **EOS 先记录再停止（L83-L90）**：`token_id` 先 `append` 进 `generated`，然后才检查是否等于 `tokenizer.eos_token_id` 并 `break`。因此 **EOS token 会出现在返回的 `token_ids` 与解码文本里**（base 模型的 EOS 是 `<|endoftext|>`；分词器 decode 保留特殊 token，承接 u2-l6，所以 notebook 里文本末尾能看到 `<|endoftext|>`）。一种合理的解读是：对 GRPO 而言「何时停下来」也是策略行为的一部分，序列打分理应覆盖停止决策——书中未明说此意图，但机制如此。
- **跳过最后一次前向（L91-L92）**：只有 `step + 1 < max_new_tokens` 才对刚采到的 token 做前向。循环最后一步采到的 token 已不会再被使用，为它前向纯属浪费；命中 EOS 时同样不再前向。

**返回三件套**。[reasoning_from_scratch/ch06.py:L94-L98](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L94-L98) 把 Python 列表 `generated` 重建为张量，与 `input_ids` 拼成一维 `full_token_ids`，同时返回 `input_ids.numel()`（prompt 长度）与解码文本。

**三件套的消费现场**。[reasoning_from_scratch/ch06.py:L137-L162](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L137-L162) 是 `compute_grpo_loss` 的 rollout 循环：`text` 喂给 `reward_rlvr` 算奖励（L149）；`token_ids` 与 `prompt_len` 一起喂给 `sequence_logprob`（L152），该函数（[reasoning_from_scratch/ch06.py:L111-L117](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L111-L117)）对整段序列做一次 teacher-forcing 前向，用 `gather` 抽出每个真实 next token 的 logprob，再从 `prompt_len - 1` 起只对答案区间求和——这正是返回 `prompt_len` 的意义：下游无需重新分词即可切出答案区间；`gen_len = token_ids.numel() - prompt_len`（L160）则进日志。另注意 [reasoning_from_scratch/ch06.py:L134-L135](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L134-L135) 与 [L164-L165](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L164-L165)：rollout 前 `model.eval()`、结束后恢复 `model.train()`——因为 `sample_response` 内部**不**做模式切换（第 4 章生成函数的第一行就是 `model.eval()`，见 [reasoning_from_scratch/ch04.py:L234](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L234)），管理权上移给了 GRPO 这一层。

#### 4.1.4 代码实践

**实践目标**：不下载任何权重，用仓库自带的哑模型单元测试验证你对 `sample_response` 行为的理解——尤其是「EOS 计入输出」与「prefill → 一步 decode → 命中 EOS」的完整路径。

**操作步骤**：

1. 先阅读 [tests/test_ch06.py:L12-L46](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch06.py#L12-L46) 的两个哑对象：`DummyTokenizer` 的 `eos_token_id=0`、decode 时 id 2 映射为 `"X"`、其余映射为数字字符串；`DummySampleModel` 在输入长度 >1（prefill）时把 token 2 的 logit 置为最大，输入长度 =1（decode）时把 token 0（即 EOS）置为最大。
2. 手推：`temperature=1.0, top_p=0.0`（`top_p=0` 会只保留概率最大的 token，见 4.2.3）时，`token_ids / prompt_len / text` 各是什么？
3. 运行该测试：

```bash
uv run pytest tests/test_ch06.py::test_sample_response_generates_until_eos -q
```

**需要观察的现象**：测试通过；断言内容（[tests/test_ch06.py:L118-L121](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch06.py#L118-L121)）为 `prompt_len == 2`、`text == "X0"`、`token_ids == [5, 6, 2, 0]`——注意最后一项：末尾的 `0` 正是 EOS，证明它被计入了输出。

**预期结果**：与你的手推一致。若想进一步确认「EOS 先 append 再 break」，可临时在本地把断言 `token_ids.tolist() == [5, 6, 2, 0]` 改成 `[5, 6, 2]`，观察测试变红（改完请还原，不要提交）。

#### 4.1.5 小练习与答案

**练习 1**：调用 `sample_response(temperature=0)` 会得到确定性的贪心输出吗？

**答案**：不会。`if temperature and temperature != 1.0:` 中 `0` 为假值，跳过温度缩放，随后依然 softmax → top-p → `multinomial` 随机采样，效果近似 `temperature=1`。要贪心应改用第 4 章 `generate_text_top_p_stream_cache(temperature=0.)`（它有显式 argmax 分支），或如单元测试那样用 `top_p=0.0` 只保留最大概率 token。

**练习 2**：为什么 `sample_response` 要返回 `prompt_len`，而不是让下游自己对 prompt 重新 `encode` 算长度？

**答案**：`sequence_logprob` 需要答案区间起点 `prompt_len - 1` 来执行「只对答案求和」。直接返回本次生成所用的同一分词结果，既省一次重复编码，也避免下游重新编码时因文本拼接方式不同导致边界错位。

**练习 3**：若传入 `temperature=-1.0`，`sample_response` 会发生什么？与第 4 章的 `scale_logits_by_temperature` 行为有何不同？

**答案**：`-1.0` 为真值且不等于 1.0，于是执行 `logits / -1`，logits 整体取反——softmax 后原本最不可能的 token 反而概率最高，采样行为完全颠倒。第 4 章 [reasoning_from_scratch/ch04.py:L83-L86](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L83-L86) 的 `scale_logits_by_temperature` 对 `temperature <= 0` 会抛 `ValueError` 防护；`sample_response` 内联了除法、没有这层防护，属于使用时需自行避开的坑。

### 4.2 top_p_filter 复用

#### 4.2.1 概念说明

`sample_response` 没有重新实现核采样，而是直接 `from .ch04 import top_p_filter`（[reasoning_from_scratch/ch06.py:L9](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L9)）。这是「一章一模块」仓库分层设计的直接收益：评测（第 3 章）、推理时扩展（第 4、5 章）与训练（第 6 章）共享同一条采样语义，保证「训练时怎么采」与「此前实验怎么采」完全一致。核采样的定义（承接 u5-l2）：保留按概率降序排列后、累积质量首次达到 `top_p` 的最小 token 集合（「核」），核外概率置零并重归一化。

#### 4.2.2 核心流程

对单行概率向量 \( p \)（已按降序排列记为 \( p_{(1)} \ge p_{(2)} \ge \dots \)），累积质量与保留判据为

\[ C_i = \sum_{j \le i} p_{(j)}, \qquad \text{keep}_{(i)} \iff C_i - p_{(i)} < \text{top\_p} \]

即「轮到第 \( i \) 个 token 之前，累积质量还没攒够 `top_p`」就保留它。五步流水线：排序 → 累积和 → 判据选核 → 置零并回填原词表位置 → 重归一化。

#### 4.2.3 源码精读

**函数本体**（第 4 章已精读，此处只看与 `sample_response` 的衔接）。[reasoning_from_scratch/ch04.py:L194-L222](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L194-L222)：L195-L196 是捷径分支——`top_p=None` 或 `>= 1.0` 时原样返回，不做任何过滤；L206-L207 用 `prefix = cumprobas - sorted_probas`（每个 token 之前的累积质量）与 `top_p` 比较选核；L209 `keep[:, 0] = True` 兜底保证核非空——**当 `top_p=0` 或极小时，核里只剩概率最大的那一个 token**，此时 multinomial 等价于 argmax，这正是单元测试用 `top_p=0.0` 制造确定性的手法；L220-L222 以 `clamp_min(1e-12)` 防除零后重归一化。

**调用点**。[reasoning_from_scratch/ch06.py:L77-L78](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L77-L78)：过滤被插在 softmax 之后、multinomial 之前——温度负责重塑分布形状，top-p 负责砍掉长尾，二者是流水线上的两道工序。`sample_response` 的默认 `top_p=0.9` 与自一致性投票（u5-l4）所用值一致。

#### 4.2.4 代码实践

**实践目标**：脱离模型，单独驱动 `top_p_filter`，亲手验证核采样的数值结果，并确认 `top_p=0` 的「只留最大」行为。

**操作步骤**（以下为示例代码，只需安装了 torch 即可运行，无需下载模型）：

```python
# 示例代码
import torch
from reasoning_from_scratch.ch04 import top_p_filter

probas = torch.tensor([[0.5, 0.41, 0.09]])   # 必须是二维：(batch, vocab)
for tp in (0.9, 0.0, 1.0):
    print(tp, top_p_filter(probas, tp))
```

**需要观察的现象与预期结果**（按公式手算可得，浮点误差在小数点后若干位）：

- `top_p=0.9`：降序累积为 \( 0.5,\,0.91,\,1.0 \)，前缀质量 \( 0,\,0.5,\,0.91 \)，小于 0.9 的只有前两个 → 保留后重归一化 \([0.5/0.91,\ 0.41/0.91,\ 0] \approx [0.5495,\, 0.4505,\, 0.0]\)。
- `top_p=0.0`：判据全为假，但 `keep[:, 0]=True` 兜底 → \([1.0,\, 0.0,\, 0.0]\)，等价贪心。
- `top_p=1.0`：命中 L195-L196 捷径，原样返回 \([0.5,\, 0.41,\, 0.09]\)。

注意输入必须是二维张量：函数沿 `dim=1` 排序并对 `keep[:, 0]` 赋值，一维输入会报错。数值请以本地实际打印为准（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`keep[:, 0] = True` 这行兜底为什么必不可少？

**答案**：若 `top_p` 取 0 或极小值，前缀判据可能一个 token 都不保留，过滤后全零，重归一化将除零（即便有 `clamp_min` 也会得到全零分布使 multinomial 无法抽样）。强制保留概率最大的 token 保证核非空、采样永远可行。

**练习 2**：在 `sample_response` 中把 `top_p` 设为 `None`，采样行为如何变化？

**答案**：`top_p_filter` 的 L195-L196 捷径生效，概率原样返回——退化为纯温度采样，分布长尾完全参与抽样；多样性更高，但也更容易采到离谱 token。GRPO 默认不用这个设置。

**练习 3**：第 6 章直接 `import top_p_filter` 而不是复制一份代码，除了省行数还有什么实际好处？

**答案**：语义一致性。自一致性投票（第 4 章）、Best-of-N（第 5 章）与 GRPO rollout（第 6 章）共用同一个过滤器，保证各章实验中「采样」的含义完全相同；若各自维护副本，修 bug 或改行为时极易产生难以察觉的分叉。

### 4.3 KVCache 使用：每次 rollout 一套全新缓存

#### 4.3.1 概念说明

`KVCache`（[reasoning_from_scratch/qwen3.py:L422-L437](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L422-L437)）本身极简：一个长度为层数的列表，`get/update` 按层索引存取每层的 `(keys, values)`，不含任何张量运算。真正需要小心的是它与模型级游标 `current_pos` 的配对关系（承接 u2-l5）：缓存装的是「过去的 K/V」，游标记的是「过去有多长」，两者一旦不配套，RoPE 位置与因果掩码就会错位。`sample_response` 的策略是最干净的一种——**每次调用都新建缓存并清零游标**（[reasoning_from_scratch/ch06.py:L68-L69](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L68-L69)），函数结束缓存随之丢弃。GRPO 每个 prompts 要连续调用 `num_rollouts` 次，这种「一次性」用法让多次 rollout 互不污染。

#### 4.3.2 核心流程

一次 `sample_response` 调用内的缓存状态演变（设 prompt 长 \( P \)，生成 \( N \) 个 token）：

| 时刻 | 动作 | `current_pos` | 缓存每层 K/V 的 token 维 |
| --- | --- | --- | --- |
| 进入时 | `KVCache(n_layers)` + `reset_kv_cache()` | 0（清零） | 空 |
| prefill | 整段 prompt 前向 | 0 → \( P \) | \( P \) |
| decode 第 1 步 | 送入 1 个新 token 前向 | \( P \) → \( P+1 \) | \( P+1 \) |
| … | 每步 +1 | … | … |
| decode 第 N 步 | 送入最后 token 前向（若发生） | \( P+N-1 \) → \( P+N \) | \( P+N \) |
| 返回 | 缓存被丢弃 | 保留脏值，等下次调用清零 | — |

#### 4.3.3 源码精读

**容器**。[reasoning_from_scratch/qwen3.py:L422-L437](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L422-L437)：`KVCache.cache = [None] * n_layers`，`get(i)/update(i, value)` 按层号存取，`reset()` 全部置 `None`。`sample_response` 从不调用 `reset()`——它直接换一个新容器。

**游标在 forward 中的生命周期**。[reasoning_from_scratch/qwen3.py:L64-L73](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L64-L73)：`pos_start` 取自 `current_pos`（无 cache 时为 0），`pos_end = pos_start + num_tokens`；越界检查（L66-L70，超过 `context_length` 抛 `ValueError`）先于一切状态修改；检查通过后才推进 `self.current_pos = pos_end`。掩码切片（L74-L76）取 `[pos_start:pos_end, :pos_end]`，含义是「新 token 只被禁止看到严格晚于它的位置」——prefill 时还原为严格上三角，decode 时得到全 False 的宽行（承接 u2-l5）。

**逐层更新**。[reasoning_from_scratch/qwen3.py:L92-L98](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L92-L98)：forward 循环里每个 TransformerBlock 拿到上一轮自己的 `blk_cache`，返回新缓存后立即 `cache.update(i, new_blk_cache)` 写回。缓存放的是 `repeat_interleave` 展开之前的压缩版 K/V（每层形状约 `(1, n_kv_groups, t, head_dim)`，0.6B 配置下即 `(1, 8, t, 128)`，承接 u2-l3）。

**清零入口**。[reasoning_from_scratch/qwen3.py:L104-L105](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L104-L105) 的 `reset_kv_cache()` 只做一件事：`current_pos = 0`。`sample_response` 在新建缓存的同时调用它（[reasoning_from_scratch/ch06.py:L68-L69](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L68-L69)）——新缓存本身是干净的，但游标挂在模型上，上次调用若中途弃置（例如命中 EOS 提前 break）会留下非零脏值，不清零的话本次 prefill 的 RoPE 偏移与掩码起点就会整体错位。这正是 u2-l5 总结过的「自愈式」配对纪律。

#### 4.3.4 代码实践

**实践目标**：源码阅读型实践——亲手追踪一次「prefill 4 个 token + 2 步 decode」的缓存状态，把 KV cache 生命周期表填完整。

**操作步骤**：

1. 在 [reasoning_from_scratch/qwen3.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py) 中找到并抄下三处代码：游标初始化（L56）、前向中推进游标的语句（L72-L73）、清零方法（L104-L105）。
2. 设 prompt 为 4 个 token、`max_new_tokens=2` 且不触发 EOS，仿照 4.3.2 的表格，为每一步填写：调用（prefill / decode 第几步）、`pos_start`、`pos_end`、`current_pos`（前向后）、mask 的形状。
3. 对照答案自查。参考答案：prefill 一次（`pos_start=0, pos_end=4`，mask `(4, 4)` 上三角）；decode 第 1 步（`pos_start=4, pos_end=5`，mask `(1, 5)` 全 False）；decode 第 2 步（`pos_start=5, pos_end=6`，mask `(1, 6)` 全 False）。注意第 2 步采样后因 `step + 1 == max_new_tokens` 不再前向。

**需要观察的现象**：每步 mask 的形状与 `current_pos` 严格对应；prompt 越长 decode 步的 mask 越宽（列数 = 全部历史 token 数）。

**预期结果**：你填的表与参考答案一致；若不一致，回到 L63-L76 逐行核对自己的 `pos_start/pos_end` 推导。

#### 4.3.5 小练习与答案

**练习 1**：如果注释掉 `model.reset_kv_cache()`（ch06.py L69），第一次调用通常仍正常，第二次调用会发生什么？

**答案**：第一次调用结束时游标停在 prompt+生成长度处。第二次调用的 prefill 会把 `pos_start` 错接到这个脏值上：RoPE 按错误偏移旋转、掩码切片整体右移，输出 logits 全错；若两次累计长度超过 `context_length` 还会触发 L66-L70 的 `ValueError`。新建空缓存只保证「过去的 K/V 是空的」，救不了记录「现在在哪」的游标。

**练习 2**：`KVCache(n_layers=model.cfg["n_layers"])` 为什么必须从模型配置取层数？

**答案**：forward 的块循环（qwen3.py L92-L98）按层号 `i` 逐层 `cache.get(i)` / `cache.update(i, ...)`，缓存槽位与 Transformer 层一一对应；层数不符会在访问层号时越界或留下永远为 `None` 的层。

**练习 3**：缓存里每层存的 keys 是 16 个查询头展开后的版本，还是 8 个 KV 组的压缩版本？

**答案**：压缩版本，形状 `(1, 8, t, 128)`。GQA 中 `torch.cat` 增量拼接发生在 `repeat_interleave` 展开之前（承接 u2-l3/u2-l5），缓存因此省一半 K/V 空间。

## 5. 综合实践

**任务**：用真实的 0.6B base 模型对同一 prompt 采样 5 个 rollout，统计长度与内容差异；再用实验揭穿「`temperature=0` 等于确定性」的直觉，找到确定性的真正来源。

**环境**：需要先完成 u1-l2 的安装并下载 base 权重（`load_model_and_tokenizer` 会自动触发下载）。CPU 可运行但较慢（共约 7 次 × 128 token 生成）；有 GPU/MPS 更佳。

以下为示例代码（综合了 notebook 6.3 与 6.5 节的加载与调用方式）：

```python
# 示例代码
import torch
from reasoning_from_scratch.ch02 import get_device
from reasoning_from_scratch.ch03 import load_model_and_tokenizer, render_prompt
from reasoning_from_scratch.ch06 import sample_response

device = get_device()
model, tokenizer = load_model_and_tokenizer(
    which_model="base", device=device, use_compile=False
)

raw_prompt = (
    "Half the value of $3x-9$ is $x+37$. "
    "What is the value of $x$?"
)
prompt = render_prompt(raw_prompt)

# Part A：5 个 rollout（temperature=0.8）
texts, lens = [], []
for i in range(5):
    torch.manual_seed(i)                      # 每个 rollout 换一个种子
    token_ids, prompt_len, text = sample_response(
        model=model, tokenizer=tokenizer, prompt=prompt,
        device=device, max_new_tokens=128,
        temperature=0.8, top_p=0.9,
    )
    gen_len = token_ids.numel() - prompt_len  # 生成 token 数（含 EOS）
    lens.append(gen_len)
    texts.append(text)
    print(f"[{i}] len={gen_len:3d}  head={text[:60]!r}")
print("互不相同的 rollout 数:", len(set(texts)), "/ 5")

# Part B：temperature=0，两个不同种子
for seed in (100, 101):
    torch.manual_seed(seed)
    _, _, text = sample_response(
        model=model, tokenizer=tokenizer, prompt=prompt,
        device=device, max_new_tokens=128,
        temperature=0, top_p=0.9,             # 关键设置
    )
    print(f"[seed={seed}] head={text[:60]!r}")

# Part C：同一颗种子跑两遍
torch.manual_seed(7)
_, _, t1 = sample_response(model=model, tokenizer=tokenizer, prompt=prompt,
                           device=device, max_new_tokens=128, temperature=0.8, top_p=0.9)
torch.manual_seed(7)
_, _, t2 = sample_response(model=model, tokenizer=tokenizer, prompt=prompt,
                           device=device, max_new_tokens=128, temperature=0.8, top_p=0.9)
print("同 seed 两次输出完全一致:", t1 == t2)
```

**需要观察的现象与预期结果**：

1. **Part A**：5 个 rollout 的 `len` 各不相同（有长有短），`head` 内容互有差异，`len(set(texts))` 通常为 5。notebook 6.5 节的同类实验（temperature=0.9、seed 0 与 5）一个直接给出 ` \boxed{58}`、另一个给出完整分步推导并答 `83`，可作参照。具体数值待本地验证。
2. **Part B（本实践的关键发现）**：两次输出**仍然不同**。回到 [reasoning_from_scratch/ch06.py:L74-L75](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L74-L75)：`if temperature and temperature != 1.0:` 在 `temperature=0` 时为假，只是**跳过缩放**，采样仍是随机的——`sample_response` 没有第 4 章那样的 argmax 贪心分支。
3. **Part C**：`t1 == t2` 为 `True`。确定性的真正来源是 `torch.manual_seed` 固定了 multinomial 的随机数序列，而不是温度取 0。
4. 文本末尾可能出现 `<|endoftext|>` 字样——EOS 被计入输出（4.1.3），decode 保留特殊 token。

**记录建议**：把 Part A 的 5 行 `len/head` 与 Part B/C 的结论整理成一张表；在表下用一句话回答——GRPO 需要组内 rollout 有差异性（否则组内优势全为零、模型不更新，见 u7-l1），这就是 `sample_response` 默认 `temperature=0.8` 而非 0 的原因。

## 6. 本讲小结

- `sample_response`（ch06.py L53-L98）是 GRPO 的 rollout 采样器：把第 2 章 KV cache 生成骨架与第 4 章温度 + top-p 采样合并瘦身，不流式、不打印，返回 `full_token_ids + prompt_len + 文本` 三件套。
- 三件套各有下游：文本喂 `reward_rlvr` 判分；`token_ids + prompt_len` 喂 `sequence_logprob` 带梯度重算答案区间对数概率；`numel() - prompt_len` 进训练日志。
- 两个反直觉行为：`temperature=0/None` 只会跳过缩放、**不会**退化为贪心（无 argmax 分支，区别于第 4 章生成函数）；EOS 先记录再 break，**计入**返回的 token 序列与文本。
- 工程细节：装饰器用 `@torch.no_grad()` 而非 `inference_mode`，因为返回张量之后要进入带 autograd 的计算；函数内部不切换 eval/train 模式，由 `compute_grpo_loss` 在 rollout 前后统一管理；最后一次前向被跳过。
- `top_p_filter` 自 ch04 直接 import 复用，插在 softmax 与 multinomial 之间；`top_p=None/1.0` 不过滤，`top_p=0` 借 `keep[:, 0]=True` 兜底退化为只留最大概率 token。
- KV cache 遵守「每次调用新建容器 + `reset_kv_cache()` 清零游标」的配对纪律：容器干净不够，挂在模型上的 `current_pos` 脏值也必须清，否则 RoPE 与掩码错位。

## 7. 下一步学习建议

下一讲（u7-l3「GRPO 损失函数」）将把本讲的三件套接进 [reasoning_from_scratch/ch06.py:L120-L187](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L120-L187) 的 `compute_grpo_loss`：rollout 的奖励如何做组内标准化变成优势、`sequence_logprob` 为什么求和而非求平均、策略梯度损失 \( \mathcal{L}_{\mathrm{PG}} = -\frac{1}{N}\sum_i A_i \log p(y^{(i)}) \) 为何取负号。建议先自行阅读 notebook 6.7-6.10 节与 `sequence_logprob`（L111-L117），思考「logprob 为什么必须保持张量而不 `.item()`」——答案就在本讲反复强调的 no_grad/inference_mode 之辨里。
