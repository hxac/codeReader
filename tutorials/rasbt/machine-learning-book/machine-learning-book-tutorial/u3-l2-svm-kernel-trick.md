# 最大间隔分类：SVM 与核技巧

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚支持向量机（SVM）的「最大间隔」直觉：它优化的不只是「分对」，而是「分得尽可能开」。
2. 理解软间隔（slack 变量）与惩罚参数 `C` 的关系：`C` 控制「错得越少」和「边界面越宽」之间的权衡。
3. 理解核技巧（kernel trick）的基本直觉：不显式构造高维映射，却能在高维空间中找线性边界，从而解决 XOR 这类线性不可分问题。
4. 区分 `gamma` 与 `C` 各自控制什么：`gamma` 决定单个训练样本的「影响半径」，`C` 决定对违例样本的容忍度。
5. 读懂 `plot_decision_regions` 中 `test_idx` 参数的作用，学会在决策区域图上高亮测试集样本。

## 2. 前置知识

本讲是第 3 章的第二站，直接建立在前面几讲的概念之上：

- **线性决策边界与净输入**（u2-l1 感知机、u3-l1 逻辑回归）：模型先计算 \( z = \mathbf{w}^\top \mathbf{x} + b \)，边界是直线/超平面 \( z = 0 \)。SVM 仍然使用这种线性边界，只是「学法」不同。
- **fit / predict / score 范式**（u3-l1）：scikit-learn 的估计器接口不变，本讲只是把 `LogisticRegression` 换成 `SVC`，其余流程（划分、标准化、训练、画图）完全复用。
- **train_test_split 与 stratify**（u3-l1）：鸢尾花数据按 70%/30% 分层划分，训练集 105 个样本、测试集 45 个样本。这个数字 105 在本讲会被 `test_idx=range(105, 150)` 直接用到。
- **StandardScaler**（u2-l3、u3-l1）：SVM 基于距离和内积，对特征尺度极其敏感，**必须先标准化**。这与梯度下降需要标准化的原因不同，但操作一样：只在训练集上 `fit`。
- **正则化参数 C**（u3-l1）：逻辑回归里的 `C` 是正则化强度的倒数，`C` 越大正则化越弱。SVM 的 `C` 沿用了完全相同的「倒数」语义，这是本讲与上一讲最重要的连接点。
- **过拟合与欠拟合**：训练集表现好、测试集表现差就是过拟合的信号。本讲的 `gamma` 和 `C` 就是直接控制这一点的两个旋钮。

一个新术语先打个比方：**支持向量机像修路**。逻辑回归考虑所有样本的平均损失，而 SVM 只关心「路两边最靠中间的那几块界碑」（支持向量），路要修得离两边的界碑尽可能远（最大间隔）。

## 3. 本讲源码地图

第 3 章所有代码集中在两个等价文件里（u1-l3 讲过双轨组织），本讲引用脚本版 `ch03.py`，因为它带行号、便于精读：

| 文件 | 作用 |
| --- | --- |
| [ch03/ch03.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py) | 第 3 章全部可执行代码：数据准备、各类分类器、可视化 |
| [ch03/ch03.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.ipynb) | 同一内容的 notebook 版（含书中讲解文字），交互实验用 |
| [ch03/figures/03_11.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/figures/03_11.png) 等 | 仓库自带的预渲染结果图（03_11、03_12、03_14、03_15、03_16 对应本讲代码段），不运行代码也能看效果 |

`ch03.py` 内与本讲相关的代码段：

| 行号 | 内容 |
| --- | --- |
| L93-L124 | 加载鸢尾花数据、划分、标准化（上一讲已精读，本讲复用） |
| L160-L201 | `plot_decision_regions` 定义，其中 L188-L201 是 `test_idx` 高亮逻辑 |
| L208-L209 | `X_combined_std` / `y_combined`：训练+测试纵向拼接 |
| L485-L497 | 线性核 SVC 训练与画图（本讲 4.1 节） |
| L505-L507 | `SGDClassifier` 三种损失的替代实现 |
| L516-L541 | 构造 XOR 线性不可分数据并画散点（本讲 4.2 节） |
| L553-L561 | RBF 核 SVC 解决 XOR 问题 |
| L567-L577 | `gamma=0.2` 在鸢尾花上的平缓边界 |
| L582-L592 | `gamma=100.0` 在鸢尾花上的过拟合边界（本讲 4.3 节） |

> 阅读提示：`ch03.py` 是「一竿子到底」的脚本，变量名 `svm` 在 L485、L553、L567、L582 被反复覆盖。从头到尾顺序执行时，后面的一次会覆盖前面的一次——读代码时务必结合行号区分是哪一次实验。

## 4. 核心概念与源码讲解

### 4.1 最大间隔与软间隔：线性 SVM

#### 4.1.1 概念说明

感知机和逻辑回归找到一个能分开两类的边界就满意了，但这样的边界有无穷多条。SVM 的主张是：**在所有能分开两类的超平面中，选离两类样本都最远的那个**。

- **间隔（margin）**：决策边界到最近的训练样本（两类各算一侧）的距离之和。SVM 要最大化这个间隔，所以叫「最大间隔分类」。
- **支持向量（support vectors）**：落在间隔边界上（或闯入间隔内）的那少数几个样本。它们「支撑」起了边界——其他样本再多、再远，只要不越过自己的间隔边界，对边界毫无影响。这也是「支持向量机」名字的由来。
- **软间隔（soft margin）**：真实数据常常不是完美可分的。松弛变量（slack variable）\(\xi_i \ge 0\) 允许个别样本越界，代价计入目标函数。

#### 4.1.2 核心流程

设超平面为 \(\mathbf{w}^\top \mathbf{x} + b = 0\)，可以证明间隔宽度为：

\[ \text{margin} = \frac{2}{\|\mathbf{w}\|} \]

所以「最大化间隔」等价于「最小化 \(\|\mathbf{w}\|\)」。引入松弛变量后，SVM 求解的优化问题（软间隔形式）是：

\[ \min_{\mathbf{w}, b, \xi} \; \frac{1}{2}\|\mathbf{w}\|^2 + C \sum_{i=1}^{n} \xi_i \]

直观读法：

- 第一项 \(\frac{1}{2}\|\mathbf{w}\|^2\) 越小 → 间隔越宽 → 边界越「从容」；
- 第二项 \(C \sum_i \xi_i\) 是所有违例程度之和的惩罚，权重就是 `C`；
- `C` 大 → 违例代价高 → 模型宁可把间隔修窄也要少错 → 边界紧贴样本（偏过拟合）；`C` 小 → 违例便宜 → 间隔更宽更平滑（偏欠拟合）。

用一句话对比上一讲：`C` 的语义与 `LogisticRegression` 的 `C` 一致，都是**正则化强度的倒数**——`C` 越大，对「拟合训练数据」越强硬，对「模型简单」越不看重。

预测时与感知机一样只用净输入的符号，SVM 的决策函数只累加支持向量的贡献：

\[ f(\mathbf{x}) = \sum_{i \in \text{支持向量}} \alpha_i y^{(i)} (\mathbf{x}^{(i)\top} \mathbf{x}) + b \]

非支持向量的系数 \(\alpha_i\) 为 0，这就是「删掉大部分样本、边界不动」的原因（见 4.1.4 的实践）。

#### 4.1.3 源码精读

训练一个线性核 SVM 只需要两行，位于「Maximum margin classification」小节：

```python
svm = SVC(kernel='linear', C=1.0, random_state=1)
svm.fit(X_train_std, y_train)
```

这两行代码见 [ch03/ch03.py:L485-L486](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L485-L486)：用 `kernel='linear'` 指定线性核（即普通线性超平面），`C=1.0` 是软间隔惩罚（也是 scikit-learn 的默认值），`random_state=1` 固定随机性保证可复现。

随后调用 `plot_decision_regions` 并把测试集索引 `range(105, 150)` 高亮出来，见 [ch03/ch03.py:L488-L491](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L488-L491)。对应的效果图仓库已预渲染在 [ch03/figures/03_11.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/figures/03_11.png)。

它使用的数据来自脚本开头（上一讲已精读）：[ch03/ch03.py:L93-L106](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L93-L106) 取鸢尾花的花瓣长度/宽度两列并按 70/30 分层划分，[ch03/ch03.py:L121-L124](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L121-L124) 只在训练集上拟合 `StandardScaler` 再分别变换。SVM 的边界由内积与距离决定，特征尺度直接改变「谁离谁近」，因此这两步标准化对本讲是**必需前置**而非可选项。

两点补充说明：

- **多分类**：这里 `y` 有 0/1/2 三个类。`SVC` 对多分类默认内部采用 OvO（one-vs-one，两两配对训练多个二分类器投票），与上一讲 `LogisticRegression(multi_class='ovr')` 显式指定的 OvR 策略不同。这是 scikit-learn 的实现行为，图中看到的是多条两两边界拼合的结果。
- **大规模数据的替代品**：书中「Alternative implementations」小节给出三种用 `SGDClassifier`（随机梯度下降）实现的等价模型，见 [ch03/ch03.py:L505-L507](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L505-L507)：`loss='perceptron'` 对应感知机、`loss='log'` 对应逻辑回归、`loss='hinge'` 对应线性 SVM。当样本量大到百万级时，`SGDClassifier(loss='hinge')` 逐批更新，比精确求解的 `SVC(kernel='linear')` 快得多。

#### 4.1.4 代码实践

**实践目标**：直观感受「边界只由支持向量决定」以及 `C` 对线性边界的微调作用。

**操作步骤**（示例代码，在 `ch03` 目录下、复用脚本已准备好的变量）：

```python
# 示例代码：先在 ch03.py 中执行到 L124（数据准备完成），或在 notebook 中运行对应单元格
from sklearn.svm import SVC

for C in [0.01, 1.0, 1000.0]:
    svm = SVC(kernel='linear', C=C, random_state=1)
    svm.fit(X_train_std, y_train)
    print(f'C={C}: train acc={svm.score(X_train_std, y_train):.3f}, '
          f'test acc={svm.score(X_test_std, y_test):.3f}, '
          f'n_support={svm.n_support_}')
```

再用 `plot_decision_regions(X_combined_std, y_combined, classifier=svm, test_idx=range(105, 150))` 分别画出三个 `C` 值的决策区域（每个 `C` 画一张图，注意在循环里 `plt.show()`）。

**需要观察的现象**：

1. `svm.n_support_` 是每个类的支持向量个数，三个类加起来远小于 105——绝大多数样本根本没有参与「支撑」边界。
2. `C` 从 0.01 到 1000，训练/测试准确率变化通常很小，边界只发生小幅平移或旋转。

**预期结果**：鸢尾花这两个特征下三类本来就近似线性可分，线性 SVM 各配置都能达到约 0.95 以上的测试准确率；`C` 越大，边界对训练样本的贴合越紧。具体数值**待本地验证**（支持向量个数会因 `C` 而异：`C` 越小、容忍的违例越多，支持向量通常越多）。

#### 4.1.5 小练习与答案

**练习 1**：删掉训练集中那些「远离边界」的样本再重新训练，决策边界会变吗？为什么？
**答案**：基本不变。远离边界的样本不是支持向量，其系数 \(\alpha_i = 0\)，不出现在决策函数 \(f(\mathbf{x})\) 中；只有删到支持向量本身，边界才会移动。

**练习 2**：`C` 极大（如 1e6）和极小（如 1e-4）分别会把模型推向哪种失效模式？
**答案**：`C` 极大近似硬间隔——几乎不容忍任何违例，边界被个别样本牵着走，方差大、易过拟合；`C` 极小时违例几乎不受罚，\(\|\mathbf{w}\|\|\) 最小化占主导，边界宽而平直，可能连训练集都分不好，易欠拟合。

**练习 3**：什么时候应该放弃 `SVC(kernel='linear')` 改用 `SGDClassifier(loss='hinge')`？
**答案**：数据量大（约十万样本以上）或需要在线/流式更新时。`SVC` 精确求解的内存与时间开销随样本数超线性增长，而 `SGDClassifier` 逐小批更新、内存恒定（第 8 章的 out-of-core 学习会用同样的思路）。

### 4.2 不可分数据与核技巧：RBF 核登场

#### 4.2.1 概念说明

线性边界再「从容」也画不出某些形状。经典反例是 **XOR（异或）数据**：两个特征同号为类 0、异号为类 1，样本在平面上形成对角分布的两团——**任何一条直线都无法把两类分开**。

核技巧（kernel trick）的思路是：既然低维空间里线性不可分，就**把样本映射到更高维的空间**，在高维空间中找一个线性超平面。神奇之处在于：我们**从不需要写出这个高维映射 \(\phi(\cdot)\)**，因为求解和预测都只依赖样本之间的内积，而内积可以直接用一个核函数 \(K(\mathbf{x}^{(i)}, \mathbf{x}^{(j)}) = \phi(\mathbf{x}^{(i)})^\top \phi(\mathbf{x}^{(j)})\) 算出来。省掉显式构造高维空间这一步，就是「技巧」二字的意思。

本讲的主角 RBF 核（radial basis function，径向基函数核，也叫高斯核）定义为：

\[ K_{\text{rbf}}(\mathbf{x}^{(i)}, \mathbf{x}^{(j)}) = \exp\left(-\gamma \, \|\mathbf{x}^{(i)} - \mathbf{x}^{(j)}\|^2\right) \]

它度量的是两个样本的「相似度」：距离为 0 时相似度为 1，距离越大相似度按指数衰减到 0。直觉上，RBF 核的决策函数相当于**给每个支持向量套上一个高斯「引力场」**，预测时看新样本落在谁的引力场里。它对应的 \(\phi\) 映射是无限维的，这正是核技巧威力的最好例证——无限维空间无法显式构造，但核函数一行就能算完。

#### 4.2.2 核心流程

用 RBF 核 SVM 解决 XOR 的流程：

1. 生成 200 个二维标准正态随机样本；
2. 按两个特征的符号异或打标签（同号一类、异号一类）；
3. 用 `SVC(kernel='rbf', gamma=..., C=...)` 训练：核函数把样本隐式映射到高维，在高维中找最大间隔超平面；
4. 预测/画图时，每个网格点的类别由它与各支持向量的核相似度加权投票决定。

XOR 标签生成的逻辑可用伪代码表示：

```
label = 1  当且仅当 (特征1 > 0) 与 (特征2 > 0) 恰好一真一假
# 即：一、三象限（同号）→ 类 0；二、四象限（异号）→ 类 1
```

#### 4.2.3 源码精读

XOR 数据的构造只有四行，见 [ch03/ch03.py:L516-L520](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L516-L520)：

```python
np.random.seed(1)
X_xor = np.random.randn(200, 2)
y_xor = np.logical_xor(X_xor[:, 0] > 0,
                       X_xor[:, 1] > 0)
y_xor = np.where(y_xor, 1, 0)
```

这段代码固定随机种子后从标准正态分布抽 200×2 个样本，`np.logical_xor` 对两个「是否大于 0」的布尔判断做异或，再用 `np.where` 把布尔转成 0/1 标签。注意这份数据**没有做标准化**——它本来就服从标准正态分布，两列尺度天然一致。

随后的散点图（见 [ch03/ch03.py:L522-L531](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L522-L531)，效果图 [ch03/figures/03_12.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/figures/03_12.png)）会清楚地显示：类 1（方块）占据二、四象限，类 0（圆点）占据一、三象限，两类在对角方向咬合，任何直线都无法分开。

接着是核技巧的「见证时刻」，见 [ch03/ch03.py:L553-L556](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L553-L556)：

```python
svm = SVC(kernel='rbf', random_state=1, gamma=0.10, C=10.0)
svm.fit(X_xor, y_xor)
plot_decision_regions(X_xor, y_xor,
                      classifier=svm)
```

把 `kernel` 从 `'linear'` 换成 `'rbf'`，同一个 `SVC` 类就获得了非线性边界的能力——这正是 scikit-learn 统一接口的好处。这里 `gamma=0.10`、`C=10.0`，画出的决策区域呈现漂亮的「太极」形状（见 [ch03/figures/03_14.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/figures/03_14.png)），把对角的两个类各自包住。RBF 核在隐式高维空间中找到的线性超平面，投影回二维就是这个弯曲的边界。

#### 4.2.4 代码实践

**实践目标**：亲眼确认「线性核在 XOR 上失败、RBF 核成功」。

**操作步骤**（示例代码）：

```python
# 示例代码：先运行 ch03.py L516-L520 生成 X_xor / y_xor
from sklearn.svm import SVC

svm_lin = SVC(kernel='linear', C=1.0, random_state=1)
svm_lin.fit(X_xor, y_xor)
print('linear kernel:', svm_lin.score(X_xor, y_xor))

svm_rbf = SVC(kernel='rbf', gamma=0.10, C=10.0, random_state=1)
svm_rbf.fit(X_xor, y_xor)
print('rbf kernel   :', svm_rbf.score(X_xor, y_xor))
```

再分别用 `plot_decision_regions(X_xor, y_xor, classifier=...)` 画出两个模型的决策区域。

**需要观察的现象**：

1. 线性核的训练准确率徘徊在 0.5 附近——和抛硬币差不多；
2. 线性核的「决策区域」是一条直线把平面切成两半，与数据的对角结构完全对不上；
3. RBF 核的训练准确率接近 1.0，决策区域呈弯曲的带状包裹两类。

**预期结果**：线性核约 0.45~0.55，RBF 核 ≥ 0.95（具体数值**待本地验证**，随机种子已固定为 1，结果可复现）。

#### 4.2.5 小练习与答案

**练习 1**：用一句话解释为什么核技巧「省掉了高维映射」。
**答案**：SVM 的训练与预测只需要样本间的内积，而核函数直接给出高维空间中的内积 \(K(\mathbf{x}^{(i)}, \mathbf{x}^{(j)}) = \phi(\mathbf{x}^{(i)})^\top \phi(\mathbf{x}^{(j)})\)，因此从头到尾不必显式计算 \(\phi(\mathbf{x})\)。

**练习 2**：RBF 核中 \(\gamma \to 0\) 的极限下，核函数退化成什么？此时边界会怎样？
**答案**：\(\exp(-\gamma d^2) \to 1\)，所有样本两两「完全相似」，核矩阵趋近全 1，模型无法区分任何样本，边界退化为近乎常数（极端欠拟合）。这也是为什么 `gamma` 太小时 RBF 核近似失去非线性能力。

**练习 3**：为什么不直接手工构造高维特征（比如加一列 \(x_1 x_2\)）来解决 XOR？
**答案**：可以，对 XOR 这种低维问题手工构造确实有效（\(x_1 x_2\) 的符号正好区分类别）。但通用情况下我们不知道该构造什么特征，而核函数等价于一次性提供了无限维的现成特征空间，且计算代价只是样本对的核值。

### 4.3 gamma 与 C：控制边界形状的两个旋钮

#### 4.3.1 概念说明

RBF 核把「边界的复杂度」交给了两个超参数，它们管的事情完全不同：

| 参数 | 控制什么 | 直觉比喻 | 过大的后果 |
| --- | --- | --- | --- |
| `gamma` (\(\gamma\)) | 单个训练样本的影响半径 | 每个支持向量周围高斯「引力场」的大小：\(\gamma\) 越大场越窄 | 每个点只影响自己周围一小圈，边界碎成围绕训练样本的「孤岛」→ 过拟合 |
| `C` (\(C\)) | 违例样本的惩罚强度 | 愿意为「分对训练样本」付出多大代价 | 宁可绕弯也要分对每个训练点 → 边界剧烈扭曲 → 过拟合 |

数学上，高斯核 \(\exp(-\gamma d^2)\) 的有效作用宽度约为 \(1/\sqrt{2\gamma}\)：\(\gamma\) 翻倍，影响半径就按 \(\sqrt{2}\) 缩窄。

需要特别记住的分工：**`gamma` 决定「单个样本能影响多远」，`C` 决定「一个样本错分要罚多重」**。两者都会影响过拟合程度，但机制不同——`gamma` 改变的是核函数本身的形状（特征空间的几何），`C` 改变的是优化目标中两項的权重（间隔宽度 vs 违例惩罚）。

另外注意：scikit-learn 的 `SVC` 中 `gamma` 默认值是 `'scale'`（即 \(\gamma = 1/(n_{\text{features}} \cdot \mathrm{Var}(X))\)，随特征数和数据方差自适应）。本书代码训练 RBF 核时总是显式传入 `gamma` 数值，这是好习惯——边界形状对它太敏感，不宜依赖隐式默认。

#### 4.3.2 核心流程

书中在鸢尾花数据上做了一组对照实验，固定 `C=1.0`，只改变 `gamma`：

```
gamma=0.2   →  边界平缓，接近线性，几乎平滑地分开三个类
gamma=100.0 →  边界收紧成围绕各类训练样本的「孤岛」，出现零碎小区域
```

判断过拟合的证据链：

1. 看图：边界是否为贴合个别训练点的扭曲形状、是否出现被单一样本主导的小岛区域；
2. 看数：训练准确率远高于测试准确率（下一节实践会同时输出两者）。

#### 4.3.3 源码精读

第一组配置见 [ch03/ch03.py:L567-L571](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L567-L571)：

```python
svm = SVC(kernel='rbf', random_state=1, gamma=0.2, C=1.0)
svm.fit(X_train_std, y_train)

plot_decision_regions(X_combined_std, y_combined,
                      classifier=svm, test_idx=range(105, 150))
```

`gamma=0.2` 时每个样本的影响半径很大、核相似度衰减缓慢，画出的边界（[ch03/figures/03_15.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/figures/03_15.png)）只比线性边界略微弯曲，三个类的区域连成整片，泛化形态健康。

第二组只把 `gamma` 换成 100，见 [ch03/ch03.py:L582-L586](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L582-L586)：

```python
svm = SVC(kernel='rbf', random_state=1, gamma=100.0, C=1.0)
svm.fit(X_train_std, y_train)

plot_decision_regions(X_combined_std, y_combined, 
                      classifier=svm, test_idx=range(105, 150))
```

此时高斯场窄到几乎只覆盖样本自身，边界（[ch03/figures/03_16.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/figures/03_16.png)）在类 1、类 2 交界处收成紧贴训练样本的细碎区域——典型的过拟合形态。注意两段代码只差一个数字，这就是把 `gamma` 称作「最敏感的旋钮」的原因。

画图所用 `X_combined_std`/`y_combined` 在 [ch03/ch03.py:L208-L209](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L208-L209) 用 `np.vstack`/`np.hstack` 把标准化后的训练集（前 105 个）与测试集（后 45 个）拼成一份，供 `test_idx` 按「全局索引」圈出测试样本。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：对 `gamma` 和 `C` 各取 4 个值组成 4×4 网格，一次看清两个旋钮各自与共同的作用。

**操作步骤**（示例代码）：

```python
# 示例代码：先运行 ch03.py 到 L209，拿到 X_train_std/X_test_std/y_train/y_test
# 以及 X_combined_std/y_combined 和 plot_decision_regions 函数
import matplotlib.pyplot as plt
from sklearn.svm import SVC

gammas = [0.01, 0.1, 1.0, 10.0]
Cs     = [0.1, 1.0, 10.0, 1000.0]

plt.figure(figsize=(14, 14))
for i, g in enumerate(gammas):
    for j, c in enumerate(Cs):
        svm = SVC(kernel='rbf', random_state=1, gamma=g, C=c)
        svm.fit(X_train_std, y_train)
        plt.subplot(4, 4, i * 4 + j + 1)
        plot_decision_regions(X_combined_std, y_combined, classifier=svm)
        plt.title(f'gamma={g}, C={c}  '
                  f'train={svm.score(X_train_std, y_train):.2f} '
                  f'test={svm.score(X_test_std, y_test):.2f}', fontsize=8)
        plt.legend(loc='lower left', fontsize=5)
plt.tight_layout()
plt.show()
```

说明：`plot_decision_regions` 内部用的是 `plt.contourf`/`plt.scatter`（不接收 `ax` 参数），它们总是画在「当前子图」上，所以先用 `plt.subplot` 切到目标格子再调用它即可，16 张小图能正确各归其位。

**需要观察的现象**（逐行扫描网格）：

1. **固定一行（gamma 不变），C 增大**：边界对小 `C` 更平滑，随 `C` 增大逐渐敢于贴向训练样本，扭曲增多；
2. **固定一列（C 不变），gamma 增大**：`gamma=0.01` 一行几乎是线性边界（欠拟合，train/test 准确率都不高）；`gamma=10` 一行出现围绕训练样本的孤岛（train 接近 1.00，test 明显掉下来）；
3. **最过拟合的角落**是「大 gamma + 大 C」，**最欠拟合的角落**是「小 gamma + 小 C」；
4. 中间区域（如 `gamma=0.1, C=1~10`）通常 train/test 都高且差距小，是最健康的配置。

**预期结果**：测试准确率大体呈「中间高、四角低」的形态；`gamma=10` 列的 train 准确率接近 1.00 而 test 准确率明显更低。具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：同事说「`C` 和 `gamma` 都能调过拟合，随便调哪个都行」。这个说法对吗？
**答案**：不对。两者机制不同：`gamma` 改变核函数形状（特征空间几何、单个样本的影响半径），`C` 改变优化目标中间隔项与违例惩罚项的权重。极端 `gamma` 下再怎么调 `C` 也救不回（如 \(\gamma\to\infty\) 时每个点自成一类）；调参时应主要用验证曲线/网格搜索同时搜索两者（第 6 章的方法）。

**练习 2**：某模型训练准确率 100%、测试准确率 70%，`gamma=50`。你会先把哪个参数往哪个方向调？
**答案**：先把 `gamma` 调小（如 0.1~1），让影响半径变大、边界更平滑；也可辅助减小 `C`。两者都是降低模型对个别训练点的敏感度。

**练习 3**：为什么书里的 RBF 实验都显式写 `gamma=0.2`/`gamma=100.0`，而不是省略？
**答案**：`SVC` 的 `gamma` 默认值是 `'scale'`（随特征数与数据方差自适应），不同版本/不同数据下隐式默认行为不可比；显式传值让实验可复现、可对照。

### 4.4 plot_decision_regions 与 test_idx：把测试集「圈」出来

#### 4.4.1 概念说明

`plot_decision_regions` 是全书复用的可视化工具（u2-l1 首次使用）。它回答两个问题：

1. **决策区域长什么样**——把整个特征平面铺成细网格，对每个网格点调用 `classifier.predict` 染色，模型学到的边界一目了然；
2. **训练样本和测试样本各在哪里**——样本以散点画出，`test_idx` 参数让测试集样本额外套上一个空心黑圈，从而在一张图里同时看到「边界」「训练点」「测试点」三样信息，肉眼就能判断测试点是否落在正确的色块里。

#### 4.4.2 核心流程

函数的执行步骤：

1. 按标签类别数截取标记与颜色（最多 5 类）；
2. 用 `np.meshgrid` 在特征平面上以 `resolution=0.02` 的步长铺网格；
3. 把网格展平成 `(N, 2)` 矩阵，整体调用 `classifier.predict`，再 `reshape` 回网格形状；
4. `plt.contourf` 按预测标签填充色块，得到背景决策区域；
5. 逐类 `plt.scatter` 画出训练样本；
6. 若传入 `test_idx`：用花式索引 `X[test_idx, :]` 取出对应行列的样本，用空心黑圈叠画在其上。

#### 4.4.3 源码精读

函数定义起于 [ch03/ch03.py:L160-L176](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L160-L176)（从颜色配置到 `contourf` 渲染背景），其中铺网格与整体预测是核心三行：

```python
xx1, xx2 = np.meshgrid(np.arange(x1_min, x1_max, resolution),
                       np.arange(x2_min, x2_max, resolution))
lab = classifier.predict(np.array([xx1.ravel(), xx2.ravel()]).T)
lab = lab.reshape(xx1.shape)
```

`np.array([xx1.ravel(), xx2.ravel()]).T` 把两个网格展平后堆成 `(N, 2)`（注意转置：堆叠时是 `2×N`，而估计器要求每行一个样本）。`test_idx` 的高亮逻辑见 [ch03/ch03.py:L188-L201](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L188-L201)：

```python
# highlight test examples
if test_idx:
    # plot all examples
    X_test, y_test = X[test_idx, :], y[test_idx]

    plt.scatter(X_test[:, 0],
                X_test[:, 1],
                c='none',
                edgecolor='black',
                ...
                label='Test set')
```

`X[test_idx, :]` 是 numpy 花式索引，`test_idx` 是「在传入的 X 中的行号」。所以 `test_idx=range(105, 150)` 之所以恰好圈出测试集，靠的是两个前提：传入的 `X` 是 [ch03/ch03.py:L208-L209](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L208-L209) 拼接的 `X_combined_std`（训练集在前、测试集在后），且 [ch03/ch03.py:L105-L106](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L105-L106) 的 70/30 划分使测试集恰好是第 105~149 行（150×0.3=45）。**三个条件缺一，黑圈圈出来的就不是测试集**——这是复用该函数时最常见的坑。

#### 4.4.4 代码实践

**实践目标**：验证 `test_idx` 只按行号取样本，与「哪些是测试集」这一语义无关。

**操作步骤**（示例代码）：

```python
# 示例代码：先运行 ch03.py 到 L209，再训练任一模型（如 L567 的 gamma=0.2 版本）
plot_decision_regions(X_combined_std, y_combined,
                      classifier=svm, test_idx=range(0, 45))
plt.xlabel('Petal length [standardized]')
plt.ylabel('Petal width [standardized]')
plt.legend(loc='upper left')
plt.show()
```

**需要观察的现象**：空心黑圈不再围绕右上方的类 2 样本，而是套在左下角类 0 的训练样本上——因为第 0~44 行是训练集（类 0 恰好集中在那个区域）。

**预期结果**：黑圈整体移动到类 0 聚集区域，证明 `test_idx` 只是行号切片。若传入 `None`（默认值），图中不出现黑圈（`if test_idx:` 判断空区间为假）。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：把 `resolution` 从 0.02 改成 0.5，图会怎么变？代价是什么？
**答案**：网格变稀、`predict` 的点数减少，边界色块出现明显锯齿（阶梯感），绘图变快但边界细节丢失；改小（如 0.005）则更平滑但更慢。

**练习 2**：为什么 `lab = classifier.predict(...)` 那一行要做 `.T`？
**答案**：`[xx1.ravel(), xx2.ravel()]` 堆出来的形状是 `(2, N)`（两行分别是两列坐标），而 scikit-learn 要求输入为 `(n_samples, n_features)` 即每行一个样本，转置成 `(N, 2)` 才能整体预测。

**练习 3**：若改用 80/20 划分（`test_size=0.2`），`test_idx` 应传什么？
**答案**：鸢尾花共 150 个样本，训练集 120 个，应传 `range(120, 150)`——前提仍是画图用的 X 是「训练在前、测试在后」的拼接数组。

## 5. 综合实践

设计一个贯穿本讲的对比实验，把「线性/非线性」「gamma」「C」「测试集评估」串成一条线。在 `ch03` 目录下新建自己的脚本或 notebook 单元（不要改动仓库原文件）：

1. **数据准备**：复用 `ch03.py` L93-L124 的流程，得到 `X_train_std`、`X_test_std` 及 `X_combined_std`/`y_combined`（运行到 L209 即可）。
2. **实验一（核的选择）**：在 XOR 数据（L516-L520）上分别训练 `SVC(kernel='linear')` 与 `SVC(kernel='rbf', gamma=0.1, C=10.0)`，记录两者的训练准确率，各画一张决策区域图，用一句话总结「核技巧解决了什么问题」。
3. **实验二（超参数网格）**：完成 4.3.4 的 4×4 网格实验，并把每个配置的 `train/test` 准确率填入下表：

   | gamma \ C | 0.1 | 1.0 | 10.0 | 1000.0 |
   | --- | --- | --- | --- | --- |
   | 0.01 | train=? / test=? | ... | ... | ... |
   | 0.1 | ... | ... | ... | ... |
   | 1.0 | ... | ... | ... | ... |
   | 10.0 | ... | ... | ... | ... |

4. **结论**：根据表格回答三个问题——(a) 最欠拟合的配置是哪个？依据是什么？(b) 最过拟合的配置是哪个？(c) 你会选哪组参数，为什么（不只看 test 准确率，还要看 train-test 差距与边界平滑度）？
5. **加分项**：对你选中的配置，用 `svm.n_support_` 数一数支持向量的个数，并对照决策区域图找到它们的位置（`svm.support_vectors_` 存有坐标，可用 `plt.scatter` 叠画验证「边界由少数样本支撑」）。

预期：表格呈「中间优、四角差」的格局，`gamma=10` 列出现 train≈1.00 而 test 回落的过拟合证据。具体数值**待本地验证**。

## 6. 本讲小结

- SVM 的目标是**最大间隔**：不只分对，还要分得尽量开；边界只由少数**支持向量**决定，删掉其余样本边界不变。
- **软间隔**用松弛变量 \(\xi\) 容忍不可分数据，惩罚权重 `C` 与逻辑回归的 `C` 语义相同——正则化强度的倒数：`C` 大 → 边界贴合训练点（易过拟合），`C` 小 → 边界宽而平滑（易欠拟合）。
- **核技巧**不必显式构造高维映射，只用核函数（如 RBF 高斯核 \(\exp(-\gamma \|\mathbf{x}^{(i)}-\mathbf{x}^{(j)}\|^2)\)）就能在高维空间找线性边界，从而解决 XOR 这类线性不可分问题——代码上只是 `kernel='linear'` 换成 `kernel='rbf'`。
- **`gamma` 与 `C` 分工不同**：`gamma` 决定单个样本的影响半径（`gamma=0.2` 边界近线性，`gamma=100` 边界碎成孤岛），`C` 决定违例代价；两者都要靠 train/test 差距与边界形状共同判断过拟合。
- `plot_decision_regions` 通过**铺网格整体预测**渲染决策区域，`test_idx` 按**行号**圈出测试样本（`range(105, 150)` 成立的前提是传入的训练/测试拼接数组与 70/30 划分）。
- SVM 基于内积与距离，**标准化是硬前提**，且 scaler 只能在训练集上拟合。

## 7. 下一步学习建议

- **下一讲（u3-l3）**：第 3 章的最后一站——决策树（熵/基尼不纯度、`max_depth` 剪枝）与 KNN。建议带着一个问题去读：它们的决策边界和 SVM 一样是「全局一个超平面」吗？（决策树是轴平行的矩形拼接，KNN 完全由局部邻居决定，与 `gamma` 很大时 RBF 的「孤岛」形态颇有神似之处。）
- **回看代码**：对照 [ch03/ch03.py:L485-L592](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L485-L592) 把本讲四段 SVM 代码连起来重读一遍，注意同一个变量 `svm` 被覆盖了四次。
- **向后预告**：本讲的 `gamma`/`C` 是手工网格实验；第 6 章（u6-l2）会学到 `GridSearchCV`/`RandomizedSearchCV` 与嵌套交叉验证，把「挑超参数」变成自动化、无偏的流程，届时可以回来把本讲的 4×4 网格升级成真正的网格搜索。
