# u3-l5 MMA 相位 6：状态更新的寄存器转置与预取环

## 1. 本讲目标

本讲是 Kernel 2（`_flash_kda_fwd_recurrence`）MMA warp 主体精读的下半场。上一讲（u3-l4）跟到了相位 5：输出 tile 写回 smem、`U` 以 B 操作数片段的形式留在寄存器数组 `tCrB_u_arr` 中。本讲精读收官的**相位 6：状态更新**

\[
s_{\text{acc}} \;\leftarrow\; s_{\text{acc}} \cdot e^{g_{\text{total}}} \;+\; k_{\text{restored}}^{\top} U
\]

学完本讲，你应该能够：

1. 解释为什么状态更新必须通过**转置布局视图**（`TransposedMMALayout` / `TransposedStateSmemLayout`）+ `LDSM_T` / `STSM_T` 完成，而相位 1 的状态读取完全不需要转置——根源是 \(k_{\text{restored}}^{\top}\) 的形状决定了哪个操作数必须"换个方向"看。
2. 跟踪 `PREFETCH=1` 预取环（`ring_A_kr` / `ring_S_acc` / `ring_g0` / `ring_g1`）中每个 slot 在 8 级 M 维循环里的装载-计算-回写时机，并推算把 `PREFETCH` 改为 2 的寄存器代价。
3. 论证「状态以 bf16 存储 + 更新路径用 fp32 FMA」这一精度取舍（对照 deep-dive 第 3 节），并对照 `torch_ref` 的 `work_state` 更新行确认 kernel 与参考实现在舍入点上逐一对齐。

## 2. 前置知识

本讲默认你已掌握以下内容（不熟悉请先回看对应讲义）：

- **KDA 的 chunk 递推与状态矩阵**（u1-l2）：状态 \(S \in \mathbb{R}^{V \times K}\)（本项目 \(V=K=D=128\)），每个 chunk 先按通道遗忘 \(S \cdot \operatorname{diag}(e^{g_{\text{total}}})\)，再叠加写入项 \(\delta s\)。
- **CuTe 布局与 GMMA atom**（u2-l4）：`Layout_K_INTER_Atom`（行主、配 `LDSM_N` 直读）与 `Layout_MN_INTER_Atom`（同一块内存的转置视图、配 `LDSM_T`）是同一套 8×8 swizzle 原子家族的两个视角。
- **K2 架构**（u3-l2）：192 线程 = 4 个 MMA warp（128 线程）+ 1 个 LOAD warp + 1 个 STORE warp；MMA warp 间用 `NamedBarrier(128)` 同步，**不能**换成 `__syncthreads()`（会与 LOAD/STORE warp 的流水线等待点死锁）。
- **MMA 相位 1-5**（u3-l4）：每 warp 负责输出/状态的两个 16×16 列块（`warp_id*2 + bi`，`bi ∈ {0,1}`）；`SM80_16x8x16_F32BF16BF16F32_TN` atom 经 `Tile<_16,_16,_16>` 组装成 16×16×16 的计算单元；C 片段每线程 8 个值、只落在两行 `{group_id, group_id+8}`（`group_id = (lane_id/4) % 8`）；相位 3 用 `MOVM_T` 把 `U` 从 C 片段转成 B 片段。
- **torch_ref 的作用**（u2-l1）：它是 kernel 行为的逐操作规格，状态更新对应 [tests/torch_ref.py:227-239](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L227-L239)。

一个贯穿全讲的记号约定：`s_acc(v, k)` 中 \(v\) 是 V 维（值/输出通道）、\(k\) 是 K 维（键/衰减通道）；**转置视角**记作 \(s_T(k, v) = s(v, k)\)。所谓"寄存器文件内转置"，指的是借助转置视图 + 转置版拷贝指令（`LDSM_T`/`STSM_T`），让数据在 smem ↔ 寄存器之间的搬运途中顺带完成转置，**不需要**额外的 smem 中转缓冲或独立的转置 pass。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注的行段 |
| --- | --- | --- |
| `csrc/smxx/fwd_kernel2.cuh` | K2 递推 kernel 全部实现 | 转置布局定义 L16-33、`s_acc_T`/`k_restored_t` 视图 L457-466、转置版 copy atom L484-502、相位 6 主体 L659-731、收尾同步 L733-741 |
| `csrc/smxx/utils.cuh` | 公共工具（PTX 数值原语等） | `bf16_to_f32` L55-59、`ex2_approx_ftz_f32` L38-42 |
| `tests/torch_ref.py` | bit-exact torch 参考实现 | 状态更新行 L227-239、`fp32_fma` L55-59、`fp32_ex2_ftz` L47-52 |
| `docs/20260420-flashkda-v1-deep-dive.md` | 官方设计文档 | 第 3 节数值精度 L45-67、K2 寄存器转置优化 L83-86 |

代码量级提醒：K2 全文 839 行，相位 6 占 L659-731 共约 73 行，是全 kernel 中密度最高的代码段之一。

## 4. 核心概念与源码讲解

### 4.1 转置布局视图与 LDSM_T/STSM_T：状态更新为什么要"换一副眼镜"

#### 4.1.1 概念说明

先看参考实现怎么写状态更新（注意其中的两次转置）：

```python
# tests/torch_ref.py:227-239（节选）
state_slice = work_state[seq_idx, h]                      # [V, K] bf16
...
U = torch.matmul(INV, v_chunk)                            # [16, V]
delta_s = torch.mm(k_restored.t(), U, out_dtype=torch.float32)   # [K, V] fp32

g_total_exp = fp32_ex2_ftz(g_total)                       # [K] fp32
g_total_exp = g_total_exp.squeeze(0).unsqueeze(-1)        # [K, 1]
work_state[seq_idx, h] = fp32_fma(delta_s, state_slice.to(torch.float32).t(),
                                  g_total_exp).to(torch.bfloat16).t()
```

把这三行翻译成数学：

\[
\delta s = k_{\text{restored}}^{\top} U \in \mathbb{R}^{K \times V}, \qquad
s_T^{\text{new}} = \delta s + s_T \odot e^{g_{\text{total}}}, \qquad
s^{\text{new}} = \left(s_T^{\text{new}}\right)^{\top}
\]

即 torch_ref 是**在转置视角 \([K, V]\) 里做读-改-写，最后转置回 \([V, K]\) 存储**。为什么要绕这一圈？因为 \(\delta s\) 的左因子是 \(k_{\text{restored}}^{\top}\)——一个 \([K, 16]\) 的矩阵，它的行方向是 K 通道，而 smem 里 `k_restored` 是按 \([16, K]\)（CHUNK 行 × D 列）存放的。

再对照输出路径（相位 1）就看清差别了：

- **相位 1 读状态**：\(\text{out} = q_{\text{decayed}} @ s^{\top}\)。HMMA 的 B 操作数需要 \(N \times K\) 排布，这里 \(N\) 维是 V、\(K\) 维是 K——恰好就是 `s_acc` 的自然存储方向 \((V, K)\)，`K_INTER` 布局 + `LDSM_N` 直接可读，**零转置**。
- **相位 6 写状态**：\(\delta s_T = k_{\text{restored}}^{\top} @ U\)。HMMA 的 **A 操作数**需要 \(M \times K\) 排布（\(M\) 是 K 通道、\(K\) 是 CHUNK），对应 \(k_{\text{restored}}^{\top} \in \mathbb{R}^{K \times 16}\)——而 smem 里只有 \([16, K]\) 的存放。**必须转置**。

那为什么选择"转置 k_restored 和 s_acc"，而不是反过来"转置 U"？这里藏着一个漂亮的衔接设计：**相位 3 的 `MOVM_T` 已经把 U 转成了 B 操作数片段** `tCrB_u_arr`（B 操作数需要 \(N \times K = (V, \text{CHUNK})\) 排布，正是 \(U\) 的转置方向）。相位 6 的 GEMM 里 B 操作数恰好又是 U——直接复用寄存器里现成的 B 片段，**一次都不用再转**。于是转置的代价被推给了本来就躺在 smem 里的 `k_restored` 和 `s_acc`，而对 smem 数据做转置读/写只需"换视图 + 换拷贝指令"。

总结成一张对照表：

| | 相位 1（读状态） | 相位 6（写状态） |
| --- | --- | --- |
| GEMM | \(q_{\text{decayed}} @ s^{\top}\) | \(k_{\text{restored}}^{\top} @ U\) |
| A 操作数 | `q_decayed` 块 \([16,16]\)，`K_INTER` + `LDSM_N` | `k_restored_t` 块 \([16,16]\)，`MN_INTER` 视图 + `LDSM_T` |
| B 操作数 | `s_acc` 块 \((V\text{ 块}, K\text{ 块})\)，`K_INTER` + `LDSM_N` | `tCrB_u_arr[bi]`（相位 3 `MOVM_T` 的寄存器遗产），零拷贝 |
| C 操作数 | 寄存器 fp32 累加器 | `s_acc_T` 块 \((K\text{ 块}, V\text{ 块})\)，`LDSM_T` 读 / `STSM_T` 写 |
| 状态视角 | 自然视角 \((V, K)\) | 转置视角 \((K, V)\) |

转置视角还有一个附赠的好处：**逐通道衰减 \(e^{g_{\text{total}}}\) 在转置视角里是"逐行缩放"**。HMMA 的 C 片段每线程只落在两行 \(\{g, g+8\}\)（\(g =\) `group_id`），所以每线程每块只需两个 fp32 标量（`ring_g0` 缩放第 \(g\) 行、`ring_g1` 缩放第 \(g+8\) 行），完全对齐片段的行分布——若在自然视角里做，衰减是"逐列"的，与片段分布不再对齐。

#### 4.1.2 核心流程

相位 6 的工作划分（4 个 MMA warp，每 warp 两个 16 宽 V 列块）：

```
s_acc_T 的形状（转置视角）：[K=128, V=128]，切成 8×8 个 16×16 块
每个 warp w 负责列块 j ∈ {2w, 2w+1}（即 V 方向 32 列），遍历全部 8 个行块 m（K 方向）
每个块 (m, j)：
  读   s_acc_T[16m:16m+16, 16j:16j+16]        —— LDSM_T 装入 C 片段 ring_S_acc
  算   δs 块 = k_restored_t[16m:16m+16, :] @ U[:, 16j:16j+16]   —— HMMA, fp32 累加
  改   ring_S_acc = BF16( f32(ring_S_acc) * e^g + δs )         —— 逐行缩放 + 单次舍入
  写   STSM_T 回 s_acc_T[16m:16m+16, 16j:16j+16]
```

注意一个关键的**条带私有性**：warp \(w\) 在相位 6 写的是 \(s_T\) 的列 \(v \in [32w, 32w+32)\)（等价于 `s_acc` 的行条带），而它在**下一个 tile 的相位 1** 读的 B 操作数块恰好是 `s_acc` 的行 \(v \in [32w, 32w+32)\)——正是自己刚写的条带。四个 warp 的读/写区域在 tile 间不交叉。

#### 4.1.3 源码精读

**（1）两个转置布局的类型定义**（与自然布局共用同一套 8×8 swizzle 原子家族）：

```cpp
using TransposedMMALayout = decltype(tile_to_shape(
    GMMA::Layout_MN_INTER_Atom<cute::bfloat16_t>{},
    make_shape(Int<D>{}, Int<CHUNK>{}),
    LayoutRight{}
));
```

这段声明把 `Layout_MN_INTER_Atom`（转置视角 atom）铺满 \((D, \text{CHUNK}) = (128, 16)\)，得到 `k_restored` smem 的转置视图布局类型。见 [csrc/smxx/fwd_kernel2.cuh:16-20](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L16-L20)。

```cpp
using TransposedStateSmemLayout = decltype(tile_to_shape(
    GMMA::Layout_MN_INTER_Atom<cute::bfloat16_t>{},
    make_shape(Int<D>{}, Int<D>{}),
    LayoutRight{}
));
```

同样的手法铺满 \((128, 128)\)，作为状态矩阵 `state_acc`（32 KB bf16，见 [csrc/smxx/fwd_kernel2.cuh:82](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L82)）的转置视图布局。见 [csrc/smxx/fwd_kernel2.cuh:29-33](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L29-L33)。由于 `MN_INTER` 与 `K_INTER` 由同一 8×8 核心原子按不同方向拼接（u2-l4），这两个类型只是"对同一块比特换一个坐标解释"，不需要搬运任何数据。

**（2）在 kernel 里创建两个转置视图张量**：

```cpp
Tensor s_acc   = make_tensor(make_smem_ptr(shared_storage.state_acc.begin()), StateSmemLayout{});
Tensor s_acc_T = make_tensor(make_smem_ptr(shared_storage.state_acc.begin()), TransposedStateSmemLayout{});
```

同一块 32 KB smem 上同时挂两个视图：相位 1 用 `s_acc`（自然视角），相位 6 用 `s_acc_T`（转置视角）。见 [csrc/smxx/fwd_kernel2.cuh:457-458](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L457-L458)。

```cpp
Tensor k_restored_t = make_tensor(make_smem_ptr(shared_storage.input[load_stage].k_restored.begin()),
                                  TransposedMMALayout{});
```

当前 stage 输入缓冲里的 `k_restored`（本 tile 由 LOAD warp 经 TMA 写入）挂上转置视图，供相位 6 作为 A 操作数读取。见 [csrc/smxx/fwd_kernel2.cuh:464](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L464)。

**（3）转置版的 smem↔寄存器拷贝 atom**：

```cpp
// A copy: MN_INTER → LDSM_T (for k_restored_t in Phase 7)
auto smem_tiled_copy_A_T = make_tiled_copy_A(Copy_Atom<SM75_U16x8_LDSM_T, BF16>{}, mma);
auto smem_thr_copy_A_T   = smem_tiled_copy_A_T.get_thread_slice(lane_id);
```

A 操作数的转置装载通道：`SM75_U16x8_LDSM_T` 是 `ldmatrix` 的转置变体，从 `MN_INTER` 视图装出的片段恰好满足 HMMA A 片段要求的 \(M \times K\) 方向（注释里的 "Phase 7" 是历史编号，即本讲的相位 6）。见 [csrc/smxx/fwd_kernel2.cuh:484-486](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L484-L486)。

```cpp
// C load/store transposed (for Phase 6 state access via s_acc_T)
auto smem_tiled_load_C_T  = make_tiled_copy_C(Copy_Atom<SM75_U16x8_LDSM_T, BF16>{}, mma);
auto smem_thr_load_C_T    = smem_tiled_load_C_T.get_slice(lane_id);
auto smem_tiled_store_C_T = make_tiled_copy_C(Copy_Atom<SM90_U16x8_STSM_T, BF16>{}, mma);
auto smem_thr_store_C_T   = smem_tiled_store_C_T.get_slice(lane_id);
```

C 操作数（状态块）的转置读/写通道：读用 `LDSM_T`、写用 `SM90_U16x8_STSM_T`（`stmatrix` 的转置变体）。对照相位 5 输出路径用的 `SM90_U32x4_STSM_N`（[csrc/smxx/fwd_kernel2.cuh:493-496](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L493-L496)），可以看到同一个 `mma` 对象通过配对不同 copy atom 就能服务两种视角。见 [csrc/smxx/fwd_kernel2.cuh:498-502](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L498-L502)。

**（4）片段类型与 B 操作数遗产**：

```cpp
using SFragT = decltype(make_fragment_like<BF16>(thr_mma.make_fragment_C(tCrC_ref)));
using AFragT = decltype(thr_mma.partition_fragment_A(A_ref));
using BFragT_u = decltype(thr_mma.partition_fragment_B(B_ref));
```

相位 6 用到的三种片段类型：`AFragT`（`k_restored_t` 的 A 片段，每线程 8 个 bf16 = 4 个 32 位寄存器）、`SFragT`（状态块的 C 片段，同规格）、`BFragT_u`（U 的 B 片段）。见 [csrc/smxx/fwd_kernel2.cuh:522-525](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L522-L525)。

```cpp
BFragT_u tCrB_u_arr[2];
```

相位 4 通过 `MOVM_T` 构造好的 U 的 B 片段数组（`bi=0/1` 对应两个列块），在相位 6 被**原样复用**为 HMMA 的 B 操作数——这就是"转置成本推给 smem 侧"的兑现点。见 [csrc/smxx/fwd_kernel2.cuh:629](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L629)。

**（5）相位 6 的注释头与 M 维块数**：

```cpp
// ======== Phase 6: s_acc update ========
// s_acc[D, D] = s_acc * g_total + k_restored_t[D, 16] @ U[16, D]
// Each warp handles columns [warp_id*32, (warp_id+1)*32] = 2 x 16x16 blocks
// U is already in tCrB_u_arr[0..1] as B operands (from Phase 4 MOVM_T)
constexpr int S_M_BLOCKS = decltype(cute::size<0>(k_restored_t))::value / 16;
```

官方注释直接点明了本讲的全部要点；`S_M_BLOCKS = size<0>(k_restored_t) / 16 = 128 / 16 = 8`，即 K 方向要迭代 8 个 16×16 块。见 [csrc/smxx/fwd_kernel2.cuh:659-663](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L659-L663)。

**（6）把 smem 块"过境"到 A 片段的暂存手法**（预取环的装载入口，4.2 详解）：

```cpp
Tensor tCrAi_kr = make_fragment_like<BF16>(thr_mma.partition_fragment_A(A_ref));
auto tCrAi_kr_view = smem_thr_copy_A_T.retile_D(tCrAi_kr);
```

`LDSM_T` 的结果先落入"拷贝布局"的暂存片段 `tCrAi_kr`，再逐元素搬进 MMA 布局的 `ring_A_kr`（编译为寄存器移动），衔接拷贝 atom 与 MMA 的值分布。见 [csrc/smxx/fwd_kernel2.cuh:665-666](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L665-L666)。

deep-dive 把这套设计总结为「K2 register-file transposes」：通过 `MOVM_T`（寄存器内，U 路径）与转置视图 + `LDSM_T`/`STSM_T`（smem 边界，本模块）消灭了阶段间全部 smem 往返，同时缩小了 K2 的 smem 缓冲需求，见 [docs/20260420-flashkda-v1-deep-dive.md:83-86](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/docs/20260420-flashkda-v1-deep-dive.md#L83-L86)。

#### 4.1.4 代码实践

**实践目标**：亲手验证"转置视角 + 分块"的读-改-写与整块更新在数学上等价，并把每条 smem 访问归类到视图/atom/操作数。

**操作步骤**（示例代码，可保存为 `phase6_views.py`）：

```python
# 示例代码：验证相位 6 的转置视角分块更新 == 整块更新（fp32 执行）
import torch
torch.manual_seed(0)
D, CHUNK = 128, 16

k_restored = torch.randn(CHUNK, D).to(torch.bfloat16)      # smem 中的 [CHUNK, K]
U          = torch.randn(CHUNK, D).to(torch.bfloat16)      # 寄存器中的 U（此处用矩阵模拟）
s          = torch.randn(D, D).to(torch.bfloat16)          # s_acc [V, K]
g_exp      = torch.rand(D) + 0.1                           # e^{g_total}，fp32，形状 [K]

# 路径 A：整块更新（torch_ref 的数学，fp32 执行）
delta = k_restored.float().t() @ U.float()                 # [K, V]
s_new_A = (s.float().t() * g_exp[:, None] + delta).to(torch.bfloat16).t()

# 路径 B：模拟 4 warp × 8 个 M 块 × 2 个列块的转置视图分块更新
s_T = torch.empty(D, D, dtype=torch.bfloat16)
for w in range(4):
    for m in range(8):
        A_blk = k_restored.float().t()[16*m:16*(m+1), :]           # k_restored_t 的 (m) 块
        for bi in range(2):
            j = 2*w + bi
            delta_blk = A_blk @ U.float()[:, 16*j:16*(j+1)]        # HMMA 做的事
            s_blk = s.float().t()[16*m:16*(m+1), 16*j:16*(j+1)]    # LDSM_T 读
            s_T[16*m:16*(m+1), 16*j:16*(j+1)] = \
                (s_blk * g_exp[16*m:16*(m+1), None] + delta_blk).to(torch.bfloat16)
s_new_B = s_T.t()

print("bit equal:", torch.equal(s_new_A, s_new_B),
      "max|diff|:", (s_new_A.float() - s_new_B.float()).abs().max().item())
```

**需要观察的现象**：两个路径的 bf16 结果是否逐位一致；若不一致，差异的量级（应当只可能是 fp32 GEMM 分块归约顺序带来的个别 ulp）。

**预期结果**：若平台上的 fp32 矩阵乘对不同分块走相同核，`bit equal: True`；否则 `max|diff|` 为 bf16 一个量化台阶以内（约 \(2^{-8}\) 相对量级）。具体打印值**待本地验证**。第二个小任务（纯阅读）：对照 4.1.3 的第（3）点，把相位 6 涉及的 4 类 smem 访问（A 装 / C 装 / C 写 / B 复用）填进 4.1.1 的对照表并核对——答案就在表中。

#### 4.1.5 小练习与答案

**练习 1**：如果把相位 6 改写成"转置 U 而不转置 k_restored"（例如 \(s \leftarrow s \cdot e^g + U^{\top} k_{\text{restored}}\) 的某种变体，A 操作数取 \(U^{\top}\)），会遇到什么额外代价？

**答案**：U 只存在于寄存器中的 B 片段 `tCrB_u_arr`；要把它变成 A 操作数，必须再做一次寄存器转置（`MOVM_T` 反向）或者经 smem 往返，而且 \(U^{\top} k_{\text{restored}}\) 的形状是 \([V, K]\)，衰减又变回"逐列缩放"、与 C 片段行分布错位。相反，`k_restored` 与 `s_acc` 本来就在 smem，换视图 + `LDSM_T`/`STSM_T` 零数据搬运。

**练习 2**：为什么相位 1 读状态不需要转置视图，而相位 6 需要？

**答案**：相位 1 的 GEMM 是 \(q_{\text{decayed}} @ s^{\top}\)，HMMA 的 B 操作数需要 \((V, K)\) 的 \(N \times K\) 排布——这正好是 `s_acc` 的自然存储方向（`K_INTER` + `LDSM_N` 直读）。相位 6 的 A 操作数是 \(k_{\text{restored}}^{\top} \in \mathbb{R}^{K \times 16}\)，行方向变成 K 通道，与 smem 存放方向 \([16, K]\) 相反，且 C 结果天然落在 \((K, V)\) 转置视角上。

**练习 3**：每线程为什么只需要 `ring_g0`/`ring_g1` 两个衰减标量，而不是 8 个？

**答案**：C 片段的 8 个元素只分布在两行 \(\{\text{group\_id}, \text{group\_id}+8\}\) 上（每行 4 个列位置），而转置视角下衰减 \(e^{g_{\text{total}}}\) 恰好按行（K 通道）作用，同一行共享同一个因子；这两个标量按块取值 `g_total(16m + group_id)` 与 `g_total(16m + group_id + 8)`。

### 4.2 PREFETCH 预取环：S_M_BLOCKS 级 M 维循环的软件流水

#### 4.2.1 概念说明

相位 6 的主循环要沿 K 方向迭代 \(S\_M\_BLOCKS = 8\) 个 16×16 块。每块的处理是典型的"装→算→写"三拍：

1. **装**：`LDSM_T` 装入 A 片段（`k_restored_t` 块）与 C 片段（`s_acc_T` 块）；
2. **算**：2 次 HMMA（每个列块 `bi` 一次）算出 fp32 的 \(\delta s\) 块，再与 C 片段做逐元素 FMA；
3. **写**：`STSM_T` 把更新后的 C 片段写回 `s_acc_T`。

三拍若串行执行，`LDSM_T` 的 smem 访问延迟（几十个周期）会逐块裸露。解法是教科书式的**软件流水**：用一组环形寄存器缓冲（ring）把"第 \(m+1\) 块的装载"塞进"第 \(m\) 块的计算与写回"中间。环的深度就是常量 `PREFETCH`：

```cpp
constexpr int PREFETCH = 1;
```

见 [csrc/smxx/fwd_kernel2.cuh:466](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L466)。`PREFETCH=1` 时环退化为单 slot：一个"正在被消费"的缓冲，装载下一块的指令插在消费指令之后，靠指令级并行（编译器重排 + 记分牌）让装载延迟被后续 FMA/STSM 覆盖。

寄存器开销的精确账本（每线程、以 32 位寄存器计）：

| 环数组 | 片段类型 | 单片段大小 | PREFETCH=1 | PREFETCH=2 | 增量 |
| --- | --- | --- | --- | --- | --- |
| `ring_A_kr[PREFETCH]` | `AFragT`（8×bf16） | 4 个 u32 | 4 | 8 | +4 |
| `ring_S_acc[2][PREFETCH]` | `SFragT`（8×bf16）×2 列块 | 4 个 u32 × 2 | 8 | 16 | +8 |
| `ring_g0[PREFETCH]`, `ring_g1[PREFETCH]` | `float` | 1 个 u32 × 2 | 2 | 4 | +2 |
| **合计** | | | **14** | **28** | **+14** |

即把 `PREFETCH` 从 1 改到 2，静态上每线程多占 **14 个 32 位寄存器**（暂存片段 `tCrAi_kr` 的 4 个寄存器不随 PREFETCH 变化）。注意这只是源码层面的推导：ptxas 可能重排、复用甚至引入溢写，SASS 级的实际增量需用 `--ptxas-options=-v`（setup.py 已默认传入）对比编译确认，**待本地验证**。

#### 4.2.2 核心流程

把 [csrc/smxx/fwd_kernel2.cuh:659-731](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L659-L731) 提炼成伪代码（`PREFETCH=1` 版本，`slot = m % PREFETCH` 恒为 0，这里保留通式）：

```text
环寄存器：ring_A_kr[P], ring_S_acc[2][P], ring_g0[P], ring_g1[P]     # P = PREFETCH
持久寄存器：tCrB_u_arr[0..1]  # U 的 B 片段，整个相位 6 只装一次

# ---- 预热（prologue）：装满环 ----
for i in 0..P-1:
    ring_A_kr[i]       = LDSM_T(k_restored_t 块 (i, 0))
    for bi in {0,1}:
        ring_S_acc[bi][i] = LDSM_T(s_acc_T 块 (i, warp_id*2 + bi))
    ring_g0[i] = g_total(i*16 + group_id)          # fp32，已是 e^g
    ring_g1[i] = g_total(i*16 + group_id + 8)

# ---- 主循环：8 个 M 块 ----
for m in 0..S_M_BLOCKS-1:                           # S_M_BLOCKS = 8
    slot = m % P
    g0, g1 = ring_g0[slot], ring_g1[slot]

    # (a) 算 δs 块：A = ring_A_kr[slot]，B = U 片段（复用），fp32 累加
    for bi in {0,1}:
        u_acc[bi] = HMMA( ring_A_kr[slot], tCrB_u_arr[bi] )     # 2 次 gemm

    # (b) 预取下一 M 块的 A 与衰减标量（写回刚消费完的同一 slot）
    if m + P < S_M_BLOCKS:
        ring_A_kr[slot] = LDSM_T(k_restored_t 块 (m+P, 0))
        ring_g0[slot]   = g_total((m+P)*16 + group_id)
        ring_g1[slot]   = g_total((m+P)*16 + group_id + 8)

    # (c) 逐元素融合更新 + 写回 + 预取下一 S 块
    for bi in {0,1}:
        ring_S_acc[bi][slot] = BF16( f32(ring_S_acc[bi][slot]) * {g0|g1} + u_acc[bi] )
        STSM_T( ring_S_acc[bi][slot] -> s_acc_T 块 (m, warp_id*2 + bi) )
        if m + P < S_M_BLOCKS:
            ring_S_acc[bi][slot] = LDSM_T(s_acc_T 块 (m+P, warp_id*2 + bi))

NamedBarrier(128).arrive_and_wait()                 # 4 个 MMA warp 会师
```

`PREFETCH=1` 与 `PREFETCH=2` 下环下标随 `m` 的变化表（预取目标块的判定条件是 `m + P < S_M_BLOCKS`）：

| P | m | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 消费块 `m` / slot | 0/0 | 1/0 | 2/0 | 3/0 | 4/0 | 5/0 | 6/0 | 7/0 |
| 1 | 预取块（`m+1`） | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 不预取 |
| 2 | 消费块 `m` / slot | 0/0 | 1/1 | 2/0 | 3/1 | 4/0 | 5/1 | 6/0 | 7/1 |
| 2 | 预取块（`m+2`） | 2 | 3 | 4 | 5 | 6 | 7 | 不预取 | 不预取 |

由于 \((m + P) \bmod P = m \bmod P\)，**预取永远写回刚刚消费完的那个 slot**——这就是"环"的含义：slot 数为 P，消费与装载相隔 P 步。守卫条件同时保证了最后一次循环不会越界读 `g_total`（最大下标 \((S\_M\_BLOCKS-1) \times 16 + 15 = 127\)）。

时序上还要注意两处顺序（都以"先消费、后覆盖"为原则）：

- A 片段的预取（步骤 b）排在 HMMA（步骤 a）**之后**：同一个 `ring_A_kr[slot]` 必须先被 gemm 读走才能覆盖；其 LDSM 延迟由步骤 c 的 FMA/STSM 覆盖。
- S 片段的预取（步骤 c 末尾）排在 `STSM_T` 写回**之后**：`ring_S_acc[bi][slot]` 必须先完成"读-改-写"的写回，才能装下一块。

gemm 计数对账（承接 u3-l4 的 52 次/warp/tile）：相位 6 贡献 \(8 \times 2 = 16\) 次 `gemm` 调用（8 个 M 块 × 2 个列块，每次是 16×16×16 的双 atom HMMA），加上相位 1 的 32 次、相位 3 与相位 4 各 2 次，合计 52 次。

**收尾同步**：主循环结束处的 [csrc/smxx/fwd_kernel2.cuh:733-741](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L733-L741)，`compute_barrier.arrive_and_wait()`（`NamedBarrier(kComputeThreads=128, 0)`，定义在 [csrc/smxx/fwd_kernel2.cuh:427](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L427)）让 4 个 MMA warp 会师，随后依次执行 `fence_view_async_shared()`（generic 代理的 STSM 写对 async 代理的 TMA 可见）、`store_pipeline.producer_commit`（放行 STORE warp 的输出 TMA）、`load_pipeline.consumer_release`（放行 LOAD warp 复用输入 stage）。从纯数据竞争看，各 warp 的状态条带在本 warp 内自洽（下一 tile 相位 1 读的正是自己相位 6 写的条带，最低只需 warp 级同步），但统一的 128 线程 NamedBarrier 一并承担了 warp 内跨 lane 可见性与流水线记账前的对齐；这里绝不能换成 `__syncthreads()`——LOAD/STORE warp 正阻塞在各自的流水线等待点上，全块屏障会死锁（u3-l2）。

#### 4.2.3 源码精读

**（1）环数组声明**：

```cpp
AFragT ring_A_kr[PREFETCH];
SFragT ring_S_acc[2][PREFETCH];
float ring_g0[PREFETCH], ring_g1[PREFETCH];
```

三组环形寄存器缓冲：A 片段、两个列块各自的状态 C 片段、两个衰减标量。数组维度 `[PREFETCH]` 使环深度成为编译期常量。见 [csrc/smxx/fwd_kernel2.cuh:668-670](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L668-L670)。

**（2）预热循环**：

```cpp
for (int i = 0; i < PREFETCH; ++i) {
    Tensor kr_block = local_tile(k_restored_t, make_shape(Int<16>{}, Int<16>{}), make_coord(i, 0));
    copy(smem_tiled_copy_A_T, smem_thr_copy_A_T.partition_S(kr_block), tCrAi_kr_view);
    cute::transform(tCrAi_kr, ring_A_kr[i], cute::identity{});

    for (int bi = 0; bi < 2; ++bi) {
        Tensor s_block = local_tile(s_acc_T, make_shape(Int<16>{}, Int<16>{}), make_coord(i, warp_id * 2 + bi));
        copy(smem_tiled_load_C_T, smem_thr_load_C_T.partition_S(s_block), smem_thr_load_C_T.retile_D(ring_S_acc[bi][i]));
    }

    ring_g0[i] = g_total(i * 16 + group_id);
    ring_g1[i] = g_total(i * 16 + group_id + 8);
}
```

装填最初的 P 个 slot：`local_tile` 从转置视图切出 \((i, 0)\) 号 A 块与 \((i, \text{warp}\_id \times 2 + bi)\) 号状态块；A 片段经暂存 `tCrAi_kr` 中转再进环；衰减标量按 C 片段行分布 \(\{g, g+8\}\) 从 `g_total` 取值。见 [csrc/smxx/fwd_kernel2.cuh:672-686](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L672-L686)。

**（3）主循环——算 δs**：

```cpp
for (int m = 0; m < S_M_BLOCKS; ++m) {
    const int slot = m % PREFETCH;
    float g0 = ring_g0[slot];
    float g1 = ring_g1[slot];

    for (int bi = 0; bi < 2; ++bi) {
        clear(u_acc[bi]);
        gemm(thr_mma, ring_A_kr[slot](_,_,Int<0>{}), tCrB_u_arr[bi](_,_,Int<0>{}), u_acc[bi]);
    }
```

每个迭代消费环中 slot：`u_acc` 复用相位 1/3 的 fp32 累加器（先清零），对两个列块各做一次 HMMA，B 操作数直接取相位 4 留下的 `tCrB_u_arr`。见 [csrc/smxx/fwd_kernel2.cuh:688-699](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L688-L699)。

**（4）主循环——预取下一块 A 与标量**：

```cpp
    if (m + PREFETCH < S_M_BLOCKS) {
        Tensor kr_next = local_tile(k_restored_t, make_shape(Int<16>{}, Int<16>{}), make_coord(m + PREFETCH, 0));
        copy(smem_tiled_copy_A_T, smem_thr_copy_A_T.partition_S(kr_next), tCrAi_kr_view);
        cute::transform(tCrAi_kr, ring_A_kr[slot], cute::identity{});

        ring_g0[slot] = g_total((m + PREFETCH) * 16 + group_id);
        ring_g1[slot] = g_total((m + PREFETCH) * 16 + group_id + 8);
    }
```

在 gemm 消费完当前 A 片段之后，把第 \(m+P\) 块装回同一 slot，同时刷新衰减标量。见 [csrc/smxx/fwd_kernel2.cuh:701-708](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L701-L708)。

**（5）主循环——融合更新、写回、预取下一块 S**：

```cpp
    for (int bi = 0; bi < 2; ++bi) {
        for (int a = 0; a < 2; ++a) {
            for (int d = 0; d < 2; ++d) {
                auto c0 = make_coord(make_coord(a, 0), 0, d);
                auto c1 = make_coord(make_coord(a, 1), 0, d);
                ring_S_acc[bi][slot](c0) = BF16(bf16_to_f32(ring_S_acc[bi][slot](c0)) * g0 + u_acc[bi](c0));
                ring_S_acc[bi][slot](c1) = BF16(bf16_to_f32(ring_S_acc[bi][slot](c1)) * g1 + u_acc[bi](c1));
            }
        }

        Tensor s_block = local_tile(s_acc_T, make_shape(Int<16>{}, Int<16>{}), make_coord(m, warp_id * 2 + bi));
        copy(smem_tiled_store_C_T, smem_thr_store_C_T.retile_S(ring_S_acc[bi][slot]),
             smem_thr_store_C_T.partition_D(s_block));

        if (m + PREFETCH < S_M_BLOCKS) {
            Tensor s_next = local_tile(s_acc_T, make_shape(Int<16>{}, Int<16>{}), make_coord(m + PREFETCH, warp_id * 2 + bi));
            copy(smem_tiled_load_C_T, smem_thr_load_C_T.partition_S(s_next), smem_thr_load_C_T.retile_D(ring_S_acc[bi][slot]));
        }
    }
}
```

读-改-写的核心：C 片段 8 个元素按坐标 \(c_0/c_1\)（行 \(g\)/行 \(g+8\)）分别用 `g0`/`g1` 缩放并加上 fp32 的 \(\delta s\)，单条表达式 `* +` 由 nvcc 默认的 FMA 收缩（`-fmad=true`，配合 setup.py 的 `--use_fast_math`）编译成**一条 FFMA、一次舍入**，随后 `BF16()` 量化（4.3 详解）；接着 `STSM_T` 写回 \((m, j)\) 块、`LDSM_T` 预取 \((m+P, j)\) 块。见 [csrc/smxx/fwd_kernel2.cuh:710-731](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L710-L731)。

**（6）会师与流水线记账**：

```cpp
compute_barrier.arrive_and_wait();

cutlass::arch::fence_view_async_shared();
store_pipeline.producer_commit(out_write);
load_pipeline.consumer_release(load_read);
++load_read;
++out_write;
```

相位 6 是每 tile 的最后一个计算相位：4 个 MMA warp 会师后，打代理围栏、放行输出 stage（STORE warp 可 TMA 写 out）、放行输入 stage（LOAD warp 可复用缓冲），推进两条流水线的游标进入下一个 tile。见 [csrc/smxx/fwd_kernel2.cuh:733-741](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L733-L741)。

#### 4.2.4 代码实践

**实践目标**：写出相位 6 主循环的伪代码与环下标表（即 4.2.2 的表格由你独立复现），并完成 `PREFETCH=2` 的寄存器增量分析。

**操作步骤**：

1. 只看源码 [csrc/smxx/fwd_kernel2.cuh:659-731](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L659-L731)（先遮住本讲 4.2.2），在纸上写出 `PREFETCH=1` 与 `PREFETCH=2` 两种参数下、`m = 0..7` 每步"消费的块号 / 写入的 slot / 预取的块号"三行表。
2. 核对要点：预取块号是否恒为 \(m+P\)、slot 是否恒为 \(m \bmod P\)、最后 P 步是否正确地停止预取。
3. 做寄存器账本：先独立推导 `AFragT`/`SFragT` 的每线程元素数（提示：16×16×16 的 MMA 每 warp 覆盖 256 个 A 元素 / 256 个 C 元素，除以 32 lanes；bf16 两两打包成 u32），再对照 4.2.1 的表格验证是否得到 +14。
4. （可选，需改源码并重装）把 [csrc/smxx/fwd_kernel2.cuh:466](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L466) 的 `PREFETCH` 改为 2，重新编译并观察 `--ptxas-options=-v` 输出中 K2 的寄存器数变化，再跑 `tests/test_fwd.py` 与 `benchmarks/bench_fwd.py` 看正确性与性能。**注意**：这会修改源码，请在独立分支/副本上做，实验后还原。

**需要观察的现象**：伪代码表与 4.2.2 完全一致；寄存器账本得到 +14；（编译实验中）SASS 实际增量是否恰为 14、是否触发溢写、性能升或降。

**预期结果**：表格与账本可静态验证；SASS 实际增量受 ptxas 调度影响，可能不等于 14，性能方向也无法先验确定——**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`PREFETCH=1` 时环只有一个 slot，"预取"还有什么意义？

**答案**：单 slot 下环退化为一个缓冲，但预取指令（`LDSM_T`/标量装载）仍被插入在 HMMA 之后、FMA/STSM 之前——装载延迟由同一迭代后半段的独立指令覆盖，这仍是软件流水，只是深度为 1。

**练习 2**：`PREFETCH=2`、`S_M_BLOCKS=8` 时，哪些迭代不发生预取？为什么此时环中不会读到过期数据？

**答案**：守卫条件 `m + 2 < 8` 只在 \(m \le 5\) 成立，故 \(m=6,7\) 不预取。迭代 \(m\) 只消费 slot \(m \bmod 2\)，其内容要么来自预热（\(m \in \{0,1\}\)），要么来自迭代 \(m-2\) 的预取（写入 \((m-2)+2=m\) 号块到 slot \((m-2) \bmod 2 = m \bmod 2\)），链路严格衔接，不存在读到过期 slot 的路径。

**练习 3**：为什么 S 片段的预取（LDSM_T）必须放在 `STSM_T` 写回之后，而 A 片段的预取只需放在 gemm 之后？

**答案**：两者是同一条规则——"覆盖必须在消费之后"。`ring_S_acc[bi][slot]` 的消费包括 FMA 修改与 `STSM_T` 写回两步，写回完成后才算消费完毕；`ring_A_kr[slot]` 的消费只到 gemm 读取为止。

### 4.3 g_total 衰减融合：bf16 存储 + fp32 FMA 的精度取舍

#### 4.3.1 概念说明

状态更新链路上的每个量的精度都不是随手选的，而是一条严格的"舍入点链"：

| 量 | 存放位置 | dtype | 说明 |
| --- | --- | --- | --- |
| `s_acc`（状态本体） | smem（32 KB） | bf16 | 省一半 smem；两次更新之间才量化 |
| `g_total` → \(e^{g_{\text{total}}}\) | 输入 stage 的 smem | **fp32** | K1 已就地做过 `ex2`（u2-l7），到 MMA 手里已是乘性衰减因子，直接喂 FFMA |
| `k_restored`、`U` | smem / 寄存器 B 片段 | bf16 | HMMA 操作数边界 |
| \(\delta s\)（HMMA 累加器 `u_acc`） | 寄存器 | **fp32** | **不**量化回 bf16 就参与融合 |
| 融合结果 | 寄存器 | fp32 FFMA → 一次 `BF16()` | 单次舍入 |

三段式更新

\[
s_T^{\text{new}}(k, v) \;=\; \operatorname{bf16}\Big( \underbrace{e^{g_k} \cdot s_T(k,v)}_{\text{fp32 乘}} \;+\; \underbrace{\delta s(k, v)}_{\text{fp32 HMMA 累加}} \Big)
\]

中，乘法与加法由 FMA 收缩合并为**一条 FFMA 指令、只在最后舍入一次**（nvcc 默认 `-fmad=true`，setup.py 又叠加 `--use_fast_math`），`BF16()` 转换是本步唯一的量化点。bf16 → fp32 的反向读取用 PTX `cvt.f32.bf16`（见 [csrc/smxx/utils.cuh:55-59](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L55-L59)），无损。

deep-dive 第 3 节给出了这一取舍的官方论证（[docs/20260420-flashkda-v1-deep-dive.md:45-55](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/docs/20260420-flashkda-v1-deep-dive.md#L45-L55)）：

> 状态以 bf16 存储可将状态的共享内存占用近乎减半，并免除每个喂给状态的 bf16 GEMM 关键路径上的 fp32→bf16 转换；**只要状态更新本身用 fp32 FMA 指令完成**，在两次更新之间以 bf16 存储累加器，在推理基准上没有引入可测量的精度损失。

换句话说：误差不是逐条指令累积的，而是**每个 tile 只舍入一次**（更新结束时）。衰减因子 \(e^{g} \in (e^{-5 \cdot 16}, 1]\) 逐 tile 乘在旧状态上（\(\text{lower\_bound}=-5\)、CHUNK=16 的范围论证见 u3-l8），旧状态的量化误差随遗忘自然衰减，新写入项的误差只来自本轮的一次 bf16 舍入——这是"bf16 存储也够用"的结构性原因。

#### 4.3.2 核心流程

kernel 与 torch_ref 的舍入点逐一对齐：

```text
kernel（L718-719）                          torch_ref（L235-239）
─────────────────────────────────────      ─────────────────────────────────────
u_acc = HMMA(k_restored_t, U)   [fp32]  ↔  delta_s = mm(k_restored.t(), U, out_dtype=fp32)
BF16(f32(s)*g + u_acc)  [一条FFMA   ↔  fp32_fma(delta_s, state_f32.t(), g_exp)
                         +一次量化]          （fp64 计算后舍到 fp32 = 单次舍入）
                                            .to(torch.bfloat16)      [一次量化]
```

torch_ref 的 `fp32_fma` 用 fp64 中间精度模拟"单次舍入的 FMA"（[tests/torch_ref.py:55-59](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L55-L59)），`fp32_ex2_ftz` 复刻 `ex2.approx.ftz.f32` 的 FTZ 行为（[tests/torch_ref.py:47-52](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L47-L52)，kernel 侧原语在 [csrc/smxx/utils.cuh:38-42](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L38-L42)）。正是这些"舍入点级"的复刻，使 `tests/test_fwd.py` 的 `torch.equal` bit-exact 断言成为可能。

一个反例可以帮你看清"单次舍入"的分量：如果把更新写成两次量化

\[
\operatorname{bf16}\big(\operatorname{bf16}(s \cdot g) + \operatorname{bf16}(\delta s)\big)
\]

每次乘、加各引入一次 bf16 舍入（相对误差约 \(2^{-9}\) 量级每步），与 torch_ref 的单次舍入点不再对齐，`torch.equal` 立刻失败——bit-exact 测试会直接抓住这种"顺手"的改法。

#### 4.3.3 源码精读

**（1）g_total 的 fp32 存放**：

```cpp
alignas(128) cute::ArrayEngine<float, cute::cosize_v<GTotalLayout>> g_total;
```

输入 stage 里唯一的 fp32 数组成员（512 字节，128 个通道）：K1 已写入 \(e^{g_{\text{total}}}\)，K2 直接以 fp32 消费，不经过任何 bf16 量化。见 [csrc/smxx/fwd_kernel2.cuh:90](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L90)。

**（2）衰减因子的读取（环标量的来源）**：

```cpp
ring_g0[i] = g_total(i * 16 + group_id);
ring_g1[i] = g_total(i * 16 + group_id + 8);
```

预热时装载第 \(i\) 个 M 块对应的两行衰减因子（fp32 标量直接进寄存器）；主循环预取处按 \((m+P) \times 16\) 刷新（[csrc/smxx/fwd_kernel2.cuh:706-707](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L706-L707)）。见 [csrc/smxx/fwd_kernel2.cuh:684-685](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L684-L685)。

**（3）单次舍入的融合更新**：

```cpp
ring_S_acc[bi][slot](c0) = BF16(bf16_to_f32(ring_S_acc[bi][slot](c0)) * g0 + u_acc[bi](c0));
ring_S_acc[bi][slot](c1) = BF16(bf16_to_f32(ring_S_acc[bi][slot](c1)) * g1 + u_acc[bi](c1));
```

全 kernel 精度设计的浓缩：bf16 状态无损升到 fp32，与 fp32 衰减因子、fp32 HMMA 累加结果做一次 FMA（源码写成 `* +`，靠 FMA 收缩合成单条 FFMA、单次舍入），最后 `BF16()` 一次量化——与 torch_ref 的 `fp32_fma(...).to(bfloat16)` 舍入点一一对应。见 [csrc/smxx/fwd_kernel2.cuh:710-721](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L710-L721)。

**（4）官方论证**：

[docs/20260420-flashkda-v1-deep-dive.md:52-55](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/docs/20260420-flashkda-v1-deep-dive.md#L52-L55) 明确写道：只要更新本身用 fp32 FMA 完成，bf16 存储累加器在内部推理基准上无可测精度损失；配合 [docs/20260420-flashkda-v1-deep-dive.md:47-50](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/docs/20260420-flashkda-v1-deep-dive.md#L47-L50) 的动机（smem 减半 + 免除关键路径上的转换），构成本模块的完整论证。

#### 4.3.4 代码实践

**实践目标**：用一个自包含脚本体感「bf16 状态 + fp32 FMA 更新」与「全程 fp32 状态」的误差差，验证 deep-dive 的说法在你的随机数据上是否站得住。

**操作步骤**（示例代码，保存为 `phase6_precision.py`）：

```python
# 示例代码：状态更新精度对比 —— bf16 存储 + fp32 FMA vs 全 fp32
import torch
torch.manual_seed(0)
D, CHUNK, TILES = 128, 16, 256

k_r = torch.randn(TILES, CHUNK, D).to(torch.bfloat16)   # 每 tile 的 k_restored
U   = torch.randn(TILES, CHUNK, D).to(torch.bfloat16)
g   = -torch.rand(TILES, 1, D).float() * 5.0            # 模拟 (lower_bound, 0) 内的门控

def run(state_dtype):
    s = torch.zeros(D, D, dtype=state_dtype)
    for t in range(TILES):
        g_exp = torch.exp(g[t, 0])                      # fp32 衰减因子
        delta = k_r[t].float().t() @ U[t].float()       # fp32 δs
        if state_dtype == torch.bfloat16:
            # kernel 语义：fp64 中间量模拟单次舍入的 FMA，再量化 bf16
            s64 = torch.zeros(D, D, dtype=torch.float64)
            s_new = (s64 + s.double() * g_exp[:, None].double()
                     + delta.double()).float().to(torch.bfloat16)
        else:
            s_new = s * g_exp[:, None] + delta
        s = s_new
    return s

s_bf  = run(torch.bfloat16)
s_f32 = run(torch.float32)
rel = (s_bf.float() - s_f32).norm() / s_f32.norm()
print(f"TILES={TILES}  relative error = {rel.item():.3e}")
```

**需要观察的现象**：相对误差的量级（bf16 单步舍入约 \(2^{-9}\)，TILES 步后大约维持在 \(10^{-3}\) 量级还是持续增长）；把 `TILES` 改成 64/1024 再跑，误差是否随步数缓慢增长。

**预期结果**：相对误差应稳定在 bf16 舍入的量级（约 \(10^{-3}\)，随衰减有界），不会像"逐步双次量化"那样明显恶化——这与 deep-dive「无可测精度损失」一致。具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果把融合更新拆成 `BF16(BF16(s*g) + BF16(u_acc))`，测试会发生什么？为什么？

**答案**：`tests/test_fwd.py` 的 `torch.equal` bit-exact 断言会失败。kernel 侧从一次舍入变成三次（乘、加、以及 `u_acc` 的多余量化），与 torch_ref 在 [tests/torch_ref.py:239](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L239) 的单次舍入点不再对齐。

**练习 2**：`g_total` 到达 MMA warp 时是什么值、什么 dtype？为什么不量化成 bf16？

**答案**：是 \(e^{g_{\text{total}}}\)（K1 已就地做过 ex2），fp32。它逐 tile 乘在整个旧状态上，是最长寿命的乘性因子；量化它会给每个状态元素都注入同源偏差，而 fp32 只占 512 字节 smem + 每线程 2 个标量寄存器，没有省的必要。

**练习 3**：deep-dive 说 bf16 状态"无可测精度损失"，前提条件是什么？

**答案**：前提是**更新本身用 fp32 FMA 完成**（HMMA fp32 累加 + 单次舍入 FFMA），量化只发生在两次更新之间的存储边界；且结论来自内部推理基准的实证验证，不是先验证明（见 [docs/20260420-flashkda-v1-deep-dive.md:52-55](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/docs/20260420-flashkda-v1-deep-dive.md#L52-L55)）。

## 5. 综合实践

把本讲三个模块串成一个任务：**为相位 6 写一份"可执行的规格说明"**。

### 任务 A：伪代码与环下标表

不看本讲正文，只读 [csrc/smxx/fwd_kernel2.cuh:659-731](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L659-L731)，写出：

1. 含预热的完整伪代码（对齐 4.2.2 的结构，标注每步用的 copy atom / 指令类别）；
2. `PREFETCH=1` 与 `PREFETCH=2` 两张环下标随 `m` 变化的表；
3. 自查清单：预热装了几个 slot？预取写的是哪个 slot？最后几步为何停预取？`tCrB_u_arr` 在整个相位里装载了几次？

### 任务 B：PREFETCH=2 寄存器分析

推导 `PREFETCH` 1→2 的每线程寄存器增量（片段大小 → 打包 → 求和，预期 +14 个 u32），写清推导过程。可选加分项：在副本分支上真实改 `PREFETCH` 编译，用 `--ptxas-options=-v` 对比 K2 寄存器数与 smem 数，跑一次 benchmark 记录性能方向（预期可能变慢——寄存器压力与占用率的博弈），标注"待本地验证"。

### 任务 C：对照 torch_ref 验证数学等价

把 4.1.4 的 `phase6_views.py` 扩展成完整对拍脚本：

1. 路径 A 直接照抄 [tests/torch_ref.py:235-239](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L235-L239) 的更新行（fp32 执行，`g_exp` 用 `torch.exp`）；
2. 路径 B 按"4 warp × 8 M 块 × 2 列块"的划分模拟 kernel 的分块与转置视角；
3. 再叠加一层"kernel 数词语义"：把路径 B 的 FMA 用 fp64 中间量 + 单次舍入模拟（对齐 `fp32_fma`），量化点放在融合之后，比较两种路径的 bf16 结果；
4. 记录 `torch.equal` 与最大偏差，并解释任何非零差异的来源（提示：fp32 GEMM 的分块归约顺序；把路径 B 的 `delta_blk` 改为对整块 `delta` 切片应能消掉这类差异）。

**预期结果**：数学等价性（fp32 宽松对齐）必然成立；bit 级结果依赖平台的 GEMM 核选择——这正是"kernel 用固定归约序的 HMMA、而参考实现要逐 FMA 复刻"（u2-l1、u3-l1）的原因。把结论写进你的学习笔记 `notes/phase6.md`。

## 6. 本讲小结

- **转置的根源是形状**：状态更新 \(\delta s_T = k_{\text{restored}}^{\top} U\) 的 A 操作数行方向是 K 通道，与 smem 存放方向相反；由于相位 3 的 `MOVM_T` 已把 U 备好为 B 片段，转置代价被推给 smem 侧的 `k_restored` 与 `s_acc`——对同一块内存挂 `TransposedMMALayout`/`TransposedStateSmemLayout` 视图、用 `LDSM_T`/`STSM_T` 装写，转置在 smem↔寄存器的搬运途中完成，无任何数据搬动与额外缓冲。
- **转置视角的附赠**：逐通道衰减在 \((K, V)\) 视角下是逐行缩放，与 C 片段两行 \(\{g, g+8\}\) 的分布对齐，每线程每块只需 `ring_g0`/`ring_g1` 两个 fp32 标量。
- **预取环是深度 1 的软件流水**：`ring_A_kr`/`ring_S_acc`/`ring_g0/1` 按 `slot = m % PREFETCH` 消费、按 \((m+P) \bmod P = m \bmod P\) 回写，守卫 `m + PREFETCH < S_M_BLOCKS` 同时负责停预取与防 `g_total` 越界；`PREFETCH` 1→2 的静态寄存器增量为每线程 +14 个 u32。
- **精度链**：状态 bf16 存储（smem 减半）、\(e^{g_{\text{total}}}\) 与 HMMA 累加保持 fp32、融合更新收缩为一条 FFMA 单次舍入后一次量化——舍入点与 torch_ref 的 `fp32_fma(...).to(bfloat16)` 逐一对齐，是 bit-exact 测试的必要条件。
- **收尾同步**：相位 6 之后 `NamedBarrier(128)` 会师 4 个 MMA warp，再打代理围栏、放行 store/load 两条流水线；此处不能换 `__syncthreads()`（LOAD/STORE warp 在流水线等待点上，会死锁）。
- 相位 6 贡献每 warp 每 tile 16 次 gemm（占全程 52 次中的 16 次）——读状态（相位 1）与更状态（相位 6）是一对对称的固定开销，这是 KDA 递推在 K2 里的最终落点。

## 7. 下一步学习建议

- **u3-l6 状态输入输出路径**：本讲只更新 `state_acc`；状态的进出（bf16 直通 TMA、fp32 经 `state_fp32_buf` 转换、零初始化）是同一块 32 KB smem 的另外三条路径，其中 fp32↔bf16 的 smem 布局转换函数正好复用本讲的"同构 atom"思想。
- **u3-l7 STORE warp 与尾块处理**：本讲结尾放行的 out stage 如何被 TMA 写回、尾块为何退化为逐元素写，是 varlen 正确性的最后一环。
- **u3-l8 数值精度总账**：把本讲的舍入点链扩展到全 kernel（K1 的 ex2/cumsum、L 的 fp32 全程、CHUNK=16 的 bf16 范围论证），形成完整的精度地图。
- 想动手的读者可以把第 5 节任务 B 做完：`PREFETCH` 是全项目少数"一行改动即可实验"的编译期旋钮，配合 u3-l12 的消融流程（改一处 → 重编译 → exact-match 回归 → benchmark）正好走一遍完整的二次开发闭环。
