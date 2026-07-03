# 构建、导入与测试：如何把 special 跑起来

## 1. 本讲目标

`scipy.special` 不是一个纯 Python 模块。它包含大量用 Cython 和 C/C++ 编写的「扩展模块」，这些模块在安装 SciPy 之前必须先被**编译**成操作系统级的共享库（`.so` / `.pyd` / `.dylib`），Python 才能导入它们。

学完本讲，你应该能够：

- 说清 `scipy.special` 为什么是「编译型子模块」，以及是谁、用什么系统把它编译出来的。
- 看懂 `scipy/special/meson.build` 里几类关键的构建目标（静态库、扩展模块、代码生成、数据打包），并理解它们各自产出什么。
- 理解「编译产物」如何被 `import scipy.special` 加载成可调用的 Python 对象。
- 掌握两种运行测试的方式：`special.test()` 和直接用 `pytest`，并理解 `tests/` 目录的组织方式。

本讲只读两个文件：[`meson.build`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/meson.build) 和 [`__init__.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py)。其余实现细节留待后续讲义。

## 2. 前置知识

- **编译型扩展模块（C extension）**：CPython 允许用 C/Cython 写代码，编译成共享库后，用 `import` 像普通 `.py` 文件一样加载。NumPy、Pandas 的核心都是这种东西。`scipy.special` 的绝大多数数学函数就住在这样的扩展模块里。
- **Cython**：一种「Python 风格但可标注 C 类型」的语言，源文件后缀 `.pyx`。Cython 编译器把 `.pyx` 翻译成 `.c`/`.cpp`，再用 C 编译器编成共享库。`special` 用 Cython 把 C/C++ 数学内核包装成 NumPy ufunc。
- **Meson**：一个现代化的构建系统（用 `meson.build` 描述），SciPy 从 1.9 起用它替代旧的 `setup.py`/`distutils`。Meson 负责「调 Cython 编译器、调 C/C++ 编译器、链接库、安装产物」这一整套流程。
- **`subdir()`**：Meson 里把子目录的 `meson.build` 嵌入主构建的语法。SciPy 顶层 `scipy/meson.build` 用 [`subdir('special')`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/meson.build#L723) 把本模块纳入构建。
- **`pytest`**：Python 的事实标准测试框架。SciPy 的测试都基于它。
- **ufunc**：NumPy 通用函数，逐元素求值、支持广播。本模块几乎所有函数都是 ufunc（详见上一讲 u1-l1）。

> 本讲承接 u1-l2 建立的「Python 包装层 → Cython 层 → C/C++ 内核层」三层地图。本讲的焦点是：这三层**是怎么被构建出来、又怎么变成可运行代码**的。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲怎么用它 |
|------|------|--------------|
| [`meson.build`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/meson.build) | 定义本模块所有构建目标（静态库、扩展模块、代码生成、数据打包、安装） | 4.1、4.2 的主战场 |
| [`__init__.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py) | 包入口；导入扩展模块、组装命名空间、暴露 `test()` | 4.2、4.3 |
| [`scipy/_lib/_testutils.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/_lib/_testutils.py) | 实现 `PytestTester`，即 `special.test()` 的本体 | 4.3 |
| [`tests/meson.build`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/tests/meson.build) | 声明要安装哪些测试文件与参考数据 | 4.3 |

## 4. 核心概念与源码讲解

### 4.1 Meson 构建流程：从 `subdir` 到扩展模块

#### 4.1.1 概念说明

「特殊函数」本身是数学，但 `scipy.special` 作为软件，其工程难点在于：**如何把 250+ 个分散在 Cython/C/C++ 文件里的函数，有条不紊地编译、链接、安装成一堆可被 Python 导入的共享库。** 这件事由 [`meson.build`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/meson.build) 全权描述。

理解这一节的关键，是把 `meson.build` 里的语句分成几类「构建目标」来看：

1. **静态库（`static_library`）**：把一组 C 源码编成 `.a` 静态库，供扩展模块链接，不直接被 Python 导入。
2. **扩展模块（`py3.extension_module`）**：编出 Python 可 `import` 的共享库，这是本模块的「成品」。
3. **代码生成（`custom_target`）**：在编译**之前**运行一个 Python 脚本，临时生成 `.pyx`/`.h` 源文件——这是本模块「工程心脏」（详见 u3 单元）。
4. **Cython 生成器（`generator`）**：把 `.pyx` 翻译成 `.c`/`.cpp`。
5. **安装（`py3.install_sources`）**：把纯 `.py` 文件拷到安装目录。
6. **子目录（`subdir`）**：继续递归处理 `tests/` 和 `_precompute/`。

#### 4.1.2 核心流程

从「源码」到「可导入模块」的整条流水线可以画成：

```
scipy/meson.build
   │  subdir('special')          # 把本目录纳入构建
   ▼
scipy/special/meson.build
   │
   ├─[代码生成] custom_target('cython_special')
   │      运行 _generate_pyx.py + functions.json
   │      → 生成 _ufuncs.pyx、_ufuncs_cxx.pyx、若干 *_defs.h
   │
   ├─[Cython 翻译] generator(cython)
   │      cython_special.pyx  → cython_special.c
   │      _ufuncs.pyx         → _ufuncs.c
   │      _ufuncs_cxx.pyx     → _ufuncs_cxx.cpp
   │
   ├─[编译+链接] py3.extension_module(...)
   │      .c/.cpp + 手写 C/C++ → _ufuncs...so / cython_special...so 等
   │
   ├─[安装 .py] py3.install_sources(python_sources)
   │      __init__.py、_basic.py、_orthogonal.py ... → 安装目录
   │
   └─[递归] subdir('tests')  subdir('_precompute')
```

注意一个**容易混淆的命名点**：`meson.build` 里同时出现了两个 `cython_special`——

- 一个是 [`custom_target('cython_special', ...)`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/meson.build#L69-L80)，它的作用是**运行代码生成器**，产出 `_ufuncs.pyx` 等文件（变量名也叫 `cython_special`，后续被 `cython_special[0]` 引用）。
- 另一个是 [`py3.extension_module('cython_special', ...)`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/meson.build#L163-L179)，它才是真正编译 `cython_special.pyx` 得到的、可被 `cimport` 的扩展模块（u6 单元的主角）。

两者名字相同但完全不同：前者是「生成步骤」，后者是「成品模块」。

#### 4.1.3 源码精读

**静态库 `cdflib_lib`**：把概率分布内核 `cdflib.c` 编成静态库，供 `_ufuncs`、`cython_special` 链接。

[`meson.build:26-30`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/meson.build#L26-L30) 把 `cdflib.c` 编成名为 `cdflib` 的静态库，并隐藏符号（`gnu_symbol_visibility: 'hidden'`），避免污染全局符号表。

**七个扩展模块**：`meson.build` 里共有 7 个 `py3.extension_module(...)` 调用，对应 7 个可导入的共享库。举三个有代表性的：

- [`_special_ufuncs`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/meson.build#L40-L48)：直接用 C++ 写（`_special_ufuncs.cpp`），链接 `xsf_dep`（xsf 现代化 C++ 库）和 NumPy。这是「新的 ufunc 注册路径」（u8 单元）。
- [`_ufuncs`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/meson.build#L98-L114)：**体量最大的扩展模块**。它的源码由三部分拼成——手写 C/C++（`_cosine.c`、`xsf_wrappers.cpp`、`sf_error.cc`、`dd_real_wrappers.cpp`）、Cython 生成的 `_ufuncs.c`，以及链接 `cdflib_lib`。本模块绝大多数 ufunc（如 `jv`、`gamma`、`erf`）都住在这里。
- [`_ufuncs_cxx`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/meson.build#L140-L150)：专门承载需要 **Boost.Math** 的 C++ 函数（`betainc` 等），因此额外依赖 `boost_math_dep` 和 `ellint_dep`。

> 为什么要把 `_ufuncs` 和 `_ufuncs_cxx` 拆开？因为 Boost.Math 是重量级 C++ 模板库，编译很慢；只有需要它的少数函数才进 `_ufuncs_cxx`，其余大量函数留在纯 C 的 `_ufuncs` 里，避免拖慢整体构建。这就是「按 C++ 重型依赖切分扩展模块」的工程取舍。

**代码生成步骤**：

[`meson.build:68-80`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/meson.build#L68-L80) 定义 `custom_target`，它把 `_generate_pyx.py` 当作一个「构建期程序」运行，输入是 `functions.json`（函数签名声明）和 `_add_newdocs.py`，输出是 `_ufuncs.pyx` 等文件。`install: false` 表示这些中间产物不安装。这一步是 u3 单元的核心。

**Cython 生成器**：

[`meson.build:86-96`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/meson.build#L86-L96) 定义两个 `generator(cython, ...)`：`uf_cython_gen`（`.pyx` → `.c`）和 `uf_cython_gen_cpp`（`.pyx` → `.cpp`）。后续 `uf_cython_gen.process(cython_special[0])` 就是把生成的 `_ufuncs.pyx` 翻成 `_ufuncs.c`。

**安装与递归**：

[`meson.build:217-252`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/meson.build#L217-L252) 列出所有要安装的纯 Python 源文件（`python_sources`），并通过 `subdir('tests')`、`subdir('_precompute')` 继续处理两个子目录。

#### 4.1.4 代码实践：数清构建目标（源码阅读型）

1. **实践目标**：不靠记忆，仅凭 `meson.build` 数出本模块定义了哪些扩展模块，并分类。
2. **操作步骤**：
   - 打开 [`meson.build`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/meson.build)。
   - 搜索所有 `py3.extension_module(`，记下每个目标名。
   - 搜索 `static_library(`、`custom_target(`、`generator(`、`py3.install_sources(`、`subdir(`，各数一遍。
3. **需要观察的现象**：你应该得到 7 个扩展模块、1 个静态库（`cdflib`）、1 个代码生成 `custom_target`、2 个 Cython `generator`、2 处 `install_sources`、2 个 `subdir`。
4. **预期结果**：扩展模块名集合为 `{_special_ufuncs, _gufuncs, _specfun, _ufuncs, _ufuncs_cxx, _ellip_harm_2, cython_special}`。
5. **说明**：本实践是纯阅读，不运行任何命令；结论可直接在文件中核对。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cdflib` 用 `static_library` 而不是 `py3.extension_module`？
> **答案**：`cdflib` 是被多个扩展模块（`_ufuncs`、`cython_special`）共用的纯 C 数学内核，不是面向 Python 的模块。编成静态库后用 `link_with: cdflib_lib` 链接进去，可以复用代码且不暴露成可导入对象。

**练习 2**：`_ufuncs` 和 `_ufuncs_cxx` 为什么要拆成两个扩展模块？
> **答案**：`_ufuncs_cxx` 依赖重量级 C++ 模板库 Boost.Math，编译慢；把它与绝大多数纯 C 函数（`_ufuncs`）隔离，避免让所有函数都背上 Boost 的编译成本。

---

### 4.2 扩展模块加载：编译产物如何变成可调用对象

#### 4.2.1 概念说明

编译只是第一步。`_ufuncs...so` 编出来、装到 `site-packages/scipy/special/` 之后，还必须被 `import` 才能用。本节回答：**当你写下 `import scipy.special` 时，那 7 个 `.so` 是怎么被加载、又怎么变成 `special.jv` 这样的函数的？**

答案藏在 [`__init__.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py) 的导入语句里。`__init__.py` 在执行时会 `from . import _ufuncs`——这一句触发 Python 的标准 C 扩展加载机制：Python 解释器在包目录里找到 `_ufuncs...so`，调用其模块初始化函数，把模块对象挂到 `scipy.special._ufuncs` 名下。随后 `from ._ufuncs import *` 把里面的 ufunc 对象搬进 `scipy.special` 命名空间。

关键认知：**`special.jv` 并不是一个 Python 函数，而是一个住在编译模块 `_ufuncs...so` 里的 NumPy ufunc 对象，只是被命名空间转发出来了。**

#### 4.2.2 核心流程

```
import scipy.special
   │
   ▼  执行 __init__.py
from . import _ufuncs            # Python 加载 _ufuncs...so（C 扩展机制）
   │
from ._ufuncs import *           # 把 _ufuncs 里的 ufunc 搬进 special 命名空间
   │
from ._basic import *            # 纯 Python 函数（comb、factorial 等）
from ._support_alternative_backends import *   # 覆盖部分函数，加 Array API 支持
from ._logsumexp import logsumexp, softmax, log_softmax
from ._multiufuncs import *
from ._orthogonal import *
from ._ellip_harm import ellip_harm, ellip_harm_2, ellip_normal
from ._lambertw import lambertw
from ._spherical_bessel import spherical_jn, ...
   │
__all__ = _ufuncs.__all__ + _basic.__all__ + ...   # 汇总公共 API
   │
test = PytestTester(__name__)    # 暴露测试入口（见 4.3）
```

注意：被 `import` 的 7 个扩展模块中，只有 `_ufuncs`、`_ufuncs_cxx`、`_specfun`、`_ellip_harm_2`、`_multiufuncs`（间接）等是**运行时被加载**的；`cython_special` 是给用户在自己的 Cython 代码里 `cimport` 用的（u6 单元），普通 Python 调用不会自动导入它。

#### 4.2.3 源码精读

[`__init__.py:786-801`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L786-L801) 是命名空间拼装的核心：

```python
from ._sf_error import SpecialFunctionWarning, SpecialFunctionError
from . import _ufuncs          # ← 触发加载 _ufuncs...so
from ._ufuncs import *         # ← 把 ufunc 搬进 special 命名空间
from . import _basic
from ._basic import *
# Replace some function definitions from _ufuncs and _basic
# to add Array API support
from ._support_alternative_backends import *
from ._logsumexp import logsumexp, softmax, log_softmax
from . import _multiufuncs
from ._multiufuncs import *
from . import _orthogonal
from ._orthogonal import *
```

注释 `Replace some function definitions ... to add Array API support` 很关键：`_support_alternative_backends` 会**覆盖**部分来自 `_ufuncs`/`_basic` 的函数，给它们加上 PyTorch/JAX/CuPy 等多后端能力（u10 单元）。所以最终 `special.gamma` 可能已经不是裸 ufunc，而是被包了一层的对象。

[`__init__.py:825-841`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L825-L841) 用各子模块的 `__all__` 拼出最终的公共 API 列表。这就是为什么 `dir(special)` 与 `special.__all__` 大体一致。

要观察「编译模块真的被加载了」，可以用：

```python
import scipy.special as sc
print(sc._ufuncs.__file__)   # 指向某个 _ufuncs...so 路径
print(sc.__path__)           # 包目录
```

`_ufuncs.__file__` 的扩展名（`.so` / `.pyd`）会随平台变化，这正是「编译型扩展」的直观证据。

#### 4.2.4 代码实践：观察加载的编译模块

1. **实践目标**：亲眼确认 `special.jv` 来自一个编译出的共享库，而不是 `.py` 文件。
2. **操作步骤**：在已安装 SciPy 的环境运行
   ```bash
   python -c "import scipy.special as sc; print(sc._ufuncs.__file__)"
   python -c "import scipy.special as sc; print(type(sc.jv), sc.jv.__module__)"
   ```
3. **需要观察的现象**：第一条打印出一个 `.so`/`.pyd` 路径；第二条显示 `jv` 的类型是 `numpy.ufunc`，`__module__` 形如 `scipy.special._ufuncs`。
4. **预期结果**：`sc.jv` 是 ufunc，且确实归属编译模块 `_ufuncs`。具体路径随安装位置变化，**待本地验证**实际字符串。
5. **延伸思考**：再对比 `sc.logsumexp.__module__`，它应指向纯 Python 模块 `_logsumexp`，说明并非所有函数都来自编译层。

#### 4.2.5 小练习与答案

**练习 1**：`import scipy.special` 失败，报「找不到 `_ufuncs`」，最可能的原因是什么？
> **答案**：SciPy 没有正确编译/安装，`site-packages/scipy/special/` 下缺少 `_ufuncs...so`。这通常意味着需要重新 `pip install` 一个已编译的 wheel，或从源码用 Meson 重新构建。

**练习 2**：为什么 `special.gamma` 可能不是「裸 ufunc」？
> **答案**：因为 [`__init__.py:794-796`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L794-L796) 在导入 `_ufuncs` 之后，又用 `_support_alternative_backends` **覆盖**了部分函数定义以加入 Array API 支持。所以在开启了多后端的配置下，`special.gamma` 可能是被包装过的对象。

---

### 4.3 pytest 测试入口：`special.test()` 与 `PytestTester`

#### 4.3.1 概念说明

数值数学库的正确性极度依赖测试——一个常数写错小数点，整条统计链路就废了。`scipy.special` 的测试代码住在 `tests/` 目录，约 50 个 `test_*.py` 文件，外加 `tests/data/` 下的参考数据。

运行这些测试有两种等价方式：

1. **模块自带入口**：`scipy.special.test()`——这是 SciPy 给每个子模块都装上的便利方法。
2. **直接用 pytest**：`pytest scipy/special/tests/test_basic.py`——更灵活，可以指定单个文件、单个用例、加 `-k` 过滤。

本节解释第一种方式背后的 `PytestTester`：它其实只是一个**很薄的封装**，把「子模块名」翻译成「pytest 命令行参数」，最终仍调用 `pytest.main()`。

#### 4.3.2 核心流程

`special.test()` 的执行链：

```
scipy.special.test           # __init__.py 里: test = PytestTester(__name__)
   │  __name__ == 'scipy.special'
   ▼
PytestTester.__call__(label='fast', verbose=1, ...)
   │
   ├─ module = sys.modules['scipy.special']
   ├─ module_path = 模块所在目录
   ├─ 组装 pytest 参数: ['--showlocals','--tb=short', ...]
   ├─ label=='fast' → 追加 ['-m','not slow']
   └─ pytest_args += ['--pyargs', 'scipy.special']
   ▼
pytest.main(pytest_args)      # 真正跑测试
   ▼
返回 bool(code == 0)          # True 表示全部通过
```

`tests/` 目录的组织（来自 [`tests/meson.build`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/tests/meson.build)）：

- 按函数/主题拆分：`test_basic.py`（综合大文件）、`test_gamma.py`、`test_lambertw.py`、`test_logsumexp.py`、`test_sf_error.py` 等。
- 参考数据：`tests/data/` 下的 `boost.npz`/`gsl.npz`/`local.npz`，由 `meson.build` 用 [`utils/makenpz.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/utils/makenpz.py) 从 `.txt` 打包生成（见 [`meson.build:181-214`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/meson.build#L181-L214)），测试时用来校验函数值。
- 安装时带 `install_tag: 'tests'`，意味着这些测试文件只有在显式安装测试集时才会进入 `site-packages`。

#### 4.3.3 源码精读

`special.test()` 的诞生地在 [`__init__.py:843-845`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L843-L845)：

```python
from scipy._lib._testutils import PytestTester
test = PytestTester(__name__)
del PytestTester
```

三行做了三件事：导入 `PytestTester` 类；用本模块名 `'scipy.special'` 实例化一个**可调用对象**并命名为 `test`；最后 `del` 掉类本身，避免它污染 `special` 命名空间。所以 `special.test` 是一个**实例**，而不是类——调用它（`special.test()`）触发 `PytestTester.__call__`。

`PytestTester` 的实现 [`scipy/_lib/_testutils.py:63-142`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/_lib/_testutils.py#L63-L142)，核心是 `__call__` 方法 [`scipy/_lib/_testutils.py:96-142`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/_lib/_testutils.py#L96-L142)：

```python
def __call__(self, label="fast", verbose=1, extra_argv=None, doctests=False,
             coverage=False, tests=None, parallel=None):
    import pytest
    module = sys.modules[self.module_name]
    module_path = os.path.abspath(module.__path__[0])
    pytest_args = ['--showlocals', '--tb=short']
    ...
    if label == "fast":
        pytest_args += ["-m", "not slow"]
    ...
    pytest_args += ['--pyargs'] + list(tests)
    try:
        code = pytest.main(pytest_args)
    except SystemExit as exc:
        code = exc.code
    return (code == 0)
```

要点：

- `label="fast"` 默认只跑非慢测试（追加 `-m not slow`）；`label="full"` 跑全部。
- `--pyargs` 让 pytest 把 `'scipy.special'` 当作已安装的包来定位测试，而不是当作文件路径——这就是为什么 `special.test()` 即使在任意目录调用都能找到测试。
- `parallel` 参数在有 `pytest-xdist` 时启用 `-n` 并行。
- 返回值是布尔：`True` = 全部通过。

> `PytestTester` 不是 `special` 独有，SciPy **每个**子模块都通过同样的三行暴露一个 `test()`。它的价值是「无需记 pytest 路径参数，一个方法跑本模块测试」。

测试文件清单见 [`tests/meson.build:1-68`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/tests/meson.build#L1-L68)：开头是一长串 `test_*.py`，末尾通过 `install_tag: 'tests'` 安装，参考数据 `data/__init__.py` 单独安装。

#### 4.3.4 代码实践：跑一小批测试

1. **实践目标**：用两种方式各跑一小批 `special` 测试，并对比行为。
2. **操作步骤**：
   - 方式 A（模块入口）：
     ```bash
     python -c "import scipy.special as sc; print('PASS' if sc.test('fast', verbose=1) else 'FAIL')"
     ```
   - 方式 B（直接 pytest，更可控）：
     ```bash
     pytest --pyargs scipy.special.tests.test_logsumexp -v
     ```
3. **需要观察的现象**：方式 A 会跑整个 `scipy.special` 的 fast 测试，耗时较长、用例很多；方式 B 只跑 `test_logsumexp.py` 一个文件，很快。两者最终都打印 pytest 的 `passed/failed` 统计。
4. **预期结果**：在正确安装的环境下，两个命令都应报告「全部通过」（`0 failed`）。具体用例数随版本变化，**待本地验证**。
5. **提示**：若想只跑名字含 `gamma` 的用例，可加 `-k gamma`；若想看慢测试，用 `sc.test('full')`。

#### 4.3.5 小练习与答案

**练习 1**：`special.test()` 和 `pytest scipy/special/tests/` 有什么本质区别？
> **答案**：`special.test()` 内部用 `--pyargs scipy.special`，针对**已安装到 site-packages** 的包定位测试；直接 `pytest <源码路径>` 针对的是**源码树**里的测试文件。前者验证你 `pip install` 的那个版本，后者验证当前源码（可能未重新编译）。

**练习 2**：为什么 `__init__.py` 最后要 `del PytestTester`？
> **答案**：`PytestTester` 只是借用来造一个 `test` 实例的工具类，不应出现在 `scipy.special` 的公共命名空间里。`del` 它可以避免 `from scipy.special import *` 时把类名也带出去，保持 API 整洁。

---

## 5. 综合实践

设计一个贯穿「构建 → 加载 → 测试」的小任务，把本讲三节串起来。

**任务**：编制一份「`scipy.special` 运行时档案」，回答三个问题。

1. **构建侧（读 `meson.build`）**：列出本模块的 7 个扩展模块名，并标注每个模块「主要承担什么」（提示：`_ufuncs`=主体 ufunc、`_ufuncs_cxx`=Boost 函数、`_special_ufuncs`/`_gufuncs`=新 C++ 注册路径、`cython_special`=Cython API、`_specfun`/`_ellip_harm_2`=专项内核）。
2. **加载侧（运行 Python）**：执行
   ```bash
   python -c "import scipy.special as sc; print(sc.jv(0, 1.0))"
   ```
   预期 \( J_0(1) \approx 0.7651976866 \)。再用
   ```bash
   python -c "import scipy.special as sc; print(sc._ufuncs.__file__)"
   ```
   记录 `_ufuncs` 共享库的实际路径，并说明这个路径证明了「jv 来自编译模块」。
3. **测试侧**：挑一个体量小的测试文件（如 `test_logsumexp.py`）用 pytest 跑通，记录通过用例数；再尝试 `sc.test('fast')` 的感觉（可用 `Ctrl-C` 提前终止，只看它能否正常启动）。

**验收标准**：

- 能准确说出 7 个扩展模块名及其分工。
- 能解释 `sc.jv(0, 1.0)` 为什么返回约 `0.7652`，且其背后是 `_ufuncs...so` 里的 ufunc。
- 能区分 `special.test()`（`--pyargs`、跑已安装包）与直接 `pytest`（跑源码树）的差异。

> 若 `sc.jv(0, 1.0)` 报 `ModuleNotFoundError: No module named 'scipy.special._ufuncs'`，说明 SciPy 安装不完整或未编译——这正是本讲的反面教材：编译型子模块缺了 `.so` 就无法运行。

## 6. 本讲小结

- `scipy.special` 是**编译型**子模块：它的函数体住在 Cython/C/C++ 编译出的共享库里，而非纯 `.py`。
- [`meson.build`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/meson.build) 是构建总图，定义了 7 个扩展模块、1 个静态库（`cdflib`）、1 个代码生成步骤（`custom_target` 跑 `_generate_pyx.py`）和 2 个 Cython `generator`。
- 构建流水线是：`subdir('special')` → 代码生成（`functions.json` → `_ufuncs.pyx`）→ Cython 翻译（`.pyx` → `.c`/`.cpp`）→ 编译链接成 `.so` → 安装 `.py`。
- `import scipy.special` 时，[`__init__.py:788-789`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L788-L789) 用 `from . import _ufuncs` 触发 `.so` 加载，再 `from ._ufuncs import *` 把 ufunc 转发进命名空间。
- 测试有两套等价入口：`special.test()`（薄封装，走 `--pyargs`）和直接 `pytest`（跑源码树），测试文件清单见 [`tests/meson.build`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/tests/meson.build)。
- `special.test` 是 `PytestTester` 的一个**实例**而非类，由 [`__init__.py:843-845`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L843-L845) 三行创建，`del PytestTester` 保持命名空间整洁。

## 7. 下一步学习建议

本讲建立了「从源码到可运行」的心智模型。建议接下来：

- **继续 u1-l4**：精读 [`__init__.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py) 的导入与 `__all__` 组装，弄清 `logsumexp`、`spherical_jn`、`lambertw`、`ellip_harm` 分别来自哪个子模块。
- **进入 u3 单元（代码生成管线）**：本讲只是「点到」`custom_target` 跑 `_generate_pyx.py`；u3 会拆解 `functions.json` 如何声明函数、`_generate_pyx.py` 如何据此生成 `_ufuncs.pyx`——这是本模块真正的「工程心脏」。
- **想动手从源码构建**：阅读 SciPy 顶层文档的「Building from source」，结合本讲的 `meson.build`，亲自跑一次 `meson setup && meson compile`，观察 `.so` 的产出位置。
- **想深入测试方法**：先跳到 u9-l1，看 [`scipy/_lib/_testutils.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/_lib/_testutils.py) 里的 `FuncData` 如何用 `tests/data/*.npz` 校验函数值。
