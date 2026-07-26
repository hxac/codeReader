# 块级 GEMM 内核解剖

## 1. 本讲目标

本讲承接 [u3-l8（TileLang 内核骨架）](u3-l8-tilelang-kernel-skeleton.md)。在上一讲里，我们已经看清一个 TileLang 算子文件「定义—调优—评估」的整体骨架（`get_configs` / `@autotune` / `@jit` / `best_result`），但当时刻意跳过了 `@T.prim_func` 内核本体。本讲就要把这块最核心的「块级 GEMM 内核」逐行拆开。

学完本讲，你应当能够：

- 说出 TileLang 块级 GEMM 的**五要素**：`T.Kernel`（网格映射）、`alloc_shared`/`alloc_fragment`（存储分配）、`T.copy`（数据搬运）、`T.gemm`（Tensor Core MMA）、`T.Pipelined`（软件流水），并理解它们如何各司其职。
- 理解 K 维循环为什么是「累加」语义，以及为什么用软件流水来隐藏访存延迟。
- 画出 A / B / C 在「全局内存 ↔ 共享内存 ↔ 寄存器 fragment」之间的数据搬运路径。
- 解释回写阶段为什么是 `C_local → C_shared → C 全局`，而不是直接写回。

本讲的内核是后续几乎所有 TileLang 算子（多精度 matmul、反量化 GEMV、FlashAttention、MLA 等）的共同原型，把它吃透是理解整个项目的前提。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

### 2.1 为什么要「分块」（tiling）

一个完整的 GEMM，比如 `C = A @ B^T`，其中 `A ∈ ℤ^{M×K}`、`B ∈ ℤ^{N×K}`、`C ∈ ℤ^{M×N}`，当 M、N、K 都上万时，矩阵远大于显存里任何一级高速缓存。GPU 不可能把整张 A、B 一次性搬进片上存储。于是我们把输出 C 切成一个个 `block_M × block_N` 的小块（tile），每个线程块（block）只负责计算一个输出块：

\[
C[m, n] = \sum_{k=0}^{K-1} A[m, k] \cdot B[n, k]
\]

切分后，单个输出块只需要 A 的一段行（`block_M × K`）和 B 的一段行（`block_N × K`）。再把 K 维也切成 `block_K` 的小段，每个线程块就可以「加载一小段 A 和 B → 做一次小矩阵乘并累加 → 再加载下一段」，直到 K 维累加完毕。这就是**块级 GEMM** 的核心思想。

### 2.2 GPU 的三级存储与「搬运」

理解本讲需要先记住 GPU 的存储层级：

| 存储层级 | 位置 | 容量 | 速度 | 在 TileLang 里的对应 |
|---|---|---|---|---|
| 全局内存（Global / HBM） | 片外显存 | 大（几十 GB） | 慢 | 内核入参 `A`、`B`、`C` |
| 共享内存（Shared Memory） | 片上（SM 内） | 小（几十 KB/block） | 快 | `T.alloc_shared(...)` |
| 寄存器 / Fragment | 单个线程内 | 极小 | 极快 | `T.alloc_fragment(...)` |

数据必须在三者之间**搬运**：全局内存的原始数据，要先搬到共享内存（让一个 block 内所有线程共用），再让 Tensor Core 从共享内存取数做矩阵乘，乘出来的累加结果先放在寄存器 fragment 里，最后再搬回全局内存。`T.copy` 就是负责搬运的指令。

### 2.3 Tensor Core 与「软流水」

Tensor Core 是 GPU 上专门做小矩阵乘加（MMA / IMMA）的硬件单元，一条指令就能完成一个（例如 `16×16×16`）的小矩阵乘加。但它执行一次 MMA 需要的数据必须已经在共享内存里。而全局→共享的搬运又很慢。如果「等搬完再算」，Tensor Core 就会闲置。

解决之道是**软件流水**（software pipelining）：当 Tensor Core 在算第 `k` 段时，提前把第 `k+1`、`k+2` 段的数据从全局内存搬到共享内存。这样搬运与计算重叠，访存延迟被「藏」了起来。TileLang 用 `T.Pipelined` 来表达这件事，`num_stages` 就是流水深度（0 表示关闭流水）。

> 概念提示：本讲反复出现的 `block_M / block_N / block_K` 是「块大小」，决定每个线程块处理的输出块与 K 段大小；它们来自 u3-l8 讲过的 `get_configs` 搜索空间。

## 3. 本讲源码地图

本讲只看一个文件，但它承载了块级 GEMM 的全部五要素：

| 文件 | 作用 |
|---|---|
| [`benchmark_tilelang_matmul.py`](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py) | hopper 架构下 dense matmul 的 TileLang 内核。`get_configs` 造搜索空间（u3-l8 已讲），`matmul()` 用装饰器定义并调优内核，本讲聚焦其中的 `@T.prim_func main`（L191-L246）。 |

辅助参考（本讲不展开，但实践任务会用到）：

| 文件 | 作用 |
|---|---|
| [`benchmark_tilelang_matmul.sh`](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.sh) | 驱动脚本，遍历 14 组 shape 调用上面的 `.py`。注意它的 dtype 只跑 `int8 int8 int32 int32`。 |

> 一个贯穿本讲的「以代码为准」提醒（u3-l8 已建立此意识）：本文件 L186-L189 的注释说「half-precision / accumulate in float」，但**紧随其后的代码**写的是 `dtype = "int8"`、`accum_dtype = "int32"`；驱动脚本 `benchmark_tilelang_matmul.sh` 也只跑 int8。因此实际执行的内核是 **int8 输入、int32 累加**的 INT8 Tensor Core（IMMA）路径。本讲讲的是**结构与五要素，这部分与精度无关**，int8 的细节留到 [u4-l12（int8 与多精度 GEMM）](u4-l12-int8-multiprecision-gemm.md)。

## 4. 核心概念与源码讲解

先给出整段内核全貌，再按五要素逐个拆。内核本体在 [benchmark_tilelang_matmul.py:191-246](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L191-L246)：

```python
@T.prim_func
def main(
        A: T.Tensor((M, K), dtype),       # 输入 A：(M, K)
        B: T.Tensor((N, K), dtype),       # 输入 B：(N, K)，注意是 N×K
        C: T.Tensor((M, N), accum_dtype), # 输出 C：(M, N)
):
    with T.Kernel(
            T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=thread_num) as (bx, by):
        A_shared = T.alloc_shared((block_M, block_K), dtype)
        B_shared = T.alloc_shared((block_N, block_K), dtype)
        C_local  = T.alloc_fragment((block_M, block_N), accum_dtype)
        C_shared = T.alloc_shared((block_M, block_N), accum_dtype)

        T.use_swizzle(panel_size=10, enable=enable_rasteration)
        T.clear(C_local)

        for k in T.Pipelined(T.ceildiv(K, block_K), num_stages=num_stages):
            T.copy(A[by * block_M, k * block_K], A_shared)
            T.copy(B[bx * block_N, k * block_K], B_shared)
            T.gemm(A_shared, B_shared, C_local, transpose_B=True, policy=policy)

        T.copy(C_local, C_shared)
        T.copy(C_shared, C[by * block_M, bx * block_N])
```

> 数据形状约定（贯穿全讲，务必先记住）：
> - 输入 `A` 是 `(M, K)`，输入 `B` 是 `(N, K)`——B 存的是「转置后」的形态，所以内核做的是 `C = A @ B^T`。
> - 每个线程块负责输出 C 的一个 `block_M × block_N` 子块。
> - K 维被切成若干 `block_K` 段，循环累加。

下面按五要素拆解。

### 4.1 模块一：T.Kernel——网格映射

#### 4.1.1 概念说明

`T.Kernel` 是 TileLang 内核的「入口块」，对应 CUDA 里的「`__global__` 函数 + 网格配置」。它做两件事：

1. 声明**网格形状**（grid），即整个计算被切成多少个线程块。
2. 把每个线程块的二维索引 `(bx, by)` 绑定出来，供内核体内计算「我负责哪个输出块」时使用。

一句话：`T.Kernel` 把「输出空间」映射到「线程块空间」。

#### 4.1.2 核心流程

设输出 C 的形状是 `(M, N)`，块大小是 `block_M × block_N`，那么：

- M 方向需要 `⌈M / block_M⌉` 个块，N 方向需要 `⌈N / block_N⌉` 个块。
- 网格二维 `(gridX, gridY) = (⌈N/block_N⌉, ⌈M/block_M⌉)`，索引 `(bx, by)` 中 `bx` 遍历 N 方向（列块），`by` 遍历 M 方向（行块）。
- 本块负责的输出子块为 `C[by·block_M : (by+1)·block_M, bx·block_N : (bx+1)·block_N]`。

向上取整用 `T.ceildiv(a, b)`（即 `⌈a/b⌉`），保证 M、N 不是 block 整数倍时也能覆盖全部输出。

#### 4.1.3 源码精读

网格映射在 [benchmark_tilelang_matmul.py:209-210](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L209-L210)：

```python
with T.Kernel(
        T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=thread_num) as (bx, by):
```

说明：

- 第一个参数 `T.ceildiv(N, block_N)` 是 gridX（N 方向块数），第二个 `T.ceildiv(M, block_M)` 是 gridY（M 方向块数）。TileLang 这里把 `bx` 绑定给 N 方向、`by` 绑定给 M 方向（与上方注释「Bind x-dimension to block index in N, y-dimension to block index in M」一致，见 [L207-L208](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L207-L208)）。
- `threads=thread_num` 设置每个线程块的线程数（来自搜索空间，如 128 或 256）。线程数会参与决定 warp 切分，但具体切分策略由 `policy` 控制，留到 u3-l11 讲。
- 之后 `(bx, by)` 在循环里被用来定位「取 A 的哪一段、取 B 的哪一段、写 C 的哪一块」。

#### 4.1.4 代码实践

**实践目标**：确认网格映射与输出块的对应关系。

**操作步骤**：

1. 打开 [benchmark_tilelang_matmul.py:209-210](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L209-L210)。
2. 取一组具体数值，例如 `M=N=8192, K=8192, block_M=block_N=128`（这是搜索空间里的合法取值）。
3. 手算 gridX、gridY 各等于多少，以及 `(bx=1, by=2)` 这个块负责 C 的哪个子块。

**需要观察的现象**：把 grid 形状与输出子块坐标写下来。

**预期结果**：`gridX = ⌈8192/128⌉ = 64`，`gridY = 64`，共 `64×64=4096` 个块。`(bx=1, by=2)` 负责 `C[2·128:3·128, 1·128:2·128] = C[256:384, 128:256]`。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `T.ceildiv` 改成普通整除 `//`，当 M 不是 `block_M` 的整数倍时会出什么问题？
**答案**：M 方向最后几行（不足一个 block_M 的部分）不会被任何线程块覆盖，输出 C 的右下角会缺一块、写不进去，结果错误。`ceildiv` 正是为了向上取整覆盖全部输出。

**练习 2**：为什么 `bx` 绑定 N 方向、`by` 绑定 M 方向，而不是反过来？
**答案**：这只是一种坐标约定，不改变计算正确性（每个块仍负责一个 `block_M × block_N` 子块）。不同绑定会影响 block 在网格里的遍历顺序，从而影响 L2 缓存命中——这正是 u3-l11 要讲的 swizzle / rasterization 优化的切入点。

---

### 4.2 模块二：alloc_shared / alloc_fragment——存储分配

#### 4.2.1 概念说明

进入 `T.Kernel` 块之后，第一件事是给本块「申请存储」。TileLang 提供两种片上存储分配原语：

- `T.alloc_shared(shape, dtype)`：在**共享内存**（block 内所有线程可见）里分配一块。共享内存是全局↔寄存器之间的「中转站」，也是 Tensor Core 取数的来源。
- `T.alloc_fragment(shape, dtype)`：在**寄存器**（fragment）里分配一块。fragment 是 Tensor Core MMA 指令的「累加器」所在，每个线程持有该块的一部分，布局由硬件 MMA 决定。

二者分工：**共享内存放「待算的数据」，fragment 放「累加结果」**。

#### 4.2.2 核心流程

本内核一共申请了 4 块片上存储：

| 变量 | 类型 | 形状 | dtype | 用途 |
|---|---|---|---|---|
| `A_shared` | shared | `(block_M, block_K)` | `dtype`(int8) | 缓存 A 的一段 |
| `B_shared` | shared | `(block_N, block_K)` | `dtype`(int8) | 缓存 B 的一段 |
| `C_local` | fragment | `(block_M, block_N)` | `accum_dtype`(int32) | **累加器**，K 循环里累加 |
| `C_shared` | shared | `(block_M, block_N)` | `accum_dtype`(int32) | 回写时的中转 |

注意形状的搭配：`A_shared(block_M,block_K)` 与 `B_shared(block_N,block_K)` 经过 `B^T` 后做矩阵乘，结果正好是 `(block_M, block_N)`，与 `C_local`、`C_shared` 的形状一致。验证一下：

\[
\underbrace{(block_M,\, block_K)}_{A_{\text{shared}}} \times \underbrace{(block_K,\, block_N)}_{B_{\text{shared}}^{\top}} = \underbrace{(block_M,\, block_N)}_{C_{\text{local}}}
\]

> 为什么 A、B 用 int8 而 C 用 int32？因为 int8 × int8 的点积会累加很多次，中间结果远超 int8 表示范围，必须用更宽的累加类型（int32）才不溢出。这是低精度 GEMM 的通用做法，详见 u4-l12。

#### 4.2.3 源码精读

四块分配集中在 [benchmark_tilelang_matmul.py:213-219](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L213-L219)：

```python
A_shared = T.alloc_shared((block_M, block_K), dtype)       # A 的共享缓冲
B_shared = T.alloc_shared((block_N, block_K), dtype)       # B 的共享缓冲
C_local  = T.alloc_fragment((block_M, block_N), accum_dtype) # 累加器（寄存器）
C_shared = T.alloc_shared((block_M, block_N), accum_dtype)  # 回写中转（共享）
```

紧随其后的是 `T.clear(C_local)`（[L225](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L225)）：

```python
T.clear(C_local)
```

说明：**累加器在使用前必须清零**。因为 K 循环里做的是 `C_local += A_shared @ B_shared^T`，若 `C_local` 初始是脏值（寄存器残留），累加结果会全部错乱。`T.clear` 把整块 fragment 置 0。A_shared / B_shared 不需要清零，因为它们在每次循环里会被 `T.copy` 整块覆盖。

#### 4.2.4 代码实践

**实践目标**：核对四块存储的形状与 dtype 是否自洽。

**操作步骤**：

1. 打开 [benchmark_tilelang_matmul.py:213-225](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L213-L225)。
2. 取 `block_M=128, block_N=128, block_K=128`，`dtype=int8`（1 字节）、`accum_dtype=int32`（4 字节）。
3. 估算本块共享内存用量 = `A_shared + B_shared + C_shared`，以及 fragment 用量 = `C_local`。

**需要观察的现象**：算出 shared 与 fragment 各占多少字节。

**预期结果**：`A_shared = 128×128×1 = 16 KB`，`B_shared = 16 KB`，`C_shared = 128×128×4 = 64 KB`，shared 合计约 `96 KB`（这只是粗算；实际还会受流水多缓冲放大，见 4.5）；`C_local = 64 KB` 寄存器（实际由 block 内线程分摊，每个线程持有其中一份）。这也能解释为什么 `block_*` 不能无限放大——共享内存有容量上限（H100 每块最多约 228 KB 可用）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `C_local` 用 fragment 而不是 shared？
**答案**：`C_local` 是 Tensor Core MMA 指令的累加器，MMA 的输入来自共享内存、输出直接落到寄存器 fragment 里。把累加器放 fragment（寄存器）读写最快，且天然匹配 MMA 的输出布局，K 循环里每段的累加都不必经过较慢的共享内存。

**练习 2**：`C_shared` 是否可以省掉、让回写直接 `C_local → C 全局`？
**答案**：从「数值正确」角度，理论上有办法直接写；但从 TileLang 的 `T.copy` 抽象和访存效率角度，fragment 是「按线程寄存器分布」的、其逻辑布局与内存地址不是简单行优先对应，直接写全局难以做到合并（coalesced）访存。`C_shared` 作为中转，先把 fragment 重排成规整的内存布局，再做一次合并的全局写回。这正是本讲综合实践要深入解释的点。

---

### 4.3 模块三：T.copy——数据搬运

#### 4.3.1 概念说明

`T.copy(src, dst)` 是 TileLang 的数据搬运原语，负责在「全局 ↔ 共享 ↔ fragment」之间搬运一块数据。它屏蔽了底层是 `cp.async`、`ldmatrix` 还是普通 load/store 的细节，让用户只描述「从哪块、搬到哪块」。本内核里一共有 **4 处** `T.copy`：K 循环里 2 处（搬 A、搬 B 进来），回写阶段 2 处（C_local→C_shared、C_shared→C 全局）。

#### 4.3.2 核心流程

本内核的搬运路径可以画成一张图（箭头表示 `T.copy` 方向）：

```
        ┌──────────── K 循环内（每段执行一次）────────────┐
全局 A ──copy──▶ A_shared ──┐
全局 B ──copy──▶ B_shared ──┤
                            └──▶ T.gemm ──▶ C_local（累加器，+=）
        └────────────────────────────────────────────────┘

        ┌──────────── K 循环外（回写，只执行一次）─────────┐
C_local ──copy──▶ C_shared ──copy──▶ C（全局）
        └────────────────────────────────────────────────┘
```

关键点：

- **进**：全局 A/B → 共享 A_shared/B_shared。注意 `T.copy` 的「源」用 `A[by * block_M, k * block_K]` 这种**带偏移的区域表达式**，表示「从 A 的 `(by·block_M, k·block_K)` 位置开始取一个与目的同形的子块」。
- **算**：不是 `T.copy`，而是 `T.gemm`，把两个 shared 块相乘累加进 `C_local`。
- **出**：`C_local`（fragment）→ `C_shared`（shared）→ `C`（全局），分两步走（原因见 4.3.5 与综合实践）。

#### 4.3.3 源码精读

K 循环内的两处加载在 [benchmark_tilelang_matmul.py:230-232](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L230-L232)：

```python
T.copy(A[by * block_M, k * block_K], A_shared)   # A 的第 by 个行块、第 k 个 K 段
T.copy(B[bx * block_N, k * block_K], B_shared)   # B 的第 bx 个行块、第 k 个 K 段
```

说明：

- `A[by * block_M, k * block_K]` 表示从全局 A 起始偏移 `(by·block_M, k·block_K)`，取一个 `(block_M, block_K)` 子块——因为目的 `A_shared` 形状是 `(block_M, block_K)`，源区域自动对齐到这个形状。
- A 用 `by`（M 方向块号）定位行，因为本块算的是 C 的第 `by` 个行块，对应的 A 数据就在 A 的第 `by` 个行段。
- B 用 `bx`（N 方向块号）定位行——这是初学者最容易看漏的点：因为 B 的形状是 `(N, K)`，本块算的是 C 的第 `bx` 个列块，而 C 的列对应 B 的行（`C = A @ B^T`），所以要取 B 的第 `bx` 个行段。

回写阶段的两处在 [benchmark_tilelang_matmul.py:243-244](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L243-L244)：

```python
T.copy(C_local, C_shared)                        # fragment → shared
T.copy(C_shared, C[by * block_M, bx * block_N])  # shared → 全局，写到本块对应的输出位置
```

说明：

- 第一步把累加器 fragment 搬到 shared，做一次**布局重排**：fragment 是按线程寄存器分布的，shared 是按地址可索引的，这一步把数据「摊平」成规整布局。
- 第二步把 shared 的 `block_M × block_N` 块写到全局 C 的 `(by·block_M, bx·block_N)` 位置——注意这里偏移用 `by` 配 M、`bx` 配 N，与网格映射一一对应。
- 这两步都在 K 循环**之外**，意味着整个 K 维累加完成后只回写一次。

#### 4.3.4 代码实践

**实践目标**：把 4 处 `T.copy` 的「源/目的/层级/位置」整理成表，建立搬运全景。

**操作步骤**：

1. 通读 [benchmark_tilelang_matmul.py:228-244](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L228-L244)。
2. 列一张表，记录每次 `T.copy`：源（含偏移表达式）、目的、源所在层级、目的所在层级、是在 K 循环内还是外。

**需要观察的现象**：数据流向是否形成「全局→shared→fragment(算)→shared→全局」的闭环。

**预期结果**（这就是本讲综合实践要求整理的表的雏形）：

| # | 源 | 目的 | 源层级 | 目的层级 | 位置 |
|---|---|---|---|---|---|
| 1 | `A[by*block_M, k*block_K]` | `A_shared` | 全局 | shared | K 循环内 |
| 2 | `B[bx*block_N, k*block_K]` | `B_shared` | 全局 | shared | K 循环内 |
| 3 | `C_local` | `C_shared` | fragment | shared | K 循环外（回写） |
| 4 | `C_shared` | `C[by*block_M, bx*block_N]` | shared | 全局 | K 循环外（回写） |

#### 4.3.5 小练习与答案

**练习 1**：`T.copy(B[bx * block_N, k * block_K], B_shared)` 里为什么定位 B 用 `bx`（N 方向块号），而不是 `by`？
**答案**：因为本块算的是输出 C 的第 `bx` 个**列**块。在 `C = A @ B^T` 中，C 的第 n 列等于 A 乘上 B 的第 n 行（B 已是 `N×K`，相当于转置后的 `K×N` 的第 n 列 = B 的第 n 行）。所以取 B 的第 `bx` 个行段。用 `by` 会取错数据。

**练习 2**：回写为什么是 `C_local → C_shared → C` 两步，而不是 `C_local → C` 一步？
**答案**：`C_local` 是 fragment（寄存器），其逻辑元素按 MMA 硬件布局分散在各线程的寄存器里，与目标内存地址不是简单的行优先映射。直接写全局难以合并访存。先 `C_local → C_shared` 把数据重排成共享内存里规整、可索引的布局，再 `C_shared → C` 做一次合并的全局写，整体吞吐更高。这是 TileLang 块级 GEMM 的标准回写 idiom。

---

### 4.4 模块四：T.gemm——Tensor Core MMA

#### 4.4.1 概念说明

`T.gemm(A, B, C, ...)` 是块级矩阵乘加原语，对应一条或多条 Tensor Core MMA（int8 时是 IMMA）指令。它的语义是：

\[
C \leftarrow C + A \times B
\]

即「读 A、读 B，做矩阵乘，**加到** C 上」。注意是「加到」而非「赋值」——所以 C 必须先清零（见 4.2 的 `T.clear`），且 K 循环里每段都累加进同一个 `C_local`。

`T.gemm` 的输入 A、B 来自**共享内存**，输出累加器 C 是**fragment**——这与硬件 MMA 指令的数据通路完全一致。

#### 4.4.2 核心流程

本内核只调一次 `T.gemm`（在 K 循环内）：

\[
C_{\text{local}} \mathrel{+}= A_{\text{shared}} \times B_{\text{shared}}^{\top}
\]

其中：

- `A_shared ∈ ℤ^{block_M × block_K}`（int8）
- `B_shared ∈ ℤ^{block_N × block_K}`（int8），因 `transpose_B=True` 实际参与相乘的是 `B_shared^T ∈ ℤ^{block_K × block_N}`
- `C_local ∈ ℤ^{block_M × block_N}`（int32 累加器）

逐段累加的数学等价性：K 循环遍历 `k = 0, 1, …, ⌈K/block_K⌉-1`，每段加载 A、B 的一个 `block_K` 切片并累加。由于矩阵乘对 K 维的可加性：

\[
A \times B^{\top} = \sum_{k_b} A_{[:,\,k_b]} \times \left(B_{[:,\,k_b]}\right)^{\top}
\]

分块累加的结果与一次性算完整 K 维完全等价。这就是「K 维流水线累加」可行的数学基础。

> 参数补充：`policy=policy` 控制 warp 切分策略（来自搜索空间的 `GemmWarpPolicy.Square`），决定 block 内的 warp 如何瓜分 `block_M × block_N` 输出块；`transpose_B=True` 表示对 B 取转置后再乘。这二者深入讨论留到 u3-l11。

#### 4.4.3 源码精读

矩阵乘加在 [benchmark_tilelang_matmul.py:235-241](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L235-L241)：

```python
T.gemm(
    A_shared,
    B_shared,
    C_local,
    transpose_B=True,
    policy=policy,
)
```

说明：

- 前两个参数是输入（都来自 shared），第三个 `C_local` 既是输入（提供累加基）也是输出（写入累加结果）。这就是「`C += A @ B^T`」的语义。
- `transpose_B=True`：因为 B 的形状是 `(N, K)`，要让 `A(block_M,block_K) × B^T(block_K,block_N)` 维度匹配，必须对 B 转置。这也呼应了内核顶部 `C = A @ B^T` 的定义。
- `policy=policy`：warp 切分策略，来自 `get_configs`（见 [L80](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L80) 的 `T.GemmWarpPolicy.Square`）。
- 这一行会被编译成 INT8 Tensor Core（IMMA）指令，单条指令完成一个固定尺寸（如 int8 下 `16×16×32`）的小矩阵乘加，比标量 dp4a 高效得多。

#### 4.4.4 代码实践

**实践目标**：确认 `T.gemm` 的形状自洽与累加语义。

**操作步骤**：

1. 阅读 [benchmark_tilelang_matmul.py:228-241](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L228-L241)（整个 K 循环体）。
2. 取 `block_M=block_N=block_K=128`，写出第 `k=0` 段 `T.gemm` 的三个操作数形状。
3. 解释为什么 `T.gemm` 之前必须 `T.clear(C_local)`，以及为什么不需要对 `C_local` 在每段循环里重新清零。

**需要观察的现象**：累加器在循环开始前清零一次，之后每段都「加到」它上面。

**预期结果**：第 `k=0` 段 `A_shared(128,128) × B_shared(128,128)^T = (128,128)`，与 `C_local(128,128)` 匹配。`T.clear` 只在 K 循环**之前**调用一次（L225）；若在循环内每段都清零，就会把之前段的累加抹掉，等价于只算了最后一段，结果错误。

#### 4.4.5 小练习与答案

**练习 1**：`T.gemm` 的第三个参数 `C_local` 既是输入又是输出，会不会有数据竞争？
**答案**：不会。`C_local` 是 fragment，其 `block_M × block_N` 元素由 block 内各 warp/线程按 `policy` 切分，每个元素归唯一一个线程持有，不存在多线程写同一元素。MMA 指令本身也保证「读旧值、加新值、写回」是原子完成的。

**练习 2**：如果去掉 `transpose_B=True`，会发生什么？
**答案**：`A_shared(block_M, block_K) × B_shared(block_N, block_K)` 维度不匹配（第二个矩阵的行数 `block_N` 不等于第一个的列数 `block_K`，除非 `block_N == block_K`），编译期就会报错或算出错误形状。`transpose_B=True` 是为了让 B 以 `(block_K, block_N)` 的有效形态参与乘法。

---

### 4.5 模块五：T.Pipelined——软件流水

#### 4.5.1 概念说明

`T.Pipelined` 是 TileLang 表达**软件流水**的循环构造。普通 `for` 循环里，每段都是「先搬数据、再算」，搬运与计算串行，Tensor Core 在搬运时空闲。`T.Pipelined` 把循环体改成多级流水：当 Tensor Core 算第 `k` 段时，提前把后面几段的数据搬进共享内存，让「搬运」和「计算」重叠。

`num_stages` 控制流水深度：

- `num_stages = 0`：关闭流水，退化为普通串行循环。
- `num_stages = 1`：单级缓冲，基本算双缓冲的雏形。
- `num_stages = 2/3`：多级流水，搬算重叠更深，但需要更多共享内存做缓冲。

#### 4.5.2 核心流程

把 K 循环画成时间线（以 `num_stages=2` 为例）：

```
时间段:        t0       t1       t2       t3       t4       ...
搬 load k=0  [======]
搬 load k=1           [======]
算 gemm k=0                    [======]
搬 load k=2                             [======]
算 gemm k=1                                      [======]
算 gemm k=2                                               [======]
```

理想情况下，「搬」与「算」错峰重叠，Tensor Core 几乎不再等待搬运。代价是：流水需要在共享内存里同时持有 `num_stages` 份 `A_shared`/`B_shared` 缓冲（即 4.2 里粗算的 shared 用量会被放大），所以 `num_stages` 不能无限大——它和 `block_*` 一起受共享内存容量约束。这也解释了 u3-l8 里 `get_configs` 为什么要把 `num_stages ∈ {0,1,2,3}` 放进搜索空间（[L78](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L78)）：不同 shape 下最佳流水深度不同，需调优决定。

> 原理补充：流水的本质是用空间（多份缓冲）换时间（隐藏访存延迟）。一个 GEMM 是「访存密集」还是「计算密集」，取决于搬运量与计算量之比。分块越大，计算/搬运比越高，越值得用更深流水；但块越大共享内存占用也越多。这是 GEMM 调优的核心矛盾。

#### 4.5.3 源码精读

K 循环的流水构造在 [benchmark_tilelang_matmul.py:228](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L228)：

```python
for k in T.Pipelined(T.ceildiv(K, block_K), num_stages=num_stages):
    T.copy(A[by * block_M, k * block_K], A_shared)
    T.copy(B[bx * block_N, k * block_K], B_shared)
    T.gemm(A_shared, B_shared, C_local, transpose_B=True, policy=policy)
```

说明：

- `T.Pipelined(T.ceildiv(K, block_K), num_stages=num_stages)`：循环次数是 K 维的段数 `⌈K/block_K⌉`，`num_stages` 来自搜索空间（由 autotuner 在 `{0,1,2,3}` 里选最优，见 [L78](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L78) 与 [L156](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L156) 的 `num_stages=None` 占位）。
- 循环体只有三句：两搬一算。`T.Pipelined` 会自动把这三句改造成「搬算重叠」的流水调度，并隐式地为 `A_shared`/`B_shared` 分配多份缓冲（用户无需手写多缓冲）。
- 底层通常映射到 CUDA 的异步拷贝（`cp.async`）+ 多缓冲（multi-buffer），但 TileLang 把这些细节都藏在了 `T.Pipelined` 里。

#### 4.5.4 代码实践

**实践目标**：理解 `num_stages` 对行为的影响（源码阅读型，性能待本地验证）。

**操作步骤**：

1. 阅读 [benchmark_tilelang_matmul.py:228](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L228) 与 [L156](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L156)。
2. 假设你想手工对比 `num_stages=0` 与 `num_stages=2` 的差异。在 `get_configs` 的非 Roller 分支（[L75-L103](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L75-L103)）里，临时把 `num_stages` 列表改成 `[0]` 跑一次、再改成 `[2]` 跑一次（**只描述改动点，不要真的改源码**——本讲不允许修改源码，这是思路说明）。

**需要观察的现象**：理论上 `num_stages=0` 时搬算串行、Tensor Core 空闲多、latency 更高；`num_stages=2` 时搬算重叠、latency 更低，但共享内存占用更大。

**预期结果**：**待本地验证**。在真实 H100 + int8 shape 上跑 `benchmark_tilelang_matmul.sh`，对比两次的 `Best latency (s)`（注意：u3-l8 已指出该标签写 `(s)` 但实际单位是 ms，见 [L278](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L278)）。预期 `num_stages=2` 的 latency 明显小于 `num_stages=0`。

#### 4.5.5 小练习与答案

**练习 1**：`num_stages` 越大越好吗？限制因素是什么？
**答案**：不是。`num_stages` 越大，需要的共享内存缓冲越多（`A_shared`/`B_shared` 各要 `num_stages` 份），会撞上 block 共享内存上限；同时超出计算/搬运比所需的流水深度后，再加深也不再带来收益。所以它在 `{0,1,2,3}` 里被当作调优旋钮，由 autotuner 按实际 shape 选。

**练习 2**：如果把 `T.Pipelined(...)` 换成普通 `for k in range(...)`（假设 TileLang 允许），最直接的影响是什么？
**答案**：失去软件流水，搬运（`T.copy`）与计算（`T.gemm`）退化为串行，每段都要「等搬完才能算」，访存延迟无法隐藏，性能显著下降。这正是 `T.Pipelined` 存在的意义。

---

## 5. 综合实践

本讲综合实践就是把五要素串起来，完成一个完整的「数据搬运全景表 + 回写顺序解释」。

### 5.1 任务

通读整段内核 [benchmark_tilelang_matmul.py:191-246](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L191-L246)，完成两件事：

1. **整理 A/B/C 的 shared/fragment 分配表，以及每次 `T.copy` 的（源, 目的）表**。
2. **解释回写阶段为什么是 `C_local → C_shared → C 全局`，而不是直接 `C_local → C 全局`**。

### 5.2 参考答案

**第一张表：存储分配**

| 变量 | 分配原语 | 形状 | dtype | 所在层级 | 角色 |
|---|---|---|---|---|---|
| `A_shared` | `alloc_shared` | `(block_M, block_K)` | int8 | 共享内存 | 缓存 A 的一段（待算输入） |
| `B_shared` | `alloc_shared` | `(block_N, block_K)` | int8 | 共享内存 | 缓存 B 的一段（待算输入） |
| `C_local` | `alloc_fragment` | `(block_M, block_N)` | int32 | 寄存器 fragment | MMA 累加器（K 循环累加） |
| `C_shared` | `alloc_shared` | `(block_M, block_N)` | int32 | 共享内存 | 回写中转（布局重排） |

**第二张表：每次 T.copy 的（源, 目的）**

| # | 源（含偏移） | 目的 | 源层级 → 目的层级 | 在 K 循环内/外 | 行号 |
|---|---|---|---|---|---|
| 1 | `A[by*block_M, k*block_K]` | `A_shared` | 全局 → shared | 内 | [L230](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L230) |
| 2 | `B[bx*block_N, k*block_K]` | `B_shared` | 全局 → shared | 内 | [L232](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L232) |
| 3 | `C_local` | `C_shared` | fragment → shared | 外（回写） | [L243](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L243) |
| 4 | `C_shared` | `C[by*block_M, bx*block_N]` | shared → 全局 | 外（回写） | [L244](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L244) |

加上 K 循环内的 `T.gemm`（[L235-L241](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L235-L241)）：`A_shared, B_shared → C_local`（shared → fragment，累加）。整个数据路径成闭环：

```
全局 A ──①──▶ A_shared ──┐
全局 B ──②──▶ B_shared ──┤
                          └─gemm─▶ C_local ──③──▶ C_shared ──④──▶ 全局 C
              （K 循环内，每段累加）            （K 循环外，回写一次）
```

**第二问的解释（为什么先 `C_local → C_shared` 再 `→ C 全局`）**：

1. `C_local` 是 fragment（寄存器），其 `block_M × block_N` 个逻辑元素按 MMA 硬件输出布局分散在 block 内各线程的寄存器里，**与目标全局内存的行优先地址不是简单的对应关系**。
2. 若直接 `C_local → C 全局`，每个线程要根据 fragment 布局反推自己该写哪些全局地址，这些地址通常是散乱的，导致全局写**不合并**（non-coalesced），显存带宽利用率极差。
3. `C_local → C_shared` 这一步本质是一次**布局重排**：fragment 把数据交出到共享内存，由 block 内线程协作把它排成规整、可按地址索引的 `block_M × block_N` 块。共享内存是 block 内所有线程都可寻址的「公共黑板」，天然适合做这种重排。
4. `C_shared → C[by*block_M, bx*block_N]` 随后做一次**合并的 2D block store**：线程们按相邻地址协同写入，对齐良好，带宽利用率高。

简言之：**共享内存充当 fragment（寄存器分布布局）与全局内存（行优先布局）之间的「布局转换 + 合并写」中转站**，这是 TileLang 块级 GEMM 回写的标准 idiom，也是它比直接写回更高效的原因。

### 5.3 进阶（可选）

试着把本讲的五要素对应到 u2-l6 讲过的 Triton 基线：Triton 用 `tl.program_id` 做网格映射（对应 `T.Kernel`）、`tl.load`/`tl.store` 做搬运（对应 `T.copy`）、`tl.dot` 做乘加（对应 `T.gemm`）。你会发现 TileLang 的 `T.Pipelined` 和 fragment/shared 的显式分层是它比 Triton 更「贴近硬件」的地方——代价是写法更繁，收益是对 Tensor Core 调度和软流水有更细粒度的控制。

## 6. 本讲小结

- 块级 GEMM 的**五要素**：`T.Kernel`（网格映射）、`alloc_shared`/`alloc_fragment`（存储分配）、`T.copy`（数据搬运）、`T.gemm`（Tensor Core MMA）、`T.Pipelined`（软件流水）。
- `T.Kernel` 把输出 `(M,N)` 切成 `block_M × block_N` 块，`bx` 绑定 N 方向、`by` 绑定 M 方向；每个线程块算一个输出子块。
- 片上存储分两类：`alloc_shared` 给「待算输入」（A_shared/B_shared）和「回写中转」（C_shared），`alloc_fragment` 给「累加器」（C_local）；累加器使用前必须 `T.clear` 清零。
- K 维被切成 `block_K` 段循环累加，`T.gemm` 做 `C_local += A_shared @ B_shared^T`（`transpose_B=True`，因为 B 是 `N×K`）；分块累加在数学上等价于完整 K 维相乘。
- 回写是 `C_local → C_shared → C 全局` 两步：共享内存负责把 fragment 的寄存器分布布局重排成可合并写的规整布局。
- 本文件多处「注释与代码不符」：注释说 half-precision / accumulate float，代码实为 **int8 / int32**（驱动脚本也只跑 int8）。结构讲解与精度无关，int8 细节留到 u4-l12。

## 7. 下一步学习建议

- 本讲只解剖了**手工搜索空间**下的块级 GEMM 内核结构，但没讲搜索空间怎么被高效生成。下一步推荐 [u3-l10（tilelang.carver 与 Roller 自动调优）](u3-l10-roller-autotuning.md)：看 Roller 如何用 `MatmulTemplate.recommend_hints` 推导 top-10 个高质量 config，替代 `get_configs` 里 `itertools.product` 的 1296 个暴搜配置。
- 如果你对 `T.use_swizzle`、`policy=GemmWarpPolicy.Square` 这些「调优旋钮」如何影响 L2 命中与 bank conflict 感兴趣，接着读 [u3-l11（swizzle、warp 策略与调优旋钮）](u3-l11-swizzle-and-warp-policy.md)。
- 想了解 int8 / 多精度路径与 dtype 切换，跳到第 4 单元 [u4-l12（int8 与多精度 GEMM）](u4-l12-int8-multiprecision-gemm.md)。
- 也可以回顾 [u2-l6（Triton 基线）](u2-l6-triton-baseline.md)，对照「Triton 指针式内核」与「TileLang 声明式块级内核」在抽象层级上的差异，巩固本讲五要素的定位。
