# 第 1 讲 · 项目总览：Muon 是什么、为什么值得学

## 1. 本讲目标

读完本讲，你应该能够：

1. 用一句话说清 **Muon 是什么**：一个专门为神经网络**隐藏层权重矩阵**设计的优化器，其余参数（embedding、输出头、增益/偏置）仍然用 AdamW 训练。
2. 说出 Muon 的来源与战绩：它诞生于 Keller Jordan 的 NanoGPT / CIFAR-10 训练速度竞赛（speedrun），后来被 Kimi.ai 等机构用于大规模语言模型训练。
3. 记住一句话级的算法摘要：**先跑普通 SGD 动量，再把更新用 Newton-Schulz 迭代正交化**。
4. 看懂 README 中的标准用法代码：`use_muon=True/False` 参数组 + `MuonWithAuxAdam`。
5. 亲手跑通第一个脚本：实例化 `SingleDeviceMuonWithAuxAdam`，观察每个参数组被自动填充的默认超参。

## 2. 前置知识

本讲是整套手册的第一篇，不假设你读过任何深度学习框架源码。只需要理解下面几个概念（都用大白话解释）：

| 术语 | 通俗解释 |
|---|---|
| 参数 / 权重矩阵 | 神经网络里可学习的数据。隐藏层的参数通常是二维矩阵（形状如 512×256），本讲的主角就是它们。 |
| 优化器（optimizer） | 决定「拿到梯度之后，参数往哪个方向、走多远」的算法，例如 SGD、Adam、AdamW。 |
| 梯度下降与学习率（lr） | 梯度指出下坡方向，学习率决定每步走多远。 |
| 动量（momentum） | 把历史梯度做指数加权平均再走，像小球带惯性，能冲过噪声和震荡。 |
| 权重衰减（weight decay） | 每步把参数略微乘小一点（如 ×0.999），防止参数无限变大。AdamW 就是 Adam + 解耦权重衰减。 |
| 正交矩阵 | 满足 \( Q^\top Q = I \) 的方阵。直觉上：它只做「旋转/反射」，**不拉伸任何方向**，所有列向量的长度都是 1。 |
| 谱范数（spectral norm） | 一个矩阵对向量最多能拉伸多少倍，等于最大奇异值。本讲只需这个直觉，第 2 单元再展开。 |
| 奇异值分解（SVD） | 任何矩阵都能写成 \( G = U S V^\top \)，其中 \( S \) 是对角矩阵，对角线上的数叫奇异值，描述 \( G \) 在各方向上的拉伸强度。 |
| embedding / head / gains / biases | 输入查表矩阵 / 网络最后一层（输出头）/ 隐藏层的缩放系数和偏置。这些**不是**标准二维权重，README 建议它们继续用 AdamW。 |
| speedrun | 「用最少算力/时间把模型训到某个精度」的竞赛，例如 2.6 A100 秒训 CIFAR-10 到 94% 准确率。 |
| bfloat16 | 一种 16 位浮点数，指数位与 float32 相同、精度更低，GPU 上算得快、数值范围还不容易溢出。 |

如果你对「正交矩阵」「SVD」还不熟也不必担心——本讲只需要「把更新的各方向拉伸强度拉平」这个直觉，数学细节在第 2 单元专门讲。

## 3. 本讲源码地图

Muon 是一个**极小而精**的研究型项目，整个仓库核心代码只有一个文件：

| 文件 | 作用 | 本讲是否涉及 |
|---|---|---|
| `README.md` | 项目定位、安装方式、标准用法、调参建议、战绩与延伸阅读 | ✅ 重点阅读 |
| `muon.py` | **全部核心实现**（约 290 行）：3 个函数 + 4 个优化器类 | ✅ 读顶部两个函数和 Muon 类 docstring |
| `setup.py` | 把单文件 `muon.py` 打包发布为 `muon-optimizer` 包 | 下一讲（u1-l2）精读 |
| `LICENSE` | MIT 许可证 | 不涉及 |

`muon.py` 内部结构一览（本讲先建立地图，后续各讲逐段精读）：

| 位置 | 内容 |
|---|---|
| [muon.py:L5-L31](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L5-L31) | `zeropower_via_newtonschulz5`：Newton-Schulz 迭代正交化（本讲 4.2） |
| [muon.py:L34-L41](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L34-L41) | `muon_update`：动量 + 正交化 + 缩放，组合成一次 Muon 更新（本讲 4.2） |
| [muon.py:L44-L96](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L44-L96) | `Muon`：分布式版优化器类，含核心 docstring（本讲 4.3） |
| [muon.py:L99-L127](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L99-L127) | `SingleDeviceMuon`：单机版，最适合入门阅读 |
| [muon.py:L130-L135](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L130-L135) | `adam_update`：30 行手写 AdamW 更新 |
| [muon.py:L138-L225](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L138-L225) | `MuonWithAuxAdam`：一个优化器同时跑 Muon + AdamW（分布式版） |
| [muon.py:L228-L287](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L228-L287) | `SingleDeviceMuonWithAuxAdam`：上述类的单机版，本讲综合实践的主角 |

## 4. 核心概念与源码讲解

### 4.1 README.md：项目定位、标准用法与战绩

#### 4.1.1 概念说明

README 开宗明义地回答了「Muon 是什么」：

> **Muon 是一个为神经网络隐藏层（hidden layers）权重设计的优化器。**
> 其他参数——例如 embedding、分类头、隐藏层的增益（gains）和偏置（biases）——应当继续使用标准 AdamW 优化。

这句话定义了 Muon 的**适用边界**：它不是「万能替换 AdamW」，而是**只接管隐藏层二维权重矩阵**，剩下的参数交给普通 AdamW。这一点贯穿整个项目的设计（第 3 单元会看到 `MuonWithAuxAdam` 就是为了「一个优化器管两类参数」而生）。

再用一句话概括 Muon 的算法（来自 `Muon` 类 docstring，4.3 节精读）：

> **Muon 内部先跑标准 SGD 动量，然后做一步「正交化后处理」：把每个二维参数的更新替换成最接近的正交矩阵。**

**它为什么值得学？** 三个理由：

1. **战绩硬**：把 CIFAR-10 训到 94% 的纪录从 3.3 A100 秒压到 2.6 A100 秒；把 GPT-2 (XL) 水平的训练成本压到 175 美元；被 Kimi.ai 用于前沿大模型训练。
2. **代码极小**：核心实现只有一个约 290 行的 `muon.py`，非常适合逐行精读、彻底吃透一个现代优化器。
3. **思想可迁移**：「动量 + 正交化后处理」和「以谱范数为单位的学习率」是理解一批新优化器（Shampoo、SOAP 等）的钥匙。

#### 4.1.2 核心流程

README 的行文顺序就是一份使用说明书，阅读路线如下：

```text
标题：定位（hidden layers 专用优化器）
  └─ Installation：pip 直接从 GitHub 安装
      └─ Usage：标准用法代码（use_muon 参数组 + MuonWithAuxAdam）
          └─ Hyperparameter tuning：哪些超参要调、哪些不用调
              └─ Benchmarks / Accomplishments：对比实验与战绩
                  └─ More learning resources：延伸阅读
```

#### 4.1.3 源码精读

**① 项目定位与出处**：[README.md:L1-L3](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L1-L3)

```markdown
# Muon: An optimizer for the hidden layers of neural networks

This repo contains an implementation of the `Muon` optimizer originally described in [this thread] ...
```

标题点明定位；正文说明本仓库是 Keller Jordan 在 X（推特）上的帖子和文章中所述算法的参考实现。

**② 安装方式**：[README.md:L5-L9](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L5-L9)

```text
pip install git+https://github.com/KellerJordan/Muon
```

直接从 GitHub 仓库安装，不需要先手动下载。（打包细节在下一讲剖析 `setup.py`。）

**③ 标准用法：把一个 AdamW 训练脚本改造成 Muon**：[README.md:L13-L33](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L13-L33)

```python
# optimizer = torch.optim.AdamW(model.parameters(), lr=3e-4, betas=(0.90, 0.95), weight_decay=0.01)

# To replace the above, do the following:

from muon import MuonWithAuxAdam
hidden_weights = [p for p in model.body.parameters() if p.ndim >= 2]
hidden_gains_biases = [p for p in model.body.parameters() if p.ndim < 2]
nonhidden_params = [*model.head.parameters(), *model.embed.parameters()]
param_groups = [
    dict(params=hidden_weights, use_muon=True,
         lr=0.02, weight_decay=0.01),
    dict(params=hidden_gains_biases+nonhidden_params, use_muon=False,
         lr=3e-4, betas=(0.9, 0.95), weight_decay=0.01),
]
optimizer = MuonWithAuxAdam(param_groups)
```

这段代码是全项目最重要的使用范式，逐行拆解：

- 注释掉的一行是「改造前」：所有参数一个 AdamW 搞定。
- 改造的关键是**把参数分成两组**，塞进一个优化器：
  - `p.ndim >= 2` 的隐藏层权重 → `use_muon=True` 组，用 Muon 训练（注意 Muon 组**没有** `betas`，因为动量只有一个系数 `momentum=0.95`）；
  - 一维的增益/偏置（`ndim < 2`）、输出头、embedding → `use_muon=False` 组，用内部 AdamW 训练，超参和原来 AdamW 一致。
- `MuonWithAuxAdam(param_groups)` 之后，训练循环里只需要 `optimizer.step()` 一个调用，不再需要分别 step 两个优化器。

**④ 适用范围的补充说明**：[README.md:L35-L36](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L35-L36)

README 提醒：示例中的 `model.body` / `model.head` / `model.embed` 要换成你自己模型里对应的模块；对卷积网络，**除第一层外的所有卷积核**都可以用 Muon 训练，其余用 AdamW。

**⑤ 调参建议**：[README.md:L44-L47](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L44-L47)

> 默认的 momentum（0.95）、nesterov（True）、ns_steps（5）通常就够了，**只有学习率和权重衰减需要调**。学习率内置 muP 缩放：模型变大时无需重新调 lr。

这条建议大大降低了使用门槛——两个「数学味最重」的参数（迭代步数、动量形式）都有可靠默认值。

**⑥ 战绩（Accomplishments）**：[README.md:L53-L59](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L53-L59)

五条战绩依次是：CIFAR-10 纪录（3.3 → 2.6 A100 秒）、175 美元训到 GPT-2 (XL) 水平、GPT-2 (small) 速度纪录提升 1.35×、Kimi.ai 用于大规模 LLM 训练、essential.ai（Ashish Vaswani 实验室）证明 Muon 特别适合大 batch 训练。**从 speedrun 玩具级验证到前沿实验室规模化应用**，这是 Muon 出名的完整轨迹。

#### 4.1.4 代码实践

**实践一（阅读 + 环境感知，约 3 分钟）**

1. **实践目标**：确认本机能在仓库根目录直接 `import muon`（无需安装），为后续所有实践打底。
2. **操作步骤**：
   - 通读一遍 `README.md`（很短）。
   - 在仓库根目录执行：
     ```bash
     python -c "import muon; print(muon.__file__)"
     ```
3. **需要观察的现象**：命令打印出一个以 `muon.py` 结尾的路径。
4. **预期结果**：因为 `muon.py` 就在仓库根目录，Python 会把它当作当前目录下的模块直接导入。本实践依赖本机已安装 PyTorch（`muon.py` 第 1 行就 `import torch`），若报 `ModuleNotFoundError: No module named 'torch'`，请先安装 torch。**待本地验证。**

#### 4.1.5 小练习与答案

**练习 1**：Muon 适用于哪些参数？哪些参数不应该用它训练？

> **答案**：适用于隐藏层的二维权重矩阵（`ndim >= 2`，含卷积核——把它压成二维看待，卷积网络中第一层卷积除外）。不应该用于：输入 embedding、最终输出层（head）、隐藏层的增益（gains）和偏置（biases）等一维或特殊参数——它们继续用 AdamW。

**练习 2**：根据 README，哪些 Muon 超参通常无需调整？哪些必须调？

> **答案**：momentum（0.95）、nesterov（True）、ns_steps（5）用默认值即可；**必须调的只有学习率和权重衰减**。且 lr 内置 muP 缩放，模型尺寸变化时不需要重调。

**练习 3**：README 用法示例中，`use_muon=True` 的参数组为什么没有 `betas`？

> **答案**：`betas=(β₁, β₂)` 是 Adam 系（一阶/二阶动量）的超参；Muon 内部跑的是 **SGD 动量**，只有一个动量系数（默认 `momentum=0.95`），所以 Muon 组不需要 `betas`。事实上第 3 单元会看到，`MuonWithAuxAdam` 构造函数对 Muon 组的合法键集合做了严格断言，传入 `betas` 会直接报错。

### 4.2 muon.py 顶部两个函数：正交化与更新计算

#### 4.2.1 概念说明

这两个函数是 Muon 的「算法心脏」，所有优化器类都只是围绕它们的壳。

**为什么需要正交化？** 一个普通梯度矩阵 \( G = USV^\top \) 在不同方向上的拉伸强度（奇异值）参差不齐：有的方向更新量很大、有的很小。把更新替换成**最接近的正交矩阵** \( UV^\top \)（相当于把所有奇异值统统拉平为 1），能让更新在各个方向上能量均衡，且更新大小只由学习率决定——这就是 docstring 中「lr 以谱范数为单位」的由来。

**为什么要用 Newton-Schulz 迭代而不是直接算 SVD？** 直接 SVD 又慢又难以在低精度下跑。Newton-Schulz 迭代**只用矩阵乘法**就能逼近「矩阵的零次幂」\( G(G^\top G)^{-1/2} \approx UV^\top \)，而且数值上足够稳定，可以在 GPU 上用 bfloat16 跑——又快又省显存。

#### 4.2.2 核心流程

`zeropower_via_newtonschulz5(G, steps)` 的伪代码：

```text
输入：矩阵 G，迭代步数 steps
X ← G 转成 bfloat16
若 X 行数 > 列数：先转置（保证行 ≤ 列）
X ← X / ‖X‖          # 谱范数归一化，保证最大奇异值 ≤ 1
重复 steps 次：
    A ← X · Xᵀ
    B ← b·A + c·A²    # 组合成五次多项式
    X ← a·X + B·X     # 朝正交矩阵推进一步
若转置过：转置回原形状
返回 X                # 近似 U·S'·Vᵀ，S' 的对角元 ≈ 0.5 ~ 1.5
```

数学目标（第 2 单元详细推导）：

\[
\text{zeropower}(G) \;=\; G\,(G^{\top}G)^{-1/2} \;=\; U V^{\top}
\]

`muon_update(grad, momentum, ...)` 把动量和正交化串成一次完整更新：

```text
momentum ← β·momentum + (1-β)·grad          # 动量缓冲区（指数滑动平均）
update   ← (1-β)·grad + β·momentum           # Nesterov 形式（可关）
若 update 是 4 维（卷积核）：压成 2 维矩阵
update   ← zeropower_via_newtonschulz5(update, ns_steps)
update   ← update × √max(1, 行数/列数)        # 与形状相关的尺度缩放
返回 update
```

#### 4.2.3 源码精读

**① `zeropower_via_newtonschulz5` 的 docstring——最重要的免责声明**：[muon.py:L5-L14](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L5-L14)

```python
def zeropower_via_newtonschulz5(G, steps: int):
    """
    Newton-Schulz iteration to compute the zeroth power / orthogonalization of G. ...
    This iteration therefore does not produce UV^T but rather something like US'V^T
    where S' is diagonal with S_{ii}' ~ Uniform(0.5, 1.5), which turns out not to hurt model
    performance at all relative to UV^T, where USV^T = G is the SVD.
    """
```

这段 docstring 诚实地说：**该迭代并不输出精确的 \( UV^\top \)**，而是输出 \( US'V^\top \)，其中 \( S' \) 的对角元近似均匀分布在 0.5～1.5——即「大致拉平、但不完全拉平」的奇异值。作者的经验是这**完全不伤**模型效果。系数选择是为了「最大化零点处斜率」以减少迭代步数，甚至宁可牺牲精确收敛。这是理解整个实现的钥匙。

**② 迭代主体**：[muon.py:L16-L27](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L16-L27)

```python
    a, b, c = (3.4445, -4.7750,  2.0315)
    X = G.bfloat16()
    if G.size(-2) > G.size(-1):
        X = X.mT

    # Ensure spectral norm is at most 1
    X = X / (X.norm(dim=(-2, -1), keepdim=True) + 1e-7)
    # Perform the NS iterations
    for _ in range(steps):
        A = X @ X.mT
        B = b * A + c * A @ A # quintic computation strategy ...
        X = a * X + B @ X
```

要点：

- 三个系数 \( (a,b,c) \) 定义了一个**五次多项式迭代**（quintic）：\( X \leftarrow aX + (bA + cA^2)X \)，其中 \( A = XX^\top \)。
- 第 17 行 `G.bfloat16()`：整个迭代在 bfloat16 下进行，快且稳。
- 第 18-19 行：若行数多于列数则先转置，让矩阵变成「扁」的（行 ≤ 列），减少矩阵乘法的计算量，最后在 [muon.py:L29-L31](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L29-L31) 转置回去，保证输出形状和输入一致。
- 第 22 行：用 Frobenius 范数归一化保证谱范数不超过 1（Frobenius 范数 ≥ 谱范数，所以除以它一定把最大奇异值压到 1 以下），这是 Newton-Schulz 迭代收敛的必要条件。

**③ `muon_update`：一次完整 Muon 更新**：[muon.py:L34-L41](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L34-L41)

```python
def muon_update(grad, momentum, beta=0.95, ns_steps=5, nesterov=True):
    momentum.lerp_(grad, 1 - beta)
    update = grad.lerp_(momentum, beta) if nesterov else momentum
    if update.ndim == 4: # for the case of conv filters
        update = update.view(len(update), -1)
    update = zeropower_via_newtonschulz5(update, steps=ns_steps)
    update *= max(1, update.size(-2) / update.size(-1))**0.5
    return update
```

逐行解读：

- 第 35 行 `momentum.lerp_(grad, 1 - beta)`：`lerp_` 是线性插值原地操作，等价于 `momentum = beta*momentum + (1-beta)*grad`，即动量的指数滑动平均。
- 第 36 行：Nesterov 动量的紧凑两行写法——先用「新梯度」修正动量，再以 `beta` 权重混合回梯度本身；`nesterov=False` 时直接用动量缓冲区。
- 第 37-38 行：卷积核是 4 维张量（如 64×3×7×7），`view(len(update), -1)` 把它压成 64×147 的二维矩阵，之后就能走和普通权重完全一样的正交化流程。
- 第 40 行：尺度缩放因子 \( \sqrt{\max(1, m/n)} \)（\( m \) 为行数、\( n \) 为列数），配合正交化让**不同形状参数的更新 RMS 大体一致**，学习率因此获得「谱范数单位」的语义。推导在第 2 单元第 3 讲。

#### 4.2.4 代码实践

**实践二：第一次亲手调用正交化函数（CPU 即可，约 5 分钟）**

1. **实践目标**：直观感受 `zeropower_via_newtonschulz5` 的输入输出——形状不变、类型变 bfloat16、奇异值被「大致拉平」。
2. **操作步骤**：在仓库根目录创建 `first_touch.py`（示例代码，自己练习用，不要写进仓库）：
   ```python
   import torch
   from muon import zeropower_via_newtonschulz5

   G = torch.randn(512, 256)
   O = zeropower_via_newtonschulz5(G, steps=5)
   print("输入形状:", G.shape, "输出形状:", O.shape, "输出dtype:", O.dtype)
   S = torch.linalg.svd_values(O.float())
   print("输出奇异值  min/max:", S.min().item(), S.max().item())
   Sg = torch.linalg.svd_values(G)
   print("输入奇异值  min/max:", Sg.min().item(), Sg.max().item())
   ```
   运行 `python first_touch.py`。
3. **需要观察的现象**：输出形状仍为 512×256、dtype 为 `torch.bfloat16`；**输出**的奇异值集中在一个窄带里，而**输入**的奇异值从接近 0 到大约 2+ 参差不齐。
4. **预期结果**：根据 docstring（[muon.py:L11-L13](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L11-L13)），输出奇异值应大致落在 0.5～1.5 之间。具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`zeropower_via_newtonschulz5` 输出的是精确的 \( UV^\top \) 吗？

> **答案**：不是。由于五次多项式系数以「最大化零点斜率、减少步数」为目标（甚至超过收敛临界点），输出是 \( US'V^\top \)，\( S' \) 对角元近似 ~ Uniform(0.5, 1.5)。docstring 明说：经验上这不伤害模型效果。

**练习 2**：函数为什么在第 17 行先做 `G.bfloat16()`？

> **答案**：Newton-Schulz 迭代只含矩阵乘法和加法，数值上足够稳定，可以在 bfloat16 下运行——GPU 上速度更快、显存更省（`Muon` 类 docstring 也强调了这一点）。

**练习 3**：`muon_update` 中 `if update.ndim == 4` 分支服务于什么参数？做了什么？

> **答案**：卷积核（conv filters）。它用 `view(len(update), -1)` 把 4 维卷积核（输出通道 × 输入通道 × 高 × 宽）压成「输出通道 × (其余维度乘积)」的二维矩阵，使其能和其他二维权重走同一条正交化流水线。

### 4.3 Muon 类 docstring：设计意图、适用边界与超参语义

#### 4.3.1 概念说明

如果说前两个函数是「算法」，`Muon` 类的 docstring 就是「产品说明书」——它回答了三个设计问题：

1. **Muon 这个名字**是 `MomentUm Orthogonalized by Newton-schulz`（动量 + Newton-Schulz 正交化）的缩写，一句话就是算法本身。
2. **架构定位**：Muon 不是从零发明的更新规则，而是「标准 SGD 动量 + 正交化后处理」的组合——先按你熟悉的动量法算出更新，再统一做正交化替换。
3. **超参语义**：学习率的单位是「每次更新的谱范数」，这和 AdamW 的「每个元素的幅度」完全不同量纲，是迁移使用时最容易踩的坑。

#### 4.3.2 核心流程

`Muon` 优化器每一步（`step()`）对每个参数做的事：

```text
取参数 p 的梯度 p.grad
    ↓
惰性初始化动量缓冲区 momentum_buffer（首次为全零）
    ↓
update ← muon_update(p.grad, momentum_buffer, beta=momentum)   # 4.2 节的函数
    ↓
p ← p × (1 − lr × weight_decay)      # AdamW 式解耦权重衰减
p ← p − lr × update                  # 沿正交化后的更新走一步
```

（分布式版 `Muon` 在此之上还有参数分片与 `all_gather` 同步，是全项目最难的部分，留到第 3 单元第 3 讲。）

#### 4.3.3 源码精读

**① 名字与算法摘要**：[muon.py:L45-L53](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L45-L53)

```python
    """
    Muon - MomentUm Orthogonalized by Newton-schulz

    Muon internally runs standard SGD-momentum, and then performs an orthogonalization post-
    processing step, in which each 2D parameter's update is replaced with the nearest orthogonal
    matrix. For efficient orthogonalization we use a Newton-Schulz iteration, which has the
    advantage that it can be stably run in bfloat16 on the GPU.
    """
```

注意「**post-processing**（后处理）」这个词：正交化不改变动量累积的方式，只替换最终的更新矩阵。这是本讲要求记住的一句话级摘要的出处。

**② 适用边界**：[muon.py:L55-L58](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L55-L58)

> Muon should only be used for hidden weight layers. The input embedding, final output layer, and any internal gains or biases should be optimized using a standard method such as AdamW. Hidden convolutional weights can be trained using Muon by viewing them as 2D and then collapsing their last 3 dimensions.

与 README 完全一致：只管隐藏层权重（含按二维看待的卷积核），embedding / 输出层 / 增益偏置交给 AdamW。

**③ 超参语义**：[muon.py:L60-L64](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L60-L64)

```python
    Arguments:
        lr: The learning rate, in units of spectral norm per update.
        weight_decay: The AdamW-style weight decay.
        momentum: The momentum. A value of 0.95 here is usually fine.
```

`lr` 的单位是**每次更新的谱范数**——正交化后更新的最大奇异值约为 1，乘上尺度缩放后仍与形状解耦，所以 lr 直接控制「这一步参数矩阵最多被拉伸多少倍」。`weight_decay` 是 AdamW 式（乘法解耦），`momentum=0.95` 通常不用动。

**④ 构造函数中的两个细节**：[muon.py:L65-L69](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L65-L69)

```python
    def __init__(self, params, lr=0.02, weight_decay=0, momentum=0.95):
        defaults = dict(lr=lr, weight_decay=weight_decay, momentum=momentum)
        assert isinstance(params, list) and len(params) >= 1 and isinstance(params[0], torch.nn.Parameter)
        params = sorted(params, key=lambda x: x.size(), reverse=True)
        super().__init__(params, defaults)
```

- `assert isinstance(params, list)`：**必须传显式的 Parameter 列表**。直接传 `model.parameters()`（生成器）会断言失败——README 示例里先做列表推导再传入，正是为了满足它。
- `sorted(..., key=..., reverse=True)`：按参数尺寸**从大到小排序**。直觉上「先处理大参数」，真正的原因在分布式版：把各 rank 的计算量均衡、并保证 `all_gather` 张量形状一致（第 3 单元第 3 讲详解）。

顺带认一下家族里的另外三个类：`SingleDeviceMuon`（[muon.py:L99-L127](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L99-L127)，单机版，无任何分布式调用，最适合入门阅读）；`MuonWithAuxAdam` / `SingleDeviceMuonWithAuxAdam`（[muon.py:L138-L164](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L138-L164) 与 [muon.py:L228-L287](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L228-L287)，把 Muon 组和 AdamW 组装进同一个优化器，`MuonWithAuxAdam` 的 docstring 里还有一份来自 NanoGPT speedrun 的真实用法示例）。

#### 4.3.4 代码实践

**实践三：用 Python 直接查阅 docstring + 验证构造约束（约 5 分钟）**

1. **实践目标**：学会用 `__doc__` 快速查阅优化器文档；亲手触发一次构造函数的断言，理解 `params` 必须是列表。
2. **操作步骤**：在仓库根目录运行（示例代码）：
   ```bash
   python - <<'EOF'
   import torch, muon
   print(muon.Muon.__doc__[:300])
   w = torch.nn.Parameter(torch.randn(64, 32))
   opt = muon.Muon([w])                      # 正确：显式列表
   print("列表传入 OK:", opt)
   try:
       muon.Muon(p for p in [w])             # 错误：生成器不是 list
   except AssertionError as e:
       print("生成器传入触发断言:", e)
   EOF
   ```
3. **需要观察的现象**：第一段打印出 4.3 节读过的 docstring 开头；列表传入正常构造；生成器传入抛出 `AssertionError`（无消息文本）。
4. **预期结果**：以上行为由 [muon.py:L67](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L67) 的 `assert isinstance(params, list)` 决定，可以从源码直接推出。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：Muon 的名字由何而来？

> **答案**：`MomentUm Orthogonalized by Newton-schulz`（动量，经 Newton-Schulz 正交化）的缩写，见 [muon.py:L46](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L46)。名字本身就是算法摘要。

**练习 2**：docstring 说 `lr` 的单位是什么？这和 AdamW 的 lr 有何不同？

> **答案**：单位是「每次更新的谱范数」（spectral norm per update）。正交化把更新的奇异值拉到约 1，因此 lr 控制的是参数矩阵整步的最大形变倍数；AdamW 的 lr 则作用于逐元素的归一化步长，两者量纲不同，不能直接照搬数值。

**练习 3**：`Muon([p for p in model.parameters()])` 和 `Muon(model.parameters())` 都能成功构造吗？

> **答案**：前者可以（列表推导式产生 `list`）；后者不行，`model.parameters()` 返回生成器，不满足 [muon.py:L67](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L67) 的 `isinstance(params, list)` 断言，会抛 `AssertionError`。

## 5. 综合实践

**综合实践：按 README 用法实例化 `SingleDeviceMuonWithAuxAdam`，观察默认超参填充（CPU 即可，约 10 分钟）**

这是本讲的核心动手任务，把「README 用法」和「muon.py 类」串起来。

1. **实践目标**：验证你已理解 `use_muon` 参数组机制——传入不完整的超参，观察构造函数如何为 Muon 组和 AdamW 组填充**不同的**默认值。

2. **操作步骤**：在仓库根目录创建 `overview_practice.py`（示例代码）：

   ```python
   import torch
   from muon import SingleDeviceMuonWithAuxAdam

   w1 = torch.nn.Parameter(torch.randn(256, 128))   # 模拟隐藏层权重 (ndim=2)
   w2 = torch.nn.Parameter(torch.randn(512, 256))
   b1 = torch.nn.Parameter(torch.randn(128))        # 模拟 bias/gain (ndim=1)

   param_groups = [
       dict(params=[w1, w2], use_muon=True, lr=0.02, weight_decay=0.01),
       dict(params=[b1], use_muon=False, lr=3e-4, betas=(0.9, 0.95), weight_decay=0.01),
   ]
   optimizer = SingleDeviceMuonWithAuxAdam(param_groups)
   print(optimizer)
   for i, g in enumerate(optimizer.param_groups):
       print(f"--- group {i} ---")
       for k, v in g.items():
           print(f"  {k}: {v}")
   ```

   运行 `python overview_practice.py`。

3. **需要观察的现象**：重点看两组被自动补全的键。Muon 组：你传了 `params / use_muon / lr / weight_decay`，构造函数补上 `momentum`。AdamW 组：你传了 `params / use_muon / lr / betas / weight_decay`，构造函数补上 `eps`。

4. **预期结果**（可直接从 [muon.py:L232-L248](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L232-L248) 推出）：
   - group 0（`use_muon=True`）：`lr=0.02`、`momentum=0.95`（默认填充）、`weight_decay=0.01`，键集合恰为 `{params, lr, momentum, weight_decay, use_muon}`；
   - group 1（`use_muon=False`）：`lr=3e-4`、`betas=(0.9, 0.95)`、`eps=1e-10`（默认填充）、`weight_decay=0.01`，键集合恰为 `{params, lr, betas, eps, weight_decay, use_muon}`；
   - 构造函数随后用 `assert set(group.keys()) == ...` 严格校验键集合——如果多传一个非法键（比如给 Muon 组传 `betas`）会直接断言失败。可以动手试试。

5. **为什么用 `SingleDeviceMuonWithAuxAdam` 而不是 `MuonWithAuxAdam`？** 后者的 `step()` 内部会调用 `dist.get_world_size()`（[muon.py:L195](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L195)），需要先初始化分布式进程组；单机 CPU 环境下用「SingleDevice」系列最省事。**本实践未实际运行，具体打印格式待本地验证。**

## 6. 本讲小结

- **Muon 是什么**：专为神经网络**隐藏层权重矩阵**设计的优化器；embedding、输出头、增益/偏置等参数仍用 AdamW 训练（README 与 `Muon` docstring 双重确认）。
- **一句话算法摘要**：先跑标准 SGD 动量，再把每个二维参数的更新用 Newton-Schulz 迭代替换为「最近正交矩阵」（实为奇异值拉平到约 0.5～1.5 的 \( US'V^\top \)）。
- **项目结构极简**：核心实现全部在约 290 行的 `muon.py` 中——3 个函数（`zeropower_via_newtonschulz5` / `muon_update` / `adam_update`）+ 4 个优化器类（分布式/单机 × 纯 Muon/带辅助 AdamW）。
- **标准用法**：把参数按 `use_muon=True/False` 分成两组，交给 `MuonWithAuxAdam`（或单机版）一个优化器搞定；只有 lr 和 weight_decay 需要认真调。
- **它为什么出名**：从 CIFAR-10 / NanoGPT speedrun 纪录，到 Kimi.ai、essential.ai 的规模化应用，验证了「正交化更新」在大小区间模型上都有效。

## 7. 下一步学习建议

- **下一讲（u1-l2）**：精读 `setup.py`，弄清「单文件如何打包成 `muon-optimizer` 包」，并在本地完成 `pip install git+https://github.com/KellerJordan/Muon` 的完整安装验证，跑通第一个最小训练循环。
- **之后再进入第 2 单元**：先补齐 SVD 与正交化的数学直觉（u2-l1），再逐行精读 Newton-Schulz 迭代（u2-l2）——本讲 4.2 节留下的「奇异值为何落在 0.5～1.5」将在那里彻底讲清。
- **延伸阅读（可选）**：README 资源区列出的 Keller Jordan 的 Muon 写作与 Jeremy Bernstein 的理论推导博文，读完本讲再去读会有完全不同的吸收度。
