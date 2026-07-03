# single linkage 的最小生成树算法

## 1. 本讲目标

本讲精读 `scipy.cluster.hierarchy` 后端里**最简洁、最快**的一条聚类路径：`mst_single_linkage`。它是 `linkage(method='single')` 的专属后端，用一种 **Prim 风格的最小生成树（Minimum Spanning Tree, MST）算法**把单链接聚类的时间复杂度压到 \(O(n^2)\)——既不需要 `fast_linkage` 那套可改值堆，也不需要 `nn_chain` 的最近邻链。

学完后你应当能够：

- 解释**为什么 single linkage 聚类等价于求距离矩阵的最小生成树**，并由此理解它为何能彻底绕开 Lance-Williams 距离更新公式。
- 逐行读懂 [`mst_single_linkage`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1032-L1087) 主循环：用 `D[i]`（点 \(i\) 到「当前树」的最近距离）+ `merged[]` 标记实现 Prim 松弛，每轮 \(O(n)\) 选出下一个并入点。
- 说出代码里**把 `_single = min` 内联进松弛步**的几何含义，并对照 u3-l4 的 Lance-Williams 公式。
- 看懂末尾「按距离排序 + `label()` 重编号」两步为何是**必需**的收尾，以及 [`LinkageUnionFind`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1090-L1118) 如何把「无序的 MST 边」翻译成合法的 linkage matrix。
- 用纯 Python 复刻一版 Prim 版 single linkage，并在 6 点小数据集上与 `scipy.cluster.hierarchy.linkage(method='single')` 逐元素对齐。

> 本讲的两个数据结构协作方（[`condensed_index`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L20-L29)、`LinkageUnionFind`）已在 **u4-l1** 详细讲过，这里只讲它们在 MST 算法里的**用法**，不重复内部实现。Lance-Williams 公式见 **u3-l4**，linkage matrix 数据结构见 **u3-l1**。

## 2. 前置知识

阅读本讲前，请先建立以下认知（来自 u3-l1、u3-l4、u4-l1）：

- **linkage matrix Z**：形状恒为 \((n-1)\times 4\)，四列依次为「被合并的两簇编号 / 合并距离 / 新簇的原始观测数」；簇编号约定原始观测占 \(0\dots n-1\)，第 \(k\) 步（从 0 起）合并出的新簇编号为 \(n+k\)。
- **压缩距离矩阵**：`pdist` 的输出，把 \(n\times n\) 对称距离矩阵的上三角按行拍平成一维数组，长度 \(n(n-1)/2\)。Cython 后端**只吃这种一维形式**，并用 [`condensed_index`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L20-L29) 把 \((i,j)\) 折算成下标（u4-l1 §4.1）。
- **single linkage 的距离定义**：两簇 \(A,B\) 之间的单链接距离为
  \[
  d_{\text{single}}(A,B)=\min_{a\in A,\,b\in B} d(a,b)
  \]
  即「两簇之间最近的一对点的距离」。这正是 [`_single`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_distance_update.pxi#L30-L32) 里 `min(d_xi, d_yi)` 的含义（u3-l4）。
- **`LinkageUnionFind` 与 `label()`**：并查集 + 重编号函数，给「无序 dendrogram」修正簇编号与簇大小（u4-l1 §4.3）。
- **Cython 语法速查**：`cdef` 声明 C 变量、`cdef int[:] x` 是类型化内存视图（typed memoryview）、`noexcept` 省去异常检查、`const double[:]` 表示只读视图。

## 3. 本讲源码地图

本讲几乎只读一个 Cython 文件，外加一个距离更新片段和一个 Python 桥接点：

| 文件 | 作用 | 本讲关注的部分 |
|------|------|----------------|
| `hierarchy/_hierarchy.pyx` | 所有聚类算法的 Cython 实现 | `mst_single_linkage`（主角）、`label()`、`condensed_index`、`LinkageUnionFind` |
| `hierarchy/_hierarchy_distance_update.pxi` | 七种方法的 Lance-Williams 更新函数 | [`_single`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_distance_update.pxi#L30-L32)（被 MST 算法「内联」，不直接调用） |
| `hierarchy/_hierarchy_impl.py` | Python 封装层 | [`cy_linkage`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L955-L965) 闭包里 `method == 'single'` 的分派 |

> `mst_single_linkage` 是唯一一个**不接受 `method` 参数**的聚类后端——因为它只为 single 服务，方法已写死。对照 `nn_chain(dists, n, method)` 与 `fast_linkage(dists, n, method)` 都带 `method`。

## 4. 核心概念与源码讲解

### 4.1 单链接聚类等价于最小生成树

#### 4.1.1 概念说明

凝聚式 single linkage 的朴素做法是：每一步在所有「现存的簇对」里找单链接距离最小的那一对合并，再用 \(d_{\text{single}}(A\cup B, C)=\min(d(A,C),d(B,C))\) 更新距离矩阵。这就是 u3-l4 里 `_single` 做的事，也是 `nn_chain` / `fast_linkage` 的通用套路。朴素实现每步要扫 \(O(n^2)\) 个簇对，共 \(n-1\) 步，复杂度 \(O(n^3)\)。

但 single linkage 有一个其他六种方法都没有的**代数特权**：它的距离更新 `_single(d_xi, d_yi) = min(d_xi, d_yi)` 只取最小值。这条「取最小」的规则恰好等价于图论里的一个经典结论——

> **定理**：把 \(n\) 个观测看成完全图 \(K_n\) 的顶点，两两距离作为边权，则 single linkage 聚类的每一次合并，对应着该图**最小生成树**的一条边；按边权从小到大加入这 \(n-1\) 条 MST 边，就得到了完整的单链接 dendrogram。

直觉上：MST 连接所有点且总权最小，它必然优先用「最短的那条跨簇边」把两簇拉到一起——这正是 single linkage「取最近一对点」的定义。换句话说，**单链接聚类 = 把 MST 的边按权排序后逐条合并**。

这个等价性的巨大回报是：求 MST 有 \(O(n^2)\) 的 Prim 算法（适合稠密距离矩阵，正是 scipy 的情形），比朴素 \(O(n^3)\) 快一个数量级，而且**完全不需要维护并更新簇间距离矩阵**——Prim 只维护「每个点到当前树的最小距离」一个数组。

#### 4.1.2 核心流程

Prim 算法（从任意种子点出发，逐步把最近的点并入树）的伪代码：

```
输入：压缩距离矩阵 dists，点数 n
D[i] = +inf              # 每个"未入树"点 i 到当前树的最近距离
merged[i] = False        # 点 i 是否已入树
x = 0                    # 任意选种子点 0
重复 n-1 次：
    merged[x] = True     # 把 x 并入树
    对每个未入树的点 i：
        dist = dists[x, i]               # 新入树点 x 到 i 的直接距离
        D[i] = min(D[i], dist)           # 松弛：用新边更新 i 到树的最短距离  ← 这就是 _single
    在未入树的点里挑 D[i] 最小的 y        # y 是离当前树最近的点
    记录一条 MST 边 (x, y, D[y])
    x = y                # 下一轮从 y 继续向外扩展
```

关键观察：

- `D[i] = min(D[i], dists[x, i])` 这一步**就是 Lance-Williams 的 `_single`**：新点 \(x\) 入树后，点 \(i\) 到「树」的单链接距离 = `min(原先 i 到树的最近距离, i 到新点 x 的距离)`。MST 把它「内联」进了松弛步，因此 `mst_single_linkage` 里**根本不调用 `_single` 函数**，也不查 `linkage_methods` 表。
- 每轮内层循环 \(O(n)\)，共 \(n-1\) 轮，总复杂度 \(O(n^2)\)，空间 \(O(n)\)（`D` 和 `merged`）。
- 朴素 single linkage 要在「簇对」层面工作，而 Prim 退回到「点到树」层面，省掉了簇的合并与距离更新——这是复杂度下降的根本原因。

#### 4.1.3 源码精读

single 方法的距离更新函数 [`_single`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_distance_update.pxi#L30-L32) 只是一个 `min`：

```cython
cdef double _single(double d_xi, double d_yi, double d_xy,
                    int size_x, int size_y, int size_i) noexcept:
    return min(d_xi, d_yi)
```

scipy 的分派层 [`cy_linkage`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L955-L965) 把 `method == 'single'` 单独路由到 MST 后端，**不走** `nn_chain` / `fast_linkage`：

```python
def cy_linkage(y, validate):
    ...
    if method == 'single':
        return _hierarchy.mst_single_linkage(y, n)      # ← 本讲主角
    elif method in ('complete', 'average', 'weighted', 'ward'):
        return _hierarchy.nn_chain(y, n, method_code)
    else:
        return _hierarchy.fast_linkage(y, n, method_code)
```

注意 `mst_single_linkage(y, n)` 只接收压缩距离矩阵和点数，**没有 `method` 参数**——因为 single 是它的唯一服务对象。

#### 4.1.4 代码实践

**实践目标**：用一个独立的最小生成树实现验证「single linkage ≡ MST」这个等价性。

**操作步骤**：

```python
import numpy as np
from scipy.spatial.distance import pdist
from scipy.cluster.hierarchy import linkage
from scipy.sparse.csgraph import minimum_spanning_tree
from scipy.sparse import squareform

# 6 个二维点
X = np.array([[0., 0.], [0., 1.], [1., 0.],
              [5., 5.], [6., 5.], [5., 6.]])
y = pdist(X)

# (1) scipy 的 single linkage 距离序列
Z = linkage(y, method='single')
single_dists = np.sort(Z[:, 2])

# (2) 用完全独立的 MST 实现得到边权序列
T = minimum_spanning_tree(squareform(y))   # 稀疏 MST
mst_dists = np.sort(T.data)

print("single linkage 距离:", single_dists)
print("MST 边权          :", mst_dists)
print("完全一致?", np.allclose(single_dists, mst_dists))
```

**需要观察的现象**：两条距离序列**逐元素相等**（顺序也一致，因为两者都是升序）。

**预期结果**：打印 `完全一致? True`。这说明 `linkage(method='single')` 产生的合并距离，恰好就是距离图最小生成树的 \(n-1\) 条边权。

#### 4.1.5 小练习与答案

**练习 1**：为什么 complete linkage（`_complete = max`）不能套用 MST 算法？

**答案**：complete 的距离更新是 `max(d_xi, d_yi)`，不是「取最小」。合并两簇后到第三簇的 complete 距离取决于「最远的一对点」，会随着簇变大而**单调增大**，无法用「每个点到当前树的最小距离」一个数组来维护——Prim 松弛 `D[i] = min(D[i], dist)` 只对「取最小」的规则成立。

**练习 2**：MST 算法为何能「完全不需要更新簇间距离矩阵」？

**答案**：因为 single 的 `_single = min` 让「点 \(i\) 到树簇 \(T\) 的单链接距离」恰好等于 `min{已入树点 x 到 i 的直接距离}`，这个量可以被 `D[i]` 增量维护（每入树一个新点 \(x\)，只需用 `dists[x, i]` 松弛一次）。其他方法合并后距离会变成两簇距离的加权和或最大值，无法这样局部增量更新。

---

### 4.2 `condensed_index`：把一维数组当二维距离表

#### 4.2.1 概念说明

`mst_single_linkage` 主循环里每次都要取 `dists[x, i]`，但 `dists` 是一维的压缩距离矩阵（`pdist` 输出），没有 `dists[x, i]` 这种二维下标。[`condensed_index`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L20-L29) 就是这个「地址翻译层」：给定 \((i,j)\)，返回该 pair 在一维数组里的下标。它是所有聚类算法复用的公共工具（u4-l1 §4.1 已讲过内部细节），本讲只看它在 MST 循环里如何被调用。

#### 4.2.2 核心流程

对 \(i<j\)，压缩下标为：

\[
\mathrm{idx}(i,j)=n\cdot i-\frac{i(i+1)}{2}+(j-i-1)
\]

公式的几何含义：跳过前 \(i\) 行（第 \(i\) 行之前共有 \(n-1+n-2+\dots+(n-i)=n\cdot i - i(i+1)/2\) 个元素），再在本行里偏移 \(j-i-1\)。当 \(i>j\) 时，代码利用距离矩阵的对称性，交换 \(i,j\) 后套用同一公式。整个压缩矩阵长度为 \(n(n-1)/2\)。

#### 4.2.3 源码精读

[`condensed_index`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L20-L29) 用 `if i < j / elif i > j` 两个分支处理对称性：

```cython
cdef inline np.npy_int64 condensed_index(np.npy_int64 n, np.npy_int64 i,
                                         np.npy_int64 j) noexcept:
    if i < j:
        return n * i - (i * (i + 1) / 2) + (j - i - 1)
    elif i > j:
        return n * j - (j * (j + 1) / 2) + (i - j - 1)
```

在 MST 主循环里，它出现在取距离的那一行（见 §4.3.3）：

```cython
dist = dists[condensed_index(n, x, i)]
```

即「当前树的新入点 \(x\) 到候选点 \(i\) 的直接距离」。因为 `x` 和 `i` 的大小关系不确定，`condensed_index` 内部的双分支正好兜住两种情况。

#### 4.2.4 代码实践

**实践目标**：验证 `condensed_index` 与 `scipy.spatial.distance.squareform` 的编码完全一致。

**操作步骤**：

```python
import numpy as np
from scipy.spatial.distance import squareform

def condensed_index(n, i, j):
    if i < j:
        return n * i - (i * (i + 1)) // 2 + (j - i - 1)
    elif i > j:
        return n * j - (j * (j + 1)) // 2 + (i - j - 1)

n = 6
y = np.arange(n * (n - 1) // 2, dtype=float)      # 假想的压缩矩阵
M = squareform(y)                                  # 还原成 n x n

ok = True
for i in range(n):
    for j in range(n):
        if i != j:
            ok &= (y[condensed_index(n, i, j)] == M[i, j])
print("编码一致?", ok)
```

**需要观察的现象**：对所有 \(i\neq j\)，`condensed_index(n,i,j)` 取到的值都等于 `squareform` 还原出的二维矩阵 `M[i,j]`。

**预期结果**：`编码一致? True`。

#### 4.2.5 小练习与答案

**练习**：对 \(n=6\)，手算 `condensed_index(6, 2, 5)` 与 `condensed_index(6, 5, 2)`，确认二者相等。

**答案**：
`condensed_index(6,2,5) = 6·2 − 2·3/2 + (5−2−1) = 12 − 3 + 2 = 11`；
`condensed_index(6,5,2)` 走 `i>j` 分支：`6·2 − 2·3/2 + (5−2−1) = 11`。两者都为 11，符合对称性。

---

### 4.3 `mst_single_linkage` 主循环精读：Prim 松弛

#### 4.3.1 概念说明

这是本讲的核心。[`mst_single_linkage`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1032-L1087) 把 §4.1 的 Prim 算法落地成 Cython。它产出的原始 `Z` 有两个特点，决定了后面必须有「排序 + label」收尾：

1. **行的顺序是「MST 边被发现」的顺序**，而非距离升序——Prim 从种子点向外扩展，先发现的边不一定更短。
2. **每行的前两列存的是「原始观测编号」**（当前点 `x` 和新并入点 `y`），不是合法的 linkage matrix 簇编号。当 `x` 或 `y` 所代表的点早已属于某个合并簇时，这个编号需要在 `label()` 阶段被翻译成正确的 \(n+k\) 簇号。

#### 4.3.2 核心流程

`mst_single_linkage(dists, n)` 的三段式结构：

```
第一段：初始化
    Z = 空 (n-1)×4 矩阵
    merged = 全 0          # 标记已入树
    D = 全 +inf            # 每个点到当前树的最近距离
    x = 0                  # 种子点

第二段：Prim 主循环（共 n-1 轮）
    每轮：
        merged[x] = 1
        扫描所有未入树点 i：
            dist = dists[condensed_index(n, x, i)]
            若 D[i] > dist：D[i] = dist        # ← 内联的 _single 松弛
            若 D[i] < current_min：记 y = i, current_min = D[i]
        Z[k] = [x, y, current_min]             # 记一条 MST 边
        x = y                                  # 下一轮从 y 扩展

第三段：收尾
    按 Z[:,2]（距离）稳定升序排序
    label(Z, n)                                # 用并查集重写簇编号与簇大小
    返回 Z
```

注意主循环只写了 `Z[k,0]、Z[k,1]、Z[k,2]` 三列，**第四列（簇大小）此时是空的/未定义**，要等 `label()` 在收尾时填上。

#### 4.3.3 源码精读

先看初始化（[L1047-L1059](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1047-L1059)）：声明 `merged` 全零标记数组、`D` 全 `INFINITY` 的「到树最近距离」数组，种子点 `x = 0`。

```cython
Z_arr = np.empty((n - 1, 4))
cdef double[:, :] Z = Z_arr

# Which nodes were already merged.
cdef int[:] merged = np.zeros(n, dtype=np.intc)

cdef double[:] D = np.empty(n)
D[:] = INFINITY
...
x = 0
```

再看主循环（[L1060-L1078](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1060-L1078)），这是 Prim 的核心：

```cython
for k in range(n - 1):
    current_min = INFINITY
    merged[x] = 1                              # 把 x 并入树
    for i in range(n):
        if merged[i] == 1:
            continue

        dist = dists[condensed_index(n, x, i)] # x 到 i 的直接距离
        if D[i] > dist:                        # ← 松弛：内联的 _single
            D[i] = dist

        if D[i] < current_min:                 # 选离树最近的未入树点
            y = i
            current_min = D[i]

    Z[k, 0] = x
    Z[k, 1] = y
    Z[k, 2] = current_min
    x = y                                      # 下一轮从 y 扩展
```

逐行解读：

- `merged[x] = 1`：把上一轮选出的 `y`（本轮的 `x`）标记为已入树。
- 内层 `for i`：对每个**未入树**的点，先用新入树点 `x` 到它的直接距离 `dist` 去**松弛** `D[i]`。这一句 `if D[i] > dist: D[i] = dist` 在数学上等价于 `D[i] = _single(D[i], dist) = min(D[i], dist)`——它就是 single linkage 的距离更新，只是被「内联」成了一句比较赋值，省掉了函数指针调用。
- 松弛之后，`D[i]` 就是「点 \(i\) 到当前整棵树的单链接距离」。接着 `if D[i] < current_min` 在所有未入树点里挑出 `D[i]` 最小的 `y`——这正是 Prim「选离树最近的点」的步骤。
- `Z[k] = [x, y, current_min]`：记录一条 MST 边。`current_min` 是 `y` 到树的距离，也就是这条边的权。
- `x = y`：Prim 的「生长」——下一轮从新并入的 `y` 出发继续松弛。这个细节保证了每加入一个点，它的所有出边都有机会更新 `D`，从而 `D` 始终是「到当前树」的真实最近距离。

最后是收尾的排序（[L1080-L1085](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1080-L1085)）：

```cython
# Sort Z by cluster distances.
order = np.argsort(Z_arr[:, 2], kind='mergesort')
Z_arr = Z_arr[order]

# Find correct cluster labels and compute cluster sizes inplace.
label(Z_arr, n)
```

`kind='mergesort'` 是**稳定排序**：当两条 MST 边权相等时，保持它们被发现的先后顺序，保证结果可复现。`label()` 的作用见 §4.4。

#### 4.3.4 代码实践

**实践目标**：用纯 Python 复刻 `mst_single_linkage` 的 Prim 主循环，验证它产生的「MST 边集合」与 scipy 一致。

**操作步骤**：

```python
import numpy as np
from scipy.spatial.distance import pdist
from scipy.cluster.hierarchy import linkage

def condensed_index(n, i, j):
    if i < j:
        return n * i - (i * (i + 1)) // 2 + (j - i - 1)
    elif i > j:
        return n * j - (j * (j + 1)) // 2 + (i - j - 1)

def mst_single_linkage_py(dists, n):
    """复刻 Prim 主循环，返回(尚未 label 的)排序后 Z。"""
    Z = np.empty((n - 1, 4))
    merged = np.zeros(n, dtype=int)
    D = np.full(n, np.inf)
    x = 0
    for k in range(n - 1):
        current_min = np.inf
        merged[x] = 1
        y = -1
        for i in range(n):
            if merged[i]:
                continue
            dist = dists[condensed_index(n, x, i)]
            if D[i] > dist:          # ← 内联的 _single 松弛
                D[i] = dist
            if D[i] < current_min:
                y = i
                current_min = D[i]
        Z[k, 0] = x
        Z[k, 1] = y
        Z[k, 2] = current_min
        x = y
    order = np.argsort(Z[:, 2], kind='mergesort')
    return Z[order]

# 6 个点
X = np.array([[0., 0.], [0., 1.], [1., 0.],
              [5., 5.], [6., 5.], [5., 6.]])
y = pdist(X)
n = len(X)

Z_py = mst_single_linkage_py(y, n)
Z_sp = linkage(y, method='single')

# 距离列不受 label 影响，可直接比对
print("我的 Z 距离列:", Z_py[:, 2])
print("scipy Z 距离列:", Z_sp[:, 2])
print("距离列一致?", np.allclose(Z_py[:, 2], Z_sp[:, 2]))

# 进一步：把每条边的端点规范化成 (min, max)，比对 MST 边集合
edges_py = {(min(int(a), int(b)), max(int(a), int(b)), round(d, 6))
            for a, b, d in Z_py[:, :3]}
print("MST 边集合(端点为原始观测号):", sorted(edges_py))
```

**需要观察的现象**：

1. `Z_py[:,2]`（距离列）与 scipy 的 `Z_sp[:,2]` 逐元素相等。
2. `Z_py` 的前两列是**原始观测号**，可能和 scipy 的簇编号（含 \(n+k\)）对不上——这正是 `label()` 要解决的问题（见 §4.4 与综合实践）。
3. 把端点规范成 `(min, max)` 后，得到的 MST 边集合描述了同一棵最小生成树。

**预期结果**：`距离列一致? True`；`MST 边集合` 是 5 条边，连接全部 6 个点且无环。

#### 4.3.5 小练习与答案

**练习 1**：把种子点从 `x = 0` 改成 `x = 3`，最终得到的 MST 边集合会变吗？距离列会变吗？

**答案**：MST 边集合**不变**（最小生成树不依赖起点）；距离列（排序后）也**不变**。但「边被发现的先后顺序」可能不同，从而排序前 `Z` 的行序、以及相等边权时的稳定排序结果可能略有差异。这正是代码固定种子点 `x=0` 并用 `kind='mergesort'` 来保证可复现的原因。

**练习 2**：主循环里如果删掉 `if D[i] > dist: D[i] = dist` 这一句松弛，会发生什么？

**答案**：`D[i]` 不再用新入树点更新，于是 `D[i]` 只反映「到第一个种子点的距离」，选出的 `y` 不再是「到整棵树最近的点」，MST 正确性被破坏。这一句就是 single linkage 的距离更新，缺它不可。

---

### 4.4 收尾重编号：`label()` 与 `LinkageUnionFind`

#### 4.4.1 概念说明

Prim 主循环产出的 `Z` 还**不是合法的 linkage matrix**，有两个硬伤：

1. **行序乱**：行按「MST 边被发现」排列，距离不单调递增。
2. **簇号非法**：每行的 `x`、`y` 是原始观测号。可是一个观测一旦被合并，它在后续行里应当以「它所属的那个新簇号 \(n+k\)」的身份出现，而不是继续用原始号。

举一个具体的矛盾：若第 0 行合并了点 0 和点 3（得到新簇 \(n+0\)），那么后面任何引用「点 3」的行，其实指的是「包含点 3 的那个簇 \(n+0\)」，必须改写成 \(n+0\)。`label()` 就是用并查集 [`LinkageUnionFind`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1090-L1118) 来做这件事，并顺手填上第四列簇大小。

> 排序（解决硬伤 1）在 `label()` 之前完成；`label()` 处理硬伤 2 并填第四列。两步合起来把「MST 边序列」翻译成「合法 dendrogram」。

#### 4.4.2 核心流程

`label(Z, n)` 按 Z 行（此时已按距离升序）从上到下扫描，对每一行 \((x, y)\)：

```
uf = LinkageUnionFind(n)             # parent[i]=i, size[i]=1, next_label=n
对第 i 行 (x, y)：
    x_root = uf.find(x)              # x 当前所属簇的代表
    y_root = uf.find(y)              # y 当前所属簇的代表
    把较小的 root 放到 Z[i,0]，较大的放到 Z[i,1]   # 保证 Z[i,0] < Z[i,1]
    Z[i,3] = uf.merge(x_root, y_root) # 合并，返回新簇大小，并分配新号 n+i
```

`LinkageUnionFind` 的特点（u4-l1 §4.3）：合并时**新建一个父节点** \(n+i\) 作为两簇的新代表（而不是把一棵挂到另一棵下），`find` 带路径压缩。`merge` 返回新簇的成员数 = 两子簇 size 之和，正好填进 `Z[i,3]`。

#### 4.4.3 源码精读

[`LinkageUnionFind.merge`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1101-L1107) 把两个孩子都指向一个**全新的**父节点 `next_label`，并累加 size：

```cython
cdef int merge(self, int x, int y) noexcept:
    self.parent[x] = self.next_label
    self.parent[y] = self.next_label
    cdef int size = self.size[x] + self.size[y]
    self.size[self.next_label] = size
    self.next_label += 1
    return size
```

[`find`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1109-L1118) 先爬到根，再把沿途节点直接挂到根（路径压缩）：

```cython
cdef find(self, int x):
    cdef int p = x
    while self.parent[x] != x:        # 第一遍：找到根 x
        x = self.parent[x]
    while self.parent[p] != x:        # 第二遍：路径压缩
        p, self.parent[p] = self.parent[p], x
    return x
```

最后是 [`label`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1121-L1133) 本体——遍历每行，把 `x,y` 翻译成它们当前的簇代表，规整大小顺序，并填第四列：

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

为什么 `label()` 必须在**按距离排序之后**运行？因为合法的 linkage matrix 要求**距离单调不降**，且簇编号 \(n+i\) 必须按合并先后（即距离升序）分配。只有先排好序，`next_label` 自增分配的 \(n, n+1, \dots\) 才会和距离升序一一对应。

#### 4.4.4 代码实践

**实践目标**：手工完成 `label()` 对一组「未重编号 Z」的改写，理解簇号翻译的过程。

**操作步骤**：假设 \(n=4\)，Prim 产出的原始边（已按距离排序）为 `[[1,2,1.0],[0,2,2.0],[3,1,3.0]]`（距离列 1、2、3 已升序）。逐步模拟 `label()`：

```python
# 模拟 label() 的并查集翻译过程
parent = {0:0, 1:1, 2:2, 3:3, 4:4, 5:5, 6:6}   # 2n-1=7 个节点
size   = {i:1 for i in range(7)}
next_label = 4                                  # n=4
raw = [[1,2,1.0],[0,2,2.0],[3,1,3.0]]

def find(x):
    while parent[x] != x:
        x = parent[x]
    return x

Z = []
for i, (x, y, d) in enumerate(raw):
    xr, yr = find(x), find(y)
    a, b = min(xr, yr), max(xr, yr)
    new = size[xr] + size[yr]
    parent[xr] = next_label
    parent[yr] = next_label
    size[next_label] = new
    next_label += 1
    Z.append([a, b, d, new])
    print(f"第{i}行: 合并 {xr},{yr} -> 新簇 {next_label-1}, Z={Z[-1]}")
```

**需要观察的现象**：

- 第 0 行合并点 1、2（都是单点），生成新簇 4，记 `[1,2,1.0,2]`。
- 第 1 行的 `y=2` 现在 `find(2)=4`（点 2 已属于簇 4），于是合并 0 和 4，生成簇 5，记 `[0,4,2.0,3]`——注意这里的 `4` 正是 `label()` 翻译出来的合法簇号。
- 第 2 行的 `1` 现在 `find(1)=5`，合并 3 和 5，生成簇 6，记 `[3,5,3.0,4]`。

**预期结果**：最终 `Z = [[1,2,1,2],[0,4,2,3],[3,5,3,4]]`，是一棵簇号合法、距离单调、第四列（簇大小）正确的 dendrogram。

#### 4.4.5 小练习与答案

**练习**：`LinkageUnionFind.merge` 为什么是「新建一个父节点」而不是「把一棵树的根挂到另一棵的根下」（普通并查集的 union 方式）？

**答案**：因为凝聚式聚类的产物是一棵**二叉 dendrogram**：每次合并产生一个新节点，它恰好有两个孩子（被合并的两簇）。新建父节点 \(n+i\) 自然表达了「这是一个新簇，由两子簇合并而来」，其编号 \(n+i\) 还可直接写进 linkage matrix 的前两列。普通 union-by-rank 会把一个根挂到另一个根下，破坏「每次合并诞生一个新二叉节点」的结构，无法对应 dendrogram。

---

## 5. 综合实践

把本讲三条线索（MST 等价性、Prim 主循环、label 重编号）串起来，完整复现 scipy 对经典 `ytdist` 数据集（测试套件 `hierarchy_test_data.ytdist` 用的 6 点距离矩阵）的 single linkage 计算，并**逐行手工验证**。

**数据**（\(n=6\) 的压缩距离矩阵，共 \(6\cdot5/2=15\) 个元素）：

```python
import numpy as np
from scipy.cluster.hierarchy import linkage

# hierarchy_test_data.ytdist 的内容（scipy/cluster/hierarchy/tests/hierarchy_test_data.py）
ytdist = np.array([662., 877., 255., 412., 996., 295., 468., 268., 400.,
                   754., 564., 138., 219., 869., 669.])

Z_sp = linkage(ytdist, method='single')
print("scipy 结果:\n", Z_sp)
# 期望（== hierarchy_test_data.linkage_ytdist_single）：
# [[2., 5., 138., 2.],
#  [3., 4., 219., 2.],
#  [0., 7., 255., 3.],
#  [1., 8., 268., 4.],
#  [6., 9., 295., 6.]]
```

**手工追踪 Prim 主循环**（种子 `x=0`，`D` 初值全 `inf`，`merged` 全 0）。下表的 `D[i]` 是每轮松弛**之后**、选 `y` **之前**各未入树点到当前树的最近距离：

| 轮 k | 入树点 x | 松弛用到的关键边 | 选出的 y | current_min | 原始 Z 行 `[x,y,d]` |
|------|----------|------------------|----------|-------------|----------------------|
| 0 | 0 | `d(0,1)=662,d(0,2)=877,d(0,3)=255,d(0,4)=412,d(0,5)=996` | 3 | 255 (`d(0,3)`) | `[0, 3, 255]` |
| 1 | 3 | `d(3,1)=468,d(3,2)=754,d(3,4)=219,d(3,5)=869` | 4 | 219 (`d(3,4)`) | `[3, 4, 219]` |
| 2 | 4 | `d(4,1)=268,d(4,2)=564,d(4,5)=669` | 1 | 268 (`d(4,1)`) | `[4, 1, 268]` |
| 3 | 1 | `d(1,2)=295,d(1,5)=400` | 2 | 295 (`d(1,2)`) | `[1, 2, 295]` |
| 4 | 2 | `d(2,5)=138` | 5 | 138 (`d(2,5)`) | `[2, 5, 138]` |

> 说明：例如第 1 轮 `x=3` 时，松弛把 `D[1]` 从 662 降到 468、`D[4]` 从 412 降到 219、`D[2]` 从 877 降到 754、`D[5]` 从 996 降到 869；最小的是 `D[4]=219`，故 `y=4`。注意第 1 轮 `d(3,1)` 是用 `condensed_index(6,3,1)` 取的——因为 `3>1`，走 `i>j` 分支，下标等于 `condensed_index(6,1,3)`，对应 `ytdist[6]=468`。

**排序**（按距离稳定升序）：距离序列 `(255,219,268,295,138)` → 升序 `(138,219,255,268,295)`，对应行序 `(第4行,第1行,第0行,第2行,第3行)`：

```
[2, 5, 138]
[3, 4, 219]
[0, 3, 255]   ← "3" 此前已合并，需 label 翻译
[4, 1, 268]   ← "4" 此前已合并
[1, 2, 295]   ← "1" 此前已合并
```

**`label()` 重编号**（`next_label` 从 \(n=6\) 开始）：

| 行 | find(x), find(y) | 规整后 `[a,b]` | merge 产出新簇 | `Z[i,3]`(簇大小) |
|----|------------------|----------------|----------------|-------------------|
| `[2,5,138]` | 2, 5（均单点） | `[2,5]` | 新簇 6 | 2 |
| `[3,4,219]` | 3, 4（均单点） | `[3,4]` | 新簇 7 | 2 |
| `[0,3,255]` | 0, find(3)=7 | `[0,7]` | 新簇 8 | 3 |
| `[4,1,268]` | find(4)=8, 1 | `[1,8]` | 新簇 9 | 4 |
| `[1,2,295]` | find(1)=9, find(2)=6 | `[6,9]` | 新簇 10 | 6 |

最终得到：

```
[[2., 5., 138., 2.],
 [3., 4., 219., 2.],
 [0., 7., 255., 3.],
 [1., 8., 268., 4.],
 [6., 9., 295., 6.]]
```

这恰好等于 `hierarchy_test_data.linkage_ytdist_single`，也与 `linkage(ytdist, method='single')` 完全一致。**这便从 MST 的第一性原理，端到端复现了 scipy 的 single linkage 后端。**

**延伸任务**（待本地验证）：在 §4.3.4 的纯 Python `mst_single_linkage_py` 基础上，再补一个 `label_py(Z, n)`（用字典模拟 `LinkageUnionFind` 的 `find`/`merge`），让它输出与 `linkage(ytdist, method='single')` **逐元素相同**的完整 4 列 Z。提示：直接照搬 §4.4.4 的并查集模拟代码，外层套一个对 `Z` 每行的循环即可。

## 6. 本讲小结

- **single linkage ≡ 最小生成树**：因为 `_single = min`，单链接聚类的每一步合并恰好对应距离图 MST 的一条边；这让 single 拥有专属的 \(O(n^2)\) Prim 后端，是七种方法里唯一不需要 Lance-Williams 距离更新的方法。
- **`mst_single_linkage` 是 Prim 的直接落地**：用 `D[i]`（点 \(i\) 到当前树的最近距离）+ `merged[]` 标记实现松弛，每轮 \(O(n)\) 选出下一个并入点；`if D[i] > dist: D[i] = dist` 这一句就是把 `_single` 内联进了松弛步。
- **[`condensed_index`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L20-L29) 是地址翻译层**：让一维压缩距离矩阵能像二维表一样被 `dists[condensed_index(n, x, i)]` 访问，所有聚类算法共用。
- **末尾两步收尾是必需的**：Prim 产出的行序乱、簇号非法，必须先按距离稳定排序、再用 [`label()`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1121-L1133) 借 [`LinkageUnionFind`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1090-L1118) 把原始观测号翻译成合法的 \(n+k\) 簇号并填第四列簇大小。
- **复杂度**：时间 \(O(n^2)\)、空间 \(O(n)\)，是 single linkage 的最优实现；对比 `fast_linkage` 的 \(O(n^3)\) 与 `nn_chain` 的 \(O(n^2)\)（但需维护链结构），MST 路径最简洁。

## 7. 下一步学习建议

- **继续本单元**：进入 **u4-l3**，学习 `nn_chain`（complete/average/weighted/ward 的最近邻链算法）与 `fast_linkage`（centroid/median 的朴素算法）。重点对照它们与 `mst_single_linkage` 的差异：为什么只有 single 能用 MST？为什么 centroid/median 不能用 nn_chain 而必须用 \(O(n^3)\) 的 `fast_linkage`？关键概念是方法的**可还原性（reducibility）**。
- **回顾数据结构**：若对 `LinkageUnionFind` 的路径压缩或 `condensed_index` 的推导还不够熟，回到 **u4-l1** 复习；`fast_linkage` 用到的可改值堆 `Heap` 也在那里。
- **阅读源码建议**：在 [`_hierarchy.pyx`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx) 中并排阅读 `mst_single_linkage`（L1032）、`nn_chain`（L924）、`fast_linkage`（L792）三个函数，体会「同一份压缩距离矩阵输入、同一份 (n-1,4) 的 Z 输出契约」下，三种算法如何各展所长。
- **跑测试**：用 `pytest scipy/cluster/hierarchy/tests/test_hierarchy.py -k "single"` 跑一遍 single 相关测试，结合本讲的手工追踪加深理解。
