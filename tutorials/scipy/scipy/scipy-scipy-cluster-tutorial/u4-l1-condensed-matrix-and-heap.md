# 压缩距离矩阵编码与二叉堆数据结构

## 1. 本讲目标

本讲进入 `scipy.cluster.hierarchy` 的 **Cython 后端**，专门讲解三个被所有聚类算法复用的「底层数据结构」：

1. **`condensed_index`**：把一对观测 \((i, j)\) 映射成一维压缩距离矩阵里的下标。
2. **`Heap`**：一个支持「按 key 改值」(change value) 的最小二叉堆，是 `fast_linkage` 选最小对的核心。
3. **`LinkageUnionFind`**：一个带路径压缩的并查集，是 `label()` 给「无序树」重新编号的核心。

学完后你应当能够：

- 手算 `condensed_index` 公式，并解释它为什么是 \(n(n-1)/2\) 长度的压缩编码。
- 看懂 `Heap` 用「双向索引」`index_by_key` / `key_by_index` 实现 \(O(\log n)\) 改值，并能用纯 Python 复刻它。
- 解释 `LinkageUnionFind` 的 `merge`/`find` 如何为 `nn_chain` 与 `mst_single_linkage` 产出的无序 dendrogram 重新标号。
- 说清楚这三个结构分别被哪几个算法使用（这是后续 u4-l2、u4-l3 的前置地图）。

> 本讲只讲数据结构本身，**不**展开 single linkage 的 MST、nearest-neighbor chain、Lance-Williams 公式等算法细节——它们分别在 u4-l2、u4-l3、u3-l4。本讲是这三篇的地基。

## 2. 前置知识

阅读本讲前，你需要先建立以下认知（来自 u3-l1、u3-l4）：

- **linkage matrix Z**：形状恒为 \((n-1)\times 4\)，四列依次是「被合并的两簇编号 / 合并距离 / 新簇的原始观测数」；簇编号约定原始观测占 \(0\dots n-1\)，第 \(k\) 步（从 0 起）合并出的新簇编号为 \(n+k\)，整树共 \(2n-1\) 个节点。
- **压缩距离矩阵**：`scipy.spatial.distance.pdist` 的输出，把 \(n\times n\) 对称距离矩阵的上三角（不含对角线）按行拍平成一维数组，长度 \(n(n-1)/2\)。`linkage` 的 Cython 后端**只吃这种一维形式**。
- **Cython 语法速查**（本讲会反复出现，先认个脸）：
  - `cdef int x` —— 编译期的 C 变量声明（比 Python 变量快，因为不带动态类型检查）。
  - `cdef class Foo` —— 扩展类型（extension type），本质是 C struct，但对外暴露成 Python 对象。
  - `cdef int[:] arr` —— **typed memoryview**（类型化内存视图），用 C 速度访问 numpy 数组的连续缓冲区；`int[:]`、`double[:]` 分别对应 int / float 的 1D 视图。
  - `cpdef` —— 既能被 Python 调用、也能被 Cython 内部以 C 速度调用的方法。
  - `noexcept` —— 声明该函数不会抛 Python 异常，省去每次调用后的异常检查开销（Cython 热点函数常见优化）。
  - `ctypedef struct Pair: ...` —— 定义一个 C struct。

如果这些概念你还陌生，不必担心，下面讲到每段代码时会再次点明它的作用。

## 3. 本讲源码地图

本讲只涉及两个 Cython 源文件，它们都在 `scipy/cluster/hierarchy/` 下：

| 文件 | 作用 | 本讲关注的部分 |
|------|------|----------------|
| `_hierarchy.pyx` | 所有聚类算法的 Cython 实现 | `condensed_index`、`LinkageUnionFind`、`label()`、以及各算法如何调用它们 |
| `_structures.pxi` | 被 `_hierarchy.pyx` 通过 `include` 引入的「数据结构库」 | `Heap` 类、`Pair` struct |

> 注意 `_structures.pxi` 不是独立编译单元。在 `_hierarchy.pyx` 第 18 行有一句 `include "_structures.pxi"`，它会把 `.pxi` 文件的源码**文本嵌入**到 `.pyx` 里一起编译。所以 `Heap` 和 `Pair` 实际上和 `condensed_index`、`LinkageUnionFind` 住在同一个编译产物里。

## 4. 核心概念与源码讲解

### 4.1 压缩距离矩阵编码 `condensed_index`

#### 4.1.1 概念说明

凝聚式聚类的每一步都要反复「查询两簇之间的距离」。如果把这个距离表存成完整的 \(n\times n\) 矩阵，会浪费一半内存（因为对称）加上对角线（自己到自己恒为 0）。`pdist` 的做法是只存上三角，拍平成一维。

但算法内部频繁地需要「给定 \(i, j\)，查它们之间的距离」或「把更新后的距离写回 \((i, j)\) 位置」。这就需要一个**双向映射**：知道 \((i, j)\) 算出一维下标（读 / 写），而写回时也要算出同一个下标。`condensed_index` 就是这个映射函数。它在 `_hierarchy.pyx` 里被 **所有** 算法（`linkage`、`fast_linkage`、`nn_chain`、`mst_single_linkage`、`cophenetic_distances`）反复调用，是出现频率最高的辅助函数。

#### 4.1.2 核心流程

约定一对 \((i, j)\) 满足 \(i < j\)（代码里若 \(i > j\) 就把两者交换再用同一公式）。压缩矩阵按「行」存储：第 \(i\) 行存的是 \((i, i+1), (i, i+2), \dots, (i, n-1)\) 这些对，共 \(n-1-i\) 个元素。

那么 \((i, j)\) 的下标 = 「第 \(0\) 到第 \(i-1\) 行的元素总数」+「第 \(i\) 行内 \((i, j)\) 之前的元素个数」。

- 前 \(i\) 行（第 \(0\) 到 \(i-1\) 行）的元素总数：
  \[
  \sum_{r=0}^{i-1}(n-1-r) = i(n-1) - \frac{i(i-1)}{2} = ni - \frac{i(i+1)}{2}
  \]
- 第 \(i\) 行内，\((i, j)\) 之前的对依次是 \((i, i+1), \dots, (i, j-1)\)，共 \(j-i-1\) 个。

合起来：
\[
\mathrm{index}(i, j) = ni - \frac{i(i+1)}{2} + (j - i - 1), \quad i < j
\]

总长度为 \(\sum_{i=0}^{n-2}(n-1-i) = \frac{n(n-1)}{2}\)，与 `pdist` 输出长度一致。

> 这里所有乘除都是整数运算且整除精确成立（因为 \(i(i+1)\) 必为偶数），所以 Cython 里用 C 整数除法 `/`（配合文件顶部的 `cdivision=True`）不会产生误差。

#### 4.1.3 源码精读

函数定义在 `_hierarchy.pyx` 顶部，是一个 `cdef inline`（仅 Cython 内部可见、且内联）的函数：

[`hierarchy/_hierarchy.pyx` L20-L29](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L20-L29) —— 这段代码把 \((i, j)\) 折算成压缩矩阵下标，对 `i<j` 与 `i>j` 分别处理（保证总是用较小者当行号）：

```cython
cdef inline np.npy_int64 condensed_index(np.npy_int64 n, np.npy_int64 i,
                                         np.npy_int64 j) noexcept:
    if i < j:
        return n * i - (i * (i + 1) / 2) + (j - i - 1)
    elif i > j:
        return n * j - (j * (j + 1) / 2) + (i - j - 1)
```

注意三个细节：

- 参数类型 `np.npy_int64` 是 64 位整数，避免大 \(n\) 时下标溢出。
- `i == j`（自己到自己）没有对应分支，函数返回「未定义值」——但调用方从不会查询对角线，所以安全。
- 两个分支是对称的：`i>j` 分支只是把 `i`、`j` 互换套用同一个公式。

来看一个真实调用点。`mst_single_linkage` 在松弛每条边时这样读距离：

[`hierarchy/_hierarchy.pyx` L1063-L1073](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1063-L1073) —— `dists[condensed_index(n, x, i)]` 就是「点 x 到点 i 的当前距离」：

```cython
for i in range(n):
    if merged[i] == 1:
        continue
    dist = dists[condensed_index(n, x, i)]
    if D[i] > dist:
        D[i] = dist
```

而 `fast_linkage` 在写回更新后的距离时同样依赖它：

[`hierarchy/_hierarchy.pyx` L891-L893](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L891-L893) —— 用 Lance-Williams 公式算出新距离并写回 \((z, y)\) 位置：

```cython
D[condensed_index(n, z, y)] = new_dist(
    D[condensed_index(n, z, x)], D[condensed_index(n, z, y)],
    dist, nx, ny, nz)
```

可见 `condensed_index` 是「把一维数组当二维表用」的那层地址翻译。

#### 4.1.4 代码实践

**实践目标**：手算验证 `condensed_index` 公式与 SciPy 的 `pdist` / `squareform` 完全一致。

**操作步骤**：

1. 取 \(n=4\) 个点，写出所有 \((i, j), i<j\) 对：\((0,1),(0,2),(0,3),(1,2),(1,3),(2,3)\)。
2. 用公式 \(\mathrm{index}=ni - i(i+1)/2 + (j-i-1)\) 逐个算，应当得到 \(0,1,2,3,4,5\)。
3. 用下面的「示例代码」核对：构造一个已知距离，再从 `pdist` 的输出里按算出的下标取值，确认取到的就是预期距离。

```python
# 示例代码（非项目源码，用于验证公式）
import numpy as np
from scipy.spatial.distance import pdist, squareform

def condensed_index(n, i, j):
    if i < j:
        return n * i - i * (i + 1) // 2 + (j - i - 1)
    elif i > j:
        return n * j - j * (j + 1) // 2 + (i - j - 1)

X = np.array([[0, 0], [0, 3], [4, 0], [4, 3]], dtype=float)
y = pdist(X)          # 压缩距离矩阵，长度 4*3/2 = 6
n = len(X)

# 手算每个 (i, j) 的下标，验证 y[idx] 等于 squareform 形式里的 D[i, j]
D = squareform(y)
for i in range(n):
    for j in range(i + 1, n):
        idx = condensed_index(n, i, j)
        assert y[idx] == D[i, j], (i, j, idx)
print("全部一致，压缩编码正确")
```

**需要观察的现象 / 预期结果**：`y[idx]` 与 `D[i, j]` 逐对相等，断言全部通过。这说明你的手算公式与 SciPy 内部的存储顺序完全对齐。

> 运行结果：本示例可在装好 SciPy 的环境直接跑通；如本机尚未安装，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：\(n=5\) 时，\((2, 4)\) 对应的压缩下标是多少？

**答案**：\(\mathrm{index}=5\cdot 2 - 2\cdot 3/2 + (4-2-1) = 10 - 3 + 1 = 8\)。

**练习 2**：为什么 `condensed_index` 不需要处理 `i == j`？

**答案**：对角线（自己到自己）的距离恒为 0，`pdist` 根本不存储它；聚类算法也从不查询 \((i, i)\)，所以函数没有 `i==j` 分支，调用约定保证不会传入。

**练习 3**：若把压缩矩阵改成「按列」存储而非「按行」存储，公式会变成什么样？

**答案**：行号与列号的角色互换，对 \((i, j), i<j\) 改成按列 \(j\) 统计前置元素：\(\mathrm{index}= \sum_{c=0}^{j-1}(\text{第 }c\text{ 列上三角元素数}) + (i - \cdots)\)。SciPy 选的是按行存储，故无需关心，但此练习帮助理解「公式取决于存储顺序」。

---

### 4.2 可改值的最小堆 `Heap`

#### 4.2.1 概念说明

`fast_linkage` 算法（centroid / median 方法用，见 u4-l3）每一步都要在所有「活跃簇」里找出距离最小的那一对。它对每个簇 \(x\) 维护一个「当前猜测的最近邻 \(y\) 及其距离 `min_dist[x]`」，于是「找全局最小对」就退化成「在一个可能被反复更新的集合里取最小值」——这正是**最小堆**的用武之地。

但这里有一个关键诉求：每当某个簇的最近邻距离被更新（变小），算法要**就地修改该簇在堆里的值**，而不是插入新元素。标准库 `heapq` 只支持 `heappush` / `heappop`，**没有**原生的「按 key 改值」操作。强行用 `heapq` 的话，只能要么整个重建堆（\(O(n)\)，太慢），要么插入新条目并对过期条目做惰性跳过（堆会无限膨胀，且丢失「key 必在堆中」的不变量）。

`scipy` 的解法是自己写一个最小堆，额外维护两个反查数组，从而支持 \(O(\log n)\) 的 `change_value`。这就是 `_structures.pxi` 里 `Heap` 类存在的理由。

#### 4.2.2 核心流程

一个 0 下标起的二叉堆，节点 \(p\) 的孩子在 \(2p+1\) 与 \(2p+2\)，父节点在 \((p-1)//2\)。堆里同时存「值 value」（决定堆序）和「键 key」（业务标识，这里就是簇编号 \(0\dots n-1\)）。

难点在于 `change_value(key, new_value)`：要知道 key 当前在堆数组的**哪个下标**，才能从那里开始向上/向下调整。于是堆维护两张反查表：

- `index_by_key[key]` = key 当前在堆数组的下标；
- `key_by_index[idx]` = 堆数组下标 idx 处放的是哪个 key。

二者互为逆映射。每次交换两个堆元素（`swap`）时，必须同步更新这两张表，保证它们始终自洽。这样：

- `change_value(key, v)`：由 `index_by_key[key]` 定位下标 → 改值 → 若变小则 `sift_up`，否则 `sift_down`。
- `get_min()`：堆顶 `values[0]`，对应 key 是 `key_by_index[0]`。
- `remove_min()`：把堆顶与末尾交换、缩小 size、再对堆顶 `sift_down`。

建堆用 Floyd 线性算法：从最后一个非叶节点到根，逐个 `sift_down`，总成本 \(O(n)\)。

#### 4.2.3 源码精读

先看 `Pair` struct 和类的字段声明：

[`hierarchy/_structures.pxi` L5-L8](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_structures.pxi#L5-L8) —— `Pair` 是 `get_min` / `find_min_dist` 返回的「(key, value)」二元组：

```cython
ctypedef struct Pair:
    int key
    double value
```

[`hierarchy/_structures.pxi` L28-L31](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_structures.pxi#L28-L31) —— 三个核心数组：堆值数组 `values`，加上两张反查表 `index_by_key` / `key_by_index`：

```cython
cdef int[:] index_by_key
cdef int[:] key_by_index
cdef double[:] values
cdef int size
```

**构造（线性建堆）**：

[`hierarchy/_structures.pxi` L33-L43](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_structures.pxi#L33-L43) —— 初始时 key 与下标一一对应（`arange`），再从最后一个非叶节点逆序 `sift_down`：

```cython
def __init__(self, double[:] values):
    self.size = values.shape[0]
    self.index_by_key = np.arange(self.size, dtype=np.intc)
    self.key_by_index = np.arange(self.size, dtype=np.intc)
    self.values = values.copy()
    cdef int i
    for i in reversed(range(self.size / 2)):   # C 整数除法 = 非叶节点个数
        self.sift_down(i)
```

**取最小 / 删最小**：

[`hierarchy/_structures.pxi` L45-L51](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_structures.pxi#L45-L51) —— 堆顶就是最小；删除时先把堆顶换到末尾、缩小逻辑大小、再下沉新堆顶：

```cython
cpdef Pair get_min(self) noexcept:
    return Pair(self.key_by_index[0], self.values[0])

cpdef void remove_min(self) noexcept:
    self.swap(0, self.size - 1)
    self.size -= 1
    self.sift_down(0)
```

> 注意 `remove_min` 并不真正擦除末尾元素，只是把 `size` 减 1，让逻辑边界越过它。后续访问都被 `size` 卡住，故安全。

**改值（本结构的灵魂）**：

[`hierarchy/_structures.pxi` L53-L60](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_structures.pxi#L53-L60) —— 用反查表定位下标，改值后按「变大/变小」决定上浮或下沉：

```cython
cpdef void change_value(self, int key, double value) noexcept:
    cdef int index = self.index_by_key[key]
    cdef double old_value = self.values[index]
    self.values[index] = value
    if value < old_value:
        self.sift_up(index)      # 变小：可能要往上走
    else:
        self.sift_down(index)    # 变大：可能要往下走
```

**上浮 / 下沉**：

[`hierarchy/_structures.pxi` L62-L81](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_structures.pxi#L62-L81) —— 经典的二叉堆调整，唯一关键是**所有交换都走 `swap`**，从而反查表永不离散：

```cython
cdef void sift_up(self, int index) noexcept:
    cdef int parent = Heap.parent(index)
    while index > 0 and self.values[parent] > self.values[index]:
        self.swap(index, parent)
        index = parent
        parent = Heap.parent(index)

cdef void sift_down(self, int index) noexcept:
    cdef int child = Heap.left_child(index)
    while child < self.size:
        if (child + 1 < self.size and
                self.values[child + 1] < self.values[child]):
            child += 1                       # 选较小的那个孩子
        if self.values[index] > self.values[child]:
            self.swap(index, child)
            index = child
            child = Heap.left_child(index)
        else:
            break
```

**保持反查表自洽的 `swap`**：

[`hierarchy/_structures.pxi` L91-L98](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_structures.pxi#L91-L98) —— 交换值的同时，把两张反查表也对称地改写：

```cython
cdef void swap(self, int i, int j) noexcept:
    self.values[i], self.values[j] = self.values[j], self.values[i]
    cdef int key_i = self.key_by_index[i]
    cdef int key_j = self.key_by_index[j]
    self.key_by_index[i] = key_j
    self.key_by_index[j] = key_i
    self.index_by_key[key_i] = j
    self.index_by_key[key_j] = i
```

**真实使用点**：`fast_linkage` 把每个活跃簇的「最近邻距离」做成一个堆：

[`hierarchy/_hierarchy.pyx` L845-L866](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L845-L866) —— 用堆取全局最小对，发现猜测过期就用 `change_value` 刷新，确认后 `remove_min`：

```cython
cdef Heap min_dist_heap = Heap(min_dist)

for k in range(n - 1):
    for i in range(n - k):
        pair = min_dist_heap.get_min()
        x, dist = pair.key, pair.value
        y = neighbor[x]
        if dist == D[condensed_index(n, x, y)]:
            break                       # 猜测仍有效，这就是真最小对
        pair = find_min_dist(n, D, size, x)
        y, dist = pair.key, pair.value
        neighbor[x] = y
        min_dist[x] = dist
        min_dist_heap.change_value(x, dist)   # 就地改值，O(log n)
    min_dist_heap.remove_min()
```

整段循环的逻辑是：堆里存的是「下界猜测」，若 `get_min` 取出的猜测与真实距离矩阵不符，就重算并 `change_value` 修正。这正是「可改值最小堆」让 `fast_linkage` 在 best case 接近 \(O(n^2)\) 的关键。

#### 4.2.4 代码实践

**实践目标**：用纯 Python 复刻一个支持 `change_value` 的最小堆，验证它在「反复改值」序列下 `get_min` 始终正确，并对比 `heapq` 说明为何需要自定义堆。

**操作步骤**：

1. 参照 `_structures.pxi` 的双向索引思路，实现 `change_value`。
2. 设计一组操作：插入初值、多次 `change_value`（既有变小也有变大），每次操作后用 `get_min` 与「朴素重排」对比。
3. 再用 `heapq` 模拟同样的「改值」需求，体会它做不到就地改值。

```python
# 示例代码（非项目源码，复刻 _structures.pxi 的 Heap）
import heapq

class ChangeValueHeap:
    def __init__(self, values):
        self.size = len(values)
        self.index_by_key = list(range(self.size))   # key -> 堆下标
        self.key_by_index = list(range(self.size))   # 堆下标 -> key
        self.values = list(values)
        for i in reversed(range(self.size // 2)):
            self._sift_down(i)

    def _parent(self, i):    return (i - 1) >> 1
    def _left(self, i):      return (i << 1) + 1

    def _swap(self, i, j):
        self.values[i], self.values[j] = self.values[j], self.values[i]
        ki, kj = self.key_by_index[i], self.key_by_index[j]
        self.key_by_index[i], self.key_by_index[j] = kj, ki
        self.index_by_key[ki], self.index_by_key[kj] = j, i

    def _sift_up(self, i):
        while i > 0 and self.values[self._parent(i)] > self.values[i]:
            self._swap(i, self._parent(i)); i = self._parent(i)

    def _sift_down(self, i):
        while True:
            c = self._left(i)
            if c >= self.size: break
            if c + 1 < self.size and self.values[c + 1] < self.values[c]:
                c += 1
            if self.values[i] > self.values[c]:
                self._swap(i, c); i = c
            else:
                break

    def get_min(self):           # 返回 (key, value)
        return self.key_by_index[0], self.values[0]

    def change_value(self, key, value):
        idx = self.index_by_key[key]
        old = self.values[idx]
        self.values[idx] = value
        if value < old: self._sift_up(idx)
        else:           self._sift_down(idx)

# --- 验证 ---
ops = [(1, 0.2), (3, 0.05), (0, 0.9), (2, 0.1), (1, 0.5)]  # (key, new_value)
h = ChangeValueHeap([0.5, 0.5, 0.5, 0.5])
shadow = {k: 0.5 for k in range(4)}     # 朴素真值表，用来交叉校验
for key, val in ops:
    h.change_value(key, val)
    shadow[key] = val
    min_key, min_val = h.get_min()
    true_min_key = min(shadow, key=shadow.get)
    assert min_val == shadow[true_min_key]
    print(f"改 key={key} -> {val}: 堆顶 (key={min_key}, val={min_val}) 正确")

# --- 对比 heapq：它没有 change_value，只能重建或惰性插入 ---
hq = [0.5, 0.5, 0.5, 0.5]; heapq.heapify(hq)
# 想「把某个已知元素改成 0.05」？heapq 没有此 API：
#   - 方法 A：list.index + 赋值 + heapify —— O(n)
#   - 方法 B：heappush(0.05)，旧值留在堆里惰性跳过 —— 堆不断膨胀
print("heapq 无原生 change_value，这正是自定义 Heap 存在的理由")
```

**需要观察的现象 / 预期结果**：

- 每一步 `get_min` 返回的值都等于朴素真值表里的最小值（断言不报错）。
- `heapq` 想做同样的事，只能 \(O(n)\) 重建堆，或让堆无限增长——从而直观理解「可改值最小堆」的必要性。

> 运行结果：本示例为纯 Python，可直接运行；如本机无 Python 环境则标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`change_value` 为什么必须区分「变小上浮 / 变大下沉」两种情况，而不是无脑 `sift_down`？

**答案**：值变小时，该节点可能比父节点更小，破坏了「父 ≤ 子」的性质，需要上浮；值变大时反之，需要下沉。只做其中一个方向无法同时修复两种破坏，堆序就得不到保证。

**练习 2**：若 `swap` 忘了更新 `index_by_key`，会发生什么？

**答案**：反查表与实际位置脱节，下次 `change_value(key)` 会用错误的下标改值，堆序被悄悄破坏，`get_min` 开始返回错误结果——且很难排查，因为不会立即报错。

**练习 3**：`fast_linkage` 用堆存「最近邻距离下界」，为什么不直接对全部 \(O(n^2)\) 个两两距离建堆？

**答案**：那需要 \(O(n^2)\) 内存且每次合并后要更新 \(O(n)\) 个条目；而「每簇只记一个最近邻」只需 \(O(n)\) 个堆元素，配合「猜测过期就重算」的惰性策略，best case 接近 \(O(n^2)\)，远优于朴素 \(O(n^3)\)。

---

### 4.3 并查集 `LinkageUnionFind`

#### 4.3.1 概念说明

`nn_chain` 和 `mst_single_linkage` 在合并簇时，产出的 linkage matrix **一开始是无序的**——合并行的顺序由算法的遍历顺序决定，不保证距离单调递增，簇编号也可能引用了「已经被合并掉」的旧 id。在返回给用户之前，必须把这种「无序 dendrogram」整理成符合 Z 矩阵规范的形态：每行的两簇编号必须是「当前仍然活跃的代表」，第四列（簇大小）必须正确。

`_hierarchy.pyx` 用一个叫 `label` 的内部函数完成这件事，而 `label` 的引擎就是 `LinkageUnionFind`——一个**并查集**（union-find / disjoint set）。

并查集是一种维护「若干互不相交集合」的数据结构，支持两种操作：`find(x)`（查 x 所在集合的代表）和 `union/merge`（合并两个集合）。它的精髓是**路径压缩**：让后续查询几乎变成 \(O(1)\)。在 `label` 里，每个原始簇自成一集合，每合并一次就创建一个新节点当父，`find` 用来把「旧 id」翻译成「当前代表」。

#### 4.3.2 核心流程

`LinkageUnionFind` 维护两个长度 \(2n-1\) 的数组（覆盖全部 \(2n-1\) 个节点）：

- `parent[x]`：节点 x 的父节点。初始 `parent[x] = x`（自成一簇，自己是根）。
- `size[x]`：节点 x 所代表簇的原始观测数。初始全 1。
- `next_label`：下一个可分配的新簇编号，从 \(n\) 开始递增（呼应「第 k 步新簇 = n+k」）。

两种操作：

- `merge(x, y)`：创建新节点 `next_label`，让 x、y 都指向它（`parent[x] = parent[y] = next_label`），记录新簇大小 `size[x] + size[y]`，`next_label += 1`，返回新大小。
  - 注意这个变体的特殊之处：合并不是把一棵树挂到另一棵下，而是**新建一个父节点**，把两棵树的根都挂上去。这恰好对应凝聚式聚类「每步产生一个新簇」的语义。
- `find(x)`：先一路沿 `parent` 走到根；再做第二趟，把路径上所有节点的 `parent` 直接指向根（**路径压缩**），摊还复杂度近 \(O(1)\)。

`label(Z, n)` 的流程：对 Z 的每一行（按当前顺序），用 `find` 把行里的两个簇 id 翻译成当前代表，按大小排序写回 Z 的前两列，并用 `merge` 得到的新大小填第四列。这样产出的 Z 才满足「两簇编号都是活跃代表 + 大小正确」。

#### 4.3.3 源码精读

`LinkageUnionFind` 定义在 `_hierarchy.pyx` 里（不是 `_structures.pxi`）：

[`hierarchy/_hierarchy.pyx` L1090-L1099](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1090-L1099) —— 初始化：每个节点自指为父、大小为 1、下一个新标签从 n 开始：

```cython
cdef class LinkageUnionFind:
    """Structure for fast cluster labeling in unsorted dendrogram."""
    cdef int[:] parent
    cdef int[:] size
    cdef int next_label

    def __init__(self, int n):
        self.parent = np.arange(2 * n - 1, dtype=np.intc)
        self.next_label = n
        self.size = np.ones(2 * n - 1, dtype=np.intc)
```

**merge**：

[`hierarchy/_hierarchy.pyx` L1101-L1107](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1101-L1107) —— 新建父节点 `next_label`，把 x、y 都挂上去，返回合并后的大小：

```cython
cdef int merge(self, int x, int y) noexcept:
    self.parent[x] = self.next_label
    self.parent[y] = self.next_label
    cdef int size = self.size[x] + self.size[y]
    self.size[self.next_label] = size
    self.next_label += 1
    return size
```

**find（带路径压缩）**：

[`hierarchy/_hierarchy.pyx` L1109-L1118](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1109-L1118) —— 第一趟走到根 x，第二趟把路径上每个节点的父直接指向根：

```cython
cdef find(self, int x):
    cdef int p = x
    while self.parent[x] != x:        # 第一趟：找到根
        x = self.parent[x]
    while self.parent[p] != x:        # 第二趟：路径压缩
        p, self.parent[p] = self.parent[p], x
    return x
```

> 第二趟里 `p, self.parent[p] = self.parent[p], x` 是「先把 p 暂存、把 p 的父改成根 x、再把 p 推进到它原来的父」——一行完成「打平 + 前进」。

**使用点 `label`**：

[`hierarchy/_hierarchy.pyx` L1121-L1133](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1121-L1133) —— 对无序 Z 的每一行，用 `find` 翻译成活跃代表、排序写回、用 `merge` 填簇大小：

```cython
cdef label(double[:, :] Z, int n):
    cdef LinkageUnionFind uf = LinkageUnionFind(n)
    cdef int i, x, y, x_root, y_root
    for i in range(n - 1):
        x, y = int(Z[i, 0]), int(Z[i, 1])
        x_root, y_root = uf.find(x), uf.find(y)
        if x_root < y_root:
            Z[i, 0], Z[i, 1] = x_root, y_root
        else:
            Z[i, 0], Z[i, 1] = y_root, x_root
        Z[i, 3] = uf.merge(x_root, y_root)
```

**谁调用 `label`**：只有 `nn_chain` 和 `mst_single_linkage` 在排好序后调用它——

[`hierarchy/_hierarchy.pyx` L1022-L1027](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1022-L1027)（`nn_chain` 末尾）与 [`hierarchy/_hierarchy.pyx` L1080-L1085](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1080-L1085)（`mst_single_linkage` 末尾）都是同一段套路：先按距离排序，再 `label` 重编号。

```cython
order = np.argsort(Z_arr[:, 2], kind='mergesort')
Z_arr = Z_arr[order]
label(Z_arr, n)
```

> 重要对比：`fast_linkage`（u4-l3）**不**调用 `label`，因为它在合并时已经直接维护好了正确的簇编号（`cluster_id[y] = n + k`），产出的 Z 天然有序、自带正确大小，无需后处理。这就是「谁需要 `LinkageUnionFind`、谁不需要」的分界线。

#### 4.3.4 代码实践

**实践目标**：用纯 Python 实现一个 `LinkageUnionFind`，手动驱动它复现 `label` 的逻辑，体会「新建父节点」式合并与路径压缩。

**操作步骤**：

1. 复刻 `parent` / `size` / `next_label` 与 `merge` / `find`。
2. 构造一个「无序」的合并序列（簇编号引用了已被合并的旧 id），用 `find` 翻译、用 `merge` 记大小。
3. 把整理后的前两列和第四列与手算对照。

```python
# 示例代码（非项目源码，复刻 LinkageUnionFind + label）
class LinkageUnionFind:
    def __init__(self, n):
        self.parent = list(range(2 * n - 1))
        self.size = [1] * (2 * n - 1)
        self.next_label = n

    def merge(self, x, y):
        self.parent[x] = self.next_label
        self.parent[y] = self.next_label
        s = self.size[x] + self.size[y]
        self.size[self.next_label] = s
        self.next_label += 1
        return s

    def find(self, x):
        p = x
        while self.parent[x] != x:
            x = self.parent[x]
        while self.parent[p] != x:        # 路径压缩
            p, self.parent[p] = self.parent[p], x
        return x

def label(Z, n):
    uf = LinkageUnionFind(n)
    for i in range(n - 1):
        x, y = int(Z[i, 0]), int(Z[i, 1])
        xr, yr = uf.find(x), uf.find(y)
        if xr < yr:
            Z[i][0], Z[i][1] = xr, yr
        else:
            Z[i][0], Z[i][1] = yr, xr
        Z[i][3] = uf.merge(xr, yr)
    return Z

# 一个 4 点的「无序」合并序列：先合并 (0,1)，再合并 (0,2) —— 注意 0 已被合并
Z = [[0, 1, 0.3, 0],
     [0, 2, 0.4, 0],
     [3, 4, 0.6, 0]]      # 3、4 是前面合并出的新簇
label(Z, 4)
for row in Z:
    print(row)
```

**需要观察的现象 / 预期结果**：

- 第二行原本写的是 `(0, 2)`，但 0 已在第一行被合并进簇 4，`find(0)` 返回 4，于是第二行被改写成 `(2, 4)`。
- 第三行 `(3, 4)` 合并后，新簇大小应为 3（簇 3 是单点、簇 4 是两点），第四列填 3。
- 全部行的前两列都是「当前活跃代表」、第四列大小正确。

> 运行结果：纯 Python 可直接运行；如本机无环境则标注「待本地验证」。你也可以把它和 `scipy.cluster.hierarchy.linkage(method='single')` 的输出对照（注意 SciPy 内部还会按距离 mergesort 排序）。

#### 4.3.5 小练习与答案

**练习 1**：`LinkageUnionFind.merge` 与教科书里「按秩合并」的 union-find 有何不同？

**答案**：教科书的 union 把一棵树挂到另一棵下（按秩/大小择根）；而这里的 `merge` **总是新建一个节点**当两棵树的父。这是因为凝聚式聚类每步产生一个新簇（编号 n+k），需要一个全新节点来承载，而不是复用已有的根。

**练习 2**：`find` 的第二趟（路径压缩）删掉会怎样？算法还能得到正确结果吗？

**答案**：结果仍正确（因为第一趟已经找到真根），但 `parent` 链会越拉越长，后续 `find` 退化为 \(O(\text{链长})\)，整体从近 \(O(n\,\alpha(n))\) 退化到 \(O(n^2)\)。路径压缩是性能优化，不是正确性必需。

**练习 3**：为什么 `fast_linkage` 不需要 `label`/`LinkageUnionFind`？

**答案**：`fast_linkage` 在合并时直接执行 `cluster_id[y] = n + k`，始终用「当前活跃代表」写 Z，且第四列用维护好的 `size` 数组当场填好（`Z[k, 3] = nx + ny`）。产出的 Z 天然有序、编号正确，无需后处理；而 `nn_chain`/`mst_single_linkage` 的合并顺序由遍历决定、编号会引用旧 id，才需要 `label` 兜底。

---

## 5. 综合实践

把三个结构串起来，完成一个「迷你 single linkage」端到端实现，验证它与 `scipy.cluster.hierarchy.linkage(method='single')` 同路。

**任务**：

1. 用 `pdist` 得到压缩距离矩阵 `y`，\(n\) 取 5–6 个点。
2. 用 4.1 的 `condensed_index` 把 `y` 当二维表读。
3. 实现一个最朴素的 single linkage 主循环（不必 MST，直接每步全局找最小对、用 `min` 合并、把被合并簇到其它点的距离改成 `min(d_xi, d_yi)`），合并时**先用一个并查集（4.3）**把旧 id 翻译成当前代表、再写 Z 的前两列与第四列大小。
4. 对比你的 Z 与 `scipy` 的 `linkage(y, method='single')`（按距离排序后比较前三列）。

```python
# 示例代码（非项目源码，综合三个结构的最小 single linkage）
import numpy as np
from scipy.spatial.distance import pdist
from scipy.cluster.hierarchy import linkage as scipy_linkage

def condensed_index(n, i, j):
    if i < j:  return n*i - i*(i+1)//2 + (j-i-1)
    if i > j:  return n*j - j*(j+1)//2 + (i-j-1)

class UF:                      # 4.3 的并查集精简版
    def __init__(self, n):
        self.parent = list(range(2*n-1)); self.size=[1]*(2*n-1); self.nl=n
    def merge(self,x,y):
        self.parent[x]=self.parent[y]=self.nl
        s=self.size[x]+self.size[y]; self.size[self.nl]=s; self.nl+=1; return s
    def find(self,x):
        while self.parent[x]!=x: x=self.parent[x]
        return x

def my_single(y, n):
    D = np.array(y, dtype=float)
    uf = UF(n)
    active = list(range(n))            # 当前活跃的代表 id
    Z = []
    for _ in range(n-1):
        # 1) 在活跃簇里找距离最小对 (用 condensed_index 读 D)
        best = None
        for a in range(len(active)):
            for b in range(a+1, len(active)):
                i, j = active[a], active[b]
                d = D[condensed_index(n, min(i,j), max(i,j))]
                if best is None or d < best[0]:
                    best = (d, i, j)
        d, x, y_ = best
        xr, yr = uf.find(x), uf.find(y_)
        xr, yr = sorted([xr, yr])
        Z.append([xr, yr, d, uf.merge(xr, yr)])
        # 2) single linkage 距离更新：到其它点取 min
        for k in active:
            if k == x or k == y_: continue
            dk = min(D[condensed_index(n,min(x,k),max(x,k))],
                     D[condensed_index(n,min(y_,k),max(y_,k))])
            D[condensed_index(n,min(y_,k),max(y_,k))] = dk
        active.remove(x)               # 丢掉 x，保留 y_ 当新代表
    Z = np.array(Z)
    Z = Z[np.argsort(Z[:,2], kind='mergesort')]   # 仿照源码：按距离排序
    return Z

X = np.array([[0,0],[0,1],[5,5],[5,6],[10,0]], dtype=float)
y = pdist(X); n = len(X)
print("我的 Z:\n", my_single(y, n))
print("SciPy Z:\n", scipy_linkage(y, method='single'))
```

**需要观察的现象 / 预期结果**：你的 Z 与 SciPy 的 Z 在前三列（两簇编号 + 距离）上一致；第四列（簇大小）也吻合。这说明三个数据结构协同起来，已经能跑通一条真实的 single linkage 链路（注：SciPy 真正用的是 `mst_single_linkage` 的 MST 实现，比这里的 \(O(n^3)\) 朴素版快得多，但结果等价）。

> 本实践用到了 `condensed_index`（读 / 写距离表）、`LinkageUnionFind`（翻译代表 + 算簇大小），但没有用到 `Heap`——因为朴素版每步全局扫描找最小对，不需要可改值堆。`Heap` 的用武之地是 u4-l3 的 `fast_linkage`，届时你可以把本实践的「找最小对」换成堆，体会性能差异。

## 6. 本讲小结

- **`condensed_index`** 用 \(ni - i(i+1)/2 + (j-i-1)\) 把 \((i, j)\) 折算成一维压缩距离矩阵的下标，是所有聚类算法「把一维数组当二维表用」的地址翻译层，出现频率最高。
- **`Heap`** 是一个支持 `change_value` 的最小二叉堆，靠 `index_by_key` / `key_by_index` 两张反查表实现 \(O(\log n)\) 就地改值；它**只被 `fast_linkage` 使用**，弥补了 `heapq` 没有「按 key 改值」的缺口。
- **`LinkageUnionFind`** 是一个「合并时新建父节点 + 路径压缩」的并查集，**只被 `label` 使用**，而 `label` **只被 `nn_chain` 和 `mst_single_linkage` 调用**，用于给无序 dendrogram 重新编号并算簇大小。
- 三个结构各司其职：`condensed_index` 管地址、`Heap` 管选最小、`LinkageUnionFind` 管标号。它们是 u4-l2（MST 单链接）与 u4-l3（nn_chain / fast_linkage）的共同地基。
- 阅读时应留意「谁用了谁」：`fast_linkage` 用 `Heap` 但不用 `label`；`nn_chain`/`mst_single_linkage` 用 `label`/`LinkageUnionFind` 但不用 `Heap`；三者都用 `condensed_index`。

## 7. 下一步学习建议

- **u4-l2 single linkage 的最小生成树算法**：看 `mst_single_linkage` 如何用 Prim 风格的 \(D\) 数组把 single linkage 跑成 \(O(n^2)\)，并大量使用本讲的 `condensed_index`，最后调用 `label` 收尾。
- **u4-l3 nearest-neighbor chain 与 fast_linkage**：看 `nn_chain` 如何用最近邻链在 \(O(n^2)\) 完成 complete/average/weighted/ward，以及 `fast_linkage` 如何用本讲的 `Heap` 把 centroid/median 的 best case 拉到接近 \(O(n^2)\)。
- **回看 u3-l4 Lance-Williams 公式**：本讲的 `condensed_index` 写回距离时（如 `fast_linkage` 的 `new_dist(...)`），右侧的距离更新函数就是 u3-l4 讲的七种 `_xxx` 之一，两篇对照阅读能看清「数据结构 + 数学公式」如何拼成完整算法。
- **延伸阅读**：`fast_linkage` 的 docstring 引用了 Daniel Mullner 的论文 *"Modern hierarchical, agglomerative clustering algorithms"*（arXiv:1109.2378），其中详细论述了「最近邻下界 + 可改值堆」为何能把 generic 算法加速到接近 \(O(n^2)\)，是理解 `Heap` 设计动机的最佳参考。
