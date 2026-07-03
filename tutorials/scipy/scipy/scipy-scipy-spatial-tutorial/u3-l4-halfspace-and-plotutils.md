# HalfspaceIntersection 与绘图助手

## 1. 本讲目标

前几讲（u3-l1 Delaunay、u3-l2 ConvexHull、u3-l3 Voronoi）处理的输入都是**点**，输出是「点构成的几何结构」。本讲反其道而行：输入是**半空间（超平面的一侧）**，输出是「这些半空间共同围出的区域」的顶点。它对应 Qhull 命令行里的 `qhalf`。读完本讲你应该能够：

- 说清「半空间」是什么，以及一摞半空间的交集围出一个什么样的几何体；
- 理解 `interior_point`（内部可行点）为何是 `HalfspaceIntersection` 的**强制参数**，以及 Qhull 靠「对偶变换」把半空间求交转成它擅长的凸包问题；
- 读懂 `dual_points`、`dual_vertices`、`intersections`、`dual_equations` 这几个属性分别代表什么、彼此如何换算；
- 用 `_plotutils.py` 里的 `delaunay_plot_2d`、`convex_hull_plot_2d`、`voronoi_plot_2d` 三个函数把结果画出来；
- 看懂 `_get_axes` 与 `_adjust_bounds` 两个小助手如何统一三张图的坐标轴行为。

---

## 2. 前置知识

本讲承接 u3-l1、u3-l2、u3-l3，你需要先掌握：

- **凸包**（u3-l2）：包住所有点的最小凸形；以及 `equations` 每行 `[normal, offset]` 表示超平面方程、法向指向外侧的约定。
- **Delaunay 三角剖分**（u3-l1）：Qhull 把 Delaunay 转成「抬到抛物面后求下凸包」的套路——本讲会看到 Qhull **同样用「转成凸包」的套路**来处理半空间。
- **`_QhullUser → _Qhull`** 这一高层壳到底层 C 库（`qhT`）的调用链。

几个本讲用到的术语，先用大白话解释：

- **半空间（halfspace）**：超平面把空间切成两半，取其中一半（含边界）就是一个半空间。2D 里半空间就是「直线某一侧（含直线）的区域」，3D 里是「平面某一侧」。
- **可行区域（feasible region）**：所有半空间的**交集**，一定是个凸多面体（可能无界）。
- **内部可行点（interior point / feasible point）**：明确落在可行区域**内部**（不是边界上）的一个点，本讲里用 \(p\) 表示。

**半空间求交的直觉**：给你一组「约束」，每条约束砍掉空间的一半，最后剩下的就是可行区域。比如二维里给三条直线 \(x=0\)、\(y=0\)、\(x+y=1\)，分别取「右侧、上侧、左下侧」，三者交集正好是一个三角形。这正是本讲综合实践要构造的例子。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [_qhull.pyx:L2723-L2971](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L2723-L2971) | `HalfspaceIntersection` 高层壳类：校验输入、拼 qhull 选项、调 `_Qhull`、在 `_update` 里把对偶结果换算回原空间 |
| [_qhull.pyx:L317-L352](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L317-L352) | `_Qhull.__init__`：识别 `H` 模式、把 `interior_point` 作为 feasible point 传给 C 库 `qh_new_qhull_scipy` |
| [_qhull.pyx:L766-L825](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L766-L825) | `_Qhull.get_hull_facets`：提取对偶凸包的面（facets）与超平面方程（dual_equations） |
| [_qhull.pyx:L720-L761](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L720-L761) | `_Qhull.get_hull_points`：半空间模式下返回的是**对偶点**而非原始点 |
| [_plotutils.py:L20-L75](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_plotutils.py#L20-L75) | `delaunay_plot_2d`：用 `triplot` 画 2D Delaunay |
| [_plotutils.py:L78-L135](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_plotutils.py#L78-L135) | `convex_hull_plot_2d`：用 `LineCollection` 画 2D 凸包边 |
| [_plotutils.py:L138-L263](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_plotutils.py#L138-L263) | `voronoi_plot_2d`：画 Voronoi，含有限脊（实线）与无限脊（虚线）的分别处理 |
| [_plotutils.py:L6-L17](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_plotutils.py#L6-L17) | `_get_axes` / `_adjust_bounds`：三张图共用的「建坐标轴」与「自动留白」助手 |
| [tests/test__plotutils.py:L18-L48](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/test__plotutils.py#L18-L48) | 三个绘图函数的 smoke test，演示 `ax` 传入与默认创建两种用法 |

---

## 4. 核心概念与源码讲解

### 4.1 半空间表示与对偶变换

#### 4.1.1 概念说明

`HalfspaceIntersection` 不接受「点」，而接受「约束」。每条约束是一个形如

\[
A\cdot x + b \le 0
\]

的不等式（\(A\) 是法向量，\(b\) 是偏移）。源码里把 \([A;\,b]\) 拼成一行，所以 `halfspaces` 数组的形状是 `(nineq, ndim+1)`——前 `ndim` 列是法向量，最后一列是偏移。比如二维下 \(x\ge 0\) 写成 \(-x \le 0\)，对应行 `[-1, 0, 0]`。

所有半空间的交集是一个凸多面体，我们要的 `intersections` 就是这个多面体的**顶点**。但 Qhull 的 C 内核并不直接做「半空间求交」——它最擅长的是凸包。于是 SciPy 借用了一个经典技巧：**对偶变换（duality）**，把「半空间求交」转成「点的凸包」。

对偶变换的核心是给定一个严格内部点 \(p\)，把每个半空间 \([A,b]\) 映射成一个**对偶点**。变换公式直接写在源码里（增量添加半空间时）：

```python
dists = points[:, :-1].dot(interior_point) + points[:, -1]   # 即 A·p + b
arr = -points[:, :-1] / dists[:, np.newaxis]                  # 对偶点 = -A/(A·p+b)
```

也就是说，对偶点

\[
d = \frac{-A}{A\cdot p + b}\,.
\]

由于 \(p\) 严格可行，\(A\cdot p + b < 0\)，分母为负，对偶点落在法向 \(A\) 那一侧。这个变换的几何意义是经典的「中心对偶（polar dual）」：半空间边界平面到 \(p\) 的距离越远，对偶点离原点越近（距离成反比）。

**为什么对偶管用？** 因为它把可行多面体与对偶点的凸包**一一对应**起来：

| 可行区域（原空间） | 对偶凸包（对偶空间） |
|---|---|
| 一个**顶点**（ndim 个面交汇） | 一个**面（facet）** |
| 一个**面**（一条半空间的边界） | 一个**顶点**（对偶点） |

所以要求可行多面体的顶点，只需：先算对偶点的凸包，再把这个凸包每个面的方程「换算回」原空间的一个点。

#### 4.1.2 核心流程

整个 `HalfspaceIntersection` 的流程，本质就是「转成凸包 → 算完 → 转回来」：

```text
HalfspaceIntersection.__init__(halfspaces, interior_point)
    └─ _Qhull(b"H", halfspaces, ..., interior_point=p)   # H = halfspace 模式
           └─ qh_new_qhull_scipy(..., feaspoint=p)        # C 库内部做对偶变换 + 求凸包
    └─ _QhullUser.__init__(qhull)
           └─ HalfspaceIntersection._update(qhull)        # 把对偶结果换算回原空间
                  ├─ get_hull_facets()   → dual_facets, dual_equations
                  ├─ get_hull_points()   → dual_points
                  ├─ volume_area()       → dual_volume, dual_area
                  └─ intersections = dual_equations[:,-1 换算] + p
```

换算回来的公式同样写在源码里：

```python
self.intersections = self.dual_equations[:, :-1] / -self.dual_equations[:, -1:] + self.interior_point
```

即对偶凸包某个面的方程是 `[normal, offset]`（`normal·y + offset = 0`），对应原空间的顶点为

\[
v = \frac{\mathrm{normal}}{-\,\mathrm{offset}} + p\,.
\]

#### 4.1.3 源码精读

`_Qhull.__init__` 通过模式串识别半空间模式，并把 `interior_point` 当作 feasible point 交给 C 库：

[_qhull.pyx:L322-L325](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L322-L325)——`H` 开头的模式串把 `_is_halfspaces` 置 1，后续所有提取函数都据此把维度减 1（因为半空间比点多一个分量 `b`）：

```cython
if mode_option.startswith(b"H"):
    self._is_halfspaces = 1
else:
    self._is_halfspaces = 0
```

[_qhull.pyx:L346-L352](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L346-L352)——把 `interior_point` 的裸指针 `coord` 传给 C 库；没有它（`NULL`）Qhull 就无法做对偶变换，所以 SciPy 把 `interior_point` 设成强制参数：

```cython
if interior_point is not None:
    coord = <coordT*>interior_point.data
else:
    coord = NULL
exitcode = qh_new_qhull_scipy(self._qh, self.ndim, self.numpoints,
                              <realT*>points.data, 0,
                              options_c, NULL, self._messages.handle, coord)
```

底层 C 库真正执行「给定 feasible 点、对所有半空间做对偶并求凸包」的函数声明见 [_qhull.pyx:L178](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L178)（`qh_sethalfspace_all`），它就是 `qhalf` 命令背后的实现。

增量模式下「半空间 → 对偶点」的换算在 [_qhull.pyx:L456-L460](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L456-L460)，注释里写明「把半空间存在 `_points`，把对偶点之后存进 `_dual_points`」：

```cython
elif self._is_halfspaces:
    #Store the halfspaces in _points and the dual points in _dual_points later
    self._point_arrays.append(np.array(points, copy=True))
    dists = points[:, :-1].dot(interior_point)+points[:, -1]
    arr = np.array(-points[:, :-1]/dists[:, np.newaxis], ...)
```

#### 4.1.4 代码实践

**实践目标**：亲手算一个对偶点，验证它与源码公式一致，建立「半空间 ↔ 对偶点」的直觉。

1. 取一条半空间 \(x + y - 1 \le 0\)，即行 `[1, 1, -1]`，故 \(A=(1,1)\)、\(b=-1\)。
2. 取内部点 \(p=(0.25, 0.25)\)。
3. 手算 \(A\cdot p + b = 0.25+0.25-1 = -0.5\)。
4. 按公式 \(d=-A/(A\cdot p+b) = -(1,1)/(-0.5) = (2,2)\)，所以这条半空间的对偶点应是 `(2, 2)`。
5. 把这步换算用代码写出来对照（**示例代码**，非项目原有）：

```python
import numpy as np
A = np.array([1.0, 1.0]); b = -1.0
p = np.array([0.25, 0.25])
d = -A / (A @ p + b)
print(d)   # 预期 [2. 2.]
```

**需要观察的现象**：当 \(p\) 取得更靠近边界（如 \(p=(0.49,0.49)\)）时，\(A\cdot p+b\) 趋近 0，对偶点模长趋于无穷——这正是「距离边界越近，对偶点越远」的反比关系。

**预期结果**：`d == [2. 2.]`。若你把 \(p\) 改到边界外（如 `(0.6,0.6)`），\(A\cdot p+b=0.2>0\)，此时 `HalfspaceIntersection` 会拒绝该 `interior_point`（见 4.2 的可行性校验）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `interior_point` 必须是**严格内部**点，不能落在边界上？

**答案**：对偶点公式 \(d=-A/(A\cdot p+b)\) 的分母是 \(A\cdot p+b\)。若 \(p\) 落在某条半空间边界上，则该半空间满足 \(A\cdot p+b=0\)，分母为零、对偶点跑到无穷远，对偶凸包退化。源码里 `add_halfspaces` 正是用 `dists >= 0` 判定违规（详见 4.2.3）。

**练习 2**：半空间模式下 `get_hull_points` 返回的到底是「原始半空间」还是「对偶点」？

**答案**：返回的是**对偶点**。其文档串明确写 "in halfspace mode, where the points are in fact the points of the dual hull"（见 [_qhull.pyx:L721-L724](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L721-L724)）。

---

### 4.2 HalfspaceIntersection 类与 dual_vertices

#### 4.2.1 概念说明

`HalfspaceIntersection` 是高层壳，沿 `HalfspaceIntersection → _QhullUser → _Qhull` 把计算下放给 Qhull C 库。它对外暴露的核心属性可分两组：

- **原空间结果**：`intersections`（可行多面体的顶点坐标）、`dual_vertices`（哪些半空间是「起作用的」，见下）。
- **对偶空间中间量**：`dual_points`（每个半空间的对偶点）、`dual_facets`（对偶凸包的面）、`dual_equations`（这些面的方程）、`dual_volume`/`dual_area`（对偶凸包的体积/面积）。

这里要特别分清 `dual_points` 与 `dual_vertices`：

- `dual_points`：**所有**输入半空间的对偶点坐标，形状 `(nineq, ndim)`，与输入半空间一一对应。
- `dual_vertices`：对偶凸包**顶点**对应的半空间**下标**。一条半空间若是「冗余的」（其边界根本碰不到可行区域），它的对偶点会落在对偶凸包内部，**不会**出现在 `dual_vertices` 里。因此 `dual_vertices` 本质上告诉你「哪些约束是真正起作用的」。

注意 `dual_volume`/`dual_area` 是**对偶凸包**的体积/面积，**不是**可行多面体的体积/面积——这是初学者最容易踩的坑。Qhull 在半空间模式下并不直接给你原空间的体积。

#### 4.2.2 核心流程

`HalfspaceIntersection._update` 是把对偶结果换算回原空间的中枢（伪代码）：

```python
def _update(self, qhull):
    self.dual_facets, self.dual_equations = qhull.get_hull_facets()  # 对偶凸包的面 + 方程
    self.dual_points = qhull.get_hull_points()                       # 对偶点
    self.dual_volume, self.dual_area = qhull.volume_area()           # 对偶凸包体积/面积
    # 把每个对偶面的方程换算回原空间的顶点
    self.intersections = (self.dual_equations[:, :-1] / -self.dual_equations[:, -1:]
                          + self.interior_point)
    if qhull.ndim == 2:
        self._vertices = qhull.get_extremes_2d()    # 2D：逆序的对偶顶点下标
    else:
        self._vertices = None
```

而 `dual_vertices` 是一个**惰性属性**：2D 时直接用 `_update` 里算好的 `get_extremes_2d()`（逆时针排序）；高维时才把所有 facet 里的顶点下标去重得到：

```python
@property
def dual_vertices(self):
    if self._vertices is None:
        self._vertices = np.unique(np.array(self.dual_facets))
    return self._vertices
```

#### 4.2.3 源码精读

构造函数 [_qhull.pyx:L2865-L2889](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L2865-L2889) 做三件事：校验输入（拒绝 masked array、检查 `interior_point` 形状必须是 `(ndim,)`，注意源码注释把这条信息写成 `(ndim-1,)` 但实际语义是「halfspaces 列数减一」即 `ndim`）、拼 qhull 选项（`ndim+1 >= 6` 即原空间维度 ≥ 5 时追加 `Qx`）、强制模式 `H`：

```cython
if interior_point.shape != (halfspaces.shape[1]-1,):
    raise ValueError('Feasible point must be a (ndim-1,) array')
halfspaces = np.ascontiguousarray(halfspaces, dtype=np.double)
self.interior_point = np.ascontiguousarray(interior_point, dtype=np.double)
...
mode_option = "H"
qhull = _Qhull(mode_option.encode(), halfspaces, qhull_options,
               required_options=None, incremental=incremental,
               interior_point=self.interior_point)
```

`_update` 的换算在 [_qhull.pyx:L2891-L2908](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L2891-L2908)，是本讲最关键的一行 `intersections` 换算：

```cython
def _update(self, qhull):
    self.dual_facets, self.dual_equations = qhull.get_hull_facets()
    self.dual_points = qhull.get_hull_points()
    self.dual_volume, self.dual_area = qhull.volume_area()
    self.intersections = self.dual_equations[:, :-1]/-self.dual_equations[:, -1:] + self.interior_point
    if qhull.ndim == 2:
        self._vertices = qhull.get_extremes_2d()
    ...
```

`dual_vertices` / `halfspaces` 两个属性见 [_qhull.pyx:L2962-L2970](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L2962-L2970)。注意 `halfspaces` 直接返回 `self._points`——在半空间模式下，`_QhullUser._update`（[_qhull.pyx:L1625-L1630](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L1625-L1630)）用 `get_points()` 取到的是**原始半空间数组**（而非对偶点），这是 `_Qhull.get_points` 在非增量情形直接返回 `_point_arrays[0]` 的结果。

增量添加半空间时的可行性校验在 [_qhull.pyx:L2944-L2960](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L2944-L2960)——重新算 `A·p+b`，凡是 `>= 0`（即不在严格内部）的新半空间都判违规并抛 `QhullError`：

```cython
halfspaces = np.atleast_2d(halfspaces)
dists = np.dot(halfspaces[:, :self.ndim], self.interior_point) + halfspaces[:, -1]
viols = dists >= 0
if viols.any():
    first_viol = np.nonzero(viols)[0].min()
    bad_hs = halfspaces[first_viol, :]
    msg = f"feasible point is not clearly inside halfspace: {bad_hs}"
    raise QhullError(msg)
```

#### 4.2.4 代码实践

**实践目标**：用三条半空间 \(x\ge 0\)、\(y\ge 0\)、\(x+y\le 1\) 构造一个三角形可行区域，验证 `intersections` 与 `dual_vertices`。

1. 把三条约束都写成 \(A\cdot x+b\le 0\) 形式：

   | 约束 | \(A\cdot x+b\le 0\) 形式 | 行 |
   |---|---|---|
   | \(x\ge 0\) | \(-x\le 0\) | `[-1, 0, 0]` |
   | \(y\ge 0\) | \(-y\le 0\) | `[0, -1, 0]` |
   | \(x+y\le 1\) | \(x+y-1\le 0\) | `[1, 1, -1]` |

2. 取内部点 `p = (0.25, 0.25)`（三角形内部）。
3. 运行（**示例代码**）：

```python
import numpy as np
from scipy.spatial import HalfspaceIntersection

halfspaces = np.array([[-1., 0., 0.],     # x >= 0
                       [0., -1., 0.],     # y >= 0
                       [1., 1., -1.]])    # x + y <= 1
p = np.array([0.25, 0.25])
hs = HalfspaceIntersection(halfspaces, p)

print("intersections:\n", hs.intersections)
print("dual_vertices:", hs.dual_vertices)
print("dual_points:\n", hs.dual_points)
```

**需要观察的现象**：
- `intersections` 应是三角形的三个顶点集合 \(\{(0,0),(1,0),(0,1)\}\)（顺序可能不同）。
- `dual_vertices` 应包含 `[0, 1, 2]`——三条半空间都「起作用」，没有冗余约束。
- `dual_points` 的每一行就是 4.1.4 里手算的那种对偶点。

**预期结果**：`intersections` 的三行按集合相等等于 \(\{(0,0),(1,0),(0,1)\}\)；`dual_vertices` 在 2D 下按逆时针给出 `[0, 1, 2]` 的一种排列。

**进阶**：再加一条冗余约束 \(x+y\le 2\)（行 `[1, 1, -2]`），它的边界在三角形外侧、根本碰不到可行区域。重跑后 `dual_vertices` 应**不再包含**这条新半空间的下标——它的对偶点落进了对偶凸包内部。

> 若你的环境未安装编译好的 SciPy，以上输出标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`dual_volume` 报的是可行三角形（原空间）的面积吗？

**答案**：**不是**。`dual_volume`/`dual_area` 是**对偶凸包**的体积/面积，来自 `qhull.volume_area()`（[_qhull.pyx:L531-L548](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L531-L548)）。原空间可行多面体的体积 Qhull 在半空间模式下并不直接给出。

**练习 2**：把 `interior_point` 改成 `(0.6, 0.6)`（落在 \(x+y\le 1\) 之外）再构造，会发生什么？

**答案**：构造阶段 Qhull C 库会发现该点对半空间 `[1,1,-1]` 不满足 \(A\cdot p+b\le 0\)（\(0.6+0.6-1=0.2>0\)），抛出 `QhullError`。这与 `add_halfspaces` 里 `dists >= 0` 的判定一致。要事先求一个合法内部点，可解 docstring 里给出的线性规划（Chebyshev 中心），见 [_qhull.pyx:L2830-L2858](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L2830-L2858)。

---

### 4.3 _plotutils 的三个 2D 绘图函数

#### 4.3.1 概念说明

`_plotutils.py` 只导出三个函数（`__all__ = ['delaunay_plot_2d', 'convex_hull_plot_2d', 'voronoi_plot_2d']`），分别给 u3-l1/u3-l2/u3-l3 的三个结果对象画 2D 图。注意：**没有** `halfspace_intersection_plot_2d`——半空间求交没有官方绘图助手，docstring 里画半空间的代码是手写示例（见 [_qhull.pyx:L2805-L2828](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L2805-L2828)）。

三个函数的统一约定：

- 只画 **2D**（`points.shape[1] != 2` 直接 `raise ValueError`）；
- 入参 `ax` 可选，不传就 `_get_axes()` 新建一个；
- 返回 `ax.figure`（即 `matplotlib.figure.Figure`）；
- 收尾都调 `_adjust_bounds(ax, points)` 自动留白。

三者最大的差异在「画什么线」：

| 函数 | 输入对象 | 画法 |
|---|---|---|
| `delaunay_plot_2d` | `Delaunay` | 散点 + `ax.triplot(x, y, tri.simplices)`（三角形边） |
| `convex_hull_plot_2d` | `ConvexHull` | 散点 + `LineCollection(hull.simplices)`（包的边） |
| `voronoi_plot_2d` | `Voronoi` | 散点 + 顶点 + 有限脊（实线）/ 无限脊（虚线） |

`voronoi_plot_2d` 最复杂，因为它要处理 u3-l3 讲过的「`-1` 表示无穷远点」：含 `-1` 的脊画成虚线，并沿外法向延伸到一个足够远的 `far_point`。

#### 4.3.2 核心流程

`delaunay_plot_2d` 的主流程（最简洁，作样板）：

```python
def delaunay_plot_2d(tri, ax=None):
    if tri.points.shape[1] != 2:
        raise ValueError("Delaunay triangulation is not 2-D")
    x, y = tri.points.T
    ax = ax or _get_axes()
    ax.plot(x, y, 'o')
    ax.triplot(x, y, tri.simplices.copy())   # .copy() 避免 matplotlib 改动原数组
    _adjust_bounds(ax, tri.points)
    return ax.figure
```

`voronoi_plot_2d` 处理无限脊的关键片段（承接 u3-l3 的法向延伸逻辑）：

```python
for pointidx, simplex in zip(vor.ridge_points, vor.ridge_vertices):
    simplex = np.asarray(simplex)
    if np.all(simplex >= 0):
        finite_segments.append(vor.vertices[simplex])        # 两端都有限：实线
    else:
        i = simplex[simplex >= 0][0]                          # 有限端顶点
        t = vor.points[pointidx[1]] - vor.points[pointidx[0]] # 切向
        t /= np.linalg.norm(t)
        n = np.array([-t[1], t[0]])                           # 法向
        midpoint = vor.points[pointidx].mean(axis=0)
        direction = np.sign(np.dot(midpoint - center, n)) * n
        ...
        far_point = vor.vertices[i] + direction * ptp_bound.max() * aspect_factor
        infinite_segments.append([vor.vertices[i], far_point])  # 虚线
```

#### 4.3.3 源码精读

`delaunay_plot_2d` 全文见 [_plotutils.py:L20-L75](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_plotutils.py#L20-L75)。注意 `tri.simplices.copy()`：因为 `ax.triplot` 会缓存传入的数组，传原数组可能被后续修改，所以拷一份。这条细节被测试特意覆盖——`test_delaunay` 断言画完图后 `obj.simplices` 不变（[tests/test__plotutils.py:L22-L30](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/test__plotutils.py#L22-L30)）。

`convex_hull_plot_2d` 见 [_plotutils.py:L78-L135](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_plotutils.py#L78-L135)，用 `LineCollection` 把 `hull.simplices`（每行是一条边的两端点）画成黑色实线段：

```python
line_segments = [hull.points[simplex] for simplex in hull.simplices]
ax.add_collection(LineCollection(line_segments, colors='k', linestyle='solid'))
```

`voronoi_plot_2d` 见 [_plotutils.py:L138-L263](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_plotutils.py#L138-L263)。它支持一串 `**kw` 关键字（`show_points`/`show_vertices`/`line_colors`/`line_width`/`line_alpha`/`point_size`，均用 `kw.get(..., 默认)` 取值）。无限脊长度的 `aspect_factor = abs(ptp_bound.max() / ptp_bound.min())` 是为修 GH#19653 的 bug 加的：当点云在各坐标轴上尺度差异巨大时，原先固定延伸长度会让无限脊画得太短或溢出，故按宽高比放大（回归测试 [tests/test__plotutils.py:L49-L91](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/test__plotutils.py#L49-L91) 固化了两组极端宽高比下的精确线段坐标）。

#### 4.3.4 代码实践

**实践目标**：构造一个 Delaunay，用 `delaunay_plot_2d` 画出来，并手动把坐标范围调到比自动留白更大，体会 `ax` 传入式用法。

1. 准备数据并画图（**示例代码**，需安装 matplotlib）：

```python
import numpy as np
import matplotlib
matplotlib.use("Agg")          # 无显示环境也能跑；交互环境可省略
import matplotlib.pyplot as plt
from scipy.spatial import Delaunay, delaunay_plot_2d

rng = np.random.default_rng(0)
points = rng.random((20, 2))
tri = Delaunay(points)

# 方式 A：让函数自己建 figure
fig = delaunay_plot_2d(tri)

# 方式 B：自己建 figure/ax，传入 ax，再手动改坐标范围
fig2 = plt.figure()
ax = fig2.gca()
delaunay_plot_2d(tri, ax=ax)
ax.set_xlim(-0.5, 1.5)          # 比 _adjust_bounds 的自动留白更宽
ax.set_ylim(-0.5, 1.5)
fig2.savefig("delaunay.png")
print("saved")
```

2. 对照测试 `test_delaunay`（[tests/test__plotutils.py:L22-L30](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/test__plotutils.py#L22-L30)）：它在传入 `ax=fig.gca()` 后断言返回值就是同一个 `fig`。你可以在方式 B 里加一行 `assert fig2 is delaunay_plot_2d(tri, ax=ax)` 验证这一约定。

**需要观察的现象**：方式 A 的坐标范围被 `_adjust_bounds` 自动设成「点云范围 ± 10% 极差」；方式 B 因为你在绘图**之后**又 `set_xlim`/`set_ylim`，最终范围是你手设的 `(-0.5, 1.5)`。

**预期结果**：生成 `delaunay.png`，图中可见散点与三角形网格；方式 B 的留白明显比方式 A 宽。

> 若无 matplotlib，标注「待本地验证」。无显示环境下务必先 `matplotlib.use("Agg")`，正如测试文件 [tests/test__plotutils.py:L5-L11](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/test__plotutils.py#L5-L11) 所做。

#### 4.3.5 小练习与答案

**练习 1**：三个绘图函数都要求输入是 2D，传入 3D 数据会怎样？

**答案**：各自第一行就 `raise ValueError`，信息分别是 "Delaunay triangulation is not 2-D" / "Convex hull is not 2-D" / "Voronoi diagram is not 2-D"（[_plotutils.py:L64-L65](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_plotutils.py#L64-L65)、[L124-L125](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_plotutils.py#L124-L125)、[L210-L211](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_plotutils.py#L210-L211)）。

**练习 2**：为什么 `delaunay_plot_2d` 里要写 `tri.simplices.copy()` 而不是直接传 `tri.simplices`？

**答案**：`matplotlib.triplot` 会把传入的三角形表缓存到 `Triangulation` 对象里；若直接传 view，后续对 `simplices` 的修改会波及 matplotlib 内部状态。`.copy()` 切断引用，测试 `test_delaunay` 正是断言「画图不应改动 `obj.simplices`」。

---

### 4.4 _get_axes 与 _adjust_bounds 辅助

#### 4.4.1 概念说明

这两个小函数是三个绘图函数的公共底盘，让它们「不传 `ax` 也能跑、画完自动留白」。

- `_get_axes()`：惰性 `import matplotlib.pyplot`，返回一个全新 `figure` 的当前 `Axes`。把 `matplotlib` 的 import 推迟到函数体内，意味着**没装 matplotlib 时只有调用绘图函数才报错**，而 `import scipy.spatial` 本身不依赖 matplotlib。
- `_adjust_bounds(ax, points)`：按 `points` 的范围给 `ax` 设 `xlim`/`ylim`，两侧各留 10% 极差（`np.ptp` 是 peak-to-peak，即 max−min）的边距。

#### 4.4.2 核心流程

`_adjust_bounds` 的逻辑只有四行：

```python
def _adjust_bounds(ax, points):
    margin = 0.1 * np.ptp(points, axis=0)        # 每个维度 10% 极差作边距
    xy_min = points.min(axis=0) - margin
    xy_max = points.max(axis=0) + margin
    ax.set_xlim(xy_min[0], xy_max[0])
    ax.set_ylim(xy_min[1], xy_max[1])
```

注意它**只用前两列**（`xy_min[0]`、`xy_max[0]` 给 xlim，`[1]` 给 ylim）——这没问题，因为调用方都已先校验过 `points.shape[1] == 2`。

#### 4.4.3 源码精读

两个函数全文见 [_plotutils.py:L6-L17](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_plotutils.py#L6-L17)：

```python
def _get_axes():
    import matplotlib.pyplot as plt
    return plt.figure().gca()


def _adjust_bounds(ax, points):
    margin = 0.1 * np.ptp(points, axis=0)
    xy_min = points.min(axis=0) - margin
    xy_max = points.max(axis=0) + margin
    ax.set_xlim(xy_min[0], xy_max[0])
    ax.set_ylim(xy_min[1], xy_max[1])
```

三个绘图函数都用 `ax = ax or _get_axes()` 这一惯用法：用户传了 `ax` 就用用户的，否则新建。`or` 在这里安全，因为一个合法的 `Axes` 对象永远 truthy。

#### 4.4.4 代码实践

**实践目标**：通过阅读 `_adjust_bounds`，**预测**一张图的自动坐标范围，再用真实绘图验证。

1. 取 `points = np.array([[0,0],[0,1],[1,0],[1,1]])`（单位正方形四角）。
2. 手算：`ptp = [1, 1]`，`margin = 0.1 * [1,1] = [0.1, 0.1]`，故 `xlim = ylim = (-0.1, 1.1)`。
3. 用 `convex_hull_plot_2d` 画图并读回坐标轴范围对照（**示例代码**）：

```python
import numpy as np
import matplotlib
matplotlib.use("Agg")
from scipy.spatial import ConvexHull, convex_hull_plot_2d

points = np.array([[0., 0.], [0., 1.], [1., 0.], [1., 1.]])
fig = convex_hull_plot_2d(ConvexHull(points))
ax = fig.gca()
print(ax.get_xlim(), ax.get_ylim())   # 预期 (-0.1, 1.1) (-0.1, 1.1)
```

**需要观察的现象**：打印的 `xlim`/`ylim` 恰为 `(-0.1, 1.1)`，与手算一致。

**预期结果**：`(-0.1, 1.1) (-0.1, 1.1)`。

> 若无 matplotlib，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `_get_axes` 把 `import matplotlib.pyplot` 写在函数体里，而不是写在文件顶部？

**答案**：让 `import scipy.spatial`（进而 `import scipy.spatial._plotutils`）**不强依赖 matplotlib**。只有用户真正调用绘图函数时才触发 matplotlib 的导入与可能的 `ImportError`。这是 SciPy 里「可选依赖惰性导入」的常见写法。

**练习 2**：若点云在某维退化为一条线（如所有点 `x` 相同，`ptp` 在该维为 0），`_adjust_bounds` 还能正常留白吗？

**答案**：能，但留白为 0——该维 `margin = 0.1 * 0 = 0`，`xlim` 退化为 `(xmin, xmin)`，是一个零宽区间，matplotlib 会显示成一条竖直线甚至报警告。这是 `_adjust_bounds` 的一个已知局限，使用退化数据时建议手动 `set_xlim`。

---

## 5. 综合实践

把本讲两块内容串起来：先用 `HalfspaceIntersection` 求出可行多面体的顶点，再**手写**一段绘图代码（因为没有现成的 `halfspace_intersection_plot_2d`）把这些顶点和半空间边界画在一起，最后用 `_adjust_bounds` 的同款思路给图留白。

**任务**：构造可行区域为四边形 \(\{x\ge0,\ y\ge0,\ x\le2,\ y\le1\}\)（一个 \(2\times1\) 矩形）的半空间组，求交、画图。

**步骤**（**示例代码**，需 matplotlib）：

```python
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.spatial import HalfspaceIntersection

# 1) 四条半空间：x>=0, y>=0, x<=2, y<=1，全部写成 A.x + b <= 0
halfspaces = np.array([
    [-1., 0., 0.],    #  x >= 0
    [ 0.,-1., 0.],    #  y >= 0
    [ 1., 0.,-2.],    #  x <= 2
    [ 0., 1.,-1.],    #  y <= 1
])
p = np.array([1.0, 0.5])                 # 矩形内部可行点
hs = HalfspaceIntersection(halfspaces, p)

print("顶点 intersections:\n", hs.intersections)
print("起作用的半空间下标 dual_vertices:", hs.dual_vertices)

# 2) 手写绘图：画半空间边界 + 顶点
fig, ax = plt.subplots()
x = np.linspace(-1, 3, 50)
for h in halfspaces:
    a1, a2, b = h
    if abs(a2) < 1e-12:                   # 竖直边界 x = -b/a1
        ax.axvline(-b / a1, color="gray")
    else:
        ax.plot(x, (-b - a1 * x) / a2, color="gray")
ix, iy = hs.intersections[:, 0], hs.intersections[:, 1]
ax.plot(ix, iy, "o", color="red")

# 3) 复用 _adjust_bounds 的思路自动留白
from scipy.spatial._plotutils import _adjust_bounds
_adjust_bounds(ax, hs.intersections)
ax.set_aspect("equal")
fig.savefig("halfspace.png")
print("saved halfspace.png")
```

**验收点**：
1. `intersections` 的四行集合等于 \(\{(0,0),(2,0),(2,1),(0,1)\}\)；
2. `dual_vertices` 包含全部四个下标 `[0,1,2,3]`（四条半空间都起作用，无冗余）；
3. 图中四条灰线围出矩形，四个红点落在矩形角上；
4. 坐标范围被 `_adjust_bounds` 自动设成 `intersections` 范围 ± 10%。

**延伸思考**：如果把 `interior_point` 改成落在某条边界上（如 `(0.0, 0.5)`），构造会失败——这呼应了 4.1.5 练习 1 与 4.2.3 的可行性校验。若想自动求一个尽量「居中」的可行点，可解 docstring 给出的线性规划求 Chebyshev 中心（[_qhull.pyx:L2830-L2858](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L2830-L2858)）。

---

## 6. 本讲小结

- `HalfspaceIntersection` 的输入是半空间 \([A;\,b]\)（行形式 `Ax+b<=0`），输出是这些半空间交集（可行多面体）的顶点 `intersections`，对应 Qhull 的 `qhalf` / 模式 `H`。
- Qhull **不直接求交**，而是用**对偶变换**：给定内部点 \(p\)，每个半空间映成对偶点 \(d=-A/(A\cdot p+b)\)，可行多面体的顶点 ↔ 对偶点凸包的面，最后用 `intersections = dual_equations[:,:-1] / -dual_equations[:,-1:] + p` 换算回原空间。
- `interior_point` 是**强制**且必须**严格内部**的参数（`A·p+b<0`），否则对偶分母为零或 Qhull 拒绝；可用线性规划求 Chebyshev 中心得到它。
- `dual_points` 是所有半空间的对偶点，`dual_vertices` 是「真正起作用」的半空间下标（冗余约束不在内）；`dual_volume`/`dual_area` 属于**对偶凸包**而非原空间。
- `_plotutils.py` 提供 `delaunay_plot_2d`/`convex_hull_plot_2d`/`voronoi_plot_2d` 三个**仅 2D** 的绘图函数，统一约定：可选 `ax`、返回 `figure`、收尾 `_adjust_bounds`；`voronoi_plot_2d` 额外处理无限脊（虚线 + 法向延伸，含修 GH#19653 的 `aspect_factor`）。
- `_get_axes`（惰性导入 matplotlib）与 `_adjust_bounds`（±10% 极差留白）是三个函数的公共底盘；半空间求交没有官方绘图助手，需手写。

---

## 7. 下一步学习建议

- **下沉到 Qhull 封装机制**：本讲只用了 `_Qhull` 的 `get_hull_facets`/`get_hull_points`/`volume_area`，这些方法、`_Qhull` 的生命周期（`__dealloc__`/`close`）与 `_qhull.pxd` 对 C 库的声明，留待 **u10-l1（_Qhull 底层封装与资源管理）** 与 **u10-l2（_QhullUser 与几何数据提取）** 详细拆解。
- **补全「壳类」全景**：至此 Delaunay / ConvexHull / Voronoi / HalfspaceIntersection 四个高层壳都已讲完，可回头对比它们各自 `_update` 的差异——这是理解 u10 的好预热。
- **进入距离度量**：u3 单元（Qhull 几何）到此结束，下一大块是 **u4（距离度量基础）**，从 `euclidean`/`minkowski` 等向量距离函数入手，那是与「几何剖分」正交的另一条主线。
- **若关心可视化扩展**：可阅读 `voronoi_plot_2d` 的 `aspect_factor` 修复（GH#19653）与对应回归测试，学习如何为绘图函数写「精确坐标断言」型测试。
