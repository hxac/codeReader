# scipy.interpolate 是什么：定位与全景

## 1. 本讲目标

本讲是整套 `scipy.interpolate`（SciPy 的插值子包）学习手册的第一篇，面向零基础读者。学完本讲你应该能够：

- 用自己的话说清楚「插值（interpolation）」和「拟合（fitting）」的区别；
- 列举 `scipy.interpolate` 解决的三大类问题：一维插值、规则网格多维插值、散点（非结构化数据）插值；
- 打开 `scipy/interpolate/__init__.py`，看懂它是如何用一连串 `from ._xxx import *` 把公共 API 拼装出来的；
- 用 `make_interp_spline` 对一组一维数据做三次样条插值并画出曲线。

本讲**不要求**你已经懂样条数学，我们只建立一个「全局地图」，后续讲义再逐块深入。

## 2. 前置知识

### 2.1 什么是插值

假设你在一些已知点 \( (x_i, y_i) \)（\( i=0,\dots,n-1 \)）上测量到了函数值，现在想知道「在这些点之间」某个新位置 \( x \) 上的函数值。

**插值（interpolation）**要找一条曲线 \( p(x) \)，让它**严格穿过**每一个已知数据点：

\[
p(x_i) = y_i,\quad i=0,\dots,n-1
\]

然后用 \( p(x) \) 去估计中间位置的值。最直观的就是「把点用直线连起来」（线性插值）。

### 2.2 插值与拟合的区别

插值要求曲线**精确穿过**所有数据点；而**拟合（fitting）**，例如最小二乘拟合，允许曲线**不穿过**数据点，只要总体误差最小：

\[
\min_{p}\ \sum_{i}\bigl(p(x_i)-y_i\bigr)^2
\]

一条经验法则：

| 场景 | 数据特点 | 通常选 |
|------|----------|--------|
| 数据本身精确、无噪声 | 想要「穿过点」 | 插值 |
| 数据含噪声 | 想要「平滑趋势」 | 拟合 / 平滑样条 |

`scipy.interpolate` 主要面向插值，但里面也包含「平滑样条」（smoothing spline）这类介于两者之间的工具——它会自动平衡「穿过点」和「光滑」。

### 2.3 三大类问题

这个子包解决的问题可以归成三大类，这也是本讲和整本手册的主线：

1. **一维插值（univariate）**：输入是一串 \( (x_i, y_i) \)，\( x \) 是一维坐标。例如随时间采样的传感器读数。
2. **规则网格多维插值**：数据排在一个规则的「网格」上（像二维表格、三维体素），坐标轴各自等距或单调。例如一张图像、一个三维温度场。
3. **散点插值（unstructured / scattered）**：数据点散落在任意位置，没有网格结构。例如分布在地图上的气象站观测。

记牢这三类，后面看 API 分类时就会发现它们一一对应。

## 3. 本讲源码地图

本讲只聚焦一个文件，它是整个子包的「门面」：

| 文件 | 作用 |
|------|------|
| [`scipy/interpolate/__init__.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/__init__.py) | 子包入口。文件顶部一段很长的 docstring 是官方 API 分类目录；之后用一连串 `from ._xxx import *` 把各子模块里的公共名字汇聚到 `scipy.interpolate` 这个命名空间。 |

另外会提到（但本讲不深读）：

| 文件 | 一句话作用 |
|------|-----------|
| `_bsplines.py` | B 样条数据结构 `BSpline` 和构造函数 `make_interp_spline` 等的真正实现，本讲实践任务用到它。 |

> 说明：本仓库里每个 `_*` 开头的文件（如 `_bsplines.py`、`_cubic.py`）才是「公共 Python 模块」，下划线只是约定「不要直接 import」。`__init__.py` 通过 `from ._xxx import *` 把它们重新暴露成公共 API。这套导入链是第 4.1 节的主题。

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

- **4.1 `__init__.py` 的模块分类**：这个子包是怎么从一个空命名空间，被一连串 `import *` 拼装出上百个公共名字的。
- **4.2 公共 API 总览**：那段长 docstring 是怎么按「三大类问题」把 API 组织起来的。

### 4.1 `__init__.py` 的模块分类

#### 4.1.1 概念说明

在 Python 里，一个「包（package）」对外的样子由它的 `__init__.py` 决定。当你写：

```python
from scipy.interpolate import make_interp_spline
```

Python 实际上执行了 `scipy/interpolate/__init__.py`，并把里面绑定到模块级命名空间的名字暴露出来。

`scipy.interpolate` 的做法是：**真正的实现分散在十几个 `_*` 子模块里**，而 `__init__.py` 只负责「收集」——用一行行 `from ._xxx import *` 把每个子模块公开的名字搬进当前命名空间。这样做的好处是：

- 实现按主题切分到小文件，便于维护；
- 用户只需要面对一个扁平的 `scipy.interpolate` 命名空间，不用记「这个函数在哪个文件里」。

#### 4.1.2 核心流程

`__init__.py` 的执行可以概括为三步：

1. **写文档**：顶部一大段 docstring 既是文档，也是给 `autosummary` 自动生成 API 参考的目录。
2. **搬名字**：依次 `from ._xxx import *`，把每个实现模块的公开符号汇入命名空间。
3. **整理 + 收尾**：用列表推导生成 `__all__`；挂上 `test` 测试入口；留一条向后兼容别名。

用伪代码表示：

```text
docstring（API 目录）
↓
for 模块 in [_interpolate, _fitpack_py, _fitpack2, _rbf, _rbfinterp,
             _polyint, _cubic, _ndgriddata, _bsplines, ...]:
    from .模块 import *      # 把公开名字搬进当前命名空间
↓
__all__ = [当前命名空间里所有不以 _ 开头的名字]
test = PytestTester(__name__)
```

注意第 3 步生成 `__all__` 的方式很巧妙：它**不是手工维护的列表**，而是直接扫描「现在命名空间里有哪些公开名字」。这意味着每加一个 `import *`，新名字会自动进入 `__all__`，无需同步修改两处。

#### 4.1.3 源码精读

打开 `__init__.py`，跳过前面 191 行 docstring，就能看到核心的导入链。下面这段是关键：

这是导入链的主体——每个 `from ._xxx import *` 都把一个实现模块的公开名字汇入 `scipy.interpolate` 命名空间（注释里的中文是我们标注的对应主题）：

- [__init__.py:192-216](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/__init__.py#L192-L216) — 一连串 `from ._xxx import *`，例如第 207 行 `from ._bsplines import *` 正是把本讲实践任务要用到的 `make_interp_spline`、`BSpline` 等搬进来的那一行。

紧跟在导入链后面的收尾逻辑：

- [__init__.py:218-219](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/__init__.py#L218-L219) — 注释 `# Deprecated namespaces, to be removed in v2.0.0`，并 `from . import fitpack, fitpack2, interpolate, ...` 保留旧命名空间。这些是「垫片（shim）」模块，方便旧代码继续工作，但计划在 SciPy 2.0 移除。

- [__init__.py:221](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/__init__.py#L221) — `__all__ = [s for s in dir() if not s.startswith('_')]`。`dir()` 列出当前命名空间所有名字，过滤掉下划线开头的私有名字，剩下的就是公共 API。这是「自动收集」写法，免去手工维护。

- [__init__.py:223-225](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/__init__.py#L223-L225) — `test = PytestTester(__name__)`，给子包挂上一个测试入口，于是你可以写 `scipy.interpolate.test()` 来跑该子包的全部测试。

- [__init__.py:227-228](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/__init__.py#L227-L228) — `pchip = PchipInterpolator`，一条向后兼容别名（PCHIP 是一种保形插值器，第 u2 单元会讲）。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `import *` 是如何把上百个名字「倒」进 `scipy.interpolate` 命名空间的。

**操作步骤**：

```python
import scipy.interpolate as si

# 1. 数一数公共 API 有多少个
print("公共名字数量:", len(si.__all__))

# 2. 看看本讲要用到的两个名字是否在 __all__ 里
for name in ["make_interp_spline", "BSpline", "CubicSpline", "RegularGridInterpolator", "griddata"]:
    print(f"{name:28s} -> 在 __all__ 中: {name in si.__all__}")

# 3. 追溯 make_interp_spline 真正来自哪个文件
print("make_interp_spline 定义于:", si.make_interp_spline.__module__)
```

**需要观察的现象**：

- `len(si.__all__)` 是一个三位数（上百个公共名字）。
- 上述四个名字都应该报告「在 `__all__` 中: True」。
- `make_interp_spline.__module__` 应该是 `scipy.interpolate._bsplines`——印证了第 4.1.2 节「它实际在 `_bsplines.py`，由 `__init__.py` 第 207 行搬进来」。

**预期结果**：

```
公共名字数量: 100 多（具体数随版本）
make_interp_spline          -> 在 __all__ 中: True
BSpline                     -> 在 __all__ 中: True
...
make_interp_spline 定义于: scipy.interpolate._bsplines
```

> 待本地验证：确切的 `__all__` 长度取决于你本地的 SciPy 版本，不必死记数字，重点理解「自动收集」机制。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `__init__.py` 用 `[s for s in dir() if not s.startswith('_')]` 自动生成 `__all__`，而不是写死一个列表？

**参考答案**：因为实现模块多、公共名字也多，写死列表容易漏写或写错（加了一个 `import *` 却忘了同步更新 `__all__`）。用 `dir()` 扫描当前命名空间可以保证「只要被搬进来的名字就一定在 `__all__` 里」，是「单一事实来源」的写法，维护成本最低。

**练习 2**：`from . import fitpack, interpolate, ...`（第 219 行）导入的 `fitpack`、`interpolate` 等模块，和 `from ._fitpack_py import *` 导入的函数，有什么本质区别？

**参考答案**：前者导入的是**旧命名空间模块本身**（`fitpack.py`、`interpolate.py` 这些垫片文件），属于「将被移除」的旧入口；后者导入的是新实现模块 `_*` 里的**公开函数/类**。新代码应只用后者，前者只为向后兼容而保留。

---

### 4.2 公共 API 总览

#### 4.2.1 概念说明

`__init__.py` 顶部那段近 190 行的 docstring 不只是注释——它同时是 [SciPy 官方文档](https://docs.scipy.org/doc/scipy/reference/interpolate.html) 的 API 目录来源（Sphinx 的 `autosummary` 会读取它）。换句话说，**这段 docstring 就是 `scipy.interpolate` 的官方「功能地图」**。

它的组织方式恰好对应我们在第 2.3 节讲的「三大类问题」，外加几类专题。读懂这个目录，你就掌握了整个子包的版图。

#### 4.2.2 核心流程

docstring 把 API 分成几个大块，下面用一张表把每一块对应到「哪类问题」和「代表 API」：

| docstring 章节 | 对应问题类别 | 代表公共 API |
|----------------|--------------|--------------|
| Univariate interpolation（一维插值） | 一维 | `make_interp_spline`、`CubicSpline`、`PchipInterpolator`、`Akima1DInterpolator` |
| （一维）Low-level data structures | 一维·底层 | `PPoly`、`BPoly`、`BSpline` |
| Multivariate — Unstructured data | 散点 | `LinearNDInterpolator`、`NearestNDInterpolator`、`CloughTocher2DInterpolator`、`RBFInterpolator` |
| Multivariate — For data on a grid | 规则网格 | `RegularGridInterpolator` |
| （多维）Low-level data structures | 多维·底层 | `NdPPoly`、`NdBSpline` |
| 1-D spline smoothing and approximation | 一维·平滑/拟合 | `make_lsq_spline`、`make_smoothing_spline`、`make_splrep`、`generate_knots` |
| Rational Approximation | 专题·有理 | `AAA` |
| FITPACK routines（1D / 2D） | 兼容旧接口 | `splrep`/`splev`、`UnivariateSpline`、`bisplrep` 等 |
| Additional tools | 杂项/便捷函数 | `lagrange`、`pade`、`interpn`、`griddata`、`Rbf`、`interp1d` |

把这张表和第 2.3 节的三大类对照，你会发现：

- **一维插值**的主力是 `make_interp_spline` / `CubicSpline` 一族；
- **规则网格多维**的主力是 `RegularGridInterpolator` 和便捷函数 `interpn`；
- **散点插值**的主力是 `griddata`（便捷函数）及其背后的一族 `*NDInterpolator`，外加 `RBFInterpolator`。

记住这张「问题 → 主力 API」的对应表，就足以在 80% 的场景里快速找到该用哪个函数。

#### 4.2.3 源码精读

docstring 的几个关键分类起点（行号见链接）：

- [__init__.py:14-16](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/__init__.py#L14-L16) — `Univariate interpolation` 一节的标题，下面用 `autosummary` 列出 `make_interp_spline`、`CubicSpline` 等一维插值器。

- [__init__.py:39-49](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/__init__.py#L39-L49) — `Multivariate interpolation`，先是 `Unstructured data`（散点，列 `LinearNDInterpolator`/`NearestNDInterpolator`/`CloughTocher2DInterpolator`/`RBFInterpolator`），再是 `For data on a grid`（规则网格，列 `RegularGridInterpolator`）。这两小节正是第 2.3 节散点和规则网格两类的源头。

- [__init__.py:96-104](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/__init__.py#L96-L104) — `Interfaces to FITPACK routines`，说明这些是 FITPACK 库的封装，并提示「大多数情况下用户更适合用前面列出的高层例程」。这是官方对「优先用新 API、FITPACK 当底层」的明确表态。

- [__init__.py:167-185](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/__init__.py#L167-L185) — `Additional tools`，里面 `interpn`、`griddata` 是两个最常用的「一次性插值」便捷函数，而 `interp1d`、`interp2d`、`lagrange`、`pade` 等则是历史遗留或已弃用的接口。

#### 4.2.4 代码实践

**实践目标**：把 docstring 里的分类和「实际能从 `scipy.interpolate` 导入的名字」对上号，建立手感。

**操作步骤**：

```python
import scipy.interpolate as si

# 给定一组名字，判断它们各自属于哪一大类
names = {
    "make_interp_spline":  "一维",
    "CubicSpline":         "一维",
    "RegularGridInterpolator": "规则网格",
    "interpn":             "规则网格（便捷函数）",
    "NearestNDInterpolator":  "散点",
    "griddata":            "散点（便捷函数）",
    "RBFInterpolator":     "散点（RBF）",
}

for n, cat in names.items():
    ok = hasattr(si, n)         # 这个名字确实能从子包顶层导入
    print(f"{n:28s} 分类={cat:20s} 顶层可导入={ok}")
```

**需要观察的现象**：每个名字都应报告 `顶层可导入=True`，说明它们虽然实现散落在不同 `_*` 文件，但都已经被 `__init__.py` 暴露在顶层命名空间。

**预期结果**：全部 `True`。这印证了「用户只需面对扁平的 `scipy.interpolate` 命名空间」。

> 待本地验证：如果你用的 SciPy 版本里某个名字刚被移除或重命名，`hasattr` 可能返回 `False`，以你本地实际结果为准。

#### 4.2.5 小练习与答案

**练习 1**：你想对一张分辨率较低的灰度图（规则二维网格）做放大，应该优先看哪个 API？

**参考答案**：规则网格场景，优先 `RegularGridInterpolator`（`method='linear'` 或 `'cubic'`），或一次性求值的便捷函数 `interpn`。

**练习 2**：docstring 里 FITPACK 一节说「大多数情况下用户更适合用前面列出的高层例程」。请举一个「更推荐用新 API」的等价替代。

**参考答案**：旧式函数 `splrep` + `splev` 拟合一维样条，可用 `make_interp_spline`（精确插值）或 `make_splrep` / `make_smoothing_spline`（平滑拟合）替代，它们返回现代的 `BSpline` 对象，接口更清晰、行为可复现。这一对比会在第 u7、u9 单元详细展开。

## 5. 综合实践

把本讲的两条主线（「`make_interp_spline` 来自 `_bsplines.py`」+「一维插值是最常用的一类」）串起来，完成下面的实战任务。

**任务**：导入 `scipy.interpolate`，对一组一维 \( (x, y) \) 数据用 `make_interp_spline` 做三次（\( k=3 \)）样条插值，并用 matplotlib 画出原数据点与插值曲线。

**操作步骤**：

```python
import numpy as np
import matplotlib.pyplot as plt
from scipy.interpolate import make_interp_spline

# 1. 准备一组稀疏的原始数据点
x = np.array([0.0, 1.0, 2.0, 3.0, 4.0, 5.0])
y = np.array([0.0, 0.8, 0.9, 0.1, -0.8, -1.0])

# 2. 用三次样条做插值，得到一个 BSpline 对象（可像函数一样调用）
spl = make_interp_spline(x, y, k=3)   # k=3 即三次，也是默认值

# 3. 在更密的网格上求值，得到平滑曲线
x_dense = np.linspace(x.min(), x.max(), 200)
y_dense = spl(x_dense)

# 4. 画图：原始点 + 插值曲线
plt.plot(x, y, 'o', label='原始数据点')
plt.plot(x_dense, y_dense, '-', label='三次样条插值')
plt.legend()
plt.title('make_interp_spline 一维三次样条插值')
plt.show()
```

**需要观察的现象**：

1. 插值曲线（实线）**精确穿过**每一个原始数据点（圆点）——这正是「插值」的定义（见第 2.1 节）。
2. 曲线在数据点之间是**光滑**的（三次样条保证二阶导连续，即 C² 连续，第 u2 单元会讲）。
3. 运行 `type(spl)` 会显示 `BSpline`，说明 `make_interp_spline` 返回的就是第 4.1 节提到的、来自 `_bsplines.py` 的 `BSpline` 对象。

**预期结果**：得到一张图，圆点与实线在 6 个原始 \( x \) 处完全重合；实线在区间之间平滑过渡。

**进阶观察（可选）**：把 `k=3` 改成 `k=1`（线性），曲线会变成「折线」——对应最简单的线性插值，光滑性下降但不再有任何「过冲」。这能帮你直观体会「插值阶数」对结果的影响。

> 说明：上述代码是示例代码，需要本地安装 `numpy`、`scipy`、`matplotlib` 才能运行；如果环境无图形界面，把 `plt.show()` 换成 `plt.savefig('interp.png')` 即可。

## 6. 本讲小结

- **插值**要求曲线精确穿过每个已知点 \( p(x_i)=y_i \)；**拟合**允许不穿过点而追求总体误差最小。`scipy.interpolate` 主打插值，也含平滑样条。
- `scipy.interpolate` 解决**三大类问题**：一维插值、规则网格多维插值、散点（非结构化数据）插值。
- `__init__.py` 用一连串 `from ._xxx import *` 把十几个实现模块（如 `_bsplines.py`）的公开名字汇聚成扁平的公共命名空间。
- `__all__` 不是手写的，而是用 `[s for s in dir() if not s.startswith('_')]` **自动收集**，保证「搬进来的名字一定在公共 API 里」。
- `__init__.py` 顶部的长 docstring 既是官方文档目录，也是按「三大类问题」组织的功能地图；`RegularGridInterpolator`、`griddata`、`make_interp_spline` 分别是三大类的代表主力。
- `make_interp_spline(x, y, k=3)` 返回一个 `BSpline` 对象，可像函数一样调用求值，是做一维插值的首选现代接口。

## 7. 下一步学习建议

本讲建立的是「全局地图」，接下来建议：

- **如果你只想快速上手一维插值**：直接进入第 **u2 单元**（一维插值快速上手），学 `CubicSpline` / `PchipInterpolator` 的边界条件与保形特性。
- **如果你想搞懂目录与构建细节**：先读第 **u1-l2**（目录结构与模块地图）和 **u1-l3**（入口与导入链路），那里会讲清「垫片模块」「meson 构建」「延迟弃用」等本讲只点到为止的机制。
- **延伸阅读源码**：在进入 u2 之前，可以打开 [`_bsplines.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/_bsplines.py) 第 1642 行附近的 `make_interp_spline`，先扫一眼它的参数（`x, y, k=3, t=None, bc_type=None`），为后续讲义做铺垫。

下一讲我们将从「目录里那些文件各自干什么」继续展开。
