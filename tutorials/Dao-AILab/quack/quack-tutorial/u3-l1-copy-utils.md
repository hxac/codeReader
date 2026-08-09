# copy_utils 拷贝工具

## 1. 本讲目标

本讲是「核心工具层」的第一讲。学完后你应该能够：

- 用一句话说清 CuTe 里 **TiledCopy（分块拷贝计划）** 是什么，以及它由哪三部分组成。
- 读懂 `tiled_copy_2d` / `tiled_copy_1d`，并能解释 `threads_per_row`、`num_threads`、`vecsize` 如何决定每个 CTA 覆盖的数据块形状 `tiler_mn`。
- 区分 GPU 上两类异步拷贝路径：**cp.async（线程级异步拷贝）** 与 **TMA（张量内存加速器，描述符驱动的整块拷贝）**，知道它们各自由谁完成、如何处理边界。
- 理解 `predicate_k` 与 `fill_oob` 如何联手处理「N 不能被 tile 整除」的非整除边界。

本讲是后续所有内核讲义的地基：归约内核（softmax/rmsnorm）靠 `tiled_copy_2d` + cp.async + `predicate_k` 加载数据；GEMM 内核靠 TMA 加载整块 tile。看懂这一讲，再读任何内核的「数据搬运」部分都不会卡壳。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（对应 u1-l4、u2-l1）：

- **静态值与动态值**：CuTe-DSL 在编译期就要知道线程布局、tile 形状、向量化宽度；运行期才知道 N、batch 这类尺寸。`const_expr(...)` 把判断标记为编译期分支。
- **CuTe 张量与布局（Layout）**：一个 `cute.Tensor` = 指针 + 布局。布局是一个 `(shape, stride)` 序列，描述「逻辑坐标 → 线性偏移」的映射。
- **CTA / warp / thread / cluster**：一个 CTA（线程块）含多个 warp（每 warp 32 线程）；Hopper 起，多个 CTA 可组成一个 cluster 协作。
- **归约内核的两层结构**（u2-l1）：主机侧 `@cute.jit` 的 `__call__` 用 `_get_tiled_copy` 推导出 `tiler_mn`、grid、block，再启动设备侧 `@cute.kernel`；设备内核用同一个 `tiled_copy` 把 gmem 数据搬到 smem 再到寄存器。

几个本讲会反复用到的术语：

| 术语 | 含义 |
|------|------|
| TiledCopy | 一份「线程↔数据」的拷贝计划，编译期确定 |
| CopyAtom | 拷贝「原子」，对应一条硬件事务（如一次 128 bit 加载） |
| `partition_S` / `partition_D` | 把张量按线程切成「源 / 目的」的每线程视图 |
| cp.async | Ampere 起的线程级异步 gmem→smem 加载 |
| TMA | Hopper 起的描述符驱动整块拷贝 |
| tiler_mn | 一个 CTA 在一次拷贝迭代里覆盖的 (M, N) 数据块形状 |

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [quack/copy_utils.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/copy_utils.py) | 全项目共享的拷贝原语集合 | `tiled_copy_2d` / `tiled_copy_1d` / `copy` / `get_copy_atom` / `predicate_k` / TMA 与 cp.async.bulk 工厂 |
| [quack/reduction_base.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduction_base.py) | 归约内核共享基类 | `_get_tiled_copy`（如何从 N、vecsize 推出 tiler_mn）|
| [quack/softmax.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py) | softmax 前/反向内核 | 真实调用点：cp.async 加载 + predicate_k + fill_oob |
| [quack/utils.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/utils.py) | 通用小工具 | `fill_oob`：给越界 smem 位置写入哨兵值 |

阅读建议：先看 4.1 把 `tiled_copy_2d` 和 `_get_tiled_copy` 串起来（这是本讲核心），再看 4.2 了解两种异步路径，最后看 4.3 理解边界处理。三者合起来就是「归约内核加载一行数据」的完整图景。

## 4. 核心概念与源码讲解

### 4.1 tiled_copy_2d / tiled_copy_1d：线程到数据的拷贝计划

#### 4.1.1 概念说明

在 GPU 上「搬运一块数据」并不是一句 `memcpy`。你需要回答三个问题：

1. **用什么指令搬？**（一次搬 16 字节还是 128 字节？走普通 load 还是异步 load？）—— 这是 **CopyAtom（拷贝原子）**。
2. **哪个线程搬哪一块？**（线程 0 搬第几行第几列？）—— 这是 **线程布局（thr_layout）**。
3. **每个线程一次搬几个元素？**（向量化宽度）—— 这是 **值布局（val_layout）**。

CuTe 把这三者打包成一个编译期对象 **`TiledCopy`**。有了它，你只要调用 `thr_copy.partition_S(src)` / `partition_D(dst)`，CuTe 就会自动把 `src`/`dst` 张量切成「每个线程负责的那一片」。QuACK 在此之上写了两个薄封装：`tiled_copy_1d`（一维，连续切分）和 `tiled_copy_2d`（二维，行/列方向分别配线程数）。

为什么归约内核需要 2D？因为归约是「沿 N 方向把一行压成一个标量」，天然要把线程沿 N 方向铺开（多个线程合作归约同一行），同时还要有少量线程沿 M 方向处理多行。`tiled_copy_2d` 正好让你独立指定「每行几个线程」和「总共几个线程」。

#### 4.1.2 核心流程

`tiled_copy_2d` 的构造流程（对应 4.1.3 的源码）：

```text
输入: dtype, threads_per_row, num_threads, num_copy_elems(=vecsize), is_async

1. num_copy_bits = vecsize * dtype.width            # 一次原子拷贝的比特数
2. copy_op       = CopyG2SOp()  若 is_async         # cp.async gmem→smem
                   否则 CopyUniversalOp()            # 普通同步拷贝
3. copy_atom     = make_copy_atom(copy_op, dtype, num_bits_per_copy=num_copy_bits)
4. thr_layout    = ((num_threads // threads_per_row, threads_per_row), order=(1,0))
5. val_layout    = (1, vecsize)
6. 返回 make_tiled_copy_tv(copy_atom, thr_layout, val_layout)
```

要点：

- `thr_layout` 是二维的：模式 0（大小 `num_threads // threads_per_row`）对应 **行（M）**，模式 1（大小 `threads_per_row`）对应 **列（N）**。`order=(1,0)` 让模式 1 变化最快，于是连续的线程号先沿 N 方向填——线程 \(t\) 的行号为 \(t // \text{threads\_per\_row}\)，列号为 \(t \bmod \text{threads\_per\_row}\)。
- `val_layout = (1, vecsize)`：每个线程在 **N 方向** 拥有 `vecsize` 个连续元素（M 方向只有 1 个）。这与 `thr_layout` 的 N 方向对齐。
- 因此一次拷贝迭代里，`threads_per_row` 个线程共同覆盖 \( \text{threads\_per\_row} \times \text{vecsize} \) 个 N 元素，而 `num_threads // threads_per_row` 行 M 被同时处理。

把这个「每线程覆盖多少」放大到整个 CTA，就得到 `tiler_mn`（见 4.1.4 的推导）。

#### 4.1.3 源码精读

`tiled_copy_2d` 的定义——本讲最重要的一个函数：

[quack/copy_utils.py:L364-L380](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/copy_utils.py#L364-L380) — 构造一个二维 TiledCopy：`copy_atom`（拷贝原子）+ `thr_layout`（线程二维布局）+ `val_layout`（每线程向量化宽度）。`assert num_threads % threads_per_row == 0` 保证线程能整齐地按行分组。

与之对照的一维版本 `tiled_copy_1d`，把线程铺成一条线、值也铺成一条线，用于「连续切一维缓冲」的简单场景：

[quack/copy_utils.py:L332-L340](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/copy_utils.py#L332-L340) — 一维 TiledCopy，`thr_layout` 与 `val_layout` 都是一维的；`vectorized_thread_partition`（[L343-L361](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/copy_utils.py#L343-L361)）在它之上把张量切成「相邻的每线程向量」。

拷贝原子与「拷贝执行函数」`get_copy_atom` / `copy`——后者是归约内核真正调用来搬数据的入口：

[quack/copy_utils.py:L307-L329](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/copy_utils.py#L307-L329) — `get_copy_atom` 按 `num_copy_bits = min(128, num_copy_elems * width)` 选 128 bit 上限的原子，并在 `is_async=True` 时选用 `CopyG2SOp()`（cp.async）；`copy` 从源张量形状读出每线程元素数、造原子、调用 `cute.copy`，并把谓词 `pred` 透传下去。

而 `_get_tiled_copy` 把 `tiled_copy_2d` 与「归约一行」的几何关系统一起来，是连接拷贝计划与 grid/block 配置的桥梁：

[quack/reduction_base.py:L42-L50](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduction_base.py#L42-L50) — 由 N、vecsize、threads_per_row、num_threads、cluster_n 推出 `tiler_mn`，并调用 `tiled_copy_2d` 生成拷贝计划。`tiler_mn[0]` 是每 CTA 处理的行数，`tiler_mn[1]` 是每 CTA 覆盖的 N 宽度。

注意它把拷贝计划（`tiled_copy`）和几何尺寸（`tiler_mn`、`threads_per_row`）一并返回——主机侧用 `tiler_mn` 算 grid（见 softmax `__call__` 的 `grid=[ceil_div(M, tiler_mn[0]), cluster_n, 1]`），设备侧用 `tiled_copy` 做实际的 `partition_S`/`partition_D`。

#### 4.1.4 代码实践

**实践目标**：亲手推导一次 `_get_tiled_copy`，确认 `threads_per_row`、`num_threads`、`vecsize` 如何决定 `tiler_mn`。

**操作步骤**：

1. 打开 [reduction_base.py:L42-L50](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduction_base.py#L42-L50)，记下两个公式：

\[
\text{num\_blocks\_N} = \left\lceil \frac{N / \text{vecsize}}{\text{threads\_per\_row} \times \text{cluster\_n}} \right\rceil
\]

\[
\text{tiler\_mn} = \big(\; \text{num\_threads} / \text{threads\_per\_row},\;\; \text{vecsize} \times \text{num\_blocks\_N} \times \text{threads\_per\_row} \;\big)
\]

2. 取一组真实参数：`dtype = bfloat16`（width = 16）、`N = 4096`、online softmax。
   - `vecsize = 128 // width = 128 // 16 = 8`（见 softmax `__call__` 的 `vecsize=128 // largest_dtype_width`，[softmax.py:L76-L78](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L76-L78)）。
   - 查 [softmax.py:L37-L42](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L37-L42) 的 `_threads_per_row`：N=4096 ≤ 6144 → `threads_per_row = 64`。
   - 查 [reduction_base.py:L22-L23](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduction_base.py#L22-L23) 的 `_num_threads`：N ≤ 16384 → `num_threads = 128`。
   - 查 [softmax.py:L44-L64](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L44-L64) 的 `_set_cluster_n`（bf16）：N=4096 ≤ 16K → `cluster_n = 1`。

3. 代入公式：

\[
\text{num\_blocks\_N} = \left\lceil \frac{4096 / 8}{64 \times 1} \right\rceil = \left\lceil \frac{512}{64} \right\rceil = 8
\]

\[
\text{tiler\_mn}[0] = 128 / 64 = 2,\qquad
\text{tiler\_mn}[1] = 8 \times 8 \times 64 = 4096
\]

**需要观察的现象**：`tiler_mn = (2, 4096)`——每个 CTA 处理 **2 行 × 4096 列**，恰好整行覆盖（因为 `cluster_n=1` 且 `tiler_mn[1] = N`）。

**预期结果**：
- grid 的 x 维 = `ceil_div(M, 2)`（每 CTA 两行）。
- 64 个线程铺满一行（N 方向），每线程搬 `vecsize=8` 个 bf16 = 128 bit = 一次 cp.async 事务；一行需要 8 次「N 迭代」（`num_blocks_N=8`）才能搬完。
- `is_even_N = (N == tiler_mn[1] * cluster_n) = (4096 == 4096)` 为真，走快路径（无谓词）。

> 若无法在本地跑 GPU，本实践为「源码阅读 + 手算」型，结果即上面推导；标「待本地验证」的部分是用 `pytest tests/test_softmax.py -x -k 'bfloat16'` 实跑确认 `tiler_mn` 推导无误。

#### 4.1.5 小练习与答案

**练习 1**：若把上面例子改成 `N = 32768`（bf16, online），重新推导 `tiler_mn`。

**参考答案**：`_threads_per_row`：N=32768 > 16384 → 256；`_num_threads`：N > 16384 → 256；`_set_cluster_n`（bf16）：N=32768 ≤ 32K → `cluster_n = 2`。`num_blocks_N = ceil((32768/8)/(256*2)) = ceil(4096/512) = 8`。`tiler_mn = (256/256, 8*8*256) = (1, 16384)`。即每 CTA 处理 1 行 × 16384 列，两个 cluster CTA 合起来覆盖 32768。

**练习 2**：`tiled_copy_2d` 里为什么 `assert num_threads % threads_per_row == 0`？

**参考答案**：线程必须能整齐地按 `threads_per_row` 分组铺到行上，否则 `thr_layout` 的模式 0（`num_threads // threads_per_row`）会出现非整数，无法构成合法的 CuTe 布局；这也保证每行分到同样多的线程，归约负载均衡。

**练习 3**：`val_layout = (1, num_copy_elems)` 的模式 0 为什么是 1？

**参考答案**：每个线程的向量化只沿 N（列）方向连续，M（行）方向每线程只负责 1 个位置；这与 `thr_layout` 把 N 设为最快变化维一致，保证一次 128 bit 原子读的是同一行内的连续元素（合并访存）。

---

### 4.2 异步 cp.async 与 TMA：两条异步拷贝路径

#### 4.2.1 概念说明

把数据从 gmem（全局显存）搬到 smem（共享内存）是几乎所有 GPU 内核最耗时的一步。为了让「计算」和「搬运」重叠，现代 GPU 提供了**异步拷贝**——发出指令后不等完成，先去做别的，稍后再统一等待。QuACK 里用到的异步拷贝分两类：

**cp.async（Ampere SM80 起）**：线程级的异步加载。每条 `cp.async` 指令由一个线程发出，搬运一小段（如 16/128 字节）gmem→smem。它粒度细、靠线程谓词控制边界，但需要很多线程一起发指令才能填满带宽。在 CuTe 里它对应 `CopyG2SOp()`——也就是本讲 `tiled_copy_2d`/`copy` 里 `is_async=True` 的那条分支。**归约内核（softmax/rmsnorm）走这条路**，因为它们的数据量是一「行」，cp.async 正好够用且边界处理灵活。

**TMA（Tensor Memory Accelerator，Hopper SM90 起）**：描述符驱动的整块拷贝。你先用张量的 shape/stride/swizzle 生成一个 **TMA descriptor（描述符）**，之后一条指令就能搬一整块 tile（甚至跨 cluster 多播）。TMA 自带**边界检查**：越界部分由硬件按规则填充（如置零），不需要线程谓词。完成与否用 **mbarrier（事务屏障）** 的 `complete_tx::bytes` 信用机制通知。**GEMM 内核走这条路**，因为 tile 大、希望一条指令搬完。此外 SM90+ 还有 `cp.async.bulk`（批量异步拷贝），既支持 G2S 也可做 SMEM→GMEM 的 `cpasync_bulk_s2g`，后者还能附带原子归约（`cp.reduce.async.bulk`）。

一句话区分：**cp.async 是「一群线程每人搬一小块」，TMA 是「一个描述符搬一整块，硬件自己管边界」**。

#### 4.2.2 核心流程

**cp.async 路径**（归约内核）：

```text
1. tiled_copy = tiled_copy_2d(..., is_async=True)   # copy_op = CopyG2SOp()
2. thr_copy   = tiled_copy.get_slice(tidx)
3. tXgX = thr_copy.partition_S(gX)                  # 每线程的 gmem 源视图
   tXsX = thr_copy.partition_D(sX)                  # 每线程的 smem 目的视图
4. copy(tXgX, tXsX, is_async=True)                  # 发出 cp.async（不等待）
5. cp_async_commit_group()                          # 把这批 cp.async 打包成一组
6. cp_async_wait_group(0)                           # 等待「≤0 组未完成」=全部完成
7. （此后 smem 数据可用，可加载到寄存器）
```

**TMA 路径**（GEMM 内核，以 `tma_get_copy_fn` 为例）：

```text
1. 预先在主机侧构造 TMA descriptor，封装进 atom (CopyAtom)
2. tma_get_copy_fn(atom, cta_coord, cta_layout, src_tensor, dst_tensor)
     → 用 cpasync.tma_partition 按 tile 切出 smem/gmem 视图
     → 返回 copy_fn(src_idx, dst_idx)
3. copy_fn 内部 cute.copy(atom, src[stage], dst[stage])      # 一条 TMA 指令搬一整块
4. 由流水线的 mbarrier arrive/wait 协议确认完成（complete_tx::bytes）
```

TMA 的完成信号不靠 `cp_async_wait_group`，而靠 **mbarrier**：发起 TMA 时把字节数「上膛」到屏障，硬件搬完后扣减信用，信用归零屏障放行——这套机制在 u3-l5（流水线与同步原语）详讲。

#### 4.2.3 源码精读

**cp.async 的入口**——`copy` 函数和它的原子工厂 `get_copy_atom`：

[quack/copy_utils.py:L307-L329](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/copy_utils.py#L307-L329) — `is_async=True` 时 `copy_op = cpasync.CopyG2SOp()`，这正是 cp.async 的 gmem→smem 原子；上限 128 bit 一次。

softmax 前向真实调用 cp.async 加载一行：

[quack/softmax.py:L133-L136](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L133-L136) — `copy(tXgX, tXsX, is_async=True)` 发出 cp.async，紧跟 `cp_async_commit_group()` + `cp_async_wait_group(0)` 等全部搬完，之后 smem 才能安全读入寄存器。

**TMA / cp.async.bulk 的工厂**——下面三个函数为 GEMM 提供「按 stage 搬整块」的拷贝闭包：

[quack/copy_utils.py:L1172-L1219](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/copy_utils.py#L1172-L1219) — `tma_get_copy_fn`：用 `cpasync.tma_partition` 按 CTA/cluster 切分 smem 与 gmem 张量，返回 `copy_tma(src_idx, dst_idx)`；`single_stage` 切换「带 stage 维 / 单 stage」两种用法。它是描述符驱动的整块拷贝。

[quack/copy_utils.py:L1036-L1108](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/copy_utils.py#L1036-L1108) — `cpasync_bulk_get_copy_fn`：自动判别方向（SMEM→GMEM 用 `cpasync_bulk_s2g`，GMEM→SMEM 用 `CopyBulkG2SOp`），同样返回按 stage 的 copy 闭包。

[quack/copy_utils.py:L849-L884](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/copy_utils.py#L849-L884) — `cpasync_bulk_s2g`：SMEM→GMEM 的批量异步拷贝；当传入 `reduction_kind` 时退化为 `cp.reduce.async.bulk`（存储即原子归约），被 split-K 的 partials 累加等场景复用。

GEMM 内核里真实调用 TMA 拷贝（以 epilogue 的 D 写回为例）：

[quack/gemm_base.py:L1188-L1194](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L1188-L1194) — 用 `tma_get_copy_fn` 生成 D 的整块写回闭包。注意旁边 L1181-L1187 的注释：TMA 拷贝支持 `cache_policy`（L2 提示）等 kwarg 透传，团队实测对当前场景是负收益而关闭——这是「测量过的事实」，不是猜的。

#### 4.2.4 代码实践

**实践目标**：在源码里把「cp.async 归约路径」与「TMA GEMM 路径」配对找出来，体会为何用两条路。

**操作步骤**：

1. 在 [softmax.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py) 搜索 `is_async=True`，确认归约内核用 cp.async 加载。
2. 在 [gemm_base.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py) 搜索 `tma_get_copy_fn` / `tma_get_block_copy_fn`，确认 GEMM 用 TMA 加载 A/B、写回 D。
3. 对比两处「等待完成」的方式：softmax 用 `cp_async_wait_group(0)`；GEMM 用流水线 mbarrier（见 u3-l5）。

**需要观察的现象**：

- 归约内核的数据搬运「薄而窄」：一行数据，几十个线程发 cp.async，一次 `commit_group` 就收尾。
- GEMM 的数据搬运「厚而重」：每块 tile 一条 TMA 指令，配合多级流水线（producer/consumer）让搬运与 MMA 计算重叠。

**预期结果**：你能用一句话讲清「softmax 为何不用 TMA」——softmax 的 tile 形状随 N 变化、且边界用谓词灵活处理更直接；TMA 的 descriptor 构造有成本，对「一行」这种小数据量得不偿失。

> 这是源码阅读型实践；若本地有 H100/B200，可用 `pytest tests/test_gemm_functional.py -x` 与 `pytest tests/test_softmax.py -x` 各跑一次，观察两者编译产物里拷贝指令的差异（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：cp.async 和 TMA 分别靠什么机制通知「搬完了」？

**参考答案**：cp.async 靠 `cp_async_commit_group` + `cp_async_wait_group(N)`（等「未完成组数 ≤ N」）；TMA 靠 mbarrier 的 `complete_tx::bytes` 信用——发起时上膛字节数，硬件搬完扣减，归零放行。

**练习 2**：`cpasync_bulk_s2g` 的 `reduction_kind` 参数有什么用？

**参考答案**：传入 `reduction_kind`（如 ADD/MIN/MAX）会把普通 bulk store 变成 `cp.reduce.async.bulk`——存储的同时按该算子与 gmem 旧值做原子归约。split-K 把多个 partials 累加到同一输出时用它，省去再启动一个归约内核。注意 `_cpasync_bulk_reduce_args`（[L808-L846](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/copy_utils.py#L808-L846)）按 PTX 合法性限制了每种 dtype 支持的算子（如 fp32 只支持 ADD）。

**练习 3**：为什么 `tiled_copy_2d` 的 `is_async` 参数对 TMA 路径无意义？

**参考答案**：`is_async` 只在选 `copy_op` 时区分 `CopyG2SOp()`（cp.async）与 `CopyUniversalOp()`（同步），两者都是线程级原子；TMA 用的是完全不同的 `CopyAtom`（descriptor 驱动），由 `tma_get_copy_fn` 等独立工厂构造，根本不走 `tiled_copy_2d`。

---

### 4.3 谓词 predicate_k 与 fill_oob：非整除边界处理

#### 4.3.1 概念说明

当 N 不能被「每 CTA 覆盖的 N 宽度」整除时，最后一轮拷贝会读到行尾之外（out-of-bounds, OOB）。这对归约内核是致命的：max 归约会把垃圾数据当成最大值，sum 归约会把垃圾加进分母。QuACK 用两件套处理边界：

- **`predicate_k`**：一个 `@cute.jit` 函数，为「k 维（N 方向）」生成一个寄存器里的布尔谓词张量——每个元素判断「这个坐标是否 < limit(=N)」。把它作为 `pred=` 传给 `cute.copy`，越界的拷贝就被屏蔽（不真正发起访存）。
- **`fill_oob`**：谓词只是「不读/不写」那些位置，smem 里那些位置仍是旧值。对归约来说必须给它们填一个**哨兵值**：max 归约填 \(-\infty\)（不影响最大值），sum 归约填 0（不影响求和）。`fill_oob` 就负责在拷贝后把 OOB 位置覆盖成哨兵值。

为什么只给 k 维算谓词？注释写得很明白：**M（mn）维用 `if` 守卫整行**——`if tXcX[0][0] < shape[0]` 跳过整行越界的 CTA；**N（k）维用谓词**逐元素屏蔽。一个用控制流，一个用数据流谓词，各取所长。

还有一个关键快路径 **`is_even_N`**：当 \( N = \text{tiler\_mn}[1] \times \text{cluster\_n} \) 时，整行恰好被 cluster 的 CTAs 无缝覆盖，**没有越界**，于是 `pred=None`、不做 `fill_oob`——省掉一堆谓词计算和 smem 写入。softmax 的 `_set_cluster_n` 和 `_cap_cluster_n`（u2-l1）之所以要精心选 cluster_n，部分原因就是尽量命中这个快路径。

#### 4.3.2 核心流程

边界处理的完整决策（softmax 前向）：

```text
is_even_N = (N == tiler_mn[1] * cluster_n)          # 编译期常量
tXpX      = None                       若 is_even_N
          else predicate_k(coord_tensor, limit=N)    # 只为 k 维算谓词
copy      = partial(copy_utils.copy, pred=tXpX)      # 所有拷贝共用同一谓词

if 行号 < M:                       # M 维越界用 if 守卫
    copy(gmem, smem, is_async=True)                  # 谓词屏蔽 k 维越界
cp_async_commit_group(); cp_async_wait_group(0)

if not is_even_N:
    fill_oob(smem, tXpX, -inf)                        # 给 OOB 填哨兵 -inf
# 此后 smem 的每一行都是「真实值 + (-inf 填充)」，可安全归约
```

`predicate_k` 内部：它接收每线程的「坐标张量」`tAcA`（由 `thr_copy.partition_S(身份张量)` 得到，每个寄存器槽对应一个数据坐标），构造一个同形状的布尔张量 `tApA`，对每个 k 槽填入 `坐标_k < limit`。由于用 `range_constexpr` 展开，槽的数量在编译期已知。

#### 4.3.3 源码精读

`predicate_k` 的定义——注意它只动 k 维，且用编译期展开：

[quack/copy_utils.py:L383-L396](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/copy_utils.py#L383-L396) — 用 `cute.elem_less(k 坐标, limit)` 给每个 k 槽填布尔值；mn 维不处理（注释「For the mn dimension, we will use "if"」）。输出的 `tApA` 布局把 k 维放在 stride-1（最快变化），与拷贝原子对齐。

softmax 前向把 `is_even_N` / `predicate_k` / `fill_oob` 串成边界处理：

[quack/softmax.py:L121-L139](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L121-L139) — `is_even_N` 为真则 `tXpX=None`（快路径）；否则用 `predicate_k` 造谓词；拷贝后用 `fill_oob(..., -inf)` 给越界位置填 \(-\infty\)，保证后续 max/sum 归约正确。

`fill_oob` 的实现——给每个 OOB 槽写哨兵值：

[quack/utils.py:L103-L118](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/utils.py#L103-L118) — 用一个填满 `fill_value` 的寄存器张量，遍历每个 (rest_v, rest_k)，凡 `tXpX` 为假的位置就把哨兵值 autovec 写进 smem。

值得对照的是 softmax **反向**：它**不调** `fill_oob`，源码注释称 cp.async 的谓词路径会把越界填 0（dot 归约需要 0）：

[quack/softmax.py:L329-L331](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L329-L331) — 反向在 `cp_async_wait_group(0)` 后直接用 smem，注释「Don't need fill_oob since cp.async will automatically fills OOB elements with zeros」。前向填 \(-\infty\)（max 用）与反向依赖 0（dot 用）正是哨兵值随归约算子而变的体现。

> 说明：cp.async 在谓词屏蔽后对 smem 目的位置的**确切硬件行为**（是否清零、是否保留旧值）在不同指令变体与 GPU 代际间有差异，本讲以源码注释和「前向显式 fill_oob、反向不 fill」的事实为准；若你在本地调试边界用例，建议用 `cute.printf` 打印 smem 越界位置的实际值确认（待本地验证）。

#### 4.3.4 代码实践

**实践目标**：跟踪一个「非整除 N」的 softmax 前向，看清谓词与 fill_oob 各自生效的时机。

**操作步骤**：

1. 取 `dtype = bfloat16`、`N = 3000`（不是 8 的倍数也对，但取一个不能被常见 tile 整除的值更能体现边界）。先按 4.1.4 的方法算出 `tiler_mn` 与 `cluster_n`。
2. 判断 `is_even_N = (3000 == tiler_mn[1] * cluster_n)`。由于 3000 含因子 3，而 `tiler_mn[1]` 是 `vecsize(8) × 某数 × threads_per_row(2 的幂)` 的形式，几乎必然 **不相等** → `is_even_N = False`。
3. 阅读 [softmax.py:L121-L139](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L121-L139)，标出三件事发生的顺序：① `predicate_k` 造谓词；② 谓词屏蔽的 cp.async 加载；③ `fill_oob(-inf)`。
4. 思考：如果不调 `fill_oob`，max 归约会怎样？

**需要观察的现象**：`is_even_N=False` 时，谓词张量 `tXpX` 非空，最后一轮 k 迭代里部分线程的谓词为假；这些线程对应的 smem 位置随后被 `fill_oob` 覆盖为 \(-\infty\)。

**预期结果**：max 归约忽略 \(-\infty\) 填充，sum 归约里 \(e^{-\infty - \text{max}} = 0\) 不影响分母——边界值被「无害化」。这就是为什么 QuACK 的 softmax 在任意 N 下都能数值正确。

> 这是源码阅读型实践。如本地有 GPU，可运行 `pytest tests/test_softmax.py -x -k 'bfloat16'` 并构造一个 N=3000 的用例（参考测试里的参数化写法），对比 `is_even_N` 真假两条路径的数值与（若开启打印）谓词输出（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `predicate_k` 只算 k 维谓词，mn 维交给 `if`？

**参考答案**：mn（M）维越界是「整行越界」，用 `if tXcX[0][0] < shape[0]` 直接跳过整个 CTA 的计算最省；k（N）维越界是「行内部分元素越界」，必须逐元素屏蔽，适合用谓词张量。控制流处理「块级」越界，谓词处理「元素级」越界。

**练习 2**：前向 fill_oob 用 \(-\infty\)，反向为什么（据注释）不需要 fill_oob？

**参考答案**：前向的 max 归约必须让越界元素「不可能是最大」，故填 \(-\infty\)；反向的 dot 归约（\(\sum dy_j y_j\)）必须让越界元素「对和没贡献」，需要 0。源码注释指出反向走的 cp.async 变体会把越界填 0，于是省掉显式 `fill_oob`——哨兵值随归约算子（max vs add）而变。

**练习 3**：`is_even_N` 为真时省掉了哪些开销？

**参考答案**：省掉了 `predicate_k` 的谓词张量构造与逐元素比较、省掉了把谓词透传给每次 `cute.copy` 的开销、省掉了 `fill_oob` 对 smem 越界位置的写入。这是一条「整除即加速」的快路径，也是 `_set_cluster_n`/`_cap_cluster_n` 尽量让 cluster 切分整齐的动机之一。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「读一行数据」的完整推演。

**任务**：给定 `dtype = bfloat16`、`N = 4096`、online softmax、`cluster_n = 1`，请你：

1. **算拷贝计划**：推出 `vecsize`、`threads_per_row`、`num_threads`、`tiler_mn`（参考 4.1.4）。
2. **画线程映射**：写出线程 `t = 0` 与 `t = 64` 各自负责的数据坐标（行号、N 起始列），验证 `thr_layout` 的 `order=(1,0)` 含义。
3. **选异步路径**：确认归约内核用 cp.async，标出 `commit_group` / `wait_group` 的位置。
4. **判边界**：算 `is_even_N`，说明本例是否需要 `predicate_k` / `fill_oob`。
5. **改一个值再推**：把 N 改成 5000，重做第 4 步，指出哪条分支会被激活、哨兵值是多少。

**参考要点**：

- 第 1 步：`vecsize=8, threads_per_row=64, num_threads=128, tiler_mn=(2, 4096)`。
- 第 2 步：`t=0` → 行 0、N 列起始 0；`t=64` → 行 1、N 列起始 0（因为 `threads_per_row=64`，第 64 号线程进入第二行）。每线程搬 8 个 bf16。
- 第 3 步：cp.async，对应 [softmax.py:L133-L136](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L133-L136)。
- 第 4 步：`is_even_N = (4096 == 4096*1) = True`，无需谓词、无需 fill_oob。
- 第 5 步：N=5000 时 `tiler_mn[1]` 形如 \(8 \times \lceil 5000/(8\cdot64)\rceil \times 64 = 8 \times 10 \times 64 = 5120 \ne 5000\)，故 `is_even_N=False`，激活 `predicate_k` + `fill_oob(-inf)`；max 归约靠 \(-\infty\) 哨兵保证正确。

> 提示：本综合实践为「手算 + 源码对照」型，全部结论可在阅读 copy_utils.py / reduction_base.py / softmax.py 后得出；若本地有 GPU，用 `cute.printf` 在内核里打印 `tXpX` 与 `tiler_mn` 验证你的推导（待本地验证）。

## 6. 本讲小结

- **TiledCopy = 拷贝原子 + 线程布局 + 值布局**。`tiled_copy_2d` 让你独立指定每行线程数（`threads_per_row`）与总线程数（`num_threads`），值沿 N 方向向量化（`vecsize`）。
- **`_get_tiled_copy` 把拷贝计划变成几何尺寸**：`tiler_mn[0] = num_threads // threads_per_row`（每 CTA 行数），`tiler_mn[1] = vecsize × num_blocks_N × threads_per_row`（每 CTA N 宽度）。
- **两条异步路径**：cp.async（线程级，`CopyG2SOp`，归约内核用，`commit/wait_group` 收尾）与 TMA（描述符级整块拷贝，GEMM 用，mbarrier 收尾）；`cp.async.bulk` 还能做带归约的 SMEM→GMEM 存储。
- **边界处理两件套**：`predicate_k` 为 k 维生成布尔谓词屏蔽越界拷贝，`fill_oob` 给 smem 越界位置填哨兵值（max 填 \(-\infty\)、sum 填 0）；mn 维用 `if` 守卫整行。
- **`is_even_N` 是快路径**：整除时跳过谓词与 fill_oob，这也是 cluster 切分尽量整齐的动机之一。

## 7. 下一步学习建议

本讲建立了「数据搬运」的地基。接下来建议：

- **u3-l2（layout_utils 布局代数）**：本讲的 `partition_S`/`partition_D` 依赖 CuTe 布局，下一讲系统讲 transpose/select/zero-stride 等布局变换，你会更清楚坐标张量是怎么来的。
- **u3-l5（异步流水线与同步原语）**：本讲提到 TMA 靠 mbarrier 完成通知，但没展开；流水线讲义会讲清 producer/consumer 的多级 mbarrier 协作，把 cp.async/TMA 真正「重叠」起来。
- **回头重读 u2-l2（softmax 前向）**：有了本讲的拷贝词汇，再读 softmax 内核的加载段会豁然开朗——它就是 4.1 + 4.2 + 4.3 的直接组合。
- 想看 TMA 的完整用法，可直接跳到 u5-l2（SM90 GEMM）观察 A/B 的 TMA 加载如何与 WGMMA 流水线衔接。
