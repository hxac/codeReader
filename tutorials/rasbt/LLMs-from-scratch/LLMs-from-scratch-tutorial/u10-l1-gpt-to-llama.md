# GPT 转 Llama：RoPE 与 RMSNorm

## 1. 本讲目标

到 [u5-l4](u5-l4-weight-loading.md) 为止，我们已经能从零搭出一个 124M 的 GPT，并加载 OpenAI 的预训练权重让它说出连贯的英文。但 2023 年之后，真正统治开源榜单的是 Meta 的 **Llama** 系列。Llama 的「骨架」仍然是解码器型 Transformer，和我们的 GPT 同宗同源——这正是本讲的关键前提：**Llama 不是另起炉灶，而是对 GPT 做了若干处精准替换**。

本讲就带你完成这次「架构迁移」。我们以仓库里的 `standalone-llama32.ipynb`（一个自包含的 Llama 3.2 实现）为目标蓝本，对照前面章节搭好的 GPT，逐项替换。学完本讲，你应当能够：

1. 说出 GPT → Llama 的关键差异清单（RMSNorm、RoPE、SwiGLU、去偏置、去 dropout、GQA、低精度），并理解每一项的动机。
2. **手写 RMSNorm**，并讲清它和第 4 章 LayerNorm 的区别（少减均值、只做缩放）。
3. **读懂 RoPE（旋转位置编码）的数学与实现**：为什么对 Q/K 做 2D 旋就能同时编码绝对与相对位置，并能实现 `compute_rope_params` / `apply_rope`。
4. 理解 **Llama 3 的频率缩放（frequency scaling）** 如何让模型在不重训的前提下把上下文从 8k 扩展到 128k。
5. 读懂 `Llama3Model` 的整体组装，理解它如何把上面这些零件与 `GroupedQueryAttention` 缝合成一个 1B/3B 的完整模型。

> 本讲只讲**架构**（前向计算图与组件差异）。权重的下载、逐层映射（含 Llama 2 的 Q/K 维度重排 `permute`）放在 4.4 末尾作为「读代码」练习，不再逐行展开。

---

## 2. 前置知识

本讲是 advanced 阶段内容，假设你已经掌握：

- **GPT 模型组装**（[u4-l3](u4-l3-gpt-model-assembly.md)）：`GPTModel` = `tok_emb` + `pos_emb` + 堆叠的 `TransformerBlock` + `final_norm` + `out_head`；配置卡 `GPT_CONFIG_124M` 的七个超参。
- **TransformerBlock 与残差连接**（[u4-l2](u4-l2-transformer-block.md)）：pre-LayerNorm、`drop_shortcut`、形状保持。
- **基础组件**（[u4-l1](u4-l1-gpt-building-blocks.md)）：LayerNorm（沿特征维归到均值 0 方差 1，再仿射）、GELU、FeedForward（`emb_dim → 4·emb_dim → emb_dim`）。
- **多头注意力**（[u3-l3](u3-l3-multihead-attention.md)）：`view` + `transpose` 无拷贝切头、因果掩码、缩放因子是 `head_dim`。
- **权重加载与 weight tying**（[u5-l4](u5-l4-weight-loading.md)）：`state_dict`、`assign` 兜底、`out_head` 复用 `tok_emb` 的 124M 口径。
- **可学习位置嵌入**（[u2-l4](u2-l4-token-positional-embeddings.md)）：`nn.Embedding(context_length, emb_dim)`，行数决定最大序列长度——这是 RoPE 要替换的对象。

另外，Llama 3 的注意力用的是 **分组查询注意力（GQA）**，它的完整原理与 KV 内存分析在 [u9-l3](u9-l3-attention-variants.md) 已经讲透。本讲把 GQA 当作「一个现成的注意力模块」直接组装，只在用到处做最少说明，不重复展开。

如果你对「为什么要给 token 加位置信息」还模糊，建议先回看 [u2-l4](u2-l4-token-positional-embeddings.md)——RoPE 的全部动机都来自「可学习绝对位置嵌入的两大软肋：不可外推、参数随长度增长」。

---

## 3. 本讲源码地图

本讲的源码集中在 `ch05/07_gpt_to_llama/` 目录，这是一个「把 GPT 改造成 Llama」的 bonus 专题，按推荐阅读顺序组织：

| 文件 | 作用 |
|------|------|
| [ch05/07_gpt_to_llama/standalone-llama32.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/07_gpt_to_llama/standalone-llama32.ipynb) | **本讲主蓝本**：一个自包含的 Llama 3.2 实现，含 `compute_rope_params`、`apply_rope`、`GroupedQueryAttention`、`TransformerBlock`、`Llama3Model`、`LLAMA32_CONFIG` |
| [ch05/07_gpt_to_llama/converting-gpt-to-llama2.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/07_gpt_to_llama/converting-gpt-to-llama2.ipynb) | **教学版**：逐项对比 GPT→Llama 2 的改动，提供从零手写的 `RMSNorm`、`SiLU`、`precompute_rope_params`、`compute_rope`（含数学公式与逐行注释） |
| [ch05/07_gpt_to_llama/converting-llama2-to-llama3.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/07_gpt_to_llama/converting-llama2-to-llama3.ipynb) | Llama 2→3 的增量改动，主要是 RoPE 的 `theta_base` 调大与频率缩放 |
| [ch05/07_gpt_to_llama/previous_chapters.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/07_gpt_to_llama/previous_chapters.py) | 复用的 `generate`、`text_to_token_ids`、`token_ids_to_text`（第 5 章解码器，Llama 原样复用） |
| [ch05/07_gpt_to_llama/tests/tests_rope_and_parts.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/07_gpt_to_llama/tests/tests_rope_and_parts.py) | 正确性测试：把自写的 RoPE/RMSNorm/SiLU 与 HuggingFace、LitGPT、`torch.nn.RMSNorm` 逐位对比 |

**两种实现风格并存，别混淆**：

- `converting-gpt-to-llama2.ipynb` 是「教学版」，**从零手写** `RMSNorm`、`SiLU`、`precompute_rope_params`、`compute_rope`，便于看清原理。
- `standalone-llama32.ipynb` 是「成品版」，凡是 PyTorch 已内置的就用内置：归一化用 `nn.RMSNorm`（需 PyTorch ≥ 2.4），激活用 `nn.functional.silu`；只有 RoPE 因为涉及 Llama 3 专属的频率缩放，仍保留自写的 `compute_rope_params` / `apply_rope`。

本讲讲解原理时优先引用「教学版」的手写实现，讲组装时引用「成品版」。

> 说明：本讲给出的 notebook 行号区间，均指 `.ipynb` 文件在 GitHub 上的 **JSON 源码行号**（notebook 以 JSON 存储，每行源码对应一个 JSON 数组元素）。`.py` 文件则为普通代码行号。

---

## 4. 核心概念与源码讲解

### 架构迁移总览：GPT → Llama 改了哪几刀

在深入每个组件前，先用一张表看清全貌。Llama 相对 GPT 的改动可以分成「换零件」和「调参数」两类：

| 维度 | GPT（第 4 章） | Llama 2 | Llama 3 / 3.2 | 动机 |
|------|----------------|---------|----------------|------|
| **归一化** | LayerNorm（减均值除方差 + 仿射） | **RMSNorm**（只除 RMS + 缩放） | RMSNorm | 省一次均值统计，更快、效果相当 |
| **位置编码** | 可学习绝对位置嵌入 `nn.Embedding` | **RoPE**（旋转 Q/K） | RoPE + **频率缩放** | 相对位置、可外推、不占参数 |
| **激活/前馈** | GELU + 两层 FFN | **SwiGLU**（SiLU 门控 + 三层 FFN） | SwiGLU | 门控提升表达力 |
| **偏置** | Linear 默认带 bias（GPT-2 `qkv_bias`） | **全部 `bias=False`** | `bias=False` | 省参数、对大模型更稳 |
| **Dropout** | `drop_rate=0.1`（emb/shortcut/attn） | **去掉** | 去掉 | 大模型靠数据量而非 dropout 正则 |
| **注意力** | 标准 MHA | MHA（7B）/ GQA（聊天版） | **GQA** | 省 KV cache 显存（见 u9-l3） |
| **精度** | float32 | **bfloat16** | bfloat16 | 省一半显存 |
| **RoPE base θ** | — | 10 000 | **500 000** | 更慢的旋转，适配超长上下文 |
| **上下文长度** | 1024 | 4096 | 8192 → **131 072**（靠频率缩放扩展） | 长上下文 |

> 关于激活：Llama 把 GPT 的 `GELU` 换成 `SiLU`（也叫 Swish，\(\text{silu}(x)=x\cdot\sigma(x)\)），并把前馈层从两层改成 **SwiGLU** 三层结构 \(\text{SwiGLU}(x)=\text{SiLU}(\text{Linear}_1(x))\cdot\text{Linear}_2(x)\) 再经 \(\text{Linear}_3\)。这部分逻辑直白：教学版见 [SiLU 实现](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/07_gpt_to_llama/converting-gpt-to-llama2.ipynb#L277-L282) 与 [SwiGLU 公式说明](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/07_gpt_to_llama/converting-gpt-to-llama2.ipynb#L317-L325)，成品版见 [FeedForward 实现](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/07_gpt_to_llama/standalone-llama32.ipynb#L127-L138)。本讲不再单设一节，在 4.4 组装时一并带过。

下面四节是本讲的四个核心模块，逐个吃透。

---

### 4.1 RMSNorm：用「均方根」替换 LayerNorm

#### 4.1.1 概念说明

第 4 章的 [LayerNorm](u4-l1-gpt-building-blocks.md) 做两件事：先把每个 token 的特征向量**中心化**（减均值）再**标准化**（除以标准差），最后做可学习的仿射（`scale · x + shift`）。

**RMSNorm（Root Mean Square Normalization）** 的洞察是：标准化里真正起作用的主要是「除以尺度」这一步，**减均值可以省掉**。于是它只计算**均方根（RMS）** 作为尺度，把输入除以它，再做一次可学习的逐维缩放：

\[
y_i = \frac{x_i}{\text{RMS}(x)}\,\gamma_i,\qquad \text{RMS}(x)=\sqrt{\epsilon+\frac{1}{n}\sum_{i=1}^{n} x_i^2}
\]

其中 \(x\in\mathbb{R}^n\) 是某个 token 的特征向量，\(\gamma\) 是可学习缩放（初始为全 1），\(\epsilon\) 防止除零。

与 LayerNorm 的关键区别：

- **不减均值**：LayerNorm 输出均值≈0、方差≈1；RMSNorm 不保证均值为 0，只把「尺度」归一到 1 附近。
- **只有一个可学习参数 \(\gamma\)（`weight`）**，没有偏移项 `shift`。
- **更省算力**：少算一次均值，且 `rsqrt` 可以一次性算出。
- 数学上 RMSNorm 是 LayerNorm 的一个特例（当输入均值恒为 0 时二者等价），实验上效果几乎不掉。

公式出处见教学版 notebook 的 [RMSNorm 说明单元](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/07_gpt_to_llama/converting-gpt-to-llama2.ipynb#L142-L149)。

#### 4.1.2 核心流程

```
输入 x: (batch, num_tokens, emb_dim)
1. means = x.pow(2).mean(dim=-1, keepdim=True)   # 每个 token 的均方，形状 (..., 1)
2. x_normed = x * torch.rsqrt(means + eps)       # 除以 sqrt(均方+eps)，等价于 / RMS
3. return (x_normed * self.weight).to(x.dtype)   # 逐维缩放，再转回原精度
```

注意第 2 步用的是 `torch.rsqrt`（reciprocal square root，\(1/\sqrt{\cdot}\)），它把「除法」变成「乘以倒数平方根」，比先 `sqrt` 再除更快，是大模型里常见的数值优化。

#### 4.1.3 源码精读

从零手写的 `RMSNorm`（教学版）：

```python
class RMSNorm(nn.Module):
    def __init__(self, emb_dim, eps=1e-5):
        super().__init__()
        self.eps = eps
        self.emb_dim = emb_dim
        self.weight = nn.Parameter(torch.ones(emb_dim)).float()   # γ，初始全 1

    def forward(self, x):
        means = x.pow(2).mean(dim=-1, keepdim=True)               # 均方（不是方差，没减均值）
        x_normed = x * torch.rsqrt(means + self.eps)              # 除以 RMS
        return (x_normed * self.weight).to(dtype=x.dtype)         # 缩放并还原精度
```

> 见 [converting-gpt-to-llama2.ipynb:L184-L194](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/07_gpt_to_llama/converting-gpt-to-llama2.ipynb#L184-L194)：`weight` 即公式里的 \(\gamma\)，`.to(dtype=x.dtype)` 是为了在 bfloat16 前向里把归一化中间过程留在高精度、输出再降回低精度。

而 `standalone-llama32.ipynb` 里直接调 PyTorch 内置 `nn.RMSNorm`：

> 见 [standalone-llama32.ipynb 的 TransformerBlock:L308-L335](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/07_gpt_to_llama/standalone-llama32.ipynb#L308-L335) 中 `self.norm1 = nn.RMSNorm(cfg["emb_dim"], eps=1e-5, dtype=cfg["dtype"])`，与手写版数值一致（测试已验证，见下）。

测试侧的正确性兜底——把手写 `RMSNorm` 与 `torch.nn.RMSNorm` 逐位对比：

> 见 [tests/tests_rope_and_parts.py:L377-L383](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/07_gpt_to_llama/tests/tests_rope_and_parts.py#L377-L383) 的 `test_rmsnorm`：`torch.allclose(rms_norm(example_batch), rmsnorm_pytorch(example_batch))`。

#### 4.1.4 代码实践

**目标**：亲眼确认 RMSNorm「只除尺度、不减均值」，并与 LayerNorm 对比。

1. 复制上面 7 行 `RMSNorm` 到一个本地脚本或 REPL。
2. 构造一个有偏的输入 `x = torch.randn(2, 3, 4) + 5`（加 5 让均值明显偏离 0）。
3. 分别过 `RMSNorm` 和第 4 章的 `LayerNorm`（或 `nn.LayerNorm`）。
4. 打印两者输出的**逐 token 均值和方差**。

**预期结果**：

- `LayerNorm` 输出：每个 token 沿特征维均值 ≈ 0、方差 ≈ 1。
- `RMSNorm` 输出：方差 ≈ 1，但**均值不为 0**（仍接近原输入的偏移方向）——这就是「省掉减均值」的直观证据。
- 两者第一行数值不同，但都把尺度拉到了 1 附近。

> 待本地验证：具体均值数值依赖随机种子，但「RMSNorm 均值不为 0、LayerNorm 均值为 0」这一对比应稳定成立。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `torch.rsqrt(means + self.eps)` 改成 `1.0 / torch.sqrt(means + self.eps)`，结果会变吗？
**答**：数值上几乎不变（`rsqrt` 只是 \(1/\sqrt{\cdot}\) 的高效实现），只在极小的浮点误差层面有差异。改动的只是速度，不是语义。

**练习 2**：RMSNorm 为什么可以去掉 `shift`（偏移项）？
**答**：因为输入在进入 RMSNorm 前，通常已经经过上一层残差与归一化的混合，分布大致零均值；去掉 `shift` 能省一个参数向量，且经验上不影响大模型效果。LayerNorm 的 `shift` 主要补偿「减均值」之后的平移，RMSNorm 本来就不减均值，自然不需要。

**练习 3**：`means` 为什么用 `x.pow(2).mean(...)` 而不是 `x.var(...)`？
**答**：`var` 会先减去均值再求平方平均（即方差），而 RMS 的定义是「平方的均值再开方」，**不减均值**。用 `pow(2).mean()` 正是 RMS 的定义；用 `var` 就退化成「除以标准差」、与 LayerNorm 的标准化部分雷同了。

---

### 4.2 RoPE：旋转位置编码（替换可学习位置嵌入）

#### 4.2.1 概念说明

[u2-l4](u2-l4-token-positional-embeddings.md) 里，GPT 用 `nn.Embedding(context_length, emb_dim)` 给每个绝对位置学一个向量，加到 token 嵌入上。它有两个软肋：

1. **不可外推**：词表行数 `context_length` 写死，训练时是 1024 就只能处理 1024，超出就报错或失效。
2. **只编码绝对位置**：注意力分数 \(q_m\cdot k_n\) 依赖的是 \(m\) 和 \(n\) 各自的绝对嵌入，而不是它们的「相对距离」\(m-n\)；而语言里真正重要的是相对位置（"猫 追 老鼠" 和 "昨天 猫 追 老鼠" 里 "追"和"老鼠"的关系不变）。

**RoPE（Rotary Position Embedding，旋转位置编码）** 同时解决这两个问题，核心思想一句话：**不把位置加到输入上，而是在注意力内部把每个位置的 Q/K 向量旋转一个角度，且位置 \(m\) 的旋转角度是 \(m\cdot\theta\)**。

为什么旋转能编码「相对位置」？关键性质：对同一个二维子空间，把 \(q_m\) 旋转 \(m\theta\)、把 \(k_n\) 旋转 \(n\theta\)，那么它们的点积只依赖旋转角之差：

\[
\langle R(m\theta)\,q_m,\; R(n\theta)\,k_n\rangle = \langle q_m,\; R((n-m)\theta)\,k_n\rangle
\]

因为旋转矩阵满足 \(R(a)^\top R(b)=R(b-a)\)。于是注意力分数自动只依赖相对位置 \(n-m\)，且**不需要任何可学习参数**——\(\theta\) 是固定频率，位置 \(m\) 是下标，RoPE 完全由数学公式决定。

> RoPE 原论文：[RoFormer (2021)](https://arxiv.org/abs/2104.09864)。它还有个好处：因为是对 Q/K 做**乘法性**的旋转而非加法性的偏置，值（V）不受影响，归一化也更稳定。

#### 4.2.2 核心流程

RoPE 把 `head_dim`（偶数）切成两半，把每一对维度 \((x_1, x_2)\) 当成一个二维向量做旋转。设有 \(d/2\) 个「频率对」，第 \(i\) 对的频率为：

\[
\theta_i = \text{base}^{-2i/d},\qquad i=0,1,\dots,d/2-1
\]

其中 `base`（Llama 2 是 10 000，Llama 3 是 500 000）控制旋转快慢。**低维对频率高、转得快（捕捉精细位置），高维对频率低、转得慢（捕捉粗略位置）**——这是一种多尺度编码。

对位置 \(m\) 的某个二维对 \(x=(x_1, x_2)\)，旋转角度 \(\phi=m\theta_i\)：

\[
R(\phi)\begin{pmatrix}x_1\\x_2\end{pmatrix}=
\begin{pmatrix}\cos\phi & -\sin\phi\\ \sin\phi & \cos\phi\end{pmatrix}
\begin{pmatrix}x_1\\x_2\end{pmatrix}=
\begin{pmatrix}x_1\cos\phi - x_2\sin\phi\\ x_1\sin\phi + x_2\cos\phi\end{pmatrix}
\]

实现分两步（对应两个函数）：

```
# 第一步：预计算 cos/sin 表（只依赖 head_dim、base、context_length，与输入无关）
inv_freq = 1 / base^(2i/d)                      # (head_dim/2,)  即 θ_i
angles = positions[:,None] * inv_freq[None,:]   # (L, head_dim/2)，angle[m,i] = m·θ_i
angles = cat([angles, angles], dim=1)           # (L, head_dim)  复制一份拼成 d 维
cos, sin = cos(angles), sin(angles)             # 两张表

# 第二步：对 Q/K 应用旋转（split-halves 写法）
x1, x2 = x[..., :d/2], x[..., d/2:]             # 把 head_dim 拆成前后两半
rotated = cat([-x2, x1], dim=-1)                # 构造旋转后的"配对项"
x_rot = x*cos + rotated*sin                     # 一次乘加同时算出两半的旋转
```

最后一步的巧妙之处：`rotated = cat(-x2, x1)` 让一次逐元素乘加 `x*cos + rotated*sin` 同时表达「前半 = \(x_1\cos - x_2\sin\)」和「后半 = \(x_2\cos + x_1\sin\)」，正好是上面旋转矩阵的两个分量。这种「**split-halves（对半切）**」写法和原论文/Meta 官方仓库的「interleaved（奇偶交错）」写法**数学等价**，只要 cos/sin 的排列方式与之配套即可（仓库里专门有 [PR #747](https://github.com/rasbt/LLMs-from-scratch/pull/747) 讨论这点）。

#### 4.2.3 源码精读

**预计算参数**（教学版，无频率缩放）：

```python
def precompute_rope_params(head_dim, theta_base=10_000, context_length=4096):
    assert head_dim % 2 == 0
    inv_freq = 1.0 / (theta_base ** (torch.arange(0, head_dim, 2)[: (head_dim // 2)].float() / head_dim))
    positions = torch.arange(context_length)
    angles = positions.unsqueeze(1) * inv_freq.unsqueeze(0)   # (L, head_dim/2)
    angles = torch.cat([angles, angles], dim=1)               # (L, head_dim)
    cos = torch.cos(angles)
    sin = torch.sin(angles)
    return cos, sin
```

> 见 [converting-gpt-to-llama2.ipynb:L428-L447](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/07_gpt_to_llama/converting-gpt-to-llama2.ipynb#L428-L447)：`arange(0, head_dim, 2)` 取偶数下标 \([0,2,4,\dots]\)，除以 `head_dim` 后给出指数 \(-2i/d\)，于是 `inv_freq[i] = base^{-2i/d} = θ_i`。

**应用旋转** `compute_rope` / `apply_rope`（两者同构）：

```python
def compute_rope(x, cos, sin):
    # x: (batch, num_heads, seq_len, head_dim)
    batch_size, num_heads, seq_len, head_dim = x.shape
    x1 = x[..., : head_dim // 2]                       # 前半
    x2 = x[..., head_dim // 2 :]                       # 后半
    cos = cos[:seq_len, :].unsqueeze(0).unsqueeze(0)   # (1,1,seq_len,head_dim) 广播
    sin = sin[:seq_len, :].unsqueeze(0).unsqueeze(0)
    rotated = torch.cat((-x2, x1), dim=-1)             # 配对项
    x_rotated = (x * cos) + (rotated * sin)            # 一次算完旋转
    return x_rotated.to(dtype=x.dtype)
```

> 教学版见 [converting-gpt-to-llama2.ipynb:L449-L466](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/07_gpt_to_llama/converting-gpt-to-llama2.ipynb#L449-L466)；成品版的 [apply_rope](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/07_gpt_to_llama/standalone-llama32.ipynb#L195-L213) 完全一致。

**在哪里调用**：注意 GPT 是把位置嵌入加在**输入**上，Llama 是把 RoPE 旋转加在**注意力内部的 Q/K** 上（V 不动）：

> 见 [standalone-llama32.ipynb:L265-L266](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/07_gpt_to_llama/standalone-llama32.ipynb#L265-L266)：在 `GroupedQueryAttention.forward` 切好头之后，对 `keys` 和 `queries` 各调一次 `apply_rope`。

**正确性测试**（很重要）：仓库把自写 RoPE 与 HuggingFace 的 `LlamaRotaryEmbedding`、LitGPT 的实现逐位对比，三者必须完全相等：

> 见 [tests/tests_rope_and_parts.py:L143-L204](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/07_gpt_to_llama/tests/tests_rope_and_parts.py#L143-L204) 的 `test_rope_llama2`：`torch.testing.assert_close(queries_rot, ref_queries_rot)`，确保我们的手写旋转和工业实现字节级一致。

#### 4.2.4 代码实践

**目标**：手算一个最小例子，确认 RoPE 的旋转矩阵写法与 4.2.2 的公式吻合。

1. 设 `head_dim=4`、`base=10000`、`context_length=3`，调用 `precompute_rope_params` 得到 `cos, sin`。
2. 构造一个位置 1 的查询向量 `q = torch.tensor([[1., 2., 3., 4.]])`（形状补成 `(1,1,1,4)`）。
3. 用 `compute_rope(q, cos, sin)` 得到 `q_rot`。
4. 手动验证：第 0 对 \((x_1,x_2)=(1,3)\)（前半第 0 维 + 后半第 0 维），旋转角 \(\phi=1\cdot\theta_0\)，应有 `q_rot[0]=1·cosθ₀ − 3·sinθ₀`、`q_rot[2]=1·sinθ₀ + 3·cosθ₀`。

**预期结果**：手算的两个分量与 `compute_rope` 输出的第 0、2 维**完全相等**，证明「split-halves + cat(-x2,x1)」确实实现了标准 2D 旋转。

> 待本地验证：具体数值由 `θ_0 = 10000^(−2·0/4) = 1` 决定，故位置 1 的第 0 对旋转角为 1 弧度，可用 `math.cos(1)/math.sin(1)` 核对。

#### 4.2.5 小练习与答案

**练习 1**：RoPE 为什么只旋转 Q 和 K，不旋转 V？
**答**：因为注意力的输出是 \(\text{softmax}(QK^\top/\sqrt d)\cdot V\)，位置信息只需要进入「谁注意谁」的打分 \(QK^\top\) 里；旋转 Q/K 就足以让打分依赖相对位置。V 是「被取值的内容」，与位置无关，旋转它只会徒增失真。

**练习 2**：把 `head_dim` 从 64 翻倍到 128，RoPE 的 `inv_freq` 向量长度怎么变？最高/最低频率怎么变？
**答**：`inv_freq` 长度从 32 变 64（始终是 `head_dim/2`）。最高频率 \(\theta_0=\text{base}^0=1\) 不变；最低频率 \(\theta_{d/2-1}=\text{base}^{-(d-2)/d}\) 变得更小（更慢的旋转），意味着更大 head_dim 能编码更长的相对距离。

**练习 3**：如果训练时 `context_length=1024`，推理时直接喂 2048 个 token，RoPE 会怎样？
**答**：RoPE 的 `cos/sin` 表虽按 `context_length` 预计算，但公式本身对任意 \(m\) 都成立——只要把 `positions = torch.arange(2048)` 重新预计算，就能直接外推（这正是它相对可学习位置嵌入的优势）。可学习位置嵌入则会因为没学过 1024 之后的位置而失效。（外推质量可进一步用 4.3 的频率缩放改善。）

---

### 4.3 频率缩放：Llama 3 如何把上下文从 8k 扩到 128k

#### 4.3.1 概念说明

上一节说 RoPE「可外推」，但朴素外推有个毛病：训练时只见过的位置范围（比如 8192），直接拉到 128k 时，那些**低频维度**（转得慢、对应长距离）的旋转角会被放大十几倍，导致注意力分布崩坏、输出乱码。

Llama 3.1/3.2 的解法是 **RoPE 频率缩放（frequency scaling）**：对不同的频率维度**区别对待**——

- **高频维度**（转得快、捕捉局部位置）：保持不变。局部信息本来就不依赖绝对长度。
- **低频维度**（转得慢、捕捉长距离）：**拉低频率**（除以一个 `factor`，如 32），相当于「放慢旋转」，让同样的角度增量覆盖更长的距离，从而把有效范围放大 `factor` 倍（8k × 32 ≈ 256k 量级）。
- **中频维度**：在两者之间做**平滑插值**，避免高低频交界处出现断裂。

这套规则由四个超参控制（见 `LLAMA32_CONFIG["rope_freq"]`）：`factor=32`、`low_freq_factor=1.0`、`high_freq_factor=4.0`、`original_context_length=8192`。同时把 `theta_base` 从 Llama 2 的 10 000 调到 **500 000**，整体压低旋转速度以适配超长上下文。

> 灵感来自 HuggingFace `transformers` 的 [`_compute_llama3_parameters`](https://github.com/huggingface/transformers/blob/main/src/transformers/modeling_rope_utils.py)，notebook 顶部已注明。

#### 4.3.2 核心流程

频率缩放发生在「算 `inv_freq` 之后、拼 `angles` 之前」。它根据每个频率的**波长** \(\lambda=2\pi/\text{inv\_freq}\) 决定缩不缩放：

```
inv_freq = 1 / base^(2i/d)                                  # 基础频率
if freq_config is not None:
    low_wavelen  = original_context_length / low_freq_factor   # 波长阈值
    high_wavelen = original_context_length / high_freq_factor
    wavelen = 2π / inv_freq
    # 1) 长波长(低频) → 除以 factor 拉低频率
    inv_freq_llama = where(wavelen > low_wavelen, inv_freq/factor, inv_freq)
    # 2) 中频带 → 在「缩放后」与「原值」之间平滑插值
    smooth = clamp((orig_len/wavelen - low_freq_factor)/(high_freq_factor - low_freq_factor), 0, 1)
    smoothed = (1-smooth)*(inv_freq/factor) + smooth*inv_freq
    is_medium = (wavelen <= low_wavelen) & (wavelen >= high_wavelen)
    inv_freq_llama = where(is_medium, smoothed, inv_freq_llama)
    inv_freq = inv_freq_llama
# 之后照常：angles = positions * inv_freq，cat，cos/sin
```

直觉：波长越长（频率越低）的维度，越需要「踩刹车」（频率除以 factor）来覆盖更远的未来；波长在两个阈值之间的维度用 `smooth` 做线性过渡，保证连续。当 `freq_config=None` 时整段被跳过，退化为 4.2 的标准 RoPE（即 Llama 2）。

#### 4.3.3 源码精读

成品版把频率缩放与预计算合进一个函数 `compute_rope_params`（多了 `freq_config` 形参）：

```python
def compute_rope_params(head_dim, theta_base=10_000, context_length=4096, freq_config=None, dtype=torch.float32):
    inv_freq = 1.0 / (theta_base ** (torch.arange(0, head_dim, 2, dtype=dtype)[: (head_dim // 2)].float() / head_dim))
    if freq_config is not None:
        low_freq_wavelen = freq_config["original_context_length"] / freq_config["low_freq_factor"]
        high_freq_wavelen = freq_config["original_context_length"] / freq_config["high_freq_factor"]
        wavelen = 2 * torch.pi / inv_freq
        inv_freq_llama = torch.where(wavelen > low_freq_wavelen, inv_freq / freq_config["factor"], inv_freq)
        smooth_factor = (freq_config["original_context_length"] / wavelen - freq_config["low_freq_factor"]) / (
            freq_config["high_freq_factor"] - freq_config["low_freq_factor"])
        smoothed_inv_freq = (1 - smooth_factor) * (inv_freq / freq_config["factor"]) + smooth_factor * inv_freq
        is_medium_freq = (wavelen <= low_freq_wavelen) & (wavelen >= high_freq_wavelen)
        inv_freq_llama = torch.where(is_medium_freq, smoothed_inv_freq, inv_freq_llama)
        inv_freq = inv_freq_llama
    positions = torch.arange(context_length, dtype=dtype)
    angles = positions.unsqueeze(1) * inv_freq.unsqueeze(0)
    angles = torch.cat([angles, angles], dim=1)
    return torch.cos(angles), torch.sin(angles)
```

> 见 [standalone-llama32.ipynb:L150-L192](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/07_gpt_to_llama/standalone-llama32.ipynb#L150-L192)：三次 `torch.where` 分段（低频缩放 / 中频平滑 / 高频不变）是整段的核心。

对应的配置（Llama 3.2 1B）：

```python
"rope_base": 500_000.0,          # base θ，远大于 Llama 2 的 10_000
"rope_freq": {                   # 频率缩放四元组
    "factor": 32.0,
    "low_freq_factor": 1.0,
    "high_freq_factor": 4.0,
    "original_context_length": 8192,
}
```

> 见 [standalone-llama32.ipynb 的 LLAMA32_CONFIG:L420-L436](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/07_gpt_to_llama/standalone-llama32.ipynb#L420-L436)：`context_length=131_072` 正是靠这套缩放从训练时的 8192 扩展而来。

正确性兜底：测试 `test_rope_llama3_12` 把带 `freq_config` 的实现与 HuggingFace 的 `rope_type="llama3"` 逐位对比：

> 见 [tests/tests_rope_and_parts.py:L277-L368](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/07_gpt_to_llama/tests/tests_rope_and_parts.py#L277-L368)。

#### 4.3.4 代码实践

**目标**：直观看到频率缩放「踩了低频的刹车」。

1. 稍作改写 `compute_rope_params`，在返回前把中间量 `inv_freq`（缩放前后）打印出来；或复制一份函数体，分别保留 `inv_freq`。
2. 调 `compute_rope_params(head_dim=64, theta_base=500_000, context_length=131072)` 两次：一次传 `freq_config=None`（标准 RoPE），一次传上面的 `rope_freq` 四元组。
3. 比较两次的 `inv_freq`，统计有多少个维度被改小了。

**预期结果**：传了 `freq_config` 的版本里，**长波长（低频）的那几个维度**的 `inv_freq` 明显变小（约为原值的 \(1/32\)），高频维度几乎不变，中频维度介于两者之间。这正对应「低频踩刹车、高频保持」的设计。

> 待本地验证：被改动的维度数量取决于阈值与 `head_dim`，但「低频变小、高频不变」的趋势稳定可见。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Llama 3 要把 `theta_base` 从 10 000 调大到 500 000？
**答**：`base` 越大，\(\theta_i=\text{base}^{-2i/d}\) 整体越小，旋转越慢，单个维度能覆盖的相对距离越长。调大 base 是为超长上下文「松刹车」的第一步，频率缩放则在此基础上对低频维度进一步精细化调整。

**练习 2**：`freq_config=None` 时，`compute_rope_params` 的行为退化成什么？
**答**：退化成 4.2 节的标准 RoPE（即 Llama 2 的预计算）：跳过整段 `if freq_config is not None` 分支，直接用原始 `inv_freq`。所以同一个函数同时服务 Llama 2（不传 freq_config）和 Llama 3（传 freq_config）。

**练习 3**：`smooth_factor` 为什么要 `clamp` 到 `[0, 1]`？
**答**：`smooth_factor` 是中频带的线性插值权重，超出 `[0,1]` 就不再是「凸组合」，会把 `smoothed_inv_freq` 外推到两个端点之外、破坏连续性。`clamp` 保证它严格在「完全缩放」与「完全不缩放」之间过渡。

---

### 4.4 Llama3Model：整体组装与配置

#### 4.4.1 概念说明

把 4.1 的 RMSNorm、4.2/4.3 的 RoPE，连同 SwiGLU 前馈、GQA 注意力缝在一起，就是 `Llama3Model`。它的数据流和 [u4-l3 的 GPTModel](u4-l3-gpt-model-assembly.md) 几乎同构，区别全在我们前面讲过的「零件替换」上：

- **去掉 `pos_emb`**：RoPE 在注意力内部编码位置，输入端只剩 `tok_emb`。
- **`trf_blocks` 用 `nn.ModuleList` 而非 `nn.Sequential`**：因为每个 block 的 `forward` 现在要多收 `mask, cos, sin` 三个参数，`Sequential` 只能传单输入，故改用 `ModuleList` 手动循环。
- **归一化全用 `nn.RMSNorm`**，残差结构（pre-norm + shortcut）不变。
- **`cos/sin` 在模型级预算一次**，作为非持久化 buffer 注册，所有层共享（而非每层各算一份）。
- **因果掩码在 `forward` 里现算**：`torch.triu(...ones..., diagonal=1).bool()`，按实际 `num_tokens` 切，不再用 `register_buffer` 存固定大矩阵。

#### 4.4.2 核心流程

```
forward(in_idx):                              # in_idx: (b, T) 整数 token ID
  x = tok_emb(in_idx)                         # (b, T, emb_dim)，注意：没有 + pos_emb
  mask = triu(ones(T,T), diagonal=1).bool()   # 因果掩码，按当前序列长现算
  for block in trf_blocks:                    # ModuleList 循环
      x = block(x, mask, self.cos, self.sin)  # 每层共享同一份 cos/sin
  x = final_norm(x)                           # RMSNorm
  logits = out_head(x.to(cfg["dtype"]))       # Linear → (b, T, vocab_size)
  return logits
```

每个 `TransformerBlock` 内部（与 u4-l2 同构，只换了零件）：

```
# 注意力子层（pre-norm + 残差）
shortcut = x; x = norm1(x); x = att(x, mask, cos, sin); x = x + shortcut
# 前馈子层（pre-norm + 残差）
shortcut = x; x = norm2(x); x = ff(x);        x = x + shortcut
```

其中注意力 `att` 是 `GroupedQueryAttention`：它对 Q/K 先做 `view/transpose` 切头、**调 `apply_rope` 注入位置**、再用 `repeat_interleave` 把 K/V 组复制到与 query 头对齐、最后做带因果掩码的缩放点积。GQA 的完整原理见 [u9-l3](u9-l3-attention-variants.md)，这里只关注「RoPE 嵌在哪一步」。

前馈 `ff` 是 SwiGLU：`fc3(silu(fc1(x)) * fc2(x))`，三个无偏置线性层，中间维 `hidden_dim`（Llama 3.2 1B 为 8192）。

#### 4.4.3 源码精读

**TransformerBlock**（成品版，注意 `nn.RMSNorm` 与传参）：

```python
class TransformerBlock(nn.Module):
    def __init__(self, cfg):
        super().__init__()
        self.att = GroupedQueryAttention(
            d_in=cfg["emb_dim"], d_out=cfg["emb_dim"],
            num_heads=cfg["n_heads"], num_kv_groups=cfg["n_kv_groups"], dtype=cfg["dtype"])
        self.ff = FeedForward(cfg)
        self.norm1 = nn.RMSNorm(cfg["emb_dim"], eps=1e-5, dtype=cfg["dtype"])
        self.norm2 = nn.RMSNorm(cfg["emb_dim"], eps=1e-5, dtype=cfg["dtype"])
    def forward(self, x, mask, cos, sin):
        shortcut = x; x = self.norm1(x); x = self.att(x, mask, cos, sin); x = x + shortcut
        shortcut = x; x = self.norm2(x); x = self.ff(x); x = x + shortcut
        return x
```

> 见 [standalone-llama32.ipynb:L308-L335](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/07_gpt_to_llama/standalone-llama32.ipynb#L308-L335)：与第 4 章 `TransformerBlock` 相比，去掉了 `drop_shortcut`、把 LayerNorm 换 RMSNorm、`forward` 多了 `mask, cos, sin`。

**Llama3Model**：

```python
class Llama3Model(nn.Module):
    def __init__(self, cfg):
        super().__init__()
        self.tok_emb = nn.Embedding(cfg["vocab_size"], cfg["emb_dim"], dtype=cfg["dtype"])
        self.trf_blocks = nn.ModuleList(           # ModuleList：要传 (x, mask, cos, sin)
            [TransformerBlock(cfg) for _ in range(cfg["n_layers"])])
        self.final_norm = nn.RMSNorm(cfg["emb_dim"], eps=1e-5, dtype=cfg["dtype"])
        self.out_head = nn.Linear(cfg["emb_dim"], cfg["vocab_size"], bias=False, dtype=cfg["dtype"])
        cos, sin = compute_rope_params(            # 模型级预算一次 RoPE 表
            head_dim=cfg["emb_dim"] // cfg["n_heads"], theta_base=cfg["rope_base"],
            context_length=cfg["context_length"], freq_config=cfg["rope_freq"])
        self.register_buffer("cos", cos, persistent=False)   # 非持久化：不进 state_dict
        self.register_buffer("sin", sin, persistent=False)
        self.cfg = cfg
    def forward(self, in_idx):
        tok_embeds = self.tok_emb(in_idx)
        x = tok_embeds                             # 没有 + pos_emb
        num_tokens = x.shape[1]
        mask = torch.triu(torch.ones(num_tokens, num_tokens, device=x.device, dtype=torch.bool), diagonal=1)
        for block in self.trf_blocks:
            x = block(x, mask, self.cos, self.sin)
        x = self.final_norm(x)
        logits = self.out_head(x.to(self.cfg["dtype"]))
        return logits
```

> 见 [standalone-llama32.ipynb:L347-L385](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/07_gpt_to_llama/standalone-llama32.ipynb#L347-L385)：注意 `register_buffer(..., persistent=False)`——`cos/sin` 是纯数学公式算出的常量，**不需要保存到权重文件**，加载时由 `compute_rope_params` 重算即可，故设为非持久化（这点和 u9-l1 KV cache 里的 `cache_k/cache_v` 同理）。

**配置卡**（Llama 3.2 1B，对照 [u4-l3 的 GPT_CONFIG_124M](u4-l3-gpt-model-assembly.md)）：

| 超参 | GPT-2 124M | Llama 3.2 1B | 含义 |
|------|------------|--------------|------|
| `vocab_size` | 50 257 | **128 256** | Llama 3 用更大的 tiktoken 词表 |
| `context_length` | 1024 | **131 072** | 靠频率缩放扩展 |
| `emb_dim` | 768 | **2048** | |
| `n_heads` | 12 | **32** | |
| `n_layers` | 12 | **16** | |
| `n_kv_groups` | — | **8** | GQA：32 个 query 头共享 8 组 K/V |
| `hidden_dim` | 4×emb | **8192** | SwiGLU 中间维 |
| `rope_base` | — | **500 000** | RoPE 频率底 |
| `dtype` | float32 | **bfloat16** | 省显存 |

> 完整配置见 [standalone-llama32.ipynb:L420-L436](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/07_gpt_to_llama/standalone-llama32.ipynb#L420-L436)。notebook 实测：1B 模型 bfloat16 下约 **5.61 GB**，加载预训练权重后用 `generate` 能流畅回答 "What do llamas eat?"。

**复用的解码器**：生成完全复用第 5 章的 `generate`（温度 + top-k + 自回归），无需改动：

> 见 [previous_chapters.py:L27-L67](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/07_gpt_to_llama/previous_chapters.py#L27-L67) 的 `generate`，以及同文件 [L16-L24](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/07_gpt_to_llama/previous_chapters.py#L16-L24) 的 `text_to_token_ids` / `token_ids_to_text`。

**权重加载（读代码练习）**：`load_weights_into_llama` 是 [u5-l4 `load_weights_into_gpt`](u5-l4-weight-loading.md) 的 Llama 版「翻译官」，处理命名差异（HF 的 `model.layers.{l}.self_attn.q_proj.weight` ↔ 我们的 `trf_blocks[l].att.W_query.weight`）、把 SwiGLU 三个矩阵（`gate_proj/up_proj/down_proj`）映射到 `fc1/fc2/fc3`，并在权重文件缺 `lm_head.weight` 时自动启用 **weight tying**（`out_head.weight = tok_emb.weight`）。Llama 3 的 safetensors 没存 `lm_head`，故 notebook 打印 `Model uses weight tying.`。

> 见 [standalone-llama32.ipynb 的 assign + load_weights_into_llama:L782-L865](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/07_gpt_to_llama/standalone-llama32.ipynb#L782-L865)。

> 补充：Llama **2** 的权重映射多一步 Q/K 的 `permute`（把 Meta 的 sliced 布局重排成我们这套 split-halves/interleaved 布局），见 [converting-gpt-to-llama2.ipynb 的 permute 与 load_weights_into_llama](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/07_gpt_to_llama/converting-gpt-to-llama2.ipynb)（细节可结合 PR #747 讨论）；而 Llama 3 的 safetensors 已经是 HF 布局，无需 `permute`，直接按名拷贝即可。

#### 4.4.4 代码实践

**目标**：验证组装正确性——参数量量级、weight tying、前向能跑通。

1. 按 [setup 文档](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/setup/README.md) 装好 PyTorch（≥ 2.4，含 `nn.RMSNorm`）。
2. 复制 `LLAMA32_CONFIG`（1B 版）与 `Llama3Model`，实例化 `model = Llama3Model(LLAMA32_CONFIG)`。
3. 打印 `sum(p.numel() for p in model.parameters())`，并减去 `model.tok_emb.weight.numel()` 得到「去 weight tying 后」的独立参数量。
4. 喂一段随机 token ID `torch.randint(0, 128256, (1, 8))`，确认 `logits.shape == (1, 8, 128256)`。

**预期结果**（与 notebook 一致）：

- 总参数量 `1,498,482,688`，扣除 `tok_emb` 后独立参数 `1,235,814,400`（约 1.2B，即「1B」口径）。
- 前向输出形状 `(1, 8, 128256)`，无报错。

> 待本地验证：未加载预训练权重时，生成的 token 是乱码（与 [u4-l4](u4-l4-simple-text-generation.md) 同理），这正常——本步只验证架构与参数量。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `trf_blocks` 用 `nn.ModuleList` 而不是 `nn.Sequential`？
**答**：`Sequential` 的每个子模块只能接收**单个**输入（上一个的输出），而这里的 `TransformerBlock.forward(x, mask, cos, sin)` 需要四个参数。改用 `ModuleList` 在 `for` 循环里手动传参即可解决。`ModuleList` 同样会正确注册子模块参数，不影响 `parameters()` / `state_dict()`。

**练习 2**：`cos/sin` 为什么用 `persistent=False` 注册，且只在模型级算一份？
**答**：它们是 `compute_rope_params` 算出的纯常量，不含可学习参数，保存到权重文件是浪费——`persistent=False` 使其不进 `state_dict`，加载权重时由公式重算。只在模型级算一份（而非每个 attention 层各算一份）是因为所有层共享同一套位置编码，省内存、也保证一致性。

**练习 3**：GPT 的 `forward` 里 `x = tok_emb + pos_emb`，Llama 里只有 `x = tok_emb`，位置信息去哪了？
**答**：搬到注意力内部了。每个 `TransformerBlock` 的 `GroupedQueryAttention.forward` 在切头后对 Q/K 调 `apply_rope(..., self.cos, self.sin)`，用旋转编码注入位置。所以输入端不需要位置嵌入，`forward` 里自然只剩 `tok_emb`。

---

## 5. 综合实践

**任务**：亲手实现 RoPE 并验证它的「相对位置」性质，再与 GPT 的可学习位置嵌入做对比，体会两种位置编码的本质差异。

### 步骤 1：实现并验证相对位置性质

把 4.2 的 `precompute_rope_params` 与 `compute_rope` 抄到本地（或直接用 `standalone-llama32.ipynb` 的 `compute_rope_params`/`apply_rope`）。设 `head_dim=16, base=10000, context_length=64`，预计算 `cos, sin`。

构造一个固定的「查询向量」`q` 和「键向量」`k`（形状 `(1, 1, 16)`）。现在做两组实验：

- **实验 A（绝对位置 m, n）**：把 `q` 放在位置 2、`k` 放在位置 5，算 `<RoPE(q,m=2), RoPE(k,n=5)>`。
- **实验 B（整体平移）**：把 `q` 放在位置 12、`k` 放在位置 15（两者都 +10），算 `<RoPE(q,m=12), RoPE(k,n=15)>`。

**预期结果**：两个点积**完全相等**（或差异在 \(10^{-6}\) 量级的浮点误差内）。因为两者相对距离都是 \(5-2=3\)，RoPE 保证点积只依赖相对位置。你可以再试一组 `(m=0, n=3)`，它也应与前两组相等——这就是「相对位置旋转性质」。

> 提示：`compute_rope` 需要 4D 输入 `(batch, heads, seq, head_dim)`。对单个向量可以构造 `(1,1,context_length,16)`（同一向量在所有位置）、在位置 m/n 处取结果；或把 `cos/sin` 切到对应位置 `cos[m:m+1]` 后与 `(1,1,1,16)` 配合。

### 步骤 2：与可学习位置嵌入对比

回到第 4 章的 GPT 思路：`pos_emb = nn.Embedding(context_length, emb_dim)`，把位置 m 的嵌入**加**到 token 嵌入上。重复上面的「平移」实验：用同一个 q、k，分别在「加了位置 m 的嵌入」和「加了位置 m+10 的嵌入」下算点积。

**预期结果**：两组点积**不相等**（除非巧合）。因为可学习位置嵌入编码的是绝对位置，平移后 q、k 各自的嵌入都变了，点积自然改变——这正是 GPT 位置编码「不感知相对位置」的体现。

### 步骤 3（选做）：跑通端到端生成

若你有足够显存（1B 模型 bfloat16 约 5.6 GB）并接受了 Llama 3.2 许可，按 [README 的「Using Llama 3.2」](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/07_gpt_to_llama/README.md) 流程：下载 `llama3.2-1B-instruct.pth`、加载权重、用 `generate` 提问 "What do llamas eat?"。

**预期结果**：得到一段连贯的羊驼食性介绍（README 示例约 50 tokens/sec、2.91 GB 显存）。若没有 GPU，可用 CPU 慢跑或仅完成步骤 1、2 的「源码阅读 + 小规模验证」。

> 本综合实践的核心交付是**步骤 1 的相对位置验证**与**步骤 2 的对比结论**，不依赖能否下载到 Llama 权重。

---

## 6. 本讲小结

- **Llama 不是新架构**，而是对 GPT 的若干精准替换：LayerNorm→RMSNorm、可学习位置嵌入→RoPE、GELU+两层 FFN→SwiGLU+三层 FFN、去偏置、去 dropout、（Llama 3）MHA→GQA、float32→bfloat16。
- **RMSNorm** 只除以均方根（`x · rsqrt(mean(x²)+ε)`）再做一次可学习缩放，**不减均值、没有 shift**，比 LayerNorm 省一次统计、效果相当；可用 `nn.RMSNorm` 直接替换。
- **RoPE** 不在输入上加位置，而是在注意力内部把每个位置的 Q/K **旋转**角度 \(m\theta\)；因 \(R(m\theta)^\top R(n\theta)=R((n-m)\theta)\)，点积自动只依赖相对位置，且**无需可学习参数、可外推**。
- **频率缩放**是 Llama 3 把上下文从 8k 扩到 128k 的关键：按波长对低频维度「除以 factor 踩刹车」、高频保持、中频平滑插值，配合 `theta_base=500_000`，使超长上下文下注意力不崩。
- **`Llama3Model`** 与 GPTModel 同构，差异是：去 `pos_emb`、`trf_blocks` 改用 `ModuleList`（因 `forward` 要传 `mask, cos, sin`）、RoPE 表在模型级以非持久化 buffer 算一份共享、归一化与残差骨架不变。
- **正确性有测试兜底**：仓库把自写 RoPE/RMSNorm/SiLU 与 HuggingFace、LitGPT、`torch.nn.RMSNorm` 逐位对比（`tests_rope_and_parts.py`），可放心学习。

---

## 7. 下一步学习建议

- **GQA 的完整原理**：本讲把 `GroupedQueryAttention` 当现成模块用，它的「共享 K/V 头省 KV cache 显存」机制在 [u9-l3 注意力变体](u9-l3-attention-variants.md) 有完整推导与内存公式，强烈建议接着读。
- **KV Cache + Llama**：`pkg/llms_from_scratch/kv_cache/llama3.py` 提供了带 KV cache 的 Llama3Model（README「Pro tip 3」），把 [u9-l1](u9-l1-kv-cache.md) 的缓存技巧与本讲的 Llama 架构结合，CPU 上能从 1 token/s 提速到 68 tokens/s。
- **现代架构概览**：[u10-l2](u10-l2-modern-llm-architectures.md) 会对比 Qwen3、Gemma3 与 Llama 的差异（Gemma3 = GQA + SWA），把本讲的架构迁移视角推广到更多现代模型。
- **权重加载细节**：若你想彻底搞懂 Llama **2** 权重里 Q/K 的 `permute` 重排（sliced↔interleaved 布局），精读 [converting-gpt-to-llama2.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/07_gpt_to_llama/converting-gpt-to-llama2.ipynb) 的 `load_weights_into_llama` 与 [PR #747](https://github.com/rasbt/LLMs-from-scratch/pull/747) 讨论。
- **原论文**：RoPE（[RoFormer 2021](https://arxiv.org/abs/2104.09864)）、RMSNorm（[2019](https://arxiv.org/abs/1910.07467)）、SwiGLU（[2020](https://arxiv.org/abs/2002.05202)）、Llama 2（[2023](https://arxiv.org/abs/2307.09288)）。
