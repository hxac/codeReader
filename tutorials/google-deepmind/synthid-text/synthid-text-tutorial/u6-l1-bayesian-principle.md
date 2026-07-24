# 贝叶斯检测原理：似然模型与后验

## 1. 本讲目标

上一讲 [u5-l2（Mean 与 Weighted Mean 打分）](./u5-l2-mean-scoring.md) 介绍了**免训练**的打分函数：把未屏蔽的 g 值做（加权）平均，看均值是否偏离 0.5。它简单到没有任何可学习参数。本讲跨入 SynthID Text 检测侧的另一条路线——**贝叶斯检测器（Bayesian detector）**，它**需要训练**，但通常能在相同假阳率下检出更多水印。

贝叶斯检测器位于 [`src/synthid_text/detector_bayesian.py`](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py) 中，用 **JAX / Flax** 实现。它的核心思想是用**贝叶斯公式**直接建模「给定 g 值，这段文本是水印文本的后验概率」\(P(w\mid g)\)。

学完本讲你应该能够：

1. 说清**水印似然模型** `LikelihoodModelWatermarked` 与**非水印似然模型** `LikelihoodModelUnwatermarked` 各自返回什么、为什么一个有可学习参数（`beta`/`delta`）而另一个对所有 g 值都返回 `0.5`。
2. 掌握潜变量 \(\psi\)（`p_two_unique_tokens`）的 **logistic 回归参数化**：\(\psi=\mathrm{sigmoid}(\Delta x+\beta)\)，以及它为何采用「只看更浅层 g 值」的自回归结构。
3. 推导后验 \(P(w\mid g)\) 是如何由「每个 g 值的相对惊奇度求和 + 先验对数几率」再套一层 `sigmoid` 得到的，并能手算一个极小例子。

> 本讲**只讲原理**（似然模型 + 潜变量 + 后验），刻意不碰训练循环、超参搜索与端到端 API——那是 [u6-l2](./u6-l2-bayesian-training-loop.md) 与 [u6-l3](./u6-l3-bayesian-data-and-api.md) 的内容。本讲聚焦 `detector_bayesian.py` 中「推理时打一次分」所经过的那条数学链路。

## 2. 前置知识

进入公式前，先用几句话把本讲会用到的概念拢一遍（细节见对应讲义）：

- **g 值**：一个 ngram + 一把水印密钥经哈希取出的一颗二进制比特，形状 `[batch, seq, depth]`，取值 0/1；`depth = len(keys)`，默认 30。见 [u2-l3](./u2-l3-g-values.md)。
- **掩码 mask**：形状 `[batch, seq]` 的 0/1 数组，标记哪些位置的 g 值可信（EOS 之后、重复上下文对应的 g 值要排除）。见 [u5-l1](./u5-l1-detection-masks.md)。
- **水印对 g 值的偏置**：施加阶段 `update_scores`（见 [u3-l3](./u3-l3-update-scores.md)）把概率质量从 g=0 系统性地推向 g=1。在标准锦标赛（`num_leaves=2`）下，水印文本的 g 值取 1 的概率约为 0.75、取 0 约为 0.25；而非水印文本的 g 值是无偏硬币，取 0/1 各 0.5。**这一条是本讲所有公式的物理出发点。**
- **mean_score 的局限**：它对每个未屏蔽 g 值**一视同仁**地求平均，无法区分「这个 g 值其实没承载水印信号」的位置。贝叶斯检测器正是来补这个缺口的——它学一套**因位置、因层而异**的权重。

最后补一条本讲专属的数学常识——**贝叶斯公式**。设 \(w\) 表示「文本是水印文本」，\(\neg w\) 表示「非水印」，\(g\) 表示观测到的全部 g 值。把后验写成**对数几率（log-odds）**形式最方便：

\[
\underbrace{\ln\frac{P(w\mid g)}{1-P(w\mid g)}}_{\text{后验对数几率}}
=
\underbrace{\ln\frac{P(w)}{1-P(w)}}_{\text{先验对数几率}}
+
\underbrace{\sum_{t,l}\ln\frac{P(g_{t,l}\mid w)}{P(g_{t,l}\mid \neg w)}}_{\text{相对惊奇度之和}}
\]

其中假设各 g 值在给定假设下条件独立，所以联合似然能拆成逐项之积、取对数后变成逐项之和。等式右边那一项，源码里叫 **relative surprisal（相对惊奇度）**：\(-\ln P(\cdot)\) 是「惊奇度（surprisal）」，故 \(\ln P(g\mid w)-\ln P(g\mid\neg w)\) 就是「在 \(\neg w\) 下比在 \(w\) 下多出的惊奇度」。它为正，说明这颗 g 值更像水印。

两边取 sigmoid 即得后验：

\[
P(w\mid g)=\mathrm{sigmoid}\!\left(\ln\frac{P(w)}{1-P(w)}+\sum_{t,l} m_t\,\ln\frac{P(g_{t,l}\mid w)}{P(g_{t,l}\mid \neg w)}\right)
\]

掩码 \(m_t\in\{0,1\}\) 把被屏蔽的位置整项置零。**上面这个式子就是 `_compute_posterior` 的全部数学。** 接下来的三节，分别讲式子里的两个似然 \(P(g\mid w)\)、\(P(g\mid\neg w)\) 怎么算（4.1、4.2），以及整条式子怎么组装（4.3）。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [`src/synthid_text/detector_bayesian.py`](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py) | 本讲唯一主角。重点行段：似然模型基类与两个子类（L209–L336）、`_compute_latents`（L264–L297）、`_compute_posterior`（L339–L382）、把它们串起来的 `BayesianDetectorModule.__call__`（L420–L441）。 |
| `src/synthid_text/logits_processing.py` | 上游来源：`compute_g_values` 产出 g 值，两个 `compute_*_mask` 产出掩码（见 u2-l3、u5-l1）。本讲只消费其输出，不再重复。 |

> 框架提示：与 [u5-l2](./u5-l2-mean-scoring.md) 一样，本文件顶部是 `import jax.numpy as jnp` 与 `import flax.linen as nn`，属于**检测侧的 JAX 世界**；文件里出现 `torch` 只用于上游数据预处理（本讲不涉及）。两侧通过 g 值这一「纯数值」桥梁衔接。

## 4. 核心概念与源码讲解

### 4.1 两个似然模型与 beta/delta 参数

#### 4.1.1 概念说明

贝叶斯公式里需要两个似然：\(P(g\mid w)\)（文本**是**水印时，观察到这样一批 g 值的概率）和 \(P(g\mid\neg w)\)（文本**不是**水印时，同样 g 值的概率）。源码把这两个分别封装成两个 Flax 模块，共享一个抽象基类 `LikelihoodModel`，约定子类实现 `__call__(g_values) -> likelihoods`，返回形状 `[batch, seq, depth]` 的逐颗似然（见基类 docstring，[detector_bayesian.py:209-226](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L209-L226)）。

两者的不对称是本节的核心：

- **`LikelihoodModelWatermarked`**：有**可学习参数** `beta` 和 `delta`，它要刻画「水印施加后 g 值究竟偏向成什么样」——这件事取决于模型、top_k、上下文等复杂因素，所以必须**从数据里学**。
- **`LikelihoodModelUnwatermarked`**：**没有任何可学习参数**，对所有 g 值一律返回 `0.5`。理由是：非水印 g 值是由 `accumulate_hash` 取某一位得到的（见 [u2-l2](./u2-l2-hashing-function.md)、[u2-l3](./u2-l3-g-values.md)），哈希结果在理论上是均匀的，所以每颗 g 值就是一枚**无偏硬币**，取 0/1 的概率严格各为 0.5。既然理论已经给定，就不必再学。

一句话：**所有的学习容量（`beta`、`delta`）都集中在水印似然这一侧**；非水印侧是一个「硬编码的常数 0.5」。`BayesianDetectorModule` 的 docstring 也点明了这一前提——本检测器**只适用于以 Bernoulli(0.5) 为 g 值分布的锦标赛水印**（[detector_bayesian.py:392-393](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L392-L393)）。

#### 4.1.2 核心流程：参数形状与初始化

`beta` 与 `delta` 在 `setup` 中声明（[detector_bayesian.py:238-259](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L238-L259)）：

| 参数 | 形状 | 初始化 | 作用 |
| --- | --- | --- | --- |
| `beta` | `[1, 1, depth]` | \(-2.5 + 0.001\cdot\mathcal{N}(0,1)\) | 每层一个**偏置**，作为 logistic 回归的截距项 |
| `delta` | `[1, 1, depth, depth]` | \(0.001\cdot\mathcal{N}(0,1)\) | 层与层之间的**权重矩阵**（自回归用，见 4.2） |

两点观察：

1. `beta` 初始化在 \(-2.5\) 附近，\(\mathrm{sigmoid}(-2.5)\approx 0.076\)——即训练开始时，模型认为「锦标赛里真有两个不同 token」的概率（也就是潜变量 \(\psi\)，下一节详讲）**很小**，水印似然几乎贴近 0.5 这个无信息基线，再由训练往上抬。
2. `delta` 初始化**接近 0**，且只有它会进入 L2 正则。`l2_loss` 把 `delta` 的所有元素平方求和（[detector_bayesian.py:261-262](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L261-L262)）：`einsum("ijkl->", delta**2)`。换言之，**正则只罚层间权重 `delta`，不罚偏置 `beta`**——这与「`beta` 决定每层基础偏置、`delta` 决定层间耦合」的分工一致：我们愿意让每层有一个稳定的整体偏置，但要把层间耦合压紧、防止过拟合（训练与 L2 的细节留给 [u6-l2](./u6-l2-bayesian-training-loop.md)）。

非水印侧则极简——`LikelihoodModelUnwatermarked.__call__` 一行实现：

```python
return 0.5 * jnp.ones_like(g_values)  # all g-values have prob 0.5.
```

它用 `ones_like` 造一个与 g 值同形状的全 1 数组再乘 0.5，保证返回形状仍是 `[batch, seq, depth]`、与水印侧逐元素对齐（[detector_bayesian.py:336](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L336)）。

#### 4.1.3 源码精读

两个参数的声明（[detector_bayesian.py:244-259](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L244-L259)）：

```python
self.beta = self.param(
    "beta",
    lambda *x: (
        -2.5 + 0.001 * noise(seed=0, shape=(1, 1, self.watermarking_depth))
    ),
)
self.delta = self.param(
    "delta",
    lambda *x: (
        0.001
        * noise(seed=0, shape=(1, 1, self.watermarking_depth, self.watermarking_depth))
    ),
)
```

- [第 244–249 行](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L244-L249)：`beta` 是 Flax 的 `self.param(name, init_fn)`，初始化函数返回 \(-2.5\) 加一点固定种子（`seed=0`）的小噪声，形状随 `watermarking_depth` 变化。
- [第 250–259 行](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L250-L259)：`delta` 是一个 `depth×depth` 的权重矩阵（外层 `[1,1]` 用于与 batch/seq 维广播），初始化为接近 0 的小噪声。

水印似然的最终组装在 `LikelihoodModelWatermarked.__call__`（[detector_bayesian.py:299-314](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L299-L314)）：

```python
p_one_unique_token, p_two_unique_tokens = self._compute_latents(g_values)
# P(g_tl | watermarked) is equal to
# 0.5 * [ (g_tl+0.5) * p_two_unique_tokens + p_one_unique_token].
return 0.5 * ((g_values + 0.5) * p_two_unique_tokens + p_one_unique_token)
```

`_compute_latents` 返回两个互补的概率（\(p_1+p_2=1\)），然后按注释里的混合公式给出 \(P(g_{t,l}\mid w)\)。这一步的数学含义放到 4.2 讲——那里会说明为什么是「\(g+0.5\)」这种写法，以及 \(\psi\) 到底从哪来。

非水印侧（[detector_bayesian.py:323-336](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L323-L336)），用 `@nn.compact` 内联定义、无任何参数：

```python
@nn.compact
def __call__(self, g_values: jnp.ndarray) -> jnp.ndarray:
  ...
  return 0.5 * jnp.ones_like(g_values)  # all g-values have prob 0.5.
```

两个模型都被 `BayesianDetectorModule.setup` 实例化（[detector_bayesian.py:413-417](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L413-L417)），在 `__call__` 里被一前一后调用（[detector_bayesian.py:437-438](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L437-L438)），各自产出一组逐颗似然，再交给 `_compute_posterior`。

#### 4.1.4 代码实践

**实践目标**：阅读源码并推理，回答本讲的核心问题——「为什么 `LikelihoodModelUnwatermarked` 对所有 g 值返回 0.5？这一假设在后验计算中起什么作用？」

**操作步骤（源码阅读型）**：

1. 打开 [detector_bayesian.py:317-336](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L317-L336)，确认 `LikelihoodModelUnwatermarked` 没有任何 `self.param`，返回值与输入 g 值的具体取值**无关**，只与其**形状**有关（`ones_like`）。
2. 回顾 [u2-l3](./u2-l3-g-values.md)：非水印 g 值由哈希取某一位得到，理论上是均匀的 Bernoulli(0.5)。这说明 `0.5` 不是拍脑袋，而是**哈希均匀性**的直接推论。
3. 跟到 [detector_bayesian.py:367](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L367) 的 `log_odds = log_likelihoods_watermarked - log_likelihoods_unwatermarked`：因为分母恒为 \(\ln 0.5\)，每颗 g 值的相对惊奇度化简为 \(\ln P(g\mid w)-\ln 0.5=\ln\bigl(2\,P(g\mid w)\bigr)\)。
4. （可选，**示例代码**）下面这段 JAX 代码示意如何把两个模型跑在同一批玩具 g 值上，观察「水印侧似然随 g 变、非水印侧恒为 0.5」：

```python
# 示例代码：仅示意调用方式，未在本讲运行；执行需安装 jax/flax
import jax.numpy as jnp
from synthid_text.detector_bayesian import (
    LikelihoodModelWatermarked, LikelihoodModelUnwatermarked,
)

depth = 4
g = jnp.array([[[1., 0., 1., 1.]]])  # [batch=1, seq=1, depth=4]
wm = LikelihoodModelWatermarked(watermarking_depth=depth)
# 真实使用时 params 由训练得到；此处仅看未训练的随机初始化行为（待本地验证）
uwm = LikelihoodModelUnwatermarked()
print(uwm(g))   # 期望: 形状同 g，元素全为 0.5
```

**需要观察的现象**：非水印模型的输出无论 `g` 怎么变，永远是全 0.5；水印模型的输出则会随 `g` 与训练好的参数变化。

**预期结果**：`uwm(g)` 形状与 `g` 相同、元素全为 `0.5`。水印侧的精确数值依赖参数（需训练），故标注「待本地验证」。

**它在后验里的作用（推理结论）**：把分母设成常数 0.5，等价于把「无信息基线」固定下来——每颗 g 值对后验的贡献完全由水印似然 \(P(g\mid w)\) 偏离 0.5 的程度决定。更妙的是：当某个位置的水印似然也等于 0.5（即模型认为该处没有真锦标赛，\(\psi=0\)，见 4.2）时，\(\ln(2\cdot 0.5)=\ln 1=0\)，这颗 g 值对后验**零贡献**。于是「常数 0.5」配合「可学的 \(\psi\)」共同实现了**按信息量自动加权**——这正是贝叶斯检测器相对 mean_score 的关键优势。

#### 4.1.5 小练习与答案

**练习 1**：既然非水印侧返回常数，那它岂不是对判别毫无用处，能否直接删掉、只算水印似然？

**参考答案**：不能删。贝叶斯公式是**比值** \(P(g\mid w)/P(g\mid\neg w)\)，分子分母同量纲才能比较。常数 0.5 提供了「无信息基线」：它把每颗 g 值的贡献归一成 \(\ln\bigl(2P(g\mid w)\bigr)\)，使得「\(P(g\mid w)=0.5\) ⇔ 零贡献」「\(P(g\mid w)>0.5\) ⇔ 正贡献」。删掉它，似然的绝对数值没有基准意义，后验也就无从校准。

**练习 2**：`l2_loss` 为什么只对 `delta` 求平方和、不含 `beta`？

**参考答案**：`beta` 是每层的整体偏置，刻画「该层 g 值平均偏水印的程度」，是检测所必需的稳定信号，不应被正则压向 0；`delta` 是层间耦合权重，自由度高、容易过拟合，所以用 L2 把它收紧。这与「截距不罚、斜率罚」的常见正则习惯一致。

### 4.2 _compute_latents：潜变量 psi 的 logistic 回归

#### 4.2.1 概念说明

水印似然 \(P(g\mid w)\) 并非直接拍一个数，而是先算一个**潜变量** \(\psi_{t,l}\)，再用它混合出似然。源码里这个潜变量叫 `p_two_unique_tokens`，其 docstring 给出物理含义（[detector_bayesian.py:264-279](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L264-L279)）：

> \(\psi_{t,l}=P(\text{第 }t\text{ 步、第 }l\text{ 层的锦标赛里恰好有两个不同的 token})\)

回忆 [u3-l3](./u3-l3-update-scores.md) 的锦标赛水印：`update_scores` 在 top_k 个候选上逐层打「比赛」，把概率推向 g=1。但只有当一场比赛里**真的有两个不同 token**对抗时，这个偏置才起作用；若候选退化成**同一个 token**（「one unique token」），就没有实质对抗，g 值退化成一枚无偏硬币（0.5）。

问题是：检测时**看不到**「当时到底有几个不同 token」——这是生成时的隐状态，最终的 token 序列里没有保留。于是模型把它当作**潜变量**，用观测到的 g 值去**推断**它的概率。这就是 `_compute_latents` 干的事。

这个推断被参数化为一个 **logistic 回归**（源码注释原话：`psi = sigmoid(delta * x + beta)`，见 [detector_bayesian.py:280-282](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L280-L282)），而特征 \(x\) 是**更浅层的 g 值**——也就是「自回归」结构：第 \(l\) 层的潜变量只由第 \(0,\dots,l-1\) 层的 g 值决定。

#### 4.2.2 核心流程

设 \(g_{t,0:d}\) 为位置 \(t\) 各层的 g 值，\(D=\)`watermarking_depth`。潜变量

\[
\psi_{t,l}=\mathrm{sigmoid}\!\left(\sum_{k<l}\Delta_{l,k}\,g_{t,k}+\beta_l\right),\qquad l=0,\dots,D-1
\]

- 求和下限是 \(k<l\)（**严格小于**），这正是自回归：第 \(l\) 层只看比自己更浅的层。\(l=0\) 时求和为空，\(\psi_{t,0}=\mathrm{sigmoid}(\beta_0)\) 仅由偏置决定。
- \(\Delta\) 即源码 `delta`（`[D,D]`），\(\beta\) 即 `beta`（`[D]`）。

拿到 \(\psi\) 后，`__call__` 用注释里的混合公式给出 \(P(g_{t,l}\mid w)\)（[detector_bayesian.py:312-314](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L312-L314)）：

\[
P(g_{t,l}\mid w)=\tfrac12\Big[(g_{t,l}+0.5)\,\psi_{t,l}+(1-\psi_{t,l})\Big]
\]

代入两种取值展开：

| 观测 \(g_{t,l}\) | \(P(g\mid w)\) | 含义 |
| --- | --- | --- |
| \(g=1\) | \(0.5+0.25\,\psi\in[0.5,\,0.75]\) | \(\psi=1\)（确有两 token）时为 **0.75**，与锦标赛理论期望一致；\(\psi=0\) 时退回 0.5 |
| \(g=0\) | \(0.5-0.25\,\psi\in[0.25,\,0.5]\) | \(\psi=1\) 时为 **0.25**；\(\psi=0\) 时退回 0.5 |

两者相加恰为 1（概率守恒）。这条公式实质是一个**两点混合**：以 \(\psi\) 的概率走「锦标赛偏置分布（0.75/0.25）」、以 \(1-\psi\) 的概率走「无偏硬币（0.5/0.5）」。而 \(\psi\) 本身又被 g 值**自回归地**推断出来——于是模型既学了「每层基础偏置 \(\beta\)」，也学了「层与层之间的相关性 \(\Delta\)」，从而把 mean_score 里「一把抓的平均」换成「因地制宜的加权」。

代码层面的三步：

1. **铺特征**：把 `[B, L, D]` 的 g 值沿新轴复制成 `[B, L, D, D]`，得到「为每一层 \(l\) 准备一份完整 g 向量作为特征」。
2. **下三角掩码**：用 `tril(x, k=-1)` 把对角线及以上的位置清零，仅保留 \(k<l\) 的项，落实自回归。
3. **logistic**：`einsum` 把 `delta` 与特征逐元素相乘并在最后一维求和，加 `beta`，得 logits `[B, L, D]`；`sigmoid` 得 \(\psi\) 与 \(1-\psi\)。

#### 4.2.3 源码精读

`_compute_latents` 全文（[detector_bayesian.py:264-297](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L264-L297)）：

```python
def _compute_latents(self, g_values):
  ...
  # Tile g-values to produce feature vectors for predicting the latents
  # for each layer in the tournament; our model for the latents psi is a
  # logistic regression model psi = sigmoid(delta * x + beta).
  x = jnp.repeat(
      jnp.expand_dims(g_values, axis=-2), self.watermarking_depth, axis=-2
  )  # [batch_size, seq_len, watermarking_depth, watermarking_depth]

  x = jnp.tril(
      x, k=-1
  )  # mask all elements above -1 diagonal for autoregressive factorization

  logits = (
      jnp.einsum("ijkl,ijkl->ijk", self.delta, x) + self.beta
  )  # [batch_size, seq_len, watermarking_depth]

  p_two_unique_tokens = jax.nn.sigmoid(logits)
  p_one_unique_token = 1 - p_two_unique_tokens
  return p_one_unique_token, p_two_unique_tokens
```

逐段说明：

- [第 283–285 行](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L283-L285)：`expand_dims(..., axis=-2)` 把 g 值从 `[B,L,D]` 变成 `[B,L,1,D]`，再 `repeat` 在倒数第二维复制 \(D\) 份，得到 `[B,L,D,D]`。此时 `x[i,t,l,k] = g_values[i,t,k]`——对每一层 \(l\) 都备好了同一份完整的 g 向量。
- [第 287–289 行](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L287-L289)：`tril(x, k=-1)` 保留**严格主对角线以下**的元素（即列号 \(k<\) 行号 \(l\)），其余置 0。这一步把「第 \(l\) 层只能看更浅层」的自回归约束硬编码进特征。注意 `k=-1` 是关键：它排除了 \(k=l\) 自己，避免层 \(l\) 直接拿自己的 g 值去预测自己的潜变量（那会造成信息泄露）。
- [第 291–293 行](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L291-L293)：`einsum("ijkl,ijkl->ijk", delta, x)` 把 `delta[B',L',D,D]`（前两维为 1，靠广播）与 `x` 在最后一维 \(k\) 上做内积，再加 `beta`，得 logits `[B,L,D]`。这正是 \(\sum_k \Delta_{l,k}x_{l,k}+\beta_l\)，且因 `x` 已被下三角清零，求和实际只在 \(k<l\) 上进行。
- [第 295–296 行](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L295-L296)：`sigmoid` 得 \(\psi=\)`p_two_unique_tokens`，\(1-\psi=\)`p_one_unique_token`，两者互补。

随后在 `__call__` 里混合成似然（[detector_bayesian.py:310-314](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L310-L314)）：

```python
p_one_unique_token, p_two_unique_tokens = self._compute_latents(g_values)
# P(g_tl | watermarked) is equal to
# 0.5 * [ (g_tl+0.5) * p_two_unique_tokens + p_one_unique_token].
return 0.5 * ((g_values + 0.5) * p_two_unique_tokens + p_one_unique_token)
```

把 \(g=1\) 代入：\(0.5[(1.5)\psi+(1-\psi)]=0.5+0.25\psi\)；把 \(g=0\) 代入：\(0.5[(0.5)\psi+(1-\psi)]=0.5-0.25\psi\)——正是上表的两行。

#### 4.2.4 代码实践

**实践目标**：手算验证「\(\psi\) 如何把无偏硬币（0.5/0.5）调和成锦标赛分布（0.75/0.25）」，并理解 \(\psi=0\) 的位置为何对后验无贡献。

**操作步骤（手算型）**：

1. 取一个位置 \(t\)、一层 \(l\)，设模型已学到 \(\psi_{t,l}=1\)（模型确信此处有两个不同 token）。
   - 若观测 \(g_{t,l}=1\)：\(P(g\mid w)=0.5+0.25\times 1=0.75\)。
   - 相对惊奇度 \(=\ln(0.75)-\ln(0.5)=\ln(1.5)\approx 0.405\)（正，推向水印）。
2. 同一位置若 \(\psi_{t,l}=0\)（模型认为此处退化成单一 token）：
   - 无论 \(g\) 取 0 或 1，\(P(g\mid w)=0.5\)。
   - 相对惊奇度 \(=\ln(0.5)-\ln(0.5)=0\)（零贡献，等价于被掩码）。
3. 把两步对比，写下结论。

**需要观察的现象**：\(\psi\) 像一个「信息量阀门」——越接近 1，该 g 值越能区分水印/非水印；\(\psi\) 趋近 0 时，该 g 值对后验的影响消失。

**预期结果**：\(\psi=1\) 且 \(g=1\) 时单颗贡献 \(\ln(1.5)\approx 0.405\)；\(\psi=0\) 时单颗贡献恒为 0。这说明模型通过学 \(\beta,\Delta\) 来调节 \(\psi\)，本质上是在学「**哪些位置、哪些层的 g 值才值得信**」——这正是相对 mean_score「全部等权平均」的改进点。

> 待本地验证：若想用代码确认，可在 [示例代码](#414-代码实践) 的基础上加载一个**训练好**的 `BayesianDetectorModule`，打印某条水印样本的 `p_two_unique_tokens`，观察其取值范围（通常显著大于 0、且逐层不同）。

#### 4.2.5 小练习与答案

**练习 1**：把 `tril(x, k=-1)` 改成 `tril(x, k=0)`（包含对角线）会有什么问题？

**参考答案**：那样第 \(l\) 层就能直接看到自己的 g 值 \(g_{t,l}\) 去预测自己的 \(\psi_{t,l}\)，形成**自我指涉**的信息泄露。直觉上，「这一层是否有两个不同 token」不应由「这一层自己抽出的 g 值」来决定，而应由更浅层（先发生的层）的 g 值来推断；`k=-1` 正是为了剔除自身、保证自回归的因果性。

**练习 2**：从 \(\psi=\mathrm{sigmoid}(\sum_{k<l}\Delta_{l,k}g_{t,k}+\beta_l)\) 出发，解释为何 \(l=0\)（最浅层）的 \(\psi\) 与 g 值无关。

**参考答案**：\(l=0\) 时求和区间 \(k<0\) 为空，求和项为 0，故 \(\psi_{t,0}=\mathrm{sigmoid}(\beta_0)\)，只依赖偏置 \(\beta_0\)，与任何 g 值都无关。最浅层没有「更浅层」可看，所以只能用一个全局常数偏置作为它「是否有两个 token」的先验估计。

### 4.3 _compute_posterior：从似然到后验 P(w|g)

#### 4.3.1 概念说明

有了两组逐颗似然 \(P(g\mid w)\)、\(P(g\mid\neg w)\)，最后一步就是把它们**汇总成一个 [0,1] 分数**——也就是开篇贝叶斯公式里的后验 \(P(w\mid g)\)。`_compute_posterior` 就是这道公式的直译：先取对数、做差得每颗 g 值的相对惊奇度，按掩码求和，加上先验对数几率，最后 `sigmoid`。

它之所以写成「对数几率 + sigmoid」而不是直接算 \(\frac{P(g\mid w)P(w)}{P(g\mid w)P(w)+P(g\mid\neg w)(1-P(w))}\)，是出于**数值稳定**：成百上千颗 g 值的似然相乘会下溢到 0，取对数后变成相加，量级可控；再套 sigmoid 把任意实数压回 (0,1)，且 sigmoid 正好是对数几率的反函数——\(\mathrm{sigmoid}(\text{logit})=P\)。

#### 4.3.2 核心流程

记两组似然 \(L^w_{t,l}=P(g_{t,l}\mid w)\)、\(L^{\neg w}_{t,l}=P(g_{t,l}\mid\neg w)\)，掩码 \(m_t\)，先验 \(p=P(w)\)。流程为：

1. **掩码升维**：把 `[B, L]` 的 mask 扩成 `[B, L, 1]`，以便在 depth 维上广播。
2. **先验裁剪**：\(p\leftarrow\mathrm{clip}(p,\,10^{-5},\,1-10^{-5})\)，避免 \(\ln 0\)。
3. **取对数 + 裁剪**：\(\ell^w=\ln(\mathrm{clip}(L^w,10^{-30},+\infty))\)，\(\ell^{\neg w}\) 同理。下界 \(10^{-30}\) 防止 \(\ln 0\)。
4. **逐颗相对惊奇度**：\(r_{t,l}=\ell^w_{t,l}-\ell^{\neg w}_{t,l}=\ln\frac{P(g_{t,l}\mid w)}{P(g_{t,l}\mid\neg w)}\)。
5. **掩码求和**：\(R_i=\sum_{t,l} m_t\,r_{t,l}\)，沿序列与深度两维压成 `[B]`。被屏蔽的位置（\(m_t=0\)）整项归零。
6. **先验对数几率**：\(R^{(0)}=\ln p-\ln(1-p)\)。
7. **合并 + sigmoid**：\(P(w\mid g)_i=\mathrm{sigmoid}\bigl(R^{(0)}+R_i\bigr)\)。

写成一条式子：

\[
P(w\mid g)_i=\mathrm{sigmoid}\!\left(\ln\frac{p}{1-p}+\sum_{t,l}m_t\,\ln\frac{P(g_{t,l}\mid w)}{P(g_{t,l}\mid\neg w)}\right)
\]

注意先验 \(p\) 本身也是一个**可学习参数**：在 `BayesianDetectorModule.setup` 里以 `self.prior = self.param("prior", lambda *x: self.baserate, (1,))` 声明，初值取 `baserate`（默认 0.5，[detector_bayesian.py:398](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L398) 与 [L418](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L418)）。默认 0.5 时先验对数几率为 0，后验完全由证据 \(R_i\) 决定。

#### 4.3.3 源码精读

`_compute_posterior` 全文（[detector_bayesian.py:339-382](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L339-L382)）：

```python
def _compute_posterior(likelihoods_watermarked, likelihoods_unwatermarked, mask, prior):
  mask = jnp.expand_dims(mask, -1)
  prior = jnp.clip(prior, a_min=1e-5, a_max=1 - 1e-5)
  log_likelihoods_watermarked = jnp.log(
      jnp.clip(likelihoods_watermarked, a_min=1e-30, a_max=float("inf"))
  )
  log_likelihoods_unwatermarked = jnp.log(
      jnp.clip(likelihoods_unwatermarked, a_min=1e-30, a_max=float("inf"))
  )
  log_odds = log_likelihoods_watermarked - log_likelihoods_unwatermarked

  # Sum relative surprisals (log odds) across all token positions and layers.
  relative_surprisal_likelihood = jnp.einsum(
      "i...->i", log_odds * mask
  )  # [batch_size].

  relative_surprisal_prior = jnp.log(prior) - jnp.log(1 - prior)

  # Combine prior and likelihood.
  relative_surprisal = (
      relative_surprisal_prior + relative_surprisal_likelihood
  )  # [batch_size]

  # Compute the posterior probability P(w|g) = sigmoid(relative_surprisal).
  return jax.nn.sigmoid(relative_surprisal)
```

逐行说明：

- [第 359 行](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L359)：`expand_dims(mask, -1)` 把掩码升成 `[B,L,1]`，使 `log_odds * mask`（后者形状 `[B,L,D]`）在 depth 维广播，被屏蔽序列位置的所有 \(D\) 颗 g 值同时归零。
- [第 360 行](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L360)：先验裁剪到 \([10^{-5},1-10^{-5}]\)，避免极端先验导致 \(\ln 0\) 或 \(\ln(1-p)\) 爆炸。
- [第 361–366 行](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L361-L366)：两组似然先 `clip` 下界到 \(10^{-30}\) 再取对数。这是纯数值保护：训练中似然若因参数走偏而极小，也不至于产生 \(-\infty\)。
- [第 367 行](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L367)：逐颗相对惊奇度 \(r_{t,l}=\ell^w_{t,l}-\ell^{\neg w}_{t,l}\)。由于 \(\ell^{\neg w}\) 恒为 \(\ln 0.5\)，这一步即 \(\ln\bigl(2\,P(g\mid w)\bigr)\)。
- [第 370–372 行](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L370-L372)：`einsum("i...->i", log_odds * mask)` 把 `[B,L,D]` 在序列、深度两维上**全部求和**，压成 `[B]`。这是把所有未屏蔽 g 值的相对惊奇度加总。
- [第 374 行](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L374)：先验对数几率 \(\ln p-\ln(1-p)\)。
- [第 377–379 行](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L377-L379)：先验与证据相加，得总对数几率。
- [第 382 行](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L382)：`sigmoid` 把对数几率压回概率，得 \(P(w\mid g)\)，形状 `[B]`。

最终的串联在 `BayesianDetectorModule.__call__`（[detector_bayesian.py:420-441](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L420-L441)）：先分别调用两个似然模型，再丢给 `_compute_posterior`：

```python
likelihoods_watermarked = self.likelihood_model_watermarked(g_values)
likelihoods_unwatermarked = self.likelihood_model_unwatermarked(g_values)
return _compute_posterior(
    likelihoods_watermarked, likelihoods_unwatermarked, mask, self.prior
)
```

至此，从 g 值到后验分数的完整推理链就闭合了：`g_values → _compute_latents(ψ) → P(g|w)`，配上常数 `P(g|¬w)=0.5`，进 `_compute_posterior` 求和 + sigmoid，输出 `[batch]` 的 \(P(w\mid g)\)。

#### 4.3.4 代码实践

**实践目标**：用一组「全是水印信号」的玩具 g 值，手算后验，体会「证据随 g 值数量线性累积、经 sigmoid 压回概率」。

**操作步骤（手算型）**：

设先验 \(p=0.5\)（先验对数几率为 0），并假设模型已学得每颗 g 值都有 \(\psi=1\)、且观测 \(g=1\)，于是 \(P(g\mid w)=0.75\)、\(P(g\mid\neg w)=0.5\)。

1. 单颗相对惊奇度：\(r=\ln(0.75)-\ln(0.5)=\ln(1.5)\approx 0.405\)。
2. 取 \(N=10\) 颗这样的 g 值（全部未屏蔽）：总证据 \(R=10\times 0.405=4.05\)。
3. 后验 \(=\mathrm{sigmoid}(0+4.05)\approx 0.983\)。
4. 再取 \(N=1\)：后验 \(=\mathrm{sigmoid}(0.405)\approx 0.600\)，对比感受「证据越多、后验越靠近 1」。

**需要观察的现象**：\(N\) 越大，总对数几率越大，sigmoid 输出越逼近 1；这与 [u5-l2](./u5-l2-mean-scoring.md) 里「文本越长、检测越可靠」的直觉一致——但贝叶斯版本是用**学到的似然比**而非朴素均值来累积证据。

**预期结果**：\(N=1\) 时后验约 0.60；\(N=10\) 时约 0.983。读者可用计算器或 `python -c "import math; print(1/(1+math.exp(-4.05)))"` 复核（\(\mathrm{sigmoid}(x)=1/(1+e^{-x})\)）。

> 待本地验证：若把上述手算用 JAX 跑一遍（构造全 1 的 g 值、人为把 `beta` 设为大正数使 \(\psi\to 1\)），`BayesianDetectorModule` 的输出应与手算接近；因涉及人为设参，精确数值留作本地实验。

#### 4.3.5 小练习与答案

**练习 1**：若某条样本的所有 g 值都被掩码（`mask` 全 0），后验会是多少？这合理吗？

**参考答案**：总证据 \(R=0\)，后验 \(=\mathrm{sigmoid}(\ln\frac{p}{1-p}+0)=p\)，即等于先验。合理——没有任何可观测证据时，只能回到先验信念；默认 \(p=0.5\) 时输出正好 0.5（「五五开」）。

**练习 2**：为什么先验要被裁剪到 \([10^{-5},1-10^{-5}]\)、似然要被裁剪到下界 \(10^{-30}\)？

**参考答案**：取对数时，若输入为 0 会得到 \(-\infty\)，使后续求和与 sigmoid 产生 `nan`/`inf`。裁剪先验避免 \(\ln p\) 或 \(\ln(1-p)\) 爆炸；裁剪似然下界避免某颗 g 值的 \(\ln P(g\mid\cdot)\) 跌到 \(-\infty\)。两处都是**数值稳定**保护，不改变正常区间的结果。

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这道「推理 + 手算」的综合任务，不要求运行代码。

**场景**：你拿到一段文本，重算得到 4 颗未屏蔽 g 值（深度 \(D=4\)，为简化假设它们都来自「确有两个 token」的位置，即 \(\psi=1\)）：\(g=[1,1,0,1]\)。先验 \(p=0.5\)。

**任务**：

1. 写出每颗 g 值的 \(P(g\mid w)\) 与 \(P(g\mid\neg w)\)。（提示：\(\psi=1\) 时 \(g=1\Rightarrow 0.75\)，\(g=0\Rightarrow 0.25\)；非水印侧恒 0.5。）
2. 算出每颗的相对惊奇度 \(r=\ln\frac{P(g\mid w)}{P(g\mid\neg w)}\)，并求和得总证据 \(R\)。
3. 写出后验 \(P(w\mid g)=\mathrm{sigmoid}(R)\)（先验对数几率为 0），并给出数值。
4. 把这个分数与「直接对这 4 颗 g 值做 mean_score」的结果对比，说明两者各自的含义差异。

**参考解答**：

1. \(P(g\mid w)=[0.75,0.75,0.25,0.75]\)；\(P(g\mid\neg w)=[0.5,0.5,0.5,0.5]\)。
2. 三颗 \(g=1\) 的 \(r=\ln(0.75/0.5)=\ln(1.5)\approx 0.405\)；一颗 \(g=0\) 的 \(r=\ln(0.25/0.5)=\ln(0.5)\approx -0.693\)。总证据 \(R\approx 3\times 0.405-0.693=0.522\)。
3. 后验 \(=\mathrm{sigmoid}(0.522)\approx 0.628\)。
4. mean_score 直接算均值 \(=(1+1+0+1)/4=0.75\)——它只反映「g 值偏 1 的程度」，且需要一个**外部阈值**才能判定；而贝叶斯分数 0.628 是**经过似然比加权、自带先验、可直接当概率读**的后验 \(P(w\mid g)\)。两者量纲与含义都不同：mean_score 是 g 值均值，贝叶斯分数是后验概率。

这道题把「似然模型（4.1）→ 潜变量 \(\psi\)（4.2，此处简化为 1）→ 后验（4.3）」完整走了一遍。

## 6. 本讲小结

- 贝叶斯检测器用贝叶斯公式直接建模后验 \(P(w\mid g)\)，相对 mean_score 的改进在于用**可学习的似然**取代「等权平均」。
- **两个似然模型不对称**：`LikelihoodModelWatermarked` 有可学参数 `beta`（每层偏置）与 `delta`（层间权重）；`LikelihoodModelUnwatermarked` 无参数、对所有 g 值返回常数 `0.5`，因为非水印 g 值在理论上是 Bernoulli(0.5)。
- **潜变量 \(\psi=\)`p_two_unique_tokens`** 用 logistic 回归 \(\mathrm{sigmoid}(\Delta x+\beta)\) 参数化，刻画「锦标赛里是否真有两个不同 token」；特征经 `tril(x, k=-1)` 做成**自回归**（第 \(l\) 层只看更浅层），故只有 `delta` 被 L2 正则。
- 水印似然 \(P(g\mid w)=0.5[(g+0.5)\psi+(1-\psi)]\) 是「锦标赛分布（0.75/0.25）」与「无偏硬币（0.5/0.5）」按 \(\psi\) 的混合；\(\psi=0\) 的位置对后验零贡献，实现了按信息量自动加权。
- `_compute_posterior` 是贝叶斯公式的对数几率直译：相对惊奇度按掩码求和、加先验对数几率、再 `sigmoid`；多处 `clip` 与对数空间计算都是为了数值稳定。
- 先验 \(p\) 本身也是可学习参数（默认 0.5），证据为空时后验退化为先验。

## 7. 下一步学习建议

本讲只完成了「**推理时如何打一次分**」的数学链路，并刻意把参数 \(\beta,\Delta,p\) 当成「已知」。这些参数从哪来？请继续：

- [u6-l2（训练循环：损失、优化与 TPR@FPR 验证）](./u6-l2-bayesian-training-loop.md)：看 `xentropy_loss`/`loss_fn` 如何用交叉熵 + L2 把 `BayesianDetectorModule` 训出来，以及为何用 `tpr_at_fpr` 作为验证指标去选最优 epoch。
- [u6-l3（数据处理与端到端检测 API）](./u6-l3-bayesian-data-and-api.md)：看 `process_raw_model_outputs` 如何把原始 token 序列处理成训练所需的 g 值/掩码/标签，以及 `BayesianDetector.train_best_detector → score` 的完整使用链路（注意：README 里的 `train_detector_bayesian.optimize_model` 在当前源码中并不存在，真实入口是 `BayesianDetector.train_best_detector`）。

若想回头巩固本讲用到的上游概念，可重温 [u2-l3（g 值）](./u2-l3-g-values.md)、[u3-l3（update_scores 锦标赛）](./u3-l3-update-scores.md)、[u5-l1（掩码体系）](./u5-l1-detection-masks.md) 与 [u5-l2（Mean 打分）](./u5-l2-mean-scoring.md)。
