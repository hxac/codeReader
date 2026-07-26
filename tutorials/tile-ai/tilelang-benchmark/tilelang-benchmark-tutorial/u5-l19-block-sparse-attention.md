# block-sparse attention

> 本讲对应讲义 id：`u5-l19`，依赖 `u5-l16`（FlashAttention：在线 softmax 与 macro 结构）。
> 唯一关键源码：`hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py`，配合驱动脚本 `benchmark_tilelang_bsa.sh`。

## 1. 本讲目标

本讲是「Attention 系列」单元的最后一篇，在 `u5-l16`（稠密 FlashAttention）的基础上专讲一个变体：**block-sparse attention（块级稀疏注意力）**。读完本讲，你应当能够：

1. 说清「块级稀疏」与「元素级稀疏」的区别——为什么稀疏掩码必须**按 `block_size × block_size` 的整块**取舍，才能在 GPU 内核里真正「跳过计算、省下算力与带宽」。
2. 读懂掩码是如何**生成**的：用一张「下采样」到块粒度的分数矩阵 `x_ds`，经 `torch.topk`（或阈值）选出每行最重要的若干块，再 `tril_()` 套上因果下三角，得到形状为 `[batch, heads, downsample_len, downsample_len]` 的 `BlockSparseMask`。
3. 写出 `downsample_len` 与 `block_size`、`seq_len` 三者的关系：\(\texttt{downsample\_len} = \lceil \texttt{seq\_len} / \texttt{block\_size} \rceil\)，并理解它同时是 K 循环的迭代上界。
4. 解释内核里三处相对稠密版本的**新增结构**——多了一个 `BlockSparseMask` 入参、用 `T.alloc_local` 预载一行掩码、以及在 K 循环里用 `if block_mask[k] != 0:` 把 `MMA0/Softmax/Rescale/MMA1` 四段**整体条件执行**——以及为什么掩码要放在 `alloc_local`（线程私有寄存器）而不是 `shared` 或 `fragment`。
5. 掌握稀疏场景下的 **FLOPS 折算**：稠密算量乘以 \((1 - \texttt{sparsity})\)（再按 causal 乘 0.5）得到「有效算量」，从而把延迟换算成有意义的 TFlops。

本讲**不**重复讲解在线 softmax 的数学证明、`exp2` 折 \(\log_2 e\) 的指令优化、fragment/shared 缓冲分配等已在 `u5-l16` 讲透的内容——它们在本内核里**几乎一字不差**地复用，本讲只点出「块稀疏改了什么」。

## 2. 前置知识

### 2.1 承接 u5-l16：稠密 FlashAttention 的四段 macro

`u5-l16` 讲过，TileLang 版 FlashAttention 把每个 K 块的计算拆成 4 个 `@T.macro`，在 K 维流水循环里依次调用：

```
for k in T.Pipelined(loop_range, num_stages=2):
    MMA0(...)      # acc_s  = Q · K_k^T   (本块分数)
    Softmax(...)   # 在线 softmax, 更新 scores_max / logsum, 产出 acc_s_cast
    Rescale(...)   # acc_o *= scores_scale (把旧输出迁到新参考最大值)
    MMA1(...)      # acc_o += acc_s_cast · V_k
```

在线 softmax 的核心结论是：只要分子 `acc_o` 与分母 `logsum` 始终被**同一个** `scores_scale` 同步缩放，循环结束后 `acc_o /= logsum` 就能把任意参考最大值约掉，得到精确 softmax。**本讲的块稀疏内核，这套数学完全不变**——它只是「有选择地」执行上述四步。如果你对四段 macro 的分工还不够熟，建议先回看 `u5-l16` 的 4.1–4.3 节。

### 2.2 为什么要稀疏注意力（要解决什么）

标准注意力对长度为 \(n\) 的序列，要算一张 \(n \times n\) 的分数矩阵 \(S = QK^\top\)，算力与序列长度的平方成正比。当 \(n\) 很大（例如 64K）时，\(O(n^2)\) 的算力与中间结果显存都成为瓶颈。

但实际中很多 attention 是**稀疏**的——对每个 query，只有少数 key 真正重要（分数显著大）。如果能提前知道「哪些 query–key 块可以不算」，就能按比例省下算力。block-sparse attention 就是把这种「先验稀疏结构」喂给内核，让它在整块级别**跳过**不重要的计算。

> 直觉：稠密 attention 对每个 query 都遍历**所有** key 块；块稀疏 attention 给每个 query 块一张「只看这些 key 块」的清单，内核只对清单上的块做 MMA。

### 2.3 为什么是「块级」稀疏，而不是元素级

理论上最细的稀疏是「元素级」——逐个 query–key 标注算不算。但 GPU 上算子是**分块**执行的（一个 block 算 `block_M × block_N` 的一块），TensorCore 的 MMA 指令也以块为最小单位。如果掩码粒度比 block 还细，就会出现「同一块里有的元素要算、有的不要算」的情况——这时**整块还是得算**（用 \(-\infty\) 屏蔽不要的元素，即 `u5-l17` 讲的 causal 掩码做法），并不能跳过 MMA、省不下算力。

所以要让「跳过 MMA」真正生效，掩码必须**以整个 `block_size × block_size` 块为单位**取舍：要么整块算，要么整块不算。这就是「block-sparse」的由来，也是本内核把掩码「下采样」到块粒度的根本原因。

## 3. 本讲源码地图

本讲引用两个文件，互为表里：内核定义算什么，shell 决定跑哪些形状。

| 文件 | 作用 |
|---|---|
| [`benchmark_tilelang_bsa.py`](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py) | TileLang 块稀疏 FlashAttention 内核。`get_sparse_attn_mask_from_topk`（L11-L23）/`get_sparse_attn_mask_from_threshold`（L26-L31）生成块掩码；`blocksparse_flashattn`（L34-L168）定义内核，其中 `@T.prim_func`（L118-L166）相比稠密版多了 `BlockSparseMask` 入参、`block_mask` 的 `alloc_local`（L140）与预载循环（L147-L148）、以及 K 循环里的 `if block_mask[k] != 0` 条件守卫（L154-L160）；`__main__`（L171-L222）拼形状、生成掩码、`tilelang.compile` 编译并计时。 |
| [`benchmark_tilelang_bsa.sh`](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.sh) | 驱动脚本，对 `seq_len ∈ {1024,2048,4096,8192}` 四档（固定 `batch=64, heads=64, dim=128, block_size=128, sparsity=0.5, causal=True`）各跑一次内核，日志 `tee` 到 `logs/bsa_<seq>.log`。 |

> ⚠️ **目录命名不一致（承接前几讲「以代码为准」的提醒）**：本算子目录 `hopper_benchmark/blocksparse_attention/` 下，Triton 基线在 `3.triton-benchmark`、TileLang 在 `1.tilelang-benchmark`（注意与 dense_matmul 的 `3.tilelang-benchmark` 编号不同）；torch 基线目录名拼错成 `2.torch-becnhmark`（`becnhmark`）。此外，与 `dense_matmul` 不同，**本算子没有顶层 `benchmark.sh` 编排脚本，也没有 `data/`、`plot_*.py` 可视化管线**——它是一个相对独立的「单内核跑分」，各 provider 子目录自带各自的 `.sh`。读源码定位文件时以 `ls` 为准。

---

## 4. 核心概念与源码讲解

本讲按 4 个最小模块展开：

- **4.1** topk / 阈值：块级掩码的生成
- **4.2** `BlockSparseMask`：从 host 张量到内核入参（`downsample_len` 与 `block_size` 的关系）
- **4.3** 条件 MMA：`alloc_local` 预载 + `if block_mask[k] != 0` 跳过
- **4.4** 稀疏 FLOPS 折算与基准口径

### 4.1 topk / 阈值：块级掩码的生成

#### 4.1.1 概念说明

要让内核「按块跳过」，先得有一张「每个 query 块该看哪些 key 块」的清单。这张清单在 host 端（CPU/PyTorch 侧）预先算好，再作为张量传进内核。本文件提供两种生成策略：

- **topk 策略**：给每个 query 块，从所有 key 块里挑「分数最高的 `topk` 个」。这里的「分数」来自一张**下采样分数矩阵** `x_ds`——它是真实 attention 分数的粗粒度近似（实际系统里通常由一次廉价的下采样 attention pass 得到；本基准里为简化直接用 `torch.randn` 占位）。
- **阈值策略**：保留所有分数大于某 `threshold` 的块，个数不固定。

两种策略都最后套一个 `tril_()`（下三角化）来兼容 causal：因果注意力里「未来的 key」本来就不该看，直接把对应块置 False。

#### 4.1.2 核心流程

topk 策略的生成步骤（对应 [`get_sparse_attn_mask_from_topk`](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L11-L23)）：

```
输入 x_ds: [bsz, num_head, downsample_len, downsample_len]   # 下采样块分数
  │
  ├─ torch.topk(x_ds, topk, dim=-1).indices   # 每行选出 topk 个 key 块的下标
  ├─ dense_mask = 全 False 的 [bsz,num_head,downsample_len,downsample_len]
  ├─ dense_mask.scatter_(-1, indices, True)    # 在选中的位置填 True
  ├─ (可选) use_dense_for_last_block: 把最后 2 个 query 块强制全 True
  └─ dense_mask.tril_()                         # 套因果下三角
        │
        ▼
  BlockSparseMask: [bsz, num_head, downsample_len, downsample_len], dtype=bool
```

两个关键点：

1. **`topk` 决定密度**：`topk` 越大，保留的块越多（越接近稠密）。`topk` 由目标稀疏度反推（见 4.4 节）：\(\texttt{TOPK} = \lceil (1 - \texttt{sparsity}) \cdot \texttt{seq\_len} / \texttt{block\_size} \rceil\)。
2. **`scatter_` + `tril_`**：先用 `scatter_` 把选中下标写成 True，再用 `tril_` 抹掉上三角。注意顺序——先选 topk 再 tril，意味着上三角里被选中的块也会被清掉，所以**实际保留下三角的块数可能少于 topk**（尤其靠后的 query 行）。这是一个「目标密度 vs 实际密度」的微小偏差，4.4 节会讨论它对 FLOPS 口径的影响。

#### 4.1.3 源码精读

[`get_sparse_attn_mask_from_topk`](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L11-L23)：

```python
def get_sparse_attn_mask_from_topk(x, topk, use_dense_for_last_block=False):
    bsz, num_head, downsample_len, _ = x.shape
    # N_CTX = downsample_len * BLOCK
    sparse_index = torch.topk(x, topk, dim=-1).indices       # 每行 topk 个 key 块下标
    dense_mask = torch.full([bsz, num_head, downsample_len, downsample_len],
                            False, dtype=torch.bool, device=x.device)
    dense_mask.scatter_(-1, sparse_index, True)              # 选中位置置 True
    if use_dense_for_last_block:
        dense_mask[:, :, -2:, :] = True                      # 最后 2 个 query 块强制稠密
    dense_mask.tril_()                                       # 因果下三角
    return dense_mask
```

- 注释 `# N_CTX = downsample_len * BLOCK`（[L13](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L13)）一语道破下采样的本质：原始序列长度 `N_CTX` 被按 `BLOCK` 压缩成 `downsample_len` 个块。
- `use_dense_for_last_block`（[L20-L21](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L20-L21)）：把最后两个 query 块对应整行强制置 True。这对应某些稀疏 attention 设计里「靠后的 token 保留全连接」的做法；本基准的 `__main__` 调用时**没有**传这个参数（[L207](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L207)），故默认关闭。

阈值策略 [`get_sparse_attn_mask_from_threshold`](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L26-L31) 结构几乎相同，只是把 `topk` 换成 `x > threshold`，每行保留的块数随分数分布而变：

```python
def get_sparse_attn_mask_from_threshold(x, threshold, use_dense_for_last_block=False):
    dense_mask = x > threshold            # 阈值筛选, 个数不固定
    ...
    dense_mask.tril_()
    return dense_mask
```

**一处「保证每行至少有一块」的关键细节**：`__main__` 生成 `x_ds` 后，紧接着把第 0 列强行设成极大值（[L206](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L206)）：

```python
x_ds[:, :, :, 0] = 100      # 强制第 0 个 key 块分数极高
```

这会让 `torch.topk` **必然选中第 0 个 key 块**。结合 `tril_()`（第 0 列处于下三角，不会被抹掉），它保证了**每个 query 块至少保留一个有效 key 块**。这个细节在 4.3 节会与 `u5-l17` 的 `-inf` / `Check_inf` 问题呼应：只要每行至少有一块参与计算，整行的 `scores_max` 就不会是 \(-\infty\)，softmax 就不会出 NaN——所以本内核里 `Softmax` 宏中那段被注释掉的 `Check_inf`（[L93-L97](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L93-L97)）确实可以不开。

#### 4.1.4 代码实践

**实践目标**：手算 topk 策略下「每个 query 块保留多少 key 块」，体会 topk 与 tril 的交互。

**操作步骤**（手算）：

1. 取驱动脚本 [`benchmark_tilelang_bsa.sh`](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.sh#L8) 的 `seq=1024, block_size=128, sparsity=0.5`。
2. 算 `downsample_len = ⌈1024/128⌉ = 8`，`TOPK = ⌈(1-0.5)·1024/128⌉ = ⌈4⌉ = 4`（见 [L190](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L190)）。
3. 思考：topk 选 4 个块，但 `tril_()` 会把上三角清掉。对**第 0 个** query 块（只有第 0 列在下三角内），topk 选的另外 3 个若落在上三角会被抹掉，实际保留几块？对**最后一个** query 块（整行都在下三角），实际保留几块？

**预期结果**：最后一个 query 块保留 4 块（满 topk）；第 0 个 query 块受 tril 限制，实际只保留第 0 列那 1 块（恰好是 `x_ds[:,:,0]=100` 强制选中的那块）。可见「目标密度 50%」只是个平均近似，各行实际密度差异很大——这正是 4.4 节要讨论的「有效 FLOPS」口径问题。

#### 4.1.5 小练习与答案

**Q1**：如果 `use_dense_for_last_block=True`，掩码会发生什么变化？为什么某些稀疏 attention 实现要这么做？

**答**：它会把最后两个 query 块对应的整行强制置 True（[L20-L21](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L20-L21)），即这两个 query 块对所有 key 块都计算（退化为稠密）。这样做通常是因为序列尾部 token（如最近生成的 token）对下游更重要，需要保留全连接以保证精度；本基准默认关闭，仅为纯算力对比。

**Q2**：topk 策略与阈值策略，哪个保证「每行保留的块数恒定」？

**答**：topk 策略保证每行**选出的下标数**恒为 `topk`（但经 `tril_()` 后实际保留下三角的块数仍可能少于 topk）；阈值策略保留的块数随分数分布浮动，不恒定。在需要稳定算力/显存占用的场景（如本基准要按 `(1-sparsity)` 折算 FLOPS），topk 更可控。

---

### 4.2 `BlockSparseMask`：内核入参与 `downsample_len` 的关系

#### 4.2.1 概念说明

4.1 节生成的掩码是一张 host 侧的 bool 张量。要让 GPU 内核用到它，就把它作为**一个普通的张量入参**（和 Q/K/V 并列）传进 `@T.prim_func`。本内核里它叫 `BlockSparseMask`，是内核的第 4 个参数（第 5 个是输出 `Output`）。

理解这个入参的关键是搞清它的形状 `[batch, heads, downsample_len, downsample_len]` 与原始 `[batch, heads, seq_len, dim]` 的 Q/K/V 之间的「粒度差」：掩码的两个尾维是**块数**（`downsample_len`），不是序列长度（`seq_len`）。

#### 4.2.2 核心流程：`downsample_len` 与 `block_size` 的关系

设序列长度 `seq_len`、块大小 `block_size`（本内核里 `block_M = block_N = block_size`，见 [L35-L36](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L35-L36)）。把 `seq_len` 按 `block_size` 切块，块数为：

\[
\texttt{downsample\_len} = \left\lceil \frac{\texttt{seq\_len}}{\texttt{block\_size}} \right\rceil
\]

于是完整的 \( \texttt{seq\_len} \times \texttt{seq\_len} \) 注意力矩阵被划成 \( \texttt{downsample\_len} \times \texttt{downsample\_len} \) 个 `block_size × block_size` 小块。`BlockSparseMask[i, j]` 就管第 `i` 个 query 块、第 `j` 个 key 块对应的那一小块「算不算」。

三个量在 `__main__` 里的推导（[L201-L202](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L201-L202)）：

```python
downsample_factor = BLOCK                               # 下采样因子 = 块大小
downsample_len = math.ceil(SEQ_LEN / downsample_factor) # 块数 = ⌈seq/block⌉
```

> 「下采样（downsample）」一词的含义正在于此：把序列维度按 `block_size` 抽样压缩，`seq_len` 个元素压成 `downsample_len` 个块。掩码的分辨率比原始 attention 矩阵**低 `block_size` 倍**。

**关键对齐**：因为 `block_M = block_N = block_size`，K 循环每次迭代 `k` 正好对应一个 key 块，于是「循环第 `k` 次迭代」与「掩码第 `k` 列」天然对齐——`block_mask[k]` 就是「当前 query 块是否要算第 `k` 个 key 块」。这正是 4.3 节 `if block_mask[k] != 0` 能直接以下标 `k` 索引的前提。

#### 4.2.3 源码精读

内核入参声明（[L119-L125](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L119-L125)）：

```python
@T.prim_func
def blocksparse_flashattn(
        Q: T.Tensor(shape, dtype),                         # [batch,heads,seq,dim]
        K: T.Tensor(shape, dtype),
        V: T.Tensor(shape, dtype),
        BlockSparseMask: T.Tensor(block_mask_shape, block_mask_dtype),  # [b,h,ds,ds] bool
        Output: T.Tensor(shape, dtype),
):
```

其中 `block_mask_shape = [batch, heads, downsample_len, downsample_len]`（[L39](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L39)），`block_mask_dtype = "bool"`（[L43](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L43)）。

对比稠密版（`u5-l16`）的入参只有 Q/K/V/Output 四个，**本内核多了一个 `BlockSparseMask`**——这是块稀疏变体在签名上的唯一差别。

驱动侧把真实张量喂给内核（[L227-L213](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L210-L213)）：

```python
program = blocksparse_flashattn(BATCH, N_HEADS, SEQ_LEN, D_HEAD, downsample_len, BLOCK, is_causal, ...)
kernel = tilelang.compile(program)
profiler = kernel.get_profiler()
latency = profiler.do_bench(input_tensors=[q, k, v, block_mask, o])
```

注意 `do_bench` 传的是 `input_tensors=[q, k, v, block_mask, o]`——五个张量按内核入参顺序一一对应，其中第 4 个就是掩码。`do_bench` 在第一位置参数省略时测的是**内核本身**（`u5-l18` 讲过这条规则），`input_tensors` 提供内核运行所需的全部真实输入。

> 关于 `out_idx`：因为 `Output` 是第 5 个参数（下标 4），若改用调优路径（`@autotune`/`@jit`）需要声明 `out_idx=[4]`。本文件走的是非调优的 `tilelang.compile` 路径，不显式用 `out_idx`（见 4.4 节末尾与 `u5-l18`）。

#### 4.2.4 代码实践

**实践目标**：把 `downsample_len`、`block_size`、`seq_len`、K 循环迭代次数四者的关系列清楚（本讲义指定实践任务的前半部分）。

**操作步骤**（源码阅读 + 手算）：

1. 读 [L34-L39](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L34-L39)，确认 `block_M = block_N = block_size`、`block_mask_shape` 的两个尾维都是 `downsample_len`。
2. 读 [L150-L152](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L150-L152) 的 `loop_range`，确认非 causal 时它等于 `T.ceildiv(seq_len, block_N)`，即正好等于 `downsample_len`。
3. 对驱动脚本的 4 档 seq（[L8-L11](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.sh#L8-L11)）填表。

**预期结果**（`block_size=128`，非 causal 的 K 循环上界）：

| `seq_len` | `downsample_len = ⌈seq/128⌉` | K 循环最大迭代次数（非 causal） | `BlockSparseMask` 尾维形状 |
|---|---|---|---|
| 1024 | 8 | 8 | `[8, 8]` |
| 2048 | 16 | 16 | `[16, 16]` |
| 4096 | 32 | 32 | `[32, 32]` |
| 8192 | 64 | 64 | `[64, 64]` |

可观察到：`downsample_len` 既是掩码的边长，也是 K 循环的迭代上界——二者本是同一个量（序列被切成多少块）。causal 时 `loop_range` 还会再按 `(bx+1)*block_M` 裁剪（同 `u5-l17`），上界更小。

#### 4.2.5 小练习与答案

**Q1**：如果把 `block_size` 从 128 改成 64（其它不变），`downsample_len` 和掩码张量大小怎么变？对内核行为有什么影响？

**答**：`downsample_len = ⌈seq/64⌉` 翻倍（如 seq=8192 时从 64 变 128），掩码尾维从 `[64,64]` 变 `[128,128]`（元素数变为 4 倍）。块变小意味着掩码粒度更细、调度更灵活（能更精确地剔除小块），但每个 block 算的 `block_M×block_N` 更小，TensorCore 利用率会下降，且 K 循环迭代次数翻倍。块大小是在「掩码灵活性」与「MMA 效率」之间的权衡。

**Q2**：为什么 `block_M` 与 `block_N` 在本内核里被设成相等（都等于 `block_size`）？

**答**：为了让掩码的行、列块网格是规整的方阵，使「query 块下标」与「key 块下标」共享同一个 `downsample_len`，从而 `BlockSparseMask[bx, k]` 直接对应「第 bx 个 query 块与第 k 个 key 块」。若 `block_M ≠ block_N`，行/列方向块数不同，掩码就不再是方阵，索引与 causal 裁剪都要相应调整。

---

### 4.3 条件 MMA：`alloc_local` 预载 + `if block_mask[k] != 0` 跳过

> 这是本讲最核心的模块——块稀疏如何真正「省下计算」。

#### 4.3.1 概念说明

有了掩码，怎么在内核里用它「跳过」不重要块？朴素想法是在 K 循环里写 `if block_mask[k]: 做MMA`。但这里有个细节：掩码原本是**全局显存**里的张量，每次循环都去全局显存取一个 bool 既慢又影响软件流水。本内核的做法是——**在进入 K 循环之前，先把当前 query 块对应的那一整行掩码预载进片上寄存器**，循环里再用下标 `block_mask[k]` 做条件判断，访问的全是寄存器，零全局访存。

预载用到的 `T.alloc_local` 是 TileLang 的「线程私有寄存器缓冲」（与 `u4-l13` 反量化 GEMV 内核里为每个线程分配私有寄存器缓冲的 `alloc_local` 同源），区别于 `u5-l16` 反复出现的 `alloc_shared`（共享内存）与 `alloc_fragment`（分片寄存器）。

#### 4.3.2 核心流程

块稀疏内核相对稠密版（`u5-l16`）的**全部新增结构**只有三处，全在 `@T.prim_func main` 里：

```
1. 多一个入参 BlockSparseMask              (4.2 节已讲)
2. 预载一行掩码到寄存器:
     block_mask = T.alloc_local([downsample_len], bool)
     for vj in T.serial(downsample_len):
         block_mask[vj] = BlockSparseMask[bz, by, bx, vj]     # 取当前 query 块 bx 的整行
3. K 循环用条件守卫包住四段 macro:
     for k in T.Pipelined(loop_range, num_stages):
         if block_mask[k] != 0:
             MMA0(...); Softmax(...); Rescale(...); MMA1(...)   # 整块跳过, 否则全做
```

**为什么「整块跳过」对在线 softmax 是正确的**？回忆 `u5-l16`：每个 K 块对最终结果的贡献是给分子 `acc_o` 加上 \(P_k V_k\)、给分母 `logsum` 加上 \(\text{rowsum}(P_k)\)。被掩码掉的第 `k` 块相当于 \(P_k = 0\)——它给分子加 0、给分母加 0，对 `scores_max` 的归约也无影响（因为根本没算分数）。所以「跳过」在数学上等价于「这块贡献为零」，`acc_o` 与 `logsum` 保持不变是完全正确的。**在线 softmax 的递推天然兼容块稀疏**——这是本内核能直接复用稠密版四段 macro 的根本原因。

#### 4.3.3 源码精读

**预载掩码**（[L140](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L140) 与 [L147-L148](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L147-L148)）：

```python
block_mask = T.alloc_local([downsample_len], block_mask_dtype)   # 线程私有寄存器, 一行掩码
...
for vj in T.serial(downsample_len):
    block_mask[vj] = BlockSparseMask[bz, by, bx, vj]             # 预载当前 query 块 bx 的整行
```

- `bz` 绑 batch、`by` 绑 head、`bx` 绑 query 块（见 [T.Kernel](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L126-L127) 的 `(bx, by, bz)`）。`BlockSparseMask[bz, by, bx, vj]` 取的是「第 bz 个 batch、第 by 个 head、第 bx 个 query 块」对应的那一行掩码（长度 `downsample_len`）。
- `T.serial` 表示**串行**循环（不并行化）：逐个把 `downsample_len` 个 bool 从全局显存搬进寄存器。这段在 K 循环**之外**，只执行一次。

**条件守卫的 K 循环**（[L154-L160](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L154-L160)）：

```python
for k in T.Pipelined(loop_range, num_stages=num_stages):
    if block_mask[k] != 0:
        MMA0(K, Q_shared, K_shared, acc_s, k, bx, by, bz)
        Softmax(acc_s, acc_s_cast, scores_max, scores_max_prev, scores_scale,
                scores_sum, logsum)
        Rescale(acc_o, scores_scale)
        MMA1(V, V_shared, acc_s_cast, acc_o, k, by, bz)
```

- `if block_mask[k] != 0:` 守住了**全部四段 macro**。注意 `MMA0` 内部第一件事是 `T.copy(K[...], K_shared)`（[L58](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L58)），`MMA1` 内部也有 `T.copy(V[...], V_shared)`（[L77](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L77)）——所以被跳过的块**连 K/V 的全局→共享搬运都不做**，算力与带宽**双重**省下。
- `block_mask[k]` 取的是预载寄存器里的第 `k` 个值，访问零全局访存。

**为什么掩码用 `alloc_local`，而不是 `alloc_shared` 或 `alloc_fragment`？**（本讲义指定的实践任务，详见 4.3.4）

| 选项 | 存储语义 | 是否适合放掩码 | 理由 |
|---|---|---|---|
| `alloc_shared` | 共享内存，**整个 block 共享一份** | 不首选 | 共享内存是稀缺资源，已被 `Q/K/V/O_shared` 四个大 fp16 tile 占用；掩码若也放 shared 会挤压这些大块、还可能引发 bank conflict。 |
| `alloc_fragment` | 分片寄存器，**按 MMA 布局分散到各线程** | 不适合 | fragment 的语义是「数据按 TensorCore 分片分布在线程间」，而分支判断 `if block_mask[k]` 需要**每个线程都能完整读到同一份掩码**才能做出一致决策；fragment 的分散分布不匹配这种「全员同读」需求。 |
| **`alloc_local`** ✅ | 线程私有寄存器，**每个线程各存一份完整副本** | **首选** | 每个线程持有完整的一行掩码副本，`if block_mask[k] != 0` 在所有线程上**求值出同一个结果**——保证整个 block/warp 一致地进入或跳过 MMA（控制流一致，不会出现「有的线程算、有的线程不算」的分支发散）。寄存器访问延迟最低，且不占共享内存。 |

一句话：**掩码要作为「分支条件」被全 block 线程一致求值，因此需要每个线程都持有同一份完整副本——这正是 `alloc_local`（线程私有、复制式）的语义**；而 shared（共享式、稀缺）与 fragment（分散式、按 MMA 布局）都不符合这个需求。

> 代价：`alloc_local` 让每个线程都加载一整行掩码（`downsample_len` 次 bool 加载/线程），是「用冗余的寄存器副本换一致的控制流 + 不挤占共享内存」。由于掩码是 bool、`downsample_len` 通常不大（seq=8192, block=128 时仅 64 字节/线程），这点冗余完全可以接受。

> ⚠️ **关于 `T.Pipelined` 与数据依赖分支的交互**：`num_stages=num_stages`（默认 2）让编译器对 K 循环做软件流水——预取后续块的 K/V 来隐藏访存延迟。但本内核的 `if block_mask[k]` 是**数据相关的分支**（取决于掩码取值），这会影响编译器对流水的调度（例如对被跳过的块是否仍预取）。这一交互的具体落地行为依赖 TileLang 编译器的实现，**待本地验证**——读者可用 `tilelang.disable_cache()` 配合 `get_kernel_source()`（见 `u5-l18`）打印生成的 CUDA 源码来观察。

#### 4.3.4 代码实践

**实践目标**：说明「把 mask 放进 `alloc_local` 而非 shared/fragment」的原因（本讲义指定实践任务的后半部分）。

**操作步骤**（源码阅读 + 推理）：

1. 读 [L140](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L140)、[L147-L148](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L147-L148) 与 [L154-L160](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L154-L160)，确认掩码的「声明—预载—使用」三段。
2. 设想：如果改成 `block_mask = T.alloc_shared([downsample_len], "bool")`，预载逻辑能否照写？会发生什么问题？
3. 再设想：如果改成 `T.alloc_fragment`，分支判断 `if block_mask[k]` 还能保证所有线程决策一致吗？

**需要观察的现象 / 预期结论**：

- 改用 `alloc_shared` 时，预载逻辑可写（一处加载、全 block 共享），但会与 `Q/K/V/O_shared` 争抢共享内存容量，且对分支条件的标量读取不如寄存器直接。
- 改用 `alloc_fragment` 时，掩码数据会按 MMA 分片分散到各线程，单线程无法直接读到 `block_mask[k]` 的完整值，分支判断需要额外的跨线程收集/广播，破坏了「全员一致跳过」的简洁性。
- 因此 `alloc_local`（线程私有、复制式寄存器）是让 `if block_mask[k] != 0` 在全 block 线程上**一致求值、零共享内存占用、最低访问延迟**的正确选择。

#### 4.3.5 小练习与答案

**Q1**：被 `if block_mask[k] != 0` 跳过的块，`acc_o`、`logsum`、`scores_max` 三个累加器各会发生什么？为什么这对最终结果是无害的？

**答**：三个累加器都**保持原值不动**（跳过了 `MMA0/Softmax/Rescale/MMA1`，没有任何更新）。这等价于该块的 \(P_k = 0\)：它给分子 `acc_o` 加 0、给分母 `logsum` 加 0、也不改变 `scores_max`。因为在线 softmax 的递推对「加零项」天然免疫（参考 `u5-l16` 的证明），最终 `acc_o /= logsum` 仍得到精确 softmax。这正是块稀疏能直接套用稠密 FlashAttention 数学的根本原因。

**Q2**：为什么 `block_mask` 要在 K 循环**外**预载（`T.serial` 那段），而不是在循环内每次 `k` 现取 `BlockSparseMask[bz,by,bx,k]`？

**答**：循环外预载一次，把整行掩码搬进寄存器，之后 K 循环里 `block_mask[k]` 全是寄存器访问，零全局访存、低延迟，且不干扰 `T.Pipelined` 的流水调度。若改为循环内现取，每次迭代都要一次全局显存读取，既慢又会让数据相关分支更难被编译器优化。预载是用一次性 `downsample_len` 次全局加载，换取后续 K 次循环的零成本分支判断。

---

### 4.4 稀疏 FLOPS 折算与基准口径

#### 4.4.1 概念说明

算完延迟后要换算 TFlops，就遇到一个问题：块稀疏内核实际只算了 \((1-\texttt{sparsity})\) 比例的块，**稠密**的 \(2 \cdot \texttt{seq}^2 \cdot \texttt{dim}\) 算量显然高估了真实工作量。若仍除以稠密 FLOPS，会**低估**真实硬件效率（看起来 TFlops 很低）。

正确做法是按「实际算的块比例」折算**有效 FLOPS**：稠密算量 × \((1 - \texttt{sparsity})\)。这表征「内核实际完成的浮点运算量」，再除以延迟，才是有意义的吞吐。

#### 4.4.2 核心流程

本内核的有效 FLOPS 推导（对应 [L216-L219](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L216-L219)）：

\[
\underbrace{\texttt{flops\_per\_matmul}}_{\text{一次 matmul}}
= 2 \cdot \texttt{B} \cdot \texttt{H} \cdot \texttt{seq}^2 \cdot \texttt{dim} \cdot (1 - \texttt{sparsity})
\]

\[
\texttt{total\_flops} = 2 \cdot \texttt{flops\_per\_matmul} \cdot
\begin{cases}
0.5, & \texttt{is\_causal} \\
1, & \text{否则}
\end{cases}
\]

其中：

- 前导 `2`（`flops_per_matmul` 里的 `2.0`）：每次乘累加算 2 个 FLOP（1 乘 + 1 加），这是 `u2-l4` 讲过的 GEMM 算量约定。
- `B·H·seq²·dim`：一次 \(Q K^\top\)（或 \(PV\)）的形状因子。
- \((1 - \texttt{sparsity})\)：**稀疏折算**，按目标密度缩减。
- 外层 `× 2`：attention 有两次 matmul（\(QK^\top\) 与 \(PV\)）。
- `× 0.5`（causal 时）：下三角只算一半，与 `u2-l4`/`u5-l16` 的 causal 折半同源。

最终 TFlops（[L222](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L222)）：

\[
\texttt{TFlops} = \frac{\texttt{total\_flops}}{\texttt{latency\_ms}} \times 10^{-9}
\]

这里的 `1e-9` 与 `u2-l4` 完全一致：\(10^{-3}\)（ms→s）与 \(10^{-12}\)（FLOPS→TFlops）之积。**前提是 `latency` 以毫秒为单位**——`do_bench` 返回的正是 ms（`u2-l6`/`u5-l18` 已确认）。

#### 4.4.3 源码精读

[`__main__`](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L216-L222) 的算量与打印：

```python
flops_per_matmul = 2.0 * BATCH * N_HEADS * SEQ_LEN * SEQ_LEN * D_HEAD * (1 - sparsity)
total_flops = 2 * flops_per_matmul
if is_causal:
    total_flops *= 0.5
print(f"Sparsity: {sparsity}")
print(f"Latency: {latency} ms")
print(f"Tflops: {total_flops / latency * 1e-9} TFLOPS")
```

**两处需要警惕的口径问题**：

1. **`(1 - sparsity)` 是「目标」密度，不是「实际」密度**。如 4.1 节所述，topk 选出的块经 `tril_()` 后下三角实际保留的块数可能与目标不同（尤其靠前的 query 行受 tril 限制更多）。所以这里的 FLOPS 是按**目标稀疏度**做的名义折算，与内核**实际跳过的块数**可能有出入。对比参考基线 [`0.block-sparse-benchmark/benchmark_block_sparse_attn.py`](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/0.block-sparse-benchmark/benchmark_block_sparse_attn.py#L43-L45) 的做法——它统计 `calculated_block_num / total_block_num` 得到 `real_sparsity`（[L43-L45](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/0.block-sparse-benchmark/benchmark_block_sparse_attn.py#L43-L45)），更贴近真实。本 TileLang 基准为简化直接用 `--sparsity` 入参折算。
2. **`x_ds[:,:,:,0]=100` 让每行至少保留 1 块**，这进一步使实际密度与 \((1-\texttt{sparsity})\) 略有偏差（多保了至少 1 块/行）。

> 这两处偏差都不大，用于纵向对比（同稀疏度下不同 seq 的趋势）足够；但**跨框架横向对比 TFlops 时**，要确认各框架的 FLOPS 口径一致（是否都按目标稀疏度折算、causal 是否都折半），否则数值不可比——这是 `u2-l4` 强调的「公平度量」原则在稀疏场景的延续。

**调优路径被注释**：文件末尾 [L224-L256](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L224-L256) 有一段被注释掉的 `AutoTuner` 调优代码（搜 `num_stages × threads` 的笛卡尔积）。当前 `__main__` 走的是 `u5-l18` 讲过的**非调优** `tilelang.compile` 路径（[L210-L211](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L210-L211)），手调 `num_stages=2, threads=256`（见 [L210](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L210)）。若要榨干性能，可解开注释走显式 `AutoTuner`（`u6-l22` 会讲这种区别于装饰器的调优 API）。

#### 4.4.4 代码实践

**实践目标**：手算一次稀疏 TFlops，体会 \((1-\texttt{sparsity})\) 折算对数值的影响。

**操作步骤**（手算）：

1. 取 `BATCH=64, N_HEADS=64, SEQ_LEN=8192, D_HEAD=128, sparsity=0.5, is_causal=True`（驱动脚本 [L11](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L11) 的最大档，假设 `causal=True`）。
2. 算 `flops_per_matmul = 2 × 64 × 64 × 8192² × 128 × (1-0.5)`。
3. 算 `total_flops = 2 × flops_per_matmul × 0.5`。
4. 假设测得 `latency = 5 ms`，算 `TFlops = total_flops / 5 × 1e-9`。
5. 对比：如果不做 \((1-\texttt{sparsity})\) 折算（即乘 1 而非 0.5），TFlops 会变成多少？

**预期结果**：

- `flops_per_matmul = 2 × 64 × 64 × 8192² × 128 × 0.5 = 2 × 64 × 64 × 67108864 × 128 × 0.5 ≈ 3.51 × 10¹⁵`。
- `total_flops = 2 × 3.51e15 × 0.5 ≈ 3.51 × 10¹⁵`（注意 causal 的 0.5 把外层 ×2 又抵消回去，所以这里总数与单次 matmul 名义值相近）。
- `TFlops ≈ 3.51e15 / 5e-3 / 1e12 ≈ 702 TFLOPS`（待本地验证：真实 latency 取决于硬件）。
- 若不做稀疏折算，`flops_per_matmul` 翻倍，`total_flops` 也翻倍，TFlops 看起来翻倍——但这「虚高」的数字并不反映真实算的工作量。可见折算与否会让吞吐数值差 2 倍，跨框架对比前必须统一口径。

> 待本地验证：上述 latency=5ms 是假设值，仅用于演示折算口径；真实运行请执行 `bash benchmark_tilelang_bsa.sh` 取 `logs/bsa_8192.log` 中的 `Latency` 行。

#### 4.4.5 小练习与答案

**Q1**：为什么稀疏内核报 TFlops 时**必须**乘 \((1-\texttt{sparsity})\)，而稠密内核不需要？

**答**：稠密内核算了完整的 \(2 \cdot \texttt{seq}^2 \cdot \texttt{dim}\)，FLOPS 与之匹配即可。稀疏内核**实际跳过**了 `sparsity` 比例的块，真实算量只有稠密的 \((1-\texttt{sparsity})\)。若仍用稠密 FLOPS，会把「没算的工作」也算进分母，TFlops 被人为压低，无法反映硬件真实效率。乘 \((1-\texttt{sparsity})\) 是把分母对齐到「实际完成的工作量」。

**Q2**：`total_flops = 2 * flops_per_matmul; if is_causal: total_flops *= 0.5`。这两个系数（外层 `×2` 与 causal `×0.5`）各对应什么？

**答**：外层 `×2` 对应 attention 的**两次 matmul**（\(QK^\top\) 与 \(PV\)，`flops_per_matmul` 只算了一次）；causal `×0.5` 对应**下三角只算一半**。当 causal 且两次 matmul 都折半时，二者数值上恰好抵消（`2 × 0.5 = 1`），但这只是巧合，二者语义独立——非 causal 时只有外层 `×2`，没有 `×0.5`。

---

## 5. 综合实践

把本讲的「块掩码生成 → `downsample_len` 对齐 → `alloc_local` 预载 → 条件 MMA → 稀疏 FLOPS」串起来，完成下面的任务。

**任务**：以驱动脚本 [`benchmark_tilelang_bsa.sh`](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.sh) 的 `seq=4096` 档（`batch=64, heads=64, dim=128, block_size=128, sparsity=0.5, causal=True`）为例，回答下面 4 个问题，并用一张「数据流图」把掩码从生成到消费的全程画出来。

1. **下采样关系**：算 `downsample_len`、`TOPK`、`BlockSparseMask` 的尾维形状、非 causal 时的 K 循环迭代上界。
2. **掩码生成**：解释 `x_ds[:,:,:,0]=100`（[L206](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L206)）为什么能保证「每个 query 块至少有一个可算的 key 块」，并说明这为什么让 `Softmax` 宏里被注释的 `Check_inf`（[L93-L97](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L93-L97)）在本配置下可不开（承接 `u5-l17`）。
3. **存储选择**：解释为什么掩码预载用 `alloc_local`（[L140](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L140)）而不是 `alloc_shared` / `alloc_fragment`，重点落在「全 block 线程一致求值分支」这一点。
4. **调参观察**：把 `--sparsity` 从 `0.5` 改成 `0.9`（更稀疏），预测 `TOPK`、被跳过的块数、`Latency` 与 `TFlops` 各会怎么变。

**参考答案要点**：

1. `downsample_len = ⌈4096/128⌉ = 32`；`TOPK = ⌈0.5 × 4096/128⌉ = 16`；掩码尾维 `[32, 32]`；非 causal K 循环上界 32（causal 时按 `(bx+1)*block_M/block_N` 裁剪，更小）。
2. 第 0 列被设成 100，`torch.topk` 必选中第 0 个 key 块；经 `tril_()` 后第 0 列处于下三角、不会被抹掉，故每个 query 块至少保留第 0 个 key 块。这样每行至少有一个块参与 MMA，整行 `scores_max` 必有限，不会触发 `exp2(-inf) = NaN`，故 `Check_inf` 可不开（与 `u5-l17` 的 `-inf` 处理同源）。
3. 见 4.3.3 的对照表：`alloc_local` 让每个线程持有一份完整掩码副本，`if block_mask[k]` 在全 block 线程上一致求值，避免分支发散、不挤占 shared、延迟最低。
4. `sparsity=0.9` → `TOPK = ⌈0.1 × 32⌉ = 4`（每行只选 4 块）；被跳过的块更多（约 28/32）→ 实际算量大减 → `Latency` 下降；但 `total_flops` 也乘了更小的 \((1-0.9)=0.1\)，故 `TFlops` 的变化取决于「延迟下降比例」与「算量折算比例」谁更快——一般更稀疏会提升「有效 TFlops」（因为固定的循环开销被分摊到更少的块上，但带宽/控制开销占比上升）。**待本地验证**实际数值。

**参考数据流图**（掩码从生成到消费）：

```
host 侧 (PyTorch):
  x_ds: [B,H,ds,ds] (下采样块分数, randn 占位)
     │  x_ds[:,:,:,0] = 100           ← 强制第 0 个 key 块必选
     ▼
  torch.topk → scatter → tril_
     │
     ▼
  BlockSparseMask: [B,H,ds,ds] bool   ← 作为内核第 4 个入参传入
     │
     ▼  do_bench(input_tensors=[q,k,v,block_mask,o])
GPU 内核 (每个 query 块 bx):
  block_mask = T.alloc_local([ds], bool)        ← 线程私有寄存器副本
     │  for vj in T.serial(ds):
     │      block_mask[vj] = BlockSparseMask[bz,by,bx,vj]   ← 预载当前 bx 的整行
     ▼
  for k in T.Pipelined(loop_range):
     if block_mask[k] != 0:        ← 寄存器内一致求值, 全 block 同进/同跳
        MMA0 → Softmax → Rescale → MMA1
     else:
        跳过 (连 K/V 的全局→shared 搬运都不做)
```

> **完成标志**：你能不假思索地说出「`downsample_len = ⌈seq/block_size⌉`，既是掩码边长也是 K 循环上界」，并能解释「掩码放 `alloc_local` 是为了让分支条件在全 block 线程上一致求值」——这两点正是本讲义指定实践任务的核心。

## 6. 本讲小结

- block-sparse attention 把稀疏掩码**按 `block_size × block_size` 整块**取舍，从而能在 GPU 内核里真正跳过 MMA、同时省下算力与带宽；元素级稀疏做不到这点，因为 TensorCore MMA 的最小单位就是块。
- 掩码由 host 侧生成：用下采样分数矩阵 `x_ds` 经 `torch.topk`（或阈值）选块、`scatter_` 成 bool、`tril_()` 套因果下三角，得到 `[batch, heads, downsample_len, downsample_len]` 的 `BlockSparseMask`；`x_ds[:,:,:,0]=100` 强制每行至少保留第 0 个 key 块，避免全 \(-\infty\) 行出 NaN。
- `downsample_len = ⌈seq_len/block_size⌉`：它既是掩码的边长，也是 K 循环的迭代上界——同一个量。因 `block_M = block_N = block_size`，循环第 `k` 次迭代与掩码第 `k` 列天然对齐，`block_mask[k]` 直接索引。
- 内核相对稠密版（`u5-l16`）**只新增三处**：`BlockSparseMask` 入参、用 `T.alloc_local` 预载一行掩码、K 循环里 `if block_mask[k] != 0:` 整体条件执行四段 macro；在线 softmax 的数学**完全不变**（被跳过的块等价于 \(P_k=0\)，对分子分母贡献为零）。
- 掩码放 `alloc_local`（线程私有、复制式寄存器）而非 `shared`（稀缺、被大 tile 占用）或 `fragment`（按 MMA 分片分散），是为了让 `if block_mask[k]` 分支在**全 block 线程上一致求值**，避免分支发散，且零共享内存占用、访问延迟最低。
- 稀疏 FLOPS 折算：稠密算量 × \((1-\texttt{sparsity})\) × （causal 再 ×0.5）得到「有效算量」，再 `÷ latency(ms) × 1e-9` 得 TFlops；`(1-sparsity)` 是目标密度名义折算，与实际跳过块数可能略有出入，跨框架对比前须统一口径。

## 7. 下一步学习建议

- **回顾与本讲对偶的稠密版本**：本内核的四段 macro（`MMA0/Softmax/Rescale/MMA1`）与 `u5-l16` 几乎逐行一致，causal 掩码、`loop_range` 裁剪、`Check_inf` 的讨论见 `u5-l17`。如果你是直接跳到本讲的，强烈建议先读这两篇，本讲的「新增结构」会显得非常薄。
- **下一单元 `u6`** 会进入更高级的 attention 变体与机制：`u6-l20` 讲 MLA decode 的 split-KV 并行与 combine（用 `log2/exp2` 在线合并各 split 的 logsum，与本讲的在线 softmax 一脉相承）、`u6-l22` 讲显式 `AutoTuner.from_kernel` API（本文件末尾被注释的那段调优代码正是这种写法）。
- **跨基线对比**：本算子目录下还有 `0.block-sparse-benchmark`（Dao-AILab `block_sparse_attn` 参考实现，统计 `real_sparsity`）、`3.triton-benchmark`（Triton 版块稀疏 attention，用 `block_mask` 同样守卫内层循环）、`2.torch-becnhmark`（torch/flex_attention 参考）。建议对照 `0` 的 `real_sparsity` 统计，体会「目标密度 vs 实际密度」的差距，印证本讲 4.4 节的口径讨论。
- **若要二次开发**：本算子缺少 `dense_matmul` 那套 `data/plot` 可视化管线（见 `u1-l2`/`u1-l2-l7`），如果你想为块稀疏 attention 加上跨框架 speedup 图，可参考 `u7-l25` 的「新增算子基准」工程约定，复用 `data_*.py` 的日志正则解析模式。
