# 构建系统与 pocketfft 后端依赖

## 1. 本讲目标

上一讲（u1-l2）我们画出了 `numpy/fft/` 的「文件地图」，并顺带提到 `meson.build` 把这个包的文件分成「编译」「直接安装」「带 tests 标签安装」三类。但当时只是**速览**——我们没有解释：

- Meson 到底是什么？`py.extension_module(...)`、`py.install_sources(...)` 这两行 Meson 代码分别在做什么？
- `_pocketfft_umath.cpp` 是怎样变成一个能在 Python 里被 `import` 的扩展模块（`.so`/`.pyd`）的？
- 为什么 `meson.build` 要专门写一行 `if not fs.exists('pocketfft/README.md')` 去检查子模块在不在？缺了会怎样？怎么修？
- `install_tag: 'tests'` 这几个字，对最终打包出来的 NumPy wheel 有什么实际影响？

本讲就把这些问题一次性讲透。学完后你应当能够：

- 理解 Meson 把 `_pocketfft_umath.cpp` 编译成 Python 扩展模块的完整流程，并能逐参数解释 `py.extension_module` 的每个参数。
- 知道 `numpy/fft/pocketfft/` 这个 git 子模块为什么默认是空的、缺失时会触发什么报错、以及用哪条 `git submodule` 命令修复。
- 清楚地区分「需要编译的扩展模块」和「被直接安装的纯 Python 源」，并理解 `install_tag` 在打包时的筛选作用。

## 2. 前置知识

本讲几乎不涉及傅里叶数学，核心是**构建（build）工程**。你需要先建立以下几个概念（上一讲已铺垫部分）：

- **源码 vs. 安装产物**：仓库里你看到的 `.py`、`.cpp` 叫**源码**；而 `pip install numpy` 之后落在你 `site-packages/numpy/` 里的，是**安装产物**。构建系统的职责，就是把源码变成安装产物。
- **纯 Python 文件**：像 `_pocketfft.py`、`_helper.py`、`.pyi` 这种文件，安装时只要**原样拷贝**到目标目录就能用，不需要编译。
- **C/C++ 扩展模块（extension module）**：像 `_pocketfft_umath.cpp` 这种文件，必须先被**C++ 编译器编译**、再链接成平台相关的动态库（Linux 上是 `_pocketfft_umath.*.so`，Windows 上是 `.pyd`），Python 才能识别它。它的导入方式和普通 `.py` 一样（都是 `import`），但来源是编译产物而不是文本。
- **构建系统（build system）与 Meson**：手动敲 `gcc` 命令编译一个大项目既繁琐又容易错。**构建系统**用一个配置文件描述「哪些文件要编译、依赖关系、装到哪里」，再由它自动生成具体的编译命令。NumPy 用的构建系统叫 **Meson**（配置文件就叫 `meson.build`，用 Meson 自己的 DSL 写），Meson 会生成 **Ninja** 的构建脚本，再由 Ninja 调用真正的编译器（`gcc`/`clang`/`msvc`）。你只需要读懂 `meson.build`，不用关心 Ninja 和编译器的细节。
- **git 子模块（submodule）**：把另一个**独立的 git 仓库**「嵌入」到当前仓库的某个子目录里。它在仓库里只留一个「指针」（记录那个仓库的某个 commit），并不会自动把对方的内容下载下来，所以一个刚 clone 的仓库里，子模块目录常常是**空的**，需要手动 `git submodule update --init` 才会被填充。
- **header-only 库**：一种只靠头文件（`.h`）分发的 C++ 库——使用方只要 `#include` 它，源码在编译期就被直接「展开」进使用者自己的目标文件，**不需要单独编译、不需要链接**。本包依赖的 `pocketfft` 就是 header-only 库。

> 概念提示：`py` 不是 Python 的 `py`，而是 Meson 脚本里一个**对象**——它代表「目标 Python 解释器」，由上层 `meson.build` 通过 `import('python').find_installation(...)` 创建，本包的 `meson.build` 直接拿来用。它身上挂着 `extension_module()`、`install_sources()` 等方法，专门用来「编译/安装与 Python 相关的文件」。

## 3. 本讲源码地图

本讲聚焦在构建链上，只用到这几个文件：

| 文件 | 作用 |
|------|------|
| `meson.build` | 本讲的**主角**：声明要编译什么、安装什么、如何检查子模块 |
| `_pocketfft_umath.cpp` | 被 `meson.build` **编译**的那个 C++ 源；它 `#include` 了子模块里的头文件 |
| `pocketfft/`（git 子模块） | 提供 `pocketfft_hdronly.h`；缺失时 `meson.build` 会拦截报错 |
| `.gitmodules`（仓库根） | 记录子模块的来源 URL（`mreineck/pocketfft`） |

`meson.build` 一共只有 38 行，逻辑非常清爽，分三段。下面四节（4.1–4.4）会把它逐段拆开。

## 4. 核心概念与源码讲解

### 4.1 Meson 构建系统速览：从源码到「可 import 的包」

#### 4.1.1 概念说明

当你在终端写 `pip install numpy`（或从源码 `meson install`）时，发生在背后的事情大致是：

1. Meson 读取仓库里所有 `meson.build`，拼出一张「要造什么东西」的清单。
2. Ninja 根据这张清单，调用 C++ 编译器把该编译的 `.cpp` 编成扩展模块（`.so`/`.pyd`）。
3. Meson 按清单把「编译产物」和「需要原样安装的 `.py`/`.pyi`」**拷贝到安装目录**（`site-packages/numpy/fft/`）。
4. 安装完成后，你 `import numpy.fft` 才能找到入口 `__init__.py`，并进一步找到那个编译好的 `_pocketfft_umath` 扩展。

`numpy/fft/meson.build` 只负责本子包这一小段。它做三件事，恰好对应三个最小模块（4.2、4.3、4.4）：

- **编译**：把 `_pocketfft_umath.cpp` 编成扩展模块 `_pocketfft_umath`。
- **安装**：把 6 个纯 Python 源/存根原样拷到安装目录。
- **检查**：在一切开始前，先确认 `pocketfft` 子模块确实存在。

#### 4.1.2 核心流程

把 `meson.build` 整个文件看成三段，自上而下执行：

```
 meson.build 的三段结构
 ┌─────────────────────────────────────────────┐
 │ ① 平台变量 + 子模块存在性检查（前置守卫）        │  → 4.1 / 4.4
 │ ② py.extension_module(...)  编译 C++ 扩展     │  → 4.2
 │ ③ py.install_sources(...)    安装 .py / .pyi   │  → 4.3
 │    py.install_sources(..., install_tag:tests) │  → 4.3
 └─────────────────────────────────────────────┘
```

第 ① 段是「守卫」：如果子模块不在，立刻 `error()` 终止，根本不会进入编译。这保证用户看到的错误是清晰的中文/英文提示，而不是一堆「找不到头文件」的 C++ 编译报错。

#### 4.1.3 源码精读

先把整个 `meson.build` 通读一遍（只有 38 行），心里建立「三段」的对应关系：

[meson.build:1-38](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/meson.build#L1-L38) —— 本子包的全部构建逻辑：第 1–8 行是平台宏与子模块检查；第 10–16 行编译 C++ 扩展；第 18–28 行安装纯 Python 源；第 30–38 行安装测试文件（带 `tests` 标签）。

第 1–4 行是一个**平台特例**，先单独说一下它的存在意义（细节在 4.2 再讲）：

[meson.build:1-4](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/meson.build#L1-L4) —— 当目标平台是 IBM AIX 时，往编译参数里加一个 `-D_LARGE_FILES` 宏，用于支持大文件（>2GB）偏移量。其他平台这行保持为空数组 `[]`，不产生任何影响。

#### 4.1.4 代码实践

1. **实践目标**：建立「读 `meson.build` 先分三段」的直觉。
2. **操作步骤**：打开本讲的 [meson.build:1-38](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/meson.build#L1-L38)，用三种颜色（或三种记号）分别标出「守卫段」「编译段」「安装段」。
3. **需要观察的现象**：你会发现整个文件里**只有一个**文件名出现在「编译」语境（`_pocketfft_umath.cpp`），其余 `.py`/`.pyi` 全部出现在「安装」语境。
4. **预期结果**：能复述「本包只有 1 个文件需要编译，其余都是原样安装」。
5. **待本地验证**：无。

#### 4.1.5 小练习与答案

**练习 1**：NumPy 用的构建系统叫什么？它生成的底层构建脚本工具又叫什么？

**答案**：构建系统是 **Meson**（配置文件 `meson.build`），它生成 **Ninja** 构建脚本，再由 Ninja 调用真正的编译器。

**练习 2**：为什么我们说本包是「接口与实现分离」？这跟构建系统有什么关系？

**答案**：接口（`_pocketfft.py` 等纯 Python）只需拷贝安装，而实现（`_pocketfft_umath.cpp`）必须编译。构建系统正是按这两种不同方式分别处理它们——这就是「分离」在工程层面的落点。

---

### 4.2 py.extension_module：把 _pocketfft_umath.cpp 编译成 Python 扩展

#### 4.2.1 概念说明

`py.extension_module(名字, 源文件, ...)` 是 Meson Python 专门用来**编译 C/C++ 扩展模块**的方法。它的产物是一个平台相关的动态库（Linux `.so`、Windows `.pyd`、macOS `.dylib`），文件名就是扩展模块的名字。编译完成后，Python 可以像 import 普通 `.py` 一样 `import` 它。

对 `_pocketfft_umath` 而言：源是 `_pocketfft_umath.cpp`，产物是 `_pocketfft_umath.*.so`，它就是 `_pocketfft.py` 里那句 `from . import _pocketfft_umath as pfu` 要导入的那个东西——真正的 FFT 计算就住在里面。

一个 C++ 文件要能被编译成「合法的 Python 扩展」，必须满足一个硬性约定：**实现一个名为 `PyInit_<模块名>` 的初始化函数**。`_pocketfft_umath.cpp` 的末尾就有它，这是扩展模块的「入口点」，Python 在 `import` 时会调用它来初始化模块、注册里面的对象（这里是五个 gufunc）。这也是为什么 `meson.build` 里扩展名 `_pocketfft_umath`、C++ 函数名 `PyInit__pocketfft_umath`、Python 导入名 `_pocketfft_umath` 三者必须**严格一致**。

#### 4.2.2 核心流程

编译一个扩展模块的流程：

```
 meson: py.extension_module('_pocketfft_umath', ['_pocketfft_umath.cpp'], ...)
          │
          ▼  Ninja 调用 C++ 编译器（gcc/clang/msvc）
   编译 _pocketfft_umath.cpp
     ├─ #include "numpy/arrayobject.h"     ← 由 np_core_dep 提供包含路径
     ├─ #include "numpy/ufuncobject.h"     ← 由 np_core_dep 提供包含路径
     └─ #include "pocketfft/pocketfft_hdronly.h"  ← 相对源文件目录解析，来自子模块
          │
          ▼  链接
   产出 _pocketfft_umath.cpython-3xx-<arch>.so（扩展模块）
          │
          ▼  install: true, subdir: 'numpy/fft'
   安装到 site-packages/numpy/fft/_pocketfft_umath.*.so
```

关键点：`#include "pocketfft/pocketfft_hdronly.h"` 这条包含路径，是**相对 `_pocketfft_umath.cpp` 所在的目录**（即 `numpy/fft/`）解析的，于是它指向 `numpy/fft/pocketfft/pocketfft_hdronly.h`——正是子模块里的头文件。**所以只要子模块被正确拉取，编译就能找到 pocketfft，且不需要单独把 pocketfft 列为编译单元**（它是 header-only）。

#### 4.2.3 源码精读

编译段的核心 7 行：

[meson.build:10-16](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/meson.build#L10-L16) —— 调用 `py.extension_module`，把 `_pocketfft_umath.cpp` 编译成名为 `_pocketfft_umath` 的扩展模块。各参数含义见下表。

逐参数解释：

| 参数 | 值 | 含义 |
|------|----|------|
| 第 1 个参数（名字） | `'_pocketfft_umath'` | 扩展模块名，也是产物文件名、Python 导入名；必须与 C++ 里 `PyInit__pocketfft_umath` 一致 |
| 源文件列表 | `['_pocketfft_umath.cpp']` | **本包唯一被编译的源文件**；注意它是个数组，理论上可放多个，但这里只有一个 |
| `c_args` | `largefile_define` | 传给编译器的宏参数，AIX 上是 `['-D_LARGE_FILES']`，其他平台是空数组 `[]`（见 4.1.3） |
| `dependencies` | `np_core_dep` | 由上层 `meson.build` 定义的依赖对象，提供 NumPy C API 头文件（`numpy/arrayobject.h`、`numpy/ufuncobject.h` 等）的包含路径，让 `.cpp` 能 `#include` 到它们 |
| `install` | `true` | 让这个扩展模块被安装到目标目录（不加则只编译不安装，`pip install` 后用不了） |
| `subdir` | `'numpy/fft'` | 安装到 `site-packages` 下的 `numpy/fft/` 子目录，使其落在包内、能被 `from . import` 找到 |

AIX 的 `_LARGE_FILES` 宏是为了让文件偏移量用 64 位（从而支持 >2GB 的文件），是 AIX 平台特有的可移植性补丁，跟 FFT 算法本身无关。

C++ 源那侧，关键的三条 `#include` 在文件开头：

[_pocketfft_umath.cpp:15-24](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft_umath.cpp#L15-L24) —— 先包含 Python（`Python.h`）和 NumPy C API（`arrayobject.h`/`ufuncobject.h`，靠 `np_core_dep` 找到），最后用 `#include "pocketfft/pocketfft_hdronly.h"` 把子模块里的 pocketfft 头文件库「缝合」进来；上面的 `#define POCKETFFT_NO_MULTITHREADING` 表示 FFT 内部不自行开线程（线程化交给 NumPy 上层）。

扩展模块的「入口点」在文件末尾，它是编译产物能否被 Python import 的硬性约定：

[_pocketfft_umath.cpp:432-443](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft_umath.cpp#L432-L443) —— 定义 `PyModuleDef`（模块名 `"_pocketfft_umath"`）并实现 `PyInit__pocketfft_umath`；这个函数名与 `meson.build` 里的扩展名、Python 的导入名三者必须一致，否则 `import` 会报「模块未找到初始化函数」。真正干活的是 slot `_pocketfft_umath_exec`，它在 import 时调用 `add_gufuncs(d)` 把五个 gufunc 注册进模块字典。

注册的五个 gufunc（`fft`/`ifft`/`rfft_n_even`/`rfft_n_odd`/`irfft`）就是后续讲义里 `_raw_fft` 会根据 `is_real`/`is_forward` 选用的后端。本讲只需知道「编译产物里注册了五个 gufunc」即可，细节留到 u5-l1。

#### 4.2.4 代码实践

1. **实践目标**：验证「扩展名 = PyInit 函数名 = 导入名」三者一致的硬性约定。
2. **操作步骤**：
   - 在 [_pocketfft_umath.cpp:432-443](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft_umath.cpp#L432-L443) 找到 `PyInit__pocketfft_umath`（注意名字里有两个下划线：`PyInit_` 前缀 + `_pocketfft_umath` 模块名）。
   - 对照 [meson.build:10-16](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/meson.build#L10-L16) 的 `py.extension_module('_pocketfft_umath', ...)`。
   - 在本地 Python 里运行 `import numpy.fft._pocketfft_umath as m; print(m.__file__)`。
3. **需要观察的现象**：打印出的路径应指向 `site-packages/numpy/fft/` 下的一个 `.so`/`.pyd` 文件（编译产物），而不是 `.py`。
4. **预期结果**：三者名字一致，扩展被成功导入，且 `__file__` 是动态库文件。
5. **待本地验证**：在不同平台（Linux/macOS/Windows）上 `__file__` 后缀分别为 `.so`/`.dylib`(实为 `.so` 命名)/`.pyd`。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `meson.build:10` 的 `'_pocketfft_umath'` 改成 `'_pocketfft_umath2'`，但 C++ 里 `PyInit__pocketfft_umath` 不改，会发生什么？

**答案**：编译能过（产物叫 `_pocketfft_umath2.so`），但 `import _pocketfft_umath2` 时 Python 会找不到 `PyInit__pocketfft_umath2` 函数而报错——因为扩展名、`PyInit_` 函数名、导入名三者必须一致。

**练习 2**：为什么 `dependencies: np_core_dep` 是必须的？

**答案**：因为 `_pocketfft_umath.cpp` 里 `#include "numpy/arrayobject.h"`、`#include "numpy/ufuncobject.h"`，需要知道这些 NumPy 头文件在磁盘上的位置；`np_core_dep` 这个依赖对象正是把 NumPy 的头文件包含路径提供给编译器。没有它，编译会在 `#include` 阶段失败。

**练习 3**：`largefile_define` 在 Linux 上是什么值？为什么？

**答案**：空数组 `[]`。因为 `if host_machine.system() == 'aix' ...` 只在 AIX 上为真，Linux 上不进入分支，`largefile_define` 保持初始化的 `[]`，相当于 `c_args: []`，不传任何额外宏。

---

### 4.3 py.install_sources：原样安装纯 Python 源与 install_tag='tests'

#### 4.3.1 概念说明

`py.install_sources([文件...], subdir: ...)` 做的事非常朴素：**把指定的源文件原样拷贝到安装目录**，不编译、不转换。它专门用于「纯 Python 文件」（`.py`）和「类型存根」（`.pyi`）。

`meson.build` 里调用了它**两次**：一次装 6 个包源/存根（`__init__.py`、`_pocketfft.py`、`_helper.py` 及其 `.pyi`），一次装 3 个测试文件（`tests/` 下的 3 个 `.py`），第二次还带了 `install_tag: 'tests'`。

**`install_tag` 是什么？** 它是给被安装文件**贴的一个标签**。Meson 安装时支持「按标签筛选」：`meson install --tags xxx` 只安装带了指定标签的文件。NumPy 打包发行时，会用这套机制把**带 `tests` 标签的测试文件排除在主 wheel 之外**——也就是说，普通用户 `pip install numpy` 装到的包里，通常**不带 `tests/` 目录**；而开发者在本地从源码构建时可以连同测试一起安装。这就是 `install_tag: 'tests'` 的实际作用：让测试文件「可被单独排除」。

#### 4.3.2 核心流程

两次 `install_sources` 的处理逻辑：

```
 install_sources（第 1 次，包源）
   输入: __init__.py, __init__.pyi, _pocketfft.py, _pocketfft.pyi, _helper.py, _helper.pyi
   行为: 原样拷贝 → site-packages/numpy/fft/
   标签: 默认（无 install_tag）→ 进入主 wheel

 install_sources（第 2 次，测试）
   输入: tests/__init__.py, tests/test_helper.py, tests/test_pocketfft.py
   行为: 原样拷贝 → site-packages/numpy/fft/tests/
   标签: install_tag: 'tests' → 打包时可选排除
```

注意 `subdir` 决定目标子目录：第 1 次是 `'numpy/fft'`，第 2 次是 `'numpy/fft/tests'`，所以测试文件会落到包内的 `tests/` 子目录，与源码安装位置自然分层。

#### 4.3.3 源码精读

第 1 次 `install_sources`，安装包源与存根：

[meson.build:18-28](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/meson.build#L18-L28) —— 把 6 个纯 Python 源/存根**原样**安装到 `numpy/fft/` 下。这 6 个文件与 4.2 编译的那个 `.cpp` 是两类完全不同的东西：它们不需要编译，安装 = 拷贝。

第 2 次 `install_sources`，安装测试文件并打 `tests` 标签：

[meson.build:30-38](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/meson.build#L30-L38) —— 把 3 个测试文件安装到 `numpy/fft/tests/`，并贴 `install_tag: 'tests'`。这个标签让打包管线能识别「这些是测试文件」，从而在生成发行 wheel 时把它们排除掉。

把 `meson.build` 三段里「被处理的文件」汇总成一张总表，区分「编译 / 直接安装（主）/ 直接安装（测试）」三类：

| 文件 | 处理方式 | 目标子目录 | install_tag |
|------|----------|-----------|-------------|
| `_pocketfft_umath.cpp` | **编译** → `_pocketfft_umath.*.so` | `numpy/fft/` | — |
| `__init__.py` / `__init__.pyi` | 直接安装 | `numpy/fft/` | —（进主包） |
| `_pocketfft.py` / `_pocketfft.pyi` | 直接安装 | `numpy/fft/` | —（进主包） |
| `_helper.py` / `_helper.pyi` | 直接安装 | `numpy/fft/` | —（进主包） |
| `tests/__init__.py` | 直接安装 | `numpy/fft/tests/` | `tests` |
| `tests/test_helper.py` | 直接安装 | `numpy/fft/tests/` | `tests` |
| `tests/test_pocketfft.py` | 直接安装 | `numpy/fft/tests/` | `tests` |

这张表也回答了本讲一个核心问题：**整个子包只有 1 个文件被编译，其余 9 个文件全是原样安装**（6 个包源 + 3 个测试）。

#### 4.3.4 代码实践

1. **实践目标**：在「已安装的 NumPy」上验证 `install_tag: 'tests'` 的实际效果。
2. **操作步骤**：
   - 在你本地 Python 环境里 `import numpy; print(numpy.__file__)` 找到安装目录。
   - 进入该目录的 `fft/` 子目录，查看里面有没有 `tests/` 文件夹，以及有没有 `_pocketfft_umath*.so` 这样的动态库。
3. **需要观察的现象**：
   - 通常你会看到 `__init__.py`、`_pocketfft.py`、`_helper.py`、`_pocketfft_umath.*.so`，但 `.pyi` 和 `tests/` 是否存在取决于发行包配置。
   - `_pocketfft_umath` 是编译产物（动态库），印证 4.2。
4. **预期结果**：能区分哪些文件是「编译产物」、哪些是「原样安装」，并理解 `tests/` 在不同发行方式下可能不存在。
5. **待本地验证**：不同来源（pip 官方 wheel / conda / 源码 `meson install`）安装的 NumPy，`tests/` 目录是否存在可能不同；这正是 `install_tag` 的作用空间。

#### 4.3.5 小练习与答案

**练习 1**：`meson.build` 里 `install_sources` 被调用了几次？分别安装什么？

**答案**：两次。第 1 次安装 6 个包源/存根（`__init__.py`、`__init__.pyi`、`_pocketfft.py`、`_pocketfft.pyi`、`_helper.py`、`_helper.pyi`）到 `numpy/fft/`；第 2 次安装 3 个测试文件到 `numpy/fft/tests/` 并带 `tests` 标签。

**练习 2**：`install_tag: 'tests'` 的实际作用是什么？

**答案**：给被安装的测试文件贴上 `tests` 标签，使打包管线能用 `meson install --tags` 之类的机制把它们排除在发行 wheel 之外，从而让普通用户安装的包更精简、不带测试。

**练习 3**：`.pyi` 文件被「直接安装」有什么意义？它们运行时会用到吗？

**答案**：`.pyi` 是类型存根，供类型检查器（mypy）和 IDE 补全使用，**运行时不参与执行**。安装它们是为了让用户在本地也能获得正确的类型提示，而不是为了 import 时加载。

---

### 4.4 pocketfft 子模块存在性检查与缺失修复

#### 4.4.1 概念说明

本包真正做 FFT 计算的算法来自一个**第三方 C++ 库 pocketfft**（作者 Martin Reinecke）。NumPy 没有把它的代码「复制粘贴」进自己仓库，而是用 **git 子模块**的方式引用它：在 `numpy/fft/pocketfft/` 这个目录里放一个指向 `https://github.com/mreineck/pocketfft` 某个 commit 的指针。

git 子模块有一个让人容易踩坑的特性：**普通 `git clone` 不会自动拉取子模块内容**。所以一个刚 clone 的 NumPy 仓库里，`numpy/fft/pocketfft/` 目录常常是**空的**（本讲当前 checkout 里它就是空的）。如果 Meson 直接开始编译，`_pocketfft_umath.cpp` 里那句 `#include "pocketfft/pocketfft_hdronly.h"` 会找不到头文件，于是报出一大堆晦涩的 C++ 编译错误。

为了把这种「难懂的编译失败」转换成「一句清晰的人话提示」，`meson.build` 在最开头加了一个**前置守卫**：检查 `pocketfft/README.md` 是否存在；不存在就立刻 `error()` 中止，并提示用户运行 `git submodule update --init`。

#### 4.4.2 核心流程

子模块从「缺失」到「可用」的完整生命周期：

```
 git clone numpy/numpy
   → numpy/fft/pocketfft/ 目录存在，但为空（只有指针，没内容）
        │
        ▼
 meson.build 守卫: if not fs.exists('pocketfft/README.md')
   → README.md 不存在 → error('Missing the `pocketfft` git submodule! ...')
        │  （用户照提示执行）
        ▼
 git submodule update --init   （或 git submodule update --init numpy/fft/pocketfft）
   → 从 https://github.com/mreineck/pocketfft 拉取对应 commit 的内容
   → numpy/fft/pocketfft/ 被填充（含 pocketfft_hdronly.h、README.md 等）
        │
        ▼
 重新 meson 配置/编译
   → 守卫通过 → 编译 _pocketfft_umath.cpp 成功（#include 能找到头文件）
```

关键点：守卫检查的不是「头文件」本身，而是 `README.md`——一个一定存在于子模块里的「哨兵文件」。这等价于「子模块是否被填充」，但比检查 `pocketfft_hdronly.h` 更稳健（README 命名简单、不易混淆）。

`.gitmodules` 记录了子模块的来源：

[.gitmodules:16-18](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/.gitmodules#L16-L18) —— `numpy/fft/pocketfft` 这个子模块指向 `https://github.com/mreineck/pocketfft`。`git submodule update --init` 就是读这条配置去拉取内容。

#### 4.4.3 源码精读

守卫代码（本讲最关键的一行 `error`）：

[meson.build:6-8](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/meson.build#L6-L8) —— 用 `fs.exists('pocketfft/README.md')` 判断子模块是否被填充；若不存在，调用 `error(...)` 立即中止配置，并提示用户运行 `git submodule update --init`。`fs` 是 Meson 的文件系统对象（由 `import('fs')` 创建，在上层 meson.build 引入）。

这条守卫之所以放在 `py.extension_module` **之前**（见 4.1.3 的整体顺序），就是要让用户在第一时间看到清晰错误，而不是在编译阶段才看到含糊的「找不到头文件」。

C++ 那侧「缝合」子模块的语句，在 4.2.3 已引用，这里再强调一次它的依赖关系：

[_pocketfft_umath.cpp:24](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft_umath.cpp#L24) —— `#include "pocketfft/pocketfft_hdronly.h"`：相对当前 `.cpp` 所在目录解析，指向 `numpy/fft/pocketfft/pocketfft_hdronly.h`。若 4.4.3 的守卫没把子模块缺失拦住，这行就会在编译期报「找不到头文件」。

#### 4.4.4 代码实践

1. **实践目标**：亲手确认子模块的「默认为空」特性和修复命令。
2. **操作步骤**：
   - 在仓库根目录查看 `.gitmodules`，找到 `[submodule "numpy/fft/pocketfft"]` 段及其 `url`（即 [.gitmodules:16-18](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/.gitmodules#L16-L18)）。
   - 进入 `numpy/fft/pocketfft/`，查看目录里是否为空（`ls`）。
3. **需要观察的现象**：若目录为空（或只有零个文件），说明子模块未 checkout——这正是 [meson.build:6-8](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/meson.build#L6-L8) 守卫要拦截的情形。
4. **预期结果**：理解「clone 不带 `--recursive` / 未手动 init 时，子模块目录为空」这一事实。
5. **待本地验证**：真正拉取需执行 `git submodule update --init numpy/fft/pocketfft`（只拉这一个）或 `git submodule update --init`（拉全部子模块）。本环境不实际执行，请在你自己的 clone 中验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么守卫检查的是 `pocketfft/README.md`，而不是直接检查 `pocketfft_hdronly.h`？

**答案**：两者都能判断「子模块是否被填充」。选 `README.md` 作为「哨兵文件」是因为它命名简单、几乎一定存在于子模块根目录、不易与编译逻辑混淆。检查任意一个必然存在的文件都可以达到「子模块是否就位」的判断目的。

**练习 2**：写出缺失子模块时应执行的 `git submodule` 命令（两种写法）。

**答案**：
- 只拉这一个子模块：`git submodule update --init numpy/fft/pocketfft`
- 拉全部子模块：`git submodule update --init`
- 或在首次 clone 时一步到位：`git clone --recursive <url>`

**练习 3**：为什么把守卫放在 `py.extension_module` **之前**，而不是之后？

**答案**：为了让用户第一时间看到清晰的中文/英文错误提示（「子模块缺失，请运行 git submodule update --init」），而不是让 Meson 进入编译、在 C++ `#include` 阶段才抛出一堆难懂的「找不到头文件」错误。提前拦截 = 更好的用户体验。

---

## 5. 综合实践

把本讲的三个最小模块串起来，完成下面这个「读懂 `meson.build`」的综合任务。

**任务**：打开 [meson.build:1-38](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/meson.build#L1-L38)，书面回答以下四问（不运行构建，纯阅读）：

1. **哪些文件被编译？** 从 `py.extension_module(...)` 段找出所有出现在「源文件列表」里的文件。
2. **哪些文件被直接安装？** 从两处 `py.install_sources(...)` 段列出所有被原样拷贝的文件，并指出它们各自的目标子目录（`numpy/fft/` 还是 `numpy/fft/tests/`）。
3. **`install_tag: 'tests'` 的作用是什么？** 解释它对「发行 wheel 是否带测试」的影响。
4. **写出在缺失 pocketfft 子模块时应当执行的 `git submodule` 命令**，并说明 `meson.build` 是用哪个文件作为「哨兵」来判断子模块是否存在的。

**参考答案**：

1. **被编译的只有 1 个**：`_pocketfft_umath.cpp`（见 [meson.build:10-16](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/meson.build#L10-L16)），产物是扩展模块 `_pocketfft_umath`。
2. **被直接安装的共 9 个**：
   - 到 `numpy/fft/`：`__init__.py`、`__init__.pyi`、`_pocketfft.py`、`_pocketfft.pyi`、`_helper.py`、`_helper.pyi`（见 [meson.build:18-28](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/meson.build#L18-L28)）。
   - 到 `numpy/fft/tests/`：`tests/__init__.py`、`tests/test_helper.py`、`tests/test_pocketfft.py`（见 [meson.build:30-38](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/meson.build#L30-L38)）。
3. `install_tag: 'tests'`（见 [meson.build:37](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/meson.build#L37)）给测试文件贴标签，使打包管线能把它们排除在发行 wheel 之外；普通用户 `pip install numpy` 得到的包通常不含 `tests/`。
4. 执行 `git submodule update --init numpy/fft/pocketfft`（或 `git submodule update --init` 拉全部）；`meson.build` 用 `pocketfft/README.md` 作为哨兵文件来判断子模块是否被填充（见 [meson.build:6-8](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/meson.build#L6-L8)）。

完成此实践后，你应该能仅凭阅读 `meson.build` 就回答出「这个子包怎么被构建出来」的全部关键问题。

## 6. 本讲小结

- `numpy/fft/meson.build` 一共 38 行，分三段：**守卫（子模块检查）→ 编译（`py.extension_module`）→ 安装（两次 `py.install_sources`）**。
- **编译**：只有 `_pocketfft_umath.cpp` 一个文件被 `py.extension_module('_pocketfft_umath', ...)` 编译成扩展模块；扩展名、C++ 里 `PyInit__pocketfft_umath`、Python 导入名三者必须一致；`np_core_dep` 提供 NumPy C API 头文件包含路径；AIX 平台额外加 `-D_LARGE_FILES`。
- **安装**：6 个包源/存根被 `py.install_sources` 原样拷到 `numpy/fft/`；3 个测试文件被拷到 `numpy/fft/tests/` 并带 `install_tag: 'tests'`，使打包时可将测试排除出发行 wheel。
- **子模块**：真正的 FFT 算法来自 git 子模块 `numpy/fft/pocketfft`（来源 `mreineck/pocketfft`，见 `.gitmodules`），它是 header-only 库，被 `_pocketfft_umath.cpp` 以 `#include "pocketfft/pocketfft_hdronly.h"` 缝合。
- **守卫**：`meson.build` 用 `fs.exists('pocketfft/README.md')` 做前置检查，缺失则 `error()` 并提示 `git submodule update --init`，把难懂的 C++ 编译失败转成清晰提示。
- 整个子包**只有 1 个文件被编译，其余 9 个文件全是原样安装**——这是「接口（Python）与实现（C++）分离」在构建层面的直接体现。

## 7. 下一步学习建议

本讲解决的是「这个包怎么被构建出来」，到此**入门单元（u1）的三讲已全部完成**：你已经会跑（u1-l1）、看得懂目录（u1-l2）、也理解构建链（u1-l3）。

接下来进入**第 2 单元（数学约定与频率 helper）**：

- 先读 **u2-l1（DFT 数学定义与实现约定）**：把本讲里一带而过的「FFT 到底在算什么」从数学上讲清，为后面所有变换铺好基础。
- 之后 **u2-l2 / u2-l3** 会进入 `_helper.py`，讲 `fftfreq`/`rfftfreq`/`fftshift`/`ifftshift` 这些纯 Python helper——它们都是「直接安装」那 6 个文件里的成员，正好承接本讲的构建视角。

如果你对 C++ 后端感兴趣，可以跳到 **u5-l1（_pocketfft_umath gufunc 注册）** 深入看本讲 4.2.3 提到的「五个 gufunc 是怎么注册的」，但建议先完成第 2、3 单元的 Python 主流程，再回头看 C++ 会更顺。
