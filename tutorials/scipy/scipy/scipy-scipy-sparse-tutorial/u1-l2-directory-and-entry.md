# 目录结构、入口与依赖关系

## 1. 本讲目标

上一讲我们建立了「为什么要用稀疏存储」的直觉，认识了 `_spbase`、`_formats`、`nnz` 以及 `spmatrix → sparray` 的迁移方向。本讲换一个视角，**从工程结构上**俯瞰 `scipy.sparse` 这个子包：它由哪些文件组成、入口在哪里、这些文件如何被组织在一起、旧接口又以什么形式苟延着。

学完后你应该能够：

1. 读懂 `scipy/sparse/` 目录里「Python 源码层 / C++ 计算后端 / 子模块 / 测试」的分层。
2. 解释包入口 `__init__.py` 用 `from ._xxx import *` 聚合导出的机制，以及 `__all__` 是如何被自动算出来的。
3. 认识那些**单字母前缀**的旧模块（`base.py`、`coo.py`、`csr.py` 等）是 v2.0 待移除的兼容垫片，知道为什么不该再用它们。
4. 理解 `csgraph`、`linalg` 两个子模块是「惰性导入（lazy import）」的，访问时才真正加载。

## 2. 前置知识

在进入源码之前，先澄清几个本讲会用到的 Python 包机制术语（如果你已经熟悉，可以跳过）：

- **包（package）**：一个含有 `__init__.py` 的目录。`import scipy.sparse` 时，Python 实际执行的是 `scipy/sparse/__init__.py` 这个文件。所以 `__init__.py` 就是整个子包的「入口」与「门面」。
- **`from .xxx import *`**：从**当前包**（`.` 代表当前目录）的 `xxx` 模块里，把它公开的名字（由它的 `__all__` 决定，或所有不以 `_` 开头的名字）批量搬进当前命名空间。这是把分散在多个文件里的类/函数「汇聚」到一个门面里的常见手法。
- **`__all__`**：一个字符串列表，声明「`from 包 import *` 时会导出哪些名字」。它也常被文档工具和 IDE 用来确定公开 API。
- **惰性导入 / 延迟加载**：不在 `import scipy.sparse` 时就把所有子模块全加载进来，而是在**第一次访问** `sparse.linalg` 这类属性时才去加载。好处是启动更快、避免无谓地加载用不到的重型依赖。
- **DeprecationWarning（弃用警告）**：Python 标准的「这个接口还能用，但未来会移除，请改用新接口」的提示机制。

> 本讲承接上一讲的认知：所有 `*_array`/`*_matrix` 类、`_spbase` 基类、`_formats` 都定义在下划线前缀的「真」模块里（`_coo.py`、`_csr.py`、`_base.py` 等），而 `__init__.py` 只是把它们重新暴露给用户。

## 3. 本讲源码地图

本讲只聚焦「结构」，不深入任何一种格式的内部算法。涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [`__init__.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/__init__.py) | 子包入口与门面。顶部是用户文档；下半部分用 `import *` 聚合所有公开类与函数，并维护弃用模块列表与子模块列表。 |
| [`meson.build`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/meson.build) | 构建脚本。声明哪些 `.py` 要被安装、哪个 C++/Cython 扩展要被编译，以及四个子目录的构建顺序。 |
| [`base.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/base.py) / [`csr.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/csr.py) / [`sparsetools.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/sparsetools.py) | 三个「弃用兼容垫片」的代表性样例，结构几乎一模一样。 |

目录层面还会涉及的子目录（本讲只点到为止，后续单元深入）：

- `sparsetools/` —— C++ 计算后端（CSR/COO/BSR/DIA 的底层内核）。
- `csgraph/` —— 图算法子模块（最短路、连通分量、匹配等）。
- `linalg/` —— 稀疏线性代数子模块（求解器、特征值等），内部又分 `_dsolve`/`_isolve`/`_eigen`/`_propack` 等子目录。
- `tests/` —— 测试套件。

## 4. 核心概念与源码讲解

### 4.1 目录整体分层：源码层、C++ 后端、子模块、测试

#### 4.1.1 概念说明

一个成熟的科学计算子包，通常不是「一个大文件」，而是**多层分工**。`scipy.sparse` 至少分成四层：

1. **Python 源码层**：定义各类稀疏数组/矩阵、构造函数、索引、工具函数。这是用户直接打交道的一层，全部是 `_xxx.py`（下划线前缀，表示「内部实现」）。
2. **C++ 计算后端**：把耗时的数值循环（矩阵转格式、求和、排序等）用 C++ 实现，通过 Cython 暴露给 Python。位于 `sparsetools/`。
3. **应用子模块**：在「稀疏存储」这一底座之上，构建更高层的算法领域——图算法（`csgraph`）与线性代数（`linalg`）。
4. **测试层**：`tests/` 里大量 `test_*.py`。

之所以要分层，是因为**关注点不同**：用户 API 要稳定易用、数值内核要极致性能、领域算法要可复用底座。把它们放进不同文件与目录，代码才便于维护和演进。

#### 4.1.2 核心流程

用一个「从上到下」的视角看调用关系：

```text
用户代码
  └─ import scipy.sparse            ← 执行 __init__.py
       ├─ _base.py / _coo.py / _csr.py / ...   ← Python 源码层（类与函数）
       │     └─ _csparsetools（编译扩展）       ← Cython 胶水
       │            └─ sparsetools/*.h/*.cxx    ← C++ 计算后端
       ├─ csgraph/（惰性）   ── 图算法
       └─ linalg/（惰性）    ── 线性代数
```

构建时则反过来：`meson.build` 先把 `.py` 安装好、把 C++/Cython 扩展编译好，子包才能被 `import`。

#### 4.1.3 源码精读

构建脚本 [`meson.build`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/meson.build) 的末尾四行决定了四个子目录的构建顺序：

[`meson.build:58-61`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/meson.build#L58-L61) —— 这四行依次进入 `sparsetools`、`csgraph`、`linalg`、`tests` 子目录构建，把分层落实到工程上。

被安装的 Python 源码清单定义在 [`meson.build:16-50`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/meson.build#L16-L50) 的 `python_sources` 列表里。注意它**同时包含**两类文件：

- 下划线前缀的「真」模块：`_base.py`、`_csr.py`、`_coo.py`、`_compressed.py`、`_construct.py`、`_data.py`、`_index.py`、`_sputils.py` 等；
- 单字母前缀的「弃用垫片」：`base.py`、`csr.py`、`coo.py`、`sparsetools.py` 等（详见 4.3）。

两者都会被安装到安装目录的 `scipy/sparse/` 下，但只有前者是「真正干活」的代码。

C++ 后端的入口扩展则由 [`meson.build:1-5`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/meson.build#L1-L5) 与 [`meson.build:7-14`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/meson.build#L7-L14) 编译：先用 `tempita` 模板把 `_csparsetools.pyx.in` 展开成 `_csparsetools.pyx`，再用 Cython 编译成 `_csparsetools` 扩展模块。这一「模板 + 代码生成」的细节是 U1-L3 与 U3-L6 的主题，这里只要知道「有这么一层 C++ 内核」即可。

#### 4.1.4 代码实践

**实践目标**：用 `meson.build` 自己验证「哪些文件会被装、哪个扩展会被编译」。

**操作步骤**：

1. 打开 [`meson.build`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/meson.build)。
2. 在 `python_sources` 列表（16–50 行）里，数一共有多少个文件、其中多少个以下划线开头、多少个是单字母前缀。
3. 找到 `py3.extension_module('_csparsetools', ...)`（7–14 行），确认扩展模块名是 `_csparsetools`，它会被安装到 `subdir: 'scipy/sparse'`。

**需要观察的现象 / 预期结果**：

- `python_sources` 共 **34** 个 `.py`：19 个下划线前缀（含 `__init__.py`），15 个单字母前缀弃用垫片。
- 被编译的扩展只有一个：`_csparsetools`（它不在 `python_sources` 里，因为是编译产物）。
- 四个 `subdir(...)` 对应四个子目录。

> 注：以上计数基于本仓库 HEAD 的源文件统计，已可直接核对；若你本地有未提交改动，请以实际为准。

#### 4.1.5 小练习与答案

**练习 1**：`meson.build` 里的 `python_sources` 没有包含 `_csparsetools.py`，但用户却能 `import` 到稀疏矩阵的计算能力，为什么？
**答案**：因为 `_csparsetools` 是一个**编译扩展模块**（由 `.pyx.in` 经 tempita + Cython 生成），通过 `py3.extension_module(...)` 单独声明并 `install: true` 安装，不需要也不能放进纯 Python 的 `python_sources` 列表。

**练习 2**：如果想新增一个纯 Python 模块 `_foo.py` 并让用户能用到，至少要改哪两处？
**答案**：在 `meson.build` 的 `python_sources` 里加上 `'_foo.py'`（否则不会被安装）；并在 `__init__.py` 里加上 `from ._foo import *`（否则不会进入 `scipy.sparse` 的公开命名空间）。

---

### 4.2 `__init__.py` 的 import 聚合：把分散的类汇成一个门面

#### 4.2.1 概念说明

`scipy.sparse` 的真正实现分散在十几个 `_xxx.py` 里，但用户只需要写 `from scipy.sparse import csr_array` 就能拿到类。这背后的「汇聚」工作就由 [`__init__.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/__init__.py) 完成。它是一个**门面（facade）**：自己几乎不含业务逻辑，只负责把各模块的公开名字搬到 `scipy.sparse` 这个命名空间下，并写好文档。

#### 4.2.2 核心流程

```text
from ._coo import *      ─┐
from ._csr import *       │  每条 import * 把对应模块的公开名字
from ._construct import * │  搬进 scipy.sparse 命名空间
...                      ─┘
        │
        ▼
__all__ = [不以 _ 开头的所有名字] + ['csgraph', 'linalg']
        │
        ▼
用户：from scipy.sparse import csr_array   ✓（名字已在门面里）
```

关键巧思在最后一行：`__all__` 不是手工维护的清单，而是用 `dir()` **自动收集**当前命名空间里所有不以 `_` 开头的名字，再拼上两个子模块名。这样只要往某个 `_xxx.py` 里新增一个公开类并在 `__init__` 里 `import *`，它就会自动成为公开 API，无需额外登记。

#### 4.2.3 源码精读

聚合导出的核心是这一段，[`__init__.py:250-262`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/__init__.py#L250-L262)：

```python
from ._base import *        # _spbase, sparray, _formats, issparse ...
from ._csr import *         # csr_array, csr_matrix, isspmatrix_csr ...
from ._csc import *         # csc_array, csc_matrix ...
from ._lil import *
from ._dok import *
from ._coo import *
from ._dia import *
from ._bsr import *
from ._construct import *   # eye_array, diags_array, random_array, kron ...
from ._extract import *     # find, tril, triu
from ._matrix import spmatrix
from ._matrix_io import *   # save_npz, load_npz
from ._sputils import get_index_dtype, safely_cast_index_arrays
```

这段做了两件事：导入七种格式的类（来自 `_csr/_csc/_bsr/_lil/_dok/_coo/_dia`），以及导入构造/工具函数（`_construct/_extract/_matrix_io/_sputils`）。注意有些是 `import *`（搬全部公开名），有些是显式点名（如 `from ._matrix import spmatrix`、`from ._sputils import get_index_dtype, safely_cast_index_arrays`）——后者是为了只暴露少数几个名字，避免污染命名空间。

紧接着 [`__init__.py:273`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/__init__.py#L273) 用一行算出公开 API：

```python
__all__ = [s for s in dir() if not s.startswith('_')] + _submodules
```

`dir()` 此刻返回的就是上面所有 `import *` 搬进来的名字加上少量局部变量；过滤掉 `_` 开头的内部名（如 `_warnings`、`_importlib`、`_submodules`），剩下的就是公开 API，再补上两个子模块名。

#### 4.2.4 代码实践

**实践目标**：验证门面里某个类的「真实出身」，体会 `import *` 的汇聚效果。

**操作步骤**（在装好 SciPy 的环境里）：

```python
import scipy.sparse as sp

# 1) 看 csr_array / coo_array 的「真实模块」
print(sp.csr_array.__module__)   # 预期: scipy.sparse._csr
print(sp.coo_array.__module__)   # 预期: scipy.sparse._coo

# 2) 看 linalg / csgraph 的「类型」
print(type(sp.linalg))           # 预期: <class 'module'>
print(sp.linalg.__name__)        # 预期: scipy.sparse.linalg

# 3) 看 __all__ 里前几个名字
print(sp.__all__[:8])
```

**需要观察的现象 / 预期结果**：

- `csr_array.__module__` 是 `scipy.sparse._csr`，说明它**定义在** `_csr.py`，只是被 `__init__.py` 转手暴露。
- `__all__` 里既包含类名（`csr_array` …）、函数名（`kron`、`save_npz` …），也包含字符串 `'csgraph'`、`'linalg'`。

> 待本地验证：具体打印内容以你安装的 SciPy 版本为准；如果你在仓库源码目录里直接 `import`，确保装的是同一份代码。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `__all__` 用 `dir()` 动态生成，而不是手写一个列表？
**答案**：手写清单容易和实际导入脱节（新增类忘记登记、删掉类忘记移除）。用 `dir()` 自动收集，能保证「只要被 `import *` 搬进来的公开名字，就一定在 `__all__` 里」，降低维护成本。

**练习 2**：`from ._sputils import get_index_dtype, safely_cast_index_arrays` 为什么不写成 `from ._sputils import *`？
**答案**：`_sputils.py` 是工具模块，内部有大量仅供其他 `_xxx.py` 使用的辅助函数（类型判断、形状校验等）。`import *` 会把这些工具名全暴露到公开 API 里，造成污染；显式点名只暴露两个确有用户需求的函数（见文档「Sparse tools」一节），是更克制的做法。

---

### 4.3 弃用兼容垫片：单字母前缀模块（v2.0 待移除）

#### 4.3.1 概念说明

历史上，用户习惯 `from scipy.sparse.csr import csr_matrix` 这样**从子模块**导入。后来 SciPy 把实现搬进了下划线前缀的 `_csr.py`，并希望统一从 `scipy.sparse` 顶层导入。但为了不立刻破坏全世界的老代码，留下了**一批同名、但去掉下划线**的「垫片」模块（`csr.py`、`coo.py`、`base.py` …）。它们**不再含真正的实现**，只负责在被打扰时报一条弃用警告，然后把请求转发给真正的 `_xxx` 模块。这批垫片计划在 **SciPy v2.0.0** 移除。

#### 4.3.2 核心流程

以 `csr.py` 为例，它的工作方式是经典的 **PEP 562 模块级 `__getattr__`**：

```text
用户: from scipy.sparse.csr import csr_matrix
   │
   ▼
Python 在 csr.py 命名空间里找不到 csr_matrix
   │
   ▼
触发 csr.py 的 __getattr__("csr_matrix")
   │
   ▼
_sub_module_deprecation(...)  发出 DeprecationWarning
   │
   ▼
从真正的 _csr.py 取出 csr_matrix 返回（仍能用，但有警告）
```

也就是说，垫片是一个「会抱怨但还是会帮你」的中转站。

#### 4.3.3 源码精读

`__init__.py` 顶部用一段注释明确标注了这批模块的去留，[`__init__.py:265-269`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/__init__.py#L265-L269)：

```python
# Deprecated namespaces, to be removed in v2.0.0
from . import (
    base, bsr, compressed, construct, coo, csc, csr, data, dia, dok, extract,
    lil, sparsetools, sputils
)
```

这里显式 `import` 进来 **14** 个垫片模块。为什么要显式 import？因为这些垫片自身在 `__init__.py` 执行时**不会**立刻报警告（`import` 一个模块并不触发它的 `__getattr__`），只有当用户**从垫片里取属性**时才报。把它们 import 进来，是为了让 `scipy.sparse.csr` 这个属性存在、保持向后兼容。

垫片本身长什么样？它们几乎一模一样。看 [`csr.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/csr.py)（全文仅 22 行）：

[`csr.py:1-22`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/csr.py#L1-L22) —— 顶部注释 `# This file is not meant for public use and will be removed in SciPy v2.0.0`；定义 `__all__`（如 `'csr_matrix'`、`'isspmatrix_csr'`）；核心是 `__getattr__`，调用 `_sub_module_deprecation(..., private_modules=["_csr"], ...)` 把请求转发给真正的 `_csr.py` 并发警告。

[`base.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/base.py)、[`coo.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/coo.py) 是同样的模板，只是 `__all__` 和 `private_modules` 指向不同。[`sparsetools.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/sparsetools.py) 更极端——`__all__` 是空列表，纯粹是为了拦截 `from scipy.sparse.sparsetools import ...` 这种历史用法。

> 小细节：磁盘上其实有 **15** 个单字母前缀垫片文件（多出一个 `spfuncs.py`），但 `__init__.py` 的弃用 import 块只显式列了 14 个（不含 `spfuncs`）。`spfuncs.py` 同样是「will be removed in SciPy v2.0.0」的垫片，只是没被挂进 `__init__` 的导入块。

#### 4.3.4 代码实践

**实践目标**：亲眼看到弃用垫片「会抱怨」。

**操作步骤**：

```python
import warnings
warnings.simplefilter("always")          # 确保警告会被显示

# 老式写法：从垫片子模块取属性 → 触发 __getattr__ → 报 DeprecationWarning
from scipy.sparse.csr import csr_matrix  # 预期：弹出 DeprecationWarning
```

**需要观察的现象 / 预期结果**：

- 控制台应出现一条 `DeprecationWarning`，大意是 `scipy.sparse.csr` 模块已弃用、请改用 `scipy.sparse` 命名空间，并说明将在 v2.0.0 移除。
- `csr_matrix` 仍然能被正常取到（它转发自 `_csr.py`），代码不会报错，只是带着警告。

**对照（推荐写法）**：

```python
from scipy.sparse import csr_array   # 新代码：直接从顶层导入 *_array
```

> 待本地验证：确切的警告文案以你安装的 SciPy 版本为准；老项目里看到这类 `from scipy.sparse.xxx import ...` 时，应按提示迁移到顶层导入。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `from . import (base, bsr, ...)` 把垫片导入 `__init__`，但 `import scipy.sparse` 时并不会立刻看到一堆弃用警告？
**答案**：因为 `import` 一个模块只**执行该模块的顶层代码**，垫片的顶层代码里并没有发警告的逻辑；发警告的逻辑写在 `__getattr__` 里，只有当**访问垫片内的属性**（如 `csr_matrix`）时才触发。所以单纯 `import scipy.sparse` 是安静的。

**练习 2**：垫片模块和真正的实现模块在命名上的对应关系是什么？
**答案**：去掉下划线即可。`csr.py`（垫片）↔ `_csr.py`（实现），`base.py` ↔ `_base.py`，`sparsetools.py` ↔ `sparsetools/` 目录下的编译扩展（垫片里写的是 `private_modules=["_sparsetools"]`）。垫片的 `__getattr__` 通过 `private_modules` 参数指明真正的实现来源。

---

### 4.4 `_submodules` 与惰性导入：`csgraph` 与 `linalg`

#### 4.4.1 概念说明

`csgraph`（图算法）和 `linalg`（线性代数）是两个**很重**的子模块——它们各自又依赖 SuperLU、ARPACK、PROPACK 等大型数值库。如果在 `import scipy.sparse` 时就把它们全加载，启动会变慢，而且很多只想用基础稀疏数组的用户根本用不到它们。

解决办法是**惰性导入**：`__init__.py` **不**在顶部 `import csgraph`/`linalg`，而是只把它们的名字登记进 `_submodules` 列表，并实现一个模块级 `__getattr__`。当用户**第一次**写 `scipy.sparse.linalg` 时，这个 `__getattr__` 才被触发，真正去加载子模块。

#### 4.4.2 核心流程

```text
import scipy.sparse            ← 此时 linalg 尚未加载（快）
       │
       ▼ （很久以后）
x = scipy.sparse.linalg        ← Python 在 sparse 命名空间里找不到 'linalg' 属性
       │
       ▼
触发 sparse.__getattr__("linalg")
       │
       ▼  发现 "linalg" ∈ _submodules
importlib.import_module("scipy.sparse.linalg")
       │
       ▼
加载完成，返回该模块；后续访问直接命中缓存
```

这是 PEP 562（模块级 `__getattr__`）的典型应用，等价于「按需加载的属性」。

#### 4.4.3 源码精读

子模块登记在一行，[`__init__.py:271`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/__init__.py#L271)：

```python
_submodules = ["csgraph", "linalg"]
```

它被 `__all__` 拼接（4.2 已见），所以 `csgraph`/`linalg` 是合法的公开属性名；同时它又被下面的 `__getattr__` 用作「是否需要惰性加载」的判据。

惰性加载逻辑在 [`__init__.py:283-292`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/__init__.py#L283-L292)：

```python
def __getattr__(name):
    if name in _submodules:
        return _importlib.import_module(f'scipy.sparse.{name}')
    else:
        try:
            return globals()[name]
        except KeyError:
            raise AttributeError(
                f"Module 'scipy.sparse' has no attribute '{name}'"
            )
```

读法：当访问 `scipy.sparse.X` 且 `X` 不在已加载的命名空间里时——

- 若 `X` 是 `csgraph` 或 `linalg`，用 `importlib` 现场加载 `scipy.sparse.X` 并返回；
- 否则先尝试从 `globals()` 找（兜底），找不到就抛标准的 `AttributeError`。

注意顶部 [`__init__.py:248`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/__init__.py#L248) 有 `import importlib as _importlib`，正是为这里的 `_importlib.import_module(...)` 服务。

#### 4.4.4 代码实践

**实践目标**：用 `sys.modules` 观察「访问前 / 访问后」子模块是否被加载，直观体会惰性导入。

**操作步骤**：

```python
import sys, scipy.sparse as sp

# 访问前
print('scipy.sparse.linalg' in sys.modules)   # 预期: False（尚未加载）

# 第一次访问 → 触发 __getattr__ → 真正加载
_ = sp.linalg

# 访问后
print('scipy.sparse.linalg' in sys.modules)   # 预期: True（已加载）
print(sp.linalg.__name__)                     # 预期: scipy.sparse.linalg
```

**需要观察的现象 / 预期结果**：

- 第一行打印 `False`：证明 `import scipy.sparse` 并没有顺带把 `linalg` 加载进来。
- 访问 `sp.linalg` 之后第二行变 `True`：惰性导入生效。

> 待本地验证：极少数环境下（例如某些 IDE 预导入、或之前已访问过）第一行可能已是 `True`；可在全新 Python 进程里测试以获得干净结果。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `_submodules` 里 `"linalg"` 删掉，`scipy.sparse.linalg` 还能用吗？
**答案**：不能。删掉后 `__getattr__("linalg")` 走 `else` 分支，`globals()` 里也没有 `linalg`，于是抛 `AttributeError: Module 'scipy.sparse' has no attribute 'linalg'`。`_submodules` 既是「公开 API 名单」，也是「允许惰性加载的白名单」。

**练习 2**：惰性导入相比「顶部直接 `from . import linalg, csgraph`」有什么收益？
**答案**：主要收益是**启动速度与按需付费**——只用基础稀疏数组的程序不必加载 linalg/csgraph 及其背后的 SuperLU/ARPACK 等重型依赖；同时也避免了潜在的循环导入风险。代价是首次访问有一次加载延迟，以及 `dir(scipy.sparse)` 之类的静态反射需要靠 `__all__`/`__dir__` 来弥补。

---

## 5. 综合实践

把本讲的四块知识串起来，完成下面这个「结构勘探」小任务，画一张属于你自己的依赖关系草图。

**任务**：

1. 在 Python 中 `import scipy.sparse as sp`，然后打印以下四者的「来源」：

   ```python
   import scipy.sparse as sp, pkgutil

   print("csr_array  →", sp.csr_array.__module__)
   print("coo_array  →", sp.coo_array.__module__)
   print("linalg     →", type(sp.linalg), sp.linalg.__name__)
   print("csgraph    →", type(sp.csgraph), sp.csgraph.__name__)
   ```

2. 用一条命令列出 `scipy.sparse` 包里所有**以下划线开头**的核心模块：

   ```python
   print([m.name for m in pkgutil.iter_modules(sp.__path__)
          if m.name.startswith('_')])
   ```

3. 基于第 1、2 步的结果，结合 [`__init__.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/__init__.py) 与 [`meson.build`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/meson.build)，手绘一张依赖关系草图（文本即可），形如：

   ```text
   scipy.sparse (__init__.py 门面)
   ├── _base.py            _spbase / sparray / _formats          （基类，见 U2-L1）
   ├── _data.py            _data_matrix                          （逐元素运算基类）
   ├── _compressed.py      CSR/CSC/BSR 公共压缩基类
   │   ├── _csr.py         csr_array / csr_matrix
   │   ├── _csc.py         csc_array / csc_matrix
   │   └── _bsr.py         bsr_array / bsr_matrix
   ├── _coo.py             coo_array / coo_matrix                （坐标/三元组）
   ├── _lil.py / _dok.py / _dia.py                               （增量/对角格式）
   ├── _index.py           IndexMixin                            （索引机制，见 U3-L2）
   ├── _construct.py       eye_array/diags_array/kron/...        （构造工厂）
   ├── _extract.py / _matrix_io.py                               （提取 / npz 持久化）
   ├── _matrix.py          spmatrix 命名空间基类                 （旧接口）
   ├── _sputils.py / _spfuncs.py                                 （工具函数）
   ├── _generate_sparsetools.py ──▶ sparsetools/ (C++ 后端)      （代码生成，见 U3-L6）
   ├── [垫片] base.py/coo.py/csr.py/... （v2.0 移除）             （兼容层，见 4.3）
   ├── csgraph/ （惰性导入）                                       （图算法，见 U5）
   └── linalg/  （惰性导入）                                       （线性代数，见 U4）
   ```

**预期结果**：

- 第 1 步：`csr_array`/`coo_array` 的 `__module__` 分别指向 `scipy.sparse._csr` / `scipy.sparse._coo`；`linalg`/`csgraph` 是 `<class 'module'>`，`__name__` 分别是 `scipy.sparse.linalg` / `scipy.sparse.csgraph`。
- 第 2 步：应列出约 18 个 `_xxx` 模块，包括 `_base, _bsr, _compressed, _construct, _coo, _csc, _csr, _data, _dia, _dok, _extract, _generate_sparsetools, _index, _lil, _matrix, _matrix_io, _spfuncs, _sputils`。（若构建产物存在，可能还含编译扩展 `_csparsetools`。）

> 待本地验证：第 2 步的精确列表与是否包含编译扩展，取决于你的安装方式与构建产物，请以实际输出为准。

完成草图后，你应该能回答：**「我要改 CSR 的构造逻辑，去哪个文件？我要加一个用户能用的工具函数，要改哪两处？」**——前者去 `_csr.py`/`_compressed.py`，后者改 `_xxx.py` + `__init__.py`（+ `meson.build` 若是新文件）。

## 6. 本讲小结

- `scipy/sparse/` 分四层：**Python 源码层**（`_xxx.py`）、**C++ 计算后端**（`sparsetools/`，经 Cython 暴露为 `_csparsetools`）、**应用子模块**（`csgraph`/`linalg`）、**测试层**（`tests/`）；构建顺序由 [`meson.build`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/meson.build) 的 `subdir(...)` 决定。
- [`__init__.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/__init__.py) 是门面：用一连串 `from ._xxx import *` 把分散在各 `_xxx.py` 的公开类/函数汇聚到 `scipy.sparse` 命名空间。
- `__all__` 用 `dir()` **自动生成**（[`__init__.py:273`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/__init__.py#L273)），免去手工维护公开 API 清单。
- `base.py`/`csr.py`/`coo.py`/… 这 **14 个被显式 import 的单字母前缀垫片**（磁盘上共 15 个，多出 `spfuncs.py`）是 v2.0 待移除的兼容层，靠 PEP 562 的 `__getattr__` + `_sub_module_deprecation` 转发到真正的 `_xxx.py` 并报弃用警告。
- `csgraph`/`linalg` 通过 `_submodules`（[`__init__.py:271`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/__init__.py#L271)）+ 模块级 `__getattr__`（[`__init__.py:283-292`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/__init__.py#L283-L292)）实现**惰性导入**，按需加载重型依赖。
- 选文件口诀：**实现看 `_xxx.py`，公开看 `__init__.py`，安装看 `meson.build`**。

## 7. 下一步学习建议

本讲只看了「骨架」，还没真正进入任何一种格式的内部。建议按以下顺序继续：

1. **U1-L3（构建系统：meson / cython / sparsetools）**：补齐本讲只点到为止的 C++ 后端构建链，弄清 `_csparsetools.pyx.in` 经 tempita、Cython 变成扩展模块的过程。
2. **U1-L4（第一个稀疏数组与基本操作）**：动手用 `coo_array`/`csr_array` 做第一次实操，把本讲的「结构认知」落到「能跑的代码」。
3. 之后进入 **U2（七种格式与类继承体系）**，从 [`_base.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py) 的 `_spbase` 出发，逐一精读各 `_xxx.py` 的存储布局——那时你会反复回到本讲的依赖草图，定位「这一段逻辑在哪个文件」。
