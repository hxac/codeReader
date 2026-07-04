# scipy.spatial 项目概览与定位

## 1. 本讲目标

本讲是整本《scipy.spatial 学习手册》的第一篇，面向从零开始的读者。读完本讲，你应该能够：

- 说清 `scipy.spatial` 在 SciPy 生态里**是什么、解决什么问题**。
- 列出它的**四大能力域**：最近邻查询、距离度量、Qhull 几何剖分、空间变换。
- 通过阅读 [`__init__.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/__init__.py) 找到它对外导出的**公共对象**，并理解 `__all__` 是如何被自动拼装出来的。

本讲不涉及算法细节，目标是建立「地图」：知道这个子包里有哪些抽屉，每个抽屉大致装什么，后面几篇讲义再逐个抽屉深入。

## 2. 前置知识

- **子包（subpackage）**：Python 里一个目录如果包含 `__init__.py`，就被当作一个可导入的包。`scipy.spatial` 就是 `scipy` 大包下的一个子包，目录就在 `scipy/spatial/`。
- **导入星号 `from .xxx import *`**：把某个内部模块里所有「不以下划线开头」的名字搬进当前命名空间。这是 `scipy/spatial/__init__.py` 把内部实现汇总成公共 API 的主要手法。
- **`__all__`**：一个列表，声明「`from package import *` 时应该导出哪些名字」。它同时也是给使用者的「公共 API 清单」。
- **几何直觉**：知道什么是「最近邻」「三角剖分」「凸包」即可，本讲会用一句话解释，深入算法留到后续讲义。

## 3. 本讲源码地图

本讲只盯住两个文件：

| 文件 | 作用 |
| --- | --- |
| [`__init__.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/__init__.py) | 子包入口。它的模块文档字符串就是公共 API 目录；它的若干行 `import` 决定了哪些对象被导出。 |
| [`doc/source/tutorial/spatial.rst`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/doc/source/tutorial/spatial.rst) | 官方教程。用通俗语言 + 可运行示例说明 `scipy.spatial` 能做什么，是理解「定位」的最佳材料。 |

其余 `_kdtree.py`、`_qhull.pyx`、`distance.py`、`transform/` 等内部模块在本讲里只作为「被 `import *` 的来源」出现，它们的内部实现是后续讲义的主题。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. `scipy.spatial` 是什么——定位与它解决的问题。
2. 四大能力域，以及 `__init__.py` 如何用导入语句把它们接上线。
3. `__all__` 的生成机制与公共对象的导出顺序。

### 4.1 scipy.spatial 是什么：定位与解决的问题

#### 4.1.1 概念说明

`scipy.spatial` 是 SciPy 里专门处理**空间几何与空间数据结构**的子包。所谓「空间」，可以理解成「带坐标的点」所在的空间。它解决两类典型问题：

- **「离我最近的是谁」类问题**：给一堆点，快速查询某个查询点的 k 近邻、半径邻域、成对距离。这是最近邻（KDTree）的领域。
- **「这些点的几何形状是什么」类问题**：给一堆点，求它们的三角剖分、凸包、Voronoi 图、球面剖分。这是几何剖分（Qhull）的领域。

此外，它还提供一组**距离度量函数**（欧氏、曼哈顿、余弦……）和**空间变换**（三维旋转、刚体变换）。官方教程一句话点明了它的定位：

> `scipy.spatial` can compute triangulations, Voronoi diagrams, and convex hulls of a set of points, by leveraging the `Qhull` library. Moreover, it contains `KDTree` implementations for nearest-neighbor point queries, and utilities for distance computations in various metrics.
> ——见 [doc/source/tutorial/spatial.rst:8-13](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/doc/source/tutorial/spatial.rst#L8-L13)

#### 4.1.2 核心流程

从「使用者视角」，`scipy.spatial` 的工作流很简单：

1. 准备一组点（通常是 NumPy 的 `(n, dim)` 数组）。
2. 把点喂给某个类（如 `Delaunay(points)`、`KDTree(points)`）。
3. 从返回对象上取属性或调用方法得到几何/邻居结果。

教程里给了一个最小可运行例子：4 个点做三角剖分，再从 `tri.simplices` 读出每个三角形由哪几个点组成。

#### 4.1.3 源码精读

教程里的 Delaunay 最小例子（这行号是 `spatial.rst` 里的，注意它位于 `doc/` 而非 `scipy/spatial/`）：

```python
>>> from scipy.spatial import Delaunay
>>> import numpy as np
>>> points = np.array([[0, 0], [0, 1.1], [1, 0], [1, 1]])
>>> tri = Delaunay(points)
>>> tri.simplices[i,:]      # 第 i 个三角形由哪几个点的下标组成
```

参见 [doc/source/tutorial/spatial.rst:28-59](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/doc/source/tutorial/spatial.rst#L28-L59)。这段代码演示了「定位」中最重要的一点：用户只面对 `Delaunay` 这个名字，不需要知道它背后是 Qhull C 库、是 Cython 封装。**把复杂实现藏起来、只暴露简洁类名**，正是 `scipy.spatial` 的设计哲学。

`__init__.py` 顶部的模块文档字符串本身就是一份公共 API 目录，它用 `autosummary` 把对象按主题分组列出（最近邻、距离、几何、绘图、杂项、错误），参见 [__init__.py:1-109](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/__init__.py#L1-L109)。

#### 4.1.4 代码实践

1. **实践目标**：跑通官方教程里的最小 Delaunay 例子，确认环境里能正常导入 `scipy.spatial`。
2. **操作步骤**：在安装了 SciPy 的 Python 环境里执行下面这段脚本。
3. **需要观察的现象**：打印出 `simplices` 数组，每个元素是一行 3 个整数下标。
4. **预期结果**：`tri.simplices` 形状为 `(2, 3)`，即 4 个点被剖成 2 个三角形。
5. 如果无法确定运行结果，明确写「待本地验证」。

```python
# 示例代码：复现官方教程最小例子
from scipy.spatial import Delaunay
import numpy as np

points = np.array([[0, 0], [0, 1.1], [1, 0], [1, 1]])
tri = Delaunay(points)
print(tri.simplices)        # 预期: 形如 [[1 0 2], [1 2 3]] 的 int32 数组
print(tri.neighbors)        # 预期: 每个三角形的邻居三角形下标，-1 表示无邻居
```

> 说明：具体 `simplices` 的列顺序取决于 Qhull 内部，但总数应为 2 个三角形（待本地验证确切下标）。

#### 4.1.5 小练习与答案

**练习 1**：把上面的 4 个点改成共线的 3 个点（如 `[[0,0],[1,1],[2,2]]`），调用 `Delaunay` 会发生什么？

**答案**：共线点无法构成三角形，Qhull 会抛出 `QhullError`（提示退化/degenerate）。这正说明 `scipy.spatial` 里的几何计算对输入有要求——后面的几何剖分讲义会详细讲 `qhull_options` 如何绕过退化。

**练习 2**：用一句话概括 `scipy.spatial` 的定位。

**答案**：它是 SciPy 中负责空间几何（三角剖分/凸包/Voronoi）与空间数据结构（KDTree 最近邻）以及距离度量的子包，底层借助 Qhull 等 C/C++ 库实现高性能。

---

### 4.2 四大能力域与 __init__.py 的导入结构

#### 4.2.1 概念说明

`scipy.spatial` 的能力可以归纳为**四大域**：

| 能力域 | 解决的问题 | 代表公共对象 | 主要内部来源 |
| --- | --- | --- | --- |
| 最近邻查询 | 快速 k 近邻 / 邻域 / 成对距离 | `KDTree`、`cKDTree`、`Rectangle` | `_kdtree.py`、`_ckdtree.pyx` |
| 距离度量 | 各类向量/集合距离、`pdist`/`cdist` | `distance` 子模块（`euclidean`、`pdist` 等） | `distance/` |
| Qhull 几何剖分 | 三角剖分、凸包、Voronoi、半空间交 | `Delaunay`、`ConvexHull`、`Voronoi`、`HalfspaceIntersection`、`SphericalVoronoi` | `_qhull.pyx`、`_spherical_voronoi.py` |
| 空间变换 | 三维旋转、刚体变换、球面插值、形状对齐 | `transform` 子模块（`Rotation`、`RigidTransform`）、`geometric_slerp`、`procrustes` | `transform/`、`_geometric_slerp.py`、`_procrustes.py` |

注意一个**关键区分**：

- **直接挂载的对象**（如 `KDTree`、`Delaunay`）由 `__init__.py` 用 `from ._xxx import *` 直接搬上来，用 `scipy.spatial.KDTree` 即可访问。
- **子模块**（`distance`、`transform`）则作为命名空间保留，对象藏在下一层，例如 `scipy.spatial.distance.euclidean`、`scipy.spatial.transform.Rotation`。

这两种「挂载方式」是理解公共 API 的两把钥匙。

#### 4.2.2 核心流程

`__init__.py` 的导入语句就是一张「接线图」。它的执行顺序大致是：

1. 用 `from ._xxx import *` 把内部实现模块的公共名字逐个搬到 `scipy.spatial` 命名空间。
2. 显式 `from . import distance, transform` 把两个子模块挂为命名空间。
3. 最后用 `__all__ = [s for s in dir() if not s.startswith('_')]` 把到目前为止所有「非下划线开头」的名字收编成公共 API 清单。

#### 4.2.3 源码精读

直接挂载的导入块（注意每行对应一个能力域的来源模块）：

```python
from ._kdtree import *                       # 最近邻: KDTree / Rectangle / minkowski_*
from ._ckdtree import *                      # 最近邻: cKDTree
from ._qhull import *                        # 几何: Delaunay/ConvexHull/Voronoi/...
from ._spherical_voronoi import SphericalVoronoi  # 几何: 球面 Voronoi
from ._plotutils import *                    # 几何: 三个 2D 绘图助手
from ._procrustes import procrustes          # 变换: 形状对齐
from ._geometric_slerp import geometric_slerp  # 变换: 球面线性插值
```

参见 [__init__.py:111-117](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/__init__.py#L111-L117)。这一段把「最近邻 / 几何 / 变换」三类的对象直接拉到了顶层。

紧接着是**弃用命名空间**（v2.0 将移除）：

```python
# Deprecated namespaces, to be removed in v2.0.0
from . import ckdtree, kdtree, qhull
```

参见 [__init__.py:119-120](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/__init__.py#L119-L120)。历史上用户曾用 `scipy.spatial.kdtree.KDTree`、`scipy.spatial.qhull.Delaunay`、`scipy.spatial.ckdtree.cKDTree` 这种「带中间模块名」的写法。如今真正的实现搬到了带下划线的私有模块（`_kdtree`、`_qhull`、`_ckdtree`），这三个旧名字只保留为转发壳，访问时会触发 `DeprecationWarning`（细节见下一讲 u1-l2）。

然后是两个**子模块命名空间**：

```python
from . import distance, transform
```

参见 [__init__.py:124](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/__init__.py#L124)。`distance` 与 `transform` 不是「把对象搬上来」，而是把整个子模块挂为属性，所以你要写 `scipy.spatial.distance.pdist`、`scipy.spatial.transform.Rotation`。

> 对照表：你可以把 `_kdtree.py` 的导出和文档字符串里的「最近邻」一节对上号——[__init__.py:20-26](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/__init__.py#L20-L26) 列出了 `KDTree`、`cKDTree`、`Rectangle`；[__init__.py:33-43](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/__init__.py#L33-L43) 列出了几何剖分类。

#### 4.2.4 代码实践

1. **实践目标**：验证「直接挂载对象」与「子模块命名空间」两种访问方式都成立，且它们指向同一个东西。
2. **操作步骤**：运行下面的脚本。
3. **需要观察的现象**：直接路径和子模块路径拿到的函数对象 `is` 相同；访问弃用模块会报警告。
4. **预期结果**：两个 `True`；并打印一条 `DeprecationWarning`。
5. 如果无法确定运行结果，明确写「待本地验证」。

```python
# 示例代码
import warnings
import scipy.spatial as s

# 直接挂载对象 vs 内部私有模块 —— 指向同一个对象
print(s.KDTree is s._kdtree.KDTree)            # 预期 True

# 子模块命名空间：对象藏在下一层
print(hasattr(s.distance, 'pdist'))            # 预期 True
print(hasattr(s.transform, 'Rotation'))        # 预期 True

# 弃用模块：访问会触发 DeprecationWarning
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    _ = s.kdtree.KDTree
    print(any(issubclass(x.category, DeprecationWarning) for x in w))  # 预期 True
```

#### 4.2.5 小练习与答案

**练习 1**：`scipy.spatial.distance` 和 `scipy.spatial.transform` 为什么没有用 `import *` 搬上来？

**答案**：因为它们各自包含大量对象（`distance` 有几十个度量函数与 `pdist/cdist/squareform` 等），全部平铺到 `scipy.spatial` 顶层会造成命名污染。保留为子模块命名空间更清晰，也符合官方文档把它俩单独列一节的做法（[__init__.py:29-31](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/__init__.py#L29-L31)、[__init__.py:14-17](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/__init__.py#L14-L17)）。

**练习 2**：把下表填完整。

| 公共对象 | 属于哪个能力域 | 来自哪个内部来源 |
| --- | --- | --- |
| `Voronoi` | 几何剖分 | `_qhull.pyx` |
| `cKDTree` | ? | ? |
| `Rotation` | ? | ? |

**答案**：`cKDTree` → 最近邻 / `_ckdtree.pyx`；`Rotation` → 空间变换 / `transform/_rotation.py`（通过 `transform` 子模块访问）。

---

### 4.3 __all__ 的生成与公共对象导出顺序

#### 4.3.1 概念说明

`__all__` 决定了 `from scipy.spatial import *` 会拿到哪些名字，也是给使用者的「官方公共 API 清单」。`scipy.spatial` 没有手写一份 `__all__`，而是用一个**动态推导**的技巧自动生成：扫描当前命名空间里所有「不以下划线开头」的名字。这样做的好处是——只要你在内部模块里新增并导出一个公共名字，它就会自动出现在 `scipy.spatial` 的公共 API 里，无需手动维护清单。

#### 4.3.2 核心流程

`__all__` 的拼装分两步：

1. **第一步推导**：在导入完所有「直接挂载」对象和弃用模块之后、导入子模块之前，执行
   `__all__ = [s for s in dir() if not s.startswith('_')]`。
   `dir()` 不带参数时返回**当前作用域里已绑定的名字，按字母序排列**（Python 中大写字母排在小写之前）。这一步会**漏掉** `distance` 和 `transform`，因为它们此时尚未导入。
2. **第二步追加**：导入子模块后，用 `__all__ += ['distance', 'transform']` 把两个子模块名补到末尾。

因此最终 `__all__` 的结构是：**字母序的主体 + 末尾两个子模块名**。

#### 4.3.3 源码精读

第一步自动推导（关键的一行）：

```python
__all__ = [s for s in dir() if not s.startswith('_')]
```

参见 [__init__.py:122](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/__init__.py#L122)。注意它在第 124 行 `from . import distance, transform` **之前**执行。

第二步追加子模块：

```python
from . import distance, transform

__all__ += ['distance', 'transform']
```

参见 [__init__.py:124-126](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/__init__.py#L124-L126)。

依据各来源模块自身的 `__all__`，可以精确还原 `scipy.spatial.__all__` 的内容：

- `_kdtree.py` 导出：`minkowski_distance_p`、`minkowski_distance`、`distance_matrix`、`Rectangle`、`KDTree`（见 [_kdtree.py:11-13](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_kdtree.py#L11-L13)）
- `_ckdtree.pyx` 导出：`cKDTree`（见 [_ckdtree.pyx:33](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_ckdtree.pyx#L33)）
- `_qhull.pyx` 导出：`Delaunay`、`ConvexHull`、`QhullError`、`Voronoi`、`HalfspaceIntersection`、`tsearch`（见 [_qhull.pyx:37](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L37)）
- `_spherical_voronoi.py` 导出：`SphericalVoronoi`；`_plotutils.py` 导出三个绘图函数；`_procrustes.py` 导出 `procrustes`；`_geometric_slerp.py` 导出 `geometric_slerp`
- 弃用模块名：`ckdtree`、`kdtree`、`qhull`（来自第 120 行的 `from . import ...`，因为它们不以下划线开头，会被 `dir()` 收进 `__all__`）

按字母序排列后，主体 20 个名字 + 末尾 2 个子模块，**预期 `scipy.spatial.__all__` 共 22 项**：

```
['ConvexHull', 'Delaunay', 'HalfspaceIntersection', 'KDTree', 'QhullError',
 'Rectangle', 'SphericalVoronoi', 'Voronoi',
 'cKDTree', 'ckdtree', 'convex_hull_plot_2d', 'delaunay_plot_2d',
 'distance_matrix', 'geometric_slerp', 'kdtree', 'minkowski_distance',
 'minkowski_distance_p', 'procrustes', 'qhull', 'tsearch',
 'distance', 'transform']
```

> 一个容易忽略的点：弃用模块名 `ckdtree`/`kdtree`/`qhull` 也会出现在 `__all__` 里，因为它们是「不以下划线开头」的名字。这是历史遗留，v2.0 移除这三个模块后会随之消失。

入口尾部还挂了一个测试钩子：

```python
from scipy._lib._testutils import PytestTester
test = PytestTester(__name__)
del PytestTester
```

参见 [__init__.py:128-130](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/__init__.py#L128-L130)。它让你可以用 `scipy.spatial.test()` 跑这个子包的测试套件（`test` 不在 `__all__` 里，因为它是在 `__all__` 之后才定义的）。

#### 4.3.4 代码实践（本讲主实践）

1. **实践目标**：导入 `scipy.spatial`，打印 `dir()` 与 `__all__`，对照 `__init__.py` 的导入把对象分成「最近邻 / 距离 / 几何 / 变换」四类并写成清单。
2. **操作步骤**：运行下面脚本；然后翻开 [`__init__.py:111-126`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/__init__.py#L111-L126)，逐个对照每个名字来自哪一行导入。
3. **需要观察的现象**：`len(__all__)` 应为 22；`distance` 与 `transform` 出现在列表**末尾**；其余名字按字母序排列。
4. **预期结果**：分类清单见下方表格。
5. 如果无法确定运行结果，明确写「待本地验证」。

```python
# 示例代码
import scipy.spatial as s

print("公共 API 数量:", len(s.__all__))
print("末尾两项:", s.__all__[-2:])          # 预期 ['distance', 'transform']
print("含弃用模块:", 'kdtree' in s.__all__)  # 预期 True
```

把 `__all__` 里的名字按下表归类（参考答案）：

| 类别 | 对象 |
| --- | --- |
| 最近邻 | `KDTree`、`cKDTree`、`Rectangle` |
| 距离度量 | `distance`（子模块）、`distance_matrix`、`minkowski_distance`、`minkowski_distance_p` |
| Qhull 几何剖分 | `Delaunay`、`ConvexHull`、`Voronoi`、`HalfspaceIntersection`、`SphericalVoronoi`、`QhullError`、`tsearch`、`delaunay_plot_2d`、`convex_hull_plot_2d`、`voronoi_plot_2d` |
| 空间变换 | `transform`（子模块）、`geometric_slerp`、`procrustes` |
| 弃用命名空间 | `ckdtree`、`kdtree`、`qhull`（v2.0 移除） |

> 说明：`minkowski_distance` / `distance_matrix` 在源码里归属 `_kdtree.py`，但语义上属于「距离度量」，因此归入距离类。`tsearch` 是 Delaunay 单纯形定位助手，归入几何类。

#### 4.3.5 小练习与答案

**练习 1**：如果把 [__init__.py:122](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/__init__.py#L122) 这行挪到第 124 行（`from . import distance, transform`）**之后**，`__all__` 会怎样变化？

**答案**：那么 `distance` 和 `transform` 就会被 `dir()` 直接捕获，进入字母序主体（`distance` 会排在 `distance_matrix` 附近、`transform` 排在 `tsearch` 附近），就不再需要第 126 行的 `+= ['distance', 'transform']` 追加了。当前写法刻意把推导放在子模块导入之前，所以必须手动追加——这是一个值得注意的顺序细节。

**练习 2**：`scipy.spatial.test` 为什么不在 `__all__` 里？

**答案**：因为 `test` 是在第 128-129 行定义的，而 `__all__` 在第 122 行就已经算完了，时间上晚于 `__all__` 的生成，所以不会被收进去。

---

## 5. 综合实践

把本讲三节串起来，完成一张「scipy.spatial 公共 API 全景表」：

1. 运行 `python -c "import scipy.spatial as s; print(sorted(s.__all__))"` 拿到完整的公共 API 列表。
2. 对**每一个**名字，确定它来自 `__init__.py` 的哪一行导入（参考 [__init__.py:111-126](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/__init__.py#L111-L126)）。
3. 把它归入四大能力域之一（最近邻 / 距离 / 几何 / 变换），标注它是「直接挂载对象」还是「子模块」。
4. 对每个直接挂载的**类**（如 `Delaunay`、`KDTree`），写一行最简调用示例（构造 + 取一个属性即可），不会用的先空着，作为后续讲义的「待学清单」。

完成后你会得到一张表，这张表就是后续所有讲义的导航索引——后面每一篇讲义正好对应表里的一类或一个对象。

> 提示：如果在归类时遇到「既像距离又像最近邻」的对象（如 `distance_matrix`），以**语义用途**为准并写下你的理由。本练习没有唯一标准答案，目的是让你把 API 和源码一一对应起来。

## 6. 本讲小结

- `scipy.spatial` 是 SciPy 中负责**空间几何与空间数据结构**的子包，底层借助 Qhull 等 C/C++ 库实现高性能。
- 它的能力可归纳为**四大域**：最近邻查询（KDTree/cKDTree）、距离度量（`distance` 子模块）、Qhull 几何剖分（Delaunay/ConvexHull/Voronoi/...）、空间变换（`transform` 子模块 + slerp/procrustes）。
- [`__init__.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/__init__.py) 用 `from ._xxx import *` 把内部实现「直接挂载」到顶层，用 `from . import distance, transform` 把两个大子模块保留为命名空间。
- `__all__` 是**动态推导**出来的：`[s for s in dir() if not s.startswith('_')]`，主体按字母序排列，末尾再追加 `distance` 和 `transform`；当前预期共 22 项。
- 弃用模块名 `ckdtree`/`kdtree`/`qhull` 仍残留在 `__all__` 中，访问会触发 `DeprecationWarning`，将在 v2.0 移除。
- `scipy.spatial.test()` 提供了运行子包测试套件的入口。

## 7. 下一步学习建议

- 想彻底搞懂「公共对象从哪来、弃用模块如何转发」，请继续学 **u1-l2 目录结构与公共 API 导出**，它会逐文件讲解 `_kdtree.py` / `_qhull.pyx` / `distance/` / `transform/` 的职责。
- 想了解这些 Cython/C++ 扩展是怎么编译出来的，请学 **u1-l3 构建系统与运行测试**（`meson.build`）。
- 想直接上手用最近邻，可以跳到 **u2-l1 KDTree 与 cKDTree 使用入门**。
- 建议的阅读顺序：u1-l2 → u1-l3 → u2-l1（先用 KDTree）→ u3-l1（用 Delaunay），把「入门层」走完再进入进阶层。
