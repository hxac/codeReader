# 完整 GEMM 例子精读

## 1. 本讲目标

本讲是 GEMM 系列的收口讲义。在前面的讲义里，你已经分别学过了内存层级（u2-l4）、软件流水线（u4-l4）、gemm 分派（u4-l2）、张量核发射器（u6-l1、u6-l2）。这些知识此前是分散的，本讲要把它们**串成一条完整的端到端链路**。

读完本讲你应当能够：

- 逐行读懂 `examples/gemm/example_gemm.py`，并能解释「搬进来—算—搬出去」每一行背后发生了什么。
- 理解什么是 **persistent kernel（持久化线程块）**，为什么它比普通 kernel 更省 launch 开销、对 L2 cache 更友好。
- 对照 `examples/gemm/example_gemm_persistent.py`，看懂普通版与持久化版的差异，并理解两者的性能差距来源。
- 掌握 GEMM 的**调参维度**（block_M/block_N/block_K、num_stages、threads），知道每个参数动了什么、该怎么量。
- 理解当矩阵尺寸不是 block 整数倍时的**边界处理**机制。

## 2. 前置知识

本讲假设你已掌握以下概念（若不熟悉请先回看对应讲义）：

- **四级显存层级**：global / shared / fragment / local，以及 `T.copy`、`T.alloc_shared`、`T.alloc_fragment` 的作用（u2-l4）。
- **软件流水线** `T.Pipelined(num_stages=...)`：用多份 shared 缓冲把「搬运」和「计算」重叠起来（u4-l4）。
- **T.gemm 的分派**：同一个 `T.gemm` 在不同 target 下会被自动映射到 mma / wgmma / mfma 指令（u4-l2）。
- **layout 推断**：fragment 的寄存器布局由编译器从各算子自动反推（u4-l3）。
- **JITKernel 对象**：`matmul.compile(...)` 的返回值，提供 `__call__`、`get_kernel_source`、`get_profiler` 等方法（u3-l2）。

如果你直接读本讲，记住一句话：TileLang 写的是「算什么」（规格），而「怎么搬数据、用哪条张量核指令」由编译器按 target 自动决定。

## 3. 本讲源码地图

本讲围绕以下文件展开：

| 文件 | 作用 |
| --- | --- |
| `examples/gemm/example_gemm.py` | 最精简的 GEMM kernel，约 70 行，是「搬进来—算—搬出去」的最小完整范式 |
| `examples/gemm/example_gemm_persistent.py` | 同时给出普通版与持久化版 GEMM，并做性能对比 |
| `examples/gemm/README.md` | 官方对 GEMM 例子的注释式讲解，含 swizzle、Parallel copy、细粒度 MMA |
| `docs/deeplearning_operators/matmul.md` | 官方 GEMM 教程，解释 Level 1/2/3 抽象与各原语 |
| `tilelang/language/loop.py` | `T.Persistent` 的 Python 定义 |
| `tilelang/language/annotations.py` | `T.use_swizzle`（L2 栅格化）的定义 |
| `src/cuda/transform/persist_threadblock.cc` | 持久化 kernel 的编译期处理（cooperative groups 标注） |
| `tilelang/carver/arch/driver/cuda_driver.py` | `get_num_sms`：查询设备 SM 数量 |

## 4. 核心概念与源码讲解

### 4.1 GEMM 全链路精读（普通 kernel）

#### 4.1.1 概念说明

这是整本手册里最重要的「样板代码」。`example_gemm.py` 用不到 30 行就写出了一个能跑、能对拍、能量延迟的 GEMM。它把前面学过的所有原语浓缩在一段 kernel 里，因此是**串联知识**的最佳锚点。

它体现的是 TileLang 的 **Level 2 抽象**：用户知道 GPU 有 shared memory、分块、线程块这些概念，但不必手写线程级指令——`T.copy` 自动选搬运指令，`T.gemm` 自动分派张量核指令，`T.Pipelined` 自动生成软件流水线。

#### 4.1.2 核心流程

整个 kernel 的执行流程可以用一段「搬进来—算—搬出去」的伪代码描述：

```
对每个输出 tile C[i, j]（每个线程块负责一个）：
    分配 shared：A_shared(block_M×block_K)、B_shared(block_K×block_N)
    分配 fragment 累加器：C_local(block_M×block_N)，并清零
    沿 K 维循环（带 num_stages 软件流水线）：
        把 A 的一小块从 global 搬到 shared
        把 B 的一小块从 global 搬到 shared
        用张量核算 C_local += A_shared @ B_shared
    把 C_local 从 fragment 搬回 global 的对应位置
```

对应到数学上，分块矩阵乘就是：

\[
C_{ij} \;=\; \sum_{k} A_{ik}\,B_{kj}
\]

只是这里的求和被切成 `ceildiv(K, block_K)` 段，每段一次 `T.gemm` 累加进 `C_local`。

#### 4.1.3 源码精读

先看 kernel 的整体签名与启动上下文：

[examples/gemm/example_gemm.py:5-26](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py#L5-L26) —— 这是整个 kernel。注意三点：

1. `@tilelang.jit` + `M, N, K = T.const("M, N, K")`：`T.const` 表示这几个维是 eager JIT 模式下运行时再回填的符号维，编译时 `.compile(M=1024, ...)` 会把具体数值烘焙进去（见 u2-l1）。
2. `with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=128) as (bx, by)`：grid 形状是 `(ceildiv(N,block_N), ceildiv(M,block_M))`，即 **bx 索引 N 方向的块、by 索引 M 方向的块**（注意顺序）。每个块负责输出 C 的一块 `[by*block_M : (by+1)*block_M, bx*block_N : (bx+1)*block_N]`。
3. `threads=128` 是每块的线程数。

kernel 主体就是「搬进来—算—搬出去」：

[examples/gemm/example_gemm.py:13-24](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py#L13-L24) —— 逐行对照前面学的概念：

- `T.alloc_shared(...)`：shared memory，块内所有线程可见，对应搬运与张量核的「中转站」。
- `T.alloc_fragment((block_M, block_N), accum_dtype)`：寄存器 tile，**逻辑形状是 block_M×block_N，但物理上由 layout 推断自动分给各线程**，是张量核的累加器（见 u2-l4、u4-l3）。
- `T.clear(C_local)`：累加前必须清零，否则 `T.gemm` 是累加（`+=`）会叠进垃圾值。
- `for k in T.Pipelined(T.ceildiv(K, block_K), num_stages=3)`：沿 K 循环，3 级软件流水线（见 u4-l4）。`num_stages=3` 表示搬运与计算之间保留 3 份 shared 缓冲。
- `T.copy(A[by * block_M, k * block_K], A_shared)`：把 A 的子块从 global 搬到 shared。源是 `A` 的一个 region（起点坐标），目标是整块 shared。具体走 cp.async / TMA / 普通循环由 C++ 侧 `Copy::Lower` 按 target 选。
- `T.gemm(A_shared, B_shared, C_local)`：`C_local += A_shared @ B_shared`，编译器按 target 分派到 mma/wgmma/mfma（见 u4-l2、u6-l1）。
- 末尾 `T.copy(C_local, C[...])`：把累加结果从 fragment 搬回 global 的对应 tile。

驱动 kernel 的部分（编译、对拍、取源码、测延迟）在 `main()` 里：

[examples/gemm/example_gemm.py:29-57](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py#L29-L57) —— 关键调用：

- `matmul.compile(M=1024, N=1024, K=1024, block_M=128, block_N=128, block_K=32)`：用具体维数实例化，返回 `JITKernel`。
- `kernel(a, b)`：直接传 torch 张量即可调用，返回结果张量。
- `kernel.get_kernel_source()`：打印编译器生成的设备源码（CUDA/HIP/MACA），这是看「编译器到底生成了什么」的窗口（见 u3-l2）。
- `kernel.get_profiler().do_bench(backend="cupti")`：用带 L2 冲刷的精确计时测延迟，单位 ms。

#### 4.1.4 代码实践

**实践目标**：亲手把这段 kernel 跑起来，看到「All check passed」与生成的 CUDA 源码。

**操作步骤**：

1. 确认已按 u1-l2 安装好 tilelang，且有一张 NVIDIA GPU（无 GPU 时可在 `import` 后只调用 `get_kernel_source()` 看源码）。
2. 运行 `python examples/gemm/example_gemm.py`。

**需要观察的现象**：

- 控制台先打印 `c` 与 `ref_c` 两个矩阵，再打印 `All check passed.`。
- 接着打印一大段 `CUDA Source:`，里面能看到 `tl::gemm_ss` 之类的模板调用和 `cp.async`/`mma` 之类指令。
- 最后打印 `tilelang Latency: <X>ms`。

**预期结果**：对 1024×1024×1024、fp16、block 128×128×32 的 GEMM，延迟在零点几毫秒量级（具体取决于 GPU，**待本地验证**）。

> 如果你没有 GPU，把 `main()` 里 `kernel(a, b)` 之后的对拍与 bench 注释掉，只保留 `print(kernel.get_kernel_source())`，仍能完成「阅读型实践」。

#### 4.1.5 小练习与答案

**练习 1**：把 grid 写成 `(bx, by)` 时，为什么 `A` 的下标用 `by * block_M` 而 `B` 的下标用 `bx * block_N`？

**答案**：因为 `bx` 索引的是 N 方向的块、`by` 索引的是 M 方向的块。`A` 形状 `(M, K)`，其行维是 M，所以要乘 `by`；`B` 形状 `(K, N)`，其列维是 N，所以要乘 `bx`。

**练习 2**：如果把 `T.clear(C_local)` 这一行删掉，会发生什么？

**答案**：`T.gemm` 是累加（`+=`），`C_local` 未初始化会叠入未定义值，结果错误。累加器在进入循环前必须清零。

---

### 4.2 Persistent kernel（持久化线程块）

#### 4.2.1 概念说明

**普通 kernel** 的启动方式是「每个输出 tile 派一个线程块」：当 tile 数量远大于 GPU 的 SM 数时，GPU 会一波一波（wave by wave）地调度这些块，每波之间有 block 退出/启动的开销，且不同波之间难复用 L2 cache。

**Persistent kernel（持久化 kernel）** 的思路反过来：**只启动恰好等于 SM 数量的线程块**，让每个块「常驻」在 SM 上，用一个循环去抢占、处理多个输出 tile，直到所有 tile 算完。好处有二：

- 省掉大量 block launch / teardown 的开销。
- 可以**人为控制 tile 的处理顺序**，让相邻 tile 复用 L2 cache（这就是 persistent 版里 `group_size` 与 `T.use_swizzle` 的目的）。

这是高性能 GEMM 库（如 CUTLASS）的常见技巧。TileLang 用 `T.Persistent` 原语把它做成了一行代码。

#### 4.2.2 核心流程

persistent kernel 的结构是：

```
sm_num = 查询设备 SM 数量
waves  = ceildiv(tile总数, sm_num)   # 需要跑几「波」
with T.Kernel(sm_num, threads=...) as (block_id,):   # 只启动 sm_num 个块
    for 每个分给本块的 tile (bx, by):               # T.Persistent 自动遍历
        清零累加器
        沿 K 流水累加
        写回结果
```

关键点：grid 维度从「tile 数」缩小成了「SM 数」，tile 的遍历改由 kernel 内部的循环负责。tile 到 block 的映射（哪个 tile 归哪个 block）由 `T.Persistent` 内部用一个**带 L2 友好的 swizzle 顺序**的 tile 迭代器完成。

下文手写版（`use_persistent_primitive=False` 分支）把这个映射摊开写出来，是最直观的参考。

#### 4.2.3 源码精读

先看 persistent kernel 的「准备工作」——查 SM 数、算 waves、定 grid：

[examples/gemm/example_gemm_persistent.py:45-51](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm_persistent.py#L45-L51) —— 三行决定了一切：

- `sm_num = driver.get_num_sms()`：编译期查询当前设备的 SM 数量。
- `waves = T.ceildiv(m_blocks * n_blocks, sm_num)`：tile 总数除以 SM 数，即每个块至少要处理的波数。
- `with T.Kernel(sm_num, threads=threads) as (block_id):`：grid 只剩一个维度 = SM 数，`block_id ∈ [0, sm_num)` 标识这是第几个常驻块。

`get_num_sms` 的实现只是读设备属性：

[tilelang/carver/arch/driver/cuda_driver.py:111-127](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/carver/arch/driver/cuda_driver.py#L111-L127) —— 返回 `prop.multi_processor_count`。（metax 分支在 `maca_driver.py` 里有一个对称实现，通过 `libmcruntime.so` 查询。）

接下来是 `T.Persistent` 的用法——这是最核心的一行：

[examples/gemm/example_gemm_persistent.py:57-66](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm_persistent.py#L57-L66) —— `for bx, by in T.Persistent([m_blocks, n_blocks], sm_num, block_id)` 把「遍历所有 tile、并把 tile 分配给常驻块」这件事压缩进一个 for。参数含义见其定义：

[tilelang/language/loop.py:90-109](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/loop.py#L90-L109) —— `domain` 是各维的 tile 数列表、`wave_size` 是波大小（这里传 `sm_num`）、`index` 是当前块编号（`block_id`）、`group_size` 控制 L2 swizzle 的分组粒度（默认 8）。它最终调用 `_ffi_api.Persistent` 落到 C++ 侧生成 tile 迭代循环。

如果想看清「tile_id → (bx, by)」到底怎么映射，看同文件的手写版分支（`use_persistent_primitive=False`）：

[examples/gemm/example_gemm_persistent.py:67-81](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm_persistent.py#L67-L81) —— 这是 `T.Persistent` 的「人话翻译」：

```
tile_id = sm_num * w + block_id          # 第 w 波里，本块负责的线性 tile 编号
bx = (tile_id // group_size) % m_blocks
by = (tile_id % group_size) + (tile_id // group_size) // m_blocks * group_size
```

这里 `group_size=8` 的作用是**把 tile 处理顺序做 2D swizzle**：先在 N 方向成组地走 8 个 tile，再推进 M 方向。这样相邻 block 处理的 tile 在 B 矩阵的列上重叠，能复用 L2 里缓存的 B 块。这跟普通版里 `T.use_swizzle(10)` 是同一思想的两副面孔（一个作用于 block 调度顺序，一个作用于 persistent 的 tile 遍历顺序）。

那么编译器如何识别这是一个 persistent kernel 并做特殊处理？看 C++ 侧：

[src/cuda/transform/persist_threadblock.cc:54-59](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/transform/persist_threadblock.cc#L54-L59) —— `tl.PersistThreadblock` pass 遍历函数体；当检测到 grid 级同步原语 `sync_grid()` 时，给函数打上 `use_cooperative_groups` 属性（见 [src/cuda/transform/persist_threadblock.cc:24-50](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/transform/persist_threadblock.cc#L24-L50)），以便生成的 kernel 以 CUDA cooperative launch 启动，保证所有 SM 上的块能做 grid 级同步。

> 注意：本讲的 persistent GEMM **每个块写互不重叠的输出 tile**，块之间不需要归约，因此没有 `sync_grid()`、也不需要 cooperative launch。`sync_grid()` 主要用于 persistent + split-K 这类需要跨块规约的场景。

再看普通版的对照——注意它用了 `T.use_swizzle(10)`：

[examples/gemm/example_gemm_persistent.py:21](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm_persistent.py#L21) —— `T.use_swizzle(10)` 是给**普通 kernel**（一 tile 一 block）的 block 调度做 L2 友好栅格化。其定义：

[tilelang/language/annotations.py:21-26](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/annotations.py#L21-L26) —— 它把 `(device_func, panel_size)` 写成一个 kernel 属性 `threadblock_swizzle_pattern`。codegen 时读这个属性并生成栅格化函数调用：

[src/cuda/codegen/codegen_cuda.cc:4732-4747](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/codegen/codegen_cuda.cc#L4732-L4747) —— codegen 解析出 `rasterization2DRow`（或 `Column`）与 `panel_size=10`，据此重排 blockIdx，让相邻 block 处理的 tile 在共享的输入上对齐，提升 L2 命中率。详细原理将在 u8-l2 展开。

最后看性能对比的驱动代码：

[examples/gemm/example_gemm_persistent.py:90-119](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm_persistent.py#L90-L119) —— 它分别编译、对拍、测延迟，并打印 `Persistent GEMM Speedup`。TFLOPS 用 `total_flops / latency_ms * 1e-9` 计算（`total_flops = 2*M*N*K`）。

#### 4.2.4 代码实践

**实践目标**：在同一台机器、同样形状（默认 4096³）下，量出 persistent 与 non-persistent 的延迟差。

**操作步骤**：

1. 运行 `python examples/gemm/example_gemm_persistent.py`（默认 M=N=K=4096；可用 `--M --N --K` 调整）。
2. 记录两段 `Latency` 与 `TFlops`，以及末尾的 `Speedup`。

**需要观察的现象**：

- 两段都先打印 `All check passed.`（数值正确）。
- persistent 的延迟通常**不高于** non-persistent，speedup ≥ 1。

**预期结果**：在大矩阵（如 4096³、8192³）上 persistent 通常更快；矩阵很小（如 512³）时 tile 数本身不多，persistent 优势不明显甚至持平。**具体数值待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：persistent kernel 的 grid 维度是 `sm_num`，那当 tile 数 `< sm_num` 时会发生什么？

**答案**：会有部分常驻块（`block_id ≥ tile数`）在循环里分不到任何 tile，直接空转退出。功能上没问题，只是没占满 SM。所以 persistent kernel 在「tile 数 ≫ SM 数」的大矩阵上收益最大。

**练习 2**：手写版里的 `group_size=8` 起什么作用？设成 1 会怎样？

**答案**：它把 tile 的处理顺序做 2D swizzle 以提升 L2 复用。设成 1 退化为最朴素的行优先遍历顺序，失去 L2 友好性，性能可能下降。

---

### 4.3 调参维度与性能

#### 4.3.1 概念说明

TileLang 让你把 GEMM 写成「规格」，但**性能仍然取决于你给的 tile 参数**。同一个 kernel、同一组数据，换不同的 block_M/block_N/block_K、num_stages、threads，延迟可能差好几倍。这一节讲清楚每个参数动了什么、该怎么系统地量。

这是后续 u8-l1（autotuner）的直觉基础——自动调优本质上就是在下面这些维度上自动搜索。

#### 4.3.2 核心流程

GEMM 的主要可调参数及其影响：

| 参数 | 作用 | 调大的影响 | 调小的影响 |
| --- | --- | --- | --- |
| `block_M`/`block_N` | 输出 tile 的行/列大小 | 单块算更多、分摊 launch；但占 shared/寄存器更多，并发块数下降 | 并发块数多，但单块算得少、launch 占比上升 |
| `block_K` | K 方向每段的大小 | 单次 `T.gemm` 算更多、循环次数少；但 shared 占用大 | 循环次数多，流水线更频繁 |
| `num_stages` | 软件流水线深度（见 u4-l4） | 搬运/计算重叠更充分；但 shared 占用 ×num_stages | 重叠少、延迟暴露多；shared 省内存 |
| `threads` | 每块线程数 | 影响搬运并行度与张量核 warp 数 | — |

关键约束是**显存占用**。shared memory 总量近似：

\[
\text{shared} \;\approx\; (\text{block\_M}\cdot\text{block\_K} + \text{block\_K}\cdot\text{block\_N})\cdot\text{dtype\_bytes}\cdot\text{num\_stages}
\]

当 shared 占用超过每块上限，或寄存器占用过高导致每 SM 只能驻留 1 个块时，性能会断崖式下跌。所以调参不是「越大越好」，而是要在「算力」与「占用」之间找平衡。

#### 4.3.3 源码精读

调参时两个常被改的点是 `.compile(...)` 的入参与 kernel 内的 `num_stages`：

[examples/gemm/example_gemm_persistent.py:99-101](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm_persistent.py#L99-L101) —— 这里 `block_M=64, block_N=64, block_K=32, threads=256, num_stages=3` 是一组「小 tile + 高并发」配置；而回归测试用的是另一组大 tile：

[examples/gemm/example_gemm_persistent.py:122-130](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm_persistent.py#L122-L130) —— `block_M=128, block_N=256, block_K=64`，是「大 tile + 低并发」配置。两套配置对应不同矩阵尺寸下的较优点，说明**没有万能配置**。

测延迟统一走 profiler：

[examples/gemm/example_gemm.py:54-55](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py#L54-L55) —— `do_bench(backend="cupti")` 用 CUPTI 做高精度计时，内部带 warmup 与 L2 冲刷（详见 u8-l3）。算 TFLOPS 的口径见 [examples/gemm/example_gemm_persistent.py:107](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm_persistent.py#L107)（`total_flops / latency_ms * 1e-9`）。

官方文档也明确强调了这一点：

[docs/deeplearning_operators/matmul.md:226-234](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/deeplearning_operators/matmul.md#L226-L234) —— 「这些测量会随 tile 尺寸、流水线级数、硬件能力而变化」。

#### 4.3.4 代码实践（本讲主任务）

**实践目标**：固定 M=N=K=4096，扫一组参数，填出一张调参记录表，找出较优配置。

**操作步骤**：

1. 以 `examples/gemm/example_gemm.py` 为模板，把 `matmul.compile(...)` 的 `block_M/block_N/block_K` 与 kernel 内 `num_stages` 改成变量。
2. 固定 M=N=K=4096、dtype=fp16，遍历下表中的组合，每组跑一次 `profiler.do_bench()` 取延迟（建议每组多跑取最小值）。
3. 填表（示例骨架，数值待本地验证）：

| block_M | block_N | block_K | num_stages | 延迟(ms) | TFLOPS | 备注 |
| --- | --- | --- | --- | --- | --- | --- |
| 128 | 128 | 32 | 3 | ? | ? | 基线 |
| 128 | 128 | 64 | 3 | ? | ? | 加大 block_K |
| 128 | 256 | 64 | 3 | ? | ? | 加大 block_N |
| 256 | 128 | 64 | 3 | ? | ? | 加大 block_M |
| 128 | 256 | 64 | 4 | ? | ? | 加深流水线 |
| 128 | 256 | 64 | 2 | ? | ? | 减浅流水线 |

**需要观察的现象**：

- `num_stages` 调大通常先变快、再因 shared 超限变慢（甚至编译失败/occupancy 下降）。
- block_M×block_N 加大通常提升算力利用率，但到某点后 occupancy 下降导致变慢。

**预期结果**：得到一张单调-ish 的趋势表，能从中挑出延迟最低的一组。**具体最优配置依赖具体 GPU，待本地验证。**

> 进阶：把这套手动扫描交给 u8-l1 的 autotuner，让它自动在更大空间里搜。

#### 4.3.5 小练习与答案

**练习 1**：block_M=128, block_N=128, block_K=64, num_stages=3, fp16，估算 single buffer 的 shared 占用（不算 num_stages 翻倍）。

**答案**：`A_shared` = 128×64×2 = 16384 字节，`B_shared` = 64×128×2 = 16384 字节，共约 32 KiB（单份）。乘 num_stages=3 约 96 KiB（实际经存储复用 pass 会少一些）。

**练习 2**：为什么 `num_stages` 不是越大越好？

**答案**：shared 占用随 num_stages 线性增长，超过每块上限会编译失败或迫使每 SM 驻留块数下降（occupancy 降低），反而拖慢。

---

### 4.4 边界处理与正确性

#### 4.4.1 概念说明

前面的例子都假设 M、N、K 是 block 的整数倍。真实场景里矩阵尺寸是任意的，会出现「边缘 tile」：最后一次 `T.copy` 只搬得到部分数据、`T.gemm` 算出的累加器只有一部分对应有效输出。处理不好就会**读到越界内存**或**写出垃圾**。

TileLang 在这里有两套机制：**编译器自动加守卫**（对 global 访问），以及**用户手写守卫**（persistent 例子的手写分支就演示了后者）。理解这两者能帮你解释「为什么 example_gemm.py 里明明没有 `if` 边界判断却也能对拍通过」。

#### 4.4.2 核心流程

边界出现在两个方向：

- **K 方向**：循环次数 `ceildiv(K, block_K)`。最后一次迭代若 `K % block_K != 0`，`T.copy` 从 global 读到的子块不完整。
- **M / N 方向**：grid 次数 `ceildiv(M, block_M)`、`ceildiv(N, block_N)`。边缘块的累加器是完整的 block_M×block_N，但只有左上角子块对应有效输出，写回 global 时不能越界。

处理方式：

```
global 访问越界  →  LegalizeSafeMemoryAccess pass 自动加 if 守卫（仅 global）
shared/local 越界 →  仅告警（这些是块内私有，越界语义可接受）
用户想要更精细控制 →  自己写 if（如 persistent 手写分支）
```

#### 4.4.3 源码精读

`example_gemm.py` 里**没有任何显式边界判断**，但仍能对拍通过，原因是这个 pass：

[src/transform/legalize_safe_memory_access.cc:665-684](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/legalize_safe_memory_access.cc#L665-L684) —— `tl.LegalizeSafeMemoryAccess` pass：对 **global memory** 的越界访问自动插入条件守卫（防止读/写超出张量边界），对 shared/local 仅告警。这正是「不写 if 也能安全」的来源（这个 pass 在 u2-l3 已介绍，这里看它的注册点）。

而 persistent 例子的手写分支展示了「用户主动写守卫」的写法：

[examples/gemm/example_gemm_persistent.py:73-81](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm_persistent.py#L73-L81) —— `if bx * block_M < M and by * block_N < N:` 在手写 tile 映射里显式跳过完全落在矩阵外的 tile。注意：手写分支因为是自己算的 `bx/by`，可能算出落在 `[M,N)` 之外的 tile，所以需要这个守卫；而 `T.Persistent` 原语与普通 `T.Kernel` 的 grid 本身由编译器结合守卫处理，用户通常不必手写。

对拍正确性的口径在两处一致：

[examples/gemm/example_gemm.py:46](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py#L46) 与 [examples/gemm/example_gemm_persistent.py:103](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm_persistent.py#L103) —— 都用 `rtol=0.01, atol=0.01`（fp16 GEMM 的典型容差），persistent 版还多了 `tensor_supply_type=Randn` 控制输入分布（见 u8-l3）。

#### 4.4.4 代码实践

**实践目标**：验证「非整数倍尺寸」下 kernel 仍然正确。

**操作步骤**：

1. 复制 `examples/gemm/example_gemm.py`，把 `main()` 里的形状从 1024³ 改成「故意不整除」的组合，例如 `M=1000, N=1024, K=999`（block 仍用 128/128/32）。
2. 同步改 torch 输入张量形状与 `ref_c = a @ b`。
3. 重新运行。

**需要观察的现象**：

- 仍打印 `All check passed.`，说明边缘 tile 被正确处理。
- 生成的 CUDA 源码里，对应 global 访问处会出现条件分支（守卫）。

**预期结果**：对拍通过。若不通过，多半是 `T.copy` 的源 region 起点写错（例如把 `by*block_M` 写成了 `bx*block_M`）。**待本地验证。**

#### 4.4.5 小练习与答案

**练习 1**：为什么 `example_gemm.py` 没写 `if` 边界判断也能对拍通过？

**答案**：`tl.LegalizeSafeMemoryAccess` pass 自动给 global memory 的越界访问加了守卫。对 global 是强制的，对 shared/local 仅告警。

**练习 2**：persistent 手写分支里的 `if bx * block_M < M and by * block_N < N:` 能否去掉？

**答案**：不能简单去掉。手写分支是自己用整数除法算 `bx/by`，可能算出落在 `[M,N)` 之外的 tile；去掉守卫会读到/写出越界。`T.Persistent` 原语内部已处理这类边界，故用原语版时不必手写。

---

## 5. 综合实践

把本讲四个模块串成一个任务：**实现一个可调参的 GEMM 调优脚本**。

要求：

1. 以 `example_gemm.py` 的 `matmul` 为基础，把 `block_M/block_N/block_K/num_stages` 提为参数，封装成函数 `bench(block_M, block_N, block_K, num_stages) -> (latency_ms, tflops)`。
2. 固定 M=N=K=4096，扫描至少 6 组配置（参考 4.3.4 的表），输出一张 Markdown 表格。
3. 在最优配置基础上，再分别切到 `example_gemm_persistent.py` 的 persistent 版，比较 persistent vs non-persistent 的延迟（参考 4.2）。
4. 选一组「非整数倍」形状（如 1000×1024×999）验证边界正确性（参考 4.4）。

验收标准：

- 表格里有明确的延迟与 TFLOPS（即便只跑出几行也算完成）。
- 能指出哪组配置最优，并给出一句话解释（算力 vs 占用）。
- 非整数倍形状对拍通过。

这个任务把「读懂全链路（4.1）→ 持久化（4.2）→ 调参（4.3）→ 边界（4.4）」全部用到了，做完你就具备了一个真实 GEMM 调优的最小工作流。下一步可把扫描换成 u8-l1 的 autotuner 自动化。

## 6. 本讲小结

- `example_gemm.py` 是「搬进来—算—搬出去」的最小完整范式：`alloc_shared`/`alloc_fragment` 分配中转与累加器，`T.Pipelined` 软件流水，`T.copy` 搬运，`T.gemm` 自动分派张量核，最后搬回 global。
- 普通 kernel 是「一 tile 一 block」；persistent kernel 只启动 `sm_num` 个常驻块，用 `T.Persistent` 遍历所有 tile，省 launch 开销且可做 L2 友好的 tile 顺序。
- `T.use_swizzle`（普通版）与 `group_size`（persistent 手写版）是 L2 cache 友好栅格化的两副面孔，本质都是重排 block/tile 处理顺序以复用缓存。
- 性能取决于 tile 参数：block_M/N/K 与 num_stages 在「算力利用率」与「shared/寄存器占用」之间取舍，没有万能配置。
- 边界处理有两套机制：global 访问由 `tl.LegalizeSafeMemoryAccess` pass 自动加守卫；用户也可像 persistent 手写分支那样主动写 `if`。
- `JITKernel` 的 `get_kernel_source` / `get_profiler().do_bench` 是「看生成代码」与「量延迟」的两个核心入口。

## 7. 下一步学习建议

- **u8-l1（autotuner）**：把本讲 4.3 的手动扫描交给自动调优器，在更大参数空间里搜索，理解 capture 与分组编译。
- **u8-l2（swizzle/persistent/splitk）**：深入 L2 swizzle 的原理与 split-K 并行策略，本讲的 `T.use_swizzle` 与 `group_size` 在那里会有完整推导。
- **u8-l3（profiling）**：系统学习 `do_bench` 的 warmup、L2 冲刷、backend 选项与 tensor 供给，把本讲的「量延迟」做严谨。
- **u8-l4（FlashAttention/elementwise）**：把本讲的 GEMM 链路迁移到带在线 softmax 与 reduction 的真实算子，检验你是否真正掌握了「搬进来—算—搬出去」范式。
