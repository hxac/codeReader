# scipy.linalg 项目概览与定位

## 1. 本讲目标

本讲是整个 `scipy.linalg` 学习手册的第一篇。读完本讲，你应该能够：

- 说清楚 `scipy.linalg` 是什么、它在 SciPy 和整个科学计算生态里扮演什么角色。
- 区分 `scipy.linalg` 与 `numpy.linalg` 的功能边界：哪些重叠、哪些互补、同名函数又可能有哪些差异。
- 在本地安装 SciPy，并通过 `import scipy.linalg` 正常调用其中的函数。
- 读懂 `scipy.linalg/__init__.py` 这个「总入口」文件，理解它是如何把几十个函数组织、汇聚并对外暴露的。

本讲不要求你已经懂线性代数的深层算法，只要你会矩阵、向量、求逆、行列式这些最基本的概念即可。

## 2. 前置知识

在开始之前，建议你大致了解以下概念（不熟悉也没关系，下面会顺带解释）：

- **矩阵（matrix）与向量（vector）**：矩阵是排成行和列的数字方块；向量可以看成只有一行或一列的矩阵。
- **线性方程组 \(A x = b\)**：已知系数矩阵 \(A\) 和右端 \(b\)，求未知向量 \(x\)。这是线性代数最经典的问题。
- **行列式（determinant, \(\det A\)）**：把一个方阵映射成一个标量；当 \(\det A = 0\) 时矩阵奇异（不可逆）。
- **范数（norm, \(\lVert \cdot \rVert\)）**：衡量向量或矩阵「大小」的量，例如向量的 2-范数 \(\lVert x \rVert_2 = \sqrt{\sum_i x_i^2}\)。
- **Python 与 NumPy**：SciPy 构建在 NumPy 之上，所有矩阵都用 NumPy 的 `ndarray` 表示。

如果你对 LAPACK / BLAS 这些名词还陌生，本讲只需要知道：它们是几十年沉淀下来的、业界标准的高性能线性代数 Fortran/C 例程库，`scipy.linalg` 的大量函数底层就是调用它们。这部分会在后续讲义（第 7 单元）深入讲解。

## 3. 本讲源码地图

本讲涉及的源码文件很少，但都很关键：

| 文件 | 作用 |
| --- | --- |
| [`__init__.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/__init__.py) | `scipy.linalg` 子包的「总入口」。它用一段长长的文档字符串把所有公开函数按功能分类列出，再用一组星号导入（`from ._xxx import *`）把这些函数从各个实现文件汇聚到 `scipy.linalg` 这个命名空间下。 |
| [`_misc.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_misc.py) | 一些「杂项」公共设施：`norm`（范数）、`bandwidth`（带宽）、`LinAlgWarning`（告警），以及从 NumPy 复用的 `LinAlgError`（异常）。本讲主要用它来观察 scipy 与 numpy 的关系。 |

> 提示：本讲只读这两个文件。`__init__.py` 里被导入的 `_basic.py`、`_decomp*.py`、`_matfuncs.py` 等真正的算法实现，会在后续讲义里逐个拆开讲解。

---

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

1. **scipy.linalg 顶层导出与文档字符串** —— 看懂 `__init__.py` 如何组织并暴露 API。
2. **numpy.linalg 对比说明** —— 理解 scipy 与 numpy 两个线性代数包的关系。

### 4.1 scipy.linalg 顶层导出与文档字符串

#### 4.1.1 概念说明

当你写下 `import scipy.linalg` 时，Python 实际上执行的是 `scipy/linalg/__init__.py` 这个文件。这个文件做了两件事：

1. **写文档**：文件开头有一段几十行的文档字符串（docstring），相当于这个子模块的「目录页」，把全部公开函数按功能分成了 7 大类。
2. **做导入**：文件后半部分用一组 `from ._xxx import *` 语句，把分散在十几个实现文件里的函数「抄」到 `scipy.linalg` 命名空间里，让用户能直接用 `scipy.linalg.solve`、`scipy.linalg.svd` 这样的方式调用。

换句话说，`__init__.py` 本身几乎不包含算法逻辑，它是**汇聚与门面**。理解了它，你就拿到了一份「`scipy.linalg` 到底提供了哪些能力」的全景地图。

#### 4.1.2 核心流程

`__init__.py` 的执行流程可以这样描述（伪代码）：

```text
1. 定义模块文档字符串（按 7 大类列出全部公开函数）
2. for 每个实现子模块 _basic, _decomp, _decomp_lu, ... :
       from ._子模块 import *        # 把它的公开符号导入到当前命名空间
3. 导入已废弃的旧命名空间（decomp, basic, ... 等，仅作兼容，v2.0.0 移除）
4. __all__ = [所有不以 _ 开头的名字]   # 自动推断公开 API
5. 挂上 test = PytestTester(...)      # 让用户能用 scipy.linalg.test() 跑测试
```

文档字符串把函数分成这 7 大类，正好对应了本学习手册后续的几个单元：

| 分类（英文） | 中文 | 代表函数 |
| --- | --- | --- |
| Basics | 基础运算 | `solve`, `inv`, `det`, `norm`, `lstsq` |
| Eigenvalue Problems | 特征值问题 | `eig`, `eigh`, `eigvals` |
| Decompositions | 矩阵分解 | `lu`, `qr`, `svd`, `cholesky`, `schur` |
| Matrix Functions | 矩阵函数 | `expm`, `logm`, `sqrtm`, `funm` |
| Matrix Equation Solvers | 矩阵方程求解 | `solve_sylvester`, `solve_continuous_are` |
| Sketches and Random Projections | 随机投影 | `clarkson_woodruff_transform` |
| Special Matrices | 特殊矩阵构造 | `toeplitz`, `hilbert`, `hadamard`, `dft` |

最后还有一个 **Low-level routines（底层例程）** 小节，暴露 `get_blas_funcs`、`get_lapack_funcs`、`find_best_blas_type`，给需要直接调用 BLAS/LAPACK 的高级用户使用。

#### 4.1.3 源码精读

**① 文档字符串开头：模块定位。**
[`__init__.py:1-17`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/__init__.py#L1-L17) 给出模块名、一句话说明 "Linear algebra functions."，并通过 Sphinx 的 `.. toctree::` 把 `blas`、`lapack`、`cython_blas`、`cython_lapack`、`interpolative` 这几个子模块挂进文档目录。这说明 `scipy.linalg` 除了主命名空间，还包含几个独立的子命名空间。

**② 第一大类：Basics（基础运算）。**
[`__init__.py:29-56`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/__init__.py#L29-L56) 列出 `inv`、`solve`、`det`、`norm`、`lstsq`、`pinv` 等，以及两个异常/告警类型 `LinAlgError`、`LinAlgWarning`。本讲实践任务用到的 `norm` 与 `det` 就在这里。

**③ 全部分类一览。**
其余几大类依次是：特征值 [`__init__.py:58-71`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/__init__.py#L58-L71)、分解 [`__init__.py:73-111`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/__init__.py#L73-L111)、矩阵函数 [`__init__.py:113-132`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/__init__.py#L113-L132)、矩阵方程 [`__init__.py:135-145`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/__init__.py#L135-L145)、随机投影 [`__init__.py:148-154`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/__init__.py#L148-L154)、特殊矩阵 [`__init__.py:156-177`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/__init__.py#L156-L177)、底层例程 [`__init__.py:179-197`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/__init__.py#L179-L197)。每一类都是 `autosummary` 指令，构建文档时自动生成每个函数的说明页。

**④ 星号导入：把实现汇聚到命名空间。**
真正的「搬运」发生在 [`__init__.py:201-221`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/__init__.py#L201-L221)，例如：

```python
from ._misc import *
from ._basic import *
from ._decomp import *
from ._decomp_lu import *
...
from .blas import *
from .lapack import *
```

这一段说明：`solve`/`inv`/`det` 来自 `_basic.py`，`norm`/`bandwidth`/`LinAlgWarning` 来自 `_misc.py`，`lu` 来自 `_decomp_lu.py`，`expm`/`sqrtm` 来自 `_matfuncs.py`，而底层分发函数来自 `blas.py` / `lapack.py`。这也是本手册后续讲义的「地图」——每个 `.py` 文件对应一个主题。

**⑤ `__all__` 的自动生成。**
[`__init__.py:229`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/__init__.py#L229) 只有一行：

```python
__all__ = [s for s in dir() if not s.startswith('_')]
```

它没有手写一份名单，而是**扫描当前命名空间里所有不以 `_` 开头的名字**当作公开 API。这意味着：任何被前面星号导入「漏进来」的公开名字都会自动成为 `scipy.linalg` 的一部分；而以下划线开头的内部实现（如 `_datacopied`）则被排除在外。

**⑥ 测试入口。**
[`__init__.py:232-234`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/__init__.py#L232-L234) 挂上 `test = PytestTester(__name__)`，所以你能在 Python 里直接用 `scipy.linalg.test()` 跑这个子包的全部测试。这一点在最后的「测试体系」讲义（第 9 单元）会详细用到。

#### 4.1.4 代码实践

**实践目标**：动手验证「`scipy.linalg` 是一个汇聚命名空间」，并确认本讲用到的 `norm`、`det` 真的能被导入。

**操作步骤**（这是示例代码，请在自己的 Python 环境运行）：

```python
# 文件名：explore_namespace.py（示例代码，非项目原有文件）
import scipy.linalg as sla

# 1. 看看 scipy.linalg 暴露了多少公开符号
public = [s for s in dir(sla) if not s.startswith('_')]
print("公开符号数量：", len(public))

# 2. 确认 norm、det、solve、expm 都在
for name in ['norm', 'det', 'solve', 'expm', 'svd', 'LinAlgError']:
    print(f"{name:>12}: {'存在' if hasattr(sla, name) else '缺失'}")

# 3. 找出 norm 所属的实现模块（提示：应为 scipy.linalg._misc）
print("norm 定义在：", sla.norm.__module__)
print("det  定义在：", sla.det.__module__)
```

**需要观察的现象**：

- 公开符号数量是一个三位数（说明 API 很大）。
- `norm` 的 `__module__` 应为 `scipy.linalg._misc`，而 `det` 的 `__module__` 应为 `scipy.linalg._basic`，正好对应上面源码精读里星号导入的来源。

**预期结果**：你能用 `hasattr` 确认上述函数都存在，并通过 `.__module__` 反推出它们各自的实现文件。具体打印数值待本地验证（取决于你安装的 SciPy 版本，公开符号总数会有差异）。

#### 4.1.5 小练习与答案

**练习 1**：`__init__.py` 里有这一行 `__all__ = [s for s in dir() if not s.startswith('_')]`。如果某个实现文件在 `__all__` 里**没有**列出某个函数，但函数名不以 `_` 开头，它还会出现在 `scipy.linalg` 命名空间吗？

> **答案**：会。因为这行代码扫描的是当前命名空间（`dir()`），而不是去查每个子模块各自的 `__all__`。只要星号导入把该名字「抄」进了 `scipy.linalg`，它就被当作公开 API。

**练习 2**：`scipy.linalg.test` 是什么？它从哪里来？

> **答案**：它是 `PytestTester(__name__)` 的实例，定义在 [`__init__.py:232-234`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/__init__.py#L232-L234)。调用 `scipy.linalg.test()` 会用 pytest 跑 `scipy/linalg/tests/` 下的全部测试。

**练习 3**：文件末尾还有一段 `from . import (decomp, decomp_cholesky, ... basic, misc, ...)`（[`__init__.py:223-227`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/__init__.py#L223-L227)），注释说「Deprecated namespaces, to be removed in v2.0.0」。这表示什么？

> **答案**：这是为了向后兼容而保留的**旧模块名**。过去用户可能写 `from scipy.linalg.basic import solve`，现在应改为 `from scipy.linalg import solve`。这些旧名字将在 SciPy 2.0 被移除，新代码不应再使用。

---

### 4.2 numpy.linalg 对比说明

#### 4.2.1 概念说明

很多初学者的第一个困惑是：「NumPy 已经有 `numpy.linalg`，为什么 SciPy 还要再做一个 `scipy.linalg`？它们是什么关系？」

要点有三：

1. **互补，不是重复**。NumPy 的线性代数子集相对精简（够日常用），而 SciPy 在其之上提供了**更多、更专业**的函数：矩阵函数（`expm`/`sqrtm`）、各种结构化分解（`ldl`/`schur`/`qz`/`cossin`）、矩阵方程（Riccati/Lyapunov/Sylvester）、特殊矩阵构造（`hilbert`/`toeplitz`/`hadamard`）、随机投影等等，这些在 `numpy.linalg` 里都没有。

2. **同名函数可能不同**。`inv`、`det`、`solve`、`norm`、`eig`、`svd`、`lstsq` 等在两个包里**都存在且同名**，但 SciPy 版本通常：
   - 底层直接调用业界标准的 **LAPACK/BLAS**（更稳定、更快、可控）；
   - 提供更多参数，例如 `check_finite`（是否校验输入含 NaN/Inf）、`overwrite_a`（是否允许原地覆写以省内存）、`lapack_driver`（选择具体驱动）等；
   - 行为细节可能与 NumPy 略有差异。

3. **共享异常类型**。两个包的线性代数异常 `LinAlgError` 其实是**同一个对象**——SciPy 直接复用了 NumPy 的，不另造一个。这点我们马上用源码证明。

社区里有一条长期约定：**做严肃的线性代数计算，优先用 `scipy.linalg`**；NumPy 主要负责数组容器和基础运算。

> ⚠️ 注意：`numpy.matrix`（旧式矩阵类）与 `numpy.linalg` 的部分行为绑定了 `matrix`，而 `scipy.linalg` 一律基于 `ndarray`。新代码请始终使用 `ndarray`。

#### 4.2.2 核心流程

`scipy.linalg` 与 `numpy.linalg` 的关系可以这样画：

```text
   numpy.linalg                         scipy.linalg
   ────────────                         ────────────
   LinAlgError  ◄────── 复用 ────────  LinAlgError（同一个类）
   inv/det/solve/norm/eig/svd          inv/det/solve/norm/eig/svd（更多参数、走 LAPACK）
                                       ＋ expm/logm/sqrtm          （矩阵函数）
                                       ＋ lu/qr/cholesky/schur/qz  （更多分解）
                                       ＋ solve_sylvester / *_are  （矩阵方程）
                                       ＋ toeplitz/hilbert/...     （特殊矩阵）
                                       ＋ get_blas_funcs / get_lapack_funcs（底层）
```

关键机制：
- **异常共享**：`scipy.linalg` 通过 `from numpy.linalg import LinAlgError` 直接拿到 NumPy 的异常类，并额外定义了一个 `LinAlgWarning`。
- **底层不同**：SciPy 的同名函数大多经由 `get_lapack_funcs` / `get_blas_funcs` 动态分发到带 `s/d/c/z` 前缀（单/双精度、实/复）的 LAPACK 例程；NumPy 的实现路径则更接近其自身的 C 内核。这部分细节在第 7 单元展开。

#### 4.2.3 源码精读

**① 文档里明说的「同名函数可能有差异」。**
[`__init__.py:21-26`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/__init__.py#L21-L26) 的 `.. seealso::` 块直接提醒用户：

> `numpy.linalg` for more linear algebra functions. Note that identically named functions from `scipy.linalg` may offer more or slightly differing functionality.

这是官方对「两者关系」最权威的一句话：互补，且同名函数行为可能不同。

**② `LinAlgError` 来自 NumPy。**
[`_misc.py:1-8`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_misc.py#L1-L8) 里写得清清楚楚：

```python
import numpy as np
from numpy.linalg import LinAlgError      # ← 直接复用 NumPy 的异常
...
__all__ = ['LinAlgError', 'LinAlgWarning', 'norm', 'bandwidth']
```

所以 `scipy.linalg.LinAlgError is numpy.linalg.LinAlgError` 结果为 `True`——它们是同一个类。这意味着无论你用哪个包，捕获奇异矩阵等错误都用同一种 `except LinAlgError`。

**③ SciPy 自己的告警 `LinAlgWarning`。**
[`_misc.py:11-16`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_misc.py#L11-L16) 定义了 SciPy 独有的告警类型，用于「算法接近失败条件、可能损失精度」时发出警告——这是 NumPy 那边没有的。

**④ `norm` 的签名比 NumPy 多了 `check_finite`。**
[`_misc.py:19`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_misc.py#L19) 的 `norm(a, ord=None, axis=None, keepdims=False, check_finite=True)` 比 `numpy.linalg.norm` 多了末尾的 `check_finite`。这正是「同名函数行为略有不同」的一个具体例子——后续每篇讲义你都会看到这个参数。

#### 4.2.4 代码实践

**实践目标**：用代码验证「两个包共享 `LinAlgError`」「同名函数都能用，但参数集合不同」。

**操作步骤**（示例代码）：

```python
# 文件名：compare_numpy_scipy.py（示例代码，非项目原有文件）
import numpy as np
import numpy.linalg as nla
import scipy.linalg as sla

A = np.array([[4.0, 7.0],
              [2.0, 6.0]])

# 1. 同名函数都能算行列式和 2-范数，结果数值一致
print("numpy det :", nla.det(A))
print("scipy det :", sla.det(A))
print("numpy norm:", nla.norm(A))
print("scipy norm:", sla.norm(A))

# 2. 验证两个包的 LinAlgError 是同一个类
print("LinAlgError 是同一个类吗：", nla.LinAlgError is sla.LinAlgError)

# 3. 验证 scipy 版 norm 多了 check_finite 参数
import inspect
print("scipy norm 参数：", list(inspect.signature(sla.norm).parameters))
print("numpy norm 参数：", list(inspect.signature(nla.norm).parameters))
```

**需要观察的现象**：

- `det` 与 `norm` 的数值在两个包里完全一致。
- `nla.LinAlgError is sla.LinAlgError` 为 `True`。
- `scipy.norm` 的参数列表里多出 `check_finite`。

**预期结果**：以上三点都应成立（具体数值与 `det` 的符号取决于矩阵本身，待本地运行确认）。这就从「异常类型」「函数可用性」「参数差异」三个角度印证了 4.2.1 的结论。

#### 4.2.5 小练习与答案

**练习 1**：以下哪些函数**只**在 `scipy.linalg` 里有，`numpy.linalg` 里没有？（A）`expm` （B）`inv` （C）`sqrtm` （D）`svd` （E）`solve_sylvester`

> **答案**：A、C、E。`inv` 和 `svd` 两个包都有（同名）；而 `expm`（矩阵指数）、`sqrtm`（矩阵平方根）、`solve_sylvester`（矩阵方程）是 SciPy 独有。你可以用 `hasattr(numpy.linalg, 'expm')` 验证。

**练习 2**：为什么 `scipy.linalg.LinAlgError is numpy.linalg.LinAlgError` 会是 `True`？

> **答案**：因为 [`_misc.py:2`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_misc.py#L2) 直接 `from numpy.linalg import LinAlgError`，SciPy 没有重新定义，只是把同一个类重新暴露出来。

**练习 3**：`scipy.linalg.norm` 多出来的 `check_finite=True` 有什么用？关掉它（`check_finite=False`）会有什么后果？

> **答案**：`check_finite=True` 会在计算前检查输入是否含 `NaN` 或 `±Inf`，如果有就抛错，避免把脏数据喂给底层 LAPACK 导致崩溃或静默错误。关掉它能**略微提速**（省去一次扫描），但若输入确实含非有限值，可能出现崩溃或无意义结果。这是后续讲义里反复出现的「安全 vs 性能」权衡。

---

## 5. 综合实践

把本讲学的「定位、命名空间、与 numpy 的关系」串起来，完成下面这个端到端小任务。

**任务**：安装 SciPy，编写脚本调用 `scipy.linalg` 中的 `norm` 与 `det`，并和 `numpy.linalg` 中同名函数对比。

**步骤 1 —— 安装 SciPy**（在系统 shell，不是 Python 里）：

```bash
# 推荐用 pip 安装预编译 wheel（自带底层 LAPACK/BLAS）
python -m pip install scipy
```

> 如果你是在本仓库源码里学习，也可以参考 `meson.build` 从源码构建，但那是第 3 篇讲义（构建系统）的内容，本篇先用现成 wheel 即可。

**步骤 2 —— 验证导入并完成对比**（示例代码）：

```python
# 文件名：u1_l1_practice.py（示例代码，非项目原有文件）
import numpy as np
import numpy.linalg as nla
import scipy.linalg as sla

# 构造一个 3x3 矩阵
A = np.array([[2.0, 1.0, 0.0],
              [1.0, 3.0, 1.0],
              [0.0, 1.0, 2.0]])

# (1) 用 scipy.linalg 调两个函数
det_s  = sla.det(A)
norm_s = sla.norm(A)          # 默认 Frobenius 范数（2 范数矩阵情形）
print(f"scipy det(A)  = {det_s}")
print(f"scipy ||A||_F = {norm_s}")

# (2) 用 numpy.linalg 调同名函数，对比
det_n  = nla.det(A)
norm_n = nla.norm(A)
print(f"numpy det(A)  = {det_n}")
print(f"numpy ||A||_F = {norm_n}")

# (3) 观察差异：结果应一致
print("det  一致：", np.isclose(det_s, det_n))
print("norm 一致：", np.isclose(norm_s, norm_n))

# (4) 故意制造一个奇异矩阵，触发 LinAlgError（用 inv 演示异常共享）
singular = np.array([[1.0, 2.0],
                     [2.0, 4.0]])
try:
    sla.inv(singular)
except sla.LinAlgError as e:
    print("捕获到 LinAlgError：", e)
    print("它就是 numpy 的异常吗：", type(e) is nla.LinAlgError)
```

**步骤 3 —— 需要观察的现象与预期结果**：

1. `(1)` 与 `(2)` 打印的 `det` 与 `norm` 数值应当**完全一致**（`isclose` 为 `True`）。Frobenius 范数为
   \[
   \lVert A \rVert_F = \sqrt{\sum_{i,j} |a_{ij}|^2},
   \]
   对上面的 \(A\) 你可以手算验证。
2. `(4)` 对奇异矩阵求逆会抛出 `LinAlgError`（"Singular matrix"），且这个异常与 `numpy.linalg.LinAlgError` 是同一个类。
3. 行列式 \(\det A\) 的具体数值、范数具体数值，待本地运行确认（取决于浮点实现，但两个包必然一致）。

**如果出错怎么办**：

- `ImportError: DLL load failed` —— 多半是底层 LAPACK/BLAS 没装好，换 `pip install --force-reinstall scipy` 或用 conda。
- `LinAlgError` 没被触发 —— 检查你的「奇异矩阵」是否真的行列式为 0（上面 `[1,2;2,4]` 满足）。

完成本任务后，你已经亲手验证了本讲的全部结论：`scipy.linalg` 可用、与 `numpy.linalg` 同名函数结果一致但参数更丰富、异常类型共享。

## 6. 本讲小结

- `scipy.linalg` 是 SciPy 中负责线性代数的子包，入口是 [`__init__.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/__init__.py)，它把功能分成 Basics、特征值、分解、矩阵函数、矩阵方程、随机投影、特殊矩阵 7 大类。
- 它本身几乎不含算法，而是用一组 `from ._xxx import *` 把十几个实现文件的函数汇聚到一个命名空间，再用 `[s for s in dir() if not s.startswith('_')]` 自动生成 `__all__`。
- 与 `numpy.linalg` 是**互补**关系：SciPy 函数更多更专业，同名函数底层多走 LAPACK/BLAS 并提供 `check_finite`、`overwrite_a` 等额外参数。
- 两包**共享同一个 `LinAlgError`**（SciPy 直接 `from numpy.linalg import LinAlgError`），SciPy 另外定义了 `LinAlgWarning`。
- 你可以用 `scipy.linalg.test()` 一键跑这个子包的全部测试。
- 文档字符串里那句「identically named functions may offer more or slightly differing functionality」是理解两包差异的官方依据。

## 7. 下一步学习建议

下一篇讲义是 **u1-l2「模块组织、目录结构与 `__init__.py` 导出」**，它会带你更细致地走一遍 `scipy/linalg/` 目录里的每一个 `.py` / `.pyx` / `.pyf.src` 文件，并练习根据 `solve`、`svd`、`expm`、`toeplitz` 这些函数名反查它们各自来自哪个实现文件。

在你有了「文件 ↔ 主题」的全景图之后，建议按手册顺序进入：

- 第 1 单元后半（构建系统、第一个程序）。
- 第 2 单元：基础运算（`norm`、`solve`、`inv`、`det`、结构化求解器）——这是日常用得最多的部分。
- 之后再到分解、特征值、矩阵函数等更专门的算法主题。

继续阅读的源码建议：先把 [`__init__.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/__init__.py) 当成「目录」通读一遍，对每个函数名留个印象即可，不必现在就深入算法。
