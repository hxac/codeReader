# 经典早期架构：LeNet-5 与 AlexNet

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂用 `nn.Sequential` 堆叠式风格定义的 CNN 模型代码（`features` + `classifier` 两段式），并能逐层推导特征图尺寸变化。
2. 理解「同一模型，不同数据集」带来的结构适配：LeNet-5 从 MNIST（28×28 灰度）迁移到 CIFAR-10（32×32 彩色）时代码做了哪些修改、为什么这样改。
3. 说出 AlexNet 相对 LeNet-5 引入的「现代化元素」（ReLU、重叠最大池化、Dropout、大数据增强），理解 `AdaptiveAvgPool2d` 为什么能让分类头与输入尺寸解耦。
4. 掌握分组卷积（grouped convolutions）的通道切分规则、参数量缩减公式 \( \frac{1}{g} \)，以及 PyTorch 中 `groups` 参数的使用约束。

本讲承接 [u3-l1](u3-l1-cnn-basics.md) 的卷积/池化尺寸公式与 He 初始化，是进入 VGG、ResNet 等更深架构前的「历史台阶」。

## 2. 前置知识

在阅读本讲之前，请确认你已理解以下概念（前几讲已建立，这里只做简短回顾）：

- **卷积输出尺寸公式**（u3-l1）：输入宽 \( w \)、卷积核 \( k \)、填充 \( p \)、步长 \( s \) 时，输出尺寸为

  \[ o = \left\lfloor \frac{w - k + 2p}{s} \right\rfloor + 1 \]

  `MaxPool2d(kernel_size=2)` 不指定 stride 时默认 stride 等于 kernel size（即 2），输出尺寸减半。

- **`nn.Module` 自定义模型**（u2-l3）：在 `__init__` 里创建层、在 `forward` 里描述前向计算。本讲的模型仍然是 `nn.Module` 子类，只是把层「打包」进了 `nn.Sequential`。

- **`nn.Sequential` 容器**：把多个层按顺序串成一条管道，数据依次流过每一层。`Sequential` 本身也是 `nn.Module`，所以可以像调用函数一样 `self.features(x)`。它等价于在 `forward` 里手写一行一层的调用，但更紧凑、不易写错顺序——这是本讲两个模型共同采用的「堆叠式」定义风格。

- **`features` / `classifier` 两段式命名**：作者习惯把卷积+池化部分命名为 `self.features`，把末端的全连接部分命名为 `self.classifier`，中间用 `flatten` 衔接。这个命名约定贯穿整个 `pytorch_ipynb/cnn/` 目录，认得它就能快速定位模型结构代码。

- **数据集背景**：MNIST 是 28×28 的灰度手写数字图（10 类）；CIFAR-10 是 32×32 的彩色实物照片（10 类），颜色、姿态、背景变化都远大于 MNIST，所以难度显著更高——同样的模型在两个数据集上的准确率差距很大，这是预期内的。

- **历史坐标**（帮你在心里放一张时间轴）：LeNet-5（LeCun 等，1998）用于支票手写数字识别，被视为卷积网络的鼻祖；AlexNet（Krizhevsky 等，2012）在 ImageNet 竞赛中大幅领先第二名，开启了深度学习浪潮。本讲读的 notebook 就是这两个架构的现代化 PyTorch 复刻。

> **关于行号的说明**：本讲的源码是 Jupyter notebook，永久链接中的行号指向 notebook 的 JSON 源文件行（GitHub 会定位到对应单元格附近）。文中引用的准确率数字（如 98.93%）均为 notebook 中**作者保存的运行输出**，你本地复现的数字可能因环境与随机性略有出入。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pytorch_ipynb/cnn/cnn-lenet5-mnist.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-lenet5-mnist.ipynb) | LeNet-5 在 MNIST 上的完整实现：Resize 到 32×32、`LeNet5` 类、训练与评估（保存的测试准确率 98.93%） |
| [pytorch_ipynb/cnn/cnn-lenet5-cifar10.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-lenet5-cifar10.ipynb) | 同一个 `LeNet5` 类的 CIFAR-10 彩色版：通道数按 `in_channels` 等比放大（保存的测试准确率 67.30%） |
| [pytorch_ipynb/cnn/cnn-alexnet-cifar10.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-alexnet-cifar10.ipynb) | AlexNet 在 CIFAR-10 上的实现：64×64 输入、`AdaptiveAvgPool2d`、Dropout 分类头，并首次（在本讲范围内）改用 helper 共享模块组织训练（保存的测试准确率 72.11%） |
| [pytorch_ipynb/cnn/cnn-alexnet-cifar10-grouped.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-alexnet-cifar10-grouped.ipynb) | 带分组卷积的 AlexNet：`Conv2d(..., groups=2)`，并在 Markdown 单元格里详细讲解了分组卷积原理（保存的测试准确率 70.93%） |
| [pytorch_ipynb/helper_train.py](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_train.py) | 共享训练循环 `train_classifier_simple_v1`（u1-l3 已导览，AlexNet notebook 直接调用它） |
| [pytorch_ipynb/helper_data.py](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_data.py) | 共享数据加载 `get_dataloaders_cifar10`（返回 train/valid/test 三个 DataLoader） |
| [README.md](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md) | 模型动物园总目录：LeNet-5 条目在 L80–L82，AlexNet 条目在 L56–L57 |

四份 notebook 共用「Imports → Settings/Dataset → Model → Training → Evaluation」五段式结构（u1-l2 建立的认知），前两份把训练循环写在 notebook 里，后两份则调用 `pytorch_ipynb/` 下的共享 helper——这正是仓库新旧两代 notebook 风格的分水岭。

## 4. 核心概念与源码讲解

### 4.1 LeNet-5 结构：Sequential 堆叠式定义

#### 4.1.1 概念说明

LeNet-5 是 1998 年 LeCun 等人为手写数字识别设计的 7 层卷积网络，被公认为 CNN 的鼻祖。原始论文的结构是：3 个卷积层（C1、C3、C5）夹着 2 个下采样层（S2、S4），末端是全连接层 F6 和输出层；所有卷积核都是 5×5、stride 1，激活函数用 tanh，输出层用 RBF 神经元。

notebook 的开篇 Markdown 对此有一段忠实转述，并声明做了现代化替换（激活改 ReLU、输出改 softmax、平均池化改最大池化）。这里有一个**值得批判性阅读的细节**：Markdown 里写「In this notebook, we replace these with ReLU activations」，但实际代码中的激活层是 `nn.Tanh()`——说明文字与代码并不一致，**以代码为准**。真正的替换只有两处：平均池化换成了 `MaxPool2d`，输出端在返回 logits 之外顺带返回了 `softmax` 概率。读开源代码时养成「先看代码再看注释」的习惯，注释会过时，代码不会。

为什么这个 26 年前的结构值得先读？因为它小到可以整页读完（约 40 行），却已经包含现代 CNN 的全部骨架要素：交替的「卷积-激活-池化」、通道数逐层增多、空间尺寸逐层减小、末端展平接全连接。读懂 LeNet-5，后面所有 CNN 都只是「往 `Sequential` 里塞更多层」。

#### 4.1.2 核心流程

MNIST 原图 28×28，先 `Resize((32, 32))` 补到论文的 32×32 输入，然后数据流如下（\( o = \lfloor\frac{w-k+2p}{s}\rfloor + 1 \) 反复套用）：

```
输入            [N,  1, 32, 32]   灰度图，通道 1
Conv2d(1→6, 5×5, p=0, s=1)  → [N,  6, 28, 28]   32-5+1=28
MaxPool2d(2×2, s=2)        → [N,  6, 14, 14]   减半
Conv2d(6→16, 5×5, p=0, s=1) → [N, 16, 10, 10]  14-5+1=10
MaxPool2d(2×2, s=2)        → [N, 16,  5,  5]   减半
flatten                    → [N, 400]          16×5×5=400
Linear(400→120) → Tanh     → [N, 120]
Linear(120→84)  → Tanh     → [N, 84]
Linear(84→10)              → [N, 10]           logits
```

两条主线规律（u3-l1 已总结，这里再次印证）：

- **空间尺寸逐层压缩**：32 → 28 → 14 → 10 → 5，卷积（无填充）每次减 4，池化每次减半。
- **通道数逐层增多**：1 → 6 → 16，空间信息被逐步「兑换」成通道（语义）信息；`flatten` 前的 `16×5×5` 必须与分类头第一层的输入特征数 `16*5*5` **精确匹配**，差一个数都会在运行时报形状错误。

#### 4.1.3 源码精读

**（1）超参与设备设置。**
[pytorch_ipynb/cnn/cnn-lenet5-mnist.ipynb:L187-L203](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-lenet5-mnist.ipynb#L187-L203) 中定义了 `RANDOM_SEED=1`、`LEARNING_RATE=0.001`、`BATCH_SIZE=128`、`NUM_EPOCHS=10`、`NUM_FEATURES=32*32`、`GRAYSCALE=True`。注意 `DEVICE = "cuda:1"` 是写死的作者机器第二块 GPU——你本地运行时应改成 `"cuda:0"` 或沿用 u1-l2 讲过的 `torch.cuda.is_available()` 自动选择写法。

**（2）数据：把 28×28 拉伸到 32×32。**
[pytorch_ipynb/cnn/cnn-lenet5-mnist.ipynb:L230-L234](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-lenet5-mnist.ipynb#L230-L234) 中，`transforms.Compose([transforms.Resize((32, 32)), transforms.ToTensor()])` 负责把 MNIST 缩放到 32×32——这是为了**原样复刻论文输入尺寸**，从而原样保留论文的卷积核与通道配置。代价是图像被轻微拉伸变形；另一条路是保持 28×28、修改网络结构（见 4.1.5 练习 3）。训练 loader 开 `shuffle=True`，测试 loader 不 shuffle（[L247-L254](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-lenet5-mnist.ipynb#L247-L254)）。

**（3）模型定义——本讲的主角。**
[pytorch_ipynb/cnn/cnn-lenet5-mnist.ipynb:L304-L341](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-lenet5-mnist.ipynb#L304-L341) 定义了 `LeNet5` 类，要点拆开看：

- 构造参数 `grayscale=False` 在 [L313-L316](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-lenet5-mnist.ipynb#L313-L316) 决定输入通道数：灰度 1、彩色 3。一个类同时服务 MNIST 与 CIFAR-10，靠的就是这个开关。
- 卷积主干 `self.features`（[L317-L325](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-lenet5-mnist.ipynb#L317-L325)）：六个层按「卷积→tanh→池化→卷积→tanh→池化」顺序塞进一个 `Sequential`。注意这里**没有** u3-l1 里那种自定义 `forward` 逐层调用——`Sequential` 替你按序执行。
- 分类头 `self.classifier`（[L327-L333](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-lenet5-mnist.ipynb#L327-L333)）：三个 `Linear` 夹两个 `tanh`，第一层输入特征数 `16*5*5` 正是 flatten 后的 400。
- `forward`（[L336-L341](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-lenet5-mnist.ipynb#L336-L341)）只做四件事：过 `features`、`torch.flatten(x, 1)`（从第 1 维开始展平，保留 batch 维）、过 `classifier`、算 `softmax` 概率，然后 **同时返回 `logits` 和 `probas`**。

**（4）训练循环与损失。**
[pytorch_ipynb/cnn/cnn-lenet5-mnist.ipynb:L543-L585](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-lenet5-mnist.ipynb#L543-L585) 是手写版的 epoch×batch 双层循环：`model.train()`（L551）→ 五步范式 `forward → F.cross_entropy(logits, targets)（L559） → zero_grad → backward → step` → `model.eval()`（L575）后在 `torch.set_grad_enabled(False)` 下算训练准确率。两个细节呼应前几讲：

- 损失喂的是 **logits** 不是 probas——`F.cross_entropy` 内部自带数值稳定的 log-softmax（u2-l2 的结论），`probas` 只用于预测和人读。
- `model.train()`/`model.eval()` 在本模型里其实**没有实际作用**：tanh、卷积、全连接都没有随训练/评估模式切换的行为（Dropout/BatchNorm 才有，u2-l4）。写上它是无害的好习惯，为将来换正则化层留好接口。

**（5）结果。**
优化器是 Adam（[L359-L364](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-lenet5-mnist.ipynb#L359-L364)），10 个 epoch 后保存的测试准确率为 **98.93%**（输出见 [L628](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-lenet5-mnist.ipynb#L628)，打印代码在 [L634](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-lenet5-mnist.ipynb#L634)）。notebook 末尾还有一个小演示（[L682-L684](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-lenet5-mnist.ipynb#L682-L684)）：取测试集第一张图（数字 7）单独前向，保存输出显示 `Probability 7 100.00%`。

#### 4.1.4 代码实践：亲手验证特征图尺寸链

**实践目标**：不训练，只用随机张量走一遍前向，验证 4.1.2 手算的尺寸链与真实模型一致。

**操作步骤**：

1. 打开 `cnn-lenet5-mnist.ipynb`，先运行 Imports 与 MODEL 两个单元格（训练不必跑）。
2. 在新单元格中运行下面的代码（**示例代码**，非仓库原有）：

```python
# 示例代码：逐段打印 LeNet-5 特征图形状（直接在 notebook 单元格中运行，LeNet5 已由 MODEL 单元格定义）
import torch

model = LeNet5(num_classes=10, grayscale=True)
x = torch.randn(8, 1, 32, 32)          # 8 张 32x32 灰度图

# 方法一：分段手动前向
y = model.features(x)
print('features 输出:', y.shape)        # 期望 [8, 16, 5, 5]
print('flatten 后:', torch.flatten(y, 1).shape)  # 期望 [8, 400]
logits, probas = model(x)
print('logits:', logits.shape, ' probas 求和 =', probas.sum(dim=1)[0].item())

# 方法二：forward hook 自动记录每层输出形状
for name, layer in model.features.named_children():
    layer.register_forward_hook(
        lambda m, i, o, name=name: print(f'{name:>2} {m.__class__.__name__:>12} -> {tuple(o.shape)}'))
model.features(x)
```

3. 对照输出与 4.1.2 的表格逐一核对。

**需要观察的现象**：`features` 输出 `[8, 16, 5, 5]`；flatten 后 `[8, 400]`；`probas` 每行求和为 1.0（softmax 的性质）；hook 打印出 `Conv2d → [8,6,28,28]`、`MaxPool2d → [8,6,14,14]`、`Conv2d → [8,16,10,10]`、`MaxPool2d → [8,16,5,5]`。

**预期结果**：手算尺寸链与程序输出完全一致。若把输入换成 `torch.randn(8, 1, 28, 28)`（不 Resize 的 MNIST 原尺寸），flatten 后是 `16*4*4=256 ≠ 400`，第一层 `Linear` 会抛出形状不匹配错误——这就是 4.1.2 强调「精确匹配」的实证。

**待本地验证**：以上为本讲作者推理的预期，未在本地实际运行；hook 语法在旧版 PyTorch 上可能需要微调。

#### 4.1.5 小练习与答案

**练习 1**：第一个 `Conv2d` 没有传 `padding` 和 `stride`，默认值是多少？输出为什么是 28×28 而不是 32×32？

**参考答案**：默认 `padding=0, stride=1, dilation=1`。代入公式 \( o=\lfloor(32-5+0)/1\rfloor+1=28 \)。无填充卷积会「吃掉」核边缘的一半——\( k-1=4 \) 个像素。

**练习 2**：如果把 `forward` 里的 `torch.flatten(x, 1)` 误写成 `torch.flatten(x)`（不带维度参数），会发生什么？

**参考答案**：不带参数的 `flatten` 会把整个张量（含 batch 维）展平成一维 `[N×400]`，送进 `Linear(400, 120)` 时 batch 结构被破坏，通常会直接报形状错误（或即使侥幸不报错，语义也完全错了）。`flatten(x, 1)` 表示「从第 1 维开始展平、保留第 0 维（batch）」。

**练习 3**：如果不想把 MNIST Resize 到 32×32，而想保持 28×28 原尺寸，`LeNet5` 至少要改哪两处？

**参考答案**：尺寸链变为 28→24→12→8→4，flatten 后是 `16*4*4=256`，因此第一处是分类头第一层改成 `nn.Linear(16*4*4, 120)`；第二处是删掉/修改数据变换里的 `transforms.Resize((32, 32))`（同时 `NUM_FEATURES=28*28`）。卷积层本身（核大小、通道数）不用改，因为 5×5 卷积对任意 ≥5 的输入尺寸都合法——这正是 CNN「参数量与输入空间尺寸无关」的体现。

### 4.2 AlexNet 结构：更大、更深、更现代

#### 4.2.1 概念说明

AlexNet（2012）在 ImageNet 大规模视觉识别挑战赛上以巨大优势夺冠，把「深度卷积网络 + 大数据 + GPU」推上主流。与 LeNet-5 相比，它的结构语言有几个新词：

- **ReLU 激活**取代 tanh/sigmoid：正半轴导数恒为 1，深层网络不再被饱和区「卡死」梯度（呼应 u2-l5 对饱和激活的分析）。
- **重叠最大池化**（overlapping max pooling）：池化窗口 3、步长 2，窗口互相重叠，比 LeNet 的 2×2/步 2 提取更细。
- **Dropout**（u2-l4 讲过机制）：放在两个 4096 维全连接层前，抑制大规模分类头的过拟合。
- **训练期数据增强**：随机裁剪代替测试期中心裁剪，扩大有效训练样本多样性。

还有一个此前没见过的层——**自适应平均池化 `nn.AdaptiveAvgPool2d((6, 6))`**：无论输入特征图是多大，都输出固定的 6×6。它的价值在于把「卷积栈输出的空间尺寸」与「分类头要求的输入维数 `256*6*6=9216`」**解耦**：输入图片尺寸变了，分类头一行不用改。原版 AlexNet 输入 224×224，卷积栈自然输出恰好 6×6；本 notebook 输入缩小到 64×64，卷积栈输出会缩到 1×1，靠这个自适应层强行「拉回」6×6（见 4.2.2 的表格与 4.2.3 的说明）。

另一个值得注意的工程变化：从这份 notebook 开始改用仓库共享 helper——数据来自 `get_dataloaders_cifar10`（train/valid/test 三 loader，承接 u1-l3/u4-l1 的认知），训练循环整体外包给 `train_classifier_simple_v1`，notebook 里不再出现手写五步循环。模型结构反而成了 notebook 里唯一的主角。

#### 4.2.2 核心流程

对 64×64 输入（CIFAR-10 先 Resize 到 70×70 再裁剪 64×64），逐层套用尺寸公式：

| 层 | 配置 | 输出尺寸 |
| --- | --- | --- |
| Conv1 | 11×11, s=4, p=2, 3→64 | \(\lfloor(64-11+4)/4\rfloor+1=15\) → 15×15×64 |
| MaxPool1 | 3×3, s=2 | \(\lfloor(15-3)/2\rfloor+1=7\) → 7×7×64 |
| Conv2 | 5×5, s=1, p=2, 64→192 | 保持 → 7×7×192 |
| MaxPool2 | 3×3, s=2 | \(\lfloor(7-3)/2\rfloor+1=3\) → 3×3×192 |
| Conv3 | 3×3, p=1, 192→384 | 保持 → 3×3×384 |
| Conv4 | 3×3, p=1, 384→256 | 保持 → 3×3×256 |
| Conv5 | 3×3, p=1, 256→256 | 保持 → 3×3×256 |
| MaxPool3 | 3×3, s=2 | \(\lfloor(3-3)/2\rfloor+1=1\) → **1×1×256** |
| AdaptiveAvgPool2d((6,6)) | 自适应 | **6×6×256** |
| flatten | — | 9216 = 256×6×6 |
| Linear(9216→4096)+Dropout | — | 4096 |
| Linear(4096→4096)+Dropout | — | 4096 |
| Linear(4096→10) | — | 10 logits |

对照原版 224×224 输入：Conv1 输出 55×55，三级池化后（27→13→6）卷积栈**恰好**自然输出 6×6×256——`256*6*6=9216` 这个魔数就是这么来的。64×64 输入下卷积栈缩到 1×1，`AdaptiveAvgPool2d((6,6))` 在唯一的一个空间位置上取平均、复制到 6×6 网格，维度于是被统一成 9216。这也解释了 settings 里那句注释「Deterministic behavior not yet supported by AdaptiveAvgPool2d」——作者因此注释掉了 `set_deterministic()`（[pytorch_ipynb/cnn/cnn-alexnet-cifar10.ipynb:L258-L265](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-alexnet-cifar10.ipynb#L258-L265) 附近）。

#### 4.2.3 源码精读

**（1）数据管线：随机裁剪 vs 中心裁剪。**
[pytorch_ipynb/cnn/cnn-alexnet-cifar10.ipynb:L326-L341](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-alexnet-cifar10.ipynb#L326-L341) 定义了两套 transform：训练集 `Resize((70,70)) + RandomCrop((64,64))`（每个 epoch 裁在不同位置，即最朴素的**数据增强**），测试/验证集 `Resize((70,70)) + CenterCrop((64,64))`（确定性裁剪，保证评估可复现）。随后 [L336-L341](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-alexnet-cifar10.ipynb#L336-L341) 调用共享的 `get_dataloaders_cifar10(..., validation_fraction=0.1)` 拿到三个 loader。增强只作用于训练集——评估数据必须稳定，这是 u4-l3 会展开的原则。

**（2）模型定义。**
[pytorch_ipynb/cnn/cnn-alexnet-cifar10.ipynb:L414-L454](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-alexnet-cifar10.ipynb#L414-L454) 定义 `AlexNet`，结构与 LeNet-5 同款骨架但内容升级：

- `self.features`（[L418-L436](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-alexnet-cifar10.ipynb#L418-L436)）：5 个卷积层。Conv1 是 11×11 大核、stride 4（[L419](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-alexnet-cifar10.ipynb#L419)），快速压缩空间；Conv2 是 5×5（[L423](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-alexnet-cifar10.ipynb#L423)）；Conv3–Conv5 都是 3×3、`padding=1` 保持尺寸（[L427](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-alexnet-cifar10.ipynb#L427)、[L430](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-alexnet-cifar10.ipynb#L430)、[L433](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-alexnet-cifar10.ipynb#L433)）——「核越小越深」的趋势在这里已经萌芽（下一讲 VGG 把它推到极致）。激活全部是 `nn.ReLU(inplace=True)`。
- `self.avgpool = nn.AdaptiveAvgPool2d((6, 6))`（[L437](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-alexnet-cifar10.ipynb#L437)）：尺寸解耦的关键一行（见 4.2.1/4.2.2 的分析）。
- `self.classifier`（[L438-L446](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-alexnet-cifar10.ipynb#L438-L446)）：`Dropout(0.5) → Linear(9216, 4096) → ReLU → Dropout(0.5) → Linear(4096, 4096) → ReLU → Linear(4096, 10)`。两个 4096 维全连接层贡献了全模型绝大多数参数（约 3775 万 + 1678 万），Dropout 在它们之前各拦一道。
- `forward`（[L448-L454](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-alexnet-cifar10.ipynb#L448-L454)）：`features → avgpool → x.view(x.size(0), 256*6*6)` 展平（与 `torch.flatten(x, 1)` 等价的老写法）`→ classifier`。**阅读细节**：L453 算了 `probas = F.softmax(logits, dim=1)` 但 L454 只 `return logits`——probas 是一行「死代码」，训练与评估都只用 logits（`cross_entropy` 内部做 softmax）。对照 LeNet-5 返回 `(logits, probas)` 的元组，两种写法并存说明：仓库代码也有不统一之处，读代码时要以实际 `return` 为准。

**（3）训练：外包给 helper。**
[L807-L809](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-alexnet-cifar10.ipynb#L807-L809) 一行调用 `train_classifier_simple_v1(num_epochs=40, model=model, optimizer=optimizer, device=DEVICE, train_loader=..., valid_loader=..., logging_interval=50)`，超参为 `LEARNING_RATE=0.0001`、`BATCH_SIZE=256`（settings 单元格 [L244-L266](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-alexnet-cifar10.ipynb#L244-L266)）。训练循环的五步范式没有消失，只是搬进了 [pytorch_ipynb/helper_train.py:L15](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_train.py#L15)（u1-l3 精读过）。损失与准确率曲线随后用 `log_dict` 里的 `train_loss_per_batch`、`train_acc_per_epoch`、`valid_acc_per_epoch` 画出来（[L817-L838](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-alexnet-cifar10.ipynb#L817-L838)）。

**（4）评估单元格里的复制粘贴瑕疵（批判性阅读点）。**
[L906-L923](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-alexnet-cifar10.ipynb#L906-L923) 里，`train_acc` 竟然是在 `test_loader` 上算的（[L908-L910](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-alexnet-cifar10.ipynb#L908-L910)），而且 L921 打印「Train ACC」用的是 `valid_acc` 变量。这是作者的复制粘贴笔误：**打印出的 Test ACC（72.11%）本身没问题，但 Train/Validation 两行不可信**。教训：开源教学代码也会有小 bug，数字要追到「哪个变量在哪个 loader 上算的」。

**（5）结果。**
保存的输出为 Test ACC **72.11%**（[L901](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-alexnet-cifar10.ipynb#L901)）。对比 LeNet-5 在 CIFAR-10 上的 67.30%：更深更宽的 AlexNet 带来约 5 个百分点的提升，但 CIFAR-10 的难度（32×32 彩色、1000 倍类别内差异）使这个数字远低于 MNIST 上的 98.93%——**数据难度决定准确率的天花板感受**。

#### 4.2.4 代码实践：追踪 AlexNet 的尺寸链与 AdaptiveAvgPool2d

**实践目标**：用 forward hook 打印 `AlexNet` 每层输出形状，验证 4.2.2 的手算表格，并亲眼观察 `AdaptiveAvgPool2d` 如何把 1×1 拉回 6×6。

**操作步骤**：

1. 打开 `cnn-alexnet-cifar10.ipynb`，运行 Imports、settings、helper 导入与 MODEL 单元格（数据下载与训练可跳过）。
2. 在新单元格运行（**示例代码**，非仓库原有）：

```python
# 示例代码：打印 AlexNet 每层输出形状
model = AlexNet(num_classes=10)
model.eval()
x = torch.randn(2, 3, 64, 64)     # 模拟 64x64 彩色输入

feats = []
def hook(module, inp, out):
    feats.append((module.__class__.__name__, tuple(out.shape)))

for layer in model.features:
    layer.register_forward_hook(hook)
model.avgpool.register_forward_hook(hook)

with torch.no_grad():
    logits = model(x)

for i, (name, shape) in enumerate(feats, 1):
    print(f'{i:2d} {name:20s} -> {shape}')
print('logits ->', tuple(logits.shape))
```

3. 把输入换成 `torch.randn(2, 3, 224, 224)` 再跑一次，观察卷积栈输出与 `avgpool` 后尺寸的变化。

**需要观察的现象**：64×64 输入下，`MaxPool2d` 第三次输出 `(2, 256, 1, 1)`，紧接着 `AdaptiveAvgPool2d` 输出 `(2, 256, 6, 6)`；224×224 输入下卷积栈直接自然输出 `(2, 256, 6, 6)`，`AdaptiveAvgPool2d` 变成恒等式的角色；两种输入最终 logits 都是 `(2, 10)`。

**预期结果**：与 4.2.2 表格一致——`AdaptiveAvgPool2d((6,6))` 使分类头输入恒为 9216 维，与输入图片尺寸完全解耦。

**待本地验证**：以上为推理预期，未实际运行；模型约 5700 万参数，CPU 上做一次前向没有问题，但请勿误触训练单元格。

#### 4.2.5 小练习与答案

**练习 1**：如果删掉 `self.avgpool`，直接把 64×64 输入下的卷积栈输出展平送进 `classifier`，会发生什么？

**参考答案**：卷积栈输出 1×1×256，展平只有 256 维，而 `Linear(256*6*6, 4096)` 期望 9216 维，前向时抛出矩阵形状不匹配错误。若输入恰是 224×224 则不会报错——`AdaptiveAvgPool2d` 的意义正是让模型对**任意**输入尺寸都合法且分类头固定。

**练习 2**：`classifier` 里的 `Dropout(0.5)` 在训练和评估时行为有何不同？谁在切换这个行为？

**参考答案**：训练时每个 batch 随机置零一半单元并将存活单元放大 2 倍（inverted dropout，u2-l4）；评估时恒等通过。切换由 `model.train()`/`model.eval()` 控制（`Sequential` 会递归传播 `training` 标志），`torch.no_grad()` 只管梯度、不管层行为——两个开关各管一摊。

**练习 3**：列出 AlexNet 相对 LeNet-5 的至少 3 项「现代化升级」，并各用一句话说明动机。

**参考答案**：(1) ReLU 替代 tanh——避免饱和区梯度消失，深层可训；(2) 重叠最大池化（3×3/s2）——比 2×2/s2 保留更多空间细节、引入平移不变性同时降低信息损失；(3) Dropout——抑制 4096 维大分类头过拟合；(4) 训练期随机裁剪增强——扩充有效样本、提升泛化；(5) 大核 stride-4 首层——在网络前端快速降低空间分辨率以省算力。

### 4.3 分组卷积：AlexNet 的双 GPU 遗产

#### 4.3.1 概念说明

2012 年 AlexNet 训练时用的是两块 GPU（notebook 的 Markdown 转述为「each with 1.5 Gb RAM」），显存装不下整个模型，于是作者把网络从通道维**劈成两半**，一半放 GPU 1、一半放 GPU 2，只在少数层交互信息。这就是分组卷积的出身——纯粹是当年显存所迫的工程妥协。

分组卷积的规则：设组数 \( g \)，输入通道 \( C_{in} \) 与输出通道 \( C_{out} \) 各被切成 \( g \) 份，第 \( i \) 组的卷积核**只看**输入的第 \( i \) 段通道、**只产生**输出的第 \( i \) 段通道。组间在这一次卷积内**零通信**（信息交换要等后续把它们重新混合的层）。

副作用反而成了优点：

1. **参数量与计算量降为 \( \frac{1}{g} \)**。普通卷积参数为 \( k_h k_w C_{in} C_{out} + C_{out} \)；分组后为 \( k_h k_w \frac{C_{in}}{g} \frac{C_{out}}{g} g + C_{out} = \frac{1}{g} k_h k_w C_{in} C_{out} + C_{out} \)。
2. **结构上类似并列的小网络**，有时反而略提升精度（notebook 引用 Yani Ioannou 的博客说明这一点）。
3. 组内通道数更少，卷积核被迫在更窄的子空间里学特征，起到一定正则化效果。

后来这一思想被 ResNeXt、MobileNet 的深度可分离卷积（\( g=C_{out} \) 的极端情形，u3-l5 会讲）发扬光大。

#### 4.3.2 核心流程

以 notebook Markdown 里的算例入门（输入 6 通道、输出 12 通道、3×3 核）：

- 普通卷积：每个核覆盖全部 \( 3 \times 3 \times 6 \) 个权重，共 12 个核，参数 \( = 3 \times 3 \times 6 \times 12 = 648 \)。
- \( g=2 \) 分组：每组输入 3 通道、输出 6 通道，每组参数 \( 3 \times 3 \times 3 \times 6 \)，两组共 \( (3 \times 3 \times 3 \times 6) \times 2 = 324 \)——**恰好减半**，与公式 \( \frac{1}{g} \) 一致。

对 `AlexNet` 的 4 个分组卷积层（Conv2–Conv5）套用公式，卷积参数从约 247 万降到约 125 万（见 4.3.4 实践）；但由于两个 4096 维全连接层（约 5457 万参数）不参与分组，**全模型**参数只从约 5704 万降到约 5582 万，降幅约 2%——「卷积减半」不等于「全模型减半」。

约束：\( g \) 必须同时整除 \( C_{in} \) 与 \( C_{out} \)。所以第一个卷积层（3→64）不能用 `groups=2`（3 不能被 2 整除），仓库实现里它保持普通卷积。

#### 4.3.3 源码精读

**（1）notebook 开篇的分组卷积讲解。**
[pytorch_ipynb/cnn/cnn-alexnet-cifar10-grouped.ipynb:L88-L111](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-alexnet-cifar10-grouped.ipynb#L88-L111) 的 Markdown 是仓库里少见的「教学正文」：L88–L92 讲「每组独立处理一半通道再合并」的直觉；L94 提到双 GPU 历史与 Yani Ioannou 观察到的精度收益；L98–L107 就是 4.3.2 的 648 vs 324 算例；L109 给出 PyTorch 用法 `torch.nn.Conv2d(..., groups=2)`；L111 明确写出整除约束。这段 Markdown 值得完整读一遍。

**（2）模型代码：只差一个 `groups=2` 参数。**
与 4.2 的普通版逐行 diff，`class AlexNet` 唯一的变化是 Conv2–Conv5 四行各多了 `groups=2`：

- [L432](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-alexnet-cifar10-grouped.ipynb#L432)：`nn.Conv2d(64, 192, kernel_size=5, padding=2, groups=2)`——组 1 用前 32 个输入通道产出前 96 个输出通道，组 2 用后 32 个输入通道产出后 96 个输出通道。
- [L436](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-alexnet-cifar10-grouped.ipynb#L436)、[L439](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-alexnet-cifar10-grouped.ipynb#L439)、[L442](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-alexnet-cifar10-grouped.ipynb#L442)：Conv3（192→384）、Conv4（384→256）、Conv5（256→256）同样 `groups=2`。
- [L428](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-alexnet-cifar10-grouped.ipynb#L428)：Conv1 仍是 `nn.Conv2d(3, 64, kernel_size=11, stride=4, padding=2)`，无 `groups`——3 个输入通道无法分成 2 组。

其余部分（`avgpool`、`classifier`、`forward`、数据管线、超参、训练调用）与普通版完全相同。**「克隆基线、只改一处」**——这正是 u2-l4 建立的受控对比方法论在架构维度的应用。

**（3）结果与诚实的解读。**
保存输出为 Test ACC **70.93%**（[L910](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-alexnet-cifar10-grouped.ipynb#L910)），低于普通版的 72.11%。虽然 Markdown 引用的资料称分组卷积「slightly improved accuracy」，但在这两次保存的 CIFAR-10 运行里分组版并未占优。合理解读：该收益来自 ImageNet 规模的实验，40 个 epoch 的 CIFAR-10 单次运行间的随机波动（±1 个百分点量级）足以淹没「略提升」。**教训**：架构对比要么多随机种子重复、要么报告均值±方差，单次运行的胜负不可轻信。

#### 4.3.4 代码实践：量化分组卷积的参数收益

**实践目标**：统计普通版与分组版 AlexNet 的参数量，验证「卷积参数近似减半、全模型仅小幅下降」的推断；再用一次故意失败的实验看清 `groups` 的整除约束。

**操作步骤**：

1. 分别打开两份 AlexNet notebook 的 MODEL 单元格，各运行模型定义。
2. 在新单元格运行（**示例代码**，非仓库原有）：

```python
# 示例代码：对比普通版与分组版 AlexNet 的参数量
def count_params(m):
    return sum(p.numel() for p in m.parameters())

model_plain = AlexNet(num_classes=10)    # 普通版 notebook 中定义
print(f'全模型参数: {count_params(model_plain):,}')
print(f'features 参数: {count_params(model_plain.features):,}')
print(f'classifier 参数: {count_params(model_plain.classifier):,}')

# 分组版 notebook 中定义的类如果也叫 AlexNet，需先 del 再重新定义，或用两个 kernel
# 观察重点：分组版 features 参数应约为普通版的一半，classifier 不变
```

3. 在普通版 notebook 的新单元格故意构造一个非法分组（**示例代码**）：

```python
# 示例代码：观察 groups 的整除约束
import torch.nn as nn
nn.Conv2d(3, 64, kernel_size=11, stride=4, padding=2, groups=2)  # 3 % 2 != 0
```

**需要观察的现象**：普通版全模型参数约 57,044,810（其中 features 约 247 万、classifier 约 5457 万）；分组版 features 参数约 125 万（近半），classifier 不变，全模型约 5582 万；第 3 步抛出 `ValueError`，报错信息说明 in_channels 必须能被 groups 整除。

**预期结果**：与 4.3.2 的公式推断一致；`ValueError` 现象可复现。

**待本地验证**：具体参数总数以上机输出为准，手算为 57,044,810（普通）与 55,821,674（分组，含全部偏置）；若与你统计的略有出入，检查是否把 `avgpool`（无参数）计入。

#### 4.3.5 小练习与答案

**练习 1**：`nn.Conv2d(64, 192, kernel_size=5, padding=2, groups=2)` 中，输出第 150 个通道是由哪些输入通道卷积得到的？

**参考答案**：输出 192 通道每组负责 96 个：第 150 个通道落在第 2 组（后 96 个，即第 97–192 个输出通道），由输入的第 33–64 通道（后一半）卷积得到。它完全「看不到」输入的前 32 个通道。

**练习 2**：把 `groups` 从 2 改成 4（假设通道都能整除），卷积核权重参数如何变化？组间通信呢？

**参考答案**：权重参数从 \( \frac{1}{2} \) 进一步降到 \( \frac{1}{4} \)（偏置不变，仍为 \( C_{out} \)）；每次卷积内组间通信进一步减少——4 个组在该层内互不相连，信息融合更依赖后续层。极端情形 \( g=C_{in}=C_{out} \) 即深度可分离卷积（u3-l5 的 MobileNet）。

**练习 3**：既然分组版在保存的运行中准确率略低，作者为什么仍保留这个 notebook？

**参考答案**：(1) 教学价值——展示如何用最小改动复现论文的历史性设计；(2) 参数/显存收益真实存在（卷积参数近半），在大模型或大分辨率场景下有意义；(3) 文献报告的精度收益在 ImageNet 规模才显现，单次 CIFAR-10 运行不足为凭——notebook 保留了对照实验本身，把判断留给读者。

## 5. 综合实践：把 LeNet-5 的 tanh 换成 ReLU

这是本讲的规格化实践任务，把三个模块的知识串起来：改模型（4.1 的 Sequential 结构）、看收敛（训练循环日志）、解释原因（u2-l5 的饱和梯度分析 + u3-l1 的 He 初始化）。

**实践目标**：在同一份数据与超参下，对比 LeNet-5 用 `tanh` 与 `ReLU` 激活时**收敛速度**（每个 epoch 的 Cost 下降）与**最终测试准确率**，并用梯度视角解释差异。

**操作步骤**：

1. 复制 `pytorch_ipynb/cnn/cnn-lenet5-mnist.ipynb` 为一份可自由修改的副本（不要改动仓库原文件），完整跑通原版（基线：保存输出 98.93%）。注意把 `DEVICE` 改为你环境可用的设备。
2. 在 MODEL 单元格中，把 `LeNet5` 类复制一份并改名 `LeNet5ReLU`，把两处（features 内）与两处（classifier 内）共 **4 个 `nn.Tanh()` 全部换成 `nn.ReLU()`**，其余一行不动。修改示意（**示例代码**）：

```python
# 示例代码：仅展示需要修改的行，其余照抄原 LeNet5
self.features = nn.Sequential(
    nn.Conv2d(in_channels, 6, kernel_size=5),
    nn.ReLU(),                 # 原 nn.Tanh()
    nn.MaxPool2d(kernel_size=2),
    nn.Conv2d(6, 16, kernel_size=5),
    nn.ReLU(),                 # 原 nn.Tanh()
    nn.MaxPool2d(kernel_size=2)
)
```

3. 用 `model = LeNet5ReLU(NUM_CLASSES, GRAYSCALE)` 替换实例化行，`RANDOM_SEED=1`、`LEARNING_RATE=0.001`、`BATCH_SIZE=128`、`NUM_EPOCHS=10` 全部保持不变，重新训练。
4. 每个 epoch 结束时记录两版打印的 `Cost`（每 50 个 batch 打印一次）与 `Train: xx.xxx%` 训练准确率，最后记录 `Test accuracy`。
5. （可选进阶）在 ReLU 版实例化后追加 He 初始化（承接 u3-l1）：对每个卷积层调用 `nn.init.kaiming_normal_(m.weight, mode='fan_in', nonlinearity='relu')`，再训练一次，观察是否进一步加快首个 epoch 的收敛。

**需要观察的现象**：

- 前 1–2 个 epoch，ReLU 版的 Cost 下降明显更快、训练准确率爬升更早（收敛速度的差异主要出现在这里）。
- 10 个 epoch 后两版测试准确率大概率相差不大（MNIST 上 98%+ 的高位区间，差距在 0.2 个百分点量级，可能被随机性淹没）。
- 若做第 5 步：He 初始化 + ReLU 的组合，首个 epoch 的损失应低于不换初始化的 ReLU 版（待本地验证）。

**预期结果与解释**：

- **为什么 ReLU 收敛更快**：tanh 在 |z| 较大时进入饱和区，导数趋于 0；反向传播时每过一个 tanh 层梯度就乘一次小于 1 的因子，靠近输入端的层梯度极小（u2-l5 精读过 sigmoid 的同类现象，tanh 仅是以 0 为中心、饱和性相同）。ReLU 正半轴导数恒为 1，梯度可以几乎不衰减地穿过激活层直达浅层，配合 Adam 的一阶/二阶动量，前期优化显著提速。
- **为什么最终准确率未必大涨**：LeNet-5 只有 5 层、MNIST 又接近线性可分，饱和梯度问题在浅网络上不致命；tanh 有界、以 0 为中心的输出反而对浅层全连接部分是温和的「天然缩放」。深度网络的收益（ReLU 的主战场）要在几十层的网络里才真正显现。
- **结果表格建议**：列出「epoch 序号 → tanh 版 Cost / acc → ReLU 版 Cost / acc」与最终 Test accuracy，两张对比即可支撑结论。

**待本地验证**：以上现象为基于原理与文献的预期推断，具体数字需以你本地两次（或多次换种子）运行的结果为准。

## 6. 本讲小结

- LeNet-5 用 `nn.Sequential` 堆叠式定义了「卷积-激活-池化 ×2 + 展平 + 全连接 ×3」的最小完整 CNN 骨架；尺寸链 32→28→14→10→5、通道 1→6→16、flatten 后 400 必须与分类头精确匹配；MNIST 上保存的测试准确率 98.93%。
- 同一个 `LeNet5` 类迁移到 CIFAR-10 时只需把 `grayscale` 开到 3 通道并把所有通道数按 `in_channels` 等比放大（6→18、16→48），体现「输入通道变化 → 结构等比适配」的思路；CIFAR-10 上保存的准确率 67.30%，数据难度决定了准确率量级。
- AlexNet 带来 ReLU、重叠最大池化、Dropout 与随机裁剪增强四大现代化元素；`AdaptiveAvgPool2d((6,6))` 把卷积栈输出尺寸与分类头输入维数（9216）解耦；训练循环外包给共享 helper `train_classifier_simple_v1` 是仓库新版 notebook 的组织风格。
- 分组卷积（`groups=2`）把输入/输出通道各切两半、组内独立卷积，卷积权重参数降为 \( \frac{1}{g} \)；它源自 AlexNet 当年的双 GPU 显存限制，约束是 `groups` 必须同时整除输入与输出通道数（所以 3→64 的 Conv1 不能分组）。
- 批判性阅读三例：LeNet-5 的 Markdown 声称用 ReLU 但代码是 `nn.Tanh()`；AlexNet 的 `forward` 算了 `probas` 却只返回 `logits`；评估单元格把 `train_acc` 错算在 `test_loader` 上——**以代码与变量来源为准，注释与打印标签都可能骗人**。
- 单次运行的架构对比（72.11% vs 70.93%）不足为凭，随机波动可以淹没「略提升」；可信结论需要多随机种子或更长训练。

## 7. 下一步学习建议

本讲之后，你已经能读懂任何「Sequential 堆叠式」的 CNN。下一讲 [u3-l3](u3-l3-vgg-nin-allconv.md) 沿架构史前进：**VGG** 把 AlexNet「核越小越深」的趋势推到极致（全部 3×3 核、16–19 层），**NiN** 用 1×1 卷积替代全连接分类头，**All-Conv** 干脆全卷积化。建议：

1. 先读 [pytorch_ipynb/cnn/cnn-vgg16.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg16.ipynb)，检验自己能否不查资料手算每个 stage 的尺寸链。
2. 带着本讲的两个问题去看 VGG：为什么两个 3×3 卷积可以「顶替」一个 5×5（感受野相同、参数更少、非线性更多）？`VGG16` 模块化「块（block）」的定义方式与本讲一层一层的 `Sequential` 有何组织差异？
3. 若想巩固本讲的分组卷积，可提前翻阅 u3-l5 将精读的 [pytorch_ipynb/cnn/cnn-mobilenet-v2-cifar10.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-mobilenet-v2-cifar10.ipynb)，寻找 \( g=C_{out} \) 的深度可分离卷积——分组卷积的极端形式。
