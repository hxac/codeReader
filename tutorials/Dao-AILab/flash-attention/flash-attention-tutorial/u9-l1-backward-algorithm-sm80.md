# 反向算法与 Sm80 反向 Kernel

## 1. 本讲目标

本讲聚焦 FA4 中 Ampere（SM80，也是 SM120 反向的基类）的反向 kernel `FlashAttentionBackwardSm80`。读完本讲，你应该能够：

- 手推 attention 的反向公式，写出 `dQ / dK / dV` 与输入/中间量的依赖关系。
- 说清楚反向里为什么必须**重计算** `S = QKᵀ` 与 `P = softmax(S)`，而不是把前向的 `P` 存下来。
- 看懂 Sm80 反向主循环里的 **5 段 MMA**（`S`、`dP`、`dV`、`dQ`、`dK`）如何串联，以及 `dK/dV` 用寄存器累加而 `dQ` 必须原子累加的原因。
- 理解前向保存的 `LSE` 与预处理算出的 `D`（`dPsum`）如何被复用来「免费」恢复 `P` 与做 softmax 雅可比修正。
- 掌握本版本新加的**滑窗/局部（local）掩码反向**支持：`BlockInfo.get_m_block_min_max` 如何统一计算因果/滑窗下的 `m_block` 范围，以及 `m_block_min < m_block_max` 的空块早退保护。

本讲是专家层反向系列（u9）的第一篇，依赖前置的 u6-l1（前向主循环）与 u4-l1（在线 softmax）。后续 u9-l2 讲预处理/后处理，u9-l3 讲 Hopper/Blackwell 反向。

## 2. 前置知识

阅读本讲前，请确保理解以下概念（前几讲已建立）：

- **MMA（Matrix Multiply-Accumulate）**：GPU 张量核做的小矩阵乘加，FA4 里一律是 `16×8×16`、fp16/bf16 输入、fp32 累加（见 [flash_attn/cute/flash_bwd.py:313-317](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L313-L317)）。一个 tile 的 GEMM 由若干 MMA 拼成。
- **在线 softmax 与 LSE**：前向用 `row_max m` 与 `row_sum ℓ` 逐块维护归一化，最终把 `ℓ` 改写成 `LSE = ln(ℓ) + m·softmax_scale` 存下来。`exp(LSE)` 正好是 softmax 的归一化分母。
- **tiling / gmem-smem-rmem 三级存储**：前向的「Q 常驻、K/V 流水」策略（u6-l1）。反向会把它**镜像翻转**：变成「K/V 常驻、Q/dO 流水」。
- **score_mod / mask_mod**：在 softmax 之前改分数、或改可见性的 `@cute.jit` 回调（u4-l2、u3-l1）。
- **BlockInfo**：根据掩码计算每个 tile 真正要遍历的块范围（u3-l2）。

记号约定（本讲全程使用）：

- 设 `Q∈ℝ^{N×d}`、`K,V∈ℝ^{M×d}`（单头、单 batch，忽略 scale）。
- `S = softmax_scale·QKᵀ ∈ ℝ^{N×M}`（pre-softmax 打分）。
- `P = softmax(S) ∈ ℝ^{N×M}`（注意力权重）。
- `O = P V ∈ ℝ^{N×d}`（输出）。
- 上游梯度记作 `dO`（即 `∂L/∂O`），目标是求 `dQ = ∂L/∂Q`、`dK = ∂L/∂K`、`dV = ∂L/∂V`。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注的部分 |
|---|---|---|
| [flash_attn/cute/flash_bwd.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py) | `FlashAttentionBackwardSm80`：Ampere/SM120 反向 kernel 主体 | 主循环、5 段 MMA、dQ 原子累加、滑窗/空块早退 |
| [flash_attn/cute/block_info.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_info.py) | `BlockInfo`：tile 块范围计算 | `get_m_block_min_max`（反向用）、`get_n_block_min_max` |
| [flash_attn/cute/mask.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/mask.py) | `AttentionMask`：因果/滑窗/块稀疏掩码 | `apply_mask` 的 `mask_local` 分支 |
| [flash_attn/cute/interface.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py) | 公共 API 与反向分发 | `_bwd_preprocess`（算 `D`）、`_flash_attn_bwd` 实例化 Sm80 |

辅助但不必深读：`ampere_helpers.gemm`（封装 MMA 循环）、`flash_bwd_preprocess.py`（算 `D/dPsum`）、`flash_bwd_postprocess.py`（把 `dQaccum` 收敛为 `dQ`）。

---

## 4. 核心概念与源码讲解

### 4.1 dQ/dK/dV 的数学推导与 S/P 重计算

#### 4.1.1 概念说明

反向传播的本质是「沿着计算图把上游梯度 `dO` 倒着传回 `Q/K/V`」。注意力前向只做了两件事：算打分 `S = softmax_scale·QKᵀ`，再用权重乘值 `O = softmax(S)·V`。对这两步分别做链式求导，就能得到三个梯度。

关键观察：`P = softmax(S)` 这个 `N×M` 矩阵在前向里**根本没存下来**（这正是 FlashAttention 省 `O(N²)` 显存的根源）。所以反向要算用到 `P` 的式子时，必须**就地重算**它——这正是 FA 反向要「再算一遍 `QKᵀ`」的根本原因。

#### 4.1.2 核心流程

设 `S = softmax_scale·QKᵀ`，`P = softmax(S)`，`O = PV`。给定 `dO`：

1. **`dV`**：由 `O = PV` 对 `V` 求导 →
   \[
   dV = P^{\top}\, dO
   \]
2. **`dP`**（中间量）：`P` 对 `O` 的梯度，由 `O = PV` 对 `P` 求导 →
   \[
   dP = dO\, V^{\top}
   \]
3. **`dS`**：把 `dP` 穿过 softmax。softmax 的雅可比-向量积给出
   \[
   dS_{j} = P_{j}\bigl(dP_{j} - \underbrace{\textstyle\sum_{i} P_{i}\, dP_{i}}_{D}\bigr)
   \]
   其中 `D` 是**每个 Q 行一个标量**（行和），等于 `Σ_i P_i dP_i`。后面会证明 `D = (O⊙dO).rowsum`，可以在预处理一次性算好。
4. **`dQ`**：由 `S = softmax_scale·QKᵀ` 对 `Q` 求导 →
   \[
   dQ = softmax\_scale \cdot dS\, K^{\top}
   \]
5. **`dK`**：由 `S` 对 `K` 求导 →
   \[
   dK = softmax\_scale \cdot dS^{\top}\, Q
   \]

> 注意：`softmax_scale` 在前向是乘到 `QKᵀ` 上的。反向里 `dQ/dK` 都来自 `dS = ∂L/∂(QKᵀ)`，所以最终要再乘一次 `softmax_scale`。FA4 把这个缩放放到 `dK` 的 epilogue 统一处理（见 4.3）。

**为什么要重算 `S/P`？** 因为存整个 `P∈ℝ^{N×M}` 需要 `O(N²)` 显存，正是前向千方百计避免的。反向宁可把 `QKᵀ` 再算一遍（多一次 GEMM 的算力），换来显存回到 `O(N)`。这正是 FA「精确注意力」的代价与魅力。

**「免费」恢复 `P` 的技巧**：前向已经存了 `LSE`，于是不需要再单独维护行最大值：
\[
P_{ij} = \exp\bigl(S_{ij}\cdot softmax\_scale - LSE_{i}\bigr)
\]
这正是反向里恢复 `P` 的公式（见 4.2 源码精读）。

#### 4.1.3 源码精读

整段反向的「数学心脏」在 `compute_one_m_block` 里。先看 `dS` 的计算——这是把上面第 3 步公式一字不差地写进 kernel：

```python
# dS = P * (dP - D)，其中 D 就是预处理存好的 dPsum
for r in cutlass.range(cute.size(acc_dP_mn, mode=[0]), unroll_full=True):
    grad_val = acc_S_mn[r, None].load() * (acc_dP_mn[r, None].load() - tLSErdPsum[r])
    ...
    acc_dP_mn[r, None].store(grad_val)
```
见 [flash_attn/cute/flash_bwd.py:976-991](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L976-L991)。这里 `acc_S_mn` 就是 `P`，`acc_dP_mn` 就是 `dP`，`tLSErdPsum` 就是行标量 `D`。算完的 `dS` 仍存在 `acc_dP_mn` 这个累加器里（复用寄存器）。

再看 `P` 是怎么「免费」恢复的——用前向存的 `LSE`：

```python
# P = exp2(S * scale_log2 - LSE)，exp2 换底加速
for r in cutlass.range(cute.size(acc_S_mn, mode=[0]), unroll_full=True):
    acc_S_mn[r, None].store(
        cute.math.exp2(acc_S_mn[r, None].load() * softmax_scale_log2 - tLSErLSE[r], fastmath=True)
    )
```
见 [flash_attn/cute/flash_bwd.py:952-953](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L952-L953)。`softmax_scale_log2 = softmax_scale·log₂e`（u4-l1），用 `exp2` 替代 `exp` 走硬件 `ex2` 指令。`tLSErLSE` 来自前向保存的 `mLSE`。

而 `D = (O⊙dO).rowsum` 在**预处理 kernel** 里一次性算好（注释写得很直白）：

```python
def _bwd_preprocess(...):
    """Backward preprocess: compute (o * dout).sum(dim=-1) - dLSE, lse * log2_e, and zero out dq_accum."""
```
见 [flash_attn/cute/interface.py:1203-1216](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L1203-L1216)。`- dLSE` 项用于当 `LSE` 本身也参与下游 loss 时把它的梯度加进来；标准情况下 `dLSE=0`，`D` 就是 `(O⊙dO).rowsum`。

#### 4.1.4 代码实践

**目标**：用 PyTorch autograd 拿到「真值」梯度，再用手写公式核对，建立对五条公式的信心。

**步骤**：

```python
# 示例代码（非项目源码）
import torch
torch.manual_seed(0)
N, M, d = 64, 64, 32
scale = 1.0 / d**0.5
Q = torch.randn(N, d, requires_grad=True)
K = torch.randn(M, d, requires_grad=True)
V = torch.randn(M, d, requires_grad=True)

S = scale * (Q @ K.T)            # (N, M)
P = torch.softmax(S, dim=-1)     # 注意力权重
O = P @ V                        # (N, d)

dO = torch.randn_like(O)
O.backward(dO)                   # autograd 真值
dQ_ref, dK_ref, dV_ref = Q.grad.clone(), K.grad.clone(), V.grad.clone()

# 手写公式核对
D = (O * dO).sum(-1)             # 行标量 D = (O⊙dO).rowsum, (N,)
dP = dO @ V.T                    # (N, M)
dS = P * (dP - D[:, None])       # softmax 雅可比-向量积, (N, M)
dV_my = P.T @ dO                 # (M, d)
dQ_my = scale * (dS @ K.T)       # (N, d)
dK_my = scale * (dS.T @ Q)       # (M, d)

print((dQ_my - dQ_ref).abs().max())  # ~0
print((dK_my - dK_ref).abs().max())  # ~0
print((dV_my - dV_ref).abs().max())  # ~0
```

**预期结果**：三条最大误差都在 `1e-5` 量级（fp32 舍入）。**待本地验证**：若误差偏大，检查是否漏了 `scale`、`D` 维度广播。

#### 4.1.5 小练习与答案

**练习 1**：为什么反向要重算 `P`，而不像普通 attention 实现那样把 `P` 存下来？

> **答**：存 `P∈ℝ^{N×M}` 需要 `O(N²)` 显存，正是 FlashAttention 前向设法避免的。反向宁可重算一次 `QKᵀ`（多用算力换显存），把显存压回 `O(N)`。由于前向已存 `LSE`，恢复 `P = exp(S·scale − LSE)` 不需要额外的行最大值。

**练习 2**：请说明 `D = Σ_i P_i dP_i` 为什么等于 `(O⊙dO).rowsum`。

> **答**：`dP_j = (dO Vᵀ)_j = Σ_k dO_k V_{jk}`，于是 `Σ_j P_j dP_j = Σ_j P_j Σ_k dO_k V_{jk} = Σ_k dO_k Σ_j P_j V_{jk} = Σ_k dO_k O_k = (O⊙dO).rowsum`。最后一个等号用到 `O = PV`，即 `O_k = Σ_j P_j V_{jk}`。

---

### 4.2 反向主循环：五段 MMA 与 dK/dV 寄存器累加

#### 4.2.1 概念说明

反向把前向的「Q 常驻、K/V 流水」**镜像翻转**成「K/V 常驻、Q/dO 流水」。原因来自工作划分：反向的 thread block 按 **n_block（K/V tile）** 划分（见 [flash_attn/cute/flash_bwd.py:424-433](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L424-L433)，`num_block` 基于 `mK.shape[1]`）。一个 K/V tile 要对所有相关的 Q tile 求和，所以 K/V 在主循环外加载一次、常驻，Q/dO 流水进出。

每个 (n_block, m_block) 内，要算 4.1 推导出的全部量，对应 **5 段 MMA**：`S=QKᵀ`、`dP=dO·Vᵀ`、`dV=Pᵀ·dO`、`dQ=dS·Kᵀ`、`dK=dSᵀ·Q`。其中 `dK/dV` 跨 m_block 累加进同一个寄存器累加器（最后写一次），`dQ` 则要原子加到全局缓冲（见 4.3）。

#### 4.2.2 核心流程

`compute_one_m_block` 处理一个 m_block，伪代码：

```
# 已加载：sQ[m_block], sdO[m_block], sK[n_block], sV[n_block]（sK/sV 常驻）
1. acc_S  = MMA(Q, K)           # S = QKᵀ           (thr_mma_sdp)
2. (可选) call_score_mod        # 用户 score_mod 改分数
3. apply_mask(acc_S)            # 因果/滑窗/块稀疏掩码
4. acc_S = exp2(S*scale_log2 - LSE)   # P = softmax(S)  ← 重计算
5. acc_dP = MMA(dO, V)          # dP = dO·Vᵀ        (thr_mma_sdp)
6. acc_dS = P * (dP - D)        # dS，逐元素         ← softmax 修正
7. acc_dV += MMA(Pᵀ, dO)        # dV 累加            (thr_mma_dkv)
8. acc_dQ  = MMA(dS, Kᵀ)        # dQ                 (thr_mma_dq) → 原子加到 dQaccum
9. acc_dK += MMA(dSᵀ, Q)        # dK 累加            (thr_mma_dkv)
```

三套不同的 MMA（`thr_mma_sdp` 算 `S/dP`，`thr_mma_dkv` 算 `dK/dV`，`thr_mma_dq` 算 `dQ`）在 `__init__` 时按布局参数构造，见 [flash_attn/cute/flash_bwd.py:310-330](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L310-L330)。`acc_dK/acc_dV` 在进入 m_block 循环前清零并跨循环累加：

```python
acc_dK = cute.make_rmem_tensor(acc_shape_dK, cutlass.Float32)
acc_dV = cute.make_rmem_tensor(acc_shape_dV, cutlass.Float32)
acc_dK.fill(0.0)
acc_dV.fill(0.0)
```
见 [flash_attn/cute/flash_bwd.py:656-659](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L656-L659)。

#### 4.2.3 源码精读

**第 1 段 MMA：`S = QKᵀ`**（重计算打分矩阵）：

```python
sm80_utils.gemm(
    mma_params.thr_mma_sdp, acc_S, mma_params.tSrQ, mma_params.tSrK,
    smem_copy_params.tSsQ[None, None, None, ...], smem_copy_params.tSsK,
    smem_copy_params.smem_thr_copy_QdO, smem_copy_params.smem_thr_copy_KV,
    swap_AB=self.SdP_swapAB,
)
```
见 [flash_attn/cute/flash_bwd.py:917-923](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L917-L923)。

**第 5 段 MMA：`dV = Pᵀ·dO`**（用刚算出的 `P`）：

```python
sm80_utils.gemm(
    mma_params.thr_mma_dkv, mma_params.acc_dV, tdVrP, mma_params.tdVrdO,
    smem_copy_params.tdVsPt,                                          # P 转置视图
    smem_copy_params.tdVsdOt[None, None, None, ...],                  # dO
    smem_copy_params.smem_thr_copy_PdSt, smem_copy_params.smem_thr_copy_QdOt,
    A_in_regs=self.Mma_dKV_is_RS, swap_AB=self.dKV_swapAB,
)
```
见 [flash_attn/cute/flash_bwd.py:1011-1018](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L1011-L1018)。结果累加进 `acc_dV`（跨 m_block 复用同一寄存器）。

**第 9 段 MMA：`dK = dSᵀ·Q`**：

```python
sm80_utils.gemm(
    mma_params.thr_mma_dkv, mma_params.acc_dK, tdKrdS, mma_params.tdKrQ,
    smem_copy_params.tdKsdSt,                                         # dS 转置视图
    smem_copy_params.tdKsQt[None, None, None, ...],                   # Q 转置视图
    ...
)
```
见 [flash_attn/cute/flash_bwd.py:1054-1062](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L1054-L1062)。结果累加进 `acc_dK`。

**dK/dV 的 epilogue 写回**（只在 m_block 循环结束后做一次）：把寄存器里的 `acc_dK/acc_dV` 转成 fp16、经 smem 中转、按 `seqlen_k` 谓词写回 gmem，见 [flash_attn/cute/flash_bwd.py:864-877](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L864-L877) 与 `epilogue` 的 [L1068-L1170](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L1068-L1170)。注意 epilogue 里还顺手做了 `dK *= softmax_scale`（4.1 公式里 `dK/dQ` 需要的那个缩放）：

```python
if cutlass.const_expr(self.qhead_per_kvhead == 1):
    acc_dK.store(acc_dK.load() * softmax_scale)
```
见 [flash_attn/cute/flash_bwd.py:868-869](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L868-L869)。

#### 4.2.4 代码实践

**目标**：阅读 kernel 后，把 5 段 MMA 各自对应的代码段标出来，理解数据流。

**步骤**：
1. 打开 [flash_attn/cute/flash_bwd.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py) 的 `compute_one_m_block`（L879 起）。
2. 用下表对照 5 段 `sm80_utils.gemm(...)` 调用，填写每段用的累加器、操作数 A/B、对应的数学式：

| 段 | 行号 | 累加器 | A | B | 数学式 |
|---|---|---|---|---|---|
| S | 917 | `acc_S` | `tSrQ` | `tSrK` | `QKᵀ` |
| dP | 961 | `acc_dP` | `tdPrdO` | `tdPrV` | `dO·Vᵀ` |
| dV | 1011 | `acc_dV` | `tdVrP` | `tdVrdO` | `Pᵀ·dO` |
| dQ | 1029 | `acc_dQ` | `tdQrdS` | `tdQrK` | `dS·Kᵀ` |
| dK | 1054 | `acc_dK` | `tdKrdS` | `tdKrQ` | `dSᵀ·Q` |

3. **观察**：`acc_dK/acc_dV` 是在 `kernel`（L656-659）里声明并清零、在 `compute_one_m_block` 外层（L854 的 m_block 循环）反复累加，最后只在 epilogue 写一次；而 `acc_S/acc_dP/acc_dQ` 都是每个 m_block 内部局部声明、用完即弃（见 L913、L957、L1027）。

**预期结果**：能清楚说出「dK/dV 寄存器累加、dQ 不行」的根因——见下一节。

#### 4.2.5 小练习与答案

**练习 1**：反向为什么把工作按 n_block（而非 m_block）切给 thread block？

> **答**：因为同一个 K/V tile（n_block）要被所有相关 Q tile 求和才能得到完整的 `dK/dV`。让一个 thread block「持有」一个固定的 n_block、内部循环 m_block，`dK/dV` 就能在寄存器里连续累加、最后只写一次 gmem，避免大量全局原子操作。

**练习 2**：`S = QKᵀ` 这段 MMA 在反向里算的是「前向的 S」。为什么不能直接复用前向算过的 `S`？

> **答**：前向根本没存 `S`（也没存 `P`），它们都被 tiling + online softmax 消化掉了，只留下 `LSE`。反向必须重算 `QKᵀ`，再用 `LSE` 恢复 `P = exp(S·scale − LSE)`。

---

### 4.3 dQ 的原子累加与 LSE/D 复用

#### 4.3.1 概念说明

`dQ` 的处境和 `dK/dV` 完全相反：一个 **Q tile（m_block）** 的梯度来自**所有相关的 K/V tile（n_block）**，而这些 n_block 分散在**不同 thread block** 上。也就是说，同一个 `dQ[m_block]` 会被多个 thread block 并发地累加。GPU 上跨 thread block 的累加只有一条路：**全局内存原子加**。

所以 FA4 维护一个 fp32 的全局缓冲 `dQaccum`，每个 thread block 算完自己那份 `dQ` 后，用 `atomic_add` 累加进去；最后由**后处理 kernel** 把 `dQaccum` 收敛（必要时除以缩放、转 dtype）得到最终的 `dQ`。本节顺带把 `LSE` 和 `D`（`dPsum`）的复用串起来——它们是反向能「轻装」重算 `P` 与做 softmax 修正的两个关键复用量。

#### 4.3.2 核心流程

```
# 在 compute_one_m_block 内，算完 dS 之后：
acc_dQ = MMA(dS, Kᵀ)                      # 本 n_block 对 dQ[m_block] 的贡献
for i in elements(acc_dQ):
    atomic_add_fp32(acc_dQ[i], &dQaccum[m_block][i])   # 原子累加进全局 fp32 缓冲

# （主循环外、所有 thread block 都跑完后）
postprocess: dQ = dQaccum  →  (除缩放 / 转 fp16)  →  最终 dQ
```

复用关系小结：

- **`LSE`（前向存）**：在反向里用来恢复 `P = exp2(S·scale_log2 − LSE)`，省掉行最大值的维护。
- **`D`（`dPsum`，预处理算）**：softmax 雅可比修正项 `dS = P·(dP − D)` 里的行标量，预处理一次性算成 `(O⊙dO).rowsum`，主循环只读取。

#### 4.3.3 源码精读

`dQ` 的 MMA 与原子累加封装在 `dQ_mma` 内层函数里：

```python
def dQ_mma(hook_fn):
    ...
    sm80_utils.gemm(
        mma_params.thr_mma_dq, acc_dQ, mma_params.tdQrdS, mma_params.tdQrK,
        smem_copy_params.tdQsdS, smem_copy_params.tdQsKt,
        ...
    )
    acc_dQ_atomic = gmem_copy_params.gmem_thr_copy_dQaccum.retile(acc_dQ)
    tdQgdQaccum_atomic = gmem_copy_params.tdQgdQaccum[None, None, m_block]
    ...
    for i in cutlass.range(cute.size(acc_dQ_atomic), unroll_full=True):
        utils.atomic_add_fp32(acc_dQ_atomic[i], utils.elem_pointer(tdQgdQaccum_atomic, i))
```
见 [flash_attn/cute/flash_bwd.py:1023-1042](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L1023-L1042)。`atomic_add_fp32` 是封装好的 fp32 全局原子加；`tdQgdQaccum` 指向 `mdQaccum[batch, head, m_block, head_dim]`（见 gmem 分区 [L646](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L646)、形状对齐 [L602](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L602)）。

`dQ_mma` 的调用点受流水线策略控制——为了让 Q/dO 的下一次加载尽早发射，`dQ` 的 MMA 在 `num_stages_Q>1` 时排在 `dV` 之后、`dK` 之前；`num_stages_Q==1` 时则排到 `dK` 之后（再用 `load_Q_next` 的 hook 流水），见 [flash_attn/cute/flash_bwd.py:1046-1047](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L1046-L1047) 与 [L1064-L1066](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L1064-L1066)。

`LSE` 与 `D` 的加载复用：两者都用前向/预处理存好的张量，按 MMA 布局重排后逐线程读进寄存器：

```python
# LSE 读进寄存器，供恢复 P 用
tLSErLSE = cute.make_fragment_like(smem_copy_params.tSsLSEMma[None, 0])
cute.autovec_copy(smem_copy_params.tSsLSEMma[None, ...], tLSErLSE)
```
见 [flash_attn/cute/flash_bwd.py:926-929](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L926-L929)；`D`（`dPsum`）同理见 [L969-L972](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L969-L972)。它们都来自 `load_Q_LSE` / `load_dO_dPsum`（[L1258-L1344](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L1258-L1344)）随 Q/dO 一起流式加载，和 Q/dO 共享同一个循环缓冲 stage。

**类型断言**也印证了这套缓冲的设计：`dQaccum`、`dPsum`、`LSE` 都必须是 Float32（`acc_dK/acc_dV` 是 fp32 累加器，最终转 fp16 写回），见 `_check_type` 的 [flash_attn/cute/flash_bwd.py:177-182](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L177-L182)。

#### 4.3.4 代码实践

**目标**：在 PyTorch 里复现「dQ 来自多个 K/V 块的累加」这一结构，直观体会为什么必须用原子加（或多块求和）。

**步骤**：

```python
# 示例代码（非项目源码）
import torch
N, M, d = 4, 8, 16          # M 用 2 个 KV 块，每块 4 行
scale = 1.0 / d**0.5
Q = torch.randn(N, d); K = torch.randn(M, d); V = torch.randn(M, d)
dO = torch.randn(N, d)
S = scale * (Q @ K.T); P = torch.softmax(S, -1); O = P @ V

D = (O * dO).sum(-1)                       # (N,)
dP = dO @ V.T                              # (N, M)
dS = P * (dP - D[:, None])                 # (N, M)

# 把 M 维（KV）切成两块，分别算 dQ 再「累加」——模拟两个 thread block
dQ_accum = torch.zeros_like(Q)
for k0 in (0, 4):                          # 两个 n_block
    dS_blk = dS[:, k0:k0+4]                # (N, 4)
    K_blk  = K[k0:k0+4]                    # (4, d)
    dQ_accum += scale * (dS_blk @ K_blk.T) # ← 这就是 atomic_add 的标量对应物

print(torch.allclose(dQ_accum, scale * (dS @ K.T)))  # True
```

**预期结果**：`True`。说明 `dQ` 是各 KV 块贡献的逐元素累加，这正是 kernel 里 `atomic_add_fp32` 在做的事。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `dK/dV` 可以用寄存器累加，而 `dQ` 必须用全局原子加？

> **答**：工作按 n_block 切给 thread block。`dK/dV` 的所有贡献都来自同一个 n_block 的不同 m_block，由同一个 thread block 内部循环产生，能安全地在寄存器里累加、最后写一次。`dQ` 的贡献则来自不同 n_block（不同 thread block），要并发写同一个 `dQ[m_block]`，跨 thread block 没有共享寄存器，只能用全局内存原子加（`dQaccum`）。

**练习 2**：前向存的 `LSE` 在反向里被复用了几次、各起什么作用？

> **答**：主要一次：恢复注意力权重 `P = exp2(S·scale_log2 − LSE)`（[flash_bwd.py:952-953](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L952-L953)），省掉对行最大值的维护。预处理里还顺手算了 `LSE·log₂e`（`mLSElog2`）等导出量供它处使用。

---

### 4.4 滑窗/局部掩码反向与 BlockInfo 块范围

#### 4.4.1 概念说明

本版本（`1f7ce2f..5835c73`）给 Sm80 反向 kernel 新增了对**滑窗/局部（local）掩码**的支持。此前 Sm80 反向只认 `causal`。

滑窗注意力的语义：Q 行 `i` 只看 K 列 `j` 满足 `i − window_size_left ≤ j ≤ i + window_size_right`（end-aligned 坐标系，见 u3-l1）。反向时，对**固定的一个 K/V tile（n_block）**，并非所有 Q tile 都与它有交集——很多 Q tile 完全落在滑窗外，整块都是被掩掉的。如果还老老实实遍历它们，会浪费大量 MMA 与 softmax 计算。

解决方案和前向/BlockInfo 一脉相承（u3-l2）：在主循环开始前，用 `BlockInfo.get_m_block_min_max(seqlen, n_block)` 算出「这个 n_block 真正需要遍历的 m_block 范围 `[m_block_min, m_block_max)`」，把因果和滑窗两种掩码**统一**在一套公式里。这是「块级跳过」；块内边界处还残留的非法元素，再交给 `AttentionMask.apply_mask(..., mask_local=True)` 做「元素级掩码」。

#### 4.4.2 核心流程

`get_m_block_min_max` 的推导（end-aligned，记 `Δ = seqlen_q − seqlen_k`）：

- **右边界（因果 或 带右窗）**：K tile 起点 `n_idx_min = n_block·tile_n`。能 attend 到它的最小 Q 行索引约 `m_idx_right = n_idx_min − Δ`（因果）或 `− window_size_right`（滑窗）。于是
  `m_block_min = max(0, m_idx_right // tile_m)`。
- **左边界（带左窗）**：K tile 终点 `n_idx_max = (n_block+1)·tile_n`。能 attend 到它的最大 Q 行索引约 `m_idx_left = n_idx_max − Δ + window_size_left`。于是
  `m_block_max = min(全部 m_block 数, ceil_div(m_idx_left, tile_m))`。

得到的 `[m_block_min, m_block_max)` 就是主循环的遍历范围。

> 约定：FA4 的因果是 **end-aligned**（`kv_idx ≤ q_idx + seqlen_k − seqlen_q`），所以 `Δ` 项让公式在 `seqlen_q ≠ seqlen_k`（如交叉注意力）下也成立。

#### 4.4.3 源码精读

**构造函数新增 `is_local`**（本版本新加）：

```python
is_local: bool = False,        # 构造参数
...
self.is_local = is_local
```
见 [flash_attn/cute/flash_bwd.py:45](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L45) 与 [L88](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L88)。

**用 BlockInfo 统一计算 m_block 范围**（本版本把原先只认 causal 的逻辑换成统一调用）：

```python
block_info = BlockInfo(
    self.m_block_size,
    self.n_block_size,
    self.is_causal,
    self.is_local,            # ← 新增：把 local 标志传进去
    False,                    # is_split_kv
    window_size_left,
    window_size_right,
)
m_block_min, m_block_max = block_info.get_m_block_min_max(seqlen, n_block)
```
见 [flash_attn/cute/flash_bwd.py:552-561](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L552-L561)。

**`get_m_block_min_max` 本体**（causal 与 local 共用一套公式）：

```python
@cute.jit
def get_m_block_min_max(self, seqlen_info, n_block):
    m_block_max = cute.ceil_div(seqlen_info.seqlen_q, self.tile_m)
    m_block_min = 0
    if const_expr(self.is_causal or (self.is_local and self.window_size_right is not None)):
        n_idx_min = n_block * self.tile_n
        m_idx = n_idx_min + seqlen_info.seqlen_q - seqlen_info.seqlen_k
        m_idx_right = m_idx if const_expr(self.is_causal) else m_idx - self.window_size_right
        m_block_min = max(m_block_min, m_idx_right // self.tile_m)
    if const_expr(self.is_local and self.window_size_left is not None):
        n_idx_max = (n_block + 1) * self.tile_n
        m_idx = n_idx_max + seqlen_info.seqlen_q - seqlen_info.seqlen_k
        m_idx_left = m_idx + self.window_size_left
        m_block_max = min(m_block_max, cute.ceil_div(m_idx_left, self.tile_m))
    return m_block_min, m_block_max
```
见 [flash_attn/cute/block_info.py:57-71](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_info.py#L57-L71)。注意 `is_causal/is_local` 是 `Constexpr`，`const_expr` 会在编译期裁掉不成立的分支，特化出无冗余的 kernel（这与 u3-l1/u3-l2 一致）。

**块内边界交给元素级掩码**：主循环里构造 `mask_fn`，把 `mask_local` 也传进去：

```python
mask_fn = partial(
    mask.apply_mask, n_block=n_block, thr_mma=thr_mma_sdp,
    batch_idx=batch_idx, head_idx=head_idx,
    mask_seqlen=True, mask_causal=self.is_causal, mask_local=self.is_local
)
```
见 [flash_attn/cute/flash_bwd.py:845-849](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L845-L849)。`apply_mask` 在 `mask_local=True` 时走 local 分支，对每个 Q 行算出 `[col_limit_left, col_limit_right)`，把区间外的列置 `-inf`：

```python
else:  # Local
    local_row_offset_right = (causal_row_offset + self.window_size_right) if ... else None
    local_row_offset_left  = (causal_row_offset - 1 - self.window_size_left) if ... else None
    ...
    col_limit_right = row_idx + local_row_offset_right
    col_limit_left  = row_idx + local_row_offset_left
    ...  # 区间外 acc_S_mn[r,c] = -inf
```
见 [flash_attn/cute/mask.py:331-376](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/mask.py#L331-L376)。`mask_causal` 与 `mask_local` 不能同时为真（断言见 [mask.py:192](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/mask.py#L192)）。

> 小结：**块级跳过**（`get_m_block_min_max`）负责「整块都在窗外」的 m_block；**元素级掩码**（`apply_mask`）负责「块内部分列越界」。两者协同，与 u3-l2 描述的前向 BlockInfo 机制完全对称。

#### 4.4.4 代码实践

**目标**：用一段 Python 复现 `get_m_block_min_max` 在滑窗下的输出，建立直觉，再与「稠密掩码」对照确认范围正确。

**步骤**：

```python
# 示例代码（非项目源码）
import math
def get_m_block_min_max(seqlen_q, seqlen_k, tile_m, tile_n, n_block,
                        is_causal, is_local, wl, wr):
    m_block_max = math.ceil(seqlen_q / tile_m)
    m_block_min = 0
    if is_causal or (is_local and wr is not None):
        n_idx_min = n_block * tile_n
        m_idx = n_idx_min + seqlen_q - seqlen_k
        m_idx_right = m_idx if is_causal else m_idx - wr
        m_block_min = max(m_block_min, m_idx_right // tile_m)
    if is_local and wl is not None:
        n_idx_max = (n_block + 1) * tile_n
        m_idx = n_idx_max + seqlen_q - seqlen_k
        m_idx_left = m_idx + wl
        m_block_max = min(m_block_max, math.ceil(m_idx_left / tile_m))
    return m_block_min, m_block_max

# 滑窗：window_left=window_right=2，tile=4，N=M=16 → 4 个块
sq = sk = 16; tile = 4; wl = wr = 2
for n in range(4):
    lo, hi = get_m_block_min_max(sq, sk, tile, tile, n, False, True, wl, wr)
    print(f"n_block={n}: m_block in [{lo}, {hi})")
```

**对照稠密掩码**：构造 `(16,16)` 的滑窗布尔掩码 `M[i,j] = (i-wl <= j <= i+wr)`，对每个 n_block（列块）找出哪些 m_block（行块）在该列块内有**任意** True，应与上面的 `[lo, hi)` 一致。

**预期结果**：n_block=0 时范围较小（只有靠前的 Q 行能看到最左边的 KV），n_block 越大范围越靠后，整体呈带状。**待本地验证**：把打印结果与稠密掩码推导对照，确认每个 n_block 的 `[lo,hi)` 正好覆盖所有「该列块内有 True」的行块。

#### 4.4.5 小练习与答案

**练习 1**：`is_local` 是 `Constexpr`，意味着什么？改 `window_size_left/right` 的具体数值会不会触发重编译？

> **答**：`is_local` 是编译期常量，`const_expr` 会据此裁剪 `get_m_block_min_max` 与 `apply_mask` 里的分支，特化出 local 专用 kernel。但 `window_size_left/right` 是运行期 `Int32`（[block_info.py:19-20](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_info.py#L19-L20)），改它们的数值**不**触发重编译；只有把 `is_local` 从 False 改成 True（或 `window_size` 从 None 变非 None 这种「是否存在」的变化）才会。

**练习 2**：为什么 local 掩码需要同时算 `m_block_min`（右边界）和 `m_block_max`（左边界），而纯 causal 只需要 `m_block_min`？

> **答**：纯因果下「上半部分」全被掩掉，Q 行 `i` 只看 `j ≤ i`，所以一个 K tile 只被「足够靠后」的 Q 行看到——只需用右边界抬升 `m_block_min`，左边界自然是 0。local 还额外有 `window_size_left`，使「太靠后」的 Q 行也看不到这个 K tile——所以还要用左边界压低 `m_block_max`，得到一个有限的带状范围。

---

### 4.5 空块范围早退保护

#### 4.5.1 概念说明

有了 4.4 的块范围计算，会出现一种新情形：**某些 n_block 的 `[m_block_min, m_block_max)` 是空区间**（`m_block_min ≥ m_block_max`）。这发生在因果/滑窗下 K tile 完全落在所有 Q 行的可视范围之外（比如滑窗很窄时，最右边或最左边的 KV 块可能没有任何 Q 行看得到它）。

如果对空区间还强行进入 prologue（加载 K/V、起流水、跑 m_block 循环 0 次、再走 epilogue），不仅白做一堆 gmem 加载，epilogue 里按 `seqlen_k` 谓词写回 `dK/dV` 也可能写出未初始化或错误的数据。因此本版本用一句 `if m_block_min < m_block_max:` 把整个 prologue + mainloop + epilogue 包起来，**空块直接早退**。

#### 4.5.2 核心流程

```
m_block_min, m_block_max = get_m_block_min_max(seqlen, n_block)
if m_block_min < m_block_max:        # ← 空块早退保护（本版本新加）
    # Prologue：加载 V、K，预热 Q/dO 流水
    # Mainloop：for m_block in [min, max): compute_one_m_block(...)
    # Epilogue：dK *= scale；写回 dK/dV
# 否则：本 n_block 对任何梯度都无贡献，跳过
```

注意：`dK/dV` 的累加器 `acc_dK/acc_dV` 在 `if` 之外初始化为零（[L656-659](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L656-L659) 在 `if` 之前的 kernel 体内），但 prologue/epilogue 都在 `if` 内。源码里还留了一句 `# TODO: return early if m_block_max == 0`（[L562](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L562)），说明更激进的「整个 thread block 直接返回」是后续优化方向；当前的 `m_block_min < m_block_max` 判断已能覆盖空区间的常见情况。

#### 4.5.3 源码精读

```python
m_block_min, m_block_max = block_info.get_m_block_min_max(seqlen, n_block)
# TODO: return early if m_block_max == 0
...
if m_block_min < m_block_max:
    # ///////////////////////////////////////////////////////////////////////////////
    # Prologue
    # ///////////////////////////////////////////////////////////////////////////////
    self.load_V(...)
    ...
    self.load_K(...)
    cute.arch.cp_async_commit_group()
    ...  # 预热 num_stages_Q 份 Q/dO
    # ///////////////////////////////////////////////////////////////////////////////
    # Mainloop
    # ///////////////////////////////////////////////////////////////////////////////
    mask = AttentionMask(...)
    for m_tile in cutlass.range(m_block_min, m_block_max, unroll=1):
        compute_one_m_block(...)
        ...
# ///////////////////////////////////////////////////////////////////////////////
# Epilogue
# ///////////////////////////////////////////////////////////////////////////////
if cutlass.const_expr(self.qhead_per_kvhead == 1):
    acc_dK.store(acc_dK.load() * softmax_scale)
self.epilogue(acc_dK, acc_dV, ...)
```
见 [flash_attn/cute/flash_bwd.py:801-877](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L801-L877)。可以清楚看到 prologue、mainloop、epilogue 三段都被 `if m_block_min < m_block_max:` 守护（epilogue 的写回在该 `if` 块内）。

> 与前向的对照：前向用 `n_block_min < n_block_max` 跳过空 split/空 n_block（u3-l2、u7-l2）。反向这里是它在 m 维上的对偶——同一个思想，不同维度。

#### 4.5.4 代码实践

**目标**：用窄滑窗构造一个会产生「空 m_block 范围」的 n_block，验证 `get_m_block_min_max` 会给出 `min ≥ max`。

**步骤**：

1. 用 4.4 的 `get_m_block_min_max`，取 `N=M=64, tile=16`，滑窗 `window_left=window_right=3`（窗口很窄，远小于 tile）。
2. 遍历所有 `n_block`（0..3），打印每个的 `[m_block_min, m_block_max)`。
3. 找出 `m_block_min >= m_block_max` 的 n_block——这些就是在 kernel 里会被 `if m_block_min < m_block_max:` 早退掉的块。

**预期结果**：窗口远小于 tile 时，**不会**出现 `min ≥ max`（因为只要 Q 行的窗口与 K tile 的 16 列有任何重叠，就至少有一个 m_block 落在范围内）。要让 `min ≥ max` 出现，可改用 `window_left=0, window_right=0`（等价 causal+无前瞻）+ 不整除的尺寸，或增大 tile 使一个 K tile 整体落在窗口之外。**待本地验证**：构造出至少一个 `min ≥ max` 的 n_block，并说明它在 kernel 里不会产生任何 `dK/dV/dQ` 写入（早退）。

> 思考题（不写代码）：如果删掉这个 `if` 保护，对空 n_block 会发生什么？——prologue 仍会加载 K/V、mainloop 跑 0 次、epilogue 把「全是 0 的 acc_dK」写回 gmem。若该 n_block 完全合法（只是被掩掉），写回 0 是正确的；但代价是无谓的 gmem 流量与一个本可省掉的 thread block 启动开销。早退是**性能保护**，同时也避免了边界情形下的未定义写入。

#### 4.5.5 小练习与答案

**练习 1**：`m_block_min < m_block_max` 不成立时，kernel 会跳过哪些阶段？

> **答**：跳过 prologue（不加载该 n_block 的 V/K、不预热 Q/dO 流水）、mainloop（不跑任何 m_block、不算任何 MMA）、以及 epilogue 里的 `dK *= scale` 与 `dK/dV` 写回。整个 thread block 对该 n_block 不产生任何梯度贡献。

**练习 2**：注释 `# TODO: return early if m_block_max == 0` 暗示了一个比当前更激进的优化。它和当前的 `if m_block_min < m_block_max:` 有什么区别？

> **答**：当前判断只在「整块工作（prologue+mainloop+epilogue）」层面跳过，但 thread block 仍然会执行到 `if` 判断、分配 smem、走一些 setup。`return early if m_block_max == 0` 指的是让 thread block 在更早的位置（如读完 seqlen、算完范围后立刻）直接 `return`，连 smem 分配和后续 setup 都省掉，进一步减少空块的启动开销。

---

## 5. 综合实践

把本讲的五块知识串起来，完成一个「阅读 + 验证」小任务：

**任务**：以 `window_size_left = window_size_right = W`（如 32）的滑窗注意力为例，从 kernel 入口到一次 `dQ` 原子加，画出完整的反向数据流，并用 PyTorch 验证数值。

**步骤**：

1. **读链路**：从 [interface.py:1789-1813](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L1789-L1813) 看 Sm80 反向 kernel 如何被实例化（注意 `local` 参数传给 `is_local`）。确认 `_resolve_causal_local_window`（[interface.py:275-295](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L275-L295)）会把 `window_size_left/right` 都设置的场景解析成 `local=True, causal=False`。

2. **读范围**：在 kernel 入口 [flash_bwd.py:552-561](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L552-L561)，确认 `BlockInfo(..., self.is_local, ..., window_size_left, window_size_right)` 与 `get_m_block_min_max`。

3. **读主循环**：在 [flash_bwd.py:854-862](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L854-L862) 的 m_block 循环里，标注 5 段 MMA（S/dP/dV/dQ/dK）各自位置，指出 `dQ` 走 [atomic_add_fp32](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L1040-L1041)、`dK/dV` 走寄存器累加 + epilogue 写回。

4. **数值验证**：用 FA4 实跑一次滑窗反向，与你的 PyTorch 参考实现（稠密 `attn_mask`）对比 `dQ/dK/dV`：

```python
# 示例代码（非项目源码）—— 假设已安装 flash_attn.cute 并有 GPU
import torch
from flash_attn.cute import flash_attn_func
b, sq, sk, h, d = 1, 256, 256, 2, 64
W = 32
q = torch.randn(b, sq, h, d, dtype=torch.float16, device='cuda', requires_grad=True)
k = torch.randn(b, sk, h, d, dtype=torch.float16, device='cuda', requires_grad=True)
v = torch.randn(b, sk, h, d, dtype=torch.float16, device='cuda', requires_grad=True)
out, _ = flash_attn_func(q, k, v, window_size=(W, W))   # 滑窗
dO = torch.randn_like(out)
out.backward(dO)
# 参考：稠密掩码 attention + autograd（略，按 u4 公式实现），对比 q.grad/k.grad/v.grad
```

5. **观察**：
   - 输出在容差内一致（fp16 舍入级误差）。
   - 用窄 `W`（如 8）时，部分 n_block 的 m_block 范围会被 BlockInfo 显著裁剪，理论上反向比「无掩码稠密 + 事后 mask」更快——若有条件可用 `torch.cuda.Event` 计时对比。

**预期结果**：三条梯度最大相对误差在 `1e-2 ~ 1e-3` 量级（fp16）。若在 SM80/SM120 上运行，确认走的是 `FlashAttentionBackwardSm80`（可用 `FLASH_ATTENTION_ARCH=sm_80` 强制）。**待本地验证**（本环境无 GPU）。

---

## 6. 本讲小结

- **反向五条公式**：`dV = PᵀdO`、`dP = dO·Vᵀ`、`dS = P·(dP − D)`、`dQ = scale·dS·Kᵀ`、`dK = scale·dSᵀ·Q`，其中 `D = (O⊙dO).rowsum`。
- **重计算 `S/P`**：反向必须再算一次 `QKᵀ` 并用前向存的 `LSE` 恢复 `P = exp2(S·scale_log2 − LSE)`，因为存整个 `P` 会破坏 `O(N)` 显存。
- **5 段 MMA**：`S→dP→dS→dV→dQ→dK`，工作按 n_block 切，K/V 常驻、Q/dO 流水（前向的镜像）。
- **累加策略分野**：`dK/dV` 跨 m_block 在寄存器里累加、epilogue 写一次；`dQ` 因来自不同 thread block，必须用全局 `atomic_add_fp32` 累加进 `dQaccum`，再由后处理收敛。
- **新增加的滑窗/局部掩码反向**：`is_local` + `window_size_left/right` 经 `BlockInfo.get_m_block_min_max(seqlen, n_block)` 统一算出 m_block 范围（块级跳过），块内边界交 `AttentionMask.apply_mask(mask_local=True)`（元素级掩码）。
- **空块早退**：`if m_block_min < m_block_max:` 把 prologue/mainloop/epilogue 整段包起，对完全落在掩码外的 n_block 直接跳过，省掉无谓的 gmem 流量。

## 7. 下一步学习建议

- **u9-l2 反向预处理与后处理**：本讲多次提到的 `D/dPsum`（预处理）、`dQaccum → dQ`（后处理）就是那里的主角，建议接着读 `flash_bwd_preprocess.py` / `flash_bwd_postprocess.py`，把三阶段数据流（LSE、D、dO、dQ 在 pre/main/post 间的流动）补全。
- **u9-l3 Hopper/Blackwell 反向**：对比 Sm80 的「warp 级 MMA + cp.async」与 Sm90 的「warp-group MMA + TMA」、Sm100 的「UMMA + 2CTA dQ reduce」，理解架构升级如何改变 `dQ` 的累加策略（原子加 → 2CTA 归约 → tmem 归约）。
- **延伸阅读**：u3-l2（BlockInfo）、u4-l1（在线 softmax/LSE）、u3-l1（AttentionMask 的 R2P 位图）是本讲的直接前置，若对块范围推导或掩码机制有疑问可回看；`hopper/mainloop_bwd_sm80.hpp`（文件头注释指向的 C++ 原型，[flash_bwd.py:2](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L2)）是这套实现的 C++ 出处，可作交叉参考。
