# Softmax 回归与第一个完整训练循环

## 1. 本讲目标

学完本讲，你应该能够：

1. 用 `datasets.MNIST` + `DataLoader` 搭出最小可用的数据管线，并说清楚每个参数（`train`、`transform`、`download`、`shuffle`）的作用。
2. 写出 softmax 函数与交叉熵损失的数学定义，解释为什么 `F.cross_entropy` 接收的是 logits 而不是概率。
3. 逐行解释「forward → loss → zero_grad → backward → step」标准训练循环中每一行代码在做什么，以及为什么少了 `zero_grad` 训练就会失败。
4. 用一个带 `predict` 方法的包装类，把 PyTorch 模型接到 mlxtend 的 `plot_decision_regions` 上，直观看到三分类的决策边界。

本讲是整个学习手册的「枢纽站」：上一讲（u2-l1）我们在玩具数据上见到了五步训练范式的雏形，本讲把它放进真实的 MNIST 数据集和完整的 epoch/batch 双层循环里——这套循环结构几乎原样出现在本仓库后续所有 notebook 中，读懂它就拿到了阅读其余 100 多个 notebook 的钥匙。

## 2. 前置知识

本讲默认你已读过前三讲的以下结论（忘记的话建议先翻回来看一眼）：

- **线性打分**（u2-l1）：感知机和逻辑回归共享同一个"身体"——用 \( z = w^\top x + b \) 给样本打分。本讲只是把"一个分数"扩展成"K 个分数"。
- **逻辑回归的梯度**（u2-l1）：对权重的梯度形如 \( (p - y)x \)，其中 p 是预测概率、y 是真实标签。本讲会看到多分类版本恰好是 \( p_j - y_j \)，形式完全一致。
- **五步训练范式**（u2-l1）：forward → loss → zero_grad → backward → step。本讲逐行展开。
- **梯度累加与清零**（u2-l1）：PyTorch 的 `backward()` 会把梯度**累加**到 `.grad` 上，所以每轮更新前必须 `zero_grad()`。
- **device 规则**（u1-l2）：参与同一运算的张量必须在同一 device 上，标准写法是 `torch.device("cuda:0" if torch.cuda.is_available() else "cpu")`。
- **五段式结构**（u1-l2）：仓库 notebook 都是「Imports → Settings/Dataset → Model → Training → Evaluation」五段，本讲的两份 notebook 完全遵守。
- **DataLoader 一词**（u1-l3）：helper 模块里出现过 `get_dataloaders_mnist`，本讲讲解它内部用到的最原始用法。
- **数据泄漏**（u2-l1）：标准化只能用训练集的统计量。本讲 mlxtend notebook 里会再次看到这一原则。

对初学者的新名词，先给一句白话解释：

| 术语 | 白话解释 |
|---|---|
| MNIST | 70000 张 28×28 像素的手写数字（0-9）灰度图，深度学习的"Hello World"数据集 |
| epoch | 把训练集完整过一遍 |
| batch | 一次送进模型的一小撮样本（如 256 张图） |
| iteration | 处理一个 batch 的一轮前向+反向（60000 样本 ÷ 256 ≈ 235 个 iteration/epoch） |
| logits | 线性层输出的、还没归一化的 K 个"原始分数" |
| softmax | 把 K 个分数压成"和为 1"的概率分布的函数 |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [pytorch_ipynb/basic-ml/softmax-regression.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/softmax-regression.ipynb) | 主线 notebook：MNIST 上的 softmax 回归，含标准训练循环与准确率评估 |
| [pytorch_ipynb/basic-ml/softmax-regression-mlxtend-1.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/softmax-regression-mlxtend-1.ipynb) | 姊妹篇：Iris 数据上的同款模型，演示用 `ModelWrapper` 对接 mlxtend 的决策边界可视化 |
| [pytorch_ipynb/data/iris.data](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/data/iris.data) | 150 行鸢尾花数据，第 3、4 列是花瓣长/宽，第 5 列是类别名（mlxtend notebook 用相对路径 `../data/iris.data` 读它） |

> **关于行号的说明**：`.ipynb` 文件本质是 JSON，本讲所有永久链接的行号都指向该 JSON 文件的原始行（即 GitHub 上打开 notebook 后切换到 code 视图能看到的位置）。行号基于当前 HEAD `59beda3`，已逐条核对。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：**MNIST 与 DataLoader** → **softmax 与交叉熵** → **标准训练循环** → **mlxtend 决策边界**。

### 4.1 MNIST 与 DataLoader

#### 4.1.1 概念说明

`torchvision.datasets.MNIST` 是一个现成的 Dataset 类：第一次调用会自动从网上下载 MNIST 到本地 `data/` 目录，之后每次直接读本地缓存。它负责回答两个问题——"这个数据集有多少样本"和"第 i 个样本是什么"。

但深度学习训练不是一个个样本喂，而是一小批一小批喂。把 Dataset 变成"按批吐数据"的迭代器，就是 `torch.utils.data.DataLoader` 的工作。它还会替你做 shuffle（打乱顺序）、分批、可选地并行加载。

这对组合是本仓库一切数据管线的原子单位。u1-l3 讲过的 `helper_data.get_dataloaders_mnist` 只是把这几行代码包成了函数。

#### 4.1.2 核心流程

```text
datasets.MNIST(root, train, transform, download)   # ① 下载/加载原始数据，transform 决定每张图如何预处理
        │
        ▼
DataLoader(dataset, batch_size, shuffle)            # ② 包成按批迭代器
        │
        ▼
for images, labels in train_loader:                  # ③ 每次迭代得到 (batch_size, 1, 28, 28) 和 (batch_size,)
```

两个关键点：

- `transform=transforms.ToTensor()` 把 PIL 图片转成张量，并把像素值从 `[0, 255]` 缩放到 `[0.0, 1.0]`，形状为通道×高×宽（即 `1×28×28`）。
- 训练集 loader 要 `shuffle=True`（每个 epoch 顺序不同，避免按顺序学习带来的偏差），测试集 loader 保持 `shuffle=False`（评估要稳定可复现）。

#### 4.1.3 源码精读

**超参数设置**——[pytorch_ipynb/basic-ml/softmax-regression.ipynb:L106-L113](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/softmax-regression.ipynb#L106-L113) 定义了四个训练超参数（种子 123、学习率 0.1、10 个 epoch、batch 256）和两个结构参数：`num_features = 784` 正是 \( 28 \times 28 \) 展平后的维度，`num_classes = 10` 对应十个数字。

**数据集创建**——[pytorch_ipynb/basic-ml/softmax-regression.ipynb:L120-L127](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/softmax-regression.ipynb#L120-L127) 中，训练集设 `train=True, download=True`，测试集设 `train=False`；两者都套同一个 `transforms.ToTensor()`。

**DataLoader 组装**——[pytorch_ipynb/basic-ml/softmax-regression.ipynb:L130-L136](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/softmax-regression.ipynb#L130-L136)：注意训练与测试 loader 唯一的差别就是 `shuffle`。

**形状自检**——[pytorch_ipynb/basic-ml/softmax-regression.ipynb:L139-L143](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/softmax-regression.ipynb#L139-L143) 从 loader 里取第一个 batch 就 `break`，只为打印形状。notebook 中保存的输出是：

```text
Image batch dimensions: torch.Size([256, 1, 28, 28])
Image label dimensions: torch.Size([256])
```

这四维分别是 `batch × 通道(1, 灰度) × 高(28) × 宽(28)`；标签是一维的 256 个整数（0-9）。**养成"先看形状再训练"的习惯**，形状对不上是深度学习调试中最常见的错误来源。

一个值得亲手验证的算术：\( 60000 \div 256 = 234.375 \)，所以 DataLoader 每个 epoch 会吐 **235** 个 batch（234 个满批 + 1 个 96 张图的尾批）；而后面训练日志里打印的 `/234` 来自 `len(train_dataset)//batch_size`（整数除法向下取整），只是个标签，并不代表实际批数。

#### 4.1.4 代码实践

1. **实践目标**：直观感受 `batch_size` 如何决定每个 batch 的形状与批次数。
2. **操作步骤**：复制 notebook 中 L130-L136 的两个 loader，把 `batch_size` 依次改成 `64`、`256`、`10000`，分别运行 L139-L143 的自检代码。
3. **需要观察的现象**：`Image batch dimensions` 的第一个数字随之变化；第一个 batch 的形状分别是 `[64, 1, 28, 28]`、`[256, 1, 28, 28]`、`[10000, 1, 28, 28]`。
4. **预期结果**：形状如上；若把自检循环里的 `break` 删掉并计数，`len(train_loader)` 应分别等于 938、235、6（对 60000 个样本向上取整除）。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么测试集的 loader 用 `shuffle=False` 也可以，而训练集强烈建议 `shuffle=True`？

**答案**：shuffle 只影响样本被取出的顺序，不影响数据内容，所以评估结果与顺序无关；而训练时若每个 epoch 都按同一顺序喂样本，梯度更新会带上与该顺序相关的系统性偏差（极端例子：数据若按类别排序，模型会"学了 0 忘了 9"），因此训练集要打乱。

**练习 2**：`transforms.ToTensor()` 之后，一张 MNIST 图像的取值范围是多少？如果不做任何标准化，这个范围对训练有什么潜在影响？

**答案**：`[0.0, 1.0]`（原始灰度 0-255 除以 255）。本 notebook 就停在这一步；值域不算大所以训练仍能收敛。后续讲义（如 u4-l3 的逐通道标准化）会进一步减去均值、除以标准差，让各通道分布以 0 为中心、方差为 1，收敛通常更快更稳。

**练习 3**：`for images, labels in train_loader` 循环体里只有 `print` 和 `break`，为什么不会把整个数据集遍历一遍？

**答案**：`DataLoader` 是惰性迭代器（生成器语义），每次 `next` 才组装一个 batch；`break` 在第一批之后就跳出循环，剩余数据从未被加载。

### 4.2 softmax 与交叉熵

#### 4.2.1 概念说明

逻辑回归输出**一个**概率，只能做二分类。softmax 回归（也叫多项逻辑回归）把它推广到 K 类：线性层对每个样本输出 K 个 logits \( z_1, \dots, z_K \)（本讲 K=10），softmax 函数把它们变成一个合法的概率分布：

\[ p_j = \frac{e^{z_j}}{\sum_{k=1}^{K} e^{z_k}}, \qquad j = 1, \dots, K \]

所有 \( p_j > 0 \) 且 \( \sum_j p_j = 1 \)。分类时就取概率最大的类：\( \hat{y} = \arg\max_j p_j \)。注意 \( \arg\max \) 不改变排序，所以对 logits 做 `max` 和对概率做 `max` 结果相同——这是后面代码直接 `torch.max(probas, 1)` 的原因。

训练目标是最小化交叉熵损失。对 one-hot 的真实标签 y（正确类为 1，其余为 0）：

\[ L = -\sum_{k=1}^{K} y_k \log p_k = -\log p_{y} \]

即"让正确类的概率尽量大"。它对 logits 的梯度有一个极其优雅的形式：

\[ \frac{\partial L}{\partial z_j} = p_j - y_j \]

对比 u2-l1 逻辑回归的梯度 \( (p - y)x \)：多分类版本只是把标量误差换成了向量误差 \( p - y \)，这正说明 softmax 回归是逻辑回归的自然推广，两者本质同一族模型。

#### 4.2.2 核心流程

为什么 `F.cross_entropy` 要吃 logits 而不是概率？看两步计算：

```text
logits z ──(softmax 的 log 域实现: 先减最大值稳定, 再 log)──> log_softmax
        ──(与真实类做负对数)──> 交叉熵损失
```

\[ \log p_j = z_j - \log\!\Big(\sum_k e^{z_k}\Big) = z_j - \Big(\max_k z_k + \log\sum_k e^{z_k - \max_k z_k}\Big) \]

公式右边的写法（先减去 \( \max_k z_k \)）就是著名的 **log-sum-exp 稳定技巧**：不先算 \( e^{z} \) 再取 log，避免大 logits 直接溢出。`F.cross_entropy` 内部把 `log_softmax` 和负对数似然合并成一步，既数值稳定又省一次往返。如果你先自己 `softmax` 再把**概率**喂给 `cross_entropy`，等于在概率上再做一次 log_softmax，数学上就错了——这就是源码里那行注释存在的意义。

#### 4.2.3 源码精读

**模型定义**——[pytorch_ipynb/basic-ml/softmax-regression.ipynb:L156-L168](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/softmax-regression.ipynb#L156-L168)：

```python
class SoftmaxRegression(torch.nn.Module):

    def __init__(self, num_features, num_classes):
        super(SoftmaxRegression, self).__init__()
        self.linear = torch.nn.Linear(num_features, num_classes)

        self.linear.weight.detach().zero_()
        self.linear.bias.detach().zero_()

    def forward(self, x):
        logits = self.linear(x)
        probas = F.softmax(logits, dim=1)
        return logits, probas
```

要点逐条说：

- 整个"模型"就一层 `Linear(784, 10)`，参数量 \( 784 \times 10 + 10 = 7850 \)。没有隐藏层——softmax 回归是**线性分类器**，它的决策边界是直线/超平面。
- **权重和偏置被显式清零**（L162-L163）。`.detach()` 先把张量从计算图上摘下来（u2-l1 讲过 detach），再 `zero_()` 原地清零，这样修改的是参数底层数据而不惊动 autograd。
- `forward` **同时返回 logits 和 probas**：训练用 logits（喂给 `cross_entropy`），评估/可视化用 probas。`dim=1` 表示沿着"类别"这一维做 softmax（每个样本自己归一化）。

**零初始化的一个漂亮副作用**：第一个 batch 的损失恰好是

\[ L_0 = -\log \frac{1}{10} = \log 10 \approx 2.3026 \]

因为全零参数 → 所有 logits 为 0 → softmax 输出每类 0.1 的均匀分布。notebook 保存的训练日志第一行正是 `Epoch: 001/010 | Batch 000/234 | Cost: 2.3026`——你甚至可以用这个数字反向验证初始化确实生效了。

**损失计算处的注释**——[pytorch_ipynb/basic-ml/softmax-regression.ipynb:L280-L287](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/softmax-regression.ipynb#L280-L287)：作者特意写了注释提醒 `CrossEntropyLoss works with logits, not probabilities`，随后 `cost = F.cross_entropy(logits, targets)`（L285）传入的是 logits。

**优化器**——[pytorch_ipynb/basic-ml/softmax-regression.ipynb:L179](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/softmax-regression.ipynb#L179) 用最朴素的 `torch.optim.SGD(model.parameters(), lr=0.1)`。softmax 回归的损失是关于参数的凸函数，SGD 足够好用。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证「零初始化 → 首个损失 = ln 10」和「`F.cross_entropy` 等价于手工 `-log softmax`」。
2. **操作步骤**：在 notebook 末尾新建一个 cell，运行下面的**示例代码**（非仓库原有代码）：

```python
import math, torch
import torch.nn.functional as F

print("ln(10) =", math.log(10))          # 与日志里的 2.3026 对照

torch.manual_seed(0)
logits = torch.randn(5, 10)              # 5 个样本、10 类的假 logits
targets = torch.randint(0, 10, (5,))     # 每个样本随机一个正确类

# 官方实现
loss_fast = F.cross_entropy(logits, targets)

# 手工实现：取正确类位置的 log_softmax 再取负
log_probas = F.log_softmax(logits, dim=1)
loss_manual = -log_probas[range(5), targets].mean()

print(loss_fast.item(), loss_manual.item())   # 两者应完全一致
```

3. **需要观察的现象**：`ln(10)` 打印约 2.3026；两个损失打印值相同。
4. **预期结果**：两种算法每个数字都一致（同一套 log-sum-exp 底层实现）；再用 `probas = F.softmax(logits, dim=1)` 后误把 probas 喂给 `F.cross_entropy`，会得到一个明显不同的数，说明"概率版"用法是错的。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：既然 `forward` 已经算出了 `probas`，为什么损失不写成 `F.cross_entropy(probas, targets)` 加个修正？

**答案**：`F.cross_entropy` 的契约就是接收**未归一化的 logits**，内部自己完成 log_softmax（含数值稳定的 log-sum-exp）。喂概率进去等于做了两次 softmax，损失值和梯度都是错的。要么全程 logits + `cross_entropy`（推荐），要么自己用 `log_softmax` 手写损失，不能混。

**练习 2**：把 `F.softmax(logits, dim=1)` 的 `dim` 误写成 `dim=0` 会发生什么？

**答案**：`dim=0` 会让"同一个类别跨 batch 内所有样本"的分数归一化为 1，而不是每个样本自己归一化。形状不会报错，概率却不再表示"这个样本属于各类的程度"，训练和评估都会被破坏。多维张量做 softmax 时 dim 是最常错的参数。

**练习 3**：softmax 回归没有隐藏层，它能画出弯曲的决策边界吗？

**答案**：不能。它对 784 维输入的每个类别学一个线性打分 \( w_j^\top x + b_j \)，类别之间的边界 \( w_i^\top x = w_j^\top x \) 是超平面。MNIST 上 91.77% 的测试准确率说明线性边界已经相当能打，但弯曲边界要等下一讲的多层感知机。

### 4.3 标准训练循环

#### 4.3.1 概念说明

本模块是全手册最重要的 30 行代码。它把 u2-l1 见过的五步范式装进「epoch × batch」双层循环：

- **外层 `for epoch`**：把 60000 张图完整过 10 遍。模型不是一遍学会的，每遍都在 refine 参数。
- **内层 `for batch_idx, (features, targets)`**：每个 iteration 处理 256 张图，做一轮完整的"前向 → 损失 → 清梯度 → 反向 → 更新"。

五个步骤各自负责什么、为什么缺一不可：

| 步骤 | 代码 | 作用 | 少了会怎样 |
|---|---|---|---|
| ① 前向 | `logits, probas = model(features)` | 用当前参数算出预测 | — |
| ② 损失 | `cost = F.cross_entropy(logits, targets)` | 把预测与标签的差距变成一个标量 | — |
| ③ 清梯度 | `optimizer.zero_grad()` | 把上次 `backward()` 累积的 `.grad` 清零 | 梯度跨 batch 累加，更新方向被历史污染 |
| ④ 反向 | `cost.backward()` | 自动微分，把梯度写进每个参数的 `.grad` | 参数没有梯度 |
| ⑤ 更新 | `optimizer.step()` | 按梯度和学习率移动参数 | 前功尽弃，参数永远不变 |

还有两处容易忽视的细节：

- **展平发生在循环里，不在模型里**：`features.view(-1, 28*28)`（L276）把 `256×1×28×28` 摊平成 `256×784`。`-1` 让 PyTorch 自动推断该维大小。后续 notebook 常把这行挪进 `forward`，两种写法等价，但你要知道数据进 `Linear` 前必须是二维的。
- **epoch 末评估**：`with torch.set_grad_enabled(False)` 包住准确率计算，避免为评估建计算图、浪费内存。本 notebook 没调 `model.eval()`——softmax 回归没有 Dropout/BatchNorm 这类"训练态≠评估态"的层，所以不调也不出错；换成 u2-l4 的模型就必须调，这是新式 helper（u1-l3）统一加 `model.eval()` 的原因。

#### 4.3.2 核心流程

```text
for epoch in 1..10:                        # 外层：数据集过 10 遍
    for batch_idx, (features, targets) in enumerate(train_loader):
        features = features.view(-1, 784).to(device)   # 展平 + 搬到 GPU/CPU
        targets  = targets.to(device)
        logits, probas = model(features)    # ① 前向
        cost = F.cross_entropy(logits, targets)  # ② 损失（吃 logits）
        optimizer.zero_grad()               # ③ 清梯度
        cost.backward()                     # ④ 反向求梯度
        optimizer.step()                    # ⑤ SGD 更新参数
        每 50 个 batch 打印一次损失
    with no_grad: 计算 epoch 末的训练准确率
```

#### 4.3.3 源码精读

**准确率函数**——[pytorch_ipynb/basic-ml/softmax-regression.ipynb:L259-L270](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/softmax-regression.ipynb#L259-L270)：同样先 `view(-1, 28*28)` 展平（L263，与训练保持一致）；`torch.max(probas, 1)` 沿类别维取最大值，返回 `(最大值, 最大值下标)`，只要下标作预测标签；`(predicted_labels == targets).sum()` 统计答对数，除以样本数乘 100 得百分比。

**训练循环主体**——[pytorch_ipynb/basic-ml/softmax-regression.ipynb:L273-L297](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/softmax-regression.ipynb#L273-L297)：

```python
for epoch in range(num_epochs):
    for batch_idx, (features, targets) in enumerate(train_loader):

        features = features.view(-1, 28*28).to(device)     # L276 展平+搬运
        targets = targets.to(device)                       # L277

        ### FORWARD AND BACK PROP
        logits, probas = model(features)                   # L280 ①
        cost = F.cross_entropy(logits, targets)            # L285 ②
        optimizer.zero_grad()                              # L286 ③
        cost.backward()                                    # L287 ④

        ### UPDATE MODEL PARAMETERS
        optimizer.step()                                   # L290 ⑤

        ### LOGGING
        if not batch_idx % 50:                             # L293 仅在 0/50/100/150/200 批打印
            print('Epoch: %03d/%03d | Batch %03d/%03d | Cost: %.4f' % ...)
```

注意三个注释块 `FORWARD AND BACK PROP / UPDATE MODEL PARAMETERS / LOGGING` 正好把五步切成"算梯度、用梯度、打日志"三段——这个排版习惯贯穿整个仓库，扫一眼注释就能定位代码。

**epoch 末评估**——[pytorch_ipynb/basic-ml/softmax-regression.ipynb:L298-L302](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/softmax-regression.ipynb#L298-L302) 在 `torch.set_grad_enabled(False)` 上下文里打印训练准确率。

**notebook 保存的运行结果**（作者环境：torch 1.0.0）：训练损失从 2.3026 一路降到 0.33 左右；训练准确率从 88.04% 爬到 91.40%；最后一个 cell 打印 `Test accuracy: 91.77%`。一条没有隐藏层的线性模型能把 MNIST 做到九成以上——这是很好的 baseline 直觉，后面所有 CNN 都是在跟它比。

#### 4.3.4 代码实践

1. **实践目标**：用两个"破坏性实验"证明 `zero_grad` 和 `step` 各自不可或缺。
2. **操作步骤**：
   - 实验 A：注释掉 L286 的 `optimizer.zero_grad()`，重跑训练 cell（先重跑模型 cell 重建参数）。
   - 实验 B：注释掉 L290 的 `optimizer.step()`，重跑训练 cell。
3. **需要观察的现象**：实验 A 中损失下降明显变慢或后期震荡（旧梯度持续累加，等效于一个怪异的大学习率）；实验 B 中损失几乎纹丝不动、准确率停在 10% 附近（随机猜十类的水平）。
4. **预期结果**：如上所述。两个实验都请重跑模型定义 cell 让参数回到零初始化再观察。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`optimizer.zero_grad()` 为什么放在 `backward()` 之前而不是之后？

**答案**：`backward()` 是**往 `.grad` 里累加**。必须先清零再反向，这一次的梯度才纯净地对应本 batch。若放在 backward 之后，等于把刚算好的梯度清掉了，`step()` 将无梯度可用（或用上一次的旧梯度，取决于位置细节），两种顺序都不对。

**练习 2**：日志里 `Batch %03d/%03d` 显示的 234 是准确的批数吗？

**答案**：不是。它来自 `len(train_dataset)//batch_size = 60000//256 = 234`（向下取整），而 DataLoader 实际产生 235 个 batch（最后一个只有 96 张图）。想拿准确批数直接用 `len(train_loader)`。

**练习 3**：如果把 `features.view(-1, 28*28)` 误写成 `features.view(-1, 28)`，会在哪一步报什么样的错？

**答案**：`view` 要求元素总数一致，`256×1×28×28 = 200704` 个元素无法被 28 整除（200704/28 = 7168，其实恰好整除——但形状会变成 `[7168, 28]`，样本边界错位，标签 `targets` 仍有 256 个）。更早爆掉的是矩阵乘法：`Linear(784, 10)` 期望最后一维是 784，收到 28 会抛出 mat1/mat2 shape mismatch 的运行时错误。这题说明展平维数错误通常在**第一次前向**就暴露，"先看形状"再次救你一命。

### 4.4 mlxtend 决策边界

#### 4.4.1 概念说明

u2-l1 用 2D 玩具数据画过决策边界。MNIST 是 784 维，画不了；要**看见** softmax 回归的决策边界，得回到 2 特征、3 类的鸢尾花（Iris）数据：每个样本由花瓣长度、花瓣宽度两个特征描述，属于山鸢尾/变色鸢尾/维吉尼亚鸢尾三类的之一。`num_features=2, num_classes=3`，平面上一画，三个类各占一块区域，区域之间的交界线就是线性决策边界。

mlxtend 库的 `plot_decision_regions` 可以在平面上涂出这些区域，但它只认一个约定：传入的对象要有 `predict(X)` 方法（输入 numpy 数组、输出整数标签）——这正是 scikit-learn 的接口风格。PyTorch 模型的接口是 `model(tensor)`，两者不匹配。解决办法简单到只有一句话（notebook 原话："All we need is a simple class that implements a `predict` method. That's it."）：写一个**适配器类**包一层。这是工程上非常常用的 Adapter 模式的最小示例。

#### 4.4.2 核心流程

```text
numpy 读 ../data/iris.data (150×2 特征 + 类别名)
  → 用 RandomState(123) 洗牌
  → 标准化：均值/标准差只取自训练部分（防数据泄漏，呼应 u2-l1）
  → MyDataset(Dataset) 包装 → DataLoader(batch=8, num_workers=4)
  → 同一个 SoftmaxRegression 类 + Adam 优化器
  → 同一套五步训练循环
  → ModelWrapper(model).predict(X) ──→ mlxtend plot_decision_regions 涂色出图
```

#### 4.4.3 源码精读

**超参数**——[pytorch_ipynb/basic-ml/softmax-regression-mlxtend-1.ipynb:L98-L105](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/softmax-regression-mlxtend-1.ipynb#L98-L105)：与 MNIST 版对照着看很有意思——`learning_rate=0.05`、`batch_size=8`（数据只有 150 条，256 的批根本凑不满）、`num_features=2`、`num_classes=3`。

**数据读取与划分**——[pytorch_ipynb/basic-ml/softmax-regression-mlxtend-1.ipynb:L123-L125](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/softmax-regression-mlxtend-1.ipynb#L123-L125) 用 `np.genfromtxt` 读 CSV，取第 3、4 列（花瓣长/宽）作特征，第 5 列类别名经字典 `d = {'Iris-setosa': 0, ...}` 映射成 0/1/2。[L140-L152](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/softmax-regression-mlxtend-1.ipynb#L140-L152) 先用固定种子洗牌，切出 `X_train`（前 70）与 `X_test`，再**只用 X_train 的均值和标准差**标准化两份数据——防泄漏原则的又一次落地。

> **仔细阅读的陷阱**：后面 loader 实际用的是 [L185-L186](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/softmax-regression-mlxtend-1.ipynb#L185-L186) 的 `MyDataset(X[:100], y[:100])` 与 `MyDataset(X[100:], y[100:])`——即洗牌后**前 100 条训练、后 50 条测试**；L146-L147 算出的 `X_train/X_test` 变量本身并没有被 loader 使用（只借走了均值/标准差）。别被变量名带偏，读源码要以真正被消费的数据为准。

**自定义 Dataset**——[pytorch_ipynb/basic-ml/softmax-regression-mlxtend-1.ipynb:L170-L182](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/softmax-regression-mlxtend-1.ipynb#L170-L182) 是你第一次见到**手写 Dataset 三要素**：

```python
class MyDataset(Dataset):
    def __init__(self, X, y):                # 把 numpy 数组转成张量
        self.X = torch.tensor(X, dtype=torch.float32)
        self.y = torch.tensor(y, dtype=torch.int64)   # 类别标签必须 int64
    def __getitem__(self, index):            # 第 index 个样本
        return self.X[index], self.y[index]
    def __len__(self):                       # 样本总数
        return self.y.shape[0]
```

`__getitem__`/`__len__` 就是 DataLoader 与你的数据之间的全部契约。这个主题在 u4-l2 会全面展开，这里先混个脸熟。注意标签用 `torch.int64`（`cross_entropy` 对 target 的 dtype 要求），特征用 `float32`。[L189-L197](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/softmax-regression-mlxtend-1.ipynb#L189-L197) 的 DataLoader 还加了 `num_workers=4`（4 个子进程并行取数，小数据集上作用不大）。

**模型与优化器**——[L210-L222](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/softmax-regression-mlxtend-1.ipynb#L210-L222) 的 `SoftmaxRegression` 类与 MNIST 版逐字相同（这就是"自包含 notebook"的代价与风格）；差别在 [L233](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/softmax-regression-mlxtend-1.ipynb#L233) 换成了 **Adam** 优化器。训练循环部分（L273 起）与 4.3 完全一致，唯一不同是不需要 `view(-1, 28*28)`——特征本来就是 2 维向量。notebook 保存的输出里训练准确率在 44%↔95% 之间大幅震荡、测试准确率 84%：batch 只有 8、又用了自适应学习率的 Adam，梯度噪声很大，这是小数据+小 batch 的典型形态。

**适配器**——[pytorch_ipynb/basic-ml/softmax-regression-mlxtend-1.ipynb:L355-L365](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/softmax-regression-mlxtend-1.ipynb#L355-L365)：

```python
class ModelWrapper():
    def __init__(self, model, device):
        self.model = model
        self.device = device

    def predict(self, X):
        features = torch.tensor(X, dtype=torch.float32, device=self.device)
        logits, probas = self.model(features)
        _, predicted_labels = torch.max(probas, 1)
        return predicted_labels.numpy()
```

`predict` 做的事和 `compute_accuracy` 内核一样：转张量 → 前向 → 取最大概率下标，只是最后转回 numpy 交还 mlxtend。[L374](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/softmax-regression-mlxtend-1.ipynb#L374) 用 **CPU** 构造 wrapper：若模型在 GPU 而 features 建在 CPU，会触发 u1-l2 讲过的设备不匹配错误，且 `predicted_labels.numpy()` 对 CUDA 张量也会失败（需先 `.cpu()`）。本地若有 GPU 想跑通，把模型 `.to('cpu')` 或让 wrapper 与模型同 device 即可。待本地验证。

**画图**——[pytorch_ipynb/basic-ml/softmax-regression-mlxtend-1.ipynb:L396-L401](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/basic-ml/softmax-regression-mlxtend-1.ipynb#L396-L401) 一行 `plot_decision_regions(X, y, mymodel)` 传入全部 150 个样本，`plt.show()` 出图：三个色块铺满平面，色块间的边界是**直线段**——softmax 回归线性本质的直接证据。

#### 4.4.4 代码实践

1. **实践目标**：在 Iris 上亲手跑通「训练 → 包装 → 画决策边界」全流程。
2. **操作步骤**：
   - 进入 `pytorch_ipynb/basic-ml/` 目录启动 Jupyter（数据用相对路径 `../data/iris.data` 读取，目录结构不能动，u1-l2 讲过）。
   - 安装 `mlxtend`（`pip install mlxtend`）。
   - 本地较新 NumPy 会在 `y.astype(np.int)` 处报错（`np.int` 已在 NumPy 1.24 移除，u1-l2 提过同款问题），改成 `y.astype(int)`。
   - 从上到下运行全部 cell。
3. **需要观察的现象**：训练准确率逐 epoch 震荡上升；最后一幅图上三个类别区域被两三条直线边界分开，样本点基本落在自己类的色块里。
4. **预期结果**：与 notebook 保存的输出同构（测试准确率量级 ~84%）；山鸢尾（setosa）与其他两类之间应有一条非常干净的直线边界（该类线性可分），变色与维吉尼亚交界处会有少量样本交叉。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`ModelWrapper.predict` 里为什么不写 `with torch.no_grad()`？不写有什么代价？

**答案**：推理时建计算图没有数学错误，只是白白记录中间激活、多耗内存且略慢。示例修复：函数体包进 `with torch.no_grad():` 即可。小型可视化场景代价可忽略，但这是生产代码里必须改掉的坏习惯（u1-l3 的新式 helper 已统一用 no_grad）。

**练习 2**：为什么这个 notebook 用 Adam 而 MNIST 版用 SGD？换成 Iris 用 SGD 会发生什么？

**答案**：两者都合法。Iris 只有 100 个训练样本、batch=8，SGD 对学习率更敏感、收敛更慢，Adam 的自适应步长在迷你数据集上往往"开箱即用"；MNIST 数据充足、损失凸而光滑，lr=0.1 的朴素 SGD 既快又稳。这不是硬规则，而是"小数据小批用自适应优化器更省心"的经验倾向。换成 SGD 的具体表现待本地验证。

**练习 3**：如果把这个 Iris 模型的 `MyDataset` 中 `torch.int64` 改成 `torch.float32` 存标签，会在哪一步出错？

**答案**：训练循环调用 `F.cross_entropy(logits, targets)` 时报错——cross_entropy 要求 target 是整数类别索引（int64/Long 类型），不接受浮点。这是初学者最常见的 dtype 坑之一。

## 5. 综合实践

**任务**：以 `softmax-regression.ipynb` 为骨架，做一次 3 组超参的对照实验，回答"学习率和训练轮数如何共同决定收敛效果"。

**设计**（除超参外一切保持不变，`random_seed=123` 固定）：

| 组别 | learning_rate | num_epochs | 预期角色 |
|---|---|---|---|
| A（基线） | 0.1 | 10 | 复现 notebook 保存结果 |
| B（保守） | 0.01 | 10 | 步长小，10 轮可能学不够 |
| C（激进短跑） | 0.5 | 3 | 步长大、轮数少，看能否"快而糙" |

**操作步骤**：

1. 复制 `pytorch_ipynb/basic-ml/softmax-regression.ipynb` 为自己的实验副本（**不要改动仓库原文件**）。
2. 每组实验：先改 L107-L108 的 `learning_rate`/`num_epochs`，再**从模型定义 cell（L156 起）开始重跑**——这一步必不可少，否则沿用上一组训练好的权重，对照就失效了；`SGD` 优化器 cell 也要重建。
3. 每组记录：第一个 batch 的 Cost、最后一个 epoch 的 Cost、最终训练准确率、`Test accuracy`。
4. 把三行结果填进一张对照表，并在每组旁边用一句话写下你观察到的损失曲线形态（平滑下降/震荡/下降快慢）。

**结果记录表（模板）**：

| 组别 | lr | epochs | 首批 Cost | 末轮 Cost | 训练准确率 | 测试准确率 | 现象一句话 |
|---|---|---|---|---|---|---|---|
| A | 0.1 | 10 | 2.3026（notebook 保存值） | ~0.33 | 91.40% | 91.77% | 平滑收敛 |
| B | 0.01 | 10 | 待本地验证 | | | | |
| C | 0.5 | 3 | 待本地验证 | | | | |

**预期与分析要点**：A 组应精确复现 notebook 保存的数字（种子相同、数据顺序相同）；B 组首批 Cost 同样是 2.3026（零初始化与学习率无关），但下降更慢，末轮损失和准确率都可能落后于 A；C 组可能前几批下降飞快但出现震荡，3 个 epoch 后测试准确率未必输 B——思考"总更新次数 = epoch 数 × 批次数"与"每次更新幅度 = 学习率"的权衡。B、C 两组数字待本地验证。

**加分项**：把三组的"epoch → Cost"曲线画在同一张图上（每 50 批取一个点即可），直观看到三条曲线的斜率与稳定性的差别。

## 6. 本讲小结

- `datasets.MNIST` + `DataLoader` 是最小数据管线：前者管"第 i 个样本是什么"（`ToTensor` 把像素压到 [0,1]），后者管"按多大的批、要不要打乱地吐出来"；训练集 `shuffle=True`、测试集 `shuffle=False`。
- softmax 回归 = 一个 `Linear(784, 10)`：logits 经 softmax 变概率分布，交叉熵 \( -\log p_y \) 作损失，梯度 \( p - y \) 与逻辑回归一脉相承；它是线性分类器，MNIST 上测试准确率 91.77%。
- `F.cross_entropy` 只吃 **logits**（内部做数值稳定的 log_softmax），喂概率是错的；零初始化让首个损失恰为 \( \ln 10 \approx 2.3026 \)。
- 标准训练循环的五步（forward → loss → zero_grad → backward → step）缺一不可：`zero_grad` 防梯度跨批累加，`step` 才真正更新参数；这套「epoch × batch」结构是后续所有 notebook 的模板。
- 展平 `view(-1, 784)` 发生在循环里而不是模型里；评估要在 `no_grad` 下做，含 Dropout/BatchNorm 的模型还必须 `model.eval()`（本讲的线性模型可省）。
- 读别人 notebook 要看"真正被消费的数据"：mlxtend 版里变量 `X_train/X_test` 与 loader 实际用的 `X[:100]/X[100:]` 并不一致；`ModelWrapper` 只需一个 `predict` 方法就能把 PyTorch 模型接进 mlxtend 的决策边界可视化。

## 7. 下一步学习建议

下一讲（u2-l3「多层感知机与 nn.Module」）将把本讲的单一 `Linear` 升级为 784→128→256→10 的多层结构：你会看到为什么需要隐藏层和非线性激活才能画出弯曲的决策边界，为什么权重不能像本讲这样全零初始化（本讲的 `zero_()` 是线性模型的特权），以及如何数一个模型的参数量。建议带着一个问题去读：**本讲的 91.77% 里，被"线性边界"卡住的错误大概占多少？** 

想延伸阅读的读者可以提前看两处源码：`pytorch_ipynb/helper_evaluate.py` 里的 `compute_accuracy`（本讲手写函数的"官方版"）和 `pytorch_ipynb/mlp/mlp-basic.ipynb`（下一讲的主线 notebook）。
