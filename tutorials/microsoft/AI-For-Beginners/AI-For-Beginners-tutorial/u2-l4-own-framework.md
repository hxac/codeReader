# 从零搭建自己的神经网络框架

> 对应课程：`lessons/3-NeuralNetworks/04-OwnFramework`
> 关键 Notebook：`OwnFramework.ipynb`、`lab/MyFW_MNIST.ipynb`

## 1. 本讲目标

上一讲（[u2-l3 感知机](u2-l3-perceptron.md)）我们得到了「最简单的神经元」：一个线性分类器 \(y=f(w^\top x+b)\)，它只能处理**线性可分**的二分类问题，权重更新规则是手推出来的 \(w \leftarrow w+\eta x t\)。

本讲要把这个单神经元升级成一个**可拼装的迷你深度学习框架**。读完本讲，你应当能够：

1. 用「**层（Layer）+ 计算图（Computational Graph）**」的视角理解神经网络，看懂课程如何用纯 NumPy 把 `Linear`、`Softmax`、`CrossEntropyLoss`、`Tanh` 拼成任意结构。
2. 说清**损失函数（Loss Function）**为什么这么设计、**优化器（Optimizer）**如何用梯度下降最小化损失，并把它和上一讲的感知机更新规则统一起来。
3. 手推并实现**反向传播（Backpropagation）**：误差如何沿着计算图从输出端倒流回每一个参数。
4. 用自己写的框架在二维数据上训练多类分类器，并完成 lab：在 **MNIST** 上训练 1/2/3 层感知机。

核心一句话：本讲把「训练一个神经网络」彻底拆成 **前向算损失 → 反向算梯度 → 更新参数** 三件事，并用一个不到 100 行的框架把它们实现出来。

## 2. 前置知识

本讲默认你已经掌握（若生疏可回看对应讲义）：

- **单个神经元的前向计算** \(y=f(w^\top x+b)\)（[u2-l3 感知机](u2-l3-perceptron.md)）。
- **从数据中学习权重**的最朴素循环：`ŷ = w·x`、`w ← w + η·error·x`（[u1-l4 第一个 AI 程序](u1-l4-first-ai-program.md)）。
- 基本的 **NumPy 数组运算**（矩阵乘法 `np.dot`、广播、`axis` 聚合）。
- 高数里的**链式求导法则**：若 \(L=L(p(z(W)))\)，则 \(\frac{\partial L}{\partial W}=\frac{\partial L}{\partial p}\frac{\partial p}{\partial z}\frac{\partial z}{\partial W}\)。

几个本讲会反复出现的术语，先给直觉：

| 术语 | 直觉解释 |
| --- | --- |
| 层（Layer） | 一个带参数的可计算模块，输入一个张量、输出一个张量 |
| 计算图 | 把若干层串成有向无环图，数据从左流到右 |
| 损失函数 | 把「预测得好不好」压成一个标量数字 |
| 梯度（Gradient） | 损失对某个参数的导数，指示「往哪调、调多少」 |
| 学习率 \(\eta\) | 每次更新的步长，太大发散、太慢收敛 |
| 批（minibatch） | 一次更新用到的一小撮样本，代替全量数据 |

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/04-OwnFramework/README.md) | 课程的文字讲义：机器学习形式化、梯度下降、多层感知机与反向传播的数学 |
| [OwnFramework.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/04-OwnFramework/OwnFramework.ipynb) | 核心：逐步用 NumPy 搭出框架（`Linear`/`Softmax`/`CrossEntropyLoss`/`Tanh`/`Net`）并训练 |
| [lab/README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/04-OwnFramework/lab/README.md) | 实验任务说明：用本讲框架解 MNIST |
| [lab/MyFW_MNIST.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/04-OwnFramework/lab/MyFW_MNIST.ipynb) | 实验起始 Notebook：下载 MNIST、切分训练/测试集，留空让你填入框架 |

> 说明：本讲给出的 Notebook 行号是 `.ipynb` 源文件（JSON）中的行号，点击永久链接会跳到 GitHub 上对应位置。

## 4. 核心概念与源码讲解

本讲的三个最小模块互为因果：**计算图**决定网络长什么样 → **损失函数与优化器**决定怎么评估和调整 → **反向传播**决定梯度怎么高效算出来。

### 4.1 计算图：用「层」拼出神经网络

#### 4.1.1 概念说明

上一讲的感知机只有一个神经元，公式写死成 \(f(x)=w^\top x+b\)。真实网络是**很多函数的复合**，比如两层网络：

\[
z_1 = W_1 x + b_1,\quad z_2 = W_2 \alpha(z_1) + b_2,\quad f = \sigma(z_2)
\]

其中 \(\alpha\) 是非线性激活（如 `tanh`），\(\sigma\) 是 softmax。

如果每写一种结构都要重写一遍公式和求导，代码会爆炸。课程的解法是**把每个函数封装成一个「层」对象**，每个层只负责两件事：

- `forward(x)`：把输入变换成输出；
- 持有自己的**参数**（如 `W`、`b`）。

把这些层像积木一样首尾相接，数据从第一层流向最后一层，就构成一张**计算图（Computational Graph）**。这种「层 = 可复用模块」的设计，正是 PyTorch `nn.Module`、Keras `Layer` 的同一思想——本课程先用 60 行 NumPy 把它讲透。

> Notebook 原话：「若干层的组合可以表示成一张**计算图**」——[OwnFramework.ipynb 的 Computational Graph 小节](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/04-OwnFramework/OwnFramework.ipynb)。

#### 4.1.2 核心流程

一次「前向传播」就是让一个批量样本 \(x\) 依次穿过每一层：

```text
x ──► [Linear: z = x·Wᵀ + b] ──► [Tanh: a = tanh(z)] ──► [Linear] ──► [Softmax: p] ──► 概率
```

对应到代码就是 `Net.forward` 里一个正序循环。要换结构，只需 `net.add(...)` 多塞几层，循环逻辑完全不变——这就是计算图带来的解耦。

#### 4.1.3 源码精读

**① 最基础的线性层（先只实现前向）。** 它把感知机的单神经元推广成「一层神经元」：输入 `nin` 维、输出 `nout` 维，权重 `W` 形状是 `(nout, nin)`。

[OwnFramework.ipynb:L410-L416](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/04-OwnFramework/OwnFramework.ipynb#L410-L416) —— `Linear` 的前向版本：权重用 \(\mathcal{N}(0, 1/\sqrt{n_{in}})\) 初始化（类似 Xavier 缩放，让方差与输入维度匹配），偏置初始化为 0；`forward` 做一次矩阵乘法 \(z=xW^\top+b\)。

```python
class Linear:
    def __init__(self,nin,nout):
        self.W = np.random.normal(0, 1.0/np.sqrt(nin), (nout, nin))
        self.b = np.zeros((1,nout))
    def forward(self, x):
        return np.dot(x, self.W.T) + self.b
```

> 对比上一讲：感知机是 `nout=1` 的退化；这里 `nout` 可以是 2（二分类）、10（MNIST 的 10 个数字），一次就把「单神经元」变成了「一层神经元」。

**② Softmax：把任意打分变成概率。** 线性层输出的是未归一化的「打分」（logits），softmax 把它压成一组和为 1 的概率。

[OwnFramework.ipynb:L469-L474](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/04-OwnFramework/OwnFramework.ipynb#L469-L474) —— 注意它先减去 `zmax` 再取 `exp`，这是经典的**数值稳定技巧**：\(\sigma(z)_c=\frac{e^{z_c-z_{\max}}}{\sum_j e^{z_j-z_{\max}}}\)，数学上等价但避免 `exp` 溢出。

```python
class Softmax:
    def forward(self,z):
        zmax = z.max(axis=1,keepdims=True)
        expz = np.exp(z-zmax)
        Z = expz.sum(axis=1,keepdims=True)
        return expz / Z
```

**③ 把层串成网络的 `Net` 类。** 它只做三件事：正序前向、反序反向、逐层更新。

[OwnFramework.ipynb:L896-L916](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/04-OwnFramework/OwnFramework.ipynb#L896-L916) —— `forward` 让数据依次穿过每层；`backward` 用 `self.layers[::-1]` **倒序**遍历（反向传播的关键，下一节详解）；`update` 只对「拥有 `update` 方法」的层（即有参数的 `Linear`）执行更新，对无参数的 `Softmax`/`Tanh` 自动跳过。

```python
class Net:
    def __init__(self):
        self.layers = []
    def add(self,l):
        self.layers.append(l)
    def forward(self,x):
        for l in self.layers:
            x = l.forward(x)
        return x
    def backward(self,z):
        for l in self.layers[::-1]:   # 倒序：从损失端往输入端
            z = l.backward(z)
        return z
    def update(self,lr):
        for l in self.layers:
            if 'update' in l.__dir__():
                l.update(lr)
```

有了 `Net`，定义一个二分类网络就只要四行，结构一目了然：

```python
net = Net()
net.add(Linear(2,2))
net.add(Softmax())
```

#### 4.1.4 代码实践

**目标**：亲手验证「层 = 积木」和 softmax 的归一化性质。

1. 在 `ai4beg` 环境启动 Jupyter，打开 `OwnFramework.ipynb`，从第一个 cell 顺序运行到 `Softmax` 定义处。
2. 新建一个 cell，运行下面这段（示例代码，非 Notebook 原有）：

```python
# 示例代码：验证计算图的前向与 softmax 归一化
net = Linear(2,2)
sm = Softmax()
p = sm.forward(net.forward(train_x[0:5]))
print(p)                      # 5×2 的概率矩阵
print(p.sum(axis=1))          # 每一行应等于 1.0
```

3. **观察现象**：`p.sum(axis=1)` 的每一项都应非常接近 `1.0`，说明 softmax 确实把打分归一成了概率分布。
4. **预期结果**：得到一个形状 `(5,2)` 的数组，每行求和为 1。
5. 若报 `NameError`，多半是内核没选 `ai4beg` 或前面的 cell 没运行——回看 [u1-l3 环境搭建](u1-l3-environment-setup.md)。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `Linear(2,2)` 改成 `Linear(2,10)`，`forward` 输出的形状会变成什么？为什么？

> **答案**：变成 `(N, 10)`。因为 `W` 形状是 `(nout, nin)=(10,2)`，`x·Wᵀ` 把 `nin=2` 维输入映射到 `nout=10` 维输出。这正是把网络从「两类」扩到「十类」（如 MNIST）的方式。

**练习 2**：`Net.update` 里为什么要判断 `if 'update' in l.__dir__()`？

> **答案**：因为 `Softmax` 和 `Tanh` 没有可训练参数，自然没有 `update` 方法；只有 `Linear` 有参数需要更新。这个判断让 `Net` 可以无差别地遍历所有层，只对有参数的层做更新。

---

### 4.2 损失函数与优化器：把「学得好不好」变成可最小化的数字

#### 4.2.1 概念说明

计算图只告诉我们「怎么算预测」，但训练需要一个**优化目标**。课程给出机器学习的一般形式（[README.md:L17-L28](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/04-OwnFramework/README.md#L17-L28)）：

\[
\theta = \arg\min_\theta \mathcal{L}(f_\theta(X), Y)
\]

即：在参数 \(\theta=\langle W,b\rangle\) 中，找一组让**损失函数 \(\mathcal{L}\)** 最小的值。损失函数把「整个数据集上预测得多差」压成一个标量。

常见损失（[README.md:L17-L24](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/04-OwnFramework/README.md#L17-L24)）：

- **回归**：绝对误差 \(\sum_i |f(x_i)-y_i|\) 或平方误差 \(\sum_i (f(x_i)-y_i)^2\)。
- **分类**：0-1 损失（就是准确率，但不可导）、**交叉熵（cross-entropy）** —— 本框架的默认选择。

> 为什么不用准确率？因为它「不连续」：预测概率从 0.49 跨到 0.51，准确率突然跳变，没法求导也就没法做梯度下降。交叉熵 \(-\log p_c\)（\(c\) 是真实类别，\(p_c\) 是网络给出的概率）处处可导，而且**对「自信地猜错」惩罚极大**（\(p_c\to 0\) 时 \(-\log p_c\to\infty\)），这正是我们想要的训练信号。

优化器方面，本框架用的是最朴素的**梯度下降（SGD）**：

\[
W^{(i+1)} = W^{(i)} - \eta\,\frac{\partial \mathcal{L}}{\partial W},\qquad b^{(i+1)} = b^{(i)} - \eta\,\frac{\partial \mathcal{L}}{\partial b}
\]

注意符号是**减号**：沿梯度的**反方向**走，损失才会下降。这一点把前面两讲统一起来了：

- [u1-l4](u1-l4-first-ai-program.md) 的 `w ← w + η·error·x` 用加号，是因为那里的 `error` 已被定义成「目标−预测」，本身带着让 `w` 增大/减小的方向；
- 这里用通用的梯度下降语言，把 `error` 换成了真正的导数 \(\partial\mathcal{L}/\partial W\)，于是统一成「减去学习率×梯度」。

#### 4.2.2 核心流程

一个完整的训练步（对一个小批量 minibatch）：

```text
1. 前向：x → … → p（概率）
2. 算损失：L = CrossEntropy(p, y)
3. 反向：从 L 倒推每个参数的梯度 dW、db      ← 4.3 节详解
4. 更新：W -= η·dW； b -= η·db
```

把「全量数据」切成很多小批（minibatch），每批算一次梯度就更新一次，遍历完一遍数据叫**一个 epoch**。因为每批是随机取的，这套方法叫**随机梯度下降（SGD）**——[README.md:L30-L39](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/04-OwnFramework/README.md#L30-L39)。

#### 4.2.3 源码精读

**① 交叉熵损失层（前向）。** 它被实现成一个「层」，`forward` 接收两个输入：网络输出概率 `p` 和真实标签 `y`。

[OwnFramework.ipynb:L598-L604](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/04-OwnFramework/OwnFramework.ipynb#L598-L604) —— 用 fancy indexing `p[np.arange(len(y)), y]` 取出「真实类别对应的概率」\(p_c\)，取对数取负，再对整个 minibatch `.mean()` 得到一个标量损失。

```python
class CrossEntropyLoss:
    def forward(self,p,y):
        self.p = p
        self.y = y
        p_of_y = p[np.arange(len(y)), y]
        log_prob = np.log(p_of_y)
        return -log_prob.mean()   # 对整个 minibatch 取平均，得到一个数
```

> 关键点：损失必须返回**一个标量**（代表整个 minibatch 的总误差），所以末尾要 `.mean()`。这一点会在反向传播里决定梯度的尺度。

**② 计算图上的一次完整前向（得到损失）。** 这三行就是「数据穿过计算图、最后算出损失」的最简写法：

[OwnFramework.ipynb:L642-L644](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/04-OwnFramework/OwnFramework.ipynb#L642-L644) —— `z = net.forward(...)`（线性打分）→ `p = softmax.forward(z)`（概率）→ `loss = cross_ent_loss.forward(p, y)`（标量损失）。

**③ 参数更新 `Linear.update`。** 这就是梯度下降公式 \(W\leftarrow W-\eta\,\frac{\partial\mathcal{L}}{\partial W}\) 的直译，`dW`/`db` 由下一节的 `backward` 算好并存起来：

```python
def update(self,lr):
    self.W -= lr*self.dW
    self.b -= lr*self.db
```

#### 4.2.4 代码实践

**目标**：体会「学习率 \(\eta\) 是步长旋钮」以及损失随训练下降。

1. 打开 `OwnFramework.ipynb`，运行到「Training the Model」一节（含手写训练循环的 cell）。
2. 把 `learning_rate = 0.1` 分别改成 `0.01`、`0.1`、`1.0`，各跑一遍同一个 cell，记录 `Final accuracy`。
3. **观察现象**：
   - `0.01`：步长太小，一个 epoch 后准确率提升有限；
   - `0.1`：稳定上升到约 0.8；
   - `1.0`：步长过大，可能震荡甚至变差（发散）。
4. **预期结果**：存在一个「刚好」的学习率区间；过大过小都训不好。这与 [u1-l4](u1-l4-first-ai-program.md) 里调 `learning_rate` 观察到的现象一致。
5. 「待本地验证」：具体准确率数值取决于随机种子，重点看**趋势**而非绝对值。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `CrossEntropyLoss.forward` 末尾要 `.mean()`？如果改成 `.sum()` 会怎样？

> **答案**：损失要代表「整个 minibatch 的总误差」并作为一个标量被求导。`.mean()` 让损失的尺度与 batch 大小无关，从而学习率不必随 batch 变化；若改成 `.sum()`，梯度会随 batch 线性放大，大 batch 时需要等比例调小学习率，否则容易发散。

**练习 2**：网络对真实类别给出概率 \(p_c=0.01\)，交叉熵是多少？\(p_c=0.99\) 呢？

> **答案**：\(-\log 0.01 \approx 4.61\)（大错，重罚）；\(-\log 0.99 \approx 0.01\)（几乎全对，接近 0）。可见交叉熵对「自信地错」惩罚极重。

---

### 4.3 反向传播：让误差沿着计算图倒流，自动算出每个参数该怎么调

#### 4.3.1 概念说明

有了损失，还得知道**每个参数该往哪调**——也就是 \(\partial\mathcal{L}/\partial W\)。对一层网络，手算还能凑合；对多层网络，靠的就是**链式法则**。课程在 [README.md:L41-L57](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/04-OwnFramework/README.md#L41-L57) 给出两层网络的关键观察：

\[
\frac{\partial\mathcal{L}}{\partial W_2} = \underbrace{\frac{\partial\mathcal{L}}{\partial\sigma}\frac{\partial\sigma}{\partial z_2}}_{\text{最左端，对所有层都一样}}\frac{\partial z_2}{\partial W_2},\qquad
\frac{\partial\mathcal{L}}{\partial W_1} = \underbrace{\frac{\partial\mathcal{L}}{\partial\sigma}\frac{\partial\sigma}{\partial z_2}}_{\text{同一份}}\frac{\partial z_2}{\partial\alpha}\frac{\partial\alpha}{\partial z_1}\frac{\partial z_1}{\partial W_1}
\]

注意每个式子的**最左端是相同的**。这意味着：可以从损失端出发，沿着计算图**倒着**一层层把误差传回去，每层只需做一次局部乘法——这便是**反向传播（backpropagation）**。它本质上就是把链式法则工程化：每个层实现一个 `backward(dz)`，输入「上一层传回来的误差」、输出「传给下一层（更靠近输入端）的误差」，顺手算好自己的参数梯度。

#### 4.3.2 核心流程

一次训练 = 一次前向 + 一次反向 + 一次更新：

```text
前向（正序）：  x → Linear → Softmax → p → CrossEntropy → L
反向（倒序）：  L ← dL/dp ← dL/dz ← dL/dW, dL/db
更新：          W -= η·dW ; b -= η·db
```

对应 [OwnFramework.ipynb:L852-L866](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/04-OwnFramework/OwnFramework.ipynb#L852-L866) 的手写训练循环：先 `forward` 算 `loss`，再依次 `loss.backward → softmax.backward → lin.backward`，最后 `lin.update(lr)`。

#### 4.3.3 源码精读

**① `Linear` 的完整版（前向 + 反向 + 更新）。** 这是反向传播最核心的一块。对线性层 \(z=xW^\top+b\)，可推出（[OwnFramework.ipynb 的反向实现说明](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/04-OwnFramework/OwnFramework.ipynb)）：

\[
\Delta x = \Delta z \cdot W,\qquad \Delta W = \Delta z^\top \cdot x,\qquad \Delta b = \sum \Delta z
\]

[OwnFramework.ipynb:L756-L777](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/04-OwnFramework/OwnFramework.ipynb#L756-L777) —— 注意 `forward` 里多了 `self.x = x`，把输入**缓存**下来给反向用；`backward(dz)` 同时算出传给上一层的 `dx`、以及本层参数梯度 `dW`/`db` 并保存；`update(lr)` 用它们做梯度下降。

```python
class Linear:
    def __init__(self,nin,nout):
        self.W = np.random.normal(0, 1.0/np.sqrt(nin), (nout, nin))
        self.b = np.zeros((1,nout))
        self.dW = np.zeros_like(self.W)
        self.db = np.zeros_like(self.b)
    def forward(self, x):
        self.x = x                       # 缓存输入，供反向使用
        return np.dot(x, self.W.T) + self.b
    def backward(self, dz):
        dx = np.dot(dz, self.W)          # 传给上一层（更靠近输入）的误差
        dW = np.dot(dz.T, self.x)        # 本层权重的梯度
        db = dz.sum(axis=0)              # 本层偏置的梯度
        self.dW = dW
        self.db = db
        return dx
    def update(self,lr):
        self.W -= lr*self.dW
        self.b -= lr*self.db
```

> 「缓存输入」是反向传播的通用套路：前向时把反向需要的中间量（这里是 `x`）存成 `self.x`，反向时直接取用。PyTorch 的 `ctx.save_for_backward` 是同一思想。

**② Softmax 与 CrossEntropy 的反向。** 二者的 `backward` 合在一起，正好实现了「交叉熵 + softmax」这条最常用输出链的梯度。

[OwnFramework.ipynb:L793-L815](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/04-OwnFramework/OwnFramework.ipynb#L793-L815) —— `CrossEntropyLoss.backward` 给出 \(\partial(-\log p)/\partial p\) 形式的误差（在真实类别位置为 \(-1/(N\cdot p)\)），`Softmax.backward` 再用 softmax 的雅可比把它转成对打分 `z` 的误差 \(p\odot dp - p\odot\sum(p\odot dp)\)。

```python
class Softmax:
    def forward(self,z):
        self.z = z
        zmax = z.max(axis=1,keepdims=True)
        expz = np.exp(z-zmax)
        Z = expz.sum(axis=1,keepdims=True)
        return expz / Z
    def backward(self,dp):
        p = self.forward(self.z)
        pdp = p * dp
        return pdp - p * pdp.sum(axis=1, keepdims=True)

class CrossEntropyLoss:
    def forward(self,p,y):
        self.p = p; self.y = y
        p_of_y = p[np.arange(len(y)), y]
        return -np.log(p_of_y).mean()
    def backward(self,loss):
        dlog_softmax = np.zeros_like(self.p)
        dlog_softmax[np.arange(len(self.y)), self.y] -= 1.0/len(self.y)
        return dlog_softmax / self.p
```

**③ 为什么多层之间必须夹非线性：`Tanh`。** 线性层的复合仍是线性（\(W_2(W_1 x)=W'x\)），叠再多层也等价于一层。所以层与层之间要插入非线性激活 `Tanh`，它同样遵循「前向缓存、反向用导数」的模式，导数为 \(1-\tanh^2\)：

[OwnFramework.ipynb:L1159-L1165](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/04-OwnFramework/OwnFramework.ipynb#L1159-L1165) —— `forward` 缓存输出 `self.y`，`backward` 返回 \((1-y^2)\,dy\)，把误差继续往更靠近输入的层传。

```python
class Tanh:
    def forward(self,x):
        y = np.tanh(x)
        self.y = y
        return y
    def backward(self,dy):
        return (1.0-self.y**2)*dy
```

有了 `Tanh`，就能搭出**两层网络**（[OwnFramework.ipynb:L1203-L1208](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/04-OwnFramework/OwnFramework.ipynb#L1203-L1208)），它有能力拟合**非线性可分**的数据：

```python
net = Net()
net.add(Linear(2,10))
net.add(Tanh())
net.add(Linear(10,2))
net.add(Softmax())
```

由于 `Net.backward` 是倒序遍历、`update` 只更新有参数的层，这条任意深度的链路不需要改任何框架代码就能训练——这正是反向传播 + 计算图的威力。

#### 4.3.4 代码实践

**目标**：亲眼看到「反向传播在算梯度」，并验证非线性层的必要性。

1. 在 `Linear.backward` 里临时加一行打印（示例代码）：

```python
# 示例代码：观察反向传播产生的梯度量级
def backward(self, dz):
    dx = np.dot(dz, self.W)
    self.dW = np.dot(dz.T, self.x)
    print("||dW|| =", np.abs(self.dW).max())   # 观察权重梯度的最大绝对值
    return dx
```

2. 运行「Training the Model」cell，**观察现象**：每个 minibatch 都打印一行 `||dW||`，数值随训练逐渐变小——说明损失曲面越来越平、参数越来越接近极小值。
3. **去掉非线性的对照实验**：把上面两层网络里的 `net.add(Tanh())` 删掉，变成 `Linear(2,10) → Linear(10,2)`，再训练。
   - **预期结果**：尽管有两层 `Linear`，决策边界仍是**直线**（因为线性复合=线性），准确率不会比一层好。这验证了「层间必须有非线性」。
4. 训练更深的网络（3 层）时，如果出现准确率上不去甚至 NaN，留意梯度是否过大——这正是 lab 里「层数增加时是否遇到训练困难」这一问题指向的过拟合/数值问题。
5. 「待本地验证」：打印出的具体数值依种子而异。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Linear.forward` 要把输入存到 `self.x`，而 `Softmax.forward` 存的是 `self.z`？

> **答案**：因为反向时各自需要不同的中间量。`Linear.backward` 算 `dW = dzᵀ·x` 需要前向的**输入** `x`；`Softmax.backward` 重算概率 `p` 需要 softmax 之前的**输入打分** `z`。缓存哪个量，取决于该层反向公式里要用到什么——这是所有框架「前向存中间量」的通用原则。

**练习 2**：去掉两层 `Linear` 之间的 `Tanh` 后，网络表达能力会退化成什么？

> **答案**：退化成一层线性分类器。因为 \(W_2(W_1 x+b_1)+b_2 = (W_2 W_1)x + (W_2 b_1+b_2)=W'x+b'\)，仍是线性函数，决策边界仍是超平面，无法解决 XOR 等非线性可分问题。

**练习 3**：`Net.backward` 为什么要倒序遍历 `self.layers[::-1]`？

> **答案**：因为误差从损失端（网络末端）出发，逐层传向输入端。前向是「输入→输出」的正序，反向自然是「输出→输入」的倒序；只有倒序，每层才能拿到「更靠近输出」的层传回来的误差。

## 5. 综合实践

把三个模块串起来，完成课程官方 lab：**用自己的框架在 MNIST 上做手写数字分类**。

任务来源：[lab/README.md:L5-L7](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/04-OwnFramework/lab/README.md#L5-L7)（用本讲框架解 MNIST）与起始 Notebook [lab/MyFW_MNIST.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/04-OwnFramework/lab/MyFW_MNIST.ipynb)。

**步骤**：

1. 打开 `lab/MyFW_MNIST.ipynb`，依次运行前几个 cell：它会从仓库下载 `data/mnist.pkl.gz`、解压成 `mnist.pkl`，并读出 `MNIST['Train']['Features']`（形状 `(42000, 784)`，即 42000 张 28×28 拉平的灰度图）和 `Labels`，再用 scikit-learn 切成训练/测试集。
2. 把 `OwnFramework.ipynb` 里的框架代码（`Linear`/`Softmax`/`CrossEntropyLoss`/`Tanh`/`Net`/`train_epoch`/`get_loss_acc`）**复制到这个 Notebook 顶部**（或存成单独的 `.py` 模块再 `import`，更清爽）。
3. 关键改造：MNIST 输入是 784 维、10 类，所以：
   - 一层感知机用 `Linear(784, 10)` + `Softmax`；
   - 两层用 `Linear(784, 128) → Tanh → Linear(128, 10) → Softmax`；
   - 三层再多加一个 `Linear/Tanh`。
4. 分别训练 1/2/3 层网络，**记录并报告测试集准确率**。
5. 回答 lab 给出的思考题（[lab/README.md:L14-L20](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/04-OwnFramework/lab/README.md#L14-L20)）：
   - 中间层激活函数（如把 `Tanh` 换成 `ReLU`/`Sigmoid`）是否影响性能？
   - 这个任务到底需要 2 层还是 3 层？
   - 层数增加时是否遇到训练困难（梯度消失/发散）？
   - 画出「权重最大绝对值 vs epoch」曲线，观察权重在训练中的行为。

**交付物**：三个网络的测试准确率（一层通常约 0.92，两层可到 0.97 左右，依实现和超参而异——「待本地验证」），以及一张训练/验证准确率曲线图。

> 提示：MNIST 像素值在 0~255，建议先归一化到 0~1 再喂给网络；学习率从 `0.01`、`batch_size=64` 起步较稳。

## 6. 本讲小结

- **神经网络 = 计算图**：每个「层」是一个带参数、有 `forward` 的对象；`Net` 用正序循环把它们串起来，结构可任意拼装。
- **损失函数**把「预测好坏」压成标量：分类用**交叉熵** \(-\log p_c\)，处处可导且重罚「自信地错」；**优化器**用梯度下降 \(W\leftarrow W-\eta\,\partial\mathcal{L}/\partial W\)，这与前两讲的权重更新本质同源。
- **反向传播**是链式法则的工程化：损失端的误差沿计算图**倒序**逐层传递，每层 `backward(dz)` 同时算出「传给上一层的误差」和「自己的参数梯度」；前向缓存中间量是反向的前提。
- **层间必须夹非线性**（`Tanh` 等），否则多层线性等价于一层；本框架的 `Net.backward` 倒序遍历、`update` 自动跳过无参数层，使任意深度网络无需改代码即可训练。
- 一个训练步永远是三件事：**前向算损失 → 反向算梯度 → 按学习率更新参数**；遍历一遍数据为一个 epoch，小批量随机更新即 SGD。
- 自己手写的这套框架，正是 PyTorch/Keras 的最小内核——下一讲换成工业级框架后，思想完全一致。

## 7. 下一步学习建议

- **横向对照工业框架**：下一讲 [u2-l5 框架与过拟合](u2-l5-frameworks-overfitting.md) 会把同样的训练流程用 PyTorch/Keras 重写，你会看到 `nn.Module`、`loss.backward()`、`optimizer.step()` 如何对应本讲的 `Net`/`backward`/`update`，并正式引入「过拟合」概念。
- **深化反向传播**：通读 [README 的 Review & Self Study](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/04-OwnFramework/README.md#L77-L79) 推荐的维基百科条目，自己手推一次两层网络的 \(\partial\mathcal{L}/\partial W_1\)。
- **续读源码**：重看 `OwnFramework.ipynb` 的 `train_and_plot`（[L994-L1020](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/04-OwnFramework/OwnFramework.ipynb#L994-L1020)），理解如何把训练过程可视化成决策边界与准确率曲线——这是后续计算机视觉单元调试模型的常用手段。
