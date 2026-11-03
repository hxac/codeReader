# 讲义 u2-l4：RoPE 旋转位置编码

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `compute_rope_params` 中逆频率 `inv_freq`、角度矩阵 `angles` 与 cos/sin 表的计算方式，以及 `theta_base`（Qwen3 取 1,000,000）对频率谱的影响。
2. 解释 `apply_rope` 的 split-halves（前后半配对）旋转实现，并用代码验证 RoPE 旋转**不改变向量的 L2 范数**。
3. 说出 `offset` 参数如何服务于 KV cache 增量解码：新 token 必须用「绝对位置」的角度旋转，才能与缓存中已旋转的旧 K 形成正确的相对位置关系。
4. 对比源码注释中 split-halves 与 interleaved 两种数学等价的 RoPE 实现风格。

本讲只关注 `qwen3.py` 中与位置编码直接相关的三个最小模块：`compute_rope_params`、`apply_rope`、GQA 中的调用链。KV cache 的完整生命周期留给下一讲 u2-l5。

## 2. 前置知识

**为什么需要位置编码？** 自注意力的核心运算是 `Q @ K^T`，这个操作对输入序列的顺序完全不敏感——把一句话的词打乱，每个词算出的注意力分数一模一样。所以必须把「这是第几个词」的信息注入模型，否则「猫追狗」和「狗追猫」无法区分。

**绝对编码 vs 相对编码。** 最原始的办法是给第 m 个位置加一个位置向量（绝对编码）。但语言更关心**相对距离**：`分数(q_位置m, k_位置n)` 最好只依赖 `m - n`。「猫」和「追」隔 3 个词还是隔 30 个词，语义关系类似，绝对位置反而无关紧要。

**用旋转表示位置（RoPE 的直觉）。** RoPE（Rotary Position Embedding，旋转位置编码）把每个 query/key 向量的头维度看成若干个二维平面，把第 m 个位置的向量在这些平面上**旋转** `m × 频率` 的角度。回忆三角恒等式：

\[ \cos(\alpha - \beta) = \cos\alpha\cos\beta + \sin\alpha\sin\beta \]

两个分别旋转了角度 \(\alpha\) 与 \(\beta\) 的向量做内积时，结果只含 \(\cos(\alpha - \beta)\) 这类**角度差**项——位置 m 与位置 n 的交互自然只剩 `m - n`。这就是「旋转 → 相对位置」的全部直觉。

**正交变换保范数。** 二维旋转矩阵

\[
\begin{pmatrix} \cos\theta & -\sin\theta \\ \sin\theta & \cos\theta \end{pmatrix}
\]

是正交矩阵，它不改变向量长度，只改变方向。所以 RoPE 旋转后的向量范数不变，本讲的代码实践将直接验证这一点。

**多尺度频率。** 只用一个频率时，位置相差 \(2\pi/\theta\) 的整数倍的 token 会拿到完全相同的角度（位置混叠）。RoPE 给不同维度对分配从高到低指数衰减的频率：高频区分相邻位置，低频区分远距离位置，类似傅里叶级数用不同波长的正弦波叠加表达信号。

需要的数学工具只有：三角函数、矩阵乘法、L2 范数 \(\|x\|_2 = \sqrt{\sum_i x_i^2}\)。

## 3. 本讲源码地图

| 位置 | 作用 |
|---|---|
| [reasoning_from_scratch/qwen3.py:L25](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L25) | 配置字典中的 `rope_base`（RoPE 的 θ 基准，Qwen3 取 1,000,000） |
| [reasoning_from_scratch/qwen3.py:L48-L54](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L48-L54) | 模型初始化时预计算 cos/sin 表并注册为非持久 buffer |
| [reasoning_from_scratch/qwen3.py:L63-L73](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L63-L73) | `forward` 中根据 KV cache 状态计算位置起点 `pos_start` |
| [reasoning_from_scratch/qwen3.py:L92-L96](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L92-L96) | 把 cos/sin 与 `start_pos` 逐层传入 TransformerBlock |
| [reasoning_from_scratch/qwen3.py:L197-L213](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L197-L213) | GQA 中的调用点：QK-Norm 之后对 Q/K 应用 `apply_rope` |
| [reasoning_from_scratch/qwen3.py:L228-L277](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L228-L277) | 源码注释：split-halves 与 interleaved 两种等价风格的总览 |
| [reasoning_from_scratch/qwen3.py:L280-L299](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L280-L299) | `compute_rope_params`：本讲模块一 |
| [reasoning_from_scratch/qwen3.py:L302-L320](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L302-L320) | `apply_rope`：本讲模块二 |
| [tests/test_qwen3.py:L86-L134](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3.py#L86-L134) | `test_rope`：用 HuggingFace 参考实现验证本仓库 RoPE 数值一致 |

本讲的关键源码文件只有一个：`reasoning_from_scratch/qwen3.py`；测试文件 `tests/test_qwen3.py` 用于实践环节的对照。

## 4. 核心概念与源码讲解

### 4.1 逆频率与角度表：compute_rope_params

#### 4.1.1 概念说明

`compute_rope_params` 解决的问题是：**提前算好一张「每个位置 × 每个维度对」的旋转角度余弦/正弦表**，模型前向时只做查表和逐元素乘法，不在每个层重复计算三角函数。

它输出的 `cos`、`sin` 两张表形状都是 `(context_length, head_dim)`——行是位置，列是头维度。对 Qwen3 0.6B 而言是 `(40960, 128)`。

#### 4.1.2 核心流程

计算过程分四步：

```text
1. 逆频率：inv_freq[i] = 1 / θ^(2i/d)        i = 0, 1, ..., d/2-1   共 d/2 个频率
2. 位置序列：positions = 0, 1, ..., L-1       L = context_length
3. 角度矩阵：angles[m, i] = m × inv_freq[i]   形状 (L, d/2)
4. 拼接成对：cos = cos([angles | angles])     形状 (L, d)，前半与后半共享同一角度
             sin = sin([angles | angles])
```

写成数学式：

\[ \text{inv\_freq}_i = \theta^{-2i/d}, \qquad \text{angles}[m, i] = m \cdot \text{inv\_freq}_i \]

频率随维度指数衰减。以本讲实践的迷你配置 `head_dim=8、theta_base=1e6` 为例：

| 维度对 i | 指数 \(2i/d\) | inv_freq | 位置 15 时的角度 |
|---|---|---|---|
| 0 | 0 | 1.0 | 15 rad（已转 2 圈多） |
| 1 | 0.25 | ≈ 0.0316 | 0.47 rad |
| 2 | 0.5 | 0.001 | 0.015 rad |
| 3 | 0.75 | ≈ 3.16e-5 | 4.7e-4 rad（几乎没动） |

高频对（i=0）大约每 \(2\pi \approx 6.28\) 个位置转满一圈，负责精细的局部位置；低频对（i=3）走完整个上下文也只转过不到 0.001 弧度，负责长程位置。`theta_base` 越大，低频端的频率越小、可区分的距离越长——这正是 Qwen3 把 `rope_base` 从原论文的 10,000 提高到 1,000,000 的原因：它要服务 40,960 的上下文长度。

#### 4.1.3 源码精读

逆频率的计算，一步向量化完成（示例代码之外的部分均为仓库原码）：

[reasoning_from_scratch/qwen3.py:L280-L290](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L280-L290) —— 断言头维度必须为偶数（维度要两两配对）；用 `torch.arange(0, head_dim, 2)` 生成 \(0, 2, 4, \dots, d-2\) 共 d/2 个指数，除以 `head_dim` 后作为 θ 的幂指数取倒数得到 `inv_freq`；`positions` 是 0 到 L-1 的位置序列；外积 `positions.unsqueeze(1) * inv_freq.unsqueeze(0)` 得到形状 `(L, d/2)` 的角度矩阵。

```python
def compute_rope_params(head_dim, theta_base=10_000, context_length=4096, dtype=torch.float32):
    assert head_dim % 2 == 0, "Embedding dimension must be even"

    # Compute the inverse frequencies
    inv_freq = 1.0 / (theta_base ** (torch.arange(0, head_dim, 2, dtype=dtype)[: (head_dim // 2)].float() / head_dim))

    # Generate position indices
    positions = torch.arange(context_length, dtype=dtype)

    # Compute the angles
    angles = positions.unsqueeze(1) * inv_freq.unsqueeze(0)  # Shape: (context_length, head_dim // 2)
```

[reasoning_from_scratch/qwen3.py:L292-L299](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L292-L299) —— `torch.cat([angles, angles], dim=1)` 把角度矩阵左右复制拼接成 `(L, d)`：split-halves 布局中，前半 `x1` 的第 i 维与后半 `x2` 的第 i 维配成一对、共用角度 \(\theta_i\)，所以 cos/sin 表必须与输入向量同宽 `head_dim`，才能在 `apply_rope` 里逐元素相乘。最后对整张表取 cos/sin 返回。

```python
    # Expand angles to match the head_dim
    angles = torch.cat([angles, angles], dim=1)  # Shape: (context_length, head_dim)

    # Precompute sine and cosine
    cos = torch.cos(angles)
    sin = torch.sin(angles)

    return cos, sin
```

两张表的存储代价：`(40960, 128)` 的 fp32 张量每张约 21 MB，cos 与 sin 共约 42 MB。模型初始化时它们只被计算一次：

[reasoning_from_scratch/qwen3.py:L48-L54](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L48-L54) —— `Qwen3Model.__init__` 用配置中的 `head_dim`、`rope_base`、`context_length` 调用 `compute_rope_params`，并把结果注册为 buffer。`persistent=False` 表示这两张表是纯函数计算结果而非可学习参数，不写入 state_dict，因此加载官方权重时不会发生键冲突（buffer 注册机制已在 u2-l1 讲过，此处承接）。

```python
        cos, sin = compute_rope_params(
            head_dim=head_dim,
            theta_base=cfg["rope_base"],
            context_length=cfg["context_length"]
        )
        self.register_buffer("cos", cos, persistent=False)
        self.register_buffer("sin", sin, persistent=False)
```

注意一个数值细节：`compute_rope_params` 的 `dtype` 参数默认 `torch.float32`，而模型配置的 `dtype` 是 `bfloat16`（[qwen3.py:L26](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L26)）；初始化处没有传 dtype，所以 cos/sin 表以 fp32 存储——三角函数值对精度敏感，用 fp32 保存是刻意的数值稳定选择。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「不同维度对以不同速度旋转」，建立对逆频率的直观感受。

**操作步骤**（示例代码）：

```python
import torch
from reasoning_from_scratch.qwen3 import compute_rope_params

head_dim, theta_base, L = 8, 1_000_000, 16

# 1) 手动复算逆频率，对照源码 L284 的公式
inv_freq = 1.0 / (theta_base ** (torch.arange(0, head_dim, 2).float() / head_dim))
print("inv_freq:", inv_freq)

# 2) 拿官方函数算 cos/sin 表
cos, sin = compute_rope_params(head_dim=head_dim, theta_base=theta_base, context_length=L)
print("cos shape:", cos.shape)   # 期望 (16, 8)

# 3) 观察第 0 对（高频）与第 3 对（低频）随位置的变化
print("sin[:, 0]:", sin[:, 0])   # 高频：随位置剧烈振荡
print("sin[:, 3]:", sin[:, 3])   # 低频：几乎恒为 0
```

**需要观察的现象**：`sin[:, 0]` 在 16 个位置里来回摆动多轮；`sin[:, 3]` 的值全部接近 0（位置 15 时角度才 4.7e-4 弧度）。

**预期结果**：`inv_freq ≈ [1.0, 0.0316, 0.001, 3.16e-5]`；`cos.shape == (16, 8)`。若把 `theta_base` 换成 10_000 再跑一遍，最后一项会变成 `1e-3`——θ 变小，低频端频率升高，长程区分能力变弱。

#### 4.1.5 小练习与答案

**练习 1**：把 `theta_base` 从 1,000,000 改为 10,000、`head_dim=8`，`inv_freq` 的最后一项是多少？

**答案**：\(10^{4 \times (-6/8)} = 10^{-3} = 0.001\)。即频率范围从 `[1, 3.16e-5]` 收窄为 `[1, 1e-3]`，最低频率升高约 32 倍，说明大 θ 换来了更低的低频端、适配更长上下文。

**练习 2**：为什么 `angles` 要 `torch.cat([angles, angles], dim=1)` 复制拼接，而不是直接用 `(L, d/2)` 的表？

**答案**：split-halves 布局把向量分成前后两半，第 i 对是 `(x1[i], x2[i])`，两者共用同一个旋转角 \(\theta_i\)。`apply_rope` 中要做 `x * cos + rotated * sin` 的逐元素乘法，cos/sin 必须与 x 同宽 `head_dim`，因此角度表也要拼到同样宽度，前半与后半填同一份角度。

**练习 3**：Qwen3 0.6B 的 cos/sin 两张表共占多少显存？为什么它们不值得放进 state_dict？

**答案**：`40960 × 128 × 4 字节 ≈ 21 MB`/张，两张共约 42 MB。它们由 `(head_dim, rope_base, context_length)` 三个超参完全决定，是确定性函数的输出而非学习到的参数，`persistent=False` 让它们不进 checkpoint，加载权重时也避免键不匹配。

### 4.2 split-halves 旋转：apply_rope

#### 4.2.1 概念说明

`apply_rope` 是真正执行旋转的函数：把形状 `(batch, num_heads, seq_len, head_dim)` 的 Q 或 K 张量，按位置旋转。它采用 **split-halves（前后半配对）** 风格：向量的前半 `x1` 与后半 `x2` 的同下标维度组成一个二维子空间，在每个子空间内做普通的二维旋转。

由于二维旋转是正交变换，整个操作不改变每个 token 向量的 L2 范数，只改变方向——位置信息以「方向」的形式编码，而不污染向量的「长度」（幅值信息）。

#### 4.2.2 核心流程

对位置 m 的向量 \(x = [x_1 | x_2]\)（各 d/2 维），第 i 对 \((x_{1,i}, x_{2,i})\) 旋转角度 \(\theta_i = m \cdot \text{inv\_freq}_i\)：

\[
\begin{pmatrix} x'_{1,i} \\ x'_{2,i} \end{pmatrix}
=
\begin{pmatrix}
\cos\theta_i & -\sin\theta_i \\
\sin\theta_i & \cos\theta_i
\end{pmatrix}
\begin{pmatrix} x_{1,i} \\ x_{2,i} \end{pmatrix}
=
\begin{pmatrix}
x_{1,i}\cos\theta_i - x_{2,i}\sin\theta_i \\
x_{2,i}\cos\theta_i + x_{1,i}\sin\theta_i
\end{pmatrix}
\]

展开成向量形式（源码用的写法）：

\[ x' = x \odot \cos\theta + \mathrm{rot}(x) \odot \sin\theta, \qquad \mathrm{rot}(x) = [-x_2 \mid x_1] \]

执行流程：

```text
1. x 沿最后一维切成两半：x1 = x[..., :d/2]，x2 = x[..., d/2:]
2. 按行切片 cos/sin 表：cos[offset : offset+seq_len]，再扩维到 (1, 1, seq_len, d) 广播
3. 构造 rot(x) = concat(-x2, x1)
4. x_rotated = x * cos + rot(x) * sin
5. 转回输入 dtype 返回
```

**split-halves 与 interleaved 的对比**（对应源码注释 [qwen3.py:L228-L277](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L228-L277)）：两种风格都把 d 维向量看成 d/2 个二维子空间分别旋转，数学上完全等价，区别只在**哪些维度被配成一对**：

- **split-halves**（本仓库、HuggingFace Transformers）：前一半与后一半配对，`(x0, x_{d/2})`、`(x1, x_{d/2+1})`……注释中画出了对应的块对角旋转矩阵，`[cosθ0, 0, -sinθ0, 0; 0, cosθ1, 0, -sinθ1; sinθ0, 0, cosθ0, 0; 0, sinθ1, 0, cosθ1]`。
- **interleaved**（RoPE 原论文、Llama 仓库）：相邻的偶/奇维配对，`(x0, x1)`、`(x2, x3)`……旋转矩阵是标准的 2×2 块沿对角线排列。

两者编码相同的相对位置信息；但因为配对方式不同，**用一种布局训练的权重不能直接在另一种布局下使用**。本仓库选择 split-halves 是为了与 HuggingFace 的 Qwen3 权重逐数值对齐（`test_rope` 正是验证这一点）。

#### 4.2.3 源码精读

[reasoning_from_scratch/qwen3.py:L302-L313](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L302-L313) —— 解包出四维形状并断言头维度为偶数；把 x 切成前后两半；用 `offset` 行切片 cos/sin 表并 `unsqueeze` 两次成 `(1, 1, seq_len, head_dim)`，让同一份角度广播到所有 batch 和所有注意力头。

```python
def apply_rope(x, cos, sin, offset=0):
    # x: (batch_size, num_heads, seq_len, head_dim)
    batch_size, num_heads, seq_len, head_dim = x.shape
    assert head_dim % 2 == 0, "Head dimension must be even"

    # Split x into first half and second half
    x1 = x[..., : head_dim // 2]  # First half
    x2 = x[..., head_dim // 2:]  # Second half

    # Adjust sin and cos shapes
    cos = cos[offset:offset + seq_len, :].unsqueeze(0).unsqueeze(0)  # Shape: (1, 1, seq_len, head_dim)
    sin = sin[offset:offset + seq_len, :].unsqueeze(0).unsqueeze(0)
```

[reasoning_from_scratch/qwen3.py:L315-L320](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L315-L320) —— 旋转本体：`rotated = cat(-x2, x1)` 实现 \(\mathrm{rot}(x)\)；逐元素乘加完成「自身转 cos 分量 + 正交分量转 sin 分量」。最后一行 `.to(dtype=x.dtype)` 把结果转回输入精度——cos/sin 表是 fp32，与 bfloat16 的 x 相乘后类型会提升为 fp32，转回去避免后续注意力计算的显存翻倍；源码注释明确说明「施加 cos/sin 旋转之后用低精度没有问题」。

```python
    # Apply the rotary transformation
    rotated = torch.cat((-x2, x1), dim=-1)
    x_rotated = (x * cos) + (rotated * sin)

    # It's ok to use lower-precision after applying cos and sin rotation
    return x_rotated.to(dtype=x.dtype)
```

对照数学式：`x * cos + rotated * sin` 展开后的前半是 \(x_1\cos\theta - x_2\sin\theta\)、后半是 \(x_2\cos\theta + x_1\sin\theta\)，正是 4.2.2 中的二维旋转公式。还可以在 [tests/test_qwen3.py:L86-L134](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_qwen3.py#L86-L134) 的 `test_rope` 中看到权威背书：它用 `transformers` 的 `Qwen3RotaryEmbedding` 与 `apply_rotary_pos_emb` 生成参考 cos/sin 与旋转结果，再用 `torch.testing.assert_close` 逐张量断言本仓库实现与其完全一致（该测试在未安装 `transformers` 时自动跳过）。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：验证「RoPE 旋转不改变向量范数」，并观察同一 token 在不同 `offset`（即不同位置）下被旋转到不同方向。

**操作步骤**（示例代码）：

```python
import torch
from reasoning_from_scratch.qwen3 import compute_rope_params, apply_rope

torch.manual_seed(123)

# 1) 生成 cos/sin 表：head_dim=8, context_length=16
cos, sin = compute_rope_params(head_dim=8, context_length=16)

# 2) 构造一个单 token 的头向量，形状 (batch, num_heads, seq_len, head_dim)
x = torch.randn(1, 1, 1, 8)

# 3) 分别在位置 0 与位置 5 旋转同一个 token
x_rot0 = apply_rope(x, cos, sin, offset=0)
x_rot5 = apply_rope(x, cos, sin, offset=5)

# 4) 验证范数不变（误差 < 1e-5）
print(f"原始范数:     {x.norm().item():.6f}")
print(f"offset=0 范数: {x_rot0.norm().item():.6f}")
print(f"offset=5 范数: {x_rot5.norm().item():.6f}")
assert torch.allclose(x.norm(), x_rot0.norm(), atol=1e-5)
assert torch.allclose(x.norm(), x_rot5.norm(), atol=1e-5)
print("范数不变验证通过")

# 5) 观察两个位置的旋转结果差异
diff = (x_rot0 - x_rot5).abs().max().item()
print(f"offset=0 与 offset=5 的最大元素差: {diff:.6f}")
```

**需要观察的现象**：三个范数打印值在小数点后 5 位内一致；而 `x_rot0` 与 `x_rot5` 是两个明显不同的向量（最大元素差与向量本身的量级相当）。

**预期结果**：两个断言均通过。原因在数学上很明确：split-halves 旋转是 d/2 个正交二维旋转的直和，整体仍是正交变换，\(\|x'\|_2 = \|x\|_2\)。而 `offset` 改变了角度 \(\theta_i = m \cdot \text{inv\_freq}_i\) 中的位置 m，方向因此不同。若在你的机器上因随机数版本导致断言失败，请以「范数差 < 1e-5」为准自行核对打印值。

#### 4.2.5 小练习与答案

**练习 1**：如果 `head_dim` 是奇数（例如 7），`apply_rope` 会发生什么？

**答案**：直接触发 `assert head_dim % 2 == 0` 断言错误（[qwen3.py:L305](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L305)）。`compute_rope_params` 在 [qwen3.py:L281](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L281) 有同样的断言。旋转必须两两配对，奇数维无法分成对。

**练习 2**：手写 d=4 的 split-halves 旋转矩阵，标出每个元素对应的 cos/sin 表位置。

**答案**：配对为 (x0, x2)、(x1, x3)，旋转矩阵为

\[
\begin{pmatrix}
\cos\theta_0 & 0 & -\sin\theta_0 & 0 \\
0 & \cos\theta_1 & 0 & -\sin\theta_1 \\
\sin\theta_0 & 0 & \cos\theta_0 & 0 \\
0 & \sin\theta_1 & 0 & \cos\theta_1
\end{pmatrix}
\]

cos 表提供第 0/1 列的系数，sin 表提供第 2/3 列的系数——与源码注释 [qwen3.py:L245-L253](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L245-L253) 中的矩阵一致。

**练习 3**：为什么 `apply_rope` 最后要把结果 `.to(dtype=x.dtype)` 转回 bfloat16？直接用 fp32 结果行不行？

**答案**：cos/sin 表是 fp32，`x * cos` 的类型提升会把整个 (b, 头数, 序列长, 128) 张量升到 fp32，后续与 K 的矩阵乘、KV cache 存储的显存都会翻倍。旋转本身在 fp32 下完成已保证数值精度，转回 bf16 是显存与吞吐的工程取舍，源码注释「It's ok to use lower-precision after applying cos and sin rotation」说的正是这件事。

### 4.3 GQA 中的调用链与 offset 的作用

#### 4.3.1 概念说明

RoPE 在模型里的位置有三条铁律：

1. **只旋转 Q 和 K，不旋转 V**——位置信息只需要参与「打分」（\(QK^T\)），V 是纯内容载荷，加位置反而污染内容。
2. **在 QK-Norm 之后旋转**——承接 u2-l3 的链路：投影 → 分头 → q_norm/k_norm → RoPE。
3. **KV cache 里存的是已旋转的 K**——旧 token 的旋转结果连同 K 一起被缓存，增量解码时只须用 `offset` 给新 token 正确的绝对位置角度。

`offset` 参数正是第 3 条的产物：整段输入（prefill）从位置 0 开始，取表的第 0 行起；带缓存逐 token 解码时，新 token 的位置是 `current_pos`，必须从表的第 `current_pos` 行取角度。

#### 4.3.2 核心流程

完整调用链（自顶向下）：

```text
Qwen3Model.__init__
  └─ compute_rope_params(head_dim, rope_base, context_length) → cos/sin 表（一次性）
       ↓ 注册为非持久 buffer
Qwen3Model.forward(in_idx, cache)
  ├─ pos_start = current_pos if cache is not None else 0
  ├─ pos_end = pos_start + num_tokens（越界抛 ValueError）
  └─ for block in trf_blocks: block(x, mask, self.cos, self.sin, start_pos=pos_start, ...)
       ↓
TransformerBlock.forward → self.att(x, mask, cos, sin, start_pos=start_pos, cache=cache)
       ↓
GroupedQueryAttention.forward
  ├─ 投影、分头、QK-Norm
  ├─ queries = apply_rope(queries, cos, sin, offset=start_pos)
  ├─ keys_new = apply_rope(keys_new,  cos, sin, offset=start_pos)
  ├─ cache 非空: keys = cat([prev_k, keys_new], dim=2)   ← 拼接的是已旋转的 K
  └─ 注意力打分：Q·K^T 只依赖位置差 m − n
```

为什么「已旋转的旧 K + offset 旋转的新 Q」能得到正确结果？设旧 token 在位置 n、新 token 在位置 m，两者各自按绝对位置旋转后，内积中的角度项只剩 \(\cos((m-n)\theta_i)\)（见 4.2.2 的推导）——相对位置关系自动成立。这就是 offset 的全部意义：**它让增量解码无需重算任何旧 token 的旋转**。

#### 4.3.3 源码精读

GQA 中的调用点（承接 u2-l3 讲过的前半段链路）：

[reasoning_from_scratch/qwen3.py:L197-L204](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L197-L204) —— 先做可选的 QK-Norm（RoPE 前的逐头归一化，u2-l3 已详述），随后对 Q 与 K 各调用一次 `apply_rope`，`offset` 接的就是 `start_pos`。

```python
        # Optional normalization
        if self.q_norm:
            queries = self.q_norm(queries)
        if self.k_norm:
            keys_new = self.k_norm(keys_new)

        # Apply RoPE
        queries = apply_rope(queries, cos, sin, offset=start_pos)
        keys_new = apply_rope(keys_new, cos, sin, offset=start_pos)
```

[reasoning_from_scratch/qwen3.py:L206-L213](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L206-L213) —— cache 非空时把**已旋转**的 `keys_new` 与缓存中的 `prev_k` 沿序列维拼接；无 cache 时 `start_pos = 0  # reset RoPE` 是模块级兜底：单独使用 GQA（例如单元测试里直接调用）且没传 cache 时，位置偏移没有意义，归零以保证从位置 0 旋转（在 `Qwen3Model.forward` 层面，无 cache 时 `pos_start` 本来就是 0，见下一段，两处互为双保险）。

```python
        if cache is not None:
            prev_k, prev_v = cache
            keys = torch.cat([prev_k, keys_new], dim=2)
            values = torch.cat([prev_v, values_new], dim=2)
        else:
            start_pos = 0  # reset RoPE
            keys, values = keys_new, values_new
        next_cache = (keys, values)
```

`offset` 的来源在模型层：

[reasoning_from_scratch/qwen3.py:L63-L73](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L63-L73) —— `pos_start` 在有缓存时取 `current_pos`（已处理的 token 数，即新 token 的绝对位置），无缓存时为 0；`pos_end` 超过 `context_length` 直接抛 `ValueError`（RoPE 表也只有这么多行）；有缓存时更新 `current_pos = pos_end` 供下一次解码使用。

```python
        num_tokens = x.shape[1]
        pos_start = self.current_pos if cache is not None else 0
        pos_end = pos_start + num_tokens
        if pos_end > self.cfg["context_length"]:
            raise ValueError(...)

        if cache is not None:
            self.current_pos = pos_end
```

[reasoning_from_scratch/qwen3.py:L92-L96](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L92-L96) —— 主循环把 `self.cos`、`self.sin` 与 `start_pos=pos_start` 一起传进每个 TransformerBlock，再由块转交给 GQA（[qwen3.py:L123-L127](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L123-L127)）。所有 28 层共享同一张 cos/sin 表。

```python
        for i, block in enumerate(self.trf_blocks):
            blk_cache = cache.get(i) if cache else None
            x, new_blk_cache = block(x, mask, self.cos, self.sin,
                                     start_pos=pos_start,
                                     cache=blk_cache)
```

位置状态的生命周期由 [qwen3.py:L56](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L56)（初始化为 0）与 [qwen3.py:L104-L105](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L104-L105)（`reset_kv_cache` 归零）管理：开始一段新对话必须重置，否则 `current_pos` 从上一段末尾继续，RoPE 角度与真实位置错位（这属于下一讲 u2-l5 的 KV cache 生命周期主题，此处只需知道 `current_pos` 同时是 RoPE 的 offset 来源）。

#### 4.3.4 代码实践

**实践目标**：用最小实验证明「带 offset 的增量旋转」与「整段一次性旋转」完全等价——这正是 KV cache 增量解码能省掉重复计算的根基。

**操作步骤**（示例代码）：

```python
import torch
from reasoning_from_scratch.qwen3 import compute_rope_params, apply_rope

torch.manual_seed(0)
cos, sin = compute_rope_params(head_dim=8, context_length=16)

# 模拟 prefill：一次输入 4 个 token，占据位置 0-3
q_prompt = torch.randn(1, 1, 4, 8)
q_prompt_rot = apply_rope(q_prompt, cos, sin, offset=0)   # 整段旋转，行切片 [0:4]

# 模拟 decode：下一个 token 的绝对位置是 4，用 offset=4 单独旋转
q_new = torch.randn(1, 1, 1, 8)
q_new_rot = apply_rope(q_new, cos, sin, offset=4)         # 行切片 [4:5]

# 等价性验证：把 5 个 token 拼成整段、从 offset=0 旋转，第 5 个结果应与上面一致
q_all = torch.cat([q_prompt, q_new], dim=2)
q_all_rot = apply_rope(q_all, cos, sin, offset=0)
print(torch.allclose(q_all_rot[:, :, 4], q_new_rot[0, 0], atol=1e-6))  # 期望 True
```

**需要观察的现象**：`allclose` 打印 `True`——单独用 `offset=4` 旋转第 5 个 token，与把它拼进整段从 0 旋转，结果逐元素一致。

**预期结果**：True。因为 `cos[offset:offset+seq_len]` 的行切片保证每个 token 取到的都是自己绝对位置那一行角度。这也说明：若解码时忘记更新 offset（一直传 0），新 token 会永远使用位置 0 的角度，注意力分数的相对位置关系被破坏，长文本生成质量会明显劣化。

#### 4.3.5 小练习与答案

**练习 1**：KV cache 里存的 K 是旋转前还是旋转后的？这样存有什么好处？

**答案**：旋转后的（[qwen3.py:L204](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L204) 先对 `keys_new` 应用 RoPE，[qwen3.py:L208](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L208) 才把它 `cat` 进缓存）。好处是旧 token 的旋转只需做一次，之后每步解码只旋转当前这 1 个新 token 的 Q 和 K，成本从 O(已有序列长度) 降到 O(1)。

**练习 2**：假设某段对话 prefill 了 100 个 token，接下来逐 token 解码。写出前 3 次前向各自的 `pos_start` 与 `apply_rope` 实际使用的表行范围。

**答案**：prefill：`pos_start=0`，行切片 `[0:100]`；第 1 次解码：`current_pos=100`，`pos_start=100`，行切片 `[100:101]`；第 2 次解码：`pos_start=101`，行切片 `[101:102]`。`current_pos` 每次前向后加上新 token 数（[qwen3.py:L73](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L73)）。

**练习 3**：RoPE 为什么不作用于 V（value 向量）？

**答案**：注意力机制中位置信息只需要影响打分环节 \(QK^T\)：旋转 Q/K 后内积自动只依赖位置差。V 是注意力加权平均的「内容载荷」，如果也加位置，等于把位置混进被取回的内容里，反而破坏了「按相关性取内容」的语义。

## 5. 综合实践

**任务：验证 RoPE 的「相对位置」性质——注意力分数只依赖位置差。**

本任务把 4.1 的角度表、4.2 的旋转、4.3 的 offset 三个模块串起来。理论依据（4.2.2 的推导）：位置 m 的 q 与位置 n 的 k 各自旋转后的内积满足

\[ \langle R_m q, R_n k \rangle = \sum_i \left[ (q_{1,i}k_{1,i} + q_{2,i}k_{2,i})\cos((m{-}n)\theta_i) + (q_{1,i}k_{2,i} - q_{2,i}k_{1,i})\sin((m{-}n)\theta_i) \right] \]

每一项都只含位置差 \(m - n\)。

**操作步骤**（示例代码）：

```python
import torch
from reasoning_from_scratch.qwen3 import compute_rope_params, apply_rope

torch.manual_seed(42)
cos, sin = compute_rope_params(head_dim=8, context_length=32)

q = torch.randn(1, 1, 1, 8)   # 一对固定的 query/key 头向量
k = torch.randn(1, 1, 1, 8)

def score(m, n):
    """q 放在位置 m、k 放在位置 n，返回旋转后的内积（未除以 sqrt(d) 的原始分数）"""
    qm = apply_rope(q, cos, sin, offset=m)
    kn = apply_rope(k, cos, sin, offset=n)
    return (qm * kn).sum().item()

# 第 1 组：位置差都等于 1 → 分数应几乎相同
print(score(2, 1), score(5, 4), score(10, 9))

# 第 2 组：位置差各不相同 → 分数应明显不同
print(score(2, 1), score(3, 1), score(2, 7))
```

**预期结果**：第 1 组三个数字在小数点后 5 位内一致（浮点误差来自 cos/sin 表的 fp32 精度）；第 2 组三个数字彼此不同。

**进阶（可选）**：若环境装有 `transformers`，运行 `pytest tests/test_qwen3.py::test_rope -v`，确认本仓库的 RoPE 与 HuggingFace 官方 `Qwen3RotaryEmbedding` 数值逐位一致（该测试在未安装 `transformers` 时会自动 skip）。若未安装或不确定运行结果，此步标注「待本地验证」。

## 6. 本讲小结

- `compute_rope_params` 用一条向量化公式生成逆频率 \(\text{inv\_freq}_i = \theta^{-2i/d}\)，外积位置得到角度矩阵，拼成 `(context_length, head_dim)` 的 cos/sin 表；模型初始化时算一次，注册为非持久 buffer，全部 28 层共享。
- 频率随维度指数衰减：高频对区分相邻位置，低频对区分长程位置；Qwen3 把 `rope_base` 提高到 1,000,000 以适配 40,960 的长上下文。
- `apply_rope` 采用 split-halves 风格：前后半同下标维度配对，`x' = x·cos + rot(x)·sin`；旋转是正交变换，不改变向量范数（主实践已验证），位置信息全部编码在方向上。
- split-halves 与 interleaved 数学等价，只是维度配对方式不同；本仓库选前者是为了与 HuggingFace 权重逐数值对齐。
- RoPE 只作用于 Q/K、且在 QK-Norm 之后；KV cache 存已旋转的 K，`offset=current_pos` 保证新 token 用绝对位置的角度旋转，内积自动只剩位置差。
- `current_pos` 同时是 RoPE 的 offset 来源与 KV cache 的位置游标，`reset_kv_cache` 将其归零；无 cache 路径的 `pos_start=0` 在模型层与 GQA 层各有一道保险。

## 7. 下一步学习建议

本讲的 `offset`/`start_pos`/`current_pos` 已经把脚踩进了 KV cache 的门槛。下一讲 **u2-l5「KV Cache 与因果掩码」** 将正面拆解 `KVCache` 类的预分配与逐层更新、`Qwen3Model.forward` 中 prefill 与缓存解码两种路径的掩码切片 `[pos_start:pos_end, :pos_end]`，以及超长输入的 `ValueError` 行为。建议在进入下一讲前，先完成本讲综合实践，确认你能解释「为什么第 1 组三个分数相等」；随后带着这个问题去读 u2-l5：既然旧 K 已缓存且已旋转，为什么掩码还要随位置动态变化？
