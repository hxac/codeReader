# 稀疏与块稀疏注意力

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚「块稀疏注意力（block sparse attention）」要解决什么问题，以及它与稠密注意力在计算量、显存上的差异。
- 用 BSR（Block Sparse Row）格式描述一个稀疏掩码，并理解 `indptr` / `indices` / `block_mask` 三种输入分别对应什么场景。
- 看懂 `BlockSparseAttentionWrapper` 的 `plan`/`run` 如何**复用分页注意力（paged attention）基础设施**实现块稀疏——这是本讲最核心的设计巧思。
- 理解 BSR 掩码到 VSA（`q2k_index` / `q2k_num`）表示的转换，以及 Blackwell 原生 BSA 后端的约束。
- 会使用 `VariableBlockSparseAttentionWrapper` 处理「每个块大小不同、每个 KV 头掩码不同」的更一般场景。

本讲是「进阶注意力变体」单元的一篇，**前置讲义是 u3-l4（`BatchPrefillWithPagedKVCacheWrapper` 的 plan/run）**。我们会反复用到那里建立的 `qo_indptr`、页表三件套（`paged_kv_indptr` / `paged_kv_indices` / `paged_kv_last_page_len`）以及 `PrefillPlanInfo` 等概念，请确认你已经熟悉它们。

## 2. 前置知识

### 2.1 稠密注意力的代价

单头注意力的核心运算是：

\[
\mathrm{Attn}(Q,K,V) = \mathrm{softmax}\!\left(\frac{QK^\top}{\sqrt{d}}\right)V
\]

其中 \(Q\in\mathbb{R}^{M\times d}\)、\(K,V\in\mathbb{R}^{N\times d}\)。计算 \(QK^\top\) 的代价是 \(O(M\cdot N\cdot d)\)，它对序列长度是**平方**关系。当 \(M,N\) 很大（长上下文，例如 32k~128k token）时，这个平方项会成为显存与算力的主要瓶颈。

### 2.2 稀疏注意力：只算「该算的」

很多实际场景里，注意力矩阵天然是稀疏的——大量 query–key 对的注意力权重本应被掩掉（或可忽略）。典型例子：

- **因果掩码（causal）**：下三角，约一半元素为 0。
- **带状 / 局部窗口（band / sliding window）**：每个 query 只关注附近的若干 key。
- **块对角 / 分组（block-diagonal）**：序列被切成若干段，段间不交互。
- **学习到的稀疏模式**：如 MoBA、NSA 等只在选中的块上计算。

如果我们能把这些 0 整块跳过、根本不把它们送进 kernel，就能把平方代价降下来。**块稀疏注意力**就是干这件事的：它在「块」的粒度上描述哪些 query–key 块对需要计算，从而把计算量从 \(O(MNd)\) 降到 \(O(\rho\,MNd)\)，其中 \(\rho\in(0,1]\) 是非零块的比例（密度）。

### 2.3 BSR（Block Sparse Row）格式

FlashInfer 用 SciPy 的 [`bsr_matrix`](https://docs.scipy.org/doc/scipy/reference/generated/scipy.sparse.bsr_matrix.html) 格式来描述块稀疏掩码。把 \(M\times N\) 的掩码切成 \(R\times C\) 的小块后：

- `MB = ceil_div(M, R)`：行方向的块数。
- `NB = N // C`：列方向的块数（要求 `N` 能被 `C` 整除）。
- `indptr`：长度 `MB+1` 的「行指针」，CSR 风格。第 `i` 个块行所关注的列块号，存放在 `indices[indptr[i] : indptr[i+1]]`。
- `indices`：长度 `nnz`（非零块数）的「列块索引」数组。

> 一句话：`indptr` 划分出「每个块行管哪些列块号」，`indices` 是这些列块号的紧凑列表。这正是 OS 里 CSR 稀疏矩阵的标准三件套，只是粒度从「单个元素」变成了「\(R\times C\) 的块」。

如果你已经学过 u3-l2 的**页表三件套**，会发现 BSR 的 `indptr`/`indices` 与分页 KV 的 `paged_kv_indptr`/`paged_kv_indices` 形状与语义几乎一致——这不是巧合，而是本讲核心技巧的基础。

### 2.4 一个关键回忆：u3-l4 的 plan/run

分页 prefill wrapper 在 `plan` 阶段把变长批次展平成等粒度工作单元（切 Q、切 KV），产出一份 `PrefillPlanInfo`（15 个 int64），`run` 阶段只携带每层 Q/K/V 数据走 `paged_run`。本讲的经典后端会**把块稀疏问题映射成一个分页 prefill 问题**，所以 plan/run 的整体形状你会觉得很眼熟。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [flashinfer/sparse.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/sparse.py) | 两个 wrapper（`BlockSparseAttentionWrapper`、`VariableBlockSparseAttentionWrapper`）以及 BSR↔VSA 的转换函数，是本讲主线。 |
| [include/flashinfer/attention/scheduler.cuh](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/scheduler.cuh) | `plan` 的 C++ 调度层：`PrefillSplitQOKVIndptr`、`DecodeSplitKVIndptr`、`cost_function`、`PrefillPlanInfo`/`DecodePlanInfo` 等。 |
| [flashinfer/prefill.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/prefill.py) | 提供 `_compute_page_mask_indptr` 与 `get_batch_prefill_module`，经典后端直接复用。 |
| [flashinfer/quantization/packbits.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/quantization/packbits.py) | `segment_packbits`：把逐元素布尔掩码按段比特打包，用于「块内还有逐元素掩码」的场景。 |
| [flashinfer/utils.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py) | `MaskMode` 枚举（`NON_CAUSAL`/`CAUSAL`/`CUSTOM`）、`determine_attention_backend`。 |
| [flashinfer/cute_dsl/sparse/bsa_attn.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/cute_dsl/sparse/bsa_attn.py) | Blackwell 原生 BSA（blk128）kernel 的 Python 入口 `bsa_attn_fwd`。 |

---

## 4. 核心概念与源码讲解

### 4.1 块稀疏注意力与 BSR 掩码格式

#### 4.1.1 概念说明

`BlockSparseAttentionWrapper` 是 FlashInfer 对「块粒度稀疏注意力」的标准入口。它的掩码用 BSR 格式描述（见 2.3），并且**支持任意块大小 `(R, C)`**——这是它区别于 Blackwell 专用后端（只允许 64 或 128）的地方。

我们先看官方文档里那个最小例子，建立直观感受。给定一个 \(3\times 3\) 的掩码：

\[
\begin{bmatrix}0&0&1\\ 1&0&1\\ 0&1&1\end{bmatrix}
\]

取 `R=C=1`（每个「块」就是单个元素），那么 `MB=NB=3`，对应的 BSR 三件套为：

- `indptr = [0, 1, 3, 5]`：第 0 块行有 1 个非零块、第 1 块行有 2 个、第 2 块行有 2 个。
- `indices = [2, 0, 2, 1, 2]`：第 0 块行关注列块 2；第 1 块行关注列块 0、2；第 2 块行关注列块 1、2。

这与 [flashinfer/sparse.py:213-237](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/sparse.py#L213-L237) 的 docstring 示例完全一致。注意末尾它用 `single_prefill_with_kv_cache(q, k, v, custom_mask=mask)` 做稠密参照，断言两者结果一致——这说明**块稀疏在数值上等价于「稠密 attention + 同一个掩码」**，只是跳过了被掩掉的块。

#### 4.1.2 核心流程：从 BSR 到一次 `run`

使用流程是标准的 plan/run 两段式（承接 u3-l4）：

1. **构造 wrapper**：传入一块 float workspace（推荐 128MB），指定 `backend`（`auto`/`fa2`/`fa3`/`vsa_blackwell`/`vsa_blackwell_blk64`）。
2. **`plan(indptr, indices, M, N, R, C, num_qo_heads, num_kv_heads, head_dim, ...)`**：把 BSR 掩码翻译成内部数据结构（页表、可选的打包掩码、调度计划），结果可被同一前向的多个 attention 层复用。
3. **`run(q, k, v)`**：携带本层数据真正计算，返回 `[M, num_qo_heads, head_dim]`。

构造函数只做 workspace 准备，关键的几个内部缓冲区如下（[flashinfer/sparse.py:240-289](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/sparse.py#L240-L289)）：

```python
self._int_workspace_buffer = torch.empty((8 * 1024 * 1024,), dtype=torch.uint8, ...)
self._kv_lens_buffer = torch.empty((32768,), dtype=torch.int32, ...)
self._pin_memory_int_workspace_buffer = torch.empty(..., pin_memory=True, device="cpu")
```

其中 `_int_workspace_buffer` 用来存放 `plan` 产出的索引数组（`request_indices`、`kv_tile_indices` 等），`_pin_memory_int_workspace_buffer` 是它的**页锁定（pinned）CPU 镜像**——`plan` 在 CPU 上算好调度，再异步拷回 GPU，这套机制与 decode/prefill wrapper 完全相同。

掩码模式由 `MaskMode` 枚举决定（[flashinfer/utils.py:38-42](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py#L38-L42)）：

```python
class MaskMode(Enum):
    NON_CAUSAL = 0
    CAUSAL = 1
    CUSTOM = 2
    MULTIITEMSCORING = 3
```

- 不传 `mask` 且 `causal=False` → `NON_CAUSAL`（块级稀疏，块内全算）。
- 不传 `mask` 且 `causal=True` → `CAUSAL`。
- 传了逐元素 `mask`/`packed_mask` → `CUSTOM`（块级稀疏 + 块内还有逐元素细节）。

#### 4.1.3 源码精读：构造与掩码模式

构造函数初始化各缓冲区并把 `backend` 存下，留待 `plan` 里按后端分支处理（[flashinfer/sparse.py:259-289](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/sparse.py#L259-L289)）。

当用户提供了逐元素 `mask`（形状 `(nnz, R, C)`，即每个非零块内部还有自己的布尔掩码）时，需要把它转成 FlashInfer 的扁平布局并比特打包。布局转换函数 `convert_bsr_mask_layout`（[flashinfer/sparse.py:170-192](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/sparse.py#L170-L192)）把 BSR 的 `(nnz, R, C)` 数据按块行重排成与 prefill wrapper 期望一致的扁平序列；随后 `segment_packbits` 把每段 8 个 bit 压成 1 字节（小端序），`mask_mode` 被置为 `CUSTOM`。这条「块内逐元素掩码」路径相对小众，多数场景下块是「要么全算要么全跳过」，用不到它。

#### 4.1.4 代码实践：手搓一个带状 BSR 掩码

> **实践目标**：在 CPU 上手工构造一个带状（band）稀疏模式的 BSR 三件套，并把它展开成稠密布尔矩阵，确认你的 `indptr`/`indices` 描述的就是你想要的窗口。

**操作步骤**（示例代码，可在纯 CPU 上运行，无需 GPU）：

```python
# 示例代码：构造带状 BSR 掩码（CPU，可直接运行）
import torch

M = N = 16          # 序列长度
R = C = 4           # 块大小
MB = M // R         # 4
NB = N // C         # 4
band = 1            # 半窗口（块单位）：每个块行关注 [i-band, i+band] 的列块

indptr = [0]
indices = []
for i in range(MB):
    lo = max(0, i - band)
    hi = min(NB - 1, i + band)
    cols = list(range(lo, hi + 1))
    indices.extend(cols)
    indptr.append(len(indices))

indptr = torch.tensor(indptr, dtype=torch.int32)
indices = torch.tensor(indices, dtype=torch.int32)
print("indptr  =", indptr.tolist())   # 期望 [0, 2, 3, 3, 2] 附近
print("indices =", indices.tolist())

# 展开成稠密布尔矩阵，目视确认是带状
dense = torch.zeros(MB, NB, dtype=torch.bool)
for i in range(MB):
    s, e = indptr[i].item(), indptr[i+1].item()
    for j in indices[s:e].tolist():
        dense[i, j] = True
print(dense.int())
```

**需要观察的现象**：打印出的 `dense` 应当是沿对角线的带状 True 区域；`band=1` 时第 0 块行只覆盖列块 0、1，最后一块行只覆盖 2、3。

**预期结果**：`indptr` 长度为 `MB+1=5`；`indices` 中每个列块号都在 `[0, NB)` 范围内。若超出范围，4.2 里的 `plan` 会抛 `indices out of bound`。

#### 4.1.5 小练习与答案

**练习 1**：把上面的 `band` 改成 0（即每个块行只看自己对角那一个列块），`indptr` 和 `indices` 长度分别变成多少？

> **答案**：每个块行恰好 1 个非零块，共 `MB=4` 个。`indices` 长度为 4（值为 `[0,1,2,3]`），`indptr` 长度为 5（值为 `[0,1,2,3,4]`）。这是最稀疏的合法模式（每个块行至少要有一个块，否则该 query 行没有任何 key 可看）。

**练习 2**：若把 `R=C=1`、`band` 取使得每个块行覆盖约一半列块，此时的「密度」\(\rho\) 大致是多少？

> **答案**：\(\rho \approx (\text{每块行非零块数})/NB\)。例如 `MB=NB=16`、`band=8` 时每块行约 17 个块（受边界裁剪），\(\rho \approx 17/16 \approx 1\)（接近稠密）；`band=2` 时约 5 个，\(\rho \approx 5/16 \approx 0.31\)。密度越低，块稀疏相对稠密的收益越大。

---

### 4.2 经典后端：把 BSR 复用为分页页表

#### 4.2.1 概念说明：本讲最核心的巧思

`BlockSparseAttentionWrapper` 的经典后端（`auto`/`fa2`/`fa3`）并没有为块稀疏写一套全新的 kernel，而是做了一个极其优雅的**视角转换**：

> **块稀疏注意力 = 分页注意力**，其中：
> - 每个块行（block-row）= 一个「请求」；
> - 每个大小为 `C` 的 KV 块 = 一个「页」，`page_size = C`；
> - BSR 的 `indices` = 页表 `paged_kv_indices`（物理页号）；
> - BSR 的 `indptr` = 页表 `paged_kv_indptr`（每请求的页段指针）；
> - 该块行里的 `R` 个 query = 这个「请求」的 query 序列。

换句话说，**BSR 的 `indices` 数组本身就是页表**！这是可能的，因为 FlashInfer 的 KV 是按 token 顺序连续存放的，第 `j` 个大小为 `C` 的块恰好对应 `k[j*C:(j+1)*C]`，所以「列块号 `j`」天然就是「物理页号 `j`」。于是 4.1 的 BSR 掩码被无损地复用成了 u3-l4 的分页页表，块稀疏 kernel = 分页 prefill/decode kernel。

这个设计的好处显而易见：块稀疏**零新增 kernel**，直接继承分页注意力在变长、split-kv、GQA、多后端上的全部工程成果。

#### 4.2.2 核心流程：`plan` 的经典后端路径

`plan` 在经典后端里做四件事（[flashinfer/sparse.py:637-680](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/sparse.py#L637-L680)）：

1. **派生 `qo_indptr`**：每个块行贡献 `R` 个 query，故 `qo_indptr = R * [0,1,...,MB]`，末尾改成实际 `M`（最后一块行可能不满 `R`）。
2. **设定 `last_block_len`**：每个「页」都写满，即 `last_block_len = full((MB,), C)`，`page_size = C`。
3. **（可选）打包逐元素掩码**：若给了 `mask`/`packed_mask`，用 `_compute_page_mask_indptr` + `segment_packbits` 生成 `CUSTOM` 掩码（4.1.3）。
4. **二选一选 kernel**：cuda-core decode 实现 vs tensor-core prefill 实现（见下）。

派生 `qo_indptr` 的代码很短但很关键：

```python
num_blocks_row = len(indptr) - 1            # = MB
qo_indptr_host = R * torch.arange(num_blocks_row + 1, dtype=torch.int32)
qo_indptr_host[-1] = M                       # 最后一块行按实际 M 收尾
```

随后是 kernel 选择的判定（[flashinfer/sparse.py:692-696](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/sparse.py#L692-L696)）：

```python
if (
    R * (num_qo_heads // num_kv_heads) < 4
    and mask_mode != MaskMode.CUSTOM.value
    and q_data_type not in [torch.float8_e4m3fn, torch.float8_e5m2]
):
    self._use_tensor_cores = False            # cuda-core decode 实现
    self._cached_module = get_batch_decode_module(...)
else:
    self._use_tensor_cores = True             # tensor-core prefill 实现
    self._cached_module = get_batch_prefill_module(self._backend, ...)
```

直觉是：当「每块行 query 数 × GQA 组数」很小、又没有逐元素掩码、也不是 FP8 时，问题**不是 compute-bound**，用 cuda-core 的 decode 风格 kernel 更划算；否则用 tensor-core 的 prefill kernel。这正是 u3-l1 里「decode 是 memory-bound、prefill 是 compute-bound」判断的又一次复用。两种路径都把 `indptr`/`indices` 当作页表喂给 `plan`，例如 tensor-core 路径会算出每个请求（块行）的 KV 长度：

```python
kv_lens_arr_host = (kv_indptr_host[1:] - kv_indptr_host[:-1]) * self.C
```

即「该块行的页数 × 页大小」。然后调用 `self._cached_module.plan(...)`，进入 C++ 调度层。

#### 4.2.3 源码精读：`run` 的视角转换 + scheduler.cuh

`run` 里同样只需做一次「把连续 KV 切成页」的 reshape，就能直接调用 `paged_run`（[flashinfer/sparse.py:978-979](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/sparse.py#L978-L979)）：

```python
k = k.reshape(-1, self.C, *k.shape[-2:])      # [N,H,D] -> [NB, C, H, D]
v = v.reshape(-1, self.C, *v.shape[-2:])
```

reshape 之后，`k[j]` 就是物理页 `j`，与 `_paged_kv_indices_buf`（=BSR `indices`）里的页号一一对应。随后 tensor-core 路径调用 `paged_run`（[flashinfer/sparse.py:1008-1042](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/sparse.py#L1008-L1042)），把 workspace、plan_info、页表缓冲区、掩码缓冲区等一并传入——参数表与 u3-l4 的 paged prefill `run` 完全一致，因为它们就是同一个 kernel。

`plan` 调用的 C++ `PrefillPlan`（u3-l4 已见过）真正的「切分大脑」在 [include/flashinfer/attention/scheduler.cuh](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/scheduler.cuh)。其中 `PrefillSplitQOKVIndptr`（[scheduler.cuh:544-663](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/scheduler.cuh#L544-L663)）同时切 Q（`cta_tile_q`）与 KV（split-kv），把批次展平为若干个 `(request_idx, qo_tile_idx, kv_tile_idx)` 工作单元：

- 先按 `packed_qo_len = qo_len × gqa_group_size` 估出 Q tile 大小 `cta_tile_q`（[scheduler.cuh:593-605](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/scheduler.cuh#L593-L605)）。
- 再用二分搜索 `PrefillBinarySearchKVChunkSize`（[scheduler.cuh:101-130](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/scheduler.cuh#L101-L130)）找到一个 KV chunk 大小，使展平后的 `new_batch_size` 不超过 GPU 能容纳的 CTA 上限 `max_batch_size_if_split`。
- 最终回填 `request_indices`/`qo_tile_indices`/`kv_tile_indices`/`merge_indptr`/`o_indptr` 等数组，并乘上 `page_size` 得到以 token 为单位的 `kv_chunk_size`。

调度用的负载均衡代价函数非常简单（[scheduler.cuh:916](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/scheduler.cuh#L916)）：

```cpp
inline float cost_function(int qo_len, int kv_len) { return 2 * float(qo_len) + kv_len; }
```

它被 SM90/MLA 的贪心调度器（`MinHeap`）用来近似「这个工作单元有多贵」，从而把工作尽量均摊到各 CTA。对块稀疏而言，由于每个「请求」（块行）的 KV 长度由 BSR 的非零块数决定、长短不一，这套负载均衡尤其重要——否则稀疏模式里偶发的「超长块行」会拖慢整体。

切分结果被打包进 `PrefillPlanInfo`（[scheduler.cuh:665-741](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/scheduler.cuh#L665-L741)），它就是 u3-l4 里那张 15 个 int64 的偏移表（`padded_batch_size`、`cta_tile_q`、各索引数组在 int workspace 里的偏移、`split_kv` 标志等），`FromVector`/`ToVector` 负责它在 Python↔C++ 之间的序列化。decode 路径对应更小的 `DecodePlanInfo`（10 个 int64，[scheduler.cuh:366-422](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/scheduler.cuh#L366-L422)）与 `DecodeSplitKVIndptr`（[scheduler.cuh:348-364](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/scheduler.cuh#L348-L364)），逻辑同构。

#### 4.2.4 代码实践：带状块稀疏 vs 稠密的正确性与显存

> **实践目标**：用 4.1.4 构造的带状 BSR 掩码跑通 `BlockSparseAttentionWrapper`（经典后端），对照稠密 `single_prefill_with_kv_cache(custom_mask=...)` 验证数值一致，并比较两者的掩码元数据规模。

**操作步骤**（需要 CUDA GPU；具体耗时与显存数字**待本地验证**）：

```python
# 示例代码：带状块稀疏注意力
import torch, flashinfer

M = N = 4096
R = C = 64                      # NB = 64
num_qo_heads, num_kv_heads, head_dim = 32, 8, 128
band = 4                        # 每块行关注 [i-4, i+4] 共最多 9 个列块

MB = M // R
indptr = [0]; indices = []
for i in range(MB):
    cols = list(range(max(0, i-band), min(MB, i+band+1)))
    indices.extend(cols); indptr.append(len(indices))
indptr  = torch.tensor(indptr,  dtype=torch.int32, device="cuda")
indices = torch.tensor(indices, dtype=torch.int32, device="cuda")

q = torch.randn(M, num_qo_heads, head_dim, dtype=torch.float16, device="cuda")
k = torch.randn(N, num_kv_heads, head_dim, dtype=torch.float16, device="cuda")
v = torch.randn(N, num_kv_heads, head_dim, dtype=torch.float16, device="cuda")

ws = torch.empty(128*1024*1024, dtype=torch.uint8, device="cuda")
w  = flashinfer.BlockSparseAttentionWrapper(ws)   # backend 默认 auto
w.plan(indptr, indices, M, N, R, C, num_qo_heads, num_kv_heads, head_dim)
o_sparse = w.run(q, k, v)

# 稠密参照：把同样的带状掩码展开成稠密布尔矩阵
dense_mask = torch.zeros(M, N, dtype=torch.bool, device="cuda")
for i in range(MB):
    for j in indices[indptr[i].item():indptr[i+1].item()].tolist():
        dense_mask[i*R:(i+1)*R, j*C:(j+1)*C] = True
o_ref = flashinfer.single_prefill_with_kv_cache(q, k, v, custom_mask=dense_mask)

print("max abs diff:", (o_sparse - o_ref).abs().max().item())

# 显存元数据对比：稠密掩码是 M*N 个布尔，稀疏元数据是 indptr+indices
print("dense mask bytes:", dense_mask.numel())            # M*N
print("sparse meta bytes:", (indptr.numel()+indices.numel())*4)
```

**需要观察的现象**：
- `max abs diff` 应在 fp16 数值精度内（约 1e-2 量级），证明块稀疏与稠密等价。
- `dense mask bytes`（约 16M 字节）应远大于 `sparse meta bytes`（约几千字节）。注意这只是「掩码元数据」的对比；kernel 实际峰值显存的差异取决于 split-kv workspace，需用 `torch.cuda.max_memory_allocated` 分别测量（**待本地验证**）。

**预期结果**：数值一致；掩码元数据上稀疏远省。当 `band` 越小（密度越低），稀疏相对稠密的**计算量**收益越大，因为 kernel 真正读取的 KV token 数正比于 `nnz·C`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `plan` 里要把 `last_block_len` 全部填成 `C`，而不是像 decode wrapper 那样允许最后一个页不满？

> **答案**：因为块稀疏的「页」就是定长的 KV 块，`k[j*C:(j+1)*C]` 对每个 `j` 都恰好是 `C` 个 token——KV 张量本身按 `C` 整齐切片。只有最后一块行可能在 query 侧不满（由 `qo_indptr[-1]=M` 处理），KV 侧每个被 `indices` 引用的块都是完整的，故 `last_block_len=C`。

**练习 2**：若把 `num_qo_heads=32, num_kv_heads=8, R=1` 代入 kernel 选择条件 `R*(num_qo_heads//num_kv_heads) < 4`，会走哪条路径？

> **答案**：`1*(32//8)=4`，**不满足** `< 4`，走 tensor-core prefill 路径。这说明即便 `R=1`（最细粒度），GQA 组数较大时问题仍偏 compute-bound，用 tensor-core 更合适。

---

### 4.3 BSR/掩码到 VSA 的转换与 Blackwell 原生后端

#### 4.3.1 概念说明：为什么需要 VSA

经典后端用「分页注意力」伪装块稀疏，灵活（任意 `(R,C)`）但并非为稀疏**原生优化**。Blackwell（SM100/110）上有专门的 BSA（Block-Sparse Attention）tensor-core kernel，它在 tile 调度器里**直接遍历稀疏块索引列表**，而非遍历连续页。这种 kernel 需要一种不同的稀疏表示——**VSA（Variable-block Sparse Attention）格式**：

- `q2k_index`：形状 `[1, H, MB, NB]`（或带 GQA tiling 的变体），int32。对每个 Q 块，列出它关注的 KV 块号，末尾用 `-1` 填充。
- `q2k_num`：形状 `[1, H, MB]`，int32。每个 Q 块**实际**关注多少个 KV 块（即 `q2k_index` 里非 `-1` 的个数）。

之所以要 `q2k_num`，是因为不同 Q 块关注的 KV 块数不同，定长的 `q2k_index` 用 `-1` 填齐到 `max_nnz`，真实长度由 `q2k_num` 给出。

FlashInfer 提供两个 Blackwell 后端：

| 后端 | 架构 | 块大小 | head_dim | dtype | GQA |
|------|------|--------|----------|-------|-----|
| `vsa_blackwell` | SM100/SM110 | `R=C=128` | 64/96/128 | fp16/bf16 | pack_gqa |
| `vsa_blackwell_blk64` | 仅 SM100 | `R=C=64` | 128 | bf16 | 普通 |

它们**不支持**逐元素块内掩码、causal、RoPE/ALIBI、logits_soft_cap（见 [flashinfer/sparse.py:463-481](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/sparse.py#L463-L481) 的约束检查），因为原生 BSA kernel 只认「块级」稀疏。

#### 4.3.2 核心流程：两种输入 → VSA

`plan` 的 VSA 分支接受两种输入（[flashinfer/sparse.py:519-535](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/sparse.py#L519-L535)）：

1. **头无关 BSR（`indptr`/`indices`）**：所有头共享同一稀疏模式。用 `_bsr_to_vsa_index` 把它广播到每个头。
2. **逐头块掩码（`block_mask`，形状 `[H, MB, NB]` 布尔）**：每个头有自己的模式。用 `_block_mask_to_vsa_index` 转换。

对于 GQA（`num_qo_heads > num_kv_heads`），`vsa_blackwell`（blk128）采用 **pack_gqa** 模式：tile 调度器按 KV 头遍历、并把 `m_block` 维度扩展 `qhead_per_kvhead` 倍，因此 `q2k_index` 的块索引头维是 `num_kv_heads`、Q 块维是 `qhead_per_kvhead * MB`。转换函数里用 `repeat_interleave` 做这个 tiling（[flashinfer/sparse.py:89-95](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/sparse.py#L89-L95)）。

> ⚠️ 一个重要限制（[flashinfer/sparse.py:495-499](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/sparse.py#L495-L499)）：blk128 的 pack_gqa 每个 KV 头只持有一份块索引列表，所以**同一 KV 头组内的所有 QO 头必须共享相同的稀疏模式**。若传入 `(num_qo_heads, MB, NB)` 的逐头掩码，代码会**只取每组第一个 QO 头**（`block_mask[::qhead_per_kvhead]`）静默降级，组内其余头的差异被忽略。使用逐头掩码时务必保证组内一致。

#### 4.3.3 源码精读：两个转换函数

`_bsr_to_vsa_index`（[flashinfer/sparse.py:45-111](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/sparse.py#L45-L111)）把头无关 BSR 转成 VSA。核心是把每个块行的 `indices` 段写进一个 `MB×NB` 的「-1 填充」矩阵，再广播到所有头：

```python
q2k_index_flat = torch.full((MB, NB), -1, dtype=torch.int32)
q2k_num_flat = (indptr_cpu[1:] - indptr_cpu[:-1]).to(torch.int32)
for i in range(MB):
    s, e = indptr_cpu[i].item(), indptr_cpu[i+1].item()
    if e > s:
        q2k_index_flat[i, : e - s] = indices_cpu[s:e]
# 广播到所有头：[1, H, MB_packed, NB]
q2k_index = q2k_index_flat.unsqueeze(0).unsqueeze(0).expand(1, num_heads, -1, -1).contiguous()
```

`_block_mask_to_vsa_index`（[flashinfer/sparse.py:114-167](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/sparse.py#L114-L167)）处理逐头掩码，做法更「向量化」：先用 `argsort(~block_mask, stable=True)` 把 `True` 的列块号排到前面，再按每行实际的 `q2k_num` 截断、其余填 `-1`：

```python
q2k_num = block_mask_cpu.sum(dim=-1).to(torch.int32)      # [H, MB]
max_nnz = int(q2k_num.max().item())
sorted_idx = torch.argsort(~block_mask_cpu, dim=-1, stable=True)[:, :, :max_nnz]
valid = torch.arange(max_nnz).unsqueeze(0).unsqueeze(0) < q2k_num.unsqueeze(-1)
q2k_index = torch.where(valid, sorted_idx, torch.full_like(sorted_idx, -1)).to(torch.int32)
```

转换完成后，`run` 的 VSA 分支（[flashinfer/sparse.py:884-922](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/sparse.py#L884-L922)）把 NHD 布局unsqueeze 成 BSA kernel 期望的 BSHD（`[1, M, H, D]`），调用 CuTe-DSL 的 `bsa_attn_fwd`（[flashinfer/cute_dsl/sparse/bsa_attn.py:62-96](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/cute_dsl/sparse/bsa_attn.py#L62-L96)），再把输出从 `[1,H,M]` 的 lse 转回 `[M,H]`。注意 `block_sparse_num` 参数在提供了 `q2k_block_nums` 时被忽略，真正起作用的是 `q2k_block_nums=self._vsa_q2k_num`。

#### 4.3.4 代码实践：手算一个 VSA 转换

> **实践目标**：用一个小例子手工验证 `_bsr_to_vsa_index` 的输出形状与填充逻辑，不依赖 Blackwell GPU。

**操作步骤**（CPU 即可）：

```python
# 示例代码：手工复现 _bsr_to_vsa_index 的形状（CPU）
import torch

MB, NB, num_heads = 3, 4, 2
indptr  = torch.tensor([0, 2, 2, 5])           # 第 1 块行为空
indices = torch.tensor([0, 3,      1, 2, 3])   # 第 0/2 块行的列块号

q2k_index_flat = torch.full((MB, NB), -1, dtype=torch.int32)
for i in range(MB):
    s, e = indptr[i].item(), indptr[i+1].item()
    if e > s:
        q2k_index_flat[i, : e-s] = indices[s:e]

q2k_num = (indptr[1:] - indptr[:-1]).to(torch.int32)
q2k_index = q2k_index_flat.unsqueeze(0).unsqueeze(0).expand(1, num_heads, -1, -1).contiguous()
print(q2k_index_flat)
print("q2k_num =", q2k_num.tolist(), " shape =", tuple(q2k_index.shape))
```

**需要观察的现象**：第 1 块行（`indptr` 差为 0）对应 `q2k_index_flat[1]` 全 `-1`、`q2k_num[1]=0`；输出形状为 `[1, 2, 3, 4]`。

**预期结果**：`q2k_num = [2, 0, 5]`？注意 `indices` 只有 5 个元素，第 2 块行占 3 个 → `q2k_num=[2,0,3]`，`q2k_index_flat[2]=[1,2,3,-1]`。若你的 `indices` 段长超过 `NB` 会越界——真实函数在 [flashinfer/sparse.py:69-75](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/sparse.py#L69-L75) 有 `indices out of range [0, NB)` 的校验。

#### 4.3.5 小练习与答案

**练习 1**：为什么 VSA 要同时存 `q2k_index` 和 `q2k_num`，而不是只用一个变长列表？

> **答案**：GPU kernel 需要定长张量才能高效索引（每个 thread block 按固定的 `max_nnz` 步长读取）。变长列表无法直接在 tensor-core kernel 里高效遍历。所以用 `-1` 填齐到 `max_nnz`，真实长度由 `q2k_num` 给出，kernel 遇到 `-1` 或达到 `q2k_num` 即停止。

**练习 2**：`vsa_blackwell` 要求 `R=C=128`，而经典后端允许任意 `(R,C)`。如果你的稀疏模式天然是 64 粒度，能用 `vsa_blackwell` 吗？

> **答案**：不能直接用 blk128（它强制 128 粒度）。你可以把 64 粒度的模式**合并**成 128 粒度（每 2×2 个 64-块合并成一个 128-块，只要其中有非零就置为非零），但会损失一些稀疏度；或者改用 `vsa_blackwell_blk64`（若你在 SM100 且满足 head_dim=128、bf16 约束），它正好是 64 粒度。

---

### 4.4 可变块稀疏注意力

#### 4.4.1 概念说明：块大小不再固定

`VariableBlockSparseAttentionWrapper` 解决更一般的情形：

- **每个块的大小可以不同**：用 `block_row_sz[h, i]` 给出第 `h` 个 KV 头第 `i` 个块行的行数，`block_col_sz[h, j]` 给出列块 `j` 的列数。
- **每个 KV 头可以有自己独立的稀疏模式**：`block_mask_map[h, i, j]` 是逐头的 `[num_kv_heads, MB, NB]` 布尔掩码。

典型场景是某些定制化注意力（如 MoBA、分组注意力的变体）里，序列被切成不等长的段、不同头关注不同段。由于块大小不固定，无法像经典后端那样把 `C` 当成统一 `page_size`，于是它采用了一个不同的视角：**`page_size = 1`（token 级页）**，把变长块展开成 token 级 CSR。

#### 4.4.2 核心流程：块掩码 → token 级 CSR + GQA 打包

`plan` 的关键函数 `_block_mask_map_to_expanded_indices`（[flashinfer/sparse.py:1292-1337](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/sparse.py#L1292-L1337)）把块级掩码展开成 token 级的 `kv_indptr` / `kv_indices`：

1. 每个块行的「token 长度」= 该行选中列块的 `block_col_sz` 之和（[sparse.py:1308](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/sparse.py#L1308)），据此累加得到 `kv_indptr`。
2. 计算每个列块在其头内的全局 token 偏移 `col_offset` 与 `head_offset`。
3. 用 `block_mask_map.nonzero()` 找出所有选中的 `(h, r, c)`，按 `block_col_sz[h,c]` 把每个列块**展开成一串连续 token 索引**写入 `kv_indices`（[sparse.py:1329-1333](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/sparse.py#L1329-L1333)）。

随后它用与经典后端相同的 `get_batch_prefill_module` + `plan` 机制（`page_size=1`），但有一个额外的 **GQA 打包**技巧：因为每个 KV 头的稀疏模式不同，它把 `num_kv_heads` 折进「序列」维度，让 `gqa_group_size = num_qo_heads // num_kv_heads` 充当 kernel 的「头数」（[sparse.py:1413-1416](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/sparse.py#L1413-L1416)）：

```python
num_qo_heads // num_kv_heads,  # num_qo_heads (gqa_group_size)
1,                             # num_kv_heads（被打平进序列）
1,                             # page_size
```

`run` 里对应地用 `einops.rearrange` 把输入 Q/K/V 重排到这个打包布局（[flashinfer/sparse.py:1526-1539](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/sparse.py#L1526-L1539)），算完再 rearrange 回 `[num_qo_heads, qo_len, head_dim]`。注意它的输入布局是 **HND**（`[num_qo_heads, qo_len, head_dim]`），与 `BlockSparseAttentionWrapper` 的 NHD（`[M, num_qo_heads, head_dim]`）相反，这是两个 wrapper 最容易踩的坑。

> 说明：`_block_mask_map_to_expanded_indices` 目前是纯 PyTorch 实现，作者在 [sparse.py:1291](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/sparse.py#L1291) 留了 `NOTE(Yilong): This could be perf bottleneck. Consider Triton implementation.`——当头数与块数很大时，`plan` 本身可能不便宜。

#### 4.4.3 源码精读

`plan` 主体在 [flashinfer/sparse.py:1262-1342](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/sparse.py#L1262-L1342)。注意它强制 `page_size == 1`（`last_block_len` 全填 1，注释 [sparse.py:1286](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/sparse.py#L1286)），随后若干断言校验形状一致性（`num_kv_heads` 与三个张量的第 0 维匹配、`kv_indptr[-1] == len(kv_indices)` 等）。它**只走 tensor-core prefill 路径**（没有 decode 分支、没有逐元素掩码、没有 VSA 分支），backend 仅在 `fa2`/`fa3` 间由 `determine_attention_backend` 自动选择。

#### 4.4.4 代码实践：跑一个可变块稀疏例子

> **实践目标**：参照 docstring 示例（[flashinfer/sparse.py:1086-1104](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/sparse.py#L1086-L1104)）跑通 `VariableBlockSparseAttentionWrapper`，并用稠密参照验证。

**操作步骤**（需要 CUDA GPU；数值**待本地验证**）：

```python
# 示例代码：可变块稀疏注意力
import torch, flashinfer

num_qo_heads = num_kv_heads = 1
head_dim = 128
seq_len = 6

ws = torch.empty(128*1024*1024, dtype=torch.uint8, device="cuda")
w  = flashinfer.VariableBlockSparseAttentionWrapper(ws)

# block_mask_map[h, i, j]：第 h 头、块行 i 是否关注列块 j
block_mask_map = torch.tensor([[[0,0,1],[1,0,1],[0,1,1]]], dtype=torch.bool, device="cuda")
block_row_sz   = torch.tensor([[1,2,3]], dtype=torch.int32, device="cuda")  # 每块行行数
block_col_sz   = torch.tensor([[3,1,2]], dtype=torch.int32, device="cuda")  # 每列块列数

w.plan(block_mask_map, block_row_sz, block_col_sz,
       num_qo_heads, num_kv_heads, head_dim)

# 注意 HND 布局：[num_qo_heads, qo_len, head_dim]
q = torch.randn(num_qo_heads, seq_len, head_dim, dtype=torch.float16, device="cuda")
k = torch.randn(num_kv_heads, seq_len, head_dim, dtype=torch.float16, device="cuda")
v = torch.randn(num_kv_heads, seq_len, head_dim, dtype=torch.float16, device="cuda")
o = w.run(q, k, v)
print(o.shape)   # 期望 [num_qo_heads, seq_len, head_dim]
```

**需要观察的现象**：`block_row_sz` 之和（1+2+3=6）与 `block_col_sz` 之和（3+1+2=6）都等于 `seq_len`，这是必需的——块要恰好铺满序列。若不等，`plan` 内部断言或后续 reshape 会失败。

**预期结果**：输出形状 `[1, 6, 128]`。可用 `test_block_sparse.py` 里 `_ref_attention`（[tests/attention/test_block_sparse.py:142-175](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/attention/test_block_sparse.py#L142-L175)）的同款思路做稠密参照：把块掩码用 `repeat_interleave` 展开成元素级掩码，再调 `single_prefill_with_kv_cache(custom_mask=...)` 比对。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `VariableBlockSparseAttentionWrapper` 只支持 tensor-core prefill 后端，而没有像 `BlockSparseAttentionWrapper` 那样的 cuda-core decode 分支？

> **答案**：可变块稀疏天然是「每个块行有多个 query、每个块行 KV 长度不同」的变长批次，本质是 prefill（compute-bound）问题，且 `page_size=1` 让每个「请求」的页数等于其 token 长度，长短差异极大，更适合 tensor-core 的 split-kv 调度。decode 分支的前提是「每请求单个 query」，与可变块的多 query 语义不符。

**练习 2**：两个 wrapper 的 Q 张量布局分别是什么？

> **答案**：`BlockSparseAttentionWrapper` 是 NHD：`[M, num_qo_heads, head_dim]`；`VariableBlockSparseAttentionWrapper` 是 HND：`[num_qo_heads, qo_len, head_dim]`（见 [sparse.py:1478](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/sparse.py#L1478)）。混用会报形状错误。

---

## 5. 综合实践

把本讲三个最小模块串起来，设计一个「**为同一个稀疏模式选择三种执行路径并对比**」的小任务。

**场景**：长序列 `M=N=8192`，带状稀疏（`band=8` 个 64-块），`num_qo_heads=32, num_kv_heads=8, head_dim=128, bf16`。请完成：

1. **构造 BSR 掩码**：用 4.1.4 的方法生成 `indptr`/`indices`（`R=C=64`）。
2. **路径 A（经典后端）**：`BlockSparseAttentionWrapper(backend="auto")`，跑 `plan`/`run`，记录输出与首次 JIT 编译耗时。
3. **路径 B（稠密参照）**：展开成稠密掩码，`single_prefill_with_kv_cache(custom_mask=...)`，验证与路径 A 数值一致。
4. **路径 C（VSA，仅 Blackwell）**：若你在 SM100/110 上，把同一掩码用 `BlockSparseAttentionWrapper(backend="vsa_blackwell_blk64")` 再跑一次（需 `R=C=64`、`head_dim=128`、bf16），对比输出与耗时。若不在 Blackwell 上，跳过并说明原因。
5. **显存对比**：用 `torch.cuda.max_memory_allocated()` 分别测量三种路径的峰值显存，并测量「掩码元数据」的字节数（`indptr`+`indices` vs 稠密 `M×N` 布尔）。

**交付物**：
- 一张表：三条路径的「最大数值误差 / 峰值显存 / kernel 耗时」。
- 一段话：解释为什么路径 A 能复用分页注意力、路径 C 为什么需要先做 BSR→VSA 转换。
- （选做）把 `band` 从 1 扫到 32，画出路径 A 的耗时随密度 \(\rho\) 的变化曲线，验证「越稀疏越省」。

> 注意：所有 GPU 计时与显存数字**待本地验证**。本实践的核心是理解三条路径的**关系**，而非跑出某个绝对数字。

## 6. 本讲小结

- **块稀疏注意力**用 BSR 格式（`indptr`/`indices`）在块粒度描述掩码，把计算量从 \(O(MNd)\) 降到 \(O(\rho MNd)\)，是长上下文场景的关键优化。
- **经典后端的核心巧思**：块稀疏 = 分页注意力。BSR 的 `indices` 直接当作页表 `paged_kv_indices`，`page_size = C`，从而零新增 kernel 地复用 u3-l4 的 paged prefill/decode 基础设施。
- **kernel 选择**由 `R*(num_qo_heads//num_kv_heads) < 4` 等条件决定走 cuda-core decode 还是 tensor-core prefill，对应 `DecodePlanInfo`/`PrefillPlanInfo` 两套调度，真正的切分逻辑在 `scheduler.cuh` 的 `PrefillSplitQOKVIndptr`/`DecodeSplitKVIndptr`。
- **Blackwell 原生后端**（`vsa_blackwell`/`vsa_blackwell_blk64`）用专门的 BSA tensor-core kernel，需要先把 BSR/逐头掩码转成 VSA 的 `q2k_index`/`q2k_num`（`_bsr_to_vsa_index`/`_block_mask_to_vsa_index`），约束更严（固定块大小、不支持 causal/RoPE/soft_cap）但原生优化。
- **可变块稀疏**（`VariableBlockSparseAttentionWrapper`）处理「块大小不定、逐头模式不同」，用 `page_size=1` 把变长块展开成 token 级 CSR，并把 `num_kv_heads` 折进序列维度实现 GQA；输入是 HND 布局，与块稀疏 wrapper 的 NHD 相反。
- 逐元素块内掩码（`mask`/`packed_mask` + `segment_packbits`）是「块级稀疏 + 块内细节」的高级用法，对应 `MaskMode.CUSTOM`。

## 7. 下一步学习建议

- **u4-l4（POD 混合批处理）** 与 **u4-l5（BatchAttention 统一混合批处理）**：它们同样建立在分页注意力之上，理解了本讲「块稀疏 = 分页」的视角后，你会更容易看清它们如何在一个批次里混合 prefill/decode。
- **深入调度层**：重读 [scheduler.cuh](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/include/flashinfer/attention/scheduler.cuh) 的 `PrefillSM90Plan`（FA3 的 MinHeap 贪心调度）与 `MLAPlan`，对比本讲的 `PrefillSplitQOKVIndptr`，体会「同一调度框架如何服务不同注意力变体」。
- **Blackwell 原生 kernel**：若你有 SM100 硬件，阅读 [flashinfer/cute_dsl/sparse/bsa_attn.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/cute_dsl/sparse/bsa_attn.py) 及其 `blk128`/`blk64` 实现，结合 CuTe-DSL 文档理解 tile 调度器如何遍历 `q2k_index`。
- **测试参照**：[tests/attention/test_block_sparse.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/attention/test_block_sparse.py) 与 [tests/attention/test_vsa_block_sparse.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/tests/attention/test_vsa_block_sparse.py) 提供了从随机 BSR 到带状模式的完整用例，是写自定义稀疏模式的最佳模板。
