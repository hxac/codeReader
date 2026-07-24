# 重写 _sample 采样循环

## 1. 本讲目标

上一讲（u4-l1）我们看到了 SynthID 是如何通过 `SynthIDSparseTopKMixin._get_logits_warper` 把一个 `SynthIDLogitsProcessor` 塞进 HuggingFace 的 warper 列表的。但那只是「挂载点」——真正让水印在生成过程中逐 token 生效的，是本讲要拆解的 `_sample` 采样循环。

学完本讲，你应该能够：

- 说清楚为什么 SynthID 要**复制并改写** HuggingFace 的 `_sample`，而不是简单复用。
- 理解 `watermarked_call` 返回的「稀疏三元组」中每一项的作用，以及 `next_token_scores` 与 `indices_mapping` 是如何协同工作的。
- 看懂 `torch.vmap(torch.take)` 这一行为什么必须与 `watermarked_call` 成对出现。
- 理解在 `return_dict_in_generate=True` 时，`scores` 是用「未水印得分」算出来的，而不是水印后的得分。

---

## 2. 前置知识

在进入源码前，先用最朴素的方式理解几个关键概念。

### 2.1 什么是采样循环（sampling loop）

大语言模型「生成文本」本质上是**一个 token 一个 token 地预测**。每一步：

1. 把当前已有序列喂给模型，得到下一个位置上、**整个词表**每个候选词的「原始分数」（logits）。
2. 根据采样策略（贪心、温度采样、top_k 采样等）从这些分数里挑出下一个 token。
3. 把挑出的 token 接到序列末尾，重复。

HuggingFace 把这个过程封装在 `GenerationMixin._sample` 里。SynthID 要做的就是**在第 2 步挑选之前，悄悄给分数加上水印偏置**。

### 2.2 稠密分数 vs 稀疏分数

- **稠密（dense）**：分数形状是 `[batch, vocab_size]`，即对词表里每一个词都有一个分数。HuggingFace 原版的 logits warper 全部假设输入输出都是稠密的。
- **稀疏（sparse）**：SynthID 为了降延迟，只对 **top_k 个最有可能的候选词**施加水印，因此水印后的分数形状是 `[batch, top_k]`。

问题来了：HuggingFace 的 `_sample` 期待 warper 输出仍是稠密 `[batch, vocab_size]`，而 SynthID 的 `watermarked_call` 返回的是稀疏 `[batch, top_k]`，**还额外多吐了一个「这 top_k 个候选的真实词表下标」的张量**。这两件事 HF 原版循环都处理不了，所以必须重写。这也是类名 `SynthIDSparseTopKMixin` 里 **Sparse（稀疏）** 一词落到代码层面的根因。

### 2.3 「局部下标」与「词表下标」的区分（本讲最关键直觉）

这是理解本讲的一把钥匙，请反复体会：

- 假设 `top_k=40`，那么水印后的分数只覆盖了 40 个候选词。
- 这 40 个候选词在**稀疏分数张量里的位置**是 `0, 1, 2, …, 39`，我们叫它**局部下标**（local index）。
- 但这 40 个词在**完整词表里的真实 id** 可能是 `12, 88, 503, …`，我们叫它**词表下标**（dense / vocab index）。
- 当我们从 40 个候选里「采样」出第 5 个时，得到的是**局部下标 `5`**，它**并不等于**词表 id `5`！

所以采样之后，必须有一次「翻译」：把局部下标 `5` 映射回它对应的真实词表 id。这个翻译就是 `indices_mapping` 加上 `torch.vmap(torch.take)` 干的事。记住这个区分，后面所有源码都会变得顺理成章。

---

## 3. 本讲源码地图

本讲几乎全部聚焦于一个文件：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/synthid_text/synthid_mixin.py` | 提供 `SynthIDSparseTopKMixin` 及两个空子类 | 其中 `_sample` 方法（第 129–393 行）是本讲主角 |

为了讲清契约，我们还会引用它的「被调用方」：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/synthid_text/logits_processing.py` | 实现 `SynthIDLogitsProcessor` | `watermarked_call` 的返回值形状契约（第 229–243、326 行） |

简而言之：`synthid_mixin._sample` 是**消费方**，`logits_processing.watermarked_call` 是**生产方**，二者通过一个「稀疏三元组」约定协作。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**采样主循环结构**、**稀疏 watermarking 调用**、**indices_mapping 回映**。

### 4.1 采样主循环结构

#### 4.1.1 概念说明

`_sample` 是 HuggingFace `GenerationMixin` 里负责「多项式采样（multinomial sampling）」生成 token 序列的核心循环。SynthID **原样复制了它，并做了最小改动**，目的是在采样前插入水印。

为什么是「复制改写」而不是「继承覆盖某个小钩子」？因为水印要维护**跨 token 的状态**（上一讲讲的 `SynthIDState`：上下文、上下文历史、调用计数），并且它的输出是**稀疏**的——这两个特性都超出了 HF 原版循环的设计。HF 没有提供「我的 warper 返回了额外的下标映射，请帮我处理」这种接口，所以只能整体接管循环。

源码里的 docstring 也坦白说明了这一点：

[src/synthid_text/synthid_mixin.py:149-157](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L149-L157) — 注释说明：本函数从 HuggingFace 仓库复制并做了最小改动；重写基类实现是为了「单独保留 top_k 的下标、不让 logits 重新变稠密」，从而避免把整个词表都卷入水印计算。

#### 4.1.2 核心流程

每次迭代（生成一个 token）的流程可以画成下面这样：

```text
┌──────────────────────────── while 还有未完成序列 ────────────────────────────┐
│  1. prepare_inputs_for_generation   准备模型输入                              │
│  2. self(**model_inputs)            前向传播，得到 outputs.logits            │
│  3. next_token_logits = outputs.logits[:, -1, :]   取最后一个位置的稠密 logits│
│  4. next_token_scores = logits_processor(input_ids, next_token_logits) 预处理 │
│  5. if do_sample:                                                            │
│         拆出 watermarking warper → watermarked_call → 得到稀疏三元组          │
│  6. token selection: softmax + multinomial（或 argmax）→ next_tokens          │
│  7. 记录 scores（用「未水印得分」算困惑度相关分数）                            │
│  8. torch.vmap(torch.take)(indices_mapping, next_tokens)  局部下标→词表下标   │
│  9. 已完成序列填 pad；input_ids 拼上新 token；更新 model_kwargs               │
└──────────────────────────────── while 还有未完成序列 ──────────────────────────┘
```

注意第 6 步的采样是基于第 5 步得到的**稀疏** `next_token_scores`（形状 `[batch, top_k]`），所以采样出来的 `next_tokens` 一开始是**局部下标**，必须经过第 8 步回映。

#### 4.1.3 源码精读

**循环主体**是一个 `while`，条件由 HF 提供的 `_has_unfinished_sequences` 判断：

[src/synthid_text/synthid_mixin.py:254-256](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L254-L256) — 只要还有序列没生成完（没遇到停止条件），就继续循环。

每轮先准备输入并做**前向传播**，拿到模型输出：

[src/synthid_text/synthid_mixin.py:263-268](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L263-L268) — 调用模型自身（`self(...)`）得到 `outputs`。

接着取出**最后一个位置**的 logits，这一步是稠密的 `[batch, vocab_size]`：

[src/synthid_text/synthid_mixin.py:275](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L275) — `outputs.logits[:, -1, :].clone()`；`.clone()` 是为了避免持有首轮可能非常大的 logits 张量的悬空引用（注释在第 273–274 行）。

然后用 `logits_processor` 做一遍预处理（这一步与水印无关，是 HF 标准流程，例如重复惩罚等）：

[src/synthid_text/synthid_mixin.py:278](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L278) — `next_token_scores = logits_processor(input_ids, next_token_logits)`。

紧接着把两个「稀疏专用品」先初始化为 `None`，后面在 `do_sample` 分支里才会被赋值：

[src/synthid_text/synthid_mixin.py:279-280](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L279-L280) — `indices_mapping = None`、`unwatermarked_scores = None`。

**token 选择**这一段很关键：它对稀疏分数做 softmax 再采样，得到**局部下标**：

[src/synthid_text/synthid_mixin.py:301-305](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L301-L305) — `do_sample` 时 `softmax(next_token_scores)` 再 `multinomial` 采样 1 个；否则 `argmax`。注意此时 `next_token_scores` 是稀疏的 `[batch, top_k]`，所以 `multinomial` 的结果落在 `[0, top_k)` 区间，是局部下标。

循环结尾的「收尾三件套」：已完成序列填 pad、把新 token 拼到 `input_ids`、更新缓存相关的 `model_kwargs`：

[src/synthid_text/synthid_mixin.py:342-345](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L342-L345) — 已结束的序列，下一个 token 被强制改成 `pad_token_id`。

[src/synthid_text/synthid_mixin.py:348](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L348) — `input_ids = torch.cat([input_ids, next_tokens[:, None]], dim=-1)`，把刚采到的真实词表 id 接到序列末尾，进入下一轮。

#### 4.1.4 代码实践

**实践目标**：在脑中（或纸上）完整跑一遍 `_sample` 的「单次迭代」，确认每一步张量形状的变化。

**操作步骤**：

1. 假设 `batch_size=2`、`vocab_size=1000`、`top_k=40`、`do_sample=True`。
2. 从第 4.1.2 节的流程图里第 1 步走到第 9 步，逐行写出关键变量形状。
3. 重点关注：第 3 步 `next_token_logits` 的形状、第 5 步后 `next_token_scores` 的形状、第 6 步 `next_tokens` 的形状、第 8 步后 `next_tokens` 的形状。

**需要观察的现象**：

- 第 3 步：`next_token_logits` 形状 `[2, 1000]`（稠密）。
- 第 5 步后：`next_token_scores` 形状 `[2, 40]`（变稀疏了！）。
- 第 6 步后：`next_tokens` 形状 `[2]`，值域 `[0, 40)`（局部下标）。
- 第 8 步后：`next_tokens` 形状仍是 `[2]`，但值域变成 `[0, 1000)`（真实词表 id）。

**预期结果**：你能清楚说出「形状从稠密变稀疏发生在第 5 步，值域从局部变全局发生在第 8 步」。如果说不清，回到 2.3 节再看一遍。

> 说明：本实践为源码阅读型实践，无需运行；如需实际观察，可参考第 5 节综合实践。

#### 4.1.5 小练习与答案

**练习 1**：为什么 SynthID 选择「复制并改写」`_sample`，而不是写一个新的 logits warper 并指望 HF 原版循环正确处理它？

> **参考答案**：因为 HF 原版 `_sample` 假设所有 logits warper 都是「稠密进、稠密出」，而 SynthID 的 `watermarked_call` 返回的是稀疏 `[batch, top_k]` 分数，外加一个 `indices_mapping`。HF 循环既不认识这个额外的返回值，也无法处理稀疏分数后的采样回映，所以必须整体接管循环。

**练习 2**：第 6 步的 `multinomial` 采样结果，为什么不能直接当作下一个 token 的词表 id 拼进 `input_ids`？

> **参考答案**：因为采样是在稀疏的 `[batch, top_k]` 分配上进行的，结果落点是 `[0, top_k)` 的局部下标，而不是 `[0, vocab_size)` 的词表 id。必须先经过第 8 步的 `indices_mapping` 回映，才能得到真实词表 id。

---

### 4.2 稀疏 watermarking 调用

#### 4.2.1 概念说明

`do_sample` 分支里真正的「水印时刻」就是对 `watermarked_call` 的调用。上一讲我们已经知道 `_construct_warper_list` 只往列表里塞了**一个** warper（就是 `SynthIDLogitsProcessor`），所以这里拆包后 `regular_warpers` 是空的，`watermarking_logits_warper` 就是那个唯一的 SynthID 处理器。

`watermarked_call` 的返回值是一个**三元组**，这是本模块要死磕的契约：

1. **水印后的稀疏分数** `updated_watermarked_scores`，形状 `[batch, top_k]`，用于采样。
2. **下标映射** `top_k_indices`（在 `_sample` 里被命名为 `indices_mapping`），形状 `[batch, top_k]`，记录这 top_k 个候选**在完整词表里的真实 id**。
3. **未水印的稀疏分数** `scores_top_k`（命名为 `unwatermarked_scores`），形状 `[batch, top_k]`，保留原始（温度缩放 + top_k 后、但没加水印）的分数，用于事后算困惑度/分数。

这个契约在 `logits_processing.py` 的 docstring 里写得很清楚：

[src/synthid_text/logits_processing.py:238-243](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L238-L243) — Returns 说明三元组分别是：水印后分数 `[batch, top_k]`、top_k 的真实下标 `[batch, top_k]`、用于困惑度的原始分数 `[batch, top_k]`。

[src/synthid_text/logits_processing.py:326](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L326) — 实际的 `return updated_watermarked_scores, top_k_indices, scores_top_k`。

#### 4.2.2 核心流程

```text
logits_warper（一个长度为 1 的列表）
    │
    ├─ *regular_warpers,  watermarking_logits_warper = logits_warper
    │   （regular_warpers 为空，watermarking_logits_warper 即 SynthID processor）
    │
    ├─ 校验：watermarking_logits_warper 必须是 SynthIDLogitsProcessor 且在列表末尾
    │
    ├─ for logit_warper in regular_warpers: （本轮为空，跳过）
    │       next_token_scores = logit_warper(...)
    │
    └─ next_token_scores, indices_mapping, unwatermarked_scores =
           watermarking_logits_warper.watermarked_call(input_ids, next_token_scores)
       （返回稀疏三元组）
```

注意：`watermarked_call` 内部自己做了温度缩放和 top_k 截断（上一讲 u3-l2 讲的 5 步流程），所以传进去的 `next_token_scores` 虽然是稠密的 `[batch, vocab]`，出来的却是稀疏的 `[batch, top_k]`。这正是「稠密进、稀疏出」的接口特征。

#### 4.2.3 源码精读

**拆包与校验**：用 Python 的星号解包把列表最后一项单独取出，并强校验它的类型，确保 SynthID 处理器是 warper 列表的最后一个：

[src/synthid_text/synthid_mixin.py:282-290](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L282-L290) — `*regular_warpers, watermarking_logits_warper = logits_warper`，若最后一项不是 `SynthIDLogitsProcessor` 则抛 `ValueError`。这个校验是一道保险：即便将来有人在 warper 列表里插入了别的处理器，也会被立刻拦下，防止水印被「后面某个 warper」覆盖或破坏稀疏契约。

**真正的水印调用**：把当前（仍是稠密的）`next_token_scores` 交给 `watermarked_call`，接住稀疏三元组：

[src/synthid_text/synthid_mixin.py:291-298](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L291-L298) — 先跑（空的）`regular_warpers`，再调用 `watermarked_call`，注释明确写着「Watermark final scores with sparse top_k」（用稀疏 top_k 给最终分数加水印）。

这一行同时完成了三件事，理解它的最好方式是记住三个返回值分别交给谁用：

| 返回值 | 在 `_sample` 里的名字 | 形状 | 用途 |
| --- | --- | --- | --- |
| 水印后稀疏分数 | `next_token_scores` | `[batch, top_k]` | 第 6 步 softmax + multinomial 采样 |
| 真实下标映射 | `indices_mapping` | `[batch, top_k]` | 第 8 步把局部下标翻译回词表 id |
| 未水印稀疏分数 | `unwatermarked_scores` | `[batch, top_k]` | 第 7 步算「干净」的分数/困惑度 |

#### 4.2.4 代码实践

**实践目标**：理解「未水印分数」为何要单独返回，并验证 `_sample` 在记录 `scores` 时确实用的是它。

**操作步骤**：

1. 阅读 [src/synthid_text/synthid_mixin.py:308-316](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L308-L316) 这段记录 `scores` 的代码。
2. 注意它做的是 `torch.gather(-torch.log(Softmax(unwatermarked_scores)), 1, next_tokens[:, None])`。
3. 思考：如果这里改用水印后的 `next_token_scores` 会带来什么问题？

**需要观察的现象**：

- 计算 score 用的输入是 `unwatermarked_scores`（第三返回值），不是 `next_token_scores`（水印后）。
- 用作 gather 下标的 `next_tokens` 此时还是**局部下标**，而 `unwatermarked_scores` 的形状正是 `[batch, top_k]`，二者空间一致，能正确对齐。

**预期结果**：你能解释「报告给用户的分数应当反映模型本身的偏好（用于算困惑度/判断质量），而不是被水印扭曲后的偏好，所以必须用未水印分数」。这是一个很务实的设计：水印影响的是「采哪个 token」，而「这个 token 有多符合模型分布」的度量必须保持纯净。

> 说明：本实践为源码阅读型实践。

#### 4.2.5 小练习与答案

**练习 1**：`*regular_warpers, watermarking_logits_warper = logits_warper` 这行解包后，`regular_warpers` 的长度通常是几？为什么？

> **参考答案**：通常是 0。因为上一讲 u4-l1 讲过，`_construct_warper_list` 只往列表里 append 了唯一一个 `SynthIDLogitsProcessor`，列表长度恒为 1。所以解包后 `regular_warpers` 为空，`watermarking_logits_warper` 就是那个 SynthID 处理器。

**练习 2**：为什么 SynthID 处理器必须放在 warper 列表的**最后一个**？如果把它放在前面会怎样？

> **参考答案**：水印是在「最终的、已经 top_k 截断过的稀疏分数」上施加的，它输出的稀疏张量无法再被任何「稠密进稠密出」的普通 warper 处理。如果它不在最后，后续 warper 会拿到稀疏张量却以为是稠密词表分数，导致形状/语义错乱。第 287–290 行的校验就是强制保证这一点。

---

### 4.3 indices_mapping 回映

#### 4.3.1 概念说明

本模块解决本讲最核心的问题：**采样得到的局部下标，如何翻译回真实词表 id？**

答案就是这两行（已在 4.1.3 节见过）：

[src/synthid_text/synthid_mixin.py:335-339](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L335-L339) — 先 `assert indices_mapping is not None`，再用 `torch.vmap(torch.take, in_dims=0, out_dims=0)(indices_mapping, next_tokens)` 完成回映。

来拆解 `torch.vmap(torch.take)(indices_mapping, next_tokens)`：

- `torch.take(src, index)` 的语义是「从 `src` 里取出第 `index` 个元素」。
- 这里 `src = indices_mapping`（形状 `[batch, top_k]`），`index = next_tokens`（形状 `[batch]`，是局部下标）。
- 我们想要的是：对每个 batch `b`，取出 `indices_mapping[b, next_tokens[b]]`，也就是「第 b 条样本采到的那个局部下标，对应的真实词表 id」。
- `torch.vmap(..., in_dims=0, out_dims=0)` 的作用是把上面这个「逐 batch」的操作**沿第 0 维（batch 维）向量化并行**：`in_dims=0` 表示两个输入张量都沿第 0 维切分，`out_dims=0` 表示输出也拼回第 0 维。

用伪代码表达就是：

```text
for b in range(batch):
    next_tokens[b] = indices_mapping[b][ next_tokens[b] ]   # 局部下标 → 词表 id
# torch.vmap 把这个循环编译成一次批量并行
```

#### 4.3.2 核心流程

把「水印调用」和「回映」串起来看，二者是**严格配对**的：

```text
稀疏水印调用（生产稀疏张量）
    next_token_scores[batch, top_k],  indices_mapping[batch, top_k], unwatermarked ...
                                        │
                                        ▼
softmax + multinomial（在 top_k 维采样）
    next_tokens[batch]   ← 值域 [0, top_k)，是局部下标
                                        │
                                        ▼
torch.vmap(torch.take)(indices_mapping, next_tokens)
    next_tokens[batch]   ← 值域 [0, vocab)，是真实词表 id
                                        │
                                        ▼
拼进 input_ids，进入下一轮
```

为什么必须配对？因为 `multinomial` 的输出是**指向稀疏分数那个维度**的下标，而不是指向词表的下标。如果没有回映这一步，直接把局部下标 `5` 当词表 id 用，你几乎一定会取到一个**完全错误的 token**（词表里 id=5 的词，和「top_k 候选里的第 5 个」八竿子打不着）。回映就是用 `indices_mapping` 这张「对照表」把局部下标翻译回它真正指向的词表 id。

#### 4.3.3 源码精读

**强制断言**：在回映之前，先断言 `indices_mapping` 一定不是 `None`：

[src/synthid_text/synthid_mixin.py:335](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L335) — `assert indices_mapping is not None`。回想 4.1.3 节，`indices_mapping` 只在 `if do_sample:` 分支里被赋值，否则一直是 `None`。这个断言放在 `if do_sample` **外面**，等价于「强制要求 `do_sample=True`」——因为水印本质上是采样方法，贪心解码（`do_sample=False`、`top_k=1`）会让水印彻底失效，所以这里宁可断言失败也不允许无水印的贪心路径悄悄通过。这与上一讲 processor 层「`top_k>1`」的校验一脉相承。

**回映本体**：

[src/synthid_text/synthid_mixin.py:337-339](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L337-L339) — `torch.vmap(torch.take, in_dims=0, out_dims=0)(indices_mapping, next_tokens)`，把每个 batch 的局部下标翻译成真实词表 id。注释「re-mapping to dense indices with indices_mapping」（用 indices_mapping 回映到稠密下标）点明了它的作用。

一个值得注意的细节：回映发生在**记录 scores 之后**（第 308–316 行先用了局部下标去 gather `unwatermarked_scores`，第 337 行才把 `next_tokens` 改成词表 id）。这个顺序不是随便排的——`unwatermarked_scores` 本身就是 `[batch, top_k]`，用局部下标 gather 它正好正确；如果先回映再用词表 id 去 gather `[batch, top_k]`，就会越界或取错。所以「先在稀疏空间里算分数，再回映到稠密空间」是刻意的。

#### 4.3.4 代码实践（本讲主任务）

**实践目标**：在 `_sample` 中精确定位「调用 `watermarked_call`」与「执行 `torch.vmap(torch.take)` 回映」这两处代码，并解释它们为何必须成对出现。

**操作步骤**：

1. 打开 `src/synthid_text/synthid_mixin.py`。
2. 找到调用 `watermarked_call` 的那一处（提示：在第 294–298 行附近，形如 `watermarking_logits_warper.watermarked_call(...)`）。记下它返回的三个变量名。
3. 找到执行回映的那一处（提示：在第 337–339 行附近，形如 `torch.vmap(torch.take, ...)(indices_mapping, next_tokens)`）。
4. 在两处之间，找到把 `watermarked_call` 返回的「下标映射」变量传递到回映处的数据通路（即第 294 行的 `indices_mapping` 一路被读到第 337 行）。
5. 用一句话写下：如果删掉回映这一行（或删掉 `watermarked_call`），分别会发生什么。

**需要观察的现象**：

- `watermarked_call` 的返回值里，第二个变量 `indices_mapping` 没有立刻被用于采样，而是「带着」走到第 337 行才被消费。
- 采样（`multinomial`）发生在两者之间，且它的输入是稀疏分数、输出是局部下标。

**预期结果**：你能清楚陈述这对配对关系——

- `watermarked_call` 负责「在 top_k 个候选里加水印，并告诉我们这 top_k 个候选的真实词表 id」；
- `torch.vmap(torch.take)` 负责「把采样挑中的局部下标，按这张对照表翻译回真实词表 id」；
- 二者缺一不可：缺前者就没有水印、也没有对照表；缺后者，采样结果就是无意义的局部下标，会取到错误的 token。

> 说明：本实践为源码阅读型实践，定位代码即可，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：假设 `top_k=40`，某条样本经过采样得到 `next_tokens[b] = 7`，而 `indices_mapping[b] = [503, 12, 88, …, 977, 4]`（长度 40）。回映后 `next_tokens[b]` 应该是多少？如果没有回映这一步，拼进 `input_ids` 的又会是多少？

> **参考答案**：回映后 `next_tokens[b] = indices_mapping[b][7]`，即对照表里第 7 个真实词表 id（具体取决于表内容，比如可能是 `301`）。如果没有回映，拼进去的就是 `7`——一个几乎肯定与「第 7 个 top_k 候选」无关的错误词表 id。

**练习 2**：为什么 `torch.vmap(torch.take, in_dims=0, out_dims=0)` 要沿第 0 维（batch 维）向量化，而不是直接对整个二维张量用 `torch.take`？

> **参考答案**：因为 `torch.take` 默认会把输入**展平成一维**再按下标取，那会把 `[batch, top_k]` 拍平成 `batch*top_k` 长度的一维数组，下标语义完全错乱。我们需要的是「逐 batch、在各自那 40 个候选里取」，即对每个 batch 元素独立地 `take(indices_mapping[b], next_tokens[b])`，这正是 `vmap(..., in_dims=0)` 的用途——沿 batch 维并行地执行这个逐元素操作。

---

## 5. 综合实践

**任务**：把本讲三个最小模块串起来，画一张「单次迭代张量形状流转图」，并回答一个「如果…会怎样」的问题。

**步骤**：

1. 设定参数：`batch_size=2`、`vocab_size=1000`、`top_k=40`、`do_sample=True`、`return_dict_in_generate=True`、`output_scores=True`。
2. 在一张图上，按 `_sample` 单次迭代的顺序，标出下列变量在每一步的**形状**和**值域**：
   - `next_token_logits`（第 275 行）
   - `next_token_scores`（预处理后，第 278 行）
   - `next_token_scores`（`watermarked_call` 后，第 294 行）—— 注意形状发生了变化
   - `indices_mapping`、`unwatermarked_scores`（第 294 行）
   - `next_tokens`（采样后，第 303 行）
   - `next_tokens`（回映后，第 337 行）
3. 在图上用箭头标出 `indices_mapping` 是如何从第 294 行「携带」到第 337 行被消费的。
4. 回答：如果有人把第 337–339 行的回映**注释掉**，但保留其余代码，生成出来的文本会是什么样？`scores` 字段又会变成什么样？

**预期结果**：

- 形状流转图能清楚体现「稠密 `[2,1000]` → 稀疏 `[2,40]` → 局部下标 `[2]` → 真实 id `[2]`」这条主线。
- 关于问题 4：若删掉回映，`next_tokens` 会停留在局部下标 `[0,40)`，拼进 `input_ids` 的就是错误的词表 id，生成的文本将是乱码（语义完全错乱）；但 `scores` 字段**不受影响**，因为第 308–316 行在回映**之前**就已经用 `unwatermarked_scores` 把分数算好了——这恰好印证了 4.2.4 节「分数空间与采样空间分离」的设计。

> 待本地验证：若你想实际观察乱码现象，可在本地用 `SynthIDGPT2LMHeadModel` 跑一次 `generate`，临时注释回映行后对比输出（注意这会破坏水印，仅用于理解）。

---

## 6. 本讲小结

- SynthID **复制并最小改写**了 HuggingFace 的 `_sample` 采样循环，因为水印的输出是稀疏 `[batch, top_k]` 且附带额外下标映射，HF 原版「稠密进稠密出」的循环无法承载。
- `do_sample` 分支把 warper 列表拆成 `regular_warpers`（为空）和唯一的 `watermarked_call`，后者返回**稀疏三元组**：水印后分数、真实下标映射 `indices_mapping`、未水印分数。
- 采样（`multinomial`）是在稀疏分数上进行的，得到的 `next_tokens` 是**局部下标** `[0, top_k)`，不是词表 id。
- `torch.vmap(torch.take, in_dims=0, out_dims=0)(indices_mapping, next_tokens)` 负责把局部下标**回映**为真实词表 id，它必须与 `watermarked_call` **成对出现**——前者提供对照表，后者提供待翻译的下标。
- `assert indices_mapping is not None` 放在 `if do_sample` 之外，实质上**强制要求采样模式**，杜绝让水印失效的贪心路径。
- 记录 `scores` 时用的是**未水印分数** `unwatermarked_scores`，且发生在回映之前（此时 `next_tokens` 还是局部下标，与 `[batch, top_k]` 的分数空间正好对齐），保证报告的分数反映模型本身分布而非水印扭曲。

---

## 7. 下一步学习建议

本讲把「水印如何挂进生成循环」讲完了。接下来建议：

1. **u4-l3 子类化 Gemma 与 GPT-2**：看看 `SynthIDGPT2LMHeadModel` 与 `SynthIDGemmaForCausalLM` 这两个空类，如何通过多重继承把本讲的 Mixin（连同 `_sample`）挂到真实模型上，做到 `from_pretrained(...).generate(...)` 即可生成水印文本。
2. **回顾 u3-l2 `watermarked_call` 五步流程**：本讲只把它当作「返回稀疏三元组」的黑盒，建议回到 u3-l2 把它内部的温度缩放、滑动上下文、g 值采样、得分更新、上下文去重再串一遍，理解三元组里每个值是怎么算出来的。
3. **动手跑 Notebook**：在理解源码后，运行 `notebooks/synthid_text_huggingface_integration.ipynb`，把本讲的形状流转图与实际 `generate` 调用对应起来，建立从源码到运行的完整闭环。
