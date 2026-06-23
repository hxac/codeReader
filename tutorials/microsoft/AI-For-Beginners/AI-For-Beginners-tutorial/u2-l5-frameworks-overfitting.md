# 引入 PyTorch/Keras 框架与过拟合

## 1. 本讲目标

学完本讲，你应当能够：

- 说出**为什么要从上一讲自研的 NumPy 框架切换到工业级框架**，并区分「低层 API（张量 + 计算图）」与「高层 API（层序列 + `fit`）」两种使用方式。
- 用 **PyTorch** 写出一个完整的「前向 → 损失 → 反向 → 更新」训练循环，用 **Keras** 写出等价的 `compile` + `fit` 三行训练代码，并把两者与上一讲的迷你框架一一对应。
- 理解**过拟合（overfitting）**的成因、表现与判据，看懂训练曲线里「训练误差下降、验证误差反弹」的拐点。
- 掌握应对过拟合的三类手段——**增加数据、降低模型复杂度、正则化（如 Dropout）**，并知道本课程把 Dropout 的细节放在了哪一课。

本讲是「符号 AI 与神经网络基础」单元的收尾，承上（自研框架）启下（计算机视觉单元会用同样的训练循环跑 CNN）。

## 2. 前置知识

本讲默认你已经学完 **u2-l4（从零搭建自己的神经网络框架）**。需要回忆的几个概念：

- **计算图**：把网络拆成一个个「层」，每层有 `forward` 和 `backward`，首尾相接。
- **损失函数（loss）**：衡量预测与真值的差距；分类常用交叉熵 \(-\log p_c\)。
- **优化器（optimizer）**：沿梯度反方向更新参数，\(W \leftarrow W - \eta\,\partial \mathcal{L}/\partial W\)。
- **反向传播（backpropagation）**：误差从损失端沿计算图倒序逐层回传，靠链式法则算出每层参数的梯度。
- **epoch**：把训练数据完整过一遍叫一个 epoch。

上一讲我们**亲手用纯 NumPy 实现了上面这一切**——`Linear`、`Softmax`、`Tanh` 层、`Net` 容器、手写的 `backward`。本讲要回答的问题是：**既然原理已经懂了，为什么真实工程里大家不这么写，而是用 PyTorch / Keras？** 答案藏在两个词里：**张量（tensor）** 与 **自动求导（autograd）**。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [lessons/3-NeuralNetworks/05-Frameworks/README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/05-Frameworks/README.md) | 本课主讲义：先讲两大框架的低层/高层 API 取舍，再讲过拟合。 |
| [lessons/3-NeuralNetworks/05-Frameworks/IntroPyTorch.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/05-Frameworks/IntroPyTorch.ipynb) | PyTorch 入门：从张量、自动求导，到 `nn.Module` 定义网络、手写训练循环，再到 PyTorch Lightning 高层 API。 |
| [lessons/3-NeuralNetworks/05-Frameworks/IntroKeras.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/05-Frameworks/IntroKeras.ipynb) | Keras 入门：用 `Sequential` 把层串成序列，`compile` 配置损失与优化器，`fit` 一键训练，并给出「任务类型 → 激活函数 → 损失函数」对照表。 |
| [lessons/3-NeuralNetworks/05-Frameworks/lab/README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/05-Frameworks/lab/README.md) | 综合实践说明：用单层 / 多层全连接网络做 Iris 与 MNIST 分类。 |

> 提示：README 明确说本课程**两个框架并行**，你只需挑一个深入。下面为了对照，两边都讲。

---

## 4. 核心概念与源码讲解

### 4.1 框架训练循环

#### 4.1.1 概念说明

上一讲的自研框架有一个致命短板：**每个层的 `backward` 都得手写导数**。一旦网络变深、出现卷积、注意力等新运算，手写导数既繁琐又容易出错。工业级框架解决的就是这个问题，它额外提供两件自研框架没有的东西：

1. **张量（Tensor）**：多维数组（标量是 0 维、向量 1 维、矩阵 2 维……），但能在 **GPU/TPU** 上做并行运算。深度学习计算量极大，把矩阵乘法分布到上千个 GPU 核心上是训练可行的前提。
2. **自动求导（autograd）**：只要前向计算用的是框架内置函数，框架就**自动**帮你建好计算图、算出任意表达式的梯度——你再也不用写 `backward`。

README 把主流框架分成两层 API，理解这张表是本节的关键：

- **低层 API（TensorFlow / PyTorch）**：让你手动搭建「计算图」，对训练过程控制力强，研究新架构时常用。
- **高层 API（Keras / PyTorch Lightning）**：把网络看成「一串层」，构造典型网络极快，训练通常就是调一个 `fit` 函数。

两层 API **可以混用**：用低层 API 写一个自定义层，再放进高层 API 搭的大网络里。

#### 4.1.2 核心流程

**PyTorch 的训练循环（低层 API，手写）**，本质和上一讲自研框架完全同构，只是把「手算导数」换成「框架算导数」：

```
定义网络 (nn.Module)        ← 对应自研框架的 Net
选择损失函数 + 优化器         ← 对应 loss + SGD
for epoch in range(N):       ← 外层遍历数据
    for x, y in dataloader:  ← 内层按批次(minibatch)取数据
        optim.zero_grad()    ← 清空上一步残留梯度（PyTorch 梯度会累加）
        out  = net(x)        ← 前向传播
        loss = criterion(out, y)
        loss.backward()      ← 反向传播：框架自动算所有参数梯度
        optim.step()         ← 优化器按梯度更新参数
```

四个新名词：

- **minibatch（小批量）**：每次用一小撮样本（而不是全部、也不是单个）算梯度，兼顾速度与稳定性。
- **`zero_grad()`**：PyTorch 的梯度默认**累加**而非覆盖，所以每步开头必须清零，否则梯度会越积越大。
- **`loss.backward()`**：这一句取代了上一讲手写的整套 `backward` 链——这就是自动求导的威力。
- **`optim.step()`**：优化器（如 SGD、Adam）根据 `backward` 算出的梯度更新所有参数。

**Keras 的训练（高层 API）**，把上面整个循环压缩成三步：

```
model = Sequential([Dense(...), Dense(...)])   # 1. 把层串成序列
model.compile(optimizer=..., loss=..., metrics=['acc'])  # 2. 配置损失与优化器
model.fit(x, y, validation_data=..., epochs=..., batch_size=...)  # 3. 一键训练
```

`fit` 内部跑的就是 PyTorch 那套循环，只是被框架封装好了。

> 对应关系一句话：**自研框架的 `Net`/`backward`/手写 SGD ≡ PyTorch 的 `nn.Module`/`loss.backward()`/`optim.step()` ≡ Keras 的 `Sequential`/`fit`。** 抽象层次不同，骨架相同。

#### 4.1.3 源码精读

**(1) 框架的两层 API —— README 的对照表**

README 用一张表点明了「低层 / 高层」之分，并强调高层 API 把网络看作层的序列、用 `fit` 训练：

[lessons/3-NeuralNetworks/05-Frameworks/README.md:16-28](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/05-Frameworks/README.md#L16-L28) —— 说明 TensorFlow/PyTorch 是低层 API，Keras/PyTorch Lightning 是高层 API；低层搭计算图、高层用 `fit`，二者可混用。

**(2) 自动求导长什么样 —— PyTorch 的 `backward()`**

要体会 autograd 的简洁，看这段「对任意表达式求梯度」的代码：只要设了 `requires_grad=True`，调用 `backward()` 后梯度就出现在 `.grad` 里。

[lessons/3-NeuralNetworks/05-Frameworks/IntroPyTorch.ipynb:344-350](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/05-Frameworks/IntroPyTorch.ipynb#L344-L350) —— 设置 `requires_grad=True`，对 `c = mean(sqrt(a² + b²))` 调用 `c.backward()`，`a.grad` 自动得到结果；这正是取代上一讲手写 `backward` 的机制。

> 该 Notebook 还在前面演示了「梯度会累加，所以要用 `zero_()` 清零」的细节，这与训练循环里的 `optim.zero_grad()` 是同一个道理。

**(3) 用 `nn.Module` 定义网络 —— PyTorch 版的 `Net`**

`MyNet` 继承 `torch.nn.Module`，在 `__init__` 里声明两个线性层 `fc1`/`fc2`（`Linear` 就是我们自研框架里的 `Linear` 层），在 `forward` 里描述数据怎么流过它们。**全程没有一行导数代码**——梯度由框架自动算。

[lessons/3-NeuralNetworks/05-Frameworks/IntroPyTorch.ipynb:1640-1654](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/05-Frameworks/IntroPyTorch.ipynb#L1640-L1654) —— `class MyNet(nn.Module)`：`fc1 = Linear(2, hidden_size)` → 激活 `func` → `fc2 = Linear(hidden_size, 1)`，结构就是上一讲的「两层全连接夹非线性」。

**(4) 标准训练循环 —— PyTorch 的 `train()`**

这是本节最该背下来的一段代码。`train()` 函数外层按 epoch 循环、内层从 `dataloader` 取小批量，五件套 `zero_grad → 前向 → loss → backward → step` 一气呵成，每个 epoch 末尾在验证集上算一次准确率：

[lessons/3-NeuralNetworks/05-Frameworks/IntroPyTorch.ipynb:1507-1518](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/05-Frameworks/IntroPyTorch.ipynb#L1507-L1518) —— `def train(net, dataloader, val_x, val_lab, epochs=10, lr=0.05)`：用 `Adam` 优化器，每个 batch 里 `optim.zero_grad()` → `loss.backward()` → `optim.step()`，epoch 末打印 `val acc`。

核心三行（在 Notebook 中可见）是所有 PyTorch 项目的共同骨架：

```python
optim.zero_grad()    # 清空梯度
loss.backward()      # 自动反向传播
optim.step()         # 更新参数
```

**(5) PyTorch Lightning 的高层写法 —— 让训练循环消失**

同样一个网络，用 PyTorch Lightning（高层 API）后，你只需写 `training_step`（一个 batch 怎么算 loss）和 `configure_optimizers`（用哪个优化器），外层的 epoch/batch 循环、验证逻辑全由框架接管——这正是 Keras `fit` 的同款思想：

[lessons/3-NeuralNetworks/05-Frameworks/IntroPyTorch.ipynb:1765-1788](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/05-Frameworks/IntroPyTorch.ipynb#L1765-L1788) —— `class MyNetPL(pl.LightningModule)`：`training_step` 只负责前向 + 算 loss，`configure_optimizers` 返回 `SGD(lr=0.005)`，循环交给 Trainer。

**(6) Keras 的三行训练**

Keras 把网络当成层序列。下面是多分类的典型写法：两个 `Dense` 层（隐藏层 ReLU、输出层 softmax），`compile` 配好 Adam 与交叉熵损失，`fit` 传入验证集即可边训练边看验证准确率：

[lessons/3-NeuralNetworks/05-Frameworks/IntroKeras.ipynb:578-589](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/05-Frameworks/IntroKeras.ipynb#L578-L589) —— `Sequential([Dense(5, relu), Dense(2, softmax)])` → `compile(Adam(0.01), 'categorical_crossentropy', ['acc'])` → `fit(..., validation_data=..., batch_size=1, epochs=10)`。

**(7) 「任务类型 → 输出 → 激活函数 → 损失函数」对照表**

新手最容易卡在「分类任务该用哪个激活、哪个损失」。Keras Notebook 给了一张速查表，建议背下来：

[lessons/3-NeuralNetworks/05-Frameworks/IntroKeras.ipynb:678-683](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/05-Frameworks/IntroKeras.ipynb#L678-L683) —— 二分类用 `sigmoid` + `binary crossentropy`；多分类（独热编码）用 `softmax` + `categorical crossentropy`；多分类（类别编号）用 `softmax` + `sparse categorical crossentropy`。

其中 **softmax** 把一组任意实数压成「和为 1 的概率」：

\[
p_i = \frac{\exp(z_i)}{\sum_{j} \exp(z_j)}
\]

#### 4.1.4 代码实践

**实践目标**：亲手把 PyTorch 的五件套训练循环跑通，体会 autograd 取代手写 `backward` 的便利。

**操作步骤**：

1. 在 `ai4beg` 环境里打开 `lessons/3-NeuralNetworks/05-Frameworks/IntroPyTorch.ipynb`（环境搭建见 u1-l3）。
2. 从第一个 cell 顺序运行到「Computing Gradients」一节，观察 `a.grad` 自动得到梯度。
3. 继续运行到「用 `nn.Module` 定义网络」与 `train()` 训练循环的 cell。
4. 在 `train()` 调用处，把 `epochs` 从默认值改成 `5`，再改成 `30`，分别记录每个 epoch 末打印的 `val acc`。

**需要观察的现象**：训练开始时 `val acc` 随 epoch 上升；`backward()` 一行就完成了上一讲需要几十行才能写完的反向传播。

**预期结果**：`val acc` 随训练逐步提高（具体数值待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：PyTorch 训练循环里，如果把 `optim.zero_grad()` 这一行删掉，会发生什么？

> **参考答案**：PyTorch 的梯度默认累加，删掉后每一步的梯度会叠到之前所有步的梯度上，参数更新方向被污染，训练会发散或异常。

**练习 2**：把 PyTorch 的 `train()` 与 Keras 的 `fit` 对应起来——`fit` 的哪几个参数分别对应 `train()` 里的 `epochs`、`dataloader`、`val_x/val_lab`？

> **参考答案**：`epochs=...` 对应 `epochs`；`batch_size`（配合 `x,y`）对应 `dataloader` 的小批量划分；`validation_data=(val_x, val_lab)` 对应验证集 `val_x/val_lab`。

---

### 4.2 过拟合现象

#### 4.2.1 概念说明

能用工业级框架把训练误差压到接近 0，是好事吗？**不一定**——这正是**过拟合（overfitting）**要警告我们的。

过拟合指模型「把训练数据学得太死」，连数据里的噪声都背了下来，结果**对新数据（验证/测试集）反而预测得很差**。README 用一个经典对比说明这一点：用 5 个点拟合曲线——

- **线性模型（2 个参数）**：训练误差 5.3，验证误差 5.1——参数量与数据量匹配，抓住了数据背后的规律。
- **非线性模型（7 个参数）**：训练误差 0（穿过所有点），验证误差 20——模型太强，硬凑出一条穿过每个点的曲线，却完全没学到真实规律。

一句话：**要在「模型容量（参数量）」与「训练样本数」之间找平衡。**

#### 4.2.2 核心流程

过拟合的判据来自**两条曲线**：训练误差与验证误差随 epoch 的变化。

```
epoch:    1     5    10    15    20   ...
训练误差: 高 ──↓─────────────────────→ 趋近 0
验证误差: 高 ──↓──↓── (拐点) ──↑──↑──→ 反弹上升
                       ↑
                  这里开始过拟合，应停止训练
```

正常训练时两条曲线一起下降；当**验证误差停止下降并开始反弹**，就是过拟合信号，此时应停止训练（或保存这一刻的模型快照）。这与统计学里的**偏差-方差权衡（Bias-Variance Tradeoff）**是同一件事：

- **偏差误差（bias）**：模型能力不足、学不到数据规律——对应**欠拟合（underfitting）**。
- **方差误差（variance）**：模型把数据噪声也学进去了——对应**过拟合（overfitting）**。

训练过程中偏差下降、方差上升，需在拐点停下。

#### 4.2.3 源码精读

**(1) 过拟合的直观例子 —— 线性 vs 非线性拟合**

README 用 5 个点的拟合对比，给出过拟合最直观的图像与数值：

[lessons/3-NeuralNetworks/05-Frameworks/README.md:50-60](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/05-Frameworks/README.md#L50-L60) —— 线性模型（2 参数）训练误差 5.3 / 验证误差 5.1；非线性模型（7 参数）训练误差 0 / 验证误差 20，模型太强反而学不到规律。

**(2) 过拟合的三个成因与判据**

README 接着列出成因和「怎么发现」：

[lessons/3-NeuralNetworks/05-Frameworks/README.md:63-73](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/05-Frameworks/README.md#L63-L73) —— 成因：训练数据太少 / 模型太强 / 输入噪声太多；判据：训练误差很低但验证误差很高，验证误差由降转升即为过拟合信号。

**(3) 偏差-方差权衡**

README 把过拟合纳入更一般的统计框架：

[lessons/3-NeuralNetworks/05-Frameworks/README.md:83-90](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/05-Frameworks/README.md#L83-L90) —— 偏差误差对应欠拟合（模型不够强），方差误差对应过拟合（学进噪声）；训练时偏差降、方差升，需适时停止。

**(4) 在训练中观察验证指标 —— Keras 的 `validation_data`**

要发现过拟合，就得**边训练边在验证集上评估**。Keras 的 `fit` 通过 `validation_data` 参数自动每轮打印 `loss`/`acc` 与 `val_loss`/`val_acc`，正好用来观察拐点：

[lessons/3-NeuralNetworks/05-Frameworks/IntroKeras.ipynb:378-378](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/05-Frameworks/IntroKeras.ipynb#L378-L378) —— `model.fit(..., validation_data=(test_x_norm, test_labels), epochs=10, batch_size=1)`：每个 epoch 同时输出训练与验证指标，`val_acc` 由升转降即为过拟合。

Keras Notebook 在讨论超参数时也直接点出了过拟合风险：

[lessons/3-NeuralNetworks/05-Frameworks/IntroKeras.ipynb:390-390](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/05-Frameworks/IntroKeras.ipynb#L390-L390) —— 提示「学习率太高可能导致过拟合或结果不稳定」。

#### 4.2.4 代码实践

**实践目标**：亲眼看到「训练准确率继续涨、验证准确率开始掉」的过拟合拐点。

**操作步骤**：

1. 打开 `lessons/3-NeuralNetworks/05-Frameworks/IntroKeras.ipynb`，运行到含 `model.fit(..., validation_data=...)` 的 cell（约第 378 行附近）。
2. 把 `epochs` 改大到 `50`，重新运行。
3. 观察 cell 输出里每个 epoch 的 `acc` 与 `val_acc` 两列数值（也可运行紧随其后的 `plt.plot(hist.history['val_acc'])` 画曲线）。

**需要观察的现象**：前若干轮 `acc` 与 `val_acc` 同步上升；到某个 epoch 后，`acc` 仍上升或维持高位，`val_acc` 却开始下降或剧烈震荡。

**预期结果**：能定位到一个「验证准确率由升转降」的拐点 epoch（具体轮次待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：训练误差为 0 一定是好事吗？为什么？

> **参考答案**：不一定。若验证误差同时很高，说明模型只是「背下」了训练集（含噪声），属于过拟合，泛化能力差。

**练习 2**：欠拟合与过拟合分别对应偏差-方差里的哪种误差？训练时它们如何变化？

> **参考答案**：欠拟合对应高偏差（模型太弱），过拟合对应高方差（学了噪声）。训练中偏差误差下降、方差误差上升，需在方差开始主导时停下。

---

### 4.3 正则化方法

#### 4.3.1 概念说明

发现过拟合后怎么办？README 给出三类对策：

1. **增加训练数据**——最根本，数据多了模型自然背不完噪声。
2. **降低模型复杂度**——减少层数、神经元数或参数量，让模型「背不动」噪声。
3. **正则化（regularization）**——在不改数据、不大改结构的前提下，给训练加约束，**Dropout** 是其中最常用的技巧之一。

**Dropout（随机失活）** 的直觉：训练时每次随机「关闭」一部分神经元（按概率 `p` 把它们的输出置零），迫使网络不要过度依赖少数几个神经元，从而分散表达能力、降低过拟合。推理时关闭 Dropout、用全部神经元。

#### 4.3.2 核心流程

Dropout 一层的两种模式：

```
训练时 (net.train()):  对该层输出按概率 p 随机置零，并缩放保活的神经元
推理时 (net.eval()):   不做任何随机，使用全部神经元
```

PyTorch 用 `net.train()` / `net.eval()` 切换模式，Keras 则由 `fit`/`predict` 自动切换。此外，**早停（early stopping）**——即 4.2 里说的「验证误差一反弹就停」——也是一种正则化手段。

> 关于 Dropout 的完整代码演示，本课 README 明确把它**指向了后续的迁移学习课**，因此本节重点放在「知道有哪些手段、去哪里找」，而非在两个入门 Notebook 里硬找（入门 Notebook 里并没有 Dropout 代码）。

#### 4.3.3 源码精读

**(1) 应对过拟合的三类手段 —— README 的清单**

README 把「如何防止过拟合」浓缩成三条，并把 Dropout 等正则化技巧的细节指向后续课程：

[lessons/3-NeuralNetworks/05-Frameworks/README.md:75-81](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/05-Frameworks/README.md#L75-L81) —— 防止过拟合：增加数据量 / 降低模型复杂度 / 使用正则化（如 Dropout），并链接到 `4-ComputerVision/08-TransferLearning/TrainingTricks.md`。

**(2) Dropout 的实际代码 —— 在后续课程的 TrainingTricks 里**

README 指向的 Dropout 细节位于迁移学习课：

[lessons/4-ComputerVision/08-TransferLearning/TrainingTricks.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/08-TransferLearning/TrainingTricks.md) —— 训练技巧讲义，含 Dropout 一节；同目录下还有 `Dropout.ipynb` 可运行演示。

> 诚实说明：本课的 `IntroPyTorch.ipynb` 与 `IntroKeras.ipynb` 两个入门 Notebook **并未包含 Dropout 代码**，正则化的实操放在了计算机视觉单元的 `08-TransferLearning`。本节作为「目录索引」，指引你到正确位置。

#### 4.3.4 代码实践

**实践目标**：定位 Dropout 等正则化技巧的真实代码位置，并理解 `train()/eval()` 模式切换。

**操作步骤**（源码阅读型实践）：

1. 打开 README 第 75-81 行给出的链接，跳到 `lessons/4-ComputerVision/08-TransferLearning/TrainingTricks.md` 的 Dropout 一节。
2. 同目录打开 `Dropout.ipynb`，阅读其中如何把 `Dropout` 层加入网络、以及训练/推理时的差异。
3. 回到 `IntroPyTorch.ipynb` 的 `train()` 函数（4.1.3 第 4 点），思考：如果网络里加了 `nn.Dropout`，为什么训练循环里需要（隐式或显式地）保证 `net.train()`、验证时保证 `net.eval()`？

**需要观察的现象**：`Dropout.ipynb` 中加 Dropout 后验证准确率曲线比不加时更平稳。

**预期结果**：能口头解释「训练随机失活、推理全开」为何能抑制过拟合。Dropout 对验证集准确率的具体提升幅度待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：除了 Dropout，再举出两种本讲提到的抗过拟合手段。

> **参考答案**：增加训练数据量；降低模型复杂度（减少层数/神经元数）。此外，早停（验证误差反弹即停训）也是常用手段。

**练习 2**：为什么 Dropout 在训练时和推理时行为不同？

> **参考答案**：训练时随机置零部分神经元是为了阻止网络过度依赖少数节点、强制分散表达，降低过拟合；推理时需要稳定、确定的输出，因此用全部神经元（并配合缩放保证期望一致）。

---

## 5. 综合实践

完成本课的官方 lab：[lessons/3-NeuralNetworks/05-Frameworks/lab/LabFrameworks.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/05-Frameworks/lab/LabFrameworks.ipynb)。

**任务说明**（见 [lab/README.md:5-12](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/05-Frameworks/lab/README.md#L5-L12)）：用单层与多层全连接网络解决两个分类问题——

1. **Iris 鸢尾花分类**：4 个数值特征 → 3 类，典型的表格数据分类。
2. **MNIST 手写数字分类**：上一讲见过的数据集，这次用工业级框架重做。

**串联本讲三模块的练习路径**：

1. **框架训练循环**：先用 PyTorch（`nn.Module` + `train()` 五件套）实现一个多层全连接网络做 MNIST；再用 Keras（`Sequential` + `compile` + `fit`）做**同一个**任务，对比两种写法的代码量与最终准确率。这是把 4.1 落到代码。
2. **过拟合现象**：把网络加深、把 epoch 调大，用 `validation_data`/验证集观察 `val_acc` 曲线，定位过拟合拐点。这是把 4.2 落到代码。
3. **正则化方法**：在过拟合的网络里，分别尝试「减小隐藏层神经元数」和「加 Dropout（参考 4.3 指向的 TrainingTricks）」，看验证准确率是否回升。这是把 4.3 落到代码。

**交付**：记录三种设置（强模型无正则 / 减小容量 / 加 Dropout）下 MNIST 的训练准确率与验证准确率，写成一张对比表。

> 提示：lab README 写的是「用 PyTorch **或** TensorFlow」，但本讲要求**两个框架各做一遍同一个模型**并比较，以便亲手体会 4.1 的低层/高层 API 对照。比较的结论（准确率差异、代码差异）记录到你的学习笔记即可，**具体数值待本地验证**。

## 6. 本讲小结

- 工业级框架相对自研框架多了两件武器：**GPU 张量运算**与**自动求导（autograd）**；后者让我们彻底告别手写 `backward`。
- 框架分两层 API：**低层（TensorFlow/PyTorch）** 手动搭计算图、控制力强；**高层（Keras/PyTorch Lightning）** 把网络看作层序列、用 `fit` 一键训练。两层可混用。
- **PyTorch 训练循环五件套**：`zero_grad → 前向 → loss → backward → step`，外层 epoch、内层小批量；**Keras 三件套**：`Sequential → compile → fit`。二者骨架相同、抽象不同。
- 分类任务的「输出→激活→损失」要匹配：二分类 `sigmoid`+`binary crossentropy`，多分类 `softmax`+`categorical/sparse categorical crossentropy`。
- **过拟合**＝模型背下训练集噪声、对新数据泛化变差；判据是验证误差由降转升，对应偏差-方差权衡里的高方差。
- **应对过拟合**：增加数据、降低复杂度、正则化（Dropout 等）；Dropout 细节本课指向了后续 `08-TransferLearning` 课。

## 7. 下一步学习建议

- **横向巩固**：先把本讲的两个入门 Notebook 完整跑一遍（`IntroPyTorch.ipynb`、`IntroKeras.ipynb`），再做完第 5 节的 lab，确认你能独立写出训练循环。
- **纵向深入**：第 4 单元「计算机视觉」第 1 课（`06-IntroCV`）会从 OpenCV 图像处理起步，随后 `07-ConvNets` 用本讲学到的**同一套 PyTorch/Keras 训练循环**训练卷积神经网络——届时训练循环会成为肌肉记忆，重点将转向「卷积层」这个新组件。
- **补齐正则化**：在进入 CV 单元前，建议先顺路读一遍 `08-TransferLearning/TrainingTricks.md` 与 `Dropout.ipynb`，把本讲 4.3 留下的 Dropout、数据增强等训练技巧补全，正好衔接 CV 单元的迁移学习课。
