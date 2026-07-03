# 模块组织、目录结构与 `__init__.py` 导出

## 1. 本讲目标

本讲承接 [u1-l1](u1-l1-project-overview.md) 对 `scipy.linalg` 的整体定位，把视线从「这个子包是什么」下沉到「它的源码目录长什么样、公共 API 是怎么被组装出来的」。

学完本讲你应该能够：

- 说出 `scipy/linalg/` 目录下源码文件的组织方式和命名约定。
- 理解 `__init__.py` 如何用「星号导入」+「自动 `__all__`」把几十个实现文件里的函数汇聚到一个统一命名空间。
- 看到一个函数名（比如 `svd`、`expm`、`toeplitz`），能立刻定位到它真正的实现文件。

> 这一篇是「阅读导航篇」：它本身不讲解任何算法，但它是后续所有讲义的地图。没有这张图，你会在几十个 `_decomp_*.py` 里迷路。

## 2. 前置知识

阅读本讲前，建议你已经知道（在 [u1-l1](u1-l1-project-overview.md) 中介绍过）：

- **子包（subpackage）**：`scipy.linalg` 是 SciPy 下的一个子包，对应磁盘上的一个目录 `scipy/linalg/`，目录里的 `__init__.py` 决定了 `import scipy.linalg` 时能拿到什么。
- **命名空间（namespace）**：当你写 `scipy.linalg.solve(...)` 时，`scipy.linalg` 这个名字背后绑定的就是一组名字（`solve`、`det`、`svd`……），这组名字构成一个命名空间。
- **星号导入（star import）**：`from ._basic import *` 的意思是「把 `_basic` 模块里所有「公开」的名字（由它的 `__all__` 决定）全部搬进当前模块」。
- **`__all__`**：一个 Python 模块里的列表，声明「这个模块对外公开哪些名字」。它同时控制 `from module import *` 会搬走哪些名字。

如果这些概念还比较模糊，记住一句话即可：**`scipy.linalg` 是一个「大房间」，实现算法的各个 `_xxx.py` 文件是「小仓库」，`__init__.py` 负责把小仓库里的工具搬到这个大房间里供人使用。**

## 3. 本讲源码地图

本讲主要阅读下面两个文件：

| 文件 | 作用 |
|---|---|
| [`__init__.py`](__init__.py) | 子包入口。本身不含算法，只做两件事：用一段大文档字符串把所有公共函数按功能分类列出；用一组星号导入把各实现文件的函数汇聚到统一命名空间。 |
| [`_misc.py`](_misc.py) | 「杂项」实现文件之一，提供 `norm`、`bandwidth`，并定义 SciPy 自有的 `LinAlgWarning`、复用 NumPy 的 `LinAlgError`。我们用它来代表「一个典型的实现文件长什么样」。 |

此外，本讲会**列出但不必逐行阅读**的实现文件家族：`_basic.py`、`_decomp.py`、`_decomp_lu.py`、`_decomp_svd.py`、`_matfuncs.py`、`_special_matrices.py` 等（后续讲义会逐个深入）。

## 4. 核心概念与源码讲解

### 4.1 目录布局：从「功能分类」到「文件命名」

#### 4.1.1 概念说明

`scipy/linalg/` 是一个体量很大的目录（几十个源码文件）。如果所有函数都堆在一个文件里，既无法维护也无法阅读。SciPy 的做法是**按「算法主题」拆文件**，让每个文件专注一类计算。

拆分遵循两条约定，掌握这两条你就能「望文生义」地猜出函数的位置：

1. **以 `_` 开头的文件是「真正的实现」**，属于内部私有模块（Python 里前导下划线表示「不要直接 import 我」）。用户不应写 `from scipy.linalg._basic import solve`，而应写 `from scipy.linalg import solve`。
2. **文件名暗示主题**：`_decomp_*.py` 是各种「矩阵分解」（decomposition），`_matfuncs*.py` 是「矩阵函数」（matrix functions），`_special_matrices.py` 是「特殊矩阵构造」，等等。

> 顺带一提：目录里还有一批**不带下划线**的同名文件，比如 `basic.py`、`decomp.py`、`matfuncs.py`、`misc.py`。它们**不是实现**，而是历史遗留的「废弃命名空间垫片」，会在 SciPy v2.0.0 移除——本讲 4.3.3 节会专门说明，**初学者请一律忽略它们**。

#### 4.1.2 核心流程

当你调用 `scipy.linalg.svd(A)` 时，名字解析的路径是：

```text
scipy.linalg.svd
   └── 在 __init__.py 执行时，由 `from ._decomp_svd import *` 把 svd 搬进来
        └── 真正定义在 _decomp_svd.py 里
```

也就是说，`__init__.py` 在 `import scipy.linalg` 的瞬间把所有实现文件的公开名字「摊」到了顶层命名空间。你看到的 `scipy.linalg.svd` 只是 `_decomp_svd.svd` 的一个引用。

#### 4.1.3 源码精读：目录里都有哪些文件

执行 `ls` 后，`scipy/linalg/` 下的文件大致可以分成四组：

| 组别 | 代表文件 | 说明 |
|---|---|---|
| **入口** | `__init__.py` | 汇聚导出，本讲主角。 |
| **实现（Python）** | `_basic.py`、`_decomp.py`、`_decomp_lu.py`、`_decomp_svd.py`、`_matfuncs.py`、`_misc.py`、`_special_matrices.py`、`_solvers.py`、`_procrustes.py`、`_sketches.py`、`_expm_frechet.py`、`_matfuncs_inv_ssq.py`、`_matfuncs_sqrtm.py` | 纯 Python 写的高层逻辑，是后续讲义的主要阅读对象。 |
| **实现（Cython / C / C++）** | `_cythonized_array_utils.pyx`、`_solve_toeplitz.pyx`、`_matfuncs_sqrtm_triu.pyx`、`_decomp_update.pyx.in`、`_decomp_interpolative.pyx`、`src/` 目录 | 性能关键的热路径，用编译型语言写成。 |
| **底层绑定与构建** | `blas.py`、`lapack.py`、`interpolative.py`、`*.pyf.src`、`meson.build`、`_generate_pyx.py` | 对接 BLAS/LAPACK 的分发与构建脚本。 |

注意 `__init__.py` 的文档字符串已经把全部公共函数按功能分成了 7 大类，这其实就是「实现文件分组」的用户视角投影。下面是其中「Basics（基础）」一类的文档片段：

[__init__.py:29-56](__init__.py#L29-L56) —— 文档字符串里「Basics」分类，列出了 `inv`、`solve`、`det`、`norm`、`bandwidth`、`issymmetric` 等基础函数。注意这些只是「文档目录」，真正代码并不在这里。

[__init__.py:113-132](__init__.py#L113-L132) —— 「Matrix Functions（矩阵函数）」分类，列出 `expm`、`logm`、`sqrtm`、`funm` 等。这一类函数的实现几乎都集中在 `_matfuncs.py`。

> 小贴士：文档字符串里的 7 大分类（Basics / Eigenvalue Problems / Decompositions / Matrix Functions / Matrix Equation Solvers / Sketches and Random Projections / Special Matrices）和文件的拆分**不完全一一对应**——比如「Basics」里的函数其实分散在 `_basic.py`、`_misc.py`、`_cythonized_array_utils.pyx`、`_procrustes.py` 等多个文件。这正是为什么我们需要 4.3 节的「文件 → 函数」对照表。

#### 4.1.4 代码实践

1. **实践目标**：建立「文件名 ↔ 主题」的直觉。
2. **操作步骤**：在你的 SciPy 安装环境里执行下面这行（或对照本讲目录表）：

   ```python
   import scipy.linalg, os
   print(os.path.dirname(scipy.linalg.__file__))
   ```

   然后用编辑器打开该目录，浏览文件名，试着不看答案地把下面 4 个名字归到主题：`solve_banded`、`lu_factor`、`sqrtm`、`hilbert`。
3. **需要观察的现象**：你会发现文件名里的关键词（`banded`、`lu`、`matfuncs`、`special_matrices`）几乎直接对应函数名里的线索。
4. **预期结果**：`solve_banded` 在 `_basic.py`（基础求解）、`lu_factor` 在 `_decomp_lu.py`（LU 分解）、`sqrtm` 在 `_matfuncs.py`（矩阵函数）、`hilbert` 在 `_special_matrices.py`（特殊矩阵）。

#### 4.1.5 小练习与答案

**练习 1**：目录里同时存在 `_decomp.py` 和 `_decomp_lu.py`、`_decomp_svd.py` 等，为什么不把所有分解都放进 `_decomp.py`？

> **参考答案**：随着功能增加，单个文件会过大、难以维护。SciPy 把「特征值问题」留在 `_decomp.py`，而把体量较大的 LU、SVD、QR、Cholesky、Schur、QZ、LDL、Polar、Cossin 各自拆成独立的 `_decomp_<方法>.py`，做到「一文件一主题」，便于阅读、测试和并行开发。

**练习 2**：文件名以 `_` 开头（如 `_basic.py`）和不以 `_` 开头（如 `basic.py`）有什么本质区别？

> **参考答案**：`_basic.py` 是**真正的实现**；`basic.py`（无下划线）是**废弃垫片**，只为兼容老的 `scipy.linalg.basic.xxx` 写法而存在，内部通过 `__getattr__` 转发并发出弃用警告，将在 v2.0.0 删除。新代码一律用 `scipy.linalg.xxx`。

---

### 4.2 `__init__.py` 的分类与导入顺序

#### 4.2.1 概念说明

`__init__.py` 的核心职责有两个，恰好对应文件的两段：

1. **文档（前 ~200 行）**：一段很长的文档字符串，扮演「公共 API 的菜单」。它用 `.. autosummary::` 指令把函数按 7 大类列出来——这主要服务 Sphinx 文档生成，但也是人类读者的目录。
2. **导入（后 ~30 行）**：真正把函数「搬」进命名空间的代码。它由 21 条星号导入 + `__all__` 生成 + 测试钩子三部分组成。

理解的关键是：**菜单（文档）和上菜（导入）是两回事**。文档列出的函数必须确实被某条 `from ._xxx import *` 搬进来，否则 `scipy.linalg.该函数` 就会 `AttributeError`。

#### 4.2.2 核心流程

`import scipy.linalg` 时发生的事情：

```text
1. Python 执行 __init__.py
2. 逐行执行 21 条星号导入：
   from ._misc import *            → 搬入 norm, bandwidth, LinAlgError, LinAlgWarning
   from ._cythonized_array_utils import *  → 搬入 issymmetric, ishermitian
   from ._basic import *           → 搬入 solve, inv, det, lstsq ...
   ... （依次搬入各实现文件的公开名字）
3. 执行 __all__ = [s for s in dir() if not s.startswith('_')]
   → 扫描当前命名空间，把所有「不以 _ 开头」的名字收进 __all__
4. 挂上 test = PytestTester(__name__)
```

第 3 步是关键巧思：作者**没有手写 `__all__`**，而是先让星号导入把名字都搬进来，再用 `dir()` 扫一遍「当前房间里有哪些不带下划线的东西」自动生成。这样新增一个实现文件、加一条星号导入后，`__all__` 会自动更新，无需同步维护两处列表。

#### 4.2.3 源码精读

**21 条星号导入**是整个汇聚机制的发动机：

[__init__.py:201-221](__init__.py#L201-L221) —— 依次从 `_misc`、`_cythonized_array_utils`、`_basic`、`_decomp`、`_decomp_lu`、`_decomp_ldl`、`_decomp_cholesky`、`_decomp_qr`、`_decomp_qz`、`_decomp_svd`、`_decomp_schur`、`_decomp_polar`、`_matfuncs`、`blas`、`lapack`、`_special_matrices`、`_solvers`、`_procrustes`、`_decomp_update`、`_sketches`、`_decomp_cossin` 把公开函数搬入。每条 `*` 搬走什么，由对应文件的 `__all__` 决定。

注意导入**顺序**并非任意：`_misc` 在最前，是因为 `norm`/`LinAlgError` 这类基础工具会被后续文件依赖；而 `blas`/`lapack` 排在实现文件中间，因为很多实现文件本身（如 `_basic.py`）会 `from .lapack import get_lapack_funcs`——不过这里的星号导入顺序主要影响「同名函数谁覆盖谁」，SciPy 设计上各文件 `__all__` 没有冲突，所以顺序在实践中不敏感。

**自动生成 `__all__`**：

[__init__.py:229](__init__.py#L229-L229) —— `__all__ = [s for s in dir() if not s.startswith('_')]`。`dir()` 返回当前模块命名空间里的所有名字，过滤掉带前导下划线的「内部」名字（如导入进来的模块名 `_basic`、Python 内置等），剩下的就是对外公开的 API。星号导入越多，这个列表越长，完全自动化。

**测试钩子**：

[__init__.py:232-234](__init__.py#L232-L234) —— 引入 `PytestTester` 并绑定到 `test`，所以你能写 `scipy.linalg.test()` 来跑该子包的测试；随后 `del PytestTester` 把类本身从命名空间删掉，避免它污染公开 API（也正因如此，上一行的 `__all__` 里不会出现 `PytestTester`）。

> 一个推论：因为 `__all__` 是自动扫描出来的，任何被星号导入搬进来的「不带下划线的名字」都会变成公开 API。这也是为什么实现文件里的私有辅助函数都严格用 `_` 前缀命名（例如 `_datacopied`、`_format_emit_errors_warnings`）——否则它们会被意外「漏」进公开命名空间。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看「星号导入 + 自动 `__all__`」搬出了哪些名字。
2. **操作步骤**：在装好 SciPy 的环境里运行：

   ```python
   import scipy.linalg as sla
   # 1) 看 __all__ 长什么样（节选前 15 个）
   print(sla.__all__[:15])
   # 2) 看某个名字是不是真的是被搬进来的、来自哪个文件
   print("svd 的定义文件：", sla.svd.__module__)
   print("expm 的定义文件：", sla.expm.__module__)
   print("solve 的定义文件：", sla.solve.__module__)
   print("toeplitz 的定义文件：", sla.toeplitz.__module__)
   ```

3. **需要观察的现象**：`__all__` 是一个很长的字符串列表，不含任何带 `_` 开头的名字；四个函数的 `__module__` 分别指向不同的实现文件。
4. **预期结果**：`svd.__module__ == 'scipy.linalg._decomp_svd'`，`expm.__module__ == 'scipy.linalg._matfuncs'`，`solve.__module__ == 'scipy.linalg._basic'`，`toeplitz.__module__ == 'scipy.linalg._special_matrices'`。这直接验证了 4.1.2 里描述的「名字解析路径」。

#### 4.2.5 小练习与答案

**练习 1**：如果有人在 `_basic.py` 里新增了一个函数 `foo` 并加进它的 `__all__`，但**没有**在 `__init__.py` 里加新的星号导入，`scipy.linalg.foo` 能用吗？

> **参考答案**：能用。因为 `__init__.py` 里已经有 `from ._basic import *`，新增的 `foo` 会被这条现成的星号导入搬进来，并自动出现在 `__all__` 里。这正是「自动 `__all__`」机制的好处——只要实现文件的 `__all__` 更新了，公开 API 就同步更新。

**练习 2**：为什么 `__init__.py` 末尾要 `del PytestTester`？

> **参考答案**：`PytestTester` 是一个工具类，不是 `scipy.linalg` 想对外暴露的线性代数 API。删掉它后，`dir()` 扫描时就不会把它收进 `__all__`，保持公开命名空间的干净（用户不会误以为 `PytestTester` 是个可用的线性代数函数）。注意绑定到名字 `test` 的实例仍然保留，所以 `scipy.linalg.test()` 仍可用。

---

### 4.3 实现文件家族：`_decomp_*` 与 `_matfuncs`

#### 4.3.1 概念说明

知道了「星号导入会搬走文件 `__all__` 里的名字」，接下来就要建立**「函数名 → 实现文件」的对照表**。这张表是后续阅读源码的索引：当你想研究某个算法，先查表找到文件，再打开它。

每个实现文件顶部都有一个 `__all__`，明确声明「我对外提供哪些函数」。我们读这些 `__all__`，就能拼出完整的对照关系。

#### 4.3.2 核心流程

对照表的构造方法很简单：

```text
对 __init__.py 里每一条 `from ._xxx import *`：
    打开 _xxx.py，读它的 __all__
    记录：「_xxx.py 提供 [__all__ 里的这些函数]」
把所有记录拼起来 = 全局对照表
```

下面这张表就是按这个流程、基于真实 `__all__` 得到的。

#### 4.3.3 源码精读：文件 → 函数 对照表

先看两个本讲主角文件的 `__all__`：

[_misc.py:8](_misc.py#L8-L8) —— `_misc.py` 的 `__all__ = ['LinAlgError', 'LinAlgWarning', 'norm', 'bandwidth']`。注意 `LinAlgError` 其实是从 `numpy.linalg` 复用进来的（见 [_misc.py:2](_misc.py#L2-L2) 的 `from numpy.linalg import LinAlgError`），SciPy 并没有重新定义它；而 `LinAlgWarning` 是 SciPy 自有的（[_misc.py:11-16](_misc.py#L11-L16)）。`norm` 的实现很有代表性——它在能走 BLAS/LAPACK 快速路径时走底层例程，否则回退到 `numpy.linalg.norm`（[_misc.py:146-181](_misc.py#L146-L181)），这正呼应了 [u1-l1](u1-l1-project-overview.md) 讲过的「SciPy 同名函数底层多走 LAPACK/BLAS」。

把全部 21 条星号导入对应的 `__all__` 汇总，就得到下表（按 `__init__.py` 的导入顺序）：

| 实现文件 | `__all__` 对外公开的函数 |
|---|---|
| `_misc.py` | `norm`, `bandwidth`, `LinAlgError`, `LinAlgWarning` |
| `_cythonized_array_utils.pyx` | `issymmetric`, `ishermitian` |
| `_basic.py` | `solve`, `solve_triangular`, `solveh_banded`, `solve_banded`, `solve_toeplitz`, `solve_circulant`, `inv`, `det`, `lstsq`, `pinv`, `pinvh`, `matrix_balance`, `matmul_toeplitz` |
| `_decomp.py` | `eig`, `eigvals`, `eigh`, `eigvalsh`, `eig_banded`, `eigvals_banded`, `eigh_tridiagonal`, `eigvalsh_tridiagonal`, `hessenberg`, `cdf2rdf` |
| `_decomp_lu.py` | `lu`, `lu_solve`, `lu_factor` |
| `_decomp_ldl.py` | `ldl` |
| `_decomp_cholesky.py` | `cholesky`, `cho_factor`, `cho_solve`, `cholesky_banded`, `cho_solve_banded` |
| `_decomp_qr.py` | `qr`, `qr_multiply`, `rq` |
| `_decomp_qz.py` | `qz`, `ordqz` |
| `_decomp_svd.py` | `svd`, `svdvals`, `diagsvd`, `orth`, `subspace_angles`, `null_space` |
| `_decomp_schur.py` | `schur`, `rsf2csf` |
| `_decomp_polar.py` | `polar` |
| `_matfuncs.py` | `expm`, `cosm`, `sinm`, `tanm`, `coshm`, `sinhm`, `tanhm`, `logm`, `funm`, `signm`, `sqrtm`, `fractional_matrix_power`, `expm_frechet`, `expm_cond`, `khatri_rao` |
| `blas.py` | `get_blas_funcs`, `find_best_blas_type` |
| `lapack.py` | `get_lapack_funcs` |
| `_special_matrices.py` | `toeplitz`, `circulant`, `hankel`, `hadamard`, `leslie`, `block_diag`, `companion`, `helmert`, `hilbert`, `invhilbert`, `pascal`, `invpascal`, `dft`, `fiedler`, `fiedler_companion`, `convolution_matrix` |
| `_solvers.py` | `solve_sylvester`, `solve_continuous_lyapunov`, `solve_discrete_lyapunov`, `solve_lyapunov`, `solve_continuous_are`, `solve_discrete_are` |
| `_procrustes.py` | `orthogonal_procrustes` |
| `_decomp_update.pyx.in` | `qr_delete`, `qr_insert`, `qr_update` |
| `_sketches.py` | `clarkson_woodruff_transform` |
| `_decomp_cossin.py` | `cossin` |

几个值得注意的点：

- **`_decomp_*` 家族**几乎一文件对应一种分解：LU→`_decomp_lu.py`、Cholesky→`_decomp_cholesky.py`、QR→`_decomp_qr.py`、SVD→`_decomp_svd.py`、Schur→`_decomp_schur.py`、QZ→`_decomp_qz.py`、LDL→`_decomp_ldl.py`、Polar→`_decomp_polar.py`、Cossin→`_decomp_cossin.py`；只有「特征值问题」这一大类（`eig`/`eigh`/`eig_banded`/`eigh_tridiagonal`/`hessenberg`/`cdf2rdf`）集中在较老的 `_decomp.py` 里。
- **`_matfuncs.py`** 是矩阵函数的「大本营」，但并非全部：`sqrtm` 的底层分块算法另写在 `_matfuncs_sqrtm.py` + `_matfuncs_sqrtm_triu.pyx`，`expm` 的 Padé 内核写在 C 文件里——这些「辅助文件」多数**没有自己的 `__all__`**（或只作为内部模块），所以不直接贡献顶层 API，而是被 `_matfuncs.py` 调用。后续 u5 单元会专门拆解。
- **Cython 文件**（`.pyx`）也能参与星号导入：`_cythonized_array_utils.pyx` 和 `_decomp_update.pyx.in`（经 Tempita 模板生成 `.pyx` 后编译）都定义了 `__all__`，和普通 `.py` 文件一视同仁。

**关于「无下划线垫片文件」**：[__init__.py:224-227](__init__.py#L224-L227) 用 `from . import (decomp, decomp_lu, ... basic, misc, special_matrices, matfuncs)` 显式导入了那批废弃垫片模块。它们的真实内容（以 [matfuncs.py](matfuncs.py) 为例）只是一个 `__getattr__` 转发器：当你写 `scipy.linalg.matfuncs.expm` 时，它会发出 `DeprecationWarning` 并从 `_matfuncs` 取值。这段代码注释明确写了「will be removed in SciPy v2.0.0」。**新代码绝不要用这些子模块路径**，统一用 `scipy.linalg.expm`。

#### 4.3.4 代码实践（本讲主任务）

1. **实践目标**：不看答案，独立判断 `solve`、`svd`、`expm`、`toeplitz` 四个函数分别来自哪个子模块，并用代码验证。
2. **操作步骤**：
   - 先凭「文件名 ↔ 主题」直觉猜测：
     - `solve`（求解线性系统）→ 基础运算，猜 `__init__.py` 哪条导入？
     - `svd`（奇异值分解）→ 哪个 `_decomp_*`？
     - `expm`（矩阵指数）→ 哪个 `_matfuncs*`？
     - `toeplitz`（构造 Toeplitz 矩阵）→ 哪个文件？
   - 再用代码验证（与 4.2.4 同样的 `__module__` 技巧）：

     ```python
     import scipy.linalg as sla
     for name in ['solve', 'svd', 'expm', 'toeplitz']:
         fn = getattr(sla, name)
         print(f"{name:10s} -> {fn.__module__}")
     ```

   - 最后，做一次「反向验证」：确认 `from scipy.linalg import solve, svd, expm, toeplitz` 都能成功导入。

     ```python
     from scipy.linalg import solve, svd, expm, toeplitz
     print("全部导入成功")
     ```

3. **需要观察的现象**：四个函数的 `__module__` 各不相同；显式 `from scipy.linalg import ...` 不会报错。
4. **预期结果**：

   | 函数 | 定义模块 |
   |---|---|
   | `solve` | `scipy.linalg._basic` |
   | `svd` | `scipy.linalg._decomp_svd` |
   | `expm` | `scipy.linalg._matfuncs` |
   | `toeplitz` | `scipy.linalg._special_matrices` |

   这与 4.3.3 的对照表完全一致。

#### 4.3.5 小练习与答案

**练习 1**：我想研究 Cholesky 分解的源码，应该打开哪个文件？里面除了 `cholesky` 还有哪些相关函数？

> **参考答案**：打开 [_decomp_cholesky.py](_decomp_cholesky.py)。它的 `__all__` 提供 `cholesky`、`cho_factor`、`cho_solve`、`cholesky_banded`、`cho_solve_banded` 五个函数——分别对应「一次性分解」「分解+求解的两步复用」「带状矩阵的分解与求解」。

**练习 2**：`orth` 和 `null_space` 这两个「求列空间/零空间」的函数，为什么放在 `_decomp_svd.py` 而不是单独成文件？

> **参考答案**：因为它们在内部都是基于 SVD 实现的（用奇异值做秩截断后取 `U`/`V` 的列）。把它们和 `svd` 放在一起，体现了「按所用核心算法归档」的组织原则，方便读者顺着同一条 SVD 调用链阅读。这会在 [u3-l4](u3-l4-svd.md) 详细讲解。

**练习 3**：`bandwidth` 既不出现在 `_basic.py`，也不在 `_decomp_*.py`，它在哪？为什么？

> **参考答案**：它在 [_misc.py](_misc.py)（`__all__` 含 `bandwidth`，见 [_misc.py:8](_misc.py#L8-L8)），实现见 [_misc.py:197-274](_misc.py#L197-L274)。`bandwidth` 只是一个「描述矩阵结构的杂项工具」，不属于任何一类分解或求解，所以归入「杂项」文件 `_misc.py`，和 `norm` 放在一起。

## 5. 综合实践

把本讲的三块知识（目录布局、星号导入、文件对照表）串起来，完成下面这个「API 体检」小任务：

1. 列出 `scipy.linalg` 的全部公开函数名（`sla.__all__`）。
2. 对其中**随机抽取的 10 个函数**，分别用 `fn.__module__` 找到它们的实现文件。
3. 对照 4.3.3 的「文件 → 函数」表，验证你的查找结果是否一致。
4. 挑一个你感兴趣的文件（比如 `_decomp_lu.py` 或 `_special_matrices.py`），打开它，确认它顶部的 `__all__` 与表中列出的一致。

```python
import random, scipy.linalg as sla

names = sla.__all__
sample = random.sample(names, 10)   # 随机抽 10 个（示例代码，需本地 numpy/random 环境）
for n in sample:
    print(f"{n:30s} -> {getattr(sla, n).__module__}")
```

> 说明：上面的 `random.sample` 属于「示例代码」，仅为展示思路；在受限环境里你也可以手动挑 10 个名字。关键是体会「**公开 API 名字 → 实现文件**」这条可验证的链路。完成本任务后，你应当能凭直觉定位绝大多数 `scipy.linalg` 函数的源码位置。

## 6. 本讲小结

- `scipy/linalg/` 按**算法主题**拆文件：`_decomp_*.py` 是各种矩阵分解，`_matfuncs*.py` 是矩阵函数，`_special_matrices.py` 是特殊矩阵构造，`_basic.py` 是基础求解/求逆/行列式。
- `__init__.py` 本身不含算法，它用 **21 条星号导入**（`from ._xxx import *`）把各实现文件 `__all__` 里的函数汇聚到 `scipy.linalg` 顶层命名空间。
- `__all__` 是**自动生成**的：`[s for s in dir() if not s.startswith('_')]`，扫描「不带下划线的名字」，无需手写、自动跟随星号导入更新。
- 每个实现文件顶部的 `__all__` 决定了「星号导入会搬走哪些函数」，把它们拼起来就是「文件 → 函数」对照表——这是阅读源码的索引。
- 目录里**不带下划线**的同名文件（`basic.py`、`matfuncs.py` 等）是**废弃垫片**，将在 v2.0.0 移除，新代码一律用 `scipy.linalg.函数名`。
- 验证某个函数来自哪个文件，最直接的方法是看 `scipy.linalg.函数名.__module__`。

## 7. 下一步学习建议

有了这张「目录与导出」地图，下一步可以沿着两条路深入：

- **如果想立刻动手用**：进入 u1 单元的 [u1-l4 第一个线性代数程序](u1-l4-first-program.md)，学习 `solve`/`inv`/`det`/`norm` 的实际用法和 `check_finite`、`overwrite_a` 等公共参数。
- **如果想继续摸清底层**：先读 [u1-l3 构建系统](u1-l3-build-system.md)，了解 `_basic.py`、`_decomp_*.py` 这些 `.py` 文件如何与 f2py、Cython、C++ 扩展一起被 Meson 编译成可用的 `scipy.linalg`。

建议按 u1-l3 → u1-l4 的顺序学完入门层，再进入 u2/u3 的算法实现讲义。届时你会发现，每当讲义提到某个函数，你都能迅速在本讲的对照表里找到它的源码文件——这正是本讲想留给你的能力。
