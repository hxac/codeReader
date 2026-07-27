# T.gemm 与 tile op 体系

## 1. 本讲目标

矩阵乘（GEMM）是大模型里最核心、也最吃性能的算子。本讲聚焦 tilelang 中「写一行 `T.gemm`，编译器自动生成张量核（Tensor Core）指令」这一关键能力，学完后你应当能够：

- 会用 `T.gemm` 在 shared 缓冲上做分块矩阵乘，并累加到 fragment（寄存器）缓冲；
- 理解 `T.gemm` 这一行 Python 是如何变成 TIR 里的 `tl.tileop.gemm` intrinsic，再被分发到 CuTe / MMA / WGMMA / MFMA 等后端实现的；
- 读懂 `tileop/gemm` 的 registry 与 `resolve_gemm_impl` 分发机制，以及每个后端实现需要实现的 `infer_layout` / `lower` 两个钩子；
- 了解 `T.gemm_sp` 的 2:4 结构化稀疏张量核路径。

本讲对应最小模块：`tilelang.language.gemm_op` 与 `tilelang.tileop.gemm`。

## 2. 前置知识

在进入源码前，先用直觉建立三个概念。

**第一，为什么要分块（tile）。** 显存（global memory）很慢，而 GPU 片上的 shared memory 与寄存器（fragment）快得多。高性能 GEMM 的标准套路是：把大矩阵切成一个个小块，先把一小块 A 和一小块 B 从 global 搬到 shared，再搬到寄存器，在寄存器里反复乘加，最后把结果小块写回 global。这正是 u2-l2 讲过的「global → shared → fragment」数据流。

**第二，为什么要张量核。** 普通的 CUDA core 一次只能算一个标量的乘加；而张量核（NVIDIA 的 MMA / WGMMA / TCGEN5MMA、AMD 的 MFMA）一条指令就能算一个小矩阵块（例如 16×16×16）的乘加。手写这些指令极其繁琐，`T.gemm` 的价值就是让你用一行高级语义去换取这条底层指令。

**第三，什么是 tile op。** 在 tilelang 里，`T.gemm`、`T.copy` 这类「tile 级操作」并不是普通的函数调用，而是被翻译成一种特殊的 TIR 节点——`call_intrin`，对应的 op 叫 `tl.tileop.gemm`。这个节点在 IR 里只是一个「占位的、语义级的高层操作」，真正展开成张量核指令是在编译后期的 `lower_tile_op` Pass 里完成的。这种「先留语义占位、后按目标硬件展开」的设计，就是 tile op 体系的核心。

如果你对 `@T.prim_func`、`T.Kernel`、`T.alloc_shared/alloc_fragment`、`T.copy` 还不熟悉，请先学习 u2-l1、u2-l2。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tilelang/language/gemm_op.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/gemm_op.py) | DSL 用户面：`gemm`/`wgmma_gemm`/`tcgen05_gemm` 等 Python 入口，把调用翻译成 `tl.tileop.gemm` intrinsic |
| [tilelang/tileop/gemm/\_\_init\_\_.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tileop/gemm/__init__.py) | `Gemm` IR 节点的 Python 包装：暴露 `infer_layout` / `lower` 钩子与指令键选择 |
| [tilelang/tileop/gemm/registry.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tileop/gemm/registry.py) | 后端实现的注册表：`register_gemm_impl` 注册、`resolve_gemm_impl` 按 target 分发 |
| [tilelang/tileop/gemm/gemm_base.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tileop/gemm/gemm_base.py) | 所有后端实现的公共基类 `GemmBase`：按 A/B 的 scope 分类（SS/SR/RS/RR/TS）并提供属性访问 |
| [tilelang/tileop/base.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tileop/base.py) | `GemmWarpPolicy` 枚举（Square/FullRow/FullCol）与 warp 切分算法 |
| [tilelang/cuda/op/gemm/\_\_init\_\_.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/op/gemm/__init__.py) | CUDA 后端的 5 个实现注册（mma / mma_sm70 / mma_sm75 / wgmma / tcgen05） |
| [tilelang/cuda/op/gemm/gemm_mma.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/op/gemm/gemm_mma.py) | 一个具体的后端实现样例：`GemmMMA` 的 `infer_layout` 与 `lower` |
| [tilelang/language/experimental/gemm_sp_op.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/experimental/gemm_sp_op.py) | 稀疏 DSL 入口 `gemm_sp` |
| [src/op/gemm.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/gemm.cc) | C++ 侧 `GemmNode`：注册 `tl.tileop.gemm` op、指令键选择入口、InferLayout/Lower 调度 |
| [examples/gemm/example_gemm.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/gemm/example_gemm.py) | 最简洁的 `T.gemm` 用法示例 |
| [examples/gemm_sp/example_gemm_sp.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/gemm_sp/example_gemm_sp.py) | 2:4 稀疏 GEMM 示例 |

一句话数据流：**用户写 `T.gemm(A_shared, B_shared, C_local)` → `gemm_op.gemm()` 构造 `tl.tileop.gemm` intrinsic 节点 → layout_inference Pass 调 `Gemm.infer_layout` 推导布局 → lower_tile_op Pass 调 `Gemm.lower` 展开成张量核指令序列。**

## 4. 核心概念与源码讲解

### 4.1 T.gemm 的用法与参数语义

#### 4.1.1 概念说明

`T.gemm` 是 tilelang 暴露给用户的「同步」矩阵乘入口：你给它两个输入缓冲 A、B 和一个输出（累加）缓冲 C，它就在 tile 级别上完成

\[ C \mathrel{+}= A \times B \]

注意这里写的是「+=」，即默认行为是**累加**到 C 已有的值上，而不是覆盖。这对应参数 `clear_accum=False`（默认）。如果你想要一个干净的开始，就要在调用前用 `T.clear(C)` 把 C 清零——这正是累加型 GEMM 的标准写法。如果你想让它自己清零，可以传 `clear_accum=True`。

`T.gemm` 还提供一组语义相关的参数：

- `transpose_A` / `transpose_B`：是否对输入做转置（影响 K 维落在哪个轴）；
- `policy`：warp 在 M/N 两个方向上如何切分（`GemmWarpPolicy.Square/FullRow/FullCol`）；
- `k_pack`：ROCm 专用，控制 K 维打包个数；
- `mbar`：Blackwell TCGEN5MMA 信号屏障，仅在显式异步路径需要。

「同步」的含义是：编译器在 Hopper 上若选了 WGMMA，会**自动**在后面插入 `warpgroup_wait`；在 Blackwell 上若选了 TCGEN5MMA，会自动插入 `mbarrier_wait_parity`。你不用自己管同步。如果想要完全手动调度的异步版本，另有 `T.wgmma_gemm` / `T.tcgen05_gemm`，它们**不**自动插 wait、且强制走对应路径（不支持就编译失败，不悄悄回退）。

#### 4.1.2 核心流程

以累加型分块 GEMM 为例，每个线程块做的工作是：

1. 在 shared 里分配 `A_shared`、`B_shared`，在 fragment 里分配 `C_local`；
2. `T.clear(C_local)`：清零累加器；
3. 沿 K 轴遍历各个 K-tile（通常套 `T.Pipelined` 做软件流水线）：
   - `T.copy` 把当前 K-tile 的 A、B 从 global 搬到 shared；
   - `T.gemm(A_shared, B_shared, C_local)`：\(C_{\text{local}} \mathrel{+}= A_{\text{shared}} \times B_{\text{shared}}\)；
4. `T.copy(C_local, C[...])`：把累加结果写回 global。

数学上，分块矩阵乘把

\[ C_{ij} = \sum_{k=0}^{K-1} A_{ik}\, B_{kj} \]

改写为沿 K-tile 的累加：

\[ C^{\text{tile}} \mathrel{+}= \sum_{t} A^{(t)}_{\text{shared}} \cdot B^{(t)}_{\text{shared}}, \quad C^{\text{tile}} \in \mathbb{R}^{\text{block\_M}\times\text{block\_N}} \]

每个 `T.gemm` 调用就是上式里的一次「块乘加」，最终多个 K-tile 累加得到完整结果。

#### 4.1.3 源码精读

最干净的用法在示例里：[examples/gemm/example_gemm.py:L13-L26](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/gemm/example_gemm.py#L13-L26) 是一个完整 kernel。关键三行是：

```python
T.clear(C_local)                                   # 1) 先清零累加器
for k in T.Pipelined(T.ceildiv(K, block_K), num_stages=3):
    T.copy(A[by * block_M, k * block_K], A_shared) # 2) 搬数据
    T.copy(B[k * block_K, bx * block_N], B_shared)
    T.gemm(A_shared, B_shared, C_local)            # 3) 块乘加（默认累加）
```

注意 `A_shared`、`B_shared` 是 shared 缓冲，`C_local` 是 fragment 缓冲，这种「SS 入 + R 出」的组合正是张量核最典型的数据布局。

`T.gemm` 的入口定义在 [tilelang/language/gemm_op.py:L149-L198](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/gemm_op.py#L149-L198)。它的函数体极其简短，只是把参数转交给内部实现 `_gemm_impl`，并固定 op key 为 `"tl.tileop.gemm"`：

```python
def gemm(A, B, C, transpose_A=False, transpose_B=False,
         policy=GemmWarpPolicy.Square, clear_accum=False,
         k_pack=1, mbar=None):
    return _gemm_impl("tl.tileop.gemm", A, B, C, transpose_A, transpose_B,
                      policy, clear_accum, k_pack, 0, mbar)
```

它的文档串（[gemm_op.py:L160-L185](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/gemm_op.py#L160-L185)）清楚说明了「同步语义、自动插 wait」的承诺，以及「手动异步请用 `T.wgmma_gemm` / `T.tcgen05_gemm`」。

`policy` 参数的类型 `GemmWarpPolicy` 是一个枚举，定义在 [tilelang/tileop/base.py:L5-L12](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tileop/base.py#L5-L12)：`Square=0`（行/列均衡）、`FullRow=1`（warp 全给行）、`FullCol=2`（warp 全给列）。`compute_warp_partition`（[base.py:L65-L158](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tileop/base.py#L65-L158)）根据 M、N 与 warp 总数算出 `(m_warp, n_warp)` 二元组，决定每个 warp 负责输出块的哪一块。

#### 4.1.4 代码实践

**实践目标：** 体会 `policy` 与 `clear_accum` 对生成 kernel 的影响。

**操作步骤：**

1. 复制 `examples/gemm/example_gemm.py` 为本地 `my_gemm.py`；
2. 第一次运行原版，用 `kernel.get_kernel_source()` 打印生成的 CUDA 源码；
3. 把 `T.gemm(A_shared, B_shared, C_local)` 改成 `T.gemm(A_shared, B_shared, C_local, policy=T.GemmWarpPolicy.FullRow)`，再次打印源码；
4. （可选）把 `T.clear(C_local)` 删掉，并把 `T.gemm(...)` 改成 `T.gemm(..., clear_accum=True)`，验证结果仍然正确。

**需要观察的现象：** 改 `policy` 后，生成源码中 warp 划分 / `mma` 指令的排布会变化；用 `clear_accum=True` 时源码里会出现一次清零动作（由 op 自己负责）。

**预期结果：** 三种写法的数值结果都与 `a @ b`（torch 参考）一致；`policy` 的差异主要体现在性能与寄存器/线程映射上。

> 待本地验证：具体生成的 CUDA 指令文本与延迟数值取决于你的 GPU 架构（sm_70/75/80/90…），请在本机实跑确认。

#### 4.1.5 小练习与答案

**练习 1：** 为什么累加型 GEMM 通常要先 `T.clear(C_local)` 再循环里调 `T.gemm`？
**答案：** `T.gemm` 默认 `clear_accum=False`，即把本次块乘加的结果**累加**到 C 已有值上。若不清零，C 里的初始垃圾值会被加进最终结果。所以要么先 `T.clear`，要么显式传 `clear_accum=True`。

**练习 2：** `T.gemm` 和 `T.wgmma_gemm` 的关键区别是什么？
**答案：** `T.gemm` 是同步接口：编译器若选了 WGMMA 路径会**自动**插入 `warpgroup_wait`，且允许编译器按目标硬件自由选择 MMA/WGMMA 等路径。`T.wgmma_gemm` 是显式异步接口：强制走 WGMMA、不自动插 wait（需用户自己 `T.wait_wgmma`），若硬件不支持则直接编译失败而不回退。

---

### 4.2 从 T.gemm 到 tl.tileop.gemm：tile op 的「占位」本质

#### 4.2.1 概念说明

`T.gemm(A, B, C)` 在 Python 层并不是真的「执行」了一次矩阵乘，而是向正在构建的 TIR 里**注入一个语义占位节点**——一个 op 为 `tl.tileop.gemm` 的 `call_intrin`。这个节点携带了 M/N/K、转置、stride、policy、clear_accum 等全部语义信息，但**不包含任何具体指令**。

为什么这样做？因为「用哪条张量核指令」取决于：目标硬件（Hopper 用 WGMMA、Blackwell 用 TCGEN5MMA、Ampere 用 MMA、AMD 用 MFMA）、A/B 的 scope 组合（shared 还是 register）、布局推理结果（swizzle 后的排布）等。这些信息在「写 DSL 时」还没确定。所以 tilelang 选择：DSL 阶段只留语义占位，等到 layout 推理和 `lower_tile_op` Pass 时再展开成具体指令。这就是 tile op 与普通 TIR 表达式的根本区别。

#### 4.2.2 核心流程

`_gemm_impl` 做的事可以概括为「校验 + 归一化 + 生成 intrinsic」：

1. **legalize**：如果 A/B/C 是被 `let` 绑定的变量，还原成底层 buffer；
2. **取形状/步长/偏移**：把参数归一化成 `BufferRegion`，抽出 M、N、K、stride、offset；
3. **形状一致性校验**：检查 A、B、C 的 M/N/K 是否自洽（含转置与 2CTA 特例）；
4. **生成 intrinsic**：构造一个 `tirx.call_intrin("handle", Op.get("tl.tileop.gemm"), ...)`，把所有语义信息作为参数塞进去。

伪代码：

```
_gemm_impl(op_key, A, B, C, ...):
    A, B, C = legalize(A), legalize(B), legalize(C)
    A_region, B_region, C_region = to_buffer_region(各参数)
    M, N = C_shape
    K = (按 transpose 从 A_shape 推出)
    校验 M_A==M, K_A==K_B, N_B==N
    return call_intrin("handle", Op.get(op_key),
                       A_region, B_region, C_region,
                       transpose_A, transpose_B, M, N, K,
                       policy, clear_accum, stride_a, stride_b, ...)
```

#### 4.2.3 源码精读

形状推导与校验在 [tilelang/language/gemm_op.py:L85-L100](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/gemm_op.py#L85-L100)，注意转置时 K 维从 A 的「另一根轴」取出，并与 B 的 K 维比对：

```python
M, N = C_shape
M_A = A_shape[-1] if transpose_A else A_shape[-2]
K   = A_shape[-2] if transpose_A else A_shape[-1]
N_B = B_shape[-2] if transpose_B else B_shape[-1]
K_B = B_shape[-1] if transpose_B else B_shape[-2]
assert prim_expr_equal(M_A, M), ...
assert prim_expr_equal(K, K_B), ...
assert prim_expr_equal(N_B, N), ...
```

最终生成 intrinsic 的代码在 [gemm_op.py:L123-L146](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/gemm_op.py#L123-L146)：

```python
return tirx.call_intrin(
    "handle",
    tirx.op.Op.get(op_key),   # op_key = "tl.tileop.gemm"
    A_arg, B_arg, C_arg,
    transpose_A, transpose_B, M, N, K,
    policy, clear_accum,
    stride_a, stride_b, offset_a, offset_b,
    k_pack, wg_wait, mbar_arg, C_coords[0], C_coords[1],
    annotations=annotations,
)
```

`tl.tileop.gemm` 这个 op 本身是在 C++ 侧注册的，见 [src/op/gemm.cc:L261-L264](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/gemm.cc#L261-L264)：`TIR_REGISTER_TL_TILE_OP(Gemm, gemm)`，并标了 `kOpaque` 的调用效果（意味着它在 TIR 里被当作不透明的整体，不会被普通 Pass 随意改写，专门留给 `lower_tile_op` 处理）。`wgmma_gemm` / `tcgen05_gemm` 则是同一 `Gemm` 节点但带额外标注（`is_wgmma` / `is_tcgen05`）的变体 op（[gemm.cc:L266-L292](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/gemm.cc#L266-L292)）——它们最终都会构造同一个 `Gemm` IR 对象，只是带了不同的「我要走哪条路径」的注释。

> 提示：这意味着无论你写 `T.gemm`、`T.wgmma_gemm` 还是 `T.tcgen05_gemm`，IR 层面都是同一个 `Gemm` 节点，差别只在标注。统一节点、按标注分发，是 tile op 体系的一个关键设计。

#### 4.2.4 代码实践

**实践目标：** 在 IR 里亲眼看到 `tl.tileop.gemm` 这个占位节点。

**操作步骤：**

1. 用 `pass_configs={"tl_enable_dump_ir": True}` 或环境变量打开 IR dump（详见 u6-l1），编译 `examples/gemm/example_gemm.py`；
2. 在 `lower_tile_op` **之前**的 IR 里搜索 `tl.tileop.gemm`，确认它是一个 `call_intrin` 占位；
3. 在 `lower_tile_op` **之后**的 IR 里再搜索一次，确认占位已被展开成 `mma`/`wgmma` 等具体操作。

**需要观察的现象：** 前一份 IR 里 `T.gemm` 还是「一句话」；后一份 IR 里它变成了若干 `T.allocate`、`T.mma`、地址计算与循环的组合。

**预期结果：** 这正是「语义占位 → 后期展开」的最直观证据。

> 待本地验证：dump IR 的开关名与输出路径以本机 tilelang 版本为准（参见 u6-l1）。

#### 4.2.5 小练习与答案

**练习 1：** 为什么不在 DSL 阶段就把 `T.gemm` 直接展开成 mma 指令？
**答案：** 因为展开需要 layout 推理结果（swizzle 后的地址排布）和目标硬件信息，而这些在 layout_inference / lower_tile_op Pass 之前尚未确定。先留占位、后展开，才能让同一份 DSL 适配多种硬件与布局。

**练习 2：** `tl.tileop.gemm`、`tl.tileop.wgmma_gemm`、`tl.tileop.tcgen05_gemm` 三个 op 在 IR 层面是同一个节点吗？
**答案：** 是。它们都会构造同一个 `Gemm` IR 对象，仅通过 `is_wgmma` / `is_tcgen05` 标注区分，由后端的指令键选择与 `resolve_gemm_impl` 决定真正走哪条路径。

---

### 4.3 注册与分发：resolve_gemm_impl 如何挑出后端实现

#### 4.3.1 概念说明

`Gemm` 节点知道「要做什么」，但「怎么做」要交给具体后端实现类（`GemmMMA` / `GemmWGMMA` / `GemmTCGEN5` / `GemmMFMA` / `GemmScalar` / …）。如何根据 target 选对实现？这就是 `tileop/gemm/registry.py` 解决的问题。

它用一个很轻量的注册表：每个后端实现类在 import 时调用 `register_gemm_impl`，把自己和一个 `(inst_name, predicate)` 绑定。`inst_name` 是指令键（如 `"cuda.mma"`、`"cuda.wgmma"`、`"rocm.mfma"`），`predicate` 是一个「这个 target 我能不能处理」的判断函数。运行时 `resolve_gemm_impl(inst_name, target)` 在表里找出**唯一**匹配的实现类。

而 `inst_name` 本身，则由 C++ 侧根据目标架构选出来——这套「先选指令键、再用指令键查实现」的两段式分发，让 Python 侧的注册/分发逻辑极其简洁。

#### 4.3.2 核心流程

```
Gemm.infer_layout / lower
    └─ _select_gemm_instruction(thread_nums, target)
           └─ C++ GemmGetGemmInstructionKey → 选出 inst_name
                  （TCGEN5MMA → WGMMA → MFMA → MMA → Scalar）
    └─ _get_implementation_class(inst_name, target)
           └─ resolve_gemm_impl(inst_name, target)
                  └─ 在 _GEMM_IMPLS 里找 inst_name 匹配且 predicate(target) 为真的唯一实现类
    └─ impl_class(self).infer_layout(...) / .lower(...)
```

指令键的优先级（见 `Gemm._select_gemm_instruction` 的文档串）大致是：

1. **TCGEN5MMA** —— Blackwell 架构；
2. **WGMMA** —— Hopper 架构，且矩阵/warp 数量足够；
3. **MFMA** —— AMD CDNA；
4. **MMA** —— 通用 CUDA（Volta/Turing/Ampere 等）；
5. **Scalar** —— CPU 目标，标量回退。

#### 4.3.3 源码精读

注册表核心在 [tilelang/tileop/gemm/registry.py:L23-L46](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tileop/gemm/registry.py#L23-L46)。`register_gemm_impl` 把条目放进全局 `_GEMM_IMPLS`（同名覆盖）；`resolve_gemm_impl` 找出 `inst_name` 匹配且 `predicate(target)` 为真的条目，要求**恰好一个**，否则报错：

```python
def resolve_gemm_impl(gemm_inst, target):
    matches = [e for e in _GEMM_IMPLS
               if e.inst_name == gemm_inst and e.predicate(target)]
    if not matches:
        raise ValueError(f"No GEMM implementation registered for ...")
    if len(matches) > 1:
        raise ValueError(f"Multiple GEMM implementations matched ...")
    return matches[0].impl_class
```

CUDA 后端的注册在 [tilelang/cuda/op/gemm/\_\_init\_\_.py:L34-L38](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/op/gemm/__init__.py#L34-L38)：

```python
register_gemm_impl("cuda.mma",      GEMM_INST_MMA,    _match_mma,      GemmMMA)
register_gemm_impl("cuda.mma_sm70", GEMM_INST_MMA,    _match_mma_sm70, GemmMMASm70)
register_gemm_impl("cuda.mma_sm75", GEMM_INST_MMA,    _match_mma_sm75, GemmMMASm75)
register_gemm_impl("cuda.wgmma",    GEMM_INST_WGMMA,  _match_wgmma,    GemmWGMMA)
register_gemm_impl("cuda.tcgen05",  GEMM_INST_TCGEN05,_match_tcgen05,  GemmTCGEN5)
```

注意一个精妙之处：`GemmMMA`、`GemmMMASm70`、`GemmMMASm75` 三个实现共享**同一个** `inst_name`（`GEMM_INST_MMA = "cuda.mma"`），靠 `predicate` 区分：`_match_mma_sm70` 只在 Volta（sm_70）为真，`_match_mma_sm75` 只在 Turing（sm_75）为真，`_match_mma` 在「CUDA 但非 Volta/Turing」时为真（[gemm/\_\_init\_\_.py:L14-L23](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/op/gemm/__init__.py#L14-L23)）。这样同一个指令键在不同代际的 GPU 上分发到不同的 MMA 实现类，互不冲突。

调用链的 Python 入口在 [tilelang/tileop/gemm/\_\_init\_\_.py:L121-L174](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tileop/gemm/__init__.py#L121-L174)。`infer_layout` / `lower` 都先调 `_select_gemm_instruction` 拿到指令键（[L141-L158](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tileop/gemm/__init__.py#L141-L158)），再调 `_get_implementation_class` → `resolve_gemm_impl` 拿到实现类（[L160-L174](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tileop/gemm/__init__.py#L160-L174)），最后实例化并调用对应钩子：

```python
def infer_layout(self, target, thread_nums):
    gemm_inst = self._select_gemm_instruction(thread_nums, target)
    impl_class = self._get_implementation_class(gemm_inst, target)
    return impl_class(self).infer_layout(target, thread_nums)
```

指令键选择最终落到 C++ 的 `GemmNode::GetGemmInstructionKey`（[src/op/gemm.cc:L166-L168](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/gemm.cc#L166-L168)），它委托给 `ResolveGemmImpl(target).select_inst(...)` 按架构选出键名。C++ 侧的 `InferLayout`（[gemm.cc:L221-L259](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/gemm.cc#L221-L259)）通过全局函数 `"tl.gemm.infer_layout"` 回调到上面那段 Python；`Lower`（[gemm.cc:L176-L219](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/gemm.cc#L176-L219)）则回调 `"tl.gemm.lower"`（注册见 [tileop/gemm/\_\_init\_\_.py:L12-L29](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tileop/gemm/__init__.py#L12-L29)）。这种「C++ IR 节点 ↔ Python 实现类」经 `tvm_ffi` 全局函数互调的模式，正是 u1-l3 提到的系统级前后端镜像。

每个后端实现类都继承自公共基类 `GemmBase`（[tilelang/tileop/gemm/gemm_base.py:L16-L71](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tileop/gemm/gemm_base.py#L16-L71)），它按 A/B 的内存 scope 把 GEMM 分类为 SS（双 shared）、SR（A shared B register）、RS、RR、TS（A 在 tensor memory）等变体，并提供 M/N/K、stride、clear_accum 等属性的统一访问。比如 `is_gemm_ss` 就是「A、B 都在 shared」：

```python
def is_gemm_ss(self):
    return is_shared(self.A) and is_shared(self.B)
```

一个具体实现的样例是 `GemmMMA.infer_layout`（[tilelang/cuda/op/gemm/gemm_mma.py:L42-L69](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/op/gemm/gemm_mma.py#L42-L69)），它根据 SS/SR/RS/RR 给 A、B、C 各自推导出合适的内存布局（shared 上做 swizzle、fragment 上做 MMA store 布局），返回一个 `{buffer: layout}` 字典。`lower`（[gemm_mma.py:L71-L90](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/op/gemm/gemm_mma.py#L71-L90)）则据此生成真正的 ldmatrix / mma 指令序列。`lower` 的调用发生在 `lower_tile_op` Pass 中（[src/transform/lower_tile_op.cc:L1195](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/lower_tile_op.cc#L1195) 调 `tile_op->Lower(...)`，其参数说明见 [L1077](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/lower_tile_op.cc#L1077)）。

#### 4.3.4 代码实践（源码阅读型）

**实践目标：** 画出「指令键 → 实现类」的分发表。

**操作步骤：**

1. 读 `tilelang/cuda/op/gemm/__init__.py`、`tilelang/rocm/op/gemm/__init__.py`、`tilelang/cpu/op/gemm/__init__.py`、`tilelang/metal/op/gemm/__init__.py`；
2. 为每个 `register_gemm_impl(...)` 调用记录四元组 `(name, inst_name, predicate 作用, impl_class)`；
3. 回答：为什么 CUDA 有三条 `inst_name == "cuda.mma"` 的注册？它们靠什么互斥？

**需要观察的现象：** 同一指令键下多个实现靠 `predicate`（按架构代际）互斥；不同后端（rocm/cpu/metal）用不同指令键（`rocm.mfma`/`cpu.scalar`/`metal.simdgroup`）天然分离。

**预期结果：** 你能得到一张清晰的「target × 架构 → 实现类」对照表。

#### 4.3.5 小练习与答案

**练习 1：** `resolve_gemm_impl` 在匹配到 0 个或多个实现时分别会怎样？
**答案：** 0 个匹配抛 `ValueError("No GEMM implementation registered for ...")`；多于 1 个匹配抛 `ValueError("Multiple GEMM implementations matched ...")`。它要求**恰好一个**匹配，避免静默选错。

**练习 2：** 如果要为一个新的 GPU 架构加一种 GEMM 实现，需要改哪些地方？
**答案：** 主要是两处：(1) 写一个继承 `GemmBase` 的实现类，实现 `infer_layout` 与 `lower`；(2) 在对应后端目录（如 `tilelang/cuda/op/gemm/`）的 `__init__.py` 里 `register_gemm_impl(name, inst_name, predicate, ImplClass)`。如果还需要新的指令键，则还要在 C++ 侧 `select_inst` 里加上对应的架构判定。

---

### 4.4 T.gemm_sp：2:4 结构化稀疏张量核路径

#### 4.4.1 概念说明

NVIDIA Ampere 起的硬件支持 **2:4 结构化稀疏**：在沿 K 轴的每 4 个连续元素里，恰好有 2 个非零、2 个为零。硬件的张量核能直接吃这种压缩格式，理论上把 GEMM 吞吐翻倍。代价是：稀疏矩阵 A 要被**压缩**存储——只保留非零元（于是 K 维缩为 K/2），并用一个**元数据**张量 E 记录「每 4 个里是哪 2 个非零」。

形式上，稠密乘法

\[ C_{ij} = \sum_{k=0}^{K-1} A_{ik}\, B_{kj} \]

在 2:4 稀疏下变成只对非零位置求和：

\[ C_{ij} = \sum_{k \in \mathrm{nz}(i)} A^{\text{sparse}}_{i,\,\cdot}\, B_{kj}, \]

其中 \(A^{\text{sparse}} \in \mathbb{R}^{M \times K/2}\)，而 E 编码了每个 4 元组里非零的位置。`T.gemm_sp` 就是 tilelang 对应的 DSL 入口，它额外多接一个 E（元数据）参数，并复用同一套 tile op 分发机制（`tl.tileop.gemm_sp`，后端实现在 `tilelang/tileop/gemm_sp/` 与 `tilelang/cuda/op/gemm_sp/`）。

#### 4.4.2 核心流程

`gemm_sp` 的用法与 `gemm` 几乎一致，只是参数变成 `(A_sparse, E, B, C, ...)`：

1. 用工具（见 `examples/gemm_sp/sparse_utils.py` 的 `compress`）把稠密 A 压缩成 `(A_sparse, E)`；
2. 在 kernel 里 alloc `A_shared (M, K/2)`、`E_shared (M, K/e_factor)`、`B_shared (K, N)`、`C_local`；
3. `T.clear(C_local)`，沿 K-tile 循环搬数据；
4. `T.gemm_sp(A_shared, E_shared, B_shared, C_local, ...)`：硬件一次完成「按 E 解压 + 块乘加」。

#### 4.4.3 源码精读

稀疏入口在 [tilelang/language/experimental/gemm_sp_op.py:L119-L170](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/experimental/gemm_sp_op.py#L119-L170)，结构与 `gemm` 同构，op key 为 `"tl.tileop.gemm_sp"`，多出 `transpose_E` 与元数据 E 参数。其 K 维校验（[gemm_sp_op.py:L77-L79](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/experimental/gemm_sp_op.py#L77-L79)）体现了「压缩使 K 缩半」的事实：

```python
K = 2 * (A_shape[-2] if transpose_A else A_shape[-1])  # 还原成稠密 K
K_B = B_shape[-1] if transpose_B else B_shape[-2]
assert prim_expr_equal(K, K_B), ...
```

完整用法见 [examples/gemm_sp/example_gemm_sp.py:L16-L42](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/gemm_sp/example_gemm_sp.py#L16-L42)，核心调用是第 [L37](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/gemm_sp/example_gemm_sp.py#L37) 行：

```python
T.gemm_sp(A_shared, E_shared, B_shared, C_local,
          transpose_A=False, transpose_E=False, transpose_B=False,
          policy=policy)
```

和 `gemm` 一样，`gemm_sp` 也有同步版 `T.gemm_sp` 与显式异步版 `T.wgmma_gemm_sp` / `T.tcgen05_gemm_sp`（同文件），遵循相同的「同步自动插 wait、异步不插 wait 且不回退」约定。

#### 4.4.4 代码实践

**实践目标：** 跑通 2:4 稀疏 GEMM 并观察吞吐。

**操作步骤：**

1. 进入 `examples/gemm_sp/`，运行 `python example_gemm_sp.py`（默认 M=N=K=16384，需 NVIDIA Ampere 及以上 GPU）；
2. 阅读脚本里 `compress`/`randn_semi_sparse`（来自同目录 `sparse_utils.py`）如何把稠密 A 变成 `(A_sparse, E)`；
3. 记录脚本打印的 `Sparse TFLOPS` 与 `Reference TFLOPS`。

**需要观察的现象：** 稀疏版相对稠密参考实现应有可观的吞吐提升（理论上限约 2×，实际受访存与稀疏率影响）。

**预期结果：** 数值精度 `torch.testing.assert_close` 通过；稀疏 TFLOPS 高于参考。

> 待本地验证：实际 TFLOPS 与提升幅度取决于 GPU 型号、`e_dtype`（int8/int16/int32，注意 int8/int32 仅 sm90+ 支持，见脚本 argparse 说明）与 block 配置。

#### 4.4.5 小练习与答案

**练习 1：** 为什么 `T.gemm_sp` 的 A 维度是 K/2 而不是 K？
**答案：** 因为 2:4 结构化稀疏下每 4 个元素只有 2 个非零，压缩存储后 A 沿 K 方向正好缩半，所以 `A_shared` 的 K 维是 `block_K // 2`，元数据 E 维度则是 `block_K // e_factor`。

**练习 2：** `gemm_sp` 的 op key 是什么？它与 `gemm` 共用同一套后端实现注册表吗？
**答案：** op key 是 `"tl.tileop.gemm_sp"`。它有自己独立的后端实现目录（`tilelang/tileop/gemm_sp/` 与各 backend 的 `op/gemm_sp/`）和注册逻辑，但整体设计模式（注册 + 指令键分发 + infer_layout/lower 钩子）与 `gemm` 完全一致。

---

## 5. 综合实践

**任务：** 实现一个带累加（`clear_accum=False`）的分块 GEMM，并对比 `T.gemm` 与「手写双重循环累加」两种写法的性能差异，体会 tile op 的价值。

**背景：** `T.gemm` 会被展开成张量核（MMA/WGMMA）指令；而「手写双重循环 + 标量乘加」只会用普通 CUDA core。两者的性能应有数量级差距，这正是 tile op 存在的意义。

**操作步骤：**

1. 基于 `examples/gemm/example_gemm.py` 写出版本 A（tile op 版），保留：

   ```python
   T.clear(C_local)
   for k in T.Pipelined(T.ceildiv(K, block_K), num_stages=3):
       T.copy(A[...], A_shared); T.copy(B[...], B_shared)
       T.gemm(A_shared, B_shared, C_local)   # 默认 clear_accum=False，累加
   T.copy(C_local, C[...])
   ```

2. 写出版本 B（手写累加版），把 `T.gemm(...)` 那一行替换为显式的三重循环，在 fragment 上做标量乘加（伪代码）：

   ```python
   # 示例代码：手写累加，不用 T.gemm
   for i, j in T.serial(block_M), ...:        # 遍历输出块
       acc = C_local[i, j]
       for kk in T.serial(block_K):           # K 维累加
           acc += A_shared[i, kk] * B_shared[kk, j]
       C_local[i, j] = acc
   ```
   （注意：fragment 的索引写法以本机能编译通过为准；关键是**不调用** `T.gemm`，改用显式乘加循环。）

3. 两个版本都用 `kernel.get_profiler().do_bench()` 测延迟，并都用 `assert_allclose` 对照 `a @ b` 验证正确性。

**需要观察的现象：**

- 两份 `get_kernel_source()` 的差异：版本 A 里能看到 `mma`/`wgmma` 张量核指令，版本 B 里只有普通的 `fma`/循环；
- 延迟差异：版本 A 应明显快于版本 B。

**预期结果：** 两者数值都正确；版本 A 的延迟显著低于版本 B（具体倍数待本地验证，取决于硬件与 block 配置）。

> 提示：如果你在无 GPU 环境下，可降级为「源码阅读型实践」——只比较两份生成的 CUDA 源码，标注版本 A 里的张量核指令来源（对应 `lower_tile_op` → `GemmMMA.lower`），并说明版本 B 为何拿不到这些指令。

## 6. 本讲小结

- `T.gemm(A, B, C)` 是同步、默认累加（`clear_accum=False`）的 tile 级矩阵乘；累加型用法需先 `T.clear(C)`，或传 `clear_accum=True`。
- DSL 阶段的 `T.gemm` 只生成一个 `tl.tileop.gemm` 的 `call_intrin` **语义占位**，具体指令留到 `lower_tile_op` Pass 展开——这是 tile op 与普通 TIR 表达式的根本区别。
- `Gemm` / `wgmma_gemm` / `tcgen05_gemm` 在 IR 层是同一个 `Gemm` 节点，仅靠 `is_wgmma`/`is_tcgen05` 标注区分。
- 后端实现走「两段式分发」：C++ 先按架构选**指令键**（TCGEN5MMA→WGMMA→MFMA→MMA→Scalar），Python 的 `resolve_gemm_impl` 再用 `(inst_name, predicate)` 在注册表里挑出**唯一实现类**。
- 每个实现类继承 `GemmBase`，按 A/B 的 scope 分 SS/SR/RS/RR/TS 变体，并实现 `infer_layout`（推 shared swizzle / fragment 布局）与 `lower`（生成张量核指令）两个钩子。
- `T.gemm_sp(A_sparse, E, B, C)` 复用同一套机制支持 2:4 结构化稀疏张量核；A 沿 K 缩半，E 编码稀疏模式。

## 7. 下一步学习建议

- 想搞清楚 `T.gemm` 周围的「多级缓冲 + 数据预取」如何自动调度，进入 **u3-l3 软件流水线 Pipelined**，学习 `T.Pipelined` 如何与本讲的 K-tile 主循环配合隐藏访存延迟。
- 想理解 `infer_layout` 推导出的 swizzle/fragment 布局到底长什么样，进入 **u3-l4 布局标注、swizzle 与 L2 优化**，并用 `plot_layout` 把 shared 布局画出来。
- 想看到「占位 → 展开」在 IR 里的完整证据，进入 **u6-l2 关键 lowering Pass 解读** 与 **u6-l3 设备代码生成**，对照 dump 的 IR 找到 `lower_tile_op` 把 `tl.tileop.gemm` 变成 `mma` 的那一瞬间。
