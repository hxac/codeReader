# 秩 / 选择 / 通用 / 向量化滤波

> 本讲属于「进阶：滤波与傅里叶」单元（u2），承接 u2-l2（多维相关卷积、footprint 与 axes 展开）。
> u2-l1～u2-l4 讲的全是**「加权求和」型**滤波：核 `weights` 沿邻域滑动，把每个邻域里的元素加权累加（相关/卷积）、或先造核再累加（高斯/均匀/微分）。
> 本讲换一条路线——**「从邻域里挑出一个元素」或「把邻域整个交给一个回调」**：
> `rank_filter` / `median_filter` / `percentile_filter` 挑出排序后第 `rank` 个元素；
> `minimum_filter` / `maximum_filter` 挑出最小/最大（恰好是 rank 滤波的端点特例）；
> `generic_filter` / `generic_filter1d` 把每个邻域塞给一个 Python / `LowLevelCallable` 回调；
> `vectorized_filter` 则用 `np.pad` + 滑动窗口把整个滤波变成一次向量化调用，从而支持 `float16`、复数、NaN 控制。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `rank_filter` / `median_filter` / `percentile_filter` 三个公开函数**只是同一引擎 `_rank_filter` 的三件外套**，它们的差别仅在于「如何把人给的参数换算成内部的 `rank` 整数」。
- 解释 `_rank_filter` 里两段关键分支：**端点短路**（`rank==0` → `minimum_filter`、`rank==filter_size-1` → `maximum_filter`）与**一维快速路径**（输入是一维数组且 dtype 合法时，走 C++ 双堆 `_rank_filter_1d.rank_filter`，否则走通用 C 内核 `_nd_image.rank_filter`）。
- 解释 `minimum_filter` / `maximum_filter` 为何共用 `_min_or_max_filter`，并能区分**可分离路径**（逐轴调用 1-D `min_or_max_filter1d`）与**不可分离路径**（一次调用 N-D `_nd_image.min_or_max_filter`）。
- 说清 `generic_filter` / `generic_filter1d` 的「回调契约」：前者每个元素回调一次、后者每条线回调一次，回调可以是 Python 可调用对象，也可以是 `scipy.LowLevelCallable`（带固定 C 签名）。
- 说清 `vectorized_filter` 为何能在 `median_filter` 力不从心的场景（`float16`、含 NaN、偶数窗口、要多输出）下「给出可控结果」：它根本不走 C 邻域循环，而是 `np.pad` → `sliding_window_view` → 把整块窗口喂给一个**已向量化**的可调用对象（如 `np.nanmedian`）。

## 2. 前置知识

### 2.1 「选出一个元素」≠「加权求和」

u2-l1 的 `correlate1d` 是「加权求和」：邻域里每个元素乘一个权重再相加，结果一般是**邻域里没有的新值**。本讲的 rank/选择滤波完全不同——它**从邻域里原样挑出一个元素**作为输出：

> 把当前点邻域内的所有元素排个序，输出第 `rank` 个（从 0 数起）。

- `rank = 0`：挑最小的 → 就是 `minimum_filter`。
- `rank = n-1`（`n` 是邻域元素数）：挑最大的 → 就是 `maximum_filter`。
- `rank = n // 2`：挑中间那个 → 中位数 `median_filter`。
- `rank = int(n * p / 100)`：挑第 `p` 百分位 → `percentile_filter`。

所以这五个函数在数学上**同属一族**——「顺序统计量滤波」（order-statistic filter）。理解了这一点，再看源码里 `rank==0` 直接调 `minimum_filter` 就不会意外了。

### 2.2 rank / percentile / median 三者如何统一

设邻域元素数为 `n`（即 footprint 里 `True` 的个数，记作 `filter_size`）。三者只是把不同的「人话」翻译成同一个整数 `rank`：

| 公开函数 | 人的输入 | 换算成 `rank` |
| --- | --- | --- |
| `rank_filter` | 整数 `rank`（可为负，`-1` 表最大） | 直接用（负数则 `rank += n`） |
| `median_filter` | 无 | `rank = n // 2` |
| `percentile_filter` | 百分比 `p ∈ [0,100]`（可为负） | `rank = int(n * p / 100)`，`p==100` 时 `rank = n-1` |

注意 `median_filter` 取的是排序后**第 `n//2` 个**元素，当 `n` 为偶数时**不是**常规中位数（常规中位数是中间两个的平均），而是「下中位数」。这是它的一个已知行为差异。

### 2.3 可分离性：min/max 为何能逐轴算

u2-l3 讲过「可分离性」：若一个多维核能写成各维 1-D 核的乘积，N-D 卷积就等于逐轴 1-D 卷积。`min` / `max` 天然满足一个更强的性质：

\[
\min_{\text{box}} I \;=\; \min_{x}\Bigl(\min_{y} I\Bigr), \qquad \max_{\text{box}} I \;=\; \max_{x}\Bigl(\max_{y} I\Bigr)
\]

即「一个矩形邻域的最小值 = 先沿 x 轴每条线取最小、再沿 y 轴取最小」。因此**矩形（全 True footprint）的最小/最大滤波可以用 1-D 滤波逐轴串联**，每轴用专门的 O(n) 算法（MINLIST/MAXLIST）。这正是 `_min_or_max_filter` 里「可分离路径」的依据。

但**任意形状的 footprint 不满足**这一性质（一个十字形邻域的最小值不能拆成逐轴最小），所以非矩形 footprint 必须走不可分离的 N-D 内核。rank/中位数滤波同理——中位数**不可分离**，所以 `_rank_filter` 没有逐轴路径（除了一维输入这条特殊捷径）。

### 2.4 三种「让用户自定义邻域运算」的方式

本讲的 8 个公开函数可按「邻域运算由谁完成」分成三档：

| 档次 | 代表函数 | 运算在哪里发生 | 灵活性 / 速度 |
| --- | --- | --- | --- |
| 专用 C 内核 | `minimum_filter` / `maximum_filter` / `rank_filter` / `median_filter` / `percentile_filter` | 编译好的 C/C++ 内核 | 运算写死，最快；但 dtype 受限、NaN 行为不可控 |
| 回调式 C 循环 | `generic_filter` / `generic_filter1d` | C 负责遍历邻域，每个邻域回调一次 Python/C 函数 | 任意运算；但 Python 回调开销大 |
| 纯向量化 | `vectorized_filter` | `np.pad` + 滑动窗口，整块喂给一个向量化函数 | 任意运算 + NaN/dtype 可控；但是暴力法，有冗余计算 |

理解这三档的取舍，是本讲的核心收获。

> 概念衔接：`correlate1d` 与「邻域/核」（u2-l1）、footprint/size 与 `_expand_footprint`/`_expand_origin`/`_expand_mode`「展开三件套」（u2-l2）、`_get_output` / `_check_axes` / `_normalize_sequence` / `_extend_mode_to_code`（u1-l4）。

## 3. 本讲源码地图

本讲涉及**两个文件**：一个 Python 包装层，一个 C++ 一维秩滤波内核。

| 文件 | 作用 |
| --- | --- |
| [`_filters.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py) | 滤波子包的全部 Python 实现。本讲读其中 10 个函数 + 1 个输入校验函数。 |
| [`src/_rank_filter_1d.cpp`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/_rank_filter_1d.cpp) | 一维秩滤波的 C++ 内核，用「双堆 Mediator」做 O(n log w) 滑动窗口求第 `rank` 小。 |

涉及的函数与职责：

| 函数 | 所在文件 | 行号区间 | 职责 |
| --- | --- | --- | --- |
| `minimum_filter1d` | `_filters.py` | L1625–L1678 | 1-D 最小滤波，调 `_nd_image.min_or_max_filter1d(..., 1)`。 |
| `maximum_filter1d` | `_filters.py` | L1682–L1735 | 1-D 最大滤波，调 `_nd_image.min_or_max_filter1d(..., 0)`。 |
| `_min_or_max_filter` | `_filters.py` | L1738–L1826 | **共用引擎**：可分离则逐轴 1-D，否则 N-D 内核。 |
| `minimum_filter` | `_filters.py` | L1830–L1874 | 外套：`_min_or_max_filter(..., minimum=1)`。 |
| `maximum_filter` | `_filters.py` | L1878–L1924 | 外套：`_min_or_max_filter(..., minimum=0)`。 |
| `_rank_filter` | `_filters.py` | L1928–L2028 | **共用引擎**：算 rank、端点短路、选 1-D 快速路径或 N-D 内核。 |
| `rank_filter` | `_filters.py` | L2032–L2075 | 外套：`operation='rank'`。 |
| `median_filter` | `_filters.py` | L2079–L2138 | 外套：`operation='median'`。 |
| `percentile_filter` | `_filters.py` | L2142–L2185 | 外套：`operation='percentile'`。 |
| `generic_filter1d` | `_filters.py` | L2189–L2294 | 每条线回调一次，调 `_nd_image.generic_filter1d`。 |
| `generic_filter` | `_filters.py` | L2298–L2446 | 每个元素回调一次，调 `_nd_image.generic_filter`。 |
| `vectorized_filter` | `_filters.py` | L206–L480 | 纯 Python：`pad` + 滑动窗口 + 向量化回调。 |
| `_vectorized_filter_iv` | `_filters.py` | L55–L202 | `vectorized_filter` 的输入校验与包装。 |
| `Mediator`（类） | `src/_rank_filter_1d.cpp` | L14–L49 | 双堆数据结构，维护滑动窗口第 `rank` 小。 |
| `MediatorInsert` | `src/_rank_filter_1d.cpp` | L124–L160 | O(log w) 插入新元素、挤出旧元素、维护 rank。 |
| `_rank_filter`（C++ 模板） | `src/_rank_filter_1d.cpp` | L166–L278 | 逐位置滑动窗口，含 5 种边界模式的前/后填充。 |
| `rank_filter`（C 包装） | `src/_rank_filter_1d.cpp` | L282–L345 | 解析 Python 参数，按 dtype 分派到模板。 |

> 阅读策略：先读 `_rank_filter`（L1928）和 `_min_or_max_filter`（L1738）这两个「引擎」，看清分支结构；再看 5 个公开外套如何一行填参；`generic_*` 与 `vectorized_filter` 是两条独立的「自定义」支线，单独看。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：① `rank_filter` / `percentile_filter` / `median_filter`（含 `_rank_filter` 引擎与 C++ 双堆）；② `minimum_filter` / `maximum_filter`（含 `_min_or_max_filter`）；③ `generic_filter` / `generic_filter1d`（回调机制）；④ `vectorized_filter`（向量化 + NaN 控制）。

### 4.1 rank_filter / percentile_filter / median_filter

#### 4.1.1 概念说明

「秩滤波」要做的事很直白：在每个输出点，把它邻域（由 `size` 或 `footprint` 定义）内的所有元素收集起来排序，输出排在第 `rank` 位（从 0 开始）的那个元素。`median_filter` 和 `percentile_filter` 没有独立的算法，只是把「中位数」「百分位」这两种更人性的说法换算成同一个 `rank` 整数，然后复用同一套引擎。

为什么要单独造一个 C++ 内核 `_rank_filter_1d`？因为「每个窗口都排序」的最朴素实现是 O(n·w·log w)（n 是数组长度、w 是窗口宽度），对长序列很慢。一维情况下，可以用**双堆（max-heap + min-heap）滑动窗口**把每个新窗口的秩元素更新降到 O(log w)，总体 O(n log w)。这就是 `_rank_filter_1d.cpp` 里 `Mediator` 类做的事。

#### 4.1.2 核心流程

`_rank_filter` 引擎的执行流程（伪代码）：

```
1. footprint 归一化：size → np.ones(size, bool)；已有 footprint → asarray(bool)
2. 「展开三件套」：_expand_footprint / _expand_origin / _expand_mode
   （处理 axes 子集，见 u2-l2）
3. filter_size = footprint 里 True 的个数
4. 按 operation 把人的参数换算成整数 rank：
     median     → rank = filter_size // 2
     percentile → rank = int(filter_size * pct / 100)（pct==100 取 filter_size-1）
     rank       → 直接用（负数则 += filter_size）
5. 端点短路：
     rank == 0                  → 直接 return minimum_filter(...)   # 复用！
     rank == filter_size - 1    → 直接 return maximum_filter(...)   # 复用！
6. 中间 rank：准备 output，处理 input/output 别名
7. mode = _extend_mode_to_code(mode, is_filter=True)
8. 选内核：
     if 一维输入 且 lim2 >= 0（或 size==1） 且 dtype 合法:
         按 dtype 转换 → _rank_filter_1d.rank_filter(...)   # C++ 双堆，快
     else:
         _nd_image.rank_filter(...)                          # 通用 C，N-D
```

第 5 步是理解全模块的钥匙：**最小/最大滤波就是秩滤波的端点**，所以代码直接调 `minimum_filter` / `maximum_filter`，复用模块 4.2 的实现。第 8 步的「一维快速路径」只对 `input.ndim == 1` 的数组生效，多维数组一律走 `_nd_image.rank_filter`。

#### 4.1.3 源码精读

**三个公开外套**——一行填参而已：

[`_filters.py:L2032-L2075`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L2032-L2075) —— `rank_filter`：先用 `operator.index(rank)` 把 `rank` 钳成整数（拒绝 `3.0` 这类「看起来像整数」的浮点），再把 `operation='rank'` 传给引擎。

[`_filters.py:L2137-L2138`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L2137-L2138) —— `median_filter`：传一个占位的 `0`，`operation='median'`（真正的 rank 在引擎里由 `filter_size // 2` 算出）。

[`_filters.py:L2184-L2185`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L2184-L2185) —— `percentile_filter`：把 `percentile` 当作 `rank` 占位传进去，`operation='percentile'`。

**引擎里 operation 换算 rank**（核心）：

[`_filters.py:L1960-L1976`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L1960-L1976) —— 先用 `np.where(footprint, 1, 0).sum()` 数出 `filter_size`，再按 `operation` 三分支算 `rank`；`percentile` 分支里 `percentile<0` 时先 `+=100`（所以 `-20` 等价于 `80`），`percentile==100` 特判为 `filter_size-1`（避免 `int(n*100/100)` 在浮点误差下落不到末位）；最后 `rank<0` 再 `+=filter_size`，并校验 `0 ≤ rank < filter_size`。这段把「rank / 中位数 / 百分位」三种语义统一到一个整数。

**端点短路**（连接模块 4.2）：

[`_filters.py:L1977-L1982`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L1977-L1982) —— `rank==0` 直接 `return minimum_filter(...)`、`rank==filter_size-1` 直接 `return maximum_filter(...)`。这就是「最小/最大 = 秩滤波端点」在代码里的直接体现，省去排序开销。

**一维快速路径的判定与 dtype 转换**：

[`_filters.py:L2001-L2022`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L2001-L2022) —— 关键条件 `input.ndim == 1 and ((lim2 >= 0) or (input.size == 1))`（`lim2 = input.size - ((footprint.size - 1)//2 - origin)`，排除「footprint 比数组还大」的边角情形，见 gh-23293）。满足后，按 dtype 把输入/输出转换到 C++ 支持的三种类型（`int64` / `float64` / `float32`）：`float16` 提升到 `float32`、其它整型提升到 `int64`、提升后的结果用 `np.copyto(..., casting='unsafe')` 写回原 dtype 的 `output`。

[`_filters.py:L2019-L2020`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L2019-L2020) —— 调用 C++ 内核 `_rank_filter_1d.rank_filter(x, rank, footprint.size, x_out, mode, cval, origin)`。注意它传的是 `footprint.size`（窗口宽度），意味着这条快速路径**只认一维、全宽窗口**。

[`_filters.py:L2023-L2024`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L2023-L2024) —— 否则（多维输入或不满足条件）走通用 C 内核 `_nd_image.rank_filter(input, rank, footprint, output, mode, cval, origins)`，它接受任意形状的 footprint。

**C++ 双堆 Mediator**（一维快速路径的灵魂）：

[`src/_rank_filter_1d.cpp:L14-L49`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/_rank_filter_1d.cpp#L14-L49) —— `Mediator` 类用一块连续 `int` 数组同时存 `pos`（每个值在堆里的位置）和 `heap`（堆里存的是数据下标）。构造时 `minCt = nItems - rank - 1`、`maxCt = rank`，把「比 rank 小的 `rank` 个元素」放进负下标的 max-heap，「比 rank 大的元素」放进正下标的 min-heap，`heap[0]` 恒为第 `rank` 小的那个元素。

[`src/_rank_filter_1d.cpp:L124-L160`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/_rank_filter_1d.cpp#L124-L160) —— `MediatorInsert` 在 O(log w) 内完成「新值入窗、旧值出窗、维护双堆」。它的三种分支（`p>0` 新值在 min-heap、`p<0` 在 max-heap、`p==0` 正好在 rank 位）分别做最小限度的堆调整。每次插入后 `data[m->heap[0]]` 就是当前窗口的第 `rank` 小。

[`src/_rank_filter_1d.cpp:L235-L241`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/_rank_filter_1d.cpp#L235-L241) —— 主循环里每读入一个新元素就写出一个结果：`out_arr[i - lim] = data[m->heap[0]]`，即「窗口第 rank 小」。

[`src/_rank_filter_1d.cpp:L201-L276`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/_rank_filter_1d.cpp#L201-L276) —— 边界处理：进入数组前按 `mode`（REFLECT/CONSTANT/NEAREST/MIRROR/WRAP）预填窗口，扫完数组后再按 `mode` 补尾部窗口。这正是 u1-l4 里 `_extend_mode_to_code` 编码出来的 5 种模式在 C 端的对应实现（注意此处 `Mode` 枚举只有 5 种，比 Python 端的 7 种少，因为一维秩滤波的 `is_filter=True` 已把 `grid-wrap`/`grid-constant` 归并掉了）。

#### 4.1.4 代码实践

**目标**：验证「median = rank(n//2) = percentile(50)」，并体会端点短路。

```python
import numpy as np
from scipy import ndimage

rng = np.random.default_rng(0)
a = rng.integers(0, 100, size=12).astype(np.int64)
print("input :", a)

size = 5                       # 窗口宽 5 → filter_size=5, n//2 = 2
med   = ndimage.median_filter(a, size=size)
p50   = ndimage.percentile_filter(a, 50, size=size)
r2    = ndimage.rank_filter(a, rank=2, size=size)
print("median:", med)
print("pctl50:", p50)
print("rank2 :", r2)
print("三者完全相等？", np.array_equal(med, p50) and np.array_equal(med, r2))

# 端点短路：rank=0 应与 minimum_filter 逐元素相同
print("rank0 == min ?", np.array_equal(
    ndimage.rank_filter(a, rank=0, size=size),
    ndimage.minimum_filter(a, size=size)))
print("rank4 == max ?", np.array_equal(
    ndimage.rank_filter(a, rank=4, size=size),
    ndimage.maximum_filter(a, size=size)))
```

**操作步骤**：直接运行。可改 `size=4`（偶数）观察 `median_filter` 取「下中位数」（第 `4//2=2` 个，而非第 1、2 两个的平均）。

**预期现象**：
- `median` / `pctl50` / `rank2` 三行**逐元素完全相同**（因为 `int(5*50/100)=2=5//2`）。
- `rank0 == min`、`rank4 == max` 均为 `True`（端点短路）。
- `size=4` 时 `median` 取排序后第 2 个（下标 `4//2=2`），与 `np.median` 的「中间两个取平均」不同。

> 待本地验证：C++ 快速路径仅在 `input.ndim == 1` 时触发；若把 `a` 改成 2-D（如 `size=(5,5)`），`median_filter` 会改走 `_nd_image.rank_filter`，结果数值一致但走的内核不同（可在 `_rank_filter` 的 L2002 条件处加打印验证）。

#### 4.1.5 小练习与答案

**练习 1**：`percentile_filter(a, -20, size=5)` 等价于 `percentile_filter(a, ?, size=5)`？

**答案**：等价于 `percentile=80`。源码 L1965–L1966：`percentile < 0` 时 `percentile += 100.0`，`-20 → 80`。

**练习 2**：`median_filter` 对 `float16` 输入会怎样？为什么文档推荐改用 `vectorized_filter(np.nanmedian)`？

**答案**：`median_filter` **不支持** `float16`（一维快速路径会把 `float16` 提升到 `float32` 再算，但文档明确声明不支持 `float16`、NaN 行为未定义、偶数窗口不返回常规中位数、内存随 `n**4` 增长，见 L2110–L2121）。`vectorized_filter` 因为把整块窗口交给 `np.nanmedian` 这种已向量化的函数，能同时解决 `float16`、NaN 忽略、偶数窗口常规中位数三个问题（见模块 4.4）。

**练习 3**：为什么 `_rank_filter` 在 L1994 用 `is_filter=True` 调 `_extend_mode_to_code`？

**答案**：回顾 u1-l4，`is_filter=True` 会把 `grid-wrap` / `grid-constant` 这两种「插值专用」模式归并到 C 内核能识别的 `wrap` / `constant` 码。秩滤波是纯滤波（不做样条插值），所以用 `is_filter=True` 复用同一套 C 边界处理。

---

### 4.2 minimum_filter / maximum_filter（_min_or_max_filter）

#### 4.2.1 概念说明

最小/最大滤波输出每个邻域内的最小/最大值。它们既是最常用的「顺序统计量滤波」，又是模块 4.1 的端点特例（`rank=0` / `rank=n-1`）。

它们相比 rank 滤波多一个**重要性质**：**可分离**（见 2.3）。这意味着矩形邻域（`size` 或全 True footprint）的最小/最大滤波，可以拆成逐轴 1-D 滤波，每轴用专门的 O(n) 算法（MINLIST/MAXLIST，与窗口大小无关的线性时间）。只有当 footprint 是任意形状（非全 True）或带 `structure`（灰度形态学偏置）时，才退化为不可分离，走 N-D 内核。

#### 4.2.2 核心流程

`_min_or_max_filter` 引擎流程：

```
1. size/footprint/structure 三者归一化，判定 separable 标志：
     structure is None 且 (footprint is None 或 footprint.all())  → separable=True
     否则（任意 footprint 或带 structure）                        → separable=False
2. 复数输入直接报错；准备 output；处理 input/output 内存别名
3. _check_axes 规范 axes
4. if separable:
     逐轴（跳过 size==1 的轴）调用 minimum_filter1d / maximum_filter1d
     用 input=output 链式就地复用同一缓冲
   else:
     _expand_footprint / _expand_origin 展开
     _nd_image.min_or_max_filter(...)   # 一次性 N-D
```

`minimum_filter1d` / `maximum_filter1d` 则是更薄的 1-D 外套，直接调 C 内核 `_nd_image.min_or_max_filter1d`，最后一个参数 `1` 表最小、`0` 表最大。

#### 4.2.3 源码精读

**两个 N-D 外套**：

[`_filters.py:L1873-L1874`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L1873-L1874) —— `minimum_filter` 调 `_min_or_max_filter(..., 1, axes)`（`minimum=1`）。

[`_filters.py:L1923-L1924`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L1923-L1924) —— `maximum_filter` 调 `_min_or_max_filter(..., 0, axes)`（`minimum=0`）。

**separable 判定**：

[`_filters.py:L1743-L1760`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L1743-L1760) —— 关键判定：`structure is None` 且 footprint 为空（用 `size`）或全 True（`footprint.all()`）时 `separable=True`；只要 footprint 有 False（任意形状）或带了 `structure`，就 `separable=False`。注意全 True footprint 会被改写成 `size = footprint.shape; footprint = None`，从而走可分离快路径。

**可分离路径（逐轴 1-D）**：

[`_filters.py:L1776-L1791`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L1776-L1791) —— 把 `origin/size/mode` 都 `_normalize_sequence` 到各轴，**丢掉 `size==1` 的轴**（窗口为 1 等于不滤），然后逐轴调用 `minimum_filter1d` 或 `maximum_filter1d`，用 `input = output` 让上一轴的输出成为下一轴的输入（就地链式，与 u2-l3 高斯滤波同款手法）。若无轴可滤，直接 `output[...] = input[...]`。

**不可分离路径（N-D 内核）**：

[`_filters.py:L1792-L1822`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L1792-L1822) —— `_expand_footprint`/`_expand_origin` 展开（u2-l2 的「展开三件套」之二，注意此处**不调用** `_expand_mode`，且 L1816–L1819 显式拒绝「逐轴 mode 序列」，与相关/卷积一致）；处理 `structure` 的灰度形态学偏置；最后 `_nd_image.min_or_max_filter(input, footprint, structure, output, mode, cval, origins, minimum)` 一次性算完。

**1-D 外套**：

[`_filters.py:L1676-L1677`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L1676-L1677) —— `minimum_filter1d` 调 `_nd_image.min_or_max_filter1d(input, size, axis, output, mode, cval, origin, 1)`，末位 `1` 表最小。文档（L1650–L1652）说明它实现 Harter 的 MINLIST 算法，**保证 O(n)**，与窗口大小无关。

[`_filters.py:L1733-L1734`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L1733-L1734) —— `maximum_filter1d` 同样调用，末位 `0` 表最大（MAXLIST 算法）。

#### 4.2.4 代码实践

**目标**：验证「矩形 footprint 的 N-D 最小滤波 = 逐轴 1-D 最小滤波」，并观察任意 footprint 退化为不可分离。

```python
import numpy as np
from scipy import ndimage

rng = np.random.default_rng(1)
img = rng.integers(0, 100, size=(6, 6)).astype(np.int64)
print("img:\n", img)

# (a) 矩形邻域：可分离
nd_min = ndimage.minimum_filter(img, size=3)                 # N-D，走可分离路径
ax_min = img.copy()
for axis in (0, 1):                                          # 手动逐轴 1-D
    ax_min = ndimage.minimum_filter1d(ax_min, 3, axis=axis)
print("矩形 size=3: N-D == 逐轴1-D ?", np.array_equal(nd_min, ax_min))

# (b) 任意 footprint：不可分离，不能拆成逐轴
cross = np.array([[0, 1, 0],
                  [1, 1, 1],
                  [0, 1, 0]], dtype=bool)
fp_min = ndimage.minimum_filter(img, footprint=cross)
print("十字 footprint 结果与矩形不同 ?", not np.array_equal(fp_min, nd_min))
# 试着用逐轴复现十字最小值——会失败，因为中位数/十字最小不可分离
```

**操作步骤**：运行后确认 (a) 相等、(b) 不等。可把 `cross` 换成 `np.ones((3,3), bool)`，此时 (b) 应与 (a) 完全相等（全 True footprint 被改写成 `size`，走可分离路径）。

**预期现象**：
- (a) `True`：矩形最小滤波可分离，两种调用殊途同归。
- (b) `True`（即「与矩形不同」）：十字邻域的最小值无法拆成逐轴。
- 把 footprint 换成全 True 后，`fp_min` 与 `nd_min` 逐元素相等。

> 待本地验证：若在 `_min_or_max_filter` 的 L1776（`if separable:`）前后各加一行 `print(separable)`，能看到矩形 footprint 打印 `True`、十字 footprint 打印 `False`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `minimum_filter` 不支持复数输入？

**答案**：L1766–L1767 直接 `if np.iscomplexobj(input): raise TypeError`。复数没有全序关系，无法定义「最小」。（`_rank_filter` 同样在 L1935–L1936 拒绝复数，排序同样需要全序。）

**练习 2**：`minimum_filter(img, size=3)` 和 `minimum_filter(img, size=(3,1))` 在 2-D 上结果有何不同？

**答案**：`size=(3,1)` 第二轴窗口为 1，L1781 的 `if sizes[ii] > 1` 会跳过该轴，只沿轴 0 做最小滤波——等价于 `minimum_filter1d(img, 3, axis=0)`。`size=3` 则广播成 `(3,3)`，两轴都滤。

---

### 4.3 generic_filter / generic_filter1d

#### 4.3.1 概念说明

前两节的滤波运算（求和、取最小、取第 rank 位）都写死在 C 内核里。如果运算无法预测——比如「邻域方差」「邻域出现最多的值」「自定义打分函数」——就需要一种「把任意运算塞进邻域循环」的机制。`generic_filter` / `generic_filter1d` 就是这个机制：C 负责遍历并把每个邻域（或每条线）的数据打包好，**调用一个用户给的函数**算出结果。

两者粒度不同：
- `generic_filter1d`：沿某条轴，**每条线**回调一次。回调签名是 `function(input_line, output_line)`，必须**就地修改** `output_line`。`input_line` 已按窗口大小和 `mode` 做了边界扩展。
- `generic_filter`：每个**元素**回调一次。回调签名是 `function(buffer)`，`buffer` 是该元素 footprint 内所有值组成的 1-D 数组，回调返回一个标量。

两个函数都支持 `scipy.LowLevelCallable`（C 回调），避免 Python 回调的解释器开销，适合性能敏感场景。

#### 4.3.2 核心流程

两个函数的 Python 层都很薄，几乎只做参数校验，真正的遍历在 C 内核：

```
generic_filter1d:
  校验 → _get_output → mode 编码 → _nd_image.generic_filter1d(...)
  （C 端逐线：把线拷进缓冲并按 mode 扩展 → 调 function(input_line, output_line)）

generic_filter:
  footprint 归一化 → 「展开三件套」(footprint/origin) → _get_output
  → mode 编码 → _nd_image.generic_filter(...)
  （C 端逐元素：把 footprint 内值填进 buffer → 调 function(buffer) → 写标量到输出）
```

`extra_arguments` / `extra_keywords` 是给回调的附加参数透传通道。

#### 4.3.3 源码精读

**`generic_filter1d` 的回调契约**：

[`_filters.py:L2189-L2294`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L2189-L2294) —— 文档（L2226–L2244）给出 C 回调签名 `int function(double *input_line, npy_intp input_length, double *output_line, npy_intp output_length, void *user_data)`，强调「必须就地修改 `output_line`」「返回 1 表成功、0 表失败」。Python 回调则按 `function(input_line, output_line)` 调用。L2291–L2293 把 `extra_arguments`/`extra_keywords` 一并传给 C 内核 `_nd_image.generic_filter1d`。

**`generic_filter` 的回调契约**：

[`_filters.py:L2341-L2366`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L2341-L2366) —— C 回调签名 `int callback(double *buffer, npy_intp filter_size, double *return_value, void *user_data)`：`buffer` 是 footprint 内的值，`filter_size` 是 True 的个数，结果写进 `return_value`。Python 回调按 `function(buffer)` 调用、返回标量。

**`generic_filter` 的实现（与 `_rank_filter` 高度同构）**：

[`_filters.py:L2410-L2446`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L2410-L2446) —— 同样是 `size/footprint` 二选一、`_expand_footprint`/`_expand_origin` 展开、origin 校验、`_get_output`、mode 编码，最后 `_nd_image.generic_filter(input, function, footprint, output, mode, cval, origins, extra_arguments, extra_keywords)`。把它和 `_rank_filter`（L1928）的中段并排看，会发现「参数预处理」几乎一字不差——这就是 u1-l4 讲的那套共享骨架。

#### 4.3.4 代码实践

**目标**：用 `generic_filter` 配合 `lambda` 计算 3×3 局部方差图（标准库没有现成的「邻域方差」滤波）。

```python
import numpy as np
from scipy import ndimage

rng = np.random.default_rng(2)
img = rng.integers(0, 50, size=(6, 6)).astype(np.float64)

# 局部方差：每个 3x3 邻域的方差
var_img = ndimage.generic_filter(img, np.var, size=3)
print("局部方差图:\n", np.round(var_img, 2))

# 对照：用 lambda 显式写一遍，并验证与 np.var 一致
var_img2 = ndimage.generic_filter(img, lambda buf: np.var(buf), size=3)
print("与 lambda 版一致 ?", np.allclose(var_img, var_img2))

# 用 extra_arguments 演示参数透传：算邻域分位数 p=0.9
q90 = ndimage.generic_filter(img, np.quantile, size=3,
                             extra_arguments=([0.9],))
print("邻域 0.9 分位:\n", np.round(q90, 1))
```

**操作步骤**：直接运行。`generic_filter` 会把每个 3×3 邻域（9 个值）打包成 1-D 数组传给 `np.var`。把 `size=3` 换成自定义 `footprint`（如上节十字形）再观察。

**预期现象**：
- `var_img` 每个点是该点 3×3 邻域的方差；与手动 `lambda` 版逐元素一致。
- `extra_arguments=([0.9],)` 让 `np.quantile(buf, [0.9])` 被调用——注意它返回长度 1 的数组，`generic_filter` 取其标量结果。

> 注意：`generic_filter` 对每个元素都回调一次 Python 函数，6×6 数组就回调 36 次，大图上很慢。下一节的 `vectorized_filter` 用向量化调用解决这个瓶颈。

> 待本地验证：`np.quantile(buf, [0.9])` 返回的是数组而非标量，`generic_filter` 实际取其首元素；不同 NumPy 版本行为可能略有差异，建议改用返回标量的 `lambda buf: np.quantile(buf, 0.9)`。

#### 4.3.5 小练习与答案

**练习 1**：`generic_filter1d` 的回调为什么必须「就地修改 `output_line`」而不是 `return`？

**答案**：C 内核把输出线的 C 缓冲直接传给回调（见 L2226–L2244 的签名 `double *output_line`），回调把结果写进这块缓冲，内核再整体拷回输出数组。若用 `return`，内核拿不到数据。Python 回调同样遵守「改 `output_line[:] = ...`」的契约（见 L2270–L2271 的官方示例）。

**练习 2**：什么场景该用 `LowLevelCallable` 而不是 Python 函数？

**答案**：当 `function` 很轻（如求和、极值）但数组很大时，Python 回调的每次解释器往返会成为瓶颈。`LowLevelCallable` 直接在 C 端调用一个函数指针，省掉往返。文档（L2337–L2339）明确推荐：纯 Python 向量化用 `vectorized_filter`，C 回调用 `generic_filter` + `LowLevelCallable`。

---

### 4.4 vectorized_filter

#### 4.4.1 概念说明

`vectorized_filter` 是本单元里**最年轻、最不一样**的函数：它根本不走 `_nd_image` 那套 C 邻域循环。思路极其直白——

> 先用 `np.pad` 按 `mode` 给数组加边界，再用 `np.lib.stride_tricks.sliding_window_view` 一次性切出所有窗口，然后把这一整块窗口**作为一个数组**喂给一个**已向量化**的可调用对象（如 `np.mean`、`np.nanmedian`、`np.quantile`）。

这个「暴力但向量化」的思路带来了 `median_filter` / `generic_filter` 都做不到的能力：

- **`float16` 与复数 dtype**：因为全程在 NumPy（或 CuPy）数组上运算，dtype 由 `function` 决定，没有 C 内核的 dtype 白名单。
- **NaN 控制**：只要 `function` 自己会处理 NaN（如 `np.nanmedian`、`np.nanmean`），滤波就能「忽略 NaN」——而 `median_filter` 的 NaN 行为是未定义的。
- **偶数窗口的常规中位数**：`np.median` 对偶数个元素取中间两个的平均，符合常规定义（`median_filter` 取下中位数）。
- **多输出**：`function` 可以一次返回多个量（如同时返回 25/50/75 分位）。
- **内存控制**：`batch_memory` 参数限制每块窗口数组的最大字节数。
- **`'valid'` 模式**：不扩展边界，而是缩小输出形状。

代价是：它是**暴力法**，每个窗口都重新算一遍（不像 `minimum_filter` 有 O(n) 增量算法），冗余计算多；文档（L306–L310）建议「有专用滤波就用专用的」。

#### 4.4.2 核心流程

```
1. _vectorized_filter_iv(...)  做全部输入校验，返回规范化的一组值，
   其中关键的是 wrapped_function（已包好 footprint 选择、内存分批、output 填充）
2. 把 axes 移到末尾（moveaxis），方便 sliding_window_view 在末尾切窗
3. 按 mode 给 input 加边界（np.pad），origin 决定左右各加多少
4. swv = sliding_window_view(bordered_input, size, working_axes)  # 切出所有窗口
5. res = wrapped_function(view)   # 一次向量化调用
6. moveaxis 把结果轴挪回原位
```

`_vectorized_filter_iv` 内部还会把 `function` 包两层：
- 若给了 `footprint`，包成 `footprinted_function`，先 `input[..., footprint]` 选出要参与运算的元素；
- 再包成 `wrapped_function`，按 `batch_memory` 把窗口维度分块，逐块调 `footprinted_function`，控制峰值内存。

#### 4.4.3 源码精读

**主体（极简，复杂度都在校验里）**：

[`_filters.py:L434-L480`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L434-L480) —— L434–L436 调 `_vectorized_filter_iv` 拿到规范化输入和 `wrapped_function`；L467–L472 按 `mode`（已映射成 `np.pad` 认识的名字）和 `origin` 算出每轴左右各加多少，`np.pad` 加边界；L476 `sliding_window_view` 切出全部窗口；L477 `function(view)` 一次调用；L480 `moveaxis` 把窗口轴挪回原 `axes` 位置。注意 `'valid'` 模式（L471–L472）不加边界，输出形状自然变小。

**`footprint` 选择**：

[`_filters.py:L84-L96`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L84-L96) —— 若给 `footprint`，先 `size = footprint.shape`，再把 `function` 包成 `footprinted_function`，内部 `function(input[..., footprint], ..., axis=-1, **kwargs)`——即用布尔索引从窗口里挑出 True 位置的元素再交给用户函数。

**mode 名字映射**：

[`_filters.py:L131-L140`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L131-L140) —— `vectorized_filter` 支持 9 种 mode（比其它函数多一个 `'valid'`），并把 ndimage 名字映射成 `np.pad` 名字（如 `nearest→edge`、`reflect→symmetric`、`mirror→reflect`）。这是它能「不依赖 C 内核的边界扩展」的关键——边界扩展交给 NumPy/CuPy 的 `pad`。

**内存分批与 output 填充**：

[`_filters.py:L169-L199`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L169-L199) —— `wrapped_function` 先按 `batch_memory` 算每批处理几条窗口（L176–L177），若一批就能装下就直接整块调（L181–L186），否则沿窗口维分块循环（L188–L198）。若 `output is None`，第一批调用还顺带探测输出 dtype（L193–L194），再分配数组。这套机制让大图不会因窗口视图过大而 OOM。

**CuPy 支持**：

[`_filters.py:L450-L463`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L450-L463) —— 若 `xp` 是 CuPy，用 `cp.lib.stride_tricks.sliding_window_view` 和 `cp.pad`，整个滤波在 GPU 上完成（CuPy 是目前唯一同时有这两种 API 的 GPU 后端）。

#### 4.4.4 代码实践

**目标**：体会 `vectorized_filter` 在「含 NaN 的偶数窗口中位数」上相对 `median_filter` 的优势。

```python
import numpy as np
from scipy import ndimage

rng = np.random.default_rng(3)
a = rng.integers(0, 100, size=10).astype(np.float64)
a[3] = np.nan                      # 人为埋一个 NaN
print("input        :", a)

# median_filter：NaN 行为未定义（通常会被污染）
m1 = ndimage.median_filter(a, size=4)
print("median_filter:", m1)

# percentile_filter(50)：同样未定义 NaN 行为
p1 = ndimage.percentile_filter(a, 50, size=4)
print("pctl_filter  :", p1)

# vectorized_filter + np.nanmedian：忽略 NaN，且偶数窗口取常规中位数
v1 = ndimage.vectorized_filter(a, function=np.nanmedian, size=4)
print("vec nanmedian:", v1)

# 对照：np.nanmedian 对 [x0,x1,x2,x3] 取中间两个的平均（常规中位数）
print("vec median   :", ndimage.vectorized_filter(a, function=np.median, size=4))
```

**操作步骤**：运行后比较四行结果。可把 `size=4` 改成 `size=3`（奇数）观察差异缩小，再把 `a` 转成 `float16`（`a.astype(np.float16)`）验证 `vectorized_filter` 能跑而 `median_filter` 不被推荐。

**预期现象**：
- `median_filter` / `percentile_filter` 在含 NaN 时输出被「污染」（NaN 附近出现 NaN 或异常值），因为它们没设计 NaN 处理。
- `vec nanmedian` 在 NaN 附近仍给出合理中位数（`np.nanmedian` 忽略 NaN）。
- `vec median`（不带 nan）会传播 NaN，与 `median_filter` 类似——说明 NaN 控制权完全在 `function` 手里。
- `float16` 输入下 `vectorized_filter` 正常返回 `float16`（dtype 由 `np.nanmedian` 决定）。

> 待本地验证：具体 NaN 传播范围取决于边界模式与窗口位置；建议在 REPL 里逐元素核对 `[i-1:i+3]` 窗口与输出 `[i]` 的对应关系。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `vectorized_filter` 没有 `extra_arguments` / `extra_keywords`？

**答案**：文档（L318–L327）给两个理由：① 用户可以用 `lambda` 自己包一层透传参数；② 这两个参数名被「预留」给未来「把额外滑动窗口数据传给 `function`」的特性，避免接口重复。所以需要附加参数时，写 `function=lambda x, axis: myfunc(x, *args, axis=axis, **kwargs)`。

**练习 2**：`vectorized_filter(img, np.min, size=3)` 和 `minimum_filter(img, size=3)` 结果相同吗？该用哪个？

**答案**：数值相同（都是 3×3 邻域最小），但应优先 `minimum_filter`——它用 O(n) 的 MINLIST 算法，而 `vectorized_filter` 是暴力法，每个窗口重算，慢得多、内存也大（文档 L306–L310 明确建议「有专用滤波就用专用的」）。`vectorized_filter` 的价值在「没有专用滤波」时（如 `np.nanmedian`、自定义多输出）。

**练习 3**：`mode='valid'` 时输出形状如何变化？

**答案**：不加边界，输出沿每个 `axes` 维缩小 `window_size - 1`（文档 L263–L269 给出公式 `output_shape[axes] -= (window_size - 1)`）。源码 L142–L143 还校验 `mode='valid'` 与 `origin` 不兼容（用了 origin 就不能 valid）。

---

## 5. 综合实践

把本讲四个模块串起来：对同一张含噪声与小亮斑的图，分别用「专用」「回调」「向量化」三档实现「3×3 局部标准差」滤波，并对比它们的结果、速度与可控性。

```python
import numpy as np
from scipy import ndimage

rng = np.random.default_rng(42)
img = np.ones((20, 20)) * 10
img[5:8, 5:8] = 80                      # 一块亮斑
img += rng.normal(0, 2, size=img.shape) # 加噪

# ---- 档 1：用 generic_filter 回调 np.std（每元素回调一次）----
res_generic = ndimage.generic_filter(img, np.std, size=3)

# ---- 档 2：用 vectorized_filter 向量化 np.std（一次调用）----
res_vec = ndimage.vectorized_filter(img, function=np.std, size=3)

# ---- 档 3：对照——专用滤波做不了 std，但能做 min/max，验证三者框架一致----
res_max = ndimage.maximum_filter(img, size=3)
res_max_v = ndimage.vectorized_filter(img, function=np.max, size=3)

print("generic vs vectorized (std) 一致 ?", np.allclose(res_generic, res_vec))
print("maximum_filter vs vectorized(max) 一致 ?", np.allclose(res_max, res_max_v))
print("亮斑处 std 偏大（边缘响应）:", np.round(res_vec[6, 6], 2))

# 现在埋一个 NaN，体会三档的差异
img[10, 10] = np.nan
print("\n含 NaN 后:")
print(" generic_filter(np.std) :", np.round(ndimage.generic_filter(img, np.std, size=3)[10, 10], 2))
print(" vectorized(np.nanstd)  :", np.round(
    ndimage.vectorized_filter(img, function=np.nanstd, size=3)[10, 10], 2))
```

完成后请回答：

- 「专用」档为何做不了「局部标准差」？因为没有写死的「std」C 内核——这正是 `generic_filter` / `vectorized_filter` 存在的意义（让用户自定义运算）。
- `generic_filter` 与 `vectorized_filter` 在无 NaN 时结果一致（都算 3×3 标准差），但含 NaN 后前者（`np.std`）会传播 NaN，后者只要换成 `np.nanstd` 就能忽略 NaN——体会「NaN 控制权在 `function`」的设计。
- （进阶）用 `%timeit` 对比 `generic_filter(img, np.std, size=3)` 与 `vectorized_filter(img, np.std, size=3)` 在 200×200 图上的耗时，体会「每元素回调」vs「一次向量化调用」的速度差。

这个综合实践一次性覆盖了：① 秩/选择滤波作为「顺序统计量」一族，端点短路到 min/max；② min/max 的可分离性；③ `generic_filter` 的「每元素回调」机制；④ `vectorized_filter` 的「向量化 + NaN/dtype 可控」机制；⑤ 三档滤波在结果一致性与可控性上的取舍。

## 6. 本讲小结

- `rank_filter` / `median_filter` / `percentile_filter` 是同一引擎 `_rank_filter` 的三件外套，差别只在「如何把人的输入换算成整数 `rank`」：中位数取 `n//2`、百分位取 `int(n*p/100)`、秩直接用。
- `_rank_filter` 有两段关键分支：**端点短路**（`rank==0`→`minimum_filter`、`rank==n-1`→`maximum_filter`）和**一维快速路径**（一维输入且 dtype合法→C++ 双堆 `_rank_filter_1d.rank_filter`，否则→通用 `_nd_image.rank_filter`）。
- `_rank_filter_1d.cpp` 的 `Mediator` 用「max-heap + min-heap + rank 位」双堆结构，把每个窗口的第 `rank` 小更新做到 O(log w)，总体 O(n log w)，并自带 5 种边界模式的前/后填充。
- `minimum_filter` / `maximum_filter` 共用 `_min_or_max_filter`；矩形邻域（可分离）逐轴调用 1-D `min_or_max_filter1d`（O(n) 的 MINLIST/MAXLIST），任意 footprint 或带 `structure` 才走 N-D `_nd_image.min_or_max_filter`。
- `generic_filter`（每元素回调 `function(buffer)`）与 `generic_filter1d`（每线回调 `function(input_line, output_line)`，须就地改）把任意运算塞进 C 邻域循环，支持 `LowLevelCallable`，但 Python 回调有开销。
- `vectorized_filter` 走另一条路：`np.pad` + `sliding_window_view` + 一次向量化调用，从而支持 `float16`/复数/NaN 控制/偶数窗口常规中位数/多输出/`batch_memory`，但属暴力法，有专用滤波时应优先用专用的。

## 7. 下一步学习建议

本讲把「加权求和」之外的另一大类滤波——「顺序统计量 / 回调 / 向量化」——讲完了，u2 滤波单元只剩频域一讲。建议接着学习：

- **u2-l6 频域（傅里叶）滤波**：从空域（本讲的邻域运算）跳到频域（`fourier_gaussian` 等做 FFT 域的乘法核），看高斯平滑在频域如何等价实现，与本单元 u2-l3 的 `gaussian_filter` 互为对照。
- **u4 测量与连通区域**：本讲的 `minimum_filter` / `maximum_filter` / `rank_filter` 常用于「预处理」（去噪、增强）后再做 `label` / `find_objects`；学完 u4 能把「滤波→分割→统计」串成完整流水线。
- **u6 专家单元（C 内核）**：想下探的读者，学完本讲可去 `src/ni_filters.c`（u6-l3）看 `_nd_image.rank_filter` / `min_or_max_filter` 这些 N-D 内核在 C 端如何用 `NI_FilterIterator` 遍历邻域；本讲的 `_rank_filter_1d.cpp` 则是 u6-l4「非 `_nd_image` 扩展」的范例。
