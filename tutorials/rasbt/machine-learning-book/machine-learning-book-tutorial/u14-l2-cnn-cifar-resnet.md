# 手写 CNN 实战：MNIST 与 CelebA 完整训练流程（u14-l2）

## 1. 本讲目标

学完本讲，你应该能够：

1. 用 `torchvision.datasets` + `transforms.Compose` + `DataLoader` 搭出一条标准的图像输入管线，并理解训练集用随机变换（数据增强）、验证/测试集用确定性变换的原因。
2. 读懂并自行搭建「卷积块堆叠 + 全连接分类头」的 CNN 结构，能按卷积输出尺寸公式手动推演每层输出的空间尺寸与展平后的维度。
3. 逐行读懂 `train` 函数中「训练阶段 + 验证阶段」的双循环，理解 `model.train()` / `model.eval()` 与 `torch.no_grad()` 的分工。
4. 理解 dropout 在训练/评估两种模式下的行为差异（并了解 batchnorm 同属此机制，但本仓库 ch14 代码未使用）。
5. 说清 Top-1 / Top-5 错误率与残差连接（ResNet）的概念定位。

> **与学习大纲的差异说明（重要）**：本讲在大纲中的原始标题是「手写 CNN 训练 CIFAR-10 与 ResNet」，学习目标里包含 `resnet18(pretrained=True)` 微调与 Top-1/Top-5 指标。但经逐行核实，本仓库 `ch14/` 目录的两个代码文件（`ch14_part1.py`、`ch14_part2.py`）以及两个 notebook 中**均不存在** CIFAR-10、`torchvision.models.resnet18` 或 Top-5 相关代码。仓库第 14 章的真实内容是：MNIST 手写数字 CNN（part1）与 CelebA 人脸微笑二分类 CNN（part2）。按照本手册「不编造不存在的代码」的原则，本讲以仓库真实代码为准组织内容，ResNet 与 Top-5 仅作为概念在 4.4 节讲解并明确标注出处边界。

## 2. 前置知识

本讲站在前面几讲的肩膀上，先快速对齐概念：

- **卷积与输出尺寸公式**（承接 u14-l1）：卷积核在输入上滑窗做互相关，输出尺寸为
  \[ O = \left\lfloor \frac{N + 2p - M}{s} \right\rfloor + 1 \]
  其中 \( N \) 是输入边长、\( M \) 是核边长、\( p \) 是零填充、\( s \) 是步幅。`MaxPool2d(kernel_size=2)` 约等于 \( s=2, M=2, p=0 \)，即边长减半。本讲要用它手动算出展平维度。
- **Dataset / DataLoader 契约**（承接 u12-l2）：Dataset 只需实现 `__len__` 与 `__getitem__`；`torchvision.datasets` 里的数据集遵守同一协议，`transform` 在 `__getitem__` 被调用时才惰性地作用于单张图片；DataLoader 负责堆批。
- **训练五步循环**（承接 u12-l3）：`pred = model(x)` → `loss = loss_fn(pred, y)` → `loss.backward()` → `optimizer.step()` → `optimizer.zero_grad()`。
- **训练/评估模式**（承接 u13-l1）：`nn.Module` 有个 `self.training` 标志，由 `model.train()` 置 True、`model.eval()` 置 False。dropout 只在训练模式随机置零，评估模式恒等；batchnorm 同样依赖这个开关（本仓库 ch14 未用 batchnorm，但机制相同）。
- **损失函数匹配**（承接 u12-l3）：多分类输出层给 logits、配 `nn.CrossEntropyLoss()`（内部做 softmax + 交叉熵）；二分类输出层配 sigmoid + `nn.BCELoss()`。
- **transforms 是什么**：torchvision 提供的图像变换函数（裁剪、翻转、缩放、转张量等），`transforms.Compose([...])` 把它们按顺序串成管线，输入输出一般是 PIL 图片或张量。

一个必须澄清的术语：大纲里说的「transforms 标准化」在本仓库代码中实际是 `transforms.ToTensor()`——它把 uint8 图片（0~255）转成浮点张量并**除以 255 缩放到 [0, 1]**。这是「归一化到固定区间」，而不是「减均值除标准差」的 z-score 标准化（后者在 torchvision 中叫 `transforms.Normalize(mean, std)`，本仓库 ch14 没有用到）。两者都能改善数值条件，但含义不同，面试和写代码时不要混用。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ch14/ch14_part1.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py) | 第 14 章前半部分：手写 conv1d/conv2d（u14-l1 已讲）、**MNIST 数据加载、两层卷积块 CNN、train 双循环、测试评估与模型保存**。本讲重点读它的后半段。 |
| [ch14/ch14_part2.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py) | 第 14 章后半部分：CelebA 人脸数据集加载、**随机数据增强 vs 确定性变换、四卷积块 + 全局平均池化的微笑二分类 CNN、train/valid/test 三循环**。 |
| [ch14/ch14_part1.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.ipynb) / [ch14/ch14_part2.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.ipynb) | 与 .py 等价的 notebook 版本（仓库的源头形态），适合交互式跑本讲的实践。 |
| [ch14/README.md](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/README.md) | 第 14 章大纲与 notebook 使用说明。 |

## 4. 核心概念与源码讲解

### 4.1 torchvision 数据集与 transforms 变换管线

#### 4.1.1 概念说明

torchvision 把「常见数据集 + 图像变换 + 读图工具」打包在一起。本讲用到它的三样东西：

1. **数据集类**：`torchvision.datasets.MNIST` / `CelebA`。构造参数里最关键的是 `transform`（对图像做什么变换）与 `target_transform`（对标签做什么变换）；它们会在每次取样本时执行（惰性，承接 u12-l2）。
2. **transforms 管线**：`transforms.Compose([...])` 按顺序执行变换。训练集可以放**随机变换**（RandomCrop、RandomHorizontalFlip）做数据增强——同一张图每个 epoch 被裁/翻成不同样子，等于免费扩充数据、抑制过拟合；验证/测试集必须用**确定性变换**（CenterCrop + Resize），保证评估可复现。
3. **Subset 划分**：`torch.utils.data.Subset(dataset, indices)` 从已有数据集按下标切出子集，用来从一个完整训练集里再分出验证集。

#### 4.1.2 核心流程

MNIST 输入管线的流程：

```text
下载/加载 MNIST(train=True) ──ToTensor──► 60000 个 (1×28×28 张量, 标签)
        │
        ├─ Subset(前 10000 个)  ──► 验证集 valid_dl（不洗牌）
        └─ Subset(10000 之后)   ──► 训练集 train_dl（shuffle=True）
两者都交给 DataLoader，按 batch_size=64 堆批
```

CelebA 输入管线的关键差异：训练集走 `transform_train`（随机裁剪 178×178 → 随机水平翻转 → 缩放 64×64 → ToTensor），验证/测试集走 `transform`（把随机操作换成确定性的 CenterCrop），并用 `target_transform=get_smile` 从 40 个属性里取出「是否微笑」这一位作为标签。

#### 4.1.3 源码精读

MNIST 的加载与划分（ch14_part1.py）：

- [ch14/ch14_part1.py:295-L308](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L295-L308)：`transform = transforms.Compose([transforms.ToTensor()])` 只做「转张量并缩放到 [0,1]」一件事；随后构造 MNIST 训练集（`train=True, download=True`）与测试集（`train=False`）。注意这里**没有** `transforms.Normalize`——再次印证 2 节的澄清。
- [ch14/ch14_part1.py:303-L304](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L303-L304)：`Subset(mnist_dataset, torch.arange(10000))` 取**前 1 万个**当验证集，`torch.arange(10000, len(...))` 取其余 5 万个当训练集——一个数据集切成两份的惯用法。
- [ch14/ch14_part1.py:315-L318](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L315-L318)：`batch_size = 64`；训练 DataLoader `shuffle=True`（每个 epoch 重新洗牌，承接 u12-l2「按类排序的数据必须洗牌」），验证 DataLoader `shuffle=False`。

CelebA 的两套 transform 与标签提取（ch14_part2.py）：

- [ch14/ch14_part2.py:192](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L192)：`get_smile = lambda attr: attr[18]`——CelebA 每个样本带 40 维属性向量，第 18 位是「Smiling」，用它做 `target_transform` 把多属性标签变成 0/1 标签。
- [ch14/ch14_part2.py:194-L199](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L194-L199)：`transform_train` = RandomCrop([178,178]) → RandomHorizontalFlip() → Resize([64,64]) → ToTensor()。前两步是随机的：同一个人的照片每个 epoch 都以不同的裁剪/朝向出现，这就是数据增强。
- [ch14/ch14_part2.py:201-L205](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L201-L205)：`transform` = CenterCrop([178,178]) → Resize([64,64]) → ToTensor()。把随机裁剪换成**中心**裁剪、去掉翻转——验证/测试要的是同一个输入永远得到同一个输出。
- [ch14/ch14_part2.py:211-L216](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L211-L216)：重新构造训练集时同时传入 `transform=transform_train` 与 `target_transform=get_smile`，图片与标签的变换在取样本时各自行使职责。
- [ch14/ch14_part2.py:261-L275](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L261-L275)：`Subset(..., torch.arange(16000))` 把 16 万+ 的训练集截到 1.6 万（书里为缩短训练时间的做法）；随后 train/valid/test 三个 DataLoader，训练的 shuffle=True，其余 False。

> 下载提示：CelebA 有每日下载限额，[ch14/ch14_part2.py:66-L77](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L66-L77) 的注释给出了手动下载与解压步骤，且要求 `download=False`。MNIST 则可自动下载，本讲实践优先用 MNIST。

#### 4.1.4 代码实践

1. **实践目标**：亲眼确认 ToTensor 的缩放行为与 Subset 划分结果。
2. **操作步骤**：在 `ch14/` 目录下新建一个脚本（不要改动仓库源码），写入：

   ```python
   # 示例代码：验证 ToTensor 与 Subset（需已安装 torch、torchvision）
   import torch, torchvision
   from torchvision import transforms
   from torch.utils.data import Subset, DataLoader

   transform = transforms.Compose([transforms.ToTensor()])
   ds = torchvision.datasets.MNIST(root='./', train=True, transform=transform, download=True)
   valid = Subset(ds, torch.arange(10000))
   train = Subset(ds, torch.arange(10000, len(ds)))
   dl = DataLoader(train, batch_size=64, shuffle=True)

   x, y = next(iter(dl))
   print('batch 形状:', x.shape, y.shape)   # 期望 torch.Size([64, 1, 28, 28]) / torch.Size([64])
   print('像素范围:', x.min().item(), x.max().item())  # 期望 0.0 ~ 1.0
   print('划分大小:', len(train), len(valid))          # 期望 50000 / 10000
   ```

3. **需要观察的现象**：像素最小值 0.0、最大值 1.0（证明 ToTensor 已除以 255）；batch 形状是 `(N, C, H, W) = (64, 1, 28, 28)`，通道维在最前。
4. **预期结果**：三行打印与注释一致。首次运行会自动下载 MNIST（约十几 MB）。
5. 本环境未执行该脚本，结果标注「待本地验证」。

#### 4.1.5 小练习与答案

1. **问**：`Subset(mnist_dataset, torch.arange(10000))` 切出的是训练集还是验证集？另 5 万个样本用了哪个下标区间？
   **答**：切出的是验证集（代码里命名为 `mnist_valid_dataset`）；训练集用 `torch.arange(10000, len(mnist_dataset))`，即下标 10000 到 59999。
2. **问**：为什么 `RandomHorizontalFlip` 只出现在 `transform_train`，而验证/测试用 `CenterCrop`？
   **答**：随机翻转是数据增强，目的是让训练分布更丰富；验证与测试要求对同一输入给出确定、可复现的评估，必须用确定性变换，否则每次评估分数都会随机波动。
3. **问**：`ToTensor()` 之后单张 MNIST 图片的形状与取值范围是什么？
   **答**：形状 `(1, 28, 28)`（CHW 约定），取值范围 `[0.0, 1.0]`（uint8 的 0~255 除以 255）。

### 4.2 自定义 CNN 模块结构：卷积块 + 全连接分类头

#### 4.2.1 概念说明

典型 CNN 分两段：

- **特征提取器（backbone）**：重复「Conv2d → ReLU → MaxPool2d」卷积块。每过一个块，空间尺寸变小、通道数（特征种类）变多——这就是「特征层级」：浅层学边缘纹理，深层学部件与整体。
- **分类头（head）**：`Flatten` 把 `(C, H, W)` 摊平成一维，接 `Linear → ReLU → Dropout → Linear` 完成分类。

两个网络各代表一种头部设计：

- MNIST CNN 用** Flatten + 两个全连接**（3136 → 1024 → 10），参数量大但表达力强。
- CelebA CNN 用**全局平均池化（Global Average Pooling, GAP）+ 单个全连接**（256 → 1）：对每个通道的整个 8×8 特征图取平均，直接得到 256 维向量。GAP 没有可学参数、天然对输入尺寸不敏感，还把「特征在哪」压缩成「有哪些特征」。

#### 4.2.2 核心流程

MNIST CNN 的逐层尺寸（输入 1×28×28，卷积核 5、填充 2、池化 2）：

```text
输入 (1, 28, 28)
 ├─ conv1: 1→32 通道, k=5, p=2  → (32, 28, 28)   # 28 → ⌊(28+4-5)/1⌋+1 = 28，尺寸不变
 ├─ pool1: k=2                 → (32, 14, 14)   # 边长减半
 ├─ conv2: 32→64, k=5, p=2     → (64, 14, 14)
 ├─ pool2: k=2                 → (64, 7, 7)
 ├─ flatten                    → 64×7×7 = 3136
 ├─ fc1: 3136→1024 + ReLU + Dropout(0.5)
 └─ fc2: 1024→10                # logits，10 类
```

CelebA CNN 的逐层尺寸（输入 3×64×64，卷积核 3、填充 1，前三个块带池化）：

```text
输入 (3, 64, 64)
 ├─ conv1: 3→32  + pool → (32, 32, 32)
 ├─ conv2: 32→64 + pool → (64, 16, 16)
 ├─ conv3: 64→128 + pool → (128, 8, 8)
 ├─ conv4: 128→256（无池化） → (256, 8, 8)
 ├─ AvgPool2d(8)（GAP）      → (256, 1, 1)
 ├─ flatten                  → 256
 └─ fc: 256→1 + Sigmoid       # 微笑概率，二分类
```

#### 4.2.3 源码精读

MNIST CNN（ch14_part1.py）：

- [ch14/ch14_part1.py:345-L351](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L345-L351)：用 `nn.Sequential()` + `add_module(名字, 层)` 逐层搭两个卷积块。命名的好处是打印模型与后续引用（如 `model.conv1`）都清晰。`Conv2d(in_channels=1, out_channels=32, kernel_size=5, padding=2)`：p=2 恰好让 k=5 的卷积保持空间尺寸不变（\(\lfloor(28+4-5)/1\rfloor+1=28\)）。
- [ch14/ch14_part1.py:353-L354](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L353-L354)：`x = torch.ones((4, 1, 28, 28)); model(x).shape`——书中演示的「加一段模块就喂个假输入看形状」的调试技巧，比心算更可靠。
- [ch14/ch14_part1.py:359-L362](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L359-L362)：加 `nn.Flatten()` 后再次验证形状，确认摊平结果是 3136。
- [ch14/ch14_part1.py:367-L371](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L367-L371)：分类头三件套 `Linear(3136, 1024)` → ReLU → `Dropout(p=0.5)`，再 `Linear(1024, 10)` 输出 10 类 logits。dropout 夹在两个全连接之间，训练时随机丢弃一半隐藏单元以抑制过拟合（承接 u14-l1）。
- [ch14/ch14_part1.py:384-L385](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L384-L385)：`CrossEntropyLoss()` + `Adam(lr=0.001)`。注意模型末尾**没有** softmax——CrossEntropyLoss 要求输入 logits，内部自带 log-softmax（承接 u12-l3）。

CelebA CNN（ch14_part2.py）：

- [ch14/ch14_part2.py:289-L307](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L289-L307)：四个卷积块，通道 3→32→64→128→256；前两个块后接 `Dropout(p=0.5)`（卷积后接 dropout、池化后也接，正则化更激进）；conv4 只有卷积+ReLU，池化留给后面的 GAP。
- [ch14/ch14_part2.py:311-L312](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L311-L312)：先验证到 conv4 为止的输出形状是 `(4, 256, 8, 8)`——64 经三次 2×2 池化变成 8。
- [ch14/ch14_part2.py:317-L318](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L317-L318)：`AvgPool2d(kernel_size=8)` 对 8×8 特征图整体求平均，等价于全局平均池化；[L320-L321](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L320-L321) 验证形状变 `(4, 256)`。
- [ch14/ch14_part2.py:326-L327](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L326-L327)：`Linear(256, 1)` + `Sigmoid()` 输出微笑概率——输出层按任务匹配（二分类 → sigmoid，承接 u12-l3），所以损失函数用 `BCELoss()`（[L350](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L350)）。

#### 4.2.4 代码实践

1. **实践目标**：用尺寸公式手算展平维度，再用假输入验证——这正是大纲实践任务的后半部分（「把第一个全连接输入维度按卷积输出尺寸公式手动计算一遍」；大纲原文写的 `num_features`，在仓库代码里对应 `fc1` 的 `in_features=3136`）。
2. **操作步骤**：
   - 先在纸上算：MNIST 输入 28×28，conv1 (k=5, p=2, s=1) 后 \(\lfloor(28+2\times2-5)/1\rfloor+1=28\)；pool1 后 14；conv2 后 14；pool2 后 7；通道 64；展平 = \(64 \times 7 \times 7 = 3136\)。
   - 再跑「示例代码」核对：

   ```python
   # 示例代码：逐段验证 MNIST CNN 的形状
   import torch, torch.nn as nn
   model = nn.Sequential()
   model.add_module('conv1', nn.Conv2d(1, 32, kernel_size=5, padding=2))
   model.add_module('relu1', nn.ReLU())
   model.add_module('pool1', nn.MaxPool2d(kernel_size=2))
   model.add_module('conv2', nn.Conv2d(32, 64, kernel_size=5, padding=2))
   model.add_module('relu2', nn.ReLU())
   model.add_module('pool2', nn.MaxPool2d(kernel_size=2))
   model.add_module('flatten', nn.Flatten())
   x = torch.ones((4, 1, 28, 28))
   print(model(x).shape)          # 期望 torch.Size([4, 3136])
   print(64 * 7 * 7)              # 期望 3136
   ```

3. **需要观察的现象**：打印 `torch.Size([4, 3136])`，与手算的 \(64\times7\times7\) 一致。
4. **预期结果**：完全一致则说明你掌握了公式；若不一致，检查填充与池化的参数。「待本地验证」。
5. 附加思考：如果把 `pool2` 去掉，`fc1` 的输入维度应改成多少？（答案见下方练习 3。）

#### 4.2.5 小练习与答案

1. **问**：CelebA CNN 输入 64×64，到 conv4 输出时空间尺寸为什么是 8×8？
   **答**：64 经 pool1、pool2、pool3 三次 k=2 的最大池化，每次减半：64→32→16→8；conv4 (k=3, p=1) 保持尺寸不变。
2. **问**：全局平均池化 `AvgPool2d(kernel_size=8)` 之后为什么恰好是 256 维？
   **答**：特征图是 (256, 8, 8)，kernel_size=8 覆盖整张图，每个通道输出 1 个数，得到 (256, 1, 1)，Flatten 后即 256 维。
3. **问**：若去掉 MNIST CNN 的 `pool2`，`fc1` 的输入维度要改成多少？
   **答**：pool2 去掉后特征图为 (64, 14, 14)，展平 = \(64\times14\times14=12544\)，`fc1` 应为 `Linear(12544, 1024)`。
4. **问**：为什么 MNIST 网络的输出层后没有 softmax，CelebA 网络却有 sigmoid？
   **答**：MNIST 用 `CrossEntropyLoss`，它要求输入 logits（内部自带更数值稳定的 log-softmax）；CelebA 用 `BCELoss`，它要求输入概率，所以模型末尾显式加了 Sigmoid。

### 4.3 train/valid 双阶段训练循环

#### 4.3.1 概念说明

`train` 函数是本讲的灵魂，骨架是「外层 epoch、内层两个阶段」：

- **训练阶段**：`model.train()` 打开训练模式（dropout 生效）；遍历训练 DataLoader，执行五步循环；同时累计批损失与正确数。
- **验证阶段**：`model.eval()` 切到评估模式（dropout 关闭）；`torch.no_grad()` 关掉梯度追踪——验证不需要反向传播，不建计算图可省大量显存并提速；遍历验证 DataLoader 只前向统计。

两个统计技巧贯穿始终：

- **批损失的加权累加**：`loss.item() * y_batch.size(0)` 把「批平均损失」换算成「批内样本损失之和」，epoch 结束除以 `len(train_dl.dataset)`（样本总数），得到**按样本加权的 epoch 平均损失**——最后一批不满（比如 50000 = 781×64 − 8，末批只有 56 个）时也不会被高估（与 u13-l3 讲的 torchmetrics「按样本加权」思想一致）。
- **向量化准确率**：`(torch.argmax(pred, dim=1) == y_batch).float()` 一次算出整批的对错 0/1 向量，`.sum().cpu()` 把 GPU 上的计数搬回 CPU 累加。

两个文件里的 `train` 函数结构几乎逐行相同，只差输出层的用法（多分类 `argmax` vs 二分类 `[:, 0]` + 阈值 0.5）——这本身就是「训练循环可以抽成通用组件」的证据（u13-l2 正是这么做的）。

#### 4.3.2 核心流程

```text
for epoch in range(num_epochs):
    model.train()                        # 开启 dropout
    for x_batch, y_batch in train_dl:
        搬到 GPU → 前向 → 算损失 → backward → step → zero_grad
        累计 加权批损失、正确数
    除以训练集样本数 → 本 epoch 训练损失/准确率

    model.eval()                         # 关闭 dropout
    with torch.no_grad():                # 不建计算图
        for x_batch, y_batch in valid_dl:
            前向 → 算损失 → 累计
    除以验证集样本数 → 本 epoch 验证损失/准确率
    打印一行进度
返回 4 条历史曲线（train/valid 的 loss 与 acc）
```

#### 4.3.3 源码精读

MNIST 版 train（ch14_part1.py）：

- [ch14/ch14_part1.py:387-L391](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L387-L391)：函数签名 `train(model, num_epochs, train_dl, valid_dl)`；四条历史列表预分配为 `[0] * num_epochs`，下标即 epoch。
- [ch14/ch14_part1.py:392-L404](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L392-L404)：`model.train()` 开训练模式；每个 batch 先 `.to(device)` 搬到 GPU；五步循环（前向、损失、backward、step、zero_grad，顺序承接 u12-l3）；`loss.item()` 把单元素张量转成 Python 数以便累加。
- [ch14/ch14_part1.py:406-L407](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L406-L407)：epoch 内累计值除以 `len(train_dl.dataset)`（样本数而非批数）——批大小不整除时依然正确。
- [ch14/ch14_part1.py:409-L421](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L409-L421)：`model.eval()` + `with torch.no_grad():` 包住验证循环，结构同训练但无 backward/step；末尾同样按样本数归一。
- [ch14/ch14_part1.py:423](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L423)：每 epoch 打印训练/验证准确率，便于随时观察是否过拟合（训练涨、验证跌）。
- [ch14/ch14_part1.py:426-L428](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L426-L428)：`num_epochs = 20`，调用 `train` 得到 `hist`。（注意：大纲实践任务里写的是「epoch 从 10 降到 2」，仓库真实默认值是 20，实践时以 20 为基准。）
- [ch14/ch14_part1.py:457-L461](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L457-L461)：测试评估——先把模型 `.cpu()`，再把整个测试集张量 `data.unsqueeze(1) / 255.` 一次性前向（这里手动做了 ToTensor 等价的除以 255），统计 Top-1 准确率并打印。
- [ch14/ch14_part1.py:488-L492](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L488-L492)：`torch.save(model, path)` 整模型保存（承接 u13-l2 讲过的两种保存方式）。

CelebA 版 train 的差异点（ch14_part2.py）：

- [ch14/ch14_part2.py:353-L390](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L353-L390)：函数骨架与 MNIST 版完全一致，仅两处适配二分类：`pred = model(x_batch)[:, 0]` 去掉多余的通道维；正确判定改为 `(pred >= 0.5).float() == y_batch`（概率过半算微笑）。
- [ch14/ch14_part2.py:422-L435](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L422-L435)：测试循环显式 `model.eval()` + `torch.no_grad()`，逐批累计——当测试集大到一个张量放不下时，这种逐批写法比 MNIST 版的「一次性前向」更通用。

#### 4.3.4 代码实践

1. **实践目标**：以最小代价跑通 MNIST CNN 全流程（大纲实践任务的前半部分：先把轮数调小跑通）。
2. **操作步骤**：
   - 把 `ch14_part1.py` 另存一份到自己的练习目录（**不要修改仓库源文件**），做两处修改：
     a. [L427](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L427) 的 `num_epochs = 20` 改为 `num_epochs = 2`；
     b. 没有 GPU 时把 [L376](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L376) 的 `device = torch.device("cuda:0")` 换成下一行注释里的 `torch.device("cpu")`。
   - 运行脚本，观察每个 epoch 结束时打印的 `accuracy` 与 `val_accuracy`。
3. **需要观察的现象**：第 1 个 epoch 训练准确率就应该明显超过 10%（随机猜的水平），验证准确率通常已有 97% 上下；第 2 个 epoch 两者继续小幅上升。（2 个 epoch 在 CPU 上约需十几分钟量级，取决于机器。）
4. **预期结果**：跑通后得到 2 个 epoch 的 `hist`，四条曲线各 2 个点；训练/验证准确率都在 95% 以上。具体数值「待本地验证」。
5. 跑通后可再把 `num_epochs` 恢复为 20 观察完整曲线（时间充裕时）。

#### 4.3.5 小练习与答案

1. **问**：`model.train()` / `model.eval()` 分别影响本讲网络中的哪个层？原理是什么？
   **答**：影响 `Dropout(p=0.5)`。dropout 模块内部读 `self.training` 标志：训练模式随机把一半激活置零并放大其余激活；评估模式变为恒等映射。batchnorm 也依赖同一开关，但本仓库 ch14 的两个 CNN 都没有用 batchnorm。
2. **问**：验证循环为什么要包在 `with torch.no_grad():` 里？
   **答**：验证只需要前向结果，不需要梯度。`no_grad` 让 PyTorch 不记录中间计算图，省显存、提速，也避免把验证操作误加入梯度累计。
3. **问**：`loss_hist_train[epoch] += loss.item() * y_batch.size(0)` 里为什么要乘批大小？
   **答**：`loss_fn` 返回的是批内**平均**损失；乘以批大小换算成批内损失之和，最后除以样本总数，得到按样本加权的 epoch 平均。若最后一批不满（56 个样本），直接按「批数 × 批平均」会把小批的权重放大。
4. **问**：为什么训练 DataLoader `shuffle=True` 而验证 DataLoader `shuffle=False`？
   **答**：训练时洗牌让每个 batch 组合随 epoch 变化、梯度估计更无偏（u12-l2）；验证只需遍历全集统计，顺序不影响结果，不洗牌还能保证可复现。

### 4.4 ResNet、Top-1/Top-5 与本仓库的真实边界

#### 4.4.1 概念说明

这一节对应大纲里「resnet18 微调」与「Top-1/Top-5 错误率」两个学习目标。先讲概念，再划清仓库边界。

**残差连接（ResNet 的核心）**：普通深层网络学的是 \( H(x) \)，层数一深梯度在反向传播中逐层衰减（梯度消失），网络反而变差。ResNet（2015）把要学的东西改成残差 \( F(x) = H(x) - x \)，模块输出为

\[ y = F(x, \{W_i\}) + x \]

恒等捷径 \( +x \) 给梯度提供了一条「直通路」（对 \(x\) 的偏导至少有 1），使几十层甚至上百层的网络也能稳定训练。这个思想后来渗透到 Transformer（u16 的残差连接）等几乎所有深层架构。

**Top-1 / Top-5 错误率**：ImageNet 有 1000 个类别，模型输出一个概率排序。Top-1 错误率 = 最高分类别不是正确答案的比例（即 1 − 我们一直算的准确率）；Top-5 错误率 = 正确答案不在前 5 名的比例。类别多、语义相近（如不同犬种）时，Top-5 是更宽容的模型质量指标。本讲代码里 `torch.argmax(pred, dim=1)` 取的就是 Top-1 预测。

**batchnorm vs dropout 的 train/eval 差异**：dropout 训练时随机置零、评估时恒等；batchnorm 训练时用当前批统计量并更新运行均值/方差，评估时改用累积的运行统计量——两者都必须随 `model.train()`/`model.eval()` 切换，忘切是深度学习最常见的 bug 之一（评估忘记 `model.eval()` 会让 dropout 随机化验证结果）。本仓库 ch14 只出现 dropout。

#### 4.4.2 核心流程

以一个典型残差块为例（概念示意，非仓库代码）：

```text
输入 x ──┬─────────────────► (+) ──► ReLU ──► 输出 y
         │                    ↑
         └── Conv-ReLU-Conv ──┘
             （学残差 F(x)）
```

#### 4.4.3 源码精读（仓库边界核实）

- 本人对 `ch14/` 全目录做过关键词检索（`cifar`、`resnet`、`top-5` 等，大小写不敏感）：[ch14/ch14_part1.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py) 与 [ch14/ch14_part2.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py) 中**没有**任何 CIFAR-10 数据集、`torchvision.models.resnet18` 或 Top-5 计算的代码；`ch14_part2.ipynb` 中唯一的「命中」来自内嵌图片的 base64 数据，属偶然字节序列。ResNet、GoogLeNet 等架构与 ImageNet 基准属于书第 14 章正文的图文内容，未进入配套代码。
- 仓库中与「预训练权重」真正相关的代码不在 ch14：例如 [ch18/ch18_part2.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.py) 使用的 MoleculeNet 分子数据集（第 18 章 GNN），与本讲的 ImageNet 预训练是不同场景。
- 若你想亲手试一下 `resnet18`，以下为**示例代码**（非仓库代码，仅演示 `torchvision.models` 的真实接口，需本地安装 torchvision 才能运行）：

  ```python
  # 示例代码：加载 ImageNet 预训练的 ResNet18（不属于本仓库）
  from torchvision import models
  import torch
  model = models.resnet18(weights=models.ResNet18_Weights.IMAGENET1K_V1)  # 新版写法
  # 旧版写法 models.resnet18(pretrained=True) 仍见于大量教程，但已被弃用
  model.eval()
  with torch.no_grad():
      logits = model(torch.ones(1, 3, 224, 224))
  top5 = torch.topk(logits, k=5)   # Top-5 类别与分数
  ```

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：在不安装任何东西的前提下，把「Top-1/Top-5」与本讲源码中的对应点挂钩。
2. **操作步骤**：
   - 打开 [ch14/ch14_part1.py:403](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L403)，确认 `torch.argmax(pred, dim=1)` 取的是每行最大分量的下标——这就是 Top-1 预测。
   - 思考：若 MNIST（10 类）要算 Top-5 准确率，应把这一行换成什么？（答案见练习 1。）
   - 对照 [ch14/ch14_part1.py:393](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L393) 与 [L409](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L409)，数一数 `model.train()`/`model.eval()` 各出现在哪个阶段，验证 4.3 节的结论。
3. **需要观察的现象**：纯阅读即可完成，无需运行。
4. **预期结果**：能独立说出「Top-1 预测 = argmax；Top-5 = 排序后取前 5 看是否包含真值」。
5. 若尝试 4.4.3 的 resnet18 示例代码，其输出「待本地验证」（需要下载约 45 MB 的预训练权重）。

#### 4.4.5 小练习与答案

1. **问**：把 `torch.argmax(pred, dim=1) == y_batch` 改写成 Top-5 判定，该怎么写？
   **答**：`top5 = torch.topk(pred, k=5, dim=1).indices`，然后 `(top5 == y_batch.unsqueeze(1)).any(dim=1)` 得到「真值是否在 前 5」的布尔向量，取 `.float().mean()` 即 Top-5 准确率。
2. **问**：残差连接为什么能缓解梯度消失？
   **答**：输出 \( y = F(x) + x \) 对 \( x \) 的导数为 \( \partial F/\partial x + 1 \)，那个常数 1 保证梯度至少原样回传一部分，不随层数连乘而消失。
3. **问**：为什么本讲不引用仓库里的 `resnet18(pretrained=True)` 代码？
   **答**：因为仓库 ch14 中不存在这段代码；本手册的原则是只引用真实存在的源码，杜绝编造。相关的 torchvision 接口演示均明确标注为「示例代码」。

## 5. 综合实践

**任务：以最小成本完整走一遍「数据管线 → 建模 → 双阶段训练 → 尺寸验证」闭环。**

1. 复制 `ch14/ch14_part1.py` 到自己的练习目录，改造为快速版：`num_epochs = 2`、`device = cpu`（无 GPU 时）。
2. 运行前先在纸上完成两张「尺寸推演表」：
   - MNIST CNN：28 →（conv k5 p2）→ 28 →（pool2）→ 14 →（conv k5 p2）→ 14 →（pool2）→ 7，展平 \( 64\times7\times7 = 3136 \)；
   - CelebA CNN：64 → 32 → 16 → 8（三次 pool），GAP 后 256。
3. 运行脚本，记录两个 epoch 的 `accuracy` / `val_accuracy`，再对照打印确认 `fc1` 的输入维度确实是 3136（与 [ch14/ch14_part1.py:367](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L367) 的 `nn.Linear(3136, 1024)` 一致）。
4. 进阶（可选）：仿照 [ch14/ch14_part1.py:345-L371](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L345-L371) 再搭一个「三卷积块」的变体（例如 32/64/128 通道、各带 pool2），自己算出展平维度并写对 `Linear` 的输入维数，跑一个 epoch 验证没有形状报错。
5. 全部数值结论标注「待本地验证」——本讲义编写环境未执行训练。

## 6. 本讲小结

- torchvision 输入管线三件套：`datasets.XXX(transform=...)` 惰性变换、`transforms.Compose` 串联管线（训练用随机增强、验证/测试用确定性变换）、`Subset` + `DataLoader` 划分与堆批；仓库用的 `ToTensor` 是除以 255 的区间缩放，不是 z-score 标准化。
- CNN = 卷积块堆叠（空间变小、通道变多）+ 分类头；MNIST 版用 Flatten + 双全连接（3136→1024→10），CelebA 版用全局平均池化 + 单全连接（256→1），后者参数更少且对输入尺寸不敏感。
- `train` 函数的双阶段骨架：`model.train()` 下走五步循环，`model.eval()` + `torch.no_grad()` 下只前向统计；损失乘批大小再除以样本总数，得到按样本加权的 epoch 平均。
- 输出层与损失必须配对：多分类 logits + `CrossEntropyLoss`（网络末尾不加 softmax），二分类 sigmoid + `BCELoss`（判定用 `pred >= 0.5`）。
- dropout 是 `train()`/`eval()` 切换的实际受益者（batchnorm 同机制但仓库 ch14 未用）；评估忘写 `model.eval()` 会让验证结果随机波动。
- **边界澄清**：大纲中的 CIFAR-10、`resnet18(pretrained=True)`、Top-5 实验在本仓库 ch14 代码中不存在；ResNet 与 Top-1/Top-5 作为概念掌握即可，涉及 torchvision.models 的代码均为标注过的示例。

## 7. 下一步学习建议

- 下一讲（u14-l3）主题为卷积网络的迁移学习；鉴于本仓库 ch14 未包含 `torchvision.models` 微调代码，届时同样会以「仓库真实源码 + 明确标注的示例代码」方式处理，建议先读完本讲的 4.4 节建立概念。
- 回到源码巩固：重读 [ch14/ch14_part2.py:353-L390](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part2.py#L353-L390)，把它与 [ch14/ch14_part1.py:387-L424](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch14/ch14_part1.py#L387-L424) 并排对照，体会「训练循环与任务解耦」的抽象方式，这正是 u13-l2 Lightning `training_step`/`validation_step` 拆分的雏形。
- 若想看序列数据如何替换本讲的卷积特征提取器，预习 [ch15/ch15_part1.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part1.py)（RNN 手算示例，u15-l1）。
- 延伸阅读（仓库外）：torchvision 官方文档的 `models` 页面与 ResNet 原论文 *Deep Residual Learning for Image Recognition*（He et al., 2015），补齐 4.4 节概念对应的工程细节。
