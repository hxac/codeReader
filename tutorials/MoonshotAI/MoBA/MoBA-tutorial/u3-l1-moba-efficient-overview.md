# moba_attn_varlen 总览：从 naive 到高效实现的四步流程

## 1. 本讲目标

本讲是「核心单元」的第一讲，从高空俯瞰 `moba/moba_efficient.py` 中的 `moba_attn_varlen`——生产环境真正使用的 MoBA 实现。读完本讲，你应该能够：

1. 建立高效实现的**四步心智模型**：算 chunk 元数据 → gate 打分选块 → varlen 重组 → LSE 合并，并能把它映射到源码的具体行区间。
2. 说清两个最容易被问「为什么」的设计：**为什么每个 batch 的最后一块被保留给 self-attention 支路、因此 `moba_topk` 要减一**；以及**为什么 `need_moba_attn` 为假时可以直接退化为 `flash_attn_varlen_func` 全量自注意力**。
3. 理解 `MixedAttention.apply` 这一次调用里传了哪些东西、每一路注意力各自用什么 varlen 边界和因果设置（细节留给 u3-l5/u3-l6）。
4. 画出完整流程图并标注每一步张量形状，为后续五讲逐段精读建立地图。

本讲只做**总览**，刻意不深挖 `calc_chunks` 的索引推导（u3-l2）、gate 掩码（u3-l3）、varlen 重组（u3-l4）、LSE 合并与反向（u3-l5/u3-l6）的实现细节。

## 2. 前置知识

本讲假设你已读完 u1-l3（张量布局与 varlen）和 u2-l1（naive 实现精读）。这里回顾并补充四个概念。

### 2.1 naive 实现的语义基线（来自 u2-l1）

naive 版本对每个 query 的注意力范围是：

- **当前块必选**：query 所在块被置 `+inf`，永远在 top-k 名单里（块内还要叠 token 级因果下三角掩码）；
- **未来块必不选**：`-inf` 排除；
- **其余历史块凭 gate 分数竞选**，总共选 `moba_topk` 个块（含当前块）。

因此「用户口味的 `moba_topk`」是**包含当前块在内的总块数**，自由选择的预算其实是 `moba_topk - 1`。这一点是理解本讲 topk 调整的关键。

### 2.2 varlen 约定回顾（来自 u1-l3）

- Q/K/V 布局为 `[S, H, D]`（S 是整个批次打包后的总 token 数）；
- `cu_seqlens` 是长度 `batch+1` 的前缀和，标记各序列边界；`flash_attn_varlen_func` 以它划分序列；
- 同一个 flash-attn 内核，**换一套 `cu_seqlens` 就能表达完全不同的注意力结构**——这是高效实现全部魔法的物理基础。

### 2.3 什么是 LSE（log-sum-exp）

flash-attn 内核对每条 varlen「序列」返回两个东西：注意力输出 `out`，以及打分指数和的对数：

\[
\mathrm{lse} = \log \sum_{i} e^{s_i}
\]

其中 \(s_i\) 是该 query 对自己那段序列内所有 key 的打分。有了每段的 `out` 和 `lse`，两段部分注意力的结果可以**精确合并**成「把两段 key 拼在一起做 softmax」的结果（数学推导见 4.4 节，实现细节见 u3-l5）。这叫 online softmax / LSE 合并。

### 2.4 `torch.autograd.Function` 是什么

PyTorch 的自动微分靠记录算子计算图。但本讲会看到，高效实现里充满了 `index_select`、`nonzero`、`scatter_` 这类**为前向服务的索引重组**——对它们自动求导既不必要也容易出错。解决办法是写一个 `torch.autograd.Function` 子类（本项目中的 `MixedAttention`），手工实现 `forward` 和 `backward`，把「两次 flash-attn 前向 + LSE 合并」整体封装成一个可微分算子。反向的推导在 u3-l6，本讲只需要知道这个封装的存在和它的调用接口。

### 2.5 依赖锁定的原因（来自 u1-l2）

[moba/moba_efficient.py:5-9](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L5-L9) 从 `flash_attn.flash_attn_interface` 导入了**带下划线的私有 API** `_flash_attn_varlen_forward` 和 `_flash_attn_varlen_backward`（公有 API `flash_attn_varlen_func` 不返回反向所需的中间量）。这就是 `flash-attn==2.6.3` 被精确锁定的原因。

## 3. 本讲源码地图

本讲的主角是一个文件，配角是两个参照物：

| 文件 | 角色 | 本讲关注 |
|---|---|---|
| `moba/moba_efficient.py` | 高效实现（本讲主角） | 整体骨架：`calc_chunks`（L14-L64）、`MixedAttention`（L67-L267）、`moba_attn_varlen`（L270-L443） |
| `moba/moba_naive.py` | 黄金参考 | topk 语义对照（L58-L66） |
| `tests/test_moba_attn.py` | 正确性验证 | `moba_attn_varlen` 的调用方式与参数组合（L37-L62） |

`moba/moba_efficient.py` 的三大块布局：

```text
moba/moba_efficient.py
├── calc_chunks()          L14-L64    chunk 元数据：分块、跨 batch 索引、过滤最后一块
├── class MixedAttention   L67-L267   自定义 autograd.Function：两路前向 + LSE 合并 + 两路反向
└── moba_attn_varlen()     L270-L443  主入口：四步流程的编排
```

## 4. 核心概念与源码讲解

### 4.1 moba_attn_varlen 主流程：四步心智模型

#### 4.1.1 概念说明

naive 实现把稀疏注意力写成「稠密打分矩阵 + 加性掩码 + 完整 softmax」，计算量仍是 \(O(S^2)\)——它为了可读性牺牲了性能，不能上生产。高效实现的本质是换一种**表达方式**而不是换算法：

> **把「每个 query 各自看不同块」的稀疏注意力，改写成若干个规整的小规模 varlen 稠密注意力，全部交给 flash-attn 内核计算，再用 LSE 把两路结果数学精确地拼回去。**

函数 docstring 把它总结为四步（[moba/moba_efficient.py:279-286](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L279-L286)）：

1. **算 chunk 元数据**：把每条序列按 `moba_chunk_size` 分块；每个 batch 的**最后一块保留给 self-attention 支路**，其余块进入后续步骤；
2. **gate 打分**：块内 K 均值作为代表向量，与 Q 内积得到 gate logit，据此为每个（query, head）做 top-k 选块；
3. **varlen 重组**：把所有「query ↔ 被选块」配对拼成一条变长序列，做 varlen 注意力；
4. **LSE 合并**：用 online softmax 把 moba 支路和 self-attention 支路的结果融合成最终输出。

一个小提醒：docstring 里写 `n = floor(data_size / chunk_size)`，是单 batch 整除情形下的简化说法；代码实际对**每个 batch 分别**取 `ceil(batch_size / chunk_size)`（见 u3-l2）。另外 docstring 提到 "triton kernels"，但当前实现的前向反向计算完全由 flash-attn 内核承担，仓库中没有独立的 triton kernel（这一点在 u4-l4 复盘）。

#### 4.1.2 核心流程

用一张文字流程图表示主流程（含两条支路）。记 `S` 为打包总长、`B` 为 batch 数、`C` 为总块数、`F` 为过滤后块数、`H` 为头数：

```text
输入: q,k,v [S,H,D], cu_seqlens [B+1], max_seqlen, moba_chunk_size, moba_topk
  │
  ├─ 步骤0  kv = torch.stack((k, v), dim=1)            → kv [S, 2, H, D]
  │
  ├─ 步骤1  calc_chunks(cu_seqlens, moba_chunk_size)
  │         → cu_chunk [C+1], filtered_chunk_indices [F],
  │           num_filtered_chunk, chunk_to_batch [C]
  │
  ├─ 步骤1.5  moba_topk = min(moba_topk - 1, F)
  │           need_moba_attn = moba_topk > 0
  │           └─ 若为假 ──► flash_attn_varlen_func(q,k,v, cu_seqlens, causal=True)
  │                          直接返回全量因果自注意力（兜底分支）
  │
  ├─ 步骤2  filtered_kv gather → gate（K 均值·Q）→ 因果/batch 掩码 → topk
  │         → gate_mask [F, H, S]（True = 该(块,头)被该 query 选中）
  │
  ├─ 步骤3  varlen trick：按 (块 × 头) 收集被选中的 q
  │         → moba_q [N, 1, D], moba_kv [F·H·chunk, 2, 1, D],
  │           moba_cu_seqlen_q / moba_cu_seqlen_kv
  │
  └─ 步骤4  MixedAttention.apply(q, k, v, self_attn_cu_seqlen, moba_q, moba_kv, ...)
             ├─ 支路A self-attn: (q,k,v), varlen 边界 = cu_chunk, causal=True
             ├─ 支路B moba-attn: (moba_q, moba_kv), 边界 = moba_cu_seqlens, causal=False
             └─ LSE 合并 → output [S, H, D]
```

两条支路的分工（这是本单元最重要的一句话）：

- **支路 A（块内）**：以**块边界** `cu_chunk` 为 varlen 边界，每个块内部做因果自注意力。任何 query 对「自己所在块内、位置不晚于自己」的 token 的注意力全由它负责——这正是 naive 里「当前块必选」的物化。
- **支路 B（跨块）**：query 对**历史整块**的注意力。gate 的因果掩码保证被选中的块整体都在 query 之前，所以这条支路可以放心用 `causal=False` 的非因果注意力。

#### 4.1.3 源码精读

主入口签名与 docstring：

[moba/moba_efficient.py:270-297](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L270-L297) 定义 `moba_attn_varlen(q, k, v, cu_seqlens, max_seqlen, moba_chunk_size, moba_topk)`，入参出参形状与 naive 完全一致（`[S,H,D]` 进、`[S,H,D]` 出）——这是「两者可互相替换、互为黄金参考」的接口前提。docstring 的四步描述就在 L279-L286。

函数体按四步分段（行号基于当前 HEAD `b5d5836`）：

| 步骤 | 行区间 | 干什么 |
|---|---|---|
| 步骤 0 | [L299-L303](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L299-L303) | `kv = torch.stack((k, v), dim=1)`；解包 `seqlen, num_head, head_dim` |
| 步骤 1 | [L305-L323](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L305-L323) | 调 `calc_chunks` 拿元数据；topk 减一调整；兜底分支；`self_attn_cu_seqlen = cu_chunk` |
| 步骤 2 | [L325-L371](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L325-L371) | gather 出 `filtered_kv`；算 `key_gate_weight`（块内 K 均值）与 `gate`；因果与 batch 掩码；topk 选块得 `gate_mask` |
| 步骤 3 | [L373-L428](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L373-L428) | varlen trick：`moba_q_indices` / `moba_q` / `moba_q_sh_indices` / `moba_kv` / 两个 `moba_cu_seqlen`，含零 expert 裁剪 |
| 步骤 4 | [L430-L443](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L430-L443) | 一次性调用 `MixedAttention.apply` 完成两路前向、LSE 合并与反向封装 |

测试入口（怎么被调用）：[tests/test_moba_attn.py:54-62](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L54-L62) 中 `moba_attn_varlen(q, k, v, cu_seqlen, max_seqlen, moba_chunk_size=..., moba_topk=...)` 的调用方式，以及 L37-L42 的参数化网格（batch/head/seqlen/chunk/topk 的组合）——每个组合都会与 naive 对比输出和梯度。

#### 4.1.4 代码实践

**实践目标**：把 docstring 的四步描述与真实代码段一一对上，建立「读文字能定位代码」的肌肉记忆。

**操作步骤**：

1. 打开 [moba/moba_efficient.py:279-286](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L279-L286)，阅读四步 docstring。
2. 依次跳转到 4.1.3 表格中的五个行区间，在每段开头用注释笔标记 `# step 0` … `# step 4`（在本地副本或纸上做，不要改仓库文件）。
3. 对照 4.1.2 的流程图，检查每个中间变量的形状：`kv`、`cu_chunk`、`filtered_chunk_indices`、`gate`、`gate_mask`、`moba_q`、`moba_kv`。

**需要观察的现象**：你会发现函数体几乎没有「真正的算术」，除了一个 `einsum`（gate 打分）和一次 `mean`（块内 K 均值），其余全是索引操作和一次 `MixedAttention.apply`——计算密度极低，这正是「编排层 + 内核层」分层的典型形态。

**预期结果**：能不回头看讲义，说出四步各自的行区间和核心输出张量。

#### 4.1.5 小练习与答案

**练习 1**：naive 实现有双重循环（batch 循环 + 块循环），高效实现一个 Python 循环都没有。它靠什么把「按块」的逻辑做掉了？

**答案**：靠张量索引操作向量化。分块信息被编码进 `cu_chunk` 等**整数索引张量**（`calc_chunks` 一次性算好），块的收集用 `index_select`，块内求均值用 `view(...).mean(dim=1)`，块选择用 `topk` + `scatter_`，最后把「每个 query 看哪些块」编码成新的 `cu_seqlens` 交给 flash-attn。循环消失了，但语义被完整保留。

**练习 2**：为什么支路 B（moba 支路）可以用 `causal=False`？请从 gate 掩码的角度回答。

**答案**：因为 gate 的因果掩码（`gate_chunk_end_mask`，L357）保证：一个 query 能选中的块必须**整块结束在 query 位置之前**（`gate_seq_idx >= chunk_end` 才不被屏蔽）。既然被选块里所有 token 都严格在 query 之前，块内就不存在「未来 token」需要屏蔽，非因果注意力是安全的。（对照 naive：被选的历史块在 token 级掩码里也是整列放开，只有当前块需要 tril。）

**练习 3**：`MixedAttention` 为什么必须手工写 backward，而不能让 PyTorch 自动微分？

**答案**：前向里为了组织数据做了大量索引重组（`index_select`/`nonzero`/`index_add_`），且两次 `_flash_attn_varlen_forward` 是私有 API，不参与自动微分图；同时合并输出的数学形式（LSE 加权）虽然可导，但直接复用 flash-attn 自带的 `_flash_attn_varlen_backward` 内核求两路梯度远比重建计算图高效（推导见 u3-l6）。

### 4.2 kv 堆叠：把 K 和 V 打包成一个张量

#### 4.2.1 概念说明

主函数第一行实际代码是：

```python
kv = torch.stack((k, v), dim=1)
```

它把两个 `[S, H, D]` 张量沿新维度 1 堆叠成 `[S, 2, H, D]`。这么做的原因：**后续所有「按块搬运 KV」的索引操作只需做一次，K 和 V 永远同步移动**。块稀疏注意力的核心开销之一就是「把选中的 KV 挑出来」，如果 K、V 分开挑，索引操作和显存搬运都要翻倍；堆叠后一次 `index_select` 同时搬走 K 和 V。

这个堆叠还决定了数据在两处的最终形态：

- moba 支路的输入 `moba_kv` 是 `[段数×chunk_size, 2, 1, D]`，前向里 `moba_kv[:, 0]` 当 K、`moba_kv[:, 1]` 当 V（[L107-L108](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L107-L108)）；
- 反向结束时 `dmkv = torch.stack((dmk, dmv), dim=1)`（[L266](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L266)），梯度以同样的堆叠形态返回给 `autograd.Function` 的返回值约定，前后对称。

#### 4.2.2 核心流程

```text
k [S,H,D] ─┐
           ├─ stack(dim=1) ─► kv [S,2,H,D] ─► index_select(按块gather) ─► filtered_kv [F·chunk,2,H,D]
v [S,H,D] ─┘                                                                    （细节在 u3-l3）
                                                              └─► 重排成 moba_kv（细节在 u3-l4）
```

#### 4.2.3 源码精读

[moba/moba_efficient.py:299-303](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L299-L303)：`kv = torch.stack((k, v), dim=1)` 得到 `[S, 2, H, D]`，并解包出 `seqlen, num_head, head_dim` 三个基本量（注释明确写了 `qkv shape = [ S, H, D ]`）。

[moba/moba_efficient.py:325-L330](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L325-L330)：堆叠的第一次受益——用 `filtered_kv_indices` 一次 `index_select` 把所有候选块的 K、V 整体搬出，得到稠密的 `filtered_kv`。注释写明它是 "a dense matrix that only contains filtered chunk of kv"。

[moba/moba_efficient.py:105-L116](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L105-L116)：堆叠的第二次受益——moba 支路把 `moba_kv[:, 0]` 和 `moba_kv[:, 1]` 直接切出来作为 K、V 传给 flash-attn，无需两次 gather。

[moba/moba_efficient.py:266-L267](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L266-L267)：反向的对称返回——`dmkv = torch.stack((dmk, dmv), dim=1)`，与输入 `moba_kv` 的堆叠布局一一对应。

#### 4.2.4 代码实践

**实践目标**：在纯 CPU 上手感 `stack + index_select` 的形状语义（不需要 GPU 和 flash-attn）。

**操作步骤**（示例代码，非仓库文件）：

```python
import torch

S, H, D, chunk = 6, 2, 4, 3
k = torch.arange(S * H * D, dtype=torch.float32).reshape(S, H, D)
v = k + 1000.0
kv = torch.stack((k, v), dim=1)          # [S, 2, H, D]，对应 L299
print(kv.shape)                           # torch.Size([6, 2, 2, 4])

# 模拟 L326-L330：gather 出第 0 块（chunk_size=3）的 K、V
block0_idx = torch.arange(0, chunk)
kv_block0 = kv.index_select(0, block0_idx)   # [3, 2, H, D]
print(kv_block0[:, 0].shape)                 # [3, H, D] —— 块内 K
print(torch.equal(kv_block0[:, 0], k[:3]))   # True
print(torch.equal(kv_block0[:, 1], v[:3]))   # True —— K、V 一次搬运、天然同步
```

**需要观察的现象**：`index_select` 一次调用后，块的 K 和 V 同时就位；`kv_block0[:, 0]`、`kv_block0[:, 1]` 分别与 `k[:3]`、`v[:3]` 完全相等。

**预期结果**：输出两个 `True`。若把 `stack` 的 `dim` 改成 0 或 2，后续 `[:, 0]` 的含义就变了——体会 `dim=1` 这个选择是为了让「token 维仍在第 0 维、K/V 在第 1 维」方便按块 gather 与按 `[:, 0]/[:, 1]` 切分。

#### 4.2.5 小练习与答案

**练习 1**：`torch.stack((k, v), dim=1)` 与 `torch.cat((k, v), dim=1)` 有什么区别？

**答案**：`stack` 沿**新建的**维度拼接，输出多一维：`[S, 2, H, D]`；`cat` 沿已有维度拼接，输出 `[S, 2H, D]`。后者会把 K、V 混进同一维，之后无法用 `[:, 0]` 干净切分，也破坏了 head 维的语义。

**练习 2**：反向里为什么 `dmkv` 要 `stack` 回 `[*, 2, *, D]` 而不是分开返回 `dmk`、`dmv`？

**答案**：`autograd.Function.backward` 的返回值必须与 `forward` 的**输入参数**逐一对齐。`forward` 的第 6 个输入是堆叠的 `moba_kv`（[L77](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L77)），所以梯度也必须是同形态的堆叠张量，PyTorch 才能把它进一步还原成对原始 `k`、`v` 的梯度。

### 4.3 moba_topk 调整：最后一块保留给 self-attention

#### 4.3.1 概念说明

这是本讲两个核心「为什么」之一。高效实现里，每个 query 的注意力被拆成两路，**当前块（块内因果部分）永远由 self-attn 支路负责**，等价于 naive 里「当前块必选」。因此：

1. **跨块自由选择的预算是 `moba_topk - 1`**。源码注释原话："we will adjust selective topk to moba_topk - 1, as the last chunk is always chosen"（[L313](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L313)）。
2. 与 naive 的一致性：naive 用 `k = min(moba_topk, num_block)` 选**含当前块在内**的 `moba_topk` 块（[moba/moba_naive.py:62-66](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L62-L66)）；高效实现选 `moba_topk - 1` 个历史块 + 块内自注意力，两者总数相同、语义对齐——这是测试能把两版输出对齐的前提。

那为什么 `calc_chunks` 还要把**每个 batch 的最后一块**从候选集里剔除（`filtered_chunk_indices`）？由因果规则可以推出：**一个 batch 的最后一块永远不会被任何 query「自由选中」**——

- 同 batch 内的 query 位置都小于该块的结束位置（batch 末尾），被 `gate_chunk_end_mask` 屏蔽；
- 其他 batch 的 query 被 `gate_batch_end_mask` 屏蔽（batch 之间不许注意）。

既然它在候选集里永远是 `-inf`，剔除它只是省掉无用的 gate 计算与无效的 KV 段，**不改变语义**。docstring 把这一点表述为 "tokens in the tail chunk are reserved for self attn"（尾部块的 token 由 self-attn 支路处理）。至于「每个块内」的注意力本来就由 self-attn 支路覆盖（varlen 边界是全部块边界 `cu_chunk`，不只是最后一块），这一点在 4.4 节展开。

#### 4.3.2 核心流程

```text
moba_topk（用户参数，含当前块的总选块数）
    │
    ├─ num_filtered_chunk = C - B        （总块数减 batch 数：每 batch 剔除最后一块）
    │
    ├─ moba_topk = min(moba_topk - 1, num_filtered_chunk)
    │
    ├─ need_moba_attn = moba_topk > 0
    │      │
    │      ├─ False ──► flash_attn_varlen_func(q, k, v, cu_seqlens, ..., causal=True)
    │      │            兜底：退化为全量因果自注意力，直接返回
    │      └─ True  ──► 继续走 MoBA 主路径
    │
    └─ self_attn_cu_seqlen = cu_chunk    （self-attn 支路的 varlen 边界 = 块边界）
```

触发兜底的两种典型情形：

- `moba_topk == 1`：减一后为 0，没有跨块预算；
- `num_filtered_chunk == 0`：每条序列都不超过一个块（如 `seqlen=512, chunk_size=1024, batch=1`），无历史块可选。

#### 4.3.3 源码精读

[moba/moba_efficient.py:305-L315](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L305-L315)：调用 `calc_chunks` 拿到四个元数据；然后是本模块的核心三行——

```python
# we will adjust selective topk to moba_topk - 1, as the last chunk is always chosen
moba_topk = min(moba_topk - 1, num_filtered_chunk)
need_moba_attn = moba_topk > 0
```

`min` 的第二参数防止 top-k 超过候选块总数（`torch.topk` 要求 `k <= 维度大小`）。

[moba/moba_efficient.py:317-L321](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L317-L321)：兜底分支。`need_moba_attn` 为假时直接返回 `flash_attn_varlen_func(q, k, v, cu_seqlens, cu_seqlens, max_seqlen, max_seqlen, causal=True)`——**用原始的 batch 级 `cu_seqlens` 做全序列因果注意力**，与 u1-l2 讲过的「短序列退化为全量注意力」现象同一来源。

[moba/moba_efficient.py:323](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L323)：`self_attn_cu_seqlen = cu_chunk`——self-attn 支路拿块边界当 varlen 边界，这一行是「两路分工」的落地点。

对照 naive 的 topk 语义：[moba/moba_naive.py:58-L66](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L58-L66) 先做 `+inf/-inf` 因果修正（当前块必选），再 `torch.topk(gate, k=min(moba_topk, num_block), ...)`——k 里含被 `+inf` 钉死的当前块。

#### 4.3.4 代码实践

**实践目标**：手工推导三种配置下的 chunk 元数据与 topk 调整结果，验证对兜底分支的判断。

**操作步骤**：

1. 对下表三行，先**合上讲义**手工计算 `C`（总块数）、`F = C - B`（过滤后块数）、调整后的 `moba_topk`、`need_moba_attn`，并预测走哪条分支：

| 配置 | B | seqlen | chunk_size | 用户 topk | C | F | 调整后 topk | 分支 |
|---|---|---|---|---|---|---|---|---|
| A | 1 | 1024 | 256 | 1 | ? | ? | ? | ? |
| B | 1 | 1024 | 256 | 3 | ? | ? | ? | ? |
| C | 1 | 512 | 1024 | 3 | ? | ? | ? | ? |

2. （可选，GPU 环境）分别用配置 A 和 B 跑一次（示例代码）：

```python
import torch
from moba.moba_efficient import moba_attn_varlen
from flash_attn import flash_attn_varlen_func

S, H, D = 1024, 2, 128
q, k, v = (torch.randn(S, H, D, dtype=torch.bfloat16, device="cuda") for _ in range(3))
cu = torch.tensor([0, S], dtype=torch.int32, device="cuda")
o_moba = moba_attn_varlen(q, k, v, cu, S, moba_chunk_size=256, moba_topk=1)
o_full = flash_attn_varlen_func(q, k, v, cu, cu, S, S, causal=True)
print((o_moba - o_full).abs().max())   # 预期为 0 或 bf16 舍入级别
```

**需要观察的现象**：配置 A 输出与全量因果自注意力逐元素一致（两者调用的是同一个内核、同一套参数）；配置 B 走 MoBA 主路径，输出与全量注意力有可见差异。

**预期结果**：表格答案——A：C=4、F=3、调整后 0、走兜底；B：C=4、F=3、调整后 2、走 MoBA；C：C=1、F=0、调整后 0、走兜底。数值对比部分**待本地验证**（需要 GPU 与 flash-attn==2.6.3 环境）。

#### 4.3.5 小练习与答案

**练习 1**：batch=4、各序列都恰好是 2 个块，`moba_topk=3`。求 `C`、`F`、调整后 topk。

**答案**：`C = 4 × 2 = 8`；每个 batch 剔除最后一块，`F = 8 - 4 = 4`；调整后 `min(3 - 1, 4) = 2`。

**练习 2**：`moba_topk=1` 时，高效实现退化为**全序列**因果自注意力；但 naive 实现在 `moba_topk=1` 时每个 query 只选「当前块」、只能看到自己块内的 token（可从 [moba/moba_naive.py:58-L66](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L58-L66) 的 `+inf` 规则推出）。两版本在 `topk=1` 这个点上语义并不相同，为什么测试没有暴露？

**答案**：因为 [tests/test_moba_attn.py:42](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L42) 的参数化网格里 `moba_topk` 只取 `{2, 3, 4}`，从未覆盖 1；两版的等价性只在 `topk >= 2` 时被测试担保。这个边界差异是从两段源码各自的行为推出的（源码推断，数值上**待本地验证**）。它也解释了为什么兜底分支选择「全量注意力」而非「块内注意力」：`topk=1` 被实现视为「没有跨块预算」，直接回到全量，与 README 强调的「全量/稀疏无缝切换」气质一致。

**练习 3**：为什么不把兜底写成「每个块独立做因果自注意力」（即仍用 `cu_chunk` 边界），而要回到 batch 级全量注意力？

**答案**：若 `topk=1` 时只保留块内注意力，每个 query 将完全看不到任何历史块，信息流被块边界切断，模型质量会严重受损；回到全量因果注意力则保证「至少不差于全量」的下界。此外全量路径只有一次内核调用，比「块内 + 空的 moba 支路」更简单高效。

### 4.4 MixedAttention 调用：两路注意力与 LSE 合并总览

#### 4.4.1 概念说明

主函数的最后一步把所有准备好的材料一次性交给 `MixedAttention.apply`。这个自定义算子的 `forward`（[L69-L183](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L69-L183)）做三件事：

1. **支路 A（self-attn）**：对原始 `q, k, v` 调 `_flash_attn_varlen_forward`，varlen 边界用 `self_attn_cu_seqlen = cu_chunk`、`causal=True`——**每个块是一条独立「序列」，块内因果**。注意边界是**全部块边界**，所以任何 query 对自己块内前缀的注意力都由这条路负责，而不只是「最后一块」。
2. **支路 B（moba-attn）**：对重组后的 `moba_q, moba_kv` 调同一内核，边界用 `moba_cu_seqlen_q/kv`、`causal=False`——每个「(被选块, 头)」段是一条序列，段内 query 对整块非因果注意。
3. **LSE 合并**：两路各自是「部分 key 集合上的 softmax」，用各自的 LSE 把它们加权相加成「全集合 softmax」。

合并的数学（细节与稳定性技巧在 u3-l5）：设某 query 的 key 全集被分成 \(A\)（自身块内因果部分）与 \(B\)（选中的历史块），内核分别给出

\[
\mathrm{out}_A = \frac{\sum_{i \in A} e^{s_i} v_i}{Z_A},\quad \mathrm{lse}_A = \log Z_A,\qquad Z_A = \sum_{i \in A} e^{s_i}
\]

（\(B\) 同理）。那么对 \(A \cup B\) 的完整 softmax 输出为

\[
\mathrm{out} = e^{\mathrm{lse}_A - \mathrm{lse}}\,\mathrm{out}_A + e^{\mathrm{lse}_B - \mathrm{lse}}\,\mathrm{out}_B,
\qquad \mathrm{lse} = \log\!\left(e^{\mathrm{lse}_A} + e^{\mathrm{lse}_B}\right)
\]

即：**合并输出 = 两路输出按 \(e^{\mathrm{lse}-\text{各自 lse}}\) 的归一化权重相加**。代码里先减去两路 LSE 的最大值再 `exp`，防止上溢（[L130-L149](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L130-L149)，逐行精读留待 u3-l5）。

#### 4.4.2 核心流程

```text
MixedAttention.apply(q, k, v, self_attn_cu_seqlen, moba_q, moba_kv,
                     moba_cu_seqlen_q, moba_cu_seqlen_kv, max_seqlen,
                     moba_chunk_size, moba_q_sh_indices)
  │
  ├─ 支路A: _flash_attn_varlen_forward(q,k,v; cu=cu_chunk, causal=True)
  │         → self_attn_out_sh, self_attn_lse_hs
  ├─ 支路B: _flash_attn_varlen_forward(moba_q,moba_kv[:,0],moba_kv[:,1];
  │                                     cu=moba_cu_seqlens, causal=False)
  │         → moba_attn_out, moba_attn_lse_hs
  │
  ├─ LSE 合并: max_lse → 指数权重 factor → 加权相加（index_reduce / index_add）
  └─ 返回 output [S,H,D]；save_for_backward 存反向所需张量
```

#### 4.4.3 源码精读

[moba/moba_efficient.py:431-L443](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L431-L443)：主函数唯一的「计算」调用——`MixedAttention.apply` 的 11 个参数及形状：

| 参数 | 来源 | 形状 | 用途 |
|---|---|---|---|
| `q, k, v` | 原始输入 | `[S,H,D]` | 支路 A 的三件套 |
| `self_attn_cu_seqlen` | `= cu_chunk`（L323） | `[C+1]` | 支路 A 的 varlen 边界（块边界） |
| `moba_q` | L381-L384 | `[N, 1, D]`（N=被选中 query 数） | 支路 B 的 Q（单头化） |
| `moba_kv` | L406-L414 | `[F·H·chunk, 2, 1, D]` | 支路 B 的 K/V（每个(块,头)段恰 chunk 个 token） |
| `moba_cu_seqlen_q` / `_kv` | L399-L423 | `[F·H+1]`（等差、公差 chunk） | 支路 B 的 varlen 边界 |
| `max_seqlen` | 入参 | int | 内核启动参数 |
| `moba_chunk_size` | 入参 | int | 反向时作支路 B 的 `max_seqlen_k` |
| `moba_q_sh_indices` | L386 | `[N]` | 把支路 B 的输出散射回原始 `[S,H]` 位置的映射 |

[moba/moba_efficient.py:89-L102](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L89-L102)：支路 A 的前向——`cu_seqlens_q = cu_seqlens_k = self_attn_cu_seqlen`、`causal=True`。每个块内部是一条约 `chunk_size` 长的短序列，flash-attn 按 64/128 行的分块流水线处理，无需 \(S^2\) 显存。

[moba/moba_efficient.py:105-L116](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L105-L116)：支路 B 的前向——`causal=False`，`max_seqlen_k = moba_chunk_size`；K、V 直接从堆叠的 `moba_kv` 切片。

[moba/moba_efficient.py:118-L168](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L118-L168)：LSE 合并的落点——`index_reduce` 取两路 LSE 的逐元素最大值（L132-L135）、减最大值后 `exp` 相加再 `log` 得混合 LSE（L136-L149）、两路输出各乘 `factor = exp(自身lse - 混合lse)` 后累加进输出缓冲（L151-L166）。本讲只需对照 4.4.1 的公式认出这三个阶段。

[moba/moba_efficient.py:169-L181](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L169-L181)：`ctx.save_for_backward` 保存反向所需的 11 个张量（合并后的 `output` 与 `mixed_attn_lse_sh` 是反向复用内核的关键，见 u3-l6）。

#### 4.4.4 代码实践

**实践目标**：给 `MixedAttention.apply` 的 11 个参数各标注形状与用途，检验对本讲的综合理解。

**操作步骤**：

1. 取一组具体数字：`batch=2`（序列长 640 与 384）、`chunk_size=256`、`H=2`、`D=128`、`moba_topk=3`。
2. 手工计算：`S`、`C`、`F`、`cu_chunk`、调整后 topk、`moba_q` 与 `moba_kv` 的第一维上限（提示：`moba_kv` 段数上限为 `F·H`，每段 `chunk_size` 个 token；实际是否裁剪取决于零 expert，本讲先按上限算）。
3. 把结果填进 4.4.3 的参数表，与 [L431-L443](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L431-L443) 的传参顺序逐一对号。

**需要观察的现象**：支路 B 的「序列条数」是 `F·H` 级别、每条固定 `chunk_size` 长——这是一个**规则矩形**问题集，恰好是 flash-attn 最擅长处理的形态；而 query 侧每段长度（`moba_seqlen_q`）参差不齐，是真正的 varlen。

**预期结果**：`S = 1024`；`C = 3 + 2 = 5`；`F = 5 - 2 = 3`；调整后 topk = `min(2, 3) = 2`；`cu_chunk = [0, 256, 512, 640, 896, 1024]`（第 2 个 batch 的最后一块只有 128 个 token）；`moba_kv` 第一维上限 `3 × 2 × 256 = 1536`。形状核对无误即通过。

#### 4.4.5 小练习与答案

**练习 1**：支路 A 的 varlen 边界是 `cu_chunk` 而不是原始 `cu_seqlens`，这带来什么计算特征？

**答案**：每条「序列」变成不超过一个块长的小段，内核内每个 query 的 key 范围 ≤ `chunk_size`，显存与计算都按块长而非整序列长增长；同时块与块之间在支路 A 里完全隔离，跨块注意力全部交给支路 B，两路职责零重叠。

**练习 2**：两个支路一次都没有用到 `+inf/-inf` 加性掩码，naive 里靠掩码实现的因果性去哪了？

**答案**：被「数据编排 + 因果设置」吸收了。query 自己块内的因果性 → 支路 A 的 `causal=True`；跨块因果性 → gate 阶段的 `gate_chunk_end_mask / gate_batch_end_mask` 在**选块时**就保证了只有历史块能入选，入选块整体在 query 之前，于是支路 B 可以 `causal=False`。掩码从「注意力计算时」前移到了「选块时」。

**练习 3**：合并公式里为什么要先取 `max_lse` 再做指数？

**答案**：数值稳定。LSE 是 log 域的量，直接 `exp(lse)` 可能溢出（打分大时 \(Z\) 可达 \(e^{数十}\) 以上）；先减去两路 LSE 的逐元素最大值，把所有指数的输入压到 ≤ 0，`exp` 结果落在 (0, 1]，最后再加回 `max_lse`（L168）回到真实 log 域。这与 softmax 减最大值是同一技巧。

## 5. 综合实践

**任务**：为 `moba_attn_varlen` 画一张完整流程图，并用它分析 `topk=1` 的边界行为。（本任务纸笔即可完成，第 3 步可选 GPU 验证。）

**步骤**：

1. **画流程图**：以 4.1.2 的文字版为底稿，自己重画一张，必须包含：
   - 兜底分支（`need_moba_attn` 为假 → 全量因果自注意力）；
   - self-attn 支路与 moba-attn 支路各自的输入、varlen 边界（`cu_chunk` vs `moba_cu_seqlen_*`）、`causal` 取值；
   - LSE 合并节点；
   - **每个箭头上的张量形状**（用符号 `S/H/D/C/F/N`，并写明每个符号的含义）。
2. **边界分析**：设 `batch=1, chunk_size=256, seqlen=1024, moba_topk=1`。依次回答：
   - `calc_chunks` 返回的 `C`、`F` 各是多少？`filtered_chunk_indices` 是什么？
   - 调整后 `moba_topk` 与 `need_moba_attn` 是多少？程序实际执行哪一行返回？
   - 为什么此时结果与「纯 self-attention」逐元素一致？
3. **（可选，GPU）数值验证**：运行 4.3.4 第 2 步的示例脚本，确认配置 A 的 `o_moba` 与 `o_full` 最大误差为 0；再对 `moba_topk=2` 各跑一次，观察输出不再一致。**待本地验证**。

**参考答案**（第 2 步）：

- `C = 4`（块边界 `[0,256,512,768,1024]`），`F = 4 - 1 = 3`，`filtered_chunk_indices = [0,1,2]`（剔除唯一 batch 的最后一块 3）；
- 调整后 `moba_topk = min(1-1, 3) = 0`，`need_moba_attn = False`；
- 程序在 [L318-L321](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L318-L321) 提前返回：`flash_attn_varlen_func(q, k, v, cu_seqlens, cu_seqlens, max_seqlen, max_seqlen, causal=True)`；
- 与「纯 self-attention」一致的原因：这条调用用的正是**原始 batch 级 `cu_seqlens` + 整序列 `causal=True`**，也就是不加任何稀疏结构的全量因果注意力本身——兜底分支不是「近似」，而是精确退化为全量注意力。

## 6. 本讲小结

- 高效实现的本质：**换表达不换算法**——把「每 query 各看各的块」重写成「块内因果 varlen 注意力 + 选中块非因果 varlen 注意力」两路 flash-attn 调用，再用 LSE 数学精确合并。
- 四步心智模型与行区间的映射：元数据（L305-L323）→ gate 选块（L325-L371）→ varlen 重组（L373-L428）→ `MixedAttention.apply`（L430-L443）。
- `kv = stack((k,v), dim=1)` 让 K、V 在所有后续按块搬运中永远同步移动，反向梯度以同样的堆叠形态返回。
- `moba_topk` 是「含当前块的总块数」：当前块必选被物化为 self-attn 支路，因此跨块预算减一；每个 batch 的最后一块因因果规则永不可能被自由选中，被 `calc_chunks` 从候选集剔除，语义不变、计算更省。
- `need_moba_attn` 为假（`topk=1` 或序列短于一个块）时，函数**精确退化为** `flash_attn_varlen_func` 全量因果自注意力。
- 因果性从「注意力计算时的加性掩码」前移到「选块时的 gate 掩码 + 支路 A 的 `causal=True`」，全代码没有一处 `+inf/-inf` 掩码参与最终注意力计算。

## 7. 下一步学习建议

下一讲 **u3-l2（calc_chunks：chunk 元数据构造与最后一块的过滤）**将钻进本讲一笔带过的 [moba/moba_efficient.py:14-L64](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L14-L64)：`cu_num_chunk / cu_chunk / chunk_to_batch / filtered_chunk_indices` 是如何从 `cu_seqlens` 一路推导出来的，`lru_cache` 缓存张量参数的行为与风险。建议在进入下一讲前，先把本讲综合实践的流程图画熟——后续五讲的所有细节都会挂在这张图上。
