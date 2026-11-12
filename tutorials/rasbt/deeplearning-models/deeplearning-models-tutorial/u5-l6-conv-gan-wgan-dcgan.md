# 卷积 GAN、WGAN 与 DCGAN

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清**卷积 GAN（gan-conv）**与上一讲全连接 GAN 的每一处差异：投影 + 转置卷积生成器、步长卷积判别器、BatchNorm 替代 Dropout、logits 直出配 `binary_cross_entropy_with_logits`。
2. 逐行解释 **WGAN 的四件套改造**：无 Sigmoid 的 critic、`wasserstein_loss`、\(-1/+1\) 标签、权重裁剪与「critic 训 5 次」；并知道 WGAN 的损失该怎么读（不再以 \(\ln 2\) 为参考线）。
3. 理解 **label smoothing** 抑制判别器过度自信的原理：`valid*0.9` 这一处最小改动为什么有效，它与上一讲 `0.5*` 缩放的亲缘关系。
4. 走通 **DCGAN 在 CelebA 人脸上的完整流程**：无标签自定义 Dataset、`Normalize(0.5, 0.5)` 对齐 Tanh 值域、DCGAN 权重初始化、`betas=(0.5, 0.999)` 的 Adam、`fixed_noise` 网格评估。
5. 会设计**受控对照实验**比较不同 GAN 训练配方的稳定性，并诚实处理「结构 + 损失同时改变」带来的混杂变量。

## 2. 前置知识

### 2.1 从 u5-l5 带走什么

上一讲我们建立了 GAN 的完整骨架，本讲所有 notebook 都是对它的「换件升级」：

- **两幕戏训练循环**：每个 batch 先训生成器再训判别器（或反过来），各走一遍 forward → loss → zero_grad → backward → step 五步范式；
- **`detach()` 切断梯度**：判别器阶段喂 `假图.detach()`；
- **值域对齐**：真实图像重缩放到 \([-1,1]\) 与生成器 `Tanh` 输出对齐；
- **读数方式**：BCE 损失下双方在 \(\ln 2 \approx 0.693\) 附近拉锯才是健康均衡。

### 2.2 为什么要卷积化

u3-l1 讲过卷积的两个先验：**局部连接**（像素只与邻近像素强相关）与**权值共享**（同一个卷积核扫全图）。全连接 GAN 的生成器把 784 个像素当成 784 个互不相关的自由变量，判别器要识别「图像局部纹理」就得为每个位置单独学权重——这对图像任务是浪费。u5-l2 讲过的**转置卷积上采样**（把 \(7\times7\) 特征图放大到 \(28\times28\)）在本讲的生成器里原样复用；u3-l3 讲过的「**步长卷积替代池化做下采样**」在本讲的判别器里复用。

### 2.3 GAN 训练为什么不稳定：WGAN 的动机

原始 GAN 的博弈在数学上等价于最小化两个分布的 **Jensen–Shannon 散度（JSD）**。JSD 有个糟糕的性质：当真实分布 \(p_{\text{data}}\) 与生成分布 \(p_g\) 的支撑**完全不重叠**（训练初期几乎必然如此——真实图像聚在数据流形上，生成图像从噪声里随机冒出来），JSD 是常数，梯度为零，生成器失去学习信号。上一讲能训起来，靠的是非饱和技巧 + 概率版 BCE 的 \(p-y\) 梯度在错得离谱时依然巨大，但「D 太快碾压 G」的威胁始终存在。

**Wasserstein 距离**（又叫推土机距离，EM distance）度量「把一个分布搬运成另一个分布的最小代价」，即使两分布不重叠，它也随分布间距**平滑变化**——这正是我们希望损失函数具有的性质。WGAN（Arjovsky 等，2017）用一个改造后的「critic」来近似估计这个距离，代价是要给 critic 加 **Lipschitz 约束**，本讲看到的最粗暴做法就是**权重裁剪**。

### 2.4 术语表

| 术语 | 含义 |
| --- | --- |
| critic（评论家） | WGAN 里的「判别器」；它不再输出概率，而是输出一个实数打分，名字也从「判别」改为「评价」 |
| Lipschitz 约束 | 函数变化率的约束：\(\|f(x_1)-f(x_2)\| \le K\|x_1-x_2\|\)；WGAN 需要 \(K\le1\)（1-Lipschitz） |
| 权重裁剪（weight clipping） | 每次更新后把权重强制压回 \([-c, c]\) 区间，是近似 Lipschitz 约束的粗暴手段 |
| label smoothing（标签平滑） | 把硬标签 1 软化为 0.9 之类的软目标，抑制判别器过度自信 |
| DCGAN | Radford 等（2015）总结的一整套卷积 GAN 结构与训练配方 |
| 模式崩塌（mode collapse） | 生成器只产出少数几种样本骗过判别器的失败形态 |

## 3. 本讲源码地图

本讲涉及 `pytorch_ipynb/gan/` 下的五份 notebook，它们都以 u5-l5 的 `gan.ipynb` 为共同祖先，各自沿一个维度改造：

```text
                    gan.ipynb（u5-l5 全连接基线：Sigmoid + BCE）
                     │                              │
        卷积化（结构与激活）                    换损失（Wasserstein + 裁剪）
                     │                              │
              ┌──────┴──────┐                ┌──────┴──────┐
              │             │                │             │
         gan-conv.ipynb  gan-conv-       wgan-1.ipynb   dc-wgan-1.ipynb
         （卷积 GAN）    smoothing.ipynb  （全连接 WGAN） （卷积 WGAN）
                          │
                     +label smoothing
                          │
                     dcgan-celeba.ipynb（DCGAN 论文配方 + CelebA 人脸完整工程）
```

| 文件 | 作用 | 本讲聚焦点 |
| --- | --- | --- |
| [pytorch_ipynb/gan/gan-conv.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan-conv.ipynb) | 卷积 GAN，MNIST，100 epoch | 4.1 |
| [pytorch_ipynb/gan/wgan-1.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/wgan-1.ipynb) | 全连接 WGAN，MNIST，100 epoch | 4.2 |
| [pytorch_ipynb/gan/dc-wgan-1.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dc-wgan-1.ipynb) | 卷积 WGAN，MNIST，100 epoch | 4.2 对照 |
| [pytorch_ipynb/gan/gan-conv-smoothing.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan-conv-smoothing.ipynb) | 卷积 GAN + label smoothing | 4.3 |
| [pytorch_ipynb/gan/dcgan-celeba.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb) | DCGAN 生成 CelebA 人脸，50 epoch | 4.4 |

与 u5-l5 相同，本讲的行号均指向 **.ipynb 的 JSON 源码行**（GitHub 打开文件后锚点可直接定位）。各关键段落的位置：

| notebook | 段落 | 行号 |
| --- | --- | --- |
| gan-conv | 超参数 | L106-L118 |
| gan-conv | GAN 类（含 Reshape1） | L173-L235 |
| gan-conv | 调试用 forward hook | L307-L320 |
| gan-conv | 对抗训练循环 | L959-L1022 |
| wgan-1 | WGAN 设置（clip、critic 次数） | L126-L139 |
| wgan-1 | wasserstein_loss 与模型 | L192-L226 |
| wgan-1 | 训练循环（含 critic 5 次循环与裁剪） | L868-L961 |
| dc-wgan-1 | RMSprop 优化器 | L362-L363 |
| dc-wgan-1 | 训练循环（critic 循环 + 裁剪） | L990-L1069 |
| gan-conv-smoothing | `valid*0.9` 两处 | L1000、L1011 |
| dcgan-celeba | CelebaDataset | L229-L253 |
| dcgan-celeba | transforms 与 DataLoader | L261-L288 |
| dcgan-celeba | DCGAN 模型与初始化 | L345-L528 |
| dcgan-celeba | 训练循环（D 先 G 后） | L1501-L1568 |

五份 notebook 都沿用全仓库的「Imports → Settings/Dataset → Model → Training → Evaluation」五段式结构（u1-l2）。

## 4. 核心概念与源码讲解

### 4.1 卷积 GAN：把两个对手都换成卷积

#### 4.1.1 概念说明

`gan-conv.ipynb` 对 u5-l5 基线做了一次「结构大手术」，训练数学完全不变。先看差异全表：

| 维度 | gan.ipynb（u5-l5） | gan-conv.ipynb（本讲） |
| --- | --- | --- |
| 生成器 | `Linear(75→128) → Linear(128→784)` | 投影 `Linear(100→3136)` + `Reshape` 成 \((64,7,7)\) + 4 层转置卷积 |
| 判别器 | `Linear(784→128) → Linear(128→1)` | 2 层步长卷积（\(28\to14\to7\)）+ `Flatten` + `Linear(1568→1)` |
| 正则化 | `Dropout(0.5)` | `BatchNorm`（Dropout 全部被注释掉） |
| 判别器末端 | `Sigmoid` 输出概率 | **无激活，logits 直出**（Sigmoid 被注释） |
| 损失函数 | `F.binary_cross_entropy`（概率版） | `F.binary_cross_entropy_with_logits`（logits 版） |
| LeakyReLU 斜率 | 默认 0.01 | `negative_slope=0.0001`（几乎退化为普通 ReLU） |
| 隐变量维度 | 75 | 100 |
| 参数量（G / D） | 110,864 / 100,609 | 344,208 / 4,025 |

最后两行值得驻足：卷积化之后**生成器参数翻了三倍、判别器参数缩到 1/25**。权值共享让判别器只需极少参数就能扫视整幅图像，而生成器要「从 100 维噪声长出一幅图」，负担更重。GAN 的两方容量天然不对称，本讲 4.2 与 4.3 的两个技巧都在**给判别器戴上镣铐**。

另有两个从上一讲继承的实现怪癖：`discriminator_forward` 内部引用全局变量 `model` 而非 `self`（u5-l5 已分析过）；device 硬编码为 `cuda:1`，单卡机器上需要改回 `cuda:0`（u1-l2 的知识）。

#### 4.1.2 核心流程

**生成器尺寸链**（转置卷积输出公式 \(o=(i-1)s-2p+k\)，u5-l2 已推导）：

| 层 | 配置 | 输出形状 |
| --- | --- | --- |
| `Linear(100→3136)` + BN + LeakyReLU | \(3136 = 64\times7\times7\) | \((N, 3136)\) |
| `Reshape1` | — | \((N, 64, 7, 7)\) |
| `ConvTranspose2d(64→32, k3, s2, p1)` | \((7-1)\cdot2-2+3=13\) | \((N, 32, 13, 13)\) |
| `ConvTranspose2d(32→16, k3, s2, p1)` | \((13-1)\cdot2-2+3=25\) | \((N, 16, 25, 25)\) |
| `ConvTranspose2d(16→8, k3, s1, p0)` | \((25-1)\cdot1-0+3=27\) | \((N, 8, 27, 27)\) |
| `ConvTranspose2d(8→1, k2, s1, p0)` + Tanh | \((27-1)\cdot1-0+2=28\) | \((N, 1, 28, 28)\) |

注意这里**不是**每层整齐地放大 2 倍：两次 ×2 上采样冲到 25，再用两个 stride=1 的层「微调」到 28。这与 u5-l2 讲过的「转置卷积尺寸配方强耦合」一脉相承——4.4 的 DCGAN 会给出更规整的对齐方案。

**判别器尺寸链**（卷积输出公式 \(o=\lfloor(w-k+2p)/s\rfloor+1\)，u3-l1）：

| 层 | 配置 | 输出形状 |
| --- | --- | --- |
| `Conv2d(1→8, k3, s2, p1)` + BN + LeakyReLU | \(\lfloor(28-3+2)/2\rfloor+1=14\) | \((N, 8, 14, 14)\) |
| `Conv2d(8→32, k3, s2, p1)` + BN + LeakyReLU | \(\lfloor(14-3+2)/2\rfloor+1=7\) | \((N, 32, 7, 7)\) |
| `Flatten` + `Linear(1568→1)` | \(32\times7\times7=1568\) | \((N,)\) logits |

训练循环与 u5-l5 的两幕戏**逐行同构**，唯一的实质变化是损失换成 logits 版：

```text
for epoch → for batch:
  真图 = (features - 0.5)*2 → 展平        # 值域对齐（不变）
  valid = 全 1;  fake = 全 0              # 标签（不变）
  ── 第一幕：训 G ──
  z ← U(-1,1)^100;  假图 = G(z)
  gener_loss = BCEWithLogits(D(假图), valid)    # ← 换成 logits 版
  ── 第二幕：训 D ──
  real_loss = BCEWithLogits(D(真图), valid)
  fake_loss = BCEWithLogits(D(假图.detach()), fake)
  discr_loss = 0.5*(real_loss + fake_loss)      # 0.5 缩放保留
```

为什么换 `with_logits` 版是「免费升级」：判别器末端本就要经过 Sigmoid 才能算 BCE，`binary_cross_entropy_with_logits` 把 Sigmoid 与 log 合成一个数值稳定的 `logsumexp` 计算，避免概率极接近 0/1 时 log 溢出，且对 logit 的梯度仍是 \(p-y\)。这正是 u5-l1、u5-l2 反复强调的「logits 配 with_logits 版」配套规则。

#### 4.1.3 源码精读

**超参数**：[pytorch_ipynb/gan/gan-conv.ipynb:L106-L118](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan-conv.ipynb#L106-L118) —— `random_seed=42`、双学习率 0.0001（比 u5-l5 的 0.001 低一个数量级）、`NUM_EPOCHS=100`、`BATCH_SIZE=128`、`LATENT_DIM=100`。学习率降低是配 BatchNorm + 卷积结构的常见调整。

**Reshape 桥接模块**：[pytorch_ipynb/gan/gan-conv.ipynb:L173-L176](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan-conv.ipynb#L173-L176) —— `Reshape1` 把 \((N,3136)\) 的向量重新「摊开」成 \((N,64,7,7)\) 的特征图。3136 恰好等于 \(64\times7\times7\)，这个数字必须与 Linear 输出维度严格咬合。生成器「先用全连接投影到低分辨率画布、再用转置卷积放大」是介于全连接与 DCGAN 之间的过渡形态（DCGAN 的做法见 4.4）。

**生成器定义**：[pytorch_ipynb/gan/gan-conv.ipynb:L184-L208](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan-conv.ipynb#L184-L208) —— 每个转置卷积后接 `BatchNorm2d` + `LeakyReLU(negative_slope=0.0001)`，`Dropout2d(p=0.2)` 全部被注释掉——正则化的接力棒从 Dropout 交给了 BatchNorm（u2-l4 建立的两类层）。末端 `Tanh()` 输出 \([-1,1]\)，与训练循环里的重缩放对齐。所有卷积 `bias=False`：跟在后面的 BatchNorm 自带偏置 \(\beta\)，卷积再带 bias 是冗余参数（4.4 的 DCGAN 沿用此约定）。

**判别器定义**：[pytorch_ipynb/gan/gan-conv.ipynb:L210-L226](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan-conv.ipynb#L210-L226) —— 两层 stride=2 卷积完成 \(28\to14\to7\) 的下采样（无任何池化，呼应 u3-l3 的 All-Conv 思想），`Flatten` 后接单个 `Linear(7*7*32, 1)`。注意末端的 `#nn.Sigmoid()` 被注释掉——判别器输出的是 **logits**，这正是能改用 `binary_cross_entropy_with_logits` 的前提。

**两个前向方法**：[pytorch_ipynb/gan/gan-conv.ipynb:L228-L235](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan-conv.ipynb#L228-L235) —— 与 u5-l5 相同的双前向设计；`discriminator_forward` 里 `pred.view(-1)` 把输出压平以匹配标签形状。

**训练循环**：[pytorch_ipynb/gan/gan-conv.ipynb:L959-L1022](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan-conv.ipynb#L959-L1022) —— 重缩放与展平在 [L965-L966](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan-conv.ipynb#L965-L966)，`valid`/`fake` 标签在 [L970-L971](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan-conv.ipynb#L970-L971)。第一幕在 [L982-L988](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan-conv.ipynb#L982-L988)：采样 \(z\sim U(-1,1)^{100}\)、生成假图、`F.binary_cross_entropy_with_logits(discr_pred, valid)`（非饱和技巧不变）。第二幕在 [L999-L1008](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan-conv.ipynb#L999-L1008)：真图支用 `valid`、假图支用 `fake` 且输入 `detach()`，`discr_loss = 0.5*(real_loss + fake_loss)`。注意循环里反复出现 `.view(targets.size(0), 1, 28, 28)`——因为判别器吃 4D 图像张量，而 `features` 在前面被展平过，进判别器前要重新塑形。

**评估采样**：[pytorch_ipynb/gan/gan-conv.ipynb:L1137-L1141](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan-conv.ipynb#L1137-L1141) —— `model.eval()` 后用 5 个噪声向量生成图像。这里 `eval()` 的意义与 u5-l5 不同：网络里没有 Dropout，但**有 BatchNorm**——eval 模式改用 running statistics（u2-l4 的双轨机制），保证同一 \(z\) 的生成结果可复现。

作者保存的运行日志（总耗时 18.47 分钟）：第 1 个 batch `Gen/Dis Loss: 0.7042/0.6743`，第 100 个 epoch 末 `0.7279/0.6904`、`0.7286/0.7083`——双方几乎完美地贴着 \(\ln 2 \approx 0.693\) 拉锯，是教科书级的健康均衡形态。

#### 4.1.4 代码实践

**实践目标**：亲手验证 4.1.2 的两条尺寸链，体会「卷积 GAN 的形状咬合」。notebook 里其实自带了一件趁手工具——被整段注释掉的调试 hook 单元。

**操作步骤**：

1. 打开 [pytorch_ipynb/gan/gan-conv.ipynb:L307-L320](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan-conv.ipynb#L307-L320) 的调试单元：它对生成器里每个 `ConvTranspose2d` 注册 forward hook（u4-l5 建立的模块级 hook 知识），把各层输出收集进 `outputs`。
2. 去掉三引号注释，在模型实例化之后运行它；再执行 [L1141](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan-conv.ipynb#L1141) 附近调试单元里的 `for i in outputs: print(i.size())`。
3. 或者不动 notebook，用独立的等价代码（示例代码）：

   ```python
   # 示例代码：打印生成器各转置卷积层的输出形状
   shapes = []
   def hook(module, inp, out):
       shapes.append((module.__class__.__name__, tuple(out.shape)))

   hooks = [m.register_forward_hook(hook) for m in model.generator
            if isinstance(m, torch.nn.ConvTranspose2d)]

   with torch.no_grad():
       z = torch.zeros((2, LATENT_DIM)).uniform_(-1.0, 1.0).to(device)
       model.generator_forward(z)

   for h in hooks: h.remove()          # 用完移除（u4-l5 的好习惯）
   for name, s in shapes: print(f'{name:20s} {s}')
   ```

**需要观察的现象**：打印出的四个形状依次为 \((2,32,13,13)\)、\((2,16,25,25)\)、\((2,8,27,27)\)、\((2,1,28,28)\)，与 4.1.2 表格逐行吻合；最终输出恰好是 \(28\times28\)，被 Tanh 压在 \([-1,1]\)。

**预期结果**：形状链完全一致（数学上必然，可本地复现）；若对判别器做同样实验，应看到 \((N,8,14,14)\to(N,32,7,7)\)。

#### 4.1.5 小练习与答案

**练习 1**：判别器末端注释掉了 `Sigmoid`，那 BCE 里需要的概率从哪来？

> **答案**：`F.binary_cross_entropy_with_logits` 内部先对 logits 做 Sigmoid 再算交叉熵，并把两步合成数值稳定的 `logsumexp` 形式。所以概率没有被省略，只是被「折叠」进了损失函数。这也是 u5-l1 建立的配套规则：网络输出 logits 时必须用 `with_logits` 版损失。

**练习 2**：`LeakyReLU(negative_slope=0.0001)` 与普通 ReLU 差别有多大？为什么不干脆写 `nn.ReLU()`？

> **答案**：斜率 0.0001 意味着负区间几乎置零，行为上与 ReLU 几乎不可区分；保留一点泄漏是为了让负区间也有微小梯度，避免「死单元」永远无法复活（u2-l3 提过 ReLU 的死区问题）。差别更多是作者的写作习惯——效果上二者在本设置中预期差异极小（具体差异待本地验证）。

**练习 3**：若把 `Reshape1` 改成 `view(input.size(0), 16, 14, 14)`（\(16\times14\times14=3136\)，元素个数不变），还需要改哪些地方？

> **答案**：至少要改下一个转置卷积的 `in_channels=64 → 16`（参数量随之变为 1/4），且整条尺寸链改变：\(14 \xrightarrow{k3,s2,p1} 27 \xrightarrow{k3,s2,p1} 53\)，已经超过 28，后面两层必须重新设计（缩小 stride 或 kernel）。这个练习说明：`Reshape` 的目标形状与后续所有层的通道数、尺寸配方强耦合，不是「元素数对上就行」。

### 4.2 WGAN：换掉 BCE 与 Sigmoid

#### 4.2.1 概念说明

`wgan-1.ipynb` 的开头 markdown 列出了相对普通 GAN 的五点差异（判别器线性输出、标签 \(-1/+1\)、Wasserstein 损失、权重裁剪、critic 训 5 次）。我们把它整理成「四件套 + 一个节奏」，并对照源码逐个落实：

1. **critic 不再输出概率**：末端 `Sigmoid` 被注释掉，输出变成无界的实数「打分」。它不再是判别器（判真假），而是 critic（评价样本有多像真图）。
2. **标签语义翻转**：真图标 \(-1\)、假图标 \(+1\)（普通 GAN 是 \(1/0\)）。标签只是损失里的系数，配合 `wasserstein_loss` 的定义取值。
3. **`wasserstein_loss(y_true, y_pred) = mean(y_true * y_pred)`**：一行乘积取代 BCE，直接实现 Wasserstein 距离对偶形式的经验估计。
4. **权重裁剪**：critic 每次更新后把所有权压回 \([-c, c]\)，粗暴地近似 1-Lipschitz 约束。**注意一个文档与代码不一致**：notebook 开头 markdown 写「clip to [-0.1, 0.1]」，而代码实际 `weight_clip_value = 0.01`——以代码为准（这是「读源码以变量来源为准」的又一实例，u3-l2、u3-l5、u5-l5 反复遇到）。
5. **更新节奏 5:1**：critic 每训 5 次，生成器才更新 1 次。

为什么要这么大动干戈？2.3 节说过：BCE/JSD 在分布不重叠时梯度枯竭。Wasserstein 距离则由 Kantorovich–Rubinstein 对偶给出：

\[
W(p_{\text{data}}, p_g) = \sup_{\|f\|_L \le 1}\;
\mathbb{E}_{x \sim p_{\text{data}}}[f(x)] - \mathbb{E}_{z \sim p_z}[f(G(z))]
\]

critic 就是学到的 \(f\)。**critic 越接近最优 \(f^*\)，\(\mathbb{E}[f(x)] - \mathbb{E}[f(G(z))]\) 就越接近真实距离 \(W\)**——这就是 critic 要多训几次的原因；而 Lipschitz 约束（这里的裁剪）保证这个上确界有意义。对生成器而言，最小化 \(-\mathbb{E}[f(G(z))]\) 相当于把假图搬向真图分布，**无论两分布是否重叠，这个梯度都存在且有方向**——WGAN 训练更稳的理论根基。

#### 4.2.2 核心流程

`wasserstein_loss` 虽只有一行，配合 \(-1/+1\) 标签可以写出全部四种损失的代数语义（\(D\) 输出为 critic 打分，不再是概率）：

| 损失 | 代码形式 | 展开后的期望 | 最小化它意味着 |
| --- | --- | --- | --- |
| critic 真图支 | `wasserstein_loss(valid=-1, D(x))` | \(-\mathbb{E}[D(x)]\) | 真图得分尽量**高** |
| critic 假图支 | `wasserstein_loss(fake=+1, D(G(z)))` | \(+\mathbb{E}[D(G(z))]\) | 假图得分尽量**低** |
| critic 总损失 | `real_loss + fake_loss` | \(\mathbb{E}[D(G(z))] - \mathbb{E}[D(x)] = -\hat{W}\) | 最大化距离估计 \(\hat{W}\) |
| 生成器损失 | `wasserstein_loss(valid=-1, D(G(z)))` | \(-\mathbb{E}[D(G(z))]\) | 假图得分向真图**看齐** |

一图流：

```text
                     ┌──────────── critic f（线性输出，权重裁剪在 [-0.01, 0.01]）────────────┐
                     │  真图得分 E[D(x)] 尽量高 ◄────────┐        ┌──────► 假图得分 E[D(G(z))] 尽量低 │
                     └──────────────────────────────────┼────────┼──────────────────────────────────┘
                                                        │        │
        critic 目标：拉大间距  Ŵ = E[D(x)] − E[D(G(z))]◄─┘        └─► G 目标：缩小间距（-E[D(G(z))]）
```

训练节奏（每个 batch）：

```text
z ← U(-1,1)^LATENT_DIM;  假图 = G(z)
gener_loss = -E[D(假图)]                      # 1 次生成器更新
五步范式（optim_gener）

for _ in range(5):                            # 5 次 critic 更新
    real_loss = -E[D(真图)];  fake_loss = +E[D(假图.detach())]
    discr_loss = real_loss + fake_loss
    五步范式（optim_discr）
    for p in critic.parameters():
        p.data.clamp_(-0.01, 0.01)            # 每次更新后裁剪
```

**读数方式彻底改变**：WGAN 的损失没有 \(\ln 2\) 参考线，取值可正可负；`-discr_loss`（即 \(\hat{W}\)）大致反映「真图与假图在 critic 眼中的距离」，理论上随训练下降。G/D 两条曲线是否平滑、是否发散，比绝对数值更有信息量。

#### 4.2.3 源码精读

**WGAN 专属设置**：[pytorch_ipynb/gan/wgan-1.ipynb:L126-L139](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/wgan-1.ipynb#L126-L139) —— `random_seed=0`、学习率 0.0005、`LATENT_DIM=50`，以及 `num_iter_critic = 5` 与 `weight_clip_value = 0.01` 两个新旋钮。

**wasserstein_loss**：[pytorch_ipynb/gan/wgan-1.ipynb:L192-L193](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/wgan-1.ipynb#L192-L193) —— `torch.mean(y_true * y_pred)`。\(y_{\text{true}}\) 取 \(\pm1\) 时，这一行就是「带符号平均得分」：\(-1\) 把「最大化得分」变成最小化问题，\(+1\) 保持「最小化得分」。

**模型定义**：[pytorch_ipynb/gan/wgan-1.ipynb:L202-L217](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/wgan-1.ipynb#L202-L217) —— 生成器仍是 `Linear(50→128) → Linear(128→784) → Tanh` 的全连接沙漏（**wgan-1 是全连接版**，卷积 WGAN 是姊妹篇 dc-wgan-1）；判别器 `Linear(784→128) → Linear(128→1)`，末端 [L215](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/wgan-1.ipynb#L215) 的 `#nn.Sigmoid()` 被注释并注明 "WGAN should have linear activation"。Dropout 同样被注释掉——被裁剪压扁的 critic 不需要额外正则。

**标签翻转**：[pytorch_ipynb/gan/wgan-1.ipynb:L883-L884](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/wgan-1.ipynb#L883-L884) —— `valid = -(torch.ones(...))`（真图 = \(-1\)）、`fake = torch.ones(...)`（假图 = \(+1\)），与普通 GAN 的 `1/0` 写法并排注释在上方，方便对照。

**生成器更新**：[pytorch_ipynb/gan/wgan-1.ipynb:L895-L910](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/wgan-1.ipynb#L895-L910) —— `gener_loss = wasserstein_loss(valid, discr_pred)` 即 \(-\mathbb{E}[D(G(z))]\)，随后照常五步范式。

**critic 五连更与裁剪**：[pytorch_ipynb/gan/wgan-1.ipynb:L918-L944](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/wgan-1.ipynb#L918-L944) —— `for _ in range(num_iter_critic)` 内：真图支 [L924](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/wgan-1.ipynb#L924)、假图支 [L931](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/wgan-1.ipynb#L931)（输入 `generated_features.detach()`），`discr_loss = real_loss + fake_loss` 在 [L934](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/wgan-1.ipynb#L934)（注意这里**没有** 0.5 缩放），每次 `optim_discr.step()` 之后 [L943-L944](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/wgan-1.ipynb#L943-L944) 用 `p.data.clamp_(-weight_clip_value, weight_clip_value)` 把 critic 所有权压回 \([-0.01, 0.01]\)。两个细节值得圈出：(a) 五次迭代里假图**不重新生成**，用的是本 batch 生成器更新前的那批 `generated_features.detach()`；(b) 循环外只记录最后一次的 `discr_loss`（[L947](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/wgan-1.ipynb#L947)）。

**卷积姊妹篇 dc-wgan-1**：把 4.1 的卷积结构与 WGAN 损失组合。判别器换成三层步长卷积（\(28\to14\to7\to4\)）加 `Linear(512, 1)`（[pytorch_ipynb/gan/dc-wgan-1.ipynb:L234-L255](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dc-wgan-1.ipynb#L234-L255)，`#nn.Sigmoid()` 在 [L253](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dc-wgan-1.ipynb#L253)），`wasserstein_loss` 定义在 [L198-L199](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dc-wgan-1.ipynb#L198-L199)；优化器改用 **RMSprop**（[L362-L363](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dc-wgan-1.ipynb#L362-L363)，学习率 0.00005）——这才是 WGAN 论文的推荐配置，wgan-1 用 Adam 属于未遵循论文的写法。训练循环里 critic 五连更与裁剪同款（[L1040-L1063](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dc-wgan-1.ipynb#L1040-L1063)），`discr_loss` 带回了 0.5 缩放（[L1055](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dc-wgan-1.ipynb#L1055)）；还有一个不起眼的不一致——噪声先验从 \(U(-1,1)\) 改成了 \(U(0,1)\)（[L1018](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dc-wgan-1.ipynb#L1018)）。

**日志对照（作者保存的运行结果）**：

| notebook | 第 1 个 batch（G/D） | 第 100 epoch 末（G/D） | 形态解读 |
| --- | --- | --- | --- |
| wgan-1（全连接） | \(-0.0519 / -0.2590\) | \(0.51\sim0.55 / -0.04\) | \(\hat{W}\approx0.04\)，损失曲线平滑（WGAN 论文卖点） |
| dc-wgan-1（卷积） | \(0.3318 / -0.0001\) | \(-0.004\sim-0.0041 / -0.0002\) | **两个期望都塌向 0**：critic 被裁剪成近似常数函数 |

dc-wgan-1 的形态值得警惕：critic 对真图、假图的打分都≈0，距离估计 \(\hat{W}\approx0.0004\)——\(\pm0.01\) 的硬裁剪把三层卷积 critic 的容量压没了。这是权重裁剪的著名病态（原论文自己也承认），后续的 WGAN-GP 用梯度惩罚替代裁剪来解决（本仓库未收录）。生成样本质量如何，待本地验证。

#### 4.2.4 代码实践

**实践目标**：用一个一维玩具验证「critic 损失确实是 Wasserstein 距离的估计」——两个方差相同、均值不同的一维高斯分布之间的 \(W\) 距离有解析解 \(W = |\mu_1 - \mu_2|\)。

**操作步骤**（示例代码，不依赖仓库数据，可独立运行）：

```python
# 示例代码：Wasserstein 距离玩具实验
import torch, torch.nn as nn

mu1, mu2 = 0.0, 2.0            # 解析解 W = |mu1 - mu2| = 2.0
torch.manual_seed(0)
critic = nn.Sequential(nn.Linear(1, 64), nn.ReLU(), nn.Linear(64, 1))
optim = torch.optim.RMSprop(critic.parameters(), lr=0.005)

for step in range(2000):
    x_real = torch.randn(256, 1) * 1.0 + mu1     # “真图”分布
    x_fake = torch.randn(256, 1) * 1.0 + mu2     # “假图”分布（固定不动）
    loss = -(critic(x_real).mean() - critic(x_fake).mean())  # 要最小化
    optim.zero_grad(); loss.backward(); optim.step()
    with torch.no_grad():
        for p in critic.parameters():
            p.clamp_(-0.01, 0.01)                # 与仓库相同的裁剪

with torch.no_grad():
    w_hat = critic(torch.randn(100000, 1) + mu1).mean() \
          - critic(torch.randn(100000, 1) + mu2).mean()
print(f'W 解析值 = {abs(mu1-mu2):.3f}, critic 估计 = {w_hat.item():.3f}')
```

**需要观察的现象**：critic 估计值随训练逼近 2.0；训练日志中 `-loss` 单调爬升到 2 附近后平台化。

**预期结果**：估计值落在 1.6～2.0 区间即算验证成功（裁剪过强会让 critic 欠拟合、估计偏低——这本身就是 4.2.3 dc-wgan-1 病态的微缩复现）；具体数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：WGAN 的 critic 为什么不能用 Sigmoid？

> **答案**：两个原因。其一，Sigmoid 把输出压进 \((0,1)\) 且两端饱和，critic 无法表达「距离很远」——两个分布相距 10 与相距 100 打出的分几乎相同，距离信号被压扁；其二，KR 对偶里的 \(f\) 是任意（Lipschitz）实值函数，线性输出层才能给出无界打分。Sigmoid + BCE 的组合恰是 WGAN 要替换掉的 JSD 目标。

**练习 2**：为什么 critic 每更新 5 次生成器才更新 1 次？反过来（G 5 次、critic 1 次）会怎样？

> **答案**：critic 越接近当前分布对下的最优 \(f^*\)，\(-\mathbb{E}[D(G(z))]\) 才越接近真正的 Wasserstein 方向，生成器才有高质量梯度；而生成器一动，最优 \(f^*\) 就变了，所以 critic 需要「追着更新」。反过来会让 G 追着一个陈旧且欠拟合的 critic 跑，方向噪声大，训练容易发散或停滞。

**练习 3**：`weight_clip_value` 取 0.01 与取 1.0 各有什么后果？

> **答案**：太小（0.01）→ critic 容量被压制，可能塌缩成近似常数函数，\(\hat{W}\) 恒接近 0，G 拿到的梯度虽稳定但很小（dc-wgan-1 的日志形态）；太大 → Lipschitz 约束形同虚设，critic 可以任意陡峭，距离估计失去意义且训练不稳。裁剪值是「约束强度 vs 容量」的折中，没有免费午餐——这正是后来 WGAN-GP 用梯度惩罚替代裁剪的动机。

### 4.3 label smoothing：给判别器戴上镣铐

#### 4.3.1 概念说明

`gan-conv-smoothing.ipynb` 开头一句话说明了自己的身份：「Same as gan-conv.ipynb but with label smoothing」——结构与超参完全不动，只在**两处**把真图标签从 1 改成 0.9。这是受控对比实验的又一范本（u2-l4 的方法论）。

label smoothing 来自 Salimans 等（2016）的《Improved Techniques for Training GANs》。直觉：BCE 是无情的损失——判别器想把真图的 logit 推向 \(+\infty\)（对应概率 1）才彻底满意。一个「过于自信」的判别器在真图上的梯度趋零，生成器失去学习信号（与 4.2 的动机殊途同归）。把目标从 1 软化为 0.9，等于给判别器的自信设了**天花板**：BCE 最优解是预测概率 = 目标，即 \(p^* = 0.9\)，对应最优 logit \(\log\frac{0.9}{0.1} = \ln 9 \approx 2.20\)。判别器到此为止，无法再「卷」，梯度信号得以持续存在。

注意这是**单侧平滑**：只把正标签（真图 = 1）软化为 0.9，负标签（假图 = 0）保持 0。如果把 0 也软化成 0.1，等于鼓励判别器给假图分配 10% 的「真图概率」，生成器靠「骗到一小部分」就能满足，可能停在次优解——所以标准做法只平滑正类。

它与 u5-l5 的 `0.5*` 缩放是一家人：都属「抑制判别器、保护生成器」的技巧家族，只是一个通过**改目标分布**实现，一个通过**改更新步长**实现。两者可以叠加使用。

#### 4.3.2 核心流程

改动前后只有两行的差异：

```text
gener_loss = BCEWithLogits(D(G(z)), valid*0.9)   # G 的目标也是 0.9
real_loss  = BCEWithLogits(D(x),    valid*0.9)   # D 的真图支目标是 0.9
fake_loss  = BCEWithLogits(D(G(z)).detach(), fake)  # 假图支保持 0（单侧）
```

均衡参考线的移动（定性推导）：若判别器已无法区分真假，其对假图输出 \(p=0.5\)（fake 支损失 \(\ln 2 \approx 0.693\)），对真图输出收敛到 \(p=0.9\)（real 支损失 \(-0.9\ln 0.9 - 0.1\ln 0.1 \approx 0.325\)），于是判别器总损失参考值约为 \(0.5\times(0.325+0.693) \approx 0.51\)，生成器损失参考值约为 \(-0.9\ln 0.5 - 0.1\ln 0.5 = \ln 2 \approx 0.693\) 的 0.9 倍偏移形态。**读 smoothing 版的日志时不要再拿 0.693 硬套判别器**。

#### 4.3.3 源码精读

**与基线唯一的设置差异**：[pytorch_ipynb/gan/gan-conv-smoothing.ipynb:L118](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan-conv-smoothing.ipynb#L118) —— `random_seed = 1`（gan-conv 是 42）；其余超参逐行相同。

**第一处平滑（生成器损失）**：[pytorch_ipynb/gan/gan-conv-smoothing.ipynb:L1000](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan-conv-smoothing.ipynb#L1000) —— `gener_loss = F.binary_cross_entropy_with_logits(discr_pred, valid*0.9)`。生成器的目标是「让判别器把假图认到 0.9」，而不是认到 1——判别器的自信上限同样约束了生成器要到达的终点。

**第二处平滑（判别器真图支）**：[pytorch_ipynb/gan/gan-conv-smoothing.ipynb:L1011](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan-conv-smoothing.ipynb#L1011) —— `real_loss = ... valid*0.9`；紧接着的假图支 [L1014](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan-conv-smoothing.ipynb#L1014) 用的仍是原封不动的 `fake`（0），落实「单侧平滑」；`0.5*` 缩放也保留在 [L1016](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/gan-conv-smoothing.ipynb#L1016)。

**日志对照（作者保存的运行结果，第 100 epoch 末）**：

| notebook | G 损失 | D 损失 | 备注 |
| --- | --- | --- | --- |
| gan-conv | 0.7279 / 0.7286 | 0.6904 / 0.7083 | 双双贴 \(\ln 2\) |
| gan-conv-smoothing | 0.7788 / 0.8079 | 0.6775 / 0.6748 | D 略低于无平滑版 |

smooth 版第 1 个 batch 的 G 损失是 0.5320（gan-conv 为 0.7042）——目标从 1 降到 0.9，起点损失自然更低。两版最终样本孰优，受种子影响大，建议按综合实践的方式多种子对比后再下结论（待本地验证）。

#### 4.3.4 代码实践

**实践目标**：亲眼确认「label smoothing 只是两行改动」，并扫描平滑强度观察判别器自信度的变化。

**操作步骤**：

1. 用文件对比工具（`diff` 或直接肉眼）逐单元对比 gan-conv 与 gan-conv-smoothing 两份 notebook，确认除 `random_seed` 与两处 `valid*0.9` 外再无差异。
2. 复制一份 gan-conv，把两处 `valid` 改成 `valid*alpha`，对 \(\alpha \in \{0.8, 0.9, 0.95\}\) 各训练 20 个 epoch（示例代码）：

   ```python
   # 示例代码：扫描平滑强度，观察 D 对真图的平均打分
   for alpha in [0.8, 0.9, 0.95]:
       # ……（重建模型与优化器，训练 20 epoch，损失用 valid*alpha）……
       with torch.no_grad():
           real_batch = next(iter(train_loader))[0].to(device)
           p_real = torch.sigmoid(model.discriminator_forward(
               ((real_batch - 0.5) * 2).view(-1, 1, 28, 28))).mean()
       print(f'alpha={alpha}: D(真图) 平均概率 = {p_real:.3f}（理论上限 ≈ {alpha}）')
   ```

**需要观察的现象**：训练充分后 `D(真图)` 的平均概率被压在各自的 \(\alpha\) 附近而难以越过；\(\alpha\) 越小，判别器损失曲线的「下限」越高。

**预期结果**：三种 \(\alpha\) 下判别器概率都不超过 \(\alpha\) 太多；生成样本质量差异待本地验证（GAN 方差大，建议 2～3 个种子）。

#### 4.3.5 小练习与答案

**练习 1**：为什么只把真图标签 1 平滑成 0.9，而假图标签 0 保持不动？

> **答案**：平滑假图标签（0 → 0.1）会让判别器的最优解变成「给假图留 10% 的真图概率」，生成器只需骗到这个较低的门槛就能把损失做小，可能停在次优；而平滑真图标签只是限制判别器别把真图推到无穷自信，判别器分辨真假的任务本身没有被削弱。这就是「单侧平滑」的理由（Salimans 等 2016）。

**练习 2**：`valid*0.9` 在 `binary_cross_entropy_with_logits` 下，判别器对真图的最优 logit 是多少？

> **答案**：BCE 最优解是预测概率等于目标，\(p^* = 0.9\)，对应 logit \(= \log(0.9/0.1) = \ln 9 \approx 2.20\)。无论训练多久，判别器把真图 logit 推过这个值只会增加损失——这就是「天花板」的定量说法。

**练习 3**：label smoothing 与 u5-l5 的 `0.5*(real_loss + fake_loss)` 有何异同？

> **答案**：同——都是「削弱判别器、让梯度信号持续供给生成器」的技巧，都不改变博弈的理论最优点。异——机制不同：平滑改的是**目标分布**（判别器收敛点的位置），缩放改的是**每次更新的有效步长**（收敛速度）；平滑是持久的结构性约束，缩放可以被更大的学习率抵消。两者在本 notebook 中同时生效。

### 4.4 DCGAN 全流程：CelebA 人脸生成

#### 4.4.1 概念说明

`dcgan-celeba.ipynb` 是本讲的压轴：把 Radford 等（2015）的 DCGAN 配方完整落在 202,600 张 CelebA 真实人脸（notebook 原文数字）上，生成 \(64\times64\) 的彩色人脸。它相对前三个变体是「全面升级」——数据、结构、初始化、优化器、评估全都换新：

| 维度 | 前三个变体 | dcgan-celeba |
| --- | --- | --- |
| 数据 | MNIST 28×28 灰度 | CelebA 178×218 彩色 → 裁剪 64×64 |
| 值域对齐 | `(x-0.5)*2` 手工重缩放 | `transforms.Normalize((0.5,)*3, (0.5,)*3)` 在数据管线里完成 |
| 生成器入口 | `Linear` 投影到 \(7\times7\) 画布 | \(z\) 直接视作 \((N,100,1,1)\) 的「1×1 特征图」，全转置卷积 |
| 初始化 | PyTorch 默认 | `weights_init`：卷积 N(0, 0.02)、BatchNorm γ N(1, 0.02) |
| 优化器 | Adam（默认 betas） | Adam(lr=2e-4, **betas=(0.5, 0.999)**) |
| 两幕戏顺序 | 先 G 后 D | **先 D 后 G** |
| 评估 | 训练后单独采样 5 张 | `fixed_noise` 64 个噪声每 epoch 生成 8×8 网格存档 |

DCGAN 论文配方的核心几条都能在本 notebook 对号入座：生成器全转置卷积 + BatchNorm + ReLU；判别器步长卷积（**无池化**）+ BatchNorm + LeakyReLU(0.2)；生成器末端 Tanh、判别器末端 Sigmoid；Adam 一阶动量 \(\beta_1\) 从 0.9 降到 0.5。

#### 4.4.2 核心流程

**数据流**：手动下载 img_align_celeba → `CelebaDataset`（只返回图像张量，无标签）→ train transform：`RandomResizedCrop(64, scale=(0.96,1.0), ratio=(0.95,1.05))` + `ToTensor` + `Normalize(0.5, 0.5)`（\([0,1]\to[-1,1]\)，与 Tanh 对齐，等价于 u5-l5 的手工重缩放但落在了 transform 里）→ DataLoader（`drop_last=True`）。

**生成器尺寸链**（\(z\) 是 \((N,100,1,1)\)，转置卷积公式 \(o=(i-1)s-2p+k\)）：

| 层 | 配置 | 输出 |
| --- | --- | --- |
| ConvTranspose(100→512, k4, s1, p0) | \((1-1)\cdot1-0+4=4\) | \(512\times4\times4\) |
| ConvTranspose(512→256, k4, s2, p1) | \((4-1)\cdot2-2+4=8\) | \(256\times8\times8\) |
| ConvTranspose(256→128, k4, s2, p1) | 同式 | \(128\times16\times16\) |
| ConvTranspose(128→64, k4, s2, p1) | 同式 | \(64\times32\times32\) |
| ConvTranspose(64→3, k4, s2, p1) + Tanh | 同式 | \(3\times64\times64\) |

对比 4.1 的「7→13→25→27→28」：DCGAN 用 **k4/s2/p1 的整齐配方**让每层恰好翻倍（u5-l2 讲过的「k4s2p1 精确加倍」），无需任何补丁层。判别器是完全镜像的步长卷积链 \(64\to32\to16\to8\to4\to1\)，末端 \(1\times1\times1\) 的「图」配合 `Sigmoid` 就是每张图一个概率。参数量：G 3,576,704、D 2,765,633（torchsummary 输出）。

**训练两幕戏（注意顺序反转）**：

```text
for epoch → for batch（features 只有图，没有标签）:
  ── 第一幕：训 D ──
  random_vec = randn(128, 100, 1, 1);  fake_images = G(random_vec)
  real_loss = BCE(D(真图), 1);  fake_loss = BCE(D(fake_images.detach()), 0)
  optim_discr: 五步范式
  ── 第二幕：训 G ──
  gener_loss = BCE(D(fake_images), 1)      # 复用同一批假图，不 detach
  optim_gener: 五步范式
  ── epoch 末尾 ──
  images_from_noise.append(make_grid(G(fixed_noise)))   # 64 个固定噪声 → 8×8 网格
```

#### 4.4.3 源码精读

**设置与多种子**：[pytorch_ipynb/gan/dcgan-celeba.ipynb:L140-L153](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L140-L153) —— `RANDOM_SEED=42`、双学习率 0.0002、`NUM_EPOCHS=50`、`BATCH_SIZE=128`、`IMAGE_SIZE=(64,64,3)`、`LATENT_DIM=100`、`NUM_MAPS_GEN/DIS=64`。与前面 notebook 只调 `torch.manual_seed` 不同，这里 `random.seed`、`np.random.seed`、`torch.manual_seed` 三管齐下（u1-l3 的 `set_all_seeds` 同款思想）。

**无标签 Dataset**：[pytorch_ipynb/gan/dcgan-celeba.ipynb:L229-L253](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L229-L253) —— `CelebaDataset` 是 u4-l2 三要素模板（`__init__` 扫目录建清单 [L236](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L236)、`__getitem__` 惰性读图 [L242](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L242)、`__len__` [L252](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L252)）的一个特殊实例：`__getitem__` **只返回图像张量**，没有标签——GAN 是无监督模型，CelebA 的属性文件根本用不上（notebook 在 [L182](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L182) 明说只需 img_align_celeba 子目录）。

**transforms 与值域对齐**：[pytorch_ipynb/gan/dcgan-celeba.ipynb:L261-L275](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L261-L275) —— train 支用 `RandomResizedCrop` 做轻微的随机缩放裁剪增强（u4-l3 的知识：随机变换只挂训练集），`Normalize((0.5,0.5,0.5),(0.5,0.5,0.5))`（[L268](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L268)）把每个通道 \([0,1]\) 映到 \([-1,1]\)——u5-l5 的 `(x-0.5)*2` 在数据管线里的规范写法。valid 支的 transform 定义了但本 notebook 未使用。

**DataLoader 与 drop_last**：[pytorch_ipynb/gan/dcgan-celeba.ipynb:L282-L288](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L282-L288) —— `drop_last=True` 在这里不是可选项：训练循环里 `random_vec = torch.randn(BATCH_SIZE, ...)` 是**固定长度**的（[L1522](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L1522)），若最后一个不满批的真图数量 < 128，假图批量与真图批量、标签向量的长度就会错位。202,600 张 ÷ 128 ≈ 1582.8，丢弃尾批后每 epoch 恰好 1582 批（与日志一致）。

**DCGAN 初始化**：[pytorch_ipynb/gan/dcgan-celeba.ipynb:L345-L356](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L345-L356) —— `weights_init` 按类名分派：名字含 `Conv` 的模块权重 ~ N(0, 0.02)；含 `BatchNorm` 的 γ ~ N(1, 0.02)、β = 0。注意 BatchNorm 的 γ 初始化在 1 附近而不是 0.02——γ 是缩放因子，起点应保持单位缩放（u2-l4 的 BatchNorm 参数语义）。它在 [L528](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L528) 通过 `model.apply(weights_init)` 递归应用到所有子模块（apply 会访问每个子模块，包括 Sequential 内部）。

**生成器定义**：[pytorch_ipynb/gan/dcgan-celeba.ipynb:L374-L417](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L374-L417) —— 入口 [L378](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L378) `ConvTranspose2d(LATENT_DIM, NUM_MAPS_GEN*8, kernel_size=4, stride=1, padding=0)` 把 \(1\times1\) 的噪声「图」放大成 \(4\times4\times512\)；之后三层 k4/s2/p1 逐级翻倍到 \(64\times64\)；末端 `Tanh()` 在 [L416](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L416)。每层注释里都标了尺寸，是「代码即文档」的好范例。

**判别器定义**：[pytorch_ipynb/gan/dcgan-celeba.ipynb:L419-L456](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L419-L456) —— 镜像的五个卷积，第一个不接 BatchNorm（靠近输入的层加 BN 会损害对原始分布的把握，DCGAN 论文经验），激活统一 `LeakyReLU(0.2)`；末端 [L453](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L453) `Conv2d(NUM_MAPS_DIS*8, 1, k4, s1, p0)` + `Sigmoid()`（[L455](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L455)）输出每图一个概率——注意这里回到了 **Sigmoid + 概率版 BCE** 的组合（与 u5-l5 相同、与 gan-conv 相反），因为用的是 `nn.BCELoss`。

**损失、标签与固定噪声**：[pytorch_ipynb/gan/dcgan-celeba.ipynb:L516-L528](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L516-L528) —— `loss_function = nn.BCELoss()`（概率版，吃 Sigmoid 后的概率）、`real_label=1`/`fake_label=0`（[L518-L519](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L518-L519)），以及关键的 `fixed_noise = torch.randn(64, LATENT_DIM, 1, 1, device=DEVICE)`（[L524](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L524)）——训练开始前一次性采好的 64 个噪声，此后每个 epoch 用**同一批** \(z\) 生成网格，纵向对比生成质量演变（u5-l5「固定噪声向量」方法论的工程化）。

**Adam 的 DCGAN 修正**：[pytorch_ipynb/gan/dcgan-celeba.ipynb:L614-L621](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L614-L621) —— 两个 Adam 都传 `betas=(0.5, 0.999)`：一阶动量系数 \(\beta_1\) 从默认 0.9 降到 0.5。GAN 的目标随对手更新不断漂移，动量会带着「过时的方向」冲，减半后更新更贴近当前梯度。

**训练循环（D 先 G 后）**：[pytorch_ipynb/gan/dcgan-celeba.ipynb:L1501-L1568](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L1501-L1568) —— 第一幕训 D（[L1507-L1533](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L1507-L1533)）：标签向量用 `torch.full` 构造（[L1514](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L1514)），真图支 [L1517](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L1517)；假图先由 `random_vec` 生成（[L1522-L1523](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L1522-L1523)），进 D 时 `detach()`（[L1525](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L1525)）；`discr_loss = 0.5*(real_loss + fake_loss)`（[L1530](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L1530)）。第二幕训 G（[L1536-L1545](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L1536-L1545)）：**复用同一批 `fake_images` 且不 detach**（梯度穿过 D 流回 G），目标标签复用 `real_label_vec`——注意此时 D 的权重已被第一幕更新过，G 面对的是「最新版」对手。epoch 末尾（[L1561-L1564](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L1561-L1564)）在 `no_grad` 下用 `fixed_noise` 生成 64 张图并 `vutils.make_grid` 拼成 8×8 网格存入列表。

**结果可视化**：[pytorch_ipynb/gan/dcgan-celeba.ipynb:L1766](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L1766) 每 5 个 epoch 抽一张网格回放演变；[L1790](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L1790) 展示最终大图。

**日志解读（作者保存的运行结果，总耗时 122.33 分钟）**：第 1 个 epoch 开局 `Gen/Dis Loss: 5.0977/0.8404`，第 100 个 batch 就出现 `33.1598/0.0001`——判别器一度把损失打到近乎 0（绝对自信地分辨真假），生成器损失飙到 33；到第 50 个 epoch 稳定在 `5.2~7.0 / 0.02~0.17`。与 MNIST 上的「双双贴 \(\ln 2\)」不同，CelebA 上判别器始终明显占优——真实人脸分布远比手写数字复杂，这也解释了为什么 DCGAN 配方（初始化、动量修正、固定噪声监控）在这里一个都不能少。

#### 4.4.4 代码实践

**实践目标**：不下载 1.4GB 的 CelebA，把 DCGAN 结构移植到 MNIST 上跑通，并产出一张 64 样本网格图——这正是综合实践第三阶段的热身。

**操作步骤**（示例代码 + 改动清单）：

1. 复制 dcgan-celeba 的 Model 单元（`weights_init` 与 `DCGAN` 类原样保留）。
2. 按以下清单适配 MNIST（示例改动）：

   ```python
   # 示例代码：DCGAN-MNIST 适配版的关键改动
   IMAGE_SIZE = (64, 64, 1)                     # ① 灰度单通道

   transform = transforms.Compose([
       transforms.Resize(IMAGE_SIZE[0]),        # ② 28×28 拉到 64×64
       transforms.ToTensor(),
       transforms.Normalize((0.5,), (0.5,)),    # ③ 单通道统计量 → [-1,1]
   ])

   # 模型内只需改两处通道数：
   #   生成器末端 ConvTranspose2d(NUM_MAPS_GEN, 1, ...)   # 输出 1 通道
   #   判别器首层 Conv2d(1, NUM_MAPS_DIS, ...)            # 输入 1 通道
   ```

3. 用 [pytorch_ipynb/gan/dcgan-celeba.ipynb:L524](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gan/dcgan-celeba.ipynb#L524) 的写法准备 64 个固定噪声，训练若干 epoch 后生成 8×8 网格（示例代码）：

   ```python
   # 示例代码：64 样本网格
   fixed_noise = torch.randn(64, LATENT_DIM, 1, 1, device=DEVICE)
   model.eval()
   with torch.no_grad():
       fake = model.generator_forward(fixed_noise).detach().cpu()
   grid = np.transpose(vutils.make_grid(fake, padding=2, normalize=True, nrow=8), (1, 2, 0))
   plt.figure(figsize=(8, 8)); plt.axis('off'); plt.imshow(grid.squeeze(), cmap='binary')
   ```

**需要观察的现象**：前几个 epoch 的网格是噪声块；随后出现模糊的浅色团块；继续训练逐渐浮现数字笔画轮廓。判别器末端是 `Sigmoid`，可随时打印 `D(真图)`、`D(假图)` 的均值看双方拉锯。

**预期结果**：MNIST 上 20～30 个 epoch 内应能看到可辨认的数字形态（DCGAN 在 MNIST 上比 CelebA 容易得多）；具体质量与耗时待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 \(z\) 要 reshape 成 \((N, 100, 1, 1)\) 而不是直接喂 \((N, 100)\) 的向量？

> **答案**：`ConvTranspose2d` 只接受 4D 的 \((N, C, H, W)\) 输入。把 \(z\) 看作 \(1\times1\) 的 100 通道「特征图」，第一个转置卷积（k4, s1, p0）就能把它放大成 \(4\times4\)——整个生成器从头到尾只用卷积算子，没有任何全连接层。这正是 gan-conv 用「Linear 投影 + Reshape」想要达成的目标的更彻底版本。

**练习 2**：`betas=(0.5, 0.999)` 相比默认 `(0.9, 0.999)` 改的是什么？为什么对 GAN 有益？

> **答案**：改的是一阶动量系数 \(\beta_1\)（梯度均值的衰减率）。GAN 里每个网络的目标都随对手的更新而漂移，是非平稳目标；\(\beta_1=0.9\) 的动量会把较旧梯度的平均值带进当前更新，方向震荡加剧。降到 0.5 让更新更依赖近期梯度，DCGAN 论文的实验发现这显著改善稳定性。

**练习 3**：本 notebook 的两幕戏顺序是「先 D 后 G」，u5-l5 与 gan-conv 是「先 G 后 D」。顺序是硬约束吗？

> **答案**：不是。两种顺序在数学上都成立，实践中都有大量成功案例（原始 GAN 论文甚至建议 D 每轮多更新几次）。真正的约束是**依赖关系**：dcgan 里第二幕复用第一幕生成的 `fake_images` 且不 detach，这要求 G 的损失计算发生在这批假图的计算图还活着的时候——代码组织必须自洽。对比两个 notebook 的变量流就能看出作者按各自顺序重排了依赖（u5-l5 练习 3 的结论在这里再次得到印证）。

## 5. 综合实践

**任务**：在同一随机种子下对比 `gan-conv`（BCE 卷积 GAN）与 `wgan-1`（Wasserstein 全连接 GAN）在 MNIST 上的训练稳定性——每 5 个 epoch 保存一次固定噪声生成图；再用 DCGAN 结构生成一张 64 样本网格图。

**先诚实声明混杂变量**：gan-conv 与 wgan-1 同时在**结构**（卷积 vs 全连接）与**损失**（BCE vs Wasserstein）两个维度上不同，直接对比只能得到「这两个配方整体谁更稳」，无法归因。建议把对照矩阵扩成 2×2——正好凑齐仓库现有 notebook：

| | BCE 损失 | Wasserstein 损失 |
| --- | --- | --- |
| 全连接结构 | gan.ipynb（u5-l5） | wgan-1.ipynb |
| 卷积结构 | gan-conv.ipynb | dc-wgan-1.ipynb |

这样「同结构不同损失」「同损失不同结构」都能各得一组受控对比。

**阶段一：搭建统一的采样协议**（示例代码，适用于所有四个 notebook）：

```python
# 示例代码：固定种子 + 固定噪声 + 周期性存档
torch.manual_seed(42)
# ……（实例化模型与两个优化器，注意种子要在建模前设定，u1-l3）……

z_fixed = torch.zeros((64, LATENT_DIM)).uniform_(-1.0, 1.0).to(device)  # 各 notebook 的 z 先验不同，按其原样采样
snapshots = {}

# 训练循环内，每个 epoch 末：
if (epoch + 1) % 5 == 0:
    model.eval()                                  # 关 BatchNorm 的随机性（4.1.3）
    with torch.no_grad():
        imgs = model.generator_forward(z_fixed).view(-1, 1, 28, 28).cpu()
    snapshots[epoch + 1] = imgs                   # 事后用 make_grid / imshow 拼网格
    model.train()
```

注意：wgan-1/dc-wgan-1 的 `LATENT_DIM`（50/100）与 z 先验范围（\(U(-1,1)\)/\(U(0,1)\)）各不相同，按各自 notebook 的原样保留；对比的是「各自配方在各自协议下的稳定性」。

**阶段二：训练并按统一标准判读**。对四个 notebook（各训 30～50 个 epoch）填写记录表：

| notebook | 损失是否发散/NaN | 损失曲线是否平滑 | 第 5/10/…/30 epoch 网格图的多样性 | 是否出现模式崩塌（网格内形态雷同） |
| --- | --- | --- | --- | --- |
| gan.ipynb |  |  |  |  |
| gan-conv.ipynb |  |  |  |  |
| wgan-1.ipynb |  |  |  |  |
| dc-wgan-1.ipynb |  |  |  |  |

判读标准（本讲各节的结论落地）：BCE 组看双方是否贴 \(\ln 2\) 拉锯（4.1）、D 是否过度自信（4.3）；WGAN 组看曲线是否平滑、\(-\)discr_loss 是否随训练收敛、critic 是否塌缩成常数（4.2.3 dc-wgan-1 的病态）。

**阶段三：DCGAN 64 样本网格**。按 4.4.4 的 MNIST 适配版（或下载 CelebA 跑原版）训练后，用 `fixed_noise`（64 个）+ `vutils.make_grid(nrow=8)` 生成最终网格，并与第 5、10、20 epoch 的存档并排展示演变轨迹。

**预期结果**（待本地验证）：MNIST 上四个配方都应能产生可辨认数字，但稳定性排序预期是「WGAN 组曲线更平滑、BCE 组更震荡」（WGAN 论文的核心主张）；dc-wgan-1 若复现 4.2.3 的 critic 塌缩形态，其网格质量应明显偏弱——这本身就是最有价值的实验发现。最终用 3～5 句话总结：**结构卷积化与损失 Wasserstein 化，各自对稳定性贡献了什么？**

## 6. 本讲小结

- **卷积 GAN（gan-conv）**：生成器改为「Linear 投影 \(100\to3136\) + Reshape 成 \((64,7,7)\) + 4 层转置卷积」，判别器改为 2 层步长卷积 + 单 Linear 读出；正则化从 Dropout 换成 BatchNorm，判别器 logits 直出配 `binary_cross_entropy_with_logits`（u5-l1 配套规则），两幕戏训练数学不变，最终双方贴 \(\ln 2\) 拉锯（0.73/0.70）。
- **WGAN 四件套 + 节奏**：critic 线性输出（去 Sigmoid）、`wasserstein_loss = mean(y_true*y_pred)`、真图 \(-1\)/假图 \(+1\) 标签、每次更新后权重裁剪 \(\pm0.01\)（notebook 文档写 0.1，以代码为准）、critic 每训 5 次生成器更 1 次；critic 损失 \(\mathbb{E}[D(\text{fake})]-\mathbb{E}[D(\text{real})]=-\hat{W}\)，读数不再有 \(\ln 2\) 参考线。
- **权重裁剪是粗暴的 Lipschitz 约束**：裁剪过强会把 critic 压成近似常数函数——dc-wgan-1 的日志（两个损失都 \(\approx0\)）就是活生生的病态案例；WGAN 稳定性的代价由 critic 容量支付。
- **label smoothing**：`valid*0.9` 两行改动把判别器对真图的自信封顶在 logit \(\ln 9\approx2.20\)，防其碾压生成器；单侧平滑（假图标签保持 0）避免给生成器留次优退路；与 `0.5*` 缩放同属「削弱判别器」家族。
- **DCGAN 配方（dcgan-celeba）**：\(z\) 作为 \((N,100,1,1)\) 的 1×1 特征图、k4/s2/p1 整齐翻倍的转置卷积链、判别器无池化全步长卷积、`weights_init`（卷积 N(0,0.02)）、`Adam(betas=(0.5,0.999))`、两幕戏反转为先 D 后 G、`fixed_noise` 64 个噪声每 epoch 存 8×8 网格；`Normalize(0.5,0.5)` 在数据管线完成 Tanh 值域对齐，`drop_last=True` 保证固定长度噪声批量不错位。
- **读日志要认清形态**：MNIST 上 BCE-GAN 均衡是双方贴 \(\ln 2\)；CelebA 上判别器长期占优（D 损失 0.02～0.17、G 5～7）是高维真实数据的常态——损失形态没有普世标准，配方的意义在于把失衡控制在「生成器还能学」的范围内。

## 7. 下一步学习建议

本讲走完了 GAN 家族在 `pytorch_ipynb/gan/` 下的全部主要形态（仅剩一份未读的姊妹篇）。建议的后续路线：

1. **同套 DCGAN 换数据**：读 `pytorch_ipynb/gan/dcgan-cats-and-dogs.ipynb`——同一配方搬到猫狗照片上，观察数据分布变化对稳定性的影响，检验你在综合实践里总结的判读标准。
2. **生成模型收官后转向序列**：第 6 单元 u6-l1 将从 IMDb 情感分类开始 RNN 之旅；生成与序列在 u6-l4 的 char-RNN 文本生成中会再次相遇——那里的采样温度与本讲固定噪声评估是同一类「控制变量看质量」的方法论。
3. **顺藤摸瓜读论文**：WGAN 的权重裁剪病态引出 WGAN-GP（Gulrajani 等，2017，梯度惩罚），DCGAN 配方则是后来 StyleGAN 系列的起点——仓库虽未收录，但你现在已具备直接读懂它们实现的全部基础。
4. **动手方向**：把综合实践的 2×2 矩阵扩展成「+ label smoothing」的 2×2×2，或给 DCGAN-MNIST 加上条件标签（对照 u5-l4 的 CVAE 思路实现 cGAN），都是检验本讲掌握程度的好题目。
