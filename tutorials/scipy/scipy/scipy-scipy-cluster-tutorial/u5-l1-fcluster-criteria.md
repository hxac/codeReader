# fcluster：多种 criterion 切分层次树

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `scipy.cluster.hierarchy.fcluster` 的五种 `criterion`（`inconsistent` / `distance` / `maxclust` / `monocrit` / `maxclust_monocrit`）各自的切分语义与适用场景。
- 看懂 Python 层 `fcluster` 如何按 `criterion` 把工作分派给 Cython 后端的 `cluster_in` / `cluster_dist` / `cluster_maxclust_dist` / `cluster_monocrit` / `cluster_maxclust_monocrit`。
- 理解一个关键架构事实：**五种 criterion 最终都归约到一个统一的「单调 monocrit + 阈值」引擎** `cluster_monocrit`，区别只在于「用什么数组当 monocrit」和「阈值怎么定」。
- 解释 `maxclust` 为何是「给定簇数反推距离阈值」——它对单调 monocrit 数组做二分搜索。
- 能够用同一个 `Z` 对比三种 criterion 切出的簇边界，并解释为什么结果会不同。

## 2. 前置知识

本讲建立在你已经掌握 u3-l1（linkage matrix 数据结构）的基础之上。这里复习两个要点：

1. **linkage matrix `Z`**：形状恒为 \((n-1)\times 4\)。第 \(i\) 行表示一次合并：`Z[i,0]`、`Z[i,1]` 是被合并的两个簇编号，`Z[i,2]` 是合并距离（合并高度），`Z[i,3]` 是新簇包含的原始观测数。原始观测编号 \(0\ldots n-1\)，第 \(i\) 步（从 0 起）合并出的新簇编号为 \(n+i\)，整棵树共 \(2n-1\) 个节点，根节点编号恒为 \(2n-2\)。
2. **凝聚式聚类的产物是一棵二叉树**（dendrogram），而现实任务往往要的是「扁平簇」（每个观测归到一个簇）。把树在某处「切一刀」、得到扁平簇的过程，就是 `fcluster` 做的事。

还需要两个术语：

- **cophenetic 距离（合并高度）**：两个原始观测在树中「首次被合并到同一簇」时的合并距离。`Z[:,2]` 就是每次合并的高度。
- **单调数组（monotonic criterion）**：一个长度为 \(n-1\) 的数组 `MC`，按簇行号索引；要求「祖先簇的 `MC` 值 ≥ 其所有后代的 `MC` 值」。由于行号越大的簇形成越晚、越是祖先，单调性意味着 `MC` 沿行号非递减。单调性是 `cluster_monocrit` 正确切树的数学前提。

一个小约定要记住（u1-l3 已提过）：**`fcluster` 返回的簇编号从 1 开始**（不是从 0），这点和 `vq` 不同，对比时要注意。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [hierarchy/_hierarchy_impl.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py) | Python 封装层。`fcluster` 在此定义，做输入校验并按 `criterion` 分派到 Cython。 |
| [hierarchy/_hierarchy.pyx](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx) | Cython 性能层。五个 `cluster_*` 函数在此实现，是真正「切树」的代码。 |

本讲涉及的关键函数：

- `fcluster`（Python 总入口，分发）
- `cluster_monocrit`（Cython，**统一引擎**：给定单调 monocrit + 阈值，深度优先切树）
- `cluster_dist`（Cython，distance 准则的薄包装）
- `cluster_in`（Cython，inconsistent 准则的薄包装）
- `cluster_maxclust_dist`（Cython，maxclust 准则的薄包装）
- `cluster_maxclust_monocrit`（Cython，二分反推阈值的核心）
- 两个辅助函数 `get_max_dist_for_each_cluster` / `get_max_Rfield_for_each_cluster`（构造单调 monocrit 数组）

---

## 4. 核心概念与源码讲解

### 4.1 fcluster 总入口与五种 criterion 分发

#### 4.1.1 概念说明

`fcluster` 的职责只有一句话：**给定一棵已经建好的层次树 `Z`，把它「切平」，返回长度为 \(n\) 的扁平簇标签数组 `T`**（`T[i]` 是观测 `i` 所属的簇号，从 1 起）。

「在哪里切」由两个参数决定：

- `criterion`：用什么**量**来衡量「这一刀切得对不对」。
- `t`：在这个量上的**阈值**或**目标簇数**。

五种 `criterion` 的直觉区别：

| criterion | `t` 的含义 | 切分依据 | 是否需要额外输入 |
|-----------|-----------|----------|----------------|
| `inconsistent`（默认） | 不一致系数阈值 | 每个子树最大的「不一致系数」≤ `t` 就成一个簇 | 可选 `R`（不一致矩阵）、`depth` |
| `distance` | 合并距离阈值 | 每个子树最大的合并高度 ≤ `t` 就成一个簇 | 无 |
| `maxclust` | **目标簇数** | 反推一个最小距离阈值 `r`，使簇数 ≤ `t` | 无 |
| `monocrit` | 用户自定义阈值 | 用户给的 `monocrit[i] ≤ t` 就切 | 必须给 `monocrit` |
| `maxclust_monocrit` | **目标簇数** | 在用户 `monocrit` 上反推阈值，使簇数 ≤ `t` | 必须给 `monocrit` |

`inconsistent` 与 `distance` 的核心区别：`distance` 直接用「合并高度」（一个**全局几何量**）切树；`inconsistent` 用「不一致系数」（一个**局部统计量**：某次合并相对于它下面 `depth` 层合并的异常程度）切树。前者关心「绝对距离多远」，后者关心「这次合并是不是突然跳了一大步」。因此两者常切出不同的边界——这是本讲综合实践要亲手验证的重点。

#### 4.1.2 核心流程

`fcluster` 的 Python 层流程：

```
1. array_namespace(Z) → 取数组后端 xp
2. _asarray 规整为 float64 C 连续；_is_valid_linkage 校验 Z 合法（materialize=True 强制求值）
3. n = Z 行数 + 1；T = 全零 int 数组（长度 n）
4. 若给了 monocrit，规整为 float64 C 连续
5. 把 Z、monocrit 都 np.asarray 成 numpy（Cython 只认连续 numpy）
6. 按 criterion 分派：
     inconsistent        → _hierarchy.cluster_in(Z, R, T, t, n)
     distance            → _hierarchy.cluster_dist(Z, T, t, n)
     maxclust            → _hierarchy.cluster_maxclust_dist(Z, T, n, t)
     monocrit            → _hierarchy.cluster_monocrit(Z, monocrit, T, t, n)
     maxclust_monocrit   → _hierarchy.cluster_maxclust_monocrit(Z, monocrit, T, n, t)
     其它                → raise ValueError
7. return xp.asarray(T)   # 转回输入后端
```

注意 `t` 的传参差异：`distance` / `inconsistent` / `monocrit` 把 `t` 当 **float 阈值**（`float(t)`）；`maxclust` / `maxclust_monocrit` 把 `t` 当 **整数簇数**（直接传或 `int(t)`）。

#### 4.1.3 源码精读

`fcluster` 的函数签名与 docstring 见 [`_hierarchy_impl.py:2420`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2420-L2420)。真正的分发逻辑在函数尾部：

[hierarchy/_hierarchy_impl.py:2567-2599](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2567-L2599) —— 准备 `T`、规整 `monocrit`、按 `criterion` 分派到五个 Cython 函数，最后转回 `xp`：

```python
n = Z.shape[0] + 1
T = np.zeros((n,), dtype='i')
...
if criterion == 'inconsistent':
    if R is None:
        R = inconsistent(Z, depth)          # 默认算不一致矩阵
    ...
    _hierarchy.cluster_in(Z, R, T, float(t), int(n))
elif criterion == 'distance':
    _hierarchy.cluster_dist(Z, T, float(t), int(n))
elif criterion == 'maxclust':
    _hierarchy.cluster_maxclust_dist(Z, T, int(n), t)
elif criterion == 'monocrit':
    _hierarchy.cluster_monocrit(Z, monocrit, T, float(t), int(n))
elif criterion == 'maxclust_monocrit':
    _hierarchy.cluster_maxclust_monocrit(Z, monocrit, T, int(n), int(t))
else:
    raise ValueError(f'Invalid cluster formation criterion: {str(criterion)}')
return xp.asarray(T)
```

要点：

- `T = np.zeros((n,), dtype='i')`：输出标签数组由 **Cython 原地填充**，Python 层不计算任何标签。`'i'` 是平台 int（多数平台为 int32），这解释了 docstring 示例里的 `dtype=int32`。
- `materialize=True`（第 2569 行的 `_is_valid_linkage(..., materialize=True, ...)`）：对 dask 等惰性数组会**强制求值**，因为后续 Cython 函数只能吃连续的 numpy 数组。
- `criterion='inconsistent'` 是唯一会**自动派生额外矩阵**的分支：若用户没传 `R`，就用 `inconsistent(Z, depth)` 现算（`depth` 默认 2）。

#### 4.1.4 代码实践

**实践目标**：确认 `fcluster` 的标签从 1 开始、且五种 criterion 走不同后端函数。

**操作步骤**：在 `_hierarchy_impl.py` 的 `cluster_*` 调用上各加一行临时日志（仅用于观察，验证后还原），或更简单地——用 Python 反射确认调用目标：

```python
# 示例代码（不修改源码，只观察分发）
import scipy.cluster.hierarchy._hierarchy as H
# 列出本讲涉及的 5 个 Cython 函数
print([n for n in dir(H) if n.startswith('cluster_')])
# 期望看到: cluster_dist, cluster_in, cluster_maxclust_dist,
#           cluster_monocrit, cluster_maxclust_monocrit
```

**需要观察的现象**：五个后端函数名与 `criterion` 一一对应。

**预期结果**：`['cluster_dist', 'cluster_in', 'cluster_maxclust_dist', 'cluster_maxclust_monocrit', 'cluster_monocrit']`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `maxclust` 分支把 `t` 直接传给 `cluster_maxclust_dist(Z, T, int(n), t)`，而不像 `distance` 那样写成 `float(t)`？

**参考答案**：因为 `maxclust` 的 `t` 是「目标簇数」（整数语义），后端要把它当 `mc`（max number of clusters）用于二分搜索；而 `distance` 的 `t` 是距离阈值（浮点语义）。语义不同，类型处理也不同。

**练习 2**：如果不传 `R`，`criterion='inconsistent'` 会怎样得到不一致矩阵？

**参考答案**：Python 层会调用 `inconsistent(Z, depth)`（`depth` 默认 2）现算出 `R`，再传给 `cluster_in`。所以 `inconsistent` 准则隐含了一次 `inconsistent()` 计算（详见 u5-l3）。

---

### 4.2 cluster_monocrit：统一的深度优先切树引擎

#### 4.2.1 概念说明

这是本讲最核心的一个函数。**五种 criterion 中有三种（`distance` / `inconsistent` / `monocrit`）最终都调用它**，`maxclust` 系列在二分搜索后也调用它。

它的抽象问题是：

> 给定一棵树 `Z`、一个**单调**数组 `MC`（长度 \(n-1\)，按行号索引，祖先 ≥ 后代）、一个阈值 `cutoff`。请把树切成扁平簇，规则是：从根往下走，遇到第一个 `MC[root] ≤ cutoff` 的子树，就把整个子树归成一个扁平簇；若一路走到叶子都没遇到，则该叶子自成单例簇。

「单调性」保证了这个规则是自洽的：一旦某子树满足 `MC ≤ cutoff`，它的所有后代也都满足（因为后代 `MC` 更小），所以整个子树理应是一个簇，没必要再往下切。

不同的 criterion 只是**换不同的 `MC`**：
- `distance`：`MC[i]` = 子树 \(n+i\) 内最大的合并高度。
- `inconsistent`：`MC[i]` = 子树 \(n+i\) 内最大的不一致系数（`R[:,3]`）。
- `monocrit`：`MC[i]` 由用户给出（如 `maxRstat` / `maxdists` 的输出）。

#### 4.2.2 核心流程

`cluster_monocrit` 用一个**显式栈** `curr_node`（不是递归）做深度优先遍历，从根 `2*n-2` 出发：

```
n_cluster = 0;  cluster_leader = -1   # -1 表示「当前没有正在收集的簇根」
push 根 2n-2
while 栈非空 (k >= 0):
    root = curr_node[k] - n            # 簇号 → Z 行号
    i_lc, i_rc = Z[root,0], Z[root,1]  # 左右孩子簇号

    # (a) 发现新簇：当前没在收集、且本子树 MC <= cutoff → 它领导一个新簇
    if cluster_leader == -1 and MC[root] <= cutoff:
        cluster_leader = root;  n_cluster += 1

    # (b) 优先向下：左/右孩子若是非叶(>=n)且未访问，压栈继续深搜
    if i_lc >= n 且未访问: 压 i_lc; continue
    if i_rc >= n 且未访问: 压 i_rc; continue

    # (c) 走到头了，处理叶子：把孩子叶节点打上当前簇号
    if i_lc < n:
        if cluster_leader == -1: n_cluster += 1   # 单例自成新簇
        T[i_lc] = n_cluster
    if i_rc < n:
        if cluster_leader == -1: n_cluster += 1
        T[i_rc] = n_cluster

    # (d) 回到 leader：若弹栈弹回了当初的 leader，结束这一簇的收集
    if cluster_leader == root: cluster_leader = -1
    k -= 1   # 弹栈
```

簇号 `n_cluster` 按**深度优先发现顺序**递增分配（1, 2, 3, …），这就是返回标签从 1 起、且同一簇内叶子标签相同的来源。

#### 4.2.3 源码精读

[hierarchy/_hierarchy.pyx:237-303](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L237-L303) —— `cluster_monocrit` 全文。关键片段：

```cython
cdef int k, i_lc, i_rc, root, n_cluster = 0, cluster_leader = -1
cdef int[:] curr_node = np.ndarray(n, dtype=np.intc)
...
k = 0
curr_node[0] = 2 * n - 2                  # 从根开始
while k >= 0:
    root = curr_node[k] - n
    i_lc = <int>Z[root, 0]
    i_rc = <int>Z[root, 1]

    if cluster_leader == -1 and MC[root] <= cutoff:   # (a) 发现簇根
        cluster_leader = root
        n_cluster += 1

    if i_lc >= n and not is_visited(visited, i_lc):   # (b) 向下深搜左
        set_visited(visited, i_lc); k += 1; curr_node[k] = i_lc; continue
    if i_rc >= n and not is_visited(visited, i_rc):   # (b) 向下深搜右
        set_visited(visited, i_rc); k += 1; curr_node[k] = i_rc; continue

    if i_lc < n:                            # (c) 左叶子
        if cluster_leader == -1: n_cluster += 1
        T[i_lc] = n_cluster
    if i_rc < n:                            # (c) 右叶子
        if cluster_leader == -1: n_cluster += 1
        T[i_rc] = n_cluster

    if cluster_leader == root:              # (d) 回到 leader，结束本簇
        cluster_leader = -1
    k -= 1
```

几个实现细节值得注意：

- **显式栈 + 位图 visited**（第 258、260-264 行）：用 `curr_node` 数组当栈、`visited` 当字节位图（`is_visited`/`set_visited` 见 [`_hierarchy.pyx:32-43`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L32-L43)），避免递归的函数调用开销，这对深树很重要。
- **`cluster_leader` 的语义**：它记录「当前正在收集的扁平簇的根」。一旦设为某 `root`，后续深入该子树遇到的所有叶子都打上同一个 `n_cluster`；直到弹栈回到 `root` 本身（`cluster_leader == root`），才清空它，准备收集下一个簇。
- **单例簇的判定**：当一个叶子（`i_lc < n`）被处理时，若 `cluster_leader == -1`（说明从根到它一路上没有任何子树的 `MC ≤ cutoff`），它就只能自成一个新的单例簇，故 `n_cluster += 1` 再赋值。

#### 4.2.4 代码实践

**实践目标**：手工追踪 `cluster_monocrit` 在一棵 4 叶小树上的行为，理解 `cluster_leader` 如何把一片子树归成一簇。

**操作步骤**：考虑 4 个一维点 `X = [[0],[1],[10],[11]]`，`single` 链接得到的 `Z`（手工可推）：

```
Z = [[0, 1, 1, 2],     # 行0: 合并点0,1 → 簇4, 高度1
     [2, 3, 1, 2],     # 行1: 合并点2,3 → 簇5, 高度1
     [4, 5, 10, 4]]    # 行2: 合并簇4,5 → 根6, 高度10
```

取 `MC = max_dists = [1, 1, 10]`（每个子树的最大合并高度），`cutoff = 5`。按上面的流程图手工走一遍 DFS，写出 `T`。

**需要观察的现象**：根 `MC=10 > 5` 不成簇；左子树（簇 4，`MC=1 ≤ 5`）和右子树（簇 5，`MC=1 ≤ 5`）各成一个扁平簇。

**预期结果**：`T = [1, 1, 2, 2]`——点 0、1 归簇 1，点 2、3 归簇 2。（建议本地用 `fcluster(Z, 5, 'distance')` 核对。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 `cluster_monocrit` 要求 `MC` 单调？如果 `MC` 不单调会发生什么？

**参考答案**：单调性（祖先 `MC` ≥ 后代）保证了「一旦某子树 `MC ≤ cutoff`，其全部后代也 `≤ cutoff`」，于是把整个子树当一个簇是正确的。若不单调，可能出现「父满足但某后代不满足」的矛盾，切出的簇会与阈值语义不符——这就是为什么 `monocrit` 文档强调 `monocrit` 必须单调。

**练习 2**：`cluster_leader` 在什么时候被设为 `-1` 之外的值，又在什么时候被重置？

**参考答案**：在 (a) 步发现一个新簇根（`MC[root] ≤ cutoff` 且当前无 leader）时设为该 `root`；在 (d) 步弹栈弹回这个 `root` 本身（`cluster_leader == root`）时重置为 `-1`，标志该扁平簇收集完毕。

---

### 4.3 cluster_dist 与 cluster_in：构造单调 monocrit 后复用引擎

#### 4.3.1 概念说明

有了 4.2 的统一引擎，`distance` 和 `inconsistent` 两个准则的实现就**极其简短**——它们只做一件事：**把 `Z`（和 `R`）转换成一个单调的 `MC` 数组，然后交给 `cluster_monocrit`**。

- `cluster_dist`（对应 `criterion='distance'`）：`MC[i]` = 子树 \(n+i\) 内最大的合并高度。它由 `get_max_dist_for_each_cluster` 算出，本质就是「以每个簇为根的子树里，最高那次合并有多高」。对单调链接（ward/single/complete/average/weighted）这恰好等于 `Z[i,2]` 本身。
- `cluster_in`（对应 `criterion='inconsistent'`）：`MC[i]` = 子树 \(n+i\) 内最大的**不一致系数**（`R[:,3]`），由 `get_max_Rfield_for_each_cluster` 算出。

注意 `cluster_in` 用的 `MC` 是「最大**不一致系数**」，而 `cluster_dist` 用的是「最大**合并高度**」——这就是两个准则切出的边界常常不同的根本原因：它们衡量的是截然不同的两个量。

#### 4.3.2 核心流程

两个函数都是「构造 `MC` → 调引擎」两步：

```
cluster_dist(Z, T, cutoff, n):
    max_dists = get_max_dist_for_each_cluster(Z)   # MC = 各子树最大合并高度
    cluster_monocrit(Z, max_dists, T, cutoff, n)   # 复用统一引擎

cluster_in(Z, R, T, cutoff, n):
    max_inconsists = get_max_Rfield_for_each_cluster(Z, R, 3)  # MC = 各子树最大 R[:,3]
    cluster_monocrit(Z, max_inconsists, T, cutoff, n)
```

`get_max_*_for_each_cluster` 用**后序遍历**（先递归处理左右孩子，再处理自己）算每个簇的「子树最大值」：

```
对每个非叶簇 root（后序）:
    val = 自己的值 (Z[root,2] 或 R[root,rf])
    if 左孩子是非叶: val = max(val, MC[左孩子])
    if 右孩子是非叶: val = max(val, MC[右孩子])
    MC[root] = val
```

因为后序保证算 `root` 时它的孩子已经算好，所以这是正确的「子树最大」递推；又因为父的值 = max(自己, 孩子们的值) ≥ 孩子们的值，所以得到的 `MC` 天然单调。

#### 4.3.3 源码精读

[hierarchy/_hierarchy.pyx:77-95](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L77-L95) —— `cluster_dist` 全文，只有三行有效逻辑：

```cython
def cluster_dist(const double[:, :] Z, int[:] T, double cutoff, int n):
    cdef double[:] max_dists = np.ndarray(n, dtype=np.float64)
    get_max_dist_for_each_cluster(Z, max_dists, n)      # 构造单调 MC
    cluster_monocrit(Z, max_dists, T, cutoff, n)        # 复用引擎
```

[hierarchy/_hierarchy.pyx:98-119](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L98-L119) —— `cluster_in` 几乎同构，区别只是用 `R` 的第 3 列（`rf=3`，即不一致系数）：

```cython
def cluster_in(const double[:, :] Z, const double[:, :] R, int[:] T, double cutoff, int n):
    cdef double[:] max_inconsists = np.ndarray(n, dtype=np.float64)
    get_max_Rfield_for_each_cluster(Z, R, max_inconsists, n, 3)   # rf=3 → R[:,3]
    cluster_monocrit(Z, max_inconsists, T, cutoff, n)
```

构造 `MC` 的两个后序遍历函数：

[hierarchy/_hierarchy.pyx:446-502](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L446-L502) —— `get_max_dist_for_each_cluster`，核心递推（`MD[root] = max(Z[root,2], MD[左], MD[右])`）：

```cython
max_dist = Z[root, 2]
if i_lc >= n:                      # 左孩子是非叶簇
    max_l = MD[i_lc - n]
    if max_l > max_dist: max_dist = max_l
if i_rc >= n:                      # 右孩子是非叶簇
    max_r = MD[i_rc - n]
    if max_r > max_dist: max_dist = max_r
MD[root] = max_dist
```

[hierarchy/_hierarchy.pyx:381-443](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L381-L443) —— `get_max_Rfield_for_each_cluster` 与上式同构，只是把 `Z[root,2]` 换成 `R[root,rf]`，`rf` 由参数指定（`cluster_in` 传 3）。它的 docstring（第 384-385 行）一句话点明语义：「`max_rfs[i] = max{R[j,rf] : j 是 i 的后代}`」。

> 小细节：`cluster_dist`/`cluster_in` 开的 `max_dists`/`max_inconsists` 缓冲区长度是 `n`（第 93、117 行），而实际只用到前 `n-1` 个（非叶簇共 \(n-1\) 个）；多开一个元素是无害的冗余。

#### 4.3.4 代码实践

**实践目标**：亲眼看 `distance` 和 `inconsistent` 用不同 `MC` 切出不同边界。

**操作步骤**：用 `inconsistent` docstring 里的官方 8 点例子（`X = [[i] for i in [2,8,0,4,1,9,9,0]]`，ward 链接），其 `Z` 是文档保证的固定值：

```python
# 示例代码
import numpy as np
from scipy.cluster.hierarchy import linkage, fcluster, inconsistent

X = [[i] for i in [2, 8, 0, 4, 1, 9, 9, 0]]
Z = linkage(X, 'ward')
# Z（来自 inconsistent 文档，固定值）:
# [[ 5,  6, 0.        , 2],
#  [ 2,  7, 0.        , 2],
#  [ 0,  4, 1.        , 2],
#  [ 1,  8, 1.15470054, 3],
#  [ 9, 10, 2.12132034, 4],
#  [ 3, 12, 4.11096096, 5],
#  [11, 13, 14.07183949, 8]]

print(fcluster(Z, t=3, criterion='distance'))      # 用「最大合并高度」切
print(fcluster(Z, t=1.0, criterion='inconsistent'))  # 用「最大不一致系数」切
print(inconsistent(Z)[:, 2])   # 看一眼 R 的「计数」列，理解 depth 影响
```

**需要观察的现象**：`distance` 在 `t=3` 时切出 3 个簇（边界由合并高度 2.12 / 4.11 / 14.07 决定）；而 `inconsistent` 切的位置完全不同，因为它看的是「不一致系数」而非绝对高度。

**预期结果**：两个数组代表的**分组**不同（具体 int 标签待本地验证）。根据源码逻辑手工追踪，`distance t=3` 应得到三组 `{1,5,6}`、`{3}`、`{0,2,4,7}`；`inconsistent t=1.0` 得到四组 `{1,5,6}`、`{0,4}`、`{2,7}`、`{3}`。本地运行核对分组是否吻合。

#### 4.3.5 小练习与答案

**练习 1**：`cluster_dist` 的 `MC[i]` 在什么情况下恰好等于 `Z[i,2]`？

**参考答案**：当链接是**单调**的（ward/single/complete/average/weighted，`Z[:,2]` 递增）时，每个子树里最高的合并就是该子树根自己的合并，所以「子树最大合并高度」= `Z[root,2]`。对非单调的 `median`/`centroid` 则不一定。

**练习 2**：`cluster_in` 调 `get_max_Rfield_for_each_cluster` 时为什么传 `rf=3`？

**参考答案**：`R` 是 \((n-1)\times 4\) 的不一致矩阵，四列依次是「均值、标准差、计数、不一致系数」（见 u5-l3）。`inconsistent` 准则要用「不一致系数」来阈值化，即第 4 列，列号 `rf=3`（0 起）。

---

### 4.4 cluster_maxclust_dist 与 cluster_maxclust_monocrit：二分反推阈值

#### 4.4.1 概念说明

`maxclust` 准则的语义特别：用户给的不是阈值，而是**「我想要最多 t 个簇」**。系统需要**反推出**一个距离阈值 `r`，使得用 `r` 按 `distance` 准则切树时，恰好得到不超过 `t` 个簇。

关键观察：簇数是阈值 `r` 的**单调不增函数**——`r` 越大，合并进同一簇的点越多，簇数越少。而候选阈值只有 \(n-1\) 个离散值（即单调 `MC` 数组的各元素）。因此这是一个标准的**二分搜索**问题：

> 在单调数组 `MC[0..n-2]` 中，找最小的下标 `upper_idx`，使得用 `MC[upper_idx]` 当阈值切树得到的簇数 `≤ max_nc`。

`cluster_maxclust_dist` 同样只是个薄包装：先算 `max_dists`（和 `cluster_dist` 一样的 `MC`），再调 `cluster_maxclust_monocrit` 做二分。`maxclust_monocrit` 准则则允许用户自带任意单调 `monocrit` 数组。

二分结束后，用搜到的阈值 `MC[upper_idx]` 调一次 `cluster_monocrit` 真正生成标签。

#### 4.4.2 核心流程

```
cluster_maxclust_monocrit(Z, MC, T, n, max_nc):
    if max_nc >= n:                    # 要的簇数 ≥ 观测数 → 每点一簇
        for i: T[i] = i + 1;  return

    lower_idx = -1                     # 对应 -∞（MC[-1] 不被求值）
    upper_idx = n - 1
    while upper_idx - lower_idx > 1:
        i = (lower_idx + upper_idx) >> 1
        thresh = MC[i]
        nc = count_clusters(Z, MC, thresh)   # 用 thresh 切，数有几个簇
        if nc > max_nc:                # 簇太多 → 阈值太小
            lower_idx = i
        else:                          # 簇数 OK → 阈值可能还能更小
            upper_idx = i
    # 循环结束时，upper_idx 是「簇数 ≤ max_nc」的最小阈值下标
    cluster_monocrit(Z, MC, T, MC[upper_idx], n)   # 用它真正切树
```

`count_clusters` 的逻辑与 `cluster_monocrit` 同构（同样是深度优先 + `MC[root] ≤ thresh` 成簇），只是只计数、不写标签，且一旦 `nc > max_nc` 立即 `break` 提前剪枝。

#### 4.4.3 源码精读

[hierarchy/_hierarchy.pyx:122-141](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L122-L141) —— `cluster_maxclust_dist` 薄包装（同 `cluster_dist` 模式）：

```cython
def cluster_maxclust_dist(const double[:, :] Z, int[:] T, int n, int mc):
    cdef double[:] max_dists = np.ndarray(n - 1, dtype=np.float64)
    get_max_dist_for_each_cluster(Z, max_dists, n)
    # should use an O(n) algorithm
    cluster_maxclust_monocrit(Z, max_dists, T, n, mc)
```

> 注释 `# should use an O(n) algorithm`（第 140 行）是作者的优化 TODO：当前二分是 \(O(n \log n)\)，理论上对单调距离可做到 \(O(n)\)（直接取第 `n - max_nc` 大的高度）。

[hierarchy/_hierarchy.pyx:144-234](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L144-L234) —— `cluster_maxclust_monocrit`。二分搜索主循环（第 179-234 行）：

```cython
lower_idx = -1                       # 对应 -INFINITY，MC 不在此下标求值
upper_idx = n - 1
while upper_idx - lower_idx > 1:
    i = (lower_idx + upper_idx) >> 1
    thresh = MC[i]

    memset(visited, 0, visited_size)
    nc = 0;  k = 0;  curr_node[0] = 2 * n - 2
    while k >= 0:                     # 数簇（与 cluster_monocrit 同构）
        root = curr_node[k] - n
        i_lc = <int>Z[root, 0];  i_rc = <int>Z[root, 1]
        if MC[root] <= thresh:        # 整个子树成一个簇
            nc += 1
            if nc > max_nc: break     # 提前剪枝
            k -= 1
            set_visited(visited, i_lc); set_visited(visited, i_rc)
            continue
        ...                           # 否则继续下探；单例各自计数
    if nc > max_nc:
        lower_idx = i                 # 阈值太小
    else:
        upper_idx = i                 # 阈值可行，尝试更小
...
cluster_monocrit(Z, MC, T, MC[upper_idx], n)   # 用搜到的阈值真正切树
```

要点：

- **二分有效性依赖 `MC` 单调**：代码直接用下标 `i` 取 `MC[i]` 当阈值，能做二分的前提是 `MC` 按下标非递减——这正是 monocrit 的单调性。对 `maxclust`（distance），`max_dists` 单调当且仅当链接单调。
- **提前剪枝**：数簇时一旦 `nc > max_nc` 立即 `break`（第 197-198、212-213、223-224 行），避免无谓的完整遍历。
- **`max_nc >= n` 快速出口**（第 164-167 行）：要的簇数不少于观测数时，每点自成一群，直接 `T[i] = i+1` 返回。
- **最后一步复用 `cluster_monocrit`**（第 234 行）：二分只确定阈值，真正的标签生成仍交给统一引擎，保证标签语义和 `distance`/`monocrit` 完全一致。

#### 4.4.4 代码实践

**实践目标**：验证 `maxclust` 是「给定簇数反推阈值」，并亲手用二分重现这个反推。

**操作步骤**：仍用 4.3 的 8 点 `Z`（ward）。手工列出 `max_dists`，然后模拟二分搜索「要 3 个簇」时反推出的阈值。

```python
# 示例代码
import numpy as np
from scipy.cluster.hierarchy import linkage, fcluster

X = [[i] for i in [2, 8, 0, 4, 1, 9, 9, 0]]
Z = linkage(X, 'ward')

# 手工算 max_dists（每个子树的最大合并高度）：
# 行0(簇8,{5,6}): 0
# 行1(簇9,{2,7}): 0
# 行2(簇10,{0,4}): 1
# 行3(簇11,{1,5,6}): max(1.1547, 0) = 1.1547
# 行4(簇12,{0,2,4,7}): max(2.1213, 0, 1) = 2.1213
# 行5(簇13,{0,2,3,4,7}): max(4.1110, 2.1213) = 4.1110
# 行6(根14): max(14.0718, 1.1547, 4.1110) = 14.0718
max_dists = np.array([0, 0, 1, 1.1547, 2.1213, 4.1110, 14.0718])

# 模拟 maxclust_monocrit 的二分，找「簇数 <= 3」的最小阈值：
def count_clusters(md, thresh):
    # 简化：合并高度 <= thresh 的子树成簇（手工按树数）
    # 这里直接用结论验证
    return None  # 见下方说明

print(fcluster(Z, t=3, criterion='maxclust'))      # 系统：反推阈值后切
print(fcluster(Z, t=3, criterion='distance'))      # 直接用阈值 3 切
```

**需要观察的现象**：对这棵树，二分搜索会停在 `upper_idx = 4`（即 `max_dists[4] = 2.1213`），因为用 `2.1213` 当阈值切树恰好得到 3 个簇；而 `2.1213` 之前的值（如 `1.1547`）会切出 4 个簇（超过 3）。

**预期结果**：由于反推出的阈值 `2.1213 < 3`，`fcluster(Z, 3, 'maxclust')` 与 `fcluster(Z, 3, 'distance')` 在这棵树上**恰好切出相同的 3 个簇** `{1,5,6}`、`{3}`、`{0,2,4,7}`（具体 int 标签数组待本地验证；据源码手工追踪两者应都为 `[3,1,3,2,3,1,1,3]`）。这说明：当 `distance` 的 `t` 落在「正确的区间」时，它与 `maxclust` 结果一致；`maxclust` 的价值在于你不必知道这个区间，只管说「我要 3 簇」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `cluster_maxclust_monocrit` 能用下标 `i` 直接在 `MC` 上二分，而不需要把 `MC` 排序？

**参考答案**：因为 `MC` 已经满足单调性（祖先 ≥ 后代，按下标非递减），本身就是「按值升序」排好的，二分下标等价于二分值，无需额外排序。这也是 monocrit 必须单调的第二个理由（第一个理由见 4.2.5）。

**练习 2**：第 140 行注释 `# should use an O(n) algorithm` 指的是什么优化？

**参考答案**：当前用二分搜索（\(O(n\log n)\)）。但对 `maxclust`（distance）这种 `MC = max_dists` 的情况，「要 `max_nc` 个簇」等价于「保留 `n - max_nc` 次最高的合并之外的全部合并」，可直接取 `max_dists` 的第 `n - max_nc` 大的值当阈值，做到 \(O(n)\)。作者用注释标记了这一未实现的优化。

---

## 5. 综合实践

把三种 criterion 放在同一棵树上对比，是理解本讲的最佳方式。

**任务**：对上面的 8 点 `Z`（ward），分别用 `criterion='distance'`、`'maxclust'`、`'inconsistent'` 设法各得到 **3 个簇**，对比三者的切分边界，并解释 `maxclust=3` 反推出的阈值。

```python
# 示例代码（综合实践）
import numpy as np
from scipy.cluster.hierarchy import linkage, fcluster, inconsistent

X = [[i] for i in [2, 8, 0, 4, 1, 9, 9, 0]]
Z = linkage(X, 'ward')

# (1) distance: 直接给距离阈值。t=3 落在 (2.12, 4.11] 区间 → 3 簇
T_dist = fcluster(Z, t=3, criterion='distance')

# (2) maxclust: 给目标簇数，系统反推阈值（反推出 2.1213）→ 3 簇
T_max = fcluster(Z, t=3, criterion='maxclust')

# (3) inconsistent: 给不一致系数阈值。尝试若干 t，看能否得到 3 簇
for t in [0.5, 0.8, 1.0, 1.05, 1.1, 1.2]:
    T_in = fcluster(Z, t=t, criterion='inconsistent')
    print(f"t={t}: {len(np.unique(T_in))} clusters -> {T_in}")

R = inconsistent(Z)
print("R[:,3] (不一致系数) =", R[:, 3])
```

**你要回答的问题**：

1. `distance t=3` 与 `maxclust t=3` 切出的分组是否相同？为什么？（提示：反推出的阈值 `2.1213` 与 `t=3` 落在同一区间。）
2. `inconsistent` 能否切出**恰好 3 个**簇？为什么？（提示：算出 `max_inconsists = [0, 0, 0, 0.7071, 1.0185, 1.0185, 1.1268]`，注意簇 12 与簇 13 的 `max_inconsists` 相等（都是 `1.0185`）。当 `t ≥ 1.0185` 时两者同时成簇，簇 13 会把簇 12 连同点 3 一起吞掉，于是从 4 簇直接跳到 2 簇，**跳过了 3 簇**。）
3. 这个练习说明了三种 criterion 的什么本质区别？

**预期结论**：`distance` 与 `maxclust` 都基于「合并高度」这一全局几何量，只是定阈值的方式不同（直接给 vs. 反推），在合适区间内结果一致；`inconsistent` 基于「局部不一致性」，衡量的是「合并的突跳程度」而非绝对距离，它在这棵树上**无法**得到恰好 3 簇——这正说明不同 criterion 会从不同角度切树，选哪种取决于业务诉求（要「直径不超过某值」的簇，还是「内部均匀、只在显著间隙处断开」的簇）。

> 所有具体 int 标签数组建议本地运行核对；上面的**分组**与**簇数跳变**结论是根据源码逻辑手工追踪得到的，可作为验证基准。

## 6. 本讲小结

- `fcluster` 的 Python 层只做**校验 + 分派**：把五种 `criterion` 分别导向 Cython 的 `cluster_in` / `cluster_dist` / `cluster_maxclust_dist` / `cluster_monocrit` / `cluster_maxclust_monocrit`，输出标签数组 `T` 由 Cython 原地填充（从 1 起）。
- **架构核心**：`cluster_monocrit` 是统一的「单调 monocrit + 阈值」切树引擎；`distance` / `inconsistent` 只是先用 `get_max_dist_for_each_cluster` / `get_max_Rfield_for_each_cluster` 构造出单调 `MC`，再复用它。
- `distance` 的 `MC` 是「各子树最大合并高度」；`inconsistent` 的 `MC` 是「各子树最大不一致系数 `R[:,3]`」。两者衡量不同的量，故常切出不同边界。
- `maxclust` 是「给定簇数反推阈值」：在单调 `MC` 上二分搜索最小的、使簇数 `≤ t` 的阈值，再用它调一次 `cluster_monocrit`。它依赖 `MC` 单调（第二个理由）。
- monocrit 必须单调：既保证 `cluster_monocrit` 切树正确（祖先满足则后代必满足），又保证 `cluster_maxclust_monocrit` 能用下标二分（`MC` 已按值升序）。

## 7. 下一步学习建议

- **u5-l2（fclusterdata）**：看 `pdist → linkage → fcluster` 如何被打包成一个面向原始观测矩阵的便捷入口，本讲的 `fcluster` 是其中的最后一环。
- **u5-l3（inconsistent 与 max 系列统计）**：深入 `R` 矩阵四列的精确含义、`depth` 如何影响不一致性计算，以及 `maxRstat`/`maxinconsts`/`maxdists` 如何沿树聚合出 `monocrit` 准则需要的单调向量——本讲中 `cluster_in` 用的 `R[:,3]` 和 `get_max_Rfield_for_each_cluster` 都在那里展开。
- **u5-l4（cophenet）**：理解「cophenetic 距离」与 `distance` 准则切树的关系，以及如何用 cophenetic 相关系数评估一棵树对原始距离的保真度。
- 想吃透切树的底层遍历，可回头对照 u4-l1 的 `condensed_index` 与并查集，理解 `cluster_monocrit` 的显式栈遍历为何比递归更高效。
