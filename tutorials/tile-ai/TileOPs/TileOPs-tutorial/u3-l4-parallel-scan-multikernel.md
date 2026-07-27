# 并行扫描的多 Kernel 分解（Cumsum 自适应）

## 1. 本讲目标

本讲是 Kernel 层的进阶篇。在 u3-l2（第一个 TileLang Kernel：RMSNorm）和 u3-l3（自动调优与数值稳定性）之后，本讲要回答一个新问题：

**当一个 TileLang kernel 在某些形状下“跑不满” GPU 时，如何把它拆成多个 kernel 协作，把顺序依赖局部化，从而把吞吐量提上去？**

学完后你应当能够：

- 解释 **形状驱动的 kernel 分发**：为什么 `M < 128 且 N > 8192` 的 cumsum 要走一条与其它形状不同的路径。
- 读懂 **local-scan → carry-scan → propagate** 三阶段并行扫描，并说出每个 `T.Kernel` 的网格、shared memory 用途，以及顺序依赖被“局部化”到哪一步。
- 解释 **cumprod 为什么没有并行实现**，以及 `tile_sums` / `tile_carries` 这两个 fp32 中间量各自承担的角色。
- 理解 Op 层与 Kernel 层的职责切分：Op 对“并行还是顺序”一无所知，选择完全发生在 Kernel 构造期。

## 2. 前置知识

本讲默认你已经掌握 u3-l1/u3-l2/u3-l3 的内容。下面只用通俗语言补三个本讲直接用到的新概念。

### 2.1 前缀和（inclusive prefix sum）

对一个一维序列 \(x_0, x_1, \dots, x_{N-1}\)，它的**包含型前缀和**定义为：

\[
y_j = \sum_{k=0}^{j} x_k, \qquad j = 0, 1, \dots, N-1
\]

关键性质：\(y_j\) 依赖 \(y_{j-1}\)，\(y_{j-1}\) 又依赖 \(y_{j-2}\)……这是一条**沿 j 的顺序依赖链**。正是这条链让前缀和“看起来”只能串行算。cumprod 同理，只是把求和换成求积。

### 2.2 SM 占用率（SM utilization）

GPU 由若干个 SM（Streaming Multiprocessor）组成。一个 TileLang kernel 启动若干个**线程块（thread block）**，GPU 把这些块分发到各个 SM 上并行执行。如果块数太少（例如只有几个），大部分 SM 会空闲——这就是“跑不满”。本讲要解决的问题，正是“**行数 M 很少、列数 N 很大**”时块数太少导致跑不满。

### 2.3 分块与 carry（tile-and-carry）

把长度 N 的序列切成 `n_tiles` 个大小为 `block_n` 的**块（tile）**。如果能先在每个块内部独立算前缀和，再想办法把“块与块之间的累积量”补回去，就能让所有块**同时算**——这就是 carry（进位）的思想。本讲的三阶段并行扫描就是它的 GPU 落地。

> 名词速查：本讲反复出现的 `(M, N)` 是 Op 层把任意维输入 `movedim + reshape` 后的二维形状——M 是“所有非扫描维的乘积”（行数），N 是扫描维（列数）。详见 4.1。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [tileops/kernels/reduction/cumulative.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/reduction/cumulative.py) | 本讲主角。含顺序 kernel `_cumulative_kernel`、三个并行 kernel（`_parallel_scan_local_kernel` / `_parallel_scan_carry_kernel` / `_parallel_scan_propagate_kernel`）、两个 `custom_op` 包装、以及 `CumulativeKernel` 类（构造期决定走并行还是顺序）。 |
| [tileops/ops/reduction/cumulative.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/reduction/cumulative.py) | Op 层。`CumulativeOp` 基类做 `movedim`/`reshape`/调用 kernel/裁剪 `N_padded`；`CumsumFwdOp` / `CumprodFwdOp` 两个子类只差一个 `_op_kind` 字符串。注意：Op 层对“并行/顺序”完全无感。 |
| [tests/ops/test_cumulative.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/ops/test_cumulative.py) | 测试。其中 `test_cumsum_backend_dispatch`、`test_cumsum_parallel_scan_row_ownership` 是本讲最重要的“可执行证据”。 |
| [tileops/manifest/scan.yaml](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/manifest/scan.yaml) | 规约。`CumsumFwdOp` / `CumprodFwdOp` 的接口真相来源。**注意它的 family 是 `scan`，不是 `reduction`**——代码物理路径在 `reduction/` 下，逻辑家族却在 `scan`，这是“family 与文件路径不 1:1”的活样本（承接 u1-l3）。 |
| [tileops/kernels/reduction/_primitives.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/reduction/_primitives.py) | 共享原语：`DEFAULT_ALIGNMENT=256`、`align_up`、`SHARED_MEMORY_BUDGET_BYTES=48KiB`。本讲的 padding、对齐、shared memory 预算都来自这里。 |

## 4. 核心概念与源码讲解

### 4.1 形状驱动的 kernel 分发

#### 4.1.1 概念说明

cumsum 的 Op 层把任意维输入拍平成 `(M, N)` 后调用 kernel（详见 4.1.3 的 `_run`）。对 `(M, N)`：

- **M 很大**时（例如 `(2048, 4096)`），按 `block_m` 切行会产生很多线程块，GPU 已经跑满了，**顺序扫描就够好**。
- **M 很小、N 很大**时（例如 `(64, 32768)`），按 `block_m` 切行只产生很少的块，大量 SM 闲置；而 N 很大意味着“可切的块数很多”——这正是并行扫描能大显身手的形状。

所以 TileOPs 给 cumsum 做了**形状驱动的自适应分发**：满足 `M < 128 且 N > 8192` 且 `op_kind == "sum"` 时走三阶段并行扫描，否则走顺序扫描。这个判断**完全发生在 Kernel 构造期**，Op 层不参与。

#### 4.1.2 核心流程

```text
Op.forward(x)
  └─ _run(x): movedim/reshape → (M, N) ──┐
                                         │ 对“并行/顺序”无感
                                         ▼
        _get_kernel(M, N, dtype, dev)  →  CumulativeKernel(M, N, op_kind, ...)
                                          │
                            ┌─────────────┴──────────────┐
                            ▼ (构造期判定)               ▼
            use_parallel = True                    use_parallel = False
            (M<128 且 N>8192 且 sum)               (其余形状 / cumprod)
                            │                            │
                  self.kernel = None          self.kernel = _cumulative_kernel(...)
                  forward → _cumulative       forward → _cumulative_fwd_wrapped
                            _parallel_fwd                    (单 kernel 顺序扫描)
                            _wrapped
```

判据本身只有一行（见 4.1.3）。阈值 `128` 与 `8192` 是经验值：低于它们时并行扫描的额外开销（多一次 global 读写 + carry 扫描）抵消不掉收益。

#### 4.1.3 源码精读

**判据在构造期写入 `self.use_parallel`**：

[tileops/kernels/reduction/cumulative.py:346-369](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/reduction/cumulative.py#L346-L369) —— 关键是第 354 行：

```python
# Parallel scan only pays off for small-M, large-N; cumprod has no
# parallel implementation.
self.use_parallel = (M < 128 and N > 8192 and op_kind == "sum")
```

注意三件事：

1. **`op_kind == "sum"` 把 cumprod 直接排除**——所以 cumprod 永远走顺序（4.3 详述）。
2. 走并行时 `self.kernel = None`：并行后端不是一个 TileLang kernel，而是三个 kernel 的组合，无法像顺序那样缓存一个 `_func`。
3. 并行后端**显式禁用 autotune**并发 `UserWarning`（第 358-365 行）。原因在 4.2 讲：并行流水是固定三段，没有“单一 tile 配置”可供 autotune 扫描。

**默认配置也按后端分叉**。[tileops/kernels/reduction/cumulative.py:371-394](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/reduction/cumulative.py#L371-L394)：

```python
if self.use_parallel:
    block_n = 256 if self.N > 16384 else 128
    smem_per_row = (block_n + _SMEM_PAD) * 4   # fp32 中间量，每元素 4 字节
    max_block_m = SHARED_MEMORY_BUDGET_BYTES // smem_per_row
    block_m = max(1, min(16, self.M, max_block_m))
    return {"block_m": block_m, "block_n": block_n, "threads": 256}
```

并行后端用 fp32 中间量（`* 4`），而顺序后端用存储 dtype（`2 * ... * elem_size`，两个 shared 缓冲）。这是 u3-l3 “fp16/bf16 提升到 fp32、边界再 cast”规则在本算子的体现。

**Op 层对这一切完全无感**。[tileops/ops/reduction/cumulative.py:77-90](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/reduction/cumulative.py#L77-L90) 的 `_get_kernel` 只按 `(M, N, dtype, device_index)` 缓存并构造 `CumulativeKernel`，从不读 `use_parallel`：

```python
key = (M, N, dtype, device_index)
if key not in self._kernel_cache:
    self._kernel_cache[key] = self.kernel_map["cumulative_fwd"](
        M, N, self._op_kind, dtype, tune=self.tune,
    )
```

而 `_run` 在拍平 `(M, N)` 后直接 `y = self._get_kernel(M, N, dtype, x.device.index)(x)`（[第 130 行](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/reduction/cumulative.py#L130)）。**“选哪条路径”是 Kernel 的私事**，这正是 Op/Kernel 双层分离（u1-l1、u2-l1）在 cumsum 上的具体收益：加并行后端不需要动 Op 层一行代码。

#### 4.1.4 代码实践

**实践目标**：验证“形状→后端”的判定边界。

**操作步骤**（需 CUDA GPU；无 GPU 时改用下方的“源码阅读型实践”）：

1. 写一段脚本，对若干 `(M, N, dtype)` 构造 `CumsumFwdOp`，跑一次 forward，再读 `kernel.use_parallel`：

```python
# 示例代码：验证 backend 判定边界
import torch
from tileops.ops.reduction.cumulative import CumsumFwdOp

cases = [
    (64, 16384, torch.float32),   # 期望 parallel
    (64, 32768, torch.bfloat16),  # 期望 parallel（block_n=256）
    (64, 8192,  torch.bfloat16),  # N 边界：期望 sequential
    (128, 16384, torch.bfloat16), # M 边界：期望 sequential
]
for M, N, dt in cases:
    op = CumsumFwdOp(dtype=dt, dim=-1)
    x = torch.randn(M, N, dtype=dt, device="cuda")
    op(x)
    k = op._get_kernel(M, N, dt, x.device.index)
    print(f"({M},{N}) use_parallel={k.use_parallel}, block_n={k.config['block_n']}")
```

2. 对照 [tests/ops/test_cumulative.py:313-340](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/ops/test_cumulative.py#L313-L340) 的 `test_cumsum_backend_dispatch`，它用 `assert kernel.use_parallel == parallel` 把每个形状的期望后端固化成了断言。

**需要观察的现象**：`use_parallel` 仅在 `M < 128 且 N > 8192` 时为 `True`；并行后端 `block_n` 在 `N > 16384` 时为 256，否则 128。

**预期结果**：与测试断言一致。若无 GPU，**待本地验证**；可改做源码阅读型实践——在 `_cumulative_parallel_fwd_wrapped`（4.2.3）和判据行之间画一条调用链，标出每个 case 命中哪条分支。

#### 4.1.5 小练习与答案

**练习 1**：把 `(64, 8192, bfloat16)` 改成 `(64, 8193, bfloat16)`，`use_parallel` 会从 False 变 True 吗？

> **答案**：会。判据是 `N > 8192`，`8193 > 8192` 命中并行（只要 `M < 128` 且 `sum`）。`test_cumsum_backend_dispatch` 里 `(64, 8200, fp32)` 正是验证“N 非 block_n 整数倍”仍走并行、靠 masked tail 处理。

**练习 2**：为什么判据要同时要求 `M < 128` 和 `N > 8192`，缺一不可？

> **答案**：`M` 大时按行切块已能产生足够多的线程块跑满 SM，并行扫描的额外 global 读写反成负担；`N` 小时 `n_tiles` 太少，并行扫描拆不出足够的并行度。两个条件同时满足才是“顺序后端跑不满、且并行后端有足够 tile 可切”的甜区。

---

### 4.2 三阶段并行扫描

这是本讲的核心。本节解决：如何把一条长度 N 的顺序前缀和链，拆成三个 kernel，让绝大部分工作可以跨 tile 并行。

#### 4.2.1 概念说明

设 \(N = \text{n\_tiles} \times B\)（\(B\) = `block_n`）。第 \(t\) 个 tile 覆盖列 \([tB,\ (t+1)B)\)。

**第 1 步 local-scan**：每个 tile **内部**独立算包含型前缀和，并记下该 tile 的总和：

\[
y^{\text{loc}}_{t,j} = \sum_{k=0}^{j} x_{tB+k},\qquad
s_t = \sum_{k=0}^{B-1} x_{tB+k} = y^{\text{loc}}_{t,B-1}
\]

注意 \(y^{\text{loc}}\) 还**不是**最终结果——它缺了“前面所有 tile 的累积量”。

**第 2 步 carry-scan**：对 tile 总和序列 \(s_0, s_1, \dots\) 沿 tile 维做**排他型**前缀和，得到每个 tile 的“进位”：

\[
c_0 = 0,\qquad c_t = \sum_{k=0}^{t-1} s_k \quad (t \ge 1)
\]

**第 3 步 propagate**：把进位补回 local 结果：

\[
y_{t,j} = y^{\text{loc}}_{t,j} + c_t
\]

可证这正是全局包含型前缀和。三步合起来把“长度 N 的串行链”换成了：

- 步 1、步 3：在 `ceildiv(M, block_m) × n_tiles` 的二维网格上**全并行**；
- 步 2：只剩 `n_tiles`（128~256）长度的串行扫描，由“一行一线程”极廉价地完成。

顺序依赖从“长度 N”压缩到了“长度 n_tiles”——这就是并行扫描的收益来源。

#### 4.2.2 核心流程

数据流（圆角为 kernel，方框为张量）：

```text
                x  (M, N)
                │
   ┌────────────┴─────────────┐
   │ Pass 1: local-scan       │  grid = (ceildiv(M,block_m), n_tiles)
   │ _parallel_scan_local_     │  smem: tile_shared (block_m, block_n+PAD)
   │   kernel                  │  frag : tile_frag (block_m, block_n) fp32
   │   T.cumsum(tile_frag,1)   │
   └────────────┬─────────────┘
        ┌───────┴───────┐
        ▼               ▼
   y_local          tile_sums        ← 两者都是 fp32
  (M, N_padded)      (M, n_tiles)       y_local=tile内含型前缀和
   float32            float32           tile_sums[r,t]=该 tile 总和
                          │
   ┌──────────────────────┴───────────┐
   │ Pass 2: carry-scan               │  grid = (ceildiv(M, threads))
   │ _parallel_scan_carry_kernel       │  无 smem：一线程一行，串行扫 n_tiles
   │   tile_carries[r,0]=0;           │
   │   running += tile_sums[r,j]      │
   └──────────────────────┬───────────┘
                          ▼
                   tile_carries           ← 排他型前缀和（进位），fp32
                   (M, n_tiles)              tile_carries[r,t]=c_t
                          │
   ┌──────────────────────┴───────────┐
   │ Pass 3: propagate                │  grid = (ceildiv(M,block_m), n_tiles)
   │ _parallel_scan_propagate_kernel   │  无 smem：直接 global→global
   │   y = cast(y_local + carry, dt)   │
   └──────────────────────┬───────────┘
                          ▼
                   y_final (M, N_padded) dtype  ← 最终结果，Op 层裁回 N 列
```

三个 kernel 由一个 `@torch.library.custom_op` 包装串联（4.2.3）。

#### 4.2.3 源码精读

**串联三步的 custom_op**。[tileops/kernels/reduction/cumulative.py:262-278](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/reduction/cumulative.py#L262-L278)：

```python
@torch.library.custom_op("top::cumulative_parallel_fwd", mutates_args=())
def _cumulative_parallel_fwd_wrapped(M, N, dtype_str, block_m, block_n, threads, x):
    n_tiles = align_up(N, DEFAULT_ALIGNMENT) // block_n
    local_fn = _parallel_scan_local_kernel(M, N, "sum", dtype_str)(block_m, block_n, threads)
    y_local, tile_sums = local_fn(x)                 # Pass 1（返回两个张量）
    carry_fn = _parallel_scan_carry_kernel(M, n_tiles)(threads)
    tile_carries_exclusive = carry_fn(tile_sums)     # Pass 2
    propagate_fn = _parallel_scan_propagate_kernel(M, N, dtype_str)(block_m, block_n, threads)
    return propagate_fn(y_local, tile_carries_exclusive)  # Pass 3
```

`@tilelang.jit(out_idx=[1, 2])` 表示 Pass 1 有两个输出（`y_local` 和 `tile_sums`），所以 `local_fn(x)` 解包成两个张量。`register_fake`（[第 281-284 行](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/reduction/cumulative.py#L281-L284)）给 torch.compile 提供 `(M, N_padded)` 的输出形状推导，使 `fullgraph=True` 可追踪（承接 u10）。这是 `out_idx` 多输出的典型用法。

**Pass 1：local-scan（含 tile 总和）**。[tileops/kernels/reduction/cumulative.py:451-512](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/reduction/cumulative.py#L451-L512)。网格是**行 × tile 二维**：

```python
with T.Kernel(T.ceildiv(M, block_m), n_tiles, threads=threads) as (pid_m, tile_idx):
    tile_shared = T.alloc_shared((block_m, block_n + _SMEM_PAD), "float32")
    tile_frag   = T.alloc_fragment((block_m, block_n), "float32")
    ... # masked 载入到 tile_shared（越界列填 0）
    T.cumsum(tile_frag, dim=1)          # 第 490 行：块内含型前缀和（fp32）
    ...
    for i in T.Parallel(block_m):       # 第 505-508 行：tile 总和 = 最后一列
        row = pid_m * block_m + i
        with T.If(row < M), T.Then():
            tile_sums[row, tile_idx] = tile_shared[i, block_n - 1]
```

两个要点：

1. **块内扫描用了 TileLang 内建 `T.cumsum(tile_frag, dim=1)`**（第 490 行），而不是顺序后端里手写的 `T.Serial` 循环。`T.cumsum` 是库/硬件友好的原语（对比 `_primitives.py` 里 `make_cumulative_scan` 提供的手写 `T.macro` 版本）。
2. **shared memory 中继**：`T.cumsum` 把结果留在 fragment（每线程寄存器）里，但 `tile_sums` 要读“第 `block_n-1` 列”，而这一列散落在别的线程寄存器中。所以 kernel 先把 fragment 写回 `tile_shared`（第 495-497 行，注释见 493-494），再统一从 shared memory 读最后一列。这是“fragment 跨线程不可见 → 经 shared memory 中继”的标准模式。

shared memory 的 `_SMEM_PAD`（=8）与 u3-l2 一致，用于打破 32-way bank conflict：行步长 `(block_n + 8)` 个元素不是 32 的倍数，相邻行落在不同 bank。

**Pass 2：carry-scan（排他型前缀和）**。[tileops/kernels/reduction/cumulative.py:515-543](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/reduction/cumulative.py#L515-L543)。这是三步里**唯一串行**的一步，但极轻：

```python
with T.Kernel(T.ceildiv(M, threads), threads=threads) as pid:
    tx = T.get_thread_binding()
    row = pid * threads + tx
    with T.If(row < M), T.Then():
        tile_carries[row, 0] = T.float32(0.0)
        running_sum = T.alloc_var("float32", init=0.0)
        for j in T.Serial(n_tiles - 1):               # 串行扫 n_tiles（128~256）
            running_sum = running_sum + tile_sums[row, j]
            tile_carries[row, j + 1] = running_sum
```

设计要点：

- 网格是**一维**，按行展开。**一个线程独占一行**（`tx` 经 thread binding 映射到 `row`），所以对 `tile_carries[row, *]` 的写不会竞争——这就是 docstring 里“writes cannot race”的含义。
- 串行循环长度是 `n_tiles`（不是 N）。因为 `block_n ≥ 128`，对 N=32768 只有 `n_tiles=256`，串行 256 次加法几乎可忽略。
- 输出是**排他型**（`tile_carries[row, 0] = 0`，`tile_carries[row, j+1] = Σ_{k≤j} tile_sums[row,k]`），正好是 propagate 要加的进位 \(c_t\)。

**Pass 3：propagate（加进位 + cast）**。[tileops/kernels/reduction/cumulative.py:546-572](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/reduction/cumulative.py#L546-L572)。网格又是**行 × tile 二维**，纯 element-wise：

```python
with T.Kernel(T.ceildiv(M, block_m), n_tiles, threads=threads) as (pid_m, tile_idx):
    for i, j in T.Parallel(block_m, block_n):
        row = pid_m * block_m + i
        col = tile_idx * block_n + j
        with T.If(row < M), T.Then():
            y_final[row, col] = T.cast(
                y_local[row, col] + tile_carries[row, tile_idx], dtype)  # 边界 cast
```

这一步同时完成两件事：把进位加回（恢复全局前缀和），以及把 fp32 中间量 `y_local` cast 回存储 dtype（u3-l3 的“边界 cast”）。它不需要 shared memory，直接 global→global。

**Kernel.forward 的分叉**。[tileops/kernels/reduction/cumulative.py:423-443](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/reduction/cumulative.py#L423-L443)：并行后端调 `_cumulative_parallel_fwd_wrapped`（上述三步），顺序后端调 `_cumulative_fwd_wrapped`（单 kernel）。两者都是 `custom_op`，对 `torch.compile` 透明。

#### 4.2.4 代码实践

**实践目标**：把 4.2.2 的数据流图在真实代码上对齐，确认每步的张量形状与 kernel 网格。

**操作步骤**（源码阅读型，无需 GPU）：

1. 打开 [tileops/kernels/reduction/cumulative.py:451-572](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/reduction/cumulative.py#L451-L572)。
2. 为三个 kernel 各填一行表：

| kernel | `T.Kernel(...)` 网格维度 | shared memory 分配 | 主要输出 |
| --- | --- | --- | --- |
| `_parallel_scan_local_kernel` | `(ceildiv(M,block_m), n_tiles)` | `tile_shared (block_m, block_n+8)` fp32 | `y_local (M,N_padded)` + `tile_sums (M,n_tiles)` |
| `_parallel_scan_carry_kernel` | `(ceildiv(M, threads))` | 无 | `tile_carries (M,n_tiles)` |
| `_parallel_scan_propagate_kernel` | `(ceildiv(M,block_m), n_tiles)` | 无 | `y_final (M,N_padded)` |

3. 对一个具体形状，例如 `(M=64, N=32768, bfloat16, block_n=256)`：算出 `N_padded = align_up(32768, 256) = 32768`，`n_tiles = 32768 / 256 = 128`。于是 `tile_sums` 形状 `(64, 128)`、`tile_carries` 形状 `(64, 128)`。

**需要观察的现象**：Pass 1 和 Pass 3 的网格第二维是 `n_tiles`（不是 1），这正是“跨 tile 并行”的体现；只有 Pass 2 沿 tile 维串行，但长度仅 `n_tiles`。

**预期结果**：表格填写结果与 4.2.2 数据流图一致。若想在 GPU 上实证 `tile_sums` 的值，可在 Pass 1 后临时打印 `y_local.sum()` 并与 `tile_sums.sum()` 比较——两者应近似相等（`tile_sums` 是每 tile 总和，全求和等于 `y_local` 末列之和）。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：Pass 1 为什么在 `T.cumsum` 之后还要把 fragment 写回 shared memory 再读？

> **答案**：`tile_sums` 需要 tile 的最后一列（`block_n-1`）即 tile 总和。`T.cumsum` 把结果留在 fragment（寄存器），而 `block_n-1` 列分散在不同线程的寄存器里，单线程无法直接读到。经 shared memory 中继（第 495-497 行）后，任意线程都能访问到 `tile_shared[i, block_n-1]`。这是 register→smem→global 的标准跨线程广播。

**练习 2**：把 Pass 2 改成“包含型”前缀和（即 `tile_carries[row,t] = Σ_{k≤t} tile_sums`），propagate 公式要怎么改？

> **答案**：propagate 给第 t 个 tile 加的应是“前 t 个 tile 的总和”，即排他型 \(c_t\)。若 Pass 2 改成包含型，则 `tile_carries[row,t]` = `Σ_{k≤t}`，propagate 要减去本 tile 总和：`y = y_local + tile_carries[t] - tile_sums[t]`。代码选择排他型（`tile_carries[row,0]=0`）正是为了让 propagate 直接相加，省一次减法。

**练习 3**：并行后端为何禁用 autotune？

> **答案**：autotune 扫的是“单一 kernel 的 tile 配置空间”。并行后端是**固定的三段流水**，配置只有 `(block_m, block_n, threads)` 且 `block_n` 由 `N` 决定、`block_m` 由 shared memory 预算决定，没有可搜索的多维配置空间。强行 autotune 要把三个 kernel 一起 benchmark，代价大且收益小，故构造期直接 `tune=False` 并告警（第 358-365 行）。

---

### 4.3 cumprod 回退：为什么乘积扫描没有并行实现

#### 4.3.1 概念说明

回到判据那一行：`self.use_parallel = (M < 128 and N > 8192 and op_kind == "sum")`。`op_kind == "sum"` 把 cumprod 永久挡在并行门外。原因有两层：

1. **数值层**：cumprod 沿大 N 累乘会指数级放大或衰减。对 fp16/bf16，几十次乘法就上溢/下溢。测试里 cumprod 的输入刻意取 `rand*0.01 + 0.99`（值域贴近 1，见 [test_cumulative.py:103](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/ops/test_cumulative.py#L103)）就是为了压住溢出。“长序列大 N 乘积扫描”本身不是有意义的工作负载，没有优化的必要。
2. **算法层**：并行扫描的 carry 是**加法**进位（`running_sum + tile_sums`）。乘积扫描的 carry 应是**乘法**进位（`running_prod * tile_prod`），propagate 要把 local 结果“乘以”进位而非“加上”。这需要一套独立的乘法版三阶段 kernel，而收益又被第 1 点抹掉——不值得写。

所以 cumprod 一律走顺序后端 `_cumulative_kernel(op_kind="prod")`。

#### 4.3.2 核心流程

```text
CumprodFwdOp._op_kind = "prod"
        │
        ▼
CumulativeKernel(M, N, "prod", ...)  →  use_parallel = False（因 op_kind != "sum"）
        │
        ▼
self.kernel = _cumulative_kernel(M, N, "prod", dtype)   # 单 kernel 顺序扫描
        │
        ▼
forward → _cumulative_fwd_wrapped(...)  # custom_op "top::cumulative_fwd"
```

顺序 kernel 的结构与 u3-l2 的 RMSNorm 同族：`@T.prim_func` + `with T.Kernel(ceildiv(M, block_m), threads=...)`，按 `block_n` 切 N、`T.Serial(n_tiles)` 串行走 tile、块内 `T.Serial(block_n)` 维护每行累加器 `acc`。cumsum 与 cumprod 的顺序 kernel 共用同一骨架，仅累加器初值（0 vs 1）与运算（`+` vs `*`）不同。

#### 4.3.3 源码精读

**顺序 kernel 的 prod 分支**。[tileops/kernels/reduction/cumulative.py:176-254](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/reduction/cumulative.py#L176-L254)。关键差异点：

```python
# 累加器初值：sum→0，prod→1（第 199 行）
for i in T.Parallel(block_m):
    acc[i] = T.float32(1)

# 块内扫描：sum→+，prod→* （第 240-243 行）
for i in T.Parallel(block_m):
    for j in T.Serial(block_n):
        acc[i] = acc[i] * tile_f32[i, j]
        out_f32[i, j] = acc[i]
```

注意全程在 fp32（`tile_f32`/`out_f32`/`acc` 都是 `"float32"`）累乘，最后才 `T.cast(out_f32, dtype)` 写回（第 247 行）——与 cumsum 一样遵守“fp32 中间量、边界 cast”的数值约定（u3-l3）。`_identity` 在 prod 分支为 `1.0`，用于越界列填充（第 88、147、227 行），保证 padded 列不改变乘积。

**Op 层对 sum/prod 的统一处理**。[tileops/ops/reduction/cumulative.py:144-197](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/reduction/cumulative.py#L144-L197)：`CumsumFwdOp` 与 `CumprodFwdOp` 几乎完全相同，唯一差别是类属性 `_op_kind`（`"sum"` vs `"prod"`）。共享的 `_run`（[第 118-140 行](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/reduction/cumulative.py#L118-L140)）把 `_op_kind` 透传给 `CumulativeKernel`。这意味着“cumprod 无并行实现”这一事实，在 Op 层根本不存在——它只是 Kernel 构造期判据的一个分支。

**manifest 的对称性**。[tileops/manifest/scan.yaml:47-85](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/manifest/scan.yaml#L47-L85)：`CumprodFwdOp` 与 `CumsumFwdOp`（[第 7-45 行](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/manifest/scan.yaml#L7-L45)）的 roofline 公式完全对称——`flops: "M * N"`、`bytes: "2 * M * N * elem_bytes"`，只是注释里把“N-1 additions”换成“N-1 multiplications”。规约层不区分并行/顺序，这与 4.1.3“Op 层无感”一致：roofline 衡量的是理论工作量，与实现走哪条后端无关。

#### 4.3.4 代码实践

**实践目标**：确认 cumprod 在所有形状下都走顺序后端，并理解其数值约束。

**操作步骤**（源码阅读型）：

1. 阅读 [test_cumulative.py:213-219](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/ops/test_cumulative.py#L213-L219) 的 `test_cumprod_op`，注意 `CumulativeTest(..., "cumprod", use_small_range=True)`——cumprod 用 `rand*0.01+0.99` 而非 `randn`。
2. 阅读 [test_cumulative.py:126-130](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/ops/test_cumulative.py#L126-L130) 的 `_cumprod_tol`：cumprod 的容差（fp16/bf16 `atol=5e-2`）比 cumsum（`1e-2`）更宽松，正反映其数值更敏感。
3. 在 GPU 上跑一段（可选）：对 `(64, 32768, bfloat16)` 构造 `CumprodFwdOp`，读 `op._get_kernel(...).use_parallel`。

**需要观察的现象**：即便 `(64, 32768)` 满足 cumsum 的并行判据，cumprod 的 `use_parallel` 仍为 `False`。

**预期结果**：`use_parallel == False`（因 `op_kind != "sum"`）。若用 `randn` 而非 `rand*0.01+0.99` 喂 cumprod，大 N 下会观察到大量 inf/0（溢出/下溢），这正是 cumprod 不值得做长序列并行实现的原因。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果将来要给 cumprod 加并行实现，最小改动集是什么？

> **答案**：(a) 判据去掉 `op_kind == "sum"` 限制；(b) 新写一套乘法版三 kernel：Pass 1 用 `T.cumprod`（或手写乘法扫描）并产出 `tile_prods`，Pass 2 做**乘法排他型**前缀积（初值 1，`running *= tile_prods`），Pass 3 把 local 结果**乘以**进位；(c) 新增 `custom_op` 包装 `_cumulative_parallel_fwd_wrapped` 的 prod 变体；(d) 数值上仍需 fp32 中间量，且要接受大 N 下溢出/下溢。改动量不小，而收益被 cumprod 的数值特性限制——这就是当前不做的理由。

**练习 2**：为什么 cumprod 的容差比 cumsum 宽？

> **答案**：累乘的相对误差会沿序列累积放大（每步乘法都把之前的误差一起放大），而累加的误差增长线性得多。fp16/bf16 下 cumprod 的末尾元素误差显著大于 cumsum，故测试给 `5e-2` 容差（`_cumprod_tol`）而非 `1e-2`。

---

## 5. 综合实践

把三个最小模块串起来：**手工模拟一次三阶段并行扫描，并与 kernel 输出对照**。

**任务**：取 `M=2, N=8, block_n=4`（即 `n_tiles=2`）的小例子，全程在 PyTorch 里手算三步，验证最终结果等于 `torch.cumsum`。

```python
# 示例代码：手工模拟三阶段并行扫描（CPU 即可，用于理解，非项目代码）
import torch

x = torch.tensor([[1., 2., 3., 4., 5., 6., 7., 8.],
                  [8., 7., 6., 5., 4., 3., 2., 1.]])   # (M=2, N=8), block_n=4 → n_tiles=2

# Pass 1: 每个 tile 内部含型前缀和 + tile 总和
tile0 = x[:, 0:4]      # [[1,2,3,4],[8,7,6,5]]
tile1 = x[:, 4:8]      # [[5,6,7,8],[4,3,2,1]]
y_loc0 = tile0.cumsum(dim=-1)   # [[1,3,6,10],[8,15,21,26]]
y_loc1 = tile1.cumsum(dim=-1)   # [[5,11,18,26],[4,7,9,10]]
tile_sums = torch.stack([y_loc0[:, -1], y_loc1[:, -1]], dim=1)  # (M, n_tiles)=[[10,26],[26,10]]

# Pass 2: 排他型前缀和（沿 tile 维）
tile_carries = torch.zeros_like(tile_sums)
tile_carries[:, 1] = tile_sums[:, 0]                 # [[0,10],[0,26]]

# Pass 3: 加进位
y_final = torch.stack([y_loc0 + tile_carries[:, 0:1],
                       y_loc1 + tile_carries[:, 1:2]], dim=2).reshape(2, 8)

ref = x.cumsum(dim=-1)
assert torch.equal(y_final, ref), (y_final, ref)
print("ok\n", y_final)
```

**操作步骤**：

1. 在 CPU 上跑上述脚本，确认 `y_final == ref`。
2. 把 `block_n` 改成 2（`n_tiles=4`），重算，确认仍相等——这验证了“tile 大小不影响正确性，只影响并行度”。
3. （GPU 可选）把 `x` 搬到 CUDA，对同样的 `(M=2, N=8)` 调 `CumsumFwdOp`，与手算对比。注意 `M=2, N=8` 不满足 `N > 8192`，会走顺序后端——但顺序与并行结果应一致。

**需要观察的现象**：

- Pass 1 后 `y_loc1`（第二个 tile）单独看不等于全局 cumsum（缺了第一个 tile 的总和 10/26）。
- Pass 3 把进位补回后，第二个 tile 的每个元素都恰好补上了前一个 tile 的总和。
- 改 `block_n` 只改变 tile 划分，最终 `y_final` 不变。

**预期结果**：手算与 `torch.cumsum` 逐元素相等；GPU 上 TileOPs 输出与手算在容差内一致。这一实践把“顺序依赖被局部化到 Pass 2 的 n_tiles 长度”这一抽象，落成了可逐行核对的数值过程。

## 6. 本讲小结

- **形状驱动分发**：cumsum 在 `M < 128 且 N > 8192 且 op_kind == "sum"` 时走三阶段并行扫描，其余形状走单 kernel 顺序扫描；判据完全在 `CumulativeKernel` 构造期（`self.use_parallel`），Op 层无感。
- **三阶段并行扫描**：local-scan（块内前缀和 + tile 总和，二维网格全并行）→ carry-scan（对 `n_tiles` 个 tile 总和做排他型前缀和，一线程一行，唯一串行但极轻）→ propagate（加进位 + 边界 cast，二维网格全并行）。顺序依赖从长度 N 压缩到长度 n_tiles。
- **fp32 中间量**：`y_local`、`tile_sums`、`tile_carries` 全是 fp32，遵循“fp16/bf16 提升到 fp32 累加、边界 cast 回存储 dtype”的数值约定；最终 cast 发生在 propagate。
- **shared memory 角色**：Pass 1 用 `tile_shared` 既做 masked 载入、又做 register→smem 中继（让 `tile_sums` 能读到 tile 末列）；Pass 2、Pass 3 不需 shared memory。`_SMEM_PAD=8` 打破 bank conflict。
- **cumprod 回退**：cumprod 因数值溢出与乘法进位的额外复杂度，永远走顺序后端；顺序 kernel 与 cumsum 共骨架，仅累加器初值（0/1）与运算（`+`/`*`）不同。
- **架构收益**：并行后端是“多 kernel 协作”的范例——加并行路径只动 Kernel 层与一个新 `custom_op`，Op 层、manifest、测试骨架全不动，体现了 Op/Kernel 双层分离的扩展性。

## 7. 下一步学习建议

- **多 kernel 协作的更复杂样本**：本讲的三个 kernel 是线性串联。接下来读 u12-l1（Attention 家族）和 u12-l3（Mamba/SSD 家族），看 GQA/Mamba2 的 forward 如何编排更多 kernel（prefill/decode/state_passing），以及它们的 kernel_map 如何登记多 kernel。
- **custom_op 与 torch.compile 边界**：本讲两个 `custom_op`（`top::cumulative_parallel_fwd` / `top::cumulative_fwd`）是并行后端能被 `torch.compile(fullgraph=True)` 追踪的关键。继续读 u10-l1（编译边界不变量）与 [tests/ops/test_cumulative.py:360-390](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/ops/test_cumulative.py#L360-L390) 的 `test_cumsum_compile_fullgraph_warm_cache`，理解“预热缓存后 fullgraph”与“冷编译契约”的区别。
- **性能度量**：本讲只讲“怎么并行”，没讲“并行后比顺序快多少、离硬件极限多远”。带着 `(64, 32768)` 这个并行形状去读 u6（性能基准）与 u7（Roofline 模型），用 `scan.yaml` 的 roofline 公式（`flops=M*N`, `bytes=2*M*N*elem_bytes`）算它的 SOL 效率。
- **建议继续阅读的源码**：`tileops/kernels/reduction/_primitives.py`（`make_cumulative_scan` 手写版扫描宏，与本讲内建 `T.cumsum` 对照）、`benchmarks/ops/bench_cumulative.py`（看并行后端的实测耗时与 PyTorch 基线对比）。
