# 自定义 Dataset 加载自己的数据

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立写出自定义 `Dataset` 的「三要素」——`__init__`、`__getitem__`、`__len__`，并解释 DataLoader 为什么只依赖这三个方法就能消费任何数据源。
2. 掌握「图片目录 + 标签清单」这一自定义视觉数据集的通用组织形态：对照 `custom-dataloader-example.ipynb`、`custom-data-loader-celeba.ipynb`、`custom-data-loader-quickdraw.ipynb` 三个 notebook，看清同一套 Dataset 模板如何适配三种不同的标签来源与目录结构。
3. 读懂 `pytorch_ipynb/helper.py` 中的 `quickdraw_npy_to_imagefile`：它如何把 Quickdraw 的 `.npy` 数组堆离线转换成「每类一个子目录」的图片目录，以及转换之后如何生成 train/valid/test 标签清单。
4. 为一个自选图片目录（或自己动手造出来的目录）编写支持 `transform` 的 Dataset 类，用 DataLoader 批量加载并可视化样本。

## 2. 前置知识

- **Dataset 与 DataLoader 的分工**（u4-l1 已建立）：`Dataset` 只回答「第 `i` 个样本是什么」和「一共有多少个」；`DataLoader` 负责批量取、打乱（经 Sampler）、多进程拼装。本讲专门解决前一半：当数据不是 `torchvision.datasets` 里内置的 MNIST/CIFAR 时，`Dataset` 这一层怎么写。
- **你已经写过一次 Dataset**（u2-l2 的 Iris 姊妹篇）：当时手写了 `__init__` / `__getitem__` / `__len__` 三要素，并记住「标签须是 int64」。本讲把同样的骨架搬到「图片文件存在磁盘上」的场景——区别在于：Iris 的特征是现成的浮点数组，而这里 `__getitem__` 要先用 `PIL.Image.open` 从磁盘读图。
- **transform 挂在 Dataset 上**（u4-l1 的伏笔）：共享 helper 之所以能给训练子集挂随机增强的 `train_transforms`、给验证/测试子集挂确定性变换，前提正是 transform 在 `__getitem__` 里**逐样本**应用——每个样本取出时才变换，而不是提前把整个数据集变换好。
- **`transforms.ToTensor()` 会把像素除以 255**（u2-l2 已提）：所以下面三个 notebook 里都不再需要手工 `x/255.`。
- **NCHW 布局**（u3-l1 已建立）：PyTorch 张量按「批数 × 通道 × 高 × 宽」排列，而 `matplotlib.imshow` 期望 HWC，可视化时要用 `permute(1, 2, 0)` 换轴。
- **Pandas 两句话**：`pd.read_csv` 把表格读成 DataFrame；`df['列名']` 取出的是带索引的 Series，`df['列名'].values` 取出的是不带索引的 NumPy 数组——这个区别在本讲 4.2.3 会变成一个真实的坑。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pytorch_ipynb/mechanics/custom-dataloader-png/custom-dataloader-example.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-dataloader-png/custom-dataloader-example.ipynb) | 最小完整示例：CSV 存「文件名 → 标签」，`MyDataset` 读同目录下的 PNG，讲清 Dataset 三要素 |
| [pytorch_ipynb/mechanics/custom-data-loader-celeba.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-data-loader-celeba.ipynb) | CelebA 人脸图片变体：标签来自数据集自带的属性文件，只取 `Male` 列做二分类，图片转灰度 |
| [pytorch_ipynb/mechanics/custom-data-loader-quickdraw.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-data-loader-quickdraw.ipynb) | Quickdraw 涂鸦图片变体：先用 helper 把 `.npy` 转成「每类一个子目录」的图片目录，再自己生成标签 CSV |
| [pytorch_ipynb/helper.py](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper.py) | 共享工具模块：`quickdraw_npy_to_imagefile` 把 Quickdraw 的 `.npy` 数组堆逐行写成 PNG 图片文件 |

同族扩展阅读（本讲不精读）：`pytorch_ipynb/mechanics/` 下还有 `custom-data-loader-mnist.ipynb`、`custom-data-loader-svhn.ipynb`、`custom-data-loader-fashionmnist.ipynb`、`custom-data-loader-csv.ipynb`、`custom-data-loader-afad.ipynb` 等同一模板的更多实例。

阅读提示：`.ipynb` 的永久链接行号指向其原始 JSON 文本中的行，在 GitHub 上打开链接后定位到对应 cell 即可对照。

## 4. 核心概念与源码讲解

### 4.1 Dataset 三要素：PyTorch 与你的数据之间的契约

#### 4.1.1 概念说明

`torchvision.datasets.MNIST` 这类内置数据集之所以「一行就能用」，是因为官方已经替你写好了读文件、解析标签的全部逻辑。但真实项目里，你的数据往往是：一个装满照片的文件夹、一份爬下来的 CSV、一批工业相机拍的单片图像——没有任何现成类可用。

PyTorch 对此的答案是**接口契约**：`DataLoader` 不关心你的数据长什么样，它只会

- 用 `dataset[i]`（触发 `__getitem__`）取第 `i` 个样本；
- 用 `len(dataset)`（触发 `__len__`）知道总数，配合 Sampler 决定索引顺序。

所以只要你写一个继承 `torch.utils.data.Dataset` 的类、实现这两个方法（习惯上把「读清单、存配置」放进 `__init__`，合称三要素），DataLoader 就能自动完成取样本、逐样本 transform、堆叠成 batch、打乱、多进程这些脏活。这也是 u4-l1 讲过的分工的另一半：**Sampler 与 DataLoader 一行都不用改，换数据只换 Dataset**。

三要素各自的职责：

| 方法 | 职责 | 典型内容 |
| --- | --- | --- |
| `__init__` | 一次性准备 | 读标签清单（CSV/txt）、记下图片根目录、保存 transform |
| `__getitem__` | 取单个样本 | 按索引拼出文件路径 → `Image.open` 读图 → 应用 transform → 返回 `(img, label)` |
| `__len__` | 报告规模 | 返回清单行数 |

一个关键设计是**惰性读取（lazy loading）**：`__init__` 只读一份轻量的标签清单，图片直到 `__getitem__` 被调用才从磁盘读入。这样内存占用与数据集规模无关——一百万张图片的数据集和一个只有一千张的，初始化开销几乎一样；图片用完即可被回收。若反过来在 `__init__` 里把全部图片读进内存，CelebA 的 20 万张 178×218 人像会立刻把内存吃穿。

#### 4.1.2 核心流程

`custom-dataloader-example.ipynb` 假设你手工准备了这样的目录（仓库里**不含**这些图片与 CSV，它们是用户自备的）：

```text
当前目录/
├── mnist_train/            # 图片文件夹
│   ├── 0.png
│   ├── 1.png
│   └── ...
├── mnist_train.csv         # 标签清单：两列
├── mnist_valid/ + mnist_valid.csv
└── mnist_test/  + mnist_test.csv
```

CSV 的形态（256 行）：

```text
   Class Label  File Name
0            5      0.png
1            8      1.png
2            8      2.png
```

DataLoader 拿到一个 batch 的索引后，对每个索引 `i` 做的事：

```text
dataset[i]
  ├─ __getitem__(i)
  │    ├─ 路径 = os.path.join(img_dir, 清单第 i 行的 File Name)
  │    ├─ img  = Image.open(路径)          # 惰性读取，此刻才碰磁盘
  │    ├─ img  = transform(img)            # ToTensor：PIL 图 → [0,1] 张量
  │    └─ return img, 清单第 i 行的 Class Label
  └─ collate 把 batch_size 个 (img, label) 堆叠成两个张量
```

#### 4.1.3 源码精读

先看它如何检查原始数据——用 PIL 打开一张图、打印数组确认像素范围：

[custom-dataloader-example.ipynb:L101-L102](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-dataloader-png/custom-dataloader-example.ipynb#L101-L102)
用 `Image.open` 打开 `mnist_train/1.png` 并显示——动手写 Dataset 前先肉眼确认一张样本，是排查「路径拼错 / 图片损坏」问题的第一手段。

[custom-dataloader-example.ipynb:L302-L304](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-dataloader-png/custom-dataloader-example.ipynb#L302-L304)
读入 `mnist_train.csv` 并打印形状与前几行：确认清单是 `(256, 2)` 的两列表，列名 `Class Label` 与 `File Name`——这两个列名马上会硬编码进 Dataset。

然后是本讲的主角 `MyDataset`，完整实现了三要素：

[custom-dataloader-example.ipynb:L519-L540](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-dataloader-png/custom-dataloader-example.ipynb#L519-L540)
`MyDataset(Dataset)` 的完整定义，逐段拆解：

- **`__init__(csv_path, img_dir, transform=None)`**：`pd.read_csv` 读标签清单；把 `df['File Name']`（文件名 Series）和 `df['Class Label']`（标签 Series）存为实例属性；再存图片根目录与 transform。注意这里**没有任何读图操作**——惰性读取。
- **`__getitem__(index)`**：`os.path.join(self.img_dir, self.img_names[index])` 拼路径 → `Image.open` 读图 → 若传了 transform 则应用 → 返回 `(img, label)` 二元组。返回单个样本而非一个 batch，攒批是 DataLoader 的事。
- **`__len__`**：`return self.y.shape[0]`，即清单行数。

transform 与 DataLoader 的组装：

[custom-dataloader-example.ipynb:L574-L592](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-dataloader-png/custom-dataloader-example.ipynb#L574-L592)
注释特意说明 `transforms.ToTensor()` 内部已把像素除以 255，所以手工 `x/255.` 的 Lambda 被注释掉；随后 `MyDataset(csv_path=..., img_dir=..., transform=custom_transform)` 实例化，`DataLoader(batch_size=32, shuffle=True, num_workers=4)` 完成封装——从这一行起，它的用法与内置 MNIST 的 loader 完全无异。

[custom-dataloader-example.ipynb:L640-L652](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-dataloader-png/custom-dataloader-example.ipynb#L640-L652)
标准「epoch × batch」双层循环迭代 train_loader，每个 batch 打印批号与批大小后 `x.to(device)` / `y.to(device)`。256 个样本、batch_size=32，输出显示每个 epoch 恰好 8 个 batch（索引 0–7），与 u4-l1 的批数公式 \(\lceil 256/32 \rceil = 8\) 一致。

[custom-dataloader-example.ipynb:L670](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-dataloader-png/custom-dataloader-example.ipynb#L670)
打印 `x.shape`，输出为 `torch.Size([32, 1, 28, 28])`——NCHW 四维，可直接喂给卷积网络；若要喂 MLP，用 [L687](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-dataloader-png/custom-dataloader-example.ipynb#L687) 的 `x.view(-1, 28*28)` 展平成 `[32, 784]`（u2-l2 讲过的循环内展平）。

#### 4.1.4 代码实践

1. **实践目标**：不改任何代码，靠「读源码 + 类型推理」预测 `dataset[0]` 返回什么，从而检验对三要素的理解。
2. **操作步骤**（源码阅读型实践，仓库不含 `mnist_train` 图片与 CSV，无法直接运行）：
   - 精读 [L519-L540](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-dataloader-png/custom-dataloader-example.ipynb#L519-L540) 的 `MyDataset`，回答：`__init__` 里哪几行在碰磁盘？`__getitem__` 呢？
   - 假设 `custom_transform = transforms.ToTensor()`，对 CSV 首行（`Class Label=5, File Name=0.png`）手写推断 `train_dataset[0]` 的返回值结构。
   - 把推断结果与下面「预期结果」对照；待本地验证（见第 5 节综合实践，我们会亲手造出可运行的同款目录）。
3. **需要观察的现象**：（本地复现后）`train_dataset[0]` 打印出 `(tensor(...), 5)` 形态的二元组。
4. **预期结果**：
   - `__init__` 碰磁盘一次：`pd.read_csv(csv_path)`；不读任何图片。
   - `train_dataset[0]` 返回二元组 `(img, label)`：`img` 是形状 `[1, 28, 28]` 的 float 张量、取值在 \([0,1]\)（ToTensor 除以 255）；`label` 是整数 `5`——默认 collate 把一批 label 堆成 **int64** 标签张量，恰好满足 `CrossEntropyLoss` 的 dtype 要求（u2-l2 的结论）。
   - `len(train_dataset)` 返回 256。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `MyDataset.__init__` 里不把 256 张图片一次性读进内存？这一设计对 20 万张的 CelebA 意味着什么？

**答案**：`__init__` 只需建立「索引 → 文件路径 + 标签」的映射（读一份轻量 CSV），图片推迟到 `__getitem__` 被调用才读。内存占用与数据集规模无关（\(O(1)\) 而非 \(O(N)\)），且图片张量用完即可回收。若在 `__init__` 全量载入，CelebA 的 202599 张 178×218×3 图像会占用数十 GB 内存，惰性读取让这件事根本不发生。

**练习 2**：如果把 `__getitem__` 里的 `if self.transform is not None` 判断去掉、直接 `return img, label`（返回 PIL 图），会在哪一步报错？

**答案**：不会在 `dataset[i]` 这一步报错，而是在 DataLoader 的 collate 阶段或模型前向时出错——默认 collate 无法把一批 PIL Image 对象堆叠成张量（它不认识 PIL 类型）；即便手工绕过，PIL 图也不是 `torch.Tensor`，`.to(device)` 与卷积层都无法接受。transform（至少一个 `ToTensor()`）是「PIL 世界」与「张量世界」的翻译官。

**练习 3**：`MyDataset` 中 `self.img_names[index]` 里 `img_names` 是 pandas Series。用 Series 做 `[]` 索引时按什么定位？为什么这份代码恰好没问题？

**答案**：Series 的 `[]` 是**按行标签（label）**定位，不是按位置。这份 CSV 用 `pd.read_csv` 读入时自动生成了 0..N-1 的整数行号索引，标签与位置恰好重合，所以 `self.img_names[index]` 表现得像按位置取。一旦清单像 CelebA/Quickdraw 那样用文件名或路径作索引，就必须改用 `.values` / `df.index.values` 拿到底层 NumPy 数组按位置取——这正是 4.2.3 要看的进化版写法。

### 4.2 图片目录数据集：同一模板的三个实例

#### 4.2.1 概念说明

三个 notebook（PNG 示例、CelebA、Quickdraw）的 Dataset 类几乎逐字相同，差异全部集中在两件事上：

1. **标签清单从哪来**：
   - PNG 示例：假设你手头已有一份两列 CSV（人工整理或脚本生成）。
   - CelebA：数据集**自带**属性文件 `list_attr_celeba.txt`（40 个属性 × 202599 行），只挑 `Male` 列当标签，把 -1/1 改成 0/1。
   - Quickdraw：什么都没有——`.npy` 转成图片目录后，**自己遍历目录生成**清单（目录名即类别，经 `label_dict` 映射成整数标签）。
2. **图片怎么摆**：
   - PNG 示例 / CelebA：所有图片平铺在一个目录里，清单只存文件名。
   - Quickdraw：**每个类别一个子目录**（`quickdraw-png_set1/penguin/penguin_112865.png`），清单存「子目录/文件名」的相对路径。

第 2 种「每类一个子目录」的摆法是自定义视觉数据集最常见的组织形态（`torchvision.datasets.ImageFolder` 也以它为约定，可作为背景知识了解），好处是标签隐含在目录结构里，清单生成可以完全自动化。

#### 4.2.2 核心流程

**CelebA 的预处理**（一次性、离线）：

```text
list_attr_celeba.txt（自带属性文件）
  → pd.read_csv(sep="\s+", skiprows=1, usecols=['Male'])   # 只留 Male 列，文件名成为索引
  → df.loc[df['Male'] == -1, 'Male'] = 0                    # -1/1 → 0/1
  → 前 80% / 后 20% 顺序切分（数据本身不按性别排序，无需 shuffle）
  → to_csv 写出 celeba_gender_attr_train.txt / _test.txt
```

**Quickdraw 的预处理**（在 `.npy` 转图片目录之后，详见 4.3）：

```text
quickdraw-png_set1/{类别}/{类别}_000000.png ...
  → 双层循环遍历目录：paths 收集 "类别/文件名"，labels 收集 label_dict[类别]
  → shuffle_arrays_unison 打乱 paths 与 labels（保持对齐）
  → 按比例切三段：cut1 = ⌊0.7N⌋，cut2 = ⌊0.8N⌋ → 70% 训练 / 10% 验证 / 20% 测试
  → 每段写成一份 CSV（Path 作索引列）
```

两者的对比浓缩成一张表：

| 维度 | CelebA | Quickdraw |
| --- | --- | --- |
| 标签来源 | 数据集自带属性文件 | 自己遍历目录 + `label_dict` |
| 清单格式 | 空格分隔 txt，文件名作索引 | 逗号分隔 CSV，`类别/文件名` 作索引 |
| 划分方式 | 顺序前 80% / 后 20% | shuffle 后 70 / 10 / 20 |
| 是否需要 shuffle | 不需要（图片不按类别排序） | 需要（按类别批量生成，天然有序） |
| transform | `Grayscale()` + `ToTensor()` | `ToTensor()` |

注意划分策略与数据摆放方式的耦合：Quickdraw 的图片是**按类别批量生成**的（一个类别的 `.npy` 连续写出），不打乱就切分会导致训练集全是前几个类别、测试集全是后几个类别——这就是它必须 `shuffle_arrays_unison` 的原因；CelebA 的文件名顺序与性别无关，顺序切分才安全。**先问「我的数据有没有按标签排序」再选划分方式**，是自定义数据管线最容易踩的坑。

#### 4.2.3 源码精读

**CelebA 侧**。先读属性文件并二值化标签：

[custom-data-loader-celeba.ipynb:L164-L169](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-data-loader-celeba.ipynb#L164-L169)
`pd.read_csv('list_attr_celeba.txt', sep="\s+", skiprows=1, usecols=['Male'])`：跳过文件头部的计数行、只保留 `Male` 列；第一列（文件名 `000001.jpg` 等）自动成为 DataFrame 索引。随后 `df.loc[df['Male'] == -1, 'Male'] = 0` 把 -1/1 标签改成 0（女）/ 1（男）。

[custom-data-loader-celeba.ipynb:L192-L197](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-data-loader-celeba.ipynb#L192-L197)
按行号顺序切前 80% 为训练、后 20% 为测试，并各自 `to_csv(sep=" ")` 写成文本清单。划分前的类别计数（[L232](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-data-loader-celeba.ipynb#L232) 的 `np.bincount`）显示训练集为 `[94093, 67986]`——两类不平衡但没有极端倾斜，顺序切分可行。

然后是与 `MyDataset` 同款的 `CelebaDataset`：

[custom-data-loader-celeba.ipynb:L394-L417](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-data-loader-celeba.ipynb#L394-L417)
与 `MyDataset` 相比只有三处不同：`pd.read_csv(txt_path, sep=" ", index_col=0)` 显式把第一列（文件名）设为索引；`self.img_names = df.index.values` 与 `self.y = df['Male'].values` 都取 `.values` 得到 NumPy 数组——按**位置**索引，绕开了 4.1.5 练习 3 的 Series 标签索引陷阱；标签列换成 `Male`。`__getitem__` / `__len__` 与模板一字不差。

[custom-data-loader-celeba.ipynb:L441-L455](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-data-loader-celeba.ipynb#L441-L455)
transform 链多了一步 `transforms.Grayscale()`：178×218×3 的彩色人脸先压成单通道，再 `ToTensor()`。DataLoader 用 `batch_size=128, shuffle=True, num_workers=4`。

[custom-data-loader-celeba.ipynb:L487-L500](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-data-loader-celeba.ipynb#L487-L500)
与 PNG 示例相同的双层迭代循环（每 epoch 只取第一个 batch 就 `break`，快速验证）。批形状见 [L528](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-data-loader-celeba.ipynb#L528)：`torch.Size([128, 1, 218, 178])`——Grayscale 生效，通道数为 1。

**Quickdraw 侧**。目录遍历与清单生成：

[custom-data-loader-quickdraw.ipynb:L115-L125](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-data-loader-quickdraw.ipynb#L115-L125)
`label_dict` 把 10 个类别名（lollipop、binoculars、mouse……）映射为整数标签 0–9——目录名 → 整数标签的「翻译表」，后面遍历目录时要用。

[custom-data-loader-quickdraw.ipynb:L196-L210](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-data-loader-quickdraw.ipynb#L196-L210)
双层循环遍历图片目录：外层 `os.listdir(main_dir)` 枚举类别子目录（跳过非目录项），内层枚举每个子目录下的文件；`paths` 收集 `类别/文件名` 形式的相对路径，`labels` 收集 `label_dict[类别]`。10 类共收集 1515745 个样本。

[custom-data-loader-quickdraw.ipynb:L226-L237](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-data-loader-quickdraw.ipynb#L226-L237)
mlxtend 的 `shuffle_arrays_unison(arrays=[paths, labels], random_seed=3)` 把路径数组与标签数组**同步**打乱（对齐不破坏）；再用两个切割点 `cut1 = int(len*0.7)`、`cut2 = int(len*0.8)` 切出 70/10/20 三段。

[custom-data-loader-quickdraw.ipynb:L321-L329](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-data-loader-quickdraw.ipynb#L321-L329)
把训练段的 `(Path, Label)` 写成 DataFrame、`set_index('Path')` 后 `to_csv` 落盘（valid/test 段同理，各写一份）——至此 Quickdraw 也拥有了自己生成的标签清单，格式与 CelebA 的 txt 完全同构。

[custom-data-loader-quickdraw.ipynb:L535-L540](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-data-loader-quickdraw.ipynb#L535-L540)
清单写完后立刻用 `Image.open` 打开其中一张（`os.path.join(main_dir, df.index[1])`，`df.index` 即 Path 列）：形状 (28, 28)、能正常显示——**生成清单后先抽查一张**，与 4.1.3 的「先看一张图」呼应。

Dataset 类与加载：

[custom-data-loader-quickdraw.ipynb:L567-L590](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-data-loader-quickdraw.ipynb#L567-L590)
`QuickdrawDataset`：第三次实例化同一模板。`pd.read_csv(txt_path, sep=",", index_col=0)` 读自己生成的 CSV，`df.index.values` 拿到 `类别/文件名` 数组，`df['Label'].values` 拿到标签。注意 `__getitem__` 里 `os.path.join(self.img_dir, "penguin/penguin_112865.png")` ——`os.path.join` 天然支持带子目录的相对路径，这是它与平铺目录的唯一区别。

[custom-data-loader-quickdraw.ipynb:L613-L626](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-data-loader-quickdraw.ipynb#L613-L626)
只有 `ToTensor()` 一个变换（Quickdraw 本就是单通道黑白涂鸦，无需 Grayscale）；`batch_size=128`。迭代循环（[L658-L671](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-data-loader-quickdraw.ipynb#L658-L671)）与 CelebA 完全相同，批形状 `torch.Size([128, 1, 28, 28])`（[L699](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-data-loader-quickdraw.ipynb#L699)）。

[custom-data-loader-quickdraw.ipynb:L734-L735](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-data-loader-quickdraw.ipynb#L734-L735)
`x[0].permute(1, 2, 0)` 把 `[1, 28, 28]` 换轴成 `[28, 28, 1]`（CHW → HWC），随后（[L759](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-data-loader-quickdraw.ipynb#L759)）`imshow` 可视化——从 DataLoader 出来的图是「完整正确」的最后一步人工核验。

#### 4.2.4 代码实践

1. **实践目标**：用一张「三处不同」对照表检验你是否真的看懂了三个 Dataset 类，而不是背下了某一个。
2. **操作步骤**（源码阅读型实践）：
   - 分别打开 [MyDataset（L519-L540）](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-dataloader-png/custom-dataloader-example.ipynb#L519-L540)、[CelebaDataset（L394-L417）](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-data-loader-celeba.ipynb#L394-L417)、[QuickdrawDataset（L567-L590）](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-data-loader-quickdraw.ipynb#L567-L590)，逐行 diff，填出下表：

   | 对比点 | MyDataset | CelebaDataset | QuickdrawDataset |
   | --- | --- | --- | --- |
   | `read_csv` 的 `sep` / `index_col` | ？ | ？ | ？ |
   | `img_names` 来源 | ？ | ？ | ？ |
   | `y` 来源 | ？ | ？ | ？ |
   | transform 链 | ？ | ？ | ？ |

   - 再回答：三个类的 `__getitem__` 与 `__len__` 有任何一行实质不同吗？
3. **需要观察的现象**：填完表应发现「不同」全部集中在 `__init__` 的清单解析两三行；`__getitem__` / `__len__` 完全同构。
4. **预期结果**：sep 分别为默认`,` / `" "` / `","`；img_names 分别来自 `df['File Name']` / `df.index.values` / `df.index.values`；y 分别来自 `df['Class Label']` / `df['Male'].values` / `df['Label'].values`；transform 分别为 ToTensor / Grayscale+ToTensor / ToTensor。`__getitem__` 与 `__len__` 除 docstring 外无实质差异——**换数据源时只需改清单解析，取样本逻辑一次写好处处复用**，这正是三要素契约的价值。

#### 4.2.5 小练习与答案

**练习 1**：CelebA 顺序切 80/20 前为什么不用 shuffle？Quickdraw 为什么必须 shuffle？

**答案**：划分前要想「文件顺序与标签是否相关」。CelebA 的图片文件名顺序与性别属性无关（notebook 明确说明 images not ordered by gender），顺序切分得到的两个子集类别比例几乎相同（训练 [94093, 67986]、测试 [24072, 16448]，比例一致）。Quickdraw 的图片是**按类别连续生成**的（一个 `.npy` 一个类别地写），不打乱就切分，训练/验证/测试会各自缺掉若干类别，模型根本见不到完整标签空间，所以必须 `shuffle_arrays_unison` 同步打乱路径与标签后再切。

**练习 2**：CelebA 的图片是 178×218×3 的彩色 JPEG，为什么批形状是 `[128, 1, 218, 178]` 而不是 `[128, 3, 218, 178]`？

**答案**：因为 transform 链在 `ToTensor()` 之前先加了 `transforms.Grayscale()`（[L444](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-data-loader-celeba.ipynb#L444)），每个样本在 `__getitem__` 里被压成单通道；维度顺序 1×218×178 则是 PyTorch 的 NCHW 约定（高在前、宽在后）。这也顺带回答了「transform 在哪一层生效」：在样本级、逐张生效，而不是对整个 batch。

**练习 3**：如果把 `QuickdrawDataset` 的 `__getitem__` 改成 `return img, self.y`（返回整个标签数组），会发生什么？

**答案**：DataLoader 的默认 collate 试图把 batch_size 个「(图, 一维数组)」堆叠时，要么直接抛形状不匹配的异常，要么（batch_size=1 时侥幸通过）产出完全错误的标签形状。`__getitem__` 的契约是**恰好返回一个样本**，索引 `index` 必须贯穿到标签（`self.y[index]`）——这是三要素中最容易被无意破坏的一条。

### 4.3 Quickdraw npy 转换：从数组堆到图片目录

#### 4.3.1 概念说明

Quickdraw（Google 的涂鸦数据集）发布的 bitmap 版本是**每类别一个 `.npy` 文件**：一个形状为 `(N, 784)` 的 uint8 数组堆，每行是一张 28×28 灰度图按行展开的像素。这种形态对 NumPy 友好，但离 4.2 的「图片目录 + 标签清单」管线还差一步**离线转换**：

- `.npy` 无法直接给 `Image.open` 读，也就进不了标准 Dataset 模板；
- 转成 PNG 后可以肉眼抽查样本、随意取子集、与任何按文件组织的数据工具兼容。

`pytorch_ipynb/helper.py` 的 `quickdraw_npy_to_imagefile` 就是这个一次性批处理工具：输入一个装着若干 `.npy` 的目录，输出「每类别一个子目录、每张图一个 PNG」的标准布局。转换只做一次，产出物（图片目录）才是 4.2 流程的输入。

#### 4.3.2 核心流程

```text
quickdraw_npy_to_imagefile(inpath, outpath, subset):
  1. 列出 inpath 下所有 .npy；若给了 subset，只保留类别名在其中的文件
  2. 对每个 .npy 文件：
       a. 从文件名提取类别标签 label
       b. 在 outpath 下建子目录 outpath/label/
       c. X = np.load(文件)                # (N, 784)
       d. 逐行 row.reshape(28, 28)，按 "label_%06d.png" 编号写出
```

其中标签提取 `label = (i.split('-')[-1]).split('.npy')[0]` 依赖 Google 的命名约定：文件形如 `full_numpy_bitmap_lollipop.npy`，按 `-` 切开取最后一段、再去掉扩展名，得到 `lollipop`。

#### 4.3.3 源码精读

[pytorch_ipynb/helper.py:L6-L32](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper.py#L6-L32)
函数签名与文档字符串：`quickdraw_npy_to_imagefile(inpath, outpath, filetype='png', subset=None)`。docstring 给出了下载数据的 `gsutil` 命令与用法示例 `quickdraw_npy_to_imagefile('quickdraw-npy', 'quickdraw-png')`，并说明 `subset` 可传类别名元组只转换感兴趣的类别（Quickdraw 全量 345 类约 37 GB，按需子集化很重要）。

[pytorch_ipynb/helper.py:L33-L41](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper.py#L33-L41)
准备工作：建输出目录；`os.listdir` 列出 `inpath` 下所有 `.npy`；若有 `subset`，用 `i.split('.npy')[0] in subset` 过滤——这就是「只转 10 类」的实现；一个 `.npy` 都没找到就抛 `ValueError`。

[pytorch_ipynb/helper.py:L45-L51](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper.py#L45-L51)
对每个文件：`label = (i.split('-')[-1]).split('.npy')[0]` 从文件名提取类别名；在 `outpath` 下建同名子目录；`np.load` 把整个数组堆读进内存（`X` 形状 `(N, 784)`）——这是全函数唯一一次全量载入，转完一个类别即可被下一个循环覆盖。

[pytorch_ipynb/helper.py:L53-L61](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper.py#L53-L61)
写出循环：逐行 `row.reshape(28, 28)` 还原成二维图像；文件名用 `'%s_%06d.%s' % (label, cnt, filetype)` 格式化（`lollipop_000123.png`，六位零填充编号）；`imageio.imwrite` 落盘，`cnt` 递增。`assert cnt < 1000000` 保证编号不超过六位数的表达范围。

回到 notebook 侧，看它如何调用这个工具并衔接 4.2 的流程：

[custom-data-loader-quickdraw.ipynb:L156-L164](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/custom-data-loader-quickdraw.ipynb#L156-L164)
notebook 位于 `pytorch_ipynb/mechanics/` 而 `helper.py` 在上一级 `pytorch_ipynb/`，所以先 `sys.path.insert(0, '..')` 再 `from helper import quickdraw_npy_to_imagefile`；调用时 `subset=label_dict.keys()` 只转换 `label_dict` 里选定的 10 个类别。转换完成后，图片目录交给 4.2 已精读的「遍历 → shuffle → 切分 → 写 CSV → QuickdrawDataset」流水线。

#### 4.3.4 代码实践

1. **实践目标**：不下载 37 GB 的 Quickdraw，用一个自造的迷你 `.npy` 完整走通 `quickdraw_npy_to_imagefile`，验证你对输出布局的理解。
2. **操作步骤**：在仓库根目录运行（示例代码，非仓库原有；`-I` 隔离解释器环境）：

   ```python
   # make_mini_npy.py —— 构造一个 8 行的假 Quickdraw 类别文件
   import os
   import numpy as np
   from torchvision import datasets

   mnist = datasets.MNIST(root='/tmp/data', train=False)   # 测试集，会自动下载
   X = mnist.data[:8].numpy().reshape(-1, 784)             # (8, 784) uint8

   os.makedirs('/tmp/mini-npy', exist_ok=True)
   np.save('/tmp/mini-npy/full_numpy_bitmap_mini-eight.npy', X)
   ```

   ```python
   # convert.py —— 调用仓库自带的转换函数
   import sys
   sys.path.insert(0, 'pytorch_ipynb')          # helper.py 所在目录
   from helper import quickdraw_npy_to_imagefile

   quickdraw_npy_to_imagefile(inpath='/tmp/mini-npy',
                              outpath='/tmp/mini-png',
                              subset=('mini-eight',))
   ```

   然后在 shell 里检查产物：`find /tmp/mini-png -type f | sort`。
3. **需要观察的现象**：`/tmp/mini-png/mini-eight/` 目录下出现 8 个 PNG，文件名从 `mini-eight_000000.png` 到 `mini-eight_000007.png`；用 `imageio.imread` 读一张，形状为 `(28, 28)`。
4. **预期结果**：目录名 `mini-eight` 来自文件名按 `-` 取最后一段再去扩展名（`full_numpy_bitmap_mini-eight.npy` → `mini-eight`）；编号从 000000 起、六位零填充。这个 8 张图的目录就是 4.2「按类别子目录」布局的微缩版，可直接接入综合实践的 Dataset。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`quickdraw_npy_to_imagefile` 是在训练时被调用，还是在训练之前？它跑一次之后，重复训练还要不要再跑？

**答案**：训练之前的一次性离线批处理。它把 `.npy` 数组堆物化成 PNG 图片目录，跑一次产物就留在磁盘上；之后每次训练都直接从图片目录 + 标签 CSV 出发（4.2 的流程），无需重新转换。这种「重预处理做一次、轻读取做千万次」的分层，与 4.1 的惰性读取是一体两面。

**练习 2**：为什么 `subset` 过滤用 `i.split('.npy')[0] in subset` 而不是 `i in subset`？

**答案**：`os.listdir` 返回的是**带扩展名的完整文件名**（如 `full_numpy_bitmap_lollipop.npy`），而 `subset` 里放的是**类别名**（如 `"lollipop"`）。`i.split('.npy')[0]` 剥掉扩展名后再与类别名比对才能命中。同理，标签提取也要先按 `-` 分段——类别名只是长文件名的一段。

**练习 3**：函数里 `X = np.load(j)` 把整个类别的数组堆（Quickdraw 每类最多 15 万行 × 784 字节 ≈ 110 MB）读进内存，为什么不担心？

**答案**：这是一次性批处理，且作用域只在 `for i, j in zip(...)` 的单次循环内——处理完一个类别，`X` 在下一轮被重新赋值，旧数组即可被垃圾回收。峰值内存只需容纳**一个类别**而非全部 345 类；这 Dataset 里的 `__getitem__` 不同（后者连一个类别都不整体载入，逐张读）。「按生命周期选内存策略」是两条代码共同体现的工程判断。

## 5. 综合实践

**任务：亲手造一个「自己的图片目录数据集」，走通「目录 → 清单 → Dataset → DataLoader → 可视化 8 张样本」全流程。**

不需要任何外部下载（torchvision 会自动下载 MNIST 作为原料）。以下为示例代码（非仓库原有），可在仓库根目录存成脚本或逐段在 notebook 中运行：

```python
# 第 1 步：从 MNIST 造一个「每类一个子目录」的 PNG 目录（模拟自有图片库）
import os
import numpy as np
import pandas as pd
import imageio
from torchvision import datasets

OUT = '/tmp/my_png_data'
mnist = datasets.MNIST(root='/tmp/data', train=True)

rows = []
for idx in range(1000):                        # 取前 1000 张
    img, label = mnist[idx]                    # PIL 图（datasets.MNIST 支持 transform=None 时返回 PIL）
    subdir = os.path.join(OUT, str(label))     # 类别名作子目录名：/tmp/my_png_data/7/
    os.makedirs(subdir, exist_ok=True)
    fname = f'{label}_{idx:06d}.png'
    img.save(os.path.join(subdir, fname))
    rows.append({'Path': f'{str(label)}/{fname}', 'Label': int(label)})

pd.DataFrame(rows).to_csv('/tmp/my_png_data/labels.csv', index=False)
```

```python
# 第 2 步：仿照 QuickdrawDataset 写自己的 Dataset（三要素 + transform）
import torch
from PIL import Image
from torch.utils.data import Dataset, DataLoader
from torchvision import transforms

class MyFolderDataset(Dataset):
    def __init__(self, csv_path, img_dir, transform=None):
        df = pd.read_csv(csv_path)
        self.img_dir = img_dir
        self.img_names = df['Path'].values      # 数组：按位置索引，安全
        self.y = df['Label'].values
        self.transform = transform

    def __getitem__(self, index):
        img = Image.open(os.path.join(self.img_dir, self.img_names[index]))
        if self.transform is not None:
            img = self.transform(img)
        return img, self.y[index]

    def __len__(self):
        return self.y.shape[0]

transform = transforms.Compose([transforms.Grayscale(),   # MNIST 本就是灰度，这里演示链式变换
                                transforms.ToTensor()])

dataset = MyFolderDataset('/tmp/my_png_data/labels.csv', OUT, transform)
loader = DataLoader(dataset=dataset, batch_size=32, shuffle=True, num_workers=0)
print(len(dataset), len(loader))               # 预期：1000 32（⌈1000/32⌉=32，见 u4-l1 批数公式）
```

```python
# 第 3 步：取一个 batch，可视化 8 张样本（permute 回 HWC 给 imshow）
import matplotlib.pyplot as plt

x, y = next(iter(loader))
print(x.shape, y.dtype)                        # 预期：torch.Size([32, 1, 28, 28]) torch.int64

fig, axes = plt.subplots(2, 4, figsize=(8, 4))
for k, ax in enumerate(axes.flat):
    ax.imshow(x[k].permute(1, 2, 0).squeeze(), cmap='binary')
    ax.set_title(f'label={int(y[k])}', fontsize=9)
    ax.axis('off')
plt.tight_layout()
plt.show()
```

**检查点**（做完后逐项自查）：

1. `len(dataset)` 与 CSV 行数一致；`dataset[0]` 返回 `(C×H×W 张量, int 标签)`，张量取值在 \([0,1]\)。
2. `y.dtype` 是 `torch.int64`——满足 `CrossEntropyLoss` 的要求（u2-l2 结论在自定义数据上依然成立）。
3. 8 张子图的标题与图像内容一致（目录 → 标签的映射没断）。
4. 思考题：把 `shuffle=True` 改成 `False`，前 8 张子图的标签分布会怎么变化？为什么训练时要 True？（提示：4.2.2 的「数据是否按标签排序」。）
5. 若把 `transforms.Grayscale()` 去掉、改用 `transforms.RandomRotation(30)`，同一个 `dataset[0]` 连续取两次会得到相同的图吗？——这为下一讲（transform 与数据增强）埋下伏笔。

以上运行结果待本地验证。

## 6. 本讲小结

- **Dataset 三要素是一份契约**：`__init__`（读清单、存配置）、`__getitem__`（拼路径 → 读图 → transform → 返回 `(img, label)`）、`__len__`（报规模）。DataLoader 只依赖这三个方法，Sampler 与加载逻辑完全不用改。
- **惰性读取**：图片在 `__getitem__` 里才从磁盘读，内存占用与数据集规模无关；transform 同样逐样本应用，这是 train/valid 挂不同变换的前提。
- **「图片目录 + 标签清单」是自定义视觉数据集的通用形态**：三个 notebook 是同一模板的三次实例化，差异只在标签来源（现成 CSV / 自带属性文件 / 遍历目录自生成）、目录摆放（平铺 / 每类一子目录）与划分策略（顺序切 / shuffle 后切）。
- **划分前先问「数据是否按标签排序」**：CelebA 无序可顺序切 80/20；Quickdraw 按类别连续生成，必须 `shuffle_arrays_unison` 同步打乱后再 70/10/20。
- **`quickdraw_npy_to_imagefile` 是一次性离线批处理**：列 `.npy` → 从文件名提取类别 → 建子目录 → 逐行 `reshape(28, 28)` 写 PNG，产出「每类一子目录」的标准布局，供 Dataset 消费。
- **两个易踩的坑**：pandas Series 的 `[]` 是按标签定位，安全写法是 `.values` / `df.index.values` 转按位置的 NumPy 数组；`__getitem__` 必须返回单个样本，标签要索引到 `self.y[index]`。

## 7. 下一步学习建议

- 下一讲（u4-l3）进入 **transforms 与标准化**：本讲的 transform 只用了 `Grayscale`/`ToTensor` 两个「确定性」变换，下一讲会讲 `transforms.Compose` 的常用组合、用训练集统计量做逐通道标准化、以及 `UnNormalize` 还原图像做可视化——正好回答综合实践检查点 5 留下的问题。
- 想看同一模板的更多变体，可通读 `pytorch_ipynb/mechanics/` 下的 `custom-data-loader-csv.ipynb`（特征存 CSV 而非图片）、`custom-data-loader-svhn.ipynb` / `custom-data-loader-fashionmnist.ipynb`（从 `.mat` / 原始二进制文件读取）与 `custom-data-loader-afad.ipynb`（多任务标签：年龄 + 性别）。
- 回顾对照：u2-l2 的 Iris `DatasetWrapper`（`predict` 方法适配 mlxtend）与本讲的三个类，体会「三要素不变、外围按需扩展」的模式；u4-l1 的 `helper_data.py` 则展示了如何把本讲的手工管线封装成可复用的 `get_dataloaders_*` 函数。
- 若你的数据天然就是「每类一个子目录」，可以进一步了解 `torchvision.datasets.ImageFolder`（背景知识，仓库未使用）——它就是本讲手写 Dataset 的官方泛化版；手写一遍之后再用它，会更能理解它替你做了什么。
