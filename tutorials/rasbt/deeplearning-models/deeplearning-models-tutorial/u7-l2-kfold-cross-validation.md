# K 折交叉验证

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清为什么「一次 `random_split` 切出验证集、训练一次、报一个分数」这种做法得到的是一个**随机数**：换一个划分、换一个种子，分数就会变；并理解 K 折交叉验证如何把单个数字升级为「均值 ± 标准差」这组更有信息量的估计。
2. 手写一个正确的 K 折循环：掌握索引构造（一次随机排列切成 K 段）、**每折重建模型与优化器**的必要性，以及「留出折」上的评估方式。
3. 读懂仓库用 `pl_cross` 库实现的 Lightning 版 5 折交叉验证：`Trainer(num_folds=5)` + `cross_validate()` 两行替代整个手写循环，同时知道它的三个坑——不调用 `validation_step`、需要配套的 logger、只支持 PyTorch Lightning < 1.6.0。

本讲是第 7 单元第二讲。上一讲（u7-l1）补齐了「训练后怎么评估、怎么诊断」，本讲回答一个更根本的问题：**你报出来的那个准确率，可信到什么程度？**

## 2. 前置知识

本讲默认你已完成 u4-l1（验证集划分）、u4-l4（训练日志与最优 checkpoint）和 u7-l1（评估工具链），以下概念直接沿用：

- **验证集与测试集的分工**：验证集供反复调参、可多次使用；测试集只在最后评一次。基线 notebook 用 `random_split` 从训练数据中切出验证集，正是 u4-l1 讲过的「单次留出法」。
- **五步训练范式**：forward → loss → zero_grad → backward → step；本讲基线 notebook 的训练循环仍是这套骨架。
- **最优 checkpoint 纪律**（u4-l4）：报告成绩前应重载验证集上最优的权重，而不是拿最后一个 epoch 的权重直接交卷。

本讲新引入的术语，先建立直觉：

- **K 折交叉验证（K-fold cross-validation）**：把（训练）数据切成 K 份，轮流留一份出来当验证集、用其余 K−1 份训练，共训练 K 次。每个样本都恰好被「验证」一次。
- **折（fold）**：切出来的一份子集。被留出来当验证集的那一份，仓库 notebook 称为 **test fold**（留出折）。
- **分层 K 折（stratified K-fold）**：切分时保证每折内各类别比例与整体一致，类别不平衡时尤其重要。
- **估计量思维**：单次划分的验证分数 = 从「所有可能划分」这个总体里抽的一个样本；K 折给你 K 个分数，于是能算均值（中心估计）和标准差（波动幅度）。

本讲还会第一次正式精读 **PyTorch Lightning** 代码（此前 u6-l6 只匆匆见过一次）。你只需要先记住三件套，细节留到 u8-l1 展开：

- **`LightningModule`**：把 PyTorch 模型、损失计算（`training_step` / `validation_step` / `test_step`）、优化器配置（`configure_optimizers`）打包成一个对象；
- **`Trainer`**：接管 epoch × batch 循环，`trainer.fit(...)` 一行启动训练；
- **`ModelCheckpoint(monitor=...)`**：按某个监控量自动保存最优权重，`trainer.test(..., ckpt_path="best")` 用最优权重测试。

最后是成本意识：K 折要把「训练一个模型」这件事重复 K 遍，深度学习里这是真金白银的时间。所以 K 折的定位通常是**超参数/模型选择阶段的多花点时间换可靠结论**，选定配置后仍按基线方式全量训练一个最终模型。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `pytorch_ipynb/kfold/baseline-cnn-mnist.ipynb` | PyTorch 版基线：`random_split` 单次 55000/5000 划分 + 手写训练循环，测试准确率 99.14%。是本讲手写 K 折实践的底版 |
| `pytorch-lightning_ipynb/kfold/baseline-light-cnn-mnist.ipynb` | Lightning 版基线：同一模型与数据，训练交给 `Trainer`，按 `valid_acc` 保存最优 checkpoint，测试准确率 99.26% |
| `pytorch-lightning_ipynb/kfold/kfold-light-cnn-mnist.ipynb` | 本讲主角：在 Lightning 基线之上引入 `pl_cross` 库，`Trainer(num_folds=5)` + `cross_validate()` 完成 5 折交叉验证 |

两个**运行时才下载的外部依赖**（不在本仓库内）：

- `rasbt/mnist-pngs`：MNIST 的 PNG 版数据仓库（三通道 RGB 图片，train 60000 张 / test 10000 张），三份 notebook 都会 `Repo.clone_from` 拉取到本地 `mnist-pngs/` 目录；
- `SkafteNicki/pl_cross`：实现 K 折的第三方 Lightning 扩展库，kfold notebook 同样以 clone + `setup.py install` 的方式安装。

## 4. 核心概念与源码讲解

本讲的三个最小模块：**基线划分**、**K 折循环**、**pl_cross 库**。

### 4.1 基线划分：一次 random_split 定乾坤

#### 4.1.1 概念说明

基线 notebook 的做法是全书最常见也最简单的「单次留出验证」：从 60000 张训练图中随机抽 55000 张训练、5000 张验证，另留 10000 张测试集最后只用一次。训练 10 个 epoch 后得到验证准确率 98.70%、测试准确率 99.14%。

问题在于：这串数字里藏着**两层随机性**。第一层来自划分本身——哪 5000 张图被抽去当验证集是随机的，换一个随机数状态，验证集里「难样本」与「易样本」的比例就会不同，分数随之波动；第二层来自训练过程——权重初始化、数据打乱、随机增强（`RandomCrop`）都会让每次训练走上略微不同的轨迹。单次运行的分数只是这些随机性的一次抽样。

K 折交叉验证正面回应第一层随机性：它让「每一段数据都轮流当一次验证集」，K 个分数的均值不再依赖某一次具体划分的运气，标准差则直接告诉你划分这个因素到底能带来多大波动。

#### 4.1.2 核心流程

PyTorch 基线的完整流程：

1. clone `mnist-pngs` 数据仓库到本地；
2. 定义两套 transform：训练用 `Resize(32) → RandomCrop(28)`（随机增强），测试用 `Resize(32) → CenterCrop(28)`（确定性裁剪），最后都 `Normalize` 到 [-1, 1]——这正是 u4-l3 讲过的「双 transform」模式；
3. `ImageFolder` 读入 60000 张训练图，`random_split` 随机切成 55000 + 5000 两份；测试集单独从 `mnist-pngs/test` 读 10000 张；
4. 三个 DataLoader：训练集 `shuffle=True, drop_last=True`，验证/测试集都不打乱；
5. 定义两层卷积的 `PyTorchCNN`（约 10.3 万参数）；
6. Adam + 交叉熵，手写五步循环训练 10 个 epoch，每个 epoch 结束在训练/验证集上报告准确率；
7. 训练结束后用**最终权重**（而非最优 checkpoint）在测试集上评估一次。

Lightning 基线的流程完全同构，差别只在工程形态：模型被包进 `LightningModel`，训练循环交给 `Trainer.fit`，并且按 `valid_acc` 保存最优 checkpoint、测试时用 `ckpt_path="best"` 重载。

#### 4.1.3 源码精读

**数据段：双 transform 与单次划分。** [pytorch_ipynb/kfold/baseline-cnn-mnist.ipynb:L127-L150](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/kfold/baseline-cnn-mnist.ipynb#L127-L150) 定义了 `"train"` / `"test"` 两套 transform。注意两个细节：一是 MNIST 原图 28×28，先放大到 32 再随机裁回 28，等于人为制造了 4 个像素的平移增强空间（测试集则用确定性的 `CenterCrop`，保证评估可复现）；二是 `Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))` 里有三个通道——因为 mnist-pngs 存的是 RGB 三通道 PNG，这也是后面模型 `in_channels=3` 的来源。

[pytorch_ipynb/kfold/baseline-cnn-mnist.ipynb:L155-L162](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/kfold/baseline-cnn-mnist.ipynb#L155-L162) 是本讲最核心的一行代码所在：`train_dset, valid_dset = random_split(train_dset, lengths=[55000, 5000])`。`random_split` 内部按数据集长度做一次随机排列，然后前 55000 个索引归训练子集、剩下 5000 个归验证子集，返回的是两个共享底层数据的 `Subset`。**划分结果取决于调用那一刻的全局随机数状态**——这就是单次划分随机性的直接来源。测试集则完全独立地从另一个目录读入。

**三个 DataLoader。** [pytorch_ipynb/kfold/baseline-cnn-mnist.ipynb:L173-L197](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/kfold/baseline-cnn-mnist.ipynb#L173-L197) 中训练 loader 开了 `drop_last=True`：55000 ÷ 128 ≈ 429.7，所以每 epoch 429 个完整 batch、丢弃 88 个样本（训练输出里的 `Batch: 100/429` 印证了这一点）；验证与测试 loader 不打乱、不丢尾批，保证评估覆盖每一个样本。

**模型。** [pytorch_ipynb/kfold/baseline-cnn-mnist.ipynb:L249-L286](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/kfold/baseline-cnn-mnist.ipynb#L249-L286) 的 `PyTorchCNN` 是「卷积块×2 + 两层全连接头」的标准结构：28×28×3 →（8 通道卷积+池化）→ 14×14×8 →（16 通道卷积+池化）→ 7×7×16，展平后 16×7×7 = 784 维接 `Linear(784, 128)` 再到 10 类。注意 [L289-L291](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/kfold/baseline-cnn-mnist.ipynb#L289-L291) 里 `torch.manual_seed(random_seed)` 紧挨着模型构造——**种子只管到初始化这一次**，这一点在 4.2 讲「每折重置」时会再次用到。

**训练与评估。** [pytorch_ipynb/kfold/baseline-cnn-mnist.ipynb:L393-L431](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/kfold/baseline-cnn-mnist.ipynb#L393-L431) 是五步范式的完整展开（Adam 优化器在 L393 创建，位于折循环之外的顶层——这个位置在 4.2 会成为关键讨论点）。评估用 [L307-L316](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/kfold/baseline-cnn-mnist.ipynb#L307-L316) 的 `compute_accuracy`：分子分母分别累计再相除，与 u7-l1 讲的「正确累计姿势」一致。最终 [L455-L456](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/kfold/baseline-cnn-mnist.ipynb#L455-L456) 直接拿训完的模型测测试集，输出 **99.14%**（[L450](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/kfold/baseline-cnn-mnist.ipynb#L450)）。

**Lightning 基线的三处不同。** 第一，模型被 [pytorch-lightning_ipynb/kfold/baseline-light-cnn-mnist.ipynb:L298-L358](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/kfold/baseline-light-cnn-mnist.ipynb#L298-L358) 的 `LightningModel` 包装：`_shared_step` 统一算 loss 与预测标签，`training_step` / `validation_step` / `test_step` 分别在三个阶段调用它，`configure_optimizers` 返回 Adam。注意 `configure_optimizers` 是**每次 `trainer.fit` 启动时被调用**的——优化器由框架在 fit 内部创建，这个设计在 Lightning 里天然满足「每次训练配新优化器」。第二，[L377-L387](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/kfold/baseline-light-cnn-mnist.ipynb#L377-L387) 配置了 `ModelCheckpoint(save_top_k=1, mode="max", monitor="valid_acc")`：按验证准确率保存最优一份权重。第三，[L596-L611](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/kfold/baseline-light-cnn-mnist.ipynb#L596-L611) 的 `trainer.fit` 之后，[L732](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/kfold/baseline-light-cnn-mnist.ipynb#L732) 用 `ckpt_path="best"` 重载最优权重再测试，得到 **99.26%**（[L716](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/kfold/baseline-light-cnn-mnist.ipynb#L716)）。

两份基线的模型、数据、超参完全相同，99.14% 与 99.26% 的差别主要来自**评估口径**（最终权重 vs 验证集最优权重）。这个对比再次印证 u4-l4 的纪律：报成绩前先说清楚测的是哪一份权重。

#### 4.1.4 代码实践

**实践目标**：不训练任何模型，直接「看见」划分本身的随机性——理解单次划分的分数为什么会波动。

**操作步骤**：

1. 打开 `pytorch_ipynb/kfold/baseline-cnn-mnist.ipynb`，先完整跑通一遍（clone 数据、划分、训练 10 epoch 约需数分钟 GPU / 更久 CPU，结果应接近 99.14%）。
2. 在新 cell 里用三个不同种子重复同一种划分，统计验证子集的类别分布（示例代码）：

```python
import numpy as np
import torch
from torchvision.datasets import ImageFolder

dset = ImageFolder(root="mnist-pngs/train", transform=data_transforms["test"])
targets = np.array(dset.targets)                # 全体 60000 个标签

for seed in [1, 2, 3]:
    torch.manual_seed(seed)
    _, valid_part = torch.utils.data.random_split(dset, lengths=[55000, 5000])
    valid_labels = targets[np.array(valid_part.indices)]   # 验证子集的标签
    counts = np.bincount(valid_labels, minlength=10)
    print(f"seed={seed} | 每类样本数 {counts.tolist()} | 极差 {counts.max()-counts.min()}")
```

**需要观察的现象**：三行输出的每类样本数各不相同（理想均匀时每类应为 500，实际会上下浮动几十个）。

**预期结果**：不同种子下验证集的类别构成确实在漂移。这就是「换划分 → 换分数」的微观机理；具体的波动幅度**待本地验证**（波动量级与样本量 5000 有关，可用二项分布标准差 \(\sqrt{500 \times 0.9 \times 0.1} \approx 6.7\) 粗估每类 ±7 左右的常态波动）。

#### 4.1.5 小练习与答案

**练习 1**：为什么验证集要从训练数据里切，而不是从 10000 张测试集里切？

**答案**：测试集的独立性是不可再生资源，只能在最终评估时使用一次。若从测试集里借数据当验证集，反复调参的过程已经让测试集信息泄漏进了模型选择，最终测试分数会偏乐观。基线 notebook 把 5000 张验证集从 60000 张训练图里切、测试集单独存放，正是这个原则的体现（呼应 u4-l1）。

**练习 2**：把 `random_split` 这一行原样再执行一次，两次得到的验证集一样吗？

**答案**：不一定一样。`random_split` 使用执行那一刻的全局随机数状态；只要两次执行之间随机状态没被重置（比如没有重新 `torch.manual_seed`），两次划分就不同。想复现划分，必须在其之前固定种子，或改用显式传索引的确定性划分方式。

**练习 3**：基线 notebook 测试得到 99.14%，这个数字能直接和 Lightning 基线的 99.26% 比较、得出「Lightning 更好」的结论吗？

**答案**：不能。两份 notebook 的模型与数据完全相同，差异主要在评估口径：前者用训练结束时的最终权重，后者重载验证准确率最高的 checkpoint。单次运行加不同口径，不足以支撑框架优劣的结论——这正是本讲要引入 K 折的原因之一。

### 4.2 K 折循环：索引构造与每折重置

#### 4.2.1 概念说明

K 折交叉验证把「划分」这件事从一次性决定变成系统性扫描：把 N 个训练样本随机排列后切成 K 段，第 i 轮（第 i 折）留出第 i 段当验证集、用其余 K−1 段训练，共训练 K 个模型。它换来两样东西：**均值**是对「这套训练配方在随机划分下平均表现」的估计，**标准差**直接量化了划分带来的波动。

深度学习里 K 折不如在传统机器学习中普及，原因很实际：训练一次就要几分钟到几天，K 折等于把成本乘以 K。所以它的正确定位是**模型/超参选择**——用 K 折比较「学习率 0.001 与 0.01 哪个好」比单次划分可靠得多，因为均值抹掉了划分运气，标准差还能告诉你两个配置的差距是否显著。

手写 K 折循环有两个最容易做错的地方，本模块的核心就是它们：

- **每折必须重新初始化模型**。K 个分数要成为 K 次「同一训练配方从零开始」的独立重复，模型权重就是配方的一部分。如果第 2 折沿用第 1 折训好的权重，它测的不再是「从零训练 10 个 epoch」这个配方，而且各折之间通过共享权重产生了关联，估计失真（通常偏乐观——后面的折起点更高）。
- **每折必须重建优化器**。Adam 为每个参数维护一阶动量 \(\hat{m}\) 与二阶动量 \(\hat{v}\) 两个缓冲区，它们是**绑定在旧模型损失面和旧步数上的状态**。给一个全新初始化的模型配一个「记着旧模型梯度历史」的优化器，动量估计与当前损失面不匹配，训练早期行为不可控。基线 notebook 里 `optimizer = torch.optim.Adam(...)` 写在循环外顶层（[L393](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/kfold/baseline-cnn-mnist.ipynb#L393)），改造时必须把它挪进折循环。

还有一个容易忽略的口径问题：每折的训练集只有全量的 (K−1)/K（5 折时 80%），所以 **CV 均值与「用全量数据训练」的成绩不可直接比较**——4.3 节会看到 98.50% 的 CV 均值低于 99.14% 的基线，训练数据变少是原因之一。

#### 4.2.2 核心流程

索引构造与折循环的伪代码：

```text
indices = 0..N-1 的一个随机排列
folds = 把 indices 切成 K 段（每段 N/K 个，前 N mod K 段多 1 个）

for i in 0..K-1:
    valid_idx = folds[i]
    train_idx = folds[其余 K-1 段拼接]
    用 train_idx / valid_idx 构造两个 DataLoader
    重置随机种子；重新初始化模型            # 关键！
    重新创建优化器                         # 关键！
    训练 num_epochs 个 epoch
    在留出折上评估 → acc_i

CV 分数 = mean(acc_1..acc_K) ± std(acc_1..acc_K)
```

样本量与折大小的算术：N = 55000、K = 5 时每折恰好 11000，每折训练 44000、验证 11000；N = 60000 时每折 12000。若 N 不能整除 K，`np.array_split` 的约定是前 \(N \bmod K\) 折每折多 1 个样本。

均值与标准差按下式计算（\(\text{acc}_k\) 为第 k 折留出折准确率）：

\[
\bar{a} = \frac{1}{K}\sum_{k=1}^{K} \text{acc}_k,
\qquad
s_{\text{pop}} = \sqrt{\frac{1}{K}\sum_{k=1}^{K}(\text{acc}_k-\bar{a})^2},
\qquad
s_{\text{sample}} = \sqrt{\frac{1}{K-1}\sum_{k=1}^{K}(\text{acc}_k-\bar{a})^2}
\]

注意分母的两种取法：除以 K 是总体标准差，除以 K−1 是带 Bessel 修正的样本标准差。PyTorch 的 `tensor.std()` 默认用后者（`unbiased=True`）——4.3.4 的实践会拿仓库 notebook 的真实输出验证这一点。

#### 4.2.3 源码精读

本仓库没有「手写 K 折循环」的 notebook（自动版在 4.3），所以本节做的是**基于真实基线代码的改造**：三个可复用组件全部来自 baseline-cnn-mnist，改动点用「示例代码」标注。

**组件一：双数据集共享同一套索引。** 基线的 `random_split` 一步切出两个 Subset，但两个 Subset 挂的是**同一个** dataset 对象、同一套 transform——训练子集也被迫用不上随机增强的独立控制。K 折循环里我们沿用 u4-l1 讲过的 helper_data 思路：对同一个图片目录构造**两个** `ImageFolder`，一个挂 `data_transforms["train"]`（随机增强，供训练折用），一个挂 `data_transforms["test"]`（确定性变换，供留出折评估用）。关键是 `ImageFolder` 对同一目录的样本枚举顺序是确定的（类别按名称排序、类内文件按名称排序），所以**同一个整数索引在两个 Dataset 里指向同一张 PNG**，只是施加的变换不同（示例代码）：

```python
# 复用基线 notebook 已定义的 data_transforms（L127-L150）
from torchvision.datasets import ImageFolder
from torch.utils.data import Subset, DataLoader

dset_train_tf = ImageFolder(root="mnist-pngs/train", transform=data_transforms["train"])
dset_eval_tf  = ImageFolder(root="mnist-pngs/train", transform=data_transforms["test"])
assert dset_train_tf.samples == dset_eval_tf.samples   # 顺序一致，索引可互换
```

**组件二：索引构造。** `np.array_split` 把一个随机排列切成 K 段（示例代码）：

```python
import numpy as np
N_FOLDS = 5
rng = np.random.RandomState(random_seed)
indices = rng.permutation(len(dset_eval_tf))     # 60000 个索引的一次随机排列
folds = np.array_split(indices, N_FOLDS)         # 5 段，每段 12000
```

**组件三：折循环内重建一切。** 训练部分整体照抄基线的 [L393-L431](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/kfold/baseline-cnn-mnist.ipynb#L393-L431)（模型定义 [L249-L286](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/kfold/baseline-cnn-mnist.ipynb#L249-L286)、评估函数 [L307-L316](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/kfold/baseline-cnn-mnist.ipynb#L307-L316) 原样复用），唯一结构性改动是把「建模型 + 建优化器」从顶层挪进折循环，见 4.2.4 的完整代码。

**顺带读懂 pl_cross 版的一个数据流细节。** 看 kfold notebook 的数据段 [pytorch-lightning_ipynb/kfold/kfold-light-cnn-mnist.ipynb:L219-L224](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/kfold/kfold-light-cnn-mnist.ipynb#L219-L224)：它原样保留了基线的 `random_split(lengths=[55000, 5000])`，而后面 `cross_validate` 拿到的 `train_loader` 包装的是切完后的 55000 张子集。也就是说，pl_cross 的 5 折是在 **55000** 张图上进行的（每折 11000：训练 44000 / 留出 11000），那 5000 张 `valid_dset` 在整个 K 折运行中实际**闲置**——这是从基线复制粘贴留下的脚手架。它不影响正确性（留出折本身就是验证角色），但提醒我们：读 notebook 要顺着变量追数据流，而不是假设每段代码都参与了主流程。

#### 4.2.4 代码实践

**实践目标**：把 PyTorch 基线改造成 5 折交叉验证循环，亲手完成「每折重建模型与优化器」，并报告各折留出折准确率的均值与标准差。

**操作步骤**：

1. 复制 `pytorch_ipynb/kfold/baseline-cnn-mnist.ipynb` 为自己的 notebook（或新 notebook），保留其 Imports / Settings / transforms / `PyTorchCNN` / `compute_accuracy` 各段。
2. **删掉**原 `random_split` 那一段，替换为下面的索引构造与折循环（示例代码；`device`、`num_epochs` 等沿用基线 Settings）：

```python
import numpy as np
from torch.utils.data import Subset, DataLoader
from torchvision.datasets import ImageFolder

N_FOLDS = 5

dset_train_tf = ImageFolder(root="mnist-pngs/train", transform=data_transforms["train"])
dset_eval_tf  = ImageFolder(root="mnist-pngs/train", transform=data_transforms["test"])

rng = np.random.RandomState(random_seed)          # 划分只依赖这一个种子
indices = rng.permutation(len(dset_eval_tf))      # 60000 个索引
folds = np.array_split(indices, N_FOLDS)          # 每折 12000

fold_accs = []
for i in range(N_FOLDS):
    valid_idx = folds[i]
    train_idx = np.concatenate([folds[j] for j in range(N_FOLDS) if j != i])

    fold_train_loader = DataLoader(
        Subset(dset_train_tf, train_idx), batch_size=batch_size,
        drop_last=True, shuffle=True, num_workers=4)
    fold_valid_loader = DataLoader(
        Subset(dset_eval_tf, valid_idx), batch_size=batch_size,
        drop_last=False, shuffle=False, num_workers=4)

    torch.manual_seed(random_seed + i)           # 每折重置随机源（初始化/增强可复现）
    model = PyTorchCNN(num_classes=num_classes).to(device)          # 每折重建模型
    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)  # 每折重建优化器

    for epoch in range(num_epochs):
        model.train()
        for features, targets in fold_train_loader:
            features, targets = features.to(device), targets.to(device)
            logits = model(features)
            loss = F.cross_entropy(logits, targets)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

    acc = compute_accuracy(model, fold_valid_loader, device)
    fold_accs.append(acc.item())
    print(f"Fold {i+1}/{N_FOLDS} | 留出折准确率: {acc*100:.2f}%")

accs = np.array(fold_accs)
print(f"\n5 折 CV: {accs.mean()*100:.2f}% ± {accs.std(ddof=1)*100:.2f}%")
```

3. 先把 `num_epochs` 临时改成 1 做一次冒烟测试（约 5 倍单次训练时长），确认整条链路跑通后再改回 10。
4. 记录每折的准确率、均值与标准差，并留作 4.3 对照。

**需要观察的现象**：每折打印的准确率彼此略有不同；总耗时约为单次训练的 4–5 倍。

**预期结果**：参考量级——每折在 48000 张图上训练、12000 张留出折上评估，准确率应在 98%–99% 附近波动，标准差在 0.2 个百分点量级（与 pl_cross 版的 ±0.23% 同量级）。具体数值**待本地验证**（取决于硬件与版本；本实践折的是全部 60000 张训练图，而 pl_cross 版折的是 55000 张子集，两者数字只能定性对照）。

#### 4.2.5 小练习与答案

**练习 1**：60000 个样本做 5 折，每折的训练集和留出集各多少张？如果样本数是 60002 呢？

**答案**：60000 ÷ 5 = 12000，每折留出 12000、训练 48000。60002 不能整除：`np.array_split` 会把前 \(60002 \bmod 5 = 2\) 折各分 12001 张、后 3 折各 12000 张。划分逻辑对不整除的情形天然兼容，只是各折大小差 1。

**练习 2**：忘记重建优化器（模型每折重建了）会出什么问题？

**答案**：Adam 的动量缓冲 `exp_avg` / `exp_avg_sq` 记录的是上一个模型损失面上的梯度一阶、二阶矩，且其偏差修正依赖累计步数。把这些状态套在新初始化的模型上，等于让优化器带着「旧地形的记忆」在新地形上走，初期更新方向和步长都被污染，各折也不再是从同一配方出发的独立重复。最干净的写法就是每折 `optimizer = torch.optim.Adam(...)` 重建一次。

**练习 3**：你用 5 折 CV 得到均值 98.6%，同事用同样的代码在全部 60000 张图上训练一次、在测试集上得到 99.2%，谁的成绩「更好」？

**答案**：这两个数字回答的不是同一个问题，不可直接比。CV 均值估计的是「用 80% 数据训练 + 在留出折上评估」这个流程的平均表现；99.2% 是「100% 数据训练 + 独立测试集评估」。K 折的价值在于比较配置（换学习率、换结构后均值和标准差怎么变），而不是产出最终部署模型。选定配置后应回到基线方式用全量数据重训（4.3 节末尾仓库 notebook 正是这么建议的）。

### 4.3 pl_cross 库：把 K 折装进 Trainer

#### 4.3.1 概念说明

`pl_cross` 是一个第三方 PyTorch Lightning 扩展（GitHub 上的 SkafteNicki/pl_cross），它把 K 折循环整个装进 `Trainer`：折的切分、每折的模型与优化器重建、训练、留出折评估、结果聚合全部自动化，你的代码只需从 `pl_cross` 导入 `Trainer`、多传一个 `num_folds` 参数，再把 `trainer.fit` 换成 `trainer.cross_validate`。作为对照：4.2 手写的约 40 行折循环，pl_cross 用约 5 行完成。

代价是引入了一个**仓库外的、绑定旧版本 Lightning 的依赖**：notebook 开头明确说明它目前只支持 PyTorch Lightning < 1.6.0（[kfold-light-cnn-mnist.ipynb:L33-L42](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/kfold/kfold-light-cnn-mnist.ipynb#L33-L42)，作者环境是 1.5.10，新版本需要降级）。用第三方库换便利时，这种版本锁定是要付的利息。

#### 4.3.2 核心流程

kfold notebook 相对 Lightning 基线只改了四处：

1. clone 并安装 pl_cross 库；
2. 日志器从 `pytorch_lightning.loggers.CSVLogger` 换成 `pl_cross.loggers.CSVLogger`（K 折下的适配版）；
3. checkpoint 的监控量从 `valid_acc` 改为 `train_acc`；
4. `pl.Trainer` 换成 `pl_cross.Trainer`，传入 `num_folds=5`、`shuffle=True`（可选 `stratified=True`），用 `trainer.cross_validate(model, train_dataloader)` 启动。

`cross_validate` 内部对传入 dataloader 背后的数据集做 K 折切分，逐折「训练 + 留出折评估」，最后返回一个带 `mean` / `std` / `raw` 三个层次指标的字典。作者保存的输出显示 5 折共耗时 6.85 分钟（单次 `trainer.fit` 是 2.00 分钟）。

#### 4.3.3 源码精读

**安装与版本限制。** [pytorch-lightning_ipynb/kfold/kfold-light-cnn-mnist.ipynb:L142-L147](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/kfold/kfold-light-cnn-mnist.ipynb#L142-L147) 用 gitpython 把 pl_cross clone 到本地，随后注释里的 `python setup.py install`（[L162](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/kfold/kfold-light-cnn-mnist.ipynb#L162)）完成安装（L33 的 markdown 同时给出 `pip install pytorch-lightning==1.5.10` 的降级提示）。注意 pl_cross **不是 pip 可直接安装的**，所以 notebook 走了源码安装这条路。

**改动一：logger 与 checkpoint 监控量。** [pytorch-lightning_ipynb/kfold/kfold-light-cnn-mnist.ipynb:L452-L474](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/kfold/kfold-light-cnn-mnist.ipynb#L452-L474) 中，CSVLogger 改从 `pl_cross.loggers` 导入（L456），而 [L469-L473](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/kfold/kfold-light-cnn-mnist.ipynb#L469-L473) 的 `ModelCheckpoint` 把监控量换成了 `train_acc`。原因写在 [L444](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/kfold/kfold-light-cnn-mnist.ipynb#L444) 与代码注释 [L465](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/kfold/kfold-light-cnn-mnist.ipynb#L465)：**pl_cross 的 K 折不会调用 `validation_step`**，`valid_acc` 根本不会被记录，监控它没有意义；留出折走的是 `test_step`（模型定义里的 `test_step` 因此派上用场，见 [L307](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/kfold/kfold-light-cnn-mnist.ipynb#L307) 的说明）。`LightningModel` 本身与基线逐字相同，`validation_step` 留在类里不删也无妨。

**改动二：pl_cross 的 Trainer 与 cross_validate。** [pytorch-lightning_ipynb/kfold/kfold-light-cnn-mnist.ipynb:L712-L738](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/kfold/kfold-light-cnn-mnist.ipynb#L712-L738) 是全 notebook 的点睛之笔：`from pl_cross import Trainer`（L715）之后，`Trainer(num_folds=5, shuffle=True, ...)`（L716-L727）——`num_folds=5` 声明 5 折，`shuffle=True` 表示切折前先打乱索引（L720 注释掉的 `stratified=True` 即分层选项），其余 `max_epochs` / `callbacks` / `logger` 等参数与普通 Trainer 完全一样。随后 [L732-L735](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/kfold/kfold-light-cnn-mnist.ipynb#L732-L735) 一行 `kfold_results = trainer.cross_validate(model=lightning_model, train_dataloader=train_loader)` 启动整个 K 折。作者保存的输出（[L531-L707](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/kfold/kfold-light-cnn-mnist.ipynb#L531-L707)）里能看到 `Starting fold 1/5` 到 `Starting fold 5/5`，每折各有一个 Training 和一个 Testing 阶段——后者就是留出折评估。

**结果解读。** [pytorch-lightning_ipynb/kfold/kfold-light-cnn-mnist.ipynb:L706-L707](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/kfold/kfold-light-cnn-mnist.ipynb#L706-L707) 的返回值（[L757](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/kfold/kfold-light-cnn-mnist.ipynb#L757) 再次打印）有三组指标，每组都有 `mean` / `std` / `raw` 三个键：

- `train_acc_raw = [0.9883, 0.9892, 0.9890, 0.9896, 0.9898]`——每折训练集上的准确率；
- `test_acc_raw = [0.9818, 0.9836, 0.9868, 0.9875, 0.9855]`——**每折留出折上的准确率**（notebook 在 [L745](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/kfold/kfold-light-cnn-mnist.ipynb#L745) 称之为 test-fold accuracies，注意它不是 `mnist-pngs/test` 那 10000 张的成绩）；
- 汇总：`test_acc_mean = 0.9850`，`test_acc_std = 0.0023`，即 **98.50% ± 0.23%**。

这组数字信息量很大：五折极差约 0.57 个百分点（98.18% → 98.75%），这就是 4.1 说的「划分随机性」的实测幅度——同一配方只是换划分，成绩就差了半个多点。相比之下 CV 均值 98.50% 稳定得多。它与基线 99.14%/99.26% 的差距不能解读为「K 折更差」：每折只有 44000 张训练图（基线 55000 张），且评估发生在 11000 张留出折上而非独立测试集，口径不同（4.2 练习 3 的结论再用一次）。耗时上，6.85 分钟约为单次 fit（2.00 分钟）的 3.4 倍——比朴素的 5 倍少，因为每折训练集只有原来的 4/5，每 epoch 的 batch 数也随之变少。

**K 折之后怎么办。** [pytorch-lightning_ipynb/kfold/kfold-light-cnn-mnist.ipynb:L778-L803](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/kfold/kfold-light-cnn-mnist.ipynb#L778-L803) 给出两条后路：一是把 CV 结果当作超参选择的依据，选定配置后回到基线方式（[baseline-cnn-mnist.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/kfold/baseline-light-cnn-mnist.ipynb) 的普通 Trainer 流程）用全量数据重训最终模型；二是把 K 个折模型组成集成（L787 的 `trainer.create_ensemble` + L796 的 `trainer.test`），但这两行被注释掉了——作者在 [L803](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/kfold/kfold-light-cnn-mnist.ipynb#L803) 注明它当时对 checkpoint 文件报错，「疑似 bug，等修复后更新」。读源码时遇到这种注释要保持同样的诚实：功能存在 ≠ 功能可用。

#### 4.3.4 代码实践

**实践目标**：只用 notebook 已保存的输出，验证 `cross_validate` 返回的 `mean`/`std` 与 `raw` 之间的换算关系，并确认 torch 的默认标准差是 Bessel 修正版。

**操作步骤**：

1. 从 [kfold-light-cnn-mnist.ipynb:L757](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/kfold/kfold-light-cnn-mnist.ipynb#L757) 抄下 `test_acc_raw` 的五个数字，在任何有 PyTorch 的环境里运行（示例代码）：

```python
import torch

raw = torch.tensor([0.9818, 0.9836, 0.9868, 0.9875, 0.9855])
print(f"mean        : {raw.mean():.4f}")   # 对照 test_acc_mean = 0.9850
print(f"std (Bessel): {raw.std():.4f}")    # 默认 unbiased=True，对照 0.0023
print(f"std (总体)   : {raw.std(unbiased=False):.4f}")   # 除以 K 而非 K-1
```

2. 对照输出：`mean` 应为 0.9850、Bessel 版 `std` 应为 0.0023，与 notebook 报告一致；总体标准差略小（约 0.0021）。
3. （可选，需要 PL ≤ 1.5.10 环境）完整运行 kfold notebook，比较你机器上的 `test_acc_raw` 与作者保存的五个数字，并解释差异来源。

**需要观察的现象**：两种 `std` 的差异随 K 变大而缩小（K=5 时差约 5%）。

**预期结果**：第 2 步的换算可以在任何环境精确复现（这是纯算术）；第 3 步的数值会因随机性与硬件不同而不同，**待本地验证**——这正是本讲的主题：换一个环境/种子，分数就有波动，所以要用「均值 ± 标准差」说话。

#### 4.3.5 小练习与答案

**练习 1**：`kfold_results` 里的 `test_acc` 是 `mnist-pngs/test` 上 10000 张图的准确率吗？

**答案**：不是。它是每折**留出折**（test fold）上的准确率——pl_cross 在留出折上调用的是 `test_step`，所以键名叫 `test_acc`。独立的 10000 张测试集在整个 `cross_validate` 过程中从未被动用；若要测它，得用 L796 被注释掉的 `trainer.test(model=ensemble_model, dataloaders=test_loader)`（或用最优配置重训后按基线方式测）。

**练习 2**：为什么 kfold notebook 的 `ModelCheckpoint` 监控 `train_acc` 而不是 `valid_acc`？这有什么副作用？

**答案**：因为 pl_cross 的 K 折不调用 `validation_step`，`valid_acc` 不会被记录，监控一个不存在的量会让 checkpoint 失效甚至报错，所以退而求其次监控 `train_acc`。副作用是「最优」的判据变成了训练集表现，不再带验证集的泛化信号——这正是 K 折框架里「留出折只在折末评估一次」的设计所付出的代价。

**练习 3**：类别不平衡的数据（如 1:99 的正负比）用 `shuffle=True` 的普通 K 折有什么风险？notebook 里给出了什么对策？

**答案**：风险是随机切折可能让某些折里少数类极少甚至缺席，该折的估计极不稳定。对策是 [L720](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch-lightning_ipynb/kfold/kfold-light-cnn-mnist.ipynb#L720) 注释掉的 `stratified=True`：分层 K 折保证每折的类别比例与整体一致。

**练习 4（思考题）**：pl_cross 拿到的 `train_loader`，其 dataset 挂的是随机增强版 transform（`RandomCrop`）。若留出折的评估样本也经由这个 dataset 取出，会对每折的 `test_acc` 产生什么影响？

**答案**：留出折图片会带上随机平移增强，相当于在一个「更难、且带随机性」的评估集上测，每折分数会略微偏低且多一个噪声源。本仓库 notebook 未展开 pl_cross 内部的处理方式（该库不在仓库内），这一点可作为延伸阅读 pl_cross 源码时的检查点——这也解释了 4.2 手写版里「双 Dataset、留出折挂确定性 transform」的做法更严谨。

## 5. 综合实践

**综合任务**：为同一个 `PyTorchCNN` 配方建立一份「成绩可信度报告」，把本讲三个模块串起来。

具体步骤：

1. **基线（4.1）**：跑通 PyTorch 基线，记录单次划分的验证准确率与测试准确率（参考值 98.70% / 99.14%）。
2. **手写 5 折（4.2）**：按 4.2.4 的循环做 5 折（冒烟时 `num_epochs=1`），记录每折留出折准确率、均值与标准差。
3. **反例对照**：把折循环里「重建模型与优化器」的两行删掉（模型与优化器都移到循环外），再跑一次 5 折。观察第 2 折起每个折的起点与最终分数如何变化。
4. **汇总成表**（示例表格模板）：

| 方案 | 训练数据量/次 | 评估对象 | 结果 |
| --- | --- | --- | --- |
| 单次划分 | 55000 | 独立测试集 10000 | 99.14%（单点值） |
| 手写 5 折 | 48000 | 留出折 12000 | 均值 ± 标准差（待本地验证） |
| 手写 5 折（不重置） | — | — | （观察是否虚高） |
| pl_cross 5 折（作者输出） | 44000 | 留出折 11000 | 98.50% ± 0.23% |

5. **写三句结论**：(a) 单次划分的分数在划分随机性下波动多大？(b) 不重置模型对 CV 估计造成了什么方向的偏差？(c) 如果下一步要在两个学习率之间做选择，你会用哪种评估方式，为什么？

预期：不重置的反例中，第 2–5 折从上一折的权重出发，留出折准确率通常会系统性偏高且逐折上升——它测的已经不是「从零训练」的配方。整套实验 GPU 上约半小时量级，CPU 上建议把 `num_epochs` 与 `N_FOLDS` 减半先跑通；具体数值**待本地验证**。

## 6. 本讲小结

- 单次 `random_split` 划分得到的验证/测试分数是随机量：哪些样本落入验证集、初始化与随机增强都参与抖动；基线（99.14%/99.26%）只是这个随机过程的一次抽样。
- K 折交叉验证把数据切成 K 段轮流留出，输出「均值 ± 标准差」：均值抹掉划分运气，标准差实测划分波动（本仓库 pl_cross 版为 98.50% ± 0.23%，五折极差约 0.57 个百分点）。
- 手写 K 折循环的三个关键：一次随机排列切 K 段的索引构造、**每折重建模型**（保证 K 次独立重复）、**每折重建优化器**（Adam 动量缓冲绑定旧损失面）；训练与留出评估分别挂随机增强与确定性两套 transform（双 Dataset 共享索引）。
- CV 均值与全量训练的成绩不可直接比较：每折训练集只有 (K−1)/K，评估对象是留出折而非独立测试集。
- pl_cross 把 K 折装进 `Trainer(num_folds=5)` + `cross_validate()`，代价是版本锁定（仅支持 PL < 1.6.0）、`validation_step` 不被调用（checkpoint 只能监控 `train_acc`）、需要配套的 `pl_cross.loggers`；其 `create_ensemble` 在 notebook 中因疑似 bug 被注释停用。
- K 折的正确角色是**超参/模型选择**：选定配置后回到基线流程，用全量数据重训最终模型，测试集只碰最后一次。

## 7. 下一步学习建议

- 下一讲 **u7-l3（训练技巧：循环学习率、梯度裁剪与 batch 退火）**与本讲直接衔接：K 折是「用可靠的评估比较配置」的方法论，LR Range Test 与循环学习率正是「待比较的配置」本身——学完 u7-l3 后，可以回头用本讲的 5 折循环去比较固定学习率与 CLR 两组配置的均值与标准差，做一次真实的超参选择实验。
- 想补 Lightning 三件套的系统讲解，跳到 **u8-l1（PyTorch Lightning 基础）**；届时再回看本讲 4.3 的 `LightningModel` 与 checkpoint 配置会完全通透。
- 复习方向：u4-l1（为什么共享数据、双 transform 的设计贯穿 `helper_data` 与本讲的手写 K 折）、u7-l1（混淆矩阵可以按折输出，进一步看每折「错成什么」是否一致——这是一个很好的延伸小实验）。
