# KDTree 与 cKDTree 使用入门

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚「最近邻查询」这类问题是什么，以及为什么直接两两比较会慢。
- 用一句话讲明白 kd-tree 的核心思想，并解释 `leafsize` 在建树中的作用。
- 用 `scipy.spatial.KDTree` 建树并完成一次 `k` 近邻查询。
- 解释 `KDTree` 与 `cKDTree` 之间的继承关系，知道两者默认 `leafsize` 不同。
- 区分 `compact_nodes`、`balanced_tree`、`copy_data`、`boxsize` 这几个构造参数的用途，特别是 `boxsize` 的周期边界语义。

## 2. 前置知识

本讲默认你已经读完前置讲义 u1-l2（目录结构与公共 API 导出），知道 `scipy.spatial` 顶层导出的 `KDTree`、`cKDTree` 来自私有模块 `_kdtree.py` / `_ckdtree.pyx`。此外需要三个基础概念：

- **最近邻问题（nearest neighbor）**：给一堆参考点和一个查询点，问「离查询点最近的参考点是哪一个」。把这个问题推广到「最近的 k 个」就是 k 近邻查询。
- **朴素算法的代价**：若有 n 个参考点，每次查询都要把查询点和全部 n 个点比一遍，单次查询代价 O(n)。当 n 很大、查询又很多次时（例如上百万次），这就成了瓶颈。
- **Python 类继承**：子类可以复用父类的方法。本讲最关键的一条事实是 `KDTree` 继承自 `cKDTree`，几乎所有真正干活的代码都在父类里。

Minkowski 距离（p 范数）会在源码里反复出现，其定义为：

\[
d_p(x, y) = \left( \sum_{i=1}^{m} |x_i - y_i|^p \right)^{1/p}, \quad 1 \le p \le \infty
\]

其中 \(p=2\) 就是欧氏距离，\(p=1\) 是曼哈顿距离，\(p=\infty\) 是切比雪夫距离（最大坐标差）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [_kdtree.py](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_kdtree.py) | 纯 Python 实现的薄封装。定义公共类 `KDTree`、几何基元 `Rectangle`，以及一些距离辅助函数。本讲只看 `KDTree`。 |
| [_ckdtree.pyx](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_ckdtree.pyx) | Cython 实现的真正内核。`cKDTree` / `cKDTreeNode` 在这里定义，建树与查询最终都落到这里。 |
| [tests/test_kdtree.py](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/test_kdtree.py) | KDTree / cKDTree 的测试套件。它用一套装饰器让同一份测试同时跑在两个实现上，是理解两者关系的最佳入口。 |

## 4. 核心概念与源码讲解

### 4.1 最近邻问题与 kd-tree 直觉

#### 4.1.1 概念说明

kd-tree（k-dimensional tree）是一种**空间划分数据结构**，专门用来加速最近邻查询。它的核心想法用一句话概括：

> 把空间反复沿某个坐标轴切成两半，直到每个小区域里的点足够少；查询时只深入「可能有最近邻」的几个区域，从而跳过绝大多数点的比较。

注意这里的 `k`（维度）和后面 `query` 的 `k`（近邻个数）是两个不同的 `k`，别混淆。

为什么这样做能省时间？因为每次切分都把点集大致减半，树高约为 \(\log_2 n\)，沿一条路径走到叶子只比较少数点；再借助「区域到查询点的最小距离」做剪枝，可以整棵子树地跳过。

#### 4.1.2 核心流程

建树（自顶向下递归）：

1. 取当前区域内所有点。
2. 若点数 ≤ `leafsize`，停止切分，把该区域记为「叶子节点」。
3. 否则选一个维度（轴）和一个切分值，把点分成 `lesser` / `greater` 两组，递归处理。

查询最近邻：

1. 从根节点出发，按查询点落在切分值哪一侧，先深入一侧子树。
2. 到达叶子后，对叶子里的点逐一计算真实距离，维护当前最优（最近）距离。
3. 回溯时判断「另一侧子树所在区域离查询点的最近距离」是否可能小于当前最优距离；若不可能则整棵子树剪掉，否则进入继续找。

切分轴与切分值的选择用 docstring 里提到的 **sliding midpoint（滑动中点）规则**，目的是避免出现又长又窄的细长区域。

#### 4.1.3 源码精读

公共类 `KDTree` 的 docstring 把算法直觉讲得很清楚，先读它的「Notes」段落：

[_kdtree.py:341-360](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_kdtree.py#L341-L360) — 说明 kd-tree 是一棵二叉树，每个节点代表一个轴对齐的超矩形（hyperrectangle），按某条轴把点一分为二；并提醒「高维（20 维以上）不要指望比暴力快多少」，因为高维最近邻本身是开放难题。

真正的建树在 Cython 内核里完成，关键一行是把工作交给 C++ 函数 `build_ckdtree`：

[_ckdtree.pyx:640-641](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_ckdtree.pyx#L640-L641) — 在 `with nogil:`（释放 GIL，可被多线程并发）块里调用 `build_ckdtree(cself, 0, cself.n, ptmpmaxes, ptmpmins, median, compact)`，把 `median`（是否用中位数切分）和 `compact`（是否收紧超矩形）作为标志传入。建树算法的 C++ 实现在 `ckdtree/src/build.cxx`，本讲不深入，留到专家层 u8-l2。

> 提示：建树细节（节点扁平化数组存储、递归划分）属于专家层内容，本讲只建立「调用链在这里」的认知即可。

#### 4.1.4 代码实践

**实践目标**：亲手感受「暴力比较」与「kd-tree 查询」输出一致，确认 kd-tree 没有算错。

**操作步骤**（示例代码，可直接运行）：

```python
# 示例代码
import numpy as np
from scipy.spatial import KDTree

rng = np.random.default_rng(0)
data = rng.random((50, 2))          # 50 个二维参考点
tree = KDTree(data)                 # 建树

q = np.array([0.3, 0.7])
d, i = tree.query(q, k=3)           # 找最近的 3 个邻居
print("距离:", d, "索引:", i)

# 暴力对照：算出查询点到全部点的距离再排序
brute = np.linalg.norm(data - q, axis=1)
top3 = np.argsort(brute)[:3]
print("暴力对照索引:", top3, "距离:", brute[top3])
```

**需要观察的现象**：`i`（kd-tree 返回的索引）应当等于 `top3`（暴力排序的索引），两者距离一致。

**预期结果**：两组索引相同（平局时具体次序可能不同，但都属于同一距离的点）。

#### 4.1.5 小练习与答案

**练习 1**：如果把上面的数据点数从 50 改成 2，`query(q, k=3)` 返回什么？
**答案**：只有 2 个点，第 3 个邻居不存在，`d` 中对应位置会是 `inf`，对应索引会是 `tree.n`（即点数，表示「无效」）。这正是 KDTree.query docstring 里「Missing neighbors are indicated with infinite distances」的含义。

**练习 2**：`query` 的参数 `p` 改成 `1` 后，返回的「最近」点会不会变？
**答案**：可能会。`p=1` 用曼哈顿距离排序，与欧氏距离（`p=2`）的排序结果不一定相同，尤其当几个候选点距离接近时。

### 4.2 KDTree 与 cKDTree 的继承关系

#### 4.2.1 概念说明

`scipy.spatial` 同时导出两个名字：`KDTree` 和 `cKDTree`。它们不是两套独立实现，而是**父子关系**：

```
cKDTree  （Cython 内核，真正干活）
   ↑
KDTree   （纯 Python 子类，做参数校验和便捷封装）
```

历史上 `cKDTree` 是用 C 语言重写的高性能版本，`KDTree` 是较早的纯 Python 版本；后来纯 Python 实现被废弃，`KDTree` 改造成 `cKDTree` 的子类，只保留少量 Python 层逻辑。所以今天两者底层完全相同，性能几乎没有差别。

#### 4.2.2 核心流程

当你调用 `KDTree(data)` 时：

1. `KDTree.__init__` 先做轻量校验（拒绝复数、转成 ndarray）。
2. 通过 `super().__init__(...)` 调用 `cKDTree.__init__`，后者把数据强制成 float64、校验有限性、处理 boxsize、真正建树。
3. 你调用 `tree.query(...)` 时，`KDTree.query` 做参数校验后，同样 `super().query(...)` 委托给 `cKDTree.query`。

换句话说，`KDTree` 的方法大多是「校验 + 转发」。

#### 4.2.3 源码精读

继承声明就一行，这是本讲最重要的一处代码：

[_kdtree.py:276](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_kdtree.py#L276) — `class KDTree(cKDTree):`，明确定义 `KDTree` 继承自 `cKDTree`。`cKDTree` 在文件顶部从 Cython 编译产物导入：[_kdtree.py:7](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_kdtree.py#L7)（`from ._ckdtree import cKDTree, cKDTreeNode`）。

`KDTree.__init__` 做的是「转 ndarray + 拒绝复数 + 转发」：

[_kdtree.py:435-443](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_kdtree.py#L435-L443) — 注意第 441 行的注释 `# Note KDTree has different default leafsize from cKDTree`，以及它把 `leafsize` 默认值设为 `10` 透传给父类。

`KDTree.query` 同样是「校验 + 转发」模式：

[_kdtree.py:550-560](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_kdtree.py#L550-L560) — 拒绝复数、拒绝 `k=None`（抛 ValueError），然后 `super().query(...)`，最后把返回的纯 Python `int` 索引包成 `np.intp`。

真正干活的 `cKDTree.__init__`，注意它的默认 `leafsize=16`：

[_ckdtree.pyx:563-564](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_ckdtree.pyx#L563-L564) — `def __init__(cKDTree self, data, np.intp_t leafsize=16, ...)`。

测试套件用一个装饰器 `KDTreeTest` 把同一份测试「复制」成两份，分别针对 `KDTree` 和 `cKDTree` 跑，这是验证「两者行为一致」的关键证据：

[test_kdtree.py:26-43](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/test_kdtree.py#L26-L43) — 装饰器遍历 `(KDTree, cKDTree)`，为每个实现动态生成一个子类、把 `kdtree_type` 设成对应的构造器。这说明维护者默认两者行为应当完全一致。

#### 4.2.4 代码实践

**实践目标**：用代码直接验证继承关系，并观察两者默认 `leafsize` 的差异。

**操作步骤**：

```python
# 示例代码
from scipy.spatial import KDTree, cKDTree

print(KDTree.__mro__)          # 方法解析顺序，能看到 cKDTree 在链上
print(KDTree is cKDTree)       # False，是两个不同的类
print(issubclass(KDTree, cKDTree))  # True

import numpy as np
pts = np.random.rand(20, 2)
print(KDTree(pts).leafsize)    # 10
print(cKDTree(pts).leafsize)   # 16
```

**需要观察的现象**：`KDTree.__mro__` 中出现 `cKDTree`；两者默认 `leafsize` 分别是 10 和 16。

**预期结果**：`issubclass` 为 True；`KDTree` 实例 `leafsize` 为 10，`cKDTree` 实例 `leafsize` 为 16。这就是 _kdtree.py:441 那条注释所指的差异。

#### 4.2.5 小练习与答案

**练习 1**：既然 `KDTree` 几乎只是转发，为什么不直接用 `cKDTree`？
**答案**：可以，历史上 `cKDTree` 更快所以有人偏好它；但官方文档主推 `KDTree`，且 `KDTree` 提供了额外的 Python 层便利（例如更友好的节点视图 `tree` 属性，见 4.3.3）。两者性能在今天几乎无差别，新代码建议用 `KDTree`。

**练习 2**：调用 `KDTree(pts).query(...)` 时，`KDTree.__init__` 并没有把数据转成 float64，数据类型是在哪里被强制的？
**答案**：在父类 `cKDTree.__init__` 里，[_ckdtree.pyx:577](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_ckdtree.pyx#L577) 用 `np.array(data, order='C', copy=copy_data, dtype=np.float64)` 强制转成连续的 float64。所以无论从哪个类进入，数据最终都是 float64。

### 4.3 构造参数：leafsize、compact_nodes、balanced_tree、copy_data

#### 4.3.1 概念说明

`KDTree(data, leafsize=10, compact_nodes=True, copy_data=False, balanced_tree=True, boxsize=None)` 共 5 个可调参数（`boxsize` 单独放到 4.4 讲）：

| 参数 | 含义 | 默认 |
| --- | --- | --- |
| `leafsize` | 叶子节点最多放多少个点，超过就继续切分；到叶子后改用暴力比较 | 10（cKDTree 为 16） |
| `compact_nodes` | 是否把每个节点的超矩形「收紧」到实际数据范围 | True |
| `balanced_tree` | 是否用中位数切分（更平衡）而非中点切分 | True |
| `copy_data` | 是否总是复制一份数据，保护树不被外部修改破坏 | False |

直觉上：`leafsize` 越小，树越深、建树越慢但查询可能更准更快；`leafsize` 越大，树越浅、建树快但查询时叶子里的暴力比较更多。`compact_nodes` 和 `balanced_tree` 影响树的形状，进而影响剪枝效率，默认值对大多数数据都是好的。

#### 4.3.2 核心流程

构造一棵树的步骤（落在 `cKDTree.__init__`）：

1. 数据强制转 float64、C 连续；若 `copy_data=False` 则尽量不复制（NumPy 的 `COPY_IF_NEEDED` 语义）。
2. 校验：必须是二维、必须有限（不能有 nan/inf）、`leafsize >= 1`。
3. 把 `data` 设为只读视图（防止外部修改破坏树）。
4. 计算 `maxes` / `mins`（每维最大最小值，定义根超矩形）、`indices`（点序号数组）。
5. 调用 `build_ckdtree` 真正建树，传入 `median`（来自 `balanced_tree`）和 `compact`（来自 `compact_nodes`）两个标志。

#### 4.3.3 源码精读

`copy_data` 与「尽量不复制」的语义：

[_ckdtree.pyx:575-577](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_ckdtree.pyx#L575-L577) — `if not copy_data: copy_data = copy_if_needed`，再用 `np.array(..., copy=copy_data, ...)` 决定是否真复制。默认 `copy_data=False` 意味着：只有当输入不是合适的 float64 连续数组时才复制。

数据只读保护与有限性校验：

[_ckdtree.pyx:581-589](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_ckdtree.pyx#L581-L589) — 把 `data.flags.writeable = False` 设为只读，并 `if not np.isfinite(data).all(): raise ValueError(...)` 拒绝 nan/inf。这也是测试 `test_kdtree_nan` / `test_nonfinite_inputs_gh_18223` 校验的行为。

`balanced_tree` / `compact_nodes` 如何变成建树标志：

[_ckdtree.pyx:630-641](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_ckdtree.pyx#L630-L641) — `compact = 1 if compact_nodes else 0`、`median = 1 if balanced_tree else 0`，再传入 `build_ckdtree`。注意建树在 `with nogil` 块内执行，说明这段 C++ 代码不持有 GIL，可被多线程并发调用。

测试 `test_kdtree_copy_data` 证明 `copy_data=True` 能挡住外部修改：

[test_kdtree.py:930-943](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/test_kdtree.py#L930-L943) — 建树后把原始 `points` 整体替换成新随机数，若 `copy_data=True`，查询结果不变；若 `copy_data=False`（默认），树内部引用了原数组，结果会被污染。这也正是 docstring 里「modifying the data after creating the KDTree may lead to data corruption」的由来。

> 补充：`KDTree` 还在 4.2 的「便捷封装」基础上多提供了一个 Python 友好的 `tree` 属性，它把 Cython 的 `cKDTreeNode` 包装成 `KDTree.node` / `leafnode` / `innernode`，见 [_kdtree.py:428-433](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_kdtree.py#L428-L433) 与 [_kdtree.py:369-426](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_kdtree.py#L369-L426)。本讲只需知道有这个属性即可，遍历节点的实践见 `test_kdtree_tree_access`。

#### 4.3.4 代码实践

**实践目标**：观察 `copy_data` 对数据隔离的影响。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.spatial import KDTree

rng = np.random.default_rng(1)
points = rng.random((100, 2))

# copy_data=False（默认）：树与 points 共享内存
t_shared = KDTree(points, copy_data=False)
before = t_shared.query(points[:3], k=1)
points[...] = rng.random((100, 2))   # 就地修改原数组
after = t_shared.query(points[:3], k=1)
print("共享时查询是否改变:", not np.array_equal(before[1], after[1]))

# copy_data=True：树持有副本，不受影响
points2 = rng.random((100, 2))
t_copy = KDTree(points2, copy_data=True)
ref = t_copy.query(points2[:3], k=1)
points2[...] = rng.random((100, 2))
ref2 = t_copy.query(points2[:3], k=1)
print("复制时查询是否稳定:", np.array_equal(ref[1], ref2[1]))
```

**需要观察的现象**：共享版本查询结果会因原数组被改而变化；复制版本查询结果保持稳定。

**预期结果**：第一行打印 True（会改变），第二行打印 True（稳定）。注意：实践中改动共享数据属于「未定义行为」，这里只为直观感受，生产代码不要这么做。

#### 4.3.5 小练习与答案

**练习 1**：默认情况下 `KDTree(data)` 会复制你传入的数组吗？
**答案**：不一定。默认 `copy_data=False`，仅当输入不是「C 连续的 float64」时才复制（`COPY_IF_NEEDED`）。如果你传的就是 `np.ascontiguousarray` 过的 float64 数组，树会直接引用它而不复制——所以建树后修改原数组是危险的。

**练习 2**：`compact_nodes=False, balanced_tree=False` 会得到一棵「错误」的树吗？
**答案**：不会错，只是形状不同。测试 [test_kdtree.py:887-900](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/test_kdtree.py#L887-L900)（`test_kdtree_build_modes`）正是验证四种 `compact/balanced` 组合的查询结果完全一致，只是性能/树形有别。

### 4.4 boxsize 周期边界

#### 4.4.1 概念说明

`boxsize` 给 kd-tree 加上**周期边界（periodic boundary）**，也就是把空间当成一个「环面（torus）」：点的坐标会按 \(x_i + n_i L_i\) 折叠，距离按「最小镜像（minimum image）」计算。

典型场景是分子动力学、宇宙学模拟：模拟在一个有限盒子（如 \([0, L)^d\)）里进行，盒子边界是周期性的——一个粒子从右边出去会从左边进来。这种情况下，查询点旁边的「最近邻」可能其实是某个点的周期镜像。

周期距离的核心是把每个坐标差折叠进 \([-L/2, L/2]\)：

\[
\Delta_i = (x_i - y_i), \quad \Delta_i \leftarrow \Delta_i - L_i \cdot \mathrm{round}(\Delta_i / L_i)
\]

实现上等价于先把大于 \(L/2\) 的差减去 \(L\)、小于 \(-L/2\) 的差加上 \(L\)。

#### 4.4.2 核心流程

开启 `boxsize` 时的额外流程（在 `cKDTree.__init__`）：

1. 把 `boxsize` 广播成 shape `(m,)` 的 float64 数组（每维可不同）。
2. 构造 `boxsize_data`：前 `m` 个是 \(L_i\)，后 `m` 个是 \(L_i/2\)（供最小镜像计算用）。
3. 校验：周期维度上，数据必须落在 \([0, L_i)\)；超出上界或为负都会抛 `ValueError`。
4. 查询时，距离计算使用 `boxsize_data` 做折叠，使得「查询点 + L」和「查询点」得到相同的邻居。

特别地，某维 `boxsize=0` 表示该维**非周期**（被 `boxsize > 0` 的掩码排除），因此可以「逐维」指定周期性。

#### 4.4.3 源码精读

boxsize 的解析、折叠常量构造与边界校验：

[_ckdtree.pyx:599-615](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_ckdtree.pyx#L599-L615) — 重点看三处：`self.boxsize_data[:self.m] = boxsize` 与 `self.boxsize_data[self.m:] = 0.5 * boxsize`（同时存 \(L\) 和 \(L/2\)）；`periodic_mask = self.boxsize > 0`（0 表示该维非周期）；两条 `raise ValueError` 分别拦截「数据 ≥ 盒子大小」和「负数据」。

测试里的「最小镜像」参考实现，正好对应上面的折叠公式：

[test_kdtree.py:46-51](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/test_kdtree.py#L46-L51) — `distance_box` 把 `diff` 折叠进 \([-0.5*boxsize, 0.5*boxsize]\)，这就是周期距离的纯 Python 参考实现，可供对照。

验证周期性的关键测试：查询点平移一整个盒子后邻居不变：

[test_kdtree.py:1032-1057](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/test_kdtree.py#L1032-L1057) — `test_kdtree_box` 建一棵 `boxsize=1.0` 的树，断言 `query(data)`、`query(data + 1.0)`、`query(data - 1.0)` 三者结果一致，并与 `simulate_periodic_box` 暴力枚举 \(3^d\) 个镜像的结果对照。`test_kdtree_box_0boxsize` 则验证 `boxsize=0.0` 退化为非周期。

边界校验测试：

[test_kdtree.py:1078-1090](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/test_kdtree.py#L1078-L1090) — `test_kdtree_box_upper_bounds` / `test_kdtree_box_lower_bounds` 验证数据越界会抛 `ValueError`，并演示 `boxsize=(2.0, 0.0)` 可让第二维非周期。

#### 4.4.4 代码实践

**实践目标**：亲手对比「无 boxsize」与「有 boxsize」在查询点接近边界时的结果差异。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.spatial import KDTree

rng = np.random.default_rng(2)
# 数据落在 [0, 1) 区间，模拟一个单位周期盒子
data = rng.random((200, 2))

tree_plain = KDTree(data)                 # 普通树
tree_box   = KDTree(data, boxsize=1.0)    # 周期树

# 查询点紧贴盒子右边界（x≈1）
q = np.array([0.99, 0.5])
d_plain, i_plain = tree_plain.query(q, k=3)
d_box,   i_box   = tree_box.query(q, k=3)

print("普通树邻居索引:", np.sort(i_plain), "距离:", np.round(d_plain, 3))
print("周期树邻居索引:", np.sort(i_box),   "距离:", np.round(d_box, 3))

# 关键观察：周期树会「绕过」边界，把 x≈0 那一侧的点也算作近邻
near_left = data[i_box][data[i_box][:, 0] < 0.2]
print("周期树是否找来了左边界附近的点:", len(near_left) > 0)
```

**需要观察的现象**：普通树只看 \(x<0.99\) 一侧的点；周期树会把 \(x\) 接近 0 的点（实际距离只需绕过 0.01 的边界）也算作近邻，因此周期树的某些距离会明显更小。

**预期结果**：周期树返回的最近距离中，至少有一个明显小于普通树的最小距离（因为它穿越了周期边界）。若随机种子使查询点附近没有「镜像近邻」，可换 `q=np.array([0.995, 0.5])` 再试。

> 「待本地验证」提示：由于数据是随机的，具体索引会随种子变化；但你应能稳定观察到「周期树的最小距离 ≤ 普通树的最小距离」这一关系。

#### 4.4.5 小练习与答案

**练习 1**：`boxsize=(1.0, 0.0)` 表示什么？
**答案**：第一维周期（盒子大小 1.0），第二维非周期。源码里 `periodic_mask = self.boxsize > 0` 会把第二维排除在周期校验和折叠之外，测试 `test_kdtree_box_upper_bounds` 正好用了这个写法来「跳过」第二维。

**练习 2**：为什么 `boxsize` 模式下要求数据落在 \([0, L)\)，而不是任意区间？
**答案**：因为最小镜像折叠以 \([0, L)\) 为基本胞元，数据必须先被 wrap 进这个基本区间，距离折叠才有意义。源码因此在校验阶段就拦截了越界数据（[_ckdtree.pyx:612-615](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_ckdtree.pyx#L612-L615)）。

## 5. 综合实践

把本讲的「建树 → k 近邻 → 周期边界」串起来：

1. 用 `rng.random((300, 2))` 生成 300 个落在 \([0,1)^2\) 的二维点。
2. 用 `KDTree` 构造两棵树：一棵普通树，一棵 `boxsize=1.0` 的周期树。
3. 选 5 个查询点，其中至少 2 个紧贴边界（如 `(0.01, 0.5)`、`(0.99, 0.5)`）。
4. 对每个查询点分别用两棵树做 `query(q, k=3)`，把结果整理成表格：`查询点 | 普通树索引 | 普通树距离 | 周期树索引 | 周期树距离`。
5. 对周期树的结果，用 4.4.3 引用的 `distance_box` 思路手算一次周期距离，验证与 `query` 返回值一致。
6. 写一句话总结：在哪几个查询点上两棵树结果不同？为什么？

参考骨架（示例代码）：

```python
# 示例代码
import numpy as np
from scipy.spatial import KDTree

rng = np.random.default_rng(42)
data = rng.random((300, 2))
t_plain = KDTree(data)
t_box = KDTree(data, boxsize=1.0)

queries = np.array([[0.5, 0.5], [0.01, 0.5], [0.99, 0.5],
                    [0.5, 0.02], [0.5, 0.98]])
for q in queries:
    dp, ip = t_plain.query(q, k=3)
    db, ib = t_box.query(q, k=3)
    print(q, "普通:", np.round(dp,3), "周期:", np.round(db,3))
```

**预期结果**：中心点 `(0.5, 0.5)` 两种树结果相同；贴边点（`(0.01,0.5)`、`(0.99,0.5)` 等）周期树会出现更小的距离，因为它穿越了周期边界。

## 6. 本讲小结

- kd-tree 通过「沿轴递归切分空间」把最近邻查询从 O(n) 降到约 O(log n)，`leafsize` 决定何时停止切分改用暴力比较。
- `scipy.spatial` 里 `KDTree` 是 `cKDTree` 的纯 Python 子类，底层建树与查询全部落在 Cython 内核 `cKDTree` 中。
- 两者行为一致（测试用 `KDTreeTest` 装饰器让同一份测试同时跑两个实现），但默认 `leafsize` 不同：`KDTree` 为 10，`cKDTree` 为 16。
- `KDTree.__init__` / `KDTree.query` 都是「校验 + `super()` 转发」模式；数据在 `cKDTree.__init__` 被强制成只读的 C 连续 float64，并拒绝 nan/inf 与复数。
- `copy_data` 控制是否复制数据（默认尽量不复制，因此建树后改原数组是危险的）；`compact_nodes` / `balanced_tree` 只影响树形不影响结果正确性。
- `boxsize` 开启周期边界（环面拓扑），数据必须落在 \([0,L)\)，距离按最小镜像计算；`boxsize=0` 的维度视为非周期。

## 7. 下一步学习建议

- 想深入了解 kd-tree 剪枝用的几何基元，请读下一讲 **u2-l2（Rectangle 几何基元与 minkowski 距离）**，它会讲清 `Rectangle` 类和 `_kdtree.py` 里的距离函数如何支撑剪枝。
- 想系统掌握各种查询语义（球形邻域、成对、计数、稀疏距离矩阵），请读 **u2-l3（KDTree 查询方法全景）**。
- 想看建树/查询的 C++ 内核实现（扁平化节点、优先队列剪枝），留到专家层 **u8-l1 / u8-l2**。
- 建议同步浏览 [tests/test_kdtree.py](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/test_kdtree.py)，它是理解两个实现等价性的最好材料。
