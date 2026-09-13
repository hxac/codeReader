# MMA 相位 1-5：双 GEMM、delta 修正与输出投影

## 1. 本讲目标

本讲逐相位精读 Kernel 2（`_flash_kda_fwd_recurrence`）中 4 个 MMA warp 的计算主体 Phase 1 到 Phase 5。学完本讲，你应该能够：

1. 说出每个 MMA warp 负责的两个 16×16 列块是如何划分的（N=128 / 4 warp = 32 = 2×16），并推导每个线程在 C 片段（accumulator fragment）中拥有哪些元素。
2. 为 Phase 1-5 中每条 `gemm` 调用列出 A/B/C 的矩阵形状、操作数来源（smem 还是寄存器）与累加精度。
3. 解释 `SM75_U32x1_MOVM_T`（movmatrix 转置）如何让 U 矩阵全程留在寄存器文件里、避免一次 smem 往返。
4. 理解 `SM80_16x8x16_F32BF16BF16F32_TN` atom 如何经 `Tile<_16,_16,_16>` 组装成每 warp 的 16×16×16 计算单元。
5. 把 kernel 的每个相位与 `tests/torch_ref.py` 中的对应语句逐一对号，理解每个 bf16 量化点为什么必须出现在那个位置（bit-exact 的关键）。

Phase 6（状态更新 `s_acc = s_acc·g_total + k_restoredᵀ@U`）是下一讲 u3-l5 的主题，本讲只在结尾做衔接性预告。

## 2. 前置知识

本讲假设你已读过以下讲义（只做最简回顾）：

- **u2-l1 chunk 化算法骨架**：每个 16-token tile 的计算序列。本讲反复对照的参考语句在 [tests/torch_ref.py:228-233](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L228-L233)：

  ```python
  v_chunk = v_chunk - torch.matmul(k_decayed, state_slice.t())   # 擦除项
  v_chunk = v_chunk * beta_val_bf16                              # 写入强度
  U = torch.matmul(INV, v_chunk)                                 # 解三角方程
  _out = torch.matmul(q_decayed, state_slice.t())                # 状态读出
  _out = _out + torch.matmul(Mqk, U)                             # 块内修正
  ```

  状态 \( S \in \mathbb{R}^{V \times K} \)（V=K=128），smem 中的 `s_acc` 就是它：第 0 维是 V（输出维），第 1 维是 K（收缩维）。
- **u3-l2 Kernel 2 架构**：192 线程 = 4 个 MMA warp + 1 个 LOAD warp + 1 个 STORE warp；MMA warp 是双流水线的中枢。本讲的代码全部位于 `warp_role == WarpRole::MMA` 分支内（[csrc/smxx/fwd_kernel2.cuh:426](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L426)）。线程数 192 = `32*2+128` 定义在 [csrc/smxx/fwd_launch.cu:186](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L186)。
- **u2-l4 CuTe 布局**：`MMALayout`（16×128，K_INTER swizzle）、`LMLayout`（16×16）、`StateSmemLayout`（128×128）等 smem 布局；K_INTER 布局天生适配 `ldmatrix` 直读。
- **u2-l8 / u3-l3 workspace 契约与 LOAD warp**：每个 tile 需要的 k_decayed/q_decayed/k_restored/g_total/INV/Mqk 六个 workspace 中间量外加 v、beta，已由 LOAD warp 预取进 `input[stage]` 三级缓冲；MMA warp 通过 `load_pipeline.consumer_wait` 拿到就绪的 stage。

还需要几个硬件级概念（初学者术语框）：

| 术语 | 含义 |
|---|---|
| HMMA | Ampere/Hopper Tensor Core 的 `mma.sync` 矩阵乘指令。`m16n8k16` 一条指令完成 16×8 输出、K=16 的收缩，A/B 为 bf16、C 为 fp32 |
| fragment（片段） | 一条 warp 指令的操作数在 32 个 lane 寄存器里的分布方式。A/B/C 各有自己的线程-元素所有权映射 |
| ldmatrix（LDSM） | SM75 起的 smem→寄存器批量搬运指令，按 8×8 子块一次性装载一个 warp 的操作数片段；`.trans` 变体装载转置视图 |
| stmatrix（STSM） | SM90 起的寄存器→smem 反向指令，与 ldmatrix 对偶 |
| movmatrix（MOVM_T） | SM75 起的 warp 内寄存器交换指令：把每 lane 持有的一个 u32（2 个相邻 b16 元素）按 8×8 子块整体转置重新分配所有权，不经过 smem |
| TN 布局 | CUTLASS 记号：A 是 M×K 行主、B 以 N×K 形式给出（即数学上的 \( C = A B^{\mathsf{T}} \) 被拆成逐 k 点积） |

本讲数学公式统一用：\( C[m,n] = \sum_k A[m,k] \cdot B[n,k] \)。

## 3. 本讲源码地图

| 文件 | 本讲涉及部分 | 作用 |
|---|---|---|
| [csrc/smxx/fwd_kernel2.cuh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh) | L425-L743 的 MMA warp 区间；其中 **L533-L657 是本讲主体 Phase 1-5**，L659-L731 是 Phase 6（下一讲） | Kernel 2 递推主体 |
| [csrc/smxx/utils.cuh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh) | L50-L53（sigmoid 近似）、L146-L165（同款 atom/Tile 的单 warp GEMM 参考）、L304-L309（MOVM_T 的另一处用法） | 公共工具：数值近似、MMA 封装 |
| [tests/torch_ref.py](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py) | L216-L241 | bit-exact 参考实现，Phase 1-5 的逐语句对照物 |
| [csrc/smxx/fwd_launch.cu](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu) | L29-L30、L186-L199 | kInputStages=3 / kOutputStages=2、192 线程等启动常量（背景） |

一句话定位：**MMA warp 每个 tile 要用 5 个相位完成 torch_ref L228-L233、L241 的全部输出侧计算，并把 U 以 B 操作数片段的形式留给 Phase 6。**

## 4. 核心概念与源码讲解

先给出总览。每个 tile 内（[csrc/smxx/fwd_kernel2.cuh:434](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L434) 的 `for (int t = 0; t < t_tiles; ++t)`），MMA warp 先 `store_pipeline.producer_acquire` 拿输出 stage、`load_pipeline.consumer_wait` 等输入 stage 就绪（L436-L439），然后为当前 stage 建 9 个 smem 张量视图（v、beta、out、六个 workspace 量、s_acc，L445-L458），进入相位流水：

| 相位 | 计算 | 数学形式 | 对应 torch_ref |
|---|---|---|---|
| Phase 1 | 双 GEMM（8 个 k 块 × 2 个列块 × 2 个 GEMM） | \( u^{(0)} = k_{dec} S^{\top},\quad out^{(0)} = q_{dec} S^{\top} \) | L228 的 matmul 与 L232 的 matmul |
| Phase 2 | out 转 bf16 存寄存器；LDSM 载 v 与 INV；激活 beta | — | L221、L232 的输出量化 |
| Phase 3 | delta 修正 + 寄存器转置 + INV@u | \( u = (v - u^{(0)}) \odot \beta_{\text{tok}},\quad U = INV \cdot u \) | L228-L231 |
| Phase 4 | Mqk@U 累加到 out | \( out = out^{(0)} + Mqk \cdot U \) | L233 |
| Phase 5 | out_bf16 写 smem out tile | — | L241（写 gmem 由 STORE warp 完成） |

源码里对这段的官方注释（[csrc/smxx/fwd_kernel2.cuh:460-462](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L460-L462)）一针见血：

```cpp
// Fused MMA: v_sub, v_beta, U=INV@v, out=q@s, out+=Mqk@U, s_acc_update
// Each warp handles TWO 16x16 column blocks (N=128 / 4 warps = 32 = 2 x 16)
// U stays in registers via SM75_U32x1_MOVM_T (no smem round-trip)
```

### 4.1 MMA 基础设施：atom 组装、线程映射与寄存器片段

#### 4.1.1 概念说明

在读相位代码之前必须先弄清三件事：**每个 warp 的 MMA 是什么形状、每个线程拥有哪些数据、寄存器片段有哪几类**。这相当于先读懂「机床的规格」，再看「加工工序」。

关键事实：

1. **每 warp 的计算单元是 16×16×16**。`SM80_16x8x16_F32BF16BF16F32_TN` 是一条 `mma.sync.m16n8k16` 指令（bf16 输入、fp32 累加），`Tile<_16,_16,_16>` 把 N 方向铺 2 份，组装成每 warp 一次 `gemm` 调用完成 16×16 输出、K=16 收缩的单元。
2. **4 个 warp 按 N（V 维）切列**：每 warp 负责列块 `warp_id*2` 与 `warp_id*2+1`，即列区间 \([32w,\ 32w+32)\)。M=16（token）不切分，K=128 由 8 个 k 块串行收缩。
3. **C 片段每线程 8 个值**（16×16/32）。按 PTX 的 m16n8k16 标准布局，lane \(\ell\) 拥有：

\[ \text{C 片段：lane } \ell \text{ 拥有行}\ \{g,\ g+8\}\ \text{与列}\ \{2r,\ 2r{+}1,\ 8{+}2r,\ 8{+}2r{+}1\},\quad g = \ell/4,\ r = \ell \bmod 4 \]

   其中行 \(g\) 与 \(g+8\) 分别对应 tile 内 token 序号 \(g\) 与 \(g+8\)——这正是后面 beta 用 `group_id` 和 `group_id+8` 两个下标取值的依据。
4. **寄存器片段分三类**：fp32 累加器（`AccFragT`，8 个 float）、bf16 存储片段（`SFragT`，8 个 bf16 = 4 个 u32）、bf16 操作数片段（A 片段 `AFragT` / B 片段 `BFragT_u`）。同一段寄存器换个视角就是不同片段——Phase 3 的 MOVM_T 本质就是在两种视角之间搬运。

#### 4.1.2 核心流程

```text
进入 MMA 分支
  ├─ 构造 NamedBarrier(128)（4 个 MMA warp 的会师点）
  ├─ for t in 0..t_tiles:              # 每个 tile
  │    ├─ store_pipeline.producer_acquire / load_pipeline.consumer_wait
  │    ├─ 建 smem 视图（9 个张量）
  │    ├─ make_tiled_mma(atom, Layout<1,1>, Tile<16,16,16>)   ← 每 warp 的 16×16×16 单元
  │    ├─ warp_id / lane_id / group_id                        ← 线程映射
  │    ├─ 构造 7 组 tiled copy（LDSM_N / LDSM_T / STSM_N / STSM_T）
  │    ├─ 分配寄存器片段（暂存 × 操作数双份 + 4 个累加器数组）
  │    └─ Phase 1 → 2 → 3 → 4 → 5 → 6（本讲 1-5）
  └─ （Phase 6 之后）compute_barrier.arrive_and_wait → 释放 stage
```

#### 4.1.3 源码精读

MMA 的组装在 [csrc/smxx/fwd_kernel2.cuh:468-478](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L468-L478)：`make_tiled_mma` 用 `Tile<_16,_16,_16>` 把 16×8×16 的 atom 沿 N 铺成 16×16；`Layout<Shape<_1,_1>>` 表示不在 warp 维度复制（列块划分靠各 warp 自己的 `local_tile` 坐标实现，而不是靠 atom 堆叠）；`mma.get_slice(lane_id)` 得到本 lane 的 ThrMMA 视图。

```cpp
auto mma = make_tiled_mma(
    MMA_Atom<SM80_16x8x16_F32BF16BF16F32_TN>{},
    Layout<Shape<_1,_1>>{},
    Tile<_16,_16,_16>{}
);
const int warp_id = compute_tid / 32;
const int lane_id = compute_tid % 32;
const int group_id = (lane_id / 4) % 8;
ThrMMA thr_mma = mma.get_slice(lane_id);
```

线程映射三件套在 [csrc/smxx/fwd_kernel2.cuh:474-476](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L474-L476)：`group_id` 就是上文 PTX 布局的行组号 \(g\)（lane<32 时 `lane/4` 已在 0..7，`%8` 是防御式写法）。

随后是 7 组 smem↔寄存器拷贝 atom，[csrc/smxx/fwd_kernel2.cuh:481-502](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L481-L502)：

| 变量 | atom | PTX 指令 | 用途（本讲/下一讲） |
|---|---|---|---|
| `smem_tiled_copy_A` | `SM75_U32x4_LDSM_N` | ldmatrix.x4 | A 操作数 ← K_INTER smem：k_decayed、q_decayed、**INV、Mqk** |
| `smem_tiled_copy_A_T` | `SM75_U16x8_LDSM_T` | ldmatrix.trans | A 操作数 ← 转置视图：k_restored_t（Phase 6） |
| `smem_tiled_copy_B` | `SM75_U32x4_LDSM_N` | ldmatrix.x4 | B 操作数 ← s_acc |
| `smem_tiled_load_C` | `SM75_U32x4_LDSM_N` | ldmatrix.x4 | **v 以 C 片段布局载入寄存器**（Phase 2） |
| `smem_tiled_store_C` | `SM90_U32x4_STSM_N` | stmatrix.x4 | out_bf16 写 smem out tile（Phase 5） |
| `smem_tiled_load_C_T` / `store_C_T` | LDSM_T / STSM_T | ldmatrix/stmatrix.trans | Phase 6 的状态访问 |

注意 `make_tiled_copy_C` 同时被用来做「载入 v」和「写出 out」——C 拷贝的线程-值映射与累加器片段一致，这是 Phase 3 能做纯寄存器逐元素运算的前提。

寄存器片段的分配在 [csrc/smxx/fwd_kernel2.cuh:504-531](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L504-L531)。每类操作数都备了**两份**：暂存片段（`tCrAi_k`、`tCrAi_q`、`tCrBi`，LDSM 的写入目的地）与操作数片段（`tCrA_k`、`tCrA_q`、`tCrB`，HMMA 的读取来源），两者之间用 `cute::transform(..., cute::identity{})` 搬运：

```cpp
Tensor tCrAi_k = make_fragment_like<BF16>(thr_mma.partition_fragment_A(A_ref));  // 暂存
auto tCrA_k = thr_mma.partition_fragment_A(A_ref);                               // 操作数
...
AccFragT u_acc[2], out_acc[2];                    // 4 个 fp32 累加器（2 列块 × 2 个 GEMM）
for (int i = 0; i < 2; ++i) { u_acc[i] = thr_mma.make_fragment_C(tCrC_ref); clear(u_acc[i]); }
for (int i = 0; i < 2; ++i) { out_acc[i] = thr_mma.make_fragment_C(tCrC_ref); clear(out_acc[i]); }
```

`[2]` 这个数组长度就是「每 warp 两个 16×16 列块」的直接体现。`partition_fragment_A` 返回的是「形状已知、内容未定义」的片段原型，`make_fragment_like<BF16>` 才真正分配 bf16 寄存器存储。

另外注意 [csrc/smxx/fwd_kernel2.cuh:133](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L133) 的 `__launch_bounds__(NumThreads)` 只约束线程数不约束最小 occupancy——与 K1 的 `__launch_bounds__(256, 8)`（压寄存器换占用）不同，K2 允许每个 MMA warp 持有较多寄存器片段，这正是 warp 专用化的交换空间。

#### 4.1.4 代码实践

**纸面推演：列块分配表。**

1. 实践目标：亲手算出 4 个 MMA warp 各自负责的输出/状态/U 列块，验证「N=128/4 warp=32=2×16」。
2. 操作步骤：对 `warp_id ∈ {0,1,2,3}`、`i ∈ {0,1}`，填出 `local_tile` 坐标 `warp_id*2+i`、对应 out/state/U 的 16 列区间；再对 lane 0、lane 5、lane 31 三条线程，按 4.1.1 的公式写出它们在各自 warp 第一个 16×16 列块中拥有的 (行, 列) 集合。
3. 需要观察的现象：每个 warp 的列区间互不重叠且并集恰为 [0,128)；lane 的行集合都是 \(\{g, g+8\}\) 两行。
4. 预期结果（参考答案）：

| warp_id | 列块坐标 (warp_id*2, warp_id*2+1) | 列区间 |
|---|---|---|
| 0 | 0, 1 | [0,16), [16,32) |
| 1 | 2, 3 | [32,48), [48,64) |
| 2 | 4, 5 | [64,80), [80,96) |
| 3 | 6, 7 | [96,112), [112,128) |

   lane 0：\(g=0,r=0\) → 行 {0,8} × 列 {0,1,8,9}；lane 5：\(g=1,r=1\) → 行 {1,9} × 列 {2,3,10,11}；lane 31：\(g=7,r=3\) → 行 {7,15} × 列 {6,7,14,15}。

#### 4.1.5 小练习与答案

**练习 1**：如果把 4 个 MMA warp 改成 8 个（kComputeThreads=256），列块映射要怎么改？只改 `warp_id*2` 够吗？

答案：8 warp 时每 warp 1 个 16 列块（128/8=16），映射改为 `warp_id*1` 并删去 `[2]` 数组的第二维。但改动不止于此：`kK2Threads`、NamedBarrier 的 128 线程数、两条 pipeline 的 `num_consumers`（[csrc/smxx/fwd_kernel2.cuh:205](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L205)）都要联动——这正是 warp 专用化架构的耦合成本。

**练习 2**：为什么 `make_tiled_mma` 的第二个参数是 `Layout<Shape<_1,_1>>`（不在 warp 间复制 atom），而不是把 4 个 warp 拼成 64×64 的大 atom？

答案：列块划分靠每个 warp 用 `local_tile` 坐标 `(warp_id*2+i)` 自取数据实现，4 个 warp 互不通信、各算各的 16×16；atom 内 32 线程已覆盖一个 16×16 C 片段。拼大 atom 需要跨 warp 的片段交换（shuffle 或 smem），反而破坏「每 warp 独立 + 寄存器本地性」。

**练习 3**：`group_id = (lane_id / 4) % 8` 里的 `% 8` 是不是多余的？

答案：lane<32 时 `lane/4 ∈ [0,8)`，`%8` 恒为恒等，功能上多余；它表达了「行组号对 8 取模」的语义意图（16 行 tile 的上下两半各 8 行）。可对照 utils.cuh 中 8×8 求逆的行号写法 `tid & 7`（[csrc/smxx/utils.cuh:236](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L236)）。

### 4.2 Phase 1：双 GEMM k_decayed@s 与 q_decayed@s（K 分块与操作数预取）

#### 4.2.1 概念说明

Phase 1 一口气算两个矩阵积：

\[ u^{(0)} = k_{dec} S^{\top} \in \mathbb{R}^{16 \times 128}, \qquad out^{(0)} = q_{dec} S^{\top} \in \mathbb{R}^{16 \times 128} \]

- \(u^{(0)}\) 是「擦除项」：当前状态对这些 key 已经记住了什么（torch_ref L228 的 matmul 部分）。
- \(out^{(0)}\) 是「读出项」：直接从状态读出的输出底座（torch_ref L232）。

**「双 GEMM」的含义**：两个积共享同一个 B 操作数（s_acc 的 tile）。每个 s_acc 块只 LDSM 一次、供两条 HMMA 消费，smem 读带宽减半——这是把 torch_ref 两条独立 matmul 融合成一个循环的根本动机。

**K 分块**：收缩维 K=128 拆成 `K_BLOCKS = 128/16 = 8` 步，每步一次 16×16×16 的 `gemm`。fp32 累加贯穿全部 8 步，**只在 Phase 2/3 出口量化一次 bf16**——与 torch bf16 matmul「内部 fp32 累加、输出单次舍入」的语义一致，这是 bit-exact 的第一个前提。

**寄存器双缓冲**：暂存片段（LDSM 目的地）与操作数片段（HMMA 来源）分离，让下一块的 ldmatrix 可以与当前块的 mma 重叠调度。

#### 4.2.2 核心流程

```text
K_BLOCKS = 128 / 16 = 8
预装载: A_k 块(0,0)、A_q 块(0,0)、B 块(n0=warp_id*2, 0)          # LDSM×3
for k = 0 .. 7:
    暂存 → 操作数: tCrA_k、tCrA_q、tCrB(n0)                        # transform(identity)×3
    LDSM: B 块(n1=warp_id*2+1, k) → tCrBi                          # 提前装载第二个列块
    gemm(A_k, B(n0)) → u_acc[0];  gemm(A_q, B(n0)) → out_acc[0]    # 列块 0 的双 GEMM
    暂存 → 操作数: tCrB(n1)
    若 k+1 < 8: LDSM: A_k 块(0,k+1)、A_q 块(0,k+1)、B 块(n0,k+1)   # 预取下一 k 步
    gemm(A_k, B(n1)) → u_acc[1];  gemm(A_q, B(n1)) → out_acc[1]    # 列块 1 的双 GEMM
```

每个 k 步 4 条 `gemm`（2 个 GEMM 问题 × 2 个列块）、4 次 LDSM；每条 `gemm` 内部是 2 条 `m16n8k16` HMMA（Tile 在 N 方向铺 2 份 atom）。

#### 4.2.3 源码精读

`K_BLOCKS` 的推导与预装载在 [csrc/smxx/fwd_kernel2.cuh:533-541](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L533-L541)：`size<1>(k_decayed)` 是 16×128 矩阵的第 1 维 128，除以 16 得 8；三个预装载的 `local_tile` 坐标分别是 (0,0)、(0,0)、(warp_id*2, 0)——注意 B 的坐标第一维是 N（V）列块、第二维才是 k 块。

主循环在 [csrc/smxx/fwd_kernel2.cuh:543-568](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L543-L568)，关键四条 gemm 与预取的交错：

```cpp
#pragma unroll
for (int k = 0; k < K_BLOCKS; ++k) {
    cute::transform(tCrAi_k, tCrA_k, cute::identity{});   // 暂存 → 操作数
    cute::transform(tCrAi_q, tCrA_q, cute::identity{});
    cute::transform(tCrBi, tCrB, cute::identity{});

    copy(smem_tiled_copy_B, ..., local_tile(s_acc, ..., make_coord(warp_id * 2 + 1, k)), tCrBi_view);

    gemm(thr_mma, tCrA_k(_,_,Int<0>{}), tCrB(_,_,Int<0>{}), u_acc[0]);    // k_decayed @ s
    gemm(thr_mma, tCrA_q(_,_,Int<0>{}), tCrB(_,_,Int<0>{}), out_acc[0]);  // q_decayed @ s

    cute::transform(tCrBi, tCrB, cute::identity{});

    if (k + 1 < K_BLOCKS) { /* LDSM A_k(0,k+1)、A_q(0,k+1)、B(n0,k+1) */ }

    gemm(thr_mma, tCrA_k(_,_,Int<0>{}), tCrB(_,_,Int<0>{}), u_acc[1]);
    gemm(thr_mma, tCrA_q(_,_,Int<0>{}), tCrB(_,_,Int<0>{}), out_acc[1]);
}
```

几处细节：

- `(_,_,Int<0>{})` 的第三个下标是 K 方向的 tile 模式，`Tile<16,16,16>` 下恒为 0，写出来说明片段是三层嵌套坐标。
- **B 的 LDSM 插在两组 gemm 之间**（L549-550）：列块 0 在算时，列块 1 的 B 正在装载——循环内也在做双缓冲，不只是跨 k 步。
- A 操作数与列块无关（M=16、K=16 的块只依赖 k），所以每 k 步只装载一次、两个列块共用；B 与列块绑定，每 k 步装载两次。
- `u_acc` 与 `out_acc` 跨 8 个 k 步持续累加，全程 fp32；量化点在下游相位。

顺带对照 utils.cuh 里的同款组装：[csrc/smxx/utils.cuh:146-165](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L146-L165) 的 `mma_m16n16_bf16bf16bf16_1warp` 用完全相同的 atom + `Tile<_16,_16,_16>` 封装了单 warp 的 16×16 GEMM（K1 构造 L/Mqk 时使用），可以互相印证这套组装是项目的「标准件」。

#### 4.2.4 代码实践

**纸面推演：展开 Phase 1 的前两个 k 步。**

1. 实践目标：把 4.2.2 的伪代码落实为具体坐标，确认每条 LDSM/gemm 的块位置。
2. 操作步骤：取 `warp_id=2`，写出 k=0 与 k=1 两轮中：每条 LDSM 的 `local_tile` 坐标（A_k、A_q、B 两种列块）、每条 gemm 的 (A 块, B 块, 目的累加器)。
3. 需要观察的现象：k=1 轮开头执行的 LDSM 其实是 k=0 轮尾部预取的那批（坐标含 k+1=1）；k=7 轮尾部没有预取。
4. 预期结果（参考答案，warp_id=2，n0=4、n1=5）：

| 轮次 | 动作 | 块坐标 / 目的 |
|---|---|---|
| k=0 开工前 | LDSM×3 | A_k(0,0)、A_q(0,0)、B(4,0) |
| k=0 中 | LDSM×1 | B(5,0) |
| k=0 尾 | LDSM×3 | A_k(0,1)、A_q(0,1)、B(4,1) |
| k=0 gemm | 4 条 | (A_k,B4)→u_acc[0]、(A_q,B4)→out_acc[0]、(A_k,B5)→u_acc[1]、(A_q,B5)→out_acc[1] |
| k=1 中 | LDSM×1 | B(5,1) |
| k=1 尾 | LDSM×3 | A_k(0,2)、A_q(0,2)、B(4,2) |

   k=7（最后一轮）尾部因 `k+1 < K_BLOCKS` 为假，无预取。

#### 4.2.5 小练习与答案

**练习 1**：Phase 1 中每个 s_acc 的 16×16 块被 LDSM 几次？被几条 gemm 消费？

答案：1 次 LDSM、2 条 gemm（k_decayed 与 q_decayed 共用）。这就是「双 GEMM」省 smem 带宽的含义：torch_ref 的两条 matmul 若各自独立实现，B 要读两遍。

**练习 2**：如果去掉「暂存片段/操作数片段」的分离，让 LDSM 直接写进 `tCrA_k`，功能还正确吗？会损失什么？

答案：功能等价（两片段类型相同，transform 是恒等搬运）。损失的是寄存器级双缓冲：LDSM 的写入目的地与 HMMA 的读取来源变成同一组寄存器，编译器难以把下一块的 ldmatrix 提前到当前 mma 之前发射，装载延迟无法与计算重叠——是性能问题而非正确性问题。

**练习 3**：`K_BLOCKS` 的值从哪个张量的哪个维度推出来？若 D=64 它是多少？

答案：`size<1>(k_decayed)/16 = D/16`（[csrc/smxx/fwd_kernel2.cuh:534](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L534)）；D=64 时为 4。但支持 D=64 需要改布局、workspace、MMA 分块全链路（见 u3-l12），远不止这一个常数。

### 4.3 Phase 2 → Phase 3：delta 修正 (v−u)·β 与寄存器内转置求 U=INV@u

#### 4.3.1 概念说明

Phase 2 是三个短准备的集合，Phase 3 完成 delta 规则的核心修正：

\[ u = (v - u^{(0)}) \odot \beta_{\text{tok}}, \qquad U = INV \cdot u \]

其中 \(\odot \beta_{\text{tok}}\) 表示每行（每个 token）乘各自的 beta。对应 torch_ref L228-L231 三条语句。

两个精妙设计：

1. **v 以 C 片段布局装载**（Phase 2 用 `smem_tiled_load_C` 而不是 A/B 拷贝）。这样 `v_bf16` 与 `u_bf16`（由 `u_acc` 量化而来）是**同一种线程所有权**，逐元素减法/乘法在各自寄存器里原地完成，零 shuffle、零 smem。
2. **U 不落 smem**。计算 \(U = INV \cdot u\) 需要 u 从「C 片段所有权」重排成「B 操作数片段所有权」。传统做法是写回 smem 再 ldmatrix；这里用 `movmatrix`（MOVM_T）在寄存器文件内完成所有权交换——源码注释里的 "U stays in registers ... (no smem round-trip)" 即指此。

数值边界：`(v − u) * beta` 是两次独立的 bf16 运算（各舍入一次），与 torch bf16 逐元素运算的舍入点一致；`INV @ u` 的 fp32 累加结果量化一次 bf16，对应 torch matmul 的 bf16 输出。

#### 4.3.2 核心流程

```text
Phase 2（三个准备）:
  out_bf16[i]   = BF16(out_acc[i])              # 读出项量化，留在寄存器
  v_bf16[i]     = LDSM_N(v 块(0, warp_id*2+i))  # v 以 C 布局进寄存器
  tCrA_k        = LDSM_N(INV)                   # A 操作数切换为 INV（寄存器复用！）
  beta0/beta1   = BF16(sigmoid(beta_tile[off+group_id / off+group_id+8]))

Phase 3（对 i = 0,1 两个列块）:
  u_bf16[i]     = BF16(u_acc[i])                # 擦除项量化
  u_bf16[i]     = (v_bf16[i] - u_bf16[i]) * beta_row    # 逐元素，C 布局内
  u_b_regs[0..3] = MOVM_T(u_bf16[i] 的 4 个 u32)         # 寄存器内转置成 B 片段
  tCrB_u_tmp     ← u_b_regs                     # 按位塞进 B 片段
  clear(u_acc[i]); gemm(INV, u) → u_acc[i]      # U = INV @ u（fp32 累加）
  u_bf16[i]     = BF16(u_acc[i])                # U 量化为 bf16
```

#### 4.3.3 源码精读

Phase 2 在 [csrc/smxx/fwd_kernel2.cuh:570-587](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L570-L587)。注意 `tCrA_k` 的角色切换——Phase 1 装的是 k_decayed，这里被 INV 覆盖（同一组寄存器的复用）：

```cpp
SFragT out_bf16[2];
for (int i = 0; i < 2; ++i)
    cute::transform(out_acc[i], out_bf16[i], [] __device__ (float x) { return BF16(x); });

SFragT v_bf16[2];
for (int i = 0; i < 2; ++i) {
    Tensor v_block = local_tile(v_tile, make_shape(Int<16>{}, Int<16>{}), make_coord(0, warp_id * 2 + i));
    copy(smem_tiled_load_C, smem_thr_load_C.partition_S(v_block), smem_thr_load_C.retile_D(v_bf16[i]));
}

copy(smem_tiled_copy_A, smem_thr_copy_A.partition_S(INV), tCrAi_k_view);
cute::transform(tCrAi_k, tCrA_k, cute::identity{});

BF16 beta0 = BF16(sigmoid_tanh_approx_f32(float(beta_tile(beta_smem_offset + group_id))));
BF16 beta1 = BF16(sigmoid_tanh_approx_f32(float(beta_tile(beta_smem_offset + group_id + 8))));
```

beta 的两个下标是理解 C 片段的关键：`group_id` 与 `group_id+8` 正是本 lane 拥有的两行 token（4.1.1 的 \(g\) 与 \(g+8\)）。`beta_smem_offset` 是 1D TMA 向下对齐（`&~7`）留下的偏移，消费端用 `&7` 取回真实起点（[csrc/smxx/fwd_kernel2.cuh:447](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L447)，承接 u3-l3）。**beta 的 sigmoid 在这里、以 tanh 近似完成**（[csrc/smxx/utils.cuh:50-53](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L50-L53)），随后量化 bf16——对应 torch_ref L220-L221 的 `beta_val_bf16`。

Phase 3 主体在 [csrc/smxx/fwd_kernel2.cuh:589-623](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L589-L623)。逐元素修正用嵌套坐标访问片段，`c0`（行 \(g\)）乘 beta0、`c1`（行 \(g{+}8\)）乘 beta1：

```cpp
auto c0 = make_coord(make_coord(a, 0), 0, d);   // 行 group_id
auto c1 = make_coord(make_coord(a, 1), 0, d);   // 行 group_id + 8
u_bf16[i](c0) = (v_bf16[i](c0) - u_bf16[i](c0)) * beta0;
u_bf16[i](c1) = (v_bf16[i](c1) - u_bf16[i](c1)) * beta1;
```

从代码行为可以直接反推出片段坐标语义：M 模式的第二个子模式区分上下半行（beta0/beta1 各管一半），`a` 与 `d` 联合给出每半行里的 4 个列位置（每线程 8 值 = 2 行 × 4 列）。

接着是本讲的招牌动作——MOVM_T 寄存器转置（[csrc/smxx/fwd_kernel2.cuh:608-620](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L608-L620)）：

```cpp
uint32_t* u_c = reinterpret_cast<uint32_t*>(&u_bf16[i](0));   // 8 个 bf16 = 4 个 u32
SM75_U32x1_MOVM_T::copy(u_c[0], u_b_regs[0]);
SM75_U32x1_MOVM_T::copy(u_c[1], u_b_regs[1]);
SM75_U32x1_MOVM_T::copy(u_c[2], u_b_regs[2]);
SM75_U32x1_MOVM_T::copy(u_c[3], u_b_regs[3]);

auto tCrB_u_tmp = thr_mma.partition_fragment_B(B_ref);
uint32_t* b_dst = reinterpret_cast<uint32_t*>(&tCrB_u_tmp(0));
b_dst[0] = u_b_regs[0]; b_dst[1] = u_b_regs[1];
b_dst[2] = u_b_regs[2]; b_dst[3] = u_b_regs[3];

clear(u_acc[i]);
gemm(thr_mma, tCrA_k(_,_,Int<0>{}), tCrB_u_tmp(_,_,Int<0>{}), u_acc[i]);   // U = INV @ u
```

`movmatrix.sync.aligned.m8n8.trans.b16` 每次转置一个跨 warp 分布的 8×8 b16 子块（每 lane 一个 u32）；16×16 片段含 4 个 8×8 子块，故恰好 4 次调用。转置前 u_bf16 是「C 片段所有权」，转置后按位塞进 `partition_fragment_B` 的壳里就成了合法的 B 操作数——**数据始终没离开寄存器文件**。同一手法在 utils.cuh 的 16×16 求逆模块也出现（[csrc/smxx/utils.cuh:304-309](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L304-L309) 的 `transpose_u32x4`），两处互为印证。

最后 [csrc/smxx/fwd_kernel2.cuh:622](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L622) 把 fp32 的 U 量化回 `u_bf16[i]`。

#### 4.3.4 代码实践

**对号入座：torch_ref ↔ kernel 行号映射。**

1. 实践目标：不看书，把 torch_ref L228-L231 四条语句映射到 kernel 的具体相位与行号。
2. 操作步骤：遮住本讲 4.3.3，只打开 [tests/torch_ref.py:227-231](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L227-L231) 与 [csrc/smxx/fwd_kernel2.cuh:570-623](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L570-L623)，逐条填写映射表。
3. 需要观察的现象：torch 的一条语句往往拆在两个相位里（GEMM 部分与逐元素部分分离）；量化点数量必须一致。
4. 预期结果（参考答案）：

| torch_ref 语句 | kernel 位置 |
|---|---|
| L228 `v_chunk - matmul(k_decayed, Sᵀ)` | GEMM 部分在 Phase 1（L543-L568 累加 u_acc）；减法在 Phase 3 L603-L604 |
| L229 `* beta_val_bf16` | Phase 3 L603-L604（同一行的 `* beta0/beta1`） |
| L231 `U = matmul(INV, v_chunk)` | Phase 2 装 INV（L583-L584）+ Phase 3 gemm（L619-L620）+ 量化（L622） |
| （L220-L221 beta 激活） | Phase 2 L586-L587 |

   可选加深（需 SM90 机器，待本地验证）：跑 `pytest tests/test_fwd.py`，确认这套相位映射下 kernel 与 torch_ref 达到 `torch.equal` 级 bit-exact。

#### 4.3.5 小练习与答案

**练习 1**：beta 的 sigmoid 为什么在 K2 的 Phase 2 做，而 K1 里乘进 L 的 beta 却是 fp32？两个 kernel 是不是重复计算了激活？

答案：不是重复。K1 与 K2 是两个独立 kernel：K1 以 fp32 beta 乘 L（求逆种子的精度要求，见 u3-l1）；K2 直接从 gmem 用 1D TMA 读原始 beta logits（不经过 workspace），在 Phase 2 激活并量化 bf16，对应 torch_ref L221 的 `beta_val_bf16`。同一数学量、两条精度路径，各自服务不同用途。

**练习 2**：`(v − u) * beta` 里的减法与乘法各发生一次 bf16 舍入，这与 torch_ref 的哪两行严格对应？为什么这不会破坏 bit-exact？

答案：对应 L228（减）与 L229（乘）。cutlass `bfloat16_t` 的运算符是「升 fp32 计算、RNE 舍回 bf16」，与 PyTorch CUDA 上 bf16 逐元素运算的舍入语义一致，两个舍入点位置相同，故 bit-exact。

**练习 3**：Phase 3 末尾为什么要把 U 量化回 bf16 的 `u_bf16`，而不是保留 fp32 给 Phase 4/6 用？

答案：一是对应 torch_ref L231 matmul 的 bf16 输出（舍入点必须复刻）；二是 U 的下游身份是两次 HMMA 的 B 操作数（Phase 4 的 Mqk@U、Phase 6 的 k_restored_t@U），而 HMMA 的 B 操作数本来就是 bf16——量化不是损失，是接口要求。

### 4.4 Phase 4 → Phase 5：Mqk@U 输出累加、STSM 写回与相位收尾

#### 4.4.1 概念说明

Phase 4 完成输出的块内修正项，Phase 5 把结果写回 smem：

\[ out = out^{(0)} + Mqk \cdot U \]

数值上必须**两次独立量化再相加**：\(out^{(0)}\) 在 Phase 2 已量化 bf16；\(Mqk \cdot U\) 在 fp32 累加后单独量化 bf16（对应 L648），然后做一次 bf16 加法（L649）——精确复刻 torch_ref L233 `_out + torch.matmul(Mqk, U)` 的两个舍入点。若图省事在 fp32 里一次累加再量化，舍入点变少，exact-match 测试立刻失败。

Phase 5 用 `stmatrix`（STSM_N）把 `out_bf16` 从 C 片段直接写进 smem 的 out tile。**写的布局必须与 STORE warp 的 TMA 描述符严格一致**（u2-l8 的位一致契约）：out tile 用 `VOLayout`（K_INTER swizzle 的 16×128），TMA 描述符按由它派生的 `TMAVOLayout` 编码，STSM 的目标也按同一 `MMALayout` 家族 partition——三者咬合，K2 写下的比特即 TMA 读走的比特。

Phase 4 还有一个承上启下的细节：它把 U **再次** MOVM_T 转置进持久数组 `tCrB_u_arr[2]`——因为 Phase 3 转置的是 GEMM 前的 u，而 Phase 4 需要的是 GEMM 后的 U（`u_bf16` 已在 L622 被覆盖为 U）；这个数组将一直活到 Phase 6，作为状态更新的 B 操作数。

#### 4.4.2 核心流程

```text
Phase 4:
  tCrA_k ← LDSM_N(Mqk)                      # A 操作数第三次换岗：k_decayed → INV → Mqk
  for i = 0,1:
    MOVM_T(u_bf16[i]) → tCrB_u_arr[i]       # U 转成 B 片段（持久，供 Phase 6 复用）
    clear(out_acc[i]); gemm(Mqk, U) → out_acc[i]
    gemm_bf16 = BF16(out_acc[i])            # 修正项量化
    out_bf16[i] = out_bf16[i] + gemm_bf16   # bf16 加法（第二次舍入）

Phase 5:
  for i = 0,1:
    STSM_N(out_bf16[i]) → out_tile 块(0, warp_id*2+i)

收尾（tile 循环体末尾）:
  compute_barrier.arrive_and_wait()          # 4 个 MMA warp 会师
  fence_view_async_shared()                  # generic 写 → async 代理可见
  store_pipeline.producer_commit(out_write)  # 通知 STORE warp 输出就绪
  load_pipeline.consumer_release(load_read)  # 归还输入 stage
```

#### 4.4.3 源码精读

Phase 4 在 [csrc/smxx/fwd_kernel2.cuh:625-650](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L625-L650)。先换 A 操作数为 Mqk，再对两个列块完成「转置 → GEMM → 两次量化相加」：

```cpp
copy(smem_tiled_copy_A, smem_thr_copy_A.partition_S(Mqk), tCrAi_k_view);
cute::transform(tCrAi_k, tCrA_k, cute::identity{});

BFragT_u tCrB_u_arr[2];                        // U 的持久 B 片段（Phase 6 还要用）
for (int i = 0; i < 2; ++i) {
    /* MOVM_T: u_bf16[i] 的 4 个 u32 → u_b_regs，塞进 tCrB_u_arr[i] */
    clear(out_acc[i]);
    gemm(thr_mma, tCrA_k(_,_,Int<0>{}), tCrB_u_arr[i](_,_,Int<0>{}), out_acc[i]);   // Mqk @ U

    SFragT gemm_bf16;
    cute::transform(out_acc[i], gemm_bf16, [] __device__ (float x) { return BF16(x); });
    cute::transform(out_bf16[i], gemm_bf16, out_bf16[i],
                    [] __device__ (BF16 c, BF16 a) { return c + a; });
}
```

三参数版 `cute::transform(dst_src, src, dst, f)` 在这里原地更新 `out_bf16`。

Phase 5 在 [csrc/smxx/fwd_kernel2.cuh:652-657](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L652-L657)，两条 STSM 写各自的列块：

```cpp
for (int i = 0; i < 2; ++i) {
    Tensor out_block = local_tile(out_tile, make_shape(Int<16>{}, Int<16>{}), make_coord(0, warp_id * 2 + i));
    copy(smem_tiled_store_C, smem_thr_store_C.retile_S(out_bf16[i]), smem_thr_store_C.partition_D(out_block));
}
```

相位收尾在 [csrc/smxx/fwd_kernel2.cuh:733-741](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L733-L741)（中间隔着 Phase 6，L659-L731，下一讲精读）：`NamedBarrier(128)` 让 4 个 MMA warp 会师后统一做代理围栏与两条 pipeline 的提交/释放。用 `__syncthreads()` 代替会把 LOAD/STORE warp 也卷进屏障、与流水线等待点互相等待而死锁（u3-l2 已分析过）。

从 torch_ref 视角，Phase 4/5 对应 L233 与 L241（写 gmem 的 TMA/逐元素分支属 STORE warp，见 u3-l7）。

#### 4.4.4 代码实践

**纸面审计：数一数每 warp 每 tile 的 GEMM 开销。**

1. 实践目标：统计 Phase 1-6（含预告的 Phase 6）每 warp 每 tile 的 `gemm` 调用数与 HMMA 指令数，体会相位间的计算配比。
2. 操作步骤：数源码中的 gemm 调用点——Phase 1（L552-L553、L566-L567，各在 8 次 k 循环内执行）、Phase 3（L620，i 循环 2 次）、Phase 4（L645，2 次）、Phase 6（L698，`S_M_BLOCKS`=8 次 m 循环 × 2 个 bi）；每条 gemm = 2 条 m16n8k16 HMMA。
3. 需要观察的现象：Phase 1 占了六成以上的 GEMM；Phase 3/4 各只有 2 条。
4. 预期结果（参考答案）：32 + 2 + 2 + 16 = **52 次 gemm**、**104 条 HMMA** 每 warp 每 tile。Phase 1 : Phase 6 = 2 : 1，可见「双 GEMM 读状态」与「状态更新」是对称的两大开销，这也是 u3-l5 要给 Phase 6 单独开流水线预取的原因。

#### 4.4.5 小练习与答案

**练习 1**：Phase 4 为什么必须 `clear(out_acc)` 后另起炉灶，而不是直接累加进 Phase 1 的 fp32 `out_acc`？

答案：torch_ref L233 是「两个各自量化 bf16 的积相加」：\(out^{(0)}\) 的量化发生在 Phase 2（对应 L232 matmul 的 bf16 输出），\(Mqk \cdot U\) 的量化发生在 L648（对应 L233 matmul 的 bf16 输出），L649 再做 bf16 加法。若在 fp32 里一次累加，会合并掉两个舍入点，数值不再 bit-exact——正确的优化也不能破坏这一契约。

**练习 2**：Phase 5 的 STSM 若把 out 写进普通（无 swizzle）行主布局的 smem，会破坏什么？

答案：STORE warp 的 TMA 描述符按 `TMAVOLayout`（由 K_INTER swizzle 的 `VOLayout` 派生）编码；smem 位模式一变，TMA 搬到 gmem 的数据就乱了。位一致契约要求「MMA 写 out 的布局 = TMA 描述符的布局」，这也是 u2-l4 强调 K_INTER 是「TMA 与 LDSM/STSM 共用的规范排布」的原因。

**练习 3**：`tCrB_u_arr` 里的 U 为什么能在 Phase 4 和 Phase 6 之间安全地长期驻留寄存器？

答案：Phase 4 与 Phase 6 之间没有别的代码写它；中间 Phase 5 只写 smem。Phase 6 消费它时（L698 的 gemm B 操作数），U 的 bf16 值正是 torch_ref L231 的 `U`（L235 的 `delta_s = k_restored.t() @ U` 用的同一矩阵）——寄存器里的数据流与参考实现的变量生命周期一一对应。

## 5. 综合实践

**任务：撰写 `phases_shapes.md`——Phase 1-5 的 GEMM 形状与来源总账，并与 torch_ref 对号。**

1. **实践目标**：用一张表把本讲全部 GEMM 调用的形状、来源、精度固定下来，作为日后读 Phase 6、做性能分析或二次开发的「账本」。
2. **操作步骤**：
   - 通读 [csrc/smxx/fwd_kernel2.cuh:533-657](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L533-L657)，为每条 `gemm` 记录：数学含义、A 的来源与形状、B 的来源与形状、C 累加器 dtype、结果去向。
   - 在 [tests/torch_ref.py:227-241](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L227-L241) 中找到每行的对应语句。
   - 保存为个人学习笔记 `phases_shapes.md`（放在你自己的笔记目录，不放仓库）。
3. **需要观察的现象**：A/B 操作数要么「smem 经 LDSM 进寄存器」要么「纯寄存器（MOVM_T 转置而来）」，没有一条 gemm 直接读 smem；fp32 累加器 `u_acc`/`out_acc` 被多个相位反复回收复用。
4. **预期结果**（参考答案表，可直接核对）：

| 相位（源码行） | gemm 的数学含义 | A：来源 × 形状 | B：来源 × 形状 | C 累加 | 结果去向 |
|---|---|---|---|---|---|
| P1（L552/L566） | \(k_{dec} S^{\top}\) 的第 k 块 | 寄存器（LDSM_N ← smem k_decayed），16×16 | 寄存器（LDSM_N ← smem s_acc），16×16 | `u_acc[i]` fp32，跨 8 个 k 块累加 | P3 量化 bf16 |
| P1（L553/L567） | \(q_{dec} S^{\top}\) 的第 k 块 | 寄存器（LDSM_N ← smem q_decayed），16×16 | 同上（**共享 B**） | `out_acc[i]` fp32 | P2 量化 bf16 |
| P3（L620） | \(U = INV \cdot u\)，列块 i | 寄存器（LDSM_N ← smem INV），16×16 | 寄存器（MOVM_T ← `(v−u)·β` 的 C 片段），16×16 | `u_acc[i]` fp32（先 clear） | L622 量化 bf16 → `u_bf16[i]` |
| P4（L645） | \(Mqk \cdot U\)，列块 i | 寄存器（LDSM_N ← smem Mqk），16×16 | 寄存器（MOVM_T ← `u_bf16[i]`），16×16 | `out_acc[i]` fp32（先 clear） | 量化 bf16 后加进 `out_bf16[i]` |
| P5（L652-L657） | （无 gemm）out 写回 | — | — | — | STSM_N → smem out tile 块 (0, warp_id*2+i) |

   torch_ref 对号：`v−u`（L228）↔ P1 的 `u_acc` + P3 的减法；`*β`（L229）↔ P3 的 beta0/beta1 乘；`U=INV@u`（L231）↔ P3 的 gemm；`out` 的两项（L232/L233）↔ P1 的 `out_acc` 与 P4；最终写 out（L241）↔ P5（gmem 侧由 STORE warp 完成）。
5. 若想在真机上核验这份账本：跑 `bash tests/test.sh`（或 `pytest tests/test_fwd.py`），exact-match 通过即证明各量化点与参考实现逐位一致（需 SM90 机器，待本地验证）。

## 6. 本讲小结

- **每 warp 两个 16×16 列块**：4 个 MMA warp 按 V 维（N=128）切列，warp w 负责列块 `2w` 与 `2w+1`；C 片段每线程 8 值，行 \(\{g, g+8\}\)（\(g = lane/4\)）——beta 的双下标取值即由此而来。
- **Phase 1 双 GEMM**：\(k_{dec} S^{\top}\) 与 \(q_{dec} S^{\top}\) 共享 B 操作数（s_acc 每块一次 LDSM 两条 HMMA），K=128 拆 8 块串行收缩，暂存/操作数双片段实现寄存器级双缓冲；fp32 全程累加、出口单次量化。
- **Phase 3 是 delta 规则的寄存器独角戏**：v 以 C 布局装载使 `(v−u)·β` 零 shuffle；`movmatrix`（MOVM_T）在寄存器文件内把 C 片段转成 B 片段，U 全程不落 smem。
- **Phase 4/5 精度与布局双契约**：两次独立量化再 bf16 相加（复刻 torch_ref 的舍入点）；STSM 写 out 的布局必须与 STORE warp 的 TMA 描述符位一致。
- **寄存器复用贯穿始终**：`tCrA_k` 三次换岗（k_decayed → INV → Mqk），`u_acc`/`out_acc` 多相位回收，`tCrB_u_arr` 把 U 送达 Phase 6。
- 每 warp 每 tile 共 52 次 `gemm`（104 条 HMMA），其中 Phase 1 占 32、Phase 6 占 16——读状态与更状态是对称双开销。

## 7. 下一步学习建议

下一讲 **u3-l5《MMA 相位 6：状态更新的寄存器转置与预取环》**接着本讲最后一米：`s_acc = s_acc·g_total + k_restoredᵀ@U` 如何用 `TransposedMMALayout`/`TransposedStateSmemLayout` 双视图 + LDSM_T/STSM_T 完成「寄存器文件内转置」，`PREFETCH=1` 的 ring_A_kr/ring_S_acc 预取环如何隐藏 Phase 6 的装载延迟，以及 g_total 衰减与 HMMA 结果的 fp32 FMA 融合。建议先自己带着本讲的 `tCrB_u_arr`（U 的 B 片段）去读 [csrc/smxx/fwd_kernel2.cuh:659-731](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L659-L731)，再对照下一讲。若想巩固本讲的 MOVM_T 直觉，可回头重读 u3-l1 中 16×16 求逆的合并路径（`transpose_u32x4`），那里的转置服务于 A 操作数、这里服务于 B 操作数，互为镜像。
