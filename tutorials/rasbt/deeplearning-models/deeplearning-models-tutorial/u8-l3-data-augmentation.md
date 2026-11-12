# 数据增强实战：AutoAugment 与 TrivialAugment

> 本讲引用 `.ipynb` 文件的行号时，指的是 notebook JSON 文件中的行号（GitHub 上点击链接即可跳转到对应位置）；引用 `.py` 文件时即为普通源码行号。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚「数据增强为什么只作用于训练集」，并对照 `Cifar10DataModule` 的双 transform 接口理解它的工程实现。
2. 会用一行 `transforms.AutoAugment(...)` 与 `transforms.TrivialAugmentWide(...)` 把新式增强策略接入训练管线，并知道它们该插在 Compose 流水线的哪个位置。
3. 会组织「受控对照实验」来度量增强收益：固定模型、优化器、随机种子，只改 transform，用验证准确率曲线与测试准确率下结论。
4. 养成批判性读码习惯：本讲的三份 notebook 里就藏着几处复制粘贴遗留与口径偏差，我们逐一指出。

## 2. 前置知识

**什么是数据增强（data augmentation）。** 训练集里的每张图片，经过一次随机几何或颜色扰动（旋转、平移、剪切、变色调……）后，标签不变——一只猫旋转 15 度还是猫。增强相当于告诉模型「这些变化不改变类别」，把不变性先验注入训练。从数学上看，普通训练最小化经验风险

\[
\frac{1}{N}\sum_{i=1}^{N} \ell\!\left(f(x_i), y_i\right),
\]

而增强训练把每个样本扩展成一个随机变换 \(t\) 下的期望：

\[
\frac{1}{N}\sum_{i=1}^{N} \mathbb{E}_{t\sim\mathcal{T}}\left[\ell\!\left(f(t(x_i)), y_i\right)\right],
\]

其中 \(\mathcal{T}\) 是增强策略定义的变换分布。有效样本分布被「撑大」了，模型更难死记训练集，从而缓解过拟合。

**增强为什么只作用于训练集。** 训练集的目标是「让模型学得会」，注入随机性能提高泛化；验证/测试集的目标是「稳定地度量」，如果评估时每次取样都施加随机旋转，同一模型两次评估的成绩都会抖动，指标就失去了可比性。所以 u4-l3 讲过的双 transform 模式（训练集挂随机增强、验证/测试集只挂确定性变换）在这里再次出现。

**新旧两代增强。** 传统做法是手工挑几条规则，比如 `RandomCrop` + `RandomHorizontalFlip`（u3-l2 的 AlexNet notebook 里已经见过类似的随机裁剪思想）。本讲的两件主角是「新式」策略：

- **AutoAugment**（Cubuk et al., 2019）：用强化学习在目标数据集上**搜索**出 25 个「子策略」，每个子策略含两个图像操作、每个操作带应用概率与幅度；训练时每张图随机抽一个子策略执行。
- **TrivialAugmentWide**（Müller & Hutter, 2021）：反其道而行——**免调参**。每张图随机抽一个算子、随机抽一个强度，直接应用，不搜索任何策略，却在很多任务上追平甚至超过 AutoAugment。

**承接前讲。** 本讲处在 u8 单元：u8-l1 讲过 `LightningModule` / `LightningDataModule` / `Trainer` 三件套，本讲的训练代码全部交给这套机制；u4-l3 讲过 `transforms.Compose` 的固定顺序「几何/颜色变换（PIL 域）→ ToTensor → Normalize」，本讲的增强操作正是插在最前面的 PIL 域。另外请带着 u3-l5 的教训来读：**比较模型必须对齐训练配方**——本讲的对照实验正是这条方法论的正面示范（但也有几处不完美，见 4.3）。

## 3. 本讲源码地图

本讲的四份文件位于 `pytorch-lightning_ipynb/data-augmentation/autoaugment/` 目录，构成一个天然的「三胞胎对照实验 + 一份共享工具」：

| 文件 | 作用 |
| --- | --- |
| [helper_utilities.py](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/data-augmentation/autoaugment/helper_utilities.py) | 三份 notebook 共享的 `LightningModel`、`Cifar10DataModule`（双 transform 接口）与绘图函数 |
| [without-augmentation.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/data-augmentation/autoaugment/without-augmentation.ipynb) | 无增强基线：ResNet-18 从零训练 CIFAR-10，40 epoch |
| [with-autoaugment.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/data-augmentation/autoaugment/with-autoaugment.ipynb) | 同一模型同一配方，仅给训练集加 AutoAugment（CIFAR10 策略），500 epoch |
| [with-trivialaugment.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/data-augmentation/autoaugment/with-trivialaugment.ipynb) | 同上，仅换成 TrivialAugmentWide，1000 epoch |

三份 notebook 开头都注明了互相参照关系（基线 notebook 的第一个 markdown 单元明确写着它是另外两份的 baseline），且都运行在 torch 1.13.1 + lightning 1.9.0 + torchvision 0.14.1 的环境上（`%watermark` 输出）。

## 4. 核心概念与源码讲解

### 4.1 无增强基线与双 transform 数据模块

#### 4.1.1 概念说明

要度量「增强带来多少收益」，先得有一个不含增强的基线。本模块精读两样东西：

1. `Cifar10DataModule`——数据管线如何同时容纳「训练用 transform」与「评估用 transform」两套配置；
2. without-augmentation notebook——基线训练怎么跑起来。

这个 DataModule 与 u8-l1 见过的 MNIST DataModule 骨架相同，但多了一个关键设计：**transform 不写死在类里，而是作为构造参数从外部注入**。这正是三份 notebook「只改一处」的支点——模型、优化器、循环全部不动，只换注入的 transform。

#### 4.1.2 核心流程

```text
构造 Cifar10DataModule(train_transform=..., test_transform=...)
        │
        ├─ prepare_data()   下载 CIFAR-10；若调用者没传 transform，
        │                   则兜底为 Resize + ToTensor（无增强、也无 Normalize）
        │
        ├─ setup()          train 数据集挂 train_transform（随机、每 epoch 不同）
        │                   test  数据集挂 test_transform（确定性）
        │                   再用 random_split 把 train 切成 45000 训练 + 5000 验证
        │
        └─ train/val/test_dataloader()   三个 DataLoader：训练集 shuffle+drop_last，
                                         验证/测试集不 shuffle
```

训练侧则完全复用 u8-l1 的范式：`LightningModel` 包住 `torchvision.models.resnet18`，`Trainer.fit` 一行启动。

#### 4.1.3 源码精读

先看双 transform 接口的定义。`Cifar10DataModule.__init__` 接收外部注入的两套 transform 并原样保存：

- [helper_utilities.py:L63-L74](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/data-augmentation/autoaugment/helper_utilities.py#L63-L74) —— 构造参数 `train_transform=None, test_transform=None`：训练与评估两套 transform 从外部注入，默认 `None` 交给后续兜底逻辑。

兜底逻辑在 `prepare_data` 里：下载 CIFAR-10 后，若没传 transform，则给训练/评估各配一份 `Resize + ToTensor`：

- [helper_utilities.py:L76-L94](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/data-augmentation/autoaugment/helper_utilities.py#L76-L94) —— 下载 CIFAR-10；`train_transform is None` 时兜底为 `transforms.Compose([Resize(height_width), ToTensor()])`。注意这套兜底 transform **没有任何增强，也没有 Normalize**——基线 notebook 走的正是这条路径。

`setup` 把两套 transform 分别挂到数据集上：

- [helper_utilities.py:L96-L111](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/data-augmentation/autoaugment/helper_utilities.py#L96-L111) —— 训练集（`train=True`）挂 `self.train_transform`，测试集（`train=False`）挂 `self.test_transform`；随后 `random_split(train, lengths=[45000, 5000])` 切出验证集。

**⚠️ 批判性读码点 1：验证集也被增强了。** 仔细读上面这段：`random_split` 切出的 `self.valid` 是「挂着 `train_transform` 的那个数据集」的子集，与 `self.train` 共享同一个 dataset 对象。也就是说，传了 AutoAugment 时，**验证集的每张图在每个 epoch 也会被随机增强一次**。这不同于 u4-l1 精读过的 `helper_data.py` 方案——那边用索引区间 + sampler 精确切分，让验证子集挂确定性的 `test_transforms`。后果是：本讲的 `val_acc` 是「模型在增强样本上的准确率」，口径偏向训练分布；不过由于验证集样本量大（5000 张）、增强是随机的，它仍能用于横向比较三种配置。读源码时认准变量来源，才能解释指标的确切含义。

三个 DataLoader 的构造在：

- [helper_utilities.py:L113-L141](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/data-augmentation/autoaugment/helper_utilities.py#L113-L141) —— 训练 loader `shuffle=True, drop_last=True`，验证/测试 loader 不打乱。与 u4-l1 讲过的批数公式、drop_last 语义一致。

训练侧的 `LightningModel`（SGD + 交叉熵 + torchmetrics 指标）在：

- [helper_utilities.py:L12-L60](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/data-augmentation/autoaugment/helper_utilities.py#L12-L60) —— `_shared_step` 复用前向与损失（L28-L34），`training_step`/`validation_step`/`test_step` 分别记日志（L36-L56），`configure_optimizers` 返回裸 SGD（L58-L60）。注意这里**没有任何学习率调度器**，后面解释为什么这会影响增强版需要更多 epoch。

基线 notebook 的训练单元（三个 notebook 中共用的骨架，差异仅在于 transform 与 Trainer 参数）：

- [without-augmentation.ipynb:L688-L708](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/data-augmentation/autoaugment/without-augmentation.ipynb#L688-L708) —— `L.seed_everything(123)` 固定随机性；`Cifar10DataModule(height_width=(32, 32), batch_size=256, num_workers=4)` **不传 transform**，走上面 prepare_data 的兜底路径；`torchvision.models.resnet18(weights=None)` 从零初始化并替换分类头 `fc = nn.Linear(512, 10)`（L691-L692）；`LightningModel(model=pytorch_model, learning_rate=0.05)`（L694）；`L.Trainer(max_epochs=40, accelerator="gpu", precision=16, devices=[0], logger=CSVLogger(save_dir="logs/", name="baseline"), deterministic=False)`（L699-L706）。

测试与结果：

- [without-augmentation.ipynb:L826](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/data-augmentation/autoaugment/without-augmentation.ipynb#L826) —— `trainer.test(model=lightning_model, datamodule=dm)`。notebook 保存的输出为 `test_acc = 0.6851`，训练耗时 3.44 分钟（40 epoch）。

基线拿到了：**ResNet-18（约 1120 万参数，notebook 输出显示 11.2M）、从零训练、无增强、无标准化、固定学习率 0.05、40 epoch → 测试准确率 68.51%**。这就是后面两个实验要挑战的数字。

#### 4.1.4 代码实践

**实践目标**：确认「增强只作用于训练集」在本仓库的实现方式，并亲眼看到兜底 transform 的内容。

1. 操作步骤（示例代码）：

   ```python
   # 示例代码：检查 Cifar10DataModule 的 transform 挂载
   from helper_utilities import Cifar10DataModule

   dm = Cifar10DataModule(height_width=(32, 32), batch_size=64)
   dm.prepare_data()
   dm.setup()
   # 训练子集与验证子集共享同一个底层 dataset 对象
   print(dm.train.dataset is dm.valid.dataset)   # 预期 True
   print(dm.train.dataset.transform)             # 兜底的 Resize+ToTensor
   print(dm.test.transform)                      # 测试集的确定性 transform
   ```

2. 需要观察的现象：`dm.train.dataset is dm.valid.dataset` 打印 `True`，说明 4.1.3 的「批判性读码点 1」成立——训练与验证子集共享同一 dataset（从而共享同一 transform）。
3. 预期结果：打印出的 transform 都是 `Compose(Resize(size=(32, 32)), ToTensor())`，不含增强、不含 Normalize。
4. 若在你的环境上运行报错（如 lightning 版本 API 变化），待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：本讲的 `Cifar10DataModule` 里，评估用的 `test_transform` 兜底值与 `train_transform` 兜底值完全相同。既然相同，为什么还要设计成两个独立参数？

**答案**：因为「训练时随机、评估时确定」是增强场景的刚需——with-autoaugment/with-trivialaugment 两份 notebook 只注入 `train_transform`，让 `test_transform` 走兜底的确定性路径，从而保证评估口径稳定。双参数接口让「换增强」成为只改一处的外科手术式变更，这正是三胞胎对照实验能成立的前提。

**练习 2**：基线 notebook 用的是 `precision=16`（L702）而两份增强 notebook 没有设置 precision。这会让「训练耗时」的直接对比变得不可靠吗？

**答案**：会。`precision=16` 启用自动混合精度通常明显提速，且基线用 `devices=[0]`、增强版用 `devices=[1]`，硬件实例也不同。所以本讲只能稳妥地比较**准确率**与 epoch 数；耗时数字（3.44 / 51.74 / 98.82 分钟）只能粗略参考。这也是 u3-l5「对比须对齐配方」教训的又一次应用。

**练习 3**：为什么兜底 transform 里连 `Normalize` 都没有？这对基线成绩意味着什么？

**答案**：作者的设计是让三份 notebook 的差异**仅限于增强策略**，transform 其余部分保持最小（Resize + ToTensor）。代价是输入未标准化，SGD 固定 lr=0.05 的训练效率不是最优——基线 68.51% 的绝对水平并不高（对照 u3-l5 中对齐配方后的 86%+），但作为「同口径下的相对比较」依然成立。读这份代码要明白：它度量的是增强的**相对收益**，不是 CIFAR-10 上的最优成绩。

### 4.2 AutoAugment：搜索出来的增强策略

#### 4.2.1 概念说明

AutoAugment（Cubuk et al., 2019, [arXiv:1805.09501](https://arxiv.org/abs/1805.09501)）把「设计增强规则」本身当成一个搜索问题：在代理任务上用强化学习为每个数据集搜出一套策略，策略由 25 个**子策略（sub-policy）**组成，每个子策略是两个有序操作 \((OP_1, P_1, M_1) \to (OP_2, P_2, M_2)\)，其中 \(P\) 是应用概率、\(M\) 是幅度（如旋转多少度、剪切多大）。训练时：

```text
取一张图 x
    │
    ├─ 从 25 个子策略中均匀随机抽 1 个（概率 1/25）
    ├─ 以概率 P1 执行操作1（强度 M1），否则跳过
    └─ 以概率 P2 执行操作2（强度 M2），否则跳过
```

CIFAR-10 策略用到的算子包括剪切、平移、旋转、色调/对比度/锐度调整、Posterize、Solarize、AutoContrast、Equalize 等。对普通使用者来说，这些搜索结果已内置在 torchvision 中——`transforms.autoaugment.AutoAugmentPolicy.CIFAR10` 一个枚举值就是整套 25 子策略。

它与手工增强（`RandomCrop` + `RandomHorizontalFlip`）的本质区别：手工增强靠人的直觉挑选两三种固定扰动；AutoAugment 靠数据搜出一大张「操作×概率×幅度」的组合表，扰动空间丰富得多。

#### 4.2.2 核心流程

接入方式只是 u4-l3 Compose 流水线的一次「插拔」：

```text
transforms.Compose([
    transforms.Resize(32),                                  # 确定性：先统一尺寸
    transforms.AutoAugment(AutoAugmentPolicy.CIFAR10),      # 随机增强：PIL 域
    transforms.ToTensor(),                                  # PIL → Tensor，值域 [0,1]
])
        │
        └─ 注入 Cifar10DataModule(train_transform=...)
                │
                └─ 其余一切（模型/优化器/Trainer/seed）与基线相同
```

注意增强操作的位置：**在 `Resize` 之后、`ToTensor` 之前**——AutoAugment 的算子工作在 PIL 图像域，遵守 u4-l3 讲过的固定顺序「PIL 域变换 → ToTensor → （可选 Normalize）」。

增强对训练动态的影响：训练损失下降变慢（同一张图每个 epoch 长得都不一样，模型更难死记），因此需要**多得多的 epoch** 才能兑现收益——这正是 notebook 把 `max_epochs` 从 40 提到 500 的原因。

#### 4.2.3 源码精读

增强 transform 的定义与注入（与基线唯一实质不同的代码单元）：

- [with-autoaugment.ipynb:L91-L105](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/data-augmentation/autoaugment/with-autoaugment.ipynb#L91-L105) —— `train_transform = transforms.Compose([transforms.Resize(32), transforms.AutoAugment(transforms.autoaugment.AutoAugmentPolicy.CIFAR10), transforms.ToTensor()])`；随后 `L.seed_everything(123)` 与基线同种子，`Cifar10DataModule(..., train_transform=train_transform, ...)` 注入。只传 `train_transform`，评估侧走兜底确定性 transform。

模型与训练配置（与基线逐行对照）：

- [with-autoaugment.ipynb:L7178-L7193](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/data-augmentation/autoaugment/with-autoaugment.ipynb#L7178-L7193) —— 同样的 `resnet18(weights=None)` + `Linear(512, 10)` + `learning_rate=0.05`；Trainer 差异：`max_epochs=500`（L7186）、无 `precision` 设置（默认 32 位）、`devices=[1]`、`deterministic=True`（L7190）。
- **⚠️ 批判性读码点 2**：[with-autoaugment.ipynb:L7189](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/data-augmentation/autoaugment/with-autoaugment.ipynb#L7189) —— CSVLogger 的 `name="baseline"` 是从基线 notebook 复制时忘改的：AutoAugment 的日志也会写进 `logs/baseline/` 目录（靠 Lightning 的 version 子目录区分）。不影响训练，但做本讲实践时要在日志目录里小心别拿错 metrics.csv。

测试结果：

- [with-autoaugment.ipynb:L7311](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/data-augmentation/autoaugment/with-autoaugment.ipynb#L7311) —— `trainer.test(...)` 保存的输出为 `test_acc = 0.7830`，500 epoch 耗时 51.74 分钟。

与基线相比：**68.51% → 78.30%，提升约 9.8 个百分点**，代价是 12.5 倍的 epoch 数。这个交换比是数据增强在小数据集上典型且可观的收益。

#### 4.2.4 代码实践

**实践目标**：亲眼看到 AutoAugment 对同一张图施加的随机扰动，理解「每 epoch 同图不同样」。

1. 操作步骤（示例代码）：

   ```python
   # 示例代码：可视化 AutoAugment 对同一张图的多次扰动
   import matplotlib.pyplot as plt
   from torchvision import datasets, transforms

   train = datasets.CIFAR10(root="./", train=True, download=False)
   img, label = train[0]          # PIL 图像

   aug = transforms.Compose([
       transforms.Resize(32),
       transforms.AutoAugment(transforms.autoaugment.AutoAugmentPolicy.CIFAR10),
   ])

   fig, axes = plt.subplots(1, 6, figsize=(12, 2.5))
   axes[0].imshow(img); axes[0].set_title("original")
   for i in range(1, 6):
       axes[i].imshow(aug(img)); axes[i].set_title(f"aug #{i}")
   for ax in axes: ax.axis("off")
   plt.savefig("autoaugment-samples.png", dpi=150)
   ```

2. 需要观察的现象：五张增强图各不相同——有的被旋转、有的被剪切、有的颜色/对比度明显改变，也有个别几乎没变（对应低概率操作未被触发）。
3. 预期结果：直观体会「子策略随机抽取 + 概率应用」的含义；同一张图在不同 epoch 以不同面貌出现，模型无法逐像素死记。
4. 若你的 torchvision 版本没有 `transforms.autoaugment`（notebook 环境为 0.14.1），待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：把 `transforms.AutoAugment(...)` 移到 `transforms.ToTensor()` 之后会发生什么？

**答案**：会报错。AutoAugment 的算子（剪切、Solarize 等）是 PIL 域实现，输入须为 PIL 图像；`ToTensor()` 之后是张量。这再次印证 u4-l3 的顺序规则：几何/颜色变换在前，`ToTensor` 居中，`Normalize` 最后。

**练习 2**：AutoAugment 版训练 500 epoch 共 51.74 分钟（约 6.2 秒/epoch），基线 40 epoch 共 3.44 分钟（约 5.2 秒/epoch），增强版每 epoch 只慢约 20%？这个对比可靠吗？

**答案**：不可靠，甚至方向都可能失真。两份 notebook 的 `precision`（16 位混合精度 vs 默认 32 位）、`devices`（[0] vs [1]）、是否 `deterministic` 都不同，硬件与精度设置对速度的影响可能与增强开销互相叠加抵消。要量化「增强的耗时开销」，必须在同一设备、同一精度、同一 deterministic 设置下重跑（见本讲综合实践）。

**练习 3**：为什么增强后需要把 `max_epochs` 从 40 提到 500？结合训练损失的角色说明。

**答案**：增强让训练集每 epoch 的表观内容都变化，等价于把有效数据分布撑大，模型拟合训练集变难、训练损失下降变慢；同时泛化上限提高。加上本配方学习率固定不衰减，收益必须靠大量 epoch 慢慢「熬」出来。若增强后仍只训 40 epoch，大概率**低于**基线——增强不是免费午餐，它用收敛速度换泛化上限。

### 4.3 TrivialAugmentWide：免调参增强与三组对照

#### 4.3.1 概念说明

TrivialAugmentWide（Müller & Hutter, 2021, [arXiv:2103.10158](https://arxiv.org/abs/2103.10158)）是对 AutoAugment 复杂性的反思：既然搜索那么贵，干脆**不搜索**。规则简单到一段话讲完——

```text
取一张图 x
    ├─ 从算子池 {Rotate, ShearX, ShearY, TranslateX, TranslateY, Color, Contrast,
    │            Brightness, Sharpness, Posterize, Solarize, AutoContrast, Equalize, Identity}
    │            中均匀随机抽 1 个
    ├─ 从 [0, num_magnitude_bins] 均匀随机抽一个强度（默认 31 档，"Wide" 即指强度范围宽）
    └─ 无条件应用（没有 AutoAugment 那样的应用概率 P）
```

没有任何在目标任务上搜出来的参数，跨数据集零适配。论文结论是它以近乎零的成本追平或超过搜索类方法——「简单基线的胜利」。

对使用者的意义：AutoAugment 需要为数据集挑选对应 policy（`AutoAugmentPolicy.CIFAR10`/`IMAGENET`/`SVHN`），换数据集要动脑；TrivialAugmentWide 换数据集什么都不用改。本讲最后一组对照会检验：在这套仓库配置下，免调参的 TrivialAugment 是否真的不输 AutoAugment。

#### 4.3.2 核心流程

三组实验的关系可以用一张表说清（同为 seed 123、SGD lr=0.05、batch 256、ResNet-18 从零训练、无 Normalize）：

| 配置 | 训练集 transform | max_epochs | test_acc（notebook 保存输出） | 耗时 |
| --- | --- | --- | --- | --- |
| without-augmentation | Resize + ToTensor（兜底） | 40 | 0.6851 | 3.44 min |
| with-autoaugment | Resize + **AutoAugment(CIFAR10)** + ToTensor | 500 | 0.7830 | 51.74 min |
| with-trivialaugment | Resize + **TrivialAugmentWide()** + ToTensor | 1000 | 0.8168 | 98.82 min |

结论一目了然：**免调参的 TrivialAugmentWide 反而最好（81.68%），比 AutoAugment 再高约 3.4 个百分点，比无增强基线高约 13.2 个百分点**——当然它也用了最多的 epoch。

#### 4.3.3 源码精读

transform 定义与注入：

- [with-trivialaugment.ipynb:L92-L106](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/data-augmentation/autoaugment/with-trivialaugment.ipynb#L92-L106) —— `train_transform = transforms.Compose([transforms.Resize(32), transforms.TrivialAugmentWide(), transforms.ToTensor()])`。与 AutoAugment 版逐字符对比，唯一差异是中间那行换成了 `TrivialAugmentWide()`——连参数都不用传。注入方式同样是 `Cifar10DataModule(..., train_transform=train_transform, ...)`。

- **⚠️ 批判性读码点 3**：[with-trivialaugment.ipynb:L16](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/data-augmentation/autoaugment/with-trivialaugment.ipynb#L16) —— 这份 notebook 的 markdown 介绍写着 "trains a ResNet-18 model **with the AutoAugment procedure**"，是从 with-autoaugment 复制时忘改的文案；而它引用的论文与文档链接其实都是 TrivialAugmentWide 的。**判断 notebook 实际做什么，永远以代码单元为准**（标题 "# With TrivialAugment" 和 transform 代码才是真相）——这是 u1-l1 以来反复强调的读码纪律在本讲的又一例证。

训练配置与结果：

- [with-trivialaugment.ipynb:L14178-L14193](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/data-augmentation/autoaugment/with-trivialaugment.ipynb#L14178-L14193) —— 模型与优化器与基线完全相同；Trainer 的 `max_epochs=1000`（L14186）、`devices=[1]`、`deterministic=True`；CSVLogger 这次正确地用了 `name="trivialaugment"`（L14189）。
- [with-trivialaugment.ipynb:L14311](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/data-augmentation/autoaugment/with-trivialaugment.ipynb#L14311) —— `trainer.test(...)` 保存的输出为 `test_acc = 0.8168`，1000 epoch 耗时 98.82 分钟。

三份 notebook 的结果可视化都交给共享的绘图函数：

- [helper_utilities.py:L167-L195](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/data-augmentation/autoaugment/helper_utilities.py#L167-L195) —— `plot_loss_and_acc` 读取 CSVLogger 落盘的 `metrics.csv`，按 epoch 分组求均值后分别绘制 `train_loss` 与 `train_acc`/`val_acc` 曲线。它依赖 CSVLogger 的目录结构（`{log_dir}/metrics.csv`），这就是为什么 Trainer 里要显式配置 logger。
- [helper_utilities.py:L144-L164](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/data-augmentation/autoaugment/helper_utilities.py#L144-L164) —— `plot_val_acc` 是只画验证准确率的轻量版本，本讲综合实践会用它叠加三组曲线。

最后给「对照实验设计」下一个诚实的评语：三组的 `max_epochs`（40/500/1000）、`precision`（16/32/32）、`deterministic`（False/True/True）并不一致，因此这是「三个各自调好的配方之争」而非严格受控实验；但由于模型、数据划分、优化器、学习率、seed、batch size 完全一致，且增强版的 epoch 数只多不少，「增强有显著正收益、TrivialAugmentWide ≥ AutoAugment > 无增强」这一结论方向是稳的。

#### 4.3.4 代码实践

**实践目标**：在同一份代码里切换三种 transform，直观对比增强强度。

1. 操作步骤（示例代码）：

   ```python
   # 示例代码：三种 transform 对同一张图的扰动对比
   import matplotlib.pyplot as plt
   from torchvision import datasets, transforms

   train = datasets.CIFAR10(root="./", train=True)
   img, _ = train[100]

   variants = {
       "none": transforms.Compose([transforms.Resize(32)]),
       "autoaug": transforms.Compose([
           transforms.Resize(32),
           transforms.AutoAugment(transforms.autoaugment.AutoAugmentPolicy.CIFAR10)]),
       "trivial": transforms.Compose([
           transforms.Resize(32),
           transforms.TrivialAugmentWide()]),
   }
   fig, axes = plt.subplots(3, 5, figsize=(11, 6.5))
   for row, (name, tf) in enumerate(variants.items()):
       axes[row, 0].imshow(img); axes[row, 0].set_ylabel(name)
       for col in range(1, 5):
           axes[row, col].imshow(tf(img))
   for ax in axes.flat:
       ax.set_xticks([]); ax.set_yticks([])
   plt.tight_layout(); plt.savefig("aug-comparison.png", dpi=150)
   ```

2. 需要观察的现象：`none` 行五张完全相同；`autoaug` 行的扰动「温和而多样」（部分图未变）；`trivial` 行扰动更狂野——强度是宽范围均匀抽取的，偶尔出现大角度旋转或极端色彩。
3. 预期结果：理解两种策略的风格差异，并能解释为什么 TrivialAugment 需要模型训练更久才能消化这种强扰动。
4. 待本地验证（取决于本地 torchvision 是否支持上述 API）。

#### 4.3.5 小练习与答案

**练习 1**：`TrivialAugmentWide()` 连 `AutoAugmentPolicy` 这样的参数都没有。这为什么反而是优点？

**答案**：AutoAugment 的 policy 是按数据集搜索的，换数据集（甚至换图像统计特性）就要重新选择；TrivialAugmentWide 无任何数据集相关参数，跨任务零配置，避免选错 policy 的风险。对教学与快速实验，「一行接入、无需决策」的价值很大。

**练习 2**：三组对照中，能不能说「TrivialAugmentWide 比 AutoAugment 好 3.4 个百分点」？

**答案**：不能下这么强的因果结论。两者的 epoch 数不同（500 vs 1000），AutoAugment 在 1000 epoch 下未必还是 78.30%；`precision` 等配置也不同。严格说法是：「在本仓库的三份配置下，TrivialAugmentWide（1000 epoch）达到 81.68%，高于 AutoAugment（500 epoch）的 78.30%」。要下强结论，需在相同 epoch 预算下重跑——这正是综合实践的任务。

**练习 3**：假设你要把本讲增强迁移到自己的 10 类工业缺陷检测数据集（每类仅几百张图），先试哪种？要注意什么？

**答案**：先试 TrivialAugmentWide（零配置、对中小数据集通常稳健），但要审查算子是否破坏任务语义——例如缺陷颜色本身就是判别特征时，`Color`/`Solarize` 等颜色扰动可能抹掉类别差异；旋转若破坏方向语义（如文字、裂纹走向）也应排除。增强注入的是「标签不变的先验」，先验错了就帮倒忙。

## 5. 综合实践

**任务：复现一个严格版三组对照实验，量化增强收益。**

背景：仓库原始三胞胎的 epoch/precision/deterministic 不对齐（4.3.3 已指出）。你的任务是把它改成受控实验并重新得出结论。

1. **准备**：复制 without-augmentation.ipynb 三份（或写一个接受 transform 名称参数的脚本），统一 Trainer 配置：`max_epochs=60`（视算力可减）、`precision=16`、`deterministic=True`、同一 `devices`、`CSVLogger` 分别命名 `exp-none` / `exp-auto` / `exp-trivial`（别重蹈 with-autoaugment 的 `name="baseline"` 复制遗留）。
2. **三组 transform**：按 4.2.3 / 4.3.3 的 Compose 原样构造，只改中间一行。
3. **训练后**：用 `plot_val_acc(trainer.logger.log_dir, ...)`（[helper_utilities.py:L144-L164](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/data-augmentation/autoaugment/helper_utilities.py#L144-L164)）分别绘制三条验证准确率曲线；再把三个 `metrics.csv` 读入同一张图叠加（示例代码）：

   ```python
   # 示例代码：叠加三组验证准确率
   import pandas as pd, matplotlib.pyplot as plt

   fig, ax = plt.subplots(figsize=(7, 4.5))
   for name in ["exp-none", "exp-auto", "exp-trivial"]:
       df = pd.read_csv(f"logs/{name}/version_0/metrics.csv")
       curve = df.groupby("epoch")["val_acc"].mean()
       ax.plot(curve.index, curve.values, label=name)
   ax.set_xlabel("Epoch"); ax.set_ylabel("Validation ACC")
   ax.grid(True); ax.legend(); plt.savefig("three-way-comparison.png", dpi=150)
   ```

4. **记录**：每组的最终 `test_acc`（`trainer.test`）、达到基线最高 val_acc 所需 epoch、每 epoch 平均耗时。
5. **观察与预期**：
   - 早期（前 10 epoch）无增强组 val_acc 领先，增强组因随机扰动收敛慢；
   - 后期增强组反超，泛化缺口（train_acc − val_acc）更小；
   - 在相同 epoch 预算下，TrivialAugment 与 AutoAugment 的次序未必与仓库原始结果一致——如出现交叉或反超，请如实记录并讨论 epoch 预算对结论的影响。
   - **待本地验证**：以上为基于仓库 notebook 与增强原理的预期，具体数字以你本地运行为准。

## 6. 本讲小结

- **数据增强 = 把标签不变的随机扰动注入训练集**，数学上是最小化 \(\mathbb{E}_{t\sim\mathcal{T}}[\ell(f(t(x)),y)]\)，用收敛速度换泛化上限；评估必须保持确定性，故 transform 分「训练随机 / 评估确定」两套，`Cifar10DataModule` 的双参数注入接口正是为此设计。
- **AutoAugment** 用搜索得到的 25 子策略（操作×概率×幅度）丰富扰动空间，一行 `transforms.AutoAugment(AutoAugmentPolicy.CIFAR10)` 接入；**TrivialAugmentWide** 零参数、随机算子+宽范围随机强度，免调参却在本对照中效果最好。
- 三组对照（同一模型/优化器/seed/划分）：无增强 68.51%（40 epoch）→ AutoAugment 78.30%（500 epoch）→ TrivialAugment 81.68%（1000 epoch）；增强操作插入位置固定在 `Resize` 之后、`ToTensor` 之前（PIL 域）。
- 批判性读码收获三处：验证子集与训练子集共享同一 dataset 对象（验证集也被增强，指标口径须牢记）；with-trivialaugment 的 markdown 文案仍写 "AutoAugment procedure"（以代码为准）；with-autoaugment 的 CSVLogger 名字忘改导致日志目录混用。
- 增强收益的度量依赖**对齐配方**的对照实验；epoch 预算本身就是配方的一部分——「增强后更慢」不是失败，而是需要更多 epoch 兑现收益的正常现象。

## 7. 下一步学习建议

本讲之后，建议按以下顺序继续：

1. **u8-l4 梯度检查点**：同属「用时间换资源」的主题——本讲用计算换泛化，下一讲用重计算换显存，可对照体会工程权衡。
2. **对照阅读 PyTorch 主库的训练技巧**：`pytorch_ipynb/tricks/cyclical-learning-rate.ipynb` 可与本讲结合成「增强 + 学习率调度」的更强配方——本讲的固定 lr=0.05 正是最值得改进的短板。
3. **深入 torchvision 文档**：阅读 `transforms.AutoAugment` 与 `transforms.TrivialAugmentWide` 的官方文档（两份 notebook 的 References 单元都给了链接），了解 `fill`、`num_magnitude_bins`、`interpolation` 等本讲未展开的参数，并思考如何为自有数据集裁剪算子集合。
4. **延伸阅读**：TrivialAugment 论文（arXiv:2103.10158）对「搜索式 vs 免调参增强」的系统对比，是理解本讲 81.68% > 78.30% 这一结果背后原理的最佳材料。
