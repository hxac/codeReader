# 卷积自编码器的上采样之道

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚**为什么要卷积化自编码器**：u5-l1 的全连接 AE 把 28×28 展平成 784 维向量，丢掉了空间结构；卷积 AE 用特征图保留它，参数量还能降两个数量级。
2. 掌握 **`torch.nn.ConvTranspose2d`（转置卷积 / 反卷积）** 的用法与输出尺寸公式，读懂 `ae-deconv.ipynb` 的解码器，并解释它为什么最后要裁剪 `[:, :, 2:30, 2:30]`。
3. 理解**最近邻插值 + 同填充卷积**这条替代上采样路线（`ae-conv-nneighbor.ipynb`）：`F.interpolate` 负责放大分辨率、无参数卷积负责「精修」，整条链路尺寸精确、无需裁剪。
4. 知道**无 pooling 版本**（`ae-deconv-nopool.ipynb`）用步长卷积替代池化做下采样的结构差异，并学会用一行代码验证「注释与实际输出尺寸不符」的源码细节。
5. 理解用 **连续 Jaccard 距离替代 BCE** 的重建损失变体（`ae-deconv-jaccard.ipynb`）：公式、直觉、实现，以及它的梯度行为与 BCE 的本质差异。
6. 完成综合实践：把 `ae-conv-nneighbor` 改造成支持更大输入尺寸（56×56）的版本，并在同一输入下对比 deconv 与 nearest 两种解码器的重建效果。

本讲是 u5-l1（自编码器基础）的直接续篇：编码器骨架、五步训练循环、BCE 重建损失都已讲过，本讲聚焦一个新问题——**解码器怎么把 7×7 的小特征图放大回 28×28**，以及围绕它的三种结构变体和一种损失变体。

## 2. 前置知识

本讲的新 PyTorch 组件只有 `ConvTranspose2d` 和 `F.interpolate` 两个，其余都是旧知识的重组：

- **自编码器三部件**（u5-l1）：encoder 压缩、瓶颈装信息、decoder 还原；训练目标就是输入本身（自监督），重建目标与 ToTensor 像素同处 [0,1] 值域，所以输出层配 sigmoid。
- **BCE 两个版本**（u5-l1）：`F.binary_cross_entropy(probas, x)` **吃概率**（sigmoid 之后）；`F.binary_cross_entropy_with_logits(logits, x)` **吃 logits**，内部用 log-sum-exp 技巧做数值稳定。模型 `forward` 返回什么，训练时就要配哪个版本——本讲四份 notebook 恰好两种搭配都有，是复习这个知识点的好机会。
- **卷积输出尺寸公式**（u3-l1）：\(o = \left\lfloor \frac{w - k + 2p}{s} \right\rfloor + 1\)；同填充（same padding）指 \(k=3, s=1, p=1\) 时输出尺寸不变；池化 \(k=2,s=2\) 尺寸减半。特征图按 NCHW（批次×通道×高×宽）组织。
- **权值共享与局部连接**（u3-l1）：卷积参数量只与卷积核大小和通道数有关，与图像尺寸无关——这是本讲参数量对比的理论基础。
- **`F.leaky_relu`**：负半轴斜率 0.01 的 ReLU，u5-l1 的 encoder 用过，本讲大量出现。
- **「注释说」与「代码做」要分开核对**（u3-l5、u4-l2 反复出现的教训）：本讲会撞上两处注释笔误，都是现成的练习素材。
- **评估可视化模式**（u5-l1）：四份 notebook 的 Evaluation 段都直接复用训练循环最后一个 batch 残留的 `features` / `decoded` 变量画「原图 vs 重建」两行对照图，不再重新前向传播。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `pytorch_ipynb/autoencoder/ae-deconv.ipynb` | **基准版**卷积 AE：MaxPool 下采样编码器 + 两层转置卷积上采样解码器，BCEWithLogits 损失。4.1 节主线。 |
| `pytorch_ipynb/autoencoder/ae-conv-nneighbor.ipynb` | **插值版**：编码器与基准版相同，解码器换成 `F.interpolate(nearest)` + 同填充卷积。4.2 节主线。 |
| `pytorch_ipynb/autoencoder/ae-deconv-nopool.ipynb` | **无池化版**：编码器去掉 MaxPool，改用步长为 2 的卷积下采样。4.3 节主线。 |
| `pytorch_ipynb/autoencoder/ae-deconv-jaccard.ipynb` | **换损失版**：模型与基准版逐字相同，把 BCE 换成连续 Jaccard 距离。4.4 节主线。 |

四份 notebook 是一组精心设计的**受控对比实验**：都压缩到 7×7×8 = 392 维编码、都用 Adam 训 10 个 epoch、batch 都是 128，甚至参数量都完全相同（665 个，见 4.1 节），差异只集中在「怎么下采样、怎么上采样、用什么损失」。

先给一张总览表，后面各节逐个拆开：

| notebook | 编码器下采样 | 解码器上采样 | `forward` 返回 | 训练损失 | seed / lr | 作者日志末段成本 |
| --- | --- | --- | --- | --- | --- | --- |
| ae-deconv | 卷积(s=1) + MaxPool ×2 | 转置卷积 ×2 + 居中裁剪 | `(logits, probas)` | `binary_cross_entropy_with_logits` | 456 / 0.005 | ≈ 0.096 |
| ae-conv-nneighbor | 卷积(s=1) + MaxPool ×2 | `interpolate` ×2 + 同填卷积 ×2（无裁剪） | `(logits, probas)` | `binary_cross_entropy_with_logits` | 123 / **0.05** | ≈ 0.082 |
| ae-deconv-nopool | 卷积(**s=2**) ×2（无池化） | 转置卷积 ×2 + 裁剪 `[:-1,:-1]` | 单个 sigmoid 张量 | `binary_cross_entropy` | 456 / 0.005 | ≈ 0.103 |
| ae-deconv-jaccard | 卷积(s=1) + MaxPool ×2 | 转置卷积 ×2 + 居中裁剪 | `(logits, probas)` | `continuous_jaccard` | 456 / 0.005 | ≈ 0.18（Jaccard 量纲） |

> **行号说明**：本讲所有永久链接指向 `.ipynb` 原始 JSON 文件的行号（在 GitHub 上打开链接即命中对应行）。作者运行环境为旧版 torch（1.x），CPU/GPU 均可运行；三份 notebook 保存了训练计时（nopool 1.18 分钟、nneighbor 1.19 分钟、jaccard 1.32 分钟，作者 GPU），ae-deconv 的保存日志只有成本没有计时行。
>
> 另外一个「读源码要细心」的小发现：四份 notebook 的说明文字都写着「compresses **768-pixel** MNIST images down to a 7x7x8 (392 pixel) representation」（如 [ae-deconv.ipynb:L53](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv.ipynb#L53)），但 28×28 = **784** 而不是 768——是作者的笔误，四份复制传播。代码本身没有错。

## 4. 核心概念与源码讲解

### 4.1 转置卷积上采样：ae-deconv.ipynb

#### 4.1.1 概念说明

**为什么自编码器要卷积化？** u5-l1 的全连接 AE 第一步就 `view(-1, 784)` 把图像展平，像素之间的二维相邻关系全部丢失，而且每个隐单元要和全部 784 个输入相连。卷积 AE 保持特征图（feature map）形态：编码器用「卷积 + 池化」把 28×28×1 逐级压到 7×7×8，解码器的任务就是**把这个小特征图放大回 28×28×1**——「放大分辨率」这个操作叫**上采样（upsampling）**，是本讲的核心问题。

**转置卷积是什么？** `ConvTranspose2d` 常被叫作「反卷积 / deconvolution」，但它并不是卷积的逆运算，而是**卷积转置在梯度视角下的正向计算**，也叫分数步长卷积（fractionally-strided convolution）。直觉理解：普通卷积是「卷积核在输入上按步长 s 滑动、每步输出一个数」——输出比输入小；转置卷积反过来，**每个输入位置都「展开」成一个 s×s 的区域、卷积核的贡献叠加起来**——输出比输入大。它带可学习权重，所以「怎么放大」本身就是学出来的。

输出尺寸公式（对照 u3-l1 的卷积公式记）：

\[ o = (w - 1) \cdot s - 2p + k + p_{out} \]

其中 \(w\) 是输入尺寸，\(p_{out}\) 是 `output_padding`（本仓库未用，取 0）。代入本讲的 \(k=3, s=2, p=0\)：

\[ o = 2(w-1) + 3 = 2w + 1 \]

即**每次放大到「两倍加一」的奇数尺寸**——7 → 15 → 31。31 比 28 大 3，所以基准版解码器最后要裁剪居中的 28×28（`x[:, :, 2:30, 2:30]`）。若想不裁剪、精确加倍，应选 \(k=4, s=2, p=1\)：\(o = 2(w-1) - 2 + 4 = 2w\)——这个配方综合实践会用到。

#### 4.1.2 核心流程

一次前向的形状链（batch 维省略）：

| 段 | 操作 | 形状变化 |
| --- | --- | --- |
| 编码 | `conv_1`（k3,s1,p1）+ LeakyReLU | 28×28×1 → 28×28×4 |
| 编码 | `pool_1`（k2,s2） | 28×28×4 → 14×14×4 |
| 编码 | `conv_2`（k3,s1,p1）+ LeakyReLU | 14×14×4 → 14×14×8 |
| 编码 | `pool_2`（k2,s2） | 14×14×8 → **7×7×8（编码，392 维）** |
| 解码 | `deconv_1`（k3,s2,p0）+ LeakyReLU | 7×7×8 → 15×15×4 |
| 解码 | `deconv_2`（k3,s2,p0）+ LeakyReLU | 15×15×4 → 31×31×1 |
| 解码 | 裁剪 `[:, :, 2:30, 2:30]` | 31×31×1 → 28×28×1 |
| 解码 | `sigmoid` | 得到 [0,1] 概率图 |

编码器与 u3-l1 的 cnn-basic 同款套路（「卷积保尺寸 + 池化减半」），完全复用；**新的东西只在解码器**。

参数量账（转置卷积的权重形状是 \([C_{in}, C_{out}, k, k]\)，与卷积同数量级）：

\[ \underbrace{(1 \cdot 4 \cdot 9 + 4)}_{conv_1=40} + \underbrace{(4 \cdot 8 \cdot 9 + 8)}_{conv_2=296} + \underbrace{(8 \cdot 4 \cdot 9 + 4)}_{deconv_1=292} + \underbrace{(4 \cdot 1 \cdot 9 + 1)}_{deconv_2=37} = 665 \]

对比 u5-l1 全连接 AE 的 50,992 个参数，卷积化后只有 **665 个（约 1.3%）**——权值共享与局部连接的威力。而且另外三份 notebook 的参数量也恰好都是 665（4.2 节解释为什么），整组对比连参数量都控住了。

#### 4.1.3 源码精读

**① 超参数**。[ae-deconv.ipynb:L106-L110](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv.ipynb#L106-L110)：`random_seed = 456`、`learning_rate = 0.005`、`num_epochs = 10`、`batch_size = 128`。数据段与 u5-l1 完全一样：MNIST、只用 `ToTensor()`（保持 [0,1] 值域）、训练集 shuffle、**不需要验证集划分**（无调参需求，自监督任务直接看重建质量）。

**② 编码器**。[ae-deconv.ipynb:L171-L197](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv.ipynb#L171-L197)。每个层定义上方都有两行注释：一行写形状变化（如 `# 28x28x1 => 28x28x4`），一行用同填充公式反推 padding（如 [L179](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv.ipynb#L179) 的 `# (1(28-1) - 28 + 3) / 2 = 1`）。这套「注释即推导」的写法值得学习——但 4.3 节会看到它也会抄错。编码器四层与 u3-l1 讲过的完全同构，不再展开。

**③ 解码器（本讲主角）**。[ae-deconv.ipynb:L198-L213](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv.ipynb#L198-L213)：

- [L201-L205](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv.ipynb#L201-L205)：`deconv_1 = ConvTranspose2d(8, 4, kernel_size=(3,3), stride=(2,2), padding=0)`，注释标明 `7x7x8 => 15x15x4`——通道翻倍的反向（8→4），空间按 \(2 \times 6 + 3 = 15\) 放大；
- [L208-L212](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv.ipynb#L208-L212)：`deconv_2 = ConvTranspose2d(4, 1, ...)`，`15x15x4 => 31x31x1`（\(2 \times 14 + 3 = 31\)）。

注意「卷积升通道、反卷积降通道」的对偶：编码器逐层加深（1→4→8），解码器逐层变浅（8→4→1），与分类 CNN「通道数越深越多」的直觉一致。

**④ 前向传播与裁剪**。[ae-deconv.ipynb:L214-L231](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv.ipynb#L214-L231)。编码段（[L216-L223](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv.ipynb#L216-L223)）是四层的顺序堆叠；解码段（[L224-L231](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv.ipynb#L224-L231)）最关键的两行：

- [L229](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv.ipynb#L229)：`logits = x[:, :, 2:30, 2:30]`——从 31×31 裁出居中的 28×28，**丢弃的是四周各 2~3 行/列的边缘预测**；
- [L230-L231](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv.ipynb#L230-L231)：`probas = torch.sigmoid(logits)`，返回 `(logits, probas)` 二元组——谁用得上就返回谁，训练用 logits（配 with_logits 版损失），可视化用 probas。

一个值得注意的细节：[L228](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv.ipynb#L228) 在最后一层转置卷积之后也套了 `F.leaky_relu`，所以严格说 `logits` 并不是裸 logit，是过了 LeakyReLU 的值——无伤大雅（LeakyReLU 是单调的，sigmoid 之后仍是概率），但命名上有点误导，读源码时留意。

**⑤ 训练循环**。[ae-deconv.ipynb:L361-L386](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv.ipynb#L361-L386)。与 u5-l1 的 AE 训练循环逐行同构（五步范式不变），差异只有两处：

- [L369](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv.ipynb#L369)：`logits, decoded = model(features)`——**不需要展平**！全连接 AE 的 `view(-1, 784)` 在卷积版里消失了，图像以 `[B, 1, 28, 28]` 原样进模型；
- [L370](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv.ipynb#L370)：`cost = F.binary_cross_entropy_with_logits(logits, features)`——吃 logits 的数值稳定版 BCE，与 forward 返回值配套。

保存的训练日志显示成本从 0.7191（第 1 个 epoch 开头，恰在 ln 2 ≈ 0.693 附近，符合 u5-l1 讲过的「未训练网络输出 0.5 概率」预期）降到第 10 个 epoch 末段的 ≈ 0.096。

**⑥ 重建可视化**。[ae-deconv.ipynb:L415-L433](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv.ipynb#L415-L433)：两行 15 列对照图，`orig_images = features[:15]`、`decoded_images = decoded[:15]`（[L427-L428](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv.ipynb#L427-L428)），每个元素 `view((28, 28))` 还原成二维。与 u5-l1 相同，这里复用的是训练循环最后一个 batch 的残留变量。四份 notebook 的这一段完全相同，后面不再重复引。

#### 4.1.4 代码实践

**实践目标**：亲手验证 4.1.2 节的形状链，确认「31×31 裁剪回 28×28」确实发生。

**操作步骤**：

1. 打开 `ae-deconv.ipynb`，运行到模型 cell 为止（不必训练）。
2. 在模型 cell 之后新建一个 cell，粘贴下面的形状追踪代码：

```python
# 示例代码：逐层打印特征图形状（不训练，只看前向）
x = torch.randn(1, 1, 28, 28)          # 一张假图
model.eval()
with torch.no_grad():
    e1 = model.conv_1(x);   print('conv_1 :', e1.shape)      # 期望 [1, 4, 28, 28]
    e2 = model.pool_1(e1);  print('pool_1 :', e2.shape)      # 期望 [1, 4, 14, 14]
    e3 = model.conv_2(e2);  print('conv_2 :', e3.shape)      # 期望 [1, 8, 14, 14]
    e4 = model.pool_2(e3);  print('pool_2 :', e4.shape)      # 期望 [1, 8, 7, 7]
    d1 = model.deconv_1(e4);print('deconv_1:', d1.shape)     # 期望 [1, 4, 15, 15]
    d2 = model.deconv_2(d1);print('deconv_2:', d2.shape)     # 期望 [1, 1, 31, 31]
    logits, probas = model(x)
    print('logits :', logits.shape, ' probas:', probas.shape) # 期望 [1,1,28,28] ×2
```

3. 对照打印结果与 4.1.2 节的表格逐行核对。

**需要观察的现象**：`deconv_2` 输出 31×31，而模型最终输出 28×28——中间那步裁剪是 `forward` 里完成的，形状追踪代码里看不到 31 到 28 的过渡，只有直接调用 `model(x)` 才能确认。

**预期结果**：七行形状全部与注释一致（`[1,4,28,28] → [1,4,14,14] → [1,8,14,14] → [1,8,7,7] → [1,4,15,15] → [1,1,31,31] → [1,1,28,28]`）。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：把 `deconv_2` 的 `padding` 从 0 改成 1（其他不动），输出尺寸变成多少？程序还能正常训练吗？

**答案**：\(o = 2(15-1) - 2 + 3 = 29\)，输出 29×29。裁剪 `x[:, :, 2:30, 2:30]` 在 29 维上只能取到索引 2~28 共 **27** 个元素，得到 27×27 的 logits；随后 `binary_cross_entropy_with_logits(27×27, 28×28)` 形状不匹配，直接 `RuntimeError`。这说明**裁剪代码和转置卷积的尺寸配方是强耦合的**，改一个必须改另一个。

**练习 2**：编码器用 MaxPool 下采样，解码器为什么不用它的「逆操作」`MaxUnpool2d`？

**答案**：`MaxUnpool2d` 需要池化时记录的最大值位置索引，且只能恢复稀疏的激活（非最大位置是 0），表达能力有限；转置卷积自带可学习权重，「怎么放大」由训练决定。仓库选择的是「下采样用便宜的池化、上采样用可学习的转置卷积」这条折中路线——代价是转置卷积参数比反池化多，收益是解码质量。

**练习 3**：本讲卷积 AE 有 665 个参数，u5-l1 全连接 AE（瓶颈 32）有 50,992 个。两者瓶颈维度不同（392 vs 32），直接比参数量公平吗？该怎么表述才严谨？

**答案**：不完全公平——瓶颈维度不同，任务难度不同。严谨的说法是：卷积 AE 用**不到 1.3% 的参数量**把图像压到 392 维编码并成功重建（末段 BCE ≈ 0.096，甚至低于全连接版 5 epoch 的 0.0935 量级，但 epoch 数也不同，需对齐后才能下结论）。参数量差距的**原因**是明确的：局部连接（每个输出只看 3×3 邻域）+ 权值共享（同一卷积核扫全图）。

---

### 4.2 最近邻插值上采样：ae-conv-nneighbor.ipynb

#### 4.2.1 概念说明

转置卷积把「放大分辨率」和「特征变换」耦合在一个带参数的层里。`ae-conv-nneighbor` 把它们**解耦**：

1. **`F.interpolate(x, scale_factor=2, mode='nearest')` 负责放大**：无参数的纯几何操作。nearest 模式就是把每个像素复制成 2×2 的小方块——7×7 变 14×14，14×14 变 28×28，**尺寸永远精确翻倍**；
2. **同填充卷积（k3, s1, p1）负责「精修」**：尺寸不变、通道调整（8→4→1），在放大后的块状图上学习平滑与细节。

这条路线的两个直接好处：

- **无需裁剪**：仓库原版 deconv 是 7→15→31→裁剪 28，nneighbor 是 7→14→28，一步不差；
- **对输入尺寸不敏感**：同填充卷积和 ×2 插值都不写死具体尺寸，换成任意能被 4 整除的边长（如 32×32）结构照常工作；而 deconv 版的裁剪 `[2:30]` 是为 28×28 量身定做的，换尺寸立刻崩（综合实践会实测这一点）。

一个仓库外但值得知道的背景知识（本仓库未讨论）：转置卷积当卷积核大小不能被步长整除时（本例 k=3, s=2），相邻输出的 receptive field 重叠不均匀，生成图像容易出现**棋盘伪影（checkerboard artifacts）**；文献中两种经典对策正是（a）改用能被步长整除的卷积核（如 k=4, s=2），（b）**用插值上采样 + 普通卷积替代转置卷积**——即本 notebook 的做法（参见 Odena et al. 2016 的讨论）。仓库这组 notebook 恰好把两种方案各实现了一份。

#### 4.2.2 核心流程

解码器形状链（编码器与 4.1 节完全相同）：

| 操作 | 形状变化 |
| --- | --- |
| 编码（同 ae-deconv） | 28×28×1 → 7×7×8 |
| `F.interpolate(scale_factor=2, nearest)` | 7×7×8 → 14×14×8 |
| `conv_3`（k3,s1,p1）+ LeakyReLU | 14×14×8 → 14×14×4 |
| `F.interpolate(scale_factor=2, nearest)` | 14×14×4 → 28×28×4 |
| `conv_4`（k3,s1,p1）+ LeakyReLU | 28×28×4 → 28×28×1 |
| `sigmoid` | [0,1] 概率图，**全程无裁剪** |

参数量：`conv_3` 是 \(8 \cdot 4 \cdot 9 + 4 = 292\)，`conv_4` 是 \(4 \cdot 1 \cdot 9 + 1 = 37\)，加上编码器的 336，**总计仍是 665**——与 deconv 版分毫不差（转置卷积和普通卷积的参数量公式同形）。这使得「同参数量下比较两种上采样路线」成为一组干净的对比。

#### 4.2.3 源码精读

**① 超参数——一个不和谐音**。[ae-conv-nneighbor.ipynb:L106-L110](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-conv-nneighbor.ipynb#L106-L110)：`random_seed = 123`、`learning_rate = 0.05`——学习率是另外三份（0.005）的 **10 倍**，种子也不同。所以它末段成本更低（≈0.082 vs ≈0.096）**不能**直接归因于「插值上采样更优」，学习率不同、随机初始化不同都是混杂因素。u3-l5 讲过「比较模型须对齐配方」，这里是又一个现成例子——严格结论要靠 4.2.4 节的实践自己补实验。

**② 解码器定义**。[ae-conv-nneighbor.ipynb:L204-L220](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-conv-nneighbor.ipynb#L204-L220)：

- [L203-L209](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-conv-nneighbor.ipynb#L203-L209)：`conv_3 = Conv2d(8, 4, k3, s1, p1)`。注意 [L203](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-conv-nneighbor.ipynb#L203) 的注释写着 `# 14x14x8 => 14x14x8`——**笔误**：`out_channels=4`，实际输出是 14×14×4（下一行注释 `# 14x14x4 => 28x28x4` 也印证了通道数是 4）。注释抄错了，代码是对的；
- [L214-L220](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-conv-nneighbor.ipynb#L214-L220)：`conv_4 = Conv2d(4, 1, k3, s1, p1)`，28×28×4 → 28×28×1。

两处 `# Unpool` 注释（[L200-L202](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-conv-nneighbor.ipynb#L200-L202)、[L212-L213](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-conv-nneighbor.ipynb#L212-L213)）标注了「放大发生在哪」——对应 forward 里的 `F.interpolate`，而不是一个真正的层（`__init__` 里没有任何 interpolate 相关定义，因为插值无参数、不需要注册）。

**③ 前向传播**。[ae-conv-nneighbor.ipynb:L221-L238](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-conv-nneighbor.ipynb#L221-L238)。解码段三步一组、重复两组：

- [L232](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-conv-nneighbor.ipynb#L232) 与 [L235](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-conv-nneighbor.ipynb#L235)：`x = F.interpolate(x, scale_factor=2, mode='nearest')`——先放大；
- 紧跟 `conv_3` / `conv_4`——再精修；
- [L237-L238](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-conv-nneighbor.ipynb#L237-L238)：`logits = F.leaky_relu(x)`、`probas = torch.sigmoid(logits)`——与 deconv 版一样「logits 先过 LeakyReLU」，返回二元组。

**④ 训练循环与日志分母**。[ae-conv-nneighbor.ipynb:L380-L404](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-conv-nneighbor.ipynb#L380-L404)，损失在 [L389](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-conv-nneighbor.ipynb#L389)（`binary_cross_entropy_with_logits`）。一个细微差别：它的日志分母用 `len(train_loader)`（[L397](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-conv-nneighbor.ipynb#L397)，打印 469 = ⌈60000/128⌉），而 deconv 版用 `len(train_dataset)//batch_size`（打印 468）。u4-l1 讲过这两个口径的差别：实际批数是 469（含一个 96 张的尾批），整除是 468——无关痛痒，但四份 notebook 里只有这份写对了。

保存的日志显示末段成本 ≈ 0.082，训练总时长 1.19 分钟。

#### 4.2.4 代码实践

**实践目标**：对齐学习率与种子，做一次真正受控的「插值 vs 转置卷积」对比。

**操作步骤**：

1. 复制 `ae-conv-nneighbor.ipynb`（如另存为 `ae-conv-nneighbor-aligned.ipynb`），把 [L107-L108](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-conv-nneighbor.ipynb#L107-L108) 的超参改成 `random_seed = 456`、`learning_rate = 0.005`，与 `ae-deconv` 完全一致。
2. 训练相同 epoch 数（10），记录每个 epoch 的成本。
3. 与 `ae-deconv` 的保存日志（末段 ≈ 0.096）并排画两条成本曲线，并目视对比两组重建图。
4. 进阶（可选）：把两处 `mode='nearest'` 改成 `mode='bilinear'`（较新版本 PyTorch 需同时传 `align_corners=False`），再训练一次，观察重建笔画是否更平滑。

**需要观察的现象**：对齐配方后两条成本曲线是否仍然分出高下；`bilinear` 版本的重建与 `nearest` 版本的差异（nearest 的复制块 vs bilinear 的线性过渡）。

**预期结果**：待本地验证。两种上采样在同等参数量下表现接近（文献经验是各有胜负，差异小于调参带来的波动）；插值模式的差异主要影响重建的「块感」程度。

#### 4.2.5 小练习与答案

**练习 1**：`conv_3` 上方注释声称 `14x14x8 => 14x14x8`，实际输出通道数是多少？如何一行代码验证？

**答案**：4（`out_channels=4`）。验证：`print(model.conv_3(torch.randn(1, 8, 14, 14)).shape)` 应输出 `[1, 4, 14, 14]`（示例代码）。这是本讲第二处「注释笔误、代码正确」的例子。

**练习 2**：为什么这条路线的解码器从头到尾不需要任何裁剪，而 deconv 版需要？

**答案**：尺寸链条精确可逆。编码是「同填卷积（保尺寸）+ ×½ 池化」，28→14→7；解码是「×2 插值 + 同填卷积（保尺寸）」，7→14→28，每一步的输出尺寸都有封闭公式且无取整损失。deconv 版的 \(o = 2w+1\) 天生产生奇数尺寸，只能靠裁剪纠偏。

**练习 3**：`F.interpolate` 没有可学习参数，为什么解码器仍算「可学习的上采样器」？

**答案**：插值只负责确定性的几何放大（复制像素），学习能力全部集中在插值之后的卷积里——卷积在放大后的图上调整通道、平滑块状边界、恢复细节。整个「放大 + 精修」的组合是可学习的，只是可学习部分与几何部分分工明确。

---

### 4.3 无 pooling 结构：ae-deconv-nopool.ipynb

#### 4.3.1 概念说明

前两版的编码器都用 `MaxPool2d` 做下采样。池化的批评者说它太「硬」：无参数、无学习，逐 2×2 窗口机械取 max，还丢弃了窗口内的其余信息。`ae-deconv-nopool` 给出第三种编码器设计：**删掉池化，把下采样交给卷积的步长**——`stride=2` 的卷积在算特征的同时把分辨率减半，下采样本身也变成可学习的操作。这与 u3-l3 讲过的 All-Convolutional 网络用步长卷积替代池化是同一个思想。

结构上的连锁反应：

- 编码器从「4 层（2 卷积 + 2 池化）」缩成「2 层卷积」；
- 解码器仍用两层转置卷积，但**第二层的 padding 改为 1**，输出 29×29 而不是 31×31，裁剪也从居中 `[2:30]` 换成切边 `[:-1, :-1]`——又一次印证「转置卷积配方与裁剪强耦合」；
- `forward` 只返回单个 sigmoid 张量（不是二元组），训练损失相应换成**吃概率的** `F.binary_cross_entropy`——forward 输出形式与损失版本必须配套，这是 u5-l1 讲过、本讲三份 notebook 反复演练的搭配规则。

#### 4.3.2 核心流程

形状链（注意第一步的注释与实际不符）：

| 操作 | 注释声称 | 实际形状 |
| --- | --- | --- |
| `conv_1`（k3, **s2**, p0）+ LeakyReLU | 28×28×1 → 14×14×4 | → **13×13×4** |
| `conv_2`（k3, s2, **p1**）+ LeakyReLU | 14×14×4 → 7×7×8 | 13×13×4 → **7×7×8** |
| `deconv_1`（k3, s2, p0）+ LeakyReLU | 7×7×8 → 15×15×4 | 7×7×8 → 15×15×4 ✓ |
| `deconv_2`（k3, s2, **p1**）+ LeakyReLU | 15×15×4 → 29×29×1 | 15×15×4 → 29×29×1 ✓ |
| 裁剪 `[:, :, :-1, :-1]` + sigmoid | — | 29×29×1 → **28×28** |

用 u3-l1 公式核算 `conv_1`：\(o = \left\lfloor \frac{28 - 3 + 0}{2} \right\rfloor + 1 = 12 + 1 = 13\)，不是注释写的 14。注释的推导是「想要 o=14 反解 p = (26-28+3)/2 = 0.5，floor 取 0」——目标尺寸本身就取不到（p 必须是整数，k=3、s=2 时 28 只能到 13 或 15）。好在 `conv_2` 从 13 出发取整后恰好还是 7×7×8，整条链没有崩，纯属「歪打正着」。

三版编码器对照：

| | ae-deconv / nneighbor | ae-deconv-nopool |
| --- | --- | --- |
| 下采样方式 | 卷积(s1,保尺寸) + MaxPool | 步长卷积(s2) |
| 编码器层数 | 4（2 conv + 2 pool） | 2（2 conv） |
| 28×28 → 7×7 路径 | 28→14→7 | 28→**13**→7 |
| 下采样可学习 | 池化部分不可学习 | 全程可学习 |

#### 4.3.3 源码精读

**① 步长卷积编码器**。[ae-deconv-nopool.ipynb:L173-L186](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv-nopool.ipynb#L173-L186)：

- [L173-L179](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv-nopool.ipynb#L173-L179)：`conv_1 = Conv2d(1, 4, kernel_size=(3,3), stride=(2,2), padding=0)`，注释 `# 28x28x1 => 14x14x4` 与 `# floor((2(14-1) - 28 + 3) / 2) = 0`——上面已核算，实际是 13×13×4；
- [L181-L186](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv-nopool.ipynb#L181-L186)：`conv_2 = Conv2d(4, 8, k3, s2, p1)`，13×13×4 → 7×7×8（\(\lfloor (13+2-3)/2 \rfloor + 1 = 7\)）。

**② 解码器与不同的裁剪**。[ae-deconv-nopool.ipynb:L190-L202](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv-nopool.ipynb#L190-L202)：`deconv_1` 与基准版相同（p0，7→15）；`deconv_2` 在 [L202](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv-nopool.ipynb#L202) 改为 `padding=1`，\(o = 2(15-1) - 2 + 3 = 29\)。于是 forward 里的裁剪换成 [L217](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv-nopool.ipynb#L217) 的 `x = x[:, :, :-1, :-1]`——切掉最后一行一列（29→28），而不是基准版的居中裁剪（31→28）。

**③ 单返回值的前向**。[ae-deconv-nopool.ipynb:L204-L219](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv-nopool.ipynb#L204-L219)：编码段只有 4 行（2 卷积 + 2 激活，无池化），结尾 [L218](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv-nopool.ipynb#L218) 直接 `x = torch.sigmoid(x)` 并 `return x`——**只返回概率图**。

**④ 训练损失的搭配**。[ae-deconv-nopool.ipynb:L360-L384](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv-nopool.ipynb#L360-L384)：[L368-L369](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv-nopool.ipynb#L368-L369) 是 `decoded = model(features)` + `cost = F.binary_cross_entropy(decoded, features)`——返回概率就配吃概率的 BCE；对照基准版 [L369-L370](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv.ipynb#L369-L370) 返回 logits 配 with_logits。两份 notebook 演示了同一规则的两种正确写法；错误的搭配（概率喂 with_logits 或 logits 喂 BCE）不会报错但结果全错，u5-l1 已经强调过。

保存的日志显示末段成本 ≈ 0.103，与基准版（≈0.096）几乎打平——在本任务的小数据上，「池化 vs 步长卷积」的差异远小于随机波动。超参与基准版完全一致（seed 456 / lr 0.005，[L106-L110](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv-nopool.ipynb#L106-L110)），这组对比是对齐了配方的。

#### 4.3.4 代码实践

**实践目标**：用一行代码裁决「注释 14 vs 实际 13」的争议。

**操作步骤**：

1. 打开 `ae-deconv-nopool.ipynb`，在 Imports 运行之后新建 cell，粘贴：

```python
# 示例代码：验证步长卷积的真实输出尺寸
conv = torch.nn.Conv2d(1, 4, kernel_size=3, stride=2, padding=0)
print(conv(torch.randn(1, 1, 28, 28)).shape)
```

2. 运行并记录输出。
3. 再验证整条编码链：把 `conv_1`、`conv_2` 换成 notebook 里定义的真实层（运行到模型 cell 后直接 `model.conv_1`、`model.conv_2`），打印两层输出的形状。

**需要观察的现象**：第一个 cell 输出 `torch.Size([1, 4, 13, 13])` 而不是注释里的 14；第二个 cell 中 `model.conv_2` 的输出是 `[1, 8, 7, 7]`——尽管第一层「错了 1 个像素」，第二层取整后仍回到 7。

**预期结果**：`[1, 4, 13, 13]` 与 `[1, 8, 7, 7]`。待本地验证。结论：形状注释是作者手算的，可信但不可迷信——尤其涉及取整时，永远可以用一行前向传播复核（这正是 4.1.4 节形状追踪实践的轻量版）。

#### 4.3.5 小练习与答案

**练习 1**：如果坚持要让第一步输出恰好 14×14（k=3、s=2 不变），padding 该取多少？可行吗？

**答案**：由 \(p = \frac{s(o-1) - w + k}{2} = \frac{26 - 28 + 3}{2} = 0.5\)，不是整数——k=3、s=2 时 28 到 14 无解。要想精确减半，可换 \(k=2, s=2, p=0\)（\(o = \lfloor 26/2 \rfloor + 1 = 14\)）或 \(k=4, s=2, p=1\)（\(o = \lfloor 26/2 \rfloor + 1 = 14\)）。这与 4.1 节「k=4, s=2, p=1 精确加倍」是同一条配方的正反两用。

**练习 2**：为什么这个版本的 `deconv_2` 要把 padding 改成 1，而不是照抄基准版的 0？

**答案**：没有必然原因，只是作者选择了另一个可行的尺寸组合：p=1 时输出 29×29，裁剪 `[:-1, :-1]` 切一行一列即可回到 28；照抄 p=0 会得到 31×31，同样可以用居中裁剪 `[2:30]` 回到 28（基准版就是这么做的）。两种都正确，共同点是**转置卷积的 (k, s, p) 与后面的裁剪方式必须一起设计**。

**练习 3**：这个 notebook 的 `forward` 返回单个张量，基准版返回二元组。两种设计各有什么好处？

**答案**：二元组的好处是调用方按需取用——训练拿 logits（配数值稳定的 with_logits 损失）、可视化拿 probas，避免外部反复手写 sigmoid；单返回值的好处是接口更简单、训练损失与可视化共用同一输出（这里直接用概率版 BCE）。选择哪种是风格问题，**关键是返回值类型与损失函数版本要配套**。

---

### 4.4 Jaccard 重建损失：ae-deconv-jaccard.ipynb

#### 4.4.1 概念说明

前三节变的是**结构**，这一节变的是**损失**：重建损失不是只有 BCE/MSE。Jaccard 距离是集合相似度指标 Jaccard index（即 IoU，交并比）在连续值上的推广。对两个非负向量 \(x, \hat{x}\)：

\[ J(x, \hat{x}) = 1 - \frac{\sum_i \min(x_i, \hat{x}_i)}{\sum_i \max(x_i, \hat{x}_i)} \]

直觉：把每个像素对 \((x_i, \hat{x}_i)\) 的较小值累加当作「交集」、较大值累加当作「并集」，距离 = 1 − 交并比。性质：

- 取值范围 [0, 1]：完全相等时分子分母相同，\(J=0\)；完全错开（逐像素一大一小）时趋于 1；
- 若 \(x, \hat{x}\) 都是 0/1 二值向量，\(\sum \min\) 就是逐位与（交集大小）、\(\sum \max\) 是逐位或（并集大小），公式退化为标准 IoU 距离；
- 它衡量的是**整体重叠比例**而非逐点误差的平方和——两张「整体形状对、亮度略有偏差」的图，MSE 会惩罚每一个偏差像素，Jaccard 只关心总量比例。

像素值在 [0,1]（ToTensor 的 features、sigmoid 的 decoded），min/max 良定义，所以这套损失与仓库的数据管线天然兼容。

梯度行为与 BCE 有本质差异（本段是原理分析，仓库未展开）：\(\min/\max\) 的次梯度每次只流给「较小/较大」的一方，且分母是整批像素的总和、量级很大，所以单像素的梯度非常小。配上 Adam 的自适应步长（按梯度量级归一化），训练仍然能进行——这解释了为什么该 notebook 沿用 lr=0.005 也能把 Jaccard 从 1.0 降到 ≈0.18。

#### 4.4.2 核心流程

这份 notebook 是教科书级的**受控实验**设计：

1. 模型与 `ae-deconv` **逐字相同**（同样的编码器、解码器、裁剪、返回二元组）；
2. 训练循环只改了两行：注释掉 BCE、换成 `continuous_jaccard`；
3. 超参相同（seed 456 / lr 0.005 / 10 epochs / batch 128）。

于是「末段成本不同」的唯一原因就是损失函数。流程上唯一要注意的数据流：`model(features)` 返回 `(logits, decoded)`，喂给 `continuous_jaccard(features, decoded)` 的是 **sigmoid 之后的概率**——与 4.4.1 节「值域 [0,1]」的前提呼应。

> 顺带一个「读说明要细心」的例子：[L56](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv-jaccard.ipynb#L56) 的说明写着「This convolutional **VAE** uses a continuous Jaccard distance」——但这份 notebook 是普通 AE，不是 VAE（VAE 是下一讲 u5-l3 的内容），复制模板时忘了改称呼。公式本身（[L57-L58](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv-jaccard.ipynb#L57-L58)）与实现一致。

#### 4.4.3 源码精读

**① `continuous_jaccard` 函数**。[ae-deconv-jaccard.ipynb:L90-L101](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv-jaccard.ipynb#L90-L101)，实现只有四步：

- [L96](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv-jaccard.ipynb#L96)：`c = torch.cat((x.view(-1).unsqueeze(1), y.view(-1).unsqueeze(1)), dim=1)`——把两个张量各展平成一列再按列拼接成 \([N, 2]\) 的配对矩阵，每行是一对 \((x_i, y_i)\)。注意 `view(-1)` 展平的是**整个 batch**（形状 `[B,1,28,28]` → `[B·784]`），所以算出的是**整批一个标量**，不是逐样本平均——后面练习 3 会讨论这个细节；
- [L98](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv-jaccard.ipynb#L98)：`numerator = torch.sum(torch.min(c, dim=1)[0])`——逐行取较小值再全局求和，即 \(\sum_i \min(x_i, y_i)\)；
- [L99](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv-jaccard.ipynb#L99)：`denominator = torch.sum(torch.max(c, dim=1)[0])`——同理取较大值求和；
- [L101](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv-jaccard.ipynb#L101)：`return 1. - numerator/denominator`。

这四行全部由可微操作组成（min/max 可求次梯度、sum/cat 可微），所以 `cost.backward()` 能直接反传——自定义损失不需要继承任何类，一个返回标量张量的普通函数就够，这是很有用的工程知识。

**② 自带的最小示例**。[L105-L110](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv-jaccard.ipynb#L105-L110) 用两个 6 维向量演示调用，保存输出为 `tensor(0.6275)`。手工核算（练习 1 会让你自己算）：逐对 min 是 \((1,2,3,4,5,4)\) 和为 19，逐对 max 是 \((7,8,9,10,11,6)\) 和为 51，\(1 - 19/51 = 0.6275\) ✓。**带可验证示例的损失函数**是好习惯，值得效仿。

**③ 训练循环的两行差异**。[ae-deconv-jaccard.ipynb:L487-L512](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv-jaccard.ipynb#L487-L512)：

- [L496](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv-jaccard.ipynb#L496)：`#cost = F.binary_cross_entropy_with_logits(logits, features)`——原损失被注释保留，方便对照与切换；
- [L497](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv-jaccard.ipynb#L497)：`cost = continuous_jaccard(features, decoded)`——注意参数顺序是 (原图, 重建)，`decoded` 是概率。

**④ 结果怎么看**。保存的日志显示 Jaccard 成本从 1.0 附近降到末段 ≈ 0.18，训练总时长 1.32 分钟（另两份有计时的同类 notebook 为 1.18/1.19 分钟——cat + min/max + 两次全局求和的开销略高于一个融合的 BCE 核）。**不能**拿 0.18 与基准版的 0.096 比大小：前者是 Jaccard 距离、后者是 BCE，量纲不同；公平的比较是训练完后用同一指标各自评估（练习 2）。

#### 4.4.4 代码实践

**实践目标**：理解 Jaccard 损失的「批级聚合」特性，并做一个跨损失的一致评估。

**操作步骤**：

1. 打开 `ae-deconv-jaccard.ipynb`，运行函数定义 cell（[L87-L110](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv-jaccard.ipynb#L87-L110)），先手工算一遍示例的 19/51，再运行核对。
2. 新建 cell，构造两张「同形状不同亮度」的图，对比 MSE 与 Jaccard 的评价（示例代码）：

```python
# 示例代码：感受两种度量的差异
a = torch.zeros(1, 1, 28, 28); a[:, :, 10:18, 10:18] = 1.0   # 方块
b = 0.8 * a                                                   # 同形状、整体暗 20%
print('Jaccard:', continuous_jaccard(a, b).item())            # 期望 1 - 0.8/1.0 = 0.2
print('MSE   :', torch.nn.functional.mse_loss(b, a).item())   # 每像素差 0.2 → 0.04
```

3. 运行完整训练后，把 `ae-deconv`（BCE 训练）与 `ae-deconv-jaccard`（Jaccard 训练）的重建图各取 15 张，**统一用 `continuous_jaccard` 评估**同一批测试图像，比较两个数。

**需要观察的现象**：步骤 2 中整体暗 20% 的图 Jaccard 恰为 0.2（只关心总量比例），MSE 则是 0.04（逐像素平方）；步骤 3 中两种损失训出的模型在同一指标下的相对高低。

**预期结果**：步骤 2 的两个数值按公式可直接推出；步骤 3 待本地验证——文献经验是用「与评估目标一致的损失」训练通常在该指标上略优。

#### 4.4.5 小练习与答案

**练习 1**：手算示例：\(x = (7,2,3,4,5,6)\)，\(y = (1,8,9,10,11,4)\)，求 \(J(x,y)\)。

**答案**：逐对 min = \((1,2,3,4,5,4)\)，和为 19；逐对 max = \((7,8,9,10,11,6)\)，和为 51；\(J = 1 - 19/51 = 1 - 0.3725 = 0.6275\)，与 notebook 保存输出 `tensor(0.6275)` 一致。

**练习 2**：为什么说「jaccard 版末段成本 0.18 > deconv 版 0.096，所以 BCE 更好」是不成立的论证？

**答案**：两个数字来自不同的损失函数，量纲不同（Jaccard 是比例距离 [0,1]，BCE 是平均对数损失，下界 0 但尺度不同），直接比大小没有意义。正确做法是**训练后用同一指标评估两批重建**：比如都算测试集上的 Jaccard 距离，或都算 BCE，甚至直接目视对比重建图。这也是 u4-l4「报告成绩前先统一口径」的又一次应用。

**练习 3**：`continuous_jaccard` 用 `view(-1)` 把整个 batch 展平成一个长向量再算交并比。改成「逐样本 Jaccard 再对 batch 求平均」该怎么写？两者有何差别？

**答案**：把 [L96](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-deconv-jaccard.ipynb#L96) 的展平改成保留 batch 维（如 `x.view(x.size(0), -1)`），拼接成 `[B, 784, 2]`，对 `dim=1` 做 min/max 并沿同一维求和，得到 `[B]` 的逐样本距离，最后 `.mean()`。差别：批级版本是「整批像素总量的交并比」，一张较暗的样本会与较亮的样本在分母上互相影响；逐样本版本每个样本独立计分再平均，语义更接近「平均每张图的重叠度」，且对 batch 内样本分布不敏感。哪个更合理取决于任务，但要知道自己用的是哪个。

---

## 5. 综合实践

**任务**：基于 `ae-conv-nneighbor.ipynb` 把模型改造成支持 **56×56 输入**的三级编解码网络（综合运用本讲全部四条知识：形状公式、插值上采样、转置卷积配方、裁剪耦合），并在同一输入下对比 nearest 与 deconv 两种解码器。

### 步骤 1：先做一个「不改模型」的预言实验

把数据 transform 加上 `Resize`（示例代码）：

```python
transform=transforms.Compose([
    transforms.Resize((32, 32)),   # 28×28 → 32×32
    transforms.ToTensor()])
```

先**笔算**再验证：32×32 输入下，`ae-conv-nneighbor` 的形状链是 32→16→8（编码）→ 16 → 32（解码），同填卷积全程兼容，**模型一行不用改**（只有可视化 cell 的 `image_width = 28` 要改成 32，否则 `view((28, 28))` 会报形状错误）；而 `ae-deconv` 的解码器输出 8→17→35，裁剪 `[2:30]` 得到 28×28 的 logits，与 32×32 的 target 形状不符，训练 cell 会直接 `RuntimeError`。这一步用最小成本验证 4.2 节「插值路线对尺寸不敏感、裁剪路线与尺寸强耦合」的结论。

### 步骤 2：改造 nneighbor 到 56×56（主体任务）

56 = 7 × 8，编码需要三级「卷积 + 池化」（56→28→14→7），解码对称地三级「插值 + 卷积」。修改模型 cell（示例代码，只列 `__init__` 新增部分）：

```python
# 编码器：56→28→14→7，通道 1→4→8→16
self.conv_3 = torch.nn.Conv2d(8, 16, kernel_size=(3, 3), stride=(1, 1), padding=1)
self.pool_3 = torch.nn.MaxPool2d(kernel_size=(2, 2), stride=(2, 2))

# 解码器：7→14→28→56，通道 16→8→4→1
self.up_conv_1 = torch.nn.Conv2d(16, 8, kernel_size=(3, 3), stride=(1, 1), padding=1)
self.up_conv_2 = torch.nn.Conv2d(8, 4, kernel_size=(3, 3), stride=(1, 1), padding=1)
self.up_conv_3 = torch.nn.Conv2d(4, 1, kernel_size=(3, 3), stride=(1, 1), padding=1)
```

`forward` 解码段改成三组「`F.interpolate(scale_factor=2, mode='nearest')` + 卷积 + LeakyReLU」（最后一组不加激活、直接 sigmoid，照抄原版收尾）。数据 transform 用 `transforms.Resize((56, 56))`，可视化 cell 的 `image_width` 改 56。训练 5~10 个 epoch。

### 步骤 3：同尺寸的 deconv 解码器对照版

复制一份，解码器换成三层转置卷积。这里用 4.1 节推导的**精确加倍配方** \(k=4, s=2, p=1\)（\(o = 2w\)）：7→14→28→56，全程无裁剪：

```python
# 示例代码：k=4, s=2, p=1 的转置卷积，输出恰好两倍
self.up_deconv_1 = torch.nn.ConvTranspose2d(16, 8, kernel_size=(4, 4), stride=(2, 2), padding=1)
self.up_deconv_2 = torch.nn.ConvTranspose2d(8, 4, kernel_size=(4, 4), stride=(2, 2), padding=1)
self.up_deconv_3 = torch.nn.ConvTranspose2d(4, 1, kernel_size=(4, 4), stride=(2, 2), padding=1)
```

### 需要观察的现象与预期结果

1. 步骤 1 的「预言」是否应验（nneighbor 直接能跑、deconv 报形状错误）；
2. 两版 56×56 模型的形状追踪输出（用 4.1.4 的实践代码）：nearest 版与 deconv 版都应无裁剪、输出严格 `[B, 1, 56, 56]`；
3. 各自末段成本曲线与重建图：56×56 的数字笔画更粗（Resize 放大了原图），对比两种解码器的重建清晰度与有无块状/棋盘状伪影；
4. 参数量对比（`sum(p.numel() for p in model.parameters())`）：nearest 版与 deconv 版应只差最后几层的 bias 数量级，主体相当。

训练与重建质量**待本地验证**；形状推导部分可以纯靠公式确认，不依赖运行。

## 6. 本讲小结

- **卷积化 AE** 用特征图保留空间结构：编码器「同填卷积 + MaxPool」把 28×28×1 压到 7×7×8（392 维），四份 notebook 参数量全部只有 665（全连接版的 1.3%），因为局部连接 + 权值共享。
- **转置卷积**（`ConvTranspose2d`）输出 \(o = (w-1)s - 2p + k\)；仓库取 k3/s2/p0 得 \(2w+1\) 的奇数尺寸，所以解码器末尾必须裁剪（基准版居中 `[2:30]`、nopool 版切边 `[:-1,:-1]`）——**裁剪与转置卷积配方强耦合**；想精确加倍用 k4/s2/p1。
- **插值 + 卷积路线**（`F.interpolate(nearest)` + 同填卷积）把「放大」与「精修」解耦：尺寸精确翻倍、全程无裁剪、对输入尺寸不敏感（换 32×32 不改一行模型代码），同参数量下与转置卷积路线相当。
- **无 pooling 版**用步长卷积（s=2）替代池化，下采样也可学习；其注释里的 `14x14x4` 实际是 13×13×4（\(\lfloor 25/2 \rfloor + 1 = 13\)）——形状注释是手算的，取整处要用一行前向传播复核。
- **forward 返回值与损失版本必须配套**：返回 logits 配 `binary_cross_entropy_with_logits`（数值稳定），返回概率配 `binary_cross_entropy`；四份 notebook 演示了全部两种正确搭配。
- **连续 Jaccard 距离** \(1 - \sum\min / \sum\max\) 是 IoU 的连续推广，衡量整体重叠比例；实现只需 min/max/sum 四行可微操作；它的成本数字与 BCE 量纲不同不可直接比，且当前实现是批级聚合而非逐样本平均。
- 对比实验要对齐配方：nneighbor 版的 lr（0.05）是其他三份的 10 倍，其更低的末段成本不能归因于上采样路线本身。

## 7. 下一步学习建议

- **下一讲 [u5-l3 变分自编码器 VAE](u5-l3-variational-autoencoder.md)**：在本讲的卷积 AE 骨架上加「重参数化技巧」就得到卷积 VAE（`ae-conv-var.ipynb` 的编码器/解码器与本讲的 `ae-deconv` 高度同构），编码器输出从特征图变成 μ 与 σ 两组特征图——先把本讲的形状链吃透，VAE 的源码只剩「采样」一处新东西。
- **同目录的放大版实战**：`ae-conv-nneighbor-celeba.ipynb`（64×64 人脸）与 `ae-conv-nneighbor-quickdraw-1.ipynb` 把本讲的插值路线用到真实彩色/更大图像上，是检验「这条路对尺寸不敏感」的现成样本。
- **回顾 [u3-l3 VGG、NiN 与 All-Conv](u3-l3-vgg-nin-allconv.md)**：步长卷积替代池化（本讲 4.3 节）与 GAP 替代全连接头同属「用更优算子替换机械操作」的设计潮流，两讲对照阅读能看出这一思想的普适性。
- 若想深挖棋盘伪影与「resize-conv vs transposed-conv」的对比实验，可阅读 Odena et al. 2016（*Deconvolution and Checkerboard Artifacts*）——本讲 4.2 节的两条路线正是该文讨论的两种方案。
