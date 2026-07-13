# 项目定位与整体概览

> 这是 NumPy 学习手册的第一篇。本篇不要求你写任何复杂代码，目标是带你从「零认识」走到「能在源码里找到 NumPy 的自我介绍和家底清单」。

## 1. 本讲目标

读完本讲，你应当能够：

- 说清楚 **NumPy 是什么**、它在 Python 科学计算生态里扮演什么角色；
- 用一句话讲明白 NumPy 提供的 **四大核心能力**；
- 在源码中找到项目的 **元信息**（名称、版本号、依赖的构建工具、Python 版本要求）；
- 解释 `import numpy as np` 之后，顶层命名空间里的对象（比如 `np.array`、`np.ndarray`、`np.cos`）**从哪里来**；
- 独立完成一次「阅读 README + pyproject.toml + `numpy/__init__.py`」的源码勘察，并写一段简短的项目说明。

## 2. 前置知识

本讲默认你具备以下基础（都不深，会一点即可）：

- **Python 基础**：会写 `import`、知道什么是模块（module）和包（package）、什么是 `__init__.py`。
- **终端基础**：能在命令行里 `git clone`、运行 `python -c "..."`。
- **一点「包管理」直觉**：听说过 `pip install`，知道 Python 项目通常有一个 `pyproject.toml` 描述「这个包叫什么、怎么构建」。

你**不需要**提前懂 C、Cython、Meson 或线性代数——这些在后续讲义中才会用到。本篇只看三类「元信息文件」：说明文档、构建配置、顶层导出文件。

> 名词速查
> - **构建后端（build backend）**：把源码编译/打包成可安装产物的工具。NumPy 用的是 `meson-python`。
> - **C 扩展**：用 C 语言写成、被 Python 以模块形式导入的代码，性能比纯 Python 高。NumPy 的核心数组就是 C 扩展。
> - **N 维数组（N-dimensional array）**：可以是一维、二维、三维……的同类元素网格，是 NumPy 的核心数据结构。

## 3. 本讲源码地图

本讲只看三个「入口级」文件，它们回答了「NumPy 是谁、怎么构建、对外暴露什么」：

| 文件 | 作用 | 本讲用来看什么 |
| --- | --- | --- |
| `README.md` | 项目的对外说明书 | NumPy 的自我定位和四大能力 |
| `pyproject.toml` | 包的元信息与构建配置 | 名称、版本、构建工具、Python 版本要求 |
| `numpy/__init__.py` | 顶层包入口 | `import numpy as np` 之后哪些对象可见、它们从哪来 |

此外会**轻量引用**两个与「版本号」相关的文件，用来解释一个常见困惑（`numpy/version.py` 为什么在源码里找不到）：

| 文件 | 作用 |
| --- | --- |
| `numpy/version.pyi` | 版本模块的类型存根（stub），声明了版本相关的字段 |
| `numpy/_build_utils/gitversion.py` | 构建时生成 `version.py` 的脚本 |
| `numpy/meson.build` | Meson 构建脚本，调用上面的脚本生成 `version.py` |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：先看 NumPy 的自我介绍，再看包的「身份证」，最后看顶层 API 是怎么汇聚出来的。

### 4.1 项目简介与核心能力（README）

#### 4.1.1 概念说明

打开任何开源项目的第一步，几乎都是读它的 `README`。README 是项目对世界的**第一句自我介绍**：它做什么、不做什么、怎么参与。对 NumPy 来说，README 把项目定位和「它能干什么」压缩在短短几行里。

NumPy（**Num**erical **Py**thon）是 Python 科学计算的**基础包（fundamental package）**。「基础」二字很关键：它本身不提供机器学习、绘图、数据分析这些上层功能，而是提供**底层的多维数组与运算能力**，让 Pandas、SciPy、scikit-learn、PyTorch 等几乎所有上层库都能站在它肩上。

#### 4.1.2 核心流程

阅读一个陌生项目 README 的通用流程：

1. 找**一句话定位**（"X is the …"）→ 知道它是什么。
2. 找**能力清单**（"It provides …"）→ 知道它能干什么。
3. 找**链接区**（官网、文档、源码、issue）→ 知道遇到问题去哪。
4. 找**测试方式**→ 知道怎么验证安装成功。

#### 4.1.3 源码精读

README 的第一句话就是 NumPy 的官方定位：

> NumPy is the fundamental package for scientific computing with Python.
>
> 见 [README.md:21](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/README.md#L21)

紧接着是项目链接区，给出官网、文档、源码、贡献指引和安全漏洞上报渠道：[README.md:23-29](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/README.md#L23-L29)。

最重要的「能力清单」只有四行：

```text
It provides:

- a powerful N-dimensional array object
- sophisticated (broadcasting) functions
- tools for integrating C/C++ and Fortran code
- useful linear algebra, Fourier transform, and random number capabilities
```

出处见 [README.md:31-36](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/README.md#L31-L36)。这四行就是 NumPy 的**四大核心能力**，本套手册的后续单元基本都在围绕它们展开：

1. **强大的 N 维数组对象**（`ndarray`）—— 一切的基础。
2. **精巧的（广播）函数**（ufunc）—— 让不同形状的数组能直接做逐元素运算。
3. **集成 C/C++ 与 Fortran 代码的工具**（C-API、f2py）—— 这是 NumPy 又快又能扩展的根源。
4. **线性代数、傅里叶变换、随机数能力**（`numpy.linalg`、`numpy.fft`、`numpy.random`）—— 三个开箱即用的子系统。

最后 README 给出了验证安装是否成功的方式：[README.md:38-42](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/README.md#L38-L42)。它要求 `pytest` 和 `hypothesis`，运行命令是：

```bash
python -c "import numpy, sys; sys.exit(numpy.test() is False)"
```

> 小提示：这条命令会调用 `numpy.test()`。为什么一个数据科学库会有 `.test()` 方法？答案藏在 `numpy/__init__.py` 的末尾，我们留到 4.3 节揭晓，也由后续 u1-l5 讲义专门讲。

#### 4.1.4 代码实践

1. **实践目标**：用「最小信息」复述 NumPy 是什么。
2. **操作步骤**：用编辑器或 `Read` 工具打开仓库根目录的 `README.md`，只读第 21–42 行。
3. **需要观察的现象**：注意这 20 行里**没有**任何 API、参数、代码示例，只有定位、链接、能力和测试方式。
4. **预期结果**：你能用自己的话回答「NumPy 是什么、提供哪四样东西」。
5. **待本地验证**：如果你想确认这四条能力对应到代码里，可以暂时跳过——后续单元（u2 创建数组、u4 ufunc、u5 linalg/fft、u6 random）会逐一对应。

#### 4.1.5 小练习与答案

**练习 1**：README 把 NumPy 称作 "fundamental package"，而不是 "framework"（框架）或 "application"（应用）。这个措辞暗示了什么？

> **参考答案**：暗示 NumPy 是**底层依赖**而非终端产品。它为别的库提供基础设施（数组 + 运算），自己不直接面向数据分析、建模等终端任务。

**练习 2**：四大核心能力里，哪一条最能解释「为什么 NumPy 比纯 Python 列表快得多」？

> **参考答案**：第 3 条「集成 C/C++ 与 Fortran 代码的工具」。NumPy 的数组运算核心是用 C 写的，绕开了 Python 解释器的逐元素开销。第 1 条提供数据结构，但「快」主要来自底层 C 实现。

---

### 4.2 包元信息与版本号（pyproject.toml、version.pyi）

#### 4.2.1 概念说明

如果说 README 是写给「人」看的自我介绍，那 `pyproject.toml` 就是写给「工具链」看的**身份证**。它告诉 pip、告诉构建工具：这个包叫什么名字、当前是哪个版本、要哪种工具来构建、最低需要什么 Python。

现代 Python 项目的标准做法是：在仓库根目录放一个 `pyproject.toml`，里面分若干 `[表]`（table）声明元信息。NumPy 用的也是这一套，但因为它含大量 C/Cython 代码，所以构建后端不是默认的 `setuptools`，而是 `meson-python`。

#### 4.2.2 核心流程

一个 `import numpy` 成功的背后，元信息的流转大致是：

1. pip 读取 `pyproject.toml` 的 `[build-system]`，知道要安装 `meson-python` 和 `Cython` 作为**构建依赖**。
2. pip 调用 `meson-python` 后端，后者读取 `numpy/meson.build` 等构建脚本，编译 C 扩展。
3. 构建过程中，脚本调用 `gitversion.py` 生成 `numpy/version.py`，把**版本号 + git 提交哈希**写进去。
4. 安装完成后，`numpy/__init__.py` 里 `from .version import __version__` 就能拿到真实版本。

版本号的拼装规则（仅开发版附加 git 信息）：

\[
\text{full\_version} =
\begin{cases}
\text{version} + \text{"+git}\langle date\rangle .\langle hash\rangle\text{"}, & \text{若为 dev 版}\\
\text{version}, & \text{否则（正式发布版）}
\end{cases}
\]

#### 4.2.3 源码精读

先看构建后端声明。`pyproject.toml` 最顶部就规定了「用什么工具构建」：

```toml
[build-system]
build-backend = "mesonpy"
requires = [
    "meson-python>=0.18.0",
    "Cython>=3.1.0",  # keep in sync with version check in meson.build
]
```

出处见 [pyproject.toml:1-6](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/pyproject.toml#L1-L6)。注意两点：

- **构建后端是 `mesonpy`**（即 meson-python），不是 `setuptools`。这是 NumPy 2.x 之后的关键变化。
- **构建依赖**包含 `meson-python` 和 `Cython`——因为很多 `.pyx` 源文件要先被 Cython 翻译成 C，再由 Meson 编译。

再看包的基本元信息：

```toml
[project]
name = "numpy"
version = "2.6.0.dev0"
description = "Fundamental package for array computing in Python"
authors = [{name = "Travis E. Oliphant et al."}]
requires-python = ">=3.12"
readme = "README.md"
```

出处见 [pyproject.toml:8-17](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/pyproject.toml#L8-L17)。这里能读出：

- **包名**：`numpy`
- **版本号**：`2.6.0.dev0`（`dev0` 表示开发中的预发布版本）
- **一句话描述**："Fundamental package for array computing in Python"（与 README 的措辞一致）
- **最低 Python**：`>=3.12`（NumPy 2.6 要求较新的 Python）
- **README 文件**：`README.md`（pip 会把它作为 PyPI 页面的长描述）

`pyproject.toml` 还注册了命令行脚本入口：

```toml
[project.scripts]
f2py = 'numpy.f2py.f2py2e:main'
numpy-config = 'numpy._configtool:main'
```

见 [pyproject.toml:85-87](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/pyproject.toml#L85-L87)。安装后，终端里就有了 `f2py` 命令（用于把 Fortran 代码编译成 Python 扩展，对应八大能力里的第 3 条），以及 `numpy-config`（查询构建配置）。

> 关于版本号的「失踪」谜题
>
> 你在源码树里**找不到 `numpy/version.py`**，只能找到 `numpy/version.pyi`。`.pyi` 是类型存根（stub），只声明字段、不提供真实值：
>
> ```python
> from typing import Final, LiteralString
>
> version: Final[LiteralString] = ...
> __version__: Final[LiteralString] = ...
> full_version: Final[LiteralString] = ...
> git_revision: Final[LiteralString] = ...
> release: Final[bool] = ...
> short_version: Final[LiteralString] = ...
> ```
>
> 见 [numpy/version.pyi:1-9](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/version.pyi#L1-L9)。注意 `= ...` 只是占位符，不是真实值。
>
> 真实的 `version.py` 是**构建时生成**的。Meson 构建脚本里有一段：
>
> ```meson
> # Generate version.py for sdist
> meson.add_dist_script(...)
> if not fs.exists('version.py')
>   generate_version = custom_target(
>     'generate-version',
>     output: 'version.py',
>     input: '_build_utils/gitversion.py',
>     command: [py, '@INPUT@', '--write', '@OUTPUT@'],
>     ...
>   )
> endif
> ```
>
> 见 [numpy/meson.build:340-356](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/meson.build#L340-L356)。生成逻辑在 `gitversion.py` 里：它先从 `pyproject.toml` 读出 `version =` 那一行得到基础版本，再用 `git log` 取提交哈希，对开发版拼上 `+git<date>.<hash>`，最后把模板写进 `version.py`：

```python
# 基础版本来自 pyproject.toml
version_line = next(line for line in data if line.startswith('version ='))
# ...
# 只有开发版才追加 git 信息
if 'dev' in version:
    version += f'+git{git_date}.{git_hash[:7]}'
```

见 [numpy/_build_utils/gitversion.py:7-18](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_build_utils/gitversion.py#L7-L18) 与 [numpy/_build_utils/gitversion.py:51-52](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_build_utils/gitversion.py#L51-L52)。生成模板里还有一个判断：

```python
release = 'dev' not in version and '+' not in version
```

见 [numpy/_build_utils/gitversion.py:81](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_build_utils/gitversion.py#L81)。它的含义是：只要版本号里含 `dev` 或 `+`，就认为不是正式发布版（`release = False`）。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看到「源码树里没有 `version.py`」，并理解它从哪来。
2. **操作步骤**：
   - 在仓库根目录执行 `ls numpy/version.py`（应报「不存在」）。
   - 再执行 `ls numpy/version.pyi`（应存在）。
   - 打开 `pyproject.toml`，确认 `version = "2.6.0.dev0"`。
3. **需要观察的现象**：源码里只有存根 `version.pyi`，没有真实的 `version.py`。
4. **预期结果**：你解释得出「`version.py` 是 Meson 构建时由 `gitversion.py` 生成的」，而不是手写在仓库里。
5. **待本地验证**：若你已完成一次从源码构建（见 u1-l2），可在安装目录里 `python -c "import numpy, numpy.version as v; print(v.version, v.release)"`，观察开发版会带 `+git...` 后缀且 `release` 为 `False`。未构建前请勿假设能直接 `import numpy.version` 成功。

#### 4.2.5 小练习与答案

**练习 1**：为什么 NumPy 要把 `version.py` 设成「构建时生成」，而不是直接手写进仓库？

> **参考答案**：因为版本号里要带 **git 提交哈希和提交日期**（尤其对开发版），这些信息只有在构建那一刻才能确定，无法提前写死。让构建脚本去查 `git log` 生成，能保证每个构建产物的版本号精确对应当时的代码状态。

**练习 2**：`requires-python = ">=3.12"` 这一行对使用者意味着什么？

> **参考答案**：意味着用 pip 安装这个版本的 NumPy 时，Python 解释器必须至少是 3.12；低于 3.12 的环境会被 pip 直接拒绝安装。

---

### 4.3 顶层导入与公开 API 集合（numpy/__init__.py）

#### 4.3.1 概念说明

当你写下 `import numpy as np`，Python 会执行 `numpy/__init__.py`。这个文件就是 NumPy 的「前台」：它决定**哪些名字能被用户看到、哪些被藏起来**。理解这个文件，你就理解了「`np.xxx` 到底从哪冒出来」。

NumPy 的代码其实是**分层**的：

- 真正干活的「核心」（数组、ufunc、C 扩展）在 `numpy/_core/`，很多是 C 写的；
- 一些纯 Python 的工具函数在 `numpy/lib/`；
- 子系统在各自的子包（`linalg`、`fft`、`random` 等）。

而 `numpy/__init__.py` 的职责，就是把这些分散在各处的有用对象，**重新汇聚（re-export）到顶层 `np.` 命名空间**，让用户只需 `import numpy as np` 就能用 `np.array`、`np.mean`、`np.cos`。

#### 4.3.2 核心流程

`import numpy` 时，`__init__.py` 的执行脉络（精简版）：

```
1. 导入 version 模块 → 得到 __version__
2. （分发器钩子）from . import _distributor_init
3. 导入构建配置 → from numpy.__config__ import show_config
4. from . import _core          ← 核心 C 扩展在这里
5. from ._core import (array, ndarray, cos, mean, ... 几百个名字)
6. from .lib._xxx_impl import (一堆工具函数)
7. 组装 __all__                  ← 公开 API 集合
8. 定义 __getattr__              ← 懒加载子模块（linalg/fft/random...）
9. 注册 test = PytestTester(...)  ← 提供 numpy.test()
10. _sanity_check()              ← 导入时自检（防 BLAS ABI 错误）
```

其中第 8 步「懒加载」是个关键设计：像 `np.linalg`、`np.random` 这些子系统**不在导入时就加载**，而是等你第一次访问 `np.linalg` 时，才由 `__getattr__` 触发真正的 `import`。这样可以让 `import numpy` 更快。

#### 4.3.3 源码精读

文件开头的模块文档串，几乎是 README 能力清单的「Python 版」复述：

```python
"""
Provides
  1. An array object of arbitrary homogeneous items
  2. Fast mathematical operations over arrays
  3. Linear Algebra, Fourier Transforms, Random Number Generation
"""
```

见 [numpy/__init__.py:5-8](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L5-L8)。它和 README 的四大能力一一对应（前两条合并成「数组对象 + 快速运算」，后两条对应子系统）。

文档串后面还列出了**可用子包**与**工具函数**：[numpy/__init__.py:41-63](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L41-L63)，例如 `lib`、`random`、`linalg`、`fft`、`polynomial`、`testing`，以及 `test`、`show_config`、`__version__`。

紧接着是版本与配置导入：

```python
from . import version
from ._expired_attrs_2_0 import __expired_attributes__
from ._globals import _CopyMode, _NoValue
from .version import __version__
```

见 [numpy/__init__.py:90-93](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L90-L93)。注意 `from .version import __version__`——这里的 `version` 就是 4.2 节讲过的、构建时生成的那个模块。

然后是核心导入。最重要的两行：

```python
from . import _core
from ._core import (
    False_,
    ...
    array,       # ← np.array 的来源
    ...
    ndarray,     # ← np.ndarray 的来源
    ...
    ufunc,       # ← np.ufunc 的来源
    ...
)
```

见 [numpy/__init__.py:119-120](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L119-L120) 起的整段导入。其中能定位到几个标志性名字：

- `np.array` 来自 [numpy/__init__.py:148](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L148)
- `np.ndarray` 来自 [numpy/__init__.py:340](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L340)
- `np.ufunc` 来自 [numpy/__init__.py:421](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L421)

这一大段导入到 [numpy/__init__.py:443](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L443) 的右括号结束。**结论：顶层那些 `np.array` / `np.ndarray` / `np.cos` 等，几乎都来自 `_core`，而 `_core` 背后是 C 扩展 `_multiarray_umath`。**

接下来是从 `numpy/lib/` 的各个 `_*_impl` 模块导入纯 Python 工具函数，例如 `from .lib._function_base_impl import (...)`、`from .lib._stride_tricks_impl import (...)` 等。这些不展开，只需知道**它们的来源是 `numpy/lib`，而不是 `_core`**。

公开 API 集合 `__all__` 是**用集合的并集**拼出来的：

```python
__all__ = list(
    __numpy_submodules__ |
    set(_core.__all__) |
    set(_mat.__all__) |
    set(lib._histograms_impl.__all__) |
    ...
    {"emath", "show_config", "__version__", "__array_namespace_info__"}
)
```

见 [numpy/__init__.py:674-693](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L674-L693)。其中 `__numpy_submodules__` 列出了所有「公开子模块」：[numpy/__init__.py:626-630](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L626-L630)。这正是 `from numpy import *` 时会被导出的全部名字的来源。

**懒加载**靠的是模块级 `__getattr__`：

```python
def __getattr__(attr):
    ...
    if attr == "linalg":
        import numpy.linalg as linalg
        return linalg
    elif attr == "fft":
        import numpy.fft as fft
        return fft
    ...
```

见 [numpy/__init__.py:700-769](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L700-L769)。也就是说，你写 `np.linalg` 时并不会在 `import numpy` 那一刻就加载 `linalg`，而是**第一次访问**时才触发。这一段还顺便处理了「已废弃/已移除属性」的友好报错（[numpy/__init__.py:759-767](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L759-L767)），所以你访问 `np.int` 之类被移除的别名时，会得到一条清晰的错误提示。

最后，文件末尾注册了 `test` 方法并做了一次导入时自检：

```python
from numpy._pytesttester import PytestTester
test = PytestTester(__name__)
```

见 [numpy/__init__.py:782-784](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L782-L784)。这就是为什么会有 `numpy.test()`——它是一个 `PytestTester` 实例，把 pytest 包装进了 NumPy 自身。紧接着的 `_sanity_check()` 会在每次导入时跑一个极小的点积运算，确保链接的 BLAS 库没出 ABI 问题：[numpy/__init__.py:786-810](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L786-L810)。

> 名词解释：BLAS（Basic Linear Algebra Subroutines）是线性代数运算的底层库标准，OpenBLAS、MKL、Accelerate 都是它的实现。如果链接错了 ABI（比如把 32 位接口的库接到期望 64 位接口的代码上），会出现「能跑但结果错」的诡异 bug，`_sanity_check` 就是为了早期发现这类问题。

#### 4.3.4 代码实践

1. **实践目标**：亲手验证「顶层名字都来自 `_core`」并观察懒加载。
2. **操作步骤**（在已安装 NumPy 的环境里）：
   ```python
   import numpy as np

   # 1) 看 array / ndarray 来自哪个模块
   print(np.array.__module__)
   print(np.ndarray.__module__)

   # 2) 看 __all__ 有多大
   print(len(np.__all__))

   # 3) 观察懒加载：访问前 linalg 还没被加载
   import sys
   print("linalg" in sys.modules)   # 多半是 False
   _ = np.linalg                     # 第一次访问，触发 import
   print("linalg" in sys.modules)   # 变成 True
   ```
3. **需要观察的现象**：
   - `np.array.__module__` 通常形如 `numpy` 或 `numpy._core.multiarray`（取决于版本），其根源是 `_core`。
   - `np.__all__` 是几百个名字的大集合。
   - `np.linalg` 访问前后，`sys.modules` 里 `"numpy.linalg"` 从无到有。
4. **预期结果**：你能用一句话说清「`np.array` 来自 `_core`，而 `np.linalg` 是懒加载的」。
5. **待本地验证**：`__module__` 的精确字符串取决于具体构建；若与上面描述略有出入，以你本机输出为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `np.array`、`np.ndarray` 来自 `_core`，而 `np.median`、`np.percentile` 来自 `numpy/lib`？

> **参考答案**：`array`/`ndarray` 是**底层数组对象与构造**，由 C 扩展直接提供，属于 `_core`；`median`/`percentile` 是**基于数组的统计工具函数**，用纯 Python 组合 `_core` 的能力即可实现，因此放在 `numpy/lib`。划分原则是「核心 vs. 组合工具」。

**练习 2**：`__all__` 里出现的 `__numpy_submodules__`（linalg/fft/random/…）和 `_core.__all__` 的「并集」有什么好处？

> **参考答案**：用并集可以自动汇集各子包自己声明的公开名字，避免在顶层**手动维护一份易过期的清单**。每当 `_core` 或某个 `lib._xxx_impl` 新增/删除公开对象，顶层 `__all__` 会自动跟着更新，降低维护成本。

**练习 3**：访问 `np.int` 会发生什么？为什么？

> **参考答案**：会抛 `AttributeError`，并提示 `np.int` 已在 NumPy 2.0 移除/曾经是内置类型的别名。这是因为 `__getattr__` 里检查了 `__former_attrs__` / `__expired_attributes__`，给出友好报错而不是无信息的「找不到」。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「项目勘察」小任务（对应大纲里的实践要求）：

**任务**：假设你要向一个从没用过 NumPy 的同事用 200 字以内介绍它。请：

1. **阅读** `README.md`（第 21–42 行）与 `pyproject.toml`（第 1–17 行、第 85–87 行）。
2. **写一段约 200 字的中文说明**，必须覆盖：
   - NumPy 是什么（用 README 的 "fundamental package" 措辞改写）；
   - 它依赖哪些**构建工具**（`meson-python`、`Cython`）；
   - 它要求的最小 Python 版本；
   - 它提供哪四大能力。
3. **列出 `np.ndarray` 之外，你能在顶层 `np.` 导入的 5 个对象**，并标注每个来自 `_core` 还是 `lib`。你可以从 `numpy/__init__.py` 的导入块（[numpy/__init__.py:120-443](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L120-L443) 与 [numpy/__init__.py:454-620](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L454-L620)）里挑。

**参考答案示例**（供自检，非唯一答案）：

> NumPy 是 Python 科学计算的基础包，用 C/Cython 写成核心，速度远超纯 Python 列表。它通过 `meson-python` 后端构建，构建期还需要 Cython 把 `.pyx` 翻译成 C；当前版本要求 Python ≥ 3.12。它提供四样东西：强大的 N 维数组对象、带广播的逐元素函数（ufunc）、集成 C/C++/Fortran 的工具（含 `f2py`），以及线性代数、傅里叶变换和随机数等开箱即用的子系统。

5 个顶层对象示例（`np.ndarray` 之外）：

| 对象 | 来自 | 用途 |
| --- | --- | --- |
| `np.array` | `_core` | 从列表等创建数组 |
| `np.cos` | `_core`（ufunc） | 逐元素余弦 |
| `np.mean` | `_core` | 求均值 |
| `np.median` | `lib`（`_function_base_impl`） | 求中位数 |
| `np.linspace` | `_core` | 生成等差数列 |

> 自检要点：你列出的每个对象，都应当能在 `numpy/__init__.py` 的某个 `from ... import (...)` 块里被找到；`array/cos/mean/linspace` 在 `_core` 那块，`median` 在 `.lib._function_base_impl` 那块。

## 6. 本讲小结

- NumPy 是 Python 科学计算的 **基础包**，定位是「为上层库提供多维数组与运算底座」，本身不是终端框架。([README.md:21](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/README.md#L21))
- 它的 **四大核心能力**：N 维数组、广播函数、C/Fortran 集成工具、线性代数/FFT/随机数。([README.md:31-36](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/README.md#L31-L36))
- `pyproject.toml` 是包的「身份证」：构建后端是 `meson-python`，构建依赖含 `Cython`，要求 Python ≥ 3.12，当前版本 `2.6.0.dev0`。([pyproject.toml:1-17](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/pyproject.toml#L1-L17))
- 真实的 `numpy/version.py` **构建时才生成**（由 `gitversion.py` 读 `pyproject.toml` + git 哈希拼出），源码树里只有存根 `version.pyi`。
- `import numpy as np` 执行 `numpy/__init__.py`，它把 `_core` 的 C 扩展对象和 `lib` 的工具函数**汇聚到顶层命名空间**，并用 `__all__` 的并集自动维护公开集合。([numpy/__init__.py:119-443](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L119-L443))
- `np.linalg`/`np.fft`/`np.random` 等子系统是**懒加载**的（由 `__getattr__` 触发），导入时还会跑 `_sanity_check` 防止 BLAS ABI 错误。([numpy/__init__.py:700-769](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L700-L769))

## 7. 下一步学习建议

本讲只看了「元信息层」，还没有真正动手用 NumPy。建议接下来按顺序：

1. **u1-l2 构建与运行**：亲手用 `meson-python`/`spin` 把 NumPy 从源码编译一次，让你后续看的每一个 C 扩展都能在本地被验证。
2. **u1-l3 目录结构与导出**：在搞懂「顶层汇聚」之后，下钻看 `_core`、`lib`、`linalg` 等子包各自负责什么。
3. **u1-l4 ndarray 初体验**：开始真正创建数组、读 `shape`/`strides`/`dtype`，把本讲的「四大能力」第一条落到实处。

> 阅读源码建议：本讲引用的 `numpy/__init__.py` 是整个项目的「总目录」，将来你找不到某个函数属于哪个子系统时，回到这里看它的 `from ... import` 块，往往一眼就能定位。
