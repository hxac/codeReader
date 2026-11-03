# 现代高效架构：DenseNet 与 MobileNet

> 所属单元：u3 卷积神经网络家族 ｜ 前置讲义：u3-l4 残差网络 ResNet 与残差块

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 DenseNet 与 ResNet 在「跨层连接」上的本质区别：**相加（add） vs 拼接（concatenate）**。
2. 读懂并手推 DenseNet-121 的通道数演化：`growth_rate` 如何让每个稠密层只「贡献」32 个新通道，`_Transition` 又如何把通道压缩一半。
3. 理解**深度可分离卷积**（depthwise + pointwise）为什么能把卷积的计算量压缩大约一个数量级。
4. 理解**倒残差块**（inverted residual）「两头窄、中间宽」的设计，以及它与 ResNet 瓶颈块正好相反的原因。
5. 会用 `p.numel()` 或 `torchinfo` 统计模型参数量，并用「准确率 / 参数量」来比较模型效率。
6. 养成一个读源码的习惯：**以代码与变量来源为准**——我们会在本讲看到一个真实 notebook 里「模型被意外覆盖、最终评估单元格报错」的案例。

## 2. 前置知识

- **残差连接（u3-l4）**：ResNet 的跳跃连接把前一层的特征与变换结果**逐元素相加**，要求两条路径的通道数与空间尺寸完全一致（不一致时用 1×1 卷积 `downsample` 整形）。DenseNet 正是与它对照提出的。
- **全局平均池化（u3-l3）**：NiN 一讲已经证明「GAP + 分类头」可以让分类器与输入尺寸解耦。DenseNet 和 MobileNet 的结尾都用 `F.adaptive_avg_pool2d`（或等价结构）把特征图压成一个向量。
- **BatchNorm 与 train/eval 双模式（u2-l4）**：本讲的两种网络里 BN 无处不在，卷积层普遍 `bias=False`——因为 BN 的可学习偏移量让卷积偏置变得冗余（notebook 源码里就有这条注释）。
- **`torch.hub`**：PyTorch 官方的模型分发机制。`torch.hub.load('pytorch/vision:v0.9.0', 'mobilenet_v2', pretrained=False)` 会从 pytorch/vision 仓库的 v0.9.0 标签拉取构建代码，实例化一个**随机初始化**（不加载 ImageNet 权重）的模型。本仓库的 MobileNet notebook 就是这么拿到架构的，而不是手写类。
- **仓库两代 notebook 风格（u1-l3）**：DenseNet notebook 是「老式」写法——模型、数据、训练循环全部内联在 notebook 里；MobileNet notebook 是「新式」写法——从上级目录导入 `helper_data` / `helper_train` 等共享模块。同一目录下两种风格并存，正好可以对照阅读。
- **参数量统计**：`nn.Module` 的每个权重都是 `torch.Tensor`，`p.numel()` 返回元素个数；把 `model.parameters()` 全部求和就是总参数量。这是衡量模型大小最直接的指标。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用法 |
| --- | --- | --- |
| [pytorch_ipynb/cnn/cnn-densenet121-mnist.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-densenet121-mnist.ipynb) | 在 MNIST 上手写实现 DenseNet-121（老式风格，模型类完整内联） | 4.1 的主要精读对象 |
| [pytorch_ipynb/cnn/cnn-densenet121-cifar10.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-densenet121-cifar10.ipynb) | 同一模型换到 CIFAR-10（`GRAYSCALE=False`、无增强） | 4.1 的对照实验与结果 |
| [pytorch_ipynb/cnn/cnn-mobilenet-v2-cifar10.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-mobilenet-v2-cifar10.ipynb) | 用 `torch.hub` 加载 MobileNet-v2 训练 CIFAR-10（新式 helper 风格） | 4.2 / 4.3 的主要精读对象 |
| [pytorch_ipynb/cnn/cnn-mobilenet-v3-small-cifar10.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-mobilenet-v3-small-cifar10.ipynb) | v3-small 版本；内含一个值得深挖的「模型覆盖」问题 | 4.3 的案例分析与修复实践 |
| [pytorch_ipynb/helper_data.py](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_data.py) | `get_dataloaders_cifar10` 等数据工具（u1-l3 已精读） | 4.2 数据管线回顾 |
| [pytorch_ipynb/helper_train.py](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_train.py) | `train_classifier_simple_v2` 训练循环（u1-l3 已精读） | 4.2 训练流程回顾 |

> 说明：`.ipynb` 的永久链接按 GitHub 的行号锚点定位到 **JSON 原始文件的行**，与 notebook 渲染视图的「单元格序号」不同。本讲引用时同时标注两者，便于你在渲染视图和原始 JSON 之间切换对照。本讲引用的 DenseNet 代码在 MNIST 与 CIFAR-10 两份 notebook 中完全相同，行号以 MNIST 版为准，CIFAR-10 版差异处单独标注。

## 4. 核心概念与源码讲解

### 4.1 DenseBlock 与 TransitionLayer：DenseNet 的密集连接

#### 4.1.1 概念说明

上一讲的 ResNet 把第 \(\ell\) 层的输出写成「变换 + 恒等」：

\[\mathbf{x}_{\ell} = H_{\ell}(\mathbf{x}_{\ell-1}) + \mathbf{x}_{\ell-1}\]

相加要求两条路径**形状完全一致**，所以每个残差块只连「隔一层」的捷径。DenseNet 更激进：块内**每一层都接收前面所有层的特征图**，在通道维度上**拼接**（concatenate）而不是相加：

\[\mathbf{x}_{\ell} = H_{\ell}\left([\mathbf{x}_{0}, \mathbf{x}_{1}, \ldots, \mathbf{x}_{\ell-1}]\right)\]

其中 \([\cdot]\) 表示按通道拼接。这个设计带来两个直接后果：

1. **特征复用**：第 \(\ell\) 层的输入通道数是 \(C_{in} + \ell \times k\)，其中 \(k\) 就是 `growth_rate`（增长率）。每层只需为新特征「贡献」\(k\) 个通道，其余全部是前人成果的免费复用。DenseNet-121 取 \(k=32\)，小得惊人。
2. **拼接不受形状约束**：拼接不要求相等，只要求空间尺寸一致（稠密块内所有卷积都是 `stride=1` 且同名填充，空间尺寸天然不变），因此不需要 ResNet 那套 `downsample` 整形逻辑。

那通道数岂不是越拼越多？这正是 `_Transition` 存在的原因：每个稠密块结束后，用 1×1 卷积把通道**压缩一半**（论文里的压缩系数 \(\theta = 0.5\)，这也是 "Densenet-BC" 中 C 的含义），再用 2×2 平均池化把空间减半。**块内自由生长、块间统一瘦身**，这就是 DenseNet 的节奏。

#### 4.1.2 核心流程

DenseNet-121 的整体结构（`growth_rate=32`，`block_config=(6, 12, 24, 16)`，`num_init_featuremaps=64`，`bn_size=4`）：

```
输入 (C=1 灰度 / 3 彩色, 32×32)
  └─ conv0: 7×7, stride=2, 64ch + BN + ReLU + MaxPool 3×3 stride=2   → 64ch, 8×8
  ├─ denseblock1 (6 层):  每层 +32 → 64 + 6×32  = 256
  └─ transition1: 1×1 卷积 256→128 + AvgPool2×2                    → 128ch, 4×4
  ├─ denseblock2 (12 层): 128 + 12×32 = 512
  └─ transition2: 512→256 + AvgPool                                  → 256ch, 2×2
  ├─ denseblock3 (24 层): 256 + 24×32 = 1024
  └─ transition3: 1024→512 + AvgPool                                 → 512ch, 1×1
  ├─ denseblock4 (16 层): 512 + 16×32 = 1024   （最后一个块后没有 transition）
  └─ norm5 + ReLU + 自适应全局平均池化 → 1024 维向量 → Linear(1024, 10)
```

三个关键子流程：

- **单个 `_DenseLayer`（预激活 + 瓶颈）**：`BN→ReLU→Conv1×1`（把 \(C_{in}\) 压到 `bn_size×k = 4×32 = 128` 维瓶颈）→ `BN→ReLU→Conv3×3`（产出 \(k=32\) 个新通道）。两次卷积都不带偏置。
- **`_DenseBlock` 前向**：维护一个特征列表 `features = [init_features]`，依次把**整个列表**拆包喂给每一层（`layer(*features)`），再把该层输出 append 进列表；块结束后 `torch.cat(features, 1)` 一次性拼接。拼接发生在「进入每一层之前」。
- **通道记账**：`num_features = num_features + num_layers * growth_rate`（块内生长），`num_features = num_features // 2`（过渡压缩），这两个表达式循环 `block_config` 四次，最后的 `num_features`（1024）正好等于分类器的输入维度。

「121」的由来：带权重的层数 = 1（conv0）+ 2×(6+12+24+16)（每个稠密层 2 个卷积）+ 3（三个 transition 的 1×1 卷积）+ 1（分类器）= 121。

#### 4.1.3 源码精读

**(a) 拼接发生在哪里：`_bn_function_factory`**

[pytorch_ipynb/cnn/cnn-densenet121-mnist.ipynb:L396-L402](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-densenet121-mnist.ipynb#L396-L402)

```python
def _bn_function_factory(norm, relu, conv):
    def bn_function(*inputs):
        concated_features = torch.cat(inputs, 1)
        bottleneck_output = conv(relu(norm(concated_features)))
        return bottleneck_output
    return bn_function
```

这个工厂函数把「拼接 + 预激活瓶颈」打包成闭包：`torch.cat(inputs, 1)` 在通道维（dim=1，NCHW 约定，见 u3-l1）把前序所有特征图拼起来，随后 `BN→ReLU→1×1卷积`。之所以做成闭包，是因为它还要被可选的梯度检查点（`cp.checkpoint`）整体包裹——那是 u8-l4 的主题，这里只需知道默认路径不启用。

**(b) 稠密层：`_DenseLayer`**

[pytorch_ipynb/cnn/cnn-densenet121-mnist.ipynb:L405-L433](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-densenet121-mnist.ipynb#L405-L433)

```python
class _DenseLayer(nn.Sequential):
    def __init__(self, num_input_features, growth_rate, bn_size, drop_rate, ...):
        ...
        self.add_module('conv1', nn.Conv2d(num_input_features, bn_size * growth_rate,
                                           kernel_size=1, stride=1, bias=False)),
        ...
        self.add_module('conv2', nn.Conv2d(bn_size * growth_rate, growth_rate,
                                           kernel_size=3, stride=1, padding=1,
                                           bias=False)),
        ...

    def forward(self, *prev_features):
        bn_function = _bn_function_factory(self.norm1, self.relu1, self.conv1)
        ...
        new_features = self.conv2(self.relu2(self.norm2(bottleneck_output)))
        ...
        return new_features
```

注意 `forward(self, *prev_features)` 的星号参数——它接收的是**任意多个**前序特征图，而不是单个张量。这正是稠密连接在函数签名上的体现。1×1 卷积把 `num_input_features` 通道压到 `bn_size×growth_rate`（瓶颈），3×3 卷积再产出 `growth_rate` 个新通道。`bias=False` 的原因写在模型类的注释里：使用 BN 时卷积偏置是冗余的。

**(c) 稠密块：`_DenseBlock` 的列表累加**

[pytorch_ipynb/cnn/cnn-densenet121-mnist.ipynb:L434-L452](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-densenet121-mnist.ipynb#L434-L452)

```python
class _DenseBlock(nn.Module):
    def __init__(self, num_layers, num_input_features, bn_size, growth_rate, ...):
        for i in range(num_layers):
            layer = _DenseLayer(
                num_input_features + i * growth_rate, ...)
            self.add_module('denselayer%d' % (i + 1), layer)

    def forward(self, init_features):
        features = [init_features]
        for name, layer in self.named_children():
            new_features = layer(*features)
            features.append(new_features)
        return torch.cat(features, 1)
```

两处值得停留：

- `num_input_features + i * growth_rate`（L439）：第 \(i\) 层（从 0 数）的输入通道数是「块入口通道 + 已生长的 \(i \times k\)」。递推公式 \(C_i = C_{in} + i \cdot k\) 就是从这行代码读出来的。
- `forward` 用 `features` 列表逐层 append，最后 `torch.cat(features, 1)`（L452）把「块输入 + 每层输出」全部拼在一起——注意列表的第一个元素是 `init_features` 本身，所以块输出通道数确实是 \(C_{in} + L \times k\)。

**(d) 过渡层：`_Transition`**

[pytorch_ipynb/cnn/cnn-densenet121-mnist.ipynb:L455-L463](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-densenet121-mnist.ipynb#L455-L463)

```python
class _Transition(nn.Sequential):
    def __init__(self, num_input_features, num_output_features):
        super(_Transition, self).__init__()
        self.add_module('norm', nn.BatchNorm2d(num_input_features))
        self.add_module('relu', nn.ReLU(inplace=True))
        self.add_module('conv', nn.Conv2d(num_input_features, num_output_features,
                                          kernel_size=1, stride=1, bias=False))
        self.add_module('pool', nn.AvgPool2d(kernel_size=2, stride=2))
```

`BN→ReLU→1×1卷积→2×2平均池化`，一个子模块干两件事：压通道（调用方传进来的 `num_output_features = num_features // 2`）和压空间（池化）。同样是预激活风格——BN/ReLU 放在卷积前面。

**(e) 总装与通道记账：`DenseNet121`**

[pytorch_ipynb/cnn/cnn-densenet121-mnist.ipynb:L481-L488](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-densenet121-mnist.ipynb#L481-L488)

```python
def __init__(self, growth_rate=32, block_config=(6, 12, 24, 16),
             num_init_featuremaps=64, bn_size=4, drop_rate=0, num_classes=1000,
             memory_efficient=False, grayscale=False):
    ...
    if grayscale:
        in_channels=1
    else:
        in_channels=3
```

默认超参就是 DenseNet-121 的标准配置；`grayscale` 开关只影响第一层卷积的输入通道数（1 或 3）——与 u3-l2 LeNet-5 的迁移手法一致，同一个类既服务 MNIST 也服务 CIFAR-10（CIFAR-10 版只是 `GRAYSCALE=False`，见 [cnn-densenet121-cifar10.ipynb:L234](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-densenet121-cifar10.ipynb#L234)）。

[pytorch_ipynb/cnn/cnn-densenet121-mnist.ipynb:L513-L525](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-densenet121-mnist.ipynb#L513-L525)

```python
self.features.add_module('denseblock%d' % (i + 1), block)
num_features = num_features + num_layers * growth_rate
if i != len(block_config) - 1:
    trans = _Transition(num_input_features=num_features,
                        num_output_features=num_features // 2)
    self.features.add_module('transition%d' % (i + 1), trans)
    num_features = num_features // 2
...
self.classifier = nn.Linear(num_features, num_classes)
```

这就是 4.1.2 流程图的直接对应物：`num_features` 像一个游标，块内 `+L×k`、过渡 `//2`，最后一个块之后不再压缩（`if i != len(block_config) - 1`）。分类器输入维度由游标的最终值自动算出（1024），所以改 `growth_rate` 或 `block_config` 时分类头会自动适配，不需要手改。

**(f) 收尾与实例化**

[pytorch_ipynb/cnn/cnn-densenet121-mnist.ipynb:L540-L545](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-densenet121-mnist.ipynb#L540-L545)：`forward` 的结尾是 `F.relu(features)` → `F.adaptive_avg_pool2d(out, (1, 1))` → `torch.flatten` → `classifier`，返回 `(logits, probas)` 二元组——与 LeNet-5/AlexNet notebook 的返回约定相同（训练时用 logits 喂 `F.cross_entropy`，见 u2-l2）。

[pytorch_ipynb/cnn/cnn-densenet121-mnist.ipynb:L564-L567](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-densenet121-mnist.ipynb#L564-L567)：

```python
model = DenseNet121(num_classes=NUM_CLASSES, grayscale=GRAYSCALE)
model.to(DEVICE)
optimizer = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE)
```

MNIST 版超参在 [L225-L234](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-densenet121-mnist.ipynb#L225-L234)：`LEARNING_RATE=0.0001`、`BATCH_SIZE=128`、`NUM_EPOCHS=10`、`GRAYSCALE=True`；CIFAR-10 版为 `LEARNING_RATE=0.001`、`NUM_EPOCHS=20`、`GRAYSCALE=False`（[L225-L227](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-densenet121-cifar10.ipynb#L225-L227)）。MNIST 输入被 `Resize((32, 32))`（[L253](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-densenet121-mnist.ipynb#L253)）放大成 32×32，让 7×7/stride-2 的 ImageNet 式主干有足够空间可下采样。

**(g) 训练结果对照**（两份 notebook 保存的输出）：

| 配置 | 数据 | 训练预算 | 测试准确率 |
| --- | --- | --- | --- |
| DenseNet-121（MNIST 版） | MNIST 32×32，无增强 | Adam 1e-4，10 epochs | **98.95%**（[输出 L864](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-densenet121-mnist.ipynb#L864)） |
| DenseNet-121（CIFAR-10 版） | CIFAR-10 32×32，仅 `ToTensor` | Adam 1e-3，20 epochs | **74.97%**（[输出 L919](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-densenet121-cifar10.ipynb#L919)） |

MNIST 上 98.95% 与 u3-l4 的 ResNet-18（99.06%）相当——再次印证「小数据上堆深度收益有限」。CIFAR-10 版只用 `transforms.ToTensor()`、无数据增强、只训 20 轮，74.97% 明显偏低；等下 MobileNet-v2 用「增强 + 64×64 + 150 轮 + 学习率调度」拿到 86.66%，**差距主要来自训练配方而不是模型优劣**——这是 u3-l2 已经强调过的「单次运行对比不可轻信」。

#### 4.1.4 代码实践

**实践目标**：验证 4.1.2 的通道数演化表，并体会 `growth_rate` 对参数量的杠杆作用。

**操作步骤**（示例代码，可在任意能 import torch 与本仓库的环境运行）：

1. 在 `pytorch_ipynb/cnn/` 目录下启动 Python / notebook，把模型定义单元格（[L378-L544](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-densenet121-mnist.ipynb#L378-L544)）复制执行，或直接运行 notebook 到模型单元格为止。
2. 逐段推特征图形状：

```python
# 示例代码
import torch
model = DenseNet121(num_classes=10, grayscale=True)
model.eval()
x = torch.randn(1, 1, 32, 32)
for name, module in model.features.named_children():
    x = module(x)
    print(f"{name:14s} -> {tuple(x.shape)}")
```

3. 手算第一个稠密层的参数量并与程序对账：

```python
# 示例代码
layer = model.features.denseblock1.denselayer1     # 第一个 _DenseLayer
for n, p in layer.named_parameters():
    print(n, tuple(p.shape), p.numel())
# 手算预期：conv1 是 1×1、64→128：64×128×1×1 = 8192
#           conv2 是 3×3、128→32：128×32×3×3 = 36864
```

4. 把 `growth_rate` 从 32 改成 12 重新实例化，统计总参数量：

```python
# 示例代码
def count_params(m):
    return sum(p.numel() for p in m.parameters())

print(count_params(DenseNet121(num_classes=10, grayscale=True)))                # k=32
print(count_params(DenseNet121(num_classes=10, grayscale=True, growth_rate=12))) # k=12
```

**需要观察的现象**：

- 第 2 步打印出的通道序列应为 `64 → 256 → 128 → 512 → 256 → 1024 → 512 → 1024`，空间序列 `8×8 → … → 1×1`，与 4.1.2 表格逐项吻合。
- 第 3 步 conv1/conv2 的 `numel` 与手算的 8192 / 36864 一致（BN 参数另算：两个 BN 各有 weight/bias，形状都是通道数）。
- 第 4 步 `k=32` 与 `k=12` 的参数量都明显小于同任务的 ResNet-18（u3-l4），且 `k=12` 更小；具体数值待本地验证（也可用 `torchinfo.summary(model, (1, 32, 32))` 交叉核对）。

**预期结果**：通道演化表成立；`growth_rate` 几乎线性地控制块内每层的「新通道预算」，因此总参数量随 \(k\) 明显变化，而网络深度（层数）完全不变。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_DenseLayer` 里的两个卷积都写 `bias=False`？

**答案**：每个卷积后面紧跟着 BatchNorm，BN 自带可学习的缩放 \(\gamma\) 与偏移 \(\beta\)，会把输入的任意常数偏移归一化掉，卷积偏置因此不起作用、纯属冗余参数。模型源码注释也写明 "bias is redundant when using batchnorm"。

**练习 2**：DenseNet-121 四个稠密块的层数是 (6, 12, 24, 16)。若把 `block_config` 改成 (6, 12, 32, 16)，分类器输入维度会变成多少？（不允许跑代码，手算）

**答案**：游标从 64 出发：64+6×32=256 → 128；128+12×32=512 → 256；256+32×32=1280 → 640；640+16×32=1152。最后没有 transition，所以 `Linear(1152, num_classes)`，即 1152。

**练习 3**：ResNet 的 `downsample`（u3-l4）在 DenseNet 里对应什么角色？为什么 DenseNet 不需要「整形捷径」？

**答案**：`downsample` 是为「相加」服务的——相加要求两侧形状逐元素一致，通道或空间变化时必须用 1×1 卷积 + 池化把捷径整形。DenseNet 用拼接代替相加，拼接只要求各张量空间尺寸相同（块内 `stride=1` + 同名填充保证了这一点），通道数不匹配反而是常态（正是「生长」的来源），因此块内不需要整形；空间与通道的调整统一交给块间的 `_Transition`。

### 4.2 深度可分离卷积：MobileNet-v2 的效率来源

#### 4.2.1 概念说明

先看仓库怎么拿到 MobileNet-v2——不是手写类，而是 `torch.hub`：

[pytorch_ipynb/cnn/cnn-mobilenet-v2-cifar10.ipynb:L231-L235](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-mobilenet-v2-cifar10.ipynb#L231-L235)

```python
model = torch.hub.load('pytorch/vision:v0.9.0', 'mobilenet_v2',
                       pretrained=False)

model.classifier[-1] = torch.nn.Linear(in_features=1280, # as in original
                                       out_features=10) # number of class labels in Cifar-10)
```

三行代码完成「取架构、换分类头」：`pretrained=False` 表示只要结构、不要 ImageNet 权重（迁移学习在 u7-l4 单独讲）；`model.classifier[-1] = ...` 用下标赋值直接替换 `nn.Sequential` 里的最后一个 `Linear`，把 1000 类的输出换成 CIFAR-10 的 10 类。这也意味着：**倒残差块、深度可分离卷积的真正实现位于 torchvision 源码**（v0.9.0 标签下的 `torchvision/models/` 目录，具体文件名随版本变化，待确认；模型的结构定义可直接在运行时用 `print(model)` 查看，见 4.3.4）。

深度可分离卷积（depthwise separable convolution）是 MobileNet 家族的效率基石。标准卷积用一个 \(D_K \times D_K \times M \times N\) 的核同时完成「空间滤波 + 跨通道混合」；深度可分离把它拆成两步：

1. **逐通道卷积（depthwise）**：每个输入通道单独配一个 \(D_K \times D_K\) 的核做空间卷积，通道之间不通信。PyTorch 里就是 `nn.Conv2d(M, M, kernel_size=D_K, groups=M)`——`groups=M`（u3-l2 讲过分组卷积，深度可分离是分组数取到最大值 `groups=输入通道数` 的特例）。
2. **逐点卷积（pointwise）**：1×1 卷积只做跨通道线性组合，把 \(M\) 通道映射到 \(N\) 通道。

计算量对比（\(D_F\) 是特征图边长）：

\[ \text{标准卷积}: \; D_K \cdot D_K \cdot M \cdot N \cdot D_F \cdot D_F \]

\[ \text{深度可分离}: \; \underbrace{D_K \cdot D_K \cdot M \cdot D_F \cdot D_F}_{\text{depthwise}} + \underbrace{M \cdot N \cdot D_F \cdot D_F}_{\text{pointwise}} \]

两者之比为：

\[ \frac{D_K \cdot D_K \cdot M \cdot D_F^2 + M \cdot N \cdot D_F^2}{D_K \cdot D_K \cdot M \cdot N \cdot D_F^2} = \frac{1}{N} + \frac{1}{D_K \cdot D_K} \]

取 \(D_K = 3\)、\(N = 256\)：\(1/256 + 1/9 \approx 0.115\)，即**计算量降到约 1/9~1/8**。参数量的下降比例完全相同。MobileNet-v2 的主干里，除第一个 3×3 标准卷积外，所有 3×3 卷积都是 depthwise，所有通道升降都靠 1×1 pointwise。

#### 4.2.2 核心流程

MobileNet-v2 notebook（新式 helper 风格）的流程：

```
1. sys.path.insert(0, "..") 导入 helper 模块            (L72-L79)
2. SETTINGS：RANDOM_SEED=123, BATCH_SIZE=128, NUM_EPOCHS=150  (L99-L101)
3. set_all_seeds(RANDOM_SEED)                            (L111)
4. 数据增强 transforms：
   训练 Resize(70,70) → RandomCrop(64,64) → ToTensor → Normalize(0.5,0.5,0.5)
   测试 Resize(70,70) → CenterCrop(64,64) → 同上          (L139-L151)
5. get_dataloaders_cifar10(validation_fraction=0.1, ...)  (L153)
6. torch.hub 加载 mobilenet_v2（pretrained=False）
   + classifier[-1] 替换为 Linear(1280, 10)               (L231-L235)
7. Adam(lr=0.01) + train_classifier_simple_v2(
     scheduler_on='valid_acc', best_model_save_path=...)  (L1179-L1190)
8. load_state_dict 最优 checkpoint → compute_accuracy     (测试 86.66%)
```

值得注意的三点：

- **输入被放大到 64×64**。notebook 注释（[cnn-mobilenet-v2-cifar10.ipynb:L136-L138](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-mobilenet-v2-cifar10.ipynb#L136-L138)）写道：保持 32×32 原始分辨率训练会快 2~3 倍，但测试准确率只有约 77%；升到 64×64 换来了 86.66%。MobileNet 主干是为 224×224 的 ImageNet 设计的，7 类下采样路径在 32×32 输入上会过早把特征图压没——这是「架构预设分辨率」与「数据实际分辨率」的匹配问题。
- **训练/测试 transform 不对称**（训练 `RandomCrop`、测试 `CenterCrop`）：增强只作用于训练集，测试必须确定性（u4-l3 会系统展开）。
- **训练预算完全不同量级**：150 epochs + Adam 0.01 + 按验证准确率调度学习率（`scheduler_on='valid_acc'`，u1-l3 精读过的 `train_classifier_simple_v2` 行为）+ 按最优验证 epoch 存 checkpoint（`best_model_save_path='mobilenet-v2-best-1.pt'`，[L1188](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-mobilenet-v2-cifar10.ipynb#L1188)）。与 DenseNet 的 20 轮无增强相比，这才是 86.66% 对 74.97% 差距的主要来源。

#### 4.2.3 源码精读

**(a) 数据管线**

[pytorch_ipynb/cnn/cnn-mobilenet-v2-cifar10.ipynb:L139-L158](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-mobilenet-v2-cifar10.ipynb#L139-L158)

```python
train_transforms = torchvision.transforms.Compose([
    torchvision.transforms.Resize((70, 70)),
    torchvision.transforms.RandomCrop((64, 64)),
    torchvision.transforms.ToTensor(),
    torchvision.transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))])

test_transforms = torchvision.transforms.Compose([
    torchvision.transforms.Resize((70, 70)),
    torchvision.transforms.CenterCrop((64, 64)),
    ...
train_loader, valid_loader, test_loader = get_dataloaders_cifar10(
    batch_size=BATCH_SIZE,
    validation_fraction=0.1,
    train_transforms=train_transforms,
    test_transforms=test_transforms,
    num_workers=2)
```

训练用 `RandomCrop` 做随机裁剪增强，测试用 `CenterCrop` 保持确定；两套 transform 经 `get_dataloaders_cifar10` 注入（该函数用 `SubsetRandomSampler` 切验证集，u4-l1 精读）。保存的输出显示 batch 形状为 `torch.Size([128, 3, 64, 64])`，确认 64×64 生效。

**(b) 训练与评估**

[pytorch_ipynb/cnn/cnn-mobilenet-v2-cifar10.ipynb:L1179-L1190](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-mobilenet-v2-cifar10.ipynb#L1179-L1190)

```python
optimizer = torch.optim.Adam(model.parameters(), lr=0.01)

minibatch_loss_list, train_acc_list, valid_acc_list = train_classifier_simple_v2(
    model=model,
    num_epochs=NUM_EPOCHS,
    ...
    best_model_save_path='mobilenet-v2-best-1.pt',
    device=DEVICE,
    scheduler_on='valid_acc',
    logging_interval=100)
```

训练循环整体外包给 helper（对照 DenseNet notebook 的内联写法，能直观看到两代风格差异）。notebook 保存的日志显示第 1 轮验证 32.60%、第 140 轮达到最优验证 87.62%（[L1147](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-mobilenet-v2-cifar10.ipynb#L1147) 附近），加载最优 checkpoint 后测试 **86.66%**（[输出 L1217](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-mobilenet-v2-cifar10.ipynb#L1217)，打印语句在 [L1225](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-mobilenet-v2-cifar10.ipynb#L1225)）。

**(c) 分类头替换的普适性**

`model.classifier[-1] = torch.nn.Linear(1280, 10)` 这个技巧对本讲四份 notebook 全部适用：torchvision 风格的模型把分类头做成 `nn.Sequential`，最后一层 `Linear` 的输出维度就是类别数。换头后，新 `Linear` 是随机初始化、其余层同样随机初始化（`pretrained=False`），整个网络从零训练。1280 这个数字就是 v2 主干末端的全局平均池化输出维度（v3-small 是 1024，见 4.3）。

#### 4.2.4 代码实践

**实践目标**：用一层张量运算亲手验证「深度可分离 = depthwise + pointwise」在参数量与输出上的等价性。

**操作步骤**（示例代码，纯 torch，无需数据集）：

```python
# 示例代码
import torch
import torch.nn as nn

torch.manual_seed(0)
x = torch.randn(1, 24, 8, 8)                    # M=24 通道的 8×8 特征图

# (1) 标准卷积 M=24 -> N=32
std = nn.Conv2d(24, 32, kernel_size=3, padding=1, bias=False)

# (2) 深度可分离：depthwise(groups=M) + pointwise(1×1)
dw  = nn.Conv2d(24, 24, kernel_size=3, padding=1, groups=24, bias=False)
pw  = nn.Conv2d(24, 32, kernel_size=1, bias=False)

print("标准卷积权重:", tuple(std.weight.shape), std.weight.numel())
print("depthwise 权重:", tuple(dw.weight.shape), dw.weight.numel())
print("pointwise 权重:", tuple(pw.weight.shape), pw.weight.numel())
print("深度可分离合计:", dw.weight.numel() + pw.weight.numel())
print("参数比:", (dw.weight.numel() + pw.weight.numel()) / std.weight.numel())
print("标准输出形状:", tuple(std(x).shape),
      "可分离输出形状:", tuple(pw(dw(x)).shape))
```

**需要观察的现象 / 预期结果**（数值可手算，必然出现）：

- `std.weight` 形状 `(32, 24, 3, 3)`，参数 \(32 \times 24 \times 9 = 6912\)；
- `dw.weight` 形状 `(24, 1, 3, 3)`（每通道一个独立 3×3 核），参数 \(24 \times 9 = 216\)；
- `pw.weight` 形状 `(32, 24, 1, 1)`，参数 \(32 \times 24 = 768\)；
- 合计 984，参数比 \(984 / 6912 \approx 0.142\)，与公式 \(1/N + 1/D_K^2 = 1/32 + 1/9 \approx 0.135\) 接近（小差异来自 \(N \ne D_K^2\)）；
- 两条路径输出形状同为 `(1, 32, 8, 8)`——深度可分离是「更便宜的近似替代」，**不是数值等价**：两组随机权重的输出数值不同，但作为「同形状的特征变换单元」可以互换使用。

**预期结果**：参数比落在 0.14 附近；把 `N` 从 32 改成 256 重跑，比值应进一步逼近 \(1/9 \approx 0.111\)（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`nn.Conv2d(24, 24, 3, groups=24)` 的 `weight.shape` 为什么是 `(24, 1, 3, 3)` 而不是 `(24, 24, 3, 3)`？

**答案**：分组卷积把输入通道分成 `groups` 组、输出通道也分成 `groups` 组，每组输出只连本组输入。权重形状约定为 `(out_channels, in_channels / groups, kH, kW)`。`groups=24` 时每组输入只有 \(24/24=1\) 个通道，所以第二维是 1——这正是 depthwise 的定义：每通道一个独立空间核。

**练习 2**：MobileNet-v2 notebook 为什么把 32×32 的 CIFAR-10 放大到 64×64 再喂给模型？代价是什么？

**答案**：该架构的主干为 ImageNet 的 224×224 设计，含多次 stride=2 下采样；32×32 输入到深层时特征图已缩到极小，有效感受野与特征容量不足，notebook 作者实测 32×32 只有约 77% 准确率。放大到 64×64 换来 86.66%，代价是约 4 倍的像素量与 2~3 倍的训练时间（notebook 注释原文）。

**练习 3**：为什么 `model.classifier[-1] = nn.Linear(1280, 10)` 只换最后一层就够了，而不是重写整个模型类？

**答案**：迁移到新数据集只需要改变「输出维度等于类别数」的那一层；主干提取的特征与类别数无关。下标赋值替换 `Sequential` 的子模块后，优化器通过 `model.parameters()` 拿到的参数集合自动更新为新头 + 原主干，无需改动任何训练代码。这是 torchvision 风格模型的标准换头手法（u7-l4 迁移学习将复用它）。

### 4.3 倒残差块与 v3 的效率设计（兼一次「读源码侦探」实践）

#### 4.3.1 概念说明

**倒残差块（inverted residual）** 是 MobileNet-v2 的基本单元。先回忆 ResNet 瓶颈块（u3-l4 的 Bottleneck）：宽 → 1×1 压窄 → 3×3 卷积 → 1×1 扩宽，形状是「**两头宽、中间窄**」，像收腰的沙漏。倒残差正好相反：

```
ResNet Bottleneck:   C_in(宽) → 1×1 压缩 → 3×3 → 1×1 扩张 → C_out(宽)   捷径：相加
MobileNet-v2 倒残差:  C(窄) → 1×1 扩张(t≈6) → 3×3 depthwise → 1×1 压缩 → C_out(窄)  捷径：相加
```

为什么反过来？关键在于 3×3 卷积的位置：

- ResNet 用**标准** 3×3 卷积，参数量 \(C_{mid} \times C_{mid} \times 9\) 随中间宽度平方增长，所以中间要窄。
- MobileNet-v2 的 3×3 是 **depthwise**，参数量只有 \(C_{mid} \times 9\)，与「宽度平方」无关——中间层放大到 \(t \times C\)（扩张比 \(t\) 常取 6）几乎不增加参数，却给了 ReLU 更高维的「工作空间」。
- 真正昂贵的是 1×1 pointwise（参数 \(C_a \times C_b\)），所以两头保持窄。1×1 压缩之后**不再接非线性**（线性瓶颈，linear bottleneck）：低维空间里的 ReLU 会丢失信息，让「投影回低维」保持线性。

倒残差块之间仍然保留 ResNet 式捷径相加（`stride=1` 且输入输出同形时），所以它叫「倒**残差**」。总结一句：**深度可分离卷积（4.2）负责让「宽中间层」变得便宜，倒残差（4.3）负责决定「在哪里宽、在哪里窄」**。

**MobileNet-v3-small** 的效率取向更进一步：块结构由神经架构搜索（NAS）挑选，引入了 hardswish 激活（ReLU6 与 x 乘积的平滑组合，比 sigmoid 类激活便宜）、通道注意力（Squeeze-and-Excitation，对全局平均池化后的通道向量学习权重再回乘各通道）、以及更省的头部设计（v2 末端 1280 维，v3-small 是 1024 维——这正是两份 notebook 换头时 `in_features` 不同的原因）。v3 的目标场景是移动端：同样准确率下参数量与延迟更低。这些结构细节同样位于 torchvision 实现，不在本仓库源码内；但**仓库 notebook 提供了加载、换头、训练、评估的完整管线**，v3-small 与 v2 只差一个模型名字符串。

#### 4.3.2 核心流程

v3-small notebook（[cnn-mobilenet-v3-small-cifar10.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-mobilenet-v3-small-cifar10.ipynb)）的设定流程与 v2 几乎逐行相同（同样的 transforms、同样的 `get_dataloaders_cifar10`、同样 150 epochs）。差异集中在模型部分——也正是本讲的「侦探现场」：

```
模型单元格 A（L231-L235）：hub 加载 mobilenet_v3_small，换头 Linear(1024, 10)
模型单元格 B（L259-L263）：hub 加载 mobilenet_v2，   换头 Linear(1280, 10)   ← 覆盖了 model！
训练单元格（L1210 附近）：  train_classifier_simple_v2(..., best_model_save_path='mobilenet-v2-best-1.pt')
评估单元格：              model.load_state_dict(torch.load('mobilenet-v2-best-1.pt')) → RuntimeError
```

从上到下顺序执行的话，训练用的是单元格 B 的 MobileNet-v2。但评估单元格加载 checkpoint 时抛出了 `RuntimeError`（4.3.3(d)），说明**保存 checkpoint 那次运行里，训练的对象并不是当前的 `model`**——单元格的实际执行顺序与文件排列顺序不一致。这正是 u3-l2 提出的原则「读 notebook 要以代码与变量来源为准，不能只看标题和排列」的活教材。

#### 4.3.3 源码精读

**(a) 加载 v3-small 并换头**

[pytorch_ipynb/cnn/cnn-mobilenet-v3-small-cifar10.ipynb:L231-L235](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-mobilenet-v3-small-cifar10.ipynb#L231-L235)

```python
model = torch.hub.load('pytorch/vision:v0.9.0', 'mobilenet_v3_small',
                       pretrained=False)

model.classifier[-1] = torch.nn.Linear(in_features=1024, # as in original
                                       out_features=10) # number of class labels in Cifar-10)
```

与 v2 notebook 唯一的实质差异：模型名 `mobilenet_v3_small`、分类头输入维 1024。

**(b) 紧随其后的「第二个模型单元格」**

[pytorch_ipynb/cnn/cnn-mobilenet-v3-small-cifar10.ipynb:L259-L263](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-mobilenet-v3-small-cifar10.ipynb#L259-L263)

```python
model = torch.hub.load('pytorch/vision:v0.9.0', 'mobilenet_v2',
                       pretrained=False)

model.classifier[-1] = torch.nn.Linear(in_features=1280, # as in original
                                       out_features=10) # number of class labels in Cifar-10)
```

这一单元格与 v2 notebook 的模型单元格**一字不差**，几乎可以断定是从 v2 notebook 复制粘贴后忘记删除的遗留物。它把变量 `model` 重新绑定成 MobileNet-v2。训练单元格保存的输出（[L1175](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-mobilenet-v3-small-cifar10.ipynb#L1175)）显示第 147 轮达到最优验证 87.32%，checkpoint 路径仍是 `'mobilenet-v2-best-1.pt'`（[L1216](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-mobilenet-v3-small-cifar10.ipynb#L1216)）——路径名同样是从 v2 版抄来的。

**(c) 训练日志佐证**

[L1175](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-mobilenet-v3-small-cifar10.ipynb#L1175) 保存的输出：`Epoch: 150/150 | Train: 98.03% | Validation: 86.52% | Best Validation (Ep. 147): 87.32%`。仅从这条日志无法分辨训练的是 v2 还是 v3（两者量级相当），需要 (d) 的报错来定案。

**(d) 决定性证据：评估单元格的报错**

[pytorch_ipynb/cnn/cnn-mobilenet-v3-small-cifar10.ipynb:L1245-L1250](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-mobilenet-v3-small-cifar10.ipynb#L1245-L1250) 保存的输出是一段 traceback，核心一行是：

```python
RuntimeError: Error(s) in loading state_dict for MobileNetV2:
	Missing key(s) in state_dict: "features.1.conv.0.0.weight", "features.1.conv.1.0.weight", ...
```

解读：`load_state_dict` 的目标模型是 MobileNetV2（单元格 B 绑定的 `model`），它期待的键名形如 `features.1.conv...`（v2 实现的子模块命名）；而 checkpoint 里**缺少**这些键——即 checkpoint 是用另一套（v3 风格的）子模块命名保存的。结论：**产生该 checkpoint 的那次训练，`model` 实际是 mobilenet_v3_small**（训练先于单元格 B 执行）；而按文件顺序重跑 notebook 的人，训练的将是 v2。同一份文件，两种执行顺序，两种模型——这就是为什么「以变量来源为准」要成为肌肉记忆。

> 顺带一提：报错能被 `load_state_dict` 精确暴露，得益于 PyTorch 对 state_dict 键名的严格匹配（默认 `strict=True`）。这是排查「我训的到底是什么模型」最可靠的手段之一。

#### 4.3.4 代码实践

**实践目标**：亲眼确认 hub 加载的 MobileNet-v2 内部确实是「1×1 扩张 → depthwise → 1×1 压缩」的堆叠，并修复 v3 notebook 的遗留问题。

**操作步骤**：

1. **检查结构**（示例代码，需要联网拉取一次 hub 缓存，之后可离线）：

```python
# 示例代码
import torch
from collections import Counter

model = torch.hub.load('pytorch/vision:v0.9.0', 'mobilenet_v2', pretrained=False)
print(model.classifier)          # Sequential(Dropout, Linear(1280, 1000))
print(type(model.features[1]).__name__)   # 倒残差块类名（ torchvision 版本相关，待本地验证）
print(Counter(type(m).__name__ for m in model.modules()))  # 统计各模块类型数量
for m in model.features[2].modules():     # 任意一个中段倒残差块
    if isinstance(m, torch.nn.Conv2d):
        print(tuple(m.weight.shape), 'groups =', m.groups)
```

2. **读权重形状找规律**：观察打印结果——单个倒残差块内应出现三组卷积：1×1（`kernel=1`，通道数从小变大）、3×3 且 `groups=通道数`（depthwise）、1×1（通道数从大变小）。
3. **修复 v3 notebook**：复制 `cnn-mobilenet-v3-small-cifar10.ipynb`，删除第二个模型单元格（L259-L263 对应的单元格），把 `best_model_save_path` 改为 `'mobilenet-v3-small-best.pt'`，`NUM_EPOCHS` 改为 10~20，从头运行。

**需要观察的现象**：

- 第 1 步的 `Counter` 里出现倒残差块类（v0.9.0 中的具体类名以本地输出为准，待本地验证）与 `Conv2d` 大量出现；
- 第 2 步同一块内三组卷积核形状呈「窄→宽（1×1）、宽→宽（3×3 depthwise）、宽→窄（1×1）」的收腰倒置模式，`groups` 恰等于该 3×3 卷积的输入通道数；
- 第 3 步修复后评估单元格不再报 `RuntimeError`，能打印出测试准确率（具体数值待本地验证）。

**预期结果**：MobileNet-v2 的参数量显著小于 DenseNet-121（第 5 节会量化）；v3 notebook 修复后训练-保存-加载链路闭环。

#### 4.3.5 小练习与答案

**练习 1**：倒残差块「中间宽」为什么几乎不增加参数量？

**答案**：中间的 3×3 卷积是 depthwise，参数量 \(C_{mid} \times 9\) 只随宽度线性增长；而把宽度从 \(C\) 扩到 \(6C\) 所增加的参数主要来自两个 1×1 pointwise（合计约 \(C \times 6C + 6C \times C = 12C^2\)），相比标准卷积里 \(6C \times 6C \times 9 = 324C^2\) 的 3×3 项便宜一个数量级以上。

**练习 2**：什么是「线性瓶颈」？为什么 1×1 压缩之后不放 ReLU？

**答案**：指倒残差块末端 1×1 投影到低维后直接输出、不再过非线性。低维空间本身已经是信息的「压缩表示」，ReLU 的零截断在低维下会不可逆地丢失信息；高维中间层放 ReLU 损失相对小（冗余维度可互相补偿）。所以非线性只放在宽的中间，窄的两端保持线性。

**练习 3**：如果你按文件顺序运行 v3 notebook，最终训练的是哪个模型？如果作者的原始执行顺序是「v3 单元格 → 训练 → v2 单元格 → 评估」呢？

**答案**：按文件顺序：变量 `model` 被第二个模型单元格覆盖为 MobileNet-v2，训练的是 v2，checkpoint 能正常加载。作者原始顺序：训练时 `model` 是 mobilenet_v3_small，checkpoint 是 v3 权重；评估时 `model` 已被覆盖为 v2，`load_state_dict` 因键名不匹配抛 `RuntimeError`——即 notebook 里保存的那段 traceback。

## 5. 综合实践：参数量—准确率效率对比

**任务**：给本讲的三个模型（DenseNet-121、MobileNet-v2、MobileNet-v3-small）建立一张「参数量 vs 准确率」对比表，并计算「测试准确率 / 参数量（百万）」效率比，最后实际训练一次 v3-small。

**步骤**：

1. **准备**：在 `pytorch_ipynb/cnn/` 目录下新建 notebook（或脚本），先执行：

```python
# 示例代码
import sys, torch
sys.path.insert(0, "..")   # 与仓库 notebook 相同的导入手法（见 v2 notebook L72）
from helper_data import get_dataloaders_cifar10
from helper_utils import set_all_seeds
```

2. **统计参数量**（DenseNet 类定义从 [cnn-densenet121-mnist.ipynb:L378-L544](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-densenet121-mnist.ipynb#L378-L544) 复制）：

```python
# 示例代码
def count_params(m): return sum(p.numel() for p in m.parameters())

densenet = DenseNet121(num_classes=10, grayscale=False)          # CIFAR-10 版
mobilenet_v2 = torch.hub.load('pytorch/vision:v0.9.0', 'mobilenet_v2', pretrained=False)
mobilenet_v2.classifier[-1] = torch.nn.Linear(1280, 10)
mobilenet_v3s = torch.hub.load('pytorch/vision:v0.9.0', 'mobilenet_v3_small', pretrained=False)
mobilenet_v3s.classifier[-1] = torch.nn.Linear(1024, 10)

for name, m in [("DenseNet-121", densenet), ("MobileNet-v2", mobilenet_v2),
                ("MobileNet-v3-small", mobilenet_v3s)]:
    print(f"{name:18s} {count_params(m)/1e6:6.2f} M")
```

3. **训练 v3-small**：按 4.3.4 第 3 步修复并运行 v3 notebook（10~20 epochs 足够观察趋势），记录最优验证准确率与测试准确率。
4. **填表计算**：

| 模型 | 参数量（M） | 测试准确率 | 测试准确率 / 参数量（%/M） | 训练配方 |
| --- | --- | --- | --- | --- |
| DenseNet-121 | 待本地验证（文献量级约 8M @1000 类，10 类头会更小） | 74.97%（notebook 记录） | 待计算 | 20 epochs，无增强，Adam 1e-3 |
| MobileNet-v2 | 待本地验证（文献量级约 3.5M @1000 类） | 86.66%（notebook 记录） | 待计算 | 150 epochs，增强，Adam 0.01+调度 |
| MobileNet-v3-small | 待本地验证 | 本次实践测得 | 待计算 | 你设定的 epochs |

5. **回答两个问题**：(a) 效率比最高的模型是哪个？(b) DenseNet 与 MobileNet 的对比里有多少差异来自架构、多少来自训练配方？如何设计一个公平对照（提示：统一 epochs、统一增强、统一学习率调度）？

**预期结果**：MobileNet 系列的参数量约为 DenseNet-121 的三分之一以下，效率比显著更高；但 DenseNet 行的准确率受「训练配方最弱」拖累，(b) 的正确答案是**当前数据不足以归因于架构本身**——需要公平对照实验（这也正是 u7-l2 交叉验证与 u8-l3 增强对照要解决的问题）。所有「待本地验证」数值以你的实际运行输出为准。

## 6. 本讲小结

- **DenseNet 用拼接、ResNet 用相加**：拼接让每层看到全部前序特征，通道数按 \(C_{in} + L \times k\) 生长，`growth_rate=32` 意味着每层只新增 32 个通道；`_Transition`（BN→ReLU→1×1→AvgPool）在每个块后把通道减半、空间减半，收放交替构成 DenseNet-121 的 (6, 12, 24, 16) 节奏。
- **预激活 + 无偏置卷积 + 记账游标** 是 DenseNet 源码的三个签名：`num_features` 游标自动算出分类头输入维度（1024），改 `growth_rate`/`block_config` 无需改头。
- **深度可分离卷积 = depthwise（`groups=C`）+ pointwise（1×1）**，计算与参数比约为 \(1/N + 1/D_K^2\)，3×3 卷积场景下约降到 1/9~1/8；它是「让宽中间层变便宜」的前提。
- **倒残差块两头窄中间宽**：1×1 扩张（t≈6）→ 3×3 depthwise → 1×1 线性压缩，捷径相加保留在窄端；v3-small 再叠加 NAS 结构、hardswish 与通道注意力，分类头输入维从 1280 降到 1024。
- **参数量用 `sum(p.numel() ...)` 统计**，「准确率/参数量」是效率的粗指标，但跨模型对比必须对齐训练配方——本讲 86.66% vs 74.97% 的差距主要是配方差异而非架构差异。
- **读 notebook 要以变量来源为准**：v3-small notebook 里第二个模型单元格把 `model` 覆盖为 v2，作者实际训练的却是 v3（评估单元格的 `load_state_dict` RuntimeError 就是证据）——标题、文件名、排列顺序都可能骗人，state_dict 键名不会。

## 7. 下一步学习建议

- **补齐数据与评估基础**：本讲 MobileNet notebook 的 `get_dataloaders_cifar10`、`train_classifier_simple_v2`、混淆矩阵等 helper 用法，将在 u4-l1（DataLoader 与验证集划分）、u4-l4（公共训练评估工具链）与 u7-l1（评估工具链）中逐个拆开精读。
- **公平对比方法论**：第 5 节综合实践引出的「训练配方不对齐」问题，正是 u7-l2（K 折交叉验证）与 u8-l3（AutoAugment/TrivialAugment 对照实验，同样的 without/with 三连 notebook 设计）要解决的主题。
- **梯度检查点前传**：DenseNet 的 `_DenseLayer` 里 `memory_efficient=True` 时用 `torch.utils.checkpoint` 包裹拼接+瓶颈计算——u8-l4 将以 NiN 为例实测「重计算换显存」。
- **迁移学习**：本讲的 `pretrained=False` + 换头只是「借结构」；u7-l4 会用 `pretrained` ImageNet 权重 + `requires_grad=False` 冻结，在 CIFAR-10 上演示「借知识」，两者对照能加深对换头技巧的理解。
- **源码延伸阅读**：把 4.3.4 打印的模型结构与 torchvision v0.9.0 的 MobileNet 实现对照（[pytorch/vision v0.9.0 models 目录](https://github.com/pytorch/vision/tree/v0.9.0/torchvision/models)，具体文件名随版本变化、待确认），找到倒残差块类与 `_make_divisible`（把通道数取整到 8 的倍数以利于硬件加速）的定义。
