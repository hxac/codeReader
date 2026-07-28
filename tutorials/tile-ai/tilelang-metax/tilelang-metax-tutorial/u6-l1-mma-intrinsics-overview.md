# 张量核 / MMA intrinsics 总览

## 1. 本讲目标

本讲聚焦 TileLang 的「张量核发射器」（Tensor Core emitter）体系——它是连接高层 `T.gemm` 与底层硬件张量核指令（MMA / WGMMA / TCGEN05 / MFMA）的那座桥。

学完后你应当能够：

- 说清 `tilelang/intrinsics` 模块的职责：它如何把一块 GEMM tile「翻译」成一串张量核 builtin 调用 + 配套的 fragment 布局。
- 区分各类 `TensorCoreIntrinEmitter`，并把它们对应到正确的 GPU 架构（Volta / Turing / Ampere / Hopper / Blackwell）。
- 理解 swizzle 布局函数与 `make_mma_load_layout` / `make_mma_store_layout` 的作用，把握「intrinsic 与 layout 的关系」。
- 准确描述 `tl.LowerIntrin` pass 做什么、**不**做什么（它与张量核 builtin 的边界）。

## 2. 前置知识

本讲假设你已掌握（详见依赖讲义 u4-l2、u5-l2、u5-l4）：

- **`T.gemm` 的两级分派**：C++ `ResolveGemmImpl(...).select_inst` 按 `target × dtype × 形状` 返回一个指令键（如 `cuda.mma` / `cuda.wgmma` / `cuda.tcgen05` / `rocm.mfma`），Python `resolve_gemm_impl` 再把键映射到一个实现类（`GemmMMA` / `GemmWGMMA` / `GemmTCGEN5` …）。
- **`Lower()` 与 `InferLayout()`**：每个 tile 算子的两个核心方法，分别由 `LowerTileOp` pass 与 `LayoutInference` pass 驱动。
- **fragment 与布局推断**：fragment 的逻辑下标与物理寄存器不一一对应，需由 layout 推断把 tile 分发到各线程。

几个名词先统一：

- **张量核（Tensor Core）/ MMA**：GPU 上专门做「小矩阵乘加」\(D = A \times B + C\) 的硬件单元，一条指令处理一个固定形状的「微矩阵」（如 16×8×16）。不同代际指令不同：MMA（m16n8k*）、WGMMA（m64n*k*，Hopper）、TCGEN05/UMMA（Blackwell）、MFMA（AMD CDNA）。
- **builtin**：TileLang 在 TIR 里用一类形如 `tl.ptx_wgmma_ss` 的「内置调用」占位表示一条硬件指令，最终由 codegen 印成对应的内联 PTX / 模板调用。
- **descriptor（描述符）**：WGMMA/TCGEN05 不再用寄存器直接喂操作数，而是把 shared memory 里一块数据用一个小结构（descriptor）描述给硬件，包含基地址、stride、swizzle 模式等。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tilelang/intrinsics/__init__.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/intrinsics/__init__.py) | intrinsics 模块的统一出口，导出各类发射器与布局工具 |
| [tilelang/cuda/intrinsics/macro/mma_macro_generator.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/macro/mma_macro_generator.py) | 标准 MMA 发射器 `TensorCoreIntrinEmitter` 及 ladder 变体 |
| [tilelang/cuda/intrinsics/macro/wgmma_macro_generator.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/macro/wgmma_macro_generator.py) | Hopper WGMMA 发射器与 descriptor 计算 |
| [tilelang/cuda/intrinsics/macro/tcgen05_macro_generator.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/macro/tcgen05_macro_generator.py) | Blackwell TCGEN05/UMMA 发射器与 descriptor 计算 |
| [tilelang/cuda/intrinsics/layout/mma_layout.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/layout/mma_layout.py) | shared↔fragment 的线程映射函数与 swizzle 布局 |
| [src/op/builtin.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/builtin.cc) | C++ 侧注册所有 `tl.*` builtin（含张量核 builtin）的元信息 |
| [src/transform/lower_intrin.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/lower_intrin.cc) | 通用 intrinsic 下译引擎 `tl.LowerIntrin`（`IntrinInjecter`） |
| [tilelang/cuda/op/gemm/__init__.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/op/gemm/__init__.py) | 把指令键 + 架构谓词绑定到具体 GEMM 实现类 |

## 4. 核心概念与源码讲解

### 4.1 intrinsics 模块：张量核发射器总览

#### 4.1.1 概念说明

回顾 u4-l2：当用户写 `T.gemm(A, B, C)`，编译器最终要把它降级成「真正能在 GPU 上跑的张量核指令」。这件事分两步：

1. **算子层**（`tileop/gemm`）决定「用哪一类指令」（mma / wgmma / tcgen05 / mfma），并选好实现类。
2. **发射器层**（本讲的 `tilelang/intrinsics`）由实现类持有，负责把一块 tile 的计算「展开」成具体的 TIR：一串 `T.ptx_mma(...)` / `T.ptx_wgmma_ss(...)` 等 builtin 调用，外加 `ldmatrix`、fence、commit、wait 等脚手架。

可以把发射器理解成一个「代码生成器对象」：你把 tile 的形状、dtype、warp 划分等参数喂给它，它就吐出对应的张量核指令序列。它存在的意义是**把与硬件强绑定的、繁琐的指令发射逻辑集中到一个地方**，让上层算子保持简洁。

> 类注释里写得很直白：`"To eliminate Python syntax within TIR Macro."`——发射器的目的就是「在 TIR 宏里消除 Python 语法」，即用 Python 对象把 TIR 构造过程封装起来。

#### 4.1.2 核心流程

每个发射器对一块 GEMM tile 做两件事（这两件事正好对应 tile 算子的两个核心方法）：

```text
                       ┌─────────────────────────────────────────┐
  tile 配置             │  TensorCoreIntrinEmitter                │
  (dtype/形状/warp) ──▶ │                                         │
                       │  职责 A: emit 指令 TIR                    │
                       │    ldmatrix_a / ldmatrix_b / mma_atom    │
                       │    stmatrix / fence / commit / wait      │
                       │     ──▶ T.ptx_mma(...) 等 builtin 调用    │
                       │                                         │
                       │  职责 B: 提供 fragment 布局               │
                       │    make_mma_load_layout  (喂 LayoutInference) │
                       │    make_mma_store_layout                │
                       └─────────────────────────────────────────┘
```

- **职责 A（发射指令）**：被 GEMM 算子的 `Lower()` 调用，产生真正的张量核 builtin 调用 TIR。这些 builtin 最终由 codegen 印成 `tl::mma_sync<>` / `tl::wgmma_ss<>` 等模板（见 u5-l4）。
- **职责 B（提供布局）**：被 GEMM 算子的 `InferLayout()` 调用，返回一个 `T.Fragment`，描述 fragment 的逻辑坐标如何分发到线程与寄存器槽位。它正是 u4-l3「Layout Inference」里 fragment 布局的来源之一。

一句话：**发射器既是「指令生成器」又是「布局说明书」**，把 intrinsic 与 layout 这两件原本耦合的事统一管理。

#### 4.1.3 源码精读

先看模块出口 [`tilelang/intrinsics/__init__.py:1-23`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/intrinsics/__init__.py#L1-L23)，它集中导出了四类发射器与若干布局工具：

- [`L6-9`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/intrinsics/__init__.py#L6-L9)：标准 MMA 发射器 `TensorCoreIntrinEmitter` 及其 ladder 变体 `TensorCoreIntrinEmitterWithLadderTransform`。
- [`L11-14`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/intrinsics/__init__.py#L11-L14)：Hopper WGMMA 发射器 `WGMMATensorCoreIntrinEmitter` 与 `WGMMADescriptorParams`。
- [`L15-18`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/intrinsics/__init__.py#L15-L18)：Blackwell TCGEN05 发射器 `TCGEN05TensorCoreIntrinEmitter` 与 `TCGEN05DescriptorParams`。
- [`L20-23`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/intrinsics/__init__.py#L20-L23)：布局工具 `get_swizzle_layout` / `make_mma_swizzle_layout`（CUDA）与 `make_mfma_swizzle_layout`（ROCm）。

注意一个容易混淆的细节：WGMMA 与 TCGEN05 的发射器**类名都叫 `TensorCoreIntrinEmitter`**，只是在各自文件里定义；`__init__.py` 用 `as` 给它们起了别名（`WGMMATensorCoreIntrinEmitter`、`TCGEN05TensorCoreIntrinEmitter`）以避免冲突。所以「类名」不能用来区分架构，要看「来自哪个文件」。

以标准 MMA 发射器为例，看它的「职责 A」如何发射一条指令。核心是 `mma_atom`，它对单个微矩阵 atom 调用 `T.ptx_mma`：

```python
# mma_macro_generator.py:575-592（节选）
@T.macro
def _atom_mma(A_local_buf, B_local_buf, C_local_buf):
    T.ptx_mma(
        accum_dtype, mma_prefix, "row", "col",
        a_dtype_abbrv, b_dtype_abbrv, accum_dtype_abbrv,
        A_local_buf.data, A_offset,
        B_local_buf.data, B_offset,
        C_local_buf.data, C_offset,
        T.bool(False),
    )
```

这里的 `mma_prefix`（如 `"m16n8k16"`）和 dtype 缩写（`fp16`/`bf16`/`tf32`…）共同决定了最终印出哪条 PTX。而 `mma()` 方法（[`L501-510`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/macro/mma_macro_generator.py#L501-L510)）只是在 `warp_rows × warp_cols` 网格上反复调用 `mma_atom`，把一个 warp 负责的整块 tile 铺满。

再看「职责 B」：[`make_mma_store_layout`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/macro/mma_macro_generator.py#L797-L867) 返回一个 `T.Fragment`，其 `forward_thread_fn` 把 fragment 的 `(i, j)` 坐标映射到「哪个线程持有它」，`forward_index_fn` 映射到「该线程的第几个寄存器槽」。这正是 u4-l3 里 Fragment 的 `forward_thread` / `replicate_size` 两维的来源。

#### 4.1.4 代码实践

**实践目标**：确认「发射器 = 指令生成 + 布局说明书」这一结论。

**操作步骤**：

1. 打开 [`tilelang/cuda/op/gemm/gemm_mma.py`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/op/gemm/gemm_mma.py)，找到 `class GemmMMA` 的 `intrin_emitter_cls = TensorCoreIntrinEmitter`（[L20-21](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/op/gemm/gemm_mma.py#L20-L21)）。
2. 在同一文件的 `lower` 里跟踪 `_make_mma_emitter`：它用 tile 参数构造一个 emitter，然后调用其发射方法。
3. 在 `infer_layout` 里找到对 `make_mma_load_layout` / `make_mma_store_layout` 的调用。

**需要观察的现象**：emitter 的发射方法（`ldmatrix_*` / `mma` / `stmatrix`）出现在 `lower` 路径，而 `make_mma_*_layout` 出现在 `infer_layout` 路径——两条路径分别由 `LowerTileOp` 与 `LayoutInference` 驱动。

**预期结果**：你会清楚看到同一个 emitter 对象在两个 pass 里扮演两种角色：给 `LowerTileOp` 提供指令 TIR，给 `LayoutInference` 提供 fragment 布局。

#### 4.1.5 小练习与答案

**练习 1**：WGMMA 发射器与 TCGEN05 发射器的类名都叫 `TensorCoreIntrinEmitter`，模块外为何不会冲突？

**参考答案**：[`tilelang/intrinsics/__init__.py:11-18`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/intrinsics/__init__.py#L11-L18) 在导入时用 `as` 重命名为 `WGMMATensorCoreIntrinEmitter` 与 `TCGEN05TensorCoreIntrinEmitter`；各自的使用方（如 `gemm_wgmma.py`、`gemm_tcgen05.py`）也是直接从各自文件导入并就地命名。

**练习 2**：发射器的 `make_mma_store_layout` 返回的对象被哪个 pass 消费？

**参考答案**：被 `tl.LayoutInference` 消费（经 GEMM 算子的 `InferLayout()` 返回），其 `forward_thread_fn` 决定 fragment 元素如何分发到线程。

### 4.2 各类 TensorCore 发射器与架构对应

#### 4.2.1 概念说明

NVIDIA 张量核指令随架构演进，每一代的最小 atom 形状与编程模型都不同：

| 架构 | compute capability | 张量核指令 | 典型 atom | 操作数来源 |
|------|-------------------|-----------|----------|-----------|
| Volta | sm_70 | MMA（初代） | m16n16k4 | 寄存器（ldmatrix） |
| Turing | sm_75 | MMA | m16n8k8 / m8n8k16 | 寄存器（ldmatrix） |
| Ampere | sm_80 | MMA | m16n8k16（fp16） | 寄存器（ldmatrix） |
| Hopper | sm_90 | **WGMMA** | m64n*k* | shared descriptor |
| Blackwell | sm_100 | **TCGEN05/UMMA** | （TMEM 累加器） | shared descriptor + TMEM |

关键演进：从 Hopper 起，指令不再逐 warp 用寄存器喂操作数，而是用 **descriptor** 直接描述 shared memory 里的一块数据，一条指令处理更大的 tile（m64 起），因此发射器要额外负责「构造 descriptor」。AMD CDNA 走的是 **MFMA**，命名与前缀都不同（详见 u7-l3）。

#### 4.2.2 核心流程

发射器在构造时根据 dtype 推出 `k_dim`，再由 `k_dim` 决定指令前缀 `mma_prefix`。以标准 MMA 为例：

\[ k_{dim} = \min\left(\frac{256}{\text{dtype.bits}},\ \text{chunk}\right) \]

即「一条指令的 K 维最多占 256 位」。得到 `k_dim` 后查表选前缀（见 4.2.3）。WGMMA 的 M 维固定为 64，N 维由 `gcd(warp_col_tiles, 256)` 决定，K 维同样按 256 位算。

发射器选哪个，**不由发射器自己决定**，而是由算子层的「指令键 + 架构谓词」决定（见 4.2.3 的 `cuda/op/gemm/__init__.py`）。

#### 4.2.3 源码精读

**标准 MMA 的前缀选择**——[`mma_macro_generator.py:118-181`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/macro/mma_macro_generator.py#L118-L181)：

```python
# L118-121：k_dim = 256 // bits，但不超过 chunk
self.k_dim = min(256 // a_dtype.bits, self.chunk)
# L157-181：按 k_dim 选 mma_prefix
if   k_dim == 8:   self.mma_prefix = "m16n8k8"    # tfloat32
elif k_dim == 16:  self.mma_prefix = "m16n8k16"   # float16/bfloat16
elif k_dim == 32:  self.mma_prefix = "m16n8k32"   # int8/fp8
elif k_dim == 64:  self.mma_prefix = "m16n8k64"   # int4/uint4
...
```

dtype 缩写表在 [`L46-65`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/macro/mma_macro_generator.py#L46-L65)（`float16→fp16`、`custom[tfloat32]→tf32` 等），累加器若是 float32 且操作数也是 float32，则改用 `tf32` 缩写（[`L139-155`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/macro/mma_macro_generator.py#L139-L155)）。

**WGMMA 的前缀选择**——[`wgmma_macro_generator.py:244-257`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/macro/wgmma_macro_generator.py#L244-L257)：M 固定 64，N 取 `gcd(warp_col_tiles, 256)`（约束在 [8,256] 且为 8 的倍数），K 仍按 256 位算，拼成 `m64n{N}k{K}`。其 descriptor 参数由 [`compute_gmma_descriptor`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/macro/wgmma_macro_generator.py#L61-L181)（CuTe `make_gmma_desc` 的移植）从 shared 布局反解出 LBO/SBO/swizzle 模式。

**TCGEN05** 走类似但独立的 [`compute_umma_descriptor`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/macro/tcgen05_macro_generator.py#L73)（CuTe `make_umma_desc` 的移植），累加器位于 TMEM。

**Volta / Turing 专用发射器**（不在顶层 `intrinsics/__init__.py` 导出，而是由对应算子直接导入）：

- Volta：[`mma_sm70_macro_generator.py:26`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/macro/mma_sm70_macro_generator.py#L26) 的 `TensorCoreIntrinEmitter`，前缀 [`m16n16k4`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/macro/mma_sm70_macro_generator.py#L114-L117)。
- Turing：[`mma_sm75_macro_generator.py:9`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/macro/mma_sm75_macro_generator.py#L9) 的 `TensorCoreIntrinEmitterSM75`，前缀 [`m16n8k8 / m8n8k16 / m8n8k32`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/macro/mma_sm75_macro_generator.py#L31-L37)。

**架构如何决定用哪个发射器**——关键在 [`tilelang/cuda/op/gemm/__init__.py:14-38`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/op/gemm/__init__.py#L14-L38)：

```python
def _match_mma_sm70(target):  return target_is_volta(target)             # Volta
def _match_mma_sm75(target):  return target_is_turing(target)            # Turing
def _match_mma(target):       return target_is_cuda(target) and not (
                                 target_is_volta(target) or target_is_turing(target))  # Ampere+
def _match_wgmma(target):     return target_is_cuda(target)              # Hopper（由 select_inst 选中）
def _match_tcgen05(target):   return target_is_cuda(target)              # Blackwell（由 select_inst 选中）

register_gemm_impl("cuda.mma_sm70", GEMM_INST_MMA,   _match_mma_sm70, GemmMMASm70)
register_gemm_impl("cuda.mma_sm75", GEMM_INST_MMA,   _match_mma_sm75, GemmMMASm75)
register_gemm_impl("cuda.mma",      GEMM_INST_MMA,   _match_mma,      GemmMMA)
register_gemm_impl("cuda.wgmma",    GEMM_INST_WGMMA, _match_wgmma,    GemmWGMMA)
register_gemm_impl("cuda.tcgen05",  GEMM_INST_TCGEN05,_match_tcgen05, GemmTCGEN5)
```

注意两层筛选：C++ `select_inst` 先按架构 + 形状 + warp 数返回指令键（Hopper 上大 tile 才返回 `cuda.wgmma`，Blackwell 才返回 `cuda.tcgen05`，否则回退 `cuda.mma`），Python `resolve_gemm_impl` 再在键内用架构谓词挑出唯一实现类（同是 `cuda.mma` 键，Volta→Sm70、Turing→Sm75、其余→GemmMMA）。每个实现类各自持有自己的 `intrin_emitter_cls`。

#### 4.2.4 代码实践

**实践目标**（即本讲核心实践）：列出 intrinsics 模块导出的各类 Emitter 类，说明它们分别对应哪个 GPU 架构。

**操作步骤**：

1. 打开 [`tilelang/intrinsics/__init__.py`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/intrinsics/__init__.py)，抄下 4 个导出的 Emitter 类及其来源文件。
2. 对每个 Emitter，打开其文件，找到决定指令前缀的方法（`_initialize_mma_prefix` / `_initialize_wgmma_prefix`）。
3. 用 [`tilelang/cuda/op/gemm/__init__.py`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/op/gemm/__init__.py#L14-L38) 的谓词，把每个 Emitter 对应到架构。补充 Volta/Turing 两个非顶层导出的发射器。

**预期结果**（对照表）：

| Emitter 类 | 来源文件 | 指令前缀（示例） | 对应架构 |
|-----------|---------|----------------|---------|
| `TensorCoreIntrinEmitter`（SM70） | `mma_sm70_macro_generator.py` | `m16n16k4` | Volta（sm_70） |
| `TensorCoreIntrinEmitterSM75` | `mma_sm75_macro_generator.py` | `m16n8k8` / `m8n8k16` | Turing（sm_75） |
| `TensorCoreIntrinEmitter` | `mma_macro_generator.py` | `m16n8k16`（fp16）等 | Ampere+（sm_80，默认 MMA） |
| `TensorCoreIntrinEmitterWithLadderTransform` | `mma_macro_generator.py` | `m16n8k16`/`m16n8k32` | Ampere+（带 ladder/反量化变换） |
| `WGMMATensorCoreIntrinEmitter` | `wgmma_macro_generator.py` | `m64n*k*` | Hopper（sm_90） |
| `TCGEN05TensorCoreIntrinEmitter` | `tcgen05_macro_generator.py` | TCGEN05/UMMA | Blackwell（sm_100） |

> 说明：顶层 [`intrinsics/__init__.py`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/intrinsics/__init__.py#L6-L18) 只导出后 4 个（标准 MMA、ladder 变体、WGMMA、TCGEN05）；Volta/Turing 两个由各自的 `gemm_mma_sm70.py` / `gemm_mma_sm75.py` 直接从子模块导入，故未在顶层出现，但它们是完整架构图谱的一部分。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Hopper 上有时仍会用标准 MMA 发射器而不是 WGMMA？

**参考答案**：`select_inst` 只有在「矩阵足够大、warp 数足够、架构允许」时才返回 `cuda.wgmma`；否则回退 `cuda.mma`，于是 `resolve_gemm_impl` 选 `GemmMMA`，用标准 MMA 发射器（见 u5-l4 的回退说明）。

**练习 2**：fp16 的 `k_dim` 是多少？对应哪个 `mma_prefix`？

**参考答案**：\(k_{dim}=\min(256/16,\text{chunk})=16\)，对应 `m16n8k16`（[`mma_macro_generator.py:163-166`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/macro/mma_macro_generator.py#L163-L166)）。

### 4.3 swizzle 布局与 fragment 布局工具

#### 4.3.1 概念说明

张量核指令对操作数在 shared memory 里的排布有严格要求：一是要避免 **bank conflict**（shared memory 按 32 个 bank 分组，同一 warp 并发访问同列元素会串行化），二是 WGMMA/TCGEN05 的 descriptor 直接编码了 swizzle 模式。**swizzle** 就是用一个位运算打散元素的物理位置，让原本落在同一 bank 的列元素分散开。

发射器还需要另一类布局工具：把「shared 里的 (row, col)」映射到「fragment 里的 (thread_id, local_id)」。这类映射函数刻画了张量核指令隐含的「哪个 lane 持有哪个元素」的硬件约定。

#### 4.3.2 核心流程

- **swizzle**：对列下标做 XOR。基本形式是「行号的部分位 XOR 列块号」，使相邻行错开 bank。粒度分 32B / 64B / 128B 三档。
- **shared→fragment 映射**：一组纯函数 `(row, col) -> (thread_id, local_id)`，每个对应一种 atom 形状（如 16×8、16×16、16×32）与一种矩阵（A/B）、是否转置。发射器的 `make_mma_load_layout` / `make_mma_store_layout` 把这些底层函数封装成 `T.Fragment` 供 LayoutInference 使用。

#### 4.3.3 源码精读

**swizzle 函数**——[`get_swizzle_layout`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/layout/mma_layout.py#L203-L243)（[L203](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/layout/mma_layout.py#L203)）按行字节数自动选 32/64/128B 粒度，把列块号与行号做 XOR 重排。注释里画得很清楚（128B 档用 8×8 置换）。其封装 [`make_mma_swizzle_layout`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/layout/mma_layout.py#L244-L256) 把它包成一个 `T.Layout`，仅当 `shape[-1] * bits % 512 == 0` 时才启用 swizzle，否则返回恒等布局。

**shared→fragment 映射**示例——16×8 tile 的 A 矩阵（[L57-59](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/layout/mma_layout.py#L57-L59)）：

```python
def shared_16x8_to_mma_a_32x4_layout(i, j):
    thread_id = 4 * (i % 8) + (j % 4)
    return thread_id, 2 * (j // 4) + (i // 8)
```

含义：fragment 里坐标 `(i, j)` 的元素，由 `thread_id` 号 lane 持有，放在该 lane 的第 `2*(j//4)+(i//8)` 个槽。这就是 m16n8k 指令的硬件 lane 约定。同文件还提供 16×16、16×32 等多种形状与 A/B/转置变体（[`L76-127`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/layout/mma_layout.py#L76-L127)），发射器按 dtype 位宽选用（32b→16x8、16b→16x16、8b→16x32，见 [`mma_macro_generator.py:709-719`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/macro/mma_macro_generator.py#L709-L719)）。

**WGMMA/TCGEN05 的布局**不再是手写 lane 表，而是交给 CuTe 分析器：`compute_gmma_descriptor` / `compute_umma_descriptor` 接受任意「WGMMA/UMMA 规范」的 shared 布局，从中解码出 swizzle 模式与 LBO/SBO，并用断言校验规范性（对应 CuTe 的 `static_assert`）。这正衔接 u4-l3 的 Layout/SwizzleMode 抽象。

#### 4.3.4 代码实践

**实践目标**：理解 swizzle 如何消除 bank conflict，并看清「intrinsic 与 layout 的绑定」。

**操作步骤**：

1. 阅读 [`get_swizzle_layout`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/layout/mma_layout.py#L203-L243) 注释里的 8×8 置换图（128B 档）。
2. 在 [`mma_macro_generator.py:670-719`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/macro/mma_macro_generator.py#L670-L719) 的 `make_mma_load_layout` 里，确认 dtype 位宽如何决定选用哪个 `shared_*_layout_sr_a` 函数。
3. （可选）结合 examples/plot_layout 可视化一个 GEMM fragment 的布局（详见 u8-l2）。

**需要观察的现象**：dtype 越窄（如 int8），同一 atom 容纳的列数越多（16→32 列），对应的映射函数也不同——这是「intrinsic 形状决定 layout」的直接体现。

**预期结果**：你能说清「选哪条张量核指令（intrinsic）→ 决定操作数必须排成哪种 lane 布局（layout）」这条因果关系，并解释 swizzle 为何能改善 bank conflict。

**待本地验证**：plot_layout 的具体可视化效果需本地运行 examples/plot_layout 脚本观察。

#### 4.3.5 小练习与答案

**练习 1**：`make_mma_swizzle_layout` 在什么条件下返回恒等（不 swizzle）布局？

**参考答案**：当 `shape[-1] * DataType(dtype).bits % 512 != 0`（行宽不足 64B）或显式 `is_smooth=True` 时，返回恒等布局（[`L248-250`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/intrinsics/layout/mma_layout.py#L248-L250)）。

**练习 2**：WGMMA 为何不再需要手写的 lane 映射表？

**参考答案**：WGMMA 用 descriptor 直接描述 shared 里的数据块，lane 分发由硬件按 descriptor 完成；发射器只需用 CuTe 分析器从 shared 布局解出 descriptor 参数（`compute_gmma_descriptor`），不再逐 lane 指定。

### 4.4 lower_intrin pass：通用 intrinsic 下译引擎

#### 4.4.1 概念说明

这里要澄清一个**关键边界**（也是本讲最易混淆处）：

- 本讲 4.1～4.3 讲的张量核 builtin（`tl.ptx_mma`、`tl.ptx_wgmma_ss`、`tl.tvm_mfma` 等）**不是**由 `tl.LowerIntrin` 下译的。它们由发射器产生为 TIR `Call`，再由 codegen 直接印成模板/PTX（见 u5-l3、u5-l4）。
- `tl.LowerIntrin` 处理的是**通用** intrinsic 与算术：快速数学（`tl.__expf`）、warp shuffle（`tl.shfl_sync`）、FMA 融合（\(a*b+c\)）、整数 `floordiv`/`floormod` 下译等。它的下译规则由各后端的 intrin_rule 通过 `<target>.FLowerIntrinsic` 属性表注入（u5-l3 已述）。

换句话说：**张量核指令走「发射器 → codegen」专线；通用 intrinsic 走「LowerIntrin → attr map」总线**。两者井水不犯河水。

#### 4.4.2 核心流程

`tl.LowerIntrin` 的核心是 `IntrinInjecter`（一个带算术分析器的 IR mutator）。它对每个 `Call` 节点，按一组「模式」依次查属性表，命中即改写：

```text
对每个 Call 节点:
  for 模式 in [<target>.FLowerIntrinsic, <target>.FLegalize,
               (可选) <target>.aarch64.*, default.FLowerIntrinsic, default.FLegalize]:
      若该模式注册了此 op 的下译函数 f:
          r = f(call)
          若 r != call: 递归访问 r 并返回   # 命中即短路
  否则保持原样
```

除 `Call` 外，它还拦截 `Add`（做 FMA 融合）、`FloorDiv`/`FloorMod`（下译为 `truncdiv`/`truncmod` 或位移）等节点。

#### 4.4.3 源码精读

**构造时收集属性表**——[`lower_intrin.cc:52-69`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/lower_intrin.cc#L52-L69)：先放 target 专属模式，再放 `default.*` 兜底，aarch64 triple 额外插入 `.aarch64.*`。`fma_` 取首个表里 `tirx.fma` 的规则，供 FMA 融合使用。

**Call 节点的下译**——[`lower_intrin.cc:72-90`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/lower_intrin.cc#L72-L90)：遍历各属性表，命中则调用注册函数改写，并递归访问结果（因为改写后的表达式可能再含可下译的 op）。

**FMA 融合**——[`lower_intrin.cc:92-99`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/lower_intrin.cc#L92-L99)：遇到 `a + (b*c)` 或 `(b*c) + a` 且为浮点时，经 `fma_` 改写成 `tirx.fma(a,b,c)`，再由后续规则下译为后端 FMA 指令。

**pass 注册**——[`lower_intrin.cc:417-435`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/lower_intrin.cc#L417-L435)：`LowerIntrin()` 是一个 PrimFunc pass，从函数的 target 属性取出 `kind->name` 与 `mtriple`，构造 `IntrinInjecter` 改写函数体；并以 `tl.transform.LowerIntrin` 注册到 FFI。

与之配套，[`src/op/builtin.cc`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/builtin.cc) 用 `TIR_DEFINE_TL_BUILTIN` 宏（[L56-62](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/builtin.cc#L56-L62)）集中登记所有 `tl.*` op 的元信息（参数个数、调用副作用）。其中张量核相关 builtin 包括 `ptx_mma_sm70`（[L293](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/builtin.cc#L293)）、`ptx_ldmatrix`（[L298](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/builtin.cc#L298)）、`ptx_wgmma_ss/rs`（[L233-241](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/builtin.cc#L233-L241)）、`ptx_tcgen05_mma_ss/ts`（[L253-261](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/builtin.cc#L253-L261)）、AMD 的 `tvm_mfma`（[L565](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/builtin.cc#L565)）/`tvm_rdna_wmma`（[L573](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/builtin.cc#L573)），以及 descriptor 构造 `initialize_wgmma_descriptor`（[L588](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/builtin.cc#L588)）/`initialize_tcgen05_descriptor`（[L593](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/builtin.cc#L593)）。注意这些只是「op 元信息登记」，**它们的实际改写/印码不在 LowerIntrin 里**——这正好印证 4.4.1 的边界。

> 补充：标准 `ptx_mma`（sm75+）与 `ptx_ldmatrix` 本身来自定制版 TVM 的 `tirx.builtin`（经 [`tilelang/language/ast/ir.py:1883`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/ast/ir.py#L1883) 暴露），故未在 tilelang 的 builtin.cc 重复注册。

#### 4.4.4 代码实践

**实践目标**：验证「LowerIntrin 不碰张量核 builtin」这一边界。

**操作步骤**：

1. 阅读 [`lower_intrin.cc:72-90`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/lower_intrin.cc#L72-L90)，确认它只对「注册了 FLowerIntrinsic/FLegalize 规则的 op」改写。
2. 在 [`src/op/builtin.cc`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/builtin.cc) 搜索 `ptx_wgmma_ss`、`tvm_mfma`，确认它们只登记了 `set_num_inputs`/`TCallEffectKind`，**没有**绑定任何 `FLowerIntrinsic` 规则。
3. 对照 u5-l3：intrin_rule 给 `tirx.*` 通用算子（如 `rsqrt`、`__shfl_sync`）注册的才是 `<target>.FLowerIntrinsic`。

**需要观察的现象**：张量核 builtin 没有 FLowerIntrinsic 规则，因此 LowerIntrin 对它们是「no-op」，它们原样流到 codegen。

**预期结果**：你能明确说出「LowerIntrin 管通用 intrinsic（经 attr map），张量核 builtin 管发射器→codegen」这条分工。

#### 4.4.5 小练习与答案

**练习 1**：`tl.__expf`、`tl.shfl_sync` 这类 op 在哪里被改写成后端内置函数？

**参考答案**：在 `tl.LowerIntrin`（`IntrinInjecter`）里，由各后端 intrin_rule 注册的 `<target>.FLowerIntrinsic` 规则改写（如 `__expf`→`__expf`、`shfl_sync`→`__shfl_sync`，详见 u5-l3）。

**练习 2**：`a*b+c`（浮点）在 LowerIntrin 里会发生什么？

**参考答案**：`VisitExpr_(AddNode)`（[L92-99](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/lower_intrin.cc#L92-L99)）识别出乘法子表达式，用 `fma_` 改写成 `tirx.fma(a,b,c)`，再递归下译为后端 FMA 指令。

## 5. 综合实践

**任务**：把本讲四个模块串起来，画出「从 `T.gemm` 到一条张量核 PTX」的完整数据流，并标注每一步由谁负责。

**要求**：

1. 从 [`tilelang/cuda/op/gemm/__init__.py`](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/op/gemm/__init__.py#L14-L38) 选定一个具体场景（例如：Hopper + fp16 + 大 tile → WGMMA；或 Ampere + fp16 → 标准 MMA），明确最终用哪个发射器。
2. 在图上标出四个角色及其依据：
   - **select_inst**（C++，u4-l2）：返回指令键。
   - **resolve_gemm_impl + 架构谓词**（本讲 4.2）：选出实现类与发射器。
   - **发射器职责 A**（本讲 4.1）：`Lower()` 里产生 builtin 调用；**发射器职责 B**：`InferLayout()` 里提供 fragment 布局（含 swizzle，本讲 4.3）。
   - **codegen + tl_templates**（u5-l3/u5-l4）：把 builtin 印成 `tl::xxx<>` 模板字符串。
3. 在图边明确标注：`tl.LowerIntrin`（本讲 4.4）只处理通用 intrinsic，**不**经过这条张量核专线。
4. 写出你选定场景下，发射器构造出的 `mma_prefix`（或 WGMMA 的 `m64n*k*`）、选用的 shared→fragment 映射函数、以及是否启用 swizzle。

**预期产出**：一张数据流图 + 一段对应你选定场景的具体参数说明。这能检验你是否真正理解了「发射器 = 指令生成 + 布局说明书」以及「张量核专线 vs LowerIntrin 总线」的分工。

## 6. 本讲小结

- `tilelang/intrinsics` 是张量核发射器的家，负责把 GEMM tile 展开成张量核 builtin 调用 TIR（职责 A），并提供配套的 fragment 布局（职责 B）。
- 顶层导出 4 个发射器：标准 MMA、ladder 变体、WGMMA（Hopper）、TCGEN05（Blackwell）；加上 Volta/Turing 两个专用发射器，覆盖 sm_70→sm_100 全系。
- 用哪个发射器由「C++ select_inst 返回的指令键 + Python 架构谓词」共同决定；指令前缀由 `k_dim = min(256/bits, chunk)` 推出。
- swizzle 布局消除 shared memory bank conflict；shared→fragment 映射函数刻画张量核的硬件 lane 约定，WGMMA/TCGEN05 改用 CuTe 解 descriptor。
- **关键边界**：张量核 builtin 走「发射器 → codegen」专线，`tl.LowerIntrin` 只下译通用 intrinsic（fast-math、shuffle、FMA、floordiv），两者互不干涉。

## 7. 下一步学习建议

- **u6-l2 GEMM intrinsics 深入**：动手跑 `examples/gemm_intrinsics.py`，对照本讲看清 WGMMA descriptor 的 LBO/SBO/swizzle 参数如何被 `compute_gmma_descriptor` 算出。
- **u7-l3 MACA MMA intrinsics（mfma）**：把本讲的「发射器 + 布局」框架迁移到 MACA 后端，看 `T.tvm_mfma` 与 `mma_layout`（MACA 版，warp_size=64）的差异。
- **重读 u5-l4 tl_templates**：现在再读 `tl::mma_sync` / `tl::wgmma_ss` 模板，你会看清「发射器产生的 builtin 调用」如何被 codegen 印成那一行模板字符串。
