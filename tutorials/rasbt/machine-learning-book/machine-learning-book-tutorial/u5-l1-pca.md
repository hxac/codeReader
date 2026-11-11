# u5-l1 PCA 主成分分析五步流程

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 PCA（主成分分析）要解决什么问题：把高维特征压缩到低维，同时尽量保留数据中的方差信息。
2. 背出并理解 PCA 的五步流程：**标准化 → 协方差矩阵 → 特征分解 → 按特征值排序取前 k 个 → 投影**，并把每一步对应到 `ch05.py` 中的真实代码。
3. 区分 sklearn PCA 的两个属性：`explained_variance_`（各主成分方向的方差，即特征值）与 `explained_variance_ratio_`（占总方差的比例），并会用累计方差图选择保留几个主成分。
4. 牢记工程原则：**PCA 只在训练集上拟合（学均值、方向），测试集只做变换**，防止数据泄漏。
5. 不借助 sklearn，只用 numpy 完整实现一遍 PCA，并验证与 sklearn 的结果一致（最多差一个正负号）。

## 2. 前置知识

本讲用到的概念在前几讲大多出现过，这里快速回顾并补充新概念：

- **降维（dimensionality reduction）**：wine 数据集有 13 个特征（第 4 章已用过），但人眼只能画二维散点图，而且特征太多会加剧过拟合与维数灾难（u4-l2）。降维就是把 \( n \) 个特征压缩成 \( k \) 个（\( k < n \)）新特征，同时尽量少丢信息。
- **无监督学习**：PCA 只用特征矩阵 `X`，**完全不用类别标签 `y`**。它保留信息的标准是「投影后的方差尽可能大」，与样本属于哪一类无关。这一点与下一讲的 LDA（监督降维）形成对比。
- **标准化（z-score standardization）**：每列减均值、除标准差（u2-l3、u3-l1 已讲）。PCA 之前必须标准化，因为 PCA 基于方差，而 wine 各特征的量纲差异巨大（酒精约 13 左右、脯氨酸约 1000 左右），不标准化的话方差大的特征会独占第一主成分。
- **协方差（covariance）**：衡量两个变量一起变化的程度。两个变量都大于（或小于）各自均值时贡献正协方差，一增一减贡献负协方差。13 个特征两两组合得到一个 13×13 的**协方差矩阵**，对角线是各特征的方差。
- **特征值与特征向量（eigenvalue / eigenvector）**：对矩阵 \( \Sigma \)，若存在非零向量 \( v \) 满足

  \[ \Sigma v = \lambda v \]

  则 \( v \) 是特征向量（方向在变换下不变），\( \lambda \) 是对应特征值（沿该方向被拉伸的倍数）。对协方差矩阵来说，特征值恰好等于数据沿对应特征向量方向投影后的**方差**。PCA 就是挑特征值最大的那些方向。
- **投影（projection）**：把样本点沿某个方向做内积，得到它在该方向上的坐标。数据矩阵右乘一个 \( n \times k \) 的矩阵 \( W \)（每列是一个方向），就一次性把所有样本变换到 k 维新坐标系。
- **train/test 划分**：沿用第 4 章的做法，wine 的 178 个样本按 70/30 分层划分，PCA 的「拟合」只在 124 个训练样本上进行。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ch05/ch05.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py) | 第 5 章全部代码的脚本形式。本讲只关注前 400 余行：PCA 手写实现（约 L87–L268）与 sklearn PCA（约 L272–L423）。后面的 LDA 与 t-SNE 留给下一讲 |
| [ch05/ch05.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.ipynb) | 同一内容的 notebook 形式，适合逐单元格交互实验（双轨工作流见 u1-l3） |
| [ch05/wine.data](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/wine.data) | wine 数据集本地副本：无表头 CSV、178 行，首列是类别标签（1/2/3），后 13 列是特征。书中从 UCI 在线读取，网络不可用时可用它（代码中有注释说明，见 L91–L95） |
| [ch05/wine.names.txt](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/wine.names.txt) | 数据集的原始字段说明文档 |
| [ch05/README.md](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/README.md) | 本章大纲（PCA → LDA → t-SNE 三大主题），运行方式见 ch01 的 README |

## 4. 核心概念与源码讲解

### 4.1 PCA 的动机与五步流程总览

#### 4.1.1 概念说明

想象把 13 维的 wine 数据「拍扁」成二维平面上的一张图。拍扁的方式有无数种：随便选两列原始特征画散点图是一种（u1-l3 对鸢尾花就是这么做的），但它只能看到 13 维中的 2 维。

PCA 的思路不同：**不挑原始特征，而是把 13 个特征线性组合成 13 个新坐标轴（主成分），然后只保留「拍扁后损失最小」的前 k 个**。「损失最小」有一个精确等价的说法：

- 第一主成分是使数据投影后方差最大的方向；
- 第二主成分在与第一主成分**正交**的前提下再取方差最大的方向；
- 依此类推。

方差大 = 样本在这个方向上散得开 = 信息多。数学上可以证明，这组方向恰好是协方差矩阵特征值最大的那些特征向量——所以 PCA 的全部计算都围绕「协方差矩阵 + 特征分解」展开。

为什么 PCA 对 wine 有用？因为 13 个特征里很多高度相关（比如「总酚」和「类黄酮」），信息是冗余的。PCA 重新组织坐标系后，往往前两三个主成分就能装下一半以上的方差，聚类结构在二维图上一目了然。

#### 4.1.2 核心流程

PCA 五步法（本讲的主线）：

```text
输入: 训练特征矩阵 X_train (124 × 13)
1. 标准化        X_std = (X_train - mean) / std        → 每列均值 0、方差≈1
2. 协方差矩阵    Σ = np.cov(X_std.T)                    → 13 × 13 对称矩阵
3. 特征分解      Σ 的特征值 λ 与特征向量 v               → Σv = λv
4. 排序取前 k    特征值从大到小排序，取前 k 个特征向量拼成 W (13 × k)
5. 投影          X_pca = X_std · W                      → 124 × k 的新特征
输出: 低维特征 X_pca 与变换矩阵 W（可复用于测试集）
```

对应的 sklearn 用法只有两行：`pca = PCA(n_components=2)` + `pca.fit_transform(X_train_std)`，但它内部做的正是上面五步（用更数值稳定的 SVD 实现）。

#### 4.1.3 源码精读

PCA 的输入数据与第 4 章完全一致，`ch05.py` 重新加载了一遍 wine 数据集：

- [ch05/ch05.py:L87-L101](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L87-L101)：从 UCI 在线读入无表头的 `wine.data`，并手工指定 14 个列名（首列 `Class label`，后 13 列是化学特征）。网络不可用时，[L91-L95](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L91-L95) 的注释给出等价的本地路径写法 `pd.read_csv('wine.data', header=None)`，仓库里恰好带了这份文件。
- [ch05/ch05.py:L112-L116](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L112-L116)：切出 `X`（178×13）与 `y`（类别 1/2/3），再做 70/30 分层划分（`stratify=y, random_state=0`），得到 124 个训练样本、54 个测试样本。

值得专门观察的一点：**在整段 PCA 代码里（L124–L268），`y_train` 从未参与任何计算**，它只在最后画散点图时用来给不同类别的点染不同颜色（L258）。这就是「无监督」的具体含义——你可以去掉 `y` 重新运行，前两个主成分一个数都不会变。

#### 4.1.4 代码实践

**实践目标**：确认数据准备环节的形状与类别分布，并亲眼验证「PCA 不用标签」。

**操作步骤**（示例代码，在 `ch05/` 目录下运行）：

```python
# 示例代码
import pandas as pd
from sklearn.model_selection import train_test_split

df_wine = pd.read_csv('wine.data', header=None)
df_wine.columns = ['Class label', 'Alcohol', 'Malic acid', 'Ash',
                   'Alcalinity of ash', 'Magnesium', 'Total phenols',
                   'Flavanoids', 'Nonflavanoid phenols', 'Proanthocyanins',
                   'Color intensity', 'Hue',
                   'OD280/OD315 of diluted wines', 'Proline']
X, y = df_wine.iloc[:, 1:].values, df_wine.iloc[:, 0].values
X_train, X_test, y_train, y_test = train_test_split(
    X, y, test_size=0.3, stratify=y, random_state=0)
print(X_train.shape, X_test.shape)      # (124, 13) (54, 13)
print(pd.Series(y_train).value_counts())  # 三个类别的训练样本数
```

**需要观察的现象**：训练集 124 行、测试集 54 行；三个类别大致按 7:3 等比例分布。

**预期结果**：`X_train.shape == (124, 13)`；类别计数之和为 124。（具体每个类别几个样本，待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：PCA 是监督学习还是无监督学习？依据是什么？

**答案**：无监督。它只依赖特征矩阵 `X` 的二阶统计量（协方差），优化目标是投影方差最大，全程不用标签 `y`；`ch05.py` 中 `y_train` 只在可视化时给点上色。

**练习 2**：wine 有 13 个特征，PCA 最多能得到几个主成分？

**答案**：13 个。主成分个数不超过 `min(特征数, 样本数−1)`；这里 13 < 123，所以最多 13 个非零方差方向。sklearn 中 `PCA(n_components=None)`（默认）即保留全部 13 个（见 [L380-L382](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L380-L382)）。

**练习 3**：为什么说 PCA 是「有损压缩」？

**答案**：只保留前 k 个主成分意味着丢弃其余 13−k 个方向上的投影坐标，这部分方差（信息）永久丢失，一般无法从 k 维表示精确还原 13 维原始数据。保留比例恰好可以用 `explained_variance_ratio_` 之和来度量。

---

### 4.2 第 1 步：标准化——协方差矩阵的前提

#### 4.2.1 概念说明

PCA 用「方差」度量信息量，而方差带量纲。wine 的 13 个特征量纲悬殊：酒精含量约 13、镁约 100、脯氨酸约 1000。不标准化时，脯氨酸一列的方差会比其他列大几个数量级，协方差矩阵被它主导，第一主成分几乎退化成「脯氨酸方向」——方差解释率虚高，但对区分酒的类型没有帮助。

标准化把每列变成：

\[ x' = \frac{x - \mu}{\sigma} \]

变换后每列均值 0、方差约 1，所有特征在同一尺度上公平竞争，协方差矩阵（此时与相关系数矩阵成比例）才能真正反映特征之间的**相关结构**。

另外要重申 u3-l1 讲过的泄漏问题：**均值和标准差只能在训练集上估计**。测试集代表「未来的新数据」，必须用训练集学到的参数去变换它。

#### 4.2.2 核心流程

```text
sc = StandardScaler()
X_train_std = sc.fit_transform(X_train)   # 在训练集上学 μ、σ 并变换
X_test_std  = sc.transform(X_test)        # 只变换，参数来自训练集
```

`fit` 学参数、`transform` 用参数——这正是 scikit-learn 估计器的统一范式（u3-l1），PCA 本身也会遵循同样的 fit/transform 纪律。

#### 4.2.3 源码精读

- [ch05/ch05.py:L124-L126](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L124-L126)：训练集 `fit_transform`、测试集只 `transform`——正确的写法。
- [ch05/ch05.py:L129-L171](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L129-L171)：作者塞进代码里的一条长注记（Note）：他承认自己曾把测试集写成 `sc.fit_transform(X_test)`，并用一个「厘米长度分类」的小例子说明危害——如果对新数据重新计算均值方差，5/6/7 厘米这种远小于训练集（10~30 厘米）的新样本会被变换回和训练样本差不多的数值，掩盖了「它们明显更小」这一关键信息。这段注记值得完整读一遍。

#### 4.2.4 代码实践

**实践目标**：亲手验证「标准化之后，协方差的对角线≈1」这一前提。

**操作步骤**（示例代码，紧接 4.1.4 的数据准备）：

```python
# 示例代码
from sklearn.preprocessing import StandardScaler
import numpy as np

sc = StandardScaler()
X_train_std = sc.fit_transform(X_train)

print('各列均值(应≈0):', np.round(X_train_std.mean(axis=0)[:4]))
print('各列标准差(应≈1):', np.round(X_train_std.std(axis=0)[:4]))

cov_mat = np.cov(X_train_std.T)          # 第 2 步在这里一并完成
print('协方差矩阵形状:', cov_mat.shape)   # (13, 13)
print('对角线(各特征方差, 应≈1):', np.round(np.diag(cov_mat)[:4], 4))
print('矩阵迹(总方差):', round(np.trace(cov_mat), 4))
```

**需要观察的现象**：均值为 0、标准差为 1；协方差矩阵是 13×13 的对称矩阵。

**预期结果**：`np.trace(cov_mat)` 约等于 12.9 而不是严格的 13。原因是一个有趣的细节：`StandardScaler` 用 n 做分母计算标准差，而 `np.cov` 用 n−1 做分母，两者相差因子 (n−1)/n = 123/124 ≈ 0.992，故总方差 ≈ 13 × 0.992 ≈ 12.9。（精确数值待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：跳过标准化直接对 wine 做 PCA，会发生什么？

**答案**：方差量级巨大的特征（如 Proline，数值上千）会支配第一主成分，`explained_variance_ratio_` 的第一项看起来很高，但这个方向基本只是「脯氨酸轴」，对分类和可视化没有价值；标准化后各特征等权参与，主成分反映的是特征间的相关结构。

**练习 2**：为什么 `X_test_std = sc.fit_transform(X_test)` 是错误的？

**答案**：`fit_transform` 会用**测试集自己的**均值和标准差重新拟合，等于把测试集的统计信息泄漏进预处理，且同一模型在训练/测试两个坐标系不一致。正确做法是 [L126](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L126) 的 `sc.transform(X_test)`，复用训练集参数。

**练习 3**：`np.cov(X_train_std.T)` 为什么要转置？

**答案**：`np.cov` 默认 `rowvar=True`，即「每行是一个变量、每列是一个观测」。而 `X_train_std` 是「行=样本、列=特征」，转置后每行才是一个特征，得到 13×13 的特征间协方差矩阵。

---

### 4.3 第 2、3 步：协方差矩阵与特征分解（eig 与 eigh）

#### 4.3.1 概念说明

第 2 步计算标准化数据的协方差矩阵：

\[ \Sigma = \frac{1}{n-1}\sum_{i=1}^{n}(x_i - \bar{x})(x_i - \bar{x})^{\mathsf{T}} \]

标准化后 \(\bar{x} \approx 0\)，所以 `np.cov` 实际上就是在算 \( \frac{1}{n-1} X^{\mathsf{T}}X \)。这个 13×13 的对称矩阵完整记录了 13 个特征的方差与两两相关性。

第 3 步对 \(\Sigma\) 做特征分解，解出 13 对 \((\lambda_j, v_j)\)。PCA 的全部答案都藏在里面：

- \(v_j\) 是主成分的**方向**（新坐标轴），长度为 1，两两正交；
- \(\lambda_j\) 是数据沿 \(v_j\) 投影后的**方差**，也就是该主成分「装了多少信息」。

为什么挑特征值大的方向就最优？因为投影到单位向量 \(w\) 上后数据的方差恰好是 \(w^{\mathsf{T}}\Sigma w\)（瑞利商），在 \(\|w\|=1\) 约束下使其最大的 \(w\) 正是 \(\Sigma\) 的最大特征值对应的特征向量——这是「保留方差最大」与「特征分解」之间的桥梁。

一个工程细节：实对称矩阵应优先使用 `np.linalg.eigh` 而不是通用的 `np.linalg.eig`。书中代码用的是 `eig`，并在注记中自己说明了这一点。

#### 4.3.2 核心流程

```text
cov_mat = np.cov(X_train_std.T)              # 13×13 对称矩阵
eigen_vals, eigen_vecs = np.linalg.eig(cov_mat)   # 或 np.linalg.eigh
# 特征值: 13 个正数, 总和 = 总方差 ≈ 12.9
# 特征向量: eigen_vecs[:, j] 是第 j 个方向 (13 维列向量)
```

`eig` 与 `eigh` 的对比：

| | `np.linalg.eig`（书中用的） | `np.linalg.eigh`（注记推荐的） |
| --- | --- | --- |
| 适用范围 | 任意方阵 | 仅 Hermitian/实对称矩阵（协方差矩阵恰好是） |
| 特征值 | 可能为复数（数值误差） | 保证全为实数 |
| 返回顺序 | 任意 | **升序**（可直接从末尾取最大 k 个） |
| 数值稳定性 | 较低 | 更高、更快 |

#### 4.3.3 源码精读

- [ch05/ch05.py:L177-L180](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac337afc3c0a91/ch05/ch05.py#L177-L180)：两行完成第 2、3 步——`np.cov` 求协方差矩阵，`np.linalg.eig` 做特征分解，随后打印 13 个特征值。特征值全为正且大小递减悬殊，正说明数据的信息集中在少数几个方向上。
- [ch05/ch05.py:L183-L187](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L183-L187)：作者注记——用 `eig` 分解对称协方差矩阵「不算错误但次优」，推荐专为 Hermitian 矩阵设计的 `eigh`：它保证返回实特征值，而数值稳定性较差的 `eig` 在某些情形会返回复数。本讲实践将采用 `eigh`。
- 同一注记在 LDA 一节再次出现（[L518-L522](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L518-L522)），而 LDA 的 \(S_W^{-1}S_B\) 不是对称矩阵，只能用 `eig`，所以书中那里的代码特意用了 `eigen_vals.real`（[L545-L546](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac337afc3c0a91/ch05/ch05.py#L545-L546)）丢弃数值虚部——这是「eig 可能返回复数」的现实例证。

#### 4.3.4 代码实践

**实践目标**：对比 `eig` 与 `eigh` 的输出，理解注记为什么推荐 `eigh`。

**操作步骤**（示例代码，紧接 4.2.4）：

```python
# 示例代码
vals_eig, vecs_eig = np.linalg.eig(cov_mat)
vals_eigh, vecs_eigh = np.linalg.eigh(cov_mat)

print('eig 的特征值是否含复数:', np.iscomplexobj(vals_eig))
print('eigh 是否升序:', np.all(np.diff(vals_eigh) >= 0))
print('排序后两者最大差:',
      np.abs(np.sort(vals_eig.real)[::-1] - vals_eigh[::-1]).max())
```

**需要观察的现象**：`eigh` 的特征值严格升序排列；`eig` 的特征值顺序不可预期；两者排序后数值几乎一致。

**预期结果**：`eig 的特征值是否含复数` 为 `True` 或 `False`（取决于 numpy/LAPACK 版本，这正是注记担心的问题）；排序后最大差应接近机器精度（约 1e-15 量级）。（具体输出待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：协方差矩阵的特征值在几何上代表什么？

**答案**：数据沿对应特征向量方向投影后的方差。13 个特征值之和等于矩阵的迹（总方差），标准化后约等于 12.9。

**练习 2**：既然书中注记推荐 `eigh`，LDA 部分为什么仍然用 `eig`？

**答案**：LDA 分解的是 \(S_W^{-1}S_B\)，两个矩阵乘积一般**不对称**，不满足 `eigh` 的适用条件；协方差矩阵是对称的，才可以用 `eigh`。这解释了为什么书中 LDA 代码要写 `eigen_vals.real`。

**练习 3**：`eigen_vecs[:, i]` 和 `eigen_vecs[i, :]` 哪个是第 i 个特征向量？

**答案**：`eigen_vecs[:, i]`（列）。numpy 的 `eig`/`eigh` 都按**列**返回特征向量，所以书中取方向时写 `eigen_vecs[:, i]`（见 [L222-L223](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L222-L223)）。

---

### 4.4 排序选 k：方差解释率与累计方差图

#### 4.4.1 概念说明

特征分解给出 13 个主成分，保留几个？先给每个主成分算「方差解释率」：

\[ \text{ratio}_j = \frac{\lambda_j}{\sum_{i=1}^{13}\lambda_i} \]

即第 j 个主成分的方差占总方差的比例。把 ratio 从大到小排列后逐项累加，就得到**累计方差解释率**——「前 k 个主成分总共保留了百分之多少的信息」。画成柱状图（单项）+ 阶梯图（累计），就能直观地选 k：常见准则是保留 80%~95% 方差，或在图中找「累计曲线由陡转平的拐点」。

这里要厘清 sklearn PCA 的两个容易混淆的属性：

| 属性 | 含义 | 手写版对应物 | 本数据量级 |
| --- | --- | --- | --- |
| `explained_variance_` | 各主成分方向的方差，即特征值 \(\lambda_j\) 本身 | `eigen_vals`（排序后） | 数量级 ≈ 0.1 ~ 4，总和 ≈ 12.9 |
| `explained_variance_ratio_` | \(\lambda_j / \sum_i \lambda_i\)，占比 | `var_exp` | 0 ~ 1 之间，总和 = 1 |

另一个相关量是**载荷（loadings）**：`特征向量 × √特征值`，衡量每个**原始特征**对某个主成分的贡献大小，可用于解释「PC1 主要由哪些化学指标构成」。

#### 4.4.2 核心流程

```text
tot = sum(eigen_vals)                                  # 总方差
var_exp = [λ / tot for λ in 排序后的特征值]             # 单项解释率
cum_var_exp = np.cumsum(var_exp)                       # 累计解释率
画柱状图(var_exp) + 阶梯图(cum_var_exp) → 目测选 k
```

选 k 的伪代码判断：

\[ k^* = \min\{k : \sum_{j \le k}\text{ratio}_j \ge 0.8\} \]

#### 4.4.3 源码精读

- [ch05/ch05.py:L195-L197](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L195-L197)：三行手写解释率——`tot` 是特征值总和（总方差），`var_exp` 用 `sorted(eigen_vals, reverse=True)` 从大到小排序后逐项除以 `tot`，`np.cumsum` 得累计值。
- [ch05/ch05.py:L204-L213](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L204-L213)：经典的「柱状+阶梯」组合图：`plt.bar` 画单项解释率，`plt.step(where='mid')` 画累计解释率，x 轴是主成分序号 1~13。你会看到第一根柱子最高、后面迅速变矮，累计曲线很快趋于平缓。
- [ch05/ch05.py:L281-L283](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L281-L283)：sklearn 对照——`PCA()`（默认保留全部 13 个）+ `fit_transform` 后直接读 `pca.explained_variance_ratio_`，数值应与手写 `var_exp` 一致。
- [ch05/ch05.py:L288-L293](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L288-L293)：用 sklearn 的 ratio 重画同一张累计方差图（书中注明这是「为复现手写结果而补充的单元格」）。
- [ch05/ch05.py:L389](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L389) 与 [L411](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L411)：载荷计算——手写版 `eigen_vecs * np.sqrt(eigen_vals)`，sklearn 版 `pca.components_.T * np.sqrt(pca.explained_variance_)`。注意后者正是 `explained_variance_`（未归一化的特征值）的用武之地：它开根号后是各主成分的实际标准差。

#### 4.4.4 代码实践

**实践目标**：用手写公式与 sklearn 各算一遍解释率，找出「保留 80% 方差」所需的最小 k。

**操作步骤**（示例代码，紧接 4.3.4）：

```python
# 示例代码
from sklearn.decomposition import PCA

tot = sum(vals_eigh)
var_exp = [(i / tot) for i in sorted(vals_eigh, reverse=True)]
cum_var_exp = np.cumsum(var_exp)

pca_full = PCA()
pca_full.fit(X_train_std)

print('手写前 5 项:', np.round(var_exp[:5], 4))
print('sklearn 前 5 项:', np.round(pca_full.explained_variance_ratio_[:5], 4))
print('达到 80% 方差至少需要 k =',
      int(np.searchsorted(cum_var_exp, 0.8)) + 1)
```

**需要观察的现象**：两列前 5 项数值逐位一致；累计解释率在前几个主成分上增长很快、之后放缓。

**预期结果**：在 wine 训练集上，前两个主成分合计解释约 55% 上下的方差（第一主成分约 1/3 强），达到 80% 通常需要 5 个左右的主成分——这正是「13 维数据确实可以压缩」的证据。（具体数值以本地输出为准，属待本地验证项。）

#### 4.4.5 小练习与答案

**练习 1**：`explained_variance_` 与 `explained_variance_ratio_` 的区别是什么？

**答案**：前者是各主成分方向的方差本身（等于协方差矩阵的特征值，带原始方差量纲），后者是除以总方差后的占比（无量纲，全部加起来等于 1）。手写版分别对应 `sorted(eigen_vals, reverse=True)` 与 `var_exp`。

**练习 2**：想保留至少 90% 的方差，除了按累计曲线手动选 k，sklearn 还有什么更直接的写法？

**答案**：`PCA(n_components=0.9)`——传入 0~1 的浮点数时，sklearn 自动选择使累计解释率 ≥ 0.9 的最小主成分数。（此为 sklearn 通用接口知识，非本章代码内容。）

**练习 3**：载荷 `loadings = eigen_vecs * np.sqrt(eigen_vals)` 为什么能反映原始特征的贡献？

**答案**：特征向量分量给出「原始特征进入主成分的线性系数」，乘上 √λ（该主成分的实际标准差）后，把无量纲的方向权重换算成与原始（标准化）特征协方差同量纲的贡献——载荷接近 ±1 表示该特征与这个主成分强相关。

---

### 4.5 第 4、5 步：构造投影矩阵 W、投影与 sklearn PCA 对照

#### 4.5.1 概念说明

选定 k 之后（wine 可视化取 k=2），把前 k 大特征值对应的特征向量按列拼成投影矩阵 \( W \in \mathbb{R}^{13 \times 2} \)，然后一步矩阵乘法完成降维：

\[ X_{\text{pca}} = X_{\text{std}} W \]

几何直觉：\(W\) 的两列是两根新的正交坐标轴，`dot` 把每个 13 维样本重新表达成在这两根轴上的坐标 (PC1, PC2)。由于特征向量正交且单位长度，这一步只是**旋转坐标系**，不拉伸、不扭曲。

一个必须提前知道的数值现象：**特征向量的正负号是不唯一的**。若 \(v\) 满足 \(\Sigma v = \lambda v\)，则 \(-v\) 也满足（\(\Sigma(-v) = \lambda(-v)\)）。所以手写 PCA 与 sklearn PCA（内部用 SVD）可能给出**符号相反**的坐标轴——对应的投影结果整体差一个正负号，散点图呈镜像，但所有几何关系（类间距离、可分性）完全等价。这不影响任何下游使用。

#### 4.5.2 核心流程

```text
书中做法（排序取前 k）:
1. 组成 (特征值, 特征向量) 二元组列表 eigen_pairs
2. eigen_pairs.sort(按特征值降序)                # 第 4 步
3. w = np.hstack((前两个特征向量各自变成列))       # 拼成 13×2 的 W
4. X_train_pca = X_train_std.dot(w)              # 第 5 步：投影

等价的 numpy 惯用写法（配合 eigh 的升序输出）:
idx = np.argsort(eigen_vals)[::-1]               # 从大到小的下标
W   = eigen_vecs[:, idx[:2]]                     # 直接按列索引取前 k
```

sklearn 侧的完整纪律：

```text
pca = PCA(n_components=2)
X_train_pca = pca.fit_transform(X_train_std)   # 只在训练集拟合
X_test_pca  = pca.transform(X_test_std)        # 测试集只变换
```

#### 4.5.3 源码精读

- [ch05/ch05.py:L222-L226](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L222-L226)：第 4 步的书中实现——把每个 `(np.abs(eigen_vals[i]), eigen_vecs[:, i])` 组成二元组（取绝对值防复数特征值干扰排序），再 `sort(key=..., reverse=True)` 按特征值从大到小排序。
- [ch05/ch05.py:L231-L233](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L231-L233)：`np.hstack` 把排名第一、第二的特征向量（各 13 维）横向拼成 13×2 的矩阵 W 并打印。`[:, np.newaxis]` 的作用是把一维数组变成列向量以便堆叠。
- [ch05/ch05.py:L236-L245](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L236-L245)：符号翻转注记——不同 numpy/LAPACK 版本可能得到符号相反的 W，书中给出数学证明 \(\Sigma(-v) = \lambda(-v)\)，说明这不是 bug。
- [ch05/ch05.py:L249](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac337afc3c0a91/ch05/ch05.py#L249)：先投影**单个样本**（`X_train_std[0].dot(w)`，13 维 → 2 维）演示一次内积的含义，这是理解第 5 步的最小例子。
- [ch05/ch05.py:L254-L268](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac337afc3c0a91/ch05/ch05.py#L254-L268)：整批投影 `X_train_std.dot(w)` 得到 124×2，然后按三个类别分别画散点——这是 wine 第一次以「全特征压缩图」的形式呈现：三个类别沿 PC1 方向几乎线性可分，仅用两个新特征就保留了大部分判别信息。
- [ch05/ch05.py:L298-L300](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L298-L300)：**本讲最重要的一行纪律**——`X_train_pca = pca.fit_transform(X_train_std)` 之后再 `X_test_pca = pca.transform(X_test_std)`，测试集只变换。PCA 的「参数」就是主成分方向（和中心），与 StandardScaler 的 μ、σ 一样只能来自训练集。
- [ch05/ch05.py:L348-L353](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L348-L353)：PCA 的实战收尾——把 2 维投影喂给 `LogisticRegression` 训练，随后用 [L358-L364](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L358-L364) 与 [L369-L375](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L369-L375) 分别在训练/测试投影上调用 `plot_decision_regions`（定义在 [L314-L340](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L314-L340)，u2-l1 已精读过）。测试图几乎与训练图一样干净——压缩到 1/6 维度后分类性能几乎没有损失。

#### 4.5.4 代码实践

**实践目标**：用书中「二元组排序」与「eigh+argsort」两种方式各构造一次 W，验证它们等价，再与 sklearn 投影结果对照（允许符号相反）。

**操作步骤**（示例代码，紧接 4.4.4）：

```python
# 示例代码
pca2 = PCA(n_components=2)
X_sk = pca2.fit_transform(X_train_std)

# 方式 A：书中写法（基于 eig 的输出）
pairs = [(np.abs(vals_eig[i]), vecs_eig[:, i].real) for i in range(13)]
pairs.sort(key=lambda kv: kv[0], reverse=True)
w_book = np.hstack((pairs[0][1][:, np.newaxis],
                    pairs[1][1][:, np.newaxis]))

# 方式 B：eigh（升序）+ argsort
idx = np.argsort(vals_eigh)[::-1]
w_eigh = vecs_eigh[:, idx[:2]]

print('两列都是单位向量:', np.allclose(np.linalg.norm(w_eigh, axis=0), 1))
print('两列正交:', np.isclose(w_eigh[:, 0] @ w_eigh[:, 1], 0))

X_np = X_train_std @ w_eigh
for j in range(2):
    print(f'PC{j+1} 与 sklearn 一致: {np.allclose(X_np[:, j], X_sk[:, j])}, '
          f'方向相反: {np.allclose(X_np[:, j], -X_sk[:, j])}')
```

**需要观察的现象**：W 的两列模长为 1、内积为 0；与 sklearn 的每一列要么逐位一致、要么整体差一个负号。

**预期结果**：两个布尔值每列恰好有一个为 `True`；即使符号不同，`|X_np|` 与 `|X_sk|` 逐样本相等。（具体哪一列翻转因 LAPACK 版本而异，待本地验证。）

#### 4.5.5 小练习与答案

**练习 1**：为什么投影矩阵 W 的列必须单位正交？

**答案**：它们是协方差矩阵的不同特征向量，实对称矩阵的特征向量天然正交。单位正交保证新坐标系各轴不相关（主成分间协方差为 0，方差恰为 λ_j）、投影只是旋转而不缩放，方差解释率的计算才成立。

**练习 2**：测试集的降维代码怎么写？为什么不能 `fit_transform(X_test_std)`？

**答案**：`X_test_pca = pca.transform(X_test_std)`（或 `X_test_std.dot(W)`，W 来自训练集）。在测试集上重新 fit 会用测试样本自己找主成分方向，既造成信息泄漏，又使训练/测试落在两套坐标系里。参见 [L298-L300](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L298-L300)。

**练习 3**：手写 PCA 得到的 PC1 坐标与 sklearn 的 PC1 坐标互为相反数，散点图左右镜像，这有问题吗？

**答案**：没有问题。特征向量符号本身不唯一（[L236-L245](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L236-L245) 的注记），取反只是把坐标轴调转 180°，样本间距离、类别可分性、任何基于投影训练的分类器都完全等价。比较两份 PCA 结果时应逐列允许 ±1 因子。

---

## 5. 综合实践

**任务**：不用 `sklearn.decomposition.PCA`，只用 numpy 把标准化后的 wine 训练集降到 2 维，再与 sklearn 的结果做数值对比，并打印两者各自的总方差解释率。这个任务把五步流程从头到尾串起来。

**完整参考脚本**（示例代码，在 `ch05/` 目录下运行）：

```python
# 示例代码：numpy 五步法 PCA vs sklearn PCA
import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from sklearn.decomposition import PCA

# ---- 数据准备（与 ch05.py L87-L116 相同）----
df_wine = pd.read_csv('wine.data', header=None)
df_wine.columns = ['Class label', 'Alcohol', 'Malic acid', 'Ash',
                   'Alcalinity of ash', 'Magnesium', 'Total phenols',
                   'Flavanoids', 'Nonflavanoid phenols', 'Proanthocyanins',
                   'Color intensity', 'Hue',
                   'OD280/OD315 of diluted wines', 'Proline']
X, y = df_wine.iloc[:, 1:].values, df_wine.iloc[:, 0].values
X_train, _, y_train, _ = train_test_split(X, y, test_size=0.3,
                                          stratify=y, random_state=0)

# ---- 五步法 ----
# 1) 标准化
X_std = StandardScaler().fit_transform(X_train)
# 2) 协方差矩阵
cov_mat = np.cov(X_std.T)
# 3) 特征分解（对称矩阵用 eigh，升序返回）
vals, vecs = np.linalg.eigh(cov_mat)
# 4) 排序取前 k=2
ratio = vals / vals.sum()
k = 2
top = np.argsort(vals)[::-1][:k]
W = vecs[:, top]
# 5) 投影
X_pca_np = X_std @ W

# ---- sklearn 对照 ----
pca = PCA(n_components=2)
X_pca_sk = pca.fit_transform(X_std)

print('手写 解释率 :', np.round(ratio[top], 4),
      '累计:', round(ratio[top].sum(), 4))
print('sklearn 解释率:', np.round(pca.explained_variance_ratio_, 4),
      '累计:', round(pca.explained_variance_ratio_.sum(), 4))

for j in range(k):
    same = np.allclose(X_pca_np[:, j],  X_pca_sk[:, j])
    flip = np.allclose(X_pca_np[:, j], -X_pca_sk[:, j])
    print(f'PC{j+1}: 一致={same}  相反={flip}')

# ---- 可视化（可选）：按类别画散点 ----
import matplotlib.pyplot as plt
for l, c, m in zip(np.unique(y_train), ['r', 'b', 'g'], ['o', 's', '^']):
    plt.scatter(X_pca_np[y_train == l, 0], X_pca_np[y_train == l, 1],
                c=c, marker=m, label=f'Class {l}')
plt.xlabel('PC 1'); plt.ylabel('PC 2'); plt.legend()
plt.show()
```

**操作步骤**：

1. 在 `ch05/` 目录下把脚本存为独立文件（例如 `my_pca.py`）后运行，或逐段贴进 `ch05.ipynb` 的空单元格执行（注意 notebook 中魔法命令与脚本转换的规则，见 u1-l3）。
2. 先只跑数值部分，看两行解释率和两行一致性判断。
3. 再放开最后的可视化段，观察三个类别在 PC1–PC2 平面上的分布。
4. 进阶：把 `k = 2` 改成 `k = 5`（4.4 节结论：约 80% 方差），把对照脚本改成打印累计解释率。

**需要观察的现象**：

- 手写与 sklearn 的解释率逐位一致（`eigh` 与 SVD 在数学上等价）；
- 每个主成分的判断要么「一致」要么「相反」，二者必有其一；
- 散点图上三个类别沿 PC1 方向近似线性可分，与书中图一致（若你的图左右镜像，正是符号翻转）。

**预期结果**：`手写 解释率` 与 `sklearn 解释率` 两组数值相同；两行判断中每行恰有一个 `True`。若网络受限请使用本地 `wine.data`（脚本已如此），无需访问 UCI。（若 numpy 版本行为不同导致 `eig`/`eigh`/SVD 的符号差异 pattern 不同，属正常现象，待本地验证。）

## 6. 本讲小结

- PCA 是**无监督**线性降维：只用 `X`，通过「投影方差最大」找新坐标轴，五步流程为标准化 → 协方差矩阵 → 特征分解 → 排序取前 k → 投影（`X' = XW`）。
- **标准化是前提**：PCA 以方差度量信息，不标准化会让量纲大的特征（如 Proline）独占第一主成分；且 μ、σ 只能在训练集估计，测试集只 `transform`（[ch05.py:L124-L126](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L124-L126)）。
- 实对称的协方差矩阵应优先 `np.linalg.eigh`（实数、升序、更稳），书中用 `eig` 并附注记说明（[L183-L187](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac337afc3c0a91/ch05/ch05.py#L183-L187)）；特征值 = 各主成分方差，特征向量按列取出、单位正交。
- 选 k 靠方差解释率：`explained_variance_` 是特征值本身，`explained_variance_ratio_` 是占总方差的比例；累计方差图（柱状+阶梯）是标准工具，wine 上前两个主成分约占一半以上方差。
- 手写与 sklearn 的结果可能**符号相反**：特征向量的 ± 不唯一（[L236-L245](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L236-L245)），比较 PCA 结果必须逐列允许翻转。
- sklearn 侧同样遵守 fit/transform 纪律：`PCA` 在训练集 `fit_transform`、测试集只 `transform`，主成分方向和 StandardScaler 的参数一样属于「只能从训练集学到的参数」。

## 7. 下一步学习建议

- **下一讲（u5-l2）**：同章的 LDA（监督降维）与核 PCA。重点对比 LDA 为什么能用标签 `y` 找「类间散布/类内散布」最优的方向，以及核 PCA 如何处理月牙形这类线性不可分数据——你会看到 `ch05.py` 后半部分（L427 起）大量复用本讲的特征分解、排序、投影套路。
- **延伸阅读源码**：`ch05/ch05.py` 的 LDA 部分（[L444-L626](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L444-L626)），特别是 `np.linalg.eig(np.linalg.inv(S_W).dot(S_B))` 处 `.real` 的用法（[L515](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac337afc3c0a91/ch05/ch05.py#L515)、[L545-L546](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L545-L546)）。
- **向后衔接（u6-l1）**：第 6 章会把 `StandardScaler` 和模型一起塞进 `Pipeline` 并做交叉验证。届时回想本讲的泄漏问题：PCA 若要放进建模流程，同样必须整体放进 Pipeline，让每折交叉验证都只用该折训练数据拟合降维。
