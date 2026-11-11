# u11-l2 反向传播与 minibatch 训练循环

## 1. 本讲目标

上一讲（u11-l1）我们读懂了 `NeuralNetMLP` 的前向传播：数据沿 `(n, 784) → (n, 50) → (n, 10)` 一路变换，得到每张手写数字图片的类别「打分」。但一个只会打分、不会学习的网络毫无用处——本讲解决「网络如何学习」。

学完本讲，你应该能够：

1. 逐行读懂 `backward` 方法：从 `d_loss__d_a_out` 出发，用链式法则推出输出层权重/偏置、隐藏层权重/偏置共四组梯度的矩阵运算，并能对每个中间数组说出形状与含义。
2. 理解 `minibatch_generator` 的「洗牌索引 + 切批 + yield」惰性流程，知道它为什么每个 epoch 都要重新洗牌。
3. 掌握 `mse_loss`、`accuracy`、`compute_mse_and_acc` 三个评估组件的分工。
4. 读懂 `train` 函数如何把前向、反向、梯度更新、逐 epoch 日志组装成完整的 mini-batch SGD 训练循环，并复现训练/验证准确率曲线。
5. 通过实践回答：把隐藏层从 50 个单元改成 10 / 50 / 100，精度和每轮耗时会怎样变化？

## 2. 前置知识

本讲站在两块基石上，请先确认你已经掌握：

- **（来自 u2-l2）梯度下降与链式求导**：AdalineGD 中我们用 \( w \leftarrow w - \eta \nabla L \) 沿负梯度方向更新权重；代价函数对权重的梯度靠链式法则逐层展开。本讲的 `backward` 就是同一件事的「两层网络 + 矩阵版」。
- **（来自 u2-l3）随机梯度下降与洗牌**：逐样本（或小批量）更新比整批更新收敛更快但曲线更抖；每轮训练前先 `shuffle` 打乱顺序，防止有序数据让更新方向周期性摆动。
- **（来自 u11-l1）前向传播的记号**：权重按「行 = 目标单元、列 = 来源特征」存放，`weight_h` 形状 `(50, 784)`、`weight_out` 形状 `(10, 50)`；`forward` 里用 `.T` 转置使维度相消；sigmoid 光滑可导，导数为 \( \sigma'(z) = \sigma(z)(1-\sigma(z)) \)；独热编码让标签与输出同形，MSE 才能逐元素计算。

几个本讲新引入的术语：

| 术语 | 含义 |
|---|---|
| epoch（轮） | 把训练集完整过一遍 |
| iteration（迭代/步） | 处理一个 minibatch 并更新一次权重；55000 个样本、batch=100 时 1 epoch = 550 次迭代 |
| minibatch（小批量） | 介于整批 GD 与逐样本 SGD 之间的折中：梯度是该批上的平均，比单样本噪声小，比整批更新频繁 |
| delta（误差信号） | 某层「损失对该层净输入」的梯度，反向传播中被逐层复用 |
| 双下划线命名 | `d_loss__d_a_out` 中的 `__` 表示「对……的偏导」，即 \( \partial L / \partial a_{out} \) |

数据准备沿用 u11-l1 交代过的流程：`fetch_openml` 取回 70000 张 28×28 灰度图（[ch11/ch11.py:L107-L109](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/ch11.py#L107-L109)），像素缩放到 \([-1, 1]\)（[ch11/ch11.py:L119](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/ch11.py#L119)），再两次 `train_test_split` 切成 55000 训练 / 5000 验证 / 10000 测试（[ch11/ch11.py:L163-L167](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/ch11.py#L163-L167)）。本讲聚焦训练机制，不再展开。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [ch11/neuralnet.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/neuralnet.py) | 可 `import` 的模型模块：`sigmoid`、`int_to_onehot`、`NeuralNetMLP` | `backward` 方法（L54-L108） |
| [ch11/ch11.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/ch11.py) | 本章完整可执行流程（由 notebook 转换而来） | `minibatch_generator`、`mse_loss`、`accuracy`、`compute_mse_and_acc`、`train` 及训练结果可视化 |
| ch11/ch11.ipynb | 上述脚本的 notebook 源 | 交互实验用 |

注意一个仓库事实：`NeuralNetMLP` 类在两个文件里各有一份完全相同的拷贝——`neuralnet.py` 是干净的模块版（L21-L108），`ch11.py` 内嵌了一份（[ch11/ch11.py:L200-L288](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/ch11.py#L200-L288)），后者让 notebook 不依赖 import 也能自包含运行。本讲精读 `backward` 时引用 `neuralnet.py` 的行号（更短、更干净）；讲训练循环时引用 `ch11.py`。两份代码逻辑一致。

## 4. 核心概念与源码讲解

本讲四个最小模块 + 一个组装模块：

1. `minibatch_generator`：shuffle 与切批
2. `mse_loss` 与 `accuracy`：两个评估原语
3. `compute_mse_and_acc`：大数组的分批评估
4. `backward`：链式法则四组梯度的 numpy 推导
5. `train` 函数：把一切组装成 minibatch 训练循环

---

### 4.1 minibatch_generator：shuffle 与切批

#### 4.1.1 概念说明

梯度下降有三个档位：整批 GD（每轮全量数据算一次梯度，u2-l2 的 AdalineGD）、逐样本 SGD（每个样本更新一次，u2-l3 的 AdalineSGD）、以及介于两者之间的 **mini-batch SGD**——每次用一小批（本书取 100 个）样本的平均梯度更新一次权重。

为什么选 minibatch？三个理由：

1. **计算效率**：一次 `np.dot` 处理 100 个样本比循环 100 次单样本快得多（矩阵运算向量化）。
2. **梯度质量**：批内平均让梯度比单样本更稳定，噪声适中还能帮助跳出局部结构。
3. **内存友好**：配合生成器（`yield`），任何时刻内存里只有一个 batch，而不是把切好的 550 个 batch 全部存下来。

`minibatch_generator` 就是这个「每轮洗牌、逐批吐出」的数据阀门。

#### 4.1.2 核心流程

```text
minibatch_generator(X, y, minibatch_size):
    indices = [0, 1, ..., n-1]          # 样本下标
    shuffle(indices)                    # 原地打乱下标（不动数据本身）
    for start in range(0, n - m + 1, m):  # 不足一个完整批的尾部被丢弃
        batch_idx = indices[start : start + m]
        yield X[batch_idx], y[batch_idx]   # 惰性产出，用一批算一批
```

关键设计点：

- **打乱的是索引而不是数据**。`X` 有 55000×784 个浮点数，复制洗牌代价大；打乱 `(55000,)` 的下标数组等价且几乎零成本。`X[batch_idx]` 用花式索引一次性取出该批样本（产生副本），且 X、y 用同一批索引，特征与标签始终行对齐。
- **`n - m + 1` 作为 range 上界**：保证每个 batch 都是完整的 m 个样本。若样本数不是 m 的整数倍，尾部不足 m 的残余会被直接丢弃（55000 = 550×100 恰好整除，本书场景无残余）。
- **每个 epoch 重新调用一次**：`train` 里每轮都新建一个生成器，即每轮重新洗牌。这与 u2-l3 AdalineSGD 的逐轮洗牌同源——相邻 epoch 的批组成不同，避免更新方向出现周期性。

#### 4.1.3 源码精读

数据装载与切批参数定义（[ch11/ch11.py:L305-L306](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/ch11.py#L305-L306)）：书中设定 `num_epochs = 50`、`minibatch_size = 100`。

生成器本体（[ch11/ch11.py:L309-L317](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/ch11.py#L309-L317)）：

```python
def minibatch_generator(X, y, minibatch_size):
    indices = np.arange(X.shape[0])
    np.random.shuffle(indices)

    for start_idx in range(0, indices.shape[0] - minibatch_size
                           + 1, minibatch_size):
        batch_idx = indices[start_idx:start_idx + minibatch_size]

        yield X[batch_idx], y[batch_idx]
```

- 第 2 行生成下标数组，第 3 行用全局 `np.random` 原地洗牌——这就是为什么训练前要 `np.random.seed(123)`（见 4.5.3）才能复现结果。
- `range(0, 55000 - 100 + 1, 100)` 即 `range(0, 54901, 100)`，起点 0, 100, …, 54900，共 **550 个 batch**。
- `yield` 使函数成为生成器：调用它不执行任何代码，只返回迭代器；`for` 循环每次取批时才执行到下一个 `yield`。

仓库紧接着用双重 `break` 演示「只取第一个 batch 验证形状」（[ch11/ch11.py:L321-L334](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/ch11.py#L321-L334)），打印结果应为 `(100, 784)` 与 `(100,)`——这是作者在正式训练前做的最小健全性检查。

#### 4.1.4 代码实践

**实践目标**：用玩具数组看清「产出多少个 batch、尾部如何被丢弃、下标如何被打乱」。

操作步骤（示例代码，待本地验证）：

```python
# mini_batch_demo.py —— 在任意目录运行：python mini_batch_demo.py
import numpy as np

def minibatch_generator(X, y, minibatch_size):   # 与 ch11.py 逐行相同
    indices = np.arange(X.shape[0])
    np.random.shuffle(indices)
    for start_idx in range(0, indices.shape[0] - minibatch_size
                           + 1, minibatch_size):
        batch_idx = indices[start_idx:start_idx + minibatch_size]
        yield X[batch_idx], y[batch_idx]

X = np.arange(20).reshape(10, 2)   # 10 个样本，y 恰为样本下标
y = np.arange(10)

np.random.seed(0)
for k, (xb, yb) in enumerate(minibatch_generator(X, y, 4), 1):
    print(f'batch {k}: y = {yb}')
```

需要观察的现象：

1. 10 个样本、batch=4：`range(0, 7, 4)` 只产生起点 0 与 4，共 **2 个 batch**（覆盖 8 个样本，尾部 2 个被丢弃）。
2. `y` 的取值是被打乱的下标子集，且每个 batch 内 4 个值互不相同、两个 batch 之间不重叠。
3. 换 `np.random.seed(1)` 重跑，batch 组成会变——验证「洗牌随机性来自全局种子」。

预期结果：每次运行产出 2 个 batch；不同种子下批内下标不同。精确输出待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：训练集 55000 条、`minibatch_size=100`，一个 epoch 有多少次权重更新？训练 50 个 epoch 共多少次？

答案：55000 ÷ 100 = 550 次更新/epoch；50 epoch 共 550 × 50 = 27500 次更新。

**练习 2**：如果直接 `np.random.shuffle(X)` 会出什么问题？

答案：两个问题。其一，只洗 X 不洗 y 会使特征与标签错位，训练彻底失败；其二，即使 X、y 都洗，对 55000×784 的大数组做原地洗牌（按行重排）也比洗长度 55000 的下标数组昂贵得多。打乱下标再用同一批索引同时索引 X 和 y，是又快又安全的做法。

**练习 3**：把训练集改成 55001 条、batch 仍为 100，`minibatch_generator` 会怎么处理多出的 1 条？

答案：`range(0, 55001-100+1, 100)` 的最后一个起点是 54900，覆盖样本 54900–54999，第 55001 条落在不完整批里被丢弃。`- minibatch_size + 1` 这个上界就是为了保证「只产出完整批」。

---

### 4.2 mse_loss 与 accuracy：两个评估原语

#### 4.2.1 概念说明

训练需要两个「裁判」回答不同的问题：

- **`mse_loss`：网络拟合得好不好？** 它把 one-hot 目标与输出概率逐元素求平方差的均值。MSE 光滑可导，是 `backward` 里求导的对象——它既是「评估指标」，更是「被优化的损失函数」。
- **`accuracy`：分类分对了多少？** 它把输出矩阵每行取 `argmax` 得到预测类别，再与真实标签比对求均值。准确率不可导（有阶跃），只用于评估、不用于求梯度。

两个量不必同调：MSE 变小时准确率通常升高，但 MSE 相同的两个模型准确率可能不同。

#### 4.2.2 核心流程

```text
mse_loss(targets, probas):
    onehot = int_to_onehot(targets)        # (n,) 整数标签 → (n, 10) 独热
    return mean((onehot - probas)^2)       # 标量：所有元素平方差的平均

accuracy(targets, predicted_labels):
    return mean(predicted_labels == targets)   # 预测对的比例
```

#### 4.2.3 源码精读

两个函数本体（[ch11/ch11.py:L341-L347](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/ch11.py#L341-L347)）：

```python
def mse_loss(targets, probas, num_labels=10):
    onehot_targets = int_to_onehot(targets, num_labels=num_labels)
    return np.mean((onehot_targets - probas)**2)

def accuracy(targets, predicted_labels):
    return np.mean(predicted_labels == targets)
```

- `mse_loss` 内部复用了 u11-l1 讲过的 `int_to_onehot`：独热矩阵与 `probas` 同形 `(n, 10)`，减法逐元素合法。
- `accuracy` 只有归一化的「对/错」布尔均值，输入必须是**已经 argmax 过的类别**，不是概率矩阵。

仓库随后在**未训练**的模型上做了一次基线测量（[ch11/ch11.py:L350-L357](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/ch11.py#L350-L357)）：对整个验证集 `forward` 一次，算出初始 MSE 与准确率。我们可以先笔算预期值——初始权重是标准差 0.1 的小随机数、偏置为 0，净输入接近 0，`sigmoid(≈0) ≈ 0.5`，于是每个输出元素都在 0.5 附近；对独热目标而言，无论该元素目标是 1 还是 0，\((0.5 - y)^2 \approx 0.25\)。所以：

- 初始 MSE 应接近 **0.25**；
- 每行概率几乎相同，`argmax` 近似随机猜，十类准确率应接近 **10%**。

（打印的精确数值待本地验证。）

#### 4.2.4 代码实践

**实践目标**：在 4 个玩具样本上手工核对 `mse_loss` 与 `accuracy` 的计算路径。

操作步骤（示例代码，待本地验证；先 `cd ch11` 使 `from neuralnet import int_to_onehot` 可用）：

```python
# loss_demo.py
import numpy as np
from neuralnet import int_to_onehot

def mse_loss(targets, probas, num_labels=10):
    onehot_targets = int_to_onehot(targets, num_labels=num_labels)
    return np.mean((onehot_targets - probas)**2)

probas = np.array([[0.7, 0.2, 0.1],
                   [0.1, 0.8, 0.1],
                   [0.3, 0.3, 0.4],
                   [0.6, 0.3, 0.1]])
targets = np.array([0, 1, 2, 0])       # 第 3 个样本预测错了

print('MSE  =', mse_loss(targets, probas, num_labels=3))
print('Acc  =', np.mean(np.argmax(probas, axis=1) == targets))
```

需要观察的现象：

1. `argmax` 逐行给出 `[0, 1, 2, 0]`，只有第 3 个样本错，准确率 = 0.75。
2. 换算 MSE：独热与 probas 的逐元素差中，每行有一个「目标位」差和两个「非目标位」差；第 3 行目标位差为 (1−0.4)=0.6，平方 0.36，明显大于其他行的目标位差。手算全部 12 个元素的平方差均值，应与程序输出一致。

预期结果：准确率 = 0.75；MSE 手算路径——四行的逐元素平方差之和分别为 0.14、0.06、0.54、0.14，合计 0.88，除以 12 个元素得 0.88/12 ≈ 0.0733。请以程序输出复核手算。精确数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `mse_loss` 要先把整数标签转独热，而 `accuracy` 不用？

答案：`mse_loss` 需要标签与概率矩阵 `(n, 10)` 逐元素相减，整数标签 `(n,)` 与概率在形状和量纲上都无法对齐，独热编码把「第几类」翻译成与输出同形的 0/1 矩阵；`accuracy` 只做类别相等判断，整数标签本身就是类别，直接比较即可。

**练习 2**：`accuracy` 的输入为什么必须是 `np.argmax(probas, axis=1)` 的结果而不是 `probas > 0.5`？

答案：u11-l1 已指出 sigmoid 输出是各单元独立计算的，一行概率之和不为 1；用 0.5 阈值可能一行里同时「超过 0.5」的单元数不是 1 个（或 0 个），无法定义唯一类别。`argmax` 只看相对大小，永远给出恰好一个类别，与「十选一」任务匹配。

**练习 3**：模型训练后 MSE 从 0.25 降到 0.01，准确率一定同步上升吗？

答案：不一定严格同步。MSE 衡量的是概率向独热的整体逼近程度，准确率只看 argmax 的相对排序；可能出现「正确类的概率仍不高、但比其余类高」的样本——MSE 改善不大、准确率却提升，反之也可能。两者相关但不等价。

---

### 4.3 compute_mse_and_acc：大数组的分批评估

#### 4.3.1 概念说明

`mse_loss(y_valid, probas)` 这条路要求先把**整个**验证集 forward 成 `(5000, 10)` 的概率矩阵——对本例尚可承受，但对 55000 条训练集（以及更大的真实数据）会一次性产生大中间数组。`compute_mse_and_acc` 的做法是：**复用 `minibatch_generator` 把评估集也切成批，逐批 forward、逐批累计**，内存峰值只与 batch 大小相关。

它同时把 MSE 与准确率的计算合二为一：一次 forward 的结果既算损失又数对错，避免重复前向。

#### 4.3.2 核心流程

```text
compute_mse_and_acc(nnet, X, y):
    mse = 0; correct = 0; num_examples = 0
    for (features, targets) in minibatch_generator(X, y, 100):
        probas = nnet.forward(features)          # (m, 10)
        pred = argmax(probas, axis=1)
        mse += mean((onehot(targets) - probas)^2)   # 累计的是「批均值」
        correct += sum(pred == targets)
        num_examples += m
    return mse / 批数, correct / num_examples
```

一个值得注意的细节：返回的 MSE 是**各批均值的平均**（`mse/(i+1)`，`i+1` 即批数）。只有当各批大小相等时它才严格等于全体样本的 MSE；本书中 55000 与 5000 都被 100 整除，因此没有偏差。准确率则不受影响——分子分母都按样本数加权，天然正确。

#### 4.3.3 源码精读

函数本体（[ch11/ch11.py:L362-L380](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/ch11.py#L362-L380)）：

```python
def compute_mse_and_acc(nnet, X, y, num_labels=10, minibatch_size=100):
    mse, correct_pred, num_examples = 0., 0, 0
    minibatch_gen = minibatch_generator(X, y, minibatch_size)

    for i, (features, targets) in enumerate(minibatch_gen):

        _, probas = nnet.forward(features)
        predicted_labels = np.argmax(probas, axis=1)

        onehot_targets = int_to_onehot(targets, num_labels=num_labels)
        loss = np.mean((onehot_targets - probas)**2)
        correct_pred += (predicted_labels == targets).sum()

        num_examples += targets.shape[0]
        mse += loss

    mse = mse/(i+1)
    acc = correct_pred/num_examples
    return mse, acc
```

逐行要点：

- 第 2 行：评估同样调用 `minibatch_generator`，因此评估集也会被随机洗牌——但评估只有 forward、没有参数更新，累计求和与顺序无关，洗牌不影响结果。
- 第 6 行：`enumerate` 拿到批号 `i`，循环结束时 `i + 1` 恰好是批数（验证集 5000/100 = 50 批）。
- 第 8 行：`forward` 返回 `(a_h, a_out)`，用 `_` 丢弃隐藏层激活——评估不需要它。
- 第 16-17 行：MSE 用「批均值之和 ÷ 批数」，准确率用「累计对题数 ÷ 总样本数」，两条口径并行走完。

仓库用它在未训练模型上再次测量（[ch11/ch11.py:L385-L387](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/ch11.py#L385-L387)），输出应与 4.2 的整批版一致——这本身就是一次「分批 = 整批」的交叉验证。

#### 4.3.4 代码实践

**实践目标**：验证「分批累计的 MSE/准确率」与「整批一次算」数值一致，并观察批数 `i+1` 的来历。

操作步骤（示例代码，待本地验证；`cd ch11` 后运行）：

```python
# eval_demo.py
import numpy as np
from neuralnet import NeuralNetMLP, int_to_onehot
from ch11_helpers import minibatch_generator, compute_mse_and_acc  # 见下方说明
```

由于 `ch11.py` 顶层会立刻下载 MNIST 并训练（import 即执行全书流程），不能直接 `from ch11 import ...`。请把 4.1.3 的 `minibatch_generator` 与上面的 `compute_mse_and_acc` **复制**到自建文件 `ch11_helpers.py` 中再运行：

```python
# eval_demo.py（续）
np.random.seed(0)
tiny_model = NeuralNetMLP(num_features=28*28, num_hidden=8, num_classes=10)

Xv = np.random.randn(250, 784)          # 250 条伪数据 → 2 批 ×100，尾部 50 条被丢
yv = np.random.randint(0, 10, size=250)

# 分批版
mse_b, acc_b = compute_mse_and_acc(tiny_model, Xv, yv)

# 整批版
_, probas = tiny_model.forward(Xv)
mse_full = np.mean((int_to_onehot(yv, 10) - probas)**2)
acc_full = np.mean(np.argmax(probas, axis=1) == yv)

print(f'分批: MSE={mse_b:.6f}  Acc={acc_b:.4f}')
print(f'整批: MSE={mse_full:.6f}  Acc={acc_full:.4f}')
```

需要观察的现象：

1. 250 条、batch=100 时 `range(0, 151, 100)` 只产出 2 批——**尾部 50 条根本没参与**分批评估，因此分批版与整批版（覆盖全部 250 条）的 MSE、准确率一般**不相等**，差异正来自被丢弃的尾部。
2. 把 `Xv` 换成 300 条（恰好 3 批无残余），则分批与整批的 MSE、准确率都应完全一致——等批大小下「批均值的平均」严格等于整体均值。

预期结果：300 条时两组数值逐位相同；250 条时分批版只反映前 200 条、两版出现差异。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`mse = mse/(i+1)` 中，`i` 从哪里来？如果传入空数组会发生什么？

答案：`i` 是 `enumerate(minibatch_gen)` 的循环变量，循环结束后 `i + 1` 等于已处理的批数。若 X 为空，循环体一次都不执行，`i` 未定义，`mse/(i+1)` 会抛 `NameError`——这是依赖「至少有一批」前提的写法，日常使用数据集非空即可。

**练习 2**：评估阶段也走了 shuffle，为什么不担心结果不可复现？

答案：shuffle 影响的是**批的处理顺序**，而 `compute_mse_and_acc` 对损失求和、对正确数求和都与顺序无关（浮点求和的舍入差异在 1e-15 量级，可忽略）；forward 又是纯函数不修改权重，所以任何洗牌顺序都得到相同结果。

**练习 3**：`compute_mse_and_acc` 里为什么用 `nnet.forward` 而不调用 `mse_loss`？

答案：功能上等价（第 11-12 行就是内联的 `mse_loss` 逻辑），但一次 forward 的 `probas` 要同时供 MSE 和 `argmax` 准确率两个用途；内联避免了重复编码/重复前向，作者选择把「单用途原语」（`mse_loss`、`accuracy`）与「复合评估器」（`compute_mse_and_acc`）分层实现。

---

### 4.4 backward：链式法则四组梯度的 numpy 推导

#### 4.4.1 概念说明

`backward` 回答一个问题：**损失 L 对全部四组参数（`weight_out`、`bias_out`、`weight_h`、`bias_h`）各自的梯度是什么？**

多维复合函数求偏导靠链式法则。以输出层权重为例，从损失到权重隔着三层复合：

\[
\frac{\partial L}{\partial W^{out}}
= \underbrace{\frac{\partial L}{\partial A^{out}}}_{\text{损失→输出激活}}
\odot \underbrace{\frac{\partial A^{out}}{\partial Z^{out}}}_{\text{激活→净输入}}
\cdot \underbrace{\frac{\partial Z^{out}}{\partial W^{out}}}_{\text{净输入→权重}}
\]

反向传播的精髓在于：**把前两个因子的乘积打包成 delta（误差信号），让它沿网络反向流动**。输出层的 `delta_out` 一方面直接组合出输出层梯度，另一方面穿过 `weight_out` 传回隐藏层、再乘上隐藏层的激活导数得到 `delta_h`，进而组合出隐藏层梯度。同一份误差信号被复用两次——这就是「反向传播」比「对每个参数独立数值求导」高效得多的原因。

对照前向传播（u11-l1）的记号：批量输入 \(X\) 形状 \((n, 784)\)，隐藏激活 \(A^h\) 为 \((n, 50)\)，输出激活 \(A^{out}\) 为 \((n, 10)\)，独热目标 \(Y\) 为 \((n, 10)\)，损失取批内平均 MSE：

\[
L = \frac{1}{n}\sum_{i=1}^{n}\sum_{j=1}^{C}\left(a^{out}_{ij} - y_{ij}\right)^2
\]

#### 4.4.2 核心流程

```text
backward(x, a_h, a_out, y):
    y_onehot = int_to_onehot(y)                    # (n, 10)

    ── Part 1：输出层 ─────────────────────────────
    d_loss__d_a_out  = 2*(a_out - y_onehot)/n      # (n,10)  ∂L/∂A_out
    d_a_out__d_z_out = a_out*(1-a_out)             # (n,10)  sigmoid 导数
    delta_out        = 两者逐元素相乘               # (n,10)  误差信号
    d_z_out__dw_out  = a_h                          # (n,50)  ∂Z_out/∂W_out
    d_loss__dw_out   = delta_out.T @ a_h            # (10,50) 输出层权重梯度
    d_loss__db_out   = delta_out 沿样本维求和        # (10,)   输出层偏置梯度

    ── Part 2：隐藏层 ─────────────────────────────
    d_z_out__a_h     = weight_out                   # (10,50) ∂Z_out/∂A_h
    d_loss__a_h      = delta_out @ weight_out       # (n,50)  误差传回隐藏层
    d_a_h__d_z_h     = a_h*(1-a_h)                  # (n,50)  sigmoid 导数
    d_z_h__d_w_h     = x                            # (n,784) ∂Z_h/∂W_h
    d_loss__d_w_h    = (d_loss__a_h ⊙ d_a_h__d_z_h).T @ x   # (50,784) 隐藏层权重梯度
    d_loss__d_b_h    = (d_loss__a_h ⊙ d_a_h__d_z_h) 沿样本维求和  # (50,)

    return 四组梯度
```

对应的数学式（矩阵形式，\(\odot\) 为逐元素乘）：

\[
\Delta^{out} = \frac{2\,(A^{out} - Y)}{n} \odot A^{out}\left(1 - A^{out}\right)
\]

\[
\frac{\partial L}{\partial W^{out}} = \Delta^{out\,\top} A^{h},
\qquad
\frac{\partial L}{\partial b^{out}} = \sum_{i}\Delta^{out}_{i,:}
\]

\[
\frac{\partial L}{\partial A^{h}} = \Delta^{out} W^{out},
\qquad
\Delta^{h} = \left(\Delta^{out} W^{out}\right) \odot A^{h}\left(1 - A^{h}\right)
\]

\[
\frac{\partial L}{\partial W^{h}} = \Delta^{h\,\top} X,
\qquad
\frac{\partial L}{\partial b^{h}} = \sum_{i}\Delta^{h}_{i,:}
\]

维度自检表（n=100, h=50, d=784, C=10）——**每一步运算前先对形状，是读懂矩阵求导最可靠的办法**：

| 代码量 | 形状 | 含义 |
|---|---|---|
| `d_loss__d_a_out` | (100, 10) | 损失对输出激活的偏导 |
| `d_a_out__d_z_out` | (100, 10) | 输出层 sigmoid 导数 |
| `delta_out` | (100, 10) | 输出层误差信号 |
| `d_z_out__dw_out = a_h` | (100, 50) | 输出净输入对输出权重的偏导 |
| `d_loss__dw_out = delta_out.T @ a_h` | (10, 50) | ✅ 与 `weight_out` 同形 |
| `d_loss__db_out` | (10,) | ✅ 与 `bias_out` 同形 |
| `d_z_out__a_h = weight_out` | (10, 50) | 输出净输入对隐藏激活的偏导 |
| `d_loss__a_h = delta_out @ weight_out` | (100, 50) | (100,10)@(10,50) → 误差传回隐藏层 |
| `d_a_h__d_z_h` | (100, 50) | 隐藏层 sigmoid 导数 |
| `d_z_h__d_w_h = x` | (100, 784) | 隐藏净输入对隐藏权重的偏导 |
| `d_loss__d_w_h` | (50, 784) | ✅ 与 `weight_h` 同形 |
| `d_loss__d_b_h` | (50,) | ✅ 与 `bias_h` 同形 |

一个容易被忽略的尺度细节：`backward` 里的 `d_loss__d_a_out` 除以的是样本数 \(n\)（`y.shape[0]`），而 4.2 的 `mse_loss` 对**全部 \(n \times C\) 个元素**取均值——两者相差常数倍 \(C=10\)。常数因子会被学习率吸收（相当于有效步长放大 10 倍），不影响梯度方向，所以训练照常工作。但当你做 4.4.4 的数值检验时，必须让参考损失与 `backward` 采用同一约定（除以 \(n\)），否则数值梯度会正好差 10 倍。

#### 4.4.3 源码精读

方法签名与入口（[ch11/neuralnet.py:L54-L61](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/neuralnet.py#L54-L61)）：`backward(x, a_h, a_out, y)` 接收的正是 `forward` 刚产出的两个激活矩阵——**反向传播直接复用前向的中间结果，不需要重新计算**；第 61 行先把整数标签转独热。

**Part 1：输出层梯度**（[ch11/neuralnet.py:L63-L85](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/neuralnet.py#L63-L85)）：

```python
# Part 1: dLoss/dOutWeights
## = dLoss/dOutAct * dOutAct/dOutNet * dOutNet/dOutWeight

# input/output dim: [n_examples, n_classes]
d_loss__d_a_out = 2.*(a_out - y_onehot) / y.shape[0]

# input/output dim: [n_examples, n_classes]
d_a_out__d_z_out = a_out * (1. - a_out) # sigmoid derivative

# output dim: [n_examples, n_classes]
delta_out = d_loss__d_a_out * d_a_out__d_z_out # "delta (rule) placeholder"

# [n_examples, n_hidden]
d_z_out__dw_out = a_h

# input dim: [n_classes, n_examples] dot [n_examples, n_hidden]
# output dim: [n_classes, n_hidden]
d_loss__dw_out = np.dot(delta_out.T, d_z_out__dw_out)
d_loss__db_out = np.sum(delta_out, axis=0)
```

- L69：MSE 对 \(a_{out}\) 求导得 \(2(a_{out}-y)/n\)，减法方向是「预测 − 目标」，与「目标 − 预测」只差符号，随后更新时会再乘负号抵消。
- L72：sigmoid 导数 \(\sigma(z)(1-\sigma(z))\)——用激活值本身表示，无需再存 z。
- L75：注释里的 "delta (rule) placeholder" 点明这就是经典的 delta 规则误差项。
- L80：由 \(z_{out} = a_h W_{out}^\top + b_{out}\) 可知 \(\partial z_{out}/\partial W_{out}\) 就是 \(a_h\)——**权重梯度的「特征」来自下一层输入**（与 AdalineGD 中梯度 \(x\) 的角色完全一致，只是 x 换成了隐藏层激活 a_h）。
- L84：`delta_out.T @ a_h` 即 (10,100)@(100,50) → (10,50)，一次矩阵乘同时完成「链式相乘」与「批内求和」。
- L85：偏置对每个样本的净输入都有贡献，所以梯度沿样本维（axis=0）求和。

**Part 2：隐藏层梯度**（[ch11/neuralnet.py:L88-L108](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/neuralnet.py#L88-L108)）：

```python
# Part 2: dLoss/dHiddenWeights
## = DeltaOut * dOutNet/dHiddenAct * dHiddenAct/dHiddenNet * dHiddenNet/dWeight

# [n_classes, n_hidden]
d_z_out__a_h = self.weight_out

# output dim: [n_examples, n_hidden]
d_loss__a_h = np.dot(delta_out, d_z_out__a_h)

# [n_examples, n_hidden]
d_a_h__d_z_h = a_h * (1. - a_h) # sigmoid derivative

# [n_examples, n_features]
d_z_h__d_w_h = x

# output dim: [n_hidden, n_features]
d_loss__d_w_h = np.dot((d_loss__a_h * d_a_h__d_z_h).T, d_z_h__d_w_h)
d_loss__d_b_h = np.sum((d_loss__a_h * d_a_h__d_z_h), axis=0)

return d_loss__dw_out, d_loss__db_out, d_loss__d_w_h, d_loss__d_b_h
```

- L93：由 \(z_{out} = a_h W_{out}^\top\)，得 \(\partial z_{out}/\partial a_h = W_{out}\)——**误差反向穿过的「通道」就是前向用过的权重矩阵本身**，这正是「反向传播」名字的由来。
- L96：`delta_out @ weight_out`，(100,10)@(10,50) → (100,50)：输出层误差信号被分配回每个隐藏单元，转置已隐含在乘法顺序中。
- L99：隐藏层自己的 sigmoid 导数。
- L105：`d_loss__a_h * d_a_h__d_z_h` 即 \(\Delta^h\)，转置后与输入 x 相乘得 (50,784)，与 `weight_h` 同形，可直接做梯度下降更新。
- L108：按固定顺序返回四组梯度；`train` 函数按同一顺序解包。

#### 4.4.4 代码实践：数值梯度检验

**实践目标**：用中心差分独立计算 `weight_out` 某个元素的数值梯度，验证 `backward` 的解析推导正确——这是深度学习工程里检验手写梯度的标准手段。

操作步骤（示例代码，待本地验证；在 `ch11` 目录下运行）：

```python
# grad_check.py
import numpy as np
from neuralnet import NeuralNetMLP

np.random.seed(0)
model = NeuralNetMLP(num_features=5, num_hidden=4, num_classes=3,
                     random_seed=1)

X = np.random.randn(6, 5)              # 6 个样本、5 维特征
y = np.array([0, 1, 2, 1, 0, 2])

a_h, a_out = model.forward(X)
d_w_out, d_b_out, d_w_h, d_b_h = model.backward(X, a_h, a_out, y)

# 参考损失必须与 backward 同约定：除以样本数 n（而不是 n*C）
def batch_loss(m):
    _, a = m.forward(X)
    Y = np.eye(m.num_classes)[y]
    return np.sum((Y - a) ** 2) / X.shape[0]

eps = 1e-6
i, j = 0, 0
orig = model.weight_out[i, j]

model.weight_out[i, j] = orig + eps;  loss_plus = batch_loss(model)
model.weight_out[i, j] = orig - eps;  loss_minus = batch_loss(model)
model.weight_out[i, j] = orig                      # 恢复

numeric_grad = (loss_plus - loss_minus) / (2 * eps)
print(f'解析梯度 d_loss__dw_out[0,0] = {d_w_out[i, j]:.8f}')
print(f'数值梯度(中心差分)       = {numeric_grad:.8f}')
```

需要观察的现象：

1. 两个梯度应当在前 6~7 位小数上一致（典型误差量级 1e-9~1e-10），证明链式法则实现无误。
2. 把 `i, j` 换成其他下标、或改成检验 `weight_h`、`bias_h`（改动 `d_w_h[i, j]` 对应行），同样应当对齐。
3. 把 `batch_loss` 换成 4.2 的 `mse_loss` 口径（`np.mean`，除以 \(n \times C\)），数值梯度会缩小为原来的 1/3（本例 C=3）——亲手复现 4.4.2 提到的常数因子现象。

预期结果：解析与数值梯度一致；改变损失口径后出现恰好 C 倍的差。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `d_loss__db_out` 是沿 `axis=0` 求和，而 `d_loss__dw_out` 要做矩阵乘？

答案：偏置进入每个样本的净输入 \(z_{out} = a_h W^\top + b\) 时没有乘任何输入，链式法则里 \(\partial z_{ij}/\partial b_j = 1\)，n 个样本的梯度贡献只能相加，故 `np.sum(delta_out, axis=0)`；权重则乘上了第 k 个输入（a_h 的第 k 列），「求和」与「乘输入」合并成一次矩阵乘 `delta_out.T @ a_h`。可以说偏置的梯度就是权重梯度在「输入恒为 1」时的特例。

**练习 2**：如果把隐藏层激活换成恒等函数（不用 sigmoid），整个网络等效于什么？`d_a_h__d_z_h` 变成多少？

答案：两层线性变换的复合仍是线性变换（u11-l1 的结论），网络退化成线性模型，无论隐藏层多宽都只能画线性决策边界。此时 \(\partial a_h/\partial z_h = 1\)，即 `d_a_h__d_z_h` 变成全 1 数组，反向传播退化为纯线性回传——这正是「非线性激活是深度的来源」的数学体现。

**练习 3**：`d_loss__a_h = np.dot(delta_out, self.weight_out)` 中为什么不需要对 `weight_out` 转置？

答案：由 \(z_{out} = a_h W_{out}^\top\) 得 \(\partial L/\partial a_h = \Delta^{out} W_{out}\)，形状 (n,10)@(10,50)=(n,50)；矩阵乘法本身的顺序（delta 在左、W 在右）已经完成了所需的「转置」，再显式转置反而维度不对。对比 Part 1 的 `delta_out.T @ a_h`：那里需要 (C,n)@(n,H)，故对 delta 转置——两边遵循的其实是同一个维度自检原则。

---

### 4.5 train 函数：把一切组装成 minibatch 训练循环

#### 4.5.1 概念说明

至此零件齐备：`minibatch_generator` 供数据、`forward` 算激活、`backward` 算梯度、`compute_mse_and_acc` 做测量。`train` 函数按「**逐 epoch → 逐 batch：前向 → 反向 → 更新 → epoch 末测量记录**」的骨架把它们串成完整的 mini-batch SGD 循环——这就是全书第一个（也是唯一一个）从零手写的神经网络训练器，结构上与 u2-l3 的 AdalineSGD「按轮-逐样本」双层循环完全同构，只是更新粒度从 1 个样本变成 100 个样本、梯度来源从解析单层公式变成反向传播。

#### 4.5.2 核心流程

```text
train(model, X_train, y_train, X_valid, y_valid, num_epochs, lr=0.1):
    记录表: epoch_loss / epoch_train_acc / epoch_valid_acc
    for e in 1..num_epochs:                     # 每个 epoch
        for (X_mini, y_mini) in minibatch_generator(X_train, y_train, 100):
            a_h, a_out = model.forward(X_mini)             # ① 前向
            四组梯度 = model.backward(X_mini, a_h, a_out, y_mini)  # ② 反向
            model.weight_h  -= lr * d_loss__d_w_h           # ③ 梯度下降
            model.bias_h    -= lr * d_loss__d_b_h           #    （减号 = 负梯度方向）
            model.weight_out -= lr * d_loss__d_w_out
            model.bias_out   -= lr * d_loss__d_b_out
        train_mse, train_acc = compute_mse_and_acc(model, X_train, y_train)  # ④ 测量
        valid_mse, valid_acc = compute_mse_and_acc(model, X_valid, y_valid)
        记录并打印一行日志
    return epoch_loss, epoch_train_acc, epoch_valid_acc
```

注意 ③ 中四组参数共用同一个学习率 0.1，更新发生在**每个 batch 之后**（一个 epoch 更新 550 次），而不是每个 epoch 之后——这是 SGD 与整批 GD（u2-l2）最本质的区别。

#### 4.5.3 源码精读

函数本体（[ch11/ch11.py:L392-L431](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/ch11.py#L392-L431)）：

```python
def train(model, X_train, y_train, X_valid, y_valid, num_epochs,
          learning_rate=0.1):

    epoch_loss = []
    epoch_train_acc = []
    epoch_valid_acc = []

    for e in range(num_epochs):

        # iterate over minibatches
        minibatch_gen = minibatch_generator(
            X_train, y_train, minibatch_size)

        for X_train_mini, y_train_mini in minibatch_gen:

            #### Compute outputs ####
            a_h, a_out = model.forward(X_train_mini)

            #### Compute gradients ####
            d_loss__d_w_out, d_loss__d_b_out, d_loss__d_w_h, d_loss__d_b_h =                 model.backward(X_train_mini, a_h, a_out, y_train_mini)

            #### Update weights ####
            model.weight_h -= learning_rate * d_loss__d_w_h
            model.bias_h -= learning_rate * d_loss__d_b_h
            model.weight_out -= learning_rate * d_loss__d_w_out
            model.bias_out -= learning_rate * d_loss__d_b_out

        #### Epoch Logging ####
        train_mse, train_acc = compute_mse_and_acc(model, X_train, y_train)
        valid_mse, valid_acc = compute_mse_and_acc(model, X_valid, y_valid)
        train_acc, valid_acc = train_acc*100, valid_acc*100
        epoch_train_acc.append(train_acc)
        epoch_valid_acc.append(valid_acc)
        epoch_loss.append(train_mse)
        print(f'Epoch: {e+1:03d}/{num_epochs:03d} '
              f'| Train MSE: {train_mse:.2f} '
              f'| Train Acc: {train_acc:.2f}% '
              f'| Valid Acc: {valid_acc:.2f}%')

    return epoch_loss, epoch_train_acc, epoch_valid_acc
```

要点：

- L395-L397：三张记录表分别存训练 MSE、训练准确率、验证准确率（都按 epoch 粒度）。
- L402-L403：每个 epoch **新建**生成器——即每轮重新洗牌（呼应 4.1）。
- L408-L411：前向拿激活、反向拿梯度；`backward` 返回的四组梯度按固定顺序解包（4.4.3 的 L108）。
- L414-L417：`-= learning_rate * 梯度`。负梯度方向就是减号；更新的是模型属性本身（原地修改，`model` 是可变对象，函数无需返回模型）。
- L420-L421：注意 `train_mse` 与 `valid_mse` 都算了，但**只有训练 MSE 进了 `epoch_loss`**；验证 MSE 仅打印时丢弃（`valid_mse` 未被使用）。
- L426-L429：`{e+1:03d}` 把 epoch 编号格式化为 3 位宽度，日志对齐。

训练的启动与复现（[ch11/ch11.py:L436-L440](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/ch11.py#L436-L440)）：

```python
np.random.seed(123) # for the training set shuffling

epoch_loss, epoch_train_acc, epoch_valid_acc = train(
    model, X_train, y_train, X_valid, y_valid,
    num_epochs=50, learning_rate=0.1)
```

`np.random.seed(123)` 必须在 `train` 之前调用：`minibatch_generator` 用的是**全局** `np.random`，种子固定后 550 次/epoch 的切批顺序可复现，整个训练过程可复现（模型权重另有 `random_seed=123`，见 u11-l1）。

训练结果的两组可视化（[ch11/ch11.py:L447-L464](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/ch11.py#L447-L464)）：先画 epoch-训练 MSE 曲线（应单调下降并趋平），再把训练/验证准确率画进同一张图（两条曲线都上升、间距不大说明没有明显过拟合）。最终在测试集上的评估（[ch11/ch11.py:L469-L470](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/ch11.py#L469-L470)）调用的正是 4.3 的 `compute_mse_and_acc`，书中运行得到的测试准确率约在 94%~95% 区间（精确数值待本地验证）；随后的失败案例可视化（[ch11/ch11.py:L477-L498](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch11/ch11.py#L477-L498)）挑出前 1000 张测试图里分错的前 25 张逐张展示「真值 vs 预测」——手写数字里确实存在人类也难分辨的样本。

#### 4.5.4 代码实践

**实践目标**：先用 2 个 epoch 冒烟测试整条训练链路（避免一上来等 50 轮才发现问题），确认日志、曲线接口都工作。

操作步骤（示例代码，待本地验证；可直接在 `ch11.ipynb` 末尾新建单元格，或参考第 5 节脚本）：

1. 打开 `ch11/ch11.ipynb`，先从头运行到模型实例化（L293 附近的 `model = NeuralNetMLP(...)`）。
2. 把 `train` 调用改为 `num_epochs=2`：

```python
np.random.seed(123)
epoch_loss, epoch_train_acc, epoch_valid_acc = train(
    model, X_train, y_train, X_valid, y_valid,
    num_epochs=2, learning_rate=0.1)
```

需要观察的现象：

1. 每个 epoch 打印一行日志；第 1 个 epoch 结束时训练/验证准确率就应明显高于 10%（通常已到 80% 以上量级），第 2 个 epoch 继续上升——说明反向传播确实在降低损失。
2. `epoch_loss` 长度为 2、`epoch_train_acc` 与 `epoch_valid_acc` 长度也是 2，可直接用于绘图。
3. 若把 `learning_rate` 改成 10，准确率可能不升反降——梯度过大导致震荡（呼应 u2-l2 的学习率敏感性结论）。

预期结果：2 轮内验证准确率显著高于随机猜测并持续改善。精确数值待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：`train` 中权重更新发生在每一批之后还是每个 epoch 之后？这对应哪种梯度下降？

答案：每个 batch（100 个样本）之后立即更新，一个 epoch 更新 550 次。这是 mini-batch SGD——u2-l3 AdalineSGD 的「逐样本」版把 batch 换成 1，u2-l2 AdalineGD 的「整批」版把 batch 换成全部样本。

**练习 2**：`epoch_loss` 里记录的是哪个集合的 MSE？`valid_mse` 去哪了？

答案：训练集的 MSE（L420 的 `train_mse`）。`valid_mse` 在 L421 计算出来后没有被记录或使用，属于「算了但没存」的死值——若想画验证 MSE 曲线，需要自行增加一张 `epoch_valid_loss` 列表并在循环里 append。

**练习 3**：为什么 `train` 函数不返回 `model`，训练效果却能保留下来？

答案：Python 的参数传递是「传对象引用」，`model` 是可变对象，函数内 `model.weight_h -= ...` 等语句原地修改了对象的属性；调用方持有的引用与函数内是同一个对象，修改直接可见。返回值只用于带回三张记录表。

---

## 5. 综合实践

**任务：隐藏层宽度扫描实验——把 50 改成 [10, 50, 100]，量化「宽度 vs 精度 vs 耗时」。**

本实验贯穿本讲全部内容：`minibatch_generator` 切批、`forward`/`backward` 前向反向、`compute_mse_and_acc` 测量、`train` 循环组装，再加上 `time.perf_counter` 计时。

先做预测（学会先猜再验）：参数量 ≈ \(784h + h + 10h + 10\)，h=10/50/100 分别约 **0.8 万 / 4.0 万 / 8.0 万**。据此猜测：每轮耗时大致随 h 线性增长；h=10 容量不足、最终准确率明显偏低；h=50 与 h=100 差距应小于 h=10 与 h=50 的差距（容量收益递减）。

操作步骤：在 `ch11` 目录下新建 `width_experiment.py`（示例代码，新建文件、不改动任何原源码；待本地验证）：

```python
# width_experiment.py —— 隐藏层宽度对比实验
# 运行：cd ch11 && python width_experiment.py
import time
import numpy as np
import matplotlib.pyplot as plt
from sklearn.datasets import fetch_openml
from sklearn.model_selection import train_test_split
from neuralnet import NeuralNetMLP, int_to_onehot

# ---------- 数据准备（与 ch11.py L107-L167 相同） ----------
X, y = fetch_openml('mnist_784', version=1, return_X_y=True)
X = ((X.values / 255.) - .5) * 2
y = y.astype(int).values
X_temp, X_test, y_temp, y_test = train_test_split(
    X, y, test_size=10000, random_state=123, stratify=y)
X_train, X_valid, y_train, y_valid = train_test_split(
    X_temp, y_temp, test_size=5000, random_state=123, stratify=y_temp)

# ---------- 训练组件（与 ch11.py 同名实现一致，增加计时） ----------
def minibatch_generator(X, y, minibatch_size):
    indices = np.arange(X.shape[0])
    np.random.shuffle(indices)
    for start_idx in range(0, indices.shape[0] - minibatch_size
                           + 1, minibatch_size):
        batch_idx = indices[start_idx:start_idx + minibatch_size]
        yield X[batch_idx], y[batch_idx]

def compute_mse_and_acc(nnet, X, y, num_labels=10, minibatch_size=100):
    mse, correct_pred, num_examples = 0., 0, 0
    minibatch_gen = minibatch_generator(X, y, minibatch_size)
    for i, (features, targets) in enumerate(minibatch_gen):
        _, probas = nnet.forward(features)
        predicted_labels = np.argmax(probas, axis=1)
        onehot_targets = int_to_onehot(targets, num_labels=num_labels)
        mse += np.mean((onehot_targets - probas)**2)
        correct_pred += (predicted_labels == targets).sum()
        num_examples += targets.shape[0]
    return mse/(i+1), correct_pred/num_examples

def train(model, X_train, y_train, X_valid, y_valid, num_epochs,
          learning_rate=0.1, minibatch_size=100):
    epoch_loss, epoch_tr, epoch_va, epoch_secs = [], [], [], []
    for e in range(num_epochs):
        t0 = time.perf_counter()
        for X_mini, y_mini in minibatch_generator(
                X_train, y_train, minibatch_size):
            a_h, a_out = model.forward(X_mini)
            d_w_out, d_b_out, d_w_h, d_b_h = model.backward(
                X_mini, a_h, a_out, y_mini)
            model.weight_h -= learning_rate * d_w_h
            model.bias_h -= learning_rate * d_b_h
            model.weight_out -= learning_rate * d_w_out
            model.bias_out -= learning_rate * d_b_out
        secs = time.perf_counter() - t0
        train_mse, train_acc = compute_mse_and_acc(model, X_train, y_train)
        _, valid_acc = compute_mse_and_acc(model, X_valid, y_valid)
        epoch_loss.append(train_mse)
        epoch_tr.append(train_acc*100)
        epoch_va.append(valid_acc*100)
        epoch_secs.append(secs)
        print(f'h={model.weight_h.shape[0]:3d} | epoch {e+1:02d}/{num_epochs} '
              f'| {secs:5.1f}s | Train {train_acc*100:5.2f}% '
              f'| Valid {valid_acc*100:5.2f}%')
    return epoch_loss, epoch_tr, epoch_va, epoch_secs

# ---------- 实验主体：h ∈ {10, 50, 100} ----------
results = {}
for num_hidden in [10, 50, 100]:
    np.random.seed(123)                 # 固定切批顺序，保证三组可比
    model = NeuralNetMLP(num_features=28*28,
                         num_hidden=num_hidden, num_classes=10)
    results[num_hidden] = train(model, X_train, y_train, X_valid,
                                y_valid, num_epochs=50)

# ---------- 可视化 ----------
fig, ax = plt.subplots(1, 2, figsize=(11, 4))
for h in [10, 50, 100]:
    _, tr, va, secs = results[h]
    ax[0].plot(tr, label=f'train h={h}')
    ax[0].plot(va, '--', label=f'valid h={h}')
    ax[1].bar(f'h={h}', np.mean(secs))
ax[0].set_xlabel('Epoch'); ax[0].set_ylabel('Accuracy (%)')
ax[0].legend(loc='lower right')
ax[1].set_ylabel('平均每轮耗时 (s)')
plt.tight_layout()
plt.savefig('width_experiment.png', dpi=150)
print('图已保存到 ch11/width_experiment.png')
```

需要观察的现象与预期结果（精确数值均待本地验证）：

1. **准确率曲线**：h=10 的训练/验证曲线明显更低且更早饱和（容量瓶颈）；h=50 与 h=100 曲线接近，后者可能略高或基本持平——宽度收益递减。
2. **训练/验证间距**：三组模型的训练与验证曲线间隙都不大，说明在本任务规模下未出现明显过拟合；h=100 的间隙通常略大于 h=10（容量越大越有过拟合倾向）。
3. **耗时**：每轮耗时应大致随 h 线性增长（矩阵乘的计算量近似正比于参数量）；h=100 的每轮耗时应约是 h=10 的 8~10 倍量级、h=50 的约 2 倍。
4. **复现性**：三组实验都在 `np.random.seed(123)` 之后开始，切批顺序一致；又因 `NeuralNetMLP(random_seed=123)` 固定了初始权重，重复运行应得到相同曲线。

实验后的分析建议：把「最终验证准确率」和「平均每轮耗时」对 h 画散点，回答——为把准确率再抬 1 个百分点，多花的计算时间是否值得？这就是模型选型时最朴素的收益/成本权衡。

## 6. 本讲小结

- `minibatch_generator` 用「打乱下标 + `range(0, n-m+1, m)` 切批 + `yield` 惰性产出」实现数据阀门：每个 epoch 重新洗牌、只产出完整批、内存峰值只与批大小相关（55000 样本 ÷ 100 = 550 批/epoch）。
- `mse_loss`（可导，是 backward 的求导对象）与 `accuracy`（不可导，只做评估）分工明确；初始未训练模型的 MSE 应接近 0.25、准确率接近 10%，这是可以笔算出来的基线。
- `compute_mse_and_acc` 复用切批器对大数组做分批评估：一次 forward 同时算 MSE 与数对错；其 MSE 是「批均值的平均」，等批大小时才严格等于整体 MSE。
- `backward` 是链式法则的矩阵版：`delta_out = 2(a_out−y)/n ⊙ a_out(1−a_out)` 组合出输出层梯度后，经 `delta_out @ weight_out` 把误差传回隐藏层得到 `delta_h`，再分别与 `a_h`、`x` 相乘得到两组权重梯度、沿样本维求和得到两组偏置梯度；误差信号被复用、前向激活被复用，是「反向传播」得名的由来。
- `train` 把上述零件组装成 mini-batch SGD：逐批「forward → backward → 四组参数同时 `-= lr*梯度`」，epoch 末测量记录；`np.random.seed(123)` 固定切批顺序保证可复现。
- 数值梯度检验（中心差分 vs 解析梯度）是验证手写 backward 的标准手段；注意 `backward` 的损失约定除以 n 而非 n×C，相差常数倍 C，会被学习率吸收。

## 7. 下一步学习建议

本讲结束，你已经完整拥有一个「纯 numpy 的两层神经网络 + 训练器」。它暴露了三个痛点，恰好是下一单元（第 12 章，PyTorch 入门）逐一解决的：

1. **手写梯度太苦**：`backward` 的每一行矩阵运算都要自己推导、自己检验。PyTorch 的 autograd 只需写 `loss.backward()`（u12-l3），四组梯度自动填进 `.grad`。
2. **权重散落各处**：`weight_h`、`bias_h`…是裸 numpy 数组，更新要逐个写。`nn.Module`（u12-l3）把参数收编成 `parameters()`，优化器一步更新全部。
3. **数据管线手搓**：`minibatch_generator` 只会切批。`Dataset` + `DataLoader`（u12-l2）提供 shuffle、多进程加载、图像目录数据集等成套设施。

建议按顺序学习 u12-l1（张量基础）→ u12-l2（Dataset/DataLoader）→ u12-l3（nn.Module 与 autograd 下的 XOR 实验）。届时可以回头把本讲的 MNIST 实验用 PyTorch 重写，对比两种实现的代码量与训练速度，这个对比本身就是最好的复习。
