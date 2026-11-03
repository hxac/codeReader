# 多层感知机与 nn.Module

## 1. 本讲目标

上一讲(u2-l2)我们用**一个** `Linear(784, 10)` 层加 softmax,在 MNIST 上拿到了 91.77% 的测试准确率。这个模型本质是一条直线切 10 类——它看不见「像素组合成笔画、笔画组合成数字」这种层次结构。

本讲我们跨出关键一步:**把多个线性层堆起来,中间插入非线性激活函数,得到多层感知机(MLP)**。学完后你应该能够:

1. 掌握自定义 `torch.nn.Module` 的标准写法:`__init__` 里搭层、`forward` 里写前向传播。
2. 理解 784-128-256-10 这个结构如何一步步对应 MNIST 的 28×28 输入和 10 个类别,并会计算每层参数量。
3. 看懂 notebook 中「手工改写层权重初始化」的那两行 `detach().normal_()` 到底在做什么、为什么这样写是安全的。
4. 说清 `torch.optim.SGD` 如何接管参数更新,并把上一讲的五步训练循环完整套到 MLP 上。

本讲全部源码来自一个 notebook:`pytorch_ipynb/mlp/mlp-basic.ipynb`。这是仓库中最经典的「模型模板」之一——读懂它,后面 CNN、RNN 的 notebook 都只是换了 `__init__` 里的层。

## 2. 前置知识

本讲假设你已经完成 u2-l2(softmax 回归与第一个完整训练循环)。以下概念直接承接,不再从零解释:

- **MNIST 数据管线**:`datasets.MNIST` + `DataLoader`,`ToTensor()` 把像素压到 [0,1],图像批形状为 `[64, 1, 28, 28]`。
- **五步训练循环**:`forward → loss → zero_grad → backward → step`,以及 `F.cross_entropy` 只吃 logits(内部自带数值稳定的 log_softmax)。
- **展平**:`features.view(-1, 28*28)` 把 `[batch, 1, 28, 28]` 的图像批变成 `[batch, 784]` 的矩阵。

在此之上,补充三个本讲要用的新概念:

1. **张量的维度约定**。PyTorch 的 `nn.Linear(in_features, out_features)` 作用于二维张量 `[batch_size, in_features]`,输出 `[batch_size, out_features]`。所以图像必须先展平成 784 维向量才能喂给全连接层——这是「MLP 吃 MNIST」的全部秘密。

2. **激活函数(activation function)**。一个逐元素的非线性函数,比如 ReLU:

   \[ \text{ReLU}(z) = \max(0, z) \]

   没有它,堆多少个线性层都等价于一个线性层(下一节证明),MLP 就退化回 softmax 回归。

3. **模块(module)与参数(parameter)**。PyTorch 把「带可学习权重的一段计算」抽象成 `nn.Module`。`nn.Linear` 是最简单的模块,内部持有 `weight` 和 `bias` 两个参数张量。我们自定义的 `MultilayerPerceptron` 也是模块——模块可以嵌套模块,整棵树上的参数用 `model.parameters()` 一次全拿到。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [pytorch_ipynb/mlp/mlp-basic.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb) | 本讲唯一主角:784-128-256-10 的 MLP 在 MNIST 上的完整实现(设置 → 模型 → 训练 → 评估),10 epoch 后测试准确率 97.54% |

`pytorch_ipynb/mlp/` 目录下还有三个 notebook,本讲不展开,但会在第 7 节预告:

- `mlp-dropout.ipynb` / `mlp-batchnorm.ipynb`:在同一个骨架上加正则化(u2-l4)。
- `mlp-fromscratch__sigmoid-mse.ipynb`:不用 `nn.Module`,手工实现反向传播(u2-l5)。

> **关于行号的说明**:notebook 在磁盘上是 JSON 文件,本文引用的行号是该 JSON 文件的真实行号,与 GitHub 上的 `#L` 锚点一致。由于 JSON 中每个 cell 的源码是连续存放的,单个代码段内部的行号连续可靠;跨 cell 引用时我们会分别给出。

## 4. 核心概念与源码讲解

### 4.1 nn.Module 与 forward:自定义模型的骨架

#### 4.1.1 概念说明

`torch.nn.Module` 是 PyTorch 所有模型的基类。自定义模型只有两条义务:

- 在 `__init__(self, ...)` 中**创建带参数的层**,并把它们存为 `self.xxx` 属性。父类会自动登记这些子模块,`model.parameters()` 因此能递归找到全部参数。
- 在 `forward(self, x)` 中**描述一次前向计算**:输入 `x` 如何流过这些层,得到什么输出。

第一行必须调用 `super().__init__()`——它初始化了父类内部的参数登记簿。漏掉这行,后面给 `self.linear_1` 赋值时 PyTorch 会直接报错,因为它不知道往哪里登记。

一个容易困惑的点:我们几乎从不直接调用 `model.forward(x)`,而是写 `model(x)`。原因是基类的 `__call__` 运算符会在调用 `forward` 前后**自动执行注册的 hook**(u4-l5 会用到这个机制)。养成写 `model(x)` 的习惯即可。

#### 4.1.2 核心流程

一个 `nn.Module` 从定义到使用的生命周期:

```text
class MyModel(nn.Module):
    __init__: super().__init__() → 创建层属性(self.linear_1 = ...)
              ↓ 登记到父类的参数树
forward(x):  纯计算描述,不引入新参数
              ↓
实例化:      model = MyModel(超参...) → 权重此刻被初始化
              ↓
迁移设备:    model = model.to(device) → 所有参数一起搬到 GPU/CPU
              ↓
调用:        logits, probas = model(x) → 自动经 __call__ 进入 forward
```

#### 4.1.3 源码精读

模型类的骨架在 [pytorch_ipynb/mlp/mlp-basic.ipynb:L158-L161](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L158-L161):`MultilayerPerceptron` 继承 `torch.nn.Module`,`__init__` 只接收 `num_features` 和 `num_classes` 两个结构参数(注意:两个隐藏层的宽度 `num_hidden_1`、`num_hidden_2` 是从 cell 顶部的全局变量读的,notebook 为了简洁没有作为参数传入),`super().__init__()` 打开参数登记簿。

`forward` 在 [pytorch_ipynb/mlp/mlp-basic.ipynb:L183-L190](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L183-L190):输入 `x` 依次经过三个线性层和两个 ReLU,最后返回 **`logits, probas` 两个值**——训练时把 `logits` 喂给 `F.cross_entropy`(上一讲强调过:它只吃 logits),评估时用 `probas` 做 `argmax`。一个 forward 同时满足两种用途,这是仓库 notebook 的常见写法。

实例化三连在 [pytorch_ipynb/mlp/mlp-basic.ipynb:L193-L199](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L193-L199):

1. `torch.manual_seed(random_seed)`——在**创建模型之前**固定随机种子,让权重初始化可复现(承接 u1-l3 关于随机性的结论:种子必须在建模型前设)。
2. `model = MultilayerPerceptron(num_features=num_features, num_classes=num_classes)`——权重在此刻生成。
3. `model = model.to(device)`——注意 `to()` 返回的是新引用,必须重新赋值给 `model`,否则参数还在原设备上。

#### 4.1.4 代码实践

**实践目标**:验证「属性即登记」机制——确认 `model.parameters()` 能自动收集所有层的权重和偏置。

**操作步骤**(在 notebook 的 MODEL cell 之后新建一个 cell,标注为示例代码):

```python
# 示例代码:检查参数登记
model_tmp = MultilayerPerceptron(num_features=784, num_classes=10)
for name, param in model_tmp.named_parameters():
    print(f'{name:24s} | shape: {tuple(param.shape)}')
```

**需要观察的现象**:打印出 6 个条目(`linear_1.weight`、`linear_1.bias`、`linear_2.weight`、`linear_2.bias`、`linear_out.weight`、`linear_out.bias`)。

**预期结果**:每个 `Linear` 层贡献恰好 2 个参数张量,名称就是「属性名 + `.weight`/`.bias`」。这说明 `__init__` 里的 `self.xxx = nn.Linear(...)` 赋值本身就是登记动作,无需任何额外注册代码。(本实践只需构造模型,不需要训练,可直接运行验证。)

#### 4.1.5 小练习与答案

**练习 1**:如果删掉 `__init__` 里的 `super(MultilayerPerceptron, self).__init__()` 这一行,会发生什么?

**答案**:实例化时抛出 `AttributeError` 或 `KeyError`(取决于 PyTorch 版本,常见提示参数登记相关属性不存在)。因为 `self.linear_1 = ...` 赋值会触发父类的 `__setattr__`,它依赖 `__init__` 里初始化好的参数容器;父类容器没建好,赋值就失败。

**练习 2**:为什么 notebook 里写 `model(features)` 而不是 `model.forward(features)`?

**答案**:`model(features)` 走基类的 `__call__`,它会在调用 `forward` 的同时执行所有已注册的 forward hook(以及 `nn.Module` 内部的其他机制);直接调 `forward` 会绕过这些机制。现在两者结果相同,但 hook 在调试和可视化(如 u4-l5 抓中间层梯度)时是关键工具,标准写法是 `model(x)`。

---

### 4.2 隐藏层与激活函数:784-128-256-10 如何吃下 MNIST

#### 4.2.1 概念说明

先看结构:三个数字定义了整张网络的「水管口径」。

```text
输入 784 (28×28 展平)
   ↓ linear_1: Linear(784, 128)     ← 第 1 个隐藏层,宽 128
   ↓ ReLU
   ↓ linear_2: Linear(128, 256)     ← 第 2 个隐藏层,宽 256
   ↓ ReLU
   ↓ linear_out: Linear(256, 10)    ← 输出层,10 类 logits
```

结构超参集中写在 [pytorch_ipynb/mlp/mlp-basic.ipynb:L111-L114](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L111-L114):`num_features = 784`、`num_hidden_1 = 128`、`num_hidden_2 = 256`、`num_classes = 10`。784 = 28×28 是 MNIST 图像展平后的维度,10 是数字类别数。

**为什么需要激活函数?** 这是本模块最重要的一个数学事实。两个不带激活的线性层复合:

\[ \text{Layer}_2(\text{Layer}_1(x)) = W_2(W_1 x + b_1) + b_2 = (W_2 W_1)x + (W_2 b_1 + b_2) = W'x + b' \]

复合结果仍是一个线性层(\(W' = W_2 W_1\),\(b' = W_2 b_1 + b_2\))。推而广之,**不加非线性,任意深的线性网络等价于单层线性模型**,表达能力不会超过上一讲的 softmax 回归。ReLU 这种逐元素非线性打破了这条恒等式,让 MLP 能拟合弯曲的决策边界。

**宽度为什么是 128、256?** 没有深奥理由——它们是常见的 2 的幂,经验上够用且不贵。真正硬性的约束只有首尾:第一层必须吃 784 维,最后一层必须吐 10 维;中间层宽度可以自由选择(综合实践里你就会改它)。

#### 4.2.2 核心流程

一个 batch `[64, 1, 28, 28]` 的完整旅程:

```text
features.view(-1, 28*28)        # [64, 784]   展平(在训练循环里做,不在 forward 里)
  → linear_1(x)                # [64, 784] × [784, 128] → [64, 128]
  → F.relu(out)                # [64, 128]   逐元素 max(0, z),负值归零
  → linear_2(out)              # [64, 128] × [128, 256] → [64, 256]
  → F.relu(out)                # [64, 256]
  → linear_out(out)            # [64, 256] × [256, 10] → [64, 10]  logits
  → F.log_softmax(logits, 1)   # [64, 10]    对数概率(每行沿 dim=1 归一)
```

维度对齐规则:**上一层的 `out_features` 必须等于下一层的 `in_features`**,否则矩阵乘法报维度错误。这就是 `Linear(num_features, num_hidden_1)` → `Linear(num_hidden_1, num_hidden_2)` → `Linear(num_hidden_2, num_classes)` 首尾相接的链条。

每层参数量 = 权重 + 偏置 = \(in \times out + out\):

| 层 | 形状 | 参数量 |
| --- | --- | --- |
| linear_1 | 784→128 | 784×128 + 128 = 100,480 |
| linear_2 | 128→256 | 128×256 + 256 = 33,024 |
| linear_out | 256→10 | 256×10 + 10 = 2,570 |
| **合计** | | **136,074** |

对比上一讲 softmax 回归的 7,850 个参数(784×10+10),MLP 多了 17 倍参数,换来测试准确率从 91.77% 到 97.54% 的跨越。

#### 4.2.3 源码精读

三个 `Linear` 层的创建在 [pytorch_ipynb/mlp/mlp-basic.ipynb:L163-L181](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L163-L181):`self.linear_1 = torch.nn.Linear(num_features, num_hidden_1)`([L164](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L164))、`self.linear_2 = torch.nn.Linear(num_hidden_1, num_hidden_2)`([L174](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L174))、`self.linear_out = torch.nn.Linear(num_hidden_2, num_classes)`([L179](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L179))——三个维度首尾相接,其余行是权重初始化(4.3 节精读)。

前向传播链在 [pytorch_ipynb/mlp/mlp-basic.ipynb:L183-L190](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L183-L190):`forward` 用中间变量 `out` 逐层传递,`F.relu` 穿插在线性层之间;末尾 `probas = F.log_softmax(logits, dim=1)`([L189](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L189))沿类别维(dim=1)求对数概率,返回 `(logits, probas)` 二元组。

一个值得注意的细节:评估函数里 `_, predicted_labels = torch.max(probas, 1)`([L434](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L434))拿的是 `probas`——它是 **log** softmax(对数概率)。由于 log 是单调函数,对 log 概率取 argmax 与对概率取 argmax 结果完全一致,所以这里不影响正确性;但如果下游需要真正的概率值,必须再过一次 `.exp()`。

#### 4.2.4 代码实践

**实践目标**:亲眼确认「去掉 ReLU,网络坍缩为线性」不是一句空话。

**操作步骤**(示例代码,在 MODEL cell 之后运行):

```python
# 示例代码:ReLU 的必要性演示
torch.manual_seed(1)
m = MultilayerPerceptron(num_features=784, num_classes=10)
x = torch.randn(8, 784)

with torch.no_grad():
    # (a) 正常前向
    logits_normal, _ = m(x)
    # (b) 绕过 ReLU 的前向:只保留三条矩阵乘法链
    z = m.linear_1(x)          # [8, 128]
    z = m.linear_2(z)          # [8, 256]  ← 直接吃上一层输出,不过 ReLU
    logits_linear = m.linear_out(z)

print(logits_normal.shape, logits_linear.shape)
print('两个输出最大差异(正常 vs 无ReLU):',
      (logits_normal - logits_linear).abs().max().item())
```

**需要观察的现象**:(b) 的输出仍然是 `[8, 10]` 的合法 logits——网络照样能跑、照样能训练;但它是三个矩阵连乘,本质是单个线性层。

**预期结果**:打印出两个输出各自的形状和最大差异。差异不为零(因为 (a) 中经过了 ReLU)。再把 (b) 的三步合并成一次 `torch.nn.Linear(784, 10)` 并对比——(b) 与合并后的单层输出应当一致(合并权重 \(W' = W_3 W_2 W_1\),可作为进阶验证)。这个实践揭示:去掉 ReLU 程序不会报错,模型却悄悄失去了表达能力——这种「静默退化」比报错更危险。

#### 4.2.5 小练习与答案

**练习 1**:把 `linear_2` 误写成 `torch.nn.Linear(num_hidden_2, num_hidden_1)`(即 256→128),程序会在哪一步报错?

**答案**:不在 `__init__`(创建层时不检查维度匹配),而在第一次前向传播:`forward` 中 `self.linear_2(out)` 收到 `[batch, 128]` 的输入(`linear_1` 的输出是 128 维),却期望 256 维输入,矩阵乘法抛出尺寸不匹配的 `RuntimeError`。教训:维度错误往往在「跑起来」那一刻才暴露。

**练习 2**:该 MLP 一个 batch(64 张图)前向传播后,`probas` 每一行的和是多少?`probas.exp()` 每行和又是多少?

**答案**:`probas = log_softmax(...)` 的每行是**对数**概率,取 exp 后每行和为 1;`probas` 本身的每行和没有固定值(是 log 概率的和,恒 ≤ 0 且随分布变化)。所以做概率语义的操作(如按概率加权)前要先 `.exp()`。

**练习 3**:为什么第一个隐藏层 128 维、第二个反而加宽到 256 维?换成 784-256-128-10 会发生本质变化吗?

**答案**:不会发生本质变化——中间宽度是自由设计空间,两个方向的网络都能正常训练,只是表达能力与参数量不同。「先窄后宽」或「先宽后窄」都是常见做法(后者像漏斗,更常见于输出层之前收窄);仓库这里的选择属于作者偏好,不是定理。

---

### 4.3 权重初始化:默认 Xavier 与手工 normal_

#### 4.3.1 概念说明

每个 `nn.Linear` 创建的瞬间,它的 `weight` 和 `bias` 就被赋予了初始数值。初始化很重要:同一结构、不同初始化,训练的起点和终点都可能不同(上一讲我们见过「全零初始化使首个损失恰为 ln 10」的现象)。

notebook 源码注释([L165-L169](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L165-L169))明确说明:PyTorch 默认的 Xavier/Glorot 初始化「通常应当优先使用」,那两行手工初始化**不是更优方案**,而是作者在演示「如何访问并改写一层的权重」这项技能本身。需要提醒的是:不同 PyTorch 版本对 `Linear` 默认初始化的具体实现有差异(注释写作时是 torch 1.0;较新版本内部采用 Kaiming-uniform 的变体),但共同点是——权重都是量级与层的 fan-in/fan-out 挂钩的小随机值,bias 也取小随机值。关键是:**默认值已经足够好,不要无理由地改**。

改初始化的正确姿势是:

```python
self.linear_1.weight.detach().normal_(0.0, 0.1)   # 均值 0、标准差 0.1 的高斯
self.linear_1.bias.detach().zero_()               # 偏置全部清零
```

两个语法点:

- `normal_(0.0, 0.1)` / `zero_()` 是**下划线结尾的原地操作**,直接改写张量里的数值,不产生新张量。
- `.detach()` 先返回一个**脱离计算图**的张量视图再改。在 `__init__` 里此刻还没有计算图,这一步是防御性写法;但当你想在训练中途改权重时,不带 `detach` 的原地修改会让 autograd 报错(修改的是被求导追踪的张量)。这是仓库里值得学的标准安全模式。

#### 4.3.2 核心流程

```text
Linear(in, out) 构造
  → PyTorch 自动用默认初始化填 weight 与 bias(作者注释称 Xavier/Glorot;
    新版实现为 Kaiming-uniform 变体,量级均与 fan-in/fan-out 挂钩)
  → (可选) detach() 拿到脱离计算图的引用
  → normal_(μ, σ) / zero_() 原地覆盖初始值
  → __init__ 结束,权重定型,等待 torch.manual_seed 保证可复现
```

为什么固定 `torch.manual_seed(random_seed)`([L193](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L193))能复现初始化?因为 `normal_` 从 PyTorch 全局随机数生成器抽样;种子在建模型之前设定,抽样序列就固定了。这呼应 u1-l3 的结论:种子要覆盖所有随机源且在建模型前调用(`helper_utils.set_all_seeds` 是它的增强版,连 NumPy/random 一起固定)。

#### 4.3.3 源码精读

手工初始化的完整示范在 [pytorch_ipynb/mlp/mlp-basic.ipynb:L163-L181](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L163-L181)。以第一层为例,[L164-L171](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L164-L171) 先创建 `linear_1`,紧跟四行注释说明「下面两行并非必要,只为演示如何访问权重并使用不同的初始化;PyTorch 默认 Xavier/Glorot 通常应优先」,然后 [L170](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L170) 用 `detach().normal_(0.0, 0.1)` 把权重换成均值 0、标准差 0.1 的高斯,[L171](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L171) 把偏置清零。

第二层与输出层完全同构:[L174-L177](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L174-L177)(`linear_2` 创建 + normal_ + zero_)、[L179-L182](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L179-L182)(`linear_out` 同样处理)。三层统一 std=0.1 的高斯初始化,是那个年代 MLP 的常见经验设置。

#### 4.3.4 代码实践

**实践目标**:对比「默认初始化」与「notebook 的 normal_(0, 0.1)」两种初始化的数值分布差异。

**操作步骤**(示例代码):

```python
# 示例代码:观察两种初始化
torch.manual_seed(1)
layer_default = torch.nn.Linear(784, 128)          # 保持 PyTorch 默认初始化

torch.manual_seed(1)
layer_custom = torch.nn.Linear(784, 128)
layer_custom.weight.detach().normal_(0.0, 0.1)     # 手工覆盖
layer_custom.bias.detach().zero_()

for name, layer in [('default', layer_default),
                    ('custom normal(0,0.1)', layer_custom)]:
    w = layer.weight.detach()
    print(f'{name:22s} weight std={w.std():.4f}  '
          f'min={w.min():.4f}  max={w.max():.4f}  bias[:3]={layer.bias[:3].tolist()}')
```

**需要观察的现象**:两种初始化的权重标准差数量级不同;默认版 bias 是非零的小随机数,手工版 bias 全零。

**预期结果**:默认初始化的权重 std 明显小于 0.1(量级在 10⁻²,具体数值随 fan-in/fan-out 与 PyTorch 版本变化);custom 版 std ≈ 0.1(高斯分布,范围更宽),bias 恰为 0。两版的对比结论——「notebook 的手工初始化让权重分布更宽、偏置归零」——不随版本改变;精确数值请以你本地输出为准。

#### 4.3.5 小练习与答案

**练习 1**:把 `self.linear_1.weight.detach().normal_(0.0, 0.1)` 里的 `.detach()` 去掉,在 `__init__` 阶段程序还能正常工作吗?什么时候会出问题?

**答案**:在 `__init__` 阶段仍然能正常工作——此刻权重还没参与任何计算、未被 autograd 追踪为「被使用」的张量,原地修改不报错。会出问题的场景是**训练中途**直接改权重:那时权重已经参与过前向计算、被追踪,原地修改会破坏计算图,autograd 抛错。因此 `detach()` 是让「改权重」在任何阶段都安全的习惯写法。

**练习 2**:如果把这个网络的权重**全部**初始化为 0(而不是高斯),训练还能成功吗?

**答案**:不能正常训练。全零权重下,同层所有神经元的输出、梯度完全相同,反向传播时它们永远保持同步更新——网络退化为「每层只有一个有效神经元」,学不到有用特征。这正是初始化需要**随机性**的原因:打破对称。(对比:上一讲 softmax 回归全零初始化可行,是因为它没有隐藏层、没有对称性问题。)

---

### 4.4 SGD 优化器与训练循环落地

#### 4.4.1 概念说明

优化器(optimizer)负责「拿着梯度改参数」。手工时代的更新规则是 \(w \leftarrow w - \eta \nabla_w L\)(u2-l1 的感知机就是这么写的);`torch.optim.SGD` 把这件事自动化:

```python
optimizer = torch.optim.SGD(model.parameters(), lr=learning_rate)
```

传入 `model.parameters()` 后,优化器持有全部 136,074 个参数的引用;每次 `optimizer.step()` 执行:

\[ \theta \leftarrow \theta - \eta \cdot \nabla_{\theta} L \]

其中 \(\eta = 0.1\)(本 notebook 的 `learning_rate`)。这就是**随机**梯度下降:每个 mini-batch(64 个样本)估计一次梯度、更新一次参数。

优化器与模型是**解耦**的:换 `torch.optim.Adam(model.parameters(), lr=...)` 一行就能切换优化算法,模型代码零改动。这也是「五步循环」里 `zero_grad` 存在的原因(承接 u2-l2):PyTorch 的梯度默认累加,必须每个 batch 手动清零,否则上一批的梯度会混进本批更新。

另一组关键 API 是 `model.train()` / `model.eval()`:切换模块的训练/评估模式。对本讲这个纯 Linear+ReLU 的 MLP,**两者行为完全相同**(没有 Dropout、BatchNorm 这类模式敏感层);但 notebook 已经按规范写了,因为后续的 `mlp-dropout`/`mlp-batchnorm`(u2-l4)里忘切模式会造成评估结果错误——从第一天就养成习惯。

#### 4.4.2 核心流程

本 notebook 的训练循环结构(承接 u2-l2 的范式,这里只列骨架):

```text
for epoch in 1..10:                       # 外层:遍历数据集 10 遍
    model.train()                         # 切训练模式
    for batch in train_loader:            # 内层:938 个 batch(60000/64)
        features.view(-1, 784).to(device) # 展平 + 上设备
        ① logits, probas = model(features)     # forward
        ② cost = F.cross_entropy(logits, targets)  # loss(吃 logits)
        ③ optimizer.zero_grad()                # 清上一批梯度
        ④ cost.backward()                      # 反向传播
        ⑤ optimizer.step()                     # SGD 更新参数
    每 50 个 batch 打印一次 cost
    每个 epoch 结束后算一次训练准确率
```

评估在 `net.eval()` + `torch.no_grad()` 下进行——双保险:前者切模块模式,后者关掉梯度追踪省内存。

#### 4.4.3 源码精读

优化器的创建在 [pytorch_ipynb/mlp/mlp-basic.ipynb:L199](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L199):`torch.optim.SGD(model.parameters(), lr=learning_rate)`,学习率 0.1 来自 [L106](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L106) 的超参设置(cell 顶部 [L98-L108](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L98-L108) 还定义了 device、`num_epochs = 10`、`batch_size = 64`)。

五步训练循环在 [pytorch_ipynb/mlp/mlp-basic.ipynb:L441-L456](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L441-L456):`model.train()` 切模式([L442](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L442)),`features.view(-1, 28*28).to(device)` 在循环内展平上设备([L445](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L445));随后就是五步——forward([L449](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L449))、`F.cross_entropy`([L450](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L450))、`zero_grad`([L451](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L451))、`backward`([L453](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L453))、`step`([L456](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L456))。与上一讲逐字相同的骨架,只是 `model` 换成了更深的 MLP。

评估函数在 [pytorch_ipynb/mlp/mlp-basic.ipynb:L426-L437](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L426-L437):`compute_accuracy` 先 `net.eval()`、再在 `torch.no_grad()` 下遍历 loader,`torch.max(probas, 1)` 取每行最大值下标为预测标签([L434](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L434)),累计正确数除以样本数。

最终评估在 [pytorch_ipynb/mlp/mlp-basic.ipynb:L488](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mlp/mlp-basic.ipynb#L488):`print('Test accuracy: %.2f%%' % ...)`,notebook 保存的运行输出为 **97.54%**(训练准确率约 99.3%,存在轻微过拟合——u2-l4 的正则化主题正由此展开)。

#### 4.4.4 代码实践

**实践目标**:体会学习率对 SGD 的影响——同一模型、同一种子,只动 \(\eta\)。

**操作步骤**:

1. 复制 `mlp-basic.ipynb` 为 `mlp-basic-lr-scan.ipynb`(只改副本,不动原文件)。
2. 保持 `random_seed = 1` 不变,分别把 `learning_rate` 设为 `0.01`、`0.1`(原值)、`1.0`,`num_epochs` 缩为 3 以节省时间,各跑一遍。
3. 记录每个学习率下第 1 个 epoch 前 100 个 batch 的 cost 曲线与最终测试准确率。

**需要观察的现象**:cost 下降的快慢与稳定性;\(\eta=1.0\) 时 cost 可能震荡甚至变大(发散),\(\eta=0.01\) 时下降明显更慢。

**预期结果**:\(\eta\) 过小收敛慢,\(\eta\) 过大不稳定,0.1 附近表现良好(这也是作者选它的原因)。具体数值**待本地验证**(结果受硬件与 PyTorch 版本影响)。

#### 4.4.5 小练习与答案

**练习 1**:`optimizer.zero_grad()` 放在 `cost.backward()` 之后、`optimizer.step()` 之前行不行?

**答案**:绝对不行——这样会把**刚刚算出来的梯度**清零,`step()` 就没有梯度可用,参数永不更新(cost 纹丝不动)。清零必须发生在 backward 之前(本 notebook 放在 forward/loss 之后、backward 之前,是标准位置)。

**练习 2**:这个 MLP 上 `model.train()` 与 `model.eval()` 的行为有区别吗?为什么 notebook 还是两个都写了?

**答案**:没有区别——网络只含 `Linear` 和 `ReLU`,两者都不受模式影响。仍要写,因为 `Dropout`(随机失活只在训练生效)和 `BatchNorm`(训练用批统计、评估用 running stats)在 u2-l4 就会出现;而且 `compute_accuracy` 是通用函数,会服务于后面所有模型。规范写法是训练前 `train()`、评估前 `eval()`。

**练习 3**:notebook 里 `optimizer` 只在模型创建后构造一次,每个 epoch 循环里都没有重建。如果第 5 个 epoch 时你新建了一个 `optimizer2 = torch.optim.SGD(model.parameters(), lr=0.01)` 并改用它,训练还能继续吗?

**答案**:能继续——两个优化器各自持有同一批参数的引用和独立的动量类状态(SGD 默认无动量,`step()` 就是纯梯度下降),切换优化器不会丢失模型权重本身;但学习率从该 batch 起变为 0.01。反过来要注意:如果**重新创建模型**而不重建优化器,旧优化器引用的还是旧模型的参数,新模型将得不到任何更新。

---

## 5. 综合实践

把本讲四个模块串成一个任务:**改造网络结构并量化其代价与收益**。

1. **准备**:复制 `mlp-basic.ipynb` 为工作副本(不要改原文件)。完整跑通一遍,确认你能复现 97.54% 量级的测试准确率(数值随环境浮动属正常)。
2. **改结构**:把网络从 2 个隐藏层(784-128-256-10)改为 3 个隐藏层(784-256-128-64-10)。需要同步修改:
   - 结构超参区:`num_hidden_1 = 256`、`num_hidden_2 = 128`,新增 `num_hidden_3 = 64`;
   - `__init__`:新增 `self.linear_3 = torch.nn.Linear(num_hidden_2, num_hidden_3)`(以及你选择的初始化);
   - `forward`:在第二个 ReLU 之后插入 `out = self.linear_3(out)` 与 `out = F.relu(out)`,再接输出层。
3. **数参数**(4.1/4.2 模块技能):

   ```python
   # 示例代码
   total = sum(p.numel() for p in model.parameters())
   print(f'总参数量: {total:,}')
   ```

   手工核算:784×256+256 = 200,960;256×128+128 = 32,896;128×64+64 = 8,256;64×10+10 = 650;**合计 242,762**(约为原 136,074 的 1.8 倍)。确认程序输出与手工计算一致。
4. **对比训练**:同种子同超参(lr=0.1、10 epoch、batch 64)训练新结构,记录测试准确率,与原结构对比。
5. **写结论**:回答——参数量接近翻倍,准确率提升了多少个百分点?这笔「参数换精度」的交易划算吗?(提示:从 91.77% → 97.54% 用了 13 万参数,从 97.54% 再往上呢?这为 u3 引入 CNN 的「参数高效」视角埋下伏笔。)

**预期结果**(准确率部分待本地验证):三隐藏层版本通常与原版相差在 ±0.3% 内——MLP 在 MNIST 上很快饱和,单纯加深加宽收益递减。能得出这个观察本身就是本实践的收获。

## 6. 本讲小结

- 自定义模型 = 继承 `nn.Module` + `__init__` 里创建层(赋值即登记)+ `forward` 里描述计算;调用时写 `model(x)` 而非 `model.forward(x)`。
- 784-128-256-10 的维度链条由数据决定首尾(28×28=784 输入、10 类输出),中间宽度自由;相邻层 `out_features` 必须等于下一层 `in_features`。
- 没有非线性激活(ReLU),任意深的线性网络坍缩为单层线性模型 \((W_2W_1)x + (W_2b_1+b_2)\);MLP 的表达能力来自「线性 + 非线性」的交替堆叠。
- `nn.Linear` 自带与 fan-in/fan-out 挂钩的默认初始化(作者注释称 Xavier/Glorot,新版实现略有差异);notebook 用 `weight.detach().normal_(0.0, 0.1)` + `bias.detach().zero_()` 演示安全改写权重的方法,`detach()` 是原地修改参数的标准护栏。
- `torch.optim.SGD(model.parameters(), lr)` 接管参数更新,五步循环(forward→loss→zero_grad→backward→step)与上一讲完全一致——本讲只换了模型,没换范式。
- 结果对比:softmax 回归 7,850 参数 / 91.77%,MLP 136,074 参数 / 97.54%;训练准确率 99.3% 高于测试 97.54%,过拟合初现,u2-l4 用 Dropout 与 BatchNorm 应对。

## 7. 下一步学习建议

下一讲(u2-l4)直接处理本讲结尾暴露的过拟合问题:同目录的 `mlp-dropout.ipynb` 讲解 Dropout 的随机失活与 `train()/eval()` 模式切换(那时本讲的 `model.train()` 才真正「有行为」),`mlp-batchnorm.ipynb` 讲解 BatchNorm1d 的批统计与 running statistics。

如果想先巩固本讲,推荐两条路:

- **向内深挖**:读 `pytorch_ipynb/mlp/mlp-fromscratch__sigmoid-mse.ipynb`(u2-l5),看不用 `nn.Module`、手工写前向与反向传播时,`cost.backward()` 背后到底发生了什么。
- **向外扩展**:浏览 `pytorch_ipynb/cnn/cnn-basic.ipynb` 的 MODEL cell(u3-l1),你会发现 CNN 模型类的骨架与本讲的 `MultilayerPerceptron` 逐行同构——只是把 `Linear` 换成了 `Conv2d`。掌握 `nn.Module` 这一套,等于掌握了仓库半数以上 notebook 的模型定义方式。
