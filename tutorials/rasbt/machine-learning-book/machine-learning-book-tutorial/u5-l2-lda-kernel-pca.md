# u5-l2 监督降维 LDA 与非线性核 PCA

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 LDA（线性判别分析）与 PCA 的本质区别：PCA 无监督地找「方差最大」的投影方向，LDA 监督地找「类间散布大、类内散布小」的投影方向。
2. 读懂 `ch05.py` 中手算 LDA 的全部代码：类均值向量、类内散布矩阵 \( S_W \)、类间散布矩阵 \( S_B \)、广义特征值问题 \( S_W^{-1}S_B \)、判别度排序与投影。
3. 掌握 sklearn 的 `LinearDiscriminantAnalysis` 用法：`fit_transform(X, y)` 要传入标签、`n_components` 最多为类别数减一（wine 三类 → 2；二分类 → 1），并沿用「训练集拟合、测试集只变换」的原则。
4. 理解为什么线性降维对月牙形这类非线性数据无能为力，以及核 PCA 如何借核技巧（u3-l2 已建立）在「核空间」里做 PCA。
5. 掌握用 `GridSearchCV` 包裹「KernelPCA + 逻辑回归」流水线、用下游监督指标搜索 `gamma` 与 `coef0` 的方法，并理解为什么核参数必须这样选。

**先声明一个重要的仓库事实**（贯穿本讲）：本仓库 `ch05.py` 的非线性降维一节收录的是 **t-SNE**，**并不包含纸书中可能提到的 KernelPCA 代码**（已用全仓库检索确认）。因此本讲 4.4、4.5 中所有 KernelPCA 相关代码都标注为「示例代码」，它们不在仓库里；而 LDA、t-SNE、`make_moons`、`GridSearchCV` 的写法全部有仓库真实代码可对照。

## 2. 前置知识

- **wine 数据集与预处理**（u4-l1、u5-l1）：178 个样本、13 个特征、3 个类别，70/30 分层划分后标准化。本讲 LDA 直接复用 `ch05.py` 前面 PCA 一节准备好的 `X_train_std`、`X_test_std`、`y_train`、`y_test`。
- **PCA 五步流程**（u5-l1）：标准化 → 协方差矩阵 → 特征分解 → 取前 k 个特征值对应的特征向量 → 投影。LDA 的计算骨架与之惊人地相似，只是「协方差矩阵」换成了「两个散布矩阵的广义特征值问题」。
- **特征值 / 特征向量**：\( \Sigma v = \lambda v \)。PCA 里特征值是投影方差；LDA 里特征值衡量的是「投影后类间分离度相对类内离散度的大小」。
- **核技巧**（u3-l2）：不显式构造高维映射 \( \phi(x) \)，只用核函数 \( k(x, y) = \phi(x)^T\phi(y) \) 完成高维空间中的内积运算。RBF（高斯）核为：

  \[ k(x, y) = \exp(-\gamma \|x - y\|^2) \]

  `gamma` 越大，单个样本的影响半径越小。
- **scikit-learn 估计器范式**（u3-l1）：`fit` 学参数、`transform` 变换数据、`fit_transform` 一步完成。Transformer 与 Classifier 可以用 `Pipeline` 串起来。
- **逻辑回归**（u3-l1）：本讲用它充当「降维之后接的分类器」，充当核 PCA 参数好坏的裁判。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ch05/ch05.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py) | 本讲主线。L427–L626 是 LDA 一节（手算 + sklearn 两套实现）；L630–L695 是非线性降维一节（t-SNE，仓库实际收录的非线性方法） |
| [ch05/ch05.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.ipynb) | 同一内容的 notebook 形式，适合逐单元格做本讲的实验（双轨工作流见 u1-l3） |
| [ch10/ch10.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py) | 第 10 章（聚类）。L496 有 `make_moons` 月牙形数据的真实用法，是本讲核 PCA 实验的数据来源 |
| [ch06/ch06.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py) | 第 6 章（模型评估与调参）。L334–L352 的 `make_pipeline` + `param_grid` + `GridSearchCV` 是本讲「搜索核参数」的仓库真实范式 |
| [ERRATA/README.md](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ERRATA/README.md) | 勘误目录。L137–L144 有一条与 LDA 类内散布矩阵直接相关的勘误（issue #169），本讲 4.2 会用到 |
| [ch05/figures/](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/figures) | 章节配图。其中 `05_11.png`（月牙形数据在合适的非线性投影后两类分开的示意图）与 `05_14.png`（两类样本投影到第一主成分后分离成两条带）**不是由 `ch05.py` 生成的**（脚本中没有对应代码），属于随仓库附带的书内插图，章节归属待确认 |

## 4. 核心概念与源码讲解

### 4.1 PCA 与 LDA 的本质区别

#### 4.1.1 概念说明

u5-l1 的 PCA 只回答一个问题：「往哪个方向投影，数据整体的方差最大？」它根本不看类别标签。这带来一个隐患：**方差大的方向未必是分类最有用的方向**。极端情况下，两类样本沿某个方向散得很开（方差大），但那恰恰是噪声方向；真正能区分两类的方向可能方差很小，被 PCA 丢掉了。

LDA 反其道而行之：既然我们最终要做分类，就直接把标签 `y` 用起来，找让**类间散布尽可能大、类内散布尽可能小**的投影方向。直观比喻：

- PCA 像是「让所有人（不分班级）在操场上站得尽量散开」；
- LDA 像是「让三个班级各自站成一堆（类内紧），且堆与堆离得越远越好（类间散）」。

LDA 的优化目标可以用 Fisher 判别准则写成（以单个投影方向 \( w \) 为例）：

\[ J(w) = \frac{w^T S_B w}{w^T S_W w} \]

分子越大，投影后类中心离得越远；分母越小，投影后每个类内部越紧凑。最大化 \( J(w) \) 的问题最终转化为一个广义特征值问题（见 4.2）。

需要提前建立的预期：LDA 是**线性**方法，投影方向仍是原始特征的线性组合；它对「类内高斯分布、协方差相近」的数据效果最好。非线性结构（比如月牙形）它同样无能为力——那是 4.4 核 PCA 的主题。

#### 4.1.2 核心流程

```text
LDA（监督降维）流程:
输入: X_train_std (124×13), y_train (1/2/3)
1. 按类计算均值向量        m_i = mean(类 i 的样本)            → 3 个 13 维向量
2. 计算类内散布矩阵 S_W    各类内部「围绕自己类中心」的散布之和   → 13×13
3. 计算类间散布矩阵 S_B    各类中心「围绕总均值」的加权散布之和   → 13×13
4. 广义特征值问题           S_W^{-1} S_B v = λ v               → 13 个 (λ, v)
5. 取前 k 个特征向量拼 W    k ≤ 类别数 − 1 = 2                  → 13×2
6. 投影                    X_lda = X_std · W                   → 124×2
输出: 低维特征 X_lda 与 W（测试集用同一 W 变换）
```

与 PCA 五步对照：第 1–4 步把「协方差矩阵 + 普通特征分解」替换成了「两个散布矩阵 + 广义特征分解」，第 5 步的 `k` 从「自由选择、看累计方差」变成「硬上限 \( c-1 \)」。

#### 4.1.3 源码精读

- [ch05/ch05.py:L427-L435](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L427-L435)：`ch05.py` 的 LDA 一节从这里开始，小节标题依次是「Supervised data compression via linear discriminant analysis」和「Principal component analysis versus linear discriminant analysis」——「监督的数据压缩」这个措辞本身就点明了 LDA 与 PCA 的分野。
- [ch05/ch05.py:L112-L126](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L112-L126)：与 PCA 一节共用的数据准备（切 X/y、70/30 分层划分、标准化）。u5-l1 已精读，此处不重复。
- [ch05/ch05.py:L597-L598](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L597-L598)：全节最能体现「监督」两字的代码——`lda.fit_transform(X_train_std, y_train)`。对比 PCA 的 `pca.fit_transform(X_train_std)`（u5-l1）：**LDA 的 `fit_transform` 多了一个 `y_train` 参数**。类别标签直接参与了方向的学习。

#### 4.1.4 代码实践

**实践目标**：用同一份数据、同一个分类器，直观对比「PCA 特征」与「LDA 特征」哪个更好分。

**操作步骤**（示例代码，在 `ch05/` 目录下运行；LDA 部分照抄仓库代码，PCA 部分沿用 u5-l1）：

```python
# 示例代码（LDA 两行来自 ch05.py L597-L605，原样可用）
from sklearn.decomposition import PCA
from sklearn.discriminant_analysis import LinearDiscriminantAnalysis as LDA
from sklearn.linear_model import LogisticRegression

# PCA 特征（无监督）
X_train_pca = PCA(n_components=2).fit_transform(X_train_std)

# LDA 特征（监督）
lda = LDA(n_components=2)
X_train_lda = lda.fit_transform(X_train_std, y_train)   # 注意这里传了 y_train

for name, X_tr in [('PCA', X_train_pca), ('LDA', X_train_lda)]:
    lr = LogisticRegression(multi_class='ovr', random_state=1,
                            solver='lbfgs').fit(X_tr, y_train)
    print(name, '训练集准确率:', lr.score(X_tr, y_train))
```

**需要观察的现象**：两张决策区域图里，LDA 的三个类是否明显更「成团」、边界更干净；准确率数字哪个更高。

**预期结果**：在 wine 数据上，LDA 特征通常比 PCA 特征分得更好（这是引入标签信息的直接回报）。具体数字**待本地验证**。

#### 4.1.5 小练习与答案

1. **练习**：「LDA 是监督降维」这一点在代码上体现在哪里？
   **答案**：`fit_transform(X_train_std, y_train)` 显式传入标签；更底层地，散布矩阵 \( S_W \)、\( S_B \) 必须按类分组计算（见 4.2），没有标签就算不出来。PCA 的全部计算则不涉及 `y`。
2. **练习**：wine 有 3 个类别，LDA 最多能给出几个判别方向？如果只有 2 个类别呢？
   **答案**：最多 \( c-1 = 2 \) 个（原因见 4.2 的秩分析）；二分类时 \( c-1 = 1 \)，即 `n_components=1`，所有样本被压到一条直线上，这正是讲义规格中「n_components=1」的典型场景。
3. **练习**：LDA 一定比 PCA 好吗？
   **答案**：不一定。LDA 的收益依赖标签质量与「各类近似高斯、协方差相近」的假设；标签噪声大、或判别信息藏在方差小的方向之外的非线性结构里（如月牙形数据）时，LDA 与 PCA 都会失效。

### 4.2 手算 LDA：散布矩阵与广义特征值问题

#### 4.2.1 概念说明

`ch05.py` 在调 sklearn 之前先手算了完整 LDA，这段代码是理解 LDA 的最佳材料。三个核心对象：

- **类均值向量** \( m_i \)：第 \( i \) 类样本在每个特征上的平均值，13 维。
- **类内散布矩阵**（within-class scatter）：

  \[ S_W = \sum_{i=1}^{c} \sum_{x \in D_i} (x - m_i)(x - m_i)^T \]

  衡量「每个类自己内部有多散」。注意每项围绕**各自的类中心** \( m_i \)，而不是总均值。
- **类间散布矩阵**（between-class scatter）：

  \[ S_B = \sum_{i=1}^{c} n_i (m_i - m)(m_i - m)^T \]

  其中 \( m \) 是所有样本的总均值，\( n_i \) 是第 \( i \) 类样本数。它衡量「类中心之间离得多远」，用 \( n_i \) 加权是让大类的话语权更大。

最大化 Fisher 准则 \( J(w) = \frac{w^T S_B w}{w^T S_W w} \) 可以推出：最优 \( w \) 是下面**广义特征值问题**的解：

\[ S_B w = \lambda S_W w \quad \Longleftrightarrow \quad S_W^{-1} S_B w = \lambda w \]

也就是说：先算 \( S_W^{-1}S_B \)，再做普通的特征分解——和 PCA 的计算骨架对齐了。

一个关键的秩约束：\( S_B \) 是 \( c \) 个向量 \( (\sqrt{n_i}(m_i - m)) \) 的外积之和，而这些向量加权和为零（\(\sum_i n_i(m_i - m) = 0\)），因此 \( \text{rank}(S_B) \le c - 1 \)。**这就是 LDA 最多只有 \( c-1 \) 个非零判别方向的数学原因**：wine 三类 → 广义特征值里至多 2 个明显非零，其余接近 0。

#### 4.2.2 核心流程

```text
手算 LDA（ch05.py L444-L588）:
1. mean_vecs: 循环 label=1..3, np.mean(X_train_std[y_train==label], axis=0)
2. S_W 版本一（教科书定义式）: 逐类逐样本累加 (row−mv)(row−mv)^T
3. S_W 版本二（"更好的"版本）: S_W = Σ np.cov(类内子样本)
   → 因为三类样本数不相等, 直接累加散布会让大类贡献偏大
4. S_B: Σ n_i (m_i − m)(m_i − m)^T
5. np.linalg.eig(np.linalg.inv(S_W) @ S_B) → 13 个广义特征对
6. 按特征值降序排序, 取前 2 个特征向量(取实部)拼 W
7. X_train_lda = X_train_std @ W → 二维散点图
```

#### 4.2.3 源码精读

- [ch05/ch05.py:L444-L449](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L444-L449)：按类计算三个均值向量 `mean_vecs`。`X_train_std[y_train == label]` 用布尔掩码取类内子集——这是全书反复出现的「按类切片」写法。
- [ch05/ch05.py:L456-L466](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L456-L466)：\( S_W \) 的教科书定义式实现——外层循环累加各类的 `class_scatter`，内层逐样本累加 `(row − mv)(row − mv)^T`（先把一维数组 reshape 成列向量再点积）。
- [ch05/ch05.py:L469-L486](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L469-L486)：代码注释写着「Better: covariance matrix since classes are not equally distributed」——用 `np.cov` 逐类求协方差再求和。动机见 [L473-L474](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L473-L474)：`np.bincount(y_train)[1:]` 打印三类训练样本数，它们并不相等（分层划分下大约是 41/49/34 的量级，具体数值待本地验证）。定义式里大类样本多、贡献自然大；换成「每类一个协方差矩阵再相加」相当于给每类同等权重。
- [ERRATA/README.md:L137-L144](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ERRATA/README.md#L137-L144)：一条官方勘误（issue #169）指出：`np.cov` 默认用无偏归一化 \( 1/(n_i-1) \)，所以把协方差矩阵**直接**相加并不等于定义式的 \( S_W \)；若要等价，每个类协方差矩阵应先乘 \( n_i - 1 \) 再累加。实践会验证这一差异对最终投影方向影响很小（整体缩放不改变特征向量，只有类间**相对**权重变化才影响方向）。
- [ch05/ch05.py:L493-L505](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L493-L505)：\( S_B \) 的实现——总均值 `mean_overall`、按类取 `n` 与类均值、累加 `n * (mean_vec − mean_overall)(mean_vec − mean_overall)^T`，与公式逐项对应。
- [ch05/ch05.py:L511-L515](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L511-L515)：一行完成广义特征值问题：`np.linalg.eig(np.linalg.inv(S_W).dot(S_B))`。读码提示：紧随其后的 L518–L522 注释建议「对称矩阵用 `eigh` 更好」——那段注释其实是从 PCA 一节复制来的；在 LDA 这里 \( S_W^{-1}S_B \) 一般**不对称**，用通用的 `eig` 恰恰是正确选择。
- [ch05/ch05.py:L529-L540](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L529-L540)：把 (特征值, 特征向量) 组成元组列表、按特征值降序排序，并打印验证。运行后应能看到**只有前两个特征值明显非零**，其余接近 0——秩约束的直接体现（数值待本地验证）。
- [ch05/ch05.py:L545-L559](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L545-L559)：仿照 PCA 的累计方差图，画「判别度比率」（`discr` / `cum_discr`）柱状+阶梯图。语义对照：PCA 的纵轴是「方差解释率」，这里换成了「判别性解释率」（注意代码用 `eigen_vals.real` 取实部——`eig` 对非对称矩阵可能返回复数）。
- [ch05/ch05.py:L564-L566](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L564-L566)：取前两个特征向量（`.real`）`np.hstack` 拼成 13×2 投影矩阵 `W`——与 PCA 一节的 [L231-L233](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L231-L233) 写法完全同构。
- [ch05/ch05.py:L574-L588](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L574-L588)：投影 `X_train_std.dot(w)` 后按类画散点。注意 L580 的 `X_train_lda[y_train == l, 1] * (-1)`：把第二判别轴**乘 −1 翻转**，纯属让图的朝向与书中插图一致——与 u5-l1 讲过的「特征向量差一个正负号不影响本质」同理。

#### 4.2.4 代码实践

**实践目标**：亲手验证 ERRATA 那条勘误——三种 \( S_W \) 算法得到的判别方向是否一致。

**操作步骤**（示例代码，需先跑完 `ch05.py` L444–L505 得到 `mean_vecs` 与 `S_B`）：

```python
# 示例代码
import numpy as np

d = 13
# 算法 A: 定义式（L456-L466 的向量化写法）
SW_def = np.zeros((d, d))
for label in range(1, 4):
    Xc = X_train_std[y_train == label]
    mv = np.mean(Xc, axis=0)
    SW_def += (Xc - mv).T @ (Xc - mv)

# 算法 B: np.cov 直接求和（ch05.py L479-L486）
SW_cov = sum(np.cov(X_train_std[y_train == l].T) for l in range(1, 4))

# 算法 C: 按 ERRATA 乘 (n_i - 1) 还原
SW_fix = np.zeros((d, d))
for label in range(1, 4):
    Xc = X_train_std[y_train == label]
    SW_fix += (Xc.shape[0] - 1) * np.cov(Xc.T)

def top_direction(SW):
    ev, evec = np.linalg.eig(np.linalg.inv(SW).dot(np.array(S_B)))
    return evec[:, np.argmax(ev.real)].real

print(np.corrcoef(np.abs(top_direction(SW_def)),
                  np.abs(top_direction(SW_cov)))[0, 1])
print(np.corrcoef(np.abs(top_direction(SW_def)),
                  np.abs(top_direction(SW_fix)))[0, 1])
```

**需要观察的现象**：两个相关系数是否都接近 1。

**预期结果**：都会非常接近 1——因为三类样本数差异不大（41/49/34 量级），「协方差直接相加」引入的相对权重失真很小。这解释了为什么勘误指出的问题在 wine 数据上**不改变实践结论**。具体数值**待本地验证**。

#### 4.2.5 小练习与答案

1. **练习**：为什么 \( S_B \) 的秩至多是 \( c-1 \)？
   **答案**：\( S_B = \sum_i n_i (m_i-m)(m_i-m)^T \) 由 \( c \) 个向量 \( m_i - m \) 的外积构成，而 \( \sum_i n_i (m_i - m) = 0 \)（总均值的定义），这 \( c \) 个向量线性相关，至多张成 \( c-1 \) 维空间，故秩 \( \le c-1 \)。
2. **练习**：手算 LDA 用 `np.linalg.eig` 而不是 `np.linalg.eigh`，是失误吗？
   **答案**：不是。`eigh` 专用于对称（Hermitian）矩阵，而 \( S_W^{-1}S_B \) 一般不对称；`eig` 是通用求解器，这里必须用它。代码 L518–L522 推荐用 `eigh` 的注释是从 PCA 一节复制的，放到 LDA 语境里并不适用。
3. **练习**：`np.bincount(y_train)[1:]` 这行打印解决什么疑问？
   **答案**：它暴露三类训练样本数不相等，从而引出「定义式 \( S_W \) 会让大类贡献更大」的问题，是书中改用逐类协方差相加的动机；`[1:]` 是为了跳过 `bincount` 结果中标签 0 的空位（wine 标签是 1/2/3）。

### 4.3 sklearn 的 LDA：fit_transform(X, y) 与 n_components 上限

#### 4.3.1 概念说明

手算 LDA 帮助理解原理，实际工程直接用 `sklearn.discriminant_analysis.LinearDiscriminantAnalysis`（`ch05.py` 在 L14 以别名 `LDA` 导入）。它有三个值得注意的特性：

1. **它既降维又分类**：这个类自带 `predict`，本身就是一个分类器；`ch05.py` 只把它当降维器用，降维后再接逻辑回归。
2. **`n_components` 有硬上限 \( c-1 \)**：wine 三类最多 2；二分类场景（讲义规格中的 `n_components=1`）把样本压到一条直线上。
3. **默认 `solver='svd'`**：用奇异值分解求解，**不需要显式求 \( S_W^{-1} \)**。手算代码里的 `np.linalg.inv(S_W)` 在特征强共线、\( S_W \) 近奇异时数值上很危险；svd 求解器对这类情况更稳健。这是「工程实现」与「教学实现」的典型差异。

#### 4.3.2 核心流程

```text
sklearn LDA 用法（ch05.py L592-L626）:
1. lda = LDA(n_components=2)
2. X_train_lda = lda.fit_transform(X_train_std, y_train)   # 训练集: 拟合+变换, 传标签
3. lr = LogisticRegression(...).fit(X_train_lda, y_train)  # 在低维空间上训练分类器
4. 决策区域图（训练集）
5. X_test_lda = lda.transform(X_test_std)                  # 测试集: 只变换
6. 决策区域图（测试集）
```

第 2 步与第 5 步依旧是 u5-l1 强调的防泄漏纪律：**参数只在训练集上学，测试集只做 `transform`**。

#### 4.3.3 源码精读

- [ch05/ch05.py:L597-L598](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L597-L598)：`lda = LDA(n_components=2)` + `fit_transform(X_train_std, y_train)`。三行之内完成手算 LDA 七步的全部工作；`n_components=2` 正是三类数据的上限。
- [ch05/ch05.py:L604-L605](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L604-L605)：在 2 个判别特征上训练逻辑回归，超参与 PCA 一节（[L352-L353](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L352-L353)）完全相同，保证对比公平。
- [ch05/ch05.py:L607-L613](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L607-L613)：用第 3 章（u3-l1）介绍过的 `plot_decision_regions`（定义在 [L314-L340](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L314-L340)）画训练集决策区域，坐标轴是 LD 1 / LD 2。
- [ch05/ch05.py:L618-L626](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L618-L626)：`lda.transform(X_test_std)` 把测试集投影到同一组判别方向上，再画测试集决策区域。注意这里**没有**再次 `fit`。

#### 4.3.4 代码实践

**实践目标**：体验 `n_components` 上限——把三类数据压到一维后还能不能分开。

**操作步骤**（示例代码，基于仓库代码微改）：

```python
# 示例代码（改自 ch05.py L597-L605）
lda1 = LDA(n_components=1)                       # 三类压到 1 维（低于上限 2）
X_train_lda1 = lda1.fit_transform(X_train_std, y_train)
print('降维后形状:', X_train_lda1.shape)          # 预期 (124, 1)

# 一维投影上逐类画直方图
import matplotlib.pyplot as plt
for label in range(1, 4):
    plt.hist(X_train_lda1[y_train == label, 0], bins=20, alpha=0.5,
             label=f'Class {label}')
plt.xlabel('LD 1'); plt.legend(); plt.show()

# 再试试超过上限
try:
    LDA(n_components=3).fit(X_train_std, y_train)
except Exception as e:
    print(type(e).__name__, ':', e)
```

**需要观察的现象**：三个类的直方图是否在 LD 1 轴上大致占据不同区间、有无重叠；`n_components=3` 的行为。

**预期结果**：一维判别方向能把类 1 与其余两类分得较开，类 2/类 3 可能有部分重叠（这正说明第二个判别方向 LD 2 仍有价值）；`n_components=3` 超过 \( c-1=2 \) 会触发 sklearn 的报错或警告（具体行为**待本地验证**）。

#### 4.3.5 小练习与答案

1. **练习**：为什么 `ch05.py` 里 LDA 之前的标准化不能省？
   **答案**：与 PCA 同理（u5-l1）：\( S_W \) 混合了量纲悬殊的特征，取逆后数值被大方差特征主导；标准化还让 LDA 与 PCA 的实验处在同一比较基准上。凡基于散布/协方差/距离的方法都应先标准化（u3-l2 的 SVM、u3-l3 的 KNN 同款结论）。
2. **练习**：`LinearDiscriminantAnalysis` 与 `PCA` 的 `fit_transform` 签名差在哪？
   **答案**：`PCA.fit_transform(X)` 不接收标签；`LDA.fit_transform(X, y)` 必须接收 `y`——监督学习的方向学习依赖标签。
3. **练习**：既然 sklearn 的 LDA 自己就能 `predict`，为什么 `ch05.py` 还要在降维后再训练一个逻辑回归？
   **答案**：教学目的——把 LDA 严格当作「特征提取器」，用统一的下游分类器（与 PCA 一节相同的逻辑回归）对比「不同特征空间对分类器的影响」；工程上两种用法都合法，后者（LDA 直接当分类器）在 `ch05.py` 中未演示。

### 4.4 非线性降维：核 PCA 与月牙形数据

#### 4.4.1 概念说明

PCA 和 LDA 都只能做**线性投影**（新特征是原始特征的线性组合）。有一大类数据这招不管用，最经典的就是**月牙形数据**：两条新月交错缠绕，任何一条直线投影都无法把两团分开。

核 PCA（Kernel PCA）的思路与核 SVM（u3-l2）完全同源：

1. 用一个非线性映射 \( \phi \) 把数据送到高维（甚至无穷维）特征空间；
2. **在那个空间里做普通 PCA**；
3. 借助核函数 \( k(x, y) = \phi(x)^T\phi(y) \)，全过程只需计算 \( n \times n \) 的核矩阵 \( K \)，**从不显式构造 \( \phi \)**。

对月牙形数据，恰当核参数下的核 PCA 能把两条月牙「拉直」，在新坐标系里变得线性可分。仓库附带的插图正展示了这个效果：[ch05/figures/05_11.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/figures/05_11.png)（左：原始空间中交错的两条月牙；右：合适的非线性投影后两类分开）；[ch05/figures/05_14.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/figures/05_14.png)（把样本投影到第一主成分后，两类样本在「投影值—样本序号」图上分离成两条带）。**注意：这两张图不由 `ch05.py` 生成**（脚本里没有对应代码），章节归属待确认。

**仓库代码现状（重要）**：本仓库 `ch05.py` 的「Nonlinear dimensionality reduction techniques」一节收录的是 **t-SNE** 可视化（[L630-L695](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L630-L695)），**没有 KernelPCA 的代码**（全仓库检索 `KernelPCA` 零命中）。本模块下面对 KernelPCA 的讲解与代码均为「示例代码」，承接纸书相应主题，供你在本仓库环境中自行运行验证。

核 PCA 中最重要的三个参数：

| 参数 | 作用 | 适用核 |
| --- | --- | --- |
| `kernel` | 核函数类型：`'rbf'`、`'sigmoid'`、`'poly'`、`'linear'`、`'cosine'` | 全部 |
| `gamma` | RBF/sigmoid/poly 核的 \( \gamma \)，控制单样本影响半径 | rbf、sigmoid、poly |
| `coef0` | sigmoid/poly 核的独立项 \( r \)（下式中的截距） | sigmoid、poly |

sigmoid 核定义为：

\[ k(x, y) = \tanh(\gamma \, x^T y + r), \quad r = \texttt{coef0} \]

`coef0` 对 `'rbf'` 核不起作用——所以给 RBF 搜索 `coef0` 是浪费算力，做网格时要分核设计参数网格（见 4.5）。

#### 4.4.2 核心流程

```text
核 PCA（对偶视角的 PCA）:
输入: X (n×d), 核函数 k
1. 计算核矩阵        K_ij = k(x_i, x_j)                  → n×n（注意是样本数×样本数）
2. 中心化核矩阵      K_c = K − 1_n K − K 1_n + 1_n K 1_n （核空间里均值应为 0）
3. 特征分解          K_c α = λ α, 取前 k 个特征值/向量
4. 归一化            α ← α / √λ
5. 投影新样本 x      x ↦ ( Σ_i α_i^j k(x_i, x) )_{j=1..k}
输出: n×k 的核主成分坐标
```

与普通 PCA 最大的结构性差异在第 1 步：矩阵规模从「特征数 × 特征数」（13×13）变成「样本数 × 样本数」（n×n）。这就是 u3-l2 说过的「对偶技巧」——升维不增加计算对象的数量，代价是样本多时核矩阵很大。

#### 4.4.3 源码精读

- [ch05/ch05.py:L630-L636](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L630-L636)：本章「非线性降维技术」一节的目录结构——仓库在这一节实际收录的是 t-SNE。
- [ch05/ch05.py:L641-L655](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L641-L655)：加载 sklearn 自带的手写数字集 `load_digits()`，展示前 4 张 8×8 灰度图；`digits.data.shape` 为 (1797, 64)——1797 个 64 维样本，是 t-SNE 的输入。
- [ch05/ch05.py:L667-L670](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L667-L670)：`TSNE(n_components=2, init='pca', random_state=123)` 后 `fit_transform`。注意两点：t-SNE **没有 `transform` 方法**（新样本必须整体重新拟合），`init='pca'` 用 PCA 结果做初始化以稳定结果——非线方法与线性方法的又一次衔接。t-SNE 的低维嵌入极擅长把 64 维数字数据聚成 10 团用于**可视化**，但它像 PCA 一样不使用标签，且一般不作为下游分类器的特征。
- [ch10/ch10.py:L496-L497](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L496-L497)：仓库中 `make_moons` 的真实用法（第 10 章 DBSCAN 一节）：`X, y = make_moons(n_samples=200, noise=0.05, random_state=0)` 后直接散点图。`make_moons` 生成两条交错的半圆并附带 0/1 标签——本讲核 PCA 实验就用它。

#### 4.4.4 代码实践

**实践目标**：亲眼确认「线性 PCA 拉不直月牙、核 PCA 可以」。

**操作步骤**（示例代码，前半复现仓库 ch10.py 的真实参数，后半为示例）：

```python
# 示例代码
import numpy as np
import matplotlib.pyplot as plt
from sklearn.datasets import make_moons          # 仓库用法见 ch10.py L496
from sklearn.decomposition import PCA, KernelPCA

X, y = make_moons(n_samples=200, noise=0.05, random_state=0)   # 与 ch10.py 相同参数

fig, axes = plt.subplots(1, 3, figsize=(13, 4))
axes[0].scatter(X[:, 0], X[:, 1], c=y, cmap='bwr', s=15)
axes[0].set_title('原始月牙数据')

# 线性 PCA: 第一主成分方向
X_pca = PCA(n_components=2).fit_transform(X)
axes[1].scatter(X_pca[:, 0], X_pca[:, 1], c=y, cmap='bwr', s=15)
axes[1].set_title('线性 PCA')

# 核 PCA: RBF 核
X_kpca = KernelPCA(n_components=2, kernel='rbf', gamma=15).fit_transform(X)
axes[2].scatter(X_kpca[:, 0], X_kpca[:, 1], c=y, cmap='bwr', s=15)
axes[2].set_title('核 PCA (rbf, gamma=15)')
plt.show()
```

**需要观察的现象**：中间图里两类是否依旧缠在一起；右图里两类是否明显分开。

**预期结果**：线性 PCA 后两月牙仍交错；`gamma=15` 的 RBF 核 PCA 通常能把两类大致分开（该参数取值是演示用的经验值，最优值要用 4.5 的网格搜索确定，**待本地验证**）。可把 `gamma` 改成 0.01 与 1000 再看：过小退化为近似线性结果，过大时所有点挤成一团（核矩阵趋近单位矩阵）。

#### 4.4.5 小练习与答案

1. **练习**：200 个样本、2 个特征做核 PCA，需要特征分解的矩阵是多大？换成 200 个样本、1000 个特征呢？
   **答案**：都是 200×200。核 PCA 分解的是 \( n \times n \) 核矩阵（对偶视角），规模只随样本数增长——这正是「升到高维核空间却不增加计算负担」的代价与收益：特征再多矩阵也不变，但样本量大时矩阵变大。
2. **练习**：`gamma` 过大会发生什么？
   **答案**：RBF 核 \( \exp(-\gamma\|x-y\|^2) \) 中，\( \gamma \) 过大时只要 \( x_i \ne x_j \) 核值就趋于 0，核矩阵趋近单位矩阵——每个样本自成一类，投影失去结构（过拟合的极端形态）；过小则所有核值趋近 1，退化为近似线性。这也是必须调参的原因。
3. **练习**：`KernelPCA(kernel='rbf')` 里设置 `coef0=4.0` 有意义吗？
   **答案**：没有。`coef0` 只被 `poly` 与 `sigmoid` 核使用，RBF 核忽略它。设计参数网格时应按核裁剪搜索维度（4.5 的做法）。

### 4.5 用监督指标选核参数：GridSearchCV 包裹 KernelPCA + 逻辑回归

#### 4.5.1 概念说明

核 PCA 引入了 `gamma`、`coef0` 这类超参数，马上面临一个问题：**怎么判断一组核参数好不好？**

- 核 PCA 本身无监督，投影「方差大」不代表「类别可分」；
- 用肉眼看散点图既主观又不可扩展；
- 因此书中的做法（也是本讲的核心方法）是：**把核 PCA 与下游分类器串成流水线，用交叉验证的分类准确率当裁判**，让 `GridSearchCV` 替我们把核参数网格扫一遍。哪个 `gamma` 使下游分类器得分最高，哪个就是好核参数。

这就是「为什么核 PCA 的核参数必须用监督指标来选」的完整逻辑：**核 PCA 的输出好坏没有内在的无监督评分与「类别可分性」对齐，只能借助监督任务的结果来定义好坏**。

实现要点（全部有仓库真实代码可对照，来自第 6 章）：

1. 用 `Pipeline` 把 `KernelPCA` 和 `LogisticRegression` 串起来；
2. 参数网格的键用 **双下划线** 语法 `'步骤名__参数名'`（如 `'kpca__gamma'`），`GridSearchCV` 会把每个候选值注入对应步骤；
3. 交叉验证在**每一折内部**重新拟合整条流水线（包括核 PCA），天然避免用测试折信息构造核矩阵的泄漏问题。

#### 4.5.2 核心流程

```text
网格搜索选核参数:
输入: X, y（月牙数据）, 参数网格
1. pipe = Pipeline([('kpca', KernelPCA(n_components=2)),
                    ('lr',  LogisticRegression())])
2. param_grid = {'kpca__gamma': [...], 'kpca__coef0': [...], 'lr__C': [...]}
   （按核裁剪: rbf 不搜 coef0; sigmoid 同时搜 gamma 与 coef0）
3. gs = GridSearchCV(pipe, param_grid, scoring='accuracy', cv=k)
4. gs.fit(X, y)
5. 读取 gs.best_score_（最佳交叉验证准确率）与 gs.best_params_（最佳参数组合）
输出: 最优核参数 + 对应准确率
```

#### 4.5.3 源码精读

仓库没有「KernelPCA + GridSearchCV」的组合代码，但「流水线 + 参数网格 + 网格搜索」的每个零件在第 6 章都有真实实现，逐项对照如下：

- [ch06/ch06.py:L334-L335](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L334-L335)：`make_pipeline(StandardScaler(), SVC(random_state=1))` —— 把预处理与模型串成流水线的真实写法。把 `SVC` 换成 `KernelPCA` + `LogisticRegression` 两步，就是本讲的流水线。
- [ch06/ch06.py:L339-L343](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L339-L343)：参数网格的**双下划线语法**的真实样例——`'svc__C'`、`'svc__gamma'`、`'svc__kernel'`。注意这里恰好也在网格搜索 `gamma`（RBF 核 SVM 的 gamma），与核 PCA 的 `gamma` 是同一个超参数名字、同一种语义（影响半径）。把这组键名里的 `svc` 换成 `kpca`，就是核 PCA 的网格。
- [ch06/ch06.py:L345-L352](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L345-L352)：`GridSearchCV(estimator=pipe_svc, param_grid=..., scoring='accuracy', refit=True, cv=10)` → `fit` → 打印 `best_score_` 与 `best_params_`。这一段是本讲实践的模板。
- [ch10/ch10.py:L496](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L496)：实验数据的真实来源——`make_moons(n_samples=200, noise=0.05, random_state=0)`。

#### 4.5.4 代码实践

**实践目标**：先跑通「RBF 核 PCA + 逻辑回归」的最小网格搜索，为综合实践的双核对比热身。

**操作步骤**（示例代码，在能联网安装 sklearn 的任意环境运行；数据参数与 ch10.py 一致）：

```python
# 示例代码
from sklearn.datasets import make_moons
from sklearn.decomposition import KernelPCA
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GridSearchCV
from sklearn.pipeline import Pipeline

X, y = make_moons(n_samples=200, noise=0.05, random_state=0)

pipe = Pipeline([('kpca', KernelPCA(n_components=2, kernel='rbf')),
                 ('lr', LogisticRegression())])

param_grid = {                       # rbf 核: 只搜 gamma（coef0 无效, 不进网格）
    'kpca__gamma': [0.001, 0.01, 0.1, 1, 10, 100],
    'lr__C': [1.0, 10.0],
}

gs = GridSearchCV(pipe, param_grid, scoring='accuracy', cv=5)
gs.fit(X, y)
print('最优 5 折准确率:', gs.best_score_)
print('最优参数:', gs.best_params_)
```

**需要观察的现象**：`best_score_` 是否达到 0.9 以上；最优 `gamma` 落在网格的中间还是边缘（落在边缘说明网格范围没罩住最优点，应扩展）。

**预期结果**：`noise=0.05` 的月牙数据比较「干净」，RBF 核 PCA + 逻辑回归的 5 折交叉验证准确率通常在 0.95 上下，最优 `gamma` 在中等量级；具体数值**待本地验证**。

#### 4.5.5 小练习与答案

1. **练习**：参数网格键名 `'kpca__gamma'` 的双下划线是什么规则？
   **答案**：`Pipeline` 中步骤名 + `__` + 该步骤估计器的参数名，`GridSearchCV` 据此把候选值注入正确的步骤。真实样例见 [ch06/ch06.py:L339-L343](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L339-L343) 的 `'svc__C'`、`'svc__gamma'`。
2. **练习**：为什么「看核 PCA 散点图挑 gamma」不如网格搜索可靠？
   **答案**：肉眼看图主观、无法量化、高维时根本看不了；且「看着分开」与「分类器交叉验证得分高」并不总一致。用监督指标 + 交叉验证可复现、可比较、可扩展到多维网格。
3. **练习**：网格搜索时交叉验证的每一折里，核 PCA 会被重新拟合吗？这重要吗？
   **答案**：会。`GridSearchCV` 对每个参数组合、每一折都用**训练折**重新 `fit` 整条流水线（含核 PCA 的核矩阵与特征分解），只用验证折打分——这保证核矩阵不含验证折样本的信息，避免泄漏。这正是把核 PCA 放进 `Pipeline` 再搜索（而不是先对全数据做核 PCA 再交叉验证）的原因。

## 5. 综合实践

**任务**：完整复现「核 PCA + 逻辑回归 + GridSearchCV 搜索 `gamma` 与 `coef0`」的流程，并对比 RBF 与 sigmoid 两种核在同一个月牙形数据上的最优表现。

**前置说明**：本实践为示例代码（仓库 `ch05.py` 未收录 KernelPCA 部分）；数据生成参数取自仓库真实代码 [ch10/ch10.py:L496](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac337afc3c0a91/ch10/ch10.py#L496)，网格搜索写法对照 [ch06/ch06.py:L345-L352](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L345-L352)。

**操作步骤**（示例代码）：

```python
# 示例代码
from sklearn.datasets import make_moons
from sklearn.decomposition import KernelPCA
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GridSearchCV
from sklearn.pipeline import Pipeline

X, y = make_moons(n_samples=200, noise=0.05, random_state=0)   # 同 ch10.py L496

lr = LogisticRegression()

# 核 1: RBF —— 只搜 gamma
pipe_rbf = Pipeline([('kpca', KernelPCA(n_components=2, kernel='rbf')),
                     ('lr', lr)])
grid_rbf = {'kpca__gamma': [0.001, 0.01, 0.1, 1, 10, 100],
            'lr__C': [1.0, 10.0, 100.0]}

# 核 2: sigmoid —— 同时搜 gamma 与 coef0
pipe_sig = Pipeline([('kpca', KernelPCA(n_components=2, kernel='sigmoid')),
                     ('lr', lr)])
grid_sig = {'kpca__gamma': [0.001, 0.01, 0.1, 1, 10],
            'kpca__coef0': [0.0, 1.0, 2.0, 4.0],
            'lr__C': [1.0, 10.0, 100.0]}

for name, pipe, grid in [('rbf', pipe_rbf, grid_rbf),
                         ('sigmoid', pipe_sig, grid_sig)]:
    gs = GridSearchCV(pipe, grid, scoring='accuracy', cv=5)
    gs.fit(X, y)
    print(f'[{name:7s}] 最优 5 折准确率 = {gs.best_score_:.4f}, '
          f'最优参数 = {gs.best_params_}')
```

**需要观察与记录**：

1. 两种核各自的最优 5 折交叉验证准确率与最优参数组合（`gamma`、`coef0`、`C`）。
2. 若最优 `gamma` 落在网格边缘，把该方向扩大一档（如加 1000）再跑，确认真正的最优点。
3. 用两种最优流水线分别 `predict`，统计错误样本数，并在原始月牙散点图上高亮错分点。

**预期结果**（定性，具体数字**待本地验证**）：

- RBF 核通常能拿到约 0.95 以上的准确率——月牙形正是它的强项；
- sigmoid 核通常明显更差（常见 0.6~0.9 区间），一个已知原因是 tanh 核并非对所有 \( \gamma, r \) 组合都正半定，部分参数组合下核矩阵质量差、投影失真；
- 由此得出本讲最重要的工程结论：**核函数与核参数共同决定核 PCA 的成败，且必须用下游监督任务的交叉验证分数来选**。

## 6. 本讲小结

- LDA 是**监督**降维：`fit_transform(X, y)` 传入标签，用类间散布 \( S_B \) 与类内散布 \( S_W \) 构造广义特征值问题 \( S_W^{-1}S_B \)，选「类间散、类内紧」的投影方向；PCA 只看整体方差、不用标签。
- \( \text{rank}(S_B) \le c-1 \) 决定了 LDA 的判别方向数硬上限：wine 三类 → `n_components=2`（仓库 [ch05.py:L597](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py#L597) 用的正是 2），二分类 → 1。
- 手算 LDA 与手算 PCA 骨架同构（特征分解 + 排序 + 投影），差异在分解对象与「按类分组」；[ERRATA:L137-L144](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ERRATA/README.md#L137-L144) 提示了 `np.cov` 无偏归一化与教科书 \( S_W \) 定义的细微差别。
- 线性方法（PCA、LDA）对月牙形数据失效；核 PCA 借核技巧在核空间做 PCA，核矩阵规模是 \( n \times n \)（对偶视角）。
- **仓库事实**：`ch05.py` 的非线性一节收录的是 t-SNE（无 `transform`、只宜可视化），KernelPCA 不在本仓库代码中——本讲 4.4/4.5 的 KernelPCA 代码均为示例代码。
- 核参数（`kernel`/`gamma`/`coef0`）必须通过「KernelPCA + 分类器」流水线的交叉验证得分来选（双下划线键名、`best_score_`/`best_params_` 的真实范式见 [ch06.py:L339-L352](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L339-L352)）；RBF 不使用 `coef0`，网格要按核裁剪。

## 7. 下一步学习建议

- **下一讲 u6-l1（Pipeline、交叉验证与学习/验证曲线）**：本讲综合实践里「流水线 + `cross_val_score` + 参数网格」的零件将在第 6 章被系统化，包括 `StratifiedKFold`、学习曲线与验证曲线的诊断用法。带着综合实践里「网格边缘最优点要扩网格」的体会去读会更有感觉。
- **延伸阅读源码**：[ch06/ch06.py:L326-L360](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L326-L360) 完整读一遍 `make_pipeline` + `GridSearchCV` 的上下文；[ch10/ch10.py:L496-L560](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L496-L560) 看 `make_moons` 数据在第 10 章如何被 KMeans（L513）、层次聚类（L526）、DBSCAN（L553）三种方法「折磨」——月牙形数据还会再登场一次，届时你会看到 DBSCAN 为什么能赢。
- **回头巩固**：把 `ch05.py` 的 L427–L626 完整跑一遍（notebook 形式更方便），对照本讲 4.2/4.3 的行号逐段确认自己能说出每行在做什么。
