# Delaunay 三角剖分

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 Delaunay 三角剖分的几何含义，以及它「为什么等价于把点抬到抛物面上再求下凸包」。
- 用 `scipy.spatial.Delaunay` 对一组点构造三角剖分，并读懂 `simplices`、`neighbors`、`coplanar`、`equations`、`transform` 这几张数组的编码方案。
- 用 `find_simplex` 定位一个查询点落在哪个单纯形里，用 `plane_distance` 看它到各单纯形所在超平面的有符号距离，并用 `transform` 手算该点的重心坐标，验证重心坐标之和为 1。
- 看懂 `_qhull.pyx` 里 `Delaunay` 类如何把工作委托给底层 `_Qhull` 与 C 库 Qhull，并能读懂 `qhull_options` 各选项的作用。

本讲只讲 **Delaunay**，凸包（`ConvexHull`）和 Voronoi 图（`Voronoi`）留给同单元后续讲义。

## 2. 前置知识

### 2.1 什么是三角剖分

给定平面上的一组点，**三角剖分**（triangulation）就是把它们的凸包内部划分成一组三角形（高维时是单纯形），满足：

1. 这些三角形的顶点恰好是给定的点。
2. 任意两个三角形要么不挨着，要么共享一条完整的边（不出现「T 形」交叉）。
3. 所有三角形拼起来正好覆盖凸包内部，不重叠、不留缝。

很多点集的三角剖分并不唯一。Delaunay 三角剖分是其中「最漂亮」的一种。

### 2.2 Delaunay 性质：空外接圆

Delaunay 三角剖分的核心定义是：**每个三角形的外接圆内部不包含任何其它输入点**（空外接圆准则，empty circumcircle）。

直觉上，这条准则会让三角形尽量「胖」，避免出现狭长的、带极小角的三角形——这对插值、网格生成、有限元都是好事。官方教程的原话是：

> The Delaunay triangulation is a subdivision of a set of points into a non-overlapping set of triangles, such that no point is inside the circumcircle of any triangle. In practice, such triangulations tend to avoid triangles with small angles.

### 2.3 抛物面提升：三角剖分 = 下凸包

这是理解 `scipy.spatial` 里 `equations`、`plane_distance`、`transform` 的钥匙。把每个二维点 \((x, y)\) 抬到三维抛物面 \(z = x^2 + y^2\) 上：

\[
(x, y) \;\mapsto\; (x, y,\, x^2 + y^2)
\]

那么这组提升后的点的**下凸包**（lower convex hull）的每一个面，投影回 \(xy\) 平面，正好是一个 Delaunay 三角形。换句话说：

\[
\text{Delaunay 三角剖分} = \text{提升点的下凸包} \;\text{投影回原空间}
\]

这正是 Qhull 算 Delaunay 的办法：它本来就是个凸包计算器，靠「抬到抛物面」把 Delaunay 问题转化成它擅长的凸包问题。源码里的 `_lift_point`（提升点）、`paraboloid_scale/shift`（抛物面的缩放与平移，用于数值稳定性）、`equations`（凸包面的超平面方程）全都围绕这一转化展开。高维同理：\(d\) 维点抬到 \(d+1\) 维抛物面，下凸包的 \(d\) 维面投影回去就是 \(d\) 维 Delaunay 单纯形。

### 2.4 重心坐标

三角形（或 \(d\) 维单纯形）内部任意一点 \(x\)，可以唯一地写成各顶点 \(r_0, r_1, \dots, r_d\) 的仿射组合：

\[
x = c_0 r_0 + c_1 r_1 + \dots + c_d r_d, \qquad \sum_{i=0}^{d} c_i = 1
\]

系数 \((c_0, \dots, c_d)\) 称为 \(x\) 在该单纯形里的**重心坐标**（barycentric coordinates）。判断点是否在单纯形内部的判据就是：所有 \(c_i \ge 0\)（在数值上允许一个小容差 \(\varepsilon\)）。重心坐标在 `find_simplex` 和 `transform` 里扮演主角。

> 术语提示：本讲里 **simplex** 翻译为「单纯形」，二维单纯形就是三角形，三维是四面体；**facet** 是凸包的「面」；**ridge** 是面与面之间的「脊」（二维里就是边）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [_qhull.pyx](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx) | 本讲绝对主角。用 Cython 写的 Qhull 封装，包含 `_Qhull`（直接包 C 库）、`_QhullUser`（高层基类）、`Delaunay`/`ConvexHull`/`Voronoi`/`HalfspaceIntersection` 四个用户类、`tsearch`，以及重心坐标变换、单纯形查找等纯算法函数。 |
| [doc/source/tutorial/spatial.rst](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/doc/source/tutorial/spatial.rst) | 官方教程。用最小例子讲 Delaunay 的 `simplices`/`neighbors` 结构与 coplanar 点现象，是本讲实践任务的设计依据。 |
| [tests/test_qhull.py](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/test_qhull.py) | Qhull 封装的测试集。`test_find_simplex`、`test_plane_distance`、`test_coplanar` 直接示范了本讲要练的几件事。 |

注意 `_qhull.pyx` 是 Cython 源码，运行前会被编译成扩展模块 `_qhull`（参见 u1-l3 的构建讲义）。我们读的是 `.pyx` 源文件，引用行号也以源文件为准。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 Delaunay 类与构造过程**：从 `Delaunay.__init__` 到底层 `_Qhull`，看清一次三角剖分是怎么跑起来的，`qhull_options` 在做什么。
- **4.2 simplices / neighbors / coplanar 编码方案**：读懂 `Delaunay` 暴露的那几张数组。
- **4.3 find_simplex / plane_distance / transform / tsearch**：定位点、算距离、手算重心坐标。

### 4.1 Delaunay 类与构造过程

#### 4.1.1 概念说明

`Delaunay` 是用户直接打交道的高层类，它本身不做几何计算，只负责：

1. 校验并规整输入点（必须是二维浮点数组、不能是 masked array、不能含 NaN）。
2. 决定传给 Qhull 的命令行选项 `qhull_options`。
3. 用 `mode_option=b"d"`（`d` 表示 Delaunay 三角剖分）构造一个底层 `_Qhull` 对象，让 C 库 Qhull 干活。
4. 把 Qhull 算出来的结果（单纯形、邻接关系、共面点、超平面方程）拷成 NumPy 数组挂到自己身上。

真正「算」三角剖分的是 `_Qhull` 这个 Cython `cdef class`，它直接持有 Qhull 的 `qhT` 结构体指针 `_qh`，调用 C 库函数 `qh_new_qhull_scipy` 完成计算。这又是一处「Python 写易读逻辑、C/Cython 写性能路径」的分层（承接 u1-l2 的结论）。

#### 4.1.2 核心流程

构造一棵 `Delaunay` 的流程，伪代码如下：

```
Delaunay(points, furthest_site, incremental, qhull_options)
 │
 ├─ 校验：拒绝 masked array；np.ascontiguousarray(points, double)
 ├─ 若 points.ndim != 2：raise ValueError
 ├─ 确定 qhull_options：
 │     非 incremental 默认 "Qbb Qc Qz Q12"，且 ndim>=5 时追加 " Qx"
 │     incremental   默认 "Qc"（"Qz" 与增量模式不兼容）
 │     并强制 required_options="Qt"（保证输出是单纯形）
 ├─ qhull = _Qhull(b"d", points, qhull_options, required_options=b"Qt", ...)
 │        └─ 内部调用 C 库 qh_new_qhull_scipy(...)，失败抛 QhullError
 ├─ _QhullUser.__init__(self, qhull, incremental) → self._update(qhull)
 │        └─ Delaunay._update：
 │              qhull.triangulate()                       # 非单纯形面拆成单纯形
 │              paraboloid_scale/shift = get_paraboloid_shift_scale()
 │              simplices, neighbors, equations,
 │                  coplanar, good = get_simplex_facet_array()
 └─ 若 incremental：把 qhull 句柄保留在 self._qhull，否则立刻 close()
```

#### 4.1.3 源码精读

**用户入口 `Delaunay.__init__`** —— 校验输入、决定选项、起底层 `_Qhull`：

[_qhull.pyx:1870-1891](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L1870-L1891) 中文说明：这是构造函数。它先把点转成 C 连续 `float64`，再按是否增量模式、维数是否 ≥5 拼出默认 `qhull_options`，最后用 `b"d"`（Delaunay 模式）构造底层 `_Qhull`。

关键的默认选项逻辑：

[_qhull.pyx:1878-1886](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L1878-L1886) 中文说明：当用户不传 `qhull_options` 时，默认给 `"Qbb Qc Qz Q12"`（增量模式只给 `"Qc"`），维数 ≥5 时再追加 `" Qx"`。各选项含义见下方表格。

| 选项 | 含义 | 本讲相关性 |
| --- | --- | --- |
| `Qbb` | 对最后一维（抛物面提升维）做尺度缩放，改善高维数值稳定性 | 对应 `paraboloid_scale` |
| `Qc` | 记录「共面/未参与三角剖分」的点，填进 `coplanar` 属性 | 直接决定 `coplanar` 是否有内容 |
| `Qz` | 添加一个「无穷远」点，处理退化情形 | 增量模式禁用，因为与 `Qz` 不兼容 |
| `Qx` | 高维（≥5）时用精确合并，提升鲁棒性 | 仅 `ndim>=5` 默认开 |
| `Q12` | 允许 Qhull 合并近似 cocircular 的面 | 减少数值噪声导致的退化 |
| `Qt` | 强制把所有面三角化成单纯形（`required_options`，必开） | 保证 `simplices` 每行都是单纯形 |
| `QJ` | 给输入加随机扰动以消除退化 | 让每个点都出现在三角剖分里 |

**底层 `_Qhull.__init__`** —— 真正调 C 库的地方：

[_qhull.pyx:340-357](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L340-L357) 中文说明：在 `with nogil` 块里分配 `qhT` 结构、调 `qh_zero` 初始化，再调 `qh_new_qhull_scipy` 跑 Qhull。退出码非 0 就把 Qhull 的错误信息收集起来、释放资源、抛 `QhullError`。注意增量模式下有一组「坏选项」校验（`Qbb/Qbk/Qz` 等不兼容），见 [_qhull.pyx:302-316](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L302-L316)。

**结果提取 `Delaunay._update`** —— 把 C 库的链表结构拷成数组：

[_qhull.pyx:1895-1909](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L1895-L1909) 中文说明：先 `triangulate()`（把非单纯形面拆成三角形），再取抛物面缩放/平移，再一次性从 Qhull 拿回 `simplices`、`neighbors`、`equations`、`coplanar`、`good` 五张数组，最后调父类 `_QhullUser._update` 补上 `points`/`min_bound`/`max_bound`。

> 链路回顾：`Delaunay.__init__` → `_Qhull(...)`（C 库算凸包）→ `_QhullUser.__init__` → `Delaunay._update` → `get_simplex_facet_array`。后面 u10 会专门剖析 `_Qhull`/`_QhullUser` 的封装机制与资源生命周期，本讲只用到结论。

#### 4.1.4 代码实践

**实践目标**：亲手跑一遍构造过程，观察默认 `qhull_options` 与报错行为。

**操作步骤**（命令行或脚本均可）：

```python
# 示例代码：观察 Delaunay 构造
import numpy as np
from scipy.spatial import Delaunay

points = np.array([[0, 0], [0, 1.1], [1, 0], [1, 1]])
tri = Delaunay(points)

print("ndim      =", tri.ndim)
print("nsimplex  =", tri.nsimplex)
print("simplices =\n", tri.simplices)
```

再故意触发 `QhullError` 与 `ValueError`，观察 `qhull_options` 的作用：

```python
# 示例代码：观察退化点集与 QJ 选项
# 1) 重复点 → coplanar 现象（不报错，但点 4 不进 simplices）
dup = np.array([[0, 0], [0, 1], [1, 0], [1, 1], [1, 1]])
tri_dup = Delaunay(dup)
print("coplanar =", tri_dup.coplanar)          # [[4, 0, 3]]

# 2) 用 QJ 强制每个点都出现
tri_qj = Delaunay(dup, qhull_options="QJ Pp")
print("with QJ, unique vertices =", np.unique(tri_qj.simplices))

# 3) 共线三点 → QhullError（二维退化，无法三角剖分）
try:
    Delaunay(np.array([[0, 0], [1, 1], [2, 2.0]]))
except Exception as e:
    print(type(e).__name__, ":", str(e)[:60])
```

**需要观察的现象**：
- 默认构造得到 `nsimplex == 2`（单位正方形被一条对角线切成两个三角形）。
- 重复点版本里 `coplanar` 形如 `[[4, 0, 3]]`，说明点 4 被判为共面、挂在单纯形 0 的顶点 3 附近。
- 加 `QJ` 后 `np.unique(...)` 包含 4，即点 4 也成了顶点（代价是出现零面积退化三角形，见教程 rst 第 109–125 行）。
- 共线三点会抛 `QhullError`（提示 Qhull 因退化报错）。

**预期结果**：与上面注释一致。若你的 SciPy 版本/平台不同，`simplices` 的行列顺序可能略有差异（官方文档明确标注「may vary」），但点集组成不变。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `incremental=True` 时默认选项去掉了 `Qz`？

**参考答案**：`Qz` 会往输入里加一个「在无穷远处」的虚拟点来处理退化，但增量模式要不断追加新点，虚拟点的存在会让点 ID 管理与追加语义变混乱。源码在 [_qhull.pyx:302-308](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L302-L308) 明确把 `Qbb/Qbk/QBk/QbB/Qz` 列为增量模式的「坏选项」，传了就 `raise ValueError`。

**练习 2**：`required_options=b"Qt"` 在 [_qhull.pyx:1889](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L1889) 强制传入，它保证了什么？

**参考答案**：`Qt` 让 Qhull 把所有（可能非单纯形的）面都三角化成单纯形。没有它，高维或退化情形下面可能有多于 \(d+1\) 个顶点，`simplices` 就不再是「每行 \(d+1\) 个点」的整齐数组，后续 `neighbors`/`transform` 的索引假设都会失效。注意 [_qhull.pyx:296-300](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L296-L300) 还有一处：若用户同时传了 `QJ`，会把 `Qt` 从必需项里移除（因为 `QJ` 本身就保证单纯形输出）。

---

### 4.2 simplices / neighbors / coplanar 编码方案

#### 4.2.1 概念说明

构造完 `Delaunay` 后，最有用的信息都编码在三张数组里：

- `simplices`：每个单纯形由哪几个点的索引组成。
- `neighbors`：每个单纯形的每个「面」对面是哪个单纯形。
- `coplanar`：哪些输入点因为数值精度没进三角剖分，以及它们「贴近」哪个单纯形、哪个顶点。

读懂它们的存储约定，是用好 Delaunay 的前提。这一节的存储方案与 `ConvexHull`、`Voronoi` 完全同构，所以在这里讲透，后面两讲可以复用。

#### 4.2.2 核心流程与编码约定

**simplices**：形状 `(nsimplex, ndim+1)`，第 `i` 行是第 `i` 个单纯形的 \(d+1\) 个顶点的**点索引**。二维时（`ndim=2`）每行 3 个点，且按逆时针方向排列（见类 docstring 第 1716–1718 行）。

**neighbors**：形状 `(nsimplex, ndim+1)`。关键约定——**第 `k` 个邻居正对着第 `k` 个顶点**，也就是说，邻居 `neighbors[i, k]` 是「去掉顶点 `simplices[i, k]` 之后剩下的那个面」所对的相邻单纯形。边界上的面没有邻居，记为 `-1`。这是定位算法 `_find_simplex_directed` 能「沿着负的重心坐标方向跳到更近的邻居」的基础。

**coplanar**：形状 `(ncoplanar, 3)`，每行 `(point_idx, simplex_idx, vertex_idx)`，意为输入点 `point_idx` 没有成为顶点，而是贴近单纯形 `simplex_idx`、离它最近的顶点是 `vertex_idx`。只有开了 `Qc` 才会填充（默认就开）。教程 rst 第 80–104 行用一个重复点的例子完整演示了它。

一条几何直觉：coplanar 几乎总是「数值精度」造成的，不是真几何共面。重复点、近似共圆的点都可能触发。要强行让每个点都出现，用 `QJ`（随机扰动）。

#### 4.2.3 源码精读

**`get_simplex_facet_array` 产出这五张数组**：

[_qhull.pyx:564-580](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L564-L580) 中文说明：函数 docstring 写清了返回的 `facets`/`neighbors`/`equations` 的形状与「第 k 个邻居对着第 k 个顶点」「无穷远用 -1」等约定，这正是 4.2.2 节编码方案的权威出处。

`facets` 与 `neighbors` 是在同一个 `with nogil` 大循环里一起填的，逐个面遍历 Qhull 的 `facet_list`：

[_qhull.pyx:673-686](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L673-L686) 中文说明：对每个面的 `ndim+1` 个顶点，依次把顶点点索引写入 `facets[j,i]`，把对应邻居的面索引（经 `id_map` 重映射）写入 `neighbors[j,i]`；随后把面法向量与偏移写入 `equations`。两者按同一列下标 `i` 对齐，从而保证「第 i 列的邻居对着第 i 列的顶点」。

`coplanar` 在同一个循环里、靠 `facet.coplanarset` 收集：

[_qhull.pyx:689-705](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L689-L705) 中文说明：若该面挂了共面点集合，就逐个取出共面点 `point`、用 `qh_nearvertex` 找离它最近的顶点 `vertex`，把三元组 `(点 id, 面 id, 顶点 id)` 追加进 `coplanar` 数组（`coplanar[ncoplanar, 0/1/2]`）。

最终返回五元组：

[_qhull.pyx:713](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L713) 中文说明：`return facets, neighbors, equations, coplanar[:ncoplanar], good`，分别对应 `Delaunay` 的 `simplices`/`neighbors`/`equations`/`coplanar`/`good` 属性（赋值见 [_qhull.pyx:1901-1902](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L1901-L1902)）。

**官方教程对 `simplices`/`neighbors` 的最小演示**（与本讲实践直接相关）：

[doc/source/tutorial/spatial.rst:49-73](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/doc/source/tutorial/spatial.rst#L49-L73) 中文说明：用 4 个点 `[[0,0],[0,1.1],[1,0],[1,1]]` 构造 Delaunay，展示 `tri.simplices[1] = [3,1,0]`、`tri.neighbors[1] = [-1,0,-1]`，并解释「邻居 0 正对着顶点 1」。这就是 4.2.2 编码约定的最小说明。

**coplanar 的教程演示**：

[doc/source/tutorial/spatial.rst:80-104](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/doc/source/tutorial/spatial.rst#L80-L104) 中文说明：5 个点（最后一个重复）构造 Delaunay，`np.unique(tri.simplices)` 不含 4，`tri.coplanar = [[4,0,3]]`，解释点 4 贴近三角形 0、顶点 3。

#### 4.2.4 代码实践

**实践目标**：用测试集里的「单位正方形」数据，亲手把 `simplices` 与 `neighbors` 的「第 k 个邻居对着第 k 个顶点」约定验证一遍。

**操作步骤**：

```python
# 示例代码：验证 neighbors 约定
import numpy as np
from scipy.spatial import Delaunay

# 与 tests/test_qhull.py:178 一致的点集
points = np.array([(0,0), (0,1), (1,1), (1,0)], dtype=np.float64)
tri = Delaunay(points)
print(tri.simplices)   # [[1 3 2], [3 1 0]]（顺序可能 may vary）

i = 0
for k in range(3):
    nb = tri.neighbors[i, k]
    v  = tri.simplices[i, k]
    if nb == -1:
        print(f"面 k={k}（去掉顶点 {v}）在边界，无邻居")
    else:
        # 邻居单纯形应包含本单纯形除顶点 v 外的另外两个点
        shared = set(tri.simplices[i].tolist()) - {int(v)}
        assert shared.issubset(set(tri.simplices[nb].tolist()))
        print(f"面 k={k}（去掉顶点 {v}）→ 邻居 {nb}，共享点 {sorted(shared)}")
```

**需要观察的现象**：对每个 `k`，邻居单纯形（若非 -1）都恰好包含本单纯形去掉 `simplices[i,k]` 后剩下的两个点，印证「第 k 个邻居对着第 k 个顶点」。边界面对应 `-1`。

**预期结果**：四个点构成的单位正方形被切成两个三角形，`tri.simplices` 共 2 行；其中一边贴边界 → 两个 `-1`；另一边两三角形相邻 → 互为邻居。具体行列顺序「may vary」，但上述共享点断言一定成立。

> 依据：`tests/test_qhull.py` 的 `test_find_simplex`（[_qhull.pyx 测试 test_qhull.py:176-197](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/test_qhull.py#L176-L197)）正是用这组点，并断言 `tri.simplices == [[1,3,2],[3,1,0]]`。

#### 4.2.5 小练习与答案

**练习 1**：`neighbors` 数组里 `-1` 的物理含义是什么？为什么 `_find_simplex_directed` 看到 `-1` 就能断定「点在三角剖分之外」？

**参考答案**：`-1` 表示该面在凸包边界上、外面没有相邻单纯形。因为 Delaunay 三角剖分覆盖的是点集的凸包，是个**凸**区域；定向游走时若某重心坐标为负、对应的邻居又是 `-1`，说明点已经越过凸包边界，必然在外部。源码注释见 [_qhull.pyx:1399-1401](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L1399-L1401)。

**练习 2**：`coplanar` 数组第二列是单纯形索引还是顶点索引？第三列呢？

**参考答案**：第二列是**单纯形索引**（`id_map[facet.id]`），第三列是**顶点点索引**（`qh_pointid(vertex.point)`）。三元组含义为 `(共面点, 最近单纯形, 最近顶点)`，见 [_qhull.pyx:702-704](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L702-L704)。

---

### 4.3 find_simplex / plane_distance / transform / tsearch

#### 4.3.1 概念说明

构造好三角剖分后，最常见的需求是「给我一个新点，告诉我它落在哪个三角形里」。`Delaunay` 提供了直接相关的四件套：

- `find_simplex(xi)`：返回 `xi` 所在单纯形的索引；在外部返回 `-1`。这是核心。
- `plane_distance(xi)`：把 `xi` 提升到抛物面后，计算它到**每个**单纯形所在超平面的有符号距离。诊断用。
- `transform`：每个单纯形的「重心坐标仿射变换矩阵」，既被 `find_simplex` 内部用来判断内外，也可以让你**自己**手算任意点的重心坐标。
- `tsearch(tri, xi)`：模块级函数，等价于 `tri.find_simplex(xi)`，但**自 1.18.0 起弃用**（计划 1.22.0 移除），新代码请用 `find_simplex`。

#### 4.3.2 核心流程

**`find_simplex` 的两段式算法**（来自 Qhull 的 `qh_findbestfacet` 思路）：

```
find_simplex(xi):
 1) 先粗定位（_find_simplex）：
      - 把 xi 提升到抛物面 z = lift(xi)
      - 从某个起始单纯形出发，沿邻居走，找第一个「超平面距离为正」的面
        （正距离 ⟺ 提升点在该面正侧 ⟺ 投影点可能在对应三角形内）
 2) 再精定位（_find_simplex_directed）：
      - 在原 N 维空间里，计算 xi 在当前单纯形的重心坐标 c
      - 若某个 c[k] < -eps：说明 xi 在「第 k 个面」的外侧，
        跳到邻居 neighbors[cur, k] 继续找；若该邻居是 -1，则在外部，返回 -1
      - 若所有 c[k] ∈ [-eps, 1+eps]：找到，返回当前单纯形
      - 遇到退化（变换含 NaN）或循环：回退到 _find_simplex_bruteforce 暴力遍历
```

关键数学：在单纯形内当且仅当所有重心坐标 \(c_i \in [0,1]\)（容差 \(\varepsilon\)）。重心坐标由仿射变换 \(T c = x - r_n\) 给出，其中 \(T\) 的列是顶点 \(r_j - r_n\)。

**`transform` 的布局**：形状 `(nsimplex, ndim+1, ndim)`。

- `transform[i, :ndim, :ndim]` = \(T^{-1}\)（矩阵 \(T\) 的逆）。
- `transform[i, ndim, :]` = \(r_n\)（最后一个顶点）。
- 前 `ndim` 个重心坐标：\(c_{0:d} = T^{-1}(x - r_n)\)。
- 第 \(d+1\) 个：\(c_d = 1 - \sum_{j<d} c_j\)（保证和为 1）。
- 退化（近似奇异）单纯形的 `transform` 行为全 `NaN`，对应 [_qhull.pyx:1194-1197](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L1194-L1197)。

`tsearch` 只是套了弃用警告再调 `find_simplex`：

[_qhull.pyx:2261-2264](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L2261-L2264) 中文说明：发 `DeprecationWarning` 后 `return tri.find_simplex(xi)`，别无他事。

#### 4.3.3 源码精读

**`find_simplex` 入口**（Python 可见方法）：

[_qhull.pyx:2074-2153](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L2074-L2153) 中文说明：整段方法。它把任意形状的 `xi` 拍平成 `(N, ndim)`，定下容差 `eps=100*macheps`、`eps_broad=sqrt(eps)`，逐点在 `with nogil` 里调 `_find_simplex`（或 `bruteforce=True` 时调 `_find_simplex_bruteforce`），把结果填进 `out_` 再 reshape 回去。

容差与回退设定：

[_qhull.pyx:2128-2135](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L2128-L2135) 中文说明：默认 `tol=None` 时 `eps = 100 * np.finfo(np.double).eps`；同时 `_get_delaunay_info` 把 `self` 的各数组指针打包成一个 `DelaunayInfo_t` 结构体传进 C 层，避免反复 Python 取属性。

**核心算法 `_find_simplex`**（抛物面游走 + 定向搜索）：

[_qhull.pyx:1474-1583](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L1474-L1583) 中文说明：先用 `_lift_point` 把查询点抬到抛物面，从起始单纯形出发，沿邻居找「超平面距离变正」的方向粗跳（`while changed` 循环，带 `eps` 防止浮点不终止，注释见 1563–1568 行），跳到正侧后把起点交给 `_find_simplex_directed` 做精定位。

**定向搜索 `_find_simplex_directed`**（重心坐标驱动的跳跃）：

[_qhull.pyx:1373-1472](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L1373-L1472) 中文说明：算法思路在 1382–1412 行的 docstring 里讲得很清楚——第 k 个邻居对着第 k 个顶点，若第 k 个重心坐标为负，就跳到该邻居；迭代次数封顶 `1 + nsimplex//4`，超时或遇退化回退到 `_find_simplex_bruteforce`。

**重心坐标计算 `_barycentric_coordinates`**（`transform` 的使用方）：

[_qhull.pyx:1258-1271](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L1258-L1271) 中文说明：逐坐标 \(c_i = \sum_j T^{-1}_{ij}(x_j - r_{n,j})\)，最后一个 \(c_d = 1 - \sum_{j<d} c_j\)，正是 4.3.2 节的公式。

**`transform` 属性与 `_get_barycentric_transforms`**：

[_qhull.pyx:1918-1940](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L1918-L1940) 中文说明：`transform` 是惰性属性，首次访问才调 `_get_barycentric_transforms` 预算好所有单纯形的 \(T^{-1}\) 与 \(r_n\)，缓存到 `self._transform`。

[_qhull.pyx:1100-1132](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L1100-L1132) 中文说明：`_get_barycentric_transforms` 的 docstring，严格定义了 \(T c = x - r_n\)、\(T_{ij} = (r_j - r_n)_i\)，并说明返回的 `Tinvs[i,:ndim,:ndim]` 存 \(T^{-1}\)、`Tinvs[i,ndim,:]` 存 \(r_n\)。它用 LAPACK 的 `dgetrf`（LU 分解）、`dgecon`（条件数估计）、`dgetrs`（回代）求逆，并用 `rcond_limit = 1000*eps` 判定退化（见 [_qhull.pyx:1156-1197](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L1156-L1197)）。

**`plane_distance`**：

[_qhull.pyx:2155-2189](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L2155-L2189) 中文说明：对每个查询点先 `_lift_point` 提升到抛物面，再对每个单纯形用 `_distplane`（[_qhull.pyx:1286-1296](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L1286-L1296)，即 `normal·point + offset`）算有符号距离。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：对一组平面点构造 Delaunay，找出包含某查询点的单纯形，并**手算**验证该查询点的重心坐标之和为 1。这正是本讲规格里要求的实践任务。

**操作步骤**（沿用类 docstring 第 1855–1862 行给出的标准做法）：

```python
# 示例代码：定位点 + 手算重心坐标
import numpy as np
from scipy.spatial import Delaunay

points = np.array([[0, 0], [0, 1.1], [1, 0], [1, 1]])
tri = Delaunay(points)

# 1) 定位一个内部点
p = np.array([0.1, 0.2])
i = tri.find_simplex(p)
print("查询点", p, "落在单纯形", i, "，其顶点为", tri.simplices[i])

# 2) 用 transform 手算重心坐标（标准写法，见类 docstring）
b = tri.transform[i, :2].dot(p - tri.transform[i, 2])   # 前 2 个坐标
c = np.append(b, 1 - b.sum())                            # 第 3 个 = 1 - 其余和
print("重心坐标 c =", c)
print("c 之和 =", c.sum())                               # 期望 1.0

# 3) 反过来用重心坐标重构原点，验证仿射组合
verts = points[tri.simplices[i]]
x_reconstructed = c @ verts
print("重构点 =", x_reconstructed, " 原点 =", p)

# 4) 一个外部点应返回 -1
print("外部点 find_simplex =", tri.find_simplex(np.array([1.5, 0.5])))  # -1
```

**需要观察的现象**：
- `find_simplex` 对内部点返回一个 `≥ 0` 的单纯形索引，对外部点返回 `-1`。
- 手算的重心坐标 `c` 三个分量之和**精确等于 1**（误差在 1e-16 量级）。
- 用 `c` 对三个顶点做加权求和，能精确重构出原查询点 `p`。
- 若把 `p` 换成某条边上的点，会有一个重心坐标正好为 0。

**预期结果**：以 `p = (0.1, 0.2)` 为例，会落在某个单纯形（如 `[3,1,0]`，顺序 may vary），其重心坐标三个分量都为正、和为 1，重构点与原点一致。注意：因为 `simplices` 顺序「may vary」，`tri.simplices[i]` 的具体数字可能与本讲写的不同，但「重心坐标和为 1」「重构等于原点」两条不变式恒成立——这两条才是你要验证的本质。

> 依据：类 docstring 在 [_qhull.pyx:1855-1862](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L1855-L1862) 给出了完全相同的 `transform` 手算写法；`test_find_simplex`（[test_qhull.py:176-197](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/test_qhull.py#L176-L197)）断言 `(0.25,0.25)→1`、`(0.75,0.75)→0`、`(0.3,0.2)→1`，可作为你定位结果的对照。

#### 4.3.5 小练习与答案

**练习 1**：`find_simplex` 默认容差 `tol=None` 对应多大的 `eps`？为什么还需要一个更大的 `eps_broad`？

**参考答案**：`eps = 100 * np.finfo(np.float64).eps`（见 [_qhull.pyx:2128-2132](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L2128-L2132)），`eps_broad = sqrt(eps)`。`eps_broad` 用在 `_find_simplex_bruteforce` 处理**退化单纯形**（`transform` 为 NaN）时——此时无法直接算该单纯形的重心坐标，只能借邻居的重心坐标并朝退化方向放宽容差（`-eps_broad <= c[m] <= 1+eps`），见 [_qhull.pyx:1361](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L1361)。

**练习 2**：`plane_distance` 返回的距离是在原 2D 平面上，还是在提升后的 3D 抛物面上？为什么「正距离」能用于定位？

**参考答案**：在**提升后的抛物面空间**。`_distplane` 用的是凸包面的法向（`equations`），输入点先经 `_lift_point` 抬到抛物面（[_qhull.pyx:2186-2187](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L2186-L2187)）。因为 Delaunay 三角形 = 下凸包面的投影，提升点在某面**正侧**（距离 > 0）正是「投影点落在该三角形内或其上方」的代数刻画，这正是 `_find_simplex` 粗定位的依据（[_qhull.pyx:1554](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L1554)）。注意 docstring（1499–1523 行）也提醒：正距离最大的面**未必**是包含点的那个面，所以正距离只用来「粗跳」，精确定位仍靠重心坐标的定向搜索。

**练习 3**：为什么新代码不该再用 `tsearch`？给出它的替代写法。

**参考答案**：`tsearch` 自 1.18.0 弃用，计划 1.22.0 移除（[_qhull.pyx:2213-2216](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L2213-L2216)）。替代写法：把 `tsearch(tri, xi)` 换成 `tri.find_simplex(xi)`。两者行为完全一致，`tsearch` 内部就是发个 `DeprecationWarning` 再转发（[_qhull.pyx:2261-2264](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L2261-L2264)）。

---

## 5. 综合实践

把本讲三块知识串起来：构造 → 读懂编码 → 定位与重心坐标。

**任务**：在单位正方形 \([0,1]^2\) 内撒 20 个随机点，构造 Delaunay，完成下面四件事，并把结论写进一张小报告。

1. 统计 `nsimplex`、`coplanar.shape[0]`，说明为什么三角形的个数大约是点数的两倍（Euler 公式：平面三角剖分里 \(F \approx 2n - 2 - b\)，\(n\) 为点数、\(b\) 为凸包边界点数）。
2. 任选一个内部查询点 `q`，用 `find_simplex` 定位，再用 `plane_distance(q)` 看「包含它的单纯形」对应的距离符号——确认它是正的，并与 docstring 第 1499–1523 行的提醒对照（正距离最大的不一定就是包含它的那个）。
3. 用 `transform` 手算 `q` 的重心坐标，验证三条不变式：分量之和为 1；所有分量 \(\ge -\varepsilon\)（说明确实在内部）；用重心坐标对顶点加权求和能重构 `q`。
4. 构造一个**凸包外部**的点，确认 `find_simplex` 返回 `-1`，并解释：定向游走在哪一步、靠什么信号判定「在外部」？

**参考脚本骨架**（示例代码）：

```python
import numpy as np
from scipy.spatial import Delaunay

rng = np.random.default_rng(0)
pts = rng.random((20, 2))
tri = Delaunay(pts)

print("nsimplex =", tri.nsimplex, " coplanar =", tri.coplanar.shape[0])

q = np.array([0.5, 0.5])
i = tri.find_simplex(q)
print("simplex of q =", i)

# 重心坐标与不变式
b = tri.transform[i, :2].dot(q - tri.transform[i, 2])
c = np.append(b, 1 - b.sum())
print("c =", c, " sum =", c.sum(),
      " all>=-eps =", np.all(c >= -1e-9),
      " reconstruct ok =", np.allclose(c @ pts[tri.simplices[i]], q))

# 外部点
print("outside ->", tri.find_simplex(np.array([1.5, 1.5])))   # -1
```

**判定标准**：
- `c.sum()` 等于 1（误差 ~1e-16）；`c` 全部非负；重构点与 `q` 一致。
- 外部点返回 `-1`，你的解释应提到：定向游走时某个重心坐标为负、而对应邻居是 `-1`（凸包边界），故判定在外部（依据 [_qhull.pyx:1438-1445](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L1438-L1445)）。

> 若运行结果与你预期不符，先怀疑浮点顺序差异（`simplices` 顺序 may vary），再怀疑理解。本实践不要求你改源码，只需读与算。

## 6. 本讲小结

- **Delaunay 三角剖分**把点集切成满足「空外接圆」的单纯形集合；几何上等价于把点抬到抛物面 \(z=\|x\|^2\) 后求**下凸包**再投影回去，Qhull 就是这么算的。
- `scipy.spatial.Delaunay` 是高层壳，真正算的是底层 `_Qhull`（持有 C 库 `qhT`），构造流程：`Delaunay.__init__` → `_Qhull(b"d", ...)` → `Delaunay._update` → `get_simplex_facet_array`。
- `qhull_options` 决定鲁棒性行为：默认 `Qbb Qc Qz Q12`（≥5 维加 `Qx`），强制 `Qt` 保证单纯形输出；`Qc` 让 `coplanar` 有内容，`QJ` 强制每个点都出现。
- `simplices`/`neighbors` 的关键约定：**第 k 个邻居正对着第 k 个顶点**，边界用 `-1`；`coplanar` 每行 `(点, 单纯形, 顶点)` 记录未参与剖分的点。
- `find_simplex` 用「抛物面粗游走 + 重心坐标定向搜索」定位点，外部返回 `-1`；`plane_distance` 给出提升点到各面的有符号距离；`transform` 存 \(T^{-1}\) 与 \(r_n\)，可手算重心坐标（和为 1）。
- `tsearch` 已弃用（1.22.0 移除），等价于 `find_simplex`，新代码勿用。

## 7. 下一步学习建议

- **紧接着读 [u3-l2 ConvexHull 凸包](u3-l2-convex-hull.md)**：`ConvexHull` 与 `Delaunay` 共用 `_Qhull`/`_QhullUser` 与同一套 `get_simplex_facet_array`，只是 `mode_option` 不同（凸包用默认而非 `b"d"`），`vertices`/`simplices`/`equations` 的存储方案与本章完全同构，学起来会很轻。
- **再读 [u3-l3 Voronoi 图](u3-l3-voronoi-diagram.md)**：Voronoi 是 Delaunay 的**对偶**，`get_voronoi_diagram` 复用同一个 `_Qhull` 对象，你会看到 `-1` 表示无穷远点的另一套编码。
- **想深入查找算法**：回看本讲引用的 `_find_simplex`/`_find_simplex_directed`（[_qhull.pyx:1373-1583](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L1373-L1583)），并对照 `tests/test_qhull.py` 的 `test_find_simplex`/`test_plane_distance` 理解边界行为。
- **想深入封装机制**：u10 会剖析 `_Qhull` 的资源生命周期（`__dealloc__`/`close`）与 `_QhullUser` 的增量更新框架，把本讲「只用到结论」的部分彻底讲透。
