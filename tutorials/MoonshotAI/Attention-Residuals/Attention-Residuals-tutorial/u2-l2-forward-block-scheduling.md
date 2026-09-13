# 分块调度：forward 中的块边界与部分和机制

## 1. 本讲目标

上一讲（u2-l1）我们逐行精读了 `block_attn_res`——Block AttnRes 的「块间注意力」计算单元，并把 `forward` 中**两处调用它的环境**刻意留白。本讲补全这另一半：**README 中 `forward` 伪代码的调度逻辑**——`block_attn_res` 这个零件如何被装配进一个完整的 transformer 层。

学完本讲，你应该能够：

1. 说清楚「双状态」设计：残差流被拆成 `blocks`（只读的已完成块缓存）与 `partial_block`（当前块的可写部分和），两者如何跨层传递。
2. 掌握 `partial_block` 的块内标准残差累加：非边界层继承上一层部分和，边界层从注意力输出「白纸起算」，以及 `None` 哨兵值的用法。
3. 理解块边界判断 `layer_number % (block_size // 2) == 0` 的每个成分：`block_size` 以 ATTN+MLP 子层计数、每层 2 个子层、层号从 0 起点时嵌入如何恰好成为 `blocks[0]`。
4. 独立实现完整的调度逻辑：写一个 8 层（16 个子层）的模型骨架，逐层打印 `blocks` 数量与 `partial_block` 形状，验证块边界触发时新块正确开启、token 嵌入已包含在 `blocks` 中。

本讲依旧没有工程源码可读——全部「源码」就是 README 中 [README.md:L67-L90](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L67-L90) 的 24 行 `forward` 伪代码。但正是这 24 行决定了 Block AttnRes 的全部工程行为。

## 2. 前置知识

### 2.1 标准 PreNorm 层写法：我们要替换的东西

一个标准 PreNorm transformer 层（u1-l2 讲过结构，这里给出典型代码形态）通常写成：

```python
# 示例代码：标准 PreNorm 层的典型写法
def forward(self, hidden_states):
    h = hidden_states + self.attn(self.attn_norm(hidden_states))   # 残差 + 注意力
    h = h + self.mlp(self.mlp_norm(h))                             # 残差 + MLP
    return h
```

它维护**一条**残差流 `hidden_states`，每个子层读完就往里加。对照 AttnRes 的 `forward` 签名：

| | 标准 PreNorm | Block AttnRes（本讲） |
|:---|:---|:---|
| 状态 | 一条残差流张量 | `blocks` 列表 + `partial_block` 张量 |
| 读取方式 | 子层直接读 `norm(残差流)` | 子层读 `norm(跨块注意力聚合 h)` |
| 写入方式 | 每个子层 `+=` 进残差流 | 每个子层 `+=` 进 `partial_block` |
| 聚合权重 | 固定全 1 累加 | 块间 softmax 注意力、块内全 1 累加 |

注意第二个参数名仍叫 `hidden_states`——与标准层签名保持一致，这正是「drop-in 替换」意图的体现。

### 2.2 Python 的引用与重绑定：为什么必须 `return blocks, partial_block`

`forward` 末尾显式返回 `(blocks, partial_block)`，原因藏在 Python 语义里：

- **列表是引用传递**：`blocks.append(x)` 是原地修改，调用方持有的同一个列表对象会同步可见；
- **张量变量是重绑定**：`partial_block = partial_block + attn_out` 创建了**新**张量对象并把局部名字指向它，调用方手里的旧引用完全不受影响。

所以 `partial_block` 不返回就会丢；`blocks` 虽靠引用共享也能传下去，但显式返回更安全（不依赖隐式共享，便于移植到函数式风格或其他框架）。

### 2.3 两个超参：`layer_number` 与 `block_size`

- `self.layer_number`：当前层的编号。README 未规定起点；本讲采用 **0 起点**（理由见 4.3.1），论文的确切约定以论文原文为准（待确认）。
- `self.block_size`：块的大小，**以子层（ATTN+MLP）为单位计数**，每个 transformer 层含 2 个子层——这是 [README.md:L74](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L74) 注释的原文约定。因此 `block_size // 2` = 每块容纳的**层数**，记作 \( k \)。

### 2.4 `None` 哨兵值

[README.md:L77](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L77) 把 `partial_block` 置为 `None` 表示「旧块已封存、新块还没有任何内容」。[README.md:L81](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L81) 用三元表达式处理两种情形——`partial_block` 非空则累加、为 `None` 则用注意力输出直接起算。用特殊值标记「尚未初始化」是常见工程手法（哨兵值）。

### 2.5 符号表

| 符号 | 含义 | 本讲实践取值 |
|:---:|:---|:---:|
| L | transformer 层数 | 8 |
| l | 层编号 `layer_number`（0 起点） | 0–7 |
| k | 每块层数 = `block_size // 2` | 4 |
| block_size | 每块子层数（ATTN+MLP 计） | 8 |
| N | `blocks` 中已完成块的数量（含嵌入块） | 随深度增长 |
| \( b_n^i \) | README docstring 对 `partial_block` 的记号：第 n 块累加到第 i 层的部分和（[README.md:L59](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L59)） | — |
| \( \mathbf{v}_i \) | 第 i 个子层输出（u1-l2 术语），\( \mathbf{v}_0 \) 为词嵌入 | — |
| B / T / D | batch / 序列长 / 隐藏维 | 2 / 10 / 32 |

## 3. 本讲源码地图

本仓库是论文发布仓库，README 中的伪代码块（[README.md:L52-L91](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L52-L91)）仍是全仓库唯一可精读的「源码」。

| 文件 | 位置 | 作用 |
|:---|:---|:---|
| `README.md` | L67–L90 | **本讲主角**：`forward` 伪代码——块边界判断、部分和维护、两次 `attn_res` 调用 |
| `README.md` | L53–L65 | `block_attn_res`：u2-l1 已逐行精读并实现，本讲作为已验证的零件直接调用 |
| `README.md` | L45–L47 | Block AttnRes 文字定义：块内标准残差、块间注意力、约 8 块恢复大部分收益 |
| `README.md` | L28 | 概览图 (c) 题注：分块把显存从 O(Ld) 降到 O(Nd) |
| `README.md` | L41 | 总公式 \( \mathbf{h}_l = \sum_{i=0}^{l-1} \alpha_{i \to l} \mathbf{v}_i \)：\( \mathbf{v}_0 \) 即词嵌入，是嵌入进 blocks 的公式依据 |
| `assets/overview.png` | — | 概览图 (c) 子图：块划分的结构示意 |

## 4. 核心概念与源码讲解

先看 `forward` 全貌（逐字引自 README）：

> [README.md:L67-L90](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L67-L90)：层级 forward，输入已完成块列表与进层状态，输出更新后的两者。

```python
def forward(self, blocks: list[Tensor], hidden_states: Tensor) -> tuple[list[Tensor], Tensor]:
    partial_block = hidden_states
    # apply block attnres before attn
    # blocks already include token embedding
    h = block_attn_res(blocks, partial_block, self.attn_res_proj, self.attn_res_norm)

    # if reaches block boundary, start new block
    # block_size counts ATTN + MLP; each transformer layer has 2
    if self.layer_number % (self.block_size // 2) == 0:
        blocks.append(partial_block)
        partial_block = None

    # self-attention layer
    attn_out = self.attn(self.attn_norm(h))
    partial_block = partial_block + attn_out if partial_block is not None else attn_out

    # apply block attnres before MLP
    h = block_attn_res(blocks, partial_block, self.mlp_res_proj, self.mlp_res_norm)

    # MLP layer
    mlp_out = self.mlp(self.mlp_norm(h))
    partial_block = partial_block + mlp_out

    return blocks, partial_block
```

一层的执行可以画成 8 步状态机（\( k = \text{block\_size} / 2 \)）：

```text
层入口: (blocks, hidden_states)
 ① partial_block ← hidden_states                      # 进层部分和
 ② h ← block_attn_res(blocks, partial, attn_res_*)    # 位点 1：注意力前的跨块聚合
 ③ 若 l mod k == 0:                                    #     块边界
      blocks.append(partial_block)                     #       封存旧块（此后只读）
      partial_block ← None                             #       开新块（哨兵值）
 ④ attn_out ← attn(attn_norm(h))                       # 自注意力子层（PreNorm）
 ⑤ partial ← partial + attn_out  或  attn_out          # 写入：边界层白纸起算
 ⑥ h ← block_attn_res(blocks, partial, mlp_res_*)      # 位点 2：MLP 前的跨块聚合
 ⑦ mlp_out ← mlp(mlp_norm(h))                          # MLP 子层（PreNorm）
 ⑧ partial ← partial + mlp_out                         # 写入
层出口: return (blocks, partial_block)
```

下面按四个最小模块拆开：**forward 调度**（双状态传递）、**partial_block 部分和**（块内累加）、**块边界判断**（子层计数与封存时机）、**attn/mlp 两次 attn_res 应用**（两个位点的差异）。

### 4.1 模块一：forward 调度——blocks 与 partial_block 的双状态传递

#### 4.1.1 概念说明

标准残差里只有一条流；Block AttnRes 把它拆成两个角色互补的状态：

- **`blocks`：只读的「已完成块」缓存**。每个元素是一份 `[B, T, D]` 的块表示，`append` 封存之后**永不修改**（write-once）。它是对下游所有层可见的历史，也是块间注意力的候选来源。
- **`partial_block`：当前块的「可写部分和」**。块内子层输出逐个 `+=` 进来；它只在当前块存续期间有效，块边界一到就被封存进 `blocks` 并重新起算。

这个拆分正是「块内标准残差、块间注意力」（[README.md:L45-L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L45-L47)）在数据结构上的体现：**写入端永远只写 partial（加法），读取端从 blocks+partial 的注意力聚合读**——读写分离。README 说分块把需要作为注意力候选保留的表示从每层一份降到每块一份，显存由 \( O(Ld) \) 降到 \( O(Nd) \)（[README.md:L28](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L28)，深入分析留给 u3-l1）。

#### 4.1.2 核心流程

跨层传递的全景（模型级视角，层号 0 起点）：

```text
初始化:  blocks = [ ]                # 空列表
         partial = embedding         # 词嵌入 = 初始部分和（也是未来的 blocks[0]）
              │
   ┌──────────▼────────────────────────────────────────────┐
   │ for l in 0..L-1:                                       │
   │     blocks, partial = layer_l(blocks, partial)          │
   │     # 层内：读(blocks,partial) → 封存/不封存 → 写partial  │
   └──────────┬────────────────────────────────────────────┘
              ▼
收尾:      最后对 (blocks, partial) 做一次聚合得到输出表示
           （README 未给出模型级收尾，属实现选择，见 5. 综合实践）
```

`len(blocks)` 随深度增长的节奏：进入第 \( l \) 层时

\[ N(l) = \left\lceil l / k \right\rceil \]

即每过 \( k \) 层，边界触发一次、`blocks` 长度加 1（第 0 层的边界封存的是嵌入）。

#### 4.1.3 源码精读

**签名与进层赋值**：

```python
def forward(self, blocks: list[Tensor], hidden_states: Tensor) -> tuple[list[Tensor], Tensor]:
    partial_block = hidden_states
```

见 [README.md:L67-L68](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L67-L68)：签名表明层的输入是「块列表 + 进层状态」、输出是「更新后的两者」；第一行把上一层返回的 `partial_block` 经 `hidden_states` 接进来，重命名为局部变量 `partial_block`。类型标注 `list[Tensor]` 明示 blocks 是张量的 Python 列表（不是堆叠好的张量），堆叠发生在 `block_attn_res` 内部的 `torch.stack`。

**返回**：

```python
    return blocks, partial_block
```

见 [README.md:L90](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L90)：以元组返回双状态，供下一层或模型收尾使用。结合 2.2 的 Python 语义：`partial_block` 的多次赋值都是重绑定，不返回就会丢失；`blocks` 虽被原地 `append`，显式返回让数据流在代码里可见。

**可直接运行的层实现**（示例代码，与伪代码逐行对应，后续实践共用）：

```python
import torch
import torch.nn as nn

class RMSNorm(nn.Module):
    """示例代码：README 未给出 RMSNorm 内部实现，与 u2-l1 保持一致"""
    def __init__(self, dim: int, eps: float = 1e-6):
        super().__init__()
        self.weight = nn.Parameter(torch.ones(dim))
        self.eps = eps
    def forward(self, x):
        return x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + self.eps) * self.weight

def block_attn_res(blocks, partial_block, proj, norm):
    """u2-l1 已验证的实现，对应 README L53-L65"""
    V = torch.stack(blocks + [partial_block])                               # [N+1,B,T,D]
    K = norm(V)
    logits = torch.einsum('d, n b t d -> n b t', proj.weight.squeeze(), K)  # [N+1,B,T]
    h = torch.einsum('n b t, n b t d -> b t d', logits.softmax(0), V)       # [B,T,D]
    return h

class BlockAttnResLayer(nn.Module):
    """示例代码：对应 README L67-L90；attn/mlp 由外部注入（可用任意子层或占位模块）"""
    def __init__(self, d, attn, mlp, layer_number, block_size):
        super().__init__()
        self.attn, self.mlp = attn, mlp
        self.attn_norm, self.mlp_norm = RMSNorm(d), RMSNorm(d)   # 子层入口 PreNorm
        self.attn_res_proj = nn.Linear(d, 1, bias=False)         # 位点 1 伪查询容器
        self.attn_res_norm = RMSNorm(d)
        self.mlp_res_proj = nn.Linear(d, 1, bias=False)          # 位点 2 伪查询容器
        self.mlp_res_norm = RMSNorm(d)
        self.layer_number, self.block_size = layer_number, block_size

    def forward(self, blocks, hidden_states):
        partial_block = hidden_states                                            # L68
        h = block_attn_res(blocks, partial_block,
                           self.attn_res_proj, self.attn_res_norm)               # L71
        if self.layer_number % (self.block_size // 2) == 0:                      # L75
            blocks.append(partial_block)                                         # L76
            partial_block = None                                                 # L77
        attn_out = self.attn(self.attn_norm(h))                                  # L80
        partial_block = partial_block + attn_out if partial_block is not None else attn_out  # L81
        h = block_attn_res(blocks, partial_block,
                           self.mlp_res_proj, self.mlp_res_norm)                 # L84
        mlp_out = self.mlp(self.mlp_norm(h))                                     # L87
        partial_block = partial_block + mlp_out                                  # L88
        return blocks, partial_block                                             # L90
```

#### 4.1.4 代码实践

**实践目标**：不写一行张量运算，只用 Python 列表和字符串「纸面模拟」整个调度，验证 `blocks` 的长度增长节奏、以及第 0 层封存的确实是词嵌入、第 1 块恰好装 8 份子层输出。

**操作步骤**（示例代码，可保存为 `practice_u2l2_scheduler.py`）：

```python
L, block_size = 8, 8
k = block_size // 2                       # 每块层数 = 4
blocks = []                               # 用字符串代表块内容
partial = "emb"                           # 词嵌入 = 初始部分和
len_trace = []
for l in range(L):
    n_cand_1 = len(blocks) + 1            # 位点 1 的候选数 N+1
    if l % k == 0:                        # L75：块边界
        blocks.append(partial)            # L76：封存
        partial = None                    # L77：开新块
    partial = (f"attn{l}+mlp{l}" if partial is None
               else partial + f"+attn{l}+mlp{l}")            # L81 + L88
    n_cand_2 = len(blocks) + 1            # 位点 2 的候选数
    len_trace.append(len(blocks))
    print(f"layer {l}: 候选数 {n_cand_1}->{n_cand_2}  len(blocks)={len(blocks)}")

print(len_trace)        # 长度轨迹
print(blocks[0])        # 第 0 块内容
print(len(blocks[1].split("+")))   # 第 1 块装的子层输出份数
```

**需要观察的现象**：`len_trace` 为 `[1, 1, 1, 1, 2, 2, 2, 2]`——只在第 0 层和第 4 层（\( l \bmod 4 = 0 \)）增长；`blocks[0]` 打印 `emb`；第 1 块内容是 `attn0+mlp0+...+attn3+mlp3`，共 8 份子层输出，恰好等于 `block_size`。

**预期结果**：以上结论由循环逻辑直接推出，属确定性结果；具体打印格式待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果删掉 `return`、调用方只在自己手里保存一份 `blocks` 引用，调度还能正确进行吗？`partial_block` 呢？

**答案**：`blocks` 能——`append` 是原地修改，调用方与被调方共享同一个列表对象，封存动作双方可见。`partial_block` 不能——它在层内被多次**重绑定**为新张量，调用方持有的旧引用不会更新，必须靠返回值传递。这也是伪代码返回元组的原因（`blocks` 靠引用也能活，但显式返回让数据流可读、可移植）。

**练习 2**：`blocks` 中已封存的块表示，之后还会被修改吗？这个性质有什么好处？

**答案**：不会。`append` 之后代码中再无任何对 `blocks[i]` 的写操作——write-once。好处：深度注意力的候选是稳定的（同层内位点 1 与位点 2 读到的 blocks 一致，除非中间发生了封存）；历史表示可以被安全地缓存复用，不必担心被后续层悄悄改写。

**练习 3**：为什么第二个参数叫 `hidden_states` 而不是直接叫 `partial_block`？

**答案**：命名对齐标准 transformer 层的惯例签名 `forward(self, hidden_states)`，体现 drop-in 替换的设计意图——在标准实现中这个位置传的是残差流，这里传的是「当前块的部分和」，角色一一对应。进入层内第一行才把它重命名为 `partial_block`。（这是对命名意图的解读，README 未明说。）

### 4.2 模块二：partial_block——块内的标准残差部分和

#### 4.2.1 概念说明

`partial_block` 就是 u1-l2 讲过的**标准残差累加，只是范围从「整条流水线」缩小到「当前块内」**。README 的 docstring 用 \( b_n^i \) 表示它（第 n 块累加到第 i 层的部分和，[README.md:L59](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L59)）。以块 1（层 0 到层 \( k{-}1 \)）为例：

\[ b_1^{(l)} = \sum_{j=0}^{l} \left( \mathbf{v}_{j}^{\text{attn}} + \mathbf{v}_{j}^{\text{mlp}} \right), \qquad l = 0, 1, \dots, k-1 \]

注意两个要点：

1. **新块从零起算**：边界层封存旧块后 `partial_block = None`，第一个子层输出（`attn_out`）直接成为新部分和——新块**不含**嵌入、也不含旧块内容。膨胀与稀释因此被限制在块内尺度（最多 `block_size` 份子层输出），而不是整条深度。
2. **部分和有两条来源路径**：非边界层的部分和继承自上一层的返回值；边界层的部分和从 `attn_out` 白纸起算。这解释了 [README.md:L81](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L81) 为什么必须写成分支形式。

#### 4.2.2 核心流程

```text
非边界层（l mod k ≠ 0）:
  partial_in (= 上一层出口部分和)
    → 位点 1 聚合读取
    → partial + attn_out          # L81：partial 非 None，走加法分支
    → 位点 2 聚合读取
    → partial + mlp_out           # L88

边界层（l mod k == 0）:
  partial_in (= 旧块最后状态)
    → 位点 1 聚合读取（旧块的最后一次被读！）
    → 封存: blocks.append(partial_in); partial = None
    → attn_out 直接成为 partial    # L81：None 分支，白纸起算
    → 位点 2 聚合读取
    → partial + mlp_out
```

#### 4.2.3 源码精读

```python
    partial_block = hidden_states
```

见 [README.md:L68](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L68)：进层部分和就位。对第 0 层而言它等于词嵌入——嵌入以「初始部分和」的身份进入调度（更多见 4.3.3）。

```python
    blocks.append(partial_block)
    partial_block = None
```

见 [README.md:L76-L77](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L76-L77)：封存与开新块。`append` 的对象是**进层时的** `partial_block`（若本层是边界层，它就是旧块所能达到的最终部分和）；随后置 `None` 作为「新块尚无内容」的哨兵。

```python
    attn_out = self.attn(self.attn_norm(h))
    partial_block = partial_block + attn_out if partial_block is not None else attn_out
```

见 [README.md:L79-L81](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L79-L81)：注意力子层的读与写。读端是 `self.attn_norm(h)`——PreNorm 结构保留，norm 的输入从「残差流」换成了「跨块聚合 h」。写端的三元表达式按 Python 优先级解析为 `partial_block = (partial_block + attn_out) if (partial_block is not None) else attn_out`，一个分支覆盖两种层型。

```python
    mlp_out = self.mlp(self.mlp_norm(h))
    partial_block = partial_block + mlp_out
```

见 [README.md:L86-L88](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L86-L88)：MLP 子层同样「读 h、写 partial」。这里**没有**判 `None`——因为执行到 L88 时，`partial_block` 在 L81 必然已被赋成张量（`None` 只存活于 L77 到 L81 之间）。这是一个值得注意的不变式。

#### 4.2.4 代码实践

**实践目标**：用 4.1.3 的 `BlockAttnResLayer`（子层用 `nn.Identity` 占位）干跑**单个层**，用 forward hook 捕获子层输出，验证边界层与非边界层部分和的两种来源公式。

**操作步骤**（示例代码，沿用 4.1.3 的三个类定义）：

```python
torch.manual_seed(0)
d = 8
outs = {}
def hook(name):
    return lambda m, i, o: outs.__setitem__(name, o)

for ln, is_boundary in [(4, True), (1, False)]:
    layer = BlockAttnResLayer(d, nn.Identity(), nn.Identity(), ln, block_size=8)
    layer.attn.register_forward_hook(hook("attn"))
    layer.mlp.register_forward_hook(hook("mlp"))
    t_old, t_in = torch.randn(2, 3, d), torch.randn(2, 3, d)
    blocks_in = [t_old]
    blocks_out, partial_out = layer(blocks_in, t_in)
    print(f"layer {ln}: len(blocks) {len(blocks_in)}->{len(blocks_out)}")
    if is_boundary:
        # 白纸起算：出口部分和 = 本层两个子层输出之和，且不含进层部分和
        print("  从零起算:", torch.allclose(partial_out, outs["attn"] + outs["mlp"], atol=1e-6))
        print("  混入旧部分和:", torch.allclose(partial_out, t_in + outs["attn"] + outs["mlp"], atol=1e-6))
        print("  封存对象是进层部分和:", torch.equal(blocks_out[-1], t_in))
    else:
        print("  继承累加:", torch.allclose(partial_out, t_in + outs["attn"] + outs["mlp"], atol=1e-6))
```

**需要观察的现象**：边界层 `len(blocks)` 从 1 变 2，三个检查打印 `True / False / True`；非边界层 `len(blocks)` 保持 1，检查打印 `True`。

**预期结果**：布尔值结论由代码路径唯一确定（`Identity` 子层不影响张量相等性判断），属确定性结果；具体打印格式待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：[README.md:L88](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L88) 的 `partial_block = partial_block + mlp_out` 为什么可以不判 `None`？

**答案**：因为 `None` 只在 L77 设置，而 L81 的两个分支都会把 `partial_block` 赋成一个张量（加法分支或 `attn_out`）。执行到 L88 时不变式「partial 是张量」必然成立。若把边界判断挪到 MLP 之后（改动了执行顺序），这个不变式就会被打破。

**练习 2**：块内部分和与 u1-l2 的标准残差流有何异同？

**答案**：相同——都是子层输出按固定权重 1 的等权累加（`+` 就是权重为 1 的写入）。不同——累加范围从整条流水线缩小到当前块（最多 `block_size` 份），且新块从零起算、不含嵌入。标准残差的「幅度无界增长、贡献稀释」在块内仍存在，但被限制在块尺度内；跨块的聚合交给 softmax 注意力，这就是 Block AttnRes 的分工。

**练习 3**：边界层（如第 4 层）的**位点 1** 聚合读到的最后一个候选是什么？

**答案**：进层时的 `partial_block`——旧块所能达到的最终部分和。位点的调用在边界判断**之前**（L71 在 L75 之前），所以旧块在被封存前还会被完整地读最后一次；封存后新部分和（只有 `attn_out` 一项）才出现在位点 2 的候选里。

### 4.3 模块三：块边界判断——layer_number % (block_size // 2) 与子层计数

#### 4.3.1 概念说明

边界判断只有一行：

```python
    if self.layer_number % (self.block_size // 2) == 0:
```

见 [README.md:L75](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L75)，其上一行注释（[README.md:L74](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L74)）给出了解读钥匙：**block_size 以 ATTN+MLP 子层计数，每个 transformer 层含 2 个**。于是：

- \( k = \text{block\_size} / 2 \) = 每块容纳的**层数**；
- 边界条件 \( l \bmod k = 0 \)，触发层为 \( \{0, k, 2k, \dots\} \)；
- 第 \( b \) 个「层块」（\( b \ge 1 \)）覆盖层 \( [\,b k,\ (b{+}1)k - 1\,] \)，恰含 \( 2k = \text{block\_size} \) 份子层输出。

**层号起点与嵌入块**。[README.md:L70](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L70) 的注释 `# blocks already include token embedding` 说明了一个不变式：跨块注意力的候选里永远有词嵌入。README 未给出模型级初始化代码；与总公式 \( \mathbf{h}_l = \sum_{i=0}^{l-1} \alpha_{i \to l} \mathbf{v}_i \)（[README.md:L41](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L41)，其中 \( \mathbf{v}_0 \) 即嵌入，见 u1-l3）最自洽的实现是：

- 模型级初始化 `blocks = []`、`partial = embedding`，层号 **0 起点**；
- 第 0 层：\( 0 \bmod k = 0 \) 必然触发——位点 1 的候选只有嵌入一份（u2-l1 练习 1 证明过：单一候选经 softmax 权重为 1，`h` 恰好等于嵌入，与标准实现第 0 层行为一致）；随后边界把嵌入封存为 `blocks[0]`。

这样嵌入成为**只含一个元素的「第 0 块」**，此后每个层块都是纯净的子层输出之和，块结构与公式的候选 \( \mathbf{v}_0, \mathbf{v}_1, \dots \) 一一对应。另一种做法是模型级预置 `blocks = [embedding]`，此时必须**跳过第 0 层的 append**，否则嵌入会被重复封存成两份相同候选——这是实现时最容易踩的坑。README 未规定层号起点与初始化细节，论文的确切做法以论文原文为准（待确认）。

#### 4.3.2 核心流程

以本讲实践配置（\( L = 8 \)，`block_size` = 8，\( k = 4 \)）为例的完整调度表：

| 层号 l | 边界触发 | 进层 len(blocks) | 位点 1 候选（N+1） | 封存动作 | 位点 2 候选 | 出口 len(blocks) |
|:---:|:---:|:---:|:---|:---|:---|:---:|
| 0 | ✓ | 0（空列表） | 1：`[emb]` | 封存 `emb` | 2：`[emb, a₀]` | 1 |
| 1 | ✗ | 1 | 2：`[emb, b₁⁰]` | — | 2 | 1 |
| 2 | ✗ | 1 | 2 | — | 2 | 1 |
| 3 | ✗ | 1 | 2 | — | 2 | 1 |
| 4 | ✓ | 1 | 2：`[emb, b₁³]`（旧块最后一读） | 封存 `b₁³` → B1 | 3：`[emb, B1, a₄]` | 2 |
| 5 | ✗ | 2 | 3 | — | 3 | 2 |
| 6 | ✗ | 2 | 3 | — | 3 | 2 |
| 7 | ✗ | 2 | 3 | — | 3 | 2 |

（`a_l` 表示第 l 层注意力输出；`b₁ⁱ` 为块 1 累加到第 i 层的部分和。）运行结束时 `blocks = [emb, B1]`、`partial` 是块 2 的部分和；若收尾再封存一次，总块数 = \( L/k + 1 = 3 \)。候选数 N+1 每过 \( k \) 层才 +1——这就是「块间注意力只需面对 \( O(Nd) \) 候选」的调度来源。

#### 4.3.3 源码精读

```python
    # if reaches block boundary, start new block
    # block_size counts ATTN + MLP; each transformer layer has 2
    if self.layer_number % (self.block_size // 2) == 0:
```

见 [README.md:L73-L75](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L73-L75)：两行注释分别说明动作（到达块边界就开新块）与计数单位（block_size 按 ATTN+MLP 计，每层 2 个），取模判断由此可翻译为「每过 block_size//2 层触发一次」。**判断的位置**也很讲究：它夹在位点 1（L71）与自注意力（L80）之间——旧块先被完整读最后一次，再封存。如果把它挪到层入口之前，位点 1 将不得不面对 `partial_block = None`（stack 会直接报错）——顺序本身承载了语义。

```python
        blocks.append(partial_block)
        partial_block = None
```

见 [README.md:L76-L77](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L76-L77)：封存与开新块的一对动作，共同维护「blocks 只增不改、partial 块内累加」的分工（见 4.1.1）。

```python
With ~8 blocks, it recovers most of Full AttnRes's gains while serving as a practical drop-in replacement with marginal overhead.
```

见 [README.md:L45-L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L45-L47)：README 对块数取舍的说明——约 8 个块即可恢复 Full 版本的大部分收益。块数 N 由 `block_size` 与总层数 L 共同决定（\( N \approx L/k + 1 \)），块越大候选越少、开销越小、粒度越粗；如何在两者之间取衡是 u3-l1 的主题。

#### 4.3.4 代码实践

**实践目标**：扫描不同 `block_size`，验证边界层集合与块数公式，并观察两种极端配置的形态。

**操作步骤**（示例代码，纯 Python 模拟即可）：

```python
def schedule(L, block_size):
    k = block_size // 2
    boundaries = [l for l in range(L) if l % k == 0]
    appends = len(boundaries)            # 运行中封存次数（含第 0 层的嵌入）
    return k, boundaries, appends

for bs in [2, 4, 8, 16]:
    k, bnd, n = schedule(L=8, block_size=bs)
    print(f"block_size={bs:>2}: k={k}  边界层={bnd}  运行中封存={n}  "
          f"每块子层输出={2*k}  收尾后总块数={8 // k + 1}")
```

**需要观察的现象**：

| block_size | k | 边界层 | 收尾后总块数（含嵌入块） |
|:---:|:---:|:---|:---:|
| 2 | 1 | 0,1,2,3,4,5,6,7（每层都是边界） | 9 |
| 4 | 2 | 0,2,4,6 | 5 |
| 8 | 4 | 0,4 | 3 |
| 16 | 8 | 0 | 2 |

**预期结果**：表格数值由取模运算直接确定（确定性结论，具体打印格式待本地验证）。两个值得玩味的极端：`block_size=2` 时块粒度等于层，块表示退化为「每层的 attn+mlp 之和」，候选粒度最细；`block_size=16 > L` 时只有第 0 层触发，整个模型只有嵌入块加一份大部分和，块间注意力只剩两个候选。

#### 4.3.5 小练习与答案

**练习 1**：把 `block_size` 设成奇数（如 5）会怎样？

**答案**：`5 // 2 = 2`，边界每 2 层触发一次，每块实际收 \( 2 \times 2 = 4 \) 份子层输出——「5」这个数字永远无法兑现，等价于 `block_size=4`。既然 block_size 的单位是「ATTN+MLP 成对计数」，它应当取偶数；奇数值会被 `// 2` 静默吞掉。

**练习 2**：24 层模型、`block_size=8`：边界在哪些层触发？运行结束时 `blocks` 有几个元素？收尾封存后呢？

**答案**：\( k = 4 \)，边界层 \( \{0, 4, 8, 12, 16, 20\} \)，共 6 次封存（第 0 次是嵌入，其余 5 次对应层 0–3、4–7、8–11、12–15、16–19 五个完整块）。运行结束时 `blocks` 有 6 个元素（嵌入 + 5 个块），`partial` 是第 6 个块（层 20–23）的部分和；收尾封存后共 7 个 = \( 24/4 + 1 \)。

**练习 3**：为什么说层号取 0 起点与 README 总公式更一致？

**答案**：公式 \( \mathbf{h}_l = \sum_{i=0}^{l-1} \alpha_{i \to l} \mathbf{v}_i \) 中 \( \mathbf{v}_0 \) 是嵌入自身这一项。0 起点下第 0 层边界触发恰好把嵌入封存为 `blocks[0]`（单元素的第 0 块），后续每个块对应一组连续的子层输出项——块结构与公式的求和项一一对应。若层号 1 起点，第 1 层不触发边界，首个块会混入嵌入或边界整体错位一层。README 确实未规定起点，此为基于公式的解读（论文细节待确认）。

### 4.4 模块四：attn / MLP 前的两次 attn_res 应用

#### 4.4.1 概念说明

每层有**两个** `attn_res` 位点：注意力前（[README.md:L71](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L71)）与 MLP 前（[README.md:L84](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L84)），各自使用**独立的参数对**：`(attn_res_proj, attn_res_norm)` 与 `(mlp_res_proj, mlp_res_norm)`。这修正一个容易混淆的粒度：u1-l3 说「每层一个可学习伪查询 \(\mathbf{w}_l\)」，落到 forward 里其实是**每个子层位点一个**伪查询——注意力子层与 MLP 子层各自决定「如何看待历史」。

两次调用的差异不止在参数：

- **位点 1 的候选**：`blocks + [进层部分和]`；
- **位点 2 的候选**：`blocks（可能已多一个封存块）+ [含本层注意力输出的部分和]`；
- **读端不变式**：两个子层都不直接消费候选，而是消费 `子层_norm(h)`——PreNorm 的「norm 在子层入口」结构原样保留，只是 norm 的对象从标准实现的残差流换成了跨块凸组合 `h`（u2-l1 讲过凸组合保证幅度有界，这正是 README 训练动态结论的结构基础，u2-l5 会实测）。

#### 4.4.2 核心流程

```text
                blocks ──┬──────────────────────────┬───────────────┐
                         │                          │               │
  partial(进层) ─┐       │                          │               │
                ├─ stack → block_attn_res ──h──→ attn_norm ──→ attn ─┤
                │        (attn_res_proj/norm)                        │ attn_out
                │                                                      ↓
                │                            partial(更新后：可能白纸起算)
                │                                                      │
                ├─ stack → block_attn_res ──h──→ mlp_norm ──→ mlp ────┤ mlp_out
                │        (mlp_res_proj/norm)                          ↓
                └──────────────────────────────────── partial(出口) ──┘
                （每个子层：从块级注意力「读」，向 partial 加法「写」）
```

位点上候选集合的四种情形（对应 4.3.2 调度表的最后两列）：

| 层型 | 位点 1 候选 | 位点 2 候选 |
|:---|:---|:---|
| 非边界层 | N+1：`blocks + 旧部分和` | N+1：同左（部分和多了 attn_out） |
| 边界层 | N+1：`blocks + 旧部分和`（旧块最后一读） | N+2：`blocks+封存块 + 仅含 attn_out 的新部分和` |

#### 4.4.3 源码精读

```python
    h = block_attn_res(blocks, partial_block, self.attn_res_proj, self.attn_res_norm)
```

见 [README.md:L71](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L71)：位点 1——注意力前的跨块聚合，注入注意力的专属 `proj/norm` 参数对（其上一行注释 `# apply block attnres before attn` 与 `# blocks already include token embedding` 分别说明时机与候选不变式，见 [README.md:L69-L70](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L69-L70)）。

```python
    h = block_attn_res(blocks, partial_block, self.mlp_res_proj, self.mlp_res_norm)
```

见 [README.md:L83-L84](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L83-L84)：位点 2——MLP 前的跨块聚合，换用 MLP 专属参数对。与位点 1 相比，此刻的 `blocks` 与 `partial_block` 都可能已经变化（边界封存 + 注意力输出写入）。

```python
    attn_out = self.attn(self.attn_norm(h))
    ...
    mlp_out = self.mlp(self.mlp_norm(h))
```

见 [README.md:L80](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L80) 与 [README.md:L87](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L87)：两个子层入口的 norm 与标准 PreNorm 写法完全同构——`attn(attn_norm(x))`、`mlp(mlp_norm(x))`，只是 `x` 的来源从「残差流」换成了「跨块聚合 h」。这就是 drop-in 的含义：子层本身零改动，改的只是喂给它们什么的「历史视图」。

**参数开销**（为 u2-l3 的组件分析做铺垫）：每个位点 `proj`（`Linear(d,1)`，d 个参数）+ `norm`（RMSNorm 增益，d 个参数）≈ 2d；每层两个位点共 ≈ 4d。对比子层自身的 \( O(d^2) \) 参数（注意力投影、MLP 升维），这是一笔可忽略的边际开销——与 [README.md:L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L47) "marginal overhead" 的说法一致。

#### 4.4.4 代码实践

**实践目标**：用 `named_parameters()` 清点每层 attn_res 组件的参数，验证「每层 4d、占比可忽略」的账目。

**操作步骤**（示例代码，沿用 4.1.3 的类定义）：

```python
d = 32
layer = BlockAttnResLayer(d, nn.Identity(), nn.Identity(), layer_number=0, block_size=8)
res_params = {n: p.numel() for n, p in layer.named_parameters() if "res_" in n}
for n, c in res_params.items():
    print(f"{n}: {c}")
print("attn_res 组件总数:", sum(res_params.values()), "  4d =", 4 * d)   # 期待 128 = 128

# 对照：一个常规 MLP 子层（升维 4 倍）的参数量
mlp = nn.Sequential(nn.Linear(d, 4 * d), nn.GELU(), nn.Linear(4 * d, d))
print("常规 MLP 参数量:", sum(p.numel() for p in mlp.parameters()))     # 8352
print("占比:", sum(res_params.values()) / sum(p.numel() for p in mlp.parameters()))
```

**需要观察的现象**：`res_params` 恰好 4 项（`attn_res_proj.weight`、`attn_res_norm.weight`、`mlp_res_proj.weight`、`mlp_res_norm.weight`），每项 32；总数 128 = 4d；相对一个常规 MLP 子层（8352 参数）占比约 1.5%。

**预期结果**：所有数字由模块定义直接算出（确定性结论）；`Identity` 占位子层不含参数，不干扰统计。具体打印格式待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：两个位点的 `proj` 能共享同一份参数吗？设计上为什么分开？

**答案**：跑通没问题（传同一个 `Linear` 即可），但设计上应分开：注意力前与 MLP 前面对的是不同子层，「该从历史取什么」理应不同——公式粒度上是两个独立的伪查询。共享会强制两个子层用同一种「历史观」打分，等价于删掉一半的表达自由度。（是否论文实测过共享版消融，仓库未提及，待确认。）

**练习 2**：子层真正消费的输入是 `h` 吗？

**答案**：不是，是 `attn_norm(h)` / `mlp_norm(h)`。`h` 是候选的凸组合（尺度有界但仍随内容变化），子层入口的 PreNorm 再做一次归一化，保证子层收到的分布稳定——这与标准 PreNorm 中 norm 的角色一致，只是搬到了「聚合结果」的入口。

**练习 3**：如果删掉位点 2（MLP 前不做 attn_res，直接 `mlp_out = mlp(mlp_norm(partial_block))`），调度状态会受影响吗？

**答案**：不会——位点 2 是**纯读取**（只算 `h`，不写 `blocks`/`partial`），删掉它不影响任何状态变量的演化，只影响 MLP 的输入内容。这正是「读端/写端分离」的好处：聚合位点可以增删、改造，而不触碰调度本身。（当然模型行为会变：MLP 失去跨块视野，退回只看当前块部分和。）

## 5. 综合实践

**任务：实现 README forward 的完整调度逻辑——8 层（16 子层）Block AttnRes 模型骨架，逐层体检块调度。**

模型级代码如下（示例代码，可保存为 `practice_u2l2_main.py`；层与 `block_attn_res` 沿用 4.1.3 的定义）：

```python
class MiniBlockAttnResModel(nn.Module):
    def __init__(self, vocab, d, n_layers=8, block_size=8):
        super().__init__()
        self.embed = nn.Embedding(vocab, d)
        def sublayer():                       # 占位子层：真实注意力/MLP 可无缝替换
            return nn.Sequential(nn.Linear(d, d), nn.GELU())
        self.layers = nn.ModuleList(
            BlockAttnResLayer(d, sublayer(), sublayer(), i, block_size)
            for i in range(n_layers))
        # 模型级收尾聚合：README 未规定收尾方式，此处再聚合一次作输出表示（示例选择，待确认）
        self.out_res_proj = nn.Linear(d, 1, bias=False)
        self.out_res_norm = RMSNorm(d)
        self.head = nn.Linear(d, vocab, bias=False)

    def forward(self, idx, verbose=False):
        emb = self.embed(idx)
        blocks, partial = [], emb             # 关键初始化：空 blocks，嵌入为初始部分和
        trace = []                            # 记录每层出口的 partial，供事后验证
        for i, layer in enumerate(self.layers):
            blocks, partial = layer(blocks, partial)
            trace.append(partial)
            if verbose:
                k = layer.block_size // 2
                print(f"layer {i}: 边界触发={i % k == 0}  len(blocks)={len(blocks)}"
                      f"  partial{tuple(partial.shape)}")
        h_final = block_attn_res(blocks, partial, self.out_res_proj, self.out_res_norm)
        return self.head(h_final), blocks, emb, trace

# ---- 运行 ----
B, T, D, L, BS = 2, 10, 32, 8, 8
model = MiniBlockAttnResModel(vocab=50, d=D, n_layers=L, block_size=BS)
idx = torch.randint(0, 50, (B, T))
logits, blocks, emb, trace = model(idx, verbose=True)

# ---- 体检 1：块边界触发时新块正确开启（第 4 层出口 = 本层两个子层输出之和）----
k = BS // 2
attn4, mlp4 = [], []
model.layers[k].attn.register_forward_hook(lambda m, i, o: attn4.append(o))
model.layers[k].mlp.register_forward_hook(lambda m, i, o: mlp4.append(o))
logits, blocks, emb, trace = model(idx)                 # 再跑一次触发钩子
print("新块白纸起算:", torch.allclose(trace[k], attn4[0] + mlp4[0], atol=1e-6))

# ---- 体检 2：token 嵌入已包含在 blocks 中 ----
print("blocks[0] 就是词嵌入:", torch.equal(blocks[0], emb))

# ---- 体检 3：块数公式 ----
print("层块数:", len(blocks) - 1, "= L // k =", L // k)
blocks.append(trace[-1])                                # 收尾封存最后一块
print("总块数(含嵌入块):", len(blocks), "= L // k + 1 =", L // k + 1)
print("logits 形状:", tuple(logits.shape))
```

**预期输出**（逐层打印部分；数值结论由调度逻辑唯一确定，具体格式待本地验证）：

```text
layer 0: 边界触发=True   len(blocks)=1  partial(2, 10, 32)
layer 1: 边界触发=False  len(blocks)=1  partial(2, 10, 32)
layer 2: 边界触发=False  len(blocks)=1  partial(2, 10, 32)
layer 3: 边界触发=False  len(blocks)=1  partial(2, 10, 32)
layer 4: 边界触发=True   len(blocks)=2  partial(2, 10, 32)
layer 5: 边界触发=False  len(blocks)=2  partial(2, 10, 32)
layer 6: 边界触发=False  len(blocks)=2  partial(2, 10, 32)
layer 7: 边界触发=False  len(blocks)=2  partial(2, 10, 32)
新块白纸起算: True
blocks[0] 就是词嵌入: True
层块数: 2 = L // k = 2
总块数(含嵌入块): 3 = L // k + 1 = 3
logits 形状: (2, 10, 50)
```

**检查要点**：

1. **逐层打印**：`len(blocks)` 只在层 0 和层 4（\( l \bmod 4 = 0 \)）增长，其余层不变；`partial` 形状恒为 `(B, T, D)`——部分和是「同形状张量的逐层替换」，不堆叠深度。
2. **边界开新块**：`trace[4]` 与第 4 层两个子层输出之和 `allclose`——第 4 层出口的部分和不包含任何层 0–3 的内容（白纸起算）。
3. **嵌入在 blocks 中**：`torch.equal(blocks[0], emb)` 为 `True`——第 0 层边界封存的对象正是词嵌入。
4. **块数公式**：层块数 = \( L/k = 2 \)，收尾总块数 = \( L/k + 1 = 3 \)（嵌入块 + 2 个层块）。

**延伸**（可选）：把 `block_size` 改成 4 再跑一遍，验证边界变为 {0, 2, 4, 6}、`len(blocks)` 轨迹变为 `[1, 1, 2, 2, 3, 3, 4, 4]`；把 `sublayer()` 换成真实的多头注意力与 MLP，骨架就升级为可训练模型——u2-l4 将在这一骨架上与标准残差基线做对比训练。

**通过标准**：四项检查全部通过，且能不看讲义说出：层 4 位点 1 与位点 2 的候选分别是什么（答案：位点 1 是 `[emb, 层0–3的部分和]` 共 2 份；位点 2 是 `[emb, B1, 仅含层4注意力输出的部分和]` 共 3 份）。

## 6. 本讲小结

- `forward` 用**双状态**替代标准残差的单一残差流：`blocks`（append-only、封存后只读的块缓存）+ `partial_block`（当前块内按权重 1 累加的部分和）；层间靠 `return blocks, partial_block` 显式传递（张量重绑定不返回即丢失）。
- `partial_block` 的两条来源路径：非边界层继承上一层部分和继续 `+=`；边界层经 `None` 哨兵后由 `attn_out` 白纸起算——块内标准残差、写入永远只发生在 partial 上。
- 块边界判断 `layer_number % (block_size // 2) == 0`：`block_size` 按 ATTN+MLP 子层计数（每层 2 个），\( k = \text{block\_size}/2 \) 为每块层数；0 起点层号下第 0 层边界恰好把词嵌入封存为单元素的第 0 块，块结构与公式 \( \mathbf{h}_l = \sum_{i=0}^{l-1} \alpha_{i \to l} \mathbf{v}_i \) 的候选一一对应（层号起点与初始化细节 README 未规定，以论文为准，待确认）。
- 边界判断**夹在**位点 1 与自注意力之间：旧块先被完整读最后一次、再封存——执行顺序本身承载语义，挪到层入口会让位点 1 面对 `None` 候选。
- 每层两个 attn_res 位点（注意力前/MLP 前）各持独立的 `proj/norm` 参数对（每位点约 2d、每层约 4d 参数，边际开销）；子层消费的是 `子层_norm(h)`——PreNorm 结构保留，子层零改动，这正是 drop-in 替换的落点。
- 候选数 N+1 每过 \( k \) 层才 +1（8 层、block_size=8 的运行中最多 3 份候选），分块调度由此把跨深度注意力的开销钉在 \( O(Nd) \)——u3-l1 将定量测这笔账。

## 7. 下一步学习建议

下一讲 **u2-l3《关键组件：伪查询、投影层与 RMSNorm 的作用》** 将深入本讲反复注入的两个组件：`attn_res_proj`（为什么伪查询只取 `Linear` 的 weight、不经输入投影）与 `attn_res_norm`（为什么归一化 K 而不归一化 V），并做组件消融实验（去 RMSNorm / 换 LayerNorm / 去投影）观察深度注意力权重与输出幅度的变化。建议在进入下一讲前，先完成本讲综合实践的骨架与四项体检——u2-l3 的消融实验会直接复用这套代码。之后再进入 u2-l4，把它升级成与标准残差基线对比训练的最小实验台。
