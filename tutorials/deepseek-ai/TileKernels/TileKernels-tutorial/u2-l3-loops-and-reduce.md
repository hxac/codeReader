# 循环、并行与规约原语

## 1. 本讲目标

本讲聚焦 GPU kernel 内部的「循环怎么写、怎么把一组数压成一个数」。读完本讲后，你应当能够：

- 区分 TileLang 的四种循环构造 `T.Parallel` / `T.unroll` / `T.vectorized` / `T.serial`，知道它们分别告诉编译器「这段循环怎么映射到 GPU 的并行硬件」，并能判断在什么场景下该用哪一种。
- 会用 `T.ceildiv` 做向上取整分块，并理解它与 wrapper 侧 `tile_kernels.utils.ceil_div` 的分工。
- 会用 `T.reduce_max` / `T.reduce_sum` / `T.reduce_absmax` 做维度规约，区分「全规约（不带 `dim`，压成标量）」和「部分规约（带 `dim=N`，沿某一轴压降一维）」两种用法。
- 看懂 `T.alloc_reducer` + `replication='all'` + `T.finalize_reducer` 这套「跨线程自定义归约」机制，理解 topk_gate 为什么靠它逐个稳定选出 top-k。

本讲承接 u2-l1（`@tilelang.jit` + `@T.prim_func` + `T.Kernel` 骨架）与 u2-l2（fragment/shared/local 三种存储与 `T.copy`）。上一讲我们说过：fragment 一旦要做跨线程规约（如量化里的 `T.reduce_absmax`），就要用到 `T.Parallel` 与 `T.reduce_*`——这正是本讲的主题。本讲不展开各算子的业务含义（量化留给 u4-l2、MoE 路由留给 u5、Sinkhorn 算法留给 u7-l3），只把它们当作「循环与规约原语的活教材」。

## 2. 前置知识

进入源码前，先用三段话建立「循环」与「规约」在 GPU 上的心智模型。

**第一，GPU 的并行不是「for 循环自动并行」，而是「你告诉编译器哪段循环该并行」。** 在普通 Python 里，`for i in range(N)` 是串行的。在 TileLang 的 `@T.prim_func` 里，你写 `for i in T.Parallel(N)` 才是在告诉编译器：「这 N 次迭代互相独立，请把它们分给线程块里的线程并行执行」。写 `for i in T.serial(N)` 则是反过来强调：「这 N 次必须按顺序执行，有数据依赖」。`T.unroll` 和 `T.vectorized` 是另外两种折中：前者把循环展开（省去分支开销、方便编译器调度），后者要求迭代间可被「打包成一条 SIMD 指令」同时处理。**选哪种循环，等于在声明这段迭代的「并行属性」。**

**第二，规约（reduce）就是把一组数压成一个数。** 比如求一行 128 个数的最大值、求和、绝对值最大（absmax）。在 GPU 上做规约的难点是「跨线程」：这 128 个数分散在不同线程的寄存器里，要算出全局最大值，线程之间必须通信。TileLang 的 `T.reduce_max(src_frag, dst_frag, dim=...)` 就是把这件事封装好了——你给一个源 fragment、一个目标 fragment、一个规约轴，编译器自动生成跨线程的归约指令树。你不用手写「warp shuffle + 共享内存」那一套。

**第三，当内置规约不够用时，用 reducer 自己拼。** `T.reduce_*` 只支持「max/sum/absmax」这几个固定操作，且要求源是一个 fragment、沿某个轴规约。但有时你要做的规约更复杂——比如「在所有等于当前最大值的元素里，取下标最小的那个」。这种「带条件的自定义归约」没有内置函数，于是 TileLang 提供 `T.alloc_reducer`：它是一个「跨线程累加器」，每个线程各持一份副本，并行更新后再用 `T.finalize_reducer` 合并成最终结果。topk_gate 选 top-k 就全靠它。

> 名词速查：规约（reduce，把多值压成少值）、部分规约（沿某一轴压降一维，如 `(M,K)` 按列求和得 `(M,)`）、全规约（压成单个标量）、absmax（绝对值最大，量化里用来定标）、reducer（自定义跨线程累加器）、warp（32 线程的调度单位，规约指令树的底层）、SIMD（单指令多数据，一条指令同时处理多个元素）。

本讲直接依赖 u2-l1（kernel 骨架、`T.dynamic` 运行时符号、grid/threads）和 u2-l2（fragment 的协作语义——所有 `T.reduce_*` 都作用在 fragment 上）。若对「编译期参数 vs 运行时符号」或「fragment vs local」还不熟，建议先回顾这两讲。

## 3. 本讲源码地图

本讲精读三个文件，它们恰好覆盖了循环与规约原语的所有典型用法：

| 文件 | 角色 | 主要循环原语 | 主要规约原语 |
| --- | --- | --- | --- |
| [tile_kernels/quant/per_token_cast_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py) | 逐 token 量化（FP8/FP4） | `T.Parallel`（逐元素计算）+ `T.ceildiv`（分块） | `T.reduce_absmax` + `T.reduce_max`（两段定标） |
| [tile_kernels/moe/topk_gate_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py) | MoE top-k 专家选择 | `T.Parallel`（加载/比较）+ `T.unroll`（逐个选 k） | `T.reduce_max`（全规约）+ `T.alloc_reducer('min')` |
| [tile_kernels/mhc/sinkhorn_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/sinkhorn_kernel.py) | Sinkhorn 行列归一化 | `T.Parallel`（逐元素）+ `T.serial`（迭代轮次） | `T.reduce_max` + `T.reduce_sum`（softmax 配方） |

辅助参考一个文件用于讲解 `T.vectorized`：

| 文件 | 用途 |
| --- | --- |
| [tile_kernels/transpose/batched_transpose_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py) | 本讲三个目标文件里没有 `T.vectorized`，故借用它作为 `T.vectorized` 的真实示例（细节属 u3-l1） |

本讲的策略是：先讲循环（4.1）和分块（4.2）这两个「骨架工具」，再讲规约（4.3，内置）和 reducer（4.4，自定义）这两个「把多数压成少数」的工具。规约是本讲的重头戏——它正是 fragment（u2-l2）之所以要「协作布局」的根本原因。

## 4. 核心概念与源码讲解

### 4.1 四种循环构造：Parallel / unroll / serial / vectorized

#### 4.1.1 概念说明

在 TileLang 的 `@T.prim_func` 里，`for ... in T.某构造(N)` 的「构造」不是装饰，而是给编译器的**并行性指令**。四种构造对应四种执行模型：

| 构造 | 语义 | 何时用 |
| --- | --- | --- |
| `T.Parallel(n)`（可多维） | 迭代相互独立，编译器可分派给线程并行 | 纯逐元素计算（逐元素赋值、逐元素乘缩放） |
| `T.unroll(n)` | 把循环完全展开成 n 份顺序代码 | 循环次数小且固定，想省分支开销、便于调度 |
| `T.vectorized(n)` | 迭代可打包成一条 SIMD 指令同时执行 | 连续内存的逐元素读/写，天然可向量化 |
| `T.serial(n)` | 必须按序执行，有数据依赖或顺序副作用 | 迭代算法的轮次（每轮依赖上一轮结果） |

关键直觉：**`T.Parallel` 是「告诉编译器放心并行」，`T.serial` 是「告诉编译器别并行、有依赖」，`T.unroll`/`T.vectorized` 是「介于两者之间的两种优化提示」。** 同一段逻辑写错构造，轻则性能差，重则结果错（比如把有数据依赖的循环写成了 `T.Parallel`）。

注意 `T.Parallel` 可以是多维的，如 `for i, j in T.Parallel(M, K)` 表示一个二维并行迭代空间，等价于 M×K 个独立任务。多维并行在逐元素 kernel 里极常见。

#### 4.1.2 核心流程

判断一段循环该用哪种构造，顺着这条决策树：

```
这段循环的各次迭代之间有数据依赖吗？
├── 有（第 t 轮要用第 t-1 轮的结果）  →  T.serial
└── 没有（相互独立）
     ├── 循环次数小且固定，想省分支开销    →  T.unroll
     ├── 连续内存读/写，可打包成 SIMD       →  T.vectorized
     └── 其余逐元素计算                     →  T.Parallel
```

本讲三个文件正好各展示一个「主用」构造：量化主用 `T.Parallel`（大量逐元素缩放）、topk 主用 `T.unroll`（固定 `num_topk` 次挑选）、Sinkhorn 主用 `T.serial`（`repeat` 轮迭代归一化）。

#### 4.1.3 源码精读

**量化 kernel：满屏 `T.Parallel` 的逐元素风格。** 几乎每一段「把 fragment 里每个元素算一遍」的代码都用 `T.Parallel`：

[per_token_cast_kernel.py:91](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L91) 用二维 `T.Parallel` 把输入 SF 变换到 fragment：

```python
for i, j in T.Parallel(num_sf_rows_per_block, num_sf_cols_per_block):
    ...
    x_sf_fragment[i, j] = transform_sf(load_sf(x_sf, m_idx, k_idx, in_config), in_config)
```

[per_token_cast_kernel.py:123](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L123) 与 [per_token_cast_kernel.py:150](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L150) 用 `T.Parallel(block_m, block_k)` 做最终的逐元素缩放写回——每个元素独立地乘以自己的 `sf_inv`，天然适合并行。

**topk_gate：`T.unroll` 把「选 k 个」展开成 k 段顺序代码。**

[topk_gate_kernel.py:41](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L41)

```python
for k in T.unroll(num_topk):
    T.reduce_max(scores_fragment, amax_fragment)
    ...
```

这里用 `T.unroll(num_topk)` 而非 `T.Parallel(num_topk)`，是因为 **k 次挑选之间有依赖**：第 k 次选完后要把选中的元素屏蔽成 `-inf`（见 [topk_gate_kernel.py:49-51](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L49-L51)），第 k+1 次才能选到次大值。但又不能用 `T.serial`——`num_topk` 通常很小（如 4、8），`T.unroll` 展开后能让编译器更灵活地调度每轮内部的并行逻辑（每轮里还有 `T.Parallel` 和 `T.reduce_max`）。

**Sinkhorn：`T.serial` 表达「每轮依赖上一轮」的迭代算法。**

[sinkhorn_kernel.py:45](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/sinkhorn_kernel.py#L45)

```python
for _ in T.serial(repeat - 1):
    # comb = comb / (comb.sum(-1) + eps)
    T.reduce_sum(comb_frag, row_sum, dim=2)
    for i, j, k in T.Parallel(token_block_size, hidden_size, hidden_size):
        comb_frag[i, j, k] = comb_frag[i, j, k] / (row_sum[i, j] + eps)
    ...
```

第 t 轮的行/列归一化读的是第 t-1 轮写出的 `comb_frag`，**轮次之间是严格串行的**，所以外层用 `T.serial(repeat - 1)`。注意每一轮**内部**的逐元素除法仍是 `T.Parallel`——「轮间串行、轮内并行」是迭代算法的经典写法。反向 kernel 里也用了同样的 `T.serial`（[sinkhorn_kernel.py:116](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/sinkhorn_kernel.py#L116) 与 [sinkhorn_kernel.py:131](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/sinkhorn_kernel.py#L131)）。

**`T.vectorized`：本讲三个目标文件里没有，借用转置 kernel 作真实示例。**

[batched_transpose_kernel.py:60](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L60)

```python
for k in T.vectorized(block_k):
    tmp_row[k] = x[pid_batch, pid_x * block_x + i * block_k + j, pid_y * block_y + col * block_k + k]
```

这里连续读 `block_k`（=4）个元素进 `tmp_row`，它们在内存里相邻、无依赖，可以打包成一条向量化 load 指令。转置 kernel 把 `T.vectorized`（搬运）和 `T.unroll`（[batched_transpose_kernel.py:56](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L56) 的外层展开）嵌套使用，细节在 u3-l1 展开。

> 对照记：**`T.Parallel`（量化，逐元素并行）、`T.unroll`（topk，固定 k 次挑选）、`T.serial`（Sinkhorn，迭代轮次）、`T.vectorized`（转置，连续搬运）**——同一个项目里四种构造各司其职，没有谁替代谁。

#### 4.1.4 代码实践

**实践目标：** 在三个目标文件里识别每一种循环构造，并验证它符合 4.1.2 的决策树。

**操作步骤：**

1. 在 `per_token_cast_kernel.py` 里数 `T.Parallel` 出现的次数（应远多于其他三种），挑两处（如 [L91](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L91)、[L123](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L123)）确认它们都是「逐元素、无依赖」的计算。
2. 在 `topk_gate_kernel.py` 里找到唯一的 `T.unroll`（[L41](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L41)），问自己：为什么这里不能改成 `T.Parallel(num_topk)`？（答：每轮依赖前一轮的屏蔽结果。）
3. 在 `sinkhorn_kernel.py` 里找到所有 `T.serial`（[L45](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/sinkhorn_kernel.py#L45)、[L116](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/sinkhorn_kernel.py#L116)、[L131](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/sinkhorn_kernel.py#L131)），确认每一处都满足「轮间有依赖」。

**需要观察的现象：** 三种构造各自主导一个文件，且每一处的选择都能用「有无依赖 + 是否固定小次数 + 是否连续可向量化」解释。

**预期结果：** 你应能对任意一段 kernel 循环，一句话说出它用了哪种构造、为什么这么选。本步骤为源码阅读型实践，无需运行。

#### 4.1.5 小练习与答案

**Q1：** 把 Sinkhorn 的 `for _ in T.serial(repeat - 1)` 改成 `for _ in T.Parallel(repeat - 1)` 会怎样？

**答：** 结果会错。每一轮的行/列归一化读的是上一轮更新后的 `comb_frag`，轮次之间有数据依赖。`T.Parallel` 假设迭代相互独立，可能乱序或同时执行各轮，读到未经更新的旧值。有依赖的迭代必须 `T.serial`。

**Q2：** topk_gate 的 `for k in T.unroll(num_topk)` 改成 `for k in T.serial(num_topk)` 还能跑对吗？

**答：** 结果应当仍正确——两者都是「按序执行」。区别在性能：`T.unroll` 把循环体复制 `num_topk` 份，省掉循环计数与分支判断，让编译器对每一轮内部的 `T.Parallel`/`T.reduce_max` 做更自由的调度；`T.serial` 保留循环结构。由于 `num_topk` 通常很小，`T.unroll` 是更优选择。

**Q3：** `T.vectorized` 和 `T.Parallel` 都要求迭代无依赖，区别在哪？

**答：** `T.Parallel` 是把迭代**分派到不同线程**并行；`T.vectorized` 是把**同一线程内的相邻迭代打包成一条 SIMD 指令**（如一条指令同时加载 4 个相邻 float）。前者跨线程，后者线程内向量化。连续内存读/写适合 `T.vectorized`，跨线程的逐元素计算适合 `T.Parallel`。

### 4.2 T.ceildiv 与向上取整分块

#### 4.2.1 概念说明

把一个大矩阵切成 tile 处理时，矩阵的维度常常不是 tile 大小的整数倍。比如 100 个 token、每块 `block_m=8`，需要 \(\lceil 100/8 \rceil = 13\) 块（最后一块只有 4 行有效）。用整数除法 `100 // 8 = 12` 会漏掉最后 4 行，所以必须**向上取整**。

`T.ceildiv(a, b)` 就是 TileLang 内核里的「向上取整除法」内建函数，返回 \(\lceil a/b \rceil\)。它最常出现在 `T.Kernel(...)` 的网格表达式里——网格的每个维度都是「需要多少个块来覆盖整个输入」。

> 与 wrapper 侧工具的关系：[tile_kernels/utils.py:1-6](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/utils.py#L1-L6) 里的 `ceil_div(x, y) = (x + y - 1) // y` 是**纯 Python** 版本，用在 wrapper（编译期、确切整数）里；`T.ceildiv` 是 **TileLang 内建**，用在 `@T.prim_func` 内部，且它的参数可以是 `T.dynamic` 运行时符号。两者语义相同，分工于「编译期 Python」与「运行时 kernel」。

#### 4.2.2 核心流程

向上取整除法的定义：

\[
\text{ceildiv}(a, b) = \left\lceil \frac{a}{b} \right\rceil = \left\lfloor \frac{a + b - 1}{b} \right\rfloor \quad (a \ge 0,\ b > 0)
\]

在 kernel 里它的典型用法是「算网格大小」，保证每个输入元素都被某个 tile 覆盖：

```
grid_m = ceildiv(num_tokens, block_m)   # 需要多少行块
grid_k = ceildiv(hidden,     block_k)   # 需要多少列块
with T.Kernel(grid_m, grid_k) as (pid_m, pid_k): ...
```

因为 `num_tokens` 常是 `T.dynamic` 运行时符号（见 u2-l1），网格维度也就随之在运行时确定——这正是 `T.ceildiv` 必须是内建（而非 Python 函数）的原因：它要在生成的代码里对运行时值做计算。

#### 4.2.3 源码精读

**量化 kernel：网格与 SF 形状都用 `T.ceildiv`。**

[per_token_cast_kernel.py:72](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L72)

```python
with T.Kernel(T.ceildiv(num_tokens, block_m), T.ceildiv(hidden, block_k), threads=num_threads) as (pid_token, pid_hidden):
```

这里 `num_tokens` 是运行时符号，`block_m`/`block_k` 是编译期参数（由 kernel 构造函数算出）。两个 `T.ceildiv` 分别给出「需要多少 token 块」和「需要多少 hidden 块」。当输入 `num_tokens` 不是 `block_m` 的整数倍时，最后一块会越界——但因为有 `num_tokens > 0` 的 wrapper 守卫（[per_token_cast_kernel.py:208](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L208)）和 fragment 的边界语义，越界访问会被处理。

SF（scaling factor）的加载也用 `T.ceildiv` 算每块覆盖多少 SF 行/列：

[per_token_cast_kernel.py:88-89](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L88-L89)

```python
num_sf_rows_per_block = T.ceildiv(block_m, in_config.sf_block[0])
num_sf_cols_per_block = T.ceildiv(block_k, in_config.sf_block[1])
```

**Sinkhorn kernel：一维网格的 `T.ceildiv`。**

[sinkhorn_kernel.py:24](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/sinkhorn_kernel.py#L24)

```python
with T.Kernel(T.ceildiv(num_tokens, token_block_size)) as pid_x:
```

Sinkhorn 按 `token_block_size` 切 token 维度，网格大小就是 `ceildiv(num_tokens, token_block_size)`。反向 kernel（[sinkhorn_kernel.py:76](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/sinkhorn_kernel.py#L76)）用完全相同的网格。

**topk_gate：不用 `T.ceildiv`，而用 `align`。** 注意 topk 的网格是 `T.Kernel(num_tokens, threads=num_threads)`（[topk_gate_kernel.py:25](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L25)），每个 token 一个块，不需要分块。但它用 `align` 把 `num_experts` 补齐到 32 的倍数：

[topk_gate_kernel.py:18](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L18)

```python
num_aligned_experts = align(num_experts, num_threads)
```

`align(x, y) = ceil_div(x, y) * y`（见 [utils.py:5-6](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/utils.py#L5-L6)），把 50 个专家补齐成 64 个（多出的位置填 `-inf`，见 [topk_gate_kernel.py:35-36](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L35-L36)），这样 `T.Parallel(num_aligned_experts)` 才能和 32 线程的 warp 整齐对应。这是「向上取整」思想的另一种应用：不是算块数，而是把维度本身补齐。

#### 4.2.4 代码实践

**实践目标：** 区分 `T.ceildiv`（运行时、网格）与 `ceil_div`/`align`（编译期、wrapper），并验证分块覆盖完整。

**操作步骤：**

1. 读 [per_token_cast_kernel.py:72](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L72)，取一组小数字手算：`num_tokens=10, block_m=4`，则 `T.ceildiv(10,4)=3` 块，分别覆盖 token `0-3`、`4-7`、`8-9`（最后一块只有 2 行有效）。
2. 读 [utils.py:1-6](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/utils.py#L1-L6)，确认 `ceil_div(10,4) = (10+4-1)//4 = 13//4 = 3`，与 `T.ceildiv` 同值。
3. 读 [topk_gate_kernel.py:18](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L18) 与 [topk_gate_kernel.py:32-36](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L32-L36)，取 `num_experts=50`：`align(50,32)=ceil_div(50,32)*32=2*32=64`，多出的 14 个位置（下标 50-63）被填成 `-T.infinity`。

**需要观察的现象：** 无论输入维度是多少，网格块数（或补齐后维度）总能让每个真实元素都被覆盖；多出的「填充位」靠 `-inf`/越界保护排除。

**预期结果：** 你能解释「为什么不能用 `//` 而必须向上取整」。本步骤为手算 + 源码阅读型实践，**待本地验证**的是你在真实输入下核对覆盖关系。

#### 4.2.5 小练习与答案

**Q1：** `T.ceildiv` 和 `utils.ceil_div` 都做向上取整，为什么要有两个？

**答：** 作用域不同。`utils.ceil_div` 是纯 Python 函数，用在 wrapper/编译期，参数是确切整数（如从 `x.shape` 取来的值）；`T.ceildiv` 是 TileLang 内建，用在 `@T.prim_func` 内部，参数可以是 `T.dynamic` 运行时符号（如 `num_tokens`），它会被编译进生成的 CUDA 代码在运行时求值。在 prim_func 里你**不能**调用 Python 的 `ceil_div`，因为那里 `num_tokens` 是个符号、不是 Python int。

**Q2：** topk_gate 为什么用 `align` 补齐 `num_experts` 而不是用 `ceildiv` 切块？

**答：** topk 的并行结构是「一个 token 一个块、块内 32 线程协作扫描所有专家」（[topk_gate_kernel.py:25](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L25)）。它不需要把专家维度切成多块（没有第二个 grid 维度），而是把专家序列补齐到 32 的倍数，让 `T.Parallel(num_aligned_experts)` 与 warp 的 32 线程整齐对应、避免分支。`align` 补齐的是「被扫描的维度本身」，`ceildiv` 算的是「需要多少块」，两者用途不同。

**Q3：** `align(x, y)` 的实现是 `ceil_div(x, y) * y`，它和 `T.ceildiv(x, y)` 的输出差什么？

**答：** `T.ceildiv(x, y)` 返回「块数」\(\lceil x/y \rceil\)；`align(x, y)` 返回「补齐后的维度」\(\lceil x/y \rceil \cdot y\)，是 `y` 的倍数。后者 = 前者 × `y`。

### 4.3 维度规约：reduce_max / reduce_sum / reduce_absmax

#### 4.3.1 概念说明

**规约（reduce）** 是把一个张量沿某个轴「压降」：用某个二元结合操作（max / sum / ...）把多个元素合并成一个。TileLang 提供三个内置规约，都作用在 **fragment** 上（这正是 u2-l2 强调 fragment 是「协作布局」的原因——规约要跨线程归并）：

| 原语 | 操作 | 典型用途 |
| --- | --- | --- |
| `T.reduce_max(src, dst, dim=d)` | 沿轴取最大 | softmax 减最大值、topk 选最大 |
| `T.reduce_sum(src, dst, dim=d)` | 沿轴求和 | softmax 分母、行列归一化分母 |
| `T.reduce_absmax(src, dst, dim=d)` | 沿轴取绝对值最大 | 量化定标（scaling factor） |

最关键的区别是**带不带 `dim`**：

- **部分规约（带 `dim=N`）**：沿第 N 轴压降，输出比输入少一维。如 `src` 形状 `(M, K)`、`dim=2` 不对——`dim=1` 得 `(M,)`（每行求和），`dim=0` 得 `(K,)`（每列求和）。
- **全规约（不带 `dim`）**：把整个 fragment 压成一个标量，`dst` 是形状 `(1,)` 的 fragment。topk_gate 的 `T.reduce_max(scores_fragment, amax_fragment)` 就是全规约——把整个专家得分序列压成一个最大值。

对量化特别重要的是 **absmax**：量化的 scaling factor 由「块内绝对值最大的元素」决定，因为要让 \([-|x|_{\max}, |x|_{\max}]\) 落进目标格式的表示范围。这就是为什么量化用 `reduce_absmax` 而非 `reduce_max`。

#### 4.3.2 核心流程

**配方一：数值稳定的 softmax（reduce_max + reduce_sum 的经典配合）。** 直接算 \(e^{x_i}\) 会溢出，标准做法是先减去最大值：

\[
m = \max_j x_j, \quad \text{softmax}(x)_i = \frac{e^{x_i - m}}{\sum_j e^{x_j - m}}
\]

落到 kernel 里是四步：`reduce_max` → 逐元素减+exp（`T.Parallel`）→ `reduce_sum` → 逐元素除（`T.Parallel`）。两个规约夹着两段逐元素计算，是 GPU 上 softmax 的标准写法。

**配方二：量化定标（reduce_absmax）。** 对一个 per-token 量化块，scaling factor 为：

\[
\text{sf} = \frac{|x|_{\max}}{v_{\max}}, \quad x_{\text{quant}} = \text{round}\!\left(\frac{x}{\text{sf}}\right)
\]

其中 \(v_{\max}\) 是目标格式（如 e4m3 的 448）的最大可表示值。`reduce_absmax` 算出的就是 \(|x|_{\max}\)，是整个定标流程的起点。

```
reduce_max  → 减最大值 → reduce_sum → 除   = softmax（Sinkhorn 用）
reduce_absmax → 除以 sf → cast        = 量化定标（per_token_cast 用）
reduce_max（全规约）→ 屏蔽 → 重复 k 次   = top-k（topk_gate 用）
```

#### 4.3.3 源码精读

**Sinkhorn 前向：教科书式的稳定 softmax + 行列归一化。**

[sinkhorn_kernel.py:32-38](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/sinkhorn_kernel.py#L32-L38)

```python
row_max = T.alloc_fragment((token_block_size, hidden_size), T.float32)
T.reduce_max(comb_frag, row_max, dim=2)              # ① 部分规约：沿 k 取 max
for i, j, k in T.Parallel(token_block_size, hidden_size, hidden_size):
    comb_frag[i, j, k] = T.exp(comb_frag[i, j, k] - row_max[i, j])
T.reduce_sum(comb_frag, row_sum, dim=2)              # ② 部分规约：沿 k 求和
for i, j, k in T.Parallel(token_block_size, hidden_size, hidden_size):
    comb_frag[i, j, k] = comb_frag[i, j, k] / row_sum[i, j] + eps
```

`comb_frag` 形状 `(token_block_size, hidden_size, hidden_size)`。`reduce_max(..., dim=2)` 沿最后一轴压降，得到 `(token_block_size, hidden_size)` 的每行最大值 `row_max`。注意：规约的**源是 fragment、目标也是 fragment**，且目标维度正好是「源去掉 `dim` 那一轴」。随后 `reduce_sum(..., dim=2)` 同理算出分母 `row_sum`。两段规约之间用 `T.Parallel` 做逐元素的 `exp` 和除法。

紧接着的列归一化用 `dim=1`（[sinkhorn_kernel.py:41](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/sinkhorn_kernel.py#L41)）——同一个 `reduce_sum`，换个轴就变成「每列求和」。这是 `dim` 参数威力的最好展示。

**量化 kernel：两段规约串联定标（with_sf 分支）。**

[per_token_cast_kernel.py:99](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L99) 先做一段 absmax：

```python
T.reduce_absmax(x_stage1_fragment_reshaped, stage1_amax_fragment, dim=-1)
```

注意 `dim=-1`（最后一轴）和 `dim=2` 在三维张量里等价。这一步把每个向量化小组的 absmax 算出来。随后乘上输入 SF，再做第二段 `reduce_max`：

[per_token_cast_kernel.py:111](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L111)

```python
T.reduce_max(stage2_amax_fragment_reshaped, sf_inv_fragment, dim=-1)
```

两段规约（absmax → max）是因为输入本身带 scaling factor 时，要把「输入 SF 的作用」和「输出定标」分两步合并——细节属 u4-l2。本讲只关注：**`reduce_absmax` 和 `reduce_max` 都带 `dim`，都是部分规约，输出比输入少一维。**

无输入 SF 的简单分支只有一段 absmax：[per_token_cast_kernel.py:137](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L137) `T.reduce_absmax(x_fragment_reshaped, amax_fragment, dim=2)`。

**topk_gate：全规约（不带 `dim`）。**

[topk_gate_kernel.py:42](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L42)

```python
T.reduce_max(scores_fragment, amax_fragment)
```

注意这里**没有 `dim` 参数**！`scores_fragment` 是一维 `(num_aligned_experts,)`，全规约把它压成标量存进 `amax_fragment[0]`（`amax_fragment` 形状 `(1,)`）。这是「全规约」的典型形态：源是一维 fragment、目标是 `(1,)` fragment、不带 `dim`。这和上面的部分规约形成鲜明对比。

> 对照记：**带 `dim` = 部分规约（降一维），不带 `dim` = 全规约（压成标量）。** Sinkhorn/量化用前者，topk 用后者。三个规约函数（max/sum/absmax）都同时支持这两种用法。

#### 4.3.4 代码实践

**实践目标：** 用 sinkhorn 的 softmax 段把「reduce_max + reduce_sum」的数据流画清楚，并区分部分/全规约。

**操作步骤：**

1. 读 [sinkhorn_kernel.py:32-38](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/sinkhorn_kernel.py#L32-L38)，在纸上画出这段的数据流：
   - `comb_frag (T,H,H)` ──`reduce_max(dim=2)`──▶ `row_max (T,H)`
   - `comb_frag` ──逐元素 `exp(x - row_max)`──▶ `comb_frag`（原地更新）
   - `comb_frag` ──`reduce_sum(dim=2)`──▶ `row_sum (T,H)`
   - `comb_frag` ──逐元素 `/ row_sum + eps`──▶ `comb_frag`
2. 对比 [topk_gate_kernel.py:42](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L42) 的全规约：`scores_fragment (E,)` ──`reduce_max`（无 dim）──▶ `amax_fragment (1,)`。标注「源、目标、是否带 dim」三个维度上的差异。
3. 思考：为什么 sinkhorn 的 `reduce_max` 不能也写成全规约？（答：它要对**每一行**分别取最大，输出是 `(T,H)` 而非单个标量。）

**需要观察的现象：** 部分规约的「目标 shape = 源 shape 去掉 dim 轴」这条规则，在三处规约上都成立；全规约的目标恒为 `(1,)`。

**预期结果：** 你能对任意一行 `T.reduce_*(src, dst, dim=...)`，秒答「源 shape、目标 shape、降了哪一维（或全规约）」。本步骤为源码阅读 + 手画数据流图，无需运行。

#### 4.3.5 小练习与答案

**Q1：** `T.reduce_absmax` 和 `T.reduce_max` 在量化里为什么不混用？为什么定标必须用 absmax？

**答：** 定标关心的是「数据的幅度范围」，而幅度由绝对值决定。若一组数是 `[-100, 3, 5]`，`reduce_max` 给 5、`reduce_absmax` 给 100——后者才是真正的峰值。用 `reduce_max` 会让负向大值被截断，量化后失真。所以量化一律用 `reduce_absmax` 定标（[per_token_cast_kernel.py:99](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L99)、[L137](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L137)）。

**Q2：** sinkhorn 里 `reduce_sum(comb_frag, row_sum, dim=2)` 和 `reduce_sum(comb_frag, col_sum, dim=1)` 用的是同一个函数，它们算出来的 `row_sum` 和 `col_sum` 形状分别是什么？

**答：** `comb_frag` 形状 `(token_block_size, hidden_size, hidden_size)`，记作 `(T, H, H)`。`dim=2` 压掉最后一轴，`row_sum` 形状 `(T, H)`（每个 `[i,j]` 是沿 k 的和）；`dim=1` 压掉中间轴，`col_sum` 形状 `(T, H)`（每个 `[i,k]` 是沿 j 的和）。两者形状相同但语义不同——一个按行求和、一个按列求和。

**Q3：** 为什么 `T.reduce_*` 的源必须是 fragment（u2-l2），不能是 local？

**答：** 规约需要**跨线程归并**——比如沿 k 轴求和时，k 维的元素分散在不同线程的寄存器里，要用 warp shuffle / 共享内存把它们加起来。fragment 是「协作布局」，其元素由编译器按已知映射分散到各线程，编译器据此生成跨线程归并指令。local 是「线程私有」，每个线程独立持有一份完整副本、彼此不可见（u2-l2），无法做跨线程归并。所以规约源必须 fragment。

### 4.4 alloc_reducer 与 replication：跨线程自定义归约

#### 4.4.1 概念说明

`T.reduce_*` 很方便，但它有三个限制：① 只支持 max/sum/absmax 这几个固定操作；② 源必须是 fragment；③ 规约轴必须是一个完整的维度。当你需要的归并逻辑超出这些限制——比如「在所有得分等于最大值的专家里，取下标最小的那个」——内置规约就不够用了。

`T.alloc_reducer` 就是为这种「自定义跨线程归约」准备的。它是一个**跨线程累加器**：

```python
reducer = T.alloc_reducer(shape, dtype, op, replication='all')
```

- `shape`：累加器的形状（常是 `(1,)` 或小向量）。
- `dtype`：元素类型。
- `op`：归并操作，如 `'min'`、`'sum'`（注意是字符串，比 `T.reduce_*` 灵活）。
- `replication='all'`：**每个线程各持一份副本**（replica）。

它的生命周期是固定的三步：

1. **`T.fill(reducer, init)`**：把所有副本初始化（如 min 操作要填最大值，sum 操作填 0）。
2. **在 `T.Parallel` 循环里更新**：每个线程用自己的副本做 `reducer[idx] = T.min(reducer[idx], ...)` 之类的竞争更新。因为每个线程有独立副本，**不会发生数据竞争**——你看到的是自己线程的局部累加值。
3. **`T.finalize_reducer(reducer)`**：把所有线程的副本按 `op` 合并成最终值。**只有调用它之后**，`reducer[idx]` 才是真正的全局归约结果，可以读用。

> 关键直觉：**`replication='all'` 把「跨线程竞争」变成「各线程先各自累加、最后一次性合并」。** 这避开了一个共享变量被多线程同时写的 race condition，又比内置规约灵活——更新逻辑是你自己写在 `T.Parallel` 里的任意表达式。

#### 4.4.2 核心流程

topk_gate 选 top-k 的完整机制（本讲最重要的算法片段）：

```
对 k = 0..num_topk-1（T.unroll 展开）:
  1. reduce_max(scores_fragment) → amax_fragment[0]   # 当前最大得分（全规约）
  2. fill(idx_reducer, +inf)                           # 重置 min 累加器
  3. Parallel: 若 scores[i] == amax[0]:
                idx_reducer[0] = min(idx_reducer[0], i)  # 在并列最大中取最小下标
  4. finalize_reducer(idx_reducer)                     # 合并各线程副本 → 真正的最小下标
  5. topk_idx_shared[k] = idx_reducer[0]               # 第 k 个结果
  6. Parallel: 若 idx[i] == idx_reducer[0]:
                scores[i] = -inf                          # 屏蔽已选，下一轮选次大
```

第 1 步用内置 `reduce_max` 找到「当前最大值是多少」；第 2-4 步用 reducer 解决「等于这个最大值的元素里下标最小的是谁」——这是内置规约做不到的条件 min。第 6 步把已选元素屏蔽，让下一轮的 `reduce_max` 自然落到次大值。**稳定性**（ties 取更小下标）完全由 `op='min'` 保证。

#### 4.4.3 源码精读

**topk_gate：reduce_max 与 reducer 的经典配合。**

[topk_gate_kernel.py:29](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L29) 分配 min reducer：

```python
idx_reducer = T.alloc_reducer((1,), T.int32, 'min', replication='all')
```

[topk_gate_kernel.py:41-51](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L41-L51) 是整个 top-k 的核心循环：

```python
for k in T.unroll(num_topk):
    T.reduce_max(scores_fragment, amax_fragment)          # ① 当前最大值
    T.fill(idx_reducer, T.max_value(T.int32))             # ② 重置为 INT_MAX（min 的单位元）
    for i in T.Parallel(num_aligned_experts):
        if scores_fragment[i] == amax_fragment[0]:
            idx_reducer[0] = T.min(idx_reducer[0], idx_fragment[i])  # ③ 并列者中取最小下标
    T.finalize_reducer(idx_reducer)                       # ④ 合并副本 → 真值
    topk_idx_shared[k] = idx_reducer[0]                   # ⑤ 写第 k 个结果
    for i in T.Parallel(num_aligned_experts):
        if idx_fragment[i] == idx_reducer[0]:
            scores_fragment[i] = -T.infinity(T.float32)   # ⑥ 屏蔽已选
```

逐行对照 4.4.2 的流程：`T.fill(idx_reducer, T.max_value(T.int32))` 是 min 操作的「单位元初始化」（任何数与 INT_MAX 取 min 都是它自己）。③ 里的 `T.min` 是逐线程在自己副本上做的，**只有 ④ 的 `T.finalize_reducer` 之后** `idx_reducer[0]` 才是全局最小下标。注意 ③ 是带条件的——只有 `scores[i] == amax[0]` 的线程才参与更新，这种「条件 min」是内置 `reduce_*` 表达不了的，正是 reducer 存在的理由。

**对照其他 reducer 用法（同项目，供横向参考）：** reducer 在别处还用于 sum 归约，如 [head_compute_mix_kernel.py:58-81](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/head_compute_mix_kernel.py#L58-L81) 用 `alloc_reducer(..., 'sum'/'all')` + `T.fill(..., 0)` + `T.finalize_reducer` 累加梯度（`'sum'` 操作、单位元 0）；[swiglu_forward_and_per_token_cast_kernel.py:81-87](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/swiglu_forward_and_per_token_cast_kernel.py#L81-L87) 用三个 `'sum'` reducer 统计元素个数。套路一致：**fill → 并行更新 → finalize → 读**。

> 对照记：内置 `T.reduce_*`（4.3）适合「固定操作 + fragment 源 + 整轴规约」；`T.alloc_reducer`（本节）适合「自定义/带条件 + 跨线程 + 非整轴」。topk 同时用了两者：`reduce_max` 找最大值，reducer 找最小下标。

#### 4.4.4 代码实践

**实践目标（对应本讲 practice_task 的两件事）：**

1. 在 topk_gate 里定位 `reduce_max` 与 `alloc_reducer('min', replication='all')` 的配合，说清它如何逐个稳定选出 top-k；
2. 仿照 topk_gate 的骨架，改写一个用 `reduce_sum` 计算每行元素和的 kernel。

**操作步骤：**

1. **跟读 top-k 流程：** 对照 [topk_gate_kernel.py:41-51](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L41-L51)，取一个小例子手算：`scores = [0.9, 0.5, 0.9, 0.2]`，`num_topk = 2`。
   - k=0：`reduce_max` 得 0.9；reducer 在「下标 0 和 2 都等于 0.9」中取 min → `idx_reducer=0`；输出 `topk[0]=0`；屏蔽 `scores[0]=-inf` → `[-inf, 0.5, 0.9, 0.2]`。
   - k=1：`reduce_max` 得 0.9（来自下标 2）；只有下标 2 满足 → `idx_reducer=2`；输出 `topk[1]=2`。
   - 结果 `[0, 2]`——并列的 0.9 取了更小下标 0 在前，**稳定性**体现。如果不取 min 而是任取，k=0 可能输出 2，结果就变成 `[2, 0]`，与「更小下标优先」不符。
2. **写 rowsum kernel（示例代码，非项目原有代码）。** 仿照 topk_gate 的「每 token 一块 + fragment 加载 + shared 输出」骨架，但用内置 `reduce_sum` 替代手写循环：

```python
# 示例代码：仅供练习，非 tile_kernels 仓库内的实现
import tilelang
import torch
from tilelang import language as T
from tile_kernels.utils import align

@tilelang.jit(pass_configs={tilelang.PassConfigKey.TL_DISABLE_WARP_SPECIALIZED: True})
def get_rowsum_kernel(num_cols: int):
    num_tokens = T.dynamic('num_tokens')
    num_aligned = align(num_cols, 32)

    @T.prim_func
    def rowsum_kernel(
        x: T.Tensor[(num_tokens, num_cols), T.float32],
        row_sum: T.Tensor[(num_tokens,), T.float32],
    ):
        with T.Kernel(num_tokens, threads=32) as pid:
            x_fragment = T.alloc_fragment((num_aligned,), T.float32)
            sum_fragment = T.alloc_fragment((1,), T.float32)
            sum_shared = T.alloc_shared((1,), T.float32)

            # 加载并把 padding 位填 0（不贡献到求和）
            for i in T.Parallel(num_aligned):
                x_fragment[i] = x[pid, i] if i < num_cols else 0.0

            T.reduce_sum(x_fragment, sum_fragment)   # 全规约：整行压成标量
            sum_shared[0] = sum_fragment[0]
            T.copy(sum_shared, row_sum[pid], disable_tma=True)

    return rowsum_kernel
```

   注意三处与 topk_gate 的呼应：① 用 `align` 补齐到 32（同 [topk_gate_kernel.py:18](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L18)），padding 位填 0 而非 `-inf`（求和的单位元是 0）；② `T.reduce_sum` 全规约把整行压成标量 `sum_fragment[0]`（类比 topk 的全规约 [L42](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L42)）；③ 经 shared 中转写回 global（同 [topk_gate_kernel.py:30](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L30) 与 [L53](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L53)）。

3. **对拍验证（若本地有 GPU）：** 用 `torch.sum(x, dim=1)` 作参考，与你的 kernel 输出比较。若无法运行，标注「待本地验证」，并说明预期：两者应位精确相等（求和是确定操作）。

**需要观察的现象：** 手算的 top-k 流程应体现「并列取小下标」的稳定性；rowsum kernel 应正确地把每行压成一个标量。

**预期结果：** 你能讲清「reduce_max 找值 + min reducer 找下标 + 屏蔽选次大」三段式 top-k，并能独立写出一个全规约求和的 kernel。运行结果**待本地验证**。

#### 4.4.5 小练习与答案

**Q1：** topk_gate 里如果忘了写 `T.finalize_reducer(idx_reducer)`，直接读 `idx_reducer[0]` 会怎样？

**答：** 读到的是**某个线程的局部副本**，而不是全局合并后的最小下标。因为 `replication='all'`，每个线程在 ③ 里只更新自己的副本；只有 `T.finalize_reducer` 才把所有副本按 `'min'` 合并。漏掉它，`topk_idx_shared[k]` 会是个不确定的局部值，结果错误。

**Q2：** 为什么 `T.fill(idx_reducer, T.max_value(T.int32))` 要填 INT_MAX，而不是 0？

**答：** 因为 reducer 的 op 是 `'min'`。min 操作的**单位元**是「正无穷」（任何数与它取 min 都是它自己）——对 int32 就是 `INT_MAX`。若填 0，那么只有下标 > 0 的元素能更新它（下标 0 永远赢不了 0），结果偏向非零下标。sum 操作的单位元才是 0（见 [head_compute_mix_kernel.py:60-61](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/head_compute_mix_kernel.py#L60-L61) 的 `T.fill(..., 0)`）。**初始化值必须匹配 op 的单位元。**

**Q3：** 能不能用内置 `T.reduce_min` 替代 topk 里的 `alloc_reducer('min')`？

**答：** 不能直接替代。内置 `reduce_min` 只能对「一个 fragment 的某一整轴」做无条件 min。而 topk 要做的是「**在满足 `scores[i] == amax[0]` 的那些元素中**取下标 min」——带条件，且参与规约的是「下标」、条件却依赖「得分」，两者分离。这种条件规约没有内置函数，必须用 reducer 手写更新逻辑。这正是 reducer 相对内置规约的核心价值。

## 5. 综合实践

**任务：** 把本讲四个主题（循环、ceildiv 分块、规约、reducer）一次性串起来——为一个「行求和」算子画出从 wrapper 到 kernel 的完整设计，并对照 topk_gate 说明「内置规约 vs 自定义 reducer」的取舍。

**步骤：**

1. **用 `T.ceildiv`/`align` 设计网格：** 假设输入 `(num_tokens, hidden)`。方案 A（仿 topk）：一个 token 一块、`align(hidden, 32)` 补齐，块内全规约；方案 B（仿量化/Sinkhorn）：按 `(block_m, block_k)` 切块、`T.ceildiv(num_tokens, block_m)` 与 `T.ceildiv(hidden, block_k)` 作网格，块内部分规约。在纸上写出两种方案的 `T.Kernel(...)` 行。
2. **选循环构造：** 对加载、逐元素、写回三段，分别标注用 `T.Parallel` 还是 `T.vectorized`（若连续可向量化）还是 `T.unroll`。说明为什么每段不能交叉用错（如加载若用 `T.serial` 会丧失并行）。
3. **选规约工具：** 行求和用内置 `T.reduce_sum(x_fragment, sum_fragment)`（全规约，见 4.3）即可——它不需要带条件，内置够用。请说明：**为什么这里不需要 reducer？**（答：求和无条件、操作固定、源是 fragment，完全落在 `T.reduce_sum` 的能力范围内；只有像 topk 那样「带条件的 min over selected」才需要 reducer。）
4. **写出 wrapper（示例）：** 仿 [topk_gate_kernel.py:58-90](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L58-L90) 的 `topk_gate` wrapper，写出 `rowsum` 的 wrapper：校验 `x.dim()==2` → 分配 `row_sum` 输出 → `num_tokens==0` 守卫 → 取 kernel → 启动 → 返回。注意零规模守卫（对应 [topk_gate_kernel.py:81-82](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L81-L82)）。
5. **对照 topk_gate 写一段总结：** 列出 topk 与 rowsum 各自「为什么用 reducer / 为什么不用」的一句理由。

**验收：** 你应当能用一张图说清「行求和」从 Python 调用到 kernel 内部规约的完整链路，并在每个环节指出本讲对应的概念（网格用 `T.ceildiv`、逐元素用 `T.Parallel`、求和用全规约 `T.reduce_sum`、wrapper 做校验与守卫）。这张图也是 u10-l3（新增算子综合实战）的预演。

> 本实践为设计 + 源码阅读型。若本地有 GPU，可把 4.4.4 的示例代码补全 wrapper 实际运行对拍；无法运行时，结论以源码行号与手算为准，运行结果标注「待本地验证」。

## 6. 本讲小结

- 四种循环构造各司其职：`T.Parallel`（迭代独立、分派到线程，量化主用）、`T.unroll`（小固定次数、展开省分支，topk 主用）、`T.serial`（轮间有依赖，Sinkhorn 迭代主用）、`T.vectorized`（连续可向量化，转置搬运用）。判断靠「有无依赖 + 是否固定小次数 + 是否连续」。
- `T.ceildiv(a,b)` 是 prim_func 内的向上取整内建，参数可为 `T.dynamic` 运行时符号，主要用在网格表达式里算「需要多少块」；它与 wrapper 侧 `utils.ceil_div`（编译期纯 Python）、`align`（补齐到倍数）分工。
- 三个内置规约 `T.reduce_max/sum/absmax` 都作用在 fragment 上；**带 `dim` 是部分规约（降一维），不带 `dim` 是全规约（压成标量）**。Sinkhorn 的稳定 softmax 用 `reduce_max`+`reduce_sum`，量化定标用 `reduce_absmax`，topk 选最大用全规约 `reduce_max`。
- `T.alloc_reducer(shape, dtype, op, replication='all')` 是跨线程自定义累加器，生命周期固定为 `T.fill`（填单位元）→ `T.Parallel` 内逐线程更新 → `T.finalize_reducer`（合并副本）→ 读。它支持带条件的自定义归并（如 topk 的「并列取最小下标」），这是内置 `reduce_*` 做不到的。
- topk_gate 的 top-k 是 reduce_max 与 reducer 的经典配合：`reduce_max` 找当前最大值、min reducer 找「等于该值的最小下标」、屏蔽已选元素后重复 `num_topk` 次（`T.unroll`），从而**稳定**地逐个选出 top-k。

## 7. 下一步学习建议

- **横向进入 u3-l1（批量转置 kernel 深入）：** 本讲借用转置 kernel 讲了 `T.vectorized`/`T.unroll`，它的「寄存器 4×4 块转置 + swizzle + loop_layout」细节在 u3-l1 专门展开，你会看到循环构造与存储布局（u2-l2）如何深度耦合。
- **纵向深入量化 u4-l2：** 本讲只把 `per_token_cast_kernel` 当作规约的例子，它的「absmax + 两段规约 + cast 写出」完整定标流程、`with_sf` 分支为何要 absmax→max 两段，在 u4-l2 完整讲解。
- **纵向深入 MoE u5-l2：** 本讲的 topk_gate 是 MoE 路由的核心积木，u5-l2 会讲它与 `top2_sum_gate`、warp shuffle（`T.shfl_sync`/`T.sync_warp`）的配合，以及完整路由参考。
- **纵向深入 mhc u7-l3：** 本讲把 Sinkhorn kernel 当作「reduce_max/sum + serial」的活教材，它的迭代行列归一化算法、以及反向 kernel 为何要保存全部中间量逆序回传（依赖本讲的 `T.serial`），在 u7-l3 专门展开。
- **综合实战 u10-l3：** 本讲 4.4.4 与第 5 节的「写一个 rowsum kernel」是新增算子的最小预演；u10-l3 会带你完成「kernel + wrapper + torch 参考 + pytest」四件套的端到端落地。
- **延伸阅读建议：** TileLang 文档里 `T.reduce_*` 的 *reduction axis* 与 `T.alloc_reducer` 的 *replication* 说明可补充本讲的语义细节；CUDA 官方手册里 *Warp Shuffle* 一章可帮你理解「跨线程归并」在硬件上到底怎么实现（与本讲 reducer 的 `finalize` 对应）。
