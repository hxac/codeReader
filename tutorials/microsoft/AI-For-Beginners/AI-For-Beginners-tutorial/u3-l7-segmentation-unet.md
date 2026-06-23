# 语义分割与 U-Net

## 1. 本讲目标

本讲是计算机视觉单元的收尾课。前面我们已经学过图像分类（整张图一个标签）、目标检测（画框＋类别）。本讲把精度推到极限——**为图像里的每一个像素单独预测类别**，这就是「语义分割」。

学完后你应当能够：

- 区分**语义分割（semantic segmentation）**、**实例分割（instance segmentation）**、**全景分割（panoptic segmentation）**三种任务。
- 读懂分割网络通用的「编码器—解码器」结构，并理解 **U-Net** 的核心创新：**跳跃连接（skip connection）**为什么能恢复精确的像素位置。
- 理解分割任务为什么用**逐像素交叉熵 / 二元交叉熵**而不是均方误差，并能说出像素准确率（pixel accuracy）这一最朴素的评估指标。
- 跟着 `SemanticSegmentationPytorch.ipynb` 看懂 SegNet 与 U-Net 的 PyTorch 实现，并有能力把 U-Net 搬到 lab 里的 BodySegmentation 人体抠图任务上。

## 2. 前置知识

本讲默认你已经掌握以下内容（对应前面几讲）：

- **卷积神经网络（CNN）**：卷积、池化、感受野、金字塔结构（见 u3-l2）。本讲的编码器就是一个标准金字塔 CNN。
- **自编码器（AE）**的「编码器→瓶颈→解码器」沙漏结构（见 u3-l4）。分割网络正是受自编码器启发，只是把重建目标从「原图」换成了「掩膜（mask）」。
- **PyTorch 训练五件套**：`zero_grad → forward → loss → backward → step`（见 u2-l5）。
- **二分类的输出与损失配对**：`sigmoid + Binary Cross Entropy`（见 u2-l5）。

几个本讲会用到的关键术语，先用一句话建立直觉：

- **掩膜（mask）**：和原图同尺寸的「答案图」，每个像素的值代表它的类别。本讲里的医学病灶是二分类（病灶 / 背景），所以掩膜是黑白图（0 或 1）。
- **逐像素分类（pixel classification）**：把「给整张图打一个标签」改写成「给每个像素打一个标签」，于是分割在数学上等价于「对 H×W 个像素各做一次分类」。
- **上采样（upsampling）**：把低分辨率特征图放大回高分辨率，是解码器把「粗糙特征」还原成「精细掩膜」的关键操作。

## 3. 本讲源码地图

本讲只涉及一个课程目录 `lessons/4-ComputerVision/12-Segmentation/`，但里面包含两类文件：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/12-Segmentation/README.md) | 课程的文字讲义：定义分割任务、画出编码器—解码器示意图、说明损失函数、布置作业。 |
| [SemanticSegmentationPytorch.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/12-Segmentation/SemanticSegmentationPytorch.ipynb) | 本讲的主 Notebook：在 PH² 皮肤病医学图像上，先实现 **SegNet**（无跳跃连接的编码器—解码器），再实现 **U-Net**（带跳跃连接）并对比。 |
| [lab/README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/12-Segmentation/lab/README.md) | lab 说明：用 Kaggle 的 Segmentation Full Body MADS 数据集做人体抠图。 |
| [lab/BodySegmentation.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/12-Segmentation/lab/BodySegmentation.ipynb) | lab 的起始 Notebook：只给出数据加载骨架，U-Net 与训练循环需要你自己搬过来补全。 |

> 找东西口诀（沿用前面几讲）：**读概念→README，看实现→主 Notebook，做实践→lab**。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：

1. **分割任务定义**——分清三种分割，理解「分割＝逐像素分类」。
2. **U-Net 结构**——编码器—解码器 ＋ 跳跃连接，这是本讲的重头戏。
3. **像素级损失与评估**——为什么用交叉熵而非 MSE，以及如何量化分割质量。

---

### 4.1 分割任务定义

#### 4.1.1 概念说明

上一讲目标检测给出的「边界框」是矩形，只能粗略框住物体。但医学影像里病灶的形状千奇百怪，自动驾驶里行人紧贴车辆，矩形框会把背景也框进来。**分割（segmentation）**给出的是像素级的精确轮廓。

README 把分割定义得很干脆：

> Segmentation can be viewed as **pixel classification**, whereas for **each** pixel of image we must predict its class (*background* being one of the classes).

也就是：对**每一个**像素预测一个类别（「背景」也算一个类别）。这一句话把分割从「整图任务」拉回到了我们已经熟悉的「分类」框架。

但「像素分类」还有两种粒度，必须分清：

| 任务 | 同类多个物体怎么处理 | 典型例子 |
| --- | --- | --- |
| **语义分割 Semantic** | 所有同类像素合并成一坨，不区分个体 | 10 只羊都标成同一个「羊」色块 |
| **实例分割 Instance** | 既分类别，又区分同类中的不同个体 | 10 只羊分成羊 #1、羊 #2……羊 #10 |
| **全景分割 Panoptic** | 语义分割＋实例分割，背景也纳入 | 自驾场景：道路（stuff）＋每辆车（thing）|

README 用一群羊举例：实例分割里这些羊是「不同对象」，语义分割里它们被合并成「一个类」。本讲只做**语义分割**（而且是二分类：病灶 vs 背景），panoptic 分割 README 提示读者自行扩展阅读。

#### 4.1.2 核心流程

一个语义分割系统从输入到输出的流水线：

1. **输入**：一张 RGB 图像，张量形状 \((3, H, W)\)。
2. **前向网络**：经过编码器—解码器，输出一张与输入**同空间尺寸**、通道数等于类别数的「分数图」。
3. **逐像素判类**：在每个像素位置取分数最大的通道作为该像素的类别（二分类则用阈值 0.5）。
4. **得到掩膜**：输出形状 \((C, H, W)\) 的预测，其中 \(C\) 是类别数。本讲 \(C=1\)（病灶）。

用一句话概括分割网络的形状约束：

\[
\text{输入}(3,H,W)\ \xrightarrow{\text{网络}}\ \text{输出}(C,H,W)
\]

**输出必须和输入同样大**——这是分割区别于分类（输出一个向量）和检测（输出若干框）的根本所在。

#### 4.1.3 源码精读

README 用两段话把任务定义钉死。先看它如何区分两类分割：

[README.md:L7-L12](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/12-Segmentation/README.md#L7-L12)：把分割定义为「逐像素分类」，并区分语义分割（只给类别，不区分同类个体）与实例分割（把同类划分成不同实例）。

接着 README 点出**所有分割网络共有的结构**——这其实是本讲第二模块的引子：

[README.md:L18-L23](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/12-Segmentation/README.md#L18-L23)：任何分割网络都由 **Encoder（编码器，提取特征）** 与 **Decoder（解码器，把特征还原成与原图同尺寸、通道数等于类别数的掩膜）** 两部分组成。注意它特别提到——这和自编码器很像，只不过目标是重建**掩膜**而非原图。

再看数据本身长什么样。主 Notebook 的 `load_dataset` 函数把 PH² 皮肤病数据集加载成「图像＋掩膜」对，关键在于掩膜是如何二值化的：

[SemanticSegmentationPytorch.ipynb:L161](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/12-Segmentation/SemanticSegmentationPytorch.ipynb#L161)：对掩膜做 `resize(...) > 0.5`，把连续灰度压成 0/1 的二值掩膜，再 `unsqueeze(1)` 补上通道维，最终形状为 \((N,1,H,W)\)——每个像素一个 0/1 标签，这正是「逐像素二分类」的 ground truth。

数据集随即被随机打乱并切成训练 / 测试两部分：

[SemanticSegmentationPytorch.ipynb:L163](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/12-Segmentation/SemanticSegmentationPytorch.ipynb#L163)：用 `np.random.permutation` 打乱索引，按 `train_size=0.9` 切分 90% 训练 / 10% 测试。

#### 4.1.4 代码实践

**实践目标**：亲手确认「分割＝逐像素分类」，并看清掩膜的数据形状。

**操作步骤**：

1. 在 `ai4beg` 环境里打开主 Notebook（环境搭建见 u1-l3）。
2. 依次运行到 `load_dataset` 与 `plotn` 之间的 cell，加载好 `train_dataset`、`test_dataset`。
3. 新增一个 cell，键入下面这段**示例代码**（非项目原有代码）来检查形状：

   ```python
   imgs, masks = train_dataset[0], train_dataset[1]
   print("图像 batch 形状:", imgs.shape)   # 期望 (N, 3, 256, 256)
   print("掩膜 batch 形状:", masks.shape)   # 期望 (N, 1, 256, 256)
   print("掩膜唯一取值:", torch.unique(masks))  # 期望只有 0.0 和 1.0
   ```

4. 运行 README 里提到的 `plotn(5, train_dataset)`，对照原图与掩膜。

**需要观察的现象**：

- 图像张量是 4 维 \((N,3,256,256)\)，掩膜张量通道数为 **1**（二分类），且空间尺寸与图像完全一致。
- 掩膜的取值只有 0（背景）和 1（病灶）两种——这正是「每个像素一个类别标签」。

**预期结果**：你会看到打印出 `图像 batch 形状: torch.Size([N, 3, 256, 256])`、`掩膜 batch 形状: torch.Size([N, 1, 256, 256])`，且掩膜唯一值为 `[0., 1.]`。

**待本地验证**：`N` 的具体数值取决于 `train_size` 与数据集大小（PH² 共 200 张，训练集约 180 张），请以本地实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：医学影像里同一次扫描可能有多颗痣（同类、多个）。如果我们的任务是把每一颗痣单独编号（痣 #1、痣 #2……），这属于哪种分割？本课 Notebook 解决的是哪一种？

> **答案**：单独编号属于**实例分割**；本课 Notebook 只输出一张 0/1 掩膜，不区分个体，属于**语义分割**。

**练习 2**：分割网络的输出通道数由什么决定？为什么二分类病灶任务里输出通道是 1 而不是 2？

> **答案**：由**类别数**决定。二分类时可以用单通道＋sigmoid（输出该像素属于病灶的概率，>0.5 即病灶），所以 1 个通道就够；也可用 2 通道＋softmax，二者等价，本课选择更省事的 1 通道方案。

**练习 3**：为什么说「分割是逐像素分类」这句话能让本章网络的设计直接复用我们已有的分类知识？

> **答案**：因为「每个像素独立判一次类」在数学上等价于在 \(H\times W\) 个空间位置上各做一次分类。于是分类里成熟的交叉熵损失、softmax/sigmoid 输出、准确率指标，都可以「逐像素」地搬过来用。

---

### 4.2 U-Net 结构

#### 4.2.1 概念说明

既然分割网络＝编码器＋解码器，最直接的实现就是 Notebook 里最先给出的 **SegNet**：编码器是一串「卷积＋ReLU＋BatchNorm＋池化」（空间尺寸不断减半），瓶颈处做一次卷积，解码器对称地用「上采样＋卷积」把尺寸逐级放大回去。

但 SegNet 有一个天然缺陷，README 的 U-Net 一节说得很清楚：

> we first apply pyramid CNN architecture to the original image, which reduces the spatial accuracy of image features. Then, when we reconstruct the image, we cannot correctly reconstruct the pixel positions.

翻译过来：金字塔 CNN 一路下采样，**空间位置信息被逐步模糊**；等解码器再一路上采样回去时，已经无法精确还原每个像素本来的位置——于是掩膜的边缘会发虚、错位。

**U-Net 的核心创新**就是用**跳跃连接（skip connection）**修补这个缺陷：在编码器每一级「下采样之前」的高分辨率特征，直接抄送给解码器对应「上采样之后」的同级，让解码器在重建掩膜时能拿到「未经模糊的原始细节」。

为什么叫 U-Net？因为画出来像字母 U：左半边（编码器）逐级下降、右半边（解码器）逐级上升，中间的横向连线就是 skip connection，整体呈 U 形。

> 直觉记忆：**编码器像「压缩记要点」，解码器像「照着要点还原原图」；skip connection 则让解码器还能随时翻看「编码器当时记的高清草稿」，所以边缘不会失真。**

#### 4.2.2 核心流程

U-Net 的前向计算可以拆成三段。设输入为 \(256\times256\)、3 通道：

**第一段：编码器逐级下采样**（左半边 U 的下降段）

每级结构为 `Conv→ReLU→BatchNorm→MaxPool(2×2)`，通道翻倍、空间减半：

| 级 | 输出通道 | 输出空间尺寸 |
| --- | --- | --- |
| e0 | 16 | 128×128 |
| e1 | 32 | 64×64 |
| e2 | 64 | 32×32 |
| e3 | 128 | 16×16 |

随后瓶颈卷积把通道升到 256，空间仍为 16×16。

**第二段：在每一级「下采样之前」抄存高清特征**

这是 skip connection 的关键。注意：抄存的是 `conv→relu→bn` 之后、**池化之前**的那份特征（空间尺寸是「池化后的两倍」），分别记作 `cat0…cat3`，它们的空间尺寸为 256、128、64、32，正好是各级「原始分辨率」。

**第三段：解码器逐级上采样，并在每一级拼接（concat）对应高清特征**（右半边 U 的上升段）

每级：`Upsample(×2) → 与同级 cat 在通道维拼接 → Conv→ReLU→BatchNorm`。拼接后通道数 = 上采样通道数 ＋ 该级 skip 通道数：

| 解码级 | 上采样后（通道/尺寸） | 拼接 skip（通道/尺寸） | 拼接后通道 | 解码卷积输出通道 |
| --- | --- | --- | --- | --- |
| d0 | 256 / 32×32 | cat3 = 128 / 32×32 | **384** | 128 |
| d1 | 128 / 64×64 | cat2 = 64 / 64×64 | **192** | 64 |
| d2 | 64 / 128×128 | cat1 = 32 / 128×128 | **96** | 32 |
| d3 | 32 / 256×256 | cat0 = 16 / 256×256 | **48** | 1 |

最后一级用 `1×1` 卷积把通道压到类别数（这里是 1），再过 `sigmoid` 得到每个像素属于病灶的概率。

**一个关键不变量**：拼接能成立，前提是「上采样后的特征」与「同级 skip 特征」**空间尺寸完全相同**——这正是 U-Net 对称结构的设计意图。通道维拼接后，再由 `3×3` 卷积把「高清细节＋抽象语义」融合。

#### 4.2.3 源码精读

先看作为对照的 **SegNet**——注意它的解码器**没有**拼接，卷积输入通道就是单纯的上采样通道（256、128、64、32）：

[SemanticSegmentationPytorch.ipynb:L278-L336](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/12-Segmentation/SemanticSegmentationPytorch.ipynb#L278-L336)：`SegNet` 类。编码器四级 `enc_conv0..3`＋池化；瓶颈 `bottleneck_conv` 把 128→256；解码器四级 `upsample..dec_conv..`，`dec_conv0` 的 `in_channels=256`（**只是**上采样通道，无 skip）；forward 中 `d0 = ...dec_conv0(upsample0(b))` 直接对上采样结果卷积，不与任何编码特征相加。

再看 **U-Net** 的 `__init__`——解码器卷积的输入通道数比 SegNet「胖」了一截，这一截正是 skip 通道：

[SemanticSegmentationPytorch.ipynb:L599-L614](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/12-Segmentation/SemanticSegmentationPytorch.ipynb#L599-L614)：U-Net 解码器的四级卷积 `dec_conv0..3` 的 `in_channels` 分别是 **384 / 192 / 96 / 48**。对比 SegNet 的 256 / 128 / 64 / 32，多出来的 128 / 64 / 32 / 16 正好等于各级 skip 特征 `cat3 / cat2 / cat1 / cat0` 的通道数。例如 `dec_conv0` 的 `in_channels=384 = 256(上采样) + 128(cat3)`。

接着看 skip 特征是如何「抄存」出来的（在池化之前的那一份）：

[SemanticSegmentationPytorch.ipynb:L624-L628](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/12-Segmentation/SemanticSegmentationPytorch.ipynb#L624-L628)：`cat0..cat3` 分别是 `bn(act(enc_conv(x/e0/e1/e2)))`，即**池化之前**的高分辨率特征。代码用「把卷积再算一遍但不接池化」的方式显式取出它们（实现上略有冗余、但语义清晰）。

最后看 forward 里 skip 是如何「拼接 + 融合」的：

[SemanticSegmentationPytorch.ipynb:L631-L635](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/12-Segmentation/SemanticSegmentationPytorch.ipynb#L631-L635)：`d0 = dec_conv0(torch.cat((upsample0(b), cat3), dim=1))` —— 先把瓶颈上采样的结果与同级 `cat3` 在**通道维（dim=1）**拼接，再过 `3×3` 卷积融合；`d1/d2/d3` 同理，逐级 `torch.cat` 上采样结果与对应 `cat`。末层 `dec_conv3`（`1×1` 卷积）输出 1 通道，过 `sigmoid` 得到逐像素概率，返回 `d3`。

> 对比小结：把 SegNet 与 U-Net 的解码卷积输入通道并排放，就能一眼看出「U-Net 多出来的通道就是 skip」——这是本讲最重要的源码阅读收获。

#### 4.2.4 代码实践

**实践目标**：在运行训练之前，先用「假输入」前向跑一次 U-Net，验证输出形状确实是 \((1, 1, 256, 256)\)——即「和输入同尺寸、通道数为类别数」。

**操作步骤**：

1. 先运行定义 `UNet` 类的 cell，以及 `device='cuda:0' if ... else 'cpu'` 所在的 cell。
2. 新增一个 cell，键入下面这段**示例代码**（非项目原有代码）：

   ```python
   net = UNet().to(device)
   dummy = torch.randn(1, 3, 256, 256, device=device)  # 一张假图
   out = net(dummy)
   print("U-Net 输出形状:", out.shape)   # 期望 (1, 1, 256, 256)
   print("取值范围:", out.min().item(), out.max().item())  # 经 sigmoid，应在 [0,1]
   ```

3. 把 `UNet` 换成 `SegNet` 再跑一次，对比输出形状是否一致。

**需要观察的现象**：

- 两个网络的输出空间尺寸都**等于输入**（256×256），通道数都为 1。
- U-Net 输出经过 `sigmoid`，数值落在 \([0,1]\)，可解释为「该像素是病灶的概率」。

**预期结果**：`U-Net 输出形状: torch.Size([1, 1, 256, 256])`；取值范围在 0 到 1 之间。

**待本地验证**：第一次 forward 时，因为 skip 拼接对通道维有严格要求，若你手改网络层数导致尺寸对不上，这里会立刻报维度错误——这正是「同尺寸不变量」在帮你查错。

#### 4.2.5 小练习与答案

**练习 1**：U-Net 的 `dec_conv0` 输入通道是 384。请拆解这 384 是怎么来的；如果有人误把它写成 256（像 SegNet 那样），代码会在哪一步出错？

> **答案**：384 = 上采样瓶颈特征 256 通道 ＋ skip 的 `cat3` 128 通道，沿 `dim=1` 拼接而成。若写成 256，`torch.cat((upsample0(b), cat3), dim=1)` 后实际是 384 通道，与 `dec_conv0` 期望的 256 不匹配，前向时 `Conv2d` 会报「输入通道数不符」的维度错误。

**练习 2**：skip connection 为什么必须在「池化之前」抄存特征，而不是抄存 `e0/e1/...`（已经池化后的特征）？

> **答案**：因为解码器在该级是「先上采样再拼接」，上采样后的特征尺寸恰好等于「池化之前的尺寸」。抄存池化前的特征才能保证二者**空间尺寸一致**、可以沿通道拼接；若抄存池化后的特征，尺寸只有一半，`torch.cat` 会因尺寸不匹配而失败。

**练习 3**：README 说 U-Net 也可以用 ResNet-50 当编码器。若把本课的简单编码器换成 ResNet-50，网络的哪一部分几乎不用改、哪一部分需要相应调整？

> **答案**：解码器与 skip 拼接的**结构思想**几乎不用改——仍然是在各级「同尺寸」处拼接编码特征与上采样特征。需要调整的是各级的**通道数**（ResNet-50 各层通道数不同）和**级数/下采样次数**，相应的 `dec_conv` 的 `in_channels` 与 skip 抄存点都要按 ResNet-50 的实际输出重算。

---

### 4.3 像素级损失与评估

#### 4.3.1 概念说明

有了网络结构，还要回答两个问题：**怎么衡量预测好坏（损失）**，以及**训练完怎么打分（评估）**。

**损失函数**。回顾自编码器（u3-l4）：那时预测和目标都是「图像」，用**均方误差 MSE** 衡量两张图的相似度即可。但分割的目标是掩膜——每个像素是「类别编号」（本课是 0/1），这是**分类**语境，所以要用分类专用的**交叉熵（cross-entropy）**，并在所有像素上求平均。

- 多类分割：用 `CrossEntropyLoss`，掩膜在通道维做 one-hot，损失对每个像素算一次、再对所有像素取平均。
- 二分类分割（本课）：用**二元交叉熵 BCE**。Notebook 用的是 `nn.BCEWithLogitsLoss`，它内部自带 sigmoid，专门接收「未经 sigmoid 的原始分数（logits）」。

**评估指标**。README 只点了最朴素的一个：

> The easiest one to understand is **pixel accuracy** - a percentage of pixels classified correctly.

即**像素准确率**：分对的像素数 ÷ 总像素数。它直观，但在「背景占绝大多数」的医学图像里有盲区——模型哪怕把所有像素都猜成「背景」，准确率也可能很高。所以实战中常用更公平的指标（见练习与延伸阅读），本课 Notebook 则采用「直接可视化预测掩膜 vs 真实掩膜」的直观评估。

#### 4.3.2 核心流程

二分类分割的损失与评估流程：

1. 网络输出 logits \(z\in\mathbb{R}^{1\times H\times W}\)（本课 forward 末尾多套了一层 `sigmoid`，见下方「源码精读」的提醒）。
2. 损失：对每个像素算二元交叉熵，再对所有像素取平均。
3. 训练循环与分类完全同构：`forward → loss → backward → step`，外层 epoch 循环。
4. 推理：对输出概率取阈值 0.5，得到 0/1 预测掩膜。
5. 评估：把预测掩膜与真实掩膜并排画出来目测，或算像素准确率。

二元交叉熵的逐像素形式（对单像素，\(y\in\{0,1\}\)，\(p\) 为预测为正类的概率）：

\[
\mathrm{BCE}(y,p) = -\bigl[\,y\log p + (1-y)\log(1-p)\,\bigr]
\]

整张图的损失即对所有像素求平均：

\[
\mathcal{L} = \frac{1}{HW}\sum_{i=1}^{H}\sum_{j=1}^{W}\mathrm{BCE}(y_{ij},\,p_{ij})
\]

像素准确率：

\[
\text{PixelAcc} = \frac{\#\{\text{预测正确的像素}\}}{H\times W}
\]

#### 4.3.3 源码精读

README 用一段话说明损失的选择，并强调二分类用 BCE：

[README.md:L27](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/12-Segmentation/README.md#L27)：分割中每个像素代表类别编号（沿通道维 one-hot），因此要用分类专用的**交叉熵损失并对所有像素取平均**；若掩膜是二值的，则用**二元交叉熵 BCE**。

Notebook 里两处都用同一个损失实例化模型：

[SemanticSegmentationPytorch.ipynb:L364](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/12-Segmentation/SemanticSegmentationPytorch.ipynb#L364)：SegNet 训练配置，`loss_fn = nn.BCEWithLogitsLoss()`，优化器为带 `weight_decay` 正则的 Adam。

[SemanticSegmentationPytorch.ipynb:L656](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/12-Segmentation/SemanticSegmentationPytorch.ipynb#L656)：U-Net 训练配置，同样是 `nn.BCEWithLogitsLoss()`。

> ⚠️ **源码阅读要带批判眼光**：`BCEWithLogitsLoss` 内部**已经包含一次 sigmoid**，而本课两个网络的 `forward` 末尾**又**套了一层 `self.sigmoid(...)`。也就是说，训练时对概率做了「两次 sigmoid」。代码仍能跑通、损失仍会下降（因为 sigmoid 再 sigmoid 仍可导），但这并非最佳实践——更规范的做法是：要么 forward 返回**裸 logits** 配 `BCEWithLogitsLoss`，要么返回概率配 `nn.BCELoss`。这是一个很好的「读源码时不盲信」的练习点（见小练习 3）。

训练循环本身与分类任务的五件套完全一致：

[SemanticSegmentationPytorch.ipynb:L390-L430](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/12-Segmentation/SemanticSegmentationPytorch.ipynb#L390-L430)：`train` 函数。每个 epoch 先 `model.train()` 走训练集——`preds = model(imgs); loss = loss_fn(preds, labels); zero_grad; backward; step`；再 `model.eval()`＋`torch.no_grad()` 在测试集上只算损失不更新参数；最后打印 train/test loss。这正是 u2-l5 学过的标准训练循环，只是这里的 `preds` 和 `labels` 是**整张掩膜**而非单个标签。

推理时用 0.5 阈值把概率压回 0/1 掩膜：

[SemanticSegmentationPytorch.ipynb:L526](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/12-Segmentation/SemanticSegmentationPytorch.ipynb#L526)：SegNet 评估 cell 中 `(model(img).detach().cpu()[0] > 0.5).float()`——对每个像素的概率以 0.5 为界，得到 0/1 预测掩膜，再用 `plotn` 与真实掩膜并排可视化。

Notebook 训练日志显示两网均跑满 30 epochs：SegNet `test loss:=0.577`，U-Net `test loss:=0.572`——U-Net 略胜，但本数据集极小（200 张）且二分类极不平衡，差距主要靠**可视化对比**体现。

#### 4.3.4 代码实践

**实践目标**：亲眼看到「逐像素交叉熵」就是「对每个像素各算一次 BCE 再平均」，并量化一个简单的像素准确率。

**操作步骤**：

1. 跑完 U-Net 的训练 cell（若没有 GPU，可把 `epochs` 调小到 3～5 先看流程）。
2. 运行 U-Net 评估 cell（cell-26），看预测掩膜与真实掩膜对比图。
3. 新增一个 cell，键入下面这段**示例代码**（非项目原有代码），手算 U-Net 在测试集上的像素准确率：

   ```python
   import torch
   images, masks = test_dataset[0], test_dataset[1]
   model.eval()
   correct, total = 0, 0
   with torch.no_grad():
       for img, mask in zip(images, masks):
           x = img.to(device).unsqueeze(0)
           prob = model(x)[0, 0].cpu()       # 预测概率图 (H,W)
           pred = (prob > 0.5).float()
           gt = mask[0]                       # 真实 0/1 掩膜 (H,W)
           correct += (pred == gt).sum().item()
           total += gt.numel()
   print("像素准确率 PixelAcc = %.4f" % (correct / total))
   ```

**需要观察的现象**：

- 即使模型还没训练好，像素准确率也可能不低——因为「背景」像素占绝大多数，全猜背景就能拿高分。
- 训练前后对比：训练充分后，预测掩膜的病灶轮廓应更贴合真实掩膜。

**预期结果**：会打印一个 0～1 之间的 `PixelAcc` 数值；同时直观看到预测掩膜里病灶的白色区域逐渐逼近真实掩膜。

**待本地验证**：具体像素准确率数值取决于训练程度与设备，请以本地输出为准。建议你顺手再算一个「全猜背景」的基线准确率，体会像素准确率在不平衡数据上的「虚高」。

#### 4.3.5 小练习与答案

**练习 1**：为什么自编码器可以用 MSE 作损失，而分割必须用交叉熵？请从「目标是什么」的角度回答。

> **答案**：自编码器的目标是**重建一张图**，预测与目标都是连续像素值，用 MSE 衡量「数值有多接近」很自然；分割的目标是**逐像素分类**，掩膜里的值是**类别编号**而非连续亮度，「类别 1 猜成类别 2」与「亮度差 1」是完全不同的错误，所以要用分类专用的交叉熵。

**练习 2**：在一张背景占 95% 的医学图像上，模型把所有像素都预测成「背景」。它的像素准确率是多少？这说明像素准确率有什么缺陷？

> **答案**：像素准确率约 **95%**——看起来很高，实则一个病灶都没找到。这说明像素准确率**对类别不平衡极度敏感**，会被多数类（背景）「注水」，单独使用容易误导。因此实战中常配合 IoU（交并比）、Dice 系数等指标。

**练习 3**：本课两个网络在 `forward` 末尾都套了 `self.sigmoid`，损失却用了 `nn.BCEWithLogitsLoss`。请指出这里的问题，并给出两种「更规范」的修法。

> **答案**：`BCEWithLogitsLoss` 内部已含一次 sigmoid，于是概率被算了**两次 sigmoid**，改变了损失的数值范围（不致崩，但非最优）。两种规范修法：① `forward` 末尾**去掉** `self.sigmoid`，返回裸 logits，继续用 `BCEWithLogitsLoss`（推荐，数值更稳定）；② 保留 `sigmoid`，把损失换成 `nn.BCELoss`（接收概率）。

---

## 5. 综合实践

把三个模块串成一个完整任务：**完成 lab/README.md 布置的 BodySegmentation——训练 U-Net 给人体抠图，并可视化结果**。这正是本讲的代码实践任务，也是检验你是否真懂 U-Net 的试金石。

任务场景（来自 lab/README.md）：视频制作（如天气预报）常需要把人像从画面里「抠」出来贴到别的背景上。本 lab 用神经网络代替传统的色键（chroma key）抠像，对人体轮廓做语义分割。

**步骤**：

1. **下载数据**：从 Kaggle 手动下载 [Segmentation Full Body MADS Dataset](https://www.kaggle.com/datasets/tapakah68/segmentation-full-body-mads-dataset) 并解压到 lab 目录，确保路径与 `BodySegmentation.ipynb` 里的 `dataset_path = 'segmentation_full_body_mads_dataset_1192_img'` 一致。
2. **读懂骨架**：`BodySegmentation.ipynb` 只给了 `load_image` 等数据加载代码，没有网络、没有训练。它的设计意图就是让你**复用主 Notebook 的成果**。
3. **迁移 U-Net**：把主 Notebook 里的 `UNet` 类、`train` 函数、`nn.BCEWithLogitsLoss` 搬过来，按本 lab 的图像尺寸调整输入。由于是「人 vs 背景」二分类，U-Net 末层 1 通道 + sigmoid/BCE 的配置**无需改动**即可直接套用。
4. **构造 DataLoader**：仿照主 Notebook 的 `load_dataset`，把 `images/` 与 `masks/` 配对、`resize` 到统一尺寸、掩膜二值化（`> 0.5`），再包成 `torch.utils.data.DataLoader`。
5. **训练并可视化**：跑训练循环，用 `plotn` 把预测人体掩膜与真实掩膜并排画出来。

**自检清单**（判断你是否做对了）：

- [ ] U-Net 输出形状与输入图像同尺寸、通道数为 1。
- [ ] skip 拼接处通道数对得上（如 `dec_conv0` 的 `in_channels=384`）；若你改了编码器通道，记得同步改解码器输入通道。
- [ ] 推理时对概率用 0.5 阈值得到 0/1 掩膜，且预测的人体白色区域与真实掩膜大体吻合。
- [ ] （进阶）顺手算一个像素准确率与 IoU，体会不平衡指标的问题。

**预期结果**：训练若干 epoch 后，预测掩膜能勾勒出人体轮廓，可作为进一步「换背景」的 alpha 蒙膜。

**待本地验证**：是否下载到数据、是否有 GPU、训练耗时与最终精度，均取决于本地环境；若暂无 GPU，可把 `epochs` 调小先打通流程。

## 6. 本讲小结

- **分割＝逐像素分类**：对图像每个像素单独预测类别，输出必须与输入同尺寸、通道数等于类别数。语义分割不区分同类个体，实例分割区分，全景分割二者合一。
- **分割网络通用结构是编码器—解码器**：编码器（金字塔 CNN）提取特征、压低分辨率；解码器（上采样＋卷积）把特征还原成与原图同尺寸的掩膜——这是自编码器思想的「换目标」复用。
- **U-Net 的灵魂是跳跃连接**：在编码器每一级「池化之前」抄存高清特征，解码器对应级「上采样之后」沿通道维拼接，使掩膜边缘不失真。源码里 U-Net 解码卷积输入通道（384/192/96/48）比 SegNet（256/128/64/32）多出来的那一截，正是 skip 通道。
- **损失用逐像素交叉熵**：分割目标是类别编号而非亮度，故用交叉熵对所有像素取平均；二分类用 BCE。本课 Notebook 在 `forward` 套 sigmoid 又用 `BCEWithLogitsLoss` 的「双 sigmoid」写法，是值得带着批判眼光读源码的练习点。
- **评估首选可视化，指标用像素准确率但需警惕不平衡**：医学图像背景占绝大多数，像素准确率会被「注水」，实战中常配 IoU / Dice。
- **训练循环与分类同构**：`forward → loss → backward → step`，唯一区别是这里的预测与标签是整张掩膜。

## 7. 下一步学习建议

本讲是计算机视觉单元（u3）的最后一课。建议接下来：

1. **打通 NLP 单元（u4）**：本讲的「编码器—解码器＋跳跃连接」与 NLP 里的 Transformer 编解码器是同一族思想，掌握 U-Net 后再看 u4-l6（Transformer 与 BERT）的自注意力会更顺。
2. **扩展评估指标**：阅读 README 推荐的 [metrics 文章](https://towardsdatascience.com/metrics-to-evaluate-your-semantic-segmentation-model-6bcb99639aa2)，给 BodySegmentation 补上 IoU、Dice 系数，体会它们相对像素准确率的优势。
3. **进阶架构**：README 的 Review 一节提示自行了解 Instance / Panoptic 分割。可进一步阅读 Mask R-CNN（实例分割）、DeepLab 系列（带「空洞卷积／dilated convolution」的语义分割）等 U-Net 之后的主流架构。
4. **修一个小 bug 当贡献**：尝试把本课 Notebook 的「双 sigmoid」问题修成规范写法（裸 logits + `BCEWithLogitsLoss`），对比修复前后的损失曲线——这既是练习，也是一次真实的开源贡献练手（贡献流程见 u6-l5）。
