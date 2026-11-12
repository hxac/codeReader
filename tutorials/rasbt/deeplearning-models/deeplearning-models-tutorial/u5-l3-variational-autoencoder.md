# 变分自编码器 VAE

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 **VAE 与普通自编码器的本质区别**：u5-l1 的 AE 只会「压缩 + 重建」，随机向量喂给 decoder 出来的是噪声；VAE 给隐空间加上概率约束，使它成为**生成模型**——能从纯噪声里「无中生有」地解码出逼真图像。
2. 理解**重参数化技巧（reparameterization trick）**：为什么 `z ~ N(μ, σ²)` 的直接采样不可导，而 `z = μ + ε·σ`（ε 与参数无关）就能让梯度流回 encoder，这是本讲最核心的一行代码。
3. 掌握 **KL 散度 + 逐像素 BCE 的组合损失**：闭式 KL 公式 \(0.5\sum(\mu^2 + \sigma^2 - \ln\sigma^2 - 1)\) 的来历与代码实现，理解两项「一个想记住样本、一个想忘掉样本」的博弈，以及 β 权重如何调节这个博弈。
4. 会**从先验采样生成新图像**：`torch.randn` + `model.decoder(...)` 两行代码背后，KL 项在训练期间对隐空间做出的「形状承诺」如何被兑现。
5. 读懂 **卷积版 VAE**（`ae-conv-var.ipynb`）：VAE 数学完全不变，只把 encoder/decoder 骨架换成步长卷积 + 转置卷积，承接 u5-l2 的上采样知识。
6. 完成综合实践：β-VAE 小实验（β ∈ {0.5, 1, 4}）+ 隐空间线性插值，亲手观察「重建保真 vs 隐空间连续」的权衡。

本讲是 u5-l1（自编码器基础）的直接续篇。u5-l1 结尾留下的遗憾——隐空间没有约束、decoder 只对「训练数据编码出的点」负责——正是 VAE 要解决的问题。

## 2. 前置知识

本讲的新概念主要是概率层面的（分布、KL 散度、ELBO），PyTorch 组件几乎全是旧知识：

- **自编码器三部件**（u5-l1）：encoder 压缩、瓶颈装信息、decoder 还原；训练目标是输入本身（自监督）；ToTensor 像素在 [0,1] 值域，输出层配 sigmoid，重建损失用**吃概率的** `F.binary_cross_entropy`。
- **五步训练循环**（u2-l2）：forward → loss → `zero_grad` → `backward` → `step`。注意：autoencoder 系列的 notebook **没有**使用 helper_train 的 `train_classifier_simple`（u4-l4），因为 VAE 的损失需要 `z_mean` / `z_log_var` 这两个额外返回值，只能手写循环——这是「共享工具何时适用」的活例子。
- **高斯分布**：一维正态 \(N(\mu, \sigma^2)\) 由均值 μ 和方差 σ² 决定；标准正态 \(N(0, 1)\) 是 μ=0、σ=1 的特例。`torch.randn` 采样的就是标准正态。
- **KL 散度（Kullback–Leibler divergence）**：衡量两个分布的差异

  \[ D_{KL}(q \,\|\, p) = \mathbb{E}_q\!\left[\log \frac{q(z)}{p(z)}\right] \ge 0 \]

  等号当且仅当两个分布完全相同。本讲里 \(q\) 是 encoder 输出的分布，\(p\) 是标准正态先验。
- **转置卷积**（u5-l2）：`ConvTranspose2d` 的输出尺寸 \(o = (w-1)\cdot s - 2p + k\)，用于卷积版 decoder 的上采样。
- **`torch.manual_seed` 与随机性**（u1-l3）：本讲两份 notebook 都在建模前设了 `torch.manual_seed(random_seed)`，但 `reparameterize` 里每个 batch 都要 `torch.randn` 一次，随机数消耗贯穿全程。
- **「注释说」与「代码做」要分开核对**（u3-l5、u4-l2、u5-l2 反复出现的教训）：本讲会撞上一处说明文字笔误（768 vs 784）和一处设备写法小坑（`cuda:1`）。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `pytorch_ipynb/autoencoder/ae-var.ipynb` | **全连接版 VAE**：784→500→(μ, log σ²)→15→500→784，本讲 4.1、4.2 节主线。 |
| `pytorch_ipynb/autoencoder/ae-conv-var.ipynb` | **卷积版 VAE**：3 个步长卷积压到 64×2×2，转置卷积还原，4.3 节主线。 |

两份 notebook 是一组**受控对比**：超参完全相同（`random_seed=0`、`learning_rate=0.001`、`num_epochs=50`、`batch_size=128`、`num_latent=15`），`reparameterize`、损失计算、生成代码逐字相同，唯一差异是 encoder/decoder 骨架。先给总览表：

| | ae-var（全连接版） | ae-conv-var（卷积版） |
| --- | --- | --- |
| encoder | `Linear(784,500)` + LeakyReLU | 3 个 stride-2 卷积：28×28×1 → 64×2×2 |
| 分布双头 | `Linear(500,15)` ×2（μ 与 log σ²） | `Linear(256,15)` ×2 |
| decoder | `Linear(15,500)` → `Linear(500,784)` + sigmoid | `Linear(15,256)` + 3 个转置卷积 + sigmoid |
| 训练时输入 | 需 `view(-1, 28*28)` 展平 | 直接用 NCHW 张量 |
| 首个 batch 损失（作者日志） | 70481.24 | 70508.81 |
| 第 50 epoch 末批损失（作者日志） | 13272.68 | 13924.47 |
| 作者环境 | CPython 3.7.1 + torch 1.0.0 + GPU | CPython 3.6.8 + torch 1.0.0 + GPU |

> **行号说明**：本讲所有永久链接指向 `.ipynb` 原始 JSON 文件的行号（GitHub 上打开即命中对应行）。
>
> 两处读源码小坑：
> ① 两份 notebook 的说明文字都写着「compresses **768-pixel** MNIST images」（[ae-var.ipynb:L53](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-var.ipynb#L53)），但 28×28 = **784**——这是 u5-l2 已撞见过的作者笔误，四份卷积 AE notebook 里同样存在，代码本身没错（`num_features = 784`）。
> ② 卷积版的 device 写的是 `torch.device("cuda:1" ...)`（[ae-conv-var.ipynb:L103](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-conv-var.ipynb#L103)）——作者机器有多张卡。**单卡机器**上 `torch.cuda.is_available()` 返回 True，但 `.to("cuda:1")` 会报 `invalid device ordinal`，跑之前要改回 `cuda:0`。

## 4. 核心概念与源码讲解

### 4.1 重参数化技巧：让「随机采样」可以被反向传播

#### 4.1.1 概念说明

**从 AE 的遗憾讲起。** u5-l1 的普通自编码器把每张图映射成**一个确定性的点**。训练数据只覆盖隐空间中极稀疏的一堆点，点与点之间的「空隙」decoder 从没见过——往里面喂一个 `torch.randn` 采样，解码出来的是一团噪声。它是个压缩器，不是生成器。

**VAE 的思路：把「点」换成「分布」。** 让 encoder 对每张输入 x 不再输出确定向量，而是输出一个高斯分布的参数——均值 \(\mu(x)\) 和方差 \(\sigma^2(x)\)，即 \(q(z|x) = N(\mu(x), \mathrm{diag}(\sigma^2(x)))\)；编码即从该分布采样 \(z \sim q(z|x)\)，再交给 decoder 重建。同时（见 4.2 的 KL 项）把所有这些分布往标准正态 \(N(0, I)\) 上拉。这样训练结束后，隐空间整体形状接近标准正态——**从 \(N(0,I)\) 采样再解码，decoder 是「见过世面」的**，于是能生成合理图像。

**障碍：采样不可导。** 训练需要梯度从损失流回 encoder 参数。但「从 \(N(\mu, \sigma^2)\) 采样」这个操作——比如 `torch.normal(mean, std)`——在计算图里是一个**断点**：输出 z 与 μ、σ 之间没有可求导的函数关系，`backward()` 传不回去。

**重参数化：把随机性挪出参数路径。** 恒等改写采样过程：

\[ z = \mu + \epsilon \cdot \sigma, \quad \epsilon \sim N(0, I) \]

两边是同一个分布（「均值 + 标准差的线性扰动」仍是 \(N(\mu, \sigma^2)\)），但计算图完全不同了：

- ε 由 `torch.randn` 生成，**与任何参数无关**，不需要梯度；
- z 到 μ、σ 之间只剩**加法和乘法**——梯度畅通无阻：

\[ \frac{\partial z}{\partial \mu} = 1, \qquad \frac{\partial z}{\partial \sigma} = \epsilon \]

一句话直觉：**随机性被装进 ε 这个「外部常数」里，参数只出现在确定性的仿射变换中**。这是「pathwise gradient / 重参数化技巧」，也是 Kingma & Welling（2013）让 VAE 能用反向传播训练的关键一步。

**为什么网络输出的是 log σ² 而不是 σ²？** 两个原因：一是方差必须为正，直接用 Linear 层输出可能为负，而 \(\sigma^2 = \exp(\log\sigma^2)\) **恒为正**；二是训练损失的 KL 项里要取对数（见 4.2），存 log σ² 可以避免重复取对数的数值麻烦。仓库源码注释（[ae-var.ipynb:L174-L180](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-var.ipynb#L174-L180)）讲的就是这件事——原论文用 z_mean + z_var，方差可能为负会在 log 处出问题，所以这里改用 z_mean + z_log_var。

#### 4.1.2 核心流程

全连接版 VAE 一次前向的形状链（batch=128 省略）：

| 段 | 操作 | 形状 / 产出 |
| --- | --- | --- |
| 编码 | `hidden_1`：Linear(784→500) + LeakyReLU | 500 |
| 分布参数 | `z_mean`：Linear(500→15) | μ ∈ R¹⁵ |
| 分布参数 | `z_log_var`：Linear(500→15) | log σ² ∈ R¹⁵ |
| 采样 | `reparameterize`：ε= randn(15)，z = μ + ε·exp(logσ²/2) | z ∈ R¹⁵ |
| 解码 | `linear_3`：Linear(15→500) + LeakyReLU | 500 |
| 解码 | `linear_4`：Linear(500→784) + sigmoid | 784（重建图） |

注意分布双头是**并列**的两条 Linear，共享 500 维隐层输入，彼此独立互不相连。伪代码：

```text
def encoder(x):
    h = leaky_relu(hidden_1(x))
    mu = z_mean(h)          # 头 1：分布中心
    logvar = z_log_var(h)   # 头 2：分布Spread
    z = mu + randn * exp(logvar / 2)   # 重参数化采样
    return mu, logvar, z

def decoder(z):
    h = leaky_relu(linear_3(z))
    return sigmoid(linear_4(h))
```

采样用 `exp(z_log_var / 2)` 而非 `exp(z_log_var)`，因为 \(\sqrt{\sigma^2} = \sigma = \exp(\tfrac{1}{2}\log\sigma^2)\)——对数方差除以 2 才是标准差。

#### 4.1.3 源码精读

模型类定义在 [ae-var.ipynb:L166-L216](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-var.ipynb#L166-L216)，这段代码做了什么：定义 `VariationalAutoencoder` 类——encoder 一个共享隐层挂两个并列 Linear 头（μ 与 log σ²），decoder 对称地把 15 维采样还原为 784 维重建。

关键代码一：**双头 `__init__`**（[ae-var.ipynb:L171-L186](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-var.ipynb#L171-L186)），这段代码做了什么：`hidden_1` 把 784 维压到 500 维；接着**两个形状完全相同的 `Linear(500, 15)` 分别产出 μ 与 log σ²**——VAE 与普通 AE 在模型定义上的唯一结构差异就是这个「一个隐层分出两个头」。

关键代码二：**重参数化函数**（[ae-var.ipynb:L188-L194](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-var.ipynb#L188-L194)），这段代码做了什么：本讲最核心的四行——采样 ε 并通过 `z = z_mu + eps * torch.exp(z_log_var/2.)` 把随机采样改写成对 μ、σ 可导的仿射变换：

```python
def reparameterize(self, z_mu, z_log_var):
    eps = torch.randn(z_mu.size(0), z_mu.size(1)).to(device)
    z = z_mu + eps * torch.exp(z_log_var/2.)
    return z
```

两个细节：① ε 的形状跟随 `(batch, 15)`，每个样本、每个隐维度独立采一份噪声；② `torch.randn` 不带梯度（叶子常数），乘法路径上的梯度全部归于 `z_mu` 与 `z_log_var` 两条 Linear 的权重。

关键代码三：**encoder / decoder / forward**（[ae-var.ipynb:L196-L216](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-var.ipynb#L196-L216)），这段代码做了什么：`encoder` 里完成「隐层 → 双头 → 采样」全过程并返回三元组 `(z_mean, z_log_var, encoded)`；`decoder` 是普通 AE 的结构（Linear + LeakyReLU + Linear + sigmoid）；`forward` 返回**四元组** `(z_mean, z_log_var, encoded, decoded)`——把分布参数一并交出去，是因为损失函数里的 KL 项需要它们，这个设计让损失计算留在模型外面（见 4.2.3）。

另注意：`reparameterize` 被**内嵌在 `encoder()` 里调用**（[ae-var.ipynb:L201](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-var.ipynb#L201)），它**不区分 train/eval 模式**——不存在 Dropout、BatchNorm 那样的开关，任何时候调用 encoder 都会加噪声。想让编码「确定下来」（比如 4.3.4 的插值实验），应该取 `z_mean` 而不是 `encoded`。

#### 4.1.4 代码实践

**实践：亲手验证「重参数化前后分布相同、但只有后者可导」。**

1. **实践目标**：用两个小实验分别证明 ① `z = μ + ε·σ` 与 `torch.normal` 直接采样是同一个分布；② 梯度只能沿重参数化路径流回 μ 的「生产者」权重。
2. **操作步骤**（示例代码，可直接粘进 notebook）：

```python
import torch
torch.manual_seed(0)

mu_prod = torch.nn.Linear(500, 15)          # 模拟 z_mean 头
x = torch.randn(128, 500)                   # 模拟 500 维隐层特征
mu = mu_prod(x)                             # requires_grad=True

# A. 分布等价性：两种写法各自的样本统计量
z1 = mu + torch.randn(128, 15) * 0.5        # 重参数化（σ=0.5）
z2 = torch.normal(mu, torch.full((128, 15), 0.5))
print(z1.mean(), z1.std(), z2.mean(), z2.std())

# B. 梯度路径：损失对 mu 的梯度能否求出
loss = z1.pow(2).mean()
grads = torch.autograd.grad(loss, mu_prod.weight, retain_graph=True)
print(grads[0] is not None, grads[0].shape)

loss2 = z2.pow(2).mean()
try:
    torch.autograd.grad(loss2, mu_prod.weight)
except RuntimeError as e:
    print('直接采样路径报错:', str(e)[:60])
```

3. **需要观察的现象**：A 中两组的均值都接近 μ、标准差都接近 0.5；B 中重参数化路径能返回形状为 `(15, 500)` 的梯度张量，而 `torch.normal` 路径抛出 RuntimeError（该采样对权重无梯度）。
4. **预期结果**：分布统计量一致 + 梯度一通一断，直观演示「重参数化不改变分布、只改变计算图」。具体数值待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：源码里 `torch.exp(z_log_var/2.)` 为什么要除以 2？

**答案**：设网络输出的是对数方差 \(\ell = \log\sigma^2\)。由 \(\log\sigma^2 = 2\log\sigma\)，得 \(\sigma = \exp(\log\sigma^2/2)\)。不除以 2 得到的是方差 σ² 而不是标准差，噪声会被放大 \(\sigma\) 倍。源码注释（L191-L192）写的 `std_dev = exp(log(var)/2)` 就是这个换算。

**练习 2**：如果把 `reparameterize` 整个删掉、直接令 `encoded = z_mean`，训练还能跑，模型变成了什么？

**答案**：变成「带 KL 正则的确定性自编码器」。梯度照常流动、损失照常下降，但每个输入只对应隐空间一个确定点，VAE 引入随机隐变量的意义（用分布覆盖隐空间空隙）丧失；同时 KL 项仍会把 μ 拉向 0，重建与正则的博弈依旧存在，但「采样带来的平滑效应」没了。这是理解「随机性本身是 VAE 一部分」的好对照。

**练习 3**：ε 是 `torch.randn` 采的、没有梯度，为什么 `loss.backward()` 却能更新 `z_mean` 头的权重？

**答案**：反向传播只需要**路径上存在可导运算**。z = μ + ε·σ 中，μ 由 `z_mean(x)` 算出、σ 由 `exp(z_log_var/2)` 算出，z 对两者分别有偏导 1 和 ε/2·σ；ε 自身是常数叶子，不需要梯度。链式法则沿「loss → z → (μ, σ) → 网络权重」传播，与 ε 无关。

### 4.2 KL + 重建损失：VAE 的目标函数

#### 4.2.1 概念说明

**ELBO：VAE 真正在优化的东西。** 我们想最大化数据的对数似然 \(\log p_\theta(x)\)，但它含有对 z 的不可积积分。变分推断给出经典下界（ELBO，evidence lower bound）：

\[ \log p_\theta(x) \;\ge\; \underbrace{\mathbb{E}_{q_\phi(z|x)}\big[\log p_\theta(x|z)\big]}_{\text{重建项}} \;-\; \underbrace{D_{KL}\big(q_\phi(z|x)\,\|\,p(z)\big)}_{\text{KL 正则项}} \]

训练就是最大化 ELBO，等价于最小化「负 ELBO」：

- **重建项**：从 \(q(z|x)\) 采一个 z、交给 decoder，希望重建得像 x。期望无法解析计算，用**单样本蒙特卡洛估计**——`reparameterize` 采的那一个 z 就够了（batch 维再平均一轮）。对 [0,1] 像素、sigmoid 输出，负对数似然对应**逐像素 BCE**（u5-l1 讲过这个搭配）。
- **KL 项**：把 \(q_\phi(z|x)\) 往先验 \(p(z) = N(0, I)\) 上拉。对两个对角高斯，它有**闭式解**，不需要采样：

\[ D_{KL}\big(N(\mu, \mathrm{diag}(\sigma^2))\,\big\|\,N(0, I)\big) = \frac{1}{2}\sum_{j=1}^{J}\Big(\mu_j^2 + \sigma_j^2 - \ln\sigma_j^2 - 1\Big) \]

推导骨架（展开 \(\mathbb{E}_q[\log q - \log p]\)，对每个维度 j 配方高斯的二阶矩与熵）：

\[ \tfrac{1}{2}\big(\mu_j^2 + \sigma_j^2\big) - \tfrac{1}{2}\big(0 + 1\big) - \tfrac{1}{2}\big(\ln\sigma_j^2 - \ln 1\big) \quad\Longleftrightarrow\quad \tfrac{1}{2}\big(\mu_j^2 + \sigma_j^2 - \ln\sigma_j^2 - 1\big) \]

代入 \(\sigma^2 = \exp(\mathrm{z\_log\_var})\)、\(\ln\sigma^2 = \mathrm{z\_log\_var}\)，正好是源码那行公式。当且仅当 μ=0 且 σ²=1 时取最小值 0。

**两项的博弈。** 重建项希望每个样本的编码「信息量足够大、彼此分得开」——极端是记住训练集；KL 项希望所有编码都塌向 \(N(0,I)\)——极端是所有样本共享同一个分布（μ→0, σ→1），decoder 收不到任何区分信息。最终隐空间是两者妥协的产物：**大致铺满标准正态、又保留可辨识的类簇结构**。这个张力叫 **posterior collapse 的对立面问题**，是 VAE 生成「偏模糊」的根源。

**β-VAE：给博弈加一个旋钮。** 把损失写成

\[ \mathcal{L} = \text{BCE} + \beta \cdot D_{KL} \]

β>1 强化正则（隐空间更平滑、重建更模糊，倾向学习更解耦的表示），β<1 弱化正则（重建更清晰、隐空间空隙更多）。源码默认 β=1（两项直接相加）。

**reduction 口径。** 两项都用 `reduction='sum'`（对 batch 和所有维度求和）。这保证**每个样本内部**「KL : 重建 = 1 : 1」的标准 ELBO 权重——若一项 sum、一项 mean，等于偷偷改了 β。总损失绝对值随 batch_size 缩放，对 Adam 优化器影响不大。

#### 4.2.2 核心流程

训练循环仍是五步范式，只是「loss」一步拆成三行：

```text
for epoch, for batch:
    features = images.view(-1, 784)          # 全连接版要展平
    z_mean, z_log_var, encoded, decoded = model(features)
    kl   = (0.5 * (z_mean**2 + exp(z_log_var) - z_log_var - 1)).sum()
    bce  = BCE(decoded, features, reduction='sum')
    cost = kl + bce
    optimizer.zero_grad(); cost.backward(); optimizer.step()
```

损失随训练的变化（作者保存的日志，[ae-var.ipynb:L244-L743](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-var.ipynb#L244-L743) 是完整的 500 行输出）：第 1 个 epoch 首批 cost ≈ 70481，第 50 个 epoch 末批 ≈ 13273。两项都以 sum 计，量级是「128 个样本 × 784 像素」求和的 BCE 主导。

#### 4.2.3 源码精读

训练循环在 [ae-var.ipynb:L748-L778](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-var.ipynb#L748-L778)，这段代码做了什么：标准的 epoch×batch 双层循环，唯一与普通 AE 训练不同的是损失计算——从 `model(features)` 的四元组返回值里取 `z_mean` / `z_log_var` 计算闭式 KL。

关键代码：**三行损失**（[ae-var.ipynb:L758-L762](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-var.ipynb#L758-L762)），这段代码做了什么：第一行按闭式公式逐元素算 KL 再求和，第二行算逐像素 BCE（sum 口径），第三行相加得到负 ELBO：

```python
kl_divergence = (0.5 * (z_mean**2 +
                        torch.exp(z_log_var) - z_log_var - 1)).sum()
pixelwise_bce = F.binary_cross_entropy(decoded, features, reduction='sum')
cost = kl_divergence + pixelwise_bce
```

对照公式逐项读：`z_mean**2` ↔ \(\mu_j^2\)，`torch.exp(z_log_var)` ↔ \(\sigma_j^2\)，`- z_log_var` ↔ \(-\ln\sigma_j^2\)，`- 1` ↔ 常数。`.sum()` 同时对 batch 维和 15 个隐维求和。

其余细节：① 展平发生在循环内（[ae-var.ipynb:L753](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-var.ipynb#L753) 的 `features.view(-1, 28*28)`），u2-l2 讲过这个习惯；② 重建可视化直接复用训练循环最后一个 batch 残留的 `features` / `decoded` 变量画「原图 vs 重建」两行对照图（[ae-var.ipynb:L816-L830](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-var.ipynb#L816-L830)），与 u5-l1、u5-l2 的评估模式一致；③ 保存的日志在 500 行处被截断，作者机器上的计时行没有保留下来（对比：卷积版保留了「Total Training Time: 7.01 min」）。

#### 4.2.4 代码实践

**实践：β-VAE 小实验——一行改动扫三个 β。**

1. **实践目标**：亲手观察 KL 权重 β 对「重建保真 vs 隐空间正则」的权衡。
2. **操作步骤**：
   - 复制 `ae-var.ipynb`，在超参区加一行 `beta = 1.0`，把损失行改为 `cost = beta * kl_divergence + pixelwise_bce`；
   - 为控制变量，可先把 `num_epochs` 降到 10（每个 β 训练更快），其余超参不动；
   - 对 β ∈ {0.5, 1, 4} 各训一次，每次记录：(a) 最后一个 epoch 的 `kl_divergence` 数值（可在循环里加一行打印）；(b) 重建对照图（复用 Evaluation 段的画图 cell）；(c) 从先验采样的 10 张生成图（4.3.4 的代码）。
3. **需要观察的现象**：β 越大，`kl_divergence` 的稳态数值越小（被压得更狠），重建图越模糊；β=0.5 时重建最锐利，但采样生成图中出现「不合理样本」的比例可能上升。
4. **预期结果**：整理成一张三行对照表（β | 末段 KL | 重建质量描述 | 生成质量描述）。方向性结论如上；具体数值与图像质量待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：KL 项什么时候等于 0？这个状态对生成是好事吗？

**答案**：当每个样本的 \(q(z|x)\) 恰为标准正态（μ=0、σ²=1 全部维度）时 KL=0。这不是好事——此时 encoder 对所有输入输出同一个分布，decoder 无法从 z 区分不同样本，重建项会崩坏（完全的后验塌缩）。KL=0 只有在模型完全放弃重建时才可能达到，实际训练停在两项的平衡点。

**练习 2**：如果把 `pixelwise_bce` 换成 `reduction='mean'` 而 KL 保持 `sum`，实际训练出的模型会偏向哪种极端？

**答案**：等效于把 β 放大很多倍（相对权重从 1:1 变成约 batch_size×784 : 1 量级）。KL 主导，编码分布被死死压向 N(0,I)，decoder 拿不到区分信息，重建会非常模糊——正是 4.2.1 说的「口径一致才保证标准 ELBO 权重」的反面教材。修复方法是两项用同一口径（都用 sum，或都 mean 且 β 保持 1）。

**练习 3**：VAE 的 KL 项和 u2-l4 的 Dropout、weight decay 一样都是「正则化」，它们正则化的对象有何不同？

**答案**：Dropout / weight decay 正则的是**网络参数**（限制模型容量、防过拟合）；VAE 的 KL 正则的是**隐空间中编码分布的形状**（限制 q(z|x) 偏离先验的程度，让隐空间连续、可采样）。前者服务于「同一测试分布上泛化更好」，后者服务于「先验区域内处处解码合理」，后者是生成能力的来源。

### 4.3 卷积版 VAE 与隐空间采样生成

#### 4.3.1 概念说明

**生成：兑现 KL 项的「形状承诺」。** 训练期间 KL 项持续把隐空间整理成「整体接近 \(N(0, I)\)」的形状；训练结束后，从 \(N(0,I)\) 采一个 15 维向量喂给 decoder，落在的是模型「操练过」的区域，于是能解码出合理的数字。生成不需要 encoder——**encoder 是训练期的脚手架，decoder 是生成期的主角**。这也解释了 4.1.1 的对比：普通 AE 的隐空间没做任何形状整理，同样的 `torch.randn` 只能解码出噪声。

**卷积版：只换骨架、不换数学。** `ae-conv-var.ipynb` 与 `ae-var.ipynb` 的 `reparameterize`、损失、生成代码逐字相同，差别只在：encoder 用 3 个 stride-2 卷积（替代 Linear）把 28×28×1 压到 64×2×2 再接分布双头；decoder 用 Linear + `view` 回特征图形态 + 3 个转置卷积还原。空间结构不再被展平摧毁（u5-l2 讲过卷积化的动机），参数量也更省——两个模型的参数量分别约为 808,314（全连接版）与 45,887（卷积版），后者不到前者的 6%（4.3.5 练习 3 让你自己验证这两个数字）。

#### 4.3.2 核心流程

**卷积 encoder 的形状链**（用 u3-l1 的公式 \(o = \lfloor (w-k)/s \rfloor + 1\)，这里 padding=0）：

| 层 | 配置 | 输出形状 |
| --- | --- | --- |
| `enc_conv_1` | k=6, s=1→2, 1→16 通道 | ⌊(28−6)/2⌋+1 = **12** → 16×12×12 |
| `enc_conv_2` | k=4, s=2, 16→32 | ⌊(12−4)/2⌋+1 = **5** → 32×5×5 |
| `enc_conv_3` | k=2, s=2, 32→64 | ⌊(5−2)/2⌋+1 = **2** → 64×2×2 |
| `z_mean` / `z_log_var` | Linear(256→15)，先 `view(-1, 256)` | μ, log σ² ∈ R¹⁵ |

**转置卷积 decoder 的形状链**（u5-l2 的公式 \(o = (w-1)\cdot s - 2p + k\)）：

| 层 | 配置 | 输出形状 |
| --- | --- | --- |
| `dec_linear_1` + `view` | Linear(15→256)，`view(-1, 64, 2, 2)` | 64×2×2 |
| `dec_deconv_1` | k=2, s=2, 64→32 | (2−1)·2−0+2 = **4** → 32×4×4 |
| `dec_deconv_2` | k=4, s=3, p=1, 32→16 | (4−1)·3−2+4 = **11** → 16×11×11 |
| `dec_deconv_3` | k=6, s=3, p=4, 16→1 | (11−1)·3−8+6 = **28** → 1×28×28 |

注意 decoder **不是 encoder 的镜像**：下采样侧是 (k6,s2)/(k4,s2)/(k2,s2)，上采样侧却用出 s=3 的配方去凑 4→11→28 的整数链。这正是 u5-l2 的结论「转置卷积的尺寸配方与结构强耦合、需要手工凑」的又一次体现。

**生成流程**（两份 notebook 相同）：

```text
rand_features = torch.randn(10, num_latent)   # 从先验 N(0, I) 采样
new_images = model.decoder(rand_features)     # 只用 decoder
```

#### 4.3.3 源码精读

关键代码一：**卷积 encoder 层定义**（[ae-conv-var.ipynb:L177-L203](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-conv-var.ipynb#L177-L203)），这段代码做了什么：三个 kernel 递减（6→4→2）、stride 全为 2 的卷积逐级压缩空间分辨率，通道翻倍（1→16→32→64）；末端两个并列的 `Linear(64*2*2, num_latent)` 产出 μ 与 log σ²——与全连接版的双头设计完全一致，只是输入从 500 维隐层换成 256 维展平特征图。

关键代码二：**转置卷积 decoder 层定义**（[ae-conv-var.ipynb:L211-L229](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-conv-var.ipynb#L211-L229)），这段代码做了什么：先 `Linear(15, 256)` 回到瓶颈形状，再由三个转置卷积按 4.3.2 表格的配方放大回 28×28；注意三个转置卷积的 (k, s, p) 各不相同——这是为凑出精确的 28×28 手工调配的结果。

关键代码三：**encoder / decoder 前向**（[ae-conv-var.ipynb:L240-L276](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-conv-var.ipynb#L240-L276)），这段代码做了什么：encoder 每个卷积后接 `F.leaky_relu`，最后 `x.view(-1, 64*2*2)` 展平接双头并调用 `reparameterize`（L253-L255，与全连接版逐字相同）；decoder 里 `x.view(-1, 64, 2, 2)` 把 Linear 输出重新折回特征图形态再逐级上采样、末端 sigmoid。源码里还留着一串被注释掉的 `#print('conv1 out:', x.size())` 调试行——作者也是靠打印形状来对齐这条链的。

关键代码四：**训练循环的差异**（[ae-conv-var.ipynb:L872-L901](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-conv-var.ipynb#L872-L901)），这段代码做了什么：与全连接版逐行对应，唯二差异是 ① `features` 不再展平（L876 直接 `.to(device)`，NCHW 喂给卷积）；② 损失三行（L882-L885）一字不差——VAE 的损失与骨架无关。作者日志：首批 cost 70508.81，第 50 epoch 末批 13924.47，总耗时 7.01 分钟（作者 GPU）。

关键代码五：**从先验采样生成**（[ae-var.ipynb:L969-L992](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-var.ipynb#L969-L992)，卷积版对应 [ae-conv-var.ipynb:L1091-L1113](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-conv-var.ipynb#L1091-L1113)），这段代码做了什么：外层循环跑 10 轮，每轮 `torch.randn(10, num_latent)` 从标准正态采 10 个隐向量、`model.decoder(...)` 直接解码成 10 张图并画成一行——总共 10 行生成样本。核心就两行（[ae-var.ipynb:L976-L977](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-var.ipynb#L976-L977)）：

```python
rand_features = torch.randn(n_images, num_latent).to(device)
new_images = model.decoder(rand_features)
```

采样分布正是 KL 项训练时对齐的目标 \(N(0, I)\)——生成与训练目标在这里闭环。

#### 4.3.4 代码实践

**实践：隐空间线性插值——验证隐空间的连续性。**

1. **实践目标**：取两张测试图像（比如一个 1 和一个 7），在隐空间连一条线段，看 decoder 是否把两端「平滑地」过渡过来——这是 KL 正则带来的隐空间连续性的直接证据。
2. **操作步骤**（示例代码，基于训练好的 `model` 与 `test_loader`）：

```python
# 示例代码：隐空间线性插值生成 10 张过渡图像
import torch, numpy as np, matplotlib.pyplot as plt

imgs, labels = next(iter(test_loader))          # 取一个测试 batch
i1, i2 = 0, 1                                  # 任选两张不同的图
x1 = imgs[i1:i1+1].view(-1, 28*28).to(device)  # 全连接版需展平
x2 = imgs[i2:i2+1].view(-1, 28*28).to(device)

with torch.no_grad():
    z1, _, _ = model.encoder(x1)               # 取 z_mean（不加采样噪声）
    z2, _, _ = model.encoder(x2)
    t = torch.linspace(0, 1, 10).unsqueeze(1).to(device)
    z_interp = (1 - t) * z1 + t * z2           # 线性插值
    imgs_out = model.decoder(z_interp).view(-1, 28, 28).cpu()

fig, axes = plt.subplots(1, 10, figsize=(15, 1.5))
for ax, im in zip(axes, imgs_out):
    ax.imshow(im, cmap='binary'); ax.axis('off')
```

3. **需要观察的现象**：从左到右，图像应从第一个数字形态逐渐「融化再凝固」为第二个数字形态，中间过渡张大多仍是可辨认的数字（而非噪声）。
4. **预期结果**：若中间出现大片明显不合理的样本，说明该 β 下隐空间连续性不足（可与 4.2.4 的 β 实验联合观察：β 大 → 插值更平滑）。具体效果待本地验证。卷积版同理，把 `view(-1, 28*28)` 去掉、直接喂 NCHW 张量即可（注意 [ae-conv-var.ipynb:L103](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-conv-var.ipynb#L103) 的 `cuda:1` 要先改回 `cuda:0`）。

#### 4.3.5 小练习与答案

**练习 1**：生成时为什么直接 `torch.randn` 采样而不经过 encoder？

**答案**：encoder 的作用是训练期间「给每个样本一个分布并把这些分布整理到 N(0,I) 附近」。训练完成后隐空间形状已经就位，从 N(0,I) 直接采样就是在按约定使用这个空间；encoder 只对训练分布内的图像有意义，生成新图像时用不上它。

**练习 2**：插值实验里为什么用 `encoder` 返回的 `z_mean`，而不是第三个返回值 `encoded`（重参数化采样）？

**答案**：`encoded` 每次调用都叠加一份新的随机噪声 ε，插值端点本身就会抖动，看不出「纯方向变化」的趋势；`z_mean` 是分布的中心、确定性的，插值只反映两个端点之间隐向量的线性路径。这也呼应 4.1.3 的观察——`reparameterize` 内嵌在 encoder 里、不分 train/eval 模式。

**练习 3**：用 `sum(p.numel() for p in model.parameters())` 分别统计两个模型的参数量，验证 4.3.1 给出的 808,314 与 45,887。

**答案**：全连接版：`hidden_1` 784×500+500 = 392,500；双头各 500×15+15 = 7,515（共 15,030）；`linear_3` 15×500+500 = 8,000；`linear_4` 500×784+784 = 392,784；合计 808,314。卷积版：三个卷积层各 (6·6·1+1)×16 = 592、(4·4·16+1)×32 = 8,224、(2·2·32+1)×64 = 8,256；双头各 256×15+15 = 3,855（共 7,710）；`dec_linear_1` 15×256+256 = 4,096；三个转置卷积（权重形状为 in×out×k×k）各 64×32×2×2+32 = 8,224、32×16×4×4+16 = 8,208、16×1×6×6+1 = 577；合计 45,887。约为全连接版的 5.7%。

## 5. 综合实践

**任务：把 β 扫描、先验采样、隐空间插值串成一个完整的小型研究实验。**

以 `ae-var.ipynb` 为底本（卷积版可作加分项），完成以下流程：

1. **改造**：在超参区加入 `beta` 变量，损失行改为 `cost = beta * kl_divergence + pixelwise_bce`；为省时可把 `num_epochs` 降到 10，并在每个 epoch 结束时打印该 epoch 的平均 `kl_divergence` 与 `pixelwise_bce`。
2. **训练三份模型**：β ∈ {0.5, 1, 4}，其余超参（seed、lr、batch、`num_latent=15`）完全不动——延续 u2-l4 确立的「克隆基线、只改一处」受控对比法。
3. **每个 β 产出四组证据**：
   - 测试集前 15 张的「原图 vs 重建」两行对照图（复用 [ae-var.ipynb:L816-L830](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-var.ipynb#L816-L830) 的画图 cell）；
   - 从先验采样的 10 张生成图（[ae-var.ipynb:L976-L977](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-var.ipynb#L976-L977) 两行代码）；
   - 一组 10 张隐空间线性插值图（4.3.4 的示例代码）；
   - 训练曲线上末段 KL 值与 BCE 值。
4. **汇总**：填一张表（β | 末段 KL | 重建清晰度 | 生成合理性 | 插值平滑度），并用 4.2.1 的「两项博弈」解释你观察到的规律。

**预期**：β=0.5 → 重建最清晰、KL 稳态值最大、插值中间可能冒出不合理样本；β=4 → KL 被压得很小、重建与生成都偏模糊、插值最平滑；β=1 居中。具体数值与图像表现待本地验证。

## 6. 本讲小结

- VAE 把 AE 的「确定性瓶颈」换成「分布瓶颈」：encoder 输出 \(q(z|x)=N(\mu, \mathrm{diag}(\sigma^2))\) 的参数（双头 Linear），编码即从该分布采样；实现上输出 log σ² 而非 σ²，因为 \(\exp\) 变换恒正且便于 KL 里的对数运算。
- **重参数化** \(z = \mu + \epsilon\sigma,\ \epsilon \sim N(0,I)\)：分布不变、计算图重排——随机性装进与参数无关的 ε，梯度经加法与乘法流回 μ 与 log σ²，这是 VAE 能用反向传播训练的关键。
- **损失 = 逐像素 BCE（sum）+ 闭式 KL（sum）**：KL 把编码分布拉向 \(N(0,I)\)、重建想把样本记牢，两者博弈塑造出「大致铺满标准正态又保留类簇」的隐空间；两项 reduction 口径必须一致，否则等效改动了 β。
- **生成 = `torch.randn` 采样 + `model.decoder(...)`**：兑现 KL 项对隐空间形状的承诺；encoder 是训练期的脚手架。隐空间线性插值用 `z_mean`（避开采样噪声）可验证空间连续性。
- **卷积版只换骨架不换数学**：stride-2 卷积压到 64×2×2、转置卷积（k/s/p 手工凑尺寸链 2→4→11→28）还原，`reparameterize` 与损失逐字相同，参数量约为全连接版的 5.7%。

## 7. 下一步学习建议

- **下一讲 u5-l4（条件 VAE，CVAE）**：把数字标签 one-hot 拼进输入与解码器，实现「指定生成哪个数字」——本讲的 `torch.randn` 采样是「盲抽」，CVAE 给它装上方向盘。阅读 `pytorch_ipynb/autoencoder/ae-cvae.ipynb` 时可对照本讲的 `reparameterize`（结构几乎一致，差异集中在拼接处）。
- 若想看**另一种生成范式**，可预习 u5-l5 的 `gan.ipynb`：GAN 用判别器替代 VAE 的显式似然，训练不稳定性换来更锐利的生成。
- **延伸阅读**：Kingma & Welling 的原始论文 *Auto-Encoding Variational Bayes*（2013，本讲两份 notebook 注释里提到的 Kingma & Welling 2015 是其期刊版）以及 *β-VAE*（Higgins et al., 2017，4.2.4 实验的理论出处）。
- **动手方向**：把 `num_latent` 从 15 改成 2，训练后把测试集编码画成 2D 散点（按标签着色），再在 [-3,3]×[-3,3] 网格上逐点解码拼成一张「隐空间地图」——这是理解 KL 正则如何排布隐空间最直观的实验。
