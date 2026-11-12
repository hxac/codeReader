# 公共训练评估工具链与确定性

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐行读懂 `train_classifier_simple_v1` 与 `train_classifier_simple_v2` 两个共享训练函数，并说清两者的差异与各自适用场景。
2. 理解 `compute_accuracy` 与 `compute_epoch_loss` 的实现细节，尤其是 `reduction='sum'` 如何避免小批次带来的统计偏差。
3. 会用 `plot_training_loss` / `plot_accuracy` 可视化训练过程，并看懂滑动平均与双横轴的实现。
4. 区分「设随机种子」与「开确定性模式」这两个层次的可复现性，理解 `set_all_seeds` / `set_deterministic` 的原理与代价。

从本讲起，我们把此前各讲手工展开的训练循环「外包」给共享工具链——这也是仓库中新版 notebook（如 MobileNet 系列）的标准组织方式。

## 2. 前置知识

- **五步训练范式**（u2-l2、u2-l3 已建立）：每个 batch 都执行 `forward → loss → zero_grad → backward → step`。本讲的训练函数只是把这个范式装进函数体。
- **train/eval 双模式**（u2-l4）：`model.train()` / `model.eval()` 翻转 `self.training` 标志，Dropout 与 BatchNorm 的行为随之改变；评估时应配合 `torch.no_grad()` 关闭梯度。
- **验证集划分**（u4-l1）：`get_dataloaders_mnist` 等函数返回 train / valid / test 三个 loader，验证集用于逐 epoch 监控与选模型，测试集只在最后用一次。
- **深度学习中的随机性来源**：权重初始化、数据打乱顺序、Dropout 掩码、数据增强抽样，以及 GPU 上 cuDNN 自动选择的非确定性卷积算法。「可复现」就是要控制这些来源。

如果对以上任何一点感到陌生，建议先回顾对应讲义再继续。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注 |
| --- | --- | --- |
| `pytorch_ipynb/helper_train.py` | 两个共享训练循环 | `train_classifier_simple_v1` / `v2` 全文 |
| `pytorch_ipynb/helper_evaluate.py` | 评估指标 | `compute_accuracy`、`compute_epoch_loss` |
| `pytorch_ipynb/helper_plotting.py` | 可视化 | `plot_training_loss`、`plot_accuracy` |
| `pytorch_ipynb/helper_utils.py` | 随机性控制 | `set_all_seeds`、`set_deterministic` |
| `pytorch_ipynb/mechanics/deterministic_benchmark.ipynb` | 确定性开关的实测基准 | ResNet-101/CIFAR-10 开关前后耗时对比 |
| `pytorch_ipynb/mechanics/deterministic_benchmark_utils.py` | 基准 notebook 附带的 ResNet-101 实现 | 仅作背景了解 |

依赖方向：`helper_train.py` 顶部从 `helper_evaluate` 导入两个评估函数（训练循环内部逐 epoch 调用它们），所以**读 helper_train 之前先读 helper_evaluate**；`helper_plotting` 与 `helper_utils` 则被各个 notebook 直接使用。一个真实调用样例见 [pytorch_ipynb/cnn/cnn-mobilenet-v2-cifar10.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-mobilenet-v2-cifar10.ipynb)，它把整条工具链串了起来。

## 4. 核心概念与源码讲解

### 4.1 训练循环 v1/v2：把五步范式封装成函数

#### 4.1.1 概念说明

前几讲的 notebook 都把训练循环直接写在 cell 里。一旦要跑几十个模型，重复写循环既冗余又容易抄错。仓库的做法是把「训练 + 逐 epoch 评估 + 日志」封装成 `helper_train.py` 里的两个函数：

- `train_classifier_simple_v1`：以 `log_dict`（字典）形式记录**每 batch 损失 + 每 epoch 的训练/验证准确率与损失**，信息最全。
- `train_classifier_simple_v2`：记录**每 batch 损失 + 每 epoch 训练/验证准确率**，并新增两个实验必备能力——**按验证准确率保存最优权重（checkpoint）**与**学习率调度器接入**。

#### 4.1.2 核心流程

两个函数的骨架相同，都是「epoch 外循环 × batch 内循环」：

```text
初始化日志容器
for epoch in 1..num_epochs:
    model.train()
    for features, targets in train_loader:      # 内循环：五步范式
        logits = model(features)
        loss = cross_entropy(logits, targets)
        optimizer.zero_grad(); loss.backward(); optimizer.step()
        记录 loss.item()；每隔 logging_interval 个 batch 打印一次

    model.eval() + torch.no_grad()               # 外循环收尾：评估
    [v1] train/valid 的 acc 与 loss 全算 → 存入 log_dict
    [v2] 只算 acc；若 valid_acc 创新高 → 保存 state_dict 到 best_model_save_path
    [v2] 若传入 scheduler → scheduler.step(监控指标)

[v2] 结束后用 test_loader 计算一次测试准确率
返回日志
```

#### 4.1.3 源码精读

**v1 的日志字典与可替换损失函数**（[pytorch_ipynb/helper_train.py:L15-L32](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_train.py#L15-L32)）：函数开头预建 5 个键的 `log_dict`——每 batch 训练损失、每 epoch 训练准确率/损失、每 epoch 验证准确率/损失；`loss_fn` 缺省为 `F.cross_entropy`，但允许外部传入自定义损失（v2 没有这个口子）。

```python
log_dict = {'train_loss_per_batch': [],
            'train_acc_per_epoch': [],
            'train_loss_per_epoch': [],
            'valid_acc_per_epoch': [],
            'valid_loss_per_epoch': []}

if loss_fn is None:
    loss_fn = F.cross_entropy
```

**v1 的内循环就是五步范式**（[pytorch_ipynb/helper_train.py:L38-L56](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_train.py#L38-L56)）：前向得 logits、算损失、清零梯度、反向、更新，然后把 `loss.item()` 追加进日志。中间夹着一个分布式兼容分支——若 logits 是 RPC 远程引用 `RRef`（模型被切到别的进程/设备时会出现），先取回本地值再算损失：

```python
logits = model(features)
if isinstance(logits, torch.distributed.rpc.api.RRef):
    logits = logits.local_value()
loss = loss_fn(logits, targets)
optimizer.zero_grad()
loss.backward()
optimizer.step()
```

**v1 的逐 epoch 统计与开关**（[pytorch_ipynb/helper_train.py:L58-L76](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_train.py#L58-L76)）：在 `model.eval()` 与 `torch.set_grad_enabled(False)` 之下，对 train/valid 两个 loader 各算一次准确率与 epoch 损失并 `.item()` 存入字典。参数 `skip_epoch_stats=True` 可跳过这整段——想快速跑通、不关心中间指标时用它省时间。

**v2 的最优 checkpoint 逻辑**（[pytorch_ipynb/helper_train.py:L94-L136](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_train.py#L94-L136)）：开跑前 `best_valid_acc` 初始化为负无穷、`best_epoch` 为 0；每个 epoch 结束后比较验证准确率，创新高就记录 epoch 号并（若给了路径）`torch.save(model.state_dict(), best_model_save_path)`。注意保存的是**验证集上最优的那一版权重**，不是最后一个 epoch 的权重——这是对抗过拟合的最朴素的「早停式」选型手段：

```python
best_valid_acc, best_epoch = -float('inf'), 0
...
if valid_acc > best_valid_acc:
    best_valid_acc, best_epoch = valid_acc, epoch+1
    if best_model_save_path:
        torch.save(model.state_dict(), best_model_save_path)
```

**v2 的学习率调度接入**（[pytorch_ipynb/helper_train.py:L147-L154](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_train.py#L147-L154)）：`scheduler_on` 二选一——`'valid_acc'` 把该 epoch 验证准确率喂给 `scheduler.step()`（适合 `ReduceLROnPlateau` 这类「指标好就少动」的调度器），`'minibatch_loss'` 则喂最后一个 batch 的损失；其余取值直接抛 `ValueError`。

**v2 的收尾测试**（[pytorch_ipynb/helper_train.py:L156-L161](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_train.py#L156-L161)）：训练结束后立刻用 `test_loader` 算一次准确率并打印，最后把三个列表（每 batch 损失、每 epoch 训练/验证准确率）作为三元组返回。

一个容易踩的细节：收尾这次测试用的是**训练结束时的最终权重**，而不是刚保存的最优 checkpoint。所以 MobileNet notebook 在训练之后会手动 `model.load_state_dict(torch.load('mobilenet-v2-best-1.pt'))` 再测一次（见 [pytorch_ipynb/cnn/cnn-mobilenet-v2-cifar10.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/cnn/cnn-mobilenet-v2-cifar10.ipynb)）。另外注意两个函数的**位置参数顺序不同**：v1 是 `(num_epochs, model, optimizer, device, ...)`，v2 是 `(model, num_epochs, ...)`——混用时务必用关键字参数传参。

**v1 与 v2 速查表**

| 维度 | v1 | v2 |
| --- | --- | --- |
| 返回值 | 5 键的 `log_dict` | `(minibatch_loss_list, train_acc_list, valid_acc_list)` 三元组 |
| 每 epoch 记录 | 训练/验证的 acc **和 loss** | 仅训练/验证 acc（`skip_train_acc` 可再省掉训练 acc） |
| 损失函数 | 可传入自定义 `loss_fn` | 写死 `F.cross_entropy` |
| 最优 checkpoint | 无 | `best_model_save_path` 按验证 acc 保存 |
| 学习率调度 | 无 | `scheduler` + `scheduler_on` |
| 测试集评估 | 无（由 notebook 自己做） | 结束时自动打印一次 |
| 典型使用者 | 确定性基准、部分机制类 notebook | MobileNet 等新版模型 notebook |

#### 4.1.4 代码实践

**实践目标**：不写一行训练代码，只靠调用 v2 跑完一次训练，并确认「最优 checkpoint + 重载」流程可用。

1. 进入 `pytorch_ipynb/` 目录（helper 模块都在这层，notebook 在子目录时靠 `sys.path.insert(0, "..")` 引入它们）。
2. 新建一个 notebook 或脚本，依次执行（示例代码）：

```python
import sys, torch, torch.nn as nn
sys.path.insert(0, ".")          # 确保 pytorch_ipynb 在搜索路径中
from helper_utils import set_all_seeds
from helper_data import get_dataloaders_mnist
from helper_train import train_classifier_simple_v2

set_all_seeds(123)               # 必须在建模之前（见 4.4）
train_loader, valid_loader, test_loader = get_dataloaders_mnist(
    batch_size=128, validation_fraction=0.1)

model = nn.Sequential(nn.Flatten(),
                      nn.Linear(784, 128), nn.ReLU(),
                      nn.Linear(128, 10))
optimizer = torch.optim.Adam(model.parameters(), lr=0.001)

minibatch_loss_list, train_acc_list, valid_acc_list = train_classifier_simple_v2(
    model=model, num_epochs=3,
    train_loader=train_loader, valid_loader=valid_loader,
    test_loader=test_loader, optimizer=optimizer,
    best_model_save_path='practice-best.pt',
    device=torch.device('cpu'), logging_interval=100)
```

3. 训练结束后执行 `model.load_state_dict(torch.load('practice-best.pt'))`，再重新调用 `helper_evaluate.compute_accuracy(model, test_loader, device)` 对比重载前后的测试准确率。

**需要观察的现象**：日志里 `Best Validation (Ep. xxx)` 的 epoch 号是否总是 ≤ 当前 epoch；重载最优权重后测试准确率与训练结束时的打印值有何差异。

**预期结果**：3 个 epoch 内验证准确率应单调上升（最优 epoch ≈ 最后一个 epoch）；重载前后的测试准确率可能不同——差异来自「最终权重 ≠ 最优权重」。MNIST 上 MLP 三个 epoch 通常在 96%–97% 区间（待本地验证，具体数值随环境而变）。

#### 4.1.5 小练习与答案

**练习 1**：v2 里 `skip_train_acc=True` 时训练准确率记为什么值？为什么这样设计而不是跳过不记？

**答案**：记为 `float('nan')`（[pytorch_ipynb/helper_train.py:L125-L130](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_train.py#L125-L130)）。因为 `train_acc_list` 与 `valid_acc_list` 按下标对齐 epoch，如果跳过不 append，两条曲线会错位、`plot_accuracy` 也会画错；用 NaN 占位既保持长度一致，绘图时又是天然的「缺口」。

**练习 2**：为什么 v2 的收尾测试准确率不能当作模型的最终成绩？

**答案**：收尾测试用的是最后一个 epoch 的权重，而实验选型依据是验证集上最优的 checkpoint；若训练后期出现过拟合，最终权重的测试成绩会低于重载最优 checkpoint 后的成绩。规范做法是 `load_state_dict` 最优权重后再测一次。

**练习 3**：`scheduler_on='valid_acc'` 与 `'minibatch_loss'` 各适合什么场景？

**答案**：`'valid_acc'` 适合 `ReduceLROnPlateau` 这类以「监控指标停滞则降学习率」为逻辑的调度器，验证指标比单 batch 损失更平稳；`'minibatch_loss'` 把调度粒度细化到最后一个 batch 的损失，反应更快但噪声大，容易被单个坏 batch 误触发。

### 4.2 评估函数：compute_accuracy 与 compute_epoch_loss

#### 4.2.1 概念说明

`helper_evaluate.py` 是工具链的最底层（`helper_train` 第 1–2 行就从这里导入）。本讲关注两个逐 epoch 被调用的函数：

- `compute_accuracy`：在整个 loader 上数「预测标签 == 真实标签」的比例，返回百分数。
- `compute_epoch_loss`：把整个 loader 的交叉熵损失**按样本数加权平均**，返回该数据集上的平均损失。

#### 4.2.2 核心流程

准确率的定义：

\[
\text{Acc} = \frac{\#\{\hat{y}_i = y_i\}}{N} \times 100\%
\]

epoch 损失的关键在**分母用样本数而不是批次数**：

\[
L_{\text{epoch}} = \frac{1}{N}\sum_{i=1}^{N} \text{CE}(z_i, y_i),
\qquad N = \sum_{b} n_b
\]

其中 \(n_b\) 是第 \(b\) 个 batch 的样本数。若改成「先算每个 batch 的平均损失、再对 batch 求平均」，最后一个不满的 batch（样本数少于 batch_size）会获得与满 batch 相同的权重，引入系统性偏差。

#### 4.2.3 源码精读

**compute_accuracy**（[pytorch_ipynb/helper_evaluate.py:L7-L22](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_evaluate.py#L7-L22)）：函数内部先 `model.eval()`（即使外层忘了切模式也能得到正确行为），再在 `torch.no_grad()` 下遍历 loader：`torch.max(logits, 1)` 取每行最大值的索引作为预测标签，逐批累计 `num_examples` 与 `correct_pred`，最后相除乘 100。中间同样保留了 `RRef` 兼容分支：

```python
_, predicted_labels = torch.max(logits, 1)
num_examples += targets.size(0)
correct_pred += (predicted_labels == targets).sum()
...
return correct_pred.float()/num_examples * 100
```

注意返回值是 0 维张量（不是 float），所以 v1 用 `.item()` 转成 Python 数值再存日志。

**compute_epoch_loss 的 reduction='sum'**（[pytorch_ipynb/helper_evaluate.py:L25-L40](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_evaluate.py#L25-L40)）：每个 batch 的损失用 `reduction='sum'` 累成「该批损失总和」，与样本数分别累计，最后一次性相除——这正是上面公式的直接实现，天然消除小批次偏差：

```python
loss = F.cross_entropy(logits, targets, reduction='sum')
num_examples += targets.size(0)
curr_loss += loss
...
curr_loss = curr_loss / num_examples
```

（`F.cross_entropy` 默认 `reduction='mean'` 返回批内平均；对不等长 batch 逐批取 mean 再平均，就不再是全数据集的平均损失。）

#### 4.2.4 代码实践

**实践目标**：亲手验证「逐批平均再平均」与「先求和再除以样本数」在含不满尾批时的差异。

1. 构造一个 10 个样本、batch_size=4 的假数据集（示例代码）：

```python
import torch, torch.nn as nn, torch.nn.functional as F
from torch.utils.data import DataLoader, TensorDataset

torch.manual_seed(0)
logits = torch.randn(10, 3)                 # 10 个样本、3 类
targets = torch.randint(0, 3, (10,))
loader = DataLoader(TensorDataset(logits, targets), batch_size=4)
# 批次样本数: 4, 4, 2 —— 最后一批只有 2 个样本

model = nn.Identity()                        # 「模型」直接返回输入
per_batch_mean = [F.cross_entropy(m(x), y).item() for m, (x, y) in
                  zip([lambda x: x]*3, loader)]
naive = sum(per_batch_mean) / len(per_batch_mean)

from helper_evaluate import compute_epoch_loss
correct = compute_epoch_loss(model, loader, device=torch.device('cpu')).item()
print(f"naive={naive:.6f}  correct={correct:.6f}")
```

2. 对比打印出的 `naive` 与 `correct`，再手算 10 个样本损失的算术平均进行核对。

**需要观察的现象**：两个数值不相等；`correct` 与手算的全样本平均一致，`naive` 偏向尾批（2 个样本那批）的损失。

**预期结果**：差异量级通常在百分之几以内，但方向确定——尾批被放大了 2 倍权重（10 = 4+4+2，正确权重应为 2/10，naive 给了 1/3）。具体数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`compute_accuracy` 为什么要在函数内部再调一次 `model.eval()`？调用了它还需要在外层调吗？

**答案**：防御性设计——保证即便调用者忘记切换模式，Dropout/BatchNorm 也处于评估行为。外层调不调都不影响结果，但显式调用是更好的习惯（v1/v2 在调用它之前也都调了）。

**练习 2**：v2 训练循环里没有调用 `compute_epoch_loss`，逐 epoch 的损失统计去哪了？

**答案**：v2 只记录训练中的每 batch 损失（`minibatch_loss_list`）和准确率，不再逐 epoch 对整个训练集重算损失——这样每个 epoch 省掉一次对 train_loader 的完整前向；损失走势改由 `plot_training_loss` 的滑动平均曲线呈现。

**练习 3**：`compute_epoch_loss` 返回的张量为什么可以直接参与 `+=` 累加而不阻断梯度？

**答案**：整个循环包裹在 `torch.no_grad()` 中，所有前向结果都不建计算图，累加的只是数值本身，结束后除以 `num_examples` 返回的是无梯度的标量张量。

### 4.3 绘图工具：plot_training_loss 与 plot_accuracy

#### 4.3.1 概念说明

训练日志如果不画出来，几乎无法判断「模型是没学到、还是学过头了」。`helper_plotting.py` 提供两个基础图：

- `plot_training_loss`：**每 batch 损失的原始曲线 + 滑动平均曲线 + 双横轴（迭代数/epoch）**。
- `plot_accuracy`：训练/验证两条准确率曲线叠画，是观察过拟合（两线张口）的标准视图。

（同文件的 `show_examples`、`plot_confusion_matrix` 属于评估可视化，留到 u7-l1 专门讲。）

#### 4.3.2 核心流程

滑动平均用长度为 \(k\) 的均匀窗口对损失序列做**valid 卷积**：

\[
\tilde{L}_t = \frac{1}{k}\sum_{j=0}^{k-1} L_{t-j},
\qquad t = k-1, \dots, T-1
\]

窗口内取平均，输出长度变为 \(T-k+1\)（不补零，避免边缘被拉平）。双横轴的映射关系：迭代数 = epoch 号 × `iter_per_epoch`（即 `len(train_loader)` 的批数）。

#### 4.3.3 源码精读

**plot_training_loss**（[pytorch_ipynb/helper_plotting.py:L8-L50](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_plotting.py#L8-L50)）：先画原始每 batch 损失；若序列超过 1000 点，把 y 轴上限压到「第 1000 点之后最大值的 1.5 倍」（[L16-L19](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_plotting.py#L16-L19)）——训练初期损失往往很高，不裁剪会把后半段的细节压成一条直线；再用 `np.convolve` 画滑动平均（[L23-L26](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_plotting.py#L23-L26)）：

```python
ax1.plot(np.convolve(minibatch_loss_list,
                     np.ones(averaging_iterations,)/averaging_iterations,
                     mode='valid'),
         label='Running Average')
```

**双横轴**（[pytorch_ipynb/helper_plotting.py:L31-L43](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_plotting.py#L31-L43)）：`ax1.twiny()` 造出共用 y 轴的第二条 x 轴，刻度位置按 `epoch × iter_per_epoch` 换算、挪到底部外侧 45 磅处，于是同一张图上既可读「第几万次迭代」也可读「第几个 epoch」。`results_dir` 不为 None 时把图存成 PDF（[L48-L50](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_plotting.py#L48-L50)），目录需要事先存在。

**plot_accuracy**（[pytorch_ipynb/helper_plotting.py:L53-L71](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_plotting.py#L53-L71)）：epoch 号从 1 起作为 x 轴，训练与验证两条曲线各 `plt.plot` 一次，图例区分；注意它按 `len(train_acc_list)` 推断 epoch 数——如果用了 `skip_train_acc`，列表里是 NaN 占位，长度不受影响，曲线会出现缺口但不会错位。

#### 4.3.4 代码实践

**实践目标**：用 4.1.4 训练返回的三元组画出标准两张图，体会参数的作用。

1. 在 4.1.4 的代码之后追加（示例代码）：

```python
import matplotlib.pyplot as plt
from helper_plotting import plot_training_loss, plot_accuracy

plot_training_loss(minibatch_loss_list=minibatch_loss_list,
                   num_epochs=3,
                   iter_per_epoch=len(train_loader),
                   results_dir=None, averaging_iterations=50)
plt.show()

plot_accuracy(train_acc_list=train_acc_list,
              valid_acc_list=valid_acc_list, results_dir=None)
plt.show()
```

2. 把 `averaging_iterations` 从 50 改成 5 再画一次，对比平滑程度。
3. 想保存图，先 `os.makedirs('results', exist_ok=True)`，再传 `results_dir='results'`，检查生成 `plot_training_loss.pdf` 与 `plot_acc_training_validation.pdf`。

**需要观察的现象**：原始损失曲线抖动大、滑动平均近似单调下降；窗口越大平均曲线越平滑但越「滞后」。

**预期结果**：MNIST + MLP 前 3 个 epoch 损失从约 2.3 快速降到 0.2 以下；训练准确率曲线略高于验证准确率曲线（3 个 epoch 内两线尚未明显张口）。具体数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`plot_training_loss` 为什么需要调用方传 `iter_per_epoch=len(train_loader)`，而不是自己从损失列表推算？

**答案**：epoch 刻度是「每 `iter_per_epoch` 个 batch 对应 1 个 epoch」的线性换算；损失列表长度只反映总 batch 数，函数无从得知 loader 的批数（尤其训练被中断或 batch 数随数据变化时），由调用方提供最可靠。

**练习 2**：`np.convolve(..., mode='valid')` 与 `mode='same'` 在这里有何区别？

**答案**：`'valid'` 只保留窗口完全覆盖的输出（长度 \(T-k+1\)），平均窗口内恰好 k 个真实值；`'same'` 会隐式补零以保持长度为 T，序列末段窗口里混入零，会把平均损失人为拉低，产生假象。这里必须用 `'valid'`。

**练习 3**：`plot_accuracy` 里 `num_epochs = len(train_acc_list)`，若某次训练中途崩溃，图会怎样？

**答案**：画出的曲线只覆盖崩溃前已记录的 epoch——这其实是个优点：日志列表本身就是「崩溃现场」，拿它照样能画已完成部分的曲线，帮助诊断是在第几个 epoch 训练恶化的。

### 4.4 随机性与确定性：set_all_seeds 与 set_deterministic

#### 4.4.1 概念说明

「同一份代码跑两次结果不同」在深度学习里是常态，因为随机性来源很多。仓库的基准 notebook（[pytorch_ipynb/mechanics/deterministic_benchmark.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/deterministic_benchmark.ipynb)）把可复现性明确分成两个层次：

1. **设随机种子**（`set_all_seeds`）：让权重初始化、数据打乱、Dropout 掩码等随机数序列每次相同。必要且几乎零代价，但**不保证 GPU 上逐位复现**——cuDNN 的 benchmark 模式会为卷积自动挑选最快算法，其中一些算法的求和顺序不确定。
2. **开确定性模式**（`set_deterministic`）：禁用 cuDNN 的算法自动调优、强制选用确定性实现，追求逐位一致，代价是可能变慢、且个别算子根本没有确定性实现（会直接报错）。

一句话：**种子管「随机数从哪开始」，确定性开关管「算法本身是否可复现」**。

#### 4.4.2 核心流程

`set_all_seeds(seed)` 一次固定四个随机源并写一个环境变量：

| 调用 | 控制的随机性 |
| --- | --- |
| `random.seed(seed)` | Python 内置随机（部分数据集/增强库用到） |
| `np.random.seed(seed)` | NumPy 随机（标签打乱、噪声数据生成等） |
| `torch.manual_seed(seed)` | CPU 上的权重初始化、`torch.rand*` |
| `torch.cuda.manual_seed_all(seed)` | 所有 GPU 设备上的随机 |
| `os.environ["PL_GLOBAL_SEED"]` | PyTorch Lightning 据此为 DataLoader 的 worker 播种（u8-l1 会用到） |

`set_deterministic()` 做三件事：`cudnn.benchmark = False`（关闭算法自动调优）、`cudnn.deterministic = True`（卷积回退到确定性实现）、`torch.set_deterministic(True)`（把全局确定性要求扩展到其他算子）。

#### 4.4.3 源码精读

**set_all_seeds**（[pytorch_ipynb/helper_utils.py:L7-L12](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_utils.py#L7-L12)）：五个赋值逐一执行，无任何条件分支。注意调用时机——**必须在创建 DataLoader、初始化模型之前**，否则打乱顺序和初始权重已经消费过旧种子：

```python
def set_all_seeds(seed):
    os.environ["PL_GLOBAL_SEED"] = str(seed)
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
```

**set_deterministic**（[pytorch_ipynb/helper_utils.py:L15-L19](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/helper_utils.py#L15-L19)）：cuDNN 相关两行只在有 GPU 时有意义；`torch.set_deterministic(True)` 对所有设备生效：

```python
def set_deterministic():
    if torch.cuda.is_available():
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True
    torch.set_deterministic(True)
```

版本兼容提示：本仓库 notebook 保存的输出对应 torch 1.7.0（基准 notebook 的 watermark 可证）；`torch.set_deterministic` 在更新的 PyTorch 中已由 `torch.use_deterministic_algorithms(True)` 取代，且 CUDA ≥ 10.2 时还需设置环境变量 `CUBLAS_WORKSPACE_CONFIG=:4096:8`，详见 notebook 中引用的官方文档 https://pytorch.org/docs/stable/notes/randomness.html 。在新版 torch 上运行旧函数名可能直接报 `AttributeError`，属预期现象，替换为等价新 API 即可（待本地验证，取决于你安装的 torch 版本）。

**基准实验的实测结论**：[deterministic_benchmark.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/deterministic_benchmark.ipynb) 用 ResNet-101（实现来自同目录的 [deterministic_benchmark_utils.py](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/mechanics/deterministic_benchmark_utils.py)）在 CIFAR-10 上各训 50 个 epoch、batch_size=128、Adam lr=0.01，两次运行唯一的差别是是否先调用 `set_deterministic()`（种子都设为 1）。保存的输出显示：不开确定性 **61.16 min**、开确定性 **61.35 min**——作者结论是「在这个用例中，确定性行为对性能没有明显影响」。当然这只是单一模型/单一 GPU 的样本，不是普适定律：官方文档明确指出确定性算法可能慢于非确定性版本，具体取决于算子与硬件。

#### 4.4.4 代码实践

**实践目标**：体会在 CPU 与 GPU 上「同种子不同算法设置」的复现差异（无 GPU 也能完成前半部分）。

1. 把 4.1.4 的训练脚本封装成函数 `run_once(seed=123, deterministic=False)`：函数开头按需调用 `set_deterministic()`，随后 `set_all_seeds(seed)`，再建 loader 与模型、训练，最后 `return np.array(minibatch_loss_list)`（示例代码，结构示意）：

```python
def run_once(seed=123, deterministic=False):
    if deterministic:
        set_deterministic()
    set_all_seeds(seed)
    train_loader, valid_loader, test_loader = get_dataloaders_mnist(
        batch_size=128, validation_fraction=0.1)
    model = build_mlp(); optimizer = ...        # 与 4.1.4 相同
    minibatch_loss_list, _, _ = train_classifier_simple_v2(...)
    return np.array(minibatch_loss_list)
```

2. 同一进程内跑 `a = run_once(); b = run_once()`，打印 `np.array_equal(a, b)`。
3. 再跑 `c = run_once(deterministic=True); d = run_once(deterministic=True)`，同样比较。

**需要观察的现象**：CPU 上两组通常都严格相等（CPU 算子基本是确定性的，种子已把随机源对齐）；GPU 上第 1 组（只设种子）往往从若干个 batch 后开始出现微小数值分叉，第 2 组（加确定性开关）应逐位一致。

**预期结果**：两次运行的损失曲线要么完全重合（`array_equal` 为 True），要么只在若干位小数后分开；把两条曲线画在同一张图上，分叉点通常出现在第一个 epoch 之内。具体行为随 torch 版本与硬件而异，待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：只调用 `set_all_seeds(1)`、不调用 `set_deterministic()`，在 GPU 上跑两次，结果一定相同吗？为什么？

**答案**：不一定。种子只固定了随机数序列；`cudnn.benchmark` 默认开启时，cuDNN 会为每个卷积配置测量并选用最快算法，其中部分算法（如某些 atomicAdd 实现）的浮点累加顺序不确定，导致数值逐位漂移。需要逐位复现就必须同时关 benchmark、开 deterministic。

**练习 2**：`set_all_seeds` 为什么把种子写进 `os.environ["PL_GLOBAL_SEED"]`？普通 PyTorch 脚本需要它吗？

**答案**：这是给 PyTorch Lightning 留的钩子——Lightning 会读取该环境变量为 DataLoader 的每个 worker 派生种子，保证多进程加载时的随机变换也可复现（u8-l1 会见到）。纯 PyTorch 脚本不读它，这行等价于无操作，但留着无害且让同一份代码在两种框架下行为一致。

**练习 3**：如果某个模型用到了「没有确定性实现」的 CUDA 算子，开启 `set_deterministic()` 会发生什么？这算 bug 吗？

**答案**：会直接抛出异常提示该算子无确定性实现（`torch.set_deterministic` / `torch.use_deterministic_algorithms` 的设计如此）。不是 bug 而是「宁可失败也不悄悄不可复现」的设计取舍：此时要么换等价的确定性算子，要么接受非确定性并只用种子近似复现。

## 5. 综合实践

把本讲四个模块串成一个完整实验：**用共享工具链训练一个可复现的 MNIST 分类器，产出「最优权重 + 两张图 + 确定性对照结论」**。

在 `pytorch_ipynb/` 目录下新建 `practice_u4l4.py`（示例代码；需要已安装 torch、torchvision、matplotlib、numpy）：

```python
# 示例代码：整合 helper 工具链的完整训练脚本
import os, sys
import numpy as np
import torch, torch.nn as nn
import matplotlib.pyplot as plt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helper_utils import set_all_seeds, set_deterministic
from helper_data import get_dataloaders_mnist
from helper_train import train_classifier_simple_v2
from helper_evaluate import compute_accuracy
from helper_plotting import plot_training_loss, plot_accuracy

NUM_EPOCHS = 3
DEVICE = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')


def run_once(tag, deterministic=False, seed=123):
    if deterministic:
        set_deterministic()               # 先开确定性（若有 GPU）
    set_all_seeds(seed)                   # 再设种子：必须先于 loader 与模型

    train_loader, valid_loader, test_loader = get_dataloaders_mnist(
        batch_size=128, validation_fraction=0.1)

    model = nn.Sequential(nn.Flatten(),
                          nn.Linear(784, 128), nn.ReLU(),
                          nn.Linear(128, 10)).to(DEVICE)
    optimizer = torch.optim.Adam(model.parameters(), lr=0.001)

    path = f'practice-{tag}.pt'
    losses, train_acc, valid_acc = train_classifier_simple_v2(
        model=model, num_epochs=NUM_EPOCHS,
        train_loader=train_loader, valid_loader=valid_loader,
        test_loader=test_loader, optimizer=optimizer,
        best_model_save_path=path, device=DEVICE, logging_interval=100)

    # 图 1：batch 损失 + 滑动平均；图 2：训练/验证准确率
    plot_training_loss(minibatch_loss_list=losses, num_epochs=NUM_EPOCHS,
                       iter_per_epoch=len(train_loader),
                       results_dir=None, averaging_iterations=50)
    plt.savefig(f'practice-{tag}-loss.png'); plt.show()
    plot_accuracy(train_acc_list=train_acc, valid_acc_list=valid_acc,
                  results_dir=None)
    plt.savefig(f'practice-{tag}-acc.png'); plt.show()

    # 重载最优 checkpoint 再测一次（v2 收尾用的是最终权重）
    model.load_state_dict(torch.load(path))
    model.eval()
    acc = compute_accuracy(model, test_loader, device=DEVICE)
    print(f'[{tag}] best-checkpoint test acc: {acc:.2f}%')
    return np.array(losses)


if __name__ == '__main__':
    a = run_once('seed-only')                        # 第 1 遍：只设种子
    b = run_once('seed-only')                        # 第 2 遍：同设置重跑
    print('两次仅种子运行逐位一致:', np.array_equal(a, b))
    print('两次仅种子运行最大差异:', np.max(np.abs(a - b)))

    # 有 GPU 时再做确定性对照（CPU 上通常已逐位一致）
    if torch.cuda.is_available():
        c = run_once('deterministic', deterministic=True)
        d = run_once('deterministic', deterministic=True)
        print('两次确定性运行逐位一致:', np.array_equal(c, d))
```

**操作步骤**：

1. `cd pytorch_ipynb && python practice_u4l4.py`（MNIST 首次运行会自动下载；CPU 上 3 个 epoch 通常几分钟内完成）。
2. 收集产物：`practice-seed-only.pt`（最优权重）、`*-loss.png` / `*-acc.png`（两张图）、终端打印的逐位一致性判断。
3. 把 `max|a - b|` 与 `np.array_equal(a, b)` 的结果记入一张小表：仅种子（两次）、确定性（两次）各一行。

**预期结果**：最优 checkpoint 的测试准确率不低于 v2 收尾打印值；仅种子两次运行在 CPU 上 `array_equal` 为 True、差异为 0，在 GPU 上可能出现非零（但很小）的差异；确定性两次运行应严格一致。全部结论待本地验证——你观察到的正是你自己环境的行为，这与仓库基准 notebook「在特定硬件上实测后得出结论」的方法论一致。

## 6. 本讲小结

- `train_classifier_simple_v1` 返回 5 键 `log_dict`（含逐 epoch 训练/验证损失），损失函数可自定义；`train_classifier_simple_v2` 返回三个列表，新增**按验证准确率保存最优 checkpoint** 与 `scheduler_on` 调度接入，但损失写死为交叉熵。
- v2 的收尾测试用的是最终权重；报告成绩前应 `load_state_dict` 重载最优 checkpoint 再测。
- `compute_accuracy` 内部自带 `model.eval()` + `no_grad()`；`compute_epoch_loss` 用 `reduction='sum'` 先求和再除以样本总数，避免不满尾批被「批均值再平均」放大权重。
- `plot_training_loss` = 原始 batch 损失 + `np.convolve` 滑动平均（valid 模式）+ 迭代/epoch 双横轴 + 超 1000 点自动裁剪 y 轴；`plot_accuracy` 画训练/验证两线观察过拟合。
- 可复现有两个层次：`set_all_seeds` 固定四个随机源（须在 loader 与模型之前调用），`set_deterministic` 关闭 cuDNN 调优并强制确定性算法；前者零代价但不保证 GPU 逐位一致，后者可能变慢或对无确定性实现的算子直接报错。
- 仓库实测（ResNet-101/CIFAR-10/50 epoch）：开确定性 61.35 min 对不开 61.16 min，差异可忽略——但这是单一环境的样本，不是普适结论。

## 7. 下一步学习建议

- **u7-l1 评估工具链**：本讲只用了 `compute_accuracy`；`compute_confusion_matrix`、`plot_confusion_matrix`、`show_examples` 将在那一讲展开，配合本讲的 checkpoint 重载流程构成完整评估报告。
- **u7-l2 K 折交叉验证**：把「每折重置模型与优化器 + 复用本讲的训练函数」组合起来，理解为什么单次 train/valid/test 划分的结论不够稳。
- **u8-l1 PyTorch Lightning 基础**：Lightning 把本讲手工拼装的「训练循环 + 评估 + 日志 + checkpoint」整体接管，且会消费 `set_all_seeds` 写入的 `PL_GLOBAL_SEED` 环境变量——学完更能看清这套手工工具链的设计意图。
- 想深挖随机性的读者，建议精读 notebook 中引用的官方文档 https://pytorch.org/docs/stable/notes/randomness.html ，再回头对照 `helper_utils.py` 的 19 行代码，理解每个开关对应的底层机制。
