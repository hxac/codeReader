# g 值是什么：从 ngram 到二进制位

## 1. 本讲目标

本讲是单元二的收尾，专门讲清楚贯穿 SynthID Text 全项目的一个核心数据结构——**g 值（g value）**。读完本讲你应该能够：

- 说出「一段 token 序列 + 水印密钥」是如何一步步被压成一组二进制位的；
- 看懂 `compute_ngram_keys` / `_compute_keys` 里用 `torch.vmap` 对 ngram 维、depth 维做哈希的写法，并能推算每一步张量的形状；
- 解释 `get_gvals` 里「多次重新哈希 + 右移 + 取位」为什么能产生近似均匀随机的 0/1 比特；
- 推导 `compute_g_values` 的输出形状，并解释为什么输出序列长度会比输入少 `ngram_len - 1`。

g 值是连接「水印施加侧（PyTorch）」与「水印检测侧（JAX）」的唯一桥梁（见 [[u1-l4]] 端到端流程）。本讲只聚焦「**如何从一个 token 序列算出 g 值**」，不涉及这些 g 值在生成时如何偏置概率（那是 [[u3-l3]] 的事），也不涉及检测时如何打分（那是 [[u5-l2]] 的事）。

## 2. 前置知识

本讲默认你已经读过以下两讲，我们只做最简回顾、不重复展开：

- **[[u2-l1]] 水印配置**：`keys` 是一串整数列表，`len(keys)` 决定水印**深度 depth**（默认配置有 30 个 key，故 depth=30）；`ngram_len=5` 对应论文 `H=4`，因为一个 ngram = `H` 个上下文 token + 1 个候选 token，即 `ngram_len = H + 1`。`keys` 还会被 SHA-256 摘要成一个不可预测的哈希初值 `hash_iv`。
- **[[u2-l2]] 哈希函数**：`hashing_function.accumulate_hash(current_hash, data)` 是一个改编自 LCG 的累加哈希。它有两个我们本讲要用到的性质：
  1. **可累积性**：`f(x, data[:T]) = f(f(x, data[:T-1]), data[T])`，因此可以「先哈希上下文、再续哈希候选 token」分步进行；
  2. **逐元素张量运算**：它内部是 `torch.add` / `torch.mul`，因此天然支持广播，可以借助 `torch.vmap` 并行处理一批 ngram。

一个对初学者可能陌生的新概念是 **`torch.vmap`**：它是 PyTorch 的「向量化映射」，相当于把一个「处理单个样本」的函数，自动改写成「一次处理一批样本」。`vmap(fn, in_dims=(None, k))` 的含义是：第一个参数不映射，第二个参数沿它的第 `k` 维做映射（即把第 `k` 维当作「批」逐片调用 `fn`）。本讲里多处用它来对「多个 ngram」或「多个 key」并行哈希。

## 3. 本讲源码地图

本讲只涉及两个文件，但它们是全项目的「g 值生产线」：

| 文件 | 本讲用到的部分 | 作用 |
| --- | --- | --- |
| `src/synthid_text/logits_processing.py` | `compute_ngram_keys`、`_compute_keys`、`get_gvals`、`compute_g_values` | 把 token 序列 + 密钥转成二进制 g 值的四个核心函数 |
| `src/synthid_text/hashing_function.py` | `accumulate_hash` | 上面四个函数都依赖的底层 LCG 哈希（[[u2-l2]] 已讲过） |

辅助文件（仅用来印证形状与统计性质，不必逐行精读）：

| 文件 | 用途 |
| --- | --- |
| `src/synthid_text/logits_processing_test.py` | 用测试断言验证 g 值形状与「近似 0.5」的无偏性 |
| `src/synthid_text/g_value_expectations.py` | 论文给出的 g 值理论期望，可作为「实现是否正确」的参照 |

本讲的四函数调用关系如下（生成侧 vs 检测侧两条路，最终都汇入 `get_gvals`）：

```
检测侧（重算一段已有序列的 g 值）
  compute_g_values
     └─ unfold 切出所有 ngram
     └─ compute_ngram_keys   ──┐
                                ├─► get_gvals ──► 二进制 g 值 [batch, seq, depth]
生成侧（一个上下文 + top_k 候选） │
  watermarked_call               │
     └─ _compute_keys    ────────┘
```

## 4. 核心概念与源码讲解

### 4.1 compute_ngram_keys 与 _compute_keys：把 ngram 搅拌成「每一层一个整数」

#### 4.1.1 概念说明

无论施加水印还是检测水印，SynthID 都需要对「**一个 ngram + 一把水印密钥**」算出一个 64 位整数，我们叫它 **ngram key**。因为 `keys` 列表里有 `depth` 把密钥，所以每个 ngram 最终会得到 `depth` 个这样的整数——对应 g 值张量的「深度维」。

项目里有两个长得几乎一样的函数，区别只在于「喂进去的 token 形状不同」：

- **`compute_ngram_keys`**（检测侧用）：一次性拿到**一大批完整的 ngram**，形状 `[batch, num_ngrams, ngram_len]`，要给每个 ngram、每层 key 都算一个整数。
- **`_compute_keys`**（生成侧用）：只有**一个上下文**（`ngram_len - 1` 个 token）和 **top_k 个候选 token 的下标**，要给「这一个上下文 × top_k 个候选 × depth 层」算整数。它还会顺带返回「只哈希上下文」的结果，供上下文去重使用（见 [[u3-l4]]）。

两者的核心算式其实是同一条「哈希链」：

\[ \text{ngram\_key}_{b, j, d} = f\big(\, f(\, f(\text{hash\_iv},\ \text{ngram}_{b,j}),\ \text{continuation}_{b,j}),\ \text{keys}_d\,)\big) \]

其中 \(f\) 就是 `accumulate_hash`，下标 \(b\) 是 batch、\(j\) 是第几个 ngram/候选、\(d\) 是第几层 key。注意最后**单独把某一层 key 续哈希一步**——这正是「每层 key 得到一个独立整数」的关键。

#### 4.1.2 核心流程

先看 `compute_ngram_keys` 的三步（这是检测侧的写法，把整条链一次走完）：

1. **初始化**：每个 batch 项的哈希状态都置为不可预测的 `hash_iv`，形状 `[batch]`。
2. **哈希整个 ngram**：用 `vmap` 沿「ngram 数」维并行，对每个 ngram 调一次 `accumulate_hash`（内部沿 `ngram_len` 维累加），结果形状 `[batch, num_ngrams]`。
3. **续哈希每层 key**：把 `keys` 摆成 `[1, 1, depth, 1]`，用 `vmap` 沿「depth」维并行，每层 key 各续哈希一步，结果形状 `[batch, num_ngrams, depth]`。

再看 `_compute_keys`（生成侧），它把第 2 步拆成「先上下文、再候选」，因为生成时上下文是共享的、只有候选在变：

1. 初始化 `[batch]` 为 `hash_iv`。
2. **只哈希上下文**得到 `hash_result_with_just_context`（`[batch]`）——这一份会单独返回，用于后续判断上下文是否重复。
3. **续哈希每个候选下标**：用 `vmap` 沿「候选数」维并行，结果 `[batch, num_indices]`。
4. **续哈希每层 key**：同上，结果 `[batch, num_indices, depth]`。

> 直觉上，`_compute_keys` 是「先把上下文算好缓存，再对每个候选只补一步」的优化版；而 `compute_ngram_keys` 是「每个 ngram 从头算」的批处理版。最终都得到 `[batch, num_X, depth]` 形状的整数张量。

#### 4.1.3 源码精读

`compute_ngram_keys`（[src/synthid_text/logits_processing.py:358-401](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L358-L401)）：先把 `hash_result` 初始化为 `hash_iv`，再分别用两次 `vmap` 对「ngram 维」和「depth 维」并行哈希，最终输出 `[batch, num_ngrams, depth]`。

```python
# 每个 batch 项都从同一个不可预测的 hash_iv 起步
hash_result = torch.full(
    (batch_size,), self.hash_iv, dtype=torch.long, device=self.device
)
# 沿 dim=1（num_ngrams）并行，内部沿 ngram_len 累加哈希 -> [batch, num_ngrams]
hash_result = torch.vmap(
    hashing_function.accumulate_hash, in_dims=(None, 1), out_dims=1
)(hash_result, ngrams)

# keys 摆成 [1, 1, depth, 1]，沿 dim=2（depth）并行续哈希 -> [batch, num_ngrams, depth]
keys = self.keys[None, None, :, None]
hash_result = torch.vmap(
    hashing_function.accumulate_hash, in_dims=(None, 2), out_dims=2
)(hash_result, keys)
return hash_result
```

`_compute_keys`（[src/synthid_text/logits_processing.py:403-448](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L403-L448)）：多了一步「只哈希上下文」的中间结果，并把它单独返回。

```python
# 先把上下文(ngram_len-1 个 token)整体哈希一次 -> [batch]
hash_result_with_just_context = hashing_function.accumulate_hash(
    hash_result, n_minus_1_grams
)
# 对每个候选下标各续哈希一步 -> [batch, num_indices]
hash_result = torch.vmap(
    hashing_function.accumulate_hash, in_dims=(None, 1), out_dims=1
)(hash_result_with_just_context, indices[:, :, None])
# 对每层 key 各续哈希一步 -> [batch, num_indices, depth]
keys = self.keys[None, None, :, None]
hash_result = torch.vmap(
    hashing_function.accumulate_hash, in_dims=(None, 2), out_dims=2
)(hash_result, keys)
return hash_result, hash_result_with_just_context   # 第二个返回值用于上下文去重
```

可以看到，`keys = self.keys[None, None, :, None]`（形状 `[1, 1, depth, 1]`）是「在 depth 维上展开」的标准手法，配合 `vmap(..., in_dims=(None, 2))`，就让每把密钥各自走一条独立的哈希链，从而在第 `d` 层得到第 `d` 把 key 对应的整数。

底层 `accumulate_hash`（[src/synthid_text/hashing_function.py:21-51](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/hashing_function.py#L21-L51)）就是把数据按最后一维逐元素「加 → 乘乘子 → 加增量」，支持广播，因此能被 `vmap` 直接套用（[[u2-l2]]）。

#### 4.1.4 代码实践：跟踪张量形状

**实践目标**：不运行代码，纯靠阅读推算 `_compute_keys` 内部每一步张量的形状。

**操作步骤**：假设 `batch_size=2, ngram_len=5`（故上下文长度 = 4），`top_k=10`，`depth=3`（即 3 把 key）。

1. 输入 `n_minus_1_grams` 形状 `[2, 4]`，`indices` 形状 `[2, 10]`。
2. `hash_result` 初始化为 `[2]`（全是 `hash_iv`）。
3. `hash_result_with_just_context = accumulate_hash(hash_result, n_minus_1_grams)`，内部沿长度 4 累加 → 形状 `[2]`。
4. `indices[:, :, None]` 形状 `[2, 10, 1]`，`vmap` 沿 dim=1 映射后 → `[2, 10]`。
5. `keys[None, None, :, None]` 形状 `[1, 1, 3, 1]`，`vmap` 沿 dim=2 映射后 → `[2, 10, 3]`。

**预期结果**：第一个返回值 `ngram_keys` 形状为 `[2, 10, 3]`，即「batch × 候选数 × depth」；第二个返回值 `hash_result_with_just_context` 形状为 `[2]`。

**需要观察的现象**：第二步到第三步形状不变（`[2]`→`[2]`），因为 `accumulate_hash` 会把数据的最后一维「吃掉」；而 `vmap` 负责把被映射的那一维「补回来」。这正是「`vmap` + `accumulate_hash`」组合的形状规律。

> 本实践为源码阅读型，无需运行；若想验证，可参考 `test_compute_ngram_keys_shape`（[src/synthid_text/logits_processing_test.py:394-414](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing_test.py#L394-L414)），它断言 `ngram_keys.shape == (batch_size, num_ngrams, num_layers)`。

#### 4.1.5 小练习与答案

**练习 1**：`_compute_keys` 为什么要单独返回 `hash_result_with_just_context`，而不是只返回最终的 `ngram_keys`？

> **答案**：因为生成时需要判断「这个上下文是不是已经见过」，见过的上下文要跳过水印（避免重复水印导致可被简单统计攻击）。判断依据正是「只哈希上下文」的结果，所以必须单独把它返回出来，存进 `context_history` 后比对（详见 [[u3-l4]]）。

**练习 2**：如果把 `keys` 从 30 把减少到 3 把，`compute_ngram_keys` 输出的张量哪一维会变？

> **答案**：depth 维会从 30 变成 3，输出形状从 `[batch, num_ngrams, 30]` 变成 `[batch, num_ngrams, 3]`。batch 维与 ngram 维不受影响（与 [[u2-l1]]「`len(keys)` 决定 depth」一致）。

### 4.2 get_gvals 取位逻辑：从 64 位整数里挤出二进制位

#### 4.2.1 概念说明

上一节得到的 `ngram_keys` 是一批 64 位整数。但水印真正要用的是**二进制**信号：在 `update_scores` 里，g 值被当成 0/1 的乘性偏置——值为 1 表示「抬高这个 token 的概率」，值为 0 表示「不动」（见 [src/synthid_text/logits_processing.py:25-53](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L25-L53)）。

`get_gvals` 的任务就是：**从每个 64 位整数里，提取出「一个」近似均匀随机的比特**。之所以只要一个比特，是因为：水印靠的是「大量比特的统计聚合」，单个比特无关紧要，但一批 g 值的平均会稳定偏离 0.5——这正是检测器能识别的信号。

关键约束：这个比特必须**近似独立同分布、且 0/1 各占一半**，否则水印要么失效（不够随机），要么破坏文本质量（太偏）。`get_gvals` 通过「多次重新哈希 + 右移」来保证这一点。

#### 4.2.2 核心流程

`get_gvals` 的默认参数是 `num_apply_hash=12, shift=0`，逻辑分两步：

1. **算右移量**：`shift = shift or (64 // num_apply_hash)`。当 `shift=0`（即未指定）时，`64 // 12 = 5`，于是每轮右移 5 位。这里 `64` 是 int64 的位宽，`num_apply_hash` 是哈希轮数，二者相除让「总位移量」大致铺满 64 位而不过界。
2. **重复 12 轮**：每轮做 `ngram_keys = accumulate_hash(ngram_keys, [1]) >> shift`。先用常量数据 `[1]` 重新哈希一次（LCG 会把比特充分打乱），再右移 5 位（丢弃低位、让高位下移）。
3. **取最终比特**：`return (ngram_keys >> 30) % 2`。右移 30 位把第 30 比特移到最低位，再 `% 2` 取出它，得到 0 或 1。

用伪代码表示：

```text
shift = 64 // 12            # = 5
重复 12 次:
    ngram_keys = ( 用常量[1]重新哈希(ngram_keys) ) 右移 shift   # 每轮打乱并丢掉低位 5 比特
g = ( ngram_keys 右移 30 ) mod 2                              # 取第 30 比特作为最终 g 值
```

为什么最终取「第 30 比特」而不是最低位？因为经过 12 轮「打乱 + 右移」后，中高位的比特（如第 30 位）受到的混淆最充分，最接近均匀随机；直接取最低位往往偏差更大。这一组参数（`12` 轮、移 `5`、最终取 `30`）是经验上/理论上调好的，无需自己改。

> 提醒：函数的 docstring 写着「iteratively take the lowest three bits ... and add it to the previous gval」，这与实际实现（右移 5 位、最终取第 30 位）并不一致。按照本手册一贯原则——**文档与源码冲突时以源码为准**——本讲以上面的实现逻辑为准。

#### 4.2.3 源码精读

`get_gvals`（[src/synthid_text/logits_processing.py:328-356](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L328-L356)）：

```python
shift = shift or (64 // num_apply_hash)        # 0 or (64//12) = 5

for _ in range(num_apply_hash):                 # 重复 12 次
  ngram_keys = (
      hashing_function.accumulate_hash(ngram_keys, torch.LongTensor([1]))
      >> shift                                   # 用常量[1]重新哈希后右移 5 位
  )

return (ngram_keys >> 30) % 2                    # 取第 30 比特 -> 0 或 1
```

注意输入 `ngram_keys` 形状是 `[batch, num_ngrams, depth]`，整个循环都是逐元素运算，所以**输出形状与输入完全相同**，只是值域从「64 位整数」塌缩成了「0/1」。

在生成主流程 `watermarked_call` 里，g 值正是这样算出来的（[src/synthid_text/logits_processing.py:289-296](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L289-L296)）：

```python
ngram_keys, hash_result_with_just_context = self._compute_keys(
    self.state.context, top_k_indices
)                                                # [batch, top_k, depth]
g_values = self.get_gvals(ngram_keys)            # [batch, top_k, depth]，值域 0/1
```

#### 4.2.4 代码实践：验证 g 值近似无偏

**实践目标**：用随机数据确认 `get_gvals` 产出的比特「0 和 1 各占约一半」。

**操作步骤**：直接调用 `_compute_keys` + `get_gvals`（参考测试 `test_g_values_uniformity_across_vocab_size`，[src/synthid_text/logits_processing_test.py:166-208](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing_test.py#L166-L208)）。

```python
# 示例代码：非项目原有，仅为演示 get_gvals 的无偏性
import torch, numpy as np
from synthid_text import logits_processing as lp

device = torch.device("cpu")
ngram_len, vocab_size, num_layers, batch_size = 5, 100, 5, 1000
proc = lp.SynthIDLogitsProcessor(
    ngram_len=ngram_len,
    keys=np.random.randint(0, 2**16, size=(num_layers,)),
    context_history_size=512, device=device, top_k=10, temperature=1.0,
)
context = torch.randint(0, vocab_size, (batch_size, ngram_len - 1), device=device)
indices = torch.stack([torch.arange(vocab_size) for _ in range(batch_size)])
ngram_keys, _ = proc._compute_keys(context, indices)   # [batch, vocab, depth]
g = proc.get_gvals(ngram_keys)                          # [batch, vocab, depth]，0/1
print(g.shape, float(g.float().mean()))
```

**需要观察的现象**：`g.shape` 应为 `[1000, 100, 5]`；均值应非常接近 `0.5`。

**预期结果**：均值落在 `0.5 ± 0.001` 附近（测试里用 `assertAlmostEqual(..., 0.5, delta=0.001)` 断言）。如果偏离 0.5 很多，说明取位逻辑出了问题。

**待本地验证**：上述均值是统计期望，具体数值依赖随机种子，但应在 0.5 附近波动。

#### 4.2.5 小练习与答案

**练习 1**：把 `num_apply_hash` 从 12 改成 24（其它不变），`shift` 会变成多少？这会影响 g 值的「随机质量」吗？

> **答案**：`shift = 64 // 24 = 2`。每轮右移更少位、哈希更多轮，理论上混淆更充分，但计算量翻倍。对于默认用途（只要一个无偏比特）12 轮已足够；改 24 主要是性能开销变化，无偏性不会有明显差别。

**练习 2**：为什么 g 值必须是 0/1 二值，而不是直接用 `ngram_keys` 的某几位当分数？

> **答案**：水印信号藏在「大量二值比特的统计聚合」里（平均会偏离 0.5）。二值化后，`update_scores` 才能用简单的「`1 + g - g_mass`」这种乘性偏置逐层修正概率（见 [[u3-l3]]）；多值反而会让失真难以控制、也让理论期望（[[u7-l1]]）难以推导。

### 4.3 compute_g_values 的输入输出形状：为什么序列会变短

#### 4.3.1 概念说明

`compute_g_values` 是**检测侧的对外入口**：给它一段已经生成好的 token 序列，它返回这段序列的 g 值张量。它内部就是「滑窗切 ngram → 算 ngram key → 取比特」三步的串联。

这里有一个初学者常疑惑的点：**为什么输出序列长度比输入短了 `ngram_len - 1`？** 因为 g 值是「按 ngram」定义的——每 `ngram_len` 个连续 token 才能拼出一个 ngram、算出一组 g 值。一个长度为 `L` 的序列，能滑出的 ngram 数量是：

\[ \text{num\_ngrams} = L - \text{ngram\_len} + 1 = L - (\text{ngram\_len} - 1) \]

所以输出序列维正好少了 `ngram_len - 1` 个位置（默认 `ngram_len=5` 时少 4 个）。这个「变短」会在后续与掩码对齐时反复出现（见 [[u5-l1]]），务必记住。

#### 4.3.2 核心流程

`compute_g_values(input_ids)` 三步：

1. **滑窗切 ngram**：`ngrams = input_ids.unfold(dimension=1, size=ngram_len, step=1)`。`unfold` 沿序列维（dim=1）以步长 1 滑动、每次取 `ngram_len` 个 token，输出形状 `[batch, num_ngrams, ngram_len]`，其中 `num_ngrams = L - ngram_len + 1`。
2. **算每层 ngram key**：`ngram_keys = compute_ngram_keys(ngrams)`，形状 `[batch, num_ngrams, depth]`（见 4.1）。
3. **取比特**：`return get_gvals(ngram_keys)`，形状不变 `[batch, num_ngrams, depth]`，但值已二值化。

最终输出形状记作 `[batch, L - (ngram_len - 1), depth]`。

#### 4.3.3 源码精读

`compute_g_values`（[src/synthid_text/logits_processing.py:458-473](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L458-L473)）：

```python
def compute_g_values(self, input_ids):
  self._check_input_ids_shape(input_ids)                 # 要求二维 [batch, L]
  ngrams = input_ids.unfold(
      dimension=1, size=self.ngram_len, step=1           # 滑窗切 ngram -> [batch, num_ngrams, ngram_len]
  )
  ngram_keys = self.compute_ngram_keys(ngrams)           # -> [batch, num_ngrams, depth]
  return self.get_gvals(ngram_keys)                      # -> [batch, num_ngrams, depth]，值域 0/1
```

这一形状由测试 `test_compute_g_values_shape` 明确钉死（[src/synthid_text/logits_processing_test.py:349-362](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing_test.py#L349-L362)）：

```python
g_values = logits_processor.compute_g_values(sequences)
self.assertEqual(
    g_values.shape, (batch_size, sequence_len - (ngram_len - 1), num_layers)
)
```

即输出序列维 = `sequence_len - (ngram_len - 1)`。以测试默认值 `sequence_len=50, ngram_len=5, num_layers=3` 为例，输出形状就是 `[1000, 46, 3]`。

#### 4.3.4 代码实践：打印形状并解释变短（本讲主实践）

**实践目标**：构造很小的 vocab 与一段随机 token，调用 `compute_g_values` 打印形状，并亲手验证「输出长度 = 输入长度 - (ngram_len - 1)」。

**操作步骤**：

```python
# 示例代码：非项目原有，用于观察 compute_g_values 的形状变化
import torch, numpy as np
from synthid_text import logits_processing as lp

device = torch.device("cpu")
ngram_len, num_layers, depth = 5, 3, 3
proc = lp.SynthIDLogitsProcessor(
    ngram_len=ngram_len,
    keys=np.random.randint(0, 2**16, size=(depth,)),   # depth 把 key
    context_history_size=512, device=device, top_k=10, temperature=1.0,
)

batch_size, seq_len, vocab_size = 2, 20, 50
tokens = torch.randint(0, vocab_size, (batch_size, seq_len), device=device)
g = proc.compute_g_values(tokens)
print("input  shape:", tuple(tokens.shape), "(seq_len =", seq_len, ")")
print("output shape:", tuple(g.shape))
print("seq 维减少了:", seq_len - g.shape[1], "= ngram_len - 1 =", ngram_len - 1)
```

**需要观察的现象**：`output shape` 的序列维应为 `20 - (5 - 1) = 16`；打印的「减少了」一栏应等于 `4`。

**预期结果**：输出形状 `(2, 16, 3)`，即 `[batch, seq_len-(ngram_len-1), depth]`；序列维比输入少 4，正好是 `ngram_len - 1`。

**待本地验证**：具体数值（随机 token）每次不同，但形状关系固定不变，可用上面代码直接核验。

**解释（为什么少 `ngram_len - 1`）**：每个 g 值对应一个完整的 ngram（长度 `ngram_len`）。长度 `L` 的序列以步长 1 滑动、每次取 `ngram_len` 个 token，能滑出的窗口数为 \(L - \text{ngram\_len} + 1 = L - (\text{ngram\_len} - 1)\)。序列最前面的 `ngram_len - 1` 个位置无法凑齐一个完整 ngram（缺前置上下文），因此没有对应的 g 值，输出序列维就少了 `ngram_len - 1`。这与 [[u1-l4]] 里检测侧把序列维统一对齐到 `output_len-(ngram_len-1)` 完全一致。

#### 4.3.5 小练习与答案

**练习 1**：若把 `ngram_len` 从 5 调到 10，一段长度 50 的序列，`compute_g_values` 输出序列维是多少？

> **答案**：`50 - (10 - 1) = 41`。`ngram_len` 越大，需要的上下文越长，能算出 g 值的位置越少，输出序列维越小。

**练习 2**：`compute_g_values` 和生成侧 `watermarked_call` 里的 g 值计算，最终都汇入哪个公共函数？

> **答案**：都汇入 `get_gvals`。区别只在「ngram key 怎么来」：检测侧用 `compute_ngram_keys`（一批完整 ngram），生成侧用 `_compute_keys`（一个上下文 + 候选下标）。两者都产出 `[batch, num_X, depth]` 的整数，再交给 `get_gvals` 取比特。

## 5. 综合实践

把本讲三块知识串起来，完成一次「**手工走通 g 值生产线**」：

1. 用 `ngram_len=5, depth=3` 建一个 `SynthIDLogitsProcessor`。
2. 造一段长度 20 的随机 token，调用 `compute_g_values`，记下输出形状。
3. 用 `compute_ngram_keys` 单独喂**同一个**序列经 `unfold` 得到的 ngram，再调 `get_gvals`，验证结果与第 2 步**逐元素相等**（说明 `compute_g_values` 就是这两步的封装）。
4. 把 `depth` 从 3 改成 6（即给 `keys` 多加 3 把），重做第 2 步，观察输出形状的**哪一维**变了、变化量是多少，并据此回答「水印层数与 g 值张量的关系」。

**预期结论**：第 3 步应完全相等；第 4 步只有 depth 维从 3 变成 6，序列维与 batch 维不变。这验证了：g 值张量 `[batch, seq, depth]` 中，seq 维由序列长度与 `ngram_len` 决定，depth 维由 `len(keys)` 决定，二者独立。

## 6. 本讲小结

- **g 值**是「一个 ngram + 一把水印密钥」经哈希后取出的**一个二进制比特**，形状为 `[batch, seq, depth]`，是连接施加侧与检测侧的唯一数据。
- **`compute_ngram_keys` / `_compute_keys`** 用 `torch.vmap` 分别沿 ngram 维、depth 维并行哈希，把 token 序列压成 `[batch, num_X, depth]` 的整数张量；二者只是「输入形状」不同，算式同源。
- **`get_gvals`** 通过「12 轮 重新哈希 + 右移 5 位」充分混淆后「取第 30 比特」，把 64 位整数塌缩成近似均匀无偏的 0/1。
- **`compute_g_values`** 是检测侧入口，三步串联：`unfold` 滑窗 → `compute_ngram_keys` → `get_gvals`。
- 输出序列维 = **输入长度 - (ngram_len - 1)**，因为每个 g 值需要一个完整 ngram，前 `ngram_len - 1` 个位置凑不齐。
- 本讲再次印证「**文档与源码冲突时以源码为准**」：`get_gvals` 的 docstring 描述与实现不一致，以实现逻辑为准。

## 7. 下一步学习建议

- 想看 g 值在**生成时如何偏置概率**，进入 [[u3-l2]]（`watermarked_call` 主流程）与 [[u3-l3]]（`update_scores` 得分更新）。
- 想看 g 值在**检测时如何参与打分**，进入 [[u5-l1]]（掩码体系，会再次遇到「序列维对齐到 `-(ngram_len-1)`」）与 [[u5-l2]]（Mean / Weighted Mean 打分）。
- 想理解 g 值的理论期望（为什么水印文本的平均 g 值会稳定偏离 0.5），进入 [[u7-l1]]（`expected_mean_g_value`）。
- 建议同时阅读 `src/synthid_text/logits_processing_test.py` 中的 `test_g_value_uniformity_for_random_ngrams`（[L140-L164](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing_test.py#L140-L164)），它是检验「g 值无偏性」最直接的测试。
