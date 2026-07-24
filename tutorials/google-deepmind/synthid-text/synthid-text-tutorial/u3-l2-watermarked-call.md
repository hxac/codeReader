# watermarked_call：水印施加主流程

## 1. 本讲目标

上一讲（u3-l1）我们只完成了处理器的「开机准备」——构造函数、`SynthIDState`、`hash_iv` 与参数校验。本讲进入真正干活的核心：`watermarked_call`。学完本讲，你应该能够：

- 说出 `watermarked_call` 的 **5 个步骤**的顺序，以及每一步的职责；
- 解释为什么要做「稀疏 top_k」、`top_k_indices`（indices mapping）是什么、调用方如何把它回映成稠密 token；
- 看懂返回的三元组 `(updated_watermarked_scores, top_k_indices, scores_top_k)` 各自的形状与用途；
- 用一段最小代码实例化处理器并验证 `watermarked_call` 的输入输出形状。

---

## 2. 前置知识

本讲默认你已经掌握：

- **g 值**（u2-l3）：ngram + 水印密钥经哈希取出的二进制位，形状 `[batch, seq, depth]`，是施加侧与检测侧的唯一桥梁。
- **`accumulate_hash` 的可累积性**（u2-l2）：\(f(x, \text{data})\) 可增量续哈希，使「上下文只哈希一次、候选 token 续哈希一步」成为可能。
- **`SynthIDState` 与 `hash_iv`**（u3-l1）：处理器内部维护滑动上下文 `context`、去重窗口 `context_history` 与调用计数 `num_calls`；`hash_iv` 是哈希初值。

两个术语快速回顾：

- **ngram**：长度为 `ngram_len` 的 token 窗口（默认 5），由 `ngram_len-1` 个上下文 token（论文里记 H=4）加 1 个候选 token 组成。
- **depth（深度）**：等于 `len(keys)`，默认 30。每个候选 token 在每个深度上各有一颗 g 值，水印信号藏在 30 颗比特的统计聚合里。

---

## 3. 本讲源码地图

本讲几乎全部聚焦于一个文件：

| 文件 | 作用 |
|---|---|
| `src/synthid_text/logits_processing.py` | `watermarked_call` 主流程（L223-L326），以及它调用的 `_compute_keys`、`get_gvals`、模块级函数 `update_scores` / `update_scores_distortionary` |
| `src/synthid_text/synthid_mixin.py` | 调用方：HF 采样循环里如何拿到三元组并用 `torch.take` 回映 token（L294-L298、L335-L339） |
| `src/synthid_text/logits_processing_test.py` | 形状/均匀性/分布收敛测试，是本讲代码实践的依据 |

需要先说明一个关键设计：`SynthIDLogitsProcessor` 继承自 `transformers.LogitsProcessor`，但它**禁用了标准的 `__call__`**——直接调用会抛 `NotImplementedError`。真正干活的是带状态的 `watermarked_call`：

[src/synthid_text/logits_processing.py:213-221](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L213-L221) 标准的 `__call__` 被显式禁用，强制调用方走 `watermarked_call`。

原因很快会看到：水印需要返回额外的 `top_k_indices`，而 HF 标准 `LogitsProcessor` 接口只允许返回「形状不变的 scores」，无法承载这个下标映射。

---

## 4. 核心概念与源码讲解

整段 `watermarked_call` 可以拆成 5 步：

1. **温度 / top_k 处理**：`scores / temperature`，取 top_k，得到稀疏候选集。
2. **滑动上下文**：把上一轮选出的 token 续进 `state.context`，凑出当前 ngram 的前 H 个 token。
3. **计算 ngram keys**：用 `_compute_keys` 把「上下文 + 候选 token + 密钥」哈希成整数。
4. **采样 g 值**：用 `get_gvals` 取哈希的某一位得到 0/1。
5. **更新得分**：用 g 值在 softmax 概率上施加偏置，并做重复上下文去重，最后返回三元组。

> 全段代码：[src/synthid_text/logits_processing.py:223-326](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L223-L326)（带 `@torch.no_grad` 装饰，整个施加过程不回传梯度）。

下面按三个最小模块展开（模块 1 = 步骤 1；模块 2 = 步骤 2-3；模块 3 = 步骤 4-5）。

---

### 4.1 温度与 top_k 处理

#### 4.1.1 概念说明

`watermarked_call` 收到的是模型给出的一整条 logits（`[batch, vocab_size]`）。如果直接对全词表计算 g 值并逐层更新得分，开销会非常大——词表动辄几万，而真正有采样价值的只有概率最高的少数 token。

所以处理器做了两件事：

- **温度缩放（temperature）**：`scores / temperature`。`temperature < 1` 让分布更尖、`> 1` 更平。这里对 logits 做「除以温度」的标准缩放。
- **稀疏 top_k**：只取温度缩放后概率最高的 `top_k` 个候选，后续所有水印计算（g 值、得分更新）都只在这 `top_k` 维上进行，把计算量从 `vocab_size` 压到 `top_k`（通常几十）。

但「只保留 top_k」会带来一个问题：后续采样得到的 token 是「top_k 内部的相对位置」（0 到 top_k-1），不是真实词表 id。所以处理器必须同时返回这 top_k 个候选对应的**真实词表下标** `top_k_indices`，交给调用方做回映。这就是返回三元组里第二个元素的来历，也是 `__call__` 被禁用的根本原因。

#### 4.1.2 核心流程

```
scores [B, V]
   │  scores / temperature
   ▼
scores_processed [B, V]
   │  torch.topk(..., k=top_k, dim=1)
   ▼
scores_top_k   [B, top_k]   ← 候选得分（稀疏）
top_k_indices  [B, top_k]   ← 候选的真实词表下标
```

#### 4.1.3 源码精读

温度缩放与 top_k 选取：
[src/synthid_text/logits_processing.py:244-247](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L244-L247) 先校验形状，再把 `scores` 除以 `temperature`，然后用 `torch.topk` 取最大的 `top_k` 个候选。

`apply_top_k` 分支：
[src/synthid_text/logits_processing.py:249-259](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L249-L259) 默认 `apply_top_k=True`，取稀疏候选；若设为 `False`，则保留全词表，并把 `top_k_indices` 填成 `[0, 1, ..., vocab_size-1]`，相当于「恒等映射」——这种模式用于测试分布收敛性（见 `test_distributional_convergence`，它设 `top_k=vocab_size` 且 `apply_top_k=False`）。

#### 4.1.4 代码实践

**目标**：观察温度与 top_k 如何把全词表压缩成稀疏候选，并验证返回三元组的形状。

**操作步骤**：

1. 复制下面的最小脚本（示例代码），在装好 torch 的环境运行。
2. 调整 `temperature` 与 `top_k`，观察输出形状如何随之变化。

```python
# 示例代码
import torch
from synthid_text import logits_processing

device = torch.device("cpu")
processor = logits_processing.SynthIDLogitsProcessor(
    ngram_len=5,
    keys=[1, 2, 3],            # depth = 3
    context_history_size=64,
    temperature=0.7,
    top_k=10,
    device=device,
)

batch, vocab = 2, 50
input_ids = torch.randint(0, vocab, (batch, 8), device=device)
scores = torch.randn(batch, vocab, device=device)

updated, indices, original = processor.watermarked_call(input_ids, scores)
print(updated.shape, indices.shape, original.shape)
```

**需要观察**：`updated` 与 `indices` 的第二维都应是 `10`（= top_k），而不是 `50`（= vocab）。

**预期结果**：打印 `torch.Size([2, 10]) torch.Size([2, 10]) torch.Size([2, 10])`（与官方测试 `test_watermarked_call_shape` 的断言一致）。

---

### 4.2 上下文滑动与 key 计算

#### 4.2.1 概念说明

水印需要 ngram（H 个上下文 token + 1 候选）。在自回归生成里，每生成一个 token，上下文窗口就向前滑动一格。`watermarked_call` 是**有状态的（stateful）**：它在内部维护 `state.context`，记录「当前 ngram 的前 H 个 token」。

- **第一次调用**：`state` 还不存在，调用 `_init_state` 创建全零的 `context`（`[B, ngram_len-1]`）。此时还没有足够的历史，上下文用 0 占位。
- **之后每次调用**：把「上一次输入的最后那个 token」续到 `context` 末尾，再砍掉最前面一个，完成长度为 `ngram_len-1` 的滑动窗口。

为什么续的是「上一次 `input_ids` 的最后一个」而不是「这一次的」？因为在真实生成循环里，每轮传入的 `input_ids` 都会在末尾追加上一轮刚采样出的 token，所以「本轮 `input_ids` 的最后一个」**正是上一轮采样出来、本轮要作为上下文使用的 token**；而本轮要预测的候选 token 还没生成，自然不在上下文里。

有了上下文，`_compute_keys` 把「上下文 + 每个 top_k 候选 token + 每个深度的密钥」哈希成一个整数 `ngram_keys`。注意它复用了上一讲（u2-l3）讲过的可累积性：**上下文只哈希一次**（`hash_result_with_just_context`），每个候选 token 只需在那之上续哈希一步，再对每个深度续哈希一步。

#### 4.2.2 核心流程

```
state.context [B, H]                (H = ngram_len-1)
   │  首次: _init_state → 全零
   │  之后: 续上轮最后 token → 砍头 → 滑动
   ▼
context [B, H]
   │  _compute_keys(context, top_k_indices)
   │    1) 上下文整体哈希一次 → hash_result_with_just_context [B]
   │    2) 对每个候选 token 续哈希 (vmap) → [B, top_k]
   │    3) 对每个深度续哈希   (vmap) → [B, top_k, depth]
   ▼
ngram_keys [B, top_k, depth]
hash_result_with_just_context [B]   ← 留给步骤 5 去重用
```

#### 4.2.3 源码精读

状态初始化与上下文滑动：
[src/synthid_text/logits_processing.py:267-278](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L267-L278) `state is None` 时初始化全零 context；否则把 `input_ids[:, -1:]`（上一轮最后一个 token）续进 context，再 `[:, 1:]` 砍掉最前一个，保持长度 `ngram_len-1`。

调用 `_compute_keys`（步骤 2-3 的衔接）：
[src/synthid_text/logits_processing.py:288-292](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L288-L292) 传入滑动后的 `context` 与 `top_k_indices`，输出 `ngram_keys` 形状 `[batch_size, top_k, depth]`。

`_compute_keys` 内部，先把上下文整体哈希一次（这正是「可累积性」的用武之地）：
[src/synthid_text/logits_processing.py:427-429](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L427-L429) `hash_result_with_just_context = accumulate_hash(hash_result, n_minus_1_grams)`，每个 batch 得到一个标量哈希。
[src/synthid_text/logits_processing.py:433-435](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L433-L435) 用 `torch.vmap` 沿候选 token 维续哈希一步。
[src/synthid_text/logits_processing.py:443-446](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L443-L446) 再沿深度维续哈希一步，并返回 `(ngram_keys, hash_result_with_just_context)`——后者供步骤 5 去重。

#### 4.2.4 代码实践

**目标**：观察「上下文滑动」让 `context` 逐步从全零变成真实 token。

**操作步骤**：在 4.1.4 脚本基础上，连续调用三次 `watermarked_call`，每次打印 `state.context` 与 `num_calls`。

```python
# 示例代码（接 4.1.4）
torch.manual_seed(0)
for step in range(3):
    ids = torch.randint(0, vocab, (batch, 8), device=device)
    last_in = ids[0, -1].item()           # 本次输入的最后一个 token
    processor.watermarked_call(ids, scores)
    print(f"step {step}: num_calls={processor.state.num_calls}, "
          f"last_in={last_in}, "
          f"context末位={processor.state.context[0, -1].item()}")
```

**需要观察**：第 0 步 `num_calls=1` 且 context 全是 0（首次初始化，不滑动）；从第 1 步起，context 的末位应该等于「上一步输入的最后一个 token」。

**预期结果**：context 长度始终是 4（= `ngram_len-1`）；`step 1` 的 context 末位 = `step 0` 的 `last_in`，依此类推。具体数值取决于随机种子——**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `state.context` 的长度是 `ngram_len - 1` 而不是 `ngram_len`？
**答案**：ngram 由 H 个上下文 token + 1 个候选 token 组成，`ngram_len = H + 1`。`context` 只存上下文部分 \(H = \text{ngram\_len}-1\)；候选 token 由 top_k 候选在下文单独提供，不写进 context。

**练习 2**：如果连续两次调用传入完全相同的上下文，`_compute_keys` 会算出相同的 `ngram_keys` 吗？
**答案**：会。`ngram_keys` 完全由（`hash_iv`, 上下文, 候选 token, `keys`）决定，是确定性的。这正是步骤 5 要做去重的动机。

---

### 4.3 g 值采样与得分更新返回

#### 4.3.1 概念说明

拿到 `ngram_keys` 后，`get_gvals` 把每个整数塌缩成一颗 0/1 的 g 值（细节见 u2-l3，本讲只关注它在主流程里的位置）。然后 `update_scores` 用这批 g 值在 softmax 概率上施加偏置：

- g 值为 1 的候选：概率被**放大**；
- g 值为 0 的候选：概率被**缩小**。

关键性质：这个偏置**逐深度（depth）层层施加**，而且每一层都保持概率质量守恒——总概率和不变，只是从 g=0 的 token 转移到 g=1 的 token。这意味着在大量随机密钥上平均后，水印化的分布会收敛回原始分布（这正是 `test_distributional_convergence` 验证的事）。

最后还有一道「重复上下文去重」：如果当前上下文之前已经水印过（出现重复 n-1 gram），就跳过本次水印，直接返回未修改的得分。这避免重复段落里水印信号被反复叠加、反而暴露水印结构。

#### 4.3.2 核心流程

```
ngram_keys [B, top_k, depth]
   │  get_gvals (取哈希某一位)
   ▼
g_values [B, top_k, depth]   (0/1)
   │  update_scores / update_scores_distortionary
   ▼
updated_scores [B, top_k]
   │  重复上下文? → torch.where 选 updated_scores 或原 scores_top_k
   ▼
updated_watermarked_scores [B, top_k]
   │
   ▼
return (updated_watermarked_scores [B, top_k],
        top_k_indices            [B, top_k],
        scores_top_k             [B, top_k])
```

#### 4.3.3 源码精读

采样 g 值（步骤 4）：
[src/synthid_text/logits_processing.py:294-296](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L294-L296) `g_values = self.get_gvals(ngram_keys)`，形状 `[batch_size, top_k, depth]`。

更新得分（步骤 5 前半，标准锦标赛 `num_leaves=2`）：
[src/synthid_text/logits_processing.py:298-305](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L298-L305) `_num_leaves==2` 走 `update_scores`，否则走 `update_scores_distortionary`（变体详见下一讲 u3-l3）。

模块级函数 `update_scores` 的逐层概率修正：
[src/synthid_text/logits_processing.py:42-47](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L42-L47) 先 softmax 得 probs，再对每个深度执行 `probs = probs * (1 + g_values_at_depth - g_mass_at_depth)`。

这里 `g_mass_at_depth` 是「当前深度上 g=1 的 token 所占的总概率」。对单层，记 \(m\) 为该质量，则修正乘子为：

- g=1 的 token：\(1 + 1 - m = 2 - m\)（放大）；
- g=0 的 token：\(1 + 0 - m = 1 - m\)（缩小）。

可以验证该层概率守恒。设 g=1 的总质量为 \(m\)、g=0 的总质量为 \(1-m\)，修正后总质量：

\[
m(2-m) + (1-m)(1-m) = 2m - m^2 + 1 - 2m + m^2 = 1
\]

所以每一层都把概率从 g=0 转移给 g=1，且总和恒为 1。这正是「水印扭曲小、难以察觉」的数学根源。

distortionary 变体（`num_leaves>2`）：
[src/synthid_text/logits_processing.py:80-87](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L80-L87) 用 \((1-g\_mass)^{\text{num\_leaves}-1}\) 等系数替代简单乘子，对应论文里 `num_leaves>2` 的锦标赛结构（下一讲 u3-l3 详讲）。

重复上下文去重与最终返回（步骤 5 后半）：
[src/synthid_text/logits_processing.py:309-326](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L309-L326) 把 4.2 算出的 `hash_result_with_just_context` 与 `context_history` 比较，若重复则 `is_repeated_context=True`；更新 history 滑窗；最后用 `torch.where` 在「水印后得分」与「原始 `scores_top_k`」之间二选一，返回三元组。

注意：第三个返回值是 `scores_top_k`（**未水印**的稀疏得分），它用于困惑度（perplexity）等「不加水印时」的统计。在调用方 mixin 里，`output_scores` 分支正是用这个 `unwatermarked_scores` 来算 \(-\log\text{softmax}\)：
[src/synthid_text/synthid_mixin.py:294-298](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L294-L298) 采样循环解包三元组。

而 `top_k_indices` 的归宿——回映成稠密 token——发生在采样之后：
[src/synthid_text/synthid_mixin.py:335-339](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L335-L339) 用 `torch.vmap(torch.take)` 把「top_k 内的相对位置」映射回真实词表 id。

#### 4.3.4 代码实践

**目标**：对比 `num_leaves=2`（标准锦标赛）与 `num_leaves=3`（distortionary 变体）对同一批 scores 的不同偏置。

**操作步骤**：

```python
# 示例代码
import torch
from synthid_text import logits_processing

device = torch.device("cpu")
common = dict(ngram_len=5, keys=[1, 2, 3],
              context_history_size=64, temperature=1.0,
              top_k=8, device=device)

p2 = logits_processing.SynthIDLogitsProcessor(num_leaves=2, **common)
p3 = logits_processing.SynthIDLogitsProcessor(num_leaves=3, **common)

torch.manual_seed(0)
input_ids = torch.randint(0, 100, (1, 8), device=device)
scores = torch.randn(1, 100, device=device)

u2, idx2, _ = p2.watermarked_call(input_ids, scores)
u3, idx3, _ = p3.watermarked_call(input_ids, scores)
print("形状:", u2.shape, u3.shape)
print("idx 相同?", torch.equal(idx2, idx3))   # top_k 候选不依赖 num_leaves
print("得分差异:", (u2 - u3).abs().max().item())
```

**需要观察**：两次返回形状相同（都是 `[1, 8]`）；`top_k_indices` 完全相同（因为候选选取与 `num_leaves` 无关）；但 `updated_scores` 不同，说明 `num_leaves` 改变了得分更新公式。

**预期结果**：`idx 相同? True`；`得分差异` 为正数。具体差异值取决于随机种子——**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：返回三元组里第三个元素 `scores_top_k` 为什么不是 `updated_watermarked_scores`？
**答案**：它保留了「未水印」的稀疏得分，供调用方计算困惑度等不希望含水印信号的统计量（见 mixin 的 `output_scores` 分支用 `unwatermarked_scores` 算 \(-\log\text{softmax}\)）。

**练习 2**：如果某次调用的上下文在 `context_history` 里命中了（重复），返回的 `updated_watermarked_scores` 会等于什么？
**答案**：等于 `scores_top_k`（未水印）。因为 `torch.where(is_repeated_context, input=scores_top_k, other=updated_scores)` 在重复时取 `input` 分支，跳过本次水印。

**练习 3**：为什么说 `update_scores` 每一层「概率守恒」对水印很重要？
**答案**：守恒保证水印只在不改变整体分布形状的前提下微调相对概率，使水印难以被人察觉、也不破坏文本质量；同时让「多密钥平均后收敛回原分布」成立（`test_distributional_convergence` 验证）。

---

## 5. 综合实践

**任务**：画一张 `watermarked_call` 的完整流程图，把 5 个步骤串起来，并在每一步标注输入/输出张量形状。

下面给出参考流程图（你可以照着补全箭头与形状）：

```
输入: input_ids [B, L_in],  scores [B, V]
   │
   ├─ 步骤1  温度 / top_k ──────────────────────────┐
   │    scores / temperature → torch.topk            │
   │    输出: scores_top_k   [B, top_k]              │
   │          top_k_indices  [B, top_k]              │
   │                                                 │
   ├─ 步骤2  滑动上下文 ────────────────────────────┤
   │    state.context [B, H]  (H = ngram_len-1)      │
   │    首次: _init_state(全零)                       │
   │    之后: 续 input_ids[:,-1] + 砍头              │
   │                                                 │
   ├─ 步骤3  计算 ngram keys ───────────────────────┤
   │    _compute_keys(context, top_k_indices)         │
   │    输出: ngram_keys                  [B, top_k, depth]
   │          hash_result_with_just_context [B]       │
   │                                                 │
   ├─ 步骤4  采样 g 值 ─────────────────────────────┤
   │    get_gvals(ngram_keys)                         │
   │    输出: g_values [B, top_k, depth]  (0/1)       │
   │                                                 │
   ├─ 步骤5  更新得分 + 去重 + 返回 ─────────────────┤
   │    update_scores(scores_top_k, g_values)         │
   │       → updated_scores [B, top_k]                │
   │    重复上下文? → torch.where 选其一              │
   │    返回三元组:                                   │
   │       updated_watermarked_scores [B, top_k]      │
   │       top_k_indices              [B, top_k]      │
   │       scores_top_k               [B, top_k]      │
   └─────────────────────────────────────────────────┘
```

**进阶**：在流程图旁标注「这一步调用了哪个函数 / 用到了哪个 `state` 字段」。例如步骤 3 对应 `_compute_keys`、用到了 `state.context`；步骤 5 的去重用到了 `state.context_history`。

---

## 6. 本讲小结

- `watermarked_call` 是带状态的水印施加主入口；标准的 `__call__` 被显式禁用（`NotImplementedError`）。
- 5 步顺序：温度/top_k → 滑动上下文 → 计算 ngram keys → 采样 g 值 → 更新得分（含去重）。
- 稀疏 top_k 把计算从全词表压到 `top_k` 维，代价是需要额外返回 `top_k_indices` 供调用方回映成稠密 token（mixin 用 `torch.vmap(torch.take)`）。
- `_compute_keys` 利用哈希可累积性：上下文只哈希一次，候选 token 与深度各自续哈希一步。
- `update_scores` 逐层概率守恒地把质量从 g=0 转给 g=1（可证明每层总和恒为 1），是水印「难以察觉」的数学根源。
- 返回三元组 `(updated_watermarked_scores, top_k_indices, scores_top_k)` 分别用于采样、回映、困惑度统计。

---

## 7. 下一步学习建议

- 下一讲 **u3-l3（得分更新：锦标赛与 distortionary 变体）** 会深入 `update_scores` 与 `update_scores_distortionary` 的公式差异，以及 `num_leaves` 如何影响水印强度与失真。
- 想看 `watermarked_call` 在真实生成循环里的调用方，先读 [src/synthid_text/synthid_mixin.py:294-339](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L294-L339)（解包三元组 + `torch.take` 回映）。
- 想验证自己对形状的理解，可运行 `pytest src/synthid_text/logits_processing_test.py -k test_watermarked_call_shape -v`。
