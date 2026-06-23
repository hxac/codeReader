# 从 examples 开始：第一个 AI 程序

## 1. 本讲目标

学完本讲后，你应当能够：

- 理解「人工智能」最朴素的核心思想：**从数据中学习规律（权重）**，而不是由人手写规则。
- 读懂并运行 `examples/01-hello-ai-world.py`，看清楚「预测 → 算误差 → 更新权重」这一最小训练循环是如何运作的。
- 理解**训练数据与模式**、**权重与学习率**、**训练循环与误差**这三个互相咬合的概念。
- 读懂 `examples/02-simple-neural-network.py`，明白「神经元 = 加权求和 + 偏置 + 激活函数」的从零实现思路，为后续真正的神经网络课程打基础。
- 亲手修改示例代码，让模型学习 `y = 3x` 的新规律。

> 本讲的两个脚本只用了 Python 标准库（`random`、`math`），**不需要** PyTorch / TensorFlow，也不依赖上一讲搭建的 `ai4beg` 环境。只要你的机器装了 Python 3.8 以上就能直接 `python xxx.py` 跑起来。这让它们成为理解「AI 在底层到底在干什么」的最佳起点。

---

## 2. 前置知识

在进入源码前，先用大白话建立几个直觉。

### 2.1 什么是「模式」与「学习」

假设我给你这样一组数字配对：

| 输入 x | 正确输出 y |
|--------|-----------|
| 1 | 2 |
| 2 | 4 |
| 3 | 6 |

你大概率会立刻看出规律：「y 是 x 的两倍」，即 \(y = 2x\)。这种**数据里稳定存在的关系**就叫「模式（pattern）」。人脑善于一眼看出简单模式，但面对上万维、上千行的数据就无能为力了——这正是计算机「学习」要解决的问题。

**AI 的「学习」不是玄学**，它的本质是：给程序一些**参数**（本讲里只有一个数，叫**权重 weight**），让它一遍遍试，每试一次就根据「错得有多离谱」微调参数，直到它能在没见过的数据上也答对。这就是本讲要演示的全部内容。

### 2.2 三个关键词

- **权重（weight）**：模型唯一要「学」出来的东西。在 `y = 2x` 里，权重就是那个系数 `2`。模型一开始不知道是 `2`，只能瞎猜，然后慢慢逼近。
- **误差（error）**：模型预测值与真实值之间的差距，是模型调整自己的唯一依据。
- **学习率（learning rate）**：每次根据误差调整权重时的「步长」。步子太大可能跳过正确答案，步子太小可能学得极慢。

### 2.3 本讲与上一讲的衔接

上一讲（u1-l3）你搭建了 `ai4beg` 的 conda 环境来跑 Jupyter Notebook。本讲的脚本更轻量，纯 Python 即可运行；但如果你想顺便验证上一讲的环境可用，在 `ai4beg` 环境里运行它们也完全没问题。把这两个脚本跑通，能帮你确认「我的机器能执行这门课的代码」，为后续 Notebook 实践扫清心理障碍。

---

## 3. 本讲源码地图

本讲只涉及 `examples/` 目录下的三个文件：

| 文件 | 作用 | 本讲用法 |
|------|------|---------|
| [examples/README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/examples/README.md) | 整个 `examples/` 目录的导读，用表格列出全部示例及其难度、前置要求，并给出推荐学习顺序。 | 先看它建立全局认识，知道我们从哪里开始、能往哪里走。 |
| [examples/01-hello-ai-world.py](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/examples/01-hello-ai-world.py) | 一个名为 `SimpleAILearner` 的类，用约 140 行纯 Python 学习线性关系 `y = 2x`。 | 本讲的主线，三个核心概念全部围绕它讲解。 |
| [examples/02-simple-neural-network.py](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/examples/02-simple-neural-network.py) | 一个名为 `SimpleNeuron` 的类，从零实现单个人工神经元（含 sigmoid 激活、前向/反向传播），把二维点分类到直线 `y = x` 的上下两侧。 | 本讲的进阶模块，把「线性学习器」升级成「神经元」，铺垫后续神经网络课程。 |

`examples/README.md` 把目录定位为「对初学者友好的、独立可运行的示例」集合，难度从 ⭐ 到 ⭐⭐，明确要求只需「Python 基础」即可起步，这正好契合本讲的入门定位。

---

## 4. 核心概念与源码讲解

### 4.1 训练数据与模式

#### 4.1.1 概念说明

任何机器学习的第一步都是**准备训练数据**。训练数据的形态直接决定模型能学到什么。

在本讲的例子里，作者把一个抽象问题「学会 y = 2x」转化成了一张具体的表格：给出若干个 `(输入 x, 正确输出 y)` 的配对。模型看不到「乘以 2」这条规则，它只能看到这五组数字，然后**自己猜**出规律。

这里有两个要点：
- **数据即规则**：只要样本足够有代表性，模型就能从数据里「反推」出生成这些数据的规则。
- **监督信号**：每条数据都带有「正确答案 y」，这种带答案的训练方式叫**监督学习（supervised learning）**。误差正是用「模型的预测」和「这个正确答案」比较得到的。

#### 4.1.2 核心流程

```
准备训练数据 [(x₁,y₁), (x₂,y₂), ...]
        ↓
模型对每个 x 给出预测 ŷ
        ↓
对比 ŷ 与真实 y，发现模型还差得远
        ↓
（后续模块：据此调整权重，让模型越来越准）
```

#### 4.1.3 源码精读

README 用一张表格介绍 `examples/` 里四个示例的难度阶梯，`01-hello-ai-world.py` 被标注为最低的 ⭐「Beginner」，前置要求仅「Python basics」：

[examples/README.md:7-12](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/examples/README.md#L7-L12) —— 示例总览表，标注难度与前置要求，本讲从第一行 ⭐ 难度的示例切入。

`01-hello-ai-world.py` 的 `main()` 一开篇就声明了要学的模式，并把抽象规则落成五条具体训练样本：

[examples/01-hello-ai-world.py:93-99](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/examples/01-hello-ai-world.py#L93-L99) —— 把「y = 2x」具象成 5 组 `(x, y)` 训练数据，如 `(1,2)`、`(2,4)` … `(5,10)`，每行注释还点明了正确答案应是多少。

训练结束后，`main()` 用一组**模型没见过的新输入** `[6, 7, 10, 15]` 来检验模型是否真的「学会了」而不是「死记硬背」：

[examples/01-hello-ai-world.py:111-116](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/examples/01-hello-ai-world.py#L111-L116) —— 用训练集之外的新数据测试，并打印「预测值 / 真实值 / 误差」三列，这是判断模型有没有真正学到规律的关键一步。

#### 4.1.4 代码实践

**实践目标**：亲手体会「训练数据决定了模型能学到什么」。

**操作步骤**：
1. 打开 `examples/01-hello-ai-world.py`，找到第 93–99 行的 `training_data`。
2. 先**不改任何代码**，在终端运行：
   ```bash
   python examples/01-hello-ai-world.py
   ```
3. 观察脚本最后打印的测试表（Input / Prediction / Actual / Difference）。

**需要观察的现象**：对于 `y = 2x` 这个任务，训练后模型对新输入 `6,7,10,15` 的预测应当非常接近真实值 `12,14,20,30`，Difference 列应当接近 0。

**预期结果**：训练前权重是随机的，预测会很离谱；训练 100 轮后权重收敛到约 `2.0`，测试误差趋近 0。这证明模型确实从 5 条样本里学到了 `2` 这个系数。

> 注：权重初始值由 `random.uniform(0,5)` 随机生成，因此每次运行的收敛轨迹可能略有不同，但最终都会逼近 `2.0`。

#### 4.1.5 小练习与答案

**练习 1**：如果把训练数据改成 `[(1,3), (2,6), (3,9), (4,12), (5,15)]`，模型最终应当学到多大的权重？为什么？

**答案**：应当学到约 `3.0`。因为这组数据对应的是 \(y = 3x\)，而本讲模型的形式是 \(\hat{y} = w \cdot x\)，所以权重 w 就等于该线性关系的斜率 `3`。这也正是本讲综合实践要做的事。

**练习 2**：为什么测试输入要选 `6,7,10,15` 这些**训练集里没有**的数？

**答案**：为了检验模型是否真正「学到了规律」而非「背下了答案」。如果它只背下了 5 个训练点，那么对没见过的输入就会答错；只有当它真的掌握了 \(y=2x\) 这条规则，才能在新输入上也准确预测——这才是「学习」成功的标志。

---

### 4.2 权重与学习率更新

#### 4.2.1 概念说明

知道了「数据决定学什么」，下一个问题是：**模型靠什么来预测，又靠什么来改进？**

在本讲最简单的模型里，答案就是一个数——**权重 w**。预测公式就是一行：

\[
\hat{y} = w \cdot x
\]

模型一开始用一个随机数当权重（瞎猜），然后根据每次预测的误差来**调整**它。调整的「方向」由误差的正负决定（预测偏小就增大权重，偏大就减小），调整的「幅度」由**学习率** \(\eta\) 和输入 x 共同决定：

\[
w \;\leftarrow\; w + \eta \cdot \underbrace{(y - \hat{y})}_{\text{误差}} \cdot x
\]

直觉解读：
- 误差 \(y - \hat{y}\) 大，说明错得离谱，要多调一点；
- 输入 x 大，说明这个权重对结果的影响大，调整它「性价比」高；
- 学习率 \(\eta\) 是一个全局的「步长旋钮」，控制所有调整的整体大小。

学习率非常关键：太大，权重会在正确答案附近来回横跳、收不敛；太小，需要极多轮才能学到位。这其实是**梯度下降（gradient descent）**的一种简化形式，后续神经网络课程会把它推广成完整的反向传播。

#### 4.2.2 核心流程

```
预测：   ŷ = w · x
算误差： error = y - ŷ
更新：   w = w + η · error · x        ← 这一行就是「学习」
（重复，直到 error 足够小）
```

#### 4.2.3 源码精读

权重和学习率在 `SimpleAILearner.__init__` 中初始化。注意权重是**随机**的初值，而学习率是人为设定的固定值 `0.01`：

[examples/01-hello-ai-world.py:26-30](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/examples/01-hello-ai-world.py#L26-L30) —— `__init__` 初始化：权重 `random.uniform(0, 5)` 随机起手，学习率固定为 `0.01`。这就是模型全部的「待学参数」和「旋钮」。

预测逻辑极其朴素，就一行乘法：

[examples/01-hello-ai-world.py:32-42](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/examples/01-hello-ai-world.py#L32-L42) —— `predict(self, x)` 直接返回 `self.weight * x`，对应公式 \(\hat{y}=w\cdot x\)。

真正「学习」发生的那一行藏在训练循环里，正是前面公式 \(w \leftarrow w + \eta \cdot \text{error} \cdot x\) 的逐字翻译：

[examples/01-hello-ai-world.py:61-68](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/examples/01-hello-ai-world.py#L61-L68) —— 对每条样本先 `predict` 得到 `y_predicted`，再算 `error = y_actual - y_predicted`，最后 `self.weight += self.learning_rate * error * x` 完成权重更新。注释明确写着「this is learning!」。

#### 4.2.4 代码实践

**实践目标**：直观感受学习率对「学习速度与稳定性」的影响。

**操作步骤**：
1. 复制一份 `01-hello-ai-world.py`（或在原文件临时改），定位第 30 行：
   ```python
   self.learning_rate = 0.01
   ```
2. 把它依次改成 `0.001`（太小）、`0.01`（原值）、`0.1`（偏大），每次都运行：
   ```bash
   python examples/01-hello-ai-world.py
   ```
3. 记下每种取值下，训练结束时打印的「Average error」和「Final weight」。

**需要观察的现象**：
- `0.001`：100 轮后 Final weight 可能还没到 2.0，Average error 仍偏大——学得太慢。
- `0.01`：正常收敛到约 2.0。
- `0.1`：可能收敛更快，但在某些随机初值下会出现误差反复跳动——步长过大。

**预期结果**：你会看到学习率是一个需要权衡的超参数；本例 `0.01` 是一个稳妥的折中。**待本地验证**不同学习率在你机器上的具体收敛曲线。

#### 4.2.5 小练习与答案

**练习 1**：在权重更新公式 `self.weight += self.learning_rate * error * x` 里，如果把末尾的 `* x` 去掉（变成 `+= self.learning_rate * error`），模型还能正常学会 `y = 2x` 吗？会出什么问题？

**答案**：会变差。`* x` 让权重的调整幅度与输入大小成正比，相当于一个简化的梯度项 \(\partial \hat{y}/\partial w = x\)。去掉后，无论输入是大是小，每条样本对权重的「拉动」都一样强，收敛会变慢，且对不同尺度的数据不够合理。这能帮你体会梯度下降里「导数」项的意义。

**练习 2**：学习率设成 `1.0` 甚至 `10.0` 会怎样？

**答案**：权重更新步长过大，每一步都「跨过头」，会在正确答案两侧来回震荡，误差不降反升，即**发散（divergence）**。这正是深度学习里需要调学习率、有时还要用学习率衰减策略的根本原因。

---

### 4.3 训练循环与误差

#### 4.3.1 概念说明

光有「更新一行权重」还不够——模型不可能看一遍 5 条数据就学准。真实的训练是一个**循环**：把全部训练样本过一遍叫一个 **epoch（轮次）**，然后重复很多个 epoch，每次都重新算误差、更新权重，误差总体上越来越小。

「训练循环」由两层构成：
- **外层 epoch 循环**：控制把数据整体看多少遍。
- **内层样本循环**：在每一轮里，逐条样本算预测、算误差、更新权重。

「误差」在这里扮演两个角色：
1. **训练依据**：每条样本的 `error` 直接驱动权重更新。
2. **监控指标**：把一轮里所有样本的误差累加起来，得到 `total_error`，用来观察「整体学得怎么样了」。误差随 epoch 下降，说明在收敛；如果不再下降甚至上升，说明出问题了。

这种「反复看、反复改、用误差监控」的结构，是几乎所有机器学习训练流程的骨架，后续 PyTorch/Keras 课程的训练循环也长这样，只是更复杂。

#### 4.3.2 核心流程

```
for epoch in range(epochs):           # 外层：重复很多轮
    total_error = 0
    for (x, y_actual) in training_data:   # 内层：逐条样本
        y_predicted = predict(x)
        error = y_actual - y_predicted
        total_error += abs(error)
        weight += learning_rate * error * x
    每 20 轮打印一次 total_error，监控收敛
```

#### 4.3.3 源码精读

`train` 方法实现了上述双层循环，并在每 20 个 epoch 打印一次平均误差，让你眼睁睁看着误差下降、权重爬升：

[examples/01-hello-ai-world.py:44-75](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/examples/01-hello-ai-world.py#L44-L75) —— `train` 方法：`for epoch in range(epochs)` 是外层轮次循环，`for x, y_actual in training_data` 是内层样本循环；每 20 轮打印一次 `Average error` 和当前 `Weight`。

外层 epoch 的数量由调用方传入。`main()` 里默认训练 100 轮：

[examples/01-hello-ai-world.py:104-105](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/examples/01-hello-ai-world.py#L104-L105) —— `main()` 创建 `SimpleAILearner()` 并调用 `ai.train(training_data, epochs=100)`，即「把这 5 条样本整体看 100 遍」。

#### 4.3.4 代码实践

**实践目标**：体会「训练轮次（epochs）越多，未必越好；监控误差才能判断」。

**操作步骤**：
1. 在 `main()` 的第 105 行，把 `epochs=100` 依次改成 `5`、`20`、`100`、`1000`，每次运行。
2. 观察打印的「Average error」曲线与最终测试的 Difference 列。

**需要观察的现象**：
- `epochs=5`：训练不充分，权重可能停在 1.x，测试误差明显。
- `epochs=100`：基本收敛，测试误差接近 0。
- `epochs=1000`：误差已经很小，继续加轮次收益递减（在本例这种极简线性问题上，再多轮也不会更准，因为已经逼近最优）。

**预期结果**：你会看到一个典型的「误差随训练轮次递减、最终趋于平稳」的收敛曲线。这正是判断「训练是否完成」的经验依据。**待本地验证**不同 epoch 数对应的最终权重。

#### 4.3.5 小练习与答案

**练习 1**：脚本用 `total_error += abs(error)` 累加**绝对误差**来监控。如果改成累加**平方误差** `error ** 2`，监控结论会改变吗？

**答案**：总体收敛趋势不会变（都随训练下降并趋平），但平方误差会放大那些「错得特别离谱」的样本的影响，使监控曲线对大误差更敏感。这正是机器学习里常用 MSE（均方误差）的原因之一——它对大误差惩罚更重。注意：这里改的只是**监控**用的累加量，真正驱动权重更新的 `error` 仍是 `y - ŷ`，没变。

**练习 2**：如果训练数据本身有矛盾（比如同时给 `(1, 2)` 和 `(1, 5)`），训练循环会怎样？

**答案**：模型无法同时满足两条矛盾的样本，权重会在两者之间被反复拉扯，误差无法收敛到 0，只能收敛到某个「折中」值（本例里会停在 3.5 附近）。这说明**训练数据的质量**比训练轮次更重要——垃圾进，垃圾出。

---

### 4.4 从线性学习器到神经元：02 的从零实现

#### 4.4.1 概念说明

前三个模块的 `SimpleAILearner` 只有一个权重，只能学最简单的 \(\hat{y}=w\cdot x\)。`02-simple-neural-network.py` 把它升级成更接近真实神经元的东西——`SimpleNeuron`，一次跨向「神经网络」。

一个**人工神经元**做三件事：

1. **加权求和**：有多个输入 \(x_1, x_2, \dots\)，每个配一个权重 \(w_i\)，再加一个偏置 \(b\)：
   \[
   z = \sum_i w_i x_i + b
   \]
2. **激活函数**：把 \(z\) 压缩成一个有意义的输出。这里用经典的 **sigmoid** 函数，把任意实数压到 \((0,1)\) 区间，可以理解为「置信度」：
   \[
   \sigma(z) = \frac{1}{1+e^{-z}}
   \]
3. **反向更新**：用误差驱动权重调整。这里的关键是用到了 sigmoid 的导数 \(\sigma'(z)=\sigma(z)(1-\sigma(z))\) 来决定调整幅度。

这个脚本的任务也升级了：从「回归一个数字」变成**二分类**——判断二维平面上的点 \((x, y)\) 在直线 \(y=x\) 的上方还是下方。输出 `1` 表示「上方」，`0` 表示「下方」。sigmoid 把输出压到 0~1，正好可以解释成「属于上方的概率」。

> 术语对照：算预测 \(\sigma(w\cdot x+b)\) 叫**前向传播（forward propagation）**；用误差回头改权重叫**反向传播（backpropagation）**。这两个词会在后续整个神经网络单元反复出现。

#### 4.4.2 核心流程

```
# 前向传播（预测）
z = w₁x₁ + w₂x₂ + b
output = sigmoid(z)          # 一个 0~1 的「置信度」

# 反向传播（学习）
error = target - output                          # 真实标签 - 预测
delta = error · output · (1 - output)            # 用 sigmoid 导数
wᵢ += learning_rate · delta · xᵢ                 # 更新每个权重
b   += learning_rate · delta                     # 更新偏置

# 外层再套 epoch 循环，重复多轮
```

其中 `delta = error · sigmoid'(output)` 这一项是关键：它把「错多少」和「当前输出对调整有多敏感」结合起来，决定每个权重该挪多少。这正是简化版反向传播的核心。

#### 4.4.3 源码精读

sigmoid 把任意输入压到 0~1，作者贴心地用了「置信度」来类比，并对极端值做了溢出保护：

[examples/02-simple-neural-network.py:20-40](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/examples/02-simple-neural-network.py#L20-L40) —— `sigmoid(x)` 返回 `1/(1+exp(-x))`；当 `x>100` 直接返回 `1.0`、`x<-100` 返回 `0.0`，避免 `math.exp` 在极大/极小值时数值溢出。

它的导数是更新权重的关键，形式非常简洁：

[examples/02-simple-neural-network.py:43-54](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/examples/02-simple-neural-network.py#L43-L54) —— `sigmoid_derivative(x)` 返回 `x * (1 - x)`。注意这里传入的是 sigmoid 的**输出值**，利用了 \(\sigma'(z)=\sigma(z)(1-\sigma(z))\) 这一恒等式，省去了再算一次指数的麻烦。

`SimpleNeuron.__init__` 为每个输入配一个权重，再加一个偏置：

[examples/02-simple-neural-network.py:69-81](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/examples/02-simple-neural-network.py#L69-L81) —— 初始化：`weights` 是长度为 `num_inputs` 的随机列表（每个输入一个权重），`bias` 也是随机的，`output` 用来暂存最近一次预测，供反向更新时使用。

前向传播严格按「加权求和 → 加偏置 → 激活」三步走：

[examples/02-simple-neural-network.py:83-103](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/examples/02-simple-neural-network.py#L83-L103) —— `feedforward`：先 `sum(w*x)` 求加权和，`+= self.bias` 加偏置，再 `sigmoid(total)` 得到 0~1 的输出并暂存到 `self.output`。

反向传播把公式 `delta = error * sigmoid_derivative(output)` 逐字翻译，然后逐个权重更新：

[examples/02-simple-neural-network.py:105-128](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/examples/02-simple-neural-network.py#L105-L128) —— `train`：`error = target - self.output`，`delta = error * sigmoid_derivative(self.output)`，循环 `self.weights[i] += learning_rate * delta * inputs[i]`，最后 `self.bias += learning_rate * delta`。这就是反向传播的全部。

训练数据由 `generate_training_data` 自动生成：随机撒点，用「点是否在 y=x 上方」自动打标签：

[examples/02-simple-neural-network.py:131-154](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/examples/02-simple-neural-network.py#L131-L154) —— 生成训练数据：在 0~10 范围随机取点 `(x, y)`，若 `y > x` 标 `1`（上方），否则标 `0`（下方）。这相当于让模型去还原 `y = x` 这条分界线。

`main()` 把上面这些拼起来：生成 100 个样本、建一个 2 输入神经元、训 50 轮、再测 10 个新点并打印准确率：

[examples/02-simple-neural-network.py:210-226](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/examples/02-simple-neural-network.py#L210-L226) —— `main()` 里建 `SimpleNeuron(num_inputs=2)`，外层 50 个 epoch，内层对每条样本先 `feedforward` 再 `train`，累加 `total_error` 监控收敛。

#### 4.4.4 代码实践

**实践目标**：运行 `02-simple-neural-network.py`，亲眼看到「从零实现的神经元」能学会一个真实的二分类任务。

**操作步骤**：
1. 运行：
   ```bash
   python examples/02-simple-neural-network.py
   ```
2. 观察两段输出：
   - 训练阶段：每 10 轮打印一次 Average error，应当逐步下降。
   - 测试阶段：`visualize_decision` 打印一张表格，含每个测试点的 Prediction / Actual / Correct?，最后一行给出整体准确率 Accuracy。

**需要观察的现象**：训练后，测试表格里绝大多数点的 `Correct?` 列是 `✓`，准确率通常在 90% 以上（受随机数据影响会有波动）。

**预期结果**：你会看到一个真正「从零搭起来」的神经元学会了把点分到直线 `y=x` 两侧。这比 `01` 的线性拟合更接近真实神经网络，也为 u2 单元的感知机课程铺好了路。**待本地验证**你这次运行的具体准确率（由于训练数据随机，每次结果略有不同）。

> 进阶玩法（可选）：把 `main()` 第 199 行的 `num_samples` 调大、第 217 行的 `epochs` 调大，或第 225 行的 `learning_rate` 改大改小，观察准确率与收敛速度的变化，重复 4.2、4.3 模块里的调参直觉。

#### 4.4.5 小练习与答案

**练习 1**：为什么神经元的权重用 `random.uniform(-1, 1)`（可正可负），而 `01` 里的权重用 `random.uniform(0, 5)`（只取正数）？

**答案**：二分类任务里，分界线 `y = x` 的系数有正有负，神经元需要权重既能抑制某些输入（负权重）又能放大某些输入（正权重），所以初始化范围要覆盖正负。`01` 学的是 `y = 2x` 这种斜率为正的简单关系，初值取正数只是为了让它更快收敛到正解，并非必须。这说明**初始化范围要与任务匹配**。

**练习 2**：脚本里判断分类用的是 `predicted_class = 1 if prediction > 0.5 else 0`。为什么阈值是 `0.5`？

**答案**：因为 sigmoid 把输出压到了 0~1，可以解释成「属于类别 1 的概率」。`0.5` 是天然的中点：概率大于一半就判类别 1，否则判类别 0。这是一个最朴素的决策阈值。在样本不均衡时，这个阈值有时会调整（比如改用 0.3 或 0.7），但本例数据均衡，`0.5` 是最优。

---

## 5. 综合实践

现在把三个核心概念串起来，完成本讲的主线任务。

### 任务：让模型学会 y = 3x

**目标**：修改 `examples/01-hello-ai-world.py`，使其训练数据与学习率适配，让模型学出 `y = 3x`，并打印最终权重与测试误差。

**操作步骤**：

1. **改训练数据**（第 93–99 行）。把 5 条样本改成 `y = 3x` 的配对：
   ```python
   training_data = [
       (1, 3),    # y = 3x
       (2, 6),
       (3, 9),
       (4, 12),
       (5, 15),
   ]
   ```
2. **改测试集的「真实值」计算**（第 115 行）。原代码里 `actual = 2 * x`，必须同步改成 `actual = 3 * x`，否则测试阶段的「真实值」是错的，Difference 列会失真：
   ```python
   actual = 3 * x  # The true answer
   ```
3. **调整学习率**（第 30 行）。可在 `0.005 ~ 0.02` 之间尝试，比如保持 `0.01`，或改成 `0.02` 看是否收敛更快。
4. 运行：
   ```bash
   python examples/01-hello-ai-world.py
   ```

**需要观察的现象**：
- 训练日志里 Average error 逐轮下降，Final weight 收敛到约 `3.0`。
- 测试表（Input / Prediction / Actual / Difference）里，对 `6,7,10,15` 等新输入，Prediction 应接近 `18,21,30,45`，Difference 接近 0。

**预期结果**：模型成功学出权重约 `3.0`，在训练集之外的新输入上预测准确。这证明你理解了「训练数据决定学什么、学习率决定学多快、训练循环用误差驱动收敛」这三件事。

**反思题**（写在你的学习笔记里）：
- 如果你只改了训练数据，却**忘了**把第 115 行的 `2 * x` 改成 `3 * x`，会发生什么？（答案：权重照样能学到约 3.0，但测试表的 Actual 列仍是按 `2x` 算的，导致 Difference 看起来很大，给你「模型学得很差」的错觉。这提醒你：**评估指标的定义必须和任务一致**。）

---

## 6. 本讲小结

- **AI 的核心思想**：不是手写规则，而是让程序从带答案的训练数据里**自己学出参数（权重）**。`examples/` 两个脚本用纯 Python 把这件事讲透了。
- **训练数据与模式**：数据里稳定的关系就是「模式」，`(x, y)` 配对是监督学习的标准形态；用**没见过的新数据**测试才能验证模型是「学会」而非「背下」。
- **权重与学习率更新**：模型靠 \(\hat{y}=w\cdot x\) 预测，靠 \(w \leftarrow w + \eta\cdot \text{error}\cdot x\) 学习；学习率是「步长旋钮」，太大发散、太小太慢。
- **训练循环与误差**：外层 epoch 重复多轮、内层逐样本更新、用 `total_error` 监控收敛——这套骨架是几乎所有训练流程的模板。
- **从线性到神经元**：`02` 把单权重升级为「加权求和 + 偏置 + sigmoid 激活」的神经元，引入了前向/反向传播，完成了从「回归」到「二分类」的跨越，为后续感知机、神经网络课程打好基础。
- **可动手**：本讲所有代码无需深度学习框架，`python xxx.py` 即可运行，改一行数据或学习率就能看到不同结果。

---

## 7. 下一步学习建议

本讲你已经亲手把「学习 = 用误差驱动权重更新」跑通。接下来：

1. **进入正式课程的第一单元**：读 [lessons/1-Intro/README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/1-Intro/README.md)，对应讲义 **u2-l1「AI 简史与两种 AI 范式」**，从历史与范式的视角把本讲的「连接主义」直觉升级成系统认知。
2. **接着学符号 AI**：讲义 **u2-l2「符号 AI：知识表示与专家系统」** 会展示与本讲「连接主义」截然相反的另一条 AI 路线——用规则和本体推理，理解 AI 的两大范式。
3. **直奔感知机**：如果你已经对「权重更新」很感兴趣，可以跳到讲义 **u2-l3「感知机：最简单的神经元」**，本讲的 `SimpleNeuron` 正是感知机的雏形，那里会给出更完整的数学模型与决策边界可视化。
4. **推荐继续阅读的源码**：
   - [examples/README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/examples/README.md) 的 Learning Path，按顺序做完 `03-image-classifier.ipynb` 与 `04-text-sentiment.py`，体验「用预训练模型」与「文本情感分析」。
   - 把本讲的 `SimpleNeuron` 与即将学到的 `lessons/3-NeuralNetworks/03-Perceptron/Perceptron.ipynb` 对照阅读，你会发现自己已经理解了一大半。
