# 迁移学习与训练技巧

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚**迁移学习（Transfer Learning）**解决什么问题，以及它为什么在图像分类里如此常用。
- 区分两种核心迁移策略：**特征提取（feature extraction / 冻结）** 与 **微调（fine-tuning）**，并知道什么时候用哪种。
- 看懂一个深度网络的「训练技巧工具箱」：权重初始化、批归一化、Dropout、动量/Adam 优化器、梯度裁剪、学习率衰减，理解它们各自修的是训练过程中的哪一块毛病。
- 理解**对抗样本（adversarial examples）**是如何通过对输入图像做梯度下降「骗过」网络的。
- 在真实数据集 **Oxford-IIIT Pets** 上，亲手完成一次迁移学习，并对比「冻结」与「微调」两种策略的效果。

## 2. 前置知识

本讲是计算机视觉单元的第三课，承接前两讲：

- **图像即 NumPy 数组**（u3-l1）：彩色图形状为 \((H,W,3)\)，喂进网络前要缩放、裁剪、归一化到模型期望的范围。
- **CNN 与经典架构**（u3-l2）：一个图像 CNN 通常由「卷积+池化组成的**特征提取器**」和「全连接层组成的**分类头**」两部分构成；LeNet/VGG/ResNet/MobileNet 等是常见预训练骨架。本讲大量出现 VGG-16 与 ResNet-18，请确认你对其「金字塔结构」有印象。
- **过拟合与正则化**（u2-l5）：训练误差持续下降、验证误差却由降转升，就是过拟合；应对手段有三类——加数据、降复杂度、正则化（Dropout、早停、权重衰减）。本讲的训练技巧本质都是在「让深网络训得动」和「别过拟合」之间找平衡。

两个本讲会反复用到、但值得先点明的术语：

- **ImageNet**：一个含约 1400 万张、1000 类的通用图像数据集。所谓「预训练模型」默认就是在它上面训出来的，因此它们已经学会了边缘、纹理、眼睛、车轮等大量通用视觉模式。
- **`requires_grad`**：PyTorch 里每个参数张量都有一个布尔标志。设为 `False` 表示「不要为它计算梯度、不要更新它」，这就是「冻结」的实现方式。

## 3. 本讲源码地图

本讲围绕 `lessons/4-ComputerVision/08-TransferLearning/` 目录展开，核心文件如下：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/08-TransferLearning/README.md) | 本课讲义正文：讲迁移学习动机、预训练模型当特征提取器、Cats vs. Dogs 数据集，以及「理想猫 / 对抗狗」可视化。 |
| [TrainingTricks.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/08-TransferLearning/TrainingTricks.md) | 训练技巧补充读物：数值范围、权重初始化、批归一化、Dropout、优化器（动量/Adam）、梯度裁剪、学习率衰减。 |
| [TransferLearningPyTorch.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/08-TransferLearning/TransferLearningPyTorch.ipynb) | PyTorch 版可执行笔记：加载 VGG-16、手动抽特征训练小分类器、端到端冻结训练、解冻微调、ResNet 结构。 |
| [Dropout.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/08-TransferLearning/Dropout.ipynb) | TensorFlow/Keras 版实验：在 MNIST 上对比 dropout = 0 / 0.2 / 0.5 / 0.8 的验证准确率曲线。 |
| [pytorchcv.py](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/08-TransferLearning/pytorchcv.py) | 笔记调用的辅助库：封装了 `train` / `train_long` / `validate` / `check_image_dir` 等，把训练循环细节藏起来。 |
| [lab/README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/08-TransferLearning/lab/README.md) | 本课作业：在 Oxford-IIIT Pets（35 类猫狗品种）上做迁移学习分类。 |

> 提醒：本目录还有 `TransferLearningTF.ipynb`（TensorFlow 版迁移学习）和 `AdversarialCat_TF.ipynb`（理想猫/对抗狗的复现代码）。本讲以 PyTorch 为主线，TF 版作为对照阅读。

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：

- **4.1 迁移学习策略**：预训练模型当特征提取器、冻结、微调。
- **4.2 数据增强与学习率调度**：transform 流水线、学习率衰减、优化器选择。
- **4.3 Dropout 与对抗样本**（兼谈批归一化等训练技巧）：Dropout 正则、批归一化、把网络「骗」成猫。

### 4.1 迁移学习策略

#### 4.1.1 概念说明

从头训练一个 CNN 又慢又吃数据。README 开篇就点明了痛点：

> Training CNNs can take a lot of time, and a lot of data is required for that task.（训练 CNN 很耗时，也需要大量数据。）—— [README.md:L3](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/08-TransferLearning/README.md#L3)

但训练时间大部分花在「学会那些最好的**底层滤波器**」上——也就是边缘、纹理这些通用模式。既然 ImageNet 上已经有人训好了一套通用滤波器，我们何不直接拿来用？这就是**迁移学习**：把一个网络在源任务上学到的知识，迁移到另一个目标任务上。README 给出的定义是：

> This approach is called **transfer learning** ... we transfer some knowledge from one neural network model to another.（这种做法叫迁移学习……我们把知识从一个网络迁移到另一个。）—— [README.md:L7](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/08-TransferLearning/README.md#L7)

迁移学习的关键直觉是**特征的可复用性**：CNN 的浅层卷积学到的是与具体类别无关的通用视觉特征（横线、竖线、角点、纹理），深层才逐步组合出「眼睛」「耳朵」这类语义。所以一个在 ImageNet 上训好的模型，其卷积部分对「几乎任何自然图像」都能抽出有用的特征。我们通常只需在它顶上接一个新的分类头，用自己那点小数据训分类头即可。

迁移学习有两种典型策略，难度递增：

1. **特征提取（feature extraction）/ 冻结（freezing）**：把预训练卷积部分的权重**冻住**不更新，只训练新接的分类头。可训练参数极少，所需数据和算力都小。
2. **微调（fine-tuning）**：在分类头训练稳定后，把卷积部分**解冻**，用**较小的学习率**继续训练整个网络，让特征也稍稍适配新任务。效果通常更好，但更慢、更吃数据。

README 也列了常见可选骨架：VGG-16/19（简单、上手首选）、ResNet（更深、更准但更重）、MobileNet（小、适合移动端）——见 [README.md:L17-L19](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/08-TransferLearning/README.md#L17-L19)。

#### 4.1.2 核心流程

把一个预训练 VGG-16 改造成猫狗二分类器，整体流程是：

```text
1. 数据：Cats vs. Dogs 图像 → Resize(256) → CenterCrop(224) → ToTensor → 用 ImageNet 的均值/方差归一化
2. 加载预训练模型：vgg = torchvision.models.vgg16(pretrained=True)
3. 拆看结构：vgg.features（卷积特征提取器） / vgg.avgpool / vgg.classifier（1000 类分类头）
4. 【策略 A：特征提取 / 冻结】
   a. 换头：vgg.classifier = nn.Linear(25088, 2)      # 1000 类 → 2 类
   b. 冻结：for p in vgg.features.parameters(): p.requires_grad = False
   c. 训练若干 epoch（只更新分类头那 5 万参数）
5. 【策略 B：微调】
   a. 解冻：for p in vgg.features.parameters(): p.requires_grad = True
   b. 用更小的学习率（如 lr=1e-4）再训几个 epoch
6. 保存 / 加载：torch.save(vgg, 'data/cats_dogs.pth') / torch.load(...)
```

记一个量化的直觉：原始 VGG-16 有约 **1.38 亿**参数全部可训练；冻结卷积后只剩分类头约 **5 万**参数可训练——参数量直接降了三个数量级，这正是迁移学习「省」的来源。

形式化地，设预训练特征提取器为 \(f_\theta\)、分类头为 \(g_\phi\)，则：

- 特征提取阶段：固定 \(\theta\)，只优化 \(\phi\)，损失为 \(\mathcal{L}(g_\phi(f_\theta(x)), y)\)。
- 微调阶段：联合优化 \((\theta, \phi)\)，但对 \(\theta\) 用更小学习率，避免一步把好不容易学到的通用特征「冲坏」。

#### 4.1.3 源码精读

**(1) 把预训练 VGG 当特征提取器——手动抽特征 + 训一个小分类器**

笔记先演示了「特征提取」最纯粹的形态：用 `vgg.features` 只跑卷积部分，把每张图压成一个 \(512\times7\times7\) 的特征向量，预先算好存进 `feature_tensor`，再在它上面训一个一层的分类网络：

```python
net = torch.nn.Sequential(
    torch.nn.Linear(512*7*7, 2),
    torch.nn.LogSoftmax()
).to(device)
```

> 见 `TransferLearningPyTorch.ipynb` 中标题为 *Extracting VGG features* 与 *Pre-trained models* 的若干 cell。这一步在小子集上就达到了约 **98%** 的验证准确率——证明 ImageNet 学到的特征对猫狗几乎「开箱即用」。

**(2) 换头 + 冻结（端到端迁移）**

笔记里换头和冻结是两行核心代码（`TransferLearningPyTorch.ipynb`，*Transfer learning using one VGG network* 一节）：

```python
vgg.classifier = torch.nn.Linear(25088, 2).to(device)   # 把 1000 类分类头换成 2 类

for x in vgg.features.parameters():
    x.requires_grad = False                              # 冻结卷积特征提取器
```

执行 `summary(vgg, (1,3,244,244))` 后可以看到：总参数约 1476 万，**可训练参数只剩 50178**，非训练参数约 1471 万——冻结生效的直接证据就在这张表里。

**(3) 微调：解冻 + 更小学习率**

笔记在 *Fine-tuning transfer learning* 一节先解冻、再用 `lr=0.0001`（比默认 0.01 小两个数量级）继续训练：

```python
for x in vgg.features.parameters():
    x.requires_grad = True                  # 解冻卷积层

train_long(vgg, train_loader, test_loader,
           loss_fn=torch.nn.CrossEntropyLoss(),
           epochs=1, print_freq=90, lr=0.0001)
```

笔记用一段重要提示解释了「为什么必须先冻结训几轮、再解冻」：如果一上来就端到端训练，分类头还是随机初始化的大误差会通过反向传播**毁掉**卷积层里宝贵的预训练权重。所以正确顺序永远是「先冻结稳定分类头 → 再解冻小步微调」。

**(4) 训练循环藏在 `pytorchcv.py` 里**

`train_long` 的实现就是标准的「五件套」训练循环，只不过把逐 minibatch 的中间结果打印出来：[pytorchcv.py:L70-L89](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/08-TransferLearning/pytorchcv.py#L70-L89)。关键四步是：

```python
optimizer.zero_grad()          # ① 清空上一步残留梯度（PyTorch 梯度默认累加）
out = net(features.to(device)) # ② 前向
loss = loss_fn(out, lbls)      # ③ 算损失
loss.backward()                # ④ 反向传播求梯度
optimizer.step()               # ⑤ 按学习率更新参数
```

这与 u2-l5 讲过的 PyTorch 训练五件套完全一致；`train_long` 只是多了 `print_freq` 打印和 `lr` 参数透传（默认 `lr=0.01`，微调时传 `0.0001`）。

#### 4.1.4 代码实践

**实践目标**：亲手完成「换头 → 冻结 → 训练 → 解冻 → 微调」的完整迁移学习链路，观察可训练参数量的变化与微调前后验证准确率的变化。

**操作步骤**：

1. 在 `ai4beg` 内核下打开 `TransferLearningPyTorch.ipynb`，从上到下依次运行到 *Transfer learning using one VGG network* 一节。
2. 运行 `vgg.classifier = torch.nn.Linear(25088,2)` 与冻结循环后，执行 `summary(vgg,(1,3,244,244))`，记录 `Total params` / `Trainable params` / `Non-trainable params` 三行。
3. 运行 `train_long(...)`（冻结训练，1 个 epoch），记下结尾打印的 `validation acc`。
4. 接着运行解冻循环与 `train_long(..., lr=0.0001)`（微调，1 个 epoch），记下微调后的 `validation acc`。

**需要观察的现象**：

- 冻结后 `Trainable params` 应约为 5 万级（不是 1.38 亿）。
- 冻结训练 1 个 epoch 即可达到约 96%~97% 的验证准确率。
- 微调刚开始的几个 minibatch 准确率通常会**先掉一下**（笔记里从 1.0 掉到 ~0.90），随后逐步回升并略超过冻结时的水平。

**预期结果**：冻结 ≈ 96.7%，微调后 ≈ 97.4%（与笔记输出量级一致；具体数值取决于数据划分与硬件，**待本地验证**）。

> ⚠️ 微调这一步要把梯度反向传过整个 VGG，速度明显变慢。笔记建议「看前几个 minibatch 的趋势即可」，CPU 上尤为明显。

#### 4.1.5 小练习与答案

**练习 1**：为什么微调时要**先冻结训几轮、再解冻**，而不是一开始就端到端训练？

> **参考答案**：分类头刚换上去是随机初始化的，损失很大、梯度很「乱」。若此时卷积层也参与训练，这些大而乱的梯度会通过反向传播把卷积层里 ImageNet 学到的通用特征权重冲坏。先冻结能让分类头先在新特征上稳定下来，误差变小后，再用小学习率解冻微调，对预训练权重的破坏就小得多。

**练习 2**：冻结前后，模型的「可训练参数」从约 1.38 亿降到约 5 万。这对**所需训练数据量**和**过拟合风险**分别意味着什么？

> **参考答案**：可训练参数少了三个数量级，意味着用很少的样本就能把这些参数拟合好（数据需求大降）；同时参数越少、模型表达力越受限，过拟合风险也相应降低。这正是迁移学习「省数据、不易过拟合」的根本原因。

**练习 3**：如果你的目标图像与 ImageNet 自然图像差异很大（例如 X 光片、电路板缺陷），冻结特征提取还大概率好用吗？该怎么办？

> **参考答案**：大概率不好用。因为 ImageNet 学到的纹理/边缘特征对医学或工业图像未必有意义。这种情况下应优先**微调**（甚至从头训练），让卷积层重新学习该领域的底层特征；同时降低对最终准确率的预期。

---

### 4.2 数据增强与学习率调度

#### 4.2.1 概念说明

这一节讲两个「让训练更稳、更省数据」的工程旋钮：**数据增强**和**学习率调度**。

**数据增强（data augmentation）**：通过对训练图像做随机变换（翻转、旋转、裁剪、颜色抖动等），人为「造」出更多样本。它的作用是双重的——既扩充了有效数据量，又让模型见过同一物体的各种姿态，从而提升泛化、抑制过拟合。TrainingTricks.md 在讲防止过拟合时也把它和「增加数据」归为同一类思路。

**学习率调度（learning rate schedule）**：训练初期希望学习率大、收敛快；接近最优解时又希望学习率小、做精细微调，避免在最优点附近来回震荡。TrainingTricks.md 说得很直白：

> 训练成功往往取决于学习率参数 \(\eta\)……大多数情况下我们希望在训练过程中**逐步降低** \(\eta\)。

迁移学习里这条规律尤其明显：4.1 节微调时把 `lr` 从默认 0.01 降到 0.0001，就是「对预训练权重要温柔」的直接体现。

#### 4.2.2 核心流程

**(a) 本课的数据流水线**

笔记里图像进 VGG 之前，要经过这样一条 transform 流水线（`TransferLearningPyTorch.ipynb`，*Cats vs. Dogs Dataset* 一节）：

```python
std_normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                     std=[0.229, 0.224, 0.225])
trans = transforms.Compose([
    transforms.Resize(256),        # 短边缩放到 256
    transforms.CenterCrop(224),    # 中心裁出 224×224
    transforms.ToTensor(),         # 转 [0,1] 张量
    std_normalize                  # 用 ImageNet 均值/方差归一化
])
```

这里要特别强调两点：

1. **必须用 ImageNet 的均值/方差归一化**。因为预训练 VGG 当年就是吃这样归一化的输入训出来的；喂给它别的尺度的输入，特征就会失真。这条流水线也原样封装在 [pytorchcv.py:L147-L155](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/08-TransferLearning/pytorchcv.py#L147-L155) 的 `common_transform()` 里。
2. 上述 `Resize + CenterCrop` 属于**确定性的预处理**，每次给同一张图得到同一张输入。真正的**数据增强**是带随机性的（如 `RandomHorizontalFlip`、`RandomResizedCrop`），训练时每次都给模型看「略微不同」的样本。

> 诚实说明：本课的 PyTorch 笔记重点放在迁移学习本身，transform 流水线只做了基础预处理，没有大量随机增强。数据增强的概念在本课以「思路 + 可扩展点」的形式讲解；如要落地，把随机变换插进 `transforms.Compose` 即可（见下方实践）。

**(b) 学习率衰减的两种做法**

TrainingTricks.md 给出学习率衰减的两种实现——见 [TrainingTricks.md:L93-L97](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/08-TransferLearning/TrainingTricks.md#L93-L97)：

- **简单衰减**：每个 epoch 把 \(\eta\) 乘以一个小于 1 的常数（例如 0.98）。
- **更复杂的调度（learning rate schedule）**：例如余弦退火、阶梯下降等。

形式化地，最朴素的指数衰减为：

\[
\eta_{t+1} = \gamma\,\eta_{t},\qquad 0<\gamma<1
\]

**(c) 优化器：动量与 Adam**

学习率调度离不开对优化器的选择。TrainingTricks.md 介绍了从经典 SGD 到动量 SGD、再到 Adam 的演进：

- **带动量的 SGD**：保留一部分上一步的梯度方向，像带「惯性」一样平滑更新轨迹。引入速度向量 \(v\)：

\[
v^{t+1} = \gamma\, v^{t} - \eta\,\nabla\mathcal{L},\qquad w^{t+1} = w^{t} + v^{t+1}
\]

其中 \(\gamma\) 控制惯性大小：\(\gamma=0\) 退化为普通 SGD，\(\gamma=1\) 是纯惯性。见 [TrainingTricks.md:L68-L75](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/08-TransferLearning/TrainingTricks.md#L68-L75)。

- **Adam / Adagrad / RMSProp**：核心思想是「只用梯度的方向、忽略其绝对大小」，从而对抗梯度爆炸/消失。TrainingTricks.md 给了一句非常实用的结论——「不确定用什么优化器，就用 **Adam**」（见 [TrainingTricks.md:L87](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/08-TransferLearning/TrainingTricks.md#L87)）。这也解释了为什么 `pytorchcv.py` 的 `train` / `train_long` 默认用 `torch.optim.Adam`——见 [pytorchcv.py:L28](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/08-TransferLearning/pytorchcv.py#L28) 与 [pytorchcv.py:L71](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/08-TransferLearning/pytorchcv.py#L71)。

#### 4.2.3 源码精读

**(1) 归一化常数的来源与封装**

笔记里的 `std_normalize` 用的是 ImageNet 的统计量 `mean=[0.485,0.456,0.406]`、`std=[0.229,0.224,0.225]`，这套常数在 [pytorchcv.py:L148-L149](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/08-TransferLearning/pytorchcv.py#L148-L149) 一字不差地重复出现——说明它是「喂给预训练模型的标准动作」，必须保持一致。

**(2) 学习率作为函数参数贯穿训练函数**

`train_long` 的签名里 `lr=0.01` 是默认值（[pytorchcv.py:L70](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/08-TransferLearning/pytorchcv.py#L70)），微调时笔记传入 `lr=0.0001`，这就是「调度」最朴素的形式：**不同阶段用不同大小的学习率**。`train_epoch` 内部则用 `optimizer = optimizer or torch.optim.Adam(net.parameters(), lr=lr)` 把学习率交给 Adam（[pytorchcv.py:L28](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/08-TransferLearning/pytorchcv.py#L28)）。

#### 4.2.4 代码实践

**实践目标**：在迁移学习的 transform 流水线里加入**随机数据增强**，观察它对验证准确率与训练/验证差距（过拟合程度）的影响。

**操作步骤**：

1. 复制 `trans`（训练用）和 `trans_test`（测试用，保持确定性）两条流水线：

   ```python
   # 示例代码：在原流水线基础上为「训练集」增加随机增强
   train_trans = transforms.Compose([
       transforms.Resize(256),
       transforms.RandomResizedCrop(224),       # 随机裁剪+缩放
       transforms.RandomHorizontalFlip(),       # 随机水平翻转
       transforms.ToTensor(),
       std_normalize
   ])
   # 测试集仍用确定性的 Resize+CenterCrop，保证评估可复现
   ```

   > 上面是**示例代码**，不是仓库原有内容；它演示了「把随机变换插进 Compose」的标准写法。

2. 用 `train_trans` 重新构建 `dataset`，重新冻结训练 1 个 epoch，记录训练准确率与验证准确率。
3. 对比「无增强」与「有增强」两次的 `Train acc` 与 `Val acc` 差值。

**需要观察的现象**：

- 加增强后，`Train acc` 通常会**略降**（因为训练变难了，模型每次看到的图都不一样）。
- 但 `Val acc` 往往**持平或略升**，且训练/验证准确率的差距缩小——这正是抗过拟合的信号。

**预期结果**：差距收窄是增强生效的典型表现；具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么喂给预训练 VGG 的图像必须用 ImageNet 的均值/方差归一化，而不能用自己的 `(0.5,0.5,0.5)`？

> **参考答案**：预训练时网络就是在这个特定归一化下学到权重分布的；输入分布若与训练时不一致，每一层的激活都会偏移，浅层抽取的特征就不再可靠，相当于自废武功。迁移学习的一条铁律是「**输入预处理要和预训练时保持一致**」。

**练习 2**：把学习率衰减写成 \(\eta_{t+1}=0.98\,\eta_t\)。若初始 \(\eta_0=0.01\)，训练 50 个 epoch 后学习率约为多少？这说明了什么？

> **参考答案**：\(\eta_{50}=0.01\times0.98^{50}\approx0.01\times0.3645\approx0.0036\)。说明指数衰减前期降得慢、后期越降越慢，50 个 epoch 后仍保留约 1/3 的初始学习率——适合「长时间训练 + 平滑收敛」的场景；若想快速压低学习率，应选更激进的调度（如余弦退火）。

**练习 3**：为什么 `pytorchcv.py` 的训练函数默认用 Adam 而不是普通 SGD？

> **参考答案**：Adam 结合了动量（方向平滑）和自适应学习率（按梯度各维历史幅度缩放），对学习率的初始选择不那么敏感、收敛快，是「不确定时最稳妥的选择」。课程作为入门教程，优先用省心的 Adam，让读者把注意力放在迁移学习本身。

---

### 4.3 Dropout 与对抗样本

#### 4.3.1 概念说明

迁移学习解决了「用别人的网络」，但训练深网络本身还有一堆「暗坑」：数值会跑飞、梯度会消失/爆炸、模型会过拟合。TrainingTricks.md 是一份「填坑工具箱」，本模块挑三个最有代表性的讲：**Dropout**、**批归一化（Batch Normalization）**，以及一个反直觉的应用——**对抗样本**。

**Dropout（随机失活）**：训练时按一定比例（典型 10%~50%）随机把上一层的一些神经元**置零**，再传给下一层。听起来像「自残」，但它能：①把优化过程「踹」出局部最优；②相当于隐式地同时训练了许多个子网络再取平均（implicit model averaging），从而提升泛化、抑制过拟合。TrainingTricks.md 的原话见 [TrainingTricks.md:L36-L44](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/08-TransferLearning/TrainingTricks.md#L36-L44)。有意思的是，VGG-16 的分类头里本来就内置了两层 `Dropout(p=0.5)`（你在 `print(vgg)` 的输出里能看到）。

**批归一化（Batch Normalization, BN）**：训练深网络时，各层激活值的尺度容易越跑越离谱，导致数值不稳定。BN 层在「加权之后、激活之前」对该 minibatch 的数据做一次减均值、除标准差的归一化，把信号拉回合理范围。TrainingTricks.md 说它能带来更高的最终精度和更快的训练——见 [TrainingTricks.md:L28-L30](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/08-TransferLearning/TrainingTricks.md#L28-L30)。ResNet 的每个残差块里都内嵌了 BN 层（`print(resnet)` 输出里的 `BatchNorm2d`），这也是 ResNet 能训到上百层的关键之一。

**对抗样本（adversarial examples）**：这是迁移学习 README 里一段很精彩的内容。预训练网络脑子里有「理想猫」的概念，我们可以从一张随机图出发，用**梯度下降优化图像本身**（而不是权重），让网络越来越相信「这是猫」。README 解释了为什么直接这么做只会得到一团噪声，以及如何用 **variation loss（全变分损失）** 把它变平滑、显出可辨认的纹理——见 [README.md:L40-L46](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/08-TransferLearning/README.md#L40-L46)。同一思路可用于**对抗攻击**：拿一张狗的图，微微扰动几下，就能让网络把它判成猫——见 [README.md:L52](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/08-TransferLearning/README.md#L52)。

> 这揭示了深度网络一个令人不安的特性：它依赖的往往是我们人眼看不出的「纹理统计」，而非真正的语义。这也是后续 AI 伦理（u5-l5）要讨论的可信性问题之一。

#### 4.3.2 核心流程

**(a) Dropout 在 Keras 里的对比实验**

`Dropout.ipynb` 用 MNIST + 一个小 CNN，把 dropout 从 0 扫到 0.8，对比验证准确率曲线。核心是定义一个带 dropout 参数 `d` 的 `train` 函数，在 `Flatten` 与最终 `Dense(10)` 之间插一层 `Dropout(d)`，然后对 `[0, 0.2, 0.5, 0.8]` 各训 5 个 epoch：

```python
def train(d):
    model = keras.Sequential([
        keras.layers.Conv2D(32, (3,3), activation="relu", input_shape=(28,28,1)),
        keras.layers.MaxPooling2D(pool_size=(2,2)),
        keras.layers.Conv2D(64, (3,3), activation="relu"),
        keras.layers.MaxPooling2D(pool_size=(2,2)),
        keras.layers.Flatten(),
        keras.layers.Dropout(d),            # ← 关键：失活比例
        keras.layers.Dense(10, activation="softmax")
    ])
    model.compile(loss='sparse_categorical_crossentropy', optimizer='adam', metrics=['acc'])
    return model.fit(x_train, y_train, validation_data=(x_test,y_test), epochs=5, batch_size=64)

res = { d : train(d) for d in [0, 0.2, 0.5, 0.8] }
```

形式化地，倒置 Dropout（inverted dropout，主流实现）在训练时对激活值做：

\[
\tilde{x} = \frac{x \odot m}{1-p},\qquad m_i \sim \mathrm{Bernoulli}(1-p)
\]

其中 \(p\) 是失活比例，\(m\) 是随机掩码，除以 \(1-p\) 是为了保持期望不变；推理时则关闭 dropout、直接用全部神经元。

**(b) 对抗样本的生成循环**

README 配图描述的是一个「图像优化循环」：固定网络权重，把输入图像当作可优化变量，对「让网络输出猫」的损失做梯度下降。为避免结果像噪声，总损失里再加一项 variation loss 惩罚相邻像素差异过大。

#### 4.3.3 源码精读

**(1) Dropout 的效果结论**

`Dropout.ipynb` 最后一段给出了实验结论（标题为 *The Effect of Dropout* 的 markdown cell）：

- dropout 在 0.2~0.5 区间时，训练最快、整体效果最好；
- 完全不用 dropout（\(d=0\)）时，训练过程更不稳、更慢；
- dropout 太大（0.8）反而变差。

从它的训练日志也能直观看到：5 个 epoch 后，\(d=0.5\) 的 `val_acc` 约为 0.9899，略高于 \(d=0\) 的 0.9894；而 \(d=0.8\) 前期 `val_acc` 明显更低（epoch 1 仅 0.9732）。

**(2) Batch Normalization 在 ResNet 里的现身**

`TransferLearningPyTorch.ipynb` 在末尾 `print(torchvision.models.resnet18())` 展示了 ResNet-18 结构，其中每个 `BasicBlock` 里都能看到 `BatchNorm2d` 层（紧跟每个 `Conv2d` 之后）。笔记专门有一节 *Batch Normalization* 解释它的作用——把流经网络的数值拉回 \([-1,1]\) 或 \([0,1]\) 的合理区间，显著提升深网络的训练稳定性。这与 TrainingTricks.md [L28-L30](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/08-TransferLearning/TrainingTricks.md#L28-L30) 的描述一致。

> 延伸：TrainingTricks.md 这份工具箱里还有**权重初始化**（Xavier 初始化，[L19-L24](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/08-TransferLearning/TrainingTricks.md#L19-L24)）、**梯度裁剪**（gradient clipping，[L91](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/08-TransferLearning/TrainingTricks.md#L91)）等，原理都是「让数值与梯度待在合理尺度」，可与 BN、Adam 配合使用。

#### 4.3.4 代码实践

**实践目标**：亲手跑 `Dropout.ipynb`，在 MNIST 上对比四种 dropout 比例的验证准确率曲线，建立「dropout 不是越大越好」的直觉。

**操作步骤**：

1. 在 `ai4beg` 内核下打开 `Dropout.ipynb`（它用 TensorFlow/Keras）。
2. 依次运行到 `res = { d : train(d) for d in [0,0.2,0.5,0.8] }`，等待四组训练完成（每组 5 个 epoch）。
3. 运行最后的绘图 cell，把四条 `val_acc` 曲线画在同一张图上。

**需要观察的现象**：

- \(d=0\)（无 dropout）的曲线最「跳」，收敛相对慢。
- \(d=0.2\) 与 \(d=0.5\) 的曲线上升更快、最终略高。
- \(d=0.8\) 的曲线明显偏低、起步最慢。

**预期结果**：与笔记结论一致——0.2~0.5 最佳，0.8 明显变差；精确数值**待本地验证**（取决于 CPU/GPU 与 TF 版本）。

#### 4.3.5 小练习与答案

**练习 1**：Dropout 在**训练时**和**推理（预测）时**的行为有何不同？为什么？

> **参考答案**：训练时按概率 \(p\) 随机置零一部分神经元（并除以 \(1-p\) 保期望）；推理时关闭 dropout、使用全部神经元。原因是推理要给出稳定、确定的输出，而训练时的随机失活只是为了正则化、逼网络学到更鲁棒的表征。

**练习 2**：批归一化（BN）放在网络的什么位置？它解决了什么问题？

> **参考答案**：BN 通常放在「卷积/线性层之后、激活函数之前」。它解决的是训练深网络时各层激活尺度越跑越离谱导致的数值不稳定（梯度消失/爆炸），把信号拉回合理区间，从而让训练更稳、更快，最终精度往往也更高。

**练习 3**：用一句话解释「对抗狗」实验为什么能成功——它暴露了深度网络的什么弱点？

> **参考答案**：因为我们固定网络权重、只对输入像素做梯度下降，总能找到一组人眼几乎看不出差异、却能让网络输出「猫」的扰动；这说明网络的判断高度依赖像素级的纹理统计特征，而非人类理解的语义，因而对精心构造的扰动非常脆弱。

---

## 5. 综合实践

**任务**：在 [Oxford-IIIT Pets](https://www.robots.ox.ac.uk/~vgg/data/pets/) 数据集（35 类猫狗品种）上完成一次迁移学习，并对比「冻结」与「微调」两种策略。

这是本课的正式作业，对应 [lab/README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/08-TransferLearning/lab/README.md) 与起始笔记 `lab/OxfordPets.ipynb`。

**建议步骤**：

1. **下载数据**（lab README 给出的片段）：

   ```bash
   wget https://www.robots.ox.ac.uk/~vgg/data/pets/data/images.tar.gz
   tar xfz images.tar.gz
   rm images.tar.gz
   ```

2. **搭流水线**：复用 4.2 节的 `std_normalize` + `Resize(256)` + `CenterCrop(224)`，用 `torchvision.datasets.ImageFolder` 加载；注意 Oxford Pets 的类别写在**文件名前缀**里（如 `samoyed_10.jpg`、`Persian_1.jpg`），你可能需要按文件名而非子目录组织类别——若用 `ImageFolder`，先把图片按品种名移进各自子目录。

3. **策略 A · 冻结**：加载 `torchvision.models.resnet18(pretrained=True)`（或 VGG-16），把最后的 `fc`（ResNet）或 `classifier`（VGG）换成输出 35 类的线性层，冻结卷积部分，训练若干 epoch，记录验证准确率。

   ```python
   # 示例代码：ResNet-18 换头 + 冻结
   model = torchvision.models.resnet18(pretrained=True)
   for p in model.parameters():
       p.requires_grad = False
   model.fc = nn.Linear(model.fc.in_features, 35)   # 35 类品种
   ```

4. **策略 B · 微调**：解冻 `model.parameters()`，用更小的学习率（如 `lr=1e-4`）再训几个 epoch，记录验证准确率。

5. **对比**：在一份表格里记录两种策略的「最终验证准确率」「训练耗时」「可训练参数量」，并写一段 100 字以内的结论：对于 35 类品种这种**比猫狗二分类更细**的任务，微调相比冻结带来了多少提升？是否值得多花的训练时间？

**预期结果**：冻结应已能达到不错的准确率（ImageNet 本就含许多猫狗品种），微调通常能再提升几个百分点；具体数值**待本地验证**。

> 提醒：35 类比 2 类难得多，注意观察是否出现过拟合（训练准确率远高于验证准确率）。若出现过拟合，可结合本讲学的 Dropout 与数据增强来缓解。

## 6. 本讲小结

- **迁移学习**复用 ImageNet 预训练模型的卷积特征，让你用很少的数据和算力就能搭出一个不错的图像分类器；核心是「换分类头」。
- 两种策略递进：**冻结/特征提取**只训分类头（可训练参数从 1.38 亿降到约 5 万），**微调**在分类头稳定后解冻卷积、用更小学习率继续训练。务必「先冻结、再解冻」，否则会冲坏预训练权重。
- **输入预处理必须与预训练时一致**：用 ImageNet 的 `mean/std` 归一化，这条铁律封装在 `pytorchcv.py` 的 `common_transform()` 里。
- **数据增强**通过随机变换造更多样本、抑制过拟合；**学习率调度**（如指数衰减 \(\eta\leftarrow0.98\eta\)）让训练「先快后精」；优化器默认用 **Adam**（「不确定就用 Adam」）。
- **Dropout** 随机失活神经元做正则（0.2~0.5 最佳，太大反而差）；**批归一化**把激活拉回合理尺度、稳定深网络训练（ResNet 每个块都内嵌 BN）。
- **对抗样本**揭示网络依赖像素级纹理而非语义，对精心构造的扰动很脆弱——这也为后续 AI 伦理讨论埋下伏笔。

## 7. 下一步学习建议

- **横向对照**：打开同目录的 `TransferLearningTF.ipynb`，看看 TensorFlow/Keras 版的迁移学习（`keras.applications`）与 PyTorch 版在 API 上的异同，巩固「换头 + 冻结 + 微调」这套通用范式。
- **深入训练技巧**：通读 [TrainingTricks.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/4-ComputerVision/08-TransferLearning/TrainingTricks.md) 中本讲没展开的部分——Xavier 初始化、梯度裁剪、LayerNorm 与 BatchNorm 的区别，并尝试在 OxfordPets 实验里启用其中一两项。
- **继续课程主线**：下一课进入 **自编码器与 VAE**（u3-l4），把视角从「分类」转向「生成与潜在空间」；迁移学习里学到的「特征提取器」概念会在那里再次出现（编码器本质上就是一个特征提取器）。
