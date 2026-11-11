# u11-l1 NeuralNetMLP 前向传播

## 1. 本讲目标

从本讲开始，我们离开 scikit-learn 的「一行建模」世界，进入第 11 章：**用 numpy 从零实现一个多层感知机（MLP）**。本讲聚焦「前向传播」这一半，学完后你应该能够：

1. 读懂 `ch11/neuralnet.py` 中 `NeuralNetMLP` 类的 `__init__` 与 `forward`，说清楚每一行代码。
2. 解释隐藏层权重 `weight_h` 为什么是 `(num_hidden, num_features)` 形状，以及 `forward` 里的 `.T` 转置在做什么。
3. 理解 `sigmoid` 激活函数的公式、值域与导数，以及它和第 2 章阶跃函数、恒等激活的关系。
4. 理解 `int_to_onehot` 标签编码：为什么多分类训练需要把 `7` 变成 `[0,0,0,0,0,0,0,1,0,0]`。
5. 独立追踪一次前向传播中所有矩阵的形状变化，并能用纯 numpy 手工复现。

`backward`（反向传播）与训练循环留给下一讲 `u11-l2`，本讲只在最后指个路。

## 2. 前置知识

本讲假设你已学完 u2 系列讲义（感知机、AdalineGD、AdalineSGD）。需要的概念回顾：

- **净输入（net input）**：感知机与 Adaline 都先算线性净输入 \( z = w^\top x + b \)，即权重与特征的加权求和。当时 `w` 是一个一维向量，因为只有一个输出单元。
- **激活函数**：感知机用阶跃函数（输出 0/1，不可导）；Adaline 训练时用恒等激活（输出连续值）；第 3 章逻辑回归用 sigmoid（输出概率）。本讲的 MLP 在隐藏层和输出层都用 sigmoid。
- **梯度下降的前提是可导**：这是从阶跃走向 sigmoid/恒等激活的根本原因，u2-l2 已详细讨论。
- **矩阵乘法与形状规则**：`(n, k)` 矩阵乘 `(k, m)` 矩阵得到 `(n, m)` 矩阵，中间维度必须相消。本讲全程都在追踪形状。
- **numpy 广播（broadcasting）**：形状 `(n, m)` 的矩阵加形状 `(m,)` 的向量时，向量会自动「复制」到每一行。`forward` 里加偏置就用了这个特性。
- **MNIST 数据集**：手写数字图片集，每张图是 28×28 像素的灰度图，展平成 784 维向量，标签是 0~9 十个数字。第 11 章用它做 10 分类。

一个值得先建立的全局图景：`ch11/ch11.py` 里的 `NeuralNetMLP` 类与独立的 `ch11/neuralnet.py` 内容几乎相同（前者是 notebook 转换产物，后者是独立的模块文件）。本讲以更短、更干净的 `neuralnet.py` 为主要精读对象，`ch11.py` 用来理解模型如何被实例化和调用。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [ch11/neuralnet.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/neuralnet.py) | 独立模型模块：`sigmoid`、`int_to_onehot`、`NeuralNetMLP`（含 `forward` 与 `backward`）。本讲主读文件 |
| [ch11/ch11.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/ch11.py) | notebook 的脚本版：加载 MNIST、实例化模型、训练与评估的完整流程 |
| [ch11/ch11.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/ch11.ipynb) | 与 `ch11.py` 等价的交互式 notebook（原理讲解配图更全） |
| [ch11/README.md](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/README.md) | 第 11 章说明与数据获取指引 |

## 4. 核心概念与源码讲解

### 4.1 int_to_onehot 标签编码

#### 4.1.1 概念说明

MNIST 的标签是整数 0~9，例如 `y = [7, 2, 1, ...]`。但 MLP 的输出层有 **10 个输出单元**，每个单元经 sigmoid 后输出一个 0~1 之间的数。要让「输出」和「标签」可以比较、可以计算误差，两者的形式必须对得上。

**独热（one-hot）编码**就是把整数标签 `$k$` 变成「只有第 `$k$` 位是 1、其余全是 0」的长度为 10 的向量：

```
7  →  [0, 0, 0, 0, 0, 0, 0, 1, 0, 0]
3  →  [0, 0, 0, 1, 0, 0, 0, 0, 0, 0]
```

这样编码后有三个好处：

1. 标签向量与网络输出 `a_out` **形状相同**（都是 `n_examples × 10`）、**取值范围可比**（都在 0~1 之间），可以直接逐元素相减计算均方误差。
2. 每个输出单元对应一个类别，训练目标就是「正确类别的单元输出接近 1，其他单元接近 0」，语义清晰。
3. 回忆 u4-l1 讲过的 `get_dummies`：那是对**特征列**做独热编码；这里是同一思想用在**标签**上，且用手写循环实现，不依赖 pandas。

#### 4.1.2 核心流程

```
输入: y (长度为 n 的整数数组), num_labels (类别数, 如 10)
1. 建一个全 0 矩阵 ary, 形状 (n, num_labels)
2. 对第 i 个样本: ary[i, y[i]] = 1
返回: ary, 每行恰好一个 1
```

注意它要求标签是 **0 开始的连续非负整数**（正好当数组下标用）。MNIST 的 0~9 天然满足。

#### 4.1.3 源码精读

[ch11/neuralnet.py:L12-L18](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/neuralnet.py#L12-L18) —— 用「先建全 0 矩阵、再把第 `val` 列点亮」的两步实现独热编码，`enumerate(y)` 同时拿到行号和标签值：

```python
def int_to_onehot(y, num_labels):

    ary = np.zeros((y.shape[0], num_labels))
    for i, val in enumerate(y):
        ary[i, val] = 1

    return ary
```

这个函数在两处被消费（都发生在训练/评估阶段，本讲先建立印象）：

- [ch11/neuralnet.py:L60-L61](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/neuralnet.py#L60-L61) —— `backward` 里把 `y` 转成 `y_onehot` 才能与 `a_out` 相减求梯度。
- [ch11/ch11.py:L341-L343](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/ch11.py#L341-L343) —— `mse_loss` 用它把标签变成 one-hot 后与 `probas`（即 `a_out`）算均方误差：

```python
def mse_loss(targets, probas, num_labels=10):
    onehot_targets = int_to_onehot(targets, num_labels=num_labels)
    return np.mean((onehot_targets - probas)**2)
```

#### 4.1.4 代码实践

**示例代码**（不依赖 MNIST，可独立运行）：

1. 实践目标：亲手验证 `int_to_onehot` 的行为与边界。
2. 操作步骤：在 `ch11` 目录下（或把 `ch11` 加入 `sys.path`）运行：

```python
import numpy as np
from neuralnet import int_to_onehot   # 需与 neuralnet.py 同目录

y = np.array([7, 2, 1, 0])
enc = int_to_onehot(y, num_labels=10)
print(enc.shape)        # (4, 10)
print(enc[0])           # [0 0 0 0 0 0 0 1 0 0]
print(enc.sum(axis=1))  # [1 1 1 1]  每行恰好一个 1
print(np.argmax(enc, axis=1))  # [7 2 1 0]  argmax 可无损还原标签
```

3. 需要观察的现象：矩阵形状、每行的 1 的位置、每行求和恒为 1。
4. 预期结果：如注释所示；`argmax` 逆变换恰好还原原标签——这解释了为什么训练后预测用 `np.argmax(a_out, axis=1)`（见 [ch11/ch11.py:L353](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/ch11.py#L353)）。
5. 以上输出为依据代码逻辑的推导，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`int_to_onehot(np.array([0, 2, 1]), num_labels=3)` 的输出是什么？

答案：`[[1,0,0], [0,0,1], [0,1,0]]`。第 0 行在第 0 列点亮，第 1 行在第 2 列点亮，第 2 行在第 1 列点亮。

**练习 2**：如果直接用整数标签 `y=7` 与网络输出 `a_out`（形状 `(n,10)`）相减算误差，会出什么问题？

答案：形状对不上（`y` 是 `(n,)`），且整数的「数值大小」与 sigmoid 输出的 0~1 量纲不可比——类别 9 并不比类别 1 「更接近正确」。独热编码把「类别身份」转成「10 个二元目标」，才能逐元素比较。

**练习 3**：若标签里混进 `val = 10` 而 `num_labels = 10`，会发生什么？

答案：`ary[i, 10]` 列下标越界（合法下标是 0~9），numpy 会抛出 `IndexError`。标签必须落在 `[0, num_labels)` 内。

### 4.2 权重矩阵的形状：weight_h 与 weight_out

#### 4.2.1 概念说明

回忆 u2-l1 的感知机：只有一个输出单元，权重 `w` 是长度为 `m+1` 的一维向量。MLP 的隐藏层有 `num_hidden` 个单元（书中取 50 个），**每个单元都想拥有自己的一套权重**。把 50 套权重向量堆起来，就得到一个矩阵。

本项目的约定是：**每一行对应一个目标单元，每一列对应一个输入特征**：

| 参数 | 形状 | 含义 |
|---|---|---|
| `weight_h` | `(num_hidden, num_features)` | 隐藏层权重：第 j 行是第 j 个隐藏单元对 784 个输入特征的权重 |
| `bias_h` | `(num_hidden,)` | 每个隐藏单元一个偏置 |
| `weight_out` | `(num_classes, num_hidden)` | 输出层权重：第 j 行是第 j 个输出单元对 50 个隐藏单元的权重 |
| `weight_out` 的偏置 `bias_out` | `(num_classes,)` | 每个输出单元一个偏置 |

以第 j 个隐藏单元为例，它的净输入正是感知机式加权求和：

\[ z_h^{(j)} = \sum_{i=1}^{num\_features} w_{h}^{(j,i)} \, x_i + b_h^{(j)} \]

权重矩阵的第 j 行 `$\mathbf{w}_h^{(j)}` 就是这个求和的系数向量——所以 `(num_hidden, num_features)` 的形状是「行=单元、列=特征」这一数学布局的直接翻译。4.4 节会看到 `forward` 里出现的 `.T` 正是为了让这个约定与「样本按行排」的输入矩阵相乘时维度相消。

初始化策略也有讲究：

- **权重**用 `rng.normal(loc=0.0, scale=0.1)` 的小随机数。不能用全 0：若所有隐藏单元权重相同，它们的净输入、激活、梯度全部相同，50 个单元将永远同步演化，等价于 1 个单元（**对称性问题**）。小方差（0.1）则让初始净输入不大、sigmoid 不至于一出生就饱和在 0 或 1。
- **偏置**可以安全地置 0，因为它不参与「单元间对称」。
- `np.random.RandomState(random_seed)` 固定随机种子，保证任何人、任何机器上初始化完全一致，实验可复现——与 u2-l3 中 `shuffle` 固定种子同一目的。

#### 4.2.2 核心流程

```
__init__(num_features, num_hidden, num_classes, random_seed=123):
1. rng = RandomState(seed)              # 固定随机源
2. weight_h  ~ N(0, 0.1²), 形状 (num_hidden, num_features)
3. bias_h    = 0,           形状 (num_hidden,)
4. weight_out ~ N(0, 0.1²), 形状 (num_classes, num_hidden)
5. bias_out   = 0,          形状 (num_classes,)
```

#### 4.2.3 源码精读

[ch11/neuralnet.py:L23-L38](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/neuralnet.py#L23-L38) —— 构造函数先建隐藏层参数、再建输出层参数，两组权重共用同一个 `rng`（所以先后的取样顺序也被种子钉死）：

```python
def __init__(self, num_features, num_hidden, num_classes, random_seed=123):
    super().__init__()

    self.num_classes = num_classes

    # hidden
    rng = np.random.RandomState(random_seed)

    self.weight_h = rng.normal(
        loc=0.0, scale=0.1, size=(num_hidden, num_features))
    self.bias_h = np.zeros(num_hidden)

    # output
    self.weight_out = rng.normal(
        loc=0.0, scale=0.1, size=(num_classes, num_hidden))
    self.bias_out = np.zeros(num_classes)
```

注意隐藏层与输出层的权重矩阵**不是方阵**（除非巧合），`weight_h` 连接 784→50，`weight_out` 连接 50→10——**前一层的输出维度就是后一层的输入维度**，这是搭网络时的硬约束。

模型在 [ch11/ch11.py:L293-L295](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/ch11.py#L293-L295) 中针对 MNIST 实例化（28×28=784 个输入特征、50 个隐藏单元、10 个类别）：

```python
model = NeuralNetMLP(num_features=28*28,
                     num_hidden=50,
                     num_classes=10)
```

#### 4.2.4 代码实践

**示例代码**：

1. 实践目标：用肉眼确认四个参数张量的形状与统计量。
2. 操作步骤：

```python
from neuralnet import NeuralNetMLP
model = NeuralNetMLP(num_features=28*28, num_hidden=50, num_classes=10)
print(model.weight_h.shape)     # (50, 784)
print(model.bias_h.shape)       # (50,)
print(model.weight_out.shape)   # (10, 50)
print(model.bias_out.shape)     # (10,)
print(model.weight_h.mean().round(3), model.weight_h.std().round(3))  # ≈ 0.0, ≈ 0.1
```

3. 需要观察的现象：形状逐一对上 4.2.1 的表格；权重的标准差接近 0.1。
4. 预期结果：参数总量 = 50×784 + 50 + 10×50 + 10 = **39,760** 个。再执行一次 `NeuralNetMLP(...)`（相同默认种子 123）打印 `np.allclose(m1.weight_h, m2.weight_h)`，应为 `True`——种子固定了初始化。
5. 待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：把 `num_hidden` 从 50 改成 100，参数总量变成多少？

答案：`weight_h` 100×784=78,400，`bias_h` 100，`weight_out` 10×100=1,000，`bias_out` 10，共 **79,510**。注意隐藏层变宽会同时影响两层参数量。

**练习 2**：为什么权重用随机小数而偏置可以用 0？

答案：随机化是为了打破隐藏单元之间的对称性——全 0（或全相同）权重让所有隐藏单元收到相同信号、得到相同梯度，永远保持一致，网络退化为单单元；偏置不存在这种单元间耦合（它只给净输入加常数），初始化为 0 不引发对称问题。

**练习 3**：`random_seed=123` 写在参数列表里有什么工程意义？

答案：默认固定种子使初始化可复现，任何人重跑代码得到同一组初始权重，实验结果可比较、可调试；想换初始化时显式传入新种子即可。

### 4.3 sigmoid 激活函数

#### 4.3.1 概念说明

激活函数作用在净输入 `$z$` 上，决定单元的「发放」程度。sigmoid 定义为：

\[ \sigma(z) = \frac{1}{1+e^{-z}} \]

它把任意实数压缩到 `$ (0, 1) $` 开区间：`$\sigma(0)=0.5$`，`$z$` 很大时趋近 1，很小时趋近 0，曲线是以 `$(0, 0.5)$` 为中心的 S 形。

放到本书的激活函数演进线索里看：

| 模型 | 激活（训练时） | 可导性 | 出处 |
|---|---|---|---|
| 感知机 | 阶跃函数 | 不可导，只能靠误分类驱动更新 | u2-l1 |
| AdalineGD/SGD | 恒等函数 | 可导，凸 SSE 代价 | u2-l2/u2-l3 |
| 逻辑回归 | sigmoid | 可导，输出可解读为概率 | u3-l1 |
| **本讲 MLP** | **sigmoid（隐藏层与输出层都用）** | 可导，且 `$0<\sigma<1$` 天然匹配 one-hot 目标 | 本章 |

sigmoid 有两个本讲用得上的性质：

1. **光滑可导**，导数有优雅的封闭形式（下一讲反向传播会反复用到）：
   \[ \sigma'(z) = \sigma(z)\,(1-\sigma(z)) \]
   即「激活值 × (1 − 激活值)」，不用重新算指数。
2. **非线性**。没有非线性激活的话，两层线性变换的复合 `$W_{out}(W_h x)$` 仍是线性变换 `$=(W_{out}W_h)x$`，再深的网络也只等价于一层线性模型——sigmoid 的弯曲才让 MLP 能拟合非线性关系（MNIST 数字就不是线性可分的）。

一个先埋伏笔的细节：sigmoid 是**逐单元独立**施加的，10 个输出单元各算各的，彼此不做归一化——所以 `a_out` 每行**并不构成概率分布**（和不等于 1）。这与第 12 章将引入的 softmax 形成对照，4.4.4 的实践里你会亲眼看到。

#### 4.3.2 核心流程

```
sigmoid(z) = 1 / (1 + exp(-z))
```

一行 numpy 代码即可向量化：对整个矩阵 `z` 施加时，`np.exp` 与除法都是逐元素运算，返回同形状矩阵。

#### 4.3.3 源码精读

[ch11/neuralnet.py:L8-L9](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/neuralnet.py#L8-L9) —— 模块级的激活函数，两行实现：

```python
def sigmoid(z):
    return 1. / (1. + np.exp(-z))
```

`ch11.py` 中有一份逐字相同的拷贝（[ch11/ch11.py:L187-L188](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/ch11.py#L187-L188)），因为 notebook 版要自包含、不 import 外部模块；`neuralnet.py` 则是抽出来的可复用模块。

它的调用点在 `forward` 的两处（4.4 节精读）：`a_h = sigmoid(z_h)` 与 `a_out = sigmoid(z_out)`。

#### 4.3.4 代码实践

**示例代码**：

1. 实践目标：建立对 sigmoid 曲线与导数公式的数值直觉。
2. 操作步骤：

```python
import numpy as np
def sigmoid(z):
    return 1. / (1. + np.exp(-z))

z = np.array([-10., -2., 0., 2., 10.])
a = sigmoid(z)
print(np.round(a, 4))
# [0.0000 0.1192 0.5    0.8808 1.    ]

# 数值导数 vs 解析导数 a*(1-a)
eps = 1e-6
num_grad = (sigmoid(z + eps) - sigmoid(z - eps)) / (2 * eps)
ana_grad = a * (1. - a)
print(np.max(np.abs(num_grad - ana_grad)))  # 应是非常小的数, 如 1e-10 量级
```

3. 需要观察的现象：`z=0` 处输出恰为 0.5；两端饱和趋近 0 与 1；数值导数与解析式 `$a(1-a)$` 几乎重合。
4. 预期结果：如注释所示；最大偏差在浮点精度量级（待本地验证）。
5. 这条导数公式 `$\sigma'=a(1-a)$` 在 [ch11/neuralnet.py:L72](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/neuralnet.py#L72) 和 [L99](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/neuralnet.py#L99) 的 `backward` 中以 `a * (1. - a)` 的形式直接出现——现在验证它，下一讲就省力了。

#### 4.3.5 小练习与答案

**练习 1**：`sigmoid(-100)` 大约是多少？这说明什么？

答案：约 `$3.7\times10^{-44}$`，实际上 numpy 中会下溢为 0.0。说明 sigmoid 两端**饱和**：大负输入几乎无输出，且饱和区导数趋近 0（梯度消失），这正是初始化要用小权重（scale=0.1）避免初始就落入饱和区的原因之一。

**练习 2**：为什么隐藏层不能用 u2-l1 感知机的阶跃函数作激活？

答案：阶跃函数几乎处处导数为 0，梯度下降没有可用的梯度信号；sigmoid 光滑可导且导数好算，能支撑下一讲的反向传播。

**练习 3**：若把两层 sigmoid 都去掉（直接用 `z` 传给下一层），整个网络的表达能力会发生什么变化？

答案：`$z_{out} = (x W_h^\top) W_{out}^\top = x (W_h^\top W_{out}^\top)$`，两层线性变换复合成一层线性映射，网络退化成线性模型，无法拟合 MNIST 这类非线性边界。

### 4.4 forward：两步矩阵运算与 a_h、a_out

#### 4.4.1 概念说明

**前向传播**就是「给定一批输入和当前参数，算出网络的输出」。`NeuralNetMLP.forward` 一次算一**批**（而不是单个）样本：输入 `x` 的形状是 `(n_examples, n_features)`，每个样本按行排列，两步矩阵乘法把整批样本同时推过网络。

对单个样本，两层的计算是：

\[ \mathbf{z}_h = \mathbf{W}_h \mathbf{x} + \mathbf{b}_h, \quad \mathbf{a}_h = \sigma(\mathbf{z}_h) \]
\[ \mathbf{z}_{out} = \mathbf{W}_{out} \mathbf{a}_h + \mathbf{b}_{out}, \quad \mathbf{a}_{out} = \sigma(\mathbf{z}_{out}) \]

与 u2 章的单层模型对照：结构完全同构（线性净输入 + 激活），只是 (1) 权重从向量变矩阵（多个单元），(2) 算完一层把**激活值而非原始输入**喂给下一层，(3) 串联两层。`forward` 返回**两个**值 `a_h, a_out`：`a_out` 是最终输出，`a_h` 是隐藏层激活——它会被 `backward` 复用来算梯度（见 [ch11/neuralnet.py:L80](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/neuralnet.py#L80)），这也是前向传播要把它一并返回的原因。

#### 4.4.2 核心流程

以 MNIST 全量批次（`n` 个样本）为例的形状流水：

```
x            (n, 784)                        输入批量
  │ np.dot(x, weight_h.T)
z_h          (n, 50)   = (n,784)·(784,50)    隐藏层净输入
  │ + bias_h (50,)     → 广播到每一行
  │ sigmoid
a_h          (n, 50)                         隐藏层激活 ──┐ 返回值 1
  │ np.dot(a_h, weight_out.T)                             │
z_out        (n, 10)   = (n,50)·(50,10)      输出层净输入 │
  │ + bias_out (10,)   → 广播                             │
  │ sigmoid                                               │
a_out        (n, 10)                          10 类得分 ──┘ 返回值 2
```

关键机制两条：

- **`.T` 转置**：`weight_h` 按「行=单元」存储成 `(50, 784)`，而输入按「行=样本」排成 `(n, 784)`。`(n,784) @ (784,50)` 要求中间维度相消，所以乘 `weight_h.T`（`(784,50)`）。转置只是**视图**，不复制内存、代价极低。
- **偏置广播**：`(n,50) + (50,)` 触发广播，同一组偏置加到每个样本的行上，等价于逐样本 `$z_j + b_j$`。

#### 4.4.3 源码精读

[ch11/neuralnet.py:L40-L52](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/neuralnet.py#L40-L52) —— `forward` 全文，注释里贴心地标注了每步维度（隐藏层、输出层各两行，先线性、后激活）：

```python
def forward(self, x):
    # Hidden layer
    # input dim: [n_examples, n_features] dot [n_hidden, n_features].T
    # output dim: [n_examples, n_hidden]
    z_h = np.dot(x, self.weight_h.T) + self.bias_h
    a_h = sigmoid(z_h)

    # Output layer
    # input dim: [n_examples, n_hidden] dot [n_classes, n_hidden].T
    # output dim: [n_examples, n_classes]
    z_out = np.dot(a_h, self.weight_out.T) + self.bias_out
    a_out = sigmoid(z_out)
    return a_h, a_out
```

真实调用点（如何被使用）：

- [ch11/ch11.py:L350-L354](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/ch11.py#L350-L354) —— 对整个验证集一次前向，`_` 丢弃 `a_h`，`argmax(axis=1)` 把每行 10 个得分变成预测标签：

```python
_, probas = model.forward(X_valid)
mse = mse_loss(y_valid, probas)

predicted_labels = np.argmax(probas, axis=1)
```

- [ch11/ch11.py:L368](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/ch11.py#L368) —— `compute_mse_and_acc` 里按 minibatch 前向后累计正确数与损失。
- [ch11/ch11.py:L408](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/ch11.py#L408) —— 训练循环里 `a_h, a_out = model.forward(X_train_mini)`，两个返回值随即都交给 `backward`。前向的两个产物在训练中一个都不浪费。

另外注意 MNIST 输入的预处理（[ch11/ch11.py:L107-L109](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/ch11.py#L107-L109) 与 [L119](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/ch11.py#L119)）：`fetch_openml('mnist_784')` 取回 70000×784 数据，再线性缩放到 `[-1, 1]`——像素 0 变 -1、255 变 +1，让输入量纲与 sigmoid 的敏感区间匹配（这与 u2-l3 的标准化动机一致，只是手段是固定公式而非统计量）。

#### 4.4.4 代码实践

本讲的正式实践任务，全部在本地完成、不需要下载 MNIST：

1. **实践目标**：用小尺寸输入把 `forward` 的每一步形状亲手过一遍，并考察 `a_out` 是否是概率分布。
2. **操作步骤**（**示例代码**，在 `ch11` 目录下运行）：

```python
import numpy as np
from neuralnet import NeuralNetMLP, sigmoid, int_to_onehot

np.random.seed(0)
x = np.random.randn(5, 3)          # 5 个样本, 每个 3 个特征
y = np.array([0, 1, 2, 1, 0])      # 5 个标签

model = NeuralNetMLP(num_features=3, num_hidden=50, num_classes=10)

# 手工复现 forward 的两步, 对照类方法
z_h  = np.dot(x, model.weight_h.T) + model.bias_h
a_h_manual = sigmoid(z_h)
z_out = np.dot(a_h_manual, model.weight_out.T) + model.bias_out
a_out_manual = sigmoid(z_out)

a_h, a_out = model.forward(x)

print(a_h.shape, a_out.shape)                # (5, 50) (5, 10)
print(np.allclose(a_h, a_h_manual))          # True
print(np.allclose(a_out, a_out_manual))      # True
print('每行和:', a_out.sum(axis=1))
print('每行范围:', a_out.min(axis=1), a_out.max(axis=1))
```

3. **需要观察的现象**：`a_h` 形状 `(5, 50)`、`a_out` 形状 `(5, 10)`；手工矩阵乘法与 `forward` 完全一致；`a_out` 每个元素在 0.4~0.6 之间。
4. **预期结果**：**未训练**网络的 `a_out` 每行之和**不等于 1**，而是聚在 **5 附近**（10 个单元 × 各自约 0.5）。原因：初始权重小（scale=0.1），`z_h`、`z_out` 都接近 0，sigmoid(≈0)≈0.5；且 sigmoid 逐单元独立施加、**没有归一化**，所以行和不保番为 1。训练后由于 MSE 逼迫 `a_out` 逼近和为 1 的 one-hot 目标，行和才会逐渐移向 1。预测时 `argmax(axis=1)` 只看 10 个得分的**相对大小**，不要求和为 1，所以这不影响分类。此数值预期由初始化与 sigmoid 性质推导，**待本地验证**。
5. 补充一步：`int_to_onehot(y, 10)` 得到 `(5, 10)`，与 `a_out` 同形，可直接算 `np.mean((int_to_onehot(y, 10) - a_out)**2)` 感受「初始 MSE ≈ 0.25~0.3」的量级（每个单元差约 0.5，平方约 0.25）。

#### 4.4.5 小练习与答案

**练习 1**：输入批量 `x` 形状为 `(32, 784)` 时，`forward` 返回的 `a_h`、`a_out` 形状分别是什么？

答案：`a_h` 为 `(32, 50)`，`a_out` 为 `(32, 10)`。批量大小只出现在第一维，后续所有形状不变——这正是「按批前向」的向量化优势。

**练习 2**：`np.dot(x, self.weight_h.T)` 为什么必须转置？去掉 `.T` 会怎样？

答案：`weight_h` 是 `(50, 784)`、`x` 是 `(n, 784)`，直接 `(n,784) @ (50,784)` 内层维度 784 ≠ 50，numpy 抛 `ValueError`。转置后 `(n,784) @ (784,50) → (n,50)`，维度相消。

**练习 3**：`bias_h` 形状是 `(50,)`，它是怎么加进 `(n, 50)` 的矩阵的？

答案：numpy 广播规则把 `(50,)` 视作 `(1, 50)` 再沿第 0 维复制到 `n` 行——每个样本都加上**同一组**隐藏层偏置，与逐样本 `$+b$` 的数学定义等价。

**练习 4**：为什么 `forward` 要返回 `a_h`，而预测时又常常把它丢弃（`_, probas = model.forward(...)`）？

答案：`a_h` 是反向传播的必需中间量（`backward` 用它计算输出层权重梯度，见 [ch11/neuralnet.py:L80](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/neuralnet.py#L80)），返回它可以避免训练时重算；纯推理场景用不到梯度，故用 `_` 忽略。

## 5. 综合实践

**任务：写一个「前向传播验算器」，把本讲四个模块串起来。**

要求（**示例代码**骨架，请补全并运行）：

```python
import numpy as np
from neuralnet import NeuralNetMLP, sigmoid, int_to_onehot

np.random.seed(7)
n, d, h, c = 8, 3, 4, 10          # 8 个样本, 3 特征, 4 隐藏单元, 10 类

model = NeuralNetMLP(num_features=d, num_hidden=h, num_classes=c)
print('参数形状:', model.weight_h.shape, model.weight_out.shape)

x = np.random.randn(n, d)
y = np.random.randint(0, c, size=n)

# ① 标签编码: 打印 one-hot 形状并验证每行和为 1、argmax 还原标签
Y = int_to_onehot(y, c)

# ② 前向传播: 打印 a_h / a_out 形状
a_h, a_out = model.forward(x)

# ③ 单元级验算: 不用矩阵乘法, 用 for 循环重算第 0 个样本第 2 个隐藏单元
j = 2
z_manual = sum(model.weight_h[j, i] * x[0, i] for i in range(d)) + model.bias_h[j]
print(np.isclose(z_manual, np.log(a_h[0, j] / (1 - a_h[0, j]))))   # sigmoid 的反函数还原 z

# ④ 初始损失: 用 one-hot 与 a_out 计算 MSE
print('initial MSE:', np.mean((Y - a_out)**2))
```

需要依次观察并记录：

1. `weight_h` 形状为 `(4, 3)`、`weight_out` 形状为 `(10, 4)`——注意隐藏层变窄后两层形状如何联动。
2. `a_h` 形状 `(8, 4)`、`a_out` 形状 `(8, 10)`。
3. 第 ③ 步：手工加权和与 `a_h` 反解出的净输入一致——这验证了「矩阵第 j 行 = 第 j 个单元的权重向量」的形状约定。`log(a/(1-a))` 是 sigmoid 的反函数（logit，见 u3-l1）。
4. 初始 MSE 的量级（预期约 0.25，因为每个输出单元都输出 ≈0.5 而目标是 0/1；待本地验证）。

完成后，你等于把 `forward` 的每一行代码都独立复算了一遍——这正是阅读数值计算源码最可靠的方式。

## 6. 本讲小结

- `int_to_onehot` 把整数标签变成每行恰有一个 1 的 `(n, num_classes)` 矩阵，使标签与网络输出**同形、同量纲**，MSE 损失与梯度计算才能逐元素进行。
- 参数形状遵循「行=目标单元、列=来源特征」：`weight_h` 是 `(num_hidden, num_features)`，`weight_out` 是 `(num_classes, num_hidden)`；权重用种子固定的小随机数打破对称性，偏置置 0。
- `sigmoid` 光滑可导（`$\sigma' = a(1-a)$`）、值域 (0,1)，既匹配 one-hot 目标，又提供非线性——没有它，多层网络会塌缩成一层线性模型。
- `forward` 是两步「线性变换 + 激活」的堆叠：`(n,784) → (n,50) → (n,10)`，`.T` 让「按单元存行」的权重与「按样本存行」的批量输入维度相消，偏置靠广播逐行相加。
- `a_out` 由 sigmoid 逐单元独立产生，**行和不等于 1**（初始约 5），不是概率分布；预测靠 `argmax` 只取相对最大者。
- `forward` 同时返回 `a_h` 与 `a_out`，因为反向传播要复用隐藏层激活。

## 7. 下一步学习建议

本讲只完成了「算出输出」，网络的权重还是初始随机值。下一讲 **u11-l2 反向传播与 minibatch 训练循环** 将精读 [ch11/neuralnet.py:L54-L107](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/neuralnet.py#L54-L107) 的 `backward`（你会看到本讲埋下的所有伏笔在此兑现：`y_onehot`、`a*(1-a)` 导数、`a_h` 复用、`weight_out` 反传路径），以及 [ch11/ch11.py:L309-L431](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/ch11.py#L309-L431) 的 `minibatch_generator`、`compute_mse_and_acc` 与 `train` 循环。建议在此之前先自行完成综合实践——带着「每一步形状我都验算过」的底气读反向传播，四组梯度矩阵的形状将不再神秘。
