# 生成对抗网络 GAN 与风格迁移

## 1. 本讲目标

上一讲我们学了**自编码器（AE）与变分自编码器（VAE）**，它们已经能「生成」新图像。但本课会告诉你：当你想生成一张**高质量、有艺术感**的图（比如一幅像样的画）时，VAE 的重建往往**模糊、收敛不好**。为了解决「高质量生成」这个问题，本讲引入两类非常重要的技术：

1. **生成对抗网络（GAN）**：用「两个网络互相博弈」的方式逼出生成质量。
2. **神经风格迁移（Neural Style Transfer）**：把一张照片的内容，用另一幅画的笔触/风格重绘出来。

学完本讲你应该能够：

- 说清 GAN 为什么是「对抗」的，以及生成器与判别器各自的胜负关系；
- 用极小极大（minimax）目标函数描述 GAN 的训练目标，理解为什么损失会「震荡」而不是单调下降；
- 读懂课程 Notebook 里「两阶段交替训练」的源码，看懂 `detach()`、`eval()`、标签 0/1 各自的作用；
- 说清风格迁移与 GAN 的根本区别——**GAN 训练的是网络权重，风格迁移优化的是图像像素**；
- 看懂内容损失、风格损失（Gram 矩阵）、全变分损失三种损失的设计意图，并亲手跑出一张风格迁移图。

---

## 2. 前置知识

本讲是「进阶」内容，建议你先具备以下认知（均来自前置讲义）：

- **生成模型与潜在空间**（来自 [u3-l4](u3-l4-autoencoders-vae.md)）：AE 的解码器把一个低维的「潜在向量」还原成图像；VAE 让潜在空间可采样从而生成新图。GAN 的**生成器**就是从潜在向量生成图像的「解码器」。
- **CNN 与经典架构**（来自 [u3-l2](u3-l2-convnets-architectures.md)）：卷积、池化、转置卷积，以及 **VGG** 网络的层结构。风格迁移会冻结一个预训练 VGG 当「特征提取器」。
- **迁移学习与冻结权重**（来自 [u3-l3](u3-l3-transfer-learning.md)）：`requires_grad=False` / `vgg.trainable=False` 把预训练模型当作不可训练的特征提取器。风格迁移正是这种用法的典范。
- **反向传播与自动求导**（来自 [u2-l4](u2-l4-own-framework.md)、[u2-l5](u2-l5-frameworks-overfitting.md)）：PyTorch 的 `backward()` 与 TensorFlow 的 `GradientTape` 都是把「求梯度」自动化，本讲的训练循环全靠它。
- **对抗样本**（来自 u3-l3）：通过对**输入像素**做梯度下降来「骗过」网络。风格迁移的底层技巧和它同源——都是在**优化输入图像**本身。

> 一句话衔接：上一讲 VAE 是「从噪声采样出图」，本讲 GAN 是「让两个网络打擂台逼出更逼真的图」，风格迁移则是「不训练网络、只优化一张图，让它同时像内容图又像风格图」。

---

## 3. 本讲源码地图

本讲所有概念都有真实代码支撑，关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `lessons/4-ComputerVision/10-GANs/README.md` | 课程讲义：解释 GAN 架构、两阶段训练、GAN 训练难题、风格迁移三大损失的设计。是本讲概念的「官方说明书」。 |
| `lessons/4-ComputerVision/10-GANs/GANPyTorch.ipynb` | PyTorch 实现：先用全连接的线性 GAN 在 MNIST 上训练，再升级到卷积版 **DCGAN**；包含 `Generator`、`Discriminator`、`train_gan` 等关键源码。 |
| `lessons/4-ComputerVision/10-GANs/StyleTransfer.ipynb` | TensorFlow/Keras 实现：冻结 VGG16，用内容损失 + 风格损失（Gram 矩阵）+ 全变分损失，对**图像像素**做梯度下降完成风格迁移。 |
| `lessons/4-ComputerVision/10-GANs/GANTF.ipynb` | GAN 的 TensorFlow/Keras 版本（双框架对照，理解一版即可）。 |
| `lessons/4-ComputerVision/10-GANs/StyleTransfer_Keras.ipynb` | 风格迁移的 Keras 写法对照版。 |

> 小贴士：本仓库课程常用「PyTorch + TensorFlow 双 Notebook 并行」的写法，你只需精读其中一个；本讲以 PyTorch 版讲 GAN、以 TensorFlow 版讲风格迁移，因为它们分别在这两个框架里写得最完整。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 对抗训练机制**、**4.2 生成器与判别器**、**4.3 神经风格迁移**。

### 4.1 对抗训练机制

#### 4.1.1 概念说明

GAN 的全称是 **Generative Adversarial Network（生成对抗网络）**。它的核心思想可以用一个比喻讲清楚：

> 想象有一个**造假钞的人（生成器 Generator）**和一个**鉴定钞票的警察（判别器 Discriminator）**。一开始两个人都很菜：假钞很假、警察也常看走眼。但他们天天较量——警察越练越能识破假钞，造假者就越要造得更像真的。经过长时间对抗，造假者造出的钞票逼真到连警察都分不清。

这就是「**对抗（adversarial）**」二字的来源：两个网络在持续竞争中一起变强。课程 README 对这两个角色的定义非常直白：

- **Generator（生成器）**：输入一个随机向量，输出一张图像（造假者）。
- **Discriminator（判别器）**：输入一张图像，判断它是「真实图（来自训练集）」还是「生成器造的假图」。它本质上就是一个**图像二分类器**（警察）。

为什么需要 GAN？因为上一讲的 VAE 用「重建 + KL 散度」训练，目标偏保守，生成的高分辨率图像容易模糊。GAN 没有显式的重建目标，而是用一个**会进化的判别器**来不断挑剔生成器的输出，从而逼出生成质量更高的图像。

#### 4.1.2 核心流程

GAN 是一个**两人零和博弈**。用数学写出来就是著名的**极小极大（minimax）目标函数**：

\[
\min_{G}\ \max_{D}\ V(D, G)
= \mathbb{E}_{x \sim p_{data}}\big[\log D(x)\big]
+ \mathbb{E}_{z \sim p_{z}}\big[\log\big(1 - D(G(z))\big)\big]
\]

别被公式吓到，它说的就是双方的目标相反：

- 判别器 \(D\) 要**最大化**这个值：让真图 \(D(x)\) 尽量接近 1（判定为真），让假图 \(D(G(z))\) 尽量接近 0（判定为假）。
- 生成器 \(G\) 要**最小化**这个值：也就是要让 \(D(G(z))\) 尽量接近 1，即**骗过判别器**，让它把假图当真图。

> 工程小窍门：直接最小化 \(\log(1-D(G(z)))\) 在训练初期梯度太弱（假图太假时梯度几乎为 0），所以实践中生成器常用**等价但梯度更强**的目标——**最大化 \(\log D(G(z))\)**，也就是「把假图标成真图（标签 1）」来算损失。课程 Notebook 正是这么做的。

每个训练步分**两个阶段交替进行**：

```
阶段一：训练生成器 G
  1. 冻结判别器 D（不更新它的权重）
  2. 采样一批随机噪声 z，生成假图 G(z)
  3. 把假图喂给 D，计算损失时「假装它们是真图」（标签=1）
  4. 反向传播 → 只更新 G 的权重
  目的：让 G 学会骗过当前的 D

阶段二：训练判别器 D
  1. 取一批真图（标签=1）和一批假图（标签=0，注意要 detach 断开梯度）
  2. 计算 D 在真假图上的总损失（两部分取平均）
  3. 反向传播 → 只更新 D 的权重
  目的：让 D 学会重新识破升级后的 G
```

一个非常反直觉但极其重要的现象：**GAN 的损失不会单调下降**。理想情况下，生成器损失和判别器损失会在某个水平上**来回震荡**，对应双方「你强一点、我追上一点」的动态平衡。如果你看到两个损失都在稳步下降，反而不一定是好事。

#### 4.1.3 源码精读

课程 README 把这套对抗流程讲得很清楚，建议先读：

- [README.md:L9-L17](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/10-GANs/README.md#L9-L17) — 定义 GAN 的核心思想：两个网络互相训练；并给出 Generator 与 Discriminator 的角色词汇（造假者 vs 鉴定者）。
- [README.md:L39-L48](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/10-GANs/README.md#L39-L48) — 解释为什么叫 adversarial，并把训练拆成「训练判别器 / 训练生成器」两阶段；特别指出两个损失应当「震荡」而非单调下降（对应 4.1.2 的核心结论）。

GAN 的训练难题同样写在 README 里，理解它们比背公式更重要：

- [README.md:L55-L62](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/10-GANs/README.md#L55-L62) — 列出 GAN 四大训练难题：**模式崩溃（Mode Collapse）**、**对超参数极其敏感**、**生成器/判别器失衡**（判别器太快掉到 0，生成器就没法学了）、**高分辨率难训练**（用 progressive growing 逐层解锁）。

Notebook 里的 `train_gan` 函数把两阶段流程逐行落地（该文件以单行 JSON 存储，GitHub 渲染为逐个 cell，下面按 cell 内容定位）：

- [GANPyTorch.ipynb（「Network training」说明 cell，紧接 `train_gan` 之前）](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/10-GANs/GANPyTorch.ipynb) — 用文字精确描述两阶段：生成器训练时用「冻结判别器 + 噪声 + 标签 1」算损失；判别器训练时把「真图标签 1」与「假图标签 0」两部分损失取平均。

两阶段对应的代码骨架（取自 `train_gan`，已精简）：

```python
# 阶段一：训练生成器
disc.eval()                 # 冻结判别器（BatchNorm 进 eval 模式）
gen.zero_grad()
noise = ...                 # 采样随机噪声
real_labels = torch.ones(...)   # 真标签 = 1
generated = gen(noise)
disc_preds = disc(generated)
g_loss = loss_fn(disc_preds, real_labels)  # 把假图当真图算损失
g_loss.backward()
optim_gen.step()            # 只更新生成器

# 阶段二：训练判别器
disc.train()
disc.zero_grad()
disc_real = disc(imgs)                              # 真图 → 标签 1
disc_real_loss = loss_fn(disc_real, real_labels)
disc_fake = disc(generated.detach())               # detach：断开到生成器的梯度
disc_fake_loss = loss_fn(disc_fake, fake_labels)   # 假图 → 标签 0
d_loss = (disc_real_loss + disc_fake_loss) / 2.0
d_loss.backward()
optim_disc.step()           # 只更新判别器
```

> 重点逐行解读：
> - `disc.eval()` 配合「只调用 `optim_gen.step()`」确保阶段一只更新生成器权重；
> - `generated.detach()` 在阶段二断开计算图，让判别器学习时**不会把梯度回传到生成器**（否则会互相干扰）；
> - 标签 `1`（真）/`0`（假）是 BCE 损失的目标，决定了每个网络「朝哪个方向」优化。

#### 4.1.4 代码实践（源码阅读 + 参数观察型）

> 本任务无需训练完成，重在**读懂两阶段交替的执行顺序**。

1. **实践目标**：在 `GANPyTorch.ipynb` 里逐行走查 `train_gan` 函数，确认「生成器先训、判别器后训」的顺序，并标出每一步冻结/解冻、清零梯度、断开梯度的语句。
2. **操作步骤**：
   - 打开 `GANPyTorch.ipynb`，找到 `train_gan` 所在 cell。
   - 在纸上抄下它的内层 `for batch` 循环，用两种颜色笔分别框出「训练生成器」和「训练判别器」两段代码。
   - 修改上方超参数 cell，把 `epochs = 100` 改成 `epochs = 5`、`plot_every = 10` 改成 `plot_every = 1`，这样每轮都能看到生成样本。
3. **需要观察的现象**：每轮打印的 `generator loss` 与 `discriminator loss` 是否在**震荡**（一会高一会低）而不是双双平稳下降；生成的数字图像是否随轮次变清晰。
4. **预期结果**：5 轮内图像可能还很模糊（GAN 收敛慢），但你应能看到损失在波动；若判别器损失迅速逼近 0 而生成器损失飙升，就对应 README 说的「失衡」问题。
5. 运行耗时与最终图像质量**待本地验证**（受算力影响很大）。

#### 4.1.5 小练习与答案

**练习 1**：为什么阶段二训练判别器时，对假图要用 `generated.detach()`？如果不加会怎样？

> **参考答案**：`detach()` 把假图从生成器的计算图中「剪断」，使得对判别器算的梯度不会回传到生成器。如果不加，判别器的反向传播会一并更新生成器权重，导致两个网络在同一阶段被同时改动，训练目标混乱、难以收敛。

**练习 2**：理想情况下，GAN 训练时两个损失应该双双下降到 0 吗？

> **参考答案**：不应该。GAN 是动态博弈，理想状态下两个损失在某个水平**来回震荡**。若判别器损失迅速降到 0，说明它太强、生成器学不动（失衡）；若两者都单调下降，反而可能不是健康的对抗状态。

---

### 4.2 生成器与判别器

#### 4.2.1 概念说明

4.1 讲了「对抗」这个机制，本模块聚焦参与对抗的**两个角色各自的网络结构**。它们是一对镜像：

- **判别器 Discriminator**：就是一个**普通的图像分类网络**（来自 [u3-l2](u3-l2-convnets-architectures.md)）。最简单时是全连接分类器；更常用的是 CNN。它把图像压成一个标量概率：这张图有多大概率是真的。
- **生成器 Generator**：可以看作**反向的判别器**，或等价于自编码器的**解码器**（来自 [u3-l4](u3-l4-autoencoders-vae.md)）。它从一个**潜在向量**（latent vector，和 VAE 的潜在空间同源）出发，逐层放大成一张完整图像。

当生成器和判别器都用**卷积**实现时，这种 GAN 叫 **DCGAN（Deep Convolutional GAN）**。它的标志性差异是：生成器用**转置卷积（ConvTranspose2d）**把小特征图逐步放大（上采样），判别器用普通卷积把图像逐步缩小（下采样）。

#### 4.2.2 核心流程

先看**线性 GAN**（全连接版）在 MNIST 上的结构走向：

```
生成器 Generator（把噪声放大成 28×28 图）：
  噪声 z (100维)
    → Linear 100→256  + BatchNorm1d + LeakyReLU
    → Linear 256→512 + BatchNorm1d + LeakyReLU
    → Linear 512→1024+ BatchNorm1d + LeakyReLU
    → Linear 1024→784 + Tanh
    → reshape 成 (1, 28, 28)   # 784 = 28×28

判别器 Discriminator（把图压成真假概率）：
  图像 (1,28,28) 展平成 784
    → Linear 784→512  + LeakyReLU
    → Linear 512→256  + LeakyReLU
    → Linear 256→1    + Sigmoid   # 输出 [0,1] 概率
```

这里有几个**生成器专属技巧**（课程 Notebook 专门用一段 markdown 解释）：

- 用 **LeakyReLU** 而不是 ReLU：对负值保留一个很小的斜率，避免「死神经元」让梯度消失。
- 用 **BatchNorm** 稳定训练（GAN 对训练稳定性极敏感）。
- 最后一层用 **Tanh**，把输出压到 \([-1,1]\) 区间（与输入图像归一化到 \([-1,1]\) 对齐）。
- 判别器最后一层用 **Sigmoid**，输出 \([0,1]\) 的真假概率。

**DCGAN** 把上述线性层换成卷积：

- 生成器用 `nn.ConvTranspose2d`（转置卷积）做上采样：100→256→128→64→1 通道，逐层把空间尺寸放大；
- 判别器用 `nn.Conv2d` 做下采样：1→64→128→256→1 通道，逐层缩小；
- 还要按 DCGAN 论文做**权重初始化**：卷积层权重用 \(\mathcal{N}(0, 0.02)\)，BatchNorm 层权重用 \(\mathcal{N}(1, 0.02)\)、偏置置 0。

损失函数统一用 **二元交叉熵 BCELoss**，优化器用 **Adam**，且有一个 GAN 特有的超参数习惯：**`beta1 = 0.5`**（默认是 0.9）。降低动量项能减少训练震荡，是 GAN 实践中的常用技巧。

#### 4.2.3 源码精读

线性 GAN 的两个网络（`GANPyTorch.ipynb`，按 cell 定位）：

- [GANPyTorch.ipynb（`Generator` 类所在 cell）](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/10-GANs/GANPyTorch.ipynb) — 生成器：4 层 `Linear`（100→256→512→1024→784），每层夹 `BatchNorm1d` + `LeakyReLU(0.2)`，末层 `Tanh` 并 reshape 成 `(1,28,28)`。它就是「把 100 维噪声放大成手写数字」的解码器。
- [GANPyTorch.ipynb（紧随其后的 markdown cell，讲生成器技巧）](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/10-GANs/GANPyTorch.ipynb) — 课程原文解释三条技巧：LeakyReLU 替代 ReLU、BatchNorm1d 稳定训练、末层 Tanh 把输出限制在 \([-1,1]\)。
- [GANPyTorch.ipynb（`Discriminator` 类所在 cell）](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/10-GANs/GANPyTorch.ipynb) — 判别器：3 层 `Linear`（784→512→256→1），夹 `LeakyReLU`，末层 `Sigmoid` 输出真假概率。本质就是图像二分类器。

实例化与超参数：

- [GANPyTorch.ipynb（超参数 cell：`device`/`lr`/`beta1`/`batch_size`/`epochs`）](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/10-GANs/GANPyTorch.ipynb) — 关键值：`lr = 2e-4`、`beta1 = 0.5`、`beta2 = 0.999`、`batch_size = 256`、`epochs = 100`。其中 `beta1=0.5` 是 GAN 区别于普通训练的标志性设置。
- [GANPyTorch.ipynb（实例化 + Adam + `loss_fn = nn.BCELoss()` 的 cell）](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/10-GANs/GANPyTorch.ipynb) — 用 `BCELoss` 作对抗损失，两个网络各配一个 Adam 优化器。

DCGAN 的卷积结构与权重初始化：

- [GANPyTorch.ipynb（`DCGenerator` 所在 cell）](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/10-GANs/GANPyTorch.ipynb) — 用 `nn.ConvTranspose2d` 做 4 次上采样（100→256→128→64→1），夹 `BatchNorm2d` + `ReLU`，末层 `Tanh`。
- [GANPyTorch.ipynb（`DCDiscriminator` 所在 cell）](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/10-GANs/GANPyTorch.ipynb) — 用 `nn.Conv2d` 做 4 次下采样（1→64→128→256→1），夹 `BatchNorm2d` + `LeakyReLU`，末层 `Sigmoid`。
- [GANPyTorch.ipynb（`weights_init` 所在 cell）](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/10-GANs/GANPyTorch.ipynb) — 按 DCGAN 论文初始化：卷积层权重 `N(0, 0.02)`，BatchNorm 权重 `N(1, 0.02)`、偏置置 0；随后用 `generator.apply(weights_init)` 应用到整个模型。

> 对照点：DCGAN 训练前会把输入图像重新映射到 \([-1,1]\)（`imgs = 2.0 * imgs - 1.0`），与生成器末层 `Tanh` 的输出范围对齐——这正是「输入输出归一化范围必须一致」的工程细节，呼应 [u3-l3](u3-l3-transfer-learning.md) 强调的「预处理要和预训练时一致」。

#### 4.2.4 代码实践（结构阅读型）

1. **实践目标**：对比线性 GAN 与 DCGAN 的生成器，理解「转置卷积上采样」如何替代「线性层放大」。
2. **操作步骤**：
   - 在 `GANPyTorch.ipynb` 中分别打开 `Generator`（线性）与 `DCGenerator`（卷积）两个 cell。
   - 画两张结构图：一张是「100→256→512→1024→784 的线性放大」，一张是「100→256→128→64→1 的转置卷积上采样」，标出每一步的空间尺寸变化。
   - 思考：为什么生成器用 `ConvTranspose2d` 而判别器用 `Conv2d`？（提示：一个要放大图像，一个要缩小图像。）
3. **需要观察的现象**：线性版生成的是「展平的 784 维再 reshape」，而 DCGAN 版全程保持二维特征图结构。
4. **预期结果**：你能用自己的话说出「生成器 = 反向判别器 = 自编码器解码器」这三者的等价关系。
5. 若想真正跑通 DCGAN 的彩色版（如 CIFAR-10 单类），效果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：生成器最后一层为什么用 `Tanh`，而判别器最后一层用 `Sigmoid`？

> **参考答案**：判别器要输出「这张图是真图的概率」，是 \([0,1]\)，所以用 `Sigmoid`。生成器输出的像素要和训练图像的取值范围对齐——课程里图像被归一化到 \([-1,1]\)（`transforms.Normalize(mean=0.5, std=0.5)` 或 `2*imgs-1`），`Tanh` 正好把输出压到 \([-1,1]\)，两端对齐才能让判别器公平比较。

**练习 2**：GAN 的 Adam 优化器为什么常把 `beta1` 设成 0.5 而不是默认的 0.9？

> **参考答案**：`beta1` 控制一阶矩（动量）的衰减。GAN 的损失景观震荡剧烈，默认的 0.9 动量太大、会把历史梯度过度平滑，导致训练不稳；降到 0.5 减弱动量惯性，能让生成器/判别器更快响应当前的对抗形势，是 GAN 实践中的经验设置。

---

### 4.3 神经风格迁移

#### 4.3.1 概念说明

GAN 是**训练网络**来生成图像；**神经风格迁移（Neural Style Transfer）**走的是另一条路：它**根本不训练网络**，而是**直接优化一张图像的像素**。

它的目标很有诗意：给定一张**内容图**（content，比如你的照片）和一张**风格图**（style，比如梵高的《星月夜》），生成一张新图——**结构内容来自内容图，笔触色调来自风格图**。

为什么能做到？关键在于复用一个**预训练 CNN（课程用 VGG16）**当「特征提取器」，而且**冻结它的权重**（`vgg.trainable = False`，这正是 [u3-l3](u3-l3-transfer-learning.md) 讲的迁移学习用法）。CNN 的不同层天然编码了不同信息：

- **浅层**特征描述局部纹理、颜色、笔触（风格）；
- **深层**特征描述「这里有一只猫」这类整体内容（内容）。

于是风格迁移的整套思路是：**从一张噪声图出发，反复调整它的像素，让它在 VGG 的特征空间里「既像内容图（深层特征接近）、又像风格图（浅层纹理接近）」**。这与 [u3-l3](u3-l3-transfer-learning.md) 提到的「对抗样本通过优化输入像素骗过网络」是同一族技巧——都是**对输入做梯度下降**。

#### 4.3.2 核心流程

总损失由三部分加权而成（课程 Notebook 用 \(\alpha,\beta,\gamma\) 表示权重）：

\[
\mathcal{L}(x) = \alpha\,\mathcal{L}_c(x, i) + \beta\,\mathcal{L}_s(x, s) + \gamma\,\mathcal{L}_t(x)
\]

其中 \(x\) 是当前正在优化的图像，\(i\) 是内容图，\(s\) 是风格图。

**① 内容损失 \(\mathcal{L}_c\)：用深层特征比「像不像内容」**

在 VGG 某个较深的层（课程选 `block4_conv2`）提取当前图与内容图的特征图，算逐元素平方误差：

\[
\mathcal{L}_c = \frac{1}{2}\sum_{i,j}\big(F_{ij}^{(l)} - P_{ij}^{(l)}\big)^2
\]

其中 \(F^{(l)}\) 是当前图在第 \(l\) 层的特征，\(P^{(l)}\) 是内容图的特征。深层特征对像素级细节不敏感、只保留「有什么、怎么排布」，所以这一项约束「内容不变」。

**② 风格损失 \(\mathcal{L}_s\)：用 Gram 矩阵比「纹理像不像」**

这是风格迁移的**核心技巧**。直接比特征会连内容一起比，不行。改比特征的 **Gram 矩阵**：

\[
G = A\cdot A^{\top}
\]

Gram 矩阵衡量的是「不同卷积核响应之间的相关性」，类似于相关性矩阵，它刻画了**纹理、笔触、配色**这些「风格」，而丢弃了空间位置信息（内容）。风格损失在多个层（课程选 `block1_conv1`、`block2_conv1`、`block3_conv1`、`block4_conv1`）上分别算 Gram 矩阵的均方误差再取平均，从而同时捕捉粗细不同尺度的风格。

**③ 全变分损失 \(\mathcal{L}_t\)：让图像更平滑、少噪点**

对相邻像素做差，惩罚剧烈跳变：

\[
\mathcal{L}_t = \sum |x_{i,j} - x_{i,j-1}| + \sum |x_{i,j} - x_{i-1,j}|
\]

它不关乎内容或风格，只为了让结果图**干净平滑**。

**优化过程**：注意，**被优化的不是网络权重，而是图像 \(x\) 本身**。把 \(x\) 设成 `tf.Variable`，用 Adam 在像素上做梯度下降，每一步用 `GradientTape` 算 \(\partial\mathcal{L}/\partial x\) 再更新像素。VGG 全程冻结。

课程 Notebook 给出两组权重对比：

- 不含全变分：`total_loss = 2*content_loss + style_loss`（内容权重高，从纯噪声出发）；
- 含全变分：`total_loss = content_loss + 150*style_loss + 30*variation_loss`（风格权重极高，因为这时从**内容图加噪声**出发，内容已经基本保留，可以放手追求风格）。

#### 4.3.3 源码精读

总损失的设计意图写在 Notebook 开头：

- [StyleTransfer.ipynb:L3-L13](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/10-GANs/StyleTransfer.ipynb#L3-L13) — 课程原文给出三部分损失公式 \(\mathcal{L}(x)=\alpha\mathcal{L}_c+\beta\mathcal{L}_s+\gamma\mathcal{L}_t\)，并解释内容损失衡量「当前图与内容图的接近度」、风格损失衡量「与风格图的接近度」、全变分损失让图像平滑。

加载并冻结 VGG：

- [StyleTransfer.ipynb:L176-L177](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/10-GANs/StyleTransfer.ipynb#L176-L177) — `vgg = tf.keras.applications.VGG16(include_top=False, weights='imagenet')` 后立即 `vgg.trainable = False`。VGG 只作特征提取器，权重永不更新（与 [u3-l3](u3-l3-transfer-learning.md) 冻结预训练模型一脉相承）。

特征提取辅助函数：

- [StyleTransfer.ipynb:L274-L277](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/10-GANs/StyleTransfer.ipynb#L274-L277) — `layer_extractor(layers)` 用 `tf.keras.Model([vgg.input], outputs)` 造一个「输出指定中间层特征」的子模型，让我们能取到任意层的特征图。

内容损失（深层特征 MSE）：

- [StyleTransfer.ipynb:L306-L313](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/10-GANs/StyleTransfer.ipynb#L306-L313) — 选 `content_layers = ['block4_conv2']`，预先算好内容图的目标特征 `content_target`；`content_loss(img)` 把当前图的同层特征与目标做 `0.5*tf.reduce_sum((z-content_target)**2)`。

风格损失（Gram 矩阵 + 多层）：

- [StyleTransfer.ipynb:L398-L403](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/10-GANs/StyleTransfer.ipynb#L398-L403) — `gram_matrix(x)` 用 `tf.linalg.einsum('bijc,bijd->bcd', x, x)` 一步算出 Gram 矩阵并按像素数归一化，对应 \(G=A A^{\top}\)。
- [StyleTransfer.ipynb:L404-L416](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/10-GANs/StyleTransfer.ipynb#L404-L416) — `style_layers` 选 4 个 `blockN_conv1`；`style_loss(img)` 在多层上分别比 Gram 矩阵的均方误差再取平均。

对图像（而非网络）做梯度下降：

- [StyleTransfer.ipynb:L352-L361](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/10-GANs/StyleTransfer.ipynb#L352-L361) — `optimize(img, loss_fn)` 用 `tf.GradientTape()` 算 \(\partial\mathcal{L}/\partial\text{img}\) 再用 `opt.apply_gradients` 更新；`train(...)` 外层 epoch、内层 `steps_per_epoch` 步。注意 `img` 是被优化的 `tf.Variable`，VGG 始终冻结。

三组损失汇总与权重对比：

- [StyleTransfer.ipynb:L453-L454](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/10-GANs/StyleTransfer.ipynb#L453-L454) — `total_loss(img) = 2*content_loss(img) + style_loss(img)`，从纯噪声 `img_result` 出发优化。
- [StyleTransfer.ipynb:L508-L516](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/10-GANs/StyleTransfer.ipynb#L508-L516) — 加入全变分损失：`variation_loss` 惩罚相邻像素差；`total_loss_var = content_loss + 150*style_loss + 30*variation_loss`，且从「内容图 + 噪声」出发，保留更多内容细节。

最终保存：

- [StyleTransfer.ipynb（保存 cell，`cv2.imwrite('result.jpg', ...)`）](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/10-GANs/StyleTransfer.ipynb) — 把优化好的图像以 BGR 顺序写回 `result.jpg`（呼应 [u3-l1](u3-l1-intro-opencv.md) 讲的 OpenCV 默认 BGR 约定）。

> 对比记忆：GAN 里 `tape.gradient`/`backward` 的梯度更新的是**网络参数**；这里更新的是**图像像素**。这是两类生成技术最根本的差别。

#### 4.3.4 代码实践（运行型 —— 本讲主实践）

这是本讲的核心动手任务，对应课程的 Challenge 与 Assignment。

1. **实践目标**：用自己的照片和一幅风格画，跑出一张风格迁移图并保存。
2. **操作步骤**：
   - 在 `ai4beg` 环境（见 [u1-l3](u1-l3-environment-setup.md)）启动 Jupyter，打开 `StyleTransfer.ipynb`。
   - 准备两张图：一张你的**内容图**（比如人像或风景照），一张**风格图**（名画、抽象画皆可，色彩笔触鲜明效果更好）。
   - 替换下载 cell 里的两个 URL，或直接把图片放到 `images/` 目录并改名为 `image.jpg`（内容）和 `style.jpg`（风格）。
   - 从上到下依次运行所有 cell，直到 `total_loss_var` 训练 cell。若效果不够，**重复运行**该 cell（Notebook 注明「即便有 GPU 也较慢，可多跑几轮」）。
   - 运行最后的 `cv2.imwrite('result.jpg', ...)`，把结果存盘。
3. **需要观察的现象**：
   - 只用内容损失训练时（`train(img, content_loss)`），随机噪声图会逐步「长出」内容图的大致轮廓；
   - 加上风格损失后，图像会染上风格图的色调与笔触；
   - 调权重：把 `total_loss_var` 中 `150*style_loss` 的系数改小（如 30），观察风格变弱、内容更清晰。
4. **预期结果**：得到一张 `result.jpg`——内容结构来自你的照片，纹理色调来自风格画。
5. 训练耗时与最终美感**待本地验证**，强烈建议在带 GPU 的环境（如 Colab，见 [u1-l3](u1-l3-environment-setup.md)）运行，否则 CPU 上会很慢。

#### 4.3.5 小练习与答案

**练习 1**：风格迁移里，为什么风格损失要比 Gram 矩阵，而不是直接比特征图本身？

> **参考答案**：直接比特征会把风格图的「内容（空间布局）」也比进去，导致结果只是把风格图复制过来。Gram 矩阵 \(G=A A^{\top}\) 只保留「不同卷积核响应之间的相关性」、丢弃空间位置，因此刻画的是纹理、配色、笔触这类与位置无关的「风格」，不会强加风格图的具体内容。

**练习 2**：风格迁移和 GAN 在「被优化的对象」上有什么根本区别？

> **参考答案**：GAN 通过反向传播更新**生成器和判别器的网络权重**，网络固定后再输入噪声生成图像；风格迁移则**冻结**预训练 VGG 的权重，把**图像本身**当作 `tf.Variable`，用梯度下降直接调整**像素**，使图像在特征空间里同时逼近内容和风格。前者训练模型，后者训练一张图。

**练习 3**：内容损失用深层（`block4_conv2`）、风格损失用多个层（`block1~4_conv1`），为什么这样搭配？

> **参考答案**：深层特征语义抽象、保留「有什么内容」而丢失像素细节，适合约束内容不变；浅层特征捕捉局部纹理笔触，多个层组合能同时描述粗、中、细不同尺度的风格。所以内容用单层深层、风格用多层浅层是兼顾「保内容」与「换风格」的经典搭配。

---

## 5. 综合实践

把本讲三个模块串成一个完整任务：**对比「训练生成模型」与「优化单张图像」两种生成路径**。

任务步骤：

1. **跑通 GAN（对应 4.1 + 4.2）**：在 `GANPyTorch.ipynb` 中训练线性 GAN（先跑少量 epoch 即可），用 `plotn` 观察生成器从噪声逐步学会画数字。记录生成器损失与判别器损失的震荡曲线。
2. **跑通风格迁移（对应 4.3）**：用同一张内容图（比如一张数字截图或你自己的照片），在 `StyleTransfer.ipynb` 里换一种风格图，生成迁移结果。
3. **写一份对比报告**（200 字以内），回答：
   - GAN 训练的是**什么**？风格迁移优化的是**什么**？
   - 哪种方法生成一次新图后能**反复快速生成**？哪种每次都要**重新优化**？
   - 各自适合什么场景？（提示：GAN 适合「学一个分布后大量采样」，风格迁移适合「一次性艺术化某张特定图」。）

> 进阶（可选）：把风格迁移的 `style_layers` 或权重 \(\beta\) 改一改，生成同一张内容图、不同风格强度的系列图，体会三个损失权重的权衡。

---

## 6. 本讲小结

- GAN 用**生成器（造假者）与判别器（鉴定者）的对抗博弈**逼出生成质量，是一个两人极小极大博弈，理想情况下两个损失会**震荡**而非单调下降。
- 每个训练步分**两阶段交替**：先冻结判别器、用「假图标真」训练生成器；再用真图（标 1）与 detach 的假图（标 0）训练判别器；`detach()` 防止梯度回传生成器是关键。
- 生成器是「反向判别器 / 自编码器解码器」，线性版用 `Linear`+`Tanh`，DCGAN 版用 `ConvTranspose2d` 做上采样；判别器是普通分类器，末层 `Sigmoid`；损失用 `BCELoss`，Adam 的 `beta1=0.5` 是 GAN 经验设置。
- GAN 训练难：**模式崩溃、超参敏感、生成器/判别器失衡、高分辨率难训**，可用差异学习率、progressive growing 等缓解。
- 神经风格迁移**不训练网络**，而是冻结预训练 VGG、**对图像像素做梯度下降**，让它在特征空间同时逼近内容图与风格图。
- 风格迁移三损失：**内容损失**（深层特征 MSE）、**风格损失**（多层 Gram 矩阵 MSE，核心技巧）、**全变分损失**（相邻像素平滑）；总损失 \(\mathcal{L}=\alpha\mathcal{L}_c+\beta\mathcal{L}_s+\gamma\mathcal{L}_t\)。

---

## 7. 下一步学习建议

本讲是计算机视觉单元（第 IV 单元）的最后一节生成主题课。接下来可以：

- 进入**目标检测**（[u3-l6](u3-l6-object-detection.md)）与**语义分割/U-Net**（[u3-l7](u3-l7-segmentation-unet.md)），把视觉从「整图分类/生成」推进到「定位物体」和「像素级标注」。
- 若对生成模型意犹未尽，可阅读 README 推荐的 [StyleGAN](https://en.wikipedia.org/wiki/StyleGAN) 与 [GAN 原论文](https://arxiv.org/abs/1406.2661)、[DCGAN 论文](https://arxiv.org/abs/1511.06434)，理解现代高保真生成架构。
- 想把「多模态」也纳入视野，可预习附加内容 [u5-l4 多模态网络 CLIP](u5-l4-multimodal-clip.md)，看图文对齐如何与生成结合。
- 建议继续阅读 `lessons/4-ComputerVision/10-GANs/README.md` 的「Review & Self Study」一节，里面有训练 GAN 一年的 10 条经验等进阶资料。
