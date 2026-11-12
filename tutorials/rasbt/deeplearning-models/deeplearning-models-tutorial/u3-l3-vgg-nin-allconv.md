# 更深的网络:VGG、NiN 与 All-Conv

> **关于行号的说明**:本仓库的源码是 Jupyter Notebook(`.ipynb`,本质是 JSON 文件)。文中引用的行号(如 `L269`)指向 `.ipynb` 文件的**原始 JSON 行号**。点击永久链接后,GitHub 会把 notebook 渲染成 cell 视图,你可以在渲染页中按行号定位到对应的源码行,或切换到 "Code" 模式查看原始 JSON。

## 1. 本讲目标

上一讲我们读完了 LeNet-5 与 AlexNet,掌握了 `nn.Sequential` 堆叠式模型定义和特征图尺寸链推导。本讲沿着架构演进史继续向前,读完本讲你应该能够:

1. 说清 VGG「小卷积核 + 深堆叠」的设计哲学,并能推导 5 个卷积 block 的尺寸链与参数量;
2. 理解 NiN(Network in Network)用 1x1 卷积做「逐像素全连接」、用全局平均池化取代巨型全连接头的动机;
3. 理解 All-Conv 用 stride=2 卷积取代池化层的思路;
4. **亲手验证**「全连接层 ↔ 卷积层」的等价转换——这是打通前三者的钥匙,也是理解「为什么 NiN 的分类头几乎不占参数」的数学基础。

## 2. 前置知识

本讲默认你已掌握 u3-l1 与 u3-l2 的内容。先补充几个新概念:

- **感受野(receptive field)**:输出特征图上一个像素「看到」的输入图像区域大小。堆叠两个 kernel 为 3×3、stride 为 1 的卷积层,感受野是 5×5;堆叠三层是 7×7。计算方式是逐层叠加:每过一层,感受野按 `k - 1` 增长(相邻两次下采样之间还有放大效应,本讲只用到 stride=1 相邻堆叠的情形)。

- **小核堆叠 vs 大核**:一次 7×7 卷积的参数量是 \(49C^2\)(C 为输入输出通道数,下同),而三层 3×3 卷积是 \(3 \times 9C^2 = 27C^2\),少了约 44%,感受野相同,还多出两次非线性变换。这是 VGG 的核心论点。

- **1x1 卷积(pointwise convolution)**:kernel 大小为 1×1 的卷积。它在**每个空间位置上**对所有输入通道做一次线性组合——数学上就是一次全连接(仅跨通道、不跨空间)。参数量只有 \(C_{in} \times C_{out} + C_{out}\)。

- **全局平均池化(global average pooling, GAP)**:把每个通道的整张特征图(如 8×8)直接求平均,输出 1×1。若最后特征图通道数恰好等于类别数,GAP 之后每个通道的均值就直接当作该类的 logit,**分类头参数量为零**。

- **配置化(configured)结构**:VGG 论文用一张「通道数配置表」(如 16 层的 A 配置、19 层的 E 配置)描述整个网络:所有卷积都是 3×3,只有每个 block 的通道数和卷积个数不同。仓库里的实现也按 block_1…block_5 组织,改配置就能得到 VGG16/VGG19。

- **步长卷积下采样(strided convolution)**:卷积 stride=2 时,输出尺寸公式 \(o = \lfloor (w - k + 2p)/s \rfloor + 1\) 同样让空间减半——也就是说,**卷积本身就能做下采样**,池化层并非必需。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pytorch_ipynb/cnn/cnn-vgg16.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg16.ipynb) | VGG16 在 CIFAR-10 上的完整实现:5 个卷积 block + 3 层全连接头 |
| [pytorch_ipynb/cnn/cnn-vgg19.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg19.ipynb) | VGG19 版本(注意:类名仍叫 `VGG16`,读源码要以代码为准) |
| [pytorch_ipynb/cnn/nin-cifar10.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/nin-cifar10.ipynb) | Network in Network:1x1 卷积 + 全局平均池化分类头 |
| [pytorch_ipynb/cnn/cnn-allconv.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-allconv.ipynb) | All-Convolutional Net:7 个卷积、无池化,stride=2 下采样 |
| [pytorch_ipynb/cnn/fc-to-conv.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/fc-to-conv.ipynb) | 全连接层与卷积层等价性的最小数值实验(不需要 GPU) |

五个 notebook 都遵循「Imports → Settings/Dataset → Model → Training → Evaluation」五段式结构,训练循环沿用 u2-l2 建立的五步范式,本讲重点全部放在 **Model 段**。

## 4. 核心概念与源码讲解

### 4.1 VGG:小卷积核的堆叠哲学与配置化结构

#### 4.1.1 概念说明

2014 年的 VGG 论文(Simonyan & Zisserman)回答了一个问题:**网络为什么变深?怎么变深?** 答案是:把 AlexNet 里 11×11、5×5 的大卷积核全部换成 3×3,靠「堆叠」而不是「大核」扩大感受野。

- 两层 3×3(stride=1)感受野 = 5×5,三层 3×3 = 7×7;
- 三层 3×3 参数量 \(27C^2\) < 一层 7×7 的 \(49C^2\);
- 中间多了两次 ReLU,非线性表达能力更强。

「VGG16」「VGG19」中的数字指**带权重的层数**:VGG16 = (2+2+3+3+3) 个卷积 + 3 个全连接 = 16 层;VGG19 = (2+2+4+4+4) + 3 = 19 层。整个网络呈配置化结构:5 个卷积 block(通道数 64→128→256→512→512),每个 block 末尾一个 2×2 最大池化把空间减半,最后接 3 层全连接。

#### 4.1.2 核心流程

CIFAR-10 版本(32×32 输入)的前向流程:

```text
输入 3×32×32
block_1: [Conv3×3(→64), ReLU, Conv3×3(64→64), ReLU, MaxPool2×2]  → 64×16×16
block_2: [Conv3×3(64→128), ReLU, Conv3×3(128→128), ReLU, MaxPool] → 128×8×8
block_3: [Conv3×3(128→256) ×3, ReLU, MaxPool]                     → 256×4×4
block_4: [Conv3×3(256→512) ×3, ReLU, MaxPool]                     → 512×2×2
block_5: [Conv3×3(512→512) ×3, ReLU, MaxPool]                     → 512×1×1
flatten → 512
classifier: Linear(512→4096) → ReLU → Linear(4096→4096) → ReLU → Linear(4096→10)
```

空间尺寸链:32 → 16 → 8 → 4 → 2 → 1。注意**分类头输入维度是 512,不是原论文的 25088**——因为原论文输入是 224×224 的 ImageNet 图像,五个 block 之后剩 7×7,即 \(512 \times 7 \times 7 = 25088\)。这是「分类头与输入尺寸耦合」的直接体现,也正是 4.2 节 NiN 要解决的问题。

#### 4.1.3 源码精读

VGG16 的模型定义整体在一个 `VGG16` 类里([cnn-vgg16.ipynb:L260](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg16.ipynb#L260)),五个 block 的起始行分别是 L269、L287、L304、L328、L351。

第一个 block 展示了 VGG 的基本重复单元——「若干个 Conv3×3 + ReLU,末尾 MaxPool」:

- [cnn-vgg16.ipynb:L269-L285](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg16.ipynb#L269-L285) — `block_1`:输入通道 3、输出 64 的第一个 3×3 卷积(padding=1 的「same」填充保持 32×32),接 64→64 卷积与 2×2 最大池化;注释里的 `(1(32-1)- 32 + 3)/2 = 1` 正是 u3-l1 讲过的 same padding 推导 \(p = (s(o-1) - w + k)/2\)。
- [cnn-vgg16.ipynb:L287-L303](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg16.ipynb#L287-L303) — `block_2`:同样的模式,通道翻倍到 128。
- [cnn-vgg16.ipynb:L304-L350](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg16.ipynb#L304-L350) — `block_3`、`block_4`:从这里开始每个 block 有 **3 个**卷积层(256 通道、512 通道),这是 VGG16 与 LeNet/AlexNet 拉开深度差距的地方。
- [cnn-vgg16.ipynb:L351-L372](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg16.ipynb#L351-L372) — `block_5`:512 通道 × 3 层,最后一个 MaxPool 把空间压到 1×1。
- [cnn-vgg16.ipynb:L374-L382](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg16.ipynb#L374-L382) — 分类头:`Linear(512, 4096) → Linear(4096, 4096) → Linear(4096, 10)`。两层 4096 的全连接是 VGG 的标志,占了全网络绝大部分参数(仅 `Linear(4096,4096)` 一层就约 1678 万参数)。注意被注释掉的 `nn.Dropout(p=0.5)`——这个 CIFAR-10 复刻版没有启用原论文的 Dropout。
- [cnn-vgg16.ipynb:L384-L389](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg16.ipynb#L384-L389) — 初始化:遍历 `self.modules()`,对 Conv2d 和 Linear 统一做 `kaiming_uniform_`(fan_in + relu),并把偏置清零。这就是 u3-l1 讲过的 He 初始化。
- [cnn-vgg16.ipynb:L390](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg16.ipynb#L390) 与 [cnn-vgg16.ipynb:L393-L407](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg16.ipynb#L393-L407) — forward:依次过 5 个 block 后 `x.view(x.size(0), -1)` 展平,再进 classifier。被注释掉的 `#self.avgpool = nn.AdaptiveAvgPool2d((7, 7))` 暗示:若输入是 224×224,需要先用自适应池化把 7×7 的特征图接进原论文的分类头——这是 u3-l2 AlexNet 里 `AdaptiveAvgPool2d` 用法的回声。

再看 VGG19。它的结构与 VGG16 几乎逐行相同,区别只有两处:

- [cnn-vgg19.ipynb:L307-L393](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg19.ipynb#L307-L393) — `block_3`(L307)、`block_4`(L337)、`block_5`(L366)各有 **4 个**卷积层(4+4+4,加上前两个 block 各 2 个,共 16 个卷积,加 3 个全连接 = 19 层)。
- [cnn-vgg19.ipynb:L404-L413](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg19.ipynb#L404-L413) — 初始化与 VGG16 不同:不是 kaiming,而是 `m.weight.detach().normal_(0, 0.05)`(标准差 0.05 的窄高斯)。这个差异在训练日志里立刻显形:VGG19 notebook 记录的**第一个 batch 损失高达 1061.4152**,第二个 batch 才回到 2.30 附近;而 kaiming 初始化的 VGG16 首个损失只有 2.4443。深网络对初始化极其敏感,这是 u3-l1 结论的又一实证。

两个读码细节值得指出:

1. [cnn-vgg19.ipynb:L263](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg19.ipynb#L263) — VGG19 notebook 中的类名**仍然叫 `VGG16`**,只是结构扩到了 19 层。延续 u3-l2 的原则:判断模型看层数组合,不看类名。
2. [cnn-vgg16.ipynb:L191](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg16.ipynb#L191) 附近的超参区写着 `num_features = 784`(MNIST 的展平维度),但模型构造函数根本没用它——分类头输入是 512。这是从 MNIST notebook 复制改写时留下的痕迹,以代码实际使用为准。

两个 notebook 保存的训练结果(各一次运行,仅供参照,不可当作严格对比):VGG16 训练 10 epoch、CIFAR-10 测试准确率 [76.31%](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg16.ipynb#L675);VGG19 训练 20 epoch、测试准确率 [74.56%](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-vgg19.ipynb#L797)。更深的网络反而更低——在「训练不充分 + 无正则化 + 无数据增强」的设置下,深度本身并不兑现收益,这与 u3-l2 的结论一致。

#### 4.1.4 代码实践

**实践目标**:不训练,只验证 VGG16 的尺寸链与参数量。

**操作步骤**(以下为示例代码,可在仓库根目录新开一个 Jupyter cell 或独立脚本运行,只需 CPU):

```python
# 示例代码:把 cnn-vgg16.ipynb Model 段的 VGG16 类原样复制进来后执行
import torch

x = torch.zeros(1, 3, 32, 32)
for i in range(1, 6):
    x = getattr(model, f'block_{i}')(x)
    print(f'block_{i} 输出: {tuple(x.shape)}')
print('flatten 后:', x.view(x.size(0), -1).shape)

total = sum(p.numel() for p in model.parameters())
head = sum(p.numel() for p in model.classifier.parameters())
print(f'总参数量: {total:,},其中分类头: {head:,}')
```

**需要观察的现象**:五个 block 的输出形状依次为 `(1,64,16,16) → (1,128,8,8) → (1,256,4,4) → (1,512,2,2) → (1,512,1,1)`;flatten 后为 `(1, 512)`,与 `Linear(512, 4096)` 精确衔接。

**预期结果**:可手工核对 block_1 的参数量:第一个卷积 \(3 \times 64 \times 3 \times 3 + 64 = 1792\),第二个 \(64 \times 64 \times 9 + 64 = 36928\),合计 38,720;分类头三层共约 \(2{,}103{,}232 + 16{,}781{,}312 + 40{,}970 \approx 18.9\mathrm{M}\) 参数,占全网络大头。**待本地验证**(具体总数以你运行的结果为准)。

#### 4.1.5 小练习与答案

**练习 1**:若把同一份 VGG16 代码用于 224×224 的 ImageNet 输入,`x.view(x.size(0), -1)` 之后特征维度是多少?为什么代码必须改?

答案:\(512 \times 7 \times 7 = 25088\)(224 经五次减半 → 7)。`Linear(512, 4096)` 的输入维度固定为 512,与 25088 不匹配会直接报错;需要改成 `Linear(25088, 4096)` 或用 `AdaptiveAvgPool2d((7,7))` 先统一尺寸——这正是代码中被注释掉的那行。

**练习 2**:三层 3×3 卷积与一层 7×7 卷积感受野相同,为什么前者参数更少、表达力还更强?

答案:参数量 \(3 \times 9C^2 = 27C^2\) 对比 \(49C^2\),少约 44%;且三层之间有两次 ReLU 非线性,能拟合更复杂的函数,而单层 7×7 仍是「一次线性 + 一次激活」。

**练习 3**:数一数 cnn-vgg19.ipynb 中 `VGG16` 类(注意类名)的带权重层数,确认它是 VGG19。

答案:block_1/2 各 2 个卷积,block_3/4/5 各 4 个卷积,共 16 个卷积,加上 classifier 的 3 个 Linear,共 19 层。

### 4.2 NiN:1x1 卷积与全局平均池化

#### 4.2.1 概念说明

VGG 的痛点:卷积部分参数不多,分类头却是三层巨型 Linear,而且**输入尺寸被锁死**(flatten 后维度 = C×H×W,换数据集就要重算)。NiN(Network in Network, Lin et al. 2013)提出两个针对性武器:

1. **1x1 卷积 = 逐像素全连接**。在每个空间位置上,1x1 卷积把 \(C_{in}\) 个通道值线性组合成 \(C_{out}\) 个——这就是一次全连接,只是「跨通道、不跨空间、权重共享」。一串「普通卷积 + 1x1 + 1x1」构成一个 *mlpconv 块*:先聚合局部空间信息,再在每个像素上跑一个小 MLP。
2. **全局平均池化取代全连接头**。最后一个 1x1 卷积直接输出 num_classes 个通道,GAP 把每个通道的 8×8 特征图平均成一个数,这个数就是该类的 logit。分类头参数从上千万降到**接近零**,而且网络对输入尺寸不再敏感。

#### 4.2.2 核心流程

NiN 在 CIFAR-10(32×32)上的尺寸链:

```text
输入 3×32×32
mlpconv 块1: Conv5×5(→192) → 1x1(→160) → 1x1(→96) → MaxPool3×3(s2)   → 96×16×16
mlpconv 块2: Conv5×5(→192) → 1x1(→192) → 1x1(→192) → AvgPool3×3(s2)  → 192×8×8
mlpconv 块3: Conv3×3(→192) → 1x1(→192) → 1x1(→10)  → AvgPool8×8(s1)  → 10×1×1
view 成 (N, 10) → logits
```

下采样尺寸用 u3-l1 公式验证:32 经 k=3, s=2, p=1 的池化, \(o = \lfloor (32-3+2)/2 \rfloor + 1 = 16\);16 经同样池化得 \( \lfloor (16-3+2)/2 \rfloor + 1 = 8\);最后 `AvgPool2d(kernel_size=8, stride=1)` 把 8×8 平均成 1×1。

参数量对比(分类头视角):

- NiN 头:1x1 卷积 192→10,\(192 \times 10 + 10 = 1930\) 个参数;
- 若沿用 VGG 式头:flatten 出 \(192 \times 8 \times 8 = 12288\) 维再 `Linear(12288, 10)`,需要 \(12288 \times 10 + 10 = 122890\) 个参数。

相差 63 倍,而且 NiN 头不含任何与空间尺寸绑定的维度。

#### 4.2.3 源码精读

整个 NiN 就是一个 `nn.Sequential`,连名字都叫 `self.classifier`——因为卷积层一路做到了分类:

- [nin-cifar10.ipynb:L301-L305](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/nin-cifar10.ipynb#L301-L305) — `class NiN` 定义与 `self.classifier = nn.Sequential(` 的开头。与 VGG 分 features/classifier 两段不同,NiN 把所有层装进一个 Sequential。
- [nin-cifar10.ipynb:L306-L313](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/nin-cifar10.ipynb#L306-L313) — 第一个 mlpconv 块:`Conv2d(3, 192, kernel_size=5, padding=2)` 之后紧跟两个 1x1 卷积(192→160、160→96),再 MaxPool 减半、Dropout(0.5)。1x1 卷积在这里同时做了**降维**(192→160→96)。
- [nin-cifar10.ipynb:L315-L322](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/nin-cifar10.ipynb#L315-L322) — 第二个 mlpconv 块:5×5 卷积升回 192,两个 1x1,然后注意下采样换成了 `AvgPool2d(kernel_size=3, stride=2, padding=1)`(平均池化,不是最大池化),后接 Dropout。
- [nin-cifar10.ipynb:L324-L330](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/nin-cifar10.ipynb#L324-L330) — 第三个 mlpconv 块 + 分类头合二为一:3×3 卷积后,`Conv2d(192, 10, kernel_size=1)` 直接输出 **10 个通道**(每通道对应一个类别),`AvgPool2d(kernel_size=8, stride=1)` 就是全局平均池化,把 10×8×8 变成 10×1×1。
- [nin-cifar10.ipynb:L334-L338](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/nin-cifar10.ipynb#L334-L338) — forward 只有三行:过完 Sequential 后 `x.view(x.size(0), self.num_classes)` 把 (N,10,1,1) 整形为 (N,10)。没有任何 Linear 层。
- [nin-cifar10.ipynb:L234-L235](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/nin-cifar10.ipynb#L234-L235) — 数据准备:用 `torch.arange(0, 49000)` / `torch.arange(49000, 50000)` 的索引把 CIFAR-10 训练集切成训练/验证两份(基于 `Subset`,u4-l1 会专门展开)。
- 训练设置:Adam、学习率 [0.0005](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/nin-cifar10.ipynb#L193)、[100 个 epoch](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/nin-cifar10.ipynb#L195)。notebook 保存的最后一个 epoch 输出为[训练准确率 92.68%、验证准确率 80.20%,总时长 34.20 分钟](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/nin-cifar10.ipynb#L798-L806)——训练/验证差距约 12 个百分点,Dropout 之外还可以继续加正则,这留给 u8-l3 的数据增强去解决。

#### 4.2.4 代码实践

**实践目标**:逐层打印 NiN 的特征图形状,验证尺寸链与 1x1 卷积的「只改通道、不改空间」特性。

**操作步骤**(示例代码,需先执行 nin-cifar10.ipynb 的 Model 段,或把 `NiN` 类复制进来):

```python
# 示例代码
import torch
model = NiN(10).eval()
x = torch.zeros(1, 3, 32, 32)
for layer in model.classifier:
    x = layer(x)
    print(f'{layer.__class__.__name__:<12} -> {tuple(x.shape)}')
```

**需要观察的现象**:每个 `Conv2d`(包括 1x1)与 `ReLU` 只改变通道维;两个池化层把 32→16→8;最后的 `AvgPool2d` 输出 `(1, 10, 1, 1)`。

**预期结果**:输出形状序列与 4.2.2 的尺寸链一致;最后一步 `model(x)[0]` 得到 `(1, 10)` 的 logits。再运行 `sum(p.numel() for p in model.classifier[-2].parameters())` 应为 1930(那个 192→10 的 1x1 卷积)。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**:1x1 卷积 `Conv2d(192, 160, kernel_size=1)` 的参数量是多少?和 5×5 卷积 `Conv2d(3, 192, kernel_size=5)` 比呢?

答案:\(192 \times 160 + 160 = 30880\);5×5 的是 \(3 \times 192 \times 25 + 192 = 14592\)。1x1 卷积不含空间项,参数只由通道数决定。

**练习 2**:为什么说 NiN 的分类头「几乎不占参数、且与输入尺寸解耦」?

答案:头就是一个 192→10 的 1x1 卷积(1930 参数)+ 一个 kernel 等于特征图边长的平均池化(无参数)。1x1 卷积逐像素运算,池化对任意 H×W 都能工作,flatten 维度 C×H×W 不再出现。

**练习 3**:第一块末尾的 MaxPool 与第三块末尾的 AvgPool(8) 各自的 stride、kernel 是什么?分别在做什么?

答案:第一块是 `MaxPool2d(kernel_size=3, stride=2, padding=1)`,做重叠下采样(32→16);第三块是 `AvgPool2d(kernel_size=8, stride=1, padding=0)`,在 8×8 特征图上做全局平均,输出 1×1。

### 4.3 All-Conv:用步长卷积取代池化

#### 4.3.1 概念说明

All-Convolutional Net(Springenberg et al. 2014, "Striving for Simplicity")把「网络里只该有卷积」推到极致:

- **用 stride=2 的卷积代替池化层做下采样**。池化(尤其 MaxPool)没有可学习参数,且 max 操作丢弃大部分响应;步长卷积的下采样方式是学出来的,让网络自己决定怎么「压缩空间」。
- 分类头沿用 NiN 的思想:最后一个卷积输出 num_classes 个通道 + 全局平均池化。

这个 MNIST 版本只有 7 个 3×3 卷积层,没有任何池化层、没有全连接层。

#### 4.3.2 核心流程

7 个卷积层,每逢偶数层用 stride=2 下采样(MNIST 28×28 输入):

```text
conv_1: 1→4,  s1 → 28×28     conv_2: 4→4,   s2 → 14×14   (下采样)
conv_3: 4→8,  s1 → 14×14     conv_4: 8→8,   s2 → 7×7     (下采样)
conv_5: 8→16, s1 → 7×7       conv_6: 16→16, s2 → 4×4     (下采样)
conv_7: 16→10,s1 → 4×4×10
F.adaptive_avg_pool2d(out, 1) → 10×1×1 → squeeze → (N, 10)
```

注意 7→4 不是减半:由 \(o = \lfloor (7 - 3 + 2 \times 1)/2 \rfloor + 1 = \lfloor 6/2 \rfloor + 1 = 4\),步长卷积对奇数边长会「多裁一格」。

#### 4.3.3 源码精读

- [cnn-allconv.ipynb:L172-L188](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-allconv.ipynb#L172-L188) — `class ConvNet` 与 `conv_1`:标准 3×3、stride=1、padding=1 的「same」卷积,1→4 通道,28×28 不变。类内注释再次给出 same padding 公式。
- [cnn-allconv.ipynb:L189-L193](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-allconv.ipynb#L189-L193) — `conv_2`:唯一区别是 `stride=(2, 2)`,28×28→14×14。**这一行就是「卷积代替池化」的全部**。
- [cnn-allconv.ipynb:L195-L226](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-allconv.ipynb#L195-L226) — `conv_3`…`conv_7`:同样的节奏,conv_4、conv_6 也用 stride=2;`conv_7` 把通道数直接设成 `self.num_classes`(16→10),为全局平均池化分类头做准备。
- [cnn-allconv.ipynb:L229-L259](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-allconv.ipynb#L229-L259) — forward:七组「卷积 + ReLU」顺序执行,最后三行完成分类——`F.adaptive_avg_pool2d(out, 1)` 全局平均,两次 `logits.squeeze_(-1)` 原地压掉宽、高两个维度,得到 (N, 10)。与 NiN 手写 `AvgPool2d(8)` 不同,`adaptive_avg_pool2d(out, 1)` **不写死 kernel 大小**,输入是 4×4 还是 7×7 都能池化到 1×1。
- 训练与结果:Adam、lr=0.001、15 epoch([L115-L116](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-allconv.ipynb#L115-L116)),notebook 保存的 MNIST 测试准确率为 [96.42%](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-allconv.ipynb#L456)。可与 u3-l1 带池化的 cnn-basic 对照。

#### 4.3.4 代码实践

**实践目标**:用尺寸公式手工推导 All-Conv 的形状链,再用代码核对;并预测「把 conv_2 改成 stride=1」的后果。

**操作步骤**:

1. 先在纸上用 \(o = \lfloor (w - k + 2p)/s \rfloor + 1\) 逐层算出 7 个输出形状;
2. 运行示例代码核对(复制 notebook 的 Model 段后):

```python
# 示例代码
import torch
x = torch.zeros(1, 1, 28, 28)
out = x
for i in range(1, 8):
    out = getattr(model, f'conv_{i}')(out)
    out = torch.relu(out)
    print(f'conv_{i}: {tuple(out.shape)}')
```

3. 把 `conv_2` 的定义复制一份、`stride` 改为 `(1, 1)`,重新跑一遍,记录每一层形状如何连锁变化,以及 `view`/`adaptive_avg_pool2d` 是否仍能工作。

**需要观察的现象**:形状依次为 28→14→14→7→7→4→4(空间),通道 1→4→4→8→8→16→16→10;conv_2 改 stride=1 后,后续所有空间尺寸都翻倍,但 `adaptive_avg_pool2d` 依旧输出 (N,10)——全局池化头天然容忍尺寸变化。

**预期结果**:与手工推导一致。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**:conv_6 的输入是 7×7,k=3, s=2, p=1,输出是多少?为什么不是「刚好减半」?

答案:\(\lfloor (7-3+2)/2 \rfloor + 1 = 4\)。奇数边长减半会多出 0.5 个格,stride 滑窗只能取整数,结果向下取整后 +1,所以 7→4 而非 7→3 或 7→3.5。

**练习 2**:相比 MaxPool,stride=2 卷积做下采样有什么 trade-off?

答案:优点——下采样方式可学习(权重参与训练)、不硬性丢弃非最大响应;缺点——每次下采样多出一份卷积参数与计算量,且若无 padding 约束会改变尺寸公式。All-Conv 论文的实验结论是替换后精度不降,但这不是普适定律。

**练习 3**:`F.adaptive_avg_pool2d(out, 1)` 与 NiN 里的 `nn.AvgPool2d(kernel_size=8, stride=1)` 有何区别?

答案:前者是「自适应」:只要给定目标输出尺寸 1×1,任意 H×W 输入都能池化;后者把 kernel 写死为 8,要求输入恰好是 8×8(更小的输入会报错)。两者在 8×8 输入上数值等价。

### 4.4 全连接层与卷积层的等价转换

#### 4.4.1 概念说明

fc-to-conv.ipynb 是全仓库最小也最精妙的 notebook 之一:它用一个 2×2 的玩具输入,数值证明 **Linear 层可以精确替换为等价的 Conv2d 层**,有两条路线:

1. **kernel 覆盖整个输入**:把 Linear 的权重 reshape 成 `(out_ch, in_ch, H, W)` 的卷积核。这解释了卷积网络末端「特征图 → Linear」的衔接本质;
2. **1x1 卷积**:把输入 reshape 成 `(N, C, 1, 1)`,Linear 权重 reshape 成 `(out_ch, C, 1, 1)`。这**正是 NiN 用 1x1 卷积替代全连接的数学依据**——4.2 节的「逐像素全连接」在这里得到严格验证。

#### 4.4.2 核心流程

设全连接层为 \(y = Wx + b\),\(W \in \mathbb{R}^{2 \times 4}\)。两条转换路线的对应关系:

\[ \text{Linear: } W(2,4) \;\Longleftrightarrow\; \text{Conv2d 核 } (2, 1, 2, 2) \text{,kernel = 输入尺寸} \]

\[ \text{Linear: } W(2,4) \;\Longleftrightarrow\; \text{Conv2d 核 } (2, 4, 1, 1) \text{,输入 reshape 为 } (1,4,1,1) \]

验证方式:给 Linear 与 Conv2d **手工填同一组权重**,喂同一输入,输出应逐位相等。

#### 4.4.3 源码精读

- [fc-to-conv.ipynb:L62-L64](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/fc-to-conv.ipynb#L62-L64) — 构造 2×2 玩具输入 `[[1., 2.], [3., 4.]]`,形状 (1,1,2,2)。
- [fc-to-conv.ipynb:L88-L94](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/fc-to-conv.ipynb#L88-L94) — 定义 `Linear(4, 2)` 并手工填入权重与偏置。
- [fc-to-conv.ipynb:L114](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/fc-to-conv.ipynb#L114) — 全连接路线:`fc(inputs.view(-1, 4))` 后接 ReLU。手工核算第一个输出:\(1.1 \times 1 + 1.2 \times 2 + 1.3 \times 3 + 1.4 \times 4 + 1.9 = 14.9\),第二个:\(1.5 \times 1 + 1.6 \times 2 + 1.7 \times 3 + 1.8 \times 4 + 2.0 = 19.0\)。
- [fc-to-conv.ipynb:L153-L158](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/fc-to-conv.ipynb#L153-L158) 与 [fc-to-conv.ipynb:L166-L167](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/fc-to-conv.ipynb#L166-L167) — 路线一:创建 `Conv2d(1, 2, kernel_size=(2,2))`(kernel 尺寸直接取自输入的形状),再把 Linear 权重 `view(2, 1, 2, 2)` 塞进卷积核。
- [fc-to-conv.ipynb:L189](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/fc-to-conv.ipynb#L189) — `torch.relu(conv(inputs))` 输出 `[[[14.9]], [[19.0]]]`,与 Linear 路线逐位相等——只是形状多了两个 1×1 的空间维。
- [fc-to-conv.ipynb:L232-L238](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/fc-to-conv.ipynb#L232-L238) — 路线二:输入 reshape 成 `(1, 4, 1, 1)`(把 4 个「特征」当作 4 个「通道」),`Conv2d(4, 2, kernel_size=(1,1))` + 权重 `view(2, 4, 1, 1)`,输出同样是 `14.9 / 19.0`。**这一行就是 NiN 分类头的原型**。

一个关键的认识:等价性是「存在权重配置使两者相同」,不是「随机初始化也相同」。两层各自随机初始化后输出必然不同——所以 notebook 必须手工复制权重,这也是本讲综合实践的做法。

#### 4.4.4 代码实践

**实践目标**:复现 notebook 的三路线等价验证(此 notebook 极轻量,无需 GPU、无需数据集,是全仓库少数可以「精确复现保存输出」的实践)。

**操作步骤**:

1. 打开 fc-to-conv.ipynb,从头到尾执行(或对照其保存的输出阅读);
2. 手工计算两个输出 14.9 与 19.0(见 4.4.3 的算式);
3. 推广实验(示例代码):

```python
# 示例代码:验证 Linear 与 1x1 卷积在随机权重下也可互换
import torch
lin = torch.nn.Linear(192, 10)
conv = torch.nn.Conv2d(192, 10, kernel_size=1)
conv.weight.data = lin.weight.data.view(10, 192, 1, 1)   # 复制权重
conv.bias.data = lin.bias.data

x = torch.randn(1, 192, 8, 8)
a = lin(x.mean(dim=(2, 3)))            # 先全局平均,再 Linear
b = conv(x).mean(dim=(2, 3))           # 先 1x1 卷积,再全局平均
print(torch.allclose(a, b, atol=1e-6)) # 两者应相等
```

**需要观察的现象**:三路线输出 `14.9000` 与 `19.0000` 完全一致;推广实验打印 `True`。

**预期结果**:第 3 步为 True 的原因:平均是线性算子,\(W \cdot \text{mean}(x) = \text{mean}(W x)\),所以「GAP 后接 Linear」与「1x1 卷积后接 GAP」可以精确互换——NiN 的分类头因此等价于「对 192 维 GAP 特征做一次 Linear」。**待本地验证**(第 1、2 步结果可离线手工确认)。

#### 4.4.5 小练习与答案

**练习 1**:为什么等价性验证必须手工设置权重,而不能让两层各自随机初始化后比较输出?

答案:等价的含义是「存在相同的权重配置」。随机初始化的两层权重几乎必然不同,输出也就不同;手工 `view + data 复制` 之后两者才是同一个线性映射的两种实现。

**练习 2**:把 `Linear(25088, 4096)`(VGG 在 224×224 下的第一层分类头)换成等价卷积层,应该怎么写?参数量变化多少?

答案:`Conv2d(512, 4096, kernel_size=(7,7))`,权重 `view(4096, 512, 7, 7)`。参数量完全相同(\(512 \times 4096 \times 49\) + 偏置),但卷积版可以接受任意 ≥7×7 的输入尺寸,对每个位置滑窗产出特征图。

**练习 3**:NiN 的 `Conv2d(192, 10, kernel_size=1)` 与「对 GAP 后的 192 维向量做 `Linear(192, 10)`」是什么关系?

答案:精确等价(见 4.4.4 第 3 步的线性交换论证),两者都是 \(192 \times 10 + 10 = 1930\) 个参数;差别只在组织方式——前者保留空间维并与 GAP 组合,后者先坍缩空间。

## 5. 综合实践

**任务:把 NiN 的分类头在「卷积形式」与「等价 Linear 形式」之间互换,并用同一输入验证输出一致**(对应大纲实践任务:「仿照 fc-to-conv.ipynb 把 nin-cifar10 的分类头换成等价的卷积形式,用同一输入验证两种形式的输出一致」)。

NiN 原版分类头是「1x1 卷积(192→10) + AvgPool(8)」;我们要构造它的 Linear 孪生兄弟「GAP + Linear(192, 10)」,证明两者可互换,并顺带量化它比 VGG 式巨型全连接头省多少参数。

完整示例代码(独立可运行,不需要数据集):

```python
# 示例代码
import torch
import torch.nn as nn

torch.manual_seed(1)

# ---------- 版本 A:NiN 原版头(Conv 形式) ----------
head_conv = nn.Sequential(
    nn.Conv2d(192, 10, kernel_size=1),   # 对应 nin-cifar10.ipynb L328
    nn.ReLU(inplace=True),
    nn.AvgPool2d(kernel_size=8, stride=1),  # 对应 L330
)

# ---------- 版本 B:等价 Linear 形式 ----------
# 先 GAP 到 (N,192),再 ReLU,再 Linear(192,10)
gap = nn.AvgPool2d(kernel_size=8, stride=1)
head_linear = nn.Linear(192, 10)

# ---------- 关键一步:复制权重,仿照 fc-to-conv.ipynb ----------
with torch.no_grad():
    head_linear.weight.copy_(head_conv[0].weight.view(10, 192))
    head_linear.bias.copy_(head_conv[0].bias)

x = torch.randn(2, 192, 8, 8)          # 模拟 NiN 第三个 3x3 卷积后的特征图

out_a = head_conv(x).view(x.size(0), 10)      # (N,10,1,1) -> (N,10)
out_b = head_linear(gap(x).view(x.size(0), 192))

print('卷积头输出:', out_a[0])
print('线性头输出:', out_b[0])
print('最大绝对差:', (out_a - out_b).abs().max().item())
assert torch.allclose(out_a, out_b, atol=1e-5), '两个头不等价!'

# ---------- 参数量对比 ----------
p_conv   = sum(p.numel() for p in head_conv.parameters())
p_linear = sum(p.numel() for p in head_linear.parameters())
p_fc     = 8 * 8 * 192 * 10 + 10     # VGG 式:flatten(12288) -> Linear(12288,10)
print(f'NiN 卷积头: {p_conv:,} | 等价 Linear 头: {p_linear:,} | VGG 式 FC 头: {p_fc:,}')
```

**要求完成的三件事**:

1. **验证等价**:断言通过,`最大绝对差` 应在 1e-5 量级(浮点顺序不同会引入微小误差)。若不通过,检查权重复制的 `view` 方向是否为 `(10, 192)`——`Linear` 的权重形状是 `(out, in)`,正好与 1x1 卷积核 `(10, 192, 1, 1)` 去掉末尾两个 1 对应。
2. **解释等价**:用一句话说明为什么 ReLU 不会破坏等价性(提示:ReLU 逐元素作用于每个位置,先 ReLU 再平均 = 先平均再 ReLU)。
3. **改造 NiN**:把 nin-cifar10.ipynb 的 Model 段复制到新 notebook,将 `nn.Conv2d(192, 10, kernel_size=1)` 与 `nn.AvgPool2d(kernel_size=8, stride=1)` 替换为「`nn.AvgPool2d(8)` → flatten → `nn.Linear(192, 10)`」(权重按上述方式迁移),对同一随机输入 `(1, 3, 32, 32)` 比较两种 NiN 的 logits 是否一致。

**预期结果**:版本 A 与 B 输出差值近似为 0;参数量打印 `NiN 卷积头: 1,930 | 等价 Linear 头: 1,930 | VGG 式 FC 头: 122,890`。第 3 步若迁移正确,两种 NiN 对同一输入的 logits 也应一致(注意 `ReLU(inplace=True)` 与 flatten 的先后顺序要保持与原版相同)。完整训练对照(两种头各训几个 epoch,观察准确率是否接近)**待本地验证**。

## 6. 本讲小结

- **VGG**:用 3×3 小核深堆叠代替大核——三层 3×3 的感受野等于一层 7×7,参数却少约 44%,还多两次非线性;实现按 block_1…block_5 配置化组织,通道 64→128→256→512→512,每个 block 末尾池化减半。分类头是三层巨型 Linear,**输入尺寸被 flatten 维度锁死**。
- **读码警觉**:cnn-vgg19.ipynb 的类名仍叫 `VGG16`,层数(16 conv + 3 FC = 19)才是身份;cnn-vgg16.ipynb 超参区的 `num_features = 784` 是未使用的复制残留。以代码实际行为为准。
- **初始化的影响立竿见影**:同构网络,窄高斯初始化(VGG19)首个 batch 损失 1061,kaiming 初始化(VGG16)只有 2.44——深度放大了初始化的重要性。
- **NiN**:1x1 卷积即「逐像素全连接」,负责跨通道融合与升降维;1x1 卷积直接输出类别数通道 + 全局平均池化,把分类头参数从十几万(VGG 式)降到 1930,并解除输入尺寸耦合。
- **All-Conv**:stride=2 的卷积可以完全取代池化做下采样(28→14→7→4);`adaptive_avg_pool2d(out, 1)` 让分类头对任意空间尺寸自适应。
- **fc-to-conv 的等价洞察**:`Linear(C_in, C_out)` ≡ kernel 覆盖全图的 `Conv2d` ≡ 作用于 `(N, C_in, 1, 1)` 的 1x1 `Conv2d`;又因平均与线性可交换,「GAP → Linear」≡「1x1 Conv → GAP」。这是 NiN/GAP 分类头的数学根基。

## 7. 下一步学习建议

下一讲 **u3-l4「残差网络 ResNet 与残差块」** 将回答本讲留下的问题:VGG 把网络堆到 19 层收益甚微,再深甚至退化,ResNet 用「跳跃连接(identity shortcut)」让几十层、上百层成为可能。建议带着两个问题去读:

1. VGG19 在该仓库的实验里并不比 VGG16 好,深网络训练难在哪里?(梯度传播路径变长)
2. ResNet 的 `BasicBlock` 与 `_make_layer` 如何把「堆叠」工程化,让 18/34/50 层共享同一套代码?

延伸阅读:想看 GAP + 1x1 卷积头在现代轻量网络中的延续,可提前浏览 [cnn-densenet121-cifar10.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-densenet121-cifar10.ipynb) 与 [cnn-mobilenet-v2-cifar10.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-mobilenet-v2-cifar10.ipynb)(u3-l5 的主角);想看等价转换在迁移学习中的应用,可回看 u3-l2 的 `AdaptiveAvgPool2d` 讨论。
