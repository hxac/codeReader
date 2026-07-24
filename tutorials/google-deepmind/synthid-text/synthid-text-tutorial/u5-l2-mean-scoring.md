# Mean 与 Weighted Mean 打分

## 1. 本讲目标

本讲聚焦 SynthID Text 检测侧最简单、也最常用的一类打分函数：**Mean** 与 **Weighted Mean**。它们都位于 [`detector_mean.py`](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_mean.py) 中，**完全不需要训练**，只要拿到 g 值和掩码就能直接算出一个分数。

学完本讲你应该能够：

1. 写出 `mean_score` 的公式，并解释它本质就是「未屏蔽 g 值的算术平均」。
2. 说清 `weighted_mean_score` 的默认权重为何从 10 线性递减到 1、如何归一化，以及它相比 Mean 的改进点。
3. 理解「分数 → 判定」需要一个阈值，且阈值依赖文本 token 长度与目标假阳率（FPR）。
4. 能用随机模拟数据复现「水印文本分数偏高、非水印文本分数聚集在 0.5 附近」这一现象。

本讲承接 [u5-l1（检测所需的掩码体系）](./u5-l1-detection-masks.md)：上一讲我们得到 `combined_mask` 与 `g_values`，本讲就回答「拿这两个数组怎么算出一个 [0,1] 的分数」。

## 2. 前置知识

在进入公式前，先用三句话回顾几个本讲会反复用到的概念（细节见前置讲义）：

- **g 值**：一个 ngram + 一把水印密钥经哈希取出的一颗二进制比特，形状 `[batch, seq, depth]`，取值 0/1。`depth = len(keys)`，默认配置下为 30。见 [u2-l3](./u2-l3-g-values.md)。
- **掩码 mask**：形状 `[batch, seq]` 的 0/1 数组，标记哪些位置的 g 值「值得信任」（EOS 之后、重复上下文对应的 g 值要排除）。见 [u5-l1](./u5-l1-detection-masks.md)。
- **g 值的关键统计性质**：对**非水印**文本，g 值近似于抛硬币——以 0.5 的概率取 1，期望均值约为 0.5；对**水印**文本，施加阶段（`update_scores`）会把概率质量从 g=0 系统性地推向 g=1，使均值升高（`num_leaves=2` 时理论期望约为 0.75）。**水印检测的本质，就是看一大批 g 值的均值是否显著偏离 0.5。**

> 框架提示：`detector_mean.py` 用的是 **JAX**（`import jax.numpy as jnp`），与施加侧的 PyTorch 不同。两侧通过 g 值这一「纯数值」桥梁衔接——把 PyTensor 转成 numpy 喂进来即可（见 Notebook 中的 `.cpu().numpy()`）。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [`src/synthid_text/detector_mean.py`](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_mean.py) | 本讲主角，只含两个函数 `mean_score` 与 `weighted_mean_score`，共约 60 行。 |
| [`notebooks/synthid_text_huggingface_integration.ipynb`](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/notebooks/synthid_text_huggingface_integration.ipynb) | 端到端示例，cell「Get Mean detector scores」展示这两个函数的实际调用方式与阈值说明。 |
| `src/synthid_text/logits_processing.py` | 上游来源：`compute_g_values` 产出 g 值，`compute_eos_token_mask` / `compute_context_repetition_mask` 产出掩码（详见 u2-l3、u5-l1）。本讲只消费它们的输出，不再重复讲解。 |

记住一句话：**本讲的两个函数只做「加权求平均」这一件事**，所有「水印在哪里」的信息都已经被上游烘焙进了 g 值里。

## 4. 核心概念与源码讲解

### 4.1 mean_score：最朴素的均值检测

#### 4.1.1 概念说明

`mean_score` 是最直接的检测器：把一个样本里所有「未被屏蔽」的 g 值（横跨序列维 `seq` 与深度维 `depth`）全部加起来，除以这些 g 值的个数，得到一个 [0,1] 的分数。

直觉上：

- 非水印文本 → g 值像均匀硬币 → 均值 ≈ 0.5。
- 水印文本 → g 值被偏向 1 → 均值 ≈ 0.75（`num_leaves=2`）。

分数越高，越像水印。它之所以「不需要训练」，是因为它没有任何可学习参数——纯算术。

#### 4.1.2 核心流程

记 `D = watermarking_depth`（深度，即 g 值最后一维大小），`m_s ∈ {0,1}` 为序列位置 `s` 的掩码值，`g_{s,d}` 为该位置深度 `d` 的 g 值。则：

\[
\text{mean\_score} = \frac{\displaystyle\sum_{s,d} g_{s,d}\cdot m_s}{D \cdot \displaystyle\sum_{s} m_s}
\]

分母 `D · Σ m_s` 正是「未屏蔽 g 值的总个数」（每个未屏蔽位置贡献 `D` 个 g 值）。所以这本质上就是未屏蔽比特的**算术平均**。

代码层面的三步：

1. `num_unmasked = sum(mask, axis=1)`：沿序列维求和，得到每个样本未屏蔽的位置数 `[batch]`。
2. 把 `mask` 从 `[batch, seq]` 扩展成 `[batch, seq, 1]`，与 `g_values`（`[batch, seq, depth]`）相乘——掩码沿 depth 维广播，被屏蔽的位置整体归零。
3. 对 `(seq, depth)` 两个维度求和，除以 `D * num_unmasked`。

#### 4.1.3 源码精读

下面是 [`mean_score`](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_mean.py#L22-L41) 的完整实现（[detector_mean.py:22-41](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_mean.py#L22-L41)）：

```python
def mean_score(g_values, mask):
  watermarking_depth = g_values.shape[-1]            # D
  num_unmasked = jnp.sum(mask, axis=1)               # [batch]
  return jnp.sum(g_values * jnp.expand_dims(mask, 2), axis=(1, 2)) / (
      watermarking_depth * num_unmasked
  )
```

逐行说明：

- [第 37 行](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_mean.py#L37)：`watermarking_depth` 取自 g 值最后一维，默认配置下是 30。
- [第 38 行](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_mean.py#L38)：`num_unmasked` 统计每个样本里有多少个序列位置参与计算。
- [第 39-41 行](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_mean.py#L39-L41)：`jnp.expand_dims(mask, 2)` 把掩码变成 `[batch, seq, 1]`，在与 `g_values` 相乘时沿 depth 自动广播；`axis=(1,2)` 表示同时对序列与深度两维求和。

> **注意一个边界情况**：代码没有对 `num_unmasked == 0` 做保护。若某条样本的所有位置都被屏蔽，会出现除以零，结果为 `nan`/`inf`。正常使用中 `combined_mask` 至少会有若干个 1，但自己造数据时要留意。

#### 4.1.4 代码实践

**实践目标**：用一组极小的手算数据，验证 `mean_score` 确实是「未屏蔽 g 值的算术平均」，并观察掩码的作用。

**操作步骤**（示例代码，可在装好 `jax` 的环境运行；若未装 jax，把 `jnp` 换成 `numpy` 即可，逻辑一致）：

```python
# 示例代码：手工验证 mean_score
import jax.numpy as jnp
from synthid_text import detector_mean

# 1 个样本，3 个序列位置，depth=2
g_values = jnp.array([[[1., 0.],     # 位置0：将被屏蔽
                       [1., 1.],     # 位置1
                       [0., 1.]]])   # 位置2
mask = jnp.array([[0., 1., 1.]])     # 屏蔽位置0

print(detector_mean.mean_score(g_values, mask))
```

**需要观察的现象**：位置 0 被屏蔽后不参与计算；剩余 4 个 g 值为 `{1,1,0,1}`，和为 3，个数为 4。

**预期结果**：`3 / 4 = 0.75`。若输出不是 0.75，说明你对掩码广播的理解有偏差。

> 若你未本地运行，此结果可手算确认；JAX 数组不可变，传入的 `g_values` 不会被修改（详见 4.2.3 的说明）。

#### 4.1.5 小练习与答案

**练习 1**：若把上面 `mask` 改成 `[[1., 1., 1.]]`（不屏蔽任何位置），分数应是多少？

**答案**：全部 6 个 g 值 `{1,0,1,1,0,1}`，和为 4，个数 6 → `4/6 ≈ 0.667`。

**练习 2**：为什么非水印文本的 `mean_score` 会聚集在 0.5 附近，而不是 0 或 1？

**答案**：因为 g 值由哈希取出的一位比特构成（见 u2-l3 的 `get_gvals`），对非水印文本近似无偏，每个比特以约 0.5 的概率取 1；大量比特求平均后依大数定律收敛到 0.5。

---

### 4.2 weighted_mean_score：给深度方向加权

#### 4.2.1 概念说明

`mean_score` 把 depth 维上的每一层（每一把 key 对应的 g 值）同等看待。`weighted_mean_score` 则在 depth 方向上加一组权重，**让靠前的层（depth 索引小）权重更高、靠后的层权重更低**。

为什么这样能改进？Notebook 的注释给了经验性结论（详见 4.2.3 引用）：加权后水印样本的分数通常**更高**、与非水印样本的分离开得**更好**。其完整理论依据见论文及补充材料——本讲不展开推导，只讲清代码做了什么、权重长什么样。

#### 4.2.2 核心流程

记深度 `d` 上的权重为 `w_d`，加权均值为：

\[
\text{wmean\_score} = \frac{\displaystyle\sum_{s,d} w_d \cdot g_{s,d}\cdot m_s}{D \cdot \displaystyle\sum_{s} m_s}
\]

为了让结果仍与 `mean_score` 处在同一量纲，权重被归一化为 **「和等于 D」**（即平均权重为 1）：

\[
\sum_{d=0}^{D-1} w_d = D
\]

这样当所有 `w_d = 1` 时，`weighted_mean_score` 退化成 `mean_score`。

默认权重是一条从 10 线性递减到 1 的直线：

\[
w_d^{\text{raw}} = \text{linspace}(10,\ 1,\ D)_d
\]

随后乘以缩放因子 `D / sum(w_raw)` 完成归一化。以默认 `D=30` 为例：

- 原始直线从 10 到 1，共 30 个点，总和 `= (10+1)/2 × 30 = 165`。
- 缩放因子 `= 30 / 165 ≈ 0.1818`。
- 归一化后首权重 `≈ 1.818`，末权重 `≈ 0.182`，首尾比恰为 10:1，且总和 `= 30 = D`。

代码层面的四步：

1. 若调用方未传 `weights`，用 `jnp.linspace(10, 1, D)` 生成默认权重。
2. 归一化：`weights *= D / sum(weights)`，强制和为 D。
3. 把权重广播到 `[1, 1, D]`，逐元素乘到 `g_values` 上。
4. 其余与 `mean_score` 完全相同：掩码相乘 → 对 `(seq, depth)` 求和 → 除以 `D * num_unmasked`。

#### 4.2.3 源码精读

下面是 [`weighted_mean_score`](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_mean.py#L44-L77) 的实现（[detector_mean.py:44-77](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_mean.py#L44-L77)）：

```python
def weighted_mean_score(g_values, mask, weights=None):
  watermarking_depth = g_values.shape[-1]

  if weights is None:
    weights = jnp.linspace(start=10, stop=1, num=watermarking_depth)

  # Normalise weights so they sum to watermarking_depth.
  weights *= watermarking_depth / jnp.sum(weights)

  # Apply weights to g-values.
  g_values *= jnp.expand_dims(weights, axis=(0, 1))

  num_unmasked = jnp.sum(mask, axis=1)
  return jnp.sum(g_values * jnp.expand_dims(mask, 2), axis=(1, 2)) / (
      watermarking_depth * num_unmasked
  )
```

关键代码点：

- [第 65-66 行](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_mean.py#L65-L66)：未传 `weights` 时，默认从 10 线性递减到 1。
- [第 68-69 行](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_mean.py#L68-L69)：归一化，使 `sum(weights) == D`。
- [第 71-72 行](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_mean.py#L71-L72)：`jnp.expand_dims(weights, axis=(0,1))` 把权重变成 `[1, 1, D]`，沿 batch 与 seq 广播，仅在 depth 方向起作用——这正是「只对深度加权、不改变序列内权重」的关键。
- [第 74-77 行](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_mean.py#L74-L77)：与 `mean_score` 完全一致的聚合。

Notebook 中的实际调用见 [cell「Get Mean detector scores」](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/notebooks/synthid_text_huggingface_integration.ipynb#L655-L688)（约第 655-688 行）：分别对水印 / 非水印的 g 值调用 `mean_score` 与 `weighted_mean_score`，注释指出加权版本「通常分类性能更好、水印样本分数更高」。

> **两个易被忽略的细节**：
>
> 1. **JAX 数组不可变**：第 72 行的 `g_values *= ...` 看似在原地修改输入，但 JAX 数组是不可变的，Python 会把它处理成「局部变量重新绑定到一个新数组」，**不会**修改调用方传入的原始 `g_values`。这一点和 NumPy 的原地语义不同，放心复用同一份 g 值多次打分。
> 2. **掩码外的 g 值也会被加权**：第 72 行对整张 `g_values`（含被屏蔽位置）都乘了权重。但这不影响结果，因为第 75 行再次乘 `mask`，被屏蔽位置的贡献仍为 0。

#### 4.2.4 代码实践

**实践目标**：验证两件事——(a) 归一化后权重之和确实等于 `D`；(b) 当权重全为 1 时，`weighted_mean_score` 与 `mean_score` 输出完全相同。

**操作步骤**（示例代码）：

```python
# 示例代码：验证权重归一化与退化等价
import jax.numpy as jnp
from synthid_text import detector_mean

D = 30
seq = 50
g_values = jnp.array(np.random.randint(0, 2, size=(1, seq, D)).astype('float32'))
mask = jnp.ones((1, seq))

# (a) 默认权重归一化后的和
w = jnp.linspace(start=10, stop=1, num=D)
w = w * (D / jnp.sum(w))
print('sum(weights) =', float(jnp.sum(w)))          # 预期 30.0

# (b) 全 1 权重应与 mean_score 完全一致
uniform_w = jnp.ones(D)
print('mean      =', float(detector_mean.mean_score(g_values, mask)[0]))
print('wmean(1)  =', float(
    detector_mean.weighted_mean_score(g_values, mask, uniform_w)[0]))
```

**需要观察的现象**：`sum(weights)` 是否等于 30；两个分数是否相等。

**预期结果**：`sum(weights) ≈ 30.0`；`mean` 与 `wmean(1)` 完全相等（退化成立）。

> 若未本地运行，结论可由公式推出：`w_d ≡ 1` 时加权均值公式与均值公式逐项相同。

#### 4.2.5 小练习与答案

**练习 1**：默认权重为何要归一化成「和为 D」而不是「和为 1」？

**答案**：为了让分母 `D * num_unmasked` 仍然表示有效的 g 值个数，结果落在与 `mean_score` 相同的 [0,1] 量纲上、可直接比较。若归一化成和为 1，分数会整体缩小 D 倍，失去直观含义。

**练习 2**：如果不传 `weights`，把 `D` 从 30 改成 3，默认权重会变成什么？

**答案**：`linspace(10, 1, 3) = [10, 5.5, 1]`，总和 16.5，缩放因子 `3/16.5 ≈ 0.1818`，归一化后约为 `[1.818, 1.0, 0.182]`，仍是首尾比 10:1、总和为 3。

---

### 4.3 阈值与假阳率：从分数到判定

#### 4.3.1 概念说明

`mean_score` / `weighted_mean_score` 只给一个 [0,1] 的分数，**本身不做「是 / 否水印」的二分类**。要下结论，必须再选一个阈值 `τ`：分数 `≥ τ` 判为水印，反之判为非水印。

阈值的选择是一个**权衡（trade-off）**：

- `τ` 偏低 → 漏检少（召回高），但容易把人类写的文本误判为水印 → **假阳率（FPR）高**。
- `τ` 偏高 → 误判少，但可能漏掉水印文本 → 假阴率（FNR）高。

而且这个阈值**不是固定常数**，它依赖两件事：

1. **文本的 token 长度**：文本越长，参与平均的 g 值越多，分数分布越集中、方差越小，阈值可以定得更精细；文本越短，分数波动越大，水印与非水印的分数分布会重叠，很难选阈值。
2. **你想要的假阳率**：不同业务对「冤枉好人」的容忍度不同。

这正是 [README](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L20-L27) 第 20-27 行强调的：跨不同 token 长度使用 Weighted Mean 时，建议「在目标假阳率下、针对具体 token 长度，经验地或理论地计算阈值」，或采用论文附录 A.3.1 的加权频率派方法。

#### 4.3.2 核心流程

为什么 token 长度会影响阈值？可以做一个近似推导。在非水印假设下，把每个 g 值近似看作独立的 `Bernoulli(0.5)`，设参与平均的有效 g 值个数为 `N = num_unmasked × D`，则均值分数：

\[
\mathbb{E}[\text{score}] = 0.5, \qquad
\text{SD}[\text{score}] \approx \frac{0.5}{\sqrt{N}}
\]

（独立性是近似假设——g 值因哈希相关性与语言模型结构并非完全独立，但作为量级估计足够。）

举例：

- 长文本 `N = 100 × 30 = 3000` → SD ≈ `0.5/√3000 ≈ 0.009`，非水印分数紧紧贴在 0.5 附近；水印分数约 0.75。两者几乎不重叠，检测很容易。
- 短文本 `N = 10 × 30 = 300` → SD ≈ 0.029，分布变宽，与水印分布（中心 0.75）开始重叠，单一阈值难以兼顾 FPR 与召回。

**校准阈值的常规流程**：

1. 在目标 token 长度下，收集一批**已知非水印**文本，算出它们的分数，得到经验「零分布」。
2. 取该分布的 `(1 − FPR)` 分位数作为阈值 `τ`。例如想要 FPR = 0.1%，就取 99.9% 分位数。
3. 用该校准好的 `τ` 对新文本判定；并可用一批水印文本评估在此 `τ` 下的召回（TPR）。

#### 4.3.3 源码精读

阈值逻辑**不在 `detector_mean.py` 内**——源码只负责吐分数。阈值说明出现在 Notebook 与 README：

- [Notebook cell「Get Mean detector scores」注释](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/notebooks/synthid_text_huggingface_integration.ipynb#L655-L661)（第 655-661 行）原文：「To classify responses you can set a score threshold, but this will depend on the distribution of scores for your use-case and your desired false positive / false negative rates.」
- [README 第 20-27 行](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L20-L27)：建议针对具体 token 长度、在目标假阳率下经验/理论计算阈值，或用附录 A.3.1 的加权频率派方法。

调用方拿到分数后自行决定阈值，例如 Notebook 里就是直接 `print` 出两组分数让人**肉眼对比**，并未硬编码阈值（[第 669-670 行](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/notebooks/synthid_text_huggingface_integration.ipynb#L669-L670)）。

> 这也呼应了 u1-l1 的边界说明：SynthID Text 是参考实现，阈值与误检率的最终确定需要使用者结合自己的数据分布来完成。

#### 4.3.4 代码实践

**实践目标**：用随机模拟复现「token 长度影响阈值」——在 FPR=1% 下，对长短两种文本分别校准阈值，并观察短文本的检测更难。

**操作步骤**（示例代码，用 `numpy` 模拟即可，无需模型）：

```python
# 示例代码：模拟校准阈值（非项目原有代码，仅供理解）
import numpy as np

D = 30

def sample_scores(num_tokens, p, n=20000):
    # p=0.50 模拟非水印；p=0.75 模拟水印(num_leaves=2)
    g = np.random.binomial(1, p, size=(n, num_tokens, D)).astype('float32')
    mask = np.ones((n, num_tokens))
    # 等价于 mean_score：未屏蔽 g 值的算术平均
    return (g * mask[..., None]).sum(axis=(1, 2)) / (D * num_tokens)

for num_tokens in [100, 10]:
    null = sample_scores(num_tokens, 0.50)   # 非水印分布
    wm   = sample_scores(num_tokens, 0.75)   # 水印分布
    tau  = np.quantile(null, 0.99)           # FPR = 1% 的阈值
    tpr  = (wm >= tau).mean()                # 该阈值下的召回
    print(f'tokens={num_tokens:3d}  tau={tau:.4f}  TPR={tpr:.3f}')
```

**需要观察的现象**：两种长度下的阈值 `tau` 与召回 `TPR` 的差异。

**预期结果**：`tokens=100` 时 `TPR` 接近 1.0（长文本易检）；`tokens=10` 时 `TPR` 明显下降（短文本分布重叠、漏检增多）。具体数值**待本地验证**（依赖随机种子），但「短文本召回更低」这一趋势是确定的。

#### 4.3.5 小练习与答案

**练习 1**：为什么不能给所有文本统一固定一个阈值（比如 0.6）？

**答案**：因为分数的方差与 token 长度强相关。短文本非水印分数也可能偶然超过 0.6 造成误判，长文本水印分数则几乎必然高于 0.6。固定阈值无法在不同长度下同时满足目标 FPR。

**练习 2**：在 FPR=1% 下，把阈值取成非水印分数分布的哪个分位数？

**答案**：第 99 个百分位数（`np.quantile(null, 0.99)`），即让只有 1% 的非水印样本超过该阈值。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「免训练检测器」的端到端模拟评估。

**任务**：用随机数据对比 `mean_score` 与 `weighted_mean_score` 在水印 / 非水印样本上的分数分布差异，并各自在 FPR=1% 下校准阈值、比较召回。

**操作步骤**（示例代码）：

```python
# 示例代码：综合实践（非项目原有代码，用 numpy 模拟）
import numpy as np
import jax.numpy as jnp
from synthid_text import detector_mean

D, num_tokens, n = 30, 50, 20000

def make(p):
    g = np.random.binomial(1, p, size=(n, num_tokens, D)).astype('float32')
    m = np.ones((n, num_tokens))
    return jnp.asarray(g), jnp.asarray(m)

null_g, mask = make(0.50)   # 非水印
wm_g,   _    = make(0.75)   # 水印(num_leaves=2)

for name, fn in [('Mean', detector_mean.mean_score),
                 ('WeightedMean', detector_mean.weighted_mean_score)]:
    null_s = np.asarray(fn(null_g, mask))
    wm_s   = np.asarray(fn(wm_g,   mask))
    tau = np.quantile(null_s, 0.99)          # FPR=1% 阈值
    tpr = (wm_s >= tau).mean()
    print(f'{name:14s} null_mean={null_s.mean():.4f} '
          f'wm_mean={wm_s.mean():.4f} tau={tau:.4f} TPR={tpr:.3f}')
```

**需要观察的现象**：

1. 两种方法下，非水印均值都接近 0.5、水印均值都明显高于 0.5。
2. Weighted Mean 的水印均值是否比 Mean 更高（更易分离）。
3. 在同一 FPR 下，哪种方法的 TPR 更高。

**预期结果**：两组 `null_mean ≈ 0.50`、`wm_mean ≈ 0.75`（量级）；Weighted Mean 通常给出略高的水印分数与略高的 TPR——这与 Notebook 注释的经验结论一致。具体数值**待本地验证**。

> 想用**真实** g 值跑一遍？回到主线 Notebook 的 cell「Get Mean detector scores」（[第 655-688 行](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/notebooks/synthid_text_huggingface_integration.ipynb#L655-L688)），它对真实的 GPT-2 / Gemma 输出调用这两个函数并打印分数，是最权威的参照。

## 6. 本讲小结

- `mean_score` 就是「未屏蔽 g 值（跨 seq 与 depth）的算术平均」，公式为 `sum(g·mask) / (D·num_unmasked)`，无任何可学习参数。
- 非水印文本均值聚集在 0.5 附近，水印文本均值升高（`num_leaves=2` 时约 0.75）——检测的本质是看均值是否显著偏离 0.5。
- `weighted_mean_score` 在 depth 方向加一条默认从 10 递减到 1 的线性权重，并归一化为「和等于 D」，使结果与 Mean 同量纲；权重全为 1 时退化为 Mean。
- 加权只在 depth 方向起作用（`expand_dims` 到 `[1,1,D]`），序列内不加权；JAX 不可变，不会修改调用方传入的 g 值。
- 分数本身不做二分类，需自选阈值；阈值依赖 token 长度（长文本方差小、易检）与目标假阳率，README 建议按长度与 FPR 经验/理论校准。
- 这两个函数是「免训练」路线的全部；若需更强检测能力，下一步进入需训练的贝叶斯检测器。

## 7. 下一步学习建议

- **进入贝叶斯检测器**：继续学习 [u6-l1（贝叶斯检测原理）](./u6-l1-bayesian-principle.md)，看 `detector_bayesian.py` 如何用似然模型与后验把同样的 g 值变成更强的检测分数（代价是需要按密钥训练）。
- **回顾 g 值来源**：若对「水印为何让均值变成 0.75」还存疑，可回到 [u3-l3（得分更新：锦标赛与 distortionary 变体）](./u3-l3-update-scores.md) 复习施加侧如何逐层把概率推向 g=1。
- **理论校验**：[u7-l1（理论期望值）](./u7-l1-theoretical-expectations.md) 会给出 `expected_mean_g_value`，可用它核对本讲假设的 0.5 / 0.75 这两个理论锚点是否自洽。
- **源码阅读建议**：通读 [`detector_mean.py`](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_mean.py) 全文（不足 80 行），它是最适合作为「JAX 检测函数」入门的微型样本。
