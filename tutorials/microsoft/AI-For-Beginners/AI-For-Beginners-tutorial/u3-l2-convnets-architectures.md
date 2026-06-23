# 卷积神经网络 CNN 与经典架构

## 1. 本讲目标

上一讲我们用 OpenCV 把图像当 NumPy 数组做预处理，这讲我们要让神经网络「自己学会看图」。

学完本讲，你应当能够：

1. 说清**卷积（convolution）**和**池化（pooling）**这两个 CNN 最基础的操作在做什么，并能手算它们的输出尺寸。
2. 理解**感受野（receptive field）**与**参数量（parameter count）**，解释为什么 CNN 比「全连接网络」参数更少、泛化更好。
3. 串起 **LeNet → VGG → ResNet → Inception → MobileNet** 的架构演进脉络，看懂课程 Notebook 里手写的几种 CNN 结构。

本讲所有结论都将对照 `07-ConvNets` 课程的真实 Notebook 与文档逐行验证。

## 2. 前置知识

在进入 CNN 之前，请确认你已经掌握（来自前面几讲）：

- **图像即数组**：灰度图是形状 \((H,W)\) 的二维矩阵，彩色图是 \((H,W,3)\) 的三维张量（见上一讲 OpenCV）。
- **全连接层（Linear / Dense）**：上一层每个神经元都和下一层每个神经元相连，参数量巨大（见 `OwnFramework`）。
- **训练五件套**：前向算 loss → `zero_grad` → `backward` → `step`（见 `05-Frameworks`）。
- **过拟合**：参数越多、越容易背下训练集噪声（见 `05-Frameworks`）。

本讲要回答的核心问题是：**既然全连接网络也能识别 MNIST 数字，为什么处理图像必须用 CNN？** 答案就藏在「卷积 + 池化」这两个操作里。

## 3. 本讲源码地图

本讲涉及的关键文件都位于 `lessons/4-ComputerVision/07-ConvNets/` 下：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/07-ConvNets/README.md) | 课程讲义正文，讲清卷积滤波器、CNN 三大思想、金字塔结构。 |
| [CNN_Architectures.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/07-ConvNets/CNN_Architectures.md) | 经典架构专题，分别介绍 VGG / ResNet / Inception / MobileNet。 |
| [ConvNetsPyTorch.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/07-ConvNets/ConvNetsPyTorch.ipynb) | 可执行 Notebook，从手写滤波器到 `OneConv` → `MultiLayerCNN` → `LeNet` 逐步实现。 |
| [pytorchcv.py](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/07-ConvNets/pytorchcv.py) | 隐藏实现细节的辅助模块，提供 `load_mnist`、`train`、`plot_convolution` 等函数。 |
| [lab/README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/07-ConvNets/lab/README.md) | 综合实践任务：用 Oxford-IIIT Pet 数据集做宠物品种分类。 |

> 提示：课程同时提供 `ConvNetsTF.ipynb`（TensorFlow 版），思路完全一致，本讲以 PyTorch 版为准。

## 4. 核心概念与源码讲解

### 4.1 卷积与池化：从滑窗到金字塔

#### 4.1.1 概念说明

全连接网络处理图像的最大毛病是：它把图像「拉成一维长向量」后，**完全丢失了像素之间的空间相邻关系**，而且每个输入像素都要配一个权重，参数量爆炸。

CNN 的破局思路来自一个直觉：**识别物体时，我们在扫描图像寻找「局部模式」**。比如找猫，先找可能组成胡须的横线，再找胡须的组合——重要的是「某些模式出现了」，而不是「它们精确落在第几个像素」。README 里正是这样描述计算机视觉与普通分类的区别的（[README.md:7](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/07-ConvNets/README.md#L7)）。

围绕这个直觉，CNN 引入两个核心操作：

- **卷积滤波器（convolutional filter）**：一个小小的权重矩阵（叫 **kernel / 卷积核**，常见 3×3 或 5×5），像一扇小窗滑过整张图像，在每个位置计算「窗口内像素与核权重的加权求和」。一个核就是一个「模式探测器」——例如竖直边缘核在遇到竖直边缘时会产生大响应，在均匀区域则归零。
- **池化层（pooling layer）**：在卷积之后「缩小图像」。常用 **Max Pooling**（取窗口内最大值，含义是「这个模式有没有在这个区域出现」）和 **Average Pooling**（取窗口内平均值）。

更进一步，CNN 的精髓是 README 里总结的**三大思想**（[README.md:25-L31](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/07-ConvNets/README.md#L25-L31)）：

1. 卷积滤波器能提取模式；
2. 我们可以让网络**自动训练**这些滤波器，而不是手设计；
3. 同样的卷积可以作用在「上一层提取出的高级特征」上，于是特征层层组合：像素 → 笔画 → 部件 → 物体。

#### 4.1.2 核心流程

**卷积的数学定义**。对二维图像 \(I\) 与 \(k\times k\) 的核 \(K\)，无填充（no padding）、步长 1 的卷积在位置 \((i,j)\) 的输出为：

\[
(I * K)(i,j) = \sum_{m=0}^{k-1}\sum_{n=0}^{k-1} I(i+m,\,j+n)\cdot K(m,n)
\]

直觉上就是把核「盖」到图像的一小块上，逐元素相乘再求和。核每滑动一个像素产生一个输出值，因此输出尺寸会**缩小**。

**输出尺寸公式**（无填充、步长 1）：

\[
W_{\text{out}} = W_{\text{in}} - k + 1
\]

例如 MNIST 输入 \(28\times 28\)，用 \(5\times 5\) 核：\(28-5+1=24\)，输出 \(24\times 24\)。

**池化**：Max Pooling 用 \(2\times 2\) 窗口、步长 2，会让空间尺寸**减半**（\(24\to 12\)），且不引入任何可训练参数。

**一条完整的 CNN 前向流程**可以概括为：

```text
输入图像 (C_in, H, W)
   │  Conv2d: 学到 out_channels 个模式
   ▼
特征图 (C_out, H', W')
   │  ReLU 激活（层间必须夹非线性）
   ▼
   │  MaxPool2d: 空间尺寸减半
   ▼
特征图 (C_out, H'/2, W'/2)
   │  （重复若干次 卷积→ReLU→池化）
   ▼
展平 → Linear → 类别数
```

随着层数加深，空间尺寸越来越小、通道数（filter 数）越来越多，整体形状像一座下窄上宽倒过来的塔——这就是下一节要讲的「金字塔结构」。

#### 4.1.3 源码精读

**(1) 用 `plot_convolution` 直观感受手写滤波器**

Notebook 先不训练，而是手动指定两个经典的边缘检测核，看它们滑过 MNIST 数字后的效果（[ConvNetsPyTorch.ipynb:77-L78](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/07-ConvNets/ConvNetsPyTorch.ipynb#L77-L78)）：

```python
plot_convolution(torch.tensor([[-1.,0.,1.],[-1.,0.,1.],[-1.,0.,1.]]),'Vertical edge filter')
plot_convolution(torch.tensor([[-1.,-1.,-1.],[0.,0.,0.],[1.,1.,1.]]),'Horizontal edge filter')
```

第一个核是**竖直边缘滤波器**：左列 -1、中间 0、右列 1。当它盖在均匀区域时，正负相加为 0；当它跨过一条竖直边缘时，两侧像素差变大，产生大响应。水平核同理，只是方向转了 90°。这正是「加权求和」公式的具象化。

> `plot_convolution` 的实现见 [pytorchcv.py:103-L119](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/07-ConvNets/pytorchcv.py#L103-L119)，它把传入的权重 `copy_` 进一个 `nn.Conv2d`，再对前 5 张训练图做卷积并对比显示。

**(2) 用 `nn.Conv2d` 定义可训练的卷积层**

Notebook 紧接着说明卷积层需要三个关键超参（[ConvNetsPyTorch.ipynb:110-L113](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/07-ConvNets/ConvNetsPyTorch.ipynb#L110-L113)）：`in_channels`（输入通道，灰度图为 1）、`out_channels`（用多少个滤波器）、`kernel_size`（滑窗大小，常用 3×3 或 5×5）。最简单的 `OneConv` 网络如下（[ConvNetsPyTorch.ipynb:153-L171](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/07-ConvNets/ConvNetsPyTorch.ipynb#L153-L171)）：

```python
class OneConv(nn.Module):
    def __init__(self):
        super(OneConv, self).__init__()
        self.conv = nn.Conv2d(in_channels=1,out_channels=9,kernel_size=(5,5))
        self.flatten = nn.Flatten()
        self.fc = nn.Linear(5184,10)        # 9*24*24 = 5184

    def forward(self, x):
        x = nn.functional.relu(self.conv(x))
        x = self.flatten(x)
        x = nn.functional.log_softmax(self.fc(x),dim=1)
        return x
```

读这段代码：9 个 \(5\times5\) 核作用在 \(28\times28\) 单通道图上 → 输出 \(9\times24\times24\)（因为 \(28-5+1=24\)）→ 展平成 5184 维向量 → 线性层映射到 10 个类别。注意层间夹了 `relu` 非线性激活。

**(3) 引入池化的多层 CNN**

Notebook 在「Multi-layered CNNs and pooling layers」一节正式定义池化（[ConvNetsPyTorch.ipynb:258-L267](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/07-ConvNets/ConvNetsPyTorch.ipynb#L258-L267)）：Average Pooling 取窗口均值、Max Pooling 取窗口最大值（用来「探测某模式是否在窗口内出现」）。随后给出带池化的多层网络（[ConvNetsPyTorch.ipynb:305-L317](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/07-ConvNets/ConvNetsPyTorch.ipynb#L305-L317)）：

```python
class MultiLayerCNN(nn.Module):
    def __init__(self):
        super(MultiLayerCNN, self).__init__()
        self.conv1 = nn.Conv2d(1, 10, 5)     # 28 -> 24
        self.pool = nn.MaxPool2d(2, 2)       # 尺寸减半
        self.conv2 = nn.Conv2d(10, 20, 5)    # 12 -> 8
        self.fc = nn.Linear(320, 10)         # 20*4*4 = 320

    def forward(self, x):
        x = self.pool(nn.functional.relu(self.conv1(x)))  # 24 -> 12
        x = self.pool(nn.functional.relu(self.conv2(x)))  # 8 -> 4
        x = x.view(-1, 320)
        x = nn.functional.log_softmax(self.fc(x),dim=1)
        return x
```

关键观察：池化层 `nn.MaxPool2d(2,2)`（窗口 2、步长 2）没有可训练参数，所以整个网络只 `new` 了一个 `self.pool` 实例并复用（这是 Notebook 在 markdown 里特意提示的工程技巧）。逐层追踪尺寸：`28 → conv1 → 24 → pool → 12 → conv2 → 8 → pool → 4`，最终 \(20\times4\times4=320\) 维特征送入分类器。

#### 4.1.4 代码实践

**实践目标**：亲手看到「卷积核 = 模式探测器」，并验证输出尺寸公式。

**操作步骤**：

1. 在 `ai4beg` 环境下打开 `ConvNetsPyTorch.ipynb`，依次运行前 4 个 cell（含 `load_mnist` 与两个 `plot_convolution`）。
2. 观察竖直/水平滤波器作用下，MNIST 数字图里的竖直/水平笔画如何被「点亮」。
3. 自己设计一个新的 \(3\times3\) 核，例如对角线核 \(\begin{bmatrix}1&0&-1\\0&1&0\\-1&0&1\end{bmatrix}\)，再调一次 `plot_convolution`，看它放大了什么方向的笔画。

**需要观察的现象**：竖直核让数字的竖笔画（如「1」「7」的竖干）变亮、横笔画变暗；水平核则相反。

**预期结果**：每个手工核都在与其方向一致的笔画处产生强响应，证明卷积确实是「模式匹配」。

**说明**：本步骤需要在本机跑通 Jupyter，若环境未就绪可先记录为「待本地验证」，阅读 [pytorchcv.py:103-L119](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/07-ConvNets/pytorchcv.py#L103-L119) 理解其绘图逻辑后再运行。

#### 4.1.5 小练习与答案

**练习 1**：把 `OneConv` 的 `kernel_size` 从 `(5,5)` 改成 `(3,3)`，卷积输出的空间尺寸变成多少？展平后的向量长度是多少？

**答案**：\(28-3+1=26\)，输出 \(9\times26\times26\)，展平为 \(9\times26\times26=6084\) 维；对应的 `nn.Linear` 第一维也要从 5184 改成 6084。

**练习 2**：为什么 `MultiLayerCNN` 里只 `new` 了一次 `self.pool` 却能在 `forward` 中被调用两次？

**答案**：Max/Average Pooling 没有任何可训练权重，它只是一个确定性的数学运算（取 max 或取 mean），所以同一个实例可以被反复复用，与卷积层、线性层必须各自持有独立权重不同。

---

### 4.2 感受野与参数量：为什么 CNN 更省更准

#### 4.2.1 概念说明

上一节我们看到 `MultiLayerCNN` 总参数量只有约 8.5K，而上一单元的全连接网络高达约 80K，但准确率反而更高、收敛更快。要理解这个「反直觉」的好处，需要两个概念：

- **感受野（receptive field）**：某一层的一个输出神经元，能「看到」原始输入图像的多大区域。浅层卷积核只看到 \(3\times3\) 或 \(5\times5\) 的小邻域；随着层数叠加与池化，深层神经元看到的是原图上一块越来越大的区域——这正是它能识别「整只猫」的原因。
- **参数量（parameter count）**：模型需要学习的权重总数。参数越少，越不容易过拟合，也越省显存。

CNN 参数量远小于全连接网络的根本原因有二：

1. **局部连接**：每个输出只依赖输入的一小块（kernel 大小），而不是整张图。
2. **权重共享**：同一个核滑过整张图像的所有位置——即「在左上角找横线的核」和「在右下角找横线的核」是**同一组权重**。这既大幅减少参数，又天然带来了**平移不变性**（模式出现在哪里都能被同一个核检出）。

#### 4.2.2 核心流程

**卷积层参数量公式**。对 `Conv2d(in_channels=C_in, out_channels=C_out, kernel_size=k)`：

\[
\text{params} = C_{\text{out}} \times (C_{\text{in}} \times k \times k + 1)
\]

括号里 \(+1\) 是每个输出通道的偏置。以 `OneConv` 的卷积层为例：\(9\times(1\times5\times5+1)=9\times26=234\)，与 Notebook 的 `summary` 输出 `Conv2d ... 234` 完全一致。

**全连接层参数量公式**。对 `Linear(in, out)`：

\[
\text{params} = \text{in}\times\text{out} + \text{out}
\]

`OneConv` 末尾 `Linear(5184, 10)`：\(5184\times10+10=51850\)，也与 `summary` 的 `51,850` 一致。

**一个关键洞察**：CNN 里真正占参数大头的，往往是**最后一层全连接分类器**，而不是卷积层。把 `OneConv`（最后一层输入 5184 维）升级成带池化的 `MultiLayerCNN`（最后一层输入仅 320 维），分类器参数从 51850 骤降到 3210——这就是池化「压缩空间尺寸」带来的参数红利。

**金字塔结构（Pyramid Architecture）**。README 把这种设计总结为：随层数加深，**空间尺寸递减、通道数递增**（[README.md:44-L46](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/07-ConvNets/README.md#L44-L46)）。空间递减靠池化、通道递增靠逐层加 filter，整体形状如金字塔。

#### 4.2.3 源码精读

**(1) 用 `torchinfo.summary` 数参数**

`OneConv` 的 `summary` 输出（见 [ConvNetsPyTorch.ipynb:153-L171](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/07-ConvNets/ConvNetsPyTorch.ipynb#L153-L171) 运行结果）清晰地分了三层：

```text
├─Conv2d: 1-1        [1, 9, 24, 24]      234
├─Flatten: 1-2       [1, 5184]           --
├─Linear: 1-3        [1, 10]             51,850
Total params: 52,084
```

对照公式逐项核验：卷积层 234、线性层 51850、合计 52084——**卷积层参数只占 0.45%**，绝大头在分类器。

Notebook 紧接着点明了对比意义（见 `cell-7` 的 markdown）：这个约 5 万参数的网络比上一单元全连接网络的约 8 万参数更少，却在更小数据集上效果更好，因为「convolutional networks generalize much better」。

**(2) 池化如何把参数压到 8.5K**

对照 `MultiLayerCNN`（[ConvNetsPyTorch.ipynb:305-L317](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/07-ConvNets/ConvNetsPyTorch.ipynb#L305-L317)）逐层算参数：

| 层 | 计算 | 参数量 |
| --- | --- | --- |
| `conv1 = Conv2d(1,10,5)` | \(10\times(1\times5\times5+1)\) | 260 |
| `conv2 = Conv2d(10,20,5)` | \(20\times(10\times5\times5+1)\) | 5020 |
| `fc = Linear(320,10)` | \(320\times10+10\) | 3210 |
| **合计** | | **≈ 8490（约 8.5K）** |

Notebook 的 markdown 明确写「The number of trainable parameters (~8.5K) is dramatically smaller」，与我们手算吻合。原因正是：两次池化把送入分类器的特征维数从 5184 压到了 320。

#### 4.2.4 代码实践

**实践目标**：亲手验证参数量公式与「金字塔压缩」效果。

**操作步骤**：

1. 在 Notebook 中 `OneConv` 与 `MultiLayerCNN` 的 `summary` cell 后，分别用公式手算每层参数。
2. 把 `MultiLayerCNN` 的两次 `MaxPool2d(2,2)` 注释掉（仅作思想实验，不必改源码——可在新 cell 里复制一个去掉 pool 的类），再跑 `summary`，观察 `Linear` 的输入维度与总参数暴涨到多少。

**需要观察的现象**：去掉池化后，特征图不再缩小，`Linear` 的输入维数会成倍增长，总参数量急剧上升。

**预期结果**：你会直观看到「池化是控制 CNN 参数量的关键阀门」。具体数值**待本地验证**（取决于你保留了几层卷积），但趋势必然是「无池化 → 参数暴涨」。

#### 4.2.5 小练习与答案

**练习 1**：`conv2 = Conv2d(10,20,5)` 这一层共有多少参数？请用公式写出。

**答案**：\(C_{\text{out}}\times(C_{\text{in}}\times k^2+1)=20\times(10\times5\times5+1)=20\times251=5020\)。

**练习 2**：为什么说卷积的「权重共享」带来了平移不变性？

**答案**：同一个核用相同的权重滑过图像每个位置，所以「横线出现在左上角」和「出现在右下角」会被同一组权重检出、产生相同强度的响应——模型天然不在乎模式出现在哪里，只在乎它有没有出现。

---

### 4.3 经典架构演进：从 LeNet 到 ResNet

#### 4.3.1 概念说明

理解了卷积、池化、感受野与金字塔之后，我们就能看懂过去十年 CNN 架构的演进主线。课程把这些放在 [CNN_Architectures.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/07-ConvNets/CNN_Architectures.md)，并在 Notebook 里用 `LeNet` 做了实战。演进的核心驱动力是：**网络越深越好，但单纯堆深会带来梯度消失和参数爆炸**，每一代架构都是在解决「如何又深又好训练」。

| 架构 | 年份 | 核心创新 | 解决的问题 |
| --- | --- | --- | --- |
| **LeNet** | 1998 | 卷积+池化的金字塔雏形 | 证明 CNN 能做手写/物体识别 |
| **VGG-16** | 2014 | 统一用 \(3\times3\) 小核堆叠 | 用小核堆出大感受野，参数仍可控 |
| **ResNet** | 2015 | 残差块（identity 短接） | 让上百层的网络也能训练 |
| **Inception** | 2014 | 每层并联多种尺度核 + \(1\times1\) 卷积 | 多尺度特征 + 通道降维 |
| **MobileNet** | 2017 | 深度可分离卷积 | 砍参数，适配移动端 |

#### 4.3.2 核心流程

**VGG-16**：典型的金字塔，由「卷积-池化」反复堆叠构成，2014 年在 ImageNet top-5 取得 92.7% 准确率（[CNN_Architectures.md:3-L13](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/07-ConvNets/CNN_Architectures.md#L3-L13)）。它证明了「两个 \(3\times3\) 的感受野等价于一个 \(5\times5\)，但参数更少、非线性更多」，从此小核成为主流。

**ResNet**：微软研究院 2015 年提出，核心是**残差块（residual block）**——在正常卷积旁加一条「恒等短路（identity passthrough）」，让该层去学习「与输入的**差值**（residual）」而非完整输出（[CNN_Architectures.md:15-L25](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/07-ConvNets/CNN_Architectures.md#L15-L25)）。直觉上：训练初期权重接近 0，信号几乎全部走短路，等价于一个浅网络；随训练推进权重变大，网络「按需」变深。这条短路还给了梯度一条畅通的回传通道，破解了深层网络的梯度消失，于是出现了 ResNet-52/101/152 这样的百层网络。

**Inception**：把「一层」做成**多条并联路径**的组合（\(1\times1、3\times3、5\times5\) 等并行），一次抓多种尺度的模式（[CNN_Architectures.md:27-L37](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/07-ConvNets/CNN_Architectures.md#L27-L37)）。其中 \(1\times1\) 卷积看似无用，实则是在「通道维度」上做加权混合，可视为对通道维度的降维/池化，大幅压缩计算量。

**MobileNet**：面向移动端的轻量家族，核心是**深度可分离卷积（depthwise separable convolution）**——把标准卷积拆成「逐通道的空间卷积 + 跨通道的 \(1\times1\) 卷积」两步，参数和计算量都大幅下降（[CNN_Architectures.md:39-L43](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/07-ConvNets/CNN_Architectures.md#L39-L43)）。

#### 4.3.3 源码精读

Notebook 用 `LeNet` 把上面这些思想落到了 CIFAR-10 上。课程先说明 LeNet 由 Yann LeCun 提出、遵循同样的金字塔原则、区别在于输入是 3 通道彩色图（[ConvNetsPyTorch.ipynb:440-L442](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/07-ConvNets/ConvNetsPyTorch.ipynb#L440-L442)），然后给出实现（[ConvNetsPyTorch.ipynb:483-L500](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/07-ConvNets/ConvNetsPyTorch.ipynb#L483-L500)）：

```python
class LeNet(nn.Module):
    def __init__(self):
        super(LeNet, self).__init__()
        self.conv1 = nn.Conv2d(3, 6, 5)      # 3 通道输入
        self.pool = nn.MaxPool2d(2)
        self.conv2 = nn.Conv2d(6, 16, 5)
        self.conv3 = nn.Conv2d(16,120,5)
        self.flat = nn.Flatten()
        self.fc1 = nn.Linear(120,64)
        self.fc2 = nn.Linear(64,10)

    def forward(self, x):
        x = self.pool(nn.functional.relu(self.conv1(x)))
        x = self.pool(nn.functional.relu(self.conv2(x)))
        x = nn.functional.relu(self.conv3(x))
        x = self.flat(x)
        x = nn.functional.relu(self.fc1(x))
        x = self.fc2(x)
        return x
```

读这段代码，可以印证本讲的几条主线：

- **金字塔**：通道数 \(3\to6\to16\to120\) 递增，经两次池化空间尺寸 \(32\to14\to5\to1\) 递减。
- **层间夹 ReLU**：每个卷积/全连接后都接 `relu` 非线性。
- **输出与损失匹配**：LeNet 末层**不加** `log_softmax`，直接返回 `fc2(x)`，因此训练时改用 `nn.CrossEntropyLoss()`（它内部自带 softmax），见 [ConvNetsPyTorch.ipynb:532](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/07-ConvNets/ConvNetsPyTorch.ipynb#L532)：

```python
hist = train(net, trainloader, testloader, epochs=3, optimizer=opt, loss_fn=nn.CrossEntropyLoss())
```

这是与 `OneConv`/`MultiLayerCNN`（用 `log_softmax`+默认 `NLLLoss`）的显著区别——**输出激活与损失函数必须成对匹配**。

#### 4.3.4 代码实践

**实践目标**：把架构演进从「文字」变成「可对照的结构」。

**操作步骤**（源码阅读型实践）：

1. 打开 [CNN_Architectures.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/07-ConvNets/CNN_Architectures.md)，对照 ResNet 残差块的插图，在纸上画出一个残差块：输入 \(x\) 同时走「两层卷积」和「恒等短接」，最后相加 \(F(x)+x\)。
2. 回到 Notebook 的 `LeNet` cell，沿着 `forward` 逐行标注每一步张量的形状（输入 \(3\times32\times32\) → …）。
3. 思考：如果要把 `LeNet` 的思想往「更深」推，遇到梯度消失时，ResNet 的残差短接会怎么帮我们？

**需要观察的现象**：手算后你会得到 `conv3` 输出为 \(120\times1\times1\)，所以 `fc1` 的输入正好是 120，与代码 `nn.Linear(120,64)` 自洽。

**预期结果**：你能凭公式独立推出 LeNet 每一层输出形状，并把 ResNet 的「学残差」用一句话讲给同学听。形状推导结果**建议本地用 `summary(net,(1,3,32,32))` 验证**。

#### 4.3.5 小练习与答案

**练习 1**：LeNet 的 `forward` 里 `conv3` 之后为什么**没有**接 `pool`？

**答案**：因为经过两次池化后空间尺寸已是 \(5\times5\)，而 `conv3` 用的是 \(5\times5\) 核，\(5-5+1=1\)，输出空间尺寸变为 \(1\times1\)，已经没有再池化的空间了，故直接展平进全连接层。

**练习 2**：ResNet 的残差块让网络学习「残差」\(F(x)\)，最终输出是 \(F(x)+x\)。为什么这比直接学完整映射 \(H(x)\) 更容易训练？

**答案**：训练初期权重很小，\(F(x)\approx0\)，输出 \(\approx x\)，相当于一个恒等映射（浅网络），梯度可经短接顺畅回传；网络先学会「不变」，再逐步学到需要的修正量 \(F(x)\)，避免了深层网络一开始就难以拟合的困境。

**练习 3**：Inception 里的 \(1\times1\) 卷积既「看不到」邻域像素，为什么还有用？

**答案**：卷积同时在「通道维」上做加权求和，\(1\times1\) 卷积不混合空间邻域，但会**混合所有输入通道**，相当于在通道维度上的线性组合/降维，能显著压缩后续大核卷积的计算量。

## 5. 综合实践

**任务**：完成 `07-ConvNets` 的 lab——**PetFaces 宠物面孔分类**，把本讲的卷积、池化、金字塔与「激活+损失匹配」全部用上。

任务背景与数据见 [lab/README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/07-ConvNets/lab/README.md)：用 [Oxford-IIIT Pet Dataset](https://www.robots.ox.ac.uk/~vgg/data/pets/)（37 种猫狗品种）训练一个 CNN 做分类。数据下载方式见 README：

```python
!wget https://thor.robots.ox.ac.uk/~vgg/data/pets/images.tar.gz
!tar xfz images.tar.gz
!rm images.tar.gz
```

**建议步骤**：

1. 用 `torchvision.datasets.ImageFolder` 把按品种分子目录的图片加载为张量，并 `resize` 成统一方形、切分训练/测试集（参照 [lab/PetFaces.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/07-ConvNets/lab/PetFaces.ipynb) 的提示）。
2. 仿照本讲的 `MultiLayerCNN` / `LeNet`，**自己定义**一个金字塔 CNN：
   - 通道数随深度递增、空间尺寸靠 `MaxPool2d` 递减；
   - 每个卷积后接 ReLU；
   - 输出神经元数 = 类别数（37）。
3. 关键：末层**不加** softmax，直接配 `nn.CrossEntropyLoss()` 训练（与 LeNet 一致）。
4. 每个 epoch 记录训练/验证准确率并画图，判断是否过拟合（验证准确率由升转降即为信号——呼应上一单元 `05-Frameworks`）。
5. 因为类别多达 37 且部分品种肉眼都难分，建议额外统计 **top-k 准确率**（如 top-3）。

**预期结果**：能在一个比 MNIST 难得多的真实数据集上拿到**远高于 1/37≈2.7% 盲猜基线**的合理准确率；具体数值取决于训练时长与是否用 GPU，**待本地验证**。lab 的 README 也提示：能拿到合理准确率、并体会「top-k 比单纯 top-1 更贴合这类易混淆任务」即算完成。

## 6. 本讲小结

- **卷积**用一个小核滑过图像做加权求和，本质是「局部模式探测器」；**池化**（Max/Average）在不增加参数的前提下缩小空间尺寸。二者是 CNN 的两大基石。
- 卷积的输出尺寸满足 \(W_{\text{out}}=W_{\text{in}}-k+1\)（无填充、步长 1），\(2\times2\) MaxPool 让空间尺寸减半——这些公式可用课程 Notebook 的 `summary` 输出逐项验证。
- **感受野**随层数与池化而扩大，使深层神经元能「看到」整只物体；**权重共享 + 局部连接**让 CNN 参数远少于全连接网络（`MultiLayerCNN` 约 8.5K vs 全连接约 80K），泛化更好。
- **金字塔结构**=空间尺寸递减、通道数递增，是几乎所有图像 CNN 的通用骨架。
- 架构演进主线：**LeNet**（金字塔雏形）→ **VGG**（小核堆叠）→ **ResNet**（残差短接破梯度消失、支持百层）→ **Inception**（多尺度并联 + \(1\times1\) 通道降维）→ **MobileNet**（深度可分离卷积，移动端轻量化）。
- 工程细节：层间必须夹 ReLU；**输出激活与损失必须成对匹配**（`log_softmax`+`NLLLoss`，或末层裸输出 + `CrossEntropyLoss`）；无参数的池化层可只 `new` 一次并复用。

## 7. 下一步学习建议

本讲我们「从零定义并训练」了 CNN，但数据集偏小、网络也偏浅。下一讲 **u3-l3 迁移学习与训练技巧** 将解决两个直接痛点：

1. 当自己的数据不够多时，如何**复用别人在大数据集（如 ImageNet）上预训练好的 CNN**（这正是本讲提到的 VGG/ResNet 的实际用法），用「特征提取」和「微调」两种策略快速得到好模型。
2. 如何用**数据增强、学习率调度、Dropout** 进一步抗过拟合——本讲的 `05-Frameworks` 已经埋下 Dropout 的伏笔，下一讲会给出完整代码。

建议继续阅读的源码：`lessons/4-ComputerVision/08-TransferLearning/` 下的 `TransferLearningPyTorch.ipynb`、`TrainingTricks.md` 与 `Dropout.ipynb`，它们与本讲的 CNN 结构直接衔接。
