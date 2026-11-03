# u1-l3 共享 helper 模块导览

## 1. 本讲目标

上一讲我们运行了仓库中最简单的 perceptron notebook，认识了「Imports → Dataset/Settings → Model → Training → Evaluation」五段式结构。但你如果翻看较新的 notebook（例如 `cnn/` 目录下的 MobileNet 系列），会发现它们不再把所有代码写在 notebook 里，而是从几个 `helper_*.py` 文件导入现成函数。

学完本讲，你应该能够：

1. 说出 `pytorch_ipynb/` 下 6 个 helper 模块各自的职责，以及每个模块提供哪些函数。
2. 逐行读懂 `get_dataloaders_mnist` / `get_dataloaders_cifar10` 的实现，理解 `validation_fraction`、`train_transforms`、`drop_last` 等每个参数的含义。
3. 会用 `set_all_seeds` 固定随机性，并理解它到底固定了什么、没固定什么。

这些模块是读懂后续所有讲义（MLP、CNN、RNN……）里训练代码的「公共词汇表」——先把它们吃透，后面每篇讲义都能少解释一半的样板代码。

## 2. 前置知识

本讲只需要上一讲的基础（能运行 notebook、理解 device 选择），再用两段话补齐两个新概念：

**（1）Python 模块与 import。** 一个 `.py` 文件就是一个模块。notebook 里写 `from helper_data import get_dataloaders_mnist`，就是让 Python 在 notebook 所在目录找到 `helper_data.py`，并取用其中定义的 `get_dataloaders_mnist` 函数。因为 notebook 与 helper 文件同在 `pytorch_ipynb/` 目录下（或通过相对路径寻址），所以无需安装任何包。

**（2）伪随机数与种子（seed）。** 计算机没有真正的随机，`random`、`numpy`、`torch` 内部都是**伪随机数生成器**（PRNG）：给它一个整数种子，它就按确定的算法产出一串「看起来随机」的数字序列。同一个种子永远产出同一个序列。深度学习实验里的随机性来源很多——权重初始化、数据打乱顺序、Dropout 的随机失活、数据增强的随机变换——它们各自调用不同的 PRNG，所以「固定随机性」需要把每一个 PRNG 的种子都设一遍，这正是 `set_all_seeds` 存在的原因。

其他术语速查：

| 术语 | 含义 |
|---|---|
| DataLoader | PyTorch 中按批（batch）迭代数据集的迭代器 |
| sampler | 决定「每次迭代取哪些样本索引」的策略对象 |
| epoch | 把训练集完整过一遍 |
| log_dict | 训练函数返回的字典，按类别记录每个 epoch / batch 的损失与准确率 |
| checkpoint | 保存到磁盘的模型参数（`state_dict`） |

## 3. 本讲源码地图

本讲涉及 6 个文件，全部位于 `pytorch_ipynb/` 目录下：

| 文件 | 行数 | 作用 |
|---|---|---|
| [helper_data.py](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_data.py) | 150 | 数据加载：`UnNormalize`、`get_dataloaders_mnist`、`get_dataloaders_cifar10` |
| [helper_train.py](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_train.py) | 162 | 训练循环：`train_classifier_simple_v1`、`train_classifier_simple_v2` |
| [helper_evaluate.py](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_evaluate.py) | 73 | 评估指标：`compute_accuracy`、`compute_epoch_loss`、`compute_confusion_matrix` |
| [helper_plotting.py](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_plotting.py) | 191 | 可视化：`plot_training_loss`、`plot_accuracy`、`show_examples`、`plot_confusion_matrix` |
| [helper_utils.py](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_utils.py) | 19 | 随机性与确定性：`set_all_seeds`、`set_deterministic` |
| [helper.py](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper.py) | 61 | 数据转换工具：`quickdraw_npy_to_imagefile`（Quick Draw `.npy` 转图片目录） |

一个真实的使用范本是 [cnn/cnn-mobilenet-v2-cifar10.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-mobilenet-v2-cifar10.ipynb)，它在导入区一次性用满了其中 5 个模块（详见 4.2 节）。`helper_train.py` 第 1–2 行还从 `helper_evaluate` 导入函数，可见模块之间也有依赖：**train 依赖 evaluate，evaluate 独立，plotting 独立，data/utils 独立**。

## 4. 核心概念与源码讲解

### 4.1 helper_data：数据加载与验证集划分

#### 4.1.1 概念说明

任何一个分类 notebook 的第一步都是：下载数据集 → 划分训练/验证/测试 → 构造三个 DataLoader。这段代码有 40~50 行，而且 90% 是重复的。`helper_data.py` 把它抽成两个函数（MNIST 版与 CIFAR-10 版），让 notebook 只需一行调用。

函数签名（两个函数完全同构）：

```python
get_dataloaders_mnist(batch_size, num_workers=0,
                      validation_fraction=None,
                      train_transforms=None,
                      test_transforms=None)
```

参数含义：

| 参数 | 含义 |
|---|---|
| `batch_size` | 每批样本数 |
| `num_workers` | DataLoader 用几个子进程加载数据（0 表示主进程加载） |
| `validation_fraction` | 从训练集中切出多大比例做验证集；`None` 表示不要验证集 |
| `train_transforms` | 训练集的预处理（可含随机数据增强）；默认 `ToTensor()` |
| `test_transforms` | 验证集与测试集共用的预处理；默认 `ToTensor()` |

注意返回值个数会变：传了 `validation_fraction` 返回 3 个 loader，否则返回 2 个。

#### 4.1.2 核心流程

`get_dataloaders_mnist` 的执行流程：

```text
1. transform 缺省补齐：None → transforms.ToTensor()
2. 构造 3 个 Dataset 对象：
   train_dataset  ← MNIST(train=True,  train_transforms)
   valid_dataset  ← MNIST(train=True,  test_transforms)   # 同一份数据!
   test_dataset   ← MNIST(train=False, test_transforms)
3. 若 validation_fraction 不为 None：
   a. num  = int(validation_fraction * 60000)
   b. 训练索引 = [0, 60000-num)，验证索引 = [60000-num, 60000)
   c. 各自包成 SubsetRandomSampler（随机无放回采样）
   d. train_loader: drop_last=True；valid_loader: 不 drop
   否则：train_loader 直接 shuffle=True
4. test_loader：shuffle=False（测试顺序无所谓，保持可复现）
5. 按 validation_fraction 是否为 None，返回 2 或 3 个 loader
```

关键设计有两个，先讲直觉再讲源码：

- **训练/验证共用同一份 `train=True` 的数据**，靠「索引区间 + sampler」切成两半，而不是复制数据。切分公式为：

  \[ N_{valid} = \lfloor f \times 60000 \rfloor, \qquad N_{train} = 60000 - N_{valid} \]

  其中 \( f \) 是 `validation_fraction`。这是**按索引划分**而非随机划分：前 90% 训练、后 10% 验证（但两个 sampler 内部各自随机打乱顺序）。

- **valid_dataset 用的是 `test_transforms`**。这是刻意为之：验证集的职责是模拟「考试」，如果验证集用了随机翻转、随机裁剪等增强，验证指标就会额外抖动，失去对照意义。

#### 4.1.3 源码精读

**① transform 缺省补齐**——用 `is None` 判断而不是 `if not train_transforms`，因为空的 transform 组合也可能是合法对象：

[pytorch_ipynb/helper_data.py:L34-L38](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_data.py#L34-L38)
```python
if train_transforms is None:
    train_transforms = transforms.ToTensor()

if test_transforms is None:
    test_transforms = transforms.ToTensor()
```
这段给两个 transform 参数提供默认值：`ToTensor()` 只做「PIL 图片 → 张量，像素值缩放到 [0,1]」，不做标准化也不做增强。

**② 三个 Dataset 对象**——注意 `train_dataset` 与 `valid_dataset` 的 `train=True` 完全相同，只有 transform 不同：

[pytorch_ipynb/helper_data.py:L40-L51](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_data.py#L40-L51)
```python
train_dataset = datasets.MNIST(root='data', train=True,
                               transform=train_transforms, download=True)

valid_dataset = datasets.MNIST(root='data', train=True,
                               transform=test_transforms)

test_dataset = datasets.MNIST(root='data', train=False,
                              transform=test_transforms)
```
`download=True` 只写在 train_dataset 上——两份其实共用同一批磁盘文件（`data/` 目录），下载一次即可。`root='data'` 是相对路径，这就是上一讲强调「保持仓库目录结构完整运行」的原因之一。

**③ 验证集划分与 sampler**——本函数最核心的 8 行：

[pytorch_ipynb/helper_data.py:L53-L70](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_data.py#L53-L70)
```python
if validation_fraction is not None:
    num = int(validation_fraction * 60000)
    train_indices = torch.arange(0, 60000 - num)
    valid_indices = torch.arange(60000 - num, 60000)

    train_sampler = SubsetRandomSampler(train_indices)
    valid_sampler = SubsetRandomSampler(valid_indices)

    valid_loader = DataLoader(dataset=valid_dataset, batch_size=batch_size,
                              num_workers=num_workers, sampler=valid_sampler)

    train_loader = DataLoader(dataset=train_dataset, batch_size=batch_size,
                              num_workers=num_workers, drop_last=True,
                              sampler=train_sampler)
```
做了三件事：按比例算出验证样本数 `num`；把 0~59999 的索引切成两段；用 `SubsetRandomSampler` 包装——它的语义是「在给定的索引子集内随机打乱地采样」。这里能看出三个易忽略的细节：

- `60000` 是硬编码的 MNIST 训练集大小，`get_dataloaders_cifar10` 里同样的位置写的是 `50000`（[L115-L117](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_data.py#L115-L117)）。所以这两个函数是**数据集专用**的，换数据集要照抄改数字。
- `drop_last=True` 只对 train_loader 开启：丢弃最后不足一个 batch 的零头，让每个 batch 尺寸一致，损失曲线更平滑；验证/测试集不丢样本。
- 指定了 `sampler` 就**不能**再传 `shuffle=True`，二者互斥（DataLoader 会直接报错）。随机性由 sampler 负责。

**④ 无验证集分支与返回值**：

[pytorch_ipynb/helper_data.py:L72-L87](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_data.py#L72-L87)
```python
else:
    train_loader = DataLoader(dataset=train_dataset, batch_size=batch_size,
                              num_workers=num_workers,
                              drop_last=True, shuffle=True)
...
if validation_fraction is None:
    return train_loader, test_loader
else:
    return train_loader, valid_loader, test_loader
```
没有 sampler 时改用 `shuffle=True` 打乱训练数据。返回值个数随参数变化——调用方必须清楚自己传没传 `validation_fraction`，否则解包会报错。

**⑤ UnNormalize：标准化的逆操作**：

[pytorch_ipynb/helper_data.py:L9-L26](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_data.py#L9-L26)
```python
class UnNormalize(object):
    def __init__(self, mean, std):
        self.mean = mean
        self.std = std

    def __call__(self, tensor):
        for t, m, s in zip(tensor, self.mean, self.std):
            t.mul_(s).add_(m)
        return tensor
```
图像标准化执行的运算是 \( x' = (x - \mu)/\sigma \)，这个类的 `__call__` 就是逆运算 \( x = x' \cdot \sigma + \mu \)（`mul_`/`add_` 是就地操作）。它存在的意义是：喂给模型的张量是标准化后的「怪样子」，人眼看不懂；画图前用它还原成正常像素。4.3 节的 `show_examples` 会用到它。

#### 4.1.4 代码实践（源码阅读型）

**目标**：不运行代码，纯靠读源码推算 DataLoader 的批次数，为综合实践做好准备。

**步骤**：

1. 阅读上文引用的 L53-L70，假设 `batch_size=64`、`validation_fraction=0.1`。
2. 计算：`num = int(0.1 * 60000) = 6000`；训练样本 54000，验证样本 6000，测试样本 10000。
3. 推算批次数：
   - train：\( \lfloor 54000 / 64 \rfloor = 843 \)（drop_last 丢掉 0.75 个 batch 的零头）
   - valid：\( \lceil 6000 / 64 \rceil = 94 \)
   - test：\( \lceil 10000 / 64 \rceil = 157 \)

**需要观察/预期结果**：把这三个数字记下来，综合实践中用真实代码验证 `len(train_loader)` 是否等于 843。这一步是「读源码 → 做预测 → 跑代码验证」的完整闭环。本推算为手工计算，具体数值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`validation_fraction=0.2` 时，训练集和验证集各多少样本？

答案：`num = int(0.2 * 60000) = 12000`，验证集 12000 个，训练集 48000 个（索引 0~47999 训练，48000~59999 验证）。

**练习 2**：为什么 `valid_dataset` 使用 `test_transforms` 而不是 `train_transforms`？

答案：验证集用于模拟测试场景、挑选模型与超参。若使用含随机增强的 train_transforms，验证指标会因增强的随机性而抖动，无法公平比较不同 epoch / 不同模型的泛化能力；测试集同理。随机增强只应作用于训练集。

**练习 3**：如果同时给 DataLoader 传 `sampler=` 和 `shuffle=True` 会怎样？

答案：PyTorch 会抛出 `ValueError`，二者互斥——sampler 本身已经决定了取样本的顺序，shuffle 参数只在没有自定义 sampler 时才有意义。所以源码里两个分支各用其一。

### 4.2 helper_train：两个版本的训练循环

#### 4.2.1 概念说明

`helper_train.py` 提供两个「开箱即用」的训练函数，代表了作者的两代设计：

| 维度 | `train_classifier_simple_v1` | `train_classifier_simple_v2` |
|---|---|---|
| 参数顺序 | `num_epochs, model, optimizer, device, ...` | `model, num_epochs, ..., optimizer, device` |
| 返回值 | 一个大字典 `log_dict` | 三个列表（minibatch 损失、训练准确率、验证准确率） |
| 损失函数 | 可传入 `loss_fn`，默认 `F.cross_entropy` | 固定 `cross_entropy` |
| 保存最优模型 | ❌ | ✅ `best_model_save_path` |
| 学习率调度器 | ❌ | ✅ `scheduler` + `scheduler_on` |
| 训练集准确率 | 每_epoch 计算（可 `skip_epoch_stats` 跳过） | 可 `skip_train_acc` 跳过 |
| 结束后测测试集 | ❌ | ✅ 自动打印 test accuracy |

v2 更贴近真实实验流程：**以验证准确率为准保存最优 checkpoint，训练结束立刻在测试集上报告成绩**。

#### 4.2.2 核心流程

两个函数的主干是同一个「五步训练循环」（上一讲的 softmax 回归里你已经见过它），v2 的整体流程：

```text
初始化 best_valid_acc = -∞, best_epoch = 0
for epoch in 1..num_epochs:
    model.train()                          # 切训练模式
    for每个 batch:
        logits = model(features)           # ① 前向
        loss  = cross_entropy(logits, targets)
        optimizer.zero_grad()              # ② 清梯度
        loss.backward()                    # ③ 反向
        optimizer.step()                   # ④ 更新参数
        记录 loss.item()                   # ⑤ 日志
    model.eval() + no_grad:
        train_acc, valid_acc = compute_accuracy(...)
        若 valid_acc > best_valid_acc:
            更新 best；若有保存路径 → torch.save(model.state_dict())
    若有 scheduler: 按 scheduler_on 指标的最新值 scheduler.step(...)
结束: compute_accuracy(model, test_loader) → 打印测试准确率
```

「最优」的判定就是一次简单比较：当 \( a_{valid} > a_{best} \) 时更新并保存。初始值取 \(-\infty\)，保证第 1 个 epoch 一定会触发保存。

#### 4.2.3 源码精读

**① 模块依赖与 v1 的日志字典**：

[pytorch_ipynb/helper_train.py:L1-L2](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_train.py#L1-L2)
```python
from helper_evaluate import compute_accuracy
from helper_evaluate import compute_epoch_loss
```
训练循环把「算准确率/算损失」委托给 `helper_evaluate`——这是本讲义 4.3 节的主角，说明 helper 模块之间是分层复用的。

[pytorch_ipynb/helper_train.py:L20-L27](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_train.py#L20-L27)
```python
log_dict = {'train_loss_per_batch': [],
            'train_acc_per_epoch': [],
            'train_loss_per_epoch': [],
            'valid_acc_per_epoch': [],
            'valid_loss_per_epoch': []}

if loss_fn is None:
    loss_fn = F.cross_entropy
```
v1 把所有训练历史装进一个五键字典；损失函数允许外部注入，这是 v1 相对 v2 灵活的地方（v2 写死了交叉熵）。

**② v1 的五步循环与 RRef 特判**：

[pytorch_ipynb/helper_train.py:L33-L48](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_train.py#L33-L48)
```python
for batch_idx, (features, targets) in enumerate(train_loader):

    features = features.to(device)
    targets = targets.to(device)

    # FORWARD AND BACK PROP
    logits = model(features)
    if isinstance(logits, torch.distributed.rpc.api.RRef):
        logits = logits.local_value()
    loss = loss_fn(logits, targets)
    optimizer.zero_grad()

    loss.backward()

    # UPDATE MODEL PARAMETERS
    optimizer.step()
```
主体就是标准五步。中间那两行 `RRef` 判断值得专门一提：`RRef`（Remote Reference）是 PyTorch 分布式 RPC 的远程对象引用。当模型被切分到多张 GPU 上做**流水线并行**时（仓库里的 `mechanics/model-pipeline-vgg16.ipynb` 就是这样用的），前向传播返回的不是张量而是 RRef，需要 `.local_value()` 取回本机的值。这两行让同一个训练函数同时兼容单机与流水线并行两种模式——一个很实用的防御式编程范例。

**③ v1 的 epoch 级统计**：

[pytorch_ipynb/helper_train.py:L58-L68](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_train.py#L58-L68)
```python
if not skip_epoch_stats:
    model.eval()

    with torch.set_grad_enabled(False):  # save memory during inference

        train_acc = compute_accuracy(model, train_loader, device)
        train_loss = compute_epoch_loss(model, train_loader, device)
        ...
        log_dict['train_loss_per_epoch'].append(train_loss.item())
        log_dict['train_acc_per_epoch'].append(train_acc.item())
```
每个 epoch 结束后切换 `model.eval()`（关闭 Dropout、冻结 BatchNorm 统计）并在 `no_grad` 下统计整轮指标。`skip_epoch_stats=True` 可以跳过这步——在大数据集上，把训练集整个再前向一遍很费时间。

**④ v2 的最优模型保存**——v2 最有价值的 4 行：

[pytorch_ipynb/helper_train.py:L133-L136](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_train.py#L133-L136)
```python
if valid_acc > best_valid_acc:
    best_valid_acc, best_epoch = valid_acc, epoch+1
    if best_model_save_path:
        torch.save(model.state_dict(), best_model_save_path)
```
只要验证准确率创历史新高，就把当前 `state_dict`（全部可学习参数）写到磁盘。训练结束后磁盘上留下的就是「验证集表现最好的那个 epoch」的模型，而不是最后一个 epoch 的——这是防过拟合最简单有效的手段。

**⑤ v2 的学习率调度开关**：

[pytorch_ipynb/helper_train.py:L147-L154](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_train.py#L147-L154)
```python
if scheduler is not None:

    if scheduler_on == 'valid_acc':
        scheduler.step(valid_acc_list[-1])
    elif scheduler_on == 'minibatch_loss':
        scheduler.step(minibatch_loss_list[-1])
    else:
        raise ValueError('Invalid `scheduler_on` choice.')
```
`ReduceLROnPlateau` 这类调度器需要「观察某个指标」来决定是否调低学习率。`scheduler_on` 参数让调用方选择观察验证准确率还是最近的 batch 损失，传别的字符串直接抛 `ValueError`——「快速失败」而不是默默忽略错误配置。

**⑥ 真实调用范本**（来自仓库 notebook，非示例代码）：

[cnn/cnn-mobilenet-v2-cifar10.ipynb:L75-L79](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-mobilenet-v2-cifar10.ipynb#L75-L79)
```python
from helper_utils import set_all_seeds, set_deterministic
from helper_evaluate import compute_confusion_matrix, compute_accuracy
from helper_train import train_classifier_simple_v2
from helper_plotting import plot_training_loss, plot_accuracy, show_examples, plot_confusion_matrix
from helper_data import get_dataloaders_cifar10, UnNormalize
```
一个 notebook 用满 5 个 helper 模块——本讲四个小节讲解的内容正好对应这五行导入。

#### 4.2.4 代码实践（源码阅读型）

**目标**：弄清 v1 与 v2 各自被仓库里哪些 notebook 使用，理解「v2 是较新的推荐版本」。

**步骤**：

1. 在仓库根目录执行（只读操作）：`grep -rl "train_classifier_simple_v2" pytorch_ipynb --include="*.ipynb"`。
2. 同样方式搜索 `train_classifier_simple_v1` 的使用位置。
3. 打开搜到的任一 notebook（如 `cnn/cnn-mobilenet-v2-cifar10.ipynb`），找到调用 `train_classifier_simple_v2(...)` 的单元格，观察它传了哪些参数。

**需要观察/预期结果**：v2 的使用集中在 MobileNet-v2/v3 等较新的 CIFAR-10 notebook（共 3 个）；v1 则出现在更早的 notebook 中。调用处你会看到 `best_model_save_path=...` 这样的参数，与 ④ 的源码对上。具体命中列表**待本地验证**（grep 结果随仓库版本可能变化）。

#### 4.2.5 小练习与答案

**练习 1**：`best_valid_acc` 为什么初始化为 `-float('inf')` 而不是 0？

答案：准确率取值范围是 0~100，初始化为 0 时如果第 1 个 epoch 验证准确率恰好为 0（例如学习率设得过大导致模型完全不学习），就不会触发保存。取 \(-\infty\) 保证任何有限的准确率都会大于初始值，第 1 个 epoch 必定触发一次「历史最优」记录与保存。

**练习 2**：`scheduler.step(valid_acc_list[-1])` 里的 `valid_acc_list[-1]` 是什么？

答案：当前 epoch 刚刚 append 进列表的**最新一个**验证准确率（Python 负索引取末尾元素）。把它喂给 `ReduceLROnPlateau` 类调度器，调度器据此判断指标是否停滞、是否调低学习率。

**练习 3**：为什么训练循环里每次 `loss.backward()` 之前都要 `optimizer.zero_grad()`？

答案：PyTorch 的梯度默认是**累加**的。若不清零，本 batch 的梯度会叠加上一个 batch 的梯度，实际执行的是「跨 batch 梯度累积」而不是普通的小批量随机梯度下降，训练行为完全偏离预期。

### 4.3 helper_evaluate 与 helper_plotting：评估与可视化

#### 4.3.1 概念说明

训练结束后要回答三个问题：**模型多准？在哪类样本上出错？训练曲线长什么样？**

- `helper_evaluate.py` 提供三个数值指标函数：`compute_accuracy`（准确率）、`compute_epoch_loss`（整轮平均损失）、`compute_confusion_matrix`（混淆矩阵）。
- `helper_plotting.py` 提供四个画图函数：`plot_training_loss`（batch 损失曲线 + 滑动平均）、`plot_accuracy`（训练/验证准确率对比）、`show_examples`（3×5 样本网格，标注预测 P 与真实 T）、`plot_confusion_matrix`（混淆矩阵热力图）。

它们与 4.2 的训练函数是配套的：训练函数内部调用 evaluate 的函数算指标，返回的历史列表再交给 plotting 画图。

#### 4.3.2 核心流程

评估函数的共同骨架：

```text
model.eval()                    # 切评估模式
with torch.no_grad():           # 关闭梯度追踪（省内存、加速）
    for每个 batch:
        logits = model(features)
        累计 correct_pred / num_examples（或 loss 总和 / 混淆计数）
return 汇总量
```

`compute_epoch_loss` 的聚合方式是重点：它不是「各 batch 平均损失的平均」，而是**先求和再除以总样本数**：

\[ \mathcal{L}_{epoch} = \frac{1}{N}\sum_{i=1}^{N} \ell_i = \frac{1}{N}\sum_{b=1}^{B} \Big(\sum_{i \in b} \ell_i\Big) \]

原因：最后一个 batch 往往比其他 batch 小（比如 6000 = 93×64 + 48），如果对「batch 平均损失」再求平均，那个小 batch 会被赋予与其他 batch 相同的权重，引入偏差。逐样本求和后除以 \( N \) 是无偏的。

#### 4.3.3 源码精读

**① compute_accuracy——最常用的一个函数**：

[pytorch_ipynb/helper_evaluate.py:L7-L22](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_evaluate.py#L7-L22)
```python
def compute_accuracy(model, data_loader, device):
    model.eval()
    with torch.no_grad():
        correct_pred, num_examples = 0, 0
        for i, (features, targets) in enumerate(data_loader):

            features = features.to(device)
            targets = targets.to(device)

            logits = model(features)
            if isinstance(logits, torch.distributed.rpc.api.RRef):
                logits = logits.local_value()
            _, predicted_labels = torch.max(logits, 1)
            num_examples += targets.size(0)
            correct_pred += (predicted_labels == targets).sum()
    return correct_pred.float()/num_examples * 100
```
逐行看：`model.eval()` 保证 Dropout 关闭、BatchNorm 使用 running stats；`torch.no_grad()` 不建计算图；`torch.max(logits, 1)` 取每行（dim=1）最大值的**索引**即预测类别（最大 logit ↔ 最大 softmax 概率）；最后 `正确数/总数×100` 得到百分比。同样有 RRef 特判（与 4.2 ② 呼应）。注意返回的是**张量**而非 Python 浮点数，所以训练函数里要用 `.item()` 取值。

**② compute_epoch_loss——reduction='sum' 的聚合技巧**：

[pytorch_ipynb/helper_evaluate.py:L25-L40](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_evaluate.py#L25-L40)
```python
def compute_epoch_loss(model, data_loader, device):
    model.eval()
    curr_loss, num_examples = 0., 0
    with torch.no_grad():
        for features, targets in data_loader:
            ...
            loss = F.cross_entropy(logits, targets, reduction='sum')
            num_examples += targets.size(0)
            curr_loss += loss

        curr_loss = curr_loss / num_examples
        return curr_loss
```
`reduction='sum'` 让交叉熵返回 batch 内**逐样本损失之和**而非均值（默认是 `'mean'`）。累加所有 batch 的和，最后除以总样本数，正是 4.3.2 公式的直接翻译。

**③ compute_confusion_matrix——教学风格的计数实现**：

[pytorch_ipynb/helper_evaluate.py:L43-L72](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_evaluate.py#L43-L72)
```python
    all_targets, all_predictions = [], []
    ...
    class_labels = np.unique(np.concatenate((all_targets, all_predictions)))
    ...
    z = list(zip(all_targets, all_predictions))
    for combi in product(class_labels, repeat=2):
        lst.append(z.count(combi))
    mat = np.asarray(lst)[:, None].reshape(n_labels, n_labels)
    return mat
```
先把全部 (真实标签, 预测标签) 对收集成列表，再用 `itertools.product` 枚举所有 (真实, 预测) 组合并数出现次数，最后 reshape 成 n×n 矩阵。行是真实类别、列是预测类别。这个实现故意写得朴素直观（大类别数下会很慢），教学价值在于让你看清混淆矩阵的定义：\( M_{ij} = \#\{\text{真实为 } i \text{ 且预测为 } j\} \)。有一处细节：`np.unique` 的输入合并了预测标签，保证某些「从未被预测过/从未真实出现」的类别也能占据一行一列。另外注意此函数里 `targets` 没有 `.to(device)`（保持在 CPU 上做后续统计），而 compute_accuracy 里有——两者侧重不同。

**④ plot_training_loss——滑动平均与双 x 轴**：

[pytorch_ipynb/helper_plotting.py:L8-L26](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_plotting.py#L8-L26)
```python
    ax1.plot(range(len(minibatch_loss_list)),
             (minibatch_loss_list), label='Minibatch Loss')

    if len(minibatch_loss_list) > 1000:
        ax1.set_ylim([
            0, np.max(minibatch_loss_list[1000:])*1.5
            ])
    ...
    ax1.plot(np.convolve(minibatch_loss_list,
                         np.ones(averaging_iterations,)/averaging_iterations,
                         mode='valid'),
             label='Running Average')
```
batch 级损失抖动很大，`np.convolve(损失序列, 全1核/长度)` 就是在做**滑动平均**：卷积核是长度 100（`averaging_iterations`）的均匀窗口，输出的每个点等于窗口内 100 个损失的平均。开头 `set_ylim` 那段是个实用技巧——训练初期损失可能爆高，把 y 轴上限压到「第 1000 个 batch 之后最大值的 1.5 倍」，避免前几个异常点把整条曲线压扁到看不清。该函数后半段（L31-L43）还用 `ax1.twiny()` 画了第二条 x 轴，把 iteration 换算成 epoch。

**⑤ show_examples——与 UnNormalize 的联动**：

[pytorch_ipynb/helper_plotting.py:L74-L92](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_plotting.py#L74-L92)
```python
    for batch_idx, (features, targets) in enumerate(data_loader):

        with torch.no_grad():
            logits = model(features)
            predictions = torch.argmax(logits, dim=1)
        break

    fig, axes = plt.subplots(nrows=3, ncols=5, ...)

    if unnormalizer is not None:
        for idx in range(features.shape[0]):
            features[idx] = unnormalizer(features[idx])
    nhwc_img = np.transpose(features, axes=(0, 2, 3, 1))
```
三个值得注意的点：循环体末尾的 `break` 让它只取**第一个 batch**（15 张图正好填 3×5 网格，所以要求 batch_size ≥ 15）；可选参数 `unnormalizer` 正是 4.1 ⑤ 的 `UnNormalize` 实例——两个模块在这里接上了头；`np.transpose(axes=(0,2,3,1))` 把 PyTorch 的 NCHW（批×通道×高×宽）转成 matplotlib 需要的 NHWC。每个子图标题形如 `P: 预测 | T: 真实`（传 `class_dict` 时显示类别名），扫一眼就能找到失败案例。

**⑥ plot_confusion_matrix——参数即开关**：

[pytorch_ipynb/helper_plotting.py:L120-L137](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_plotting.py#L120-L137)
```python
    if not (show_absolute or show_normed):
        raise AssertionError('Both show_absolute and show_normed are False')
    ...
    total_samples = conf_mat.sum(axis=1)[:, np.newaxis]
    normed_conf_mat = conf_mat.astype('float') / total_samples
```
`show_absolute` / `show_normed` 控制格子里显示原始计数还是按行归一化的比例（每行除以该真实类别的样本总数，即条件概率 \( P(\text{预测}=j \mid \text{真实}=i) \)），两者都为 False 时立即断言报错。它的输入正是 ③ 的输出——`compute_confusion_matrix` 与 `plot_confusion_matrix` 一算一画，组成混淆矩阵工具对。

#### 4.3.4 代码实践（源码阅读型）

**目标**：从真实 notebook 的评估段，看清 evaluate 与 plotting 函数如何被串联使用。

**步骤**：

1. 打开 [cnn/cnn-mobilenet-v2-cifar10.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-mobilenet-v2-cifar10.ipynb)（在 GitHub 上看或本地用 Jupyter 打开）。
2. 找到训练之后的单元格，依次确认这些调用：`plot_training_loss(minibatch_loss_list, ...)`、`plot_accuracy(train_acc_list, valid_acc_list, ...)`、`show_examples(model, test_loader, unnormalizer, class_dict)`、`compute_confusion_matrix(model, test_loader, device)` + `plot_confusion_matrix(...)`。
3. 对照本节源码，画出「训练函数返回什么 → 哪个绘图函数消费它」的连线图。

**需要观察/预期结果**：`train_classifier_simple_v2` 返回的三个列表分别被 `plot_training_loss`（minibatch_loss_list）和 `plot_accuracy`（两个 acc 列表）消费；`show_examples` 的 `unnormalizer` 参数由 `UnNormalize(mean, std)` 构造传入，`std`/`mean` 与数据标准化时用的值一致。

#### 4.3.5 小练习与答案

**练习 1**：`compute_accuracy` 里为什么需要 `model.eval()` 和 `torch.no_grad()` 两件事，缺一行会怎样？

答案：`model.eval()` 改变**前向行为**——不关的话 Dropout 在推理时仍在随机丢弃、BatchNorm 仍用 batch 统计量，结果又差又随机；`torch.no_grad()` 只是**不记录梯度**，不关的话功能上结果相同，但会白白构建计算图、多占显存且更慢。两行解决的是两个不同维度的问题。

**练习 2**：`compute_epoch_loss` 为什么用 `reduction='sum'` 而不是默认的 `'mean'` 再对 batch 求平均？

答案：见 4.3.2——最后一个 batch 常常比其他小。若先对 batch 内取平均、再对 batch 平均，小 batch 与大 batch 权重相同，结果有偏。逐样本求和再除以总样本数 \( N \) 才是真正的全数据集平均损失。

**练习 3**：混淆矩阵的**行**和**列**各代表什么？`plot_confusion_matrix` 里按行归一化后每个格子的含义是什么？

答案：行是真实类别、列是预测类别（源码 `zip(all_targets, all_predictions)` + `product(class_labels, repeat=2)` 的计数顺序决定了这一点）。按行归一化后，第 \( i \) 行第 \( j \) 列的值是 \( P(\text{预测}=j \mid \text{真实}=i) \)，即「真实是类别 i 的样本中被预测成类别 j 的比例」；对角线就是每类的召回率。

### 4.4 helper_utils：随机性、确定性与 helper.py

#### 4.4.1 概念说明

`helper_utils.py` 只有 19 行、两个函数，却回答了实验科学性的根本问题：**同样的代码跑两遍，能得出同样的结果吗？**

深度学习实验的随机性来自至少四处：

1. 权重初始化（torch 的 PRNG）；
2. 数据加载顺序（sampler/shuffle 用的随机源）；
3. 模型内部的随机层（Dropout 等）；
4. 数据增强中的随机变换（torchvision 的随机源）。

此外 Python 自带的 `random` 与 NumPy 的 `numpy.random` 是**三套独立的 PRNG**，互不影响。`set_all_seeds` 一口气把这几套全部固定；`set_deterministic` 则更进一步，把 GPU 上的非确定性计算也强行换成确定性算法。

同目录下还有一个孤立的 [helper.py](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper.py)，只有 `quickdraw_npy_to_imagefile` 一个函数，负责把 Google Quick Draw 数据集的 `.npy` 文件批量转成「按类别分目录」的 png 图片——它是为 `mechanics/custom-data-loader-quickdraw.ipynb` 这类自定义数据集实验准备原料的，本节末尾顺带认识。

#### 4.4.2 核心流程

**set_all_seeds(seed) 的作用范围**：

```text
os.environ["PL_GLOBAL_SEED"] = str(seed)   # 环境变量，供 PyTorch Lightning 读取
random.seed(seed)                          # Python 内建 PRNG
np.random.seed(seed)                       # NumPy PRNG
torch.manual_seed(seed)                    # torch 全局 PRNG（CPU）
torch.cuda.manual_seed_all(seed)           # 所有 GPU 上的 PRNG
```

**随机性的层级与两种开关的关系**：

\[ \underbrace{\text{相同 seed}}_{\text{set_all_seeds}} \Rightarrow \text{相同初始化 + 相同数据顺序} \;\xrightarrow{\text{仍可能不同}}\; \underbrace{\text{GPU 非确定性算子}}_{\text{set_deterministic 强制换成确定性版本}} \Rightarrow \text{完全可复现} \]

`set_all_seeds` 是「便宜且无副作用」的，几乎所有 notebook 都会调用；`set_deterministic` 有性能代价（GPU 算法选择受限、可能变慢），且仓库 notebook 里它经常以注释形式出现（如 `cnn-mobilenet-v2-cifar10.ipynb` 里就是 `#set_deterministic()`）——追求复现时才打开。

#### 4.4.3 源码精读

**① set_all_seeds——五行五个随机源**：

[pytorch_ipynb/helper_utils.py:L7-L12](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_utils.py#L7-L12)
```python
def set_all_seeds(seed):
    os.environ["PL_GLOBAL_SEED"] = str(seed)
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
```
逐行对应 4.4.2 的五个随机源。第一行设置的环境变量 `PL_GLOBAL_SEED` 是给 **PyTorch Lightning** 框架读的（Lightning 会在自己的 worker 进程里根据它设 seed），这为 U8 单元的 Lightning 讲义埋下伏笔。`torch.manual_seed` 同时固定 CPU 上的权重初始化与 DataLoader 的随机打乱；`manual_seed_all` 覆盖所有可见 GPU。调用时机有讲究：必须在**创建模型之前**调用——先建模型再设 seed，权重已经初始化完了，seed 影响不到它（仓库 notebook 的惯例是「设置区」先 `set_all_seeds(RANDOM_SEED)` 再定义模型）。

**② set_deterministic——三行强制确定性**：

[pytorch_ipynb/helper_utils.py:L15-L19](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_utils.py#L15-L19)
```python
def set_deterministic():
    if torch.cuda.is_available():
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True
    torch.set_deterministic(True)
```
三行的含义：`cudnn.benchmark = False` 关闭「试跑多个卷积算法挑最快」的自动调优（挑中的算法可能是非确定性的）；`cudnn.deterministic = True` 强制 cuDNN 只用确定性卷积实现；`torch.set_deterministic(True)` 把约束推广到整个 PyTorch 的算子层面（某些 GPU 上的 scatter/atomicAdd 归约天生非确定，开启后要么换确定性实现要么报错）。**版本提示**：`torch.set_deterministic` 在较新版本的 PyTorch 中已被 `torch.use_deterministic_algorithms(True)` 取代，若你的环境运行此函数报 `AttributeError`，可将其改为新 API。仓库中实测该函数行为的 notebook 是 `mechanics/deterministic_benchmark.ipynb`（U4 单元详讲）。

**③ helper.py 的 quickdraw_npy_to_imagefile——一次性数据转换工具**：

[pytorch_ipynb/helper.py:L33-L61](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper.py#L33-L61)
```python
    if not os.path.exists(outpath):
        os.mkdir(outpath)
    npy_list = [i for i in os.listdir(inpath) if i.endswith('.npy')]

    if subset:
        npy_list = [i for i in npy_list if i.split('.npy')[0] in subset]
    ...
    for i, j in zip(npy_list, npy_paths):

        label = (i.split('-')[-1]).split('.npy')[0]
        folder = os.path.join(outpath, label)
        if not os.path.exists(folder):
            os.mkdir(folder)
        X = np.load(j)

        cnt = 0
        for row in X:
            img_array = row.reshape(28, 28)
            ...
            imageio.imwrite(outfile, img_array[:, :])
            cnt += 1
```
Quick Draw 官方发布的 `.npy` 文件每个类别一个（如 `full_numpy_bitmap_lollipop.npy`），内部是 N×784 的扁平像素数组。此函数对每个文件：从文件名解析出类别标签 → 在输出目录下建同名子目录 → 逐行 reshape 成 28×28 灰度图用 `imageio.imwrite` 存成 png，文件名带 6 位序号（`%06d`）。`subset` 参数可只转换指定类别，避免下载数十 GB 全量数据。产出的「类别子目录 + 图片」结构正是 `ImageFolder` 式自定义 Dataset 的标准输入格式（U4 单元的自定义 Dataset 讲义会消费这种结构）。它与其他 `helper_*.py` 风格不同（无前缀命名、单一函数），属于早期遗留的工具模块。

#### 4.4.4 代码实践

**目标**：验证 `set_all_seeds` 确实固定了初始化，并确认你环境里的确定性 API 是否可用。

**步骤**（以下为示例代码，可在任意 Python 环境/notebook 单元中运行，无需下载数据集）：

```python
import torch
from helper_utils import set_all_seeds   # 需在 pytorch_ipynb 目录下，或先 sys.path.append

# 实验 A：固定 seed，两次生成的随机权重完全一致
set_all_seeds(123)
w1 = torch.randn(3, 3)
set_all_seeds(123)
w2 = torch.randn(3, 3)
print(torch.equal(w1, w2))          # 预期 True

# 实验 B：不设 seed，两次几乎必然不同
print(torch.equal(torch.randn(3, 3), torch.randn(3, 3)))  # 预期 False（概率意义上）

# 实验 C：检查确定性 API 在你的 torch 版本中是否存在
print(hasattr(torch, "set_deterministic"), hasattr(torch, "use_deterministic_algorithms"))
```

**需要观察/预期结果**：实验 A 打印 `True`（同一 seed 产出同一序列，这正是「两次实验可比」的基础）；实验 B 通常打印 `False`；实验 C 的两个布尔值告诉你该环境应使用哪个 API——旧版只有前者，新版两者皆有（前者已废弃）或仅剩后者。

**预期结果**：以上三组输出。运行细节**待本地验证**（实验 B 理论上存在极小概率为 True）。

#### 4.4.5 小练习与答案

**练习 1**：只在脚本开头写了 `torch.manual_seed(42)`，其余什么都不设，还有哪些随机性没被固定？

答案：Python 内建 `random` 与 NumPy 的 PRNG 未固定——任何用到它们的代码（如某些数据增强、自定义打乱、NumPy 初始化）仍不可复现；各 GPU 上的随机源也没固定（应加 `torch.cuda.manual_seed_all`）。`set_all_seeds` 的价值就是把这些一次补齐。

**练习 2**：`set_all_seeds` 应该在定义模型之前还是之后调用？为什么？

答案：之前。权重初始化发生在模型构造的那一刻；如果先建模型再设 seed，初始化已经用旧随机状态完成了，seed 无法追溯影响它。仓库 notebook 的惯例是把 `set_all_seeds(RANDOM_SEED)` 写在设置区、模型定义之前（`cnn-mobilenet-v2-cifar10.ipynb` 中第 111 行附近即是如此，且 `set_deterministic()` 保持注释掉的状态）。

**练习 3**：既然 `set_deterministic` 能保证完全复现，为什么仓库 notebook 默认不开启它？

答案：确定性有代价：`cudnn.benchmark=False` 放弃了卷积算法自动调优、确定性算子通常更慢，某些操作在确定性模式下甚至不可用（直接抛错）。日常调参实验只需 `set_all_seeds` 保证「初始化与数据顺序相同」即可接受；只有在发布结果、做基准对比等需要逐位复现的场景才值得开 `set_deterministic`。

## 5. 综合实践

**任务**：写一个独立脚本，把本讲的三个主角串起来——用 `set_all_seeds` 固定随机性，用 `get_dataloaders_mnist` 构造三个 DataLoader，打印每个 loader 的 batch 形状与批次数，并验证样本划分是否符合 `validation_fraction`。同时用 4.1.4 手工推算的数字做交叉验证。

**操作步骤**：

1. 在 `pytorch_ipynb/` 目录下新建脚本 `check_dataloaders.py`（或在该目录启动 Jupyter 新建 notebook；MNIST 会下载到 `pytorch_ipynb/data/`，脚本与 notebook 同目录即可直接 import）。
2. 写入以下内容（示例代码）：

```python
import torch
from helper_data import get_dataloaders_mnist
from helper_utils import set_all_seeds

set_all_seeds(42)  # 必须在构造任何随机对象之前；影响 MNIST 下载无、影响后续训练有

train_loader, valid_loader, test_loader = get_dataloaders_mnist(
    batch_size=64,
    validation_fraction=0.1,
    num_workers=0)

for name, loader in [('train', train_loader),
                     ('valid', valid_loader),
                     ('test', test_loader)]:
    features, targets = next(iter(loader))
    print(f'{name:5s} | len(loader)={len(loader):4d}'
          f' | batch shape={tuple(features.shape)}'
          f' | targets shape={tuple(targets.shape)}')

# 更精确的样本数验证：sampler 持有全部索引
n_train = len(train_loader.sampler)
n_valid = len(valid_loader.sampler)
n_test = len(test_loader.dataset)
print(f'sampler 样本数: train={n_train}, valid={n_valid}; test 数据集={n_test}')

expected_valid = int(0.1 * 60000)
assert n_valid == expected_valid, '验证集样本数与 validation_fraction 不符'
assert n_train == 60000 - expected_valid, '训练集样本数与 validation_fraction 不符'
print(f'验证通过: validation_fraction=0.1 → {n_train} train / {n_valid} valid / {n_test} test')
```

3. 运行脚本：`python check_dataloaders.py`（首次运行会自动下载 MNIST 约 10+ MB 到 `data/`，需联网）。

**需要观察的现象**：

- 三个 loader 各打印一行：batch 形状应为 `(64, 1, 28, 28)`（批×通道×高×宽），targets 形状 `(64,)`。
- 批次数分别约为 843 / 94 / 157——与 4.1.4 的手工推算对质：843 来自 `drop_last=True` 截断的 54000/64，94 与 157 来自向上取整（最后一个 batch 只有 48 或 8 个样本，仍会被 `next(iter(loader))` 正常取出，形状相应变小）。
- 最后一行断言通过：54000 train / 6000 valid / 10000 test。

**预期结果**：`60000 * 0.9 = 54000`，`int(0.1 * 60000) = 6000`，与 `len(loader.sampler)` 精确相等；若把 `validation_fraction` 改成 `0.2` 重跑，应得到 48000 / 12000。首个 batch 形状与批次数的具体数值**待本地验证**。

**加分项**：再跑一遍脚本，确认两次输出完全一致（`set_all_seeds` 的直接效果——虽然本脚本没有随机初始化，sampler 的打乱顺序也被固定了）；然后用 `for features, targets in train_loader:` 循环数一数实际迭代次数是否等于 `len(train_loader)`。

## 6. 本讲小结

- `pytorch_ipynb/` 的 6 个 helper 模块构成新版 notebook 的公共基础设施：**data**（加载）、**train**（循环）、**evaluate**（指标）、**plotting**（可视化）、**utils**(随机性)、**helper**（Quickdraw 转换）；依赖方向是 train → evaluate。
- `get_dataloaders_mnist/cifar10` 用「同一份 `train=True` 数据 + 索引区间 + `SubsetRandomSampler`」切出验证集（\( N_{valid} = \lfloor f \times 60000 \rfloor \)），训练集 `drop_last=True`，验证/测试集用无随机的 `test_transforms`；60000/50000 是硬编码，函数是数据集专用的。
- `train_classifier_simple_v1/v2` 都实现了「前向 → 损失 → zero_grad → backward → step」五步循环；v2 增加了最优 checkpoint 保存（`valid_acc > best` 即 `torch.save`）、学习率调度与测试集终评。
- 评估函数统一在 `model.eval()` + `no_grad` 下工作；`compute_epoch_loss` 用 `reduction='sum'` 先求和再除以样本总数，避免「小尾巴 batch」造成的平均偏差。
- `set_all_seeds` 一次固定 Python/NumPy/torch(CPU+GPU)/Lightning 五个随机源（须在建模前调用）；`set_deterministic` 以性能换逐位复现，默认不开。
- `show_examples` 只取第一个 batch 画 3×5 网格，其 `unnormalizer` 参数消费 `helper_data.UnNormalize`——模块之间在参数层面互相协作。

## 7. 下一步学习建议

helper 模块是「读懂代码」的一步，下一讲（u2-l1 感知机与逻辑回归）将回到 notebook 本身，看两个最简单的线性模型如何**不依赖**这些 helper、从零写出训练过程——那是理解 helper 到底替你做了什么的最好参照。之后再进入 u2-l2（softmax 回归），那里会把本讲的「五步训练循环」第一次完整走一遍。

在继续之前，建议你：

1. 通读一遍 [helper_train.py](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_train.py) 全文，不查任何资料认出每一个变量——它只有 162 行。
2. 浏览 [mechanics/dataloader-nesting.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/dataloader-nesting.ipynb)，了解 DataLoader 嵌套迭代的陷阱（U4 单元的前置阅读）。
3. 如果对「为什么 batch 损失要平滑」感兴趣，试着改 `plot_training_loss` 的 `averaging_iterations` 参数（100 → 10 / 500）看曲线变化——参数的行为边界也是源码的一部分。
