# inconsistent 不一致矩阵与 max 系列统计

> 承接：本讲在 [u5-l1（fcluster 多种 criterion 切分层次树）](u5-l1-fcluster-criteria.md) 之后。u5-l1 已经给出了一个核心结论——`fcluster` 的五种 criterion 最终都归约到「单调 monocrit 数组 + 阈值」这一个引擎上。本讲就回答：这些「单调 monocrit 数组」到底从哪里来、怎么算出来的。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `inconsistent(Z, d)` 返回的不一致矩阵 `R` 四列各自代表什么、`d` 如何限定计算范围。
- 看懂 `maxdists` / `maxinconsts` / `maxRstat` 三个函数是如何「沿树向上取最大值」，把逐簇统计量聚合成一条**单调**向量的。
- 读懂背后三个 Cython 后端 `inconsistent`、`get_max_dist_for_each_cluster`、`get_max_Rfield_for_each_cluster` 用「显式栈 + 位图 visited」做后序遍历的实现。
- 把 `R` 接回 `fcluster(criterion='monocrit', ...)`，亲手搭出一条 `inconsistent → maxRstat → fcluster` 的切树流水线，并解释它为什么能把「整体最紧凑的子树」切成一个簇。

## 2. 前置知识

阅读本讲前，请先具备以下认知（若不熟可回头看对应讲义）：

- **linkage matrix `Z`**：形状 (n−1)×4，四列依次是「被合并的两簇编号 / 合并距离 / 新簇的原始观测数」；新簇编号遵循 **n+i** 约定（第 i 步合并出的簇编号为 n+i），整棵树共 2n−1 个节点。详见 [u3-l1](u3-l1-linkage-matrix.md)。
- **cophenetic 距离 / 合并高度**：两簇在某次合并时写进 `Z[i,2]` 的那个距离值；它就是树里每条「链接」的高度。
- **monocrit（单调判据）**：一条长度为 n−1 的向量，下标对应每个非叶簇，要求**单调**——祖先节点的值 ≥ 所有后代节点的值。`fcluster` 用 `cluster_monocrit` 引擎按阈值在树上「自顶向下」切：遇到 `MC[root] ≤ cutoff` 就把整棵子树判为一个 flat 簇。单调性保证了切口的良定义。详见 [u5-l1](u5-l1-fcluster-criteria.md)。
- **`@lazy_cython` 与 `xpx.lazy_apply`**：Python 封装层先用 `xp = array_namespace(...)` 抽象数组后端，再把真正干活的闭包交给 `xpx.lazy_apply`；对普通 NumPy 数组直接短路调用 Cython 后端，对 dask 惰性数组则并入任务图。详见 [u3-l3](u3-l3-python-cython-bridge.md)。

一个贯穿全讲的直觉：**层次树是一棵二叉树，链接高度 `Z[i,2]` 就是每个内部节点的「权值」**。`inconsistent` 在每个节点周围一个 `d` 层的小邻域里统计这些权值的均值/标准差/个数/异常度；`max*` 系列则把任意子树里所有节点的某个量取最大，得到一条沿树单调的向量。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注的符号 |
|------|------|----------------|
| [hierarchy/_hierarchy_impl.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py) | Python 封装层：校验输入、定义闭包、交给 `xpx.lazy_apply` | `inconsistent`、`maxdists`、`maxinconsts`、`maxRstat`、`fcluster` 的 monocrit 分派 |
| [hierarchy/_hierarchy.pyx](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx) | Cython 性能层：显式栈遍历树、原地填充结果数组 | `inconsistent`、`get_max_dist_for_each_cluster`、`get_max_Rfield_for_each_cluster`、`cluster_monocrit`、`cluster_in`、`cluster_dist`、`is_visited`/`set_visited` |

记忆口诀：**Python 层只做「校验 + 包装 + lazy_apply」，Cython 层才是「在树上算数」的真正引擎**。三个 Cython 后端共用同一套「显式栈 + visited 位图 + 后序」的遍历骨架。

## 4. 核心概念与源码讲解

本讲按「先造统计量，再造单调向量，最后接回切树」的顺序，拆成四个最小模块。

### 4.1 inconsistent：不一致矩阵 R 与 depth 限定

#### 4.1.1 概念说明

`inconsistent` 仿照 MATLAB 同名函数，对每个非叶簇 `i` 在它**自身往下 `d` 层**的小邻域里统计链接高度 `Z[·,2]`，得到一行四列的统计：

- `R[i,0]`：邻域内链接高度的**均值**（mean）。
- `R[i,1]`：邻域内链接高度的**标准差**（sample std，自由度 n−1）。
- `R[i,2]`：邻域内被纳入统计的**链接条数**（count）。
- `R[i,3]`：**不一致系数**（inconsistency coefficient），即当前链接高度相对邻域均值的 z 分数。

它衡量的是「这次合并是不是一次『突然跳高』的异常合并」。如果一簇合并的高度比它下面 `d` 层的合并高度明显偏高（标准化后偏大），说明这一步把两个本不相干的子群硬连了起来，是一个「不自然」的合并点——这正是切树时想要识别的边界。

#### 4.1.2 核心流程

对每个非叶簇 `i`（i = 0…n−2）：

1. 从 `i` 出发，做一次**深度受限**的下行走查，栈深 `k` 即「层级」，下钻条件是 `k < d - 1`。
2. 每访问到一个非叶节点 `root`，就把它的链接高度 `Z[root,2]` 累加进 `level_sum`、平方累加进 `level_std_sum`、计数 `level_count += 1`。
3. 走完后：
   - 均值 = `level_sum / level_count`。
   - 方差 = `(level_std_sum − level_sum²/level_count) / (level_count − 1)`（count≥2 时为样本方差；count=1 时退化公式给出 0）。
   - 标准差 = `sqrt(方差)`（方差为 0 则置 0）。
   - 不一致系数 = `(Z[i,2] − 均值) / 标准差`（标准差为 0 则置 0）。

depth `d` 的语义：**`d` = 被纳入统计的层数**。

- `d=1`：只统计簇 `i` 自己这一条链接（count=1，std=0，系数=0）。
- `d=2`：再下钻一层，把它的直接非叶子孩子也算进来。
- `d` 越大，邻域越宽，统计越「全局」。

不一致系数的数学定义（来自 docstring）：

\[
R[i,3] = \frac{Z[i,2] - R[i,0]}{R[i,1]}
\]

#### 4.1.3 源码精读

Python 封装层非常薄：校验 `Z` 是合法 linkage、校验 `d` 是非负整数，然后定义闭包 `cy_inconsistent`，把「分配 (n−1)×4 的 `R`、调 `_hierarchy.inconsistent(Z, R, n, d)`」打包，最后交给 `xpx.lazy_apply`。

```python
# hierarchy/_hierarchy_impl.py
def cy_inconsistent(Z, d, validate):
    if validate:
        _is_valid_linkage(Z, throw=True, name='Z', xp=np)
    R = np.zeros((Z.shape[0], 4), dtype=np.float64)
    n = Z.shape[0] + 1
    _hierarchy.inconsistent(Z, R, n, d)
    return R

return xpx.lazy_apply(cy_inconsistent, Z, d=int(d), validate=is_lazy_array(Z),
                      shape=(Z.shape[0], 4), dtype=xp.float64,
                      as_numpy=True, xp=xp)
```

> 作用：[hierarchy/_hierarchy_impl.py:L1686-L1696](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L1686-L1696) 把 Cython 后端 `_hierarchy.inconsistent(Z, R, n, d)` 包成闭包交给 lazy_apply；`R` 是预分配、被后端原地填充的输出矩阵。

真正的统计逻辑在 Cython 后端。注意三个细节：`for i in range(n-1)` 对每个非叶簇单独清零 visited、单独走查；`if k < d - 1` 是 depth 闸门；叶子（编号 `< n`）不会被下钻、也不贡献链接高度。

```cython
# hierarchy/_hierarchy.pyx
for i in range(n - 1):
    k = 0
    level_count = 0; level_sum = 0; level_std_sum = 0
    memset(visited, 0, visited_size)
    curr_node[0] = i

    while k >= 0:
        root = curr_node[k]
        if k < d - 1:                       # depth 闸门：只在前 d-1 层下钻
            i_lc = <int>Z[root, 0]
            if i_lc >= n and not is_visited(visited, i_lc):   # 非叶左孩子
                set_visited(visited, i_lc); k += 1
                curr_node[k] = i_lc - n; continue
            i_rc = <int>Z[root, 1]
            if i_rc >= n and not is_visited(visited, i_rc):   # 非叶右孩子
                set_visited(visited, i_rc); k += 1
                curr_node[k] = i_rc - n; continue

        dist = Z[root, 2]                   # 累加这一条链接的高度
        level_count += 1
        level_sum += dist
        level_std_sum += dist * dist
        k -= 1                              # 回溯

    R[i, 0] = level_sum / level_count
    R[i, 2] = level_count
    if level_count < 2:                     # count=1 的退化分支
        level_std = (level_std_sum - (level_sum * level_sum)) / level_count
    else:                                   # 样本方差（自由度 count-1）
        level_std = ((level_std_sum - ((level_sum * level_sum) / level_count))
                     / (level_count - 1))
    if level_std > 0:
        level_std = sqrt(level_std)
        R[i, 1] = level_std
        R[i, 3] = (Z[i, 2] - R[i, 0]) / level_std
    else:
        R[i, 1] = 0                          # R[i,3] 保持 0（数组预置 0）
```

> 作用：[hierarchy/_hierarchy.pyx:L538-L585](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L538-L585) 逐簇做深度受限下钻，累加链接高度的均值/平方和/计数，再算样本标准差与不一致系数，原地写回 `R`。

辅助的位图 `visited` 用「每节点 1 bit」标记是否已下钻，节省内存：

```cython
# hierarchy/_hierarchy.pyx
cdef inline int is_visited(uchar *bitset, int i) noexcept:
    return bitset[i >> 3] & (1 << (i & 7))

cdef inline void set_visited(uchar *bitset, int i) noexcept:
    bitset[i >> 3] |= 1 << (i & 7)
```

> 作用：[hierarchy/_hierarchy.pyx:L32-L43](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L32-L43) 用位运算把「节点 i 是否访问过」压进字节数组，是后序遍历避免重复访问的关键。

#### 4.1.4 代码实践

这是一个**源码阅读 + 手算验证**型实践，用测试套件里的固定小数据 `ytdist`。

1. 实践目标：手算 `inconsistent(linkage_ytdist_single, d=2)` 的第 3 行，验证与官方输出逐位一致。
2. 操作步骤：
   - 固定数据：`ytdist` 是 6 个点的 pdist 距离（见 [hierarchy/tests/hierarchy_test_data.py:L35-L42](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/tests/hierarchy_test_data.py#L35-L42)），其 single linkage 为
     \[
     Z = \begin{bmatrix}2&5&138&2\\3&4&219&2\\0&7&255&3\\1&8&268&4\\6&9&295&6\end{bmatrix},\quad n=6.
     \]
   - 对簇 `i=2`（`Z[2]=[0,7,255,3]`，其右孩子是簇 7=n+1→`Z[1]`）：d=2 时邻域包含 `Z[2,2]=255` 与 `Z[1,2]=219` 两条链接（左孩子 0 是叶子不入统计）。
   - 手算：均值=(255+219)/2=237；样本方差=((255²+219²) − (255+219)²/2)/(2−1)=((65025+47961) − 474²/2)=112986 − 112338=648；std=√648≈25.4558；系数=(255−237)/25.4558≈0.7071。
3. 需要观察的现象：你算出的 `[237, 25.4558, 2, 0.7071]` 应与官方 `R[2]` 完全一致。
4. 预期结果：对照 [hierarchy/tests/hierarchy_test_data.py:L107-L111](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/tests/hierarchy_test_data.py#L107-L111) 的 `inconsistent_ytdist[2]` 第 3 行 `[237., 25.45584412, 2., 0.70710678]`，逐元素吻合。可运行：

```python
# 示例代码：验证手算
from scipy.cluster.hierarchy import inconsistent
from scipy.cluster.hierarchy.tests.hierarchy_test_data import linkage_ytdist_single
print(inconsistent(linkage_ytdist_single, 2)[2])   # 期望 [237. 25.45584412   2.   0.70710678]
```

#### 4.1.5 小练习与答案

**练习 1**：把上面的 `d` 从 2 改成 1，`R[2]` 会变成什么？为什么？

> 答案：变成 `[255., 0., 1., 0.]`。因为 d=1 时 depth 闸门 `k < 0` 永不成立，不下钻，邻域只有簇 2 自己这一条链接：count=1、std=0、系数=0。

**练习 2**：标准差用的是总体标准差还是样本标准差？依据是哪一行代码？

> 答案：样本标准差（自由度 count−1）。依据是 `level_std = (... ) / (level_count - 1)`，除以的是 `level_count − 1` 而非 `level_count`，对应 NumPy 的 `np.std(x, ddof=1)`。

**练习 3**：为什么叶子节点（编号 `< n`）从不贡献 `level_sum`？

> 答案：叶子没有对应的 `Z` 行（`Z` 只有 n−1 行，对应非叶簇 n…2n−2），它本身没有「合并高度」可言；代码里 `i_lc >= n` / `i_rc >= n` 的判断把叶子挡在下钻之外，叶子只是被「挂」在父簇下计数簇大小时才有意义。

---

### 4.2 maxdists：沿树向上的最大合并距离

#### 4.2.1 概念说明

`maxdists(Z)` 返回长度 n−1 的向量 `MD`，其中 `MD[i]` 是**簇 i 及其所有后代节点里最大的链接高度**：

\[
\text{MD}[i] = \max_{j \in Q(i)} Z[j,2]
\]

其中 `Q(i)` 是「簇 i 及其全部后代」对应的 `Z` 行下标集合。

对于**单调**链接方法（single/complete/average/ward/weighted），父簇的合并距离一定 ≥ 所有后代，所以 `MD[i] = Z[i,2]`；但对于**非单调**方法（centroid/median，可能出现距离倒挂），`MD[i]` 可能大于 `Z[i,2]`——某个后代的合并反而比父辈更高。`maxdists` 把这种「子树内最高那条链接」暴露出来。

更重要的角色：`MD` 是一条**天然单调**的向量（父 = max(自己, 所有后代) ≥ 任意后代），因此它可以直接当 monocrit 喂给 `fcluster(criterion='distance')`。事实上 `cluster_dist` 后端就是「算 `maxdists` → 喂 `cluster_monocrit`」。

#### 4.2.2 核心流程

`get_max_dist_for_each_cluster` 是一次**后序（post-order）DFS**：

1. 从树根 `2n−2` 出发，用显式栈 `curr_node` + visited 位图遍历。
2. 优先下钻到未访问的非叶孩子；只有当两个孩子都处理完（或都是叶子）时，才计算当前节点：

\[
\text{MD}[i] = \max\bigl(Z[i,2],\ \text{MD}[\text{左孩子}],\ \text{MD}[\text{右孩子}]\bigr)
\]

3. 「先孩子后父亲」保证了算父亲时两个孩子的 `MD` 已经就绪。

后序是关键：它把「逐节点取 max」变成一次 O(n) 的树形 DP。

#### 4.2.3 源码精读

```cython
# hierarchy/_hierarchy.pyx
k = 0
curr_node[0] = 2 * n - 2                 # 从树根开始
while k >= 0:
    root = curr_node[k] - n
    i_lc = <int>Z[root, 0]
    i_rc = <int>Z[root, 1]

    if i_lc >= n and not is_visited(visited, i_lc):   # 先下钻左孩子
        set_visited(visited, i_lc); k += 1
        curr_node[k] = i_lc; continue
    if i_rc >= n and not is_visited(visited, i_rc):   # 再下钻右孩子
        set_visited(visited, i_rc); k += 1
        curr_node[k] = i_rc; continue

    max_dist = Z[root, 2]                 # 自己的链接高度
    if i_lc >= n:                          # 取左子树已算好的 max
        max_l = MD[i_lc - n]
        if max_l > max_dist: max_dist = max_l
    if i_rc >= n:                          # 取右子树已算好的 max
        max_r = MD[i_rc - n]
        if max_r > max_dist: max_dist = max_r
    MD[root] = max_dist                    # 写回（原地填充）

    k -= 1                                 # 回溯到父节点
```

> 作用：[hierarchy/_hierarchy.pyx:L470-L502](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L470-L502) 后序遍历整棵树，每个节点取「自身链接高度 vs 左右子树 MD」三者最大，原地写回 `MD`。

Python 封装层依旧只做校验 + lazy_apply，注意它把后端结果 `MD` 直接返回：

```python
# hierarchy/_hierarchy_impl.py
def cy_maxdists(Z, validate):
    if validate:
        _is_valid_linkage(Z, throw=True, name='Z', xp=np)
    MD = np.zeros((Z.shape[0],))
    _hierarchy.get_max_dist_for_each_cluster(Z, MD, Z.shape[0] + 1)
    return MD
```

> 作用：[hierarchy/_hierarchy_impl.py:L3875-L3884](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L3875-L3884) 预分配 `MD`，调后端原地填充，再经 `xpx.lazy_apply` 返回。

`cluster_dist`（`fcluster(criterion='distance')` 的后端）正是 `maxdists + cluster_monocrit` 的二步组合：

```cython
# hierarchy/_hierarchy.pyx
cdef double[:] max_dists = np.ndarray(n, dtype=np.float64)
get_max_dist_for_each_cluster(Z, max_dists, n)
cluster_monocrit(Z, max_dists, T, cutoff, n)
```

> 作用：[hierarchy/_hierarchy.pyx:L93-L95](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L93-L95) 先把 `maxdists` 当作单调 monocrit，再交给统一的 `cluster_monocrit` 切树。这印证了 u5-l1 的结论：「distance 准则 = maxdists 当 monocrit」。

#### 4.2.4 代码实践

1. 实践目标：观察非单调方法（median）下 `maxdists` 与 `Z[:,2]` 的差异。
2. 操作步骤：用 `maxdists` docstring 里的 12 点数据，对 `median(pdist(X))` 的结果同时打印 `Z[:,2]` 与 `maxdists(Z)`。
3. 需要观察的现象：最后一行 `Z[10,2]=3.25`，但 `maxdists(Z)[10]=3.5`——因为它的某个后代（簇 16/17）合并高度 3.5 比它自身还高（距离倒挂）。
4. 预期结果：

```python
# 示例代码
from scipy.cluster.hierarchy import median, maxdists
from scipy.spatial.distance import pdist
X = [[0,0],[0,1],[1,0],[0,4],[0,3],[1,4],[4,0],[3,0],[4,1],[4,4],[3,4],[4,3]]
Z = median(pdist(X))
print(Z[:,2])        # 最后一项 3.25
print(maxdists(Z))   # 最后一项 3.5（> 3.25，体现非单调性）
```

5. 待本地验证：精确数值以本机 SciPy 输出为准（docstring 已给出参考值，见 [hierarchy/_hierarchy_impl.py:L3860-L3863](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L3860-L3863)）。

#### 4.2.5 小练习与答案

**练习 1**：对 `linkage_ytdist_single`（单调）调用 `maxdists`，结果应等于什么？

> 答案：等于 `Z[:,2]`，即 `[138, 219, 255, 268, 295]`。因为 single 是单调方法，父簇高度 ≥ 所有后代，`MD[i]=Z[i,2]`。

**练习 2**：为什么 `get_max_dist_for_each_cluster` 必须用后序，而不能用前序（先算父再算子）？

> 答案：父的 `MD` 依赖两个孩子的 `MD`（`max(MD[左], MD[右])`）。前序算父时孩子尚未就绪，拿不到正确值；后序保证「先孩子后父亲」，是一次正确的树形 DP。

---

### 4.3 maxinconsts 与 maxRstat：任意列的沿树最大统计

#### 4.3.1 概念说明

`maxRstat(Z, R, i)` 是 `maxdists` 的「泛化版」：它把「取 `Z[·,2]` 的最大」换成「取不一致矩阵 `R` 第 `i` 列的最大」。即对每个非叶簇 `j`：

\[
\text{MR}[j] = \max_{k \in Q(j)} R[k, i]
\]

`i ∈ {0,1,2,3}` 分别对应均值、标准差、计数、不一致系数四列。它返回一条**单调**向量（同理，父 = max(自己, 所有后代) ≥ 后代），可以直接当 monocrit。

`maxinconsts(Z, R)` 是 `maxRstat` 的特例：它**写死第 3 列**（不一致系数），等价于 `maxRstat(Z, R, 3)`。换言之：

\[
\texttt{maxinconsts}(Z, R) \equiv \texttt{maxRstat}(Z, R, 3).
\]

这一族函数的意义：`inconsistent` 给出的是「逐簇」的统计（一簇一行），但切树需要的是「逐子树」的单调判据——`maxRstat` 正是把逐簇统计沿树向上聚合成逐子树单调向量的桥梁。

#### 4.3.2 核心流程

后端 `get_max_Rfield_for_each_cluster(Z, R, max_rfs, n, rf)` 与 4.2 的 `get_max_dist_for_each_cluster` **同构**，唯一区别是把 `Z[root,2]` 换成 `R[root,rf]`：

1. 从树根 `2n−2` 后序遍历。
2. 每个节点取三者最大：`R[root,rf]`、左子树的 `max_rfs`、右子树的 `max_rfs`。
3. 原地写回 `max_rfs[root]`。

Python 层 `maxRstat` 多做两步校验：`i` 必须是 int 且在 0..3 之间，`Z` 与 `R` 行数必须一致。`maxinconsts` 则省去 `i` 校验（写死 3），但仍校验行数一致。

#### 4.3.3 源码精读

后端 `get_max_Rfield_for_each_cluster`：

```cython
# hierarchy/_hierarchy.pyx
max_rf = R[root, rf]                      # 自身第 rf 列
if i_lc >= n:
    max_l = max_rfs[i_lc - n]             # 左子树已算好的 max
    if max_l > max_rf: max_rf = max_l
if i_rc >= n:
    max_r = max_rfs[i_rc - n]             # 右子树已算好的 max
    if max_r > max_rf: max_rf = max_r
max_rfs[root] = max_rf                     # 原地写回
```

> 作用：[hierarchy/_hierarchy.pyx:L430-L439](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L430-L439) 后序取「自身 R[·,rf] vs 左右子树」三者最大。遍历骨架（栈 + visited）与 `get_max_dist_for_each_cluster` 完全一致，差别只在取值的来源列。

Python 层 `maxRstat` 的校验与分发：

```python
# hierarchy/_hierarchy_impl.py
if not isinstance(i, int):
    raise TypeError('The third argument must be an integer.')
if i < 0 or i > 3:
    raise ValueError('i must be an integer between 0 and 3 inclusive.')
...
def cy_maxRstat(Z, R, i, validate):
    if validate:
        _is_valid_linkage(Z, throw=True, name='Z', xp=np)
        _is_valid_im(R, throw=True, name='R', xp=np)
    MR = np.zeros((Z.shape[0],))
    n = Z.shape[0] + 1
    _hierarchy.get_max_Rfield_for_each_cluster(Z, R, MR, n, i)   # 注意第 5 个参数 i
    return MR
```

> 作用：[hierarchy/_hierarchy_impl.py:L4071-L4088](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L4071-L4088) 校验 `i ∈ [0,3]`，预分配 `MR`，把列号 `i` 作为 `rf` 透传给后端。

`maxinconsts` 的闭包则把 `rf` 写死为 3：

```python
# hierarchy/_hierarchy_impl.py
def cy_maxinconsts(Z, R, validate):
    ...
    _hierarchy.get_max_Rfield_for_each_cluster(Z, R, MI, n, 3)   # 写死 rf=3
    return MI
```

> 作用：[hierarchy/_hierarchy_impl.py:L3973-L3980](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L3973-L3980) `maxinconsts` 就是 `maxRstat(Z, R, 3)` 的快捷方式——后端调用只差最后一个参数 `3`。

#### 4.3.4 代码实践

1. 实践目标：验证 `maxinconsts(Z, R) == maxRstat(Z, R, 3)`，并对比四列各自的沿树最大。
2. 操作步骤：对 `median(pdist(X))` 算 `R=inconsistent(Z)`，再分别 `maxRstat(Z,R,0..3)`。
3. 需要观察的现象：`maxRstat(Z,R,3)` 与 `maxinconsts(Z,R)` 逐元素相同；`maxRstat(Z,R,0)`（均值列）单调递增地汇总了每棵子树里最大的「平均链接高度」。
4. 预期结果：对照 [hierarchy/_hierarchy_impl.py:L4051-L4062](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L4051-L4062) 的 docstring 示例，`maxRstat(Z,R,3)` 末三项均为 `1.15470054`。

```python
# 示例代码
from scipy.cluster.hierarchy import median, inconsistent, maxinconsts, maxRstat
from scipy.spatial.distance import pdist
X = [[0,0],[0,1],[1,0],[0,4],[0,3],[1,4],[4,0],[3,0],[4,1],[4,4],[3,4],[4,3]]
Z = median(pdist(X)); R = inconsistent(Z)
import numpy as np
print(np.array_equal(maxinconsts(Z, R), maxRstat(Z, R, 3)))   # True
```

#### 4.3.5 小练习与答案

**练习 1**：`maxRstat(Z, R, 2)`（计数列）在树根处的值代表什么？

> 答案：代表「整棵树被纳入统计的最大单簇链接条数」。由于 `R[:,2]` 是每个簇邻域内的链接计数，沿树取 max 后，树根处是所有簇里邻域最宽（计数最大）的那个值，通常出现在靠近树根的节点。

**练习 2**：为什么 `maxRstat` 返回的向量一定是单调的（父 ≥ 子）？

> 答案：因为每个节点的值被定义为 `max(自身, 左子树max, 右子树max)`，父节点的取值集合严格包含每个孩子的取值集合，故父值 ≥ 任意孩子值，沿树单调。这正是它能当 monocrit 的前提。

**练习 3**：如果想用「每棵子树里最大的**平均链接高度**」作为切树判据，应该调 `maxRstat` 的哪一列？

> 答案：第 0 列（均值列），即 `maxRstat(Z, R, 0)`。`R[:,0]` 是邻域均值，沿树取 max 后得到「子树内最高的邻域平均高度」。

---

### 4.4 从 monocrit 到切树：把统计量接回 fcluster

#### 4.4.1 概念说明

本模块把前三个模块串起来：`inconsistent` 造逐簇统计 `R` → `maxRstat` 把某一列聚成单调 monocrit `MR` → `fcluster(criterion='monocrit', monocrit=MR)` 用阈值 `t` 切树。

理解这条流水线，关键是看清 `cluster_monocrit` 引擎如何消费一个单调向量：它自树根向下走，**一旦某个节点 `root` 满足 `MC[root] ≤ cutoff`，就把整棵子树判为一个 flat 簇并停止下钻**；否则继续往下找更小的子树。由于 `MC` 单调，这种「在最高的合格节点处收口」是良定义的。

#### 4.4.2 核心流程

`fcluster` 的 monocrit 分派（Python 层）只是把用户传入的 `monocrit` 数组规整成 C 连续 float64，然后直接调后端 `cluster_monocrit`：

```python
# hierarchy/_hierarchy_impl.py
elif criterion == 'monocrit':
    _hierarchy.cluster_monocrit(Z, monocrit, T, float(t), int(n))
elif criterion == 'maxclust_monocrit':
    _hierarchy.cluster_maxclust_monocrit(Z, monocrit, T, int(n), int(t))
```

> 作用：[hierarchy/_hierarchy_impl.py:L2593-L2596](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2593-L2596) `monocrit` 准则直接把用户给的（单调）数组喂给 `cluster_monocrit`；`maxclust_monocrit` 则在该数组上二分搜索阈值以满足目标簇数。

后端 `cluster_monocrit` 的核心判定：

```cython
# hierarchy/_hierarchy.pyx
if cluster_leader == -1 and MC[root] <= cutoff:   # 找到一个簇的「收口点」
    cluster_leader = root
    n_cluster += 1
```

> 作用：[hierarchy/_hierarchy.pyx:L273-L275](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L273-L275) 自顶向下走，遇到第一个 `MC[root] ≤ cutoff` 的节点就把它的整棵子树判为一个 flat 簇（`cluster_leader` 记录收口点，回溯到它时复位）。

> ⚠️ 一个需要澄清的细节：`R[:,3]`（不一致系数）是一个 **z 分数**，量纲是「标准差倍数」，并不是字面的「平均距离」；字面的「平均链接高度」是 `R[:,0]`。本讲实践任务里的 `maxRstat(Z, R, 3)` 取的是**不一致系数列**，所以严格说它切出的是「**最大不一致系数低于阈值**的子树」——即「这棵子树里最『突兀』的那次合并，其突兀程度仍在 `t` 个标准差以内」。若你真的想要「最大平均距离低于阈值」，应改用 `maxRstat(Z, R, 0)`。两者都合法，差别只在选哪一列当判据。

#### 4.4.3 源码精读

`cluster_in`（`fcluster(criterion='inconsistent')` 的后端）和 `cluster_dist` 一样，是「先用 `get_max_Rfield_for_each_cluster` 造 monocrit、再调 `cluster_monocrit`」的二步组合，只不过它取 `R` 的第 3 列：

```cython
# hierarchy/_hierarchy.pyx
cdef double[:] max_inconsists = np.ndarray(n, dtype=np.float64)
get_max_Rfield_for_each_cluster(Z, R, max_inconsists, n, 3)   # 等价于 maxinconsts(Z,R)
cluster_monocrit(Z, max_inconsists, T, cutoff, n)
```

> 作用：[hierarchy/_hierarchy.pyx:L117-L119](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L117-L119) `cluster_in` 内部就是把 `R[:,3]` 沿树取 max（即 `maxinconsts`）当 monocrit，再交给统一的 `cluster_monocrit` 切树。这进一步印证 u5-l1 的统一引擎结论：`inconsistent` 准则 = `maxinconsts(Z,R)` 当 monocrit。

于是整条流水线的等价关系一目了然：

| `fcluster` criterion | 等价的 monocrit | 后端组合 |
|---|---|---|
| `distance` | `maxdists(Z)` | `get_max_dist_for_each_cluster` + `cluster_monocrit` |
| `inconsistent` | `maxinconsts(Z, R)` = `maxRstat(Z, R, 3)` | `get_max_Rfield_for_each_cluster(...,3)` + `cluster_monocrit` |
| `monocrit`（用户自定义） | 用户给的任意单调向量 | 直接 `cluster_monocrit` |
| `maxclust_monocrit` | 用户给的任意单调向量 | `cluster_maxclust_monocrit`（二分搜索阈值） |

#### 4.4.4 代码实践（本讲主实践）

这条实践直接复刻 `fcluster` docstring 里的 monocrit 示例（[hierarchy/_hierarchy_impl.py:L2463-L2464](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2463-L2464)），并解释它为什么能把「最紧凑的子树」切成一个簇。

1. 实践目标：搭出 `inconsistent → maxRstat → fcluster(monocrit)` 流水线，解释其切树含义。
2. 操作步骤：
   - 取 docstring 的 8 点数据 `X = [[i] for i in [2,8,0,4,1,9,9,0]]`，`Z = linkage(X, 'ward')`。
   - `R = inconsistent(Z, d=2)`；`MR = maxRstat(Z, R, 3)`；`T = fcluster(Z, t=0.8, criterion='monocrit', monocrit=MR)`。
   - 把 `MR` 与 `Z[:,2]` 并排打印，观察哪些簇的 `MR ≤ 0.8`。
3. 需要观察的现象：`MR` 沿树单调（靠近根的值更大）；`fcluster` 在「`MR[root] ≤ 0.8` 的最高节点」处收口，把这些节点为根的整棵子树各判为一个 flat 簇。
4. 预期结果：得到若干个 flat 簇标签（从 1 起）。逐簇检查会发现：被切成同一簇的子树，其内部所有合并的「不一致系数」都不超过 0.8，即没有任何一次「突兀跳高」的合并——这正是「紧凑子树」的含义。
5. 解释（回答实践任务的提问）：`MR = maxRstat(Z, R, 3)` 对每个簇取「自身及后代里最大的不一致系数」。`cluster_monocrit` 自顶向下走，一旦 `MR[root] ≤ t` 就把整棵子树收为一个簇。由于 `MR` 单调，子树内部所有节点的 `MR` 都 ≤ `MR[root] ≤ t`，意味着这棵子树里**最突兀的那次合并**（z 分数最大的）也仍在 `t` 个标准差以内——也就是说，这棵子树内部的合并高度都「符合邻域的常规水平」，没有跨越式的跳跃，因而是一个紧凑、自然的簇。反之，若某条合并异常突兀（`MR > t`），引擎就不会在那里收口，而是继续下钻，把异常合并两侧拆成不同的簇。这就是这条流水线能识别「紧凑子树」的根本原因。

```python
# 示例代码：inconsistent → maxRstat → fcluster(monocrit) 流水线
from scipy.cluster.hierarchy import linkage, inconsistent, maxRstat, fcluster
X = [[i] for i in [2, 8, 0, 4, 1, 9, 9, 0]]
Z = linkage(X, 'ward')
R = inconsistent(Z, d=2)
MR = maxRstat(Z, R, 3)                       # 每簇「最大不一致系数」，单调
T = fcluster(Z, t=0.8, criterion='monocrit', monocrit=MR)
print(T)                                      # flat 簇标签（从 1 起）
# 对照：换成「最大平均链接高度」当判据
MR0 = maxRstat(Z, R, 0)
T0 = fcluster(Z, t=1.5, criterion='monocrit', monocrit=MR0)
print(T0)
```

6. 待本地验证：簇标签的精确个数与编号以本机 SciPy 版本输出为准；重点是观察 `MR` 的单调性与切树位置的对应关系。

#### 4.4.5 小练习与答案

**练习 1**：把实践里的 `maxRstat(Z, R, 3)` 换成 `maxRstat(Z, R, 0)`（均值列），并把 `t` 调到一个合适值，切出的簇通常会更「按绝对高度」分组。为什么？

> 答案：第 0 列是邻域均值（绝对量纲），以其为 monocrit 等价于「按子树内的最大平均合并高度」分组；而第 3 列是 z 分数（相对量纲），按「相对邻域的突兀程度」分组。前者关注「这簇有多高」，后者关注「这簇的合并是否反常」，故分组口径不同。

**练习 2**：`fcluster(criterion='inconsistent', depth=2, t=0.8)` 与「`R=inconsistent(Z,2); MR=maxRstat(Z,R,3); fcluster(criterion='monocrit', monocrit=MR, t=0.8)`」结果应当如何？

> 答案：应当完全一致。因为 `cluster_in` 后端就是「`get_max_Rfield_for_each_cluster(...,3)`（即 maxinconsts）+ `cluster_monocrit`」，与手动流水线逐位等价。可用 `np.array_equal` 验证。

**练习 3**：为什么 monocrit 必须单调？给一个非单调 monocrit 会导致什么问题。

> 答案：`cluster_monocrit` 在 `MC[root] ≤ cutoff` 处收口并停止下钻。若 `MC` 非单调，可能出现「父 ≤ cutoff 但某个孩子 > cutoff」——引擎在父处收口把整棵子树判为一簇，可孩子处本应被「拆开」，导致切口与树的嵌套结构矛盾、簇边界不自洽。单调性保证「父合格则所有后代也合格」，切口才是良定义的。

## 5. 综合实践

把本讲四个模块串成一个端到端的小任务：**用不一致系数切树，并与 `criterion='inconsistent'` 内置实现交叉验证**。

要求：

1. 取一个 10 点的一维数据集（如 `X = [[i] for i in [2,8,0,4,1,9,9,0,5,3]]`），`Z = linkage(X, 'ward')`。
2. 手动走流水线：`R = inconsistent(Z, d=2)` → `MR = maxRstat(Z, R, 3)` → `T1 = fcluster(Z, t=1.0, criterion='monocrit', monocrit=MR)`。
3. 调用内置：`T2 = fcluster(Z, t=1.0, criterion='inconsistent', depth=2)`。
4. 验证 `T1` 与 `T2` 给出**相同的划分**（提示：簇编号可能不同，用 `np.unique` 重编号或用 [u7-l2 的 is_isomorphic](u7-l2-disjointset-leaders-isomorphism.md) 思路比较两组标签的同构性）。
5. 再额外画一张表：对每个非叶簇，列出 `Z[i,2]`（自身高度）、`R[i,0]`（邻域均值）、`R[i,3]`（不一致系数）、`maxRstat(Z,R,3)[i]`（子树最大不一致系数），亲手指出 `fcluster` 会在哪些簇「收口」。

这个任务同时检验你对 `inconsistent` 的统计口径、`maxRstat` 的沿树聚合、以及 `cluster_monocrit` 的收口语义三者的理解。

## 6. 本讲小结

- `inconsistent(Z, d)` 在每个非叶簇「自身往下 `d` 层」的小邻域里统计链接高度，返回 (n−1)×4 的 `R`：均值 / 样本标准差 / 链接计数 / 不一致系数（z 分数）。
- `maxdists(Z)` 后端 `get_max_dist_for_each_cluster` 用后序 DFS 取「子树内最大链接高度」，对单调方法等于 `Z[:,2]`，对 centroid/median 才会暴露距离倒挂。
- `maxRstat(Z, R, i)` 是 `maxdists` 的泛化（取 `R` 第 i 列）；`maxinconsts(Z, R)` ≡ `maxRstat(Z, R, 3)`，二者后端同为 `get_max_Rfield_for_each_cluster`。
- 三个 Cython 后端共用「显式栈 + visited 位图 + 后序」骨架，把逐簇统计沿树向上聚合成**单调**向量——这正是 monocrit 切树所要求的。
- `cluster_dist` 与 `cluster_in` 都只是「先用 `get_max_*` 造单调 monocrit、再调统一引擎 `cluster_monocrit`」的二步组合，印证了 u5-l1「所有 criterion 归约到 monocrit 引擎」的结论。
- 实践流水线 `inconsistent → maxRstat → fcluster(monocrit)` 之所以能切出「紧凑子树」，是因为 `maxRstat` 取的是子树内最突兀合并的 z 分数，阈值限制了「允许的最大突兀度」。

## 7. 下一步学习建议

- 想看 monocrit 引擎本身如何用显式栈实现「自顶向下收口」的细节，精读 [hierarchy/_hierarchy.pyx 的 cluster_monocrit](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L237-L303)（u5-l1 已有概览，可在此回看）。
- 想了解「两组 flat 标签是否同构」的判定（本讲综合实践第 4 步用到），继续学 [u7-l2 DisjointSet、leaders 与同构判断](u7-l2-disjointset-leaders-isomorphism.md)。
- 想把树画出来直观对照切树位置，进入 [u6-l2 dendrogram 可视化与配色](u6-l2-dendrogram.md)，用 `color_threshold` 把本讲的切树结果着色呈现。
- 若对「另一类保真度度量」感兴趣，可并行阅读 [u5-l4 cophenetic 距离与 cophenetic 相关系数](u5-l4-cophenet.md)，它从「树对原始距离的保真度」角度评估聚类质量，与本讲的「不一致性」互补。
