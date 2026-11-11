# 训练技巧：循环学习率、梯度裁剪与 batch 退火

## 1. 本讲目标

前面几讲关注「模型长什么样」，本讲关注「训练过程本身怎么调」。`pytorch_ipynb/tricks/` 目录是仓库专门存放训练技巧实验的角落，本讲精读其中三份 notebook。学完本讲，你应该能够：

1. 独立执行一次 **LR Range Test**，画出「准确率/损失 – 学习率」曲线，并据此为循环学习率选定 `base_lr` 与 `max_lr` 两个边界。
2. 讲清 `triangular`、`triangular2`、`exp_range` 三种**循环学习率（Cyclical Learning Rate, CLR）**的公式差异，读懂仓库中约 25 行的手写实现，并能识破正式训练 cell 里调度代码的一处偏差。
3. 掌握 PyTorch 的三种**梯度裁剪**写法：`clip_grad_value_`、参数 `register_hook` + `torch.clamp`、`clip_grad_norm_`，知道各自适合什么场景、为什么非对称裁剪会毁掉训练。
4. 理解「**不减学习率、改为增大 batch size**」这种退火策略背后的噪声尺度公式，并能逐步推演出仓库 notebook 中批量递增调度代码的真实行为。

方法论上承接 u2-l4 确立的**受控对比**：三份 notebook 的对比实验都是「克隆基线、只改一处」，差异才能归因到唯一变量。

## 2. 前置知识

**学习率是步长。** u2-l2 确立的五步训练范式 `forward → loss → zero_grad → backward → step` 中，`optimizer.step()` 做的事本质上是：

\[ \theta \leftarrow \theta - \epsilon \cdot g \]

\( \theta \) 是参数，\( g \) 是梯度，\( \epsilon \) 是学习率。固定 \( \epsilon \) 有一个两难：

- **早期**损失面崎岖，步子太小则探索慢、易陷在局部结构里；
- **后期**接近极小值，步子太大就会在最优点附近来回震荡、无法收敛。

经典解法是学习率衰减。本讲介绍三条更「动态」的替代路线：

1. **循环学习率**：让学习率在 `[base_lr, max_lr]` 之间周期性往返——短期看是有害的大步，长期看却能帮优化器跳出对泛化不利的尖锐极小值（sharp minima）。
2. **梯度裁剪**：问题不在步长系数，而在梯度本身偶发地变得巨大（梯度爆炸），那就把梯度「削」回合理范围再走步。
3. **batch size 退火**：小批量 SGD 的更新自带噪声，噪声大小同时由学习率 \( \epsilon \) 和批量 \( B \) 控制——衰减 \( \epsilon \) 可以等价地换成增大 \( B \)。

另外两个反复出现的概念：

- **batch step（迭代步）**：模型被更新的总次数。1 个 epoch 有 \( \lceil N/B \rceil \) 个 batch，训练 10 个 epoch、每 epoch 383 个 batch，batch step 就走到 3829。CLR 是**按 batch step** 调度的，这与按 epoch 调度的衰减不同。
- **梯度范数**：把所有参数的梯度拼成一个长向量 \( g \in \mathbb{R}^P \)，其 L2 范数 \( \|g\|_2 = \sqrt{\sum_i g_i^2} \) 度量「这一步梯度整体有多大」，是裁剪与监控的对象（呼应 u4-l5 的 hook 技巧）。

## 3. 本讲源码地图

三份 notebook 都遵循 u1-l2 介绍的「Imports → Settings/Dataset → Model → Training → Evaluation」五段式结构，且都**自包含**（不依赖 helper 模块，所有函数在 notebook 内定义）。

| 文件 | 主题 | 关键内容 |
| --- | --- | --- |
| `pytorch_ipynb/tricks/cyclical-learning-rate.ipynb` | 循环学习率 | 手写 `cyclical_learning_rate` 函数（3 种模式）、LR Range Test 完整流程、CIFAR-10 上的三组对比训练 |
| `pytorch_ipynb/tricks/gradclipping_mlp.ipynb` | 梯度裁剪 | MNIST 上 4 隐层 MLP 的三种裁剪写法对照 |
| `pytorch_ipynb/tricks/cnn-alexnet-cifar10-batchincrease.ipynb` | batch size 退火 | AlexNet/CIFAR-10 上「恒定批量」vs「中途递增批量」对照实验 |

> 说明：以下引用的行号均指 notebook **JSON 源文件**的行号（GitHub 永久链接按此定位）。ipynb 中每行源码是 JSON 数组里的一个字符串，点开链接会夹杂元数据属正常现象。

## 4. 核心概念与源码讲解

### 4.1 LR Range Test：先扫描，再训练

#### 4.1.1 概念说明

CLR 需要你先回答：**循环的上下界 `[base_lr, max_lr]` 到底设多少？** 拍脑袋给 0.001 和 0.01 往往不是最优——最优区间跟模型、数据、batch size 都有关。

LR Range Test（同样出自 Leslie N. Smith 的论文）是一个「快速且粗糙」的经验方法：训练 5–10 个 epoch，让学习率从一个较小的值**线性增长**到一个较大的上限，同时逐 batch 记录损失和准确率。然后画一张横轴为学习率的曲线图：

- **base_lr** = 准确率**开始持续改善**（损失开始明显下降）的学习率；
- **max_lr** = 准确率**不再改善、开始下降或剧烈波动**的学习率。

一句话：先花约 10% 的训练预算做一次「体检」，把学习率的可用区间摸清楚，再正式训练。

#### 4.1.2 核心流程

```
1. 初始化模型、优化器（lr = 较小的 base_lr）
2. for batch_step in range(num_epochs * iter_per_ep):
3.     正常执行五步训练范式（forward/loss/zero_grad/backward/step）
4.     记录 (当前 lr, 当前 batch 损失, 训练批准确率, 验证集准确率)
5.     lr ← 从 base_lr 线性增长一步；写回 optimizer.param_groups
6. 画图：横轴 lr、纵轴 accuracy / loss，读图选出两个边界
```

巧妙之处：**线性增长本身复用了 CLR 的 triangular 公式**。设总扫描步数为 \( T \)，把 `step_size` 设成 \( T \)，则整个扫描期间 `cycle = 1`、\( x = 1 - t/T \) 从 1 线性降到 0，lr 恰好从 `base_lr` 线性爬升到 `max_lr`——不需要另写一段调度代码。

#### 4.1.3 源码精读

notebook 对该方法的定义（跑 5–10 个 epoch、线性增大学习率、按准确率改善/停滞选两个切点）：

- [pytorch_ipynb/tricks/cyclical-learning-rate.ipynb:L684-L692](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L684-L692) —— LR Range Test 的定义与两个边界的选取规则，结尾提示可把 `step_size` 设为 `num_epochs*iter_per_ep` 来产生线性爬坡。

先用「空跑」验证线性爬坡：不训练模型，只模拟 batch step 并调用同一个 CLR 函数画 lr 曲线：

- [pytorch_ipynb/tricks/cyclical-learning-rate.ipynb:L722-L732](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L722-L732) —— 双重 for 循环模拟 `num_epochs=10` 的 batch step，把 `step_size=num_epochs*iter_per_ep` 传入 `cyclical_learning_rate`，得到一条从 base 到 max 的线性斜坡。

正式 Range Test 的设置与执行（扫描范围 `base_lr=0.01`、`max_lr=0.2`、10 个 epoch、SGD）：

- [pytorch_ipynb/tricks/cyclical-learning-rate.ipynb:L825-L843](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L825-L843) —— 本轮超参数（`num_epochs=10`、`base_lr=0.01`、`max_lr=0.2` 在 L827-L828）、按种子重建模型、`CrossEntropyLoss` 与 `SGD(lr=base_lr)`（L843），并初始化 `collect` 字典收集 lr/cost/train_batch_acc/val_acc。
- [pytorch_ipynb/tricks/cyclical-learning-rate.ipynb:L856-L870](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L856-L870) —— batch 循环内熟悉的五步范式：`batch_step += 1`、前向、`zero_grad`、`cost.backward()`（L867）、`optimizer.step()`（L870）。
- [pytorch_ipynb/tricks/cyclical-learning-rate.ipynb:L882-L888](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L882-L888) —— 每 batch 采集一条记录：先 `model.eval()`，训练准确率只在**当前这一个 batch** 上算（把 `[features, targets]` 包成单元素列表传入），验证准确率完整跑 1000 张验证图，随后切回 `model.train()`。注意本 notebook 的 `compute_accuracy`（定义在 [L758](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L758)）内部**没有** `eval()`/`no_grad()`，由调用方负责切换——与 gradclipping notebook 的规范写法不同（见 4.3.3）。
- [pytorch_ipynb/tricks/cyclical-learning-rate.ipynb:L892-L897](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L892-L897) —— batch 尾声计算下一步 lr 并写回优化器：`for g in optimizer.param_groups: g['lr'] = cur_lr`。这是 PyTorch 中**手动改学习率的标准写法**——优化器把超参数放在 `param_groups` 列表的字典里，直接改字典即生效。

读图与结论：

- [pytorch_ipynb/tricks/cyclical-learning-rate.ipynb:L944-L953](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L944-L953) —— 画「accuracy–lr」与「cost–lr」两张图。
- [pytorch_ipynb/tricks/cyclical-learning-rate.ipynb:L965-L972](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L965-L972) —— 读图结论：切点 1) 约在 0.08–0.09，切点 2) 约在 0.175 甚至 0.2；作者同时坦承曲线并不总是清晰可辨、该启发式噪声较大。

#### 4.1.4 代码实践

**实践目标**：亲手跑一遍最小版 LR Range Test，得到可读的「损失–学习率」曲线（这是综合实践第 2 步的前置）。

1. 打开 `gradclipping_mlp.ipynb`（或把其模型/数据 cell 复制到新 notebook）：沿用 4 隐层 MNIST MLP、`batch_size=64`，把优化器换成 `torch.optim.SGD(model.parameters(), lr=0.01)`（CLR 的典型场景是不带逐参数自适应的 SGD）。
2. 把 4.2.3 的 `cyclical_learning_rate` 函数（[L140-L163](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L140-L163)）原样复制进来。
3. 设 `num_epochs=5`、`iter_per_ep = len(train_loader)`、`base_lr=0.01`、`max_lr=1.0`（比仓库扫得更宽，便于看清「发散」长什么样）。
4. 照 [L856-L897](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L856-L897) 的结构写循环：每 batch 训练后记录 `cost.item()` 与当前 lr，再更新 lr 写回 `param_groups`。为省时间可每 10 个 batch 才记录一次、跳过逐 batch 的验证集评估（只画损失曲线即可）。
5. 用 `plt.plot(collect['lr'], collect['cost'])` 画图（横轴学习率、纵轴损失）。

**需要观察的现象**：曲线先平（lr 太小、损失几乎不动，约在 ln10≈2.30 附近），中段明显下降，尾部掉头向上或剧烈锯齿（lr 太大、开始发散）。
**预期结果**：得到一条「平坦 → 下降 → 上翘」的谷形曲线；据此记下两个 lr 值供 4.2 使用。具体数值与硬件/随机种子有关，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 LR Range Test 可以直接复用 triangular 模式的 CLR 函数产生线性增长，而不需要另写调度器？

**答案**：令 `step_size = T`（总扫描步数），则扫描期间 `cycle = floor(1 + t/(2T)) = 1`，\( x = |t/T - 2 + 1| = 1 - t/T \) 随 \( t \) 从 0 到 \( T \) 线性地从 1 降到 0，于是 \( \text{lr} = \text{base} + (\text{max}-\text{base})(1-x) \) 恰是从 base 到 max 的线性函数。[L722-L732](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L722-L732) 的空跑演示的正是这一点。

**练习 2**：Range Test 中每个 batch 都完整评估一次 1000 张验证图（[L882-L888](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L882-L888)），这带来什么问题？如何改进？

**答案**：Range Test 本应「便宜」（只花 5–10 个 epoch），逐 batch 全量评估验证集会让评估开销远超训练本身（约 3830 次评估 × 1000 张图）。改进：每 20–50 个 batch 评估一次、只评估验证集的一个子集、或干脆只用当前 batch 的训练损失/准确率画图（4.1.4 实践的做法）。

---

### 4.2 循环学习率：triangular / triangular2 / exp_range

#### 4.2.1 概念说明

CLR 的核心主张：**短期有害的大学习率，长期可能有益**。让学习率在 `[base_lr, max_lr]` 间周期往返，相当于交替进行「大步探索」与「小步精修」，无需手工设计衰减表；notebook 的概念部分还指出这种做法比逐参数的自适应方法计算更便宜（见 [L99](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L99)）。

仓库实现了论文的三种模式：

| 模式 | 行为 | 适用直觉 |
| --- | --- | --- |
| `triangular` | 锯齿波：lr 在两边界间等幅往返，振幅不随周期变 | 纯 CLR 基线 |
| `triangular2` | 每过一个周期，振幅减半（\( \times 1/2^{\text{cycle}-1} \)） | 兼顾「循环探索」与「后期收敛」，内置退火 |
| `exp_range` | 锯齿波整体乘 \( \gamma^t \) 指数衰减 | 想要更平滑的衰减包络时 |

#### 4.2.2 核心流程

设当前迭代步为 \( t \)、半周期长度 `step_size` 为 \( s \)（lr 从 base 爬到 max 需要 \( s \) 步）：

\[ \text{cycle} = \left\lfloor 1 + \frac{t}{2s} \right\rfloor \]

\[ x = \left| \frac{t}{s} - 2\cdot\text{cycle} + 1 \right| \]

\[ \text{lr} = \text{base\_lr} + (\text{max\_lr} - \text{base\_lr}) \cdot \max(0,\ 1 - x) \]

直觉解读：\( x \) 是一个以 \( 2s \) 为周期、在 0 与 1 之间往返的三角波相位。\( 1-x \) 在每个完整周期内先从 0 升到 1（前半周期，lr 从 base 爬到 max）再降回 0（后半周期，lr 落回 base）；`cycle` 记录「现在是第几个完整往返」。三种模式在此之上叠加修正：

- `triangular2`：增量 \( \times \dfrac{1}{2^{\text{cycle}-1}} \)，第 1 个周期振幅为原值、第 2 个减半、第 3 个再减半……
- `exp_range`：增量 \( \times \gamma^{t} \)，\( \gamma \) 略小于 1（默认 0.999995），随迭代步指数压低锯齿。

#### 4.2.3 源码精读

整个 CLR 只有约 25 行，是「论文公式直译成 NumPy」的典范：

- [pytorch_ipynb/tricks/cyclical-learning-rate.ipynb:L140-L150](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L140-L150) —— 函数签名与三角波核心，与上面三个公式逐字对应；默认值 `base_lr=0.001, max_lr=0.006, mode='triangular', gamma=0.999995`。

```python
def cyclical_learning_rate(batch_step, step_size,
                           base_lr=0.001, max_lr=0.006,
                           mode='triangular', gamma=0.999995):
    cycle = np.floor(1 + batch_step / (2. * step_size))
    x = np.abs(batch_step / float(step_size) - 2 * cycle + 1)
    lr_delta = (max_lr - base_lr) * np.maximum(0, (1 - x))
```

- [pytorch_ipynb/tricks/cyclical-learning-rate.ipynb:L152-L159](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L152-L159) —— 三种模式分支：`triangular` 原样通过；`triangular2` 把增量乘 `1 / (2. ** (cycle - 1))`；`exp_range` 乘 `gamma**batch_step`；其余取值抛 `ValueError`。
- [pytorch_ipynb/tricks/cyclical-learning-rate.ipynb:L161-L163](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L161-L163) —— `lr = base_lr + lr_delta; return lr`。

训练任何模型之前，notebook 先用双重 for 循环模拟 batch step，把三种波形画出来：

- [pytorch_ipynb/tricks/cyclical-learning-rate.ipynb:L233-L243](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L233-L243) —— `triangular`：`step_size=iter_per_ep*5`，每 5 个 epoch 完成一次爬坡；
- [pytorch_ipynb/tricks/cyclical-learning-rate.ipynb:L306-L317](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L306-L317) —— `triangular2`：同样调用、仅多传 `mode='triangular2'`（L313），输出图中锯齿逐周期减半；
- [pytorch_ipynb/tricks/cyclical-learning-rate.ipynb:L370-L382](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L370-L382) —— `exp_range`：额外传 `gamma=0.99998`（L378），锯齿包络指数下行。

实验设置：数据为 CIFAR-10，`batch_size=128`（[L467](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L467)），用打乱索引 + `SubsetRandomSampler` 切出 49000/1000 的训练/验证划分（[L482-L485](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L482-L485)，即 u4-l1 讲过的索引区间法）。被测模型 `ConvNet` 是 5 组「Conv(3×3, same)→ReLU→MaxPool(2×2)」把 32×32×3 逐级压到 1×1×30 再接 `Linear(30, 10)`：

- [pytorch_ipynb/tricks/cyclical-learning-rate.ipynb:L570-L664](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L570-L664) —— `ConvNet` 定义（每层注释都标了特征图尺寸，可对照 u3-l1 的尺寸公式练习推导）。
- [pytorch_ipynb/tricks/cyclical-learning-rate.ipynb:L552](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L552) —— 作者说明：这是刻意保持简单的网络，目的是测试 CLR 而非刷 CIFAR-10 分数。

三组正式训练用 Range Test 选出的边界 `base_lr=0.09, max_lr=0.175` 各训 150 个 epoch：

- [pytorch_ipynb/tricks/cyclical-learning-rate.ipynb:L1171-L1174](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L1171-L1174) —— `triangular` 组设置；`triangular2`、`exp_range` 组设置完全相同（[L1545-L1548](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L1545-L1548)、[L1880-L1883](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L1880-L1883)），只有 `mode` 参数不同——标准受控对比。
- [pytorch_ipynb/tricks/cyclical-learning-rate.ipynb:L1244-L1249](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L1244-L1249) —— `triangular` 组的 lr 更新调用与写回。`triangular2` 组同一处仅多 `mode='triangular2'`（[L1618-L1624](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L1618-L1624)），`exp_range` 组多 `mode='exp_range'`（[L1954-L1959](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L1954-L1959)，未传 `gamma`，用默认 0.999995）。
- 测试集结果：`triangular` 61.45%（[L1328](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L1328)）、`triangular2` 61.69%（[L1663](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L1663)）、`exp_range` 59.81%（[L1999](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L1999)）。单次运行差距不大，符合 u3-l2「单次运行的架构/策略对比不可轻信」的告诫。

**读源码发现的坑（重要）**：把三个正式训练 cell 与前面的空跑 cell 仔细对照，会发现正式训练的 lr 调度与演示图并不一致，有三个细节：

1. lr 更新放在 **epoch 循环末尾**（[L1244-L1249](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L1244-L1249) 在 `for batch_idx...` 循环之外），每 epoch 只改一次 lr，而非论文式每 batch 一改；
2. `batch_step` 在这三个 cell 里**既未重新初始化、也未自增**——它保留着 Range Test 结束时的旧值 3829。旁证：批级日志条件 `if not batch_step % 600`（[L1219](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L1219)）恒不触发（3829 % 600 = 229 ≠ 0），保存的输出里也确实只有 epoch 级日志、没有任何一条 `Batch ...` 日志；
3. 返回值被写回了 `base_lr` 变量本身（`base_lr = cyclical_learning_rate(..., base_lr=base_lr)`），下一轮以上一步的 lr 为新下界。

三者叠加的实际效果：lr 从 0.09 出发，每个 epoch 按 \( \text{lr} \leftarrow \text{lr} + c\,(0.175 - \text{lr}) \)（\( c \) 是由旧 batch_step 决定的常数 ≈ 0.067）缓慢逼近 0.175，是一条平滑的指数式爬坡线而非锯齿波；`triangular2` 的周期因子恰为 1、`exp_range` 的衰减因子 ≈ 0.981，三种模式在此实现下几乎没有差别——这也解释了三组成绩为何如此接近。这不是运行后的推断，而是代码与保存输出交叉验证的结论，再次印证 u1-l2 确立的原则：**读 notebook 要以代码和变量来源为准，输出图只是佐证**。本讲综合实践会把它修正为论文式的每 batch 调度。

#### 4.2.4 代码实践

**实践目标**：在同一个 MLP 上对比「固定学习率」与「修正版 triangular2 循环学习率」。

1. 沿用 4.1.4 的环境（MNIST + 4 隐层 MLP + SGD），把 Range Test 读出的边界记为 `(lr_min, lr_max)`（曲线不清晰时可参考仓库量级 0.09/0.175）。
2. **基线组**：固定 `lr = lr_min`，训练 10 个 epoch，逐 epoch 记录验证集准确率与平均损失。
3. **CLR 组**：把 lr 更新移进 batch 循环内部，并**重新初始化** `batch_step = -1`（修正 4.2.3 指出的坑）：

```python
# 示例代码：修正版 triangular2 调度（每 batch 更新）
batch_step = -1
iter_per_ep = len(train_loader)
step_size = 4 * iter_per_ep          # 半周期 = 4 个 epoch
for epoch in range(num_epochs):
    for features, targets in train_loader:
        batch_step += 1
        # ...五步训练范式...
        cur_lr = cyclical_learning_rate(batch_step=batch_step,
                                        step_size=step_size,
                                        base_lr=lr_min, max_lr=lr_max,
                                        mode='triangular2')
        for g in optimizer.param_groups:
            g['lr'] = cur_lr
```

4. 把两组的「验证准确率–epoch」和「损失–epoch」画进同一张图对比。

**需要观察的现象**：CLR 组的损失曲线通常带小幅周期性起伏（对应 lr 爬坡段），验证准确率可能阶段性回落再创新高；固定 lr 组曲线更平滑但可能收敛更慢或停滞。
**预期结果**：两种策略最终都能到 96%+（MNIST 上 MLP 的正常水平）；CLR 是否占优取决于边界选取，这正是「先 Range Test 再 CLR」流程的意义。具体对比结果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`step_size` 设为 `iter_per_ep*5` 与 `iter_per_ep*0.5` 分别意味着什么？

**答案**：`step_size` 是「半周期」——lr 从 base 爬到 max 所用的 batch 数。`iter_per_ep*5` 表示每 5 个 epoch 完成一次爬坡、10 个 epoch 一个完整往返（[L233-L243](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L233-L243) 用的就是它）；`iter_per_ep*0.5` 则每个 epoch 内往返一次，波动频繁得多。论文建议总训练步数为 `step_size` 的 2–10 倍。

**练习 2**：`triangular2` 第 3 个周期的最大 lr 是多少（设 base=0.09、max=0.175）？

**答案**：`triangular2` 把增量乘 \( 1/2^{\text{cycle}-1} \)，第 3 个周期（cycle=3）增量为 \( (0.175-0.09) \times 1/4 \approx 0.021 \)，峰值约 \( 0.09 + 0.021 = 0.111 \)。第 1、2、3 周期峰值依次为 0.175、0.1325、0.111，逐步向 base_lr 收缩——这就是「内置退火」的含义。

**练习 3**：为什么 CLR 通常搭配普通 SGD 而不是 Adam？

**答案**：notebook 概念部分（[L99](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L99)）指出：在给定边界内变动 lr 的做法比逐参数自适应（Adam 等）计算开销更小；且 Adam 内部已有 per-parameter 缩放，再叠加大幅全局 lr 波动会互相干扰。仓库三组正式训练用的都是 SGD（[L1189](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L1189)）。

---

### 4.3 梯度裁剪：clip_grad_value_、hook 与 clip_grad_norm_

#### 4.3.1 概念说明

**梯度爆炸**：深网络中梯度经链式法则逐层相乘，若各因子普遍偏大，越靠近输入的层梯度会被指数级放大；一次异常大的梯度配上不变的学习率，可能一步把参数踢飞、损失突然变 NaN。缺少正则、层数较多、用 ReLU 这类无界激活的简单网络尤其容易中招。

**梯度裁剪**的思路非常直接：在 `backward()` 之后、`optimizer.step()` 之前检查并修剪梯度。notebook 介绍了三种由宽到严的写法：

1. **按值裁剪 `clip_grad_value_`**：每个梯度元素独立截断到 `[-clip_value, clip_value]`。实现最简单，但只能给单个对称阈值，且元素级截断会改变梯度的相对结构（方向可能被改变）。
2. **自定义上下界（hook + clamp）**：给每个参数注册反向 hook，用 `torch.clamp(grad, min, max)` 修剪——本质与方式 1 相同，但允许**非对称**区间（如 [-0.1, 1.0]）。本 notebook 用它做了个反面教材（见下）。
3. **按范数裁剪 `clip_grad_norm_`**：把所有参数的梯度拼成一个长向量，只看整体 L2 范数。若 \( \|g\|_2 > \text{max\_norm} \) 则整体缩放：

\[ g \leftarrow g \cdot \frac{\text{max\_norm}}{\|g\|_2} \]

范数不超限就完全不动。它只改梯度的**长度**、不改**方向**，是 RNN/Transformer 训练中最常用的标准做法。

#### 4.3.2 核心流程

```
for epoch, batch:
    forward → loss → zero_grad → backward
    ── 此刻 .grad 已生成，但还没被消费 ──
    裁剪（三选一）：
      clip_grad_value_(model.parameters(), 1.)             # 元素级、对称
      hook 里 torch.clamp(grad, -0.1, 1.0)                 # 元素级、非对称（一次性注册）
      clip_grad_norm_(model.parameters(), 1., norm_type=2) # 全局范数级
    optimizer.step()   # 用修剪后的梯度更新
```

关键时序：裁剪必须卡在 `backward` 与 `step` **之间**——太早（backward 前）梯度还没算出来，太晚（step 后）梯度已被消费。

#### 4.3.3 源码精读

三种方法的文档式介绍（含官方 API 链接与用法示例）：

- [pytorch_ipynb/tricks/gradclipping_mlp.ipynb:L59-L64](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/gradclipping_mlp.ipynb#L59-L64) —— `clip_grad_value_` 说明：只能指定单个 `clip_value`，上下界对称。
- [pytorch_ipynb/tricks/gradclipping_mlp.ipynb:L74-L79](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/gradclipping_mlp.ipynb#L74-L79) —— hook 写法：模型实例化后**注册一次**即可，之后每次 `backward` 都会先经 `torch.clamp` 再进 `step`。
- [pytorch_ipynb/tricks/gradclipping_mlp.ipynb:L83-L92](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/gradclipping_mlp.ipynb#L83-L92) —— `clip_grad_norm_` 说明：范数在**全部梯度拼接成的单一向量**上计算，原地修改。

实验对象：MNIST 上的 4 隐层 MLP（784-256-128-64-32-10，比 u2-l3 的两隐层更深），`Adam(lr=0.01)`、10 个 epoch、batch 64：

- [pytorch_ipynb/tricks/gradclipping_mlp.ipynb:L147-L157](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/gradclipping_mlp.ipynb#L147-L157) —— 超参与各层宽度。
- [pytorch_ipynb/tricks/gradclipping_mlp.ipynb:L223-L256](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/gradclipping_mlp.ipynb#L223-L256) —— `MultilayerPerceptron` 定义。小细节：`forward` 末行是 `probas = F.log_softmax(logits, dim=1)`——名字叫 probas 实际是对数概率；对 `argmax` 无影响（单调变换不改最大值位置），但若拿它去算交叉熵会算错口径，读码时应留意。

三个训练 cell 结构完全相同（重建模型、重建 Adam、同一循环），唯一差别是裁剪那一行，构成三重受控对比：

- [pytorch_ipynb/tricks/gradclipping_mlp.ipynb:L512-L524](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/gradclipping_mlp.ipynb#L512-L524) —— **方式一**：`cost.backward()` 之后插入 `torch.nn.utils.clip_grad_value_(model.parameters(), 1.)`（L520），紧接着 `optimizer.step()`（L524）。测试集 96.80%（[L551](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/gradclipping_mlp.ipynb#L551)）。
- [pytorch_ipynb/tricks/gradclipping_mlp.ipynb:L797-L798](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/gradclipping_mlp.ipynb#L797-L798) —— **方式二**：建模后立刻为每个参数注册 `p.register_hook(lambda grad: torch.clamp(grad, -0.1, 1.0))`，训练循环里**没有任何**裁剪调用（裁剪在 backward 内部自动发生）。测试集仅 86.94%（[L851](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/gradclipping_mlp.ipynb#L851)），明显劣化。
- [pytorch_ipynb/tricks/gradclipping_mlp.ipynb:L1116-L1124](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/gradclipping_mlp.ipynb#L1116-L1124) —— **方式三**：同样位置换上 `torch.nn.utils.clip_grad_norm_(model.parameters(), 1., norm_type=2)`，测试集 96.89%（[L1151](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/gradclipping_mlp.ipynb#L1151)），三组中最高。

| 裁剪方式 | 语义 | 方向是否保持 | 测试准确率 |
| --- | --- | --- | --- |
| `clip_grad_value_(…, 1.)` | 每元素截到 [-1, 1] | 不保证 | 96.80% |
| hook + `clamp(-0.1, 1.0)` | 每元素截到非对称区间 | 不保证且系统性偏置 | 86.94% |
| `clip_grad_norm_(…, 1., 2)` | 整体 L2 范数等比缩放 | 保持 | 96.89% |

方式二为什么差这么多？梯度元素被限制在 [-0.1, 1.0]：负方向最多保留 0.1 的幅值、正方向却可以到 1.0——**系统性地上偏了更新方向**，等价于给所有参数持续注入向上的推力。这个 cell 的价值正在于演示：裁剪不是无害操作，非对称/过强的裁剪会直接破坏优化。

另一个值得对比的细节：本 notebook 的 `compute_accuracy`（[L198-L213](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/gradclipping_mlp.ipynb#L198-L213)）内部自带 `net.eval()` 与 `torch.no_grad()`，是更规范的写法（与 u4-l4 讲过的 helper_evaluate 一致）；而 CLR notebook 的同名函数（[L758](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L758)）两样都缺、由调用方负责——同一仓库内新旧 notebook 的风格差异，读码时不能想当然。

#### 4.3.4 代码实践

**实践目标**：亲眼看到「梯度范数什么时候会被裁」，建立对 `max_norm` 量级的直觉。

1. 打开 `gradclipping_mlp.ipynb`，运行到方式三（Norm-clipping）的训练 cell 前，先复制一份该 cell。
2. 在 `cost.backward()` 与 `optimizer.step()` 之间加入范数监控（示例代码）：

```python
# 示例代码：监控裁剪前后的梯度范数
total_norm = torch.norm(torch.stack(
    [torch.norm(p.grad.detach(), 2) for p in model.parameters()]), 2)
torch.nn.utils.clip_grad_norm_(model.parameters(), 1., norm_type=2)
clipped_norm = torch.norm(torch.stack(
    [torch.norm(p.grad.detach(), 2) for p in model.parameters()]), 2)
if batch_idx % 100 == 0:
    print(f'norm before={total_norm:.3f} after={clipped_norm:.3f}')
```

3. 分别用 `max_norm=1.` 与 `max_norm=1000.`（近乎不裁）各跑 2 个 epoch，观察打印与损失曲线。

**需要观察的现象**：`max_norm=1.` 时不少 batch 的 before 值大于 1、after 恒 ≤ 1（被等比压缩）；`max_norm=1000.` 时几乎从不触发裁剪。
**预期结果**：训练初期梯度范数通常较大、裁剪频繁，随训练推进逐渐降到阈值以下；两种 `max_norm` 下前几个 epoch 的损失轨迹可能不同（过强裁剪等于压小有效步长），后期趋同。MNIST + Adam 下差异不大属正常——裁剪的价值在梯度更不稳定的场景（RNN、GAN、大学习率）。具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `clip_grad_norm_` 比 `clip_grad_value_` 更常用？

**答案**：`clip_grad_norm_` 只在整体范数超限时做**等比缩放**，梯度方向（各分量相对比例）完全不变，相当于临时调小这一步的有效学习率；`clip_grad_value_` 是逐元素截断，个别分量被削时会改变梯度方向，且阈值难以跨参数规模/损失尺度通用。RNN/Transformer 的标准实践因此几乎都是范数裁剪。

**练习 2**：方式二把梯度截到 [-0.1, 1.0]，为什么测试准确率从 96.8% 掉到 86.9%？

**答案**：区间非对称——负梯度最多保留 0.1 的幅值、正梯度可保留 1.0，等于系统性压低「负方向更新」、放大「正方向更新」，每次 step 都给参数注入向上偏置，破坏了梯度作为下降方向的无偏性。它演示的是「裁剪可以任意扭曲优化」，并非可用技巧（作者在 [L74-L76](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/gradclipping_mlp.ipynb#L74-L76) 只把它作为自定义上下界的能力演示）。

**练习 3**：hook 方式与在循环里调用 `clip_grad_value_` 有什么本质区别？

**答案**：功能等价（都对 `.grad` 做元素级修改），区别在触发时机与代码位置：hook 在建模后注册一次，之后每次 `backward()` 结束时自动执行，训练循环保持干净；`clip_*` 函数必须每个 batch 在 `backward` 与 `step` 之间显式调用，漏写一处就漏裁一次。hook 正是 u4-l5 介绍过的张量级反向 hook 的直接应用。

---

### 4.4 batch size 退火：不减学习率，改为增大批量

#### 4.4.1 概念说明

第三条路线源自 Smith 等人 2017 年的论文《Don't decay the learning rate, increase the batch size》。推理链是：

1. 小批量 SGD 的更新自带噪声，**早期噪声是好事**——帮助探索损失面、逃离对泛化不利的尖锐极小值；
2. **后期要收敛**，需要逐步降低噪声（类似模拟退火），传统做法是衰减学习率；
3. 但噪声大小同时由学习率 \( \epsilon \)、批量 \( B \)（和动量 \( m \)）决定，于是**衰减 \( \epsilon \) 可以等价替换为增大 \( B \)**——后者还能让每步用上更多样本、减少总更新次数。

notebook 给出的梯度噪声尺度 \( g \)（\( N \) 为训练样本总数）：

\[ g = \epsilon\left(\frac{N}{B} - 1\right) \]

带动量项时：

\[ g = \frac{\epsilon}{1-m}\left(\frac{N}{B} - 1\right) \approx \frac{\epsilon N}{B(1-m)} \]

直读：批量越小（\( B \to 1 \)），\( N/B - 1 \) 越大、噪声越大；批量逼近全量（\( B \to N \)），噪声趋零。保持 \( g \) 不变时，\( \epsilon \) 减半与 \( B \) 加倍效果相当。

#### 4.4.2 核心流程

notebook 的实验设计是两组对照（其余一切相同）：

```
组 1（基线）：batch_size = 256 恒定，训 40 个 epoch
组 2（退火）：前半程 batch_size = 256；
             过半程后每隔几个 epoch 用更大的 batch 重建 train_loader
两者学习率全程恒定（Adam, lr=1e-4），唯一变量是批量调度
```

「增大批量」在工程上的实现就是**用新的 `batch_size` 重新实例化 `DataLoader`**——loader 是轻量对象，Dataset 不动、重建无副作用（u4-l1 讲过二者的分工）。

#### 4.4.3 源码精读

- [pytorch_ipynb/tricks/cnn-alexnet-cifar10-batchincrease.ipynb:L75-L100](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cnn-alexnet-cifar10-batchincrease.ipynb#L75-L100) —— 论文动机与上面两个公式（L90 无动量版、L94-L96 动量版）。

实验设置：notebook 内自定义的 AlexNet（[L357-L393](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cnn-alexnet-cifar10-batchincrease.ipynb#L357-L393)，`features`/`classifier` 两个 Sequential，风格同 u3-l2）+ CIFAR-10（48000/2000 划分见 [L250-L251](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cnn-alexnet-cifar10-batchincrease.ipynb#L250-L251)，训练集带 `Resize(70)→RandomCrop(64)` 随机裁剪增强、测试集用 `Resize(70)→CenterCrop(64)` 确定性变换，见 [L254-L259](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cnn-alexnet-cifar10-batchincrease.ipynb#L254-L259)——u4-l3 的双 transform 模式），`Adam(lr=1e-4)`、40 个 epoch：

- [pytorch_ipynb/tricks/cnn-alexnet-cifar10-batchincrease.ipynb:L217-L221](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cnn-alexnet-cifar10-batchincrease.ipynb#L217-L221) —— 全局设置：`LEARNING_RATE=0.0001, BATCH_SIZE=256, NUM_EPOCHS=40`。
- [pytorch_ipynb/tricks/cnn-alexnet-cifar10-batchincrease.ipynb:L694-L717](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cnn-alexnet-cifar10-batchincrease.ipynb#L694-L717) —— 组 1（恒定批量）训练循环：标准 epoch×batch 双层循环加五步范式，逐 batch 记录损失。最终验证 71.60% / 测试 72.37%（[L816-L817](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cnn-alexnet-cifar10-batchincrease.ipynb#L816-L817)），总耗时 8.49 分钟（[L683](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cnn-alexnet-cifar10-batchincrease.ipynb#L683)）。

组 2 的核心只有几行：

- [pytorch_ipynb/tricks/cnn-alexnet-cifar10-batchincrease.ipynb:L863-L864](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cnn-alexnet-cifar10-batchincrease.ipynb#L863-L864) —— 批量调度表：`batch_sizes = np.arange(256, 5121, 512)`，即 [256, 768, 1280, 1792, 2304, 2816, 3328, 3840, 4352, 4864] 共 10 档；`batch_size_index` 是当前档位游标。
- [pytorch_ipynb/tricks/cnn-alexnet-cifar10-batchincrease.ipynb:L1077-L1084](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cnn-alexnet-cifar10-batchincrease.ipynb#L1077-L1084) —— 调度逻辑：`if epoch > (NUM_EPOCHS//2) and not epoch % (NUM_EPOCHS//len(batch_sizes))` 成立时用当前档位重建 `train_loader` 并把游标加一。代入数值：`NUM_EPOCHS//2 = 20`、`NUM_EPOCHS//len(batch_sizes) = 40//10 = 4`，即**过了第 20 个 epoch（0 起数）且 epoch 是 4 的倍数**才换档——epoch 24/28/32/36 共 4 次；游标从 0 走到 4，实际用到的批量依次为 256（首次换档等于没换）、768、1280、1792，调度表后半段（2304–4864）在 40 个 epoch 内轮不到。

**用保存的输出验证上述推导**（又一次「代码推导 ↔ 输出对账」）：日志显示每 epoch 批数在 epoch 29 从 188 降到 63（48000/768 向上取整）、epoch 33 降到 38（48000/1280）、epoch 37 降到 27（48000/1792）：

- [pytorch_ipynb/tricks/cnn-alexnet-cifar10-batchincrease.ipynb:L996-L997](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cnn-alexnet-cifar10-batchincrease.ipynb#L996-L997) —— epoch 25 仍为 188 批（实际批量还是 256），但日志标签已打印 `Batchsize: 768`；
- [pytorch_ipynb/tricks/cnn-alexnet-cifar10-batchincrease.ipynb:L1016](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cnn-alexnet-cifar10-batchincrease.ipynb#L1016) —— epoch 29 变为 63 批、标签 1280；epoch 33 为 38 批（[L1032](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cnn-alexnet-cifar10-batchincrease.ipynb#L1032)）、epoch 37 为 27 批（[L1048](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cnn-alexnet-cifar10-batchincrease.ipynb#L1048)）。

顺带一个小日志瑕疵：标签取的是 `batch_sizes[batch_size_index]`（[L1110](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cnn-alexnet-cifar10-batchincrease.ipynb#L1110)），而游标在换档后已加一，所以打印的批量永远比实际**超前一档**（实际 256 时打 768、实际 768 时打 1280……）。读日志同样要以代码为准。

- 结果：组 2 最终验证 75.25% / 测试 73.93%（[L1200-L1201](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cnn-alexnet-cifar10-batchincrease.ipynb#L1200-L1201)），比恒定批量的 71.60%/72.37% 高约 1.5–3.5 个百分点；总耗时 7.98 分钟（[L1064](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cnn-alexnet-cifar10-batchincrease.ipynb#L1064)）对 8.49 分钟——精度与速度双收益（单次运行，量级仅供参考）。

也请注意实验边界：notebook 只做到「中期开始增大批量」，学习率全程恒定，并没有按公式把 \( \epsilon \) 与 \( B \) 严格联动（那需要 \( \epsilon \propto B \) 的缩放）。它是对论文思想的简化验证，而非复现。

#### 4.4.4 代码实践

**实践目标**：把「批量递增」搬到便宜得多的 MNIST MLP 上，验证你能**预判**调度代码的行为。

1. 新建 notebook，加载 MNIST 与任一 MLP（可直接复制 `gradclipping_mlp.ipynb` 的模型与数据 cell），设 `NUM_EPOCHS=20`、初始 `batch_size=64`。
2. 照抄调度骨架（示例代码）：

```python
# 示例代码：MNIST 版批量递增调度
batch_sizes = np.arange(64, 1025, 128)   # [64, 192, 320, ...] 共 8 档
batch_size_index = 0
for epoch in range(NUM_EPOCHS):
    if epoch > (NUM_EPOCHS//2) and not epoch % (NUM_EPOCHS//len(batch_sizes)):
        train_loader = DataLoader(train_dataset,
                                  batch_size=int(batch_sizes[batch_size_index]),
                                  shuffle=True)
        batch_size_index += 1
        print(f'-- epoch {epoch}: batch_size -> {batch_sizes[batch_size_index-1]}')
    # ...正常训练循环...
```

3. 先手动推演：`NUM_EPOCHS//2` 与 `NUM_EPOCHS//len(batch_sizes)` 各是多少？哪些 epoch 会触发换档？实际批量最大到几？把答案写下来再跑。
4. 跑完后对照换档日志与你的推演，再和同超参、恒定 `batch_size=64` 的基线比较测试准确率。

**需要观察的现象**：换档日志的 epoch 序号与你手工推导一致；换档后 `len(train_loader)` 立刻变小。
**预期结果**：MNIST 上两种策略最终准确率差距很小（任务太简单），但后期每 epoch 的 batch 数明显减少、单 epoch 耗时可能略降；重点是验证你已能预判调度代码的行为。具体数字**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：按 \( g \approx \epsilon N / (B(1-m)) \)，若想把噪声降为原来的一半，除衰减 \( \epsilon \) 外还有什么办法？各有什么代价？

**答案**：把 \( B \) 翻倍（\( g \) 减半），或把动量 \( m \) 提高（如 0.9→0.95 也使 \( 1-m \) 减半）。代价：增大 \( B \) 提高单步显存与并行度需求、减少更新次数；提高 \( m \) 引入更长历史依赖、可能让训练更震荡。公式见 [L94-L96](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cnn-alexnet-cifar10-batchincrease.ipynb#L94-L96)。

**练习 2**：组 2 的调度代码里，为什么第 25 个 epoch（0 起为 24）「换档等于没换」？最终批量为何停在 1792 而不是调度表末端的 4864？

**答案**：条件首次在 epoch 24 成立时游标还是 0，重建 loader 用的是 `batch_sizes[0]=256`（与原批量相同），之后游标才加一；40 个 epoch 内条件只在 24/28/32/36 成立 4 次，游标最多走到 4，实际用到的最大档位是 `batch_sizes[3]=1792`。列表后半段（2304–4864）永远不会被用到——调度表长度与 epoch 数不匹配是该实现的一个小缺陷。见 [L1077-L1084](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cnn-alexnet-cifar10-batchincrease.ipynb#L1077-L1084)。

**练习 3**：增大 batch size 为什么「可能减少总更新次数」却不减少训练的 epoch 数？

**答案**：每个 epoch 仍完整遍历一遍训练集（样本数不变），变的是每个 epoch 被切成多少个 batch——\( B \) 越大 batch 数越少，`optimizer.step()` 的调用次数按比例减少。更新的「质量」则因每个梯度由更多样本平均而更稳（方差更小），这正是噪声下降的来源。

---

## 5. 综合实践

**任务：在你的 MNIST MLP 上走完「Range Test → 定界 → 修正版 triangular2 → 三方对比」的完整流程。**

这个任务把本讲四个模块串成一条真实工作流（也是实践中调学习率的标准姿势）：

1. **准备**：复制 `gradclipping_mlp.ipynb` 的模型与数据 cell 到新 notebook；把 CLR 函数（[cyclical-learning-rate.ipynb:L140-L163](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/tricks/cyclical-learning-rate.ipynb#L140-L163)）复制进来；优化器换成 SGD。
2. **Range Test**（4.1.4 的实践）：扫 5 个 epoch，`base_lr=0.01 → max_lr=1.0`，画「损失–lr」曲线，读出 `(lr_min, lr_max)`。
3. **三组受控对比**（除指定变量外一切相同：各 10 个 epoch、同一随机种子、逐 epoch 记录验证准确率）：
   - A 组：固定 `lr = lr_min`；
   - B 组：修正版 `triangular2`——每 batch 更新 lr、`batch_step` 正确自增（4.2.4 的示例代码）；
   - C 组：B 组基础上在 `backward` 与 `step` 之间加 `clip_grad_norm_(model.parameters(), 1.)`（4.3 的方式三），看看「大学习率 + 裁剪」能否让循环上限取得更激进。
4. **对齐分析**：把三条验证准确率曲线画进同一张图；再画 B 组的「lr–batch step」曲线，确认它是**逐周期减半的锯齿**（而不是仓库 notebook 里那种近似的爬坡线——你现在知道该怎么核对代码了）。
5. **结论**：用一段话回答——在你的设置下，CLR 相比固定 lr 带来了什么？梯度裁剪改变了什么？如果 B/C 不如 A，最可能的原因出在哪个环节（边界？半周期长度？模型太简单）？

**验收标准**：能展示 3 张图（损失–lr、准确率对比、lr 调度曲线），并解释每张图与代码的对应关系。所有训练结果**待本地验证**。

## 6. 本讲小结

- **LR Range Test** 用 5–10 个 epoch 的线性 lr 扫描换取 `base_lr/max_lr` 的经验定界；线性扫描本身复用 triangular 公式（`step_size = 总 batch 数` 时恰为线性斜坡），逐 batch 记录损失/准确率后横轴换成 lr 读图。
- **循环学习率**的三角波核心是 `cycle/x/lr_delta` 三行公式；`triangular2` 每周期振幅减半、`exp_range` 乘 \( \gamma^t \) 指数衰减；手动改 lr 的标准写法是 `for g in optimizer.param_groups: g['lr'] = ...`。
- **读源码要抠变量来源**：仓库三个正式 CLR 训练 cell 因 `batch_step` 未重置未自增、lr 移到 epoch 级更新、返回值写回 `base_lr`，实际 lr 动态并非锯齿——保存输出（无任何批级日志）可交叉验证。
- **梯度裁剪**三法：`clip_grad_value_`（元素级、对称）、hook + `clamp`（元素级、可非对称，仓库实验证明非对称裁剪会系统性偏置更新、准确率掉约 10 个点）、`clip_grad_norm_`（整体 L2 范数等比缩放、保方向，最常用）；时序必须卡在 `backward` 与 `step` 之间。
- **batch size 退火**的理论依据是噪声尺度 \( g \approx \epsilon N/(B(1-m)) \)：衰减 \( \epsilon \) 可等价换成增大 \( B \)；仓库实现是「过半程后每隔 `NUM_EPOCHS//len(batch_sizes)` 个 epoch 重建更大批量的 DataLoader」，CIFAR-10 上验证/测试精度 +1.5–3.5 个百分点、耗时还略降。
- 本讲两次演示了「代码推导 ↔ 保存输出对账」的读码姿势：CLR 训练 cell 的失效日志条件、批量递增的换档 epoch 与批数变化，都是先算后验的。

## 7. 下一步学习建议

- **u7-l2（K 折交叉验证）**：本讲所有对比都是单次运行，学习「用 K 折的均值±标准差判断差异是否可信」，补上实验统计这一课。
- **u7-l4（迁移学习）**：从「调训练过程」转向「调预训练模型」——微调时学习率通常要小 1–2 个数量级，本讲的 Range Test 思想可直接迁移过去。
- **源码延伸**：读 `pytorch_ipynb/helper_train.py` 中 `train_classifier_simple_v2` 的学习率调度接入点（u4-l4 讲过），对比它与本讲手写调度器的关系；有兴趣可再对照 PyTorch 官方 `torch.optim.lr_scheduler.CyclicLR`（与本讲手写函数同源）阅读，理解 `scale_mode` 等参数如何对应 `triangular2`/`exp_range` 的修正项。
