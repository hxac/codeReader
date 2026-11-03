# 图像变换与标准化

> 讲义编号：u4-l3 ｜ 阶段：intermediate ｜ 前置：u4-l1（数据集、DataLoader 与验证集划分）

## 1. 本讲目标

学完本讲，你应该能够：

1. 用 `transforms.Compose` 把「几何/颜色变换 → ToTensor → Normalize」组织成一条正确的流水线，并说清每一步作用在 PIL 图片还是张量上。
2. 理解逐通道（per-channel）z-score 标准化的计算方式：为什么是 3 个均值 + 3 个标准差（灰度图则是 1 + 1），为什么统计量只能来自训练集。
3. 会用 `helper_data.py` 中的 `UnNormalize` 把标准化后的张量还原成可以 `imshow` 的图片，并理解「训练集挂随机变换、验证/测试集挂确定性变换」的双 transform 模式。

## 2. 前置知识

### 2.1 图片在 PyTorch 中的两种形态

torchvision 的数据集（如 `datasets.MNIST`、`datasets.CIFAR10`）在没有 transform 时返回的是 **PIL 图片**（Python Imaging Library 的 `Image` 对象，类似一张打开的图片，数值范围 0~255，形状是 H×W×C 的「高、宽、通道」布局）。而神经网络需要的是 **张量**（`torch.Tensor`，C×H×W 的「通道、高、宽」布局）。`transforms.ToTensor()` 就是两者之间的桥：它把 PIL 图片（或 ndarray）转成浮点张量，并把像素值从 0~255 缩放到 0~1（即除以 255）。仓库 notebook 里反复出现的那句注释 `# Note transforms.ToTensor() scales input images to 0-1 range` 说的就是这件事。

### 2.2 transform 是什么、在哪里生效

在 u4-l1 我们理清了数据管线的分工：Dataset 管单样本存取，Sampler 管取样顺序，DataLoader 管批量拼装。transform 是挂在 Dataset 上的「单样本处理函数」：DataLoader 每取一个样本，Dataset 的 `__getitem__` 就把这个样本喂给 transform 处理一次（u4-l2 讲自定义 Dataset 时已经见过这个接口）。所以：

- transform 是**逐样本**执行的，与 batch 无关；
- 同一个 Dataset 挂不同的 transform，就得到不同的数据视图——这是本讲「双 transform 模式」的基础。

### 2.3 为什么像素值需要「再」处理

`ToTensor` 之后像素已经在 0~1 之间了，为什么还要折腾？两个直觉：

1. **各通道尺度一致**：RGB 三个通道的亮度分布可能差异很大（比如数据集整体偏红），标准化把每个通道都拉到均值 0、方差 1 附近，让优化地形更「圆」，梯度更新更稳定。
2. **和预训练模型对齐**：如果用 ImageNet 预训练权重（u7-l4 迁移学习），输入必须用与预训练时相同的均值/标准差标准化，否则模型看到的分布完全对不上。

### 2.4 一句话回顾数据泄漏

u1-l2 和 u2-l2 都强调过：**标准化用的统计量只能来自训练集**。测试集/验证集要复用训练集的均值和标准差，而不是自己算一套——否则测试信息就渗进了训练流程，这叫数据泄漏（data leakage）。本讲的 `cnn-standardized.ipynb` 会严格执行这一点。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `pytorch_ipynb/mechanics/torchvision-transform-examples.ipynb` | torchvision 变换的「图鉴」：6 个小节逐个演示 ToTensor、翻转、旋转、Normalize、ColorJitter、Resize+RandomCrop 的效果（CIFAR-10 样例） |
| `pytorch_ipynb/cnn/cnn-standardized.ipynb` | 逐通道标准化的完整案例：增量计算训练集均值/标准差 → 组装 transform → 训练一个小 CNN（MNIST） |
| `pytorch_ipynb/helper_data.py` | 共享模块：`UnNormalize` 类（还原标准化张量）与 `get_dataloaders_mnist` / `get_dataloaders_cifar10` 的 `train_transforms` / `test_transforms` 双通道接口 |
| `pytorch_ipynb/cnn/cnn-mobilenet-v2-cifar10.ipynb` | 现代风格 notebook 的「标准用法」：两套 transform + `get_dataloaders_cifar10` + `UnNormalize` 可视化，把本讲三个模块串成一条完整链路 |
| `pytorch_ipynb/helper_plotting.py` | `show_examples` 函数：内部逐样本调用 unnormalizer 还原后再画图，是 `UnNormalize` 最典型的调用方 |

> **关于 notebook 行号的说明**：`.ipynb` 文件本质是 JSON，notebook 源码以字符串数组形式存放在文件里。本讲对 notebook 的永久链接与 `#L` 行号均指向**原始 JSON 文件的行号**（在 GitHub 打开 notebook 后选择查看文件原文即可对应）。`.py` 文件的行号即普通源码行号。

## 4. 核心概念与源码讲解

### 4.1 transforms 组合：Compose 与常用图像变换

#### 4.1.1 概念说明

一个 transform 就是一个「可调用对象」（实现了 `__call__` 的类实例），接收样本、返回处理后的样本。`transforms.Compose([...])` 把多个 transform 按列表顺序串成流水线：前一个的输出是后一个的输入。它解决的问题是：数据预处理从来不是一步，而是「几何变形 → 转张量 → 数值缩放」一串动作，Compose 让这串动作可以作为一个整体挂在 Dataset 上。

关键在于**顺序**。在本仓库（torch 1.0~1.8 时代）的写法里存在两个世界：

- **PIL 世界**：`RandomHorizontalFlip`、`RandomRotation`、`ColorJitter`、`Resize`、`RandomCrop` 等几何/颜色变换作用在 PIL 图片上，必须放在 `ToTensor()` **之前**；
- **张量世界**：`Normalize` 作用在张量上，必须放在 `ToTensor()` **之后**。

所以流水线的固定骨架是：

```
PIL 图片 → [几何/颜色变换（PIL→PIL）] → ToTensor（PIL→张量, [0,1]）→ [Normalize（张量→张量）]
```

#### 4.1.2 核心流程

`torchvision-transform-examples.ipynb` 用同一份 CIFAR-10 数据、同一个循环（固定 `RANDOM_SEED`，取一个 batch，画前 5 张图）演示了 6 种组合，是「控制变量」式的图鉴：

| 小节 | Compose 内容 | 类型 |
| --- | --- | --- |
| 1) No Augmentation | `ToTensor()` | 基线，无增强 |
| 2) Horizontal Flip | `RandomHorizontalFlip(p=0.5)` + `ToTensor` | 几何随机增强 |
| 3) Random Rotation | `RandomRotation(degrees=20)` + `ToTensor` | 几何随机增强 |
| 4) Normalize to [-1, 1] | `ToTensor` + `Normalize(0.5, 0.5)` | 数值缩放（4.2 详解） |
| 5) Random Color Jitter | `ColorJitter(...)` + `ToTensor`（亮度/对比度/饱和度/色调各一格） | 颜色随机增强 |
| 6) Resize and Random Crop | `Resize(38,38)` + `RandomCrop(32,32)` + `ToTensor` | 几何组合增强 |

每个小节的执行流程都一样：

1. 定义 `custom_transform = transforms.Compose([...])`；
2. 用它构造 `datasets.CIFAR10(..., transform=custom_transform)`；
3. 用 DataLoader 取一个 batch（batch 内逐样本应用了 transform）；
4. `np.transpose(img, (1, 2, 0))` 把 C×H×W 张量换回 H×W×C 再 `imshow`（matplotlib 只认 HWC 且数值须在合理范围）。

#### 4.1.3 源码精读

**基线：只有 ToTensor**。[pytorch_ipynb/mechanics/torchvision-transform-examples.ipynb:L103-L129](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/torchvision-transform-examples.ipynb#L103-L129) 中，先用只有 `transforms.ToTensor()` 的 Compose 建数据集与 DataLoader，固定 `torch.manual_seed(RANDOM_SEED)` 后取一个 batch，把前 5 张图用 `np.transpose(images1[i], (1, 2, 0))` 换轴后画出来——这就是后面所有小节的「对照组」，画出来的是未经任何增强的原始 CIFAR-10 图片。

**随机水平翻转**。[torchvision-transform-examples.ipynb:L158-L182](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/torchvision-transform-examples.ipynb#L158-L182) 在 `ToTensor` 之前加了 `transforms.RandomHorizontalFlip(p=0.5)`：每次取样时每张图有 50% 概率被左右镜像。注意它作用在 PIL 图片上，所以必须排在 `ToTensor()` 前面。与基线画出的同一批图对比，能看到部分图片方向翻转——同一个样本在不同 epoch 会呈现不同形态，这正是「数据增强」的本质。

**随机旋转**。[torchvision-transform-examples.ipynb:L211-L236](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/torchvision-transform-examples.ipynb#L211-L236) 用 `transforms.RandomRotation(degrees=20, resample=PIL.Image.BILINEAR)` 在 ±20° 内均匀随机旋转，并用双线性插值填充旋转产生的空隙。（兼容性提示：较新版本的 torchvision 把 `resample` 参数改名为 `interpolation`，`PIL.Image.BILINEAR` 在新版 Pillow 中是 `Image.Resampling.BILINEAR` 的别名，具体行为待本地验证。）

**ColorJitter 家族**。[torchvision-transform-examples.ipynb:L313-L339](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/torchvision-transform-examples.ipynb#L313-L339)、[L364-L389](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/torchvision-transform-examples.ipynb#L364-L389)、[L414-L439](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/torchvision-transform-examples.ipynb#L414-L439)、[L464-L489](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/torchvision-transform-examples.ipynb#L464-L489) 四个 cell 结构完全相同，分别只动 `ColorJitter` 的一个旋钮：`brightness=(0.75, 1.25)`（亮度乘 0.75~1.25 的随机系数）、`contrast`（对比度）、`saturation`（饱和度）、`hue=(-0.1, 0.1)`（色调平移）。四个 cell 彼此就是控制变量实验，画出的图组分别呈现变亮/变淡/变灰/偏色的效果。

**Resize + RandomCrop 组合**。[torchvision-transform-examples.ipynb:L520-L546](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/torchvision-transform-examples.ipynb#L520-L546) 先 `Resize((38, 38))` 把 32×32 放大到 38×38，再 `RandomCrop((32, 32))` 随机裁回 32×32——这是经典的位置抖动增强（随机平移的等价写法），每次取样裁剪窗口都不同，图片内容会发生小幅平移。

#### 4.1.4 代码实践

1. **实践目标**：直观感受随机变换的「随机性」，确认 Compose 顺序约束。
2. **操作步骤**：
   - 进入 `pytorch_ipynb/mechanics/` 目录启动 Jupyter（notebook 用相对路径 `data` 读写数据集，须在原目录运行，u1-l2 讲过），打开 `torchvision-transform-examples.ipynb` 顺序执行；
   - 把第 2 节的 `p=0.5` 分别改成 `p=0.0` 和 `p=1.0`，重跑该 cell；
   - 把第 6 节的 `RandomCrop((32, 32))` 改成 `RandomCrop((24, 24))`，重跑。
3. **需要观察的现象**：`p=0.0` 时画出的 5 张图与第 1 节基线完全一致（从不翻转）；`p=1.0` 时全部左右镜像；裁剪 24×24 后图片明显「放大拉近」且边缘被裁掉。
4. **预期结果**：随机变换在固定种子下可复现——notebook 开头设定 `RANDOM_SEED = 123` 并在取样前 `torch.manual_seed(RANDOM_SEED)`，同一种子重复运行同一 cell 画出的图相同；换种子则得到不同的翻转/裁剪结果。
5. 若本地暂无环境，可做**源码阅读型实践**：notebook 已保存作者运行后的输出图片，直接阅读 GitHub 上渲染好的 notebook，对照 6 个小节的图组差异；具体图像随种子而变，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `transforms.Normalize(...)` 放在 `transforms.ToTensor()` 之前会发生什么？

**答案**：会出错。`Normalize` 的输入是张量，它对张量做逐通道减均值除标准差；放在 `ToTensor` 之前时样本还是 PIL 图片，无法进行张量运算，运行时报类型错误。正确顺序是 `ToTensor()` 在前、`Normalize` 在后。

**练习 2**：为什么 notebook 中所有 `ColorJitter`、`RandomRotation` 都写在 `ToTensor()` 前面？

**答案**：这些变换在 torchvision（本仓库使用的版本）中设计为作用在 PIL 图片上；`ToTensor()` 是「PIL 世界 → 张量世界」的分界线，之后样本已是 C×H×W 浮点张量，不再是 PIL 对象。

**练习 3**：`np.transpose(images1[i], (1, 2, 0))` 这行在做什么，去掉它直接 `imshow(images1[i])` 会怎样？

**答案**：把张量从 C×H×W（PyTorch 布局）换轴成 H×W×C（matplotlib 期望的布局）。去掉后 matplotlib 会把通道维当成一维像素行来解释，要么报形状错误要么画出完全错乱的图。这个「换轴 + 数值范围」问题正是 4.3 节 `UnNormalize` 要系统性解决的。

### 4.2 逐通道标准化：z-score 与 cnn-standardized.ipynb

#### 4.2.1 概念说明

「标准化」（standardization / z-score）指把每个通道的像素值变成均值 0、方差 1：

\[ x' = \frac{x - \mu_c}{\sigma_c} \]

其中 \( \mu_c \)、\( \sigma_c \) 是**训练集**中第 \( c \) 个通道上所有像素的均值与标准差。灰度图（MNIST）只有 1 个通道，得到 1 个均值 + 1 个标准差；RGB 图有 3 个通道，得到 3 + 3。`transforms.Normalize(mean=..., std=...)` 就是这个公式的逐通道实现。

实践中的难点不是公式，而是**统计量从哪来**：整个训练集可能大到无法一次性读进内存，所以需要「增量式」计算——按批累计每批的均值/标准差，再汇总。`cnn-standardized.ipynb` 演示的正是这一步，随后用算出的统计量构造 `Normalize`，对训练集和测试集使用**同一套**统计量（测试集不自己算，避免数据泄漏）。

还有两个背景事实帮助理解：

- 统计是在 `ToTensor()` 之后的 [0,1] 尺度上算的，所以 MNIST 的均值 0.1307、标准差 0.3077 都小于 1；
- 除了数据驱动的 z-score，还有更粗糙的「常数版」：`Normalize((0.5,...), (0.5,...))` 把 [0,1] 线性映到 [-1,1]，即 \( x' = \frac{x-0.5}{0.5} = 2x-1 \)。它不需要先算统计量，是.mobilenet 系列 notebook 的选择（4.3.3 会看到）。

#### 4.2.2 核心流程

`cnn-standardized.ipynb` 的标准化流程分三步：

1. **预扫描**：用只挂 `ToTensor()` 的「临时 DataLoader」（不打乱、批 128）遍历训练集，对每个 batch 计算逐通道统计：

   `np.mean(numpy_image, axis=(0, 2, 3))`——对一个形状为 \( N \times C \times H \times W \) 的 batch 张量，沿「批、高、宽」三个维度求平均，剩下长度为 \( C \) 的向量，即该 batch 每个通道的均值（标准差同理）。

2. **汇总**：把所有 batch 的均值向量、标准差向量分别平均（`np.mean(train_mean, axis=0)`），得到全训练集的 `train_mean`、`train_std`。

3. **组装并应用**：`Compose([ToTensor(), Normalize(mean=train_mean, std=train_std)])`，训练集与测试集共用这条 transform。

一个值得留意的**近似细节**：对不等大的 batch 简单平均会引入轻微偏差（MNIST 60000 除以 128 不整除，最后一个 batch 只有 96 个样本，却被当作与 128 等权）；且「批标准差的平均」在数学上不等于「全局标准差」（当各批均值不同时会低估）。在本例的尺度下这些偏差很小，notebook 的验证结果（batch 均值 0.0035、标准差 1.0037）也说明近似足够好。想要精确值，可累计每通道的像素和与像素平方和（或直接两遍扫描）再计算——综合实践里我们会这么做。

#### 4.2.3 源码精读

**第一步：增量计算训练集统计量**。[pytorch_ipynb/cnn/cnn-standardized.ipynb:L150-L179](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-standardized.ipynb#L150-L179)：先建只挂 `ToTensor()` 的 MNIST 训练集和**不打乱**（`shuffle=False`）的 DataLoader；然后 `train_mean = []`、`train_std = []` 两个列表收集每个 batch 的 `np.mean(numpy_image, axis=(0, 2, 3))` 与 `np.std(...)`；最后 `np.mean(train_mean, axis=0)` 汇总成张量并打印。notebook 保存的输出为 `Mean: tensor([0.1307])`、`Std Dev: tensor([0.3077])`——MNIST 是灰度图，所以各只有 1 个数；数值小于 1 正因为统计发生在 `ToTensor()` 之后的 [0,1] 尺度上。

**第二步：把统计量装进 Compose**。[cnn-standardized.ipynb:L212-L213](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-standardized.ipynb#L212-L213)：`custom_transform = transforms.Compose([transforms.ToTensor(), transforms.Normalize(mean=train_mean, std=train_std)])`——这就是 4.1 流水线骨架的完整形态：先转张量、再用训练集统计量逐通道标准化。

**第三步：训练集与测试集共用同一 transform**。[cnn-standardized.ipynb:L228-L244](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-standardized.ipynb#L228-L244)：`train=True` 与 `train=False` 两个 MNIST 数据集都挂 `custom_transform`（统计量来自训练集，测试集只是被同一线性变换缩放——这是防数据泄漏的正确姿势），再分别包上 shuffle 的训练 loader 与不 shuffle 的测试 loader。

**验证：batch 通道均值≈0、标准差≈1**。[cnn-standardized.ipynb:L298-L299](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-standardized.ipynb#L298-L299)：对一个 batch 打印 `torch.mean(images[:, 0, :, :])` 与 `torch.std(...)`，保存的输出为 `0.0035` 与 `1.0037`——接近 0 和 1，证明标准化生效且前述「批平均近似」偏差确实很小（单批数值不恰好为 0/1 也属正常波动）。

**后续训练**：模型（[cnn-standardized.ipynb:L320-L350](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-standardized.ipynb#L320-L350)，结构与 u3-l1 的 `cnn-basic` 同款两层卷积网络）与五步训练循环都没变，唯一变量是输入被标准化了，10 个 epoch 后测试准确率 98.07%——这就是「控制变量」的实验组织方式：把标准化与未标准化版本（u3-l1）对比即可单独评估标准化的贡献。

#### 4.2.4 代码实践

1. **实践目标**：亲手复现统计量计算，并验证「只用训练集统计量」这一原则的实际含义。
2. **操作步骤**（在 `pytorch_ipynb/cnn/` 下运行 `cnn-standardized.ipynb` 到 L298-L299 的验证 cell）：
   - 执行 L150-L179 的统计量计算 cell，记录打印的 mean/std；
   - 复制该 cell，把 `datasets.MNIST(..., train=True, ...)` 改成 `train=False`，用测试集算一套 `test_mean`/`test_std` 并打印；
   - 执行 L228-L244 与 L298-L299，确认 batch 的通道均值/标准差接近 0/1。
3. **需要观察的现象**：测试集算出的统计量与训练集的 0.1307/0.3077 **接近但不相等**（MNIST 的 train/test 分布几乎同源，差异在小数点后几位）。
4. **预期结果**：用训练集统计量标准化的 batch，通道均值≈0、标准差≈1；若误用测试集统计量，两个数字会轻微偏离 0/1。具体差值待本地验证。
5. 若无环境：阅读型实践——notebook 已保存全部输出（Mean/Std、batch 检查值、训练日志），对照本节解读核对每个数字的来源。

#### 4.2.5 小练习与答案

**练习 1**：MNIST 的 `train_mean = 0.1307` 这个数在「图片」上是什么意思？为什么它比 0.5 小很多？

**答案**：它是训练集所有像素在 [0,1] 尺度上的平均亮度——即平均每个像素只有约 13% 的「墨水」。因为 MNIST 数字是黑底白字（大部分像素是 0），只有笔画处接近 1，所以平均亮度远低于 0.5。相应地标准差 0.3077 反映了「大面积的 0 与笔画处的 1」两极分化。

**练习 2**：对 RGB 图使用 `Normalize(mean, std)` 时，mean 和 std 各需要几个数？它们分别对应什么？

**答案**：各 3 个，分别对应 R、G、B 三个通道各自的训练集均值与标准差；`Normalize` 会对每个通道独立执行 \( (x_c - \mu_c)/\sigma_c \)（灰度图则各 1 个）。

**练习 3**：notebook 用「对每个 batch 的均值/标准差取平均」来汇总，为什么不直接把整个训练集拼成一个大数组一次性算？

**答案**：显存/内存约束——作者的假设是「整个数据集不能一次装进内存」（notebook 原文如此），所以用增量方式。代价是引入轻微近似（末尾小 batch 等权、批标准差的平均在数学上≠全局标准差）。当内存允许时，一次性计算或累计「像素和 + 像素平方和」可得精确值；本例验证显示近似误差约在千分位，实际影响很小。

### 4.3 UnNormalize 还原与 train/test 两套 transform

#### 4.3.1 概念说明

**为什么需要「还原」**：标准化后的张量不再是 [0,1] 的图片——以 MNIST 为例，取值范围约为 \( [(0 - 0.1307)/0.3077,\ (1 - 0.1307)/0.3077] \approx [-0.42, 2.82] \)。直接 `imshow` 会把超出 [0,1] 的部分裁掉、整体对比度错乱，画出来的图「看起来坏了」。要在训练/评估时查看模型实际看到的样本（比如误分类样本，u7-l1 的 `show_examples`），必须先做标准化的**逆变换**：

\[ x = x' \cdot \sigma_c + \mu_c \]

`helper_data.py` 的 `UnNormalize` 类就是它的实现：构造时传入与 `Normalize` 相同的 mean/std，调用时对 (C, H, W) 张量逐通道原地执行 `t.mul_(s).add_(m)`。它与 `Normalize` 互为逆运算，配对使用即可「标准化 → 喂模型 → 还原 → 画图」。

**为什么需要两套 transform**：训练集希望挂**随机**增强（每次取样形态不同，扩充有效数据）；而验证/测试集必须**确定性**（同一样本每次得到同一张量，否则评估结果不可复现）。这就是 u4-l1 提过的接口设计：`get_dataloaders_mnist/cifar10` 提供 `train_transforms` 与 `test_transforms` 两个参数，训练子集挂前者，验证与测试子集挂后者——验证/测试的 transform 可以有缩放（确定性部分），但不能有随机性。注意「随机增强」与「标准化」并不冲突：一条 Compose 里可以同时含 RandomCrop（随机）和 Normalize（确定），train 用「RandomCrop + ToTensor + Normalize」，test 用「CenterCrop + ToTensor + Normalize」。

#### 4.3.2 核心流程

`UnNormalize` 的使用流程（以 mobilenet-v2 notebook 为例）：

```
构造: unnormalizer = UnNormalize(mean, std)   # 与 Normalize 同参
还原: 还原后的张量 = unnormalizer(标准化张量)  # 逐通道 x'*std + mean，原地修改
画图: 换轴 C×H×W → H×W×C 后 imshow
```

两套 transform 的组装流程：

```
train_transforms = Compose([随机几何/颜色变换, ToTensor, Normalize(stats)])
test_transforms  = Compose([确定性变换(如 CenterCrop), ToTensor, Normalize(stats)])  # 同一套 stats
get_dataloaders_cifar10(..., train_transforms=..., test_transforms=...)
→ train_loader 挂 train_transforms；valid_loader 与 test_loader 挂 test_transforms
```

#### 4.3.3 源码精读

**UnNormalize 类本体**。[pytorch_ipynb/helper_data.py:L9-L26](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_data.py#L9-L26)：`__init__` 只保存 mean 和 std；`__call__` 用 `for t, m, s in zip(tensor, self.mean, self.std)` 遍历张量的第 0 维（通道维），对每个通道平面 `t.mul_(s).add_(m)`——即先乘标准差再加均值，正是 \( x = x' \cdot \sigma_c + \mu_c \)，且是**原地修改**（`mul_`/`add_` 带下划线）后返回同一张量。这样 `UnNormalize` 可以像内置 transform 一样被调用，只是作用对象是标准化后的张量。

**双 transform 接口**。[helper_data.py:L29-L51](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_data.py#L29-L51)（`get_dataloaders_mnist` 的签名与数据集构造，CIFAR-10 版本在 [L90-L112](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_data.py#L90-L112) 同构）：两个参数 `train_transforms` / `test_transforms` 默认都是 `ToTensor()`；构造数据集时 `train_dataset` 挂 `train_transforms`，而 `valid_dataset` 与 `test_dataset` 都挂 `test_transforms`——4.3.1 的「训练随机、评估确定」原则就落实在这三行挂载上（u4-l1 已从 Sampler 角度读过此函数，这里只看 transform 的分配）。

**调用方：show_examples 内部如何用 unnormalizer**。[pytorch_ipynb/helper_plotting.py:L74-L92](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_plotting.py#L74-L92)：取一个 batch 前向推理后，若传入了 `unnormalizer`，就对 batch 里**每个样本**执行 `features[idx] = unnormalizer(features[idx])`，再 `np.transpose(features, axes=(0, 2, 3, 1))` 换轴成 NHWC 去画图。也就是说，`show_examples` 把「还原 + 换轴」这套可视化苦活封装好了，调用者只需提供与训练时 Normalize 配对的 unnormalizer。

**完整用法范例**。[pytorch_ipynb/cnn/cnn-mobilenet-v2-cifar10.ipynb:L140-L157](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-mobilenet-v2-cifar10.ipynb#L140-L157)：`train_transforms` 是 `Resize(70,70) → RandomCrop(64,64) → ToTensor → Normalize((0.5,0.5,0.5),(0.5,0.5,0.5))`（有随机裁剪）；`test_transforms` 把 `RandomCrop` 换成**确定性**的 `CenterCrop(64,64)`，其余完全一致；然后一起传给 `get_dataloaders_cifar10(batch_size=..., validation_fraction=0.1, train_transforms=..., test_transforms=...)`。这里用的是 4.2 提过的「常数版」标准化：mean=std=0.5 把 [0,1] 映到 [-1,1]。[cnn-mobilenet-v2-cifar10.ipynb:L1248-L1260](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-mobilenet-v2-cifar10.ipynb#L1248-L1260)：评估阶段构造 `unnormalizer = UnNormalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))`（参数与 Normalize 严格配对），连同 `class_dict` 传给 `show_examples`，画出「预测 vs 真实」标注的样例图。

> 顺带一提：该 notebook 在训练刚开始处还用了另一招快速可视化——`torchvision.utils.make_grid(images[:64], padding=2, normalize=True)`，`make_grid` 的 `normalize=True` 会自动把张量范围拉伸到 [0,1]，适合快速看一眼；而 `UnNormalize` 是精确逆变换，能还原出与原图完全一致的像素，两者各有用途。

#### 4.3.4 代码实践

1. **实践目标**：验证 `Normalize` 与 `UnNormalize` 互为逆运算，体会参数必须配对。
2. **操作步骤**（以下为**示例代码**，可在任意能 `import torch, torchvision` 的环境运行；若要复用仓库的 `UnNormalize`，需仿照 notebook 先 `sys.path.insert(0, "..")` 再 `from helper_data import UnNormalize`）：

   ```python
   from torchvision import transforms
   from helper_data import UnNormalize   # 需让 pytorch_ipynb 在 sys.path 上

   t = transforms.Compose([
       transforms.ToTensor(),
       transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))])
   # 造一张 3x8x8 的假图片，取值 [0,1]
   x = torch.rand(3, 8, 8)
   xn = t(x)                              # 标准化后

   unnorm = UnNormalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))
   xr = unnorm(xn.clone())                # 还原（UnNormalize 是原地操作，先 clone 保留 xn）
   print((xr - x).abs().max())            # 往返误差
   ```

3. **需要观察的现象**：打印出的最大往返误差应为 0（或 1e-7 量级的浮点误差）；`xn` 的取值范围在 [-1,1]。
4. **预期结果**：配对正确时误差≈0。再故意把 unnormalizer 的 std 改成 `(0.25, 0.25, 0.25)` 试一次，误差会明显变大、还原出的图整体过曝/欠曝——体会「参数必须与 Normalize 一致」。
5. 本例未在当前环境运行，具体数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`UnNormalize` 的 `__call__` 里为什么是 `t.mul_(s).add_(m)`（先乘 std 再加 mean），而不是先加后乘？

**答案**：因为它要撤销的是 \( x' = (x - \mu)/\sigma \)。逆运算是代数上逐步反解：\( x = x' \cdot \sigma + \mu \)——先乘回标准差、再加回均值。如果写成「先加 mean 再乘 std」（即 \( (x' + \mu) \cdot \sigma \)）在数学上不是原变换的逆。

**练习 2**：为什么 `get_dataloaders_mnist` 里 `valid_dataset` 挂的是 `test_transforms` 而不是 `train_transforms`？

**答案**：验证集用于每个 epoch 后评估并指导选模型/早停，其结果必须可复现、且与测试集口径一致，所以要挂确定性变换；若挂上随机增强，同一个样本每轮验证的准确率都会抖动，模型选择就建立在噪声上。训练子集则相反，需要随机增强来扩充数据。

**练习 3**：标准化后 MNIST 张量的取值范围约为 [-0.42, 2.82]。如果不用 `UnNormalize`，直接把它 `imshow`，画面会怎样？

**答案**：`imshow` 会把 [0,1] 之外的值截断（负值当 0、超过 1 当 1），于是背景（约 -0.42→0）和笔画（约 2.82→1）都被压扁，整体变成一张高对比度、细节丢失的图，无法判断模型「看到」的内容是否正常。这就是所有可视化前都要先还原的原因。

## 5. 综合实践

**任务：为 CIFAR-10 搭一条「计算统计量 → 双 transform → 加载 → 还原可视化」的完整管线。** 这是本讲三个模块的合体，也是规格指定的实践任务。

**步骤 1：增量计算 CIFAR-10 训练集的逐通道均值/标准差**（仿照 `cnn-standardized.ipynb` L150-L179 的写法，以下为**示例代码**）：

```python
import numpy as np
import torch
from torchvision import datasets, transforms
from torch.utils.data import DataLoader

train_dataset = datasets.CIFAR10(root='data', train=True,
                                 transform=transforms.ToTensor(), download=True)
loader = DataLoader(dataset=train_dataset, batch_size=128, shuffle=False)

sums, sqs, n = 0., 0., 0                     # 精确版：累计像素和与像素平方和
for images, _ in loader:
    sums += images.sum(dim=(0, 2, 3))        # 每通道像素值之和
    sqs  += (images ** 2).sum(dim=(0, 2, 3)) # 每通道像素值平方和
    n    += images.shape[0] * images.shape[2] * images.shape[3]  # 每通道像素总数

mean = sums / n
std  = (sqs / n - mean ** 2).sqrt()          # 方差 = E[x^2] - E[x]^2
print('mean:', mean, '\nstd:', std)
```

**步骤 2：组装 train/test 两套 transform 并加载**（仿照 `cnn-mobilenet-v2-cifar10.ipynb` L140-L157；此处保持 32×32 原始分辨率，随机增强只留翻转）：

```python
train_transforms = transforms.Compose([
    transforms.RandomHorizontalFlip(p=0.5),          # 随机：仅训练集
    transforms.ToTensor(),
    transforms.Normalize(mean, std)])                # 用步骤 1 的张量
test_transforms = transforms.Compose([
    transforms.ToTensor(),                           # 确定性：验证/测试共用
    transforms.Normalize(mean, std)])

# from helper_data import get_dataloaders_cifar10   # 需 pytorch_ipynb 在 sys.path
train_loader, valid_loader, test_loader = get_dataloaders_cifar10(
    batch_size=128, validation_fraction=0.1,
    train_transforms=train_transforms, test_transforms=test_transforms)
```

**步骤 3：验证标准化生效**：取一个 batch，打印 `images.mean(dim=(0,2,3))` 与 `images.std(dim=(0,2,3))`，应分别接近 0 向量与 1 向量（训练集带随机翻转，统计量仍应近似成立）。

**步骤 4：UnNormalize 对照可视化**：取 8 个样本，把「标准化前（只挂 ToTensor 的 loader 取）｜标准化后｜还原后」三行画成对照图：

```python
# from helper_data import UnNormalize
unnorm = UnNormalize(mean.tolist(), std.tolist())
restored = torch.stack([unnorm(img.clone()) for img in images[:8]])
# 用 np.transpose(img, (1, 2, 0)) 换轴后 imshow；三行共用同一批样本索引
```

**需要观察与记录**：

1. 你算出的 mean/std 数值（文献常用参考值约为 mean (0.4914, 0.4822, 0.4465)、std (0.2470, 0.2435, 0.2616)，以你本地计算为准）；
2. 步骤 3 的两个向量；
3. 对照图中「标准化后」一行发灰/异常、「还原后」一行应与「标准化前」肉眼一致；
4. 故意用错 std（如全部乘 2）再看还原行，记录画面差异。

**预期结果**：batch 逐通道均值≈0、标准差≈1；`UnNormalize` 还原行与原始行视觉一致（浮点误差内逐像素相等）。本综合实践未在当前环境运行，全部数值待本地验证；若在较新版本 torch 上遇到 `Normalize` 的 dtype 报错，可给 mean/std 加 `.float()`。

## 6. 本讲小结

- transform 是挂在 Dataset 上的**逐样本**处理函数，`Compose` 按序串联；本仓库的固定骨架是「PIL 变换 → `ToTensor()`（PIL→张量、[0,1]）→ `Normalize`（张量）」，顺序不可颠倒。
- 逐通道标准化即 \( x' = (x - \mu_c)/\sigma_c \)，灰度图 1 对、RGB 图 3 对统计量；统计量**只能来自训练集**并对测试集复用；数据集放不进内存时用「逐批累计再汇总」的增量算法（注意批平均是近似）。
- 标准化的逆变换是 \( x = x' \cdot \sigma_c + \mu_c \)，由 `helper_data.UnNormalize` 原地实现；可视化标准化数据前必须还原（或用 `make_grid(normalize=True)` 快速拉伸）。
- 双 transform 模式：`train_transforms` 挂随机增强（RandomCrop/RandomHorizontalFlip/ColorJitter），`test_transforms` 只挂确定性变换，验证/测试共用后者——这是 `get_dataloaders_*` 接口的既定设计。
- 实验组织上，`cnn-standardized.ipynb` 用与 `cnn-basic` 完全相同的模型与循环、只改输入缩放，是「控制变量」对比的标准样板。

## 7. 下一步学习建议

- **u4-l4（公共训练评估工具链与确定性）**：把本讲的双 transform 接到 `train_classifier_simple_v2` 的完整实验流程里，并讨论随机性与可复现。
- **u7-l1（评估工具链）**：`show_examples`、混淆矩阵等可视化工具会大量使用本讲的 `UnNormalize`。
- **u8-l3（数据增强实战）**：本讲的随机变换是「手动挑选」的，下一阶段学习 AutoAugment/TrivialAugment 这类自动搜索出的增强策略，并做增强收益的对照实验。
- 延伸阅读源码：`pytorch_ipynb/cnn/cnn-vgg16-celeba.ipynb`（大图数据上的 Resize/CenterCrop 流水线）与 `pytorch-lightning_ipynb/cnn/cnn-mobilenet-v2-cifar10.ipynb`（同一套思路在 Lightning DataModule 中的组织方式）。
