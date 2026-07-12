# RoPE 旋转位置编码与 YaRN 长度外推

> 所属单元：u3 模型结构——从配置到逐层拆解
> 依赖讲义：u3-l2（RMSNorm 与 GQA 注意力）

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 Transformer 为什么需要位置编码，以及 RoPE 用「旋转」编码相对位置的数学直觉。
- 读懂 `precompute_freqs_cis` 是如何把位置和频率组合成 `freqs_cos / freqs_sin` 两张表的，并理解 `apply_rotary_pos_emb` 中 `rotate_half` 的作用。
- 理解 YaRN 为什么能在「不重新训练」的前提下把上下文从训练长度外推到更长，并知道它对「高频维不动、低频维缩放」的分段处理。
- 能够动手对比 `--inference_rope_scaling` 开关前后的频率曲线与长文本困惑度（PPL）。

本讲只解决「位置编码是怎么算出来的、长文本时怎么外推」这一件事，不涉及注意力归一化与 KV Cache 的细节（那是 u3-l2 的内容），也不涉及前馈网络（u3-l4）。

## 2. 前置知识

阅读本讲前，建议先具备以下直觉：

- **注意力是无序的**：Self-Attention 的核心运算是 `Q @ K^T`，如果把输入序列打乱，每两个 token 之间的点积不变，注意力分布也不变。所以必须额外告诉模型「每个 token 在第几个位置」，这就是**位置编码（Position Encoding）**要解决的问题。
- **绝对位置 vs 相对位置**：语言里真正重要的往往是「两个词相隔多远」，而不是「这个词在第 100 个位置」。RoPE 的卖点是：虽然它把绝对位置写进 Q/K，但最终注意力分数只依赖**相对位置**。
- **旋转矩阵**：把一个二维向量 \((x_1, x_2)\) 逆时针旋转角度 \(\theta\)，相当于乘以矩阵 \(\begin{bmatrix}\cos\theta & -\sin\theta\\ \sin\theta & \cos\theta\end{bmatrix}\)。旋转不改变向量长度（正交变换），这是后面「范数保持」练习的依据。
- **本讲上下文**：在 u3-l2 中我们看到 `Attention.forward` 在 `q_norm/k_norm` 之后调用了一行 `apply_rotary_pos_emb(xq, xk, cos, sin)`（[model/model_minimind.py:119](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L119)）。本讲就反过来说清楚这里的 `cos / sin` 从哪来、做了什么。

## 3. 本讲源码地图

| 文件 | 关键符号 | 作用 |
|------|----------|------|
| [model/model_minimind.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py) | `precompute_freqs_cis` | 预计算每个位置、每个频率维的 `cos / sin` 表 |
| 同上 | `apply_rotary_pos_emb` | 把旋转施加到 Q、K 上 |
| 同上 | `MiniMindConfig.rope_scaling` | YaRN 外推的配置字典 |
| 同上 | `MiniMindModel.__init__` / `.forward` | 把表注册为 buffer，并按 `start_pos` 切片供每层使用 |
| [eval_llm.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py) | `--inference_rope_scaling` | 推理时一键开启 YaRN 外推 |
| [README.md](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md) | 第 Ⅳ 节「RoPE 长度外推」 | 文字说明与 PPL 对比图 |

> 约定：默认配置下 `hidden_size=768`、`num_attention_heads=8`，因此 `head_dim = 768/8 = 96`。RoPE 是**逐头**施加的，所以下面所有「dim」都指 `head_dim=96`，频率维数为 `dim/2 = 48`。

---

## 4. 核心概念与源码讲解

### 4.1 `precompute_freqs_cis`：RoPE 频率矩阵的预计算

#### 4.1.1 概念说明

RoPE（Rotary Position Embedding，旋转位置编码）的核心想法有两步：

1. **把每个头的 96 维向量切成 48 个二维子向量**，每个二维平面独立旋转一个角度。
2. **位置越靠后，旋转角度越大**；不同二维平面用**不同的基础频率**旋转——靠前的平面转得快（高频），靠后的平面转得慢（低频）。

第 \(i\) 个二维平面在位置 \(m\) 处的旋转角度为：

\[
\theta_{m,i} = m \cdot \omega_i, \qquad \omega_i = \theta_{\text{base}}^{-2i/d},\ i=0,\dots,d/2-1
\]

其中 \(\theta_{\text{base}}\) 就是配置里的 `rope_theta`（默认 `1e6`）。`i` 越大，\(\omega_i\) 越小，转得越慢。这样一张「位置 × 频率」的角度表，对它取 `cos / sin` 就得到了施加旋转所需的两组系数。

为什么要预计算？因为这张表只依赖位置、频率和 `rope_theta`，与输入内容无关，可以在模型构造时算好缓存，推理时按位置切片直接用，避免每层重复计算。

#### 4.1.2 核心流程

`precompute_freqs_cis(dim, end, rope_base, rope_scaling)` 的执行流程：

1. 按 \(\omega_i = \theta_{\text{base}}^{-2i/d}\) 算出 `dim/2` 个**基础频率**（一个一维向量 `freqs`）。
2. 若传入了 `rope_scaling`（即开启 YaRN）且 `end > original_max`，对 `freqs` 做 YaRN 分段缩放（见 4.3）。
3. 生成位置序列 `t = [0, 1, ..., end-1]`，用外积 `outer(t, freqs)` 得到 `(end, dim/2)` 的角度矩阵。
4. 分别取 `cos`、`sin`，并在最后一维**复制一份**拼成 `(end, dim)`，再乘 `attn_factor`（默认 1.0）。
5. 返回 `freqs_cos, freqs_sin`。

#### 4.1.3 源码精读

先看频率定义与 YaRN 入口（YaRN 分支留到 4.3 拆）：

[precompute_freqs_cis 的基频定义与外积](model/model_minimind.py:62-78)（[永久链接](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L62-L78)）

```python
def precompute_freqs_cis(dim, end=int(32*1024), rope_base=1e6, rope_scaling=None):
    freqs, attn_factor = 1.0 / (rope_base ** (torch.arange(0, dim, 2)[: (dim // 2)].float() / dim)), 1.0
    if rope_scaling is not None:           # YaRN 分支（见 4.3）
        ...
    t = torch.arange(end, device=freqs.device)
    freqs = torch.outer(t, freqs).float()  # (end, dim/2) 的角度矩阵
    freqs_cos = torch.cat([torch.cos(freqs), torch.cos(freqs)], dim=-1) * attn_factor
    freqs_sin = torch.cat([torch.sin(freqs), torch.sin(freqs)], dim=-1) * attn_factor
    return freqs_cos, freqs_sin
```

要点：

- 第 1 行的 `rope_base ** (arange(0,dim,2)[:dim//2] / dim)` 正是 \(\theta_{\text{base}}^{2i/d}\)，取倒数即 \(\omega_i\)。
- `torch.outer(t, freqs)` 一次算出所有位置、所有频率的角度，是整个 RoPE 最关键的一行。
- `cat([cos, cos])` 把 `dim/2` 列复制成 `dim` 列，是为了配合 4.2 里 `rotate_half` 的「前后两半交换」语义——前后两半用同一个角度，才能写成 `q*cos + rotate_half(q)*sin` 这种紧凑形式。
- `attn_factor` 在本项目里恒为 1.0，所以**不做 YaRN 论文里的注意力温度修正**，只做频率缩放。

这张表在模型构造时算一次并缓存：

[MiniMindModel 把 freqs_cos/sin 注册为 buffer](model/model_minimind.py:205-207)（[永久链接](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L205-L207)）

```python
freqs_cos, freqs_sin = precompute_freqs_cis(
    dim=config.head_dim, end=config.max_position_embeddings,
    rope_base=config.rope_theta, rope_scaling=config.rope_scaling)
self.register_buffer("freqs_cos", freqs_cos, persistent=False)
self.register_buffer("freqs_sin", freqs_sin, persistent=False)
```

`persistent=False` 表示这两张表**不写进权重文件**（它们可以由配置重新算出，省盘空间）。推理时按当前位置区间切片：

[按 start_pos 切片得到当前步的 cos/sin](model/model_minimind.py:216-219)（[永久链接](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L216-L219)）

```python
start_pos = past_key_values[0][0].shape[1] if past_key_values[0] is not None else 0
...
position_embeddings = (self.freqs_cos[start_pos:start_pos + seq_length],
                       self.freqs_sin[start_pos:start_pos + seq_length])
```

注意这里的 `start_pos`：单步解码（带 KV Cache）时，新 token 的绝对位置是已缓存长度 `start_pos`，所以切片从 `start_pos` 开始——这正是 RoPE 在增量生成时位置不重排的关键。

#### 4.1.4 代码实践：画出 YaRN 开关前后的频率曲线

**实践目标**：直观看到 RoPE 的「位置 × 频率」余弦波，并验证高频维转得快、低频维转得慢。

**操作步骤**（示例代码，在项目根目录运行）：

```python
# 示例代码：可视化 RoPE 的 cos 波形
import torch, matplotlib
matplotlib.use("Agg")           # 无显示环境用 Agg，有界面可删
import matplotlib.pyplot as plt
from model.model_minimind import precompute_freqs_cis, MiniMindConfig

dim, end, rope_base = 96, 4096, 1e6            # head_dim=96
cos_off, _ = precompute_freqs_cis(dim, end, rope_base, rope_scaling=None)
cfg = MiniMindConfig(inference_rope_scaling=True)
cos_on, _ = precompute_freqs_cis(dim, end, rope_base, rope_scaling=cfg.rope_scaling)

pos = torch.arange(end)
plt.figure(figsize=(8, 4))
for idx, name in [(0, "高频维 i=0"), (47, "低频维 i=47")]:
    plt.plot(pos, cos_off[:end, idx], label=f"{name} (YaRN off)")
    plt.plot(pos, cos_on[:end, idx], "--",      label=f"{name} (YaRN on)")
plt.xlabel("position m"); plt.ylabel("cos(m·ω)"); plt.legend(); plt.tight_layout()
plt.savefig("rope_freqs.png"); print("saved rope_freqs.png")
```

**需要观察的现象**：

- `i=0` 的高频维在 0~512 区间就完成多次振荡（转得快）；`i=47` 的低频维几乎是一条平缓长波（转得慢）。
- YaRN off 时两条实线即原始 RoPE；开启 YaRN 后（虚线），高频维 `i=0` 的实/虚线**几乎重合**（不缩放），低频维 `i=47` 的虚线被**拉长**了（频率除以 `factor`）。

**预期结果**：低频维的余弦周期在 YaRN 下变为原来的约 16 倍（对应 `factor=16`）。

**说明**：若环境无 matplotlib，可改为 `print(cos_off[:10, 47])` 与 `print(cos_on[:10, 47])` 数值对比，同样能看到低频维被「拉平」。

#### 4.1.5 小练习与答案

**练习 1**：把 `rope_base` 从 `1e6` 改成 `1e4`，`i=47` 的波形会变快还是变慢？

> **答案**：变快。`rope_base` 越小，\(\omega_i\) 越大，旋转越快、波长越短。`rope_theta` 是控制 RoPE 频率分布的旋钮，Qwen3/MiniMind 选 `1e6` 是为了让长序列下低频维不至于转得太快而失效。

**练习 2**：为什么 `freqs_cos` 要 `cat([cos, cos])` 复制成 `dim` 列，而不是只保留 `dim/2` 列？

> **答案**：因为 `apply_rotary_pos_emb` 用 `rotate_half` 把向量前后两半交换来表示二维旋转，前后两半需要使用**同一个角度**的 `cos/sin`，所以表也要在列维复制一份与之对齐（见 4.2）。

---

### 4.2 `apply_rotary_pos_emb`：把旋转施加到 Q/K

#### 4.2.1 概念说明

有了 `cos / sin` 表，还要把它「作用」到 Q、K 上。对一个二维向量 \((x_1, x_2)\) 旋转 \(\theta\)：

\[
\begin{bmatrix} x'_1 \\ x'_2 \end{bmatrix}
=
\begin{bmatrix} \cos\theta & -\sin\theta \\ \sin\theta & \cos\theta \end{bmatrix}
\begin{bmatrix} x_1 \\ x_2 \end{bmatrix}
=
\begin{bmatrix} x_1\cos\theta - x_2\sin\theta \\ x_1\sin\theta + x_2\cos\theta \end{bmatrix}
\]

定义 `rotate_half([x_1,...,x_{d/2}, x_{d/2+1},...,x_d]) = [-x_{d/2+1},...,-x_d, x_1,...,x_{d/2}]`（把后半取负挪到前面、前半挪到后面），则整段旋转可以写成一行：

\[
x' = x \odot \cos\theta + \text{rotate\_half}(x) \odot \sin\theta
\]

RoPE 最迷人的性质来自旋转矩阵的正交性：\(R_m^\top R_n = R_{n-m}\)。于是注意力分数

\[
\langle R_m q,\ R_n k\rangle = q^\top R_m^\top R_n k = q^\top R_{n-m} k
\]

**只依赖相对位置 \(n-m\)**。这就是「写入绝对位置、得到相对位置」的本质。

#### 4.2.2 核心流程

`apply_rotary_pos_emb(q, k, cos, sin, unsqueeze_dim=1)` 的步骤：

1. 定义内部函数 `rotate_half(x)`：取后半取负、前半保持，拼接成「后半(取负) + 前半」。
2. 对 q 和 k 各做：`embed = q * cos + rotate_half(q) * sin`，其中 `cos/sin` 在 `unsqueeze_dim` 处插一维以便按头广播。
3. 转回原 dtype 返回新的 q、k。

#### 4.2.3 源码精读

[apply_rotary_pos_emb 与 rotate_half](model/model_minimind.py:80-84)（[永久链接](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L80-L84)）

```python
def apply_rotary_pos_emb(q, k, cos, sin, unsqueeze_dim=1):
    def rotate_half(x): return torch.cat((-x[..., x.shape[-1] // 2:], x[..., : x.shape[-1] // 2]), dim=-1)
    q_embed = ((q * cos.unsqueeze(unsqueeze_dim)) + (rotate_half(q) * sin.unsqueeze(unsqueeze_dim))).to(q.dtype)
    k_embed = ((k * cos.unsqueeze(unsqueeze_dim)) + (rotate_half(k) * sin.unsqueeze(unsqueeze_dim))).to(k.dtype)
    return q_embed, k_embed
```

要点：

- `rotate_half` 把 `[..., :d/2]` 和 `[..., d/2:]` 两半重组：后半取负在前、前半不变在后。这等价于对每个二维子向量 \((x_1, x_2)\) 输出 \((-x_2, x_1)\)，正是旋转矩阵里那一列。
- `cos.unsqueeze(1)` 把 `(seq, head_dim)` 变成 `(seq, 1, head_dim)`，与 q 的 `(bsz, seq, heads, head_dim)` 按头广播。
- `.to(q.dtype)` 收尾是为了在 fp16/bf16 混合精度下把中间 fp32 计算结果落回低精度。

它在注意力里的调用位置——位于 q/k 投影、q_norm/k_norm 之后，KV Cache 拼接之前：

[Attention.forward 中施加 RoPE](model/model_minimind.py:117-119)（[永久链接](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L117-L119)）

```python
xq, xk = self.q_norm(xq), self.k_norm(xk)
cos, sin = position_embeddings
xq, xk = apply_rotary_pos_emb(xq, xk, cos, sin)
```

注意 RoPE 施加在**旋转 Q 和 K**，但不施加在 V 上——因为只有 Q·K 的点积需要注入相对位置，V 只是加权求和的内容载体。

#### 4.2.4 代码实践：验证旋转保持向量范数

**实践目标**：验证 `apply_rotary_pos_emb` 是正交变换——旋转前后 Q 的范数不变。

**操作步骤**（示例代码）：

```python
# 示例代码：RoPE 不改变向量长度
import torch
from model.model_minimind import precompute_freqs_cis, apply_rotary_pos_emb
torch.manual_seed(0)
seq, head_dim = 4, 96
q = torch.randn(1, seq, 8, head_dim)         # (bsz, seq, heads, head_dim)
k = torch.randn(1, seq, 8, head_dim)
cos, sin = precompute_freqs_cis(head_dim, seq, 1e6, None)   # (seq, head_dim)
q2, k2 = apply_rotary_pos_emb(q, k, cos, sin)               # unsqueeze_dim=1
print("范数保持:", torch.allclose(q.norm(dim=-1), q2.norm(dim=-1), atol=1e-4))
```

**需要观察的现象**：打印 `True`。如果改成「随机不满足 \(\cos^2+\sin^2=1\)」的 cos/sin，结果会变 `False`。

**预期结果**：因为 `cos`、`sin` 来自同一个角度（`cos²+sin²=1`），`q*cos + rotate_half(q)*sin` 是严格的二维旋转，范数守恒。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `apply_rotary_pos_emb` 注释掉（不给模型任何位置信息），训练会出现什么现象？

> **答案**：模型对输入顺序不敏感（位置置换不变），无法学会「词序」，表现为语言建模 loss 不下降、生成乱码。这反向说明 RoPE 是不可缺少的位置注入点。

**练习 2**：为什么 RoPE 只乘在 Q、K 上，不乘在 V 上？

> **答案**：注意力输出是 \(\text{softmax}(QK^\top/\sqrt{d})\cdot V\)。位置信息只需进入相似度 \(QK^\top\) 即可（\(R_m^\top R_n\) 给出相对位置）；V 只是被加权求和的「内容」，给它加位置反而破坏内容的语义。

---

### 4.3 `rope_scaling` 配置：YaRN 长度外推

#### 4.3.1 概念说明

预训练/SFT 时，模型实际见过的序列长度有限（本项目 `original_max_position_embeddings = 2048`，见配置）。推理时若输入比训练长度更长，位置 \(m\) 会超出训练时见过的范围——尤其对那些**低频维**（转得慢、训练全程没转满一圈的维度），新的旋转角度是模型从未见过的，于是注意力失真、PPL 飙升。

YaRN（Yet another RoPE extensioN）的解法是**分维度处理**：

- **高频维**（训练期间转了很多圈）：天然能外推，**不动**。
- **低频维**（训练期间没转满一圈）：把频率**缩小** \(s\) 倍，让更长位置映射回训练时见过的旋转范围（插值）。
- 中间维度用线性渐变过渡。

直觉：高频维「已经见够」，外推安全；低频维「没见过新角度」，所以压扁它的频率，把长序列塞回老范围。这就是 README 第 Ⅳ 节所说「免训练地将上下文长度扩展」的原理（[README.md:1530-1559](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L1530-L1559)）。

#### 4.3.2 核心流程

设外推因子 \(s=\text{factor}\)。YaRN 把每个频率维按一个线性坡度 \(\gamma_i\in[0,1]\) 在「不动」与「除以 \(s\)」之间插值：

\[
f'(i) = f(i)\cdot\bigl((1-\gamma_i) + \gamma_i / s\bigr)
\]

- \(\gamma_i=0\)（高频维）：\(f'=f\)，频率不变（外推）。
- \(\gamma_i=1\)（低频维）：\(f'=f/s\)，频率缩小 \(s\) 倍（插值）。

\(\gamma_i\) 由一个 `ramp` 给出。代码先用 `inv_dim(b)` 反解出「波长等于 \(b\) 的那个维度下标」，分别取 `beta_fast=32`（高频边界）和 `beta_slow=1`（低频边界）得到区间 \([\text{low},\text{high}]\)，再在该区间内做 0→1 的线性 `clamp`：

\[
\text{inv\_dim}(b) = \frac{d\cdot \ln\bigl(L_{\text{orig}}/(2\pi b)\bigr)}{2\ln\theta_{\text{base}}}
\]

\[
\gamma_i = \mathrm{clamp}\!\left(\frac{i-\text{low}}{\text{high}-\text{low}},\ 0,\ 1\right),\quad
\text{low}=\lfloor\text{inv\_dim}(\beta_{\text{fast}})\rfloor,\ \text{high}=\lceil\text{inv\_dim}(\beta_{\text{slow}})\rceil
\]

#### 4.3.3 源码精读

先看配置：`inference_rope_scaling` 为真时，构造一个 `type=yarn` 的字典。

[MiniMindConfig 中的 rope_scaling 构造](model/model_minimind.py:31-39)（[永久链接](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L31-L39)）

```python
self.inference_rope_scaling = kwargs.get("inference_rope_scaling", False)
self.rope_scaling = {
    "beta_fast": 32, "beta_slow": 1, "factor": 16,
    "original_max_position_embeddings": 2048,
    "attention_factor": 1.0, "type": "yarn"
} if self.inference_rope_scaling else None
```

要点（务必注意一处出入）：

- 实际驱动数学的是 `factor=16`、`original_max_position_embeddings=2048`，所以**理论外推范围是 \(2048\times16=32768\)**。
- 但 `eval_llm.py` 的命令行帮助里写的是「4倍」（[eval_llm.py:41](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L41)），与配置里的 `factor=16` 不一致。这里的「4倍」更像是作者对**推荐实用区间**的描述，真正起作用的是配置中的 `factor=16`。阅读时应以源码数值为准。

再看 `precompute_freqs_cis` 里的 YaRN 分支：

[YaRN 频率缩放分支](model/model_minimind.py:64-73)（[永久链接](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L64-L73)）

```python
if rope_scaling is not None: # YaRN: f'(i) = f(i)((1-γ) + γ/s)
    orig_max, factor, beta_fast, beta_slow, attn_factor = (...)
    if end / orig_max > 1.0:                                  # 仅在超过训练长度时启用
        inv_dim = lambda b: (dim * math.log(orig_max / (b * 2 * math.pi))) / (2 * math.log(rope_base))
        low, high = max(math.floor(inv_dim(beta_fast)), 0), min(math.ceil(inv_dim(beta_slow)), dim // 2 - 1)
        ramp = torch.clamp((torch.arange(dim // 2).float() - low) / max(high - low, 0.001), 0, 1)
        freqs = freqs * (1 - ramp + ramp / factor)           # 即 (1-γ) + γ/s
```

要点：

- `end / orig_max > 1.0` 是守卫：只有请求的长度超过 `original_max` 才做外推，短序列保持原生 RoPE。
- `(1 - ramp + ramp / factor)` 正是 \((1-\gamma_i)+\gamma_i/s\)：`ramp=0` 时为 1（不变），`ramp=1` 时为 `1/factor`（缩放）。
- `max(high - low, 0.001)` 防止除零；`low/high` 被 clamp 到 \([0, dim/2-1]\) 内，保证下标合法。

最后是开关入口：`eval_llm.py` 把命令行参数透传进 `MiniMindConfig`。

[eval_llm.py 透传 inference_rope_scaling](eval_llm.py:15-20)（[永久链接](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L15-L20)）

```python
model = MiniMindForCausalLM(MiniMindConfig(
    hidden_size=args.hidden_size,
    num_hidden_layers=args.num_hidden_layers,
    use_moe=bool(args.use_moe),
    inference_rope_scaling=args.inference_rope_scaling
))
```

#### 4.3.4 代码实践：长文本上观察 YaRN 对生成/困惑度的影响

**实践目标**：在超过训练长度的长文本上，对比开关 YaRN 前后的模型表现。

**操作 A（生成质量，定性）**：准备一段较长的中文文本（如《西游记》白话片段，README 第 Ⅳ 节正是用此素材），把它的前半段作为 prompt 续写。

```bash
# 不开外推
python eval_llm.py --weight full_sft --max_new_tokens 512
# 开启 YaRN 外推
python eval_llm.py --weight full_sft --inference_rope_scaling --max_new_tokens 512
```

在手动输入模式粘贴长 prompt（尽量接近或超过 2048 token），观察开启 YaRN 后续写是否更连贯、是否更少重复。**待本地验证**（结果取决于具体权重与 prompt，README 给出的 PPL 对比图 [rope_ppl.png](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/images/rope_ppl.png) 显示开启后 PPL 明显下降）。

**操作 B（困惑度，定量，示例代码）**：`eval_llm.py` 本身不打印 PPL，下面给出一个最小 PPL 计算脚本（**示例代码**，需有 `full_sft_768.pth` 权重）：

```python
# 示例代码：对比 YaRN 开关下的长文本 PPL
import torch
from transformers import AutoTokenizer
from model.model_minimind import MiniMindConfig, MiniMindForCausalLM

device = "cuda" if torch.cuda.is_available() else "cpu"
tok = AutoTokenizer.from_pretrained("model")

def ppl_of(text, use_yarn, max_len=8192):
    cfg = MiniMindConfig(hidden_size=768, num_hidden_layers=8, inference_rope_scaling=use_yarn)
    m = MiniMindForCausalLM(cfg).half().to(device).eval()
    m.load_state_dict(torch.load("./out/full_sft_768.pth", map_location=device), strict=True)
    ids = tok(text, return_tensors="pt", truncation=True, max_length=max_len)["input_ids"].to(device)
    with torch.inference_mode():
        loss = m(ids, labels=ids).loss
    return torch.exp(loss).item()

text = open("long_text.txt", encoding="utf-8").read()  # 自备长文本
print("YaRN off PPL:", ppl_of(text, False))
print("YaRN on  PPL:", ppl_of(text, True))
```

**需要观察的现象**：在长度明显超过 2048 的文本上，`YaRN on` 的 PPL 应低于 `YaRN off`；在短文本上两者接近。

**预期结果**：与 README 第 Ⅳ 节描述一致——长文本场景下启用 YaRN 后 PPL 明显下降。**具体数值待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 YaRN 选择「高频维不动、低频维缩放」，而不是反过来？

> **答案**：高频维在训练期间已经转过很多圈，模型对「再转几圈」的角度是熟悉的，外推安全；低频维训练全程没转满一圈，新位置会落到完全没见过的角度，所以必须把频率压低（插值）把新位置映射回已见范围。反过来的话会把熟悉的高频维搞坏、又没解决低频维的根本问题。

**练习 2**：把配置里的 `original_max_position_embeddings` 从 2048 改成 8192，`end=32768` 时 YaRN 还会触发吗？频率缩放幅度变大还是变小？

> **答案**：仍会触发（`32768/8192=4>1`），但 `factor` 不变、`orig_max` 变大让 `inv_dim` 整体变大，`low/high` 边界移动，被判定为「需要插值」的低频维范围会改变，整体缩放幅度变小——因为模型「声称」自己训练时见过的长度更长，需要外推的程度更低了。

---

## 5. 综合实践：追踪一次长文本推理的位置编码全链路

把本讲三个模块串起来，做一次端到端的「源码追踪 + 数值验证」：

1. **配置层**：阅读 [model_minimind.py:31-39](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L31-L39)，确认 `--inference_rope_scaling` 会把 `rope_scaling` 从 `None` 变成 `type=yarn` 字典。
2. **预算层**：在 `precompute_freqs_cis` 入口处临时加一行 `print(rope_scaling)`（**仅用于观察，验证后删除，勿提交**），分别用 `eval_llm.py` 带与不带 `--inference_rope_scaling` 启动，确认打印值的变化。
3. **数值层**：运行 4.1.4 的绘图脚本，指出哪一段维度被 YaRN 缩放、哪一段没动。
4. **效果层**：运行 4.3.4 的 PPL 脚本，记录长文本下开关 YaRN 的 PPL 差值。
5. **结论**：用一两句话总结「位置编码从配置 → 预计算表 → 切片 → 施加到 Q/K → 影响注意力」的完整数据流，并说明 YaRN 在哪一步介入。

> 这个任务覆盖了 `precompute_freqs_cis`、`apply_rotary_pos_emb`、`rope_scaling` 三个最小模块的协作，做完后你应当能向别人讲清「MiniMind 是怎么给 token 标位置、又是怎么在长文本上免训练外推的」。

## 6. 本讲小结

- RoPE 把每个头的 `head_dim=96` 维切成 48 个二维平面，用 `precompute_freqs_cis` 预算一张「位置 × 频率」的 `cos/sin` 表，再由 `apply_rotary_pos_emb` 通过 `q*cos + rotate_half(q)*sin` 把旋转乘到 Q、K 上。
- 由于旋转矩阵的正交性 \(R_m^\top R_n = R_{n-m}\)，注意力分数只依赖相对位置，这是 RoPE 的本质优势。
- 推理时 RoPE 表按 `start_pos` 切片，支持 KV Cache 下的增量解码，且位置不重排。
- YaRN 针对长文本外推，按线性 `ramp` 分维度处理：高频维 \(\gamma=0\) 不动（外推），低频维 \(\gamma=1\) 频率除以 `factor=16`（插值），公式 \(f'(i)=f(i)((1-\gamma_i)+\gamma_i/s)\)。
- 触发条件是 `end / original_max > 1`（`original_max=2048`）；本项目 `attn_factor=1.0`，不做注意力温度修正。
- 一键开关是 `eval_llm.py --inference_rope_scaling`，它透传到 `MiniMindConfig.rope_scaling`；注意 CLI 帮助文字「4倍」与配置实际 `factor=16` 存在出入，以源码数值为准。

## 7. 下一步学习建议

- 接下来读 **u3-l4（SwiGLU 前馈网络与 MoE 路由）**，补齐 `MiniMindBlock` 的另一半——注意力之后的 FFN/MoE，从而拥有一个完整的 Transformer 层视图。
- 如果想进一步理解位置编码与注意力的耦合，建议带着本讲的结论回看 **u3-l2** 的 `Attention.forward`，重点跟踪 `position_embeddings → apply_rotary_pos_emb → repeat_kv → scaled_dot_product_attention` 这条链。
- 对长上下文工程感兴趣的读者，可对比 `convert_model.py` 转 Qwen3 格式时如何把 `rope_scaling` 写进 `config.json`，理解「原生 torch 模型」与「transformers/YaRN 标准 config」两种描述方式的等价性（对应 README 第 Ⅳ 节给出的 `config.json` 片段）。
