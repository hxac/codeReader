# u2-l5 从零实现反向传播：精读 mlp-fromscratch__sigmoid-mse

## 1. 本讲目标

学完本讲，你应该能够：

1. **不借助 autograd**，手工写出一个单隐藏层 MLP 的前向传播与反向传播。
2. 把链式法则落到矩阵运算上：说清反向传播「四要素」——损失对输出的导数、激活函数的局部导数、delta（误差项）、以及由 delta 拼出的权重梯度。
3. 用 `torch.autograd.grad` 或数值差分**校验**自己手写的梯度是否正确。
4. 体会 sigmoid + MSE 与 softmax + 交叉熵在梯度上的本质差别，理解为什么现代分类网络几乎都用后者。

前几讲我们反复使用「forward → loss → zero_grad → backward → step」五步范式，其中 `loss.backward()` 一直是个黑盒。本讲把这个黑盒亲手打开：`mlp-fromscratch__sigmoid-mse.ipynb` 用纯 tensor 实现了整个网络，梯度全靠手推，一行 autograd 都没用。

## 2. 前置知识

### 2.1 你应该已经了解的内容（来自前几讲）

- **五步训练循环**（u2-l1、u2-l2）：`forward → loss → zero_grad → backward → step`。
- **自定义 nn.Module**（u2-l3）：`__init__` 建层、`forward` 描述计算、`model(x)` 触发前向。
- **MNIST 数据管线**（u2-l2）：`datasets.MNIST` + `DataLoader`，`ToTensor()` 把像素压到 [0,1]，输入需 `view(-1, 28*28)` 展平成 784 维向量。

### 2.2 链式法则：一行数学 + 一个直觉

如果 \( y = f(g(x)) \)，那么：

\[
\frac{dy}{dx} = \frac{df}{dg} \cdot \frac{dg}{dx}
\]

**直觉**：想知道「拧动旋钮 \( x\) 一点点，结果 \(y\) 变多少」，就沿路径把每一段的「变化率」连乘起来。神经网络是多层复合函数，所以梯度就是若干个**局部导数**的连乘——这正是反向传播的全部秘密。

### 2.3 矩阵求导你只需要两个形状

本讲的反向传播会反复出现两种矩阵运算，先记住结论（记号：设 \(A\)、\(B\) 为两个形状匹配的矩阵）：

| 场景 | 公式 | 直觉 |
|---|---|---|
| 线性层 \(Z = XW^\top + b\) 中求 \( \partial L/\partial W \) | \( \partial L/\partial W = \Delta^\top X \)，其中 \( \Delta = \partial L/\partial Z \) | 「上游误差 × 本层输入」，转置只为让形状对上 |
| 求 \( \partial L/\partial b \) | \( \partial L/\partial b = \sum_{\text{batch}} \Delta \)（按样本维求和） | 偏置对每个样本都起作用，所以误差按样本累加 |

逐元素激活函数 \( a = \sigma(z) \) 则更简单：\( \partial L/\partial z = \partial L/\partial a \odot \sigma'(z) \)，其中 \( \odot \) 是逐元素相乘（Hadamard 积）。

### 2.4 sigmoid 的导数与「饱和」

\[
\sigma(z) = \frac{1}{1+e^{-z}}, \qquad \sigma'(z) = \sigma(z)\,(1-\sigma(z))
\]

注意导数用**输出**就能算出来（不用存输入 \(z\)），这是本讲源码里 `a_2 * (1. - a_2)` 的来历。当 \(|z|\) 很大时 \( \sigma(z) \to 0 \) 或 \( 1 \)，导数趋近 0——称为**饱和**。饱和会让梯度消失，这是 sigmoid 用于输出层 + MSE 时训练慢的根源，4.2 节详细展开。

### 2.5 one-hot 编码

MSE 损失要求「预测的概率分布」和「标签」逐元素相减，所以整数标签 `3` 要变成向量 `[0,0,0,1,0,0,0,0,0,0]`。源码用 `scatter_` 完成这件事。

## 3. 本讲源码地图

本讲只有一个主角 notebook（仓库的行号永久链接指向的是 `.ipynb` 的 **JSON 原文**，GitHub 会以源码形式打开；由于 notebook 每行源码恰好占 JSON 一行，所以「L186」既指 JSON 第 186 行，也指该 cell 源码中的对应行）：

| 文件 | 角色 |
|---|---|
| [pytorch_ipynb/mlp/mlp-fromscratch__sigmoid-mse.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-fromscratch__sigmoid-mse.ipynb) | 主文件：从零实现的前向 + 反向 + 训练 + 评估 |
| `pytorch_ipynb/mlp/mlp-basic.ipynb` | 对照文件（u2-l3 精读过）：同一任务的 nn.Module + autograd 版本，本讲末尾做行为对比 |

主 notebook 各段在 JSON 中的行号区间（便于跳转）：

| 区段 | JSON 行号区间 | 内容 |
|---|---|---|
| 设置 | L108–L114 | `RANDOM_SEED=1`、`BATCH_SIZE=100`、`NUM_EPOCHS=50`、MNIST 加载 |
| 模型类 | L164–L179 | `MultilayerPerceptron.__init__`：手工创建权重张量 |
| 前向传播 | L181–L194 | `forward`：两层 sigmoid |
| 反向传播 | L196–L253 | `backward`：本讲核心 |
| 工具函数 | L273–L296 | `to_onehot`、`loss_func`、`compute_mse` |
| 训练循环 | L299–L338 | `train`：forward → backward → 手工更新 |
| 训练调用 | L1008–L1016 | 实例化 784–50–10 网络并训练 50 epoch |
| 评估 | L1083–L1095 | `compute_accuracy`：训练 94.72% / 测试 94.49% |

（L339 到 L1007 之间是训练 cell 保存的 50 epoch 日志输出，不是源码，所以后面 cell 的行号被「撑大」了。）

## 4. 核心概念与源码讲解

本讲的三个最小模块：**4.1 手工前向传播**、**4.2 逐层反向传播公式**、**4.3 梯度数值校验**。

### 4.1 手工前向传播：不用 nn.Module 的网络

#### 4.1.1 概念说明

u2-l3 里我们写 MLP 时继承 `torch.nn.Module`，层由 `nn.Linear` 创建，参数自带 `requires_grad=True`，梯度由 autograd 记录、优化器负责更新。这一切便利在本讲全部撤掉：

- 权重就是**普通 tensor**（`torch.zeros(...).normal_(0, 0.1)`），`requires_grad` 默认为 `False`，autograd 完全不参与；
- 没有 `nn.Linear`、没有优化器对象，参数更新是裸写的 `weight -= lr * grad`；
- `forward` 与 `backward` 都是**普通方法**，不是 PyTorch 钩子协议。

这样做只有一个目的：**教学**。当每一行梯度都由你亲手算出，`loss.backward()` 就不再是魔法。

#### 4.1.2 核心流程

网络结构是 784 → 50 → 10（notebook 中 `num_features=28*28, num_hidden=50, num_classes=10`），前向传播只有四步：

\[
z_1 = X W_1^\top + b_1, \qquad a_1 = \sigma(z_1)
\]
\[
z_2 = a_1 W_o^\top + b_o, \qquad a_2 = \sigma(z_2)
\]

各张量形状（设 batch 大小为 \(N\)）：

| 张量 | 形状 | 含义 |
|---|---|---|
| `x` | \([N, 784]\) | 展平后的图像 batch |
| `weight_1` | \([50, 784]\) | 隐藏层权重 |
| `z_1`, `a_1` | \([N, 50]\) | 隐藏层净激活 / 激活后输出 |
| `weight_o` | \([10, 50]\) | 输出层权重 |
| `z_2`, `a_2` | \([N, 10]\) | 输出层净激活 / 每类「概率」 |

注意权重按 PyTorch 惯例存成 `[out, in]`，所以矩阵乘法要转置（`x @ weight_1.t()`）。`forward` 把 `a_1` 和 `a_2` 都返回——因为反向传播时需要用激活值重算导数（回忆 2.4 节：sigmoid 的导数只依赖输出）。

#### 4.1.3 源码精读

**参数创建**（[pytorch_ipynb/mlp/mlp-fromscratch__sigmoid-mse.ipynb:L164-L179](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-fromscratch__sigmoid-mse.ipynb#L164-L179)）：类不继承任何父类，`__init__` 里先用 `torch.zeros` 造出正确形状的全零张量，再用 `.normal_(0.0, 0.1)` **原地**填充均值 0、标准差 0.1 的正态噪声。下划线结尾的方法（`normal_`）都是原地操作。这里的小随机初始化是为了打破对称性（全零初始化的同层神经元梯度完全相同，永远学成一样）。

```python
self.weight_1 = torch.zeros(num_hidden, num_features,
                            dtype=torch.float).normal_(0.0, 0.1)
self.bias_1 = torch.zeros(num_hidden, dtype=torch.float)
```

**前向传播**（[pytorch_ipynb/mlp/mlp-fromscratch__sigmoid-mse.ipynb:L181-L194](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-fromscratch__sigmoid-mse.ipynb#L181-L194)）：`torch.mm` 是二维矩阵乘法；`torch.sigmoid` 作用在张量上是逐元素求值。源码注释仔细标了每一步的输入输出维度，最后返回 `(a_1, a_2)`：

```python
z_1 = torch.mm(x, self.weight_1.t()) + self.bias_1
a_1 = torch.sigmoid(z_1)
z_2 = torch.mm(a_1, self.weight_o.t()) + self.bias_o
a_2 = torch.sigmoid(z_2)
return a_1, a_2
```

两个读码发现：

1. `__init__` 第一行的 `super(MultilayerPerceptron, self).__init__()` 看起来像 nn.Module 的写法，但这个类**没有**继承任何东西（隐式继承 `object`），这行调用是无害的「习惯性残留」。
2. 输出层也用 sigmoid 而不是 softmax：10 个输出各自过 sigmoid、各自落在 (0,1)，**彼此独立，加起来不等于 1**。它不是严格的概率分布，却足以支撑 MSE 训练与 argmax 预测——这是早期神经网络教材（Rosenblatt 之后的连接主义时代）的经典配置，也是它与现代版 `mlp-basic.ipynb` 的关键差异之一。

#### 4.1.4 代码实践

1. **实践目标**：确认手工前向传播的形状流转与数值范围。
2. **操作步骤**：运行 notebook 的 Settings/Model 两个 cell（L108–L114 与 L164–L194），然后新开一个 cell 运行下面的验证脚本（**示例代码**，非仓库原有）：

```python
torch.manual_seed(RANDOM_SEED)
model = MultilayerPerceptron(num_features=28*28, num_hidden=50, num_classes=10)

for features, targets in train_loader:
    break
x = features.view(-1, 28*28)
a_1, a_2 = model.forward(x)

print('x  :', x.shape)        # 期望 torch.Size([100, 784])
print('a_1:', a_1.shape)      # 期望 torch.Size([100, 50])
print('a_2:', a_2.shape)      # 期望 torch.Size([100, 10])
print('a_2 范围:', a_2.min().item(), a_2.max().item())   # 应落在 (0, 1)
print('a_2 每行和（不等于 1 是正常的）:', a_2.sum(dim=1)[:3])
print('参数量:', model.weight_1.numel() + model.bias_1.numel()
      + model.weight_o.numel() + model.bias_o.numel())   # 期望 39760
```

3. **需要观察的现象**：`a_2` 的每个元素都在 (0,1)；每行求和既不是 1 也不恒定。
4. **预期结果**：参数量为 \(50 \times 784 + 50 + 10 \times 50 + 10 = 39760\)。若你的输出与此不符，先检查是否忘了 `view(-1, 28*28)`。具体数值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：把 `num_hidden` 从 50 改成 100，参数量变为多少？
**答案**：\(100 \times 784 + 100 + 10 \times 100 + 10 = 79510\)。两层网络的参数量对隐藏宽度近似线性（权重项）增长。

**练习 2**：为什么 `forward` 要返回 `a_1` 而不是 `z_1`？如果只返回 `z_1` 会怎样？
**答案**：因为 sigmoid 的导数公式 \( \sigma'(z) = a(1-a) \) 只需要激活值 `a_1` 就能重算；若返回 `z_1`，`backward` 里需要再算一次 `torch.sigmoid(z_1)` 才能得到导数——数学上完全可行，只是多一次计算。返回 `a_1` 是「缓存已算好的值」的小优化。

**练习 3**：`torch.mm(x, self.weight_1.t())` 中，若忘记 `.t()` 会发生什么？
**答案**：形状不匹配（[100, 784] 与 [50, 784] 无法相乘），`torch.mm` 直接抛出 runtime 错误。矩阵乘法的形状检查是最快的「维度对不对」自检。

### 4.2 逐层反向传播公式：链式法则的矩阵实现

#### 4.2.1 概念说明

**反向传播（backpropagation）不是一种训练算法，而是高效计算梯度的算法**。训练算法是梯度下降；反向传播回答的问题是：损失 \(L\) 对**每一个**权重 \(w\) 的偏导数是多少？

朴素做法是对每个权重做数值差分（4.3 节会做，但只用于校验）：每个权重要前向传播两次，39760 个参数就要约 8 万次前向。反向传播的洞察是：**从输出层往输入层逐层回推，一次反向扫过就能同时算出所有参数的梯度**，计算量与一次前向传播同量级。

每一层的反向传播都由同样的**四要素**组成（这就是本讲反复出现的结构）：

1. **上游梯度** \( \partial L/\partial a \)：损失对本层输出的导数（输出层直接由损失函数给出，隐藏层由上一层回传）；
2. **激活局部导数** \( \sigma'(z) \)：与上游梯度逐元素相乘得到 **delta**（误差项）\( \delta = \partial L/\partial a \odot \sigma'(z) \)；
3. **权重梯度** \( \partial L/\partial W = \delta^\top \cdot (\text{本层输入}) \)、偏置梯度 \( \partial L/\partial b = \sum_N \delta \)（2.3 节的两个形状公式）；
4. **继续下传**：把 \( \delta \) 乘上本层权重（不转置）传给下一层（即更靠近输入的层），作为它的上游梯度。

#### 4.2.2 核心流程

先写清楚损失。notebook 采用的 MSE（对 batch 平均）：

\[
L = \frac{1}{N} \sum_{i=1}^{N} \sum_{c=1}^{C} \left(a_2^{(i,c)} - y_{\text{onehot}}^{(i,c)}\right)^2
\]

**输出层**（要素 1→3）：

\[
\frac{\partial L}{\partial a_2} = \frac{2}{N}(a_2 - y_{\text{onehot}}),
\qquad
\frac{\partial a_2}{\partial z_2} = a_2 \odot (1 - a_2)
\]
\[
\delta_{\text{out}} = \frac{\partial L}{\partial a_2} \odot a_2(1-a_2),
\qquad
\frac{\partial L}{\partial W_o} = \delta_{\text{out}}^\top a_1,
\qquad
\frac{\partial L}{\partial b_o} = \sum_N \delta_{\text{out}}
\]

**隐藏层**（要素 4，然后重复要素 2→3）：

\[
\frac{\partial L}{\partial a_1} = \delta_{\text{out}} W_o,
\qquad
\delta_1 = \frac{\partial L}{\partial a_1} \odot a_1(1-a_1),
\qquad
\frac{\partial L}{\partial W_1} = \delta_1^\top x,
\qquad
\frac{\partial L}{\partial b_1} = \sum_N \delta_1
\]

用伪代码总结整个训练步（对照源码 `train` 函数）：

```text
for each batch (x, y):
    a_1, a_2 = forward(x)                     # 前向
    四个梯度 = backward(x, a_1, a_2, y)         # 反向（链式法则）
    W  -= lr * dL/dW    （对四个参数各做一次）  # 手工梯度下降
```

对比五步范式：`zero_grad` 和 `step` 消失了——没有 autograd 自然不需要清梯度，没有优化器对象自然手工写更新。五步塌缩成三步。

**为什么 sigmoid + MSE 训练慢**：把输出层两个因子相乘，得

\[
\delta_{\text{out}} = \frac{2}{N}(a_2 - y) \odot a_2(1-a_2)
\]

当网络「自信地犯错」（\(a_2\) 饱和到 0 或 1 且方向错误）时，\(a_2(1-a_2) \to 0\)，误差再大梯度也趋近 0——学习停滞。而 softmax + 交叉熵（u2-l2）的输出层梯度是干净的 \(p - y\)，不含会消失的因子。这是 1990 年代到 2010 年代分类网络从 sigmoid+MSE 迁移到 softmax+CE 的核心原因之一。

#### 4.2.3 源码精读

**one-hot 编码**（[pytorch_ipynb/mlp/mlp-fromscratch__sigmoid-mse.ipynb:L201-L205](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-fromscratch__sigmoid-mse.ipynb#L201-L205)）：先造 \([N, 10]\) 的全零 `FloatTensor`，`scatter_(1, index, 1)` 在第 1 维（类维度）上，把每个样本 `index` 位置写成 1。`scatter_` 要求索引是 long 类型，所以有 `.long()`。这里在 `backward` 内部现场做 one-hot，训练循环里另有一个功能相同的 `to_onehot`（L273–L277）。

```python
y_onehot = torch.FloatTensor(y.size(0), self.num_classes)
y_onehot.zero_()
y_onehot.scatter_(1, y.view(-1, 1).long(), 1)
```

**输出层四要素**（[pytorch_ipynb/mlp/mlp-fromscratch__sigmoid-mse.ipynb:L214-L230](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-fromscratch__sigmoid-mse.ipynb#L214-L230)）：三行代码与公式逐项对应——`dloss_da2` 是要素 1（上游梯度，即 MSE 对输出的导数），`da2_dz2` 是要素 2（sigmoid 局部导数），两者逐元素相乘得 `delta_out`，再用要素 3 的两个形状公式拼出权重与偏置梯度。源码变量名 `dz2__dw_out = a_1`（读作 d z₂ / d w_out）就是链式法则链条的「本层输入」一环：

```python
dloss_da2 = 2.*(a_2 - y_onehot) / y.size(0)     # ∂L/∂a2
da2_dz2 = a_2 * (1. - a_2)                       # sigmoid 导数
delta_out = dloss_da2 * da2_dz2                  # δ_out = ∂L/∂z2
dloss__dw_out = torch.mm(delta_out.t(), dz2__dw_out)   # δᵀ·a1
dloss__db_out = torch.sum(delta_out, dim=0)            # 按样本求和
```

**隐藏层四要素**（[pytorch_ipynb/mlp/mlp-fromscratch__sigmoid-mse.ipynb:L238-L251](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-fromscratch__sigmoid-mse.ipynb#L238-L251)）：要素 4 是 `dloss_a1 = torch.mm(delta_out, dz2__a1)`——注意这里乘的是**不转置**的 `weight_o`，与 `forward` 里 `a_1 @ weight_o.t()` 方向正好相反（前向投影到输出空间，反向投影回隐藏空间）。随后同样是「乘 sigmoid 导数、转置内积、按样本求和」：

```python
dz2__a1 = self.weight_o                          # 要素 4 的桥梁
dloss_a1 = torch.mm(delta_out, dz2__a1)          # ∂L/∂a1 = δ_out · W_o
da1__dz1 = a_1 * (1. - a_1)                      # sigmoid 导数
dloss_dw1 = torch.mm((dloss_a1 * da1__dz1).t(), dz1__dw1)  # δ₁ᵀ·x
dloss_db1 = torch.sum((dloss_a1 * da1__dz1), dim=0)
```

`backward` 最后（L253）按固定顺序返回四个梯度，调用方按相同顺序解包。

**训练循环**（[pytorch_ipynb/mlp/mlp-fromscratch__sigmoid-mse.ipynb:L299-L338](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-fromscratch__sigmoid-mse.ipynb#L299-L338)）：内层三段注释 `#### Compute outputs ####` / `#### Compute gradients ####` / `#### Update weights ####`（L311–L322）就是伪代码的逐字落地——先 `model.forward`，再 `model.backward` 拿四个梯度，然后四行原地减法完成梯度下降（`model.weight_1 -= learning_rate * dloss_dw1` 等）。梯度以函数返回值传递，而不是挂在参数的 `.grad` 属性上，因此不存在梯度累加问题、也就不需要 `zero_grad`。

三个值得玩味的读码发现（都是真实的仓库细节，不是缺陷指控，而是「notebook 式简化」）：

1. `train` 的形参叫 `data_loader`，但循环里迭代的是**全局变量** `train_loader`（L307）。当前恰好总是传 `train_loader` 进来所以结果正确；若有人传 `test_loader`，训练数据并不会变。读开源 notebook 时对「形参 vs 全局」要多留个心眼。
2. `compute_mse`（L284–L296）内部引用的是全局 `model.num_classes` 而非形参 `net` 的属性——同类问题。
3. **梯度与损失函数差一个常数因子**：`loss_func`（L280–L281）是先按样本求平均再按 10 个类求平均，而 `backward` 里 `dloss_da2 = 2.*(a_2 - y_onehot) / y.size(0)` 只除以了 \(N\)。也就是说手工梯度精确对应的是 \( \frac{1}{N}\sum\sum(a_2-y)^2 \)（对类求和）而非 `loss_func` 本身，两者相差常数倍 \(1/C = 1/10\)。常数因子会被学习率吸收（等效于 lr 放大 10 倍），所以训练照常工作，但做 4.3 节的梯度对账时**必须**用「对类求和」的损失定义，否则会对不上。

**训练调用与结果**（[pytorch_ipynb/mlp/mlp-fromscratch__sigmoid-mse.ipynb:L1008-L1016](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-fromscratch__sigmoid-mse.ipynb#L1008-L1016)）：固定种子后实例化 784–50–10 网络，`learning_rate=0.1` 训练 50 epoch。notebook 保存的最终结果（[L1083-L1095](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-fromscratch__sigmoid-mse.ipynb#L1083-L1095)）：训练准确率 **94.72%**，测试准确率 **94.49%**。对比 u2-l3 的 `mlp-basic.ipynb`（784-128-256-10，ReLU + 交叉熵，97.54%）低了约 3 个百分点——结构更小、损失与激活更老派、梯度更易饱和，多因素叠加，4.2.2 节的解释可以在这里落地。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看到学习率与梯度稳定性之间的张力，验证「手工梯度确实在驱动学习」。
2. **操作步骤**：
   - 先原样运行训练 cell（L1008–L1016），记录 50 epoch 后的 Train MSE 与测试准确率；
   - 把 `learning_rate` 分别改为 `0.01` 和 `1.0` 各重跑一次（每次重新执行 L1008 的 `torch.manual_seed(RANDOM_SEED)` 再重建模型，保证同起点）；
   - 观察打印的 `Cost` 序列（每 50 个 batch 打一次）。
3. **需要观察的现象**：lr=0.01 时损失下降明显变慢、50 epoch 后准确率更低；lr=1.0 时损失可能在初期震荡甚至变大（对 2.2 节「学习率过大梯度下降发散」的直接体感）。
4. **预期结果**：以 notebook 保存的 lr=0.1 结果（测试 94.49%）为基准做对比表。具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `dloss__db_out` 是 `torch.sum(delta_out, dim=0)` 而不是 `torch.mean`？
**答案**：偏置 \(b_o\) 与每个样本的净激活都是一次加法关系，\(\partial L/\partial b_o = \sum_i \partial L/\partial z_2^{(i)}\)，梯度按 batch 维**求和**。对 \(N\) 取平均的步骤已经包含在 `dloss_da2` 的 `2. / y.size(0)` 因子里（那对应损失定义中的 \(1/N\)）；权重与偏置梯度必须用同一套口径，否则两者尺度不一致。若把 sum 换成 mean，等价于把偏置的学习率缩小 \(N\) 倍。

**练习 2**：`backward` 里隐藏层的上游梯度为什么是 `delta_out @ weight_o`，而不是 `delta_out @ weight_o.t()`？
**答案**：形状推一下即可。`delta_out` 是 \([N, 10]\)，`weight_o` 是 \([10, 50]\)，相乘得 \([N, 50]\)，与 `a_1` 同形，这正是 \( \partial L/\partial a_1 \) 该有的形状。前向是 \(a_1 W_o^\top\)（[50]→[10] 方向），反向求 \( \partial L/\partial a_1 = \delta_{\text{out}} W_o\)（[10]→[50] 方向），两次转置恰好互为逆操作。记住口诀：**前向乘转置，反向乘原阵**。

**练习 3**：如果把这个网络加深一层（784–50–50–10），`backward` 需要新增哪些计算？
**答案**：需要为第二个隐藏层重复一次四要素：\( \partial L/\partial a_2^{(1)} = \delta_{\text{out}} W_o \)，乘新的 sigmoid 导数得 \( \delta_2 \)，再由 \( \delta_2 \) 拼出 \( \partial L/\partial W_2, \partial L/\partial b_2 \)，并继续把 \( \delta_2 W_2 \) 下传给第一个隐藏层。每加一层就是「delta → 权重梯度 → delta 往前传」循环的一次重复——这正是反向传播「逐层」的含义，也是它对任意深度都适用的原因。

### 4.3 梯度数值校验：让手工梯度与 autograd 对账

#### 4.3.1 概念说明

手推梯度极容易错：转置放错、维度对错、常数因子漏掉，结果往往是「网络能跑、就是不好」——错误梯度不会报错，只是让学习变慢或失效。工程上验证梯度的标准做法有两种：

1. **数值梯度校验（numerical gradient check）**：用中心差分近似导数，与手工梯度逐元素比对。这是「定义层面的真值」，不依赖任何框架。
2. **autograd 对账**：把同一网络用 `requires_grad=True` 的张量重写一遍，让 PyTorch 自动微分算梯度，与手工梯度比对。这校验的是「我的推导 vs PyTorch 的实现」。

要说明：**主 notebook 并没有包含校验代码**——它直接用手工梯度训练到底。校验脚本需要我们自己写（本节示例代码），这也正是本讲的实践任务。

#### 4.3.2 核心流程

数值梯度的定义是导数的极限形式，中心差分比单侧差分精度高一阶：

\[
\frac{\partial L}{\partial \theta_j} \approx \frac{L(\theta + \varepsilon e_j) - L(\theta - \varepsilon e_j)}{2\varepsilon}, \qquad \varepsilon \approx 10^{-4} \sim 10^{-5}
\]

其中 \(e_j\) 是第 \(j\) 个标准基向量（只扰动一个参数）。校验流程：

```text
取一个小网络（如 784-3-10）与一个固定 batch
1. 手工 backward 得到 grad_manual
2. 对每个参数 θ_j：
   L_plus  = forward_loss(θ_j + ε)
   L_minus = forward_loss(θ_j - ε)
   grad_num[j] = (L_plus - L_minus) / (2ε)
3. 比较 grad_manual 与 grad_num 的相对误差
```

两个实务要点：

- **float32 精度有限**：\(\varepsilon\) 太小（如 1e-8）时 \(L(\theta+\varepsilon)-L(\theta-\varepsilon)\) 会淹没在浮点误差里；太大（如 1e-2）则差分本身不准。float32 下 1e-4 附近是常用起点，必要时可用 `torch.float64` 让误差更干净。
- **相对误差**比绝对误差更有意义：\(\frac{\|g_m - g_n\|}{\|g_m\| + \|g_n\|}\)。float32 下 1e-2 ~ 1e-3 量级通常即可认为通过，float64 可到 1e-6 以下。

#### 4.3.3 源码精读

本节没有现成仓库代码可引，但校验对象的**定义**要严格对齐主 notebook 的两处源码：

- 手工梯度出自 [L196-L253](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-fromscratch__sigmoid-mse.ipynb#L196-L253) 的 `backward`，其口径是 \( L = \frac{1}{N}\sum_i\sum_c (a_2 - y)^2 \)（4.2.3 节读码发现 3：**对类求和**、只对样本平均）；
- 训练时实际打印的 `loss_func`（[L280-L281](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-fromscratch__sigmoid-mse.ipynb#L280-L281)）额外再对类取了平均。

因此 autograd 对账时的损失必须写成 `(a_2 - y_onehot).pow(2).sum() / y.size(0)`（**不是** `loss_func`），否则手工梯度会恰好是 autograd 梯度的 10 倍——如果你在校验里看到稳定的约 10 倍比例关系，先回来检查口径，而不是怀疑推导。

#### 4.3.4 代码实践

1. **实践目标**：用 autograd 证明（或证伪）`backward` 的四个手工梯度公式全部正确。
2. **操作步骤**：在 notebook 中新建 cell，运行下面的校验脚本（**示例代码**，非仓库原有）。思路：用与手工版**完全相同**的权重张量（复制一份并打开 `requires_grad=True`），复用 notebook 的 `MultilayerPerceptron.forward` 做前向，再用 `torch.autograd.grad` 求自动微分：

```python
# —— 梯度对账：手工 backward vs autograd ——
torch.manual_seed(0)
net = MultilayerPerceptron(num_features=28*28, num_hidden=50, num_classes=10)

# 取一个 batch 并做 one-hot
for features, targets in train_loader:
    break
x = features.view(-1, 28*28)
y_onehot = to_onehot(targets, net.num_classes)

# 1) 手工梯度
a_1, a_2 = net.forward(x)
g_manual = net.backward(x, a_1, a_2, targets)

# 2) autograd 梯度（同一组权重，requires_grad=True 的克隆）
W1 = net.weight_1.clone().requires_grad_()
b1 = net.bias_1.clone().requires_grad_()
Wo = net.weight_o.clone().requires_grad_()
bo = net.bias_o.clone().requires_grad_()

h = torch.sigmoid(x @ W1.t() + b1)
out = torch.sigmoid(h @ Wo.t() + bo)
loss = (out - y_onehot).pow(2).sum() / targets.size(0)   # 口径对齐 backward！
g_auto = torch.autograd.grad(loss, [Wo, bo, W1, b1])     # 顺序与返回值一致

# 3) 逐个比较
for name, gm, ga in zip(['W_o', 'b_o', 'W_1', 'b_1'], g_manual, g_auto):
    rel = (gm - ga).norm() / (gm.norm() + ga.norm())
    print(f'{name}: 相对误差 {rel.item():.3e}  allclose={torch.allclose(gm, ga, atol=1e-6)}')
```

3. **需要观察的现象**：四个参数的相对误差都应在 float32 舍入级别（约 1e-6 ~ 1e-7），`allclose` 全为 `True`。
4. **预期结果**：若某个 `allclose=False` 且比例恰为 10 倍左右，回看 4.3.3 的口径说明；若比例杂乱无章，则推导或转置有误，回到 4.2 逐项排查。具体数值**待本地验证**。

（想再进一步，可对少数几个参数做 4.3.2 的中心差分，把三方——手工、autograd、数值——放到同一张表里。）

#### 4.3.5 小练习与答案

**练习 1**：为什么用中心差分 \(\frac{L(\theta+\varepsilon)-L(\theta-\varepsilon)}{2\varepsilon}\) 而不是单侧差分 \(\frac{L(\theta+\varepsilon)-L(\theta)}{\varepsilon}\)？
**答案**：由泰勒展开，单侧差分的截断误差是 \(O(\varepsilon)\)，中心差分是 \(O(\varepsilon^2)\)。以 \(\varepsilon=10^{-4}\) 为例，中心差分的理论误差约 \(10^{-8}\) 量级，而单侧约 \(10^{-4}\) 量级——后者与我们要检验的梯度本身同量级，没法用。

**练习 2**：校验脚本为什么重新 `clone()` 权重再 `requires_grad_()`，而不是直接对 `net.weight_1` 打开？
**答案**：保持手工版网络「纯净」（`requires_grad=False`）以便反复对照；更重要的是避免校验脚本的状态污染训练现场。`clone()` 后 `requires_grad_()`（注意下划线的原地版）让副本进入计算图，原张量不动。

**练习 3**：数值校验为什么只在小网络上做、且只抽查部分参数？
**答案**：中心差分每个参数要**两次完整前向传播**，39760 个参数就是近 8 万次前向——这正是反向传播要避免的代价（4.2.1 节）。校验的目的是验证公式正确性，小网络 + 抽查足以暴露转置/求和/常数因子这类错误，公式一旦验证，参数量放大不改变正确性。

## 5. 综合实践

**任务：把隐藏层激活从 sigmoid 换成 tanh，完成「推导 → 实现 → 校验 → 训练」全流程。** 这是本讲的收官实践，串起全部三个最小模块。

**第 1 步：推导。** tanh 与 sigmoid 是近亲（\(\tanh(z) = 2\sigma(2z) - 1\)），输出落在 \((-1,1)\)，且导数以激活值表达同样简洁：

\[
a_1 = \tanh(z_1), \qquad \frac{\partial a_1}{\partial z_1} = 1 - a_1^2
\]

把它代入 4.2.2 的隐藏层公式，唯一变化的是要素 2：

\[
\delta_1 = (\delta_{\text{out}} W_o) \odot (1 - a_1^2)
\]

注意 \(1-a_1^2 \in (0, 1]\)，在 0 附近导数接近 1——比 sigmoid（最大 0.25）的梯度更「肥」，这正是 tanh 通常收敛更快的直觉解释。输出层保持 sigmoid 不变（这样 MSE 的目标 (0,1) 与输出范围仍然匹配）。

**第 2 步：实现。** 复制 notebook 的模型 cell，修改两处（**示例代码**，展示需改动的行）：

```python
# forward 中：
a_1 = torch.tanh(z_1)                     # 原来是 torch.sigmoid(z_1)
# backward 中：
da1__dz1 = 1. - a_1.pow(2)                # 原来是 a_1 * (1. - a_1)
```

其余（输出层梯度、权重梯度公式、训练循环）一行都不用改——因为四要素框架里激活函数只出现在要素 2，**这本身就是对框架抽象力的一次验证**。

**第 3 步：校验。** 把 4.3.4 的对账脚本中 `h = torch.sigmoid(...)` 同步改为 `h = torch.tanh(...)`，重跑。预期四个相对误差仍在 1e-6 量级（**待本地验证**）。

**第 4 步：训练对比。** 同种子（`torch.manual_seed(RANDOM_SEED)`）分别训练 sigmoid 版与 tanh 版各 50 epoch，记录：

| 版本 | 最终 Train MSE | 测试准确率 | 前 5 epoch 的 Cost 下降速度 |
|---|---|---|---|
| sigmoid（notebook 基线） | （待本地验证） | 94.49%（notebook 保存值） | （待本地验证） |
| tanh | （待本地验证） | （待本地验证） | （待本地验证） |

预期现象：tanh 版早期损失下降更快（梯度不饱和、导数上界为 1），最终准确率持平或略高；若差异不明显，可把 `num_epochs` 减到 10 放大「收敛速度」上的对比。

## 6. 本讲小结

- **反向传播 = 链式法则的高效矩阵实现**。每层反向都由四要素构成：上游梯度 → 乘激活局部导数得 delta → `δᵀ·输入` 与 `Σδ` 拼出权重/偏置梯度 → `δ·W`（不转置）继续下传。
- `mlp-fromscratch__sigmoid-mse.ipynb` 用**纯 tensor + 手工梯度**实现 784–50–10 网络：没有 nn.Module、没有优化器、没有 `zero_grad`，五步范式塌缩为「forward → backward → 原地减法」三步。
- sigmoid 的导数 \(a(1-a)\) 在饱和区趋近 0，与 MSE 相乘后造成「自信犯错时梯度消失」；softmax + 交叉熵的输出层梯度 \(p-y\) 无此问题——这是现代分类网络选择后者的核心原因之一。
- **梯度必须校验**：autograd 对账时损失口径要与 `backward` 一致（对类求和、除以 N；注意 `loss_func` 与手工梯度相差 1/10 的常数因子）；数值校验用中心差分，float32 下 ε 取 1e-4 附近。
- notebook 保存的最终结果：训练 94.72% / 测试 94.49%，低于 u2-l3 的 autograd 版 MLP（97.54%）——结构、激活、损失、梯度的差异共同作用。
- 读码时留意「形参 vs 全局变量」（`train` 里迭代的是全局 `train_loader`）这类 notebook 式简化，理解它们为什么「能跑」但不利于复用。

## 7. 下一步学习建议

- **u3-l1（第一个卷积神经网络）**：把本讲的链式法则推广到卷积层——卷积层的前向是卷积、反向是转置卷积，四要素框架不变，变化的是「线性层的具体形状公式」。建议带着「卷积层的 \( \partial L/\partial W \) 还是不是 \( \delta^\top \cdot \text{输入} \)？」这个问题去读。
- **u4-l5（Autograd 细节与模型机制杂谈）**：本讲自问了「autograd 在做什么」，那一讲正面拆解 autograd 的使用细节——`retain_grad`、`register_hook` 获取中间变量梯度，与本讲的「手工 vs 自动」视角互补。
- **u2-l3 / u2-l4 回看**：现在再读 `mlp-basic.ipynb`（及 dropout/batchnorm 版），你应该能说出 `loss.backward()` 背后发生了什么，体会 autograd 与优化器帮你省掉了哪些手推。
- 延伸阅读：Michael Nielsen《Neural Networks and Deep Learning》第 2 章对反向传播的四要素有几乎同构的推导；CS231n 的 backprop notes 则把「上游梯度 × 局部导数」的模块化视角讲得更通用。
