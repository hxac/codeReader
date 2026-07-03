# 目录结构、构建方式与 C/Cython 后端入口

## 1. 本讲目标

在上一篇里，我们建立了 `scipy.optimize` 的能力全景，也认识了三个跨求解器复用的公共对象（`OptimizeResult`、`OptimizeWarning`、`show_options`）。本讲不讨论任何具体算法，而是回答一个更底层的问题：**这一整箱子算法，在硬盘上到底是怎么摆放、又是怎么被编译成可运行代码的？**

学完本讲，你应当能够：

1. 打开 `scipy/optimize` 目录，一眼分清哪些是**纯 Python 逻辑**、哪些是**编译扩展**、哪些是**C/C++/Cython 源码**。
2. 读懂 `meson.build`，看懂每一条 `py3.extension_module(...)` 声明：它编译哪些源文件、产出哪个可导入模块。
3. 理解 `src/` 下那些 `.c` 文件的来历（它们大多是经典 Fortran 数值库的 C 移植），并建立**「算法逻辑在 `.py`、性能核心在 C/Cython」**的心智模型。
4. 自己画出一张「扩展名 → 算法 → Python 包装文件」对照表，为后续每一篇算法讲义锚定底层入口。

## 2. 前置知识

- **Python 包与模块导入**：知道 `from ._foo import bar` 表示从同一包内导入；知道 `import numpy as np` 是怎么回事。
- **编译扩展是什么**：普通 `.py` 文件由解释器逐行执行；而 C/C++/Cython 写的源码需要先用编译器「翻译」成机器码，产出一个 `.so`（Linux）或 `.pyd`（Windows）文件，Python 才能像导入普通模块一样 `import` 它。这一步翻译由**构建系统**完成。
- **Meson 构建系统**：SciPy 用 [Meson](https://mesonbuild.com/) 作为构建工具，配置写在名为 `meson.build` 的文件里。你不需要会写 Meson，本讲只需要你「读」它——把它当成一张「编译清单」即可。关键词 `py3.extension_module(...)` 表示「声明一个 Python 扩展模块」。
- **经典数值库**：`optimize` 的很多算法历史久远，最早用 Fortran 写成（如 MINPACK、L-BFGS-B、SLSQP），后来被移植成 C。看到这些名字时，把它们理解为「成熟的、经过几十年验证的数值内核」即可。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 / 目录 | 作用 |
| --- | --- |
| [`__init__.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/__init__.py) | 包入口。靠末尾的 `import` 段把各个子模块的公共 API「汇聚」到 `scipy.optimize` 名字空间。 |
| [`meson.build`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/meson.build) | 构建清单。声明全部编译扩展（`_minpack`、`_lbfgsb`、`_slsqplib`、`_zeros` 等）以及纯 Python 文件的安装。 |
| [`src/minpack.c`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/src/minpack.c) | MINPACK 的 C 移植：Levenberg-Marquardt 最小二乘、Powell hybrid 求根的数值内核。 |
| [`src/lbfgsb.c`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/src/lbfgsb.c) | L-BFGS-B（有限记忆拟牛顿 + 边界约束）的 C 内核。 |
| `src/`（其余） | `slsqp.c`（SLSQP）、`nnls.c`（非负最小二乘）、各 `.h` 头文件。 |
| `cython_optimize/`、`_direct/`、`tnc/`、`Zeros/`、`_highspy/` 等 | 子目录，分别承载 Cython 求根 API、DIRECT 全局优化、TNC 截断牛顿、一维求根 C 实现、HiGHS 线性规划求解器。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 目录三层分工**：纯 Python、子包、C/Cython 源码各司其职。
- **4.2 `meson.build` 的 `extension_module` 声明**：编译扩展是怎么被「登记」出来的。
- **4.3 `src/` 下的 C 后端**：算法来源与命名约定。

### 4.1 目录三层分工：纯 Python、子包、C/Cython 源码

#### 4.1.1 概念说明

打开 `scipy/optimize`，你会看到三类东西混在一起，初学者容易懵。其实它们扮演三种完全不同的角色：

1. **纯 Python 文件（算法逻辑层）**：形如 `_optimize.py`、`_minpack_py.py`、`_lbfgsb_py.py`。它们负责**用户接口**（参数校验、结果封装成 `OptimizeResult`、分发到不同方法），以及那些用纯 Python 就够快的算法（如 Nelder-Mead、BFGS）。
2. **子包/子目录（按主题归类的代码与源码）**：如 `_lsq/`（最小二乘）、`_trustregion_constr/`（约束信赖域）、`_direct/`（DIRECT 的 C 源码）、`cython_optimize/`（Cython 求根 API）。每个子目录往往自带一个 `meson.build`，由顶层 `meson.build` 通过 `subdir(...)` 串起来。
3. **C/C++/Cython 源码（性能核心层）**：集中在 `src/` 下，或散落在 `_direct/`、`tnc/`、`Zeros/`、`cython_optimize/` 里。这些是要被编译的「重活儿」内核。

一句话总结心智模型：**算法逻辑在 `.py`、性能核心在 C/Cython**。Python 层负责「好不好用」，C/Cython 层负责「快不快、准不准」。

#### 4.1.2 核心流程

一个典型算法的运行链路如下（以 L-BFGS-B 为例）：

```text
用户调用 minimize(method='L-BFGS-B', ...)
        │
        ▼
_minimize.py 分发 ──► _lbfgsb_py._minimize_lbfgsb(...)   ← 纯 Python：校验、封装
        │
        ▼
_lbfgsb_py.py 调用编译扩展 _lbfgsb.setulb / _lbfgsb.moduleTNC-like 入口
        │
        ▼
_lbfgsb 扩展（.so）── 编译自 _lbfgsbmodule.c + src/lbfgsb.c   ← C 内核：两环递归、边界投影
        │
        ▼
返回结果，由 Python 层组装成 OptimizeResult
```

注意那个反复出现的「下划线开头」命名约定：

- `_lbfgsb_py.py`：纯 Python 包装（**p**ython）。
- `_lbfgsb`：编译扩展（C）。
- `_lbfgsbmodule.c`：扩展的「胶水层」，负责把 Python 对象转成 C 数组、再把 C 结果转回 Python。

`_xxx_py.py`（Python）↔ `_xxx` / `_xxxlib`（编译扩展）↔ `src/xxx.c`（C 内核）这个三件套，是阅读后续算法讲义时要反复对照的结构。

#### 4.1.3 源码精读

包入口 `__init__.py` 的末尾有一段**导入汇聚段**，它决定了 `scipy.optimize` 对外暴露哪些名字。读懂这段，就等于看到了整个子包的「公开目录」：

[__init__.py:422-448](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/__init__.py#L422-L448) —— 这段从各子模块把公共 API 拉进名字空间。注意其中两类来源：
- 纯 Python 模块：如 `from ._optimize import *`、`from ._minpack_py import *`、`from ._lbfgsb_py import fmin_l_bfgs_b`。
- **直接来自编译扩展**：如 `from ._lsap import linear_sum_assignment`——`_lsap` 是 C++ 扩展，没有独立的 `_lsap_py.py`，扩展本身就充当了 Python 可导入模块。

紧随其后是一段「兼容旧命名空间」的导入：

[__init__.py:450-454](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/__init__.py#L450-L454) —— 注释写着 `Deprecated namespaces, to be removed in v2.0.0`。这些形如 `minpack`、`lbfgsb`、`slsqp`、`zeros` 的子模块名是老版本遗留的访问路径（例如 `scipy.optimize.minpack.leastsq`），官方计划在 2.0.0 移除。阅读时把它们当作「历史包袱」即可，新代码不要再用。

#### 4.1.4 代码实践

**实践目标**：亲手验证「编译扩展是真实存在的、可导入的 Python 模块」，并区分它和纯 Python 模块。

**操作步骤**：

1. 在装好 SciPy 的环境里运行：

   ```python
   import scipy.optimize as opt
   import _lbfgsb   # 多半失败，见下
   ```

2. 改成正确的相对路径导入观察：

   ```python
   import scipy.optimize._lbfgsb as ext      # 编译扩展（C）
   import scipy.optimize._lbfgsb_py as wrap  # 纯 Python 包装
   print(type(ext), type(wrap))
   print(ext.__file__)   # 指向一个 .so 文件
   print(wrap.__file__)  # 指向一个 .py 文件
   ```

**需要观察的现象**：
- `ext.__file__` 以 `.so`（或 `.pyd`）结尾——证明它是编译产物。
- `wrap.__file__` 以 `.py` 结尾——证明它是源码。

**预期结果**：两个模块一个来自磁盘上的 `.so`、一个来自 `.py`，正好对应「性能核心 vs 算法逻辑」两层。如果你只是看官方文档 `scipy.optimize`，是看不到这种区别的。

> 如果你的环境里 `import scipy.optimize._lbfgsb` 报错（例如打包精简了私有模块），可改成 `import scipy.optimize; print(opt.fmin_l_bfgs_b.__module__)` 来确认它的实现模块名。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_lsap` 没有对应的 `_lsap_py.py`，而 `_lbfgsb` 却有 `_lbfgsb_py.py`？

> **参考答案**：`_lsap` 扩展本身已经导出了可直接使用的 Python 可调用对象 `linear_sum_assignment`，且其输入输出足够「Pythonic」，不需要额外包装层；而 `_lbfgsb` 的 C 接口是面向「逐步迭代、回调求函数值」的低层风格（需要反向通信 reverse-communication），必须由 `_lbfgsb_py.py` 这种 Python 层把它包装成 `minimize(method='L-BFGS-B')` 那样一次性的高层接口。是否需要 `_py` 包装，取决于 C 内核接口的抽象层级。

**练习 2**：`__init__.py` 里 `from ._optimize import *` 这种 `*` 导入，依赖什么来控制「导出哪些名字」？

> **参考答案**：依赖各模块内部定义的 `__all__` 列表（若没有，则导出所有不以下划线开头的名字）。`__init__.py` 自己也在 [__init__.py:456](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/__init__.py#L456) 用 `__all__ = [s for s in dir() if not s.startswith('_')]` 汇总出整个子包的公开 API。

---

### 4.2 `meson.build`：编译扩展是怎么被声明的

#### 4.2.1 概念说明

`meson.build` 是这个子包的「编译清单」。每一条

```python
py3.extension_module('名字',
  ['源文件1', '源文件2', ...],
  dependencies: ...,
  install: true,
  subdir: 'scipy/optimize'
)
```

都在告诉构建系统：**请把这些源文件编译成一个名为 `名字` 的 Python 扩展，安装到 `scipy/optimize/` 下**。编译成功后，Python 里就能 `from scipy.optimize import 名字`（注意 `名字` 是私有的，实际由 `__init__.py` 中转）。

除了 `extension_module`，清单里还有两类声明：
- `static_library(...)`：静态库，不直接被 Python 导入，而是**链接进**某个扩展（复用代码）。
- `py3.install_sources([...])`：把纯 Python 文件原样复制到安装目录（不编译）。

#### 4.2.2 核心流程

构建期的数据流：

```text
meson.build 的 extension_module 声明
        │  （声明：源文件 + 依赖）
        ▼
Meson 调用 C/Cython/Pythran 编译器
        │
        ▼
产出 *.so 扩展文件，安装到 scipy/optimize/
        │
        ▼
运行期：__init__.py 通过 from ._xxx import ... 把扩展的符号拉进名字空间
```

一个值得记住的细节：**Cython 与 Pythran 也是在这里被编译的**。例如 `_moduleTNC` 用 `cython_gen.process('tnc/_moduleTNC.pyx')` 先把 `.pyx` 翻译成 C，再编译；`_group_columns` 则根据构建选项在 Cython 和 Pythran 之间二选一。

#### 4.2.3 源码精读

先看一个「教科书式」的 C 扩展声明——`_minpack`：

[meson.build:17-26](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/meson.build#L17-L26) —— 把 `_minpackmodule.c`（胶水层）和 `src/minpack.c`（算法内核）一起编译成 `_minpack` 扩展。`include_directories: 'src/'` 让胶水层能 `#include "minpack.h"`。

再看一个带「静态库 + 依赖」的更复杂例子——`_lbfgsb`：

[meson.build:67-77](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/meson.build#L67-L77) —— 注意 `dependencies: [lapack_dep, np_dep]`：L-BFGS-B 内部要做线性代数（解线性方程组、矩阵向量积），因此链接了 LAPACK 与 NumPy。

「静态库被链接进扩展」的例子——一维求根：

[meson.build:46-55](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/meson.build#L46-L55) 先把 `Zeros/bisect.c`、`Zeros/brentq.c` 等编成静态库 `rootfind`；

[meson.build:57-64](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/meson.build#L57-L64) 再让 `_zeros` 扩展 `link_with: rootfind`。这样多个求根算法共享同一套底层工具，又只对外暴露一个 `_zeros` 模块。

「Cython 源」的例子——截断牛顿：

[meson.build:79-89](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/meson.build#L79-L89) —— `cython_gen.process('tnc/_moduleTNC.pyx')` 在编译期把 Cython 翻成 C，连同 `tnc/tnc.c` 一起产出 `_moduleTNC` 扩展。

「条件二选一」的例子——稀疏雅可比列分组：

[meson.build:100-118](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/meson.build#L100-L118) —— `if use_pythran` 分支用 Pythran 编译 `_group_columns.py`，`else` 分支用 Cython 编译 `_group_columns.pyx`。同一个对外名字，两种后端实现，由构建配置决定。

最后，「纯 Python 文件原样安装」的声明：

[meson.build:162-225](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/meson.build#L162-L225) —— 这一长串 `py3.install_sources([...])` 列出了所有要被「复制」（而非编译）到安装目录的 `.py` 文件。这恰好是「算法逻辑层」的完整名单。夹在中间的：

[meson.build:153-159](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/meson.build#L153-L159) —— 一串 `subdir(...)` 把各子目录自己的 `meson.build` 递归纳入构建（如 `subdir('cython_optimize')`、`subdir('_highspy')`）。

> 一个容易踩坑的点：存在**两个**名为 `_zeros` 的扩展！顶层 [_zeros](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/meson.build#L57-L64) 由 `_zerosmodule.c` + C 静态库 `rootfind` 编译，供纯 Python 的 `_zeros_py.py`（`root_scalar`/`brentq` 等）使用；而 [cython_optimize/meson.build:24-31](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/cython_optimize/meson.build#L24-L31) 里还有一个 `_zeros`，由 `_zeros.pyx.in`（Cython）编译，专门给在 Cython/nogil 代码里直接调用求根的用户使用。二者同名但分属不同子包（`scipy.optimize._zeros` 与 `scipy.optimize.cython_optimize._zeros`），不要混淆。

#### 4.2.4 代码实践

**实践目标**：把 `meson.build` 通读一遍，亲手整理出「扩展名 → 主要源文件」的清单。

**操作步骤**：

1. 打开本目录下的 `meson.build`。
2. 用编辑器搜索 `py3.extension_module(`，每命中一次就记录三项：模块名、列出的源文件、`dependencies` 里是否出现了 `lapack_dep`。
3. 再搜索 `static_library(`，记下静态库名及其成员文件（它们最终会被某个扩展 `link_with`）。
4. 把结果填进下面这张表（第一行已示范）。

| 编译扩展名 | 主要源文件 | 关键依赖 | 静态库（若有） |
| --- | --- | --- | --- |
| `_minpack` | `_minpackmodule.c`, `src/minpack.c` | `np_dep`, `ccallback_dep` | 无 |
| `_lbfgsb` | `_lbfgsbmodule.c`, `src/lbfgsb.c` | `lapack_dep`, `np_dep` | 无 |
| `_zeros`（顶层） | `_zerosmodule.c` | `np_dep` | `rootfind` |
| `_slsqplib` | `_slsqpmodule.c`, `src/slsqp.c`, `src/nnls.c` | `lapack_dep`, `np_dep` | 无 |
| … | … | … | … |

**需要观察的现象**：哪些扩展需要 LAPACK（意味着内部解线性方程组），哪些只需要 NumPy（纯函数求值类）。

**预期结果**：你会得到约 9 条顶层扩展 + 若干来自子目录（`_highspy` 的 HiGHS、`cython_optimize` 的 `_zeros`）的扩展。凡 `dependencies` 含 `lapack_dep` 的，都是后续线性代数密集型算法的内核。

> 待本地验证：如果你在本机用 `python -c "import scipy.optimize"` 后查看 `site-packages/scipy/optimize/` 目录，应能看到与上述扩展名一一对应的 `.so` 文件。

#### 4.2.5 小练习与答案

**练习 1**：`_slsqplib` 为什么把 `src/nnls.c` 也一起编译进去（见 [meson.build:141-151](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/meson.build#L141-L151)）？

> **参考答案**：SLSQP 在每个迭代步要求解一个二次规划（QP）子问题，而其子问题求解依赖「非负最小二乘（NNLS）」。把 `nnls.c` 直接编进 `_slsqplib`，让 SLSQP 内核能就地调用 NNLS，避免跨模块开销。同时，`_nnls.py` 通过 `from ._slsqplib import nnls as _nnls` 复用同一个编译好的 NNLS 例程对外提供 `scipy.optimize.nnls`——一份 C 代码，两个用途。

**练习 2**：`subdir('_highspy')` 引入的 HiGHS 是什么？为什么它不出现在 `py3.install_sources` 的纯 Python 列表里？

> **参考答案**：HiGHS 是一个高性能的线性规划/混合整数规划 C++ 求解器，SciPy 通过 Meson 的 subproject 把它作为子项目静态编译进来（见 `_highspy/meson.build` 用 `subproject('highs', ...)`）。它不是纯 Python，所以不在 `install_sources` 列表里；它的 C++ 接口被 `_linprog_highs.py` 和 `_milp.py` 包装，是 `linprog`（默认方法）和 `milp` 的底层引擎。

---

### 4.3 `src/` 下的 C 后端：算法来源与命名约定

#### 4.3.1 概念说明

`src/` 是「经典数值算法内核」的集中营。这一目录的文件其实不多：

```text
src/
├── blaslapack_declarations.h   # BLAS/LAPACK 函数原型声明
├── lbfgsb.c / lbfgsb.h         # L-BFGS-B 内核
├── minpack.c / minpack.h       # MINPACK 内核（最小二乘 + 求根）
├── nnls.c / nnls.h             # 非负最小二乘
└── slsqp.c / slsqp.h           # SLSQP 内核
```

它们大多是**几十年前 Fortran 数值库的 C 移植**：`minpack.c` 来自明尼苏达大学 Argonne 国家实验室的 MINPACK；`lbfgsb.c` 来自 L-BFGS-B 原作者的 Fortran 代码；`slsqp.c` 来自 Dieter Kraft 的 SLSQP。把它们放在 `src/`，与「胶水层」`_xxxmodule.c` 分离，是一种常见的工程组织：**算法内核尽量保持与上游一致，便于追踪与升级；胶水层单独维护，适配 Python。**

> 命名提示：`minpack.c` 里函数名是**全大写**（`LMDIF`、`LMDER`、`HYBRD`、`HYBRJ`、`CHKDER`），这是 Fortran 子例程的传统命名；`lbfgsb.c` 则用小写 + 下划线（`mainlb`、`active`、`bmv`），更像现代 C 风格。看到全大写函数名，基本可以判定「这是 Fortran 移植」。

#### 4.3.2 核心流程

C 内核与 Python 之间的协作模式（以 L-BFGS-B 为典型）用的是**反向通信（reverse communication）**：

```text
C 内核 mainlb(...) 计算到「需要目标函数值 f 和梯度 g」时
        │  不直接调用 Python 函数，而是
        ▼
返回一个状态码（如 task = FG，表示「请给我 f、g」）
        │
        ▼
胶水层 _lbfgsbmodule.c 把控制权交回 Python
        │
        ▼
Python 层用用户提供的 fun/jac 在当前 x 处算出 f、g，写回 C 数组
        │
        ▼
再次调用 C 内核继续迭代 …… 循环直至 CONVERGENCE / STOP
```

这种「C 不调用 Python、而是请 Python 喂数据」的模式，避免了在 C 里持有 Python 对象引用带来的性能与线程安全问题，是 SciPy 中 Fortran 移植内核的标准玩法。状态码就是两边的「对话协议」。

#### 4.3.3 源码精读

`src/minpack.c` 开头先声明内部静态函数，再导出一组大写命名的入口：

[src/minpack.c:1-17](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/src/minpack.c#L1-L17) —— 顶部 `#include "minpack.h"` 之后是一串 `static void dogleg/enorm/fdjac1/fdjac2/lmpar/qrfac/qrsolv/...`，这些是 MINPACK 内部的「私有」工具（QR 分解、雅可比有限差分、信赖域步长等）。

[src/minpack.c:27](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/src/minpack.c#L27) —— 注释明确写出对外导出的函数：`CHKDER, HYBRD, HYBRJ, LMDIF, LMDER, LMSTR`。其中：
- `LMDIF` / `LMDER`：Levenberg-Marquardt 最小二乘（无导数 / 有导数），是 `leastsq`、`curve_fit` 的内核。
- `HYBRD` / `HYBRJ`：Powell hybrid 非线性方程求根，是 `fsolve`、`root(method='hybr')` 的内核。

`src/lbfgsb.c` 开头则是一组**状态码枚举**——这正是上一节「对话协议」的字面体现：

[src/lbfgsb.c:4-14](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/src/lbfgsb.c#L4-L14) —— `enum Status` 定义了 `START/NEW_X/RESTART/FG/CONVERGENCE/STOP/...`。`FG = 3` 就是反向通信里「请给我函数值与梯度」的信号。

[src/lbfgsb.c:55-62](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/src/lbfgsb.c#L55-L62) —— `static void mainlb(...)` 是 L-BFGS-B 的主循环函数，参数列表很长（`ws/wy/sy/ss/wt/wn/...`），那些 `ws, wy, sy, ss` 正是存放有限记忆历史向量 `s` 和 `y` 的工作数组，对应后续 L-BFGS-B 讲义里的「两环递归」。

#### 4.3.4 代码实践

**实践目标**：从源码层面确认「状态码 = C 与 Python 的对话协议」。

**操作步骤**：

1. 打开 [`src/lbfgsb.c`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/src/lbfgsb.c)，阅读 [4-50 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/src/lbfgsb.c#L4-L50) 的两个枚举 `Status` 与 `StatusMsg`。
2. 在仓库里搜索这些状态码如何被 Python 侧使用：

   ```bash
   grep -n "FG_START\|FG_LNSRCH\|CONV_GRAD\|task" _lbfgsb_py.py | head
   ```

3. 对照阅读 `_lbfgsb_py.py` 中 `_minimize_lbfgsb` 的主循环：你会看到一个「根据 task 决定是计算 f/g、还是收敛退出」的分支结构。

**需要观察的现象**：Python 侧的循环条件和 C 侧的 `enum StatusMsg`（`FG_START=301`、`CONV_GRAD=401` 等）一一对应。

**预期结果**：你能指着某一行 Python 代码说「这里处理的就是 C 返回 `FG`（需要函数值）的情况」，从而真正理解反向通信的运行方式。

> 待本地验证：若想动态观察，可在 `_lbfgsb_py.py` 的主循环里临时加一行 `print(task)`（仅作学习用途，勿提交），跑一次 `minimize(rosen, x0, method='L-BFGS-B')`，观察 `task` 在 `FG`/`CONVERGENCE` 之间切换。

#### 4.3.5 小练习与答案

**练习 1**：`src/minpack.c` 里函数名全大写（`LMDIF` 等），这暗示了什么？为什么 SciPy 要保留这种命名而不改成 PEP8 风格？

> **参考答案**：全大写是 Fortran 子例程的命名传统，说明这些是 Fortran 原作的直接 C 移植。保留命名是为了与原始文献、上游补丁、以及社区里大量讨论这些算法的资料保持一致，便于核对算法正确性与追踪 bug 来源。这是一种「尊重上游」的工程惯例。

**练习 2**：如果一个新算法既不需要 LAPACK、也不需要反向通信（纯函数求值，一次返回），它最可能以什么形式加入 `optimize`？

> **参考答案**：很可能直接用纯 Python 实现（放进某个 `_xxx.py`，并加入 `meson.build` 的 `install_sources` 列表与 `__init__.py` 的导入段），而不必动 `src/` 或新建编译扩展。只有当性能成为瓶颈时，才会下沉到 C/Cython——这正是 `_group_columns`（先 Python，后 Cython/Pythran 加速）走过的路。

---

## 5. 综合实践

本讲的核心实践任务是**亲手绘制一张「扩展名 → 算法 → Python 包装文件」对照表**，把前面三个最小模块串起来。

**任务**：通读 [`meson.build`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/meson.build)（含 `subdir` 引入的 `cython_optimize/meson.build`、`_highspy/meson.build`），结合 [`__init__.py` 的导入段](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/__init__.py#L422-L448)，整理出下表（已给出参考答案，请逐行在源码中找到依据）：

| 编译扩展名 | 算法来源 / 用途 | 主要源文件 | Python 包装文件 |
| --- | --- | --- | --- |
| `_minpack` | MINPACK：Levenberg-Marquardt 最小二乘、Powell hybrid 求根 | `_minpackmodule.c` + `src/minpack.c` | `_minpack_py.py`（`leastsq`、`curve_fit`、`fsolve`） |
| `_lbfgsb` | L-BFGS-B：有限记忆拟牛顿 + 边界 | `_lbfgsbmodule.c` + `src/lbfgsb.c` | `_lbfgsb_py.py`（`fmin_l_bfgs_b`） |
| `_slsqplib` | SLSQP：序列最小二乘 QP（含 NNLS 子求解） | `_slsqpmodule.c` + `src/slsqp.c` + `src/nnls.c` | `_slsqp_py.py`（`fmin_slsqp`）；NNLS 由 `_nnls.py` 转发 |
| `_zeros`（顶层） | 一维求根：bisect/brentq/brenth/ridder | `_zerosmodule.c` + 静态库 `rootfind` | `_zeros_py.py`（`brentq`、`ridder` 等） |
| `_moduleTNC` | 截断牛顿 TNC | `tnc/_moduleTNC.pyx`（Cython）+ `tnc/tnc.c` | `_tnc.py`（`fmin_tnc`） |
| `_direct` | DIRECT：分割矩形全局优化 | `_direct/direct_wrap.c` + `_direct/DIRect.c` 等 | `_direct_py.py`（`direct`） |
| `_lsap` | 线性指派：Jonker-Volgenant | `_lsapmodule.c` + 静态库 `rectangular_lsap`（C++） | 无独立 `_py`；`__init__.py` 直接导入 `linear_sum_assignment` |
| `_bglu_dense` | 修正单纯形的基矩阵 LU 更新（BGLU） | `_bglu_dense.pyx`（Cython） | `_linprog_rs.py`（`linprog(method='revised simplex')`） |
| `_group_columns` | 稀疏雅可比列分组 | `_group_columns.pyx`（Cython）或 `_group_columns.py`（Pythran） | `_numdiff.py`（`approx_derivative`） |
| `_pava_pybind` | PAVA 保序回归 | `_pava/pava_pybind.cpp`（pybind11） | `_isotonic.py`（`isotonic_regression`） |
| `_zeros`（cython_optimize/） | 供 Cython/nogil 直接调用的标量求根 | `cython_optimize/_zeros.pyx.in`（Cython） | `cython_optimize/` 子包（面向 Cython 用户） |
| `_highspy`（子项目） | HiGHS：线性规划 / MILP C++ 求解器 | HiGHS C++ 源（meson subproject） | `_linprog_highs.py`（`linprog` 默认方法）、`_milp.py`（`milp`） |

**完成后请自检**：
1. 能否解释「为什么 `_lsap` 没有 `_py` 包装」？（答：扩展本身已导出 Pythonic 的 `linear_sum_assignment`。）
2. 能否指出哪两个扩展同名却分属不同子包？（答：两个 `_zeros`，一个在顶层、一个在 `cython_optimize/`。）
3. 哪些扩展依赖 LAPACK，为什么？（答：`_lbfgsb`、`_slsqplib` 等，因为内部要做线性求解。）

## 6. 本讲小结

- `scipy/optimize` 的代码分三层：**纯 Python（算法逻辑/接口）**、**子包（按主题归类）**、**C/C++/Cython 源码（性能核心）**，心智模型是「算法逻辑在 `.py`、性能核心在 C/Cython」。
- `meson.build` 是编译清单：`py3.extension_module(...)` 声明扩展、`static_library(...)` 声明被链接的静态库、`py3.install_sources([...])` 原样安装纯 Python 文件、`subdir(...)` 递归纳入子目录的构建。
- 典型的「三件套」命名约定：`_xxx_py.py`（Python 包装）↔ `_xxx` / `_xxxlib`（编译扩展）↔ `_xxxmodule.c` + `src/xxx.c`（胶水层 + C 内核）。
- `src/` 下的 `.c` 多为经典 Fortran 数值库（MINPACK、L-BFGS-B、SLSQP、NNLS）的 C 移植；全大写函数名是 Fortran 命名传统的痕迹。
- C 内核与 Python 之间常用**反向通信**协作：C 不调用 Python，而是返回状态码（如 `FG`）请 Python 喂回函数值/梯度，再继续迭代。
- `__init__.py` 末尾的导入段把各子模块（含编译扩展）的公共 API 汇聚成 `scipy.optimize` 的对外名字空间；其中还保留了一段待在 2.0.0 移除的废弃命名空间。

## 7. 下一步学习建议

本讲建立的是「地图」，后续讲义会带你逐层深入：

- 想看「Python 包装如何把高层 API 翻译成对求解器的调用」，请接着学 **u1-l3《统一调度入口：minimize 与 minimize_scalar》**，那里会讲 `minimize` 如何根据 `method` 字符串分发到各 `_minimize_*`。
- 想深入某个具体 C 后端的运行细节，可以在学完对应算法讲义后，回头对照 `src/` 内核。例如学完 **u5-l4（L-BFGS-B）** 再读 `src/lbfgsb.c` 的 `mainlb`，学完 **u7-l1（curve_fit/leastsq）** 再读 `src/minpack.c` 的 `LMDIF`。
- 对「Cython 如何被普通用户直接使用」感兴趣，可在学完 **u2-l3（一维求根）** 后跳到 **u10-l3《Cython 加速根查找与 C/Fortran 后端》**，届时 `cython_optimize/` 子包会再次出现。
- 建议把本讲的「扩展名 → 算法 → Python 包装」对照表保存下来，作为后续每一篇算法讲义的快速定位索引。
