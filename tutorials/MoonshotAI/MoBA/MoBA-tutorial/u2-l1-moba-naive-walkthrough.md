# moba_naive 逐行精读：分块、gate 与块选择掩码

## 1. 本讲目标

本讲精读 MoBA 的「教科书式」参考实现 `moba_attn_varlen_naive`。读完本讲，你应该能够：

1. 独立说清 MoBA 前向的五个步骤：KV 分块 → 每块代表向量（K 均值）构造 → gate 打分与因果修正 → top-k 选块并展开成注意力掩码 → 带掩码的 softmax 注意力。
2. 读懂 `moba/moba_naive.py` 中的每一行关键代码，包括 `+inf / -inf` 因果修正、`gate_idx_mask` 平局保护这两个最容易被忽视的细节。
3. 在纯 CPU 环境（不需要 GPU、不需要 flash-attn）运行一个调试脚本，亲眼看到「每个 query 选中了哪些块」，并手工验证因果性。
4. 为下一单元精读 `moba_efficient.py` 建立黄金参考：高效实现的全部输出都必须和本讲的 naive 实现对齐。

## 2. 前置知识

本讲假设你已读过 u1-l1（块稀疏注意力的动机）和 u1-l3（张量布局与 varlen 约定）。这里补充四个本讲会反复用到的概念。

### 2.1 注意力与「加性掩码」

标准注意力的定义是：

\[
\mathrm{Attention}(Q, K, V) = \mathrm{softmax}\!\left(\frac{QK^\top}{\sqrt{d}}\right)V
\]

其中 \(d\) 是头维度（head_dim）。代码中并不显式构造「0/1 掩码矩阵」，而是用**加性偏置**：在 logits 上加一个矩阵，允许注意的位置加 0，禁止注意的位置加 \(-\infty\)。因为

\[
\exp(-\infty) = 0,
\]

被禁止位置的 softmax 权重自动变成 0，等价于掩码。这是读懂本代码的第一把钥匙：**后面看到的 `gate` 矩阵，既是打分矩阵，又被原地改写成加性掩码**。

### 2.2 `+inf / -inf` 参与 top-k 的语义

PyTorch 的 `torch.topk` 可以正常处理无穷大：

- `+inf` 比任何有限分数都大，必然排进 top-k 的第一名 →「强制选中」。
- `-inf` 比任何有限分数都小，只有凑不满 k 个时才会被选上 →「强制排除」。

MoBA 正是利用这一性质实现「当前块必选、未来块必不选」的因果规则，一行 `if` 都不用写。

### 2.3 MoE 的 top-k gating 类比

MoBA = Mixture of Block Attention，把 MoE（Mixture of Experts）的思想搬到注意力里：

| MoE 概念 | MoBA 对应物 |
|---|---|
| 专家（expert） | 一个 KV 块（chunk） |
| 路由器打分（router logits） | query 与块代表向量的内积（gate） |
| top-k 选专家 | `torch.topk(gate, k=moba_topk)` |
| 专家网络计算 | 对被选中块做标准注意力 |

关键区别：MoBA 的 gate **没有可学习参数**——分数直接来自 Q 与「块内 K 均值」的内积，零额外参数，但也因此需要继续训练才能学会「往哪看」（见 u1-l1）。

### 2.4 varlen 约定回顾（来自 u1-l3）

- Q/K/V 布局为 flash-attn 风格 `[总序列长 S, 头数 H, 头维度 D]`，多个 batch 的序列首尾相接打包在一起。
- `cu_seqlens` 是长度为 `batch + 1` 的前缀和，`cu_seqlens[i]` 到 `cu_seqlens[i+1]` 是第 i 个 batch 的序列切片边界。
- `max_seqlen` 只作内核启动参数，naive 实现里其实用不到它（只在签名里保持与 flash-attn 一致）。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用法 |
|---|---|---|
| `moba/moba_naive.py` | 唯一核心文件，`moba_attn_varlen_naive` 的完整前向（约 90 行） | 逐行精读 |
| `tests/test_moba_attn.py` | 用 naive 实现作为黄金参考校验高效实现 | 看它如何调用、如何设容忍度 |
| `README.md` | 项目背景与块稀疏注意力概念 | 已在 u1-l1 讲过，本讲只回引结论 |

一个实用提示：`moba/__init__.py` 的导入链（[moba/__init__.py:1-6](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/__init__.py#L1-L6)）会连带导入依赖 flash-attn 的 `wrapper` 和 `moba_efficient`。所以如果你只想跑 naive 实现（例如在一台没有 GPU / 没装 flash-attn 的机器上学习），**把 `moba_attn_varlen_naive` 复制到独立脚本**是最省事的方式——它只 `import torch` 和 `math`。这正是本讲代码实践的做法。

## 4. 核心概念与源码讲解

先给出全函数的心智图。`moba_attn_varlen_naive` 的签名与文档见 [moba/moba_naive.py:7-27](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L7-L27)：输入 varlen 布局的 q/k/v、`cu_seqlens`、`max_seqlen`、块大小 `moba_chunk_size`、选块数 `moba_topk`，输出同布局的注意力结果。

整个函数外层是一个 `for batch_idx in range(batch)` 循环（[moba/moba_naive.py:34-41](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L34-L41)）：先用 `cu_seqlens` 切出本 batch 的 `q_ / k_ / v_`，再对这个单独的序列做完整的 MoBA 前向，结果通过 `o_ += ...`（切片视图的就地写入）写回全局输出 `o`。**batch 之间互相隔离，所有块级操作都在单个序列内部进行**——这一点 naive 版靠 Python 循环天然保证，而高效版要靠专门的掩码构造（u3-l3）。

对每个 batch，五个步骤依次是：

```text
k_ [S,H,D]
  │ ① 按 chunk_size 切成 N 块，每块求均值
  ▼
key_gate_weight [N,H,D]        （块的「代表向量」）
  │ ② 与 q_ 做内积（fp32）
  ▼
gate [H,S,N]                   （每个 query 对每块的打分）
  │ ③ 因果修正：当前块→+inf，未来块→-inf；top-k 选块
  ▼
need_attend [H,S,N]            （布尔块选择掩码）
  │ ④ 展开成 token 级加性掩码
  ▼
gate [H,S,S]                   （0 / -inf）
  │ ⑤ qk += gate → softmax → 加权 V
  ▼
o_ [S,H,D]
```

下面按五个最小模块逐个拆解。

### 4.1 KV 分块与 key_gate_weight 构造

#### 4.1.1 概念说明

MoBA 的第一个动作是把本 batch 的 KV 序列（长度记为 `batch_size`，避免与 batch 个数混淆）按 `moba_chunk_size` 切成 `N = ceil(batch_size / chunk_size)` 块，最后一块可能不满。然后为每块计算一个**代表向量**：块内所有 K 在 token 维度上的均值。

为什么用均值？直觉上，块均值是这块 KV 的「质心」：query 与质心的内积衡量的是「query 与这一整块的大致相关度」，而不是与某个具体 token 的相关度。它有三个好性质：

1. **无参数**：不需要训练任何路由权重，符合 MoBA 的 parameter-less gating 设计。
2. **与块长度无关**：求均值而不是求和，长块不会仅仅因为 token 多而得分高。
3. **量级与 K 一致**：均值后的向量范数与单个 K 同量级，内积分数可以直接比较。

#### 4.1.2 核心流程

```text
num_block = ceil(batch_size / chunk_size)
for 块 i in [0, num_block):
    block_start = i * chunk_size
    block_end   = min(batch_size, block_start + chunk_size)   # 最后一块截断
    key_gate_weight[i] = mean(k_[block_start:block_end], dim=token)
# key_gate_weight 形状 [N, H, D]
```

注意 PyTorch 切片越界是安全的：`block_start:block_end` 会自动截到序列末尾，所以最后不满的一块天然被正确处理。

#### 4.1.3 源码精读

分块与求均值的实现在 [moba/moba_naive.py:42-50](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L42-L50)：

```python
# calc key gate weight
key_gate_weight = []
batch_size = batch_end - batch_start
num_block = math.ceil(batch_size / moba_chunk_size)
for block_idx in range(0, num_block):
    block_start = block_idx * moba_chunk_size
    block_end = min(batch_size, block_start + moba_chunk_size)
    key_gate_weight.append(k_[block_start:block_end].mean(dim=0, keepdim=True))
key_gate_weight = torch.cat(key_gate_weight, dim=0)  # [ N, H, D ]
```

这段代码用 Python 循环逐块切片，`mean(dim=0, keepdim=True)` 在 token 维上求均值并保留维度以便 `cat`。注意这里的 `batch_size` 是**本条序列的长度**（变量名与「batch 里有多少条序列」相冲突，阅读时要小心）。

配合外层的 batch 切片 [moba/moba_naive.py:35-41](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L35-L41)：

```python
batch_start = cu_seqlens[batch_idx].item()
batch_end = cu_seqlens[batch_idx + 1].item()
q_ = q[batch_start:batch_end]
k_ = k[batch_start:batch_end]
v_ = v[batch_start:batch_end]
```

即先按 `cu_seqlens` 切出本条序列，再在序列内部分块。**分块永远以每条序列自己的起点为基准**，不会跨 batch。

#### 4.1.4 代码实践

**实践目标**：验证分块边界的计算。

**操作步骤**（可在纯 CPU 上完成，示例代码）：

```python
import math

batch_size, chunk = 520, 128
num_block = math.ceil(batch_size / chunk)
for i in range(num_block):
    s, e = i * chunk, min(batch_size, (i + 1) * chunk)
    print(f"block {i}: [{s}, {e}), len={e - s}")
```

**需要观察的现象**：打印出 5 个块的区间。

**预期结果**：前 4 块每块 128 个 token，最后一块是 `[512, 520)`、长度 8——这就是「最后一块不满」的情形，它在本讲的 naive 版里正常参与计算（对比：高效实现把每个 batch 的最后一块单独留给 self-attention 支路，见 u3-l1）。

#### 4.1.5 小练习与答案

**练习 1**：若序列长度 512、`moba_chunk_size=128`，一共几块？每块边界？
**答案**：\(N = \lceil 512/128 \rceil = 4\) 块，边界 `[0,128) [128,256) [256,384) [384,512)`，恰好都满。

**练习 2**：如果 `moba_chunk_size` 大于等于序列长度（例如 chunk=4096、序列 512），会发生什么？
**答案**：`num_block = 1`，整个序列只有一块。后面会看到此时每个 query 的当前块就是唯一块，MoBA 退化为全量因果注意力（综合实践中我们会实际验证这一点）。

**练习 3**：为什么代表向量用 `mean` 而不是取块的第一个 token？
**答案**：块首 token 只是块内一个采样点，容易受局部抖动影响；均值对整块更具代表性，且与块长度无关、无需参数。取首 token 相当于引入「位置偏置」，与 less structure 的设计初衷不符。

### 4.2 gate 计算：fp32 下的内积打分

#### 4.2.1 概念说明

有了每块的代表向量 \(\bar{k}_{h,n}\)，query \(q_{h,s}\) 对块 \(n\) 的 gate 分数就是一个内积：

\[
g_{h,s,n} = \sum_{d=1}^{D} q_{h,s,d}\,\bar{k}_{h,n,d}
\]

物理含义：query 与块质心的相似度。分数越高，越倾向注意这一块。注意此时**没有任何因果约束**——任意 query 对任意块都有一个原始分数，约束在下一步再加。这就是「先打分、后修正」的两段式设计，它把「模型想看哪」和「因果允许看哪」解耦，逻辑更清晰。

代码还做了一个重要的数值决定：**在 fp32 中计算 gate**。bf16 只有约 3 位十进制有效数字，内积的舍入误差可能让相邻块的分数次序颠倒，导致 top-k 选错块；而块选择是「离散决策」，错了就是错了，无法靠 downstream 平均掉。所以打分这一步升级到 fp32，注释里写得很直白：`use fp32 to avoid precision issue in bf16`。

#### 4.2.2 核心流程

```text
q_, key_gate_weight 临时转为 fp32
gate = einsum("shd,nhd->hsn", q_, key_gate_weight)   # [H, S, N]
q_, key_gate_weight 转回原 dtype（k 的 dtype）
```

#### 4.2.3 源码精读

gate 的计算在 [moba/moba_naive.py:51-57](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L51-L57)：

```python
# calc & mask gate
# use fp32 to avoid precision issue in bf16
q_ = q_.type(torch.float32)
key_gate_weight = key_gate_weight.type(torch.float32)
gate = torch.einsum("shd,nhd->hsn", q_, key_gate_weight)  # [ H, S, N ]
key_gate_weight = key_gate_weight.type_as(k)
q_ = q_.type_as(k)
```

`einsum("shd,nhd->hsn")` 的三个下标：`s` = query 位置，`n` = 块编号，`h` = 头，`d` = 头维度（被求和消掉）。输出形状 `[H, S, N]`：头 h 的第 s 个 query 对第 n 块的分数。注意最后两行把 `q_`、`key_gate_weight` 转回原 dtype——`q_` 马上要参与后续 token 级注意力计算，不能一直停留在 fp32（显存翻倍且与参考语义不符）。

这里每个头独立打分：同一个 query 在不同头下可以选不同的块。这是 MoBA 相对「整层统一稀疏模式」方法的一个自由度。

#### 4.2.4 代码实践

**实践目标**：亲手验证 gate 的形状与量级。

**操作步骤**（示例代码，纯 CPU 可跑）：

```python
import torch

torch.manual_seed(0)
S, H, D, N = 512, 2, 128, 4
q = torch.randn(S, H, D)
kgw = torch.randn(N, H, D)          # 假装这是 4 个块的代表向量
gate = torch.einsum("shd,nhd->hsn", q, kgw)
print(gate.shape)                    # 期望 torch.Size([2, 512, 4])
print(gate[0, :3])                   # 看头 0 前 3 个 query 的打分
```

**需要观察的现象**：`gate` 形状为 `[H, S, N]`；打分数量级大约在 ±30（随机向量内积，随 \(\sqrt{D}\) 增长）。

**预期结果**：形状正确；不同 query 的打分差异明显（数量级远大于 bf16 的分辨率），说明 fp32 打分是安全的。量级具体数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`gate` 中 `gate[h, s, n]` 的语义是什么？
**答案**：头 h 下、第 s 个 query 对第 n 个 KV 块的原始相关性分数（尚未加任何因果约束）。

**练习 2**：为什么打分必须用 fp32，而后面算注意力可以容忍 bf16？
**答案**：top-k 选块是离散决策，分数的微小误差可能翻转块的排序，造成「选错块」这种结构性错误；而注意力加权是连续计算，bf16 舍入只会带来平均意义上极小的输出误差（测试里对应 `atol=2e-2` 的容忍度）。

**练习 3**：如果把 `einsum` 写成 `"shd,nhd->hsn"` 之外的顺序（如 `"shd,nhd->nhs"`），对后续代码有影响吗？
**答案**：有。后续所有操作（topk、`need_attend`、`repeat_interleave`）都假定块维度 `n` 在最后一维。改成 `nhs` 后必须同步改所有下游维度索引，容易出错——einsum 输出布局是一种「接口约定」。

### 4.3 因果修正与 top-k 选择

#### 4.3.1 概念说明

这是 MoBA 最核心的一步：把无约束的打分矩阵改造成满足因果律的块选择。规则有三条：

1. **query 在块 n 之后**（能看到整块 n）：分数保持原值，由模型自主决定选不选。
2. **query 在块 n 内部**：分数强制 `+inf` → 当前块必选。这不是可有可无的优化，而是**正确性要求**：因果掩码保证块内 query 至少能看到自己之前的 token，如果当前块没被选中，该 query 的可注意集合可能为空，softmax 全 `-inf` 行会产生 NaN。
3. **query 在块 n 之前**（整块 n 都在未来）：分数强制 `-inf` → 必不选，这是因果律。

修正后做 top-k：对每个 (头, query) 在块维度上取最大的 `k = min(moba_topk, num_block)` 个块（`min` 防止块数不足时 `topk` 报错）。

#### 4.3.2 核心流程

修正规则用数学写出来（\(C\) 为 chunk_size，\(B_n = [nC, (n{+}1)C)\)）：

\[
\tilde{g}_{h,s,n} =
\begin{cases}
+\infty, & s \in B_n \quad\text{（query 在块内，必选）}\\[2pt]
-\infty, & s < nC \quad\text{（query 在块前，必不选）}\\[2pt]
g_{h,s,n}, & s \ge (n{+}1)C \quad\text{（query 在块后，自主决定）}
\end{cases}
\]

```text
for 块 i:
    gate[:, 0 : (i+1)*C, i] = -inf     # 第一步：块 i 之前+之内的 query 一律先排除
    gate[:, i*C : (i+1)*C, i] = +inf   # 第二步：再覆盖，块 i 内的 query 必选
gate_top_k_val, gate_top_k_idx = topk(gate, k, dim=-1, largest=True)
threshold = gate_top_k_val.min(dim=-1)          # 每个 (H,S) 的入选门槛
need_attend = gate >= threshold                  # 初选
gate_idx_mask = scatter(True at gate_top_k_idx)  # 精确名单
need_attend = need_attend AND gate_idx_mask      # 终选，恰好 k 个
```

**两行赋值的顺序不能换**：第一步把 `[0, (i+1)C)` 全部打成 `-inf`（覆盖「块前」和「块内」两种 query），第二步再把块内区间覆盖成 `+inf`。若交换顺序，`+inf` 会被第一步的 `-inf` 覆盖，当前块永远选不上。

**为什么需要 `gate_idx_mask`**：`need_attend = gate >= threshold` 在分数**平局**时会选出多于 k 个块。平局不是罕见 corner case——考虑块 0 内的 query：它的 gate 行是 `[+inf, -inf, ..., -inf]`（未来块全被因果修正成 `-inf`），若 `moba_topk=2`，top-2 是 `[+inf, -inf]`，threshold 是 `-inf`，于是 `gate >= -inf` 整行全 True，初选会「选中」所有块！所以必须用 `scatter` 按 `gate_top_k_idx` 的精确名单再过滤一次，保证每个 query 恰好选中 k 个块。源码注释 `add gate_idx_mask in case of there is cornercases of same topk val been selected` 说的就是这件事。

#### 4.3.3 源码精读

因果修正循环在 [moba/moba_naive.py:58-61](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L58-L61)：

```python
for i in range(num_block):
    # select the future Qs that can attend to KV chunk i
    gate[:, : (i + 1) * moba_chunk_size, i] = float("-inf")
    gate[:, i * moba_chunk_size : (i + 1) * moba_chunk_size, i] = float("inf")
```

注意第一行的切片越界安全：当 `(i+1)*C` 超过序列长度时自动截断（最后一块不满的情形）。注释里的 "future Qs" 指「位置在块 i 起点之后的 query」——只有它们才被允许注意块 i。

top-k 选择与平局保护在 [moba/moba_naive.py:62-73](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L62-L73)：

```python
# gate_top_k_idx = gate_top_k_val = [ H S K ]
gate_top_k_val, gate_top_k_idx = torch.topk(
    gate, k=min(moba_topk, num_block), dim=-1, largest=True, sorted=False
)
gate_top_k_val, _ = gate_top_k_val.min(dim=-1)  # [ H, S ]
need_attend = gate >= gate_top_k_val.unsqueeze(-1)
# add gate_idx_mask in case of there is cornercases of same topk val been selected
gate_idx_mask = torch.zeros(
    need_attend.shape, dtype=torch.bool, device=q.device
)
gate_idx_mask = gate_idx_mask.scatter_(dim=-1, index=gate_top_k_idx, value=True)
need_attend = torch.logical_and(need_attend, gate_idx_mask)
```

逻辑链：`topk` 拿到入选块编号 `gate_top_k_idx`（形状 `[H, S, K]`）→ 入选分数的最小值作为门槛 → `>=` 门槛初选（可能超选）→ `scatter_` 把入选编号直接写成布尔掩码 → 两者相与，得到**恰好 k 个 True** 的 `need_attend [H, S, N]`。`gate_top_k_idx` 后续不再使用，真正生效的是 `need_attend`。

#### 4.3.4 代码实践

**实践目标**：手工构造一个小例子，观察 `+inf / -inf` 修正后的 gate 行长什么样。

**操作步骤**（示例代码，纯 CPU 可跑）：

```python
import torch

S, N, C = 8, 2, 4
gate = torch.randn(1, S, N)          # 假装这是刚算好的原始打分 [H=1, S, N]
for i in range(N):                   # 复现源码 L58-61 的修正
    gate[:, : (i + 1) * C, i] = float("-inf")
    gate[:, i * C : (i + 1) * C, i] = float("inf")
for s in range(S):
    print(f"query {s}: {gate[0, s].tolist()}")
```

**需要观察的现象**：query 0–3（块 0 内）的行为是 `[inf, -inf]`；query 4–7（块 1 内）是 `[有限值, inf]`。

**预期结果**：query 0 只能选块 0（`+inf`），块 1 是 `-inf`；query 4 对块 0 保留原始分数、对块 1 强制 `+inf`。对照 4.3.2 的三分法逐行核对。

#### 4.3.5 小练习与答案

**练习 1**：把 L60、L61 两行交换顺序，会发生什么？
**答案**：第一步的 `-inf` 会覆盖掉先写入的 `+inf`，任何 query 的当前块都变成 `-inf`；块 0 内的 query 整行都是 `-inf`，topk 凑数选出的块又全被 token 级因果掩码屏蔽，最终 softmax 对全 `-inf` 行求值得到 NaN。

**练习 2**：`moba_topk=3`，序列只有 2 个块，`torch.topk` 会怎样？
**答案**：`k = min(3, 2) = 2`，安全地退化为选 2 块。这就是 `min(moba_topk, num_block)` 的作用。

**练习 3**：query 在块 i（i ≥ 1）内时，它修正后的 gate 行由哪些值构成？
**答案**：过去 i 个块是有限原始分数、当前块是 `+inf`、其余未来块是 `-inf`。topk 必含当前块，再从 i 个过去块里按分数挑 k−1 个。

### 4.4 gate 掩码展开：从块级 [H,S,N] 到 token 级 [H,S,S]

#### 4.4.1 概念说明

`need_attend` 是**块级**选择（`[H, S, N]`），但最终注意力需要 **token 级** 掩码（`[H, S, S]`）：query s 是否注意 key t。本模块完成两级转换：

1. **块 → token**：块级布尔值沿块内的所有 key 位置复制展开（`repeat_interleave`）。选中块里的每个 key 都暂时可见，未选中的每个 key 都不可见。
2. **块内因果**：选中**当前块**只保证「块级可见」，块内 query 仍然不能看块内自己之后的 token（因果律要求 t ≤ s）。所以在展开结果上再叠加一个下三角（`tril`）掩码。

完成后，`gate` 张量被改写为纯加性掩码：可见位置 0、不可见位置 `-inf`。

#### 4.4.2 核心流程

```text
gate[need_attend]  = 0        # 选中 → 加 0
gate[~need_attend] = -inf     # 未选中 → 加 -inf
gate = gate.repeat_interleave(C, dim=-1)[:, :, :batch_size]   # [H,S,N] → [H,S,N*C] → 截断
gate[上三角] = -inf            # token 级因果（tril）
```

截断的原因：`N * C ≥ batch_size`（最后一块不满时严格大于），展开后的 key 维比实际序列长，需要切到 `batch_size`。

#### 4.4.3 源码精读

掩码改写与展开在 [moba/moba_naive.py:74-81](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L74-L81)：

```python
gate[need_attend] = 0
gate[~need_attend] = -float("inf")
gate = gate.repeat_interleave(moba_chunk_size, dim=-1)[
    :, :, :batch_size
]  # [ H, S, S ]
gate.masked_fill_(
    torch.ones_like(gate, dtype=torch.bool).tril().logical_not(), -float("inf")
)
```

三个细节：

- `gate` 在这里完成了角色转换：上一个模块它是**打分矩阵**，这个模块起它是**加性掩码**——同一个变量的两用，读代码时要跟上。
- `repeat_interleave(C, dim=-1)` 是「每个块标签重复 C 次」而不是「整体重复 C 遍」，块标签 `[b0, b1]` 展开为 `[b0×C, b1×C]`，正确对应块内 key 位置。
- `tril` 那行的构造有点绕：`ones.tril()` 是下三角为 True，`logical_not()` 后上三角为 True，`masked_fill_` 把上三角填 `-inf`。t > s 的位置全部不可见——这就是 token 级因果。它同时兜住了 4.3 中「平局/凑数多选了未来块」的情况：即使 `need_attend` 误选了未来块，其 key 位置也会在这里被 `-inf` 屏蔽，不影响结果。

#### 4.4.4 代码实践

**实践目标**：验证展开后掩码与块级选择一一对应。

**操作步骤**（示例代码，接 4.3.4 的小例子，纯 CPU 可跑）：

```python
import torch

S, N, C = 8, 2, 4
need_attend = torch.zeros(1, S, N, dtype=torch.bool)
need_attend[0, :, 0] = True                    # 简化：所有 query 都选块 0
need_attend[0, 4:, 1] = True                   # 块 1 内的 query 选块 1

gate = torch.zeros(1, S, N)
gate[need_attend] = 0
gate[~need_attend] = -float("inf")
gate = gate.repeat_interleave(C, dim=-1)[:, :, :S]
gate.masked_fill_(torch.ones_like(gate, dtype=torch.bool).tril().logical_not(), -float("inf"))
print((gate[0] == 0).int())                    # 1 = 可见
```

**需要观察的现象**：打印出的 8×8 矩阵，1 的位置构成「下三角 + 块级选择」的交集。

**预期结果**：query 0–3 只有下三角内的块 0 部分（key 0..s）可见；query 4 的 key 4 可见、key 5–7 不可见（同为块 1 但在未来）；query 7 可见 key 0–7（两个块都选中且都在因果范围内）。

#### 4.4.5 小练习与答案

**练习 1**：`repeat_interleave` 之后为什么要 `[:, :, :batch_size]`？
**答案**：`N × C ≥ 序列长`，最后一块不满时展开后的 key 维会多出一段，必须截断到实际长度，否则与 `qk` 形状不匹配（广播会直接报错）。

**练习 2**：块级选择之后为什么还需要 token 级 `tril`？块选择不是已经保证因果了吗？
**答案**：块级修正只保证「不选整块都在未来的块」，但当前块内部的 token 仍有先后：query s 选中当前块后，仍不能看块内位置大于 s 的 token。tril 补上这最后一层因果。

**练习 3**：`need_attend` 由于 `-inf` 平局「多选」了一个未来块，最终输出会错吗？
**答案**：不会。多选的块在掩码里是 0（可见），但其所有 key 位置都落在上三角（t > s），被 tril 填回 `-inf`，softmax 权重为 0，对输出无贡献。这就是 4.3 的 `gate_idx_mask` 与本模块 tril 的双保险。

### 4.5 带掩码的注意力计算与输出回写

#### 4.5.1 概念说明

最后一步是教科书式的标准注意力，只是 logits 上叠加了 4.4 的稀疏掩码。对头 h、query s：

\[
p_{h,s,t} = \frac{\exp\!\big(m_{h,s,t} + q_{h,s}\!\cdot\! k_{h,t}/\sqrt{D}\big)}
{\sum_{t'} \exp\!\big(m_{h,s,t'} + q_{h,s}\!\cdot\! k_{h,t'}/\sqrt{D}\big)},
\qquad
o_{h,s} = \sum_{t} p_{h,s,t}\, v_{h,t}
\]

其中 \(m_{h,s,t} \in \{0, -\infty\}\) 是加性掩码（选中且因果为 0，否则 \(-\infty\)）。分母只会对可见位置求和——**稀疏性由此进入计算**：每个 query 实际参与的 key 数约为 `moba_topk × moba_chunk_size`，占比约 \(\text{topk} \times C / S\)，这正是 MoBA 相对全量注意力省算力的来源。

与第 2 讲一致，注意力主体在 fp32 中计算（bf16 的 softmax 累加误差过大），最后输出转回输入 dtype。

#### 4.5.2 核心流程

```text
q_, k_, v_ 转 fp32
qk = einsum("xhd,yhd->hxy", q_, k_)     # [H, S, S] 原始 logits
qk += gate                               # 叠加稀疏掩码（0 / -inf）
qk *= softmax_scale                      # 1/sqrt(D)
p  = qk.softmax(dim=-1)
o_ += einsum("hxy,yhd->xhd", p, v_)      # 写回全局输出切片
o  = o.type_as(q)                        # 输出与输入同 dtype
```

#### 4.5.3 源码精读

注意力主体在 [moba/moba_naive.py:83-94](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L83-L94)：

```python
# calc qk = qk^t
q_ = q_.type(torch.float32)
k_ = k_.type(torch.float32)
v_ = v_.type(torch.float32)
qk = torch.einsum("xhd,yhd->hxy", q_, k_)
# mask
qk += gate
qk *= softmax_scale
# calc o
p = qk.softmax(dim=-1)
o_ += torch.einsum("hxy,yhd->xhd", p, v_)
o = o.type_as(q)
```

四个值得停一停的点：

1. **`qk += gate` 在 `*= softmax_scale` 之前**。顺序看起来反直觉（通常先 scale 后加 mask），但这里无害：`gate` 的取值只有 0 和 \(-\infty\)，\(0 \times c = 0\)、\(-\infty \times c = -\infty\)（c 为正常数），先加后乘不改变语义。若掩码里是有限值（比如相对位置偏置），这个顺序就会出错——naive 版利用了 0/-inf 的特殊性。
2. **softmax 的数值安全**：每行至少有一个可见位置（当前块 +inf 必选 + tril 保证 query 能看到自己），分母不会是 0 个 \(\exp(0)\)，不会出现全 `-inf` 行的 NaN。`softmax_scale` 在函数开头由 `q.shape[-1] ** (-0.5)` 计算（[moba/moba_naive.py:31](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L31)），即 \(1/\sqrt{D}\)。
3. **`o_ += ...` 的就地回写**：`o_` 是全局输出 `o`（`torch.zeros_like(q)` 初始化，见 [moba/moba_naive.py:33](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L33)）的切片视图，切片上的 `+=` 直接写入 `o` 对应区间，这就是 varlen 输出的组装方式。
4. **`o = o.type_as(q)`**（[moba/moba_naive.py:94](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L94)）：保证输出 dtype 与输入 q 一致（若 q 是 bf16，fp32 的中间结果落回 bf16 存储）。最后 `return o`（[moba/moba_naive.py:96](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L96)）。

整个函数对外可微（全是标准 PyTorch 算子），因此 `tests/test_moba_attn.py` 可以直接对它 `torch.autograd.backward`（见 [tests/test_moba_attn.py:70-79](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L70-L79)），把它当作高效实现的黄金参考同时校验前向与反向——这正是 naive 版「为教学与测试而生」的价值。

#### 4.5.4 代码实践

**实践目标**：验证「先加掩码再乘 scale」与「先 scale 再加掩码」结果一致。

**操作步骤**（示例代码，纯 CPU 可跑）：

```python
import torch

qk = torch.randn(1, 8, 8)
mask = (torch.ones(8, 8).tril() - 1) * float("inf")   # 0 / -inf 加性掩码
mask = mask.view(1, 8, 8)
scale = 128 ** -0.5

a = ((qk + mask) * scale).softmax(dim=-1)   # 源码顺序：先加 mask 后 scale
b = ((qk * scale) + mask).softmax(dim=-1)   # 常规顺序：先 scale 后加 mask
print(torch.allclose(a, b))
```

**需要观察的现象**：两种顺序的 softmax 输出逐元素一致。

**预期结果**：打印 `True`。原因见 4.5.3 第 1 点——0 与 \(-\infty\) 乘正常数不变。（若把掩码换成有限值如 -10，两种顺序就会不同，可以顺手试一试。）

#### 4.5.5 小练习与答案

**练习 1**：`softmax_scale` 等于多少？为什么需要它？
**答案**：\(D^{-1/2}\)（head_dim 的平方根的倒数）。内积的量级随维度增长（约 \(\sqrt{D}\) 倍），不除以它会 saturate softmax、杀死梯度。

**练习 2**：`einsum("hxy,yhd->xhd", p, v_)` 输出形状是什么？`x` 维对应什么？
**答案**：`[S, H, D]`，即本 batch 序列上每个 query 位置的输出向量；`x` 是 query 维（与 `p` 的第二维、输出 `o_` 的第一维对齐）。

**练习 3**：`moba_topk=2`、`moba_chunk_size=128`、序列长 512 时，每个 query 大约实际注意多少个 key？占全量的比例？
**答案**：约 \(2 \times 128 = 256\) 个（当前块内还会被 tril 截掉一半左右，实际略少），占比约 \(256/512 = 50\%\)。序列越长这个比例越低，稀疏收益越大。

## 5. 综合实践

**任务：跑通完整调试脚本，观察块选择模式，并验证「单块退化」情形。**

这个实践把本讲五个模块串成一条链。**不依赖 GPU 与 flash-attn，纯 CPU 可完成**。

**第一步：制作独立调试脚本。** 把 [moba/moba_naive.py:7-96](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L7-L96) 的函数完整复制到一个新文件（如 `moba_naive_debug.py`），并在 `need_attend = torch.logical_and(need_attend, gate_idx_mask)`（即源码 L73）之后插入打印（示例代码）：

```python
print("top-k blocks of head 0, query 0-2:",
      [sorted(gate_top_k_idx[0, s].tolist()) for s in range(3)])
print("need_attend of head 0, query 0-2:\n", need_attend[0, :3].int())
```

再写主程序调用它（示例代码）：

```python
import torch

torch.manual_seed(0)
S, H, D = 512, 1, 64
q = torch.randn(S, H, D)
k, v = torch.randn(S, H, D), torch.randn(S, H, D)
cu_seqlens = torch.tensor([0, S], dtype=torch.int32)

o = moba_attn_varlen_naive_debug(q, k, v, cu_seqlens, S,
                                 moba_chunk_size=128, moba_topk=2)
print("output shape:", o.shape)
```

**第二步：观察块选择（对应规格中的核心实践）。** 运行脚本，回答三个问题：

1. query 0、1、2（都在块 0 内）的 `need_attend` 里哪些块是 True？块 0 一定是 True 吗？另一个 True 落在哪，每次运行固定吗？
2. 找一个位于块 2 的 query（如 s=300），它选中的两个块分别是什么角色（当前块 / 过去块）？
3. **手工验证因果性**：query 0 只应注意到 key 0。检查 token 级生效掩码——在函数返回前再加一行 `print((gate[0, 0] == 0).nonzero())`（注意 `gate` 此时的形状是 `[H, S, S]`），nonzero 返回的 key 位置应全部 ≤ 0，即只有 key 0。

**预期结果**：块 0 的 query 行为 `[+inf, -inf, -inf, -inf]`，top-2 是 `[+inf, 某个 -inf]`，`need_attend` 恰好 2 个 True、块 0 必在 其中；第二个 True 的位置是 `-inf` 平局的任意选择，**不保证固定**（这正是 4.3 讲的 `gate_idx_mask` 要处理的 corner case，且多选的块会被 tril 屏蔽，不影响输出）。位于块 2 的 query 必选块 2，再从块 0、1 中按 gate 分数选一个。query 0 的生效 key 只有 {0}，因果性成立。具体打印数值待本地验证。

**第三步：验证单块退化。** 写一个普通因果注意力参考（示例代码）：

```python
def causal_attn(q, k, v):
    S = q.shape[0]
    scale = q.shape[-1] ** -0.5
    qk = torch.einsum("xhd,yhd->hxy", q.float(), k.float()) * scale
    qk = qk.masked_fill(
        torch.ones(S, S, dtype=torch.bool).tril().logical_not().view(1, S, S),
        float("-inf"))
    return torch.einsum("hxy,yhd->xhd", qk.softmax(-1), v.float())
```

然后令 `moba_chunk_size=512`（≥ 序列长 512，`num_block=1`）、`moba_topk` 任意（如 2），比较 `moba_attn_varlen_naive_debug` 与 `causal_attn` 的输出。**预期两者 `allclose`**：单块时每个 query 的当前块就是唯一块（`+inf` 必选），掩码退化为纯 tril，MoBA 等价于全量因果注意力。这一验证同时解释了 u1-l2 里「短提示下 moba 与 moba_naive 输出一致」的兜底行为。

## 6. 本讲小结

- **KV 分块与代表向量**：每条序列独立按 `moba_chunk_size` 分块（最后一块可不满），块内 K 均值 `key_gate_weight [N,H,D]` 是无参数的块代表向量。
- **gate 打分**：`einsum("shd,nhd->hsn")` 得到 `[H,S,N]` 打分矩阵，在 fp32 中计算以避免 bf16 精度翻转 top-k 决策。
- **因果修正的 ±inf 技巧**：当前块 `+inf`（必选，兼防 NaN）、块前 `-inf`（必不选）、块后保留原值；两行赋值顺序不可交换。
- **平局双保险**：`gate_idx_mask`（scatter 精确名单）保证恰好 k 个块入选，token 级 tril 兜底屏蔽任何被「凑数」选中的未来块。
- **掩码即加性偏置**：`gate` 从打分矩阵原地改写为 0/-inf 加性掩码，`repeat_interleave` 展开到 token 级后叠加 tril，再走标准 softmax 注意力（fp32 计算、输出转回输入 dtype）。
- **稀疏度的量级**：每个 query 实际参与约 `topk × chunk_size` 个 key，占比 \(\text{topk} \times C / S\)——这是 MoBA 加速的来源，也是 u4-l3 性能测试的理论基础。

## 7. 下一步学习建议

下一讲（u2-l2）将聚焦块选择的两个细节的深化：`-inf` 平局下 `gate_idx_mask` 的必要性实测，以及如何把 `need_attend` 掩码导出并可视化成「query × 块」热力图，直观感受 chunk_size / topk 对选择模式的影响。

之后进入第三单元前，建议你回头重读 [moba/moba_naive.py:58-81](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L58-L81) 直到能默写因果修正与掩码展开的逻辑——`moba_efficient.py` 做的每一步索引变换，本质上都是把这里的 Python 循环、掩码和 `repeat_interleave` 改写成能喂给 flash-attn 的 varlen 形式（u3-l1 起）。naive 实现将作为黄金参考贯穿后续所有正确性讨论。
