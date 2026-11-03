# 残差网络 ResNet 与残差块

## 1. 本讲目标

上一讲（u3-l3）我们看到：VGG 用 3×3 小核堆叠出 16~19 层的深网络，但实验也证明深网络对初始化极其敏感、训练不足时「更深未必更好」。本讲就来解决这个「深度瓶颈」问题——残差网络 ResNet（He et al., 2016）。学完本讲，你应该能够：

1. 说清楚**跳跃连接（skip connection）为什么能让网络更深**：残差学习的数学形式与梯度视角。
2. 动手写出**三种残差块写法**：identity 块（捷径不做任何变换）、convolutional 块（捷径用 1×1 卷积调整尺寸）、helper 类封装。
3. 读懂标准 ResNet 的三大机制：`BasicBlock`、`downsample`（下采样捷径）与 `_make_layer`（按配置批量组装 stage）。
4. 区分 **BasicBlock 与 Bottleneck** 两种残差块，理解 `expansion` 类属性的作用。
5. 手工推导 ResNet-18 在 28×28 MNIST 输入上每个 stage 的特征图形状，并能解释 `downsample` 何时被触发。

## 2. 前置知识

- **退化（degradation）问题**：在 ResNet 之前，人们发现把plain网络（无捷径的普通堆叠网络）做到几十层后，训练误差反而比浅层网络更高——这不是过拟合（训练集上就更差），而是深度带来的**优化困难**。
- **残差学习**：与其让堆叠层直接学习目标映射 \( H(x) \)，不如让它学习「残差」\( F(x) = H(x) - x \)。原始输出变成：

  \[ H(x) = F(x) + x \]

  如果某个新增层是多余的，优化器只需把 \( F(x) \) 推向 0，就得到恒等映射——「什么都不做」变成了最容易学的解。而plain网络要学恒等映射，反而需要多层非线性去逼近。
- **梯度视角**：对 \( x_L = F(x_l) + x_l \) 求梯度：

  \[ \frac{\partial L}{\partial x_l} = \frac{\partial L}{\partial x_L} \left( 1 + \frac{\partial F}{\partial x_l} \right) \]

  括号里的常数 **1** 意味着梯度可以「无损」地沿捷径直传到浅层，缓解了深层链式法则连乘导致的梯度消失。这正是 u2-l5 中手工反向传播的链式法则在结构上的对应物。
- **约定**：本讲沿用在 u3-l1 学过的输出尺寸公式 \( o = \lfloor (w - k + 2p)/s \rfloor + 1 \)，以及「NCHW」四维张量格式（批量×通道×高×宽）；1×1 卷积（NiN 一讲讲过的「逐像素全连接」）在本讲有新用途——调整捷径的通道数与分辨率。

## 3. 本讲源码地图

本讲涉及 4 个 notebook，全部位于 `pytorch_ipynb/cnn/` 下：

| 文件 | 作用 |
| --- | --- |
| `resnet-ex-1.ipynb` | 教学实验场：用 MNIST 上的迷你网络演示三种残差块写法（identity 块、conv 块、helper 类），三段代码共享同一套数据与训练循环 |
| `cnn-resnet18-mnist.ipynb` | 标准 ResNet-18（BasicBlock，`layers=[2,2,2,2]`），改写自 torchvision 官方实现，含 `conv3x3`/`BasicBlock`/`ResNet`/`_make_layer` 全套机制 |
| `cnn-resnet34-mnist.ipynb` | 与 ResNet-18 共用同一套模型代码，仅把配置换成 `layers=[3,4,6,3]`，用于体会「配置化组装」的威力 |
| `cnn-resnet50-mnist.ipynb` | Bottleneck 版残差块（`expansion=4`），展示 1×1 → 3×3 → 1×1 的瓶颈结构与通道扩张 |

> **读源码提示**：notebook 是 JSON 文件，本文引用的行号是 `.ipynb` 原始文件（GitHub 渲染的就是它）中的行号，永久链接可直接跳转。这三个 ResNet notebook 里 `DEVICE = "cuda:1"` / `"cuda:2"` 是作者多卡机器的写法，本地复现时请按 u1-l2 学过的方式改成 `"cuda:0"` 或 `"cpu"`。

## 4. 核心概念与源码讲解

### 4.1 残差块的三种写法（resnet-ex-1.ipynb）

#### 4.1.1 概念说明

残差块 = **主路径（权重层堆叠）+ 一条捷径（shortcut）**，两路结果逐元素相加后再过激活。按捷径的形态分两种：

- **identity 块**：捷径什么都不做，直接把输入 \( x \) 加到主路径输出上。要求主路径**不改变**张量的空间尺寸和通道数（否则没法相加）。对应图中 resnet-ex-1-1.png 的结构。
- **convolutional 块**：主路径会改变尺寸/通道数（例如 stride=2 降采样、通道翻倍），捷径就得配一个 **1×1 卷积 + BatchNorm** 把 \( x \) 「整容」成与主路径输出相同的形状。这就是后续标准 ResNet 里 `downsample` 的雏形。

#### 4.1.2 核心流程

identity 块的前向流程（伪代码）：

```text
输入 x
shortcut = x                 # 先存一份
out = conv_1(x) → BN → ReLU
out = conv_2(out) → BN       # 注意：主路径最后一个 BN 之后、相加之前不过 ReLU
out = out + shortcut         # 逐元素相加
out = ReLU(out)
```

convolutional 块只多一步：相加之前 `shortcut = BN(conv_1x1(x))`，用 stride=2 的 1×1 卷积同步完成「空间减半 + 通道对齐」。

一个容易被忽视的细节：**相加之前主路径不再过 ReLU**。这样 \( F(x) \) 的取值是自由的（可以为负），\( H(x) = F(x)+x \) 才是真正的「残差 + 恒等」结构；ReLU 只放在相加之后。

#### 4.1.3 源码精读

**写法一：identity 块。** 模型含两个残差块，主路径用「1×1 卷积升到 4 通道 → 3×3 卷积降回 1 通道」的组合，输入输出都是 28×28×1，尺寸、通道完全不变，所以捷径可以直接相加：

[pytorch_ipynb/cnn/resnet-ex-1.ipynb:L182-L205](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/resnet-ex-1.ipynb#L182-L205) —— `ConvNet.__init__` 的第一个残差块：`conv_1` 是 1×1 卷积（1→4 通道），`conv_2` 是带 padding 的 3×3 卷积（4→1 通道），各自配 BatchNorm2d；注释里的 `28x28x1 => 28x28x4 => 28x28x1` 说明了「先升后降、形状复原」的设计意图。

[pytorch_ipynb/cnn/resnet-ex-1.ipynb:L237-L248](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/resnet-ex-1.ipynb#L237-L248) —— `forward` 中第一个残差块的核心四步：`shortcut = x` 保存输入 → 主路径两层卷积（中间 ReLU、末端只 BN 不 ReLU）→ `out += shortcut` 逐元素相加 → 再 ReLU。第二个残差块结构完全相同（[L262](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/resnet-ex-1.ipynb#L262) 处又一次 `out += shortcut`）。

这个 identity 版网络因为通道数始终是 1、表达能力受限，10 个 epoch 后测试准确率只有 **92.20%**（见 notebook 评估单元的输出）。

**写法二：convolutional 块。** 每个残差块的主路径用 stride=2 的 3×3 卷积降采样并升通道，捷径必须同步「整形」：

[pytorch_ipynb/cnn/resnet-ex-1.ipynb:L520-L553](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/resnet-ex-1.ipynb#L520-L553) —— 第二个 `ConvNet` 的定义：第一个残差块主路径是 `conv_1`（3×3、stride=2，28×28×1 → 14×14×4）+ `conv_2`（1×1，14×14×4 → 14×14×8）；关键是 [L545](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/resnet-ex-1.ipynb#L545) 起的 `conv_shortcut_1`：1×1 卷积、stride=2、1→8 通道，把捷径输入从 28×28×1 变成 14×14×8，与主路径输出精确对齐。

[pytorch_ipynb/cnn/resnet-ex-1.ipynb:L588-L607](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/resnet-ex-1.ipynb#L588-L607) —— `forward` 中第一个残差块：主路径算完后，捷径先过 `conv_shortcut_1` + BN（代码注释特意标注 "match up dimensions using a linear function (no relu)"——捷径上不过 ReLU），然后 `out += shortcut` 再 ReLU。两个残差块把特征图从 28×28×1 压到 7×7×32，最后展平接全连接。

这一版测试准确率 **97.91%**，比 identity 版高出约 5.7 个百分点——降采样 + 通道增长的层级式特征提取功不可没。

**写法三：helper 类封装。** 前两种写法把残差块的层全部平铺在外层 `ConvNet` 里，块一多就会重复冗长。第三种写法把「conv 残差块」抽成可复用的 `ResidualBlock` 类：

[pytorch_ipynb/cnn/resnet-ex-1.ipynb:L857-L900](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/resnet-ex-1.ipynb#L857-L900) —— `ResidualBlock(torch.nn.Module)`：构造函数接收 `channels` 列表（`[输入通道, 中间通道, 输出通道]`），内部创建主路径两层卷积（[L862](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/resnet-ex-1.ipynb#L862) 的 3×3/stride=2 与随后的 1×1）以及捷径的 1×1 卷积（[L876](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/resnet-ex-1.ipynb#L876)）；`forward` 里完成「主路径 → 捷径整形 → 相加 → ReLU」（[L884](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/resnet-ex-1.ipynb#L884) 保存 shortcut，[L897](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/resnet-ex-1.ipynb#L897) 相加）。

[pytorch_ipynb/cnn/resnet-ex-1.ipynb:L915-L935](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/resnet-ex-1.ipynb#L915-L935) —— 使用侧变得极简：`ResidualBlock(channels=[1, 4, 8])` 与 `ResidualBlock(channels=[8, 16, 32])` 两行就搭出与写法二**完全等价**的网络（训练日志逐 batch 完全一致，测试准确率同为 97.91%，可对照 notebook 输出验证）。这正是下一节 torchvision 风格 `BasicBlock` 的思想原型：**块封装成类，网络按配置组装**。

#### 4.1.4 代码实践

**实践 A：三种写法对照阅读 + 修改块数量。**

1. **实践目标**：确认「helper 类写法与平铺写法等价」，并体会封装对扩展性的影响。
2. **操作步骤**：
   - 打开 `resnet-ex-1.ipynb`，从上到下依次运行三个模型单元（它们互相覆盖 `model` 变量，顺序运行即可）。
   - 对比写法二与写法三的训练日志：第一个 batch 的 Cost 是否都是 `2.3318`？（是——说明两网络结构等价、随机种子生效。）
   - 复制写法三的 `ConvNet`，把两个残差块改成三个：`ResidualBlock(channels=[1, 4, 8])`、`ResidualBlock(channels=[8, 16, 32])`、`ResidualBlock(channels=[32, 64, 128])`，并把分类头改成 `torch.nn.Linear(4*4*128, num_classes)`（28→14→7→4，每块 stride=2 减半一次）。
3. **需要观察的现象**：第三个块加入后特征图变为 4×4×128；训练能否正常收敛。
4. **预期结果**：写法二与写法三首个 batch 损失一致；三块网络仍可训练，准确率与两块版本相近或略变（MNIST 上 28×28 已很小，继续降采样未必有收益）。
5. 修改后的准确率与训练曲线**待本地验证**（作者未在 notebook 中提供三块版本的运行结果）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 identity 块的主路径可以「1×1 升通道再 3×3 降通道」，而输出必须回到与输入相同的形状？

**答案**：因为捷径 `shortcut = x` 未做任何变换，逐元素相加要求两个张量形状完全一致（批量、通道、高、宽都相同）。主路径内部形状可以随意变化，但出口必须「整形回」入口形状。

**练习 2**：convolutional 块的捷径上为什么用 1×1 卷积而不是 3×3？为什么捷径上不加 ReLU？

**答案**：捷径的职责只是「对齐尺寸」（linear projection，论文原文称 option B），1×1 卷积参数最少且能独立控制 stride 与通道；加 ReLU 会破坏 \( H(x)=F(x)+x \) 的线性恒等通路，让「学成恒等映射」失去意义，源码注释 "no relu" 明确强调了这一点。

**练习 3**：`out += shortcut` 使用原地加法（in-place）。结合 u2-l3 的知识，这会破坏 autograd 吗？

**答案**：不会。`out` 此处的值是刚由 `conv_2_bn` 计算得到的、尚未被其他节点引用为输出的中间结果，autograd 仍能对 `+=` 建立加法节点记录梯度；但如果 `out` 已被外部引用（例如还想单独取用它），原地操作就可能报错。标准写法 `out = out + residual`（下一节的 `BasicBlock`）更稳妥。

### 4.2 BasicBlock 与 downsample 机制（cnn-resnet18-mnist.ipynb）

#### 4.2.1 概念说明

标准 ResNet 把 4.1 的思路工程化：残差块统一封装为 `BasicBlock`，捷径是否「整形」不再靠人肉判断，而是由一个可选的 `downsample` 子模块决定——**传了就整形（convolutional 块），不传就是 identity 块**。一个类覆盖两种形态：

- `downsample=None`：捷径直接相加（identity 块）。
- `downsample=nn.Sequential(1×1 Conv + BN)`：捷径先投影再相加（convolutional 块）。

`BasicBlock` 主路径是两个 3×3 卷积（这就是「Basic」：无瓶颈），`expansion = 1` 表示块输出通道数等于构造时传入的 `planes`（对比 4.3 节 Bottleneck 的 `expansion = 4`）。

#### 4.2.2 核心流程

`BasicBlock.forward` 与 4.1.2 的流程几乎一致，仅捷径不同：

```text
输入 x
residual = x                        # 保存捷径初值
out = conv1(3×3, 可带 stride) → BN → ReLU
out = conv2(3×3, stride=1) → BN
if downsample is not None:
    residual = downsample(x)        # 1×1 Conv(stride) → BN
out = out + residual                # 非原地写法，更安全
out = ReLU(out)
```

注意：**stride 放在 `conv1` 上**，`conv2` 恒为 stride=1；`downsample` 的 1×1 卷积使用与 `conv1` 相同的 stride，保证主路径与捷径的空间尺寸同步减半。

#### 4.2.3 源码精读

[pytorch_ipynb/cnn/cnn-resnet18-mnist.ipynb:L320-L324](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-resnet18-mnist.ipynb#L320-L324) —— 工具函数 `conv3x3`：固定 kernel=3、padding=1（同名填充，保尺寸），`bias=False` 是关键细节——后面紧跟 BatchNorm（BN 自带可学习的偏移 β），卷积再带偏置是冗余参数，省掉它既减参数又让 BN 数学上更干净。

[pytorch_ipynb/cnn/cnn-resnet18-mnist.ipynb:L326-L357](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-resnet18-mnist.ipynb#L326-L357) —— `BasicBlock` 全貌：类属性 `expansion = 1`；`__init__` 中 `conv1 = conv3x3(inplanes, planes, stride)`（stride 只作用于第一层），`conv2 = conv3x3(planes, planes)`；`downsample` 不在块内创建，而是**由外部传入**存为属性。`forward`（[L339-L356](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-resnet18-mnist.ipynb#L339-L356)）里 `if self.downsample is not None: residual = self.downsample(x)` 一行完成两种形态的切换。

[pytorch_ipynb/cnn/cnn-resnet18-mnist.ipynb:L360-L363](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-resnet18-mnist.ipynb#L360-L363) —— `ResNet` 类开头：`self.inplanes = 64` 与 `grayscale` 开关（MNIST 灰度图输入通道 `in_dim=1`，彩色图为 3——与 u3-l2 LeNet 迁移时的通道适配同一手法）。`inplanes` 是贯穿组装全过程的「当前通道数」游标，下一节详细讲。

[pytorch_ipynb/cnn/cnn-resnet18-mnist.ipynb:L368-L387](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-resnet18-mnist.ipynb#L368-L387) —— ResNet 的「stem（茎）」与四个 stage 的声明：`conv1` 是 7×7、stride=2 的大核首层卷积 + BN + ReLU + 3×3/stride=2 最大池化，两步就把 224×224 的 ImageNet 输入压到 56×56（MNIST 28×28 压到 7×7，见 4.2.4）；随后 `layer1`~`layer4` 分别以 64/128/256/512 通道调用 `_make_layer`，只有 `layer1` 不传 `stride=2`；末尾 `fc = nn.Linear(512 * block.expansion, num_classes)`——输入维数乘 `expansion`，正是为 Bottleneck 预留的接口。初始化循环对每个 Conv2d 做 \( \mathcal{N}(0, \sqrt{2/n}) \) 采样（\( n = k_h k_w \cdot C_{out} \)，即 u3-l1 讲过的 He 初始化，此处按 fan-out 口径计算），对 BatchNorm 权重填 1、偏置清 0。

[pytorch_ipynb/cnn/cnn-resnet18-mnist.ipynb:L406-L424](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-resnet18-mnist.ipynb#L406-L424) —— `ResNet.forward`：stem → layer1 → layer2 → layer3 → layer4 → 展平 → fc。注意 [L416-L418](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-resnet18-mnist.ipynb#L416-L418) 的注释：MNIST 输入太小，到 layer4 输出已经是 1×1，因此**注释掉了 avgpool**，直接 `x.view(x.size(0), -1)` 展平。这是把 ImageNet 架构迁到小图上必须做的适配（224×224 输入在 ImageNet 原版中此处是 7×7，需要 `AvgPool2d(7)`）。

[pytorch_ipynb/cnn/cnn-resnet18-mnist.ipynb:L427-L435](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-resnet18-mnist.ipynb#L427-L435) —— 工厂函数 `resnet18`：`ResNet(block=BasicBlock, layers=[2, 2, 2, 2], ...)`。ResNet-18 的「18」由此而来：stem 1 层卷积 + 4 个 stage 共 (2+2+2+2)×2 = 16 层卷积 + 1 层全连接 = **18 层带权重层**。

这套模型代码在 MNIST 上训练 10 个 epoch，notebook 保存的测试准确率为 **99.06%**——显著高于 u2-l3 MLP 的 97.54% 和 u3-l1 双卷积层的水平。

#### 4.2.4 代码实践

**实践 B：打印 ResNet-18 各 stage 输出形状，解释 downsample 何时触发。**（本讲综合实践的第一半，此处在 notebook 里独立完成）

1. **实践目标**：用 forward hook 实测 MNIST 输入经过每个 stage 的形状，与手工推导对照；统计哪些块带 `downsample`。
2. **操作步骤**（示例代码，基于 `cnn-resnet18-mnist.ipynb` 的模型定义，需先运行其 MODEL 单元并把 `DEVICE` 改为本机可用设备）：

   ```python
   # 示例代码：打印 ResNet-18 各 stage 输出形状
   import torch
   model = resnet18(NUM_CLASSES).eval()

   shapes = []
   def make_hook(name):
       def hook(module, inp, out):
           shapes.append((name, tuple(out.shape)))
       return hook

   model.maxpool.register_forward_hook(make_hook('stem(maxpool)'))
   for i in range(1, 5):
       getattr(model, f'layer{i}').register_forward_hook(make_hook(f'layer{i}'))

   with torch.no_grad():
       model(torch.randn(1, 1, 28, 28))   # 灰度 MNIST 尺寸
   for name, s in shapes:
       print(f'{name:>14}: {s}')

   # 再检查每个 BasicBlock 是否带 downsample
   for i in range(1, 5):
       stage = getattr(model, f'layer{i}')
       for j, blk in enumerate(stage):
           print(f'layer{i}.{j} downsample:',
                 'None' if blk.downsample is None else '1x1 Conv+BN')
   ```

3. **需要观察的现象**：各 stage 输出形状依次变化；只有部分块带 downsample。
4. **预期结果**（可先用尺寸公式手工推导验证）：stem 输出 `[1, 64, 7, 7]`（28→14→7），layer1 `[1, 64, 7, 7]`，layer2 `[1, 128, 4, 4]`，layer3 `[1, 256, 2, 2]`，layer4 `[1, 512, 1, 1]`；downsample 只出现在 **layer2/3/4 各自的第一个块**（stride=2 且通道改变），layer1 的两个块与其余块均为 `None`。展平后 512 维正好对应 `fc` 的输入。实际打印数值**待本地验证**。
5. 若懒得跑代码，也可以纯手工推导：对每个 stage 的第一个 `conv3x3`（stride=2、padding=1）套 \( o = \lfloor (w-3+2)/2 \rfloor + 1 = \lfloor w/2 \rfloor \)，结果应与上面一致。

#### 4.2.5 小练习与答案

**练习 1**：ResNet-18 在 MNIST 上，为什么 `layer1` 的第一个块不需要 downsample？

**答案**：`_make_layer(block, 64, 2)` 未传 stride（默认 1），且进入 layer1 时 `self.inplanes = 64` 恰好等于 `planes * expansion = 64 * 1`。「空间不变 + 通道不变」两个条件都满足，捷径可直接相加。（对比 4.3 节：ResNet-50 的 layer1 虽然 stride 也是 1，却因通道不相等而需要 downsample。）

**练习 2**：`conv3x3` 里 `bias=False`，去掉这个参数网络还能跑吗？有什么代价？

**答案**：能跑，结果也基本等价（BN 的 β 吸收了卷积偏置的作用），但每个卷积层会多出 `out_channels` 个冗余可训练参数，且这些参数的梯度几乎学不到有效信息。ImageNet 时代的标准实践是「Conv(bias=False) + BN」。

**练习 3**：把 `forward` 里注释掉的 `self.avgpool(x)` 恢复，MNIST 上会发生什么？

**答案**：layer4 输出已是 1×1，`AvgPool2d(7, stride=1)` 对 1×1 输入会在边界大量零填充（7×7 窗口套在 1×1 上），输出仍是 512×1×1，数值上接近原值但被填充稀释——展平后 `fc` 输入维数仍是 512，程序不报错，但属于无意义的计算。这正说明「迁移架构到小图」需要检查每层的实际形状，而不是照搬。

### 4.3 `_make_layer` 组装与 Bottleneck（ResNet-18/34/50）

#### 4.3.1 概念说明

resnet-ex-1 的 helper 类要手工实例化每个块、手工核对通道衔接；标准 ResNet 用一个 `_make_layer(block, planes, blocks, stride)` 把整个 stage 的组装自动化。它的三步逻辑：

1. **判断是否需要 downsample**：条件是「stride ≠ 1 **或** 当前 `inplanes` ≠ `planes × expansion`」——即**空间变了或通道变了，任一成立**就给捷径配 1×1 Conv+BN。
2. **放第一个块**：第一个块接收 (当前 inplanes, planes, stride, downsample)，负责这个 stage 的全部「变形」（降采样、变通道）。
3. **放其余块**：更新 `self.inplanes = planes * expansion` 后，其余块全部是 stride=1、无 downsample 的 identity 形态，串联即可。

`self.inplanes` 这个「游标」是关键：它在 `__init__` 里初始化为 64，每建一个 stage 就前移一次，使得「上一个 stage 的输出通道」自动成为「下一个 stage 第一个块的输入通道」——人工核对通道衔接的工作被彻底消除。

`_make_layer(block, ...)` 的第一个参数是**块类本身**（不是实例），这让同一套 ResNet 骨架既能用 `BasicBlock`（两个 3×3，ResNet-18/34）也能用 `Bottleneck`（1×1 → 3×3 → 1×1，ResNet-50/101/152）组装。Bottleneck 的「瓶颈」指中间层窄：先 1×1 压到 `planes`，3×3 卷积在低维空间做，再 1×1 通过 `planes * 4`（`expansion = 4`）扩张回高维——用更少的参数达到更深的层数。层数命名同样成立：ResNet-34 = 1 + (3+4+6+3)×2 + 1 = 34；ResNet-50 = 1 + (3+4+6+3)×3 + 1 = 50。

#### 4.3.2 核心流程

ResNet-18（MNIST 28×28 灰度输入）的完整数据流，结合 `_make_layer` 的组装结果：

```text
输入 1×28×28
└─ stem: conv1(7×7, s2, p3) → BN → ReLU → maxpool(3×3, s2, p1)
       28→14→7，通道 64                        输出 64×7×7
└─ layer1 = _make_layer(BasicBlock, 64,  blocks=2)          64×7×7   （无downsample）
└─ layer2 = _make_layer(BasicBlock, 128, blocks=2, stride=2) 128×4×4  （首块带downsample）
└─ layer3 = _make_layer(BasicBlock, 256, blocks=2, stride=2) 256×2×2  （首块带downsample）
└─ layer4 = _make_layer(BasicBlock, 512, blocks=2, stride=2) 512×1×1  （首块带downsample）
└─ (avgpool 被注释：已是 1×1)
└─ view → 512 维 → fc → 10 类 logits + softmax
```

`_make_layer` 内部的判断逻辑伪代码：

```text
def _make_layer(block, planes, blocks, stride=1):
    downsample = None
    if stride != 1 or self.inplanes != planes * block.expansion:
        downsample = Sequential(Conv1x1(inplanes → planes*expansion, stride), BN)
    layers = [block(self.inplanes, planes, stride, downsample)]   # 首块负责变形
    self.inplanes = planes * block.expansion                      # 游标前移
    for _ in range(1, blocks):
        layers.append(block(self.inplanes, planes))               # 其余块 identity 形态
    return Sequential(*layers)
```

#### 4.3.3 源码精读

[pytorch_ipynb/cnn/cnn-resnet18-mnist.ipynb:L389-L404](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-resnet18-mnist.ipynb#L389-L404) —— `_make_layer` 全文，逐行对应上面的伪代码：[L391-L397](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-resnet18-mnist.ipynb#L391-L397) 构造 downsample（1×1 卷积的 stride 与主路径一致）；[L399-L400](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-resnet18-mnist.ipynb#L399-L400) 首块带着 stride 与 downsample 进场；[L400](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-resnet18-mnist.ipynb#L400) 游标 `self.inplanes = planes * block.expansion` 前移；随后循环追加 identity 形态的块，`nn.Sequential(*layers)` 打包返回。

[pytorch_ipynb/cnn/cnn-resnet34-mnist.ipynb:L429-L436](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-resnet34-mnist.ipynb#L429-L436) —— ResNet-34 的工厂函数：模型代码（`BasicBlock`/`ResNet`/`_make_layer`，见该文件 [L328-L424](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-resnet34-mnist.ipynb#L328-L424)）与 ResNet-18 notebook **逐行相同**，唯一差别是 `layers=[3, 4, 6, 3]`——配置化组装的价值在此一目了然：一行改动，网络从 18 层变 34 层。其保存的测试准确率为 99.04%（10 epoch），与 ResNet-18 的 99.06% 相当——MNIST 数据太简单，深度的收益显示不出来，这也再次印证 u3-l2 的结论：数据难度决定准确率量级。

[pytorch_ipynb/cnn/cnn-resnet50-mnist.ipynb:L340-L376](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-resnet50-mnist.ipynb#L340-L376) —— `Bottleneck` 类：`expansion = 4`（[L341](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-resnet50-mnist.ipynb#L341)）；主路径三层——`conv1` 1×1（inplanes→planes，降维）、`conv2` 3×3（planes→planes，stride 在这层，做空间工作）、`conv3` 1×1（[L350](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-resnet50-mnist.ipynb#L350) 处，planes→planes×4，升维）；forward 与 BasicBlock 同构，[L370-L371](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-resnet50-mnist.ipynb#L370-L371) 处 `if self.downsample is not None: residual = self.downsample(x)`。

[pytorch_ipynb/cnn/cnn-resnet50-mnist.ipynb:L448-L455](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-resnet50-mnist.ipynb#L448-L455) —— 该 notebook 的工厂函数：传入 `block=Bottleneck, layers=[3, 4, 6, 3]`，(3+4+6+3)×3+2 = 50 层。**阅读提示**：这个函数沿用了 `resnet34` 的名字、docstring 也写着 "ResNet-34"，notebook 开头 markdown 还提到 CelebA 性别分类——但看代码本身：实际加载的是 MNIST、`GRAYSCALE = True`、模型是 Bottleneck×[3,4,6,3]。这是作者从 ResNet-34 notebook 复制改造时留下的未清理痕迹，刚好提醒我们 u1-l1 建立的原则：**以代码与变量来源为准，不要轻信标题和注释**。该 notebook 保存的测试准确率为 98.39%。

**BasicBlock vs Bottleneck 对照表**：

| 维度 | BasicBlock（ResNet-18/34） | Bottleneck（ResNet-50/101/152） |
| --- | --- | --- |
| 主路径 | 3×3 → 3×3（两层） | 1×1 → 3×3 → 1×1（三层） |
| `expansion` | 1（输出通道 = planes） | 4（输出通道 = 4×planes） |
| stride 位置 | conv1 | conv2（中间的 3×3） |
| 设计取向 | 结构简单，浅层网络够用 | 参数省、层数深，适合更深网络 |
| `fc` 输入维数 | 512 × 1 = 512 | 512 × 4 = 2048 |

#### 4.3.4 代码实践

**实践 C：改配置造出「ResNet-10」，并验证 downsample 判断逻辑。**

1. **实践目标**：体会「配置即架构」——不改任何类定义，仅改 `layers` 配置得到新网络；并用断言验证 downsample 的触发条件。
2. **操作步骤**（示例代码，基于 `cnn-resnet18-mnist.ipynb` 的 MODEL 单元）：

   ```python
   # 示例代码：自定义层数配置 + downsample 断言
   model = ResNet(block=BasicBlock, layers=[1, 1, 1, 1],
                  num_classes=10, grayscale=True)   # 每stage一块的"ResNet-10"

   # layer1 首块：stride=1 且 64==64*1 → 应无 downsample
   assert model.layer1[0].downsample is None
   # layer2 首块：stride=2 且 64!=128 → 应有 downsample
   assert model.layer2[0].downsample is not None
   # layer2 若有第二个块：stride=1 且 128==128 → 无 downsample
   # （layers=[1,1,1,1] 时 layer2 只有一块，可改回 [2,2,2,2] 再验证）
   m34 = ResNet(block=BasicBlock, layers=[3, 4, 6, 3], num_classes=10, grayscale=True)
   assert m34.layer1[2].downsample is None and m34.layer3[0].downsample is not None

   # Bottleneck 的 layer1 首块：stride=1 但 64 != 64*4=256 → 仍有 downsample！
   from importlib import reload  # 若在 notebook 中，Bottleneck 定义见 resnet50 notebook
   m50 = ResNet(block=Bottleneck, layers=[3, 4, 6, 3], num_classes=10, grayscale=True)
   assert m50.layer1[0].downsample is not None
   ```

   （`Bottleneck` 与 `BasicBlock` 分属两个 notebook，实际操作时把 resnet50 notebook 的 Bottleneck 类复制过来即可。）
3. **需要观察的现象**：所有断言通过；`layers=[1,1,1,1]` 的模型参数量明显小于 ResNet-18。
4. **预期结果**：断言全部通过；特别体会最后一条——**Bottleneck 的 layer1 虽然 stride=1，仅因通道 64≠256 就触发 downsample**，这正是判断条件用「或」而非「且」的原因。
5. 断言结果**待本地验证**（逻辑由 `_make_layer` 源码 L391-L397 直接保证）。

#### 4.3.5 小练习与答案

**练习 1**：ResNet-34 与 ResNet-50 的 `layers` 同为 `[3, 4, 6, 3]`，为什么层数一个是 34 一个是 50？

**答案**：层数按**带权重的卷积/全连接层数**计。BasicBlock 每块 2 层卷积：1 + 16×2 + 1 = 34；Bottleneck 每块 3 层卷积：1 + 16×3 + 1 = 50。`_make_layer` 与层命名无关，块类决定每块贡献几层。

**练习 2**：把 `self.inplanes = planes * block.expansion` 这行删掉，ResNet-34 还能正常构建吗？

**答案**：不能正常构建。游标不前移的话，layer2 的首块会以 inplanes=64（应为 128）构造，layer2 的第二个块输入通道也对不上；更直接的是 `fc` 之前各 stage 通道错位，`_make_layer` 里 downsample 的判断与后续块的 `conv3x3(inplanes, planes)` 都会按错误的通道数建卷积，前向传播时张量无法对齐相加而报错。这行是「自动通道衔接」的核心。

**练习 3**：为什么 Bottleneck 把 stride 放在中间的 3×3 卷积（conv2）上，而不是第一层 1×1？

**答案**：1×1 卷积的每个输出像素只看一个输入位置，若它 stride=2 会直接丢弃 3/4 的输入信息；3×3 卷积 stride=2 时每个输出位置仍聚合了 2×2 邻域的信息，降采样更「有依据」。这也是现代实现的通行做法（早期 Caffe 版本曾放在 conv1 上，后修正）。

## 5. 综合实践

**任务：形状追踪 + 三种写法任选其一，搭建并训练自己的迷你 ResNet。**

综合本讲全部内容，分两步完成（即讲义规格中的实践任务）：

1. **形状追踪与 downsample 解释**（对应实践 B）：
   - 运行 `cnn-resnet18-mnist.ipynb` 的模型定义，用 forward hook 打印 stem 与 layer1~layer4 的输出形状；
   - 手工推导对照（28→14→7→7→4→2→1）；
   - 写一段话回答：「downsample 在哪些块被触发？为什么 layer1 的块不需要？」（答案要点：空间 stride≠1 或通道 inplanes≠planes×expansion 时触发；layer1 两者都不满足。）

2. **在 resnet-ex-1 中任选一种残差块写法，搭建小型网络并训练**：
   - 推荐 helper 类写法（最接近标准 ResNet 风格）：复制 `ResidualBlock`，把 `ConvNet` 改为三个残差块（通道如 `[1,4,8] → [8,16,32] → [32,64,128]`，输出 4×4×128），分类头改为 `Linear(4*4*128, 10)`；
   - 复用 notebook 现成的数据单元与训练循环（改好 `device`），训练 10 个 epoch；
   - 记录测试准确率，并与 notebook 中两块版本（97.91%）、ResNet-18（99.06%）对比；
   - 可选挑战：给中间的块改成 identity 形态（去掉 stride=2，主路径用 padding 保持 7×7），观察「不降采样只加深度」在 MNIST 上的效果差异。

**预期结果**：迷你 ResNet 应能稳定收敛到 97%~99% 区间；更深的三块版本相对两块未必提升——MNIST 太小，深度红利有限，重要的是你完整走通了「设计块 → 组装 → 训练 → 对比」的闭环。具体数值**待本地验证**。

## 6. 本讲小结

- **残差块的本质**是把学习目标从 \( H(x) \) 改写为 \( H(x) = F(x) + x \)：恒等映射变成「把 F 推向 0」这一最易学的解，捷径中的常数 1 让梯度直传浅层，深网络因此可训。
- **三种写法循序渐进**：identity 块（捷径直连，要求形状不变）→ convolutional 块（捷径配 1×1 Conv+BN 对齐尺寸）→ helper 类封装（块抽象成 `ResidualBlock`，网络按配置组装），resnet-ex-1 用三段等价/递进的代码演示了这一演化。
- **相加前不过 ReLU、捷径上不过 ReLU**：保证残差结构的线性恒等通路，两处源码都以注释明确强调。
- **BasicBlock 用可选的 `downsample` 统一两种形态**：传了是 conv 块，不传是 identity 块；stride 放在块内第一个 3×3 卷积上，捷径的 1×1 卷积与之同步。
- **`_make_layer` + `self.inplanes` 游标实现自动组装**：downsample 的触发条件是「stride≠1 **或** inplanes≠planes×expansion」；ResNet-18/34/50 共享同一套骨架，仅 `block` 类与 `layers` 配置不同。
- **BasicBlock（3×3×2，expansion=1）与 Bottleneck（1×1→3×3→1×1，expansion=4）**分别服务浅/深网络；层数命名 = stem + Σblocks×每块层数 + fc。阅读时以代码为准（resnet50 notebook 里的函数名和 docstring 就留着未清理的旧名字）。

## 7. 下一步学习建议

- **下一讲（u3-l5）**将学习 DenseNet 与 MobileNet：DenseNet 把 ResNet 的「相加」改为「通道拼接」，密集连接更进一步；MobileNet 的倒残差块恰好与 Bottleneck 的「两端宽中间窄」相反（中间宽两端窄），对照学习会非常有趣。
- **延伸阅读源码**：`pytorch_ipynb/cnn/cnn-resnet34-mnist.ipynb` 与 ResNet-18 的模型代码逐行相同，适合做 diff 式对照；README 中还有 ResNet-18/34 在 CIFAR-10 上的版本（`cnn-resnet18-cifar10.ipynb` 等，本次未精读），可自行迁移本讲的形状推导（32×32 输入时 layer4 输出为 2×2，`avgpool` 是否该启用值得思考）。
- **回头看**：如果对梯度如何「沿捷径直传」没有实感，回到 u2-l5 的手工反向传播，把 \( x_{l+1} = F(x_l) + x_l \) 的雅可比写出来，亲自算一遍 \( \partial L/\partial x_l \)，会比看十遍公式更有效。
