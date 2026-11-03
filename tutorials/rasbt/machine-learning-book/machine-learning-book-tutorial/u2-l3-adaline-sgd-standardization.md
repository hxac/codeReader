# 特征标准化与 AdalineSGD 随机梯度下降

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚**为什么**上一讲中 `eta=0.1` 的 AdalineGD 会发散，而特征标准化之后 `eta=0.5` 都能平稳收敛。
2. 会用 z-score 公式手写标准化，也能用 scikit-learn 的 `StandardScaler` 完成同样的事，并理解 `fit_transform` 与 `transform` 的分工。
3. 逐行读懂 `AdalineSGD` 类：`fit` 的「按轮-逐样本」双层循环、`_update_weights` 的单样本更新规则、`_shuffle` 的洗牌技巧、`_initialize_weights` 的权重初始化。
4. 理解 `partial_fit` 在线学习接口：为什么它可以「不重置权重」地继续训练。
5. 通过实验对比整批梯度下降（GD）与随机梯度下降（SGD）的收敛曲线差异。

## 2. 前置知识

本讲只依赖上一讲（u2-l2）的内容和一点统计学常识，新概念如下：

- **均值（μ）与标准差（σ）**：均值描述一列数据的「中心位置」；标准差描述数据围绕中心的「散布程度」，标准差越大、数据越分散。
- **特征标准化（standardization，也叫 z-score 标准化）**：把每一列特征变换成「均值 0、标准差 1」，即每个样本减去该列均值、再除以该列标准差。它**不改变**数据的分布形状和两类样本的相对位置，只是换了「刻度尺」。
- **整批梯度下降（batch GD）回顾**：上一讲 AdalineGD 每一轮用**全部**样本算一次梯度、更新一次权重；学习率稍大（0.1）就在未缩放的鸢尾花数据上发散。
- **随机梯度下降（Stochastic Gradient Descent, SGD）**：又称逐样本梯度下降。每轮把训练数据过一遍，但**每看一个样本就立刻更新一次权重**，而不是攒齐全部样本再更新。「随机」指单个样本的梯度只是真实梯度的一个带噪声的估计。
- **在线学习（online learning）**：数据一个接一个（或一批一批）到来，模型边看边学、不用重新从头训练；与之相对的是一次性拿全数据的离线（batch）学习。
- **numpy 花式索引**：`X[r]`（`r` 是整数数组）会按 `r` 中给定的顺序取出对应行，是实现洗牌的关键。

一个需要带着走的直觉：**梯度下降走的是「代价曲面」的坡**。特征尺度不一致时，这个曲面是一个被拉长的椭圆碗——沿长轴走要小步慢走，沿短轴却可以大步跳，两种需求冲突，学习率就难选。标准化把碗「整圆」，各方向坡度接近，一个适中的学习率就能全方向稳定下降。

## 3. 本讲源码地图

本讲全部源码集中在 `ch02/ch02.py`（它是 ch02.ipynb 的等价脚本，读码引行号用 `.py`，动手实验建议用 `.ipynb`，见 u1-l3 的双轨工作流）：

| 文件 | 行段 | 作用 |
| --- | --- | --- |
| [ch02/ch02.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L201-L206) | L201-L206 | 取鸢尾花前 100 个样本，构造特征 `X` 与标签 `y`（上一讲已用过） |
| [ch02/ch02.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L425-L428) | L425-L428 | 手写 z-score 标准化，得到 `X_std` |
| [ch02/ch02.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L433-L451) | L433-L451 | 用标准化数据训练 AdalineGD（eta=0.5）并画决策区域与代价曲线 |
| [ch02/ch02.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L459-L564) | L459-L564 | `AdalineSGD` 类：本讲主角 |
| [ch02/ch02.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L569-L592) | L569-L592 | 训练 AdalineSGD、可视化、以及一行 `partial_fit` 演示 |
| [ch02/iris.data](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/iris.data) | — | 鸢尾花原始数据（150 行、无表头、逗号分隔） |

> 兼容性提醒：`AdalineGD` 与 `AdalineSGD` 里的偏置初始化用了 `np.float_(0.)`（[L354](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L354)、[L542](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L542)），这个别名在 NumPy 2.0 中已被移除。按 u1-l2 用 `environment.yml`（numpy 1.21.2）建的环境可以正常运行；若你用的是新版 numpy，把它改成 `0.` 即可（这是你练习脚本里的改动，**不要**去改仓库源码）。

## 4. 核心概念与源码讲解

### 4.1 特征标准化：把损失曲面「整圆」

#### 4.1.1 概念说明

我们用的两个特征是「萼片长度（cm）」和「花瓣长度（cm）」。虽然单位相同，但两类样本在这两个维度上的**散布程度**差别很大：花瓣长度大约在 1.0~5.1 cm 之间铺开，而萼片长度集中在 4.3~7.0 cm 之间，且方差更小。

上一讲我们说过，整批梯度下降的收敛条件是学习率满足 \( 0 < \eta < 2/\lambda_{max} \)，其中 \( \lambda_{max} \) 是数据协方差矩阵的最大特征值。直觉版解释：

- 代价函数的等高线是一族**椭圆**。特征方差差异越大，椭圆越扁。
- 沿「扁碗」的长轴方向（方差大的维度，这里是花瓣长度）坡度平缓，需要大步走；沿短轴方向坡度陡，只能小步走。一个标量学习率 \( \eta \) 同时服务两个方向：
  - 按陡方向选 \( \eta \)（如 0.0001）→ 平缓方向下降极慢，收敛需要几千轮；
  - 按平缓方向选 \( \eta \)（如 0.1）→ 陡方向一步跨过谷底、越荡越高，代价发散。

标准化的做法是把每列 \( x_j \) 变换成

\[ x_j' = \frac{x_j - \mu_j}{\sigma_j} \]

其中 \( \mu_j \) 是第 \( j \) 列的均值，\( \sigma_j \) 是第 \( j \) 列的标准差。变换后每列均值 0、方差 1，等高线接近**同心圆**，各方向曲率接近，于是可以放心地用一个较大的 \( \eta \)。

scikit-learn 的 `StandardScaler` 就是这个公式的工程化封装：`fit_transform` 在训练数据上**学习**每列的 \( \mu_j \)、\( \sigma_j \) 并完成变换；`transform` 用**已经学到的**参数去变换新数据（比如测试集）——新数据绝不能再自己算一套参数，否则会造成信息泄漏（这个原则到第 6 章讲 Pipeline 时会反复出现）。

> 注意：本书 ch02 的源码**没有**用 `StandardScaler`，而是直接用 numpy 手写了同样的公式；`StandardScaler` 要到 ch03 才登场。本讲先以书中的手写版为主，实践中我们用 `StandardScaler` 复现并验证两者数值一致。

#### 4.1.2 核心流程

标准化的执行流程：

1. 复制原始特征矩阵，避免覆盖 `X`；
2. 对每一列 j：减去该列均值，除以该列标准差（numpy 的 `std` 默认除以 n 而不是 n−1，即**总体标准差**，和 `StandardScaler` 的口径一致）；
3. 用标准化后的 `X_std` 训练模型；
4. 可视化时坐标轴含义从「cm」变成「标准化后的值」。

用伪代码概括整批 GD 在标准化前后的差别：

```text
未标准化:  等高线是扁椭圆 → η 可选范围极窄 (0.0001 收敛慢, 0.1 发散)
标准化后:  等高线近似同心圆 → η=0.5 都能单调收敛
```

#### 4.1.3 源码精读

书中手写的标准化只有三行：

[ch02/ch02.py:L425-L428](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L425-L428) — 先 `np.copy` 一份特征矩阵防止覆盖原始 `X`，然后对第 0 列（萼片长度）和第 1 列（花瓣长度）分别执行「减均值、除标准差」：

```python
# standardize features
X_std = np.copy(X)
X_std[:, 0] = (X[:, 0] - X[:, 0].mean()) / X[:, 0].std()
X_std[:, 1] = (X[:, 1] - X[:, 1].mean()) / X[:, 1].std()
```

接着用标准化数据训练 AdalineGD：

[ch02/ch02.py:L433-L434](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L433-L434) — 学习率大胆地设为 **0.5**（上一讲未缩放数据上 0.1 都发散），轮数只要 20：

```python
ada_gd = AdalineGD(n_iter=20, eta=0.5)
ada_gd.fit(X_std, y)
```

[ch02/ch02.py:L445-L451](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L445-L451) — 画出每轮的 MSE 代价曲线，横轴 Epochs、纵轴 Mean squared error。这条曲线应当**单调下降且很快贴近 0**（对应书中图 2.14 下半部分），与上一讲两条「发散 / 过慢」的曲线形成鲜明对比。

```python
plt.plot(range(1, len(ada_gd.losses_) + 1), ada_gd.losses_, marker='o')
plt.xlabel('Epochs')
plt.ylabel('Mean squared error')
```

注意横轴坐标含义也变了：决策区域图的轴标签从 `'Sepal length [cm]'`（如 [L278](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L278)）换成了 `'Sepal length [standardized]'`（[L438-L439](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L438-L439)），提醒读者数据已经换了刻度。

#### 4.1.4 代码实践

**实践目标**：验证「手写标准化」与 `StandardScaler` 的输出完全一致，并亲眼看到标准化对 AdalineGD 收敛的改善。

**操作步骤**（在 u1-l2 搭建的 pyml-book 环境中，打开 ch02.ipynb 顺序运行到 L434 之后，新增一个单元格；以下为**示例代码**）：

```python
from sklearn.preprocessing import StandardScaler

sc = StandardScaler()
X_std_sklearn = sc.fit_transform(X)

# 1) 验证两种标准化数值一致
print('与手写 X_std 完全一致:', np.allclose(X_std_sklearn, X_std))

# 2) 查看学到的每列 mu 和 sigma
print('均值 mu:', sc.mean_)
print('标准差 sigma:', sc.scale_)

# 3) 对比原始数据与标准化后的统计量
for name, col in [('原始萼片长度', X[:, 0]), ('原始花瓣长度', X[:, 1])]:
    print(f'{name}: mean={col.mean():.3f}, std={col.std():.3f}')
for j in range(2):
    print(f'标准化后第{j}列: mean={X_std[:, j].mean():.3e}, std={X_std[:, j].std():.3f}')
```

**需要观察的现象**：

- `np.allclose` 返回 `True`（因为 numpy 的 `std` 与 `StandardScaler` 都用总体标准差，除以 n）；
- 原始两列的标准差明显不等（花瓣长度那列更大），标准化后两列标准差都是 1.0、均值几乎是 0；
- 再回看上一讲未缩放数据上 `eta=0.1` 发散、与本讲 `eta=0.5` 收敛的对比。

**预期结果**：`allclose` 为 `True`；萼片长度与花瓣长度的原始 std 差异清晰可见；`ada_gd.losses_` 从第一轮起平滑下降。具体数值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：标准化会改变两类样本在散点图上的相对位置关系吗？会改变决策边界的「形状」吗？

**答案**：标准化是对每列做平移加缩放的仿射变换，样本间的相对顺序和两类的大致分布形状不变，散点图只是整体平移并被各向不同程度地拉伸；线性决策边界在原空间依然是直线，映射回原坐标系后**还是一条直线**。它改变的是梯度下降**走到这条边界的过程**（能否快速稳定收敛），而不是模型能表达的分界形式。

**练习 2**：为什么对测试集只能用 `sc.transform(X_test)`，而不能 `sc.fit_transform(X_test)`？

**答案**：`fit` 会重新在测试数据上计算均值和标准差，等于把测试集的统计信息「混进」了预处理，训练与测试的变换口径不一致，还会造成信息泄漏。正确做法是在训练集上 `fit_transform` 学到 \( \mu, \sigma \)，测试集只 `transform`。

**练习 3**：除了标准化，常见的还有「min-max 缩放」（把列压缩到 [0,1]）。对本章的 AdalineGD，两者都能改善收敛吗？

**答案**：都能。两者都把各特征缩到可比的量级，等高线都会更「圆」。区别在于 z-score 标准化对离群点更鲁棒（离群点只影响 μ 和 σ 一次），min-max 会被极端值主导整个区间。书中正文选用的是标准化。

### 4.2 AdalineSGD 的逐样本更新：fit 的双层循环

#### 4.2.1 概念说明

整批 GD 每轮只更新一次权重，方向是「全体样本梯度的平均」，走得很稳但有两个工程缺点：

1. **每一步都要扫全量数据**——数据大到内存放不下时根本没法算；
2. **每轮只走一步**——同样跑 20 轮，整批 GD 只走了 20 步。

SGD 的思路：真实梯度 \( \nabla J \) 等于各单样本梯度的平均，那么**单个样本的梯度就是 \( \nabla J \) 的一个无偏估计**——期望相同，只是带噪声。于是每看一个样本就立刻更新：

\[ \mathbf{w} \leftarrow \mathbf{w} + \eta \cdot 2\, x^{(i)} \left( y^{(i)} - \hat{y}^{(i)} \right), \qquad b \leftarrow b + \eta \cdot 2 \left( y^{(i)} - \hat{y}^{(i)} \right) \]

对照整批版本（上一讲）：

\[ \mathbf{w} \leftarrow \mathbf{w} + \eta \cdot \frac{2}{n} X^{T}(y - \hat{y}) \]

可以发现：整批更新是把 \( n \) 个单样本更新**先求和再除以 n** 后一次性应用；SGD 则是**逐个应用**。对鸢尾花这 100 个样本，SGD 每轮走 100 步、整批只走 1 步，所以同样的轮数下 SGD 的代价下降快得多；代价是单样本梯度有噪声，曲线会**抖**。

#### 4.2.2 核心流程

`fit` 的执行流程（「按轮-逐样本」双层循环）：

```text
初始化权重（w_, b_, w_initialized=True）
for 每一轮 epoch in 1..n_iter:
    如果 shuffle=True: 洗牌 X, y（见 4.3）
    losses = []
    for 每个样本 (xi, target) 依次:      # 内层：逐样本
        用当前权重预测 output
        error = target - output
        w += eta * 2 * xi * error        # 立刻更新
        b += eta * 2 * error
        记录该样本的单点损失 error²
    losses_.append(mean(losses))         # 该轮平均损失
```

一个解读曲线时要注意的细节：SGD 的 `losses_[k]` 是第 k 轮里**每次更新之后立刻算出的单样本损失的平均值**（权重在不断变化中采样）；而整批 GD 的 `losses_[k]` 是第 k 轮**更新前**用全量数据算的 MSE。两者数量级可比，但 SGD 的曲线天然更「毛糙」。

#### 4.2.3 源码精读

`AdalineSGD` 类整体在 [ch02/ch02.py:L459-L564](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L459-L564)，构造函数在 [ch02/ch02.py:L487-L492](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L487-L492) — 相比 AdalineGD 多了两个成员：`shuffle`（是否每轮洗牌，默认 True）和 `w_initialized`（权重是否已初始化的标志，给 `partial_fit` 用）：

```python
def __init__(self, eta=0.01, n_iter=10, shuffle=True, random_state=None):
    self.eta = eta
    self.n_iter = n_iter
    self.w_initialized = False
    self.shuffle = shuffle
    self.random_state = random_state
```

`fit` 主体在 [ch02/ch02.py:L494-L520](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L494-L520) — 先重置权重再进入双层循环：

[ch02/ch02.py:L510-L519](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L510-L519) — 外层每轮先洗牌，内层 `zip(X, y)` 逐样本调用 `_update_weights`，最后把这一轮所有单点损失求平均记入 `losses_`：

```python
self._initialize_weights(X.shape[1])
self.losses_ = []
for i in range(self.n_iter):
    if self.shuffle:
        X, y = self._shuffle(X, y)
    losses = []
    for xi, target in zip(X, y):
        losses.append(self._update_weights(xi, target))
    avg_loss = np.mean(losses)
    self.losses_.append(avg_loss)
return self
```

单步更新在 [ch02/ch02.py:L545-L552](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L545-L552) — 注意 `xi` 是一维向量，`eta * 2.0 * xi * error` 正是上面公式里的逐样本更新，函数返回 \( error^2 \) 供外层统计：

```python
def _update_weights(self, xi, target):
    """Apply Adaline learning rule to update the weights"""
    output = self.activation(self.net_input(xi))
    error = (target - output)
    self.w_ += self.eta * 2.0 * xi * (error)
    self.b_ += self.eta * 2.0 * error
    loss = error**2
    return loss
```

对照整批版本 [ch02/ch02.py:L372-L373](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L372-L373)（`X.T.dot(errors) / X.shape[0]` 正是「单样本更新求平均」的矩阵写法），就能看到两个类在数学上完全同源，只是「先平均再更新」与「边走边更新」的区别。

`net_input` / `activation` / `predict` 与 AdalineGD 逐字相同，见 [ch02/ch02.py:L554-L564](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L554-L564) — 净输入、恒等激活、以及 predict 里 `>= 0.5` 的阈值判断（注意标签是 0/1，所以阈值取 0.5 而不是 0）。

训练调用在 [ch02/ch02.py:L569-L570](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L569-L570) — 用标准化数据、`eta=0.01`、固定 `random_state=1` 保证结果可复现：

```python
ada_sgd = AdalineSGD(n_iter=15, eta=0.01, random_state=1)
ada_sgd.fit(X_std, y)
```

损失曲线绘制在 [ch02/ch02.py:L582-L587](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L582-L587)，纵轴叫 `Average loss`，强调它是轮内平均。

#### 4.2.4 代码实践

**实践目标**：体会「同样一轮，SGD 走了 100 步、整批 GD 只走 1 步」带来的收敛速度差异。

**操作步骤**（示例代码，接在标准化之后运行；假设你已像书中一样定义好 `AdalineGD`、`AdalineSGD` 并构造了 `X_std`、`y`）：

```python
gd = AdalineGD(n_iter=15, eta=0.5).fit(X_std, y)
sgd = AdalineSGD(n_iter=15, eta=0.01, random_state=1).fit(X_std, y)

print('整批 GD 前 5 轮损失:', np.round(gd.losses_[:5], 4))
print('SGD     前 5 轮损失:', np.round(sgd.losses_[:5], 4))
```

**需要观察的现象**：SGD 的损失从第 1、2 轮就降到很低；整批 GD 虽然单调下降，但同轮数下整体「落后」几步。

**预期结果**：SGD 曲线快速下降、随后轻微抖动；GD 曲线平滑单调。具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：对这 100 个样本，`AdalineSGD.fit(n_iter=15)` 一共发生多少次权重更新？`AdalineGD(n_iter=15)` 呢？

**答案**：SGD 每轮每样本更新一次，共 \( 15 \times 100 = 1500 \) 次；整批 GD 每轮只更新一次，共 15 次。这就是「同轮数下 SGD 收敛更快」的直接原因。

**练习 2**：既然单个样本的梯度是噪声估计，为什么 SGD 还能收敛到与整批 GD 相同的极小值附近？

**答案**：因为单样本梯度的**期望**等于整批梯度（无偏）。大量小步累积后噪声相互抵消，期望轨迹沿真实负梯度方向下降。实践中还常配合随训练轮数衰减的学习率，让后期步子变小、把权重「稳定」在极小值附近。

**练习 3**：SGD 的 `losses_` 为什么比整批 GD 的 `losses_` 抖？除了噪声，还有什么实现层面的原因？

**答案**：除噪声外，实现上 SGD 的 `losses_[k]` 是第 k 轮内**每次更新之后**马上计算的单样本误差平方的平均——权重在轮内持续变化，混入了不同「时刻」的权重；而整批 GD 的 `losses_[k]` 是用同一组（该轮起始）权重在全量数据上算出的 MSE，天然平滑。

### 4.3 洗牌与权重初始化：_shuffle 与 _initialize_weights

#### 4.3.1 概念说明

**为什么要每轮洗牌？** 类的 docstring 写得很直白：`Shuffles training data every epoch to prevent cycles`（每轮洗牌以防出现周期性摆动）。这对我们的数据尤其重要：鸢尾花数据是**按类排序**的——前 50 个全是 setosa（标签 0），后 50 个全是 versicolor（标签 1）。如果按固定顺序逐样本更新，每轮都是「先被 50 个 0 类样本拉着往一边走，再被 50 个 1 类样本拉着往另一边走」，权重来回荡，收敛质量差。洗牌把两类样本打散，让每一步的更新方向随机混合，平均掉这种系统性偏移。

**权重初始化的要点**有三：

1. 权重取自均值 0、标准差 0.01 的**小随机数**，而不是全 0——全 0 初始化会让误差项对每个特征「一视同仁」，无法打破对称性（这一章特征间问题不大，但这是为后续多层网络养成的好习惯）；
2. 随机数生成器 `rgen` 被保存为**实例属性**，由 `random_state` 种子控制，保证实验可复现；
3. 设置 `w_initialized = True` 标志，供 `partial_fit` 判断「是否需要（重新）初始化」。

#### 4.3.2 核心流程

```text
_shuffle(X, y):
    r = rgen.permutation(n)   # 生成 0..n-1 的一个随机排列
    return X[r], y[r]         # 花式索引：X 与 y 按同一顺序重排（行对齐不破坏）

_initialize_weights(m):
    rgen = RandomState(random_state)
    w_ = rgen.normal(0, 0.01, m)   # m = 特征数
    b_ = 0.0
    w_initialized = True
```

关键点：`X` 和 `y` 必须用**同一个**排列 `r` 重排，否则特征和标签会错位。

#### 4.3.3 源码精读

[ch02/ch02.py:L533-L536](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L533-L536) — 用 `rgen.permutation` 生成随机排列，再对 `X`、`y` 同时做花式索引，保证特征与标签仍然一一对应：

```python
def _shuffle(self, X, y):
    """Shuffle training data"""
    r = self.rgen.permutation(len(y))
    return X[r], y[r]
```

[ch02/ch02.py:L538-L543](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L538-L543) — 初始化随机数生成器、小随机权重、零偏置，并把 `w_initialized` 置为 True：

```python
def _initialize_weights(self, m):
    """Initialize weights to small random numbers"""
    self.rgen = np.random.RandomState(self.random_state)
    self.w_ = self.rgen.normal(loc=0.0, scale=0.01, size=m)
    self.b_ = np.float_(0.)
    self.w_initialized = True
```

`fit` 中每轮开头调用洗牌的位置见 [ch02/ch02.py:L512-L514](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L512-L514)，构造函数对 `shuffle` 参数的说明见 docstring [ch02/ch02.py:L468-L469](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L468-L469)。

#### 4.3.4 代码实践

**实践目标**：在按类排序的鸢尾花数据上，亲眼看到「不洗牌」对 SGD 的伤害。

**操作步骤**（示例代码）：

```python
ada_noshuffle = AdalineSGD(n_iter=15, eta=0.01, shuffle=False, random_state=1)
ada_noshuffle.fit(X_std, y)

plt.plot(range(1, 16), ada_sgd.losses_, marker='o', label='shuffle=True')
plt.plot(range(1, 16), ada_noshuffle.losses_, marker='s', label='shuffle=False')
plt.xlabel('Epochs'); plt.ylabel('Average loss'); plt.legend(); plt.show()
```

**需要观察的现象**：`shuffle=False` 的曲线更高、更抖（每轮先被 50 个 0 类样本推向一边，再被 50 个 1 类样本推回来）；`shuffle=True` 平稳得多。

**预期结果**：两条曲线都最终下降，但不洗牌版每轮平均损失明显高于洗牌版。具体差异幅度**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `_shuffle` 写成 `return X[np.random.permutation(len(y))], y[np.random.permutation(len(y))]`（两次独立排列），会发生什么？

**答案**：`X` 和 `y` 会按**不同**的顺序重排，特征与标签大面积错位——模型在学习错误配对，损失完全不收敛。洗牌必须用同一个排列 `r` 同时作用于两者。

**练习 2**：为什么权重初始化成很小的随机数（scale=0.01），而不是较大的随机数（如 scale=10）或全 0？

**答案**：全 0 会让所有特征的更新量彼此成相同比例，破坏不了对称性（在多层网络里更致命）；过大的初始权重会让净输入的绝对值很大，饱和型激活函数（后面章节的 sigmoid/tanh）进入梯度极小区，同样学不动。小随机数是两者的稳妥折中。

**练习 3**：`random_state=1` 在这里起什么作用？去掉它（默认 None）会怎样？

**答案**：它给 `RandomState` 提供固定种子，使权重初始化与每轮洗牌顺序完全可复现——同一份代码每次跑结果一致，便于书中图表对照和排查问题。设为 None 时每次运行用不同随机序列，结果会轻微波动（对这种简单凸问题最终收敛位置差别不大，但曲线细节不同）。

### 4.4 partial_fit：不重置权重的在线学习

#### 4.4.1 概念说明

`fit` 有一个隐含约定：每次调用都**从头开始**（重新随机初始化权重）。可现实中数据常常是流式到来的——今天的影评、明天的交易记录。如果每来一批新数据就要在全量数据上重新训练，代价太大。

`partial_fit` 解决这个问题：**保留当前权重，只用新到的数据继续微调**。这正是「在线学习 / 增量学习」的形态，也是 SGD 相对整批 GD 的一大工程优势——整批 GD 的更新公式离不开全量矩阵，天然不适合流式场景。

实现上它依赖两个已经见过的部件：

1. `w_initialized` 标志：首次调用时（权重还不存在）才初始化，之后一律沿用现有权重；
2. `_update_weights`：与 `fit` 内层循环复用同一个单样本更新函数。

顺带一提：这个方法名并非作者随意起的，scikit-learn 自己的估计器（如第 8 章会用的 `SGDClassifier`）也采用同名约定 `partial_fit(X, y)`，语义相同。

#### 4.4.2 核心流程

```text
partial_fit(X, y):
    if 权重尚未初始化: _initialize_weights(...)   # 只发生在第一次
    if 传入多个样本: 逐样本 _update_weights      # 与 fit 内层一致
    else:            直接对单个 (X, y) 更新一次
    return self
```

与 `fit` 的关键差异对比：

| 行为 | `fit` | `partial_fit` |
| --- | --- | --- |
| 权重 | 每次调用都重新初始化 | 仅首次初始化，之后沿用 |
| 洗牌 | 每轮 `_shuffle` | 不洗牌 |
| `losses_` 记录 | 每轮追加平均损失 | 不记录 |

#### 4.4.3 源码精读

[ch02/ch02.py:L522-L531](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L522-L531) — `partial_fit` 的完整实现：先按需初始化，再用 `y.ravel().shape[0] > 1` 区分「一批样本」与「单个样本」两种入参形态，前者循环逐个更新、后者直接调用一次 `_update_weights`：

```python
def partial_fit(self, X, y):
    """Fit training data without reinitializing the weights"""
    if not self.w_initialized:
        self._initialize_weights(X.shape[1])
    if y.ravel().shape[0] > 1:
        for xi, target in zip(X, y):
            self._update_weights(xi, target)
    else:
        self._update_weights(X, y)
    return self
```

书中在训练完 `ada_sgd` 之后用一行代码演示了它：

[ch02/ch02.py:L592](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L592) — 用第一个样本（单个样本走 else 分支）对已训练好的模型做一次增量更新，模拟「新到一个样本就学一下」：

```python
ada_sgd.partial_fit(X_std[0, :], y[0])
```

#### 4.4.4 代码实践

**实践目标**：模拟流式学习——分两批喂数据，验证「partial_fit 接着学」与「fit 一次学全部」效果相当。

**操作步骤**（示例代码）：

```python
import copy

# 方式 A：流式——先用前 50 个样本 fit，再用后 50 个 partial_fit 接着学
stream = AdalineSGD(eta=0.01, n_iter=10, random_state=1)
stream.fit(X_std[:50], y[:50])            # 初始化 + 学前一半
acc_before = (stream.predict(X_std) == y).mean()
stream.partial_fit(X_std[50:], y[50:])    # 不重置权重，继续学后一半
acc_after = (stream.predict(X_std) == y).mean()

# 方式 B：一次性用全部 100 个样本 fit（同轮数）
batch = AdalineSGD(eta=0.01, n_iter=10, random_state=1).fit(X_std, y)
acc_batch = (batch.predict(X_std) == y).mean()

print(f'流式: 只学前一半 acc={acc_before:.2f} -> 再学后一半 acc={acc_after:.2f}')
print(f'一次性全量 fit acc={acc_batch:.2f}')
```

**需要观察的现象**：只学前一半时，由于后一半的 versicolor 样本从未见过，全量预测准确率应明显低于 1；`partial_fit` 补学后一半后准确率上升，接近一次性全量 `fit` 的结果。

**预期结果**：`acc_after` 与 `acc_batch` 都达到 1.00 左右（该数据线性可分）。具体数值**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：如果对一个刚创建、还没 `fit` 过的 `AdalineSGD` 实例直接调用 `partial_fit`，会发生什么？

**答案**：能正常工作。`partial_fit` 开头检查 `w_initialized`，发现是 False 就先调用 `_initialize_weights` 完成初始化，再进行更新。这就是它「按需初始化」的设计意图。

**练习 2**：`partial_fit` 里 `y.ravel().shape[0] > 1` 这个判断是在区分什么？如果调用 `partial_fit(X_std[0, :], y[0])` 时 `y[0]` 恰好是一个长度为 1 的数组而不是标量，会走哪个分支？

**答案**：区分调用者传入的是「一批样本」还是「单个样本」，从而决定是循环逐个更新还是直接更新一次。若 `y[0]` 是长度为 1 的数组，`y.ravel().shape[0]` 等于 1，条件为 False，走 else 分支——`_update_weights(X, y)` 对一维特征向量和长度 1 的目标同样能正确计算，行为一致。

**练习 3**：为什么 `AdalineGD` 不提供 `partial_fit`？

**答案**：整批 GD 的一次更新需要「全部训练样本」的误差来计算平均梯度，这个前提与「只用新到的一小批数据继续学」矛盾；丢掉旧数据直接用新批次算梯度，得到的就不再是原代价函数的梯度了。SGD 天然逐样本更新，才具备在线学习的资格。

## 5. 综合实践

**任务**：把本讲两件事串起来——先用 `StandardScaler` 标准化特征，再分别训练整批 `AdalineGD` 与 `AdalineSGD`，把两者的 epoch-代价曲线画在同一张图上，比较收敛速度与稳定性。

**推荐做法**：打开 `ch02/ch02.ipynb`，从头顺序运行（数据加载 → AdalineGD 定义 → 标准化 → AdalineSGD 训练），此时 `AdalineGD`、`AdalineSGD`、`X`、`y`、`X_std` 都在内存中；然后在 notebook 末尾新增一个单元格，粘贴以下**示例代码**：

```python
from sklearn.preprocessing import StandardScaler

# 1) 用 StandardScaler 重新做标准化，并与书中手写版对质
sc = StandardScaler()
X_std2 = sc.fit_transform(X)
print('StandardScaler 与手写标准化一致:', np.allclose(X_std2, X_std))

# 2) 两个模型都在标准化数据上训练（轮数对齐为 20，便于同图比较）
gd = AdalineGD(n_iter=20, eta=0.5).fit(X_std2, y)
sgd = AdalineSGD(n_iter=20, eta=0.01, random_state=1).fit(X_std2, y)

# 3) 同一张图上画两条代价曲线
plt.plot(range(1, 21), gd.losses_, marker='o', label='Batch GD (eta=0.5)')
plt.plot(range(1, 21), sgd.losses_, marker='s', label='SGD (eta=0.01)')
plt.xlabel('Epochs')
plt.ylabel('Cost (average loss)')
plt.title('Batch GD vs SGD on standardized features')
plt.legend()
plt.show()

# 4) 数值抽查
print('GD  最后 3 轮损失:', np.round(gd.losses_[-3:], 5))
print('SGD 最后 3 轮损失:', np.round(sgd.losses_[-3:], 5))
```

**需要观察与回答的问题**：

1. `np.allclose` 是否为 True？（应一致，见 4.1 的 ddof 讨论）
2. 两条曲线的**形状**差异：哪条第 1~2 轮就降到低位？哪条更平滑、哪条有轻微抖动？
3. 结合 4.2 的「更新次数」分析：同为 20 轮，GD 走了 20 步、SGD 走了 2000 步，这如何解释曲线差距？

**预期结果**：SGD 曲线在第 1~2 轮迅速下降，之后在小幅波动中趋平；整批 GD 曲线平滑单调下降。两者最终损失都接近 0（数据线性可分）。学习率 0.5/0.01 来自书中实验设定，若你改动 `eta`，曲线形态可能变化。具体数值**待本地验证**。

## 6. 本讲小结

- 特征标准化（z-score：减均值、除标准差）把各特征拉到同一量级，代价曲面由「扁椭圆」变「近似同心圆」，使学习率可选范围大幅放宽——书中的手写实现只有三行（L425-L428），效果是 `eta` 从 0.0001 的龟速直接跳到 0.5 的平稳收敛；它与 `StandardScaler` 的 `fit_transform`/`transform` 完全等价，后者学到参数后只对新数据做 `transform`。
- `AdalineSGD` 把整批 GD 的「先对 n 个样本的更新求平均、再走一步」改为「每个样本立刻走一小步」：单样本梯度是无偏的噪声估计，多走小步照样收敛，而且每轮的更新次数从 1 次变为 n 次，收敛更快、代价是可以流式计算。
- `fit` 的双层循环（外层 epoch、内层逐样本）配合 `_shuffle`（同一 `permutation` 同时重排 X 和 y）与 `_initialize_weights`（小随机权重 + 可复现种子 + `w_initialized` 标志）构成完整的训练骨架。
- `partial_fit` 复用 `_update_weights`、仅在权重未初始化时才重置，实现「不从头再练」的在线学习；这也是 scikit-learn 流式估计器的同名约定。
- 解读曲线时注意口径：GD 的 `losses_[k]` 是该轮更新前的全量 MSE，SGD 的是轮内边更新边采样的平均单点损失——所以 SGD 曲线天然更抖。

## 7. 下一步学习建议

- **下一讲（u3-l1）**将进入第 3 章：scikit-learn 的 `fit`/`predict` 范式与逻辑回归。你会发现 `LogisticRegressionGD` 与本章 Adaline 的骨架几乎一样，只是把恒等激活换成 sigmoid——本讲的标准化（`StandardScaler` 正式登场）、`train_test_split` 划分和正则化参数 `C` 都在那里继续深化。
- 建议回看源码：把 [AdalineGD（L307-L388）](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L307-L388) 与 [AdalineSGD（L459-L564）](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L459-L564) 并排对照，亲手列出「哪些方法逐字相同、哪些被改写」，这是检验你是否真的读懂两个类的最好练习。
- 向前埋两个钩子：第 8 章的情感分析会再次出现「生成器 + `partial_fit`」的 out-of-core 流式训练；第 12 章的 PyTorch 里你天天打交道的 `optimizer.step()`，背后正是本讲的 SGD 思想。

---

本讲义覆盖的最小模块：`StandardScaler` fit_transform/transform（含书中手写 z-score 标准化）、shuffle 与权重初始化（`_shuffle`/`_initialize_weights`）、`partial_fit` 单批（在线）更新、`fit` 中按批（逐样本）迭代的循环结构。
