# 迁移学习：用 ImageNet 预训练 VGG16 做 CIFAR-10 分类

## 1. 本讲目标

学完本讲，你应该能够：

1. 会用 `torchvision.models` 一行代码加载在 ImageNet 上预训练好的 VGG16，并理解「预训练权重」到底给了我们什么。
2. 理解 `param.requires_grad = False` 冻结参数的完整机制：为什么优化器拿到了全部参数，冻结层却纹丝不动。
3. 会把模型的 1000 类输出层替换成自己的分类头，并只训练新头（外加一个选择性解冻的隐藏层），跑通完整的「加载 → 冻结 → 换头 → 训练」迁移学习流程。
4. 掌握迁移学习最容易被忽视的「输入契约」：为什么必须 Resize 到 224×224、为什么必须用 ImageNet 的均值和标准差做归一化。
5. 学会一种分层微调（layer-wise fine-tuning）的实验设计，用来回答「解冻更多层值不值」这个问题。

## 2. 前置知识

### 2.1 什么是预训练模型

在第 3 单元（u3-l3）里，我们自己动手搭建过 VGG16：3×3 小卷积核深堆叠的 `features` 卷积主干，加上一个巨型全连接分类头。当时网络的权重是随机初始化的，靠 CIFAR-10 的 5 万张训练图片从零学起。

**预训练模型（pre-trained model）** 则是别人已经在更大、更难的数据集（这里是 ImageNet，约 128 万张图片、1000 个类别）上训练好的网络。下载下来的是一份「权重快照」——每个卷积核、每个全连接矩阵里都已装满了学到的数值。

### 2.2 迁移学习为什么可行

直觉上：一个在 ImageNet 上见过 128 万张图的网络，它的卷积层早已学会识别边缘、纹理、颜色斑块、车轮、眼睛这类「通用视觉零件」。这些零件对 CIFAR-10 的飞机、猫、狗同样有用。迁移学习（transfer learning）的核心假设就是——

> **低层视觉特征是可以复用的，需要换掉的往往只是最顶端的「决策部分」。**

按「复用多少」分两档：

| 路线 | 做法 | 可训练参数 | 适用场景 |
| --- | --- | --- | --- |
| 特征提取（feature extraction） | 冻结全部卷积层，只训练新分类头 | 极少（百万级） | 目标数据小、与 ImageNet 相近 |
| 微调（fine-tuning） | 解冻部分或全部卷积层，用小学习率继续训练 | 多（千万级） | 目标数据较大、与 ImageNet 有差距 |

本讲的 notebook 属于两者之间的实用折中：冻结整个卷积主干，训练新分类头，**外加解冻一个隐藏全连接层**。

### 2.3 你需要回忆的两个旧知识点

- **u3-l3 讲过的 VGG16 结构**：`features`（卷积主干）→ `avgpool`（`AdaptiveAvgPool2d` 到 7×7）→ `classifier`（`Linear(25088, 4096) → Linear(4096, 4096) → Linear(4096, 1000)`）。本讲要动的只有 `classifier` 里的层。
- **u2-l5 / u4-l5 讲过的 autograd**：PyTorch 里每个张量都有 `requires_grad` 标志，决定它是否参与梯度计算。本讲的「冻结」就是拨这个开关。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb) | 本讲主线：整理版迁移学习 notebook（torch 1.3.0 环境），流程完整、注释更细 |
| [pytorch_ipynb/mechanics/transferlearning-vgg16.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/transferlearning-vgg16.ipynb) | 姊妹篇：几乎同一份代码的早期版本（torch 1.0.1 环境），可对照验证「代码相同、结果可复现」 |

两份 notebook 的代码部分几乎逐行一致，区别只在 markdown 说明的详略和运行环境的版本。本讲以 `transfer/` 目录下的整理版为主线引用，需要对照时引用 `mechanics/` 版。

一个风格提示：这两份 notebook 都**没有**使用 u4-l4 介绍过的 `helper_train` / `helper_evaluate` 共享模块，而是把 `compute_accuracy`、`compute_epoch_loss` 和训练循环全部内联在 notebook 里——这是仓库中「自包含 notebook」的风格，便于单独复制出去用，但读的时候要意识到它与 helper 工具链是同一套逻辑。

## 4. 核心概念与源码讲解

### 4.1 预训练模型加载与它的「输入契约」

#### 4.1.1 概念说明

加载预训练模型只有一行代码：`models.vgg16(pretrained=True)`。`pretrained=True` 的意思是：除了搭建网络结构，还要从网上下载 ImageNet 训练好的权重并填进每一层。

但迁移学习不只是「拿别人的权重」，更是「接受别人的输入约定」。预训练模型附带一份**输入契约**：

1. **空间尺寸 224×224**：ImageNet 训练时图片统一缩放到这个尺寸，网络的感受野与统计特性都据此调校。
2. **归一化统计量**：必须用 ImageNet 训练集的通道均值和标准差做 z-score 标准化，即 u4-l3 讲过的逐通道标准化——只不过统计量不是从 CIFAR-10 算，而是沿用 ImageNet 的 \(\text{mean} = (0.485, 0.456, 0.406)\)、\(\text{std} = (0.229, 0.224, 0.225)\)。若换成自己数据集的统计量，等于把特征强行搬进一个网络没见过的数值分布，预训练优势会打折。

为什么必须用 ImageNet 的统计量？因为冻结住的卷积层权重是在「ImageNet 统计量标准化后的输入」上训练出来的；喂给它的数据分布越接近训练时见过的，提取的特征质量越高。这延续了 u4-l3 的原则——**标准化统计量要跟「训练那份权重」配套，而不是跟手头数据配套**。

#### 4.1.2 核心流程

```text
加载预训练 VGG16（一行）
        │
        ▼
CIFAR-10 32×32 ──Resize──▶ 224×224 ──ToTensor──▶ [0,1] ──Normalize──▶ ImageNet 统计量标准化
        │                                                            │
        ▼                                                            ▼
  10 个类别标签                                            送入预训练网络做前向
```

一个容易纠结的点：CIFAR-10 只有 32×32，硬拉大到 224×224 会不会「无中生有」？notebook 作者在 markdown 里给了两个诚实的说明（见下方源码引用）：一是 `AdaptiveAvgPool2d` 让网络技术上也能直接吃 32×32 输入，但实测测试准确率只有约 65%；二是Resize 到 224 是一种工程折中，真实应用中建议使用天然尺寸大于 224 的图片。

#### 4.1.3 源码精读

**加载数据与配套预处理**。transform 流水线是「Resize → ToTensor → Normalize」的固定顺序（u4-l3 讲过为何不可颠倒），超参数区设定 `NUM_CLASSES = 10`、`num_epochs = 10`、`batch_size = 128`：

- [pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb:L119-L125](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb#L119-L125) —— 超参数设置：类别数 10、学习率 0.0001、轮数 10、批大小 128。（注意一个伏笔：这里的 `learning_rate` 后来其实没被用上，详见 4.3.3。）
- [pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb:L132-L137](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb#L132-L137) —— `custom_transform` 三步流水线：把 32×32 拉大到 224×224、转张量并压到 [0,1]、再用 ImageNet 的均值/标准差做逐通道标准化。
- [pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb:L146-L163](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb#L146-L163) —— 用同一份 transform 构建训练集与测试集的 `CIFAR10` 对象和两个 `DataLoader`；训练 loader 开 `shuffle=True`，测试 loader 不开。

**作者对「直接喂 32×32」的说明**：

- [pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb:L80-L91](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb#L80-L91) —— markdown 说明：由于末层是平均池化，直接喂 32×32 也可运行，但实测准确率只有约 65%；并明确写出 ImageNet 归一化参数、强调「这是预训练时用过的方案」。

**真正的一行加载**：

- [pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb:L250-L251](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb#L250-L251) —— `model = models.vgg16(pretrained=True)` 后直接在 cell 里显示 `model`，打印出的结构就是 u3-l3 见过的三段式：`features`（索引 0–30 的卷积/池化层）、`avgpool`（`AdaptiveAvgPool2d` 到 7×7）、`classifier`（索引 0–6 的三个 `Linear`，末层输出 1000 维）。

顺带指出一个读源码小陷阱（延续 u3-l5「以代码为准、不以注释为准」的方法论）：数据段的注释写的是 `### MNIST DATASET`，但下面的代码加载的分明是 `datasets.CIFAR10`——这是作者从 MNIST notebook 复制代码时留下的注释残迹。**以代码为准。**

#### 4.1.4 代码实践

1. **实践目标**：亲眼确认「加载的是预训练权重、模型结构是三段式、输出是 1000 类」。
2. **操作步骤**（示例代码，可在本地 Python 或 notebook 中运行）：

```python
from torchvision import models
import torch

model = models.vgg16(pretrained=True)          # 首次运行会下载约 528 MB 权重

# 1) 看结构：三个子模块的名字与 classifier 的组成
print(model.classifier)

# 2) 确认输出层是 1000 类
print(model.classifier[6].out_features)        # 预期 1000

# 3) 确认权重不是随机的：随便看一个卷积核的数值分布
w = model.features[0].weight.detach()
print(w.shape, float(w.mean()), float(w.std()))
```

3. **需要观察的现象**：`classifier` 打印出 7 层 Sequential；`out_features` 为 1000；`features[0].weight` 的标准差是一个明显非零的特定数值（随机初始化的权重每次下载重装都会变，而预训练权重对同一版本是固定的）。
4. **预期结果**：结构、类别数、权重统计与 notebook 中打印的模型一致。
5. 权重下载与准确数值依赖本地环境与 torchvision 版本，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果不写 `pretrained=True`（或写 `pretrained=False`），得到的模型差在哪里？迁移学习还能做吗？

**答案**：只搭建结构、权重保持随机初始化。此时「迁移」的前提不存在了，整个训练退化为从零训练一个 1.38 亿参数的 VGG16——在 CIFAR-10 的 5 万张图上既慢又容易过拟合，正是 u3-l3 里见过的处境。

**练习 2**：为什么 Normalize 必须用 ImageNet 的统计量，而不能用 u4-l3 的做法、从 CIFAR-10 训练集现算均值标准差？

**答案**：u4-l3 的原则是「统计量要来自训练权重所依赖的训练集」。这里权重的「训练集」是 ImageNet，冻结的卷积层是在 ImageNet 统计量标准化后的输入上调校的；用 CIFAR-10 现算的统计量会改变输入分布，让冻结层工作在「没见过的数值范围」里。只有当你打算连卷积层一起微调、且数据量足够时，用目标数据集的统计量才更合理。

### 4.2 参数冻结：`requires_grad = False` 到底冻住了什么

#### 4.2.1 概念说明

冻结（freeze）= 把参数张量的 `requires_grad` 标志设为 `False`。它带来两个连锁效果：

1. **反向传播时**：autograd 不为该参数计算梯度，`backward()` 之后该参数的 `.grad` 保持 `None`（而不是 0）。
2. **优化器更新时**：`torch.optim.Adam` 这类优化器在 `step()` 里逐参数检查，凡是 `p.grad is None` 的参数直接跳过——所以即便把**全部参数**都交给了优化器，冻结层也一个数值都不会变。

第二点常被初学者误解。很多人以为「冻结 = 不把参数传给优化器」，于是疑惑 notebook 里为什么写 `Adam(model.parameters())` 而不是只传新头的参数。答案是两种写法都有效，机制不同但殊途同归：

| 写法 | 机制 | 效果 |
| --- | --- | --- |
| 冻结 `requires_grad` + 传全部参数 | 优化器跳过 `grad is None` 的参数 | 冻结层不更新 |
| 只把可训练参数传给优化器 | 优化器根本不持有冻结参数 | 冻结层不更新 |

前者改一处标志即可，且随时可以解冻个别层继续训练（本讲 notebook 正是这么做的）；后者省一点优化器状态内存。工程上前者更常见。

还有一个**顺序陷阱**必须讲清楚。notebook 的三步操作顺序是：

```text
① 冻结全模型（所有旧参数 requires_grad=False）
② 解冻一个隐藏层（classifier[3].requires_grad=True）
③ 替换输出层（新模块的参数是新建的，requires_grad 天生为 True）
```

如果颠倒成「先换头、再全模型冻结」，第①步的 `for param in model.parameters()` 会把**新头的参数也一起冻结**——新头永远得不到梯度，训练纹丝不动、损失卡在初始值附近。这是迁移学习最常见的 bug 之一，记住口诀：**先冻结，后换头**（或者换头之后只冻结 `model.features` 与旧 classifier 层）。

#### 4.2.2 核心流程

```text
for param in model.parameters():
    param.requires_grad = False        # ① 全模型冻结
             │
             ▼
model.classifier[3].requires_grad = True   # ② 选择性解冻倒数第二个全连接层
             │
             ▼
（下一节）替换 classifier[6] 为新分类头   # ③ 新头参数默认可训练
```

为什么偏偏解冻 `classifier[3]`（4096→4096 的那个全连接层）？作者的意图是：ImageNet 的 1000 类语义与 CIFAR-10 的 10 类并不对齐，`classifier[3]` 输出的 4096 维「语义特征」允许被新任务稍微改造，而更底层的卷积特征保持原样。这属于前文表格里「特征提取 → 微调」之间的折中：多训一层，多花一点算力，换一点可塑性。

#### 4.2.3 源码精读

- [pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb:L274-L275](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb#L274-L275) —— 第①步：遍历 `model.parameters()`，把每一个参数张量的 `requires_grad` 置为 `False`，两行完成全模型冻结。
- [pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb:L314](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb#L314) —— 第②步：一行解冻 `model.classifier[3]`（4096→4096 全连接层）。注意 `model.classifier[3]` 是一个 `Linear` 模块对象，对模块赋值 `requires_grad` 会作用到它的 `weight` 与 `bias` 两个参数上。
- [pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb:L282-L302](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb#L282-L302) —— 作者用 markdown 把模型结构再贴一遍并用 `->` 箭头标出 `classifier[3]` 的位置，交代「我们想训练的是倒数第二层」——这是给读者看的结构导航，也是我们确认层索引含义的依据。
- 姊妹版对照：[pytorch_ipynb/mechanics/transferlearning-vgg16.ipynb:L249-L250](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/transferlearning-vgg16.ipynb#L249-L250)（全冻结）与 [pytorch_ipynb/mechanics/transferlearning-vgg16.ipynb:L266](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/transferlearning-vgg16.ipynb#L266)（解冻 classifier[3]），代码与主线版完全一致。

#### 4.2.4 代码实践

1. **实践目标**：用数字验证「冻结奏效了」——统计可训练参数占总参数的比例，并亲眼看到冻结参数的 `.grad` 是 `None`。
2. **操作步骤**（示例代码，接在 4.1.4 的加载与 4.2.3 的冻结/解冻之后运行）：

```python
# ① 统计可训练参数量占比（此时已执行：全冻结 + 解冻 classifier[3]）
total     = sum(p.numel() for p in model.parameters())
trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
print(f'{trainable:,} / {total:,} = {trainable/total:.2%}')

# ② 走一个最小训练步，观察哪些参数长出了 .grad
x = torch.randn(4, 3, 224, 224)
loss = model(x).sum()
loss.backward()
print(model.features[0].weight.grad)     # 预期 None（冻结）
print(model.classifier[3].weight.grad)   # 预期一个有值的张量（已解冻）
```

3. **需要观察的现象**：可训练占比约在百分之十几（`classifier[3]` 约 1678 万参数 + 新头约 210 万参数，分母约 1.35 亿——换头后的精确数字见 4.3.3）；第一个卷积层 `features[0].weight.grad` 打印出 `None`，而 `classifier[3].weight.grad` 是一个数值张量。
4. **预期结果**：与上面括号中的分析一致；这直接证明了「优化器拿到全部参数也不会动冻结层」的前提（`.grad` 为 `None` 会被 `step()` 跳过）。
5. 精确占比取决于是否已完成 4.3 的换头步骤，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：把 `optimizer = torch.optim.Adam(model.parameters())` 改成只收集可训练参数：`torch.optim.Adam(p for p in model.parameters() if p.requires_grad)`。训练结果应该变吗？

**答案**：不应有任何变化（数值层面完全等价）。两种写法中真正被更新的都是那批 `requires_grad=True` 的参数；区别只在优化器内部是否为冻结参数维护一份状态（Adam 的动量/二阶矩缓存），后者略省内存。

**练习 2**：如果想把解冻目标从 `classifier[3]` 换成最后一个卷积块（`features[24]` 到 `features[30]`），代码怎么写？

**答案**：

```python
for param in model.parameters():
    param.requires_grad = False
for layer in model.features[24:]:      # 最后一个卷积块：24~29 是卷积+ReLU，30 是池化
    for param in layer.parameters():
        param.requires_grad = True
```

池化层（索引 30）没有参数，循环对它无害。这正是第 5 节综合实践要做的事。

### 4.3 分类头替换与「只训新头」的完整训练流程

#### 4.3.1 概念说明

预训练 VGG16 的输出层是 `classifier[6] = Linear(4096, 1000)`——为 ImageNet 的 1000 个类别而生。CIFAR-10 只有 10 类，这个层必须换：

1. **维度不匹配**：1000 维输出配 10 类标签，交叉熵根本没法算。
2. **语义不匹配**：就算恰好也是 1000 类，旧输出层的权重编码的是 ImageNet 类别的决策边界，对新任务没有直接意义。

notebook 的换法不是换成单个 `Linear(4096, 10)`，而是换成一个**两层小头**：

\[
\text{logits} = W_2 \, \cdot \, \text{Dropout}_{0.5}\big(\text{ReLU}(W_1 x)\big),\quad W_1 \in \mathbb{R}^{512 \times 4096},\ W_2 \in \mathbb{R}^{10 \times 512}
\]

即 `Linear(4096, 512) → ReLU → Dropout(0.5) → Linear(512, 10)`。先降到 512 维再投影到 10 类，中间夹一个 Dropout 正则（u2-l4 讲过它的 train/eval 行为切换）。markdown 里作者自述「实际上我们加了两个输出层」，指的就是这个两层结构。

换成小头的另一个隐性收益：参数量比原来的 `Linear(4096, 1000)`（约 409.7 万）更少（约 210.3 万），在只用 5 万张图训练的前提下，小头更不容易过拟合。

换头之后的训练循环与 u2-l2 以来反复打磨的**五步范式**完全一致：`forward → loss → zero_grad → backward → step`。迁移学习没有引入任何新的训练机制——新东西全在「模型怎么来、参数谁可训」这两步。

#### 4.3.2 核心流程

```text
model.classifier[6] = nn.Sequential(Linear(4096,512), ReLU, Dropout(0.5), Linear(512,10))
        │
        ▼
model.to(DEVICE)；optimizer = Adam(model.parameters())     ← 拿全部参数，但只更新可训练的那批
        │
        ▼
for epoch in range(10):
    model.train()                                          ← Dropout 生效
    for batch in train_loader:
        logits = model(features)                           ┐
        cost  = F.cross_entropy(logits, targets)           │ 五步范式
        optimizer.zero_grad(); cost.backward(); optimizer.step() ┘
    model.eval()；在 no_grad 下报告训练准确率与 epoch 损失
        │
        ▼
测试集上最终评估一次：Test accuracy ≈ 84%
```

#### 4.3.3 源码精读

**换头**：

- [pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb:L330-L334](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb#L330-L334) —— 用下标赋值把 `model.classifier[6]` 整体替换为 `nn.Sequential(nn.Linear(4096, 512), nn.ReLU(), nn.Dropout(0.5), nn.Linear(512, NUM_CLASSES))`。对 `Sequential` 的下标赋值会同步更新模块注册表，新参数立刻出现在 `model.parameters()` 里，且 `requires_grad` 默认为 `True`（不受之前全模型冻结影响——因为是新建的张量）。
- 姊妹版同一处：[pytorch_ipynb/mechanics/transferlearning-vgg16.ipynb:L282-L286](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/transferlearning-vgg16.ipynb#L282-L286)。

**设备搬运与优化器**：

- [pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb:L350-L351](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb#L350-L351) —— `model.to(DEVICE)` 后创建 `optimizer = torch.optim.Adam(model.parameters())`。注意：**没有传 `lr` 参数**，因此实际学习率是 Adam 的默认值 `0.001`，而第 L123 行定义的 `learning_rate = 0.0001` 从头到尾没有被使用。这是「读源码要以变量来源为准」的又一真实案例——若你复现时手动传了 `lr=0.0001`，行为就和 notebook 保存的输出不一致了。
- 姊妹版：[pytorch_ipynb/mechanics/transferlearning-vgg16.ipynb:L302-L303](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/transferlearning-vgg16.ipynb#L302-L303)（`model.to(DEVICE)` 与 Adam 各占一行）。

**内联的评估函数**（逻辑与 u4-l4 的 helper_evaluate 同款）：

- [pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb:L468-L481](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb#L468-L481) —— `compute_accuracy`：先 `model.eval()`（让 Dropout 关闭），累加预测正确的样本数再除以总数。
- [pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb:L483-L497](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb#L483-L497) —— `compute_epoch_loss`：在 `torch.no_grad()` 下用 `F.cross_entropy(..., reduction='sum')` 先求和再除以样本总数，避免小批次被放大权重（与 u4-l4 讲过的口径问题一致）。

**训练主体——五步范式原样复用**：

- [pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb:L500-L535](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb#L500-L535) —— 外层 epoch、内层 batch 的训练循环。关键行：L503 `model.train()` 开启 Dropout 训练模式；L510–L511 前向传播与交叉熵损失；L512 `optimizer.zero_grad()` 清空梯度；L514 `cost.backward()` 反向传播（梯度只流入可训练参数）；L517 `optimizer.step()` 更新参数；L526 起在 `set_grad_enabled(False)` 下做 epoch 级评估并打印耗时。
- 测试集只碰一次：[pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb:L552-L553](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb#L552-L553) —— 训练全部结束后在 `set_grad_enabled(False)` 下打印测试准确率。

**保存下来的运行结果**（notebook 内嵌输出，非本讲义运行）：

- 主线版：10 个 epoch、每轮约 10.5 分钟（V100 级 GPU、224×224 输入），最终训练集准确率 88.696%、epoch 损失 0.329（输出见 [L461-L463](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb#L461-L463)），**测试准确率 84.25%**（输出见 [L547](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb#L547)）。这份 JSON 里每个 cell 的 `outputs` 排在 `source` 之前，所以训练输出（L371–L463）位于训练源码（L468–L535）上方。
- 姊妹版（torch 1.0.1 环境）同代码得 84.23%（[pytorch_ipynb/mechanics/transferlearning-vgg16.ipynb:L499](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/transferlearning-vgg16.ipynb#L499)）——两版几乎重合，可视为「同一实验的两次实现」。

第一个 batch 的损失是 2.3803：换头后网络对新类别近乎随机猜测，理论初始损失约为 \(\ln 10 \approx 2.303\)，略高是因为新头里随机初始化的 `Linear(4096, 512)` 还叠加了一层随机投影。训练集准确率（88.7%）高于测试集（84.25%）约 4.4 个百分点，存在温和的泛化缺口——毕竟 1.35 亿参数的网络配 5 万张图，全靠冻结大部分参数压住了过拟合。

**结尾的可视化**（可选阅读）：

- [pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb:L605-L621](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/transfer/transferlearning-vgg16-cifar10-1.ipynb#L605-L621) —— `unnormalize` 把标准化张量乘回 `std`、加回 `mean` 还原成可显示的图片（与 u4-l3 的 `UnNormalize` 同理），把测试批前 10 张图的预测类别画在标题上。

#### 4.3.4 代码实践

1. **实践目标**：完整跑通主线 notebook，并对照内嵌输出检验自己的环境。
2. **操作步骤**：
   1. 克隆仓库并进入 `pytorch_ipynb/transfer/`，启动 Jupyter（保持仓库目录结构完整，notebook 用相对路径 `data/` 存数据）。
   2. 从上到下依次运行各 cell；首次运行会自动下载 CIFAR-10（约 170 MB）与 VGG16 预训练权重（约 528 MB）。
   3. 观察「Checking the dataset」cell 打印的 batch 形状：`torch.Size([128, 3, 224, 224])`——确认 Resize 生效。
   4. 训练 cell 运行 10 个 epoch；若没有 GPU，可先把 `num_epochs` 改为 1 感受流程（CPU 上 224×224 的 VGG16 前向非常慢）。
   5. 运行最后一个 cell 得到测试准确率。
3. **需要观察的现象**：首个 batch 的 Cost 约 2.3 上下；每个 epoch 训练损失单调下降（从 0.470 到 0.329）；训练准确率逐轮爬升。
4. **预期结果**：有合适 GPU 时测试准确率应在 84% 上下小幅浮动（随机性来自 Dropout 与数据打乱）；CPU 环境则主要验证流程跑通即可。
5. 具体数值随硬件、torch 版本与随机因素变化，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果把新头换成单个 `nn.Linear(4096, 10)`（不带中间的 512 维与 Dropout），参数量会变多少？可能带来什么影响？

**答案**：原新头约 \(4096 \times 512 + 512 + 512 \times 10 + 10 = 2{,}102{,}794\) 个参数；单层头为 \(4096 \times 10 + 10 = 40{,}970\) 个，少约 206 万。可训练参数大幅减少、更不易过拟合，但表达力也下降；在 CIFAR-10 这种小数据集上两者差距通常不大，值得做成对照实验（这正是本仓库「克隆 notebook、只改一处」的惯用方法论）。

**练习 2**：为什么训练循环里每个 epoch 都要显式调用 `model.train()`、评估时又要 `model.eval()`？冻结了卷积层之后这一对调用还有必要吗？

**答案**：`train()/eval()` 翻转的是 `self.training` 标志，改变行为的是 `Dropout`（新头里有一个）这类层：训练时随机失活、评估时恒等通过。即使卷积层全部冻结，新头里的 `Dropout(0.5)` 依然存在，所以两个调用都不可省——忘了 `eval()` 会让评估结果带着随机噪声且系统性偏差。此外 BatchNorm 类层也受此开关控制（本模型 VGG16 原生结构里没有，但自定义变体常见）。

**练习 3**：notebook 里 `learning_rate = 0.0001` 这个变量实际上生效了吗？

**答案**：没有。L351 创建 Adam 时未传 `lr`，用的是默认值 0.001；`learning_rate` 只在设置区定义、从未被引用。复现实验时要照代码（`Adam(model.parameters())`）而不是照变量名理解行为。

## 5. 综合实践：分层微调——解冻最后一个卷积块值不值？

本讲的综合实践把「特征提取」推进到「部分微调」：在主线 notebook 的基础上，**额外解冻 VGG16 的最后一个卷积块**（`features[24]` 到 `features[30]`，即第五个 block 的三个 512 通道卷积层加池化），对比两种配置在 CIFAR-10 上的测试准确率与训练耗时。

**背景动机**：CIFAR-10 的图像统计（低分辨率、漫画感强）与 ImageNet（高分辨率自然照片）差距不小，最靠近输出的卷积块提取的是最高层、最任务相关的特征——允许它适应新数据，理论上比「只训分类头」更有潜力；代价是可训练参数从约 1890 万（`classifier[3]` + 新头）涨到约 2660 万（再加三个 512×512 与一个 512 通道卷积块），反向传播也要穿过更深的层，每轮耗时与显存都会上升。

**操作步骤**（示例代码，基于主线 notebook 修改）：

```python
# ---------- 配置 B：解冻最后一个卷积块 ----------
model = models.vgg16(pretrained=True)

# ① 先冻结全模型
for param in model.parameters():
    param.requires_grad = False

# ② 解冻 classifier[3]（与原 notebook 相同）
model.classifier[3].requires_grad = True

# ③ 新增：解冻最后一个卷积块 features[24:31]
for layer in model.features[24:31]:
    for param in layer.parameters():
        param.requires_grad = True

# ④ 换头（保持与配置 A 完全一致，受控对比）
model.classifier[6] = nn.Sequential(
    nn.Linear(4096, 512), nn.ReLU(), nn.Dropout(0.5),
    nn.Linear(512, NUM_CLASSES))

model = model.to(DEVICE)
# 微调路线建议给预训练层更小的学习率（判别式学习率），示例：
optimizer = torch.optim.Adam([
    {'params': model.features[24:31].parameters(), 'lr': 1e-5},   # 旧层小步走
    {'params': model.classifier[3].parameters(),   'lr': 1e-5},
    {'params': model.classifier[6].parameters(),   'lr': 1e-4},   # 新头大步走
])
```

**对照实验纪律**（沿用 u2-l4 以来「克隆基线、只改一处」的方法）：

- 配置 A = 原 notebook（只训 `classifier[3]` + 新头）；配置 B = 上述代码。
- 除解冻范围与优化器分组外，数据划分、epoch 数、batch size、新头结构全部保持一致；建议先固定随机种子（u1-l3 的 `set_all_seeds`）再各跑一次。
- 记录三组量：测试准确率、单 epoch 耗时（notebook 已在打印）、GPU 显存峰值（`torch.cuda.max_memory_allocated()`，参考 u8-l4 的实测方法）。

**需要观察的现象与预期结果**：

1. 配置 B 的可训练参数量比配置 A 多出约 770 万（可用 4.2.4 的统计代码核对）。
2. 配置 B 单 epoch 耗时与显存占用高于配置 A（反向传播需要穿过被解冻的卷积块）。
3. 测试准确率可能小幅上升（微调让高层特征适应 CIFAR-10），也可能因学习率不当而不升反降——这正是判别式学习率（旧层小步、新层大步）存在的意义。
4. 以上均为定性预期，具体数值**待本地验证**；GPU 资源有限时可把 `num_epochs` 降到 3–5 先做粗对比。

## 6. 本讲小结

- 迁移学习 = **加载预训练权重 + 接受输入契约 + 选择性冻结 + 替换分类头**，训练循环本身仍是标准的五步范式，没有任何新机制。
- 预训练模型自带输入契约：Resize 到 224×224、用 **ImageNet 的均值/标准差**归一化；统计量要与「训练那份权重的数据集」配套，而不是手头数据集。
- `requires_grad = False` 的完整机制：反向传播不为其计算梯度（`.grad` 保持 `None`），优化器在 `step()` 时跳过无梯度参数——所以 `Adam(model.parameters())` 传全部参数也冻得住冻结层。
- 顺序口诀**「先冻结、后换头」**：换上的新模块参数天生可训练；若颠倒顺序，全模型冻结会把新头一并冻死。
- 本讲配置实际可训练的是 `classifier[3]`（约 1678 万参数）加两层新头（约 210 万参数），其余约 1.16 亿参数冻结；10 个 epoch 后测试准确率 84.25%（姊妹版 84.23%）。
- 读源码两个真实发现：设置区的 `learning_rate = 0.0001` 从未被使用（实际是 Adam 默认 1e-3）；数据段注释写着 `MNIST DATASET` 实际加载的是 CIFAR-10——**永远以代码为准，不以注释或变量名为准**。

## 7. 下一步学习建议

- **u7-l5（有序回归：CORAL 与 CORN）**：同属「改造输出层适配任务」的思路——当目标不是普通分类而是有序类别时，替换的就不只是 `Linear` 的输出维数，而是带秩一致性约束的整套输出层与损失。
- **u8-l3（数据增强：AutoAugment 与 TrivialAugment）**：迁移学习的天然搭档。小数据集上「预训练 + 强增强」往往比「解冻更多层」更划算，可把本讲的配置 A/B 实验与增强实验组合成 2×2 对照。
- **u8-l5（多 GPU 并行）**：本讲每 epoch 约 10.5 分钟的瓶颈在 224×224 大输入的前向计算，想规模化迁移学习实验时可参考 `cnn-vgg16-celeba-data-parallel.ipynb` 的 DataParallel 用法。
- 源码延伸阅读：`torchvision/models` 里 ResNet 的 [官方迁移学习写法](https://pytorch.org/docs/stable/torchvision/models.html)（`torch.hub` / `weights` 参数），并与 u3-l5 的 `cnn-mobilenet-v2-cifar10.ipynb` 中「加载预训练模型、替换分类头」的 torch.hub 路线对照——那是同一思想的另一种加载入口。
