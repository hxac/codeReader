# 第一个卷积神经网络

> 本讲对应的源码：`pytorch_ipynb/cnn/cnn-basic.ipynb` 与 `pytorch_ipynb/cnn/cnn-he-init.ipynb`。
> 提示：notebook 是 JSON 文件，本文所有永久链接的行号均指向 `.ipynb` 的**原始 JSON 文本行**（在 GitHub 上点击锚点即可定位到对应行）。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚卷积层（`Conv2d`）和最大池化层（`MaxPool2d`）各自做什么，以及它们与上一讲 MLP 中全连接层（`Linear`）的本质区别。
2. **独立推导**卷积层与池化层的输出尺寸：给定输入尺寸 \(w\)、卷积核 \(k\)、步长 \(s\)、填充 \(p\)，算出输出尺寸 \(o\)，并能沿整条前向链路推出每一层特征图的形状。
3. 解释特征图「通道数」如何随层演化（1 → 8 → 16），以及为什么最后要 `view(-1, 7*7*16)` 展平。
4. 理解 He 初始化（`nn.init.kaiming_normal_`）的公式、适用场景（ReLU 激活），并能从两份 notebook 保存的训练日志中看出「初始化不同 → 第一个 epoch 收敛速度天差地别」这一现象。

本讲是 U3「卷积神经网络家族」的第一讲，后续 LeNet-5、AlexNet、VGG、ResNet 等讲义全都建立在本讲的尺寸公式和层定义方式之上。

## 2. 前置知识

在学习本讲前，你应当已经完成 u2-l3（多层感知机与 `nn.Module`），记得以下事实：

- **自定义模型的标准写法**：继承 `torch.nn.Module`，在 `__init__` 里创建层（自动登记参数），在 `forward` 里描述前向计算，外部用 `model(x)` 调用。
- **五步训练循环**：forward → loss → `zero_grad` → `backward` → `step`。本讲的训练代码与上一讲**几乎逐字相同**，变的只是模型本身——这正是本手册反复强调的「范式复用」。
- **手工改权重须先 `detach()`**：u2-l3 中我们已经见过「detach 再原地操作」的写法，本讲的初始化代码会再次用到它。

在此基础上，本讲需要三个新直觉：

1. **图像的形状是 4 维张量**。上一讲 MLP 把 28×28 的 MNIST 图片直接压成 784 维向量丢进 `Linear`；本讲保留图片的空间结构，一个 batch 的形状是 `[128, 1, 28, 28]`，即 PyTorch 约定的 **NCHW**：N（batch 内样本数）、C（通道数，灰度图为 1）、H（高）、W（宽）。notebook 保存的输出可以在 [pytorch_ipynb/cnn/cnn-basic.ipynb:L91](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-basic.ipynb#L91) 看到。
2. **全连接层的两个痛点**：参数爆炸（784×128 的一个矩阵就要 10 万参数，且每个输入像素与每个输出神经元独立相连）和**平移不敏感的缺失**（数字向右挪两个像素，784 维向量面目全非，MLP 需要重新学习）。
3. **卷积的答案**：每个卷积核只在图像的一个小窗口（如 3×3）内看数据（局部连接），且同一组核权重在整张图上滑动复用（权值共享）。于是「检测一个竖线笔画」只需要一套 3×3×1 的权重，无论它出现在图片哪个角落。

术语说明：**特征图（feature map）** 是一个卷积核扫过整张输入后得到的二维响应图；一层有 8 个核就有 8 张特征图，张量通道数就是 8。**感受野（receptive field）** 指特征图上一个位置「看到」的原始图像区域大小，层数越深、池化越多，感受野越大。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [pytorch_ipynb/cnn/cnn-basic.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-basic.ipynb) | 主线 notebook：MNIST 上的两层卷积网络 | `Conv2d`/`MaxPool2d` 定义、形状注释、前向传播、（仅为演示的）高斯初始化 |
| [pytorch_ipynb/cnn/cnn-he-init.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-he-init.ipynb) | 姊妹篇：结构与 cnn-basic 同构、通道数减半，改用 He 初始化 | `nn.init.kaiming_normal_` 用法、两种初始化的训练日志对比 |

两份 notebook 的其余部分（数据加载、训练循环、评估）几乎完全一致，都是 u2 系列讲义已拆解过的标准范式；本讲把火力集中在 `## Model` 单元格和它对训练动态的影响上。

## 4. 核心概念与源码讲解

### 4.1 卷积层与池化层

#### 4.1.1 概念说明

这个模块回答的问题是：**如何用「层」的语言表达上一节说的局部连接与权值共享？**

- **`torch.nn.Conv2d(in_channels, out_channels, kernel_size, stride, padding)`**：`in_channels` 是输入特征图的通道数（第一层吃原始图像，灰度图就是 1）；`out_channels` 是这一层卷积核的个数，也是输出特征图的通道数；`kernel_size=(3,3)` 是每个核在空间上的窗口大小；`stride` 是核每次滑动的步数；`padding` 是在输入四周补几圈 0（为了让输出尺寸可控）。
- **`torch.nn.MaxPool2d(kernel_size, stride, padding)`**：不带参数的降采样层。在 2×2 窗口里取最大值，典型用法是 `kernel_size=(2,2), stride=(2,2)`，把空间尺寸减半、通道数不变。它的作用是：压缩空间分辨率（后续层参数更少）、让小幅平移不改变输出（平移不变性），并扩大感受野。
- **`F.relu`**：逐元素的非线性 \(\max(0, x)\)，与 u2-l3 中 MLP 的激活函数角色相同——没有它，卷积堆再多层也等价于一层线性运算。

与 `Linear` 的关键对比：

| 维度 | `Linear`（u2-l3 MLP） | `Conv2d`（本讲） |
| --- | --- | --- |
| 连接方式 | 每个输入与每个输出全连接 | 每个输出位置只连输入的一个局部窗口 |
| 参数量 | `out × in`（如 784×128） | `out_channels × in_channels × k_h × k_w`，与图像大小无关 |
| 权重复用 | 无（每个连接独立权重） | 同一核在整张图上滑动复用 |
| 空间结构 | 输入需展平成一维 | 保留 H×W 平面结构 |

#### 4.1.2 核心流程

前向传播的数据流（以 cnn-basic、batch=128 为例）：

```
输入图像 [128, 1, 28, 28]
   │ conv_1: Conv2d(1→8, k=3, s=1, p=1)     # 8 个核分别扫描灰度图
   ▼
[128, 8, 28, 28]  ── F.relu ──▶  [128, 8, 28, 28]
   │ pool_1: MaxPool2d(k=2, s=2)            # 空间减半
   ▼
[128, 8, 14, 14]
   │ conv_2: Conv2d(8→16, k=3, s=1, p=1)    # 16 个核，每个同时看 8 个输入通道
   ▼
[128, 16, 14, 14] ── F.relu ──▶ [128, 16, 14, 14]
   │ pool_2: MaxPool2d(k=2, s=2)
   ▼
[128, 16, 7, 7]
   │ view(-1, 7*7*16) 展平                   # 从 NCHW 回到二维矩阵
   ▼
[128, 784]
   │ linear_1: Linear(784→10)
   ▼
logits [128, 10] ── softmax ──▶ probas [128, 10]
```

两个值得注意的细节：

1. **`conv_2` 的输入是 8 通道**。第二层的每个 3×3 核实际上是一个 8×3×3 的三维核——它在「8 张特征图叠成的立方体」上做卷积，把 8 个通道的信息加权合并成 1 张新的特征图。通道数由上一层的 `out_channels` 决定。
2. **卷积/池化只负责特征提取，分类仍靠 `Linear`**。网络末端必须把 `[N, C, H, W]` 展平成 `[N, C×H×W]` 才能进全连接层，这正是 forward 里 `out.view(-1, 7*7*16)` 存在的原因。

#### 4.1.3 源码精读

层的定义全部集中在 `ConvNet.__init__`，作者在每一层上方都写了「输入形状 => 输出形状」的注释，是极佳的阅读材料：

[pytorch_ipynb/cnn/cnn-basic.ipynb:L168-L182](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-basic.ipynb#L168-L182) —— 这段先写下同名填充（same padding）的推导公式，然后定义 `conv_1`：`Conv2d(in_channels=1, out_channels=8, kernel_size=(3,3), stride=(1,1), padding=1)`，注释标明形状从 28x28x1 变为 28x28x8；紧接着 `pool_1` 用 `MaxPool2d(kernel_size=(2,2), stride=(2,2), padding=0)` 把空间尺寸减半到 14x14x8。行尾的 `# (1(28-1) - 28 + 3) / 2 = 1` 是把数值代入填充公式验算的过程（见 4.2 节）。

[pytorch_ipynb/cnn/cnn-basic.ipynb:L183-L193](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-basic.ipynb#L183-L193) —— 定义 `conv_2`：`Conv2d(in_channels=8, out_channels=16, ...)`，特征图通道翻倍到 16；再用 `pool_2` 空间减半到 7x7x16；最后 `self.linear_1 = torch.nn.Linear(7*7*16, num_classes)` 把展平后的 784 维特征映射到 10 个类别 logits。**7\*7\*16 = 784 与输入图像像素数相同纯属巧合**，它的来源是两次 2×2 池化（28→14→7）与第二层 16 个通道。

forward 方法把上面的层按「卷积 → ReLU → 池化」的节奏串起来：

[pytorch_ipynb/cnn/cnn-basic.ipynb:L205-L216](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-basic.ipynb#L205-L216) —— `forward` 中对每个卷积块依次执行 `conv → F.relu → pool`；末尾 `logits = self.linear_1(out.view(-1, 7*7*16))` 把 `[N,16,7,7]` 展平成 `[N,784]` 再分类，同时返回 `logits`（给 `F.cross_entropy` 用）和 `probas`（softmax 概率，给预测用）。这种「一份数学、两个返回值」的写法从 u2 系列沿用至今。

对比一下 cnn-he-init 的同一段（[pytorch_ipynb/cnn/cnn-he-init.ipynb:L174-L194](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-he-init.ipynb#L174-L194)）：结构完全同构，只是通道减半（1→4→8），因此 `linear_1` 是 `Linear(7*7*8, num_classes)`（392 维输入）。两份 notebook 恰好构成「只改一处」的受控对比——这正是 u2-l4 总结过的实验方法论。

#### 4.1.4 代码实践

**实践目标**：亲手摸到卷积核的形状，验证「参数量与图像大小无关、只与核尺寸和通道数有关」。

**操作步骤**（示例代码，可在任意能 import torch 的环境运行）：

```python
# 示例代码：观察 Conv2d / MaxPool2d 的参数形状
import torch

conv_1 = torch.nn.Conv2d(in_channels=1,  out_channels=8,  kernel_size=(3, 3), stride=(1, 1), padding=1)
conv_2 = torch.nn.Conv2d(in_channels=8,  out_channels=16, kernel_size=(3, 3), stride=(1, 1), padding=1)
pool_1 = torch.nn.MaxPool2d(kernel_size=(2, 2), stride=(2, 2), padding=0)

print(conv_1.weight.shape)   # 卷积核张量
print(conv_1.bias.shape)     # 每个核一个偏置
print(conv_2.weight.shape)
print(pool_1.weight if hasattr(pool_1, 'weight') else 'MaxPool2d 没有可学习参数')
```

**需要观察的现象**：`conv_1.weight.shape` 应为 `torch.Size([8, 1, 3, 3])`（8 个核，每个 1×3×3）；`conv_2.weight.shape` 应为 `torch.Size([16, 8, 3, 3])`（16 个核，每个核跨越 8 个输入通道）；`MaxPool2d` 没有 `weight` 属性。

**预期结果**：

- `conv_1` 参数 = 8×1×3×3 + 8 = **80**
- `conv_2` 参数 = 16×8×3×3 + 16 = **1168**
- 池化层参数 = **0**

如果把输入换成 1000×1000 的图像（需相应调整 padding 以保持尺寸），`conv_1.weight.shape` 不变——参数量与图像大小解耦，这是卷积相对全连接的核心优势。以上形状推导是纯张量运算，可放心验证；运行输出待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`Conv2d(in_channels=3, out_channels=32, kernel_size=(5,5))` 一共有多少参数（含偏置）？

**答案**：权重 32×3×5×5 = 2400，偏置 32，共 **2432** 个参数——注意它与输入图像的宽高完全无关。

**练习 2**：为什么 `pool_1` 用 `MaxPool2d` 而不是再做一次 `Conv2d` 来缩小尺寸？

**答案**：池化无参数、计算便宜，且「取窗口最大值」保留了最强的激活响应，带来对小幅平移的不变性；用步长为 2 的卷积也能缩小尺寸（且可学习，正是后续讲义中 stride 卷积的思路），但参数与计算都更贵。本讲网络用「卷积保精度 + 池化降分辨率」的经典组合。

**练习 3**：`conv_2` 的 `in_channels` 为什么必须等于 8？如果写成 4 会发生什么？

**答案**：因为 `conv_1` 的 `out_channels=8`，第二层每个核需要在 8 个通道上做加权和，核深度必须与输入通道数一致；写成 4 会在前向传播时报形状不匹配错误（期望 4 通道输入却收到 8 通道）。

### 4.2 特征图形状计算

#### 4.2.1 概念说明

这个模块回答的问题是：**给定一层超参数，输出特征图到底多大？** 这是读一切 CNN 源码的基本功——看到层数就应能在脑中算出形状；反过来说，看懂了形状也就看懂了结构。

对卷积层和池化层，通用公式是：

\[ o = \left\lfloor \frac{w - k + 2p}{s} \right\rfloor + 1 \]

其中 \(w\) 是输入尺寸（高或宽，公式对两者独立成立）、\(k\) 是核尺寸、\(p\) 是填充、\(s\) 是步长。

一个高频特例是**同名填充（same padding）**：希望 \(o = w\)（尺寸不变，只有卷积负责「换通道」、池化负责「减尺寸」）。令 \(s=1\)、\(o=w\)，反解出：

\[ p = \frac{k-1}{2} \]

对 3×3 核即 \(p=1\)，对 5×5 核即 \(p=2\)。这正是仓库几乎所有 CNN notebook 采用的「卷积保尺寸 + 2×2 池化减半」设计语言。

通道数的演化则只由 `out_channels` 决定：本讲网络是 1 → 8 → 16，即「越深通道越多、空间越小」——深层用更多通道去编码已经被池化压缩的空间信息，这一模式将在 LeNet/VGG/ResNet 中反复出现。

#### 4.2.2 核心流程

把公式代入 cnn-basic 的每一层，可得到完整形状表（batch 维 N=128 略去）：

| 层 | 参数 | 计算 | 输出形状 |
| --- | --- | --- | --- |
| 输入 | — | — | [1, 28, 28] |
| conv_1 | k=3, s=1, p=1 | (28−3+2)/1+1 = 28 | [8, 28, 28] |
| pool_1 | k=2, s=2, p=0 | (28−2)/2+1 = 14 | [8, 14, 14] |
| conv_2 | k=3, s=1, p=1 | (14−3+2)/1+1 = 14 | [16, 14, 14] |
| pool_2 | k=2, s=2, p=0 | (14−2)/2+1 = 7 | [16, 7, 7] |
| view | — | 16×7×7 = 784 | [784] |
| linear_1 | 784→10 | — | [10] |

验证数据来自 notebook 保存的输出：数据加载单元格打印的 `Image batch dimensions: torch.Size([128, 1, 28, 28])` 见 [pytorch_ipynb/cnn/cnn-basic.ipynb:L91](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-basic.ipynb#L91)（打印语句在 [L140](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-basic.ipynb#L140)）。

#### 4.2.3 源码精读

[pytorch_ipynb/cnn/cnn-basic.ipynb:L168-L171](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-basic.ipynb#L168-L171) —— 作者把 4.2.1 的公式直接写进注释：`(w - k + 2*p)/s + 1 = o`，并给出反解 `p = (s(o-1) - w + k)/2`。**这是全仓库 CNN notebook 的通用注释模板**，后面每一讲的 notebook 都会重复这段推导，读懂它就能自证所有行尾的验算注释（如 `(1(28-1) - 28 + 3) / 2 = 1` 就是代入 s=1, o=28, w=28, k=3 求 p）。

[pytorch_ipynb/cnn/cnn-basic.ipynb:L173-L192](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-basic.ipynb#L173-L192) —— 每层上方的形状注释（`# 28x28x1 => 28x28x8`、`# 28x28x8 => 14x14x8`、`# 14x14x8 => 14x14x16`、`# 14x14x16 => 7x7x16`）就是 4.2.2 那张表逐行的源码对应物。注意作者写形状用的是 HWC 顺序（28x28x8），而张量实际是 NCHW——读注释时别被顺序绕晕。

[pytorch_ipynb/cnn/cnn-basic.ipynb:L214](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-basic.ipynb#L214) —— `logits = self.linear_1(out.view(-1, 7*7*16))`：`view(-1, 784)` 的 `-1` 让 PyTorch 自动推断 batch 维，第二个分量 **784 必须精确等于 C×H×W=16×7×7**，写错一个数字前向就会崩。这行代码是「卷积特征 → 全连接分类」的握手点，也是初学者最常报形状错误的地方。cnn-he-init 中对应的是 `-1, 7*7*8`（见 [pytorch_ipynb/cnn/cnn-he-init.ipynb:L194](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-he-init.ipynb#L194)），因为它的通道是 8 而不是 16。

#### 4.2.4 代码实践

**实践目标**：不依赖注释，用 forward hook 自动打印每层输出的真实形状，与手工推导对账。

**操作步骤**（示例代码，把 cnn-basic 的 `ConvNet` 类定义复制进来后运行）：

```python
# 示例代码：用 forward hook 追踪每层输出形状
import torch

# ... 此处粘贴 cnn-basic.ipynb Model 单元格中的 ConvNet 类定义 ...

shapes = []
hooks = []
for name, layer in model.named_modules():
    if isinstance(layer, (torch.nn.Conv2d, torch.nn.MaxPool2d, torch.nn.Linear)):
        def make_hook(nm, ly):
            def hook(module, inp, out):
                shapes.append((nm, type(ly).__name__, tuple(out.shape)))
            return hook
        hooks.append(layer.register_forward_hook(make_hook(name, layer)))

x = torch.zeros(128, 1, 28, 28)          # 一个假 batch，不取真实数据也行
with torch.no_grad():
    model(x)
for nm, tp, s in shapes:
    print(f"{nm:10s} {tp:12s} -> {s}")
for h in hooks:
    h.remove()
```

**需要观察的现象**：打印出 5 行（conv_1、pool_1、conv_2、pool_2、linear_1），形状依次为 `[128,8,28,28]`、`[128,8,14,14]`、`[128,16,14,14]`、`[128,16,7,7]`、`[128,10]`。

**预期结果**：hook 输出与 4.2.2 的形状表逐行一致；如不一致，优先检查 `view` 前的通道数是否与 `Linear` 输入维数匹配。运行输出待本地验证（形状推导本身是上表可复核的算术）。

顺带说明：用全 0 张量做「哑输入」探形状是工程上常用技巧，不需要加载 MNIST；本仓库后续讲义（如 u4-l5 的 hooks）会再次用到 hook 机制。

#### 4.2.5 小练习与答案

**练习 1**：输入 32×32 的 CIFAR-10 彩色图像，做 `Conv2d(3, 16, kernel_size=5, stride=1, padding=2)`，输出形状是什么？

**答案**：(32−5+2×2)/1+1 = 32，输出 `[N, 16, 32, 32]`——same padding 公式 \(p=(k-1)/2=2\) 保住了尺寸。这正是下一讲 CIFAR 版 LeNet 的第一步。

**练习 2**：若把 `pool_2` 的 `kernel_size` 改成 `(2,2)` 但 `stride` 误写成 `(1,1)`，7×7 的输入输出是几？网络还能正常跑完吗？

**答案**：(7−2)/1+1 = 6，输出 6×6；网络仍能前向运行（卷积层不检查尺寸），但 `view(-1, 7*7*16)` 会因元素个数 16×6×6=576 ≠ 784 而报错。**形状错误往往在展平处才暴露**，这是排查 CNN 结构问题的重要线索。

**练习 3**：从 28×28 到 7×7，空间缩小到 1/16。如果要设计三层卷积且最终空间尺寸为 4×4（每层配一个 2×2 池化），输入应该是多大？

**答案**：三次减半后为输入的 1/8，故输入应为 4×8 = 32（32→16→8→4）。

### 4.3 He 初始化

#### 4.3.1 概念说明

这个模块回答的问题是：**训练开始前，权重该设成多大的随机数？** 看似小事，实则直接决定第一个 epoch 网络「学不学得动」。

直觉如下：权重太小，信号逐层衰减，logits 趋近 0，损失停在 \(\ln 10 \approx 2.3026\)（10 类均匀分布的交叉熵），反向传播的误差信号同样被小权重压低，网络长时间「装死」；权重太大，信号逐层放大导致饱和，梯度爆炸。理想目标是：**各层激活的方差在前向时大致不变**。

对 ReLU 激活，He 等人（2015）给出的答案是让权重服从均值为 0、标准差为

\[ \text{std} = \sqrt{\frac{2}{\text{fan\_in}}} \]

的正态分布，其中 \(\text{fan\_in}\) 是该层「每个输出神经元连接的输入个数」：对 `Conv2d` 是 \(C_{in} \times k_h \times k_w\)，对 `Linear` 就是输入特征数。直观理解：ReLU 会把约一半的激活置零（方差减半），所以系数取 2 恰好把方差补回来。PyTorch 中对应 `nn.init.kaiming_normal_`（Kaiming 是 He Kaiming 的拼音写法）。

**适用场景**：网络用 ReLU（或 leaky ReLU）做激活时首选 He 初始化；若激活是 tanh/sigmoid，则应改用 Xavier/Glorot 初始化（系数为 1 而不是 2）。本讲网络用 `F.relu`，所以配 He。

#### 4.3.2 核心流程

cnn-he-init 中初始化的执行过程：

1. 模型各层按 PyTorch 默认策略初始化（`Conv2d`/`Linear` 默认用 Kaiming-uniform 变体）。
2. 构造函数末尾，`self.modules()` 递归枚举模型的所有子层。
3. 对每个 `Conv2d` 或 `Linear`：`nn.init.kaiming_normal_(m.weight.detach())` 以 He 正态分布**重写**权重，`m.bias.detach().zero_()` 把偏置清零。
4. 训练开始，第一步梯度即有效，损失从第一个 batch 起明显下降。

与 cnn-basic 的差异仅在步骤 2–3 的具体分布：它用 `normal_(0.0, 0.01)`（标准差固定 0.01 的窄高斯），并且作者在注释里明确声明这种初始化**不推荐、仅为演示**。

#### 4.3.3 源码精读

先看 cnn-basic 的「反例」初始化：

[pytorch_ipynb/cnn/cnn-basic.ipynb:L195-L204](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-basic.ipynb#L195-L204) —— `for m in self.modules():` 枚举所有层，对 `Conv2d` 与 `Linear` 执行 `m.weight.data.normal_(0.0, 0.01)`（标准差 0.01 的窄高斯）并把偏置清零；注释直言「Gaussian weight init is not recommended and only for demonstration purposes」。细心的话会注意到偏置在这段代码里被清零了两次（`m.bias.data.zero_()` 和 `m.bias.detach().zero_()`）——历史遗留的冗余写法，效果等价，但提醒我们读源码时要分辨「必要逻辑」与「无伤大雅的赘笔」。

再看 cnn-he-init 的「正例」：

[pytorch_ipynb/cnn/cnn-he-init.ipynb:L196-L205](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-he-init.ipynb#L196-L205) —— 注释横幅「Reinitialize weights using He initialization」下的循环里，`Conv2d` 与 `Linear` 分支都调用 `nn.init.kaiming_normal_(m.weight.detach())` 并清零偏置。`m.weight.detach()` 的写法呼应 u2-l3 的结论：脱离计算图后再原地改值，避免破坏 autograd 记账。按公式，本网络三层的初始 std 分别约为：conv_1 \(\sqrt{2/9}\approx0.47\)、conv_2 \(\sqrt{2/36}\approx0.24\)、linear_1 \(\sqrt{2/392}\approx0.07\)——**层越宽 std 越小**，这正是「方差守恒」的实现方式；对比 cnn-basic 一刀切的 0.01，差异巨大。

两种初始化对**训练动态**的影响直接写在两份 notebook 保存的日志里：

- cnn-basic（窄高斯）：[L243](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-basic.ipynb#L243) 起的第一个 epoch，损失从 2.3026 起步，直到第 450 个 batch 仍在 2.23 附近徘徊，整个 epoch 都贴着 \(\ln 10\)；[L253](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-basic.ipynb#L253) 显示第 1 个 epoch 训练准确率仅 **65.38%**，到 [L265](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-basic.ipynb#L265) 第 2 个 epoch 才到 93.11%。
- cnn-he-init（He）：[L245](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-he-init.ipynb#L245) 第一个 batch 损失 2.4577（He 的 std 较大，初始 logits 偏离 0，损失可高于 \(\ln 10\)），但第 50 个 batch 就降到 1.11；[L255](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-he-init.ipynb#L255) 显示第 1 个 epoch 训练准确率即达 **91.97%**。

结论：He 初始化把这个网络「学会基本分类」的时间从两个 epoch 压缩到一个 epoch 之内。但也要如实说明：窄高斯版本最终**逃离了平台期**，10 个 epoch 后两者测试准确率相当（97.97% vs 97.67%，分别见 [cnn-basic L430](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-basic.ipynb#L430) 与 [cnn-he-init L432](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-he-init.ipynb#L432)）——**初始化主要影响收敛速度；网络越深，坏初始化越可能根本训不动**，那才是 He 初始化真正的主战场（下一讲的深层网络会印证这一点）。

#### 4.3.4 代码实践

**实践目标**：用数字验证 `kaiming_normal_` 确实按 \(\sqrt{2/\text{fan\_in}}\) 缩放。

**操作步骤**（示例代码）：

```python
# 示例代码：验证 He 初始化的实际标准差
import torch.nn as nn

conv = nn.Conv2d(in_channels=1, out_channels=4, kernel_size=(3, 3), stride=(1, 1), padding=1)
nn.init.kaiming_normal_(conv.weight.detach())
print(conv.weight.detach().std().item())        # 观察：接近 sqrt(2/9) ≈ 0.471

linear = nn.Linear(7*7*8, 10)
nn.init.kaiming_normal_(linear.weight.detach())
print(linear.weight.detach().std().item())      # 观察：接近 sqrt(2/392) ≈ 0.0714
```

**需要观察的现象**：两个打印值分别落在 0.47 与 0.07 附近（样本量 36 与 3920，统计波动很小；`Linear` 的估计更准）。

**预期结果**：实测 std 与公式值之差的绝对值小于 0.02。若把 `kaiming_normal_` 换成 `normal_(0.0, 0.01)` 再打印，std 应约等于 0.01——与 0.47 相差近 50 倍，这就是两份 notebook 第一个 epoch 判若两网的原因。运行输出待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 He 初始化的系数是 \(\sqrt{2}\) 而 Xavier 是 \(\sqrt{1}\)（指 std 公式的分子）？

**答案**：ReLU 把约一半的激活置 0，前向方差大约减半；乘 \(\sqrt{2}\)（等价于方差乘 2）恰好把这部分损失补回来，使各层方差守恒。tanh/sigmoid 是「压缩」而非「砍半」，对称压缩下 Xavier 的系数 1 即可保持方差稳定。

**练习 2**：`nn.init.kaiming_normal_(m.weight.detach())` 里如果不写 `detach()`，在什么情况下会出问题？

**答案**：`detach()` 返回一个脱离计算图的视图，在其上做原地填充不会影响 autograd 的记录；直接对 `m.weight` 做某些原地操作可能破坏叶子张量的版本计数（`RuntimeError: a leaf Variable that requires grad is being used in an in-place operation`）。这与 u2-l3「先 detach 再原地改权重」是同一条规则。

**练习 3**：cnn-basic 最终测试准确率 97.97%，比 He 版的 97.67% 还略高。这能说明「初始化不重要」吗？

**答案**：不能。本网络只有两个卷积层，窄高斯虽慢但能爬出平台期；实验结论只说明**浅网络的最终精度对初始化不敏感，但收敛速度高度敏感**（第 1 个 epoch：65.38% vs 91.97%）。网络加深后坏初始化常导致梯度彻底消失、完全训不动——那时 He 就不是「更快」而是「能不能训」的区别。

## 5. 综合实践

**任务**：为 cnn-basic 的 `ConvNet` 建立一张「结构档案」——每层输出形状 + 参数量，先手工算，再用程序验证。

**步骤**：

1. **手工推导**：不看源码注释，仅凭 4.2.1 的公式从 `[N,1,28,28]` 逐层推出 5 个层的输出形状（即完成 4.2.2 的表）。
2. **手工算参数**：按 `Conv2d` 参数 = \(C_{out} \times C_{in} \times k_h \times k_w + C_{out}\)、`Linear` 参数 = \(in \times out + out\)、池化为 0，算出每层参数量并求和。
3. **程序验证形状**：把 [cnn-basic L205-L216](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-basic.ipynb#L205-L216) 的 forward 改写成「每过一层就 `print(out.shape)`」的等价版本（或直接用 4.2.4 的 forward hook），喂入 `torch.zeros(128, 1, 28, 28)`，核对与第 1 步是否一致。
4. **程序验证参数量**（示例代码）：

```python
# 示例代码：与手工统计对账
total = 0
for name, p in model.named_parameters():
    n = p.numel()
    total += n
    print(f"{name:20s} {tuple(p.shape)} -> {n}")
print("total:", total)
# 也可用 PyTorch 官方 API 交叉验证：
print(sum(p.numel() for p in model.parameters()))
```

5. **对照实验（可选）**：把第 4 步脚本同时跑在 cnn-he-init 的 `ConvNet` 上，比较两个网络的参数量之比。

**预期结果**（手工可证，程序应逐项吻合）：

| 层 | 输出形状 | 参数量 |
| --- | --- | --- |
| conv_1 | [128, 8, 28, 28] | 80 |
| pool_1 | [128, 8, 14, 14] | 0 |
| conv_2 | [128, 16, 14, 14] | 1168 |
| pool_2 | [128, 16, 7, 7] | 0 |
| linear_1 | [128, 10] | 7850 |
| **合计** | — | **9098** |

cnn-he-init 对应的合计应为 **4266**（40 + 296 + 3930）。

**思考题**：u2-l3 的 MLP（784-128-256-10）有 136,074 个参数、测试准确率 97.54%；本讲 CNN 只有 9,098 个参数（约 1/15），测试准确率却到 97.97%。请用「局部连接 + 权值共享」解释参数效率从何而来。这正是 CNN 成为主流视觉模型的根本原因，也是整个 U3 单元的价值所在。

## 6. 本讲小结

- 卷积层用「局部窗口 + 滑动复用」替代全连接：参数量只取决于 \(C_{out} \times C_{in} \times k \times k\)，与图像大小无关；池化层无参数地把空间减半并带来平移不变性；两者通过 `forward` 里「conv → ReLU → pool」的节奏组合（[cnn-basic L205-L216](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-basic.ipynb#L205-L216)）。
- 输出尺寸公式 \(o = \lfloor (w-k+2p)/s \rfloor + 1\) 及其反解 \(p=(s(o{-}1)-w+k)/2\) 直接写在源码注释里（[cnn-basic L168-L171](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-basic.ipynb#L168-L171)），是全仓库 CNN 的通用阅读钥匙；cnn-basic 的形状链为 28×28×1 → 28×28×8 → 14×14×8 → 14×14×16 → 7×7×16 → 784 → 10。
- 通道数由 `out_channels` 决定且「越深越多」；末端 `view(-1, 7*7*16)` 必须与 C×H×W 精确匹配，是形状错误的高发地（[cnn-basic L214](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-basic.ipynb#L214)）。
- He 初始化（`nn.init.kaiming_normal_`，std = \(\sqrt{2/\text{fan\_in}}\)）是 ReLU 网络的标准选择；对照实验显示它把第 1 个 epoch 的训练准确率从 65.38% 提升到 91.97%，而窄高斯初始化被作者标注为「仅为演示、不推荐」。
- 初始化主要影响**收敛速度与可训练深度**：浅网络最终精度或许能追平，但深层网络缺了它可能完全训不动。
- 参数效率是 CNN 的核心卖点：9,098 参数、97.97% 准确率，全面优于 136,074 参数、97.54% 的 u2-l3 MLP。

## 7. 下一步学习建议

下一讲（u3-l2）将进入**经典早期架构：LeNet-5 与 AlexNet**。建议提前做两件事：

1. 通读 [pytorch_ipynb/cnn/cnn-lenet5-mnist.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-lenet5-mnist.ipynb)，用本讲的公式独立推出它的每一层形状，再与 notebook 注释对账——LeNet-5 会首次引入「`nn.Sequential` 容器」这种堆叠式写法。
2. 关注 32×32（CIFAR）与 28×28（MNIST）输入如何迫使结构微调，这个「输入尺寸 → 结构适配」的思考方式会贯穿整个 CNN 家族。

如果想先巩固本讲，可以重跑 4.2.4 的 hook 实验，但把 hook 换成 `register_forward_pre_hook` 同时打印输入与输出形状，体会「形状在层间如何握手」。
