# 有序回归：CORAL 与 CORN

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清**有序回归（ordinal regression）**是什么问题：标签有先后顺序、但相邻标签之间的「距离」未知，它夹在普通分类与普通回归之间。
2. 理解把交叉熵（CE）分类器直接用在有序标签上的缺陷：它把「错 1 档」与「错 4 档」当成同等的错误。
3. 掌握 **CORAL** 的核心设计——共享权重 + 独立偏置的输出层：输出 \(K-1\) 个二元任务，靠结构（而不是损失惩罚）保证秩一致性。
4. 掌握 **CORN** 的另一条路线——条件概率 + 链式法则分解：输出层退回普通 `Linear`，秩概率用 `cumprod` 还原。
5. 会把累积概率转换回最终的有序类别预测，并用 MAE/MSE 评估有序模型——这是本讲代码实践的核心任务。

## 2. 前置知识

### 2.1 什么是有序回归

想象预测水泥的强度等级：1 级最弱，5 级最强。这些标签有三个特点：

- **有序**：3 级比 2 级强，「差一档」是一个真实的概念；
- **间距未知**：2 级与 3 级的强度差，未必等于 3 级与 4 级的强度差；
- **类别数有限**：不是任意实数。

这类问题既不是标准分类，也不是标准回归：

| 路线 | 隐含假设 | 用在有序标签上的毛病 |
| --- | --- | --- |
| 多分类（softmax + CE） | 类别之间毫无关系 | 把「预测 2 级真实 3 级」与「预测 5 级真实 3 级」视为同等错误，顺序信息完全浪费 |
| 回归（MSE） | 标签是等距数值 | 强行假设「档距相等」，且预测可能落在 2.7 这类无意义的档位上 |
| **有序回归** | 有序、间距未知 | 在两者之间建模——正是本讲主题 |

类似的问题到处都是：电影评分（1~5 星）、年龄组估计、疼痛等级、信用评级。u2-l2 讲的 MNIST 恰恰**不是**有序数据（数字 1 和 2 的图像之间没有「大小」关系），这也是本讲换用水泥强度数据集的原因。

### 2.2 关键直觉：把有序问题拆成一串二元问题

CORAL 与 CORN 共享同一个出发点，叫**扩展二元分类（extended binary classification）**：把 \(K\) 类有序问题改写成 \(K-1\) 个二元问题——

\[
\text{任务 } k:\quad \text{「标签 } y \text{ 是否大于 } k \text{？」}\qquad k = 0, 1, \dots, K-2
\]

以 \(K=5\) 为例，一个真实标签 \(y=3\) 的样本，其扩展二元标签是：

\[
[\,y>0,\; y>1,\; y>2,\; y>3\,] = [1, 1, 1, 0]
\]

顺序信息就藏在这串编码里：标签越大，前面连续的 1 越多。两个方法分道扬镳的地方在于——**这 \(K-1\) 个二元任务的输出头怎么设计、损失怎么算**：

- **CORAL**：所有任务共享同一个权重向量，只有偏置不同（秩一致性靠结构保证）；
- **CORN**：各任务独立输出，但建模的是**条件**概率，用链式法则还原（秩一致性靠概率乘积保证）。

### 2.3 需要回忆的旧知识

- **u2-l3 的 `nn.Module` 写法**：本讲的 MLP 骨架（两个隐藏层 + 输出层）与当年完全同款；
- **u2-l1/u2-l2 的五步训练循环**：forward → loss → `zero_grad` → `backward` → `step`，本讲三个 notebook 的训练循环仍是它；
- **u4-l2 的 Dataset 三要素**：本讲的 `MyDataset` 是最小实现，可对照回忆；
- **u3-l5 / u7-l4 的教训**——「读源码要以代码为准，不要以文字为准」：本讲会再次撞见四个现成案例（重复语句、死代码、超参不一致、复制粘贴残留的说明文字）。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [pytorch_ipynb/ordinal/baseline_cement.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/ordinal/baseline_cement.ipynb) | CE 基线：普通 5 类 softmax 分类器，用于对照 |
| [pytorch_ipynb/ordinal/CORAL_cement.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/ordinal/CORAL_cement.ipynb) | CORAL 主线：notebook 内贴出了 `CoralLayer` 的完整源码 |
| [pytorch_ipynb/ordinal/CORN_cement.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/ordinal/CORN_cement.ipynb) | CORN 主线：损失来自 `coral_pytorch`，结尾演示秩概率还原 |

三个 notebook 的数据准备部分（第 0~1 节）几乎逐行相同：下载水泥数据、划分、标准化、`MyDataset` + `DataLoader`；差异全部集中在**输出层、损失函数与预测转换**三处——这正好让我们做「只改一处」的受控对比阅读。目录下还有 `beckman2016`、`niu2016`、`polat2022` 三份姊妹 notebook（其他学者提出的有序回归方法），思路相近，可作延伸阅读。

一个风格提示：这三个 notebook 都不使用共享 helper 模块，训练循环、评估函数全部内联——延续仓库「自包含 notebook」的风格。CORAL 与 CORN 依赖第三方库 `coral_pytorch`（`#!pip install coral-pytorch`），本讲会同时看到「库导入」与「源码内联」两种形态。

## 4. 核心概念与源码讲解

### 4.1 有序回归问题定义与 CE 基线

#### 4.1.1 概念说明

基线模型是我们最熟悉的普通多分类器：MLP 最后一层输出 5 个 logits，用 softmax + 交叉熵训练，预测时取 `argmax`。它完全不感知标签的顺序。

为什么它是有序任务的「不合格」基线？交叉熵只要求**正确类的概率最大**。对一个真实标签为 3 的样本，把概率质量挪给 2 类还是挪给 0 类，损失下降一样多——梯度不区分「近错」与「远错」。评估时我们却用 MAE（平均绝对误差）打分，MAE 明确认为错 1 档比错 4 档好。**训练目标与评估标准错位**，就是 CE 基线的结构性缺陷，也正是 CORAL/CORN 要修复的东西。

顺带认识数据集：水泥强度（cement_strength）数据，998 个样本、8 个连续特征（V1~V8，成分配比）、5 个有序强度等级。原始标签是 1~5，代码统一减 1 变成 0~4（PyTorch 的损失函数要求标签从 0 开始编号）。

#### 4.1.2 核心流程

```text
读取 CSV ──标签减 1──▶ 0~4 五档
        │
        ▼
train_test_split(test_size=0.2, stratify=标签)   ← 划分保持各档比例
        │
        ▼
StandardScaler：仅在训练集 fit，两个集都 transform   ← u4-l3 的防泄漏原则
        │
        ▼
MyDataset + DataLoader（batch 128，训练集 shuffle）
        │
        ▼
MLP: 8 → 300 → 300 → 5 logits
        │
        ▼
cross_entropy(logits, class_labels) 训练 50 epoch
        │
        ▼
argmax 预测 ──▶ MAE / MSE 评估（标签差当距离）
```

#### 4.1.3 源码精读

数据准备三步，与 sklearn 时代的经典流程一致：

- [pytorch_ipynb/ordinal/baseline_cement.ipynb:L51-L66](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/ordinal/baseline_cement.ipynb#L51-L66) —— 从 GitHub 拉取 CSV；`data_df["response"] - 1` 把标签 1~5 平移到 0~4；`data_features` 取 V1~V8 八列。输出确认：8 个特征、998 个样本、标签 `[0 1 2 3 4]`。
- [pytorch_ipynb/ordinal/baseline_cement.ipynb:L82-L93](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/ordinal/baseline_cement.ipynb#L82-L93) —— `train_test_split` 留出 20% 做测试集，`random_state=1` 固定划分，`stratify=data_labels.values` 按标签分层抽样，保证 5 个强度档在训练/测试集中比例一致（样本量不到一千，分层很重要）。
- [pytorch_ipynb/ordinal/baseline_cement.ipynb:L106-L110](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/ordinal/baseline_cement.ipynb#L106-L110) —— `StandardScaler` 只在训练集上 `fit_transform`，测试集只 `transform`——正是 u4-l3 讲过的「标准化统计量只能来自训练集」。

超参与数据装置：

- [pytorch_ipynb/ordinal/baseline_cement.ipynb:L148-L168](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/ordinal/baseline_cement.ipynb#L148-L168) —— 全局设置：`random_seed=1`、`learning_rate=0.001`、`num_epochs=50`、`batch_size=128`、`NUM_CLASSES=5`、device 自动选择。**记住这组数**，4.2 会发现 CORAL 版在同一位置改了两个值。
- [pytorch_ipynb/ordinal/baseline_cement.ipynb:L172-L193](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/ordinal/baseline_cement.ipynb#L172-L193) —— `MyDataset` 三要素最小实现：`__init__` 把特征转成 `float32`、`__getitem__` 返回 `(inputs, label)`、`__len__` 返回样本数。对照 u4-l2：标签保持原样（int64 的 numpy 数组），由 DataLoader 的默认 collate 自动堆成张量——交叉熵要求标签是整数类型，这里刚好满足。

模型与训练循环：

- [pytorch_ipynb/ordinal/baseline_cement.ipynb:L250-L284](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/ordinal/baseline_cement.ipynb#L250-L284) —— MLP 定义：`Linear(8,300,bias=False) → LeakyReLU → Dropout(0.2) → BatchNorm1d` 两个隐藏层，末尾 `Linear(300, 5)` 输出 5 个 logits（L271）。就是 u2-l3/u2-l4 的老朋友，唯一区别是数据换成 8 维表格特征。随后 `torch.manual_seed` 固定随机性、模型搬上 device、Adam 优化器。
- [pytorch_ipynb/ordinal/baseline_cement.ipynb:L357-L373](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/ordinal/baseline_cement.ipynb#L357-L373) —— 标准五步训练循环：`logits = model(features)` → `cross_entropy` → `zero_grad` → `backward` → `step`。**读源码发现之一**：L364 与 L366 是两行一模一样的 `logits = model(features)`——第二次调用覆盖了第一次的结果，无害但冗余，是复制粘贴的痕迹。这类小瑕疵不影响结论，但提醒我们「notebook 也是人写的代码」。

评估与结果：

- [pytorch_ipynb/ordinal/baseline_cement.ipynb:L396-L424](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/ordinal/baseline_cement.ipynb#L396-L424) —— `compute_mae_and_mse`：在 `torch.no_grad()` 下遍历 loader，`torch.argmax(logits, dim=1)` 取预测档位（L413），累加 \(|\hat{y}-y|\) 与 \((\hat{y}-y)^2\) 再除以样本数。**读源码发现之二**：L396-L399 的 `beckham_logits_to_labels` 是死代码——它调用的 `beckham_logits_to_predictions` 在整个 notebook 里没有定义（一旦调用必然 `NameError`，好在从未被调用）。它明显是从姊妹 notebook `beckham2016_cement.ipynb` 复制过来的残留。
- [pytorch_ipynb/ordinal/baseline_cement.ipynb:L449-L450](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/ordinal/baseline_cement.ipynb#L449-L450) —— 基线成绩：**MAE（训练/测试）0.22 | 0.37**，MSE 0.27 | 0.41。这组数字是与后两个方法对比的锚点。

#### 4.1.4 代码实践

**实践目标**：亲手跑通 CE 基线，并体会「CE 对错档方向不敏感」。

**操作步骤**：

1. 安装依赖后从仓库根目录启动 `jupyter notebook`，打开 `pytorch_ipynb/ordinal/baseline_cement.ipynb` 逐 cell 运行（数据从网络下载，无需手动准备）。
2. 跑通后，把 `random_state=1`（[L86](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/ordinal/baseline_cement.ipynb#L86) 附近）改成 `2` 与 `3`，重新运行整个 notebook，记录三次的测试 MAE。
3. （可选，只读实验）在评估 cell 前插入一个诊断 cell，统计「预测比真实大几档 / 小几档」的分布：

```python
# 示例代码：错档方向分布（接在 notebook 评估段之后运行）
import torch
diffs = []
with torch.no_grad():
    for features, targets in test_loader:
        pred = model(features.to(DEVICE)).argmax(dim=1).cpu()
        diffs += (pred - targets).tolist()
import collections
print(collections.Counter(diffs))  # 键为 预测-真实 的档差
```

**需要观察的现象**：三次不同划分的测试 MAE 在 0.3~0.4 之间波动——样本不到一千时单次划分的运气不可忽视；错档分布集中在 -1/0/+1，但两侧长尾确实存在。

**预期结果**：测试 MAE 约 0.37（与本 notebook 保存的输出一致）；你的 GPU/版本不同可能带来小数点后第二位的差异。若网络受限下载失败，需手动下载 CSV 并替换读取路径，其余不变。

#### 4.1.5 小练习与答案

**练习 1**：为什么评估用 MAE 而不用准确率？CE 分类器的准确率能达到很高，说明什么？

**答案**：MAE 把「错 1 档」计 1 分、「错 3 档」计 3 分，度量的是错误的**Ordinal 距离**；准确率只看对错，0/1 计分，错 1 档和错 4 档同罚。对有序任务，两个准确率相同的模型，MAE 可以差很远——MAE 才与「强度估计得准不准」的业务含义对齐。

**练习 2**：把本 notebook 的 `NUM_CLASSES` 改成 4 会发生什么？

**答案**：`cross_entropy` 会直接报错（标签 4 越界：合法标签是 0~3）。`NUM_CLASSES` 决定输出层宽度与合法标签范围，必须与数据实际档数一致；这个「必须相等」的约束在 4.2 会被 CORAL 的一个真实笔误打破，值得记住。

**练习 3**：`MyDataset` 里为什么特征要 `astype(np.float32)`，而标签不用转？

**答案**：PyTorch 的默认张量类型是 `float32`，`nn.Linear` 等层的权重也是 `float32`，特征不转会在 `model(features)` 处报 dtype 不匹配（`float64` vs `float32`）。标签交给 `cross_entropy` 时要求整数类型，numpy 的 int 数组经 collate 自动变成 `int64` 张量，刚好合规。

### 4.2 CORAL 层与损失：共享权重 + 独立偏置

#### 4.2.1 概念说明

CORAL（**CO**nstant **R**ank-consistent **A**pproach to ordinal regression **L**ayers，Cao, Mirjalili & Raschka 2020）把扩展二元分类落到实处：\(K-1\) 个二元任务的 logit 定义为

\[
\text{logit}_k(x) = f(x) + b_k,\qquad k=0,\dots,K-2
\]

其中 \(f(x)\) 是**共享的**单输出线性打分 \(w^\top h(x)\)（\(h(x)\) 是倒数第二隐藏层输出），\(b_k\) 是**各自独立**的偏置。对每个任务单独做二元交叉熵，加起来就是 CORAL 损失：

\[
\mathcal{L}_{\text{CORAL}} = -\sum_{k=0}^{K-2}\Big[\, \mathbb{1}[y>k]\log\sigma(f(x)+b_k) + \mathbb{1}[y\le k]\log\big(1-\sigma(f(x)+b_k)\big) \Big]
\]

它的妙处在**秩一致性（rank consistency）是结构白送的**。注意每个任务输出的累积概率：

\[
P(y>k\mid x) = \sigma\big(f(x)+b_k\big)
\]

sigmoid 单调，所以只要偏置有序 \(b_0 \ge b_1 \ge \dots \ge b_{K-2}\)，就有 \(P(y>0\mid x)\ge P(y>1\mid x)\ge\dots\)——而「标签越大、超过它的概率越小」正是有序数据的基本公理。CORAL 论文证明：共享权重的结构让偏置在训练中保持这一顺序。对比之下，若给每个任务配独立权重（老方法 Niu et al. 2016 的做法），每个二元分类器各学各的，完全可能学出 \(P(y>0|x) < P(y>1|x)\) 这种自相矛盾的概率。

预测时从累积概率回收标签。因为 \(y>k\) 对 \(k=0,\dots,y-1\) 全真、对 \(k\ge y\) 全假，最自然的规则是**数一数有多少个任务认为「超过」**：把 \(\sigma(\text{logit}_k)>0.5\) 的任务个数作为预测档位。4.2.4 的实践就是亲手实现这个转换。

与基线相比，改动的只有输出层与损失：softmax 消失了，取而代之的是 \(K-1\) 个独立的 sigmoid。

#### 4.2.2 核心流程

```text
整数标签 y ──levels_from_labelbatch──▶ 扩展二元标签向量（K-1 维，前 y 个 1 后面全 0）
                                                        │
隐藏层输出 h(x) ── CoralLayer ──▶ K-1 个 logit：logit_k = w·h(x) + b_k
                                                        │
                                    coral_loss(logits, levels)：K-1 个 BCE 之和
                                                        │
五步训练循环（其余与基线完全相同）
                                                        │
评估：sigmoid(logits) ──▶ 累积概率 ──proba_to_label──▶ 预测档位 ──▶ MAE
```

#### 4.2.3 源码精读

先看超参——**读源码发现之三**（本讲最重要的一处）：

- [pytorch_ipynb/ordinal/CORAL_cement.ipynb:L150-L170](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/ordinal/CORAL_cement.ipynb#L150-L170) —— 同一位置的设置 cell：`learning_rate = 0.05`（L157，是基线/CORN 的 **50 倍**）、`NUM_CLASSES = 10`（L162，而数据实际只有 **5** 类）。这不像有意为之——更像从某个 10 类数据集的模板改写时的笔误。它并不让代码崩溃（原因见下），却悄悄改变了模型与训练动力学。笔记：这是 u3-l5「比较模型必须对齐配方」的又一现成案例——三个 notebook 的成绩严格说**不是**受控对比。
- 为什么 `NUM_CLASSES=10` 不报错？`levels_from_labelbatch(标签, num_classes=10)` 会生成 **9** 维扩展标签（任务为「y>0」…「y>8」）；标签最大是 4，于是任务 5~8 的目标恒为 0——模型花了 4 个输出头学习「永假」的常量任务，`coral_loss` 也在 9 项上求和。旁证：训练日志的初始损失约 7.88，与 9 个未训练二元任务（每个约 \(\ln 2 \approx 0.69\)，加上偏置预初始的影响）的量级相符；若只有 4 个任务，初始损失应在 3 附近。

输出层 `CoralLayer`——本 notebook 最值得逐行读的 30 行：

- [pytorch_ipynb/ordinal/CORAL_cement.ipynb:L267-L320](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/ordinal/CORAL_cement.ipynb#L267-L320) —— 先 `from coral_pytorch.layers import CoralLayer`（L267），**然后又在 cell 里完整重新定义了一遍 `class CoralLayer`**（L270 起）。Python 顺序执行，后定义的类覆盖同名导入——所以实际运行的就是眼前这份贴出来的源码（两种形态在这里恰好一致，但要知道生效的是哪份）：
  - [L299](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/ordinal/CORAL_cement.ipynb#L299) `self.coral_weights = torch.nn.Linear(size_in, 1, bias=False)` ——「共享权重」：无论 \(K\) 多大，打分器只有一个输出、一组权重；
  - [L301-L302](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/ordinal/CORAL_cement.ipynb#L301-L302) `torch.arange(num_classes-1, 0, -1).float() / (num_classes-1)` ——「独立偏置」的预初始化：从大到小的等差序列，如 5 类时 \([1.0, 0.75, 0.5, 0.25]\)，天生有序，docstring（L287-L293）说明这样初始化学习更快、泛化更好；
  - [L320](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/ordinal/CORAL_cement.ipynb#L320) `return self.coral_weights(x) + self.coral_bias` —— 广播把 1 维打分加上 \(K-1\) 维偏置，得到 \(K-1\) 个 logit。整个「共享 + 独立」设计浓缩在一行加法里。

模型组装——改动之小一目了然：

- [pytorch_ipynb/ordinal/CORAL_cement.ipynb:L324-L363](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/ordinal/CORAL_cement.ipynb#L324-L363) —— MLP 骨架与基线逐行相同（两个 300 宽隐藏层），只是 `Sequential` 在第二个隐藏层处截断，输出换成 `self.fc = CoralLayer(size_in=300, num_classes=NUM_CLASSES)`（L345），`forward` 里 `logits = self.fc(x)`（L352）。markdown（[L247-L249](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/ordinal/CORAL_cement.ipynb#L247-L249)）明确提醒：**用 sigmoid，不要 softmax**——每个任务独立过 sigmoid，不存在跨类的归一化竞争。

训练循环——两处替换，五步范式不变：

- [pytorch_ipynb/ordinal/CORAL_cement.ipynb:L458-L495](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/ordinal/CORAL_cement.ipynb#L458-L495) —— `levels_from_labelbatch(class_labels, num_classes=NUM_CLASSES)` 把整数标签转成扩展二元标签（L468-L469），`coral_loss(logits, levels)` 计算损失（L477）。对比基线循环：只是 `cross_entropy(logits, class_labels)` 换成了「先转 levels、再 coral_loss」，其余（含日志格式）原封未动。

评估与结果：

- [pytorch_ipynb/ordinal/CORAL_cement.ipynb:L511-L545](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/ordinal/CORAL_cement.ipynb#L511-L545) —— `compute_mae_and_mse` 与基线同名同形，只换预测一行：`probas = torch.sigmoid(logits)`（L526）得累积概率，`predicted_labels = proba_to_label(probas)`（L527）转回档位。`proba_to_label` 来自 `coral_pytorch.dataset`，内部实现正是 4.2.1 说的「数超阈值任务数」——4.2.4 我们自己写一个并和它对账。
- [pytorch_ipynb/ordinal/CORAL_cement.ipynb:L563-L564](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/ordinal/CORAL_cement.ipynb#L563-L564) —— 成绩：**MAE（训练/测试）0.28 | 0.36**，MSE 0.29 | 0.38。测试 MAE 略优于基线 0.37——但鉴于 lr 与 NUM_CLASSES 双重不一致，这个对比只能当参考，对齐配方后的复验留给综合实践。

#### 4.2.4 代码实践

**实践目标**：亲手实现「CORAL 累积概率 → 最终有序档位」的转换函数，并与官方 `proba_to_label` 对账（这是本讲规格指定的核心实践）。

**操作步骤**：

1. 运行 CORAL notebook 至训练完成（或加载你训好的模型），新建 cell：

```python
# 示例代码：自实现 CORAL 累积概率 → 档位
import torch
from coral_pytorch.dataset import proba_to_label  # 官方实现，用于对账

def my_proba_to_label(probas: torch.Tensor) -> torch.Tensor:
    """probas: [batch, K-1] 的累积概率 P(y>0), P(y>1), ..."""
    # 每个任务独立投票：概率 > 0.5 视为「标签超过该档」
    votes = (probas > 0.5).float()
    # 标签 = 被认为「超过」的任务数（前 y 个任务应为 1）
    return votes.sum(dim=1)

# 构造人工样例验证逻辑：K=5，四个样本
fake = torch.tensor([[0.9, 0.8, 0.7, 0.1],   # 前 3 个任务超阈 → 档位 3
                     [0.9, 0.2, 0.1, 0.05],  # 只有任务 0 → 档位 1
                     [0.1, 0.05, 0.0, 0.0],  # 全否 → 档位 0
                     [0.9, 0.8, 0.7, 0.6]])  # 全是 → 档位 4
assert torch.equal(my_proba_to_label(fake), torch.tensor([3., 1., 0., 4.]))
assert torch.equal(my_proba_to_label(fake).long(),
                   proba_to_label(fake))    # 与官方实现对账

# 再在真实模型输出上对账
with torch.no_grad():
    probas = torch.sigmoid(model(next(iter(test_loader))[0].to(DEVICE)))
print(torch.equal(my_proba_to_label(probas).long(), proba_to_label(probas)))
```

2. 若第 2 个 `assert` 失败，打印两者不一致的行与对应概率，推断官方实现的差异（提示：比较 `sum` 与 `cumsum+argmax` 两种语义在「恰好等于 0.5」处的行为）。

**需要观察的现象**：人工样例的断言直接通过；真实输出上两个函数逐样本一致；注意概率向量天然非递增——共享权重 + 递减偏置的结构保证（可以打印几行验证，几乎看不到递增的反例）。

**预期结果**：两个 `assert` 均通过，`True` 被打印。官方实现的具体写法可用 `inspect.getsource(proba_to_label)` 查看（待本地验证——以你安装的 `coral_pytorch` 版本实际源码为准）。

#### 4.2.5 小练习与答案

**练习 1**：`CoralLayer` 的参数量随类别数 \(K\) 怎么增长？和基线的 `Linear(300, K)` 比呢？

**答案**：`coral_weights` 是 `Linear(size_in, 1, bias=False)`，参数 \(= \text{size\_in}\)；偏置是 \(K-1\) 维。总共 \(\text{size\_in} + K - 1\)，即 300 维隐藏层、5 类时仅 304 个参数；基线 `Linear(300, 5)` 是 \(300\times5+5=1505\) 个。类别越多差距越大——共享权重把「每个类一套权重」压缩成「全体类一套打分器 + 每类一个门槛」。

**练习 2**：为什么偏置预初始化用**递减**序列而不是全零？全零会怎样？

**答案**：全零初始化时所有任务起点相同（\(P(y>k)=\sigma(f(x))\) 全相等），任务间没有先验的次序梯度，早期训练里「永真任务」（小 k）与「永假任务」（大 k）要自己把偏置拉开，收敛更慢。递减初始化让一开始就有 \(b_0>b_1>\dots\)，配合 docstring 所说学习更快、泛化更好。注意秩一致性本身由共享权重结构保证，初始化只是给了一个好的起点。

**练习 3**：本 notebook 的 `NUM_CLASSES=10` 若改成 20，训练与评估会分别发生什么？

**答案**：仍不报错——输出头变成 19 个 logit，`levels` 变 19 维，其中任务 5~18 的目标恒 0；浪费参数与算力，且损失被一堆「学永假」的任务稀释。评估端 `proba_to_label` 对 19 维累积概率数票，预测档位仍只会在 0~4 出现（因为任务 5 以后永远投不出「超过」票——除非训练不充分），MAE 大体仍可计算。这说明扩展二元框架对「类别数写大」是静默容忍的，比 CE 的显式报错更隐蔽。

### 4.3 CORN 条件概率：链式法则分解

#### 4.3.1 概念说明

CORN（**CO**nditional **R**ank-consistent **N**eural network，Shi, Cao & Raschka 2021）对同一串二元任务换了一种提问方式。CORAL 的任务 \(k\) 直接问无条件问题「\(P(y>k)\)」；CORN 改问**条件**问题：

\[
\text{任务 } k:\quad P\big(y > k \,\big|\, y > k-1\big)
\]

即「已知标签超过了 \(k-1\)，它也超过 \(k\) 的概率是多少？」。链式法则（概率乘法法则）把条件概率串回无条件的累积概率：

\[
P(y>k\mid x) = \prod_{j=0}^{k} P\big(y>j\,\big|\,y>j-1,\,x\big),\qquad \text{约定 } P(y>0\mid y>-1)=P(y>0)
\]

这正是 notebook 结尾那句「sigmoid 得条件概率、链式法则得任务概率」的数学含义。好处有两层：

1. **无需共享权重**。每个任务一个独立的 logit（普通 `Linear` 输出 \(K-1\) 维即可），表达能力比 CORAL 的单标量打分更强；秩一致性由 `cumprod`（连乘非负概率）自动成立——乘出来的序列天然非递增。
2. **损失只算「有定义」的任务**。对真实标签 \(y\) 的样本：任务 \(0..y-1\) 的事件确定为真（目标 1）、任务 \(y\) 的事件确定为假（目标 0）、而任务 \(y+1..K-2\) 的**条件**（\(y>y\)）已知不成立，条件概率无定义，**不参与损失**。这份「相关性掩码」由 `corn_loss` 内部处理，调用者无感。

对比一句话记牢：**CORAL 用结构（共享打分器）换秩一致，CORN 用条件化（乘积链）换秩一致**；两者都不需要 softmax，都在 \(K-1\) 个二元任务上训练，最后都把累积概率换算回档位。

#### 4.3.2 核心流程

```text
隐藏层输出 ──Linear(→K-1)──▶ logits（每维 = 一个条件任务的 logit）
                                    │
        整数标签 y ──corn_loss(logits, y, K)──▶ 掩码 BCE：任务 0..y 目标 1/0，
                                    │          任务 y+1 之后不参与损失
                                    ▼
                            五步训练循环（不变）
                                    │
评估/秩概率：logits ──sigmoid──▶ 条件概率 P(y>k|y>k-1)
                        ──cumprod(dim=1)──▶ 累积概率 P(y>0), P(y>1), ...
                        ──(>0.5 计数)──▶ 预测档位 ──▶ MAE
```

#### 4.3.3 源码精读

设置与模型——改动比 CORAL 还小：

- [pytorch_ipynb/ordinal/CORN_cement.ipynb:L150-L170](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/ordinal/CORN_cement.ipynb#L150-L170) —— 设置恢复成 `learning_rate = 0.001`（L156）与 `NUM_CLASSES = 5`（L161），与基线一致——这让 CORN 与基线的对比比 CORAL 那份更「干净」。
- [pytorch_ipynb/ordinal/CORN_cement.ipynb:L255-L289](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/ordinal/CORN_cement.ipynb#L255-L289) —— MLP 定义：与基线**完全同一个类**，唯一一行差异在输出层（[L274-L275](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/ordinal/CORN_cement.ipynb#L274-L275)）：`torch.nn.Linear(num_hidden_2, (num_classes-1))`——普通线性层，宽度从 \(K\) 改成 \(K-1\)。markdown（[L303](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/ordinal/CORN_cement.ipynb#L303)）说明：条件化与链式法则全部封装在损失函数里。

训练循环——一行损失：

- [pytorch_ipynb/ordinal/CORN_cement.ipynb:L378-L405](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/ordinal/CORN_cement.ipynb#L378-L405) —— `from coral_pytorch.losses import corn_loss`（L378，CORN 与 CORAL 同住一个 `coral_pytorch` 包），循环体里 `loss = corn_loss(logits, class_labels, NUM_CLASSES)`（L391）。注意它直接吃整数标签——扩展编码与任务掩码都在损失内部完成，连 `levels_from_labelbatch` 都省了。

评估、结果与秩概率：

- [pytorch_ipynb/ordinal/CORN_cement.ipynb:L422-L450](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/ordinal/CORN_cement.ipynb#L422-L450) —— 评估函数第三次出现，预测行换成 `corn_label_from_logits(logits)`（L437）——条件概率 → 链式法则 → 档位，一步到位。
- [pytorch_ipynb/ordinal/CORN_cement.ipynb:L473-L474](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/ordinal/CORN_cement.ipynb#L473-L474) —— 成绩：**MAE（训练/测试）0.19 | 0.32**，MSE 0.20 | 0.35。三个方法里测试 MAE 最优，且它的训练 MAE（0.19）与测试 MAE（0.32）都比另两个低。
- [pytorch_ipynb/ordinal/CORN_cement.ipynb:L541-L546](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/ordinal/CORN_cement.ipynb#L541-L546) —— 全 notebook 的点睛之笔：`probas = torch.sigmoid(logits)` 得条件概率，`probas = torch.cumprod(probas, dim=1)` 沿任务维连乘得累积概率——4.3.1 的公式 \(\prod_j P(y>j|y>j-1)\) 原来就是一行 `cumprod`。保存的输出里每个样本的 4 个数都非递增（如 `9.83e-1, 9.83e-1, 9.81e-1, 9.59e-1`），秩一致性肉眼可见。
- **读源码发现之四**：[L481](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/ordinal/CORN_cement.ipynb#L481) 的 markdown 写着「Note that **MNIST** is not an ordinal dataset...」——但本 notebook 用的是水泥数据集！这句显然是从某个 MNIST 示例复制过来的残留。结论本身没错（MNIST 确实无序），放在这里却文不对题——notebook 的**文字**与**代码**冲突时，永远以代码为准。

三个方法的成绩与差异汇总（注意 4.2.3 指出的配方不一致）：

| | 输出层 | 损失 | 预测转换 | 测试 MAE | 测试 MSE |
| --- | --- | --- | --- | --- | --- |
| CE 基线 | `Linear(→5)` | `cross_entropy` | `argmax` | 0.37 | 0.41 |
| CORAL | `CoralLayer(→K-1)`（共享权重+偏置） | `coral_loss`（9 任务†） | `sigmoid` + `proba_to_label` | 0.36 | 0.38 |
| CORN | `Linear(→K-1)` | `corn_loss`（条件掩码） | `corn_label_from_logits` | **0.32** | **0.35** |

† CORAL notebook 因 `NUM_CLASSES=10` 实际在 9 个任务上训练，且 `learning_rate=0.05` 与另两份（0.001）不同——此列数字仅供参考，对齐配方后的复验见综合实践。

#### 4.3.4 代码实践

**实践目标**：不借助 `corn_label_from_logits`，用 `sigmoid` + `cumprod` + 自写计数规则，手工复现 CORN 的预测，并与官方函数对账。

**操作步骤**：

1. 运行 CORN notebook 至训练完成，新建 cell：

```python
# 示例代码：手工还原 CORN 的 秩概率 → 档位
import torch
from coral_pytorch.dataset import corn_label_from_logits

def my_corn_label_from_logits(logits: torch.Tensor) -> torch.Tensor:
    cond = torch.sigmoid(logits)        # 条件概率 P(y>k | y>k-1)
    cum = torch.cumprod(cond, dim=1)    # 链式法则 → 累积概率 P(y>k)
    return (cum > 0.5).float().sum(dim=1)  # 数「超过」票 → 档位

with torch.no_grad():
    logits = model(next(iter(test_loader))[0].to(DEVICE))
    mine = my_corn_label_from_logits(logits)
    official = corn_label_from_logits(logits).float()
    print(torch.equal(mine, official), (mine - official).abs().sum().item())
    cond = torch.sigmoid(logits)
    print((torch.cumprod(cond, dim=1)[:, :-1] >= torch.cumprod(cond, dim=1)[:, 1:]).all())
```

2. 最后一行检验「累积概率逐列非递增」（秩一致性）。第三个打印若为 `tensor(True)` 即通过。

**需要观察的现象**：`torch.equal` 为 `True`（或差值求和为 0）；秩一致性检验通过；对比 4.2.4——CORAL 与 CORN 的「概率 → 档位」规则形式相同（都是数票），差别只在概率从哪来（独立 sigmoid vs 连乘）。

**预期结果**：`True 0.0` 与 `tensor(True)`（待本地验证——若你版本的 `corn_label_from_logits` 采用 `cumprod + argmax` 而非阈值计数，两者可能在边界样本上不同，请以 `inspect.getsource` 查看后解释差异）。

#### 4.3.5 小练习与答案

**练习 1**：CORAL 与 CORN 的输出层宽度都是 \(K-1\)，但参数量差多少？各自为什么？

**答案**：CORN 的 `Linear(300, 4)` 有 \(300\times4+4=1204\) 个参数；CORAL 是共享打分器 \(300\) 加 4 个偏置共 304 个，约为 CORN 的 1/4。CORAL 用一个标量打分强制所有任务共享「强度方向」，只允许门槛不同；CORN 给每个任务独立打分的自由度。约束强未必差——样本少时 CORAL 的强先验反而抗过拟合。

**练习 2**：CORN 损失里「任务 \(y+1\) 之后不参与损失」为什么是必要的，而不是偷懒？

**答案**：任务 \(k>y\) 建模的是 \(P(y>k \mid y>k-1)\)，而该样本已知 \(y>k-1\) 为假——条件事件不发生，条件概率在该样本上无观测意义，任何目标值都编不出来。硬给它一个目标（比如 0）等于逼模型学「在永远不会出现的条件分支上的行为」，会污染梯度。掩码掉是数学上的必然。

**练习 3**：三个 notebook 的初始训练损失分别是 1.85（CE）、7.88（CORAL）、44.56（CORN）。能据此说 CORN「学得最差」吗？

**答案**：不能。三个损失的数学定义不同：CE 是单任务平均负对数似然（随机初始时约 \(\ln 5\approx1.61\)）；CORAL 是 9 个二元任务之和；CORN 是带掩码的批次求和口径。**不同损失的数值互相没有可比性**——这延续 u5-l2「换损失即换量纲」的原则：比较模型只看同一评估指标（MAE），永远不要横比训练损失的绝对值。

## 5. 综合实践

**任务：对齐配方，复验「CORAL 是否真的优于 CE 基线」。**

三个 notebook 的保存成绩不是严格受控对比（CORAL 版 `learning_rate=0.05`、`NUM_CLASSES=10`）。请你修复这两个变量，做一次干净的对照实验：

1. **准备**：复制 `baseline_cement.ipynb` 与 `CORAL_cement.ipynb` 各一份（例如另存为 `my_baseline.ipynb` / `my_coral.ipynb`，注意 notebook 经相对路径读数据、须在仓库目录内运行）。
2. **对齐**：把两份的设置 cell 统一为 `random_seed=1`、`learning_rate=0.001`、`num_epochs=50`、`batch_size=128`、`NUM_CLASSES=5`；数据划分与标准化 cell 原样保留（两者本就相同，`random_state=1` 保证拿到**同一个**训练/测试划分）。
3. **补函数**：把 4.2.4 的 `my_proba_to_label` 加进 CORAL 版评估段，与 `proba_to_label` 同时输出，验证一致。
4. **记录**：两份各跑 1 次，填表——

| 模型 | 训练 MAE | 测试 MAE | 测试 MSE |
| --- | --- | --- | --- |
| CE 基线（对齐后） |  |  |  |
| CORAL（对齐后） |  |  |  |

5. **分析**：对齐后 CORAL 的测试 MAE 相比 notebook 原值（0.36）与基线（0.37）如何变化？改善主要来自训练 MAE 还是测试 MAE？（若你有余力，把 CORN 也拉进同一配方，三向对比。）

**预期结果**：待本地验证。可以预期的定性结论：修正 `NUM_CLASSES` 后 CORAL 的损失尺度回到 4 任务口径，训练更聚焦；`lr=0.001` 更平稳。无论数字如何，你完成了一次真正的受控实验——并亲手修复了源码阅读中发现的配方缺陷。

## 6. 本讲小结

- **有序回归**处理「有序但间距未知」的标签；CE 分类器把错 1 档与错 4 档同罚，训练目标与 MAE 评估标准错位，这是它作为有序基线的结构性缺陷。
- **扩展二元分类**把 \(K\) 类有序问题改写成 \(K-1\) 个「标签是否超过 \(k\)」的二元任务，顺序信息编码在扩展标签（如 \(y=3 \to [1,1,1,0]\)）里。
- **CORAL** = 共享权重单打分器 + \(K-1\) 个独立偏置（递减初始化），logit \(=f(x)+b_k\)；sigmoid 单调性 + 偏置有序让累积概率天然非递增，秩一致性由结构保证；预测 = 数「\(\sigma>0.5\)」的任务数。
- **CORN** = 普通 `Linear(→K-1)` 输出条件概率 \(P(y>k\mid y>k-1)\)，`cumprod` 链式法则还原累积概率；条件不成立的任务被掩码在损失之外；秩一致性由概率连乘保证。
- 三个方法共享同一 MLP 骨架与五步训练循环，**改动只落在输出层、损失、预测转换三处**——这是仓库「只改一处」对比法的又一示范。
- 本讲撞见四个「读源码要以代码为准」的案例：重复的 `logits = model(features)`、死代码 `beckham_logits_to_labels`、CORAL 的 `NUM_CLASSES=10` + `lr=0.05` 配方漂移、CORN notebook 里复制残留的 MNIST 说明文字。

## 7. 下一步学习建议

- **横向扩展方法**：`pytorch_ipynb/ordinal/` 目录还有三份姊妹 notebook——`beckham2016_cement.ipynb`（不可逆有序损失）、`niu2016_cement.ipynb`（多输出独立二元分类器，正好是 CORAL 所批评的无共享结构）、`polat2022_cement.ipynb`（自蒸馏式有序回归）。带着本讲的框架去读：各自的输出层是什么、秩一致性靠什么保证，你会读得非常快。
- **读论文原文**：CORAL（Pattern Recognition Letters 140, 325-331, 2020）与 CORN（arXiv:2111.08851），重点看 CORAL 关于偏置有序性的证明与 CORN 的消融实验。
- **应用到你自己的数据**：任何「评分/等级/组别」型任务（满意度调查、信用分级、年龄组）都适用；沿用第 5 节的受控实验模板，把 CE 基线与 CORAL/CORN 各跑一遍。
- **手册内衔接**：下一讲 u7-l6 进入模型可解释性可视化（显著性图与 guided backprop），从「怎么评估模型的错」转向「怎么看模型在看哪里」；若想先补评估方法论的另一半，可回顾 u7-l2 的 K 折交叉验证——本讲 998 样本的小数据场景正是 K 折的主场。
