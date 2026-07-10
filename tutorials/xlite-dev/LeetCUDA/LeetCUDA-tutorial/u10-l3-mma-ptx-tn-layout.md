# MMA m16n8k16 PTX 与 TN 布局

> 本讲对应大纲 `u10-l3`，承接 `u10-l2`（HGEMM naive sliced-K 与 pack LDST）。
> 本次为 `update`：`notes-v2.cu` 的 Phase 7b-2（`hgemm_mma_stages_tn` 统一循环版）与 Phase 7b-3（swizzle）补充了大量注释，本讲据此新增对 **warp 2x4 layout 的 col-major 排列**、**ldmatrix.x4/x2 的线程参与规则**、**prefetch loop 已预加载 K_STAGE-1 个 stage**、**epilogue 中 RC0/RC1 寄存器含义**以及 **swizzle 与 TMA SWIZZLE_32B 联系**的讲解。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说出 **MMA `m16n8k16` PTX 指令**的作用，以及它相对 `u10-l1` 的 **WMMA `m16n16k16`** API 在抽象层次、输出规模、寄存器数量上的区别。
2. 解释 **TN 布局**（A 行优先、B 列优先）在 BLAS 语义、全局索引、smem 存储、ldmatrix 选择上的完整含义。
3. 读懂 `hgemm_mma_stages_tn` 的 **统一循环版 multistage pipeline** 结构，并说清 `ldmatrix.x4` 需要 32 线程参与、`ldmatrix.x2` 只需前 16 线程参与的原因。
4. 理解 MMA `m16n8k16` 的 **C fragment 寄存器排布**，以及 epilogue 如何用 `__shfl_sync` + 128-bit store 把累加器写回 global memory。

## 2. 前置知识

在进入本讲前，你需要先掌握以下概念（来自前置讲义）：

- **线程层次与 SIMT**（`u2-l1`）：warp 是 32 线程的最小执行单位。MMA/ldmatrix 都是 **warp 级指令**，32 个线程协同完成一次操作。
- **共享内存与 bank**（`u2-l2`、`u7-l2`）：Tensor Core 数据要先落到 smem，再由 ldmatrix 装进寄存器；smem 的 32 bank 划分决定了是否产生 bank conflict。
- **cp.async 与 multistage**（`u9-l3`）：用 `cp.async` 异步搬运 + 多 stage 缓冲区隐藏访存延迟。
- **WMMA 入门**（`u10-l1`）：知道 `nvcuda::wmma` 的 `load_matrix_sync / mma_sync / store_matrix_sync` 三段式，以及 `m16n16k16` 半精度 fragment 的概念。
- **HGEMM sliced-K 与 pack**（`u10-l2`）：知道手写 Tensor Core 路径要自己管理寄存器、pack 加载、sliced-K 累加。

本讲会把 WMMA 那层「高层封装」剥掉，直接使用底层的 **PTX 内联汇编**（`mma.sync`、`ldmatrix`、`cp.async`），因此会大量出现 `asm volatile(...)` 的写法。你不需要预先会写汇编，本讲会逐条解释。

几个会用到的术语：

- **MMA（Matrix Multiply-Accumulate）**：Ampere（sm_80+）Tensor Core 的 warp 级矩阵乘累加指令。
- **ldmatrix**：配套的 smem→寄存器 加载指令，专门为喂给 MMA 而设计。
- **fragment（片段）**：一个矩阵在 warp 内 32 个线程寄存器里的分布式存储，每个线程只持有矩阵的一部分。
- **op(A)/op(B)**：BLAS 对矩阵「是否转置」的标记，N=Normal（不转置），T=Transposed（转置）。

## 3. 本讲源码地图

本讲涉及两个核心源码文件：

| 文件 | 作用 |
| --- | --- |
| [kernels/interview/notes-v2.cu](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/interview/notes-v2.cu) | 单文件学习笔记。**Phase 7b-1** 定义 MMA PTX 宏，**Phase 7b-2** 是「统一循环版」`hgemm_mma_stages_tn` kernel（本讲主角），**Phase 7b-3** 是带 swizzle 的版本。 |
| [kernels/hgemm/mma/basic/hgemm_mma_stage_tn.cu](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/hgemm/mma/basic/hgemm_mma_stage_tn.cu) | 可独立编译的同款 TN kernel，用「三段式循环」（prefetch + 主循环 + 尾循环），含 `main()` 自测入口。 |

> 阅读顺序建议：先看 notes-v2.cu 的 **Phase 7b-1 宏定义**（理解指令），再看 **Phase 7b-2 统一循环 kernel**（理解数据流），最后翻 hgemm_mma_stage_tn.cu 对照「三段式」写法。两者的 **kernel 计算逻辑完全一致**，差别只在循环怎么组织。

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

1. **MMA m16n8k16 PTX 指令与宏定义**（对应 Phase 7b-1）
2. **TN 布局语义与 ldmatrix 加载**（含本次新增的 ldmatrix 线程参与注释）
3. **统一循环版 multistage pipeline**（对应 Phase 7b-2 kernel 主体）
4. **C fragment 寄存器排布与 epilogue**（含本次新增的 RC0/RC1 注释）

### 4.1 MMA m16n8k16 PTX 指令与宏定义

#### 4.1.1 概念说明

`u10-l1` 里我们用的是 **WMMA**（`nvcuda::wmma`），它是一层 C++ 封装：你声明 `wmma::fragment`，调 `mma_sync`，库帮你管寄存器。问题是它**不透明**——你不太清楚一条指令到底算了多大一块、寄存器怎么排、能不能选 A/B 的行列主序。

本讲下沉到 **PTX 汇编层**，直接用 `mma.sync.aligned.m16n8k16` 指令。它的含义是一条 warp 指令完成：

\[ C[16\times 8] = A[16\times 16] \times B[16\times 8] + C[16\times 8] \]

也就是 **M=16, N=8, K=16** 的矩阵乘累加。与 WMMA `m16n16k16` 的对比：

| 维度 | WMMA `m16n16k16` | MMA PTX `m16n8k16` |
| --- | --- | --- |
| 单条指令输出 | \(16\times16=256\) 元素 | \(16\times8=128\) 元素 |
| 单条指令 FMA 数 | \(16\cdot16\cdot16=4096\) | \(16\cdot8\cdot16=2048\) |
| 关系 | 高层封装 | **2 条 `m16n8k16` = 1 条 `m16n16k16`**（WMMA 在底层就是拆成 2 条 m16n8k16） |
| A fragment 寄存器 | 由 fragment 类型隐藏 | 显式 4 个 `uint32` |
| B fragment 寄存器 | 隐藏 | 显式 2 个 `uint32` |
| C/D fragment 寄存器 | 隐藏 | 显式 2 个 `uint32` |
| 布局选择 | 受限 | 可显式选 `row.col` 等 |

> 直觉：**MMA PTX 是 Tensor Core 的「原生指令粒度」**，WMMA `m16n16k16` 是把它两两打包后的高层 API。下沉到 PTX 后，我们能精细控制寄存器分配、累加精度（如 `f16.f16.f16.f16` 用 fp16 累加）和 A/B 的行列主序，这正是后续 HGEMM/FlashAttention 高性能实现的基础。

每个线程持有的寄存器数量可以由规模反推：

- A 是 \(16\times16=256\) 个 half，32 线程分担 → 每线程 \(256/32=8\) 个 half = **4 个 `uint32`**（每个 `uint32` 装 2 个 half）。
- B 是 \(16\times8=128\) 个 half → 每线程 4 个 half = **2 个 `uint32`**。
- C/D 是 \(16\times8=128\) 个 half → 每线程 4 个 half = **2 个 `uint32`**。

#### 4.1.2 核心流程

一条 `mma.sync.aligned.m16n8k16` 指令的执行：

1. warp 内 32 线程**锁步**执行（`.sync.aligned`）。
2. 每个线程从自己的寄存器里读取 A 片段（4 个 `uint32`）、B 片段（2 个 `uint32`）、C 片段（2 个 `uint32`）。
3. 硬件 Tensor Core 完成 \(16\times8\times16\) 乘累加，把结果写回 D 片段（2 个 `uint32`，通常和 C 复用同一对寄存器做「原地累加」）。
4. 指令后缀 `row.col` 表示 **A 按 row-major 提供、B 按 col-major 提供**——这决定了 ldmatrix 该怎么排数据。

#### 4.1.3 源码精读

Phase 7b-1 把 PTX 指令包成 C 宏。最核心的是 `HMMA16816`：

[notes-v2.cu:1311-1317](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/interview/notes-v2.cu#L1311-L1317) 把 `mma.sync.aligned.m16n8k16.row.col.f16.f16.f16.f16` 包成 `HMMA16816(RD0,RD1, RA0..RA3, RB0,RB1, RC0,RC1)`，参数顺序严格对应 PTX 的 `{D}, {A}, {B}, {C}`：2 个输出 + 4 个 A 寄存器 + 2 个 B 寄存器 + 2 个累加器。

注意后缀的四个类型 `f16.f16.f16.f16` 分别是 **A/B/C/D** 的类型：这里 A、B、累加都是 fp16（`CUBLAS_COMPUTE_16F` 与之对应）。Phase 7b 头部对此有说明：

[notes-v2.cu:1305-1310](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/interview/notes-v2.cu#L1305-L1310) 解释了 `row.col`（A row-major、B col-major）、累加精度选择，以及「2 个输出寄存器 + 4 个 A + 2 个 B」的配比。

配套的 gmem→smem 拷贝宏 `cp.async`（[notes-v2.cu:1271-1280](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/interview/notes-v2.cu#L1271-L1280)）和 smem→寄存器加载宏 `ldmatrix`（下一节细讲）共同构成了「搬数据 + 喂数据 + 算」的三件套。

#### 4.1.4 代码实践

**实践目标**：建立「指令规模 ↔ 寄存器数量」的直觉。

**操作步骤**：

1. 打开 [notes-v2.cu:1305-1317](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/interview/notes-v2.cu#L1305-L1317)。
2. 数一数 `HMMA16816` 的参数：`RD0,RD1`（2）+ `RA0..RA3`（4）+ `RB0,RB1`（2）+ `RC0,RC1`（2）。
3. 对照本讲 4.1.1 的表格，验证「2+4+2+2」与 A/B/C 规模的反推是否一致。

**预期结果**：每个线程参与一次 MMA 时，恰好动用 \(4+2+2=8\) 个 `uint32` 寄存器（A/B/C 合计），与本讲推导一致。这就是为什么后续 kernel 里 `RA[i][4]`、`RB[j][2]`、`RC[i][j][2]` 这么声明。

#### 4.1.5 小练习与答案

**练习 1**：为什么 WMMA `m16n16k16` 的一条指令 FMA 数是 MMA `m16n8k16` 的两倍？

**答案**：WMMA `m16n16k16` 输出 \(16\times16\)，MMA `m16n8k16` 输出 \(16\times8\)；同样的 K=16 下，输出元素数是两倍，故 FMA 数也是两倍（\(16\cdot16\cdot16\) vs \(16\cdot8\cdot16\)）。底层 WMMA 用 2 条 `m16n8k16` 实现。

**练习 2**：`HMMA16816` 宏里为什么 A 是 4 个寄存器、B 只有 2 个？

**答案**：A 规模 \(16\times16=256\) half / 32 线程 = 8 half/线程 = 4 个 `uint32`；B 规模 \(16\times8=128\) half / 32 线程 = 4 half/线程 = 2 个 `uint32`。寄存器数正比于矩阵元素数。

---

### 4.2 TN 布局语义与 ldmatrix 加载

#### 4.2.1 概念说明

**TN 布局**是本讲第二个重点，也是面试高频考点。它来自 BLAS 的 `op(A) × op(B)` 约定：

- 第一个字母描述 **A**，第二个字母描述 **B**。
- **N（Normal）**：列优先（column-major），是 BLAS（源自 Fortran）的原生格式。
- **T（Transposed）**：行优先（row-major），相对 BLAS 是「转置过的」。

所以 **TN** = A 行优先（T）、B 列优先（N）。在 row-major 视角下，等价描述为：

\[ C[M\times N] = A[M\times K]_{\text{row-major}} \times B^T[N\times K]_{\text{row-major}} \]

也就是 **B 以其转置 \(B^T\) 按行优先存储**（等价于 B 本身按列优先存储）。这种布局的好处是：MMA 指令后缀 `row.col`（A row-major、B col-major）正好天然匹配——**ldmatrix 加载 B 时无需 `.trans`**。

**ldmatrix** 是配套的 smem→寄存器加载指令，格式 `ldmatrix.sync.aligned.xN.m8n8.shared.b16`，N∈{1,2,4} 表示一次加载几个 8×8 矩阵。关键语义（**本次 diff 重点补充**）：

- 一个 8×8 矩阵需要 **8 个线程**各提供一个行地址（每个线程给出某一行的起始地址）。
- 因此 **x1 用 8 线程、x2 用 16 线程、x4 用 32 线程**，多余线程提供的地址被忽略。

这正是本次新增注释里「`ldmatrix.x4` 需要 warp 内 32 线程都参与，`ldmatrix.x2` 仅前 16 线程参与」的由来。

#### 4.2.2 核心流程

加载 A 的一个 \(16\times16\) 片段（`ldmatrix.x4`）：

1. 一个 \(16\times16\) 块 = 4 个 8×8 矩阵 → 需要 32 个行地址 → **32 线程全部参与**。
2. 32 线程分成两组：前 16 线程（lane 0–15）给出 16 行 × 第一个 8 列段的地址，后 16 线程（lane 16–31）给出 16 行 × 第二个 8 列段的地址。
3. 每个线程最终拿到 4 个 `uint32`（A fragment）。

加载 B 的一个 \(8\times16\) 片段（`ldmatrix.x2`，B 是 \(K\times N\)，这里取 K 维 16、N 维 8 的一块）：

1. 一个 \(8\times16\) 块按 smem 里 \(B^T\) 的 row-major 看，是「2 个 8×8」→ 需要 16 个行地址 → **前 16 线程参与，后 16 线程地址被忽略**。
2. 因为 smem 存的是 \(B^T\) row-major，逐行加载直接得到 B 的列 → 天然 col-major 的 B fragment，匹配 `row.col`，**不需要 `.trans`**。

#### 4.2.3 源码精读

Phase 7b 头部对 TN 布局有详尽说明：

[notes-v2.cu:1225-1252](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/interview/notes-v2.cu#L1225-L1252) 解释 BLAS 的 T/N 语义、TN = A row-major / B col-major、`cublasGemmEx(..., CUBLAS_OP_T, CUBLAS_OP_N, ...)` 的对应关系，以及为什么 TN 下 ldmatrix 无需 `.trans`、`mma...row.col` 天然匹配。

宏定义里，`LDMATRIX_X4`/`LDMATRIX_X2`（[notes-v2.cu:1286-1295](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/interview/notes-v2.cu#L1286-L1295)）分别对应 x4（装 16×16 的 A）和 x2（装 8×16 的 B）。还有一个带 `.trans` 的 `LDMATRIX_X2_T`（[notes-v2.cu:1299-1303](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/interview/notes-v2.cu#L1299-L1303)），它**不在 TN kernel 里用**，而是留给 NN 布局或 FlashAttention 中 P@V（V 需 col-major）这类场景。

kernel 中的实际加载代码（本次 diff 在此补充了线程参与注释）：

[notes-v2.cu:1467-1482](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/interview/notes-v2.cu#L1467-L1482) 是 A 的 `ldmatrix.x4`。新增注释点明：

- `warp_m {0,1}` 把 8 个 warp 在 M 方向分两组，warp_m_0 覆盖行偏移 {0,16,32,48}，warp_m_1 覆盖 {64,80,96,112}；
- `lane_smem_a_m = warp_smem_a_m + lane_id % 16`（lane 0–15 与 16–31 都映射到行 0–15），`lane_smem_a_k = (lane_id/16)*8`（lane 0–15 取列段 0、16–31 取列段 8），**32 线程各提供一个不重叠的地址**，正好覆盖 4 个 8×8（即 16×16）。

[notes-v2.cu:1484-1502](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/interview/notes-v2.cu#L1484-L1502) 是 B 的 `ldmatrix.x2`。新增注释（[notes-v2.cu:1490-1494](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/interview/notes-v2.cu#L1490-L1494)）说明：

- `warp_n {0,1,2,3}` 把 N 方向分四组，偏移分别是 {0,8,16,24}/{32,40,48,56}/{64,72,80,88}/{96,104,112,120}；
- `lane_smem_b_n = ... + lane_id % 8`，`lane_smem_b_k = ((lane_id/8)%2)*8`，于是 **lane 0–15 提供所需的 16 个地址（2 个 8×8），lane 16–31 的地址被硬件忽略**——这就是「x2 只用前 16 线程」。

#### 4.2.4 代码实践

**实践目标**：手推一次 ldmatrix 的线程→地址映射，验证「x4 用 32 线程、x2 用 16 线程」。

**操作步骤**：

1. 假设 `warp_m=0, warp_n=0, lane_id=20`。
2. 算 A 加载：`lane_smem_a_m = 0 + 20%16 = 4`，`lane_smem_a_k = (20/16)*8 = 8`。地址有效且不重复。
3. 算 B 加载：`lane_smem_b_n = 0 + 20%8 = 4`，`lane_smem_b_k = ((20/8)%2)*8 = (2%2)*8 = 0`。但 lane 20 ≥ 16，按注释其地址会被忽略——与 lane 4（`n=4,k=0`）给出的地址完全相同。

**需要观察的现象**：lane 20 在 B 加载时给出的地址与 lane 4 重复，硬件只用前 16 线程的 16 个地址（正好 2 个 8×8 矩阵）。

**预期结果**：A 加载每个 lane 都贡献唯一地址（32 个地址 = 4 个 8×8 = 16×16）；B 加载只有 lane 0–15 的 16 个地址生效（2 个 8×8 = 8×16）。这与注释一致。

#### 4.2.5 小练习与答案

**练习 1**：TN 布局下，A 和 B 在 global memory 里分别是什么主序？

**答案**：A 是 **row-major**（\(A[m*K+k]\)）；B 以 \(B^T\) row-major 存储（\(B^T[n*K+k]\)，等价于 B col-major）。内维（K 维）在两者里都是连续的。

**练习 2**：为什么 TN kernel 加载 B 用 `LDMATRIX_X2` 而不是 `LDMATRIX_X2_T`？

**答案**：smem 里存的是 \(B^T\) row-major，逐行 ldmatrix 直接得到 B 的列（col-major fragment），正好匹配 `mma...row.col` 的 B 输入要求，无需 `.trans`。`.trans` 版留给 V 需要 col-major 但 smem 是 row-major 的场景（如 FlashAttention P@V）。

---

### 4.3 统一循环版 multistage pipeline

#### 4.3.1 概念说明

`hgemm_mma_stage_tn.cu` 用的是**三段式循环**（prefetch → 主循环 → 尾循环），而 notes-v2.cu 的 Phase 7b-2 把它重构成了**统一循环版**（single loop），核心思想是：**让循环变量 k 从 0 开始，每次迭代既负责「计算 tile k」也负责「预取 tile k+K_STAGE-1」**，从而消除尾部的重复代码。

要理解这个 kernel，先看它的 **Tile 层级**（Tile Hierarchy）——这是它能在 256 线程下算 128×128 输出的关键：

- **MMA Atom**：`m16n8k16`，一条 MMA 指令算的最小块（输出 16×8）。
- **MMA Tile（多个 warp）**：8 个 warp 排成 2×4 → \(2\times16, 4\times8 = 32\times32\)，即一次 8 个 MMA atom。
- **VAL Tile（多次重复）**：每个 warp 在 M、N 方向各重复 4 次 → \(32\times4, 32\times4 = 128\times128\)。

于是 `BM=BN=128`，8 warps × 32 线程 = **256 线程**，正好对应 `__launch_bounds__(256)`。

本次 diff 还特别澄清了一个易混淆点：**warp 2×4 layout 是按 col-major 排列 MMA0~MMA7 的**（见 4.3.3）。另外 prefetch loop 已预加载 `K_STAGE-1` 个 stage，主循环的「预取」从 tile `k+K_STAGE-1` 开始。

#### 4.3.2 核心流程

统一循环版的整体节奏（以 `K_STAGE=3` 为例）：

```
1. 预加载 stage 0, 1（共 K_STAGE-1=2 个），wait_group(K_STAGE-2=1)，sync
2. for k = 0 .. NUM_K_TILES-1:        # 统一循环
     a. 若 k+K_STAGE-1 < NUM_K_TILES:  # 还有未来 tile 要预取
          cp.async 预取 tile (k+K_STAGE-1) 到 stage (k+K_STAGE-1)%K_STAGE
          commit_group
     b. 从 stage (k%K_STAGE) ldmatrix 加载 A(x4)、B(x2)
     c. 16 次 HMMA16816 累加进 RC
     d. wait_group(K_STAGE-2)（满载）或 wait_group(0)（尾部排空），sync
3. Epilogue: RC 寄存器 → global memory
```

要点：

- `smem_sel = k % K_STAGE` 是「当前要算的 stage」，`smem_sel_next = (k+K_STAGE-1) % K_STAGE` 是「要预取进哪个 stage」。
- **预取语义是「为未来准备」**：迭代 k 计算的是 tile k，但加载的是 tile `k+K_STAGE-1`，这就是「加载语义为预取未来」。
- `wait_group` 自适应：满载期允许 `K_STAGE-2` 个 group 未完成，尾部排空则 `wait_group(0)` 等全部完成。

#### 4.3.3 源码精读

kernel 签名与 Tile 常量（[notes-v2.cu:1353-1364](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/interview/notes-v2.cu#L1353-L1364)）：模板参数 `MMA_M=16, MMA_N=8, MMA_K=16, MMA_TILE_M=2, MMA_TILE_N=4, VAL_TILE_M=4, VAL_TILE_N=4, K_STAGE=3`，推导出 `BM=BN=128, BK=16`。

**warp 2×4 layout（本次 diff 新增注释）**：[notes-v2.cu:1379-1382](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/interview/notes-v2.cu#L1379-L1382)

```c
// warp_m变化快(0->0,1->1), warp_n变化慢([0,1]->0,[2,3]->1,...), 因此，
// 这种2x4的MMA(Warp) layout是按照col major的顺序来排列MMA0~MMA7的
const int warp_m = warp_id % 2; // 0,1（M 方向 2 个 warp）
const int warp_n = warp_id / 2; // 0,1,2,3（N 方向 4 个 warp）
```

含义：`warp_id` 从 0 到 7，`warp_m=warp_id%2` 变化快（0,1,0,1,…），`warp_n=warp_id/2` 变化慢（0,0,1,1,2,2,3,3）。因此 warp 编号到 (m,n) 的映射是 **col-major**：MMA0→(0,0), MMA1→(1,0), MMA2→(0,1), MMA3→(1,1), …，MMA7→(1,3)。这与 2×4 排布的物理顺序一致。

**预加载 loop（本次 diff 澄清）**：[notes-v2.cu:1407-1428](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/interview/notes-v2.cu#L1407-L1428) 预加载前 `K_STAGE-1` 个 stage，每个 stage 用一条 `cp.async.cg ... 16`（16 字节 = 8 个 half）搬一块，A、B 各一块，然后 `commit_group`。

**统一主循环的条件预取（本次 diff 澄清）**：[notes-v2.cu:1437-1459](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/interview/notes-v2.cu#L1437-L1459)。新增注释强调：**因为 prefetch loop 已经 load 了 `K_STAGE-1` 个 stage，所以主循环从这里开始预取 tile `k+K_STAGE-1`**，即「迭代 k 计算当前 tile，同时为 K_STAGE-1 步之后预取」。`if (k+K_STAGE-1 < NUM_K_TILES)` 保证不越界。

**MMA 计算**：[notes-v2.cu:1504-1514](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/interview/notes-v2.cu#L1504-L1514) 是双重 `#pragma unroll` 循环，`VAL_TILE_M × VAL_TILE_N = 4×4 = 16` 次 `HMMA16816`，每次把 RA[i]×RB[j] 累加进 RC[i][j]。这正是「一个 warp 算 128×128 输出」的 16 次 MMA。

**自适应 wait_group**：[notes-v2.cu:1516-1522](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/interview/notes-v2.cu#L1516-L1522) 满载期 `wait_group(K_STAGE-2)`，尾部 `wait_group(0)`。

对照看独立文件 `hgemm_mma_stage_tn.cu` 的三段式写法（kernel 主体 [hgemm_mma_stage_tn.cu:124-268](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/hgemm/mma/basic/hgemm_mma_stage_tn.cu#L124-L268)）：它的主循环从 `k=K_STAGE-1` 起步（[hgemm_mma_stage_tn.cu:201-208](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/hgemm/mma/basic/hgemm_mma_stage_tn.cu#L201-L208)），并额外有一段尾循环处理最后 `K_STAGE-1` 个 tile（[hgemm_mma_stage_tn.cu:276-360](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/hgemm/mma/basic/hgemm_mma_stage_tn.cu#L276-L360)）。两者**计算逻辑等价**，统一循环版只是把尾循环合并进了主循环的「条件预取」分支。

#### 4.3.4 代码实践

**实践目标**：跑通 verification harness，确认 MMA TN kernel 正确；并对照两种循环写法。

**操作步骤**：

1. 按 `u1-l2` 的 Quick Start 编译 notes-v2.cu（Ada 用 `sm_89`，Hopper 用 `sm_90a`；本段不依赖 CuTe/WGMMA 宏，可不加）。
2. 运行二进制，在输出表里找 `HGEMM MMA` 这一行。
3. 阅读测试函数 [notes-v2.cu:4474-4546](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/interview/notes-v2.cu#L4474-L4546)：它先构造 A、B，再手工生成 \(B^T\)（[notes-v2.cu:4488-4494](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/interview/notes-v2.cu#L4488-L4494)），用 cuBLAS FP16 求参考解，再启动 kernel（[notes-v2.cu:4522-4528](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/interview/notes-v2.cu#L4522-L4528)），最后比对 Max Err。

**需要观察的现象**：`HGEMM MMA` 行的 Max Err 小于阈值 `1.0f`（fp16 GEMM 放宽阈值，见 [notes-v2.cu:4539](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/interview/notes-v2.cu#L4539)），状态为 `PASS`。

**预期结果**：Max Err 通常在 \(10^{-2}\sim10^{-1}\) 量级（fp16 + fp16 累加的固有误差），远小于 1.0，判 PASS。> 待本地验证（若无 GPU，按 `u1-l2` 记录命令即可）。

#### 4.3.5 小练习与答案

**练习 1**：`K_STAGE=3` 时，prefetch loop 预加载了几个 stage？主循环第一次迭代（k=0）预取的是哪个 tile？

**答案**：预加载 `K_STAGE-1=2` 个 stage（stage 0、1）。k=0 时预取 tile `k+K_STAGE-1 = 0+2 = 2`，写入 stage `(0+2)%3 = 2`。

**练习 2**：统一循环版相对三段式版省掉了什么？

**答案**：省掉了独立的「尾循环」——三段式要在主循环后再写一段处理最后 `K_STAGE-1` 个 tile 的代码，统一循环版用「条件预取 + 自适应 wait_group」把这部分合并进主循环，逻辑更紧凑、不易写错。

---

### 4.4 C fragment 寄存器排布与 epilogue

#### 4.4.1 概念说明

MMA 算完后，结果 C 分布在 warp 内 32 线程的寄存器里，**不是连续存放**。要写回 global memory，必须先搞清楚「哪个线程的哪个寄存器对应 C 的哪个元素」——这就是 **C fragment 寄存器排布**。

`m16n8k16` 的 C fragment（16×8=128 元素，32 线程每线程 4 个 half = 2 个 `uint32`）排布规则：

- 每个线程持有 4 个 half，分成 **RC[0]（行 0–7）和 RC[1]（行 8–15）** 两个 `uint32`。
- 行号 = `lane_id / 4`（每 4 个连续 lane 负责同一行）。
- 列对 = `lane_id % 4`（每个 `uint32` 装 2 个相邻列的 half，4 组 lane 分别覆盖列对 c0-1/c2-3/c4-5/c6-7）。

本次 diff 新增的关键注释澄清了 **RC0/RC1 的物理含义**：它们不是「相邻两列」，而是 **按 col-major 排布的两个 8×8 子矩阵上同一位置、不同物理行的元素**。也就是说 RC0 表示「第一行」（上 8×8 子矩阵的某行），RC1 表示「第二行」（下 8×8 子矩阵的对应行，跨了 8 行）。这正是 epilogue 里要把 RC0、RC1 分开 store 的原因。

#### 4.4.2 核心流程

epilogue 把 RC 寄存器写回 global memory 的步骤：

1. **warp shuffle 收集**：一个 4-lane 组里，lane 0 只有自己的 {c0,c1}，需要从 lane 1/2/3 各收 2 个 half，用 3 次 `__shfl_sync` 凑齐一行 8 个 half。
2. **128-bit store**：只有 `lane_id % 4 == 0` 的线程（每组的 lane 0）执行写回，一次 `float4`（128 bit = 8 half）写一行。
3. **RC0/RC1 分两次写**：RC0 写到行 `m`，RC1 写到行 `m+8`（因为 RC1 是跨 8 行的「第二行」）。

#### 4.4.3 源码精读

epilogue 开头的 RC0/RC1 注释（**本次 diff 新增**）：[notes-v2.cu:1528-1534](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/interview/notes-v2.cu#L1528-L1534)

```
// RC[...][...][2] 中保存了2个 uint32 寄存器，每个uint32 表示两个临近的fp16值{c0,c1}，
// 然后 RC[...][...][0] 和 RC[...][...][1] 代表的是按照 col-major 排布的2个8x8子矩阵上
// （不同物理行，跨8x8）同一个位置上的元素，实际代表了2个不同的行的元素，因此要分开RC0,RC1；
// RC0 表示第一行，8个half，可以用4个uint32寄存器来装；同理，RC1 表示第二行。
```

紧跟着的 ASCII 表（[notes-v2.cu:1535-1557](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/interview/notes-v2.cu#L1535-L1557)）把 16×8 C 矩阵逐行列出每个 lane 持有的 {c0,c1}：行 0–7 由 RC[0] 表达（lane 0–31 各贡献），行 8–15 由 RC[1] 表达（同样的 lane 0–31）。

shuffle 收集与 128-bit store 代码：[notes-v2.cu:1558-1599](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/interview/notes-v2.cu#L1558-L1599)。其中：

- `RC0[j][1..3]` 用 `__shfl_sync(..., lane_id+1/+2/+3)` 从相邻 lane 收集，凑齐一行的 8 half（[notes-v2.cu:1562-1567](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/interview/notes-v2.cu#L1562-L1567)）。
- 行号映射表（[notes-v2.cu:1570-1582](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/interview/notes-v2.cu#L1570-L1582）：lane 0–3→row 0/8，lane 4–7→row 1/9，…）说明同一个 lane 同时负责「上 8×8 的某行」和「下 8×8 的对应行」。
- `lane_id%4==0` 的线程做写回（[notes-v2.cu:1583-1599](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/interview/notes-v2.cu#L1583-L1599)）：`store_gmem_c_addr_0 = m*N + n`（写 RC0，即第一行），`store_gmem_c_addr_1 = (m+8)*N + n`（写 RC1，即第二行），两次 `float4` 写入。

#### 4.4.4 代码实践

**实践目标**：搞清一个 lane 在 epilogue 里写回哪两个元素。

**操作步骤**：

1. 设 `warp_m=0, warp_n=0, lane_id=0, i=0, j=0`，`by=0, bx=0`（即第一个 block 的第一个 warp）。
2. 算 RC0 行：`store_lane_gmem_c_m = 0*128 + 0 + 0/4 = 0`；列：`store_warp_smem_c_n = 0 + 0 = 0`，`store_lane_gmem_c_n = 0 + 0 = 0`。RC0 写到 `C[0*128 + 0]`，即 C 的 (0,0)。
3. RC1 行：`(0+8)*128 + 0 = C[8*128]`，即 C 的 (8,0)。
4. 对照 ASCII 表：lane 0 在 RC[0] 持有 {c0,c1}（行 0，列 0-1），在 RC[1] 持有 {c0,c1}（行 8，列 0-1）。

**预期结果**：lane 0 一次 128-bit store 写 C(0, 0..7)，再一次写 C(8, 0..7)。两次写对应「RC0 第一行、RC1 第二行（跨 8 行）」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 epilogue 用 `__shfl_sync` 收集，而不是每个线程自己写自己的 4 个 half？

**答案**：MMA 的 C fragment 是分布式排布——一行 8 个 half 散落在 4 个 lane 手里（每个 lane 2 half）。要让 global memory 的写入是**合并访问**（一行连续 8 half = 16 字节 = 一次 128-bit store），必须先用 shuffle 把同行 4 个 lane 的 half 汇聚到一个 lane，再由它一次性写出。

**练习 2**：RC0 写到行 m，RC1 写到行 m+8。为什么相差 8 而不是 1？

**答案**：`m16n8k16` 的 C fragment 把 16 行拆成两个 8×8 子矩阵（行 0–7 与行 8–15），按 col-major 排布时同一 lane 的 RC[0] 和 RC[1] 恰好对应「相隔 8 行」的两个元素。所以同一 lane 的两次 store 地址相差 8 行。

---

## 5. 综合实践

把本讲 4 个模块串起来，完成下面这个**源码阅读型实践**（无需 GPU 也能做）：

**任务**：给 `hgemm_mma_stages_tn` 画一张「数据生命周期图」，标注一个数据块从 HBM 到寄存器再到输出的完整旅程，并把本次 diff 的 5 处新注释对应到图上。

**步骤**：

1. **入口与布局**：从 [notes-v2.cu:1353-1382](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/interview/notes-v2.cu#L1353-L1382) 出发，标注 BM=BN=128、8 warps 排成 2×4（**对应新注释：col-major 排列 MMA0~MMA7**）。
2. **HBM→smem（cp.async）**：画 prefetch loop（[notes-v2.cu:1407-1428](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/interview/notes-v2.cu#L1407-L1428)）预加载 K_STAGE-1 个 stage，主循环条件预取 tile k+K_STAGE-1（**对应新注释：prefetch 已加载 K_STAGE-1 个 stage**）。
3. **smem→寄存器（ldmatrix）**：画 A 用 x4（32 线程）、B 用 x2（前 16 线程）（**对应新注释：x4 需 32 线程、x2 仅前 16 线程**）。
4. **寄存器计算（MMA）**：画 16 次 HMMA16816 累加进 RC（[notes-v2.cu:1504-1514](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/interview/notes-v2.cu#L1504-L1514)）。
5. **寄存器→HBM（epilogue）**：画 RC0/RC1 经 shuffle 后 128-bit store，RC0→行 m、RC1→行 m+8（**对应新注释：RC0/RC1 表达 col-major 的两个 8×8 子矩阵相邻行**）。
6. **额外关联**：翻到 Phase 7b-3 的 `swizzle_permuted_A_j`（[notes-v2.cu:1652-1656](https://github.com/xlite-dev/LeetCUDA/blob/3af51ebd83b98d37e27bbee065ed2fcbe3bbb93e/kernels/interview/notes-v2.cu#L1652-L1656)），在图上 smem 这一环节标注「8 half=16B=128bit=4 banks 构成一个 phase，对应 TMA 的 SWIZZLE_32B pattern」——这是 `u7-l2`/`u11-l2` 要展开的 swizzle 主题在本讲的预告。

**交付物**：一张含 5 个阶段（HBM→prefetch→ldmatrix→MMA→epilogue）的流程图，每阶段旁注明对应的新注释与行号。

> 这个练习把「布局 → 异步搬运 → Tensor Core 喂数据 → 累加 → 写回」整条链路打通，是理解后续 swizzle 版（`u11-l2`）、CuTe 版（`u12-l2`）、WGMMA 版（`u13-l1`）的共同基础。

## 6. 本讲小结

- **MMA `m16n8k16` 是 Tensor Core 的原生指令粒度**：一条指令算 \(16\times8\times16\)，A fragment 4 个 `uint32`、B 2 个、C/D 2 个；WMMA `m16n16k16` 在底层就是 2 条 `m16n8k16`。
- **TN 布局** = A row-major、B col-major（即 \(B^T\) row-major），内维 K 连续；它让 `mma...row.col` 天然匹配，B 的 ldmatrix **不需要 `.trans`**。
- **ldmatrix 线程参与规则**：一个 8×8 矩阵需 8 个线程给行地址，故 **x4 用 32 线程、x2 用前 16 线程、x1 用前 8 线程**（本次 diff 在代码里明确标注）。
- **统一循环版 multistage**：k 从 0 开始，每次迭代「算 tile k + 预取 tile k+K_STAGE-1」，prefetch loop 先预加载 K_STAGE-1 个 stage，从而省掉独立的尾循环。
- **warp 2×4 layout 按 col-major 排列 MMA0~MMA7**（warp_m 变化快、warp_n 变化慢），8 warp × 4×4 VAL Tile = 128×128 输出。
- **C fragment epilogue**：RC0/RC1 是 col-major 排布的两个 8×8 子矩阵的「相邻行」（实际跨 8 行），用 `__shfl_sync` 收集后由 `lane%4==0` 的线程做 128-bit store。

## 7. 下一步学习建议

- **`u11-l1` Multistage pipeline + 寄存器 double buffer**：本讲的 multistage 只用了 smem 多 stage，下一步是把寄存器也做成 ping-pong double buffer，进一步隐藏 MMA 延迟。
- **`u11-l2` SMEM Swizzle 消除 bank conflict（LDSM）**：本讲提到的 `swizzle_permuted_A_j` 与 TMA `SWIZZLE_32B` 的联系，会在 `u11-l2` 展开成完整的 swizzle 消 bank conflict 实战。
- **`u12-l2` CuTe HGEMM**：本讲手写了 ldmatrix/MMA/swizzle/流水线同步的全部细节，下一步可以看 CuTe DSL 如何用 tile+layout 抽象自动生成这些，体会「手写 PTX」与「CuTe 调度」的取舍。
- **`u13-l1` WGMMA + TMA（Hopper）**：本讲的 MMA 是 Ampere 同步指令，下一步进入 Hopper 的 warpgroup 级异步 WGMMA（`m64n128k16`）与 TMA 异步搬运，理解「本次提到的 SWIZZLE_32B 正是为 TMA 准备的」。
