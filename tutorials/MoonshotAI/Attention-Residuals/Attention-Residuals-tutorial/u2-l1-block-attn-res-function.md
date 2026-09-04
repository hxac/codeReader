# 伪代码导读：block_attn_res 函数逐行解析

## 1. 本讲目标

上一讲（u1-l3）我们已经理解了 AttnRes 的核心公式 \( h_l = \sum_i \alpha_{i \to l} \mathbf{v}_i \)，并亲手实现了一个最简版的 `full_attn_res`。本讲我们进入工程实现的核心：**逐行精读 README 中的 `block_attn_res` 函数**——这是 Block AttnRes（论文的实用形态）的最小计算单元。

学完本讲，你应该能够：

1. 说清楚 `block_attn_res` 的输入输出语义：`blocks`（N 份已完成块表示）+ `partial_block`（当前块部分和）如何堆叠成形状为 `[N+1, B, T, D]` 的候选张量 `V`。
2. 理解「归一化 K、保留 V」的键值分离设计：为什么打分用 `norm(V)`、聚合却用原始的 `V`。
3. 独立写出两次 `torch.einsum`：第一次用伪查询算出逐 token 的深度 logits，第二次沿深度轴加权聚合。
4. 用随机输入验证：输出形状为 `[B, T, D]`，且 `softmax(0)` 得到的权重沿深度轴求和恒为 1。

本讲所有「源码」都来自 README 中唯一的 PyTorch 风格伪代码块，我们会为每一行给出永久链接和行号。

## 2. 前置知识

本讲用到的 PyTorch API 不多，但每个都有容易踩坑的细节。先用一两分钟把这几个工具彻底搞清楚。

### 2.1 einsum：用下标字母描述张量运算

`torch.einsum`（Einstein summation，爱因斯坦求和约定）用「下标字母」描述输入和输出张量的维度：**出现在输入但不出现在输出中的下标会被求和（收缩）掉**。

例如矩阵乘法 `C = A @ B` 可以写成：

```python
# 示例代码
C = torch.einsum('i k, k j -> i j', A, B)   # k 在输出中消失 → 对 k 求和
```

本讲会遇到两种典型用法：

| 写法 | 数学含义 | 输出 |
|:---|:---|:---|
| `'d, n b t d -> n b t'` | 向量与每个候选做点积（对 d 求和） | 每个 (n, b, t) 位置一个标量 |
| `'n b t, n b t d -> b t d'` | 权重与候选加权求和（对 n 求和） | 每个 (b, t) 位置一个 d 维向量 |

记忆口诀：**消失的下标被求和，剩下的下标原样保留**。

### 2.2 torch.stack 与 torch.cat 的区别

- `torch.cat(tensors, dim=0)`：沿**已有维度**拼接，总形状是该维长度相加。
- `torch.stack(tensors, dim=0)`：沿**新建维度**拼接，要求所有张量形状完全相同。

例如 3 个 `[2, 5, 8]` 的张量：

```python
# 示例代码
torch.stack(xs).shape    # torch.Size([3, 2, 5, 8])  ← 新增第 0 维
torch.cat(xs, dim=0).shape  # torch.Size([6, 5, 8])  ← 第 0 维相加，语义被破坏
```

`block_attn_res` 需要 `stack`：它要把 N+1 份候选叠成一个**深度维**，而不是把 batch 维翻倍。

### 2.3 softmax 沿指定维度

`logits.softmax(dim)` 把张量沿 `dim` 归一化为非负、和为 1 的分布。`softmax(0)` 就是沿第 0 维（本讲中即深度维）归一化——这是 AttnRes 权重 \(\alpha_{i \to l}\) 非负且和为 1 的直接来源（上一讲称之为「凸组合」，它保证输出幅度被候选的最大幅度封顶）。

### 2.4 Linear 的 weight 形状 与 RMSNorm 回顾

- `nn.Linear(in_features=d, out_features=1, bias=False)` 的 `weight` 形状是 `[1, d]`。`weight.squeeze()` 去掉长度为 1 的维度后得到 `[d]` 向量。本讲中你会看到一个反直觉的用法：**伪代码根本不调用 `proj(x)`，而是直接把 `weight` 当作伪查询向量 \(\mathbf{w}_l\) 参与运算**——Linear 在这里只是 `w_l` 的参数化容器。
- RMSNorm 沿最后一维 D 归一化：对向量 \(\mathbf{x} \in \mathbb{R}^d\)，
  \[ \mathrm{RMSNorm}(\mathbf{x}) = \frac{\mathbf{x}}{\sqrt{\tfrac{1}{d}\sum_k x_k^2 + \epsilon}} \odot \mathbf{g} \]
  其中 \(\mathbf{g}\) 是可学习增益（README 伪代码未给出 RMSNorm 内部实现，是否含增益等细节以论文为准；不影响本讲的形状与归一化讨论）。关键性质：**沿 D 操作，前面的所有维度都当作独立批次**，所以对 `[N+1, B, T, D]` 一次性做 RMSNorm，等价于对 N+1 份候选各自归一化。

### 2.5 维度符号约定

| 符号 | 含义 | 本讲实践取值 |
|:---:|:---|:---:|
| B | batch 大小 | 2 |
| T | 序列长度（token 数） | 5 |
| D | 隐藏维度 | 8 |
| N | 已完成块的数量 | 3 |
| N+1 | 候选总数（N 块 + 1 份部分和） | 4 |

## 3. 本讲源码地图

本仓库是论文发布仓库，没有任何工程代码，**README 中的伪代码块是全仓库唯一可精读的「源码」**。

| 文件 | 位置 | 作用 |
|:---|:---|:---|
| `README.md` | L52–L91 | PyTorch 风格伪代码块：`block_attn_res`（L53–L65）与 `forward`（L67–L90），本讲的主角 |
| `README.md` | L45–L47 | Block AttnRes 的文字说明：块内标准残差、块间注意力、O(Ld)→O(Nd) |
| `README.md` | L67–L90 | `forward` 伪代码：本讲只看其中**两处对 `block_attn_res` 的调用环境**（L71、L84），完整调度逻辑留给下一讲 u2-l2 |
| `assets/overview.png` | — | 概览图 (c) 子图，直观展示 Block AttnRes 的分块结构 |

## 4. 核心概念与源码讲解

先看全函数，建立整体印象，再拆成三个最小模块逐一精读：

> 完整函数见 [README.md:L53-L65](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L53-L65)，这是 README 伪代码块中的第一个函数，承担 Block AttnRes 的全部「块间注意力」计算。

```python
def block_attn_res(blocks: list[Tensor], partial_block: Tensor, proj: Linear, norm: RMSNorm) -> Tensor:
    V = torch.stack(blocks + [partial_block])  # [N+1, B, T, D]
    K = norm(V)
    logits = torch.einsum('d, n b t d -> n b t', proj.weight.squeeze(), K)
    h = torch.einsum('n b t, n b t d -> b t d', logits.softmax(0), V)
    return h
```

短短 5 行（不含注释与 docstring）做了四件事：**拼装候选 → 归一化打分 → 深度 softmax → 加权聚合**。注意与上一讲 `full_attn_res` 的两点不同：候选从「全部 L 层输出」换成了「N+1 份块级表示」；打分对象从原始输出换成了归一化后的 K。

### 4.1 模块一：block_attn_res 函数总览——签名、候选集合与调用环境

#### 4.1.1 概念说明

`block_attn_res` 是 Block AttnRes 的最小计算单元。回忆 [README.md:L45-L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L45-L47) 的定义：**块内用标准残差累加，块间才做注意力**。这个函数只负责「块间」这一半——它回答的问题是：

> 当前层（第 l 层）在进入注意力/MLP 子层之前，应该如何看待此前所有块已经积累下来的信息？

它把「历史」抽象成 N+1 份候选表示，输出一份聚合后的 `h`，交给下游子层使用。函数本身是**组件无关**的：投影 `proj` 和归一化 `norm` 都是外部注入的参数，所以同一个函数可以在注意力前和 MLP 前复用，只是换不同的参数对。

#### 4.1.2 核心流程

```text
输入: blocks = [v_0, v_1, ..., v_{N-1}]   （N 份已完成块表示, 各 [B,T,D]）
      partial_block = b_n^i                （当前块内到第 i 层的部分和, [B,T,D]）
      proj, norm                          （本层专属的伪查询容器与 RMSNorm）

第 1 步: 候选集合 = blocks + [partial_block]     共 N+1 份
第 2 步: V = stack(候选集合)                      [N+1, B, T, D]
第 3 步: K = RMSNorm(V)                          键侧归一化，形状不变
第 4 步: logits[b,t,n] = <w_l, K[n,b,t,:]>       伪查询与每个候选逐 token 点积
第 5 步: α = softmax(logits, dim=深度)            每个 token 位置沿深度归一化
第 6 步: h[b,t,:] = Σ_n α[n,b,t] · V[n,b,t,:]    加权聚合原始候选

输出: h [B, T, D]
```

写成数学形式（候选索引 \( i = 0, 1, \dots, N \)，共 N+1 份）：

\[ \alpha_{i \to l} = \frac{\exp\left(\langle \mathbf{w}_l,\ \mathrm{norm}(\mathbf{v}_i) \rangle\right)}{\sum_{j=0}^{N} \exp\left(\langle \mathbf{w}_l,\ \mathrm{norm}(\mathbf{v}_j) \rangle\right)}, \qquad \mathbf{h}_l = \sum_{i=0}^{N} \alpha_{i \to l}\, \mathbf{v}_i \]

#### 4.1.3 源码精读

**函数签名与 docstring**：

```python
def block_attn_res(blocks: list[Tensor], partial_block: Tensor, proj: Linear, norm: RMSNorm) -> Tensor:
```

见 [README.md:L53-L60](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L53-L60)。签名之后紧跟的 docstring 明确了两个张量参数的语义：

- `blocks`：N 个 `[B, T, D]` 张量，「每个先前已完成块的表示」；
- `partial_block`：`[B, T, D]`，「块内部分和」，README 原文标注了符号 \( b_n^i \)（第 n 块累加到第 i 层的部分和）。

docstring 第一行写明函数职责："Inter-block attention: attend over block reps + partial sum"（块间注意力：关注块表示 + 部分和）。

**调用环境（预告，不展开）**：这个函数在 `forward` 中被调用两次——

```python
h = block_attn_res(blocks, partial_block, self.attn_res_proj, self.attn_res_norm)
...
h = block_attn_res(blocks, partial_block, self.mlp_res_proj, self.mlp_res_norm)
```

分别位于注意力子层之前（[README.md:L71](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L71)）和 MLP 子层之前（[README.md:L84](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L84)），两次使用**各自独立的 proj/norm 参数对**。另外注意 L70 的注释 `# blocks already include token embedding`——blocks 列表的第 0 项就是词嵌入，这呼应了 u1-l2 「词嵌入视作第 0 项」的说法。块边界如何维护、两次调用之间 `partial_block` 如何变化，是下一讲 u2-l2 的主题。

#### 4.1.4 代码实践

**实践目标**：不借助任何框架封装，亲手搭建 `block_attn_res` 的运行环境——构造 N=3 个随机历史块、1 份随机部分和、一个 `Linear(D, 1)` 伪查询容器和一个 RMSNorm 模块，先只跑通「候选集合」这一步，观察维度如何从「N 个独立张量」变成「1 个 4 深度的张量」。

**操作步骤**（以下均为示例代码，可保存为 `practice_u2l1_step1.py` 逐段运行）：

```python
import torch
import torch.nn as nn

torch.manual_seed(0)
B, T, D, N = 2, 5, 8, 3

# N 份已完成块表示 + 1 份当前块部分和（随机数仅用于形状验证）
blocks = [torch.randn(B, T, D) for _ in range(N)]
partial_block = torch.randn(B, T, D)

candidates = blocks + [partial_block]     # Python 列表拼接，长度 N+1 = 4
V = torch.stack(candidates)               # 关键一步：新增深度维
print(len(candidates), V.shape)           # 4 torch.Size([4, 2, 5, 8])
```

**需要观察的现象**：`torch.stack` 之前是 4 个彼此独立的 `[2, 5, 8]` 张量；之后变成一个 `[4, 2, 5, 8]` 张量，第 0 维恰好等于候选数 N+1。

**预期结果**：打印 `4` 和 `torch.Size([4, 2, 5, 8])`。这一步的形状结论由 `torch.stack` 的定义直接保证；具体打印格式待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果 `blocks` 是空列表（模型刚起步、连词嵌入都还没进 blocks），`torch.stack(blocks + [partial_block])` 还能工作吗？输出形状是什么？

**答案**：能。`blocks + [partial_block]` 是长度 1 的列表，`stack` 得到 `[1, B, T, D]`；后续 `softmax(0)` 对唯一候选归一化，权重恒为 1，输出就等于 `partial_block` 本身——函数优雅地退化。不过按 forward 的逻辑（L70 注释），blocks 至少包含 token embedding，实践中不会为空。

**练习 2**：函数返回的 `h` 和输入的 `partial_block` 形状相同，它们是同一个东西吗？

**答案**：不是。`partial_block` 只是 N+1 个候选之一（当前块的部分和）；`h` 是全部 N+1 个候选的 softmax 加权平均，融合了更早所有块的信息。形状相同只是因为聚合不改变逐 token 的维度结构。

**练习 3**：为什么把 `proj`、`norm` 作为函数参数传入，而不是在函数内部创建？

**答案**：每个 transformer 层在注意力前和 MLP 前各有一组独立的 `proj/norm`（L71 与 L84 传入的是不同参数对）。参数注入让同一个纯函数能被任意层、任意子层位置复用，参数的生命周期由层模块管理——这是典型的计算与参数分离的设计。

### 4.2 模块二：torch.stack 拼装 V 与「归一化 K、保留 V」的键值分离

#### 4.2.1 概念说明

本模块覆盖函数的前两行：

```python
V = torch.stack(blocks + [partial_block])  # [N+1, B, T, D]
K = norm(V)
```

见 [README.md:L61](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L61)（stack 拼装 V）与 [README.md:L62](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L62)（键侧归一化）。

这里有三个设计要点：

1. **深度维是新增的第 0 维**。候选们本来就是同形状的 `[B, T, D]`，`stack` 把「第几个候选」这个语义变成一个真实的张量维度，后续才能对这个维度做 softmax 和加权求和。
2. **V 保留原始值**。聚合（第二次 einsum）作用在未经归一化的 `V` 上，各候选的真实幅度信息得以保留。
3. **K 只用于打分**。`K = norm(V)` 是「键」（key）的角色：伪查询与 K 做点积产生 logits。这就是标题所说的「归一化 K、保留 V」的键值分离——形式上类似 PreNorm 的思想（打分前先归一化，聚合输出保持原尺度）。

为什么要这样分离？两个理由：

- **打分需要尺度可比**。u1-l2 讲过，标准残差主干幅度随深度增长，不同块表示的幅度可能相差很大。若直接用原始 V 打分，logits 会候选幅度主导——幅度大的块「嗓门大」，无论内容是否相关。RMSNorm 把每个候选在每个 token 位置归一化到统一尺度后，点积大小才主要反映**方向**上的匹配程度。
- **聚合需要真实幅度**。输出 \(\mathbf{h}_l\) 是候选的凸组合（上一讲结论：权重非负、和为 1），其幅度被最大候选封顶。若把 V 也归一化，就等于丢弃了「哪个块信息量大」的幅度线索，且会改变残差主干向下游传递的尺度。

一句话总结：**K 负责「该关注谁」（尺度归一化、方向匹配），V 负责「取回什么」（原始信息）**。

#### 4.2.2 核心流程

```text
blocks ──┐ (N × [B,T,D])
         ├─ list 拼接 ─→ stack(dim=0) ─→ V [N+1,B,T,D] ──┬─→ 直接作为聚合的「值」
partial_block ─┘ ([B,T,D])                              │
                                                        └─→ K = RMSNorm(V)
                                                              沿最后一维 D 归一化
                                                              形状不变 [N+1,B,T,D]
                                                              仅作为打分的「键」
```

RMSNorm 只沿 D 操作，因此 n、b、t 三个维度都被当作独立批次——对 `[N+1, B, T, D]` 整体做一次 `norm`，与「先拆开、逐候选逐 token 归一化、再拼回去」完全等价，但省去了 Python 循环。

#### 4.2.3 源码精读

```python
V = torch.stack(blocks + [partial_block])  # [N+1, B, T, D]
```

见 [README.md:L61](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L61)：Python 列表相加 `blocks + [partial_block]` 把部分和追加为最后一个候选，`torch.stack` 新建第 0 维，行尾注释明确标注结果形状 `[N+1, B, T, D]`。**注意顺序**：部分和永远排在深度维末尾（索引 N），词嵌入所在的第 0 块排在最前（深度索引 0）。

```python
K = norm(V)
```

见 [README.md:L62](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L62)：一行完成全部候选的键侧归一化。变量命名直接借用了注意力机制的经典记号 K（key）/ V（value），提示这套「伪查询—键—值」的对应关系。

#### 4.2.4 代码实践

**实践目标**：验证「归一化 K、保留 V」的一个直接推论——**打分对候选的幅度尺度不敏感**。把某个块整体放大 100 倍，观察权重几乎不变；同时确认 V 路径确实保留了原始幅度。

**操作步骤**（示例代码）：

```python
import torch
import torch.nn as nn

torch.manual_seed(0)
B, T, D, N = 2, 5, 8, 3

class RMSNorm(nn.Module):   # 示例代码：README 未给出 RMSNorm 内部实现
    def __init__(self, dim, eps=1e-6):
        super().__init__()
        self.weight = nn.Parameter(torch.ones(dim))   # 增益初始化为 1
        self.eps = eps
    def forward(self, x):
        return x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + self.eps) * self.weight

def depth_weights(blocks, partial_block, proj, norm):
    """示例代码：只算到 α（权重），用于观察打分行为"""
    V = torch.stack(blocks + [partial_block])
    K = norm(V)
    logits = torch.einsum('d, n b t d -> n b t', proj.weight.squeeze(), K)
    return logits.softmax(0), V

blocks = [torch.randn(B, T, D) for _ in range(N)]
partial_block = torch.randn(B, T, D)
proj = nn.Linear(D, 1, bias=False)     # weight 形状 [1, D]
norm = RMSNorm(D)

alpha_before, V_before = depth_weights(blocks, partial_block, proj, norm)

blocks_scaled = [b * 100.0 for b in blocks]          # 全部块放大 100 倍
alpha_after, V_after = depth_weights(blocks_scaled, partial_block, proj, norm)

print(alpha_before.shape)                             # torch.Size([4, 2, 5])
print((alpha_before - alpha_after).abs().max())       # 接近 0
print(V_after[0].norm() / V_before[0].norm())         # ≈ 100，V 保留原始幅度
```

**需要观察的现象**：候选放大 100 倍后，α 的最大逐元素变化量极小（理论上应为 0，实际因浮点误差与 eps 项约为 1e-7 量级）；而 V 中对应候选的范数精确放大 100 倍。

**预期结果**：`(alpha_before - alpha_after).abs().max()` 打印出一个接近 0 的小数；范数比值打印 `tensor(100.)`。具体数值待本地验证。

**为什么会这样**：RMSNorm 具有尺度不变性——\(\mathrm{norm}(c \cdot \mathbf{x}) = \mathrm{norm}(\mathbf{x})\)（对任意 \( c > 0 \)，忽略 eps），所以放大候选不改变 K，自然不改变 logits 与 α。这正是「打分看方向、不看嗓门」的定量体现。

#### 4.2.5 小练习与答案

**练习 1**：如果误把 `torch.stack` 写成 `torch.cat(blocks + [partial_block], dim=0)`，会发生什么？

**答案**：`cat` 沿已有第 0 维（batch 维）拼接，得到 `[(N+1)·B, T, D]`——候选维与 batch 维混叠，深度维根本不存在，后续 `softmax(0)` 会错误地沿 batch 维归一化，且函数形状断言全部失效。这是实现时最常见的错误。

**练习 2**：如果把 `K = norm(V)` 改成 `K = V`（不归一化直接打分），打分行为会怎样退化？

**答案**：logits 变成 \(\langle \mathbf{w}_l, \mathbf{v}_i \rangle\)，其大小同时受候选**幅度**与**方向**影响。幅度大的块即使方向与伪查询无关，也可能得到更大的 logit——「嗓门大的块垄断话语权」。本模块实践里的放大实验会直接展示这种退化（改掉 norm 后 α 将发生显著变化）。

**练习 3**：`norm` 作用在 `[N+1, B, T, D]` 上时，归一化的「粒度」是什么？

**答案**：粒度是单个 token 位置的 d 维向量：对每个 (n, b, t) 组合独立地沿 D 计算 RMS 并缩放。不同候选、不同 token 之间互不影响。

### 4.3 模块三：两次 einsum——深度 logits 与加权聚合

#### 4.3.1 概念说明

本模块覆盖函数的最后三行，也是整个 AttnRes 的「注意力」本体：

```python
logits = torch.einsum('d, n b t d -> n b t', proj.weight.squeeze(), K)
h = torch.einsum('n b t, n b t d -> b t d', logits.softmax(0), V)
return h
```

见 [README.md:L63-L65](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L63-L65)。

三个关键认知：

1. **第一次 einsum：伪查询逐 token 点积**。`proj.weight.squeeze()` 就是上一讲的伪查询 \(\mathbf{w}_l \in \mathbb{R}^d\)——`nn.Linear(D, 1)` 的 weight 形状 `[1, D]`，squeeze 后 `[D]`。下标式 `'d, n b t d -> n b t'` 中 d 被收缩：每个深度候选、每个 batch、每个 token 位置得到一个标量 logit。**没有使用 `proj(x)` 的调用形式，也没有出现 bias**——README 伪代码中的 logits 就是纯内积（实际实现是否如此以论文为准，仓库内待确认）。
2. **softmax(0)：注意力只作用于深度轴**。dim=0 是深度维（N+1 个候选）。b 和 t 两维不参与归一化——**每个 token 位置独立地**在自己的深度方向上分配权重，这正是「逐 token 输入依赖权重」的来源：不同位置的 K 不同，点积出的 logits 不同，softmax 后的 α 自然逐 token 不同。
3. **第二次 einsum：加权聚合原始 V**。下标式 `'n b t, n b t d -> b t d'` 中 n 被收缩：把 α 当系数，对 N+1 份原始候选做逐 token 加权求和，得到 `[B, T, D]`。

与标准 Transformer 注意力对比一下，能看清它「特化」在哪里：

| | 标准 token 注意力 | AttnRes 深度注意力 |
|:---|:---|:---|
| 查询 Q | 由输入 x 经 W_Q 投影 | 每层可学习伪查询 \(\mathbf{w}_l\)，不经输入投影 |
| 注意力轴 | 序列 T 维 | 深度 N+1 维 |
| 归一化维度 | softmax over T | softmax over 深度（dim=0） |
| 逐位置独立性 | token 间相互注意 | 各 token 位置完全独立 |

#### 4.3.2 核心流程

维度流转全景（以实践参数 N=3, B=2, T=5, D=8 为例）：

```text
proj.weight            [1, 8]  --squeeze()-->  w_l [8]
K = norm(V)            [4, 2, 5, 8]
   │
   ├─ einsum('d, n b t d -> n b t', w_l, K)
   │        d 被收缩：逐 (n,b,t) 点积
   ▼
logits                 [4, 2, 5]
   │
   ├─ softmax(dim=0)   沿深度 n 归一化，逐 (b,t) 独立
   ▼
α (权重)               [4, 2, 5]          α.sum(0) == 1 对每个 (b,t) 成立
   │
   ├─ einsum('n b t, n b t d -> b t d', α, V)
   │        n 被收缩：沿深度加权求和，聚合的是原始 V
   ▼
h                      [2, 5, 8]
```

用公式表达（\( k_i = \mathrm{norm}(\mathbf{v}_i) \)，对每个 token 位置独立计算）：

\[ \mathrm{logits}_i = \langle \mathbf{w}_l, \mathbf{k}_i \rangle, \qquad \alpha_i = \frac{e^{\mathrm{logits}_i}}{\sum_{j=0}^{N} e^{\mathrm{logits}_j}}, \qquad \mathbf{h} = \sum_{i=0}^{N} \alpha_i\, \mathbf{v}_i \]

顺带一个值得注意的观察：**训练开始时 α 接近均匀分布**。`nn.Linear` 默认初始化下 \(\mathbf{w}_l\) 各分量的量级约为 \(1/\sqrt{d}\)，与随机 K 的内积很小且候选间差异不大，softmax 输出接近 \(1/(N+1)\)——即初始时 AttnRes 近似「各候选等权平均」，随后在训练中逐渐分化出选择性。这是定性推断，具体数值待本地验证。

#### 4.3.3 源码精读

```python
logits = torch.einsum('d, n b t d -> n b t', proj.weight.squeeze(), K)
```

见 [README.md:L63](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L63)：伪查询（从 Linear 的 weight 中取出）与归一化候选逐 token 点积，产生深度方向的 logits `[N+1, B, T]`。两个下标式中 b、t 都只是「旁观」的批次维——再次印证注意力只在深度轴上发生。

```python
h = torch.einsum('n b t, n b t d -> b t d', logits.softmax(0), V)
return h
```

见 [README.md:L64-L65](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L64-L65)：`logits.softmax(0)` 内联在第二个 einsum 的第一个参数里，沿第 0 维（深度）归一化后，立刻与**原始 V**（而非 K）做加权求和，返回 `[B, T, D]`。一行完成了「归一化权重 + 凸组合聚合」两件事，也把 K/V 分离落到了实处。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：严格按照 README 伪代码实现完整的 `block_attn_res` 函数，构造 N=3 个历史块与 1 份部分和，验证三件事：(a) 输出形状为 `[B, T, D]`；(b) `softmax(0)` 权重沿深度求和为 1；(c) einsum 实现与显式循环实现数值等价。

**操作步骤**（示例代码，可保存为 `practice_u2l1_main.py`）：

```python
import torch
import torch.nn as nn

torch.manual_seed(0)
B, T, D, N = 2, 5, 8, 3

class RMSNorm(nn.Module):   # 示例代码：README 未给出 RMSNorm 内部实现
    def __init__(self, dim, eps=1e-6):
        super().__init__()
        self.weight = nn.Parameter(torch.ones(dim))
        self.eps = eps
    def forward(self, x):
        return x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + self.eps) * self.weight

def block_attn_res(blocks, partial_block, proj, norm):
    """与 README.md L53-L65 逐行对应的实现"""
    V = torch.stack(blocks + [partial_block])                                  # [N+1, B, T, D]
    K = norm(V)                                                                # [N+1, B, T, D]
    logits = torch.einsum('d, n b t d -> n b t', proj.weight.squeeze(), K)     # [N+1, B, T]
    h = torch.einsum('n b t, n b t d -> b t d', logits.softmax(0), V)          # [B, T, D]
    return h

# ---- 构造输入 ----
blocks = [torch.randn(B, T, D) for _ in range(N)]
partial_block = torch.randn(B, T, D)
proj = nn.Linear(D, 1, bias=False)   # weight: [1, D] -> squeeze -> [D]，即伪查询 w_l
norm = RMSNorm(D)

# ---- 运行 ----
h = block_attn_res(blocks, partial_block, proj, norm)
print("h.shape =", h.shape)                       # (a) 期待 torch.Size([2, 5, 8])

# ---- 单独重算权重，检查深度归一化 ----
V = torch.stack(blocks + [partial_block])
alpha = torch.einsum('d, n b t d -> n b t',
                     proj.weight.squeeze(), norm(V)).softmax(0)
print("alpha.shape =", alpha.shape)               # 期待 torch.Size([4, 2, 5])
print("alpha.sum(0) =", alpha.sum(0))             # (b) 期待每个 (b,t) 位置均为 1
print("alpha.mean() =", alpha.mean())             # 期待接近 1/(N+1) = 0.25

# ---- 与显式循环实现对照（把 einsum 翻译成 for 循环）----
h_manual = sum(alpha[n] * V[n] for n in range(N + 1))
print("allclose:", torch.allclose(h, h_manual, atol=1e-5))   # (c) 期待 True
```

**需要观察的现象**：

1. `h.shape` 打印 `torch.Size([2, 5, 8])`，深度维在输出中被收缩掉；
2. `alpha.sum(0)` 打印一个 `[2, 5]` 的张量，所有元素为 1（浮点误差内，如 0.9999999 / 1.0000001）；
3. `alpha.mean()` 接近 0.25，印证「初始化时权重接近均匀」的推断；
4. einsum 版与循环版 `allclose` 为 `True`。

**预期结果**：以上四项均由张量运算规则唯一确定，属于可从代码推出的确定性结论；具体打印的小数位待本地验证。

**常见调试提示**：若 `alpha.sum(0)` 不为 1，几乎一定是 `softmax` 的维度写错（比如写成了 `softmax(-1)`——那是沿 D 维归一化，语义完全错误）；若 `h.shape` 多出一个维度，检查第二次 einsum 的输出下标是否漏写了某个批次维。

#### 4.3.5 小练习与答案

**练习 1**：第一次 einsum 的输出下标是 `'n b t'`。如果把 K 换成形状 `[N+1, B, T, D]` 但下标式写成 `'d, n b t d -> n b t d'`，结果形状是什么？还能直接 softmax(0) 吗？

**答案**：`'d, n b t d -> n b t d'` 仍是合法的 einsum：d 在输出中出现，因此**不**被求和，w 沿 d 维广播成 `[N+1, B, T, D]`，结果是 \( w_d \cdot K_{nbt} \) 的逐分量乘积——不再是点积 logits，形状 `[N+1, B, T, D]`，语义错误。softmax(0) 虽能运行，但归一化的是「逐分量乘积」，不再是候选间的竞争。要点：**输出下标里不写 d，才会对 d 求和、得到内积形式的 logits**。

**练习 2**：标准 Transformer 注意力在 softmax 前会除以 \(\sqrt{d}\)（缩放点积注意力）。README 伪代码里有对应的缩放吗？

**答案**：观察 [README.md:L63](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L63) 可见：伪代码中 logits 是纯内积，没有 \(1/\sqrt{d}\) 缩放，也没有 bias 项。这不代表论文实际实现一定如此（以论文为准，仓库内待确认）。可以思考的点是：内积方差随 d 增长，缺乏缩放时 d 很大的模型 logits 可能偏大、softmax 趋近 one-hot——这是一个值得在自己实验台上做消融的问题（u2-l3 会系统做组件消融）。

**练习 3**：第二次 einsum 把 `logits.softmax(0)` 与 `V` 相乘求和。如果把 V 换成 K 会发生什么？

**答案**：输出变成归一化候选的凸组合，尺度统一在 RMSNorm 后的量级上：(a) 丢失了各候选真实幅度的信息；(b) 残差主干的尺度被强行改变，下游子层（`self.attn(self.attn_norm(h))`）收到的 h 含义不同；(c) 反向传播路径也改变（经过 norm 的除法）。这正是 4.2 强调「聚合必须用原始 V」的原因。

## 5. 综合实践

**任务：给你的 `block_attn_res` 做一次「可信度体检」，并用一个可手算的极小例子验证权重语义。**

前四项检查基于本讲主实践的代码环境（N=3, B=2, T=5, D=8），第五项换用极小配置：

1. **形状流转检查**：在函数内每行之后打印中间量形状（`V`、`K`、`logits`、`α`、`h`），与 4.3.2 的维度流转表逐项对照，确认每一行的输入输出形状吻合。
2. **归一化检查**：验证 `alpha.sum(0)` 与全 1 张量的最大误差小于 1e-5；再验证 `alpha` 全体元素非负。
3. **等价性检查**：用显式循环 `sum(alpha[n] * V[n] for n in range(N+1))` 复算 h，`torch.allclose` 应为 True——确保你写的 einsum 下标与头脑中的语义一致。
4. **尺度不变性检查**：重做 4.2.4 的放大实验（块 ×100），记录 α 的最大变化量，确认打分与幅度解耦。
5. **可手算的极小例子**：设 `B = T = 1, D = 2, N = 1`，构造两个方向正交的候选（示例代码）：

```python
torch.manual_seed(0)
blocks = [torch.tensor([[[3.0, 0.0]]])]        # 候选 0：指向 x 轴
partial_block = torch.tensor([[[0.0, 5.0]]])   # 候选 1：指向 y 轴
proj = nn.Linear(2, 1, bias=False)
with torch.no_grad():
    proj.weight.copy_(torch.tensor([[1.0, 0.0]]))   # 伪查询 w = [1, 0]，只认 x 方向
norm = RMSNorm(2)
```

   先在纸上推导：RMSNorm 后 \( k_0 = [\sqrt{2}, 0] \)、\( k_1 = [0, \sqrt{2}] \)（增益为 1 时），logits 为 \( [\sqrt{2}, 0] \)，因此 \( \alpha \approx [0.804, 0.196] \)，输出 \( \mathbf{h} \approx 0.804 \cdot [3, 0] + 0.196 \cdot [0, 5] \approx [2.41, 0.98] \)。然后运行函数对照打印值。

**通过标准**：五项检查全部通过，且第 5 项的手算预测与程序输出在 0.01 的绝对误差内一致（具体数值待本地验证）。完成后，你就拥有了一个经过验证、可以插入任意 Transformer 层的 Block AttnRes 计算单元——下一讲我们让它接入真实的 `forward` 调度。

## 6. 本讲小结

- `block_attn_res` 接收 N 份已完成块表示 + 1 份当前块部分和，`torch.stack` 沿**新增的第 0 维**拼成候选张量 `V [N+1, B, T, D]`——深度候选是 Block AttnRes 相对 Full 版本省显存的关键（候选数从 L 降到 N+1）。
- **键值分离**是本函数最重要的设计：打分用 `K = norm(V)`（RMSNorm 尺度不变，打分只看方向不看幅度），聚合用原始 `V`（保留候选真实幅度，凸组合保证输出有界）。
- 第一次 einsum `'d, n b t d -> n b t'` 用伪查询 \(\mathbf{w}_l\)（直接取 `Linear(D,1)` 的 `weight.squeeze()`，不经输入投影）对每个 token 位置独立地给每个候选打分。
- `softmax(0)` 沿深度维归一化，权重非负、逐 token 求和为 1；第二次 einsum `'n b t, n b t d -> b t d'` 沿深度加权聚合原始候选，输出回到 `[B, T, D]`。
- 初始化时权重接近均匀（约 \(1/(N+1)\)），选择性是训练出来的；README 伪代码的 logits 不含 \(1/\sqrt{d}\) 缩放与 bias（实际实现细节以论文为准）。
- 函数通过参数注入 `proj/norm` 实现计算与参数分离，被每层的注意力前（L71）与 MLP 前（L84）复用，两次使用独立的参数对。

## 7. 下一步学习建议

下一讲 **u2-l2《分块调度：forward 中的块边界与部分和机制》** 将补全本讲刻意跳过的另一半：`forward` 伪代码（[README.md:L67-L90](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L67-L90)）如何维护 `partial_block` 的块内标准残差累加、如何在 `layer_number % (block_size // 2) == 0` 时开新块、词 embedding 如何成为 blocks 的第 0 项。建议在进入下一讲前，确保本讲主实践的 `block_attn_res` 已通过综合实践的全部检查——它会作为零件直接拼进下一讲的完整调度逻辑。之后 u2-l3 会深入 `attn_res_proj/attn_res_norm` 两组组件的设计动机与参数开销分析。
