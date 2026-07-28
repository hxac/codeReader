# GEMM intrinsics 深入

## 1. 本讲目标

u6-l1 已经建立了一个总览结论：**张量核发射器 = 指令生成器 + 布局说明书**——职责 A 由 `T.gemm` 算子的 `Lower()` 驱动，把一块 tile 展开成张量核 builtin 调用；职责 B 由 `InferLayout()` 驱动，返回 `T.Fragment` 喂给 `LayoutInference`。本讲把镜头拉近，钻进**三类 GEMM 张量核发射器**（MMA / WGMMA / TCGEN05）的内部原理，搞清楚：

1. MMA、WGMMA、TCGEN05 三种发射器在**指令形状、操作数来源、同步模型**上到底差在哪里。
2. Hopper 的 **WGMMA descriptor**（GMMA 描述符）和 Blackwell 的 **TCGEN05 descriptor**（UMMA 描述符）由哪些字段构成、如何从 shared memory 布局反推出来。
3. **ldmatrix** 与**布局变换函数**如何把 shared memory 的数据搬到每个线程的寄存器里，凑成 MMA fragment。
4. 用 `examples/gemm/example_gemm_intrinsics.py` 这一「专家级」例子验证：手动驱动一个发射器（本例是 MACA 的 MFMA）写 GEMM 是什么体验。

学完后，你应能读懂任何一个发射器的 `mma_atom` / `wgmma_ss_atom` / `tcgen05_ss_atom`，并理解它们和 `T.gemm` 自动分派之间的关系。

## 2. 前置知识

本讲建立在 u6-l1 之上，先确认你熟悉下列概念（不熟悉请回看 u6-l1）：

- **发射器（Emitter）**：连接高层 `T.gemm` 与底层硬件指令（`mma.sync` / `wgmma` / `tcgen05.mma` / `mfma`）的桥梁，本质是一个在编译期被 `@T.macro` 展开成 TIR 的 Python 类。
- **Fragment**：`alloc_fragment` 分配的寄存器 tile，其逻辑下标与物理寄存器不一一对应，由 `LayoutInference` 把 tile 切分给各线程。
- **warp / warp-group**：CUDA 一个 warp = 32 线程；Hopper 的 warp-group = 4 个 warp = 128 线程；MACA 一个 warp = 64 线程。
- **shared memory bank conflict 与 swizzle**：用行号 XOR 列块号打散同列元素以消除 bank conflict（u4-l3）。
- **`T.gemm` 的两级分派**：C++ `select_inst` 返回指令键（如 `cuda.wgmma`、`maca.mma`、`rocm.mfma`），Python 再把键映射到发射器实现类（u4-l2）。

补充三个本讲要用到的硬件术语：

- **PTX**：NVIDIA 的并行线程执行指令集，是 GPU 机器码的「汇编」。`mma.sync`、`wgmma.mma_async`、`tcgen05.mma` 都是 PTX 指令。
- **Tensor Memory（TMEM）**：Blackwell（SM100）新增的片上存储，专门给 TCGEN05 的累加器用，比寄存器大、比 shared memory 快。
- **mbarrier**：Hopper/Blackwell 的异步屏障，用于 TMA 拷贝与 TCGEN05 MMA 的完成信号握手（u4-l4）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `tilelang/intrinsics/__init__.py` | intrinsics 模块顶层导出，集中暴露各类发射器与布局工具 |
| `tilelang/cuda/intrinsics/macro/mma_macro_generator.py` | CUDA 标准 MMA 发射器（`mma.sync`，SM70–89），也是 WGMMA/TCGEN05 的父类 |
| `tilelang/cuda/intrinsics/macro/wgmma_macro_generator.py` | Hopper WGMMA 发射器 + `WGMMADescriptorParams` + `compute_gmma_descriptor` |
| `tilelang/cuda/intrinsics/macro/tcgen05_macro_generator.py` | Blackwell TCGEN05 发射器 + `TCGEN05DescriptorParams` + `compute_umma_descriptor` |
| `tilelang/maca/intrinsics/macro/mma_macro_generator.py` | MACA MFMA 发射器（`tvm_mfma`，warp_size=64），`example_gemm_intrinsics.py` 直接使用它 |
| `tilelang/cuda/intrinsics/layout/utils.py` | `get_ldmatrix_offset` 等 ldmatrix 索引映射工具 |
| `tilelang/cuda/intrinsics/layout/mma_layout.py` | shared↔register 的逐线程布局变换函数、`get_swizzle_layout` |
| `tilelang/language/gemm_op.py` | DSL 入口 `T.gemm` / `T.wgmma_gemm` / `T.tcgen05_gemm` 的 Python 下译 |
| `src/op/builtin.cc` | C++ 侧注册 `tl.*` 底层 builtin（`ptx_wgmma_ss`、`tvm_mfma`、`initialize_wgmma_descriptor` 等） |
| `examples/gemm/example_gemm_intrinsics.py` | 专家级例子：手动驱动 MACA MFMA 发射器写 GEMM |

## 4. 核心概念与源码讲解

### 4.1 张量核发射器三态：MMA / WGMMA / TCGEN05

#### 4.1.1 概念说明

NVIDIA 三代张量核指令对应三种发射器，它们的根本差异在于**「操作数从哪里来、一次算多大、谁来同步」**：

| 维度 | MMA（`mma.sync`） | WGMMA（`wgmma.mma_async`） | TCGEN05（`tcgen05.mma`） |
|------|-------------------|----------------------------|--------------------------|
| 架构 | Volta–Ada（SM70–89） | Hopper（SM90） | Blackwell（SM100） |
| 执行单元 | 1 个 warp（32 线程） | 1 个 warp-group（128 线程） | 1 个 warp-group（128 线程） |
| 指令形状 | 小，如 m16n8k16 | M 恒为 64，N∈[8,256]，K=256/bits | 由 instr_desc 决定 |
| 操作数 A | 寄存器（需先 ldmatrix） | 寄存器（RS）或 shared（SS） | shared（SS）或 TMEM（TS） |
| 操作数 B | 寄存器（需先 ldmatrix） | shared（descriptor） | shared（descriptor） |
| 累加器 C | 寄存器 | 寄存器 | **TMEM（张量内存）** |
| 同步 | 同步（指令返回即完成） | 异步（fence/arrive/commit/wait） | 异步（mbarrier 信号） |

**MACA 的 MFMA** 在这个表里最接近「MMA」一列，但 warp_size=64、指令由 `T.tvm_mfma` 发射，4.4 节的例子用的就是它。

为什么要有这三态？核心驱动力是**「减少 ldmatrix 的开销、放大单条指令的算力」**：

- MMA 时代，操作数必须先由 `ldmatrix` 从 shared 搬进寄存器，每个 warp 只能算 \(16\times8\times16\) 的小块，搬数据占了大量指令槽。
- WGMMA 让 warp-group 直接拿 shared memory 的**描述符**当操作数（SS 变体），一条指令算 \(64\times N\times K\) 的大块，省掉了 A 的 ldmatrix；并且是异步的，可以和 copy 重叠。
- TCGEN05 进一步把累加器放进专用 TMEM，腾出通用寄存器给其它计算，并用 mbarrier 做完成信号。

#### 4.1.2 核心流程

三类发射器的「展开成 TIR」流程可以统一抽象为：

```
用户调用 emitter.mma(A_local, B_local, C_local)      # 或 wgmma / tcgen05mma
   │
   ├─ for i,j in grid(warp_rows, warp_cols):         # 遍历输出 tile 里的每个指令原子
   │     emitter.<xxx>_atom(A, B, C, i, j, ki)       # 发射单条张量核指令
   │           └─ T.ptx_mma / T.ptx_wgmma_ss / T.tvm_mfma / ...   # builtin 调用
   │
   └─ （WGMMA/TCGEN05 额外）fence → arrive → commit → wait / mbarrier
```

而「布局说明书」流程（由 `InferLayout` 驱动）是：

```
emitter.make_mma_load_layout(local_buf, matrix="A"/"B")   # 返回 A/B 的 fragment 布局
emitter.make_mma_store_layout(local_buf)                  # 返回 C 累加器的 fragment 布局
   │
   └─ 由 LayoutInference pass 写进 SBlock 的 layout_map 注解
```

**ldmatrix 的本质**：MMA 指令要求每个线程的寄存器里放着 fragment 的特定切片。`ldmatrix_a/b` 不是真的「矩阵加载」，而是**一张「线程号 → shared memory 坐标」的查表函数**，告诉每个线程该去 shared 的哪个 `(row, col)` 取数，凑齐 MMA fragment 所需的寄存器排布。WGMMA(SS)/TCGEN05 用 descriptor 直接定位 shared，就不需要这张表了。

#### 4.1.3 源码精读

三类发射器的「祖宗」是 CUDA 的 MMA 发射器基类，它定义了共同的骨架（`warp_rows`/`warp_cols`、`local_size_*`、`make_mma_*_layout`）：

> [tilelang/cuda/intrinsics/macro/mma_macro_generator.py:36-46](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/macro/mma_macro_generator.py#L36-L46) —— 标准 MMA 发射器基类，`M_DIM=16`、`WARP_SIZE=32`，所有发射器共享这套微调参数初始化骨架。

ldmatrix 的查表函数集中在 layout 工具里。以 fp16（16 bit）为例：

> [tilelang/cuda/intrinsics/layout/mma_layout.py:18-21](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/layout/mma_layout.py#L18-L21) —— `ldmatrix_32x8_to_shared_16x16_layout`：给定 `(thread_id, local_id)`，返回该线程应取的 shared memory `(row, col)`。这是「32 个线程各取 8 个 fp16 → 拼成 16×16 fragment」的坐标映射。

对应的统一入口会按 dtype 分派不同的映射函数：

> [tilelang/cuda/intrinsics/layout/utils.py:20-64](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/layout/utils.py#L20-L64) —— `get_ldmatrix_offset`：按 `dtype_bits`（32/16/≤8）选不同的 ldmatrix 坐标变换，并区分 A/B 矩阵与是否转置。

顶层 `tilelang/intrinsics/__init__.py` 把所有发射器收口导出，是本讲整个模块的「目录」：

> [tilelang/intrinsics/__init__.py:1-23](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/intrinsics/__init__.py#L1-L23) —— 导出标准 MMA 发射器、`WGMMATensorCoreIntrinEmitter` + `WGMMADescriptorParams`、`TCGEN05TensorCoreIntrinEmitter` + `TCGEN05DescriptorParams`，以及 swizzle 布局工具。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：用一个表格把三类发射器「操作数来源」对齐。
2. **步骤**：打开 `tilelang/intrinsics/__init__.py`，记下三个发射器类名；再分别打开 `mma_macro_generator.py`（CUDA）、`wgmma_macro_generator.py`、`tcgen05_macro_generator.py`，找到它们各自的 `WARP_SIZE` 与主入口方法名（`mma` / `wgmma` / `tcgen05mma`）。
3. **观察现象**：CUDA MMA 基类 `WARP_SIZE=32`，而 `tilelang/maca/intrinsics/macro/mma_macro_generator.py` 里 `WARP_SIZE=64`。
4. **预期结果**：你能说出「MMA 操作数在寄存器、WGMMA 的 B 操作数走 shared descriptor、TCGEN05 的 C 在 TMEM」三句话。

#### 4.1.5 小练习与答案

**练习 1**：为什么 WGMMA 的 SS 变体不需要 `ldmatrix_a`？

**答案**：SS 变体的 A、B 操作数都直接来自 shared memory，硬件通过 descriptor 定位，由 warp-group 内部完成搬运与排布，因此不需要软件先用 `ldmatrix` 把数据搬进寄存器。只有 RS 变体（A 在寄存器）才需要先加载 A。

**练习 2**：`get_ldmatrix_offset` 按 `dtype_bits` 分了三档（32/16/≤8），为什么 int8（8 bit）要单独一档？

**答案**：不同位宽下，单条 `ldmatrix` 指令搬运的元素数和 fragment 形状不同。int8 时一个 128-bit 寄存器槽能装更多元素，坐标映射函数（`ldmatrix_32x16_to_shared_16x32_layout_*`）与 fp16（`ldmatrix_32x8_to_shared_16x16_layout`）不同，且还要乘以 pack factor（`8 // dtype_bits`）。

---

### 4.2 WGMMA 发射器与 GMMA descriptor（Hopper）

#### 4.2.1 概念说明

WGMMA（Warp-Group MMA）是 Hopper（SM90）引入的异步张量核指令。它的两个关键设计：

1. **操作数可以用 shared memory descriptor**：不再需要把 A、B 都 `ldmatrix` 进寄存器，而是把 shared memory 里某块 tile 的「基地址 + 布局」打包成一个 64-bit **descriptor**，硬件自己读。这就是 SS（Shared-Shared）变体；若 A 已在寄存器则是 RS（Register-Shared）变体。
2. **异步执行**：WGMMA 提交后立即返回，结果稍后才到，需要 `warpgroup_arrive` / `warpgroup_commit_batch` / `warpgroup_wait` 序列来同步。

**descriptor 是什么？** 它是 Hopper 硬件规定的一个 64-bit 位域，编码了 shared memory 操作数的：基地址指针、swizzle 模式（NONE/32B/64B/128B）、Leading Byte Offset（LBO）、Stride Byte Offset（SBO）。软件的职责就是把 shared memory 的逻辑布局翻译成这四个字段。

#### 4.2.2 核心流程

WGMMA 发射器把一块输出 tile 展开成多条 `wgmma` 指令的流程：

```
wgmma(A_region, B_region, C_region, clear_accum, wg_wait)
  │
  ├─ 若 A 在 fragment → wgmma_rs（RS 变体）；否则 → wgmma_ss（SS 变体）
  │
  ├─ SS 变体：
  │   ├─ compute_wgmma_a_desc_params / compute_wgmma_b_desc_params   # 纯 Python，算 LBO/SBO/swizzle
  │   ├─ init_wgmma_a_desc / init_wgmma_b_desc                       # 发射 T.initialize_wgmma_descriptor
  │   ├─ wgmma_fence_c / wgmma_arrive
  │   ├─ for j,i,ki: wgmma_ss_atom(...) → T.ptx_wgmma_ss             # 逐原子发射指令
  │   └─ wgmma_commit / wgmma_wait(wg_wait)
```

descriptor 字段由 `compute_gmma_descriptor` 从 CuTe 布局反推（这是 CuTe `make_gmma_desc` 的移植）。其核心数学是把 shared memory 的 (MN, K) 二维布局按规范 tiler 做 `logical_divide`，再读取划分后各 mode 的 stride 作为 LBO/SBO 标量。规范的 K-major 划分是：

\[
((8,m),(2,k)) : ((8,\text{SBO}),(1,2))
\]

即 MN 方向先切成 8 一组、K 方向先切成 2 一组；划分后 stride\(<0,0>\) 必须等于 swizzle 宽度 \(W = 2^{b}\)（\(b\in\{5,6,7\}\) 对应 32B/64B/128B）。这些 `assert` 就是「不是规范 GMMA 布局就报错」的安全网。

#### 4.2.3 源码精读

WGMMA 发射器继承自 MMA 发射器，新增 descriptor 与异步同步逻辑：

> [tilelang/cuda/intrinsics/macro/wgmma_macro_generator.py:33-58](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/macro/wgmma_macro_generator.py#L33-L58) —— `WGMMADescriptorParams` 数据类：把 `compute_gmma_descriptor` 的产物打包成 `swizzle_mode` / `leading_byte_offset` / `stride_byte_offset` / `swizzle_atom_elems` / `k_atom_size` / `slice_byte_offset` 等字段，LBO/SBO 已右移 4 位（`>> 4`），可直接喂给 `T.initialize_wgmma_descriptor`。

指令前缀（决定算多大）由 N 维推导：

> [tilelang/cuda/intrinsics/macro/wgmma_macro_generator.py:244-257](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/macro/wgmma_macro_generator.py#L244-L257) —— `_initialize_wgmma_prefix`：WGMMA 的 M 恒为 64，N 取 `gcd(warp_col_tiles, 256)`（必须是 8 的倍数且落在 [8,256]），K 取 `256 // bits`（每条指令吃 256 bit）。由此拼出前缀字符串 `m64n{N}k{K}`。

主入口按 A 是否在 fragment 分派 SS/RS：

> [tilelang/cuda/intrinsics/macro/wgmma_macro_generator.py:282-320](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/macro/wgmma_macro_generator.py#L282-L320) —— `wgmma`：先 `compute_wgmma_a/b_desc_params`，再在 `T.macro` 里 `alloc_wgmma_desc` → `init_*_desc` → `fence_c`/`arrive` → 三层 unroll 循环调 `wgmma_ss_atom` → `commit`/`wait`。

单条指令的发射（SS 原子）：

> [tilelang/cuda/intrinsics/macro/wgmma_macro_generator.py:736-753](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/macro/wgmma_macro_generator.py#L736-L753) —— `T.ptx_wgmma_ss(...)`：把累加器 dtype、前缀、A/B 是否 K-major、descriptor 句柄、A/B 偏移（字节右移 4 位）、C 偏移、`scale_out`（首拍是否清零累加器）等参数传给 builtin，最终印成 `wgmma.mma_async ...` PTX。

descriptor 初始化与 slice 偏移推进：

> [tilelang/cuda/intrinsics/macro/wgmma_macro_generator.py:426-461](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/macro/wgmma_macro_generator.py#L426-L461) —— `init_wgmma_b_desc`：先 `T.initialize_wgmma_descriptor(desc, base_ptr, swizzle_mode, lbo, sbo)` 从 buffer 基址建描述符（保持 warp 一致），若是切片操作数再 `T.increase_descriptor_offset(desc, slice_byte_offset)` 推进到切片起点。

C++ 侧注册了这些底层 builtin：

> [src/op/builtin.cc:233-241](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/builtin.cc#L233-L241) —— `ptx_wgmma_ss` / `ptx_wgmma_rs` 各 15 个输入，标记为 `kOpaque`（有副作用）。

> [src/op/builtin.cc:588-601](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/builtin.cc#L588-L601) —— `initialize_wgmma_descriptor`（5 输入）与 `increase_descriptor_offset`（2 输入），即上面 `T.initialize_wgmma_descriptor` / `T.increase_descriptor_offset` 的注册点。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：理解 WGMMA 指令前缀如何随 `warp_col_tiles` 变化。
2. **步骤**：阅读 `_initialize_wgmma_prefix`，假设 `block_col_warps=2`、`warp_col_tiles=64`，手算 `inst_n = gcd(64, 256)` 与 `inst_k`（fp16）。
3. **观察现象**：`inst_n` 应为 64，`inst_k` 应为 16，前缀为 `m64n64k16`。
4. **预期结果**：你能解释「N 越大，单条 WGMMA 算的越多，但 N 必须 8 的倍数且 ≤256」这条 Hopper 硬件约束如何反映在代码的 `assert` 里。

#### 4.2.5 小练习与答案

**练习 1**：`WGMMADescriptorParams.slice_byte_offset` 什么时候非零？为什么 descriptor 要从 buffer 基址建、再推进偏移，而不是直接用切片指针？

**答案**：当操作数是 buffer 的切片（如 `B[:, j*64:...]`）时非零。从基址建描述符是为了让 `cvta`（取地址）操作数是循环不变量、warp 一致；若直接用携带归纳变量的切片指针做 `cvta`，描述符就不再是 warp 一致的，会出错。所以用 `increase_descriptor_offset` 在描述符内部「原地加」到切片起点。

**练习 2**：WGMMA 的 `wg_wait` 参数为 `-1` 时表示什么？

**答案**：`wg_wait >= 0` 才发射 `warpgroup_wait(wg_wait)`；`wgmma_gemm`（显式异步入口）正是传 `-1` 来**禁止**自动等待，把同步留给用户用 `T.wait_wgmma` 手动控制（见 `gemm_op.py` 中 `wgmma_gemm` 传 `wg_wait=-1`）。

---

### 4.3 TCGEN05 发射器与 UMMA descriptor（Blackwell）

#### 4.3.1 概念说明

TCGEN05（`tcgen05.mma`）是 Blackwell（SM100）的张量核指令。它和 WGMMA 同属「descriptor 驱动的异步 MMA」，但有两点关键进化：

1. **累加器 C 在 Tensor Memory（TMEM）**：不再占用通用寄存器，腾出的寄存器可做 epilogue 计算。输入操作数 A 可来自 shared（SS 变体）或 TMEM（TS 变体），B 始终来自 shared。
2. **用 mbarrier 做完成信号**：不再用 `warpgroup_wait`，而是发射后 `tcgen05.mma` 的完成由一个 mbarrier 通知，用户用 `mbarrier_wait_parity(...)` 等待。

TCGEN05 的 descriptor（UMMA descriptor）与 WGMMA 的 GMMA descriptor 字段几乎一样（swizzle_mode / LBO / SBO / swizzle_atom_elems / k_atom_size / slice_byte_offset），但多了 `elem_bits`（用位宽做偏移数学，支持 FP4/FP6 等子字节类型），且由 `compute_umma_descriptor`（CuTe `make_umma_desc` 的移植）计算。

#### 4.3.2 核心流程

```
tcgen05mma(A_buf, B_buf, C_local_buf, mbar, clear_accum)
  │
  ├─ 若 A 在 TMEM → tcgen05mma_ts；否则 → tcgen05mma_ss
  │
  ├─ SS 变体：
  │   ├─ compute_tcgen05_a/b_desc_params        # 纯 Python，算 UMMA descriptor 字段
  │   ├─ compute_tcgen05_instr_desc             # 算指令级描述符（M/N/K atoms）
  │   ├─ init_tcgen05_a/b_desc                  # T.initialize_tcgen05_descriptor / T.alloc_tcgen05_smem_desc
  │   ├─ for j,i,ki: tcgen05_ss_atom(...) → T.ptx_tcgen05_mma_ss
  │   └─ tcgen05_atom_arrive(mbar)              # 通知 mbarrier
```

注意 `C_local_buf` 在 TCGEN05 里实际是 TMEM buffer（`alloc_local` + `shared.tmem` scope），`make_mma_store_layout` 也要给出 TMEM 的存储布局——这就是 `gemm_op.py` 里 `make_blockscaled_gemm_layout` 存在的原因：用户必须为 TMEM 累加器显式标注布局，后续 `T.copy(C_tmem, ...)` 才能正确下译。

#### 4.3.3 源码精读

UMMA descriptor 数据类（对比 4.2 的 GMMA 版，多了 `elem_bits`）：

> [tilelang/cuda/intrinsics/macro/tcgen05_macro_generator.py:29-55](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/macro/tcgen05_macro_generator.py#L29-L55) —— `TCGEN05DescriptorParams`：字段与 `WGMMADescriptorParams` 同构，但用 `elem_bits`（位宽）而非 `elems_in_bytes`，方便 FP4/FP6 子字节类型的偏移计算。

主入口按 A 是否在 TMEM 分派 SS/TS：

> [tilelang/cuda/intrinsics/macro/tcgen05_macro_generator.py:272-293](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/macro/tcgen05_macro_generator.py#L272-L293) —— `tcgen05mma`：`is_tensor_memory(A_buf)` 为真走 TS 变体，否则走 SS 变体。注释说明 SS=「A、B 都来自 shared」，TS=「A 来自 TMEM、B 来自 shared」。

SS 变体的发射骨架（与 WGMMA 高度对称，但末尾用 mbarrier 而非 wait）：

> [tilelang/cuda/intrinsics/macro/tcgen05_macro_generator.py:295-337](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/macro/tcgen05_macro_generator.py#L295-L337) —— `tcgen05mma_ss`：`alloc_tcgen05_smem_desc` → `init_*_desc` → 三层 unroll 调 `tcgen05_ss_atom` → `tcgen05_atom_arrive(mbar)`。注意它没有显式 wait，完成信号交给 mbarrier。

C++ 侧的 TCGEN05 builtin 注册：

> [src/op/builtin.cc:253-261](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/builtin.cc#L253-L261) —— `ptx_tcgen05_mma_ss`（14 输入）与 `ptx_tcgen05_mma_ts`（13 输入），分别对应 SS/TS 两条 PTX 路径。

> [src/op/builtin.cc:593-596](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/builtin.cc#L593-L596) —— `initialize_tcgen05_descriptor`（7 输入，比 WGMMA 的 5 输入多出指令级描述符参数）。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：对比 GMMA 与 UMMA descriptor 的差异。
2. **步骤**：并排打开 `compute_gmma_descriptor`（wgmma 文件 L61 起）与 `compute_umma_descriptor`（tcgen05 文件 L73 起），逐行对比它们的字段计算。
3. **观察现象**：两者都用 `cute.ComposedLayout.from_tilelang` 解码布局、都按 K-major/MN-major 做 `logical_divide`、都读同样的规范 stride；唯一显著差异是偏移单位（GMMA 用字节 `elems_in_bytes`，UMMA 用位 `elem_bits`）与 swizzle_atom_elems 的换算。
4. **预期结果**：你能说出「UMMA 是 GMMA 的 Blackwell 推广，多支持了子字节类型与 TMEM 累加器」这句话。

#### 4.3.5 小练习与答案

**练习 1**：为什么 TCGEN05 的累加器必须在 TMEM，而不能像 WGMMA 那样放寄存器？

**答案**：TCGEN05 指令的累加器硬件接口就是 TMEM（一条 `tcgen05.mma` 的输出地址指向 TMEM 槽位）。把累加器移出通用寄存器是 Blackwell 的设计目标——释放寄存器给 epilogue 与其它计算，TMEM 提供大得多的累加空间。

**练习 2**：`tcgen05mma` 如何决定走 SS 还是 TS？

**答案**：用 `is_tensor_memory(A_buf)` 检测 A 的内存 scope；若 A 在 `shared.tmem`（TMEM）则走 TS（A 来自 TMEM、B 来自 shared descriptor），否则走 SS（A、B 都来自 shared descriptor）。

---

### 4.4 实战：example_gemm_intrinsics.py 与 MACA MFMA

#### 4.4.1 概念说明

前面三节都是「`T.gemm` 自动分派到发射器」的隐式用法。`examples/gemm/example_gemm_intrinsics.py` 展示的是**专家级（Level 3）显式用法**：用户**自己构造发射器、自己调用 `ldmatrix_a` / `ldmatrix_b` / `mma` / `stmatrix`**，像写手写 CUDA 一样精确控制每条张量核指令。这种用法在需要精细控制 shared→register 布局变换（如反量化 GEMM）时很有用。

这个例子特意导入的是 **MACA 的 MFMA 发射器**（`tilelang.maca.intrinsics.macro.mma_macro_generator.TensorCoreIntrinEmitter`），用 `T.tvm_mfma` 发射 MetaX GPU 的矩阵乘指令，`warp_size=64`。它和 CUDA 标准 MMA 发射器同构，但指令后缀、warp 大小、ldmatrix 坐标映射都按 MACA 硬件重写。

MFMA 的指令后缀由「数据类型 + k_dim」拼出，例如 fp16 → `16x16x16f16`、tf32 → `16x16x8tf32`、int8（C500/C600 代）→ `16x16x16i8`。

#### 4.4.2 核心流程

例子的 kernel 结构是经典的「搬进来—算—搬出去」，但**算的部分被拆成了手动的 ldmatrix + mma + stmatrix**：

```
with T.Kernel(...) as (bx, by):
    alloc A_shared / B_shared / C_shared (shared.dyn)
    alloc A_local / B_local / C_local    (local，线程私有寄存器)
    annotate_layout(A_shared, swizzle); annotate_layout(B_shared, swizzle)
    T.clear(C_local)                                    # 累加前必须清零
    for ko in T.Pipelined(K // block_K, num_stages=2):  # 软件流水
        # 1. global → shared（T.Parallel 手写拷贝）
        # 2. 对每个 micro-k：
        mma_emitter.ldmatrix_a(A_local, A_shared, ki)   # shared → 寄存器 fragment
        mma_emitter.ldmatrix_b(B_local, B_shared, ki)
        mma_emitter.mma(A_local, B_local, C_local)      # C_local += A_local @ B_local
    mma_emitter.stmatrix(C_local, C_shared)             # fragment → shared
    # 3. shared → global
```

注意：因为 A/B 用的是 `alloc_local`（线程私有数组）而非 `alloc_fragment`，所以这里**不走 LayoutInference 的自动布局推断**——每个线程取哪些元素完全由发射器的 `ldmatrix_a/b` 内部坐标函数决定。这是「Level 3 手动控制」与「Level 2 自动推断」的分水岭。

#### 4.4.3 源码精读

例子导入 MACA 发射器并用 64 线程 warp：

> [examples/gemm/example_gemm_intrinsics.py:6-8](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm_intrinsics.py#L6-L8) —— 从 `tilelang.maca.intrinsics.macro.mma_macro_generator` 导入 `TensorCoreIntrinEmitter`，这是 MACA 的 MFMA 发射器（区别于 CUDA 同名类）。

> [examples/gemm/example_gemm_intrinsics.py:80-81](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm_intrinsics.py#L80-L81) —— `warp_size = 64`（MACA 与 CUDA 的 32 不同），`threads = warp_size * (block_row_warps * block_col_warps)`。

手动构造发射器并驱动四步：

> [examples/gemm/example_gemm_intrinsics.py:89-100](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm_intrinsics.py#L89-L100) —— 构造 `TensorCoreIntrinEmitter`，传入 dtype、`b_transposed=True`（B 以转置形式存放）、warp 切分与 chunk；这正是「指令生成器 + 布局说明书」的实例化。

> [examples/gemm/example_gemm_intrinsics.py:131-142](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm_intrinsics.py#L131-L142) —— 内层 `ki` 循环里依次 `ldmatrix_a` → `ldmatrix_b` → `mma`，循环结束后 `stmatrix` 把累加器写回 C_shared。

发射器内部，MFMA 单原子的指令发射：

> [tilelang/maca/intrinsics/macro/mma_macro_generator.py:483-502](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/intrinsics/macro/mma_macro_generator.py#L483-L502) —— `mma_atom` 里调 `T.tvm_mfma(mma_suffix, "row", "row", compute_a_dtype, compute_b_dtype, compute_out_dtype, B_ptr, B_off, A_ptr, A_off, C_ptr, C_off, ...)`，把后缀（如 `16x16x16f16`）与 A/B/C 的寄存器偏移交给 builtin。

MFMA 指令后缀与 k_dim 的推导（决定算什么类型）：

> [tilelang/maca/intrinsics/macro/mma_macro_generator.py:122-147](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/intrinsics/macro/mma_macro_generator.py#L122-L147) —— `_initialize_k_dim`：按 dtype 位宽（16→k=16、tf32→k=8、int8 还看 `mcpu` 代号 serial：1000–1499→16、1500–1600→32）定 k_dim。

> [tilelang/maca/intrinsics/macro/mma_macro_generator.py:167-201](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/intrinsics/macro/mma_macro_generator.py#L167-L201) —— `_initialize_mma_prefix`：由 dtype 缩写拼出 `mmaSuffix`，如 fp16→`16x16x16f16`、tf32→`16x16x8tf32`、int8→`16x16x16i8`。

C++ 侧 `tvm_mfma` builtin 注册：

> [src/op/builtin.cc:565-566](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/builtin.cc#L565-L566) —— `tvm_mfma`（12 输入），即 MACA MFMA 指令的底层注册点。

#### 4.4.4 代码实践（可运行型）

这是本讲的主实践。**目标**：运行 `example_gemm_intrinsics.py`，记录它显式使用的 intrinsic，并对照源码说明其参数如何构造。

1. **实践目标**：确认本例子显式使用的是 MACA MFMA（`T.tvm_mfma`，后缀 `16x16x16f16`），并理解 `ldmatrix_a` 如何把 shared 数据搬进寄存器。

2. **操作步骤**：

   (a) 确认在 MACA 环境下（已按 u1-l2 安装 MACA SDK、导出 `MACA_PATH` / `LD_LIBRARY_PATH` / `PATH`，并设 `export TILELANG_DEFAULT_TARGET=maca`）。因为发射器的 `_initialize_k_dim` 要读 `target.attrs["mcpu"]`，必须以 maca 为目标编译。

   (b) 取 kernel 源码（无设备时可只看源码）：
   ```bash
   python -c "
   import tilelang
   from examples.gemm.example_gemm_intrinsics import tl_matmul
   import tilelang.language as T
   kernel = tl_matmul.compile(M=512, N=512, K=512,
       in_dtype=T.float16, out_dtype=T.float16, accum_dtype=T.float32)
   print(kernel.get_kernel_source())
   "
   ```

   (c) 在 MACA 设备上完整运行（有设备时）：
   ```bash
   python examples/gemm/example_gemm_intrinsics.py
   ```

3. **需要观察的现象**：
   - 生成的源码里应出现 `__builtin_maca_mfma`（或对应 MACA MFMA 内置）调用，其参数后缀包含 `16x16x16f16`。
   - `ldmatrix_a/b` 不会出现在最终源码里——它们已在编译期被展开成「按 `shared_16x16_to_local_64x4_layout_A` 坐标取数」的普通 load 序列。
   - `stmatrix` 被展开成「按 `mma_store_index_map` 坐标写回」的 store 序列。

4. **预期结果**：
   - 打印出的 kernel 源码非空（`assert src_code is not None` 通过）。
   - `profiler.assert_allclose(ref_program, atol=1e-2, rtol=1e-2)` 通过（数值与 `A @ B.T` 对齐）。
   - 若无 MACA 设备/SDK：步骤 (b) 可能在 `get_target_serial()` 处失败（拿不到 `mcpu`），此时应**显式传 target**，或仅做源码阅读——属「待本地验证」。

5. **对照源码说明参数构造**：
   - **intrinsic 种类**：MACA MFMA，发射自 [mma_macro_generator.py:486](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/intrinsics/macro/mma_macro_generator.py#L486) 的 `T.tvm_mfma`。
   - **后缀参数**：fp16 ⇒ `k_dim=16` ⇒ `mmaSuffix="16x16x16f16"`（由 `_initialize_k_dim` + `_initialize_mma_prefix` 构造）。
   - **A/B/C 偏移**：`a_local_stride = k_inner * warp_rows * k_pack * local_size_a`，`C_offset = inst_m_idx * warp_cols * local_size_out + inst_n_idx * local_size_out`，即按 warp 切分把每个原子定位到正确的寄存器槽位。
   - **ldmatrix 坐标**：fp16(k_dim=16) 走 `shared_16x16_to_local_64x4_layout_A/B`——64 个线程各取 4 个 fp16，拼成 16×16 fragment。

#### 4.4.5 小练习与答案

**练习 1**：把例子的 `in_dtype` 从 `float16` 改成 `int8`，`mmaSuffix` 会变成什么？需要同时改哪个参数？

**答案**：int8 时 `_initialize_mma_prefix` 走 `i8` 分支，后缀为 `16x16x16i8`（假设 mcpu 代号在 1000–1499，k_dim=16）。同时例子里 `if out_dtype == T.int32: micro_size_k = 32`，且发射器对 int8 在 1500–1600 代 GPU 会取 k_dim=32（后缀变 `16x16x32i8`），所以还与目标 `mcpu` 代号有关。

**练习 2**：这个例子为什么用 `alloc_local` 而不是 `alloc_fragment` 装 A/B？

**答案**：用 `alloc_local`（线程私有数组）意味着不走 LayoutInference 的自动布局推断，每个线程取哪些元素完全由 `ldmatrix_a/b` 的坐标函数手动决定——这正是「专家级 Level 3」要的精细控制。若用 `alloc_fragment`，则布局交给编译器自动推断（Level 2 风格），就不需要手动调 ldmatrix 了。

---

## 5. 综合实践

把本讲的三类发射器与 descriptor 串起来，完成下面这个**对照阅读任务**：

**任务**：在同一份 GEMM 计算规格下（M=N=K=4096、fp16、block_M=block_N=128、block_K=32），对照三种写法生成的 kernel 源码差异。

1. **写法 A（Level 2，自动分派）**：用 `T.gemm(A_shared, B_shared, C_local)`，分别在 `target="cuda"`（Hopper 架构 `arch=90`）和 `target={"kind":"maca"}` 下编译，用 `get_kernel_source()` 取源码。
2. **写法 B（Level 3，手动发射器）**：直接运行 `examples/gemm/example_gemm_intrinsics.py`（MACA MFMA）。
3. **对照点**（填表）：

   | 对照项 | 写法 A (cuda/Hopper) | 写法 A (maca) | 写法 B (maca 手动) |
   |--------|----------------------|---------------|--------------------|
   | 使用的 intrinsic | `wgmma.mma_async` | `tvm_mfma` | `tvm_mfma` |
   | 是否有 descriptor | 是（GMMA desc） | 否 | 否 |
   | 是否有 ldmatrix | SS 变体无 | 有（坐标展开） | 有（坐标展开） |
   | 累加器位置 | 寄存器 | 寄存器 | 寄存器（local） |
   | 同步方式 | `warpgroup_wait` | 同步 | 同步 |

4. **进阶**：把写法 A 的 `target` 换成 Blackwell（`arch=100`）并改用 `T.tcgen05_gemm(..., mbar=...)`，观察源码里是否出现 `tcgen05.mma` 与 TMEM 相关分配（需要 Blackwell 设备或只读源码——「待本地验证」）。

**交付物**：一张填好的对照表 + 一段话解释「为什么 Hopper 走 descriptor、而 MACA 走 ldmatrix + 寄存器」。

## 6. 本讲小结

- GEMM 张量核发射器分三态：**MMA**（同步、操作数在寄存器、需 ldmatrix）、**WGMMA**（Hopper 异步、shared descriptor、`warpgroup_wait`）、**TCGEN05**（Blackwell 异步、累加器在 TMEM、mbarrier 信号）。
- **ldmatrix 本质是「线程号 → shared 坐标」的查表函数**，把 shared 数据搬进寄存器凑齐 MMA fragment；坐标函数按 dtype 位宽分档（`get_ldmatrix_offset`）。
- **descriptor**（GMMA/UMMA）编码 shared 操作数的「基地址 + swizzle 模式 + LBO + SBO」，由 `compute_gmma_descriptor` / `compute_umma_descriptor` 从 CuTe 布局反推，是 WGMMA(SS)/TCGEN05 免去 ldmatrix 的关键。
- WGMMA 指令前缀由 `m64n{N}k{K}` 决定（M 恒 64、N∈[8,256]、K=256/bits）；descriptor 从 buffer 基址建、再用 `increase_descriptor_offset` 推进到切片以保持 warp 一致。
- `example_gemm_intrinsics.py` 是**专家级显式用法**：手动构造 MACA MFMA 发射器、手动调 `ldmatrix_a/b` + `mma` + `stmatrix`，用 `alloc_local` 绕过自动布局推断、实现精细控制。
- 三类发射器都遵循「指令生成器 + 布局说明书」双职责：`mma_atom`/`wgmma_ss_atom`/`tcgen05_ss_atom` 负责生成指令，`make_mma_load_layout`/`make_mma_store_layout` 负责给 LayoutInference 提供布局。

## 7. 下一步学习建议

- **u6-l3（完整 GEMM 例子精读）**：把本讲学到的「发射器内部」接回 `T.gemm` 的自动分派链路，端到端走一遍 `example_gemm.py`，看 Level 2 写法如何被编译器自动选到这些发射器。
- **u7-l3（MACA MMA intrinsics / mfma）**：本讲的 4.4 节已经触及 MACA MFMA，u7-l3 会更系统地讲 `mma_macro_generator.py` 的指令命名、`mma_layout.py` 的 shared→fragment 布局变换，以及 C++ `src/maca/op/gemm.cc` 的指令选择。
- **u4-l3 / u4-l4（布局推断与软件流水线）**：如果你想搞清楚 `make_mma_load_layout` 返回的 `T.Fragment` 是如何被 `LayoutInference` 消费的、以及 WGMMA 的异步如何与 `T.Pipelined` 重叠，回看这两讲。
- **源码延伸**：阅读 `tilelang/cuda/intrinsics/macro/tcgen05_macro_generator.py` 的 `tcgen05mma_ts` 与 `compute_tcgen05_instr_desc`，理解 Blackwell 的指令级描述符（instr_desc）与 TMEM 布局，作为通往 block-scaled GEMM（`tcgen05_gemm_blockscaled`）的台阶。
