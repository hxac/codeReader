# PyTorch 核心基础

> 对应源码：`appendix-A/01_main-chapter-code/code-part1.ipynb`（A.1–A.8）、`code-part2.ipynb`（A.9 GPU 部分）

## 1. 本讲目标

在前面的章节里，你已经用 PyTorch 写过了 `GPTModel`、`train_model_simple`、`MultiHeadAttention` 等大量代码。本讲是**一次系统性的「回头看」**：把支撑全书的 PyTorch 基础正式补齐、讲透。

学完本讲，你应当能够：

1. 说清楚 **张量（tensor）** 是什么，以及它和 Python 列表、NumPy 数组的关系。
2. 理解 **自动微分（autograd）** 如何沿着计算图自动算出梯度，并能区分 `torch.autograd.grad` 与 `loss.backward()` 两种用法。
3. 掌握 **`nn.Module`** 作为「可训练模型标准容器」的职责：子模块注册、参数管理、`forward` 约定。
4. 闭着眼写出 **通用训练循环范式**：`model.train() → forward → loss → zero_grad → backward → step → model.eval()`，并理解 DataLoader、评估、设备（CPU/GPU）、保存加载如何挂进这条主线。

本讲不引入任何新的 LLM 知识，而是把你在 u5-l2（训练循环）、u5-l1（交叉熵损失）、u1-l2（eval 模式）里「用过但没细究」的 PyTorch 机制讲到底，为 u8-l2（warmup/余弦衰减/梯度裁剪）和 u8-l3（DDP 多卡训练）打底。

## 2. 前置知识

- **Python 基础**：会写类（`class`、`__init__`、继承）、会写 `for` 循环与列表推导。
- **一点点矩阵知识**：知道向量点乘、矩阵乘法即可。本讲只会用到 \( z = xw + b \) 这种最基础的线性式。
- **本书前序经验（非必需但 helpful）**：你已经跑通过 `ch04/01_main-chapter-code/gpt.py`（见 u1-l2），知道 `model.eval()`、`torch.no_grad()` 这些调用「大概在做什么」。本讲会把这些调用的底层机制讲明白。

> 几个关键术语，先混个眼熟，后面逐个展开：
> - **张量（tensor）**：PyTorch 里所有数据的容器，本质是「多维数组 + 设备信息 + 是否要求梯度」三件套。
> - **计算图（computation graph）**：把一次前向计算画成有向无环图，节点是运算，边是张量；自动微分就是沿这张图反向走一遍。
> - **梯度（gradient）**：损失对某个参数的偏导数，告诉你「参数往哪个方向调，损失下降最快」。
> - **`nn.Module`**：PyTorch 所有模型层和模型的基类，负责自动收集参数、自动处理子模块。

## 3. 本讲源码地图

附录 A 把 PyTorch 入门拆成了两个 notebook，本讲只精读这两个文件：

| 文件 | 作用 |
|---|---|
| [`appendix-A/01_main-chapter-code/code-part1.ipynb`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/code-part1.ipynb) | 附录 A 的 A.1–A.8：张量、数据类型、运算、计算图、自动微分、`nn.Module`、DataLoader、训练循环、评估、保存加载。**本讲的主战场。** |
| [`appendix-A/01_main-chapter-code/code-part2.ipynb`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/code-part2.ipynb) | 附录 A 的 A.9：GPU/设备处理。演示 `.to("cuda")`、设备不一致报错，以及把训练循环改造成单 GPU 版本。 |

> 旁注：同目录还有 [`DDP-script.py`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/DDP-script.py)（多卡分布式训练脚本），它属于 **u8-l3**，本讲只在结尾的「下一步」里点到，不展开。

---

## 4. 核心概念与源码讲解

本讲把附录 A 的内容组织成四个最小模块，前两个对应「张量与自动微分」，后两个分别对应「`nn.Module`」和「训练循环范式」。

### 4.1 张量：PyTorch 的基本数据结构

#### 4.1.1 概念说明

神经网络里的一切数据——输入文本的嵌入向量、注意力分数、权重矩阵、损失值——在 PyTorch 里统统是 **张量（tensor）**。你可以把张量理解成「会算梯度、能住进 GPU 的 NumPy 数组」。

张量按维数（也叫 rank / ndim）分类：

| 名称 | 维数 | 例子 |
|---|---|---|
| 标量 scalar | 0D | 损失值 `loss = 0.0852` |
| 向量 vector | 1D | 一条 token ID 序列 `[1, 2, 3]` |
| 矩阵 matrix | 2D | 一个批次的嵌入 `(batch, emb_dim)` |
| 3D 张量 | 3D | 一个批次的序列特征 `(batch, num_tokens, emb_dim)` |

在 GPT 里，主干张量就是 `(batch, num_tokens, emb_dim)` 这个 3D 张量，它在整个前向过程中**形状保持不变**（见 u4-l2 的 TransformerBlock）。

#### 4.1.2 核心流程

- **创建**：用 `torch.tensor(...)` 从 Python 列表造，或从 NumPy 数组转换。
- **数据类型**：整数默认 `int64`，浮点数默认 `float32`；模型权重必须是浮点型才能求导。
- **形状操作**：`.shape` 看形状，`.reshape`/`.view` 改形状，`.T` 转置。
- **运算**：`+`、`*` 逐元素；`@` 或 `.matmul` 做矩阵乘法。

矩阵乘法是神经网络最核心的运算：一层线性变换就是 \( z = xW + b \)。附录 A 用 `tensor2d @ tensor2d.T` 演示，本质和 `GPTModel` 里 `out_head(x)` 做的事一样。

#### 4.1.3 源码精读

**创建不同维数的张量**（[code-part1.ipynb:L131-L165](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/code-part1.ipynb#L131-L165)）：从 Python 整数造 0D 张量、从列表造 1D/2D/3D 张量。注意 NumPy 互操作有两种方式——`torch.tensor(ary)` **复制**内存，`torch.from_numpy(ary)` **共享**内存（改一个另一个变）。这条「共享内存」特性在后面 ch05 加载 OpenAI 权重时也会遇到。

**数据类型由内容推断**（[code-part1.ipynb:L222-L223](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/code-part1.ipynb#L222-L223)）：`[1, 2, 3]` 推断成 `int64`，`[1.0, 2.0, 3.0]` 推断成 `float32`。用 `.to(torch.float32)` 可以显式转换——这正是本书把整数 token ID 转成可训练嵌入前必须做的事。

**矩阵乘法两种等价写法**（[code-part1.ipynb:L405](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/code-part1.ipynb#L405) 与 [code-part1.ipynb:L427](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/code-part1.ipynb#L427)）：`tensor2d.matmul(tensor2d.T)` 和 `tensor2d @ tensor2d.T` 完全等价，都算矩阵乘法。全书统一用 `@`。

#### 4.1.4 代码实践

1. **实践目标**：亲手感受张量的维数、数据类型与矩阵乘法。
2. **操作步骤**：在仓库根目录启动 Python（`python` 或 `ipython`），逐行输入：

   ```python
   import torch
   s = torch.tensor(1)                       # 0D
   v = torch.tensor([1, 2, 3])               # 1D
   M = torch.tensor([[1., 2.], [3., 4.]])    # 2D，注意带小数点
   print(s.ndim, v.ndim, M.ndim)             # 0 1 2
   print(v.dtype, M.dtype)                   # torch.int64  torch.float32
   print(M @ M.T)                            # 矩阵乘法
   ```
3. **需要观察的现象**：整数张量 `v` 是 `int64`、无法求导；带小数点的 `M` 是 `float32`。`M @ M.T` 得到一个 2×2 结果。
4. **预期结果**：`M @ M.T` 输出 `tensor([[ 5., 11.], [11., 25.]])`。
5. 若你尝试 `v.requires_grad_()` 会报错——因为 `int64` 不支持 autograd，这正是下一节要讲的「浮点 + requires_grad」前提。

#### 4.1.5 小练习与答案

**练习 1**：为什么模型权重必须是 `float32` 而不能是 `int64`？
**答**：autograd 需要对参数做浮点微分运算；整数类型没有「可微」语义，PyTorch 直接禁止它参与梯度计算。

**练习 2**：`torch.tensor(ary)` 和 `torch.from_numpy(ary)` 的关键区别是什么？
**答**：前者复制一份数据（互不影响），后者与原 NumPy 数组**共享内存**，改一个另一个跟着变。

---

### 4.2 自动微分（autograd）：让梯度自动算

#### 4.2.1 概念说明

训练神经网络，本质是**不断微调参数让损失下降**。要「往下降」，就得知道每个参数的梯度 \(\partial L / \partial w\)。手动对每个公式求导既繁琐又易错，PyTorch 的 **autograd** 替你把这件事自动化了。

它的核心思想是**计算图 + 反向传播**：

1. **前向**时，PyTorch 把每一步运算记成一张有向无环图（DAG），每个张量带一个 `grad_fn` 指向「生产它的运算」。
2. **反向**时，从损失 `loss` 出发，沿图倒着走，用 **链式法则（chain rule）** 逐节点算偏导，最终累积到每个 `requires_grad=True` 的叶子参数上。

附录 A 用一个迷你神经元演示：给定输入 \(x_1\)、权重 \(w_1\)、偏置 \(b\)，算净输入 \(z\)、激活 \(a\) 与二元交叉熵损失 \(L\)（[code-part1.ipynb:L461-L473](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/code-part1.ipynb#L461-L473)）：

\[
z = x_1 w_1 + b,\qquad a = \sigma(z),\qquad L = \mathrm{BCE}(a, y)
\]

链式法则给出权重梯度：

\[
\frac{\partial L}{\partial w_1} = \frac{\partial L}{\partial a}\cdot\frac{\partial a}{\partial z}\cdot\frac{\partial z}{\partial w_1}
\]

autograd 就是自动、逐节点地把这条乘积链算出来。

#### 4.2.2 核心流程

1. 给需要训练的参数标上 `requires_grad=True`（输入数据不用标）。
2. 前向计算，得到 `loss`。
3. 反向求梯度，有两种等价写法：
   - `torch.autograd.grad(loss, w1)` —— 显式「我要 loss 对 w1 的导数」，返回值即是梯度，**不**写入 `w1.grad`。
   - `loss.backward()` —— 一次性把 loss 对**所有**叶子参数的梯度算好，并存进各自的 `.grad` 属性。
4. 优化器读 `.grad` 去更新参数（下一节的训练循环里讲）。

> 关键约定：`.grad` 默认是**累加**的，所以每轮反向前必须 `optimizer.zero_grad()` 清零——这正是你在 u5-l2 见过的「训练四件套」第一步。

#### 4.2.3 源码精读

**用 `requires_grad=True` 标记可训练参数，再用 `torch.autograd.grad` 求导**（[code-part1.ipynb:L507-L525](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/code-part1.ipynb#L507-L525)）：注意只有 `w1`、`b` 设了 `requires_grad=True`，输入 `x1` 和标签 `y` 没设（它们不是要优化的对象）。`grad(loss, w1, retain_graph=True)` 里 `retain_graph=True` 是因为同一个图还要再对 `b` 求一次导，不保留会被释放。输出 `tensor([-0.0898])` 即 \(\partial L/\partial w_1\)。

**更常用的 `loss.backward()` 写法**（[code-part1.ipynb:L543-L548](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/code-part1.ipynb#L543-L548)）：一行 `loss.backward()` 把所有梯度算好并塞进 `w1.grad`、`b.grad`，结果与上面 `grad()` 完全一致（都是 `-0.0898`、`-0.0817`）。全书训练循环统一用这种写法。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看到 autograd 自动算出的梯度，并验证它和「手算」一致。
2. **操作步骤**：

   ```python
   import torch
   import torch.nn.functional as F
   from torch.autograd import grad

   y  = torch.tensor([1.0])
   x1 = torch.tensor([1.1])
   w  = torch.tensor([2.2], requires_grad=True)
   b  = torch.tensor([0.0], requires_grad=True)

   z = x1 * w + b
   a = torch.sigmoid(z)
   loss = F.binary_cross_entropy(a, y)

   loss.backward()
   print("dL/dw =", w.grad, " dL/db =", b.grad)   # -0.0898  -0.0817
   ```
3. **需要观察的现象**：`w.grad` 是负数，说明**增大** \(w\) 会让损失**下降**——这正是梯度下降要利用的方向。
4. **预期结果**：`dL/dw = tensor([-0.0898])`、`dL/db = tensor([-0.0817])`，与 notebook 输出一致。
5. 待本地验证：若你接着再调用一次 `loss.backward()` 会报错「图已被释放」；这说明每次反向用的是一次性图，必须重新前向。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `loss.backward()` 之前通常要 `optimizer.zero_grad()`？
**答**：因为 PyTorch 的 `.grad` 是**累加**而非覆盖。不清零的话，新梯度会叠到上一轮的旧梯度上，参数更新方向就错了。

**练习 2**：`torch.autograd.grad(loss, w)` 和 `loss.backward()` 有何区别？
**答**：前者显式指定「对谁求导」、返回梯度值但不写 `.grad`；后者一次性求 loss 对**所有**叶子参数的导数并写入各自的 `.grad`。工程里几乎只用后者。

---

### 4.3 `nn.Module`：搭建可训练模型的标准容器

#### 4.3.1 概念说明

如果每个权重都像 4.2 那样手动声明成 `requires_grad=True` 的张量，一个 GPT 有上亿参数，根本管不过来。`torch.nn.Module` 解决的就是「**把一层层运算和它们的参数打包、自动管理**」这件事。

`nn.Module` 是 PyTorch 里**所有层和模型的基类**。你在本书里写过的 `LayerNorm`、`FeedForward`、`MultiHeadAttention`、`GPTModel` 全部继承自它。它提供三样核心能力：

1. **自动注册子模块与参数**：在 `__init__` 里把一个 `nn.Linear` 赋给 `self.xxx`，它就被自动登记为子模块；其中的权重自动成为可训练参数。
2. **统一的 `forward` 约定**：调用 `model(x)` 实际触发 `model.__call__(x)`，后者再调你写的 `forward(x)`——所以**只重写 `forward`，不要自己调 `forward`**。
3. **参数集合 API**：`model.parameters()` 迭代所有参数，`model.state_dict()` 导出命名权重字典——优化器和保存加载都依赖它们。

附录 A 的 `NeuralNetwork` 是一个最小示范：两层隐藏层 + 输出层，用 `nn.Sequential` 串起来（[code-part1.ipynb:L572-L593](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/code-part1.ipynb#L572-L593)）。这和 `GPTModel` 的结构同构——只是把 12 个 TransformerBlock 换成了 3 个 Linear。

#### 4.3.2 核心流程

1. 继承 `torch.nn.Module`，`__init__` 里先 `super().__init__()`，再创建各子层。
2. 写 `forward(self, x)`，描述前向数据流（只描述计算，不含参数更新）。
3. `model = MyModel(...)` 实例化后：
   - `model(x)` 做前向（自动建计算图）。
   - `sum(p.numel() for p in model.parameters() if p.requires_grad)` 数可训练参数。
   - 用 `torch.manual_seed(123)` 固定随机初始化，保证可复现。
4. 推理/评估时用 `with torch.no_grad():` 关掉建图，或用 `model.eval()` 关掉 dropout、batchnorm 等训练态行为。

> 形状约定（本书反复出现）：`nn.Linear(in, out)` 的权重形状是 `(out, in)`，所以 `model.layers[0].weight.shape` 是 `(30, 50)` 而不是 `(50, 30)`。这一点在 u5-l4 把 OpenAI 的 `Conv1D` 权重转置进 `Linear` 时非常关键。

#### 4.3.3 源码精读

**`NeuralNetwork` 的完整定义**（[code-part1.ipynb:L572-L593](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/code-part1.ipynb#L572-L593)）：`super().__init__()` 必须调用，否则子模块注册机制不工作；`nn.Sequential` 把若干层按顺序打包成一个子模块 `self.layers`；`forward` 里 `logits = self.layers(x)` 即「未归一化的原始输出」（softmax 留到 loss 函数内部做，数值更稳）。

**统计可训练参数**（[code-part1.ipynb:L646](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/code-part1.ipynb#L646)）：`model.parameters()` 自动递归收集所有子模块的参数，`p.requires_grad` 过滤掉被冻结的（u6-l1 冻结主干时就靠它）。这个范式和 u4-l3 统计 `GPTModel` 的 163M/124M 参数是**同一行代码**。

**前向 + 关闭建图**（见 notebook 中 `with torch.no_grad(): out = model(X)`）：评估时不需要梯度，关掉既省内存又提速。这与 u5-l1 的 `evaluate_model`、u6-l2 的 `calc_accuracy_loader` 用的是同一个套路。

#### 4.3.4 代码实践

1. **实践目标**：仿照附录 A，用 `nn.Module` 手写一个**单变量线性回归**模型 \( \hat{y}=wx+b \)，体会「子模块自动注册 + 参数自动收集」。
2. **操作步骤**（以下为**示例代码**，不在仓库内，可直接运行）：

   ```python
   import torch
   torch.manual_seed(0)

   # 造一批带噪声的线性数据：真实 w=2, b=0.5
   X = torch.linspace(-1, 1, 50).reshape(-1, 1)
   y = 2.0 * X + 0.5 + 0.1 * torch.randn_like(X)

   class LinearRegression(torch.nn.Module):
       def __init__(self):
           super().__init__()
           self.linear = torch.nn.Linear(1, 1)   # 自动注册参数 w、b
       def forward(self, x):
           return self.linear(x)

   model = LinearRegression()
   print("可训练参数个数：", sum(p.numel() for p in model.parameters()))  # 2 (w 和 b)
   ```
3. **需要观察的现象**：`sum(...)` 输出 `2`，证明 `nn.Linear(1,1)` 的权重和偏置被自动登记为可训练参数，无需手动 `requires_grad=True`。
4. **预期结果**：`可训练参数个数： 2`。打印 `model.linear.weight` 和 `model.linear.bias` 可见它们带 `requires_grad=True`。
5. 把这个模型留到 4.4 的实践里继续训练。

#### 4.3.5 小练习与答案

**练习 1**：为什么实例化子层后，不用手动 `requires_grad=True`？
**答**：`nn.Module` 在注册子模块时，其内部 `nn.Parameter` 默认就是 `requires_grad=True`，autograd 会自动追踪。

**练习 2**：调用 `model(x)` 和直接 `model.forward(x)` 哪个对？为什么？
**答**：用 `model(x)`。`__call__` 会先做一些钩子和模式处理再转调 `forward`；直接调 `forward` 会绕过这些机制，容易出 bug。

---

### 4.4 通用训练循环范式（DataLoader → 训练 → 评估 → 设备 → 保存）

#### 4.4.1 概念说明

把前三个模块拼起来，就得到深度学习里**最通用的一条主线**——本书从第 5 章预训练到第 7 章指令微调，循环骨架几乎一模一样，只是换了数据、损失和模型。附录 A 用一个玩具二分类把这条主线讲到了最简：

> **每个 epoch**：`model.train()` → 遍历 DataLoader 的每个 batch → forward 算 logits → 算 loss → `zero_grad` → `backward` → `step` →（周期性）`model.eval()` 评估。

这条主线挂载着四件配套设施，本模块逐一过：

1. **`Dataset` + `DataLoader`**：把原始数据变成「按 batch 迭代、自动打乱」的流水线。
2. **损失函数**：分类用 `F.cross_entropy`，回归用 `F.mse_loss`。
3. **评估**：准确率用 `argmax`（注意它不可微，所以**只能用来汇报、不能用来优化**——优化靠可微的交叉熵代理，这点 u6-l2 已强调过）。
4. **设备与保存加载**：`.to(device)` 把模型和数据搬上 GPU；`state_dict` 存/取权重。

#### 4.4.2 核心流程

```
Dataset(__getitem__, __len__)      # 自定义数据集：按下标取一条样本
        │
        ▼
DataLoader(dataset, batch_size,    # 工厂：自动分批、打乱、多进程加载
           shuffle, drop_last, num_workers)
        │
        ▼
for epoch in range(num_epochs):
    model.train()                  # 训练态（启用 dropout 等）
    for features, labels in loader:
        logits = model(features)   # 前向
        loss  = F.cross_entropy(logits, labels)
        optimizer.zero_grad()      # 清空累加梯度
        loss.backward()            # 反向（autograd）
        optimizer.step()           # 用 .grad 更新参数
    model.eval()                   # 评估态
    acc = compute_accuracy(model, test_loader)   # 用 argmax 汇报
```

设备处理（来自 code-part2）只是在这条流程上「加三行」：

- 开头：`device = torch.device("cuda" if torch.cuda.is_available() else "cpu")` + `model.to(device)`。
- 循环内：`features, labels = features.to(device), labels.to(device)`。

#### 4.4.3 源码精读

**自定义 `Dataset`**（[code-part1.ipynb:L844-L861](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/code-part1.ipynb#L844-L861)）：只需实现 `__init__`（存数据）、`__getitem__`（按下标返回一条 `(x, y)`）、`__len__`（返回样本数）。这正是本书 `GPTDatasetV1`（u2-l3）、`SpamDataset`（u6-l1）、`InstructionDataset`（u7-l1）的共同骨架。

**`DataLoader` 工厂**（[code-part1.ipynb:L888-L895](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/code-part1.ipynb#L888-L895)）：`batch_size=2`、`shuffle=True` 训练时打乱、`num_workers=0` 单进程加载、`drop_last=True` 丢弃凑不满一个 batch 的尾部（保证 batch 形状一致）。这些参数你会在 u2-l3 的 `create_dataloader_v1` 里再次见到。

**训练循环本体**（[code-part1.ipynb:L1020-L1042](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/code-part1.ipynb#L1020-L1042)）：`SGD` 优化器、`F.cross_entropy` 损失、清晰的 `zero_grad → backward → step` 三件套、epoch 末 `model.eval()`。这和 u5-l2 的 `train_model_simple` 是**同一套骨架**——只不过那里优化器换成了 AdamW、损失带掩码、并多了周期性采样与绘图。

**评估函数 `compute_accuracy`**（[code-part1.ipynb:L1169-L1186](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/code-part1.ipynb#L1169-L1186)）：`torch.no_grad()` 关建图、`argmax(dim=1)` 取预测类、统计正确数除以总数。它和 u6-l2 的 `calc_accuracy_loader` 几乎逐行相同。

**保存与加载**（保存 [code-part1.ipynb:L1245](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/code-part1.ipynb#L1245)、加载 [code-part1.ipynb:L1267](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/code-part1.ipynb#L1267)）：`torch.save(model.state_dict(), "model.pth")` 存权重字典；新建**同结构**模型后 `load_state_dict(torch.load(..., weights_only=True))` 还原。这正是 u5-l4 保存微调权重、u7-l2 存 SFT 权重 `gpt2-medium355M-sft.pth` 的底层操作。

**设备迁移与「同设备」约束**（[code-part2.ipynb:L145-L148](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/code-part2.ipynb#L145-L148)）：`.to("cuda")` 把张量搬上 GPU，输出会带 `device='cuda:0'`。code-part2 特意演示了一个反面教材（[code-part2.ipynb:L176-L177](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/code-part2.ipynb#L176-L177)）：把 `tensor_1` 搬回 CPU 后再与仍在 GPU 的 `tensor_2` 相加，会抛 `RuntimeError: Expected all tensors to be on the same device`。**参与同一次运算的所有张量必须在同一设备上**——这是本书 `GPTModel` 里 `torch.arange(seq_len, device=x.device)` 这种写法的根本原因。

**带设备的训练循环**（[code-part2.ipynb:L327-L355](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/code-part2.ipynb#L327-L355)）：相比 code-part1 的 CPU 版，只多了 `model.to(device)` 与 batch 内的 `features.to(device), labels.to(device)`，循环逻辑完全不变。`compute_accuracy` 也对应加了 `device` 参数（[code-part2.ipynb:L373-L389](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/code-part2.ipynb#L373-L389)）。

> 回扣 u1-l2：那里提到「`gpt.py` 故意只跑 CPU、设备选择要到第 5 章训练才登场」。现在你看到了——`torch.device(...)` 范式就出自附录 A 的这段 code-part2，全书训练脚本统一沿用。

#### 4.4.4 代码实践（本讲核心实践）

1. **实践目标**：把 4.3 写的 `LinearRegression` 用 autograd 训练起来，画出损失下降曲线，完整复现「`nn.Module` + 训练循环」最小范式。
2. **操作步骤**（以下为**示例代码**，承接 4.3.4，可直接运行；建议存为 `linreg_demo.py`）：

   ```python
   import torch
   import matplotlib.pyplot as plt
   torch.manual_seed(0)

   X = torch.linspace(-1, 1, 50).reshape(-1, 1)
   y = 2.0 * X + 0.5 + 0.1 * torch.randn_like(X)

   class LinearRegression(torch.nn.Module):
       def __init__(self):
           super().__init__()
           self.linear = torch.nn.Linear(1, 1)
       def forward(self, x):
           return self.linear(x)

   model = LinearRegression()
   optimizer = torch.optim.SGD(model.parameters(), lr=0.1)
   loss_fn = torch.nn.MSELoss()

   losses = []
   for epoch in range(100):
       model.train()
       pred = model(X)                  # 前向（自动建图）
       loss = loss_fn(pred, y)          # 均方误差
       optimizer.zero_grad()            # 清空梯度
       loss.backward()                  # autograd 反向
       optimizer.step()                 # 更新参数
       losses.append(loss.item())

   print("学到的 w =", model.linear.weight.item(),
         " b =", model.linear.bias.item())   # 应接近 2.0 / 0.5
   plt.plot(losses); plt.xlabel("epoch"); plt.ylabel("MSE loss"); plt.title("Loss curve")
   plt.savefig("loss_curve.png")   # 或 plt.show()
   ```
3. **需要观察的现象**：
   - 损失曲线从某个较高值**单调快速下降**并趋于平缓。
   - 学到的 `w`、`b` 逼近真实值 `2.0`、`0.5`。
4. **预期结果**：约 100 个 epoch 后，`w ≈ 2.0`、`b ≈ 0.5`；`loss_curve.png` 是一条典型下降曲线（前几十步骤降、后段收敛）。若曲线不收敛，把学习率调小（如 `0.01`）再试。
5. **与本书对照**：把这里的四件套 `zero_grad → backward → step` 和 [code-part1.ipynb:L1020-L1042](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/code-part1.ipynb#L1020-L1042) 逐行比对——你会发现范式完全相同，只是损失从 `cross_entropy` 换成了 `MSELoss`、数据从分类换成了回归。

#### 4.4.5 小练习与答案

**练习 1**：为什么评估/推理时要用 `model.eval()` 和 `with torch.no_grad():`？它们各自管什么？
**答**：`model.eval()` 关闭训练态行为（如 dropout 随机丢弃、batchnorm 用全局统计量）；`torch.no_grad()` 关闭 autograd 建图、不存中间激活、省内存提速。两者作用不同，评估时通常**同时**使用。

**练习 2**：`compute_accuracy` 用 `argmax` 得到预测，为什么不能直接把准确率当损失去 `backward`？
**答**：`argmax` 不可微（梯度几乎处处为 0），无法反传。所以**训练用可微的交叉熵损失作代理、评估用准确率汇报**——这正是 u6-l2 强调的基本分工。

**练习 3**：`load_state_dict` 时为什么要「新建同结构模型」？
**答**：`state_dict` 只存权重数值、不存模型结构。必须先 `model = MyModel(...)` 重建一张同样拓扑的「空壳」，再把权重「灌」进去，否则参数无处安放。

---

## 5. 综合实践

把本讲四个模块串成一个迷你端到端任务，完整走一遍「数据 → 模型 → 训练 → 评估 → 保存 → 设备」全流程，作为对附录 A 的总验收。

**任务**：在附录 A 的玩具二分类数据上，复现 `NeuralNetwork` 的训练，并做三处改造观察现象。

1. **运行基线**：打开 [`code-part1.ipynb`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/code-part1.ipynb) 的 A.5–A.7，跑通 `NeuralNetwork(2, 2)` 的 3 个 epoch 训练，确认 `compute_accuracy(model, test_loader)` 返回 `1.0`。
2. **改造一（autograd 观察）**：在训练循环的 `loss.backward()` 之后、`optimizer.step()` 之前，打印 `model.layers[0].weight.grad` 的前 3 个值；再在 `step()` 之后打印一次权重本身。观察「梯度→权重更新」的因果链。
3. **改造二（参数管理）**：用 `sum(p.numel() for p in model.parameters() if p.requires_grad)` 数出该网络的可训练参数量，并手算验证：`(2×30+30) + (30×20+20) + (20×2+2) = 1612`（核对与程序输出是否一致）。
4. **改造三（设备 + 保存）**：参照 [code-part2.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/code-part2.ipynb) 给训练循环加上 `device` 处理（无 GPU 时自动回落 CPU 不报错），训练完后用 `torch.save(model.state_dict(), "my_model.pth")` 存盘，再新建模型 `load_state_dict` 验证 `<All keys matched successfully>`。

**验收标准**：能解释每一步「为什么这么做」，并说清 `model.train()/eval()`、`zero_grad`、`backward`、`step`、`.to(device)`、`state_dict` 各自在流程中的位置与作用。

## 6. 本讲小结

- **张量**是 PyTorch 的统一数据容器，按维数分 0D/1D/2D/3D；模型权重必须是 `float32` 才能 autograd，矩阵乘法用 `@`。
- **自动微分**靠前向建计算图、反向走链式法则；`loss.backward()` 一次性把梯度算好写入各参数的 `.grad`，而 `.grad` 默认累加故须 `zero_grad`。
- **`nn.Module`** 是所有层/模型的基类，自动注册子模块与参数、约定只重写 `forward`、用 `parameters()`/`state_dict()` 统一管理权重。
- **通用训练循环**是 `train() → forward → loss → zero_grad → backward → step → eval()`，本书第 5–7 章的所有训练都是这条骨架的变体。
- **评估**用不可微的 `argmax` 算准确率（只汇报），**优化**用可微的交叉熵/MSE 当代理损失。
- **设备**上所有参与运算的张量必须同设备；`state_dict` 只存权重不存结构，加载前要先建同结构空壳。

## 7. 下一步学习建议

- **紧接本讲**：进入 **u8-l2（训练循环增强：warmup / 余弦衰减 / 梯度裁剪）**，把本讲的朴素 `SGD` 循环升级为大模型训练更稳定的版本（附录 D），届时你会再次回到 `train_model_simple` 这条骨架上。
- **多卡训练**：本讲的设备处理只涉及单 GPU。想了解多 GPU，读 [`DDP-script.py`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/DDP-script.py)，对应 **u8-l3（分布式训练 DDP）**。
- **回看正文**：带着本讲的底层视角，重读 u5-l2 的 `train_model_simple`、u6-l2 的 `train_classifier_simple`，你会发现自己过去「照抄」的那些 PyTorch 调用，现在每行都能说清原理。
- **进阶 autograd**：本讲只用了默认的标量反向；若对计算图细节感兴趣，可深入 `torch.autograd.grad`、`retain_graph`、二阶导等主题。
