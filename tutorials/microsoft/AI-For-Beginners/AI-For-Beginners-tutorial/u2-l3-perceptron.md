# 感知机：最简单的神经元

## 1. 本讲目标

在上一单元里，我们用 `examples/01-hello-ai-world.py` 里只有一个权重的小学习者，体会了「从数据中学权重」的连接主义思想；在 [u2-l1](u2-l1-ai-intro-history.md) 里我们又把单个神经元抽象成 \(y=f(\sum_i w_i x_i+b)\)。本讲就把这个公式真正「落地」——讲清楚工程上最古老、最简单的一个神经元模型：**感知机（Perceptron）**。

学完本讲，你应当能够：

1. 写出感知机的前向计算公式，说清输入、权重、偏置、激活函数各自的角色。
2. 理解「阶跃激活函数」如何把一个连续打分变成 +1 / −1 的二分类结果。
3. 掌握感知机的权重更新规则（感知机准则 + 梯度下降），并能读懂课程 Notebook 里逐样本更新的训练循环。
4. 用课程的 `Perceptron.ipynb` 在二维数据上可视化**决策边界**，理解什么是「线性可分」、为什么 XOR 解不了。
5. 完成 `lab/PerceptronMultiClass.ipynb`，把二分类感知机扩展成「一对其余（one-vs-rest）」的 10 分类手写数字识别器。

---

## 2. 前置知识

本讲默认你已经掌握以下概念（若不熟，先回看对应讲义）：

- **从数据中学权重**：见 [u1-l4](u1-l4-first-ai-program.md)。核心是模型不再靠人写规则，而是靠训练数据反复调整参数（权重）。那里的一维模型 \(\hat{y}=w\cdot x\) 是本讲的「退化版」。
- **单个神经元的数学抽象**：见 [u2-l1](u2-l1-ai-intro-history.md)。一个神经元做的事是「加权求和再加偏置，最后过一个激活函数」：\(y=f(\sum_i w_i x_i+b)\)。
- **符号 AI vs 连接主义**：见 [u2-l2](u2-l2-symbolic-ai.md)。感知机是连接主义的最小可运行单元，与上一讲的专家系统（靠人写规则推理）形成鲜明对照——这里没有任何手写规则，只有权重。
- **少量 Python / NumPy**：能看懂 `np.dot`（向量点积）、`np.zeros`（建零向量）、数组切片即可。
- **向量与点积的直觉**：两个向量的点积 \(\mathbf{w}^{\mathrm T}\mathbf{x}\) 可以理解成「\(\mathbf{x}\) 在 \(\mathbf{w}\) 方向上的投影长度再乘以 \(\|\mathbf{w}\|\)」，其正负号决定了 \(\mathbf{x}\) 落在以 \(\mathbf{w}\) 为法线的超平面的哪一侧。这正是感知机分类的几何本质。

> 名词小词典
> - **二分类（binary classification）**：把样本分到两个类别之一，例如「良性 / 恶性」「数字 1 / 数字 0」。
> - **决策边界（decision boundary）**：模型把空间一分为二的那条线（或高维超平面）。
> - **线性可分（linearly separable）**：存在一条直线（超平面）能把两类样本完全分开。感知机只在数据线性可分时才保证收敛。
> - **偏置（bias）**：类似 \(y=kx+b\) 里的截距 \(b\)，决定直线整体上下平移；没有它，决策边界只能过原点。

---

## 3. 本讲源码地图

本讲全部围绕课程第 3 单元「神经网络」下的第 03 课，目录是 `lessons/3-NeuralNetworks/03-Perceptron/`：

| 文件 | 作用 | 本讲如何使用 |
|------|------|--------------|
| [`README.md`](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/03-Perceptron/README.md) | 课程讲义：感知机的历史、模型公式、训练准则 | 给出权威定义与感知机准则的数学表达 |
| [`Perceptron.ipynb`](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/03-Perceptron/Perceptron.ipynb) | 可执行 Notebook，从玩具数据到 MNIST | 本讲源码精读的主要对象，含完整 `train()`/`accuracy()`/`plot_boundary()` |
| [`lab/README.md`](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/03-Perceptron/lab/README.md) | 实验任务说明 | 综合实践的题目来源（多分类感知机） |
| [`lab/PerceptronMultiClass.ipynb`](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/03-Perceptron/lab/PerceptronMultiClass.ipynb) | 实验起始 Notebook（含半成品代码） | 综合实践中你要修改、补全的对象 |

> 小提示：Notebook 在 GitHub 上默认以「渲染后的富文本」展示。本讲给出的带行号永久链接（如 `Perceptron.ipynb#L235-L268`）指向的是 `.ipynb` 源文件（JSON）的对应行，便于你精确定位到 `train()` 这段代码。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，正好对应课程的设计：先把神经元当**数学模型**（4.1），再看它的**激活函数**如何产生分类输出（4.2），最后讲**训练算法**如何把权重学出来（4.3）。三者环环相扣：模型定义了「算什么」，激活函数定义了「怎么变成类别」，训练算法定义了「怎么变准」。

### 4.1 神经元数学模型

#### 4.1.1 概念说明

感知机由 Frank Rosenblatt 在 1957 年提出，最初是一台叫 **Mark-1** 的硬件设备，用一个神经元（他称之为 *threshold logic unit*，阈值逻辑单元）来识别三角形、正方形、圆等简单图形。课程 [`README.md`](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/03-Perceptron/README.md) 的开头就讲了这段历史。

感知机要解决的问题很具体：**二分类**。给定一个输入向量 \(\mathbf{x}\)（比如一张图的所有像素，或病人的若干体检指标），输出它属于两类中的哪一类，记作 \(+1\) 或 \(-1\)。

它的数学模型就是 [u2-l1](u2-l1-ai-intro-history.md) 里那个抽象神经元的具体化：

\[
y(\mathbf{x}) = f\!\left(\mathbf{w}^{\mathrm T}\mathbf{x} + b\right)
\]

其中：

- \(\mathbf{x}\in\mathbb{R}^N\) 是输入特征向量（\(N\) 个特征）。
- \(\mathbf{w}\in\mathbb{R}^N\) 是**权重向量**，每个特征配一个权重，表示该特征的重要程度与方向。
- \(b\) 是**偏置**（标量）。
- \(\mathbf{w}^{\mathrm T}\mathbf{x}+b\) 是一个连续的「打分」（score），几何上决定了 \(\mathbf{x}\) 在以 \(\mathbf{w}\) 为法线的超平面的哪一侧、距离多远。
- \(f\) 是**激活函数**（4.2 详讲），把打分映射成 \(+1/-1\)。

> 与 u1-l4 的关系：那里 \(\hat{y}=w\cdot x\) 只有一个权重、没有激活函数、做的是回归；这里把权重升级成向量、加上偏置和激活函数，就变成了一个能分类的神经元。可以说感知机是 `01-hello-ai-world.py` 的「多维 + 分类」升级版。

#### 4.1.2 核心流程：一个巧妙的工程简化——把偏置「吃」进权重

完整公式里有个独立的偏置 \(b\)，处理起来要单独维护一个标量。课程 Notebook 用了一个非常常见的工程技巧：**给输入人为补一维常数 1**，这样偏置就成了权重向量的最后一个分量，公式退化为

\[
y(\mathbf{x}) = f(\mathbf{w}^{\mathrm T}\mathbf{x}),\quad \text{其中 } \mathbf{x}\text{ 的最后一维恒为 }1
\]

这样 \(\mathbf{w}\) 的最后一个分量就承担了偏置的角色，代码里只需维护一个权重向量，点积一次搞定，省去了「加偏置」这步。这个技巧在后续自研框架（u2-l4）里还会反复出现。

#### 4.1.3 源码精读

课程 README 给出的权威模型定义：

> [lessons/3-NeuralNetworks/03-Perceptron/README.md:L19-L28](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/03-Perceptron/README.md#L19-L28)
> 这段定义了感知机是二分类模型，输出 \(y(\mathbf{x})=f(\mathbf{w}^{\mathrm T}\mathbf{x})\)，并指出 \(f\) 是阶跃激活函数。

Notebook 里把「补一维常数 1 吸收偏置」这个技巧落到了代码——构造正例时，每个样本从 2 维扩成 3 维，第 3 维恒为 1：

> [lessons/3-NeuralNetworks/03-Perceptron/Perceptron.ipynb:L192-L196](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/03-Perceptron/Perceptron.ipynb#L192-L196)
> `pos_examples` 的每个元素是 `[特征1, 特征2, 1]`，那个末尾的 `1` 就是用来吸收偏置的「虚拟特征」。于是整条权重向量长度变成 3，最后一维权重要学的其实就是偏置 \(b\)。

对应的数据生成代码（生成二维玩具数据，并把标签从 0/1 转成 −1/+1）：

> [lessons/3-NeuralNetworks/03-Perceptron/Perceptron.ipynb:L82-L85](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/03-Perceptron/Perceptron.ipynb#L82-L85)
> 用 `sklearn` 的 `make_classification` 造 50 个二维样本，`Y = Y*2-1` 把原始的 0/1 标签线性映射成感知机要求的 −1/+1 标签——这是感知机公式里 \(t_n\in\{-1,+1\}\) 的直接来源。

#### 4.1.4 代码实践：手算一个感知机的前向输出

这是一个纯纸笔练习，帮助你建立对「打分 + 阈值」的直觉。

1. **实践目标**：亲手算一次感知机前向计算，理解权重向量与偏置各自的作用。
2. **操作步骤**：
   - 取权重 \(\mathbf{w}=(1,-2,0.5)\)（最后一维 \(0.5\) 是偏置），输入样本 \(\mathbf{x}=(2,1,1)\)（末位 1 是补的虚拟特征）。
   - 计算打分 \(s=\mathbf{w}^{\mathrm T}\mathbf{x}=1\cdot2 + (-2)\cdot1 + 0.5\cdot1\)。
   - 套用阶跃函数 \(f(s)=+1\) 若 \(s\ge 0\)，否则 \(-1\)。
3. **需要观察的现象**：打分的正负号决定了类别；改变偏置 \(0.5\) 会整体平移决策边界。
4. **预期结果**：\(s=2-2+0.5=0.5\ge0\)，故输出 \(+1\)。
5. 若把偏置改成 \(-1\)，则 \(s=2-2-1=-1<0\)，输出翻成 \(-1\)——这就是偏置「平移决策边界」的体现。

#### 4.1.5 小练习与答案

**练习 1**：为什么把输入补一维常数 1 之后，就可以不再单独写偏置 \(b\)？

> **答案**：设补维后的权重为 \(\mathbf{w'}=(w_1,\dots,w_N,w_{N+1})\)，输入为 \(\mathbf{x'}=(x_1,\dots,x_N,1)\)。则 \(\mathbf{w'}^{\mathrm T}\mathbf{x'}=\sum_{i=1}^N w_i x_i + w_{N+1}\)，与原式 \(\mathbf{w}^{\mathrm T}\mathbf{x}+b\) 形式一致，只需令 \(b=w_{N+1}\)。于是偏置被「吸收」成最末一个权重，公式统一为一次点积。

**练习 2**：若所有特征都乘以 100，权重不变，分类结果会变吗？为什么？

> **答案**：分类结果由打分的**正负号**决定。所有特征同乘 100，打分也乘 100，正负号不变，故分类结果不变（但训练时打分绝对值变大，会影响更新步长，所以实际中常做特征归一化）。

---

### 4.2 激活函数

#### 4.2.1 概念说明

光有打分 \(s=\mathbf{w}^{\mathrm T}\mathbf{x}\) 还不够——打分是个任意实数，而我们要的是「属 +1 类还是 −1 类」这样一个判决。**激活函数** \(f\) 就是把连续打分「压缩」成离散类别的那一步。

感知机用的是最朴素的**阶跃（符号）激活函数**：

\[
f(s)=\begin{cases}+1,& s\ge 0\\ -1,& s<0\end{cases}
\]

它就是一个阈值判决：打分非负判 +1，为负判 −1，决策边界正是 \(s=0\) 即 \(\mathbf{w}^{\mathrm T}\mathbf{x}=0\) 这条直线（高维下是超平面）。

> 为什么感知机的激活函数这么「硬」？因为它本来就被设计成二分类判决器。后续课程（u2-l4 自研框架、u2-l5 PyTorch/Keras）会换成 sigmoid、ReLU 等「软」激活函数，那是为了让网络可多层堆叠、可平滑求导。本讲先把最简单的阶跃版本吃透。

#### 4.2.2 核心流程：从打分到决策边界

把前向过程串起来：

```
输入 x  →  补维(末位=1)  →  打分 s = w·x  →  阶跃 f(s)  →  类别 +1 / -1
```

而决策边界是令 \(s=0\) 得到的方程。在二维情形（补维后三维权重 \((w_0,w_1,w_2)\)，其中 \(w_2\) 是偏置），边界方程为：

\[
w_0 x_0 + w_1 x_1 + w_2 = 0
\]

整理成我们熟悉的直线方程：

\[
x_1 = -\frac{w_0 x_0 + w_2}{w_1}
\]

课程 Notebook 的 `plot_boundary()` 正是按这个公式画线。

#### 4.2.3 源码精读

阶跃激活函数在 Notebook 里**并没有显式写成一个函数**，而是直接用「打分的正负号」来表达——这正是阶跃函数的本质。体现在两处：

- 训练时判断「是否分错」：正例要求 \(z=\text{np.dot(pos, weights)}\ge 0\)，负例要求 \(z<0\)（见 4.3.3 的 `train()`）。
- 画决策边界时令 \(\mathbf{w}^{\mathrm T}\mathbf{x}=0\)：

> [lessons/3-NeuralNetworks/03-Perceptron/Perceptron.ipynb:L342-L356](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/03-Perceptron/Perceptron.ipynb#L342-L356)
> `plot_boundary()` 把 \(w_0 x_0+w_1 x_1+w_2=0\) 解成 \(x_1=-(w_0 x_0+w_2)/w_1\)，再叠上正例（蓝点）和负例（红点），画出绿色决策边界直线。代码里对 `weights[1]`（即 \(w_1\)）是否接近 0 做了特判，避免除零。

#### 4.2.4 代码实践：用阶跃函数「看」决策边界

1. **实践目标**：在 `Perceptron.ipynb` 里运行到画图 cell，亲眼看到一条直线把两类点分开。
2. **操作步骤**：在 `ai4beg` 内核下从前往后执行 Notebook，直到运行调用 `plot_boundary(pos_examples, neg_examples, wts)` 的 cell（`Perceptron.ipynb` 中位于 `plot_boundary` 定义之后的那个 cell）。
3. **需要观察的现象**：绿色直线大致把蓝色正例与红色负例分开；直线两侧分别是 \(f(s)=+1\) 与 \(f(s)=-1\) 的区域。
4. **预期结果**：直线把绝大多数点正确分到两侧，准确率接近 90%（与训练日志一致）。
5. 若点群无法被一条直线分开（例如 XOR），这条绿线无论怎么摆都会分错一些点——这就是 4.3 要讲的线性可分限制。**待本地验证**：具体准确率随随机种子（Notebook 开头 `np.random.seed(1)`）与学习率而变。

#### 4.2.5 小练习与答案

**练习 1**：决策边界 \(\mathbf{w}^{\mathrm T}\mathbf{x}=0\) 为什么一定是「直的」（线性）？

> **答案**：因为 \(\mathbf{w}^{\mathrm T}\mathbf{x}\) 是关于 \(\mathbf{x}\) 的一次（线性）函数，令其为 0 得到的等式约束在几何上就是一条直线（二维）或超平面（高维）。没有非线性项，所以边界只能是直的。

**练习 2**：把阶跃函数换成「打分本身」作为输出（即 \(f(s)=s\)），感知机还能做二分类吗？

> **答案**：仍可以分类——只需约定「打分 ≥0 判 +1，否则 −1」，判决规则没变。但作为「模型输出」，它给出的不再是类别标签而是连续打分，更适合做回归或排序。这正是 [u1-l4](u1-l4-first-ai-program.md) 那个一维学习者（无激活函数）与感知机（有阶跃激活）的区别。

---

### 4.3 感知机训练算法

#### 4.3.1 概念说明

模型和激活函数定义好了，剩下的核心问题是：**权重 \(\mathbf{w}\) 从哪来？** 答案就是「训练」——从数据里学。

感知机训练的目标是找一个 \(\mathbf{w}\)，让分错的样本尽量少。课程用一个叫**感知机准则（perceptron criterion）**的误差来衡量：

\[
E(\mathbf{w}) = -\sum_{n\in\mathcal{M}} \mathbf{w}^{\mathrm T}\mathbf{x}_n\, t_n
\]

其中 \(\mathcal{M}\) 是**当前被分错的样本集合**，\(t_n\in\{-1,+1\}\) 是真实标签。直觉是：对每个分错的样本，我们希望「打分 \(\mathbf{w}^{\mathrm T}\mathbf{x}_n\) 与真实标签 \(t_n\) 同号」，于是它们的乘积应为正；分错时乘积为负，加负号后 \(E\) 为正，成了我们要最小化的误差。

> 注意：这里的「误差」只在分错的样本上累加，分对的样本不贡献——这是感知机准则与后续均方误差、交叉熵的关键不同。

#### 4.3.2 核心流程：梯度下降 → 权重更新规则

用**梯度下降**最小化 \(E\)。对 \(\mathbf{w}\) 求梯度：

\[
\nabla E(\mathbf{w}) = -\sum_{n\in\mathcal{M}} \mathbf{x}_n t_n
\]

代入下降公式 \(\mathbf{w}^{\tau+1}=\mathbf{w}^{\tau}-\eta\nabla E\)，得到感知机的**权重更新规则**：

\[
\mathbf{w}^{\tau+1} = \mathbf{w}^{\tau} + \eta\sum_{n\in\mathcal{M}} \mathbf{x}_n t_n
\]

其中 \(\eta\) 是**学习率**（learning rate），控制每步走多远。把求和拆成「逐样本」就得到了课程代码里那种**随机感知机算法**（每次只取一两个样本更新）：

```
初始化 w = 0
重复 num_iterations 次：
    随机取一个正例 pos 和一个负例 neg
    若 w·pos < 0（正例被错判成负）：w ← w + η·pos      # 相当于 t=+1
    若 w·neg ≥ 0（负例被错判成正）：w ← w − η·neg      # 相当于 t=−1
返回 w
```

> **训练能收敛吗？** 感知机有收敛定理（perceptron convergence theorem）：**当数据线性可分时**，上述算法必在有限步内把所有样本分对。但若数据**不是**线性可分，算法会来回震荡永不收敛。Notebook 用 XOR 演示了这一局限。

#### 4.3.3 源码精读

Notebook 里完整、可运行的训练循环（这是本讲最重要的一段代码）：

> [lessons/3-NeuralNetworks/03-Perceptron/Perceptron.ipynb:L235-L268](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/03-Perceptron/Perceptron.ipynb#L235-L268)
> `train()` 函数。要点：
> - 权重初始化为 `np.zeros((num_dims,1))`（全零），含偏置那一维。
> - 每轮随机抽一个正例、一个负例，计算 `z = np.dot(pos, weights)`。
> - **更新规则的落地**：正例若 `z < 0`（分错）则 `weights = weights + learning_rate * pos.reshape(weights.shape)`，对应公式 \(\mathbf{w}\leftarrow\mathbf{w}+\eta\mathbf{x}t\) 中 \(t=+1\) 的情形；负例若 `z >= 0`（分错）则减去 \(\eta\cdot\)neg，对应 \(t=-1\)。代码用「加减分支」把 \(\eta\mathbf{x}t\) 里的正负号 \(t\) 编码进去了。
> - 每 10 轮在全部样本上统计正/负例正确率并打印，用来观察收敛。

更新规则也可以在课程 README 里看到概念骨架：

> [lessons/3-NeuralNetworks/03-Perceptron/README.md:L41-L47](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/03-Perceptron/README.md#L41-L47)
> 推导出权重更新公式 \(\mathbf{w}^{(t+1)}=\mathbf{w}^{(t)}+\sum\eta\mathbf{x}_i t_i\)。（README 第 51–68 行还贴了一段示意 `train()`，注意那只是**概念骨架**，真正能跑的是 Notebook 里的版本。）

学习率的影响在 Notebook 里专门做了对比实验：

> [lessons/3-NeuralNetworks/03-Perceptron/Perceptron.ipynb:L404-L420](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/03-Perceptron/Perceptron.ipynb#L404-L420)（对应说明该影响的 markdown cell，编号 cell-10）
> 课程明确指出：学习率过大（如 `1.0`）学得快但可能越过最优；过小（如 `0.001`）学得慢但更精细。

线性可分的局限——XOR 实验：

> Notebook cell-25 ~ cell-28（`Perceptron.ipynb` 中 XOR 训练与讨论部分）
> 用 4 个样本构造 XOR 真值表，训练 1000 轮后准确率始终上不了 100%（卡在 75%），因为没有任何一条直线能把 XOR 的两类分开。课程借此引出 Minsky & Papert 1969 年的著名批评，并预告多层网络能解决它。

#### 4.3.4 代码实践：调学习率，观察收敛与发散

1. **实践目标**：直观感受学习率 \(\eta\) 对训练的影响，呼应 u1-l4 里「学习率是步长旋钮」的结论。
2. **操作步骤**：
   - 在 `Perceptron.ipynb` 的「Experimenting with Learning Rates」cell（cell-17）里，列表已是 `learning_rates = [0.001, 0.01, 0.1, 1.0]`，依次运行。
   - 或者手动调用 `train(pos_examples, neg_examples, num_iterations=100, learning_rate=0.1)` 并对比不同值的训练日志。
3. **需要观察的现象**：不同 \(\eta\) 下，决策边界（绿线）达到稳定的速度和最终位置不同；过大的 \(\eta\) 可能让边界来回抖动。
4. **预期结果**：合适的学习率（如 `0.01`）几轮内正/负例正确率就升到 ~90%；过小则 100 轮还没学好；过大可能出现震荡。**待本地验证**：因随机抽样，每次运行数值略有差异，但趋势稳定。
5. 进阶：把 `num_iterations` 调到很大（如 1000），观察 XOR 数据下准确率是否始终无法到 100%，亲证「线性不可分 → 不收敛」。

#### 4.3.5 小练习与答案

**练习 1**：更新公式 \(\mathbf{w}\leftarrow\mathbf{w}+\eta\mathbf{x}t\) 里，为什么正例（\(t=+1\)）被分错时要**加**上 \(\eta\mathbf{x}\)？

> **答案**：正例被分错意味着打分 \(\mathbf{w}^{\mathrm T}\mathbf{x}<0\)，我们希望把它往「打分变大、变正」的方向调。梯度下降给出的更新方向是 \(\mathbf{x}t=\mathbf{x}\)（因 \(t=+1\)），沿 \(\mathbf{x}\) 方向走一步，新的打分 \((\mathbf{w}+\eta\mathbf{x})^{\mathrm T}\mathbf{x}=\mathbf{w}^{\mathrm T}\mathbf{x}+\eta\|\mathbf{x}\|^2\) 比原来增大了 \(\eta\|\mathbf{x}\|^2>0\)，正好朝正确方向修正。

**练习 2**：感知机准则只在分错的样本上累加误差。如果一个样本已经被分对，下一轮它还会影响权重吗？

> **答案**：不会。因为它不在集合 \(\mathcal{M}\) 中，对 \(E\) 的梯度没有贡献。在逐样本实现的代码里，分对的样本不进入 `if z<0` / `if z>=0` 的分支，权重不变。这是感知机「只纠错」的特性。

**练习 3**：给定一组**线性可分**的数据，感知机训练一定收敛吗？给定一组**线性不可分**的数据呢？

> **答案**：线性可分时，由感知机收敛定理，必在有限步内把全部样本分对（收敛）。线性不可分时，不存在能全分对的超平面，算法会反复纠错、来回震荡，永不收敛——这就是 XOR 实验观察到的现象，也正是历史上一度让神经网络研究陷入低谷的原因。

---

## 5. 综合实践

**任务来源**：[`lab/README.md`](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/03-Perceptron/lab/README.md) 的「Multi-Class Classification with Perceptron」。

**背景**：感知机天生只能做二分类，但 MNIST 是 10 个数字的识别问题。怎么用二分类器做十分类？课程给出的标准思路是**一对其余（one-vs-rest）**：为每个数字训练一个「我是这个数字 / 我不是」的二分类感知机，共 10 个；预测时让 10 个感知机都打分，取打分最高（`argmax`）的那个数字。

**实验目标**：在 [`lab/PerceptronMultiClass.ipynb`](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/03-Perceptron/lab/PerceptronMultiClass.ipynb) 中，把课程给的二分类 `set_mnist_pos_neg(positive_label, negative_label)` 改造成「一对其余」数据集，训练 10 个感知机，拼成一个十分类器，并报告准确率与混淆矩阵。

**操作步骤**：

1. 打开 `lab/PerceptronMultiClass.ipynb`，确认 `ai4beg` 内核。起始 Notebook 已提供本课的 `train()` 与 `accuracy()`：
   > [lessons/3-NeuralNetworks/03-Perceptron/lab/PerceptronMultiClass.ipynb:L50-L78](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/03-Perceptron/lab/PerceptronMultiClass.ipynb#L50-L78)
   > 这是实验用的 `train()`，结构与 4.3.3 一致（注意此处没有显式 `learning_rate` 参数，步长隐含为 1）。
2. 仿照课程提示（[lab/README.md:L9-L15](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/03-Perceptron/lab/README.md#L9-L15)），为每个数字 \(d\in\{0,\dots,9\}\) 构造正例=数字 \(d\)、负例=其余所有数字的「一对其余」数据集，训练 10 个感知机，得到 10 个权重向量。
3. 实现分类函数 `classify(x)`：对输入 \(x\)，计算 10 个感知机的打分，返回 `argmax` 对应的数字。
4. **进阶（推荐）**：把 10 个权重向量堆成一个矩阵 \(W\)，用一次矩阵乘法 \(W\mathbf{x}\) 同时算出 10 个打分，再 `argmax`。这正是课程 hint 所说的「矩阵化」技巧，能让推理又快又简洁。
5. 在测试集上统计准确率，并用 `sklearn.metrics.confusion_matrix` 画混淆矩阵，观察哪些数字最易混淆（通常是与某些数字线性可分性差的，呼应主 Notebook 里 2 vs 5 的讨论）。

**需要观察的现象**：

- 单个「数字 d vs 其余」感知机的训练日志里，正例（数字 d）相对稀少（约占 1/10），负例极多，正/负例正确率会不对称。
- 最终十分类准确率通常显著低于「0 vs 1」这种简单二分类（后者在主 Notebook 里接近 100%），因为「一对其余」让每个分类器面对的是更难分的数据。

**预期结果**：能跑通一个端到端的 10 分类感知机；测试准确率「待本地验证」（取决于训练轮数与随机种子，课程未给固定数值），但混淆矩阵应显示形状相近的数字（如 4/9、3/5、3/8）误判较多。

**交付物**：在 Notebook 末尾打印总体准确率与混淆矩阵，并用一两句话分析准确率最低的那对数字为什么难分（提示：在 784 维像素空间里它们线性可分性差，可参考主 Notebook 的 PCA 分析）。

---

## 6. 本讲小结

- 感知机是最简单的神经元：前向计算 \(y=f(\mathbf{w}^{\mathrm T}\mathbf{x}+b)\)，把加权求和过激活函数得到二分类结果。
- 课程用一个工程技巧——给输入补一维常数 1——把偏置吸收进权重向量，公式统一成一次点积，代码只需维护一个权重向量。
- 激活函数用的是阶跃（符号）函数，决策边界是线性的 \(\mathbf{w}^{\mathrm T}\mathbf{x}=0\)。
- 训练靠**感知机准则 + 梯度下降**，得到更新规则 \(\mathbf{w}\leftarrow\mathbf{w}+\eta\mathbf{x}t\)；Notebook 的 `train()` 用「加减分支」把标签符号 \(t\) 编码进去，只在分错时更新。
- 学习率 \(\eta\) 是步长旋钮，太大震荡、太小缓慢——这与 u1-l4 的结论一脉相承。
- 感知机是**线性分类器**，只在数据线性可分时收敛；XOR 是经典反例，正是它当年被批评、促使人们走向多层网络的起因。

---

## 7. 下一步学习建议

本讲只造了「一个」神经元。要解决 XOR、要做真正的多层网络，下一步请进入：

- **[u2-l4 从零搭建自己的神经网络框架](u2-l4-own-framework.md)**：把单个感知机扩展成多层、把阶跃激活换成可导的 sigmoid、用计算图和反向传播自动求导——这是理解一切深度学习框架内核的关键一课，正好回答「多层网络怎么解决 XOR」。
- **[u2-l5 引入 PyTorch/Keras 框架与过拟合](u2-l5-frameworks-overfitting.md)**：从自研框架过渡到工业级框架，并接触过拟合、Dropout 等训练实务。

如果想立刻加深对感知机的理解，推荐继续阅读源码：

- 重跑 `Perceptron.ipynb` 的 MNIST 部分（cell-28 之后），观察「0 vs 1」近 100% 准确、而「2 vs 5」卡在 ~85% 的现象，并用其 PCA 分析（cell-41 ~ cell-44）从几何上理解「线性可分性」差异。
- 阅读 [`README.md`](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/3-NeuralNetworks/03-Perceptron/README.md) 末尾推荐的延伸文章，补充对感知机历史与局限的认识。
