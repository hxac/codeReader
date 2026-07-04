# Voronoi 图

## 1. 本讲目标

本讲专讲 `scipy.spatial.Voronoi`（凸包在 u3-l2，Delaunay 在 u3-l1 已讲过）。读完本讲你应该能够：

- 说清 Voronoi 图是什么，以及它和 Delaunay 三角剖分的**对偶关系**；
- 用 `Voronoi` 类构造一张 Voronoi 图；
- 读懂 `vertices`、`regions`、`point_region`、`ridge_vertices`、`ridge_points` 这五个数据结构的**编码方式**；
- 掌握 **`-1` 表示无穷远点**的约定，区分「有限脊」与「无限脊」；
- 复现官方教程里「沿法向延伸画出无限脊」的绘制逻辑。

---

## 2. 前置知识

本讲建立在前两讲之上，你需要先理解：

- **凸包**（u3-l2）：包住所有点的最小凸形；
- **Delaunay 三角剖分**（u3-l1）：满足「空外接圆」准则的三角剖分，Qhull 靠「把点抬到抛物面 \(z=\lVert x\rVert^2\) 后求下凸包」来完成；
- **`_QhullUser → _Qhull`** 这一高层壳到底层 C 库的调用链。

几个本讲用到的几何术语，先用大白话解释：

- **外心（circumcenter）**：三角形三条边中垂线的交点，即到三个顶点距离相等的点。
- **种子点（site / generator）**：构造 Voronoi 图时输入的那组点，每个点「拥有」一个区域。
- **脊（ridge）**：在 2D 里就是线段，是两个相邻 Voronoi 区域之间的边界。

**Voronoi 图的直觉**：给定一组种子点，把整个空间按下述规则切分——空间里每个位置都归给「离它最近」的那个种子点；所有归同一个种子点的位置连成一片，叫一个**区域（region / cell）**。区域与区域之间的边界，就是 Voronoi 脊。

形式化地，种子点 \(p_i\) 的区域定义为：

\[
V_i = \{\, x \mid d(x, p_i) \le d(x, p_k)\ \text{对所有}\ k \,\}
\]

在 2D 里，两个种子点 \(p_i, p_j\) 之间的脊，正是它们连线的**垂直平分线**上的一段。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [_qhull.pyx:L2555-L2714](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L2555-L2714) | `Voronoi` 高层壳类，校验输入、拼 qhull 选项、把计算下放给底层 |
| [_qhull.pyx:L2668-L2700](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L2668-L2700) | `Voronoi.__init__` 与 `_update`，调用 `get_voronoi_diagram` 装填属性 |
| [_qhull.pyx:L827-L969](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L827-L969) | `_Qhull.get_voronoi_diagram`，真正从 Qhull C 库提取整张图 |
| [_qhull.pyx:L1049-L1083](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L1049-L1083) | `_visit_voronoi` 回调，逐条收集脊（ridge） |
| [doc/source/tutorial/spatial.rst:L161-L283](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/doc/source/tutorial/spatial.rst#L161-L283) | 官方教程，含无限脊的绘制逻辑 |

---

## 4. 核心概念与源码讲解

### 4.1 Voronoi 类与 Delaunay 的对偶关系

#### 4.1.1 概念说明

`Voronoi` 和 `Delaunay` 是**同一个几何结构的两面**，二者严格对偶：

| Delaunay 一侧 | Voronoi 一侧 |
|---|---|
| 一个单纯形（2D 三角形） | 一个 Voronoi **顶点**（即该三角形的外心） |
| 两个相邻单纯形共享的一条 ridge（2D 边） | 连接两个外心的一条 Voronoi **脊** |
| 一个输入点（种子点） | 一个 Voronoi **区域**（cell） |

为什么是对偶？因为 Delaunay 的「空外接圆」准则，等价于说「三角形外心到三个顶点等距且没有任何其它点更近」——这正好是 Voronoi 顶点的定义。所以 Qhull **先算 Delaunay**（提升到抛物面求下凸包），再把 Voronoi 当作 Delaunay 的对偶**导出**，而不是另起炉灶算一遍。

源码直接印证了这一点：Voronoi 用模式串 `b"v"`，但在 `_Qhull.__init__` 里它和 Delaunay 的 `b"d"` 一起被归入同一族——`_is_delaunay = 1`。

#### 4.1.2 核心流程

`Voronoi` 沿用前两讲建立的壳层结构 `Voronoi → _QhullUser → _Qhull`，调用链如下：

```text
Voronoi.__init__(points)
    └─ _Qhull(b"v", points, "Qbb Qc Qz")      # v = Voronoi 模式
           └─ qh_new_qhull_scipy(...)          # 调用 Qhull C 库
    └─ _QhullUser.__init__(qhull)
           └─ self._update(qhull)              # Voronoi._update 覆写了它
                  └─ qhull.get_voronoi_diagram()  # 提取五元组
                         ├─ qh_eachvoronoi_all + _visit_voronoi  # 收集脊
                         ├─ 遍历 vertex_list                  # 收集区域
                         └─ 遍历 facet_list                   # 收集顶点
```

伪代码概括：

```python
class Voronoi(_QhullUser):
    def __init__(self, points, ...):
        qhull = _Qhull(b"v", points, "Qbb Qc Qz")
        _QhullUser.__init__(self, qhull)     # 内部会调 self._update

    def _update(self, qhull):
        (self.vertices, self.ridge_points, self.ridge_vertices,
         self.regions, self.point_region) = qhull.get_voronoi_diagram()
```

#### 4.1.3 源码精读

[`Voronoi` 类定义与完整 docstring](_qhull.pyx:L2555-L2567)：类体头部，docstring 列出了 `points / vertices / ridge_points / ridge_vertices / regions / point_region` 全部属性及其形状。

[`Voronoi.__init__`](_qhull.pyx:L2668-L2691)：这是高层壳的入口。它做四件事——(1) 拒绝 masked array；(2) 把输入转成 C 连续 float64 并要求是二维；(3) 拼 qhull_options，**默认 `"Qbb Qc Qz"`**，当 `ndim ≥ 5` 时追加 `" Qx"`（与 Delaunay 同款的高维数值稳定选项）；(4) 以模式 `b"v"` 启动底层 `_Qhull`。

[模式 `b"v"` 被归入 Delaunay 族](_qhull.pyx:L317-L320)：`if mode_option in (b"d", b"v"): self._is_delaunay = 1`。这一行是「Voronoi 由 Delaunay 对偶导出」在源码里的直接证据——底层把 Voronoi 当 Delaunay 来算，只是输出阶段换成 `get_voronoi_diagram`。

[`Voronoi._update`](_qhull.pyx:L2693-L2700)：覆写基类的 `_update`，把 `get_voronoi_diagram()` 返回的五元组分别赋给 `vertices / ridge_points / ridge_vertices / regions / point_region`，再调 `_QhullUser._update` 装填 `points / ndim / npoints` 等公共字段。

#### 4.1.4 代码实践

实践目标：亲手构造一张 Voronoi 图，验证「Voronoi 顶点 = 内部 Delaunay 三角形的外心」这一对偶关系。

操作步骤（示例代码，可直接运行）：

```python
# 示例代码
import numpy as np
from scipy.spatial import Voronoi, Delaunay

points = np.array([[0, 0], [0, 1], [0, 2],
                   [1, 0], [1, 1], [1, 2],
                   [2, 0], [2, 1], [2, 2]])

vor = Voronoi(points)
tri = Delaunay(points)

print("Voronoi 顶点数 =", vor.vertices.shape[0])
print("Delaunay 三角形数 =", tri.simplices.shape[0])
```

需要观察的现象与预期结果：

- `vor.vertices` 应为 4 个点，正是中央 4 个三角形的四个外心 `(0.5,0.5),(0.5,1.5),(1.5,0.5),(1.5,1.5)`；
- 注意：**Voronoi 顶点数（4）一般不等于 Delaunay 三角形总数**，因为只有「外心落在凸包内部」的三角形才贡献有限 Voronoi 顶点，凸包边界上的三角形其外心是「无穷远点」，不进入 `vertices`。具体数量关系待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `vor.vertices` 的行数通常远少于输入点数？

> **参考答案**：Voronoi 顶点是 Delaunay 三角形的外心。只有「内部」三角形贡献有限顶点；许多输入点（尤其凸包边界上的点）只是无限区域的种子，本身不产生 Voronoi 顶点。因此 9 个点可能只有 4 个 Voronoi 顶点。

**练习 2**：把 `Voronoi(points, furthest_site=True)` 与默认结果对比，顶点数量会怎样变化？

> **参考答案**：`furthest_site=True` 计算的是**最远点 Voronoi 图**（内部把选项 `Qz` 换成 `Qu`，见 _qhull.pyx 的选项处理），它对偶于「最远点 Delaunay」（上凸包）。最远点 Voronoi 的区域只覆盖凸包内部，顶点布局与近邻版完全不同——具体数值待本地验证。

---

### 4.2 vertices / regions / point_region 编码

#### 4.2.1 概念说明

整张 Voronoi 图被拆成三个互相索引的数据结构：

- **`vertices`**：所有**有限** Voronoi 顶点的坐标，形状 `(nvertices, ndim)`；
- **`regions`**：一个列表的列表，第 `k` 个子列表是第 `k` 个区域的顶点索引（指向 `vertices`），其中的 `-1` 表示「无穷远顶点」；
- **`point_region`**：长度等于输入点数的整数数组，`point_region[i]` 给出第 `i` 个输入点对应的区域在 `regions` 里的**下标**。

为什么需要 `point_region` 这个中间层，而不是直接让「第 i 个点 ↔ 第 i 个区域」？因为默认选项里的 `Qz` 会在内部额外塞一个「无穷远点」辅助计算，导致 `regions` 比 `point_region` 多一项，下标不再一一对应。`point_region` 正是来抹平这个错位的。

#### 4.2.2 核心流程

`get_voronoi_diagram` 分三段提取这三个结构（[_qhull.pyx:L892-L964](_qhull.pyx#L892-L964)）：

1. **收集脊**（见 4.3 节）；
2. **收集区域**：遍历 Qhull 的 `vertex_list`（每个 vertex 对应一个输入种子点）。对每个 vertex，先把它相邻的 facet 邻居排序，再把每个邻居的 `visitid - 1` 作为 Voronoi 顶点索引追加进当前区域。关键约定：`visitid == 0` 时 `i == -1`，即无穷远；用 `inf_seen` 标志保证每个区域里**至多一个** `-1`。
3. **收集顶点 + 修正 point_region**：遍历 `facet_list`，凡 `facet.visitid > 0` 的是有限 Voronoi 顶点，用 `qh_facetcenter(facet.vertices)` 求其坐标（即对偶 Delaunay 单纯形的外心）。

一个细节：`qh_eachvoronoi_all` 在收集脊时，已经把各 facet 的 `visitid` 初始化为「Voronoi 顶点编号」（注释见 [_qhull.pyx:L889-L890](_qhull.pyx#L889-L890)），所以后两段可以直接复用 `visitid - 1` 作为顶点索引。

#### 4.2.3 源码精读

[区域收集循环](_qhull.pyx:L892-L924)：`point_region` 先全部初始化为 `-1`（L895-L897）；对每个 vertex，`i = qh_pointid(...)` 得到输入点下标，`point_region[i] = len(regions)` 记录该点对应的区域号（L906）；`inf_seen` 去重保证每区域至多一个无穷远顶点（L908-L918）；特例 `len(cur_region)==1 and cur_region[0]==-1` 时把区域改写成空列表 `[]`（L919-L921），这是为 `Qz` 引入的「内部无穷远点」准备的空区域，行为与 qvoronoi 的 `o` 输出一致。

[顶点收集与 point_region 修正](_qhull.pyx:L926-L964)：`center = qh_facetcenter(...)` 求外心作为 Voronoi 顶点坐标（L935）；`voronoi_vertices[facet.visitid-1, k] = center[k]`（L946）；若 facet 带 `coplanarset`，则把共面点也映射到最近顶点的区域（L950-L960），保证每个输入点都有归属。

#### 4.2.4 代码实践

实践目标：弄清 `regions` 与 `point_region` 的关系，并用它们取出「中心点」的唯一有界区域。

操作步骤（示例代码）：

```python
# 示例代码
import numpy as np
from scipy.spatial import Voronoi

points = np.array([[0, 0], [0, 1], [0, 2],
                   [1, 0], [1, 1], [1, 2],
                   [2, 0], [2, 1], [2, 2]])
vor = Voronoi(points)

print("regions =", vor.regions)
print("len(regions) =", len(vor.regions))
print("len(point_region) =", len(vor.point_region))

# 中心点索引为 4，取它对应的区域
center_region_idx = vor.point_region[4]
print("中心点的区域顶点索引 =", vor.regions[center_region_idx])
```

需要观察的现象与预期结果：

- `len(regions)` 应为 **10**，而 `len(point_region)` 应为 **9**（输入点数），差 1 正是 `Qz` 多出的那个空区域 `[]`；
- 中心点（索引 4）的区域应为 `[0, 1, 3, 2]`，是**唯一不含 `-1` 的有界区域**；
- 其余区域都含 `-1`，说明它们是无限区域。

#### 4.2.5 小练习与答案

**练习 1**：`len(vor.regions)` 与 `len(vor.point_region)` 为什么不相等？

> **参考答案**：默认选项含 `Qz`，它在内部添加一个辅助计算的「无穷远点」，于是 `regions` 多出一个空列表 `[]` 区域；而 `point_region` 只对**真实输入点**编索引，长度等于输入点数。所以 `len(regions) == len(point_region) + 1`。

**练习 2**：如何判断一个区域是不是有界（有限）的？

> **参考答案**：有界区域**不含 `-1`** 且非空；只要 `regions[k]` 里出现 `-1`，该区域就是无限区域（向外延伸到无穷远）。空列表 `[]` 则是 `Qz` 的辅助区域，不代表任何真实输入点的可见区域。

---

### 4.3 ridge_vertices / ridge_points 与无穷远点（-1）约定

#### 4.3.1 概念说明

脊（ridge）是 Voronoi 图里最容易踩坑的部分，本讲的核心实践也在这里。两个相关属性：

- **`ridge_points`**：形状 `(nridges, 2)` 的整数数组，每行 `[i, j]` 表示这条脊位于输入点 `i` 与 `j` 之间——几何上，这条脊就是 \(p_i p_j\) 连线的垂直平分线（的一段）。
- **`ridge_vertices`**：列表的列表，第 `k` 项给出第 `k` 条脊由哪些 Voronoi 顶点构成（索引指向 `vertices`）。

**`-1` 约定**：当一条脊的一端延伸到无穷远时，`ridge_vertices` 里对应位置写 **`-1`**（不存在那个有限顶点）。于是：

- 有限脊：两个索引都 `≥ 0`，例如 `[0, 1]`，是首尾相接的有限线段；
- 无限脊：含一个 `-1`，例如 `[-1, 0]`，一端是有限顶点，另一端射向无穷远。

什么时候出现无限脊？当某条 \(p_i p_j\) 中垂线落在**凸包边界上**时——此时两个区域都向外敞开，脊的一端没有「封口」的顶点，便记作 `-1`。

#### 4.3.2 核心流程

脊的收集由 Qhull 的 `qh_eachvoronoi_all` 驱动，它对每条脊回调一次 [_visit_voronoi](_qhull.pyx#L1049-L1083)：

```text
get_voronoi_diagram
 └─ qh_eachvoronoi_all(..., &_visit_voronoi, ...)      # 遍历所有脊
        └─ _visit_voronoi(vertex, vertexA, centers, ...)   # 每条脊调一次
               ├─ point_1 = pointid(vertex);   point_2 = pointid(vertexA)   → ridge_points
               └─ for c in centers: ix = c.visitid - 1                      → ridge_vertices（-1 即无穷远）
```

回调里 `ix = centers.e[i].visitid - 1`：`visitid` 为 0 的 facet 是无穷远顶点，减 1 得 `-1`，这就是 `-1` 约定的产生处。

#### 4.3.3 源码精读

[脊收集的初始化与驱动](_qhull.pyx:L875-L887)：`_ridge_points` 预分配为 `(10, 2)`、`_ridge_vertices` 为空列表，调用 `qh_eachvoronoi_all(..., qh_RIDGEall, 1)`（`qh_RIDGEall` 表示取所有脊，最后一个 `1` 是 `inorder` 有序输出）。

[`_visit_voronoi` 回调](_qhull.pyx:L1049-L1083)：L1067-L1068 取 `point_1 / point_2` 写入 `_ridge_points`（这条脊在哪两点之间）；L1076-L1078 遍历 `centers`，用 `visitid - 1` 得到顶点索引并追加进 `cur_vertices`——**`-1` 正来源于 `visitid == 0`**；L1079 把整条脊的顶点列表 append 进 `_ridge_vertices`。数组放不下时 L1058-L1064 做扩容。

#### 4.3.4 代码实践 ⭐（本讲核心实践）

实践目标：复现官方教程里「有限脊画实线、无限脊沿法向延伸画虚线」的绘制逻辑（[doc/source/tutorial/spatial.rst:L267-L281](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/doc/source/tutorial/spatial.rst#L267-L281)），理解每一步如何处理 `-1`。

操作步骤（以下代码改编自官方教程，可直接运行）：

```python
# 示例代码（改编自 scipy 官方教程 doc/source/tutorial/spatial.rst）
import numpy as np
import matplotlib.pyplot as plt
from scipy.spatial import Voronoi

points = np.array([[0, 0], [0, 1], [0, 2],
                   [1, 0], [1, 1], [1, 2],
                   [2, 0], [2, 1], [2, 2]])
vor = Voronoi(points)

plt.plot(points[:, 0], points[:, 1], 'o')
plt.plot(vor.vertices[:, 0], vor.vertices[:, 1], '*')
plt.xlim(-1, 3); plt.ylim(-1, 3)

# (1) 有限脊：两端索引都 >= 0，直接连实线
for simplex in vor.ridge_vertices:
    simplex = np.asarray(simplex)
    if np.all(simplex >= 0):
        plt.plot(vor.vertices[simplex, 0], vor.vertices[simplex, 1], 'k-')

# (2) 无限脊：含 -1，从有限端沿「外法向」延伸画虚线
center = points.mean(axis=0)
for pointidx, simplex in zip(vor.ridge_points, vor.ridge_vertices):
    simplex = np.asarray(simplex)
    if np.any(simplex < 0):
        i = simplex[simplex >= 0][0]                 # 有限端 Voronoi 顶点
        t = points[pointidx[1]] - points[pointidx[0]]  # 两点连线方向（切向）
        t = t / np.linalg.norm(t)
        n = np.array([-t[1], t[0]])                  # 旋转 90° 得法向
        midpoint = points[pointidx].mean(axis=0)     # 两点中点（在脊上）
        # 用「中点到整体质心」的向量定法向正负，保证指向区域外侧
        far_point = vor.vertices[i] + np.sign(np.dot(midpoint - center, n)) * n * 100
        plt.plot([vor.vertices[i, 0], far_point[0]],
                 [vor.vertices[i, 1], far_point[1]], 'k--')

plt.show()
```

需要观察的现象与预期结果：

- 4 条有限脊（实线）围出中心的那个有界四边形区域；
- 其余含 `-1` 的脊画成虚线，从有限 Voronoi 顶点向**外**延伸到画布边缘；
- 9 个输入点（圆点）中，只有中心点 (1,1) 被一个封闭四边形完全包围，其余 8 个点的区域都敞向无穷远。

> 说明：无限脊的延伸长度 `* 100` 是教程里取的一个「足够远」的画图常数；`np.sign(np.dot(midpoint - center, n))` 决定法向指向哪一侧——因为整体质心 `center` 近似落在区域内部，法向应背向它，才能把脊画到区域外面。具体渲染效果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么无限脊的方向要用「脊中点到整体质心」的向量来判定？

> **参考答案**：Voronoi 区域总是凸的。无限脊的有限端是两个相邻区域共用的顶点，无限方向必须**远离**区域内部。整体质心 `center` 近似位于区域内部一侧，因此 `np.dot(midpoint - center, n)` 的符号能告诉我们法向 `n` 当前是指向「内侧」还是「外侧」；取 `sign(...)` 后让法向恒指向外侧，脊就被正确地画到区域之外。

**练习 2**：`ridge_points` 的每行 `[i, j]` 与 `ridge_vertices` 的对应项，在几何上是什么关系？

> **参考答案**：`ridge_points` 的 `[i, j]` 给出脊两侧的两个种子点 \(p_i, p_j\)；这条 Voronoi 脊正是线段 \(p_i p_j\) 的**垂直平分线**（2D 下）。`ridge_vertices` 给出该中垂线由哪些 Voronoi 顶点（外心）拼成；若中垂线落在凸包边界，则一端无有限顶点，记作 `-1`。

**练习 3**：`vor.ridge_dict` 这个属性懒加载了什么？它和 `ridge_points / ridge_vertices` 是什么关系？

> **参考答案**：见 [_qhull.pyx:L2709-L2714](_qhull.pyx#L2709-L2714)，`ridge_dict` 把 `ridge_points` 的每行转成元组键 `(i, j)`，映射到对应的 `ridge_vertices` 子列表，方便「已知两点查脊」。它只是这两个数组的字典视图，不含新信息。

---

## 5. 综合实践

把本讲三块知识串起来：写一个小脚本，**自动给「唯一有界区域」涂色**，并在图上标注每条脊是有限还是无限。

要求：

1. 用 `regions` + `point_region` 找出唯一不含 `-1` 且非空的区域，用 `matplotlib.patches.Polygon` 把它填色；
2. 统计 `ridge_vertices` 中有限脊（`np.all(simplex >= 0)`）与无限脊（`np.any(simplex < 0)`）各有多少条，打印计数；
3. 用 `ridge_dict` 查询中心点 (1,1) 与其右邻 (2,1) 之间的脊（键 `(4, 7)` 或 `(7, 4)`，注意顺序），打印该脊的顶点索引。

参考思路（示例代码骨架）：

```python
# 示例代码（骨架，部分待你补全）
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon
from scipy.spatial import Voronoi

points = np.array([[0,0],[0,1],[0,2],[1,0],[1,1],[1,2],[2,0],[2,1],[2,2]])
vor = Voronoi(points)

# 1) 找有界区域并涂色
bounded = [r for r in vor.regions if len(r) > 0 and all(v >= 0 for v in r)]
for r in bounded:
    poly = Polygon(vor.vertices[r], alpha=0.3, color='C0')
    plt.gca().add_patch(poly)

# 2) 统计脊类型
n_finite = sum(1 for s in vor.ridge_vertices if np.all(np.asarray(s) >= 0))
n_infinite = sum(1 for s in vor.ridge_vertices if np.any(np.asarray(s) < 0))
print(f"有限脊 {n_finite} 条，无限脊 {n_infinite} 条")

# 3) 用 ridge_dict 查询 (4,7) 之间的脊
rd = vor.ridge_dict
key = (4, 7) if (4, 7) in rd else (7, 4)
print("脊", key, "的顶点 =", rd[key])
plt.plot(points[:,0], points[:,1], 'o')
plt.xlim(-1, 3); plt.ylim(-1, 3); plt.gca().set_aspect('equal')
plt.show()
```

预期：应找到 1 个有界区域（中心四边形），有限脊 4 条、无限脊 8 条（共 12 条脊），与官方教程示例一致；`(4,7)` 之间是一条有限脊。具体数值待本地验证。

---

## 6. 本讲小结

- `Voronoi` 是 `Delaunay` 的**对偶**：Delaunay 单纯形 ↔ Voronoi 顶点（外心）、Delaunay ridge ↔ Voronoi 脊、输入点 ↔ Voronoi 区域；源码里模式 `b"v"` 与 `b"d"` 同被归入 `_is_delaunay=1`。
- `Voronoi` 沿用 `Voronoi → _QhullUser → _Qhull` 壳层，`_update` 调一次 `get_voronoi_diagram()` 即装填全部五个属性。
- 整张图由 `vertices`（有限顶点）、`regions`（区域顶点索引）、`point_region`（点→区域下标）、`ridge_points`（脊在哪两点间）、`ridge_vertices`（脊由哪些顶点构成）共同编码。
- **`-1` 约定**贯穿 `ridge_vertices` 与 `regions`：`-1` 表示无穷远顶点，含 `-1` 的脊/区域都是「无限」的；`Qz` 选项会额外产生一个空区域 `[]`，使 `len(regions) == len(point_region) + 1`。
- 绘制无限脊的关键是：从有限端出发，沿「脊中点到整体质心」反方向的外法向延伸——这正是官方教程那段代码的几何含义。

---

## 7. 下一步学习建议

- **u3-l4 HalfspaceIntersection 与绘图助手**：`HalfspaceIntersection` 同样基于 `_QhullUser`，且 `voronoi_plot_2d` 就在 `_plotutils.py` 里，可以拿来和本讲手写的绘制对比；
- **u5-l1 SphericalVoronoi**：把 Voronoi 思想搬到**球面**上，区域用立体角度量，是本讲平面 Voronoi 的自然推广；
- **u10-l1 / u10-l2（专家层）**：若想彻底搞清 `get_voronoi_diagram` 如何与 Qhull C 库的 `qh_eachvoronoi_all`、`qh_facetcenter` 交互，以及 `_Qhull` 的资源生命周期，可下沉到 Qhull Cython 封装机制两讲。
