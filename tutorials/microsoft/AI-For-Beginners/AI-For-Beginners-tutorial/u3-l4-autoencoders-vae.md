# 自编码器与变分自编码器 VAE

## 1. 本讲目标

前面三讲我们都在做「分类」：给一张图，输出它属于哪一类。分类依赖**大量人工标注的标签**，而标注又贵又慢。本讲换一个完全不同的训练思路——**自监督学习（self-supervised learning）**：不要标签，让网络用「图片本身」既当输入又当目标，自己教自己。

学完本讲，你应当能够：

- 说清**自编码器（Autoencoder, AE）**的「编码器 → 潜在空间 → 解码器」三段式结构，以及它为什么能在无标签数据上学到有用特征。
- 理解**潜在空间（latent space）**是什么、它的维度大小如何影响「重建质量」与「生成能力」，并能举出去噪、超分辨率、降维可视化三类典型应用。
- 掌握**变分自编码器（Variational Autoencoder, VAE）**的两个关键创新：**重参数化（reparameterization）**采样技巧，以及由「重建损失 + KL 损失」组成的损失函数。
- 能够打开课程 Notebook，亲手修改潜在空间维度，观察重建与生成的变化。

## 2. 前置知识

本讲是计算机视觉单元的第四课，承接 [u3-l2 CNN](u3-l2-convnets-architectures.md) 与 [u3-l3 迁移学习](u3-l3-transfer-learning.md)。读本讲前，请确认你理解下面几个概念：

- **卷积层、池化层、上采样**：编码器靠「卷积 + 池化」把图压小，解码器靠「卷积 + 上采样（Upsample）」把图放大，二者互为镜像。这是上一讲 CNN 金字塔结构的直接复用。
- **特征提取器即编码器**：在迁移学习里，预训练 CNN 的卷积部分叫「特征提取器」。本讲的编码器本质上就是一个特征提取器，只是它的训练目标不是分类，而是「重建原图」。
- **图像即 NumPy 数组**：MNIST 手写数字在本讲里是 28×28×1 的张量，像素值归一化到 [0,1]。
- **损失函数驱动训练**：和前几讲一样，训练仍是「前向算损失 → 反向算梯度 → 优化器更新参数」三步循环，只是这里的损失不是分类的交叉熵，而是衡量「重建得像不像」的误差。

另外，VAE 用到一点概率论：正态分布 \(N(\mu,\sigma^2)\)、KL 散度（衡量两个分布有多像）。本讲会从直觉讲起，不会默认你熟悉公式推导。

## 3. 本讲源码地图

本讲只涉及课程第 09 课的讲义与一个 PyTorch Notebook：

| 文件 | 作用 |
| --- | --- |
| [lessons/4-ComputerVision/09-Autoencoders/README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/09-Autoencoders/README.md) | 课程讲义，讲清自编码器的动机、应用场景与 VAE 的概率思想。 |
| [lessons/4-ComputerVision/09-Autoencoders/AutoEncodersPyTorch.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/09-Autoencoders/AutoEncodersPyTorch.ipynb) | 可执行 Notebook，用 PyTorch 在 MNIST 上实现普通 AE、去噪 AE、超分 AE、VAE 与 AAE。 |

> ⚠️ **关于 Notebook 的行号**：本仓库的 `AutoEncodersPyTorch.ipynb` 在 git 中是**单行压缩的 JSON**（整个文件只有 1 行）。因此无法用 `#L起始-L结束` 这种行号片段精确跳转到某段代码。下文引用 Notebook 代码时，**一律按 cell（单元格）序号定位**（例如「cell 12 的 `Encoder` 类」），并配永久链接指向该文件。要找到对应代码，在 GitHub 渲染页或本地 Jupyter 中按 cell 编号往下数即可。README.md 是普通多行文件，行号可精确引用。

Notebook 的整体脉络（共 59 个 cell）是：

1. **cell 0–11**：导入库、加载 MNIST、定义可视化函数。
2. **cell 12–19**：**普通自编码器**（`Encoder`/`Decoder`/`AutoEncoder` + 训练 + 重建演示）。
3. **cell 20–26**：**去噪自编码器**（输入加噪、目标无噪）。
4. **cell 27–33**：**超分辨率自编码器**（输入缩小、目标高清）。
5. **cell 34–44**：**变分自编码器 VAE**（`VAEEncoder`/`VAEDecoder`/`vae_loss`/`train_vae`）。
6. **cell 45–57**：**对抗自编码器 AAE**（GAN + VAE 的结合，本讲只作了解）。

本讲重点是第 2、5 两段。

## 4. 核心概念与源码讲解

### 4.1 编码-解码结构：自编码器

#### 4.1.1 概念说明

分类网络要人工标标签。但很多时候我们手上只有一大堆「没标过的图」，能不能也让它学到东西？答案就是**自监督学习**：把图片既当网络的**输入**、又当网络的**输出目标**。

> 课程 README 对自编码器的定义是：用一个**编码器（encoder）**把输入图压成一个较小的**潜在空间（latent space）**向量，再用**解码器（decoder）**把它尽量还原回原图。详见 [README.md:L7](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/09-Autoencoders/README.md#L7)。

用一句话概括它的**训练目标**：**让重建出来的图和原图尽可能像**。注意几个关键点：

- **没有标签**：目标就是输入本身，所以叫「自」编码。这是它和分类最大的区别。
- **瓶颈（bottleneck）**：潜在空间比原图小得多（28×28=784 个像素被压成几百维甚至更小），信息被迫「浓缩」。正因为塞不下所有细节，网络只能学会**最重要的特征**，这恰是我们想要的「表示（representation/embedding）」。
- **有损（lossy）**：重建不可能和原图一模一样，差距由损失函数决定。README 把「有损」「数据特定」「无标签」列为自编码器三大性质，见 [README.md:L68-L72](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/09-Autoencoders/README.md#L68-L72)。

#### 4.1.2 核心流程

自编码器的前向流程是一条「先压后展」的沙漏：

```
输入图 x (28×28)
   │  Encoder：卷积+池化，逐级缩小空间尺寸、提炼通道特征
   ▼
潜在表示 z  ←—— 这就是「学到的特征」，维度远小于原图
   │  Decoder：卷积+上采样，逐级放大回原图尺寸
   ▼
重建图 x̂ (28×28)
```

训练时拿 `x̂` 和 `x` 算损失（本课用 BCELoss），再反向传播更新编码器和解码器的参数。整个数据流没有用到标签，只有图片本身。

> 直觉：编码器是「**把图翻译成一串密码**」，解码器是「**把密码翻译回图**」。要让翻译可逆，这串密码就必须抓住图的本质，于是密码（潜在表示）就成了有用的特征。

#### 4.1.3 源码精读

Notebook 的 `Encoder`（cell 12）是一个纯卷积金字塔，把 28×28×1 的图逐级压成 4×4×8 的特征图：

```python
class Encoder(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv1 = nn.Conv2d(1, 16, kernel_size=(3, 3), padding='same')  # 28×28 → 28×28×16
        self.maxpool1 = nn.MaxPool2d(kernel_size=(2, 2))                   # → 14×14×16
        self.conv2 = nn.Conv2d(16, 8, kernel_size=(3, 3), padding='same')  # → 14×14×8
        self.maxpool2 = nn.MaxPool2d(kernel_size=(2, 2))                   # → 7×7×8
        self.conv3 = nn.Conv2d(8, 8, kernel_size=(3, 3), padding='same')
        self.maxpool3 = nn.MaxPool2d(kernel_size=(2, 2), padding=(1, 1))   # → 4×4×8（潜在空间）
        self.relu = nn.ReLU()
```

见 [AutoEncodersPyTorch.ipynb · Encoder 类（cell 12）](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/09-Autoencoders/AutoEncodersPyTorch.ipynb)：三次「卷积 + ReLU + 最大池化」把空间尺寸 28→14→7→4，通道数 1→16→8→8。最终的潜在表示是 **4×4×8 = 128 个数值**——注意它是一个**空间特征图**，不是一个一维向量。

`Decoder`（cell 13）是 `Encoder` 的镜像，用「卷积 + 上采样」一路放大回 28×28×1，末层用 `Sigmoid` 把像素值压回 [0,1]：

```python
class Decoder(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv1 = nn.Conv2d(8, 8, kernel_size=(3, 3), padding='same')
        self.upsample1 = nn.Upsample(scale_factor=(2, 2))   # 4×4 → 8×8
        # ... 再两组 conv + upsample ...
        self.conv4 = nn.Conv2d(16, 1, kernel_size=(3, 3), padding='same')  # → 28×28×1
        self.sigmoid = nn.Sigmoid()                          # 像素压回 [0,1]
```

`AutoEncoder`（cell 14）把两者拼起来，并留了一个 `super_resolution` 开关用于后面换编码器：

```python
class AutoEncoder(nn.Module):
    def __init__(self, super_resolution=False):
        super().__init__()
        self.encoder = Encoder() if not super_resolution else SuperResolutionEncoder()
        self.decoder = Decoder()
    def forward(self, input):
        return self.decoder(self.encoder(input))
```

训练用 `BCELoss`（二元交叉熵，适合像素值在 [0,1] 的图）和 `Adam` 优化器（cell 15），训练 30 个 epoch 后 train/test loss 都降到约 0.104（cell 17 的输出）。损失函数和训练循环详见 [AutoEncodersPyTorch.ipynb · train 函数（cell 16）](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/09-Autoencoders/AutoEncodersPyTorch.ipynb)。

#### 4.1.4 代码实践（源码阅读型 + 可选运行）

**实践目标**：理解「瓶颈越窄，重建越糊」这一核心张力。

**操作步骤**：

1. 打开 Notebook，定位到 cell 18。它对 5 张测试图做了重建，并分两行画出「原图 / 重建图」。
2. 阅读cell 16 的 `train` 函数，确认两点：①损失是 `loss_fn(preds, imgs)`，**目标 `imgs` 就是输入本身**（无标签）；②训练循环骨架仍是 `zero_grad → 前向 → loss → backward → step`。
3. （可选运行）在你的 `ai4beg` 环境里从 cell 2 顺序运行到 cell 18，观察重建结果。
4. 进阶：按 Notebook cell 19 的 **Task 1** 提示，在 `Encoder` 卷积部分之后接一个全连接层，把潜在向量显式压到 **2 维**，再画散点图。

**需要观察的现象**：

- 重建图整体像原图，但笔画边缘略「糊」——这就是「有损」。
- 把潜在维度压到 2 维后，重建会更模糊（信息更少），但可以用散点把每个数字点出来（为下一节的 VAE 铺路）。

**预期结果**：标准 AE（潜在 128 维）能较清晰地重建；潜在压到 2 维后重建明显变糊。具体 loss 数值**待本地验证**（取决于是否用 GPU，30 个 epoch 在 CPU 上约需数分钟到十几分钟）。

#### 4.1.5 小练习与答案

**练习 1**：自编码器的「目标标签」是什么？为什么说它是「自监督」？

> **答案**：目标标签就是**输入图片本身**。因为监督信号（目标）不是人工标注的，而是直接从输入数据自动生成（输入=目标），所以叫自监督学习。

**练习 2**：如果把潜在空间取得和原图一样大（比如 784 维），会发生什么？

> **答案**：网络可以学成一个「恒等映射（直接抄答案）」——把输入原样拷到输出，不丢失任何信息，因而**学不到有用的特征**。瓶颈（让潜在维度远小于输入）正是迫使网络必须提炼本质的关键。

**练习 3**：解码器末层为什么用 `Sigmoid`？

> **答案**：MNIST 像素被 `ToTensor()` 归一化到 [0,1]，`Sigmoid` 把任意实数输出压到 (0,1)，与目标范围对齐，配合 `BCELoss` 才能正确计算损失。

---

### 4.2 潜在空间：压缩、去噪与超分辨率

#### 4.2.1 概念说明

**潜在空间（latent space）**就是编码器输出的那串「密码」所在的数学空间。它是本讲最核心的概念，理解了它，就能理解自编码器为什么「有用」、以及 VAE 为什么比普通 AE 更强。

潜在空间有两面：

- **好的方面**：它是原图的**低维紧凑表示**。README 指出自编码器能用来**降维可视化**或**训练图像嵌入**，而且通常比传统的 PCA（主成分分析）效果更好，因为它考虑了图像的空间结构和层次特征，见 [README.md:L21](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/09-Autoencoders/README.md#L21)。
- **麻烦的方面**：普通 AE 的潜在空间**没有结构**。README 举了 MNIST 的例子：相近的潜在向量**不一定**对应同一个数字，潜在空间里到处是「空洞」，你不知道随便挑一个点解码出来会是什么，见 [README.md:L28](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/09-Autoencoders/README.md#L28)。

潜在空间维度大小是一个**旋钮**：

- 维度**太小** → 信息塞不下，重建模糊，但表示极度浓缩。
- 维度**太大** → 重建清晰，但接近「抄答案」，表示没意义，且容易过拟合。
- 合适的维度在两者间取得平衡——这正是本讲综合实践要你亲手调的。

#### 4.2.2 核心流程

改变「输入和目标的配对方式」，同一个编码-解码骨架就能干三件不同的事：

| 任务 | 网络输入 | 训练目标 | 直觉 |
| --- | --- | --- | --- |
| **降维/特征学习** | 原图 x | 原图 x | 学浓缩表示 |
| **去噪（Denoising）** | 加噪图 x̃ | 无噪原图 x | 瓶颈塞不下噪声，被迫只留信号 |
| **超分辨率（Super-Res）** | 缩小图 | 高清原图 | 学会从粗到细补细节 |
| **生成（Generative）** | 随机潜在向量 z | —— | 解码器从「假」密码造新图 |

去噪的原理尤其巧妙：因为潜在空间小，**噪声这种「没用的信息」装不进去**，解码器重建时自然就把噪声丢掉了。README 对去噪和超分辨率的说明见 [README.md:L22-L24](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/09-Autoencoders/README.md#L22-L24)。

#### 4.2.3 源码精读

去噪靠 `train` 函数里的一个开关实现（cell 16）。当传入 `noisy` 参数时，输入变成 `imgs + 噪声`，但损失仍是和**无噪原图** `imgs` 比：

```python
imgs_noisy = imgs + noisy_tensor     # 输入：加噪
imgs_noisy = torch.clamp(imgs_noisy, 0., 1.)
preds = model(imgs_noisy)
loss = loss_fn(preds, imgs)          # 目标：无噪原图 imgs（不是 imgs_noisy！）
```

噪声由 `noisify` 生成（cell 10）：`np.random.normal(loc=0.5, scale=0.3, size=shapes)`，即均值为 0.5、标准差 0.3 的高斯噪声。去噪训练用 100 个 epoch（cell 24）。

超分辨率则换了一个更浅的编码器 `SuperResolutionEncoder`（cell 29），它只做两次池化，专门接收 14×14 的小图输入；`train` 函数里用 `transforms.Resize` 把高清图缩成 14×14 当输入，再拿高清原图当目标：

```python
class SuperResolutionEncoder(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv1 = nn.Conv2d(1, 16, kernel_size=(3, 3), padding='same')
        self.maxpool1 = nn.MaxPool2d(kernel_size=(2, 2))   # 14→7
        self.conv2 = nn.Conv2d(16, 8, kernel_size=(3, 3), padding='same')
        self.maxpool2 = nn.MaxPool2d(kernel_size=(2, 2), padding=(1, 1))  # 7→4
        self.relu = nn.ReLU()
```

详见 [AutoEncodersPyTorch.ipynb · SuperResolutionEncoder（cell 29）与 train（cell 16）](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/09-Autoencoders/AutoEncodersPyTorch.ipynb)。注意 `AutoEncoder(super_resolution=True)`（cell 30）会切换到这个浅编码器，而解码器 `Decoder` 不变——**同一个解码器既负责普通重建，也负责把小图放大**。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：通过阅读 `train` 函数，看清「输入/目标配对」如何决定任务性质。

**操作步骤**：

1. 打开 cell 16 的 `train`，追踪 `noisy` 和 `super_res` 两个参数如何改变 `imgs_noisy`（输入）和 `imgs`（目标）的关系。
2. 对照下表填空（自己写结论）：

| 模式 | `imgs_noisy` 是什么 | `imgs`（目标）是什么 |
| --- | --- | --- |
| 普通（`noisy=None, super_res=None`） | ? | ? |
| 去噪（`noisy=...`） | ? | ? |
| 超分（`super_res=2.0`） | ? | ? |

3. 阅读cell 25（去噪效果展示）和 cell 32（超分效果展示），对照 Markdown 说明。

**预期结果**：你会发现三种模式下**网络结构几乎不变**，变的只是「喂什么当输入、拿什么当答案」。这是理解自编码器灵活性的关键。

**参考答案**：普通模式下输入=目标=原图；去噪模式下输入=加噪图、目标=无噪原图；超分模式下输入=缩小图、目标=高清原图。

#### 4.2.5 小练习与答案

**练习 1**：为什么去噪自编码器能把噪声去掉，而不是把噪声也「记下来」？

> **答案**：因为潜在空间维度远小于输入，**容量有限**。噪声是高频、无规律的信息，无法被压缩进这么小的瓶颈；网络为了把重建误差降到最低，只能优先保留有结构的「数字笔画」信号，丢弃无法压缩的噪声。

**练习 2**：README 说自编码器「数据特定（data specific）」是什么意思？会有什么后果？

> **答案**：指它只对**训练时见过的那类图像**有效。比如用花朵训练的超分网络，拿去处理人脸就会效果很差（见 [README.md:L70](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/09-Autoencoders/README.md#L70)）。后果是：自编码器学到的细节来自训练分布，换一类图就「补不出」正确的细节。Notebook cell 26 的练习正是让你拿 MNIST 去噪器去试 Fashion-MNIST 来验证这一点。

---

### 4.3 变分自编码器 VAE 与重参数化

#### 4.3.1 概念说明

普通 AE 的潜在空间「没结构、有空洞」，所以**不能用来生成新图**——你随便挑一个潜在向量，解码出来很可能是垃圾。如果想做**生成模型**（凭空造新数字），就需要潜在空间「整齐、连续、可采样」。

**变分自编码器（VAE）**就是为此而生。它的核心改造是：编码器不再输出**一个确定的点**，而是输出**一个概率分布**。README 的说明见 [README.md:L26-L38](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/09-Autoencoders/README.md#L26-L38)。具体做法（README 第 36–38 行总结）：

1. 对每个输入，编码器预测分布的参数 `z_mean`（均值）和 `z_log`（方差的常用对数，课程里写作 `z_log_sigma`）；
2. 从这个分布 \(N(\mathrm{z\_mean},\,e^{\mathrm{z\_log}})\) 里**随机采样**一个向量 `sample`（代码里叫 `z_val`）；
3. 解码器用这个采样向量去重建原图。

为什么要从分布里采样、而不是直接用均值？因为「采样」给训练引入了**随机性**，迫使潜在空间变得**连续平滑**——相邻的分布会解码出相似的图，整个潜在空间被「摊平」成一个规整的流形。这样训练好后，从标准正态分布 \(N(0,I)\) 随便采一个点，解码出来就是一张合理的新图。README 用 2D 潜在空间的 MNIST 例子展示了「拨动潜在坐标，数字平滑变化」的效果，见 [README.md:L49-L59](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/09-Autoencoders/README.md#L49-L59)。

#### 4.3.2 核心流程

VAE 的训练难点在于：**「采样」这个操作不可导**，梯度没法穿过 `np.random.normal` 反向传回编码器。解决办法是**重参数化技巧（reparameterization trick）**——把随机性「拎到计算图外面」：

\[ z = \mu + \sigma \odot \varepsilon,\qquad \varepsilon \sim N(0, I) \]

这里 \(\mu\) 是 `z_mean`，\(\sigma\) 是标准差，\(\varepsilon\) 是独立采样的噪声。这样一来，对 \(\mu\) 和 \(\sigma\) 而言，采样变成了一个**可导的加减乘运算**，梯度能正常回传；随机性全藏在 \(\varepsilon\) 里，而 \(\varepsilon\) 不需要求导。

VAE 的损失（变分下界 ELBO 的负数）由两部分组成，README 见 [README.md:L44-L47](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/09-Autoencoders/README.md#L44-L47)：

\[ \mathcal{L} = \underbrace{\mathcal{L}_{\text{rec}}}_{\text{重建损失}} + \underbrace{\mathcal{L}_{\text{KL}}}_{\text{KL 损失}} \]

- **重建损失** \(\mathcal{L}_{\text{rec}}\)：和解码图与原图的差距（MSE），和普通 AE 一样，保证「重建得像」。
- **KL 损失** \(\mathcal{L}_{\text{KL}}\)：约束每个输入对应的分布 \(N(\mu,\sigma^2)\) 不要离标准正态 \(N(0,1)\) 太远。它基于 **KL 散度**——衡量两个分布差异的指标。这一项是 VAE 能「整齐采样」的关键。

KL 损失的标准形式（推导略）为：

\[ \mathcal{L}_{\text{KL}} = -\frac{1}{2} \sum_{i=1}^{d} \left( 1 + \log\sigma_i^2 - \mu_i^2 - \sigma_i^2 \right) \]

#### 4.3.3 源码精读

VAE 的编码器（cell 35）是一个**全连接**网络（注意它不是卷积，与前面的 AE 不同）。它输出两个 2 维向量 `z_mean`、`z_log`，然后用重参数化得到采样向量 `z_val`：

```python
class VAEEncoder(nn.Module):
    def __init__(self, device):
        super().__init__()
        self.intermediate_dim = 512
        self.latent_dim = 2                      # ← 潜在空间维度，本课硬编码为 2（便于 2D 可视化）
        self.linear = nn.Linear(784, self.intermediate_dim)
        self.z_mean = nn.Linear(self.intermediate_dim, self.latent_dim)
        self.z_log  = nn.Linear(self.intermediate_dim, self.latent_dim)
        self.relu = nn.ReLU()

    def forward(self, input):
        bs = input.shape[0]
        hidden = self.relu(self.linear(input))
        z_mean = self.z_mean(hidden)
        z_log  = self.z_log(hidden)
        # 重参数化：把随机性拎到 eps 里，使采样对 z_mean/z_log 可导
        eps = torch.FloatTensor(np.random.normal(size=(bs, self.latent_dim))).to(device)
        z_val = z_mean + torch.exp(z_log) * eps   # ← 重参数化采样
        return z_mean, z_log, z_val
```

见 [AutoEncodersPyTorch.ipynb · VAEEncoder（cell 35）](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/09-Autoencoders/AutoEncodersPyTorch.ipynb)。注意几个细节：

- **潜在维度硬编码为 2**（`self.latent_dim = 2`），所以训练好后可以把测试集每个数字的 `z_mean` 画成二维散点，直接看到 0–9 各自聚成一团。这正是本讲综合实践要你修改的地方。
- 重参数化那一行 `z_val = z_mean + torch.exp(z_log) * eps`，把随机性完全交给 `eps`（独立同分布的标准正态噪声），对 `z_mean`/`z_log` 而言是一条可导路径。

> 🔍 **源码批判性阅读（重要）**：本 Notebook 的实现与「教科书标准 VAE」有一处细节出入，值得你 aware。标准 VAE 通常令编码器输出 \(\log\sigma^2\)（**对数方差**），此时重参数化应为 \(z=\mu+\exp(\tfrac{1}{2}\log\sigma^2)\odot\varepsilon\)，而 KL 损失恰好是 \(-\tfrac12\sum(1+\log\sigma^2-\mu^2-\sigma^2)\)。本 Notebook 里：KL 项（cell 39 的 `1 + z_log - z_mean² - exp(z_log)`）**把 `z_log` 当作对数方差**，与标准公式一致；但采样行（上面的 `torch.exp(z_log)*eps`）却**把 `exp(z_log)` 当作标准差**，少了一个开方。两者口径差了一个系数 2。README 把该变量命名为 `z_log_sigma`（对数标准差），与采样行一致，但又与 KL 项不一致。结论：**这是一份用于教学演示的简化实现**，网络会自适应地学到合适的 `z_log` 量级，所以训练仍能收敛、仍能生成合理数字；但若你拿去做严肃研究，建议把采样行改为 `z_mean + torch.exp(0.5 * z_log) * eps` 以与 KL 项对齐。养成「读源码时对齐公式」的习惯，正是本课希望训练的能力。

解码器（cell 36）是 `2 → 512 → 784` 的全连接网络，末层 `Sigmoid`：

```python
class VAEDecoder(nn.Module):
    def __init__(self):
        super().__init__()
        self.intermediate_dim = 512
        self.latent_dim = 2
        self.linear = nn.Linear(self.latent_dim, self.intermediate_dim)
        self.output = nn.Linear(self.intermediate_dim, 784)   # 28×28=784
        self.sigmoid = nn.Sigmoid()
```

`VAEAutoEncoder`（cell 37）把三者串起来，并把 `z_vals` 缓存起来供损失函数取用：

```python
class VAEAutoEncoder(nn.Module):
    def __init__(self, device):
        super().__init__()
        self.encoder = VAEEncoder(device)
        self.decoder = VAEDecoder()
        self.z_vals = None
    def forward(self, input):
        bs = input.shape[0]
        input = input.view(bs, -1)        # 把 1×28×28 拉平成 784
        encoded = self.encoder(input)     # 返回 (z_mean, z_log, z_val) 三元组
        self.z_vals = encoded
        decoded = self.decoder(encoded[2])  # 只把采样的 z_val 喂给解码器
        return decoded
```

损失函数 `vae_loss`（cell 39）正是上面公式的代码化：

```python
def vae_loss(preds, targets, z_vals):
    mse = nn.MSELoss()
    reconstruction_loss = mse(preds, targets.view(targets.shape[0], -1)) * 784.0   # 重建损失
    temp = 1.0 + z_vals[1] - torch.square(z_vals[0]) - torch.exp(z_vals[1])        # KL 内部项
    kl_loss = -0.5 * torch.sum(temp, axis=-1)                                      # KL 损失
    return torch.mean(reconstruction_loss + kl_loss)
```

其中 `z_vals[0]` 是 `z_mean`、`z_vals[1]` 是 `z_log`。重建损失乘 784 是为了在数值上和 KL 项量级匹配（MSE 默认是按元素平均，乘以像素数还原成总误差量级）。训练用 `RMSprop` 优化器（cell 40，注意与普通 AE 用的 Adam 不同）。详见 [AutoEncodersPyTorch.ipynb · vae_loss（cell 39）、train_vae（cell 41）](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/09-Autoencoders/AutoEncodersPyTorch.ipynb)。

#### 4.3.4 代码实践（动手修改型）

**实践目标**：亲手修改 VAE 的潜在空间维度，观察「重建质量」与「采样生成多样性」如何随维度变化。这是本讲的核心实践。

**操作步骤**：

1. 在 Notebook 的 `VAEEncoder`（cell 35）和 `VAEDecoder`（cell 36）中，把 `self.latent_dim = 2` **同时**改成 `8`（或 `16`）。两处必须一致，否则维度对不上会报错。
2. 把 `np.random.normal(size=(bs, self.latent_dim))` 里依赖 `self.latent_dim` 的部分会自动跟随，无需改。
3. 从 cell 40 重新运行（重新建模型与优化器）→ cell 42 训练 → cell 43 查看重建结果。
4. 生成实验：训练后，从标准正态分布采样一个潜在向量，喂给解码器看生成的图。可仿照下面这段**示例代码**（非 Notebook 原有，需你新建一个 cell）：

   ```python
   # 示例代码：从 N(0, I) 采样并生成新数字
   model.eval()
   with torch.no_grad():
       z = torch.randn(1, model.encoder.latent_dim).to(device)   # 随机潜在向量
       generated = model.decoder(z).view(1, 28, 28).cpu()
   plotn(1, [(generated, 0)])
   ```

5. 把 `latent_dim` 分别设回 `2` 和设为 `16`，各训练一次，对比结果。

**需要观察的现象**：

- **latent_dim = 2**：重建偏模糊（2 维信息量小）；但能把测试集的 `z_mean` 画成二维散点，看到 0–9 各成聚类，且从空间某点平滑滑到另一点时，生成数字会渐变（这正是 README 配图展示的现象）。
- **latent_dim = 16**：重建更清晰（信息量大）；但二维散点画不出来了，且从 \(N(0,I)\) 随机采样生成的图，质量未必更好——因为高维下 KL 项更难把整个空间都「摊平」，可能出现空洞区域生成出模糊数字。

**预期结果**：维度增大 → 重建质量提升、二维可解释性丧失；维度减小 → 重建变糊、但潜在空间更易可视化。具体生成图像质量**待本地验证**（受训练 epoch、是否 GPU、随机种子影响）。

> 💡 若你没有 GPU 或时间有限，可只做「源码阅读」部分：对照公式读懂 `vae_loss` 与重参数化行即可，不必跑完整训练。Notebook 原始训练在 cell 42（`train_vae`，30 epoch）。

#### 4.3.5 小练习与答案

**练习 1**：为什么要用「重参数化技巧」把采样写成 \(z=\mu+\sigma\odot\varepsilon\)，而不是直接 `z = np.random.normal(mu, sigma)`？

> **答案**：直接从 \(N(\mu,\sigma^2)\) 采样，采样操作本身**不可导**，梯度无法穿过它传回编码器的 \(\mu,\sigma\)，网络就学不了。重参数化把随机性分离到独立的 \(\varepsilon\) 里，\(\varepsilon\) 不需要求导；而对 \(\mu,\sigma\) 而言，\(z\) 只是普通的加减乘运算，梯度能正常回传。

**练习 2**：VAE 的损失为什么必须有 KL 项？去掉它会怎样？

> **答案**：KL 项约束每个输入对应的潜在分布贴近标准正态 \(N(0,1)\)。去掉它，VAE 就退化成普通 AE——编码器可以让每个分布的方差趋于 0（退化为一个确定的点），潜在空间重新变得「空洞、无结构」，于是训练后**无法从 \(N(0,I)\) 采样生成合理新图**。重建损失管「像不像」，KL 项管「能不能整齐采样」，缺一不可。

**练习 3**：本 Notebook 的 VAE 用的是全连接层，而前面的普通 AE 用的是卷积层。如果想做「卷积版 VAE」，需要改哪里？（提示：Notebook cell 44 的 Task）

> **答案**：把 `VAEEncoder` 的全连接 `nn.Linear(784, 512)` 换成卷积金字塔（类似 cell 12 的 `Encoder`），并在输出端接一层把特征图变成 `z_mean`/`z_log`；`VAEDecoder` 相应换成反卷积/上采样结构。这就是 Notebook cell 44 留给你的进阶任务。

---

## 5. 综合实践

**任务**：用一份「潜在维度对比实验」，把本讲三个最小模块串起来。

**背景**：你已理解编码-解码结构、潜在空间、VAE 重参数化。现在请系统对比「潜在空间维度」对**重建**与**生成**两方面的影响，并解释原因。

**步骤**：

1. **基线**：保持 Notebook 原始 VAE（`latent_dim = 2`），训练并完成两件事：
   - 记录最终 train/test loss（来自 `train_vae` 的进度条）。
   - 把测试集所有样本的 `z_mean`（通过 `model.get_zvals()[0]`）画成二维散点，按数字标签着色，观察是否形成聚类。
2. **加宽**：把 `latent_dim` 改为 `8`，重新训练，记录 loss，并查看重建质量（cell 43）是否变清晰。
3. **生成对比**：对两种配置，各从 \(N(0,I)\) 采样 10 个潜在向量，用解码器生成 10 张图，主观比较哪组生成数字更「像真数字」。
4. **分析**：写一段 200 字以内的结论，回答：维度增大时，**重建**变好还是变差？**生成**呢？为什么会出现这种「重建和生成不完全同步」的现象？（提示：联系 KL 项在高维下更难把空间彻底摊平。）

**交付物**：

- 一张二维散点图（仅 `latent_dim=2` 时可画）。
- 两组 loss 数字与重建/生成图对比。
- 一段分析结论。

**预期结果**：`latent_dim=2` 重建较糊但潜在空间可可视化、生成尚可；`latent_dim=8` 重建更清晰，但随机生成质量提升有限甚至局部变差。具体数值与图像**待本地验证**。这个实验能让你真切体会到「潜在空间维度」是在**表示能力**与**生成可控性**之间权衡的旋钮。

## 6. 本讲小结

- **自编码器**用「输入既当输入又当目标」的**自监督**方式，在无标签数据上学习；结构是「编码器 → 潜在空间 → 解码器」的沙漏，瓶颈迫使网络提炼本质特征。
- **潜在空间**是原图的低维紧凑表示，可用于降维、去噪、超分辨率；它的**维度大小**是表示能力与信息浓缩之间的旋钮。普通 AE 的潜在空间**无结构、有空洞**，不能用于生成。
- **VAE** 让编码器输出**概率分布**而非确定点，靠**重参数化技巧** \(z=\mu+\sigma\odot\varepsilon\) 让采样可导，并用「**重建损失 + KL 损失**」既保证重建、又把潜在空间摊平成可采样的规整流形，从而能**生成新图**。
- 同一个编码-解码骨架，通过改变「输入/目标配对」（原图、加噪图、缩小图）就能做降维、去噪、超分三种任务，体现了自编码器的灵活性。
- 本 Notebook 的 VAE 实现是**教学简化版**：KL 项与采样行对 `z_log` 的口径略有出入，训练能收敛但做研究时应对齐标准公式——这示范了「读源码要对齐公式」的习惯。
- 课程把自编码器放在 CV 单元，是因为它的编码器本质是**特征提取器**，与上一讲的迁移学习一脉相承；而它的「解码器生成」能力，又为下一讲 GAN 的对抗生成埋下伏笔。

## 7. 下一步学习建议

- **紧接的下一讲**是 [u3-l5 GAN 与风格迁移](u3-l5-gans-style-transfer.md)。VAE 是「显式建模分布」的生成模型，GAN 则是「用判别器对抗」的生成模型，两者是生成模型的两条主流路线，对比学习收益最大。Notebook cell 45–57 的**对抗自编码器 AAE** 正是 GAN + VAE 的结合，可作为衔接阅读。
- **动手进阶**：完成 Notebook cell 44 的 Task，把全连接 VAE 改造成**卷积 VAE**，体会卷积编码器在图像上的优势。
- **延伸阅读**：README 末尾的 Review & Self Study 给了 Keras 官方博客、VAE 解释、条件 VAE 等链接，想深入概率推导可读 Kingma & Welling 的原论文《Auto-Encoding Variational Bayes》（Notebook cell 0 与 cell 34 顶部都给了 arXiv 链接）。
- **跨单元视角**：自编码器的「编码器=特征提取器」思想会在 [u5-l4 多模态 CLIP](u5-l4-multimodal-clip.md) 中再次出现——CLIP 也是用编码器把图像和文本压成可对齐的潜在向量，只是训练目标换成了对比学习。
