# 感知机与逻辑回归

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 Rosenblatt 感知机的**逐样本更新规则**，并对一个错分样本手工算出权重该如何变化。
2. 读懂 `perceptron.ipynb` 中不依赖 autograd 的完整训练实现，理解它为什么只在样本被错分时才更新。
3. 读懂 `logistic-regression.ipynb` 中逻辑回归的**三种实现层次**：手工梯度版、autograd 版、`nn.Module` + 优化器版，并理解三者的训练循环为何等价。
4. 理解 sigmoid 如何把线性输出变成概率、二元交叉熵损失长什么样、它的梯度为什么形式上和感知机的"误差项"很像。
5. 对比「手工误差驱动更新」与「基于梯度的更新」的本质差异，为下一讲的 softmax 回归与标准训练循环打下基础。

## 2. 前置知识

本讲假设你已经完成 u1-l2（会运行 notebook、认识 device 选择与五段式结构），另外需要以下数学直觉，都不超过高中/大一水平：

- **矩阵乘法**：一个 \( n \times 2 \) 的样本矩阵乘以 \( 2 \times 1 \) 的权重向量，得到 \( n \times 1 \) 的打分。对应 PyTorch 里的 `torch.mm(x, weights)`。
- **二分类**：标签只有两类，本讲统一用 0/1 编码（感知机的原始理论常用 -1/+1，这一点会在 4.2 节专门解释）。
- **梯度下降的直觉**：想象损失函数是一座山，梯度指出"最陡上坡方向"，沿反方向走一小步，损失就会变小。逻辑回归是本学习手册中第一个真正使用梯度的模型。
- **导数与链式法则**：只需要知道"复合函数的导数 = 各环节导数相乘"这一句即可，本讲会直接给出推导结果。
- **前向传播 / 反向传播**：本讲只需前向（算预测）和一个手动版本的反向（算梯度），完整的从零反向传播在 u2-l5 展开。

一个重要的全局视角：**感知机与逻辑回归的"身体"是同一个**——都是线性打分 \( z = w^\top x + b \)。区别只在"大脑"：

| | 感知机 | 逻辑回归 |
|---|---|---|
| 输出 | 阶跃后的硬标签 0/1 | sigmoid 后的概率 \( \in (0,1) \) |
| 训练信号 | 错分误差 \( y - \hat{y} \in \{-1,0,1\} \) | 梯度 \( (p - y)x \)，连续可导 |
| 更新时机 | 只在错分时更新 | 每个 epoch 全样本更新 |
| 需要损失函数吗 | 不需要 | 需要（交叉熵） |
| 线性不可分数据 | 权重振荡，永不收敛 | 仍能收敛到一个"折中"解 |

带着这张表去读源码，你会发现两个 notebook 的代码结构惊人地相似——这正是作者刻意安排的教学对比。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [pytorch_ipynb/basic-ml/perceptron.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb) | Rosenblatt 感知机：手工更新规则，不用 autograd |
| [pytorch_ipynb/basic-ml/logistic-regression.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/logistic-regression.ipynb) | 逻辑回归：同一模型的三种实现层次（手工梯度 / autograd / nn.Module） |
| [pytorch_ipynb/data/perceptron_toydata.txt](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/data/perceptron_toydata.txt) | 感知机用的 2D toy 数据：100 行、制表符分隔、两列特征 + 一列标签 |

> 说明：本仓库的源码是 Jupyter Notebook（JSON 文件），下文引用的行号均指 `.ipynb` 原始 JSON 文件中的行号，点击永久链接可直接跳到对应代码。逻辑回归 notebook 的数据来自 UCI Iris 在线下载，不落盘。

## 4. 核心概念与源码讲解

### 4.1 toy 数据准备与可视化

#### 4.1.1 概念说明

为什么深度学习教学偏爱 **2D toy 数据**？因为两个特征可以画在平面直角坐标系里，模型学到的边界是一条直线，"学习"这件事从此可见。本模块解决三个问题：

1. 数据从哪来（本地 txt 文件 / 在线 Iris 数据集）；
2. 为什么要**打乱 + 划分**训练/测试集；
3. 为什么要**标准化**，而且必须只用训练集的均值和方差。

其中第 3 点承接 u1-l2 已建立的原则：测试集统计量一旦参与预处理，就发生了**数据泄漏**（data leakage）——你把"考试答案的分布"提前透露给了训练过程。

#### 4.1.2 核心流程

两个 notebook 的数据准备流程完全同构：

```
读取 2 特征 + 标签数据
    ↓
用固定种子的随机数生成器打乱样本顺序
    ↓
按索引切分：感知机 70/30，逻辑回归 75/25
    ↓
用【训练集】的 mu、sigma 标准化训练集与测试集
    ↓
散点图可视化（两色两类）
```

#### 4.1.3 源码精读

**感知机的数据准备**（读本地 toy 文件）：

[pytorch_ipynb/basic-ml/perceptron.ipynb:L98-L122](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L98-L122) —— 这段代码依次完成：`np.genfromtxt` 读入 100 个样本；用 `np.random.RandomState(123)` 创建**独立于全局种子**的打乱器，保证这段预处理可复现；按打乱后的索引把前 70 个作训练集、后 30 个作测试集；最后用**训练集**的 `mu, sigma` 同时标准化训练集与测试集。

其中 [L104](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L104) 的 `y = y.astype(np.int)` 在 NumPy 1.24+ 会报错（`np.int` 已被移除），需要改成 `y.astype(int)`——这与 u1-l2 提到的兼容性注意一致。

**逻辑回归的数据准备**（在线下载 Iris 前 100 行）：

[pytorch_ipynb/basic-ml/logistic-regression.ipynb:L104-L128](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/logistic-regression.ipynb#L104-L128) —— 这段代码用 `np.lib.DataSource` 缓存式下载 UCI Iris 数据，只取前 100 行（前 50 行山鸾尾、后 50 行变色鸾尾）的前两列特征（花萼长/宽），把标签构造成 `y = 0/1`；随后 [L115-L119](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/logistic-regression.ipynb#L115-L119) 用 `np.random.seed(1)` 打乱并切出 25 个测试样本、75 个训练样本；[L120-L121](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/logistic-regression.ipynb#L120-L121) 同样只用训练集统计量做标准化。

> 兼容性提示：新版 NumPy 已移除 `np.lib.DataSource`，本地复现时可改为 `requests.get` / `urllib` 下载，或用 `sklearn.datasets.load_iris` 取前 100 行前 2 列代替。

**散点图可视化**（看一眼数据是否线性可分）：

[pytorch_ipynb/basic-ml/perceptron.ipynb:L144-L149](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L144-L149) —— 这段代码用布尔索引 `X_train[y_train==0, 0]` 选出第 0 类样本的第一列特征作横坐标，两类用不同标记（圆/方）画在同一张图上。从 notebook 保存的输出图可以看到两类点云之间有一条清晰的空隙——**线性可分**，这正是感知机收敛的前提。

#### 4.1.4 代码实践

1. **实践目标**：亲手验证"标准化后训练集均值约为 0、标准差约为 1，且测试集用的是训练集的统计量"。
2. **操作步骤**：复制 perceptron.ipynb 的数据准备 cell 到一个新 cell（或独立脚本），在最后追加两行（示例代码）：

   ```python
   print('train mean/std:', X_train.mean(axis=0), X_train.std(axis=0))
   print('test  mean/std:', X_test.mean(axis=0), X_test.std(axis=0))
   ```

3. **需要观察的现象**：训练集均值≈0、标准差≈1；测试集均值/标准差接近但不完全等于 0/1。
4. **预期结果**：测试集统计量偏离 0/1，因为标准化用的是训练集的 `mu, sigma`——偏离是正常且正确的；若测试集也恰好是精确的 0/1，反而说明统计量泄漏了。
5. 该实验逻辑明确，具体数值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：把感知机的 70/30 划分改成 50/50，测试精度可能变大还是变小？为什么？

> **参考答案**：结果不确定，但方差会变大。训练样本从 70 减到 50，模型学到的边界更"抖"；同时测试样本变多，评估本身更稳定。小数据集上训练与评估两方面的效应会相互抵消一部分，这正是后面 u7-l2 要用 K 折交叉验证来解决的不稳定性。

**练习 2**：`np.random.RandomState(123)` 与 `np.random.seed(1)` 都能固定随机性，两者写法有什么区别？

> **参考答案**：前者创建一个**独立的**随机数生成器对象，只影响通过该对象产生的随机数，不污染全局状态，适合 notebook 中只固定某一步（如打乱）的场景；后者设置**全局**种子，影响之后所有使用全局随机源的代码。感知机 notebook 用独立生成器是为了打乱可复现、又不干扰其它 cell 的随机性（可对照 u1-l3 介绍的 `set_all_seeds`，它统一固定多个全局随机源）。

### 4.2 感知机模型与更新规则

#### 4.2.1 概念说明

感知机（Perceptron，Rosenblatt，1958）是最早的可学习神经元模型：输入特征加权求和，超过 0 就输出 1，否则输出 0。它的学习规则极其简单：

- 样本分对了 → 什么都不做；
- 样本分错了 → 把权重往该样本的方向（或反方向）推一步。

它没有损失函数、没有梯度、没有学习率（隐含为 1）。理论保证：**当数据线性可分时，感知机在有限步内收敛**（Novikoff 定理，误差次数上限与间隔成反比）；但数据线性不可分时，权重会永远振荡——这个缺陷正是本讲综合实践要亲眼看到的现象，也是历史上推动多层网络与梯度方法出现的动因之一。

#### 4.2.2 核心流程

对每个训练样本 \( (x_i, y_i) \)（标签 0/1）：

\[
z_i = w^\top x_i + b, \qquad \hat{y}_i = \mathbb{1}[z_i > 0]
\]

更新规则（学习率 \( \eta = 1 \)）：

\[
w \leftarrow w + \eta \, (y_i - \hat{y}_i)\, x_i, \qquad b \leftarrow b + \eta \, (y_i - \hat{y}_i)
\]

误差项 \( e_i = y_i - \hat{y}_i \) 只有三种取值，每种对应明确的几何动作：

| \( e_i \) | 含义 | 更新效果 |
|---|---|---|
| \( 0 \) | 预测正确 | 权重不动 |
| \( +1 \)（真实 1，预测 0） | 假阴性 | \( w \) 向 \( x_i \) 方向移动，未来更倾向打 1 |
| \( -1 \)（真实 0，预测 1） | 假阳性 | \( w \) 向 \( -x_i \) 方向移动，未来更倾向打 0 |

训练循环伪代码：

```
for epoch in range(epochs):
    for 每个样本 x_i（逐样本！）:
        e = y_i - predict(x_i)
        w += e * x_i
        b += e
```

注意与下一节逻辑回归的关键差异：这里是**逐样本（stochastic）**更新，且完全不需要求导。

#### 4.2.3 源码精读

感知机的全部模型代码集中在一个 cell 中：

[pytorch_ipynb/basic-ml/perceptron.ipynb:L165-L201](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L165-L201) —— 这段代码定义了 `custom_where` 与完整的 `Perceptron` 类：权重和偏置初始化为全零张量并直接放到 `device` 上；注意它们是**普通张量**，没有 `requires_grad`——本 notebook 自始至终不用 autograd。逐段看：

**前向传播** [L179-L182](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L179-L182)：

```python
def forward(self, x):
    linear = torch.add(torch.mm(x, self.weights), self.bias)
    predictions = custom_where(linear > 0., 1, 0).float()
    return predictions
```

`torch.mm` 是矩阵乘法（批量的 \( x w + b \)），`custom_where(cond, 1, 0)` 用算术（乘加）模拟了 `torch.where` 的阶跃：条件为真取 1、为假取 0。这就是阶跃函数 \( \mathbb{1}[z>0] \) 的张量化写法。

**误差计算** [L184-L188](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L184-L188) —— `backward` 这个名字有点误导：它只是计算 \( e = y - \hat{y} \)，没有任何梯度。方法名叫 backward 只是为了形式上与 forward 对仗。

**逐样本训练循环** [L189-L197](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L189-L197)：

```python
def train(self, x, y, epochs):
    for e in range(epochs):
        for i in range(y.size()[0]):
            errors = self.backward(x[i].view(1, self.num_features), y[i]).view(-1)
            self.weights += (errors * x[i]).view(self.num_features, 1)
            self.bias += errors
```

内层循环逐个取样本，`view(1, self.num_features)` 把一维样本升成 \( 1\times2 \) 的矩阵以配合 `torch.mm` 的形状要求（注释也说明了这一点）；随后正是更新公式 \( w \leftarrow w + e\,x \)、\( b \leftarrow b + e \)。没有学习率参数——隐含为 1。

**训练调用与结果** [L228-L236](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L228-L236) —— 把 NumPy 数组转成 `torch.tensor` 并搬到 `device`，训练 5 个 epoch。notebook 保存的输出显示最终权重 `[[1.2734], [1.3464]]`、偏置 `-1.0`。

**评估** [L198-L201](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L198-L201) 与 [L261-L265](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L261-L265) —— 准确率 = 预测等于标签的个数除以样本数；保存的输出为测试集 **93.33%**（30 个测试样本错 2 个）。

**决策边界可视化** [L287-L314](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L287-L314) —— 这段代码手工解出直线方程：边界 \( w_1 x_1 + w_2 x_2 + b = 0 \) 上，给定 \( x_1 \) 时 \( x_2 = -(w_1 x_1 + b)/w_2 \)。取 \( x_1 \in \{-2, 2\} \) 两端点连线，分别叠加在训练/测试散点上。这个"两点连线画边界"的小技巧会在仓库多个 notebook 中复用。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看到"错分才更新"——统计每个 epoch 内发生更新的次数。
2. **操作步骤**：把 `train` 方法复制到一个新 cell，在内层循环里加一个计数器（示例代码）：

   ```python
   def train_with_count(self, x, y, epochs):
       for e in range(epochs):
           n_updates = 0
           for i in range(y.size()[0]):
               errors = self.backward(x[i].view(1, self.num_features), y[i]).view(-1)
               if errors.abs().sum() > 0:
                   n_updates += 1
               self.weights += (errors * x[i]).view(self.num_features, 1)
               self.bias += errors
           print(f'epoch {e+1}: {n_updates} updates')
   ```

   然后用 `Perceptron.train_with_count = train_with_count` 把方法挂到类上再训练。
3. **需要观察的现象**：随着 epoch 推进，每个 epoch 的更新次数总体递减。
4. **预期结果**：数据线性可分时更新次数逐渐降到 0（此后权重不再变化）；若某个 epoch 更新数为 0，说明已经收敛。
5. 具体次数**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：经典感知机理论常用 -1/+1 标签，本 notebook 用 0/1。用 0/1 编码时更新公式 \( w \leftarrow w + (y - \hat{y})x \) 与 -1/+1 编码下的公式是什么关系？

> **参考答案**：-1/+1 编码下更新为 \( w \leftarrow w + y\,\hat{y}_{\text{sign}}\,x \)（当预测符号与标签不一致时才更新，系数恰为 ±1）。0/1 编码下 \( y - \hat{y} \in \{-1, 0, +1\} \) 给出了完全相同的"±1 或不更新"三种动作，两者等价：预测正确时误差为 0，预测错误时误差的符号正是需要修正的方向。

**练习 2**：感知机的 `train` 里没有学习率参数。如果想加一个 `learning_rate`，应该改哪两行？加上之后对线性可分数据有什么影响？

> **参考答案**：改 `self.weights += ...` 与 `self.bias += ...` 两行，分别乘上 `learning_rate`。对线性可分数据，最终能否收敛不受步长影响（更新次数上限会变），但收敛路径不同：步长太小收敛慢；步长超过 1 也仍能收敛（Novikoff 定理只依赖是否错分），只是边界可能"过冲"后来回修正。

**练习 3**：为什么感知机无法表达"每个样本有多大概率属于第 1 类"，而逻辑回归可以？

> **参考答案**：感知机的输出经过阶跃函数，只有 0/1 两个离散值，中间信息全部丢失，也没有可供优化的连续损失；逻辑回归用 sigmoid 输出连续概率，配合交叉熵损失形成光滑的优化目标——这正是 4.3 节的主题。

### 4.3 逻辑回归与二元交叉熵：三种实现层次

#### 4.3.1 概念说明

逻辑回归名字里带"回归"，实际是分类器。它在感知机的"身体"上换了一个"大脑"：

\[
z = w^\top x + b, \qquad p = \sigma(z) = \frac{1}{1 + e^{-z}}
\]

sigmoid \( \sigma \) 把任意实数压缩到 \( (0,1) \)，可以当作"属于第 1 类的概率"。训练目标是最大化似然，等价于最小化**二元交叉熵**（对 \( n \) 个样本求和的形式）：

\[
L = -\sum_{i=1}^{n} \Big[ y_i \log p_i + (1 - y_i)\log(1 - p_i) \Big]
\]

对 \( w \) 求梯度（利用 \( \sigma'(z) = \sigma(z)(1-\sigma(z)) \)，链式法则中 sigmoid 的导数恰好与交叉熵的导数相消，得到异常简洁的结果）：

\[
\frac{\partial L}{\partial w} = \sum_{i=1}^{n} (p_i - y_i)\, x_i
\]

注意这个梯度与感知机的误差 \( (y - \hat y) x \) **形状几乎一样，只是符号相反、且误差是连续的** \( p - y \)——这就是"逻辑回归是感知机的可微版本"这句话的确切含义。

notebook 用**三种层次**实现同一个模型，这是本讲最值得精读的部分：

| 版本 | 梯度从哪来 | 参数更新 | 对应代码 |
|---|---|---|---|
| `LogisticRegression1` | 手工推导公式 | 手工张量运算 | 低层 API + manual gradients |
| `LogisticRegression2` | `cost.backward()` 自动求导 | 手工 `detach` + 原位减 | 低层 API + autograd |
| `LogisticRegression3` | `cost.backward()` | `optimizer.step()` | `nn.Module` + `BCELoss` + `SGD` |

第三种正是全仓库所有"正式" notebook 使用的标准范式（u1-l3 的 helper 训练循环同款），前两种是帮你理解它背后发生了什么。

#### 4.3.2 核心流程

标准梯度下降训练循环（batch 版，全样本一步）：

```
初始化 w = 0, b = 0
for epoch in range(num_epochs):
    proba = sigmoid(x @ w + b)          # ① 前向
    cost  = -Σ[y·log p + (1-y)·log(1-p)] # ② 损失
    cost.backward()                      # ③ 求梯度（自动或手工）
    w -= learning_rate * grad_w          # ④ 更新
    b -= learning_rate * grad_b
    grad 清零                              # ⑤ 为下一轮做准备
```

第三种实现里这五步被封装成 PyTorch 的惯用写法：**forward → loss → `zero_grad` → `backward` → `step`**。请记住这五个动词，它是读懂后续所有 notebook 的钥匙。

#### 4.3.3 源码精读

**(a) 手工梯度版 `LogisticRegression1`**

[pytorch_ipynb/basic-ml/logistic-regression.ipynb:L144-L202](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/logistic-regression.ipynb#L144-L202) —— 类结构与感知机几乎逐行对应（零初始化权重、`forward` 里同样的 `torch.mm`），差别只在三处：

- [L158-L162](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/logistic-regression.ipynb#L158-L162) `forward` 末尾接 `_sigmoid` 输出概率而非阶跃；
- [L180-L184](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/logistic-regression.ipynb#L180-L184) `_logit_cost` 就是交叉熵公式 \( -[y\log p + (1-y)\log(1-p)] \) 的张量实现（`torch.mm` 把逐样本对数损失求和）；
- [L185-L202](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/logistic-regression.ipynb#L185-L202) `train` 中最关键的两行：

```python
errors = self.backward(probas, y)                              # y - p
neg_grad = torch.mm(x.transpose(0, 1), errors.view(-1, 1))     # x^T (y - p)
self.weights += learning_rate * neg_grad
self.bias += learning_rate * torch.sum(errors)
```

[L193](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/logistic-regression.ipynb#L193) 算的正是 \( x^\top(y-p) \)，即 \( -\frac{\partial L}{\partial w} \)（负梯度），所以 [L196-L197](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/logistic-regression.ipynb#L196-L197) 用 `+=` 沿负梯度方向走——数学上与 4.3.1 的推导严格一致。

> **读输出的一个小彩蛋**：这一版保存的输出前两个 epoch 显示 `Train ACC: 0.000`，而 cost 明明在下降。原因是 `evaluate` 里 `torch.sum(labels == y)` 得到整型张量，在旧版 PyTorch 中整型张量除以整数做的是截断除法，任何小于 100% 的准确率都被截成 0；后面 `LogisticRegression2` 的 `evaluate` 给求和结果加了 `.float()`，就正确显示 0.987 了。这是很典型的张量 dtype 陷阱，值得记住。

**(b) autograd 版 `LogisticRegression2`**

[pytorch_ipynb/basic-ml/logistic-regression.ipynb:L334-L401](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/logistic-regression.ipynb#L334-L401) —— 与 (a) 的唯一区别是"求梯度"这一步交给 autograd：

- [L338-L350](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/logistic-regression.ipynb#L338-L350)：权重和偏置在创建时加了 `requires_grad=True`（[L345](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/logistic-regression.ipynb#L345) 与 [L349](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/logistic-regression.ipynb#L349)，注释写着 `# req. for autograd!`）——从这一刻起，PyTorch 会记录所有涉及这两个张量的运算，形成计算图；
- [L379-L383](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/logistic-regression.ipynb#L379-L383)：先前向算 `cost`，再 `cost.backward()`——autograd 沿计算图反向传播，把 \( \partial L/\partial w \)、\( \partial L/\partial b \) 写进 `.grad` 属性。手工推导的 `x^T(p-y)` 公式在这里被完全替代；
- [L387-L395](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/logistic-regression.ipynb#L387-L395)：更新与清零。`detach()` 返回与原张量**共享存储**的视图，所以对 `tmp` 的原位减法会真正修改权重，同时避免更新操作本身被记进计算图；最后 `grad.zero_()` 把梯度清零，否则下个 epoch 的梯度会累加到旧值上（PyTorch 的梯度默认是累加语义）。

保存的输出显示 (a)、(b) 两版 10 个 epoch 后权重完全一致（`4.2267 / -2.9613`、偏置 `0.0994`，测试准确率均 100%）——这是"autograd 算的梯度 == 手工推导的梯度"最直接的实证。

**(c) `nn.Module` 版 `LogisticRegression3`：全仓库标准范式**

[pytorch_ipynb/basic-ml/logistic-regression.ipynb:L533-L553](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/logistic-regression.ipynb#L533-L553) —— 继承 `torch.nn.Module`，用现成的 `nn.Linear(num_features, 1)` 代替手工维护的 `weights/bias`（Linear 内部自动带 `requires_grad=True`）；[L542-L543](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/logistic-regression.ipynb#L542-L543) 把权重清零是为了与前两个版本的零初始化对齐、便于对照输出（注释里还提醒：下划线后缀表示原位操作）。

[pytorch_ipynb/basic-ml/logistic-regression.ipynb:L562-L567](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/logistic-regression.ipynb#L562-L567) —— 损失用 `torch.nn.BCELoss(reduction='sum')`（与手工版的"求和、不除以样本数"口径一致），优化器用 `torch.optim.SGD(model.parameters(), lr=0.1)`。

[pytorch_ipynb/basic-ml/logistic-regression.ipynb:L601-L637](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/logistic-regression.ipynb#L601-L637) —— 标准训练循环，核心五行：

```python
out = model(X_train_tensor)              # ① forward
cost = cost_fn(out, y_train_tensor)      # ② loss
optimizer.zero_grad()                    # ③ 清梯度
cost.backward()                          # ④ backward
optimizer.step()                         # ⑤ step 更新参数
```

保存的输出显示这版与 (a)(b) 的逐 epoch cost 完全相同、最终参数也相同——三种实现是同一个数学对象的三种写法。

#### 4.3.4 代码实践

1. **实践目标**：验证 `BCELoss` 的 `reduction` 参数与学习率之间的换算关系。
2. **操作步骤**：把 [L563](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/logistic-regression.ipynb#L563) 的 `reduction='sum'` 改为 `'mean'`（损失除以样本数 75，等效于梯度缩小 75 倍），同时把 [L567](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/logistic-regression.ipynb#L567) 的 `lr=0.1` 改为 `0.1 * 75`，重跑训练循环 cell。
3. **需要观察的现象**：逐 epoch 打印的 cost 数值变为原来的约 1/75，但训练准确率的爬升节奏与原版一致。
4. **预期结果**：`mean` 损失 + 等比放大的学习率 ≈ `sum` 损失 + 原学习率，最终权重与原版基本一致（浮点误差允许范围内）。这说明"损失口径"与"学习率"是一对需要配套看待的量。
5. **待本地验证**：受浮点舍入影响，两版参数可能在小数点后几位有细微差别。

#### 4.3.5 小练习与答案

**练习 1**：`LogisticRegression2.train` 里如果不调用 `self.weights.grad.zero_()`，会发生什么？

> **参考答案**：PyTorch 的梯度是**累加**的：`backward()` 每次把新梯度加到 `.grad` 已有值上。不清零的话，第 2 个 epoch 用的其实是"第 1 轮梯度的 2 倍"，第 3 个 epoch 是 3 倍……有效学习率不断膨胀，训练震荡甚至发散。`nn.Module` 版里等价的语句是 `optimizer.zero_grad()`（标准五步的第三步）。

**练习 2**：为什么 `LogisticRegression2` 更新权重时必须先 `detach()`，而 `nn.Module` 版可以直接 `optimizer.step()`？

> **参考答案**：`detach()` 返回与原张量共享存储、但脱离计算图的视图。如果直接对带 `requires_grad=True` 的张量做原位更新 `weights -= lr * grad`，autograd 会试图记录并反向传播这条更新路径本身，引发报错（对 autograd 记录的张量做原位操作破坏计算图）。`optimizer.step()` 内部用的是 `torch.no_grad()` 上下文里的无梯度更新，等价于替你做了 `detach` 这件事。

**练习 3**：逻辑回归在感知机收敛不了的线性不可分数据上会怎样？

> **参考答案**：交叉熵损失是凸函数，无论数据是否可分，梯度下降都会收敛到唯一（或稳定）的最优解——一个训练误差最小意义下的"折中"边界，并且输出概率依然有意义（重叠区域的样本概率靠近 0.5）。感知机则因错分样本永远存在而无限更新。完整实验见第 5 节综合实践。

## 5. 综合实践

**任务：制造线性不可分数据，让感知机与逻辑回归同场对比。**

这是本讲的收官实验，把 4.1–4.3 的内容全部串起来。

**步骤 1：生成可控重叠度的数据**（示例代码，保存为独立脚本或 notebook cell）：

```python
import numpy as np

rng = np.random.RandomState(42)
n = 100
# 关键参数：两类均值 centers 与标准差 std。std 越大 / 均值越近，重叠越严重
std = 2.0                                    # 原始 toy 数据约 1.0 量级
X0 = rng.normal(loc=[-1, -1], scale=std, size=(n // 2, 2))
X1 = rng.normal(loc=[ 1,  1], scale=std, size=(n - n // 2, 2))
X = np.vstack([X0, X1])
y = np.array([0] * (n // 2) + [1] * (n - n // 2))
np.savetxt('my_toydata_overlap.txt', np.column_stack([X, y]), delimiter='\t')
```

**步骤 2：接入感知机 notebook**。把 [perceptron.ipynb L102](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/perceptron.ipynb#L102) 的 `genfromtxt` 路径换成你生成的文件（注意保持仓库目录结构不动，把新文件放到 notebook 可寻址的相对路径），其余 cell 原样运行。

**步骤 3：接入逻辑回归 notebook**。把 [logistic-regression.ipynb L108-L113](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/logistic-regression.ipynb#L108-L113) 的下载与标签构造替换为同一份 `X, y`，后续标准化、切分、`LogisticRegression3` 训练循环照旧。

**步骤 4：观察并记录**（建议做 3 组：std = 0.8 / 1.5 / 2.5）：

- 感知机：保存的 5 个 epoch 后权重是否还随 epoch 剧烈变化？决策边界（L287 的画图代码）是否在不同随机种子下来回摆动？测试准确率是否明显低于逻辑回归？
- 逻辑回归：cost 是否仍然单调下降并趋于平稳？重叠区样本的预测概率是否靠近 0.5（可在评估 cell 打印 `probas` 查看）？

**预期结果**：std 较小时两者都好；std 增大后，感知机准确率下滑且解不稳定（每次运行边界差异大），逻辑回归仍给出稳定、可解释的边界与概率。具体数值**待本地验证**——这正是感知机历史局限性的第一手体验，也是为什么下一章的 softmax 回归与神经网络全部建立在"损失函数 + 梯度"范式之上。

## 6. 本讲小结

- 感知机与逻辑回归共享同一个线性"身体" \( z = w^\top x + b \)，区别在输出端（阶跃 vs sigmoid）与训练信号（离散误差 vs 连续梯度）。
- 感知机的更新规则 \( w \leftarrow w + (y - \hat y)x \) 只在错分时触发、学习率隐含为 1，线性可分时有限步收敛、不可分时永不收敛。
- 交叉熵对权重的梯度是 \( \sum (p - y)x \)，与感知机误差项形式相同但连续可导——逻辑回归因此是"可微的感知机"。
- logistic-regression.ipynb 用三种实现证明同一数学：手工梯度、autograd（`requires_grad` + `backward` + `detach` + `grad.zero_()`）、`nn.Module` + `BCELoss` + `SGD`，三版逐 epoch cost 与最终权重一致。
- `forward → loss → zero_grad → backward → step` 五步范式在本讲首次完整亮相，是读懂仓库其余全部 notebook 的钥匙。
- 数据准备中的两个工程细节：标准化只用训练集统计量（防泄漏）；`np.int` / `np.lib.DataSource` 等旧 API 在新版环境需替换。

## 7. 下一步学习建议

- 下一讲 **u2-l2（Softmax 回归与第一个完整训练循环）**：把二分类的 sigmoid + BCE 推广到多分类的 softmax + 交叉熵，并首次用 `datasets.MNIST` + `DataLoader` 跑完整标准训练循环——本讲的五步范式将原封不动地复用。
- 若想深挖 autograd 的内部机制（计算图、`detach`、hook），可先跳读 u4-l5（Autograd 细节），再回来继续主线。
- 若想彻底搞懂反向传播的链式法则，u2-l5（从零实现反向传播）会手工实现一个 MLP 的梯度，与本讲 4.3 的手工梯度推导一脉相承。
- 建议同时回看 [pytorch_ipynb/basic-ml/](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/) 目录下的 `softmax-regression.ipynb`，它介于本讲与下一讲之间，可作为预习材料。
