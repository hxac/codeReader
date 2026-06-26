# 核心组件：LayerNorm / GELU / FeedForward

## 1. 本讲目标

在 [u3-l3](u3-l3-multihead-attention.md) 里，我们完成了 GPT 模型的「大脑」——多头因果注意力。但一个完整的 Transformer 块还需要另外三块「零件」才能运转：

- 把每个 token 的特征分布稳定下来的**层归一化（LayerNorm）**；
- 给网络引入非线性的**激活函数 GELU**；
- 在每个位置上做特征变换的**前馈网络（FeedForward）**。

本讲的目标是让你：

1. 理解**为什么**需要 LayerNorm，并手写一个带可学习 `scale`/`shift` 的 `LayerNorm`；
2. 理解 GELU 相比 ReLU 的优势，并实现 GPT-2 实际使用的 tanh 近似 GELU；
3. 把它们组装成 `FeedForward` 两层网络，理解 4× 扩展的结构与「逐 token 独立处理」的特点。

学完本讲，你就凑齐了下一讲 [u4-l2](u4-l2-transformer-block.md) 组装 `TransformerBlock` 所需的全部非注意力组件。

## 2. 前置知识

- **PyTorch 的 `nn.Module` 与 `nn.Parameter`**：本书所有组件都继承自 `nn.Module`，把可学习参数用 `nn.Parameter` 注册后，优化器才能更新它们（见 [u8-l1](u8-l1-pytorch-essentials.md)）。
- **张量的维度与广播**：`mean(dim=-1, keepdim=True)` 表示沿最后一维（特征维）求平均，并保留维度以便广播减法。
- **注意力层的输出形态**：经过 [u3-l3](u3-l3-multihead-attention.md) 的多头注意力后，张量形状是 `batch × num_tokens × emb_dim`，本讲的所有组件都作用在最后的 `emb_dim` 特征维上。
- **均值与方差**：均值为 0、方差为 1 是「标准化」的含义，这是 LayerNorm 的核心直觉。

> 关键直觉：注意力让 token 之间「交换信息」，而 LayerNorm 与 FeedForward 则负责把交换后的信息「归整」并「非线性地加工」，两者交替堆叠才构成 Transformer。

## 3. 本讲源码地图

本讲涉及的真实源码文件只有两个，都在 `ch04/01_main-chapter-code/` 下：

| 文件 | 作用 |
| --- | --- |
| [`ch04/01_main-chapter-code/gpt.py`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py) | 第 2–4 章的汇总脚本，`LayerNorm`、`GELU`、`FeedForward` 三个类的最终成品都在这里，本讲以它为引用基准。 |
| [`ch04/01_main-chapter-code/ch04.ipynb`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/ch04.ipynb) | 第 4 章正文 notebook，逐格讲解了这三个组件的动机、推导与可视化，是理解「为什么」的依据。 |

> 约定提醒（见 [u1-l3](u1-l3-repo-reading-map.md)）：`gpt.py` 是**自包含汇总脚本**，开头没有 `from previous_chapters import`，单文件即可运行；而 `ch04.ipynb` 里的 `TransformerBlock` 单元格则用 `from previous_chapters import MultiHeadAttention` 复用了第 3 章代码。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：LayerNorm、GELU、FeedForward。三者都是「逐位置（per-token）」作用于特征维 `emb_dim` 的，输入输出形状始终保持 `batch × num_tokens × emb_dim`。

### 4.1 LayerNorm：归一化激活值

#### 4.1.1 概念说明

深层网络在训练时常常遇到一个麻烦：每一层的输入分布会随着前面层的参数更新而不断变化（这叫**内部协变量偏移，internal covariate shift**）。分布一旦飘忽不定，训练就难以收敛。

**层归一化（Layer Normalization, LayerNorm）** 的做法很简单：对**每个样本、在它自己的特征维**上，把激活值强行拉成均值 0、方差 1：

\[ \hat{x} = \frac{x - \mu}{\sqrt{\sigma^2 + \epsilon}} \]

其中 \(\mu\) 和 \(\sigma^2\) 都沿特征维（最后一维）计算，\(\epsilon\) 是一个防止除以 0 的小常数。

> 为什么是「层」归一化而不是「批」归一化？BatchNorm 沿 batch 维统计，依赖 batch 大小、且在序列长度可变的 NLP 任务里不友好；LayerNorm 沿特征维统计，**每个样本独立计算**，与 batch 大小无关，更适合 Transformer。

光把分布拉成均值 0、方差 1 还不够——这会限制网络的表达能力。因此 LayerNorm 又加了两个**可学习参数** `scale`（\(\gamma\)）和 `shift`（\(\beta\)），让网络自己决定要不要、以及如何「撤销」这次归一化：

\[ y = \gamma \cdot \hat{x} + \beta \]

#### 4.1.2 核心流程

`LayerNorm` 的前向计算可以拆成 4 步：

1. 沿最后一维（特征维）求每个 token 向量的均值 \(\mu\)；
2. 沿最后一维求每个 token 向量的方差 \(\sigma^2\)（用**有偏**估计，见下文）；
3. 标准化：\(\hat{x} = (x - \mu) / \sqrt{\sigma^2 + \epsilon}\)；
4. 缩放与偏移：\(y = \gamma \cdot \hat{x} + \beta\)，其中 \(\gamma\)、\(\beta\) 是可学习参数。

伪代码：

```text
mean      = x.mean(dim=-1, keepdim=True)        # 每个 token 一个均值
var       = x.var(dim=-1, keepdim=True, unbiased=False)
norm_x    = (x - mean) / sqrt(var + eps)         # 标准化
return scale * norm_x + shift                    # 可学习缩放与偏移
```

#### 4.1.3 源码精读

`LayerNorm` 的完整实现只有十几行（[ch04/01_main-chapter-code/gpt.py:114-125](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L114-L125)）：

```python
class LayerNorm(nn.Module):
    def __init__(self, emb_dim):
        super().__init__()
        self.eps = 1e-5
        self.scale = nn.Parameter(torch.ones(emb_dim))
        self.shift = nn.Parameter(torch.zeros(emb_dim))

    def forward(self, x):
        mean = x.mean(dim=-1, keepdim=True)
        var = x.var(dim=-1, keepdim=True, unbiased=False)
        norm_x = (x - mean) / torch.sqrt(var + self.eps)
        return self.scale * norm_x + self.shift
```

逐行解读：

- **`self.eps = 1e-5`**（[L117](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L117)）：方差里加上 \(\epsilon=10^{-5}\)，防止某 token 所有特征恰好相等（方差为 0）时出现除零。
- **`self.scale` / `self.shift`**（[L118-L119](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L118-L119)）：用 `nn.Parameter` 注册，初值分别为全 1 和全 0，意味着**一开始归一化效果原样输出**，训练过程中再由优化器调整。这是 LayerNorm 仅有的两个可学习参数。
- **`unbiased=False`**（[L123](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L123)）：使用**有偏方差** \(\frac{\sum_i (x_i-\bar{x})^2}{n}\)（分母是 \(n\) 而非 \(n-1\)，不做贝塞尔校正）。GPT-2 当年就是用有偏方差训练的，为了后续章节能精确对齐 OpenAI 预训练权重，这里必须保持一致。由于 `emb_dim` 很大（768），\(n\) 与 \(n-1\) 差异可忽略。
- **`keepdim=True`**：保留形状为 `(..., 1)`，使 `(x - mean)` 能正确广播。

> 这段代码与 notebook 里逐格推导的过程一一对应：notebook 先手算 `mean/var`、再手算标准化结果、最后封装成 `LayerNorm` 类（见 ch04.ipynb 的 4.2 节）。

#### 4.1.4 代码实践

**实践目标**：把一个随机张量送进 `LayerNorm`，验证输出在每个 token（每行）上均值 ≈ 0、方差 ≈ 1。

**操作步骤**：

```python
import torch
from gpt import LayerNorm  # 需与 gpt.py 同目录，或 from previous_chapters import LayerNorm

torch.manual_seed(123)
# 模拟 2 个样本、6 维特征（相当于 emb_dim=6 的玩具示例）
batch_example = torch.randn(2, 6)

ln = LayerNorm(emb_dim=6)
out = ln(batch_example)

mean = out.mean(dim=-1, keepdim=True)
var  = out.var(dim=-1, keepdim=True, unbiased=False)
print("Mean:\n", mean)
print("Variance:\n", var)
```

**需要观察的现象**：每行的均值接近 0、方差接近 1（但不精确等于 1）。

**预期结果**：方差略小于 1（如 0.9995），原因是公式里加了 `eps`。notebook 在 ch04.ipynb 4.2 节正是给出了这一结论，并特别注明「Variance is not exactly 1 because we use `eps`」。

> 待本地验证：在更大维度（如 `emb_dim=768`）上重复该实验，方差会更贴近 1。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `unbiased=False` 改成 `unbiased=True`（默认值），加载 OpenAI 预训练权重后会发生什么？

**参考答案**：归一化的统计量会与 GPT-2 原始训练时不同，激活分布发生偏移，模型输出会异常。这就是代码里**显式**写 `unbiased=False` 的原因——为了和 GPT-2 的有偏方差保持兼容。

**练习 2**：`scale` 初始化为 1、`shift` 初始化为 0，为什么这样设计？

**参考答案**：这样在训练刚开始时，LayerNorm 等价于「纯标准化」，不引入额外偏移，保证前向传播数值稳定；随着训练推进，网络根据需要自主学习合适的缩放与偏移。

---

### 4.2 GELU：平滑的非线性激活

#### 4.2.1 概念说明

神经网络必须有**非线性激活函数**，否则多层线性层叠加仍等价于一层。深度学习里最常见的 ReLU 定义为 \(\max(0, x)\)：简单高效，但它在 \(x=0\) 处不可导，且对所有负输入一律输出 0（负区间梯度恒为 0，即「死 ReLU」）。

GPT-2 使用的是 **GELU（Gaussian Error Linear Unit，高斯误差线性单元）**。它是一条**平滑曲线**：对正输入近似保留（像 ReLU），但对负输入不是一刀切到 0，而是给出一个小的、非零的输出，从而在负区间也保留梯度。

GELU 的精确定义是 \(\text{GELU}(x)=x\cdot\Phi(x)\)，其中 \(\Phi(x)\) 是标准正态分布的累积分布函数。但工程上普遍使用一个计算更便宜的 **tanh 近似**（原始 GPT-2 也是用这个近似训练的）：

\[ \text{GELU}(x) \approx 0.5 \cdot x \cdot \left(1 + \tanh\left[\sqrt{\frac{2}{\pi}} \cdot \left(x + 0.044715 \cdot x^3\right)\right]\right) \]

#### 4.2.2 核心流程

`tanh` 近似的计算分两步：

1. 计算括号内的线性+三次项：\(a = x + 0.044715 \cdot x^3\)，再乘以常数 \(\sqrt{2/\pi}\)；
2. 套上 `tanh` 并组合：\(\text{GELU}(x) = 0.5 \cdot x \cdot (1 + \tanh(a))\)。

对正的 \(x\)，\(\tanh(a)\to 1\)，于是 \(\text{GELU}(x)\to x\)（同 ReLU）；对负的 \(x\)，输出是一个平滑的小值，而非硬归零。

#### 4.2.3 源码精读

`GELU` 实现（[ch04/01_main-chapter-code/gpt.py:128-136](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L128-L136)）就是上述公式的逐字翻译：

```python
class GELU(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, x):
        return 0.5 * x * (1 + torch.tanh(
            torch.sqrt(torch.tensor(2.0 / torch.pi)) *
            (x + 0.044715 * torch.pow(x, 3))
        ))
```

要点：

- **无可学习参数**（[L129-L130](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L129-L130)）：`__init__` 只有 `super().__init__()`，GELU 是一个纯函数。
- **`torch.tensor(2.0 / torch.pi)`**（[L134](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L134)）：常数 \(\sqrt{2/\pi}\)，在张量运算中显式构造为张量以匹配数据类型/设备。
- **`torch.pow(x, 3)`**：计算 \(x^3\)，即公式中的三次项。

> 对照：notebook 在 4.3 节把 GELU 与 ReLU 画在同一张图上对比，可见 ReLU 在 \(x<0\) 处恒为 0、在 0 处有折角；GELU 则全程光滑，仅在约 \(x\approx -0.75\) 附近达到最小（小负值），再缓慢趋于 0。

#### 4.2.4 代码实践

**实践目标**：直观感受 GELU 与 ReLU 的差异——GELU 对负输入有非零输出。

**操作步骤**：

```python
import torch
import torch.nn as nn
from gpt import GELU

x = torch.tensor([-2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0])
print("x     :", x.tolist())
print("ReLU  :", torch.relu(x).tolist())
print("GELU  :", GELU()(x).tolist())
```

**需要观察的现象**：ReLU 对所有负值都输出 0；GELU 对负值给出小的非零输出（如 \(x=-1\) 时约 \(-0.159\)）。

**预期结果**：GELU 在 \(x=2\) 时略小于 2（≈1.954，不是严格等于输入），在负区间平滑过渡，与 ReLU 的硬截断形成对比。

> 待本地验证：可用 notebook 4.3 节的 matplotlib 代码画出 GELU 与 ReLU 曲线，更直观地看到 GELU 的平滑性。

#### 4.2.5 小练习与答案

**练习 1**：为什么 LLM 倾向用 GELU 而不是 ReLU？

**参考答案**：GELU 处处可导、平滑，且在负区间保留非零梯度，有助于缓解「死神经元」并让深层网络训练更稳定；实验上在大模型上表现更好。

**练习 2**：如果把近似 GELU 换成精确的 \(\text{GELU}(x)=x\cdot\Phi(x)\)，模型还能正常加载 OpenAI 预训练权重吗？

**参考答案**：可以。激活函数不涉及可学习参数，权重形状不变；只是前向数值略有差异（近似误差很小），对推理质量影响极小。本书用 tanh 近似是为了与 GPT-2 原始训练时的实现保持一致。

---

### 4.3 FeedForward：逐位置的两层网络

#### 4.3.1 概念说明

注意力层负责 token 之间的「横向交流」，但模型还需要一种「纵向」的处理：对**每个 token 独立地**做一次特征变换、引入非线性、再投回原维度。这正是**前馈网络（FeedForward Network, FFN）** 的职责。

GPT-2 的 FFN 是一个非常简单的**两层瓶颈结构**：

\[ \text{FFN}(x) = W_2 \,\big(\text{GELU}(W_1 x)\big) \]

- 第一层把 `emb_dim` 扩展到 `4 × emb_dim`（先升维，增加表达能力）；
- 中间用 GELU 引入非线性；
- 第二层再投影回 `emb_dim`（降回原维度）。

> 关键特点：FFN 对**每个 token 位置独立、相同地**作用。它不关心 token 之间的顺序或关系——那是注意力的活。正因为如此，FFN 可以用普通的全连接层 `nn.Linear` 实现，输入输出的 token 维度完全不变。

#### 4.3.2 核心流程

对形状为 `batch × num_tokens × emb_dim` 的输入：

1. `nn.Linear(emb_dim, 4*emb_dim)`：最后一个维度 768 → 3072；
2. `GELU()`：逐元素非线性；
3. `nn.Linear(4*emb_dim, emb_dim)`：3072 → 768，还原维度。

三个步骤用 `nn.Sequential` 串联，输出形状与输入完全相同。

#### 4.3.3 源码精读

`FeedForward` 实现（[ch04/01_main-chapter-code/gpt.py:139-149](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L139-L149)）：

```python
class FeedForward(nn.Module):
    def __init__(self, cfg):
        super().__init__()
        self.layers = nn.Sequential(
            nn.Linear(cfg["emb_dim"], 4 * cfg["emb_dim"]),
            GELU(),
            nn.Linear(4 * cfg["emb_dim"], cfg["emb_dim"]),
        )

    def forward(self, x):
        return self.layers(x)
```

要点：

- **`cfg` 配置字典**（[L140](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L140)）：接收 `GPT_CONFIG_124M`，只用其中的 `emb_dim`（768）。
- **`4 * cfg["emb_dim"]`**（[L143](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L143)）：中间隐藏层扩到 4 倍（3072），这是 GPT-2/Transformer 的标准设计，也是模型参数量的一大来源。
- **中间夹 `GELU()`**（[L144](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L144)）：上一模块实现的 GELU 直接被复用，体现「积木复用」。
- **`forward` 直接调用 `self.layers(x)`**（[L148-L149](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L148-L149)）：`nn.Sequential` 自动按顺序执行三步。

> 在 `TransformerBlock` 里，FFN 与注意力是**并列的两个子模块**，各配一个 LayerNorm 和一条残差连接（见 [u4-l2](u4-l2-transformer-block.md)）。

#### 4.3.4 代码实践

**实践目标**：用真实配置实例化 `FeedForward`，验证它保持 token 数与特征维度不变。

**操作步骤**：

```python
import torch
from gpt import FeedForward

GPT_CONFIG_124M = {
    "vocab_size": 50257, "context_length": 1024, "emb_dim": 768,
    "n_heads": 12, "n_layers": 12, "drop_rate": 0.1, "qkv_bias": False,
}

ffn = FeedForward(GPT_CONFIG_124M)
# 模拟 [batch_size, num_tokens, emb_dim]
x   = torch.rand(2, 3, 768)
out = ffn(x)
print("Input shape :", x.shape)
print("Output shape:", out.shape)
```

**需要观察的现象**：输出形状与输入完全一致。

**预期结果**：`torch.Size([2, 3, 768])` —— 这正是 notebook 4.3 节给出的结果，说明 FFN 是「形状保持」的逐位置变换。

> 待本地验证：`print(sum(p.numel() for p in ffn.parameters()))` 会看到约 4.7M 参数（\(768\times3072 + 3072 + 3072\times768 + 768\)），FFN 是 GPT-2 各层参数的重要组成。

#### 4.3.5 小练习与答案

**练习 1**：为什么 FFN 的中间层要扩到 4 倍而不是 2 倍或保持不变？

**参考答案**：扩展到 4 倍提供了更大的中间表示空间，让模型在每个 token 上能学到更丰富的非线性特征，再压缩回原维度。这是原始 Transformer 论文就采用的经验性设计，在大模型上被反复验证有效。

**练习 2**：如果输入形状是 `batch × num_tokens × emb_dim`，FFN 会不会让不同 token 之间互相影响？

**参考答案**：不会。`nn.Linear` 只作用在最后一维（特征维），每个 token 的计算彼此独立、且共享同一组权重。token 间的信息交互完全由注意力层负责。

---

## 5. 综合实践

把本讲三个组件串起来跑一遍，体会它们各自的角色。这个任务综合了 LayerNorm、GELU 与 FeedForward，并直接呼应下一讲的 `TransformerBlock`。

1. **构建配置**：复用上面的 `GPT_CONFIG_124M`（`emb_dim=768`）。
2. **实例化三个组件**：`ln = LayerNorm(768)`、`ffn = FeedForward(GPT_CONFIG_124M)`、`gelu = GELU()`。
3. **造输入**：`x = torch.randn(2, 4, 768)`（2 个样本、4 个 token、768 维）。
4. **依次前向**：
   ```python
   h = ln(x)        # 1) 归一化：每个 token 均值≈0、方差≈1
   print("after LayerNorm var:", h.var(dim=-1, unbiased=False).mean().item())
   h = ffn(h)       # 2) 逐位置特征变换（内部用到 GELU）
   print("after FeedForward shape:", h.shape)
   ```
5. **观察并思考**：
   - 过 LayerNorm 后方差应接近 1；
   - 过 FeedForward 后形状仍是 `[2, 4, 768]`，但方差会**显著偏离 1**（因为 FFN 改变了数值分布）——这正是为什么 `TransformerBlock` 在 FFN 之后还要再接一个 LayerNorm，并在两处都加残差连接。
6. **延伸**：把 `gelu` 单独作用在一段 `torch.linspace(-3, 3, 100)` 上，对比 `torch.relu` 的输出，确认 GELU 的平滑性。

> 这条「LayerNorm → FeedForward → 再归一化 → 残差」的链路，就是下一讲 [u4-l2](u4-l2-transformer-block.md) 里 `TransformerBlock` 的核心骨架，本讲只是把它拆开逐块吃透。

## 6. 本讲小结

- **LayerNorm** 沿特征维把每个 token 拉成均值 0、方差 1，再用可学习的 `scale`（初始 1）和 `shift`（初始 0）做仿射变换；显式使用 `unbiased=False` 是为了和 GPT-2 的有偏方差对齐。
- **GELU** 是 GPT-2 的激活函数，本项目用其 tanh 近似实现，平滑且在负区间保留梯度，优于硬截断的 ReLU；它没有可学习参数。
- **FeedForward** 是 `768 → 3072 → 768` 的两层瓶颈网络，中间夹 GELU，对每个 token 独立作用、形状保持不变，是模型参数的重要来源。
- 三者都只作用于最后一维（特征维），不改变 `batch × num_tokens × emb_dim` 的整体形态，是 `TransformerBlock` 里与注意力并列的非注意力子模块。
- 这三个类都被汇总进 `gpt.py`，是自包含、可直接 `python gpt.py` 运行的成品代码。

## 7. 下一步学习建议

- **下一讲 [u4-l2：TransformerBlock 与残差连接](u4-l2-transformer-block.md)**：把本讲的 LayerNorm + FeedForward 与 [u3-l3](u3-l3-multihead-attention.md) 的多头注意力拼成完整的 `TransformerBlock`，并理解 **pre-LayerNorm** 结构与**残差连接（shortcut）** 为何能缓解梯度消失。
- **后续 [u4-l3](u4-l3-gpt-model-assembly.md)**：把 12 个 `TransformerBlock` 堆叠起来，加上嵌入层和输出头，组装成完整的 124M `GPTModel`。
- **延伸阅读**：可先翻看 ch04.ipynb 的 4.2–4.3 节，对照 notebook 里逐格推导的数值（如手算的 mean/var），加深对 LayerNorm 与 GELU 的手感。
