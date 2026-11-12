# Autograd 细节与模型机制杂谈

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 PyTorch 为什么默认只给「叶子张量」保存梯度，并掌握获取中间变量梯度的三种手段：`autograd.grad`（含元组一次遍历写法）、`retain_grad()`、张量级 `register_hook`。
2. 理解「层内权重共享」的实现思路：把权重矩阵降级为权重向量、用广播机制复制输出、再手动定义逐单元偏置参数。
3. 掌握 `nn.Sequential` 堆叠式模型定义的写法，并用模块级 forward hook 观测 Sequential 内部每层的输出。
4. 分清两类容易混淆的 hook：张量级 hook（反向传播时触发，拿到的是梯度）与模块级 forward hook（前向传播时触发，拿到的是层的输出）。

## 2. 前置知识

本讲是「机制杂谈」，不动模型主线，但需要你带着以下几个概念来（不熟悉的话请先回顾对应讲义）：

- **计算图与链式法则**：PyTorch 把每一步张量运算记录成一张动态图，`backward()` 沿图反向用链式法则逐段求导（回顾 u2-l5 从零实现反向传播）。
- **叶子张量（leaf tensor）**：由用户直接创建、不是由其他运算得出的张量，例如 `torch.tensor([3.], requires_grad=True)`，以及神经网络的权重 `nn.Linear` 中的 `weight`/`bias`。`a = relu(x*w + b)` 这类运算结果则是**非叶子（中间）张量**。
- **默认省内存策略**：`backward()` 之后，PyTorch 默认只把梯度写进叶子张量的 `.grad` 属性；中间张量的梯度算完就丢。想看中间梯度，就必须显式「留一手」——这正是本讲模块一的主题。
- **hook（钩子）**：注册到框架执行流里的回调函数。张量上的 hook 在「关于该张量的梯度被算出来」时触发；模块（`nn.Module`）上的 forward hook 在「该模块完成一次前向」时触发。
- **广播（broadcasting）**：形状 `[N, 1]` 的张量与形状 `[10]` 的张量相加时，PyTorch 自动把两者扩展成 `[N, 10]` 完成运算。模块二的权重共享实现全靠它。
- **五步训练范式**：`forward → loss → zero_grad → backward → step`（回顾 u2-l2 / u2-l3），本讲三个 notebook 的训练循环都是它的直接套用。

> **行号约定**：本讲引用的 notebook 永久链接，行号均指向 `.ipynb` 文件（JSON 源码）中的行，而非渲染后 notebook 的单元格序号。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pytorch_ipynb/mechanics/manual-gradients.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/manual-gradients.ipynb) | 用一个 5 节点 toy 计算图演示获取中间变量梯度的三种方法（`grad`、`retain_grad`、张量 hook），并附 TF1 对照 |
| [pytorch_ipynb/mechanics/cnn-weight-sharing.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/cnn-weight-sharing.ipynb) | 在一个小 CNN 的最后一层实现「所有输出单元共享权重、各自独享偏置」的技术演示 |
| [pytorch_ipynb/mechanics/mlp-sequential.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/mlp-sequential.ipynb) | 用 `nn.Sequential` 重新定义 u2-l3 的 784-128-256-10 MLP，并用 forward hook 抓取第 2 层的输出 |

这三个 notebook 都位于 `pytorch_ipynb/mechanics/` 目录——仓库专门存放「机制实验」的地方，它们不为刷准确率，而是回答「框架在这里面到底做了什么」。

## 4. 核心概念与源码讲解

### 4.1 中间变量梯度：grad、retain_grad 与张量 hook

#### 4.1.1 概念说明

训练网络时，我们通常只关心**损失对权重的梯度**（权重是叶子张量，`backward()` 后自动出现在 `.grad` 里）。但很多调试与研究场景需要**中间变量的梯度**：

- 检查梯度在哪一层消失或爆炸（回顾 u2-l5：sigmoid 饱和区梯度趋零）；
- 实现自定义正则或损失（对某个中间激活加约束）；
- 做可视化（对输入求梯度得到显著性图，见 u7-l6）。

问题在于：中间张量不是叶子，PyTorch 默认不为它们保存 `.grad`。这个 notebook 用一个只有 5 个节点的计算图，把「怎么把中间梯度拿出来」讲得干干净净。toy 图是：

\[
u = x \cdot w,\qquad v = u + b,\qquad a = \mathrm{relu}(v)
\]

取 \(x=3,\ w=2,\ b=1\)，则前向得到 \(u=6,\ v=7,\ a=7\)。由于 \(v>0\)，ReLU 处于导通区（局部导数为 1），各偏导数为：

\[
\frac{\partial a}{\partial v}=1,\quad
\frac{\partial a}{\partial u}=1,\quad
\frac{\partial a}{\partial b}=1,\quad
\frac{\partial a}{\partial w}=\frac{\partial a}{\partial u}\cdot\frac{\partial u}{\partial w}=1\cdot x=3,\quad
\frac{\partial a}{\partial x}=1\cdot w=2
\]

下面三种方法算出来的都应该是这一组数。

#### 4.1.2 核心流程

三种方法的执行过程对比：

```text
方法一：autograd.grad 逐个求 + retain_graph=True
  建图 → 对 b 调 grad（保留图）→ 对 u 调 grad（保留图）→ ... → 对 x 调 grad（释放图）
  缺点：每调一次 grad 都重放一遍反向计算，多次遍历图，低效

方法二：autograd.grad 一次传入元组
  建图 → grad(a, (x, w, b, u, v)) → 一次反向遍历，同时返回 5 个偏导（推荐）

方法三：retain_grad + backward
  建图 → u.retain_grad(); v.retain_grad() → a.backward()
  → 中间张量的 .grad 也被填充（最贴近日常训练代码的写法）

方法四：张量 register_hook
  建图 → 对每个张量注册回调 → a.backward()
  → 反向经过每个张量时回调被触发，梯度存入外部字典
  作者注明：这是「不推荐的 workaround」，但它展示了 hook 机制本身
```

关键参数辨析：

- `retain_graph=True`：保留**计算图结构**，让 `grad`/`backward` 可以被再次调用（默认 False，一次反向后图被释放）。
- `retain_grad()`：让**这个非叶子张量**的梯度在反向后被写进它的 `.grad` 属性（默认不做，省内存）。

两者名字像，作用对象完全不同：一个管「图能不能再跑」，一个管「这张量要不要记梯度」。

#### 4.1.3 源码精读

**方法一：逐个 `grad` + `retain_graph=True`**

[pytorch_ipynb/mechanics/manual-gradients.ipynb:L168-L187](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/manual-gradients.ipynb#L168-L187) 中，先用三个 `requires_grad=True` 的标量张量搭出 toy 图（`u = x * w`、`v = u + b`、`a = F.relu(v)`），然后：

```python
d_a_b = grad(a, b, retain_graph=True)
d_a_u = grad(a, u, retain_graph=True)
d_a_v = grad(a, v, retain_graph=True)
d_a_w = grad(a, w, retain_graph=True)
d_a_x = grad(a, x)   # 最后一个不用保留图
```

前四次调用都必须传 `retain_graph=True`，否则图在第一次 `grad` 后就被释放，第二次调用直接报错。注意 `x`、`w`、`b` 虽是叶子，中间量 `u`、`v` 也不是叶子，`grad` 函数对两者一视同仁——只要图还在，对哪个节点求偏导都行。

**方法二：元组一次遍历**

[pytorch_ipynb/mechanics/manual-gradients.ipynb:L217-L231](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/manual-gradients.ipynb#L217-L231) 把五个目标打包成元组一次传入：

```python
partial_derivatives = grad(a, (x, w, b, u, v))
```

`grad` 的第二个参数本身就接受「张量序列」，一次反向遍历即可同时得到全部偏导并复用中间结果——notebook 引用 Adam Paszke（PyTorch 作者之一）的建议，明确指出这比方法一高效得多。需要「只算梯度、不碰 `.grad` 属性」时，这是首选。

**方法三：`retain_grad` + `backward`**

[pytorch_ipynb/mechanics/manual-gradients.ipynb:L271-L285](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/manual-gradients.ipynb#L271-L285) 回到日常训练中最常用的 `backward()` 路线：

```python
u.retain_grad()
v.retain_grad()

a.backward()

for name, var in zip("xwbuv", (x, w, b, u, v)):
    print('d_a_%s:' % name, var.grad)
```

`retain_grad()` 必须在 `backward()` **之前**调用才生效；调用后，反向传播路过 `u`、`v` 时会把梯度写入它们的 `.grad`。三次实验的输出完全一致：`d_a_x: 2`、`d_a_w: 3`、`d_a_b: 1`、`d_a_u: 1`、`d_a_v: 1`，与 4.1.1 的手推结果吻合。

**方法四：张量 hook**

[pytorch_ipynb/mechanics/manual-gradients.ipynb:L337-L358](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/manual-gradients.ipynb#L337-L358) 用「工厂函数 + 闭包」把梯度收集到全局字典：

```python
grads = {}
def save_grad(name):
    def hook(grad):
        grads[name] = grad
    return hook

x.register_hook(save_grad('d_a_x'))
# ... w、b、u、v 同理 ...
a.backward()
```

`save_grad('d_a_x')` 返回一个闭包 `hook`，它「记住」了名字 `d_a_x`；`register_hook` 把它挂到张量上。`backward()` 反向经过每个张量时，回调以「该张量收到的梯度」为入参被触发，于是 `grads` 字典收齐五个偏导。注意 hook 拿到的是**梯度值本身**，不写入 `.grad` 属性；而且 notebook 的 markdown 明确说这是「不推荐的 workaround」——但它正是理解 hook 机制的最小样例，模块三的 forward hook 与它同源不同向。

顺带一提，notebook 开头还有一段 TensorFlow 1.x 的对照实现（[L94-L134](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/manual-gradients.ipynb#L94-L134)）：TF1 在建图阶段就能「声明」`tf.gradients(a, u)` 这类任意节点的梯度，运行会话时一并求值——两种框架求解目标相同，组织方式完全不同，可在 u8-l7 的框架对照中再回味。

#### 4.1.4 代码实践

**实践目标**：亲手验证三种方法给出同一组偏导，并观察 ReLU 饱和时梯度的行为。

**操作步骤**（以下为示例代码，非项目原有代码，可直接在仓库根目录用 `python -i` 或新建单元格运行）：

1. 复现方法三：建 toy 图 → `u.retain_grad()` → `a.backward()` → 打印五个 `.grad`；
2. 改用方法二：重建同一张图（旧图已被释放，必须重建），`grad(a, (x, w, b, u, v))` 一次求全；
3. 把 `x` 从 3 改成 `-3`（此时 \(v=-6+1=-5<0\)，ReLU 关断），重复步骤 2。

**需要观察的现象**：

- 步骤 1、2 的输出都应是 `2, 3, 1, 1, 1`；
- 步骤 3 中五个偏导全部变为 `0`——ReLU 在 \(v<0\) 时局部导数为 0，链式法则整条路被掐断，这正是「ReLU 死区」的梯度视角。

**预期结果**：三种方法数值完全一致；`x=-3` 时全零梯度。

**待本地验证**：以上为代码逻辑推演，具体打印格式请以本地运行为准。

#### 4.1.5 小练习与答案

**练习 1**：`retain_graph=True` 和 `retain_grad()` 分别解决什么问题？

**答案**：`retain_graph=True` 让计算图在一次 `grad`/`backward` 之后不被释放，从而可以再次反向（解决「图能否重跑」）；`retain_grad()` 让某个非叶子张量在反向后把梯度存进自己的 `.grad` 属性（解决「中间量梯度去哪了」）。前者是 `grad`/`backward` 的调用参数，后者是张量上的方法，且必须在反向之前调用。

**练习 2**：为什么方法二（元组传参）比方法一（多次 `grad`）高效？

**答案**：方法一每调用一次 `grad` 都要走一遍反向遍历、重算沿途的中间梯度，五次调用就是五次遍历；方法二把五个目标一起传入，单次反向遍历中沿路分摊各节点的梯度，中间结果只算一遍。

**练习 3**：张量 `register_hook` 的回调在什么时候触发？它写入 `.grad` 吗？

**答案**：在反向传播中「关于该张量的梯度被计算出来」的那一刻触发，回调入参就是这个梯度。它不写入 `.grad` 属性——梯度去向完全由回调自己决定（notebook 里是存进外部字典 `grads`）。

### 4.2 层内权重共享：把权重矩阵降为权重向量

#### 4.2.1 概念说明

常规全连接层 `Linear(in, out)` 的权重是一个 `[out, in]` 的**矩阵**：每个输出单元有自己的一列（PyTorch 按行存放，逻辑上是每组独立权重）权重。某些研究场景（如 Siamese 结构、 tying 输入与输出层）需要**层内共享权重**：所有输出单元用同一组权重，只保留各自独立的偏置。

notebook 的做法不是把矩阵的各行复制 10 份（那是「共享了但参数照付」），而是彻底把矩阵降级为**一个向量**：让层只算出一个标量得分，再借助**广播**把它「铺」到 10 个类别上，最后逐类别加独立偏置。参数量从 \(392\times10\) 直降到 \(392\)。

注意：这是一个**技术演示**，notebook 自己反复声明这种权重共享对 MNIST 分类并不合适（4.2.2 会从数学上解释为什么它注定失败）。

#### 4.2.2 核心流程

设最后一个隐藏层输出为展平后的向量 \(h\in\mathbb{R}^{392}\)（即 7×7×8）。常规层：

\[
\text{logits}_j = w_j^\top h + b_j,\qquad j=1,\dots,10
\]

权重共享层把 10 个 \(w_j\) 换成同一个 \(w\)：

\[
\text{logits}_j = w^\top h + b_j
\]

实现分三步：

```text
1. 定义层：Linear(7*7*8, 1, bias=False)  → 只含一个 [1, 392] 权重向量，无偏置
2. 手动定义偏置：nn.Parameter(torch.zeros(num_classes)) → 10 维可学习向量
3. forward：logits = linear_1(h)          → 形状 [N, 1]
           logits = logits + bias         → [N, 1] + [10] 广播成 [N, 10]
```

广播规则：`[N, 1]` 与 `[10]` 相加时，右对齐比较维度——第一维 `1` 与 `10` 广播为 `10`，第二维补成 `N`，结果 `[N, 10]`。第 \(j\) 列全部等于 \(w^\top h_i + b_j\)。

**为什么这个模型注定失败**（一个漂亮的推论）：对任意样本 \(i\)，共享项 \(w^\top h_i\) 对所有类别 \(j\) 是**同一个常数**，因此

\[
\arg\max_j\ \text{logits}_{i,j} \;=\; \arg\max_j\ b_j
\]

预测类别与输入 \(h_i\) 完全无关——无论看到什么图片，模型永远输出 \(\arg\max_j b_j\) 这一个类别！所以它的准确率只能停在「单一类别在数据中的占比」附近（MNIST 每类约 10%）。notebook 里训练准确率十个 epoch 恒定 11.24%、测试准确率 11.35%，正是这个数学结论的实证。交叉熵依然会更新参数（\(\partial L/\partial b_j = p_j - y_j\)），但预测能力已被结构封死。

#### 4.2.3 源码精读

**markdown 中的思路说明**

[pytorch_ipynb/mechanics/cnn-weight-sharing.ipynb:L80-L100](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/cnn-weight-sharing.ipynb#L80-L100) 先在 markdown 里给出改法：注释掉 `Linear(7*7*8, num_classes)`（L80），换成 `Linear(7*7*8, 1, bias=False)`（L83）并用 `nn.Parameter` 手动定义偏置（L86-L87），forward 里先算单输出（L95）再加偏置（L100）。注意 markdown 里这段是**示意代码**（其中 `ones = torch.ones(...)` 后一行紧跟 `ones = logits`，示意「复制输出」的思路），真正落地的模型代码在下面这个 cell，靠广播完成复制，写法更简洁。

**`__init__`：共享权重 + 手动偏置**

[pytorch_ipynb/mechanics/cnn-weight-sharing.ipynb:L261-L270](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/cnn-weight-sharing.ipynb#L261-L270) 是 `ConvNet.__init__` 中被注释块标出的核心段：

```python
### WEIGHT SHARING IN LAST LAYER

#self.linear_1 = torch.nn.Linear(7*7*8, num_classes)

# Use only a weight vector instead of weight matrix:
self.linear_1 = torch.nn.Linear(7*7*8, 1, bias=False)

# Define bias manually:
self.linear_1_bias = torch.nn.Parameter(torch.tensor(torch.zeros(num_classes),
                                        dtype=self.linear_1.weight.dtype))
```

三个要点：`out_features=1` 把权重压成单个向量；`bias=False` 关掉层内偏置（因为它只有 1 个，而我们需要 10 个独立的）；偏置用 `torch.nn.Parameter` 包裹后赋为属性——`nn.Module` 会自动把 `Parameter` 登记进 `parameters()`，所以 `optimizer = SGD(model.parameters(), ...)` 照常能更新它。这一步正是 u2-l3 讲过的「`__init__` 中创建的参数自动登记」机制的直接应用。

**`forward`：一次矩阵-向量乘 + 广播加偏置**

[pytorch_ipynb/mechanics/cnn-weight-sharing.ipynb:L283-L292](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/cnn-weight-sharing.ipynb#L283-L292)：

```python
### WEIGHT SHARING IN LAST LAYER

# Duplicate outputs over all output units
logits = self.linear_1(out.view(-1, 7*7*8))

# then manually add bias
logits = logits + self.linear_1_bias
```

`out.view(-1, 7*7*8)` 把池化后的 `[N, 8, 7, 7]` 特征展平（卷积主干 28×28×1 → 14×14×4 → 7×7×8，与 u3-l1 的尺寸链一致），`linear_1` 输出 `[N, 1]`，加 10 维偏置广播成 `[N, 10]`。「复制到所有输出单元」这件事由广播隐式完成——注释里的 Duplicate 说的就是它。

**验证偏置确实被独立更新**

[pytorch_ipynb/mechanics/cnn-weight-sharing.ipynb:L518](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/cnn-weight-sharing.ipynb#L518) 在训练后直接查看 `model.linear_1_bias`，输出是 10 个互不相同的值（如 `-0.0202, 0.1097, -0.0029, ...`），说明手动 `Parameter` 与普通层参数一样参与了 SGD 更新。最终的测试评估在 [L529-L546](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/cnn-weight-sharing.ipynb#L529-L546)，测试准确率 11.35%。另注意 [L269-L270](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/cnn-weight-sharing.ipynb#L269-L270) 的 `torch.tensor(torch.zeros(num_classes), ...)` 会触发一条 `UserWarning`（从张量拷贝构造建议改用 `.clone().detach()`）——这是旧式写法，读代码时理解意图即可。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：用参数量与预测公式两个角度，定量理解「共享了什么、损失了什么」。

**操作步骤**：

1. 数参数：原版 `Linear(392, 10)` 有 \(392\times10+10=3930\) 个参数；共享版只有 \(392+10=402\) 个。打开 notebook 核对 L263（被注释的原版）与 L266（共享版）；
2. 读训练日志：翻看 notebook 训练 cell（[L312-L492](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/cnn-weight-sharing.ipynb#L312-L492)）的输出，注意每个 epoch 的损失都在 2.30 附近徘徊（\(\ln 10 \approx 2.3026\)，即「均匀猜测」的交叉熵水平）、训练准确率恒为 11.24%；
3. 回答：结合 4.2.2 的公式解释为什么准确率恒定在 11.24% 而不是随训练上升。

**需要观察的现象**：损失几乎不动；训练/测试准确率都停在 10%~11.5% 区间；偏置却在正常更新（L518 的十个值不同）。

**预期结果**：能说出「共享项 \(w^\top h\) 对 argmax 不起作用，预测永远等于 \(\arg\max_j b_j\)，模型退化为『只猜一个类』的常数分类器」。

**待本地验证**：若在你本地重跑 notebook，11.24% 这一数值可能随初始化微变，但「恒定不涨」的现象应保持。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `linear_1` 要设 `bias=False`？

**答案**：`Linear(392, 1)` 的内置偏置只有 1 个标量，而设计需要 10 个互相独立的类别偏置；若保留内置偏置，它会先被加进 `[N, 1]` 的输出，与后面手动偏置的语义重复且不对称。关掉它，偏置完全由手动的 `linear_1_bias` 承担。

**练习 2**：`self.linear_1_bias` 为什么不用 `torch.zeros(10)` 直接赋值，而要包一层 `nn.Parameter`？

**答案**：`nn.Module` 只把 `nn.Parameter` 类型的属性自动登记进 `parameters()`，进而被 `model.parameters()` 交给优化器；裸张量赋值只会成为常量属性，训练中永远不更新。

**练习 3**：如果把这种「全共享」放松为「分 2 组共享」（每组 5 个类别共用一个权重向量），模型还能区分开组内的类别吗？组间呢？

**答案**：组内不能、组间能。同组内 5 个类别的 logits 仍只差偏置，组内 argmax 仍由偏置决定；但不同组使用不同权重向量，\(w_1^\top h\) 与 \(w_2^\top h\) 一般不同，组间的大小关系随输入变化，因此「选哪一组」是可以由输入决定的。（这个思想延伸出去就是分组卷积的「组内共享、组间独立」，见 u3-l2 的 AlexNet 分组卷积。）

### 4.3 Sequential API 与 forward hook

#### 4.3.1 概念说明

u2-l3 里我们手写 `forward`：一层一层调用 `self.z1 = ...`。当网络是「纯顺序堆叠」时，`nn.Sequential` 可以替你把模块按顺序串起来——它本身也是 `nn.Module`，`forward` 就是依次把输入喂给每个子模块。好处是定义紧凑、不易写错层的顺序；**代价**是中间结果不再有变量名可引用，想看第 2 层的输出就没有 `self.layer_2_out` 这样的东西可拿。

补回这个能力的正是**模块级 forward hook**：`module.register_forward_hook(fn)` 注册后，该模块每次前向结束都会调用 `fn(module, input, output)`——我们可以在 `fn` 里把 `output` 存起来。它与 4.1 的张量 hook 恰好是一对：

| | 张量 hook（`tensor.register_hook`） | 模块 forward hook（`module.register_forward_hook`） |
| --- | --- | --- |
| 挂载对象 | 张量（含中间激活） | `nn.Module` 层 |
| 触发时机 | 反向传播、关于该张量的梯度算出时 | 前向传播、该层算完输出时 |
| 回调入参 | 一个 `grad` 张量 | `(module, input, output)` 三元组 |
| 典型用途 | 抓中间梯度（4.1） | 抓中间激活、统计分布、可视化（本模块） |

#### 4.3.2 核心流程

```text
定义：MultilayerPerceptron 内部 self.net = Sequential(Linear, ReLU, Linear, ReLU, Linear)
前向：model(x) → forward(x) → self.net(x) 依次流过 5 个子模块 → logits
观察：model.net[2].register_forward_hook(hook)   # 给第 3 个子模块（下标 2）挂回调
触发：下一次 model(features) 前向 → 第 3 个模块执行完 → hook(module, input, output)
     → outputs.append(output)
```

`Sequential` 支持整数下标访问（`model.net[0]` 是第一个 Linear、`model.net[2]` 是第二个 Linear），这让「给某一层挂 hook」变得非常方便。还要注意 hook 一经注册就**持续生效**（直到调用返回的 `RemovableHandle.remove()`），所以每次前向都会向 `outputs` 追加一个元素——用它统计「每次前向」很合适，用完记得移除。

#### 4.3.3 源码精读

**Sequential 定义 784-128-256-10 MLP**

[pytorch_ipynb/mechanics/mlp-sequential.ipynb:L163-L174](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/mlp-sequential.ipynb#L163-L174)：

```python
self.net = torch.nn.Sequential(
    torch.nn.Linear(num_features, num_hidden_1),
    torch.nn.ReLU(inplace=True),
    torch.nn.Linear(num_hidden_1, num_hidden_2),
    torch.nn.ReLU(inplace=True),
    torch.nn.Linear(num_hidden_2, num_classes)
)
# ...
def forward(self, x):
    logits = self.net(x)
    probas = F.log_softmax(logits, dim=1)
    return logits, probas
```

与 u2-l3 的 `mlp-basic` 逐层手写版对比：结构完全相同（784-128-256-10），只是把五层的调用顺序交给 `Sequential` 接管，`forward` 缩成一行 `self.net(x)`。两个细节：`ReLU(inplace=True)` 直接原地改写输入张量以省一点显存；`forward` 仍返回 `(logits, probas)` 二元组，训练用 `F.cross_entropy(logits, targets)`（只要 logits），`compute_accuracy` 用 `probas` 取 argmax（`log_softmax` 的最大值索引与 `softmax` 一致）。训练循环（[L188-L459](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/mlp-sequential.ipynb#L188-L459)）仍是标准五步范式，测试准确率 97.82%（[L460-L476](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/mlp-sequential.ipynb#L460-L476)），与手写版的 97.54% 相当——定义方式变了，数学没变。

**查看层的顺序**

[pytorch_ipynb/mechanics/mlp-sequential.ipynb:L513](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/mlp-sequential.ipynb#L513) 直接打印 `model.net`，输出显示 5 个子模块及下标 0~4：`Linear(784,128)`、`ReLU`、`Linear(128,256)`、`ReLU`、`Linear(256,10)`。hook 挂谁，就靠这个下标。

**注册 forward hook**

[pytorch_ipynb/mechanics/mlp-sequential.ipynb:L540-L544](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/mlp-sequential.ipynb#L540-L544)：

```python
outputs = []
def hook(module, input, output):
    outputs.append(output)

model.net[2].register_forward_hook(hook)
```

回调签名固定为 `(module, input, output)`；这里只用了 `output`，把它追加进外部列表。注册调用返回一个 `RemovableHandle`（notebook 输出可见），需要撤销时对其调用 `.remove()`。

**触发并收集**

[pytorch_ipynb/mechanics/mlp-sequential.ipynb:L575-L577](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/mlp-sequential.ipynb#L575-L577)：

```python
_ = model(features)
print(outputs)
```

仅仅调用一次前向，`outputs` 里就出现了 `model.net[2]`（第二个 Linear）的输出张量：形状 `[64, 256]`。但细看会发现两处「反常」：一是大量精确的 `0.0000`（Linear 的原始输出应有正有负，不会整列恰为零）；二是 `grad_fn=<ThresholdBackward1>`——这是 **ReLU** 的反向节点名，而 Linear 输出的 grad_fn 本应是 `AddmmBackward`。原因藏在 [L165-L167](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/mlp-sequential.ipynb#L165-L167) 的 `ReLU(inplace=True)`：Sequential 继续执行到第 3 个子模块（下标 3 的 ReLU）时，会在 Linear 输出的**同一块内存**上原地截断负值，而 hook 存进 `outputs` 的正是这块内存的引用——打印时看到的已是「被下一层改写后」的张量。这是一个非常实用的教训：**inplace 操作会让 forward hook 拿到的输出被后续层污染**；若要拿到某一层的原始输出，应在 hook 里对 `output` 做 `.clone()` 快照，或避免使用 `inplace=True`。

> 顺带一个提醒：notebook 里 device 写的是 `torch.device("cuda:3" ...)`（作者机器的第 4 块 GPU），本地复现请按 u1-l2 的写法改成自动选择；另外 cell 里 hook 注册后未 remove，后续每次前向都会往 `outputs` 追加——自己写代码时注意在统计结束后移除。

#### 4.3.4 代码实践

**实践目标**：用 forward hook 打出整条 MLP 每一层的输出形状与统计量，直观建立「数据在网络里如何变形」的图像。

**操作步骤**（示例代码，非项目原有；基于本 notebook 的模型，也可用任意顺序模型）：

1. 训练或加载模型后，为 `model.net` 的**每个**子模块注册同一个 forward hook；

```python
stats = []
def stat_hook(module, inp, out):
    stats.append((module.__class__.__name__,
                  tuple(out.shape),
                  out.detach().mean().item(),
                  out.detach().std().item()))

handles = [m.register_forward_hook(stat_hook) for m in model.net]
_ = model(features)                     # 触发一次前向
for h in handles: h.remove()            # 用完移除
for name, shape, mu, sd in stats: print(name, shape, f"mean={mu:.4f}", f"std={sd:.4f}")
```

2. 换一个 batch 再跑一次，观察统计量的波动。

**需要观察的现象**：`stats` 恰好 5 条记录（hook 只挂在 `model.net` 的 5 个子模块上，输入本身不触发 hook），形状链 `64×128 → 64×128 → 64×256 → 64×256 → 64×10`（Linear 改维度、ReLU 原样通过）；ReLU 之后输出的均值明显为正、且最小值贴着 0。

**预期结果**：能对上「每个子模块一条记录、ReLU 不改变形状」的判断；两次不同 batch 的均值/标准差接近但不完全相同。

**待本地验证**：具体数值取决于权重与输入批次，以本地输出为准。

#### 4.3.5 小练习与答案

**练习 1**：`Sequential` 与手写逐层 `forward` 相比，得失各是什么？

**答案**：得——定义紧凑、层序一目了然、可用整数下标访问子模块（挂 hook、抽层都方便）；失——中间结果没有变量名，前向内部若有分支、拼接、残差加法等非顺序结构就表达不了（ResNet 的跳跃连接必须手写 forward，见 u3-l4），且默认拿不到中间激活（需 hook 补救）。

**练习 2**：`model.net[2].register_forward_hook(hook)` 中的 `hook` 什么时候被调用？入参是什么？

**答案**：在每次前向传播中，`model.net[2]`（第二个 Linear）算完输出时立即调用；入参依次为该模块本身、它的输入（元组）、它的输出。它在前向阶段触发，不涉及梯度。

**练习 3**：如何让 hook 在统计完成后停止追加？为什么建议这么做？

**答案**：保存 `handle = module.register_forward_hook(fn)` 返回的 `RemovableHandle`，统计完后调用 `handle.remove()`。不移除的话 hook 会伴随模型终身生效：每次前向都执行回调，既浪费算力，也可能让 `outputs` 列表无限增长、甚至无意间持有带计算图的张量导致显存无法释放（统计时配合 `out.detach()` 更稳妥）。

## 5. 综合实践

把本讲三块机制串成一个独立小实验（示例代码，不依赖 MNIST，CPU 即可）：

**任务**：对一个两层 MLP（如 `2 → 16 → 3`），(a) 分别用 `retain_grad()` 与张量 `register_hook` 捕获**第一个隐藏层输出**的梯度，验证两者数值一致；(b) 用 forward hook 统计**每层输出**的均值与方差；(c) 顺带回答：隐藏层梯度与隐藏层输出之间发生了什么运算才得到权重梯度？

参考骨架：

```python
import torch, torch.nn as nn, torch.nn.functional as F

torch.manual_seed(0)
model = nn.Sequential(nn.Linear(2, 16), nn.ReLU(), nn.Linear(16, 3))
x = torch.randn(8, 2); y = torch.randint(0, 3, (8,))

# (a) 两条路抓同一份梯度
h = model[0](x)                       # 手动前向第一层，拿到中间张量
h.retain_grad()                       # 路一：保留非叶子的 .grad
captured = {}
h.register_hook(lambda g: captured.__setitem__('h', g))   # 路二：张量 hook

# 继续走完剩余层（0 层已算过，跳过）
out = model[1](h); out = model[2](out)
loss = F.cross_entropy(out, y)
loss.backward()

print(torch.allclose(h.grad, captured['h']))   # 预期 True

# (b) forward hook 统计每层输出
stats = []
handles = [m.register_forward_hook(
    lambda mod, i, o: stats.append((mod.__class__.__name__, tuple(o.shape),
                                    o.detach().mean().item(), o.detach().var().item())))
    for m in model]
_ = model(x)
for hh in handles: hh.remove()
print(stats)   # 预期：Linear (8,16) / ReLU (8,16) / Linear (8,3) 各一条
```

**预期结果**：

1. `torch.allclose` 返回 `True`——两种方式拿到的是同一份反向梯度（这也正是规格实践要验证的结论：`retain_grad` 写 `.grad`，hook 直接递梯度值，殊途同归）；
2. `stats` 恰好三条记录，ReLU 层输出的方差通常小于其输入（负半轴被截为 0）；
3. (c) 的答案：\(\partial L/\partial W_1 = \delta_1^\top h_{in}\)，即「误差项（隐藏层输出处的梯度）与该层输入的外积/矩阵乘」——隐藏层输出处的梯度还要再乘 ReLU 的局部导数才继续往输入层传，这一链式结构正是 u2-l5 手工反向传播的四要素。

**待本地验证**：数值以本地运行为准；若 `allclose` 因浮点顺序差异返回 `False`，可改用 `torch.allclose(a, b, atol=1e-7)` 比较。

## 6. 本讲小结

- PyTorch 默认只为叶子张量保存 `.grad`；取中间梯度有三条路：`grad` 传元组一次遍历（最推荐）、`retain_graph=True` 逐个求（低效）、`retain_grad()` + `backward()`（最贴近训练代码）；张量 `register_hook` 也能抓梯度，但 notebook 自己注明那是「不推荐的 workaround」，其价值在于展示 hook 机制。
- `retain_graph` 管的是「图能否再次反向」，`retain_grad` 管的是「这个张量要不要记梯度」——一字之差，作用完全不同。
- 层内权重共享的高效实现：`Linear(in, 1, bias=False)` 把权重矩阵降为向量，`nn.Parameter` 手动定义逐单元偏置，`[N,1] + [num_classes]` 靠广播完成「复制」；数学上 \(\arg\max_j(w^\top h + b_j) = \arg\max_j b_j\)，预测与输入无关，故该演示在 MNIST 上恒定 11% 左右。
- `nn.Sequential` 适合纯顺序结构：定义紧凑、可下标访问子模块，但拿不到中间激活、表达不了分支（残差、拼接），需手写 `forward`。
- 两类 hook 要分清：张量 hook 反向时触发、入参是梯度；模块 forward hook 前向时触发、入参是 `(module, input, output)`。hook 注册后持续生效，用完应通过 `RemovableHandle.remove()` 移除。
- 同一模型换个定义方式，数学不变：Sequential 版 MLP 测试准确率 97.82%，与手写版 97.54% 相当。

## 7. 下一步学习建议

- **下一讲（u5-l1）**进入生成模型单元，从自编码器的 encoder/decoder 结构开始——其「编码提取 + 重建损失」的训练与本讲的五步范式完全一致，读起来会更轻松。
- 想继续深挖 hook：u7-l6 的模型可解释性讲义会用「对输入求梯度 + guided backprop 改写 ReLU 反向」把张量 hook 用到实战；届时回看本讲 4.1 的 `save_grad` 闭包，会觉得非常眼熟。
- 想看「Sequential 表达不了的结构」：直接读 u3-l4 ResNet 的 `BasicBlock` 与 `forward` 中的跳跃连接，对照本讲 4.3 练习 1 的结论。
- 若对「参数如何被登记与更新」感兴趣，可回到 u2-l3 复习 `nn.Module` 的参数登记机制，再与本讲 4.2 的手动 `nn.Parameter` 偏置互相印证。
