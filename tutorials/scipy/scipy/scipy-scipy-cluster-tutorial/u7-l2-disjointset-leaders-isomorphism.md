# DisjointSet、leaders 与同构判断

## 1. 本讲目标

本讲是 `scipy.cluster.hierarchy` 工程化主题的第二篇（承接 u7-l1 的校验设施）。学完后你应当能够：

- 说清 `DisjointSet` 这个被 hierarchy 重新导出的并查集（union-find）数据结构提供了哪些能力，以及它的 `merge`/`find`/`connected` 各自的复杂度与实现技巧。
- 理解 `leaders(Z, T)` 如何「反向」地从一棵层次树 `Z` 和一组扁平簇标签 `T` 找回每个扁平簇在树中的代表节点（leader），以及它如何用一遍后序 DFS 把「单色子树」的根标出来。
- 看懂 `is_isomorphic(T1, T2)` 用「按 T1 排序 + 比较值变化边界」的 \(O(n\log n)\) 技巧判断两组标签是否代表同一个划分。
- 把这三者放进同一个「聚类等价性（clustering equivalence）」的概念框架里，并明白它们之间是**概念同源、实现独立**的关系。

## 2. 前置知识

- **linkage matrix `Z`**：形状 \((n-1)\times4\)，四列依次为「被合并的两簇编号、合并距离、新簇的原始观测数」；原始观测占编号 \(0\dots n-1\)，第 \(i\) 步合并产生的新簇编号为 \(n+i\)，整棵树共 \(2n-1\) 个节点。详见 u3-l1。
- **扁平簇标签 `T`**：`fcluster` 的输出，是一个长度为 \(n\)、从 1 起的 int32 数组，`T[p]` 表示第 \(p\) 个原始观测所属的扁平簇编号。详见 u5-l1。
- **并查集（union-find / disjoint-set）**：一种维护「若干互不相交集合」的数据结构，核心操作是 `find(x)`（求 `x` 所在集合的代表元）与 `union(x,y)`（合并两集合）。配合「按大小合并 + 路径压缩」可让单次操作接近常数时间。
- **代表元 / leader**：在并查集里每个集合挑一个固定元素当「根」；在层次聚类里，一个扁平簇的 leader 是树中「以它为根的子树全部叶子都属于同一个扁平簇」的那个**最深**节点。
- 本讲依赖 u5-l1（`fcluster` 的 `T`），并复用 u3-l3 讲过的 `lazy_cython`/`xpx.lazy_apply` 桥接三件套（本讲不再重复其机制，只在 `leaders` 处指出它沿用了同一套桥接）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [scipy/_lib/_disjoint_set.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_disjoint_set.py) | `DisjointSet` 类的纯 Python 实现（位于 `scipy/_lib`，被 hierarchy 重新导出）。 |
| [hierarchy/_hierarchy_impl.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py) | Python 封装层：导入并重新导出 `DisjointSet`；实现 `is_isomorphic`、`leaders` 的输入校验与桥接。 |
| [hierarchy/_hierarchy.pyx](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx) | Cython 性能层：`leaders` 的真正后序 DFS 算法 `leaders(...)`。 |
| [hierarchy/tests/test_disjoint_set.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/tests/test_disjoint_set.py) | `DisjointSet` 的单元测试，是理解其行为最好的「可执行文档」。 |
| [hierarchy/tests/test_hierarchy.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/tests/test_hierarchy.py) | `TestLeaders`、`TestIsIsomorphic` 两组测试，含同构判断的反例与回归用例。 |

> **一句话先澄清关系**：`DisjointSet` 是 hierarchy **面向用户导出**的通用并查集工具；但 `leaders` 与 `is_isomorphic` **并没有在内部调用 `DisjointSet`**——它们各自用了更贴合「树形结构 / 一维数组」的专用算法。三者的联系是**概念层面**的（都围绕「聚类等价性」），而非调用关系。这一点会在 4.1.3 与综合实践中再次强调。

## 4. 核心概念与源码讲解

### 4.1 DisjointSet：公开的并查集数据结构

#### 4.1.1 概念说明

`DisjointSet` 解决的是**增量连通性问题**：给定一堆元素，支持「把 x、y 所在的集合合并」「查询 x、y 是否已连通」「查询 x 所在集合的全部成员」。它就是教科书里的 union-find（merge-find）数据结构。

为什么 hierarchy 要导出它？因为「聚类」本质上就是把观测分成若干互不相交的集合，而并查集正是描述「互不相交集合」最自然的数据结构。SciPy 把这个通用工具放在 `scipy._lib._disjoint_set`，再由 `cluster.hierarchy` 重新导出，方便聚类用户直接用来做自定义的「连通 / 合并」推理（例如自己实现一种合并式算法、或在预处理阶段把已知等价的样本预先合并）。

它对外暴露的核心方法：`add`（加元素）、`merge`（合并两集合）、`__getitem__`（即 `find`，求代表元）、`connected`（连通性查询）、`subset`/`subsets`（取集合成员）、`subset_size`（集合大小），以及属性 `n_subsets`（当前集合数）。

#### 4.1.2 核心流程

`DisjointSet` 用四张字典维护状态，每个操作的核心流程如下：

- **建/加**：每个新元素 `x` 自成一集，`_parents[x]=x`、`_sizes[x]=1`、`_nbrs[x]=x`（循环链表自指）、记录插入序号，`n_subsets += 1`。
- **find（`__getitem__`）**：沿 `_parents` 往上找根，过程中做**路径折半（path halving）**——把当前节点直接挂到祖父下面，摊薄后续查询深度。
- **merge**：先 `find` 两边根 `xr`、`yr`；若同根返回 `False`；否则**按大小合并**（小集并入大集），等大时以「先插入者为父」打破平局；同时把两条循环链表拼接，`n_subsets -= 1`。
- **subset（取成员）**：利用 `_nbrs` 这条**循环链表**，从 `x` 出发绕一圈即可枚举同集全部元素，无需遍历整张表。

两个关键不变量（invariant）：

1. **按大小合并 + 路径折半** ⇒ 单次 `find`/`merge` 的均摊复杂度约为 \(O(\alpha(n))\)，其中 \(\alpha\) 是反阿克曼函数，对任意现实 n 几乎是常数。
2. `_nbrs` 始终是若干不相交的**循环链表**，每条链表对应一个集合，故 `subset(x)` 只需 \(O(\text{集合大小})\)。

#### 4.1.3 源码精读

类的导入与重新导出，注意它在 `__all__` 里与算法函数并列：

[hierarchy/_hierarchy_impl.py:L47-L47](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L47-L47) —— 从 `scipy._lib._disjoint_set` 导入 `DisjointSet`。

[hierarchy/_hierarchy_impl.py:L55-L55](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L55-L55) —— 把 `DisjointSet` 放进 `__all__`，成为 hierarchy 的公开名字。

`__init__` 建立四张字典的初始状态，`_nbrs` 是循环链表、`_indices` 记录插入顺序（既用于 `__iter__` 也用于 merge 的平局打破）：

[scipy/_lib/_disjoint_set.py:L96-L106](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_disjoint_set.py#L96-L106)

find 用**路径折半**：循环里把 `x` 的父亲直接改成祖父，再上移，直到 `x` 的序号等于其父亲的序号（即到根）：

[scipy/_lib/_disjoint_set.py:L137-L142](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_disjoint_set.py#L137-L142)

merge 的核心是按大小合并 + 平局打破。注意第 184 行那个**元组比较**：先比 `sizes`（大者当父），等大时比 `_indices`（先插入者当父），这正是 docstring 里「equal size 选先插入者为父」的实现：

[scipy/_lib/_disjoint_set.py:L178-L190](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_disjoint_set.py#L178-L190)

> 读懂第 188 行 `self._nbrs[xr], self._nbrs[yr] = self._nbrs[yr], self._nbrs[xr]`：这是把两条循环链表「交叉拼接」成一条的经典两步交换，让 `subset(xr)` 能绕到原 `yr` 集合的所有元素。

`connected` 复用 find 比较根的序号是否相等（用 `_indices` 而非元素本身比较，可处理「不可比较 / 哈希但无序」的元素）：

[scipy/_lib/_disjoint_set.py:L205-L205](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_disjoint_set.py#L205-L205)

`subset` 顺着 `_nbrs` 循环链表绕一圈收集成员：

[scipy/_lib/_disjoint_set.py:L223-L228](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_disjoint_set.py#L223-L228)

#### 4.1.4 代码实践

**实践目标**：用 `DisjointSet` 模拟一次并查过程，验证「按大小合并」与「先插入者打破平局」两条规则。

**操作步骤**（可直接在 Python REPL 运行）：

```python
from scipy.cluster.hierarchy import DisjointSet

ds = DisjointSet(['a', 'b', 'c', 'd', 'e'])   # 5 个单元素集
print(ds.n_subsets)                            # 5

ds.merge('a', 'b')   # {a,b}：等大，先插入的 'a' 当根
ds.merge('c', 'd')   # {c,d}：等大，先插入的 'c' 当根
ds.merge('b', 'c')   # 合并 {a,b} 与 {c,d}：都是 size=2，先插入根 'a' 当根
ds.merge('d', 'e')   # 把 'e' 并入大集合

print(ds['e'])              # 'a'：find('e') 走路径折半后落到根 'a'
print(ds.n_subsets)         # 1
print(ds.subset('a'))       # {'a','b','c','d','e'}
print(ds.subsets())         # [{'a','b','c','d','e'}]
```

**需要观察的现象**：

1. 每次成功的 `merge` 返回 `True`，重复合并同一集合返回 `False`。
2. `ds['e']` 恰好返回 `'a'`——因为三次合并都是「等大」，每次都选了先插入的根，最终根是最早插入的 `'a'`。
3. `n_subsets` 从 5 单调递减到 1。

**预期结果**：上面三处 `print` 依次输出 `5`、`'a'`、`1`、`{'a', 'b', 'c', 'd', 'e'}`、`[{'a', 'b', 'c', 'd', 'e'}]`。集合元素的打印顺序可能因循环链表拼接而不同，但成员齐全即可。

#### 4.1.5 小练习与答案

**练习 1**：如果把上面实践里的初始顺序改成 `DisjointSet(['e','d','c','b','a'])`，最终 `ds['a']` 会是谁？为什么？

> **答案**：是 `'e'`。因为元素插入顺序反过来了，所有「等大平局」都改为选最先插入的 `'e'` 当根。

**练习 2**：`subset_size(x)` 为什么比 `len(subset(x))` 快？

> **答案**：`subset_size` 直接读 `_sizes[find(x)]` 这个维护好的字段（\(O(\alpha(n))\)），不需要沿 `_nbrs` 链表枚举全部成员（\(O(\text{集合大小})\)）。源码见 [scipy/_lib/_disjoint_set.py:L247-L247](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_disjoint_set.py#L247-L247)。

---

### 4.2 leaders：从扁平标签反查层次树代表节点

#### 4.2.1 概念说明

`leaders(Z, T)` 回答一个「反向」问题：给定一棵层次树 `Z` 和用 `fcluster` 切出的扁平标签 `T`，每个扁平簇在树里的**代表节点（leader）**是哪个？

leader 的定义（来自 docstring）：对扁平簇 \(j\)，leader 是树中满足下列两条件的**最低**节点 \(i\)：

1. \(i\) 的所有叶子后代都属于扁平簇 \(j\)（子树是「单色」的）；
2. 不存在 \(i\) 之外的、也属于簇 \(j\) 的叶子（即 \(i\) 恰好覆盖簇 \(j\) 的全部叶子）。

直观地说：沿着树自顶向下走，每个扁平簇对应一片「单色子树」，leader 就是这片单色子树的**根**。它返回两个并列数组 `L` 和 `M`：`L[k]=i` 是第 k 个 leader 的节点编号，`M[k]=j` 是它代表的扁平簇号。这样即使 `T` 里的簇号是任意整数（不连续、不从 1 起），也能正确对上。

#### 4.2.2 核心流程

后端算法（Cython）用**显式栈模拟的后序 DFS**，维护一个长度 \(2n-1\) 的 `cluster_ids` 数组：

1. `cluster_ids[0:n] = T`：每个**叶子**直接继承它的扁平簇号。
2. `cluster_ids[n:] = -1`：所有**内部节点**先标「未知」。
3. 从根 `2n-2` 出发后序遍历；对每个内部节点，取左右孩子 `i_lc`、`i_rc` 的 `cid`：
   - 若 `cid_lc == cid_rc`（且非 -1）：两孩子在同一扁平簇 ⇒ 本节点仍在该单色子树内，向上传播 `cluster_ids[root] = cid_lc`。
   - 若 `cid_lc != cid_rc`：本节点是「分叉点」，两孩子分属不同簇 ⇒ 每个非 -1 的孩子就是一个 leader（它是其所在单色子树的根），记入 `L`/`M`，本节点标 -1（跨簇）。
4. 根节点单独处理：若根的两孩子同簇且非 -1，则根本身是 leader。
5. 若过程中 leader 数超过 `nc`（`T` 里的不同簇数），说明 `T` 与 `Z` 不一致，返回出错节点号供 Python 层抛异常。

关键不变量：**leader 恰好是「同簇上传播、异簇处分叉」的分叉点的孩子**，等价于「以它为根的子树单色、但其父不再单色」。

#### 4.2.3 源码精读

Python 层 `leaders` 做校验、算簇数 `nc`、把工作委托给内嵌闭包 `cy_leaders`，最后用 `xpx.lazy_apply` 桥接（与 u3-l3 的 `linkage` 同构）：

[hierarchy/_hierarchy_impl.py:L4210-L4214](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L4210-L4214) —— 校验 `Z` 合法、`T` 必须是 int32、且长度 \(= n\)。

[hierarchy/_hierarchy_impl.py:L4218-L4228](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L4218-L4228) —— `cy_leaders` 闭包：用 `xpx.nunique(T)` 算出簇数 `n_clusters`，分配 `L`/`M`，调 Cython `_hierarchy.leaders`；若返回值 `s >= 0` 表示 `T` 非法，抛 `ValueError`。

Cython 后端的核心循环——注意它**没有用 `DisjointSet`**，而是用 `cluster_ids` 数组直接做「簇号传播」：

[hierarchy/_hierarchy.pyx:L622-L624](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L622-L624) —— 初始化：叶子继承 `T`，内部节点标 -1，栈顶压入根。

[hierarchy/_hierarchy.pyx:L632-L642](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L632-L642) —— DFS 下降：优先深入未访问的非叶孩子。

[hierarchy/_hierarchy.pyx:L644-L668](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L644-L668) —— 后序归约：同簇则传播、异簇则记 leader，是整个算法的「决策核心」。

[hierarchy/_hierarchy.pyx:L670-L681](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L670-L681) —— 真正的树根特判：若两孩子同簇且非 -1，根本身是 leader。

#### 4.2.4 代码实践

**实践目标**：跑通 docstring 里的标准示例，亲手验证 `L`/`M` 与 `Z`、`T` 的对应关系。

**操作步骤**：

```python
from scipy.cluster.hierarchy import ward, fcluster, leaders
from scipy.spatial.distance import pdist

X = [[0, 0], [0, 1], [1, 0],
     [0, 4], [0, 3], [1, 4],
     [4, 0], [3, 0], [4, 1],
     [4, 4], [3, 4], [4, 3]]

Z = ward(pdist(X))                 # n=12, Z 是 11x4
T = fcluster(Z, 3, criterion='distance')   # 切成 4 个扁平簇
print(T)                           # [1 1 1 2 2 2 3 3 3 4 4 4]

L, M = leaders(Z, T)
print(L)                           # [16 17 18 19]
print(M)                           # [1 2 3 4]
```

**需要观察的现象**：

1. `T` 把 12 个点分成 4 个扁平簇，每簇 3 个点。
2. `L=[16,17,18,19]`：由于 \(n=12\)，节点 12~22 对应 `Z` 的 11 行；16、17、18、19 都是内部节点，说明每个 leader 都是「3 叶单色子树」的根（而非单个叶子）。
3. `M=[1,2,3,4]` 与 `T` 里的 4 个簇号一一对应。

**预期结果**：`L` 为 `[16, 17, 18, 19]`，`M` 为 `[1, 2, 3, 4]`（dtype 均为 int32），与 docstring 一致。若 `T` 与 `Z` 不一致（例如手动改坏 `T`），会抛 `ValueError: T is not a valid assignment vector ...`。

#### 4.2.5 小练习与答案

**练习 1**：在上例里，为什么 leader 都是内部节点（编号 \(\geq n=12\)）而不是叶子？什么情况下 leader 会是叶子？

> **答案**：因为 `fcluster(Z, 3, criterion='distance')` 切得较粗，每个扁平簇包含 3 个点，单色子树的根必然是合并这 3 个点的内部节点。当一个扁平簇**只含 1 个点**时，该点本身（叶子，编号 \(< n\)）就是 leader。

**练习 2**：Cython 后端用 `cluster_ids` 数组传播簇号，而不用 `DisjointSet`。给出一个理由说明这里用数组更合适。

> **答案**：这里的「合并关系」完全由**固定的树结构 `Z`** 决定，元素是 \(0\dots 2n-2\) 的连续整数、合并顺序已知（后序），用稠密数组 `cluster_ids` 下标访问是 \(O(1)\) 且缓存友好；`DisjointSet` 的优势在于**动态、任意哈希元素、按需合并**，本场景用不上这些能力，反而会引入额外的字典与路径压缩开销。

---

### 4.3 is_isomorphic：判断两组扁平标签是否同构

#### 4.3.1 概念说明

`is_isomorphic(T1, T2)` 判断两个长度相同的扁平标签数组是否**代表同一个划分**——即只是簇号不同、分组完全一致。形式化地说：

\[
\text{isomorphic}(T_1, T_2) \iff \big(\forall i,j:\ T_1[i]=T_1[j] \Leftrightarrow T_2[i]=T_2[j]\big)
\]

也就是说，「在 T1 里同簇的两点，在 T2 里也必须同簇；反之亦然」。簇号本身的具体数值无所谓——`[1,1,2]` 与 `[7,7,3]` 同构，但 `[1,2,2]` 与 `[1,1,2]` 不同构。

典型用途：用两种 linkage 方法（如 `single` 与 `complete`）聚类同一数据，想确认它们切出的扁平结构是否一致，而不在乎簇号怎么编。

#### 4.3.2 核心流程

朴素做法是 \(O(n^2)\) 两两比较。SciPy 用了一个 \(O(n\log n)\) 的排序技巧：

1. 取 `idx = argsort(T1)`，按 T1 的值对观测排序。
2. 用同一个 `idx` 重排 T1、T2。重排后 T1 **单调不减**，相同 T1 值连续成段。
3. 计算「值变化边界」：`changes1 = (T1 != roll(T1, -1))`，`changes2 = (T2 != roll(T2, -1))`，即在哪些位置上「当前值 ≠ 下一个值」。
4. 若 `changes1` 与 `changes2` 完全相等，则同构。

**为什么成立**：重排后 T1 连续分段。若 T1、T2 同构，则 T2 在这个顺序下也必须**恰好**在同样的位置分段（同一段内 T2 全等、跨段 T2 必变）。反过来，若两边的「变化边界」逐位相同，说明分段结构一致，正是同构。`roll(..., -1)` 引入的「末位 vs 首位」比较，恰好把最后一段的边界也纳入比较，使得单簇（全相等）与多簇情形都自洽。

#### 4.3.3 源码精读

入口与校验，注意它只要求一维、同长，对 dtype 不限：

[hierarchy/_hierarchy_impl.py:L3769-L3778](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L3769-L3778) —— 取数组命名空间、规整，校验一维与等长。

核心算法只有 6 行，注释点明了它是 \(O(n\log n)\) 并提到存在更慢的 `unique_all` 替代：

[hierarchy/_hierarchy_impl.py:L3786-L3794](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L3786-L3794) —— 排序 + 重排 + 比较变化边界，最后 `all(changes1 == changes2)` 给出结论。

> 注意 `is_isomorphic` 同样**没有用 `DisjointSet`**：它把「等价类」问题转化成了「排序后比较边界」的数组问题，省掉了并查集的常数。这是「选对数据表示让问题变简单」的典型例子。

#### 4.3.4 代码实践

**实践目标**：自己用 numpy 复现 `is_isomorphic` 的算法，并与官方实现逐例比对。

**操作步骤**：

```python
import numpy as np
from scipy.cluster.hierarchy import is_isomorphic

def my_is_isomorphic(T1, T2):
    T1 = np.asarray(T1)
    T2 = np.asarray(T2)
    if T1.shape != T2.shape:
        return False
    idx = np.argsort(T1, kind='mergesort')   # 稳定排序，与 xp 行为接近
    T1 = T1[idx]
    T2 = T2[idx]
    c1 = T1 != np.roll(T1, -1)
    c2 = T2 != np.roll(T2, -1)
    return bool(np.all(c1 == c2))

cases = [
    ([1, 1, 1], [2, 2, 2], True),     # 同构：都单簇
    ([1, 7, 1], [2, 3, 2], True),     # 同构：中间一个、两边一个
    ([1, 2, 3, 3], [1, 3, 2, 3], False),  # 不同构
    ([1, 2, 3], [1, 3, 2], True),     # 同构：三个单点簇
    ([1, 2, 3], [1, 1, 1], False),    # 不同构（gh-6271 回归用例）
]
for a, b, expect in cases:
    got = my_is_isomorphic(a, b)
    ref = is_isomorphic(a, b)
    print(a, b, "mine=", got, "scipy=", ref, "OK" if got == ref == expect else "MISMATCH")
```

**需要观察的现象**：每一行都应打印 `OK`，即自定义实现与 `scipy.cluster.hierarchy.is_isomorphic` 完全一致，且与人工预期相符。

**预期结果**：5 行全部 `OK`。特别注意第 5 个用例 `[1,2,3]` vs `[1,1,1]`（点数相同但一个三簇、一个单簇）返回 `False`，这正是回归测试 `test_is_isomorphic_7` 守护的 gh-6271 场景。

#### 4.3.5 小练习与答案

**练习 1**：`is_isomorphic([1,2,3], [1,2,3])` 与 `is_isomorphic([1,2,3], [3,2,1])` 各返回什么？为什么后者也成立？

> **答案**：都返回 `True`。`[1,2,3]` 表示三个单点簇；`[3,2,1]` 也是三个单点簇，只是簇号换了，划分完全一致（每个观测自成一类），故同构。

**练习 2**：把 `roll(T1, -1)` 改成 `roll(T1, 1)` 会影响结果吗？

> **答案**：不影响。`roll(T1, 1)` 比较 `T1[i]` 与 `T1[i-1]`，与 `roll(T1, -1)` 比较 `T1[i]` 与 `T1[i+1]` 只是「变化边界」的位置整体平移了一位；由于 T1、T2 用同一种 roll，`changes1 == changes2` 的结论不变。源码选 `-1` 是惯例。

---

## 5. 综合实践

把三个最小模块串起来，完成 spec 要求的综合任务：**用 `DisjointSet` 模拟并查过程（参考 `leaders` 的「簇合并」直觉），再实现一个判断两组标签是否同构的小函数，与官方 `is_isomorphic` 对比验证**。

```python
import numpy as np
from scipy.cluster.hierarchy import DisjointSet, is_isomorphic, linkage, fcluster
from scipy.spatial.distance import pdist

# ---- 第 1 步：用 DisjointSet 「自底向上合并」模拟一次层次式的簇合并 ----
# 6 个观测，假设我们按某种顺序得到这些「应合并」的对：
n = 6
ds = DisjointSet(range(n))
merge_pairs = [(0, 1), (1, 2), (3, 4), (4, 5)]   # {0,1,2} 与 {3,4,5}
for a, b in merge_pairs:
    ds.merge(a, b)

# 用 find 给每个观测打上「代表元」作为自定义标签
T_custom = np.int32([ds[i] for i in range(n)])
print("DisjointSet 产生的标签:", T_custom)        # 例如 [0 0 0 3 3 3]

# ---- 第 2 步：用真实 linkage+fcluster 产生另一组标签 ----
X = np.array([[0., 0.], [0., 1.], [1., 0.],
              [9., 9.], [9., 10.], [10., 9.]])
Z = linkage(pdist(X), method='single')
T_fcluster = fcluster(Z, 2, criterion='maxclust')   # 切成 2 簇
print("fcluster 产生的标签:    ", T_fcluster)       # 例如 [1 1 1 2 2 2]

# ---- 第 3 步：两种标签的簇号完全不同，但「划分」应一致 ⇒ 同构 ----
print("is_isomorphic 官方结果: ", is_isomorphic(T_custom, T_fcluster))   # True
```

**需要观察的现象与解释**：

1. `T_custom` 的具体数值取决于 `DisjointSet` 的「按大小 + 先插入者」规则，通常形如 `[0 0 0 3 3 3]`（前三个落到根 0，后三个落到根 3）。
2. `T_fcluster` 从 1 起编号，形如 `[1 1 1 2 2 2]`。
3. 两者簇号毫无关系，但都把观测 `{0,1,2}` 归一组、`{3,4,5}` 归另一组 ⇒ 划分相同 ⇒ `is_isomorphic` 返回 `True`。
4. 这恰好呼应了本讲的核心结论：`DisjointSet` 给你「构造划分」的能力，`leaders`/`is_isomorphic` 给你「校验 / 比较划分」的能力，三者共同构成一套围绕「聚类等价性」的工具——但它们在 hierarchy 内部**各走各的实现**，并不互相调用。

> 若想进一步验证 `leaders`：在第二步后加一行 `L, M = leaders(Z, T_fcluster)`，观察 `L` 里的节点编号是否落在 `Z` 行数范围内（\(n=6\) ⇒ 内部节点 6~10），并理解每个 leader 对应一片单色子树。

## 6. 本讲小结

- `DisjointSet` 是 hierarchy 从 `scipy._lib` 重新导出的**通用并查集**：`find`（路径折半）+ `merge`（按大小合并、等大取先插入者为父）让单次操作近似 \(O(\alpha(n))\)；`subset` 靠 `_nbrs` 循环链表 \(O(\text{size})\) 枚举成员。
- `leaders(Z, T)` 用 `cluster_ids` 数组做一遍后序 DFS：叶子继承 `T`、同簇向上传播、异簇处分叉记 leader，返回 `L`（leader 节点号）与 `M`（对应扁平簇号）；`T` 与 `Z` 不一致时会抛 `ValueError`。
- `is_isomorphic(T1, T2)` 把「等价类」问题转为「按 T1 排序后比较值变化边界」的 \(O(n\log n)\) 数组问题，无需并查集即可判断两组标签是否同构。
- **三者概念同源（聚类等价性）、实现独立**：`leaders` 与 `is_isomorphic` 都**没有**内部调用 `DisjointSet`，而是各选了最贴合数据形态（树 / 一维数组）的专用算法——这是「数据表示决定算法效率」的范例。
- 工程上三者都接入了 array API：`leaders` 用 `lazy_cython`+`lazy_apply` 桥接（cpu_only、对 dask 合并分块告警），`is_isomorphic` 是纯数组运算、天然跨后端。

## 7. 下一步学习建议

- **u7-l3（array API 兼容、惰性数组与测试体系）**：本讲多次出现的 `xp_capabilities`/`lazy_apply`/`xpx.nunique` 会在那里系统讲解，并带你读 `test_disjoint_set.py`、`TestLeaders`、`TestIsIsomorphic` 的测试组织。
- 若想看 hierarchy 里**唯一真正在算法内部使用并查集**的地方，可回看 u4-l1 的 `LinkageUnionFind`（`_structures.pxi`），对比它与通用 `DisjointSet` 在「合并时新建父节点 + 路径压缩」上的设计差异。
- 延伸阅读：对照 MATLAB 的 `clusterdata`/`cluster` 体系，理解 SciPy hierarchy 的 `fcluster`/`leaders`/`is_isomorphic` 命名与语义渊源。
