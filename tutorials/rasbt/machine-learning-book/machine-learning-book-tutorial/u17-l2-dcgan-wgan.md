# DCGAN 与 WGAN-GP 进阶

## 1. 本讲目标

上一讲（u17-l1）我们用全连接层搭出了第一个 GAN：生成器与判别器都是普通的 `nn.Linear` 堆叠，图像被摊平成一维向量处理。本讲进入 `ch17/ch17_part2.py`，完成两次升级：

1. **结构升级（DCGAN）**：把全连接换成卷积——判别器用带步幅（strided）的 `nn.Conv2d` 学习式下采样，生成器用 `nn.ConvTranspose2d`（转置卷积/反卷积）上采样，两者构成一对严格镜像。
2. **训练目标升级（WGAN-GP）**：放弃 BCE 损失，改用「EM 距离 + 梯度惩罚」训练同一个 DCGAN 架构，理解 1-Lipschitz 约束为什么要从原始 WGAN 的权重截断演进为梯度惩罚。

学完本讲，你应该能够：

- 手推 DCGAN 生成器与判别器每一层的特征图尺寸，解释镜像结构；
- 理解潜在噪声为何是 `(batch_size, z_size, 1, 1)` 形状，以及 `mode_z` 的两种分支；
- 说清权重截断 `[-0.01, 0.01]` 的思想与缺陷（并知道仓库代码并未实现它）；
- 逐行读懂 `gradient_penalty` 的插值、求导与惩罚构造；
- 对比 BCE GAN 与 WGAN-GP 训练循环的三处本质差别，并设计实验验证梯度惩罚系数 λ 的作用。

## 2. 前置知识

本讲默认你已完成 u17-l1（GAN 对抗训练、BCELoss、翻转标签技巧、固定噪声监控）和 u14-l1（卷积输出尺寸公式）。以下三个新概念用通俗语言先建立直觉：

**（1）带步幅卷积 = 可学习的下采样。** 第 14 章的卷积网络里，缩小特征图靠 MaxPool；DCGAN 的判别器把池化换成 `stride=2` 的卷积——窗口每次跳 2 格滑动，输出尺寸减半。好处是「往哪里压缩信息」也变成可学习参数，而不是固定的取最大值。

**（2）EM 距离（推土机距离）。** GAN 的本质是让生成分布 \(P_g\) 靠近真实分布 \(P_r\)。JS 散度（原版 GAN 的隐含目标）有个毛病：两个分布不相交时，无论隔得远还是近，JS 都是一个常数，梯度为零，生成器失去学习信号。EM 距离则度量「把一堆生成的土搬运到真实土堆所需的最小搬运量」，分布隔多远、搬多少土是连续变化的：

\[
W(P_r, P_g) = \inf_{\gamma \in \Pi(P_r, P_g)} \; \mathbb{E}_{(x, y) \sim \gamma} \, \|x - y\|
\]

距离连续可导，梯度就不会凭空消失——这是 WGAN 训练更稳的理论根源。

**（3）Lipschitz 连续与 critic。** 直接优化上式不可行，需要 Kantorovich–Rubinstein 对偶把它转成最大化问题，但前提是打分函数 \(f\) 必须满足 1-Lipschitz 约束——即输出变化不能快过输入变化：

\[
\|f(x_1) - f(x_2)\| \le L \, \|x_1 - x_2\|, \quad L \le 1
\]

WGAN 里扮演 \(f\) 的网络不再输出「真图概率」，而是输出一个分数，所以改叫 **critic（评价器）**。如何让 critic 满足 1-Lipschitz，正是本讲从「权重截断」讲到「梯度惩罚」的主线。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ch17/ch17_part2.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L1-L529) | 本讲唯一核心源码，从上到下依次是：数据准备 → DCGAN（生成器/判别器/BCE 训练循环）→ WGAN-GP（换归一化的模型/gradient_penalty/WGAN 训练循环） |
| [ch17/ch17_part2.py:L328-L511](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L328-L511) | 脚本中 "Dissimilarity measures"、"EM distance"、"Gradient penalty"、"Mode collapse" 等小节只剩标题，推导正文在书里，代码只有 WGAN-GP 实现 |
| [ch17/ch17_part2.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.ipynb) | 等价 notebook，**实践任务的推荐载体**：代码按单元切分，便于反复重跑 WGAN 段做 λ 实验 |
| [ch17/ch17_part1.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part1.py#L1-L1) | u17-l1 已精读的全连接版 GAN，本讲对照引用其训练思路 |

另外：[ERRATA/README.md:L334-L348](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ERRATA/README.md#L334-L348) 中第 17 章唯一勘误是书中 KL 散度信息框的公式笔误，与本讲代码无关。

## 4. 核心概念与源码讲解

本讲的四个最小模块：**判别器 strides 模型设计**、**生成器 ConvTranspose2d 反卷积**、**判别器权重截断**（概念模块）、**WGAN-GP 梯度惩罚训练循环**。

### 4.1 判别器 strides 模型设计

#### 4.1.1 概念说明

全连接判别器把 28×28 图像摊平成 784 维向量，丢掉了空间结构。DCGAN 判别器则是一串「卷积 + LeakyReLU + BatchNorm」块，用 `stride=2` 的卷积逐步把 28×28 的图像压缩成一个标量分数。三个值得注意的设计约定（都出自 DCGAN 论文，也体现在代码里）：

- 用 strided conv 替代池化做下采样（学习式压缩）；
- 隐藏层激活用 LeakyReLU(0.2)，避免死区（承接 u17-l1）；
- BatchNorm 不放在第一层（第一层卷积直接吃原始输入），避免批内样本相互耦合。

#### 4.1.2 核心流程

卷积输出尺寸公式（承接 u14-l1，高宽各算一遍）：

\[
H_{out} = \left\lfloor \frac{H_{in} + 2p - k}{s} \right\rfloor + 1
\]

代入判别器四层卷积（kernel/stride/padding 依次为 4/2/1、4/2/1、3/2/1、4/1/0）：

| 层 | 输出形状（n_filters=32） | 说明 |
| --- | --- | --- |
| Conv2d(1, 32, **4, 2, 1**) | (B, 32, 14, 14) | 28→14，无 BN |
| Conv2d(32, 64, **4, 2, 1**) + BN | (B, 64, 7, 7) | 14→7 |
| Conv2d(64, 128, **3, 2, 1**) + BN | (B, 128, 4, 4) | 7→4 |
| Conv2d(128, 1, **4, 1, 0**) + Sigmoid | (B, 1, 1, 1) | 4→1，压成每图一个概率 |

最后一层卷积把 4×4 特征图直接卷成 1×1，等价于一个作用在空间上的全连接头；`view(-1, 1)` 后得到 `(batch_size, 1)` 的真图概率。

#### 4.1.3 源码精读

判别器定义在 [ch17/ch17_part2.py:L170-L190](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L170-L190)：`__init__` 用 `nn.Sequential` 堆叠四个卷积块，只有第二、三块接 `nn.BatchNorm2d`，第一块刻意不接；所有卷积 `bias=False`，因为随后的归一化层自带偏置（BatchNorm 的 β 平移参数），卷积偏置是冗余的。

[ch17/ch17_part2.py:L188-L190](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L188-L190) 的 `forward` 把 `(B, 1, 1, 1)` 的输出 `view(-1, 1)` 成 `(B, 1)` 概率向量（`squeeze(0)` 在批量大于 1 时不起作用，属防御性写法）。

实例化与打印在 [ch17/ch17_part2.py:L195-L201](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L195-L201)：`z_size = 100`、`n_filters = 32`（基础通道数，各层按 ×1/×2/×4 扩张），`print(disc_model)` 可直接看到整个结构。

#### 4.1.4 代码实践

**目标**：不训练，只用前向传播验证 4.1.2 的尺寸表。

1. 把 [ch17/ch17_part2.py:L170-L190](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L170-L190) 的 `Discriminator` 类定义复制到你自己的 notebook 或脚本（不要在仓库源文件上改）。
2. 运行下面的示例代码：

```python
# 示例代码（非仓库原有代码）
import torch
torch.manual_seed(1)

disc = Discriminator(n_filters=32)
x = torch.randn(8, 1, 28, 28)     # 8 张标准形状的假输入
proba = disc(x)
print(proba.shape)                # 预期: torch.Size([8, 1])
```

3. 逐层核对：可用 `torch.randn(8, 1, 14, 14)` 跳过第一层的心算，或直接 `print(disc)` 对照 4.1.2 的表。

**需要观察的现象**：输出形状恰为 `(8, 1)`；再打印 `proba` 本身，值全部落在 (0, 1)——Sigmoid 的效果（待本地验证）。

**预期结果**：尺寸表全部对上，说明 `stride=2` 卷积确实把 28×28 压到 1×1，全程无池化。

#### 4.1.5 小练习与答案

**练习 1**：为什么判别器第一层卷积不加 BatchNorm？
**答案**：第一层直接作用在输入样本上，BatchNorm 会在同一批样本之间引入统计耦合（每个样本的输出依赖同批其他样本），削弱判别器对单张图的独立判断；DCGAN 论文明示输入层不接 BN。代码 [L174-L175](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L174-L175) 中第一块确实只有卷积加 LeakyReLU。

**练习 2**：把第三层卷积的 kernel 从 3 改成 4（stride=2, padding=1 不变），输出尺寸变成多少？
**答案**：\(\lfloor(7 + 2 - 4)/2\rfloor + 1 = 3\)，特征图变成 3×3；那么最后一层 Conv2d(128, 1, 4, 1, 0) 要求输入至少 4×4，前向会直接报尺寸错误——这解释了为什么 kernel 取 3：7→4 恰好给最后一层留足 4×4 输入。

### 4.2 生成器 ConvTranspose2d 反卷积与噪声设计

#### 4.2.1 概念说明

生成器要做判别器的逆运算：从一个 100 维噪声向量「放大」出 28×28 图像。直接用 `nn.Upsample` 放大是固定插值，不可学习；`nn.ConvTranspose2d`（转置卷积，俗称反卷积）把「步幅卷积的反向传播梯度」直接当作前向运算，天然是卷积的镜像伙伴——放大到哪里、填什么值都可学习。

仓库实现里噪声的形状值得特别注意：不是 part1 里的 `(batch_size, z_size)`，而是 **`(batch_size, z_size, 1, 1)`**——把 100 个噪声数当作「100 通道的 1×1 图像」，第一层转置卷积再把它放大成 4×4 的 128 通道特征图。这样生成器从第一层起就是纯卷积网络，没有全连接层。

`create_noise` 只实现了两种先验分布分支（规划大纲中提到的 `upsample` 空间噪声模式**未收录于仓库代码**，属书中正文讨论的概念）：`uniform` 分支产出 `[-1, 1]` 均匀分布，`normal` 分支产出标准正态分布。

#### 4.2.2 核心流程

转置卷积输出尺寸公式：

\[
H_{out} = (H_{in} - 1) \times s - 2p + k + p_{out}
\]

代入生成器四层（kernel/stride/padding 依次为 4/1/0、3/2/1、4/2/1、4/2/1）：

| 层 | 输出形状（n_filters=32, z_size=100） | 尺寸变化 |
| --- | --- | --- |
| ConvTranspose2d(100, 128, **4, 1, 0**) + BN | (B, 128, 4, 4) | 1→4 |
| ConvTranspose2d(128, 64, **3, 2, 1**) + BN | (B, 64, 7, 7) | 4→7 |
| ConvTranspose2d(64, 32, **4, 2, 1**) + BN | (B, 32, 14, 14) | 7→14 |
| ConvTranspose2d(32, 1, **4, 2, 1**) + Tanh | (B, 1, 28, 28) | 14→28 |

对比 4.1.2 的表：生成器尺寸链 `1→4→7→14→28` 与判别器 `28→14→7→4→1` 严格互补，且每层的 (kernel, stride, padding) 与判别器对应层**完全相同、只是顺序倒排**——这就是「镜像结构」。末端 Tanh 把像素压到 `[-1, 1]`，与数据管线的 `Normalize(mean=0.5, std=0.5)`（[L130-L133](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L130-L133)）对齐，真图与假图处于同一值域。

#### 4.2.3 源码精读

生成器工厂函数在 [ch17/ch17_part2.py:L151-L168](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L151-L168)：`make_generator_network(input_size, n_filters)` 返回 `nn.Sequential`，每个转置卷积后接 `BatchNorm2d` 与 `LeakyReLU(0.2)`，唯独最后一层只接 `Tanh`（输出层不归一化，与判别器第一层不归一化对称）。

噪声构造在 [ch17/ch17_part2.py:L214-L219](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L214-L219)：`uniform` 分支用 `torch.rand(...)*2 - 1` 把 `[0,1)` 平移缩放到 `[-1,1)`，末两维是 `1, 1` 以匹配 `ConvTranspose2d` 的 4 维输入约定 `(N, C, H, W)`。

采样工具在 [ch17/ch17_part2.py:L275-L281](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L275-L281)：训练前用固定种子造 `fixed_z`，每个 epoch 结束用 `create_samples` 让**同一批噪声**过生成器，把 `(x+1)/2` 映射回 `[0,1]` 供 matplotlib 显示——固定噪声才能逐轮对比「同一张图如何进化」（承接 u17-l1 的监控思想）。

#### 4.2.4 代码实践

**目标**：验证 `z_size` 只是「1×1 噪声图的通道数」，改它不影响输出尺寸链。

1. 把 [L151-L168](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L151-L168) 的 `make_generator_network` 与 [L214-L219](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L214-L219) 的 `create_noise` 复制到自己的 notebook。
2. 运行示例代码：

```python
# 示例代码（非仓库原有代码）
gen = make_generator_network(input_size=50, n_filters=32)  # z_size 从 100 改成 50
z = create_noise(8, 50, mode_z='uniform')   # (8, 50, 1, 1)
print(gen(z).shape)                          # 预期: torch.Size([8, 1, 28, 28])
```

**需要观察的现象**：输出仍是 `(8, 1, 28, 28)`，只有第一层转置卷积的权重形状变化（通道 100→50）。

**预期结果**：确认潜在维度只影响第一层的「入口通道数」，上采样尺寸链由 kernel/stride/padding 决定，与 `z_size` 无关（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`uniform` 分支里 `*2 - 1` 的作用是什么？不加会怎样？
**答案**：`torch.rand` 产出 `[0,1)`；乘 2 减 1 平移到 `[-1,1)`，使噪声零均值、正负对称。不加的话噪声全为正，分布偏斜，训练初期生成器收到的信号有系统性偏置。

**练习 2**：为什么生成器末端用 Tanh 而不是 Sigmoid？
**答案**：Tanh 输出 `[-1,1]`，恰与数据管线 `Normalize((0.5,), (0.5,))` 把 MNIST 像素映射到的 `[-1,1]` 区间对齐（[L130-L133](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L130-L133)）；Sigmoid 的 `(0,1)` 与之错位，真图假图值域不一致，判别器可以轻易区分，对抗失去意义。

### 4.3 判别器权重截断：1-Lipschitz 的第一种方案（概念模块）

#### 4.3.1 概念说明

要critic逼近 EM 距离的对偶形式，必须把 critic 限制在 1-Lipschitz 函数族内。原始 WGAN（Arjovsky et al., 2017）的做法最直接粗暴：**训练中每一步更新完，把 critic 的全部权重强行截断到 `[-0.01, 0.01]`**。直觉是：网络输出对输入的敏感度逐层相乘，每层权重有界，复合后的 Lipschitz 常数就有界。

但权重截断有两个著名缺陷（WGAN-GP 论文用实验展示）：

1. **容量浪费**：优化器想把权重推大，截断又把它压回边界，大量权重最终挤在 ±0.01 两个极端值上，critic 实际只能表达简单函数；
2. **梯度病态**：截断下梯度范数随层数加深不是爆炸就是消失，深 critic 无法训练。

因此改进方案是**不限制权重本身，只限制 critic 的输出梯度**——即下一模块的梯度惩罚。

> **读码澄清（重要）**：仓库 `ch17/ch17_part2.py` **没有实现权重截断**——全文搜不到 `clamp`/`clip` 相关调用，WGAN 部分直接给出的是梯度惩罚版本（WGAN-GP）。本模块按「书中正文的概念」讲解，配套的代码均为示例代码。

#### 4.3.2 核心流程

权重截断方案的训练循环与 BCE 版几乎相同，仅在 critic 每步 `optimizer.step()` 之后增加：

```text
对 critic 的每个参数 p：  p ← clamp(p, -c, c)   # c = 0.01
```

对照 Kantorovich–Rubinstein 对偶，critic 最大化下式（等价于最小化其相反数）：

\[
W(P_r, P_g) = \sup_{\|f\|_L \le 1} \; \mathbb{E}_{x \sim P_r}[f(x)] - \mathbb{E}_{z \sim p(z)}[f(g_\theta(z))]
\]

权重截断是保证 \(\|f\|_L \le 1\) 的**硬约束**（直接改参数），梯度惩罚则是下一模块的**软约束**（加惩罚项）。

#### 4.3.3 源码精读

本模块在仓库中无可引用的实现代码；可引用的「证据」是 [ch17/ch17_part2.py:L401-L418](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L401-L418) 的 `gradient_penalty`——仓库选择用惩罚替代截断。若要体会截断写法，以下为示例代码：

```python
# 示例代码（非仓库原有代码）：原始 WGAN 的权重截断长什么样
for p in disc_model.parameters():
    p.data.clamp_(-0.01, 0.01)
```

#### 4.3.4 代码实践

**目标**：亲手确认「仓库没有权重截断实现」，避免张冠李戴。

1. 在仓库根目录执行：`grep -n "clamp\|clip" ch17/ch17_part2.py`。
2. 观察 WGAN 段（L346 之后）的全部代码，确认 critic 更新处只有 `zero_grad → backward → step` 三步，见 [L424-L439](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L424-L439)。

**需要观察的现象**：grep 无匹配（`ch17_part1.ipynb` 中 grep 到的 "clip" 命中只是内嵌 base64 图片数据的巧合字符）。

**预期结果**：确认仓库实现的是 WGAN-GP，不是截断版 WGAN。

#### 4.3.5 小练习与答案

**练习 1**：为什么「每层权重有界」能（粗略地）保证复合网络的 Lipschitz 常数有界？
**答案**：复合函数的 Lipschitz 常数近似等于各层常数的乘积；每一层输出变化上界正比于该层权重范数与输入变化，把每层权重按元素压在 `[-c, c]` 内，每层范数就有上界，逐层相乘后整体仍有界。这是充分不必要条件——它连「不需要那么小」的权重也一并压扁，这正是容量浪费的来源。

**练习 2**：权重截断是硬约束，梯度惩罚是软约束。「软」体现在哪里？
**答案**：梯度惩罚不修改权重，只是在损失里加一项 \((\|\nabla_{\hat{x}} D\|_2 - 1)^2\)，允许 critic 在「梯度范数接近 1」与「主目标」之间权衡；只要惩罚系数足够大，最优解处范数会贴近 1，但优化路径上并不强制每一刻都满足。

### 4.4 WGAN-GP 梯度惩罚训练循环

#### 4.4.1 概念说明

WGAN-GP 用梯度惩罚实现（近似的）1-Lipschitz 约束：在真图与假图的**连线上随机插值**取样，惩罚 critic 在这些点上的梯度范数偏离 1 的平方：

\[
\text{GP} = \lambda \; \mathbb{E}_{\hat{x} \sim P_{\hat{x}}} \left[ \left( \|\nabla_{\hat{x}} D(\hat{x})\|_2 - 1 \right)^2 \right],
\qquad \hat{x} = \epsilon \, x_{real} + (1 - \epsilon)\, x_{fake}, \;\; \epsilon \sim U[0,1]
\]

为什么「梯度范数处处等于 1」与 Lipschitz 有关？因为 Lipschitz 常数正是梯度的上确界范数 \(\sup \|\nabla f\|\)；惩罚偏离 1 的平方，既阻止梯度超过 1（保证约束），也轻推它别太小（避免梯度消失），一举两得。

由此，critic（判别器换叫法）与生成器的损失变为：

- critic：最小化 \(E[D(fake)] - E[D(real)] + \text{GP}\)（前两项是 EM 距离估计的相反数，GP 是约束）；
- 生成器：最小化 \(-E[D(fake)]\)（让 critic 给假图打更高分）。

没有 BCE、没有概率、没有标签翻转。配套还有两个结构性改动：BatchNorm 换成 InstanceNorm（WGAN-GP 论文指出批归一化让批内样本的 critic 输出相互耦合，破坏逐样本梯度惩罚的假设）；每个 batch 先训 critic 5 次、再训生成器 1 次（critic 越接近最优，EM 估计越准）。

#### 4.4.2 核心流程

WGAN 训练循环每个 batch 的流程（对应 [L470-L477](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L470-L477)）：

```text
for 每个 batch x:
    重复 5 次 (critic_iterations):
        d_train_wgan(x):
            真图过 critic 得 d_real；噪声过 G 得假图，过 critic 得 d_generated
            gp = 在 x 与 g_output 连线上插值，惩罚梯度范数偏离 1
            d_loss = mean(d_generated) - mean(d_real) + gp
            只对 critic 参数 backward + step
    g_train_wgan(x):
        噪声过 G 得假图，过 critic 得 d_generated
        g_loss = - mean(d_generated)
        只对 G 参数 backward + step
```

与 BCE 版（[L225-L270](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L225-L270)）的差别一览：

| 维度 | BCE GAN（u17-l1 / 本讲 DCGAN 段） | WGAN-GP |
| --- | --- | --- |
| 判别器输出 | Sigmoid 概率 | 分数（本仓库实现仍保留 Sigmoid，见下） |
| D 损失 | `BCE(D(x), 1) + BCE(D(G(z)), 0)` | `mean(D(fake)) - mean(D(real)) + λ·GP` |
| G 损失 | `BCE(D(G(z)), 1)`（翻转标签） | `- mean(D(G(z)))` |
| 每批训练次数 | D、G 各 1 次 | critic 5 次、G 1 次 |
| 归一化 | BatchNorm2d | InstanceNorm2d |
| 学习率 | G 0.0003 / D 0.0002（[L208-L209](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L208-L209)） | 双方均 0.0002（[L393-L394](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L393-L394)） |

#### 4.4.3 源码精读

**（a）gradient_penalty 逐行精读**，见 [ch17/ch17_part2.py:L401-L418](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L401-L418)：

- [L405](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L405)：`alpha` 形状 `(B, 1, 1, 1)` 且 `requires_grad=True`——四维里只有批维非 1，广播后**每张图一个插值系数**（同图所有像素共用 ε）；`requires_grad=True` 使插值样本成为可求导的「叶」，这是下一步对输入求梯度的前提。
- [L406](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L406)：`interpolated = alpha * real_data + (1 - alpha) * generated_data`——真图与假图连线上的随机点，即公式中的 \(\hat{x}\)。
- [L409](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L409)：插值样本过 critic 得分数。
- [L412-L414](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L412-L414)：`torch_grad`（即 `torch.autograd.grad`，在 [L14](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L14) 导入）计算 \(\partial D(\hat{x}) / \partial \hat{x}\)。三个参数各司其职：`grad_outputs=ones` 因为输出是向量，取「对全 1 向量的雅可比-向量积」等价于对各样本分数求和再求导；`create_graph=True` 让**求导本身构成计算图**，使惩罚项稍后还能对 critic 参数 backward（二阶导路径）；`retain_graph=True` 保留中间图供后续反向。
- [L416-L418](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L416-L418)：把每张图的梯度摊平成 `(B, 784)`，沿特征维求 L2 范数得每样本范数，惩罚 `(‖·‖ - 1)²` 的批均值，最后乘全局系数 `lambda_gp`。

**（b）d_train_wgan**，见 [ch17/ch17_part2.py:L424-L439](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L424-L439)：损失一行在 [L435](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L435)——`d_loss = d_generated.mean() - d_real.mean() + gradient_penalty(...)`。注意传给 `gradient_penalty` 的是 `x.data` 与 `g_output.data`：`.data` 切断生成器的计算图，保证惩罚项的梯度**只流向 critic 参数**，既省算力又避免污染 G 的梯度缓存。`zero_grad → backward → step` 只更新 critic。

**（c）g_train_wgan**，见 [ch17/ch17_part2.py:L445-L459](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L445-L459)：损失仅一行 `g_loss = -d_generated.mean()`（[L453](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L453)），最大化 critic 给假图的平均分。

**（d）WGAN 模型与训练循环**：生成器 `make_generator_network_wgan`（[L346-L363](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L346-L363)）与 critic `DiscriminatorWGAN`（[L365-L385](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L365-L385)）把 DCGAN 版里的 `BatchNorm2d` 全部换成 `InstanceNorm2d`，卷积形状原封未动。训练循环在 [L464-L483](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L464-L483)：`lambda_gp = 10.0`（WGAN-GP 论文推荐值）、`critic_iterations = 5`；[L474-L476](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L474-L476) 内层 `for` 连训 critic 5 次，**只把最后一次的 d_loss 记入列表**（`d_losses.append` 在循环外）；日志也只打印 D 损失（[L479-L480](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L479-L480)）。

**（e）两处诚实的读码观察**（仓库实现与论文常规写法的差别，读码时应注意）：

1. `DiscriminatorWGAN` 末端仍保留 `nn.Sigmoid()`（[L380-L381](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L380-L381)），即 critic 分数被压回 `(0, 1)`；原始 WGAN 论文的 critic 通常不加 Sigmoid、分数无界。由于 sigmoid 的导数在两端饱和，这一保留会削弱梯度惩罚「鼓励梯度范数为 1」的能力——它更像本书的一个实现取舍，做对比实验时应知晓。
2. `lambda_gp` 定义于 [L465](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L465)，而 `gradient_penalty`（L401）在其**之前**定义却引用它——Python 在函数调用时才查找全局名，因此调整 λ 只需在训练循环前改这一处，实践任务将利用这一点。

#### 4.4.4 代码实践

**目标**：把 critic 损失拆成「EM 估计项 + 惩罚项」，观察两部分随训练的相对消长。

1. 打开 `ch17/ch17_part2.ipynb`（或把 [L401-L439](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L401-L439) 复制到自己的脚本），把 `d_train_wgan` 中的一行损失改写为等价的两步并加日志：

```python
# 示例代码（非仓库原有代码）：等价改写 + 日志
gp = gradient_penalty(x.data, g_output.data)
d_loss = d_generated.mean() - d_real.mean() + gp
if i % 50 == 0:   # 每 50 个 batch 打一次
    print(f'EM估计项 {(d_generated.mean()-d_real.mean()).item():+.4f} | GP项 {gp.item():.4f}')
```

2. 数据准备同 DCGAN 段（[L129-L146](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L129-L146)），首次运行需把 [L137](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L137) 的 `download=False` 改为 `True`（在 notebook 单元里改，不动仓库文件）。

**需要观察的现象**：训练初期 GP 项偏大（critic 梯度范数未受控），随训练推进 GP 项回落到接近某个小稳态值，EM 估计项围绕小幅波动。

**预期结果**：λ=10 时 GP 项整体被压得较小，说明约束大体满足；若把 λ 改成 1，GP 项与 EM 项都会明显变大且更抖（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `alpha` 的形状是 `(B, 1, 1, 1)` 而不是 `(B, 1, 28, 28)`（每像素独立插值）？
**答案**：惩罚项的理论依据是在真假两分布之间的「直线路径」上约束梯度，每样本取一个 ε、整图同一系数，插值点才严格落在连线上；若逐像素独立采样，插值的是真假图的一个随机逐像素混合体，不再是两点连线，约束的几何意义被破坏。且实现上 `(B,1,1,1)` 直接广播即可。

**练习 2**：`torch_grad(..., create_graph=True)` 若写成 `create_graph=False` 会发生什么？
**答案**：对 `interpolated` 求梯度这个过程不建图，得到的 `gradients` 是「常数」；随后 `d_loss.backward()` 时惩罚项 \((\|\nabla\|-1)^2\) 对 critic 参数的导数无法回传，梯度惩罚失去约束力——critic 只会最小化均值差而不受任何 Lipschitz 限制，退化为无约束 critic。

**练习 3**：为什么 WGAN 版把 BatchNorm 换成 InstanceNorm？
**答案**：BatchNorm 的输出依赖同批其他样本的统计量，使「单样本的 critic 输出」不再是单样本的函数，对插值样本求出的梯度混入了批内耦合，梯度惩罚的假设失效；InstanceNorm 逐样本归一化，保持样本独立。WGAN-GP 论文明确建议用 LayerNorm（无批统计）或 InstanceNorm 替代，仓库选了后者。另可注意 PyTorch 中 `InstanceNorm2d` 默认 `affine=False`（无可学习缩放平移），与 `BatchNorm2d` 默认 `affine=True` 不同，这是换归一化层时容易被忽略的副作用。

## 5. 综合实践

**任务**：在相同 epoch 数下对比训练 DCGAN（BCE）与 WGAN-GP，保存两者固定噪声生成样本的对比图；再对梯度惩罚系数 λ 做 {1, 10, 100} 三点扫描，用损失曲线解释 λ 的作用。整个任务把本讲四个模块串起来：跑通镜像卷积架构（4.1/4.2）、对比两种训练目标（4.4）、体会 λ 如何拿捏 1-Lipschitz 软约束（4.3/4.4）。

**操作步骤**（推荐在 `ch17/ch17_part2.ipynb` 中做，代码已按单元切分；GPU 环境建议用 Google Colab，承接 u1-l2）：

1. **准备数据**：运行 [L129-L146](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L129-L146) 的数据单元，把 `download=False` 改为 `True` 首次下载 MNIST。
2. **实验 A（架构对比）**：
   - 把两处 `num_epochs`（[L285](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L285) DCGAN 段、[L466](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L466) WGAN 段）都改为 20（CPU 上 100 epoch 过慢）；
   - 依次运行 DCGAN 段（L151-L325）与 WGAN-GP 段（L346-L508）；两边共用同一 `fixed_z`，`epoch_samples` 与 `epoch_samples_wgan` 因此可直接对比；
   - 仿照 [L306-L325](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L306-L325) 的 6×5 网格图（`selected_epochs` 改为如 `[1, 2, 4, 10, 15, 20]`），把两幅图分别存为 `dcgan_samples.png` 与 `wgangp_samples.png`（用 `plt.savefig`，仓库原行的 savefig 被注释在 [L324](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L324) / [L507](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L507)）。
3. **实验 B（λ 扫描）**：只重跑 WGAN-GP 段三次，[L465](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L465) 的 `lambda_gp` 分别设 1、10、100；在训练循环里把每轮的 `d_losses` 均值追加进列表，最后把三条曲线画到同一坐标系（示例代码）：

```python
# 示例代码（非仓库原有代码）：收集并对比三条 d_loss 曲线
history = {1: [], 10: [], 100: []}
for lam in (1, 10, 100):
    lambda_gp = lam          # gradient_penalty 运行时才读取全局名，改这里即可
    losses = []
    for epoch in range(1, 21):
        for i, (x, _) in enumerate(mnist_dl):
            for _ in range(5):
                d_loss = d_train_wgan(x)
            losses.append(d_loss)
        history[lam].append(sum(losses[-len(mnist_dl):]) / len(mnist_dl))
for lam, curve in history.items():
    plt.plot(curve, label=f'lambda={lam}')
plt.xlabel('epoch'); plt.ylabel('critic loss'); plt.legend(); plt.show()
```

**需要观察的现象**：

- 实验A：两组样本随 epoch 都从噪声斑块进化出数字轮廓；DCGAN 的 G/D 两条 BCE 损失曲线剧烈震荡，数值高低与样本质量没有直观对应；WGAN-GP 的 critic 损失数值范围更窄、更平滑（受 4.4.3 (e) 所述 Sigmoid 保留的影响，其取值范围有限）。
- 实验B：λ=1 时曲线抖动大、GP 项偏大（约束太弱，critic 梯度范数失控）；λ=100 时 critic 损失几乎被惩罚项主导、样本改善变慢（critic 被压得过度保守）；λ=10 介于其间，通常样本质量与稳定性最好。

**预期结果**：λ 越小越接近「无约束 critic」（训练不稳），λ 越大越接近「惩罚主导」（逼近 EM 距离的能力被削弱），λ=10 是论文给出的平衡点。以上定性预期**待本地验证**——实际曲线形态依赖随机种子与硬件，建议固定 `torch.manual_seed(1)`（[L467](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L467)）再对比。

## 6. 本讲小结

- **DCGAN 把 GAN 卷积化**：判别器用 `stride=2` 卷积学习式下采样（无池化），生成器用 `ConvTranspose2d` 反卷积上采样；两者的 kernel/stride/padding 序列严格倒排，尺寸链 `1→4→7→14→28` 与 `28→14→7→4→1` 互为镜像。
- **潜在噪声是 (B, 100, 1, 1) 的「100 通道 1×1 图」**，使生成器从第一层起就是纯卷积；仓库 `create_noise` 只实现 `uniform`（`[-1,1)`）与 `normal` 两支，大纲提到的 `upsample` 空间噪声模式未收录于代码。
- **1-Lipschitz 约束的两种方案**：原始 WGAN 用权重截断到 `[-0.01, 0.01]`（硬约束，容量浪费、梯度病态，仓库未实现）；WGAN-GP 改为在真假连线插值点上惩罚梯度范数偏离 1 的平方（软约束，仓库实现）。
- **`gradient_penalty` 的三个工程细节**：`alpha` 形状 `(B,1,1,1)` 保证每图一个插值系数；`create_graph=True` 让惩罚项可对 critic 参数二次求导；调用处传 `.data` 切断生成器图。
- **WGAN 训练循环与 BCE 版的三处本质差别**：损失用均值差（`mean(D(fake)) - mean(D(real)) + λ·GP` 与 `-mean(D(fake))`）、每批先训 critic 5 次、归一化换 InstanceNorm；稳定性收益来自 EM 距离目标比 JS/BCE 的梯度更连续。
- **读码保持怀疑**：仓库 critic 末端仍保留 Sigmoid、WGAN 日志只打印最后一次 critic 损失——实现与论文常规写法的出入，做实验时必须心里有数。

## 7. 下一步学习建议

- 下一讲进入 **u18-l1 图卷积层从零实现**：从「规则网格上的卷积」转向「图结构上的邻接矩阵聚合」，`BasicGraphConvolutionLayer` 与本讲的 `ConvTranspose2d` 一样，都是「把一种结构先验烧进网络结构」的例子。
- 延伸阅读（按性价比排序）：WGAN-GP 论文 *Improved Training of Wasserstein GANs*（重点看权重截断 vs 梯度惩罚的梯度实验图）、DCGAN 论文 *Unsupervised Representation Learning with Deep Convolutional GANs*（本讲两条 BN 摆放约定的出处）、WGAN 原始论文（EM 距离对偶推导）。
- 仓库脚本中 "Mode collapse" 一节（[L511](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py#L511)）只剩标题、无配套代码，模式坍缩的概念建议直接读书中正文，并可回看 u17-l1 讲过的固定噪声监控法来识别它。
- 若想巩固本讲，可回头对照 u14-l1 的卷积尺寸公式把两个网络的参数量算一遍（如生成器第一层 `100→128, k=4` 的权重共 `100×128×4×4 = 204800` 个），体会卷积架构相对全连接的参数效率。
