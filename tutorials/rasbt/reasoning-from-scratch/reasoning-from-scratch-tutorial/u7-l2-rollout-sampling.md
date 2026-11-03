# Rollout 采样：sample_response

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 **rollout** 这个强化学习术语的含义，以及它为什么是 GRPO 一切学习信号的源头。
2. 逐行读懂 [reasoning_from_scratch/ch06.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py) 中的 `sample_response` 函数：它如何自建 `KVCache`、复用第 4 章的 `top_p_filter`、用 `multinomial` 采样、在 EOS 处提前停止，并跳过最后一次无用的前向。
3. 对比 rollout 采样与第 4 章生成函数的异同——尤其是两处容易踩坑的行为差异（EOS 是否入输出、`temperature=0` 是否退化为贪心）。
4. 解释返回值 `(token_ids, prompt_len, text)` 三件套为何是「为 GRPO 量身定制」的接口设计。
5. 动手统计同一 prompt 的多个 rollout 的长度与内容差异，验证随机采样带来的多样性。

---

## 2. 前置知识

本讲站在前面多讲的基础上，先用三段话把需要的概念备齐。

**（1）rollout：RL 行话里的「一次生成」**

在第 4~5 章里，我们管模型的输出叫「回答」（response）；进入第 6 章后，教材改用 **rollout** 这个强化学习术语。notebook 6.5 节说得很直白："Rollout is RL jargon for generated response"——rollout 与 completion 可以互换使用。为什么 RL 要换一个词？因为强化学习把语言模型看作**策略（policy）**：给定前文（状态），模型输出下一个 token 的概率分布（动作）。一条 rollout 就是「策略在环境里走了一步完整的轨迹」——从 prompt 出发自回归地生成出整个回答。GRPO 的学习信号（奖励、优势、对数概率）全部从 rollout 中来，所以 rollout 采样是 GRPO 训练循环的**第一块零件**。

**（2）随机采样为什么是必需品而非装饰**

第 4~5 章里，温度采样、top-p 过滤是「推理时扩展」的技巧：多次采样 + 投票可以提升准确率。到了 GRPO，随机采样升级为**探索机制**：`compute_grpo_loss` 对同一个 prompt 采样 `num_rollouts` 条不同回答，用组内相对比较构造优势（好的 rollout 获得正优势、差的获得负优势）。如果每次采样都产出完全相同的文本，组内就没有差异，`rewards.std()` 为零，优势全为零，模型无从学习。因此 `sample_response` 的默认参数是 `temperature=0.8, top_p=0.9`——仓库标准的「RL 采样配置」。

**（3）两块旧零件：KVCache 与 top_p_filter**

- **KVCache**（u2-l5 精读过）：一个「每层一个槽位」的纯 Python 列表容器（[qwen3.py:L422-L437](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L422-L437)）。有了它，生成从「每步对全序列前向」变成「prefill 一次 + 每步只前向 1 个新 token」。模型对象上还有一个位置游标 `current_pos`，记录缓存写到哪里了。
- **top_p_filter**（u5-l2 精读过）：核采样过滤器（[ch04.py:L194-L222](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L194-L222)），五步流水线：降序排序 → 累积和 → 用「前缀质量 < top_p」选核 → 置零核外并回填原词表位置 → 重归一化，且 `keep[:, 0] = True` 保证核非空。本讲的 `sample_response` 通过 `from .ch04 import top_p_filter`（[ch06.py:L9](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L9)）原样复用它。

另外请回忆两件事：`@torch.no_grad()` 与 `@torch.inference_mode()` 都能关闭自动求导，但语义有细微差别（本讲 4.4 会展开）；第 2~4 章的生成函数族都是 **generator**（`yield` 逐 token 产出），本讲的函数**不是**。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [reasoning_from_scratch/ch06.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py) | 本讲主角。`sample_response`（L53-L98）是 rollout 采样器；`sequence_logprob`（L111-L117）与 `compute_grpo_loss`（L120-L187）是它的下游消费者，本讲只看调用点，内部留给 u7-l3 |
| [reasoning_from_scratch/ch04.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py) | 被 `from .ch04 import top_p_filter` 复用的核采样过滤器（L194-L222）；其 `generate_text_temp_stream_cache`（L150-L192）是与 `sample_response` 对比的关键参照 |
| [reasoning_from_scratch/qwen3.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py) | `KVCache` 类（L422-L437）、`Qwen3Model.forward` 的缓存游标逻辑（L63-L73）与 `reset_kv_cache`（L104-L105） |
| [ch06/01_main-chapter-code/ch06_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/01_main-chapter-code/ch06_main.ipynb) | 教材 6.5 节：函数的诞生现场与两个种子的对比实验 |
| [tests/test_ch06.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch06.py) | `test_sample_response_generates_until_eos`（L104-L121）用假模型锁定 EOS 行为 |
| [tests/test_generation_final_forward.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_generation_final_forward.py) | 把 `sample_response` 与全书各生成函数放进同一参数化夹具，断言「最后一个 token 不做多余前向」（L12-L24, L75-L99） |

---

## 4. 核心概念与源码讲解

### 4.1 rollout 采样器的定位：一个「为 RL 瘦身」的生成函数

#### 4.1.1 概念说明

`sample_response` 不是从零发明的新东西。notebook 6.5 节作者自己总结：它就是把第 2 章的 `generate_text_basic_stream_cache`（KV 缓存式流式生成）与第 4 章的温度 + top-p 采样**直接合并**后的精简版（"a leaner version"）。瘦身体现在三处：

1. **不流式**：不再 `yield` 每个 token——训练循环不需要实时打印，token 收进 Python 列表即可。
2. **自己编码**：吃文本 `prompt` 和 `tokenizer`，内部完成 encode；而第 2~4 章的生成函数吃的是现成的 `token_ids` 张量。
3. **输出面向下游算法**：返回 `(token_ids, prompt_len, text)` 三件套，恰好是 GRPO 下两步（对数概率、奖励）需要的输入形状。

#### 4.1.2 核心流程

整条 rollout 的生成流程可以画成：

```text
文本 prompt
   │ tokenizer.encode
   ▼
input_ids (1维张量) ──► 新建 KVCache（每层一个空槽位）
                    ──► model.reset_kv_cache()（位置游标归零）
                    ──► prefill：整段前向一次，取最末位置 logits
   ▼
┌─ 循环 max_new_tokens 次 ──────────────────────────┐
│  logits / temperature        （温度缩放，可跳过）      │
│  softmax → 概率分布                                 │
│  top_p_filter(probas, top_p) （核采样截断+重归一化）   │
│  multinomial → next_token    （按概率随机抽取）        │
│  generated.append(token_id)                        │
│  若 token_id == eos_token_id → break（EOS 入输出）    │
│  若还没到最后一步 → 单 token 前向，取 logits          │
└───────────────────────────────────────────────────┘
   ▼
返回 (prompt+生成的完整 token_ids, prompt_len, 生成的文本)
```

#### 4.1.3 源码精读

先看函数签名与装饰器：

```python
@torch.no_grad()
def sample_response(
    model, tokenizer, prompt, device,
    max_new_tokens=512, temperature=0.8, top_p=0.9,
):
```

对应 [ch06.py:L53-L62](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L53-L62)。这段定义了 rollout 的「采样预算」（`max_new_tokens=512`，比第 3 章评测的默认值更宽裕）与「探索强度」（`temperature=0.8, top_p=0.9`）。注意对比第 4 章 `generate_text_top_p_stream_cache` 的默认 `temperature=0.`（贪心）——rollout 场景默认值反转为随机采样，这正是「评测技巧 → 探索机制」的角色转变在参数默认值上的投影。

再看与第 4 章生成函数的**行为差异总表**（本讲最重要的对照，建议抄在笔记上）：

| 维度 | ch04 生成函数族 | ch06 `sample_response` |
|------|----------------|------------------------|
| 输入 | `token_ids` 张量 | 文本 `prompt` + `tokenizer`（内部 encode） |
| 输出 | generator，逐 token `yield` | 一次性返回 `(token_ids, prompt_len, text)` |
| 装饰器 | `@torch.inference_mode()`（如 [ch04.py:L150](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L150)） | `@torch.no_grad()`（[ch06.py:L53](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L53)） |
| EOS 去向 | 检查在 `yield` 之前，EOS **不产出**（[ch04.py:L185-L189](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L185-L189)） | 先 append 再检查，EOS **保留在输出里**（[ch06.py:L83-L90](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L83-L90)） |
| `temperature=0` | 显式 `argmax` 分支，退化为贪心（[ch04.py:L170-L171](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L170-L171)） | **无** argmax 分支，T=0 等价于 T=1，仍是随机采样（[ch06.py:L74-L81](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L74-L81)） |
| eos 来源 | 函数参数 `eos_token_id` | `tokenizer.eos_token_id` 属性 |

后两行差异的机制在 4.3 详解——它们是本讲两个最值得做的「坑点实验」。

#### 4.1.4 代码实践

**实践：从 notebook 复现「同题不同 rollout」**

1. **实践目标**：亲眼确认随机采样让同一道题产出多条不同的 rollout，且正确性不一——这是 GRPO 组内比较有意义的前提。
2. **操作步骤**：打开 [ch06_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/01_main-chapter-code/ch06_main.ipynb) 6.5 节，按顺序执行 6.3 节的模型加载单元（`load_model_and_tokenizer(which_model="base", ...)`），然后找到两次调用 `sample_response` 的单元（分别设了 `torch.manual_seed(0)` 与 `torch.manual_seed(5)`，`temperature=0.9, top_p=0.9, max_new_tokens=512`）。若本机跑不动 notebook，可直接阅读其输出。
3. **需要观察的现象**：seed=0 的 rollout 极短，直接给出 ` \boxed{58}<|endoftext|>`（正确答案就是 58）；seed=5 的 rollout 是一篇长长的分步推导，最终给出 `\boxed{83}`——**同一个模型、同一道题，两条 rollout 走向不同答案**。
4. **预期结果**：长推导那条反而答错了。同时注意 decode 出的文本末尾带着 `<|endoftext|>` 字样——这是 4.1.3 对照表第 4 行（EOS 入输出）的直观证据。
5. 具体生成文本随环境（设备、torch 版本）浮动，输出内容**待本地验证**；但「两条 rollout 内容不同、末尾含 `<|endoftext|>`」这两点由源码结构保证。

#### 4.1.5 小练习与答案

**练习 1**：GRPO 中如果 `sample_response` 是确定性的（每次返回同一文本），训练会发生什么？

**答案**：同组 `num_rollouts` 条 rollout 完全相同 → 奖励完全相同 → `rewards - rewards.mean()` 全为 0 → 优势全为 0 → 策略梯度损失恒为 0，模型不更新。随机采样提供的多样性是 GRPO 组内相对比较的学习信号来源。

**练习 2**：作者说 `sample_response` 是第 2 章与第 4 章代码的「合并精简版」。请指出它从第 2 章继承了什么、从第 4 章继承了什么、又删掉了什么。

**答案**：从第 2 章继承 KV 缓存式生成骨架（新建 `KVCache` + `reset_kv_cache` + prefill/decode 两阶段 + 跳过最后一次前向）；从第 4 章继承温度缩放与 top-p 过滤、multinomial 采样；删掉的是流式 `yield`（改为列表收集）与逐 token 打印相关的逻辑。

---

### 4.2 准备阶段：自建 KVCache 与状态归零

#### 4.2.1 概念说明

`sample_response` 的前三行准备工作，是 u2-l5 讲过的「缓存生命周期自愈」模式在 RL 场景的复刻：**每次调用都新建一份缓存、把位置游标归零**，保证无论上一次调用如何收场（正常结束、异常中断、被 Ctrl-C 打断），这一次 rollout 都从位置 0 干净起步。对训练循环来说这尤为重要——`compute_grpo_loss` 会在一步之内连续调用 `sample_response` 多次（每条 rollout 一次），任何一次留下脏的 `current_pos`，下一次 prefill 的 RoPE 位置和因果掩码就全错了。

#### 4.2.2 核心流程

```text
prompt 文本 ──tokenizer.encode──► input_ids（1 维张量，长度 = prompt_len）
KVCache(n_layers) ──► 每层一个 None 槽位的新容器
model.reset_kv_cache() ──► model.current_pos = 0
model(input_ids.unsqueeze(0), cache=cache)[:, -1]
   ──► prefill：一次前向填满缓存，取最末位置的 logits 作为「下一个 token」的分布依据
```

#### 4.2.3 源码精读

准备阶段三连（编码、建缓存、归零 + prefill）：

```python
input_ids = torch.tensor(
    tokenizer.encode(prompt),
    device=device
    )

cache = KVCache(n_layers=model.cfg["n_layers"])
model.reset_kv_cache()
logits = model(input_ids.unsqueeze(0), cache=cache)[:, -1]
```

对应 [ch06.py:L63-L70](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L63-L70)。逐行说明：

- **L63-L66**：把文本 prompt 编码成 token id 张量。`encode` 的行为由 `Qwen3Tokenizer` 决定——base 模型走裸文本 BPE 编码（u2-l6）。
- **L68**：`KVCache(n_layers=model.cfg["n_layers"])` 自建缓存容器。看它的实现（[qwen3.py:L422-L437](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L422-L437)）：`__init__` 只是 `self.cache = [None] * n_layers`，外加 `get` / `update` / `get_all` / `reset` 四个薄方法——一个纯粹的数据容器，不含任何张量计算。0.6B 模型 `n_layers=28`，所以是 28 个空槽位。
- **L69**：`model.reset_kv_cache()` 把模型级游标归零（[qwen3.py:L104-L105](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L104-L105)，实现就一行 `self.current_pos = 0`）。注意它**不清理旧缓存对象**——所以必须与 L68 的「新建容器」**成对使用**：新容器本来就是空的，归零的游标配新容器才自洽。若只归零游标而复用旧容器，旧 K/V 会与新 token 拼在一起，输出错乱。
- **L70**：prefill。`model(input_ids.unsqueeze(0), cache=cache)` 升维成 `(1, prompt_len)` 做整段前向：模型 `forward` 内部读 `current_pos` 作 `pos_start`、算出 `pos_end` 并在成功后更新游标（[qwen3.py:L63-L73](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L63-L73)），28 个块逐层把 `(keys, values)` 写进缓存槽位（[qwen3.py:L92-L98](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L92-L98)）。`[:, -1]` 只保留最后一个位置的 logits——自回归生成每步只关心「下一个 token」的分布。

一个常被忽略的细节：`input_ids.unsqueeze(0)` 造出的 batch 维固定为 1。`sample_response` 是**单序列**采样器；想要一次采一整批 rollout，要用 ch06/02 目录下的批量化变体 `sample_responses_batched`（u7-l4 会讲）。

#### 4.2.4 代码实践

**实践：读测试，验证「EOS 入输出」与三件套的形状**

1. **实践目标**：不下载权重、不跑真模型，仅凭 `pytest` 与假模型理解函数行为契约。
2. **操作步骤**：阅读并运行 [tests/test_ch06.py:L104-L121](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch06.py#L104-L121)（命令：`uv run pytest tests/test_ch06.py::test_sample_response_generates_until_eos -v`）。该测试用 `DummySampleModel`（prefill 时把 token 2 的 logit 抬到 0、其余压到 -1e9；decode 时抬 token 0）与 `DummyTokenizer`（`eos_token_id=0`）驱动 `sample_response(max_new_tokens=5, temperature=1.0, top_p=0.0)`。
3. **需要观察的现象**：断言 `prompt_len == 2`、`text == "X0"`、`token_ids.tolist() == [5, 6, 2, 0]` 全部通过。
4. **预期结果**：末尾的 `0` 正是 EOS token——它**留在了** `generated` 里，decode 后成为 `"0"`；这与第 2 章贪心函数「EOS 不入输出」相反。另外 `top_p=0.0` 会触发 `top_p_filter` 的 `keep[:, 0]=True` 兜底（只留最高概率 token），配合 one-hot 的假 logits，把随机采样变成了确定结果——测试借此锁死行为。本实践在 CPU 上秒级完成，可直接运行验证。
5. 若想进一步确认 `reset_kv_cache` 被调用过：测试断言 `model.reset_called is True`（[test_ch06.py:L118](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch06.py#L118)）——4.2.3 讲的「成对使用」也有测试兜底。

#### 4.2.5 小练习与答案

**练习 1**：如果把 L69 的 `model.reset_kv_cache()` 删掉，在「循环调用 `sample_response` 多次」的 GRPO 场景下会出现什么错误？

**答案**：第一次调用正常结束时模型 `current_pos` 已被推进到 `prompt_len + 生成长度`。第二次调用 prefill 时 `pos_start` 从旧值起算，RoPE 会给 prompt 的 token 编上错误的位置、因果掩码的形状与语义也随之错位；若累计位置超过 `context_length`（40960），则直接触发 `ValueError`。而如果两次 prompt 长度恰好相同且未越界，输出也会是静默的错误结果。

**练习 2**：`KVCache` 为什么设计成每个模型层占一个槽位，而不是把全部层的 K/V 拼成一个大张量？

**答案**：Transformer 的 28 个块串行执行，第 i 层只在它前向的瞬间产生新的 `(keys, values)`；列表结构让 `forward` 循环里每层就地 `cache.update(i, new_blk_cache)`（[qwen3.py:L97-L98](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L97-L98)），无需知道其他层的信息。各层张量形状一致但彼此独立，拼接成大张量只会增加耦合与转置开销，毫无收益。

---

### 4.3 采样循环精读：温度、top-p 复用、multinomial 与两种停止

#### 4.3.1 概念说明

循环体是 `sample_response` 的心脏，每轮做四件事：**温度缩放 → 概率化 → 核采样截断 → 随机抽取**，随后处理两种停止条件（EOS 提前停止、生成预算用尽时的「最后一次前向跳过」）。流水线中温度与 top-p 的分工在 u5-l1/u5-l2 已建立：温度**重塑分布形状**（T<1 锐化、T>1 平滑），top-p **砍掉长尾**（只保留累积质量达标的核），先reshape 后截断的顺序不可交换。本讲的新看点是：这套流水线在 RL 语境下如何被「极简」地实现，以及两个与第 4 章不同的坑点行为。

#### 4.3.2 核心流程

每一步采样内部的数值流水线：

\[ p_i = \frac{\exp(z_i / T)}{\sum_{j \in \text{vocab}} \exp(z_j / T)} \quad\text{（温度缩放 + softmax）} \]

\[ \tilde{p}_i = \frac{p_i \cdot \mathbb{1}[i \in \text{core}]}{\sum_{j \in \text{core}} p_j} \quad\text{（top-p 截断 + 重归一化）} \]

其中「核」core 是按概率降序排列后、累积质量首次达到 `top_p` 的最小 token 集合。最后 `next_token ~ multinomial(\tilde{p})`——把分布变成一次随机抽取。

两个停止条件的执行顺序：

```text
采样并 append token_id
  ├─ token_id == eos_token_id？ ──是──► break（EOS 已在 generated 中）
  └─ step + 1 < max_new_tokens？ ──是──► 单 token 前向取新 logits
                    └──否──► 循环自然结束（最后一次前向被跳过）
```

#### 4.3.3 源码精读

循环体全文（[ch06.py:L72-L92](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L72-L92)）：

```python
generated = []
for step in range(max_new_tokens):
    if temperature and temperature != 1.0:
        logits = logits / temperature

    probas = torch.softmax(logits, dim=-1)
    probas = top_p_filter(probas, top_p)
    next_token = torch.multinomial(
        probas.cpu(), num_samples=1
    ).to(device)

    token_id = next_token.item()
    generated.append(token_id)

    if (
        tokenizer.eos_token_id is not None
        and token_id == tokenizer.eos_token_id
    ):
        break
    if step + 1 < max_new_tokens:  # Skips the forward pass after the last token is generated
        logits = model(next_token, cache=cache)[:, -1]
```

逐段拆解：

- **温度缩放的条件分支（L74-L75）**：`if temperature and temperature != 1.0`。除以 T=1 本是恒等变换，跳过它是纯省算；但注意 `temperature=0`（或 `None`）时条件同样为假——**直接跳过缩放**，接着照常 `softmax` + `multinomial`。也就是说，`sample_response` 里 `temperature=0` 并**不**退化为贪心 argmax，行为等同 `temperature=1` 的随机采样。对照第 4 章：`generate_text_temp_stream_cache` 有显式的 `if temperature is None or temperature == 0.0: next_token = torch.argmax(...)` 分支（[ch04.py:L170-L171](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L170-L171)），而 `sample_response` 把这个分支删掉了。若你在 GRPO 里想要确定性 rollout，传 `temperature=0` 是无效的——这是读源码才能发现的坑。
- **概率化与核采样（L77-L78）**：`torch.softmax(logits, dim=-1)` 得到 (1, vocab_size) 概率张量，随即 `top_p_filter(probas, top_p)`（导入自 [ch06.py:L9](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L9)）截断长尾并重归一化。过滤器内部（[ch04.py:L194-L222](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L194-L222)）的 `prefix = cumprobas - sorted_probas; keep = prefix < top_p` 判据与 `keep[:, 0] = True` 兜底在 u5-l2 推导过，此处不赘述；对 rollout 而言它保证「核非空」，使 `multinomial` 永远有 token 可抽，训练循环不会因一次误传参数而崩溃。
- **multinomial 与 `.cpu()`（L79-L81）**：`torch.multinomial(probas.cpu(), num_samples=1).to(device)`——先把概率张量搬到 CPU 再抽样、抽完搬回 `device`。`multinomial` 在部分加速设备（如 MPS）上支持不全或较慢，CPU 抽样是仓库生成函数的通用稳妥写法（第 4 章同款，见 [ch04.py:L180-L182](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L180-L182)）。`num_samples=1` 让它返回 (1, 1) 张量。
- **收集（L83-L84）**：`.item()` 转 Python int 后 `generated.append(token_id)`。用列表而非张量收集，避免每步 `torch.cat` 的反复内存分配；这也呼应「不流式」——没有消费者需要中途拿到 token。
- **EOS 提前停止（L86-L90）**：判断条件是 `tokenizer.eos_token_id is not None and token_id == tokenizer.eos_token_id`。注意顺序：**先 append 再 break**，EOS token 留在 `generated` 里、也留在 decode 出的文本里（u2-l6 讲过 `decode` 保留特殊 token，所以文本末尾能看到 `<|endoftext|>`）。这对 RL 是合理设计：完整的 token 序列（含终止符）将被送进 `sequence_logprob` 重算对数概率，EOS 本身也是策略「决定何时停」的一部分。对比第 2 章贪心函数：那里检查放在拼接**之前**、EOS 不入输出（u3-l1）。
- **跳过最后一次前向（L91-L92）**：`if step + 1 < max_new_tokens` 才执行 `logits = model(next_token, cache=cache)[:, -1]`。直觉：采样出第 `max_new_tokens` 个 token 后循环就结束了，这个 token 后面「下一个 token」的 logits 永远不会被使用，为它做一次前向纯属浪费。于是前向总次数 = 1 次 prefill + (实际生成数 − 1) 次 decode。该性质有专门的回归测试：`sample_response` 与全书各生成函数一起被参数化断言 `calls == [prompt_length] + [1] * (max_new_tokens - 1)`（[tests/test_generation_final_forward.py:L75-L83](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_generation_final_forward.py#L75-L83)，夹具注册见 [L17](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_generation_final_forward.py#L17)）；EOS 场景同样不多做前向（`calls == [8]`，[L86-L99](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_generation_final_forward.py#L86-L99)）。这个「末 token 免前向」优化从第 2 章一路继承而来（u3-l2 提过），在 GRPO 里价值被放大——rollout 动辄 512 token，每条省一次前向，乘以 rollout 数与训练步数就是可观开销。

#### 4.3.4 代码实践（本讲主实践）

**实践：统计 5 条 rollout 的多样性，并亲手验证 `temperature=0` 的坑**

1. **实践目标**：(a) 观察随机采样产生的 rollout 多样性（长度 + 内容）；(b) 用实验戳穿「T=0 应该是贪心」的直觉，回到源码解释现象。
2. **操作步骤**（示例代码，需在已安装本项目的环境中运行；权重约 1.2 GB，首次运行会自动下载）：

   ```python
   # 示例代码：rollout 多样性统计（保存为 rollout_demo.py 后运行）
   import torch
   from reasoning_from_scratch.ch02 import get_device
   from reasoning_from_scratch.ch03 import load_model_and_tokenizer, render_prompt
   from reasoning_from_scratch.ch06 import sample_response

   device = get_device()
   model, tokenizer = load_model_and_tokenizer(which_model="base", device=device)

   prompt = render_prompt(
       "Half the value of $3x-9$ is $x+37$. What is the value of $x$?"
   )

   torch.manual_seed(0)
   texts = []
   for i in range(5):
       token_ids, prompt_len, text = sample_response(
           model=model, tokenizer=tokenizer, prompt=prompt, device=device,
           max_new_tokens=128, temperature=0.8, top_p=0.9,
       )
       gen_len = token_ids.numel() - prompt_len          # 生成 token 数
       texts.append(text)
       print(f"#{i+1}  len={gen_len:3d}  head={text[:60]!r}")

   print("互不相同的 rollout 数 =", len(set(texts)))

   # 坑点实验：temperature=0 真的贪心吗？
   torch.manual_seed(1)
   _, _, t_a = sample_response(model=model, tokenizer=tokenizer, prompt=prompt,
                               device=device, max_new_tokens=64, temperature=0, top_p=0.9)
   torch.manual_seed(2)
   _, _, t_b = sample_response(model=model, tokenizer=tokenizer, prompt=prompt,
                               device=device, max_new_tokens=64, temperature=0, top_p=0.9)
   print("T=0 两次输出相同？", t_a == t_b)
   ```

3. **需要观察的现象**：5 条 rollout 的 `len` 参差不齐（个位数到 128 皆可能）、前 60 字符各异、`互不相同的 rollout 数 = 5`；多数文本以 `\boxed{...}` 收尾，不少末尾带 `<|endoftext|>`。
4. **预期结果**：多样性部分（长度不一、内容互异）由采样机制保证。坑点部分——基于 [ch06.py:L74-L75](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L74-L75) 的源码逻辑可以**确定地**推断：`temperature=0` 为假值跳过缩放分支，行为等同 T=1，两次不同 seed 的输出**大概率不同**（`T=0 两次输出相同？ False`）。若你先前学过 u5-l1「T=0 退化为贪心」，请特别留意那是 `generate_text_temp_stream_cache` 的行为——它有 argmax 分支，`sample_response` 没有。运行耗时与具体文本**待本地验证**（CPU 上 5×128 token 约需数分钟，建议 GPU）。
5. **延伸**：把 `temperature` 换成 `None` 再试一次，现象应与 T=0 相同（同一个 falsy 判断）；对照跑一次第 4 章的 `generate_text_top_p_stream_cache(temperature=0)`（如有条件），体会「同参数、不同函数、不同行为」。

#### 4.3.5 小练习与答案

**练习 1**：`top_p=1.0` 时 `top_p_filter` 返回什么？这时的采样等价于什么？

**答案**：看 [ch04.py:L195-L196](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch04.py#L195-L196)：`top_p >= 1.0` 直接原样返回概率。此时采样退化为「纯温度采样」——只受 temperature 控制的 softmax-multinomial。

**练习 2**：`max_new_tokens=3` 且从不触发 EOS，`sample_response` 一共做几次模型前向？分别处理多少个 token？

**答案**：3 次。1 次 prefill 处理整段 prompt（假设长 L），2 次 decode 各处理 1 个 token（step=0、step=1 各一次；step=2 是最后一个 token，触发 `step + 1 < max_new_tokens` 为假而跳过）。对照测试断言 `calls == [L, 1, 1]`。

**练习 3**：为什么 `sample_response` 把 EOS 保留在输出中，而第 2 章的 `generate_text_basic` 把它排除？

**答案**：用途不同。第 2 章面向「给人看文本」，EOS 无信息量还碍事，排除更干净；rollout 面向「给算法消费」，完整序列（含 EOS）要送进 `sequence_logprob` 计算序列级对数概率，「何时停」本身就是策略行为的一部分，截掉 EOS 反而丢失信息。

---

### 4.4 返回三件套与 GRPO 的消费方式

#### 4.4.1 概念说明

`sample_response` 的返回值是一个三元组 `(full_token_ids, prompt_len, text)`。这不是随手设计——它精确对应 GRPO 下游两步的输入需求：`sequence_logprob` 需要「完整序列 + prompt 长度」来定位答案区间并重算带梯度的对数概率；`reward_rlvr` 需要「生成文本」来提取 `\boxed{}` 并判分。一个函数调用，两个下游同时喂饱，调用方零重复编码。同样精心选择的是装饰器：`@torch.no_grad()` 而非第 2~4 章的 `@torch.inference_mode()`——这是本讲最后一个关键设计点。

#### 4.4.2 核心流程

```text
generated（Python list）──torch.cat──► full_token_ids = [prompt_ids ; generated]
                                        │           │
                prompt_len = len(prompt_ids)         │ text = decode(generated)
                    │                               │
                    ▼                               ▼
        sequence_logprob(model,                 reward_rlvr(text,
          token_ids, prompt_len)                  ground_truth)
        带梯度地重算答案区间的                     提取 \boxed{} 并
        序列级对数概率（u7-l3）                    数学判分（u7-l1）
```

#### 4.4.3 源码精读

返回语句（[ch06.py:L94-L98](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L94-L98)）：

```python
full_token_ids = torch.cat(
    [input_ids,
     torch.tensor(generated, device=device, dtype=input_ids.dtype),]
)
return full_token_ids, input_ids.numel(), tokenizer.decode(generated)
```

三件套逐一说：

1. **`full_token_ids`**：prompt 与生成部分拼成的完整一维张量。注意 `dtype=input_ids.dtype` 的对齐——生成的 id 是 Python int，拼接时显式转回输入的类型（整数类型），避免类型提升的隐患。这个完整序列正是 `sequence_logprob(model, token_ids, prompt_len)`（[ch06.py:L111-L117](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L111-L117)）的第一个参数：该函数会把整段序列**不带缓存地**整段前向一次（因为要用 teacher forcing 拿到全部位置的 logits），再用 `prompt_len` 切出答案区间的对数概率求和——这正是需要 prompt_len 的原因（推导在 u6-l1，GRPO 版求和而非求均的差异留给 u7-l3）。
2. **`prompt_len`**：`input_ids.numel()`，即 prompt 的 token 数。notebook 6.8 节作者点明这层关系："since we have the `sample_response` ... return the `token_ids` and `prompt_len`, we can drop the encoding and `full_ids` computation"——下游因此省掉重复编码。
3. **`text`**：`tokenizer.decode(generated)`，只解码生成部分（含 EOS）。它直接作为 `reward_rlvr(text, example["answer"])` 的输入（[ch06.py:L149](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L149)）。

再看消费现场——`compute_grpo_loss` 中的 rollout 循环（[ch06.py:L137-L162](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L137-L162)，完整拆解是 u7-l3 的任务，此处只看接口）：

```python
for _ in range(num_rollouts):
    # Stage 1: generate rollouts
    token_ids, prompt_len, text = sample_response(...)
    # Stage 2: compute rewards
    reward = reward_rlvr(text, example["answer"])
    # Stage 4: compute logprobs
    logp = sequence_logprob(model, token_ids, prompt_len)
```

三件套在四行之内被逐个消费（解包见 [L139-L147](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L139-L147)）；连 `gen_len = token_ids.numel() - prompt_len` 都被用来统计响应长度（[L160](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L160)）——三件套的每一件都有明确买主。

最后是装饰器之谜：为什么是 `@torch.no_grad()`（[ch06.py:L53](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L53)）而不是第 2~4 章通用的 `@torch.inference_mode()`？notebook 6.5 节给出了作者的原始注释：`inference_mode` 「做得太多」，会报

> RuntimeError: Inference tensors cannot be saved for backward.

原因是：GRPO 一步之内要**先**采样（不需要梯度）、**后**调用 `sequence_logprob`（需要梯度、要让 autograd 保存中间张量）。`inference_mode` 下产生的张量被标记为 inference tensor，之后被 autograd 计算图捕获保存时会被拒绝；`no_grad` 只是不在采样阶段建图，不产生这类受限张量，于是「采样的输出」与「训练的图」可以无缝衔接。这是「推理工具函数」升级为「训练循环内部件」时必须付出的工程注意，也是 u3-l2/u3-l3 讲过的纯推理场景里从未暴露过的问题。

#### 4.4.4 代码实践

**实践：零权重验证三件套的接口契约（CPU 秒级）**

1. **实践目标**：用假模型 + 假分词器亲自调用 `sample_response`，检查三件套的类型、长度与拼接关系；不下载任何权重。
2. **操作步骤**（示例代码，可直接保存为 `check_contract.py` 运行）：

   ```python
   # 示例代码：接口契约检查
   import torch
   from reasoning_from_scratch.ch06 import sample_response

   class TinyModel:
       """prefill 抬高 token 2；decode 抬高 token 0（eos）"""
       def __init__(self):
           self.cfg = {"n_layers": 1}
       def reset_kv_cache(self):
           pass
       def __call__(self, token_ids, cache=None):
           b, t = token_ids.shape
           logits = torch.full((b, t, 4), -1e9)
           if t > 1:
               logits[..., 2] = 0.0
           else:
               logits[..., 0] = 0.0
           return logits

   class TinyTok:
       eos_token_id = 0
       def encode(self, text):
           return [5, 6]           # 固定 prompt：2 个 token
       def decode(self, ids):
           return "".join({2: "X", 0: "0"}.get(i, "?") for i in ids)

   token_ids, prompt_len, text = sample_response(
       model=TinyModel(), tokenizer=TinyTok(), prompt="hi",
       device="cpu", max_new_tokens=5, temperature=1.0, top_p=0.0,
   )
   print(type(token_ids), token_ids.tolist())   # 期望 [5, 6, 2, 0]
   print(prompt_len)                            # 期望 2
   print(repr(text))                            # 期望 'X0'
   print("gen_len =", token_ids.numel() - prompt_len)  # 期望 2
   ```

3. **需要观察的现象**：`token_ids` 是一维张量且等于 prompt `[5, 6]` 与生成 `[2, 0]` 的拼接；`prompt_len == 2`；`text == "X0"`（X 是第一个生成 token，0 是 EOS——再次印证 EOS 入输出）。
4. **预期结果**：以上四个断言值均可从源码推出且该假模型与 `tests/test_ch06.py` 的官方假模型同构，CPU 上必现、无需联网（`temperature=1.0` 跳过缩放、`top_p=0.0` 触发兜底只留最高概率 token，故输出确定）。
5. **延伸思考**：把 `top_p=0.0` 改成 `0.9` 再跑几次，输出还确定吗？（提示：one-hot 分布下核里只有 1 个 token，仍然确定——可对照 u5-l2 的核定义想清楚为什么。）

#### 4.4.5 小练习与答案

**练习 1**：为什么 `sequence_logprob` 不复用 `sample_response` 已经建好的 KV cache，而要对完整序列重新做一次整段前向？

**答案**：两个原因。其一，缓存里的 K/V 是采样阶段在 `no_grad` 下产生的，不带梯度信息，而 `sequence_logprob` 需要可反向传播的计算图；其二，teacher forcing 打分需要**每个位置**的 logits（错位配对抽取 next-token 对数概率），缓存解码只返回末位 logits。所以干脆用完整序列无缓存地前向一次（[ch06.py:L112](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L112)），这正是三件套要返回 `token_ids + prompt_len` 的原因。

**练习 2**：`@torch.inference_mode()` 与 `@torch.no_grad()` 都关闭梯度记录，`sample_response` 选后者的实际原因是什么？

**答案**：`inference_mode` 产生的 inference tensor 不能被后续 autograd 计算图保存用于 backward。GRPO 一步内先采样后算 `sequence_logprob`（要 backward），若采样阶段用 `inference_mode`，其输出张量进入带梯度的计算就会触发 RuntimeError。`no_grad` 只是不建图、不产生受限张量，采样产物可安全进入后续训练计算。

**练习 3**：若想让 GRPO 一步内多条 rollout 共享同一次 prefill（同 prompt），`sample_response` 的哪个结构挡住了这条路？

**答案**：它把 batch 维写死为 1（`input_ids.unsqueeze(0)`）且每次调用新建 KVCache、调用 `model.reset_kv_cache()`——多条 rollout 必须串行多次调用、重复 prefill。批量化改造（同 prompt 的 N 份采样并成一个大 batch、共享 prefill）正是 ch06/02 目录 `sample_responses_batched` 做的事，u7-l4 精读。

---

## 5. 综合实践

**任务：给 `sample_response` 写一份「行为审计报告」**

把本讲的四个观察点串成一份小报告，每项给出「源码证据（文件:行号）+ 实验或测试证据」：

1. **多样性审计**：运行 4.3.4 的主实践，记录 5 条 rollout 的 `gen_len` 与前 60 字符，计算「互不相同比例」；再改 `temperature=1.5` 与 `temperature=0.5` 各跑 5 条，比较三组的长度方差（用 `statistics.pvariance`）与互不相同比例，验证「温度越高多样性越大」。
2. **EOS 审计**：检查任一条 rollout 的 `text` 是否以 `<|endoftext|>` 结尾，并在 `token_ids` 末尾找到等于 `tokenizer.eos_token_id` 的分量；用一句话解释它为何在输出里（证据：[ch06.py:L83-L90](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L83-L90) 的先 append 后 break）。
3. **坑点审计**：用两个不同 seed 跑 `temperature=0`，记录两次输出是否相同，并引用 [ch06.py:L74-L75](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L74-L75) 解释「为什么 T=0 不贪心」。
4. **契约审计**：运行 `uv run pytest tests/test_ch06.py::test_sample_response_generates_until_eos tests/test_generation_final_forward.py -v`，把两条测试的断言各抄一句，分别对应本讲的「EOS 入输出」与「末 token 免前向」两个结论。

要求：报告中每个结论都必须能指到具体的源码行或测试断言；无法运行的部分（如无 GPU 时第 1 项的高温组）标注「待本地验证」，不得编造数值。完成这份报告，你就把「读源码 → 做实验 → 找证据」的闭环走了一遍——这正是下一讲拆解 `compute_grpo_loss` 时要反复使用的方法。

---

## 6. 本讲小结

- **rollout = RL 语境下的一次完整生成**：`sample_response`（[ch06.py:L53-L98](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L53-L98)）是第 2 章 KV 缓存生成骨架与第 4 章温度 + top-p 采样的精简合并，默认 `temperature=0.8, top_p=0.9`——随机采样从评测技巧升级为 GRPO 的探索机制。
- **准备阶段自愈**：每次调用新建 `KVCache`（每层一个槽位的纯容器）+ `model.reset_kv_cache()` 归零位置游标，二者成对使用，保证连续多次 rollout 都从位置 0 干净起步。
- **采样流水线**：温度缩放（`T=1` 或 T 为假值时跳过）→ softmax → `top_p_filter`（自 ch04 原样复用，`keep[:,0]=True` 保证核非空）→ `multinomial`（先 `.cpu()` 再抽样）。
- **两个停止条件与两个坑点**：EOS 先 append 再 break，**保留在输出里**（与第 2 章相反）；`temperature=0` **不退化为贪心**（无 argmax 分支，等价 T=1，与 ch04 的温度函数相反）；最后一个 token 的前向被 `step + 1 < max_new_tokens` 跳过，前向次数 = prefill 1 次 + (生成数 − 1) 次，均有回归测试锁定。
- **三件套接口设计**：`(token_ids, prompt_len, text)` 分别喂饱 `sequence_logprob`（重算带梯度的答案区间对数概率）与 `reward_rlvr`（提取 `\boxed{}` 判分）乃至 `gen_len` 统计——一个返回值，三个买主。
- **装饰器的取舍**：用 `@torch.no_grad()` 而非 `@torch.inference_mode()`，因为 GRPO 一步内先采样后反向传播，inference tensor 无法进入 autograd 图。

---

## 7. 下一步学习建议

本讲交付了 GRPO 的「Stage 1 零件」：rollout 及其三件套。下一讲 **u7-l3「GRPO 损失函数」**将把零件装配成损失：精读 [ch06.py:L111-L187](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L111-L187) 中的 `sequence_logprob`（为什么**求和**而非 u6-l1 的求均——序列级对数概率如何正确缩放梯度）与 `compute_grpo_loss` 的五阶段（rollout → 奖励 → 组内标准化优势 → 对数概率 → 策略梯度损失 \(-(\text{advantage} \cdot \log p)\)）。建议预习时带着三个问题读代码：优势为什么要 `detach`？损失为什么取负号？全对/全错组为何天然不更新？之后再进入 u7-l4 的训练循环与批量化变体（`sample_responses_batched`——本讲练习 3 埋下的伏笔）。
