# 自编码器基础

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚自编码器（Autoencoder, AE）是什么：一个「把输入压缩再还原」的无监督神经网络，以及 encoder / decoder / 瓶颈（bottleneck）三个部件各自的作用。
2. 读懂 `ae-basic.ipynb` 中 784 → 32 → 784 的单隐藏层自编码器定义，并会推导它的参数量。
3. 理解「重建损失」：为什么训练目标就是输入本身，源码中 `F.binary_cross_entropy(decoded, features)` 这一行为什么成立，它与 MSE 重建损失的关系。
4. 掌握「提取编码 + 下游分类器」的表示评估方法：读懂 `ae-basic-with-rf.ipynb` 如何把 32 维编码交给 sklearn 随机森林，并与原始像素、PCA 降维做三组对照。
5. 完成一个瓶颈维度扫描实验（{4, 16, 64}），体会「压缩率—重建质量—下游分类精度」三者的权衡。

本讲是第 5 单元（生成模型）的入口：自编码器本身不是严格的生成模型，但它引出的「隐空间 / 低维表示」概念，是后面卷积自编码器、VAE、GAN 全部内容的地基。

## 2. 前置知识

本讲不引入新的 PyTorch 语法，全部是在 [u2-l3 多层感知机与 nn.Module](u2-l3-mlp-basics.md) 之上的重组。需要回顾的概念：

- **无监督 / 自监督学习**：之前的分类任务用人工标注的标签做监督信号；自编码器不需要标签，它从数据自身构造监督信号（目标 = 输入），所以叫「自监督」。这也意味着同样的方法可以用在任何无标注数据上。
- **`nn.Module` 的三件套**（u2-l3）：`__init__` 里建层、`forward` 里写前向计算、`model(x)` 触发调用。本讲模型仍然只有两个 `Linear` 层。
- **五步训练范式**（u2-l2）：`forward → loss → zero_grad → backward → step`。本讲训练循环一步不多、一步不少，唯一的变化是 loss 的 target 从标签换成了图像本身。
- **sigmoid 与交叉熵的梯度**（u2-l1、u2-l5）：sigmoid + 二元交叉熵（BCE）组合下，损失对 logit 的梯度是 \(\hat{x} - x\)，不含会在饱和区消失的 \(\hat{x}(1-\hat{x})\) 因子。u2-l5 已经见过「sigmoid + MSE 在饱和区梯度消失」的问题，本讲正好是它的反面教材。
- **`transforms.ToTensor()`**（u2-l2）：把像素值缩放到 [0, 1] 区间。这个值域细节决定了本讲为什么能用 sigmoid + BCE，第 4.2 节会展开。
- **随机森林**（sklearn）：一种决策树集成模型，训练不需要梯度，只要「特征矩阵 + 标签」。本讲把它当作「检验表示质量的探针」，不需要深究其内部原理。
- **手工改权重先 detach**（u2-l3）：对 `layer.weight` 做原地操作前要先 `.detach()`，本讲源码里会再次出现。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `pytorch_ipynb/autoencoder/ae-basic.ipynb` | 最小自编码器：784 → 32 → 784 的单隐藏层全连接 AE，含训练循环与「原图 vs 重建」可视化。本讲 4.1、4.2 节的主线。 |
| `pytorch_ipynb/autoencoder/ae-basic-with-rf.ipynb` | 同款 AE 的接口升级版：把 `forward` 拆成 `encoder()` / `decoder()` 两个方法，并在训练后做三组随机森林对照实验（原始像素 / PCA 32 维 / AE 32 维编码）。本讲 4.3 节的主线。 |

两份 notebook 的 AE 部分几乎逐行相同（都是 `Imports → Dataset/Settings → Model → Training → Evaluation` 五段式，见 u1-l2），`ae-basic-with-rf.ipynb` 相当于在 `ae-basic.ipynb` 后面接了一章「编码质量评估」。建议对照阅读。

> **行号说明**：本讲所有永久链接指向 `.ipynb` 原始 JSON 文件的行号（在 GitHub 上打开链接即命中对应行）。作者运行环境为 Python 3.6.8 / torch 1.0.0（ae-basic）与 Python 3.7.3 / torch 1.3.0（ae-basic-with-rf），CPU 或 GPU 均可运行，本机版本不同时代码同样适用。

## 4. 核心概念与源码讲解

### 4.1 自编码器结构：encoder、decoder 与瓶颈

#### 4.1.1 概念说明

自编码器要解决的问题是：**如何在不依赖标签的情况下，让神经网络学到数据的紧凑表示？**

做法是搭一个「沙漏型」网络：

```
输入 x          瓶颈 z（编码）        重建 x̂
784 维  ──►  encoder  ──►  32 维  ──►  decoder  ──►  784 维
 (图像)     (压缩, W₁)   (隐空间)     (还原, W₂)     (重建图像)
```

- **encoder（编码器）**：把 784 维像素压成 32 维向量。本例就是一层 `Linear(784, 32)` + LeakyReLU。
- **瓶颈（bottleneck）**：中间的 32 维。它是整条通路最窄的地方，信息必须「挤」过这里。压缩比 784 / 32 = 24.5 倍。
- **decoder（解码器）**：把 32 维编码尽量还原回 784 维原图。本例是一层 `Linear(32, 784)` + sigmoid。

直觉是：网络要还原原图，就必须在 32 个数字里装下「这张图是数字几、笔画在哪里」的关键信息，装不进无关的像素噪声。于是瓶颈处的编码（latent code，隐编码）就成了数据的一种**低维表示**——这叫**表示学习（representation learning）**。

两个先修问题先想清楚：

1. **为什么不直接学恒等映射？** 如果中间层和输入一样宽（784 维），网络抄近路记住输入即可，什么都学不到。瓶颈的「窄」才是学习的驱动力——这是自编码器设计的核心。
2. **和 PCA 什么关系？** PCA 是经典的线性降维方法（找方差最大的投影方向）。只有一层线性层、不加任何激活的 AE，学到的变换与 PCA 高度相关；本例 encoder 带了 LeakyReLU，是轻微非线性的版本。4.3 节的对照实验会直接给出两者在同一任务上的成绩差。

#### 4.1.2 核心流程

数据在模型里的一次往返（batch 大小 B）：

| 步骤 | 操作 | 形状 |
| --- | --- | --- |
| 1 | 输入展平 `view(-1, 28*28)` | \(([B, 1, 28, 28] \to [B, 784])\) |
| 2 | encoder：`Linear(784, 32)` + LeakyReLU | \([B, 784] \to [B, 32]\) |
| 3 | decoder：`Linear(32, 784)` + sigmoid | \([B, 32] \to [B, 784]\) |
| 4 | 重建 \(\hat{x}\) 与原图 \(x\) 逐像素算损失 | 标量 |

前向公式（\(\sigma\) 为 sigmoid，编码 \(z\) 可为负值，重建 \(\hat{x}\) 在 [0,1]）：

\[ z = \mathrm{LeakyReLU}(W_1 x + b_1), \qquad \hat{x} = \sigma(W_2 z + b_2) \]

参数量随瓶颈宽度 \(h\) 的变化（encoder 与 decoder 的权重矩阵互为转置形状）：

\[ P(h) = \underbrace{784h + h}_{\text{encoder}} + \underbrace{784h + 784}_{\text{decoder}} = 1569h + 784 \]

代入 \(h = 32\)：\(P(32) = 1569 \times 32 + 784 = 50{,}992\)。

#### 4.1.3 源码精读

**① 超参数设置**。[pytorch_ipynb/autoencoder/ae-basic.ipynb:L96-L107](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic.ipynb#L96-L107) 定义了 device、学习率 0.005、5 个 epoch、batch 256，以及本讲的主角——结构超参数 `num_features = 784` 与 `num_hidden_1 = 32`。改瓶颈维度只需要改这一个数（综合实践就会这样做）。

> 顺带一个「以代码为准」的小发现：notebook 的 Markdown 描述里写的是「compresses **768**-pixel MNIST images into 32-pixel vectors (32-times smaller)」，而代码实际是 `num_features = 784`（28 × 28），真实压缩比是 784/32 ≈ 24.5 倍。读源码时描述文字只能当路标，数字要以代码为准。

**② 模型定义**。[pytorch_ipynb/autoencoder/ae-basic.ipynb:L158-L196](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic.ipynb#L158-L196) 是完整的 `Autoencoder` 类、实例化和优化器。逐段看：

- encoder 层：[L164](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic.ipynb#L164) 创建 `self.linear_1 = torch.nn.Linear(num_features, num_hidden_1)`，即压缩映射 \(784 \to 32\)；
- 手工初始化演示：[L170-L171](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic.ipynb#L170-L171) 用 `self.linear_1.weight.detach().normal_(0.0, 0.1)` 把权重改成 0 均值、0.1 标准差的正态分布，偏置清零。这正是 u2-l3 讲过的「先 detach 再原地改」手法，代码注释也说明这只是演示，默认初始化通常够用；
- decoder 层：[L174](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic.ipynb#L174) 创建 `self.linear_2 = torch.nn.Linear(num_hidden_1, num_features)`，把 32 维编码映射回 784 维；
- **一个值得注意的源码笔误**：[L175-L176](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic.ipynb#L175-L176) 紧跟在 `linear_2` 定义之后，再次初始化的却是 `linear_1`（复制粘贴痕迹）。实际效果是：`linear_1` 被初始化了两次（第二次生效），`linear_2` 一直用 PyTorch 默认初始化。无伤大雅，但印证了 u3-l5 的教训——注释说要做的事和代码真正做的事要分开核对；
- 前向传播：[L179-L189](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic.ipynb#L179-L189)。encoder 段是 `linear_1` + `F.leaky_relu`（[L182-L183](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic.ipynb#L182-L183)），decoder 段是 `linear_2` 后接 `torch.sigmoid`（[L186-L187](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic.ipynb#L186-L187)）。注意两段用了**不同**的激活函数：编码没有值域约束（可以是任意实数，LeakyReLU 还避免了 ReLU 的死区），而重建必须落在 [0,1] 才能和 ToTensor 之后的像素比较（原因见 4.2 节）；
- 实例化与优化器：[L192-L196](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic.ipynb#L192-L196)，先 `torch.manual_seed(random_seed)` 固定初始化，再 `model.to(device)`，优化器用 **Adam**（lr=0.005），不是之前常用的 SGD——AE 任务对学习率较敏感，Adam 的自适应步长更省心。

**③ 数据管线**。[pytorch_ipynb/autoencoder/ae-basic.ipynb:L113-L140](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic.ipynb#L113-L140) 加载 MNIST 并打印一个 batch 的形状 `torch.Size([256, 1, 28, 28])`。注意 transform **只有 `ToTensor()`，没有 `Normalize`**——这不是疏忽：像素保持 [0,1] 值域，才能与 sigmoid 输出直接对齐（4.2 节展开）。

#### 4.1.4 代码实践：数一数沙漏的参数

1. **实践目标**：验证 4.1.2 的参数量公式 \(P(h) = 1569h + 784\)，并直观感受瓶颈对模型大小的影响。
2. **操作步骤**：运行 `ae-basic.ipynb` 的模型 cell 后，在新增 cell 里执行下面的统计（**示例代码**，非仓库原有）：

```python
# 示例代码：统计自编码器各部件参数量
for name, param in model.named_parameters():
    print(f'{name:24s} {tuple(param.shape)}  numel={param.numel()}')

total = sum(p.numel() for p in model.parameters())
print('总参数量:', total)          # 预期 50992
print('encoder 占比:', sum(p.numel() for n, p in model.named_parameters()
                           if n.startswith('linear_1')) / total)
```

3. **需要观察的现象**：`linear_1.weight` 形状 `(32, 784)`、`linear_2.weight` 形状 `(784, 32)`——两者元素数相同但形状互为转置；总参数量 50,992 与手算一致。
4. **预期结果**：把 `num_hidden_1` 依次改成 4 / 16 / 64 / 128，总参数量应为 7,060 / 25,888 / 101,200 / 201,616（按公式 \(1569h+784\) 计算）。
5. 以上数值来自公式推导，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `num_hidden_1` 设成 784（与输入同宽），这个自编码器还能学到有用的表示吗？

> **答**：基本不能。没有瓶颈时，网络最容易学的解是（近似）恒等映射——极端情况下一层线性层取 \(W_1 = W_2^{-1}\) 就能零误差「还原」，编码只是输入的另一个线性组合，没有任何压缩和信息提炼。瓶颈的窄是表示学习的驱动力。

**练习 2**：为什么 decoder 输出端用 sigmoid，encoder 却用 LeakyReLU？

> **答**：输出 \(\hat{x}\) 要和 [0,1] 值域的像素目标逐元素比较，sigmoid 恰好把输出压进 [0,1]；而编码 \(z\) 没有值域要求，LeakyReLU 引入非线性（让 AE 强于纯线性降维）同时避免 ReLU 的神经元死区，允许编码取负值。

**练习 3**：这个 AE 一共有多少参数？encoder 和 decoder 各占多少？

> **答**：共 \(P(32) = 50{,}992\)。encoder：\(784 \times 32 + 32 = 25{,}120\)；decoder：\(32 \times 784 + 784 = 25{,}872\)。两者权重矩阵元素数一样多，差在偏置个数（32 对 784）。

### 4.2 重建损失：把输入当作目标的无监督训练

#### 4.2.1 概念说明

自编码器的损失叫**重建损失（reconstruction loss）**：衡量重建 \(\hat{x}\) 与原图 \(x\) 的差距。最常用的两种：

- **逐像素二元交叉熵（BCE）**：把「像素值是不是 1」当作 784 个独立的二元问题，输出层配 sigmoid。**本仓库源码用的就是它。**
- **均方误差（MSE）**：\(\|\hat{x} - x\|^2\)，对输出值域没有要求，通常配线性输出层。

讲义规划里预期是 MSE，但源码实际用的是 BCE——这不是谁对谁错，两种都是标准做法，本讲以源码为准精读 BCE，并在代码实践里补上 MSE 对照。

为什么这里能用 BCE？关键在两件「配套」：

1. `transforms.ToTensor()` 已把像素缩放到 [0, 1]，像素值可以被解释为「这个位置有墨迹的概率」；
2. decoder 末端的 sigmoid 输出也在 [0, 1]。

两者值域对齐，逐像素交叉熵才有意义。**如果像 u4-l3 那样再做 z-score 标准化（像素变成大致 [−1,1]，均值 0），sigmoid+BCE 就不再合法**，应改用 MSE 配线性输出。这是「数据预处理—输出激活—损失函数」三者的联动约束，比记结论更重要。

BCE 相对 MSE 的一个好处与 u2-l5 的结论成镜像：sigmoid+BCE 对 logit \(\ell_j\) 的梯度是

\[ \frac{\partial \mathcal{L}_j}{\partial \ell_j} = \hat{x}_j - x_j \]

和 softmax+交叉熵的 \(p-y\) 同构，**没有** sigmoid+MSE 里那个会在饱和区消失的 \(\hat{x}_j(1-\hat{x}_j)\) 因子（u2-l5 的「自信犯错梯度消失」问题），训练更稳。

#### 4.2.2 核心流程

训练循环还是五步范式，只是 target 变了。伪代码：

```
for epoch, for batch:
    features = 图像展平成 [B, 784]        # labels 被解包但从不使用
    decoded = model(features)             # ① forward：一次编码+解码
    cost   = BCE(decoded, features)       # ② loss：目标就是输入自己
    optimizer.zero_grad()                 # ③ 清梯度
    cost.backward()                       # ④ 反向传播
    optimizer.step()                      # ⑤ 更新
```

逐像素 BCE（`F.binary_cross_entropy` 默认 reduction='mean'，对所有 \(B \times 784\) 个元素取平均）：

\[ \mathcal{L} = -\frac{1}{B \times 784}\sum_{i=1}^{B}\sum_{j=1}^{784}\Big[\, x_{ij}\log\hat{x}_{ij} + (1-x_{ij})\log(1-\hat{x}_{ij}) \,\Big] \]

监督信号完全来自输入：**MNIST 的标签 `targets` 在整个训练里一次都没被用到**。这就是「无监督/自监督」在代码层面的样子。

#### 4.2.3 源码精读

**① 训练循环**。[pytorch_ipynb/autoencoder/ae-basic.ipynb:L252-L277](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic.ipynb#L252-L277)。对比 u2-l2 的分类训练循环，结构逐行相同，差异只有两处：

- [L256-L257](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic.ipynb#L256-L257)：注释明说 `# don't need labels, only the images (features)`，并把图像展平成 `[B, 784]`；
- [L260-L264](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic.ipynb#L260-L264)：**本讲最关键的一行**是 `cost = F.binary_cross_entropy(decoded, features)`——第一个参数是模型输出（input），第二个参数是原图（target）。u2-l2 强调过 `F.cross_entropy` 只吃 logits，而 `F.binary_cross_entropy` 恰恰相反，**只吃概率**（sigmoid 之后的值），这就是 forward 里必须先 `torch.sigmoid` 的原因。喂 logits 进去不会报错但结果是错的，属于静默错误，要特别小心。

作者记录的运行输出：cost 从首个 batch 的 **0.7100** 一路降到第 5 个 epoch 的 **0.0935** 左右并趋于平台。0.71 ≈ −ln(0.5)×784/784… 换句话说，初始模型对每个像素都输出约 0.5 的「不确定」概率，训练后网络学会了「哪里通常是白的（接近 0）、哪里可能出现笔画（接近 1）」。

**② 重建可视化**。[pytorch_ipynb/autoencoder/ae-basic.ipynb:L306-L324](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic.ipynb#L306-L324) 画两行 15 列的对照图（[L318-L319](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic.ipynb#L318-L319)）：上排 `orig_images` 是原图、下排 `decoded_images` 是重建，每个元素 `view((28, 28))` 还原成二维再 `imshow(cmap='binary')`。

一个 notebook 编程细节：这里的 `features` / `decoded` 复用的是**训练循环最后一个 batch 残留的变量**（Python 循环变量在循环结束后仍在作用域内），并没有重新前向传播。`ae-basic-with-rf.ipynb` 里标题为 "Training Dataset" / 测试集的两个可视化 cell（[L355](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic-with-rf.ipynb#L355)、[L408](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic-with-rf.ipynb#L408)）也是同一模式——新建了 loader 却只用了残留变量，两个 cell 画出的其实是同一批图。读 notebook 时留意变量来源，自己写时建议显式重新前向。

#### 4.2.4 代码实践：BCE 换 MSE 对照

1. **实践目标**：亲手验证「损失函数—输出激活」的配套约束，观察两种重建损失的训练差异。
2. **操作步骤**：
   - 完整运行 `ae-basic.ipynb` 一遍，记下最终 cost 与可视化图；
   - 把训练循环里的损失行（L261）替换为（**示例代码**）：`cost = F.mse_loss(decoded, features)`，保持 sigmoid 输出不动，重新训练；
   - 再做一版「正统 MSE」：把 forward 里的 `torch.sigmoid(logits)` 改成直接 `return logits`，同样用 `F.mse_loss`，第三组实验；
   - 分别用可视化 cell 画出三组的重建图。
3. **需要观察的现象**：三组都能收敛；sigmoid+BCE 与线性输出+MSE 的重建清晰度相近（数字轮廓、笔画都应可辨），sigmoid+MSE 的组合收敛最吃力（呼应 u2-l5 的梯度消失分析）。
4. **预期结果**：784 → 32 → 784 的压缩率下，三组重建都不会「完美」，细节（笔画粗细、拐角）有涂抹感属正常；重点是三组**相对**差异与 cost 曲线形态。具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：训练循环里 `(features, targets)` 解包了 targets，它被用在哪里？

> **答**：哪里都没用。它只是 DataLoader 默认返回 `(图像, 标签)` 二元组的副产品，训练中唯一被消费的是 `features`（既是输入又是目标）。这正是无监督训练的标志。

**练习 2**：`F.binary_cross_entropy` 和 `F.cross_entropy` 对输入的要求分别是什么？搞反会发生什么？

> **答**：`F.cross_entropy` 要求 logits（内部自动做 log_softmax，见 u2-l2）；`F.binary_cross_entropy` 要求已经过 sigmoid 的概率。把 sigmoid 后的概率喂给前者、或把 logits 喂给后者，多数情况**不会抛异常**，但损失语义错了、梯度也错了，属于最难排查的静默错误。

**练习 3**：如果给 MNIST 加上 `transforms.Normalize(mean=(0.5,), std=(0.5,))`，源码里的损失还能用吗？怎么改？

> **答**：不能直接用。标准化后像素值域约为 [−1,1]，而 sigmoid 输出恒在 [0,1]，两者不可比，BCE 还会对 \(\hat{x}=0\) 或 1 的极端值产生数值问题。两种改法：去掉 Normalize 回到 [0,1]；或保留标准化，把 decoder 输出激活换成 `tanh`（值域 [−1,1]）配 MSE，或直接线性输出配 MSE。

### 4.3 提取编码：用随机森林检验表示质量

#### 4.3.1 概念说明

自编码器训练完只说明「重建得不错」，但 32 维编码到底有没有用？`ae-basic-with-rf.ipynb` 给出一个朴素的检验思路：**把编码当作特征，交给一个与深度学习无关的分类器（随机森林），看它能不能从 32 个数字里认出数字类别**。分类准确率越高，说明编码保留的类别判别信息越多。

为此，这个 notebook 对模型类做了一处关键重构：把原来塞在 `forward` 里的两段计算拆成 **`encoder()` 和 `decoder()` 两个独立方法**，`forward` 只是依次调用它们。这样训练时仍用完整的 `model(x)`（行为与 `ae-basic` 完全一致），而提取编码时可以直接调用 `model.encoder(x)` 拿到 32 维向量、跳过解码——**一个模型，两种调用粒度**。这是很值得学的接口设计。

#### 4.3.2 核心流程

整章实验是三组对照（相同的 RF 超参数 `n_estimators=500, n_jobs=-1`）：

```
① 原始像素   X: 60000×784 ──► RandomForest ──► 测试准确率
② PCA 降维   X: 60000×784 ──► PCA(32) ──► 60000×32 ──► RF ──► 测试准确率
③ AE 编码    X: 60000×784 ──► model.encoder ──► 60000×32 ──► RF ──► 测试准确率
```

提取编码的伪代码（以 batch=1000 流过整个数据集）：

```
预分配 X_compr = np.ones((60000, 32)); start_idx = 0
for images, labels in train_loader:
    z = model.encoder(images.view(-1, 784))     # 只编码，不解码
    X_compr[start_idx : start_idx+1000] = z.cpu().detach().numpy()
    start_idx += 1000
```

#### 4.3.3 源码精读

**① 模型重构：encoder / decoder 拆分**。[pytorch_ipynb/autoencoder/ae-basic-with-rf.ipynb:L158-L198](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic-with-rf.ipynb#L158-L198)。与 `ae-basic` 的差异集中在三个方法：

- `encoder()`：[L179-L182](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic-with-rf.ipynb#L179-L182)，`linear_1` + LeakyReLU，返回 32 维编码；
- `decoder()`：[L184-L187](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic-with-rf.ipynb#L184-L187)，`linear_2` + sigmoid，返回重建；
- `forward()`：[L190-L198](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic-with-rf.ipynb#L190-L198)，`encoded = self.encoder(x)` → `decoded = self.decoder(encoded)`，训练路径不变。

训练循环（[L261-L276](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic-with-rf.ipynb#L261-L276) 的核心段，BCE 在 [L270](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic-with-rf.ipynb#L270)）与 `ae-basic` 逐行等价，记录的 cost 曲线也几乎一致（0.7100 → 0.0935），说明重构没有改变模型行为。

**② 基线：RF 直接吃原始 784 维像素**。[L438-L447](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic-with-rf.ipynb#L438-L447) 先建两个「整数据集一个 batch」的 loader（`batch_size=60000` / `10000`），随后 [L467-L482](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic-with-rf.ipynb#L467-L482) 各取一个 batch 展平成 NumPy 矩阵 `X_train (60000, 784)` / `X_test (10000, 784)`。sklearn 吃 NumPy 数组，所以这里有一步 `np.array(images.reshape(60000, 28*28))` 的格式转换。[L500-L502](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic-with-rf.ipynb#L500-L502) 训练并评估：**Train 100.0% / Test 97.09%**。

**③ 对照一：PCA 32 维**。[L518-L522](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic-with-rf.ipynb#L518-L522) 用 `PCA(n_components=32)`——注释明确写着 "same size as autoencoder latent space"，即与 AE 的 32 维对齐比较。注意它遵守了 u4-l3 的铁律：`fit_transform` 只作用于训练集，测试集只 `transform`（降维统计量不能泄漏）。[L540-L542](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic-with-rf.ipynb#L540-L542) 的结果：**Train 100.0% / Test 95.7%**。

**④ 对照二：AE 32 维编码**。先把 loader 换成 `batch_size=1000`（[L558-L564](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic-with-rf.ipynb#L558-L564)），然后 [L573-L583](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic-with-rf.ipynb#L573-L583) 提取训练集编码：预分配 `np.ones((60000, num_hidden_1))`，用 `start_idx` 游标每批填充 1000 行，核心调用是 `model.encoder(features)` 加 `.to(torch.device('cpu')).detach().numpy()` 三连（搬回 CPU → 脱离计算图 → 转 NumPy）。测试集同样处理（[L592-L602](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic-with-rf.ipynb#L592-L602)）。[L620-L622](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-basic-with-rf.ipynb#L620-L622) 的结果：**Train 100.0% / Test 95.23%**。

> 又一个「变量名不可全信」的例子：这两个提取 cell 里承接编码的变量沿用了训练循环的名字 `decoded = model.encoder(features)`——它存的实际是 **encoder 输出的 32 维编码**，不是解码结果。

**⑤ 三组对照汇总**（作者记录的运行结果）：

| 特征 | 维度 | RF 训练准确率 | RF 测试准确率 | 相对基线损失 |
| --- | --- | --- | --- | --- |
| 原始像素 | 784 | 100.0% | **97.09%** | — |
| PCA | 32 | 100.0% | 95.70% | −1.39 pt |
| AE 编码 | 32 | 100.0% | 95.23% | −1.86 pt |

三个读数：

1. **24.5 倍压缩只掉了不到 2 个百分点**——32 维编码确实保住了绝大部分类别判别信息。重建任务「逼」网络保留了数据的主要结构，而 MNIST 的主要结构恰好就是数字形状。
2. **AE 与 PCA 打平（95.23% vs 95.70%）**。单隐藏层 + LeakyReLU 的轻微非线性，在这个任务上没有明显超过线性降维；这与理论直觉一致（线性 AE ≈ PCA）。深度/卷积版的 AE（下一讲）才能拉开差距。
3. **三组训练准确率都是 100%**——500 棵深树的 RF 容量足以记住任何训练集，训练准确率在此无信息量，泛化只看测试集。

#### 4.3.4 代码实践：给探针换个刻度

1. **实践目标**：确认「编码质量结论」不依赖某一个特定 RF 配置，并练习批量提取编码的写法。
2. **操作步骤**：运行 `ae-basic-with-rf.ipynb` 到第 ④ 步后，在新增 cell 对同一份 `X_train_compr` / `X_test_compr` 扫描树的数量（**示例代码**）：

```python
# 示例代码：树的数量扫描
for n in [10, 50, 100, 500]:
    rf = RandomForestClassifier(n_estimators=n, n_jobs=-1).fit(X_train_compr, y_train)
    print(f'{n:4d} trees -> Test Acc: {rf.score(X_test_compr, y_test)*100:.2f}%')
```

3. **需要观察的现象**：树很少时（如 10 棵）准确率明显偏低且随种子波动，50 棵之后趋稳，500 棵相对 100 棵的增益已很小——精度的「饱和」。
4. **预期结果**：各配置的绝对数值**待本地验证**；定性预期是随树数单调不降、边际收益递减。可加测每组耗时（`%%time`）体会 `n_jobs=-1` 的并行效果。

#### 4.3.5 小练习与答案

**练习 1**：为什么随机森林在 32 维编码上仍能拿到约 95% 的准确率？

> **答**：重建目标迫使编码保留图像的主要变化结构，而 MNIST 中最主要的结构就是「是哪个数字」。类别信息近乎完整地穿过瓶颈，RF 这类强分类器足以从 32 个数里把它读出来。

**练习 2**：`ae-basic-with-rf.ipynb` 为什么要给模型加 `encoder()` / `decoder()` 方法，而不像 `ae-basic` 那样全写在 `forward` 里？

> **答**：`forward` 只暴露端到端输出，外部拿不到中间的编码。拆成两个方法后，训练走 `model(x)` 不变，提取特征时单独调 `model.encoder(x)` 即可——一份权重，两种调用粒度。这是「为下游任务暴露中间表示」的标准做法。

**练习 3**：PCA 那一步为什么是训练集 `fit_transform`、测试集只 `transform`？编码提取那步需要类似的谨慎吗？

> **答**：PCA 的投影方向是从数据估出来的统计量，只能来自训练集；若用全量数据拟合再把测试集投影进去，就构成数据泄漏（u4-l3 的原则在无监督方法上的体现）。AE 编码不需要这一层谨慎——encoder 的参数在无监督训练阶段就已经完全确定，且训练时从未接触测试集图像，推理时对新样本只是确定性前向。

## 5. 综合实践

**任务：瓶颈维度扫描——压缩率、重建质量与下游分类的三方权衡。**

1. **实践目标**：用一组受控实验回答：瓶颈取多窄时，编码仍然「够用」？把本讲三个模块（结构、训练、编码评估）串成一条流水线。

2. **操作步骤**：
   - 以 `ae-basic-with-rf.ipynb` 为模板（它已经具备 encoder 提取与 RF 评估的全部零件），对 `num_hidden_1 ∈ {4, 16, 64}` 各训练一个 AE（其余超参保持 lr=0.005、5 epochs、batch=256 不动，遵循 u2-l4 的「只改一处」原则）；
   - 用字典管理三个模型（**示例代码**）：

```python
# 示例代码：瓶颈维度扫描骨架
results = {}
for h in [4, 16, 32, 64]:          # 32 是 notebook 原始设置，作基准
    num_hidden_1 = h
    torch.manual_seed(random_seed)
    model_h = Autoencoder(num_features=784).to(device)   # num_hidden_1 为全局变量
    opt = torch.optim.Adam(model_h.parameters(), lr=learning_rate)
    # ……五步训练循环（照抄 L261-L276，loss 用 BCE）……
    # ……按 L573-L602 的游标切片法提取编码，注意预分配形状改成 (60000, h)……
    # ……RandomForestClassifier(n_estimators=500, n_jobs=-1) 训练并评分……
    results[h] = {'final_cost': ..., 'rf_test_acc': ...}
```

   - 对每个 h 用 4.2 节的可视化 cell 画「原图 / 重建」对照图；
   - 把最终 cost、RF 测试准确率、参数量 \(P(h)\) 汇总成一张表。

3. **需要观察的现象**：
   - **h=4**：压缩 196 倍，重建明显模糊，但数字轮廓大概率仍可辨认（MNIST 本征维度很低）；RF 准确率显著下滑；
   - **h=16**：重建笔画变清晰，准确率接近基准；
   - **h=64**：重建细节更丰富，但 RF 准确率相对 h=32 的提升应当很有限——编码容量早已「装下」类别信息，多余容量花在重建细节上。

4. **预期结果**：一条「准确率随 h 上升而饱和、重建质量随 h 单调变好」的曲线；h=32 的基准应复现 notebook 的 95.23%（允许小幅波动）。各 h 的具体数值**待本地验证**。

5. **思考题（选做）**：把 h=64 的 RF 准确率与 4.3 节 PCA-32 的 95.7% 对比——若 AE-64 仍追不上，说明瓶颈在哪里？（提示：LeakyReLU 一层的表达能力，而非维度。）

## 6. 本讲小结

- 自编码器 = encoder（压缩）+ 瓶颈（窄通道）+ decoder（还原），训练目标就是输入本身，全程不用标签；瓶颈的「窄」才是学到紧凑表示的驱动力。
- 本仓库的最小 AE 是 784 → 32 → 784：`Linear(784,32)` + LeakyReLU 编码，`Linear(32,784)` + sigmoid 解码，参数量 \(P(h) = 1569h + 784\)（h=32 时 50,992）。
- 重建损失源码用的是逐像素 **BCE**（不是 MSE）：合法的前提是「ToTensor 的 [0,1] 像素 + sigmoid 的 [0,1] 输出」值域对齐；`F.binary_cross_entropy` 吃概率不吃 logits；sigmoid+BCE 对 logit 的梯度 \(\hat{x}-x\) 无饱和因子，优于 sigmoid+MSE。
- 训练循环仍是五步范式，唯一变化是 `cost = F.binary_cross_entropy(decoded, features)`——target 换成图像本身；cost 从 0.71 降到 0.09。
- 评估表示质量的探针法：`ae-basic-with-rf.ipynb` 把模型拆成 `encoder()`/`decoder()` 方法，用 `model.encoder()` 提取 32 维编码交给随机森林，三组对照为原始像素 97.09% / PCA 95.70% / AE 95.23%——24.5 倍压缩只损失不到 2 个百分点，且单层 AE 与线性 PCA 基本打平。
- 读源码三个新教训：notebook 文字写 768 像素而代码是 784；`linear_1` 在 `linear_2` 定义后被重复初始化（笔误）；提取编码的变量名叫 `decoded` 但装的其实是编码——数字与语义都要以代码实际行为为准。

## 7. 下一步学习建议

- **下一讲 [u5-l2 卷积自编码器的上采样之道](u5-l2-conv-autoencoders.md)**：全连接 AE 忽略了空间结构，卷积自编码器用 Conv 层编码、用转置卷积或最近邻插值上采样解码，是图像 AE 的标准形态；届时「重建模糊」的问题会被大幅改善。
- **再往后 [u5-l3 变分自编码器 VAE](u5-l3-variational-autoencoder.md)**：本讲的隐空间是「训练样本的落点分布」，没有结构、不能采样生成；VAE 给隐空间加先验约束，才真正跨入生成模型。带着「为什么这个 AE 不能生成新图像」的问题去读效果最好。
- **延伸阅读**：`pytorch_ipynb/autoencoder/` 目录下的 `ae-deconv-jaccard.ipynb`（换一种重建损失）、`ae-conv-nneighbor.ipynb`（插值上采样）可以先扫一眼目录结构，等学完 u5-l2 再精读。
- **回顾巩固**：如果对 4.2 节「值域—激活—损失」的联动还有疑虑，建议回看 [u2-l5 从零实现反向传播](u2-l5-mlp-backprop-from-scratch.md) 的 sigmoid+MSE 梯度推导与 [u4-l3 图像变换与标准化](u4-l3-transforms-standardization.md) 的 Normalize 一节，三处对照着读会豁然开朗。
