# ClusterNode、to_tree、cut_tree 与 leaves_list

## 1. 本讲目标

学完本讲，你应当能够：

- 读懂 `ClusterNode` 这个二叉树节点类的五个属性（`id/left/right/dist/count`）以及它「叶子计数为 1、内部节点计数等于两孩子之和」的递归结构。
- 用 `to_tree(Z)` 把一个 (n−1)×4 的 linkage matrix 还原成一棵 `ClusterNode` 树，并说清楚 `rd=False` 与 `rd=True` 两种返回值的区别。
- 说出 `leaves_list(Z)` 返回的「从左到右的叶子顺序」是怎么来的，并理解它与 `ClusterNode.pre_order()` 的等价关系。
- 跟踪 `cut_tree` 如何借助 `_order_cluster_tree`（按距离自底向上的节点序列）「回放」合并过程，从而在任意簇数或任意高度处给出扁平归属。

本讲承接 u3-l1（linkage matrix 数据结构），是把「矩阵 Z」翻译成「树」并做各种基于树的操作的总入口。

## 2. 前置知识

在进入源码前，先用通俗语言把几个概念说清楚。

- **linkage matrix Z**：u3-l1 讲过，它是 (n−1)×4 的矩阵。第 `i` 行（从 0 起）描述一次合并：`Z[i,0]`、`Z[i,1]` 是被合并的两个簇编号，`Z[i,2]` 是合并距离，`Z[i,3]` 是合并后新簇包含的原始观测数。原始观测占编号 `0..n−1`，第 `i` 步合并出的新簇占编号 `n+i`，整棵树共 `2n−1` 个节点。
- **凝聚式聚类**：自底向上，每步合并两个簇，共 n−1 步。这个「合并历史」就是 Z。
- **二叉树（binary tree）**：每个内部节点恰好有两个孩子。`scipy.cluster.hierarchy` 里的层次树是**满二叉树**（full / proper binary tree）——一个节点要么是叶子（无孩子），要么有两个孩子，不允许只有一个孩子。
- **前序遍历（pre-order traversal）**：对一棵树「先访问自己，再访问左子树，再访问右子树」的深度优先顺序。前序遍历里叶子被访问的先后次序，就是它们「从左到右」排布的次序。
- **回放（replay）**：cut_tree 的核心思路。把 n 个观测初始化为「各成一簇」，然后按合并距离从小到大重放每一次合并：每处理一个内部节点，就把它名下所有叶子并成一个簇。重放到第 k 步时的归属状态，就对应「n−k 个簇」的切分。

## 3. 本讲源码地图

本讲全部代码集中在 `hierarchy/_hierarchy_impl.py`，外加 `leaves_list` 调用的 Cython 后端 `hierarchy/_hierarchy.pyx`。

| 文件 | 作用 |
| --- | --- |
| `hierarchy/_hierarchy_impl.py` | 公共 API 与纯 Python 实现。本讲涉及 `ClusterNode`、`to_tree`、`leaves_list`、`_order_cluster_tree`、`cut_tree` 五处。 |
| `hierarchy/_hierarchy.pyx` | Cython 性能层。`leaves_list` 最终落到这里的 `prelist`，用显式栈做前序遍历。 |

阅读顺序建议：先看 `ClusterNode`（节点长什么样）→ 再看 `to_tree`（怎么搭出整棵树）→ 再看 `leaves_list`（怎么只取叶子顺序）→ 最后看 `_order_cluster_tree` 与 `cut_tree`（怎么基于树做切分）。

## 4. 核心概念与源码讲解

### 4.1 ClusterNode：树的节点表示

#### 4.1.1 概念说明

`ClusterNode` 是一个普普通通的 Python 类，用来表示层次树里的一个节点。它不参与任何算法计算——docstring 里明确写了「ClusterNodes are not used as input to any of the functions in this library」，它存在的唯一目的是**给库使用者提供一个好读、好遍历的树对象**。

每个 `ClusterNode` 有五个属性：

- `id`：节点编号。`0 <= id < n` 是叶子（原始观测），`n <= id < 2n−1` 是内部节点（某次合并产生的新簇）。
- `left` / `right`：左右两个孩子，都是 `ClusterNode` 或 `None`。叶子节点的两个孩子都是 `None`。
- `dist`：该簇的合并距离（即 Z 里的 `Z[i,2]`），叶子为 0。
- `count`：该簇包含的原始观测（叶子）数量。叶子为 1。

#### 4.1.2 核心流程

构造与计数规则：

- 叶子：`left is None`，`count` 取传入参数（默认 1）。
- 内部节点：`count = left.count + right.count`，**忽略**传入的 `count` 参数，直接由两个孩子相加得到。

这是一个递归定义，整棵树任意节点的 `count` 都等于它名下所有叶子的个数。用公式表达：

\[
\text{count}(u) =
\begin{cases}
1, & u \text{ 是叶子} \\
\text{count}(u.\text{left}) + \text{count}(u.\text{right}), & \text{否则}
\end{cases}
\]

另外，`ClusterNode` 重载了 `<`、`>`、`==` 三个比较运算符，**比较的是 `dist`**。这看似无关紧要，其实是 4.4 节 `_order_cluster_tree` 用 `bisect.insort_left` 按距离排序的关键。

#### 4.1.3 源码精读

构造函数做了多项校验：`id` 非负、`dist` 非负、必须是满二叉树（不能只有一个孩子）、`count >= 1`，然后按上面的递归规则算 `count`。

[ClusterNode.__init__:L1008-L1027](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L1008-L1027) —— 这是节点构造与 `count` 递归计算的核心。注意倒数三行：叶子保留传入 `count`，内部节点则用 `left.count + right.count` 覆盖。

比较运算符一律比较 `dist`，并对类型不匹配抛 `ValueError`：

[ClusterNode.__lt__:L1029-L1033](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L1029-L1033) —— 正是因为 `__lt__` 比较 `dist`，后面 `bisect.insort_left` 才能把节点按距离排好序。

`pre_order` 是本类最值得读的方法：它**不用递归**，而是用一个显式栈 `curNode` 配合 `lvisited`/`rvisited` 两个集合做前序遍历，遇到叶子就把 `func(nd)`（默认返回 `nd.id`）追加到结果列表。返回值就是「叶子从左到右」的 id 序列。

[ClusterNode.pre_order:L1115-L1175](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L1115-L1175) —— 注意默认 `func=(lambda x: x.id)`，所以 `root.pre_order()` 直接返回叶子 id 列表，等价于 `leaves_list(Z)`（见 4.3）。

#### 4.1.4 代码实践

**实践目标**：亲手构造几个 `ClusterNode`，体会 `count` 的递归计算与满二叉树校验。

```python
from scipy.cluster.hierarchy import ClusterNode

# 两个叶子
l0 = ClusterNode(0)
l1 = ClusterNode(1)
# 合并成内部节点，编号 n=2（假设 n=2）
n2 = ClusterNode(2, l0, l1, dist=1.0)

print(n2.count)        # 2  —— 由 left.count + right.count 得到
print(n2.is_leaf())    # False
print(l0.is_leaf())    # True

# 故意制造「只有一个孩子」的非法节点，观察报错
try:
    ClusterNode(3, l0, None)
except ValueError as e:
    print("校验生效:", e)
```

**操作步骤**：直接运行上面的脚本。**预期结果**：依次打印 `2`、`False`、`True`、以及一条「Only full or proper binary trees are permitted」的校验信息。如果你构造 `ClusterNode(2, l0, l1, dist=1.0, count=99)`，`n2.count` 仍然是 `2`——内部节点忽略传入的 count。该运行结果可本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ClusterNode.__init__` 里内部节点要忽略传入的 `count` 参数，而由两孩子相加得到？

**答案**：因为 Z 矩阵的第四列 `Z[i,3]` 可能与「真实孩子计数之和」不一致（比如人为篡改了 Z）。由孩子相加得到真实计数，既能自洽，又能在 `to_tree` 里与 `Z[i,3]` 比对，发现「损坏的矩阵」（见 4.2）。

**练习 2**：`pre_order` 为什么用显式栈而不是递归？

**答案**：为了避免 Python 递归深度限制。当树很深时（数据点很多），递归可能触发 `RecursionError`；显式栈把调用栈搬到堆上，更安全。

---

### 4.2 to_tree：把 Z 矩阵还原成树

#### 4.2.1 概念说明

Z 是一个矩阵，适合喂给算法，但不适合人阅读——你想知道「谁和谁先合的、某条路径长什么样」时，矩阵很难看。`to_tree` 的作用就是把 Z「展开」成一棵 `ClusterNode` 树，根节点就是最后一次合并（编号 `2n−2`）。

它有两种返回模式，由 `rd` 参数控制：

- `to_tree(Z)`（`rd=False`，默认）：只返回根节点 `ClusterNode`，顺着 `.left/.right` 就能遍历整棵树。
- `to_tree(Z, rd=True)`：返回一个二元组 `(root, nodelist)`，其中 `nodelist` 是长度 `2n−1` 的列表，按下标即节点 id 存放所有 `ClusterNode`（叶子在前 n 个、内部节点在后 n−1 个）。

#### 4.2.2 核心流程

`to_tree` 的算法是**自底向上的一遍扫描**：

1. 先用 `_is_valid_linkage` 校验 Z 合法（形状、单调性、簇编号等，见 u7-l1）。
2. 算出 `n = Z.shape[0] + 1`（原始观测数 = 行数 + 1）。
3. 建一个长度 `2n−1` 的列表 `d`，前 n 个位置先放好叶子节点 `ClusterNode(i)`。
4. 逐行扫描 Z：对第 `i` 行，读出两个孩子编号 `fi`、`fj`，构造内部节点 `ClusterNode(i+n, d[fi], d[fj], row[2])`，放进 `d[n+i]`。
5. 全部扫完后，最后一个构造的节点 `nd` 就是根节点。按 `rd` 决定返回 `nd` 还是 `(nd, d)`。

因为扫描是按行号递增的，而 `d[fi]`、`d[fj]` 引用的孩子在更早的行（或叶子）里已经构造好了，所以这是一遍顺序正确的迭代，不需要递归。

#### 4.2.3 源码精读

叶子节点预创建：

[to_tree 预创建叶子:L1370-L1371](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L1370-L1371) —— 前 n 个位置放叶子 `ClusterNode(i)`。

逐行构造内部节点并做两项校验：

[to_tree 构造内部节点:L1375-L1394](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L1375-L1394) —— 关键三处：(1) 用 `_int_floor` 把 `row[0]/row[1]` 转成整数下标 `fi/fj`；(2) 校验 `fi`、`fj` 不超过 `i+n`，防止「引用了还没生成的新簇」；(3) 构造 `nd` 后校验 `row[3] == nd.count`，即 Z 第四列必须等于由孩子相加得到的真实计数。

> 注：装饰器 `@xp_capabilities(jax_jit=False, allow_dask_compute=True)`（L1298）声明此函数对 jax 不可 jit、但允许 dask 触发 compute。它只接收 NumPy 类数组，是纯 Python 实现。

按 `rd` 决定返回形态：

[to_tree 返回:L1396-L1399](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L1396-L1399) —— `rd=True` 时返回 `(根节点, 全节点列表)`，否则只返回根节点。

#### 4.2.4 代码实践

**实践目标**：用 `to_tree` 还原一棵小树，并对比 `rd=False` 与 `rd=True` 的返回值。

```python
import numpy as np
from scipy.cluster.hierarchy import linkage, to_tree

# 8 个二维点
X = np.array([[0,0],[0,1],[1,0],
              [0,4],[0,3],[1,4],
              [4,0],[3,0]], dtype=float)
Z = linkage(X, method='single')

root = to_tree(Z)                       # 只拿根节点
root2, nodelist = to_tree(Z, rd=True)   # 根节点 + 全节点列表

print(root is root2)                    # True —— 同一个根对象
print(len(nodelist))                    # 2*8 - 1 = 15
print(nodelist[14] is root)             # True —— 最后一个内部节点就是根
print(root.id, root.count, root.dist)   # 14 8 (最后一次合并距离)
```

**操作步骤**：运行脚本。**预期结果**：依次打印 `True`、`15`、`True`、以及 `14 8 <某距离>`。`nodelist` 长度恒为 `2n−1`。该结果可本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `n = Z.shape[0] + 1`，而不是直接等于行数？

**答案**：Z 有 n−1 行（n−1 次合并），所以原始观测数 n = 行数 + 1。这是 linkage matrix 的固定约定（见 u3-l1）。

**练习 2**：把某个 `Z[i,3]`（簇大小）改成错误值，再调用 `to_tree`，会发生什么？

**答案**：`to_tree` 会抛 `ValueError('Corrupt matrix Z. The count Z[i,3] is incorrect.')`，因为 L1391 的 `if row[3] != nd.count` 校验失败了。

---

### 4.3 leaves_list：左到右的叶子顺序

#### 4.3.1 概念说明

`leaves_list(Z)` 返回一个长度为 n 的 int32 数组，是「按树从左到右排布的叶子 id 序列」。它在画 dendrogram（系统树图，见 u6-l2）时用来给 x 轴的叶子排序——你看到的叶子左右顺序就来自这里。

它本质上是 `root.pre_order()`（4.1 节那个默认返回 `x.id` 的前序遍历）的等价物，只不过 `leaves_list` 不先建 `ClusterNode` 树，而是直接在 Z 上用 Cython 跑前序遍历，更快。

#### 4.3.2 核心流程

`leaves_list` 的 Python 层是个薄桥接：

1. 校验 Z。
2. 定义闭包 `cy_leaves_list(Z, validate)`：开一个长度 n 的 int32 缓冲 `ML`，调 Cython 函数 `_hierarchy.prelist(Z, ML, n)` 原地填充，返回 `ML`。
3. 用 `xpx.lazy_apply` 把闭包接到（可能的）惰性数组后端上；对普通 NumPy 数组走快路径直接调用。

Cython 后端 `prelist` 的算法是**显式栈 + visited 位图的前序遍历**：从根节点 `2n−2` 出发，对当前节点的左孩子、右孩子依次判断——若没访问过且是内部节点就压栈继续往下，若是叶子就写入结果数组。这样写出来的顺序恰好是前序遍历的叶子顺序。

#### 4.3.3 源码精读

Python 桥接层，闭包 + `lazy_apply`：

[leaves_list:L2754-L2769](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2754-L2769) —— `validate=is_lazy_array(Z)` 决定是否在惰性输入时再校验一次；`as_numpy=True` 表示输出统一成 NumPy int32。`shape=(n,)` 告诉 `lazy_apply` 输出形状。

Cython 后端 `prelist`，显式栈前序遍历：

[prelist 入口与栈初始化:L1136-L1163](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1136-L1163) —— 从 `curr_node[0] = 2*n - 2`（根）出发，用 `visited` 位图（大小 `(2n-1)/8 + 1` 字节）记录访问过的节点。

左/右孩子的下钻与叶子写入：

[prelist 遍历主体:L1166-L1188](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy.pyx#L1166-L1188) —— 先处理左孩子 `i_lc`：未访问且 `>= n`（内部节点）就压栈 `continue`，否则（叶子）写入 `members[mem_idx]`。右孩子同理。两侧都处理完才 `k -= 1` 回溯。

> 这里的关键不变量：**先左后右**的下钻顺序保证了叶子按「从左到右」写入 `members`，这正是 `leaves_list` 语义的来源。

#### 4.3.4 代码实践

**实践目标**：验证 `leaves_list(Z)` 与 `to_tree(Z).pre_order()` 给出完全相同的叶子顺序。

```python
import numpy as np
from scipy.cluster.hierarchy import linkage, leaves_list, to_tree

X = np.array([[0,0],[0,1],[1,0],[0,4],[0,3],[1,4],[4,0],[3,0]], dtype=float)
Z = linkage(X, method='single')

a = leaves_list(Z)                  # Cython 前序遍历
b = np.array(to_tree(Z).pre_order())# ClusterNode 前序遍历

print("leaves_list :", a)
print("pre_order   :", b)
print("完全一致?", np.array_equal(a, b))   # True
```

**操作步骤**：运行脚本。**预期结果**：两行数组完全相同，最后一行打印 `True`。这印证了「`leaves_list` 是 `root.pre_order()` 的快路径等价物」。该结果可本地验证。

#### 4.3.5 小练习与答案

**练习 1**：既然 `to_tree(Z).pre_order()` 也能得到叶子顺序，为什么还要单独的 `leaves_list`？

**答案**：性能。`to_tree` 要先在 Python 里构造 `2n−1` 个 `ClusterNode` 对象（有对象创建开销），而 `leaves_list` 直接在 Z 上用 Cython 显式栈遍历，不建任何 Python 对象，快得多。此外 `leaves_list` 还支持惰性数组后端（`lazy_apply`）。

**练习 2**：`prelist` 里为什么要先处理左孩子、再处理右孩子？如果反过来会怎样？

**答案**：先左后右保证了「从左到右」的叶子顺序，与 dendrogram 的画法一致。如果反过来，得到的叶子序列就是「从右到左」的镜像，虽然仍是合法的前序遍历，但不再符合 `leaves_list` 的语义约定。

---

### 4.4 _order_cluster_tree：按距离自底向上的节点序列

#### 4.4.1 概念说明

`_order_cluster_tree(Z)` 是一个**私有**辅助函数（带下划线前缀），不在公共 API 里，但它被 `cut_tree` 直接依赖。它的作用是：返回一个「按合并距离 `dist` 从小到大排序」的**内部节点列表**（注意：只含内部节点，不含叶子）。

为什么 cut_tree 需要这个？因为「切树」本质上是「按距离从小到大回放合并」——你必须先处理距离近的合并、再处理距离远的合并，才能在每个中间步骤得到正确的簇归属。

#### 4.4.2 核心流程

算法是「广度优先遍历 + 按距离插入排序」：

1. 用 `to_tree(Z)` 拿到根节点，放进一个 `deque` 队列。
2. 循环从队首取节点 `node`：
   - 如果是内部节点（非叶子），用 `bisect.insort_left(nodes, node)` 把它**按 `dist` 插入到有序列表 `nodes` 的正确位置**，然后把它的右孩子、左孩子依次入队。
   - 如果是叶子，跳过（不入 `nodes`）。
3. 队列空了，返回 `nodes`。

这里的魔法在 `bisect.insort_left`：它依赖 `ClusterNode.__lt__`（比较 `dist`，见 4.1）来决定插入位置，所以无论 BFS 以什么顺序访问节点，最终 `nodes` 都是按 `dist` 升序排列的。

#### 4.4.3 源码精读

[_order_cluster_tree:L1196-L1207](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L1196-L1207) —— `bisect.insort_left(nodes, node)` 把内部节点按 `dist` 维持成升序；`q.append(node.get_right())` 和 `q.append(node.get_left())` 把两孩子入队（顺序无关紧要，因为最终靠 insort 排序）。`if not node.is_leaf()` 确保叶子不进 `nodes`。

> 注意：返回的列表长度是 `n−1`（内部节点数），且单调方法下 `nodes[i].dist` 单调递增；非单调方法（centroid/median）可能出现距离倒挂，此时排序后仍按 dist 升序，但相邻节点的 dist 可能反映这种倒挂。

#### 4.4.4 代码实践

**实践目标**：观察 `_order_cluster_tree` 返回的节点确实按 `dist` 升序、且只含内部节点。

```python
import numpy as np
from scipy.cluster.hierarchy import linkage, _order_cluster_tree

X = np.array([[0,0],[0,1],[1,0],[0,4],[0,3],[1,4],[4,0],[3,0]], dtype=float)
Z = linkage(X, method='single')

nodes = _order_cluster_tree(Z)
print("内部节点个数:", len(nodes), " 期望:", Z.shape[0])      # 7
dists = [round(n.dist, 4) for n in nodes]
print("各节点 dist:", dists)
print("是否升序?", all(dists[i] <= dists[i+1] for i in range(len(dists)-1)))
print("全部非叶子?", all(not n.is_leaf() for n in nodes))
```

**操作步骤**：运行脚本。**预期结果**：内部节点个数 `7`（= n−1），dist 序列单调不降，「是否升序」与「全部非叶子」均为 `True`。该结果可本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`_order_cluster_tree` 为什么用 `bisect.insort_left` 而不是先收集全部再 `sorted`？

**答案**：功能上等价（都靠 `ClusterNode.__lt__` 比较 `dist`）。`insort` 在遍历同时维护有序性，省一次显式排序；这里更重要的点是它复用了 `__lt__` 的语义。两者复杂度同为 O((n−1) log(n−1)) 级别。

**练习 2**：如果 Z 是 centroid 方法产生、存在距离倒挂，`nodes` 还是「严格升序」吗？

**答案**：仍是「按 dist 升序」排列（insort 保证），但反映的是倒挂后的真实距离值；这意味着回放合并的「距离顺序」可能与「行号顺序」不一致。这正是 cut_tree 用 dist 排序而非行号排序的原因。

---

### 4.5 cut_tree：按簇数或高度切树

#### 4.5.1 概念说明

`cut_tree(Z, n_clusters=None, height=None)` 把一棵树「切成」扁平簇归属，可以一次性返回**多个切点**的结果。输出是一个形状 `(nobs, n_cols)` 的 int64 数组：每行是一个观测，每列是一个切点下的簇编号（从 0 起）。

三种使用方式：

- `cut_tree(Z)`：不传 `n_clusters` 也不传 `height`，返回「完整切树」——`nobs` 列，第 0 列每个观测自成一组，最后一列全部合并成一组。
- `cut_tree(Z, n_clusters=[2, 3])`：在每个指定簇数处切一刀，列数 = 列表长度。
- `cut_tree(Z, height=[1.0, 2.0])`：在每个指定高度处切一刀（仅对 ultrametric / 单调树有意义）。

`n_clusters` 与 `height` 不能同时给定，否则抛 `ValueError`。

#### 4.5.2 核心流程

cut_tree 的算法是**回放合并**，分两阶段。

**阶段一：确定要输出哪些列（`cols_idx`）。**

把「切点」翻译成「重放到第几步」。一次重放的「步数」= 已完成的合并数：

- 完整切树：`cols_idx = arange(nobs)`，即每一步都输出。
- 按高度 `height`：先取出 `nodes` 里每个节点的 `dist`（已升序），用 `searchsorted(heights, height)` 找到「dist ≤ height 的合并数」作为步数。
- 按簇数 `n_clusters`：`cols_idx = nobs - searchsorted(arange(nobs), n_clusters)`。因为第 `s` 步时有 `nobs - s` 个簇，所以「k 个簇」对应步数 `s = nobs - k`。

**阶段二：回放合并，在需要的步上快照。**

1. 初始化 `last_group = arange(nobs)`（每个观测自成一组，标签 0..nobs−1）。
2. 按 `_order_cluster_tree` 给出的距离升序，逐个处理内部节点 `node`：
   - `idx = node.pre_order()`：取出该节点名下所有叶子的下标。
   - `this_group = copy(last_group)`。
   - `this_group[idx] = min(last_group[idx])`：把这些叶子**并成一个簇**（取它们当前的最小标签）。
   - `this_group[this_group > max(last_group[idx])] -= 1`：把大于该簇最大标签的所有标签**减 1**，重新编号紧凑。
   - 如果当前步号 `i+1` 在 `cols_idx` 里，就把 `this_group` 存进输出对应列。
   - `last_group = this_group`，进入下一轮。
3. 返回 `groups.T`（转置成 `(nobs, n_cols)`）。

直觉上：每处理一个内部节点，就完成一次「把两簇合为一簇」，簇总数减 1；标签始终维持在 `0..(当前簇数−1)` 的连续区间。

#### 4.5.3 源码精读

确定输出列 `cols_idx`：

[cut_tree 计算 cols_idx:L1261-L1272](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L1261-L1272) —— 注意 `n_clusters` 分支用 `nobs - searchsorted(arange(nobs), n_clusters)`：因为第 `s` 步对应 `nobs - s` 个簇，反解出 `s`。`height` 分支用节点的 `dist` 升序数组做 `searchsorted`。

回放合并的主体循环：

[cut_tree 回放循环:L1280-L1293](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L1280-L1293) —— `node.pre_order()` 拿到该簇全部叶子；`this_group[idx] = min(last_group[idx])` 完成合并；`this_group[this_group > max(...)] -= 1` 收紧编号；命中 `cols_idx` 就存快照。

> 装饰器 `@xp_capabilities(np_only=True, reason="non-standard indexing")`（L1210）声明 cut_tree **只支持 NumPy**——因为循环里用到了「非标准索引」（如 `this_group[idx] = ...` 这种花式赋值），array API 标准尚未覆盖，故对 jax/dask 不开放。

#### 4.5.4 代码实践

**实践目标**：对同一棵树用 `n_clusters=[2,3]` 切，观察不同切点下的归属，并与 `fcluster` 对比。

```python
import numpy as np
from scipy.cluster.hierarchy import linkage, cut_tree, fcluster

X = np.array([[0,0],[0,1],[1,0],[0,4],[0,3],[1,4],[4,0],[3,0]], dtype=float)
Z = linkage(X, method='single')

ct = cut_tree(Z, n_clusters=[2, 3])
print("cut_tree(Z, n_clusters=[2,3]) =")
print(ct)   # 形状 (8, 2)：第0列=2簇、第1列=3簇

# 用 fcluster 在 t=2 处切，按 maxclust 比较（标签集合应同构）
fc = fcluster(Z, t=2, criterion='maxclust')
print("fcluster (2簇):", fc)
```

**操作步骤**：运行脚本，对比 `ct[:,0]`（2 簇切分）与 `fcluster` 的结果。**需要观察的现象**：两者簇编号的具体数字可能不同（cut_tree 从 0 起、fcluster 从 1 起），但**把观测分成两组的方式应当一致**（即「同簇关系」相同，编号无关）。**预期结果**：`ct[:,0]` 与 `fc-1`（统一基线后）描述完全相同的两簇划分。该结果可本地验证。

#### 4.5.5 小练习与答案

**练习 1**：`cut_tree(Z, n_clusters=[2,3])` 的两列之间有什么关系？

**答案**：第 1 列（3 簇）是第 0 列（2 簇）的「细化」——3 簇切分一定可以把 2 簇切分的某个簇再拆成两簇。这是层次聚类的「嵌套性」：在更细的切点上，同一个观测所属的簇是更粗切点对应簇的子集。

**练习 2**：回放循环里 `this_group[this_group > max(last_group[idx])] -= 1` 这一句为什么必要？

**答案**：合并后，原来分布在多个标签里的叶子并到了最小那个标签，导致标签集合里出现「空位」（被合并掉的较大标签不再被使用）。把所有大于 `max(last_group[idx])` 的标签减 1，是为了消除空位、让标签重新变成连续的 `0..(新簇数−1)`，否则后续的 `min`/`max` 与簇数统计会错乱。

---

## 5. 综合实践

把本讲的四个函数串起来，做一次「从矩阵到树到切分」的完整探索。

**任务**：给定 8 个二维点，完成下列步骤，并用一张文字流程图把数据流画出来。

```python
import numpy as np
from scipy.cluster.hierarchy import (
    linkage, to_tree, leaves_list, cut_tree, ClusterNode
)

X = np.array([[0,0],[0,1],[1,0],
              [0,4],[0,3],[1,4],
              [4,0],[3,0],[4,1]], dtype=float)[:8]   # 取 8 个点
Z = linkage(X, method='single')

# 步骤1：把 Z 还原成树，手写递归前序遍历，打印每个叶子 id
root, nodelist = to_tree(Z, rd=True)

def recursive_pre(node, out):
    if node.is_leaf():
        out.append(node.id)
    else:
        recursive_pre(node.left,  out)
        recursive_pre(node.right, out)
    return out

my_leaves = recursive_pre(root, [])
print("手写递归前序:", my_leaves)
print("leaves_list  :", list(leaves_list(Z)))
print("两者一致?", my_leaves == list(leaves_list(Z)))   # 期望 True

# 步骤2：在 2 簇和 3 簇处切树，观察归属变化
ct = cut_tree(Z, n_clusters=[2, 3])
print("2簇切分:", ct[:,0].tolist())
print("3簇切分:", ct[:,1].tolist())

# 步骤3：解释嵌套性
#   把 3 簇切分里属于同一组的观测，在 2 簇切分里是否也同组？逐对验证。
```

**操作步骤**：

1. 运行脚本，记录三步输出。
2. 画出数据流：`X → linkage → Z → to_tree → ClusterNode 树 →（手写递归 / leaves_list / cut_tree）`。
3. 回答：手写递归前序与 `leaves_list` 是否一致？3 簇切分相对 2 簇切分，是哪一个簇被拆开了？

**预期结果**：手写递归前序与 `leaves_list` 逐元素相同（印证 4.1/4.3 的等价关系）；`cut_tree` 的 3 簇切分是 2 簇切分的细化。若某些结果因数据/版本差异不确定，标注「待本地验证」。

## 6. 本讲小结

- `ClusterNode` 是纯 Python 的树节点类，五个属性 `id/left/right/dist/count`；`count` 递归定义为「叶子=1、内部=左+右」，并重载了按 `dist` 比较的 `<` 运算符，供排序使用。
- `to_tree(Z)` 一遍顺序扫描把 Z 还原成 `ClusterNode` 树，`rd=False` 返回根节点、`rd=True` 返回 `(根, 全节点列表 2n−1)`；扫描中会校验「引用未生成簇」与「Z[:,3] 计数正确」。
- `leaves_list(Z)` 是 `root.pre_order()` 的 Cython 快路径等价物，用显式栈 + visited 位图在 Z 上直接做前序遍历，先左后右保证「从左到右」语义，返回 int32 叶子 id 数组。
- `_order_cluster_tree(Z)` 用 BFS + `bisect.insort_left` 返回按 `dist` 升序的**内部节点**列表（长度 n−1），是 cut_tree 回放合并的依据。
- `cut_tree(Z, n_clusters=..., height=...)` 把切点翻译成「重放到第几步」，再从「各成一簇」出发逐节点合并并重编号，在命中的步上快照，返回 `(nobs, n_cols)` 的 int64 归属数组；仅支持 NumPy。

## 7. 下一步学习建议

- **u6-l2（dendrogram 可视化）**：本讲的 `leaves_list` 正是 dendrogram 给 x 轴叶子排序的依据，下一步看 `_dendrogram_calculate_info` 如何用叶子顺序画出 U 形链接。
- **u6-l3（optimal_leaf_ordering）**：本讲的叶子顺序是「聚类结构决定的固定顺序」；最优叶序则在不改变结构的前提下重排叶子使相邻距离最小，可作为对照学习。
- **u7-l1（校验）**：本讲多次出现 `_is_valid_linkage`，它的完整规则留到 u7-l1 精读；如果想理解「为什么 to_tree 能信任 Z」，那是必读。
- **重读 u3-l1**：如果对「Z 的四列含义、n+i 编号约定」仍有模糊，建议回头巩固，因为本讲所有函数都建立在这个数据结构之上。
