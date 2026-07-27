# 高级 CUDA intrinsics、TMA/cluster 与 iket

## 1. 本讲目标

本讲面向已经掌握 tilelang 编译流水线（u4/u6）与布局/调度原语（u3）的读者，目标是把「DSL 一行调用 → 真实 GPU 指令」这条链路的**最后一公里**打通到 Hopper(SM90)/Blackwell(SM100) 这一代硬件的最强特性上。

学完后你应该能够：

- 说清 `tilelang.cuda.language` 这个 CUDA 方言门面是如何在通用 `tilelang.language` 之上叠加硬件扩展的，并能定位 cluster、warpgroup、pdl、random、PTX intrinsics 这几组扩展各自的位置。
- 写出使用 **TMA 拷贝**（`T.tma_copy` / `T.copy_cluster`）或 **cluster launch**（`T.ClusterKernel`）的 kernel，并能在生成的 CUDA 源码里指出它们对应的 PTX 指令（`cp.async.bulk`、`cp.async.bulk.tensor...multicast::cluster`、`barrier.cluster.*` 等）。
- 理解 **warpgroup 切分**（`T.ws`）、**WGMMA/TCGEN5MMA** 显式异步 GEMM、**PDL**（Programmatic Dependent Launch）与 **curand 随机数** 这些 SM80+/SM90 intrinsics 的 DSL 入口。
- 掌握 `tilelang.tools.cuda.iket` 这个**实验性 kernel 事件插桩工具**的工作原理：它如何通过一个 CUDA 源码后处理回调，把命名事件/markers 编码进 TIR，再在生成代码里注入 `pmevent.mask` 内联 PTX，最终被外部 IKET profiler 收集成 Perfetto trace。

> ⚠️ 一个需要澄清的认知：本讲的 iket **并不是**「写 CUDA kernel / 内联汇编的开发入口」，而是一个**性能剖析插桩工具**。它确实会向生成代码注入内联 PTX 汇编（`pmevent.mask`），但目的是产生 profiling 事件，而不是让你手写 kernel。理解这一点能避免把工具用错方向。

## 2. 前置知识

本讲假设你已经学过：

- **u1-l3 / u4-4**：tilelang 用 `target` 描述硬件，`tilelang.language` 默认是 CUDA 方言，其它后端需经 `tilelang.<backend>.language` 显式引入；方言用 `__tilelang_dialect__` 标识。
- **u3-l1 / u6-3**：`T.gemm` 只在 DSL 层留一个 `tl.tileop.gemm` 占位，真正的 MMA/WGMMA/TCGEN5MMA 指令在 `lower_tile_op` Pass 里展开；device codegen 把底层 intrinsic 打印成 CUDA C++。
- **u3-l3**：软件流水线 `T.Pipelined` 让搬运与计算重叠。
- **u3-l4**：shared-memory swizzle 与 threadblock swizzle 是两回事。
- 一些 GPU 基础概念：warp（32 线程）、warp group（Hopper 上 = 4 个 warp = 128 线程）、shared memory、barrier、CTA（即 thread block）。

几个本讲会反复用到的硬件术语，先用一段话建立直觉：

- **TMA（Tensor Memory Accelerator）**：Hopper 起内置的专用 DMA 引擎，能一条指令把一个多维 tile 从 global memory 搬进 shared memory（`cp.async.bulk.tensor`），比「每个线程发一条 `ld.global`」省指令、省功耗、自带异步完成通知（mbarrier）。
- **Thread Block Cluster**：Hopper 起允许把若干个 CTA（默认最多 8 个）编成一组，组内 CTA 共享一段 `shared::cluster` 地址空间，可以直接读对方的 shared memory、可以用一条 TMA 把同一块数据**多播（multicast）**给组内多个 CTA。
- **WGMMA / TCGEN5MMA**：分别是 Hopper / Blackwell 的「warpgroup 级」张量核乘法指令，一条指令完成大块矩阵乘，且是异步的（发出后不等结果，需要显式 wait 或 mbarrier）。
- **PDL（Programmatic Dependent Launch）**：Hopper 起允许相邻的两个 kernel launch 在 launch 层面重叠（前一个还没结束就发起后一个），用 `pdl_trigger` / `pdl_sync` 显式控制同步点。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `tilelang/cuda/language/__init__.py` | CUDA 方言门面：在通用 `language.common` 之上叠加 cluster/intrinsics/pdl/random/tir/warpgroup 扩展，并标记 `__tilelang_dialect__ = "cuda"`。 |
| `tilelang/cuda/language/cluster.py` | cluster 同步原语：`cluster_sync`/`cluster_arrive`/`cluster_wait`/`block_rank_in_cluster` 与 CLC（Cluster Launch Control）查询。 |
| `tilelang/cuda/language/warpgroup.py` | warpgroup 切分 `WarpSpecialize`（别名 `T.ws`），用于 warp specialization。 |
| `tilelang/cuda/language/pdl.py` | PDL 同步原语 `pdl_trigger` / `pdl_sync`。 |
| `tilelang/cuda/language/random.py` | curand 随机数封装 `rng_init` / `rng_rand` / `rng_rand_float`。 |
| `tilelang/cuda/language/intrinsics.py` | CUDA intrinsics 总目录：聚合 PTX 指令封装（`ptx_*`）与各类 TensorCore emitter（MMA/WGMMA/TCGEN05/稀疏）。 |
| `tilelang/cuda/language/tir.py` | 把 C++ 侧 `ptx_cp_async`/`ptx_mma`/`ptx_wgmma_*`/`ptx_tcgen05_mma_*` 等 PTX 算子转发到 Python 命名空间。 |
| `tilelang/language/kernel.py` | `ClusterKernel`：在普通 `Kernel` 之上加 `cluster_dims` 注解（SM90+）。 |
| `tilelang/language/copy_op.py` | `tma_copy`（用户自管同步的 TMA）与 `copy_cluster`（TMA 多播 / SM-to-SM 拷贝）。 |
| `tilelang/language/gemm_op.py` | `gemm`（同步，自动 wait）、`wgmma_gemm`（Hopper 异步显式）、`tcgen05_gemm`（Blackwell 异步显式）。 |
| `tilelang/tools/cuda/iket/*.py` | IKET 插桩工具：`frontend.py`（事件 API）、`session.py`（编译会话/回调生命周期）、`codegen.py`（注入内联 PTX 与元数据）、`metadata.py`（token 编解码）、`cli.py`（输出目录/profiler 命令构造）。 |
| `docs/programming_guides/cluster_tma.md` | 官方 cluster + TMA 编程指南，本讲实践的主要依据。 |
| `examples/gemm_sm100/`、`examples/warp_specialize/`、`examples/iket/` | 对应的真实可运行示例。 |

## 4. 核心概念与源码讲解

### 4.1 CUDA 方言门面与 PTX intrinsics 体系

#### 4.1.1 概念说明

回顾 u4-l4：`import tilelang.language as T` 默认拿到的就是 CUDA 方言，而其它后端（cpu/rocm/metal/webgpu）要经 `tilelang.<backend>.language` 显式引入。这个「方言」到底是什么？它其实就是一个 Python 模块，在**通用语言层**（`tilelang.language.common`，包含 `Kernel`、`copy`、`gemm`、循环原语等所有后端共享的部分）之上，**额外 `import *` 进来一组硬件专属符号**。

CUDA 方言额外叠加的符号分几组：cluster 同步、PTX intrinsics、PDL、随机数、warpgroup 切分、调试打印，以及一批「CUDA only」的 builtin（如 `tma_load`、`tma_copy`、`ldg128`、`match_any_sync` 等）。换句话说，`tilelang.cuda.language` 是一个**门面（facade）**，它的全部工作就是把这些零散分布在子模块里的符号汇聚到同一个命名空间，并打上 `__tilelang_dialect__ = "cuda"` 这个方言标签。

这里面最低层的东西是 **PTX intrinsics**。PTX 是 NVIDIA 的并行线程汇编（一种虚拟指令集），`cp.async`、`ldmatrix`、`mma`、`wgmma`、`cp.async.bulk` 都是 PTX 指令。tilelang 不让你直接写汇编，而是把每条常用 PTX 指令封装成一个 TIR 算子（`ptx_mma`、`ptx_wgmma_ss`、`ptx_cp_async_bulk` …），这些算子的真实「打印成汇编字符串」实现写在 C++ 侧（见 u6-l3 的 device codegen），Python 侧只做转发。再往上一层，「张量核发射器（TensorCoreIntrinEmitter）」把这些 PTX 片段组合成完整的 ldmatrix→mma→stmatrix 序列，供 `examples/gemm/example_gemm_intrinsics.py` 这种「手动拼 MMA」的写法使用。

> 与 u3-l1 的关系：`T.gemm` 是**最高层**的 tile op（留 `tl.tileop.gemm` 占位，后端自动选指令）；本节讲的 `ptx_*` 与 emitter 是**最底层**的、直接对应单条 PTX 指令的积木。中间层是 `wgmma_gemm` / `tcgen05_gemm` 这种「显式异步 GEMM」。

#### 4.1.2 核心流程

CUDA 方言门面的组装流程：

1. `from tilelang.language.common import *` —— 先拿到所有后端通用的 DSL 原语。
2. 从 `tilelang.language` 各处导入「CUDA only」builtin（`tma_copy`、`tma_load`、`create_tma_descriptor`、各种 `ldg/lds/stg/sts` 向量读写、`match_any_sync` 等）。
3. `from .cluster import *` / `.intrinsics import *` / `.pdl import *` / `.random import *` / `.tir import *` / `.warpgroup import *` —— 把六个 CUDA 专属子模块的符号全部合并进来。
4. 设置 `__tilelang_dialect__ = "cuda"`，并把所有来源的 `__all__` 去重合并成一个新的 `__all__`。

PTX intrinsic 的暴露流程（以 `ptx_wgmma_ss` 为例）：

1. C++ 侧 `tir.op` 注册了 `ptx_wgmma_ss` 算子（其 `MakePTXIntrinsic` 在 codegen 时打印成 `wgmma.mma_async.sync.aligned.m64n...k...` PTX 字符串）。
2. `tilelang/cuda/language/tir.py` 用 `_dtype_forward(_tir_op.ptx_wgmma_ss)` 把它包成 Python 可调用对象并放进 `__all__`。
3. 上层的 `wgmma_gemm` emitter 在 `lower_tile_op` 里调用这些 `ptx_wgmma_*` 生成完整的 WGMMA 序列。

#### 4.1.3 源码精读

CUDA 方言门面的「叠加 + 标记」结构（[tilelang/cuda/language/__init__.py:49-62](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/language/__init__.py#L49-L62)）——它依次合并 cluster、intrinsics、pdl、print、random、tir、warpgroup 七个子模块的符号：

```python
from .cluster import *        # noqa: F401,F403
from .intrinsics import *     # noqa: F401,F403
from .pdl import *            # noqa: F401,F403
...
from .warpgroup import *      # noqa: F401,F403
```

方言标签写死为 cuda（[tilelang/cuda/language/__init__.py:114](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/language/__init__.py#L114)），这正是 u4-l4 提到的「以 `__tilelang_dialect__` 标识当前方言」：

```python
__tilelang_dialect__ = "cuda"
```

intrinsics 总目录 `tilelang/cuda/language/intrinsics.py` 是一个**纯聚合文件**：它本身不实现指令，而是把分布在 `tilelang.cuda.intrinsics.*`、`tilelang.language.gemm_op`、`tilelang.language.builtin`、`.tir` 里的符号汇拢。注意它还给两个异步 GEMM 起了 `_mma` 别名（[tilelang/cuda/language/intrinsics.py:74-76](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/language/intrinsics.py#L74-L76)），方便按「mma 风格」记忆：

```python
tcgen05_mma = tcgen05_gemm          # Blackwell 异步 GEMM 别名
wgmma_mma  = wgmma_gemm             # Hopper  异步 GEMM 别名
```

PTX 算子的转发层（[tilelang/cuda/language/tir.py:17-29](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/language/tir.py#L17-L29)）——每个 `ptx_*` 都对应一条真实 PTX 指令：

```python
ptx_cp_async_bulk       = _dtype_forward(_tir_op.ptx_cp_async_bulk)   # cp.async.bulk (TMA 的底层)
ptx_ldmatrix            = _dtype_forward(_tir_op.ptx_ldmatrix)        # ldmatrix
ptx_mma                 = _dtype_forward(_tir_op.ptx_mma)             # mma.sync (Ampere)
ptx_wgmma_ss            = _dtype_forward(_tir_op.ptx_wgmma_ss)        # wgmma.mma_async (Hopper, SS 操作数)
ptx_tcgen05_mma_ss      = _dtype_forward(_tir_op.ptx_tcgen05_mma_ss)  # TCGEN5MMA (Blackwell)
```

「手动拼 MMA」的典型用法是 `TensorCoreIntrinEmitter`，它把 ldmatrix + mma + stmatrix 打包成一个可在 DSL 里调用的对象（见 [examples/gemm/example_gemm_intrinsics.py:88-100](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/gemm/example_gemm_intrinsics.py#L88-L100)）：

```python
mma_emitter = TensorCoreIntrinEmitter(a_dtype=in_dtype, b_dtype=in_dtype, ...)
...
mma_emitter.ldmatrix_a(A_local, A_shared, ki)   # 发 ldmatrix
mma_emitter.mma(A_local, B_local, C_local)      # 发 mma.sync
```

> 这层 emitter 主要服务于 Ampere(SM80) 这一代的「warp 级 MMA」。到了 Hopper/Blackwell，通常不再手动拼，而是用 `T.gemm` 自动落到 WGMMA/TCGEN5MMA，或用 4.3 节的显式异步接口。

#### 4.1.4 代码实践

**源码阅读型实践：在你的环境里跑通「手动 MMA」示例并定位 PTX。**

1. **实践目标**：建立「DSL 调用 → emitter → PTX 指令」的直觉。
2. **操作步骤**：
   - 打开 `examples/gemm/example_gemm_intrinsics.py`，找到 `mma_emitter.ldmatrix_a` / `mma_emitter.mma` 调用。
   - 运行 `python examples/gemm/example_gemm_intrinsics.py`（需 CUDA GPU；无 GPU 则跳到步骤 3 做静态阅读）。
   - 在 `main()` 末尾加一行 `print(matmul.get_kernel_source(...))` 已有，阅读打印出的 CUDA 源码，搜索 `ldmatrix` 与 `mma.sync` 字样。
3. **需要观察的现象**：生成源码里出现 `asm volatile("ldmatrix.sync.aligned.m8n8.x4 ...")` 与 `mma.sync.aligned.m16n8k16 ...` 这类内联 PTX。
4. **预期结果**：能逐条把 emitter 的三个方法对应到源码里的三段 `asm volatile`。
5. 运行结果：**待本地验证**（本环境无 GPU，无法执行；但源码阅读部分无需运行即可完成）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `tilelang.language` 默认就是 CUDA 方言，却还要单独存在一个 `tilelang.cuda.language` 模块？
**参考答案**：`tilelang.language` 是「默认方言快捷方式」，内部等同于 CUDA 方言；而 `tilelang.cuda.language` 是这个方言的**实体模块**，负责把通用 `language.common` 与 CUDA 专属扩展（cluster/intrinsics/pdl/random/warpgroup）合并并打上 `__tilelang_dialect__="cuda"` 标签。其它后端（cpu/metal/…）共享 `language.common` 但叠加各自的扩展。

**练习 2**：`ptx_wgmma_ss` 里的 `ss` 是什么含义？
**参考答案**：表示两个矩阵操作数都来自 shared memory（A=shared, B=shared）。WGMMA 还支持 `rs`（A=register, B=shared）等变体，tilelang 在 `tir.py` 里都做了转发。这与 u3-l1 讲的 SS/SR/RS/RR/TS 操作数 scope 变体是同一概念。

---

### 4.2 Cluster launch 与 cluster 同步原语

#### 4.2.1 概念说明

从 Hopper（SM90）起，NVIDIA 引入了 **Thread Block Cluster**：可以把最多 8 个 CTA 编成一个「簇」，簇内 CTA 共享一段 `shared::cluster` 虚拟地址空间，能够：

- **互访 shared memory**：CTA A 可以直接读 CTA B 的 shared memory（叫 DSMem，Distributed Shared Memory），不必绕 global memory。
- **TMA 多播**：一条 TMA 指令把同一块 global 数据同时送给簇内多个 CTA。
- **簇级 barrier**：`barrier.cluster.arrive` / `barrier.cluster.wait` 做簇内同步。

在 tilelang 里，要用 cluster 特性，必须把启动上下文从 `T.Kernel` 换成 `T.ClusterKernel`，并显式给出 `cluster_dims`（簇的形状）。`cluster_dims=(4,1,1)` 表示每 4 个 CTA 组成一个一维簇。注意：簇内 CTA 总数不能超过 8，且 `gridDim` 必须是 `cluster_dims` 的整数倍。

cluster 同步原语都封装在 `tilelang/cuda/language/cluster.py`，它们都遵循 u3-l1 讲过的「留 `tl.*` intrinsic 占位 → 后端展开成 PTX」模式：Python 侧每个函数都只是一行 `tirx.call_intrin(..., tirx.op.Op.get("tl.cluster_*"))`，真正的 `barrier.cluster.*` PTX 由 C++ codegen 注入。

#### 4.2.2 核心流程

一个 cluster kernel 的典型结构（摘自官方指南）：

```python
with T.ClusterKernel(grid_x, grid_y, threads=128, cluster_dims=(4, 1, 1)) as (bx, by):
    rank = T.block_rank_in_cluster()   # 0..3，当前 CTA 在簇内的编号
    ...                                  # 各 CTA 用 rank 分工
    T.cluster_sync()                    # 簇内 barrier（arrive + wait）
```

执行流程：

1. `T.ClusterKernel(...)` 调用 `_ffi_api.KernelLaunch(blocks, threads, attrs)`，其中 `attrs["cluster_dims"] = [4,1,1]`，把这个注解挂到 PrimFunc 上。
2. host 侧 launch 时，adapter 用 `cudaLaunchKernelEx` + `cudaLaunchAttributeClusterDimension` 发起带 cluster 维度的启动（这是它与普通 `T.Kernel` 的唯一区别）。
3. kernel 内的 `T.cluster_sync()` 等原语生成 `barrier.cluster.arrive.aligned` + `barrier.cluster.wait.aligned`。

cluster 同步语义要点：

- `cluster_arrive()` / `cluster_wait()` 是**分离式** barrier（分别发 arrive 和 wait，允许 arrive 后先做别的再 wait）。
- `cluster_sync()` 是 arrive + wait 的合并，等价于簇内 `__syncthreads()`。
- `cluster_arrive_relaxed()` 的 relaxed 表示「不要求该线程已完成的内存写对其它簇成员可见」，用于你已经用其它手段（如 mbarrier）保证可见性的场景，开销更低。

此外还有一个**高级特性 CLC（Cluster Launch Control）**，相关原语 `clc_try_cancel` / `clc_is_canceled` 等允许持久化（persistent）kernel 动态取消下一次 launch，用于减少 launch 抖动。它比较小众，本讲只要求知道入口位置。

#### 4.2.3 源码精读

cluster 同步原语全是「占位式」一行函数。完整同步（[tilelang/cuda/language/cluster.py:42-44](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/language/cluster.py#L42-L44)）：

```python
def cluster_sync() -> tirx.PrimExpr:
    """Issue cluster barrier arrive + wait (full synchronization)."""
    return tirx.call_intrin("void", tirx.op.Op.get("tl.cluster_sync"))
```

分离式 arrive / wait（[tilelang/cuda/language/cluster.py:27-39](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/language/cluster.py#L27-L39)）：

```python
def cluster_arrive_relaxed() -> tirx.PrimExpr:
    """Issue barrier.cluster.arrive.relaxed.aligned."""
    return tirx.call_intrin("void", tirx.op.Op.get("tl.cluster_arrive_relaxed"))

def cluster_arrive() -> tirx.PrimExpr:
    """Issue barrier.cluster.arrive.aligned."""
    return tirx.call_intrin("void", tirx.op.Op.get("tl.cluster_arrive"))

def cluster_wait() -> tirx.PrimExpr:
    """Issue barrier.cluster.wait.aligned."""
    return tirx.call_intrin("void", tirx.op.Op.get("tl.cluster_wait"))
```

查询当前 CTA 在簇内的编号（[tilelang/cuda/language/cluster.py:47-49](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/language/cluster.py#L47-L49)），对应 PTX `%cluster_ctarank`：

```python
def block_rank_in_cluster() -> tirx.PrimExpr:
    """Return the 1-D rank of the calling CTA within its cluster (%%cluster_ctarank)."""
    return tirx.call_intrin("int32", tirx.op.Op.get("tl.block_rank_in_cluster"))
```

CLC 查询入口（[tilelang/cuda/language/cluster.py:52-59](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/language/cluster.py#L52-L59)），属于高级持久化 kernel 优化：

```python
def clc_try_cancel(result, mbarrier) -> tirx.PrimExpr:
    """Issue a single-CTA cluster launch control query."""
    return tirx.call_intrin("void", tirx.op.Op.get("tl.clc_try_cancel"),
                            _to_ptr(result, "w"), _to_ptr(mbarrier, "rw"))
```

启动上下文 `ClusterKernel`（[tilelang/language/kernel.py:343-393](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/kernel.py#L343-L393)）——它本质就是普通 `Kernel` 加一个 `cluster_dims` 注解，docstring 明确这是 SM90+ only、用 `cudaLaunchKernelEx` 启动：

```python
def ClusterKernel(*blocks, cluster_dims, threads=None, prelude=None):
    """Construct a kernel launch frame with a CUDA thread block cluster (SM90+ only).
    ... launched with cudaLaunchKernelEx using cudaLaunchAttributeClusterDimension."""
    ...
    cluster_dims = _normalize_cluster_dims(cluster_dims)   # (4,1,1) 或 4 都归一成 [4,1,1]
    if cluster_dims is not None:
        attrs["cluster_dims"] = cluster_dims
    return _ffi_api.KernelLaunch(blocks, threads, attrs)
```

`cluster_dims` 的归一化（[tilelang/language/kernel.py:133-146](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/kernel.py#L133-L146)）会接受 `int`/`tuple`/`list`，并在 `(1,1,1)` 时返回 `None`（即退化为普通 kernel）：

```python
def _normalize_cluster_dims(cluster_dims, ...):
    ...
    return None if cluster_dims == [1, 1, 1] else cluster_dims
```

#### 4.2.4 代码实践

**源码阅读 + 待本地验证型实践：阅读 cluster TMA 多播测试，理解 rank 分工。**

1. **实践目标**：在不手写 kernel 的前提下，看懂官方 cluster 多播示例里 4 个 CTA 如何按 rank 分工。
2. **操作步骤**：
   - 阅读 `docs/programming_guides/cluster_tma.md` 的「Feature 1 — TMA Multicast」一节与配套测试 `testing/python/cuda/test_tma_multicast_demo.py`。
   - 画出 `cluster_mask=0b0011` 时 rank 0/1/2/3 各自的动作（提示：rank 0 发多播、rank 1 被动接收、rank 2/3 各发普通 TMA load）。
3. **需要观察的现象**：理解「最低置位 bit 的 CTA 负责发出 `cp.async.bulk.tensor...multicast::cluster`，其余被 mask 覆盖的 CTA 不发指令被动接收」。
4. **预期结果**：能复述 cluster 多播节省的是 DRAM 读带宽（同一块数据只读一次，广播给多个 CTA）。
5. 运行结果：**待本地验证**（需 SM90+ GPU 才能真正跑；阅读部分无需运行）。

#### 4.2.5 小练习与答案

**练习 1**：`T.cluster_sync()` 与 `T.cluster_arrive()` + `T.cluster_wait()` 有何区别？什么时候用后者更有利？
**参考答案**：`cluster_sync` 是 arrive 后立刻 wait；分离式允许在 arrive 与 wait 之间插入其它不依赖该同步的工作，从而重叠等待时间。当你能在 arrive 之后塞入一些独立计算时，分离式能隐藏 barrier 延迟。

**练习 2**：`cluster_dims=(2,1,1)` 时，`T.block_rank_in_cluster()` 的取值范围是多少？`gridDim.x=5` 合法吗？
**参考答案**：取值 0 或 1。不合法——`gridDim` 必须是 `cluster_dims` 的整数倍，5 不是 2 的倍数，应改成偶数。

---

### 4.3 TMA 异步拷贝、warpgroup 切分、显式异步 GEMM 与 PDL/random

#### 4.3.1 概念说明

这一节把「能让 Hopper/Blackwell 跑满」的几个关键 intrinsics 串起来，它们通常**配合使用**：warpgroup 切分让一部分 warp 专门搬数据、另一部分专门算；搬运用 TMA（异步、自通知）；计算用 WGMMA/TCGEN5MMA（异步、需显式 wait）；同步用 mbarrier。

**（a）TMA 拷贝。** tilelang 里有三个层次：

| 接口 | 同步模型 | 适用场景 |
| --- | --- | --- |
| `T.copy(src, dst)` | 全自动：后端按 scope 组合选 TMA / cp.async / 循环，并自动插入完整的 arrive+load+wait | 绝大多数场景的首选（u2-l2） |
| `T.tma_copy(src, dst, barrier=...)` | 半自动：发 TMA load，但**不自动 wait**，同步交给你管（`barrier_arrive` + `mbarrier_wait_parity`） | warp specialization 流水线，需要把 wait 推迟到真正用数据前 |
| `T.copy_cluster(...)` | cluster 专用：多播（`cluster_mask`）或 SM-to-SM 拷贝（`dst_block` + `remote_barrier`） | split-K、跨 SM 交换部分和 |

**（b）显式异步 GEMM。** `T.gemm` 是同步的（Hopper 上自动插 `warpgroup_wait`，Blackwell 上自动插 `mbarrier_wait_parity`）。当你自己做 warp specialization 流水线、想精确控制 wait 时机时，用显式版本：

- `T.wgmma_gemm(...)`（Hopper）：强制走 WGMMA lowering，**不**自动 wait，需配 `T.wait_wgmma()`。
- `T.tcgen05_gemm(..., mbar=...)`（Blackwell）：强制走 TCGEN5MMA，结果写到 **tmem**（tensor memory，Blackwell 新增的片上存储，需 `T.alloc_tmem` 分配），需配 `T.mbarrier_wait_parity(mbar, ...)`。

**（c）warpgroup 切分 `T.ws(n)`。** Hopper 上一个 warp group = 128 线程。`with T.ws(0):` 把代码限定在第 0 个 warp group（threadIdx 0..127）执行，`with T.ws(1):` 限定在第 1 个（128..255）。这就是 **warp specialization（warp 特化）**：让 producer warp group 专门发 TMA copy，consumer warp group 专门发 GEMMA，二者靠 mbarrier 握手，实现比 `T.Pipelined` 更细粒度的重叠。

**（d）PDL（Programmatic Dependent Launch）。** `T.pdl_trigger()` / `T.pdl_sync()` 控制相邻 kernel launch 的重叠，属于 launch 层优化，常用于 persistent kernel。

**（e）随机数。** `rng_init` / `rng_rand` / `rng_rand_float` 封装了 CUDA 的 curand 设备 API（默认 Philox4_32_10 生成器），用于 dropout、随机掩码等需要在 kernel 内产随机数的算子。

#### 4.3.2 核心流程

一个典型的「warp specialization + TMA + WGMMA」GEMM 主循环（来自 `examples/warp_specialize/example_warp_specialize_gemm_copy_0_gemm_1.py`）：

```python
data_is_ready   = T.alloc_barrier(arrive_count=128)
compute_is_done = T.alloc_barrier(arrive_count=128)

for ko in T.Pipelined(T.ceildiv(K, block_K), num_stages=0):
    with T.ws(0):                                   # producer warp group
        T.barrier_wait(compute_is_done, (ko + 1) % 2)
        T.tma_copy(A[...], A_shared, barrier=data_is_ready)   # 异步 TMA，不 wait
        T.barrier_arrive(data_is_ready)
    with T.ws(1):                                   # consumer warp group
        T.barrier_wait(data_is_ready, ko % 2)        # 等数据就绪
        T.gemm(A_shared, B_shared, C_local)          # 这里用同步 T.gemm
        T.barrier_arrive(compute_is_done)
```

关键握手：producer 发完 TMA 后 `barrier_arrive(data_is_ready)`；consumer 在算之前 `barrier_wait(data_is_ready, ko%2)`，奇偶轮交替（双缓冲）实现流水。`T.tma_copy` 不自动 wait，所以 wait 必须显式写在 consumer 一侧——这正是它存在的意义。

Blackwell TCGEN5MMA 版本（来自 `examples/gemm_sm100/gemm_tcgen5mma.py`）：累加器在 tmem，每轮 GEMM 配一个 mbarrier：

```python
C_tmem = T.alloc_tmem([block_M, block_N], accum_dtype)
mbar   = T.alloc_barrier(1)
for k in T.Pipelined(...):
    T.copy(A[...], A_shared); T.copy(B[...], B_shared)
    T.tcgen05_gemm(A_shared, B_shared, C_tmem, trans_A, trans_B,
                   mbar=mbar, clear_accum=(k == 0))
    T.mbarrier_wait_parity(mbar, k % 2)              # 显式等 TCGEN5MMA 完成
T.copy(C_tmem, C_local)                              # tmem → fragment 再写回 global
```

#### 4.3.3 源码精读

**warpgroup 切分**（[tilelang/cuda/language/warpgroup.py:19-55](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/language/warpgroup.py#L19-L55)）——把 threadIdx 折算成一维 tid，再按 128 线程一个 warp group 分组，docstring 给出了 `T.ws(0)`/`T.ws(1)`/`T.ws(0,1)` 的精确语义：

```python
def WarpSpecialize(*warp_group_idx) -> WarpSpecializeFrame:
    """...
    >>> T.ws(0)     -> if tx < 128
    >>> T.ws(1)     -> if tx >= 128 and tx < 256
    >>> T.ws(0, 1)  -> if tx < 128 or (tx >= 128 and tx < 256)
    """
    ...
    # only available for nvidia gpus.
    warp_group_size = 128
    ...
    return _ffi_api.WarpSpecialize(warp_group_ids, tid, warp_group_size)

ws = WarpSpecialize   # 别名，DSL 里写 T.ws(0)
```

**TMA 拷贝（用户自管同步）**（[tilelang/language/copy_op.py:234-274](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/copy_op.py#L234-L274)）——docstring 明确：load 只发 `expect_tx + tma_load`（不发 wait），store 只发 `tma_store + tma_store_arrive`（不发 wait），同步交给用户：

```python
def tma_copy(src, dst, *, barrier=None, leader_scope_threads=None, ...):
    """TMA copy with user-managed synchronization.
    For loads (global -> shared): issues expect_tx + tma_load (no wait).
    ...
    For stores (shared -> global): issues tma_store + tma_store_arrive (no wait).
    """
```

**cluster 多播 / SM-to-SM 拷贝**（[tilelang/language/copy_op.py:137-187](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/copy_op.py#L137-L187)）——把 `dst_block`/`cluster_mask`/`remote_barrier` 收进 `tl.tileop.copy` 的注解，后端据此选多播或 SM-to-SM 路径：

```python
def copy_cluster(src, dst, *, dst_block=None, cluster_mask=None,
                 remote_barrier=None, eviction_policy=None, ...):
    """Cluster-aware copy for TMA multicast or SM-to-SM shared-memory copy."""
    ...
    if dst_block is not None:    ann["dst_block"] = dst_block
    if cluster_mask is not None: ann["cluster_mask"] = cluster_mask
    if remote_barrier is not None: ann["barrier"] = remote_barrier
    ...
    return tirx.call_intrin("handle", tirx.op.Op.get("tl.tileop.copy"),
                            src, dst, annotations=ann if ann else None)
```

官方文档把 `copy_cluster` 的三条 lowering 路径讲得很清楚（[docs/programming_guides/cluster_tma.md:124-138](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/cluster_tma.md#L124-L138)）：TMA fast path（连续 + 有 barrier，发一条 `tl::tma_store_cluster`）、Multi-TMA path（非连续 ND 区域，逐行发）、SIMT fallback（无 barrier，用 `map_shared_rank` 标量写）。

**显式异步 GEMM**（[tilelang/language/gemm_op.py:201-233](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/gemm_op.py#L201-L233)）——`wgmma_gemm` 强制 WGMMA、`wg_wait=-1` 表示不自动 wait：

```python
def wgmma_gemm(A, B, C, transpose_A=False, transpose_B=False,
               policy=GemmWarpPolicy.Square, clear_accum=False):
    """Explicit Hopper WGMMA GEMM without an implicit wait.
    - it always requests the WGMMA lowering path
    - it never auto-emits an inlined `warpgroup_wait`
    If the current target or operand pattern cannot use Hopper WGMMA,
    compilation fails instead of silently falling back to MMA."""
    return _gemm_impl("tl.tileop.wgmma_gemm", ..., 0, -1, None)
```

Blackwell 版本（[tilelang/language/gemm_op.py:236-260](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/gemm_op.py#L236-L260)）——结果写 tmem、必须传 mbar，还支持 `use_2cta`（要求 `cluster_dims=(2,1,1)`）：

```python
def tcgen05_gemm(A, B, C, ..., *, mbar, use_2cta=False):
    """Explicit Blackwell TCGEN05 GEMM without an implicit wait.
    ... never auto-emits an inlined `mbarrier_wait_parity`.
    When use_2cta=True, ... requires cluster_dims to be (2,1,1) or (1,2,1)."""
    ann = {"is_tcgen05": 1}
    if use_2cta: ann["use_2cta"] = 1
    return _gemm_impl("tl.tileop.tcgen05_gemm", ..., annotations=ann)
```

**PDL 与随机数**都很短。PDL（[tilelang/cuda/language/pdl.py:10-21](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/language/pdl.py#L10-L21)）：

```python
def pdl_trigger() -> tirx.PrimExpr:
    return tirx.call_intrin("void", tirx.op.Op.get("tl.pdl_trigger"))

def pdl_sync() -> tirx.PrimExpr:
    return tirx.call_intrin("void", tirx.op.Op.get("tl.pdl_sync"))
```

随机数（[tilelang/cuda/language/random.py:8-39](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/language/random.py#L8-L39)）——默认 Philox4_32_10 生成器，`seq` 不给则用 `tx + bx*threads` 自动派生序列号，保证各线程独立流：

```python
def rng_init(seed, seq=None, off=0, generator="curandStatePhilox4_32_10_t"):
    ...
    if seq is None:
        bx = T.get_block_binding(); ex = T.kernel.get_thread_extent()
        tx = T.get_thread_binding()
        seq = tirx.convert(tx + bx * ex)          # 自动派生 per-thread 序列号
    return tirx.call_intrin("void", tirx.op.Op.get("tl.rng_init"), seed, seq, off, generator)
```

#### 4.3.4 代码实践

**待本地验证型实践：用 `T.tma_copy` + warpgroup 切分跑一个 GEMM，定位生成代码里的 TMA 指令。**

1. **实践目标**：亲手跑通「warp specialization + TMA」流水线，并验证 `T.tma_copy` 在源码里对应 `cp.async.bulk.tensor`。
2. **操作步骤**：
   - 运行 `python examples/warp_specialize/example_warp_specialize_gemm_copy_0_gemm_1.py`（需 SM90+ GPU）。
   - 取消示例里被注释的 `get_kernel_source` 那两行（[examples/warp_specialize/example_warp_specialize_gemm_copy_0_gemm_1.py:62-63](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/warp_specialize/example_warp_specialize_gemm_copy_0_gemm_1.py#L62-L63)），打印源码。
   - 在源码里搜索 `cp.async.bulk` 与 `wgmma` / `mma` 字样。
3. **需要观察的现象**：源码里出现 `cp.async.bulk.tensor.2d.shared::cluster.global.mbarrier::complete_tx::bytes ...`（TMA load），以及 warp group 之间的 `barrier.arrive`/`barrier.wait` 握手。
4. **预期结果**：能把 DSL 里的 `T.tma_copy`、`T.ws(0)`/`T.ws(1)`、`T.barrier_arrive`/`T.barrier_wait` 四类调用分别对应到源码里的具体指令段。
5. 运行结果：**待本地验证**（本环境无 GPU；无 GPU 时可改为静态阅读 `examples/warp_specialize/` 下任一文件的源码并画出握手时序图）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `T.tma_copy` 不自动 wait，而 `T.copy` 会？既然不自动 wait 更麻烦，为什么 warp specialization 场景偏要用 `T.tma_copy`？
**参考答案**：`T.copy` 自动插完整 arrive+load+wait，写起来简单但 wait 位置固定（紧跟 load）；`T.tma_copy` 只发 load 不 wait，让你把 wait 推迟到 consumer 真正读数据之前。在 warp specialization 里，producer 发完 load 后还要服务下一级流水，若立刻 wait 会阻塞 producer，故必须用 `T.tma_copy` 把 wait 移到 consumer 侧。

**练习 2**：Blackwell 上 `T.tcgen05_gemm` 的累加器为什么不能直接用 `T.alloc_fragment`，而要用 `T.alloc_tmem`？
**参考答案**：TCGEN5MMA 指令把结果写到 Blackwell 新增的 **tmem（tensor memory）** 片上存储，而不是普通的寄存器 fragment。所以累加器必须用 `T.alloc_tmem` 分配，算完后再 `T.copy(C_tmem, C_local)` 搬到 fragment 才能写回 global。

**练习 3**：`T.copy_cluster(s, d, cluster_mask=0b0011)` 与 `T.copy_cluster(s, d, dst_block=1, remote_barrier=b)` 分别走哪条 lowering 路径？
**参考答案**：前者是 **TMA 多播**：mask 最低位置位位（rank 0）发 `cp.async.bulk.tensor...multicast::cluster`，rank 1 被动接收。后者是 **SM-to-SM 拷贝**：因为有 `remote_barrier` 且若区域连续，走 TMA fast path 发一条 `tl::tma_store_cluster`，把本 CTA 的 shared 推到 rank 1 的 shared。

---

### 4.4 iket 工具：kernel 事件插桩与内联 PTX 注入

#### 4.4.1 概念说明

`tilelang.tools.cuda.iket` 是一个**实验性的 CUDA kernel 性能剖析插桩工具**。它解决的问题：你想知道生成的 kernel 内部「哪一段耗时、何时执行」，但 tilelang 默认生成的 CUDA 源码里没有任何可被外部 profiler 识别的命名标记。iket 让你在 DSL 里写**命名事件**（markers / ranges），它会在编译期把这些事件**编码进 TIR**，再通过一个 **CUDA 源码后处理回调**把事件「物化」成：

1. 一段 `__device__` 字节数组形式的 **IKET 元数据**（告诉外部 IKET runtime 有哪些事件、叫什么名字）。
2. 一组用 **内联 PTX 汇编**（`pmevent.mask` 指令）实现的**事件宏**（`TL_IKET_EVENT` 等），在 kernel 执行到该位置时打一个时间戳记录。

外部 IKET profiler 收集这些记录，导出成 Perfetto trace（`.pftrace` / `.html`），你就能在时间轴上看到每个命名事件的执行情况。

整个工具分四个文件，各司其职：

- `frontend.py`：DSL 侧 API——`iket.mark(name)`、`iket.range(name)`（上下文管理器）、`iket.payload(expr, dtype=...)`。每个调用生成一个 TIR `call_extern("TL_IKET_EVENT", id, token)`，其中 `token` 是把事件元数据 base64 编码后的字符串。
- `metadata.py`：事件元数据的编解码（`encode_event`/`decode_event`），用 base64url + JSON 把 `(name, event_id, kind, range_id, payload_type, ...)` 压成一个 source-safe token，嵌进 TIR StringImm。
- `session.py`：编译会话管理。`with iket.session(...):` 包住 `tilelang.compile(...)`，会注册一个全局回调 `tilelang_callback_cuda_postproc`，并在退出时还原。回调引用计数，支持嵌套。
- `codegen.py`：后处理核心。在生成好的 CUDA 源码里，用正则把每个 `TL_IKET_EVENT(id, ..., token)` 调用里的 token 解码出来，给事件分配**模块级唯一 id**，然后注入元数据数组与 PTX 事件宏。
- `cli.py`：host 侧辅助——`set_output_dir`、`profile_command`（拼出 `python -m iket.cli.main profile -- ...` 命令字符串）、`trace_files`。

> 与 u9-l1 的关系：u9-l1 的 lower_trace / pass_visualizer 看的是**编译期 IR 如何变形**；iket 看的是**运行期 kernel 实际执行情况**。二者正交。

#### 4.4.2 核心流程

iket 的端到端工作流：

1. **写事件**：用户在 DSL 里写 `iket.mark("before_store")`、`with iket.range("compute"): ...`。
2. **编码进 TIR**：`frontend._event_call` 把事件编成 token，生成 `call_extern("TL_IKET_EVENT", event_id, token)`。此时 `event_id` 只是占位，token 才是信息载体。
3. **进 session 编译**：`with iket.session(output_dir=...):` 注册 `tilelang_callback_cuda_postproc = _cuda_postproc`，然后 `tilelang.compile(...)`。device codegen 产出 CUDA 源码后，回调被触发。
4. **后处理注入**：`inject_iket_cuda(code, target, runtime_payloads=...)`：
   - `_canonicalize_cuda_events`：正则扫出所有 `TL_IKET_EVENT(...)` 调用，解码 token，按 `(kind, name)` 去重，分配模块级 id（跳过保留 id 31）。
   - `_metadata_decls`：为每个事件生成一个 `__device__ ... unsigned char __iket_evt_decl_..._attrs[60] = {...};` 字节数组，外加一个总表 `__iket_meta_info[48]`。
   - `_event_macros`：生成 `#define TL_IKET_EVENT(ID, ...) asm volatile("{ ... pmevent.mask " #ID "; ...")`——这就是注入的内联 PTX。SM90+ 用 `%cluster_ctarank`，老架构用常数 0。
   - 把这些内容插到源码第一个 `#include` 之前。
5. **收集 trace**：用外部 profiler 跑 instrumented kernel，导出 Perfetto trace。

事件宏的核心 PTX（简化版，来自 `codegen.py`）做三件事：读全局时钟低 32 位、把事件 id 或运算进时间戳、写进 shared memory 一块固定区域、最后发 `pmevent.mask` 触发记录。带 payload 的版本再多一次 32 位 volatile store 写 payload值——注意是**两次分离的 32 位 store**而非一次 64 位 store，因为外部 IKET patcher 不接受 `STS.64` 记录形状。

#### 4.4.3 源码精读

DSL 侧事件 API（[tilelang/tools/cuda/iket/frontend.py:83-87](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/cuda/iket/frontend.py#L83-L87) 与 [tilelang/tools/cuda/iket/frontend.py:119-135](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/cuda/iket/frontend.py#L119-L135)）：

```python
def mark(name: str, payload: Any = None):
    """Emit an IKET instant marker at the current program point."""
    payload_spec = _payload_spec(payload)
    event = _get_event(name, "mark", payload_spec=payload_spec)
    return _event_call(event, payload_spec)

def range(name: str, payload: Any = None) -> _RangeScope:
    """Return a scope that emits IKET range-start and range-end events."""
    return _RangeScope(name, payload=payload)
```

每个事件最终落地为一个带 token 的 extern 调用（[tilelang/tools/cuda/iket/frontend.py:193-198](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/cuda/iket/frontend.py#L193-L198)）：

```python
def _event_call(event, payload_spec):
    token = encode_event(event)                       # base64 编码的元数据
    if payload_spec is None:
        return tirx.call_extern("handle", _EVENT_EXTERN, event.event_id, token)
    extern = _EVENT_PAYLOAD_F32_EXTERN if payload_spec.dtype == "float32" else _EVENT_PAYLOAD_U32_EXTERN
    return tirx.call_extern("handle", extern, event.event_id, payload_spec.expr, token)
```

元数据 token 的编解码（[tilelang/tools/cuda/iket/metadata.py:28-33](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/cuda/iket/metadata.py#L28-L33)）——把事件字典 JSON 化后 base64url，前缀 `__tl_iket_v1_`，确保 source-safe：

```python
def encode_event(event: Event) -> str:
    data = {"version": _TOKEN_VERSION, **asdict(event)}
    payload = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return TOKEN_PREFIX + encoded
```

session 注册后处理回调（[tilelang/tools/cuda/iket/session.py:45-60](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/cuda/iket/session.py#L45-L60)）——`enable()` 用 `tvm_ffi.register_global_func` 把 `_cuda_postproc` 挂到 `tilelang_callback_cuda_postproc`，引用计数 `_enable_depth` 支持嵌套：

```python
_CUDA_POSTPROC = "tilelang_callback_cuda_postproc"

def _cuda_postproc(code: str, target: Any) -> str:
    return inject_iket_cuda(code, target, runtime_payloads=runtime_payloads_enabled())

def enable(*, override: bool = True) -> None:
    """Enable IKET CUDA source post-processing for subsequent compiles."""
    ...
    previous = tvm_ffi.get_global_func(_CUDA_POSTPROC, allow_missing=True)
    tvm_ffi.register_global_func(_CUDA_POSTPROC, f=_cuda_postproc, override=override)
    _previous_cuda_postproc = previous
    _enable_depth = 1
```

session 还会默认 `disable_cache()`（[tilelang/tools/cuda/iket/session.py:111-140](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/cuda/iket/session.py#L111-L140)），因为「回调是否激活」「payload 模式」是 host 侧编译状态，不进 TIR 缓存键，复用旧二进制可能拿到没插桩的代码（这是 u4-l3 缓存键设计的一个真实坑）。

后处理核心 `inject_iket_cuda`（[tilelang/tools/cuda/iket/codegen.py:226-248](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/cuda/iket/codegen.py#L226-L248)）——规整事件、生成元数据 + 宏、插到第一个 `#include` 前：

```python
def inject_iket_cuda(code: str, target: Any, *, runtime_payloads: bool) -> str:
    if _INJECTION_MARKER in code:        # 幂等：已注入则跳过
        return code
    code, events = _canonicalize_cuda_events(code)
    if not events:
        return code
    insert_at = code.find("#include <tl_templates")
    ...
    prefix = (f"// {_INJECTION_MARKER}\n..."
              + _metadata_decls(events, runtime_payloads=runtime_payloads)
              + "\n" + _event_macros(target, runtime_payloads=runtime_payloads) + "\n")
    return code[:insert_at] + prefix + code[insert_at:]
```

注入的内联 PTX 事件宏（[tilelang/tools/cuda/iket/codegen.py:172-185](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/cuda/iket/codegen.py#L172-L185)）——核心是 `pmevent.mask` 指令；`_cluster_rank_instruction` 在 SM90+ 用 `%cluster_ctarank`，否则用 0（[tilelang/tools/cuda/iket/codegen.py:165-169](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/cuda/iket/codegen.py#L165-L169)）：

```python
def _event_macros(target, *, runtime_payloads):
    rank_instruction = _cluster_rank_instruction(target)
    event = rf"""
#define TL_IKET_EVENT(ID, ...) \
  asm volatile("{{\n\t" \
               ".reg .b32 r, t;\n\t" \
               "{rank_instruction}\n\t" \
               "mov.u32 t, %globaltimer_lo;\n\t" \
               "or.b32 t, t, " #ID ";\n\t" \
               "mad.lo.u32 r, r, 0x1000000, 0x20;\n\t" \
               "st.weak.shared.u32 [r], t;\n\t" \
               "pmevent.mask " #ID ";\n\t" \
               "}}" ::: "memory")
"""
```

最后看一个完整的最小用法（[examples/iket/minimal.py:29-44](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/iket/minimal.py#L29-L44)）——`iket.range`/`iket.mark` 直接当 DSL 语句写在 `@T.prim_func` 内：

```python
def elementwise_add_with_iket(n, threads=THREADS, dtype=T.float32):
    @T.prim_func
    def main(A, B, C):
        with T.Kernel(T.ceildiv(n, threads), threads=threads) as bx, iket.range("kernel_total"):
            for i in T.Parallel(threads):
                idx = bx * threads + i
                if idx < n:
                    iket.mark("before_store")
                    C[idx] = A[idx] + B[idx]
                    iket.mark("after_store")
    return main
```

并在 session 内编译（[examples/iket/minimal.py:57-66](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/iket/minimal.py#L57-L66)）：

```python
with iket.session(output_dir=args.iket_output_dir):
    program = elementwise_add_with_iket(N)
    kernel = tilelang.compile(program, out_idx=-1, target="cuda", execution_backend="cython")
```

#### 4.4.4 代码实践

**源码阅读型实践：跟踪一个 iket 事件从 Python 到注入 PTX 的完整路径。**

1. **实践目标**：把 4.4.2 描述的五步流程在真实代码里逐段定位。
2. **操作步骤**：
   - 阅读 `examples/iket/minimal.py`，找到 `iket.mark("before_store")`。
   - 跟到 `tilelang/tools/cuda/iket/frontend.py` 的 `mark` → `_event_call`，确认它生成 `call_extern("TL_IKET_EVENT", id, token)`。
   - 跟到 `metadata.py` 的 `encode_event`，理解 token 里装的是什么。
   - 跟到 `session.py` 的 `enable` 与 `codegen.py` 的 `inject_iket_cuda`，看回调如何把 token 还原成元数据并注入 `pmevent.mask` 宏。
   - （可选，需 GPU + 外部 iket 包）运行 `python examples/iket/minimal.py --iket-output-dir /tmp/tl_iket`，打开写出的 `.cu` 文件搜索 `TL_IKET_EVENT` 与 `pmevent`。
3. **需要观察的现象**：生成的 `.cu` 里，在 `#include <tl_templates...>` 之前有一段 `extern "C" { __device__ ... unsigned char __iket_evt_decl_...[]; ... }` 与 `#define TL_IKET_EVENT(...) asm volatile("{ ... pmevent.mask ... }")`；kernel 体里 `before_store`/`after_store` 位置变成了 `TL_IKET_EVENT(1, "__tl_iket_v1_...");` 调用。
4. **预期结果**：能画出「`iket.mark` → token → extern 调用 → 后处理回调 → 元数据数组 + PTX 宏」的数据流图。
5. 运行结果：**待本地验证**（完整运行需 GPU 与外部 IKET 包；纯源码阅读与生成源码检查无需 GPU 即可做——只要 `tilelang.compile` 能跑，`inject_iket_cuda` 就会生效）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 iket 要把事件元数据编码成 base64 token 塞进 TIR，而不是用一个进程级字典在编译时查？
**参考答案**：因为 TIR 是「真理之源」——它会进入缓存键、会跨 session 存活。token 嵌在 TIR StringImm 里，意味着：(1) 一个预构建的 `PrimFunc` 即使换了 session、清了 frontend 注册表，事件元数据仍在；(2) 后处理回调能直接从**生成的 CUDA 源码**里把 token 解出来还原元数据，而不依赖进程内状态（多进程编译、缓存命中场景也能工作）。

**练习 2**：为什么带 payload 的事件宏要用**两次分离的 32 位 store**，而不是一次 64 位 store？
**参考答案**：外部 IKET runtime 的 NativeDump patcher 只接受「时间戳」与「payload」作为两条独立 32 位 `STS` 记录的形状；若 ptxas 把它们合并成一条 `STS.64`，patcher 无法识别。所以 payload store 必须用 `volatile` 阻止合并。

**练习 3**：`iket.session()` 默认会 `disable_cache()`，为什么？
**参考答案**：事件名/schema 固然进了 TIR 缓存键，但「postproc 回调是否激活」「payload 模式开没开」是 host 侧编译状态，**不**进缓存键。若复用一个在回调未激活时编译的二进制，会得到没有插桩的 kernel。默认关缓存是为了避免这种「静默漏插桩」。

---

## 5. 综合实践

**任务：写一个使用 TMA 拷贝（`T.tma_copy`）或 cluster launch（`T.ClusterKernel`）的 GEMM，并说明 DSL intrinsics 最终对应到 kernel source 里的哪条指令。**

参考 `examples/gemm_sm100/` 与 `examples/warp_specialize/`，任选一条路线：

- **路线 A（cluster 多播，SM90+）**：仿照 `docs/programming_guides/cluster_tma.md` 的 split-K 草图，用 `T.ClusterKernel(..., cluster_dims=(4,1,1))` + `T.copy_cluster(..., cluster_mask=0b0011)` 写一个把 A 多播给 rank 0/1 的 GEMM。编译后用 `get_kernel_source()` 查看源码，定位 `cp.async.bulk.tensor...multicast::cluster` 指令，并解释为什么多播能省 DRAM 带宽。
- **路线 B（warp specialization + TMA，SM90+）**：以 `examples/warp_specialize/example_warp_specialize_gemm_copy_0_gemm_1.py` 为模板，用 `T.ws(0)`/`T.ws(1)` 切分 producer/consumer，producer 用 `T.tma_copy` 搬数据，consumer 用 `T.gemm`（或 `T.wgmma_gemm` + `T.wait_wgmma`）计算。查看源码，把 `T.tma_copy` 对应到 `cp.async.bulk.tensor`，把 `T.ws` 对应到 `if (threadIdx.x < 128)` 之类的 warp group guard。

无论选哪条路线，交付三样东西：

1. 可编译运行的 kernel 代码（或注明在哪个 example 基础上改了什么）。
2. 一张对照表：DSL 调用 → 生成的 PTX/CUDA 指令 → 该指令的作用。
3. 用 `tilelang.profiler.do_bench` 测一次延迟（无 GPU 则写明「待本地验证」并给出预期的测量步骤）。

> 提示：若手头没有 SM90+ GPU，可以把 target 降到普通 CUDA、用 `T.copy`（自动 TMA）+ `T.gemm` 跑通正确性，然后**静态阅读** `examples/gemm_sm100/gemm_tcgen5mma.py` 的源码与 `docs/programming_guides/cluster_tma.md`，画出指令对照表——这同样完成本讲的认知目标。

## 6. 本讲小结

- `tilelang.cuda.language` 是一个**门面模块**，在通用 `language.common` 之上叠加 cluster/intrinsics/pdl/random/warpgroup 等扩展，并用 `__tilelang_dialect__ = "cuda"` 标识方言；底层 PTX 指令（`cp.async.bulk`、`ldmatrix`、`mma`、`wgmma`、`tcgen05_mma`）以 `ptx_*` TIR 算子的形式暴露，由 C++ codegen 打印成汇编。
- **Cluster**（SM90+）通过 `T.ClusterKernel(..., cluster_dims=...)` 启用，cluster 同步用 `T.cluster_sync/arrive/wait`，查询簇内编号用 `T.block_rank_in_cluster()`；`T.copy_cluster` 的 `cluster_mask` 走 TMA 多播、`dst_block`+`remote_barrier` 走 SM-to-SM 拷贝。
- **TMA** 有三个层次：`T.copy`（全自动）、`T.tma_copy`（半自动，不 wait，用于 warp specialization）、`T.copy_cluster`（cluster 多播/SM-to-SM）。`T.tma_copy` 对应 `cp.async.bulk.tensor`。
- **显式异步 GEMM**：`T.wgmma_gemm`（Hopper，需配 `T.wait_wgmma`）、`T.tcgen05_gemm`（Blackwell，结果写 tmem，需配 mbarrier + `T.mbarrier_wait_parity`）；它们是 `T.gemm` 的「强制异步、不自动 wait」版本。**warpgroup 切分** `T.ws(n)` 按 128 线程一组划分，是 warp specialization 的基础。
- **PDL**（`pdl_trigger`/`pdl_sync`）与 **curand 随机数**（`rng_init`/`rng_rand`/`rng_rand_float`）是两类更小众但常用的 intrinsics 入口。
- **iket** 是一个**性能剖析插桩工具**（不是 kernel 开发入口）：在 DSL 写命名事件 → 编码成 base64 token 嵌进 TIR → session 注册 `tilelang_callback_cuda_postproc` 回调 → 后处理把 token 还原成元数据并注入 `pmevent.mask` 内联 PTX → 外部 profiler 收集成 Perfetto trace。带 payload 的事件用两次分离 32 位 store 以兼容外部 patcher。

## 7. 下一步学习建议

- **想看完整编译链路如何把这些 intrinsics 落地**：回到 u6-l2/u6-l3，重点读 `lower_tile_op` 如何把 `tl.tileop.copy`/`tl.tileop.gemm` 展开成本节看到的 `cp.async.bulk`/`wgmma`，以及 device codegen 如何按需 include `tl_templates/cuda/` 下的 TMA/WGMMA 模板头。
- **想做自动调优**：结合 u8-l1（Autotuner）把本讲的 `num_stages`、`cluster_dims`、warp specialization 结构纳入搜索空间；用 u8-l3（Profiler）的 `do_bench(backend="cupti")` 测延迟。
- **想深入 cluster 编程**：精读 `docs/programming_guides/cluster_tma.md` 的 split-K 综合示例与 `testing/python/cuda/test_tma_dsmem.py`（覆盖 SM-to-SM 的 fast path / multi-TMA / SIMT fallback 三条路径）。
- **想用 iket 做实战剖析**：跑通 `examples/iket/all_features.py`，用 `python -m iket.cli.main profile ...` 收集 trace，在 Perfetto 里对照事件与 u9-l1 的 lower trace，把「编译期 IR 变化」与「运行期执行时序」两个视角叠加起来看。
- **下一讲 u10-l2** 会讲如何为 tilelang 扩展新 op / 新 pass / 新后端，本节接触的 `tl.cluster_*`、`tl.tileop.copy(cluster_mask=...)` 等 intrinsic 注册机制正是「新增 tile op / intrinsic」的具体范例，可以承接。
