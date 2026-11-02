# 项目全景：这是一个什么样的模型库

## 1. 本讲目标

学完本讲，你应该能够：

1. 用一句话说清 `rasbt/deeplearning-models` 这个仓库是什么、适合谁、怎么用。
2. 说出仓库收录的主要模型类别（线性模型、MLP、CNN、RNN、Transformer、自编码器、GAN、GNN 等）。
3. 会读 README 中的索引表格，根据「模型名 + 框架徽章」快速定位到对应的 notebook 文件路径。
4. 理解 `pytorch_ipynb` / `pytorch-lightning_ipynb` / `tensorflow1_ipynb` 三个目录各自的分工与规模。
5. 掌握 notebook 的命名规律，看到一个文件名就能猜出「什么架构 + 什么数据集 + 什么变体」。

本讲不写模型代码，只解决一个问题：**在这个 180+ 个 notebook 的大仓库里，如何不迷路。**这是阅读后续所有讲义的地基。

## 2. 前置知识

本讲几乎不需要深度学习基础，但下面几个名词最好先有个直观印象：

- **深度学习模型（model）**：可以理解为一个带很多可调参数的函数，比如「输入一张手写数字图片，输出它是 0~9 中哪一个」。CNN、RNN、Transformer 等都是不同结构的模型。
- **Jupyter Notebook**：一种把「说明文字 + 代码 + 运行结果」混排在同一个文件里的文档，扩展名为 `.ipynb`。你可以逐格（cell）运行代码，非常适合学习和做实验。本仓库的所有模型都以 notebook 为载体。
- **模型动物园（model zoo）**：业界习惯把「收录了许多现成模型实现供人查阅」的地方叫 model zoo。这个仓库就是一个 notebook 版的模型动物园。
- **三大框架**：
  - **PyTorch**：目前学界最主流的深度学习框架，代码以「命令式」著称，写起来像普通 Python。
  - **PyTorch Lightning**：PyTorch 之上的一层封装，把训练循环、日志等样板代码接管掉，让你只写模型与训练步骤。它是 PyTorch 生态的一部分，不是竞争者。
  - **TensorFlow 1.x**：Google 的老一代框架，采用「先建计算图、再开会话执行」的范式，现在已基本被 TensorFlow 2 取代，仓库保留它主要是历史对照价值。
- **徽章（badge）**：README 表格里那些彩色小图标（如红色的 `Py-Torch`、紫色的 `PyTorch-Lightning`、橙色的 `Tensor-Flow1.0`），每个徽章本身是一个超链接，点击即跳到对应框架版本的 notebook。

## 3. 本讲源码地图

| 路径 | 作用 |
| --- | --- |
| `README.md` | 全仓库唯一的总索引：按模型类别分表列出所有 notebook 及其框架徽章链接 |
| `pytorch_ipynb/` | PyTorch 版模型库，规模最大（121 个 notebook），并附带 6 个共享 `helper_*.py` 工具模块 |
| `pytorch-lightning_ipynb/` | PyTorch Lightning 版模型库（39 个 notebook），是部分 PyTorch notebook 的「换框架孪生版」 |
| `tensorflow1_ipynb/` | TensorFlow 1.x 版模型库（22 个 notebook），覆盖基础模型，用于框架对照 |
| `templates/pytorch_lightning/` | Lightning 分类任务通用模板（模板 notebook + 调参脚本） |
| `deeplearning-models-tutorial/` | 你正在阅读的学习手册讲义目录（本套讲义的产出） |

本讲精读的核心文件只有一个：`README.md`。其余目录通过实际浏览确认结构。

## 4. 核心概念与源码讲解

### 4.1 仓库定位与 README 索引

#### 4.1.1 概念说明

大多数 GitHub 项目是「软件包」——有入口文件、有 `pip install`、有 API。这个仓库不是。它是一个**教学参考库**：作者（Sebastian Raschka，`rasbt`）把各种经典深度学习架构逐一实现成一个个自包含的 Jupyter Notebook，每个 notebook 都能独立阅读、独立运行。

这种形态决定了它的使用方式：

- 没有「主程序」可运行，学习的单位是**单个 notebook**。
- notebook 之间大多互不依赖（少数共享同目录的 `helper_*.py`，将在下一讲详述）。
- README 不是普通的项目说明书，而是**全库的目录页 / 索引**——你想找任何模型，第一站都是 README。

为什么值得这样学？因为论文只描述思想，框架官方示例只展示最小用法，而这里把「同一个模型在多个框架、多个数据集、多种变体」的实现并排放好，非常适合对照阅读。

#### 4.1.2 核心流程

用 README 定位一个模型的标准流程：

```text
1. 打开 README.md
2. 按二级标题（##）浏览模型大类
   （Traditional ML / MLP / CNN / Transformers / Autoencoders / GAN / RNN / ...）
3. 进入大类后，必要时再看三级标题（### / ####）细分小类
   （如 CNN 下的 Basic / AlexNet / DenseNet / ResNet / VGG ...）
4. 在表格中按 Title 列找到目标模型
5. 看 Notebooks 列的徽章：
   - 红色 Py-Torch          → 链接到 pytorch_ipynb/ 下的 notebook
   - 紫色 PyTorch-Lightning → 链接到 pytorch-lightning_ipynb/ 下的 notebook
   - 橙色 Tensor-Flow1.0    → 链接到 tensorflow1_ipynb/ 下的 notebook
   没有某框架徽章 = 该模型没有对应框架的实现
6. 点击徽章（或在本地打开对应路径）开始阅读
```

README 表格的通用列结构：

| 列 | 含义 |
| --- | --- |
| Title | 模型名称（常含架构、数据集信息） |
| Dataset | 训练用的数据集（很多行还是占位的 TBD） |
| Description | 一句话描述（很多行还是 TBD） |
| Notebooks | 框架徽章链接，一行为同一个模型的多框架入口 |

#### 4.1.3 源码精读

**① 仓库的一句话定位**——README 开头第 2~4 行：

[README.md:L2-L4](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L2-L4)

这两行说明：这是一个用 Jupyter Notebook 收录「各种深度学习架构、模型与技巧」的集合，覆盖 TensorFlow 与 PyTorch。注意措辞是 *collection ... in Jupyter Notebooks*——它从一开始就自我定位为 notebook 合集，而非软件包。

**② 第一个索引表：Traditional Machine Learning**——README 第 8~15 行：

[README.md:L8-L15](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L8-L15)

以第 12 行的 Perceptron（感知机）行为例，一行里同时挂了两个徽章：红色 PyTorch 徽章指向 `pytorch_ipynb/basic-ml/perceptron.ipynb`，橙色 TF 徽章指向 `tensorflow1_ipynb/basic-ml/perceptron.ipynb`。**两个路径只有顶层目录不同、其余完全相同**——这不是巧合，而是全仓库刻意维持的约定（详见 4.2）。这个表也告诉你：仓库从最基础的感知机、逻辑回归、softmax 回归讲起，「从零开始」名副其实。

**③ 三徽章并排的例子：Multilayer Perceptrons 表**——README 第 18~25 行：

[README.md:L18-L25](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L18-L25)

第 22 行的 Multilayer Perceptron 一行并排挂了 **3 个徽章**：Lightning、PyTorch、TF1 三个版本同时存在，路径分别是 `pytorch-lightning_ipynb/mlp/mlp-basic.ipynb`、`pytorch_ipynb/mlp/mlp-basic.ipynb`、`tensorflow1_ipynb/mlp/mlp-basic.ipynb`。这是「同一模型、三种实现」最直观的样本，学到这里你就能明白三个目录的关系了。

**④ 模型大类全景**——README 用二级标题划分大类，逐段数过去（均可用行号区间直达）：

| 大类（## 标题） | 行号 | 内容要点 |
| --- | --- | --- |
| Traditional Machine Learning | L8 | 感知机、逻辑/softmax 回归 |
| Multilayer Perceptrons | L18 | 基础 MLP + Dropout/BatchNorm/从零反向传播变体 |
| CNN for Computer Vision | L29 | 按 LeNet/AlexNet/VGG/NiN/ResNet/DenseNet/MobileNet 等三级标题细分，是全库最大的一块 |
| CNN for NLP | L137 | Text CNN 文本情感分类 |
| Transformers | L145 | DistilBERT 特征提取与微调 |
| Ordinal Regression | L157 | CORAL/CORN 等有序回归方法 |
| Normalization Layers / Metric Learning | L174/L183 | 归一化层对比、孪生网络 |
| Autoencoders | L189 | 全连接/卷积/变分/条件变分自编码器 |
| GANs | L237 | GAN/WGAN/DCGAN 家族 |
| GNNs | L250 | 图神经网络三种入门实现 |
| RNNs | L260 | many-to-one 情感分类与 many-to-many 字符生成 |
| Model Evaluation / Data Augmentation / Tips / Transfer Learning / Visualization | L288-L331 | K 折、数据增强、训练技巧、迁移学习、可解释性 |
| PyTorch / TensorFlow Workflows and Mechanics | L335/L409 | 数据管线、autograd、多 GPU 等工程机制 |
| Related Libraries | L430 | torchmetrics 等周边库 |

其中 CNN 大类的入口在 [README.md:L29-L45](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L29-L45)，可以看到它内部又用 `#### Basic`、`#### Concepts` 等三级标题切分子类；ResNet 子表在 [README.md:L118-L132](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L118-L132)，一口气列了 ResNet-18 到 ResNet-152 在 MNIST/CelebA/CIFAR-10 上的各种组合。

**⑤ 一个提醒：README 索引偶尔滞后于实际文件。**例如 README 第 57 行把「Grouped AlexNet」链接写成 `pytorch_ipynb/cnn/cnn-alexnet-grouped-cifar10.ipynb`：

[README.md:L57](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L57)

但浏览 `pytorch_ipynb/cnn/` 目录会发现实际文件名是 `cnn-alexnet-cifar10-grouped.ipynb`（词序不同）。所以养成习惯：**徽章打不开时，回到对应目录里 `ls` 一下确认真实文件名**，README 是地图，目录才是领土。

#### 4.1.4 代码实践

**实践目标**：证明你真的会用 README 索引，而不是只会滚动页面。

**操作步骤**：

1. 在本地（或 GitHub 网页上）打开 `README.md`。
2. 只用 README 的表格，回答下面三个「寻址」问题：
   - ResNet-18 在 MNIST 上的 PyTorch 版 notebook 完整路径是什么？
   - 「DistilBERT 作为特征提取器」的 notebook 在哪个目录？
   - 哪个模型有全部三种框架（Lightning / PyTorch / TF1）的实现？
3. 用命令验证答案（示例命令，在你自己的终端运行）：

```bash
# 确认 README 指向的文件真实存在
ls pytorch_ipynb/cnn/cnn-resnet18-mnist.ipynb

# 数一数 README 里三个框架徽章各出现多少次
grep -o 'pytorch_ipynb/' README.md | wc -l
grep -o 'pytorch-lightning_ipynb/' README.md | wc -l
grep -o 'tensorflow1_ipynb/' README.md | wc -l
```

**需要观察的现象**：`ls` 能找到文件说明索引有效；三个 `grep` 计数会直观展示「PyTorch 版最多、Lightning 次之、TF1 最少」的规模梯度。

**预期结果**：三问答案分别为 `pytorch_ipynb/cnn/cnn-resnet18-mnist.ipynb`（见 [README.md:L123](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L123)）、`pytorch_ipynb/transformer/` 目录（见 [README.md:L150](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L150)）、例如 Multilayer Perceptron（见 [README.md:L22](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L22)）。grep 的具体计数值：待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：你想同时学习「基础 MLP」的 PyTorch 写法和 Lightning 写法，请仅凭 README 给出两个 notebook 的路径。
**答案**：`pytorch_ipynb/mlp/mlp-basic.ipynb` 与 `pytorch-lightning_ipynb/mlp/mlp-basic.ipynb`，二者都在 [README.md:L22](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L22) 的同一行里以两个徽章给出。

**练习 2**：README 里 Dataset / Description 两列大量写着 "TBD"，这说明什么？使用仓库时应该怎么办？
**答案**：说明这些行的文档尚未补全，README 只保证「索引到文件」这一层可靠，不保证元信息完整。想知道某 notebook 用什么数据、做什么实验，应当直接打开 notebook 本身阅读（每个 notebook 内部有完整说明），以目录和文件为准。

**练习 3**：仓库的学习单位为什么是 notebook 而不是「一个可 pip install 的包」？
**答案**：因为仓库定位是教学参考（collection of architectures/models/tips in Jupyter Notebooks，见 [README.md:L2-L4](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L2-L4)）。每个模型自带数据加载、训练、评估的完整代码，独立可运行，便于逐个拆开学习；打包成库反而会破坏这种自包含性。

### 4.2 三大框架目录分工

#### 4.2.1 概念说明

仓库顶层只有四个业务目录（其余是 `LICENSE`、`README.md` 和讲义目录）。三个框架目录的关系可以这样理解：

```text
                pytorch_ipynb/            ← 主体（121 个 notebook）
                 │ 按「同一模型同一相对路径」约定
                 ├──镜像──▶ pytorch-lightning_ipynb/  （39 个，换 Lightning 写法）
                 └──镜像──▶ tensorflow1_ipynb/        （22 个，换 TF1 写法）

                templates/                ← Lightning 通用实验模板（不是模型）
```

- **`pytorch_ipynb/` 是绝对主体**。它目录最全（autoencoder、basic-ml、cnn、cnn-nlp、gan、gnn、kfold、mechanics、mlp、ordinal、rnn、transfer、transformer、tricks、viz 等 20 余个子目录），并且**只有它**附带了 6 个共享工具模块：`helper_data.py`、`helper_train.py`、`helper_evaluate.py`、`helper_plotting.py`、`helper_utils.py` 和 `helper.py`。这些 helper 是新版 notebook 的公共基础设施，下一讲专门精读。
- **`pytorch-lightning_ipynb/` 是部分模型的 Lightning 孪生版**。子目录只有 6 个（cnn、data-augmentation、kfold、mlp、ordinal、transformer），凡是存在的 notebook，都与 `pytorch_ipynb` 下的同名文件保持相同相对路径——例如两边的 `cnn/cnn-basic.ipynb` 就是同一个模型两种写法。
- **`tensorflow1_ipynb/` 覆盖基础模型，用于框架对照**。子目录包括 autoencoder、basic-ml、cnn、gan、mechanics、metric、mlp 等。TF1 已停止主流演进，这批 notebook 的价值在于「同一个模型换一种框架范式长什么样」。
- **`templates/` 不是模型库**，只含 `templates/pytorch_lightning/` 下的 3 个文件：`template_classification_basic.ipynb`（分类任务通用模板）、`tune_classification_basic.py`（调参脚本）、`submit_command.sh`。它服务于「批量跑实验」而非「读模型」。

#### 4.2.2 核心流程

判断「某个模型有没有某框架版本」的流程：

```text
1. 在 README 表格中找到该模型行
2. 数该行有几个徽章：
   3 个徽章 → 三个目录里各有一份
   2 个徽章 → 只有徽章对应的两个目录有
   1 个徽章 → 通常只在 pytorch_ipynb/ 下
3. 按徽章里的路径直接定位文件
4. （可选）验证：ls 对应目录，确认文件存在
```

反过来，想系统性比较三种框架时，去 README 里找**三徽章行**（如 MLP 基础版），然后打开三个同名文件对照阅读。

#### 4.2.3 源码精读

**① 目录分工的直接证据——README 第 22 行（再次引用）**：

[README.md:L22](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L22)

三个徽章的链接目标分别是 `pytorch-lightning_ipynb/mlp/mlp-basic.ipynb`、`pytorch_ipynb/mlp/mlp-basic.ipynb`、`tensorflow1_ipynb/mlp/mlp-basic.ipynb`。三个路径仅在**顶层框架目录**上不同，子目录与文件名逐字相同——这就是「镜像式布局」约定，记住它，你只需要记住一份相对路径就能在三个框架间自由切换。

**② PyTorch 主库的规模与结构**（本讲实际浏览仓库的观察结果）：

```text
pytorch_ipynb/
├── basic-ml/        感知机、逻辑回归、softmax 回归
├── mlp/             多层感知机及其正则化、从零反向传播变体
├── cnn/             最大的一块：LeNet/AlexNet/VGG/ResNet/DenseNet/MobileNet/...
├── cnn-nlp/         Text CNN（NLP 用卷积）
├── rnn/             RNN/LSTM/GRU/char-RNN
├── transformer/     DistilBERT 系列
├── autoencoder/     AE / 卷积AE / VAE / CVAE
├── gan/             GAN / WGAN / DCGAN
├── gnn/             图神经网络
├── ordinal/         有序回归 CORAL/CORN
├── kfold/ tricks/ transfer/ viz/ related-libraries/   评估、技巧、迁移、可视化
├── mechanics/       数据管线、autograd、并行等工程机制
├── lightning/       PyTorch Lightning 入门示例（注意：在主库内也有一组）
├── helper*.py       6 个共享工具模块（下一讲主角）
└── data/ images/    数据与图片资源
```

目录名与 README 大类基本一一对应，所以「README 大类 ↔ 磁盘子目录」也构成一张速查映射。

**③ 三库规模对比**（在仓库根目录用 `find <目录> -name "*.ipynb" | wc -l` 实测）：

| 目录 | notebook 数量 | 子目录数 | 角色 |
| --- | --- | --- | --- |
| `pytorch_ipynb/` | 121 | 20+ | 主体实现 + helper 基础设施 |
| `pytorch-lightning_ipynb/` | 39 | 6 | 部分模型的 Lightning 孪生版 + 数据增强专题 |
| `tensorflow1_ipynb/` | 22 | 9 | 基础模型的 TF1 对照实现 |

值得注意的细节：`pytorch-lightning_ipynb/` 有一个主库没有的专题子目录 `data-augmentation/`（AutoAugment/TrivialAugment 对照实验，入口见 [README.md:L299-L303](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L299-L303)）；而 `tensorflow1_ipynb/` 也有独有的 `metric/`（孪生网络）。所以「孪生」是大格局，不是铁律。

**④ 模板目录**：

[templates/pytorch_lightning/template_classification_basic.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/templates/pytorch_lightning/template_classification_basic.ipynb)

这是 Lightning 分类任务的通用模板，配套 `tune_classification_basic.py` 调参脚本。它告诉我们仓库的第三种用法：不逐个读模型，而是拿模板批量开实验。本套讲义在 U8 工程化单元会回到这里。

#### 4.2.4 代码实践

**实践目标**：亲手验证「镜像式布局」约定与三库规模。

**操作步骤**：

1. 在仓库根目录执行：

```bash
# 三个框架目录的 notebook 数量
find pytorch_ipynb -name "*.ipynb" | wc -l
find pytorch-lightning_ipynb -name "*.ipynb" | wc -l
find tensorflow1_ipynb -name "*.ipynb" | wc -l
```

2. 验证镜像同名文件：

```bash
ls pytorch_ipynb/cnn/cnn-basic.ipynb pytorch-lightning_ipynb/cnn/cnn-basic.ipynb
ls pytorch_ipynb/mlp/mlp-dropout.ipynb pytorch-lightning_ipynb/mlp/mlp-dropout.ipynb
```

3. 找出「只在 Lightning 库存在」的独有内容：

```bash
ls pytorch-lightning_ipynb/
ls pytorch_ipynb/ | grep data-augmentation || echo "主库无此目录"
```

**需要观察的现象**：步骤 1 应输出 121 / 39 / 22 左右的规模梯度（仓库仍在更新，数字可能小幅变化）；步骤 2 两条 `ls` 都应成功列出同名文件；步骤 3 应看到 `data-augmentation` 只出现在 Lightning 库。

**预期结果**：确认「相对路径一致 + 顶层目录区分框架」的镜像布局成立，并理解 Lightning 库虽小但有独有专题。具体数字以你本地运行结果为准（本讲在 HEAD `59beda3` 下实测为 121/39/22）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 6 个 `helper_*.py` 只放在 `pytorch_ipynb/` 下，而不是仓库根目录供三个框架共享？
**答案**：因为它们是 **PyTorch** 的基础设施（数据加载、训练循环、评估、绘图都调用 `torch` / `torchvision`），TF1 notebook 无法使用；Lightning 库复用的是 PyTorch 张量与模块体系，需要的则是 Lightning 风格的工具。放在 `pytorch_ipynb/` 下与其服务对象一致，也维持了三个框架目录的自包含性。

**练习 2**：朋友告诉你「仓库没有 Lightning 版的 ResNet-18 MNIST」，你如何用 30 秒验证？
**答案**：看 [README.md:L123](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L123) 的 ResNet-18 Digit Classifier 行——该行只有一个红色 PyTorch 徽章，没有紫色徽章；再 `ls pytorch-lightning_ipynb/cnn/ | grep resnet18` 应无结果。结论：朋友说得对。

**练习 3**：`templates/` 目录和三个框架目录的本质区别是什么？
**答案**：三个框架目录收录的是「具体模型的实现」，每个 notebook 解决一个具体模型；`templates/` 收录的是「填空式通用骨架」，不绑定具体模型，用于快速起一个新分类实验并批量调参（`tune_classification_basic.py`）。前者用来读，后者用来改。

### 4.3 notebook 命名规律

#### 4.3.1 概念说明

仓库里 180+ 个文件名不是随意起的，主流命名模式是：

```text
〈架构前缀〉-〈模型名〉[-〈变体〉]-〈数据集〉.ipynb     （连字符风格，CNN/AE/GAN 等多用）
〈架构前缀〉_〈细节〉_〈数据集〉.ipynb                （下划线风格，RNN 系列多用）
```

例如 `cnn-resnet18-mnist.ipynb` 可以直接读成一句话：**用 CNN 家族的 ResNet-18，在 MNIST 数据集上训练**。掌握这个规律后，README 表格甚至都不用看 Title 列，扫一眼文件名就能分类。

#### 4.3.2 核心流程

拆解一个文件名的流程（以 `cnn-alexnet-cifar10-grouped.ipynb` 为例）：

```text
cnn  -  alexnet  -  cifar10  -  grouped
 │        │           │          │
 │        │           │          └─ 变体/技巧后缀：分组卷积版
 │        │           └─ 数据集：CIFAR-10
 │        └─ 架构：AlexNet
 └─ 家族前缀：卷积神经网络

→ 「带分组卷积的 AlexNet，跑 CIFAR-10」
```

常见家族前缀：`cnn-`（卷积网络）、`mlp-`（多层感知机）、`ae-`（autoencoder 自编码器）、`gan-`/`wgan-`/`dcgan-`（生成对抗网络家族）、`rnn_`（循环网络）、`char_rnn-`（字符级 RNN）。常见数据集后缀：`mnist`、`cifar10`、`celeba`、`quickdraw`、`imdb`、`agnews`、`cement`。常见变体词：`deconv`（转置卷积）、`nopool`（无池化）、`packed`（packed sequences）、`var`（变分）、`cvae`（条件 VAE）、`smoothing`（标签平滑）、`fromscratch`（从零实现）、`he-init`（He 初始化）、`dataparallel`（多卡数据并行）。

#### 4.3.3 源码精读

**① 家族前缀 + 架构 + 数据集**——`pytorch_ipynb/cnn/` 目录下的一批真实文件名（本讲实际浏览所得）：

[pytorch_ipynb/cnn/（目录）](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn)

```text
cnn-resnet18-mnist.ipynb        → ResNet-18 + MNIST
cnn-resnet34-quickdraw.ipynb    → ResNet-34 + QuickDraw
cnn-lenet5-cifar10.ipynb        → LeNet-5 + CIFAR-10
cnn-densenet121-mnist.ipynb     → DenseNet-121 + MNIST
cnn-mobilenet-v3-small-cifar10.ipynb → MobileNet-v3 small + CIFAR-10
```

同一架构换数据集（resnet18-mnist / resnet34-quickdraw）、同一数据集换架构（lenet5-cifar10 / mobilenet-v3-small-cifar10）构成了漂亮的「网格」，想找任何组合都能按名索骥。

**② 变体后缀表达技巧差异**：

```text
cnn-alexnet-cifar10-grouped.ipynb   → AlexNet 的分组卷积变体
cnn-resnet34-cifar10-pinmem.ipynb   → ResNet-34 的 pinned memory（锁页内存）变体
cnn-resnet18-celeba-dataparallel.ipynb → ResNet-18 的多卡 DataParallel 变体
```

这些后缀正是 README 中「Tips and Tricks」「Parallel Computing」等大类（[README.md:L308-L314](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L308-L314)、[README.md:L383-L388](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L383-L388)）落到磁盘上的形态：**一个技巧 = 一个独立 notebook = 文件名里一个后缀词**。

**③ 自编码器家族的前缀演化**——README 第 189~232 行的 Autoencoders 大区：

[README.md:L189-L232](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L189-L232)

```text
ae-basic.ipynb            → 基础全连接自编码器
ae-deconv.ipynb           → 反卷积（转置卷积）解码器
ae-deconv-nopool.ipynb    → 反卷积且不用池化
ae-conv-nneighbor.ipynb   → 最近邻插值上采样解码器
ae-var.ipynb              → 变分自编码器 VAE
ae-conv-var.ipynb         → 卷积版 VAE
ae-cvae.ipynb             → 条件 VAE
ae-cnn-cvae_no-out-concat.ipynb → 卷积条件 VAE，标签不参与重建损失
```

从 `ae-` 前缀出发，后缀像搭积木一样叠加出整个家族谱系——读懂名字等于读懂了这组 notebook 的目录页。

**④ 双下划线的特殊约定**：

[README.md:L25](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L25)

`mlp-fromscratch__sigmoid-mse.ipynb` 中用**双下划线**把「从零实现」与「sigmoid 激活 + MSE 损失」的实现细节隔开。这类极少数文件提醒我们：规律覆盖 95%，个例要单独记。

**⑤ RNN 系列的下划线风格**——README 第 260~284 行：

[README.md:L260-L284](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L260-L284)

```text
rnn_simple_imdb.ipynb             → 单层简单 RNN + IMDb
rnn_simple_packed_imdb.ipynb      → 加 packed sequences
rnn_lstm_packed_imdb-glove.ipynb  → LSTM + packed + GloVe 词向量
rnn_bi_multilayer_lstm_own_csv_agnews.ipynb → 双向多层 LSTM + 自有 CSV + AG News
char_rnn-charlesdickens.ipynb     → 字符级 RNN 生成狄更斯风格文本
```

RNN 目录用下划线分层（这批 notebook 成文较早），但「前缀 → 细节堆叠 → 数据集」的骨架完全一致。另外注意 `imdb` 指 IMDb 影评情感分类数据集，`own_csv` 表示「这是教你接入自己 CSV 数据的模板」。

#### 4.3.4 代码实践

**实践目标**：建立「文件名 → 模型语义」的快速解码能力。

**操作步骤**：

1. 列出一个目录的全部文件名，逐个解码：

```bash
ls pytorch_ipynb/cnn/
ls pytorch_ipynb/autoencoder/
ls pytorch_ipynb/rnn/
```

2. 按名字猜内容再打开验证。例如对以下 4 个文件，先写下你猜的「架构 / 数据集 / 特殊技巧」，再打开 notebook 开头的标题与说明 cell 对照：

```text
pytorch_ipynb/gan/wgan-1.ipynb
pytorch_ipynb/cnn/nin-cifar10_filter-response-norm.ipynb
pytorch_ipynb/mlp/mlp-fromscratch__sigmoid-mse.ipynb
pytorch_ipynb/rnn/rnn_gru_packed_imdb.ipynb
```

3. 反向练习：想找「在 CelebA 人脸数据上训练的 VGG-16」，先猜文件名再验证：

```bash
ls pytorch_ipynb/cnn/ | grep -i "vgg16.*celeba"
```

**需要观察的现象**：步骤 2 中你的猜测与 notebook 实际标题的吻合度；步骤 3 中 `grep` 应命中 `cnn-vgg16-celeba.ipynb`（还有一个 `cnn-vgg16-celeba-data-parallel.ipynb` 是它的多卡变体，见 [README.md:L111](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L111)）。

**预期结果**：多数文件名可被准确解码；解码不出的词（如 `pinmem`、`nin`）记入生词本，查 README 对应行即可弄清（`nin` = Network in Network，见 [README.md:L99-L103](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L99-L103)）。`wgan-1`、`wgan` 与 `gan` 的区别需打开 notebook 确认，此处待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：解码 `cnn-resnet50-celeba-dataparallel.ipynb` 与 `ae-deconv-jaccard.ipynb`。
**答案**：前者 = ResNet-50 架构 + CelebA 人脸数据 + 多 GPU DataParallel 技巧的 CNN；后者 = 用转置卷积解码、以 Jaccard 距离为重建损失的自编码器（对应 [README.md:L208](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L208)）。

**练习 2**：`ae-cvae.ipynb` 和 `ae-cvae_no-out-concat.ipynb` 这对文件名的差异对应 README 里哪两个标题？
**答案**：对应 [README.md:L229-L230](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L229-L230) 的 "Conditional Variational Autoencoder (with labels in reconstruction loss)" 与 "(without labels in reconstruction loss)"——`no-out-concat` 指标签不拼接到解码器输出端、即不参与重建损失。

**练习 3**：如果仓库未来新增「用 Vision Transformer 在 CIFAR-10 上训练」的 notebook，按命名规律它的文件名大概是什么？
**答案**：按 CNN 家族的连字符风格，大概率落在 `pytorch_ipynb/cnn/` 或新目录下，形如 `cnn-vit-cifar10.ipynb` 或 `vit-cifar10.ipynb` 之类（架构前缀 + 架构名 + 数据集）。这是规律的外推预测，实际命名以仓库更新为准。

## 5. 综合实践

**任务：制作你的专属「模型动物园速查表」。**

1. **克隆并通读**：在本地 `git clone` 本仓库，通读一遍 `README.md`（435 行，10 分钟）。
2. **建表**：新建一个 Markdown 文件（可放在本讲义目录外你自己的笔记里），做一张三列表格——`模型类别 | 所在目录 | 代表性 notebook 文件名`。要求覆盖至少 8 个大类，例如：

| 模型类别 | 所在目录 | 代表性 notebook 文件名 |
| --- | --- | --- |
| 传统机器学习（线性模型） | `pytorch_ipynb/basic-ml/` | `softmax-regression.ipynb` |
| 多层感知机 | `pytorch_ipynb/mlp/` | `mlp-basic.ipynb`、`mlp-dropout.ipynb` |
| 卷积网络 | `pytorch_ipynb/cnn/` | `cnn-resnet18-mnist.ipynb` |
| 循环网络 | `pytorch_ipynb/rnn/` | `rnn_lstm_packed_imdb.ipynb` |
| Transformer | `pytorch_ipynb/transformer/` | `distilbert-hf-finetuning.ipynb` |
| 自编码器 | `pytorch_ipynb/autoencoder/` | `ae-conv-var.ipynb` |
| 生成对抗网络 | `pytorch_ipynb/gan/` | `dcgan-celeba.ipynb` |
| ……（继续补充） | | |

3. **标注个人目标**：在表中加一列「我想学」，从全库挑出**你最想学会的 5 个模型**，写出完整相对路径（如 `pytorch_ipynb/cnn/cnn-densenet121-cifar10.ipynb`），并用一句话说明为什么选它。
4. **交叉验证**：对这 5 个路径逐一 `ls` 确认存在；同时在 README 里找到它们所在的行，记录每行有哪些框架徽章。
5. **检验镜像约定**：对这 5 个模型，检查 `pytorch-lightning_ipynb/` 与 `tensorflow1_ipynb/` 下是否存在同名文件，在表里标注「有 Lightning 版 / 有 TF1 版 / 仅 PyTorch」。

**产出标准**：这张表就是你后续学习全仓库的导航图——本套讲义 U2~U8 的每一篇，都对应你表里的一行或几行。

## 6. 本讲小结

- 本仓库是**以 Jupyter Notebook 为载体的深度学习模型动物园**，没有可安装的包和主程序，学习单位是单个自包含 notebook；`README.md` 是全库唯一总索引。
- README 按「大类二级标题 → 小类三级标题 → 表格行 → 框架徽章」组织，**徽章颜色即框架**：红色 PyTorch、紫色 Lightning、橙色 TF1，点徽章直达文件。
- 三个框架目录遵循**镜像式布局**：`pytorch_ipynb/`（121 个 notebook，主体，独享 6 个 `helper_*.py`）、`pytorch-lightning_ipynb/`（39 个，孪生版 + 数据增强专题）、`tensorflow1_ipynb/`（22 个，基础模型对照）；同名 notebook 相对路径完全一致，仅顶层目录不同。
- `templates/pytorch_lightning/` 是通用实验骨架，不是模型收藏，服务于「改模板跑实验」的用法。
- notebook 命名高度规律：**家族前缀 - 架构 - [变体] - 数据集**（RNN 系用下划线风格），扫文件名即可完成粗分类。
- README 索引偶尔滞后（如 Grouped AlexNet 一行链接与实际文件名词序不符），**徽章打不开时以目录为准**。

## 7. 下一步学习建议

下一讲（`u1-l2-setup-first-notebook.md`，环境搭建与第一个 Notebook）将带你安装 PyTorch 环境、实际运行全库最简单的 `pytorch_ipynb/basic-ml/perceptron.ipynb`，并拆解几乎所有 notebook 共有的「Imports → Settings/Dataset → Model → Training → Evaluation」五段式结构——那是读懂每一个 notebook 的通用钥匙。

再往下一讲（`u1-l3-helper-modules.md`）会精读 `pytorch_ipynb/` 下 6 个 `helper_*.py` 共享模块。如果你想提前热身，可以先通读 `README.md` 中你感兴趣的模型行，并试着打开一两个 notebook 感受其结构。
