# 第一个 kernel：quickstart 详解

## 1. 本讲目标

前两讲（u1-l1、u1-l2）我们搞清了「TileScale 是什么」和「怎么把 `tilelang` 装起来并 `import`」。本讲开始真正写代码：我们把仓库里最短小、也最经典的示例 [examples/quickstart.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py) 逐行拆开，端到端走通一个 TileLang 程序。

读完本讲，你应该能够：

1. 读懂一个完整的 TileLang kernel，说清每一段在做什么（启动、分配、搬运、计算、回写）。
2. 掌握 tile 声明（`T.alloc_shared` / `T.alloc_fragment`）、数据搬运（`T.copy`）、矩阵乘（`T.gemm`）和循环（`T.Pipelined` / `T.Parallel`）这几块最基础的写法。
3. 学会用 `@tilelang.jit` 把一个 Python 函数编译成可执行 kernel，用 PyTorch 张量去调用它，并和 PyTorch 的参考结果做数值校验。
4. 知道怎么用 `get_profiler().do_bench()` 量一个 kernel 的延迟。

> 承接 u1-l2：`tilelang` 已安装且能 `import` 是本讲的前提。本讲全程只用单 GPU 能力，不涉及分布式，因此**不依赖 `tilescale_ext`**——即使你环境里那个可选的分布式扩展没装，本讲的示例也能跑。

## 2. 前置知识

本讲会用到一点 GPU 与线性代数的常识。不熟悉没关系，先看这张表：

| 名词 | 通俗解释 |
| --- | --- |
| GEMM | General Matrix Multiply，矩阵乘 `C = A @ B`。它是深度学习里最核心、也最吃性能的算子，几乎所有 kernel 优化示例都从它开始。 |
| thread / block / grid | GPU 的执行层次：一个 block 里有多个 thread，一次启动的 block 集合叫 grid。TileLang 里 `T.Kernel(...)` 决定 grid 形状，`threads=` 决定每个 block 的线程数。 |
| global memory | 显卡主显存（即显存），容量大但慢。输入输出张量 `A/B/C` 就住在这里。 |
| shared memory | 每个 block 内部的一小块高速存储（SMEM），同一 block 的线程可共享。`T.alloc_shared` 分配的就是它。 |
| 寄存器 / fragment | 每个线程私有的最快存储。`T.alloc_fragment` 分配的「片段」最终落在寄存器/线程本地存储里，常用来做累加器。 |
| TMA / cp.async | 现代 GPU 上搬运显存块的异步指令。你不用直接写它们，`T.copy` 会在编译期替你选合适的搬运方式。 |
| tile（分块） | 把一个大矩阵切成小块（比如 128×128），每次只搬一小块到 shared memory 算。TileLang 的名字就来源于这种「按 tile 编程」的思想。 |
| 软件流水（software pipeline） | 计算当前 tile 的同时，提前搬运下一个 tile，用「重叠」隐藏访存延迟。`T.Pipelined(num_stages=...)` 就是开启它。 |

如果你写过一点 PyTorch，理解矩阵形状 `(M, K) @ (K, N) -> (M, N)` 就足够跟上本讲。

## 3. 本讲源码地图

本讲围绕一个文件展开，辅以若干语言原语定义：

| 文件 | 作用 |
| --- | --- |
| [examples/quickstart.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py) | 主角：一个 matmul + relu 的完整 TileLang 程序，含编译、运行、校验、测速。 |
| [tilelang/language/kernel.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py) | `T.Kernel` 的定义：grid/threads 配置、块索引绑定、各种 `get_*_binding` 辅助函数。 |
| [tilelang/language/allocate.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/allocate.py) | `alloc_shared` / `alloc_fragment` / `alloc_local` 等 tile 声明原语。 |
| [tilelang/language/copy.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/copy.py) | `T.copy` 的实现：把一段内存搬运编译成 `tl.copy` intrinsic。 |
| [tilelang/language/gemm_op.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/gemm_op.py) | `T.gemm` 的实现：编译成 `tl.tileop.gemm` intrinsic。 |
| [tilelang/language/pipeline.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/pipeline.py) | `T.Pipelined`：带 `num_stages` 的软件流水循环。 |
| [tilelang/language/parallel.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/parallel.py) | `T.Parallel`：元素级并行循环。 |
| [tilelang/jit/__init__.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/__init__.py) | `@tilelang.jit` 装饰器与 `compile`：把 Python 函数编译成 `JITKernel`。 |
| [tilelang/language/fill_op.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/fill_op.py) | `T.clear` / `T.fill`：把一段 tile 初始化为 0 或某常量。 |
| [tilelang/utils/tensor.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/tensor.py) | `TensorSupplyType`：profiler 用什么分布喂测试张量。 |

先看 quickstart 的全景（只保留骨架，省略细节）：

```python
@tilelang.jit
def matmul(M, N, K, block_M, block_N, block_K, dtype, accum_dtype):
    @T.prim_func
    def matmul_relu_kernel(A, B, C):
        with T.Kernel(grid_x, grid_y, threads=128) as (bx, by):
            # 1. 分配 tile
            # 2. T.clear 清零累加器
            # 3. T.Pipelined 循环：T.copy + T.gemm
            # 4. T.Parallel 做 relu
            # 5. T.copy 把结果写回 C
    return matmul_relu_kernel

kernel = matmul(M, N, K, ...)      # 编译
kernel(a, b, c)                    # 运行
torch.testing.assert_close(...)    # 校验
kernel.get_profiler().do_bench()   # 测速
```

下面四个小节分别拆解这五步中的四个最小模块。

## 4. 核心概念与源码讲解

### 4.1 T.Kernel：启动上下文与 (bx, by) 绑定

#### 4.1.1 概念说明

`T.Kernel(...)` 是一个 **上下文管理器**（`with` 语句）。它干两件事：

1. 声明这个 kernel 启动时的 **grid 形状**（多少个 block）和 **threads 数量**（每个 block 多少线程）。
2. 给你返回 **块索引变量**（`bx`, `by`, ...），你在 kernel 体内用它们区分「当前 block 负责数据的哪一块」。

这是 TileLang 对底层 CUDA `<<<grid, threads>>>` 启动配置的封装：你不必手写 `blockIdx.x`，只要在 `with T.Kernel(...) as (bx, by):` 里直接用 `bx`、`by`。

> 一个最常见的坑：`T.Kernel` 的第一个参数对应 `blockIdx.x`（即 `bx`），第二个对应 `blockIdx.y`（即 `by`）。quickstart 里 `bx` 绑的是 N 维度、`by` 绑的是 M 维度——下面会讲为什么。

#### 4.1.2 核心流程

quickstart 的启动配置是：

```python
with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=128) as (bx, by):
```

执行语义等价于：

1. 算出 grid 形状：`grid_x = ceil(N / block_N)`、`grid_y = ceil(M / block_M)`。
2. 每个 block 拿到一对索引 `(bx, by)`，其中 `bx ∈ [0, grid_x)`、`by ∈ [0, grid_y)`。
3. `threads=128` 表示每个 block 跑 128 个线程（编译期绑定到 `threadIdx`）。
4. `T.ceildiv` 是向上取整除法，保证 M、N 不能被块大小整除时也不丢数据。

为什么 `bx` 绑 N、`by` 绑 M？因为这是分块矩阵乘的自然选择：

- 矩阵 `C` 形状 `(M, N)`，被切成 `(block_M, block_N)` 的小块。
- 第 `(by, bx)` 个 block 负责计算 `C` 的第 `by*block_M : (by+1)*block_M` 行、第 `bx*block_N : (bx+1)*block_N` 列。

所以 kernel 体内你会看到 `C[by * block_M, bx * block_N]`（行用 by，列用 bx），方向一致。

#### 4.1.3 源码精读

quickstart 的启动行：

[examples/quickstart.py:17](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py#L17) —— 用 `T.ceildiv` 算 grid、`threads=128`、解包出 `(bx, by)`。

`T.Kernel` 本身的定义在 [tilelang/language/kernel.py:228-302](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L228-L302)。关键几行：

- [kernel.py:284-285](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L284-L285)：不指定 `threads` 时默认 128（quickstart 显式写了 `threads=128`，和默认值一致）。
- [kernel.py:287-292](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L287-L292)：把 `threads` 统一规整成 `[tx, ty, tz]` 三元组（缺省补 1）。
- [kernel.py:302](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L302)：最终调用 `_ffi_api.KernelLaunch(...)`，进入 C++ 构造一个 `KernelLaunchFrame`。

进入 `with` 时，`__enter__` 返回块索引变量：

[kernel.py:101-121](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L101-L121) —— 返回前若干个 frame 的迭代变量作为 `(bx, by, ...)`；末尾 4 个 frame 留给 `threadIdx.x/y/z` 和 block 属性，不暴露给用户。

此外这个类还提供一组调试用的辅助函数，未来你想在 kernel 体内查「我有几个线程」「blockIdx 是谁」时会用到：

- [kernel.py:170-182](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L170-L182)：`get_thread_binding` / `get_thread_bindings`，取 `threadIdx` 绑定。
- [kernel.py:193-204](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L193-L204)：`get_block_binding` / `get_block_bindings`，取 `blockIdx` 绑定（即 `bx/by/bz`）。
- 顶层还导出了不带 `Current()` 前缀的便捷函数 `T.get_thread_binding()`、`T.get_block_binding()` 等（[kernel.py:305-350](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L305-L350)）。

#### 4.1.4 代码实践

**目标**：亲眼确认「`bx` 绑第一个参数、`by` 绑第二个参数」。

**操作步骤**：

1. 复制 `examples/quickstart.py` 为 `my_launch.py`。
2. 把 kernel 体内的 `T.copy(C_local, C[by * block_M, bx * block_N])` 临时改成只写一个角：`T.copy(C_local, C[0, 0])`（让所有 block 都写到左上角，制造「错误」）。
3. 重新运行 `python my_launch.py`。

**需要观察的现象**：校验 `assert_close` 会**失败**（多个 block 抢着写同一个输出位置），从而证明 `by/bx` 确实在驱动每个 block 写到不同位置。

**预期结果**：终端抛出 `AssertionError`，报数值不匹配。这反向印证了 `bx/by` 绑定是有效的。

> 若无法运行（无 GPU），标注「待本地验证」，但你可以在脑中走一遍这个推理。

#### 4.1.5 小练习与答案

**练习 1**：如果矩阵是 `C: (M, N)` 且你想让 `bx` 绑 M 维、`by` 绑 N 维，应该怎么改 `T.Kernel(...)` 和 `as (...)`？写出对应的 `C[?, ?]` 下标。

**答案**：把启动写成 `with T.Kernel(T.ceildiv(M, block_M), T.ceildiv(N, block_N), threads=128) as (bx, by):`，此时 `bx` 绑 M（行）、`by` 绑 N（列），回写应为 `C[bx * block_M, by * block_N]`。绑哪个维度纯粹由参数顺序决定。

**练习 2**：`T.Kernel` 不传 `threads` 会怎样？

**答案**：默认 `threads=128`（见 [kernel.py:284-285](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py#L284-L285)），等价于 quickstart 里显式写的 `threads=128`。

---

### 4.2 多级显存 tile 声明与 T.copy 数据搬运

#### 4.2.1 概念说明

GPU 有明显的显存层级：global（大而慢）→ shared（小而快、block 内共享）→ 寄存器/fragment（最快、线程私有）。高性能 kernel 的核心套路就是「把数据从 global 分块搬到 shared，再在 shared 上做矩阵乘，结果暂存在 fragment 累加器里，最后搬回 global」。

TileLang 用三个原语直接对应这三级：

| 原语 | 分配在哪 | 典型用途 |
| --- | --- | --- |
| `T.alloc_shared(shape, dtype)` | shared memory | 缓存当前 tile 的 `A`、`B` |
| `T.alloc_fragment(shape, dtype)` | 寄存器/线程本地（fragment） | 放累加器 `C_local` |
| `T.alloc_local(shape, dtype)` | local memory（线程私有，走寄存器/栈） | 标量临时变量 |

`T.copy(src, dst)` 则负责在任意两级之间搬运一整块数据，并自动选最快的搬运指令（TMA / cp.async / LSU）。

#### 4.2.2 核心流程

quickstart 的「分配 + 搬运」部分：

```python
A_shared = T.alloc_shared((block_M, block_K), dtype)      # shared 上的 A tile
B_shared = T.alloc_shared((block_K, block_N), dtype)      # shared 上的 B tile
C_local  = T.alloc_fragment((block_M, block_N), accum_dtype)  # 寄存器上的累加器

for ko in T.Pipelined(T.ceildiv(K, block_K), num_stages=3):
    T.copy(A[by * block_M, ko * block_K], A_shared)        # global -> shared
    T.copy(B[ko * block_K, bx * block_N], B_shared)        # global -> shared
    T.gemm(A_shared, B_shared, C_local)                    # shared -> fragment 累加
...
T.copy(C_local, C[by * block_M, bx * block_N])             # fragment -> global
```

数据流向（一个 block 内）：

```text
A[行 by]  --copy--> A_shared (shared)  --\
                                           +--> T.gemm --> C_local (fragment) --copy--> C[行 by, 列 bx]
B[列 bx]  --copy--> B_shared (shared)  --/
```

注意 `A[by * block_M, ko * block_K]` 这种写法：传一个**左上角坐标**，`T.copy` 会根据 `A_shared` 的形状自动推断要搬 `(block_M, block_K)` 这么大的一块。

#### 4.2.3 源码精读

quickstart 的分配行：[examples/quickstart.py:18-20](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py#L18-L20)。注意 `C_local` 的 dtype 是 `accum_dtype=float32`，而 `A/B/C` 是 `float16`——矩阵乘用高精度累加是惯例。

三次 `T.copy`：[examples/quickstart.py:31](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py#L31)、[examples/quickstart.py:34](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py#L34)、[examples/quickstart.py:45](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py#L45)。

分配原语的实现：

- [allocate.py:40-55](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/allocate.py#L40-L55)：`alloc_shared` 调用 `T.alloc_buffer(shape, dtype, scope="shared.dyn")`，scope 决定它落在 shared memory。
- [allocate.py:72-83](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/allocate.py#L72-L83)：`alloc_fragment` 用 `scope="local.fragment"`，这正是后面 `T.gemm` 能映射到 tensor core 累加器的关键。

`T.copy` 的实现：[copy.py:12-31](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/copy.py#L12-L31)。它的本质是把源和目的都转成「tile region」，再发出一个 `tl.copy` intrinsic：

[copy.py:88](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/copy.py#L88) —— `tir.call_intrin("handle", tir.op.Op.get("tl.copy"), src, dst, ...)`。`T.copy` 还支持 `disable_tma`、`eviction_policy` 等参数（[copy.py:12-18](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/copy.py#L12-L18)），本讲用不到，知道即可。

> 小知识：`fragment` 与 `local` 的区别在于 layout。`fragment` 会被编译器推断成「跨线程的碎片化布局」（正好喂给 mma/wgmma 指令），`local` 则是普通的线程私有标量存储。所以**累加器要用 `alloc_fragment`，不要用 `alloc_local`**。

#### 4.2.4 代码实践

**目标**：直观感受「数据在 shared 中转」这步的存在。

**操作步骤**：

1. 复制 quickstart，在 `T.copy(A[...], A_shared)` 之后、`T.gemm(...)` 之前，临时加一句把 `A_shared` 清零：`T.clear(A_shared)`。
2. 运行 `python my_copy_test.py`。

**需要观察的现象**：因为 A_shared 被清零，gemm 算出来基本是 0（B 还在），最终 `C` 的结果与参考值 `relu(a@b)` 严重不符。

**预期结果**：`assert_close` 失败。这说明 `A_shared` 确实是 gemm 的输入中转站。**待本地验证**（无 GPU 时仅作推理）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `C_local` 用 `accum_dtype=float32` 而不是和 `A/B` 一样用 `float16`？

**答案**：矩阵乘过程中要累加很多项，低精度累加误差大。用 float32 累加、最后再降回 float16 写回 global，是兼顾精度与显存的常见做法。

**练习 2**：`T.copy(A[by*block_M, ko*block_K], A_shared)` 里，`T.copy` 怎么知道要搬多大一块？

**答案**：`A_shared` 的形状是 `(block_M, block_K)`，`T.copy` 用目的 tile 的形状推断搬运范围（参见 [copy.py:50-62](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/copy.py#L50-L62) 中对 `src_extent`/`dst_extent` 的处理）。

---

### 4.3 T.Pipelined 软件流水、T.gemm 与 T.Parallel 元素并行

#### 4.3.1 概念说明

这一节串起三件事：

- **`T.Pipelined(num_stages=N)`**：把一个 for 循环变成「多缓冲软件流水」。当你在算第 `k` 个 tile 时，已经在后台搬运第 `k+1`、`k+2` 个 tile，从而把访存和计算重叠起来。
- **`T.gemm(A, B, C)`**：tile 级矩阵乘，输入在 shared、输出累加到 fragment。编译器会根据目标架构分发到对应的指令（NVIDIA 的 mma/wgmma、AMD 的 mfma 等）。
- **`T.Parallel(...)`**：元素级并行循环。循环体里每个元素的运算互相独立，编译器会把它们分摊给 block 内的所有线程并行执行。

quickstart 用 `T.Pipelined` 把 K 维循环跑成流水，用 `T.gemm` 做核心计算，最后用 `T.Parallel` 对每个累加器元素套一层 relu。

#### 4.3.2 核心流程

```python
T.clear(C_local)                                      # 累加前先清零
for ko in T.Pipelined(T.ceildiv(K, block_K), num_stages=3):
    T.copy(A[by*block_M, ko*block_K], A_shared)       # 搬当前 tile
    T.copy(B[ko*block_K, bx*block_N], B_shared)
    T.gemm(A_shared, B_shared, C_local)               # C_local += A_shared @ B_shared

for i, j in T.Parallel(block_M, block_N):             # 对每个元素并行
    C_local[i, j] = T.max(C_local[i, j], 0)           # relu
```

软件流水的直觉（`num_stages=3`）：

```text
时间 →
tile 0: [搬A0/B0][算0]
tile 1:          [搬A1/B1][算1]
tile 2:                   [搬A2/B2][算2]
...（搬运与计算重叠，访存延迟被隐藏）
```

`num_stages` 是「生产者和消费者之间最多缓冲几个 stage」：值越大，重叠越多，但占用的 shared memory 也越多（要开多份缓冲）。

#### 4.3.3 源码精读

quickstart 的循环与计算：[examples/quickstart.py:26](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py#L26)（`T.clear`）、[examples/quickstart.py:28](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py#L28)（`T.Pipelined`）、[examples/quickstart.py:38](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py#L38)（`T.gemm`）、[examples/quickstart.py:41-42](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py#L41-L42)（`T.Parallel` + relu）。

原语实现：

- `T.clear` 的本质是「fill 0」：[fill_op.py:39-62](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/fill_op.py#L39-L62)，内部调用 `fill(buffer, 0)`（[fill_op.py:9-36](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/fill_op.py#L9-L36)）。
- `T.Pipelined`：[pipeline.py:10-47](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/pipeline.py#L10-L47)。参数 `num_stages` 含义在 docstring 里写得很清楚：[pipeline.py:27-30](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/pipeline.py#L27-L30) ——「生产者与消费者之间最多使用的缓冲数；为 0 则不开启流水」。它最终发出一个 `_ffi_api.Pipelined(...)`（[pipeline.py:47](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/pipeline.py#L47)）。
- `T.gemm`：[gemm_op.py:191-202](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/gemm_op.py#L191-L202) 是公开接口，它最终通过 `_gemm_impl` 发出 `tl.tileop.gemm` intrinsic（[gemm_op.py:104-126](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/gemm_op.py#L104-L126)）。注意它有 `transpose_A`/`transpose_B`、`policy: GemmWarpPolicy`、`clear_accum` 等参数——quickstart 都用了默认值（不转置、不清累加）。
- `T.Parallel`：[parallel.py:10-30](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/parallel.py#L10-L30)，发出 `_ffi_api.Parallel(...)`。它支持多维（这里传了 `block_M, block_N` 两个 extent）。

> `T.max`、`T.exp`、`T.tanh`、`T.sqrt` 这些数学函数来自 TVM 的 tir 命名空间（`tilelang/language/__init__.py` 里有 `from tvm.script.parser.tir import *`），所以可以直接写 `T.max(...)`。TileLang 自己在 [math_intrinsics.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/math_intrinsics.py) 里还提供了一批带 `tl.__exp`/`tl.__log` 等前缀的「快速数学」版本（如 [math_intrinsics.py:133-147](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/math_intrinsics.py#L133-L147)），追求极致速度时可以替换。

#### 4.3.4 代码实践

**目标**：体会 `num_stages` 对延迟的影响（软件流水到底有没有用）。

**操作步骤**：

1. 复制 quickstart。
2. 把 `for ko in T.Pipelined(T.ceildiv(K, block_K), num_stages=3)` 的 `num_stages` 分别改成 `1` 和 `3`，各编译一次。
3. 用脚本末尾的 `profiler.do_bench()` 各测一次延迟（见 4.4 节）。

**需要观察的现象**：`num_stages=3` 的延迟通常**小于** `num_stages=1`（后者基本等于不开流水）。

**预期结果**：记录两个延迟数，差距取决于你的 GPU 与矩阵规模。**待本地验证**（需要真实 GPU 才能量延迟）。无 GPU 时，可作为后续 u4-l2「软件流水」一讲的伏笔。

#### 4.3.5 小练习与答案

**练习 1**：`T.Pipelined` 的 `num_stages` 调得越大一定越好吗？

**答案**：不一定。更大的 `num_stages` 需要更多 shared memory 做缓冲，可能挤占可用资源、甚至超出硬件上限；收益也会随 tile 数减少而饱和。通常 2~4 是常见取值，需要实测权衡。

**练习 2**：为什么 relu 用 `T.Parallel` 而 gemm 不用？

**答案**：`T.gemm` 是一个已经被编译器映射到 tensor core 的高层算子，内部自带并行；`T.Parallel` 是「我自己写的元素级循环」，需要显式声明可并行，让编译器把 `block_M*block_N` 个元素分摊给 block 内 128 个线程。

---

### 4.4 编译、调用与 torch 参考结果校验

#### 4.4.1 概念说明

到目前为止，我们写的还只是「描述计算」的 Python 函数。要让它真正在 GPU 上跑，需要经过编译。TileLang 提供了 `@tilelang.jit` 装饰器：被它装饰的函数在「被调用」时会按传入的参数（形状、dtype、块大小）即时编译（JIT）成一个可执行的 `JITKernel`，并把结果缓存起来，第二次同样参数调用直接复用。

校验正确性的套路很朴素：**用 PyTorch 算一遍参考答案，再和 kernel 输出逐元素比对**。`torch.testing.assert_close` 不抛异常即视为通过。

#### 4.4.2 核心流程

quickstart 的「编译 → 运行 → 校验 → 测速」：

```python
@tilelang.jit                         # 0. 标记为 JIT 函数
def matmul(M, N, K, ...):
    @T.prim_func
    def matmul_relu_kernel(A, B, C): ...
    return matmul_relu_kernel

kernel = matmul(M, N, K, block_M, block_N, block_K)   # 1. 首次调用 → 编译，得到 JITKernel

a = torch.randn(M, K, device="cuda", dtype=torch.float16)   # 2. 准备输入
b = torch.randn(K, N, device="cuda", dtype=torch.float16)
c = torch.empty(M, N, device="cuda", dtype=torch.float16)

kernel(a, b, c)                       # 3. 运行：结果写进 c

ref_c = torch.relu(a @ b)             # 4. 参考答案
torch.testing.assert_close(c, ref_c, rtol=1e-2, atol=1e-2)   # 5. 校验

profiler = kernel.get_profiler(tensor_supply_type=tilelang.TensorSupplyType.Normal)
latency = profiler.do_bench()         # 6. 测延迟
```

几个要点：

- `@T.prim_func` 把内层函数标记成一个 TVM 的 `PrimFunc`（TIR 函数）；`matmul` 外层函数接受「形状/块大小」这些**编译期参数**，返回这个 `PrimFunc`。
- `@tilelang.jit` 装饰的是**外层** `matmul`。调用 `matmul(M, N, ...)` 触发编译，返回 `JITKernel`。
- `JITKernel` 可像普通函数一样 `kernel(a, b, c)` 调用，参数是**运行期张量**。

#### 4.4.3 源码精读

quickstart 的编译与校验：[examples/quickstart.py:8](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py#L8)（`@tilelang.jit`）、[examples/quickstart.py:58](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py#L58)（编译）、[examples/quickstart.py:63-68](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py#L63-L68)（准备张量并运行）、[examples/quickstart.py:72](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py#L72)（参考答案）、[examples/quickstart.py:75](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py#L75)（校验）、[examples/quickstart.py:83-87](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py#L83-L87)（测速）。

`@tilelang.jit` 的定义：[jit/__init__.py:450-461](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/__init__.py#L450-L461)。它支持「裸用」`@tilelang.jit` 或「带参」`@tilelang.jit(target="cuda", ...)`（quickstart 用的是裸用）。

被装饰后返回的是一个 `JITImpl` 对象（[jit/__init__.py:506-524](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/__init__.py#L506-L524)）。调用它会：

1. `get_tir(...)`：执行你的外层函数，拿到 `PrimFunc`（[jit/__init__.py:297-310](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/__init__.py#L297-L310)）。
2. `compile(...)`：走 TVM 编译流水线，产出 `JITKernel`（[jit/__init__.py:357-383](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/__init__.py#L357-L383)）。
3. 按参数做**缓存**：同样的 `(M,N,K,block_M,block_N,block_K)` 第二次调用直接复用，不重新编译（[jit/__init__.py:419-426](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/__init__.py#L419-L426)）。

`JITKernel` 本身的 `__call__` 负责「把 torch/dlpack 张量喂进编译好的 kernel 并执行」（[jit/kernel.py:194](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/kernel.py#L194) 及其后）。

`TensorSupplyType` 是 profiler 喂测试张量的分布选择，定义在 [tilelang/utils/tensor.py:28-35](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/tensor.py#L28-L35)：`Normal`（正态分布）、`Uniform`、`Integer`、`Zero`、`One` 等。matmul 这类对数值敏感的算子，用 `Normal` 比较接近真实分布。

> 容错提示：`assert_close` 用的 `rtol=1e-2, atol=1e-2` 比较宽松，因为 float16 矩阵乘本身误差就不小。如果你把 dtype 全换成 float32，可以把容限收紧。

#### 4.4.4 代码实践

**目标**：把 quickstart 完整跑通，并取出它生成的 CUDA 源码。

**操作步骤**：

1. 确认环境（承接 u1-l2）：`python -c "import tilelang; print(tilelang.__version__)"` 能正常打印。
2. 运行：`python examples/quickstart.py`。
3. 打开示例末尾注释掉的 `[examples/quickstart.py:79](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py#L79)` 那两行（`get_kernel_source()`），打印生成的 CUDA。

**需要观察的现象**：

- 终端先打印张量 `c`（一个 `(1024, 1024)` 的 float16 矩阵）。
- 打印 `Kernel output matches PyTorch reference.`。
- 打印 `Latency: <某毫秒数> ms`。
- 生成的 CUDA 源码里能看到由 `T.gemm`/`T.copy` 展开出的设备模板调用（具体形态取决于你的架构）。

**预期结果**：校验通过、延迟打印成功。CUDA 源码的具体内容**待本地验证**（与你的 GPU 架构有关）。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `matmul(M, N, K, block_M, block_N, block_K)` 连续调用两次（同样参数），会编译两次吗？

**答案**：不会。`JITImpl` 用参数构造 cache key，命中就复用（[jit/__init__.py:419-426](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/__init__.py#L419-L426)）。改了块大小才会重新编译。

**练习 2**：`@tilelang.jit` 和 `@T.prim_func` 各装饰哪一层？能不能去掉其中一个？

**答案**：`@T.prim_func` 装饰**内层** kernel 函数（把它标成 TIR 函数），`@tilelang.jit` 装饰**外层**返回 PrimFunc 的工厂函数（负责编译）。去掉 `@T.prim_func`，内层就不是合法的 TIR 函数；去掉 `@tilelang.jit`，`matmul(...)` 只会返回一个裸 `PrimFunc`，不能直接 `.cuda()`/`kernel(a,b,c)` 运行。

---

## 5. 综合实践：把 relu 换成 gelu

本任务把第 4 节的四个模块串起来。你要在读懂 quickstart 全貌之后，做一处**有意义的修改**：把输出端的激活函数从 `relu` 改成 `gelu`，并保证数值校验通过。

### 5.1 背景与公式

GELU 是比 ReLU 更平滑的激活函数，Transformer 里几乎必用。它的 tanh 近似形式为：

\[
\mathrm{GELU}(x) \approx 0.5\,x\,\left(1 + \tanh\!\left(\sqrt{\tfrac{2}{\pi}}\,\left(x + 0.044715\,x^{3}\right)\right)\right)
\]

其中 \(\sqrt{2/\pi} \approx 0.7978845608\)。

quickstart 现在的激活是：

```python
for i, j in T.Parallel(block_M, block_N):
    C_local[i, j] = T.max(C_local[i, j], 0)      # relu
```

### 5.2 操作步骤

1. 复制 `examples/quickstart.py` 为 `my_gelu.py`。
2. 把上面那段 `T.Parallel` 循环体替换为 gelu 的 tanh 近似（**示例代码**，注意 `C_local` 是 float32 累加器，常数写成浮点）：

   ```python
   # 示例代码：GELU(tanh 近似)
   for i, j in T.Parallel(block_M, block_N):
       x = C_local[i, j]
       inner = 0.7978845608 * (x + 0.044715 * x * x * x)
       C_local[i, j] = 0.5 * x * (1.0 + T.tanh(inner))
   ```

   说明：`T.tanh` 来自 TVM tir 命名空间（见 4.3.3 的说明）。如果你用的是不支持 `tanh` 的后端，可改用「快速数学」`T.__exp` 自行构造（[math_intrinsics.py:133-147](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/math_intrinsics.py#L133-L147)）或 sigmoid 近似。
3. 把参考答案也改成 gelu，并且**用同样的 tanh 近似**比对（因为 PyTorch 默认 gelu 用的是精确 erf 形式，和 tanh 近似有微小差异）：

   ```python
   ref_c = torch.nn.functional.gelu(a @ b, approximate="tanh")
   ```

   容差可保持 `rtol=1e-2, atol=1e-2`，必要时适当放宽。
4. 运行 `python my_gelu.py`。

### 5.3 需要观察的现象与预期结果

- `assert_close` 通过，打印类似 `Kernel output matches PyTorch reference.`。
- 输出矩阵不再像 relu 那样把负数直接截断为 0，而是负数会变成接近 0 但略小/略大的平滑值。
- 若 `T.tanh` 在你的目标后端报错，回退到基于 `T.__exp` 的 sigmoid 近似（`gelu(x) ≈ x * sigmoid(1.702 * x)`）。

### 5.4 进阶（可选）

- 用 `profiler.do_bench()` 对比 relu 版与 gelu 版的延迟，体会「elementwise 数学函数比 `T.max` 贵多少」。
- 把 `block_K` 调大（如 64），观察 `T.Pipelined` 的缓冲与延迟变化。

> 所有性能数字与最终 CUDA 形态都与具体 GPU 相关，**待本地验证**。

## 6. 本讲小结

- `T.Kernel(grid_x, grid_y, threads=...) as (bx, by)` 是启动配置：第一个参数绑 `blockIdx.x`（`bx`），第二个绑 `blockIdx.y`（`by`）；不传 `threads` 默认 128。
- tile 编程的核心是**显存分级**：`alloc_shared` 缓存输入 tile、`alloc_fragment` 放累加器、`alloc_local` 放标量；`T.copy(src, dst)` 在各级之间搬运一整块数据。
- `T.Pipelined(num_stages=N)` 把 K 维循环变成多缓冲软件流水，隐藏访存延迟；`T.gemm` 是映射到 tensor core 的 tile 级矩阵乘；`T.Parallel` 声明元素级并行循环。
- `@tilelang.jit` 把「返回 PrimFunc 的工厂函数」变成 JIT 入口：首次按参数编译成 `JITKernel` 并缓存，之后 `kernel(a, b, c)` 直接执行。
- 正确性靠「PyTorch 参考答案 + `torch.testing.assert_close`」校验；性能靠 `kernel.get_profiler().do_bench()` 量延迟，张量分布由 `tilelang.TensorSupplyType` 控制。
- quickstart 是后续所有讲义的「最小公倍数」：u2 会展开每个 `T.*` 原语，u3 会展开编译流水线，u4 会展开 `Pipelined`/layout 等优化。

## 7. 下一步学习建议

- **横向吃透原语**：进入 Unit 2。先读 [tilelang/language/kernel.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/kernel.py)（u2-l1，`T.Kernel` 的线程/块绑定），再读 [allocate.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/allocate.py)（u2-l2，显存层级）和 [gemm_op.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/gemm_op.py)/[reduce_op.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/reduce_op.py)（u2-l3，计算原语）。
- **纵向看编译**：想搞懂「我写的这些 `T.copy`/`T.gemm` 到底怎么变成 CUDA」，进入 Unit 3，从 [tilelang/engine/lower.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/engine/lower.py)（u3-l1）开始。
- **动手建议**：在跑通本讲的基础上，先尝试 Unit 2 的 elementwise/小 reduce kernel，再回头看 quickstart，你会对「tile 分块 + 软件流水」有完全不同的理解。
