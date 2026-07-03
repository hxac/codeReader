# 讲义 u6-l3：optimal_leaf_ordering 最优叶序

## 1. 本讲目标

本讲承接 u6-l1（`ClusterNode`、`to_tree`、`cut_tree`、`leaves_list`）。在那里我们已经知道：一棵由 `linkage` 产生的层次树，其叶子从左到右的顺序由 `leaves_list(Z)` 给出，而这个顺序本质上是**建树算法在每一步「先左孩子后右孩子」的任意选择**累积出来的——并不携带任何「相邻叶子应当尽量靠近」的语义。

学完本讲后，你应该能够：

1. 说清楚「最优叶序问题（optimal leaf ordering）」到底在优化什么：**固定树结构不动，只交换每个内部节点的左右孩子**，使所有相邻叶子的两两距离之和最小。
2. 掌握 `optimal_leaf_ordering(Z, y)` 这个公开函数的输入输出、它的代价（计算较慢），以及它如何与 `linkage` 的 `optimal_ordering=True` 参数对接。
3. 看懂 Cython 后端 `_optimal_leaf_ordering.pyx` 中的动态规划算法：`identify_swaps` 如何用 \(U[u,w]=U[u,m]+D[m,k]+U[k,w]\) 的递推式逐层算出最优顺序，并把结果编码成一串「是否翻转某节点」的位标记。

## 2. 前置知识

阅读本讲前，请确保你已经理解：

- **linkage matrix Z**（见 u3-l1）：形状 (n−1)×4，前两列是被合并的两个簇编号，第三列是合并距离，第四列是新簇的原始观测数。原始观测编号 0…n−1，第 i 步产生的新簇编号为 n+i。
- **leaves_list(Z)**（见 u6-l1）：返回「从左到右」的叶子序，即 dendrogram 自左向右排列的原始观测编号序列。
- **「交换孩子不改变聚类」**：把任意一个内部节点的左、右孩子对调，**合并关系和距离完全不变**，得到的是一个等价的 linkage matrix（聚类结果完全相同），只是叶子的左右顺序变了。这正是最优叶序得以成立的前提——它只重排顺序，绝不改变聚类结构。
- **两两距离矩阵**：`pdist` 输出的一维压缩距离矩阵（长度 \(n(n-1)/2\)），或其方阵形式 `squareform`。本讲的「相邻叶子距离」用的就是这些距离。

一个直觉性的比喻：层次聚类像把一堆点两两合并成一棵树，树形（谁和谁合并、何时合并）已经固定。但同一棵二叉树，你可以把任意一个分叉的「左边」和「右边」整体对调，叶子排列顺序就变了。最优叶序就是要在所有 \(2^{\text{内部节点数}}\) 种合法排列里，挑出一种让「相邻两个叶子挨得最近」的排列——这样画出来的 dendrogram 视觉上色块更连续、更易读。

## 3. 本讲源码地图

本讲涉及两个关键源码文件：

| 文件 | 作用 |
|------|------|
| [hierarchy/_hierarchy_impl.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py) | 纯 Python 封装层。包含公开入口 `optimal_leaf_ordering`（含 docstring、输入校验、惰性数组桥接）与 `linkage`（含 `optimal_ordering` 参数的处理）。 |
| [hierarchy/_optimal_leaf_ordering.pyx](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_optimal_leaf_ordering.pyx) | Cython 性能层。实现 Bar-Joseph 等人 2001 年论文《Fast Optimal Leaf Ordering》的动态规划算法，核心是 `identify_swaps` 函数。 |

调用链总览（自顶向下）：

```
linkage(..., optimal_ordering=True)        # _hierarchy_impl.py
        │  (建树后)
        ▼
optimal_leaf_ordering(Z, y)                # _hierarchy_impl.py，公开入口
        │  (校验 + 桥接)
        ▼
xpx.lazy_apply(cy_optimal_leaf_ordering)   # 惰性数组桥接
        │
        ▼
_optimal_leaf_ordering.optimal_leaf_ordering(Z, D)   # .pyx，预处理 + 调度
        │
        ▼
identify_swaps(sorted_Z, sorted_D, cluster_ranges)   # .pyx，动态规划核心
```

## 4. 核心概念与源码讲解

### 4.1 最优叶序问题与 `optimal_leaf_ordering` 公开入口

#### 4.1.1 概念说明

给定一棵已经建好的层次树（linkage matrix `Z`）和叶子之间的距离 `y`，**最优叶序问题**是：在所有「只交换内部节点左右孩子」得到的合法叶子排列中，找出使下面这个目标函数最小的那个排列：

\[
\mathrm{cost}(\pi) \;=\; \sum_{i=1}^{n-1} D[\pi_i,\, \pi_{i+1}]
\]

其中 \(\pi=(\pi_1,\dots,\pi_n)\) 是叶子排列，\(D[\pi_i,\pi_{i+1}]\) 是排列中第 \(i\) 与第 \(i+1\) 个叶子的距离。

要点有三：

1. **树结构不变**：哪两个簇在哪个距离合并，一字不改。所以最优叶序得到的 Z 与原 Z 描述**完全相同的聚类**，只是「左/右孩子」次序不同。
2. **目标只看相邻叶子**：它不关心不相邻叶子的距离，只最小化相邻叶子距离之和。这让 dendrogram 看起来「色块连续」。
3. **穷举不可行**：一棵 n 叶子树有 n−1 个内部节点，理论上可交换 \(2^{n-1}\) 种，必须用动态规划高效求解（见 4.3）。

公开入口 `optimal_leaf_ordering(Z, y, metric='euclidean')` 就是对这个问题的封装：吃一个 Z 和一个距离（压缩距离矩阵或观测矩阵），吐出一个**重排后的新 Z**（原始 Z 不被修改）。

#### 4.1.2 核心流程

`optimal_leaf_ordering` 的 Python 层只做三件事：

1. **输入规整与校验**：把 `Z` 转 C 连续数组；把 `y` 规整成压缩距离矩阵（若 `y` 是二维观测矩阵则用 `pdist` 算距离；若长得像未压缩的方阵距离矩阵则发 `ClusterWarning`）。
2. **桥接到 Cython**：通过内嵌闭包 `cy_optimal_leaf_ordering` + `xpx.lazy_apply` 把活儿交给编译后端，同时兼容 dask 惰性数组（这一桥接套路与 `linkage`、`cophenet` 完全同构，见 u3-l3）。
3. **返回重排后的 Z**：形状、距离、簇大小全都不变，只是部分行的前两列（左右孩子）被对调了。

伪代码：

```text
function optimal_leaf_ordering(Z, y):
    校验 Z 是合法 linkage matrix
    若 y 是二维观测矩阵:  y = pdist(y, metric)
    校验 y 是有限值的压缩距离矩阵
    return lazy_apply(把 Z, y 喂给 Cython 的 _optimal_leaf_ordering.optimal_leaf_ordering)
```

#### 4.1.3 源码精读

公开入口定义与 docstring（含一个 ward 示例，叶子序从 `[0 3 1 9 2 5 7 4 6 8]` 变为 `[3 0 2 5 7 4 8 6 9 1]`）：

[optimal_leaf_ordering(Z, y, metric='euclidean') 的定义与 docstring 示例:_hierarchy_impl.py:1403-1439](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L1403-L1439)
> 这段是函数签名、参数说明、返回值说明，以及一个对比「重排前后叶子序」的可运行示例。注意 docstring 明确写着返回值是「A copy of the linkage matrix Z, reordered to minimize the distance between adjacent leaves」——重排、非原地、目标是最小化相邻叶子距离。

输入规整与惰性桥接：

[optimal_leaf_ordering 的输入校验与 cy_optimal_leaf_ordering 闭包:_hierarchy_impl.py:1442-1476](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L1442-L1476)
> 关键点：`y.ndim == 1` 时当作压缩距离矩阵直接用 `distance.is_valid_y` 校验；`y.ndim == 2` 时**默认当观测矩阵**走 `pdist`，只有当它「对角线全 0、非负、对称」时才警告「像未压缩距离矩阵」（`ClusterWarning`）。内嵌 `cy_optimal_leaf_ordering(Z, y, validate)` 闭包在 `validate=True` 时再校验一遍 finite，最终落到 `_optimal_leaf_ordering.optimal_leaf_ordering(Z, y)`。`xpx.lazy_apply(..., as_numpy=True)` 表示结果强制转回 numpy 数组。

#### 4.1.4 代码实践

**实践目标**：亲手验证「最优叶序确实让相邻叶子距离之和更小」，并确认聚类结构未被改变。

**操作步骤**：

```python
import numpy as np
from scipy.cluster import hierarchy
from scipy.spatial.distance import pdist

rng = np.random.default_rng(42)
X = rng.standard_normal((10, 2))          # 10 个二维点
Z = hierarchy.linkage(X, method='ward')   # 建树，默认 optimal_ordering=False

Z_opt = hierarchy.optimal_leaf_ordering(Z, X)   # 重排

leaves = hierarchy.leaves_list(Z)
leaves_opt = hierarchy.leaves_list(Z_opt)

D = pdist(X)  # 压缩距离矩阵；下面用方阵形式查相邻距离
from scipy.spatial.distance import squareform
DM = squareform(D)

def adj_sum(order):
    return sum(DM[order[i], order[i+1]] for i in range(len(order)-1))

print("原叶序   :", leaves.tolist(),   "相邻距离和 =", round(adj_sum(leaves), 4))
print("最优叶序 :", leaves_opt.tolist(), "相邻距离和 =", round(adj_sum(leaves_opt), 4))
```

**需要观察的现象**：

1. 两种叶子序不同（叶序发生了重排）。
2. `adj_sum(leaves_opt)` **小于或等于** `adj_sum(leaves)`。
3. 对比 Z 与 Z_opt：每一行的**第三列（距离）和第四列（簇大小）完全相同**，只有部分行的前两列左右孩子被对调——聚类结构未变。可用 `np.allclose(Z[:, 2], Z_opt[:, 2])` 与 `np.allclose(Z[:, 3], Z_opt[:, 3])` 验证。

**预期结果**：最优叶序的相邻距离和更小；Z 与 Z_opt 的第 3、4 列逐位相等。若运行环境未安装 scipy，则「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`optimal_leaf_ordering(Z, y)` 中，如果 `y` 传入的是二维观测矩阵而非压缩距离矩阵，函数内部会发生什么？

> **答案**：Python 层检测到 `y.ndim == 2`，会先判断它是否「长得像未压缩的方阵距离矩阵」（对角线全 0、非负、对称），若是则发 `ClusterWarning`；否则当作观测矩阵，调用 `distance.pdist(y, metric)` 算出压缩距离矩阵再继续。

**练习 2**：为什么说最优叶序「不改变聚类」？请从「交换左右孩子」的角度解释。

> **答案**：交换一个内部节点的左右孩子，只改变后续 `leaves_list` 从左到右的顺序，**不改变「哪两个簇在第几步、以多大距离合并」**这一聚类事实。所以 Z 的距离列、簇大小列、合并关系完全一致，`fcluster` 在任何阈值下都会给出相同的扁平簇划分。

### 4.2 `linkage` 的 `optimal_ordering` 参数：建树后自动重排

#### 4.2.1 概念说明

除了单独调用 `optimal_leaf_ordering`，`linkage` 本身也提供了一个 `optimal_ordering` 布尔参数（自 SciPy 1.0.0 起加入）。它的工作方式非常简单：**先按正常流程建树，如果 `optimal_ordering=True`，就在返回前对结果调用一次 `optimal_leaf_ordering`**。

它的 docstring 有一句重要提醒：默认是 `False`，**因为这个算法在大数据集上可能很慢**（见 [4.2.3](#423-源码精读)）。所以它是一个「可选的、最后一步的美化」，而非建树算法本身的组成部分。

#### 4.2.2 核心流程

```text
function linkage(y, method, metric, optimal_ordering):
    ...（校验、规整、按 method 分派到 mst_single_linkage / nn_chain / fast_linkage）...
    result = <Cython 后端算出的 Z>
    if optimal_ordering:
        return optimal_leaf_ordering(result, y)   # 末尾加一步重排
    else:
        return result
```

也就是说，`linkage(y, optimal_ordering=True)` 等价于：

```python
Z = linkage(y)              # 普通建树
Z = optimal_leaf_ordering(Z, y)   # 再重排
```

注意这里把原始输入 `y` 直接透传给了 `optimal_leaf_ordering`，所以无论 `y` 一开始是压缩距离矩阵还是观测矩阵，都能被正确处理（`optimal_leaf_ordering` 内部会再判一次维度）。

#### 4.2.3 源码精读

`linkage` 的 `optimal_ordering` 参数说明（含「为何默认 False」的理由）：

[linkage 的 optimal_ordering 参数 docstring:_hierarchy_impl.py:865-872](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L865-L872)
> docstring 原文：开启后「the linkage matrix will be reordered so that the distance between successive leaves is minimal」，并明确指出「defaults to False, because this algorithm can be slow, particularly on large datasets」。这正是 4.1 描述的最优叶序问题。

`linkage` 末尾对 `optimal_ordering` 的处理（建树分派之后）：

[linkage 末尾：optimal_ordering 为真时调用 optimal_leaf_ordering:_hierarchy_impl.py:967-974](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L967-L974)
> `result = xpx.lazy_apply(cy_linkage, y, ...)` 先拿到正常的 Z；紧接着 `if optimal_ordering: return optimal_leaf_ordering(result, y)` 把原始 `y` 透传给重排函数。注意这一步在惰性桥接**之外**——`optimal_leaf_ordering` 内部有自己的 `lazy_apply`，所以对 dask 输入仍能正确工作。

#### 4.2.4 代码实践

**实践目标**：验证 `linkage(optimal_ordering=True)` 与「`linkage` + `optimal_leaf_ordering`」两步法结果一致。

**操作步骤**：

```python
import numpy as np
from scipy.cluster import hierarchy

X = np.random.default_rng(0).standard_normal((12, 3))

# 方式 A：一步到位
Z_one_step = hierarchy.linkage(X, method='average', optimal_ordering=True)

# 方式 B：两步
Z_built = hierarchy.linkage(X, method='average')
Z_two_step = hierarchy.optimal_leaf_ordering(Z_built, X)

print("叶序一致 :", np.array_equal(
    hierarchy.leaves_list(Z_one_step), hierarchy.leaves_list(Z_two_step)))
print("Z 全等   :", np.allclose(Z_one_step, Z_two_step))
```

**需要观察的现象**：两种方式得到的 `leaves_list` 完全相同，Z 矩阵逐元素相等（可能存在极小浮点差异，故用 `allclose`）。

**预期结果**：两个布尔值都为 `True`。这印证了 `optimal_ordering=True` 仅是「建树 + 重排」的语法糖。「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果数据集有 50000 个点，你会用 `linkage(optimal_ordering=True)` 吗？为什么？

> **答案**：通常不会。docstring 明确警告该算法在大数据集上「can be slow」。最优叶序的动态规划在最坏情况下时间复杂度较高（见 4.3），点数上万时会非常耗时。大场景下应保持 `optimal_ordering=False`，仅在需要可视化的小数据集上开启。

**练习 2**：`linkage` 把 `optimal_ordering` 之外，还把什么参数透传给了 `optimal_leaf_ordering`？

> **答案**：只透传了原始输入 `y`（即调用者传给 `linkage` 的第一个参数）。`method`、`metric` 都不再传——因为 `optimal_leaf_ordering` 不关心用什么方法建的树，它只关心「树结构 Z」和「叶子距离 y」。

### 4.3 Cython 后端 `_optimal_leaf_ordering`：动态规划核心

#### 4.3.1 概念说明

真正干活的 Cython 后端位于 `_optimal_leaf_ordering.pyx`，实现的是 Ziv Bar-Joseph、David K. Gifford、Tommi S. Jaakkola 在 2001 年《Bioinformatics》发表的论文 _Fast Optimal Leaf Ordering for Hierarchical Clacking_（DOI: 10.1093/bioinformatics/17.suppl_1.S22）的算法。

核心思想是**动态规划（DP）+ 自底向上合并**。对每个内部节点 \(V\)（其左右孩子为 \(V_l, V_r\)），设其叶子排列的最左叶为 \(u\)、最右叶为 \(w\)，定义：

\[
U_V[u, w] \;=\; \text{子树 } V \text{ 在「最左叶 } u \text{、最右叶 } w \text{」约束下的最优相邻距离和}
\]

由于 \(V = V_l \cup V_r\)，最优排列一定形如「\(V_l\) 的某个排列 + \(V_r\) 的某个排列」，两段在中间以一对叶子 \((m, k)\) 相接（\(m\) 是 \(V_l\) 的最右叶，\(k\) 是 \(V_r\) 的最左叶）。于是有递推：

\[
U_V[u, w] \;=\; \min_{m \in V_l,\; k \in V_r}\Big(\, U_{V_l}[u, m] \;+\; D[m, k] \;+\; U_{V_r}[k, w] \,\Big)
\]

且还需在 \(V_l\)、\(V_r\) 各自「是否交换左右孩子」的 4 种朝向里取最小（代码里用 `swap_L`、`swap_R` 两层循环枚举）。当 \(V_l\) 是单叶时 \(u=m\)，\(U_{V_l}\) 退化为 0。

为了高效，代码做了两个关键优化：

1. **「簇 = 连续区间」编码**：先把 Z、D 按当前叶子序重新编号，使**每个簇在排序后的叶子序上占据一个连续区间** \([\text{start}, \text{end})\)。这样 `u ∈ V_l` 就等价于 `u ∈ [u_min, u_max)`，可用整数范围循环，且 `cluster_ranges[cluster]` 直接给出该簇的叶子区间——省去显式存叶子集合。
2. **排序 + 提前剪枝**：对每个固定的 \((u, w)\)，把候选 \(m\) 按 \(M[u,m]\) 升序排列、候选 \(k\) 按 \(M[w,k]\) 升序排列，一旦 `M[u,m] + M[w,k] + min_km_dist >= cur_min_M` 就 `break` 提前退出内外层循环（因为再往后只会更大）。这是论文里把朴素复杂度降下来的核心技巧。

DP 算完后，得到的是每个节点「取最优时是否需要交换左右孩子」的位标记 `must_swap`；再沿树自顶向下把祖先的交换需求「传递」给后代（翻转一个节点等价于翻转它和它的所有后代），对 2 取模得到每个节点最终是否翻转，最后据此生成新的 `swapped_Z`。

#### 4.3.2 核心流程

整个 `.pyx` 文件分两个函数，流程如下：

```text
function optimal_leaf_ordering(Z, D):           # .pyx 入口（预处理 + 调度）
    校验 Z 合法；把 D 转成方阵 sorted_D
    sorted_leaves = leaves_list(Z)              # 当前叶子序
    把 Z 的叶子编号重映射到 sorted 序 -> sorted_Z（去掉高度列，纯整数）
    把 sorted_D 按 sorted_leaves 重排行列
    算出每个簇在 [0, n) 上的连续区间 cluster_ranges
    must_swap = identify_swaps(sorted_Z, sorted_D, cluster_ranges)   # DP 核心
    用 is_descendant 矩阵把 must_swap 沿树传递，对 2 取模 -> final_swap
    按 final_swap 生成 swapped_Z（翻转需要翻转的节点的左右孩子）
    return swapped_Z

function identify_swaps(sorted_Z, sorted_D, cluster_ranges):   # DP 核心
    for 每个内部节点 i（自底向上，因 sorted_Z 按合并顺序）:
        v_l, v_r = 该节点的左右孩子
        for swap_L in {不翻 V_l, 翻 V_l}:
            for swap_R in {不翻 V_r, 翻 V_r}:
                确定 u, m 的区间（来自 V_l）和 k, w 的区间（来自 V_r）
                for u in V_l 区间:
                    把 M[:, u] 候选 m 排序
                    for w in V_r 区间:
                        把 M[:, w] 候选 k 排序
                        带提前剪枝地搜索最优 (m, k)：
                        M[u,w] = min(m,k) { M[u,m] + M[w,k] + D[m,k] }
                        记录 swap_status[u,w] = (swap_L, swap_R)
        在 V 的所有 (u, w) 里取最小 M，回写 must_swap[v_l], must_swap[v_r]
    return must_swap
```

#### 4.3.3 源码精读

**（a）`.pyx` 入口与预处理。** 校验 Z 后，把距离转方阵、按叶子序重排、构造 `cluster_ranges`：

[optimal_leaf_ordering 入口：校验、转方阵、取当前叶子序:_optimal_leaf_ordering.pyx:376-411](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_optimal_leaf_ordering.pyx#L376-L411)
> docstring 解释了「为什么要先按叶子序重排」：排好序后「每个簇（包括单叶）都能用它落在 \((0\ldots n)\) 上的区间来定义」，这是后续所有高效循环的基础。`is_valid_y`/`is_valid_dm` 分别处理压缩 / 方阵两种距离输入。

把 Z 重写成「排序后编号、去掉高度列」的纯整数 `sorted_Z`，并按叶子序重排距离矩阵：

[重写 sorted_Z 与按叶子序重排 sorted_D:_optimal_leaf_ordering.pyx:414-429](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_optimal_leaf_ordering.pyx#L414-L429)
> 注意它「Remove the 'height' column so we can cast the whole thing as integer」——去掉高度列是为了把 Z 整体当 int32 传给 C 函数，简化接口。距离矩阵 `sorted_D` 用 `sorted_D[sorted_leaves, :][:, sorted_leaves]` 做行列重排。

为每个簇（含 n 个单叶 + n−1 个内部节点 = 2n−1 个）计算连续区间 `cluster_ranges`：

[计算每个簇的连续区间 cluster_ranges:_optimal_leaf_ordering.pyx:431-442](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_optimal_leaf_ordering.pyx#L431-L442)
> 单叶区间为 `[i, i+1)`；内部节点 v 的区间是「左孩子的起点」到「右孩子的终点」。建好后立即调用 `identify_swaps(...)` 进入 DP。

**（b）DP 核心 `identify_swaps`。** 函数头与算法出处：

[identify_swaps 函数头与论文出处:_optimal_leaf_ordering.pyx:142-156](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_optimal_leaf_ordering.pyx#L142-L156)
> 注释明确标注实现的是 Bar-Joseph 等人 2001 年的论文。核心数据结构：`M[n,n]`（float32，存每对 (u,w) 的最优相邻距离和）、`swap_status[n,n,2]`（存取最优时左右子树各需否翻转）、`must_swap[n-1]`（每个内部节点最终需否翻转）。

递推式的文字说明与树形示意（u/m 是 V_l 的边界叶，k/w 是 V_r 的边界叶）：

[递推式 U[u,w]=U[u,m]+U[k,w]+D[m,k] 的注释与树形图:_optimal_leaf_ordering.pyx:199-216](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_optimal_leaf_ordering.pyx#L199-L216)
> 这段注释把整个 DP 思路讲得最清楚：对节点 V，找最左叶 u 和最右叶 w 使 U[u,w]（线性排列中所有相邻单叶距离之和）最小；递推地，通过优化 V_l 的边界 (u,m) 与 V_r 的边界 (k,w) 使 `U[u,w] = U[u,m] + U[k,w] + D[m,k]` 最小；并指出若 u 来自 V_l 的左孩子则 m 来自右孩子（反之亦然），故需搜索 4 种 (V_li, V_rj) 组合——这就是下面 `swap_L`/`swap_R` 双重循环的由来。

4 种朝向的枚举（对应「交换 V_l / 交换 V_r」的 2×2 组合）：

[swap_L/swap_R 双重循环枚举 4 种朝向:_optimal_leaf_ordering.pyx:268-285](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_optimal_leaf_ordering.pyx#L268-L285)
> `swap_L` 决定 u/m 各自来自 V_l 的哪个孩子，`swap_R` 决定 k/w 各自来自 V_r 的哪个孩子；通过 `cluster_ranges` 把「来自某孩子」翻译成具体的叶子区间 `[u_min,u_max)` 等。

递推式的真正计算行（含提前剪枝）：

[递推计算 M[u,w]=min{M[u,m]+M[w,k]+D[m,k]} 与提前剪枝:_optimal_leaf_ordering.pyx:318-348](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_optimal_leaf_ordering.pyx#L318-L348)
> 第 333 行 `current_M = M[u, m] + M[w, k] + sorted_D[m, k]` 就是递推式本体。第 321–322、329–331 行的 `break` 是论文的剪枝优化：候选 m、k 已分别按 `M[u,m]`、`M[w,k]` 升序排好（见 `_sort_M_slice`），一旦 `M[u,m] + M[w,k] + min_km_dist >= cur_min_M` 就不可能再改善，提前退出。算完后第 340–341 行把最优值写入 `M[u,w]` 与对称位 `M[w,u]`；第 345–348 行把当前朝向 `(swap_L, swap_R)` 记进 `swap_status`，省去后续回溯 (m,k)。

节点级最优回写：在该节点所有 (u,w) 里取最小，并把对应的朝向写进 `must_swap`：

[节点级取最优并记录 must_swap:_optimal_leaf_ordering.pyx:357-373](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_optimal_leaf_ordering.pyx#L357-L373)
> 对节点 V 在其左右孩子的叶子区间上扫一遍 `M[u,w]` 找最小，得到 `best_u, best_w`；若 V_l/V_r 非单叶，就把 `swap_status[best_u, best_w, 0/1]` 写入 `must_swap`——即「为了让 V 达到最优，它的左右孩子各自需不需要翻转」。

**（c）从 `must_swap` 到最终 `swapped_Z`。** 翻转一个节点等价于翻转它和它所有后代，故需沿树传递并对 2 取模：

[is_descendant 矩阵与 final_swap = (传递后的翻转计数) mod 2:_optimal_leaf_ordering.pyx:444-467](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_optimal_leaf_ordering.pyx#L444-L467)
> 注释解释得很清楚：「rotate around the axis of a node」需要同时翻转它的所有后代。`is_descendant[i,j]=1` 表示 j 是 i 的后代（含自身）；`applied_swap` 把每个节点自身的翻转需求广播给它所有后代；`final_swap = applied_swap.sum(axis=0) % 2` 表示「节点被翻转的总次数（自身 + 每个需要翻转的祖先）对 2 取模」——翻转两次等于没翻。

按 `final_swap` 生成新的 linkage matrix（只对调需要翻转的节点的左右孩子）：

[按 final_swap 生成 swapped_Z:_optimal_leaf_ordering.pyx:469-481](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_optimal_leaf_ordering.pyx#L469-L481)
> 遍历原 Z 的每一行，若 `final_swap[i]` 为真就把该行的左右孩子 `in_l/in_r` 对调成 `out_l=in_r, out_r=in_l`，否则保持原序；高度 `h` 和簇大小 `v_size` 原样保留。这就证明了重排后聚类结构不变——只有前两列的部分行被对调。

#### 4.3.4 代码实践

**实践目标**：用极小的手工数据追踪一次最优叶序，直观理解「交换左右孩子 → 相邻距离和下降」。

**操作步骤**：

```python
import numpy as np
from scipy.cluster import hierarchy
from scipy.spatial.distance import pdist, squareform

# 4 个一维点：0 和 1 很近，2 和 3 很近，但 {0,1} 与 {2,3} 很远
X = np.array([[0.0], [1.0], [10.0], [11.0]])
Z = hierarchy.linkage(X, method='single')

print("原 Z:\n", Z)
print("原叶序   :", hierarchy.leaves_list(Z).tolist())

Z_opt = hierarchy.optimal_leaf_ordering(Z, X)
print("重排 Z:\n", Z_opt)
print("最优叶序 :", hierarchy.leaves_list(Z_opt).tolist())

# 找出哪些行的左右孩子被对调了
swapped_rows = [i for i in range(Z.shape[0])
                if not np.array_equal(Z[i, :2], Z_opt[i, :2])]
print("被对调的行号:", swapped_rows)
```

**需要观察的现象**：

1. `Z` 与 `Z_opt` 的第 3 列（距离）、第 4 列（簇大小）完全一致；只有合并两个大簇的那一行的前两列可能被对调。
2. 重排前若叶序是 `[0,1,2,3]` 或 `[1,0,2,3]` 之类，相邻距离和里会出现「1→10」这种跨群大跳；重排后叶序让两个相近的点排在一起，相邻距离和显著变小。
3. `swapped_rows` 指出 DP 决定翻转的具体内部节点。

**预期结果**：重排后相邻叶子距离和 ≤ 重排前；被对调的只是树根附近连接两群的节点行。若 scipy 未安装则「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `.pyx` 入口要先把 Z、D 按当前叶子序重排，并算出每个簇的「连续区间」`cluster_ranges`？

> **答案**：重排后每个簇（包括单叶）在叶子序上占据一个连续区间 \([\text{start}, \text{end})\)，于是「u 属于 V_l」就能用整数范围循环 `for u in range(u_min, u_max)` 表达，`cluster_ranges[cluster]` 直接给出区间端点。这避免了显式存每个簇的叶子集合，让 DP 的四重循环可以用最紧凑的下标访问 M 和 D，极大提升效率。

**练习 2**：递推式 `M[u,w] = min(m,k){ M[u,m] + M[w,k] + D[m,k] }` 中，朴素实现要对每个 (u,w) 遍历所有 (m,k)，复杂度很高。代码用了什么技巧来加速？

> **答案**：两个技巧。（1）对固定的 (u,w)，把候选 m 按 `M[u,m]` 升序、候选 k 按 `M[w,k]` 升序预先排好（`_sort_M_slice`）。（2）利用 `M[u,m] + M[w,k] + min_km_dist >= cur_min_M` 作为剪枝条件，一旦当前组合加上「{m}×{k} 的距离下界」已不可能改善当前最优，就 `break` 退出内/外层循环。排序保证了「越往后越大」，所以提前退出是安全的。

**练习 3**：`must_swap` 只记录了「每个节点自身是否需要翻转」，但翻转一个节点其实要连带翻转它的所有后代。代码怎么处理这件事？

> **答案**：构造 `is_descendant` 矩阵，把每个节点的翻转需求广播给它所有后代（含自身）求和，再对 2 取模得到 `final_swap`。因为「翻转两次 = 没翻转」，取模后 `final_swap[i]` 才是节点 i 最终真正需要翻转与否。最后按 `final_swap` 决定每行的左右孩子是否对调，生成 `swapped_Z`。

## 5. 综合实践

把本讲三条主线（问题定义、`linkage` 集成、Cython DP）串成一个完整的小任务。

**任务**：构造一个 10 点数据集，对比「普通建树」「`optimal_ordering=True`」「先建树再调 `optimal_leaf_ordering`」三种结果的叶子序与相邻距离和，并用 `dendrogram` 把普通树与最优叶序树都画出来，直观感受「色块连续性」的差异。

```python
import numpy as np
from scipy.cluster import hierarchy
from scipy.spatial.distance import pdist, squareform
import matplotlib.pyplot as plt

rng = np.random.default_rng(7)
# 构造 3 个明显分离的簇，每簇 3~4 个点，共 10 点
X = np.vstack([
    rng.standard_normal((3, 2)) + [0, 0],
    rng.standard_normal((4, 2)) + [10, 0],
    rng.standard_normal((3, 2)) + [5, 8],
])

# (1) 普通建树
Z_plain = hierarchy.linkage(X, method='ward')
# (2) 一步到位
Z_opt1  = hierarchy.linkage(X, method='ward', optimal_ordering=True)
# (3) 两步
Z_opt2  = hierarchy.optimal_leaf_ordering(hierarchy.linkage(X, method='ward'), X)

DM = squareform(pdist(X))
def adj_sum(order):
    return sum(DM[order[i], order[i+1]] for i in range(len(order)-1))

for name, Z in [("普通", Z_plain), ("最优(一步)", Z_opt1), ("最优(两步)", Z_opt2)]:
    lv = hierarchy.leaves_list(Z)
    print(f"{name:<10} 叶序={lv.tolist()} 相邻距离和={adj_sum(lv):.3f}")

# 验证一步与两步等价
print("一步==两步:", np.allclose(Z_opt1, Z_opt2))

# 可视化对比
fig, axes = plt.subplots(1, 2, figsize=(12, 5))
hierarchy.dendrogram(Z_plain, ax=axes[0]); axes[0].set_title("plain")
hierarchy.dendrogram(Z_opt1,  ax=axes[1]); axes[1].set_title("optimal_ordering=True")
plt.tight_layout(); plt.show()
```

**需要观察并解释**：

1. 「最优(一步)」与「最优(两步)」的相邻距离和相等且都 ≤ 「普通」。
2. 右侧 dendrogram（最优叶序）中同一簇的叶子更连续地排在一起，U 形链接高度从左到右变化更平滑。
3. 解释为何「相邻距离和更小」不等于「聚类更好」——聚类（哪些点合并）完全没变，变的只是展示顺序。这正是最优叶序的定位：**可视化美化**，而非聚类质量提升。

## 6. 本讲小结

- **最优叶序问题**：固定层次树结构不动，只交换内部节点的左右孩子，使相邻叶子的两两距离之和最小；它**不改变聚类**，只改善叶子的左右排列（dendrogram 视觉更连续）。
- **公开入口 `optimal_leaf_ordering(Z, y)`**：纯 Python 封装层做输入校验与惰性桥接（`cy_optimal_leaf_ordering` + `xpx.lazy_apply`），把活儿交给 Cython 后端；`y` 既可是压缩距离矩阵也可是观测矩阵（后者内部 `pdist`）。
- **`linkage(optimal_ordering=True)`**：建树末尾对结果调一次 `optimal_leaf_ordering`，是「建树 + 重排」的语法糖；默认 False，因为算法在大数据集上较慢。
- **Cython DP 核心 `identify_swaps`**：实现 Bar-Joseph 等人 2001 年的算法，用递推 \(U_V[u,w]=\min_{m,k}\{U_{V_l}[u,m]+D[m,k]+U_{V_r}[k,w]\}\) 自底向上算每个节点最优，配合「簇=连续区间」编码与「排序 + 提前剪枝」两个优化加速。
- **从 DP 到输出**：`must_swap` 记录每个节点自身翻转位，经 `is_descendant` 沿树传递并对 2 取模得到 `final_swap`，据此生成 `swapped_Z`——只对调部分行的左右孩子，距离列与簇大小列原样保留，证明聚类结构不变。

## 7. 下一步学习建议

本讲是 hierarchy 子模块「树表示与可视化」单元（u6）的收尾。建议接下来：

1. **进入 u7（校验、数据结构与工程化）**：阅读 `is_valid_linkage` 等校验函数（u7-l1），理解 `optimal_leaf_ordering` 入口里 `_is_valid_linkage(Z, throw=True)` 背后的完整校验规则。
2. **回顾 u6-l2（dendrogram）**：用本讲学到的最优叶序重新画 dendrogram，体会「color_threshold 配色 + 最优叶序」组合如何让可视化效果最佳。
3. **扩展阅读**：若对算法本身感兴趣，可阅读原始论文 _Bar-Joseph, Gifford, Jaakkola, "Fast Optimal Leaf Ordering for Hierarchical Clustering", Bioinformatics 2001_，对照 `.pyx` 中 `identify_swaps` 的注释与剪枝逻辑，理解从朴素 \(O(n^4)\) 到带剪枝版本的复杂度改进。
4. **关注惰性数组**：若你用 dask 处理大数据，可结合 u7-l3 复习 `xpx.lazy_apply` 如何让这个「仅 CPU」的 Cython 后端也能吃惰性输入（注意它会合并 dask 分块，见 `lazy_cython` 的告警配置）。
