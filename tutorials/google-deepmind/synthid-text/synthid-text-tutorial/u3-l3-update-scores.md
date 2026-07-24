# 得分更新：锦标赛与 distortionary 变体

## 1. 本讲目标

上一讲（u3-l2）我们走完了 `watermarked_call` 的 5 步主流程，在第 4 步「修改 scores」处一笔带过。本讲就专门拆开这一步，把水印施加「真正动手脚」的地方讲透。

学完本讲，你应当能够：

- 说出 `update_scores` 是如何**在不破坏概率分布总和为 1** 的前提下，把采样概率推向 g=1 的 token 的。
- 写出 `update_scores_distortionary` 的通用系数公式，并证明当 `num_leaves=2` 时它与 `update_scores` **数学上完全等价**。
- 解释 `num_leaves`（锦标赛叶子数 N）如何在「水印强度」与「文本失真」之间做权衡。

本讲只涉及一个源码文件：`src/synthid_text/logits_processing.py`，但会结合 `g_value_expectations.py` 的理论值来验证直觉。

## 2. 前置知识

在进入源码前，先用一句话复习三个关键概念（详细推导见 u2-l3 与 u3-l2）：

- **g 值**：一个 ngram 加上一把水印密钥，经哈希后取出的**一颗二进制比特**，取值 0 或 1。每个 token 在每个「深度」上各有一颗 g 值，故 g 值张量形状为 `[batch, vocab(或 top_k), depth]`，`depth = len(keys)`（默认 30）。
- **scores**：模型对每个候选 token 打出的**对数概率（logits）**。本讲两个函数都假设输入是 log 空间，函数内部先 `softmax` 成概率再用，最后再 `log` 回去。
- **水印的检测原理**：如果生成时把概率悄悄推向 g=1 的 token，那么实际生成文本的 g 值平均数会**偏离 0.5 而偏高**；检测侧读到这个偏高就是水印信号。本讲讲的，正是「如何悄悄推」的数学。

一个贯穿全讲的记号：设当前某层深度上，g=1 的 token 所占的**概率质量**为

\[
m = \sum_{j} g_j \, p_j
\]

它就是源码里的 `g_mass_at_depth`。注意 $m$ 不是 g=1 token 的**个数**，而是它们**带权重的概率之和**。如果概率分布大致均匀、约一半 token 的 g=1，则 $m \approx 0.5$。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/synthid_text/logits_processing.py` | 本讲主角。包含 `update_scores`（标准锦标赛）、`update_scores_distortionary`（通用 N 变体），以及在 `watermarked_call` 里二选一的派发逻辑。 |
| `src/synthid_text/g_value_expectations.py` | 给出均匀 LM 分布下、单层锦标赛的理论期望 g 值（N=2 与 N=3 两种），用于校验「推得多狠」是否正确。 |
| `src/synthid_text/logits_processing_test.py` | 用大 batch 随机试验验证实际均值是否收敛到理论期望，是理解 `num_leaves` 行为的最佳示例。 |

## 4. 核心概念与源码讲解

### 4.1 update_scores：标准锦标赛（num_leaves=2）的概率守恒修正

#### 4.1.1 概念说明

`update_scores` 是水印施加的「默认算法」，对应论文里的**标准锦标赛（standard tournament）**，叶子数 N=2。

它的目标看似矛盾：既要**改变**采样分布（让 g=1 的 token 更容易被抽中），又**不能改变分布的总和**（概率和必须恒为 1，否则采样会出错，文本质量也会崩）。

解决办法是：对每个 token，乘上一个**只依赖 g 值与当前概率质量 $m$ 的系数**，并精心选择系数，使得「g=1 的被放大、g=0 的被缩小」，同时所有系数的加权平均恰好为 1。

为什么叫「锦标赛」？直观理解：把候选 token 想象成两两（N=2）对决，g=1 的选手赢；对所有对决取期望后，等价于给每个 token 乘一个「胜率倍率」。N=2 的对决恰好能化简成下面这组极简系数。

#### 4.1.2 核心流程

输入：`scores [B, V]`（log 空间）、`g_values [B, V, depth]`。

```
1. probs = softmax(scores, dim=1)          # 转成概率，和为 1
2. 对每一个深度 i = 0 .. depth-1：
     g_i = g_values[:, :, i]               # 本层每个 token 的 g 值 [B, V]
     m   = sum(g_i * probs, axis=1)        # 本层 g=1 token 占的概率质量 [B,1]
     对 g=1 的 token：probs *= (1 + 1 - m) = 2 - m   # 放大
     对 g=0 的 token：probs *= (1 + 0 - m) = 1 - m   # 缩小
3. log_probs = log(probs)，把 -inf/NaN 兜底成 -1e12
4. 返回 log_probs [B, V]
```

**数学验证（概率守恒）**：设本层 g=1 的总质量为 $m$，则 g=0 的总质量为 $1-m$。更新后总和为

\[
(2-m)\cdot m \;+\; (1-m)\cdot(1-m)
= 2m - m^{2} + 1 - 2m + m^{2} = 1
\]

所以**每一层都保持归一化**——这就是水印「难以察觉」的数学根源：它只是把同一块概率蛋糕在 g=1/g=0 之间重新切分，从不凭空增减。

当 $m \approx 0.5$ 时，g=1 的倍率是 $2-0.5=1.5$，g=0 的倍率是 $1-0.5=0.5$，即一个 g=1 token 的相对权重是一个 g=0 token 的 **3 倍**。这就是单层施加的「推力」。

#### 4.1.3 源码精读

整段函数不到 30 行，但每一行都关键。先看整体：

[update_scores 函数定义与 softmax 起手](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L25-L42)：函数先把 `scores` 从 log 空间 `softmax` 成概率 `probs`，再取出 `depth`（即 `len(keys)`，默认 30），准备逐层循环。

[逐层概率修正循环（核心两行）](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L44-L47)：

```python
g_values_at_depth = g_values[:, :, i]
g_mass_at_depth = (g_values_at_depth * probs).sum(axis=1, keepdims=True)  # m
probs = probs * (1 + g_values_at_depth - g_mass_at_depth)
```

- `g_mass_at_depth` 就是上面的 $m$，`keepdims=True` 保证它能和 `[B, V]` 的 `probs` 广播相乘。
- 关键乘子 `1 + g_values_at_depth - g_mass_at_depth`：当 `g=1` 得 `2-m`，当 `g=0` 得 `1-m`，与推导完全一致。

[log 兜底与返回](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L49-L53)：循环结束后 `probs` 里可能有些 g=0 token 被逐层压成精确的 0，`log(0)=-inf`。这里用 `torch.where(isfinite, ..., -1e12)` 把它们替换成一个很大的负数 `-1e12`，保证返回值仍是合法 log 空间分数、可安全交给采样器。

#### 4.1.4 代码实践

下面是一段**示例代码**（非项目原有代码），直接调用 `update_scores`，验证「概率守恒」与「g=1 被放大」两个性质。需在装好 torch 与 synthid_text 的环境中运行。

```python
# 示例代码
import torch
from synthid_text import logits_processing

torch.manual_seed(0)
B, V, depth = 1, 8, 4
scores   = torch.randn(B, V)                       # 原始 logits
g_values = torch.randint(0, 2, (B, V, depth)).float()

updated = logits_processing.update_scores(scores, g_values)
probs_before = torch.softmax(scores, dim=1)
probs_after  = torch.softmax(updated, dim=1)

print("归一化前:", probs_before.sum().item())     # 预期 1.0
print("归一化后:", probs_after.sum().item())      # 预期 1.0（概率守恒）
# 看 g=1 的 token 在施加后是否获得了更高概率
g_layer0 = g_values[0, :, 0]
print("g=1 token 平均增益:",
      (probs_after[0][g_layer0==1] / probs_before[0][g_layer0==1]).mean().item())
```

需要观察的现象与预期结果：

1. 两个 `sum()` 都应打印 `1.0`（允许极小浮点误差）——验证每一层都保持归一化。
2. g=1 token 的平均增益应 > 1，g=0 token 的应 < 1。
3. 具体数值「待本地验证」，但方向（g=1 被放大、g=0 被缩小）是确定的。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `update_scores` 必须先 `softmax` 成概率、最后再 `log` 回去，而不是直接在 log 空间操作？

**答案**：因为「概率守恒」（和为 1）是这套乘子设计的核心约束，它只在概率空间成立。若直接在 log 空间加减，无法保证指数还原后仍归一化。函数对外契约是 log 空间（方便与 HF 采样器拼接），对内则是概率空间。

**练习 2**：当某一层几乎所有概率都已集中在 g=1 token 上（$m \to 1$）时，g=0 和 g=1 的倍率分别趋于多少？这说明了什么？

**答案**：$m \to 1$ 时，g=1 倍率 $2-m \to 1$（不再额外放大），g=0 倍率 $1-m \to 0$（被彻底压垮）。说明该层已「收敛」：概率牢牢留在 g=1 token 上，符合「逐层累积偏向」的直觉。

---

### 4.2 update_scores_distortionary：通用 num_leaves 的失真变体

#### 4.2.1 概念说明

`update_scores_distortionary` 是锦标赛的**通用版本**，叶子数 `num_leaves`（记为 N）可以是任意 ≥2 的整数（但理论只对 N=2、3 给出了闭式期望）。N>2 时，每个「锦标赛节点」比较 N 个候选，对原始 LM 分布的**扭曲更大**，函数名里的 distortionary（失真的）正是此意。

它与 `update_scores` 的关系是本讲最关键的结论：

> 当 `num_leaves=2` 时，`update_scores_distortionary` 与 `update_scores` **在数学上完全等价**。

所以 `update_scores` 不过是 N=2 的一个更省计算、数值更稳的特化实现（少了一次除法、少了一次幂运算）。下一节的派发逻辑正是基于这一点。

#### 4.2.2 核心流程

输入多了 `num_leaves`（N）。前两步与 `update_scores` 相同，区别在第 2 步的系数：

```
1. probs = softmax(scores, dim=1)
2. 对每一个深度 i：
     g_i = g_values[:, :, i]
     m   = sum(g_i * probs, axis=1)            # g=1 token 的概率质量
     对 g=0 的 token：coeff = (1 - m) ** (N - 1)
     对 g=1 的 token：coeff = (1 - (1 - m) ** N) / m
     probs = probs * coeff
3. log_probs = log(probs)，兜底 -1e12，返回
```

**数学验证（概率守恒）**：更新后总和为 g=1 部分 $m \cdot \text{coeff\_in}$ 加 g=0 部分 $(1-m)\cdot \text{coeff\_not\_in}$：

\[
m \cdot \frac{1-(1-m)^{N}}{m} \;+\; (1-m)\cdot(1-m)^{N-1}
= \bigl[1-(1-m)^{N}\bigr] + (1-m)^{N} = 1
\]

依然归一化。✅

**N=2 退化验证**：代入 N=2：

\[
\text{coeff\_in} = \frac{1-(1-m)^{2}}{m} = \frac{1-(1-2m+m^{2})}{m} = 2-m
\]

\[
\text{coeff\_not\_in} = (1-m)^{2-1} = 1-m
\]

正好就是 `update_scores` 的 $(2-m)$ 与 $(1-m)$。这从代数上证明了二者等价。

**N=3 的推力对比**：取 $m=0.5$，N=3 时 coeff_in $= 3-3m+m^{2}=3-1.5+0.25=1.75$，coeff_not_in $=(0.5)^{2}=0.25$，g=1 相对 g=0 的权重比为 $1.75/0.25=7$ 倍（N=2 时只有 3 倍）。可见 N 越大，单层推力越猛。

#### 4.2.3 源码精读

[update_scores_distortionary 函数签名与起手](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L56-L75)：比 `update_scores` 多一个 `num_leaves` 参数；同样先 `softmax`、取 `depth`。

[两组系数的计算](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L80-L87)：

```python
coeff_not_in_g = (1 - g_mass_at_depth) ** (num_leaves - 1)
coeff_in_g = (1 - (1 - g_mass_at_depth) ** (num_leaves)) / g_mass_at_depth
coeffs = torch.where(
    torch.logical_and(g_values_at_depth == 1, probs > 0),
    coeff_in_g,
    coeff_not_in_g,
)
probs = probs * coeffs
```

三个要点：

- `coeff_not_in_g` 与 `coeff_in_g` 分别对应公式里的 $(1-m)^{N-1}$ 与 $\frac{1-(1-m)^{N}}{m}$。
- `torch.where` 按 g 值挑选系数：g=1 且概率大于 0 的 token 用 `coeff_in_g`，其余用 `coeff_not_in_g`。
- **数值保护**：当 $m=0$（本层没有 g=1 的 token）时 `coeff_in_g` 是 $0/0$，会得到 NaN。但此时 `where` 的条件 `g==1` 全部为假，所有 token 都选 `coeff_not_in_g = (1-0)^{N-1}=1`，NaN 虽被计算却不会被选中，因此不会污染 `probs`。这是 `where` 短路掉病态分支的典型用法（注意 PyTorch 的 `where` 仍会**求值**两个分支，只是不**采用**）。

[log 兜底与返回](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L89-L93)：与 `update_scores` 完全一致，把 `log(0)` 的 `-inf` 兜成 `-1e12`。

#### 4.2.4 代码实践

下面这段**示例代码**直接对比三个调用，最能说明问题（与下一节 4.3 的派发逻辑呼应）：

```python
# 示例代码
import torch
from synthid_text import logits_processing

torch.manual_seed(0)
B, V, depth = 1, 8, 4
scores   = torch.randn(B, V)
g_values = torch.randint(0, 2, (B, V, depth)).float()

u2     = logits_processing.update_scores(scores, g_values)                       # N=2 标准版
ud2    = logits_processing.update_scores_distortionary(scores, g_values, 2)      # N=2 通用版
ud3    = logits_processing.update_scores_distortionary(scores, g_values, 3)      # N=3 失真版

print("N=2 两版等价？", torch.allclose(u2, ud2, atol=1e-5))    # 预期 True
print("N=2 vs N=3 平均差:", (u2 - ud3).abs().mean().item())     # 预期 > 0
# 比较「最爱」与「最不爱」token 的分数差，看谁推得更狠
print("N=2 极差:", (u2.max(1).values - u2.min(1).values).item())
print("N=3 极差:", (ud3.max(1).values - ud3.min(1).values).item())
```

需要观察的现象与预期结果：

1. `N=2 两版等价？` 应为 `True`——直接验证 4.2.2 的等价性。
2. N=3 的分数「极差」应明显大于 N=2——说明 N=3 把概率推得更两极化，即对原始分布的扭曲更大。
3. 精确数值「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：当 $m=0$（本层无 g=1 token）时，`update_scores_distortionary` 会让 `probs` 变成什么？为什么不会出错？

**答案**：所有 token 的 g=0，都选 `coeff_not_in_g=(1-0)^{N-1}=1`，`probs` 不变。`coeff_in_g` 虽是 $0/0$ 的 NaN，但 `where` 条件全为假而不被采用，故不污染结果。

**练习 2**：手算 N=4、$m=0.5$ 时 g=1 与 g=0 的系数之比，与 N=2（3 倍）、N=3（7 倍）比较，趋势是什么？

**答案**：coeff_in $=\frac{1-0.5^{4}}{0.5}=\frac{1-0.0625}{0.5}=1.875$，coeff_not_in $=0.5^{3}=0.125$，比值 $1.875/0.125=15$ 倍。趋势：N 越大，单层推力增长很快（3→7→15），水印越强、失真也越大。

---

### 4.3 num_leaves 的选择：水印强度 vs 文本失真

#### 4.3.1 概念说明

`num_leaves`（N）是水印施加**唯一一个直接影响「强度—失真权衡」的旋钮**。从 4.1、4.2 已知：N 越大，每一层把概率推向 g=1 的力度越大。

这个「力度」最终体现为一个可观测量——**生成文本的 g 值平均数**。对均匀 LM 分布，`g_value_expectations.expected_mean_g_value` 给出了理论闭式：

\[
\text{N=2}: \quad \mathbb{E}[\bar g] = 0.5 + 0.25\left(1-\frac{1}{V}\right)
\]

\[
\text{N=3}: \quad \mathbb{E}[\bar g] = \frac{7}{8} - \frac{3}{8V}
\]

（分别对应论文补充材料的 Corollary 27 与 Theorem 25。）以词表 V=1000 为例：N=2 期望约 0.7497，N=3 期望约 0.8746。也就是说，N=3 的水印信号比 N=2 **强约 0.125**——离 0.5（无水印）更远，检测器更容易把它和自然文本区分开。

代价是：N 越大，对原始 LM 分布的扭曲越大，**文本质量（流畅度、困惑度）下降越多**。「distortionary」之名由此而来。因此选 N 是一个工程权衡，而非越大越好。

#### 4.3.2 核心流程：watermarked_call 如何二选一

在 `watermarked_call` 的第 4 步，处理器根据自身 `_num_leaves` 字段派发：

```
if self._num_leaves == 2:
    updated_scores = update_scores(scores_top_k, g_values)            # 标准、省算
else:
    updated_scores = update_scores_distortionary(scores_top_k, g_values, self._num_leaves)  # 通用、可调
```

因为 N=2 时二者等价（4.2 已证），所以默认走更快、数值更稳的 `update_scores`；只有当用户显式传入 `num_leaves != 2` 时才走通用版。这是一个「特化快路径 + 通用慢路径」的经典设计。

#### 4.3.3 源码精读

[派发逻辑：根据 _num_leaves 二选一](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L299-L304)：

```python
if self._num_leaves == 2:
  updated_scores = update_scores(scores_top_k, g_values)
else:
  updated_scores = update_scores_distortionary(
      scores_top_k, g_values, self._num_leaves
  )
```

这是上一讲 u3-l2 第 4 步的完整实现。注意它的输入是 `scores_top_k [B, top_k]`（稀疏化后的少量候选）而非全词表，这也是为什么水印只在 top_k 个候选间重新分配概率——既施加了水印，又控制了延迟（详见 u4-l1、u7-l3）。

[num_leaves 的来源：构造函数默认值与校验](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L146-L196)：构造函数签名里 `num_leaves: int = 2`，默认即走标准锦标赛；它被存入 `self._num_leaves`。注意：源码**只对 `temperature`、`top_k` 做了合法性校验**（u3-l1 讲过），对 `num_leaves` 并未做 `==2 或 ==3` 的强校验——传 4、5 代码也能跑，只是 `expected_mean_g_value` 那一侧的理论值会抛错，且效果未经论文验证。所以在工程上，`num_leaves` 的合理取值实际只有 2 与 3。

[理论期望的闭式实现](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/g_value_expectations.py#L37-L49)：`expected_mean_g_value` 显式只支持 N=2 与 N=3，其余抛 `ValueError`。它正是上面两个公式的来源，也是测试用例判断「实际均值是否收敛」的基准。

[测试如何用 num_leaves 验证收敛](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing_test.py#L278-L293)：测试参数表里同时挂了 `num_leaves=3` 的用例，用 50000 的 batch 跑随机 token，把实际 g 值均值与 `expected_mean_g_value(...)` 比较。这是理解「N 越大均值越高」最直接的证据。

#### 4.3.4 代码实践

本讲的主实践：**取一段固定 scores，分别用 num_leaves=2 与 3 走完整处理器，对比输出的 updated_scores 差异**。下面**示例代码**同时演示「直接调函数」与「通过处理器派发」两条路径：

```python
# 示例代码
import torch
from synthid_text import logits_processing

torch.manual_seed(0)
B, V, depth = 2, 6, 5
scores   = torch.randn(B, V)
g_values = torch.randint(0, 2, (B, V, depth)).float()

# 路径 A：直接调两个函数（最干净，聚焦打分逻辑）
out2 = logits_processing.update_scores(scores, g_values)
out3 = logits_processing.update_scores_distortionary(scores, g_values, num_leaves=3)

# 路径 B：通过处理器派发（模拟真实生成时的 if/else）
#    这里只看派发等价性，不真正跑 watermarked_call（那需要构造 state/上下文）
for n in (2, 3):
    proc = logits_processing.SynthIDLogitsProcessor(
        ngram_len=depth, keys=list(range(depth)),
        context_history_size=16, temperature=0.7, top_k=V,
        device=scores.device, num_leaves=n,
    )
    # 构造期即可确认 num_leaves 被正确保存
    print(f"num_leaves={n} -> proc._num_leaves =", proc._num_leaves)

p2 = torch.softmax(out2, dim=1)
p3 = torch.softmax(out3, dim=1)
print("N=2 最大 token 概率:", p2.max(dim=1).values.mean().item())
print("N=3 最大 token 概率:", p3.max(dim=1).values.mean().item())
```

需要观察的现象与预期结果：

1. `proc._num_leaves` 应分别打印 2 与 3，确认构造参数正确流入派发字段。
2. N=3 下「最大 token 概率」的均值应**大于** N=2——因为 N=3 把概率更集中地推向少数 g=1 token，分布更尖。
3. 具体数值「待本地验证」，但 N=3 比 N=2 更「尖」的方向是确定的。

> 进阶：若想看长期统计，可仿照 `does_mean_g_value_matches_theoretical`（`logits_processing_test.py`）用大 batch 重复采样，计算生成 token 的 g 值均值，应分别收敛到约 0.75（N=2）与 0.875（N=3）。

#### 4.3.5 小练习与答案

**练习 1**：假设你希望水印「更难被攻击者察觉」，应该选 N=2 还是 N=3？为什么？

**答案**：选 N=2。N=2 对原始 LM 分布的扭曲更小（单层推力 3 倍 vs 7 倍），文本质量更接近无水印版本，更难被察觉；代价是检测信号较弱（均值约 0.75 vs 0.875），需要更多 token 才能达到同样的检测置信度。这是一个典型的强度—隐蔽性权衡。

**练习 2**：为什么 `update_scores` 和 `update_scores_distortionary` 可以共用同一套「log 兜底成 -1e12」的收尾代码？

**答案**：两者都先 `softmax` 到概率空间、逐层乘系数、最后 `log` 回去，且概率守恒保证 `probs` 非负。唯一的病态来源是某些 token 被逐层压成精确 0 导致 `log(0)=-inf`，这在两个函数里都会发生，因此共用同一段兜底逻辑即可。

## 5. 综合实践

把本讲三个模块串起来，做一个「水印打分数学自检」小任务（纯阅读 + 少量计算，无需 GPU）：

1. **读公式**：从源码抄出 `update_scores` 与 `update_scores_distortionary` 的核心两行，手写推导证明两者在 N=2 时等价（参考 4.2.2）。
2. **算理论值**：用 `g_value_expectations.expected_mean_g_value` 计算 V=1000、N=2 与 N=3 的理论期望，解释为什么 N=3 更高（提示：单层推力更大 → g=1 token 被采中的概率更高）。
3. **跑对比**：运行 4.3.4 的示例代码，记录 N=2 / N=3 下「最大 token 概率」与「分布极差」，用一句话描述 N 增大带来的两个可观测变化（分布更尖、极差更大）。
4. **定位派发点**：在 `logits_processing.py` 中找到 `watermarked_call` 里 `if self._num_leaves == 2` 的那一处（L299-L304），解释为什么把这个判断放在这里、而不是在构造函数里就固定调用哪个函数。

完成后再回到 u3-l2 的 5 步流程图，在第 4 步「修改 scores」旁边补注：N=2 走 `update_scores`、否则走 `update_scores_distortionary`，本讲就真正落地了。

## 6. 本讲小结

- `update_scores`（标准锦标赛，N=2）通过乘子 $(2-m)$ / $(1-m)$ 在**概率守恒**的前提下把概率推向 g=1 token，$m$ 是当前 g=1 token 的概率质量。
- `update_scores_distortionary` 是通用 N 版本，系数为 $(1-m)^{N-1}$ 与 $\frac{1-(1-m)^{N}}{m}$；**N=2 时与 `update_scores` 完全等价**。
- 两个函数都「先 softmax 到概率空间 → 逐层乘系数 → log 回去」，并用 `torch.where` 把 `log(0)` 兜底成 `-1e12`。
- `watermarked_call` 用 `if self._num_leaves == 2` 派发：默认走更快更稳的 `update_scores`，仅 N≠2 时才走通用版。
- `num_leaves` 是强度—失真权衡旋钮：N 越大，水印信号越强（理论均值从约 0.75 升到 0.875），但对 LM 分布的扭曲也越大。
- 源码对 `num_leaves` 未做强校验，但 `expected_mean_g_value` 只支持 2、3，故工程上合理取值只有 2 与 3。

## 7. 下一步学习建议

本讲补完了 u3-l2 第 4 步的细节，水印施加侧的核心数学至此讲完。建议按以下顺序继续：

- **横向**：进入单元四，学习 `synthid_mixin.py` 如何把 `SynthIDLogitsProcessor` 挂进 HuggingFace 的采样循环（u4-l1 → u4-l2），理解 `updated_scores` 与 `top_k_indices` 如何被 `torch.vmap(torch.take)` 回映成稠密 token。
- **纵向（检测）**：跳到单元五，看检测侧如何重算 g 值并用 `mean_score` / `weighted_mean_score` 读取本讲施加的「均值偏高」信号（u5-l1 → u5-l2）。
- **理论**：若想深究锦标赛期望公式的来源，直接读 `g_value_expectations.py` 并对照论文补充材料的 Corollary 27（N=2）与 Theorem 25（N=3）。
