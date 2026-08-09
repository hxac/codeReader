# ReductionBase 共享基类

## 1. 本讲目标

本讲是第 2 单元（归约内核）的第一讲。QuACK 里 `rmsnorm`、`softmax`、`cross_entropy`、`topk` 等归约内核都继承自同一个基类 `ReductionBase`。学完本讲，你应该能够：

1. 说出 `ReductionBase` 为所有归约内核抽象出了哪些**共享逻辑**（线程数、cluster 配置、tiled copy、归约缓冲、mbarrier）。
2. 推导 `_get_tiled_copy` 如何由 `N`、`vecsize`、`threads_per_row`、`cluster_n` 算出 `tiler_mn`，并理解 `tiler_mn` 的物理含义。
3. 解释 `_cap_cluster_n` 为什么要在运行时把 `cluster_n` 往小「夹」一刀，否则会发生重复计数。
4. 读懂归约缓冲（reduction buffer）的三维布局 `(rows, (warps_per_row, cluster_n), num_slots)` 与 mbarrier 数组的分配与初始化。

本讲只讲**主机侧配置与共享骨架**，不深入每个内核的 `@cute.kernel` 主循环（那是 u2-l2 的事），也不深入 `reduce.py` 里的归约原语（那是 u2-l4 的事）。

## 2. 前置知识

本讲承接 u1-l4《CuTe-DSL 编程模型入门》，假定你已经知道：

- **`@cute.jit` / `@cute.kernel`**：前者是可编译的 Python 函数（可作主机编排者，也可作设备内联辅助函数），后者是可 `.launch` 启动的并行 CUDA 内核。`ReductionBase` 的 `__call__` 是 `@cute.jit`（主机侧配置 + 启动），`kernel` 是 `@cute.kernel`（设备侧）。
- **`const_expr(...)`**：把判断标记为**编译期**分支，只编入命中分支。本讲里所有 `if const_expr(self.cluster_n > 1)` 都是编译期特化——`cluster_n` 不同的取值会编译出不同的 cubin。
- **GPU 层次术语**：thread（线程）→ warp（线程束，32 线程）→ CTA/Block（线程块）→ cluster（Hopper 起引入的「CTA 簇」，最多 16 个 CTA 协作）。`WARP_SIZE = 32`。
- **TiledCopy**：CuTe 里描述「线程如何协作搬运一块数据」的抽象，由一个 copy atom（搬运指令）+ 线程布局（thr_layout）+ 值布局（val_layout）构成。
- **smem / mbarrier**：共享内存（shared memory）是 CTA 内高速存储；mbarrier（内存屏障）是 Hopper 起用于异步拷贝完成同步与 cluster 间同步的硬件原语。

如果你对「为什么归约内核要沿 N 维用多个 CTA（cluster）协作」还没有直觉，别急，4.1 节会从头解释。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到什么 |
| --- | --- | --- |
| [`quack/reduction_base.py`](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduction_base.py) | 归约内核的**共享基类** | 全部方法（本讲主角） |
| [`quack/copy_utils.py`](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/copy_utils.py) | 被一切内核复用的拷贝原语 | `tiled_copy_2d` / `tiled_copy_1d` |
| [`quack/softmax.py`](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py) | Softmax 前向/反向内核 | `_threads_per_row`、`_set_cluster_n`、`__call__` 配置段 |
| [`quack/rmsnorm.py`](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py) | RMSNorm 前向/反向内核 | `__call__` 里调用 `_cap_cluster_n` / `_get_tiled_copy` |
| [`quack/reduce.py`](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py) | 归约原语 | `row_reduce` 如何消费归约缓冲（仅作衔接） |

官方一句话定位（来自 `AGENTS.md`）：

> Reduction kernels (`rmsnorm.py`, `softmax.py`, `cross_entropy.py`) inherit from `ReductionBase` in `reduction_base.py`. They share a pattern: configure cluster size, get tiled copies, allocate reduction buffers with mbarriers, then launch a `@cute.kernel`.

参考：[AGENTS.md:L51-L53](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/AGENTS.md#L51-L53)。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 ReductionBase 类与配置方法**——它抽象了什么，子类怎么定制。
- **4.2 tiled_copy 与 tiler_mn 推导**——配置如何变成「一块数据怎么搬」。
- **4.3 reduction buffer 与 mbarrier 分配**——跨 warp / 跨 CTA 归约的「中转站」如何搭起来。

### 4.1 ReductionBase 类与配置方法

#### 4.1.1 概念说明

归约内核（比如对一行 \( N \) 个元素求 max、求和、求平方和）有大量共性：

1. 这一行太长，需要很多线程一起读、一起算。
2. 读完后要把分散在各线程、各 warp、甚至各 CTA 里的部分结果**合并**成最终结果——这就是「归约」。
3. 合并需要一块临时存储（归约缓冲）和同步原语（mbarrier）。
4. 在 Hopper/Blackwell 上，可以让一个 cluster 里的多个 CTA 协作处理同一行，进一步加速。

`ReductionBase` 把这套「**配置线程与 cluster → 取 tiled copy → 分配归约缓冲与 mbarrier → 启动 kernel**」的流程固化成模板方法，子类（`Softmax`、`RMSNorm`、`CrossEntropy`、`TopK` 等）只需覆盖少数几个钩子来定制自己的并行度策略。

#### 4.1.2 核心流程

整体是典型的「**模板方法 + 钩子**」设计：

```text
ReductionBase（模板/共享骨架）
├─ __init__(dtype, N, stage, reduction_dtype)   # 记录基本参数
├─ _threads_per_row()        # 钩子：每行用多少线程（子类实现）
├─ _num_threads()            # 默认实现：N<=16384 用 128 线程，否则 256
├─ _set_cluster_n()          # 钩子：设 cluster_n（子类实现；默认=1）
├─ _cap_cluster_n(vecsize)   # 共享：运行时把 cluster_n 夹到安全上限
├─ _get_tiled_copy(vecsize)  # 共享：算 tiled_copy + tiler_mn
├─ _get_reduction_buffer_layout(...)   # 共享：归约缓冲布局
├─ _allocate_reduction_buffer_and_mbar(...)  # 共享：分配缓冲+mbar
└─ _initialize_cluster(...)  # 共享：初始化 mbarrier + cluster arrive

子类只需覆盖：_threads_per_row、_set_cluster_n（有时也覆盖 _get_tiled_copy）
子类自己的 __call__（@cute.jit）：调上面的共享方法 → 配 grid/block/cluster → launch kernel
```

每个内核的 `__call__` 大致都长这样（以 softmax 为例）：

1. `self._set_cluster_n()`——先按架构和 `N` 选一个 `cluster_n`。
2. `self._get_tiled_copy(vecsize=...)`——拿到 `tiled_copy`、`tiler_mn`、`threads_per_row`。
3. `self.kernel(...).launch(grid=..., block=..., cluster=..., stream=...)`——启动设备内核。
4. 在内核内部（`@cute.kernel`）才调 `_allocate_reduction_buffer_and_mbar` 和 `_initialize_cluster`。

> 注意：`_set_cluster_n` / `_cap_cluster_n` / `_get_tiled_copy` 是**主机侧（`@cute.jit` 的 `__call__` 里）**调的；而 `_allocate_reduction_buffer_and_mbar` / `_initialize_cluster` 是**设备侧（`@cute.kernel` 里）**调的，因为共享内存只能在内核里分配。

#### 4.1.3 源码精读

基类与构造函数只记录四个基本参数：

[quack/reduction_base.py:L12-L17](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduction_base.py#L12-L17) — `ReductionBase.__init__` 记录 `dtype`、行宽 `N`、`stage`（每个 barrier 同步的值个数）、`reduction_dtype`（归约缓冲用什么精度累加）。

```python
class ReductionBase:
    def __init__(self, dtype, N, stage, reduction_dtype=Float32):
        self.dtype = dtype
        self.N = N
        self.stage = stage
        self.reduction_dtype = reduction_dtype
```

两个钩子（默认实现）：

[quack/reduction_base.py:L19-L26](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduction_base.py#L19-L26) — `_threads_per_row` 抛 `NotImplementedError`（强制子类实现）；`_num_threads` 给默认值；`_set_cluster_n` 默认 `cluster_n=1`（不开 cluster）。

```python
def _threads_per_row(self):
    raise NotImplementedError()

def _num_threads(self):
    return 128 if self.N <= 16384 else 256

def _set_cluster_n(self):
    self.cluster_n = 1
```

子类如何定制钩子？看 softmax 的两个覆盖：

[quack/softmax.py:L37-L42](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L37-L42) — `_threads_per_row` 按 `N` 分档：`N` 越大，每行用越多线程（8→16→32→64→128→256）。

```python
def _threads_per_row(self):
    N = self.N
    for limit, threads in [(64, 8), (128, 16), (3072, 32), (6144, 64), (16384, 128)]:
        if N <= limit:
            return threads
    return 256
```

[quack/softmax.py:L44-L64](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L44-L64) — `_set_cluster_n` 按架构（SM8x 无 cluster；SM12x 上限 8；其余上限 16）和数据宽度，用一张阈值表把 `cluster_n` 设成 1/2/4/8/16。

最后看主机侧如何把这些配置串起来并启动：

[quack/softmax.py:L66-L85](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L66-L85) — `__call__`（`@cute.jit`）先 `_set_cluster_n()`，再 `_get_tiled_copy(vecsize=128 // largest_dtype_width)`，最后用 `tiler_mn[0]` 算 grid、用 `cluster_n` 算 cluster，启动内核。

```python
self._set_cluster_n()
largest_dtype_width = const_expr(max(t.element_type.width for t in [mX, mO]))
tiled_copy, tiler_mn, threads_per_row = self._get_tiled_copy(vecsize=128 // largest_dtype_width)
num_threads = tiled_copy.size
self.kernel(mX, mO, tiler_mn, tiled_copy, threads_per_row).launch(
    grid=[cute.ceil_div(mX.shape[0], tiler_mn[0]), self.cluster_n, 1],
    block=[num_threads, 1, 1],
    cluster=[1, self.cluster_n, 1] if const_expr(self.cluster_n > 1) else None,
    stream=stream,
)
```

> grid 第二维就是 `cluster_n`——每个 cluster 内的 peer CTA 沿 N 维切分同一行。`cluster=` 只在 `cluster_n > 1` 时传（否则 `None`），这正对应 u1-l4 讲过的「`const_expr` 分支只编入命中分支」。

#### 4.1.4 代码实践

**实践目标**：确认「基类提供共享骨架，子类只改钩子」这套结构。

**操作步骤**：

1. 打开 `quack/softmax.py`、`quack/rmsnorm.py`、`quack/cross_entropy.py`、`quack/topk.py`，分别找到它们继承 `ReductionBase` 的类定义。
2. 在每个类里数一下：它覆盖了 `_threads_per_row` / `_set_cluster_n` / `_get_tiled_copy` 中的哪几个？哪些方法它**完全没有**覆盖（直接用基类的）？

**需要观察的现象**：

- 四个内核都覆盖了 `_threads_per_row`（因为并行度策略各不相同）。
- `RMSNorm` 把 `threads_per_row` 从一个 `config` 对象里取（[quack/rmsnorm.py:L88-L95](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L88-L95)），而 `Softmax` 是写死的分档表——同样是覆盖钩子，但数据来源不同。
- `_get_tiled_copy` 几乎没人覆盖（`CrossEntropy` 和 `TopK` 各自重写了它，因为它们的 tile 形状需求不同，见 [quack/cross_entropy.py:L407-L411](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cross_entropy.py#L407-L411)）。

**预期结果**：你会看到「骨架共享、钩子定制」非常清晰——这正是基类存在的意义。

#### 4.1.5 小练习与答案

**练习 1**：`_num_threads` 为什么在 `N <= 16384` 时返回 128、否则返回 256？

**参考答案**：线程越多，每个线程分到的数据越少、归约的「跨线程合并」开销越大；但 `N` 很大时单 CTA 的线程太少会导致一个 CTA 要循环很多轮才能读完一行。16384 这个分界是经验性的权衡点：行宽不超过 16384 时 128 线程够用且归约更快，更宽的行才动用 256 线程。

**练习 2**：`_set_cluster_n` 是「主机侧」还是「设备侧」调用的？为什么它必须在 `@cute.jit` 的 `__call__` 里、而不是 `@cute.kernel` 里调？

**参考答案**：主机侧。因为 `cluster_n` 决定了 `launch` 时的 `grid` 第二维和 `cluster` 参数——这些是启动配置，必须在真正 launch 内核之前确定。`@cute.kernel` 是设备侧、内核已经启动之后才执行的，那时再改 `cluster_n` 已无意义。

---

### 4.2 tiled_copy 与 tiler_mn 推导

#### 4.2.1 概念说明

`_get_tiled_copy` 是基类最核心的共享方法之一。它回答一个问题：**「一行 N 个元素，怎么被一组线程协作地搬进共享内存 / 寄存器？」** 它产出三样东西：

- `tiled_copy`：一个 `TiledCopy` 对象，描述线程→数据的映射（哪个线程搬哪些元素、每次搬多少）。
- `tiler_mn`：一个二元组 `(tile_M, tile_N)`，表示一个 CTA 处理的「数据块」形状（沿 M=行方向多少行、沿 N=列方向多少元素）。
- `threads_per_row`：回传，供后续归约使用。

`tiler_mn` 是后面一切切片（`local_tile`）、grid 计算（`ceil_div(总行数, tiler_mn[0])`）和边界谓词的基础。

#### 4.2.2 核心流程

先看数学。给定行宽 `N`、向量化宽度 `vecsize`（每次搬 `vecsize` 个元素）、每行线程数 `threads_per_row`、cluster 大小 `cluster_n`：

1. 把一行切成「向量块」，共 \( N / \text{vecsize} \) 块。
2. 一个 cluster 沿 N 方向有 `cluster_n` 个 CTA，每个 CTA 有 `threads_per_row` 个线程负责 N 方向。所以一个 CTA 每趟能覆盖 `threads_per_row` 个向量块；整个 cluster 一趟覆盖 `threads_per_row * cluster_n` 个向量块。
3. 一个 CTA 需要走多少趟才能覆盖它负责的那一段？用上取整：

\[
\text{num\_blocks\_N} = \left\lceil \frac{N / \text{vecsize}}{\text{threads\_per\_row} \times \text{cluster\_n}} \right\rceil
\]

4. 于是这个 CTA 沿 N 方向负责的元素数为：

\[
\text{tiler\_mn}[1] = \text{vecsize} \times \text{num\_blocks\_N} \times \text{threads\_per\_row}
\]

而沿 M 方向，一个 CTA 负责的行数为「总线程数 ÷ 每行线程数」：

\[
\text{tiler\_mn}[0] = \text{num\_threads} / \text{threads\_per\_row}
\]

直觉上：`tiler_mn` 就是「一个 CTA 一口气处理的 (行数 × 列数) 数据块」。整个 cluster 沿 N 铺开 `cluster_n` 个这样的块，正好覆盖一行。

#### 4.2.3 源码精读

[quack/reduction_base.py:L42-L50](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduction_base.py#L42-L50) — `_get_tiled_copy` 完整实现，逐行对应上面的公式。

```python
def _get_tiled_copy(self, vecsize: int = 1):
    assert self.N % vecsize == 0, f"Input N {self.N} is not divisible by vector size {vecsize}"
    threads_per_row = self._threads_per_row()
    num_threads = self._num_threads()
    assert num_threads % cute.arch.WARP_SIZE == 0
    num_blocks_N = cute.ceil_div(self.N // vecsize, threads_per_row * self.cluster_n)
    tiler_mn = (num_threads // threads_per_row, vecsize * num_blocks_N * threads_per_row)
    tiled_copy = copy_utils.tiled_copy_2d(self.dtype, threads_per_row, num_threads, vecsize)
    return tiled_copy, tiler_mn, threads_per_row
```

注意开头那个 `assert N % vecsize == 0`。它要求 `vecsize` 必须整除 `N`。两个内核用不同策略保证这一点：

- **softmax**（[quack/softmax.py:L76-L78](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L76-L78)）：`vecsize = 128 // largest_dtype_width`（如 bf16→8），是固定值，依赖外部保证 `N` 是它的倍数。
- **rmsnorm**（[quack/rmsnorm.py:L126-L128](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L126-L128)）：`vecsize = gcd(N, 128 // largest_dtype_width)`，用最大公约数**主动**保证整除，更鲁棒。

`tiled_copy_2d` 又是怎么把线程布局搭起来的？

[quack/copy_utils.py:L364-L380](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/copy_utils.py#L364-L380) — 线程布局是 `(行数, 每行线程数)` 的二维布局，`每行线程数` 是连续（最快变化）的那一维；值布局是 `(1, vecsize)`，即每个线程一趟搬 `vecsize` 个连续元素。

```python
def tiled_copy_2d(dtype, threads_per_row, num_threads, num_copy_elems=1, is_async=False):
    num_copy_bits = num_copy_elems * dtype.width
    copy_op = cpasync.CopyG2SOp() if is_async else cute.nvgpu.CopyUniversalOp()
    copy_atom = cute.make_copy_atom(copy_op, dtype, num_bits_per_copy=num_copy_bits)
    assert num_threads % threads_per_row == 0
    thr_layout = cute.make_ordered_layout(
        (num_threads // threads_per_row, threads_per_row), order=(1, 0),
    )
    val_layout = cute.make_layout((1, num_copy_elems))
    return cute.make_tiled_copy_tv(copy_atom, thr_layout, val_layout)
```

读法：`thr_layout = (num_threads // threads_per_row, threads_per_row)`——把 `num_threads` 个线程排成「每行 `threads_per_row` 个、共若干行」的网格，相邻的线程 id 落在同一行（保证一个行内搬运是合并访存 coalesced）。`val_layout = (1, num_copy_elems)` 告诉硬件每个线程每次搬 `num_copy_elems` 个连续元素。

> 对照记忆：`tiler_mn`（数据块形状）和 `thr_layout`（线程网格形状）其实是**同构**的——`num_threads // threads_per_row` 既是线程网格的行数，也是 `tiler_mn[0]`（一个 CTA 处理的数据行数）。这不是巧合，而是「线程网格负责的数据块」这一设计的直接体现。

#### 4.2.4 代码实践（本讲主实践任务）

**实践目标**：读懂 `_cap_cluster_n` 为什么存在，并用具体数字推算 `cluster_n` 上限。这是本讲指定的实践任务。

**操作步骤**：

1. 阅读基类的 `_cap_cluster_n` 及其 docstring：[quack/reduction_base.py:L28-L40](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduction_base.py#L28-L40)。

   ```python
   def _cap_cluster_n(self, vecsize: int) -> None:
       """Cap cluster_n so every peer CTA owns a distinct, non-empty N-tile.
       ...（详见下方解释）"""
       max_cluster_n = max(1, (self.N // vecsize) // self._threads_per_row())
       self.cluster_n = min(self.cluster_n, max_cluster_n)
   ```

2. 理解它要避免的故障：当一个 CTA 的 N-tile 已经**整行宽**（`tiler_mn[1] >= N`）时，`local_tile(mT, tiler_mn, (bidx, cluster_y))` 在 N 维只有「第 0 块」有意义，于是 cluster 里所有 `cluster_y` 都被折叠到同一块数据上——每个 peer 都把**同一整行**再归约一遍，结果在 cluster 归约时被求和 `cluster_n` 次，导致重复计数（例如 rstd 被多除以 \(\sqrt{\text{cluster\_n}}\)）。

3. 夹紧公式保证：令 `cluster_n <= (N // vecsize) // threads_per_row`，即 `threads_per_row * cluster_n <= N // vecsize`，这样 `num_blocks_N = ceil_div(...)` 会让每个 peer 拿到一段**严格小于整行**的、互不重叠的 N-tile（只要夹紧后 `cluster_n > 1`，就一定有 `tiler_mn[1] < N`）。

4. **用一组具体 `N` 值手算 `cluster_n` 上限**（取 `vecsize = 8`，即 bf16 的典型值）：

| `N` | `N // vecsize`（向量块数） | `threads_per_row = 32` 时上限 | `threads_per_row = 64` 时上限 |
| --- | --- | --- | --- |
| 256 | 32 | \( \lfloor 32/32 \rfloor = 1 \) | \( \lfloor 32/64 \rfloor = 0 \to 1 \) |
| 1024 | 128 | \( \lfloor 128/32 \rfloor = 4 \) | \( \lfloor 128/64 \rfloor = 2 \) |
| 4096 | 512 | \( \lfloor 512/32 \rfloor = 16 \) | \( \lfloor 512/64 \rfloor = 8 \) |
| 8192 | 1024 | \( \lfloor 1024/32 \rfloor = 32 \) | \( \lfloor 1024/64 \rfloor = 16 \) |

   表里的 `-> 1` 表示 `max(1, 0)` 把负/零结果兜底为 1（不开 cluster）。

5. 对照真实回归测试核对：[tests/test_rmsnorm.py:L645-L673](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_rmsnorm.py#L645-L673) 里的 `test_rmsnorm_degenerate_cluster_config_is_clamped` 正是用 `M=N=256, vecsize=8, cluster_n=2, threads_per_row∈{32,64}` 这个「退化」配置，验证 `_cap_cluster_n` 把 `cluster_n` 夹回 1 后前向/反向数值仍正确。

**需要观察的现象**：

- `N=256` 这一行：无论 `threads_per_row` 是 32 还是 64，上限都是 1。这正是测试里 `cluster_n=2` 会退化、必须被夹回 1 的原因。
- `N` 越大、`threads_per_row` 越小，能开的 `cluster_n` 越大——和直觉一致（行越宽、每行线程越少，越有空间让多个 CTA 分担）。

**预期结果**：你能用自己的话解释——「`_cap_cluster_n` 是一道运行时保险：当配置的 `cluster_n` 大到让单个 CTA 的 N-tile 已经盖住整行时，把它夹回 `(N//vecsize)//threads_per_row`，保证每个 peer CTA 分到一段非空且互不重叠的列，避免 cluster 归约时重复计数。」

> 关于「是否真的会退化」：**待本地验证**——你可以在装好 GPU 的环境里跑 `pytest tests/test_rmsnorm.py::test_rmsnorm_degenerate_cluster_config_is_clamped -x`，观察它通过（即夹紧后正确）。源码阅读本身不需要运行即可完成上表的手算。

#### 4.2.5 小练习与答案

**练习 1**：`softmax` 的 `__call__` **没有**调用 `_cap_cluster_n`（对比 rmsnorm 在 [quack/rmsnorm.py:L127](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L127) 调了）。为什么 softmax 可以不调？

**参考答案**：softmax 的 `vecsize = 128 // width` 是固定值（bf16 时为 8），且其 `_set_cluster_n` 的阈值表是按这个较大的 `vecsize` 保守设计的；同时 softmax 的 `threads_per_row` 分档也较粗。rmsnorm 则用 `vecsize = gcd(N, 128//width)`，`vecsize` 会随 `N` 变小（甚至可能很小），更容易触发退化，所以必须额外调 `_cap_cluster_n` 兜底。两种策略都合法，只是 rmsnorm 选择了更鲁棒、对小 `vecsize` 更安全的路径。

**练习 2**：若 `N=4096, vecsize=8, threads_per_row=32`，`cluster_n` 上限是 16。此时一个 CTA 的 `tiler_mn[1]` 是多少？整个 cluster 是否正好覆盖一行？

**参考答案**：`num_blocks_N = ceil_div(4096/8, 32*16) = ceil_div(512, 512) = 1`，所以 `tiler_mn[1] = 8 * 1 * 32 = 256`。整个 cluster 覆盖 `cluster_n * tiler_mn[1] = 16 * 256 = 4096 = N`，正好覆盖一行，且每个 peer 的 256 元素段互不重叠。✓

---

### 4.3 reduction buffer 与 mbarrier 分配

#### 4.3.1 概念说明

归约不是「每个线程算完就结束」。一行 `N` 个元素被 `threads_per_row` 个线程读完后，每个线程手里只有**部分和**；这些部分和要先在 warp 内合并（warp_reduce），再在「协作于同一行的多个 warp」之间合并，最后（如果开了 cluster）在 cluster 的多个 peer CTA 之间合并。

跨 warp / 跨 CTA 合并不能只用寄存器里的 shuffle，需要一个**共享内存中转站**——这就是 reduction buffer。而 cluster 内多个 CTA 的同步要用 **mbarrier**（内存屏障）。`ReductionBase` 提供了三个共享方法来搭这个中转站：

- `_get_reduction_buffer_layout`：算缓冲的布局。
- `_allocate_reduction_buffer_and_mbar`：在 smem 里真正分配缓冲 + mbarrier 数组。
- `_initialize_cluster`：初始化 mbarrier 并发 cluster arrive。

#### 4.3.2 核心流程

缓冲布局是三维的 `(rows, (warps_per_row, cluster_n), num_slots)`：

```text
reduction_buffer[(rows), (warps_per_row, cluster_n), (num_slots)]
                   ↑       ↑                ↑             ↑
              并行的行数  同一行协作的 warp 数  peer CTA 数   每个 barrier 同步几个值
```

- **dim 0 `rows = num_warps // warps_per_row`**：一个 CTA 同时归约多少个独立的行。`num_warps = num_threads / 32`。
- **dim 1 `(warps_per_row, cluster_n)`**：协作于同一行的 warp 数（`warps_per_row ≈ threads_per_row // 32`，至少为 1）× cluster 内 peer CTA 数。这俩打包在一起，是因为它们都要参与「同一行的最终合并」。
- **dim 2 `num_slots = stage`**：一个 barrier 一次同步几个值。比如非 online softmax 要先求 max、再求 sum，就是 2 个 slot；online softmax 把 max 和 sum 打包进一个 Int64，就是 1 个 slot（但仍可能用 override 让 slot 数与 stage 不同）。

`order=(1, 0, 2)` 指定维度的优先级（哪一维在内存里连续），保证合并时访问是高效的。

mbarrier 数组：**只有 `cluster_n > 1` 时才分配**，长度为 `stage`（每个 slot 一个 barrier），类型 `Int64`。`cluster_n == 1` 时返回 `None`——因为单 CTA 内用普通的 `barrier()` 就够了，不需要 cluster 级 mbarrier。

#### 4.3.3 源码精读

[quack/reduction_base.py:L52-L68](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduction_base.py#L52-L68) — `_get_reduction_buffer_layout`：从 `tiled_copy` 的线程-值布局 `tv_layout` 推出 `num_warps` 和 `warps_per_row`，再拼出三维布局。

```python
def _get_reduction_buffer_layout(self, tv_layout, cluster_n, num_slots=None):
    num_warps = cute.size(tv_layout, mode=[0]) // cute.arch.WARP_SIZE
    warps_per_row = (
        num_warps
        if cute.rank(tv_layout.shape[0]) == 1
        else max(tv_layout.shape[0][0] // cute.arch.WARP_SIZE, 1)
    )
    if num_slots is None:
        num_slots = self.stage
    return cute.make_ordered_layout(
        (num_warps // warps_per_row, (warps_per_row, cluster_n), num_slots),
        order=(1, 0, 2),
    )
```

读法：

- `tv_layout` 是 `tiled_copy.layout_tv_tiled`（见 [quack/softmax.py:L96](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L96)），它的第 0 维（`mode=[0]`）是线程布局。
- 当线程布局是分层的（2D，对应 `tiled_copy_2d`）时，`warps_per_row = max(threads_per_row // 32, 1)`——即「每行有几个 warp 协作」；当线程布局是扁平的（1D，对应 `tiled_copy_1d`）时，所有 warp 都算作同一行。
- `num_slots` 默认取 `self.stage`，但 docstring 说明可以被覆盖（当一个 barrier 实际同步的值个数与 `stage` 不同时）。

[quack/reduction_base.py:L70-L85](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduction_base.py#L70-L85) — `_allocate_reduction_buffer_and_mbar`：在 smem 里分配缓冲（`reduction_dtype` 精度，8 字节对齐），并**仅当 `cluster_n > 1`** 时分配长度为 `stage` 的 `Int64` mbarrier 数组。

```python
def _allocate_reduction_buffer_and_mbar(self, smem, tv_layout):
    """Single-shot (non-persistent) reduction: full barriers only. ..."""
    reduction_buffer = smem.allocate_tensor(
        self.reduction_dtype,
        self._get_reduction_buffer_layout(tv_layout, self.cluster_n),
        byte_alignment=8,
    )
    if const_expr(self.cluster_n > 1):
        mbar_ptr = smem.allocate_array(Int64, num_elems=self.stage)
    else:
        mbar_ptr = None
    return reduction_buffer, mbar_ptr
```

注意 docstring 里的关键信息：这是「单次（非持久化）归约」用的 **full barrier**；持久化内核用的是 `quack.pipeline.PipelineStasAsync`（它会额外管理 empty barrier 以在多次迭代间复用缓冲）。本讲的归约内核都是单次的。

[quack/reduction_base.py:L87-L95](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduction_base.py#L87-L95) — `_initialize_cluster`：仅 `cluster_n > 1` 时执行——用前 `stage` 个线程初始化各 mbarrier（`mbarrier_init(..., 1)`），发 fence，再 `cluster_arrive_relaxed()` 让 cluster 内所有 CTA 在此汇合。

```python
@cute.jit
def _initialize_cluster(self, tidx, mbar_ptr, num_warps):
    if const_expr(self.cluster_n > 1):
        if tidx < self.stage:  # Initialize full barrier
            cute.arch.mbarrier_init(mbar_ptr + tidx, 1)
        cute.arch.mbarrier_init_fence()
        # Cluster arrive after barrier init
        cute.arch.cluster_arrive_relaxed()
```

最后，这块缓冲和 mbarrier 是怎么被**消费**的？以 `row_reduce` 为例（详细协议是 u2-l4 的主题，这里只看衔接）：

[quack/reduce.py:L245-L267](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L245-L267) — `row_reduce` 从缓冲 shape 读出 `warps_per_row, cluster_n = bufs[0].shape[1]`，并断言「`cluster_n > 1` 时必须传 `mbar_ptr`」，然后在 `warps_per_row > 1 or cluster_n > 1` 时调 `block_or_cluster_reduce` 做跨 warp / 跨 CTA 合并。

```python
warps_per_row, cluster_n = bufs[0].shape[1]
assert cluster_n == 1 or mbar_ptr is not None, (
    "mbar_ptr must be provided for cluster reduction"
)
if const_expr(warps_per_row > 1 or cluster_n > 1):
    ...
    reduced = block_or_cluster_reduce(..., reduction_buffer, mbar_ptr, ...)
```

这条断言正好印证了 4.3.1 的结论：**mbarrier 只在 cluster 归约时需要**；单 CTA 内的跨 warp 归约走的是 smem + 普通 `barrier()`，不需要 mbarrier。

#### 4.3.4 代码实践

**实践目标**：把「配置 → 缓冲布局 → 消费」这条链路在源码里走一遍。

**操作步骤**：

1. 在 `quack/softmax.py` 的 `kernel` 里定位缓冲分配调用：[quack/softmax.py:L111](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L111)——`reduction_buffer, mbar_ptr = self._allocate_reduction_buffer_and_mbar(smem, tv_layout)`。
2. 紧接着找 `_initialize_cluster` 调用：[quack/softmax.py:L131](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L131)——`self._initialize_cluster(tidx, mbar_ptr, num_warps)`。
3. 再往后找到 `online_softmax_reduce(...)` 或 `row_reduce(...)` 的调用，确认它把 `reduction_buffer` 和 `mbar_ptr` 原样传了进去。

**需要观察的现象**：

- `tv_layout = tiled_copy.layout_tv_tiled` 是分配缓冲的输入——**缓冲布局完全由 tiled copy 的线程布局决定**，二者严格对应。
- 当 `cluster_n == 1` 时，`mbar_ptr` 是 `None`，`_initialize_cluster` 整个 `if` 块在编译期被剔除（u1-l4 讲过的 `const_expr` 特化）。

**预期结果**：你能画出一条调用链 `_get_tiled_copy → tv_layout → _get_reduction_buffer_layout → _allocate_reduction_buffer_and_mbar → _initialize_cluster → row_reduce/online_softmax_reduce`，并解释每一步的输入输出。如果某一步在 `cluster_n==1` 时被省略，也说得出来。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_allocate_reduction_buffer_and_mbar` 在 `cluster_n == 1` 时返回 `mbar_ptr = None`，而不是返回一个用不到的 mbarrier？

**参考答案**：共享内存是稀缺资源，mbarrier 也占 smem。`cluster_n == 1` 时没有跨 CTA 同步需求，单 CTA 内用 `barrier()`（线程块屏障）即可，省下 `stage` 个 `Int64` 的 smem。同时，`const_expr(self.cluster_n > 1)` 让这整段分配代码在编译期就被剔除——`cluster_n==1` 的 cubin 里根本不存在 mbarrier 相关指令。

**练习 2**：缓冲布局第二维为什么把 `warps_per_row` 和 `cluster_n` **打包**成一个嵌套模式 `(warps_per_row, cluster_n)`，而不是分成两个独立维度？

**参考答案**：因为「同一行的多个协作 warp」和「cluster 内的多个 peer CTA」参与的是**同一次最终合并**——它们各自贡献的部分和要被加到一起。把它们打包在同一个模式里，配合 `order=(1,0,2)`，可以让合并时对这一维的访问是连续的、且能复用同一套蝶形 / mbarrier 协议（见 u2-l4 的 `block_or_cluster_reduce`）。从概念上，它们是「同一行的不同贡献者」，所以理应在同一维度。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「**给定的归约配置，推算全部启动参数与缓冲**」的纸上推演。

**任务**：假设要写一个新的归约内核 `MyReduce(ReductionBase)`，目标硬件是 Hopper（SM90），数据类型 bf16（width=16），行宽 `N = 8192`，`online` 模式（因此 `stage = 1`，`reduction_dtype = Int64`）。你决定 `threads_per_row = 64`、`cluster_n` 先由 `_set_cluster_n` 设到 8，`vecsize = gcd(8192, 128//16) = gcd(8192, 8) = 8`。

请推算并回答：

1. `_num_threads()` 返回什么？（提示：`N <= 16384`？）
2. `_cap_cluster_n(8)` 之后 `cluster_n` 变成多少？（用本讲 4.2.4 的公式）
3. `_get_tiled_copy(8)` 返回的 `tiler_mn` 是什么？`tiled_copy.size`（即 `num_threads`）是多少？
4. `_get_reduction_buffer_layout` 给出的布局三维各是多少？（设 `num_warps = num_threads/32`，`warps_per_row = max(threads_per_row//32, 1)`）
5. 这次会分配 mbarrier 数组吗？长度是多少？
6. 用你的答案写出 `launch` 的 `grid`、`block`、`cluster` 三个参数。

**参考答案**：

1. `N = 8192 <= 16384`，所以 `_num_threads() = 128`。
2. `max_cluster_n = max(1, (8192//8)//64) = max(1, 1024//64) = 16`；`min(8, 16) = 8`，故 `cluster_n` 仍为 **8**（未被夹紧）。
3. `num_blocks_N = ceil_div(8192//8, 64*8) = ceil_div(1024, 512) = 2`；`tiler_mn = (128//64, 8*2*64) = (2, 1024)`。`tiled_copy.size = 128`。
4. `num_warps = 128/32 = 4`；`warps_per_row = max(64//32, 1) = 2`；`rows = 4//2 = 2`；`num_slots = stage = 1`。布局 = `(2, (2, 8), 1)`。
5. `cluster_n = 8 > 1`，**会**分配 mbarrier 数组，长度 = `stage = 1`。
6. `grid = [ceil_div(总行数, tiler_mn[0]=2), cluster_n=8, 1]`；`block = [128, 1, 1]`；`cluster = [1, 8, 1]`。

> 推演无需 GPU 即可完成。若你想在真实内核上核对思路，可以在阅读完 u2-l2（softmax 内核逐行解读）后，回到 `quack/softmax.py` 的 `__call__`，把上面 6 个值代入对应的变量，确认逻辑自洽——这一步**待本地验证**。

## 6. 本讲小结

- `ReductionBase` 是 rmsnorm / softmax / cross_entropy / topk 等归约内核的**共享骨架**，用「模板方法 + 钩子」固化了「配置 cluster → 取 tiled copy → 分配归约缓冲与 mbarrier → 启动 kernel」的流程；子类通常只覆盖 `_threads_per_row` 和 `_set_cluster_n`。
- `_get_tiled_copy` 由 `N`、`vecsize`、`threads_per_row`、`cluster_n` 算出 `tiler_mn = (num_threads // threads_per_row, vecsize * num_blocks_N * threads_per_row)`，其中 `num_blocks_N = ceil_div(N//vecsize, threads_per_row * cluster_n)`；`tiler_mn` 既描述数据块形状，也决定 grid 与切片。
- `_cap_cluster_n` 是一道**运行时保险**：当配置的 `cluster_n` 大到让单个 CTA 的 N-tile 盖住整行（`tiler_mn[1] >= N`）时，把它夹回 `(N//vecsize)//threads_per_row`，避免 cluster 归约时 peer CTA 折叠到同一块而重复计数。
- 归约缓冲布局是三维 `(rows, (warps_per_row, cluster_n), num_slots)`：并行行数 ×（同行协作 warp × peer CTA）× 每个 barrier 同步的值个数；它完全由 `tiled_copy` 的线程布局决定。
- mbarrier 数组**只在 `cluster_n > 1` 时分配**（长度 `stage`），并在 `_initialize_cluster` 里初始化 + cluster arrive；单 CTA 归约走 smem + 普通 `barrier()`，不需要 mbarrier。
- `_set_cluster_n` / `_cap_cluster_n` / `_get_tiled_copy` 是**主机侧**（`@cute.jit`）调用，而 `_allocate_reduction_buffer_and_mbar` / `_initialize_cluster` 是**设备侧**（`@cute.kernel`）调用，因为 smem 只能在内核里分配。

## 7. 下一步学习建议

本讲只讲了**共享骨架与配置**，还没有进入任何具体内核的设备侧主循环。建议按以下顺序继续：

1. **u2-l2《Softmax 前向内核逐行解读》**：拿本讲推出的 `tiler_mn`、`tiled_copy`、`reduction_buffer`、`mbar_ptr`，去看 `Softmax.kernel` 内部是怎么用它们做 gmem→smem→register 拷贝、边界谓词、online softmax 两阶段归约的。这是把本讲「零件」组装成「整机」的过程。
2. **u2-l4《归约原语：warp/row/online》**：深入 `reduce.py` 的 `warp_reduce` / `row_reduce` / `online_softmax_reduce`，搞清楚本讲 4.3 里「缓冲和 mbarrier 是怎么被消费的」——即 `block_or_cluster_reduce` 的蝶形协议与跨 CTA mbarrier 同步细节。
3. **u3-l1《copy_utils 拷贝工具》**：系统了解 `tiled_copy_2d` / `tiled_copy_1d`、异步 cp.async 与 TMA 拷贝、谓词 `predicate_k` 等被本讲反复引用的拷贝原语。

读完 u2-l2，你就能独立看懂 rmsnorm / cross_entropy 的内核，因为它们共享同一套 `ReductionBase` 骨架。
