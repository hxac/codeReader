# dK/dV 与 dQ kernel：shared program-id 设计

## 1. 本讲目标

本讲是 Triton 反向的第二篇，承接 [u5-l1](./u5-l1-bwd-algo-delta-preprocess.md) 的 delta 预处理，进入反向主路径（`Nq >= 8`）真正计算三个梯度 dQ/dK/dV 的 kernel。

学完后你应该能够：

1. 说清 FFPA 反向主路径的 **shared program-id（共享 pid）网格**：为什么一个 pid 同时被当作「K 列块索引」和「Q 行块索引」来用，以及 `grid dim0 = max(cdiv(Nk,BN), cdiv(Nq,BM))` 的来历。
2. 读懂 [`_ffpa_bwd_dkdv`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L591-L832)（按 Q 块累加 dK/dV）与 [`_ffpa_bwd_dq`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L1149-L1302)（按 K 块累加 dQ）两个 kernel 的内部循环。
3. 解释一个关键的设计权衡：**为什么非融合路径里 dQ 可以不用 `atomic_add`，而融合变体 [`_ffpa_bwd_dkdvdq`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L866-L1144) 里 dQ 又必须用 `atomic_add`**——这背后是「输出 tile 的所有权（ownership）」问题。
4. 知道反向主路径有三种 launch 模式（单 launch 共享 pid / split 双 launch / dkdvdq 融合），以及它们由哪些开关触发。

本讲**不**覆盖 decode 反向（`Nq < 8`）的 stage1+reduce 两阶段路径，那是 [u5-l3](./u5-l3-decode-bwd-dq-reduce.md) 的主题；也**不**深入 SM90 专用变体与 TMA/warp-specialize 开关，那是 [u5-l4](./u5-l4-bwd-advanced-tma-ws-persist.md) 的主题。

## 2. 前置知识

本讲默认你已经掌握：

- **Split-D 精细分块**（[u4-l2](./u4-l2-split-d-fine-grained-tiling.md)）：head_dim 太大装不进寄存器，所以两次矩阵乘（QKᵀ 与 PV）都要沿 D 维切成 `BLOCK_HEADDIM` 宽的片段循环累加。反向的 dK/dV/dQ 同样是矩阵乘，也一样要走 D 分片。
- **online softmax 与反向链式法则**（[u4-l1](./u4-l1-triton-fwd-online-softmax.md)、[u5-l1](./u5-l1-bwd-algo-delta-preprocess.md)）：反向需要重建 `P = exp(S − lse)`，并用 `delta = rowsum(dO·O)` 修正 softmax 的行间耦合。
- **Triton 的 program / grid / pid 模型**：一次 kernel launch 启动一个三维 grid，每个 program 用 `tl.program_id(axis)` 拿到自己在这三维里的编号，grid 的某一维可以是「逻辑上不存在、只是占位」的 1。

几个本讲要反复用到的记号：

| 记号 | 含义 |
|---|---|
| `Nq` / `seqlen_q` | query 序列长度（行数） |
| `Nk` / `seqlen_k` | key/value 序列长度（列数） |
| `BM` / `BLOCK_M` | Q 行方向的 tile 宽 |
| `BN` / `BLOCK_N` | K 列方向的 tile 宽 |
| `BD` / `BLOCK_HEADDIM` | D 分片宽度（Split-D 切片） |
| `pid` | `tl.program_id(0)`，本讲的主角 |

## 3. 本讲源码地图

本讲几乎全部内容集中在一个文件里：

| 文件 | 作用 |
|---|---|
| [src/ffpa_attn/triton/_ffpa_bwd.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py) | Triton 反向全部 kernel 与启动器 |

文件内本讲涉及的关键符号：

- [`_ffpa_bwd_kernel_impl`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L476-L586)：单 launch 主 kernel，按 `USE_DKDVDQ_FUSION` 分发到融合或非融合两条路。
- [`_ffpa_bwd_dkdv`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L591-L832)：dK/dV 角色，pid 当 K 列块。
- [`_ffpa_bwd_dkdvdq`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L866-L1144)：融合变体，dQ 用 atomic_add。
- [`_ffpa_bwd_dq`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L1149-L1302)：dQ 角色，pid 当 Q 行块。
- [`grid`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L2471-L2485)：启动器里计算网格的闭包，shared-pid 的「max」就出自这里。

## 4. 核心概念与源码讲解

### 4.1 shared program-id 网格：一个 pid 身兼两职

#### 4.1.1 概念说明

反向要算三个梯度，它们的「自然归属维度」不同。把 softmax scale 记为 τ（代码里已折进 `dS`），由链式法则（详见 u5-l1）可得每个 score tile `[BM, BN]` 的贡献：

\[
\begin{aligned}
dK_{[BN,BD]} &= dS^\top \cdot Q_{[BM,BD]} \\
dV_{[BN,BD]} &= P_{drop}^\top \cdot dO_{[BM,BD]} \\
dQ_{[BM,BD]} &= dS \cdot K_{[BN,BD]}
\end{aligned}
\]

注意三者累加的方向：

- **dK、dV 的第一维是 K 位置 `n`** → 一个完整的 dK/dV tile 由「某个 K 列块」拥有，需要把所有 Q 行块的贡献累加进来。
- **dQ 的第一维是 Q 位置 `q`** → 一个完整的 dQ tile 由「某个 Q 行块」拥有，需要把所有 K 列块的贡献累加进来。

这就引出一个朴素问题：如果让一个 program 负责一个 K 列块（算 dK/dV），那它得遍历所有 Q 行块；如果让一个 program 负责一个 Q 行块（算 dQ），那它得遍历所有 K 列块。FFPA 借鉴 FlashAttention-2 反向的做法，**让同一个 pid 在两个角色里被重新解释**：

- 在 `_ffpa_bwd_dkdv` 里，`pid` 是 K 列块编号：`start_n = pid * BN`。
- 在 `_ffpa_bwd_dq` 里，`pid` 是 Q 行块编号：`start_m = pid * BM`。

因为「K 列块个数」和「Q 行块个数」不一定相等，网格第 0 维取二者的**最大值**，多出来的 pid 只做一个角色（另一个角色由 `if start < seqlen` 守卫跳过）。这样每个 dK/dV tile 和每个 dQ tile 都有**唯一的 program 拥有者**，全部可以用普通 store，不需要原子操作。

#### 4.1.2 核心流程

shared-pid 单 launch 的网格（非融合时）：

\[
\text{grid} = \Big(\;\max\!\big(\lceil N_k/BN\rceil,\ \lceil N_q/BM\rceil\big),\ 1,\ B{\cdot}H\;\Big)
\]

每个 program（`pid` 固定，`off_hb` 固定 batch×head）做两件事：

```text
program(pid, off_hb):
  # 角色 A：dK/dV —— pid 当 K 列块
  start_n = pid * BN
  if start_n < Nk:
      遍历相关 Q 行块 start_m:
          重建 S, dP (Split-D 分片) → 算 dS
          dK[start_n] = load-add-store 累加 dSᵀ@Q
          dV[start_n] = load-add-store 累加 P_dropᵀ@dO

  # 角色 B：dQ —— pid 当 Q 行块
  start_m = pid * BM
  if start_m < Nq:
      遍历相关 K 列块 start_n:
          重建 S_qk, dP_qk (Split-D 分片) → 算 dS_qk
          dQ[start_m] = load-add-store 累加 dS@K
```

关键点：角色 A 内部「遍历 Q 行块」是**同一个 program 串行做的**，所以 dK/dV 的累加发生在单一 program 内部，不存在并发写；角色 B 同理。这就是非原子的根因——**所有权（ownership）**：每个输出 tile 只被一个 program 写。

#### 4.1.3 源码精读

设计意图写在 kernel 上方的注释里，明确说「one program_id serves as both the K-column block index and the Q-row block index」，并指出「Because each program owns a unique Q-row block, dQ can be written non-atomically」：

[src/ffpa_attn/triton/_ffpa_bwd.py:460-475](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L460-L475) — shared-pid 设计注释，说明 pid 兼任 K 列块与 Q 行块、dQ 因唯一归属而无需原子。

网格的「max」出自启动器里的 `grid` 闭包。融合时第 0 维只取 K 列块数（pid 只当 K 列块，所以 dQ 必须原子，见 4.4）；非融合时取 max：

[src/ffpa_attn/triton/_ffpa_bwd.py:2471-2485](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L2471-L2485) — `grid` 闭包，非融合分支返回 `max(cdiv(Nk,BN), cdiv(Nq,BM))`，第 1 维恒为 1（占位），第 2 维是 `batch*nheads`。

分发入口 `_ffpa_bwd_kernel_impl` 本身不取 pid，只按 `USE_DKDVDQ_FUSION` 把同一个 pid 传给两条路：

[src/ffpa_attn/triton/_ffpa_bwd.py:552-586](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L552-L586) — 单 launch 分发：融合走 `_ffpa_bwd_dkdvdq`；非融合先 `_ffpa_bwd_dkdv` 再 `_ffpa_bwd_dq`，两者共用同一个 pid。

> 备注：除「单 launch 共享 pid」外，启动器还有 **split_launch** 模式——把 dKdV 和 dQ 拆成**两次独立 launch**，各自网格只索引自己的维度（[`dkdv_grid`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L2598-L2602) 用 `cdiv(Nk,BN)`、`dq_grid` 用 `cdiv(Nq,BM)`）。它没有「max 浪费的 pid」，两个 kernel 也都非原子，代价是两次 launch 与同一套 S/dP 被两套程序各算一遍。本讲聚焦共享 pid 的单 launch 路径，split_launch 的开关语义留到 u5-l4。

#### 4.1.4 代码实践

**实践目标**：亲手把 shared-pid 的映射画出来，并验证「dQ 非原子」的推理。

**操作步骤**：

1. 取一个具体形状，例如 `Nq = Nk = 8192`、`BM = BN = 128`（ autotune 默认候选之一）。计算 `cdiv(8192,128) = 64`，于是 `grid dim0 = max(64,64) = 64`。
2. 在纸上画一张 64 行的表，每行是一个 `pid`（0~63）。对每个 pid 标出：
   - 角色 A 拥有的 K 列块 `start_n = pid*128`（覆盖 key 维 0~8192）；
   - 角色 B 拥有的 Q 行块 `start_m = pid*128`（覆盖 query 维 0~8192）。
3. 再取一个不等长的形状体会「max」的作用：`Nq = 2048`（`cdiv=16`）、`Nk = 8192`（`cdiv=64`），`grid dim0 = 64`。标出 pid 0~15 两个角色都干活，pid 16~63 只有角色 A（dK/dV）干活、角色 B 被 `if start_m < seqlen_q` 跳过。
4. 用一句话写下「为什么 dQ 不需要 atomic」：因为每个 Q 行块 `start_m` 恰好被一个 pid 拥有，K 列块的累加发生在该 program 内部的串行循环里，不存在两个 program 写同一个 dQ 元素。

**需要观察的现象**：在 `Nq != Nk` 时，`max` 让网格按较大的那个维度启动，浪费的 pid 不会算出错误结果——它们只是跳过越界角色。

**预期结果**：你能用「所有权唯一 → 单写者 → 无需原子」一句话解释非融合 dQ 的非原子性；并能指出融合变体为何打破了这一点（见 4.4）。

> 这个映射图无需运行代码即可完成；若想在真实 kernel 里核对 pid 解释，可在 [`_ffpa_bwd_dkdv`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L652-L655) 与 [`_ffpa_bwd_dq`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L1196-L1199) 的 `pid = tl.program_id(0)` 处对照。

#### 4.1.5 小练习与答案

**练习 1**：若 `Nq = 1024`、`Nk = 4096`、`BM = BN = 128`，非融合单 launch 的 `grid dim0` 是多少？哪些 pid 只算 dK/dV？

答案：`cdiv(1024,128)=8`、`cdiv(4096,128)=32`，`grid dim0 = max(8,32) = 32`。pid 0~7 两个角色都做；pid 8~31 只做角色 A（dK/dV），因为 `start_m = pid*128 >= 1024` 被守卫跳过。

**练习 2**：为什么网格第 1 维固定为 1 却还要写出来？

答案：Triton 的 `program_id` 按轴取值，第 2 维（`program_id(2)`）用于 `batch*nheads` 索引 `off_hb`。第 1 维留作占位的 1，是为了让 batch×head 落在 `program_id(2)` 上，与 kernel 内 `off_hb = tl.program_id(2)` 的取法对齐（见 [`_ffpa_bwd_dkdv` 取 pid 处](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L652-L655)）。

---

### 4.2 `_ffpa_bwd_dkdv`：dK/dV 角色（pid 当 K 列块）

#### 4.2.1 概念说明

`_ffpa_bwd_dkdv` 负责 dK 与 dV。它把 `pid` 解释为一个 **K 列块** `start_n`，然后遍历所有相关的 Q 行块，逐块重建 score 与 dS，把贡献累加进 `DK[start_n]` 与 `DV[start_n]`。因为整个 K 列块只被这一个 program 拥有，累加是 program 内部的串行循环，写回用普通的 **load → add → store**，不用原子。

由于 head_dim 很大，Q/K/V/dO 不能整块加载，必须沿 D 分片（Split-D）：先用一个 D 分片循环把整个 score tile `S` 与 `dP` 累加出来，算出 `dS`，再用第二个 D 分片循环把 `dS` 与 `P_drop` 分片累加进 dK/dV。

#### 4.2.2 核心流程

```text
pid -> start_n = pid * BN          # 拥有一个 K 列块
if start_n >= Nk: return            # 越界 pid 跳过（max 带来的空角色）
for start_m in 相关 Q 行块:          # causal 时从 start_n 对齐处开始
    # Phase 1: Split-D 累加出完整 S 与 dP
    S = 0; dP = 0
    for d_chunk in D 分片:
        q,k,v,do = load(Q,K,V,DO 的 [BM,BD] 片段)
        S  = dot(q, kᵀ, acc=S)      # QKᵀ 沿 D 归约
        dP = dot(do, vᵀ, acc=dP)    # dO·Vᵀ 沿 D 归约
    # 应用 causal / attn_bias / dropout，重建 P、dS
    P = exp(S - lse); dS = P*(dP - Di)*scale
    # Phase 2: Split-D 把 dS、P_drop 分片累加进 dK/dV
    for d_chunk in D 分片:
        dk_d = dSᵀ @ Q_片段;  dv_d = P_dropᵀ @ dO_片段
        DK[start_n] = (首个 Q 块) ? store(dk_d) : load+add+store
        DV[start_n] = (首个 Q 块) ? store(dv_d) : load+add+store
```

causal 时用 `begin_m = start_n // BM * BM` 跳过必然被掩码的前导 Q 块（块级剪枝），剩余的逐元素掩码由 `tl.where(offs_qm >= offs_n, ...)` 兜底。

#### 4.2.3 源码精读

pid 解释与越界守卫：

[src/ffpa_attn/triton/_ffpa_bwd.py:652-672](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L652-L672) — `pid = program_id(0)` 当 K 列块，`start_n = pid * BLOCK_N`，`if start_n < seqlen_k` 守卫越界 pid；causal 的 `begin_m` 块级剪枝。

Phase 1 的 Split-D score/dP 累加（沿 D 归约）：

[src/ffpa_attn/triton/_ffpa_bwd.py:683-709](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L683-L709) — 每个程序先把 `S`、`dP` 在 D 分片循环里用 `tl.dot(..., acc=...)` 累加成完整 `[BM,BN]` tile，再进入因果/偏置/dropout 与 dS 计算。

Phase 2 的 dK/dV load-add-store（非原子累加的关键）：

[src/ffpa_attn/triton/_ffpa_bwd.py:802-831](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L802-L831) — `dk_d = trans(dot(trans(q), dS))` 算出 dSᵀ@Q；首个 Q 块直接 `tl.store`，其余 `tl.load` 旧值 → 相加 → `tl.store`，全程无 `atomic_add`，并用 `eviction_policy="evict_last"` 把反复读写的梯度留在缓存。

精度说明：这种 load-add-store 的累加精度由 DK/DV 缓冲区的**存储 dtype** 决定（注释见 [src/ffpa_attn/triton/_ffpa_bwd.py:798-801](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L798-L801)）；wrapper 若分配 fp32 缓冲（`grad_kv_storage_dtype`），跨块累加就保持 fp32，否则每次 store 都按 bf16/fp16 舍入。

#### 4.2.4 代码实践

**实践目标**：核对「首个 Q 块 store、其余 load-add-store」这一分支，理解它如何替代原子。

**操作步骤**：

1. 打开 [src/ffpa_attn/triton/_ffpa_bwd.py:802-831](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L802-L831)。
2. 找到 `if start_m == begin_m:` 分支（直接 store）与 `else:` 分支（load + add + store）。
3. 回答：为什么「首个 Q 块」可以不 load？因为此时 `DK[start_n]` 还没有任何贡献，等价于从 0 开始累加，直接 store 即可；后续 Q 块则需要读回之前累加的结果。

**预期结果**：你能说清这一分支用「program 内串行循环 + 首块特判」实现了等价于原子累加的效果，但没有原子操作的成本。

#### 4.2.5 小练习与答案

**练习 1**：dK 的公式是 `dSᵀ @ Q`，代码里却写成 `tl.trans(tl.dot(tl.trans(q), dS))`。为什么不直接写 `tl.dot(tl.trans(dS), q)`？

答案：两者数学等价（`(qᵀ·dS)ᵀ = dSᵀ·q`），写法差异来自 Triton `tl.dot` 对操作数布局/MMA 指令的约束与转置成本，作者选择了能映射到高效 MMA 的一种排布。

**练习 2**：`eviction_policy="evict_last"` 在这里起什么作用？

答案：dK/dV 在 Q 块循环里被反复 load/store，`evict_last` 告诉缓存「这块数据近期还会再用，尽量晚淘汰」，降低反复全局往返的带宽压力。

---

### 4.3 `_ffpa_bwd_dq`：dQ 角色（pid 当 Q 行块）

#### 4.3.1 概念说明

`_ffpa_bwd_dq` 与 dKdV 完全对偶：它把**同一个 pid** 解释为一个 **Q 行块** `start_m`，遍历所有相关 K 列块，逐块重建 score 与 dS，把贡献累加进 `DQ[start_m]`。因为整个 Q 行块只被这一个 program 拥有，累加同样是 program 内部串行循环，写回用 load-add-store，**不需要原子**——这是非融合路径的核心收益。

#### 4.3.2 核心流程

```text
pid -> start_m = pid * BM          # 拥有一个 Q 行块
if start_m >= Nq: return            # 越界 pid 跳过
for start_n in 相关 K 列块:          # causal 时只到 start_m+BM 为止
    # Phase 1: Split-D 累加 S_qk 与 dP_qk
    S_qk = 0; dP_qk = 0
    for d_chunk in D 分片:
        S_qk  = dot(q, kᵀ, acc=S_qk)
        dP_qk = dot(do, vᵀ, acc=dP_qk)
    # 重建 P_qk、dS_qk（与 dKdV 完全相同的因果/偏置/dropout 处理）
    P_qk = exp(S_qk - lse); dS_qk = P_qk*(dP_qk - Di)*scale
    # Phase 2: Split-D 把 dS_qk 分片累加进 dQ
    for d_chunk in D 分片:
        dq_d = dS_qk @ K_片段
        DQ[start_m] = (首个 K 块) ? store(dq_d) : load+add+store
```

causal 时 `end_n_k = start_m + BM` 提前结束 K 块循环（块级剪枝），逐元素由 `tl.where(offs_m >= offs_nk, ...)` 兜底。

#### 4.3.3 源码精读

pid 解释为 Q 行块与越界守卫：

[src/ffpa_attn/triton/_ffpa_bwd.py:1196-1213](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L1196-L1213) — `pid = program_id(0)` 当 Q 行块，`start_m = pid * BLOCK_M`，`if start_m < seqlen_q` 守卫；causal 的 `end_n_k` 块级剪枝。

dS 重建（与 dKdV 共用同一套因果/偏置/dropout 逻辑）：

[src/ffpa_attn/triton/_ffpa_bwd.py:1251-1280](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L1251-L1280) — 因果/bias/dropout 处理后得到 `dS_qk = (P_qk * (dP_qk - Di) * softmax_scale)`。

dQ 的非原子 load-add-store（本讲的「非原子 dQ」就发生在这里）：

[src/ffpa_attn/triton/_ffpa_bwd.py:1282-1301](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L1282-L1301) — `dq_d = tl.dot(dS_qk, k)`；首个 K 块直接 `tl.store`，其余 `tl.load + add + tl.store`，全程无 `atomic_add`。

> 注意一个**性能代价**（写在文件顶部 docstring）：在单 launch 共享 pid 路径里，同一个 program 先后执行 dKdV 与 dQ 两个角色，两套循环各自重建了一遍 score tile `S`。也就是说整张 score 矩阵在网格层面被算了**两遍**。这是 kernel 结构本身的问题，不是 launch 形状的问题（见 [src/ffpa_attn/triton/_ffpa_bwd.py:50-57](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L50-L57)）。融合变体正是为了消除这一重复重算而存在——代价是 dQ 改用原子。

#### 4.3.4 代码实践

**实践目标**：对比 dQ 与 dKdV 的对偶结构，确认「dQ 非原子」的代码事实。

**操作步骤**：

1. 并排打开 [dKdV 的累加段 802-831](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L802-L831) 与 [dQ 的累加段 1282-1301](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L1282-L1301)。
2. 用 `grep -n "atomic_add" src/ffpa_attn/triton/_ffpa_bwd.py` 在本文件里搜原子操作，确认 `_ffpa_bwd_dkdv` 与 `_ffpa_bwd_dq` 函数体内**没有** `atomic_add` 写 dQ/dK/dV（atomic 只出现在 attn_bias 梯度与融合变体里）。

**预期结果**：两个 kernel 的累加段结构对偶（一个外层遍历 Q、一个外层遍历 K），且都不用原子写主梯度。

#### 4.3.5 小练习与答案

**练习 1**：dQ 的循环为什么是「遍历 K 列块」而不是「遍历 Q 行块」？

答案：dQ 的拥有者是 Q 行块（`start_m = pid*BM` 固定），需要把所有 K 列块对该 Q 块的贡献累加起来，所以内层循环必须遍历 K 列块。遍历 Q 行块是 dKdV 的事。

**练习 2**：非融合路径里，整张 score 矩阵 S 大约被计算了几次？

答案：约两次——dKdV 角色扫一遍（每个 K 块遍历所有 Q 块）、dQ 角色又扫一遍（每个 Q 块遍历所有 K 块），合计覆盖全矩阵两遍。这正是融合变体想消除的开销。

---

### 4.4 `_ffpa_bwd_dkdvdq`：融合变体与触发条件（dQ 改用 atomic_add）

#### 4.4.1 概念说明

融合变体 `_ffpa_bwd_dkdvdq` 解决上一节提到的「score 算两遍」问题：它对每个 (K 块, Q 块) 配对**只算一次 dS**，然后把这同一份 dS 复用到 dK、dV、dQ 三个累加上。

但天下没有免费的午餐。为了复用 dS，程序必须以 **K 块**为拥有者（遍历该 K 块的所有 Q 块）。于是 pid 只索引 K 列块（`grid dim0 = cdiv(Nk,BN)`，不再取 max），这意味着：

- dK/dV 仍然由唯一的 K 块拥有者写 → 仍可非原子 load-add-store；
- **dQ 不再有唯一拥有者**——多个 K 块程序都会对同一个 Q 行块贡献 dQ → 必须用 `tl.atomic_add`。

这就是「所有权」与「重复重算」之间的权衡：非融合用「score 算两遍」换「dQ 非原子」；融合用「dQ 原子」换「score 只算一遍」。

#### 4.4.2 核心流程

```text
pid -> start_n = pid * BN          # 只当 K 列块（grid 不取 max）
if start_n >= Nk: return
for start_m in 相关 Q 行块:
    Phase 1: Split-D 算出 S, dP（一次）
    重建 P, dS（一次）
    Phase 2: 复用同一份 dS / P_drop 到三个累加
        dK[start_n] = load-add-store(dSᵀ@Q)      # 非原子
        dV[start_n] = load-add-store(P_dropᵀ@dO) # 非原子
        dQ[start_m] = atomic_add(dS@K)           # 原子！多 K 块共写一个 Q 块
```

触发条件（在启动器里判定）：

```python
use_dkdvdq_fusion = (
    bool(int(os.environ.get("FFPA_TRITON_BWD_FUSE_DKDVDQ", "0")))
    and seqlen_q >= 8 and not split_launch
)
```

即：默认**关闭**，需显式设环境变量 `FFPA_TRITON_BWD_FUSE_DKDVDQ=1`、且为主路径（`Nq >= 8`）、且未启用 split_launch 时才生效。

#### 4.4.3 源码精读

融合判定与网格分支：

[src/ffpa_attn/triton/_ffpa_bwd.py:2104-2107](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L2104-L2107) — `use_dkdvdq_fusion` 由 `FFPA_TRITON_BWD_FUSE_DKDVDQ`、`seqlen_q >= 8`、`not split_launch` 共同决定。

[src/ffpa_attn/triton/_ffpa_bwd.py:2472-2477](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L2472-L2477) — 融合时 `grid dim0 = cdiv(Nk, BN)`（只索引 K 块），这是 dQ 必须原子的根本原因。

Phase 2 里 dK/dV 非原子、dQ 原子的三段对照：

[src/ffpa_attn/triton/_ffpa_bwd.py:1064-1144](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L1064-L1144) — dK（1072-1093）、dV（1095-1118）用 load-add-store；dQ 用 `tl.atomic_add(dq_ptrs, dq_d, sem="relaxed", ...)`（1144）。

融合路径的一个**精度陷阱**写在原子调用上方的长注释里：当 DQ 缓冲是 bf16/fp16 时，每次 `atomic_add` 都要做 load→fp32 加→舍入回存储 dtype→store 的往返，在 SM<90（如 L20/SM89）上 bf16 没有硬件 atomic，会退化成 CAS 循环，性能急剧下降（约 2.4× 慢于 fp16）。两条缓解：① 用非融合路径（默认）；② 用 `grad_q_storage_dtype=torch.float32` 走 fp32 硬件原子（更准但 2× dQ 显存）。

[src/ffpa_attn/triton/_ffpa_bwd.py:1128-1144](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L1128-L1144) — bf16/fp16 DQ 原子精度的注释与 `tl.atomic_add` 调用，附 Triton issue 链接。

#### 4.4.4 代码实践

**实践目标**：理解「融合省重算、但 dQ 付原子代价」的权衡，并知道如何切换。

**操作步骤**：

1. 在源码里确认触发条件 [src/ffpa_attn/triton/_ffpa_bwd.py:2104-2107](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L2104-L2107)，写下要开启融合需要导出的环境变量。
2. 回答：开启融合后，dQ 的写者从「1 个」变成了「`cdiv(Nk,BN)` 个」，所以必须用 `atomic_add`；而 dK/dV 的写者仍是「1 个 K 块拥有者」，所以仍非原子。
3. （可选，待本地验证）在 GPU 上分别用默认（非融合）与 `FFPA_TRITON_BWD_FUSE_DKDVDQ=1` 跑同一个反向用例，比较吞吐与 dQ 误差，体会「原子换重算」的得失。

**预期结果**：你能用一句话讲清「融合 = score 算一次 + dQ 原子；非融合 = score 算两次 + dQ 非原子」，并知道默认是非融合。

#### 4.4.5 小练习与答案

**练习 1**：为什么融合变体的 dK/dV 仍然可以非原子？

答案：融合变体仍以 K 块为程序拥有者，每个 K 块由唯一 program 写 dK/dV，累加在该 program 内部串行完成，所以 dK/dV 保持 load-add-store 非原子；只有 dQ 因为换成了「多 K 块共写一个 Q 块」才需要原子。

**练习 2**：在 L20（SM89）上把 DQ 设成 bf16 并开启融合，为什么可能比 fp16 慢很多？

答案：SM<90 没有 bf16 硬件 atomicAdd，`atomic_add` 退化成 CAS 循环，多个 K 块程序在同一 dQ 元素上激烈争用，性能显著下降；fp16 在该硬件上有更高效的原子路径，故反而更快。

---

## 5. 综合实践

把本讲的三件事——shared-pid 映射、非原子 dQ、融合切换——串成一个可运行的小任务。

**任务**：复现 [docs/index.md 的 backward 示例](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/index.md#L169-L200)，并在默认（非融合）与融合两种模式下比较 dQ 与 SDPA 的误差。

```python
# 示例代码：基于 docs/index.md 的 backward 示例改写
import os, math, torch
import torch.nn.functional as F
from ffpa_attn import ffpa_attn_func

B, H, N, D = 1, 32, 8192, 512
scale = 1.0 / math.sqrt(D)

def run(fuse: bool):
    if fuse:
        os.environ["FFPA_TRITON_BWD_FUSE_DKDVDQ"] = "1"
    else:
        os.environ.pop("FFPA_TRITON_BWD_FUSE_DKDVDQ", None)
    q = torch.randn(B, H, N, D, dtype=torch.bfloat16, device="cuda", requires_grad=True)
    k = torch.randn(B, H, N, D, dtype=torch.bfloat16, device="cuda", requires_grad=True)
    v = torch.randn(B, H, N, D, dtype=torch.bfloat16, device="cuda", requires_grad=True)
    ffpa_attn_func(q, k, v, scale=scale).sum().backward()
    qr = q.detach().clone().requires_grad_(True)
    kr = k.detach().clone().requires_grad_(True)
    vr = v.detach().clone().requires_grad_(True)
    F.scaled_dot_product_attention(qr, kr, vr, scale=scale).sum().backward()
    print(f"fuse={fuse}: dQ max_abs_err={(q.grad - qr.grad).abs().max().item():.4e}")

run(False)  # 非融合：shared-pid，dQ 非原子
run(True)   # 融合：dQ 走 atomic_add
```

**操作步骤**：

1. 先在纸上为 `N=8192, BM=BN=128` 算出 `grid dim0 = 64`，画出 pid 0~63 各自拥有的 K 列块与 Q 行块（4.1.4 的映射图）。
2. 运行上面的脚本（需要 GPU 与已安装的 ffpa_attn）。
3. 比较两种模式的 `dQ max_abs_err`：两者都应与 SDPA 接近，但融合模式因 bf16 原子往返，误差可能略大、在 SM89 上可能更慢。

**需要观察的现象**：非融合与融合都给出与 SDPA 数值一致的 dQ；融合在 bf16 下可能误差更大或更慢（取决于硬件是否有 bf16 原子）。

**预期结果**：你能把「映射图（4.1）→ 非原子 dQ（4.3）→ 融合改用原子（4.4）」三条线索对应到一次真实的反向调用上。若本地无 GPU，明确标注「待本地验证」，转而做源码阅读型实践：用 `grep -n "atomic_add\|tl.store\|USE_DKDVDQ_FUSION" src/ffpa_attn/triton/_ffpa_bwd.py` 把三种 launch 模式的写回方式列成表。

## 6. 本讲小结

- 反向主路径（`Nq >= 8`）用 **shared program-id**：同一个 `pid` 在 `_ffpa_bwd_dkdv` 里当 K 列块、在 `_ffpa_bwd_dq` 里当 Q 行块，网格第 0 维取 `max(cdiv(Nk,BN), cdiv(Nq,BM))`。
- **所有权决定是否需要原子**：dK/dV 由 K 块拥有者单写、dQ 由 Q 块拥有者单写，所以非融合路径里三者都用 load-add-store，**无 `atomic_add`**。
- `_ffpa_bwd_dkdv` 外层遍历 Q 块、`_ffpa_bwd_dq` 外层遍历 K 块，二者结构对偶；各自的累加都用「首块直接 store、其余 load+add+store」替代原子。
- 非融合的代价是 **score 被算两遍**（dKdV 与 dQ 各扫一遍全矩阵）；融合变体 `_ffpa_bwd_dkdvdq` 只算一次 dS 并复用，但 pid 改为只索引 K 块，使 dQ 失去唯一拥有者，**必须用 `tl.atomic_add`**。
- 融合由 `FFPA_TRITON_BWD_FUSE_DKDVDQ=1` 触发（且 `Nq>=8`、未 split_launch），默认关闭；bf16 DQ 在 SM<90 上原子会退化成 CAS 循环，需用 `grad_q_storage_dtype=fp32` 缓解。
- 主路径有三种 launch 模式：单 launch 共享 pid（默认）、split 双 launch、dkdvdq 融合；Split-D 的 D 分片循环贯穿所有模式。

## 7. 下一步学习建议

- 阅读 [u5-l3](./u5-l3-decode-bwd-dq-reduce.md) 看 `Nq < 8` 的 decode 反向如何用 stage1 + reduce 两阶段解决「Q 行太少、dQ 必然跨 K 块归约」的问题，以及 `Nq==1` 的 GEMV 特化。
- 阅读 [u5-l4](./u5-l4-bwd-advanced-tma-ws-persist.md) 了解 `enable_tma`/`enable_ws`/`persist_dkdv`/`split_launch` 等反向开关的语义，以及 SM90 专用变体 `_ffpa_bwd_dkdv_persist_sm90` 如何用 fp32 寄存器累加替代本讲看到的 load-add-store 往返。
- 想验证本讲的数值结论，可跑 [tests/test_ffpa_bwd.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_bwd.py) 中大 head_dim 的用例，对照 dQ/dK/dV 与 SDPA 的容差。
