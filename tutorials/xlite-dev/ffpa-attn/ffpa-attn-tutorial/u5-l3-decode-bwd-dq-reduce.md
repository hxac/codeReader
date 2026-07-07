# Decode 反向与 dQ 跨块归约

## 1. 本讲目标

本讲承接 [u5-l2](u5-l2-dkdv-dq-shared-pid.md) 的「Nq≥8 shared-pid 反向主路径」，专门解决**短 query（`Nq < 8`）的 decode 反向**问题。读完本讲你应当掌握：

- 为什么 `Nq < 8` 时不能再沿用 shared-pid 矩阵主路径，必须改走「stage1 + reduce」两阶段；
- `_ffpa_bwd_decode_stage1_kernel` 如何按 **K 块**切分，使 `dK/dV` 各块独立、`dQ` 写成「部分贡献」；
- `_ffpa_bwd_decode_dq_reduce_kernel` 如何把跨 K 块的 `PartialDQ` 求和还原出最终 `DQ`，以及 `WrittenKBlocks` 这个「握手」变量的作用；
- `USE_GEMV`（`Nq == 1`）单 query 特化路径为何用一维向量归约更划算。

本讲全部源码集中在一个文件：`src/ffpa_attn/triton/_ffpa_bwd.py`。

## 2. 前置知识

本讲默认你已经掌握以下概念（前序讲义已建立）：

- **delta 预处理**（[u5-l1](u5-l1-bwd-algo-delta-preprocess.md)）：反向第一步先算 `delta = rowsum(dO * O)`，它是 softmax 行间耦合修正项 `D_m = Σ_k P_{m,k} · dP_{m,k}` 的廉价等价形式，由独立 kernel `_ffpa_bwd_pre_impl` 一次性预算，本讲 decode 路径直接复用它的结果。
- **反向链式法则**：`dS = P ⊙ (dP − Δ_m)`，进而 `dQ = scale·dS·K`、`dK = scale·dSᵀ·Q`、`dV = P·dO`。
- **所有权（ownership）决定是否要原子/归约**（[u5-l2](u5-l2-dkdv-dq-shared-pid.md)）：一个输出 tile 若被唯一 program 单写，则无需归约；若被多 program 共写，则必须归约。
- **decode 前向的 split-KV 思路**（[u4-l3](u4-l3-decode-fwd-split-kv.md)）：query 行太少会让网格塌缩、SM 跑不满，于是沿 KV 切 chunk 补并行度。decode 反向用的也是同一类「以 KV 维并行补 Q 维不足」的思路，但落地细节不同。

本讲的关键直觉是：**在 decode 反向里，dQ 与 dK/dV 的「所有权结构」不对称**——这是它需要两阶段的根本原因，下一节会展开。

## 3. 本讲源码地图

| 位置 | 作用 |
|---|---|
| `_ffpa_bwd.py:71-77` | 模块顶部 docstring 对 decode 路径的总述（两阶段、GEMV、causal 尾对齐）。 |
| `_ffpa_bwd.py:2298-2468` | 启动器 `_ffpa_attn_backward_triton_impl` 中 `if seqlen_q < 8:` 的 decode 分支：选 block、分配 `partial_dq`/`written_k_blocks`、串起 stage1 与 reduce。 |
| `_ffpa_bwd.py:1673-1978` | `_ffpa_bwd_decode_stage1_kernel`：每个 program 处理一个 K 块，算 `dK/dV` 并写 `PartialDQ`。 |
| `_ffpa_bwd.py:1780-1858` | stage1 的 `USE_GEMV`（`Nq==1`）特化分支。 |
| `_ffpa_bwd.py:1981-2028` | `_ffpa_bwd_decode_dq_reduce_kernel`：跨 K 块把 `PartialDQ` 求和成 `DQ`。 |
| `_ffpa_bwd.py:1568-1643` | decode stage1 的 autotune 候选生成与缓存包装。 |

## 4. 核心概念与源码讲解

### 4.1 decode 反向为什么要拆两阶段：所有权不对称

#### 4.1.1 概念说明

u5-l2 的主路径用 **shared-pid 矩阵 kernel**，其网格第 0 维取 `max(cdiv(Nk,BN), cdiv(Nq,BM))`。当 `Nq` 很小（比如 1~7 行）时，`cdiv(Nq, BM)` 在 `BM=64/128` 下恒等于 1——也就是说**整个 query 维度只有 1 个块**，每个 head 只能派出很少的 program，GPU 上大量 SM 闲置。

decode 前向（u4-l3）面对同样问题时选择「沿 KV 切 chunk 补并行度」。decode 反向照搬这个思路：**把并行轴从 Q 行块换成 K 列块**，让 `cdiv(Nk, BLOCK_N)` 个 program 同时跑起来填满 SM。

但这一换，立刻引出一个所有权问题。回顾三个梯度的下标：

\[ \text{dQ}[m,d] = \tau\sum_k \text{dS}[m,k]\,K[k,d],\quad \text{dK}[k,d] = \tau\sum_m \text{dS}[m,k]\,Q[m,d],\quad \text{dV}[k,d] = \sum_m P[m,k]\,\text{dO}[m,d] \]

- `dK[k,:]` 与 `dV[k,:]` 的下标是 **K 位置 k**。把 K 切成不相交的块后，每个 K 块**独占**自己那批 k 位置 → `dK/dV` 的每个输出 tile 都有唯一写者，**无需归约**。
- `dQ[m,:]` 的下标是 **Q 位置 m**，但它要对**所有** k 求和。每个 K 块都向同一批 `dQ[m,:]` 贡献一部分 → 写者不唯一，**必须跨 K 块归约**。

这就是「所有权不对称」：**dK/dV 是 K-tile 独占的，dQ 是所有 K-tile 共享的**。于是一个 kernel 搞不定——stage1 让每个 K 块写自己独占的 `dK/dV`，同时把对 `dQ` 的贡献写成一块「部分结果」`PartialDQ`；reduce kernel 再把所有 `PartialDQ` 求和。

#### 4.1.2 核心流程

```text
delta 预处理（_ffpa_bwd_pre_impl，对全部路径共用）
      │  delta = rowsum(dO * O)，形状同 lse
      ▼
if seqlen_q < 8:                      # 走 decode 路径
   ├─ use_gemv = (seqlen_q == 1)
   ├─ 选 block：GEMV→(BM=8,BN=64)；矩阵→(BM=16,BN=128)
   ├─ 分配 partial_dq[B,H,num_k_blocks,BM,D] (fp32, empty)
   ├─ 分配 written_k_blocks[B*H] (int32)
   ├─ stage1 grid = (cdiv(Nk,BN), B*H)
   │     每个 program：算 dK/dV（独占 K 块）+ 写 PartialDQ[k_block]
   └─ reduce grid = (cdiv(D,BHD), cdiv(Nq,BM), B*H)
         每个 program：把 PartialDQ 沿 K 块求和 → DQ
else:
   └─ 走 u5-l2 的 shared-pid 主路径
```

#### 4.1.3 源码精读

分发判定在启动器里，门槛就是 `seqlen_q < 8`：

[src/ffpa_attn/triton/_ffpa_bwd.py:2298-2310](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L2298-L2310) 中文说明：注释点明 decode 路径的动机——「短 query 时很多 K 块都向同一两个 query 行贡献，专用路径让 dK/dV 按 K 块保留、显式归约 dQ，比给 tiny Nq 启动矩阵 kernel 更快」；`use_gemv = seqlen_q == 1` 决定走哪条特化，并据此选 `BLOCK_M/BLOCK_N`。

接着是两个关键临时缓冲的分配。注意 `partial_dq` 用 `torch.empty`（**不**清零），`written_k_blocks` 记录每个 head 实际启动的 K 块数：

[src/ffpa_attn/triton/_ffpa_bwd.py:2337-2350](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L2337-L2350) 中文说明：`num_k_blocks` 按「最小候选 `BLOCK_N`」算，保证 autotune 切换到小 block 时缓冲也有足够槽位；`partial_dq` 形状是 `(batch, nheads, num_k_blocks, block_m_decode, headdim)` 的 fp32 张量；`written_k_blocks` 是 `B*H` 的 int32，供 reduce 阶段只求和「真正写了的」前缀（故 `partial_dq` 无需整体清零）。

`partial_dq` 形状里多出来的 `block_m_decode` 维（GEMV 时为 8）值得记住——它在 GEMV 路径下其实只用到第 0 行，4.3 节会解释。

#### 4.1.4 代码实践

1. **实践目标**：确认 decode 路径的触发条件与缓冲形状随 `Nq` 变化。
2. **操作步骤**：在 `_ffpa_attn_backward_triton_impl` 的 `if seqlen_q < 8:` 这一行（2298 附近）上方临时插一行 `print("DECODE BWD", seqlen_q, use_gemv if 'use_gemv' in dir() else "?")`（**示例代码**，仅用于阅读追踪，勿提交）。
3. **需要观察的现象**：用 `Nq=1` 与 `Nq=4` 各跑一次反向（见 4.4 综合实践的脚本），观察是否都命中该分支、`use_gemv` 分别为 `True/False`。
4. **预期结果**：`Nq∈{1,2,3,4,7}` 均进入 decode 分支；仅 `Nq==1` 时 `use_gemv=True`。运行结果**待本地验证**（需 CUDA 环境）。

#### 4.1.5 小练习与答案

**Q1**：为什么 `partial_dq` 用 `empty` 而不是 `zeros`？
**答**：reduce kernel 只对 `written_k_blocks` 记录的「写了的」K 块前缀求和，并用 `mask` 把其余槽位当 0 读入（`other=0.0`），未写区域从不参与运算，故无需整体清零。autotune 模式下另有 `reset_to_zero=["PartialDQ"]` 保证多次候选运行间互不污染。

**Q2**：如果把门槛从 `seqlen_q < 8` 改成 `seqlen_q < 16`，正确性会破坏吗？
**答**：不会破坏正确性（数学等价），但 `Nq∈[8,15]` 本可走矩阵主路径、并行度已足够，强行走 decode 反而多一次 reduce kernel 的开销，得不偿失。

---

### 4.2 stage1 kernel：每个 K 块算 dK/dV + 写 PartialDQ

#### 4.2.1 概念说明

`_ffpa_bwd_decode_stage1_kernel` 的并行粒度是「**一个 K 块 × 一个 (batch, head)**」。每个 program 干三件事：

1. 算出这个 K 块对应的 score `S = scale·Q·Kᵀ` 与 `dP = dO·Vᵀ`（沿 D 做 Split-D 分片累加，与 u4-l2 一致）；
2. 用预算好的 `delta` 与 `lse` 重建 `P`、算出 `dS = P⊙(dP−Δ_m)`；
3. 写出**本块独占**的 `dK/dV`，以及对本块贡献的**部分** `dQ`（`PartialDQ`）。

由于每个 program 的 K 块与其他 program 不相交，`dK/dV` 的写入互不重叠，用普通 `tl.store` 即可，**无原子、无归约**。只有 `PartialDQ` 因为多个 K 块都指向同一 query 行，需要后续 reduce。

#### 4.2.2 核心流程

单 program（矩阵路径，`USE_GEMV=False`）的数据流：

```text
pid(0)=start_n_block（K 块号），pid(1)=off_hb（batch*head 拍扁）
  │
  ├─ Phase 1（Split-D 算 score 与 dP）：
  │    for d_chunk in range(num_d_chunks):
  │        scores = dot(Q_block, K_blockᵀ, acc=scores)   # [BM,BN]
  │        dP     = dot(dO_block, V_blockᵀ, acc=dP)      # [BM,BN]
  ├─ Phase 2（softmax + dS）：
  │    P  = exp(scores - lse_m)；P = P * dropout_mult
  │    dS = (P * (dP - delta_m) * scale).to(DTYPE)       # [BM,BN]
  ├─ Phase 3（按 K 块独占写 dK/dV，写 PartialDQ）：
  │    for d_chunk in range(num_d_chunks):
  │        DK = trans(dS) · Q        → store（独占，无原子）
  │        DV = trans(P_drop) · dO   → store（独占，无原子）
  │        partial_dq = dS · K       → store 到 PartialDQ[k_block,:,:]
```

注意 score 被算了**一次**（不像 u5-l2 主路径那样 dKdV 角色与 dQ 角色各算一次）——这是 decode 路径的一个附带优点：单 program 内 `dS` 复用到 `dK/dV/partial_dq` 三处，没有重复重算。

#### 4.2.3 源码精读

程序映射与「握手」写入。每个 head 的第一个 K 块程序把**实际启动的 K 块数**写进 `WrittenKBlocks[off_hb]`，reduce 阶段据此知道该求和多少块：

[src/ffpa_attn/triton/_ffpa_bwd.py:1751-1757](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L1751-L1757) 中文说明：`start_n_block=pid(0)` 是 K 块号，`off_hb=pid(1)` 是 batch×head；当 `start_n_block==0` 时写 `tl.num_programs(0)`（即 grid 第 0 维大小 `cdiv(Nk,BN)`）到 `WrittenKBlocks`——这就是 reduce 阶段读取的「实际块数」。

矩阵路径（`USE_GEMV=False`）的 Phase 1 用 Split-D 分片累加 score 与 dP，与 u4-l2 的 V-group 思路一致（这里以 `num_d_chunks` 控制归约循环）：

[src/ffpa_attn/triton/_ffpa_bwd.py:1860-1885](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L1860-L1885) 中文说明：`scores` 与 `dP` 都是 `[BLOCK_M, BLOCK_N]` 的 fp32 累加器，`for d_chunk in range(num_d_chunks)` 把 head_dim 切成 `BLOCK_HEADDIM` 宽的片段分别累加进 score——这正是 Split-D 在反向的体现，使 SRAM 工作集与 D 无关。

Phase 2 重建 `P` 并算 `dS`，causal 采用与 decode 前向一致的「尾部对齐」约定（query 行 m 能看到 key 列 `≤ m + (Nkv − Nq)`）：

[src/ffpa_attn/triton/_ffpa_bwd.py:1887-1924](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L1887-L1924) 中文说明：`scores *= scale`，加 bias，做 causal 尾对齐掩码；`P = exp(scores - lse_i)`，乘 dropout 掩码；`dS = (P * (dP - delta_i) * scale).to(DTYPE)`，其中 `delta_i` 来自预算的 `D` 张量。

Phase 3 写出 dK/dV（独占 store）与 partial_dq：

[src/ffpa_attn/triton/_ffpa_bwd.py:1944-1978](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L1944-L1978) 中文说明：`dk = trans(dS)·Q`、`dv = trans(P_drop)·dO` 用普通 `tl.store` 写到本块独占的 K 位置；`partial_dq = dS·K` 写到 `PartialDQ[k_block, m, d]`，等 reduce 求和。

#### 4.2.4 代码实践

1. **实践目标**：理解 stage1 的网格与每个 program 的工作边界。
2. **操作步骤**：阅读启动器里的 `decode_grid` 与 stage1 的 pid 映射，对 `Nq=4, Nkv=513, D=512, B=1, H=2`（矩阵路径）手算：grid 形状、每个 program 写到的 `PartialDQ` 下标范围。
3. **需要观察的现象**：确认 `dK/dV` 的写入互不重叠（每个 K 块独占），而 `PartialDQ` 的 query 行被多个 K 块重复写入不同 k_block 槽位。
4. **预期结果**：`grid=(cdiv(513,128)=5, 2)`，共 10 个 program；`dK/dV` 按 K 块切分无重叠；`PartialDQ` 形状 `(1,2,5,16,512)`，reduce 时第 5 个 K 块只有前 `513-512=1` 个有效 key（由 mask 处理）。**待本地验证**。

#### 4.2.5 小练习与答案

**Q1**：stage1 里 `dS` 为什么只算一次就能同时给 dK/dV 和 partial_dq 用？
**答**：因为一个 program 内 `dS[BLOCK_M,BLOCK_N]` 是寄存器里的中间量，`dK=trans(dS)·Q`、`partial_dq=dS·K` 都直接复用它，无需像主路径那样在两个角色里各重算一次 score。代价是这要求程序内同时持有 Q 与 K 块，但 decode 下 query 很短，开销可接受。

**Q2**：causal decode 下「尾部对齐」对单 query（`Nq=1`）意味着什么？
**答**：query 行对齐到 KV 末尾，唯一的 query 行可看到全部合法 K 位置（`≤ 0 + (Nkv−1)`），故 causal 掩码对 `Nq=1` 实际不裁剪任何真 key，只用于屏蔽 `BLOCK_N` 内的 padding lane（见 4.3 节 GEMV 分支的注释）。

---

### 4.3 USE_GEMV 路径：Nq==1 的向量特化

#### 4.3.1 概念说明

当 `Nq == 1` 时，`Q` 只有一行，`QKᵀ` 退化为**向量-矩阵乘（GEMV）**：score 是长度为 `BLOCK_N` 的一维向量，而非 `[BLOCK_M, BLOCK_N]` 矩阵。若仍走矩阵路径，`BLOCK_M=8/16` 但有效 query 行只有 1，剩下 7/15 行全是 padding lane，`tl.dot` 的算力被白白浪费。

GEMV 路径把所有二维 tile 换成一维向量与逐元素归约：

- score 用一维累加器 `scores[BLOCK_N]`，靠 `tl.sum(k * q[None,:], axis=1)` 做 Split-D 点积；
- `partial_dq` 也退化为一维 `[headdim]`（只对应 query 第 0 行），用 `tl.sum(dS[:,None]*k, axis=0)`；
- 配合更小的 `BLOCK_N=64`（矩阵路径是 128）和 `BLOCK_M=8`，匹配单 query 的低算力密度、偏内存带宽的特性。

#### 4.3.2 核心流程

```text
scores = zeros([BLOCK_N])          # 1D，而非 [BM,BN]
dP     = zeros([BLOCK_N])
for d_chunk in range(num_d_chunks):     # Split-D
    scores += sum(k * q[None,:], axis=1)   # q 与 BLOCK_N 个 key 逐个点积
    dP     += sum(v * do[None,:], axis=1)
P = exp(scores*scale - lse)；dS = P*(dP - delta)*scale
for d_chunk in range(num_d_chunks):
    DK = dS[:,None] * q[None,:]   → store（K 块独占）
    DV = P_drop[:,None] * do[None,:]
    partial_dq = sum(dS[:,None]*k, axis=0)  → [headdim]，存到 PartialDQ[k_block,0,:]
```

#### 4.3.3 源码精读

GEMV 分支的入口与一维 score/dP 累加（Split-D 逐片段点积）：

[src/ffpa_attn/triton/_ffpa_bwd.py:1780-1798](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L1780-L1798) 中文说明：`scores`/`dP` 都是 `[BLOCK_N]` 一维 fp32；`tl.sum(k * q[None,:], axis=1)` 把 `[BLOCK_N, headdim_chunk]` 沿 head_dim 归约成 `[BLOCK_N]`——这就是把矩阵 GEMM 退化成 GEMV 的关键。

GEMV 下 P/dS 的重建（注意 lse/delta 都是标量单元素加载，因只有一个 query 行）：

[src/ffpa_attn/triton/_ffpa_bwd.py:1813-1827](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L1813-L1827) 中文说明：`lse_i = tl.load(LSE)`、`delta_i = tl.load(D)` 都是标量；`dBias = P*(dP-delta_i)`、`dS = dBias*scale`，整体与矩阵路径公式一致，只是作用在一维向量上。

GEMV 的写出（含 partial_dq 退化为一维，且无 query 行偏移）：

[src/ffpa_attn/triton/_ffpa_bwd.py:1836-1858](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L1836-L1858) 中文说明：`dk = dS[:,None]*q[None,:]`、`dv = P_drop[:,None]*do[None,:]` 仍是 GEMV 风格的外积；`partial_dq = tl.sum(dS[:,None]*k, axis=0)` 得到 `[headdim]`，存到 `PartialDQ + d_offs`（**无** `offs_m` 偏移，注释指出 GEMV 只用于 `Nq==1`，故 bias/partial 指针不带 query 维偏移）。

`USE_GEMV` 编译期开关由 `use_gemv = (seqlen_q == 1)` 决定，并在启动时作为 constexpr 传入：

[src/ffpa_attn/triton/_ffpa_bwd.py:2306-2309](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L2306-L2309) 中文说明：`use_gemv=True` 时用更小的 `BLOCK_M=8/BLOCK_N=64`；`USE_GEMV` 作为 `tl.constexpr` 让 Triton 在编译期裁掉未走分支，零运行期分支开销。

#### 4.3.4 代码实践

1. **实践目标**：对比 GEMV 与矩阵路径在「有效 query 行占比」上的差异。
2. **操作步骤**：设 `BLOCK_M=8`。矩阵路径（`Nq=4`）有效行占比为 `4/8=50%`；GEMV 路径（`Nq=1`）若强行用矩阵路径占比仅 `1/8=12.5%`。手算两种路径下 `partial_dq` 在 `BLOCK_M` 维实际用到的行数。
3. **需要观察的现象**：GEMV 把 score/dP/partial_dq 全压成一维，避免了 7 行 padding lane 的浪费。
4. **预期结果**：GEMV 路径 `partial_dq[k_block,0,:]` 只填第 0 行，其余 7 行为未初始化值，但 reduce kernel 用 `mask_m = offs_m < seqlen_q=1` 屏蔽掉，不影响正确性（见 4.4.3）。

#### 4.3.5 小练习与答案

**Q1**：为什么 GEMV 用 `BLOCK_N=64` 而矩阵路径用 `128`？
**答**：单 query 时整个计算偏内存带宽受限（要遍历整个 KV），单 program 算力需求低；用更小的 `BLOCK_N` 能启动更多 program、提高并行度与显存访问并发，整体更匹配 GEMV 的性能特征。这是 autotune 候选（`_gen_decode_bwd_stage1_autotune_configs`，1568 行起）里 `use_gemv` 分支固定较小的 block 的原因。

**Q2**：`partial_dq` 在 GEMV 下是 `[headdim]`，但缓冲形状仍是 `(...,BLOCK_M=8,headdim)`，会不会读到第 1~7 行的脏数据？
**答**：不会。reduce kernel 的 load 带 `mask=(k_blocks<written_k_blocks) & mask_m & (offs_d<headdim)` 且 `other=0.0`，其中 `mask_m = offs_m < seqlen_q=1` 只放行第 0 行；脏数据所在行被屏蔽为 0，不参与求和。

---

### 4.4 reduce kernel：跨 K 块归约 PartialDQ → DQ

#### 4.4.1 概念说明

stage1 之后，`PartialDQ[k_block, m, d]` 存的是「K 块 k_block 对 `dQ[m,d]` 的贡献」。由 4.1 的公式：

\[ \text{dQ}[m,d] = \tau\sum_{k}\text{dS}[m,k]\,K[k,d] = \sum_{b}\text{PartialDQ}[b,m,d] \]

reduce kernel 就做这个**沿 K 块的线性求和**。注意它是普通加法（不是前向 decode 的 log-sum-exp 合并）——因为梯度贡献已经在「线性域」，无需再做对数域 stabilize。

reduce 还有一个细节：它不能假设 `partial_dq` 的 K 块数等于 `num_k_blocks`（autotune 可能选不同 `BLOCK_N`，启动块数会变），故读 `WrittenKBlocks[off_hb]` 拿到 stage1 实际写的块数，只对该前缀求和。

#### 4.4.2 核心流程

```text
grid = (cdiv(D, BLOCK_HEADDIM), cdiv(Nq, BLOCK_M), B*H)
pid(0)=d_block，pid(1)=q_block，pid(2)=off_hb
  written = WrittenKBlocks[off_hb]
  acc = zeros([BLOCK_M, BLOCK_HEADDIM])
  for start_k in range(0, written, BLOCK_K):
      partial = load(PartialDQ[k_blocks, offs_m, offs_d], mask=..., other=0)
      acc += sum(partial, axis=0)            # 沿 K 块归约
  store(DQ[offs_m, offs_d], acc)
```

#### 4.4.3 源码精读

reduce kernel 的程序映射与 K 块前缀读取：

[src/ffpa_attn/triton/_ffpa_bwd.py:2000-2022](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L2000-L2022) 中文说明：三维网格分别对应 head_dim 块、query 行块、batch×head；`written_k_blocks = tl.load(WrittenKBlocks + off_hb)` 取实际块数，`for start_k in range(0, written_k_blocks, BLOCK_K)` 只遍历真正写了的 K 块，`acc += tl.sum(partial, axis=0)` 把 `[BLOCK_K,BLOCK_M,BLOCK_HEADDIM]` 沿 K 块维归约成 `[BLOCK_M,BLOCK_HEADDIM]`。

reduce 在启动器中的 grid 与 block 配置：

[src/ffpa_attn/triton/_ffpa_bwd.py:2445-2468](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L2445-L2468) 中文说明：reduce grid 为 `(cdiv(headdim, reduce_block_headdim_decode), cdiv(seqlen_q, block_m_decode), batch*nheads)`；`BLOCK_K=64` 是一次归约的 K 块批量；reduce 之后即 `return`，结束 decode 反向。

stage1 与 reduce 的「握手」链路回顾：stage1 的首个 K 块程序写 `WrittenKBlocks`（1756-1757），reduce 读取它（2011）。这一对读写让 reduce 不依赖启动器对块数的静态计算，能容忍 autotune 改变 `BLOCK_N`。

#### 4.4.4 代码实践

1. **实践目标**：验证 reduce 的求和正确性。
2. **操作步骤**：阅读 `_ffpa_bwd_decode_dq_reduce_kernel`，对照 4.4.1 的公式，确认 `acc` 的累加等价于「对所有 K 块的 partial_dq 求和」。
3. **需要观察的现象**：reduce 是纯求和（无 exp/log），与 decode **前向** stage2 的 log-sum-exp 合并（u4-l3）形成对照。
4. **预期结果**：手推一个 toy 例子（2 个 K 块、`headdim=4`、`Nq=1`），手算 `PartialDQ` 后求和，与公式 `dQ = Σ_b PartialDQ[b,0,:]` 一致。

#### 4.4.5 小练习与答案

**Q1**：reduce 为什么不像前向 decode stage2 那样用 log-sum-exp 合并？
**答**：前向合并的是各 chunk 的 softmax 输出（指数域，数值范围差异大，需 LSE 稳定化）；反向这里合并的是**梯度在线性域的部分和**，直接相加即为正确梯度，无需对数域 stabilize。

**Q2**：若 `WrittenKBlocks` 没写（假设 stage1 跳过了首个 K 块程序），reduce 会怎样？
**答**：`written_k_blocks` 会读到 `empty` 的未初始化垃圾值，reduce 的循环范围不确定，结果错误。这就是为什么 stage1 用 `if start_n_block == 0` 显式写入它——保证每个 head 必有一个 program 负责记录块数。

---

## 5. 综合实践：追踪 decode 反向（Nq=1, Nkv=8192, D=512）的完整数据流

本任务把三个最小模块串起来，完整还原一次单 query decode 反向的张量形状与 kernel 调用。

**步骤 1：手算形状（不依赖 GPU，可立即验证）**

设 `B=1, H=32, Nq=1, Nkv=8192, D=512`，dtype=fp16，非 autotune。按启动器逻辑：

| 量 | 取值 | 依据 |
|---|---|---|
| `use_gemv` | `True`（Nq==1） | 2306 行 |
| `BLOCK_M / BLOCK_N` | `8 / 64` | 2307-2308 行 |
| `num_k_blocks` | `cdiv(8192, 64) = 128` | 2338 行 |
| `partial_dq` 形状 | `(1, 32, 128, 8, 512)` fp32 | 2343-2347 行 |
| `written_k_blocks` 形状 | `(32,)` int32，每元素=128 | 2348-2350、1757 行 |
| stage1 grid | `(128, 32)` = 4096 个 program | 2352-2353 行 |
| reduce grid | `(cdiv(512,64)=8, cdiv(1,8)=1, 32)` = `(8,1,32)` | 2446-2448 行 |

**步骤 2：描述数据流**

1. 预处理 kernel 先算 `delta`，形状同 `lse`（`[B,H,Nq_rounded]`，`Nq_rounded` 来自前向 LSE 的 padding 存储）。
2. stage1 的 4096 个 program 各处理一个 K 块（64 个 key）：
   - 用 GEMV 一维 score 算出本块的 `dK/dV`（独占这 64 个 key 位置，普通 store）；
   - 算 `partial_dq[k_block, 0, :512]`（第 0 行，长度 512）写入 `partial_dq`。
   - 首个 K 块程序同时把 `128` 写入 `written_k_blocks[h]`。
3. reduce 的 256 个 program（`8×32`）每个负责一小段 head_dim（64 宽）×唯一 query 行块：
   - 读 `written_k_blocks[h]=128`，把 `partial_dq[0:128, 0, d_slice]` 求和 → `DQ[0, d_slice]`。

**步骤 3：解释「为何 Nq==1 用 GEMV」**

单 query 时矩阵路径 `BLOCK_M=8` 的有效行占比仅 `1/8`，`tl.dot` 的 7/8 query lane 是浪费；GEMV 把 score/dP/partial_dq 全部降为一维，用 `tl.sum` 归约替代矩阵乘，且配更小 `BLOCK_N=64` 提高并行度，更贴合 GEMV「偏带宽、低算力」的特性。

**步骤 4：可选的运行验证（待本地验证，需 CUDA + ffpa_attn 已装）**

下面脚本（**示例代码**）复用 `tests/test_ffpa_bwd.py:733` 的形状，对照 SDPA 校验 dQ/dK/dV：

```python
import torch, math
from ffpa_attn import ffpa_attn_func

B, H, Nq, Nkv, D = 1, 32, 1, 8192, 512
torch.manual_seed(42)
q = torch.randn(B, H, Nq, D, dtype=torch.float16, device="cuda", requires_grad=True)
k = torch.randn(B, H, Nkv, D, dtype=torch.float16, device="cuda", requires_grad=True)
v = torch.randn(B, H, Nkv, D, dtype=torch.float16, device="cuda", requires_grad=True)

scale = 1.0 / math.sqrt(D)
out = ffpa_attn_func(q, k, v, scale=scale, backward_backend="triton")
out.sum().backward()   # 此处命中 decode 反向 (Nq=1 < 8, USE_GEMV=True)

# 参考 SDPA 反向，逐梯度对比（参考 _sdpa_ref_grads 实现）
ref = torch.nn.functional.scaled_dot_product_attention(
    q, k, v, scale=scale)
ref.sum().backward()
print("dQ max_abs_err:", (q.grad - q.grad).abs().max().item())  # 占位，需分桶保存 ffpa 与 sdpa 各自梯度
```

> 注：上面脚本只演示调用入口；要真正逐梯度对比，需像 `tests/test_ffpa_bwd.py` 那样分别保存 FFPA 与 SDPA 的 `.grad`（不能共用同一组叶子张量）。完整对照写法请直接参考该测试文件。运行结果**待本地验证**。

## 6. 本讲小结

- decode 反向（`Nq < 8`）面对的核心矛盾：query 维度太短撑不起矩阵 kernel 的并行度，于是**把并行轴换成 K 列块**，用 `cdiv(Nk,BN)×B×H` 个 program 填满 SM。
- 关键的**所有权不对称**：`dK/dV` 的下标是 K 位置、被各 K 块独占（普通 store、无归约）；`dQ` 的下标是 Q 位置、被所有 K 块共享 → 必须 stage1 写「部分贡献」`PartialDQ`、再由 reduce 求和。
- `_ffpa_bwd_decode_stage1_kernel`：一个 program 处理一个 K 块，Split-D 分片算 score/dP，单次算出 `dS` 复用到 `dK/dV/PartialDQ`，并通过 `WrittenKBlocks` 与 reduce「握手」。
- `_ffpa_bwd_decode_dq_reduce_kernel`：沿 K 块做**线性求和**（非 log-sum-exp），用 `WrittenKBlocks` 限定求和前缀，`mask` 屏蔽未写区域，故 `partial_dq` 用 `empty` 即可。
- `USE_GEMV`（`Nq==1`）：把所有 tile 降为一维向量、用 `tl.sum` 归约，避免矩阵路径 `BLOCK_M` 内 query lane 的浪费，并配更小 `BLOCK_N` 适配 GEMV 偏带宽的特性。

## 7. 下一步学习建议

- 本讲是 Triton 反向的最后一篇。建议回到 [u5-l4](u5-l4-bwd-advanced-tma-ws-persist.md) 阅读 `enable_tma / enable_ws / persist_dkdv / split_launch` 等反向高级开关（注意这些开关的 SM90 专用变体仅对 `seqlen_q >= 8` 的主路径生效，decode 路径不触发）。
- 若想对照「跨块归约」的另一种形态，可重读 [u4-l3](u4-l3-decode-fwd-split-kv.md) decode **前向**的 stage2 log-sum-exp 合并，体会「前向合并在线性→指数域、反向合并在纯线性域」的差异。
- 想验证理解，可运行 `pytest tests/test_ffpa_bwd.py -k decode`，重点阅读 `test_ffpa_bwd_triton_decode_matches_sdpa`（640 行起，覆盖 `Nq∈{1,2,3,4,7}` × {base,causal,mask,gqa,d512}）与单 query 大 KV 的 `test_ffpa_bwd_triton_decode_autotune_fp32_kv_storage_matches_sdpa`（733 行）。
