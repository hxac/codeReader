# 16x16 求逆：8x8 fp32 前代换 + bf16 HMMA 块合并

## 1. 本讲目标

学完本讲，你应该能够：

1. 推导单位下三角矩阵的分块求逆公式，并把公式中的每个块对应到 kernel 代码里的 `P / M / dc / o` 四个变量。
2. 逐行读懂 `inv_fwd_subst_fused_1warp` 的 CUDA 实现：lane 到行的映射、`__shfl_sync` 广播主元行的前代换、以及「LDSM 载入 → MOVM_T 寄存器转置 → 两次 HMMA → STSM 写回」的寄存器内数据通路。
3. 说清楚为什么旧的 fp16 Neumann 级数在近共线 key（\(|L| \to 1\)）时会灾难性失效，而前代换不会；并能用脚本复现这条误差曲线。
4. 理解「seed 全程 fp32、只在 HMMA 输入处量化 bf16」这套精度设计，以及 `tests/torch_ref.py` 如何逐 FMA 顺序复刻它实现 bit-exact 对拍。

## 2. 前置知识

本讲是手册中最「数学」的一篇，但所需的全部基础都可以用几句话讲清：

- **单位下三角矩阵（unit lower triangular）**：对角线全为 1、上三角全为 0 的方阵。KDA 中 \(X = I + L\)，其中 \(L\) 严格下三角（对角线也是 0），所以 \(X\) 是单位下三角。
- **前代换（forward substitution）**：解下三角线性方程组最朴素的方法——从第一行开始，每行只有一格未知数，逐行向下代入。它同时也是求三角矩阵逆的标准办法：对 \(X Y = I\) 逐列求解，或等价地做逐主元的行消去。本讲 kernel 用的是后者（Gauss–Jordan 风格的原位行消去）。
- **Neumann 级数**：矩阵版的几何级数。对满足 \(L^{16} = 0\) 的 16 阶严格下三角 \(L\)（严格下三角矩阵幂零，\(L^{16}\) 必为 0），有
  \[ (I+L)^{-1} = I - L + L^2 - \cdots + L^{15} = (I-L)(I+L^2)(I+L^4)(I+L^8). \]
  这是旧版 kernel 的求逆方式。
- **灾难性抵消（catastrophic cancellation）**：两个大数相减得到小数时，大数低位携带的舍入误差会吞掉小数的有效数字。后面会看到 \(|L| \to 1\) 时 Neumann 的中间幂次达到 \(\sim 3\times 10^3\)，而最终结果只有 \(O(1)\)。
- **warp / lane / shuffle**：CUDA 中 32 个线程组成一个 warp，线程在 warp 内的编号叫 lane。`__shfl_sync(mask, var, src_lane)` 让每个 lane 从指定 lane 的寄存器里取值，是 warp 内高速数据交换指令；`mask` 内的所有 lane 必须都执行这条指令（收敛要求）。
- **HMMA / LDSM / STSM / MOVM_T**：SM80 的 `m16n8k16` 矩阵乘指令（本讲用 bf16 输入、fp32 累加的 `SM80_16x8x16_F32BF16BF16F32_TN`）；LDSM 从共享内存把操作数片段直读进寄存器；STSM 把寄存器结果写回共享内存；MOVM_T（movmatrix）在寄存器文件内做 8x8 转置重排。它们在 u2-l4、u3-l4 中已系统介绍，本讲只用结论。

回顾 u2-l7 的结论：Kernel 1 在 smem 里算出了 seed \(L = \mathrm{tril}(k_{decayed} k_{inv}^{\top}, -1) \cdot \beta\)（fp32），本讲要解决的问题是：**如何把这个 16x16 的 \(I+L\) 高精度、低成本地求逆成 bf16 的 INV，写给 workspace**。INV 在 Kernel 2 中被用来解 \(U = INV \cdot u\)（u2-l1 的 chunk 算法），因此它是整条链路里唯一「除法」的来源。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `csrc/smxx/utils.cuh` | 公共工具头 | `inv_fwd_subst_fused_1warp`（求逆本体）、`mma_m16n16_bf16bf16fp32_1warp`（fp32 存储的 L MMA） |
| `csrc/smxx/fwd_kernel1.cuh` | Kernel 1（prepare） | seed L 的构造、`M_bf16` 复用死缓冲、求逆调用点、INV 的 TMA store |
| `tests/torch_ref.py` | bit-exact torch 参考 | `inv_fwd_subst_16`：逐 FMA 复刻 kernel 求逆 |
| `docs/20260420-flashkda-v1-deep-dive.md` | 官方深度解读 | 第 3 节精度设计中的「前代换精确求逆」条目 |

建议按「先读 `fwd_kernel1.cuh` 的调用点 → 再精读 `utils.cuh` 的实现 → 最后对读 `torch_ref.py`」的顺序进行。

## 4. 核心概念与源码讲解

### 4.1 分块前代换算法

#### 4.1.1 概念说明

直接对 16x16 矩阵做前代换是纯串行的：第 \(i\) 行依赖第 \(i-1\) 行……行，一个 warp 里让 32 个线程各等一行，并行度极差。FlashKDA 的做法是**把 16x16 按 8x8 分块**：

\[
X = I + L = \begin{bmatrix} A & 0 \\ C & B \end{bmatrix}, \qquad
X^{-1} = \begin{bmatrix} A^{-1} & 0 \\ -\,B^{-1} C\, A^{-1} & B^{-1} \end{bmatrix},
\]

其中 \(A\)、\(B\) 各是 8x8 单位下三角（\(X\) 对角线上的两个块），\(C\) 是左下 8x8 块。这个公式的好处是把问题拆成了三份性质不同的工作：

1. **两个 8x8 对角块的逆** \(A^{-1}, B^{-1}\)：规模减半后，串行前代换只有 7 步、每步至多 6 次乘加，且 8 行恰好塞进 8 个 lane——每 lane 持一行，warp shuffle 广播主元行，串行深度骤降；
2. **非对角块的合并** \(-B^{-1} C A^{-1}\)：这是两次 8x8 乘法的组合，可以整体提升为 16x16 的矩阵乘，直接复用现成的 HMMA 通路。

代码把公式里的量重新组合成两个 16x16 矩阵，让合并只用 **两次** GEMM：

\[
P = \begin{bmatrix} A^{-1} & 0 \\ 0 & B^{-1} \end{bmatrix}, \qquad
M = \begin{bmatrix} 0 & 0 \\ C & 0 \end{bmatrix},
\]
\[
dc = P M = \begin{bmatrix} 0 & 0 \\ B^{-1}C & 0 \end{bmatrix}, \qquad
o = (-dc)\, P = \begin{bmatrix} 0 & 0 \\ -\,B^{-1} C\, A^{-1} & 0 \end{bmatrix},
\]
\[
INV = P + o.
\]

注意 \(P\) 的非零块（两个对角块）与 \(o\) 的非零块（左下块）**互不相交**，所以最后一步只是逐元素加零，不产生任何抵消。源码注释把这套记号完整写在了函数头上。

#### 4.1.2 核心流程

整个求逆（单 warp，32 线程）的伪代码：

```text
输入: L_fp32 (16x16 严格下三角, smem), 输出: INV_bf16 (16x16 bf16, smem)

# ① 8x8 fp32 前代换（每 lane 一行）
for lane i in 0..7 (块 A: lanes 0-7/16-23; 块 B: lanes 8-15/24-31):
    inv[0..7] = [I + L_block 的第 i 行]
for s = 0..6:                       # 主元步
    row_scale = -inv[s]             # 我这一行在第 s 列的当前值（尚未被更新过）
    for p = 0..s-1:                 # 主元行 s 已定终身，从它的 owner lane 广播 inv[p]
        pivot = shuffle(inv[p], src = 组内 lane s)
        if i > s: inv[p] = fma(row_scale, pivot, inv[p])
    if i > s: inv[s] = row_scale
# 此后 inv[] 即所在块逆矩阵的第 i 行（fp32，从未量化）

# ② 暂存（分 4 个 8-lane 组）
group 0: P 行 0-7   写入 INV_bf16  (bf16(inv[]), 右半置 0)
group 1: P 行 8-15  写入 INV_bf16  (左半置 0, bf16(inv[]))
group 2: M 行 0-7   写入 M_bf16    (全 0)
group 3: M 行 8-15  写入 M_bf16    (左半 = bf16(L), 右半置 0)
__syncwarp()

# ③ 寄存器内合并（两次 16x16 HMMA，bf16 操作数 + fp32 累加）
P_a, M_a = LDSM 载入 P、M 的 A-操作数片段
M_b = MOVM_T(M_a)                    # 寄存器内转置成 B-操作数排布
dc_c = P_a @ M_b                     # fp32 累加器
negdc_a = bf16(-dc_c)                # fp32 取负后 RNE 量化打包
P_b = MOVM_T(P_a)
o_c  = negdc_a @ P_b                 # fp32 累加器
INV_c = P_a + bf16(o_c)              # 逐元素 bf16 加（非零块不相交）

# ④ 写回
STSM: INV_c → INV_bf16 (smem)
```

关于第 ① 步的正确性，可以先用 3x3 手算建立直觉（下一节的实践会让你再机算一遍）。设

\[
T = \begin{bmatrix} 1 & & \\ a & 1 & \\ b & c & 1 \end{bmatrix}, \qquad
T^{-1} = \begin{bmatrix} 1 & & \\ -a & 1 & \\ ca - b & -c & 1 \end{bmatrix}.
\]

算法在 lane 2 上的执行轨迹：初始化 `inv = [b, c, 1]`；主元步 \(s=0\)：`row_scale = -b`，`inv[0] ← -b`；主元步 \(s=1\)：`row_scale = -c`，从 lane 1 广播 `pivot = inv[0] = -a`，`inv[0] = fma(-c, -a, -b) = ca - b`，`inv[1] ← -c`。与解析式完全一致。关键不变量是：**第 \(s\) 步开始时，lane \(s\) 的行已是最终值，而其余行第 \(s\) 列仍是原值**——前者因为 lane \(s\) 只在步 \(< s\) 被更新，后者因为既往步骤只触碰列 \(\le s-1\)。

#### 4.1.3 源码精读

函数头部的注释就是 4.1.1 的公式与精度设计总纲，值得先读一遍：

- [csrc/smxx/utils.cuh:194-219](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L194-L219)：`inv_fwd_subst_fused_1warp` 的文档注释与签名。注释明确写出分块公式、`P/M/dc/o/INV` 五步、以及与旧 Neumann 的对比（「Neumann 的幂达到 ~1e3 并在 fp16 中灾难性抵消」）。

seed \(L\) 的构造在 Kernel 1 里（求逆的直接输入）：

- [csrc/smxx/fwd_kernel1.cuh:478-486](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L478-L486)：`L_fp32` 用 `LF32Layout`（朴素行主 16x16）绑定到 smem；warp 0 调 `mma_m16n16_bf16bf16fp32_1warp(k_decayed, k_inv, L_fp32, ...)` 算 \(L = k_{decayed} k_{inv}^{\top}\)，**把裸 fp32 累加器直接落 smem**（这正是本 commit 把旧的 fp16 存储版改名为 `...fp32_1warp` 的改动，见 [csrc/smxx/utils.cuh:167-192](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L167-L192)）。
- [csrc/smxx/fwd_kernel1.cuh:493-507](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L493-L507)：同一线程同元素地做 `tril(diagonal=-1)` 清零 + 乘 `sigmoid(beta)`，全程 fp32。至此 seed 完成且**从未离开 fp32**。

前代换本体的两段循环：

- [csrc/smxx/utils.cuh:229-242](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L229-L242)：lane 映射与初始化。`i = tid & 7` 是组内行号，`row0 = tid & 8` 选择块 A（行 0-7）或块 B（行 8-15）；`inv[p]` 初始化为 `L_fp32(row0+i, row0+p)`（p < i）、对角 1、右上 0——即 \(I + L_{block}\) 的第 \(i\) 行。
- [csrc/smxx/utils.cuh:243-252](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L243-L252)：7 个主元步。`pivot = __shfl_sync(0xFFFFFFFFu, inv[p], (tid & ~7) | s)` 从**本 8-lane 组的第 s 个 lane** 广播主元行元素；更新用 `fmaf`（单次舍入）。注意 shuffle 对所有 lane 无条件执行（收敛要求），只有 `i > s` 的 lane 使用结果。

torch 参考侧的同构实现（对拍依据）：

- [tests/torch_ref.py:95-107](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L95-L107)：`inv8` 把两个对角块拼成 `[2N, 8, 8]` 批量张量，同样 7 步主元消去；`fp32_fma`（见 [tests/torch_ref.py:55-59](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L55-L59)，用 fp64 中转模拟单次舍入的 FMA）保证与 kernel 的 `fmaf` 逐比特一致，`inv8[:, s+1:, p]` 的切片顺序对应 kernel 中「行 i > s 才更新」。

#### 4.1.4 代码实践

**实践目标**：用 fp64 数值验证分块公式 \(P + (-PM)P = X^{-1}\) 与前代换行消去算法的正确性（纯 CPU，无需 GPU）。

**操作步骤**：

1. 新建 `inv_block_check.py`（示例代码，不属于仓库）：

```python
import torch

torch.manual_seed(0)
n = 16
# 随机严格下三角 seed
L = torch.tril(torch.randn(n, n, dtype=torch.float64), -1)
X = torch.eye(n, dtype=torch.float64) + L

# --- (1) 分块公式：P/M/dc/o 组合 ---
A = X[:8, :8]; B = X[8:, 8:]; C = X[8:, :8]
P = torch.zeros(n, n, dtype=torch.float64)
P[:8, :8] = torch.linalg.inv(A); P[8:, 8:] = torch.linalg.inv(B)
M = torch.zeros(n, n, dtype=torch.float64)
M[8:, :8] = C
dc = P @ M
o = (-dc) @ P
INV_block = P + o

# --- (2) 逐主元行消去（模拟 kernel 的单 8x8 块）---
inv8 = X[:8, :8].clone()          # 单位下三角，等价于 seed+I
for s in range(7):
    row_scale = -inv8[s, :].clone()
    for i in range(s + 1, 8):
        inv8[i, :] += row_scale * inv8[s, :]   # 行向量形式的原位更新
INV_fwd = torch.zeros(n, n, dtype=torch.float64)
INV_fwd[:8, :8] = inv8

ref = torch.linalg.inv(X)
print("block  rel-err:", (INV_block - ref).norm() / ref.norm())
print("fwd8   rel-err:", (INV_fwd[:8, :8] - ref[:8, :8]).norm() / ref[:8, :8].norm())
print("X @ INV_block == I:", torch.allclose(X @ INV_block, torch.eye(n, dtype=torch.float64), atol=1e-10))
```

2. 运行 `python inv_block_check.py`。

**需要观察的现象**：两条相对误差都在 1e-14 量级（fp64 舍入水平）；`X @ INV_block` 恢复单位阵。

**预期结果**：分块组合与直接求逆在 fp64 下逐元素一致，公式实现无误。（待本地验证）

#### 4.1.5 小练习与答案

**练习 1**：为什么合并阶段只需要两次 GEMM，而不是「先算 \(B^{-1}C\)、再乘 \(A^{-1}\)」的两次 8x8 乘法加一次取负？

**答案**：两次 GEMM 指的是 16x16 尺寸的 `dc = P @ M` 与 `o = (-dc) @ P`。把 \(A^{-1}, B^{-1}\) 拼进对角块矩阵 \(P\)、把 \(C\) 拼进 \(M\) 后，`P @ M` 一次就把 \(B^{-1}C\)（以及值为零的其余块）算完，`(-dc) @ P` 一次就完成左乘 \(A^{-1}\)（右乘 \(B^{-1}\) 的部分自然为零）。这样能整体复用现成的 16x16 HMMA 通路（LDSM/MOVM_T/STSM），无须为 8x8 另写一套分块乘法。

**练习 2**：`INV = P + bf16(o)` 里说「非零块不相交，所以加法精确」。请解释这句话，并指出它依赖分块的哪个性质。

**答案**：\(P\) 只在两个 8x8 对角块非零，\(o\) 只在左下 8x8 块非零。逐元素相加时，一方总是精确的 bf16 零，`x + 0 = x` 无舍入，因此不会引入新的量化误差、也不会发生抵消。它依赖的是块对角 \(\times\) 块下三角的结构：\(X^{-1}\) 的右上块为零使得 \(o\) 恰好只填 \(P\) 的空块。

**练习 3**：前代换为什么不需要第 8 个主元步（循环只到 s=6）？

**答案**：8x8 块有 8 行，但第 8 行（i=7）下面没有行需要消去，它自己的值在第 7 步（s=6）就被最终确定；而单位对角保证第 8 行不需要归一化。推广到 16x16 整体：直接前代换需要 15 步，分块后每个 8x8 块只要 7 步，串行深度几乎减半。

### 4.2 warp shuffle 求逆实现

#### 4.2.1 概念说明

算法落到一个 warp 上，核心是三个映射问题：

1. **lane ↔ 行**：32 个 lane、两个 8x8 块各 8 行，怎么分工？答案是把 warp 切成 4 个 8-lane 组：组 0（lane 0-7）和组 2（lane 16-23）都算块 \(A\)，组 1（lane 8-15）和组 3（lane 24-31）都算块 \(B\)。组 2/3 的结果是**冗余拷贝**——它们算完就丢弃，换来的是所有 `__shfl_sync` 都能用全掩码 `0xFFFFFFFF`，无须按组维护掩码、也不存在部分 lane 缺席导致的 shuffle 不收敛。
2. **寄存器 ↔ smem**：前代换的输入 \(L\) 与输出 \(P/M\) 在 smem，中间量全在寄存器。两个 16x16 的 HMMA 操作数用 LDSM 直读进寄存器片段，B 操作数用 MOVM_T 在寄存器文件内转置（不经 smem），结果用 STSM 直写回。
3. **生命周期 ↔ 缓冲**：\(P\) 直接暂存进 `INV_bf16`（反正它先被读进寄存器、之后才被结果覆盖），\(M\) 暂存进**已经消费完的 `k_inv` smem 缓冲**——这是 u2-l8 讲过的 Phase 生命周期复用在 warp 级的延续，省下 512 字节。

精度上要记住一条主线：**fp32 只在前代换（对角块）里「常住」，一旦进入合并路径就在 HMMA 输入边界被量化成 bf16**（\(P\) 的暂存、\(M\) 中 \(C\) 的暂存、\(-dc\) 的重打包），两次 HMMA 内部用 fp32 累加，出口量化回 bf16。

#### 4.2.2 核心流程

从 Kernel 1 调用点看数据流：

```text
K1 主流程 (fwd_kernel1.cuh)
  ├─ L = mma(k_decayed, k_inv^T)        # fp32 累加器直接落 smem（不再转 fp16）
  ├─ L = tril(L,-1) * sigmoid(beta)     # fp32，同线程同元素
  ├─ __syncthreads()
  ├─ INV   ← shared_storage.INV          # 16x16 bf16（LMLayout swizzle）
  ├─ M_bf16 ← shared_storage.k_inv       # 复用"已死"的 k_inv 缓冲
  └─ inv_fwd_subst_fused_1warp(L_fp32, M_bf16, INV, compute_tid)   # 256 线程都调
        └─ 内部: tid >= 32 直接 return   # 只有 warp 0 干活
  ├─ fence_view_async_shared + __syncthreads
  └─ 线程 0: TMA store INV → workspace   # 位一致契约，见 u2-l8
```

warp 内部再细一层（对照 4.1.2 的伪代码）：

```text
lanes 0-31, 全掩码 shuffle
  ① 前代换: inv[8] fp32 寄存器（组 0/2 → A^-1, 组 1/3 → B^-1）
  ② 暂存:   组 0/1 写 P 到 INV_bf16；组 2/3 写 M 到 M_bf16（分支发散）
  __syncwarp()                          # 发散分支后重汇聚 + smem 可见性
  ③ LDSM 载入 P、M 的 A 片段（4 个 u32 / 线程）
     MOVM_T(M_a → M_b), MOVM_T(P_a → P_b)
     dc_c = HMMA(P_a, M_b)              # fp32[8]
     negdc_a = pack_bf16x2(-dc_c)       # fp32 取负 + RNE 打包
     o_c = HMMA(negdc_a, P_b)           # fp32[8]
     INV_c = P_a + bf16(o_c)            # __hadd2 逐元素
  ④ STSM(INV_c → INV_bf16)
```

#### 4.2.3 源码精读

**调用点与缓冲复用**：

- [csrc/smxx/fwd_kernel1.cuh:489-491](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L489-L491)：`INV` 绑定到专用 smem；`M_bf16` 显式注释为「staged into the (dead) k_inv smem buffer」——`k_inv` 已被 L/Mqk 两次 MMA 消费完毕（且过了 `__syncthreads()`），缓冲空闲。
- [csrc/smxx/fwd_kernel1.cuh:510-514](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L510-L514)：全部 256 线程无条件调用求逆；返回后 `fence_view_async_shared` + `__syncthreads()` 打通「寄存器→smem→TMA 读」的代理可见性，随后线程 0 发起 6 次 TMA store（INV 是其中之一，见 [csrc/smxx/fwd_kernel1.cuh:561-571](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L561-L571)）。

**函数内部**：

- [csrc/smxx/utils.cuh:222-227](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L222-L227)：构造 `SM80_16x8x16_F32BF16BF16F32_TN` + `Tile<_16,_16,_16>` 的单 warp TiledMma；`if (tid >= int(size(mma))) return;`——`size(mma)` 是参与线程数 32，所以 K1 传进来的 256 个 tid 里只有 warp 0 生效。这正是 u2-l7 里 L/Mqk 两个「单 warp MMA」的同款写法。
- [csrc/smxx/utils.cuh:254-277](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L254-L277)：按 `group = tid >> 3` 四路分支暂存 \(P\) 与 \(M\)。组 0 写 `INV_bf16(i, j)`：左半 `BF16(inv[j])`、右半 `BF16::bitcast(0)`；组 3 写 `M_bf16(8+i, j)`：左半 `BF16(L_fp32(8+i, j))`——**seed \(C\) 块在此第一次（也是唯一一次）被量化成 bf16**。注释指出 `L_fp32` 只读所以暂存前不需要同步，但四路分支是发散的，末尾的 `__syncwarp()` 负责重汇聚并保证组 2/3 能看到组 0/1 写入的 \(P\)。
- [csrc/smxx/utils.cuh:279-299](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L279-L299)：用 `make_tiled_copy_A(Copy_Atom<SM75_U32x4_LDSM_N, BF16>)` 把 `INV_bf16`（此刻内容是 \(P\)）与 `M_bf16` 各载入成每线程 4 个 u32 的 A 操作数片段 `tCrP / tCrM`。读进寄存器后 smem 里的 \(P\) 就「过期」了，最终结果稍后直接覆盖 `INV_bf16`——同一块 smem 先后扮演「\(P\) 暂存区」与「INV 输出区」两个角色（同 warp 内指令按序执行，LDSM 先于 STSM 完成，无竞态）。
- [csrc/smxx/utils.cuh:301-328](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L301-L328)：寄存器工具箱。`transpose_u32x4`（304-309 行）用 4 条 `SM75_U32x1_MOVM_T::copy` 做 8x8 寄存器转置，把 A 片段排布变成 B 片段排布；`mma_16x16`（311-315 行）把 16x16 的乘法拆成沿 N 的两个 `m16n8k16` atom（注意 B 操作数每 atom 只要 2 个 u32）；`pack_bf16x2`（322-328 行）用 `__floats2bfloat162_rn` 把两个 fp32 按 RNE 打包成一个 bf16x2，注释强调「第一个元素放低半字，与 C 累加器的 A 片段配对顺序一致」——这个配对方向错了结果就整体错位，是 bit-exact 的隐藏细节。
- [csrc/smxx/utils.cuh:330-341](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L330-L341)：两次 HMMA。`dc = P @ M`（330-333 行）先把 `M_a` 转置成 `M_b`；`o = bf16(-dc) @ P`（335-341 行）先对 fp32 累加器取负再量化打包成 `negdc_a`，并把 `P_a` 转置成 `P_b`。注释点明取负是「fp32 negate with fast-math ftz」——取负本身逐位精确，ftz 只影响 denormal 边界情形。
- [csrc/smxx/utils.cuh:343-355](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L343-L355)：`INV = P + bf16(o)`：fp32 结果 `o_c` 量化打包成 `o_b`，再用 `__hadd2` 与 `P_a` 逐元素 bf16 相加。
- [csrc/smxx/utils.cuh:357-368](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L357-L368)：把 `INV_c`（4 个 u32）填进 C 片段寄存器，经 `SM90_U32x4_STSM_N` 直写回 `INV_bf16`。

**torch 参考的合并段**（与上面 330-355 行逐条对应）：

- [tests/torch_ref.py:109-121](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L109-L121)：`P`、`M` 组装（`inv8` 转 bf16、`seed[..., 8:, :8]` 转 bf16），`dc = torch.mm(P[b], M[b], out_dtype=torch.float32)`（bf16 输入 fp32 累加）、`o = torch.mm((-dc).to(torch.bfloat16), P[b], out_dtype=torch.float32)`、`INV[b] = P[b] + o.to(torch.bfloat16)`。K=16 恰好是一个 `m16n8k16` atom 的 K 维深度，单 atom 内无分割-K 的求和顺序问题，这是 torch 的 `mm` 能与两条 HMMA 指令逐比特对齐的前提。
- INV 的消费端在 [tests/torch_ref.py:225-231](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L225-L231)：`INV = inv_fwd_subst_16(L)` 之后 `U = torch.matmul(INV, v_chunk)`，对应 Kernel 2 的 Phase 3（u3-l4）。

官方文档对本模块的定位：

- [docs/20260420-flashkda-v1-deep-dive.md:62-67](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/docs/20260420-flashkda-v1-deep-dive.md#L62-L67)：「seed L 全程 fp32（L 的 GEMM 存裸 fp32 累加器、tril/beta 掩码在 fp32 中做）、两个对角 8x8 块用 fp32 前代求逆、非对角块用两次 bf16-HMMA（fp32 累加）合并」——即本讲三个模块的一句话总纲。
- [docs/20260420-flashkda-v1-deep-dive.md:20-22](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/docs/20260420-flashkda-v1-deep-dive.md#L20-L22)：CHUNK=16 的动机之一就是「16x16 求逆足够便宜，可直接前代换完成而无须进一步分解」。

#### 4.2.4 代码实践

**实践目标**：把 lane↔数据映射手工推一遍，确认「每 lane 一行、shuffle 广播主元行」的覆盖性与正确性（纸笔 + 小脚本，无需 GPU）。

**操作步骤**：

1. 对 `tid = 11` 与 `tid = 21` 分别写出 `i`、`row0`、`group`，并回答：这个 lane 在前代换里持有哪个块的哪一行？暂存阶段它写哪个矩阵的哪些元素？
2. 写 `lane_sim.py`（示例代码）：用 Python 列表模拟「每 lane 一行」的算法，与 `torch.linalg.inv` 对拍单个 8x8 块：

```python
import torch
torch.manual_seed(1)
T = torch.eye(8, dtype=torch.float64) + torch.tril(torch.randn(8, 8), -1)
rows = [T[i, :].clone() for i in range(8)]     # lane i 持第 i 行
for s in range(7):
    for i in range(s + 1, 8):
        rows[i] += (-rows[i][s]) * rows[s]     # row_scale * 广播来的主元行
inv_sim = torch.stack(rows)
print("max err:", (inv_sim - torch.linalg.inv(T)).abs().max())
```

3. 把 `rows` 的更新语句与 [csrc/smxx/utils.cuh:243-252](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L243-L252) 的 `fmaf(row_scale, pivot, inv[p])` 逐个对上号，注意模拟里 `rows[s]` 的角色就是 kernel 里从 lane s 广播来的 `pivot`。

**需要观察的现象**：`max err` 在 1e-15 量级；`rows[i][s]`（更新前的值）确实等于原始 \(T_{is}\)，验证 4.1.2 提到的不变量。

**预期结果**：模拟与解析逆一致；tid=11 → i=3、row0=8、group=1（块 B 行 3，暂存 \(P\) 第 11 行）；tid=21 → i=5、row0=0、group=2（块 A 行 5 的冗余拷贝，暂存 \(M\) 第 5 行全零）。（待本地验证）

#### 4.2.5 小练习与答案

**练习 1**：为什么组 2/3 要「白白」重算一份 \(A^{-1}/B^{-1}\)？

**答案**：前代换循环里的 `__shfl_sync` 用的是全掩码 `0xFFFFFFFF`，硬件要求掩码内全部 32 个 lane 都执行该指令。与其让一半 lane 跳过循环再按组维护窄掩码，不如让所有 lane 走完全相同的代码路径——冗余计算只在寄存器里、代价可忽略，换来统一的控制流；同时组 2/3 顺手承担了暂存 \(M\) 的工作，32 个 lane 每人写 16 个元素正好覆盖两个 16x16 矩阵。

**练习 2**：`P` 暂存在 `INV_bf16`、结果又写回 `INV_bf16`，中间隔着两次 HMMA。为什么不会读到被覆盖的旧值？

**答案**：`P` 在暂存后立刻被 LDSM 读进寄存器片段 `tCrP`（[utils.cuh:284-289](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L284-L289)），此后计算全部发生在寄存器；最后的 STSM 覆盖发生在同一 warp 的更晚指令处。同一条 warp 指令流按序执行（中间无分支发散），LDSM 先于 STSM 完成，因此读到的必然是 \(P\)。唯一的跨线程 smem 依赖——组 0/1 写 \(P\)、全 warp LDSM 读——已由暂存后的 `__syncwarp()` 覆盖。

**练习 3**：两次 HMMA 的 B 操作数（`M_b`、`P_b`）为什么必须先过 `MOVM_T`？

**答案**：LDSM 直读 smem 得到的是 **A 操作数**（行主排布）的寄存器片段；而 TN 布局的 HMMA 要求 B 操作数是**列视角**的片段。`SM75_U32x1_MOVM_T`（movmatrix）在寄存器文件内把 warp 级 8x8 片段做转置重排，恰好把 A 片段变成对应转置矩阵的 B 片段——等价于免费拿到 \(M^{\top}\)、\(P^{\top}\) 而不发生任何 smem 往返。这与 Kernel 2 Phase 3 对 `u` 的处理（u3-l4）是同一个技巧。

### 4.3 与 Neumann 级数的精度对比

#### 4.3.1 概念说明

**旧实现回顾**。本 commit（`7afb9f4`，即当前 HEAD）之前，kernel 用 `neumann_inv_fused_1warp` 计算

\[ (I+L)^{-1} \approx (I-L)(I+L^2)(I+L^4)(I+L^8), \]

实现为 3 次平方（\(L^2, L^4, L^8\)）加 3 次累乘，共 6 次 16x16 GEMM，且用的是 `SM80_16x8x16_F16F16F16F16_TN`——**fp16 操作数、fp16 累加**；torch 参考侧对应地用 cuBLAS `CUBLAS_COMPUTE_16F`（fp16 累加）复刻。seed \(L\) 也先被量化成 fp16。

**为什么会失败**。两个效应叠加：

1. **幂次放大**。\(L\) 的元素 \(L_{ij} = \beta_i \, e^{G_i - G_j} (\hat{k}_i \cdot \hat{k}_j)\)（\(\hat{k}\) 为 L2 归一化后的 key，\(G\) 为门控 cumsum，恒负），幅度天然落在 \((-1, 1)\)；当块内两个 key 近共线（重复 token）且门控衰减弱时 \(\hat{k}_i \cdot \hat{k}_j \to 1\)，\(L_{ij} \to 1\)。对元素全为 \(a\) 的严格下三角 \(L = aW\)，有 \((W^k)_{ij} = \binom{i-j-1}{k-1}\)（路径计数），\(i - j = 15\)、\(k = 8\) 时 \(L^8\) 的元素约为 \(\binom{14}{7} a^8 \approx 3.4 \times 10^3\)（\(a \to 1\)）——正是源码注释说的「powers reach ~1e3」。而此时真值 \((I+aW)^{-1} = (I-S)\sum_k (1-a)^k S^k\) 的元素只有 \(O(1)\)。
2. **灾难性抵消**。fp16 只有 11 位尾数（相对舍入单位 \(2^{-11} \approx 4.9 \times 10^{-4}\)），对 \(3 \times 10^3\) 量级的中间量做量化，绝对误差已达到 \(O(1)\)，与最终结果同量级；随后 \((I-L)(I+L^2)\cdots\) 的相减把这些垃圾残差原样带进答案。fp16 累加让情况更糟。

**新实现为何免疫**。前代换从不构造任何 \(L^k\)：消去法始终作用在 \(O(1)\) 的原矩阵上，误差不会被幂次放大；对角块的逆全程 fp32 + `fmaf` 单次舍入。唯一的 bf16 量化发生在合并路径的 HMMA 输入（\(P\)、\(C\)、\(-dc\)、\(o\)），相对误差停在 bf16 水平（\(\sim 2^{-8}\)）且无抵消放大——与 kernel 其他部分（workspace 本来就是 bf16）的精度预算一致。

顺带的收益是成本：6 次 GEMM（每线程 12 条 `m16n8k16` 指令）+ fp16→bf16 转换，变成 2 次 GEMM（4 条指令）+ 少量 shuffle/FMA——精度和速度同时改善。

#### 4.3.2 核心流程

对比两条路线的数值通路：

```text
旧（fp16 Neumann，HEAD~1 的 utils.cuh neumann_inv_fused_1warp）
  seed L: fp32 → fp16（输入即量化）
  INV = I - L
  重复 3 轮: Lpow = Lpow @ Lpow (fp16 acc)
             INV  = INV + INV @ Lpow (fp16 acc)
  INV: fp16 → bf16 → smem
  中间量峰值 ~3e3（|L|→1 时），最终值 O(1) → 灾难性抵消

新（fp32 前代换 + bf16 合并，当前 utils.cuh inv_fwd_subst_fused_1warp）
  seed L: 全程 fp32（L 的 GEMM 存裸 fp32 累加器，tril/beta 在 fp32）
  对角块: fp32 前代换（fmaf 单次舍入）→ A^-1, B^-1
  合并:   P、C、-dc 在 HMMA 输入边界量化 bf16，fp32 累加
  INV:    bf16 出口量化 → smem
  中间量始终 O(1)，无幂次放大
```

相对误差的量级估计（作为实践曲线的预期）：

- 前代换路线：\(\approx 2^{-8}\) 量级（bf16 量化主导），几乎不随 \(\|L\|\) 变化；
- fp16 Neumann：随 \(a\) 增大急剧上升，\(a \to 1\) 时相对误差趋于 \(O(1)\)（答案完全失真）。

#### 4.3.3 源码精读

- 当前实现全文：[csrc/smxx/utils.cuh:194-369](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L194-L369)。函数头注释（194-212 行）就是给未来维护者的「为什么放弃 Neumann」决策记录：*「this never forms \(L^k\) intermediates, so it stays accurate for near-collinear keys where \(|L| \to 1\) (the Neumann powers reach ~1e3 there and cancel catastrophically in fp16)」*。
- 替换的 commit：[commit 7afb9f4](https://github.com/MoonshotAI/FlashKDA/commit/7afb9f454f160a6c4bbc0999beca0a8c40a38934)（"replace fp16 Neumann inverse with 8x8 fp32 forward substitution + 16x16 bf16 merge"）。diff 里能看到被删除的 `neumann_inv_fused_1warp`（`SM80_16x8x16_F16F16F16F16_TN`、`L^2/L^4/L^8` 三轮平方累乘）与 `mma_m16n16_bf16bf16fp16_1warp` 更名。
- seed 精度链的配套改动：[csrc/smxx/fwd_kernel1.cuh:21](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L21) 新增 `LF32Layout`；[csrc/smxx/fwd_kernel1.cuh:67](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L67) 把 SharedStorage 里的 `L` 缓冲从 `BF16` 改为 `float`——fp32 常驻不是免费的，smem 里多花了 512 字节，换来 seed 零量化。
- 精度提升的外部证据：该 commit 同时更新了 `docs/assets/compare_with_fla.png`（与 fla 的精度对比图），deep-dive 第 3 节相应改写为 [docs/20260420-flashkda-v1-deep-dive.md:62-67](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/docs/20260420-flashkda-v1-deep-dive.md#L62-L67) 的「computed exactly」表述。
- torch 参考侧同步重写：[tests/torch_ref.py:81-122](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L81-L122) 的节注释明确写着「replaces the fp16 Neumann series, which loses accuracy when \(|L| \to 1\) near-collinear keys」；同 commit 的 diff 删除了基于 ctypes 调 cuBLAS fp16 累加的 `matmul_fp16acc`，并把 seed 从 `.to(torch.float16)`（旧 216 行）改回 fp32（[tests/torch_ref.py:216](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L216)、beta 掩码也回到 fp32（[tests/torch_ref.py:222](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L222)）。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：写 `inv_compare.py`，在 \(|L|\) 从 0.3 扫到 0.99 时对比三种求逆的相对误差，复现「Neumann 在 \(|L| \to 1\) 时发散、前代换保持平坦」的结论。

**操作步骤**：

1. 新建 `inv_compare.py`（示例代码，不属于仓库）。三种方法：(a) fp64 朴素求逆作金标；(b) `tests/torch_ref.py` 的 `inv_fwd_subst_16`；(c) fp16 Neumann 级数。seed 用「常数填充」\(L = aW\)——这是 Neumann 的最坏情形（路径数二项式增长最快）：

```python
import torch, matplotlib.pyplot as plt

torch.manual_seed(0)

def make_L(a):
    L = torch.zeros(16, 16, dtype=torch.float64)
    L[torch.tril(torch.ones(16, 16, dtype=torch.bool), -1)] = a
    return L

def inv_fp64(L):                       # (a) 金标
    return torch.linalg.inv(torch.eye(16, dtype=torch.float64) + L)

def inv_neumann_fp16(L):               # (c) 旧 kernel 思路的 fp16 复刻
    Lh = L.to(torch.float16)
    I = torch.eye(16, dtype=torch.float16)
    INV = I - Lh
    L2 = Lh @ Lh;  INV = INV + INV @ L2
    L4 = L2 @ L2;  INV = INV + INV @ L4
    L8 = L4 @ L4;  INV = INV + INV @ L8
    return INV

# (b) 需要 GPU：torch_ref 在 import 时会用 load_inline 编译 sigmoid 近似 kernel
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))
from torch_ref import inv_fwd_subst_16

def relerr(X, ref):
    return ((X.to(torch.float64) - ref).norm() / ref.norm()).item()

As = torch.linspace(0.30, 0.99, 35)
errs = {"fwd_subst (new)": [], "neumann fp16 (old)": []}
for a in As:
    L = make_L(float(a))
    ref = inv_fp64(L)
    errs["fwd_subst (new)"].append(relerr(inv_fwd_subst_16(L.to(torch.float32).cuda()), ref))
    errs["neumann fp16 (old)"].append(relerr(inv_neumann_fp16(L), ref))

for name, e in errs.items():
    plt.semilogy(As, e, marker="o", label=name)
plt.xlabel("entries of L (all = a)"); plt.ylabel("relative Frobenius error")
plt.legend(); plt.grid(True); plt.savefig("inv_compare.png", dpi=150)
for a, e1, e2 in zip(As, *errs.values()):
    print(f"a={a:.2f}  fwd_subst={e1:.3e}  neumann={e2:.3e}")
```

2. 确认环境：需要 GPU + 已能 `import torch_ref`（它会现场编译一个极小的 CUDA 扩展）；`matplotlib` 已是 `tests/test.sh` 的测试依赖。
3. 运行 `python inv_compare.py`，观察终端表格与 `inv_compare.png`。
4. 附加检查：打印 `a=0.99` 时 `L8 = (L.to(torch.float16)**?) ` 的中间量——直接算 `((Lh @ Lh) @ (Lh @ Lh)) @ ...` 的最大元素，验证它确实达到 ~3e3。

**需要观察的现象**：

- `fwd_subst` 曲线平坦，相对误差停留在 1e-2 附近（bf16 量化主导，注意 `inv_fwd_subst_16` 出口是 bf16，所以地板是 bf16 精度而非 fp32）；
- `neumann` 曲线在 \(a \gtrsim 0.7\) 后快速抬升，\(a \to 0.99\) 时相对误差逼近甚至超过 1（答案失真）；
- Neumann 中间量 `L8` 的最大元素在 \(a = 0.99\) 时约 \(3 \times 10^3\)。

**预期结果**：与上述现象一致，即源码注释「powers reach ~1e3 and cancel catastrophically」的复现。（待本地验证；若只想验证 (a)/(c) 的对比，可注释掉 `inv_fwd_subst_16` 相关行，CPU 即可运行——torch 的 fp16 matmul 是 fp32 累加 + fp16 存储，旧 kernel 连累加都是 fp16，所以本实践给出的是旧实现误差的**下界**。）

#### 4.3.5 小练习与答案

**练习 1**：为什么说本实践里 torch fp16 matmul 复刻的是旧实现误差的下界？

**答案**：旧 kernel 用 `SM80_16x8x16_F16F16F16F16_TN`，操作数与累加器都是 fp16；torch 的 fp16 `@` 在 CUDA 上以 fp32 累加、只在存储时量化 fp16。两者共享「中间幂次以 fp16 存储」这一主要误差源（\(3\times10^3\) 量级上的 fp16 量化误差 \(\sim 1.5\)），但 fp16 累加会再叠加求和过程中的逐项舍入，所以旧实现的真实误差只会更大。

**练习 2**：\(|L| \to 1\) 对应什么输入？为什么 KDA 特别容易碰到？

**答案**：\(L_{ij} = \beta_i e^{G_i - G_j} (\hat{k}_i \cdot \hat{k}_j)\)（\(i > j\)）。key 经过 L2 归一化后 \(\hat{k}_i \cdot \hat{k}_j \in [-1, 1]\)，门控 cumsum 差 \(G_i - G_j \le 0\) 只会衰减，所以 \(L_{ij}\) 天然有界于 1；当块内两个 token 的 key 几乎同向（重复/近重复内容，即「近共线 key」）且遗忘门偏弱时 \(L_{ij} \to 1\)。文本里重复模板、代码里的重复行都是天然高频模式，所以这不是病态构造而是常态输入。

**练习 3**：如果 CHUNK 从 16 改成 64，前代换方案会发生什么？（联系 u1-l1 的 CHUNK=16 设计决策。）

**答案**：分块前代换本身可推广（64x64 可再分成 8x8 或 16x16 的块阵），但 CHUNK=64 意味着门控 cumsum 跨 64 步，\(e^{\mathrm{cumsum}}\) 的动态范围会超出 bf16 表示能力（deep-dive 第 1 节），需要块内重缩放；同时 \(L\) 变成 64x64，块内求逆的计算与寄存器压力都暴涨。CHUNK=16 让「bf16 范围 + 前代换精确求逆 + SM80 MMA 完美匹配」三件事同时成立——求逆方式与 chunk 尺寸是耦合设计，不能单独改一个。

## 5. 综合实践

把三个模块串起来做一次「从真实输入到 bit-exact」的闭环验证（需要 GPU 与已安装的 flash_kda，参考 u1-l3/u1-l5）：

1. **构造近共线输入**：写 `collinear_e2e.py`，生成 `B=1, T=64, H=4, D=128` 的输入，其中 k 的前 32 个 token 全部相同（保留逐通道微小抖动 \(\sim 10^{-3}\) 以免完全退化）、g 的 logits 取偏大值（弱遗忘）、beta 取偏大 logits（强写入）——这正是让 \(\|L\| \to 1\) 的真实分布。
2. **先单点验证求逆**：调用 `torch_ref.torch_ref` 前手动复算 `L`（照抄 [tests/torch_ref.py:216-225](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L216-L225) 的路径：L2 归一化 → 衰减变体 → `torch.mm` → tril×beta），对 `chunk_idx=1, h=0` 的 L 分别算 `inv_fwd_subst_16(L)` 与 fp64 金标，确认相对误差在 bf16 水平；同时用 4.3.4 的 fp16 Neumann 对同一 L 求逆，确认它在该 L 上已经失真。
3. **端到端 bit-exact**：按 `tests/test_fwd.py` 的用法对同一输入跑 `flash_kda.fwd` 与 `torch_ref`，断言 `torch.equal`——确认新求逆路径的每个量化点都被参考实现复刻。
4. **端到端精度**：再用 u1-l2 的逐 token fp64 双层循环递推做金标，比较 `flash_kda.fwd` 的 `out` 与 `final_state` 的 max/mean 相对误差；它应当停在 bf16 水平（~1e-2），而不是 Neumann 时代的 O(1) 失真。

预期结论：近共线输入下，(2) 中前代换与金标的偏差是 bf16 量级而 Neumann 明显更大；(3) bit-exact 成立；(4) 端到端误差与 `docs/20260420-flashkda-v1-deep-dive.md` 中与 fla 的对比结论一致。（待本地验证）

## 6. 本讲小结

- 16x16 求逆被拆成「8x8 对角块 fp32 前代换 + 16x16 bf16 HMMA 块合并」：\(X^{-1} = P + (-PM)P\)，其中 \(P = \mathrm{diag}(A^{-1}, B^{-1})\)、\(M = [0\,0;\,C\,0]\)，两次 GEMM 完成 \(-B^{-1}CA^{-1}\)。
- 实现全部塞进一个 warp：每 lane 持一行、全掩码 `__shfl_sync` 广播主元行做 7 步行消去；组 2/3 的冗余拷贝换取统一的 shuffle 收敛并顺手暂存 \(M\)（复用已死的 `k_inv` smem）；合并路径 LDSM→MOVM_T→HMMA→STSM 全程不落 smem。
- 精度设计：seed \(L\) 全程 fp32（L 的 GEMM 存裸 fp32 累加器、tril/beta 掩码 fp32），量化只发生在 HMMA 输入边界（\(P\)、\(C\)、\(-dc\)、\(o\)）与最终出口，均 RNE bf16。
- 旧 fp16 Neumann \((I-L)(I+L^2)(I+L^4)(I+L^8)\) 在近共线 key（\(|L|\to1\)）时中间幂次达 ~3e3、最终值 O(1)，fp16 下灾难性抵消；前代换不构造 \(L^k\)，天然免疫，且少用 4 条 HMMA 指令。
- `tests/torch_ref.py` 的 `inv_fwd_subst_16` 用 `fp32_fma` 与相同的切片顺序逐比特复刻 kernel，是 exact-match 测试能够成立的关键一环。

## 7. 下一步学习建议

本讲搞定了 Kernel 1 的最后一块硬骨头（INV），下一讲 **u3-l2「Kernel 2 架构」** 将进入消费 INV 的递推 kernel：192 线程的 warp 专用化（4 MMA + 1 LOAD + 1 STORE）、`SharedStorageK2` 的多级缓冲与 union、以及两条 pipeline 的生产者-消费者构造。届时可以重点关注 INV 如何在 Phase 3 被 `INV @ u` 消费（u3-l4），把本讲的输出接到下一环。若想先巩固本讲，建议动手做完 4.3.4 的误差曲线，再回头通读一遍 [csrc/smxx/utils.cuh:194-369](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L194-L369)，并对照 [commit 7afb9f4](https://github.com/MoonshotAI/FlashKDA/commit/7afb9f454f160a6c4bbc0999beca0a8c40a38934) 的完整 diff 看「一次精度驱动的重构」如何同时改动 kernel、参考实现与文档。
