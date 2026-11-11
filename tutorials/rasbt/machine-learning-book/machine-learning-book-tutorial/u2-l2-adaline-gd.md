# u2-l2 AdalineGD 与梯度下降

## 1. 本讲目标

上一讲我们实现了感知机：它只能靠「分错的样本」推动权重更新，阶跃函数不可导，导致它没有可优化的连续目标。本讲进入 `ch02/ch02.py` 中的第二个模型 `AdalineGD`（ADAptive LInear NEuron，自适应线性神经元），学完后你应当能够：

1. 说清感知机与 Adaline 的本质区别：连续线性激活函数 + 平方误差代价函数，取代阶跃函数 + 误分类计数。
2. 推导均方误差（MSE）代价函数关于权重的梯度，并逐行读懂 `fit` 中向量化的整批（batch）梯度下降更新。
3. 理解 `activation`（恒等函数）与 `predict`（阈值 0.5 的阶跃）各自在代码中的角色。
4. 通过书中经典的两个学习率实验（η=0.1 与 η=0.0001），解释梯度下降为什么会发散或收敛过慢，并亲手找到一个能让代价平稳下降的学习率。

## 2. 前置知识

本讲只需要一点微积分直觉，不需要深度数学背景。

- **代价函数（cost function）**：给一组权重「打分」的函数，分数越低说明模型拟合得越好。机器学习训练的本质就是「调整参数让代价函数变小」。感知机没有显式代价函数，只有每轮误分类数 `errors_`，它是离散的、不可导的，无法用微积分优化。
- **导数与梯度**：一元函数的导数表示「往右挪一点，函数值变多少」。多元函数对所有参数各求一次偏导，拼成的向量叫梯度 \( \nabla J \)，它指向代价上升最快的方向。反着走——沿负梯度方向更新参数——就是**梯度下降（gradient descent）**。
- **为什么需要可导的激活函数**：上一讲看到，感知机的阶跃函数 \( \phi(z) \) 在 z=0 处是「断崖」，导数处处为 0（或不存在），链式法则在这里断裂。Adaline 的解法是：训练时用**恒等函数** \( \phi(z)=z \) 作为激活，让输出保持连续实数，整个代价函数就变成权重的光滑二次函数，可以求导了。
- **凸函数**：形状像一只碗的函数，只有一个全局最低点。恒等激活下的 MSE 代价关于 \( (\mathbf{w}, b) \) 是凸二次函数，所以「只要学习率合适，梯度下降一定能走到碗底」。
- **超参数（hyperparameter）**：训练前人工设定、不被算法自己学到的参数，比如学习率 η 和迭代轮数 `n_iter`。本讲的核心教训就是：η 是梯度下降最敏感的超参数。
- **前置讲义**：u1-l3 中我们用 `df.iloc[0:100, [0, 2]].values` 与 `np.where` 准备好了特征矩阵 `X`（100 个样本 × 2 个特征）和 0/1 标签 `y`；u2-l1 中我们读懂了感知机的 `net_input`/`predict`/`fit`。本讲直接复用这些产出。

## 3. 本讲源码地图

| 文件 | 关键位置 | 作用 |
| --- | --- | --- |
| ch02/ch02.py | L307-L388 | `AdalineGD` 类：本讲主角，含 `fit`/`net_input`/`activation`/`predict` |
| ch02/ch02.py | L393-L408 | 书中实验：η=0.1 与 η=0.0001 两幅代价曲线对比 |
| ch02/ch02.py | L201-L206 | 数据准备：从鸢尾花 DataFrame 切出 `X`（萼片长、花瓣长）与 0/1 标签 `y` |
| ch02/ch02.py | L92-L158 | `Perceptron` 类：对照阅读，理解 Adaline 改了什么 |
| ch02/ch02.py | L425-L451 | 特征标准化与 η=0.5 的收敛实验：下一讲（u2-l3）的预告 |
| ch02/iris.data | — | 鸢尾花原始数据，无表头、逗号分隔，150 行 |

读码提示（承接 u1-l1 的结论）：`ch02.py` 由 `ch02.ipynb` 自动转换生成，markdown 单元格都变成了 `#` 注释；引用行号请以 `.py` 为准，动手实验可以在 notebook 里做对应单元格。

## 4. 核心概念与源码讲解

### 4.1 从感知机到 Adaline：连续激活函数与代价函数

#### 4.1.1 概念说明

1960 年 Bernard Widrow 与 Ted Hoff 提出的 Adaline，与 Rosenblatt 的感知机几乎同时代，但训练思路截然不同。两者的对比如下：

| | 感知机（u2-l1） | Adaline（本讲） |
| --- | --- | --- |
| 训练时的输出 | 阶跃函数后的 0/1 标签 ŷ | 连续实数净输入 \( o = z \)（恒等激活） |
| 与标签比较 | ŷ 与 y 都是离散 0/1 | o 与 y 之差是连续误差向量 |
| 优化目标 | 无显式代价函数，误分类驱动更新 | 最小化均方误差 \( J(\mathbf{w},b) \) |
| 更新频率 | 每个样本更新一次（一轮最多 n 次） | 每轮用全批梯度只更新一次 |
| 收敛性 | 线性可分数据必收敛，η 几乎不影响 | 凸二次代价，η 合适时收敛到全局最小 |

关键洞察：Adaline 把「比较」这一步从阶跃函数**之后**挪到了**之前**。感知机比较的是「阶跃后的预测标签」与真实标签；Adaline 比较的是「连续输出值」与真实标签。就这一处改动，让代价函数变得光滑可导，梯度下降从此可用——这条路线一直延伸到今天的深度学习。

#### 4.1.2 核心流程

Adaline 的完整数学表述（记样本数为 \( n \)，本讲数据 \( n=100 \)）：

净输入（与感知机完全相同）：

\[ z = \mathbf{w}^\top \mathbf{x} + b \]

线性激活（恒等函数）：

\[ \phi(z) = z \]

输出 \( o = \phi(z) \)。均方误差代价函数：

\[ J(\mathbf{w}, b) = \frac{1}{n}\sum_{i=1}^{n}\bigl(y^{(i)} - o^{(i)}\bigr)^2 \]

书中把它称为平方误差和（SSE）家族的代价；代码实际取的是「均值」版本（MSE，即 SSE 除以 n），好处是学习率的合适量级不会随样本数变化。

最终预测时才用阶跃函数，且阈值是 0.5 而非感知机的 0.0：

\[ \hat{y} = 1 \; \text{若} \; o \ge 0.5, \; \text{否则} \; \hat{y} = 0 \]

为什么是 0.5？因为训练目标让 \( o \) 逼近标签值 0 或 1，两类的「中点」自然是 0.5；感知机的净输入以 0 为界，是因为它的输出本来就围绕 ±1 或 0/1 的符号含义设计。决策边界在几何上仍是一条直线（\( o = 0.5 \) 即 \( \mathbf{w}^\top\mathbf{x} + b = 0.5 \)，只是对 \( b \) 做了等价平移）。

#### 4.1.3 源码精读

类的三个「推理」方法：[ch02/ch02.py:L378-L388](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L378-L388) —— `net_input` 计算线性净输入（与感知机逐字相同）；`activation` 是恒等函数，原样返回输入；`predict` 在最终预测时套一层阶跃，阈值取 0.5。

```python
def net_input(self, X):
    """Calculate net input"""
    return np.dot(X, self.w_) + self.b_

def activation(self, X):
    """Compute linear activation"""
    return X

def predict(self, X):
    """Return class label after unit step"""
    return np.where(self.activation(self.net_input(X)) >= 0.5, 1, 0)
```

你可能会问：恒等函数写出来毫无作用，为什么还要单独一个方法？源码里 `fit` 中的注释直接回答了这个问题：[ch02/ch02.py:L359-L365](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L359-L365) —— 作者说明 activation 在这里「没有实际效果」，完全可以写成 `output = self.net_input(X)`；保留它是**概念性**的：以后实现逻辑回归（第 3 章）时，只需把它换成 sigmoid 函数，类的其余结构不动。

规格说明：本仓库这一版代码中并不存在名为 `_activate` 或 `cost` 的独立方法——代价是直接在 `fit` 循环内以 `loss = (errors**2).mean()` 一行计算的（见 4.2.3）；带下划线的私有方法（`_shuffle`、`_update_weights` 等）出现在下一讲的 `AdalineSGD` 中。读代码时请以真实源码为准。

对照感知机的 `predict`：[ch02/ch02.py:L156-L158](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L156-L158) —— 感知机直接用 `net_input(X) >= 0.0` 做阶跃，因为它从不输出连续值；Adaline 则把阶跃推迟到最后一刻。

#### 4.1.4 代码实践

1. **实践目标**：亲眼确认 Adaline 训练时的输出是连续实数而非 0/1，并理解 `activation` 只是占位。
2. **操作步骤**：
   - 在 ch02 的 notebook（或自己的脚本）中先运行数据准备单元格，得到 `X`、`y`；
   - 复制 `AdalineGD` 类定义并执行（来源：[ch02/ch02.py:L307-L388](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L307-L388)）；
   - 执行下面的**示例代码**（自行编写，非仓库原有）：

   ```python
   ada = AdalineGD(n_iter=1, eta=0.0001).fit(X, y)   # 只训练 1 轮，便于观察

   # 恒等激活：activation 对输出没有任何改变
   z = ada.net_input(X)
   print(np.array_equal(ada.activation(z), z))        # 预期 True

   # 看一小段连续输出与连续误差（感知机里这两者只能是 0/1 和 -1/0/1）
   o = ada.activation(ada.net_input(X))
   print(o[:5].round(4))                              # 连续值，例如形如 [-0.0031, ...]
   print((y[:5] - o[:5]).round(4))                    # 连续误差
   ```
3. **需要观察的现象**：`array_equal` 返回 `True`；`o` 的前 5 个值是带小数的连续数，而不是 0 或 1。
4. **预期结果**：验证「训练用连续输出、预测才用阶跃」。具体打印数值与随机初始化有关，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：AdalineGD 的 `predict` 阈值是 0.5，感知机是 0.0，为什么不同？

**答案**：Adaline 的连续输出经过训练后会逼近标签值 0 和 1，两类预测的自然分界点是它们的中点 0.5；感知机只看净输入的符号，0.0 就是它的天然分界。两个阈值在各自语义下都对应「决策边界落在两类之间」。

**练习 2**：如果把 `activation` 方法直接改成 `return 1 / (1 + np.exp(-X))`（sigmoid），`fit` 里的更新公式还正确吗？

**答案**：不正确。`fit` 中的梯度更新是针对恒等激活推导的（见 4.2 的推导）；换成 sigmoid 后，按链式法则梯度会多出一个 \( o(1-o) \) 因子，必须同步修改更新公式。这也解释了注释里说的「概念占位」：换激活函数不是只改一行，而是要重推梯度。

**练习 3**：既然 `activation` 是恒等函数，删掉它会让程序出错吗？会让模型变差吗？

**答案**：不会出错、模型也不会变差——把 `fit` 里的 `output = self.activation(net_input)` 换成 `output = net_input`、`predict` 里去掉 `.activation(...)` 结果完全一样。它存在的意义是架构上的：让「激活函数」成为类的一个显式可替换部件，第 3 章的逻辑回归正是在这个位置换上 sigmoid。

### 4.2 整批梯度计算与权重更新：精读 `fit`

#### 4.2.1 概念说明

有了可导的代价函数，训练就变成标准的梯度下降：每轮（epoch）用**全部 n 个样本**一次性算出代价函数的梯度，然后更新一次权重。因为每轮都用「整个批次」，这叫**整批梯度下降（batch gradient descent）**。它与感知机的逐样本更新形成鲜明对比：

- 感知机 `fit`：内层 `for xi, target in zip(X, y)` 逐样本判断、逐样本更新（[ch02/ch02.py:L142-L149](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L142-L149)）。
- Adaline `fit`：没有内层循环，所有样本的误差先汇总成一个梯度，再更新一次。

代价是权重的凸二次函数，所以梯度下降像「下山」：站在山坡上（当前权重），找最陡的下坡方向（负梯度），走一小步（步长由 η 控制），重复。只要步长合适，最终一定走到谷底（全局最小值）。

#### 4.2.2 核心流程

先手推梯度。记误差 \( e^{(i)} = y^{(i)} - o^{(i)} \)，代价：

\[ J(\mathbf{w}, b) = \frac{1}{n}\sum_{i=1}^{n}\bigl(e^{(i)}\bigr)^2, \qquad o^{(i)} = \mathbf{w}^\top\mathbf{x}^{(i)} + b \]

对第 j 个权重求偏导（链式法则：先对 \( o \) 求导得 \( -2e^{(i)} \)，再乘上 \( \partial o/\partial w_j = x_j^{(i)} \)）：

\[ \frac{\partial J}{\partial w_j} = -\frac{2}{n}\sum_{i=1}^{n} e^{(i)}\, x_j^{(i)}, \qquad \frac{\partial J}{\partial b} = -\frac{2}{n}\sum_{i=1}^{n} e^{(i)} \]

沿负梯度方向更新（负号相互抵消，得到「加法」形式）：

\[ \mathbf{w} \leftarrow \mathbf{w} + \eta\cdot\frac{2}{n}\,X^\top\mathbf{e}, \qquad b \leftarrow b + \eta\cdot 2\,\overline{e} \]

其中 \( \overline{e} \) 是误差的均值。写成伪代码：

```text
初始化: w ~ N(0, 0.01²), b = 0, losses_ = []
重复 n_iter 轮:
    z = X @ w + b          # 一次性算全部样本的净输入
    o = z                  # 恒等激活
    e = y - o              # 连续误差向量, 形状 (n,)
    w ← w + η * (2/n) * Xᵀ e      # 全批梯度更新权重
    b ← b + η * 2 * mean(e)       # 全批梯度更新偏置
    记录 mean(e²) 到 losses_      # 本轮 MSE 代价
```

#### 4.2.3 源码精读

构造参数：[ch02/ch02.py:L331-L334](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L331-L334) —— 默认 `eta=0.01`、`n_iter=50`、`random_state=1`，与感知机的签名一致，方便两章对照实验。

`fit` 的初始化部分：[ch02/ch02.py:L352-L355](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L352-L355) —— 用固定种子的随机数发生器把权重初始化为均值 0、标准差 0.01 的小随机数（避免全零初始化的对称问题），偏置初始化为 0，并准备记录每轮代价的空列表 `losses_`。`np.float_(0.)` 就是浮点数 0.0（NumPy 2.x 中该别名可能报错，届时直接写 `0.0` 即可）。

`fit` 的核心循环：[ch02/ch02.py:L357-L376](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L357-L376) —— 每个 epoch 依次完成「全批前向 → 误差 → 向量化梯度更新 → 记录代价」四步：

```python
for i in range(self.n_iter):
    net_input = self.net_input(X)        # 全部样本的 z
    output = self.activation(net_input)  # 恒等激活，output == net_input
    errors = (y - output)                # 连续误差向量 e

    self.w_ += self.eta * 2.0 * X.T.dot(errors) / X.shape[0]
    self.b_ += self.eta * 2.0 * errors.mean()
    loss = (errors**2).mean()
    self.losses_.append(loss)
```

两处细节值得逐一对照公式：

- 权重更新行 [ch02/ch02.py:L372](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L372) 正是 \( \mathbf{w} + \eta\cdot\frac{2}{n}X^\top\mathbf{e} \)。系数 `2.0` 来自对平方求导；除以 `X.shape[0]`（即 n）把「求和」换成「均值」。
- 代价行 [ch02/ch02.py:L374-L375](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L374-L375) 计算 \( \text{mean}(e^2) \) 并存入 `losses_`——这就是本讲的「cost」，没有独立方法，就是这一行。

源码中还保留了一段被注释掉的逐权重写法：[ch02/ch02.py:L369-L370](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L369-L370) —— 逐个权重计算 `mean(X[:, w_j] * errors)` 再累加：

```python
#for w_j in range(self.w_.shape[0]):
#    self.w_[w_j] += self.eta * (2.0 * (X[:, w_j]*errors)).mean()
```

它与 L372 的 `X.T.dot(errors) / X.shape[0]` 在数学上完全等价：`X.T @ e` 的第 j 个分量就是 `X[:, j]` 与 `e` 的内积，再除以 n 就是逐分量乘积的均值。向量化写法把 Python 循环交给 NumPy 的底层实现，快得多——这也是全书代码风格的一贯选择。

#### 4.2.4 代码实践

1. **实践目标**：验证「注释掉的逐权重循环」与「向量化更新」结果一致，理解两者等价性。
2. **操作步骤**：
   - 复制 `AdalineGD` 类，改名 `AdalineGDLoop`（**示例代码**，基于源码 L369-L372 修改）；
   - 把 L372 的向量化更新换成注释中的循环版本：

   ```python
   # 用源码中注释掉的逐权重写法替换向量化行
   for w_j in range(self.w_.shape[0]):
       self.w_[w_j] += self.eta * (2.0 * (X[:, w_j] * errors)).mean()
   self.b_ += self.eta * 2.0 * errors.mean()
   ```

   - 用同一组参数分别训练两个版本并对比：

   ```python
   a = AdalineGD(n_iter=20, eta=0.0005).fit(X, y)
   b = AdalineGDLoop(n_iter=20, eta=0.0005).fit(X, y)
   print(np.allclose(a.losses_, b.losses_))   # 预期 True
   print(np.allclose(a.w_, b.w_))             # 预期 True
   ```

3. **需要观察的现象**：两条 `losses_` 曲线数值重合；最终权重一致（浮点精度内）。
4. **预期结果**：两个 `allclose` 均为 `True`（可能存在极小的浮点舍入差异，`allclose` 的默认容差可覆盖）。同参数下两个版本耗时差异明显——向量化明显更快，**待本地验证**具体时间。

#### 4.2.5 小练习与答案

**练习 1**：更新公式里的系数 `2.0` 从哪里来？如果代价定义为 \( J = \frac{1}{2n}\sum e^2 \)（多除以 2），更新公式会变成什么样？

**答案**：`2.0` 是对 \( e^2 \) 求导得到的 \( 2e \)。若代价写成 \( \frac{1}{2n}\sum e^2 \)，求导后 2 被约掉，更新变为 \( \mathbf{w} \leftarrow \mathbf{w} + \eta\cdot\frac{1}{n}X^\top\mathbf{e} \)，代码里的 `2.0` 相应消失。两种写法只差一个常数因子，可互相吸收进 η。

**练习 2**：为什么更新时要除以 `X.shape[0]`（取均值），而不是直接用误差求和？

**答案**：取均值让代价和梯度的量级与样本数 n 无关：同一套 η 在 100 个样本和 100 万个样本上量级相当。若用求和，n 越大梯度越大，η 必须跟着缩小，超参数无法复用。

**练习 3**：感知机 `fit` 一轮最多更新多少次权重？`AdalineGD` 一轮更新多少次？

**答案**：感知机在每个分错（或边界）的样本上都更新，一轮最多 n 次（本数据 n=100）；`AdalineGD` 每轮汇总全批梯度后只更新一次。这也解释了为什么 Adaline 的 `losses_` 每轮恰好追加一个值。

### 4.3 两个学习率下的代价曲线对比：发散与收敛

#### 4.3.1 概念说明

梯度下降唯一的「旋钮」就是步长 η。把它想成下山时的步幅：

- **η 太大**：一步跨过谷底跳到对面更高的山坡，下一步跨回来时蹦得更高，越荡越高——代价不降反升，最终溢出浮点范围，这就是**发散（divergence）**。
- **η 太小**：每轮只挪一丁点，方向虽对但进展极慢，有限轮数内到不了谷底——**收敛过慢**。

对二次凸代价，梯度下降收敛的充要条件是 \( 0 < \eta < 2/\lambda_{\max} \)，其中 \( \lambda_{\max} \) 是代价函数 Hessian 矩阵的最大特征值（本例中 Hessian 为 \( \frac{2}{n}\tilde{X}^\top\tilde{X} \)，\( \tilde{X} \) 是 X 加一列全 1）。鸢尾花两个特征未经缩放（萼片长约 4~8、花瓣长约 1~7），不同方向的山坡陡峭程度差别很大，单个 η 很难同时满足所有方向——这正是书中实验要演示的痛点，也是下一讲「特征标准化」的引子。

#### 4.3.2 核心流程

书中实验在同一份未标准化数据 `X` 上跑两个极端学习率：

```text
eta = 0.1:    n_iter = 15 轮 → 记录 losses_ → 纵轴取 log10 绘制   → 观察「上升」曲线（发散）
eta = 0.0001: n_iter = 15 轮 → 记录 losses_ → 纵轴用原始值绘制   → 观察「缓慢下降」曲线（过慢）
```

为什么左图取 `log10` 而右图不取？发散时代价按**数量级**增长（可能从 10² 涨到 10⁷ 以上），线性坐标下曲线会瞬间冲出画面；取对数后纵轴压缩为数量级刻度，仍能看出「单调上升」这一发散特征。而 η=0.0001 的代价本身数值不大、变化平缓，直接线性绘制即可读。

#### 4.3.3 源码精读

实验代码：[ch02/ch02.py:L393-L408](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L393-L408) —— 创建 1×2 的子图画布，左右各训练一个模型并绘制代价曲线：

```python
fig, ax = plt.subplots(nrows=1, ncols=2, figsize=(10, 4))

ada1 = AdalineGD(n_iter=15, eta=0.1).fit(X, y)
ax[0].plot(range(1, len(ada1.losses_) + 1), np.log10(ada1.losses_), marker='o')
ax[0].set_xlabel('Epochs')
ax[0].set_ylabel('log(Mean squared error)')
ax[0].set_title('Adaline - Learning rate 0.1')

ada2 = AdalineGD(n_iter=15, eta=0.0001).fit(X, y)
ax[1].plot(range(1, len(ada2.losses_) + 1), ada2.losses_, marker='o')
ax[1].set_xlabel('Epochs')
ax[1].set_ylabel('Mean squared error')
ax[1].set_title('Adaline - Learning rate 0.0001')
```

四个要点：

- 左图模型：[ch02/ch02.py:L395-L399](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L395-L399) —— η=0.1 在未缩放数据上过大，代价逐轮上升；纵轴是 `np.log10(ada1.losses_)`。
- 右图模型：[ch02/ch02.py:L401-L405](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L401-L405) —— η=0.0001 足够小，代价确实在降，但 15 轮后离谷底还很远。
- 训练用的 `X`、`y` 来自：[ch02/ch02.py:L201-L206](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L201-L206) —— 前 100 个样本的萼片长、花瓣长两列，以及 setosa=0 / versicolor=1 的整数标签，全程未做任何缩放。
- 转机预告：[ch02/ch02.py:L425-L434](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L425-L434) —— 紧接着书中把两列特征标准化后，η=0.5 这样「大」的学习率也能平滑收敛。为什么标准化有如此魔力，是 u2-l3 的主题。

#### 4.3.4 代码实践

1. **实践目标**：亲眼看到发散与收敛过慢两种形态，理解「η 是最敏感的超参数」。
2. **操作步骤**：按第 5 节综合实践运行完整脚本（它包含本模块实验），或先只跑左图两行：`AdalineGD(n_iter=15, eta=0.1).fit(X, y)` 并绘制 `np.log10(losses_)`。
3. **需要观察的现象**：
   - η=0.1：曲线逐轮**上升**；运行时可能伴随 `RuntimeWarning: overflow encountered ...`（误差平方超出 float64 上限）——这个警告本身就是发散最直接的证据，**待本地验证**是否触发；
   - η=0.0001：曲线单调下降但 15 轮后数值仍然很大、远未变平。
4. **预期结果**：左图呈上升的 log 代价曲线，右图呈缓慢下降的 MSE 曲线，与书图 2-11 一致；具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 η=0.1 的图纵轴用 `log10`，而 η=0.0001 的图不用？

**答案**：发散时代价跨越多个数量级（可从几十涨到 10⁷ 以上），线性坐标会把曲线压成「贴地后瞬间冲天」不可读；log10 把数量级变成线性刻度，上升形态清晰可见。η=0.0001 的代价数值小且变化平缓，线性坐标即可。

**练习 2**：η=0.0001 的模型「收敛」了吗？

**答案**：没有。代价在下降只说明方向正确（负梯度方向），但 15 轮后代价仍远高于最小值、曲线远未进入平台期，属于「收敛过慢」。判断收敛应看曲线是否趋平并接近某个下界，而不是只看「在下降」。

**练习 3**：如果运行 η=0.1 时 NumPy 报出 `overflow` 警告甚至出现 `inf`/`nan`，这说明什么？该怎么处理？

**答案**：说明权重和误差已在逐轮放大中超出浮点表示范围，是发散的极端形态。处理办法是减小 η 重跑；工程上也可以在训练循环里检查 `losses_` 是否非有限（`np.isfinite`）并提前终止。

## 5. 综合实践

**任务：复现书中的双学习率实验，并亲手在两个极端之间搜出一个能平稳收敛的学习率。**

### 5.1 实践目标

把本讲三个模块串联起来：定义 AdalineGD → 观察两个极端学习率的代价曲线 → 用网格搜索找到一个 η 使代价单调下降且快速趋平，体会「学习率选择之难」。

### 5.2 操作步骤

先运行数据准备与类定义（来自 [ch02/ch02.py:L201-L206](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L201-L206) 与 [ch02/ch02.py:L307-L388](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L307-L388)），然后执行下面的**示例代码**（组装自仓库代码并加入搜索循环）：

```python
# 示例代码：在 ch02 目录下运行，或把 iris.data 路径改为你环境的实际路径
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

# 1. 数据准备（与书中 L201-L206 相同）
df = pd.read_csv('iris.data', header=None, encoding='utf-8')
y = df.iloc[0:100, 4].values
y = np.where(y == 'Iris-setosa', 0, 1)
X = df.iloc[0:100, [0, 2]].values

# 2. 复现书中两个极端学习率（L393-L408）
fig, ax = plt.subplots(nrows=1, ncols=2, figsize=(10, 4))

ada1 = AdalineGD(n_iter=15, eta=0.1).fit(X, y)
ax[0].plot(range(1, len(ada1.losses_) + 1), np.log10(ada1.losses_), marker='o')
ax[0].set_xlabel('Epochs'); ax[0].set_ylabel('log(MSE)')
ax[0].set_title('Adaline - Learning rate 0.1')

ada2 = AdalineGD(n_iter=15, eta=0.0001).fit(X, y)
ax[1].plot(range(1, len(ada2.losses_) + 1), ada2.losses_, marker='o')
ax[1].set_xlabel('Epochs'); ax[1].set_ylabel('MSE')
ax[1].set_title('Adaline - Learning rate 0.0001')
plt.show()

# 3. 在两个极端之间搜索合适的学习率
for eta in [0.0002, 0.0005, 0.001, 0.005]:
    ada = AdalineGD(n_iter=30, eta=eta).fit(X, y)
    plt.plot(range(1, len(ada.losses_) + 1), ada.losses_,
             marker='o', label=f'eta={eta}')
plt.xlabel('Epochs'); plt.ylabel('Mean squared error')
plt.legend(); plt.show()
```

### 5.3 需要观察的现象

- 第一幅图的左子图（log 坐标）曲线上升、右子图曲线下降极缓；
- 第二幅图中：偏小的 η 曲线下降但慢，偏大的 η 可能震荡或直接上扬；某个中间值应呈现「快速下降后趋平」的形态。

### 5.4 预期结果

- 左右两个极端的行为与书图 2-11 一致（η=0.1 发散、η=0.0001 过慢），具体数值与是否触发 overflow 警告**待本地验证**；
- 未标准化数据上「能收敛」的 η 大概率落在两个极端之间的较小区间内，且比标准化后可用的 η=0.5（L433）小几个数量级——这正是「特征缩放的必要性」的实验证据，哪个候选值最优**待本地验证**。

### 5.5 思考题（选做）

用 4.3 节的 \( \eta < 2/\lambda_{\max} \) 条件解释：为什么萼片长度（均值约 5.8）这一维的「山坡」比标准化后（均值 0、方差 1）陡得多，导致可用 η 被压得极小？

## 6. 本讲小结

- Adaline 用**恒等激活**保持输出连续，把「比较」从阶跃之后挪到之前，从而获得光滑可导的代价函数；阶跃只在最终 `predict` 时使用（阈值 0.5，即两类标签 0/1 的中点）。
- 代价函数是均方误差 \( J = \frac{1}{n}\sum(y-o)^2 \)，源码中以 `loss = (errors**2).mean()` 一行计算并记入 `losses_`，本仓库版本没有独立的 `cost` 方法。
- `fit` 实现**整批梯度下降**：每轮用全部样本计算一次梯度并更新一次，核心一行 `self.w_ += self.eta * 2.0 * X.T.dot(errors) / X.shape[0]`；系数 2 来自平方求导，除以 n 把 SSE 换算成 MSE。
- 源码中注释掉的逐权重循环与向量化更新在数学上完全等价，向量化版本把循环交给 NumPy，显著更快。
- η=0.1 在未缩放数据上发散（代价按数量级增长，故纵轴取 log10）；η=0.0001 收敛过慢。学习率是梯度下降最敏感的超参数。
- 对二次凸代价，收敛要求 \( 0 < \eta < 2/\lambda_{\max} \)；特征未缩放时各方向曲率差异巨大，单一 η 难以兼顾——标准化是解药，也是下一讲主题。

## 7. 下一步学习建议

下一讲（u2-l3）学习 `StandardScaler` 标准化与 `AdalineSGD` 随机梯度下降：先读 [ch02/ch02.py:L425-L434](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L425-L434)，看标准化后 η=0.5 为何能平滑收敛；再逐行读 [ch02/ch02.py:L459-L565](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L459-L565) 的 `AdalineSGD`，重点比较 `_update_weights`（逐样本更新）与本讲的全批更新，以及 `partial_fit` 如何支持在线学习。往后看，本讲的「连续激活 + 可导代价 + 梯度下降」三件套将在第 3 章（[ch03/ch03.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py) 的 `LogisticRegressionGD`）换成 sigmoid 激活，在第 11 章扩展为多层网络的反向传播——值得时常回看本讲的梯度推导。
