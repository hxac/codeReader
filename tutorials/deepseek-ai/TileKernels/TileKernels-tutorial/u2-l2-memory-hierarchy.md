# GPU 存储层级与数据搬运

## 1. 本讲目标

本讲聚焦 GPU kernel 内部的「数据放哪里、怎么搬」。读完本讲后，你应当能够：

- 区分 TileLang 的三种存储分配 `T.alloc_fragment` / `T.alloc_shared` / `T.alloc_local`，知道它们分别对应 GPU 的哪一层物理存储、谁能看到、谁要同步。
- 会用 `T.copy` 在 global / shared / fragment 三级之间搬运数据，并理解 `disable_tma=True` 选项关掉了什么、什么时候该关。
- 看懂转置 kernel 里 `out_shared` 形状 `(block_y, block_x + block_k)` 中那个 `+block_k`「额外加一列」的 padding 为什么能显著减少 shared memory 的 bank conflict。

本讲不教你写完整算子（那是 u2-l1 的骨架 + 后续各算子讲义的事），只教你「同一个 tile 在不同存储层级之间怎么走」这条贯穿所有算子的通用主线。

## 2. 前置知识

在进入源码前，先用三段话建立 GPU 存储的心智模型。如果你已经熟悉，可以跳到第 3 节。

**第一层：全局内存（global memory / 显存，HBM）。** 这是最外层、容量最大、但最慢的存储。PyTorch 张量（`torch.empty(..., device='cuda')` 分配出来的）就活在 global memory 里。一个 kernel 的输入和输出几乎都从这里来、回这里去。访问 global memory 的代价是几百个时钟周期量级。

**第二层：共享内存（shared memory，SMEM）。** 它位于芯片上、每个 SM（流式多处理器）内部，由同一个线程块（thread block）内的所有线程共享。容量小（SM90/SM100 每块上百 KB 量级），但访问速度接近寄存器。线程块结束就被回收。它是「线程之间交换数据」的中转站——比如转置时，一个 tile 先写进 shared memory，块内所有线程同步后，再用另一种顺序读出来。

**第三层：寄存器（register）。** 每个线程私有，最快、容量最小。真正在「计算」的数据必须落在这里（ALU 只能直接操作寄存器）。

**一个隐藏的关键概念：shared memory 的 bank。** shared memory 物理上被切成 **32 个 bank**，每个 bank 宽 4 字节，每个周期每个 bank 只能服务一次访问。一个 warp 正好 32 个线程，理想情况是 32 个线程各访问一个不同的 bank（一次完成）；若多个线程访问同一个 bank，就会「排队」——这叫 **bank conflict**，访问被串行化，性能下降。矩阵转置之所以难写，正是因为「写时按行、读时按列」很容易让一整列数据撞进同一个 bank。

> 名词速查：SM（流式多处理器，GPU 的计算单元）、warp（32 个线程组成的调度单位）、bank conflict（多线程同周期访问同 bank 导致串行）、TMA（Tensor Memory Accelerator，Hopper/Blackwell 上专做异步批量搬运的硬件单元，见 4.2）。

本讲承接 u2-l1 建立的 `@tilelang.jit` + `@T.prim_func` + `T.Kernel` 骨架；如果你对「编译期参数 vs 运行时符号」还不熟，建议先回顾 u2-l1。

## 3. 本讲源码地图

本讲精读两个文件，它们恰好代表两种典型的数据搬运风格：

| 文件 | 角色 | 主要存储分配 | 是否用 `T.copy` |
| --- | --- | --- | --- |
| [tile_kernels/transpose/batched_transpose_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py) | 批量矩阵转置 | `alloc_local`（寄存器）+ `alloc_shared`（带 padding） | 否，全程直接索引读写 |
| [tile_kernels/quant/per_token_cast_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py) | 逐 token 量化（FP8/FP4） | `alloc_fragment`（寄存器）+ `alloc_shared` | 是，两处 `T.copy(..., disable_tma=True)` |

对照着看这两个文件，你会发现同一件事（在存储层级间搬运一个 tile）有两种写法：**细粒度手控**（转置，用 `alloc_local` + 手写 swizzle）和**粗粒度批量**（量化，用 `alloc_fragment` + `T.copy`）。理解这种取舍是本讲的核心收获。

## 4. 核心概念与源码讲解

### 4.1 三种存储分配：fragment / shared / local

#### 4.1.1 概念说明

TileLang 提供三个分配原语，对应三种「数据可见性 + 物理位置」组合：

| 原语 | 物理位置 | 可见范围 | 典型用途 |
| --- | --- | --- | --- |
| `T.alloc_fragment(shape, dtype)` | 寄存器 | 整个 block 协作持有（每个线程持一部分，布局受控） | 承载一个 tile 参与集体加载/规约 |
| `T.alloc_local(shape, dtype)` | 寄存器（线程私有） | 每个线程**独占一份完整副本**，互不可见 | 线程内临时缓冲、细粒度逐元素计算 |
| `T.alloc_shared(shape, dtype)` | 共享内存（SMEM） | block 内**所有线程共享** | 线程间数据交换、转置中转、输出暂存 |

最容易混的是 fragment 和 local：两者物理上都在寄存器，区别在「语义」。fragment 是**协作布局**的——一个 `(block_m, block_k)` 的 fragment，其元素被按某种线程映射分散到各线程的寄存器里，因此可以整块参与 `T.copy`（集体加载）、`T.reduce_*`（跨线程规约）、`T.annotate_layout`（指定 swizzle）。而 local 是**线程私有**的——每个线程都拥有该 shape 的一份独立副本，线程之间看不到彼此的 local，适合你想自己控制「哪个线程算哪些元素」的场景。

shared 则是跨线程的公共黑板：写进去后必须 `T.sync_threads()` 同步，别的线程才读得到（写时存在 bank conflict 风险，见 4.3）。

#### 4.1.2 核心流程

一个 tile 在 kernel 内的典型生命周期：

```
global(HBM)
   │  加载(load)
   ▼
fragment 或 local(寄存器)  ← 计算、规约都在这里做
   │  写入(store)
   ▼
shared(SMEM，块内共享，可能带 padding/swizzle)
   │  T.sync_threads() 屏障
   ▼
fragment 或 local(寄存器)  ← 换一种顺序读出来
   │  写回(store)
   ▼
global(HBM)
```

转置就是这条流水线的教科书案例：按行读进寄存器 → 写进 shared（带 swizzle）→ 同步 → 按列从 shared 读出 → 写回 global。下面分别看两个文件怎么落地。

#### 4.1.3 源码精读

**转置 kernel 用 `alloc_local` 做线程私有寄存器缓冲**（4×4 寄存器小块）：

[batched_transpose_kernel.py:54-55](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L54-L55) 分配两个 local 缓冲：`tmp` 是 4×4 的寄存器小块，`tmp_row` 是一行临时缓冲。注意它们是 `alloc_local`——每个线程各自持有一份，互不可见，作者要的就是这种「线程私有」的细粒度控制。

随后两段循环在 local 缓冲内部完成「读一行、摆成 4×4」的寄存器级转置：[batched_transpose_kernel.py:59-63](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L59-L63)。因为数据始终在线程私有寄存器里，不需要任何同步。

而 `out_shared` 是块内共享的中转区：[batched_transpose_kernel.py:45](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L45)，它的 padding 细节留到 4.3 讲。写完之后必须 `T.sync_threads()` 让块内线程互见：[batched_transpose_kernel.py:71](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L71)。

**量化 kernel 用 `alloc_fragment` 做协作 tile**，风格完全不同：

[per_token_cast_kernel.py:73-75](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L73-L75) 一次分配输入 fragment、SF（scaling factor）倒数 fragment、输出 shared。注意这里 `x_fragment` 是 fragment 而非 local——因为后面要整块 `T.copy` 加载、还要做 `T.reduce_absmax` 跨线程规约（[per_token_cast_kernel.py:99](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L99)），这些都依赖 fragment 的协作语义。

> 对照记：转置用 `local`（要细粒度手控 4×4 布局 + swizzle），量化用 `fragment`（要集体加载 + 跨线程规约）。选哪个，取决于你接下来要对这份数据做什么集体操作。

#### 4.1.4 代码实践

**实践目标：** 在源码层面识别三种分配，并推断每处「谁能看到这份数据」。

**操作步骤：**

1. 打开 `tile_kernels/quant/per_token_cast_kernel.py`，搜索 `alloc_`，统计 `fragment` / `shared` 各出现几次（`local` 此文件里应为 0）。
2. 对每处 `alloc_fragment`，看它后面紧跟的是 `T.copy`（集体加载）还是 `T.reduce_*`（跨线程规约）——这些都是「必须 fragment」的信号。
3. 打开 `tile_kernels/transpose/batched_transpose_kernel.py`，确认 `tmp`/`tmp_row` 是 `alloc_local`，并问自己：为什么这里不用 fragment？因为转置的 4×4 块布局是作者按线程手算的（见 4.1.3 的 swizzle），不需要 fragment 提供的自动协作布局。

**需要观察的现象：** 你会发现「需要跨线程集体操作 → fragment」「只在线程内私有计算 → local」「需要跨线程交换 → shared」这条规则在两个文件里都成立。

**预期结果：** 量化文件里输入/中间量几乎都是 fragment，只有输出暂存 `out_shared` 是 shared；转置文件里中间量是 local，中转是 shared。两个文件都没有「该用 fragment 却用了 local」的反例。本步骤为源码阅读型实践，**待本地验证**的是你自己列出的统计表是否与上述一致。

#### 4.1.5 小练习与答案

**Q1：** 如果把量化 kernel 里的 `x_fragment` 从 `alloc_fragment` 改成 `alloc_local`，会发生什么？

**答：** `T.copy(x[...], x_fragment, disable_tma=True)` 和 `T.reduce_absmax(x_stage1_fragment_reshaped, ...)` 都依赖 fragment 的协作布局（整块被线程集体持有、跨线程归约）。改成 local 后每个线程持有一份独立副本、彼此不可见，集体 `T.copy` 与跨线程规约会失去语义，编译期或运行期会出错。结论：需要集体操作就必须 fragment。

**Q2：** 转置 kernel 写完 `out_shared` 后为什么必须有 `T.sync_threads()`，而写 `tmp`（local）时不需要任何同步？

**答：** `out_shared` 在 shared memory，是 block 内所有线程共享的；一个线程写入后，别的线程不一定已经执行到该写，必须用 `T.sync_threads()` 屏障等所有线程都写完。`tmp` 是 local（线程私有），一个线程写的只有它自己读，不存在「等别人」的问题，所以无需同步。

**Q3：** fragment 和 local 物理上都在寄存器，那它们本质区别是什么？

**答：** 语义不同。fragment 是「协作布局」——一个 shape 的元素按某种映射分散到各线程寄存器，可整块参与 `T.copy`/规约/`annotate_layout`；local 是「线程私有」——每个线程独立持有完整 shape 的一份副本，互不可见，适合自己手控线程-元素映射。

### 4.2 T.copy 跨级搬运与 disable_tma

#### 4.2.1 概念说明

`T.copy(src, dst)` 是 TileLang 的「跨存储层级批量搬运」原语。它的两端可以是 global / shared / fragment 的合法组合，编译器会自动选择合适的底层指令（`cp.async`、向量化的 `LDG`/`STG`、或 TMA）。相比你手写循环逐元素 `for ...: a[i] = b[i]`，`T.copy` 让编译器有机会选最优指令、做向量化、插入异步拷贝。

`disable_tma` 这个开关关的是 **TMA（Tensor Memory Accelerator）**——Hopper（SM90）/Blackwell（SM100）上专门做「global ↔ shared 之间多维、对齐、异步批量搬运」的硬件单元。TMA 很强，但它有前提：拷贝形状要规则、地址要对齐、通常目标是 shared memory。当你的搬运不符合这些前提（比如目标是 fragment 而非 shared、或形状太小太碎）时，`disable_tma=True` 让编译器退回到「标量/向量化 load-store」路径，反而更合适。

#### 4.2.2 核心流程

`T.copy` 的方向由 src / dst 的存储层级决定。本讲两个文件用到的两种方向：

```
方向 A（加载输入）:  global  ──T.copy(disable_tma=True)──▶  fragment
方向 B（写回输出）:  shared  ──T.copy(disable_tma=True)──▶  global
```

注意「fragment → shared」这一步在量化 kernel 里**没有**用 `T.copy`，而是直接逐元素写（`out_shared[i,j] = ...`）。为什么？因为这一步要顺带做数值缩放（乘 `sf_inv`），是「计算」，逐元素写更直接；而真正成块的「shared → global」才用 `T.copy` 走批量路径。

#### 4.2.3 源码精读

量化 kernel 只有**两处** `T.copy`，都显式 `disable_tma=True`：

**加载输入（global → fragment）：** [per_token_cast_kernel.py:85](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L85)

```python
T.copy(x[pid_token * block_m, pid_hidden * block_k], x_fragment, disable_tma=True)
```

源端是 global 上的 `T.StridedTensor` 切片（`token_stride` 可能非平凡，见 u2-l1），目标端是 fragment。因为目标是 fragment（寄存器）而非 shared memory，TMA 的典型用武之地（往 shared 里灌）对不上，所以禁用 TMA、走向量化 load。

**写回输出（shared → global）：** [per_token_cast_kernel.py:154](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L154)

```python
T.copy(out_shared, out[pid_token * block_m, pid_hidden * block_k], disable_tma=True)
```

先把算好的低比特（FP8/FP4）值逐元素从 fragment 写进 `out_shared`（[per_token_cast_kernel.py:126](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L126)），再从这里成块 `T.copy` 回 global。shared 在这里充当「输出暂存」：让低比特打包写回走一条连续的批量路径。

**对照：转置 kernel 完全不用 `T.copy`。** 它的全局读写都是直接索引：读用 `tmp_row[k] = x[...]`（[batched_transpose_kernel.py:61](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L61)），写用 `out[...] = out_shared[i, j]`（[batched_transpose_kernel.py:74](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L74)）。因为转置要精确控制「哪个线程读写哪个元素 + 自定义 swizzle」，直接索引比 `T.copy` 更灵活。这正是两种风格的取舍。

> 小贴士：`disable_tma` 不是「更快」或「更慢」的开关，而是「换一条搬运路径」的开关。选 TMA 还是标量/向量路径，取决于搬运形状、目标存储、对齐情况——本讲两个文件都因为「目标是 fragment / 形状需要精细控制」而选择禁用。

#### 4.2.4 代码实践

**实践目标：** 用一个最小例子说清 `T.copy` 在 fragment / shared / global 三级之间的搬运方向。

**操作步骤：**

1. 读 [per_token_cast_kernel.py:85](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L85) 与 [per_token_cast_kernel.py:154](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L154)，在纸上画出这个 kernel 里「输入 global → fragment →（计算）→ shared → global 输出」的完整箭头图，并在每个箭头旁标注：用了 `T.copy` 还是直接索引？是否 `disable_tma`？
2. 再读转置 kernel，画出它的对应箭头图（注意它多了一级 local，且全程直接索引）。
3. 设环境变量 `TK_PRINT_KERNEL_SOURCE=1`（见 u1-l2）跑一次量化或转置，在打印出的 CUDA 源码里找 `T.copy` 编译出的底层指令（`cp.async`、`LDG`/`STG`、`stmatrix` 之类），印证「`T.copy` 会根据方向和开关选择不同指令」。

**需要观察的现象：** 箭头图应清晰显示——量化 kernel 有两个 `T.copy` 边（global→fragment、shared→global），中间 fragment→shared 是直接索引；转置 kernel 没有任何 `T.copy` 边，全是直接索引。

**预期结果：** 你能对任意一段 kernel，指出每个 tile「此刻在哪一级存储、下一步要去哪一级、用什么原语搬」。源码里 `T.copy` 对应的底层指令若因环境无法运行，标注 **待本地验证**。

#### 4.2.5 小练习与答案

**Q1：** 量化 kernel 里「fragment → shared」这一步（写 `out_shared[i,j] = ...`）为什么不用 `T.copy`？

**答：** 这一步同时要做数值缩放（乘 `sf_inv`、按 per-channel 因子），是「边算边写」的计算密集步骤，逐元素写更直接；`T.copy` 适合「纯搬运、无计算」的成块拷贝。真正无计算的「shared → global」才用 `T.copy`。

**Q2：** `disable_tma=True` 是不是意味着「关掉所有异步优化、变得更慢」？

**答：** 不是。它只是让编译器**不走 TMA 这条路径**，改走向量化 `LDG`/`STG` 或 `cp.async`。当目标是 fragment（如本讲加载输入）、或形状太小/不对齐、或需要精细控制时，禁用 TMA 反而更合适。TMA 的强项是「global ↔ shared 的规则多维异步批量拷贝」，用错场景才需要关。

**Q3：** 转置 kernel 全程没有 `T.copy`，它是怎么把数据从 global 搬进来的？

**答：** 直接索引读：`tmp_row[k] = x[pid_batch, ..., ...]`（[batched_transpose_kernel.py:61](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L61)）。因为转置要精确控制「哪个线程读哪个元素 + 4×4 寄存器块布局 + 写 shared 时的 swizzle」，直接索引比 `T.copy` 更灵活。两种风格都能完成搬运，取舍在于「你要不要自己控制线程-元素映射」。

### 4.3 共享内存 padding 与 bank conflict

#### 4.3.1 概念说明

回顾第 2 节：shared memory 有 **32 个 bank**，每 bank 宽 4 字节，每周期每 bank 一次访问。bank 由地址决定：`bank = (字节地址 / 4) mod 32`。一个 warp 的 32 个线程若同周期访问同一 bank，就会串行化（bank conflict）。

矩阵转置是 bank conflict 的「重灾区」：你按**行**把 tile 写进 shared memory，再按**列**读出来。按列读时，同一列的相邻元素在内存里间隔「一行」的距离——如果这个行距离（按 bank 计）恰好是 32 的倍数，整列就全落进同一个 bank，造成最严重的 32 路 conflict。

破局思路：给每行**多加几列 padding**，让行距离不再是 32（bank 数）的倍数，于是同一列的元素被摊到不同 bank 上。

#### 4.3.2 核心流程

用 fp32（4 字节）举例，行宽 `block_x = 128`：

**不 padding（形状 `(128, 128)`）：** 元素 `[i, j]` 的字节地址是 `(i*128 + j)*4`。按列读（固定 `j`，遍历 `i`）时，

\[
\text{bank}_i = \left\lfloor \frac{(i\cdot128 + j)\cdot 4}{4} \right\rfloor \bmod 32 = (i\cdot128 + j) \bmod 32
\]

因为 \(128 = 4\times32\)，\(i\cdot128 \bmod 32 = 0\) 对任意 \(i\) 成立，所以 \(\text{bank}_i = j \bmod 32\)（常数！）。整列 32 个线程撞进同一个 bank → **32 路 bank conflict**。

**加 `block_k=4` 列 padding（形状 `(128, 132)`）：** 字节地址变成 `(i*132 + j)*4`，

\[
\text{bank}_i = (i\cdot132 + j) \bmod 32, \qquad 132 \bmod 32 = 4
\]

于是 \(\text{bank}_i = (j + 4i) \bmod 32\)，相邻行差 4 个 bank，同一列被摊到多个 bank 上，**最坏冲突从 32 路大幅降低**。残留的小冲突（与 dtype 有关）再由代码里的 swizzle 兜底（见 4.3.3）。

代价仅是每行浪费 `block_k=4` 个元素的显存（约 3%），换来冲突骤降，极其划算。

#### 4.3.3 源码精读

**padding 的分配点：** [batched_transpose_kernel.py:45](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L45)

```python
out_shared = T.alloc_shared((block_y, block_x + block_k), dtype)
```

其中 `block_k = 4`（[batched_transpose_kernel.py:33](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L33)）。这就是那个 `+block_k` 的来历：**行宽从 `block_x` 变成 `block_x + 4`，打破「行宽是 32 的倍数」这个致祸对齐**。注意 `block_k` 同时也是 4×4 寄存器小块的边长（[batched_transpose_kernel.py:54](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L54)），一物两用。

**写阶段的 swizzle（兜底残留冲突）：** [batched_transpose_kernel.py:67-69](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L67-L69)

```python
swizzle_j = (j + tid // (8 // dtype.bytes)) % block_k
...
out_shared[col * block_k + swizzle_j, i * block_k + k] = tmp[swizzle_j, k]
```

`tid // (8 // dtype.bytes)` 把线程 id 混进列偏移，让相邻线程的写地址进一步错开到不同 bank。`dtype.bytes` 让 swizzle 步长随 dtype 自适应（fp16 时 `8//2=4`，fp32 时 `8//4=2`）。padding 解决「列读」的主冲突，swizzle 解决「写」与残留的次冲突，两者配合。

**读阶段的 swizzle：** [batched_transpose_kernel.py:36](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L36) 与 [batched_transpose_kernel.py:73-74](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L73-L74) 用 `loop_layout`（由 `create_loop_layout_fn` 构造）给「读 shared → 写 global」的线程映射再做一次错峰，确保读 shared 时也不撞 bank。

> 一句话：**padding 改的是 shared 的物理行宽（静态），swizzle 改的是线程到元素的映射（动态）**，两者目标一致——让 32 个线程的访问尽量落在 32 个不同 bank 上。

#### 4.3.4 代码实践

**实践目标：** 解释 `out_shared` 形状 `(block_y, block_x + block_k)` 的 `+block_k` padding 为何减少 bank conflict，并通过 benchmark 感受它的作用。

**操作步骤：**

1. **纸面推导（fp32，block_x=128）：** 按 4.3.2 的公式，算出未 padding 时一列 32 个线程的 bank 全相同（32 路 conflict），加 padding 后 bank 按 `(j + 4i) mod 32` 摊开。
2. **改参数观察（源码阅读 + 本地可选运行）：** 想象把 [batched_transpose_kernel.py:45](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L45) 改回 `(block_y, block_x)`（去掉 padding）。**不要真改源码**，而是在本地 fork 一份实验副本，或仅做思想实验。
3. **跑 benchmark：** 用 `pytest tests/transpose/test_transpose.py --run-benchmark`（见 u1-l2、u3-l2）对比「有 padding（原版）」与「无 padding（实验副本）」在相同 dtype/shape 下的延迟与 GB/s 带宽。

**需要观察的现象：** 无 padding 版本在「列读」阶段出现严重 bank conflict，延迟应明显高于有 padding 版本，带宽（`count_bytes`/耗时，见 u1-l2）明显更低。

**预期结果：** 有 padding 版本带宽更接近硬件显存带宽极限；无 padding 版本因 bank conflict 串行化而带宽打折。具体的「无 padding 延迟数字」**待本地验证**——本讲不假装已运行，结论方向可由 4.3.2 的数学推导确定。

#### 4.3.5 小练习与答案

**Q1：** 为什么 padding 取 `block_k = 4` 这么小的值就够了？取 1 行不行吗、取 32 行是不是更稳？

**答：** 只要 padding 让「行宽 mod 32 ≠ 0」就能打破最坏对齐。`block_k=4` 让 fp32 行宽变 132（132 mod 32 = 4）、fp16 行宽变 132 元素（按字折算也错开），已能把 32 路 conflict 降到很低，残留部分由 swizzle 兜底。取更小（如某些 dtype 下）可能不足以错开 bank；取 32 等于直接把行宽又变成 32 的倍数，反而**重新引入**对齐冲突，且浪费显存。4 是个兼顾「打破对齐 + 浪费小 + 与 4×4 寄存器块共用」的好选择。

**Q2：** 如果把 dtype 从 fp16 换成 fp32，`out_shared` 的 bank 冲突模式会变吗？

**答：** 会。bank 按 4 字节一字编址：fp32 一个元素占满一个 bank 槽，fp16 两个元素共享一个 4 字节槽（sub-word 访问，还可能产生另一种 broadcast/冲突）。所以同一 padding 下两种 dtype 的冲突路数不同——这正是代码里 swizzle 用 `dtype.bytes` 自适应步长（[batched_transpose_kernel.py:67](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L67)）的原因。

**Q3：** padding 和 swizzle 都在减少 bank conflict，它们的职责怎么分？

**答：** padding 是**静态**手段——改 shared memory 的物理行宽，让「按列读」时相邻行天然落到不同 bank，消灭主冲突。swizzle 是**动态**手段——改「线程 → 元素」的映射，让写阶段和读阶段里相邻线程的访问也错开，消灭残留冲突。两者互补，缺一不可。

## 5. 综合实践

**任务：** 给两个 kernel 各画一张「存储层级时序图」，并把本讲三个主题（三种分配、`T.copy`/`disable_tma`、padding/swizzle）一次性串起来。

**步骤：**

1. 选定一个 kernel（建议先 `per_token_cast_kernel`，结构更规整）。
2. 在图上画出 4 个存储位置：**global**、**fragment**、**local**（若无则标「未使用」）、**shared**。
3. 用带标注的箭头连出每个 tile 的移动轨迹，每个箭头写三件事：
   - 搬运用了什么：`T.copy(disable_tma=True)` / 直接索引读 / 直接索引写（带 swizzle）；
   - 涉及的源码行号（带永久链接）；
   - 是否需要同步（如 `T.sync_threads()`）。
4. 在 `shared` 节点上特别标注：是否 padding？padding 多少？为什么？是否有 swizzle 兜底？
5. **对比两个 kernel**：列出「转置用 local + 直接索引 + 手写 swizzle」vs「量化用 fragment + `T.copy` + 无 padding 的 shared」三处差异，并解释每种选择背后的动机。

**验收：** 你应当能用这张图向别人讲清「数据从进 kernel 到出 kernel，经过了哪些存储、每一步用什么原语、为什么这么选」。这张图也是后续读 MoE/engram/mhc 等更复杂算子时的通用心智模板——它们的存储搬运无非是同一套原语的不同组合。

> 本实践为源码阅读型 + 可选本地运行。若本地有 GPU，可配合 `TK_PRINT_KERNEL_SOURCE=1` 与 `--run-benchmark` 印证箭头图里 `T.copy` 对应的指令与带宽；无法运行时，结论以源码行号与数学推导为准。

## 6. 本讲小结

- TileLang 三种分配对应三种语义：`alloc_fragment`（协作寄存器，支持 `T.copy`/规约/布局标注）、`alloc_local`（线程私有寄存器，适合手控线程-元素映射）、`alloc_shared`（块内共享，需 `T.sync_threads`）。
- `T.copy(src, dst, disable_tma=True)` 是跨级批量搬运原语，方向由两端存储层级决定；`disable_tma` 关掉 TMA 硬件路径、走向量化 load-store，适合目标是 fragment 或形状需精细控制的场景。
- 两种搬运风格：转置全程直接索引 + `local` + 手写 swizzle（要细粒度控制），量化用 `fragment` + `T.copy`（要集体加载与规约）——取舍取决于你接下来对数据做什么集体操作。
- shared memory 有 32 个 bank，矩阵转置「按行写按列读」最易撞 bank；`+block_k` padding 让行宽不再是 32 的倍数，把最坏的 32 路 conflict 大幅降低，残留由 swizzle 兜底。
- padding 是静态改物理行宽，swizzle 是动态改线程-元素映射，二者互补——这是所有需要 shared memory 中转的算子（不止转置）的通用优化心法。

## 7. 下一步学习建议

- **下一步读 u2-l3（循环、并行与规约原语）**：本讲的 `fragment` 一旦要做跨线程规约（如量化里的 `T.reduce_absmax`），就要用到 `T.Parallel`/`T.unroll`/`T.vectorized` 与 `T.reduce_*`，那是 u2-l3 的主题，正好承接。
- **横向对照 u3-l1（批量转置 kernel 深入）**：本讲只讲了转置的存储搬运与 padding，转置 kernel 的「寄存器 4×4 块转置 + `loop_layout_fn` swizzle」细节在 u3-l1 专门展开。
- **回看量化全貌 u4-l2**：本讲把 `per_token_cast_kernel` 当作存储搬运的例子，它的真正主题（absmax + 两段规约 + cast 写出）在 u4-l2 完整讲解。
- **延伸阅读建议**：CUDA 官方手册里 *Shared Memory* 与 *Tensor Memory Accelerator (TMA)* 两章，可帮你把本讲的 `bank`/`TMA` 概念落到硬件层面；TileLang 文档里 `T.copy` 的 *storage scope* 说明可补充本讲没覆盖的搬运组合。
