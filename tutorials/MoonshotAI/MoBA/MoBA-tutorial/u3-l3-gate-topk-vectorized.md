# u3-l3 gate 的向量化计算与跨 batch 因果掩码

## 1. 本讲目标

上一讲（u3-l2）我们拿到了 `calc_chunks` 产出的四个纯索引张量——`cu_chunk`、`filtered_chunk_indices`、`num_filtered_chunk`、`chunk_to_batch`。本讲进入 `moba_attn_varlen` 四步心智模型的**第 2 步：gate 打分选块**。读完本讲，你应该能够：

1. 说清楚 `filtered_kv` 的 gather 是如何把散落在 varlen 序列里的候选块「抽」成一块规整的稠密张量的，以及为什么它要求「候选块恒为满块」；
2. 掌握 `gate_chunk_end_mask` 与 `gate_batch_end_mask` 这两个布尔掩码如何用**一次比较 + 一次广播**，同时完成「块内因果」和「跨 batch 隔离」两种约束——这是 naive 版本里 Python 双重 for 循环的向量化替身；
3. 理解 `torch.topk(gate, k, dim=0)` 如何做到**每个 head 独立选块**，以及最终 `gate_mask` 与 naive 版本 `need_attend` 的逐条等价关系。

本讲结束后得到的 `gate_mask [N_CHUNK, HEAD, SEQ]` 是下一讲（u3-l4 varlen trick）的直接输入——整个高效实现的索引魔法都从这张布尔表出发。

## 2. 前置知识

### 2.1 回顾：u3-l2 的元数据张量与本讲贯穿示例

本讲全程使用一个贯穿示例（与第 5 节综合实践的输入一致）：

> **贯穿示例**：`cu_seqlens = [0, 20, 33]`（2 条序列，长度 20 和 13，共 33 个 token），`moba_chunk_size = 8`（记作 \(C\)），`moba_topk = 3`。

由 u3-l2 的推导方法可得 `calc_chunks` 的全部返回值：

| 张量 | 值 | 含义 |
|---|---|---|
| `cu_chunk` | `[0, 8, 16, 20, 28, 33]` | 全部 5 块的全局起始偏移（首元素 0 为哨兵） |
| `filtered_chunk_indices` | `[0, 1, 3]` | 候选块编号：剔除各 batch 最后一块（块 2、块 4） |
| `num_filtered_chunk` | `3` | 候选块数 = 总块数 5 − 序列条数 2 |
| `chunk_to_batch` | `[0, 0, 0, 1, 1]` | 每块所属序列 |

三个候选块的几何位置：

| 候选块 | 全局区间 | `chunk_end`（结束偏移） | 所属 batch | `batch_end`（序列结束） |
|---|---|---|---|---|
| 块 0 | `[0, 8)` | 8 | 0 | 20 |
| 块 1 | `[8, 16)` | 16 | 0 | 20 |
| 块 3 | `[20, 28)` | 28 | 1 | 33 |

被剔除的块 2（`[16, 20)`，只有 4 个 token）和块 4（`[28, 33)`，只有 5 个 token）是各自序列的不满尾块——它们不参加本讲的任何竞争。

### 2.2 einsum 下标怎么读

`torch.einsum("nhd,shd->nhs", A, B)` 的读法：出现在两个输入里、但**不出现在输出里**的下标（这里是 `d`）被求和；出现在两个输入里的同名下标（这里是 `h`）必须配对相同，且不跨下标求和。于是：

\[
\text{gate}[n, h, s] \;=\; \sum_{d=0}^{D-1} A[n,h,d] \cdot B[s,h,d]
\]

### 2.3 布尔掩码、广播与 masked_fill_

| 操作 | 语义 |
|---|---|
| `t[:, None]` | 在中间插入长度 1 的维度，配合比较运算得到逐行阈值掩码 |
| `mask.unsqueeze(1)` | 把 `[N, S]` 变成 `[N, 1, S]`，沿 HEAD 维广播 |
| `t.masked_fill_(mask, value)` | 原地操作：`mask` 为 True 的位置填 `value` |
| `t.scatter_(dim, index, value)` | 按 `index` 张量把 `value` 散射到指定位置（u2-l2 已精读） |

### 2.4 -inf 与 topk 的配合（回顾 u2-l2）

- `-inf` 参与 `topk(largest=True)` 时排在最后，只有当「有限值数量不足 k」时才会被**被迫**选中；
- u2-l2 的结论：scatter 出来的 top-k **下标名单**是精确答案，阈值筛选（`gate >= 第 k 大值`）可能因平局多选、需要与名单取交集；
- 本讲会看到高效版本干脆**丢掉阈值筛选**，只保留「名单 ∩ 非 -inf」，并用 `torch.isinf()` 把先前写入的 `-inf` 直接当作掩码源复用。

## 3. 本讲源码地图

| 文件 | 本讲关注范围 |
|---|---|
| [moba/moba_efficient.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py) | 本讲主角：`kv` 堆叠（L299）、`moba_topk` 减一（L313-L315）、`filtered_kv` gather（L325-L330）、`key_gate_weight` 与 gate 打分（L332-L349）、两组掩码（L351-L360）、top-k 与最终 `gate_mask`（L362-L371） |
| [moba/moba_naive.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py) | 对照阅读：per-batch 循环切片（L34-L41）、逐块求均值（L43-L50）、fp32 打分（L52-L55）、因果修正双写（L58-L61）、top-k 与阈值+名单（L62-L73） |

两份文件实现**同一个 gate 语义**，但表达方式完全不同——本讲的核心乐趣就是把它们逐行对上。

## 4. 核心概念与源码讲解

### 4.1 filtered_kv gather：把候选块收成稠密张量

#### 4.1.1 概念说明

gate 打分的对象是「每个候选块的代表向量」，而候选块在 varlen 序列里是**不连续**的：贯穿示例中候选块 0/1/3 占据全局位置 `[0,8)`、`[8,16)`、`[20,28)`，中间隔着被剔除的尾块 `[16,20)` 和 `[28,33)`。如果直接在原序列上做分段均值，就得写 Python 循环（naive 版本正是这么做的）。

向量化做法是先用一次 gather 把所有候选块**抽出来拼成稠密张量**：形状变成 `[num_filtered_chunk × C, 2, H, D]`——每块恒好 `C` 个 token，块在 dim 0 上依次排列。这一步是后面所有「规整变形」（`view(N, C, H, D)`、`split(C, dim=1)`）的前提，而它之所以合法，完全依赖 u3-l2 的过滤：**候选集里没有不满的尾块**。

#### 4.1.2 核心流程

```text
输入: kv [S, 2, H, D]（L299 把 k、v 沿 dim1 堆叠）
  1. arange(0, C) 复制 N 份          -> [N, C]，每行都是 [0 .. C-1]
  2. 每行加上该块的全局起始偏移        -> [N, C]，第 n 行 = [start_n .. start_n+C-1]
  3. index_select 展平成一维去取行    -> filtered_kv [N*C, 2, H, D]
输出: 候选块按「块优先、块内按 token 顺序」连续排列
```

贯穿示例中第 2 步的结果是 `[[0..7], [8..15], [20..27]]`——恰好跳过了 `16..19`（尾块 2）与 `28..32`（尾块 4），共收集 24 行。

#### 4.1.3 源码精读

[悬赏的 kv 堆叠发生在主流程开头](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L299)——`kv = torch.stack((k, v), dim=1)` 把 K、V 捆成一个张量，此后一次索引就能同步搬运两者（`filtered_kv[:, 0]` 是 K、`filtered_kv[:, 1]` 是 V）：

```python
kv = torch.stack((k, v), dim=1)
```

[gather 的三行核心代码](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L325-L330)：先造「块内相对下标 + 块起始偏移」的绝对下标矩阵，再一次性取行：

```python
    # filtered_kv is a dense matrix that only contains filtered chunk of kv
    filtered_kv_indices = torch.arange(
        0, moba_chunk_size, dtype=torch.int32, device=q.device
    )[None, :].repeat(num_filtered_chunk, 1)
    filtered_kv_indices += cu_chunk[filtered_chunk_indices][:, None]
    filtered_kv = kv.index_select(0, filtered_kv_indices.view(-1))
```

- 第 1 步 `arange(...)[None, :].repeat(N, 1)` 得到每行相同的 `[0..C-1]`；
- 第 2 步 `cu_chunk[filtered_chunk_indices][:, None]` 是每个候选块的起始偏移（u3-l2 的 `cu_chunk` 在这里第一次被消费），广播相加后第 `n` 行就是块 `n` 的全局 token 下标；
- 第 3 步 `index_select(0, ...view(-1))` 把 `[N, C]` 展平成 `N*C` 个下标去取行，输出 `[N*C, 2, H, D]`，排列顺序为「块 0 的 C 个 token、块 1 的 C 个 token、……」。

注意 `cu_chunk` 是**逐块累计**出来的（块不跨 batch、尾块不满，见 u3-l2），所以这里不能写成 `n * C` 这样的等差公式——否则会错位。

#### 4.1.4 代码实践（可运行）

**目标**：验证 gather 下标矩阵的构造逻辑。**步骤**：运行下面的小脚本（示例代码，仅需 CPU 上的 PyTorch）：

```python
import torch
cu_chunk = torch.tensor([0, 8, 16, 20, 28, 33], dtype=torch.int32)
filtered_chunk_indices = torch.tensor([0, 1, 3])
C = 8
idx = torch.arange(0, C, dtype=torch.int32)[None, :].repeat(3, 1)
idx += cu_chunk[filtered_chunk_indices][:, None]
print(idx)
assert idx.tolist() == [list(range(0, 8)), list(range(8, 16)), list(range(20, 28))]
```

**需要观察的现象**：打印出的 3 行分别覆盖 `[0,7]`、`[8,15]`、`[20,27]`。**预期结果**：断言通过；下标集合大小为 24，且**不含** `16..19` 与 `28..32`。本节实践可在任意环境运行，结果确定。

#### 4.1.5 小练习与答案

**练习 1**：若把 `filtered_chunk_indices` 换成全部 5 块 `[0,1,2,3,4]`，gather 还能安全进行吗？
**答案**：不能。块 2 只有 4 个 token（区间 `[16,20)`），第 2 行下标会算出 `[16..23]`，越界取到块 3 的 token，块边界被破坏；块 4 同理（`[28..35]` 越界到序列末尾之外会直接报错）。满块假设是 gather 的安全条件。

**练习 2**：为什么先把 k、v 堆叠成 `kv` 再 gather，而不是分别 gather 两次？
**答案**：语义等价，但一次 `index_select` 同时搬运 K 和 V，减少一次索引内核启动与中间显存分配；后续 `filtered_kv[:, 0]` / `[:, 1]` 都是零拷贝的视图切分。

### 4.2 key_gate_weight：块内 K 均值与 fp32 gate 打分

#### 4.2.1 概念说明

MoBA 的 gate 是**无参数**的：每个候选块的代表向量不是可学习的投影，而是**块内 K 的算术平均**。直觉是「一个块里所有 key 的中心点」——query 与这个中心做内积，就能估量「这一整块对我大概有多重要」，而无需逐 key 打分。这正是 u1-l1 讲过的核心思想：打分成本只有 \(N \times S \times D\)（块数 × query 数 × 维度），远小于逐 key 的 \(S^2 D\)。

打分必须在 **fp32** 中进行：gate 分数只用来做离散的 top-k 决策，bf16 的舍入误差足以在分数接近时翻转选择，而高效实现与 naive 实现必须做出**完全相同的块选择**才能在测试中对齐输出。

还有一个容易困惑的细节：注意力打分通常会乘 \(\text{softmax\_scale} = D^{-1/2}\)，gate 却**不乘**。原因是 gate 只用于排序、从不进入 softmax——任何正常数缩放都不改变 top-k 的次序，乘不乘无所谓。两份实现在此一致。

#### 4.2.2 核心流程

```text
filtered_kv [N*C, 2, H, D]
  1. 取 K 部分 [:, 0]                          -> [N*C, H, D]
  2. view(N, C, H, D) + mean(dim=1)             -> key_gate_weight [N, H, D]（块内均值）
  3. .float()，q 也临时转 fp32
  4. einsum("nhd,shd->nhs")                     -> gate [N, H, S]
  5. key_gate_weight 与 q 都还原回原 dtype（fp32 只服务于打分这一步）
```

数学上：

\[
\bar{k}[n,h,d] = \frac{1}{C}\sum_{c=0}^{C-1} k[\text{start}_n + c, h, d],
\qquad
\text{gate}[n,h,s] = \sum_{d} \bar{k}[n,h,d]\, q[s,h,d]
\]

注意 `mean` 发生在**原精度**（如 bf16）上、之后才转 fp32——这个顺序与 naive 保持一致（见 4.2.3 对照），先转 fp32 再求均值虽然更精确，却可能与 naive 的数值不逐位一致。

#### 4.2.3 源码精读

[高效版本的均值与打分](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L332-L349)——一次 `view + mean` 替代了循环：

```python
    """ calc key_gate_weight and gate """

    # key_gate_weight [ F_N_CHUNK, HEAD, HEAD_DIM ]
    key_gate_weight = (
        filtered_kv[:, 0]
        .view(num_filtered_chunk, moba_chunk_size, num_head, head_dim)
        .mean(dim=1)
        .float()
    )
    q = q.type(torch.float32)  # float logit on the fly for better gate logit perception
    key_gate_weight = key_gate_weight.type(
        torch.float32
    )  # float logit for better gate logit perception
    gate = torch.einsum(
        "nhd,shd->nhs", key_gate_weight, q
    )  # gate [ F_N_CHUNK, HEAD, SEQ ]
    key_gate_weight = key_gate_weight.type_as(k)
    q = q.type_as(k)
```

- `view(N, C, H, D)` 之所以合法，是因为 4.1 节的 gather 保证了「每块恰好 C 个连续 token」；
- `mean(dim=1)` 沿块内 token 维求平均，得到 `[N, H, D]`；
- 打分完成后 `type_as(k)` 把两个张量**还原**回原 dtype——fp32 是一次性的，下游 flash-attn 仍消费原精度。

[naive 版本的对应实现](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L43-L55)——双重循环逐块切出来求均值，再在 fp32 中打分：

```python
        key_gate_weight = []
        batch_size = batch_end - batch_start
        num_block = math.ceil(batch_size / moba_chunk_size)
        for block_idx in range(0, num_block):
            block_start = block_idx * moba_chunk_size
            block_end = min(batch_size, block_start + moba_chunk_size)
            key_gate_weight.append(k_[block_start:block_end].mean(dim=0, keepdim=True))
        key_gate_weight = torch.cat(key_gate_weight, dim=0)  # [ N, H, D ]
        # calc & mask gate
        # use fp32 to avoid precision issue in bf16
        q_ = q_.type(torch.float32)
        key_gate_weight = key_gate_weight.type(torch.float32)
        gate = torch.einsum("shd,nhd->hsn", q_, key_gate_weight)  # [ H, S, N ]
```

两处布局差异值得注意：naive 的 einsum 输出 `[H, S, N]`（块在最后一维，方便 `topk(dim=-1)`），高效版本输出 `[N, H, S]`（块在第 0 维，方便 `topk(dim=0)` 以及 u3-l4 里 `reshape(shape[0], -1)` 的「按块×头分组」）。**数值相同，布局服务各自的后处理**。

另一个要点：gate 的数值**从不流入输出**，只决定选哪些块——它是离散的路由决策，没有梯度。反观 `MixedAttention.backward`（u3-l6 精读），dq 完全来自两条 flash-attn 反向支路，与 gate 无关。

#### 4.2.4 代码实践（可运行）

**目标**：验证 `view + mean(dim=1)` 与「循环逐块 mean」数值一致。**步骤**（示例代码）：

```python
import torch
k = torch.arange(4 * 2 * 3, dtype=torch.float32).reshape(4, 2, 3)  # 4 token、2 head、3 dim，假装 C=2
vectorized = k.view(2, 2, 2, 3).mean(dim=1)          # [2 块, 2 head, 3 dim]
looped = torch.stack([k[i * 2:(i + 1) * 2].mean(dim=0) for i in range(2)])
print(torch.allclose(vectorized, looped))
```

**需要观察的现象**：两种算法得到相同的块均值。**预期结果**：打印 `True`。再把 `dtype=torch.float32` 换成 `torch.bfloat16` 重跑，观察结果是否仍为 True（均值在 bf16 下的舍入路径）——**待本地验证**，不同 PyTorch 版本的归约实现可能有差异。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `.float()` 挪到 `.mean(dim=1)` 之前（先转 fp32 再求均值），算法还正确吗？为什么作者不这么做？
**答案**：算法仍然正确（甚至数值更精确），但会与 naive 版本「原精度 mean → fp32 打分」的顺序不一致；两份实现的 gate 数值若因舍入路径不同而出现微小差异，接近的分数可能在 top-k 平局处翻转选择，导致 efficient 与 naive 选块不同、测试无法对齐。保持顺序一致是让 naive 能当「黄金参考」的前提之一。

**练习 2**：`gate [N, H, S]` 中 `n=3, s=33`（贯穿示例），一共有多少个 gate 分数？与逐 key 打分 \(S^2\) 相比规模如何？
**答案**：\(3 \times H \times 33\) 个，随块数线性增长；逐 key 打分是 \(33^2 \times H\)。序列越长（块数远小于 token 数），gate 的相对开销越小。

### 4.3 gate_chunk_end_mask：一个比较同时挡住当前块与未来块

#### 4.3.1 概念说明

拿到原始 gate 分数后，必须施加因果与结构约束。naive 版本（u2-l1/u2-l2 精读过）用了两行赋值：对块 `i`，把「位置小于 `(i+1)*C` 的 query」全写 `-inf`（禁选），再把「块内 query」覆写 `+inf`（当前块必选），且**顺序不可交换**。

高效版本的洞察是：**当前块根本不必参与竞争**。u3-l1 讲过两支路设计——query 所在的当前块由 self-attn 支路按 `cu_chunk` 边界做块内因果注意力，MoBA 支路只需要在「严格过去的整块」里选 `moba_topk - 1` 个（主流程 [L313-L314](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L313-L314) 已把 k 减一）。于是：

- 不需要 `+inf` 强制选中——没有「必选」语义要表达；
- 「query 在块内」（当前块）与「query 在块之前」（未来块）两种非法情形，可以被**同一个比较** `s < chunk_end[n]` 一并挡住：query 位置必须不小于块的结束偏移，块才整体位于 query 的过去。

用公式写就是：候选块 \(n\) 可被 query \(s\) 竞选的必要条件为

\[
s \;\ge\; \text{chunk\_end}[n] \;=\; \text{chunk\_start}[n] + C
\]

由于候选块恒满（`chunk_end = start + C`），在序列局部坐标下它恰好等价于 naive 的 `-inf` 写入条件 `s_{\text{local}} < (i+1)\,C`。

由此还能推出一个漂亮的结构性质：**同一块内的所有 query 拥有完全相同的候选集**——块 `j` 里的 query 可以自由竞选本 batch 中所有编号 `i < j` 的候选块。掩码因此呈「阶梯对角带」形状（u2-l2 的热力图里见过）。

#### 4.3.2 核心流程

```text
gate [N, H, S]（原始分数）
  1. gate_seq_idx = [0..S-1] 复制 N 份             -> [N, S]
  2. chunk_end  = cu_chunk[filtered_chunk_indices + 1]   （每块结束偏移）
  3. gate_chunk_end_mask[n, s] = (s < chunk_end[n])      -> [N, S] 布尔
  （与 4.4 的 batch 掩码求或后，一次性 masked_fill_ 成 -inf，沿 HEAD 广播）
```

贯穿示例：`chunk_end = [8, 16, 28]`，于是 `s ∈ [0,8)` 什么都不能选；`s ∈ [8,16)` 只能竞选块 0；`s ∈ [16,20)` 可竞选块 0、1；`s ∈ [20,28)` 与 `s ∈ [28,33)` 的候选留待 4.4 补全。

#### 4.3.3 源码精读

[掩码构造与写入](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L351-L360)——注意真正的 `-inf` 写入发生在两组掩码求「或」之后（见 4.4）：

```python
    # pose process gate, masking unchosen batch and apply causal mask to current chunk
    gate_seq_idx = torch.arange(0, seqlen, device=q.device, dtype=torch.int32)[
        None, :
    ].repeat(num_filtered_chunk, 1)
    chunk_end = cu_chunk[filtered_chunk_indices + 1]
    batch_end = cu_seqlens[chunk_to_batch[filtered_chunk_indices] + 1]
    gate_chunk_end_mask = gate_seq_idx < chunk_end[:, None]
    gate_batch_end_mask = gate_seq_idx >= batch_end[:, None]
    gate_inf_mask = gate_chunk_end_mask | gate_batch_end_mask
    gate.masked_fill_(gate_inf_mask.unsqueeze(1), -float("inf"))
```

逐行看本模块相关的三行：

- `gate_seq_idx`：`[N, S]` 的 query 全局位置矩阵，每行相同；
- `chunk_end = cu_chunk[filtered_chunk_indices + 1]`：候选块的**结束**偏移——用 `+1` 取 `cu_chunk` 的下一段起点即是本段终点（前缀和数组的经典用法），这是 u3-l2 的 `cu_chunk` 第二次被消费；
- `gate_chunk_end_mask = gate_seq_idx < chunk_end[:, None]`：`chunk_end[:, None]` 变 `[N, 1]`，广播比较得 `[N, S]`。True 意味着「query 尚未到达该块末尾」→ 该块对此 query 非法。

对照 [naive 的因果修正循环](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L58-L61)：

```python
        for i in range(num_block):
            # select the future Qs that can attend to KV chunk i
            gate[:, : (i + 1) * moba_chunk_size, i] = float("-inf")
            gate[:, i * moba_chunk_size : (i + 1) * moba_chunk_size, i] = float("inf")
```

第一行 `-inf` 写入与本模块的 `s < chunk_end` 完全同语义（局部坐标 `s_local < (i+1)C`）；第二行 `+inf` 在高效版本中**没有对应物**，其职责被「self-attn 支路 + L313-L314 的 topk 减一 + u3-l2 的尾块剔除」三者共同接管。也正因为高效版本**从不写 `+inf`**，后文才能放心用 `~gate.isinf()` 把 `-inf` 当掩码用（`isinf` 对 `±inf` 都返回 True，若混入 `+inf` 就会误杀当前块）。

#### 4.3.4 代码实践（可运行）

**目标**：手工推演并验证 chunk_end 掩码。**步骤**（示例代码）：

```python
import torch
chunk_end = torch.tensor([8, 16, 28], dtype=torch.int32)
gate_seq_idx = torch.arange(33, dtype=torch.int32)[None, :].repeat(3, 1)
gate_chunk_end_mask = gate_seq_idx < chunk_end[:, None]
print(gate_chunk_end_mask[1, [10, 15, 16]])   # 块 1 对 query 10 / 15 / 16
```

**需要观察的现象**：query 10、15 处为 True（屏蔽），query 16 处为 False（放行）。**预期结果**：`tensor([True, True, False])`——块 1 的结束偏移是 16，`s=16` 是块 2 的第一个 token，恰好在块 1 末尾之后，从此块 1 解禁。先笔算再运行核对。

#### 4.3.5 小练习与答案

**练习 1**：为什么用 `chunk_end`（结束偏移）而不是 `chunk_start`（起始偏移）做比较？用 `s < chunk_start` 会怎样？
**答案**：`s < chunk_start` 只能挡「query 在块之前」的未来块，挡不住「query 在块内部」的当前块——`start ≤ s < end` 的 query 会错误地把自己的当前块也选进 MoBA 支路，与 self-attn 支路重复计票（且破坏「当前块必选、k 减一」的对齐）。用结束偏移一次比较同时覆盖两种情形。

**练习 2**：贯穿示例中 query `s=18`（位于被剔除的尾块 2 内）的 chunk_end 候选集是什么？
**答案**：满足 `18 >= chunk_end` 的候选块：块 0（end=8）、块 1（end=16）；块 3（end=28）不满足。它的当前块是尾块 2，本来就不在候选集里，由 self-attn 支路处理——尾块剔除对它零损失。

### 4.4 gate_batch_end_mask：压平 for 循环的代价——跨 batch 隔离

#### 4.4.1 概念说明

naive 版本按 batch 逐段处理：每次循环切出 `q[batch_start:batch_end]`，gate 只在本序列内部 `[H, S_b, N_b]` 上计算，**跨 batch 泄漏在结构上就不可能发生**。高效版本为了消灭 Python 循环，把 gate 放大到全局布局 `[N, H, S]`——所有候选块 × 所有 query 一起打分。这带来一个新问题：

- **第二条序列的 query 的全局位置天然大于第一条序列所有块的 `chunk_end`**。贯穿示例中 batch 1 的 query `s=25` 对块 0（end=8）、块 1（end=16）都满足 `25 >= chunk_end`，chunk_end 掩码**完全挡不住**——它会错误地「向后看」到别家序列的块。

`gate_batch_end_mask` 补上这个方向：query 位置一旦越过该块所属序列的结束边界，就禁选。两个掩码各挡一个方向的泄漏：

| 泄漏方向 | 例子 | 谁来挡 |
|---|---|---|
| 后 batch 的 query 选前 batch 的块 | `s=25` 选块 0/1 | `gate_batch_end_mask`（25 ≥ 20） |
| 前 batch 的 query 选后 batch 的块 | `s=10` 选块 3 | `gate_chunk_end_mask`（10 < 28） |
| 同 batch 内选当前/未来块 | `s=10` 选块 1 | `gate_chunk_end_mask`（10 < 16） |

合并后的精确充要条件：

\[
\text{块 } n \text{ 可被 query } s \text{ 竞选} \;\Longleftrightarrow\;
\text{chunk\_end}[n] \;\le\; s \;<\; \text{batch\_end}[b(n)]
\]这个区间自动保证 \(s\) 落在 \([\text{batch\_start}[b(n)], \text{batch\_end}[b(n)])\) 内（因为 \(\text{chunk\_end}[n] \ge \text{batch\_start}[b(n)]\)），即 **query 与块同属一个 batch**，且块整体位于 query 的严格过去（块内所有 key 位置都小于 \(s\)）——这也解释了为什么 u3-l5 里 MoBA 支路敢用 `causal=False`：因果性已经在选块阶段前置完成。

#### 4.4.2 核心流程

```text
（接 4.3 的步骤 2）
  2'. batch_end = cu_seqlens[chunk_to_batch[filtered_chunk_indices] + 1]   （每块所属序列的结束边界）
  3'. gate_batch_end_mask[n, s] = (s >= batch_end[n])                      -> [N, S]
  4'. gate_inf_mask = gate_chunk_end_mask | gate_batch_end_mask
  5'. gate.masked_fill_(gate_inf_mask.unsqueeze(1), -inf)   （unsqueeze(1) 沿 HEAD 广播，掩码与 head 无关）
```

贯穿示例：`batch_end = [20, 20, 33]`。与 4.3 的结果合并后，三个候选块的可选 query 区间为：

| 候选块 | 可选的 query 区间 | 宽度 |
|---|---|---|
| 块 0 | `8 ≤ s < 20` | 12 |
| 块 1 | `16 ≤ s < 20` | 4 |
| 块 3 | `28 ≤ s < 33` | 5 |

#### 4.4.3 源码精读

[batch 掩码与合并写入](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L355-L360)（与 4.3 同一段，这里聚焦另外三行）：

```python
    chunk_end = cu_chunk[filtered_chunk_indices + 1]
    batch_end = cu_seqlens[chunk_to_batch[filtered_chunk_indices] + 1]
    gate_chunk_end_mask = gate_seq_idx < chunk_end[:, None]
    gate_batch_end_mask = gate_seq_idx >= batch_end[:, None]
    gate_inf_mask = gate_chunk_end_mask | gate_batch_end_mask
    gate.masked_fill_(gate_inf_mask.unsqueeze(1), -float("inf"))
```

- `batch_end` 的下标链是本讲的索引体操精华：`chunk_to_batch[filtered_chunk_indices]` 先把候选块映射回所属序列编号（u3-l2 的 `chunk_to_batch` 在这里第三次被消费），`+1` 后去 `cu_seqlens` 里取该序列的**结束**边界；
- 两组掩码用 `|` 合并成 `gate_inf_mask [N, S]`，`unsqueeze(1)` 变 `[N, 1, S]` 后沿 HEAD 维广播——掩码只依赖位置，与 head 无关；
- 最终**一次** `masked_fill_` 写入 `-inf`：naive 里「先 `-inf` 后 `+inf` 覆盖」的书写顺序问题（u2-l2）在这里根本不存在，因为高效版本只有一种掩码取值。

对照 [naive 的 per-batch 循环切片](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L34-L41)：

```python
    for batch_idx in range(batch):
        batch_start = cu_seqlens[batch_idx].item()
        batch_end = cu_seqlens[batch_idx + 1].item()
        # get qkv of this batch
        q_ = q[batch_start:batch_end]
        k_ = k[batch_start:batch_end]
        ...
```

这就是「同样语义」在 naive 中的代码行：隔离不是靠掩码，而是靠**循环边界**结构性地实现。向量化把 for 循环压平成掩码——收益是全局一次算分、无 Python 开销；代价是必须显式补 `gate_batch_end_mask` 这一维约束。理解这对「收益/代价」是理解整个高效实现风格的关键。

#### 4.4.4 代码实践（可运行）

**目标**：验证两个泄漏方向各自被哪个掩码挡住。**步骤**（示例代码，承接 4.3.4 的变量）：

```python
batch_end = torch.tensor([20, 20, 33], dtype=torch.int32)
gate_batch_end_mask = gate_seq_idx >= batch_end[:, None]
# 方向一：s=25（batch 1 的 query）对块 0/1（batch 0 的块）
print(gate_batch_end_mask[0, 25], gate_batch_end_mask[1, 25])
# 方向二：s=10（batch 0 的 query）对块 3（batch 1 的块）——batch 掩码挡不住！
print(gate_batch_end_mask[2, 10])
print(gate_chunk_end_mask[2, 10])   # 由 chunk_end 掩码兜底
```

**需要观察的现象**：第一行打印两个 True（batch 掩码起作用）；第二行打印 False（batch 掩码对「前看后」无效）；第三行打印 True（chunk_end 掩码接住）。**预期结果**：`tensor(True) tensor(True)`、`tensor(False)`、`tensor(True)`。本实践同时证明两个掩码**缺一不可**。

#### 4.4.5 小练习与答案

**练习 1**：删掉 `gate_batch_end_mask`（令 `gate_inf_mask = gate_chunk_end_mask`），贯穿示例里哪些 query 会出错？
**答案**：batch 1 的所有 query（`s ∈ [20, 33)`）都可能错误选中块 0/1。例如 `s=28..32` 本应只竞选块 3，现在块 0、1 也进入候选集，可能把本属于块 3 的名额挤掉——batch 1 的输出会混入 batch 0 的信息。

**练习 2**：`batch_end` 为什么必须通过 `chunk_to_batch` 查表，而不能也用 `cu_chunk[... + 1]` 之类的块级数组推导？
**答案**：块的几何边界（`cu_chunk`）不含归属信息——全局相邻的两块可能属于不同 batch（如块 2 与块 3 分属 batch 0/1）。只有 `chunk_to_batch` 回答「块属于谁」，再去 `cu_seqlens` 查该 batch 的边界才是正确语义。

**练习 3**：掩码为什么可以不区分 head（`unsqueeze(1)` 广播）？
**答案**：因果与 batch 归属只由 token 的全局位置决定，与 head 无关；gate 分数才随 head 变化。掩码 `[N, 1, S]` 广播到 `[N, H, S]` 后，每个 head 使用同一张位置合法性表、在各自的分数上竞争。

### 4.5 gate_top_k_idx：按 head 独立 top-k 与最终 gate_mask

#### 4.5.1 概念说明

掩码后的 `gate [N, H, S]` 里，每个 `(head, query)` 列上有若干有限分数（合法候选）与若干 `-inf`。现在对每个 `(head, query)` 独立选出分数最大的 `k = moba_topk - 1` 个块——**topk 沿 dim=0（块维）进行**，不同 head 的选择互不影响（这是「按 head 分别路由」的 MoBA 语义，与 naive `topk(dim=-1)` 等价，只是布局转置）。

两个必答的问题：

1. **k 超过该列的合法候选数怎么办？**（贯穿示例里 `s ∈ [8,16)` 只有块 0 一个合法候选，而 k=2。）`torch.topk` 仍会返回 k 个下标——不足部分用 `-inf` 条目**平局任意**地凑数。高效版本的解法是拿 `~gate.isinf()` 把这些凑数项过滤掉：该 query 实际选中的块数小于 k，少掉的注意力由 self-attn 支路兜底。对照 u2-l2：naive 需要「阈值 + token 级 tril」两层兜底处理同一问题，高效版本的 `isinf` 过滤一层就够——因为违规项在**掩码阶段**就被丢弃，而不是留到 softmax 里靠 `-inf` 加性掩码归零。
2. **平局会不会多选？** 不会。`scatter_` 写入的是 topk 的**下标名单**，每列恰好 k 个位置被置 True；u2-l2 已证明「阈值掩码 ⊇ 名单」，交集恒等于名单——高效版本干脆省去阈值筛选，直接用「名单 ∩ 非 -inf」，从侧面印证了名单本身已是精确答案（这里能这么省，前提还是没有 `+inf` 混入）。

最终的 `gate_mask [N, H, S]`（True = 该（块, head, query）组合需要 MoBA 注意力）就是下一讲 varlen trick 的全部输入。

#### 4.5.2 核心流程

```text
gate [N, H, S]（已写入 -inf）
  1. gate_top_k_idx = topk(gate, k=moba_topk(已减一), dim=0, largest=True, sorted=False)  -> [k, H, S]
  2. gate_mask      = ~gate.isinf()                          # 位置合法性（因果 + batch）
  3. gate_idx_mask  = zeros[N, H, S].scatter_(0, gate_top_k_idx, True)   # 精确名单
  4. gate_mask      = gate_mask & gate_idx_mask              # 最终选择
输出: gate_mask [N, H, S]
```

贯穿示例（`moba_topk=3` → `k=2`）中，每个 query 的选中块数完全确定（与随机种子无关）：

| query 区间 | 所在块 | 合法候选 | 实际选中数 |
|---|---|---|---|
| `s ∈ [0, 8)` | 块 0 | 无 | 0（只有 self-attn） |
| `s ∈ [8, 16)` | 块 1 | 块 0 | 1（topk 的另一个名额被 -inf 凑数项占用后过滤） |
| `s ∈ [16, 20)` | 尾块 2 | 块 0、1 | 2 |
| `s ∈ [20, 28)` | 块 3 | 无 | 0 |
| `s ∈ [28, 33)` | 尾块 4 | 块 3 | 1 |

#### 4.5.3 源码精读

[top-k 与最终掩码](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L362-L371)：

```python
    """ find moba q that needs moba attn """
    # find topk chunks
    # gate_mask [ N_CHUNK, HEAD, SEQ ], true indicates that needs attention
    _, gate_top_k_idx = torch.topk(gate, k=moba_topk, dim=0, largest=True, sorted=False)
    # apply causal mask
    gate_mask = torch.logical_not(gate.isinf())
    # select topk chunks
    gate_idx_mask = torch.zeros(gate_mask.shape, dtype=torch.bool, device=q.device)
    gate_idx_mask = gate_idx_mask.scatter_(dim=0, index=gate_top_k_idx, value=True)
    gate_mask = torch.logical_and(gate_mask, gate_idx_mask)
```

- `dim=0` 是块维：对每个 `(h, s)` 列独立排序取前 k，天然实现「按 head 分别选块」；`sorted=False` 省掉名单内部的排序（下游只关心集合，不关心名次）；返回的 `gate_top_k_idx [k, H, S]` 是块编号；
- 这里的 `moba_topk` 已经是主流程 [L313-L314](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L313-L314) 调整过的值：`min(moba_topk - 1, num_filtered_chunk)`——减一补偿 self-attn 支路必然处理的当前块，`min` 防止 k 超过候选块总数（对照 naive [L63-L65](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L62-L65) 的 `k=min(moba_topk, num_block)`，两处截断同义）；
- `scatter_(dim=0, index=gate_top_k_idx)`：对每个 `(h, s)`，把 `k` 个块编号位置写成 True——与 naive [L69-L73](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L68-L73) 沿 `dim=-1` 的 scatter 是同一技巧的转置版本：

```python
        gate_idx_mask = torch.zeros(
            need_attend.shape, dtype=torch.bool, device=q.device
        )
        gate_idx_mask = gate_idx_mask.scatter_(dim=-1, index=gate_top_k_idx, value=True)
        need_attend = torch.logical_and(need_attend, gate_idx_mask)
```

naive 用「阈值筛选 ∩ 名单」，高效用「非 -inf ∩ 名单」——在两份实现 gate 数值一致、且高效无 `+inf` 的前提下，两者产生**相同的块选择集合**：当前块 +（k−1 个或不足时的全部）最优过去整块。这正是 `tests/test_moba_attn.py` 能拿 naive 当黄金参考校验 efficient 输出的前提之一。

#### 4.5.4 代码实践（源码阅读 + 预测型）

**目标**：预测并验证选中块数的分布。**步骤**：先**不运行**任何代码，仿照 4.5.2 的表格，笔算 `moba_topk = 5`（→ `k = min(4, 3) = 3`）时贯穿示例每个 query 区间的实际选中块数；然后运行第 5 节综合实践的脚本，把 `moba_topk` 改成 5，对比 `gate_mask.sum(dim=0)` 的输出。**需要观察的现象**：`s ∈ [16, 20)` 仍然只选中 2 块（合法候选只有块 0、1，第 3 个名额是 `-inf` 凑数项、被 `isinf` 过滤），并不会因为 k=3 而「凑满」。**预期结果**：选中数序列为 8 个 0、8 个 1、4 个 2、8 个 0、5 个 1——与 k=2 时完全相同（本例中合法候选数本身就 ≤ 2）。**待本地验证**（需要运行第 5 节脚本）。

#### 4.5.5 小练习与答案

**练习 1**：`gate_top_k_idx` 的形状是 `[k, H, S]`，而 `gate_mask` 是 `[N, H, S]`（k < N）。`scatter_` 为什么允许 index 与 self 在 dim=0 上长度不同？
**答案**：`scatter_` 的规则要求 index 与 self 维数相同、非散射维长度一致；沿散射维（dim=0）index 可以更短——它只是「写哪几行」的名单：对每个 `(h, s)`，把 `k` 个块编号位置写成 True，不要求覆盖全部 N 行。

**练习 2**：如果去掉 `gate_mask = torch.logical_and(gate_mask, gate_idx_mask)` 里的 `gate_mask`（即只留 scatter 名单），哪些位置会出错？
**答案**：合法候选不足 k 的列里，topk 被迫返回的 `-inf` 凑数下标会留在名单里（例如 `s ∈ [8,16)` 列的第二个名额可能落在块 1 或块 3 上）。这些块对本 query 是未来块或别家块，会错误进入后续 varlen 重组，造成与非因果/跨 batch 的 key 做注意力。

**练习 3**：为什么说「`~isinf` 过滤」比 naive 的「阈值 + token 级 tril」更干净？
**答案**：高效版本在**选块阶段**就把违规项从布尔表中剔除，违规 (块, query) 对根本不进入 varlen 重组；naive 则把违规块留在加性掩码里、靠 softmax 前的 `-inf`（块级 gate 掩码 + token 级 tril）让它们的贡献在数值上归零。前者不产生无效计算，后者要为最终为零的连接付出计算与显存——这正是「高效」二字的微观来源之一。

## 5. 综合实践

**任务**：把 `moba_attn_varlen` 的 gate 段落抽成一个可独立运行的脚本，在 CPU 上复现 `gate_inf_mask` 与最终 `gate_mask`，验证两条不变式，并完成与 naive 的行号对照。这是本讲规格中的核心实践。

**为什么不能直接调用 `moba_attn_varlen`**：该模块顶层 `import flash_attn`，没有 GPU/flash-attn 的环境无法导入；而 gate 段落在调用 flash-attn 之前就已完成，完全可以单独复现。下面的脚本把 `calc_chunks`（L14-L64）与 gate 段（L325-L371）**原样复制**，仅追加了打印与断言（示例代码，保存为 `gate_practice.py` 运行，只需 PyTorch）：

```python
import torch
from functools import lru_cache

# ↓↓↓↓ 以下 calc_chunks 复制自 moba/moba_efficient.py L14-L64，未做改动 ↓↓↓↓
@lru_cache(maxsize=16)
def calc_chunks(cu_seqlen, moba_chunk_size):
    batch_sizes = cu_seqlen[1:] - cu_seqlen[:-1]
    batch_num_chunk = (batch_sizes + (moba_chunk_size - 1)) // moba_chunk_size
    cu_num_chunk = torch.ones(
        batch_num_chunk.numel() + 1, device=cu_seqlen.device, dtype=batch_num_chunk.dtype
    )
    cu_num_chunk[1:] = batch_num_chunk.cumsum(dim=0)
    num_chunk = cu_num_chunk[-1]
    chunk_sizes = torch.full(
        (num_chunk + 1,), moba_chunk_size, dtype=torch.int32, device=cu_seqlen.device
    )
    chunk_sizes[0] = 0
    batch_last_chunk_size = batch_sizes - (batch_num_chunk - 1) * moba_chunk_size
    chunk_sizes[cu_num_chunk[1:]] = batch_last_chunk_size
    cu_chunk = chunk_sizes.cumsum(dim=-1, dtype=torch.int32)
    chunk_to_batch = torch.zeros((num_chunk,), dtype=torch.int32, device=cu_seqlen.device)
    chunk_to_batch[cu_num_chunk[1:-1]] = 1
    chunk_to_batch = chunk_to_batch.cumsum(dim=0, dtype=torch.int32)
    chunk_to_remove = cu_num_chunk[1:] - 1
    chunk_to_remain = torch.ones((num_chunk,), dtype=torch.bool, device=cu_seqlen.device)
    chunk_to_remain[chunk_to_remove] = False
    filtered_chunk_indices = chunk_to_remain.nonzero(as_tuple=True)[0]
    num_filtered_chunk = len(filtered_chunk_indices)
    return (cu_chunk, filtered_chunk_indices, num_filtered_chunk, chunk_to_batch)
# ↑↑↑↑ 复制结束 ↑↑↑↑

torch.manual_seed(0)
H, D = 2, 16
S = 33
q = torch.randn(S, H, D, dtype=torch.bfloat16)
k = torch.randn(S, H, D, dtype=torch.bfloat16)
v = torch.randn(S, H, D, dtype=torch.bfloat16)
kv = torch.stack((k, v), dim=1)                      # 对应 L299
cu_seqlens = torch.tensor([0, 20, 33], dtype=torch.int32)   # batch=2，长度 20 和 13
moba_chunk_size, moba_topk = 8, 3

seqlen, num_head, head_dim = q.shape
cu_chunk, filtered_chunk_indices, num_filtered_chunk, chunk_to_batch = calc_chunks(
    cu_seqlens, moba_chunk_size
)
moba_topk = min(moba_topk - 1, num_filtered_chunk)   # 对应 L313-L314

# ↓↓↓↓ 以下 gate 段复制自 moba/moba_efficient.py L325-L371，未做改动 ↓↓↓↓
filtered_kv_indices = torch.arange(
    0, moba_chunk_size, dtype=torch.int32, device=q.device
)[None, :].repeat(num_filtered_chunk, 1)
filtered_kv_indices += cu_chunk[filtered_chunk_indices][:, None]
filtered_kv = kv.index_select(0, filtered_kv_indices.view(-1))

key_gate_weight = (
    filtered_kv[:, 0]
    .view(num_filtered_chunk, moba_chunk_size, num_head, head_dim)
    .mean(dim=1)
    .float()
)
q = q.type(torch.float32)
key_gate_weight = key_gate_weight.type(torch.float32)
gate = torch.einsum("nhd,shd->nhs", key_gate_weight, q)
key_gate_weight = key_gate_weight.type_as(k)
q = q.type_as(k)

gate_seq_idx = torch.arange(0, seqlen, device=q.device, dtype=torch.int32)[
    None, :
].repeat(num_filtered_chunk, 1)
chunk_end = cu_chunk[filtered_chunk_indices + 1]
batch_end = cu_seqlens[chunk_to_batch[filtered_chunk_indices] + 1]
gate_chunk_end_mask = gate_seq_idx < chunk_end[:, None]
gate_batch_end_mask = gate_seq_idx >= batch_end[:, None]
gate_inf_mask = gate_chunk_end_mask | gate_batch_end_mask
gate.masked_fill_(gate_inf_mask.unsqueeze(1), -float("inf"))

_, gate_top_k_idx = torch.topk(gate, k=moba_topk, dim=0, largest=True, sorted=False)
gate_mask = torch.logical_not(gate.isinf())
gate_idx_mask = torch.zeros(gate_mask.shape, dtype=torch.bool, device=q.device)
gate_idx_mask = gate_idx_mask.scatter_(dim=0, index=gate_top_k_idx, value=True)
gate_mask = torch.logical_and(gate_mask, gate_idx_mask)
# ↑↑↑↑ 复制结束 ↑↑↑↑

print("cu_chunk               =", cu_chunk)
print("filtered_chunk_indices =", filtered_chunk_indices)
print("chunk_end =", chunk_end, " batch_end =", batch_end)
print("\ngate_inf_mask（# = 屏蔽，. = 可竞选）:")
for n in range(num_filtered_chunk):
    print(f"chunk {filtered_chunk_indices[n].item():d}: "
          + "".join("#" if gate_inf_mask[n, s] else "." for s in range(seqlen)))
print("\n每个 query 选中的块数 gate_mask.sum(dim=0):")
print(gate_mask.sum(dim=0))

# 不变式 1：第二个 batch 的 query（s >= 20）不能选中第一个 batch 的块（块 0/1）
assert gate_inf_mask[:2, 20:].all()
assert not gate_mask[:2, :, 20:].any()
# 不变式 2：所有放行位置都满足 chunk_end <= s < batch_end（块整体在 query 的过去且同 batch）
for n in range(num_filtered_chunk):
    for s in range(seqlen):
        if not gate_inf_mask[n, s]:
            assert chunk_end[n] <= s < batch_end[n]
# 选中块数的分段模式：8 个 0 | 8 个 1 | 4 个 2 | 8 个 0 | 5 个 1
expected = [0]*8 + [1]*8 + [2]*4 + [0]*8 + [1]*5
assert all(gate_mask.sum(dim=0)[h].tolist() == expected for h in range(num_head))
print("\n所有断言通过")
```

**操作步骤**：

1. 安装 PyTorch 后保存并运行 `python gate_practice.py`；
2. 核对打印的 `gate_inf_mask` 图案与 4.4.2 推导的可选区间一致；
3. 核对选中块数的分段模式（与随机种子无关，因为不足 k 的凑数项会被 `isinf` 过滤）；
4. 完成下表的 naive 行号对照（答案已给出，先自己填一遍再看）。

**需要观察的现象与预期结果**：掩码图案为——块 0 在 `s ∈ [8,20)` 放行；块 1 在 `s ∈ [16,20)` 放行；块 3 在 `s ∈ [28,33)` 放行；其余全屏蔽。两条不变式的断言全部通过。若 GPU 环境可用，可另行运行 `pytest tests/test_moba_attn.py` 观察完整前向中两份实现输出的对齐（本脚本不涉及 flash-attn）。

**naive 行号对照表（实践第 4 步的答案）**：

| 语义 | 高效实现（moba_efficient.py） | naive（moba_naive.py） |
|---|---|---|
| 候选块收集 | L325-L330 gather | L43-L50 循环 append + cat |
| 块代表向量（K 均值，原精度 mean） | L334-L340 `view + mean(dim=1)` | L43-L49 循环 `mean(dim=0)` |
| gate 打分（fp32） | L341-L347 einsum `"nhd,shd->nhs"` → `[N,H,S]` | L52-L55 einsum `"shd,nhd->hsn"` → `[H,S,N]` |
| 因果（当前块/未来块禁选） | L355、L357 `gate_chunk_end_mask` | L60 写 `-inf` |
| 当前块必选 | 无对应（self-attn 支路 + L313-L314 减一） | L61 写 `+inf` |
| 跨 batch 隔离 | L356、L358 `gate_batch_end_mask` | L34-L41 per-batch 循环切片（结构性隔离） |
| top-k（k 截断） | L314 `min(topk-1, N)` + L365 `dim=0` | L63-L64 `min(topk, num_block)` + `dim=-1` |
| 精确名单防平局/凑数 | L367-L371 `~isinf` + `scatter(dim=0)` | L66-L73 阈值 + `scatter(dim=-1)` +（L76-L81 token 级 tril 兜底） |

## 6. 本讲小结

- **filtered_kv gather**（L325-L330）：用「块内相对下标 + `cu_chunk` 起始偏移」构造绝对下标矩阵，一次 `index_select` 把不连续的候选块抽成 `[N×C, 2, H, D]` 稠密张量；合法性依赖 u3-l2 保证的「候选块恒满」。
- **key_gate_weight**（L334-L349）：`view(N, C, H, D) + mean(dim=1)` 向量化求块内 K 均值，与 naive 的循环版逐数值一致；gate 在 fp32 中打分、用完即还原原 dtype，且从不乘 `softmax_scale`（只影响排序）。
- **gate_chunk_end_mask**（L355、L357）：一次比较 `s < chunk_end[n]` 同时挡住当前块与未来块；「当前块必选」不再需要 `+inf`，由 self-attn 支路 + topk 减一接管。
- **gate_batch_end_mask**（L356、L358）：挡住「后 batch query 选前 batch 块」的反向泄漏，是压平 per-batch for 循环的必要代价；合并条件 `chunk_end[n] ≤ s < batch_end[b(n)]` 同时保证同 batch、严格过去，因此 MoBA 支路可用 `causal=False`。
- **gate_top_k_idx**（L365-L371）：`topk(dim=0)` 让每个 head 独立选块；`scatter` 名单 ∩ `~isinf` 处理平局与「合法候选不足 k」两种边角，比 naive 的「阈值 + tril」更干净，且两者产出相同的选择集合——这是 naive 能当黄金参考的前提。

## 7. 下一步学习建议

下一讲 **u3-l4（varlen trick：被选中的 Q 与 KV 的重组索引）** 将以本讲的 `gate_mask [N, H, S]` 为唯一输入，展示整个项目最精巧的索引变换：`nonzero` 得到 `moba_q_indices`、`sum(dim=-1)` 得到每个（块 × head）段的 `moba_seqlen_q`、`% seqlen` 与 `// seqlen` 的下标换算，以及零 expert 裁剪。建议先复习 `tensor.reshape/nonzero/index_select` 的行为，再把本讲综合实践脚本里的 `gate_mask` 打印出来，试着手工推导它的 `nonzero` 顺序（提示：按块 × head 分段排列 `[C0H0][C0H1]...[CnHm]`）。之后再进入 u3-l5 的 LSE 合并与 u3-l6 的自定义反向，回到注意力数学本身。
