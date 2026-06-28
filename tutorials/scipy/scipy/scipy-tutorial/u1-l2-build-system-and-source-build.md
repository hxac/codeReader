# 构建系统与从源码编译运行

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 SciPy「为什么需要一个复杂的构建系统」：它是一个 Python、C、C++、Cython、Fortran、Pythran 多语言混合的项目。
- 读懂三个核心配置文件：`pyproject.toml`（声明构建后端与依赖）、`meson.build`（顶层构建编排）、`meson.options`（构建可配置开关）。
- 理解 `meson-python` + `meson` + `ninja` 这条构建链是如何把源码编译成可导入的 `scipy` 包的。
- 学会两种从源码构建 SciPy 的方式：`spin build` 与 `pip install -e . --no-build-isolation`。
- 运行 `scipy.show_config()` 并解读输出，特别是 **BLAS/LAPACK 来自哪里**。

承接上一讲（u1-l1）：上一讲我们建立了 SciPy 的全局认知，并提到 `version.py` 是「构建时动态生成」的。本讲就来揭开「构建」这件事的全过程。

## 2. 前置知识

在进入源码前，先用大白话建立几个直觉。

### 2.1 为什么 SciPy 的构建不简单

绝大多数纯 Python 包的「安装」其实就是把 `.py` 文件拷贝到 `site-packages`。但 SciPy 不一样：

- 它的**高性能算法**用 C、C++、Fortran 写成，必须**编译**成机器码。
- 它大量使用 **Cython**（一种 Python 的「超集」语言，`.pyx` 文件会先被翻译成 C，再编译）。
- 它依赖 **BLAS/LAPACK**（线性代数底层库，最常见的是 OpenBLAS）来做矩阵运算。
- 它还用到 **Pythran**（把 Python 数值代码编译成高性能 C++）和 **pybind11**（C++ 与 Python 的胶水）。

所以 SciPy 的构建 = 「编译多种语言的源码 + 链接外部数学库 + 生成 Python 扩展模块（`.so`/`.pyd`）」。这远超 `setup.py` 能优雅处理的范围，于是 SciPy 选用了 **Meson** 构建系统。

### 2.2 三个关键角色

| 角色 | 是什么 | 在 SciPy 里的文件 |
|------|--------|------------------|
| **Meson** | 一个现代构建系统（类似 CMake 的替代品），用一种声明式语言描述「要编译什么、怎么编译」 | `meson.build`、`meson.options` |
| **Ninja** | Meson 调用的实际执行器（底层跑编译命令的工具，以快著称） | 由 Meson 自动调用 |
| **meson-python** | 把 Meson 包装成符合 Python 打包标准（PEP 517）的「构建后端」，这样 `pip` 才能驱动它 | 在 `pyproject.toml` 里声明 |

一句话关系：**`pip` → 调用 `meson-python` 后端 → 用 `meson` 解析 `meson.build` → 用 `ninja` 真正编译**。

### 2.3 BLAS / LAPACK 是什么

- **BLAS**（Basic Linear Algebra Subprograms）：底层线性代数运算（向量点积、矩阵乘法）的接口标准。
- **LAPACK**（Linear Algebra PACKage）：在 BLAS 之上构建的高阶例程（解线性方程组、特征值、SVD 等）。

它们只是「接口标准」，具体实现有很多种：**OpenBLAS**（开源，SciPy 默认）、**MKL**（Intel）、**Accelerate**（macOS 苹果自带）。本讲实践的关键，就是搞清楚你构建出的 SciPy 到底链接了哪一个实现。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [`pyproject.toml`](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/pyproject.toml) | 项目的「身份证」：声明构建后端、构建依赖、运行依赖、Python 版本要求、spin 命令注册 |
| [`meson.build`](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/meson.build) | 根级 Meson 脚本：定义项目、检查编译器、引入子项目、默认链接 OpenBLAS、进入 `scipy/` 子目录 |
| [`meson.options`](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/meson.options) | 用户可配置的构建开关：BLAS/LAPACK 切换、ILP64、Pythran 开关、系统库选择 |
| [`.spin/cmds.py`](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/.spin/cmds.py) | 开发命令行工具 `spin` 的实现：`build`/`test`/`docs` 等命令 |
| [`tools/gitversion.py`](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/tools/gitversion.py) | 构建时生成版本号的脚本（解释 u1-l1 提到的「动态生成 version.py」） |
| [`scipy/__config__.py.in`](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__config__.py.in) | `show_config()` 输出的模板文件，构建时被填入真实库信息 |

## 4. 核心概念与源码讲解

### 4.1 `pyproject.toml`：构建后端与依赖声明

#### 4.1.1 概念说明

`pyproject.toml` 是现代 Python 项目的标准配置文件（由 [PEP 518](https://peps.python.org/pep-0518/) 和 [PEP 517](https://peps.python.org/pep-0517/) 定义）。对 SciPy 而言，它回答三个问题：

1. **用什么构建？** → `[build-system]` 段指定 `mesonpy` 作为构建后端。
2. **构建时需要哪些工具？** → `requires` 列出 Cython、pybind11、pythran、numpy 等。
3. **运行时需要什么？安装到什么 Python？** → `[project]` 段声明。

它还有一个关键段 `[tool.spin.commands]`，把 `spin` 的各子命令注册进来。

#### 4.1.2 核心流程

```
pip 读 pyproject.toml
   ├── [build-system].build-backend = 'mesonpy'  → 决定用 meson-python 构建
   ├── [build-system].requires                     → 在「构建隔离环境」里安装这些构建工具
   └── meson-python 接管 → 读 meson.build → ninja 编译
```

> 注意「构建依赖」和「运行依赖」是两套东西。Cython/pythran 只在**构建时**需要（用来生成 C/C++ 代码），最终用户运行 SciPy 时并不需要安装它们。

#### 4.1.3 源码精读

先看构建后端的声明，这是整个构建的起点：

[pyproject.toml:17-26](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/pyproject.toml#L17-L26) —— 声明 `mesonpy` 为构建后端，并列出构建期依赖：

```toml
[build-system]
build-backend = 'mesonpy'
requires = [
    "meson-python>=0.15.0",
    "Cython>=3.2.0",        # when updating version, also update check in meson.build
    "pybind11>=2.13.2",     # when updating version, also update check in scipy/meson.build
    "pythran>=0.18.1",      # when updating version, also update check in meson.build
    "numpy>=2.0.0",
]
```

注意每行后面 `# when updating ...` 的注释：SciPy 在多个文件里**重复校验**这些版本下限（`meson.build`、`scipy/meson.build` 里都有对应检查），改一处必须同步改别处，这是大型项目避免「构建依赖与运行时不一致」的常见做法。

再看 `[project]` 段对 Python 与 NumPy 版本的硬性要求：

[pyproject.toml:41-44](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/pyproject.toml#L41-L44) —— 运行依赖与 Python 版本下限，注释要求与 `meson.build` 保持同步：

```toml
requires-python = ">=3.12"  # keep in sync with `min_python_level` in meson.build
dependencies = [
    "numpy>=2.0.0",
] # keep in sync with `min_numpy_version` in meson.build
```

SciPy 1.19 要求 Python \(\geq 3.12\)、NumPy \(\geq 2.0.0\)。

最后看 `spin` 命令的注册，这决定了你在命令行能敲哪些 `spin xxx`：

[pyproject.toml:269-285](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/pyproject.toml#L269-L285) —— 把 `.spin/cmds.py` 里的函数注册为 `spin` 的子命令：

```toml
[tool.spin.commands]
"Build & Develop" = [
  ".spin/cmds.py:build",
  ".spin/cmds.py:test",
  ...
]
"Environments" = [
  "spin.cmds.meson.run",
  ".spin/cmds.py:python",
  ...
]
```

可以看到 `spin` 既调用项目自定义命令（`.spin/cmds.py:build`），也复用 `spin` 自带的命令（`spin.cmds.meson.run`）。

#### 4.1.4 代码实践（源码阅读型）

**目标**：理解「构建依赖」与「运行依赖」是分开的。

**步骤**：

1. 打开 `pyproject.toml`，找到 `[build-system].requires`（构建期）和 `[project].dependencies`（运行期）。
2. 对比两者：构建期有 Cython/pythran/pybind11/meson-python/ninja，运行期只有 `numpy>=2.0.0`。
3. 再看 [dependency-groups](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/pyproject.toml#L87-L148) 段，观察 `build`、`test`、`doc` 等依赖组是如何组合的。

**需要观察的现象**：构建期依赖（Cython 等）不在运行期 `dependencies` 里——这说明它们生成的 C 代码已经被编译进 `.so`，最终用户无需再装 Cython。

**预期结果**：你能用一句话解释「为什么 `pip install scipy` 的用户不需要安装 Cython」。

#### 4.1.5 小练习与答案

**练习 1**：如果想把构建所需的 Cython 版本下限提高到 `3.3.0`，需要改哪几个地方？
**答案**：至少改三处——`pyproject.toml` 的 `[build-system].requires`、`pyproject.toml` 的 `dependency-groups.build`，以及 `meson.build` 里对应版本检查（见 4.2.3）。这正是源码注释反复提醒的「keep in sync」。

**练习 2**：`build-backend = 'mesonpy'` 这一行如果删掉会怎样？
**答案**：`pip` 不知道该用哪个构建后端，无法构建。PEP 517 规定必须有构建后端；没有这一行时 pip 会回退到传统的 `setup.py`，而 SciPy 已经不再维护 `setup.py`，所以会直接失败。

### 4.2 `meson.build`：多语言编译的顶层编排

#### 4.2.1 概念说明

`meson.build` 是 Meson 构建系统的「主脚本」，位于项目根目录。它做四件大事：

1. **声明项目**：名字、所用编程语言、版本、默认选项（例如默认用 OpenBLAS）。
2. **环境检查**：找到 Python 安装、检查编译器版本是否够新。
3. **引入子项目（subprojects）**：SciPy 把一些第三方库（`xsf`、`unuran`、`array_api_compat` 等）作为 git submodule 内嵌进来。
4. **进入主源码目录**：调用 `subdir('scipy')`，把控制权交给 `scipy/meson.build`，由后者逐个编译各子包。

#### 4.2.2 核心流程

```
meson 读 meson.build
   ① project(...)  →  声明语言 c/cpp/cython，默认 blas=openblas
   ② 检查 Python 版本 ≥ 3.12、编译器（gcc≥9.1 / clang≥15 / msvc≥19.20）、Cython≥3.2
   ③ 校验 5 个 submodule（xsf/unuran/array_api_compat/array_api_extra/cobyqa）存在
   ④ subproject() 引入这些第三方库
   ⑤ 处理 use-system-libraries 选项（boost.math / qhull 用系统版还是内嵌版）
   ⑥ subdir('scipy')  →  交给 scipy/meson.build 编译所有子包
```

版本检查本质上是断言 SciPy 支持的工具链范围，记作一个区间约束：

\[
\text{gcc} \in [9.1,\,\infty),\quad \text{clang} \in [15.0,\,\infty),\quad \text{Cython} \in [3.2.0,\,\infty)
\]

#### 4.2.3 源码精读

先看 `project()` 声明——这是整个构建的根：

[meson.build:1-15](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/meson.build#L1-L15) —— 声明项目用 `c`/`cpp`/`cython` 三种语言，默认链接 OpenBLAS：

```meson
project(
  'scipy',
  'c', 'cpp', 'cython',
  version: run_command(['tools/gitversion.py'], check: true).stdout().strip(),
  license: 'BSD-3',
  meson_version: '>= 1.5.0',
  default_options: [
    'buildtype=debugoptimized',
    'b_ndebug=if-release',
    'c_std=c17',
    'cpp_std=c++17',
    'blas=openblas',
    'lapack=openblas'
  ],
)
```

要点：
- 第二行的 `'c', 'cpp', 'cython'` 直接说明了「三种语言都要编译」。
- `version` 不是写死的，而是**运行 `tools/gitversion.py` 动态算出来**的（4.4 节详述）。
- `default_options` 里的 `blas=openblas`、`lapack=openblas` 是**默认用 OpenBLAS**——这正是 `show_config()` 里会显示的内容。

再看编译器与 Python 版本检查：

[meson.build:27-30](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/meson.build#L27-L30) —— Python 版本下限检查（与 `pyproject.toml` 同步）：

```meson
min_python_version = '3.12'   # keep in sync with pyproject.toml
python_version = py3.language_version()
if python_version.version_compare(f'<@min_python_version@')
  error(f'Minimum supported Python version is @min_python_version@, found @python_version@')
endif
```

[meson.build:49-65](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/meson.build#L49-L65) —— 编译器版本检查（gcc≥9.1、clang≥15、msvc≥19.20、Cython≥3.2）：

```meson
if cc.get_id() == 'gcc'
  if not cc.version().version_compare('>=9.1')
    error('SciPy requires GCC >= 9.1')
  endif
elif cc.get_id() == 'clang' or cc.get_id() == 'clang-cl'
  if not cc.version().version_compare('>=15.0')
    error('SciPy requires clang >= 15.0')
  endif
...
if not cy.version().version_compare('>=3.2.0')
  error('SciPy requires Cython >= 3.2.0')
endif
```

然后是 submodule 存在性校验——这是新手构建 SciPy 时最常见的报错来源：

[meson.build:153-168](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/meson.build#L153-L168) —— 检查 5 个内嵌子项目是否已拉取：

```meson
# Subprojects
if not fs.exists('subprojects/array_api_compat/README.md')
  error('Missing the `array_api_compat` submodule! Run `git submodule update --init` to fix this.')
endif
...
if not fs.exists('subprojects/xsf/README.md')
  error('Missing the `xsf` submodule! Run `git submodule update --init` to fix this.')
endif
```

如果你 `git clone` 后忘了 `git submodule update --init`，构建会在这里报错。

最后是「系统库 vs 内嵌库」的选择，以及进入主目录：

[meson.build:206-243](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/meson.build#L206-L243) —— 根据 `use-system-libraries` 选项决定 boost.math/qhull 用系统版还是内嵌版，最后 `subdir('scipy')`：

```meson
use_system_libraries = get_option('use-system-libraries')
...
if all_system_libraries or use_system_libraries.contains('boost.math')
  boost_math_dep = dependency('boost', version : boost_version)
...
else
  boost_math = subproject('boost_math', version : boost_version)
  boost_math_dep = boost_math.get_variable('boost_math_dep')
endif
...
subdir('scipy')
```

进入 `scipy/meson.build` 后，那里会探测 NumPy 头文件目录、pybind11、pythran 等，再逐个子包编译——这些会在后续子包讲义中展开。这里只看一眼它如何拿到 NumPy 依赖：

[scipy/meson.build:24-24](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/meson.build#L24) —— 通过 `numpy-config` 可执行文件获取 NumPy 依赖（含头文件路径）：

```meson
_numpy_dep = dependency('numpy')
```

#### 4.2.4 代码实践（源码阅读型）

**目标**：理解 submodule 缺失时的报错链。

**步骤**：

1. 阅读 [meson.build:153-168](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/meson.build#L153-L168) 中 5 个 `fs.exists(...)` 检查。
2. 在仓库根目录执行（只读查看，不修改任何东西）：
   ```bash
   git submodule status
   ```
3. 对照检查列表：`array_api_compat`、`array_api_extra`、`cobyqa`、`unuran`、`xsf`。

**需要观察的现象**：每个 submodule 前面有一个标识符（空格=未初始化、`+`=已检出、`-`=未初始化且未注册）。

**预期结果**：你能说出「构建前必须保证这 5 个 submodule 都已 `git submodule update --init` 拉取」，并解释为什么 `meson.build` 要在编译前逐一 `fs.exists` 检查。

#### 4.2.5 小练习与答案

**练习 1**：`project()` 里写了 `'fortran'` 吗？SciPy 不是有 Fortran 代码吗？
**答案**：根 `meson.build` 的 `project()` 只声明了 `'c', 'cpp', 'cython'`。Fortran 相关的例程（如 BLAS/LAPACK 的 f2py 包装）在更细粒度的子目录里按需处理；现代 SciPy 正在把 Fortran 逐步迁移为 Cython/C++。这个细节属于后续讲义（u4 linalg）的范围，这里只需知道「顶层声明的语言不一定是全部」。

**练习 2**：`default_options` 里 `blas=openblas` 和 `meson.options` 里 `option('blas', value: 'openblas')` 是什么关系？
**答案**：`meson.options` 定义了 `blas` 这个**选项**及其默认值；`meson.build` 的 `default_options` 是在「项目未被用户覆盖前」的初始取值。两者一致地指向 OpenBLAS。用户可以用 `-Dblas=mkl` 覆盖（见 4.3）。

### 4.3 `meson.options`：构建的可配置开关

#### 4.3.1 概念说明

`meson.options` 是 Meson 的「用户可调参数表」。它定义了一批构建开关，让不同环境（conda-forge、Linux 发行版、macOS、CI）能用同一份源码构建出不同配置的 SciPy。每个 `option()` 声明一个开关的：名字、类型、默认值、可选取值、说明文字。

#### 4.3.2 核心流程

```
用户构建时可通过 -D<选项>=<值> 覆盖默认值，例如：
   spin build --setup-args=-Dblas=mkl
   pip install -e . --no-build-isolation -Csetup-args=-Duse-ilp64=true
meson 读 meson.options 得到选项定义 → 与 meson.build 的 default_options 合并 → 生效
```

#### 4.3.3 源码精读

最核心的两个选项——BLAS/LAPACK 切换：

[meson.options:1-4](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/meson.options#L1-L4) —— 定义 `blas`/`lapack` 选项，默认 `openblas`：

```meson
option('blas', type: 'string', value: 'openblas',
        description: 'option for BLAS library switching')
option('lapack', type: 'string', value: 'openblas',
        description: 'option for BLAS library switching')
```

这是一个 `string` 类型选项，理论上可填任意库名（`openblas`、`mkl`、`accelerate`、`blas` 等），但能否成功取决于该库是否能在系统里被探测到。

ILP64（64 位整数接口）相关选项——对超大规模矩阵很重要：

[meson.options:7-16](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/meson.options#L7-L16) —— 是否使用 64 位整数的 BLAS/LAPACK 接口：

```meson
option('use-ilp64', type: 'boolean', value: false,
       description: 'Use ILP64 (64-bit integer) BLAS and LAPACK interfaces')
option('cython-blas-abi', type: 'combo', choices: ['auto', 'lp64', 'ilp64'],
       value: 'auto', ...)
```

> BLAS 默认用 32 位整数（LP64）索引数组元素，最多约 \(2^{31}\) 个元素。当矩阵元素总数超过这个上限时，需要 ILP64（64 位整数）接口。SciPy 默认关闭它。

Pythran 开关与系统库选择：

[meson.options:23-30](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/meson.options#L23-L30) —— Pythran 开关与系统库选择：

```meson
option('use-pythran', type: 'boolean', value: true,
       description: 'If set to false, disables using Pythran ...')
option('use-system-libraries', type: 'array',
        choices : ['none', 'all', 'auto', 'boost.math', 'qhull'], value : ['none'],
        description: 'Choose which system libraries for subprojects ...')
```

- `use-pythran=false` 时，原本用 Pythran 编译的代码会回退到纯 Python 或 Cython 实现。
- `use-system-libraries` 控制 `boost.math`/`qhull` 是用系统已装的版本还是 SciPy 内嵌的版本——Linux 发行版打包时常用 `auto` 或 `all` 以复用系统库、减小体积。

#### 4.3.4 代码实践（源码阅读型）

**目标**：搞懂 `.spin/cmds.py` 的 `build` 命令如何把选项翻译成 meson 参数。

**步骤**：

1. 阅读 [.spin/cmds.py:131-143](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/.spin/cmds.py#L131-L143)，看 `--with-accelerate`、`--with-scipy-openblas`、`--use-system-libraries` 这三个开关如何变成 `-Dblas=...`、`-Duse-system-libraries=...`。

   ```python
   if with_accelerate:
       meson_args = meson_args + ("-Dblas=accelerate", )
   elif with_scipy_openblas:
       configure_scipy_openblas(with_scipy_openblas)
       ...
   if use_system_libraries:
       meson_args = meson_args + ("-Duse-system-libraries=auto",)
   ```

2. 对照 `meson.options`，确认这些 `-D<key>=<value>` 的 key 都在选项表里定义过。

**需要观察的现象**：`spin` 的高层开关（`--with-accelerate`）本质上只是把 `meson.options` 里定义的选项设成特定值的语法糖。

**预期结果**：你能说出「`spin build --with-accelerate` 等价于在 meson 层面 `-Dblas=accelerate`」。

#### 4.3.5 小练习与答案

**练习 1**：默认 `use-ilp64=false`。如果一个用户的矩阵有 \(5\times 10^{9}\) 个元素，是否需要打开它？
**答案**：需要。\(5\times 10^{9} > 2^{31} \approx 2.15\times 10^{9}\)，32 位整数无法索引这么大的数组，必须用 `use-ilp64=true` 构建（且 BLAS 实现也得是 ILP64 版本）。

**练习 2**：为什么 Linux 发行版（如 Fedora、Debian）打包 SciPy 时倾向于 `-Duse-system-libraries=auto`？
**答案**：发行版希望复用系统已有的 `boost`、`qhull` 等库，避免在 SciPy 包里重复携带一份，从而减小体积、统一安全更新。`auto` 表示「系统有就用系统的，没有就回退到内嵌 subproject」。

### 4.4 构建产物：版本号生成与 `show_config` 验证

#### 4.4.1 概念说明

构建不只是「编译」，它还会**生成一些 Python 文件**：

1. **`version.py`**：由 `tools/gitversion.py` 在构建时生成，写入版本号（含 git 提交信息）。这解释了 u1-l1 里「version.py 是动态生成」的悬念。
2. **`scipy/__config__.py`**：由 `scipy/__config__.py.in` 模板填充真实库信息后生成。`scipy.show_config()` 读的就是这个文件。

理解这两点，你就能回答「为什么源码里找不到 `scipy/version.py`，但安装后却存在」。

#### 4.4.2 核心流程

**版本号生成**：

```
tools/gitversion.py
   ├── init_version()：从 pyproject.toml 读 'version = "1.19.0.dev0"'
   └── git_version()：若是 dev 版，附加 '+git<日期>.<commit短哈希>'
        → 写入 scipy/version.py
```

**`__config__.py` 生成**：

```
meson 在配置阶段探测 BLAS/LAPACK/编译器信息
   → 把结果填入 scipy/__config__.py.in 的占位符（@BLAS_NAME@ 等）
   → 生成 scipy/__config__.py
   → 运行时 scipy.show_config() 打印其中的 CONFIG 字典
```

#### 4.4.3 源码精读

先看版本号如何动态生成：

[tools/gitversion.py:6-18](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/tools/gitversion.py#L6-L18) —— 从 `pyproject.toml` 读取基础版本号：

```python
def init_version():
    init = os.path.join(os.path.dirname(__file__), '../pyproject.toml')
    with open(init) as fid:
        data = fid.readlines()
    version_line = next(line for line in data if line.startswith('version ='))
    version = version_line.strip().split(' = ')[1]
    version = version.replace('"', '').replace("'", '')
    return version
```

[tools/gitversion.py:21-54](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/tools/gitversion.py#L21-L54) —— 对开发版附加 git 提交信息：

```python
def git_version(version):
    # ... 调用 git log -1 --format="%H %aI"
    # Only attach git tag to development versions
    if 'dev' in version:
        version += f'+git{git_date}.{git_hash[:7]}'
    return version, git_hash
```

所以从源码构建的开发版 `scipy.__version__` 形如 `1.19.0.dev0+git20260628.8149225`，末尾精确到提交。这正是 `meson.build:4` 里 `run_command(['tools/gitversion.py'], ...)` 调用的脚本。

再看 `show_config()` 的输出从哪来：

[scipy/__config__.py.in:72-102](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__config__.py.in#L72-L102) —— `Build Dependencies` 段记录构建时探测到的 BLAS/LAPACK 来源（模板里的占位符在构建时被替换）：

```python
"Build Dependencies": {
    "blas": {
        "name": "@BLAS_NAME@",
        "found": bool("@BLAS_FOUND@".lower().replace('false', '')),
        "version": "@BLAS_VERSION@",
        "detection method": "@BLAS_TYPE_NAME@",
        ...
        "openblas configuration": r"@BLAS_OPENBLAS_CONFIG@",
    },
    "lapack": { ... },
    ...
}
```

`@BLAS_NAME@` 这类占位符会在 `meson configure` 阶段被真实值替换（如 `openblas`）。

而 `show_config` 在 `scipy/__init__.py` 里只是一个别名：

[scipy/__init__.py:47](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L47) —— 把 `scipy.__config__.show` 重命名为公共 API `show_config`：

```python
from scipy.__config__ import show as show_config
```

`show()` 函数本身支持两种输出模式：

[scipy/__config__.py.in:117-166](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__config__.py.in#L117-L166) —— `show()` 把 `CONFIG` 字典以 YAML（装了 pyyaml 时）或 JSON 形式打印，或以 `mode='dicts'` 返回字典：

```python
def show(mode=DisplayModes.stdout.value):
    if mode == DisplayModes.stdout.value:
        try:
            yaml = _check_pyyaml()
            print(yaml.dump(CONFIG))
        except ModuleNotFoundError:
            ...
            print(json.dumps(CONFIG, indent=2))
    elif mode == DisplayModes.dicts.value:
        return CONFIG
```

#### 4.4.4 代码实践（运行型）

**目标**：用编程方式提取 BLAS/LAPACK 来源，而不是靠肉眼读控制台。

**步骤**：

1. 在已安装 SciPy 的环境里运行：
   ```python
   import scipy
   cfg = scipy.show_config(mode='dicts')
   blas = cfg['Build Dependencies']['blas']
   print("BLAS 名称:", blas['name'])
   print("探测方式:", blas['detection method'])
   print("是否 ILP64:", blas.get('has ilp64'))
   print("openblas 配置:", blas.get('openblas configuration'))
   ```
2. 同样地取 `cfg['Build Dependencies']['lapack']`，看 LAPACK 是否也来自同一个 OpenBLAS（OpenBLAS 同时提供 BLAS 和 LAPACK）。
3. 再看 `cfg['Compilers']`，确认 C/C++/Cython 编译器身份。

**需要观察的现象**：`blas` 与 `lapack` 的 `name` 通常都是 `openblas`，`detection method` 会显示是「通过哪种方式找到的」（如 `pkgconfig` 或 `scipy-openblas` wheel）。

**预期结果**：你得到一个字典，能精确指出「我的 SciPy 链接的是 OpenBLAS 版本 X、通过 pkg-config 发现、库在 Y 目录」。如果你用 `--with-accelerate` 构建，这里会变成 `accelerate`。

> 若尚未从源码构建，此步可先用任意已装 SciPy 练习读字段；真正的源码构建验证放在第 5 节综合实践。

#### 4.4.5 小练习与答案

**练习 1**：为什么源码树里找不到 `scipy/version.py`，但 `import scipy; scipy.__version__` 却能正常工作？
**答案**：`scipy/version.py` 是**构建时由 `tools/gitversion.py` 生成的**，不在 git 仓库里。从源码编译安装后它才出现在安装目录中。`meson.build:4` 用 `run_command(['tools/gitversion.py'], ...)` 触发生成。

**练习 2**：`show_config(mode='dicts')` 与 `show_config()`（默认）返回值有何不同？
**答案**：默认 `mode='stdout'` 打印到屏幕并返回 `None`；`mode='dicts'` 不打印，而是返回 `CONFIG` 字典，便于程序解析。两者读的是同一份 `scipy/__config__.py` 数据。

## 5. 综合实践

**任务**：从源码构建 SciPy，并用 `show_config()` 验证 BLAS/LAPACK 来源。

> ⚠️ 本实践需要一台装有编译工具链（gcc/g++/Fortran 编译器、Python 3.12+ 开发头文件）的机器，且耗时较长（首次约 10–30 分钟）。若环境不具备，可降级为「源码阅读型」：只读 `meson.build` 与 `meson.options`，预测构建会链接哪个 BLAS。

### 步骤一：准备环境

确保已安装构建工具组（参考 `pyproject.toml` 的 `dependency-groups.build`）：

```bash
git clone https://github.com/scipy/scipy.git
cd scipy
git submodule update --init    # 拉取 xsf/unuran 等 5 个子项目（见 meson.build:153-168）
python -m venv .venv && source .venv/bin/activate
pip install -r <(python -c "print('numpy>=2.0.0 meson-python>=0.15.0 Cython>=3.2.0 pybind11>=2.13.2 pythran>=0.18.1 ninja>=1.8.2')")
```

> 安装命令里的版本下限严格取自 [pyproject.toml:96-103](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/pyproject.toml#L96-L103) 的 `build` 依赖组。

### 步骤二：选择一种方式构建

**方式 A（推荐开发场景）：`spin build`**

```bash
pip install spin
spin build
```

`spin build` 的实现在 [.spin/cmds.py:64-161](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/.spin/cmds.py#L64-L161)，它本质上包装了 `meson compile`，并把产物装到 `build-install/` 目录。

**方式 B（标准 PEP 517 方式）：`pip install -e . --no-build-isolation`**

```bash
pip install -e . --no-build-isolation
```

`--no-build-isolation` 很关键：它告诉 pip「不要另建一个隔离环境装构建依赖，直接用当前环境里已经装好的 meson-python/Cython/pythran」。因为 SciPy 的构建依赖需要你自己提前装好（方式一已装）。

### 步骤三：验证并解读

构建完成后：

```bash
spin python --  # 或直接用方式 B 安装好的 python
```

进入 Python 后：

```python
import scipy
print(scipy.__version__)          # 应显示 1.19.0.dev0+git<日期>.<哈希>
scipy.show_config()               # 打印 BLAS/LAPACK/编译器信息
```

**重点解读 `show_config()` 输出中的 `Build Dependencies`**：

- `blas.name` 与 `lapack.name` → 应为 `openblas`（对应 `meson.build` 的 `default_options: blas=openblas`）。
- `blas.detection method` → 说明 OpenBLAS 是怎么被找到的（`pkgconfig` / `scipy-openblas` / `system`）。
- `blas.openblas configuration` → OpenBLAS 自带的编译配置串，能看出它是 32 位还是 64 位整数、用了哪个线程后端（pthreads/openmp）。
- `Compilers` 段 → 确认 C 编译器是 gcc/clang/msvc 中的哪个，版本是否满足 [meson.build:49-62](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/meson.build#L49-L62) 的下限。

### 步骤四（进阶，可选）：换一个 BLAS 后端重建

如果你在 macOS 上，可以验证 `meson.options` 的 `blas` 选项确实可切换：

```bash
spin build --with-accelerate      # 等价于 -Dblas=accelerate（见 .spin/cmds.py:131-133）
# 或手动指定：
spin build --setup-args=-Dblas=blas
```

重建后再 `show_config()`，对比 `blas.name` 是否如预期变化。**待本地验证**：不同平台可用的 BLAS 不同，此步结果取决于你的系统。

## 6. 本讲小结

- SciPy 是 **Python + C/C++/Cython/Fortran/Pythran** 多语言项目，所以必须用真正的构建系统（Meson + Ninja），而不能只靠拷贝 `.py`。
- 构建链是 **`pip` → `meson-python`（PEP 517 后端）→ `meson`（读 `meson.build`）→ `ninja`（执行编译）**。
- [`pyproject.toml`](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/pyproject.toml) 声明构建后端 `mesonpy`、构建依赖（Cython/pythran/pybind11）、运行依赖（numpy≥2.0.0）、Python≥3.12，以及 `spin` 命令注册。
- [`meson.build`](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/meson.build) 是顶层编排：声明三种语言、默认链接 OpenBLAS、检查编译器/Python 版本、校验 5 个 submodule、最后 `subdir('scipy')` 进入子包编译。
- [`meson.options`](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/meson.options) 暴露可配置开关：`blas`/`lapack` 切换、`use-ilp64`、`use-pythran`、`use-system-libraries`，可用 `-D<选项>=<值>` 覆盖。
- 构建还会**生成文件**：`tools/gitversion.py` 生成 `version.py`，`scipy/__config__.py.in` 被填充为 `__config__.py`——后者正是 `scipy.show_config()` 的数据来源。
- 从源码构建有两种方式：开发用 `spin build`（产物在 `build-install/`），标准用 `pip install -e . --no-build-isolation`。

## 7. 下一步学习建议

- **下一讲（u1-l3）** 将深入 `scipy/__init__.py`，讲解包的目录结构与**延迟导入（lazy import）**机制——你会看到构建产出的 `version.py` 是如何被加载的。
- 想深入了解构建工具链本身，可阅读 [Meson 官方手册](https://mesonbuild.com/Manual.html) 与 [meson-python 文档](https://meson-python.readthedocs.io/)。
- 想看「子包如何被逐个编译」，可先浏览 `scipy/meson.build`（本讲只看了它的开头），它展示了 NumPy 头文件、pybind11、pythran 依赖如何接入——这部分会在 u2（共享基础设施）和各子包讲义中反复用到。
- 想理解「编译扩展如何写」，可跳到 u13-l1（用 Cython/Fortran 添加底层函数），那里会演示如何在一个最小 `meson.build` 里注册 `extension_module`。
