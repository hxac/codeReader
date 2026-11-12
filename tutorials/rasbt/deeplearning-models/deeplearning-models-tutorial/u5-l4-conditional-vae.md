# 条件变分自编码器 CVAE

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 **CVAE 与 VAE 的唯一区别**：u5-l3 的 VAE 学的是无条件的 \(p(x|z)\)，采样时「出什么数字全凭运气」；CVAE 把类别标签 y 一起喂进 encoder 和 decoder，学的是**条件分布** \(p(x|y,z)\)，生成时可以**指定要哪个数字**。
2. 理解**标签 one-hot 拼接**这个最朴素的条件化手法：`to_onehot` + `torch.cat` 两行代码，如何把「数字是几」变成网络输入向量里的 10 个维度，以及为什么不能用原始整数标签直接拼。
3. 掌握**条件解码器**的两种实现并加以区分：标签只进 decoder 输入（`ae-cvae_no-out-concat`），还是让 decoder 额外输出 10 维「标签预测头」并参与重建损失（`ae-cvae`）——两种写法只差一层 `Linear` 的输出维度和损失的目标向量。
4. 读懂**卷积版 CVAE**（`ae-cnn-cvae`）：条件从「向量拼接」变成「通道拼接」——10 个 one-hot 值广播成 10 张与输入图像同尺寸的掩码平面，与图像在通道维拼成 11 通道输入；decoder 也对称地输出 11 通道。
5. 会**按条件采样生成**：`torch.randn` 采 z + 手工指定标签 + 只调 `model.decoder(z, labels)`，并完成本讲综合实践——**固定同一个 z、遍历 0–9 十个标签**，观察「同一笔迹风格被渲染成十个不同数字」。

本讲是 u5-l3（VAE）的直接续篇：重参数化、KL + 重建损失、从先验采样这三件 VAE 的核心资产全部原样继承，一行都不用改；新增的只有「条件怎么进网络」这一件事。

## 2. 前置知识

- **VAE 三件套**（u5-l3）：① 重参数化 \(z = \mu + \epsilon\sigma\) 让采样可导；② 损失 = 逐像素 BCE（重建）+ 闭式 KL 项 \(\frac{1}{2}\sum(\mu^2 + \sigma^2 - \ln\sigma^2 - 1)\)；③ 训练后从 \(N(0, I)\) 采样、只喂 decoder 即可生成图像。本讲三份 notebook 的 `reparameterize` 与损失代码与 u5-l3 **逐字相同**。
- **one-hot 编码**：类别标签 7 不是数字 7，而是一个只有第 7 位为 1 的 10 维单位向量 \(e_7\)。u2-l2 里标签以 int64 整数喂给 `cross_entropy`；本讲标签要**作为输入特征**拼进网络，必须先 one-hot 化，原因见 4.1.1。
- **`Tensor.scatter_`**：按索引在指定维度上原地写入值。`labels_onehot.scatter_(1, labels.view(-1,1), 1)` 的语义是：对每一行（dim=0 方向遍历），把 1 写进第 1 维上索引等于该行标签值的那个位置。
- **转置卷积输出尺寸**（u5-l2）：\(o = (w-1)\cdot s - 2p + k\)，卷积版 CVAE 的 decoder 上采样链要用它手工推导。
- **NCHW 与通道维拼接**（u3-l1）：卷积网络输入形状是 \((N, C, H, W)\)，`torch.cat(..., dim=1)` 沿通道维拼接是「给卷积网络加额外输入信息」的标准姿势。
- **五步训练循环**（u2-l2）：forward → loss → `zero_grad` → `backward` → `step`。autoencoder 系列一如既往**不用** helper_train 的共享训练函数（u4-l4），因为损失需要 `z_mean` / `z_log_var`，只能手写循环。
- **读源码两处老坑**（u5-l2、u5-l3 反复出现）：① 三份 notebook 的说明文字都写着「compresses **768-pixel** MNIST images」，实际 28×28=**784**，代码（`num_features = 784`）才是对的；② `ae-cvae` 与 `ae-cvae_no-out-concat` 的 device 写的是 `cuda:1`，**单卡机器会报 invalid device ordinal**，跑之前要改成 `cuda:0`。

## 3. 本讲源码地图

README 的「Conditional Variational Autoencoders」小节（[README.md:L225-L232](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/README.md#L225-L232)）收录了 4 份 CVAE notebook，本讲精读其中 3 份：

| 文件 | 角色 |
| --- | --- |
| `pytorch_ipynb/autoencoder/ae-cvae.ipynb` | **全连接 CVAE（标签参与重建损失）**：794→500→(μ, logσ²)→35→45→500→794，4.1、4.2、4.4 节主线。 |
| `pytorch_ipynb/autoencoder/ae-cvae_no-out-concat.ipynb` | **全连接 CVAE（标签不参与重建损失）**：与上者仅差 `linear_4` 输出维度（794 vs 784）和损失目标，4.2 节做受控对比。 |
| `pytorch_ipynb/autoencoder/ae-cnn-cvae.ipynb` | **卷积 CVAE（标签参与重建损失）**：条件通道化注入 + 转置卷积解码，4.3 节主线。 |
| `pytorch_ipynb/autoencoder/ae-cnn-cvae_no-out-concat.ipynb` | 卷积版「不拼接标签」姊妹篇，本讲不精读，仅作对照存在。 |

三份精读 notebook 的结构总览（超参均完全相同：`random_seed=0`、`learning_rate=0.001`、`num_epochs=50`、`batch_size=128`）：

| | ae-cvae | ae-cvae_no-out-concat | ae-cnn-cvae |
| --- | --- | --- | --- |
| 条件进入 encoder | 拼接 10 维 one-hot（输入 794） | 同左（794） | 拼接 10 个条件通道（输入 11×28×28） |
| 条件进入 decoder | 拼接 10 维 one-hot（输入 45） | 同左（45） | Linear 前拼接 10 维（输入 60） |
| decoder 输出 | **794**（784 像素 + 10 标签头） | **784**（纯像素） | **11×28×28**（1 图像通道 + 10 标签通道） |
| 重建损失目标 | `x_con = cat(features, onehot)` | `features` | `x_con = cat(features, 条件平面)` |
| `num_latent` | 35 | 35 | 50 |
| 参数量（按层定义手工统计） | 853,364 | 848,354 | 86,927 |
| 首个 batch 损失（作者日志） | 71038.59 | 70192.22 | 767976.69 |
| 第 50 epoch 末段损失（作者日志） | ≈12278.76 | ≈12618.78 | ≈22728.79 |

> **行号说明**：本讲所有永久链接指向 `.ipynb` 原始 JSON 文件的行号（GitHub 上打开即命中对应行）。
>
> 两点提醒：① 损失数值**跨行不可直接比**——输出维度不同（794 vs 784 vs 11×28×28=8624 个 BCE 目标），只有同列内纵向看下降趋势才有意义；② 卷积版每个样本要重建 8624 个目标，所以首个 batch 损失比全连接版大一个数量级是维度差异，不代表更差。

## 4. 核心概念与源码讲解

### 4.1 标签 one-hot 拼接：条件信息的三处注入

#### 4.1.1 概念说明

**从 VAE 的一个遗憾讲起。** u5-l3 的 VAE 从先验采样时，z 里必须**同时**编码「这是哪个数字」和「这个数字长什么样（粗细、倾斜、连笔）」两类信息。但 KL 项又在不遗余力地把所有编码拉向 \(N(0,I)\)——它并不知道「1 和 7 是不同的类别」，只会把不同数字的编码在隐空间里压到一起。结果就是无条件采样经常得到笔画含混、介于两个数字之间的图像。

**CVAE 的思路：类别信息不劳 z 编码，直接告诉网络。** 我们真正想学的生成模型从 \(p(x)\) 升级为**条件生成模型** \(p_\theta(x \mid y)\)——「给定标签 y（比如 7），生成一张 7 的图像」。按生成过程 \(y \to z \to x\) 分解，推断与生成两侧各加一个条件：

\[ \text{推断（encoder）：} q_\phi(z \mid x, y) \qquad \text{生成（decoder）：} p_\theta(x \mid y, z) \]

对应的条件 ELBO（与 u5-l3 的无条件版本逐项对照，只是各多了个 \(y\)）：

\[ \log p_\theta(x \mid y) \;\ge\; \mathbb{E}_{q_\phi(z|x,y)}\big[\log p_\theta(x|y,z)\big] \;-\; D_{KL}\big(q_\phi(z|x,y)\,\|\,p(z)\big) \]

注意一个关键事实：**本实现里先验 \(p(z)=N(0,I)\) 不以 y 为条件**（标签信息既不进 KL 项也不进采样），所以 KL 项的闭式公式与 u5-l3 **一字不差**。条件的全部作用都通过「拼接进网络输入」实现。

**分工的直觉**：y 负责「画什么」，z 只负责「怎么画」。类间差异这副重担从 z 身上卸下后，z 只需在**类内**描述笔迹风格，KL 项也不再有机会把不同类别的编码糊在一起——这就是 CVAE 生成的数字通常比 VAE 更清晰的原因。

**为什么必须 one-hot 而不能直接拼整数标签？** `Linear` 层的输入是一个向量，第 i 个维度乘的是权重矩阵第 i 行。如果直接把标签 7 以数值 7 拼进去：① 数值大小暗示了「7 号类比 1 号类大 7 倍」这种**人为的序关系与量纲**，MNIST 类别之间并无此关系；② 一个标量只占一个输入维度，信息容量太小。one-hot 把类别 c 变成单位向量 \(e_c\)：各类别两两正交（无虚假序关系）、各占一个专属输入维度（各配一列专属权重），是最干净的做法。

#### 4.1.2 核心流程

条件在**三个位置**注入（`ae-cvae` 为例）：

```text
标签 y (int64, 形状 [N])
   │ to_onehot → onehot (float32, 形状 [N,10])
   │
   ├──① encoder 输入端：cat(features 784, onehot 10) → 794 维 ─→ q(z|x,y)
   │
   ├──② decoder 输入端：cat(z 35, onehot 10) → 45 维 ─→ p(x|y,z)
   │
   └──③ 重建损失目标端（仅 out-concat 版）：cat(features, onehot) = 794 维目标
                                                        （见 4.2）
```

one-hot 的构造只有三行：建全零张量 → `scatter_` 按标签位置写 1 → 返回。它在**模型外**定义成自由函数，模型内部与训练循环各调用一次。

#### 4.1.3 源码精读

**关键代码一：`to_onehot` 工具函数**（[ae-cvae.ipynb:L172-L178](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-cvae.ipynb#L172-L178)），这段代码做了什么：把 int64 标签批量转成 one-hot 浮点矩阵——先建 `(N, 10)` 全零张量搬到 device，再用 `scatter_` 把每行第「标签值」列写成 1：

```python
def to_onehot(labels, num_classes, device):
    labels_onehot = torch.zeros(labels.size()[0], num_classes).to(device)
    labels_onehot.scatter_(1, labels.view(-1, 1), 1)
    return labels_onehot
```

两个细节：① `labels.view(-1, 1)` 把形状 `(N,)` 变成 `(N, 1)`——`scatter_` 沿 dim=1 写入时要求索引张量与源张量形状一致，每行一个标量索引；② 它是**模块级函数而非模型方法**（三份 notebook 均如此，如 [ae-cnn-cvae.ipynb:L171-L177](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-cnn-cvae.ipynb#L171-L177)），所以训练循环里算损失时也能直接调它。

**关键代码二：encoder 的条件拼接**（[ae-cvae.ipynb:L212-L223](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-cvae.ipynb#L212-L223)），这段代码做了什么：`encoder` 开头两行就是「条件化」——one-hot 后与展平的像素向量沿特征维 cat 成 794 维，其余（隐层→双头→重参数化）与 u5-l3 的 VAE 完全相同：

```python
def encoder(self, features, targets):
    ### Add condition
    onehot_targets = to_onehot(targets, self.num_classes, device)
    x = torch.cat((features, onehot_targets), dim=1)   # [N,784] + [N,10] → [N,794]
    x = F.leaky_relu(self.hidden_1(x))
    z_mean = self.z_mean(x)
    z_log_var = self.z_log_var(x)
    encoded = self.reparameterize(z_mean, z_log_var)
    return z_mean, z_log_var, encoded
```

对应地，第一层的输入维度在 `__init__` 里就是 `num_features+num_classes`（[ae-cvae.ipynb:L188](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-cvae.ipynb#L188)）：`torch.nn.Linear(num_features+num_classes, num_hidden_1)`，即 `Linear(794, 500)`。**拼接与层维度是一对必须同时改的地方**——只改 cat 不改维度（或反之）会直接形状报错。

**关键代码三：decoder 的条件拼接**（[ae-cvae.ipynb:L225-L235](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-cvae.ipynb#L225-L235)），这段代码做了什么：decoder 收到的不是裸 z，而是 45 维拼接向量——z 管「风格」，one-hot 管「画什么」，两层信息并排进入解码网络：

```python
def decoder(self, encoded, targets):
    onehot_targets = to_onehot(targets, self.num_classes, device)
    encoded = torch.cat((encoded, onehot_targets), dim=1)   # [N,35]+[N,10] → [N,45]
    x = F.leaky_relu(self.linear_3(encoded))
    x = self.linear_4(x)
    decoded = torch.sigmoid(x)
    return decoded
```

第一层维度对应 `Linear(num_latent+num_classes, num_hidden_1)` 即 `Linear(45, 500)`（[ae-cvae.ipynb:L201](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-cvae.ipynb#L201)）。**decoder 的条件注入是 CVAE 真正的「生成开关」**：训练结束后生成新图像时 encoder 整个退役（u5-l3 讲过它是训练期脚手架），用户能控制的只有喂给 decoder 的 `(z, y)` 两个输入——条件生成能力完全由这处拼接提供。

注意 `forward`（[ae-cvae.ipynb:L237-L243](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-cvae.ipynb#L237-L243)）的签名是 `forward(self, features, targets)`——**标签成了模型输入的一部分**，这是 CVAE 系列与此前所有模型的显著不同：`model(features, targets)` 而不是 `model(features)`。

#### 4.1.4 代码实践

**实践：验证 one-hot 拼接的形状链与正交性。**

1. **实践目标**：亲手确认 `to_onehot` 的输出形状与取值、以及拼接后各层维度首尾相接。
2. **操作步骤**（示例代码，可在 notebook 的 Model 单元格之后运行）：

```python
# A. to_onehot 语义检查
labels = torch.tensor([0, 3, 7, 9])
oh = to_onehot(labels, 10, device)
print(oh.shape)                  # 期望 torch.Size([4, 10])
print(oh[2])                     # 期望第 7 位为 1，其余为 0
print((oh @ oh.t()))             # 期望：单位矩阵（两两内积=对角线 1）

# B. 条件 encoder 的形状链（不训练，只看维度）
model_cpu = ConditionalVariationalAutoencoder(784, 500, 35, 10)
x = torch.randn(4, 784)
y = torch.tensor([1, 2, 3, 4])
z_mean, z_log_var, encoded = model_cpu.encoder(x, y)
print(z_mean.shape, encoded.shape)      # 期望 [4,35] [4,35]
print(model_cpu.decoder(encoded, y).shape)  # out-concat 版期望 [4,794]
```

3. **需要观察的现象**：A 中内积矩阵为单位阵（one-hot 向量两两正交）；B 中各输出形状与推导一致。
4. **预期结果**：形状全部吻合即证明拼接维度正确；具体输出待本地验证。注意 B 若在单卡机器上直接用原 notebook 的模型（`cuda:1`），需先改回 `cuda:0` 或像上面这样在 CPU 上重建一个实例。

#### 4.1.5 小练习与答案

**练习 1**：如果把标签直接以 `targets.float()`（取值 0–9 的标量列）拼在 784 维像素后面，会发生什么？

**答案**：形状上能跑通（输入变 785 维），但模型被迫学习「标签数值 × 某个权重」的虚假线性关系——它会认为类别 9「大于」类别 1、类别 2 是类别 1 的「两倍」，这些关系在数字类别间毫无道理；同时所有类别共享一个输入维度，表达力也远不如每类一个专属维度。one-hot 用 10 个维度换来类别间正交、无序、互不干扰。

**练习 2**：条件注入了 encoder、decoder、（下节的）损失三处。哪一处是「生成时仍然起作用」的？

**答案**：只有 decoder 输入端的注入。生成新图像时只调用 `model.decoder(z, labels)`：encoder（连同它的条件拼接）只在训练期参与；损失端的拼接更是训练期的监督信号，推理时不存在。所以「条件生成能力」最终物化为 decoder 第一层 `Linear(45, 500)` 中那 10 列条件专属权重。

**练习 3**：CVAE 的 KL 项为什么可以和 VAE 完全一样，不用改成 \(D_{KL}(q(z|x,y)\,\|\,p(z|y))\)？

**答案**：因为本实现选的先验 \(p(z)=N(0,I)\) **不以 y 为条件**——隐变量与标签在先验下独立，\(p(z|y)=p(z)\)，两项写法本就相等。闭式 KL 只取决于 \(q(z|x,y)=N(\mu(x,y),\sigma^2(x,y))\) 的参数与标准正态，公式自然是 \(\frac{1}{2}\sum(\mu^2+\sigma^2-\ln\sigma^2-1)\)。（若实现选条件先验，KL 会随先验形式改变，但本仓库未这样做。）

### 4.2 条件解码器：标签是否参与重建损失的两种实现

#### 4.2.1 概念说明

**条件解码器的第一个分岔**：z 与 one-hot 拼接后进 decoder 是固定动作，问题是 **decoder 该输出什么**。

- **变体 A（without out-concat，`ae-cvae_no-out-concat`）**：decoder 输出 784 维纯像素，重建损失就是熟悉的 `BCE(decoded, features)`。标签的作用止步于「给 decoder 提供输入信息」。
- **变体 B（with out-concat，`ae-cvae`）**：decoder 最后一层输出 **794** 维——前 784 维照常重建像素，**末尾 10 维额外重建 one-hot 标签本身**；损失目标是 `cat(features, onehot)` 拼成的 794 维向量。相当于给 decoder 加了一个**辅助分类头**：不仅要把图画对，还要能「认出」自己画的是哪个数字。

**变体 B 的直觉**：一个把「7」重建得很像的解码过程，理应也能从自己的生成结果里认出 7。强迫 decoder 输出标签，等于给它一个额外的监督信号，促使其内部特征对类别信息更敏感——这是非卷积 CVAE 文献里的常见做法，`ae-cvae` 的说明文字也明确写道本实现「leads to slightly better results」（[ae-cvae.ipynb:L57](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-cvae.ipynb#L57)）。

**但结论并不普适**。卷积版的说明文字写着**正好相反**的经验：拼接标签导致「substantially poorer results」（[ae-cnn-cvae.ipynb:L57](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-cnn-cvae.ipynb#L57)），所以才有 `ae-cnn-cvae_no-out-concat` 这份姊妹篇。两处源码注释也各自留下了立场：`ae-cvae_no-out-concat` 里被注释掉的损失行旁边写着「Disabled for reconstruction loss as it gives poor results」（[ae-cvae_no-out-concat.ipynb:L797](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-cvae_no-out-concat.ipynb#L797)）。

结合 u3-l5 的教训——**单次运行、未对齐训练配方的对比不可轻信**——正确的态度是：把「标签是否进重建损失」当作一个**需要自己做对照实验的超参选择**，两份 notebook 恰好就是现成的 A/B 模板。

#### 4.2.2 核心流程

两个变体的训练循环都是标准五步，唯一分岔在损失计算：

```text
for batch in loader:
    features 展平/搬 device，targets 搬 device
    z_mean, z_log_var, encoded, decoded = model(features, targets)   # 前向
    kl = 0.5*(μ² + exp(logσ²) − logσ² − 1).sum()                     # KL（两版相同）
    变体A: bce = BCE(decoded[784], features)                         # 目标=纯像素
    变体B: x_con = cat(features, onehot); bce = BCE(decoded[794], x_con)
    cost = kl + bce
    zero_grad → backward → step
```

注意 BCE 与 KL 都用 `reduction='sum'`：对 batch 内所有样本、所有输出维度求和。这让损失数值随 batch 大小与输出维度变化（解释了总览表中三列损失不可横向比），但梯度方向不受均值化的「小批权重放大」问题影响（u4-l4 讲过 epoch 聚合时同样先用 sum 再除样本数的思想）。

#### 4.2.3 源码精读

**关键代码一：变体 B 的「加宽」输出层**（[ae-cvae.ipynb:L201-L202](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-cvae.ipynb#L201-L202)），这段代码做了什么：decoder 第一层吃 `num_latent+num_classes`=45 维条件输入，最后一层输出 `num_features+num_classes`=794 维——多出的 10 维就是标签预测头：

```python
self.linear_3 = torch.nn.Linear(num_latent+num_classes, num_hidden_1)
self.linear_4 = torch.nn.Linear(num_hidden_1, num_features+num_classes)
```

**关键代码二：变体 A 的对照写法**（[ae-cvae_no-out-concat.ipynb:L202-L204](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-cvae_no-out-concat.ipynb#L202-L204)），这段代码做了什么：同一位置只差一个数字——`linear_4` 输出 784，且注释里保留了被否决的 794 维写法和理由（「yields worse results」）：

```python
# don't output labels in resulting image as it yields worse results
#self.linear_4 = torch.nn.Linear(num_hidden_1, num_features+num_classes)
self.linear_4 = torch.nn.Linear(num_hidden_1, num_features)
```

这是仓库里罕见的「**用注释保存实验结论**」的写法，读源码时应注意：被注释的行是作者试过并放弃的方案，不是待完成代码。

**关键代码三：两版损失的分岔点**。变体 B（[ae-cvae.ipynb:L842-L849](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-cvae.ipynb#L842-L849)）：

```python
kl_divergence = (0.5 * (z_mean**2 + torch.exp(z_log_var) - z_log_var - 1)).sum()
# add condition
x_con = torch.cat((features, to_onehot(targets, num_classes, device)), dim=1)
pixelwise_bce = F.binary_cross_entropy(decoded, x_con, reduction='sum')
cost = kl_divergence + pixelwise_bce
```

这段代码做了什么：KL 与 u5-l3 逐字相同；重建目标 `x_con` 在像素后又拼上 one-hot，与 decoder 的 794 维输出逐维对齐——像素位比像素、标签位比标签，一份 BCE 同时干两件事。

变体 A（[ae-cvae_no-out-concat.ipynb:L793-L802](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-cvae_no-out-concat.ipynb#L793-L802)）则把 `x_con` 两行注释掉，直接 `BCE(decoded, features, reduction='sum')`。

**读源码彩蛋——日志里的「003」**：变体 B 的日志打印写的是 `len(train_loader)//batch_size`（[ae-cvae.ipynb:L861](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-cvae.ipynb#L861)），469 个 batch 除以 128 取整得 3，于是作者保存的输出里出现了「Batch 050/003」这种分母比分子还小的怪日志；变体 A 用的是正确的 `len(train_loader)`（[ae-cvae_no-out-concat.ipynb:L814](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-cvae_no-out-concat.ipynb#L814)）。纯属显示瑕疵、不影响训练——但再次提醒「日志格式串里的每个表达式都值得多看一眼」。

**参数量对账**（按层定义手工统计，`权重+偏置`）：

| 层 | 变体 B（ae-cvae） | 变体 A（no-out-concat） |
| --- | --- | --- |
| hidden_1 | 794×500+500 = 397,500 | 397,500 |
| z_mean / z_log_var | 500×35+35 = 17,535 ×2 | 同左 ×2 |
| linear_3 | 45×500+500 = 23,000 | 23,000 |
| linear_4 | 500×**794**+794 = 397,794 | 500×**784**+784 = 392,784 |
| 合计 | **853,364** | **848,354** |

两版只差 5,010 个参数（0.6%）——真正的差异在**监督信号**而非模型容量。

#### 4.2.4 代码实践

**实践：把两份 notebook 改成同种子短程对照。**

1. **实践目标**：在同一环境下用相同种子跑两版各若干 epoch，对比重建质量与条件采样的清晰度，亲测「标签是否进重建损失」的影响。
2. **操作步骤**：
   - 分别打开两份 notebook，把 device 改为 `cuda:0`（或 cpu）、`num_epochs` 降为 10 以内；
   - 在两版第一个单元格里都加 `torch.manual_seed(0)` 与 `np.random.seed(0)`（原 notebook 只在建模型前设了 `torch.manual_seed`，见 [ae-cvae.ipynb:L245](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-cvae.ipynb#L245)）；
   - 训练结束后用 4.4 节的采样代码各生成 10 张「标签=7」的图像并排贴出。
3. **需要观察的现象**：两版都能生成可辨认的 7；短程训练下细节清晰度可能有肉眼差异。
4. **预期结果**：与作者结论（全连接版 B 略好）方向一致或不同都正常——10 epoch 的单次对比本就不足以定论，这个实践的价值在于**搭好 A/B 模板**；具体结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：变体 B 中，decoder 末尾 10 维输出的损失「标签位比 one-hot」——这 10 维经过 sigmoid 吗？这合理吗？

**答案**：经过了——`sigmoid` 作用在 `linear_4` 输出的**整个 794 维**上，末 10 维也被压到 [0,1]，而 one-hot 目标恰在 {0,1}，与 BCE 的值域要求（u5-l1 讲过 BCE 吃概率）完全匹配。所以同一个 sigmoid 同时服务像素与标签两条通路，无需拆分。

**练习 2**：变体 B 的损失里，10 个标签位的 BCE 与 784 个像素位的 BCE 直接相加。如果类别很难预测，这个辅助信号会不会「喧宾夺主」？

**答案**：有可能，这正是权衡所在。标签只有 10 维、且 decoder 手握 one-hot 输入，预测标签近乎「抄答案」，通常很快饱和、对总损失贡献小；但梯度上它仍会参与塑造 decoder 的中间特征。卷积版作者报告拼接标签明显变差，一种解释就是 11 通道输出里标签通道的重建干扰了图像通道的优化。这也是把两种实现都保留在仓库里的价值：哪种好，让实验说话。

**练习 3**：为什么两份 notebook 的损失数值（如首个 batch 71038 vs 70192）不能直接比较大小来判断好坏？

**答案**：`reduction='sum'` 下损失等于所有维度的 BCE 之和，变体 B 每个样本多 10 个目标维度（约 1.3% 的维度差）；更重要的是同维度下不同模型处于不同训练状态时，损失受随机初始化与采样噪声影响。要比较应固定种子、对齐 epoch，并主要看**生成图像质量**这类与目标直接相关的指标。

### 4.3 卷积版 CVAE：条件注入的通道化改造

#### 4.3.1 概念说明

全连接版的条件拼接是**向量维**的（784+10）；卷积网络的输入是 4 维张量 \((N, C, H, W)\)，one-hot 拼不进空间维——条件注入必须换一种几何：**把 10 个标签值变成 10 张与输入图像同尺寸的「条件平面」**，沿通道维与图像拼成 11 通道输入。

具体做法（源码三步，见 4.3.3）：把 `(N,10)` 的 one-hot 变形为 `(N,10,1,1)`，乘上形状 `(N,10,28,28)` 的全 1 张量——广播把每个标签值铺满整张平面，得到 10 张「全 c 值平面」；再与 1 通道图像 cat。对类别 c，第 c 张条件平面全 1、其余全 0，本质上是 **10 张二值掩码**：卷积核在图像每个位置都能「看到」这是什么类别，条件信息带上了空间结构。

decoder 侧对称：`Linear(50+10 → 64·2·2)` 先把条件拼进向量（与全连接版同法），还原成 64×2×2 特征图后经三个转置卷积上采样，**最后一层输出 11 个通道**（1 个图像通道 + 10 个标签通道），对应 4.2 节的变体 B——用通道而非向量维度实现「标签参与重建损失」。

#### 4.3.2 核心流程

卷积版一次前向的形状链（batch 维省略；encoder 用 u3-l1 的卷积公式 \(o=\lfloor(w-k+2p)/s\rfloor+1\)，decoder 用 u5-l2 的转置卷积公式 \(o=(w-1)s-2p+k\)）：

| 段 | 操作 | 形状 |
| --- | --- | --- |
| 条件平面 | one-hot 广播成掩码 | 10×28×28 |
| 拼接 | cat(图像, 掩码, dim=1) | **11**×28×28 |
| enc_conv_1 | Conv2d(11→16, k6, s2) | 16×12×12 |
| enc_conv_2 | Conv2d(16→32, k4, s2) | 32×5×5 |
| enc_conv_3 | Conv2d(32→64, k2, s2) | 64×2×2 |
| 双头 | Linear(256→50) ×2 + 重参数化 | 50 |
| 拼接 | cat(z, one-hot) | 60 |
| dec_linear_1 | Linear(60→256) + reshape | 64×2×2 |
| dec_deconv_1 | ConvT(64→32, k2, s2) | 32×4×4 |
| dec_deconv_2 | ConvT(32→16, k4, s3, p1) | 16×11×11 |
| dec_deconv_3 | ConvT(16→**11**, k6, s3, p4) | **11**×28×28 |

逐层验算两处关键尺寸：encoder 末层 \((5-2)/2+1 = 2\)，与 `z_mean` 的输入 `64*2*2`=256 精确匹配；decoder 末层 \((11-1)\cdot3-2\cdot4+6 = 28\)，恰好还原原图尺寸。`num_latent` 从全连接版的 35 提到 50（[ae-cnn-cvae.ipynb:L119](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-cnn-cvae.ipynb#L119)）。

参数量按层手工统计合计 **86,927**（如 6352+8224+8256+12850×2+15616+8224+8208+6347），约为全连接版的十分之一——卷积的参数共享优势（u3-l1）在生成模型里同样成立。

#### 4.3.3 源码精读

**关键代码一：入口通道数 +1 变 +11**（[ae-cnn-cvae.ipynb:L195-L200](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-cnn-cvae.ipynb#L195-L200)），这段代码做了什么：第一个卷积层的 `in_channels` 不是 1 而是 `1+self.num_classes`=11，为 10 张条件平面预留输入通道：

```python
self.enc_conv_1 = torch.nn.Conv2d(in_channels=1+self.num_classes,
                                  out_channels=16, kernel_size=(6, 6), stride=(2, 2), padding=0)
```

**关键代码二：条件平面的广播构造**（[ae-cnn-cvae.ipynb:L258-L270](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-cnn-cvae.ipynb#L258-L270)），这段代码做了什么：encoder 前段的三步条件化——one-hot 变形为 `(N,10,1,1)`、乘全 1 张量广播成 10 张 28×28 掩码、与图像沿通道维拼接：

```python
onehot_targets = to_onehot(targets, self.num_classes, device)
onehot_targets = onehot_targets.view(-1, self.num_classes, 1, 1)   # [N,10]→[N,10,1,1]
ones = torch.ones(features.size()[0], self.num_classes,
                  features.size()[2], features.size()[3], ...).to(device)  # [N,10,28,28] 全1
ones = ones * onehot_targets          # 广播：每张平面填入对应标签值（0或1）
x = torch.cat((features, ones), dim=1)  # [N,1,28,28]+[N,10,28,28] → [N,11,28,28]
```

广播规则（u2-l3 提过）：`(N,10,28,28)` 与 `(N,10,1,1)` 相乘时，后两维自动扩张——**没有复制内存**，每个空间位置共享同一个标签值。

**关键代码三：出口 11 通道**（[ae-cnn-cvae.ipynb:L243-L247](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-cnn-cvae.ipynb#L243-L247)），这段代码做了什么：decoder 最后一个转置卷积输出 11 通道——通道 0 是重建图像，通道 1–10 对应 one-hot 的 10 个标签位，是 4.2 节变体 B 的通道版：

```python
self.dec_deconv_3 = torch.nn.ConvTranspose2d(in_channels=16,
                                             out_channels=11,   # 1 图像 + 10 标签
                                             kernel_size=(6, 6), stride=(3, 3), padding=4)
```

**关键代码四：训练损失的同构改造**（[ae-cnn-cvae.ipynb:L917-L936](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-cnn-cvae.ipynb#L917-L936)），这段代码做了什么：训练循环里把 4.3.3 关键代码二的三步原样重演一遍构造 11 通道目标 `x_con`，再对 decoder 的 11 通道输出做逐通道 BCE；KL 项依旧原封不动：

```python
...
ones = ones * onehot_targets
x_con = torch.cat((features, ones), dim=1)          # 11×28×28 的目标
pixelwise_bce = F.binary_cross_entropy(decoded, x_con, reduction='sum')
cost = kl_divergence + pixelwise_bce
```

**关键代码五：可视化只取通道 0**（[ae-cnn-cvae.ipynb:L1000](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-cnn-cvae.ipynb#L1000)），这段代码做了什么：重建展示时用 `decoded[:n_images, 0]` 切出图像通道、丢弃 10 个标签通道——通道维上的「掐头」与全连接版向量维上的 `[:, :-num_classes]`（4.4 节）是同一个动作的两种几何。

#### 4.3.4 代码实践

**实践：手工推导 + 代码验证形状链。**

1. **实践目标**：用本节两条尺寸公式推出全链形状，再用代码逐层验证，确认「掩码广播无复制、尺寸零裁剪」。
2. **操作步骤**（示例代码，在模型实例化后运行）：

```python
m = ConditionalVariationalAutoencoder(num_features=784, num_latent=50, num_classes=10)
x = torch.randn(2, 1, 28, 28)          # NCHW
y = torch.tensor([3, 5])

shapes = []
def hook(mod, inp, out): shapes.append((mod.__class__.__name__, tuple(out.shape)))
for mod in [m.enc_conv_1, m.enc_conv_2, m.enc_conv_3,
            m.dec_deconv_1, m.dec_deconv_2, m.dec_deconv_3]:
    mod.register_forward_hook(hook)    # forward hook 用法见 u4-l5

z_mean, z_log_var, enc = m.encoder(x, y)
out = m.decoder(enc, y)
for s in shapes: print(s)              # 对照 4.3.2 的表格逐行核对
print(tuple(out.shape))                # 期望 (2, 11, 28, 28)
```

3. **需要观察的现象**：六层输出形状与 4.3.2 表格逐行一致，decoder 末端精确回到 28×28，全程没有裁剪操作（对比 u5-l2 转置卷积的奇数尺寸裁剪问题——这里的 k/s/p 配方是精心凑好的）。
4. **预期结果**：全部吻合；若不符优先检查自己手算的取整方向（卷积向下取整、转置卷积不取整）。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么不把 one-hot 直接 `cat` 到图像张量后面（变成 29 行），而要费劲广播成 10 张平面？

**答案**：`Conv2d` 要求输入形状固定为 `(N, C, H, W)`，空间维 H、W 由卷积核几何决定，把 10 个数拼成「第 29 行像素」会破坏这一约定，且这 10 个值会被卷积核当作普通像素滑窗处理、位置语义混乱。通道维才是为「并行的特征平面」设计的维度：10 张常数平面与 1 张图像平面在卷积眼里一视同仁，第一个卷积核的 11 通道权重自然学会「读图 + 读条件」。

**练习 2**：卷积版作者报告「标签进重建损失」明显更差（与全连接版结论相反）。给出一个可能的机理猜想。

**答案**：一种解释是输出端的**通道不平衡**：11 个输出通道里 10 个是近乎「抄答案」的标签通道（decoder 输入端就有 one-hot），其 BCE 很快趋零；但这些通道与图像通道共享前面的卷积特征，标签通道的梯度会持续把共享特征往「复制 one-hot」的方向拉，挤占了本该用于重建的空间结构学习。全连接版里标签头只占 794 维中的 10 维且经由独立的最后一层权重，干扰相对小。（猜想需实验检验——正好可用 `ae-cnn-cvae_no-out-concat.ipynb` 做对照。）

**练习 3**：卷积版的 `num_latent` 是 50，全连接版是 35。隐变量维度不同会影响两版之间的比较吗？

**答案**：会影响。两版对比时同时改变了「骨架（全连接 vs 卷积）」与「隐维（35 vs 50）」两个变量，不是严格的受控实验（u2-l4 的「只改一处」原则）。若想干净比较，应把两版的 `num_latent` 对齐再跑——这也是自己做实验时值得注意的细节。

### 4.4 按条件采样：从「听天由命」到「指哪打哪」

#### 4.4.1 概念说明

**生成的全部输入是 `(z, y)`。** 训练结束后，CVAE 的生成流程是：

1. 从先验 \(z \sim N(0, I)\) 采样（`torch.randn`）——决定「风格」；
2. **手工指定标签 y**——决定「画什么」；
3. 只调 `model.decoder(z, y)`，得到图像。

对照 u5-l3 的无条件采样：那里 z 独自承担全部信息，采样落点是「哪个数字的领地」纯看运气，还常常落在两类之间的模糊地带；这里 y 直接写在 decoder 输入里，类别**由用户决定**。而 z 的先验依然是无条件标准正态（4.1 讲过），所以采样代码与 u5-l3 几乎相同——只是 decoder 多收一个参数。

**「固定 z、遍历 y」是 CVAE 最有说服力的演示**：同一个 z 配上 0–9 十个标签，decoder 应当输出**同一笔迹风格**（粗细、倾斜、饱满程度相近）的十个不同数字。它直观地展示了 y 与 z 的分工——y 换数字、z 保风格——这正是 4.1.1 那个「卸担子」论述的实验证据，也是本讲综合实践的主任务。

#### 4.4.2 核心流程

notebook 的条件采样循环（每个类别生成一批）：

```text
for i in 0..9:
    labels = tensor([i]*10)             # 10 个相同的标签
    z = randn(10, num_latent)           # 10 份先验采样
    images = decoder(z, labels)         # 10 张「数字 i」
    可视化时掐掉标签维度，只留像素
```

三份 notebook 的这段代码几乎逐字相同，差异只在「掐标签」的方式：

| notebook | decoder 输出 | 取图像的方式 |
| --- | --- | --- |
| ae-cvae | `(N, 794)` 向量 | 切片 `[:, :-num_classes]`（掐掉末 10 维） |
| ae-cvae_no-out-concat | `(N, 784)` 向量 | 直接用（无标签维可掐） |
| ae-cnn-cvae | `(N, 11, 28, 28)` | 切通道 `[:, 0]` |

#### 4.4.3 源码精读

**关键代码一：全连接版的条件采样**（[ae-cvae.ipynb:L1126-L1135](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-cvae.ipynb#L1126-L1135)），这段代码做了什么：对每个类别 i 构造 10 个相同标签、采 10 份 z，直接调用 `model.decoder` 生成——不经过 encoder、不经过任何确定性开关：

```python
for i in range(10):
    labels = torch.tensor([i]*10).to(device)
    n_images = labels.size()[0]
    rand_features = torch.randn(n_images, num_latent).to(device)   # 先验采样
    new_images = model.decoder(rand_features, labels)              # 条件解码
```

**关键代码二：掐掉标签维度**（[ae-cvae.ipynb:L1144](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-cvae.ipynb#L1144)），这段代码做了什么：decoder 输出 794 维，可视化前用负索引切片丢掉末尾 10 个标签维、只留 784 个像素维，再 reshape 成 28×28 画图：

```python
decoded_images = new_images[:n_images][:, :-num_classes]
```

若忘了这一步，`view(28, 28)` 会因 794 不能整除而报错——这是 out-concat 版特有的「输出里混着标签头」的后处理税。

**关键代码三：变体 A 无需切片**（[ae-cvae_no-out-concat.ipynb:L1085-L1088](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-cvae_no-out-concat.ipynb#L1085-L1088) 与 [L1097](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-cvae_no-out-concat.ipynb#L1097)），这段代码做了什么：采样三行与变体 B 完全相同，但输出就是 784 维纯像素，`decoded_images = new_images[:n_images]` 直接可用——「要不要掐标签维」是两版在推理代码里的唯一差异。

**关键代码四：卷积版取通道 0**（[ae-cnn-cvae.ipynb:L1211-L1220](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-cnn-cvae.ipynb#L1211-L1220)，可视化切片在 [L1229](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/autoencoder/ae-cnn-cvae.ipynb#L1229)），这段代码做了什么：同样的 `randn + decoder` 模式，`new_images[:n_images, 0]` 取第 0 个通道作图像、丢弃 10 个标签通道。

#### 4.4.4 代码实践

**实践：复现 notebook 的逐类别采样，并做一版「类别对齐」的对照。**

1. **实践目标**：跑通条件采样；再固定同一份 z 比较 0 和 8 两个类别的输出，初步感受「同 z 不同 y」。
2. **操作步骤**（示例代码，训练完成后运行）：

```python
import matplotlib.pyplot as plt

# A. notebook 原版：每个类别独立采 z
for i in [0, 8]:
    labels = torch.tensor([i]*10).to(device)
    z = torch.randn(10, num_latent).to(device)
    imgs = model.decoder(z, labels)[:, :-num_classes]   # 掐掉标签维（out-concat 版）
    plt.figure(figsize=(10, 1.2))
    for j in range(10):
        plt.subplot(1, 10, j+1)
        plt.imshow(imgs[j].detach().cpu().view(28, 28), cmap='binary'); plt.axis('off')
    plt.suptitle(f'label = {i}'); plt.show()

# B. 对齐版：0 和 8 共用同一份 z
z_shared = torch.randn(10, num_latent).to(device)
for i in [0, 8]:
    labels = torch.tensor([i]*10).to(device)
    imgs = model.decoder(z_shared, labels)[:, :-num_classes]
    print('label', i, '第 3 张像素均值:', imgs[3].mean().item())   # 风格探针
```

3. **需要观察的现象**：A 中两批图像分别是清一色的 0 与 8；B 中第 j 张 0 与第 j 张 8 来自同一个 z（z_shared 的第 j 行），笔画粗细/倾斜等风格上应有可辨识的相似性。
4. **预期结果**：类别正确性通常训练几个 epoch 后就很稳；风格一致性较微妙（35 维 z + 50 epoch 下未必张张明显），可多换几个 z 观察。具体效果待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：生成时如果不传标签、随便喂一个全零的 `targets` 张量（one-hot 全 0），会发生什么？

**答案**：不会报错——`to_onehot` 对全零标签产出全 0 的 one-hot（scatter 没有位置可写 1），decoder 照常解码，只是条件输入退化成「什么类别都不是」。输出大概率是模糊、无明确类别的图像，因为训练时从未见过这种「非法条件」。这提示：**条件生成模型对条件输入的合法性有隐含约定**。

**练习 2**：想用训练好的 CVAE 做「把 3 改写成 8」的风格迁移式编辑，最小可行做法是什么？

**答案**：用 encoder 把真实的 3 编码成 z（取 `z_mean` 更稳定，u5-l3 讲过 `encoded` 带采样噪声），再把**同一个 z 配上标签 8** 交给 decoder：`z, _, _ = model.encoder(x3, torch.tensor([3])); out = model.decoder(z, torch.tensor([8]))`。前提正是本讲的分工假设——z 里主要剩风格信息、类别由 y 承载。效果是否如意待本地验证。

**练习 3**：为什么生成代码用 `torch.randn` 而不是把训练集编码的 z 拿来复用？

**答案**：两种都能生成，含义不同。`randn` 采的是**先验**里的点，检验的是「KL 项把隐空间整理成标准正态」的承诺兑现程度（u5-l3 的核心论点）；复用编码 z 则是「重建/编辑」场景（练习 2），检验的是条件解码的保真度。notebook 选择前者，是为了展示模型作为**生成模型**的能力——这也是 CVAE 与 u5-l1 普通 AE 的本质分界。

## 5. 综合实践

**任务：固定隐变量、遍历 0–9 十个标签，可视化「同一风格、十个数字」。**

1. **实践目标**：把本讲三个最小模块串成一条链——用 `to_onehot` 的条件接口（4.1）、训练好的条件解码器（4.2）、按条件采样（4.4），产出一组可解释的生成结果。
2. **操作步骤**：
   - 打开 `ae-cvae.ipynb`，把 device 改为 `cuda:0`（单卡机器），`num_epochs` 视算力降到 10–50，完整训练；
   - 训练结束后在最后一个单元格里运行下面的生成函数（示例代码）：

   ```python
   def generate_one_of_each(model, z_row, device, num_classes=10):
       """固定一个隐向量 z_row，用 0..9 十个标签各生成一张图，返回 (10,28,28) 张量"""
       z = z_row.view(1, -1).repeat(num_classes, 1).to(device)      # 同一个 z 复制 10 份
       labels = torch.arange(num_classes).to(device)                 # 标签 0,1,...,9
       imgs = model.decoder(z, labels)[:, :-num_classes]            # 掐掉标签维
       return imgs.view(num_classes, 28, 28).detach().cpu()

   torch.manual_seed(123)
   base_z = torch.randn(1, num_latent)                              # 固定的「风格种子」
   grid = generate_one_of_each(model, base_z, device)

   fig, axes = plt.subplots(2, 5, figsize=(10, 4))
   for digit, ax in enumerate(axes.flat):
       ax.imshow(grid[digit], cmap='binary'); ax.set_title(str(digit)); ax.axis('off')
   plt.suptitle('same z, labels 0-9'); plt.show()

   # 进阶：换 3 个不同的 base_z，观察风格随 z 变化、类别只随标签变化
   for seed in [1, 2, 3]:
       torch.manual_seed(seed)
       g = generate_one_of_each(model, torch.randn(1, num_latent), device)
       # ... 同样方式画图
   ```

   - 进阶扩展：仿照 u5-l3 的插值实验，固定标签 7、在两个 z 之间线性插值取 10 个点，观察风格渐变而类别不变。
3. **需要观察的现象**：每张图都是可辨认的对应数字；同一行内（同一 base_z）十个数字的笔画粗细、倾斜等风格应大体一致；换 base_z 后整体风格随之改变。
4. **预期结果**：类别控制基本 100% 生效（one-hot 直接写入 decoder 输入，学得很快）；「风格跨类别保持」是 softer 的性质，50 epoch、35 维 z 下部分数字对（如 1 和 0）风格相似性更明显，另一些可能较弱。若风格一致性差，可尝试增大 `num_latent` 或延长训练再对比。具体效果待本地验证。

## 6. 本讲小结

- **CVAE = VAE + 条件拼接**：重参数化、KL + 重建损失、先验采样全部原样继承；新增的只有「把 one-hot 标签拼进 encoder 输入（794/11 通道）与 decoder 输入（45/60）」，模型由此学条件分布 \(p(x|y,z)\)，先验 \(p(z)=N(0,I)\) 不变，故 KL 闭式公式不变。
- **标签 one-hot 拼接**：`to_onehot` 用 `zeros + scatter_` 三行实现，类别间正交、各占一个专属输入维度；标签从「监督信号」升级为「模型输入的一部分」，`forward(features, targets)` 的签名变化是外在标志。
- **条件解码器的两个变体只差一处**：`linear_4` 输出 794（标签头参与重建损失，变体 B）还是 784（纯像素，变体 A），参数量差 0.6%，差的是监督信号；全连接版作者报告 B 略好、卷积版报告 B 明显更差——这类经验结论应视为待检验的实验选项，仓库正好提供了现成 A/B 模板。
- **卷积版把条件「空间化」**：one-hot 广播成 10 张 28×28 掩码平面、与图像拼成 11 通道输入，decoder 对称输出 11 通道；形状链 11×28×28→64×2×2→11×28×28 全程零裁剪，参数量约为全连接版十分之一。
- **按条件采样 = randn + 指定标签 + 只调 decoder**；out-concat 版输出里混着标签维，可视化要掐尾（向量版 `[:, :-num_classes]`、卷积版 `[:, 0]`）；「固定 z、遍历标签」实验直接验证了 y 管内容、z 管风格的分工。
- 沿袭的读源码习惯：说明文字的「768 像素」笔误、`cuda:1` 的设备写法、日志里 `len(train_loader)//batch_size` 的显示瑕疵——**以代码与变量来源为准**。

## 7. 下一步学习建议

- **下一讲 u5-l5（GAN 基础）**：换一条技术路线实现「按噪声生成图像」——用生成器与判别器的对抗训练替代 ELBO 的重建 + KL；学完后可对照思考：CVAE 的「显式标签控制」在 GAN 里要靠条件 GAN（把标签拼进生成器/判别器，思路与本讲完全同源）才能获得。
- **补做对照实验**：跑 `ae-cnn-cvae_no-out-concat.ipynb`（README 第 232 行徽章），验证 4.3.5 练习 2 对卷积版「标签通道干扰图像重建」的机理猜想。
- **横向串联**：把本讲的「条件拼接」与 u5-l3 的 β-VAE 组合，做「条件 + 解耦」实验（例如 β=4 时固定 z 遍历标签，看风格是否更稳定）；再到 u6-l6 看 DistilBERT 如何把「条件」从 one-hot 标签升级为自然语言序列。
- **源码延伸阅读**：`pytorch_ipynb/autoencoder/` 目录下 14 份 notebook 已在 u5-l1 至本讲覆盖了 7 份核心，剩余的 quickdraw/celeba 数据版 AE 可作为 u4-l2（自定义 Dataset）与本讲内容的综合练手素材。
