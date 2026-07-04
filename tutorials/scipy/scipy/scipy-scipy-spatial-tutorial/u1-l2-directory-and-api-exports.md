# 目录结构与公共 API 导出

## 1. 本讲目标

学完本讲，你应该能够：

- 读懂 `scipy/spatial/` 这个目录里「每个文件分别干什么」，并能按职责把它们分类。
- 区分**私有实现模块**（以下划线 `_` 开头，真正的实现）与**公共入口**（`__init__.py` 暴露给用户的命名空间）。
- 理解 `from ._xxx import *` 这种写法如何把私有模块的符号「搬」到顶层公共命名空间。
- 看懂 `kdtree.py` / `qhull.py` / `ckdtree.py` 这三个「影子模块」为什么存在，以及 `__getattr__` 如何把它们**转发**到私有模块、同时发出 `DeprecationWarning`。

本讲只讲**结构与导出机制**，不涉及任何算法。它是上一讲「项目概览」的落地版：上一讲告诉你「四大能力域是什么」，本讲告诉你「这些能力在磁盘上的哪个文件里、怎么被组装成 `scipy.spatial` 这个名字」。

## 2. 前置知识

### 2.1 什么是「模块」和「包」

- 一个 `.py` 文件就是一个**模块**（module），文件名去掉 `.py` 就是模块名。例如 `kdtree.py` 对应模块 `kdtree`。
- 一个含有 `__init__.py` 的目录就是一个**包**（package）。`scipy/spatial/__init__.py` 让 `spatial/` 成为 `scipy` 下的一个子包，模块全名是 `scipy.spatial`。

### 2.2 `import *` 与 `__all__`

当你写 `from some_module import *` 时，Python 会把 `some_module` 里「不以下划线开头」的名字全部搬过来。但如果 `some_module` 定义了 `__all__` 列表，那么 `import *` 就**只**搬 `__all__` 里列出的名字。换句话说，`__all__` 是一个模块主动声明的「我的公共出口清单」。

### 2.3 模块级的 `__getattr__`（PEP 562）

普通模块只有真正定义在文件里的名字。但 Python 3.7 起（PEP 562）允许在模块里写一个函数 `__getattr__(name)`：当你访问一个**该模块里并不存在**的名字时，Python 会转而调用这个函数，由它决定返回什么。这正是 `scipy.spatial` 实现「弃用转发」的关键武器。

> 小贴士：`__getattr__` 只在「名字找不到」时才触发。如果名字已经存在（比如模块本身已经被 `import` 进来），访问它**不会**调用 `__getattr__`。这个细节后面会反复用到。

## 3. 本讲源码地图

本讲涉及的关键文件如下（永久链接基准为当前 HEAD `ce1f6477`）：

| 文件 | 角色 | 一句话职责 |
|---|---|---|
| [`__init__.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/__init__.py) | 公共入口 | 把各私有模块的符号组装成 `scipy.spatial` 命名空间，并定义 `__all__` |
| [`kdtree.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/kdtree.py) | 弃用影子模块 | 把 `KDTree`/`Rectangle` 等转发到私有 `_kdtree`，并发出弃用警告 |
| [`qhull.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/qhull.py) | 弃用影子模块 | 把 `Delaunay`/`ConvexHull` 等转发到私有 `_qhull` |
| [`ckdtree.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/ckdtree.py) | 弃用影子模块 | 把 `cKDTree` 转发到私有 `_ckdtree` |
| [`scipy/_lib/deprecation.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/_lib/deprecation.py) | 机制实现 | 提供 `_sub_module_deprecation`，是影子模块转发的真正引擎 |

> 说明：`deprecation.py` 不在 `spatial/` 目录下，而在 `scipy/_lib/`，它是全 SciPy 共用的工具。本讲会引用它，因为它正是转发机制的实现所在。

## 4. 核心概念与源码讲解

### 4.1 目录布局与文件职责映射

#### 4.1.1 概念说明

一个中等规模的项目，源码不可能全堆在一个文件里。`scipy.spatial` 把实现拆成了很多文件，但**用户并不需要知道这些文件名**——用户只认 `scipy.spatial` 这一个入口。于是这里就有两层关系：

- **实现层**：一堆负责真正干活的文件（建树、算距离、做三角剖分……）。
- **接口层**：`__init__.py`，它把实现层挑选出来的符号重新挂到 `scipy.spatial` 名下。

理解目录布局，本质上是建立「文件名 → 它实现哪一块能力」的映射。

#### 4.1.2 核心流程

读 `__init__.py` 顶部的 import 块，就能反推出整个目录的依赖骨架。流程是：

1. Python 执行 `import scipy.spatial`，于是运行 `spatial/__init__.py`。
2. 该文件依次 `from ._xxx import *`，把每个私有实现模块的公共符号搬进来。
3. 用户随后用 `scipy.spatial.KDTree`、`scipy.spatial.Delaunay` 等访问这些符号。

因此，`__init__.py` 里**每一行 `import`** 就对应目录里**一个实现文件**。

#### 4.1.3 源码精读

先看入口文件的 import 区：

[\_\_init\_\_.py:L111-L117](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/__init__.py#L111-L117) —— 这 7 行就是「实现层 → 接口层」的搬运指令。前缀 `.` 表示「当前包（spatial）内部」：

- `from ._kdtree import *` → 纯 Python 的 kd 树、矩形、距离辅助。
- `from ._ckdtree import *` → Cython 实现的高速 kd 树。
- `from ._qhull import *` → 基于 Qhull 的 Delaunay/凸包/Voronoi。
- `from ._spherical_voronoi import SphericalVoronoi` → 球面 Voronoi。
- `from ._plotutils import *` → 三个 2D 绘图助手。
- `from ._procrustes import procrustes` → 形状对齐。
- `from ._geometric_slerp import geometric_slerp` → 球面线性插值。

把上面这 7 行和目录里的文件一一对应，就得到下面这张**文件职责映射表**。表里把文件按类别分组（下划线前缀 = 私有实现，无前缀的 `.py` = 弃用影子模块）：

| 类别 | 文件 | 语言 | 职责 |
|---|---|---|---|
| **最近邻** | `_kdtree.py` | Python | `KDTree`（继承自 `cKDTree`）、`Rectangle`、`minkowski_distance(_p)`、`distance_matrix` |
| | `_ckdtree.pyx` | Cython | 高性能 `cKDTree`（C++ 内核的 Python 绑定） |
| | `setlist.pxd` | Cython 声明 | `_ckdtree` 用到的有序集合容器声明 |
| | `ckdtree/`（子目录） | C++ | `cKDTree` 的 C++ 内核源码（`ckdtree/src/`） |
| **几何剖分** | `_qhull.pyx` | Cython | `Delaunay`/`ConvexHull`/`Voronoi`/`HalfspaceIntersection`、`QhullError`、`tsearch` |
| | `_qhull.pxd` | Cython 声明 | `_qhull.pyx` 对外 C 接口声明 |
| | `_qhull.pyi` | 类型存根 | 静态类型提示 |
| | `qhull_misc.c` / `.h` | C | 对接 Qhull C 库的杂项胶水代码 |
| | `_spherical_voronoi.py` | Python | 球面 Voronoi |
| | `_voronoi.pyx` / `.pyi` | Cython | `sort_vertices_of_regions` |
| | `_plotutils.py` | Python | `delaunay_plot_2d` 等 2D 绘图 |
| **距离度量** | `distance.py` / `.pyi` | Python | 距离函数族 + `pdist`/`cdist`/`squareform` |
| | `_hausdorff.pyx` | Cython | `directed_hausdorff` |
| | `src/`（子目录） | C++/pybind | 距离度量的 C++/pybind11 后端 |
| **杂项算法** | `_procrustes.py` | Python | `procrustes` 形状对齐 |
| | `_geometric_slerp.py` | Python | 球面线性插值 |
| **空间变换** | `transform/`（子目录） | 混合 | `Rotation`/`RigidTransform` 子模块 |
| **测试** | `tests/`（子目录） | Python | 测试套件与数据基准 |
| **构建** | `meson.build` | Meson | 编译 Cython/C/C++ 扩展的规则 |

> 读这张表的方法：先按「能力域」（最近邻 / 几何剖分 / 距离 / 变换）看行，再注意每行的**语言**列——你会看到一个清晰的模式：**Python 写易读的逻辑，Cython/C++ 写性能关键路径**。

#### 4.1.4 代码实践

**实践目标**：用 Python 自省，亲手验证 `__init__.py` 的 7 行 import 到底搬来了哪些符号，并把它们归到对应文件。

**操作步骤**：

```python
import scipy.spatial as sp

# 1. 看看顶层命名空间里都有哪些「非下划线」名字
public = [s for s in dir(sp) if not s.startswith('_')]
print("公共名字数:", len(public))
print(public)
```

**需要观察的现象**：输出里应该能看到 `KDTree`、`cKDTree`、`Delaunay`、`ConvexHull`、`Voronoi`、`HalfspaceIntersection`、`SphericalVoronoi`、`procrustes`、`geometric_slerp`、`delaunay_plot_2d` 等，以及三个**模块名字** `kdtree`、`qhull`、`ckdtree`（它们是模块对象，不是类）。

**预期结果**：每个公共名字都能在 4.1.3 的职责表里找到它「来自哪个文件」。例如 `SphericalVoronoi` 来自 `_spherical_voronoi.py`，`tsearch` 来自 `_qhull.pyx`。

**待本地验证**：具体名字总数会随版本微调，请以你本机的 `len(public)` 为准，不要硬记一个固定数字。

#### 4.1.5 小练习与答案

**练习 1**：`dir(sp)` 里为什么会出现 `kdtree`、`qhull`、`ckdtree` 这三个**模块**名字？它们是哪里来的？

> **答案**：来自 `__init__.py` 第 120 行 `from . import ckdtree, kdtree, qhull`。这行把三个影子模块对象绑定到了 `spatial` 命名空间，所以它们出现在 `dir()` 里。

**练习 2**：`_qhull.pxd` 和 `_qhull.pyi` 都不是 `.py` 文件，它们分别是什么用途？

> **答案**：`.pxd` 是 Cython 的声明文件（给其他 `.pyx` 提供 C 级接口）；`.pyi` 是类型存根（给静态类型检查器/IDE 用）。两者都不参与「运行时搬运符号」，只是辅助。

---

### 4.2 私有实现模块与公共命名空间

#### 4.2.1 概念说明

注意职责表里几乎每个实现文件都以**下划线开头**：`_kdtree.py`、`_ckdtree.pyx`、`_qhull.pyx`……这是 Python 社区的约定：**以下划线开头的名字是「私有的」**，意味着作者保留随时改动甚至删除它们的权利，外部代码不应直接 `import` 它们。

那用户该用谁？答案是公共命名空间 `scipy.spatial`。`scipy.spatial` 把私有模块里「愿意公开」的符号，通过 `import *`（受 `__all__` 控制）重新暴露出来。于是形成了：

- **私有模块**（`_*`）：实现细节，可变。
- **公共命名空间**（`scipy.spatial`）：稳定接口，面向用户。

这种「私有实现 + 公共出口」的分层，让 SciPy 能在不动用户代码的前提下，重构内部实现。

#### 4.2.2 核心流程

公共命名空间的组装流程：

1. 每个私有模块在自己的源码里写一个 `__all__`，声明「我愿意被 `import *` 搬走哪些名字」。
2. `__init__.py` 用 `from ._xxx import *`，按 `__all__` 把这些名字搬进 `scipy.spatial`。
3. `__init__.py` 最后再统一推导出顶层的 `__all__`，作为整个子包的对外清单。

所以「哪些名字能从 `scipy.spatial` 拿到」这件事，是**两层 `__all__` 共同决定**的：先由私有模块的 `__all__` 放行，再由顶层 `__all__` 汇总。

#### 4.2.3 源码精读

先看顶层如何汇总。[\_\_init\_\_.py:L122](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/__init__.py#L122) 用一行推导式动态生成 `__all__`：

```python
__all__ = [s for s in dir() if not s.startswith('_')]
```

它的含义是：「把此刻当前命名空间里所有**不以 `_` 开头**的名字，都收进 `__all__`」。因为这一行之前已经执行了 7 行 `import`，所以 `dir()` 里既有各私有模块放行的公共符号，也有三个影子模块名。

紧接着，[\_\_init\_\_.py:L124-L126](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/__init__.py#L124-L126) 把两个**子模块命名空间**追加进来：

```python
from . import distance, transform
__all__ += ['distance', 'transform']
```

注意 `distance` 和 `transform` 是作为**子模块**（而不是符号）加入的——它们各自又是一个包，里面有自己的一整套 API。这也是上一讲强调的「直接挂载」与「子模块命名空间」两种挂载方式的代码体现：类/函数用 `import *` 直接搬到顶层，而 `distance`/`transform` 太大，保留为二级命名空间。

再看私有模块这一侧。`_kdtree.py` 的出口清单是：

[\_kdtree.py:L11-L13](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_kdtree.py#L11-L13) —— 声明 `minkowski_distance_p`、`minkowski_distance`、`distance_matrix`、`Rectangle`、`KDTree` 这 5 个名字可被 `import *` 搬走。

[\_qhull.pyx:L37](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_qhull.pyx#L37) —— 声明 `Delaunay`、`ConvexHull`、`QhullError`、`Voronoi`、`HalfspaceIntersection`、`tsearch` 这 6 个名字。

[\_ckdtree.pyx:L33](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/_ckdtree.pyx#L33) —— 只放行 `cKDTree` 一个名字。

把这三张小清单和顶层 `__all__` 对照，就能验证「私有放行 → 公共汇总」这条链是闭合的。

#### 4.2.4 代码实践

**实践目标**：验证顶层公共名字确实「来自」私有模块的 `__all__`，体会两层 `__all__` 的关系。

**操作步骤**：

```python
import scipy.spatial as sp
from scipy.spatial import _kdtree, _qhull, _ckdtree

# 私有模块各自放行的名字
print("_kdtree.__all__ :", _kdtree.__all__)
print("_qhull.__all__  :", _qhull.__all__)
print("_ckdtree.__all__:", _ckdtree.__all__)

# 顶层能拿到的对应名字，应当就是上面这些
for name in _kdtree.__all__ + _qhull.__all__ + _ckdtree.__all__:
    obj_priv = getattr(_kdtree, name, None) or getattr(_qhull, name, None) or getattr(_ckdtree, name, None)
    obj_pub  = getattr(sp, name)
    print(f"{name:25s} 公私同一对象: {obj_pub is obj_priv}")
```

**需要观察的现象**：每一行都应打印 `公私同一对象: True`，说明 `sp.KDTree` 和 `_kdtree.KDTree` 是**同一个对象**——顶层并没有复制，只是多挂了一个名字。

**预期结果**：`sp.KDTree is _kdtree.KDTree` 为 `True`，`sp.cKDTree is _ckdtree.cKDTree` 为 `True`，以此类推。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `__init__.py` 的 `__all__` 要用 `[s for s in dir() if not s.startswith('_')]` 动态生成，而不是手写一个列表？

> **答案**：动态生成可以**避免遗漏和不同步**——只要某个私有模块新增了一个公共符号并写进自己的 `__all__`，顶层 `import *` 就会自动把它带进来，`dir()` 自然包含它，无需再手动维护顶层清单。代价是连 `kdtree`/`qhull`/`ckdtree` 这三个弃用模块名也被一并收进 `__all__`。

**练习 2**：`distance` 和 `transform` 为什么不像 `KDTree` 那样直接搬到顶层，而是作为子模块保留？

> **答案**：它们各自体量很大、内部还有完整一层 API（如 `distance.cdist`、`transform.Rotation`）。直接 `import *` 会把大量名字灌进顶层，既污染命名空间也难维护，所以保留为二级命名空间，由用户写 `scipy.spatial.distance.xxx` 访问。

---

### 4.3 弃用命名空间与 `__getattr__` 转发

#### 4.3.1 概念说明

历史上，很多老代码是这样写的：

```python
from scipy.spatial.kdtree import KDTree      # 老写法
from scipy.spatial.qhull import Delaunay     # 老写法
from scipy.spatial.ckdtree import cKDTree    # 老写法
```

也就是说，`kdtree`、`qhull`、`ckdtree` 曾经是**公共子模块**。但如 4.2 所述，SciPy 的设计意图是让用户统一走 `scipy.spatial` 顶层，而不是直接戳私有实现。为了**平滑迁移**而不是一刀切地报错，SciPy 保留了 `kdtree.py` / `qhull.py` / `ckdtree.py` 这三个**影子模块**：它们看起来还能用，但每次访问都会**警告**「请改用 `scipy.spatial` 命名空间」，并计划在 SciPy 2.0.0 彻底移除。

这三个影子模块的实现极简——它们自己**根本不定义任何符号**，全靠 `__getattr__` 把访问**转发**给真正的私有模块。

#### 4.3.2 核心流程

以 `scipy.spatial.kdtree.KDTree` 为例，一次属性访问的完整链路：

1. `import scipy.spatial` 时，`__init__.py` 第 120 行 `from . import ckdtree, kdtree, qhull` 把影子模块对象绑定进 `spatial` 命名空间。**这一步不报警告**。
2. 用户写 `sp.kdtree.KDTree`。`sp.kdtree` 拿到影子模块对象（已存在，不触发 `__getattr__`）。
3. 接着访问 `.KDTree`：影子模块里并没有 `KDTree` 这个名字，于是 Python 调用影子模块的 `__getattr__("KDTree")`。
4. `__getattr__` 调用 `_sub_module_deprecation(...)`，它做三件事：
   - 检查 `"KDTree"` 是否在允许清单 `__all__` 里；不在就直接抛 `AttributeError`。
   - 发出一条 `DeprecationWarning`，提示改用 `scipy.spatial` 命名空间。
   - 从私有模块 `scipy.spatial._kdtree` 取出真正的 `KDTree` 并**返回**。
5. 用户既拿到了「真身」，也看到了警告。下次再访问会**重复**触发整个过程（因为返回值不会被缓存到影子模块里）。

伪代码概括这条转发链：

```
sp.kdtree.KDTree
   └─ kdtree 模块没有 KDTree
        └─ kdtree.__getattr__("KDTree")
             └─ _sub_module_deprecation(...)
                  ├─ "KDTree" in __all__ ?  否 → AttributeError
                  ├─ warnings.warn(DeprecationWarning)
                  └─ return getattr(_kdtree, "KDTree")
```

#### 4.3.3 源码精读

先看 `__init__.py` 是怎么把三个影子模块挂上来的：

[\_\_init\_\_.py:L119-L120](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/__init__.py#L119-L120) —— 注释明确写着「Deprecated namespaces, to be removed in v2.0.0」，下一行 `from . import ckdtree, kdtree, qhull` 把它们导入。注意：仅仅是**导入模块本身**，不触发任何警告——这正是为什么 `sp.kdtree`（取模块）安静无声，而 `sp.kdtree.KDTree`（取属性）才报警。

再看影子模块 `kdtree.py` 的全部「干货」：

[kdtree.py:L1-L5](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/kdtree.py#L1-L5) —— 顶部注释直接告诉读者：「本文件不供公开使用，将在 SciPy v2.0.0 移除」，并只 `import` 了那个共用的弃用工具。

[kdtree.py:L8-L15](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/kdtree.py#L8-L15) —— 声明 `__all__`。注意这里的名字（`KDTree`、`Rectangle`、`cKDTree`、`distance_matrix`、`minkowski_distance(_p)`）**在本文件里根本没有定义**，所以行尾标了 `# noqa: F822`（让 flake8 别报「未定义名字」）。`__all__` 在这里起的是「允许转发的白名单」作用。

[kdtree.py:L18-L25](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/kdtree.py#L18-L25) —— `__dir__()` 让 `dir(scipy.spatial.kdtree)` 和 tab 补全能列出这些名字（否则模块里其实啥都没有，补全会是空的）；`__getattr__` 则把每一次属性访问交给 `_sub_module_deprecation`。注意参数 `private_modules=["_kdtree"]` ——它指明了真正的实现藏在哪个私有模块。

`qhull.py` 和 `ckdtree.py` 结构完全一样，只是白名单和目标私有模块不同：

- [qhull.py:L8-L15](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/qhull.py#L8-L15) 与 [qhull.py:L22-L25](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/qhull.py#L22-L25) ——白名单是 `Delaunay`/`ConvexHull`/`HalfspaceIntersection`/`QhullError`/`Voronoi`/`tsearch`，转发到 `_qhull`。
- [ckdtree.py:L8](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/ckdtree.py#L8) 与 [ckdtree.py:L15-L18](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/ckdtree.py#L15-L18) ——白名单只有 `cKDTree`，转发到 `_ckdtree`。

最后看真正的引擎 `_sub_module_deprecation`：

[\_lib/deprecation.py:L15-L16](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/_lib/deprecation.py#L15-L16) ——函数签名。关键字参数 `sub_package`、`module`、`private_modules`、`all`、`attribute` 与影子模块调用时传入的一一对应；`correct_module` 默认 `None` 表示「正确入口就是 `scipy.spatial` 顶层」。

[\_lib/deprecation.py:L39-L49](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/_lib/deprecation.py#L39-L49) ——先算出 `correct_import`（这里是 `scipy.spatial`），再判断：若被访问的 `attribute` 不在白名单 `all` 里，直接抛 `AttributeError`，防止影子模块被当成「什么都能转」的黑洞。

[\_lib/deprecation.py:L51-L68](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/_lib/deprecation.py#L51-L68) ——构造警告文案：若该名字在正确入口 `scipy.spatial` 里能找到（通常都能，因为顶层已经 `import *`），就提示「请从 `scipy.spatial` 命名空间导入」；然后用 `warnings.warn(..., category=DeprecationWarning, stacklevel=3)` 发出警告。`stacklevel=3` 很关键：它让警告指向**用户写代码的那一行**，而不是 `__getattr__` 内部。

[\_lib/deprecation.py:L70-L78](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/_lib/deprecation.py#L70-L78) ——最后遍历 `private_modules`，从 `scipy.spatial._kdtree` 取出真正的符号并 `return`。若所有私有模块都没有该名字，才把异常抛出去。

把 4.3.2 的流程图和这段源码对照，每一步都能在源码里找到对应行。

#### 4.3.4 代码实践

**实践目标**：亲手触发并捕获三个影子模块的 `DeprecationWarning`，并验证转发拿到的就是私有模块里的「真身」。这正是本讲规格里要求的实践任务。

**操作步骤**：

```python
import warnings
import scipy.spatial as sp

# 1) 分别访问三个弃用命名空间里的对象，记录所有警告
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")          # 确保每条警告都被记录
    KD = sp.kdtree.KDTree       # 触发 kdtree.__getattr__("KDTree")
    DL = sp.qhull.Delaunay      # 触发 qhull.__getattr__("Delaunay")
    CK = sp.ckdtree.cKDTree     # 触发 ckdtree.__getattr__("cKDTree")

print("=== 捕获到的警告 ===")
for w in caught:
    print(f"[{w.category.__name__}] {w.message}")

# 2) 验证：转发拿到的对象 == 顶层公共对象 == 私有模块里的对象
from scipy.spatial import _kdtree, _qhull, _ckdtree
print("\n=== 是否同一个对象 ===")
print("sp.kdtree.KDTree   is sp.KDTree          :", KD is sp.KDTree)
print("sp.qhull.Delaunay  is sp.Delaunay        :", DL is sp.Delaunay)
print("sp.ckdtree.cKDTree is sp.cKDTree         :", CK is sp.cKDTree)
print("KDTree 真正定义在模块                      :", KD.__module__)

# 3) 对照观察：只取「模块」本身，不取属性 → 不触发 __getattr__ → 无警告
with warnings.catch_warnings(record=True) as quiet:
    warnings.simplefilter("always")
    _ = sp.kdtree          # 仅取模块对象
print("\n仅访问 sp.kdtree（模块）捕获到的警告数:", len(quiet))
```

**需要观察的现象**：

1. 第 1 段应打印 **3 条** `DeprecationWarning`，每条都写着类似「Please import `KDTree` from the `scipy.spatial` namespace; the `scipy.spatial.kdtree` namespace is deprecated and will be removed in SciPy 2.0.0.」
2. 第 2 段三个 `is` 比较应全部为 `True`，`KD.__module__` 应为 `scipy.spatial._kdtree`——证明转发确实落到了私有模块。
3. 第 3 段「仅访问模块」捕获的警告数应为 **0**——印证 4.3.3 的结论：`__getattr__` 只在取属性时触发，取模块本身不会。

**预期结果**：三段现象如上。若你把 `with warnings.catch_warnings(...)` 去掉，再直接运行 `sp.kdtree.KDTree`，会在 stderr 看到同样的 `DeprecationWarning`（默认每次只显示一次同一位置的警告，这就是为什么用 `catch_warnings(record=True)` 配 `simplefilter("always")` 来可靠捕获）。

**待本地验证**：警告文案的精确措辞以你本机 SciPy 版本为准；只要包含 `deprecated` 和 `2.0.0` 即符合预期。

#### 4.3.5 小练习与答案

**练习 1**：如果有人写 `scipy.spatial.kdtree.NotARealName`，会发生什么？为什么？

> **答案**：会抛 `AttributeError`。因为 `"NotARealName"` 不在 `kdtree.py` 的 `__all__` 白名单里，`_sub_module_deprecation` 在 [deprecation.py:L44-L49](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/_lib/deprecation.py#L44-L49) 直接 `raise AttributeError`。白名单机制保证了影子模块不会无差别地转发任意名字。

**练习 2**：为什么 `warnings.warn` 要用 `stacklevel=3` 而不是默认的 `stacklevel=1`？

> **答案**：调用栈是「用户代码 → `kdtree.__getattr__` → `_sub_module_deprecation` → `warnings.warn`」。`stacklevel=1` 会把警告指向 `warn` 这一行（没用），`stacklevel=2` 指向 `__getattr__`（也没用），`stacklevel=3` 才指向**用户写 `sp.kdtree.KDTree` 的那一行**，让用户能立刻定位需要修改的代码。

**练习 3**：把 `from . import ckdtree, kdtree, qhull`（`__init__.py` 第 120 行）删掉，`sp.kdtree.KDTree` 还能工作吗？

> **答案**：不能。删掉后 `spatial` 命名空间里就没有 `kdtree` 这个名字，`sp.kdtree` 会先报 `AttributeError`，根本到不了影子模块的 `__getattr__`。这一行 import 正是让影子模块「可见」的钩子。

## 5. 综合实践

**任务**：为 `scipy.spatial` 画一张「名字→源头」的导出关系图，并用代码自检它的正确性。

**步骤**：

1. 列出 `scipy.spatial.__all__` 里的全部名字。
2. 对每个名字，判定它属于哪一类：
   - **A 直接挂载的类/函数**：来自某个 `_*` 私有模块（用 `getattr(obj, "__module__", "")` 反查定义模块）。
   - **B 子模块命名空间**：`distance`、`transform`。
   - **C 弃用影子模块**：`kdtree`、`qhull`、`ckdtree`。
3. 对 A 类名字，进一步标注它来自 `_kdtree` / `_ckdtree` / `_qhull` / `_spherical_voronoi` / `_plotutils` / `_procrustes` / `_geometric_slerp` 中的哪一个。
4. 写一段自检代码：对每个 A 类名字 `n`，断言 `getattr(sp, n) is getattr(对应私有模块, n)`；对 C 类名字，断言访问其任一白名单符号会触发 `DeprecationWarning`。

**参考骨架**（示例代码，不是项目原有代码）：

```python
import warnings, scipy.spatial as sp
from scipy.spatial import _kdtree, _ckdtree, _qhull, _spherical_voronoi, _plotutils

priv_map = {**{n: _kdtree for n in _kdtree.__all__},
            **{n: _ckdtree for n in _ckdtree.__all__},
            **{n: _qhull for n in _qhull.__all__}}
SUBMODULES = {"distance", "transform"}
SHIMS = {"kdtree", "qhull", "ckdtree"}

for name in sp.__all__:
    if name in SUBMODULES or name in SHIMS:
        print(f"{name:22s} -> 子模块/影子模块")
        continue
    obj = getattr(sp, name)
    mod = obj.__module__ if hasattr(obj, "__module__") else "?"
    print(f"{name:22s} -> 定义于 {mod}")
    if name in priv_map:
        assert obj is getattr(priv_map[name], name), f"{name} 不一致!"
```

**预期结果**：自检断言全部通过，输出清单能让你一眼看出「每个公共名字背后是哪个私有文件」。完成这张图，你就把本讲的「文件职责映射」「私有 vs 公共」「弃用转发」三个模块串成了一条完整的认知链。

## 6. 本讲小结

- `spatial/` 目录由**公共入口** `__init__.py` 和一堆**私有实现模块**（`_*` 前缀）组成，外加 `distance`/`transform` 两个子模块、`tests`/`src`/`ckdtree` 等子目录。
- `__init__.py` 用 7 行 `from ._xxx import *` 把私有模块的公共符号搬到顶层；每个私有模块用 `__all__` 控制「愿意放行哪些名字」。
- 顶层 `__all__` 用 `[s for s in dir() if not s.startswith('_')]` **动态推导**，再把 `distance`/`transform` 两个子模块名追加进去。
- `kdtree.py`/`qhull.py`/`ckdtree.py` 是**弃用影子模块**：自身不定义任何符号，全靠模块级 `__getattr__` 把属性访问**转发**到对应私有模块（`_kdtree`/`_qhull`/`_ckdtree`）。
- 转发的真正引擎是共用的 `_sub_module_deprecation`：白名单外的名字直接抛 `AttributeError`，白名单内的名字发一条 `DeprecationWarning`（`stacklevel=3` 指向用户代码）后返回私有模块里的真身。
- 关键细节：**取模块本身**（`sp.kdtree`）不触发警告，只有**取模块属性**（`sp.kdtree.KDTree`）才会触发 `__getattr__`；这套机制将在 SciPy 2.0.0 移除。

## 7. 下一步学习建议

- 下一讲 **u1-l3 构建系统与运行测试** 会解释这些 `.pyx`/`.c`/`.cxx` 文件是如何被 `meson.build` 编译成可导入的扩展模块的——届时你会明白为什么 `from ._ckdtree import *` 在源码树里能成功。
- 想立刻进入算法？可以直接跳到 **u2 单元（KDTree 最近邻查询）**，结合本讲建立的「`_kdtree.py` 是 Python 层、`_ckdtree.pyx` 是 Cython 层」的认知去读 `KDTree`。
- 想理解几何剖分？进入 **u3 单元（Qhull 几何计算）**，本讲里频繁出现的 `_qhull.pyx` / `QhullError` 会在那里被逐层拆解。
- 建议随手运行一次本讲的 4.3.4 实践，亲眼看到 `DeprecationWarning` 再继续——它会让你对「公共 API 的稳定性边界」有直观体感。
