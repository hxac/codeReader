# cophenetic 距离与 cophenetic 相关系数

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 **cophenetic 距离** 的定义：它就是两个原始观测在层次树上「首次被并入同一簇」时所在的合并高度。
- 看懂 `scipy.cluster.hierarchy.cophenet` 这个公开入口的双层结构：Python 校验/桥接层如何把请求转发到 Cython 后端 `cophenetic_distances`，以及它在两种返回形态（只给距离向量、或额外给出相关系数）之间的分支。
- 读懂 Cython 后端 `cophenetic_distances` 的核心算法：用「显式栈 + 位图 visited + 叶子名册 members」做一遍后序遍历，按簇对填充压缩距离向量。
- 掌握 **cophenetic 相关系数** c 的数学含义：它衡量一棵层次树对原始两两距离的「保真度」，并能动手比较不同 linkage 方法的保真度高低。

## 2. 前置知识

本讲假定你已经学过 **u3-l1（linkage matrix 数据结构）**。这里快速回顾两个要点：

- **linkage matrix Z**：形状恒为 \((n-1)\times 4\)，四列依次是「被合并的两个簇编号 / 合并距离 / 新簇包含的原始观测数」。原始观测编号为 \(0\dots n-1\)，第 \(i\) 步（从 0 起）合并出的新簇编号为 \(n+i\)，整棵树共 \(2n-1\) 个节点，根节点编号为 \(2n-2\)。
- **压缩距离矩阵（condensed matrix）**：即 `scipy.spatial.distance.pdist` 的输出，把 \(n\times n\) 对称距离矩阵的上三角「按行拍平」成长度为 \(n(n-1)/2\) 的一维向量。簇对 \((i,j),\,i<j\) 在其中对应的下标由公式 `condensed_index(n, i, j) = n*i - i*(i+1)/2 + (j-i-1)` 计算（见 [u4-l1](u4-l1-condensed-matrix-and-heap.md)）。

本讲还会用到两个已经在前面讲义出现过的「桥接」概念（详见 [u3-l3](u3-l3-python-cython-bridge.md)）：

- `@lazy_cython`：模块级别名 `xp_capabilities(cpu_only=True, reason="Cython code", warnings=[("dask.array", "merges chunks")])`，声明「这段是 Cython 代码、只在 CPU 跑、对 dask 输入会合并分块并告警」。
- `xpx.lazy_apply`：桥接总开关——普通（eager）NumPy 数组走快路径直接调用闭包；dask 等惰性数组则把闭包注册进任务图。

最后两个名词：

- **LCA（Lowest Common Ancestor，最近公共祖先）**：在树里两个叶子「最低的」那个共同祖先节点。
- **Pearson 相关系数**：衡量两组数据线性相关程度的指标，取值 \([-1,1]\)，越接近 1 表示两组数据越「同起同落」。

## 3. 本讲源码地图

| 文件 | 关键符号 | 作用 |
| --- | --- | --- |
| [hierarchy/_hierarchy_impl.py:1479-1619](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L1479-L1619) | `cophenet` | 公开入口：校验 Z、桥接到 Cython 算距离向量；若额外传入原始距离矩阵 `Y`，则计算 cophenetic 相关系数 c。 |
| [hierarchy/_hierarchy.pyx:306-378](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L306-L378) | `cophenetic_distances` | Cython 后端：后序遍历整棵树，把每对叶子的 cophenetic 距离按位写入压缩向量 `d`。 |
| [hierarchy/_hierarchy.pyx:20-43](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L20-L43) | `condensed_index` / `is_visited` / `set_visited` | 后端依赖的三个小工具：压缩下标换算 + 位图访问。 |
| [hierarchy/_hierarchy_impl.py:83-85](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L83-L85) | `lazy_cython` | 装饰 `cophenet` 的能力约束别名。 |

调用链一句话总结：

```
cophenet(Z[, Y])  ──lazy_apply──▶  cy_cophenet  ──▶  _hierarchy.cophenetic_distances(Z, d, n)
   (Python 层)        (桥接)          (内嵌闭包)              (Cython 后序遍历)
                                                              └─ 用 condensed_index 写入 d
```

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：先讲清 cophenetic 距离「是什么」（4.1），再讲 Python 入口与桥接（4.2），最后下沉到 Cython 后端的遍历算法（4.3）。

### 4.1 cophenetic 距离：定义、直觉与数学

#### 4.1.1 概念说明

层次聚类把 \(n\) 个观测逐层合并成一棵二叉树。这棵树「压扁」了原始的两两距离：原本每对观测 \((i,j)\) 都有一个真实距离 \(Y_{ij}\)，而在树上，这对观测只在某一个特定的合并步骤里「首次相遇」。

> **cophenetic 距离** \(d_{ij}\)：观测 \(i\) 与 \(j\) 在层次树上的 cophenetic 距离，就是它们**首次被并入同一簇**时那次合并的距离（即 Z 矩阵第三列的值）。

用树的术语说，\(d_{ij}\) 等于叶子 \(i\) 与叶子 \(j\) 的 **LCA 节点的合并高度** \(Z[\text{LCA}, 2]\)。这正是 `cophenet` docstring 里那句拗口的描述——「设 \(p,q\) 分别属于不相交簇 \(s,t\)，而 \(s,t\) 被一个直接父簇 \(u\) 合并，则 \(p,q\) 的 cophenetic 距离就是 \(s,t\) 之间的距离」（见 [hierarchy/_hierarchy_impl.py:1485-1490](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L1485-L1490)）。

为什么需要它？因为层次树是一种「有损压缩」的距离表示：它只用 \(n-1\) 个合并高度，就要复现 \(\binom{n}{2}\) 个两两距离。cophenetic 距离就是「树复原出来的那版距离」。拿它和原始距离 \(Y\) 一比，就能知道这棵树「失真」了多少——这就是下一节的相关系数要做的事。

#### 4.1.2 核心流程

给定一棵合法的层次树，计算全部 cophenetic 距离的思路：

1. 对树中每一个**内部节点** \(u\)（共 \(n-1\) 个），它有两个子树：左子树叶子集合 \(L\)、右子树叶子集合 \(R\)。
2. 对任意 \(i\in L,\,j\in R\)，节点 \(u\) 正是 \(i,j\) 的 LCA（因为 \(u\) 是「左子树里所有点」和「右子树里所有点」首次相遇的地方）。
3. 于是把 \(d_{ij} \leftarrow Z[u,2]\)（\(u\) 的合并高度）。
4. 由于每一对叶子有且仅有一个 LCA，所有 \(\binom{n}{2}\) 个距离恰好各被填一次，无重复无遗漏。

这等价于一遍**后序遍历**：先把左右子树的叶子名单收集齐，再在当前节点处一次性「跨左右」填入所有对的距离。Cython 后端正是这么做的（见 4.3）。

#### 4.1.3 源码精读

cophenetic 距离的定义写在 `cophenet` 的 docstring 开头（这一段是权威定义）：

```python
# hierarchy/_hierarchy_impl.py:1485-1490
# 假设 p、q 分别属于不相交簇 s、t，而 s、t 被直接父簇 u 合并，
# 则观测 i、j 的 cophenetic 距离就是 s、t 之间的距离。
```

docstring 里的官方示例最能说明直觉：12 个点排成 3×4 的「角落」图案，`single` 聚类后，**同一角落内**的点 cophenetic 距离为 1（很早合并），**跨角落**的点 cophenetic 距离为 2（要等到两个角落簇合并时才相遇），见 [hierarchy/_hierarchy_impl.py:1581-1584](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L1581-L1584)。

#### 4.1.4 代码实践

**实践目标**：用 4 个观测手工算出全部 cophenetic 距离，验证它就是「LCA 合并高度」。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.cluster.hierarchy import linkage, cophenet
from scipy.spatial.distance import pdist

X = np.array([[0., 0.], [0., 1.], [5., 5.], [6., 5.]])
Z = linkage(X, method='single')
print(Z)
# 第 0 行：把点 0、1 在距离 1.0 处合并成新簇 4
# 第 1 行：把点 2、3 在距离 1.0 处合并成新簇 5
# 第 2 行：把簇 4、5 在距离 ~7.07 处合并成根 6
print(cophenet(Z))
```

**手工推算**（4 个点 \(\{0,1,2,3\}\)，压缩向量顺序为 (0,1),(0,2),(0,3),(1,2),(1,3),(2,3)）：

| 对 (i,j) | LCA 节点 | 合并高度 \(d_{ij}\) |
| --- | --- | --- |
| (0,1) | 新簇 4 | 1.0 |
| (2,3) | 新簇 5 | 1.0 |
| (0,2),(0,3),(1,2),(1,3) | 根 6 | ~7.07 |

**预期结果**：`cophenet(Z)` 返回 `[1.0, 7.07..., 7.07..., 7.07..., 7.07..., 1.0]`，与上表逐一对应。（具体的小数末位**待本地验证**，取决于 linkage 实际算出的根距离。）

#### 4.1.5 小练习与答案

**练习 1**：cophenetic 距离满足三角不等式吗？为什么 single 方法下它恰好等于「树上两叶之间路径的最大边权」？

**答案**：不一定满足。cophenetic 距离是「合并高度」而非真实距离，只有在距离矩阵本身满足超度量（ultrametric）条件时（即对所有 \(i,j,k\) 有 \(d_{ij}\le\max(d_{ik},d_{jk})\)）才满足强三角不等式。single 方法的 cophenetic 距离正是最小生成树上两叶路径的最大边权（见 [u4-l2](u4-l2-mst-single-linkage.md)），因此 single 下的 cophenetic 距离构成超度量。

**练习 2**：把 `cophenet(Z)` 的输出用 `scipy.spatial.distance.squareform` 转成方阵后，对角线是什么？

**答案**：对角线为 0（自己到自己的 cophenetic 距离无定义，squareform 约定置 0），其余 \(ij\) 元素就是 \(d_{ij}\)。

---

### 4.2 cophenet：Python 入口、惰性桥接与相关系数

#### 4.2.1 概念说明

`cophenet` 是面向用户的公开入口，它做两件相对独立的事：

1. **算 cophenetic 距离向量**：输入 Z，输出长度为 \(n(n-1)/2\) 的压缩向量 `zz`。这是它「永远会做」的核心工作，由 Cython 后端 `cophenetic_distances` 完成。
2. **（可选）算 cophenetic 相关系数 c**：只有当用户额外传入原始压缩距离矩阵 `Y` 时，才计算 `zz` 与 `Y` 的 Pearson 相关系数，返回 `(c, zz)`；否则只返回 `zz`。

因此它的返回类型随 `Y` 而变：不传 `Y` 返回 ndarray，传 `Y` 返回 tuple。这是它最容易踩的坑，docstring 的 Returns 段也专门强调了（见 [hierarchy/_hierarchy_impl.py:1505-1511](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L1505-L1511)）。

#### 4.2.2 核心流程

`cophenet` 的执行流程：

1. 用 `array_namespace(Z, Y)` 拿到数组后端命名空间 `xp`，把 Z 规整成 `float64` C 连续数组，并用 `_is_valid_linkage` 校验合法性（非法 Z 直接抛异常）。
2. 定义内嵌闭包 `cy_cophenet(Z, validate)`：分配长度 \(n(n-1)/2\) 的输出向量 `zz`，调用 `_hierarchy.cophenetic_distances(Z, zz, n)` 填充它。
3. 用 `xpx.lazy_apply` 把闭包应用到 Z——普通 NumPy 数组走快路径直接算，dask 惰性数组则进任务图。
4. 若 `Y is None`：直接返回 `zz`，结束。
5. 若给了 `Y`：校验 `Y` 也是合法压缩距离矩阵，然后按 Pearson 公式算相关系数 c，返回 `(c, zz)`。

第 1–3 步正是 [u3-l3](u3-l3-python-cython-bridge.md) 讲过的「Python↔Cython 桥接」模式在这里的又一次复用。

#### 4.2.3 源码精读

**桥接三件套**。首先，`cophenet` 头上挂着 `@lazy_cython`，也就是模块级别名 `xp_capabilities(cpu_only=True, ...)`，声明它是 CPU-only 的 Cython 代码：

```python
# hierarchy/_hierarchy_impl.py:83-85
lazy_cython = xp_capabilities(
    cpu_only=True, reason="Cython code",
    warnings=[("dask.array", "merges chunks")])
```

函数体先规整并校验 Z（`_asarray` 强制 `float64` C 连续，因为「Cython 代码不处理 stride」，见注释）：

```python
# hierarchy/_hierarchy_impl.py:1587-1590
xp = array_namespace(Z, Y)
# Ensure float64 C-contiguous array. Cython code doesn't deal with striding.
Z = _asarray(Z, order='C', dtype=xp.float64, xp=xp)
_is_valid_linkage(Z, throw=True, name='Z', xp=xp)
```

接着是核心闭包 `cy_cophenet`，它捕获外层的校验逻辑，把 Cython 调用包成统一的 `(数据, validate)` 形态——这与 `linkage` 里的 `cy_linkage`（[u3-l3](u3-l3-python-cython-bridge.md)）如出一辙：

```python
# hierarchy/_hierarchy_impl.py:1592-1598
def cy_cophenet(Z, validate):
    if validate:
        _is_valid_linkage(Z, throw=True, name='Z', xp=np)
    n = Z.shape[0] + 1
    zz = np.zeros((n * (n-1)) // 2, dtype=np.float64)
    _hierarchy.cophenetic_distances(Z, zz, n)   # 真正干活的是 Cython 后端
    return zz
```

注意 `n = Z.shape[0] + 1`：Z 有 \(n-1\) 行，所以观测数 \(n=\text{行数}+1\)。输出向量长度正是 \(\binom{n}{2}=n(n-1)/2\)。

然后 `xpx.lazy_apply` 负责调度这个闭包：

```python
# hierarchy/_hierarchy_impl.py:1600-1603
n = Z.shape[0] + 1
zz = xpx.lazy_apply(cy_cophenet, Z, validate=is_lazy_array(Z),
                    shape=((n * (n-1)) // 2, ), dtype=xp.float64,
                    as_numpy=True, xp=xp)
```

`as_numpy=True` 表示无论输入是什么后端，输出都规整成 NumPy 数组（cophenetic 距离是「派生量」，没必要保留 JAX/Dask 后端）。

**相关系数分支**。拿到 `zz` 后，按 `Y` 是否存在分两条路：

```python
# hierarchy/_hierarchy_impl.py:1605-1619
if Y is None:
    return zz                              # 只算距离，直接返回

Y = _asarray(Y, order='C', xp=xp)
distance.is_valid_y(Y, throw=True, name='Y')   # 校验 Y 是合法压缩距离矩阵

z = xp.mean(zz)
y = xp.mean(Y)
Yy = Y - y
Zz = zz - z
numerator = (Yy * Zz)
denomA = Yy**2
denomB = Zz**2
c = xp.sum(numerator) / xp.sqrt(xp.sum(denomA) * xp.sum(denomB))
return (c, zz)
```

最后那段就是手写的 Pearson 相关系数（见 4.4 的公式）。`distance.is_valid_y` 来自 `import scipy.spatial.distance as distance`（[hierarchy/_hierarchy_impl.py:44](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L44)），用于确认 `Y` 长度确实是 \(\binom{n}{2}\)。

#### 4.2.4 代码实践

**实践目标**：观察 `cophenet` 两种返回形态，并手算一遍相关系数，确认它就是 Pearson。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.cluster.hierarchy import linkage, cophenet
from scipy.spatial.distance import pdist

rng = np.random.default_rng(0)
X = rng.standard_normal((20, 2))
Y = pdist(X)                  # 原始压缩距离矩阵
Z = linkage(Y, method='average')

# 形态一：只给 Z，返回距离向量
zz = cophenet(Z)
print(type(zz), zz.shape)     # <class 'numpy.ndarray'> (190,)

# 形态二：给 Z 和 Y，返回 (c, zz)
c, zz2 = cophenet(Z, Y)
print(c)                      # 一个标量，越接近 1 保真度越高

# 手算 Pearson，验证与 c 完全一致
zz = zz.astype(float); Yf = Y.astype(float)
num = np.sum((Yf - Yf.mean()) * (zz - zz.mean()))
den = np.sqrt(np.sum((Yf - Yf.mean())**2) * np.sum((zz - zz.mean())**2))
print(num / den, c)           # 两个数应当相等
```

**预期结果**：最后一行打印的两个数相等，证明 `c` 就是原始距离 `Y` 与 cophenetic 距离 `zz` 之间的 Pearson 相关系数。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `cy_cophenet` 里要再调一次 `_is_valid_linkage(Z, ..., xp=np)`？外层不是已经校验过了吗？

**答案**：外层校验发生在「桥接之前」，针对的是用户原始输入（可能是 dask 等惰性数组，用 `xp` 校验）。而 `xpx.lazy_apply` 对惰性输入会把闭包推迟到任务图执行时才调用，那时拿到的是合并分块后的具体数据块；`validate` 标志让闭包在「真正拿到 NumPy 数据块时」再校验一次，防止用户事后传入的 Z 块非法导致 Cython 越界（这正是 gh-22183 修的坑，见 4.3.3）。

**练习 2**：若调用 `cophenet(Z, Y)` 但 `Y` 的长度与 `n(n-1)/2` 不符，会发生什么？

**答案**：`distance.is_valid_y(Y, throw=True, name='Y')`（[hierarchy/_hierarchy_impl.py:1609](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L1609)）会抛 `ValueError`，因为 `Y` 不是合法的压缩距离矩阵。

---

### 4.3 cophenetic_distances：Cython 后序遍历后端

#### 4.3.1 概念说明

`_hierarchy.cophenetic_distances` 是真正干活的 Cython 内核。它接收已规整的 Z、一个预分配的输出向量 `d`（长度 \(n(n-1)/2\)）和观测数 `n`，原地填充 `d`。

它的难点在于：要把「树结构」高效地翻译成「一维压缩向量的下标」。它用了一套**迭代式后序遍历**（不递归，避免深树爆栈），核心是用三个数组模拟「调用栈 + 叶子名册」。

#### 4.3.2 核心流程

算法用四个数组协同工作：

- `curr_node[k]`：当作栈用，`k` 是栈顶指针；存「当前正在处理的节点 id」。初始 `curr_node[0] = 2*n-2`（根节点）。
- `left_start[k]`：栈第 k 层那个节点「左子树叶子在 members 里的起始下标」。
- `members[...]`：长度 n 的「叶子名册」，按遍历顺序铺开存放叶子的原始观测号。
- `visited`：长度 \(\lceil(2n-1)/8\rceil\) 的**位图**（bitset），标记哪些内部节点已经下钻过，避免重复访问。

每一层做这样几步（伪代码）：

```
root = curr_node[k] - n            # 当前节点在 Z 中的行号
i_lc, i_rc = Z[root,0], Z[root,1]  # 左右孩子
# 处理左孩子：
若 i_lc 是内部节点 且 未访问 → 标记已访问，push 左孩子，continue（先下钻左子树）
若 i_lc 是叶子             → n_lc=1，把它的观测号写进 members[left_start[k]]
否则（已访问过的内部节点） → n_lc = Z[i_lc-n, 3]（该子树叶子数）
# 处理右孩子：同理，但右子树的 left_start = 左子树 left_start + n_lc
# 当左右子树都已就绪（都不再 continue）：
dist = Z[root, 2]
对 members 中「左子树区间」的每个 i 与「右子树区间」的每个 j：
    d[condensed_index(n, members[i], members[j])] = dist
k -= 1   # 回到父节点
```

关键直觉：`members` 在回溯过程中被「左右拼接」地填满——左子树贡献一段连续区间，右子树紧接其后贡献下一段。当回到某个节点时，它左子树的所有叶子恰好在 `members[left_start[k] : left_start[k]+n_lc]`，右子树叶子恰好在 `members[left_start[k]+n_lc : left_start[k]+n_lc+n_rc]`。于是「跨左右」的双重循环就把「以当前节点为 LCA 的所有叶子对」一次性写好，距离取当前节点的合并高度——这与 4.1.2 的定义完全吻合。

#### 4.3.3 源码精读

先看三个底层小工具。`condensed_index` 把簇对 \((i,j)\) 翻译成压缩向量下标（i、j 谁大都行，函数内自动排序）：

```cython
# hierarchy/_hierarchy.pyx:20-29
cdef inline np.npy_int64 condensed_index(np.npy_int64 n, np.npy_int64 i,
                                         np.npy_int64 j) noexcept:
    if i < j:
        return n * i - (i * (i + 1) / 2) + (j - i - 1)
    elif i > j:
        return n * j - (j * (j + 1) / 2) + (i - j - 1)
```

`is_visited` / `set_visited` 用「字节 + 位掩码」实现位图——节点 i 对应第 `i>>3` 个字节的第 `i&7` 位：

```cython
# hierarchy/_hierarchy.pyx:32-43
cdef inline int is_visited(uchar *bitset, int i) noexcept:
    return bitset[i >> 3] & (1 << (i & 7))

cdef inline void set_visited(uchar *bitset, int i) noexcept:
    bitset[i >> 3] |= 1 << (i & 7)
```

接着是后端主体。先分配输出无关的几个工作数组与位图（位图用 `PyMem_Malloc` 手动分配、`memset` 清零，遍历结束 `PyMem_Free` 释放）：

```cython
# hierarchy/_hierarchy.pyx:319-333
cdef int i, j, k, root, i_lc, i_rc, n_lc, n_rc, right_start
cdef double dist
cdef int[:] curr_node = np.ndarray(n, dtype=np.intc)
cdef int[:] members = np.ndarray(n, dtype=np.intc)
cdef int[:] left_start = np.ndarray(n, dtype=np.intc)

cdef int visited_size = (((n * 2) - 1) >> 3) + 1
cdef uchar *visited = <uchar *>PyMem_Malloc(visited_size)
if not visited:
    raise MemoryError
memset(visited, 0, visited_size)

k = 0
curr_node[0] = 2 * n - 2     # 从根节点出发
left_start[0] = 0
```

主循环里，对左孩子的处理体现了「能下钻就下钻，否则登记/读取叶子数」的逻辑：

```cython
# hierarchy/_hierarchy.pyx:334-349
while k >= 0:
    root = curr_node[k] - n
    i_lc = <int>Z[root, 0]
    i_rc = <int>Z[root, 1]

    if i_lc >= n:                      # 左孩子是内部节点
        n_lc = <int>Z[i_lc - n, 3]
        if not is_visited(visited, i_lc):
            set_visited(visited, i_lc)
            k += 1
            curr_node[k] = i_lc
            left_start[k] = left_start[k - 1]   # 左子树与父节点共用同一起点
            continue                              # 先去下钻左子树
    else:                              # 左孩子是叶子
        n_lc = 1
        members[left_start[k]] = i_lc
```

右孩子同理，但右子树的 `left_start` 要跳过左子树的 `n_lc` 个叶子（[hierarchy/_hierarchy.pyx:352-363](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L352-L363)）。

当左右子树都处理完（不再 `continue`），执行核心的「跨左右」填充。这里有一段重要注释——**非法的 linkage 矩阵会让 `members` 越界**，所以入口处 `_is_valid_linkage` 的校验是安全前提：

```cython
# hierarchy/_hierarchy.pyx:366-376
dist = Z[root, 2]
right_start = left_start[k] + n_lc
# NOTE: an invalid linkage matrix (gh-22183)
# can cause an out of bounds memory access
# of `j` on `members` memoryview below, if not
# caught ahead of time by `is_valid_linkage`
for i in range(left_start[k], right_start):
    for j in range(right_start, right_start + n_rc):
        d[condensed_index(n, members[i], members[j])] = dist

k -= 1   # back to parent node
```

这就是「以当前节点为 LCA 的所有叶子对，距离统一置为当前合并高度」的代码实现。遍历结束释放位图（[hierarchy/_hierarchy.pyx:378](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L378)）。

> 小结：该算法时间复杂度 \(O(n^2)\)（每对叶子恰好写一次），空间 \(O(n)\)（工作数组与位图都线性），且因不递归而对深树安全。

#### 4.3.4 代码实践

**实践目标**：用一个极小的 Z（4 个观测）逐步追踪 `members` 与 `left_start` 的变化，理解后序遍历如何填出压缩向量。

**操作步骤**：仍用 4.1.4 那棵 4 点树（点 0,1 合并成簇 4；点 2,3 合并成簇 5；簇 4,5 合并成根 6）。在纸上模拟：

1. `k=0`，`curr_node[0]=6`（根），`left_start[0]=0`。根的左孩子是簇 4（内部、未访问）→ 标记、push：`k=1`，`curr_node[1]=4`，`left_start[1]=0`，continue。
2. `k=1`，节点 4 的左孩子是叶子 0 → `members[0]=0`，`n_lc=1`；右孩子是叶子 1 → `members[1]=1`，`n_rc=1`。左右就绪：`dist=Z[4-n=0, 2]=1.0`，`right_start=1`，填 `d[condensed_index(4, 0, 1)] = 1.0`。`k` 减回 0。
3. `k=0`，回到根。左孩子簇 4 已访问 → `n_lc=Z[0,3]=2`。右孩子簇 5 未访问 → push：`k=1`，`curr_node[1]=5`，`left_start[1]=left_start[0]+2=2`，continue。
4. `k=1`，节点 5 的左孩子叶子 2 → `members[2]=2`；右孩子叶子 3 → `members[3]=3`。填 `d[condensed_index(4,2,3)] = 1.0`。`k` 减回 0。
5. `k=0`，回到根。右孩子簇 5 已访问 → `n_rc=Z[1,3]=2`。左右就绪：`dist=Z[2,2]`（根距离），`right_start=left_start[0]+2=2`，填 4 个跨对 `(0,2),(0,3),(1,2),(1,3)` 都等于根距离。`k` 减到 -1，结束。

**预期结果**：`members` 最终为 `[0,1,2,3]`，`d` 的 6 个元素 = `[1.0, 根距离, 根距离, 根距离, 根距离, 1.0]`，与 `cophenet(Z)` 输出逐位一致。若你的手工结果与程序不符，请重点检查第 3 步 `left_start[1]=2` 是否漏算了 `+n_lc`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `visited` 用位图（bitset）而不是普通的 `np.ndarray` 布尔数组？

**答案**：位图每个节点只占 1 bit，对 \(2n-1\) 个节点仅需 \(\lceil(2n-1)/8\rceil\) 字节，比布尔数组（每元素 1 字节）省 8 倍内存；且位运算极快。这些 Cython 后端（`cophenetic_distances`、`inconsistent` 等）都用同一套 `PyMem_Malloc`+位掩码方案。

**练习 2**：算法为什么不直接用递归写后序遍历？

**答案**：退化的层次树（例如 single linkage 产生的「长链」树）高度可达 \(O(n)\)，递归会爆 Python/C 栈。改用 `curr_node` 数组模拟显式栈，栈深虽仍是 \(O(n)\) 但落在堆上，安全且省去了函数调用开销。

---

### 4.4 cophenetic 相关系数：衡量聚类保真度

（本节聚焦 [hierarchy/_hierarchy_impl.py:1611-1619](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L1611-L1619) 那段手写的 Pearson 计算。）

#### 4.4.1 概念说明

**cophenetic 相关系数** c 是原始两两距离 \(Y\) 与层次树复出的 cophenetic 距离 \(d\) 之间的 Pearson 相关系数。它回答一个问题：**这棵树在多大程度上忠实保留了原始距离结构？**

\[
c \;=\; \frac{\sum_{i<j}\bigl(Y_{ij}-\bar Y\bigr)\bigl(d_{ij}-\bar d\bigr)}
              {\sqrt{\sum_{i<j}(Y_{ij}-\bar Y)^2}\;\sqrt{\sum_{i<j}(d_{ij}-\bar d)^2}}
\]

取值范围 \([-1,1]\)。c 越接近 1，说明树上「谁和谁更近」的相对顺序与原始距离越一致，聚类保真度越高；c 偏低则说明该方法「扭曲」了距离结构（比如为了别目标——簇内方差——而牺牲了距离保真）。

#### 4.4.2 核心流程

计算只需把 \(Y\) 和 \(zz\)（都是长度 \(\binom n2\) 的压缩向量）做一次 Pearson：各自去均值、求内积作分子、求各自平方和作分母、相除。源码里没用 `np.corrcoef`，而是手动展开，这样能跨数组后端用 `xp`（`xp.mean`、`xp.sum`、`xp.sqrt`）通用表达。

#### 4.4.3 源码精读

```python
# hierarchy/_hierarchy_impl.py:1611-1619
z = xp.mean(zz)            # d 的均值
y = xp.mean(Y)             # Y 的均值
Yy = Y - y                 # Y 去均值
Zz = zz - z                # d 去均值
numerator = (Yy * Zz)      # 逐元素乘（求和前）
denomA = Yy**2             # Y 的平方（求和前）
denomB = Zz**2             # d 的平方（求和前）
c = xp.sum(numerator) / xp.sqrt(xp.sum(denomA) * xp.sum(denomB))
return (c, zz)
```

逐行对应上面的公式：分子 = \(\sum(Y-\bar Y)(d-\bar d)\)，分母 = \(\sqrt{\sum(Y-\bar Y)^2 \cdot \sum(d-\bar d)^2}\)。这正是 Pearson 的定义式。

#### 4.4.4 代码实践

**实践目标**：用 `np.corrcoef` 复算 c，确认源码手写版本与库函数一致。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.cluster.hierarchy import linkage, cophenet
from scipy.spatial.distance import pdist

X = np.random.default_rng(1).standard_normal((30, 2))
Y = pdist(X)
Z = linkage(Y, method='average')
c, zz = cophenet(Z, Y)

# 用 numpy 的 corrcoef 复算
print(c)
print(np.corrcoef(Y, zz)[0, 1])   # 应与 c 几乎完全相等
```

**预期结果**：两个值在浮点误差范围内相等。

#### 4.4.5 小练习与答案

**练习 1**：c 是「有量纲」还是「无量纲」的指标？为什么用相关系数而不是「平均绝对误差」来衡量保真度？

**答案**：c 无量纲（Pearson 消除了量纲和尺度）。用它是因为层次树本身就把距离「离散化」成了若干合并高度，绝对误差会受树高度整体放缩影响，而相关系数只关心相对单调关系——这正是「保真度」想刻画的。

**练习 2**：是否存在 c 很高但聚类「没用」的情况？

**答案**：存在。c 只衡量距离的保真，不衡量簇是否「有意义」。例如 single linkage 常给出很高的 c，却容易产生链式（chaining）簇，实际可用性未必好。c 是诊断指标之一，不能单独作为聚类好坏的判据。

## 5. 综合实践

**任务**：对同一数据集，比较 `single` 与 `ward` 两种方法的 cophenetic 相关系数，判断哪种对原始距离保真度更高，并用 `leaves_list` 与 `dendrogram` 直观解释差异来源。

**操作步骤**：

```python
# 示例代码
import numpy as np
import matplotlib.pyplot as plt
from scipy.cluster.hierarchy import linkage, cophenet, leaves_list, dendrogram
from scipy.spatial.distance import pdist

rng = np.random.default_rng(42)
# 三个略重叠的高斯团，便于观察 chaining 效应
X = np.vstack([rng.standard_normal((40, 2)),
               rng.standard_normal((40, 2)) + [6, 0],
               rng.standard_normal((40, 2)) + [3, 5]])
Y = pdist(X)

for method in ['single', 'ward']:
    Z = linkage(Y if method == 'single' else X, method=method)
    c, _ = cophenet(Z, Y)
    print(f"{method:8s} cophenetic c = {c:.4f}")
    print(f"          leaf order = {leaves_list(Z)[:10]}...")

    # 画 dendrogram 直观比较
    plt.figure()
    dendrogram(Z, no_plot=False, truncate_mode='level', p=5)
    plt.title(f"{method} linkage (c={c:.3f})")
    plt.show()
```

**需要观察的现象**：

1. **c 的数值**：通常 `single` 的 c 明显高于 `ward`（**待本地验证**具体数值，取决于数据）。
2. **dendrogram 形态**：`single` 的树呈「阶梯状/链式」——很多簇在很矮的高度逐个挂接，因为它总是合并最近的两点；`ward` 的树更「平衡」，合并高度随簇增大而显著上升，因为它最小化的是簇内方差增量（Ward 距离），与原始欧氏距离不在一个尺度上。
3. **leaves_list**：`single` 的叶序常把同团点拉成长链；`ward` 的叶序更紧凑地按团分组。

**解释差异来源**：`single` 的 cophenetic 距离 = 最小生成树上两叶路径的最大边权（见 [u4-l2](u4-l2-mst-single-linkage.md)），与「最近邻距离」高度同构，故与原始距离的相对排序吻合得很好，c 偏高；但这也正是它链式合并、产生不均衡簇的根源。`ward` 的合并高度是 Ward 方差增量的开方（见 [u3-l4](u3-l4-lance-williams-update.md)），是一种「重加权」的距离，系统地偏离了原始欧氏距离，故 c 偏低，但换来的是更紧凑、更均衡的簇。

**结论反思**：c 高 ≠ 聚类好。`single` 保真度高却易链式，`ward` 保真度低却簇形好——这正说明 cophenetic 相关系数只是「距离保真」这一个维度的诊断量，需结合实际聚类目标取舍。

## 6. 本讲小结

- **cophenetic 距离** = 两观测在层次树上首次并入同一簇时的合并高度，即它们 LCA 节点的 \(Z[:,2]\)。
- `cophenet(Z)` 返回长度 \(n(n-1)/2\) 的**压缩**距离向量；`cophenet(Z, Y)` 额外返回相关系数，形态变为 `(c, zz)`——返回类型随 `Y` 是否传入而变。
- Python 入口用 `@lazy_cython` + 内嵌 `cy_cophenet` 闭包 + `xpx.lazy_apply` 三件套桥接到 Cython，与 `linkage` 同构（[u3-l3](u3-l3-python-cython-bridge.md)）。
- Cython 后端 `cophenetic_distances` 用**显式栈 + 位图 visited + 叶子名册 members** 做迭代后序遍历，在每节点「跨左右」一次写好所有以其为 LCA 的叶子对，\(O(n^2)\) 时间、\(O(n)\) 空间。
- **cophenetic 相关系数** c 是原始距离与 cophenetic 距离的 Pearson 相关系数，衡量树的距离保真度；源码手写公式而非用 `np.corrcoef`，以兼容多数组后端。
- 入口的 `_is_valid_linkage` 校验是 Cython 安全的前提——非法 Z 会让 `members` 越界（gh-22183）。

## 7. 下一步学习建议

- 想理解「树的表示」与遍历的更多玩法，继续读 [u6-l1（ClusterNode、to_tree、cut_tree 与 leaves_list）](u6-l1-clusternode-and-tree.md)，对照 `leaves_list` 的 Cython 实现（同样是显式栈遍历）。
- 想看「后序遍历 + 位图 visited」骨架在别处的复用，读 [u5-l3（inconsistent 与 max 系列统计）](u5-l3-inconsistent-and-maxstats.md)，那几个 Cython 后端与 `cophenetic_distances` 共享同一套遍历模板。
- 想深入「桥接层」机制（`lazy_cython`、`xpx.lazy_apply`），复习 [u3-l3](u3-l3-python-cython-bridge.md) 与 [u7-l3（array API 兼容与测试体系）](u7-l3-arrayapi-and-tests.md)。
- 建议直接打开 [hierarchy/_hierarchy.pyx:306-378](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L306-L378)，配合本讲 4.3.4 的手工追踪把整段算法在脑中跑一遍，这是巩固后序遍历直觉的最佳方式。
