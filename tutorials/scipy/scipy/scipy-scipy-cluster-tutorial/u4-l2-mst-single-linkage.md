# single linkage 的最小生成树算法

## 1. 本讲目标

本讲精读 `scipy.cluster.hierarchy` 后端里**最简洁、最快**的一条聚类路径：`mst_single_linkage`。它是 `linkage(method='single')` 的专属后端，用一种 **Prim 风格的最小生成树（MST）算法**把 \(O(n^2)\) 的时间复杂度做到极致——既不需要 `fast_linkage` 那套可改值堆，也不需要 `nn_chain` 的最近邻链。

学完后你应当能够：

- 解释**为什么 single linkage 聚类等价于求距离矩阵的最小生成树**，并由此理解它为何能避开 Lance-Williams 距离更新公式。
- 看懂 `mst_single_linkage` 主循环：用 `D[i]`（点 \(i\) 到「当前树」的最近距离）+ `merged[]` 标记实现 Prim 松弛，每轮 \(O(n)\) 选出下一个并入点。
- 说出代码里**把 `_single = min` 内联进松弛步**的几何含义，并对照 u3-l4 的 Lance-Williams 公式。
- 理解末尾「按距离排序 + `label()` 重编号」两步为何是**必需**的收尾（承接 u4-l1 的 `LinkageUnionFind`）。
- 用纯 Python 复刻一版 Prim 版 single linkage，并在 6 点小数据集上与 `scipy.cluster.hierarchy.linkage(method='single')` 逐元素对齐。

> 本讲的两个数据结构协作方（`condensed_index`、`LinkageUnionFind`）已在 **u4-l1** 详细讲过，这里只讲它们在 MST 算法里的**用法**，不重复内部实现。Lance-Williams 公式见 **u3-l4**，linkage matrix 数据结构见 **u3-l1**。

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
- **Cython 语法速查**：`cdef` 声明 C 变量、`cdef int[:] x` 是类型化内存视图、`noexcept` 省去异常检查、`const double[:]` 表示只读视图。

## 3. 本讲源码地图

本讲几乎只读一个 Cython 文件，外加一个距离更新片段和一个 Python 桥接点：

| 文件 | 作用 | 本讲关注的部分 |
|------|------|----------------|
| `hierarchy/_hierarchy.pyx` | 所有聚类算法的 Cython 实现 | `mst_single_linkage`（主角）、`label()`、`condensed_index` |
| `hierarchy/_hierarchy_distance_update.pxi` | 七种方法的 Lance-Williams 更新函数 | `_single`（被 MST 算法「内联」，不直接调用） |
| `hierarchy/_hierarchy_impl.py` | Python 封装层 | `cy_linkage` 闭包里 `method == 'single'` 的分派 |

> `mst_single_linkage` 是唯一一个**不接受 `method` 参数**的聚类后端——因为它只为 single 服务，方法已写死。对照 `nn_chain(dists, n, method)` 与 `fast_linkage(dists, n, method)` 都带 `method`。

## 4. 核心概念与源码讲解

### 4.1 单链接聚类等价于最小生成树

#### 4.1.1 概念说明

凝聚式 single linkage 的朴素做法是：每一步在所有「现存的簇对」里找单链接距离最小的那一对合并，再用 \(d_{\text{single}}(A\cup B, C)=\min(d(A,C),d(B,C))\) 更新距离矩阵。这就是 u3-l4 里 `_single` 做的事。朴素实现每步要扫 \(O(n^2)\) 个簇对，共 \(n-1\) 步，复杂度 \(O(n^3)\)。

但 single linkage 有一个**漂亮的等价性质**：把每个观测看作图的一个顶点，两两距离看作边权，则

> **single linkage 聚类产生的合并序列，等价于该完全图的「最小生成树（MST）」的边按权值升序加入的序列。**

直觉上：每次合并「最近的两簇」，本质上就是加入一条「连接两个连通分量的最短边」——这正是 MST 的定义（Kruskal 算法就是这么建 MST 的）。而 single linkage 的距离更新 `min(d(A,C),d(B,C))` 恰好保证：合并 \(A,B\) 后，新簇到 \(C\) 的距离就是 MST 里 \(C\) 到 \(\{A,B\}\) 的最短边。

于是求 single linkage dendrogram ⟺ **求一棵 MST，再把它的 \(n-1\) 条边按权值排序、依次合并两端点所在的连通分量**。这把问题从「反复维护 \(O(n^2)\) 距离矩阵」降为「求一次 MST」。

#### 4.1.2 核心流程：Prim 风格的 MST

求 MST 有两大经典算法：Kruskal（按边权排序 + 并查集）和 Prim（从一点出发，逐步把「离当前树最近的点」并入）。`scipy` 选了 **Prim**，因为它在稠密图（完全距离矩阵正是稠密图）上是 \(O(n^2)\)，而 Kruskal 要先排序 \(O(n^2)\) 条边，是 \(O(n^2\log n)\)。

Prim 的核心是维护一个数组：

\[
D[i] = \text{点 } i \text{ 到「当前已并入的树」的最近距离}
\]

每轮做两件事（伪代码）：

```
merged[x] = True                         # 把上一轮选出的 x 并入树
对每个未并入的 i：D[i] = min(D[i], dist(x, i))   # 用 x 的所有边「松弛」D
y = argmin_{未并入} D[i]                  # 选离树最近的点 y
记录一条 MST 边 (x, y, D[y])
x = y                                    # 下一轮以 y 为松弛中心
```

第一行松弛 `D[i] = min(D[i], dist(x, i))` 就是 Prim 的「扩展前沿」：新并入的 \(x\) 可能给某些点带来更近的树距离。最后 `argmin D[i]` 选出全局离树最近的点加入。重复 \(n-1\) 轮即得一棵生成树。

> 关键观察：这个松弛步 `min(D[i], dist(x,i))` 在数学上**正是** `_single` 的距离更新 `min(d_xi, d_yi)`——只不过 `_single` 是「合并两簇后更新到第三簇」，而 Prim 是「并入一点后更新到所有未并入点」。两者都是 single linkage 的 `min`。所以 `mst_single_linkage` **从不调用** `linkage_methods[method]` 函数指针表，而是把 `_single=min` 直接内联进松弛循环（u3-l4 末尾提到过这个「唯一例外」）。

#### 4.1.3 源码精读：分派入口与被内联的 `_single`

先看 Python 层如何把 `method='single'` 路由到这个后端：

[`hierarchy/_hierarchy_impl.py` L955-L965](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L955-L965) —— `cy_linkage` 闭包里，`single` 单独走 `mst_single_linkage`，其余方法走 `nn_chain` / `fast_linkage`：

```python
def cy_linkage(y, validate):
    if validate and not np.all(np.isfinite(y)):
        raise ValueError("The condensed distance matrix must contain only "
                         "finite values.")
    if method == 'single':
        return _hierarchy.mst_single_linkage(y, n)
    elif method in ('complete', 'average', 'weighted', 'ward'):
        return _hierarchy.nn_chain(y, n, method_code)
    else:
        return _hierarchy.fast_linkage(y, n, method_code)
```

注意 `mst_single_linkage(y, n)` **没有 `method_code` 参数**——方法已写死为 single。

再看被「内联」的那个 `_single` 长什么样：

[`hierarchy/_hierarchy_distance_update.pxi` L30-L32](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_distance_update.pxi#L30-L32) —— single linkage 的距离更新就是取 `min`：

```cython
cdef double _single(double d_xi, double d_yi, double d_xy,
                    int size_x, int size_y, int size_i) noexcept:
    return min(d_xi, d_yi)
```

`mst_single_linkage` 里看不到 `_single` 的调用，因为它把 `min` 直接写进了松弛步（见 4.2.3 的 `if D[i] > dist: D[i] = dist`，等价于 `D[i] = min(D[i], dist)`）。这是 single linkage 能用 MST 求解的根源：`min` 满足「可结合性」，使得 `D[i] = min` over tree nodes 可以增量维护，而不必每次合并后重算。

#### 4.1.4 代码实践：直观验证「single = 排序后的 MST 边」

**实践目标**：用一个独立方法求 MST 的边权，验证它升序排列后就是 `scipy` single linkage 的 `Z[:,2]`（距离列）。

**操作步骤**：

1. 取 6 个二维点，用 `pdist` 得到压缩距离矩阵。
2. 调 `linkage(method='single')` 得到 `Z`，取出距离列 `Z[:,2]`。
3. 用 `scipy.sparse.csgraph.minimum_spanning_tree`（或 `scipy.spatial.distance.squareform` 还原成稠密矩阵后求 MST）独立求一棵 MST，收集它的边权并升序排序。
4. 比较两组数据是否逐项相等。

```python
# 示例代码（非项目源码，验证 single linkage 距离列 == MST 边权升序）
import numpy as np
from scipy.spatial.distance import pdist, squareform
from scipy.sparse.csgraph import minimum_spanning_tree
from scipy.cluster.hierarchy import linkage

X = np.array([[0., 0], [0, 1], [0, 2], [5, 0], [5, 1], [9, 5]])
y = pdist(X)
n = len(X)

Z = linkage(y, method='single')
single_dists = np.sort(Z[:, 2])

# 独立求 MST 的边权
G = squareform(y)
mst = minimum_spanning_tree(G).toarray()
mst_dists = np.sort(mst[mst > 0])

print("single 距离列:", single_dists)
print("MST 边权   :", mst_dists)
assert np.allclose(single_dists, mst_dists), "两者应一致"
print("一致：single linkage 的合并距离正是 MST 边权")
```

**需要观察的现象 / 预期结果**：两组数据完全相等，断言通过。这从「另一条独立路径」印证了 single linkage = MST。

> 运行结果：本示例依赖 SciPy；若本机未装则标注「待本地验证」。具体数值随数据而变，但「两列相等」这一结论恒成立。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `mst_single_linkage` 的函数签名里没有 `method` 参数，而 `nn_chain` / `fast_linkage` 都有？

**答案**：因为 MST 等价只对 single linkage 成立（其 `min` 更新可增量维护）。该方法已写死，不需要分派；而 `nn_chain` / `fast_linkage` 要用同一个函数指针表 `linkage_methods[method]` 服务 complete/average/ward/centroid/median 等多种方法，故需 `method` 入参。

**练习 2**：若改用 Kruskal 求 MST 再合并，复杂度会比 Prim 的 \(O(n^2)\) 高还是低？

**答案**：更高。Kruskal 需要先对 \(n(n-1)/2=O(n^2)\) 条边排序，复杂度 \(O(n^2\log n)\)；Prim 在稠密完全图上是 \(O(n^2)\)。完全距离矩阵正是稠密图，故 Prim 更优。

**练习 3**：`complete` linkage（取 `max`）能不能也用 MST 求解？

**答案**：不能。complete 的距离更新 `max(d_xi,d_yi)` 不具备 `min` 那种「可增量结合」的性质——合并两簇后到第三簇的距离可能**变大**，无法用一棵生成树的边权单调表达。所以 complete 走 `nn_chain`，而非 MST。

---

### 4.2 mst_single_linkage 主循环：D 数组与 Prim 松弛

#### 4.2.1 概念说明

4.1 讲了「为什么用 MST」，本节讲「具体怎么用 Prim 跑」。核心是三个数组和一个游标：

- **`merged[i]`**（int，0/1）：点 \(i\) 是否已被并入树。Prim 的「已访问」标记。
- **`D[i]`**（double）：点 \(i\) 到「当前树」的最近距离。初始化全 `+inf`。这是 Prim 的灵魂数组。
- **`x`**（int）：**当前松弛中心**，即「上一轮刚并入树的点」。每轮用它的边去松弛所有未并入点。初始 `x=0`（任选起点）。
- **`y`**（int）：每轮 `argmin D` 选出的「离树最近的未并入点」，本轮要并入。

注意一个细节：代码**只维护 `D[i]`，不维护「是哪个树节点给出了这个最小值」**（教科书 Prim 通常会维护一个 `link[i]`）。它直接把 `Z[k,0]` 记成松弛中心 `x`——这会引出一个微妙问题，留到 4.2.3 的「进阶说明」细讲。

#### 4.2.2 核心流程与复杂度

每一轮 \(k=0,\dots,n-2\)：

1. `merged[x] = 1`：把上一轮选出的 \(x\) 正式标记为已并入。
2. 遍历所有未并入 \(i\)：
   - `dist = dists[condensed_index(n, x, i)]`（读 \(x\) 到 \(i\) 的原始距离）。
   - 松弛：`if D[i] > dist: D[i] = dist`（即 `D[i] = min(D[i], dist(x,i))`）。
   - 顺手跟踪本轮最小：`if D[i] < current_min: y = i; current_min = D[i]`。
3. 写一行 linkage 记录：`Z[k] = (x, y, current_min)`。
4. `x = y`：下一轮以新并入的 \(y\) 为松弛中心。

复杂度：外层 \(n-1\) 轮，每轮内层遍历 \(n\) 个点做 \(O(1)` 松弛与比较，总计 \(O(n^2)\)，**不需要堆**（对比 `fast_linkage` 用可改值堆来选最小）。这是 single linkage 在稠密图上能达到的最优复杂度。

#### 4.2.3 源码精读

先看初始化：

[`hierarchy/_hierarchy.pyx` L1047-L1057](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1047-L1057) —— 分配 Z、`merged` 全 0、`D` 全 `INFINITY`、松弛中心 `x=0`：

```cython
Z_arr = np.empty((n - 1, 4))
cdef double[:, :] Z = Z_arr

# Which nodes were already merged.
cdef int[:] merged = np.zeros(n, dtype=np.intc)

cdef double[:] D = np.empty(n)
D[:] = INFINITY

cdef int i, k, x, y = 0
cdef double dist, current_min

x = 0
```

再看主循环（本节的核心）：

[`hierarchy/_hierarchy.pyx` L1060-L1078](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1060-L1078) —— 每轮：标记 \(x\) 已并入 → 用 `condensed_index` 读 \(x\) 到各未并入点的距离 → 松弛 `D` → 跟踪最小 → 写一行 → 把 \(y\) 设为下一轮的 \(x`：

```cython
for k in range(n - 1):
    current_min = INFINITY
    merged[x] = 1
    for i in range(n):
        if merged[i] == 1:
            continue

        dist = dists[condensed_index(n, x, i)]
        if D[i] > dist:
            D[i] = dist

        if D[i] < current_min:
            y = i
            current_min = D[i]

    Z[k, 0] = x
    Z[k, 1] = y
    Z[k, 2] = current_min
    x = y
```

逐行对照 4.2.2 的步骤，可以看到：

- `merged[x] = 1` 把上一轮（或初始）的 \(x\) 并入树。
- `condensed_index(n, x, i)` 是 u4-l1 §4.1 讲的地址翻译：把 \((x,i)\) 折算到一维压缩距离矩阵 `dists` 的下标。**这是本函数里 `condensed_index` 唯一被调用之处**。
- `if D[i] > dist: D[i] = dist` 就是 Prim 松弛，也就是被内联的 `_single = min`。
- 内层循环边松弛边找最小，省去单独再扫一遍 `argmin`。
- 末尾 `x = y` 让下一轮以新点为松弛中心。

注意函数只填了 `Z` 的前三列（两簇编号 + 距离），**第四列（簇大小）没有在这里填**——它由后面的 `label()` 统一回填。

> **进阶说明：`Z[k,0]=x` 记的真是 \(y\) 在树中的最近邻吗？** 不一定。代码里的 \(x\) 是「上一轮刚并入树的点」，而 \(y\) 在树中**真正的**最近邻 \(x'\)（即 `argmin` 树节点）可能是更早并入的某个点——只要 \(\mathrm{dist}(x',y) < \mathrm{dist}(x,y)\)。那么用 \(x\) 顶替 \(x'\) 会不会算错？**不会**。可以证明一个干净的事实：
>
> **只要 \(x \neq x'\)，就必然有当前边权 \(w_k \geq\) 上一条边权 \(w_{k-1}\)。**
>
> 理由：\(x'\neq x\) 意味着第 \(k-1\) 轮（松弛中心恰是 \(x\)）的松弛**没有**让 \(x\) 成为 \(y\) 的新最近邻，即 \(\mathrm{dist}(x,y) > D[y]\)，于是 \(D[y]\) 在第 \(k-1\) 轮之后没变小；而第 \(k-1\) 轮选中 \(x\) 时，\(D[x]\) 正是当时的全局最小，故 \(D[y]\geq D[x]=w_{k-1}\)，即 \(w_k=D[y]\geq w_{k-1}\)。
>
> 由此推出：排序时连接 \(x\) 的那条边（权 \(w_{k-1}\leq w_k\)）会**先于**当前边被处理（用稳定的 mergesort，等权时按原顺序 \(k-1\) 在 \(k\) 之前），于是 \(x\) 在当前边被处理前已被并入「已合并子树」，`label()` 的 `find(x)` 与 `find(x')` 解析到**同一个代表**——最终 dendrogram 与真正的 single linkage 完全一致。这也是为什么本函数敢省掉教科书 Prim 里的 `link[i]` 数组。

#### 4.2.4 代码实践：手算一轮松弛

**实践目标**：用最小数据集手算 Prim 的前两轮，把 `D` 数组与 `merged` 的变化画出来，建立对「松弛 + argmin」的肌肉记忆。

**操作步骤**：

1. 取 4 个点，距离矩阵如下（已写成压缩形式，对应对 (0,1),(0,2),(0,3),(1,2),(1,3),(2,3)）：

   \[
   \text{dists} = [1,\ 5,\ 6,\ 2,\ 4,\ 3]
   \]

   即 \(\mathrm{dist}(0,1)=1,\ \mathrm{dist}(0,2)=5,\ \mathrm{dist}(0,3)=6,\ \mathrm{dist}(1,2)=2,\ \mathrm{dist}(1,3)=4,\ \mathrm{dist}(2,3)=3\)。
2. 初始 `merged=[0,0,0,0]`，`D=[inf,inf,inf,inf]`，`x=0`。
3. 手算第 0 轮：`merged[0]=1`；用 \(x=0\) 松弛：`D=[inf,1,5,6]`；`argmin`=点 1（D=1）。写 `Z[0]=(0,1,1)`，`x=1`。
4. 手算第 1 轮：`merged[1]=1`；用 \(x=1\) 松弛未并入 {2,3}：`D[2]=min(5,dist(1,2)=2)=2`，`D[3]=min(6,dist(1,3)=4)=4`；`argmin`=点 2（D=2）。写 `Z[1]=(1,2,2)`，`x=2`。
5. 再算第 2 轮，应得 `Z[2]=(2,3,3)`。
6. 用下面的示例代码核对：

```python
# 示例代码（非项目源码，核对上面的手算）
import numpy as np
from scipy.cluster.hierarchy import linkage

dists = [1, 5, 6, 2, 4, 3]      # 4 个点的压缩距离矩阵
Z = linkage(dists, method='single')
print(np.round(Z, 4))
# 期望（前三列距离）：(0,1,1),(1,2,2),(?, ?, 3) —— 第四列大小由 label() 填
```

**需要观察的现象 / 预期结果**：手算的 `Z[:,2]` = `[1, 2, 3]`，与代码输出的距离列一致；前两列的具体编号取决于 `label()` 的重排（4.3 节），但距离序列严格递增，对应 MST 的三条边。

> 运行结果：可在装好 SciPy 的环境直接核对；若无环境则标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：把初始 `x` 从 0 改成 2，最终得到的 MST 边权集合会变吗？

**答案**：不会。MST 的边权集合由距离矩阵决定，与起点无关（Prim 算法的性质）。改变的只是边被发现的**顺序**，因而 `Z[k,0]=x` 的具体编号会变，但经排序 + `label()` 后，最终 dendrogram 完全相同。

**练习 2**：为什么内层循环要「边松弛边找最小」，而不是先松弛完、再单独 `argmin`？

**答案**：纯为省一次遍历。松弛和找最小都要扫一遍未并入点，合并成一个循环就把每轮成本从 \(O(2n)\) 降到 \(O(n)\)，对 \(O(n^2)\) 总复杂度而言常数减半。

**练习 3**：函数里完全没有调用 `Heap`（u4-l1 §4.2），为什么 single linkage 不需要堆？

**答案**：Prim 每轮都**全量扫描**未并入点找 `argmin D`，用普通数组 + 线性扫描即可 \(O(n)\)/轮。`Heap` 的价值是「按 key 改值后快速取最小」，服务于 `fast_linkage` 那种「猜测可能过期、需要随机更新」的场景；Prim 的 `D` 只会单调变小且每轮整体重扫，用堆反而徒增复杂度。

---

### 4.3 收尾：按距离排序 + `label()` 重编号

#### 4.3.1 概念说明

主循环产出的原始 `Z` 有两个问题，不能直接返回给用户：

1. **行序不一定按距离升序**。Prim 按发现顺序产出边，但相邻边的权值并非单调递增（回忆 4.2.3，\(w_k\) 可以小于 \(w_{k-1}\)）。而合法的 linkage matrix 要求 `Z[:,2]` 单调不减。
2. **第一列 `Z[k,0]=x` 可能引用「已被合并掉的旧簇编号」**，且第四列（簇大小）根本没填。

这两个问题分别由「排序」和「`label()`」解决。这一步是 `mst_single_linkage` 与 `nn_chain` **共享**的收尾套路（u4-l1 §4.3 末尾已指出），而 `fast_linkage` 因为在合并时就维护好了正确的编号与大小，**不需要**这一步。

#### 4.3.2 核心流程

```
order = argsort(Z[:,2], kind='mergesort')   # 按距离升序、稳定排序
Z = Z[order]
label(Z, n)                                  # 用 LinkageUnionFind 重写前两列与第四列
```

`label()`（u4-l1 §4.3）对每一行：用 `find()` 把行里两个簇 id 翻译成「当前活跃代表」、按大小排序写回前两列、用 `merge()` 得到的新大小填第四列。这一步既修正了 4.2.3 提到的「\(x\) 顶替 \(x'\)」问题，也补上了缺失的簇大小。

#### 4.3.3 源码精读

[`hierarchy/_hierarchy.pyx` L1080-L1085](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1080-L1085) —— `mst_single_linkage` 末尾的两步收尾：

```cython
# Sort Z by cluster distances.
order = np.argsort(Z_arr[:, 2], kind='mergesort')
Z_arr = Z_arr[order]

# Find correct cluster labels and compute cluster sizes inplace.
label(Z_arr, n)
```

注意三点：

- `kind='mergesort'` 是**稳定排序**。当多条边等权时，保持它们在原始 `Z` 里的先后顺序（即 Prim 的发现顺序）。这正是 4.2.3「进阶说明」里 \(w_{k-1}=w_k\) 时仍能保证 \(x\) 先被并入的关键。
- `label` 是 `cdef` 函数（仅 Cython 内部可见），原地修改 `Z`，无返回值。它的引擎 `LinkageUnionFind` 见 u4-l1 §4.3。
- 这两步与 `nn_chain` 末尾 [`hierarchy/_hierarchy.pyx` L1022-L1027](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1022-L1027) 逐字相同，是两者的共同收尾模式。

[`hierarchy/_hierarchy.pyx` L1121-L1133](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1121-L1133) —— `label` 主体：对每行用 `find` 翻译代表、排序写回、用 `merge` 填大小（详见 u4-l1 §4.3.3）：

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

> 经过 `label()` 后，4.2.3 里那个「\(x\) 顶替 \(x'\)」的顾虑被彻底消除：无论 Prim 记录的是 \(x\) 还是真正的最近邻 \(x'\)，`find()` 都把它们解析成当前代表，写出合法的簇编号与簇大小。

#### 4.3.4 代码实践：对比「排序前 / 排序后 / label 后」

**实践目标**：直接观察排序与 `label()` 各自修正了什么，理解它们为何缺一不可。

**操作步骤**：

1. 用 4.2.4 的 4 点数据，调用 `scipy` 的 `linkage(method='single')` 得到最终 `Z`。
2. 自己用纯 Python 跑一遍 Prim（4.2.2 的伪代码），得到**未排序、未 label** 的原始 `Z_raw`。
3. 对比三个版本：`Z_raw`（行序乱、第四列空）、`Z_sorted`（仅排序）、最终 `Z`（排序 + label）。

```python
# 示例代码（非项目源码，观察三阶段差异）
import numpy as np
from scipy.cluster.hierarchy import linkage

def condensed_index(n, i, j):
    if i < j:  return n*i - i*(i+1)//2 + (j-i-1)
    if i > j:  return n*j - j*(j+1)//2 + (i-j-1)

def prim_raw(dists, n):
    INF = float('inf'); merged = [0]*n; D = [INF]*n
    Z = []; x = 0
    for _ in range(n-1):
        merged[x] = 1; cur = INF; y = -1
        for i in range(n):
            if merged[i]: continue
            d = dists[condensed_index(n, x, i)]
            if D[i] > d: D[i] = d
            if D[i] < cur: cur = D[i]; y = i
        Z.append([x, y, cur, 0])      # 第四列留空，label() 才填
        x = y
    return np.array(Z, dtype=float)

dists = [1, 5, 6, 2, 4, 3]; n = 4
Z_raw = prim_raw(dists, n)
print("原始（行序随 Prim 发现顺序，第4列空）:\n", Z_raw)
order = np.argsort(Z_raw[:, 2], kind='mergesort')
print("仅排序:\n", Z_raw[order])
print("scipy 最终（排序+label）:\n", linkage(dists, method='single'))
```

**需要观察的现象 / 预期结果**：

- `Z_raw` 的距离列不单调（取决于数据），第四列全 0，且第一列可能引用「旧」编号。
- 仅排序后距离列变单调，但前两列编号与第四列仍不对。
- `scipy` 最终输出前两列为合法簇编号（如 `(0,1),(2,3),(...)`），第四列为正确的簇大小（2、2、4 之类）。

> 运行结果：可在装好 SciPy 的环境核对；若无环境则标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么排序必须用**稳定**的 mergesort，而不能用默认的 quicksort？

**答案**：等权边的处理顺序会影响合并结果（哪两个簇先合并）。稳定的 mergesort 在等权时保持 Prim 的发现顺序，从而保证 4.2.3「进阶说明」里 \(w_{k-1}=w_k\) 时 \(x\) 仍先被并入。quicksort 不稳定，等权边的相对顺序会被打乱，可能改变 dendrogram 结构。

**练习 2**：如果删掉 `label(Z_arr, n)` 这一行，返回的 `Z` 还能通过 `is_valid_linkage` 校验吗？

**答案**：基本不能。第四列簇大小会是 0（主循环没填），且前两列可能引用已被合并的旧 id、不满足 `Z[i,0] < Z[i,1]` 与编号合法性。`label()` 同时修复编号与大小，是返回合法 Z 的必需步骤。

**练习 3**：`fast_linkage` 为什么不需要这两步收尾？

**答案**：`fast_linkage` 在合并时直接执行 `cluster_id[y] = n + k`，始终用「当前活跃代表」写 Z，并当场维护 `size` 数组填好第四列（`Z[k,3] = nx + ny`），产出的 Z 天然有序、编号与大小正确，无需后处理。这正是 u4-l1 §4.3 末尾指出的「谁需要 `label`、谁不需要」的分界线。

---

## 5. 综合实践

把本讲三节串起来，**用纯 Python 完整复刻一版 Prim 版 single linkage**，并在 6 点小数据集上与 `scipy.cluster.hierarchy.linkage(method='single')` 逐元素对齐。这是本讲的核心实践任务。

**任务**：

1. 用 `pdist` 得到 6 点的压缩距离矩阵 `y`。
2. 实现 `condensed_index`（u4-l1 §4.1）以便读一维 `y`。
3. 实现 Prim 主循环（4.2.2）：`merged` 标记 + `D` 数组松弛 + `argmin` 选点，产出原始 `Z_raw`（前三列）。
4. 按距离 mergesort 排序，再实现一个最简 `LinkageUnionFind`（u4-l1 §4.3）做 `label()`，回填前两列编号与第四列大小。
5. 与 `scipy` 的输出逐元素比较（前三列距离、簇编号、簇大小）。

```python
# 示例代码（非项目源码，综合复刻 mst_single_linkage）
import numpy as np
from scipy.spatial.distance import pdist
from scipy.cluster.hierarchy import linkage as scipy_linkage, is_valid_linkage

def condensed_index(n, i, j):
    if i < j:  return n*i - i*(i+1)//2 + (j-i-1)
    if i > j:  return n*j - j*(j+1)//2 + (i-j-1)

class UF:                                 # u4-l1 §4.3 的并查集精简版
    def __init__(self, n):
        self.parent = list(range(2*n-1)); self.size = [1]*(2*n-1); self.nl = n
    def merge(self, x, y):
        self.parent[x] = self.parent[y] = self.nl
        s = self.size[x] + self.size[y]; self.size[self.nl] = s; self.nl += 1
        return s
    def find(self, x):
        p = x
        while self.parent[x] != x: x = self.parent[x]      # 走到根
        while self.parent[p] != x:                          # 路径压缩
            p, self.parent[p] = self.parent[p], x
        return x

def label(Z, n):                          # 4.3 的重编号
    uf = UF(n)
    for i in range(n-1):
        x, y = int(Z[i,0]), int(Z[i,1])
        xr, yr = uf.find(x), uf.find(y)
        if xr < yr: Z[i,0], Z[i,1] = xr, yr
        else:       Z[i,0], Z[i,1] = yr, xr
        Z[i,3] = uf.merge(xr, yr)

def my_mst_single(y, n):                  # 4.2 的 Prim 主循环
    INF = float('inf'); merged = [0]*n; D = [INF]*n
    rows = []; x = 0
    for _ in range(n-1):
        merged[x] = 1; cur = INF; ynode = -1
        for i in range(n):
            if merged[i]: continue
            d = y[condensed_index(n, x, i)]
            if D[i] > d: D[i] = d        # Prim 松弛 = 内联的 _single = min
            if D[i] < cur: cur = D[i]; ynode = i
        rows.append([x, ynode, cur, 0])   # 第四列留给 label()
        x = ynode
    Z = np.array(rows, dtype=float)
    order = np.argsort(Z[:,2], kind='mergesort')
    Z = Z[order]
    label(Z, n)
    return Z

X = np.array([[0.,0],[0,1],[0,2],[5,0],[5,1],[9,5]])
y = pdist(X); n = len(X)

mine = my_mst_single(y, n)
ref  = scipy_linkage(y, method='single')

print("我的 Z:\n", np.round(mine, 6))
print("SciPy Z:\n", np.round(ref, 6))
print("我的 valid:", is_valid_linkage(mine, warning=False))
assert np.allclose(mine, ref), "应与 scipy 逐元素一致"
print("完全一致：纯 Python Prim 版 single linkage 跑通了")
```

**需要观察的现象 / 预期结果**：

- 你的 `Z` 与 `scipy` 的 `Z` 逐元素相等（断言通过），且通过 `is_valid_linkage` 校验。
- 距离列严格单调不减（MST 边权升序）。
- 这说明 `condensed_index`（读距离）+ Prim 主循环（求 MST）+ `label()`（`LinkageUnionFind` 重编号）三件套协同，已经等价复现了 `mst_single_linkage` 的全部行为。

> 运行结果：本实践依赖 SciPy；若本机未装则标注「待本地验证」。复刻版是 \(O(n^2)\) 的 Python 实现，正确性与 Cython 原版一致，只是速度慢得多。

## 6. 本讲小结

- **single linkage 等价于最小生成树**：合并「最近两簇」⟺ 加入「连接两个连通分量的最短边」，故 single linkage dendrogram 可由一棵 MST 的边权升序合并得到。这是 `mst_single_linkage` 得以成立的几何根基。
- **`mst_single_linkage` 用 Prim 算法**，核心是 `D[i]`（点 \(i\) 到当前树的最近距离）+ `merged[]` 标记。每轮松弛 `D[i]=min(D[i],dist(x,i))` 再 `argmin` 选下一个并入点，总复杂度 \(O(n^2)\)，**不需要堆**。
- **`_single=min` 被内联进松弛步**：本函数从不调用 `linkage_methods` 函数指针表，因为 Prim 的松弛 `min` 已经等价于 single linkage 的距离更新。
- **`Z[k,0]=x` 记的是松弛中心而非真正最近邻**，但可证明「出现偏差时当前边权必不小于上一条边权」，故经排序 + `label()` 的 `find()` 后总能解析到正确代表，最终 dendrogram 完全正确——这就是函数敢省掉教科书 Prim 的 `link[i]` 数组的原因。
- **末尾的「mergesort 排序 + `label()`」是必需收尾**：前者保证距离单调，后者用 `LinkageUnionFind`（u4-l1）修正簇编号并回填第四列大小；这与 `nn_chain` 共享同一套路，而 `fast_linkage` 因合并时即维护好编号与大小而无需此步。
- **三个最小模块的分工**：`mst_single_linkage`（算法主体）依赖 `condensed_index`（地址翻译，u4-l1 §4.1）读距离、依赖 `LinkageUnionFind` 经 `label()`（u4-l1 §4.3）收尾，三者协同完成 single linkage。

## 7. 下一步学习建议

- **u4-l3 nearest-neighbor chain 与 fast_linkage**：看 complete/average/weighted/ward 为何不能走 MST，而要用 `nn_chain`（最近邻链，\(O(n^2)\)）或 `fast_linkage`（带可改值堆的 generic 算法，best case 接近 \(O(n^2)\)）。重点对比它们与本讲的 **复杂度来源**：本讲靠 Prim 全量扫描，那两个靠链/堆避免全量扫描。
- **回看 u3-l4 Lance-Williams 公式**：本讲的松弛 `min` 是 `_single` 的几何化身；读完 u4-l3 后再看 u3-l4，会把「七种方法 → 三种算法」的分派逻辑（可还原性 reducibility）彻底串通。
- **延伸阅读**：`fast_linkage` 的 docstring 引用了 Daniel Mullner 的论文 *"Modern hierarchical, agglomerative clustering algorithms"*（arXiv:1109.2378），其中第 2 节严格论述了「single linkage = MST」以及 Prim、Kruskal、SLINK 等 \(O(n^2)\) 算法的关系，是理解本讲为什么「这么快」的最佳参考。
- **动手验证**：把综合实践的复刻版改用 Kruskal（`scipy.sparse.csgraph.minimum_spanning_tree` 求 MST 再合并）实现一遍，确认它与 Prim 版、与 `scipy` 输出三者一致，从而从两条独立 MST 路径双重印证本讲的等价性。
