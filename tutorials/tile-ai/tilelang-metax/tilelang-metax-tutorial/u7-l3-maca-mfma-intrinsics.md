# MACA MMA intrinsics（mfma）

## 1. 本讲目标

本讲是 U7 Metax/MACA 后端系列的第三篇，承接 [u7-l1（MACA 后端架构总览）](./u7-l1-maca-backend-overview.md) 与 [u6-l1（张量核 / MMA intrinsics 总览）](./u6-l1-mma-intrinsics-overview.md)，钻入 MACA 后端最核心的计算零件——**mfma（Matrix Fused-Multiply-Add）张量核指令**的发射与布局。

学完后你应当能够：

1. 说清「用户写 `T.gemm` → 最终生成 `__builtin_mxc_mma_*`」这条链路上，Python 侧 `TensorCoreIntrinEmitter` 与 C++ 侧 `gemm.cc` 各自做了什么。
2. 根据**输入 dtype** 推出 `k_dim`，再推出 `mma_suffix`（如 `float16 → 16x16x16f16`、`float32 → 16x16x8tf32`）。
3. 理解 `warp_size = 64` 如何决定 shared→fragment 的 lane 映射（64 个线程各持几个元素）。
4. 读懂 `mma_layout.py` 里那一族 `shared_NxK_to_local_64xM_*` 布局函数的物理含义，以及它们如何被 `LayoutInference` 消费。
5. 把握 C++ `Gemm::SelectInst` 的指令选择与 `ComputeWarpPartition` 的 warp 切分。

---

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**第一，什么是 mfma。** MetaX GPU（MACA 架构）的张量核指令叫 mfma，类比 AMD CDNA 的 `mfma`、NVIDIA 的 `mma`/`wgmma`。一条 mfma 指令在硬件上完成一个小矩阵乘加 \[ C += A × B \]，其中 A、B、C 是固定形状的「瓦片」（tile）。本讲反复出现的形状是 `16×16` 的输出瓦片，配合不同的 K 维深度（`k_dim`）。

**第二，为什么发射器要操心「布局」。** 张量核指令不是「给两个地址就能算」的。一条 mfma 要求它的操作数 A、B 已经按硬件规定的「线程—寄存器」排列方式躺在 64 个线程的寄存器里：第 `t` 号线程必须持有操作数的特定几个元素。因此发射器有两项职责（与 u6-l1 的结论一致——「发射器 = 指令生成器 + 布局说明书」）：

- **指令生成**：把一个 tile 展开成 `T.tvm_mfma(...)` 的 TIR 调用；
- **布局说明**：用 `make_mma_load_layout` / `make_mma_store_layout` 返回 `T.Fragment`，告诉 `LayoutInference` 这个 fragment 该如何切分给线程。

**第三，MACA 与 CUDA 在这里的两个关键差异。** 一是 `WARP_SIZE = 64`（CUDA 为 32），所以「64 个线程覆盖一个 16×k_dim 操作数瓦片」；二是指令前缀不同——MACA 走 `__builtin_mxc_mma_`（由 mxcc 编译器识别），CUDA 走 `mma.sync` PTX。这两点贯穿全讲。

> 名词速查：**fragment**（`local.fragment` 作用域的寄存器 tile）、**lane**（warp 内的线程号，0..63）、**warp_size**（一个 warp 的线程数，MACA=64）、**builtin**（编译器内置函数）、**canonicalizer**（target 属性规范化器，u7-l1 讲过它会自动补 `mcpu`）。

---

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用途 |
| --- | --- | --- |
| `tilelang/maca/intrinsics/macro/mma_macro_generator.py` | MACA **mfma 发射器** `TensorCoreIntrinEmitter` | 4.1 / 4.2 的主角：算 `k_dim`、拼 `mma_suffix`、发射 `T.tvm_mfma`、给布局 |
| `tilelang/maca/intrinsics/layout/mma_layout.py` | shared↔fragment 的 **lane 映射纯函数**族 | 4.3：解释 16×K 操作数如何分给 64 个线程 |
| `src/maca/op/gemm.cc` | C++ 侧 GEMM **指令选择**与 warp 切分 | 4.4：`SelectInst` 恒返回 `maca.mma`、`k_n_per_warp=16` |
| `tilelang/maca/op/gemm/gemm_mma.py` | Python 侧 `GemmMMA` 实现类（`infer_layout` + `lower`） | 串联发射器与布局，生成真正下译用的 `@T.prim_func` |

辅证文件（非主角，用于补全链路）：`tilelang/maca/op/gemm/__init__.py`（注册）、`tilelang/language/tir/op.py`（`T.tvm_mfma` 定义）、`src/op/builtin.cc`（`tl.tvm_mfma` 注册）、`src/maca/codegen/codegen_maca.cc`（把 `tvm_mfma` 印成 `__builtin_mxc_mma_*`）。

---

## 4. 核心概念与源码讲解

### 4.1 mfma 发射器：TensorCoreIntrinEmitter

#### 4.1.1 概念说明

`TensorCoreIntrinEmitter` 是 MACA 张量核的 Python 侧发射器。它**不直接运行计算**，而是在编译期被调用，产出两样东西：

- 一段 TIR（`T.tvm_mfma(...)` 调用），描述「搬数 + 算 + 存回」；
- 若干 `T.Fragment` 布局，供 `LayoutInference` pass 把 fragment 的逻辑坐标映射到「线程号 + 线程内局部槽位」。

它的「原子粒度」是 **一个 16×16 的输出瓦片**——这是 MACA mfma 的基本输出形状。类常量固化了这一点：

[mma_macro_generator.py:43-45](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/intrinsics/macro/mma_macro_generator.py#L43-L45) —— 锁定 `M_DIM = N_DIM = 16`、`WARP_SIZE = 64`，这是 MACA 与 CUDA（`WARP_SIZE=32`）最硬编码的区别。

#### 4.1.2 核心流程

发射器在 `__init__` 里跑一连串 `_initialize_*`，把后续要用到的派生量全部预算好：

1. `_initialize_k_dim(a_dtype)` —— 由输入 dtype + MetaX 卡号序号推出 K 维深度 `k_dim`；
2. `_initialize_abbrev(...)` —— 取 dtype 的显示缩写（仅供诊断）；
3. `_initialize_local_size(16, 16, k_dim, 64)` —— 算每个线程持多少个 A/B/C 元素；
4. `_initialize_mma_prefix(k_dim)` —— 拼 `mma_suffix` 字符串；
5. `_initialize_micro_size(...)` —— `micro_size_x = micro_size_y = 16`、`micro_size_k = k_dim`。

之后 `GemmMMA.lower()` 会调用发射器的三个动作方法 `ldmatrix_a` / `ldmatrix_b` / `mma`（以及存回时的 `stmatrix`）生成 TIR。

#### 4.1.3 源码精读

构造函数与派生量初始化：

[mma_macro_generator.py:88-113](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/intrinsics/macro/mma_macro_generator.py#L88-L113) —— 保存 dtype 与 warp 提示信息，依次跑 `_initialize_k_dim`、`_initialize_local_size`、`_initialize_mma_prefix` 等；最后 `self.threads = 64 × (block_row_warps × block_col_warps) × reduce_k`，即整个线程块要发射的总线程数。

每个线程持有的元素数由「操作数瓦片面积 / 64」给出：

[mma_macro_generator.py:149-152](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/intrinsics/macro/mma_macro_generator.py#L149-L152)

\[ \text{local\_size\_a} = \frac{M_{\text{DIM}} \times k_\text{dim}}{\text{WARP\_SIZE}} = \frac{16 \times k_\text{dim}}{64} = \frac{k_\text{dim}}{4} \]

于是 `k_dim=16 → local_size_a=4`、`k_dim=32 → local_size_a=8`、`k_dim=8 → 2`、`k_dim=4 → 1`。输出瓦片恒为 16×16，故 `local_size_out = (16×16)/64 = 4`，与 k_dim 无关。

发射单条 mfma 指令的「原子」方法 `mma_atom`——这是整条链路的 TIR 产出点：

[mma_macro_generator.py:483-502](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/intrinsics/macro/mma_macro_generator.py#L483-L502) —— 在 `T.macro` 里对 `k_pack` 做 grid，调用 `T.tvm_mfma(mma_suffix, "row", "row", compute_a_dtype, compute_b_dtype, compute_out_dtype, B_ptr, b_offset, A_ptr, a_offset, C_ptr, c_offset)`。注意它把**向量化的 dtype**（如 `float16x4`）作为操作数类型传进去，让 codegen 知道一条指令吃几个元素。

> `mma()` 是 `mma_atom()` 的批量包装：对 `(warp_rows, warp_cols)` 的 grid 逐格调用 `mma_atom`（[mma_macro_generator.py:417-422](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/intrinsics/macro/mma_macro_generator.py#L417-L422)）。即一个 warp 负责的输出子块由若干个 16×16 原子拼成。

#### 4.1.4 代码实践（源码阅读型）

**目标**：弄清「构造一个发射器后，它内部到底预算了哪些派生量」。

**步骤**：

1. 打开 `mma_macro_generator.py`，从 `__init__`（L69）读到 L113，列出构造完成后 `self` 上多了哪些属性。
2. 对 `k_dim = 16`（fp16 场景）手算 `local_size_a`、`local_size_b`、`local_size_out`、`micro_size_k`、`threads`（假设 `block_row_warps=block_col_warps=2`、`reduce_k=1`）。

**预期结果**：`local_size_a = local_size_b = 4`、`local_size_out = 4`、`micro_size_k = 16`、`threads = 64×4 = 256`。这解释了 `example_gemm_intrinsics.py` 里 `threads = warp_size * (block_row_warps * block_col_warps)` 的来历。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `local_size_out` 与 `k_dim` 无关，而 `local_size_a`/`local_size_b` 与 `k_dim` 成正比？
**答**：输出瓦片固定为 16×16，面积不变，故 64 线程均分恒为 4；而 A/B 操作数瓦片是 16×k_dim，面积随 k_dim 线性增长。

**练习 2**：`mma_atom` 里 `a_local_stride = k_inner * warp_rows * k_pack * local_size_a`（仅当 A 是 fragment 时）的作用是什么？
**答**：在 K 内层循环里，每前进一个 `k_inner` 步，A fragment 的读指针要在「warp 内所有 16×16 原子 + k_pack」的元素跨度上偏移，从而取出本步对应的操作数切片。

---

### 4.2 指令命名：mma_suffix 与 k_dim

#### 4.2.1 概念说明

MACA mfma 指令的「名字」由 `mma_suffix` 决定，它最终被拼进设备端的 `__builtin_mxc_mma_<suffix>`。命名规则是：

\[ \text{mma\_suffix} = M_{\text{DIM}} \times N_{\text{DIM}} \times k_\text{dim}\,(\text{dtype\_abbrv}) \]

例如 `16x16x16f16`、`16x16x8tf32`、`16x16x32i8`。其中 `k_dim` 是「单条指令在 K 维吃多少个元素」，由**输入位宽**决定——位宽越低，一条指令能吞吐的 K 元素越多。

> ⚠️ 注意区分两个缩写字典：类属性 `dtype_abbrv`（[L46-60](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/intrinsics/macro/mma_macro_generator.py#L46-L60)，用 `fp16` 等显示名，**不**参与命名）与 `_initialize_mma_prefix` 内部的 `in_dtype_map`（[L174-187](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/intrinsics/macro/mma_macro_generator.py#L174-L187)，用 `f16` 等，**真正**拼进 suffix）。所以 fp16 的 suffix 是 `16x16x16f16`，不是 `16x16x16fp16`。

#### 4.2.2 核心流程

`k_dim` 的推导（`_initialize_k_dim`）按位宽分档，且 8 位整型还依赖 MetaX 卡号序号：

[mma_macro_generator.py:122-147](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/intrinsics/macro/mma_macro_generator.py#L122-L147) —— 32 位中 `tfloat32`/`float32` 取 8、其余（int32 等）取 4；64 位取 4；16 位取 16；float8 系列取 32；8 位整型 `int8` 按 `serial`（从 `mcpu` 抠出的数字，如 `xcore1000 → 1000`）取 16（1000–1499）或 32（1500–1600）。

拿到 `k_dim` 后，`_initialize_mma_prefix` 拼 suffix：

[mma_macro_generator.py:190-201](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/intrinsics/macro/mma_macro_generator.py#L190-L201) —— 按输入 dtype 缩写选择分支，统一格式 `{16}x{16}x{k_dim}{abbrv}`。

汇总成表（`M_DIM=N_DIM=16`，`serial=1000` 档 int8 取 16）：

| 输入 dtype | k_dim | abbrv | mma_suffix | 设备 builtin |
| --- | --- | --- | --- | --- |
| float16 | 16 | f16 | `16x16x16f16` | `__builtin_mxc_mma_16x16x16f16` |
| bfloat16 | 16 | bf16 | `16x16x16bf16` | `__builtin_mxc_mma_16x16x16bf16` |
| float32（tf32） | 8 | tf32 | `16x16x8tf32` | `__builtin_mxc_mma_16x16x8tf32` |
| float64 | 4 | f64 | `16x16x4f64` | `__builtin_mxc_mma_16x16x4f64` |
| int8（serial 1000） | 16 | i8 | `16x16x16i8` | `__builtin_mxc_mma_16x16x16i8` |
| int8（serial 1500） | 32 | i8 | `16x16x32i8` | `__builtin_mxc_mma_16x16x32i8` |
| float8_e4m3* | 32 | f8 | `16x16x32f8` | `__builtin_mxc_mma_16x16x32f8` |
| float8_e5m2* | 32 | bf8 | `16x16x32bf8` | `__builtin_mxc_mma_16x16x32bf8` |

> 验证直觉：位宽减半 → k_dim 翻倍。16 位（16 元素）→ 8 位（32 元素）→ 一条指令吃更多 K，吞吐更高；32 位 tf32 反而只吃 8。

#### 4.2.3 源码精读：从 suffix 到设备 builtin

`T.tvm_mfma` 是把 suffix 等参数打包成 `tl.tvm_mfma` intrinsic 调用的语言入口，固定 12 个参数：

[op.py:1627-1706](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/tir/op.py#L1627-L1706) —— 参数顺序：`(dtype, shape=suffix, A_layout, B_layout, A_dtype, B_dtype, C_dtype, multiplicand_a, a_index, multiplicand_b, b_index, accumulator, c_index)`。

C++ 侧用 `TIR_DEFINE_TL_BUILTIN` 注册这个 12 入参的内置函数：

[builtin.cc:565-566](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/builtin.cc#L565-L566) —— `TIR_DEFINE_TL_BUILTIN(tvm_mfma).set_num_inputs(12)`。

最终在 MACA codegen 里，`VisitExpr_(CallNode*)` 命中 `tvm_mfma`，把 suffix 拼成设备 builtin 并印出 C 代码：

[codegen_maca.cc:2120-2138](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.cc#L2120-L2138) —— 模板 `*((C_dtype*)c_ref + c_bias) = __builtin_mxc_mma_<prefix>(*((A_dtype*)a_ref + a_bias), *((B_dtype*)b_ref + b_bias), *((C_dtype*)c_ref + c_bias));`，其中 `prefix` 就是 Python 传来的 `mma_suffix`，`A/B/C_dtype` 经 `dtype_map`（[L2093-2119](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.cc#L2093-L2119)）把向量类型（如 `float16x4`）映射到设备端向量类型（如 `float16x4`）。

> 链路全貌：`mma_atom`（Python）→ `T.tvm_mfma("16x16x16f16", ...)` → `tl.tvm_mfma` builtin → codegen 印 `__builtin_mxc_mma_16x16x16f16(...)` → mxcc 编为机器码。这条线**不经过** CUTE 模板，是 MACA dense GEMM 的主路径（见 4.4 的分派结论）。

#### 4.2.4 代码实践（本讲主实践）

**目标**：阅读 `mma_macro_generator.py`，列出支持的数据类型与对应 `mma_suffix`，并说明 `k_dim` 取值。这正是本讲规格指定的实践任务。

**步骤**：

1. 打开 `_initialize_k_dim`（L122-147）与 `_initialize_mma_prefix`（L167-201）。
2. 对每种输入 dtype，填出 `k_dim`、`abbrv`、`mma_suffix` 三列。
3. 解释 float8 系列为何统一 `k_dim=32`、`int8` 为何依赖卡号序号。

**预期结果**：见 4.2.2 的表格。float8 因 8 位宽且在 L125 被字符串特判提前 `return`，故恒为 32；int8 走到 L139-145 的 `serial` 分档，老卡（1000 系）取 16、新卡（1500 系）取 32。

#### 4.2.5 小练习与答案

**练习 1**：若把 `accum_dtype` 设为 `float32`、`a_dtype` 设为 `float16`，`mma_suffix` 会受 `accum_dtype` 影响吗？
**答**：不会。suffix 只由**输入** dtype（`a_dtype`）经 `in_dtype_map` 决定；累加类型只影响 codegen 的 `C_dtype` 映射与 `local_size_out` 的位宽处理，不进 suffix。

**练习 2**：为什么 `_initialize_k_dim` 一进来就无条件调用 `get_target_serial()`，哪怕 dtype 是 fp16？
**答**：因为 int8 分支需要 `serial`，而函数在分支判断**之前**就调用了它；这是实现上的早求值。副作用是：构造发射器时必须有合法的 maca target（带 `mcpu`），否则 `get_target_serial` 会报错（见综合实践的「待本地验证」说明）。

---

### 4.3 mma_layout：shared↔fragment 的 lane 映射

#### 4.3.1 概念说明

mfma 要求操作数按硬件规定的 lane 映射躺在寄存器里。`mma_layout.py` 用一族**纯函数**刻画这套映射，分两类：

- `shared_<R>x<C>_to_local_64x<L>_layout_<X>(i, j) -> (thread_id, local_id)`：给定操作数瓦片内的逻辑坐标 `(i, j)`，返回「该元素归第几个线程、线程内第几个槽」——**正向**（逻辑→物理）。
- `thread_id_shared_access_64x<L>_to_<R>x<C>_layout_<X>(thread_id, local_id) -> (i, j)`：**反向**，给定线程与槽位返回逻辑坐标，`ldmatrix` 用它从 shared 取数。

函数名里的数字是「物理形状」：`64` = WARP_SIZE（线程数），`16xK` = 操作数瓦片形状，`L = local_size = k_dim/4`（每线程持有元素数）。

#### 4.3.2 核心流程

以最常用的 `k_dim=16`（fp16/bf16）为例，A 操作数瓦片是 16×16，64 线程各持 4 元素：

[mma_layout.py:123-126](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/intrinsics/layout/mma_layout.py#L123-L126)

```python
def shared_16x16_to_local_64x4_layout_A(i, j):
    thread_id = i + 16 * (j // 4)
    local = j % 4
    return thread_id, local
```

直觉：行号 `i` 直接进线程号的低 4 位（16 行 → 16 个线程基），列号 `j` 按 4 一组交替分给线程内槽位与前 16 线程之外。每个 `(thread_id, local)` 唯一确定一个元素。

`k_dim` 不同则选不同函数，`get_ldmatrix_index_map` 按维度查表：

[mma_macro_generator.py:243-264](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/intrinsics/macro/mma_macro_generator.py#L243-L264) —— 按 `k_dim = self.k_dim * k_pack`（4/8/16/32）选对应的正向/反向映射，并依 `is_b` 与是否转置切换 A/B 版本。

输出瓦片 C 的 lane 映射（`thread_id_shared_access_64x4_to_16x16_layout_C_n_m`）被 `mma_store_index_map` 复用：

[layout/utils.py:4-10](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/intrinsics/layout/utils.py#L4-L10) —— 非 float64 直接走 `C_n_m` 映射；float64 因每线程持 double×4 的特殊排列另算。

#### 4.3.3 源码精读：从映射函数到 T.Fragment

发射器把上述纯函数包成 `T.Fragment`，供 `LayoutInference` 消费。`make_mma_load_layout` 构造 A/B fragment：

[mma_macro_generator.py:586-648](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/intrinsics/macro/mma_macro_generator.py#L586-L648) —— 选 `transform_func_sr_a`（按 k_dim），用 `IndexMap.from_func` 求逆得 `inverse_mma_load_layout`；`forward_thread`/`forward_index` 把逻辑坐标 `(i,j)` 经逆映射拆成「线程号 + 槽位」，再用 `base_fragment.repeat(...).replicate(...)` 沿 warp/block 维复制成整个 fragment 布局。

`make_mma_store_layout` 同理构造 C 的存回布局：

[mma_macro_generator.py:709-744](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/intrinsics/macro/mma_macro_generator.py#L709-L744) —— `forward_thread` 先把 `(i,j)` 拆成「block 内 warp 偏移 + 16×16 原子内坐标」，原子内坐标再经 `inverse_mma_store_layout` 映射到 lane，最后按 `is_m_first` 决定的线程绑定顺序拼出全局线程号。

此外，shared 端用 swizzle 消 bank conflict：

[mma_layout.py:249-271](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/intrinsics/layout/mma_layout.py#L249-L271) —— `make_mma_swizzle_layout` 按 `perPhase`/`maxPhase` 对列做 XOR swizzle（与 u4-3 的 swizzle 原理一致）。

#### 4.3.4 代码实践（源码阅读型）

**目标**：验证 lane 映射的「一一对应」性质。

**步骤**：

1. 取 `k_dim=16` 的 A 映射 `shared_16x16_to_local_64x4_layout_A`（L123）。
2. 对 `i ∈ [0,16)`、`j ∈ [0,16)` 的全部 256 个 `(i,j)`，调用该函数，把结果 `(thread_id, local)` 收集成集合。
3. 检查：`thread_id ∈ [0,64)`、`local ∈ [0,4)`，且 256 个 `(thread_id, local)` 两两不同。

**预期结果**：恰好填满 64 线程 × 4 槽 = 256 个组合，证明映射是双射——这正是 mfma 操作数能被正确分发到各线程的数学保证。（可用纸笔或一段离线 Python 验证，无需 GPU。）

#### 4.3.5 小练习与答案

**练习 1**：`k_dim=32` 时，A 操作数瓦片是 16×32，应选哪个映射函数？每线程持几个元素？
**答**：选 `shared_16x32_to_local_64x8_layout_A`（L165）；每线程持 `16×32/64 = 8` 个元素，`local ∈ [0,8)`。

**练习 2**：`forward_thread` / `forward_index` 为什么叫 forward，却用的是 `inverse_*_layout`？
**答**：命名视角不同。`shared_to_local` 是「逻辑坐标→物理线程」的正向物理映射；而从 fragment 的「线程—槽」视角看，要由逻辑坐标反查线程，相当于对该映射求逆，故变量名带 `inverse`。两者描述同一套双射的两个方向。

---

### 4.4 gemm.cc 的指令选择

#### 4.4.1 概念说明

前面三节讲「发射器会做什么」，本节回答「**什么时候**用 MACA 发射器」。指令选择分两级（与 u4-2 一致）：

- **C++ 级**：`src/maca/op/gemm.cc` 的 `Gemm::SelectInst` 决定指令键，恒为 `"maca.mma"`；`ComputeWarpPartition` 决定 warp 切分。
- **Python 级**：`resolve_gemm_impl` 把指令键 `"maca.mma"` 映射到实现类 `GemmMMA`，后者真正调用 4.1 的发射器。

#### 4.4.2 核心流程

C++ 侧的指令选择极其简单——MACA 目前**只有一种** GEMM 指令实现：

[gemm.cc:141-164](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/op/gemm.cc#L141-L164) —— `SelectInst` 无条件返回 `kMacaMMA = "maca.mma"`（[L26](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/op/gemm.cc#L26)）；`ComputeWarpPartition` 算 `num_warps = block_size / TargetMacaGetWarpSize(target)`（=64），固定 `k_n_per_warp = 16`，调 `ComputeDefaultWarpPartition`。

> 对比 CUDA：CUDA 的 `SelectInst` 会按架构在 `mma`/`wgmma`/`tcgen05` 间选；MACA 没有这层分叉，统一走 mfma。这是「MACA 后端目前只对接一种张量核」的直接体现。

warp 切分由 `GemmWarpPolicy`（Square/FullRow/FullCol）驱动，关键常量是 `kMPerWarp = 16`（M 方向）与 `k_n_per_warp = 16`（N 方向）：

[gemm.cc:69-137](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/op/gemm.cc#L69-L137) —— `ComputeDefaultWarpPartition` 先 ICHECK `M % 16 == 0`、`N % 16 == 0`，再按策略切分；Square 策略遍历 `m ∈ [1, max_m_warps]` 找使 `m_per_warp/n_per_warp` 最接近 `M/N` 理想比的 `(m_warp, n_warp)`，且满足 `m_warp × n_warp == num_warps`。

C++ 把这套选择注册进全局表：

[gemm.cc:174-185](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/op/gemm.cc#L174-L185) —— `RegisterMacaGemm()` 用 `RegisterGemmImpl` 注册 `name="maca.Gemm"`、匹配谓词 `MatchMacaGemmTarget`（`TargetIsMaca || TargetIsCuTeDSL`，[L170-172](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/op/gemm.cc#L170-L172)）。

#### 4.4.3 源码精读：Python 侧的注册与分派

Python 侧用指令键 `"maca.mma"` 注册实现类：

[gemm/__init__.py:10](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/op/gemm/__init__.py#L10) —— `register_gemm_impl("maca.mma", GEMM_INST_MMA, target_is_maca, GemmMMA)`，其中 `GEMM_INST_MMA = "maca.mma"`（[gemm_mma.py:17](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/op/gemm/gemm_mma.py#L17)）。

`resolve_gemm_impl` 按指令键 + target 谓词查表，命中 `GemmMMA`：

[registry.py:38-46](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/tileop/gemm/registry.py#L38-L46) —— 遍历 `_GEMM_IMPLS`，匹配 `inst_name == gemm_inst and predicate(target)`，要求恰好一个命中。

`GemmMMA` 把发射器与布局串起来。`infer_layout` 按 shared/fragment 组合返回布局：

[gemm_mma.py:40-67](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/op/gemm/gemm_mma.py#L40-L67) —— shared 操作数走 `make_swizzled_layout`，fragment 操作数走发射器的 `make_mma_load_layout`/`make_mma_store_layout`；输出 C 恒为 `make_mma_store_layout`。

`lower` 生成下译用的内联 `@T.prim_func`，以 ss（双 shared）路径为例：

[gemm_mma.py:104-139](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/op/gemm/gemm_mma.py#L104-L139) —— 分配 `A_local`/`B_local` fragment，按需 `T.clear(C)`，循环 `ki ∈ [0, block_K // micro_size_k)` 依次 `ldmatrix_a` → `ldmatrix_b` → `mma`，最后 `_Simplify` 化简索引。这正是 u6-2 所述「Level 2：T.gemm 自动分派 + alloc_fragment」的落地——只不过发射器换成了 MACA 版。

> 结论呼应：dense MACA GEMM 的下译路径是 `GemmMMA.lower` → `mma_emitter.mma` → `mma_atom` → `T.tvm_mfma` → codegen `__builtin_mxc_mma_*`。MACA codegen 只 `#include <tl_templates/maca/gemm_sp.h>`（稀疏路径），dense 路径不经 CUTE `gemm.h` 的 `tl::gemm_ss`——这与 CUDA 的 MMA 路径不同，是 MACA 后端的一个实现特点。

#### 4.4.4 代码实践（源码阅读型）

**目标**：理清「指令键如何在 C++ 与 Python 间传递」。

**步骤**：

1. 在 `gemm.cc` 找到 `SelectInst` 返回值（L142-144）与 `RegisterMacaGemm` 注册名（L175-181）。
2. 在 `gemm/__init__.py` 找到 Python 侧注册的 `inst_name`（L10）。
3. 确认三者用同一个字符串 `"maca.mma"` 串联。

**预期结果**：C++ `SelectInst` 返回 `"maca.mma"` → Python `resolve_gemm_impl("maca.mma", maca_target)` 命中 `GemmMMA`。指令键是跨语言握手的「契约」。

#### 4.4.5 小练习与答案

**练习 1**：`ComputeWarpPartition` 里 `k_n_per_warp = 16`，而 CUDA 路径里这个值常为 16 或 8。它受 `warp_size` 影响吗？
**答**：`k_n_per_warp` 是「每个 warp 在 N 方向负责的 tile 宽度」，由指令形状（N_DIM=16）决定，与 `warp_size` 无关；但 `num_warps = block_size / 64` 用了 MACA 的 `warp_size=64`，故同样 `block_size` 下 MACA 的 warp 数是 CUDA 的一半。

**练习 2**：`ReuseExistingSharedLayout` 对 `maca.mma` 返回 `true`（[gemm.cc:154-156](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/op/gemm.cc#L154-L156)），含义是什么？
**答**：表示普通 MMA 可以复用别的算子已经为 shared buffer 推断的布局，而不必强制自定布局——这与 WGMMA/TCGEN05（须自定布局）相反（u4-2 讲过该区别）。

---

## 5. 综合实践

**任务**：把本讲四个最小模块串成一条完整的「指令诞生」链路，并（在有 MACA 环境时）亲手构造发射器观察派生量。

### 步骤一：画出指令诞生链路图（源码阅读型，必做）

用纸笔或文本画一张图，标注每一步的文件与关键符号：

```
T.gemm(A_shared, B_shared, C_frag)          # 用户 DSL
   │  (C++) LowerTileOp 驱动
   ▼
gemm.cc :: Gemm::SelectInst  ──返回──► "maca.mma"        # 4.4
gemm.cc :: ComputeWarpPartition(warp_size=64)            # 4.4
   │  (C++→Python) resolve_gemm_impl("maca.mma")
   ▼
gemm_mma.py :: GemmMMA.lower / infer_layout              # 4.4
   │  构造 TensorCoreIntrinEmitter
   ▼
mma_macro_generator.py:                                  # 4.1
   _initialize_k_dim   (dtype → k_dim)        ── 4.2
   _initialize_mma_prefix (→ mma_suffix)      ── 4.2
   make_mma_load/store_layout (用 mma_layout) ── 4.3
   mma_atom → T.tvm_mfma(suffix, ...)         ── 4.1
   ▼
codegen_maca.cc :: __builtin_mxc_mma_<suffix>            # 4.2
```

在图上标出 `k_dim`、`mma_suffix`、`local_size_a` 在哪一步被算出、被谁消费。

### 步骤二：构造发射器观察派生量（需 MACA 环境，待本地验证）

下面这段示例代码（非项目原有代码，标注为「示例代码」）在 MACA 环境下构造发射器，打印各派生量。**注意**：`_initialize_k_dim` 会无条件调用 `get_target_serial()`，读取当前 target 的 `mcpu`，因此必须先把默认 target 设为 maca（参考 [u3-l3](./u3-l3-running-on-metax-maca.md)）。

```python
# 示例代码：需在 MACA 环境（mcpu 可解析）下运行
import os
os.environ["TILELANG_DEFAULT_TARGET"] = "maca"   # 让 determine_target 返回 maca
import tilelang.language as T
from tilelang.maca.intrinsics.macro.mma_macro_generator import TensorCoreIntrinEmitter

for in_dt in [T.float16, T.bfloat16, "float32", "float8_e4m3fn"]:
    e = TensorCoreIntrinEmitter(
        a_dtype=in_dt, b_dtype=in_dt, accum_dtype=T.float32,
        a_transposed=False, b_transposed=True,
        block_row_warps=2, block_col_warps=2,
        warp_row_tiles=64, warp_col_tiles=64, chunk=32,
    )
    print(in_dt, "→ k_dim=", e.k_dim,
          "suffix=", e.mma_suffix,
          "local_size_a=", e.local_size_a,
          "micro_size_k=", e.micro_size_k)
```

**需要观察的现象**：

- `float16 → k_dim=16, suffix=16x16x16f16, local_size_a=4`；
- `bfloat16 → k_dim=16, suffix=16x16x16bf16`；
- `float32 → k_dim=8, suffix=16x16x8tf32, local_size_a=2`；
- `float8_e4m3fn → k_dim=32, suffix=16x16x32f8, local_size_a=8`。

**预期结果**：打印结果应与 4.2.2 的表格完全一致。若 `mcpu` 无法解析（非 MACA 环境），会在 `get_target_serial` 处抛错——此时该步标注「待本地验证」，回到步骤一的源码阅读即可完成本讲。

---

## 6. 本讲小结

- **发射器双职责**：`TensorCoreIntrinEmitter` 既生成 `T.tvm_mfma` 指令（`mma_atom`），又产出 `T.Fragment` 布局（`make_mma_load/store_layout`），是 MACA 张量核的 Python 侧总装车间。
- **命名由输入 dtype 决定**：`mma_suffix = 16x16x{k_dim}{abbrv}`，`k_dim` 随位宽反向变化（16 位→16、32 位 tf32→8、8 位→32），int8 还依赖 MetaX 卡号序号。
- **设备端落地**：suffix 经 `T.tvm_mfma` → `tl.tvm_mfma` builtin → MACA codegen 印成 `__builtin_mxc_mma_<suffix>`，由 mxcc 编译。
- **布局是双射**：`mma_layout.py` 的 lane 映射把 16×k_dim 操作数一一分给 64 个线程（每线程 k_dim/4 个元素），是 mfma 能正确取数的数学基础。
- **指令选择简单**：C++ `SelectInst` 恒返回 `maca.mma`，Python `resolve_gemm_impl` 映射到 `GemmMMA`；`warp_size=64` 使同 block_size 下 warp 数减半。
- **dense 主路径不经 CUTE**：MACA dense GEMM 走 Python 发射器 + `tvm_mfma` builtin，CUTE `gemm.h` 的 `tl::gemm_ss` 不是 dense 主路径。

---

## 7. 下一步学习建议

- 想看「发射器产出的 TIR 如何被 pass 处理」：继续 [u7-l4（MACA 编译流水线与 transform）](./u7-l4-maca-pipeline.md)，其中 `LowerMACAIntrin` 会进一步处理 MACA 专属 intrinsic。
- 想横向对比三后端的张量核：回看 [u7-l5（MACA vs CUDA vs ROCm 差异对比）](./u7-l5-maca-vs-cuda-vs-rocm.md)，把本讲的 `__builtin_mxc_mma_*` 与 CUDA `mma.sync`/WGMMA、ROCm `mfma` 并排对照。
- 想动手扩展：阅读 `tilelang/maca/intrinsics/macro/mma_sp_macro_generator.py`（2:4 稀疏 mfma 发射器）与 `src/maca/op/gemm_sp.cc`，看稀疏路径如何复用本讲的布局与命名机制。
- 建议精读的源码：把 `example_gemm_intrinsics.py`（Level-3 手动发射器用法）与本讲对照，确认「手动构造 `TensorCoreIntrinEmitter` + `alloc_local` + `ldmatrix`/`mma`/`stmatrix`」的每一步都对应得上 4.1 的方法。
