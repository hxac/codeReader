# 项目全景：这是一个什么样的模型库

## 1. 本讲目标

学完本讲，你应该能够：

1. 用一句话说清 `rasbt/deeplearning-models` 这个仓库是什么、适合谁、怎么用。
2. 说出仓库收录的主要模型类别（线性模型、MLP、CNN、RNN、Transformer、自编码器、GAN、GNN 等）。
3. 会读 README 中的索引表格，根据「模型名 + 框架徽章」快速定位到对应 notebook 的文件路径。
4. 理解 `pytorch_ipynb` / `pytorch-lightning_ipynb` / `tensorflow1_ipynb` 三个目录各自的分工与规模。
5. 掌握 notebook 的命名规律，看到一个文件名就能猜出「什么架构 + 什么变体 + 什么数据集」。

本讲不写模型代码，只解决一个问题：**在这个 180 多个 notebook 的大仓库里如何不迷路**。这是阅读后续所有讲义的地基。

## 2. 前置知识

本讲几乎不需要深度学习基础，但下面几个名词最好先有个直观印象：

- **深度学习模型（model）**：可以理解为一个带很多可调参数的函数，比如「输入一张手写数字图片，输出它是 0~9 中的哪一个」。CNN、RNN、Transformer 等只是结构不同的模型。
- **Jupyter Notebook**：一种把「说明文字 + 代码 + 运行结果」混排在同一个文件里的文档，扩展名 `.ipynb`，可以逐格（cell）运行代码。本仓库所有模型都以 notebook 为载体，每个 notebook 都是自包含的：从导库、加载数据、定义模型到训练、评估一气呵成。
- **模型动物园（model zoo）**：业界习惯把「收录许多现成模型实现供人查阅」的地方叫 model zoo。这个仓库就是一个 notebook 版的模型动物园——它不是一个 `pip install` 的软件包，没有 `setup.py`，也没有统一的命令行入口。
- **三大框架**：
  - **PyTorch**：目前学界最主流的深度学习框架，代码命令式、像普通 Python。
  - **PyTorch Lightning**：PyTorch 之上的一层封装，把训练循环、日志等样板代码接管掉，让你只写模型与训练步骤。
  - **TensorFlow 1.x**：Google 的老一代框架，「先建计算图、再开会话执行」的范式，如今已基本被 TensorFlow 2 取代，仓库保留它主要作历史对照。
- **徽章（badge）**：README 表格里那些彩色小图标（红色 `Py-Torch`、紫色 `PyTorch-Lightning`、橙色 `Tensor-Flow1.0`）。每个徽章本身是超链接，点击即跳到对应框架版本的 notebook。

## 3. 本讲源码地图

| 路径 | 作用 |
| --- | --- |
| `README.md` | 仓库唯一的「总目录」：按模型类别分节的索引表格，每行链接到具体 notebook。本讲的主线阅读材料。 |
| `pytorch_ipynb/` | PyTorch 版 notebook 主库（121 个 notebook），以及 6 个共享 `helper_*.py` 模块，是整套学习手册的主要对象。 |
| `pytorch-lightning_ipynb/` | 同批模型的 PyTorch Lightning 版重写（39 个 notebook），子目录结构与 `pytorch_ipynb` 对应。 |
| `tensorflow1_ipynb/` | TensorFlow 1.x 版本（22 个 notebook），覆盖基础模型，作框架对照。 |
| `templates/pytorch_lightning/` | 官方提供的 Lightning 分类模板 notebook 与调参脚本，用于快速起一个新实验。 |

本讲的「源码精读」集中在 `README.md`——对这个仓库来说，README 就是它的公共接口和目录索引，学会读它等于拿到了整个动物园的地图。

## 4. 核心概念与源码讲解

### 4.1 仓库定位与 README 索引

#### 4.1.1 概念说明

这个仓库的作者是 Sebastian Raschka（《Python Machine Learning》作者、Lightning AI 的开源负责人之一）。仓库定位写在第一段：

> A collection of various deep learning architectures, models, and tips for TensorFlow and PyTorch in Jupyter Notebooks.

翻译过来就是：**一个用 Jupyter Notebook 收录各种深度学习架构、模型和技巧的合集**。它不是库、不是框架，而是一批「教学级参考实现」：

- 每个模型一个独立 notebook，不依赖仓库里的其他 notebook；
- 代码风格统一、注释详尽，刻意保留可读性而不是追求工程封装；
- 同一模型常有 PyTorch / Lightning / TF1 多个版本，方便对照学习。

README 的结构是一张「模型类别 → notebook 链接」的巨型索引表，全部内容按二级标题分节，例如 `Traditional Machine Learning`、`Multilayer Perceptrons`、`Convolutional Neural Networks for Computer Vision`、`Autoencoders`、`Generative Adversarial Networks (GANs)` 等。

#### 4.1.2 核心流程

用 README 找到你想学的模型的路径，只需要三步：

1. **按类别找节**：在 README 里按二级标题（`##`）找到模型大类，例如 CNN、RNN、GAN；大类下还有三级小节（`####`），例如 CNN 下再分 `AlexNet`、`VGG`、`ResNet` 等家族。
2. **按行找模型**：每行是一条模型记录，四列依次是 `Title | Dataset | Description | Notebooks`。
3. **按徽章选框架**：`Notebooks` 列里的彩色徽章即链接——红=PyTorch、紫=Lightning、橙=TF1。点徽章直达对应目录下的 notebook 文件。

需要注意：很多行的 `Description` 一栏写的是 `TBD`（to be determined，待补充），这是仓库尚未填完的占位，不代表功能缺失；真正的说明在 notebook 内部的 Markdown 单元格里。

#### 4.1.3 源码精读

**① 仓库的定位声明**（README 开头，一句话定义整个项目）：

[README.md:L2-L4](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L2-L4)

这里给出仓库标题 `# Deep Learning Models` 和唯一一句描述。注意措辞是 "in Jupyter Notebooks"——载体是 notebook，这决定了整个学习方式：**逐格运行、逐格阅读**，而不是调用一个包。

**② 索引表格的基本形态**（以最简单的 Traditional Machine Learning 一节为例）：

[README.md:L8-L15](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L8-L15)

这 8 行展示了所有表格的共同格式：表头四列 `Title | Dataset | Description | Notebooks`；第一行 Perceptron 的徽章链接分别是 `pytorch_ipynb/basic-ml/perceptron.ipynb`（红）和 `tensorflow1_ipynb/basic-ml/perceptron.ipynb`（橙）——同一个模型在两个框架目录下各有一份同名实现。

**③ 大类下的家族小节**（CNN 一节及其 ResNet 小节）：

[README.md:L29-L36](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L29-L36)

`## Convolutional Neural Networks for Computer Vision` 是大类；紧接着 `#### Basic` 是小节。注意 L36 这一行 `Convolutional Neural Network` 挂了**三个徽章**——同一个 `cnn-basic.ipynb` 文件名在三个框架目录里各存一份，这是仓库「同模型多框架对照」的最典型样本。

[README.md:L118-L132](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L118-L132)

ResNet 小节展示了另一种常见模式：**同一架构 × 多种数据集/任务**。从 L122 的残差块教学示例（`resnet-ex-1.ipynb`），到 ResNet-18/34/50/101/152 分别在 MNIST（数字分类）、CelebA（属性分类）、CIFAR-10（物体分类）上的实现，共 11 行。`Dataset` 列还顺带给出了 MNIST、CelebA、CIFAR-10、QuickDraw 的官方外链。

**④ 生成模型与序列模型两大板块**：

[README.md:L189-L196](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L189-L196)

Autoencoders 一节按「全连接 → 卷积 → 变分（VAE）→ 条件变分（CVAE）」四个小节递进组织，L195 的 `ae-basic.ipynb` 是后面生成模型单元（U5）的起点。

[README.md:L237-L247](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L237-L247)

GANs 一节从全连接 GAN（L241）一路排到 Wasserstein GAN、卷积 GAN、label smoothing 变体、DCGAN（L247），构成一条由浅入深的生成对抗网络学习线。

[README.md:L260-L284](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L260-L284)

RNNs 一节先按任务形态分组——`Many-to-one: Sentiment Analysis / Classification`（L265）与 `Many-to-Many / Sequence-to-Sequence`（L280），再在组内按单元类型（simple RNN → LSTM → GRU → 双向多层）排列。这种「任务形态优先」的分节方式本身就是理解 RNN 的一个好框架。

**⑤ 「机制与技巧」类内容**（不只是模型）：

[README.md:L335-L343](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L335-L343)

`## PyTorch Workflows and Mechanics` 一节收录的不是新架构，而是**工程机制**：自定义数据加载、验证集划分、梯度检查点、多 GPU、确定性设置、autograd 细节等。这些 notebook 是理解「PyTorch 怎么干活」的钥匙，本手册的 U4、U8 单元会大量用到。

[README.md:L409-L419](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L409-L419)

对应的 `## TensorFlow Workflows and Mechanics` 收录 TF1 时代的数据管线机制（TFRecords、队列、Dataset API），与 PyTorch 机制一节互为镜像。

#### 4.1.4 代码实践

**实践目标**：不靠肉眼逐行扫 README，而是用命令行把索引表里的所有 notebook 链接抽出来，做成一份可检索的清单。

**操作步骤**：

1. 进入仓库根目录，运行下面第一条命令，从 README 中提取所有指向三个框架目录的 `.ipynb` 链接：

   ```bash
   grep -oE '\((pytorch_ipynb|pytorch-lightning_ipynb|tensorflow1_ipynb)/[^)]+\.ipynb\)' README.md | tr -d '()' | sort
   ```

2. 想按框架统计各有多少条链接，再运行：

   ```bash
   grep -oE '\((pytorch_ipynb|pytorch-lightning_ipynb|tensorflow1_ipynb)/[^)]+\.ipynb\)' README.md | tr -d '()' | cut -d/ -f1 | sort | uniq -c
   ```

3. 把第一条命令的输出重定向到文件（如 `notebook-index.txt`），方便后续用 `grep` 随时查：想找 ResNet 相关的就 `grep resnet notebook-index.txt`。

**需要观察的现象**：

- 提取出的路径全部以三个框架目录之一开头，与徽章颜色一一对应；
- 同一个文件名（如 `mlp/mlp-basic.ipynb`）会在不同框架目录下重复出现；
- Data Augmentation 一节（[README.md:L299-L303](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L299-L303)）的徽章链接指向的是一个**目录**而非单个 `.ipynb`，所以不会出现在上面的提取结果里——这是唯一的例外，值得留意。

**预期结果**：得到一份一百多行的清单，按目录排序；`uniq -c` 统计出的三个目录链接数与目录的实际规模成正比（PyTorch 最多，Lightning 次之，TF1 最少）。

（注：链接数与目录内实际文件数不完全相等——个别 notebook 未列入 README，个别行链接目录而非文件。）

#### 4.1.5 小练习与答案

**练习 1**：你想学习「在 CIFAR-10 上用 AlexNet 做分类」，请说出对应的 PyTorch notebook 路径，以及它有没有 Lightning 版本。

**答案**：查 README 的 AlexNet 小节 [README.md:L52-L57](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L52-L57)。PyTorch 版路径是 `pytorch_ipynb/cnn/cnn-alexnet-cifar10.ipynb`；有 Lightning 版，路径为 `pytorch-lightning_ipynb/cnn/cnn-alexnet-cifar10.ipynb`（同小节 L56 一行挂了紫、红两个徽章）。

**练习 2**：README 表格中大量出现的 `TBD` 是什么意思？它影响你使用仓库吗？

**答案**：`TBD` = to be determined，表示作者还没填这一格。它只影响 README 的描述完整性，不影响 notebook 本身——每个 notebook 内部有自己的 Markdown 说明文字，照常可读可运行。

**练习 3**：仓库里「PyTorch 机制（Workflows and Mechanics）」类内容和「模型架构」类内容在 README 里是怎么区分的？

**答案**：模型架构类按架构名分节（CNN、RNN、GAN……），机制类集中在 `## PyTorch Workflows and Mechanics`（[README.md:L335-L406](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L335-L406)）和 `## TensorFlow Workflows and Mechanics`（[README.md:L409-L428](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L409-L428)），其下再按 Custom Datasets、Training and Preprocessing、Autograd 等主题分小节。

### 4.2 三大框架目录分工

#### 4.2.1 概念说明

仓库根目录下与学习直接相关的是三个框架目录加一个模板目录：

| 目录 | 框架 | notebook 数（HEAD `59beda3` 实测） | 角色 |
| --- | --- | --- | --- |
| `pytorch_ipynb/` | PyTorch | 121 | **主库**。覆盖全部模型类别，且独有 6 个共享 `helper_*.py` 模块（数据加载、训练循环、评估、绘图、工具）。 |
| `pytorch-lightning_ipynb/` | PyTorch Lightning | 39 | 主库中一部分模型的 Lightning 重写版，子目录（`cnn/`、`mlp/`、`kfold/`、`ordinal/`、`transformer/`、`data-augmentation/`）与主库同名对应。 |
| `tensorflow1_ipynb/` | TensorFlow 1.x | 22 | 基础模型（感知机、MLP、CNN、自编码器、GAN 等）的 TF1 版，属于较早期的代码。 |
| `templates/pytorch_lightning/` | Lightning 模板 | — | 官方实验模板：`template_classification_basic.ipynb` 与调参脚本 `tune_classification_basic.py`。 |

三者的分工可以概括为：**`pytorch_ipynb` 是「完整版教科书」，`pytorch-lightning_ipynb` 是「工程化示范」，`tensorflow1_ipynb` 是「历史对照组」**。

另一个重要差别在共享基础设施：只有 `pytorch_ipynb/` 根目录下放着 `helper_data.py`、`helper_train.py`、`helper_evaluate.py`、`helper_plotting.py`、`helper_utils.py` 和 `helper.py` 六个共享模块，新版 PyTorch notebook 靠 `import` 它们来复用数据加载与训练循环；`pytorch-lightning_ipynb/` 只有 `helper_utilities.py`；`tensorflow1_ipynb/` 只有一个旧版 `helper.py`。这批 helper 是本手册 U1-L3 的主角，也是理解仓库代码组织方式的关键。

#### 4.2.2 核心流程

三个目录的对应关系是「按子目录名 + 按文件名」双重对齐：

```
pytorch_ipynb/mlp/mlp-basic.ipynb          ← 主库版本（手写训练循环）
pytorch-lightning_ipynb/mlp/mlp-basic.ipynb ← Lightning 版（LightningModule + Trainer）
tensorflow1_ipynb/mlp/mlp-basic.ipynb       ← TF1 版（graph + session）
```

同一行 README 记录会同时挂多个徽章，就是这种一一对应关系的体现。所以阅读策略可以是：

1. 先在 `pytorch_ipynb` 里读懂某个模型的「裸写法」；
2. 再打开 `pytorch-lightning_ipynb` 的同名文件，看同样的模型如何被 Lightning 的抽象接管；
3. 有兴趣时再对照 TF1 版，体会两代框架在设计哲学上的差异（这一步是选修）。

#### 4.2.3 源码精读

**① 同一模型三框架并排的索引行**：

[README.md:L18-L25](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L18-L25)

MLP 一节的 L22 行：`Multilayer Perceptron` 一行同时给出 `pytorch-lightning_ipynb/mlp/mlp-basic.ipynb`（紫）、`pytorch_ipynb/mlp/mlp-basic.ipynb`（红）、`tensorflow1_ipynb/mlp/mlp-basic.ipynb`（橙）三个链接——三个目录的「同名对齐」在这里一目了然。对比 L25 的 `mlp-fromscratch__sigmoid-mse.ipynb` 只有红、橙两个徽章，说明该模型没有 Lightning 版：**徽章数量本身就告诉你某模型的框架覆盖情况**。

**② Lightning 独有内容**：

[README.md:L299-L303](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L299-L303)

`## Data Augmentation` 一节只挂了 Lightning 徽章，链接指向 `pytorch-lightning_ipynb/data-augmentation/autoaugment` 目录——那里是一组三个对照 notebook（无增强 / AutoAugment / TrivialAugment）。这是「Lightning 目录承载新式实验」的典型例子。

[README.md:L290-L295](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L290-L295)

Model Evaluation 一节：L294 基线 CNN 同时有 PyTorch 与 Lightning 版；L295 用 `pl_cross` 库做 5 折交叉验证的实现**只有 Lightning 版**——因为 `pl_cross` 本身就是 Lightning 生态的库。这再次印证：Lightning 目录偏重「工程化/生态集成」。

**③ TF1 目录的内容边界**：

[README.md:L187-L187](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L187-L187)

`## Metric Learning` 一节只有橙色徽章（`tensorflow1_ipynb/metric/siamese-1.ipynb`）——孪生网络是 TF1 目录独占的内容之一；反过来 `pytorch_ipynb` 独有的 `gnn/`、`viz/`、`tricks/` 等目录在 TF1 侧没有对应。两个目录是「部分重叠」而非「完全镜像」。

**④ 模板目录**：

README 没有为 `templates/` 建索引，它是给想批量做实验的人用的：`templates/pytorch_lightning/template_classification_basic.ipynb` 是一个分类任务模板 notebook，`tune_classification_basic.py` 是配套的调参脚本，`submit_command.sh` 用于把任务提交到计算节点。本手册 U8-L2 会专门讲它。

#### 4.2.4 代码实践

**实践目标**：用文件系统命令亲自验证三个目录的规模差异与同名对齐关系。

**操作步骤**：

1. 统计每个目录的 notebook 数量：

   ```bash
   find pytorch_ipynb -name '*.ipynb' | wc -l
   find pytorch-lightning_ipynb -name '*.ipynb' | wc -l
   find tensorflow1_ipynb -name '*.ipynb' | wc -l
   ```

2. 列出 `pytorch_ipynb/` 根目录下的共享 Python 模块：

   ```bash
   ls pytorch_ipynb/*.py
   ```

3. 验证同名 notebook 在三个框架目录中的对齐：

   ```bash
   ls pytorch_ipynb/mlp/mlp-basic.ipynb pytorch-lightning_ipynb/mlp/mlp-basic.ipynb tensorflow1_ipynb/mlp/mlp-basic.ipynb
   ```

**需要观察的现象**：

- 三个 `wc -l` 数字分别是 121、39、22（若上游仓库更新，数字可能变化，但「主库远大于另外两个」的关系不变）；
- `ls pytorch_ipynb/*.py` 会列出 6 个文件：`helper.py`、`helper_data.py`、`helper_evaluate.py`、`helper_plotting.py`、`helper_train.py`、`helper_utils.py`；而 Lightning 目录根下没有这一整套；
- 第 3 步中三个路径同时存在，说明「同名对齐」不是巧合而是仓库的组织约定。

**预期结果**：三条 `ls` 均成功返回文件路径，无 "No such file" 报错。若你尝试把路径换成 `pytorch-lightning_ipynb/mlp/mlp-fromscratch__sigmoid-mse.ipynb`，则会报不存在——因为该模型没有 Lightning 版（对照 4.2.3 ① 的徽章数量）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `pytorch_ipynb` 比 `pytorch-lightning_ipynb` 大得多？Lightning 目录的收录标准像是什么？

**答案**：`pytorch_ipynb` 是完整的主库，覆盖所有类别；`pytorch-lightning_ipynb` 只重写了其中一部分常用模型（cnn、mlp、kfold、ordinal、transformer、data-augmentation 六类）。收录标准更像「挑选有代表性的模型做 Lightning 工程化示范」，而不是追求与主库一一对应。

**练习 2**：如果你只想学 PyTorch 本身（不碰 Lightning 和 TF1），可以完全无视另外两个目录吗？有什么损失？

**答案**：可以，`pytorch_ipynb` 自包含且最完整。损失两点：一是 Lightning 版是「同一模型的另一种组织方式」，对照阅读能加深对训练循环本质的理解；二是少数内容（如 `pl_cross` 5 折交叉验证、AutoAugment 对照实验、TF1 独占的 Siamese 网络）只存在于另外两个目录。

**练习 3**：`templates/pytorch_lightning/` 下的模板和 `pytorch-lightning_ipynb/` 下的 notebook 有什么区别？

**答案**：`pytorch-lightning_ipynb/` 是教学展示用的成品示例；`templates/` 是「填空式」起点——`template_classification_basic.ipynb` 把模型、数据配置留成可替换的位置，`tune_classification_basic.py` 支持从命令行批量调参启动实验，面向的是复用与批跑而非阅读。

### 4.3 notebook 命名规律

#### 4.3.1 概念说明

仓库里的 notebook 文件名高度规律化，绝大多数可以拆成几段来读：

```
cnn - resnet18 - mnist .ipynb
 │       │         │
 │       │         └─ 数据集 / 任务
 │       └─ 架构（含版本号 / 变体）
 └─ 模型大类缩写
```

常见模型大类前缀：

| 前缀 | 含义 | 目录 |
| --- | --- | --- |
| （无前缀）`perceptron`、`logistic-regression` | 传统机器学习 | `basic-ml/` |
| `mlp-` | 多层感知机 | `mlp/` |
| `cnn-` | 卷积网络（含具体架构名） | `cnn/` |
| `ae-` | 自编码器家族 | `autoencoder/` |
| `gan-` / `wgan-` / `dcgan-` | 生成对抗网络家族 | `gan/` |
| `rnn_` / `char_rnn-` | 循环网络 | `rnn/` |
| `gnn-` | 图神经网络 | `gnn/` |

两类特殊记号：

- **双下划线 `__`**：把「实现要点」写进文件名，如 `mlp-fromscratch__sigmoid-mse.ipynb`（从零实现、sigmoid 激活、MSE 损失）、`cnn-viz-grad__vgg16-cats-dogs.ipynb`（可视化、对输入求梯度、VGG16、猫狗数据）。看到 `__` 就知道这个 notebook 是某种特定技术组合的示范。
- **连字符 vs 下划线风格混用**：`cnn/`、`mlp/`、`autoencoder/` 等目录用连字符（`cnn-resnet18-mnist`），而 `rnn/`、`ordinal/`、`cnn-nlp/`、`transformer/` 用下划线（`rnn_simple_imdb`、`CORAL_cement`）。这只是历史习惯，不影响含义，但查文件时两种都要试。

#### 4.3.2 核心流程

拿到一个文件名后，按下面的顺序解码：

1. **切前缀** → 得到模型大类，确定所在目录；
2. **找架构名** → 中间一段通常是架构 + 版本号（`resnet18`、`densenet121`、`mobilenet-v3-small`）；基础款直接叫 `basic`（`mlp-basic`、`cnn-basic`、`ae-basic`）；
3. **看结尾** → 最后一段（若存在）是数据集（`mnist`、`cifar10`、`celeba`、`quickdraw`、`imdb`、`cement`）；没有数据集段通常默认 MNIST；
4. **认变体词** → `-he-init`（He 初始化）、`-batchnorm`、`-dropout`、`-grouped`（分组卷积）、`-dataparallel`（多 GPU）、`-smoothing`（label smoothing）、`-packed`（packed sequences）等，标明该 notebook 的教学重点；
5. **跨目录对照** → 去掉目录前缀后文件名相同的三份 notebook，就是同一模型的三框架版本。

#### 4.3.3 源码精读

**① 基础款命名：`{大类}-basic`**

README 中 CNN Basic 小节：

[README.md:L32-L37](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L32-L37)

L36 的 `cnn-basic.ipynb` 与 L37 的 `cnn-he-init.ipynb` 形成对照：前者是「裸 CNN」，后者仅多一个 `-he-init` 变体词，表示在同样结构上重点演示 He 参数初始化。文件名差异 = 教学内容差异。

**② 架构 × 数据集命名**（LeNet 小节）：

[README.md:L76-L82](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L76-L82)

同一 LeNet-5 架构依次有 `cnn-lenet5-mnist`（L80）、`cnn-lenet5-cifar10`（L81）、`cnn-lenet5-quickdraw`（L82）三种结尾——架构段不变，只换数据集段。反过来在 MobileNet 小节 [README.md:L87-L94](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L87-L94)，数据集固定为 CIFAR-10，架构段在 `mobilenet-v2` / `mobilenet-v3-small` / `mobilenet-v3-large` 之间变化。README 的表格和文件名共享同一套「可替换维度」。

**③ 双下划线命名**：

[README.md:L25-L25](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L25-L25)

`mlp-fromscratch__sigmoid-mse.ipynb`：`fromscratch`（不用 autograd、手工实现反向传播）+ `sigmoid`（激活函数）+ `mse`（损失函数），一个文件名交代了实现路线的全部关键选择。

**④ 下划线风格命名**（RNN / 有序回归）：

[README.md:L269-L276](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L269-L276)

`rnn_simple_imdb` → simple RNN + IMDb；`rnn_simple_packed_imdb` → 加了 packed sequences；`rnn_lstm_packed_imdb-glove` → LSTM + packed + GloVe 预训练词向量。变体词按需追加，命名像「特性叠加」。

[README.md:L161-L168](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L161-L168)

有序回归一节是 `{方法}_{数据集}` 命名：`baseline_cement`、`CORAL_cement`、`CORN_cement`、`niu2016_cement`……方法名直接取自论文（CORAL/CORN 是两个有序回归方法，niu2016/beckham2016/polat2022 是论文作者+年份）。看到「作者+年份」式命名，就要意识到这个 notebook 是某篇论文的参考实现。

#### 4.3.4 代码实践

**实践目标**：用文件系统验证命名规律，做到「看名识模型」。

**操作步骤**：

1. 把架构名当检索词，看同一架构覆盖了哪些数据集：

   ```bash
   find pytorch_ipynb -name '*resnet18*'
   find pytorch_ipynb -name '*lenet5*'
   ```

2. 把数据集当检索词，看有多少模型实现用了它：

   ```bash
   find pytorch_ipynb -name '*mnist*' | wc -l
   find pytorch_ipynb -name '*cifar10*' | wc -l
   ```

3. 检索双下划线命名的特殊实现：

   ```bash
   find pytorch_ipynb -name '*__*'
   ```

4. 人肉解码练习：对下面 4 个真实文件名写出「大类 / 架构 / 变体 / 数据集」四段拆解（答案见小练习）：

   ```
   cnn-mobilenet-v3-small-cifar10.ipynb
   ae-conv-var.ipynb
   gan-conv-smoothing.ipynb
   rnn_lstm_bi_imdb.ipynb
   ```

**需要观察的现象**：

- 第 1 步里 `resnet18` 命中的文件包括 `cnn-resnet18-mnist.ipynb` 与 `cnn-resnet18-celeba-dataparallel.ipynb` 等，结尾的数据集/任务段各不相同；
- 第 2 步两个数字都不小，说明 MNIST / CIFAR-10 是仓库的「默认练兵场」；
- 第 3 步命中的是少数几个「技术组合示范」型 notebook。

**预期结果**：所有命令都能返回非空结果；第 4 步你能独立完成拆解并与答案核对。具体输出与仓库版本有关，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：拆解 `cnn-mobilenet-v3-small-cifar10.ipynb`。

**答案**：大类 `cnn`（卷积网络，位于 `cnn/`）；架构 `mobilenet-v3-small`（MobileNet 第三代的 small 规格版）；无额外变体词；数据集 `cifar10`。

**练习 2**：拆解 `ae-conv-var.ipynb` 和 `gan-conv-smoothing.ipynb`。

**答案**：`ae-conv-var`：自编码器（`autoencoder/` 目录）+ 卷积版 + 变分（variational），未写数据集说明默认 MNIST，即「卷积变分自编码器」。`gan-conv-smoothing`：GAN（`gan/` 目录）+ 卷积判别器/生成器 + label smoothing 技巧变体，同样是 MNIST。

**练习 3**：`rnn_lstm_bi_imdb.ipynb` 每一段是什么意思？它和 `rnn_simple_imdb.ipynb`、`rnn_lstm_packed_imdb.ipynb` 是什么关系？

**答案**：`rnn` 大类（`rnn/` 目录）+ LSTM 单元 + bi（双向）+ IMDb 数据集。三者是同一任务（IMDb 情感分类）下的特性递进：simple（最朴素 RNN）→ lstm+packed（更强单元且能跳过 padding）→ bi（双向读取序列），对应 README [README.md:L269-L276](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L269-L276) 里自上而下的排列顺序，也是本手册 U6 单元的教学顺序。

## 5. 综合实践

**任务：制作你的专属「模型动物园速查表」。**

1. **准备**：把仓库 clone 到本地（或在已有 clone 中），确认 `git log -1` 能看到当前 HEAD。

2. **提取索引**：运行 4.1.4 中的 `grep` 命令，把 README 里全部 notebook 链接导出为 `notebook-index.txt`。

3. **整理速查表**：通读 README 的各节标题，为你感兴趣的类别各挑 1~2 个代表模型，整理成一张 Markdown 表（示例代码，格式可自定）：

   ```markdown
   | 模型类别 | 框架目录 | notebook 文件名 | 我想学的原因 |
   | --- | --- | --- | --- |
   | CNN/ResNet | pytorch_ipynb | cnn/cnn-resnet18-mnist.ipynb | 学残差连接 |
   | 生成模型/GAN | pytorch_ipynb | gan/gan.ipynb | 学最简单的 GAN |
   | ... | ... | ... | ... |
   ```

4. **锁定 5 个目标**：从速查表中标出**你最想学会的 5 个模型**及其完整文件路径，并给每个写一句「学会它的判据」（例如：能不看 notebook 自己重写训练循环）。

5. **交叉验证**：对你选出的 5 个路径逐个执行 `ls <路径>`，确认文件真实存在；再对照 README 相应行，记下每个模型有哪些框架版本（几个徽章）。

**验收标准**：速查表至少覆盖 5 个不同模型类别；5 个目标路径全部真实存在；你能口头复述每个文件名各段的含义。

这张表就是你后续的学习合同——本手册的单元设计（U2 基础模型 → U3 CNN → U5 生成模型 → U6 序列模型 → U7/U8 进阶）可以和你选的 5 个目标对照排期。

## 6. 本讲小结

- `rasbt/deeplearning-models` 是一个 **notebook 版模型动物园**：121（PyTorch）+ 39（Lightning）+ 22（TF1）个自包含 Jupyter Notebook，不是可安装的软件包。
- **README 是总目录**：按模型类别分节、每行 `Title | Dataset | Description | Notebooks` 四列，徽章即链接（红=PyTorch、紫=Lightning、橙=TF1），大量 `TBD` 只是占位。
- **三个框架目录分工明确**：`pytorch_ipynb` 是完整主库且独享 6 个 `helper_*.py` 共享模块；`pytorch-lightning_ipynb` 是工程化示范；`tensorflow1_ipynb` 是历史对照；`templates/` 提供批量实验模板。
- **同名对齐**是仓库的组织约定：同一模型在多个框架目录下使用相同文件名，README 一行挂几个徽章就有几个版本。
- **文件名即摘要**：`{大类}-{架构/变体}-{数据集}` 三段式 + `__` 技术组合记号 + 连字符/下划线两种历史风格，学会解码就能「看名识模型」。
- 仓库除模型外还收录大量**机制类内容**（数据加载、验证集划分、梯度检查点、多 GPU、确定性、autograd 细节），集中在 Mechanics 两节，是进阶单元的素材。

## 7. 下一步学习建议

下一讲（U1-L2《环境搭建与第一个 Notebook》）将动手搭建 Python 环境，完整运行 `pytorch_ipynb/basic-ml/perceptron.ipynb`，并认识几乎所有 notebook 共有的「Imports → Settings/Dataset → Model → Training → Evaluation」五段式结构。

在进入下一讲之前，建议你：

1. 把本讲的速查表放在手边，后续每学一个模型就在表上打钩；
2. 在 GitHub 上浏览一次 [`pytorch_ipynb/` 根目录](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_data.py)下的 6 个 `helper_*.py` 文件名（可从 [helper_data.py](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_data.py) 入手），混个眼熟即可（U1-L3 会精读）；
3. 如果对「模型 zoo」这个概念还好奇，可以对照看 torchvision 的模型文档，体会「教学实现」与「生产实现」的差别——本仓库的笔记本保留了完整的推导与注释，这正是它适合学习的原因。
