# ConvexHull 凸包

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚「凸包（convex hull）」的几何含义，以及为什么它是空间几何里最高频的基础结构之一。
- 会用 `scipy.spatial.ConvexHull` 构造一个点集的凸包，并读懂它返回的几组数组：`points`、`vertices`、`simplices`、`neighbors`、`equations`、`coplanar`、`good`。
- 理解 `equations` 这组「超平面方程」的编码方式与符号约定，能用它判定一个点是否落在凸包内部。
- 理解 `volume` 与 `area` 两个标量属性，包括它们在二维与高维下的不同含义。
- 顺着 `ConvexHull → _QhullUser → _Qhull` 的调用链，看懂高层 Python/Cython 类如何把工作下放给底层 Qhull C 库。

本讲只讲 **凸包**。Delaunay 三角剖分已在 u3-l1 讲过，Voronoi 图留给 u3-l3。

## 2. 前置知识

### 2.1 什么是凸包

给定平面（或高维空间）里的一堆点，**凸包**就是「能把这些点全部包住的最小凸形」。

- 「凸」的意思是：形内任意两点连一条直线段，整段都不会跑到形外。
- 一个直观的物理模型：把每个点想象成钉在木板上的钉子，用一根橡皮筋套住所有钉子再松手，橡皮筋绷紧后围出的多边形就是这些点的二维凸包。

数学上，点集 \(S\) 的凸包定义为：

\[
\mathrm{conv}(S)=\left\{\sum_{i=1}^{m}\lambda_i x_i\ \middle|\ m\ge 1,\ \lambda_i\ge 0,\ \sum_{i=1}^{m}\lambda_i=1,\ x_i\in S\right\}
\]

也就是所有「把 \(S\) 中的点做凸组合（加权平均，权重非负且和为 1）」能得到的点。落在凸包边界最外侧、真正「撑起」橡皮筋的那些点叫**顶点（vertices）**；内部的点对凸包形状没有贡献。

### 2.2 超平面方程（hyperplane equation）

在 \(d\) 维空间里，一个**超平面**是 \(d-1\) 维的「平直切片」，可以用一个法向量 \(\mathbf{n}\) 和一个偏移 \(b\) 写成：

\[
\mathbf{n}\cdot \mathbf{x}+b=0
\]

凸包的每个「面（facet）」都落在一个超平面上。Qhull 会为每个面给出一组 \([\mathbf{n},\ b]\)，这就是 `equations` 数组。把任意点 \(\mathbf{x}\) 代入 \(\mathbf{n}\cdot\mathbf{x}+b\)，得到的**有符号值**就能告诉我们这个点相对该面在「内侧 / 面上 / 外侧」。

### 2.3 与上一讲的衔接

u3-l1 已经讲过：`scipy.spatial` 里 `Delaunay`、`ConvexHull`、`Voronoi`、`HalfspaceIntersection` 都是高层「壳类」，它们都继承自 `_QhullUser`，真正的几何计算由底层 `_Qhull`（持有 Qhull C 库的 `qhT` 结构）完成。本讲把这条链路在凸包场景下走一遍。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`_qhull.pyx`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx) | Cython 实现。含 `ConvexHull` 高层类、`_QhullUser` 基类、底层 `_Qhull`（封装 Qhull C 库）。本讲重点读 `ConvexHull` 及其调用的 `get_simplex_facet_array`、`volume_area`、`get_extremes_2d`。 |
| [`_plotutils.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_plotutils.py) | 纯 Python 绘图助手。`convex_hull_plot_2d` 用来把二维凸包画出来。 |
| [`tests/test_qhull.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/test_qhull.py) | 测试。`TestConvexHull` 里有 cube 的体积/面积、二维顶点逆时针序等断言，可作行为基准。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 ConvexHull 类**：构造参数、与底层 `_Qhull` 的协作、`_update` 增量更新框架。
- **4.2 vertices / simplices / equations**：三组结果数组的编码方式，重点讲超平面方程的符号约定。
- **4.3 volume / area**：体积与面积（含二维特殊含义）的来源。

### 4.1 ConvexHull 类

#### 4.1.1 概念说明

`ConvexHull` 是用户直接使用的入口类。它的职责很薄：

1. 校验并把输入点转成 Qhull 需要的格式（连续内存的 `float64`）。
2. 拼装 `qhull_options`（传给 Qhull C 库的选项字符串）。
3. 起一个底层 `_Qhull` 对象，让它真正去算凸包。
4. 把底层算出的结果数组「搬」到自己的属性上（`simplices`、`equations`、`volume`、`area` 等）。

真正干活的不是 `ConvexHull` 自己，而是它继承链上的 `_QhullUser`（管生命周期与增量更新）和 `_Qhull`（封装 C 库）。这种「薄壳 + 厚内核」的分层在 `scipy.spatial` 里反复出现。

#### 4.1.2 核心流程

构造一个 `ConvexHull` 的执行链可以概括为：

```text
ConvexHull(points, incremental, qhull_options)
   │
   ├── 校验：拒绝 masked array；ascontiguousarray(..., float64)
   ├── 拼 qhull_options：默认 ndim>=5 时加 "Qx"；始终强制 "Qt"
   │
   ├── qhull = _Qhull(b"i", points, qhull_options, required_options=b"Qt", incremental)
   │        └── 调用 Qhull C 库，mode_option=b"i" 表示「普通凸包」
   │
   └── _QhullUser.__init__(qhull, incremental)
            └── self._update(qhull)          # ConvexHull._update 覆写了它
                    ├── qhull.triangulate()              # 把所有面切成单纯形
                    ├── qhull.get_simplex_facet_array()  # 取 simplices/neighbors/equations/coplanar/good
                    ├── qhull.volume_area()              # 取 volume/area
                    ├── (二维) qhull.get_extremes_2d()   # 取逆时针顶点序
                    └── _QhullUser._update(self, qhull)  # 取 points/ndim/npoints/bounds
```

几个关键点先建立直觉：

- **`mode_option=b"i"`**：这是告诉 Qhull 「我要算凸包、并按面索引输出」。对比一下，Delaunay 用的是 `b"d"`（lifting 到抛物面求凸包），Voronoi 用 `b"v"`，半空间求交用 `b"H"`。底层 `_Qhull` 会据此置位 `_is_delaunay` / `_is_halfspaces` 等标志，影响后续数组提取的维度计算。
- **`Qt` 永远开启**：Qhull 在高维或退化输入下可能返回非单纯形的面（比如三维里的六边形面）。`Qt` 强制把所有面**三角剖分**成单纯形，保证 `simplices` 每一行都是一个固定顶点数的单纯形，数组形状规整。
- **`incremental`**：若为 `True`，底层 `_Qhull` 对象会被保留（`self._qhull = qhull`），之后可以 `add_points` 增量加点；否则算完就 `close()` 释放 C 资源。

#### 4.1.3 源码精读

**构造函数** [_qhull.pyx:2486-2501](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L2486-L2501)：

```python
def __init__(self, points, incremental=False, qhull_options=None):
    if np.ma.isMaskedArray(points):
        raise ValueError('Input points cannot be a masked array')
    points = np.ascontiguousarray(points, dtype=np.double)

    if qhull_options is None:
        qhull_options = b""
        if points.shape[1] >= 5:
            qhull_options += b"Qx"
    else:
        qhull_options = qhull_options.encode('latin1')

    # Run qhull
    qhull = _Qhull(b"i", points, qhull_options, required_options=b"Qt",
                   incremental=incremental)
    _QhullUser.__init__(self, qhull, incremental=incremental)
```

逐行看：

- 拒绝 `masked array`（缺测值对几何无意义），并把点转成 C 连续的 `float64`（Qhull C 库要求连续内存）。
- `qhull_options is None` 时给默认值：维度 ≥ 5 追加 `Qx`（高维下用精确的合并算法，避免数值退化），否则空串。
- `required_options=b"Qt"` 保证三角化输出，**用户无法关掉**。
- 最后把 `qhull` 交给父类 `_QhullUser.__init__`，由它触发 `_update` 并决定是否保留底层对象。

**`_update` 方法**（ConvexHull 自己覆写的版本） [_qhull.pyx:2503-2534](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L2503-L2534)：

```python
def _update(self, qhull):
    qhull.triangulate()

    self.simplices, self.neighbors, self.equations, self.coplanar, self.good = \
                   qhull.get_simplex_facet_array()
    ...
    self.volume, self.area = qhull.volume_area()

    if qhull.ndim == 2:
        self._vertices = qhull.get_extremes_2d()
    else:
        self._vertices = None

    self.nsimplex = self.simplices.shape[0]
    _QhullUser._update(self, qhull)
```

可以看到所有「结果属性」都来自底层 `qhull` 的四个方法：`get_simplex_facet_array`、`volume_area`、`get_extremes_2d`（仅二维），以及父类的 `_update`（取 `points/ndim/npoints/min_bound/max_bound`）。增量加点时，这套 `_update` 会被重新调用，属性随之刷新。

**父类 `_QhullUser`** [_qhull.pyx:1590-1628](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L1590-L1628) 负责生命周期：`incremental=True` 才保留 `self._qhull`，否则立即 `qhull.close()`；`__del__` 与 `close()` 会释放底层 C 资源。`_QhullUser._update`（[第 1625-1630 行](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L1625-L1630)）把 `points`、维度、点数、坐标范围挂到实例上。

#### 4.1.4 代码实践

**实践目标**：亲手构造一个凸包，确认输入校验与 `qhull_options` 默认行为。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.spatial import ConvexHull

# 1) 普通二维点集
points = np.array([[0, 0], [4, 0], [4, 3], [0, 3], [2, 1.5]])
hull = ConvexHull(points)
print("ndim      =", hull.ndim)
print("npoints   =", hull.npoints)
print("nsimplex  =", hull.nsimplex)   # 二维下 = 边数

# 2) 触发输入校验：masked array 应抛 ValueError
try:
    ConvexHull(np.ma.masked_all((3, 2)))
except ValueError as e:
    print("masked array 被拒绝:", e)

# 3) 含 nan 的点也应失败（Qhull 内部校验）
try:
    ConvexHull(np.array([[0, 0], [1, 1], [2, np.nan]]))
except Exception as e:
    print("nan 点被拒绝:", type(e).__name__)
```

**需要观察的现象**：第 1 步应输出 `ndim=2`、`npoints=5`、`nsimplex=4`（矩形 4 条边，内部点 `[2,1.5]` 不贡献面）；第 2、3 步分别抛出 `ValueError`（参考测试 [test_qhull.py:610-616](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/test_qhull.py#L610-L616)）。

**预期结果**：上述断言成立。如果运行环境未编译扩展，则「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `required_options=b"Qt"` 用户关不掉？关掉会怎样？
**答案**：高维或退化输入下 Qhull 可能给出非单纯形面（如三维六边形面），导致 `simplices` 各行顶点数不一致、数组无法写成规整的 `(nfacet, ndim)`。强制 `Qt` 把每个面三角化，保证输出形状齐整。

**练习 2**：`ConvexHull` 自己存了几何结果吗？
**答案**：没有。它只是把底层 `_Qhull` 算出的数组搬到自身属性上。真正计算在 `_Qhull`（封装 Qhull C 库）里完成，符合 `scipy.spatial` 的「薄壳 + 厚内核」分层。

---

### 4.2 vertices / simplices / equations

#### 4.2.1 概念说明

`ConvexHull` 返回三组最常用的数组：

| 属性 | 形状 | 含义 |
|------|------|------|
| `points` | `(npoints, ndim)` | 输入点坐标（副本） |
| `vertices` | `(nvertices,)` | 构成凸包**顶点**的点索引；二维下按**逆时针**序，高维按输入序 |
| `simplices` | `(nfacet, ndim)` | 每个面（单纯形）的顶点索引；二维每行是一条边的两端点 |
| `neighbors` | `(nfacet, ndim)` | 每个面的相邻面索引；第 k 列是「正对第 k 个顶点」的邻面，`-1` 表示无邻面（边界） |
| `equations` | `(nfacet, ndim+1)` | 每个面的超平面方程 `[normal, offset]` |
| `coplanar` | `(ncoplanar, 3)` | 因数值精度未能参与构形的共面点（需 `Qc` 选项） |
| `good` | `(nfacet,)` bool 或 `None` | 配合 `QGn`/`QG-n` 选项标记「从某点可见」的面 |

最需要理解的是 **`equations`**：它把凸包的每个面表达成一个超平面方程，让你不依赖具体顶点坐标就能判定任意点与该面的相对位置。

#### 4.2.2 核心流程

`equations` 的每一行是 \([\mathbf{n},\ b]\)，对应超平面方程：

\[
\mathbf{n}\cdot \mathbf{x}+b=0
\]

Qhull 的符号约定（关键）：**法向量 \(\mathbf{n}\) 指向凸包外侧**。于是对一个测试点 \(\mathbf{x}\)，逐面计算有符号距离

\[
s_j(\mathbf{x})=\mathbf{n}_j\cdot \mathbf{x}+b_j
\]

有：

- \(s_j(\mathbf{x})=0\)：\(\mathbf{x}\) 在第 \(j\) 个面上；
- \(s_j(\mathbf{x})<0\)：\(\mathbf{x}\) 在第 \(j\) 个面的**内侧**（朝凸包内部）；
- \(s_j(\mathbf{x})>0\)：\(\mathbf{x}\) 在第 \(j\) 个面的**外侧**。

因为凸包 = 所有面的内侧交集，所以「点在凸包内部（含边界）」等价于：

\[
\boxed{\ \max_{j}\ s_j(\mathbf{x})\le 0\ }
\]

即「对所有面都有 \(s_j\le 0\)」。这正是用 `equations` 做点包含判定的原理。`vertices` 在二维额外由 `get_extremes_2d` 给出**逆时针**顺序，方便绘图与有向面积计算；高维则退化为 `np.unique(simplices)`（无序）。

#### 4.2.3 源码精读

**结果数组的提取**发生在底层 `get_simplex_facet_array` [_qhull.pyx:564-715](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L564-L715)。输出数组先按面数 `j` 分配：

```python
facets    = np.zeros((j, facet_ndim), dtype=np.intc)       # 即 simplices
neighbors = np.zeros((j, facet_ndim), dtype=np.intc)
equations = np.zeros((j, facet_ndim+1), dtype=np.double)   # 多一列存 offset
```

每个面的方程由 Qhull C 结构 `facet` 上的 `normal` 与 `offset` 直接拷贝 [_qhull.pyx:684-686](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L684-L686)：

```python
# Save simplex equation info
for i in range(facet_ndim):
    equations[j, i] = facet.normal[i]
equations[j, facet_ndim] = facet.offset
```

注意 `equations` 形状是 `(nfacet, ndim+1)`——前 `ndim` 列是法向量，最后一列是 `offset`。这正是 4.2.2 里 \([\mathbf{n},\ b]\) 的来源，且法向方向沿用 Qhull 的「外法向」约定。

**二维顶点的逆时针序**由 `get_extremes_2d` 单独算 [_qhull.pyx:974-1042](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L974-L1042)：它沿面的邻接链 `facet → nextfacet` 走一圈，依次收集每个面的两个顶点，并用 `visitid` 去重，从而得到逆时针排列的顶点索引。

**`vertices` 属性** [_qhull.pyx:2543-2547](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L2543-L2547) 的双分支：

```python
@property
def vertices(self):
    if self._vertices is None:
        self._vertices = np.unique(self.simplices)
    return self._vertices
```

二维用 `get_extremes_2d` 预存的逆时针序；高维 `_vertices` 为 `None`，首次访问时用 `np.unique(simplices)` 现算（因此高维顶点无特定顺序）。

#### 4.2.4 代码实践

**实践目标**：用 `equations` 判定点是否在凸包内侧，并与「逆时针叉积」朴素判定法对照，验证两者一致、且印证外法向符号约定。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.spatial import ConvexHull

border = np.array([[0, 0], [4, 0], [4, 3], [0, 3]], dtype=float)
interior = np.array([[2, 1.5], [1, 1], [3, 2]], dtype=float)
points = np.vstack([border, interior])
hull = ConvexHull(points)

tests = np.array([[2.0, 1.5],   # 内部
                  [2.0, 3.0],   # 边界上
                  [5.0, 1.5],   # 外部
                  [-1.0, -1.0]])# 外部

# 方法 A：用 equations（外法向约定，内侧所有面 <= 0）
def inside_equations(eq, x):
    s = eq[:, :-1] @ x + eq[:, -1]   # 每个面的有符号值 s_j
    return np.all(s <= 1e-9), s.max() # 是否全 <=0，及最外侧那个值

# 方法 B：朴素逆时针叉积法（几何上完全独立）
def inside_ccw(verts_ccw, x):
    n = len(verts_ccw)
    for i in range(n):
        a = verts_ccw[i]
        b = verts_ccw[(i + 1) % n]
        cross = (b[0]-a[0])*(x[1]-a[1]) - (b[1]-a[1])*(x[0]-a[0])
        if cross < -1e-9:
            return False
    return True

ccw = hull.points[hull.vertices]      # 二维保证逆时针
for p in tests:
    in_eq, smax = inside_equations(hull.equations, p)
    in_ccw = inside_ccw(ccw, p)
    print(f"{p} -> equations 内={in_eq} (max s={smax:+.3f})  叉积法内={in_ccw}")
```

**需要观察的现象**：

- 内部点 `[2,1.5]`：两种方法都返回 `内=True`，且 `max s < 0`。
- 边界点 `[2,3.0]`：`max s ≈ 0`（落在上边面上）。
- 外部点 `[5,1.5]`、`[-1,-1]`：`max s > 0`，两种方法都返回 `内=False`。
- 两种方法的判定**完全一致**——这反向印证了 Qhull 的「外法向、内侧为负」约定。

**预期结果**：四组判定一致。若你的实现里 `max s` 符号相反，说明把内外侧弄反了，应回到 4.2.2 复核符号约定。「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`simplices` 与 `vertices` 在二维下是什么关系？
**答案**：二维下 `simplices` 每行是一条边（两个顶点索引），`vertices` 是这些边涉及的所有顶点索引去重后的结果（`np.unique(simplices) == np.sort(vertices)`，见测试 [test_qhull.py:631](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/test_qhull.py#L631)）。区别在于 `vertices` 在二维还保证**逆时针**顺序，而 `simplices` 的边顺序无此保证。

**练习 2**：给定 `equations`，如何求一个外部点到凸包表面的最近距离的**下界**？
**答案**：对每个面算 \(s_j=\mathbf{n}_j\cdot\mathbf{x}+b_j\)，取所有 \(s_j>0\)（点在该面外侧）者。由于 Qhull 的法向量是**单位**法向量，\(s_j\) 即点到该面所在超平面的有符号垂直距离；\(\min_{j:s_j>0} s_j\) 就是点到凸包边界的距离下界（精确最近距离还取决于点是否投影落在某面内部）。

---

### 4.3 volume / area

#### 4.3.1 概念说明

除了数组，`ConvexHull` 还给两个标量：

- **`volume`**：输入维度 > 2 时是凸包的**体积**；输入是二维时是凸多边形的**面积**。
- **`area`**：输入维度 > 2 时是凸包的**表面积**；输入是二维时是凸多边形的**周长**。

注意二维下的「名字复用」：二维的 `volume` 其实是面积、`area` 其实是周长。这是为了用一个统一接口覆盖任意维度而做的约定，初学时容易踩坑。

#### 4.3.2 核心流程

体积与面积并非由 Python 算出，而是直接读 Qhull C 库里维护的两个累计量：

```text
qhull.volume_area()
   ├── with nogil: qh_getarea(qh, facet_list)   # 遍历面，累加体积/面积
   └── return (qh.totvol, qh.totarea)            # totvol=总体积, totarea=总面积
```

Qhull 的 `qh_getarea` 遍历每个面，把每个面的面积累加成 `totarea`，并把每个面与某个参考点（如内部点）张成的「锥」体积累加成 `totvol`。对凸多面体这恰好就是表面积与体积；对二维，按 Qhull 约定 `totvol` 退化为多边形面积、`totarea` 退化为周长。

#### 4.3.3 源码精读

**`volume_area` 方法** [_qhull.pyx:531-548](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L531-L548)：

```python
def volume_area(self):
    cdef double volume
    cdef double area

    self.acquire_lock()
    try:
        self.check_active()
        self._qh.hasAreaVolume = 0
        with nogil:
            qh_getarea(self._qh, self._qh[0].facet_list)
        volume = self._qh[0].totvol
        area = self._qh[0].totarea
        return volume, area
    finally:
        self.release_lock()
```

要点：

- `check_active()` 确保底层 C 对象还没被 `close()`（增量模式下 close 后再取值会报错）。
- `with nogil` 释放 GIL，让 C 库 `qh_getarea` 全速遍历面表。
- 结果直接取 `totvol` / `totarea` 两个 C 字段——**零 Python 计算**。

随后在 `ConvexHull._update` 里挂到属性 [_qhull.pyx:2525](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L2525)：

```python
self.volume, self.area = qhull.volume_area()
```

测试 [test_qhull.py:669-676](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/test_qhull.py#L669-L676) 用单位立方体验证：8 个角点构造凸包，`volume == 1`、`area == 6`。

#### 4.3.4 代码实践

**实践目标**：用立方体与二维矩形两套输入，直观对照「体积/面积」与「面积/周长」两套语义。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.spatial import ConvexHull

# 三维：单位立方体
cube = np.array([(0,0,0),(0,1,0),(1,0,0),(1,1,0),
                 (0,0,1),(0,1,1),(1,0,1),(1,1,1)], dtype=float)
h3 = ConvexHull(cube)
print(f"3D: volume={h3.volume} (期望 1), area={h3.area} (期望 6)")

# 二维：4x3 矩形
rect = np.array([[0,0],[4,0],[4,3],[0,3]], dtype=float)
h2 = ConvexHull(rect)
print(f"2D: volume={h2.volume} (期望面积 12), area={h2.area} (期望周长 14)")
```

**需要观察的现象**：三维 `volume=1`、`area=6`；二维 `volume=12`（面积）、`area=14`（周长）。

**预期结果**：与上述期望一致，印证二维下 `volume` 实为面积、`area` 实为周长。「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么二维下 `volume` 给的是面积而不是真的「体积」？
**答案**：`scipy.spatial.ConvexHull` 用同一对属性覆盖任意维度。Qhull 对二维输入计算 `totvol` 时退化为多边形面积、`totarea` 退化为周长，于是 API 借用 `volume`/`area` 两个名字分别承载「填充度量」与「边界度量」。这是文档明示的约定（见类 docstring 的 area/volume 说明）。

**练习 2**：增量加点后，`volume` 会自动更新吗？
**答案**：会。`add_points` 最终会再次调用 `_update`（[第 2503 行](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L2503)），其中重新执行 `qhull.volume_area()`，`volume`/`area` 随新点集刷新。测试 [test_qhull.py:269-284](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/test_qhull.py#L269-L284) 验证了增量与一次性构造给出相同的体积/面积。

## 5. 综合实践

把本讲三块内容串起来：**构造凸包 → 用 `equations` 做点包含判定 → 用 `convex_hull_plot_2d` 可视化**。

```python
# 示例代码
import numpy as np
import matplotlib.pyplot as plt
from scipy.spatial import ConvexHull, convex_hull_plot_2d

rng = np.random.default_rng(42)
points = rng.random((40, 2))
hull = ConvexHull(points)

# (1) 点包含判定：用 equations 判一批测试点
tests = rng.random((200, 2))
signed = tests @ hull.equations[:, :-1].T + hull.equations[:, -1]  # (200, nfacet)
inside = np.all(signed <= 1e-9, axis=1)

# (2) 可视化：凸包边 + 测试点按内外染色
fig, ax = plt.subplots()
ax.scatter(points[:, 0], points[:, 1], c='k', s=20, label='输入点')
ax.scatter(tests[inside, 0],  tests[inside, 1],  c='g', s=10, label='判定为内')
ax.scatter(tests[~inside, 0], tests[~inside, 1], c='r', s=10, label='判定为外')
convex_hull_plot_2d(hull, ax=ax)   # 画出凸包边（LineCollection）
ax.legend(); ax.set_title(f"volume(area)={hull.volume:.3f}, area(perim)={hull.area:.3f}")
plt.show()
```

**关注点**：

- `convex_hull_plot_2d` 内部就是把 `hull.simplices` 的每条边做成线段集合绘制（见 [_plotutils.py:128-132](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_plotutils.py#L128-L132)），它要求二维输入，否则抛 `ValueError`（[第 124-125 行](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_plotutils.py#L124-L125)）。
- 向量化判定 `tests @ eq[:, :-1].T + eq[:, -1]` 一次性算出所有 (测试点, 面) 的有符号值，比循环快得多。
- 绿点应全部落在凸包多边形内部或边上，红点全部在外——若不符，复核 4.2.2 的符号约定。

**预期结果**：图上绿/红点与凸包边界关系正确；标题显示的 `volume` 为多边形面积、`area` 为周长。无 matplotlib 环境则「待本地验证」。

## 6. 本讲小结

- 凸包是「包住所有点的最小凸形」；`ConvexHull` 是薄壳，真正计算在底层 `_Qhull`（Qhull C 库）完成。
- 构造时 `mode_option=b"i"` 表示普通凸包，`Qt` 永远开启以保证输出单纯形化、形状规整。
- `simplices`/`neighbors`/`equations` 来自 `get_simplex_facet_array`；`equations` 每行 `[normal, offset]`，法向**指向外侧**，内部点满足所有面 `normal·x + offset ≤ 0`。
- `vertices` 在二维由 `get_extremes_2d` 给出**逆时针**序，高维退化为 `np.unique(simplices)`。
- `volume`/`area` 直接读 Qhull 的 `totvol`/`totarea`；二维下 `volume` 实为面积、`area` 实为周长。
- 用 `equations` 做「点是否在凸包内」判定，是最干净、最高维通用的方法。

## 7. 下一步学习建议

- **u3-l3 Voronoi 图**：Voronoi 与凸包在 Qhull 内部同源，学完凸包再读 Voronoi 会很顺，重点看 `ridge` 与无穷远点（`-1`）约定。
- **u3-l4 HalfspaceIntersection**：凸包的「对偶」视角——凸包是「点的组合」，半空间求交是「面的组合」，对照阅读能加深对 `equations` 超平面方程的理解。
- **u10-l1 / u10-l2（专家层）**：若想下沉到底层，可读 `_Qhull` 的生命周期（`__dealloc__`/`close`）与 `_QhullUser._update` 的增量框架，看 `get_simplex_facet_array` 如何遍历 Qhull 的 C 面表。
