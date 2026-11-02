# 感知机从零实现

上一讲（u1-l3）我们已经用 pandas 读入了 `ch02/iris.data`，切出了特征矩阵 `X`（前 100 个样本的萼片长度、花瓣长度两列）和标签数组 `y`（setosa 编为 0、versicolor 编为 1），并从散点图上看到两类样本近似线性可分。本讲就在这份 `X`、`y` 的基础上，逐行阅读仓库中的第一个机器学习模型——`ch02/ch02.py` 里的 `Perceptron` 类。它是全书所有模型的起点：后续的 Adaline、逻辑回归、直到第 11 章的神经网络，都是在这个「净输入 → 激活函数 → 权重更新」骨架上演化出来的。

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `Perceptron` 类的 `fit`、`predict`、`net_input` 三个方法的每一行代码，理解它们各自在算法中的角色。
2. 理解**线性净输入**（加权求和 \( z = \boldsymbol{w}^\top \boldsymbol{x} + b \)）与**阈值激活函数**（单位阶跃函数）为什么共同构成一个线性分类器。
3. 理解感知机权重更新规则的直觉：只有分错的样本才推动权重改变。
4. 会读取 `errors_` 属性绘制「每轮误分类次数」曲线，并调用 `plot_decision_regions` 可视化决策区域。
5. 通过实验确认一个反直觉的现象：对线性可分数据，学习率 \(\eta\) 的大小几乎不影响感知机的收敛快慢——这与下一讲 Adaline 的行为形成鲜明对比。

## 2. 前置知识

本讲只需要少量基础概念，逐一用通俗语言解释：

- **特征向量与标签**：每个样本用几个数值描述，例如一朵鸢尾花用 `[萼片长度, 花瓣长度]` 两个数描述，这就是一个二维特征向量 \(\boldsymbol{x}\)；它的类别（0 或 1）是标签 \(y\)。机器学习分类器的任务就是学习一个从 \(\boldsymbol{x}\) 到 \(y\) 的映射。
- **权重与偏置**：权重 \(\boldsymbol{w}\) 是模型自己学出来的「每个特征有多重要」的系数；偏置 \(b\) 是一个独立可学的常数，相当于「基准分」。两者合起来定义了一条直线（二维时）\( w_1 x_1 + w_2 x_2 + b = 0 \)，这条直线把平面切成两半，称为**决策边界**。
- **线性可分**：如果存在一条直线能把两类样本完全分开，就说数据线性可分。上一讲的散点图显示 setosa 与 versicolor 在「萼片长度—花瓣长度」平面上可以被一条直线分开，所以它们线性可分。
- **激活函数**：把连续的数值压缩/映射成我们想要的形式的函数。感知机用的是最简单的**单位阶跃函数**：输入 \(\ge 0\) 输出 1，否则输出 0。
- **学习率 \(\eta\)（eta）**：每次修正权重时的「步子大小」。太小学得太慢，太大可能过冲——这是对大多数算法而言的直觉；本讲会看到感知机恰好是个有趣的例外。
- **epoch（轮）**：把全部训练样本完整过一遍叫一个 epoch。感知机训练就是反复很多个 epoch，直到不再有分错的样本。
- **NumPy 向量化**：`np.dot`、`np.where` 这类函数可以一次对整个数组做运算，避免写 Python 循环。`Perceptron` 的 `net_input` 和 `predict` 既接受单个样本也接受整个矩阵，靠的就是向量化。

需要的数学只有两处，都用简单公式给出，见到时再解释即可。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ch02/ch02.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py) | 本章全部代码的脚本形式。本讲涉及四段：`Perceptron` 类（L92–L158）、数据读取与 `X`/`y` 切分（L178–L206）、模型训练与 `errors_` 曲线（L227–L236）、`plot_decision_regions` 函数及其调用（L246–L284）。 |
| [ch02/iris.data](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/iris.data) | 鸢尾花数据集的本地副本：150 行、逗号分隔、无表头，列为萼片长、萼片宽、花瓣长、花瓣宽、品种名。前 50 行 setosa、中间 50 行 versicolor、后 50 行 virginica。 |
| [python_environment_check.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/python_environment_check.py) | ch02.py 开头导入的 `check_packages` 所在脚本（u1-l2 已讲过），运行脚本前它会做一遍包版本体检。本讲不展开。 |

按 u1-l1 建立的习惯：**读代码、引用行号用 `ch02.py`；动手实验可打开同目录的 `ch02.ipynb` 对应单元格**，两者内容等价。

## 4. 核心概念与源码讲解

### 4.1 net_input：线性加权求和

#### 4.1.1 概念说明

人工神经元的祖先是 1943 年的 McCulloch–Pitts 神经元：若干输入加权求和，再与阈值比较。感知机（Rosenblatt, 1957）完整继承了这个结构。它分两步：

1. **净输入（net input）**：把特征加权求和，再加上偏置：

\[
z = \boldsymbol{w}^\top \boldsymbol{x} + b = w_1 x_1 + w_2 x_2 + b
\]

2. **激活**：把 \(z\) 送入激活函数得到类别（见 4.2）。

直觉：把 \(-b\) 看作「及格线」，\(z\) 就是「加权总分」。\(\boldsymbol{w}\) 决定各科分数怎么折算，\(b\) 决定总分要超过多少才算「正类」。净输入的符号（而不是大小）决定分类结果——这一点是理解本讲实践现象的关键。

#### 4.1.2 核心流程

```
输入: 样本 x（单个向量 或 整个矩阵 X，形状 [n_samples, n_features]）
      已学到的 w_（长度 = n_features）、b_（标量）
流程: z = X · w_ + b_        # np.dot 一次算完所有样本
输出: z（单个标量 或 长度 n_samples 的数组）
```

`net_input` 不做任何判断，只负责「打分」。判断在 `predict` 里。

#### 4.1.3 源码精读

[net_input 的全部实现只有一行](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L152-L154)：

```python
def net_input(self, X):
    """Calculate net input"""
    return np.dot(X, self.w_) + self.b_
```

这行代码做的事：`np.dot(X, self.w_)` 对每个样本做内积得到加权和，再加上偏置 `self.b_`。由于 NumPy 的广播机制，`X` 是单个样本 `xi`（一维数组）时返回一个标量；`X` 是整个训练矩阵时返回长度为 100 的分数数组——同一个方法同时服务于逐样本更新（4.3）和整片区域预测（4.4），无需写两份代码。

权重与偏置从哪来？在 `fit` 开头初始化，见[权重初始化代码](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L136-L140)：

```python
rgen = np.random.RandomState(self.random_state)
self.w_ = rgen.normal(loc=0.0, scale=0.01, size=X.shape[1])
self.b_ = np.float_(0.)
```

这里用固定种子的随机数发生器 `rgen` 从均值为 0、标准差为 0.01 的正态分布里抽初始权重，偏置从 0 出发。两个设计意图：一是权重不能全零初始化（否则对感知机这种更新规则，全零起点配合对称特征会学不出方向差异；小随机扰动打破对称），二是 `random_state` 种子让每次运行结果可复现。

另外注意[构造函数](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L115-L118)只是保存超参数、不做任何计算：

```python
def __init__(self, eta=0.01, n_iter=50, random_state=1):
    self.eta = eta
    self.n_iter = n_iter
    self.random_state = random_state
```

`eta`（学习率）、`n_iter`（轮数）、`random_state`（随机种子）都是「训练前就定好」的超参数，默认值分别是 0.01、50、1。

#### 4.1.4 代码实践

（示例代码，可粘进 Jupyter 单元格或临时 .py 文件运行）

1. **实践目标**：亲手算一遍净输入，确认 `net_input` 就是加权求和，并体会「预测只依赖 z 的符号」。
2. **操作步骤**：

```python
import numpy as np
from ch02.ch02 import Perceptron   # 若在仓库根目录，也可先 sys.path.insert(0, 'ch02') 再 import

ppn = Perceptron(eta=0.1, n_iter=10)
ppn.w_ = np.array([0.2, 0.5])      # 直接手动赋值，绕过 fit，便于手工核对
ppn.b_ = -3.0

x = np.array([5.1, 1.4])           # 一朵 setosa：萼片长 5.1、花瓣长 1.4
print(ppn.net_input(x))            # 期望 0.2*5.1 + 0.5*1.4 - 3.0 = -0.28
```

3. **需要观察的现象**：打印值与手算的 \(-0.28\) 一致；它小于 0，说明这个（随手设的）权重把这朵花打成了负类区域。
4. **预期结果**：`net_input(x)` 输出 `-0.28`（浮点误差内）。再试 `ppn.w_ * 100` 与 `ppn.b_ * 100` 后重新调用，净输入变为 `-28.0`——数值放大 100 倍，但**符号没变**，即分类结果不会变。这个「尺度不变性」正是 4.3 实践的理论伏笔。

#### 4.1.5 小练习与答案

**练习 1**：若把 `w_` 和 `b_` 同时乘以正数 1000，样本的净输入如何变？模型预测的类别会变吗？

**答案**：净输入整体放大 1000 倍（\(z' = 1000z\)），但符号不变；而感知机的预测只看 `net_input >= 0.0` 这个符号条件，所以类别预测完全不变。

**练习 2**：`net_input` 为什么能同时接受单个样本和整个样本矩阵？

**答案**：因为 `np.dot` 对一维/二维输入都适用，且 `+ self.b_` 靠 NumPy 广播对每个结果都加上同一个标量；这是 NumPy 向量化替代 Python 循环的典型写法。

---

### 4.2 predict：阈值激活函数

#### 4.2.1 概念说明

净输入 \(z\) 是个连续分数，分类需要的是 0/1 标签。感知机用**单位阶跃函数（unit step）**做这个「一刀切」的转换：

\[
\phi(z) = \begin{cases} 1 & z \ge 0 \\ 0 & z < 0 \end{cases}
\]

之所以叫「激活函数」，是沿用神经元的说法：净输入超过阈值，神经元「激活」输出 1；否则不激活输出 0。决策边界正是所有满足 \(z=0\) 的点连成的直线，直线两侧分别被判为 1 类和 0 类。

#### 4.2.2 核心流程

```
输入: X（单样本或矩阵）
流程: z = net_input(X)                    # 4.1 的加权求和
      ŷ = 1 若 z >= 0，否则 0              # np.where 一步完成
输出: ŷ（0/1 标签，与 X 的行数相同）
```

#### 4.2.3 源码精读

[predict 的实现](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L156-L158)：

```python
def predict(self, X):
    """Return class label after unit step"""
    return np.where(self.net_input(X) >= 0.0, 1, 0)
```

`np.where(条件, a, b)` 逐元素判断：条件成立取 `a`（1），否则取 `b`（0）。传入矩阵时一次返回整列预测，传入单个样本时返回单个 0/1。注意边界条件 `>= 0.0`：恰好落在决策线上的点算 1 类。

在 4.3 将看到的训练循环里，`predict` 会先于 `fit` 完成就被调用——这不是笔误：`fit` 内部对每个样本调 `self.predict(xi)`，用「当前模型的预测」与真实标签比对来决定是否更新。

#### 4.2.4 代码实践

（示例代码，接 4.1.4 的会话）

1. **实践目标**：确认阶跃函数的跳变位置，直观感受「决策边界 = \(z=0\) 的直线」。
2. **操作步骤**：

```python
for x2 in [1.0, 3.0, 4.4, 5.0]:
    print(x2, '->', ppn.predict(np.array([5.1, x2])))
```

3. **需要观察的现象**：沿花瓣长度 \(x_2\) 从小到大扫描，预测在某个值处从 0 跳到 1。
4. **预期结果**：以 4.1.4 手设的权重（\(0.2 x_1 + 0.5 x_2 - 3.0 = 0\) 在 \(x_1=5.1\) 时解得 \(x_2 = 4.0\)）为准：\(x_2 < 4.0\) 预测 0，\(x_2 > 4.0\) 预测 1。跳变点 \(x_2 \approx 4.0\) 就是这条决策直线在 \(x_1=5.1\) 处的交点。

#### 4.2.5 小练习与答案

**练习 1**：为什么说决策边界是一条直线？写出它的方程。

**答案**：预测规则为 \(\phi(w_1 x_1 + w_2 x_2 + b) \ge 0\)，切换发生在 \(z = 0\)，即 \(w_1 x_1 + w_2 x_2 + b = 0\)。这是关于 \(x_1, x_2\) 的一次方程，图像是一条直线；高维时推广为超平面。

**练习 2**：把激活函数换成 \(\phi(z) = z\)（恒等），模型还能用同样的方式输出 0/1 吗？

**答案**：不能直接输出。恒等函数返回连续值，需要再设阈值才能得到 0/1；这正是下一讲 Adaline 的处境——它保留连续输出用于计算代价，只在最终 `predict` 时才比较阈值（`>= 0.5`）。这个差异是感知机与 Adaline 的分水岭。

---

### 4.3 fit 与 errors_：权重更新规则与误分类记录

#### 4.3.1 概念说明

`fit` 是感知机的学习过程，核心是**感知机学习规则**。对每个训练样本 \((\boldsymbol{x}^{(i)}, y^{(i)})\)，先算预测 \(\hat{y}^{(i)}\)，然后：

\[
w_j \leftarrow w_j + \eta \left(y^{(i)} - \hat{y}^{(i)}\right) x_j^{(i)}, \qquad
b \leftarrow b + \eta \left(y^{(i)} - \hat{y}^{(i)}\right)
\]

关键在于误差项 \(y^{(i)} - \hat{y}^{(i)}\) 只有三种取值，每种对应一种情形：

| 情形 | \(y - \hat{y}\) | 更新量 | 直觉解释 |
| --- | --- | --- | --- |
| 预测正确 | 0 | \(0\)，权重不动 | 分对了就不动 |
| 真实 1、预测 0 | \(+1\) | \(+\eta \boldsymbol{x}\) | 把净输入往「变大成 1」方向推 |
| 真实 0、预测 1 | \(-1\) | \(-\eta \boldsymbol{x}\) | 把净输入往「变小成 0」方向推 |

即：**只有被分错的样本才推动权重改变**，且修正方向由真实标签与预测的差决定。偏置 \(b\) 可以理解成与一个恒为 1 的输入特征 \(x_0\) 相配的权重，所以它的更新量没有特征因子，只有 \(\pm\eta\)。

一个重要的理论结论（感知机收敛定理）：若数据线性可分，感知机必在有限次更新内收敛到零误分类；且由于预测只依赖 \(z\) 的符号，\(\eta\) 只是把 \((\boldsymbol{w}, b)\) 整体缩放，**学习率不影响收敛所需的更新次数**（一阶近似下；初始随机权重非零会带来轻微差异）。这一点会在下面的实践中亲眼验证，并与下一讲 Adaline 形成对比。

`errors_` 属性记录每个 epoch 中发生更新的次数。因为「有更新」当且仅当「分错」（见上表：误差非零 ⇔ 预测错误），它就是每轮的误分类样本数——这正是监控训练是否收敛的最直接指标。类 docstring 对它的描述见[属性说明](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L105-L112)："Number of misclassifications (updates) in each epoch."

#### 4.3.2 核心流程

```
fit(X, y):
    用固定种子随机初始化 w_（小扰动）、b_ = 0，errors_ = 空列表
    重复 n_iter 轮:
        errors = 0
        顺序遍历每个样本 (xi, target):
            update = eta * (target - predict(xi))     # 误差非零 ⇔ 分错
            w_ += update * xi                          # 权重朝纠错方向移动
            b_ += update                               # 偏置同步移动
            errors += 1 if update != 0 else 0
        errors_.append(errors)                         # 记录本轮误分类数
    返回 self
```

训练数据在上一讲已备好，见[标签与特征切分代码](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac337afc3c0a91/ch02/ch02.py#L201-L206)：`y` 取第 5 列（品种名）前 100 行并映射成 0/1，`X` 取第 1、3 列（萼片长度、花瓣长度）。

#### 4.3.3 源码精读

[fit 的核心训练循环](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L142-L149)：

```python
for _ in range(self.n_iter):
    errors = 0
    for xi, target in zip(X, y):
        update = self.eta * (target - self.predict(xi))
        self.w_ += update * xi
        self.b_ += update
        errors += int(update != 0.0)
    self.errors_.append(errors)
```

逐行解读：外层循环跑 `n_iter` 个 epoch（`_` 表示轮序号用不到）；内层 `zip(X, y)` 让特征与标签成对出现，样本按行顺序逐个通过；`update` 就是公式里的 \(\eta(y^{(i)}-\hat{y}^{(i)})\)；随后两行分别更新权重与偏置（NumPy 的 `+=` 就地修改）；`int(update != 0.0)` 把布尔转成 0/1 累加——更新非零意味着该样本当时被分错。每轮结束把累计值 append 进 `self.errors_`。

注意两个细节：一是即便本轮已零误分类，循环仍会继续跑满 `n_iter` 轮（脚本里训练用了 10 轮，[见训练代码](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L227-L229)），只是后面每轮 `errors=0`、权重不再变化；二是感知机的更新基于阶跃预测，训练曲线路径**不保证单调下降**（某轮改权重可能让别的样本变错），但只要线性可分，最终必归零。

训练后随即绘制 `errors_` 曲线，[绘图代码](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L231-L236)：

```python
plt.plot(range(1, len(ppn.errors_) + 1), ppn.errors_, marker='o')
plt.xlabel('Epochs')
plt.ylabel('Number of updates')
```

横轴从 1 开始编号每个 epoch，纵轴是当轮误分类次数——归零并保持，就说明模型已把两类完全分开。

顺带说明数据入口：脚本[先尝试从 UCI 网址在线读取、失败后回退本地文件](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L178-L190)。本地实验时可直接 `pd.read_csv('ch02/iris.data', header=None)` 离线读取。另注意一个已知小瑕疵：`except HTTPError`（L185）用到的 `HTTPError` 在 ch02.py 中并未 import（可对照 [ch02.ipynb 对应单元](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.ipynb)，同样没有），因此一旦 URL 读取真的抛异常，except 子句本身会再抛 `NameError`。离线读本地文件即可绕开这条路径。

#### 4.3.4 代码实践：学习率 η 对收敛的影响（本讲主实践）

（示例代码，其中 `X`、`y` 的准备复用 u1-l3 / 脚本 L201–L206 的做法）

1. **实践目标**：把书中的 `eta=0.1` 分别改成 `0.0001` 和 `10`，观察每轮误分类数曲线如何变化，验证「线性可分时 η 只缩放权重、不影响收敛」的结论。
2. **操作步骤**：

```python
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from ch02.ch02 import Perceptron

df = pd.read_csv('ch02/iris.data', header=None, encoding='utf-8')
y = np.where(df.iloc[0:100, 4].values == 'Iris-setosa', 0, 1)
X = df.iloc[0:100, [0, 2]].values

fig, ax = plt.subplots(nrows=1, ncols=3, figsize=(12, 4), sharey=True)
for i, eta in enumerate([0.0001, 0.1, 10]):
    ppn = Perceptron(eta=eta, n_iter=10, random_state=1)
    ppn.fit(X, y)
    ax[i].plot(range(1, len(ppn.errors_) + 1), ppn.errors_, marker='o')
    ax[i].set_xlabel('Epochs'); ax[i].set_title(f'eta = {eta}')
ax[0].set_ylabel('Number of updates')
plt.tight_layout(); plt.show()
```

3. **需要观察的现象**：三张图的曲线形态是否相同？各在第几轮归零？`eta=10` 有没有像下一讲 Adaline 那样发散？
4. **预期结果**：三条曲线高度相似——`eta=0.1` 与 `eta=10` 的误分类数几乎逐轮一致并先后归零；`eta=0.0001` 可能前几轮下降稍慢（初始随机权重相对单步更新量更大，见下），但同样在 10 轮内归零，且不会有任何一轮出现「误差爆炸」。原因：更新量 \(\eta(y-\hat{y})\boldsymbol{x}\) 整体随 η 缩放，等价于把 \((\boldsymbol{w}, b)\) 换算到另一个尺度，而预测只看净输入符号（4.1.5 练习 1）。与 4.2.4 不同，`eta` 缩放的只是「修正幅度」，初始 `w_` 是非零随机值，故极小 η 的早期轮次可能略有差异。具体第几轮归零取决于随机初始化与 η 的组合，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`errors += int(update != 0.0)` 统计的到底是什么？为什么它等于本轮误分类样本数？

**答案**：统计本轮发生权重更新的次数。`update = eta * (target - predict)`，由于标签与预测都取 0/1，`target - predict` 只在预测错误时非零（±1）、预测正确时为 0；所以 `update != 0` 当且仅当该样本被分错，更新次数即误分类样本数。

**练习 2**：如果训练数据不是线性可分的（例如换成 versicolor 与 virginica 的花瓣特征），`errors_` 曲线会怎样？

**答案**：永远不会归零。感知机对线性不可分数据不收敛，`errors_` 会在某个非零水平上下波动（权重在边界附近来回摆动）。这解释了书中为什么选 setosa/versicolor 这对线性可分的类别做演示。可自行把 L202–L206 的行区间换成 `50:150` 验证，待本地验证。

**练习 3**：既然 η 不影响感知机收敛，为什么代码里还要保留 η？

**答案**：η 是整个「基于误差的权重更新」框架的通用部件，感知机（阶跃激活）恰好对它不敏感；同样的框架换成连续激活（Adaline、逻辑回归、神经网络）后，η 直接决定梯度步长，太大发散、太小太慢。统一的接口让我们能平滑过渡到后续章节。

---

### 4.4 plot_decision_regions：决策区域可视化

#### 4.4.1 概念说明

训练完成后，我们想知道模型学到的决策边界长什么样。直觉做法：把整个特征平面按一小步长切成网格，对每个网格点调用 `predict` 染上对应类别的底色，再把真实样本叠上去——染色的两片区域就是**决策区域**，两片的交界即决策边界。这个 `plot_decision_regions` 函数是全书复用率最高的工具函数之一（ch03 起还会加 `test_idx` 参数来标注测试样本）。

#### 4.4.2 核心流程

```
plot_decision_regions(X, y, classifier, resolution=0.02):
    1. 准备 marker 与颜色表（按类别数截取）
    2. 在两个特征的 [min-1, max+1] 范围内以 0.02 为步长铺网格（meshgrid）
    3. 把网格点摊平成 [n_points, 2] 的矩阵，整体调用 classifier.predict
    4. 预测结果 reshape 回网格形状，contourf 按类别填色
    5. 用布尔掩码 X[y == cl] 取出每类样本，叠加散点
```

#### 4.4.3 源码精读

先看[函数签名与配色准备](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L246-L251)：

```python
def plot_decision_regions(X, y, classifier, resolution=0.02):
    markers = ('o', 's', '^', 'v', '<')
    colors = ('red', 'blue', 'lightgreen', 'gray', 'cyan')
    cmap = ListedColormap(colors[:len(np.unique(y))])
```

预留了 5 种标记/颜色，实际用 `np.unique(y)` 的类别数截取——两类只用红、蓝。这是函数能通用于多类数据的原因。

网格铺点与整体预测见[网格代码](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L254-L262)：

```python
x1_min, x1_max = X[:, 0].min() - 1, X[:, 0].max() + 1
x2_min, x2_max = X[:, 1].min() - 1, X[:, 1].max() + 1
xx1, xx2 = np.meshgrid(np.arange(x1_min, x1_max, resolution),
                       np.arange(x2_min, x2_max, resolution))
lab = classifier.predict(np.array([xx1.ravel(), xx2.ravel()]).T)
lab = lab.reshape(xx1.shape)
plt.contourf(xx1, xx2, lab, alpha=0.3, cmap=cmap)
```

各方向留 1 个单位（cm）的边距让图不局促；`meshgrid` 生成两个同形状坐标矩阵，`ravel()` 摊平后转置成 `predict` 需要的 `[n_points, 2]` 布局，一次向量化预测所有网格点，再 `reshape` 回网格形状交给 `contourf` 填色。这正是 4.1 中「`net_input`/`predict` 同时支持矩阵输入」的红利：这里没有任何 Python 循环。

最后是[逐类散点叠加](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L265-L272)，用布尔掩码 `X[y == cl]` 选出每类样本画散点。函数在脚本中的调用见[绘图调用](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L277-L284)：

```python
plot_decision_regions(X, y, classifier=ppn)
plt.xlabel('Sepal length [cm]')
plt.ylabel('Petal length [cm]')
plt.legend(loc='upper left')
```

#### 4.4.4 代码实践

（示例代码，接 4.3.4 的会话）

1. **实践目标**：直观看到训练后的决策边界，并体会 `resolution` 参数的作用。
2. **操作步骤**：

```python
from ch02.ch02 import plot_decision_regions

ppn = Perceptron(eta=0.1, n_iter=10, random_state=1).fit(X, y)
plot_decision_regions(X, y, classifier=ppn)
plt.xlabel('Sepal length [cm]'); plt.ylabel('Petal length [cm]')
plt.legend(loc='upper left'); plt.show()

# 再把 resolution 依次换成 0.5 与 2.0 各画一次，观察边界锯齿
```

（`from ch02.ch02 import plot_decision_regions` 导入后重复调用没问题；若改 `resolution`，可通过 `plot_decision_regions(X, y, classifier=ppn, resolution=0.5)` 传参。）

3. **需要观察的现象**：两片染色区之间出现一条近似直线的分界；setosa（红色圆点）与 versicolor（蓝色方点）各占一侧且无交叉。`resolution` 调大后边界出现锯齿甚至色块断开。
4. **预期结果**：决策边界是一条直线（感知机是线性模型），两类样本被完全分开——与 `errors_` 归零互相印证；`resolution=2.0` 时网格点稀疏，`contourf` 插值出的边界明显粗糙。默认 `0.02` 的网格点数量约在数万量级（约 (8/0.02)×(5/0.02) 量级），一次向量化预测瞬间完成，待本地验证具体点数。

#### 4.4.5 小练习与答案

**练习 1**：为什么必须 `ravel()` 摊平再 `.T` 转置，而不能直接把 `xx1`、`xx2` 传给 `predict`？

**答案**：`predict`/`net_input` 期望形状为 `[n_samples, n_features]` 的输入，每行一个样本。`meshgrid` 输出的是两个二维坐标矩阵，直接传入形状不对；摊平成两个一维数组后叠成两行再转置，才得到「每行一个网格点 (x1, x2)」的矩阵。

**练习 2**：这个函数如何做到不修改就支持三分类？

**答案**：颜色与标记表按 `len(np.unique(y))` 截取，`contourf` 与散点循环都由 `np.unique(y)` 驱动；类别变成 3 个时自动启用第三种颜色 `lightgreen` 与标记 `^`。ch03 将复用该函数处理三类鸢尾花。

---

## 5. 综合实践

把本讲四个模块串成一个完整实验：**学习率敏感性 + 收敛验证 + 决策边界可视化**。

（示例代码，完整流程）

1. **实践目标**：对同一份鸢尾花两类数据，比较 3 个学习率下感知机的收敛过程，并可视化每个模型学到的决策区域，最终能用自己的话解释「感知机为什么对 η 不敏感」。

2. **操作步骤**：

```python
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from ch02.ch02 import Perceptron, plot_decision_regions

# ① 数据准备（复用 u1-l3 的结论：本地离线读取）
df = pd.read_csv('ch02/iris.data', header=None, encoding='utf-8')
y = np.where(df.iloc[0:100, 4].values == 'Iris-setosa', 0, 1)
X = df.iloc[0:100, [0, 2]].values

# ② 三种学习率各训练一次，画 errors_ 曲线（4.3 模块）
etas = [0.0001, 0.1, 10]
models = [Perceptron(eta=e, n_iter=10, random_state=1).fit(X, y) for e in etas]

fig, ax = plt.subplots(1, 3, figsize=(12, 4), sharey=True)
for a, m, e in zip(ax, models, etas):
    a.plot(range(1, 11), m.errors_, marker='o')
    a.set_xlabel('Epochs'); a.set_title(f'eta = {e}')
ax[0].set_ylabel('Number of updates'); plt.tight_layout(); plt.show()

# ③ 三条决策区域对比（4.4 模块）
fig, ax = plt.subplots(1, 3, figsize=(15, 4))
for a, m, e in zip(ax, models, etas):
    plot_decision_regions(X, y, classifier=m)
    a.set_title(f'eta = {e}'); a.set_xlabel('Sepal length [cm]')
ax[0].set_ylabel('Petal length [cm]'); plt.tight_layout(); plt.show()

# ④ 数值核对（4.1/4.2 模块）：预测 = 符号判断
z = models[1].net_input(X)                 # 任取一个模型的净输入
assert (models[1].predict(X) == np.where(z >= 0, 1, 0)).all()
```

3. **需要观察的现象**：左图三条 `errors_` 曲线的形态；右图三条决策边界的位置是否几乎重合；`assert` 是否通过。

4. **预期结果**：
   - 三条 `errors_` 曲线高度相似，均在 10 轮内归零且无发散——数据线性可分保证了收敛，η 只整体缩放 \((\boldsymbol{w}, b)\)，不改变由净输入符号决定的预测序列；
   - 三条决策边界近乎同一条直线（可能因初始随机权重的相对影响而轻微平移/偏转，η 越小这种影响越明显）；
   - `assert` 通过，确认 `predict` 就是 `net_input` 的符号判断。
   - 具体第几轮归零、边界是否完全重合，待本地验证。

5. **思考题（对应书中 2.3 节后的讨论）**：如果把同样的三个 η 用在下一讲的 `AdalineGD` 上，`eta=0.1` 的误差曲线会越来越大（发散）——同样是「学习率」，为什么命运不同？提示：Adaline 的激活是恒等函数，代价是平方误差和，η 直接控制梯度步长，步长过大时会冲过谷底越荡越高。这正是 u2-l2 的主题。

## 6. 本讲小结

- `Perceptron` 由三个方法构成最小闭环：`net_input` 算线性加权净输入 \(z = \boldsymbol{w}^\top\boldsymbol{x} + b\)（ch02.py L152–L154），`predict` 用单位阶跃函数把 \(z\) 的符号映射成 0/1（L156–L158），`fit` 用感知机规则逐样本更新权重（L120–L150）。
- 感知机学习规则只在样本被分错时更新：\(w_j \leftarrow w_j + \eta(y^{(i)}-\hat{y}^{(i)})x_j^{(i)}\)，\(b\) 同步加 \(\eta(y^{(i)}-\hat{y}^{(i)})\)；`errors_` 记录每轮更新次数，即误分类样本数，归零意味着训练集已被完全分开。
- 对线性可分数据感知机必收敛（收敛定理），且 η 只缩放 \((\boldsymbol{w}, b)\) 而不改变净输入的符号，故学习率对收敛速度几乎无影响——实验中 `eta=0.0001 / 0.1 / 10` 的 `errors_` 曲线几乎一致。
- `plot_decision_regions` 的套路是「meshgrid 铺网格 → 整体 predict → reshape → contourf 填色 → 布尔掩码叠加真实样本」，它依赖 `predict` 的向量化能力，也是全书最常用的工具函数。
- 感知机的局限：只能输出线性决策边界、对线性不可分数据不收敛、阶跃激活不可导——这三点正是下一讲 Adaline 要解决的入口。

## 7. 下一步学习建议

下一讲（u2-l2「AdalineGD 与梯度下降」）将阅读同一个文件里的 `AdalineGD` 类（ch02.py L307–L388）。建议按以下顺序对照阅读：

1. 先看 `AdalineGD` 的 `activation`（L382–L384）与 `predict`（L386–L388）：激活换成恒等函数、预测阈值挪到 0.5，体会「连续输出使代价函数可定义」这一步的意义。
2. 再看 `fit`（L336–L376）里如何一次性对全量数据求梯度更新（整批梯度下降），并与本讲的逐样本更新对比。
3. 重点复现书中的双学习率实验（L393–L408）：`eta=0.1` 发散、`eta=0.0001` 收敛过慢，与本讲实践 3 形成「同样的 η、相反的命运」的完整对照。
4. 带着问题读：为什么感知机的 η 无关紧要而 Adaline 的 η 至关重要？（答案在 4.3.5 练习 3 与第 5 节思考题的提示里。）
