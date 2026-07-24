# 理论期望值：expected_mean_g_value

## 1. 本讲目标

本讲聚焦整个仓库里最小、最「纯」的一个文件：`g_value_expectations.py`。它只有一个函数 `expected_mean_g_value`，没有任何深度学习依赖，却回答了一个关键问题——**「水印文本的 g 值均值在理论上到底应该是多少？」**

学完本讲你应该能够：

1. 说清 `expected_mean_g_value` 的全部假设：均匀 LM 分布、单层锦标赛、`Bernoulli(0.5)` 的 g 值、`N = num_leaves` 个候选。
2. 亲手推导 `num_leaves=2`（标准锦标赛）与 `num_leaves=3`（distortionary 变体）两个闭式期望公式，并理解它们与上一讲 `mean_score` 里「约 0.75」这个数字的关系。
3. 知道这个理论值如何被测试套件当作「标尺」，用来校验水印施加实现的正确性。

## 2. 前置知识

本讲是专家层讲义，会用到前几讲已经建立的认知，不重复：

- **g 值**（u2-l3）：一个 ngram + 一把水印密钥经哈希取出的一颗二进制比特，近似服从 `Bernoulli(0.5)`、与 token 的概率无关。
- **得分更新**（u3-l3）：`update_scores`（`num_leaves=2`）与 `update_scores_distortionary`（通用 `N`）按层把概率质量从 g=0 推向 g=1，且每层概率守恒。本讲会反复用到它们引入的量 **`g_mass_at_depth`**，即本层 g=1 token 的总概率，记作 \( m \)。
- **Mean 打分**（u5-l2）：`mean_score` 就是「未屏蔽 g 值的算术平均」。非水印文本均值聚集在 0.5，水印文本被推高；本讲精确刻画这个「被推高到多少」。

此外需要一点概率论基础：期望的线性性、伯努利分布、二项分布 `Binomial(V, 1/2)` 的低阶矩。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/synthid_text/g_value_expectations.py](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/g_value_expectations.py) | 唯一的纯 Python 文件，给出 `num_leaves=2/3` 两种闭式期望，并在注释中标注论文出处。 |
| [src/synthid_text/logits_processing.py](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py) | 提供 `update_scores`、`update_scores_distortionary` 与 `watermarked_call` 中的派发逻辑，是推导期望公式的源头。 |
| [src/synthid_text/logits_processing_test.py](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing_test.py) | 辅助函数 `does_mean_g_value_matches_theoretical` 把「理论值」和「经验均值」对拍，用于校验实现。 |

## 4. 核心概念与源码讲解

### 4.1 期望 g 值：把「水印强度」变成一个可预测的数

#### 4.1.1 概念说明

检测之所以能成立（u5-l2），是因为水印文本的 g 值均值会偏离 0.5。上一讲我们反复用一个近似数字「`num_leaves=2` 时约 0.75」来描述这种偏离——**这个 0.75 不是拍脑袋估的，而是可以闭式算出来的理论值**，就来自 `expected_mean_g_value`。

这个函数的定位是**一把理论标尺**：

- 它不依赖任何运行时状态，只吃两个整数（词表大小 `vocab_size`、叶子数 `num_leaves`）。
- 它在「均匀 LM 分布」这个理想化假设下，给出「单层锦标赛水印后，被采样 token 的 g 值的期望」。
- 测试与调参时，把它和实际跑出来的经验均值对拍，就能判断水印实现是否正确。

#### 4.1.2 核心流程

函数逻辑极其简单——按 `num_leaves` 分支返回两个闭式公式，其余取值直接报错：

```text
expected_mean_g_value(vocab_size, num_leaves):
  若 num_leaves == 2: 返回 0.5 + 0.25 * (1 - 1/vocab_size)
  若 num_leaves == 3: 返回 7/8 - 3/(8*vocab_size)
  否则: 抛 ValueError
```

注意函数文档里点明的三个假设，它们是后续推导的全部前提：

1. LM 分布 `p_LM` 是**均匀**的（每个 token 等概率 \( 1/V \)，\( V \) 为词表大小）。
2. 这只是**单层**（single-layer）锦标赛的期望。多层水印时每层对称，期望相同。
3. g 值服从 `Bernoulli(0.5)`，每个 token 取 N=`num_leaves` 个候选参与锦标赛。

#### 4.1.3 源码精读

整段函数很短，核心是两个 `return` 与论文出处注释：

[src/synthid_text/g_value_expectations.py:L19-L49](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/g_value_expectations.py#L19-L49) —— 函数签名、文档（点明均匀 LM、单层、`Bernoulli(0.5)`、N=num_leaves 四个假设），以及 `num_leaves==2` / `==3` 两条分支与 `else` 抛错。

其中关键的两行公式与论文对应关系：

[src/synthid_text/g_value_expectations.py:L37-L44](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/g_value_expectations.py#L37-L44) —— `num_leaves==2` 对应论文补充材料 **Corollary 27**；`num_leaves==3` 对应 **Theorem 25**（取 N=3 且 p_LM 均匀的特例）。

非 2、3 的取值会被拒绝：

[src/synthid_text/g_value_expectations.py:L45-L49](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/g_value_expectations.py#L45-L49) —— 抛 `ValueError`，只支持 2 或 3 个叶子。

#### 4.1.4 代码实践

**实践目标**：直接调用函数，直观感受「词表越大、叶子越多，期望 g 值越高」。

**操作步骤**：在装好本库的环境里执行（示例代码）：

```python
# 示例代码
from synthid_text import g_value_expectations as gve

for V in [10, 100, 1000, 10_000]:
    e2 = gve.expected_mean_g_value(V, num_leaves=2)
    e3 = gve.expected_mean_g_value(V, num_leaves=3)
    print(f"V={V:>6}  N=2 -> {e2:.6f}   N=3 -> {e3:.6f}")

# 试试非法取值，观察报错
# gve.expected_mean_g_value(1000, num_leaves=4)  # 预期抛 ValueError
```

**预期结果**（闭式可手算，无需运行）：

| V | N=2 | N=3 |
| --- | --- | --- |
| 10 | 0.725000 | 0.837500 |
| 100 | 0.747500 | 0.871250 |
| 1000 | 0.749750 | 0.874625 |
| 10000 | 0.749975 | 0.874963 |
| →∞ | 0.75 | 0.875 |

**需要观察的现象**：随 V 增大，两列都单调逼近各自的极限 0.75 / 0.875；N=3 始终高于 N=2。若放开注释行，应抛 `ValueError: Only 2 or 3 leaves are supported...`。

#### 4.1.5 小练习与答案

**练习**：把 `vocab_size=1` 代入 N=2 公式，得到多少？它合理吗？

**答案**：\( 0.5 + 0.25(1 - 1) = 0.5 \)。合理——词表只有一个 token 时根本没有「选择」，水印无从施加，g 值均值退化回无偏硬币的 0.5。这正好说明「`1 - 1/V`」这一项刻画的是「词表多样性」。

---

### 4.2 num_leaves=2 的期望公式

#### 4.2.1 概念说明

标准锦标赛（`num_leaves=2`）走的是 `update_scores`，它的更新系数是 `g=1 乘 (2−m)`、`g=0 乘 (1−m)`。我们要算的量是：**水印后从更新分布里采样一个 token，它的 g 值的期望**，再对 g 值的随机赋值取平均。直觉上，由于 g=1 的概率质量被放大、g=0 被压缩，期望必然高于 0.5；推导会告诉我们恰好高到 \( 3/4 - 1/(4V) \)。

#### 4.2.2 核心流程

设本层 g=1 token 的总概率为 \( m \)（即源码里的 `g_mass_at_depth`）。

第一步，**给定一次 g 赋值**，求被采样 token 的 g 值期望。`update_scores` 对 g=1 token 把概率乘以 \( (2-m) \)，所以：

\[
\mathbb{E}[g_{\text{winner}}\mid m] = \sum_{t:\,g_t=1} p_t\,(2-m) = (2-m)\sum_{t:\,g_t=1}p_t = (2-m)\,m.
\]

第二步，**对 g 的随机赋值取期望**。均匀 LM 下 \( p_t = 1/V \)，设 \( K \) 为被分到 g=1 的 token 数，则 \( K\sim\mathrm{Binomial}(V,\tfrac12) \)、\( m = K/V \)。用二项分布的低阶矩：

\[
\mathbb{E}[m]=\tfrac12,\qquad \mathbb{E}[m^2]=\tfrac14+\tfrac{1}{4V}.
\]

于是

\[
\mathbb{E}[g_{\text{winner}}] = \mathbb{E}[(2-m)m] = 2\mathbb{E}[m] - \mathbb{E}[m^2] = 1 - \left(\tfrac14+\tfrac{1}{4V}\right) = \tfrac34 - \tfrac{1}{4V}.
\]

稍作变形即得源码形式：

\[
\tfrac34 - \tfrac{1}{4V} = \tfrac12 + \tfrac14\left(1-\tfrac1V\right).
\]

当 \( V\to\infty \)，\( m \) 收敛到 \( 1/2 \)，期望收敛到 \( 3/4 = 1-(1/2)^2 \)——正是「抛 2 枚硬币至少出现一次正面」的概率，对应「抽 2 个候选，只要有一个 g=1 就赢」的锦标赛直觉。

#### 4.2.3 源码精读

更新系数来自 `update_scores`：

[src/synthid_text/logits_processing.py:L42-L47](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L42-L47) —— 先 softmax 到概率，逐层用 `g_mass_at_depth = (g*probs).sum()` 算出 \( m \)，再执行 `probs = probs * (1 + g_values_at_depth - g_mass_at_depth)`，即 g=1 乘 \( (2-m) \)、g=0 乘 \( (1-m) \)。

`watermarked_call` 据此派发：

[src/synthid_text/logits_processing.py:L299-L300](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L299-L300) —— `_num_leaves==2` 时调用标准 `update_scores`。

闭式结果：

[src/synthid_text/g_value_expectations.py:L37-L40](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/g_value_expectations.py#L37-L40) —— 返回 \( 0.5 + 0.25(1-1/V) \)，并注明来自论文 **Corollary 27** 的均匀分布特例。

#### 4.2.4 代码实践

**实践目标**：用一个迷你 `update_scores` 复刻推导，验证 \( \mathbb{E}[(2-m)m] \) 的展开。

**操作步骤**（示例代码，纯 numpy 模拟「均匀 LM + 单层标准锦标赛」）：

```python
# 示例代码
import numpy as np

V = 1000
n_trials = 200_000
rng = np.random.default_rng(0)

vals = []
for _ in range(n_trials):
    g = rng.integers(0, 2, size=V)          # 每个 token 的 g 值 ~ Bernoulli(0.5)
    p = np.full(V, 1.0 / V)                 # 均匀 LM
    m = (g * p).sum()                       # g_mass = m
    new_p = p * (1 + g - m)                 # update_scores 的单层
    winner_g = rng.choice(g, p=new_p / new_p.sum())  # 从更新分布采样
    vals.append(winner_g)

emp = np.mean(vals)
from synthid_text import g_value_expectations as gve
print(f"经验均值={emp:.5f}  理论={gve.expected_mean_g_value(V, 2):.5f}")
```

**预期结果**：经验均值应落在 0.74975 附近（atol 取几个千分点内即可吻合）。

**需要观察的现象**：增大 `n_trials`，经验值向理论值收敛；这正是函数被当「标尺」的依据。

> 说明：上面是对「单层、均匀 LM」的最小模拟，省略了真实 `update_scores` 的 log 空间与多层循环，仅用于验证期望推导。

#### 4.2.5 小练习与答案

**练习**：为什么是 \( \mathbb{E}[(2-m)m] \) 而不是 \( (2-\mathbb{E}[m])\mathbb{E}[m] \)？二者差在哪一项？

**答案**：因为 \( m \) 本身是随机的，\( (2-m)m = 2m - m^2 \) 对 \( m \) 非线性，期望不能拆开。二者之差为 \( \mathbb{E}[m^2] - (\mathbb{E}[m])^2 = \mathrm{Var}(m) = 1/(4V) \)，正是有限词表带来的修正项——也是公式里「\( -1/(4V) \)」的来源。

---

### 4.3 num_leaves=3 的期望公式

#### 4.3.1 概念说明

当 `num_leaves=3`（或任意 `N>2`），`watermarked_call` 改走 `update_scores_distortionary`。它仍逐层守恒，但系数更通用：g=1 乘 \( \bigl(1-(1-m)^N\bigr)/m \)、g=0 乘 \( (1-m)^{N-1} \)。`num_leaves=3` 时这一套系数把期望推得更高（极限 0.875），代价是对 LM 分布的扭曲更大（见 u3-l3）。

#### 4.3.2 核心流程

仍设本层 g=1 总概率为 \( m \)。`update_scores_distortionary` 对 g=1 token 的系数为 \( \bigl(1-(1-m)^3\bigr)/m \)，故：

\[
\mathbb{E}[g_{\text{winner}}\mid m] = m\cdot\frac{1-(1-m)^3}{m} = 1-(1-m)^3.
\]

对 g 赋值取期望：注意 \( Y=1-m \) 与 \( m \) 同分布（因为 \( V-K \sim \mathrm{Binomial}(V,\tfrac12) \) 与 \( K \) 同分布），所以 \( \mathbb{E}[(1-m)^3]=\mathbb{E}[m^3] \)。需要三阶矩：

\[
\mathbb{E}[m^3] = \tfrac18 + \tfrac{3}{8V}.
\]

（它来自 \( K^3 = K^{\underline 3}+3K^{\underline 2}+K^{\underline 1} \) 对 \( K\sim\mathrm{Binomial}(V,\tfrac12) \) 取期望后除以 \( V^3 \)，整理得 \( (V^3+3V^2)/(8V^3) \)。）

于是

\[
\mathbb{E}[g_{\text{winner}}] = 1 - \mathbb{E}[m^3] = 1 - \left(\tfrac18+\tfrac{3}{8V}\right) = \tfrac78 - \tfrac{3}{8V}.
\]

\( V\to\infty \) 时收敛到 \( 7/8 = 1-(1/2)^3 \)，即「抽 3 个候选至少一个 g=1」的概率。

#### 4.3.3 源码精读

通用系数来自 `update_scores_distortionary`：

[src/synthid_text/logits_processing.py:L80-L81](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L80-L81) —— `coeff_not_in_g = (1-m)**(num_leaves-1)`、`coeff_in_g = (1-(1-m)**num_leaves)/m`，正是推导里用到的两个系数。

派发逻辑：

[src/synthid_text/logits_processing.py:L301-L304](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L301-L304) —— `_num_leaves != 2` 时调用 `update_scores_distortionary`，把 `self._num_leaves` 传入。

闭式结果：

[src/synthid_text/g_value_expectations.py:L41-L44](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/g_value_expectations.py#L41-L44) —— 返回 \( 7/8 - 3/(8V) \)，注明来自论文 **Theorem 25** 取 N=3、p_LM 均匀的特例。

#### 4.3.4 代码实践

**实践目标**：把 4.2 的模拟改成 N=3 的 distortionary 系数，复现 0.874625。

**操作步骤**（示例代码，替换 4.2 中的更新行）：

```python
# 示例代码（接 4.2，仅改 update 与采样逻辑）
V = 1000
N = 3
# g, p, m 同前
coeff_in  = (1 - (1 - m) ** N) / m
coeff_out = (1 - m) ** (N - 1)
new_p = p * np.where(g == 1, coeff_in, coeff_out)
winner_g = rng.choice(g, p=new_p / new_p.sum())
# 多次重复取均值，对比理论 7/8 - 3/(8V)
```

**预期结果**：经验均值落在 0.874625 附近。

**需要观察的现象**：同样的词表下，N=3 的经验均值系统性高于 N=2，且与理论吻合——印证「叶子数增大 → 期望 g 值升高」。

#### 4.3.5 小练习与答案

**练习**：N=3 公式里的修正项是 \( -3/(8V) \)，比 N=2 的 \( -1/(4V) \) 大。请用 \( \mathrm{Var}(m) \) 的语言解释为何 N 越大、有限词表修正「绝对值」越大。

**答案**：把期望写成 \( 1-\mathbb{E}[m^N] \)，修正来自 \( m \) 在其均值 \( 1/2 \) 附近的波动。N 越大，\( m^N \) 对波动越敏感（高次幂放大方差贡献），故有限词表下方差项的影响随 N 增大而增强，表现为修正项绝对值变大。

---

### 4.4 通用规律与论文补充材料的对应

#### 4.4.1 概念说明

把两节的结果并排放，能看出一条统一规律，这也是论文补充材料用更一般定理（Theorem 25 / Corollary 27）要表达的「单层锦标赛在均匀 LM 下的期望行为」。

#### 4.4.2 核心流程

对通用 N，由 `update_scores_distortionary` 的系数可证 \( \mathbb{E}[g_{\text{winner}}\mid m] = 1-(1-m)^N \)，再取期望得 \( 1-\mathbb{E}[m^N] \)。当 \( V\to\infty \)，\( m\to 1/2 \)，给出**通用极限**：

\[
\mathbb{E}[g_{\text{winner}}] \;\xrightarrow{V\to\infty}\; 1-\left(\tfrac12\right)^N.
\]

- N=2：\( 1-1/4 = 3/4 \)
- N=3：\( 1-1/8 = 7/8 \)

有限词表则给出一个负的修正项（随 \( 1/V \) 衰减）：

| num_leaves N | 闭式期望 | 论文出处 | \( V\to\infty \) 极限 |
| --- | --- | --- | --- |
| 2 | \( \tfrac12 + \tfrac14(1-\tfrac1V) \) | Corollary 27 | 0.75 |
| 3 | \( \tfrac78 - \tfrac{3}{8V} \) | Theorem 25 (N=3, 均匀) | 0.875 |

为什么 N 增大会抬高期望 g 值？因为极限 \( 1-(1/2)^N \) 随 N 单调递增——锦标赛叶子越多，「N 个候选里至少有一个 g=1」的概率越大，水印信号越强（但失真也越大，见 u3-l3）。代码只硬编码 N=2、3，是因为更高 N 的闭式高阶矩 \( \mathbb{E}[m^N] \) 越发繁琐、且工程上合理取值也仅 2 与 3，故其余取值直接 `ValueError`。

#### 4.4.3 源码精读

论文出处注释就在两条 `return` 上方，是本讲与论文的唯一直接对应点：

[src/synthid_text/g_value_expectations.py:L37-L44](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/g_value_expectations.py#L37-L44) —— 注释明确：N=2 对应 Corollary 27、N=3 对应 Theorem 25，且都强调「in the case where p_LM is uniform」，与本讲「均匀 LM」假设完全一致。

#### 4.4.4 代码实践

**实践目标**：用函数画出「期望 g 值 vs 词表大小」曲线，量化两条公式的差异。

**操作步骤**（示例代码）：

```python
# 示例代码
from synthid_text import g_value_expectations as gve

Vs = [16, 64, 256, 1024, 4096, 16384]
print(f"{'V':>8} {'N=2':>10} {'N=3':>10} {'极限2':>8} {'极限3':>8}")
for V in Vs:
    print(f"{V:>8} {gve.expected_mean_g_value(V,2):>10.6f} "
          f"{gve.expected_mean_g_value(V,3):>10.6f} {0.75:>8} {0.875:>8}")
```

**预期结果**：两列各自从略低于极限单调爬升并收敛；N=3 列恒高于 N=2 列约 0.12 以上。

**需要观察的现象**：词表越大，两列越贴近 0.75 / 0.875；这解释了为何真实大词表模型（Gemma、GPT-2）的经验均值非常接近这两个极限值。

#### 4.4.5 小练习与答案

**练习**：若想要一个比 0.75 更强、却仍只用 N=2 标准锦标赛的水印，单靠增大词表能做到吗？为什么？

**答案**：不能。N 固定为 2 时，极限就是 0.75，增大 V 只是让修正项 \( -1/(4V) \) 趋零、让经验值「逼近」0.75，无法突破。要抬升期望必须增大 `num_leaves`（如改用 N=3 走 distortionary 分支），代价是分布失真增大。

---

### 4.5 用理论值校验实现：对拍测试

#### 4.5.1 概念说明

闭式公式最大的工程价值是**当标尺**。如果水印施加实现正确，那么在「均匀 LM」条件下跑出来一大批 token、重算 g 值取均值，应当落在 `expected_mean_g_value` 给出的理论值附近——这就是仓库里 `does_mean_g_value_matches_theoretical` 辅助函数干的事。它把上一节的「理论」和真实 `watermarked_call` 的「经验」直接对拍。

#### 4.5.2 核心流程

测试按公式假设精确构造场景：

1. **造均匀 LM**：令 `scores = torch.ones(...)`，softmax 后每个 token 等概率——精确满足公式的均匀假设。
2. **跑真实水印**：调用 `logits_processor.watermarked_call`，再 `multinomial` 采样、用 `indices_mapping` 回映成稠密 token。
3. **重算 g 值取均值**：`compute_g_values(ngrams).mean(...)` 得到经验均值。
4. **与理论对拍**：用 `torch.isclose(经验, 理论, atol=..., rtol=0)` 判定是否吻合。

由于经验均值是蒙特卡洛估计，`batch_size` 必须足够大、`atol` 给一定容差，方差才能收敛到容差以内。

#### 4.5.3 源码精读

均匀 LM 的构造与对拍：

[src/synthid_text/logits_processing_test.py:L77-L81](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing_test.py#L77-L81) —— `scores = torch.ones((batch_size, vocab_size), ...)`，softmax 即均匀分布，精确落地公式的「uniform p_LM」假设。

经验均值与理论值的比较：

[src/synthid_text/logits_processing_test.py:L105-L119](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing_test.py#L105-L119) —— 对 `compute_g_values` 的输出取 `mean`，与 `g_value_expectations.expected_mean_g_value(vocab_size, num_leaves)` 用 `isclose(atol=atol, rtol=0)` 对拍，返回 `(经验, 理论, 是否吻合)` 三元组。

调用入口与说明（这是该对拍的封装）：

[src/synthid_text/logits_processing_test.py:L29-L54](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing_test.py#L29-L54) —— 函数名 `does_mean_g_value_matches_theoretical`，文档明确「Tests that the mean g-value is close to theoretical value」。

#### 4.5.4 代码实践

**实践目标**：通过阅读对拍逻辑，理解「理论值如何变成测试断言」。

**操作步骤**（源码阅读型实践，无需运行）：

1. 打开 `src/synthid_text/logits_processing_test.py`，定位 `does_mean_g_value_matches_theoretical`（L29 起）。
2. 跟踪三个量：`scores`（L77，全 1 = 均匀）、`mean_g_values`（L105，经验均值）、`expected_mean_g_value`（L107，理论值）。
3. 找到 `isclose(..., atol=atol, rtol=0)`（L110-L119），确认它用的是**绝对容差**而非相对容差——因为期望值本身在 0.75/0.875 量级，相对容差意义不大。

**需要观察的现象**：`vocab_size` 与 `num_leaves` 同时被传给真实处理器与理论函数（L66-L75、L107-L109），保证两侧在「同一个假设」下比较。

**预期结果**：你应当能用一句话复述——「全 1 scores 实现均匀 LM → 真实水印采样 → 重算 g 值均值 → 与 `expected_mean_g_value` 在 atol 内吻合，即判实现正确」。如果哪天改动了 `update_scores` 的系数却忘了同步本函数，这个测试就会失败。

#### 4.5.5 小练习与答案

**练习**：如果测试里把 `scores` 从全 1 改成「非均匀分布」，`isclose` 还应当成立吗？为什么？

**答案**：不应当。`expected_mean_g_value` 的全部推导都建立在「p_LM 均匀」上；一旦分布非均匀，\( m \) 不再等于 \( K/V \)、矩也不再是 \( 1/4+1/(4V) \) 等，闭式公式失效。此时经验均值会偏离理论值，测试反而会用「偏离」暴露「假设被破坏」——这正是该对拍只敢在均匀条件下运行的原因。

## 5. 综合实践

把本讲串起来做一个端到端的小验证（结合真实源码与闭式理论）：

1. 用 `expected_mean_g_value` 计算 **`vocab_size=1000`、`num_leaves=2` 和 `3`** 的理论期望（应分别得 **0.74975** 与 **0.874625**）。
2. 写一段最小蒙特卡洛（参考 4.2 / 4.3 的示例代码），在 `vocab_size=1000`、均匀 LM 下，分别用 N=2 的标准系数与 N=3 的 distortionary 系数跑大量 trial，取经验均值。
3. 把经验均值与第 1 步的理论值并排，确认二者在容差内吻合。
4. 回答：**为何 `num_leaves` 增大会提高期望 g 值？** 用本讲的统一极限 \( 1-(1/2)^N \) 解释——叶子越多，N 个候选里「至少一个 g=1」的概率越大，水印信号越强（代价是失真增大，见 u3-l3）。

> 提示：第 1 步是闭式手算，可立即给出确切数字；第 2 步若本地无环境，则标注「待本地验证」并只交付第 1、4 步的理论结论。

## 6. 本讲小结

- `expected_mean_g_value` 是纯 Python 的「理论标尺」，在均匀 LM、单层锦标赛、`Bernoulli(0.5)` g 值、`N=num_leaves` 四个假设下给出水印文本 g 值的期望。
- `num_leaves=2` 公式 \( \tfrac12+\tfrac14(1-\tfrac1V) \) 由 `update_scores` 的系数 \( (2-m) \) 推出，对应论文 **Corollary 27**，极限 0.75。
- `num_leaves=3` 公式 \( \tfrac78-\tfrac{3}{8V} \) 由 `update_scores_distortionary` 的系数推出，对应论文 **Theorem 25**（N=3 均匀特例），极限 0.875。
- 统一规律：\( \mathbb{E}=1-\mathbb{E}[m^N]\to 1-(1/2)^N \)，所以 N 越大期望越高；有限词表的负修正项随 \( 1/V \) 衰减。
- 工程价值：测试 `does_mean_g_value_matches_theoretical` 用「全 1 scores = 均匀 LM」精确落地假设，把经验 g 值均值与理论值 `isclose` 对拍，从而校验水印实现正确性。
- 全手册原则延续：函数只支持 N=2、3（其余 `ValueError`），与工程上合理取值一致；文档与源码冲突时以源码为准。

## 7. 下一步学习建议

- 下一讲 **u7-l2 测试套件如何验证水印正确性** 会把本讲的 `does_mean_g_value_matches_theoretical` 放进更大的测试图景，讲解 g 值均匀性测试、分布收敛性测试与 mixin 的 mock 测试，建议紧接着读。
- 若想深挖推导源头，可阅读论文 *SynthID Text*（Nature, 2024）的 Supplementary Information 中的 **Theorem 25** 与 **Corollary 27**，对照本讲的两条公式。
- 若关心「非均匀 LM」下的期望，可尝试把本讲的均匀假设换成真实模型分布，体会闭式公式为何失效、以及为何检测时必须改用经验/加权阈值（见 u5-l2 的阈值校准讨论）。
