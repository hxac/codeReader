# 数据集、DataLoader 与验证集划分

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `Dataset`、`DataLoader`、`Sampler` 三者各自负责什么，以及 `shuffle`、`drop_last`、`num_workers` 这些常用参数的行为。
2. 精读 `helper_data.py` 中 `get_dataloaders_mnist` / `get_dataloaders_cifar10` 的实现：理解它如何用两个索引区间加 `SubsetRandomSampler`，从同一份训练数据中「切」出验证集，并组织成 train / valid / test 三个 loader。
3. 对比 `validation-splits.ipynb` 中划分验证集的两种方法（`Subset` 与 `SubsetRandomSampler`），明白共享 helper 为什么选择后者。
4. 理解 `dataloader-nesting.ipynb` 的实验结论：为什么在训练循环内部再嵌套一层对同一 DataLoader 的迭代是安全的，以及真正会出事的写法（共享同一个显式迭代器）长什么样。

## 2. 前置知识

- **三个数据集的角色**。在第 2 单元我们只用过训练集和测试集：训练集用来更新权重，测试集只在最后评估一次。但反复调超参（学习率、层数、epoch 数……）时，如果每调一次就看一眼测试集，测试集就间接参与了选择，成绩会被高估。**验证集**就是为「反复调参与早停」准备的替身：测试集留到最后只碰一次。
- **MNIST / CIFAR-10 没有现成的验证集**。`torchvision` 只给出 train / test 两份，验证集需要自己从训练集里再切一块出来——这正是本讲的主题。
- **Dataset 与 DataLoader 的分工**（u2-l2 已初见）：`Dataset` 只回答「第 i 个样本是什么」（`__getitem__`）和「一共有多少个」（`__len__`）；`DataLoader` 负责批量取、打乱、多进程加载。中间还藏着一个本讲的主角——**Sampler**，它决定「取哪些索引、按什么顺序」。
- **iterable 与 iterator 的区别**（4.3 节会用）：`for x in obj` 的本质是先调用 `iter(obj)` 拿到一个**迭代器**（iterator），再不断调用 `next()` 取值。可迭代对象（iterable，比如 DataLoader）可以反复交出**新的、彼此独立**的迭代器；迭代器自己则是有状态的游标，用完即弃。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pytorch_ipynb/helper_data.py](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_data.py) | 共享数据模块：`get_dataloaders_mnist` / `get_dataloaders_cifar10`（本讲精读）与 `UnNormalize`（u4-l3 再讲） |
| [pytorch_ipynb/mechanics/validation-splits.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/validation-splits.ipynb) | 机制实验：演示 `Subset` 与 `SubsetRandomSampler` 两种验证集划分方法，并验证打乱行为 |
| [pytorch_ipynb/mechanics/dataloader-nesting.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/dataloader-nesting.ipynb) | 机制实验：验证嵌套迭代 DataLoader 时，内外两层循环互不干扰 |

阅读提示：`.ipynb` 的永久链接行号指向其原始 JSON 文本中的行，在 GitHub 上打开链接后定位到对应 cell 即可对照。

## 4. 核心概念与源码讲解

### 4.1 DataLoader 与 Sampler

#### 4.1.1 概念说明

把数据管线想象成一条流水线，三个角色各管一段：

- **Dataset**：仓库保管员。知道第 `i` 件货是什么（返回 `(图片, 标签)`）、总共有多少件。
- **Sampler**：领料单生成器。产出一份「索引顺序表」，决定这一轮（epoch）按什么顺序取哪些样本。
- **DataLoader**：传送带。按领料单从仓库取货，攒成一批一批（batch），必要时用多个子进程加速。

关键认知是：**打乱不是 DataLoader 亲自做的，而是委托给 Sampler**。`DataLoader` 参数中的 `shuffle=True` 只是一个快捷方式，等价于「帮我装一个随机 Sampler」；而 `sampler=` 参数让你自带领料单。两者**互斥**——同时传 `shuffle` 和 `sampler`，PyTorch 会直接抛 `ValueError`。后面会看到 `helper_data.py` 严格遵守这条规则：用 sampler 的分支不传 shuffle，不用 sampler 的分支才传 `shuffle=True`。

另外两个常用参数：

- `drop_last=True`：丢掉最后不足一个 batch 的零头，保证每个 batch 尺寸一致。训练集常开（batch 尺寸稳定对 BatchNorm 等层的统计更友好），验证/测试集不需要（评估只求遍历全部样本）。
- `num_workers`：加载样本的子进程数，`0` 表示在主进程里同步加载（无并行开销，最稳定），共享 helper 默认取 `0`。

#### 4.1.2 核心流程

DataLoader 每被迭代一轮（一个 epoch），内部大致发生：

```text
for 每个 batch:
    索引表 = sampler 产出的下一批索引（或 shuffle=True 时由内置随机采样器产出）
    样本列表 = [ dataset[i] for i in 索引表 ]     # 逐样本应用 transform
    batch = collate(样本列表)                      # 堆叠成张量
    yield batch
```

batch 数量由样本数与 batch_size 决定。设某 loader 的样本数为 \( N \)，batch 大小为 \( B \)：

- 不开 `drop_last`：批数为 \( \lceil N/B \rceil \)，最后一批可能小于 \( B \)；
- 开 `drop_last`：批数为 \( \lfloor N/B \rfloor \)。

这个公式在本讲的实践环节会反复用来核对 `len(loader)`。

#### 4.1.3 源码精读

先看 `helper_data.py` 的导入：同时引入了 `DataLoader` 与 `SubsetRandomSampler`，前者是传送带，后者是本讲第二位主角。

[pytorch_ipynb/helper_data.py:L1-L6](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_data.py#L1-L6)
导入 `DataLoader`、`SubsetRandomSampler`、`datasets`、`transforms` 等数据管线所需的全部构件。

再看 `get_dataloaders_mnist` 中**不划分验证集**的分支，它是「最小 DataLoader 用法」的样板：

[pytorch_ipynb/helper_data.py:L72-L82](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_data.py#L72-L82)
训练 loader：`drop_last=True` 丢零头、`shuffle=True` 每个 epoch 重新打乱；测试 loader：`shuffle=False`，评估顺序固定、结果可复现。注意训练与测试的两个 loader 一个开 shuffle 一个不开——打乱只为训练服务。

以及函数签名与默认 transform：

[pytorch_ipynb/helper_data.py:L29-L38](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_data.py#L29-L38)
`get_dataloaders_mnist(batch_size, num_workers=0, validation_fraction=None, train_transforms=None, test_transforms=None)`。若调用者不传 transform，训练与测试统一退化为 `transforms.ToTensor()`（只把像素压到 0–1，不含任何随机增强）。

#### 4.1.4 代码实践

1. **实践目标**：验证「批数公式」与 shuffle/drop_last 的实际行为。
2. **操作步骤**：在 `pytorch_ipynb/` 目录下新建一个脚本或 notebook cell（示例代码，非仓库原有）：

   ```python
   import sys; sys.path.insert(0, '.')   # 让 helper_data 可被导入
   from helper_data import get_dataloaders_mnist

   train_loader, valid_loader, test_loader = get_dataloaders_mnist(
       batch_size=64, validation_fraction=0.1)

   print(len(train_loader), len(valid_loader), len(test_loader))
   images, labels = next(iter(test_loader))
   print(images.shape, labels.shape)
   ```

3. **需要观察的现象**：三个数字分别是多少；`images.shape` 是否为 `torch.Size([64, 1, 28, 28])`；最后一个测试 batch 是否可能小于 64。
4. **预期结果**：划分后训练样本 54000、验证 6000、测试 10000（推导见 4.2.3）。按公式：`len(train_loader)` = ⌊54000/64⌋ = 843（drop_last），`len(valid_loader)` = ⌈6000/64⌉ = 94，`len(test_loader)` = ⌈10000/64⌉ = 157。首次运行会自动下载 MNIST 到 `pytorch_ipynb/data/`。以上为按公式与源码推得的结果，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `helper_data.py` 中测试 loader 用 `shuffle=False`，而训练 loader 要打乱？

**答案**：打乱是为了让每个 batch 的梯度更接近全局梯度的无偏估计，避免按固定顺序学习出「顺序依赖」，这是训练期的需求；评估只需要遍历一遍全部样本求指标，顺序不影响准确率的分子分母，固定顺序反而让结果可复现、便于比对。

**练习 2**：如果把 `shuffle=True` 和 `sampler=...` 同时传给 `DataLoader` 会发生什么？

**答案**：PyTorch 会抛出 `ValueError`（二者互斥）。`shuffle=True` 的含义就是「使用内置随机采样器」，与自带的 `sampler` 冲突。`helper_data.py` 的两个分支（L66-L70 用 sampler 不传 shuffle，L73-L77 传 shuffle 不用 sampler）正是对这个约束的正确遵守。

**练习 3**：`num_workers=0` 与 `num_workers=4` 的区别是什么？

**答案**：`0` 表示在主进程同步加载样本，简单稳定、适合调试；`4` 表示用 4 个子进程并行做「取样本 + transform + 拼批」，能减少 GPU 等数据的时间，但有进程启动与进程间传输的额外开销，且要求代码可被子进程安全导入（Windows/notebook 环境下更易踩坑）。

### 4.2 验证集划分：Subset 与 SubsetRandomSampler 两种方法

#### 4.2.1 概念说明

从 60000 张训练图中划出一块当验证集，直觉上有两条路：

- **方法 A：`Subset`（先切 dataset）**。把一个 dataset 对象按索引列表包成两个小 dataset：`Subset(dataset, train_indices)` 与 `Subset(dataset, valid_indices)`。之后照常 `DataLoader(..., shuffle=True)`。缺点：两个子集共享同一个 dataset 对象，因此**共用同一套 transform**——训练集想做随机裁剪、验证集想要确定性变换时就拧巴了。
- **方法 B：`SubsetRandomSampler`（后挂 sampler）**。建两个指向同一份底层数据的 dataset 实例（各挂各的 transform），再用两个 sampler 分别圈定各自的索引区间。DataLoader 迭代时，sampler 会**在不放回的前提下随机打乱自己的索引列表**，等价于「子集内每个 epoch 自动重洗」。

`validation-splits.ipynb` 的作者把方法 B 的优势说得很直白：当训练与验证子集需要**不同的变换方法**时，`SubsetRandomSampler` 是更方便的方案。共享 helper（`helper_data.py`）采用的正是方法 B——这也是为什么它为 MNIST 建了两个 `train=True` 的 dataset：`train_dataset` 挂 `train_transforms`（可以带随机增强），`valid_dataset` 挂 `test_transforms`（确定性）。

#### 4.2.2 核心流程

`helper_data.py` 的划分逻辑是**按索引区间切**：设训练集总数为 \( N \)（MNIST 为 60000，CIFAR-10 为 50000），验证比例为 \( f \)，则

\[
\text{num} = \lfloor f \cdot N \rfloor, \qquad
\text{train\_indices} = [0,\; N-\text{num}), \qquad
\text{valid\_indices} = [N-\text{num},\; N)
\]

即「**最后 num 个样本当验证集**」，两段区间不重叠、恰好铺满，且划分完全确定（同一份代码每次运行切出的验证集相同）。注意每个 epoch 内部的顺序仍会被 `SubsetRandomSampler` 随机打乱——「划分确定」与「训练顺序随机」是两回事。

随后的 loader 组织：

```text
train_dataset  (train=True,  train_transforms) ──sampler=train_indices──▶ train_loader (drop_last=True)
valid_dataset  (train=True,  test_transforms ) ─sampler=valid_indices──▶ valid_loader
test_dataset   (train=False, test_transforms ) ──shuffle=False──────────▶ test_loader
```

一个容易忽略的细节：`valid_dataset` 建立时**没有传 `download=True`**（helper_data.py L45-L47）。它能工作的前提是 `train_dataset` 先一步带着 `download=True` 把数据落了盘——两个 dataset 指向同一份磁盘文件。若把创建顺序反过来或删掉前者的 download，在无缓存的环境里就会报「文件不存在」。

#### 4.2.3 源码精读

**（1）`validation-splits.ipynb`：方法 A —— Subset**

[pytorch_ipynb/mechanics/validation-splits.ipynb:L210-L220](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/validation-splits.ipynb#L210-L220)
先 `valid_indices = torch.arange(0, 1000)`、`train_indices = torch.arange(1000, 60000)`（注意：这个 notebook 与 helper_data 的方向相反，它取**前** 1000 个做验证），再用 `Subset(train_and_valid, indices)` 把同一个 dataset 包成两个子集。

[pytorch_ipynb/mechanics/validation-splits.ipynb:L229-L238](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/validation-splits.ipynb#L229-L238)
为两个 Subset 各建 DataLoader：训练的 `shuffle=True`，验证的不打乱。

顺带一个「以代码为准」的小案例：notebook 的 markdown（[L190-L192](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/validation-splits.ipynb#L190-L192)）说「`Subset` 方法会在每个 epoch 前自动打乱数据」，但读 L229 的代码可见，真正干活的是 DataLoader 的 `shuffle=True`——`Subset` 本身只是个按索引取样的包装器，不含任何随机逻辑。文字描述偷懒了，代码不会。

**（2）`validation-splits.ipynb`：方法 B —— SubsetRandomSampler**

[pytorch_ipynb/mechanics/validation-splits.ipynb:L351-L365](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/validation-splits.ipynb#L351-L365)
构造两个索引区间与对应的 `SubsetRandomSampler`；同时定义两套不同的 transform：`training_transform` 带 `RandomCrop`（随机增强），`valid_transform` 用 `CenterCrop`（确定性）——这正是方法 B 的卖点。

[pytorch_ipynb/mechanics/validation-splits.ipynb:L369-L384](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/validation-splits.ipynb#L369-L384)
建立三个 dataset：`train_dataset` 与 `valid_dataset` 都是 `train=True`（同一份底层数据），但分别挂 `training_transform` 与 `valid_transform`；`test_dataset` 用 `train=False` 挂 `valid_transform`。

[pytorch_ipynb/mechanics/validation-splits.ipynb:L386-L399](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/validation-splits.ipynb#L386-L399)
三个 DataLoader：训练与验证的都通过 `sampler=` 圈定各自子集（都不传 shuffle），测试的 `shuffle=False`。

[pytorch_ipynb/mechanics/validation-splits.ipynb:L306-L319](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/validation-splits.ipynb#L306-L319)
可复现性检查：两段代码都在迭代前 `torch.manual_seed(123)`，notebook 记录的输出显示两个 epoch 打出的标签序列**完全相同**（`tensor([1, 0, 3, 7, 0, 7, 5, 6, 8, 3])` 各出现一次）；而不设种子时（L277-L288）两轮序列不同。结论：`SubsetRandomSampler` 的随机打乱由全局随机数生成器驱动，可以被 `set_all_seeds` 一类手段固定（呼应 u1-l3）。

**（3）`helper_data.py`：生产版实现**

[pytorch_ipynb/helper_data.py:L40-L51](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_data.py#L40-L51)
一次性建立三个 dataset：`train_dataset`（train=True，train_transforms，download=True）、`valid_dataset`（train=True，**test_transforms**，不带 download）、`test_dataset`（train=False，test_transforms）。验证集用确定性 transform，保证每个 epoch 看到的验证样本一致，指标才可比。

[pytorch_ipynb/helper_data.py:L53-L59](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_data.py#L53-L59)
核心三行：`num = int(validation_fraction * 60000)`，`train_indices = torch.arange(0, 60000 - num)`，`valid_indices = torch.arange(60000 - num, 60000)`，再各自包一层 `SubsetRandomSampler`。两个 sampler 各自独立打乱自己的区间。

[pytorch_ipynb/helper_data.py:L61-L70](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_data.py#L61-L70)
valid_loader 不丢零头；train_loader `drop_last=True` 且通过 `sampler=train_sampler` 圈定训练子集（无 shuffle 参数）。

[pytorch_ipynb/helper_data.py:L84-L87](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_data.py#L84-L87)
返回值的「重载」：传了 `validation_fraction` 返回三元组 `(train_loader, valid_loader, test_loader)`，没传则只返回二元组 `(train_loader, test_loader)`。调用方解包时要与之匹配。

[pytorch_ipynb/helper_data.py:L114-L131](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_data.py#L114-L131)
`get_dataloaders_cifar10` 是同一模板的复制：仅把 60000 换成 50000、dataset 类换为 `datasets.CIFAR10`。读懂 MNIST 版即读懂了它。

#### 4.2.4 代码实践

1. **实践目标**：亲眼核对「sampler 圈定的样本数」与「loader 批数」的换算关系，弄清 `len(loader)` 为什么不等于样本数。
2. **操作步骤**（示例代码）：

   ```python
   from helper_data import get_dataloaders_mnist
   train_loader, valid_loader, test_loader = get_dataloaders_mnist(
       batch_size=64, validation_fraction=0.1)

   n_train = len(train_loader.sampler)      # 54000
   n_valid = len(valid_loader.sampler)      # 6000
   n_test  = len(test_loader.dataset)       # 10000
   print(n_train, n_valid, n_test)
   print(n_train + n_valid == len(train_loader.dataset))   # True：两段索引铺满 60000
   ```

3. **需要观察的现象**：`len(loader.sampler)` 与 `len(loader)` 的差别；最后把 `batch_size` 改成 7（不整除）再跑一次。
4. **预期结果**：`len(loader.sampler)` 直接给出子集样本数；`len(loader)` 是批数（54000/64 → 843，因 drop_last 取整）。batch_size=7 时训练批数为 ⌊54000/7⌋ = 7714，验证批数为 ⌈6000/7⌉ = 858，且验证的最后一个 batch 只有 6000 − 857×7 = 1 个样本。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`helper_data.py` 的划分里，同一张图片有没有可能既出现在训练 loader 又出现在验证 loader 中？

**答案**：不可能。`train_indices = [0, 60000-num)` 与 `valid_indices = [60000-num, 60000)` 是两个首尾相接、不重叠的区间，两段长度之和恰为 60000。`SubsetRandomSampler` 只会在自己的索引列表内打乱，不会越界取样。（但注意：若数据增强是随机生成新样本的变换，训练子集「看过的图片变体」与验证集原图仍可能内容相近——这是另一层面的信息泄漏，划分本身没有问题。）

**练习 2**：既然 `Subset` 更简单，为什么 `helper_data.py` 选了 `SubsetRandomSampler`？

**答案**：为了给训练与验证子集挂**不同的 transform**。训练集可以配随机裁剪/翻转等增强（`train_transforms`），验证与测试集必须用确定性变换（`test_transforms`）保证评估可复现。`Subset` 包出的两个子集共享同一个 dataset 实例、同一套 transform；而方法 B 为验证集单独建一个挂 `test_transforms` 的 dataset 实例，再由 sampler 圈定范围，两全其美。

**练习 3**：`valid_dataset` 的构造（L45-L47）没有写 `download=True`，它为什么不会报错？这依赖什么前提？

**答案**：因为 `train_dataset`（L40-L43）先带着 `download=True` 执行，数据已下载到 `root='data'`；`valid_dataset` 指向同一磁盘路径，直接读现成文件。前提是**创建顺序不能颠倒**、且首次运行前 `data/` 里没有残留损坏的缓存——这也是「读源码要注意变量/对象的来源与先后依赖」的又一例。

### 4.3 嵌套迭代 DataLoader：独立迭代器与真正的陷阱

#### 4.3.1 概念说明

一个常见的担忧：训练循环里已经 `for batch in train_loader` 了，如果在循环体内某个函数（比如估计当前损失的小函数）里**再次**迭代同一个 `train_loader`，会不会把外层循环「吃掉」几批数据、导致外层跳批或错乱？

`dataloader-nesting.ipynb` 用一个最小实验回答：**不会**。原因回到 4.1 节的 iterable/iterator 之分：

- `DataLoader` 是**可迭代对象**，不是迭代器。它不保存「迭代到哪了」的状态。
- 每写一个 `for ... in loader`，Python 就调用一次 `loader.__iter__()`，**新建一个独立的迭代器**。外层循环拿着迭代器甲，内层拿着迭代器乙，各走各的游标，互不影响。
- 内层循环 `break` 只丢弃了迭代器乙，甲原地不动；外层的下一个 batch 紧接在它上次停的地方。

真正会踩坑的写法是：把 `it = iter(loader)` 提出来，让内外两层**共享同一个迭代器对象**——这时两层消费同一条数据流，内层吃掉的批次外层就再也见不到了。DataLoader 本身没这个坑，坑在 Python 迭代协议的用法上。

#### 4.3.2 核心流程

notebook 的实验设计（一个数字Dataset，1~10，batch_size=1）：

```text
main loop:  for i in integer_loader:        # 迭代器甲：依次取 1..10
    nested:     for j in integer_loader:    # 每次进入都新建迭代器乙：从 1 开始
                    if j >= 5: break        # 乙走到 5 即弃
```

若嵌套迭代会污染外层状态，输出应当是交错的乱序；若二者独立，输出应当是「main 1 → nested 1..5 → main 2 → nested 1..5 → …… → main 10」的规整结构。

#### 4.3.3 源码精读

[pytorch_ipynb/mechanics/dataloader-nesting.ipynb:L38](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/dataloader-nesting.ipynb#L38)
notebook 的结论陈述：在单独函数里迭代一个 DataLoader **不会**影响主训练循环中它的状态；DataLoader 是可被多次独立迭代的 iterable，每次迭代都从头开始（若 shuffle=True，每轮顺序还各不相同）。

[pytorch_ipynb/mechanics/dataloader-nesting.ipynb:L116-L144](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/dataloader-nesting.ipynb#L116-L144)
完整实验代码：定义返回整数的 `IntegerDataset`（1 到 10），`calc_loss(data_loader, iters)` 在内部**再次**迭代 `integer_loader` 并在取到 `iters` 时 break；主循环逐个打印 `main loop`，每步调用一次 `calc_loss(integer_loader, iters=5)`。

[pytorch_ipynb/mechanics/dataloader-nesting.ipynb:L52-L110](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/dataloader-nesting.ipynb#L52-L110)
notebook 记录的输出：规整的「`main loop: 1` → `nested loop: 1..5` → `main loop: 2` → `nested loop: 1..5` → … → `main loop: 10`」。外层完整走完 1..10，内层每次都从 1 重新开始——证明两层迭代持有各自独立的迭代器。

需要留意的代价：内层每被调用一次就把**整条数据流重新迭代**（本例因 break 提前结束，只走到 5）。若 `calc_loss` 不设上限地完整遍历 train_loader，每个训练 batch 都触发一整轮数据扫描，开销是乘法级的。工程上通常给内层加 `max_batches` 一类的早停（本例的 `iters` 参数就是这个角色），或改用迭代器 `itertools.islice`。

#### 4.3.4 代码实践

1. **实践目标**：亲手复现「安全嵌套」，再构造「共享迭代器」的反例，对比两者输出。
2. **操作步骤**（示例代码，第一段复刻自 notebook，第二段是本讲设计的反例）：

   ```python
   from torch.utils.data import Dataset, DataLoader

   class IntegerDataset(Dataset):
       def __init__(self, start, end):
           self.data = list(range(start, end + 1))
       def __len__(self):
           return len(self.data)
       def __getitem__(self, idx):
           return self.data[idx]

   loader = DataLoader(IntegerDataset(1, 6), batch_size=1, shuffle=False)

   # 实验 1：直接嵌套 for（各自调用 iter()，互不干扰）
   for i in loader:
       for j in loader:
           break                      # 内层只取 1 个
       print("main:", i.item(), "nested saw:", j.item())

   # 实验 2：把迭代器提出来共享（危险写法）
   it = iter(loader)
   for i in it:
       for j in it:
           break
       print("shared-iterator main:", i.item(), "nested saw:", j.item())
   ```

3. **需要观察的现象**：实验 1 中 `main` 是否完整打印 1..6 且 `nested saw` 恒为 1；实验 2 中 `main` 还能打印几个数。
4. **预期结果**：实验 1 输出 6 行，main 依次 1..6。实验 2 中外层与内层消费同一条流：i=1 时内层取走 2，外层下一个拿到 3，内层又取走 4……最终 `main` 只打印 1、3、5 共 3 行便终止——数据被两层「抢食」，外层永远丢失了一半 batch。这正是 4.3.1 说的真陷阱。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `for x in loader` 写两层也不会互相干扰？

**答案**：`for` 语句每次都隐式调用 `iter(loader)`，DataLoader 的 `__iter__` 会返回一个**新建的**迭代器对象（维护自己的批次游标，必要时还会重新生成采样顺序）。内外两层持有的是两个不同迭代器，状态彼此隔离。

**练习 2**：`shuffle=True` 时，内外两层迭代看到的样本顺序一样吗？

**答案**：不一样（大概率）。每个新迭代器都会重新生成一遍随机顺序（由当时的全局随机数生成器决定）。因此「嵌套估计损失」取到的前几批与外层训练正在用的前几批通常不同——如果业务上要求内外层看同样的子序列，需要固定种子或改用显式切片的索引列表，而不是依赖巧合。

**练习 3**：既然嵌套安全，能否在训练循环里对 `train_loader` 做无限次完整遍历而不影响正确性？

**答案**：正确性上安全（外层不会丢 batch），但性能上是灾难：每个训练 step 都触发一整轮数据加载，复杂度变成「批数 × 批数」。实用做法是像 notebook 的 `calc_loss(..., iters)` 那样限制内层只取固定几批，或把估计损失放到独立的、更小的 loader 上。

## 5. 综合实践

**任务：仿照 `get_dataloaders_mnist`，为 FashionMNIST 实现支持 `validation_fraction` 的 `get_dataloaders_fashionmnist`，并验证三个 loader 的样本数账目正确。**

FashionMNIST 与 MNIST 规模完全相同（训练 60000、测试 10000，28×28 灰度图、10 类），因此可以逐行迁移 `helper_data.py` 的模板。

**步骤 1**：在 `pytorch_ipynb/` 目录下新建文件 `fashion_loader.py`（新文件，不改动任何现有源码），写入（示例代码）：

```python
import torch
from torchvision import datasets
from torchvision import transforms
from torch.utils.data import DataLoader
from torch.utils.data import SubsetRandomSampler


def get_dataloaders_fashionmnist(batch_size, num_workers=0,
                                 validation_fraction=None,
                                 train_transforms=None,
                                 test_transforms=None):
    if train_transforms is None:
        train_transforms = transforms.ToTensor()
    if test_transforms is None:
        test_transforms = transforms.ToTensor()

    train_dataset = datasets.FashionMNIST(root='data', train=True,
                                          transform=train_transforms,
                                          download=True)
    valid_dataset = datasets.FashionMNIST(root='data', train=True,
                                          transform=test_transforms)
    test_dataset = datasets.FashionMNIST(root='data', train=False,
                                         transform=test_transforms)

    if validation_fraction is not None:
        num = int(validation_fraction * 60000)
        train_indices = torch.arange(0, 60000 - num)
        valid_indices = torch.arange(60000 - num, 60000)

        train_sampler = SubsetRandomSampler(train_indices)
        valid_sampler = SubsetRandomSampler(valid_indices)

        valid_loader = DataLoader(dataset=valid_dataset,
                                  batch_size=batch_size,
                                  num_workers=num_workers,
                                  sampler=valid_sampler)
        train_loader = DataLoader(dataset=train_dataset,
                                  batch_size=batch_size,
                                  num_workers=num_workers,
                                  drop_last=True,
                                  sampler=train_sampler)
    else:
        train_loader = DataLoader(dataset=train_dataset,
                                  batch_size=batch_size,
                                  num_workers=num_workers,
                                  drop_last=True,
                                  shuffle=True)

    test_loader = DataLoader(dataset=test_dataset,
                             batch_size=batch_size,
                             num_workers=num_workers,
                             shuffle=False)

    if validation_fraction is None:
        return train_loader, test_loader
    else:
        return train_loader, valid_loader, test_loader
```

**步骤 2**：在同一目录验证账目（示例代码）：

```python
from fashion_loader import get_dataloaders_fashionmnist

train_loader, valid_loader, test_loader = get_dataloaders_fashionmnist(
    batch_size=100, validation_fraction=0.2)

# 1) 三个 loader 的样本数
n_train = len(train_loader.sampler)
n_valid = len(valid_loader.sampler)
n_test = len(test_loader.dataset)
print(n_train, n_valid, n_test)

# 2) 账目断言：train+valid 恰好铺满 60000，且两段索引无交集
assert n_train + n_valid == 60000
assert n_train + n_valid + n_test == 70000
train_idx = set(train_loader.sampler.indices.tolist())
valid_idx = set(valid_loader.sampler.indices.tolist())
assert train_idx.isdisjoint(valid_idx)

# 3) 批数与公式对照（drop_last 只作用于训练）
assert len(train_loader) == 48000 // 100
assert len(valid_loader) == -(-12000 // 100)   # 向上取整
assert len(test_loader) == -(-10000 // 100)

# 4) 两个 loader 分到的 transform 是否不同源
x, _ = next(iter(valid_loader))
print(x.shape, x.min(), x.max())
```

**步骤 3**：把 `validation_fraction` 改为 `None` 再调用一次，确认返回值退化成二元组；顺手 `break` 掉一个训练循环，打印一个 batch 的形状。

**预期结果**（按源码与公式推得，待本地验证）：

- 步骤 2 打印 `48000 12000 10000`；三条 assert 全部通过；`x.shape` 为 `torch.Size([100, 1, 28, 28])`，`ToTensor` 下 `x.min()≥0`、`x.max()≤1`。
- 步骤 3 返回 `(train_loader, test_loader)` 两个对象，训练 batch 形状 `[100, 1, 28, 28]`。
- 首次运行会把 FashionMNIST 下载到 `pytorch_ipynb/data/`（约几十 MB）；离线环境下需提前放置数据。

**思考题**：如果 FashionMNIST 的训练集不是 60000 而是别的数字（比如像 CIFAR-10 那样是 50000），你的函数要改哪几处？——硬编码的 `60000` 出现在 `num`、`train_indices`、`valid_indices` 三行，共四处；把它们参数化成 `n = len(train_dataset)` 是更不易错的写法。

## 6. 本讲小结

- `Dataset` 管单样本存取，`Sampler` 管「取哪些索引、什么顺序」，`DataLoader` 管批量拼装与多进程加载；`shuffle=True` 只是「用内置随机采样器」的快捷方式，与 `sampler=` 互斥。
- `helper_data.py` 用两个首尾相接的索引区间（`[0, N-num)` 与 `[N-num, N)`）加两个 `SubsetRandomSampler`，从同一份训练数据切出验证集：划分确定、区区间不重叠，epoch 内顺序仍随机。
- 为了让训练子集用可随机增强的 `train_transforms`、验证/测试子集用确定性的 `test_transforms`，helper 为同一份底层数据建了两个 dataset 实例——这是它选 `SubsetRandomSampler` 而非更简单的 `Subset` 的原因；`valid_dataset` 不带 `download=True`，依赖 `train_dataset` 先落盘。
- 训练 loader `drop_last=True` 丢零头、批数取 ⌊N/B⌋；验证/测试不丢、取 ⌈N/B⌉；`len(loader)` 是批数，样本数要看 `len(loader.sampler)` 或 `len(loader.dataset)`。
- DataLoader 是可迭代对象而非迭代器：每个 `for` 都新建独立迭代器，因此嵌套迭代互不干扰（`dataloader-nesting.ipynb` 实验证明）；真正的陷阱是共享 `iter(loader)` 返回的同一个迭代器对象，内外层会互相吞 batch。

## 7. 下一步学习建议

- 下一讲 **u4-l2 自定义 Dataset 加载自己的数据**：把本讲的「torchvision 现成 dataset」换成自己写的 `Dataset` 子类（`__init__` / `__getitem__` / `__len__` 三要素），加载按类别存放的图片目录，DataLoader 与 Sampler 的机制原封不动地复用。
- 若想继续深挖 transform 与「训练/测试两套变换」的细节，可先跳读 **u4-l3 图像变换与标准化**，其中会用到本讲 `helper_data.py` 里的 `UnNormalize`。
- 想验证「打乱可被种子固定」的读者，可结合 **u4-l4 公共训练评估工具链与确定性** 中 `set_all_seeds` / `set_deterministic` 的源码，回看 `validation-splits.ipynb` L306-L319 的种子实验。
- 直接阅读建议：`pytorch_ipynb/helper_data.py` 全文不足 150 行，通读一遍并对照本讲 4.2.3 的逐段精读，是性价比最高的复习。
