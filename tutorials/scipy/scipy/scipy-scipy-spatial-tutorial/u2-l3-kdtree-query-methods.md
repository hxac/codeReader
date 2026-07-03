# KDTree 查询方法全景

## 1. 本讲目标

学完本讲后，你应该能够：

- 列举 `KDTree` 的七类查询接口，并能说清它们各自回答的问题（最近邻 / 球形邻域 / 成对 / 计数 / 距离矩阵）。
- 区分每个查询方法的语义与输出形态：返回的是「索引」「距离」「配对集合」还是「稀疏矩阵」。
- 理解 `workers`、`weights`、`cumulative`、`output_type`、`return_sorted`、`return_length` 等关键参数的含义与适用场景。
- 顺着 `KDTree`（Python 薄封装）→ `cKDTree`（Cython）→ `with nogil` 调用 C++ 内核这条调用链，看懂一次查询在源码里是如何分发与剪枝的。
- 针对一个真实的「统计两点云之间近邻配对」问题，正确在 `count_neighbors` 与 `sparse_distance_matrix` 之间做选择。

## 2. 前置知识

本讲承接 u2-l1（KDTree/cKDTree 入门）与 u2-l2（Rectangle 与 minkowski 距离）。回顾三个要点：

1. **kd-tree 的价值**：它把最近邻查询从朴素 \(O(n)\) 压到约 \(O(\log n)\)，靠的是「节点对应的超矩形到查询点的最小距离」做整子树剪枝（见 u2-l2 的 `Rectangle.min_distance_point`）。
2. **继承关系**：`KDTree` 是 `cKDTree` 的纯 Python 子类，真正建树与查询都落在 Cython 内核 `cKDTree`，`KDTree` 的查询方法只做参数校验再 `super().xxx(...)` 转发。本讲会反复看到这种「Python 校验 → Cython 执行」的分层。
3. **闵可夫斯基范数 \(p\)**：\(p=1\) 曼哈顿距离、\(p=2\) 欧氏距离、\(p=\infty\) 切比雪夫距离。所有查询方法都接受 `p` 参数。

再补两个本讲会用到的术语：

- **单点查询 vs 批量查询**：传给 `query` 的 `x` 可以是单个向量（一维数组）或一组向量（二维数组），输出形状会随之变化。
- **dual-tree（双树）算法**：当查询两端都是 kd-tree 时，可以同时遍历两棵树，用「两棵子树之间的最近/最远距离」双向剪枝，比「逐点查询」快得多。`query_ball_tree`、`query_pairs`、`count_neighbors`、`sparse_distance_matrix` 都属于这一类。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用到的部分 |
| --- | --- | --- |
| [`_kdtree.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_kdtree.py) | `KDTree`（Python 薄封装）与 `distance_matrix` 函数 | 7 个查询方法的 Python 签名、docstring、校验与 `super()` 转发 |
| [`_ckdtree.pyx`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_ckdtree.pyx) | `cKDTree`（Cython 真正实现） | 每个方法的 Cython 主体、`get_num_workers`、`_run_threads`、`coo_entries`/`ordered_pairs` 结果容器 |

> 说明：`KDTree` 的每个查询方法在 `_kdtree.py` 里都很短，关键逻辑都在 `_ckdtree.pyx` 里。本讲会两边对照阅读：先看 Python 层做了什么校验，再看 Cython 层如何调用 C++ 内核。C++ 内核（`ckdtree/src/query.cxx` 等）留到 u8-l2 详讲。

## 4. 核心概念与源码讲解

先给一张「查询方法速查表」，建立全局印象，下面再逐模块拆解。

| 方法 | 回答的问题 | 输入另一端 | 典型输出 | 支持 `workers` 并行 |
| --- | --- | --- | --- | --- |
| `query` | 离我最近的 k 个是谁 | 单点/多点 `x` | 距离 `d` + 索引 `i` | ✅ |
| `query_ball_point` | 半径 r 内都有谁 | 单点/多点 `x` | 索引列表（可只返回长度） | ✅ |
| `query_ball_tree` | 两棵树之间距离 ≤ r 的配对 | 另一棵树 | 嵌套列表 `results[i]=[j,...]` | ❌（双树已剪枝） |
| `query_pairs` | 同一棵树内部距离 ≤ r 的配对 | 自身 | `set` 或 `ndarray`，且 `i<j` | ❌ |
| `count_neighbors` | 两棵树之间距离 ≤ r 的**配对数**（可加权） | 另一棵树 | 标量或一维数组 | ❌ |
| `sparse_distance_matrix` | 两棵树之间距离 ≤ r 的**稀疏距离矩阵** | 另一棵树 | `dok_array`/`coo_array`/`dict`/`ndarray` | ❌ |
| `distance_matrix`（模块级函数） | 两点集之间的**稠密**距离矩阵 | 点集 `y` | `(M,N)` ndarray | ❌（已弃用） |

记住一个核心分野：**带 `workers` 的（`query`、`query_ball_point`）是「逐点查询」**，可把大批量查询点切片分给多线程；**不带 `workers` 的四个是「双树查询」**，靠两棵树同时下钻做剪枝，单线程已足够高效。

### 4.1 最近邻与球形邻域：query / query_ball_point

#### 4.1.1 概念说明

- `query(x, k)`：最近邻查询。给定查询点 `x`，返回树中离它最近的 `k` 个点的距离与索引。这是「**离我最近的是谁**」型问题。
- `query_ball_point(x, r)`：球形邻域查询。给定查询点和半径 `r`，返回所有落在以 `x` 为心、`r` 为半径的「球」（\(L_p\) 意义下）内的点索引。这是「**半径 r 内都有谁**」型问题。

两者的差别在于「要多少」：`query` 要「最近的 k 个」，`query_ball_point` 要「距离阈值内的全部」。

#### 4.1.2 核心流程

`query` 的近似与剪枝由两个参数控制：

- `eps`（近似容差）：第 k 个返回点保证不超过真实第 k 近的 \((1+\text{eps})\) 倍。`eps>0` 会放过更多子树，换速度。
- `distance_upper_bound`：只返回距离小于它的邻居，用于给一串连续查询提供上界来提前剪枝；找不到的点距离记为 `inf`、索引记为 `self.n`（哨兵值，表示「越界」）。

`query_ball_point` 多了 `return_sorted`（是否对返回的索引排序）与 `return_length`（只返回数量、不返回索引列表，省内存）。它的剪枝同样靠 `eps`：子树最近点若已超过 `r/(1+eps)` 则整树跳过。

两者都支持 `workers`，把 `n` 个查询点切成 `n_jobs` 段并行查询。切分逻辑见 4.1.4 的 `_run_threads`。

#### 4.1.3 源码精读

**Python 层**（`KDTree.query`）只做两件校验，然后转发：

[`_kdtree.py`:L445-L560](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_kdtree.py#L445-L560) —— `KDTree.query` 的签名与 docstring；核心实现在末尾几行：

```python
x = np.asarray(x)
if x.dtype.kind == 'c':
    raise TypeError("KDTree does not work with complex data")
if k is None:
    raise ValueError("k must be an integer or a sequence of integers")
d, i = super().query(x, k, eps, p, distance_upper_bound, workers)
```

注意 `k` 可以是整数（取前 k 近）也可以是**整数序列**（如 `k=[1,3]` 表示要第 1 近和第 3 近）。

**Cython 层**（`cKDTree.query`）才是真正的执行体：

[`_ckdtree.pyx`:L813-L860](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_ckdtree.pyx#L813-L860) —— 这段把 `x` 拍平成 `(n, m)` 的连续 `float64` 数组，预分配结果数组，然后定义一个闭包 `_thread_func`，在 `with nogil` 块里调用 C++ 内核 `query_knn`：

```cython
np.intp_t num_workers = get_num_workers(workers, kwargs)
...
def _thread_func(np.intp_t start, np.intp_t stop):
    cdef:
        np.float64_t *pdd = &dd[start,0]
        np.intp_t *pii = &ii[start,0]
        const np.float64_t *pxx = &xx[start,0]
    with nogil:
        query_knn(cself, pdd, pii,
            pxx, stop-start, pkk, kk.shape[0], kmax, eps, p, distance_upper_bound)

_run_threads(_thread_func, n, num_workers)
```

可以看到：每个线程拿到一段查询点 `[start, stop)`，把结果直接写进预分配的 `dd`/`ii` 缓冲区——零拷贝、无锁（各线程写不同区段）。C++ 函数 `query_knn` 本身定义在 `ckdtree/src/query.cxx`，本讲只关注它「填满 dd/ii」的契约。

`k` 是序列时的优化也在这段：先做一次 `arange(max(k))` 的完整查询，但只保留请求列（`np.arange(1, k+1)` 与 `kk`），以减少内存。

**并行切分** `_run_threads`：

[`_ckdtree.pyx`:L1641-L1657](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_ckdtree.pyx#L1641-L1657) —— 把 `n` 个点均分给 `n_jobs` 个线程；当 `n_jobs<=1` 时退化为直接同步调用：

```cython
n_jobs = min(n, n_jobs)
if n_jobs > 1:
    ranges = [(j * n // n_jobs, (j + 1) * n // n_jobs) for j in range(n_jobs)]
    threads = [threading.Thread(target=_thread_func, args=(start, end))
               for start, end in ranges]
    ...
else:
    _thread_func(0, n)
```

`get_num_workers` 负责把 `workers` 归一化：`None`→1，`-1`→`os.cpu_count()`，其余必须为正整数（[`_ckdtree.pyx`:L388-L408](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_ckdtree.pyx#L388-L408)）。

`query_ball_point` 的 Python/Cython 结构几乎一样（[`_kdtree.py`:L562-L637](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_kdtree.py#L562-L637) → [`_ckdtree.pyx`:L879-L1025](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_ckdtree.pyx#L879-L1025)），区别在于结果是「变长索引列表」而非定长 `k`，所以 C++ 内核把每个查询点的命中点写进一个 `std::vector<std::vector<npy_intp>>`，再由 `_thread_func` 把它拷成 Python `list`（`return_length=True` 时则只取 `.front()` 的计数值，跳过建表）。

#### 4.1.4 代码实践

**实践目标**：用 `query` 与 `query_ball_point` 在同一棵树上查询，并验证 `workers` 对批量查询的加速。

```python
# 示例代码
import numpy as np
from scipy.spatial import KDTree

rng = np.random.default_rng(0)
data = rng.random((50_000, 3))
tree = KDTree(data)

queries = rng.random((20_000, 3))

# 1) 最近邻：k=3
d, i = tree.query(queries, k=3)
print(d.shape, i.shape)          # 预期 (20000, 3) (20000, 3)

# 2) 球形邻域：半径 0.05 内的点数（return_length 更省内存）
counts = tree.query_ball_point(queries, r=0.05, return_length=True, workers=-1)
print(counts.shape, counts.mean())
```

**操作步骤**：把上面的代码存为 `q.py` 运行；再用 `%timeit`（或 `time.perf_counter`）分别给 `query(..., workers=1)` 与 `query(..., workers=-1)` 计时。

**需要观察的现象**：
- 单点查询时（`queries` 只有一行），`d`/`i` 会被挤压成 0 维标量（见 `_ckdtree.pyx` 的 `single` 与 `nearest` 分支）。
- 批量查询时，`workers=-1` 应明显快于 `workers=1`（具体加速比取决于 CPU 核数与树规模）。

**预期结果**：`d.shape == (20000, 3)`；`counts` 是长度 20000 的一维整数数组。加速比为「待本地验证」（随机器核数变化）。

#### 4.1.5 小练习与答案

**练习 1**：`tree.query([0.5, 0.5, 0.5], k=1)` 返回的 `i` 是什么类型？为什么不是 numpy 标量？

**答案**：返回 Python `int`。因为在 `_ckdtree.pyx` 的 `query` 中，当 `single and nearest`（单点 + k=1）时，会执行 `iiret = int(iiret)`，把 numpy 标量显式转成 Python 标量，方便无 NumPy 依赖的代码使用。

**练习 2**：若想让 `query_ball_point` 的返回索引按距离排序，应设哪个参数？默认值行为是什么？

**答案**：设 `return_sorted=True`。默认 `return_sorted=None`：单点查询不排序，多点查询才排序（保持旧版本兼容，见 `_ckdtree.pyx` 的 `sort_output = return_sorted or (return_sorted is None and x_arr.ndim > 1)`）。

### 4.2 双树成对查询：query_ball_tree / query_pairs

#### 4.2.1 概念说明

- `query_ball_tree(other, r)`：在「本树」与「另一棵树」之间，找出所有距离 ≤ r 的配对。结果 `results[i]` 是本树第 `i` 个点在另一棵树里的邻居索引列表。
- `query_pairs(r)`：在「同一棵树」内部，找出所有距离 ≤ r 的点对，且自动去重为 `i<j`。

可以把 `query_pairs(r)` 理解为 `query_ball_tree(self, r)` 再去掉 `i==j` 与重复对（`(i,j)` 和 `(j,i)` 只留一个）。两者都用双树算法：同时递归两棵树，用 `Rectangle` 之间的最近/最远距离做双向剪枝，所以**不需要 `workers`**。

#### 4.2.2 核心流程

双树剪枝的关键不等式（承接 u2-l2 的超矩形距离）：

\[
d_{\min}(R_1, R_2) = \big\|\,\text{超出量}\,\big\|_p,\quad
\text{超出量}_i=\max(0,\,\text{mins}_1^{(i)}-\text{maxes}_2^{(i)},\,\text{mins}_2^{(i)}-\text{maxes}_1^{(i)})
\]

- 若 \(d_{\min}(R_1,R_2) > r\)：两棵子树之间不可能有任何配对，整对剪掉。
- 若 \(d_{\max}(R_1,R_2) \le r\cdot(1+\text{eps})\)：两棵子树之间所有点两两都在阈值内，可以「整块累加」（bulk add），不必逐点比较。

`query_pairs` 在 C++ 里把命中对写入一个 `ordered_pairs` 容器，保证 `i<j`；`output_type='set'` 返回 Python `set`，`'ndarray'` 返回 `(N,2)` 数组。

#### 4.2.3 源码精读

**`query_ball_tree`** 的 Cython 主体很简洁：

[`_ckdtree.pyx`:L1082-L1118](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_ckdtree.pyx#L1082-L1118) —— 校验两树维度一致后，分配 `n` 个 `std::vector`，在 `nogil` 块里调用 C++ `query_ball_tree`，再把每个 vector 拷成 Python list：

```cython
if self.m != other.m:
    raise ValueError("Trees passed to query_ball_tree have different dimensionality")
...
vvres.resize(n)
with nogil:
    query_ball_tree(self.cself, other.cself, r, p, eps, vvres.data())
# store the results in a list of lists
results = n * [None]
for i in range(n):
    ...  # 把 vvres[i] 拷成 tmp
```

注意它**没有 `workers` 参数**，也没有 `_run_threads`——单次双树遍历已经足够快。

**`query_pairs`** 用的是另一个结果容器 `ordered_pairs`：

[`_ckdtree.pyx`:L1174-L1186](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_ckdtree.pyx#L1174-L1186) —— C++ `query_pairs` 把去重后的 `(i,j)` 对写进 `results.buf`，再按 `output_type` 转成 `set` 或 `ndarray`：

```cython
cdef ordered_pairs results
results = ordered_pairs()
with nogil:
    query_pairs(self.cself, r, p, eps, results.buf)
if output_type == 'set':
    return results.set()
elif output_type == 'ndarray':
    return results.ndarray()
```

`ordered_pairs` 容器本身定义在 [`_ckdtree.pyx`:L245-L298](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_ckdtree.pyx#L245-L298)：它持有一个 `std::vector<ordered_pair>*buf`，`ndarray()` 通过 `__array_interface__` 把 C++ 内存零拷贝暴露成 NumPy 数组，`set()` 则逐对 `add((i,j))`。

> **Python 层**（[`_kdtree.py`:L639-L736](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_kdtree.py#L639-L736)）几乎是纯转发：`query_ball_tree` 直接 `return super().query_ball_tree(other, r, p, eps)`；`query_pairs` 直接 `return super().query_pairs(r, p, eps, output_type)`，没有额外校验。

#### 4.2.4 代码实践

**实践目标**：验证「`query_pairs` 等于去重后的 `query_ball_tree(self, ...)`」。

```python
# 示例代码
import numpy as np
from scipy.spatial import KDTree

rng = np.random.default_rng(1)
pts = rng.random((200, 2))
tree = KDTree(pts)

r = 0.08

# 方式 A：query_pairs，直接得到去重的 (i<j) 对
pairs = tree.query_pairs(r, output_type='set')

# 方式 B：query_ball_tree(self)，手动去重
raw = tree.query_ball_tree(tree, r)
pairs_b = set()
for i, js in enumerate(raw):
    for j in js:
        if i < j:                 # 去掉 i==j 和 (j,i)
            pairs_b.add((i, j))

print(len(pairs), len(pairs_b), pairs == pairs_b)   # 预期三者一致
```

**需要观察的现象**：`pairs == pairs_b` 应为 `True`，且 `len(pairs)==len(pairs_b)`。

**预期结果**：两个集合完全相等。这验证了 `query_pairs` 在语义上就是「`query_ball_tree(self,r)` 去重后的 `i<j` 对」。具体对数「待本地验证」（取决于随机点）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `query_ball_tree` 不接受 `workers` 参数，而 `query_ball_point` 接受？

**答案**：`query_ball_point` 是「逐点」查询（树固定，查询点可批量），可以把查询点切片并行；`query_ball_tree` 是「双树」查询，靠两棵树同时下钻做剪枝，本身就是单次高效遍历，再切片并行反而增加同步开销，所以没有 `workers`。

**练习 2**：`query_pairs` 默认返回什么容器？为什么处理上百万对时可能想换 `output_type`？

**答案**：默认返回 Python `set`，元素是 `(i,j)` 元组。对数极大时，`set` 的内存与哈希开销高，可改用 `output_type='ndarray'` 拿到 `(N,2)` 的连续数组，更省内存、更适合后续向量化处理。

### 4.3 计数与距离矩阵：count_neighbors / sparse_distance_matrix / distance_matrix

#### 4.3.1 概念说明

这三个都围绕「配对计数 / 距离」，但产物不同：

- `count_neighbors(other, r)`：只数「距离 ≤ r 的配对有多少个」，**不**保留是哪些对。支持加权（数权重的乘积之和）和多个半径 `r`（一次遍历得到多个计数的数组）。等价于「`query_ball_tree` 各列表长度之和」，但快得多。
- `sparse_distance_matrix(other, max_distance)`：返回距离 ≤ `max_distance` 的**稀疏距离矩阵**（只存非零项，即近邻对的距离值）。是 `count_neighbors` 的「细化版」——既要计数，也要知道每对的距离。
- `distance_matrix(x, y)`（模块级函数）：返回 `x`、`y` 之间**全部**两两距离的**稠密** `(M,N)` 矩阵，不带剪枝。**自 1.18.0 起弃用**，新代码应改用 `scipy.spatial.distance.cdist`。

选型直觉：只要「数量」用 `count_neighbors`；要「稀疏的成对距离」用 `sparse_distance_matrix`；要「完整稠密矩阵」用 `distance.cdist`（不再用 kd-tree 的 `distance_matrix`）。

#### 4.3.2 核心流程

`count_neighbors` 有两个值得细看的设计：

**(a) 半径 `r` 的去重与 \(r^p\) 变换**。`r` 可以是标量或一维数组。源码先用 `np.unique` 去重（避免对相同半径重复查询），再把每个阈值在内部表示成 \(r^p\)（因为 C++ 内核全程用距离的 \(p\) 次幂比较，省去开方）：

\[
r_i^{\text{internal}} = (r_i)^p \quad (p<\infty)
\]

**(b) `cumulative` 开关与两种算法**。`cumulative=True`（默认）返回累积计数（`result[i]` 是距离 ≤ `r[i]` 的配对数），适合少量半径；`cumulative=False` 时 `r` 被当作**分箱边界**，`result[i]` 是落在 \((r[i-1], r[i]]\) 区间内的配对数，且算法对「大量分箱」做了优化——计算时间不随分箱数线性增长。这正是宇宙学两点相关函数（Landy-Szalay 估计）所需要的形式。

加权时，计数变成「权重的乘积之和」，结果类型从整数变浮点。

`sparse_distance_matrix` 的 C++ 内核把每对命中 `(i, j, 距离值)` 写进 `coo_entries` 容器，再按 `output_type` 转成稀疏数组/字典/记录数组。

#### 4.3.3 源码精读

**`count_neighbors` 的 r 处理与算法分派**（核心一段）：

[`_ckdtree.pyx`:L1401-L1444](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_ckdtree.pyx#L1401-L1444) —— 去重、`r**p` 变换、`cumulative=False` 时校验 `r` 非降、再按是否加权分派到整数核 `count_neighbors_unweighted` 或浮点核 `count_neighbors_weighted`：

```cython
real_r, uind, inverse = np.unique(real_r, return_inverse=True, return_index=True)
n_queries = real_r.shape[0]
# Internally, we represent all distances as distance ** p
if not isinf(p):
    for i in range(n_queries):
        if not isinf(real_r[i]):
            real_r[i] = real_r[i] ** p
...
if self_weights is None and other_weights is None:
    int_result = True
    results = np.zeros(n_queries + 1, dtype=np.intp)        # 整数核
    with nogil:
        count_neighbors_unweighted(self.cself, other.cself, n_queries,
                                   prr, pir, p, cum)
else:
    int_result = False
    ...
    results = np.zeros(n_queries + 1, dtype=np.float64)     # 浮点核（加权）
    with nogil:
        count_neighbors_weighted(self.cself, other.cself,
                                 w1p, w2p, w1np, w2np, n_queries, prr, pfr, p, cum)
```

注意 `n_queries + 1`：多留一格放「0 号哨兵」（距离 ≤ 0 的计数，配合非累积分箱时表示 \((-\infty, r_0]\)）。

加权核需要的「节点权重」由 `_build_weights` 预计算（[`_ckdtree.pyx`:L1188-L1230](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_ckdtree.pyx#L1188-L1230)）：它把每个数据点的权重沿树向上求和，得到「每个节点子树的总权重」，这样双树剪枝时能直接整块加权，不必逐点。

`weights` 参数解析（[`_ckdtree.pyx`:L1421-L1428](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_ckdtree.pyx#L1421-L1428)）：`None`→不加权；元组 `(w_self, w_other)`→两树分别加权；单个数组→要求 `other is self`（否则报错），因为单个数组无法区分两端的权重。

**`sparse_distance_matrix`** 用 `coo_entries` 容器承接 C++ 结果：

[`_ckdtree.pyx`:L1571-L1597](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_ckdtree.pyx#L1571-L1597) —— 校验两树维度后，调用 C++ `sparse_distance_matrix` 填充 `res.buf`，再按 `output_type` 转换：

```cython
cdef coo_entries res
if self.m != other.m:
    raise ValueError("Trees passed to sparse_distance_matrix have different dimensionality")
res = coo_entries()
with nogil:
    sparse_distance_matrix(self.cself, other.cself, p, max_distance, res.buf)
if output_type == 'dict':
    return res.dict()
elif output_type == 'ndarray':
    return res.ndarray()
elif output_type == 'dok_array':
    return res.dok_array(self.n, other.n)
...
```

`coo_entries` 容器（[`_ckdtree.pyx`:L159-L239](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_ckdtree.pyx#L159-L239)）持有一个 `std::vector<coo_entry>*buf`，每个 `coo_entry = {i, j, v}`。`ndarray()` 通过 `__array_interface__` 零拷贝暴露成带字段 `('i','j','v')` 的记录数组；`coo_array(m,n)` 再用 `scipy.sparse.coo_array` 包装。

> **弃用的 `distance_matrix`**（[`_kdtree.py`:L961-L1021](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_kdtree.py#L961-L1021)）：纯 Python 实现，当 `M*N*K <= threshold`（默认 1e6）时用 `minkowski_distance` 广播一次性算出，否则退化成 Python 循环以避免超大临时数组。它**不建树、不剪枝**，只 brute-force 算稠密矩阵，且自 1.18.0 起弃用，建议改用 `scipy.spatial.distance.cdist`。

#### 4.3.4 代码实践

**实践目标**：验证 `count_neighbors` 与「`sparse_distance_matrix` 的非零项数」给出相同的配对计数，并比较两者的开销与输出形态。

```python
# 示例代码
import numpy as np
import time
from scipy.spatial import KDTree

rng = np.random.default_rng(2)
A = rng.random((3_000, 3))
B = rng.random((3_000, 3))
treeA, treeB = KDTree(A), KDTree(B)
r = 0.05

# 方式一：count_neighbors，只要数量
t0 = time.perf_counter()
n_pairs = treeA.count_neighbors(treeB, r)
t1 = time.perf_counter()
print("count_neighbors ->", n_pairs, f"{(t1-t0)*1e3:.1f} ms")

# 方式二：sparse_distance_matrix，既要数量也要每对距离
t0 = time.perf_counter()
sdm = treeA.sparse_distance_matrix(treeB, r, output_type="coo_array")
t1 = time.perf_counter()
print("sparse_distance_matrix -> nnz =", sdm.nnz, f"{(t1-t0)*1e3:.1f} ms")

print("两者配对数相等？", n_pairs == sdm.nnz)
```

**操作步骤**：运行上述脚本；再尝试把 `r` 调大到 `0.1`、`0.2`，观察两者计时随近邻密度增长的趋势。

**需要观察的现象**：
- `n_pairs == sdm.nnz` 应为 `True`——同一阈值下，计数等于稀疏矩阵的非零项数。
- `count_neighbors` 通常明显快于 `sparse_distance_matrix`（前者只数、不存每对距离，也无需构建稀疏容器）。
- `sdm` 的类型是 `scipy.sparse.coo_array`，形态 `(3000, 3000)` 但只存少量非零项；`count_neighbors` 只返回一个标量。

**预期结果**：两个数字相等；`count_neighbors` 更快、输出更小。具体毫秒数「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`count_neighbors` 内部为什么把半径变换成 `r**p` 再传给 C++？

**答案**：C++ 内核为了省去逐对开方，全程在「距离的 \(p\) 次幂」空间里比较。把阈值也变成 \(r^p\) 后，比较 \(d(x_1,x_2)^p \le r^p\) 等价于 \(d(x_1,x_2) \le r\)（因 \(t\mapsto t^p\) 在 \(p\ge1\) 时单调），既正确又快。`p==inf` 时跳过该变换。

**练习 2**：给 `count_neighbors(treeA, r, weights=w)` 传单个数组 `w`，但 `other` 是另一棵树 `treeB`，会发生什么？为什么？

**答案**：抛 `ValueError`。因为单个权重数组无法同时表示两棵树各自的权重；源码里 `self_weights = other_weights = weights` 后立即检查 `if other is not self: raise ValueError(...)`。正确做法是用元组 `weights=(wA, wB)` 分别指定。

## 5. 综合实践

**任务**：模拟「两组星系位置（点云 A、B），统计它们在多个尺度下的近邻配对」，把本讲三类查询串起来。

要求：

1. 生成 `A`（5000×3）、`B`（5000×3）两组点云，分别建 `KDTree`。
2. 用 `query` 找出 A 中每个点在 B 中的最近邻距离，画出这些最近邻距离的直方图，目测一个合理的阈值 `r`。
3. 取一组半径 `radii = np.linspace(r_min, r_max, 20)`，用 `count_neighbors(treeB, radii, cumulative=False)` 得到分箱计数（即两点相关函数的分子），再画出计数随尺度的曲线。
4. 选其中一个半径，用 `sparse_distance_matrix` 得到稀疏矩阵，验证其 `nnz` 等于 `count_neighbors(treeB, R, cumulative=True)`。
5. 思考题：如果你**只需要曲线（步骤 3）**，你会用 `count_neighbors` 还是 `sparse_distance_matrix`？为什么？

参考骨架：

```python
# 示例代码
import numpy as np
import matplotlib.pyplot as plt
from scipy.spatial import KDTree

rng = np.random.default_rng(42)
A = rng.random((5000, 3))
B = rng.random((5000, 3))
tA, tB = KDTree(A), KDTree(B)

# 2) 最近邻距离直方图
d, _ = tA.query(B, k=1, workers=-1)        # B 中每点在 A 中的最近邻
plt.hist(d, bins=40); plt.xlabel("NN distance"); plt.show()

# 3) 多尺度分箱计数
radii = np.linspace(0.02, 0.12, 20)
counts = tA.count_neighbors(tB, radii, cumulative=False)
plt.step(radii, counts, where='post'); plt.xlabel("r"); plt.ylabel("pairs in bin")
plt.show()

# 4) 验证 nnz == 累积计数
R = radii[10]
sdm = tA.sparse_distance_matrix(tB, R, output_type="coo_array")
print(sdm.nnz == tA.count_neighbors(tB, R))   # 预期 True
```

**思考题参考答案**：只用曲线时应选 `count_neighbors`。因为它只数不存，时间和内存都只与「半径数」相关（`cumulative=False` 时甚至不随分箱数线性增长），而 `sparse_distance_matrix` 必须把每对命中存进稀疏容器，近邻密集时内存远大于一条曲线所需。

## 6. 本讲小结

- `KDTree` 的 7 类查询可按产物分四组：最近邻（`query`）、球形邻域（`query_ball_point`）、成对配对（`query_ball_tree`/`query_pairs`）、计数与矩阵（`count_neighbors`/`sparse_distance_matrix`/`distance_matrix`）。
- **分层一致**：每个方法都是 `_kdtree.py`（Python 校验 + `super()` 转发）→ `_ckdtree.pyx`（Cython 主体，`with nogil` 调 C++ 内核）→ `ckdtree/src/*.cxx`（算法本体，u8 详讲）。
- **并行分野**：只有 `query`、`query_ball_point` 带 `workers`（逐点切片并行，靠 `_run_threads` 均分查询点）；其余双树查询靠两棵树同时下钻剪枝，无需 `workers`。
- `query_pairs(r)` ≡ `query_ball_tree(self, r)` 去重为 `i<j`；`count_neighbors(tree, r)` ≡ 这些配对的总数，但快得多。
- `count_neighbors` 的两个关键设计：半径内部表示成 \(r^p\)（省开方）、`cumulative` 开关在「少量阈值累积」与「大量分箱非累积」间切换算法；加权由 `_build_weights` 预聚合到节点。
- `sparse_distance_matrix` 用 `coo_entries` 容器零拷承接 C++ 结果，可输出 `coo_array`/`dok_array`/`dict`/`ndarray`；模块级 `distance_matrix` 是 brute-force 稠密实现，已弃用，新代码用 `scipy.spatial.distance.cdist`。

## 7. 下一步学习建议

- **向下看内核**：本讲所有 `with nogil` 调用的 C++ 函数（`query_knn`、`query_ball_point`、`query_pairs`、`count_neighbors_*`、`sparse_distance_matrix`）都定义在 `ckdtree/src/`。建议接着学 **u8-l1（`_ckdtree.pyx` Cython 层与多线程）** 与 **u8-l2（C++ 内核）**，看懂优先队列与剪枝的真正实现。
- **横向看距离度量**：本讲所有 `p` 参数都是闵可夫斯基 \(L_p\) 范数。想了解更丰富的度量（余弦、马氏、杰卡德等）及其 C++/pybind 后端，进入 **u4（距离度量基础）** 与 **u9（度量注册与 C++ 后端）**。
- **配套测试**：`tests/test_kdtree.py` 里每个查询方法都有针对性用例（如 `test_query_pairs_single_node`、`test_query_pairs_eps`），遇到边界行为不清楚时，读测试断言是最快的验证方式。
