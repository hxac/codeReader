# 第一个 GAN：MNIST 生成对抗训练

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚生成器（G）和判别器（D）各自的输入、输出与训练目标，理解二者「对抗」在哪里。
2. 读懂 `make_generator_network` / `make_discriminator_network` 两个工厂函数的网络结构，解释 `Tanh`、`LeakyReLU`、`Dropout`、`Sigmoid` 各自出现在那个位置的原因。
3. 理解潜在向量 z 的两种先验分布：`mode_z='uniform'` 与 `mode_z='normal'` 的差别。
4. 逐行读懂 `d_train` / `g_train` 的交替训练循环：BCELoss 怎么用「真图标 1、假图标 0 / 假图标 1」两套标签实现对抗，以及为什么每步只更新一方的参数。
5. 理解 MMD（最大均值差异）如何用核技巧定量评估「生成的假图分布」与「真图分布」的距离。

本讲是生成对抗网络单元的第一讲，后续 u17-l2 将在此基础上升级为 DCGAN 与 WGAN-GP。

## 2. 前置知识

### 2.1 判别模型 vs 生成模型

- **判别模型**：学习「特征 → 标签」的映射，例如前面章节的分类器（逻辑回归、CNN）。它们回答的是「这张图是什么」。
- **生成模型**：学习数据本身的分布 \( p_{\text{data}}(x) \)，然后从中**采样新样本**。它们回答的是「怎么造一张看起来像真数据的图」。

GAN 是生成模型的一种训练框架：它不直接写出 \( p(x) \) 的公式，而是让两个网络互相博弈，间接学会生成。

### 2.2 一个直觉类比：造假币者与鉴定师

- **生成器 G**：造假币者。输入一段随机噪声 z，输出一张「假币」（图像）。
- **判别器 D**：鉴定师。输入一张图，输出「这是真币的概率」。

双方交替进步：鉴定师越犀利，造假者被迫造得越逼真；造假者越逼真，鉴定师被迫练得更犀利。理想终点是假币与真币无法区分——此时 G 就学到了数据分布。

### 2.3 需要的 PyTorch 基础（已在前置讲义建立）

- `nn.Sequential` 搭网络、`nn.Linear` 权重形状 `(out, in)`（u12-l3）。
- 训练五步循环 `zero_grad → 前向 → 算损失 → backward → step`，梯度是**累加**语义（u12-l3）。
- `model.train()` / `model.eval()` 切换 `Dropout` 行为（u14-l2）。
- sigmoid 输出配 `BCELoss`（二分类交叉熵）——u12-l3 已讲过「输出层按任务匹配损失函数」。

### 2.4 MNIST 与本讲的数据流向

MNIST 是 28×28 的手写数字灰度图。代码把每张图**展平成 784 维向量**（`np.prod(image_size)`），所以本讲的 GAN 是全连接（MLP）版本，不涉及卷积——卷积版在 ch17_part2。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [ch17/ch17_part1.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part1.py) | 本讲唯一主源码：G/D 网络定义、噪声生成、数据管线、交替训练循环、MMD 评估，全部在这一个文件里 |
| [ch17/README.md](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/README.md) | 第 17 章大纲：part1 是从零实现 GAN，part2 是 DCGAN 与 WGAN-GP |
| [ERRATA/README.md](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ERRATA/README.md) | 第 17 章勘误：书中 626 页 KL 散度信息框的公式方向与 log 符号有误，正确形式见勘误页（不影响代码） |

与 u1-l1 的仓库约定一致：读码、标行号用 `.py`；想看图文混排的解释可打开等价的 `ch17_part1.ipynb`。文件开头的 `check_packages` 要求 `torch>=1.8.0`、`torchvision>=0.9.0`（[ch17/ch17_part1.py:L35-L42](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part1.py#L35-L42)），u1-l2 搭建的 pyml-book 环境满足要求。

## 4. 核心概念与源码讲解

### 4.1 生成器与判别器网络

#### 4.1.1 概念说明

两个网络结构极不对称，这是有意的：

- **生成器 G**：`20 维噪声 → 100 维隐藏层 → 784 维图像`。它是一个「解码器」，把低维随机向量放大成高维像素。输出端用 `Tanh`，把像素压到 \((-1, 1)\)。
- **判别器 D**：`784 维图像 → 100 维隐藏层 → 1 个标量概率`。它就是一个普通的二分类器（u14-l2 讲过的分类头），输出端用 `Sigmoid` 给出「真图概率」。

注意容量设计：本例中 G 约 8.1 万参数、D 约 7.9 万参数（下面实践里会亲手数出来），二者大致相当。GAN 训练依赖双方力量的**平衡**——这一点是本讲综合实践的核心实验主题。

#### 4.1.2 核心流程

```text
生成器 G(z):
  z (batch, 20)
  → Linear(20, 100) → LeakyReLU        # 隐藏层（可堆叠多层）
  → Linear(100, 784) → Tanh            # 输出层：像素 ∈ (-1, 1)
  → 假图 (batch, 784)

判别器 D(x):
  x (batch, 784)
  → Linear(784, 100, bias=False) → LeakyReLU → Dropout(0.5)   # 隐藏层
  → Linear(100, 1) → Sigmoid           # 输出：真图概率 ∈ (0, 1)
  → 概率 (batch, 1)
```

为什么这么选激活函数：

- **G 输出用 `Tanh` 而非 `Sigmoid`**：因为数据管线上真图被 `Normalize(0.5, 0.5)` 映射到 \([-1,1]\)（见下文 4.1.3），生成器的输出值域必须与之对齐，损失才有共同的「度量衡」。
- **隐藏层用 `LeakyReLU`**：ReLU 在负区间梯度恒为 0，可能产生「死单元」；LeakyReLU 保留一个小斜率，让梯度在两个方向都能流动——GAN 的训练信号本来就不稳定，这点很关键。
- **D 隐藏层加 `Dropout(0.5)` 且隐藏层 `bias=False`**：Dropout 是给 D 的正则化，防止 D 过强、过早把 G「打死」。
- **D 输出用 `Sigmoid`**：`BCELoss` 的输入必须是概率。

#### 4.1.3 源码精读

**生成器工厂函数**：循环堆叠 `Linear + LeakyReLU` 隐藏层，末尾固定 `Linear + Tanh` 输出层。`input_size` 在循环中被逐步改写，实现层与层的自动衔接：

[ch17/ch17_part1.py:L150-L166](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part1.py#L150-L166) —— 定义生成器：`fc_g{i}` 隐藏层接 `relu_g{i}`（实际是 LeakyReLU），最后 `fc_g{num_hidden_layers}` 接 `tanh_g`。

```python
def make_generator_network(
        input_size=20, num_hidden_layers=1,
        num_hidden_units=100, num_output_units=784):
    model = nn.Sequential()
    for i in range(num_hidden_layers):
        model.add_module(f'fc_g{i}', nn.Linear(input_size, num_hidden_units))
        model.add_module(f'relu_g{i}', nn.LeakyReLU())
        input_size = num_hidden_units          # 下一层的输入 = 本层输出
    model.add_module(f'fc_g{num_hidden_layers}',
                    nn.Linear(input_size, num_output_units))
    model.add_module('tanh_g', nn.Tanh())
    return model
```

**判别器工厂函数**：结构与生成器镜像对称（784 进、1 出），差异在三个细节——隐藏层 `bias=False`、多了 `Dropout`、输出端是 `Sigmoid`：

[ch17/ch17_part1.py:L169-L186](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part1.py#L169-L186) —— 定义判别器：隐藏层 `fc_d{i}`（无偏置）+ LeakyReLU + `Dropout(p=0.5)`，输出层接 `Sigmoid` 产出概率。

**超参数与实例化**：

[ch17/ch17_part1.py:L191-L217](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part1.py#L191-L217) —— `image_size=(28,28)`、潜在向量维度 `z_size=20`、G/D 各 1 个隐藏层、各 100 个隐藏单元，随后实例化两个模型并 print。

注意：仓库代码中判别器默认隐藏单元数是 **100**（`disc_hidden_size = 100`），不是某些资料里的 256——本讲综合实践会以这个真实默认值为基线做削弱实验。

**数据管线（为什么是 \([-1,1]\)）**：

[ch17/ch17_part1.py:L228-L240](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part1.py#L228-L240) —— `ToTensor()` 把像素缩放到 \([0,1]\)，`Normalize(mean=0.5, std=0.5)` 再映射到 \([-1,1]\)；随后取出一个样本打印 `Min/Max/shape`，正常应看到 Min≈-1.0、Max≈1.0、`torch.Size([1, 28, 28])`。

```python
transform = transforms.Compose([
    transforms.ToTensor(),
    transforms.Normalize(mean=(0.5), std=(0.5)),
])
```

两点工程细节：

1. `download=False` 是仓库原样——首次运行若本地没有 MNIST，需要把这一项改为 `True`（属于你自己的运行配置，不是修改仓库）。
2. u14-l2 讲过 `ToTensor` 是「除以 255」的区间缩放，`Normalize(0.5, 0.5)` 则是逐像素的 \( x' = (x - 0.5)/0.5 \)，把 \([0,1]\) 拉到 \([-1,1]\)，正好与 G 的 `Tanh` 输出值域对齐。

#### 4.1.4 代码实践

**实践目标**：亲眼确认两个网络的结构与参数量，建立「G、D 体量相当」的直觉。

**操作步骤**（可直接在 `ch17/` 目录下的 notebook 或自己的脚本里执行，以下为示例代码）：

```python
import numpy as np, torch
from ch17_part1 import make_generator_network, make_discriminator_network  # 或直接复制函数定义

image_size = (28, 28)
z_size = 20
gen_model = make_generator_network(input_size=z_size,
        num_hidden_layers=1, num_hidden_units=100,
        num_output_units=np.prod(image_size))
disc_model = make_discriminator_network(input_size=np.prod(image_size))
print(gen_model)
print(disc_model)
print('G params:', sum(p.numel() for p in gen_model.parameters()))
print('D params:', sum(p.numel() for p in disc_model.parameters()))
```

**需要观察的现象**：print 出的两个 `Sequential` 结构应与 4.1.2 的流程图逐层对应。

**预期结果**：G 参数量 = \(20×100+100 + 100×784+784 = 81284\)；D 参数量 = \(784×100\)（无偏置）\(+\ 100×1+1 = 78501\)。若你的数字对不上，检查是否漏了某个偏置。生成器略大于判别器，但同一量级。

#### 4.1.5 小练习与答案

**练习 1**：把生成器的 `Tanh` 换成 `Sigmoid`（即输出 \((0,1)\)），其余不变，会发生什么？

**答案**：G 的输出值域 \((0,1)\) 与真图的 \([-1,1]\) 不对齐，判别器可以仅凭「像素是否出现接近 -1 的值」这类廉价统计特征区分真假，G 的损失很难降下去；即便收敛，生成图像的对比度也会系统性偏低。若真要用 `Sigmoid`，数据管线须同步改成只做 `ToTensor()`（保持 \([0,1]\)）。

**练习 2**：为什么 `Dropout` 只出现在判别器、不出现在生成器？

**答案**：Dropout 在这里是正则化手段，用来限制判别器的拟合能力，防止 D 过强导致 G 得不到有效梯度（G 的梯度要穿过 D 才能回传）。生成器若也加 Dropout，会直接破坏它要生成的图像内容（随机抹掉像素维度），通常得不偿失。

### 4.2 create_noise：两种潜在向量输入模式

#### 4.2.1 概念说明

生成器的输入 z 叫**潜在向量**（latent vector），它从一个人为选定的先验分布 \( p_z(z) \) 中采样。代码提供两种模式：

- `uniform`：\([-1, 1]\) 上的均匀分布，每个分量等概率取值，有界。
- `normal`：标准正态分布 \( \mathcal{N}(0, 1) \)，无界，大部分分量集中在 ±3 以内。

两种都能用，但会影响生成结果的特性：均匀分布的样本在 20 维超立方体内「均匀铺开」；正态分布的样本倾向于聚在高维球壳附近（高维几何特性），且尾部更长——训练后期从尾部采样的 z 可能生成更「罕见」的样本。理论上只要 G 够强，任何连续分布都能被映射成数据分布，差别在训练难易与插值性质。

#### 4.2.2 核心流程

```text
create_noise(batch_size, z_size, mode_z):
  uniform → torch.rand(batch, z_size) * 2 - 1   # rand 在 [0,1)，平移缩放到 [-1,1)
  normal  → torch.randn(batch, z_size)           # 标准正态
```

#### 4.2.3 源码精读

[ch17/ch17_part1.py:L245-L250](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part1.py#L245-L250) —— 一个三行分支函数，返回 `(batch_size, z_size)` 的张量。

```python
def create_noise(batch_size, z_size, mode_z):
    if mode_z == 'uniform':
        input_z = torch.rand(batch_size, z_size)*2 - 1
    elif mode_z == 'normal':
        input_z = torch.randn(batch_size, z_size)
    return input_z
```

[ch17/ch17_part1.py:L257-L275](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part1.py#L257-L275) —— 用 `batch_size=32` 做一次「数据流体检」：取一批真图 `input_real`（`view(batch_size, -1)` 展平为 `(32, 784)`），采样 `input_z`（`mode_z = 'uniform'`，见 L263），依次过 G 和 D，打印各步形状。

预期输出（待本地验证，以仓库代码逻辑推得）：

```text
input-z -- shape: torch.Size([32, 20])
input-real -- shape: torch.Size([32, 784])
Output of G -- shape: torch.Size([32, 784])
Disc. (real) -- shape: torch.Size([32, 1])
Disc. (fake) -- shape: torch.Size([32, 1])
```

另注意：`mode_z` 是模块级全局变量，后面 `d_train` / `g_train` 通过闭包直接引用它（L263 赋值一次，训练全程生效）。想换分布只需改这一处。

#### 4.2.4 代码实践

**实践目标**：用直方图直观感受两种模式的分布差异。

**操作步骤**（示例代码）：

```python
import torch, matplotlib.pyplot as plt
u = torch.rand(100000, 20)*2 - 1
n = torch.randn(100000, 20)
fig, axes = plt.subplots(1, 2, figsize=(10, 3))
axes[0].hist(u.flatten().numpy(), bins=50); axes[0].set_title('uniform')
axes[1].hist(n.flatten().numpy(), bins=50); axes[1].set_title('normal')
plt.show()
```

**需要观察的现象**：左图是 \([-1,1]\) 上的平顶分布；右图是钟形，横轴范围约 \([-4, 4]\)。

**预期结果**：`u` 的 min≈-1、max≈1；`n` 的 std≈1、mean≈0。可再用 `print(u.min(), u.max(), n.mean(), n.std())` 数值验证。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `torch.rand(batch_size, z_size)*2 - 1` 里的 `*2 - 1` 去掉，z 的分布变成什么？会有问题吗？

**答案**：变成 \([0,1)\) 均匀分布。训练本身仍能进行（先验是任意的），但 \([0,1)\) 均匀分布的 20 维样本全挤在正象限的一小块区域，与 D 见过的 \([-1,1]\) 图像尺度不匹配程度更大；更重要的是它和代码注释里声明的设计意图（uniform 指对称的 \([-1,1]\)）不符，复现实验时容易踩坑。

**练习 2**：训练完成后，固定一个 z 不动、微调其中一个分量，生成图会怎么变？

**答案**：通常会引起生成图像某个连续方向上的平滑变化（笔画粗细、倾斜度等），这说明 G 学到的是从潜在空间到图像空间的（近似）连续映射。这也是「潜在空间插值」实验的原理——本仓库代码未包含该实验，感兴趣可自行尝试（示例代码）。

### 4.3 对抗训练：BCELoss 与 d_train / g_train 交替循环

#### 4.3.1 概念说明

GAN 的原始价值函数是一个极小极大问题：

\[
\min_G \max_D V(D, G) = \mathbb{E}_{x \sim p_{\text{data}}}[\log D(x)] + \mathbb{E}_{z \sim p_z}[\log(1 - D(G(z)))]
\]

- D 想最大化 \( V \)：真图概率 \( D(x) \) 越接近 1、假图概率 \( D(G(z)) \) 越接近 0 越好。
- G 想最小化 \( V \)：让 \( D(G(z)) \) 越接近 1 越好（骗过 D）。

代码里两边都通过**二元交叉熵** `nn.BCELoss` 转化为「最小化」问题。回顾 BCE 定义（\(p\) 为预测概率，\(t\) 为目标标签）：

\[
\text{BCE}(p, t) = -[\,t \log p + (1-t)\log(1-p)\,]
\]

于是两套标签规则构成对抗：

| 训练谁 | 喂给 D 的数据 | BCE 目标标签 | 直觉含义 |
|---|---|---|---|
| **D**（`d_train`） | 真图 x | 1 | D 学会「认真」 |
| | G 生成的假图（detach 前正常反传） | 0 | D 学会「识假」 |
| **G**（`g_train`） | G 生成的假图 | **1** | G 逼 D 把假图判成真 |

G 的损失把假图标成 1（「真」），这叫**非饱和技巧**：直接最小化 \(\log(1-D(G(z)))\) 在训练初期（D 轻松识假、概率接近 0）梯度太小；改为最大化 \(\log D(G(z))\) 在 D 犯错时梯度更大，学得动。

#### 4.3.2 核心流程

每个 batch 的训练节奏是「先 D 后 G」：

```text
for each batch x:
    # ---- d_train(x)：只更新 D ----
    D.zero_grad()
    d_loss_real = BCE(D(x), 1)                     # 真图项
    z = create_noise(...)；d_loss_fake = BCE(D(G(z)), 0)   # 假图项
    d_loss = d_loss_real + d_loss_fake
    d_loss.backward(); d_optimizer.step()          # 只动 D 的参数

    # ---- g_train(x)：只更新 G ----
    G.zero_grad()
    g_loss = BCE(D(G(z')), 1)                      # 假图标 1：骗 D
    g_loss.backward(); g_optimizer.step()          # 只动 G 的参数
```

关键机制（结合 u12-l3 的梯度累加语义）：

1. **梯度穿 D 而过、D 参数不动**：`g_train` 里 `g_loss.backward()` 会算出 D 参数的梯度（G 的梯度必须沿 \( G(z) \to D \) 的计算图回传），但 `g_optimizer` 只持有 `gen_model.parameters()`，`step()` 只更新 G。D 上被「污染」的梯度会在下一轮 `d_train` 开头的 `disc_model.zero_grad()` 被清掉。
2. **两组损失不可比，画图时除以 2**：`d_loss` 是两项 BCE 之和，代码画曲线时专门做了 `half_d_losses`（L432）把 D 损失砍半，与 G 损失放到同一视觉尺度。

#### 4.3.3 源码精读

**损失函数的第一次亮相（教学演示段）**：

[ch17/ch17_part1.py:L282-L295](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part1.py#L282-L295) —— 先用未训练的模型手工演示两条损失：G 的损失以 `d_proba_fake` 对全 1 标签计算；D 的损失分 real/fake 两项分别对 1 和 0 标签计算。

```python
loss_fn = nn.BCELoss()
g_labels_real = torch.ones_like(d_proba_fake)
g_loss = loss_fn(d_proba_fake, g_labels_real)     # 假图标 1 → G 的目标
d_labels_real = torch.ones_like(d_proba_real)
d_labels_fake = torch.zeros_like(d_proba_fake)
d_loss_real = loss_fn(d_proba_real, d_labels_real)  # 真图标 1
d_loss_fake = loss_fn(d_proba_fake, d_labels_fake)  # 假图标 0
```

**正式训练的装配**：

[ch17/ch17_part1.py:L302-L326](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part1.py#L302-L326) —— `batch_size=64`；`DataLoader(..., shuffle=True, drop_last=True)`（MNIST 按类别有序存放，必须洗牌，且丢掉尾批保证形状固定）；重建两个模型并 `.to(device)`；`BCELoss` + 两个独立的 `Adam` 优化器（未指定学习率，用 PyTorch 默认 0.001）。

**判别器的单批训练**：

[ch17/ch17_part1.py:L332-L356](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part1.py#L332-L356) —— `d_train`：真图与假图各算一项 BCE，相加后一次 `backward` + `d_optimizer.step()`；返回损失数值和两组 **detach** 后的概率（供监控，不保留计算图）。

```python
def d_train(x):
    disc_model.zero_grad()
    batch_size = x.size(0)
    x = x.view(batch_size, -1).to(device)
    d_labels_real = torch.ones(batch_size, 1, device=device)
    d_proba_real = disc_model(x)
    d_loss_real = loss_fn(d_proba_real, d_labels_real)
    input_z = create_noise(batch_size, z_size, mode_z).to(device)
    g_output = gen_model(input_z)
    d_proba_fake = disc_model(g_output)
    d_labels_fake = torch.zeros(batch_size, 1, device=device)
    d_loss_fake = loss_fn(d_proba_fake, d_labels_fake)
    d_loss = d_loss_real + d_loss_fake
    d_loss.backward()
    d_optimizer.step()
    return d_loss.data.item(), d_proba_real.detach(), d_proba_fake.detach()
```

两个值得咀嚼的细节：真图批与假图批共用同一个 `batch_size`（各 64 张，D 一次更新见到 128 个样本）；`g_output` 没有 detach——D 训练时不需要也不应该阻断 G 的计算图，反正 `d_optimizer.step()` 碰不到 G 的参数。

**生成器的单批训练**：

[ch17/ch17_part1.py:L362-L377](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part1.py#L362-L377) —— `g_train`：重新采一批 z（与 `d_train` 用的不是同一批），假图标 1，反传后只 step G 的优化器。

```python
def g_train(x):
    gen_model.zero_grad()
    batch_size = x.size(0)
    input_z = create_noise(batch_size, z_size, mode_z).to(device)
    g_labels_real = torch.ones(batch_size, 1, device=device)  # 关键：目标=真
    g_output = gen_model(input_z)
    d_proba_fake = disc_model(g_output)
    g_loss = loss_fn(d_proba_fake, g_labels_real)
    g_loss.backward()
    g_optimizer.step()
    return g_loss.data.item()
```

**主循环与过程监控**：

[ch17/ch17_part1.py:L382-L418](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part1.py#L382-L418) —— 训练前先备好 `fixed_z`（固定噪声，用于每轮生成可对比的样本）与 `create_samples`（把 784 维输出 reshape 回 28×28，并做 `(images+1)/2.0` 把 \([-1,1]\) 映射回 \([0,1]\) 以便显示）；主循环 `num_epochs=100`，每个 batch 先 `d_train` 后 `g_train`，逐轮记录平均损失、D 对真/假图的平均输出，并把这轮 `fixed_z` 生成的样本存进 `epoch_samples`。

```python
for epoch in range(1, num_epochs+1):
    d_losses, g_losses = [], []
    ...
    for i, (x, _) in enumerate(mnist_dl):
        d_loss, d_proba_real, d_proba_fake = d_train(x)
        d_losses.append(d_loss)
        g_losses.append(g_train(x))
        d_vals_real.append(d_proba_real.mean().cpu())
        d_vals_fake.append(d_proba_fake.mean().cpu())
    print(f'Epoch {epoch:03d} | Avg Losses >> G/D ...')
    epoch_samples.append(create_samples(gen_model, fixed_z).detach().cpu().numpy())
```

**结果可视化**：

[ch17/ch17_part1.py:L426-L447](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part1.py#L426-L447) —— 左图画 G/D 损失曲线（D 损失去半成 `half_d_losses`）；右图画 D 对真图、假图的平均输出 \( D(\mathbf{x}) \) 与 \( D(G(\mathbf{z})) \)。健康训练中两条输出曲线围绕 0.5 上下拉锯；若 D(fake) 长期贴 0，说明 G 被压制。

[ch17/ch17_part1.py:L452-L471](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part1.py#L452-L471) —— 从 `epoch_samples` 里挑 `[1, 2, 4, 10, 50, 100]` 六个轮次、每轮 5 张排成 6×5 网格，直观呈现「噪声 → 模糊数字影子 → 可辨认数字」的进化过程。

#### 4.3.4 代码实践

**实践目标**：不追求训练完 100 轮，先用 2~3 个 epoch 跑通整个循环、看懂日志格式。

**操作步骤**：复制 `ch17_part1.py` 的训练段到自己的脚本（或直接在 notebook 里改单元格），把 `num_epochs = 100` 改为 `num_epochs = 2`（这是你自己实验脚本的改动），其余不动，运行。CPU 上一个 epoch 约几分钟，属可接受范围；跑满 100 轮建议用书中推荐的 Google Colab GPU（README 的章节标题即「Training GAN models on Google Colab」）。

**需要观察的现象**：每轮打印一行的日志，格式为：

```text
Epoch 001 | Avg Losses >> G/D 3.1xxx/1.2xxx [D-Real: 0.5xxx D-Fake: 0.4xxx]
```

**预期结果**：初期 D-Real、D-Fake 都在 0.5 附近（未训练的 D 输出接近随机）；随轮数增加 D-Real 应抬升、D-Fake 应被压低后又随 G 进步而回升。若 2 轮后 loss 出现 NaN，多半是学习率或数据范围问题——请先对照 4.1.3 检查 Normalize 是否生效。具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`d_train` 里若忘了 `disc_model.zero_grad()`，会发生什么？

**答案**：D 的梯度跨 batch 累加（u12-l3 讲过的累加语义），等效于用一个不断膨胀的梯度更新参数，训练迅速发散。更隐蔽的是：`g_train` 中 `g_loss.backward()` 也会给 D 的参数写入梯度，若 `d_train` 开头不清零，这些「垃圾梯度」会混进 D 的下一次更新。

**练习 2**：为什么 `g_train` 里 G 的目标标签是 1（真）而不是 0（假）？明明生成的是假图。

**答案**：G 的目标是**骗过** D，即最大化 \( D(G(z)) \)。在 BCE 框架下，「希望预测概率接近 1」就等价于把目标标签设为 1。若设为 0，G 反而会学着生成更容易被认出的假图——目标完全反了。这正是「对抗」在代码里的落点：同一份数据、同一套损失函数，只靠翻转标签就实现了对立的训练目标。

**练习 3**：主循环里为什么每个 batch 固定「先 D 后 G」，反过来行吗？

**答案**：行，顺序不是原理性约束，只是惯例。但顺序会影响两者的相对信息新鲜度：本代码中 G 更新时用的 D 是「刚被这一批数据强化过的 D」，对抗信号略强。工程上更重要的是保证两者更新频率相当（1:1），避免某一方长期落后。

### 4.4 MMD 距离与分数评估

#### 4.4.1 概念说明

GAN 训练完了，怎么客观回答「生成的数字像不像真的」？肉眼看图主观，损失值又无法跨模型比较（G/D 损失反映博弈状态而非图像质量）。代码给了一个分布层面的定量工具——**最大均值差异（Maximum Mean Discrepancy, MMD）**：

把真图集合与假图集合看作两个分布 \( P \)、\( Q \) 的样本，MMD 回答「这两个分布差多远」：

\[
\text{MMD}^2(P, Q) = \mathbb{E}_{x, x' \sim P}[k(x, x')] + \mathbb{E}_{y, y' \sim Q}[k(y, y')] - 2\,\mathbb{E}_{x \sim P,\, y \sim Q}[k(x, y)]
\]

其中 \( k \) 是核函数，代码用高斯核：

\[
k(x, y) = \exp\!\left(-\frac{\lVert x - y\rVert^2}{2\sigma^2 \cdot \text{scale}}\right), \qquad \text{scale} = \overline{M_{xx}}
\]

直觉：\( k(x,y) \) 度量两点「多近」。若 \( P \) 与 \( Q \) 是同一个分布，则同分布内点的平均接近程度（前两项）应恰好等于跨分布点的平均接近程度（第三项），MMD² = 0；分布差得越远，MMD 越大。**MMD 越小，生成分布越贴近真实分布。**

带宽参数用 `scale = Mxx.mean()`（真图两两距离的均值）自适应归一化，避免 784 维距离的绝对量纲影响核的分辨力。

#### 4.4.2 核心流程

```text
compute_score(fake, real):
  Mxx = distance(real, real)   # 真图-真图 两两平方距离矩阵
  Mxy = distance(real, fake)   # 真-假
  Myy = distance(fake, fake)   # 假-假
  mmd(Mxx, Mxy, Myy, sigma):
    三个矩阵都做 exp(-d² / (2σ²·scale))   # 距离 → 核相似度
    a = mean(Mxx_kernel) + mean(Myy_kernel) - 2·mean(Mxy_kernel)
    return sqrt(max(a, 0))                 # 数值保护后开根

distance(X, Y):  # 两两平方欧氏距离，避免循环的展开技巧
  ‖xi - yj‖² = ‖xi‖² + ‖yj‖² - 2·xiᵀ·yj
  # 即 [X2 展开成列] + [Y2 展开成行] - 2·X·Yᵀ
```

`distance` 里 \(\lVert x_i \rVert^2 + \lVert y_j \rVert^2 - 2x_i^\top y_j\) 的恒等式把 \( n_X \times n_Y \) 个距离压成一次矩阵乘法——与 u5-l1 PCA 里「协方差矩阵的特征分解」一样，都是把双重循环降为线性代数操作的经典手法。

#### 4.4.3 源码精读

[ch17/ch17_part1.py:L481-L497](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part1.py#L481-L497) —— `distance` 计算两两平方欧氏距离矩阵：`(X*X).sum(1)` 得每行范数平方，`expand` 广播成矩阵形状后相加减去 `2·X·Yᵀ`；`sqrt=True` 时可返回真实欧氏距离（`compute_score` 传的是 `False`，保持平方距离即可，因为核里本来就要平方）。

```python
M = torch.zeros(nX, nY)
M.copy_(X2.expand(nX,nY) + Y2.expand(nY,nX).transpose(0,1)
        - 2*torch.mm(X, Y.transpose(0,1)))
```

⚠️ 两个工程事实：其一，`X.view(nX,-1).cuda()` 是**无条件** `.cuda()`——这段评估代码只能在 GPU 环境运行，CPU 机器上会报错（综合实践给出应对办法）；其二，`resize_` 是 PyTorch 1.x 早期风格 API，在较新版本会有弃用警告，原理不受影响。

[ch17/ch17_part1.py:L502-L510](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part1.py#L502-L510) —— `mmd` 先用 `scale = Mxx.mean()` 定带宽，三个距离矩阵过同一个高斯核，按公式取「内项相加、跨项减二倍」，`max(a, 0)` 防止浮点误差造出负数后开根出 NaN。

```python
scale = Mxx.mean()
Mxx = torch.exp(-Mxx/(scale*2*sigma*sigma))
...
a = Mxx.mean()+Myy.mean()-2*Mxy.mean()
mmd = math.sqrt(max(a, 0))
```

[ch17/ch17_part1.py:L515-L538](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part1.py#L515-L538) —— `compute_score` 组装三组距离矩阵并打印 MMD；评估规模为 `whole_dl`：batch_size=10000 的 DataLoader 取一批真图，假图用训练循环存下的 `epoch_samples[-1]`（最后一轮、fixed_z 生成的 64 张，`torch.from_numpy` 转回张量）。

一个细心的读者会注意到的**量纲不一致**：`create_samples` 输出做过 `(images+1)/2.0`，值域 \([0,1]\)（供显示）；而 `real_image` 经过 `Normalize(0.5, 0.5)`，值域 \([-1,1]\)。仓库原样代码直接拿两者算 MMD，距离中含有一个整体平移的偏差。更严谨的做法是先把两者对齐到同一值域再算（见下面实践与练习 2）——这属于对仓库代码的批判性阅读，不影响 MMD 的教学意义。

#### 4.4.4 代码实践

**实践目标**：给 MMD 建立数量级直觉——「纯噪声图 vs 真图」与「训练后的生成图 vs 真图」的 MMD 应有明显差距。

**操作步骤**：GPU 环境下，在训练段之后追加（示例代码）：

```python
noise_images = torch.rand(64, 784)                # 纯噪声基线：64 张"图"
compute_score(noise_images, real_image)           # 噪声 vs 真：应明显偏大
compute_score(torch.from_numpy(epoch_samples[0]),  # 第 1 轮的生成样本
              real_image)
compute_score(torch.from_numpy(epoch_samples[-1]), # 最后一轮的生成样本（书中做法）
              real_image)
```

CPU 环境无法直接运行仓库版 `distance`（含 `.cuda()`），可把 `distance`/`mmd`/`compute_score` 三个函数复制进自己的脚本，将两处 `.cuda()` 改为 `.to(device)`（示例代码级修改，仓库文件不动）。

**需要观察的现象**：三个 MMD 值的相对大小。

**预期结果**：纯噪声 vs 真图最大；第 1 轮生成图次之；最后一轮最小。若「最后一轮」反而不小于第 1 轮，说明训练出了问题（如 G 被 D 压死），回头看 4.3.3 的 D-Real/D-Fake 曲线即可定位。具体数值**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：MMD 为什么比「逐像素平均差」更适合评估生成质量？

**答案**：逐像素平均差只比较两图的均值，两张完全不同但平均亮度相同的图会得 0 分。MMD 通过核函数比较**两两样本关系**的整体结构（分布形状），只要 \( P \neq Q \)，总能找到一个核尺度让 MMD > 0；它衡量的是分布层面而非样本均值的差异。

**练习 2**：4.4.3 指出的值域不一致（假图 \([0,1]\) vs 真图 \([-1,1]\)）会给 MMD 带来什么方向的偏差？怎么修？

**答案**：会系统性抬高 \( M_{xy} \)（跨分布距离多了恒定位移），使 MMD 偏大——即低估生成质量；且不同轮次的样本若值域处理不一致，比较也不公平。修法：比较前统一值域，例如假图用 `epoch_samples[-1]*2-1` 映射回 \([-1,1]\)，或把 `real_image` 映射到 \([0,1]\)（`(real_image+1)/2`），两者等价。

**练习 3**：`mmd` 里的 `scale = Mxx.mean()` 起什么作用？若固定 `scale=1` 会怎样？

**答案**：它是自适应带宽——用真图间的平均平方距离把 784 维距离归一化，使核的衰减尺度与数据本身的尺度匹配。固定 `scale=1` 时，784 维空间中平方距离动辄上万，`exp(-d²/2σ²)` 对几乎所有点对都归零，MMD 退化为 0 附近的噪声，失去分辨力。

## 5. 综合实践

**任务：削弱判别器，用 MMD 量化「对抗平衡」被打破的后果。**

背景先对齐事实：仓库代码中判别器默认隐藏单元是 `disc_hidden_size = 100`（[ch17/ch17_part1.py:L196-L197](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part1.py#L196-L197)），生成器为 100——不是 256。实践以真实默认值为基线。

**步骤**：

1. **基线运行**：按原配置（G=100，D=100）完整训练。时间紧张可把 `num_epochs` 降到 20~50（自己脚本里的改动），或用 Google Colab GPU 跑满 100 轮。
2. **削弱判别器**：在训练装配处把 `make_discriminator_network(..., num_hidden_units=disc_hidden_size)` 的实参改为 32（或直接改自己脚本里的 `disc_hidden_size = 32`），生成器保持 100 不动，重新训练相同轮数。
3. **（可选）加强判别器**：再把 D 的隐藏单元改成 256，做第三组对照。
4. **三组评估**：对每组用 `compute_score`（或其 CPU 改写版）算「最后一轮生成样本 vs 真图」的 MMD，并把 `epoch_samples` 里最后一轮的 5 张图并排画出来肉眼对照。
5. **记录**：每组的三条曲线（G loss、D-Real、D-Fake）形态 + MMD 数值 + 样本图。

**需要观察的现象与解释要点**：

- **弱 D（32 单元）**：D 的容量不足，早期 D-Fake 输出可能就压不下去；G「轻易骗过」一个太弱的鉴定师，反而缺少梯度压力去精细化数字结构——生成图常见的问题不是崩坏而是「含混、笔画粘连」。MMD 预期比基线差（待本地验证）。
- **强 D（256 单元）**：D 几乎永远赢，D-Fake 长期贴近 0。回忆 4.3.1 的非饱和技巧：G 的损失梯度来自 \( D(G(z)) \)，D 太强时这个概率趋于 0，`log` 端梯度消失，G 学不动——这就是教科书里「D 过强导致梯度消失」的现场。
- **结论指向**：对抗训练的本质是让双方在动态平衡中互相抬升；任何一方长期碾压另一方，博弈就退化成单方面学习，生成质量（MMD）随之受损。这也是 ch17_part2 引入 WGAN-GP 的动机之一——它从损失函数层面缓解这类失衡，详见下一讲。

## 6. 本讲小结

- GAN = 生成器（噪声→图像）与判别器（图像→真/假概率）的双网络博弈；仓库实现是 784 维全连接版 MNIST GAN，G 输出 `Tanh`（\([-1,1]\)）与数据管线的 `Normalize(0.5, 0.5)` 严格对齐。
- `create_noise` 提供两种潜在向量先验：`uniform`（\([-1,1]\) 平顶、有界）与 `normal`（标准正态、无界），一个全局变量 `mode_z` 切换。
- 对抗靠「同一份 BCELoss + 翻转标签」实现：D 训练时真图=1、假图=0（两项相加）；G 训练时假图=1（非饱和技巧）。`d_train`/`g_train` 各自 `zero_grad→backward→step`，靠「优化器只持有一方参数」保证只更新一方，G 的梯度穿过 D 反传但污染不了 D。
- 训练监控看三样：G/D 损失曲线（D 损失画图时除以 2 对齐尺度）、D 对真/假图的平均输出（围绕 0.5 拉锯为健康）、`fixed_z` 固定噪声的逐轮样本进化。
- MMD 用高斯核比较真/假两个分布的两两结构差异，MMD 越小生成分布越贴近真实分布；带宽由 `Mxx.mean()` 自适应。注意仓库版 `distance` 无条件 `.cuda()`（需 GPU），且假图 \([0,1]\) 与真图 \([-1,1]\) 存在值域不一致的瑕疵。
- 对抗训练依赖 G/D 强度平衡：D 太弱则 G 缺少精进压力，D 太强则 G 梯度消失——综合实践用「削弱/加强 D + MMD 量化」验证了这一点。

## 7. 下一步学习建议

下一讲 **u17-l2：DCGAN 与 WGAN-GP 进阶**将精读 [ch17/ch17_part2.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch17/ch17_part2.py)：用 `ConvTranspose2d`（转置卷积）与带步幅卷积把本讲的全连接 GAN 升级为 DCGAN，并用 WGAN-GP 的梯度惩罚替代 BCE 对抗损失、以 EM 距离评估生成质量——正好回应本讲结尾「对抗失衡」的问题。建议先自行浏览 ch17_part2.py 开头的 `make_generator_complex` / `make_discriminator_complex`，对比它们与本讲两个工厂函数的对应关系。
