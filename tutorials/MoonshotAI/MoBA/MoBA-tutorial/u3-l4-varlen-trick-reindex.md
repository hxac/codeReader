# u3-l4 varlen trick：被选中的 Q 与 KV 的重组索引

## 1. 本讲目标

上一讲（u3-l3）结束时，我们手里已经有一张布尔掩码 `gate_mask [N, H, S]`：它标记了「每个 query 位置、每个头」选中了哪些候选块。但 flash-attn 的 varlen 接口根本不认识「块选择掩码」这种东西——它只会老老实实地对一段段连续序列做（稠密）注意力。

本讲要解决的问题就是：**如何把一张稀疏的块选择掩码，重新组织成 flash-attn varlen 接口能直接消化的张量**。这段代码是整个 `moba_efficient.py` 中索引操作最密集、也最精巧的部分，源码作者自己给它起的注释名就叫 "varlen trick"。

学完本讲你应该能：

1. 说清 `gate_mask.nonzero` 如何变成 `moba_q_indices`，以及它的分段顺序约定（块优先、头次之）。
2. 独立推导 `moba_q_sh_indices = moba_q_indices % seqlen * num_head + moba_q_indices // seqlen` 中 `%` 与 `//` 各自还原出什么坐标。
3. 解释 `moba_kv` 的 `rearrange + split + cat` 三步如何把 KV 重排成与 Q 完全对齐的段顺序。
4. 解释「零 expert」是什么、为什么必须裁剪掉（否则反向梯度可能 NaN）、两处 `assert` 各自在守护什么。

## 2. 前置知识

### 2.1 varlen 打包格式（回顾 u1-l3）

flash-attn 的 varlen 接口把一个 batch 的多条变长序列首尾拼接成一条「长序列」，再用 `cu_seqlens`（长度 = 批次数 + 1、单调递增、首元素为 0 的前缀和）标记每条序列的起止边界。注意力只发生在每条序列内部，序列之间互相不可见。**这正是 MoBA 借用的通道：把「一个 query 对一个选中块」的一次注意力，伪装成 varlen 批次里的一条独立短序列。**

### 2.2 gate_mask（回顾 u3-l2 / u3-l3）

- 候选块 = 每个 batch 中除最后一块以外的所有块（`filtered_chunk_indices`），候选块**全部是满块**（长度恰为 `moba_chunk_size`）——这是 u3-l2 的核心结论，本讲会再次用到。
- `gate_mask [N, H, S]` 中 `N = num_filtered_chunk`（候选块数）、`H = num_head`、`S` 为 varlen 批次总 token 数；`gate_mask[n, h, s] = True` 表示位置 `s` 的 query 在头 `h` 上选中了候选块 `n`。
- 因果性与 batch 隔离已经在选块阶段由 gate 掩码前置完成，所以后面 MoBA 支路可以用 `causal=False`。

### 2.3 一个容易踩的命名坑：本讲里的 `seqlen` 是「总 token 数」

在 [moba/moba_efficient.py:L303](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L303) 中：

```python
seqlen, num_head, head_dim = q.shape
```

`q` 的形状是 `[S, H, D]`，是**整个 varlen 批次拼接后的总 token 数**，而不是某一条序列的长度。本讲所有 `% seqlen`、`// seqlen` 都是对这个「全局长度」取模/整除，读代码时务必记住。

### 2.4 「expert」术语

MoBA 全称 Mixture of Block Attention，借用了 MoE（混合专家）的比喻：每个块像一个「专家」，query 像「token 路由到专家」。源码注释里的 "zero experts"（[L391](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L391)）指的就是**没有任何 query 选中的块**——像一个门可罗雀的专家。

### 2.5 需要用到的 PyTorch / einops 原语

| 原语 | 作用 |
| --- | --- |
| `tensor.nonzero(as_tuple=True)` | 返回每组维度上的非零下标，按行主（字典序）排序 |
| `tensor.index_select(dim, idx)` | 沿某维按下标列表抽取行 |
| `tensor.sum(dim)` / `.flatten()` | 求和 / 展平 |
| `rearrange(x, "s h d -> ( h s ) d")` | einops 语法：把 `s`、`h` 两轴按 `h` 在外、`s` 在内的顺序合并成一根轴 |
| `tensor.split(size, dim)` / `torch.cat` | 按固定长度切段 / 沿某维拼接 |

## 3. 本讲源码地图

本讲几乎全部内容集中在同一个文件的一段代码里：

| 文件 | 本讲关注的区域 | 作用 |
| --- | --- | --- |
| [moba/moba_efficient.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py) | L373-L428 | varlen trick 主体：`moba_q_indices` / `moba_seqlen_q` / `moba_q` / `moba_q_sh_indices` / `moba_kv` / 两个 `cu_seqlen` / 零 expert 裁剪 |
| 同上 | L299, L325-L330 | 上游输入：`kv = stack((k, v))` 与 `filtered_kv` 的 gather（u3-l3 已讲，本讲只引用结论） |
| 同上 | L105-L116 | 下游消费：`MixedAttention.forward` 里 MoBA 支路如何使用这些重组后的张量 |
| 同上 | L231-L241 | 反向传播中 `moba_q_sh_indices` 的复用（细节留给 u3-l6） |
| [tests/test_moba_attn.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py) | L37-L42 | 参数化测试网格，综合实践中用作验证入口 |

## 4. 核心概念与源码讲解

先给一张总览图。本讲覆盖从 `gate_mask` 到 `MixedAttention.apply` 之间的全部索引变换：

```text
gate_mask [N, H, S]                      ← u3-l3 的产物
    │  reshape(N, H*S) + nonzero         （模块 4.1）
    ├─→ moba_q_indices      [T]          选中清单，值域 [0, H*S)，按（块, 头, 位置）排序
    │
    │  sum(dim=-1).flatten()             （模块 4.2）
    ├─→ moba_seqlen_q       [N*H]        每个（块 × 头）段收集到多少个 Q
    │
    │  rearrange(q, "s h d -> (h s) d")  （模块 4.2）
    │    .index_select(0, moba_q_indices)
    ├─→ moba_q              [T, 1, D]    被选中的 Q 的拷贝（头维折叠成 1）
    │
    │  % seqlen * H + // seqlen          （模块 4.3）
    ├─→ moba_q_sh_indices   [T]          每份 Q 拷贝对应原始 q 的展平下标（值域 [0, S*H)）
    │
    │  rearrange + split + cat           （模块 4.4）
    ├─→ moba_kv             [N'*cs, 2, 1, D]   按（块, 头）段排列的 KV
    │
    │  零 expert 裁剪 + 前缀和 + 等差数列 （模块 4.5）
    ├─→ moba_cu_seqlen_q    [N'+1]       Q 侧变长段边界
    ├─→ moba_cu_seqlen_kv   [N'+1]       KV 侧定长段边界（每段恰为 chunk_size）
    ↓
MixedAttention.apply(...)                ← u3-l5 的起点
```

其中 `T` = 选中总数，`N' = N*H − 零 expert 数`，`cs = moba_chunk_size`。

### 4.1 moba_q_indices：把块选择掩码拉直成「选中清单」

#### 4.1.1 概念说明

`gate_mask` 是一张三维布尔表，直接拿着它去 gather 张量很不方便。我们想要的是一份**一维的、按段组织的选中清单**：清单里每一项对应「某个 (块, 头) 段收集到的一个 query 位置」，段与段首尾相接。`moba_q_indices` 就是这份清单。

关键设计决策是**段的排列顺序**：先按块（chunk）分段，每块内部再按头（head）分段，每个 (块, 头) 段内部按 query 位置升序。源码注释用一行画出了这个布局：

```text
[ C0H0 ][ C0H1 ][ C0H2 ][ ... ][ CnHm ]
```

也就是说，(块, 头) 的优先级高于 (头, 块)。后面会看到，KV 侧重排（4.4）刻意对齐到了同一个顺序，Q 侧与 KV 侧才能逐段配对。

#### 4.1.2 核心流程

1. 把 `gate_mask [N, H, S]` 重塑为二维 `[N, H*S]`：把（头, 位置）两轴按「头在外、位置在内」合并成一列下标，满足

   \[ \text{col} = h \cdot S + s \]

2. 对二维矩阵做 `nonzero`：PyTorch 保证返回的下标按行主（字典序）排列——先按第 0 维（块编号 `n`）升序，再按第 1 维（列 `col`）升序。这一排序性质正是「按段组织」的全部来源。
3. `nonzero(as_tuple=True)` 返回一个元组，每维一个下标张量；取 `[-1]` 只要**列下标**，因为行号（块号）的信息已经隐含在分段顺序里了（段边界由 4.2 的 `moba_seqlen_q` 单独记录）。

于是 `moba_q_indices` 是长度为 `T = gate_mask.sum()` 的一维张量，第 `i` 个元素落在某个 (块 `n`, 头 `h`) 段内，值为 `h*S + s`。

#### 4.1.3 源码精读

[moba/moba_efficient.py:L373-L377](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L373-L377)——注释里的 `C0H0` 即「chunk 0, head 0」段，`[ HS indices ] * N` 说明清单按块分成 N 段、每段内是 HS 展平后的列下标：

```python
# varlen trick: combining all q index that needs moba attn
# the result will be like [ C0H0 ][ C0H1 ][ C0H2 ][ ... ][ CnHm ]
moba_q_indices = gate_mask.reshape(gate_mask.shape[0], -1).nonzero(as_tuple=True)[
    -1
]  # [ HS indices ] * N
```

两点说明：

- `reshape(N, -1)` 只重塑、不复制内存，`nonzero` 的行主排序让「块 0 的所有选中 → 块 1 的所有选中 → …」自然成为清单的一级结构。
- 每个 query 位置（每列 `s`、每个头 `h`）最多有 `moba_topk − 1` 个 `True`（u3-l1 讲过 topk 已减一）。注意这是**上限**而非精确值：受因果与 batch 边界限制，序列开头的位置可选块更少，甚至可能一个都没有（例如某条序列第一个块内的位置，没有任何「已结束的满块」可选）。反过来，某个 (块, 头) 段也可能一个 query 都收不到——那就是 4.5 要裁剪的「零 expert」。

#### 4.1.4 代码实践

**实践目标**：亲眼确认 `nonzero` 的行主排序如何产生分段结构。

**操作步骤**（示例代码，只需 CPU 版 PyTorch，可写在独立脚本或 REPL 里）：

```python
import torch

N, H, S = 2, 2, 8
gate_mask = torch.zeros(N, H, S, dtype=torch.bool)
gate_mask[0, 0, [2, 5, 7]] = True   # 块0 头0 收到 s=2,5,7
gate_mask[1, 0, [4, 6]] = True      # 块1 头0 收到 s=4,6
gate_mask[0, 1, [1, 3]] = True      # 块0 头1 收到 s=1,3
gate_mask[1, 1, [0, 4, 7]] = True   # 块1 头1 收到 s=0,4,7

print(gate_mask.reshape(N, -1))     # 先看二维矩阵长什么样
idx = gate_mask.reshape(N, -1).nonzero(as_tuple=True)[-1]
print(idx)
```

**需要观察的现象与预期结果**（手工推演，待本地验证）：二维矩阵第 0 行（块 0）的 `True` 位于列 {2, 5, 7, 9, 11}，第 1 行（块 1）位于列 {4, 6, 8, 12, 15}，因此输出应为

```text
tensor([ 2,  5,  7,  9, 11,  4,  6,  8, 12, 15])
```

前 5 个元素是块 0 的两个头（列 2/5/7 属头 0，列 9/11 属头 1，因为头 1 的列区间从 `1*8+0=8` 开始），后 5 个元素是块 1 的两个头。这份清单将在本讲后续模块中反复使用。

#### 4.1.5 小练习与答案

**练习 1**：`moba_q_indices` 中元素的取值范围是多少？为什么不含块编号？
**答案**：范围是 `[0, H*S)`，每个值编码一对 `(h, s) = (值 // S, 值 % S)`。块编号不需要编码——它由段边界隐含表达，段长度另行记录在 `moba_seqlen_q` 里。

**练习 2**：如果把 `reshape(N, -1)` 误写成 `reshape(-1)`（直接拉平成一维），会发生什么？
**答案**：`nonzero` 会返回 `[0, N*H*S)` 内的全局展平下标，清单虽然仍是升序，但一级结构变成了「头优先」而非「块优先」，与后面 `moba_seqlen_q` 的 `flatten()`（块优先）以及 `moba_kv` 的段顺序全部错位，无法配对。

**练习 3**：为什么取 `nonzero(as_tuple=True)[-1]` 而不是 `[0]`？
**答案**：`[0]` 是每个 `True` 的行号（块号）序列；但块号可以由「段长度做前缀和」恢复，而列下标不可恢复，所以只保留 `[-1]`（列下标），块边界信息交给 `moba_seqlen_q`。

### 4.2 moba_seqlen_q 与 moba_q：段长度统计与 Q 的按需拷贝

#### 4.2.1 概念说明

有了选中清单还不够，flash-attn varlen 接口需要三样东西：**拼接好的 Q 张量**、**Q 侧的段边界（cu_seqlens_q）**、以及稍后的 KV 侧段边界。本模块完成前两样：

- `moba_seqlen_q[n*H + h]` = 候选块 `n` 在头 `h` 上收集到的 query 数量。它同时承担两个角色：**做前缀和得到 Q 侧 cu_seqlens**；**标记每个 (块, 头) 段的长度**。
- `moba_q` = 按 `moba_q_indices` 逐份拷贝出来的 Q。注意一个 query 若选中了 3 个块，就会被拷贝 3 份——**Q 是允许重复的**，重复的代价由下一讲的 LSE 合一来补偿。

还有一个容易忽略的形状细节：`moba_q` 最终是 `[T, 1, D]`，中间那个 `1` 是**头数**。也就是说，在 MoBA 支路里，「头」这根轴被彻底折叠进了 varlen 批次维：每个 (块, 头) 段是一条**单头**的独立序列。flash-attn 从头到尾都以为自己在处理一个「几千条单头变长序列」的普通批次。

#### 4.2.2 核心流程

1. `gate_mask.sum(dim=-1)` 对每个 (块, 头) 数 True 的个数 → `[N, H]`；`.flatten()` → `[N*H]`，**块优先、头次之**，与 4.1 的段顺序一致。
2. 把 `q [S, H, D]` 重排成 `[(H S), D]`：第 `h*S + s` 行恰是 `q[s, h]`——与 `moba_q_indices` 的编码方式严丝合缝。
3. `index_select(0, moba_q_indices)` 按清单抽出 Q 的拷贝，再 `unsqueeze(1)` 补上长度为 1 的头维，得到 `[T, 1, D]`。

不变量（裁剪前）：

\[ T \;=\; \sum_{n,h} \text{moba\_seqlen\_q}[nH+h] \;=\; \text{moba\_q\_indices.shape[0]} \;=\; \text{moba\_q.shape[0]} \]

#### 4.2.3 源码精读

[moba/moba_efficient.py:L378-L384](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L378-L384)——先数段长，再按清单抽 Q：

```python
# moba_seqlen_q indicates that how many q chunks are selected for each kv chunk - head
moba_seqlen_q = gate_mask.sum(dim=-1).flatten()
# select all q that needs moba attn based on the moba_q_indices
moba_q = rearrange(q, "s h d -> ( h s ) d").index_select(
    0, moba_q_indices
)  # [ selected_S, D ]
moba_q = moba_q.unsqueeze(1)
```

下游消费方式可以印证「头折叠进 varlen」的说法。[moba/moba_efficient.py:L105-L116](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L105-L116) 中，`moba_q` 被直接当作 varlen Q 传入 flash-attn 内部接口，段边界用 `moba_cu_seqlen_q`，且 `causal=False`：

```python
_, _, _, _, moba_attn_out, moba_attn_lse_hs, _, _ = _flash_attn_varlen_forward(
    q=moba_q,
    k=moba_kv[:, 0],
    v=moba_kv[:, 1],
    cu_seqlens_q=moba_cu_seqlen_q,
    cu_seqlens_k=moba_cu_seqlen_kv,
    ...
    causal=False,
)
```

#### 4.2.4 代码实践

**实践目标**：验证 `moba_seqlen_q` 的段顺序与 `moba_q_indices` 的分段一一对齐，且 `moba_q` 抽取的行确实是 `q[s, h]`。

**操作步骤**（示例代码，接 4.1 的 `gate_mask`）：

```python
from einops import rearrange

moba_seqlen_q = gate_mask.sum(dim=-1).flatten()
print(moba_seqlen_q)                # 期望 [3, 2, 2, 3]

q = torch.arange(S * H, dtype=torch.float32).reshape(S, H, 1)  # 用行号当身份证
moba_q = rearrange(q, "s h d -> ( h s ) d").index_select(0, idx).unsqueeze(1)
print(moba_q.squeeze(1).flatten())
```

**需要观察的现象与预期结果**（手工推演，待本地验证）：`moba_seqlen_q = [3, 2, 2, 3]`，即四个段 [C0H0, C0H1, C1H0, C1H1] 的长度；`rearrange` 后第 `h*S+s` 行的值是 `s*H+h`（`q` 的展平下标），按 `idx = [2,5,7,9,11,4,6,8,12,15]` 抽取后应打印

```text
tensor([4., 10., 14., 3., 7., 8., 12., 1., 9., 15.])
```

（例如 `idx[0]=2` 编码 `h=0, s=2`，对应 `q[2,0]`，展平下标 `2*2+0=4`。）这个数字串在 4.3 会再次出现——它就是 `moba_q_sh_indices`。

**待本地验证**：以上输出为手工推演，请实际运行核对。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `moba_q` 要 `unsqueeze(1)`？
**答案**：flash-attn varlen 接口要求 Q 形状为 `[total_tokens, num_heads, head_dim]`。MoBA 支路把头折叠进了 varlen 批次维，所以 `num_heads = 1`，需要补一根长度为 1 的轴。

**练习 2**：某 query 位置在头 `h` 上选中了 3 个块，它在 `moba_q` 中出现几次？最终输出里它对应几份结果？如何合并？
**答案**：出现 3 次（每个选中块一份拷贝）；mixed 输出中它会得到 3 份局部注意力结果，加上 self-attn 支路 1 份，共 4 份，由 `moba_q_sh_indices` 定位后用 LSE 在线合并（u3-l5 的主题）。

**练习 3**：`moba_seqlen_q` 的长度是多少（零 expert 裁剪前）？
**答案**：`N * H`，即候选块数 × 头数——每个 (块, 头) 段一个条目。

### 4.3 moba_q_sh_indices：hs 坐标到 sh 坐标的换算

#### 4.3.1 概念说明

`moba_q` 里的每一份 Q 拷贝，最终算完注意力后要**写回原始输出的正确位置**；反向传播时也要用它把输出梯度搬到 MoBA 布局。这需要一份「反向地址簿」：`moba_q_sh_indices[i]` = 第 `i` 份 Q 拷贝来自原始 `q` 的哪个展平位置。

微妙之处在于**两套展平坐标并存**：

- 收集 Q 时用的 `(h s)` 布局：`idx_hs = h·S + s`（`moba_q_indices` 的值域）。
- 写回输出时用的 `(s h)` 布局：输出缓冲 `output [S, H, D]` 展平成 `[S*H, D]` 后，位置 `s` 头 `h` 的行号是 `idx_sh = s·H + h`。

`moba_q_sh_indices` 做的就是这两套坐标之间的换算——本质上是对 `(s, h)` 网格做一次「转置」：

\[
\text{idx}_{sh} \;=\; \big(\text{idx}_{hs} \bmod S\big)\cdot H \;+\; \Big\lfloor \text{idx}_{hs} / S \Big\rfloor
\]

#### 4.3.2 核心流程

对清单中每个 `idx_hs`：

1. `idx_hs % seqlen` 还原 `s`：因为 `idx_hs = h·S + s` 且 `0 ≤ s < S`，模 `S` 的余数恰为 `s`。
2. `idx_hs // seqlen` 还原 `h`：商恰为 `h`。
3. 重新组合成 `s·H + h`。

Python 运算符优先级提醒：`%`、`//`、`*` 同级且左结合，所以源码里不加括号的写法

```python
moba_q_indices % seqlen * num_head + moba_q_indices // seqlen
```

等价于 `(moba_q_indices % seqlen) * num_head + (moba_q_indices // seqlen)`。初读时很容易在这行停顿，实际语义就是上面的公式。

#### 4.3.3 源码精读

[moba/moba_efficient.py:L385-L386](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L385-L386)——注释直白地说明了用途：「每个 Q 拷贝在原始 q 张量中的位置」：

```python
# moba_q_sh_indices represents the position in the origin q tensor of each q token inside moba_q
moba_q_sh_indices = moba_q_indices % seqlen * num_head + moba_q_indices // seqlen
```

它的消费点分布在前向与反向两侧：

- 前向合并（详见 u3-l5）：[L132-L135](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L132-L135) 用它 `index_reduce` 求每个输出位置的混合最大 LSE；[L146-L148](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L146-L148) 用它 `index_add_` 累加指数权重；[L165](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L165) 用它把 MoBA 支路输出累加进输出缓冲。
- 反向（详见 u3-l6）：[L231-L241](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L231-L241) 用它把 `d_output`、`output`、混合 LSE 从 `[S, H]` 布局搬到 MoBA 支路的逐拷贝布局。

#### 4.3.4 代码实践

**实践目标**：确认换算公式的正确性，并体会运算符优先级。

**操作步骤**（示例代码）：

```python
import torch

S, H = 8, 2
idx = torch.tensor([2, 5, 7, 9, 11, 4, 6, 8, 12, 15])
sh = idx % S * H + idx // S
sh_paren = (idx % S) * H + (idx // S)     # 显式括号版
print(torch.equal(sh, sh_paren))          # True：优先级确实如此解析
print(sh)
```

**需要观察的现象与预期结果**（手工推演，待本地验证）：

```text
tensor([ 4, 10, 14,  3,  7,  8, 12,  1,  9, 15])
```

与 4.2 实践中 `moba_q` 抽出的「身份证」数字串完全一致——这正说明 `moba_q_sh_indices` 记录的就是每份拷贝的原始展平行号。逐例核对两个：`idx=9 → h=1, s=1 → 1*2+1=3`；`idx=12 → h=1, s=4 → 4*2+1=9`。

#### 4.3.5 小练习与答案

**练习 1**：`idx_hs = 11`，`S = 8`，`H = 2`，求 `(h, s)` 与 `idx_sh`。
**答案**：`h = 11 // 8 = 1`，`s = 11 % 8 = 3`，`idx_sh = 3*2 + 1 = 7`。

**练习 2**：为什么写回时不能用 `moba_q_indices` 本身当行号？
**答案**：`moba_q_indices` 是 `(h s)` 展平（头优先），而输出缓冲 `output.view(-1, D)` 是 `[S, H, D]` 的 `(s h)` 展平（位置优先）。同一对 `(s, h)` 在两套展平里的线性下标不同，混用会张冠李戴。

**练习 3**：假如代码改成先 `q.flatten(0, 1)`（得到 `(s h)` 布局）再 gather，`moba_q_sh_indices` 还需要吗？
**答案**：gather 本身可以直接用 `moba_q_sh_indices` 完成，省掉 `rearrange`；但反向和合并处仍反复需要这份地址簿，所以无论如何它都必须被计算并保存（事实上它被一路传进 `MixedAttention` 并 `save_for_backward`）。

### 4.4 moba_kv 重排：rearrange + split + cat 对齐（块, 头）顺序

#### 4.4.1 概念说明

Q 侧已经按「(块, 头) 段」组织好了，KV 侧也必须排成完全相同的段顺序，flash-attn 的 varlen 段才能一一配对：第 `i` 段的 Q 们，恰好对上第 `i` 段的那个块。

KV 的出发点是 u3-l3 里 gather 出来的 `filtered_kv`，形状 `[N*cs, 2, H, D]`（token 优先，`2` 是 K/V 栈维，来自 [L299](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L299) 的 `kv = torch.stack((k, v), dim=1)`）。而目标布局是：**第 `n*H + h` 段 = 候选块 `n` 在头 `h` 上的 `cs` 个 token**——即段顺序变为块优先、头次之，段内 token 升序。

#### 4.4.2 核心流程

三步走，每步只交换两根轴的优先级：

| 步骤 | 代码 | 形状变化 | 效果 |
| --- | --- | --- | --- |
| 1 | `rearrange(filtered_kv, "s x h d -> h s x d")` | `[N*cs, 2, H, D] → [H, N*cs, 2, D]` | 把头提到最前 |
| 2 | `.split(moba_chunk_size, dim=1)` | → N 个 `[H, cs, 2, D]` | 按块切开（token 轴本就是「块, 块内位置」打包的） |
| 3 | `torch.cat(..., dim=0)` | → `[N*H, cs, 2, D]` | 块与头交换优先级：段序变为 (块, 头) |

随后两步收尾（[L414](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L414)）：

| 步骤 | 代码 | 形状变化 | 效果 |
| --- | --- | --- | --- |
| 4 | `.flatten(0, 1)` | `[N'*cs, 2, D]` | 拼成 varlen 总长 |
| 5 | `.unsqueeze(2)` | `[N'*cs, 2, 1, D]` | 补头维=1，与 `moba_q [T, 1, D]` 配套 |

三步之后可以验证一个漂亮的对应关系：

\[ \text{moba\_kv}[nH+h,\; t,\; x,\; :] \;=\; \text{候选块 } n \text{ 第 } t \text{ 个 token 在头 } h \text{ 上的 } K/V \]

行号 `n*H + h` 与 `moba_seqlen_q.flatten()` 的下标完全同构——这不是巧合，而是 4.1 刻意选择「块优先」段顺序的原因。

#### 4.4.3 源码精读

[moba/moba_efficient.py:L388-L408](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L388-L408)——注释点明动机：Q 已经按 HS×N 组织，KV 必须重排适配：

```python
""" prepare moba kv """
# Since moba_q is organized as HS * N, we need to reorganize kv to adapt to q

# cut off zero experts
q_zero_mask = moba_seqlen_q == 0
valid_expert_mask = ~q_zero_mask
zero_expert_count = q_zero_mask.sum()
# only keep the kv that has q select > 0
if zero_expert_count > 0:
    moba_seqlen_q = moba_seqlen_q[valid_expert_mask]
# moba cu_seqlen for flash attn
moba_cu_seqlen_q = torch.cat(
    (
        torch.tensor([0], device=q.device, dtype=moba_seqlen_q.dtype),
        moba_seqlen_q.cumsum(dim=0),
    ),
    dim=0,
).to(torch.int32)
moba_kv = rearrange(filtered_kv, "s x h d -> h s x d")
moba_kv = moba_kv.split(moba_chunk_size, dim=1)
moba_kv = torch.cat(moba_kv, dim=0)
```

以及 [L414](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L414) 的收尾：

```python
moba_kv = moba_kv.flatten(start_dim=0, end_dim=1).unsqueeze(2)
```

调用处（[L107-L108](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L107-L108)）印证了 `2` 这根轴的语义：`k=moba_kv[:, 0]`、`v=moba_kv[:, 1]`，K 和 V 自始至终绑在同一根轴上搬运，保证下标永远同步。

（`split` 之所以能等长切分，是因为候选块全是满块——再次用到 u3-l2「剔除最后一块」的结论。零 expert 相关的四行先略过，4.5 专门讲。）

#### 4.4.4 代码实践

**实践目标**：用「身份证张量」验证三步重排后的段顺序确为 (块, 头)。

**操作步骤**（示例代码）：

```python
from einops import rearrange

cs, N, H = 4, 2, 2
# 值 = token 在 filtered_kv 中的全局行号（身份）
filtered_kv = torch.arange(N * cs, dtype=torch.float32).reshape(N * cs, 1, 1, 1)
filtered_kv = filtered_kv.repeat(1, 2, H, 1)            # [8, 2, 2, 1]

moba_kv = rearrange(filtered_kv, "s x h d -> h s x d")   # [2, 8, 2, 1]
moba_kv = moba_kv.split(cs, dim=1)                       # 2 × [2, 4, 2, 1]
moba_kv = torch.cat(moba_kv, dim=0)                      # [4, 4, 2, 1]
print(moba_kv.shape)
print(moba_kv[:, :, 0, 0])                               # 看每段每 token 的身份
```

**需要观察的现象与预期结果**（手工推演，待本地验证）：形状为 `[4, 4, 2, 1]`；打印值为

```text
tensor([[0., 1., 2., 3.],    # 段0 = C0H0：块0 的 token 0..3
        [0., 1., 2., 3.],    # 段1 = C0H1：块0 的 token 0..3（另一个头）
        [4., 5., 6., 7.],    # 段2 = C1H0：块1 的 token 0..3
        [4., 5., 6., 7.]])   # 段3 = C1H1：块1 的 token 0..3
```

即 `moba_kv[n*H + h, t, x, 0] = n*cs + t`，段序 (块, 头) 与 `moba_seqlen_q` 的展平顺序对齐。

#### 4.4.5 小练习与答案

**练习 1**：只做 `rearrange` 不做 `split + cat`，段顺序是什么？
**答案**：`[H, N*cs, 2, D]` 是「头优先」布局：第 0 行是头 0 的所有块。而 Q 侧段顺序是块优先，两侧配不上对，必须用 `split + cat` 交换 (头) 与 (块) 的优先级。

**练习 2**：最终 `moba_kv [N'*cs, 2, 1, D]` 里 `2` 和 `1` 各代表什么？
**答案**：`2` 是 K/V 栈维（`moba_kv[:, 0]` 是 K、`moba_kv[:, 1]` 是 V）；`1` 是头维——头已折叠进 varlen 批次维，与 `moba_q [T, 1, D]` 配套。

**练习 3**：为什么 KV 侧每段长度恰好等于 `moba_chunk_size`？
**答案**：因为 `calc_chunks` 已把每个 batch 的最后一块（唯一可能不满的块）剔除出候选集，剩下的候选块全是满块。这也是 4.5 中 `moba_cu_seqlen_kv` 能用等差数列构造的前提。

### 4.5 零 expert 裁剪、moba_cu_seqlen_kv 与两个形状断言

#### 4.5.1 概念说明

**零 expert（zero expert）**= `moba_seqlen_q` 中值为 0 的条目，即某个 (候选块, 头) 组合没有收到任何一个 query。它可能出现在：可选块很少的短序列、gate 分数极端分布、或 head 间选择差异大的场合。随机数据下不常见，但**必须防御**——因为它会制造「空段」：

- 若保留空段，`moba_cu_seqlen_q` 中会出现两个相邻相等的边界，对应一条 **q 长度为 0** 的 varlen「序列」，而其配对的 KV 段却有 `chunk_size` 个 token。
- 前向尚可容忍（空段不产出任何输出行），但反向时 flash-attn 要按段用保存的 LSE 重算 softmax 权重：一条没有 query 的段没有有效的 LSE/归一化因子，`exp(-inf − (-inf))` 之类未定义运算会把 NaN 写进该段 KV 的梯度，再随 `dmkv` 扩散。源码注释一锤定音：**"cut off zero Q expert from kv, or the grad may be nan"**（[L413](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L413)）。（flash-attn 内核对空段的具体行为属实现细节，此处为基于 varlen 语义的合理推断，标记待确认；教程侧只需记住结论：**两侧必须同步裁剪，保证没有空段**。）

裁剪必须**双侧同步**：Q 侧删掉 `moba_seqlen_q` 的零条目，KV 侧删掉 `moba_kv` 对应的段。两个 `assert` 就是这个「同步」的守护者。

#### 4.5.2 核心流程

1. 构造零掩码：`q_zero_mask = (moba_seqlen_q == 0)`，`valid_expert_mask = ~q_zero_mask`，`zero_expert_count = q_zero_mask.sum()`。
2. Q 侧：若有零 expert，`moba_seqlen_q = moba_seqlen_q[valid_expert_mask]`。
3. 构造 Q 侧边界：`moba_cu_seqlen_q = cat([0], cumsum(moba_seqlen_q))`，转 `int32`——标准 varlen 前缀和，**段长不定长**。
4. KV 侧重排（4.4），然后删掉零 expert 对应的段（行）。
5. 构造 KV 侧边界：

   \[ \text{moba\_cu\_seqlen\_kv}[i] \;=\; i \cdot \text{chunk\_size}, \quad i = 0, 1, \dots, N' \]

   其中 `N' = N*H − zero_expert_count`。因为**每个存活的 KV 段都恰好长 `chunk_size`**（候选块全是满块），边界就是一个纯等差数列，连前缀和都不用算——这是 u3-l2「过滤最后一块」在本文的第二次变现。
6. 两处断言兜底（见 4.5.3）。

#### 4.5.3 源码精读

Q 侧裁剪与 cu_seqlen 构造：[moba/moba_efficient.py:L391-L405](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L391-L405)——注意 `moba_cu_seqlen_q` 是在裁剪**之后**构造的，所以它只数非空段：

```python
# cut off zero experts
q_zero_mask = moba_seqlen_q == 0
valid_expert_mask = ~q_zero_mask
zero_expert_count = q_zero_mask.sum()
# only keep the kv that has q select > 0
if zero_expert_count > 0:
    moba_seqlen_q = moba_seqlen_q[valid_expert_mask]
# moba cu_seqlen for flash attn
moba_cu_seqlen_q = torch.cat(
    (
        torch.tensor([0], device=q.device, dtype=moba_seqlen_q.dtype),
        moba_seqlen_q.cumsum(dim=0),
    ),
    dim=0,
).to(torch.int32)
```

KV 侧裁剪（紧跟 4.4 的三步重排之后）：[moba/moba_efficient.py:L409-L414](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L409-L414)——按同一张 `valid_expert_mask` 删行，注释即 NaN 警告：

```python
if zero_expert_count > 0:
    assert valid_expert_mask.sum() == moba_kv.shape[0] - zero_expert_count
    moba_kv = moba_kv[
        valid_expert_mask
    ]  # cut off zero Q expert from kv , or the grad may be nan
moba_kv = moba_kv.flatten(start_dim=0, end_dim=1).unsqueeze(2)
```

KV 侧边界与形状断言：[moba/moba_efficient.py:L415-L428](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L415-L428)——等差数列的终点 deliberately 减去 `zero_expert_count`：

```python
moba_cu_seqlen_kv = (
    torch.arange(
        0,
        num_filtered_chunk * num_head + 1 - zero_expert_count,
        dtype=torch.int32,
        device=q.device,
    )
    * moba_chunk_size
)

# Shape check
assert (
    moba_cu_seqlen_kv.shape == moba_cu_seqlen_q.shape
), f"moba_cu_seqlen_kv.shape != moba_cu_seqlen_q.shape {moba_cu_seqlen_kv.shape} != {moba_cu_seqlen_q.shape}"
```

两个断言的分工：

| 断言 | 位置 | 校验内容 | 性质 |
| --- | --- | --- | --- |
| `valid_expert_mask.sum() == moba_kv.shape[0] - zero_expert_count` | [L410](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L410) | 裁剪前 KV 段数（此时 `moba_kv.shape[0] = N*H`）减零 expert 数应等于有效段数 | 按定义恒成立（`valid = ~zero`，两者相加恒为 `N*H`），属防御性自检，防止后续重构破坏这层关系 |
| `moba_cu_seqlen_kv.shape == moba_cu_seqlen_q.shape` | [L426-L428](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L426-L428) | 两侧 cu_seqlen 长度一致，即 **Q 侧删掉的段数与 KV 侧一致**（都应是 `N'+1`） | 真正「咬合」两条裁剪路径的一致性检查：若只删了一侧（比如忘了过滤 `moba_seqlen_q`），段数失配立刻报错，错误信息直接打印两个形状 |

最后，[L431-L443](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L431-L443) 把本讲构造的全部张量打包送进 `MixedAttention.apply`——那就是下一讲 u3-l5 的入口：

```python
return MixedAttention.apply(
    q, k, v, self_attn_cu_seqlen,
    moba_q, moba_kv, moba_cu_seqlen_q, moba_cu_seqlen_kv,
    max_seqlen, moba_chunk_size, moba_q_sh_indices,
)
```

#### 4.5.4 代码实践

**实践目标**：亲手触发一次零 expert 裁剪，验证两个 cu_seqlen 的形状咬合。

**操作步骤**（示例代码，接 4.1/4.3 的变量；把块 1 头 1 整段清空，制造一个零 expert）：

```python
gate_mask[1, 1, :] = False                 # C1H1 变成零 expert

moba_q_indices = gate_mask.reshape(gate_mask.shape[0], -1).nonzero(as_tuple=True)[-1]
moba_seqlen_q  = gate_mask.sum(dim=-1).flatten()
print(moba_q_indices, moba_seqlen_q)       # 期望 [2,5,7,9,11,4,6] 与 [3,2,2,0]

q_zero_mask      = moba_seqlen_q == 0
valid_expert_mask = ~q_zero_mask
zero_expert_count = int(q_zero_mask.sum()) # 期望 1

if zero_expert_count > 0:
    moba_seqlen_q = moba_seqlen_q[valid_expert_mask]
moba_cu_seqlen_q = torch.cat(
    (torch.tensor([0]), moba_seqlen_q.cumsum(dim=0))
).to(torch.int32)

# 模拟 4.4 的 KV 段（4 个段），再按同一张掩码裁剪
moba_kv = torch.arange(4 * 4, dtype=torch.float32).reshape(4, 4, 1, 1)  # 段×token××头
if zero_expert_count > 0:
    assert valid_expert_mask.sum() == moba_kv.shape[0] - zero_expert_count
    moba_kv = moba_kv[valid_expert_mask]

moba_cu_seqlen_kv = torch.arange(2 * 2 + 1 - zero_expert_count, dtype=torch.int32) * 4
assert moba_cu_seqlen_kv.shape == moba_cu_seqlen_q.shape
print(moba_cu_seqlen_q, moba_cu_seqlen_kv, moba_kv.shape)
```

**需要观察的现象与预期结果**（手工推演，待本地验证）：

```text
moba_q_indices = tensor([2, 5, 7, 9, 11, 4, 6])     # 只剩 7 份拷贝
moba_seqlen_q  = tensor([3, 2, 2, 0])               # C1H1 段长为 0 → 零 expert
moba_cu_seqlen_q = tensor([0, 3, 5, 7], dtype=torch.int32)
moba_cu_seqlen_kv = tensor([0, 4, 8, 12], dtype=torch.int32)
moba_kv.shape    = torch.Size([3, 4, 1, 1])         # 4 段裁成 3 段
```

两个 cu_seqlen 都是 `[4]`（3 个存活段 + 1），断言通过；配对关系变为：段 0 = C0H0 的 3 个 Q ↔ 块 0 头 0 的 4 个 KV，段 1 = C0H1 的 2 个 Q ↔ 块 0 头 1，段 2 = C1H0 的 2 个 Q ↔ 块 1 头 0。

**延伸实验（需 GPU 与 flash-attn，待本地验证）**：取 [tests/test_moba_attn.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py) 的调用方式构造一个小输入，在本地脚本副本中注释掉 [L409-L413](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L409-L413) 的 KV 侧裁剪并强制构造零 expert，观察 `.backward()` 后梯度是否出现 NaN——这是 u3-l6 的预习实验。

#### 4.5.5 小练习与答案

**练习 1**：`zero_expert_count = 3` 时，`moba_cu_seqlen_q` 与 `moba_cu_seqlen_kv` 的长度分别是多少？
**答案**：都是 `N*H − 3 + 1`。Q 侧由裁剪后的 `moba_seqlen_q`（长 `N*H−3`）加前导 0 得到；KV 侧由 `arange(0, N*H + 1 − 3)` 得到——这正是形状断言成立的原因。

**练习 2**：为什么 `moba_cu_seqlen_kv` 可以用等差数列构造，而 `moba_cu_seqlen_q` 必须做前缀和？
**答案**：KV 侧每个存活段长度恒为 `chunk_size`（候选块全是满块），边界天然是 `0, cs, 2cs, …`；Q 侧段长是「选中该块的 query 数」，各不相同，只能前缀和。

**练习 3**：如果把 [L396-L397](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L396-L397)（Q 侧对 `moba_seqlen_q` 的过滤）删掉，哪个断言会先报警？
**答案**：[L426-L428](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L426-L428) 的形状断言。此时 `moba_cu_seqlen_q` 长度为 `N*H + 1`，而 `moba_cu_seqlen_kv` 长度为 `N*H − zero_expert_count + 1`，两者失配（`zero_expert_count > 0` 时）。这也解释了断言报错信息为何把两个形状都打印出来——方便直接定位是哪一侧的裁剪出了问题。

## 5. 综合实践

把五个模块串成一个完整的「人肉执行 varlen trick」任务。**实践目标**：不看源码，仅凭 `gate_mask` 手工推导出送入 flash-attn 前的全部 8 个张量，再用脚本逐项核对。

**设定**：`N = 2` 个候选块、`H = 2` 个头、`S = 8` 个 token、`chunk_size = 4`，`gate_mask` 同 4.1；KV 侧用「身份证张量」代替真实 K/V。

**第 1 步（手工推演）**：在下表空格处填入期望值（形状与内容）：

| 张量 | 形状 | 内容（手工推演） |
| --- | --- | --- |
| `moba_q_indices` | `[10]` | `[2, 5, 7, 9, 11, 4, 6, 8, 12, 15]` |
| `moba_seqlen_q` | `[4]` | `[3, 2, 2, 3]` |
| `moba_q` | `[10, 1, D]` | 身份证 `[4, 10, 14, 3, 7, 8, 12, 1, 9, 15]` |
| `moba_q_sh_indices` | `[10]` | `[4, 10, 14, 3, 7, 8, 12, 1, 9, 15]` |
| `moba_kv`（裁剪前） | `[4, 4, 2, 1]` | 段序 C0H0/C0H1/C1H0/C1H1 |
| `moba_cu_seqlen_q` | `[5]` | `[0, 3, 5, 7, 10]` |
| `moba_cu_seqlen_kv` | `[5]` | `[0, 4, 8, 12, 16]` |
| 配对关系 | — | 段 0：3 个 Q ↔ 块 0 头 0；段 1：2 个 Q ↔ 块 0 头 1；…… |

注意 `moba_q` 的「身份证」与 `moba_q_sh_indices` 完全相同——这不是巧合，而是「拷贝自哪里，就合并回哪里」的直接体现。

**第 2 步（脚本核对，示例代码）**：

```python
# varlen_trick_toy.py —— 示例代码：复刻 moba_efficient.py L373-L428 的索引逻辑（CPU 即可运行）
import torch
from einops import rearrange

N, H, S, CS = 2, 2, 8, 4            # 候选块数 / 头数 / 总 token 数 / 块大小

# ① 手工构造 gate_mask（真实代码中来自 u3-l3 的 gate + topk）
gate_mask = torch.zeros(N, H, S, dtype=torch.bool)
gate_mask[0, 0, [2, 5, 7]] = True
gate_mask[1, 0, [4, 6]] = True
gate_mask[0, 1, [1, 3]] = True
gate_mask[1, 1, [0, 4, 7]] = True

# ② L375-377：选中清单
moba_q_indices = gate_mask.reshape(N, -1).nonzero(as_tuple=True)[-1]

# ③ L379 + L381-384：段长 + Q 拷贝（用展平下标当身份证）
moba_seqlen_q = gate_mask.sum(dim=-1).flatten()
q = torch.arange(S * H, dtype=torch.float32).reshape(S, H, 1)
moba_q = rearrange(q, "s h d -> ( h s ) d").index_select(0, moba_q_indices).unsqueeze(1)

# ④ L386：写回地址簿
moba_q_sh_indices = moba_q_indices % S * H + moba_q_indices // S

# ⑤ L391-405：零 expert 裁剪（Q 侧）+ cu_seqlen_q
q_zero_mask, valid_expert_mask = moba_seqlen_q == 0, ~(moba_seqlen_q == 0)
zero_expert_count = int(q_zero_mask.sum())
if zero_expert_count > 0:
    moba_seqlen_q = moba_seqlen_q[valid_expert_mask]
moba_cu_seqlen_q = torch.cat((torch.tensor([0]), moba_seqlen_q.cumsum(dim=0))).to(torch.int32)

# ⑥ L406-408 + L409-414：KV 重排 + 裁剪
filtered_kv = torch.arange(N * CS, dtype=torch.float32).reshape(N * CS, 1, 1, 1).repeat(1, 2, H, 1)
moba_kv = rearrange(filtered_kv, "s x h d -> h s x d")
moba_kv = torch.cat(moba_kv.split(CS, dim=1), dim=0)
if zero_expert_count > 0:
    assert valid_expert_mask.sum() == moba_kv.shape[0] - zero_expert_count
    moba_kv = moba_kv[valid_expert_mask]
moba_kv = moba_kv.flatten(0, 1).unsqueeze(2)

# ⑦ L415-428：cu_seqlen_kv + 形状断言
moba_cu_seqlen_kv = torch.arange(N * H + 1 - zero_expert_count, dtype=torch.int32) * CS
assert moba_cu_seqlen_kv.shape == moba_cu_seqlen_q.shape

for name, t in [("q_indices", moba_q_indices), ("seqlen_q", moba_seqlen_q),
                ("sh_indices", moba_q_sh_indices), ("cu_q", moba_cu_seqlen_q),
                ("cu_kv", moba_cu_seqlen_kv), ("kv", moba_kv)]:
    print(name, t.shape, t.flatten()[:16])
```

**第 3 步（观察与预期）**：对照第 1 步的表格逐项核对（本环境无法运行 Python，以上全部期望值为手工推演，**待本地验证**）。特别验证三个不变量：

1. `moba_q.shape[0] == moba_q_indices.shape[0] == moba_q_sh_indices.shape[0]`；
2. `moba_q_sh_indices` 与 `moba_q` 的身份证逐位相同；
3. `moba_cu_seqlen_q[-1] == moba_q.shape[0]`，`moba_cu_seqlen_kv[-1] == 存活段数 × CS`。

**第 4 步（制造零 expert）**：在脚本开头加一行 `gate_mask[1, 1, :] = False`，重跑并核对 4.5.4 中的预期值，确认两个断言在裁剪后依然通过、且若删除 Q 侧过滤则形状断言立刻失败。

**第 5 步（对照真实调用，源码阅读型实践）**：打开 [moba/moba_efficient.py:L105-L116](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L105-L116)，给玩具里的每个段标注它在真实前向中的身份：`max_seqlen_k=moba_chunk_size`（KV 段定长）、`causal=False`（因果已在选块阶段完成）、头数为 1（头已折叠进 varlen）。若本机有 GPU，可再运行 `pytest tests/test_moba_attn.py -k "seqlen512"` 抽查若干参数组合，确认真实路径行为与你的理解一致（待本地验证）。

## 6. 本讲小结

- **varlen trick 的本质**：把「每个 query 对每个选中块」的一次稀疏注意力，伪装成 varlen 批次里一条独立的**单头**短序列——头折叠进批次维，flash-attn 在毫不知情的情况下变成了块稀疏注意力引擎。
- `moba_q_indices` 由 `gate_mask.reshape(N, -1).nonzero()` 得到，行主排序天然给出「块优先、头次之」的分段结构；段长由 `moba_seqlen_q = gate_mask.sum(-1).flatten()` 单独记录。
- Q 允许重复拷贝（选几块拷几份），`moba_q_sh_indices = idx % S * H + idx // S` 把 (h s) 坐标换算回输出缓冲的 (s h) 展平坐标，是前向合并与反向搬运共用的地址簿。
- KV 侧用 `rearrange + split + cat` 三步把 (头) 与 (块) 的优先级对调，段序与 Q 侧严格同构；候选块全为满块使 `moba_cu_seqlen_kv` 退化为等差数列。
- 零 expert（没有 query 选中的块×头段）必须从 Q、KV **两侧同步**裁剪，否则空段会让反向梯度出现 NaN（源码注释明示）；两个 `assert` 分别做防御性自检与两侧段数一致性检查。

## 7. 下一步学习建议

本讲结束时，`MixedAttention.apply` 的全部输入都已就位，但留了一个悬而未决的问题：**同一个 query 的多份拷贝各自算出了局部注意力，怎么合并成一个正确的全局结果？** 这正是下一讲 u3-l5（MixedAttention 前向：两路注意力与 LSE 在线合并）的主题——`self_attn` 支路、`moba` 支路如何用 log-sum-exp 数学精确融合，`moba_q_sh_indices` 如何充当合并地址。之后再进入 u3-l6 看反向如何复用 `moba_q_sh_indices` 与混合 LSE。建议阅读顺序：先精读 [L88-L168](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L88-L168)（前向），带着本讲的玩具段配对图去对照每一次 `index_select / index_reduce / index_add_` 的落点。
