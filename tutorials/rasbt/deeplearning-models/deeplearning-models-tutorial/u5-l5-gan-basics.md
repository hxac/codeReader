# 生成对抗网络 GAN 基础

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 GAN 中**生成器（Generator）**与**判别器（Discriminator）**各自的输入、输出与结构，以及为什么把两个「对手」装进同一个 `nn.Module`。
2. 逐行解释**对抗训练循环**：一个 batch 里先后上演的两幕戏——先训生成器、再训判别器，各自走一遍「forward → loss → zero_grad → backward → step」五步范式。
3. 掌握 **BCE 损失下真实/伪造标签的构造**：`valid=1`、`fake=0`，以及生成器「反用」`valid` 标签的非饱和技巧。
4. 理解 `detach()` 切断梯度、`0.5*` 缩放判别器损失、真实图像重缩放到 \([-1,1]\) 这三个关键工程细节。
5. 会**固定噪声向量**，在不同 epoch 用同一批 \(z\) 生成图像，对比生成质量的演变。

## 2. 前置知识

### 2.1 生成模型的两条路线：VAE 与 GAN

在前几讲中，我们已经见过了自编码器家族（u5-l1～u5-l4）。它们属于「显式」生成路线：给输入编码成一个向量或分布，再用重建损失（必要时加 KL 项）把 encoder 和 decoder 绑在一起训练。GAN 走的是完全不同的「博弈」路线：

| 对比项 | VAE（u5-l3） | GAN（本讲） |
| --- | --- | --- |
| 训练信号 | 重建损失 + KL 散度 | 对抗损失（无重建、无 KL） |
| 网络角色 | encoder + decoder | generator + discriminator |
| 有无 encoder | 有（训练期脚手架） | 无，只有生成方向 |
| 生成质量特点 | 通常偏模糊、训练稳定 | 通常更锐利、训练不稳定 |
| 理论基础 | 变分下界（ELBO） | 极小极大博弈 |

### 2.2 造假者与鉴伪者的直觉

GAN 的直觉是一个博弈：**生成器是造假币的人，判别器是验钞的警察**。警察看得越多、鉴别越准，造假者就被迫提高工艺；造假者造得越真，警察也得升级鉴别手段。两边在对抗中共同变强。理想终点是：假币与真币无法区分——此时判别器对任何输入都只能输出 0.5（「拿不准」），而生成器输出的分布与真实数据分布重合。

### 2.3 你需要带着走的旧知识

- **五步训练范式**（u2-l2 建立）：forward → loss → zero_grad → backward → step。本讲的训练循环里它会出现**两次**，一次给生成器、一次给判别器。
- **BCE 的「概率版 / logits 版」配套规则**（u5-l1、u5-l2 建立）：`F.binary_cross_entropy` 吃 **(0,1) 概率**（网络末端要有 Sigmoid）；`F.binary_cross_entropy_with_logits` 吃 logits。本讲判别器以 `Sigmoid` 结尾，所以配套概率版。
- **Dropout 与 train/eval 双模式**（u2-l4 建立）：`model.train()` / `model.eval()` 翻转层的行为。本讲**两个网络里都有 Dropout**，采样生成时必须 `eval()`。
- **`p−y` 梯度**（u2-l1、u2-l2 建立）：sigmoid + BCE 对 logit 的梯度是「预测概率 − 标签」，饱和因子被约掉了。这个结论在本讲 4.3 节会再次发挥关键作用。

### 2.4 极小极大博弈的形式化

原始 GAN 论文（Goodfellow 等，2014）把博弈写成如下值函数：

\[
\min_G \max_D V(D, G) = \mathbb{E}_{x \sim p_{\text{data}}}[\log D(x)] + \mathbb{E}_{z \sim p_z}[\log(1 - D(G(z)))]
\]

- \(D(x)\)：判别器认为 \(x\) 是真实图像的概率；
- \(G(z)\)：生成器从噪声 \(z\) 造出的图像；
- 判别器想最大化 \(V\)（鉴别得越准越好），生成器想最小化 \(V\)（越难分辨越好）。

理论上可以证明：固定 \(G\) 时最优判别器为

\[
D^*(x) = \frac{p_{\text{data}}(x)}{p_{\text{data}}(x) + p_g(x)}
\]

当生成分布 \(p_g = p_{\text{data}}\) 时 \(D^* = 1/2\)。这给了我们一个读训练日志的「物理基准」：BCE 损失下「完全拿不准」对应 \(-\log 0.5 = \ln 2 \approx 0.693\)。

## 3. 本讲源码地图

本讲唯一的源码主角是一个自包含 notebook：

| 文件 | 作用 |
| --- | --- |
| [pytorch_ipynb/gan/gan.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan.ipynb) | 全连接 GAN 的完整实现：MNIST 数据、GAN 类（生成器 + 判别器）、对抗训练循环、损失曲线、采样可视化 |

notebook 内部各段在 JSON 源文件中的位置（**行号指向 .ipynb 的 JSON 源码行**，GitHub 上打开文件后锚点可直接定位）：

| 段落 | 行号 | 内容 |
| --- | --- | --- |
| 超参数设置 | L105-L122 | device、学习率、`NUM_EPOCHS`、`LATENT_DIM` 等 |
| MNIST 数据 | L126-L150 | `datasets.MNIST` + DataLoader |
| GAN 类定义 | L175-L204 | 生成器、判别器与两个前向方法 |
| 实例化与双优化器 | L213-L219 | 两个独立的 Adam 优化器 |
| 对抗训练循环 | L843-L902 | 先训 G 再训 D 的交替更新 |
| 损失曲线绘制 | L947-L967 | 双横轴（迭代/epoch）损失图 |
| 采样可视化 | L995-L1011 | 固定 5 个噪声向量生成图像 |

这个 notebook 沿用全仓库的「Imports → Settings/Dataset → Model → Training → Evaluation」五段式结构（u1-l2 建立），且是 `gan/` 目录下其他变体（卷积版、WGAN、DCGAN）的共同基线。

## 4. 核心概念与源码讲解

### 4.1 生成器与判别器：装进一个类的两个对手

#### 4.1.1 概念说明

- **生成器 G**：把一个 75 维噪声向量 \(z \sim U(-1, 1)\)（`LATENT_DIM = 75`）映射成 784 维的「图像向量」。末端用 `Tanh` 激活，输出值域 \([-1, 1]\)。
- **判别器 D**：把 784 维输入映射成一个标量概率——「这是真实图像的可能性」。末端用 `Sigmoid`，输出 (0, 1)。
- 两者结构互为镜像：G 是「75 → 128 → 784」的喇叭口，D 是「784 → 128 → 1」的沙漏，中间统一用 `LeakyReLU` + `Dropout(0.5)`。
- 本实现把 G 与 D 装进**同一个 `GAN` 类**（都是 `nn.Sequential` 子模块），但训练时用**两个独立的优化器**分别接管两半参数。
- 注意这是一个**无条件 GAN**：MNIST 的标签 `targets` 在整个训练循环里只被用来取 batch 大小（`targets.size(0)`），完全没有参与损失计算——模型不知道自己在生成哪个数字。这与上一讲 CVAE「指定标签生成」形成对照。

#### 4.1.2 核心流程

```text
噪声 z (75维, U(-1,1))
      │
      ▼
┌─────────────┐   784维假图 (Tanh, 值域[-1,1])
│  生成器 G    │ ─────────────────────────┐
│ 75→128→784  │                          │
└─────────────┘                          ▼
                              ┌─────────────────┐
   真实图像 (784维, 重缩放到[-1,1]) ──────▶│   判别器 D       │──▶ 概率 (0,1)
                              │  784→128→1      │    「是真图吗？」
                              └─────────────────┘
```

- 前向只有两条路径：`z → G → 假图` 与 `（真图或假图）→ D → 概率`。
- 训练时两半参数**交替更新**（见 4.2），推理生成时只用 G。

#### 4.1.3 源码精读

**生成器定义**：[pytorch_ipynb/gan/gan.ipynb:L181-L187](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan.ipynb#L181-L187) —— 两层全连接的喇叭口：`Linear(75→128)` → `LeakyReLU` → `Dropout(0.5)` → `Linear(128→784)` → `Tanh`。输出值域 \([-1,1]\)，这正是后面训练循环要把真实图像从 \([0,1]\) 重缩放到 \([-1,1]\) 的原因——**两边的值域必须对齐**，否则判别器可以仅凭数值范围就分辨真假，博弈退化。

**判别器定义**：[pytorch_ipynb/gan/gan.ipynb:L189-L195](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan.ipynb#L189-L195) —— 镜像的沙漏：`Linear(784→128)` → `LeakyReLU` → `Dropout(0.5)` → `Linear(128→1)` → `Sigmoid`。末端 Sigmoid 输出概率，与概率版 `F.binary_cross_entropy` 配套（u5-l1 建立的配套规则）。

**两个前向方法**：[pytorch_ipynb/gan/gan.ipynb:L198-L204](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan.ipynb#L198-L204) —— `generator_forward(z)` 与 `discriminator_forward(img)`，后者把输出 `view(-1)` 压成一维张量以匹配 BCE 的标签形状。注意 GAN 类**没有定义统一的 `forward`**——因为这个模型没有单一的前向语义，两个子网络被交替调用，分开命名反而更清晰。另外有个值得留意的细节：`discriminator_forward` 内部写的是 `model.discriminator(img)` 而不是 `self.discriminator(img)`——它引用的是 notebook 里的**全局变量** `model`。在本 notebook 的上下文里恰好能跑通，但如果实例化第二个 GAN 对象，它调用的仍是全局 `model` 的判别器权重。这是「读源码要看清变量的真实来源」的又一实例（u3-l2、u3-l5 都遇到类似教训）。

**双优化器**：[pytorch_ipynb/gan/gan.ipynb:L213-L219](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan.ipynb#L213-L219) —— `torch.manual_seed` 后实例化模型，然后创建**两个独立的 Adam 优化器**：`optim_gener` 只含 `model.generator.parameters()`，`optim_discr` 只含 `model.discriminator.parameters()`，学习率都是 0.001。两个优化器各自 `step()` 时只会更新自己接管的那一半参数——这是「交替训练」能实现的机制保障。

**超参数**：[pytorch_ipynb/gan/gan.ipynb:L112-L122](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan.ipynb#L112-L122) —— `random_seed=123`、双学习率 0.001、`NUM_EPOCHS=100`、`BATCH_SIZE=128`、`LATENT_DIM=75`。注意 `IMG_SIZE` 是把 `IMG_SHAPE=(1,28,28)` 连乘得到的 784——展平后的图像维度。

**数据加载**：[pytorch_ipynb/gan/gan.ipynb:L132-L150](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan.ipynb#L132-L150) —— 标准 MNIST 管线，`ToTensor` 把像素压到 \([0,1]\)，训练 loader `shuffle=True`。虽然也加载了 `test_dataset`，但本 notebook 的训练与评估都没有用到它。

#### 4.1.4 代码实践

**实践目标**：动手验证两个网络的结构、参数量与输出值域，建立「未训练时会发生什么」的直觉。

**操作步骤**（示例代码，建议在运行完 notebook 的 Model 单元之后新开 cell 执行）：

```python
# 示例代码：结构检查
torch.manual_seed(123)
model = GAN()

# 1) 参数量对账
num_g = sum(p.numel() for p in model.generator.parameters())
num_d = sum(p.numel() for p in model.discriminator.parameters())
print('Generator params:', num_g)     # 预期 110,864
print('Discriminator params:', num_d) # 预期 100,609

# 2) 值域检查：未训练的 G 输出应在 [-1, 1]
z = torch.zeros((8, 75)).uniform_(-1.0, 1.0)
fake_img = model.generator_forward(z)
print('G output min/max:', fake_img.min().item(), fake_img.max().item())

# 3) 未训练的 D 输出应在 0.5 附近
pred = model.discriminator_forward(fake_img)
print('D preds:', pred.detach().numpy().round(3))
```

**需要观察的现象**：

1. 参数量是否与手工计算一致（G：\(75\times128+128 + 128\times784+784 = 110{,}864\)；D：\(784\times128+128 + 128\times1+1 = 100{,}609\)）。
2. G 输出的 min/max 被 Tanh 限制在 \([-1,1]\) 内。
3. 未训练的 D 对 8 张假图的判别概率散布在 0.5 附近（随机初始化的 logit 接近 0，Sigmoid 后约 0.5）。

**预期结果**：三步都会通过；具体数值待本地验证。这个「未训练 D 输出 ≈ 0.5」的直觉将在 4.3 解释第一个 epoch 的损失日志时直接用到。

#### 4.1.5 小练习与答案

**练习 1**：为什么生成器末端用 `Tanh` 而判别器末端用 `Sigmoid`？可以互换吗？

> **答案**：两者由各自的任务决定，不能互换。G 要输出「与真实图像同值域的图像」，本实现把真实数据重缩放到 \([-1,1]\)，Tanh 的值域恰好是 \([-1,1]\)；D 要输出「是真实图像的概率」，必须在 \((0,1)\) 且要与概率版 BCE 配套，Sigmoid 的值域恰好是 \((0,1)\)。互换后 G 输出 \((0,1)\) 与重缩放后的真实数据值域不匹配、D 输出 \([-1,1]\) 不再是合法概率。

**练习 2**：把 `LATENT_DIM` 从 75 改成 5，模型还能训练吗？会有什么影响？

> **答案**：能训练——G 的第一层 `Linear(5→128)` 照常工作。但隐空间维度大幅压缩意味着所有生成图像都要从 5 维噪声里「挤」出来，生成多样性与可表达的隐空间结构都会受限（直观上更容易出现模式崩塌或样本趋同）。这是开放性问题，具体表现待本地验证。

**练习 3**：本 notebook 是「无条件 GAN」。如果想让它知道要生成哪个数字，最小改动是什么思路？

> **答案**：把数字标签的 one-hot 向量拼接到 G 的输入 \(z\)（以及可选地拼接进 D 的输入），变成条件 GAN（CGAN）。这与上一讲 CVAE「把 one-hot 拼进 encoder 与 decoder」的思路完全同构——上一讲我们已经用 `ae-cvae` 实现过 PyTorch 版本，可直接对照。

### 4.2 对抗训练循环：一个 batch 里的两幕戏

#### 4.2.1 概念说明

GAN 训练循环的本质：**每个 batch 内先演完「生成器视角」再演「判别器视角」两幕戏**，各自走一遍完整的五步范式：

- 第一幕（训 G）：G 造假图，希望 D 把假图判为真（用 `valid` 标签算损失），只更新 G 的参数。
- 第二幕（训 D）：D 分别看真图与假图，学会分辨（真图标 1、假图标 0），只更新 D 的参数。

关键工程细节有三个：

1. **`detach()` 切断梯度**：第二幕中假图以 `generated_features.detach()` 的身份进 D，防止 D 阶段的反向传播波及生成器。
2. **真实图像重缩放**：`features = (features - 0.5) * 2.` 把 \([0,1]\) 映射到 \([-1,1]\)，与 G 的 Tanh 输出值域对齐。
3. **`0.5*` 缩放判别器损失**：`discr_loss = 0.5*(real_loss + fake_loss)`，把 D 的每次更新幅度减半，给 G 留活路（详见 4.3）。

#### 4.2.2 核心流程

```text
for epoch in range(NUM_EPOCHS):                 # 100 个 epoch
  model.train()                                  # Dropout 生效
  for features, targets in train_loader:         # 469 个 batch

    真图 = (features - 0.5) * 2  → 展平 784 维     # 值域对齐
    valid = 全 1 张量;  fake = 全 0 张量            # 标签构造

    ─── 第一幕：训练生成器 ───
    z ← U(-1, 1)^75                               # 本 batch 新采样的噪声
    假图 = G(z)
    gener_loss = BCE(D(假图), valid)              # 「骗过 D」
    optim_gener: zero_grad → backward → step      # 只更新 G

    ─── 第二幕：训练判别器 ───
    real_loss = BCE(D(真图), valid)               # 真图标 1
    fake_loss = BCE(D(假图.detach()), fake)       # 假图标 0，梯度不回传到 G
    discr_loss = 0.5 * (real_loss + fake_loss)
    optim_discr: zero_grad → backward → step      # 只更新 D

    记录两个 loss
```

作者保存的运行日志（torch 1.2.0，总耗时 20.94 分钟）给了两个有信息量的快照：

- 第 1 个 epoch 开头：`Gen/Dis Loss: 0.6576/0.7134`——两者都接近 \(\ln 2 \approx 0.693\)，对应「未训练的 D 输出约 0.5、完全拿不准」（正好接上 4.1.4 的实践）。
- 第 1 个 epoch 中段：`Gen/Dis Loss: 5.1797/0.0280`——**D 几乎立刻学会分辨**（loss 0.028 意味着非常自信），G 损失飙升。
- 第 100 个 epoch：`Gen/Dis Loss: 0.87~1.16 / 0.60~0.66`——两者都回到 \(\ln 2\) 附近拉锯，说明博弈接近均衡：D 不再能轻松分辨。

**读图警告**：GAN 的损失曲线与分类任务完全不同——**不是越低越好，也没有单调下降**。G loss 低表示骗过了 D，D loss 低表示鉴别力强，两者此消彼长。均衡态附近的典型特征是两条曲线都在 \(\ln 2 \approx 0.693\) 附近震荡。

#### 4.2.3 源码精读

**循环骨架与 train 模式**：[pytorch_ipynb/gan/gan.ipynb:L843-L849](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan.ipynb#L843-L849) —— `discr_costs`/`gener_costs` 两个列表逐 batch 收集损失（注意收集的是张量不是标量，画图时才被 matplotlib 隐式取出）；每个 epoch 开头 `model = model.train()` 确保 Dropout 按训练模式工作（u2-l4 的双模式知识）。

**重缩放与展平**：[pytorch_ipynb/gan/gan.ipynb:L853-L855](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan.ipynb#L853-L855) —— `(features - 0.5)*2.` 完成 \([0,1] \to [-1,1]\)，随后 `view(-1, IMG_SIZE)` 展平成 128×784。这两行的顺序很重要：值域对齐必须在进 D 之前完成。

**标签构造**：[pytorch_ipynb/gan/gan.ipynb:L857-L858](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan.ipynb#L857-L858) —— `valid` 是与 batch 等长的全 1 张量，`fake` 是全 0 张量。同一对标签在两幕戏里被「各取所需」（详见 4.3.3）。

**第一幕：训练生成器**：[pytorch_ipynb/gan/gan.ipynb:L864-L879](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan.ipynb#L864-L879) —— `torch.zeros((batch, LATENT_DIM)).uniform_(-1.0, 1.0)` 先建零张量再用原地 `uniform_` 填充均匀噪声；生成假图后计算 `gener_loss = F.binary_cross_entropy(discr_pred, valid)`——注意标签用的是 `valid`（1）！G 的目标是让 D 把假图判为真。随后 `optim_gener` 走完 zero_grad → backward → step 三步，只更新 G 的参数。

**第二幕：训练判别器**：[pytorch_ipynb/gan/gan.ipynb:L881-L895](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan.ipynb#L881-L895) —— 真图用 `valid` 标签、假图用 `fake` 标签各算一个 BCE，取平均再乘 0.5 得 `discr_loss`。最关键的一行是 `generated_features.detach()`：`detach()` 返回一个脱离计算图的新张量（成为叶子），D 的反向传播到此为止。若不 detach，会发生两件事：其一，`gener_loss.backward()` 已经释放过 G 的计算图，第二次 backward 会因试图穿越已释放的图而报错（除非 `retain_graph=True`）；其二，即便不报错，梯度也会白白算进 G 的参数里。所以 detach 既省计算又防止误更新。

**损失记录**：[pytorch_ipynb/gan/gan.ipynb:L897-L898](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan.ipynb#L897-L898) —— 每 batch 追加两个损失，供后续画曲线。

**损失曲线**：[pytorch_ipynb/gan/gan.ipynb:L947-L967](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan.ipynb#L947-L967) —— 与 u4-l4 见过的 `plot_training_loss` 同款的双横轴技巧：`ax1` 横轴是迭代步，`twiny()` 生成的 `ax2` 移到下方显示 epoch 刻度。G/D 两条曲线画在同一坐标系里观察拉锯。

#### 4.2.4 代码实践

**实践目标**：亲眼看到「D 从拿不准到能分辨、再回到拉锯」的动态过程。

**操作步骤**（示例代码，把 notebook 的 Training 单元复制一份后改造）：

```python
# 示例代码：把 NUM_EPOCHS 临时改为 2，并在训练循环内加一段诊断打印
NUM_EPOCHS_DIAG = 2
for epoch in range(NUM_EPOCHS_DIAG):
    model.train()
    for batch_idx, (features, targets) in enumerate(train_loader):
        # ……（与原循环相同，略）……

        # 第二幕结束后插入诊断（每 50 个 batch 打一次）：
        if not batch_idx % 50:
            with torch.no_grad():
                p_real = model.discriminator_forward(features).mean().item()
                p_fake = model.discriminator_forward(
                    generated_features.detach()).mean().item()
            print(f'ep{epoch} b{batch_idx:03d} | '
                  f'D(真图)均值={p_real:.3f} | D(假图)均值={p_fake:.3f}')
```

**需要观察的现象**：训练刚开始时 `D(真图)` 与 `D(假图)` 都在 0.5 附近；随着 batch 推进，两者迅速分离（`D(真图)` 冲向 1、`D(假图)` 压向 0）；之后随 G 变强又会出现回落与震荡。

**预期结果**：分离趋势明确；具体分离速度与回弹幅度受随机种子与硬件影响，待本地验证。若全程分离度不降，说明 G 完全没学到东西——那正是 GAN 训练失败的典型形态。

#### 4.2.5 小练习与答案

**练习 1**：如果省略第一幕里的 `optim_gener.zero_grad()`，会发生什么？

> **答案**：G 的梯度会跨 batch 累加（u2-l2 建立的知识），本 batch 的更新方向混入历史梯度，训练变得不可控。注意本实现里 G 与 D 各有自己的优化器和梯度域，所以省略 `optim_discr.zero_grad()` 也同理只会污染 D 的梯度，不会互相影响。

**练习 2**：训练循环里为什么从头到尾没出现 test_loader 和验证集？

> **答案**：GAN 是无监督的生成模型，没有「验证准确率」这类可早停的标量指标（生成质量本身难以自动化度量）。本 notebook 加载了 test_dataset 但未使用；评估方式是第 5 段的「采样画图 + 人工检视」，以及观察损失是否进入 \(\ln 2\) 附近的拉锯区。

**练习 3**：把两幕戏的顺序对调（先训 D 再训 G），需要改哪些东西？

> **答案**：不是简单交换代码块。对调后第二幕的 G 需要**当轮 D 更新之后**重新生成假图（或沿用旧假图但要重新审视 detach 时机与标签语义）；此外 `generated_features` 的计算依赖 G 的参数，代码组织需重排。顺序本身不是数学硬约束（原始论文甚至建议 D 每轮多更新几步），但本 notebook 的变量流是按「先 G 后 D」写好的——改动前先在纸上画出每个张量的依赖关系。

### 4.3 BCE 损失与真实/伪造标签的构造

#### 4.3.1 概念说明

二元交叉熵（BCE）对单个样本的定义：

\[
L(p, y) = -\big[\, y \log p + (1-y)\log(1-p) \,\big]
\]

其中 \(p \in (0,1)\) 是判别器输出的概率，\(y \in \{0, 1\}\) 是标签。本讲涉及的标签语义一共三种用法，全部由同一对 `valid`/`fake` 张量承担：

| 场景 | 损失的输入 | 使用的标签 | 语义 |
| --- | --- | --- | --- |
| 训练 G | \(D(G(z))\) | `valid`（1） | 「希望 D 把假图判为真」 |
| 训练 D：真图支 | \(D(x)\) | `valid`（1） | 真图标 1 |
| 训练 D：假图支 | \(D(G(z))\)（detach 后） | `fake`（0） | 假图标 0 |

最反直觉的是第一行：G 的损失**借用 `valid` 标签**。这对应「非饱和（non-saturating）」改写——原始极小极大公式里 G 的损失是 \(\log(1-D(G(z)))\)，实践中普遍改为最大化 \(\log D(G(z))\)，即最小化 \(-\log D(G(z)) = \text{BCE}(D(G(z)),\, 1)\)。两者在全局最优处相同，但训练初期的梯度表现天差地别（见 4.3.2）。

另外两个损失构造细节：

- **`0.5*(real_loss + fake_loss)`**：数学上等价于对两支取平均，效果是把 D 的每次更新步长减半。训练初期 D 学得远比 G 快（4.2.2 的日志：第 1 个 epoch 内 D loss 就降到 0.028），缩放 D 是给 G 留活路的常用启发式。
- **概率版配套**：判别器末端有 Sigmoid 输出概率，所以用 `F.binary_cross_entropy`；若改用 `binary_cross_entropy_with_logits`，则应去掉 Sigmoid（数值上更稳，是工业实现的常见选择）。

#### 4.3.2 核心流程

**（1）为什么必须非饱和：梯度层面的解释。** 设 \(p = D(G(z))\)，比较两种 G 损失对 \(p\) 的导数：

\[
\frac{d}{dp}\big[\log(1-p)\big] = -\frac{1}{1-p},
\qquad
\frac{d}{dp}\big[-\log p\big] = -\frac{1}{p}
\]

训练初期 D 轻松碾压 G，\(p \to 0\)（D 非常确定地判假）。此时：

- 饱和版（原始公式）梯度大小 \(\frac{1}{1-p} \to 1\)——梯度趋近常数且整体数值很小，G 几乎学不动；
- 非饱和版梯度大小 \(\frac{1}{p} \to \infty\)——D 越自信地判假，推 G 改进的力反而越大。

这正是 4.2.2 日志里「D loss 低到 0.028 时 G 仍能继续学习（G loss 后来从 5.18 一路降回 1 附近）」的机制保障。更深一层，sigmoid + BCE 对 logit 的梯度是 \(p - y\)（u2-l1、u2-l2 已推导过同款结论）：饱和因子被约掉，梯度大小恰是「错得多离谱」，这解释了非饱和版为什么在 \(p \to 0\) 时仍有大梯度。

**（2）均衡态的损失读数。** 当 \(p_g = p_{\text{data}}\) 时最优判别器输出 \(D^* = 1/2\)，代入 BCE：

\[
\text{BCE}(0.5,\, y) = -\log 0.5 = \ln 2 \approx 0.693
\]

所以 G 与 D 的损失都应以 \(\ln 2\) 为「势均力敌」的参考线。对照作者日志第 100 epoch：G ≈ 0.87～1.16（略高于 0.693，G 还没完全骗过 D）、D ≈ 0.60～0.66（略低于 0.693，D 还保留一点鉴别力）——双方都贴着均衡线拉锯，这正是「能训练出像样样本」的健康状态。

**（3）三种标签用法的关系图**：

```text
                valid = 1 ──────────────┐
                 (G 想被叫真)            │
                    │                   │
     BCE(D(G(z)), valid)      BCE(D(x), valid)     BCE(D(G(z)).detach(), fake)
     = -log D(G(z))           = -log D(x)          = -log(1 - D(G(z)))
        「造假者目标」            「验钞·真币支」         「验钞·假币支」
              │                      └─────────┬─────────┘
              ▼                                ▼
        只更新 G 的参数              discr_loss = 0.5*(两支之和)，只更新 D
```

#### 4.3.3 源码精读

**标签张量**：[pytorch_ipynb/gan/gan.ipynb:L857-L858](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan.ipynb#L857-L858) —— `valid`/`fake` 是与 batch 等长的全 1/全 0 浮点张量，并搬到 device。它们只是「模板」，同一对张量在三处损失里被赋予不同语义。

**生成器损失（非饱和实现）**：[pytorch_ipynb/gan/gan.ipynb:L872-L875](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan.ipynb#L872-L875) —— `gener_loss = F.binary_cross_entropy(discr_pred, valid)`，其中 `discr_pred` 是 D 对假图的概率。注释写明意图："Loss for fooling the discriminator"。用 `valid` 标签即最大化 \(\log D(G(z))\)。

**判别器损失（两支 + 0.5 缩放）**：[pytorch_ipynb/gan/gan.ipynb:L885-L891](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan.ipynb#L885-L891) —— 真图支 `real_loss` 用 `valid`，假图支 `fake_loss` 用 `fake` 且输入是 `generated_features.detach()`（4.2.3 已分析），最后 `discr_loss = 0.5*(real_loss + fake_loss)`。

**概率版 BCE 的值域要求**：判别器定义 [pytorch_ipynb/gan/gan.ipynb:L189-L195](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan.ipynb#L189-L195) 末端的 `Sigmoid` 保证了进 BCE 的张量在 (0,1)——这是使用 `F.binary_cross_entropy` 的前提；若把 Sigmoid 去掉而直接喂 Linear 的原始输出，log 会遇到非正数，产生 nan 或报错。

#### 4.3.4 代码实践

**实践目标**：用 autograd 数值验证「sigmoid + BCE 对 logit 的梯度 = \(p - y\)」，把 4.3.2 的理论亲手确认一遍；并画出两条 BCE 曲线建立直觉。

**操作步骤**（示例代码，可在任意环境独立运行）：

```python
# 示例代码：梯度验证 + BCE 曲线
import torch
import torch.nn.functional as F
import matplotlib.pyplot as plt

# 1) 验证 d/dz BCE(sigmoid(z), y) == sigmoid(z) - y
for z0, y in [(0.0, 1), (2.0, 0), (-3.0, 1)]:
    z = torch.tensor([z0], requires_grad=True)
    p = torch.sigmoid(z)
    loss = F.binary_cross_entropy(p, torch.tensor([float(y)]))
    loss.backward()
    print(f'z={z0:+.1f}, y={y} | p={p.item():.4f}, 解析梯度 p-y={p.item()-y:+.4f}, '
          f'autograd={z.grad.item():+.4f}')

# 2) 两条损失曲线
p = torch.linspace(0.01, 0.99, 100)
plt.plot(p, -torch.log(p), label='BCE(p, 1) = -log p   （G 的非饱和损失）')
plt.plot(p, -torch.log(1-p), label='BCE(p, 0) = -log(1-p) （D 的假图支）')
plt.axvline(0.5, ls='--', c='gray'); plt.axhline(0.693, ls=':', c='gray')
plt.xlabel('D 输出概率 p'); plt.ylabel('损失'); plt.legend(); plt.show()
```

**需要观察的现象**：

1. 每一行 `autograd` 打印值与「解析梯度 \(p-y\)」完全一致（数学恒等式，必然成立）。
2. 曲线图中：`-log p` 在 \(p \to 0\) 处陡峭上升（错得离谱 → 梯度巨大，即非饱和版的力量来源）；`-log(1-p)` 在 \(p \to 1\) 处陡峭上升；灰色虚线交点 \((0.5, \ln 2)\) 即均衡参考点。

**预期结果**：梯度三组全部吻合（例如 \(z=0, y=1\) 时 \(p=0.5\)，梯度 \(= -0.5\)）；曲线形状如上。数值精确值可本地复现。

#### 4.3.5 小练习与答案

**练习 1**：G 的损失为什么用 `valid`（1）而不是 `fake`（0）作标签？用 `fake` 会发生什么？

> **答案**：用 `valid` 等价于最小化 \(-\log D(G(z))\)，即最大化「D 认假为真」的对数似然——这是非饱和技巧。若改用 `fake` 标签，G 的损失变成 \(-\log(1-D(G(z)))\)，梯度方向完全反了：G 会被推着「让自己更假」，训练目标被摧毁。（注意：若只想实现**饱和版**原始公式 \(\log(1-D(G(z)))\) 的最小化，同样应该以 `valid` 为标签、但要取负号改成最大化——直接换标签不是实现饱和版，而是南辕北辙。）

**练习 2**：`F.binary_cross_entropy` 能不能直接吃判别器里 `Linear(128, 1)` 的原始输出？

> **答案**：不能。该函数要求输入是 \((0,1)\) 内的概率，内部要对输入取 `log`，输入超出 \((0,1)\) 会让 log 无定义（产生 nan/报错）。两个正确选项：(a) 保留 Sigmoid 用概率版（本实现）；(b) 去掉 Sigmoid 改用 `F.binary_cross_entropy_with_logits`（数值更稳定，与 u5-l1 讲过的「logits 配 with_logits 版」规则一致）。

**练习 3**：`discr_loss = 0.5*(real_loss + fake_loss)` 里的 0.5 起什么作用？去掉它训练还能收敛吗？

> **答案**：0.5 把 D 的每次更新步长减半（对两支之和取平均），是抑制「D 学得太快、G 被甩开」的启发式——对应 4.2.2 日志里 D 一开始就冲到 0.028 的现实威胁。去掉它并不必然失败（学习率 0.001 的 Adam 有一定鲁棒性），但 D 压制 G 的风险变大，训练方差会更高。是否影响本 notebook 的最终样本质量，待本地对照验证。

## 5. 综合实践

**任务**：修改生成器与判别器的隐藏层宽度，在多组「容量配比」下训练 20 个 epoch，用**固定的同一批噪声向量**追踪生成质量的演变，总结容量失衡的后果。

**实验设计**（沿用 u2-l4 确立的「克隆基线、一次只改一处」方法论）：

| 组别 | G 隐层宽度 | D 隐层宽度 | 考察问题 |
| --- | --- | --- | --- |
| A（基线） | 128 | 128 | 原版对照 |
| B（生成器弱） | 32 | 128 | G 容量不足会怎样 |
| C（判别器弱） | 128 | 32 | D 容量不足会怎样 |
| D（双方同扩） | 256 | 256 | 对等放大的收益与代价 |

**操作步骤**：

1. 复制 `gan.ipynb` 四份，只改 [pytorch_ipynb/gan/gan.ipynb:L182](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan.ipynb#L182) 与 [pytorch_ipynb/gan/gan.ipynb:L190](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan.ipynb#L190) 两处 `Linear` 的隐藏层宽度；`NUM_EPOCHS` 改为 20，其余超参（lr、batch、`LATENT_DIM`、Dropout、随机种子）一律不动。
2. 训练前生成并保存固定噪声（示例代码）：

   ```python
   # 示例代码：固定噪声，训练外只生成一次
   torch.manual_seed(0)
   z_fixed = torch.zeros((20, LATENT_DIM)).uniform_(-1.0, 1.0).to(device)
   ```

3. 在训练循环的 epoch 末尾插入采样（示例代码）：

   ```python
   # 示例代码：每 5 个 epoch 用同一批 z_fixed 生成 20 张图
   if (epoch + 1) % 5 == 0:
       model.eval()                       # 关掉 G 里的 Dropout，生成才可复现
       with torch.no_grad():
           imgs = model.generator_forward(z_fixed).view(-1, 28, 28).cpu()
       # 保存 imgs 供事后拼图对比（matplotlib imshow, cmap='binary'）
       model.train()
   ```

   注意 `model.eval()` 这一步不可省：G 内部也有 Dropout（u2-l4 的双模式知识），不关的话同一个 \(z\) 每次生成结果都不同，「固定噪声对比」就失效了。
4. 训练结束后按表格记录：两组参数量（用 4.1.4 的统计代码）、最后 20 个 batch 的 Gen/Dis 损失均值、第 5/10/15/20 epoch 的固定噪声样本网格、主观质量评分（如 1～5 分）。

**需要观察的现象与预期结果（待本地验证，GAN 训练方差大，建议同组重复 2～3 个种子再下结论）**：

- 组 B（G 弱）：参数量约降到基线的 1/3。预期 G 损失整体偏高、样本更模糊，甚至向「少数几个形态坍缩」的模式崩塌方向走——瓶颈（u5-l1 的概念）从数据侧被人为搬进了网络容量。
- 组 C（D 弱）：D 损乏鉴别力，G 可能很早就能「骗过去」（G 损失不高），但失去了一个强有力的老师，样本细节预期提升有限或停滞。
- 组 D（双方同扩）：参数量约为基线 4 倍。样本可能更锐利，但单 epoch 耗时上升，且更容易出现训练震荡——容量越大博弈越难稳。
- 对照所有组的固定噪声网格：好的训练应在 20 个 epoch 内看到从噪声块 → 模糊数字轮廓 → 可辨认数字的演变轨迹。

**总结要求**：用 3～5 句话回答「容量失衡的后果是什么」——重点区分「G 弱」与「D 弱」两种失衡方向各自的失败形态，并说明为什么保持双方势均力敌（或刻意削弱 D，如 4.3.3 的 0.5 缩放）是 GAN 训练配方的核心考量。

## 6. 本讲小结

- GAN 把生成建模变成**博弈**：生成器从 \(z \sim U(-1,1)^{75}\) 造假图，判别器输出「是真图的概率」；本实现把两者装进同一个 `GAN` 类、用两个独立优化器交替更新，MNIST 标签完全不用（无条件生成）。
- 训练循环是**一个 batch 两幕戏**：先以 `valid` 标签训 G（骗过 D），再以「真图标 1 / 假图标 0」训 D；每幕各走一遍五步范式，`optim_*` 的参数划分保证各自只更新自己那半。
- 三个工程关键点：`detach()` 让 D 阶段的梯度止步于假图（既避免穿越已释放的计算图报错，又防止误更新 G）；真实图像重缩放到 \([-1,1]\) 与 Tanh 输出值域对齐；`0.5*` 缩放 D 损失给 G 留活路。
- G 损失用 `valid` 标签是**非饱和技巧**：最小化 \(-\log D(G(z))\)，使 D 自信判假（\(p \to 0\)）时梯度 \(\frac{1}{p}\) 反而巨大；sigmoid+BCE 对 logit 的梯度 \(p - y\) 是其数学根基。
- 读日志的方式与分类任务相反：损失**不单调下降**，双方在 \(\ln 2 \approx 0.693\) 附近拉锯才是健康均衡（作者日志第 100 epoch：G ≈ 0.87～1.16 / D ≈ 0.60～0.66）。
- 评估靠**固定噪声向量**采样画图：`model.eval()` 关掉 G 的 Dropout 后，同一 \(z\) 的生成结果可复现，从而纵向对比质量演变。

## 7. 下一步学习建议

本讲的全连接 GAN 用 `Linear + Dropout` 做两个网络，生成的是 784 维向量。下一讲 **u5-l6（卷积 GAN、WGAN 与 DCGAN）** 将沿三个方向升级，建议按此顺序阅读对应 notebook：

1. **卷积化**：读 `pytorch_ipynb/gan/gan-conv.ipynb`——判别器换成卷积、生成器换成转置卷积（u5-l2 讲过的上采样之道在此复用），生成质量显著提升。
2. **换损失**：读 `pytorch_ipynb/gan/wgan-1.ipynb`——WGAN 用权重裁剪 + 无 sigmoid 的损失替代 BCE 博弈，训练更稳；对比它与本讲 BCE 版的损失曲线形态差异。
3. **完整工程**：读 `pytorch_ipynb/gan/dcgan-celeba.ipynb` 与 `gan-conv-smoothing.ipynb`——真实人脸数据上的 DCGAN 全流程，以及 label smoothing 这一缓解 D 过自信的小技巧（正好呼应本讲的 0.5 缩放）。

阅读时带着本讲的问题去对照：每一份变体分别改动了「结构、损失、标签、更新节奏」四个维度中的哪一个？
