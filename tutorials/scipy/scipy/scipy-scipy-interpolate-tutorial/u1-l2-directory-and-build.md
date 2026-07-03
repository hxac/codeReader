# 目录结构与模块地图

## 1. 本讲目标

学完本讲，你应该能够：

- 看懂 `scipy/interpolate/` 目录里四类文件分别是什么、各起什么作用：**公共 Python 模块**（`_xxx.py`）、**Cython 源**（`.pyx/.pxi`）、**原生 C/C++ 扩展**（`src/`）、**已弃用的命名空间垫片**（`fitpack.py`、`interpolate.py` 等）。
- 把任意一个公共 API 名字（例如 `CubicSpline`、`splrep`）追溯回它真正定义的那个 `_xxx.py` 私有模块。
- 读懂 `meson.build`，知道每个原生扩展是如何被编译、安装进 `scipy.interpolate` 的。
- 理解为什么存在「垫片文件」，以及它们如何在 SciPy v2.0 被移除前继续兼容旧式 `from scipy.interpolate.interpolate import interp1d` 写法。

承接上一讲（u1-l1）：上一讲建立了「插值 vs 拟合」「三大类问题」的全景，并指出 `__init__.py` 用一连串 `from ._xxx import *` 拼装出扁平命名空间。本讲就钻进这个目录，把那张「拼装图」拆开给你看。

## 2. 前置知识

- **模块（module）与包（package）**：一个 `.py` 文件就是一个模块；一个含 `__init__.py` 的目录就是一个包。`scipy.interpolate` 是 `scipy` 包里的一个子包。
- **`__all__`**：模块里一个列表，决定 `from module import *` 时会导出哪些名字。本讲会大量遇到它。
- **`from .xxx import *`**：`.` 表示「当前包」，所以 `from ._bsplines import *` 的意思是「从同目录下的 `_bsplines.py` 导入其全部公开名字」。
- **扩展模块（extension module）**：用 C / C++ / Cython 写、需要编译成 `.so`（Linux）才能被 Python `import` 的模块。它们和普通 `.py` 一样能被 `import`，但源码不在 `.py` 里。
- **构建系统（build system）**：SciPy 用 [Meson](https://mesonbuild.com/) 描述「哪些文件要编译、怎么编译、装到哪里」。每个子目录的 `meson.build` 就是该子目录的编译说明书。

> 不需要你写过 Meson 或 Cython；本讲只要求你「读懂」说明书，看懂文件之间的对应关系即可。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用它来 |
|---|---|---|
| `__init__.py` | 子包入口，拼装公共命名空间 | 读 `import` 顺序，建立「公共名→私有模块」对照 |
| `meson.build` | 子包的编译/安装说明书 | 看原生扩展如何被构建 |
| `fitpack.py` | 弃用垫片（旧命名空间 `scipy.interpolate.fitpack`） | 理解垫片的延迟弃用机制 |
| `interpolate.py` | 弃用垫片（旧命名空间 `scipy.interpolate.interpolate`） | 同上，对照多个垫片 |
| `src/` 目录 | 原生 C/C++ 源码（`__fitpack.cc` 等） | 认识原生扩展的真正位置 |

永久链接基准（本讲所有链接均基于当前 HEAD `5f09bd7`）：
`https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/`

## 4. 核心概念与源码讲解

### 4.1 三类（实为四类）文件与目录结构

#### 4.1.1 概念说明

打开 `scipy/interpolate/` 目录，你会看到几十个文件，初看很乱。但其实它们只分成四类：

1. **公共 Python 模块**：名字以下划线开头（`_bsplines.py`、`_cubic.py` 等）。这是**真正的实现**所在。下划线是 Python 的约定，表示「私有」，不该被用户直接 `from scipy.interpolate._bsplines import ...`。
2. **Cython 源文件**：`.pyx`（如 `_ppoly.pyx`）和 `.pxi`（如 `_poly_common.pxi`）。它们是「长得像 Python、但能编译成 C 扩展」的源码，用来做性能关键路径（求值、求根）。
3. **原生 C/C++ 源码**：集中在 `src/` 子目录（`__fitpack.cc`、`_fitpackmodule.c` 等）。这是 Fortran/C 的数值计算内核（FITPACK 算法）。
4. **已弃用的命名空间垫片（shim）**：`fitpack.py`、`interpolate.py`、`fitpack2.py`、`ndgriddata.py`、`polyint.py`、`rbf.py`、`interpnd.py`、`dfitpack.py` 这几个**没有下划线**的文件。它们**几乎不含实现**，只负责把旧式导入路径重定向到新模块，并发出弃用警告。SciPy v2.0 会删除它们。

记住一个直觉：**「带下划线的是干活的人，不带下划线的是已退休但还挂着名牌的老员工。」**

为什么要有第 4 类垫片？历史上用户写 `from scipy.interpolate.interpolate import interp1d`（注意两层 `interpolate`）是合法的，因为 `interpolate.py` 曾是真正的实现文件。后来 SciPy 重构，实现搬到了 `_interpolate.py`，但为了不立刻破坏全世界已有的代码，就留了一个空的 `interpolate.py`「垫片」做转发，并计划在 v2.0 彻底删除。

#### 4.1.2 核心流程：`__init__.py` 如何拼装命名空间

`scipy.interpolate` 这个扁平名字空间，是 `__init__.py` 顶部的十几行 `import` 一块块拼出来的。流程是：

```text
for 每个 _xxx.py 私有模块:
    执行  from ._xxx import *
        ↓ 把该模块 __all__ 里的名字「倒」进当前命名空间
所有名字汇聚到 __init__.py 的全局变量表
        ↓
__all__ = [s for s in dir() if not s.startswith('_')]   # 自动收集，不手写
```

关键点：**顺序很重要**。后导入的模块若也导出了同名符号，会覆盖前面的。因此要看某个公共名字「来自哪里」，最可靠的方法是按 `__init__.py` 的 import 顺序、结合各模块的 `__all__` 来判断。

#### 4.1.3 源码精读

**(a) `__init__.py` 的导入链路**——这就是上一讲提到的「拼装图」本体：

[__init__.py:192-216](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/__init__.py#L192-L216) 把十几个私有模块的公开名字汇聚到 `scipy.interpolate`。摘录关键几行：

```python
from ._interpolate import *          # interp1d, PPoly, BPoly, NdPPoly, lagrange ...
from ._fitpack_py import *           # splrep, splev, splint ...
from ._fitpack2 import *             # UnivariateSpline 等面向对象类
from ._rbf import Rbf
from ._cubic import *                # CubicSpline, PchipInterpolator, Akima ...
from ._bsplines import *             # BSpline, make_interp_spline ...
from ._fitpack_repro import generate_knots, make_splrep, make_splprep   # 显式列举
from ._rgi import *                  # RegularGridInterpolator, interpn
from ._ndbspline import NdBSpline
from ._bary_rational import *        # AAA, FloaterHormannInterpolator
```

注意两种风格混用：大部分用 `import *`（依赖各模块的 `__all__`），而 `_fitpack_repro`、`_ndbspline` 则**显式列出**要导入的名字。

紧接着是 [__init__.py:218-221](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/__init__.py#L218-L221) 的垫片导入与自动 `__all__`：

```python
# Deprecated namespaces, to be removed in v2.0.0
from . import fitpack, fitpack2, interpolate, ndgriddata, polyint, rbf, interpnd

__all__ = [s for s in dir() if not s.startswith('_')]
```

`dir()` 返回当前已绑定的全部名字，过滤掉下划线开头的，剩下的就是公共 API。这就是为什么你**看不到手写的 `__all__` 列表**——它是自动生成的。

**(b) 各私有模块的 `__all__`**——这是做「公共名→私有模块」对照的依据。几个典型：

- [_interpolate.py:1](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/_interpolate.py#L1)：`__all__ = ['interp1d', 'interp2d', 'lagrange', 'PPoly', 'BPoly', 'NdPPoly']`
- [_bsplines.py:22](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/_bsplines.py#L22)：`__all__ = ["BSpline", "make_interp_spline", "make_lsq_spline", "make_smoothing_spline"]`
- [_cubic.py:17](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/_cubic.py#L17)：`__all__ = ["CubicHermiteSpline", "PchipInterpolator", "pchip_interpolate", "Akima1DInterpolator", "CubicSpline"]`
- [_rgi.py:1](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/_rgi.py#L1)：`__all__ = ['RegularGridInterpolator', 'interpn']`
- [_bary_rational.py:34](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/_bary_rational.py#L34)：`__all__ = ["AAA", "FloaterHormannInterpolator"]`

**(c) 垫片文件长什么样**——以 [fitpack.py:1-31](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/fitpack.py#L1-L31) 为例，全文只有三段，没有任何实现：

```python
# This file is not meant for public use and will be removed in SciPy v2.0.0.
from scipy._lib.deprecation import _sub_module_deprecation

__all__ = ['BSpline', 'bisplev', 'bisplrep', 'insert', 'spalde', ...]   # 仅声明

def __getattr__(name):
    return _sub_module_deprecation(sub_package="interpolate", module="fitpack",
                                   private_modules=["_fitpack_py"], all=__all__,
                                   attribute=name)
```

注意 [fitpack.py:28-31](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/fitpack.py#L28-L31) 的 `__getattr__`：模块级的 `__getattr__` 是 Python 3.7+ 的特性，**只有当你真正去取某个属性时才会触发**。所以垫片不会在被 import 时就报警告，而是延迟到你访问 `scipy.interpolate.fitpack.splrep` 那一刻——这就是「延迟弃用（lazy deprecation）」，能把无谓的警告噪声降到最低。`interpolate.py` 走的是完全相同的套路，见 [interpolate.py:28-30](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/interpolate.py#L28-L30)，只是 `private_modules` 指向 `["_interpolate", "fitpack2", "_rgi"]` 这几个真正的实现模块。

#### 4.1.4 代码实践：建立「公共名→私有模块」对照表

**实践目标**：亲手把 `__init__.py` 的 import 顺序和各模块 `__all__` 对齐，得到一张可信的对照表，并用代码验证。

**操作步骤**：

1. 读 [__init__.py:192-216](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/__init__.py#L192-L216)，按出现顺序记下每个私有模块。
2. 对每个模块查它的 `__all__`（用编辑器打开，或 `python -c "import scipy.interpolate._cubic as m; print(m.__all__)"`）。
3. 把结果填进下表（已据当前 HEAD 核对）：

| 公共 API 名 | 真正定义所在的私有模块 |
|---|---|
| `interp1d`, `interp2d`, `lagrange`, `PPoly`, `BPoly`, `NdPPoly` | `_interpolate.py` |
| `splrep`, `splprep`, `splev`, `splint`, `sproot`, `spalde`, `bisplrep`, `bisplev`, `insert`, `splder`, `splantider` | `_fitpack_py.py` |
| `UnivariateSpline`, `InterpolatedUnivariateSpline`, `LSQUnivariateSpline`, `BivariateSpline`, `SmoothBivariateSpline`, `LSQBivariateSpline`, `RectBivariateSpline`, 球面各类 | `_fitpack2.py` |
| `Rbf` | `_rbf.py` |
| `RBFInterpolator` | `_rbfinterp.py` |
| `KroghInterpolator`, `krogh_interpolate`, `BarycentricInterpolator`, `barycentric_interpolate`, `approximate_taylor_polynomial` | `_polyint.py` |
| `CubicSpline`, `PchipInterpolator`, `pchip_interpolate`, `Akima1DInterpolator`, `CubicHermiteSpline` | `_cubic.py` |
| `griddata`, `NearestNDInterpolator`, `LinearNDInterpolator`, `CloughTocher2DInterpolator` | `_ndgriddata.py` |
| `BSpline`, `make_interp_spline`, `make_lsq_spline`, `make_smoothing_spline` | `_bsplines.py` |
| `generate_knots`, `make_splrep`, `make_splprep` | `_fitpack_repro.py` |
| `pade` | `_pade.py` |
| `RegularGridInterpolator`, `interpn` | `_rgi.py` |
| `NdBSpline` | `_ndbspline.py` |
| `AAA`, `FloaterHormannInterpolator` | `_bary_rational.py` |

4. 用下面这段「示例代码」程序化验证（确认每个公共名字的 `__module__` 落在上表预测的模块上）：

```python
# 示例代码：验证公共名 → 私有模块 的映射
import scipy.interpolate as I

expect = {
    "CubicSpline": "_cubic", "splrep": "_fitpack_py", "BSpline": "_bsplines",
    "RegularGridInterpolator": "_rgi", "griddata": "_ndgriddata",
    "RBFInterpolator": "_rbfinterp", "AAA": "_bary_rational",
}
for name, mod in expect.items():
    obj = getattr(I, name)
    print(f"{name:28s} -> {obj.__module__.rsplit('.', 1)[-1]:16s}  预测={mod}")
```

**需要观察的现象**：打印出的实际 `__module__` 末尾段应与「预测」列完全一致；`pchip_interpolate`、`Rbf` 等也会各自落到对应私有模块。

**预期结果**：所有名字都对得上，证明 `__init__.py` 的 `import *` 确实只是「搬运」而非「定义」。若本地未安装可编辑版 SciPy，可用 `python -c "import scipy; print(scipy.__file__)"` 确认安装位置；无法运行则记为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `__init__.py` 用 `[s for s in dir() if not s.startswith('_')]` 自动生成 `__all__`，而不是手写一个列表？
**答案**：因为公共名字分散在十几个私有模块里，且会随重构变化；手写列表很容易和实际导出的名字脱节。自动收集能保证 `__all__` 永远等于「当前命名空间里所有不带下划线的名字」，零维护成本。

**练习 2**：用户写 `from scipy.interpolate.interpolate import interp1d` 会发生什么？
**答案**：Python 先导入垫片模块 `interpolate.py`；该模块本身不报错，但当你取 `interp1d` 时触发其模块级 `__getattr__`，进而调用 `_sub_module_deprecation` 发出 `DeprecationWarning`，并把请求转发到真正的实现模块 `_interpolate`。该垫片计划在 SciPy v2.0 删除。

**练习 3**：`_fitpack_impl.py` 也定义了和 `_fitpack_py.py` 几乎一样的 `__all__`（见 [_fitpack_impl.py:24](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/_fitpack_impl.py#L24)），但 `__init__.py` 只 `import *` 了 `_fitpack_py`。这两个模块是什么关系？
**答案**：`_fitpack_py.py` 是面向用户的函数式 FITPACK 接口（纯 Python 包装），它内部再调用 `_fitpack_impl.py` 这个更底层的实现层（后者直接对接 C 扩展）。公共命名空间只暴露上层 `_fitpack_py`，避免名字冲突；`_fitpack_impl` 是给同子包其他模块（如 `_fitpack2.py`）复用的「内部 API」。

### 4.2 原生扩展与 meson.build 构建系统

#### 4.2.1 概念说明

上一节讲的都是 `.py` 文件——它们装上就能用。但 `scipy.interpolate` 还有一批**必须先编译**才能 `import` 的扩展模块，比如 `_ppoly`、`_dierckx`、`_fitpack`、`_interpnd`。这些扩展承载了性能关键或数值稳定的内核（分段多项式求值、de Boor 算法、FITPACK 最小二乘）。

「编译」这件事由 Meson 负责。`scipy/interpolate/meson.build` 这份说明书告诉 Meson 三件事：

1. **哪些 Cython `.pyx` 要编译成扩展**（经 Cython 转译成 C 再编译）。
2. **哪些 C/C++ 源要编译成扩展**，以及它们依赖哪些静态库。
3. **哪些 `.py` 要原样安装**到 `scipy/interpolate/` 目录下。

为什么这对你重要？因为当你 `import scipy.interpolate` 报 `ModuleNotFoundError: _ppoly` 之类的错时，根源往往就在这份 `meson.build`——某个扩展没被正确编译或安装。

#### 4.2.2 核心流程：扩展的三种构建模式

`meson.build` 里出现了三种构造扩展的方式：

```text
模式 A：纯 Cython 扩展
   _interpnd.pyx  --(Cython 转译)-->  _interpnd.c  --(编译)-->  _interpnd.so

模式 B：C/C++ 扩展 + 静态库依赖
   src/__fitpack.cc  --(编译)-->  静态库 lib__fitpack.a
   src/_dierckxmodule.cc --(链接静态库)-->  _dierckx.so

模式 C：可选扩展（取决于构建配置）
   if use_pythran:  编译 _rbfinterp_pythran.py 成扩展
   else:            直接当普通 .py 安装（退化为 numpy 后端）
```

关键术语：

- `py3.extension_module(name, ...)`：Meson SciPy 惯用的「定义一个 Python 扩展模块」的函数，`install: true` 表示装到 site-packages。
- `static_library`：把一组源编成静态库，便于多个扩展共享同一段 C++ 代码（这里是 FITPACK 内核 `__fitpack.cc`）。
- `declare_dependency`：把静态库包装成「依赖项」，后续扩展 `dependencies:` 引用它即可链接。
- `py3.install_sources([...])`：把纯 Python 文件**原样复制**到安装目录（不编译）。

#### 4.2.3 源码精读

**(a) 三个 Cython 扩展**——分别对应 `_interpnd.pyx`、`_ppoly.pyx`、`_rgi_cython.pyx`：

[meson.build:1-25](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/meson.build#L1-L25) 定义了这三个。以 `_ppoly` 为例（[meson.build:10-17](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/meson.build#L10-L17)）：

```meson
py3.extension_module('_ppoly',
  linalg_cython_gen.process('_ppoly.pyx'),   # Cython 预处理（还注入了 LAPACK 头）
  c_args: cython_c_args,
  dependencies: np_dep,                       # 依赖 NumPy C-API
  link_args: version_link_args,
  install: true,
  subdir: 'scipy/interpolate'                 # 装到这个相对路径
)
```

`linalg_cython_gen` / `spt_cython_gen` 是上层 `meson.build` 预定义的 Cython 生成器，区别在于前者额外带上 LAPACK 头文件——这正是 `_ppoly` 的 `roots` 能调用 LAPACK 特征值例程的编译期前提（详见第 16 单元）。

**(b) C++ 静态库 + `_dierckx` 扩展**——现代 FITPACK 路径：

[meson.build:27-44](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/meson.build#L27-L44) 先把 `src/__fitpack.cc` 编成静态库，再用它构建 `_dierckx`：

```meson
__fitpack_lib = static_library('__fitpack',
    ['src/__fitpack.h', 'src/__fitpack.cc'],
    dependencies:[lapack_dep, np_dep, py3_dep],
)
__fitpack_dep = declare_dependency(link_with: __fitpack_lib, )

py3.extension_module('_dierckx',
    ['src/_dierckxmodule.cc'],
    include_directories: 'src/',
    dependencies: [np_dep, __fitpack_dep],    # 链接上面的静态库
    ...
)
```

注意 `src/` 目录里 [src/](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/src) 实际包含六类文件：`__fitpack.cc/.h`（现代 C++ FITPACK 内核）、`_dierckxmodule.cc`（`_dierckx` 的 pybind 风格绑定）、`_fitpackmodule.c`（`_fitpack` 的 C 绑定）、`dfitpack.c/.h`（由 Fortran 经 f2c 转译的 netlib FITPACK）。**`_dierckx` 和 `_fitpack` 是两套并行的 FITPACK 后端**，这是全子包最重要的架构事实之一，第 16 单元会专讲。

**(c) `_fitpack` 扩展（f2c 路径）**：

[meson.build:46-53](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/meson.build#L46-L53) 把 `_fitpackmodule.c` 和 `dfitpack.c` 一起编进 `_fitpack` 扩展——它服务于老的 `splrep` / `UnivariateSpline` 函数式接口。

**(d) 可选的 pythran 扩展**：

[meson.build:55-69](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/meson.build#L55-L69) 用 `if use_pythran` 分支：有 Pythran 就把 `_rbfinterp_pythran.py` 编成加速扩展，没有就当普通 `.py` 安装（RBF 退回纯 numpy 后端）。这就是为什么不同机器上 RBF 的后端可能不同。

**(e) 纯 Python 文件的安装清单**：

[meson.build:71-102](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/meson.build#L71-L102) 的 `py3.install_sources([...])` 是一张「要原样安装的 `.py`」清单。**仔细看这张清单**：它包含了所有 `_xxx.py` 私有模块，**也包含了 `fitpack.py`、`interpolate.py` 等垫片文件**——这正是垫片能存在于安装目录、从而支撑旧式导入的物理原因。最后一行 [meson.build:104](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/meson.build#L104) 的 `subdir('tests')` 递归进入 `tests/` 子目录的 `meson.build`。

#### 4.2.4 代码实践：核对「扩展名 → 源文件 → 构建方式」

**实践目标**：把 `meson.build` 里每个 `extension_module` 与它编译用的源文件、构建模式对齐，形成一张可核对的表，并在运行时确认扩展真的被装上了。

**操作步骤**：

1. 通读 [meson.build:1-104](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/interpolate/meson.build#L1-L104)，逐个 `extension_module` 抄下名字与源。
2. 整理成下表（已据当前 HEAD 核对）：

| 扩展名 | 源文件 | 构建模式 | 主要服务对象 |
|---|---|---|---|
| `_interpnd` | `_interpnd.pyx` | Cython | 散点 LinearND/CloughTocher |
| `_ppoly` | `_ppoly.pyx` | Cython（带 LAPACK） | PPoly/BPoly 求值、求根 |
| `_rgi_cython` | `_rgi_cython.pyx` | Cython | RegularGridInterpolator 2D 线性快速路径 |
| `_dierckx` | `src/_dierckxmodule.cc` + 静态库 `__fitpack` | C++ + 静态库 | 现代 BSpline/make_* 路径 |
| `_fitpack` | `src/_fitpackmodule.c` + `src/dfitpack.c` | C（f2c） | 旧 splrep/UnivariateSpline |
| `_rbfinterp_pythran` | `_rbfinterp_pythran.py` | Pythran（可选） | RBF numpy 后端加速 |

3. 用「示例代码」在运行时确认这些扩展确实以 `.so` 形式存在：

```python
# 示例代码：确认原生扩展已被编译安装
import importlib, os
for ext in ["_ppoly", "_dierckx", "_fitpack", "_interpnd", "_rgi_cython"]:
    m = importlib.import_module(f"scipy.interpolate.{ext}")
    path = getattr(m, "__file__", "")
    suffix = os.path.splitext(path)[1]
    print(f"{ext:16s} -> {os.path.basename(path)}  (编译产物后缀: {suffix})")
```

**需要观察的现象**：每个 `__file__` 都指向一个 `.so`（Linux）/ `.pyd`（Windows）/ `.dylib` 相关的编译产物，而**不是** `.py`。若 `_rbfinterp_pythran` 不在列表里能 import 成功，说明本机构建走了 `else` 分支（纯 `.py` 安装）。

**预期结果**：五个核心扩展均为编译产物。若你在未编译源码树里直接跑，可能 `import` 失败——此时记为「待本地验证」，并说明需先 `pip install -e . --no-build-isolation` 或用已发行版 SciPy。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_dierckx` 要先把 `__fitpack.cc` 编成静态库，再链接，而不是直接把 `__fitpack.cc` 列进 `_dierckx` 的源？
**答案**：因为 `__fitpack.cc` 是**多个扩展共享**的内核。把它做成静态库 + `declare_dependency`，未来若有别的扩展也需要这套 FITPACK 内核，只需在 `dependencies:` 里加一个 `__fitpack_dep` 即可，避免源码重复编译、保证一致性。（当前主要消费者是 `_dierckx`。）

**练习 2**：`_ppoly` 用 `linalg_cython_gen`，而 `_interpnd` 用 `spt_cython_gen`，这两个生成器的实质区别是什么？
**答案**：两者都先把 `.pyx` 转成 `.c`，但 `linalg_cython_gen` 额外注入了 SciPy 的 LAPACK/BLAS 头与链接信息，使扩展能调用线性代数例程（`_ppoly` 的求根依赖 LAPACK 特征值求解）；`spt_cython_gen`（spatial/template 通用生成器）则不带这些。所以选哪个生成器，取决于扩展是否需要 LAPACK。

**练习 3**：如果一台机器没装 Pythran，`scipy.interpolate` 还能用 `RBFInterpolator` 吗？
**答案**：能。`meson.build` 的 `if use_pythran / else` 分支保证：无 Pythran 时 `_rbfinterp_pythran.py` 作为普通 `.py` 安装，RBF 退回纯 numpy 后端（`_rbfinterp_np`），功能完整但更慢。Pythran 只是可选的加速手段。

## 5. 综合实践

**任务**：画出 `scipy.interpolate` 的「文件到运行时对象」全景图，把本讲两节串起来。

具体做法：

1. **静态侧（来自 `meson.build`）**：列出本子包的 6 个原生扩展，以及它们的源文件类别（Cython / C++ / f2c-C / Pythran）。
2. **静态侧（来自 `__init__.py`）**：列出十几个私有 `_xxx.py` 模块，各贡献了哪些公共 API。
3. **运行时侧**：写一段「示例代码」，对一个公共对象同时打印它的 `__module__`（来自哪节）和它间接依赖的扩展是否存在：

```python
# 示例代码：把"公共对象 -> 私有模块 -> 原生扩展"三层串起来
import scipy.interpolate as I
import importlib.util

obj_to_ext = {
    "CubicSpline": "(纯 Python，无原生扩展)",
    "BSpline":     "_dierckx",
    "PPoly":       "_ppoly",
    "splrep":      "_fitpack",
    "RegularGridInterpolator": "_rgi_cython",
    "LinearNDInterpolator":    "_interpnd",
}
for name, ext in obj_to_ext.items():
    obj = getattr(I, name)
    mod = obj.__module__
    present = importlib.util.find_spec(f"scipy.interpolate.{ext}") is not None if ext else "N/A"
    print(f"{name:26s} | 定义于 {mod:32s} | 依赖扩展 {ext:14s} 已安装={present}")
```

4. **观察并解释**：哪些公共对象是「纯 Python」（如 `CubicSpline`，定义在 `_cubic.py`，构造阶段不直接调用扩展）？哪些对象的求值/构造一定会落到原生扩展？把结论写在你的笔记里——这张图就是后续每一讲的「定位坐标」。

**预期结果**：你能凭这张图，对任何一个 `scipy.interpolate` 的名字立刻回答两个问题：「它定义在哪个 `.py`？」「它跑起来要靠哪个 `.so`？」这正是阅读本子包源码的基本功。

## 6. 本讲小结

- `scipy/interpolate/` 的文件分四类：**公共私有模块**（`_xxx.py`，干活）、**Cython 源**（`.pyx/.pxi`）、**原生 C/C++ 源**（`src/`）、**弃用垫片**（不带下划线的 `fitpack.py` 等）。
- `__init__.py` 用一连串 `from ._xxx import *` 把各模块的公开名字拼成扁平命名空间，`__all__` 用 `dir()` 自动收集，零手写维护。
- 任何公共 API 都能通过查对应私有模块的 `__all__` 追溯回定义文件；后导入的同名符号会覆盖先导入的，故顺序重要。
- 垫片文件几乎不含实现，靠模块级 `__getattr__` 做**延迟弃用**，计划在 SciPy v2.0 移除。
- `meson.build` 用三种模式构建扩展：纯 Cython、C/C++ + 静态库（`__fitpack`）、可选 Pythran；并用 `install_sources` 原样安装所有 `.py`（含垫片）。
- 子包内并存 **`_dierckx`（现代 C++ FITPACK）** 与 **`_fitpack`（f2c 旧 FITPACK）** 两套原生后端，这是后续第 16 单元的核心主题。

## 7. 下一步学习建议

- 下一讲 **u1-l3 入口与导入链路** 会更细地拆解 `__init__.py` 的导入顺序、`__all__` 的生成时机，并亲手触发垫片的 `DeprecationWarning`，把本讲的「延迟弃用」机制跑给你看。
- 想立刻看「干活的人」长什么样，可以直接打开 `_bsplines.py`（B 样条，第 5 单元）或 `_cubic.py`（一维三次插值，第 2 单元）的顶部 `__all__`，对照本讲的对照表确认。
- 对原生扩展感兴趣的同学，建议先读 `src/__fitpack.h` 的注释了解现代 FITPACK 接口，但要等第 16 单元才会系统讲解两套后端的差异。
