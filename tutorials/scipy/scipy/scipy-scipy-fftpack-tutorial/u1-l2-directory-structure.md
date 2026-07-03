# 目录结构与构建配置

## 1. 本讲目标

上一篇（`u1-l1`）我们弄清了 `scipy.fftpack` 的「遗留身份」，也看懂了 `__init__.py` 如何聚合导出公共 API、`basic.py` 等 shim 如何发出弃用警告。本讲换一个视角——**从文件系统和构建系统的角度俯瞰整个 `scipy/fftpack/` 目录**。读完本讲后，你应当能够：

- 看一眼目录里的文件名，就能把它归入「核心实现 / 弃用垫片 / Cython 扩展 / 测试」四类之一。
- 说清楚「带下划线的 `_basic.py`」与「不带下划线的 `basic.py`」为什么会成对出现。
- 读懂 [`meson.build`](meson.build)，解释 `convolve.pyx` 是如何被编译成扩展模块、其余 `.py` 文件又是如何作为纯 Python 安装的。
- 解释 `tests/meson.build` 如何把 `.npz` 测试参考数据一并安装，以及 `install_tag: 'tests'` 的意义。
- 理解 [`MANIFEST.in`](MANIFEST.in) 在源码发行版（sdist）里扮演的角色。

本讲几乎不涉及数学，重点是「工程视角」，为后续深入各变换族（单元 2）和卷积后端（单元 3）打下地图感。

## 2. 前置知识

### 2.1 什么是构建系统，为什么需要它

你写完一堆 `.py` 文件，直接 `import` 就能用——这没错。但当项目里还有「需要先编译才能用的代码」（比如 C、Fortran、Cython），就必须有一个工具把这些代码**编译、链接、安装**到正确的位置。这个工具就是**构建系统（build system）**。SciPy 当前使用的是 **Meson**（配合 `ninja` 执行器），构建脚本写在名为 `meson.build` 的文件里。

### 2.2 什么是 Cython 和 .pyx

Cython 是一种「带类型的 Python 方言」。`.pyx` 文件用接近 Python 的语法写，但可以声明 C 类型的变量，运行前会被**转译（transpile）**成 C 代码，再编译成机器码。这样写出来的函数比纯 Python 快得多。`scipy/fftpack/convolve.pyx` 就是一个 Cython 源文件，它最终会被编译成一个能被 `import` 的二进制扩展模块（`.so` / `.pyd`）。

> 小提示：Cython 还会用到 `.pxd`（声明文件）和 `.pyx`（实现文件）。本讲只需记住：`.pyx` = 需要编译的 Cython 源。

### 2.3 什么是源码发行版（sdist）与 MANIFEST.in

把一个 Python 项目打包发布时，常见的产物有两种：

- **wheel**（`.whl`）：已编译好的二进制包，装上即用。
- **sdist**（源码发行包，`.tar.gz`）：只包含源码，安装时要在本机重新编译。

`MANIFEST.in` 是一个清单文件，用来告诉打包工具「除了默认的 `.py` 文件，sdist 里还要额外塞进哪些文件」（比如编译需要的 C/Fortran 源、文档、测试数据）。我们会在 4.4 节看到 `fftpack` 的这份清单。

### 2.4 install_tag：给安装文件贴标签

Meson 允许给每个被安装的文件贴一个 **install tag（安装标签）**。例如把测试数据贴上 `'tests'` 标签后，构建生产环境 wheel 时就可以「跳过所有带 `tests` 标签的文件」，从而让发布包更小。这个机制会在 4.3 节用到。

## 3. 本讲源码地图

本讲围绕「目录整体」展开，主要涉及以下几个文件：

| 文件 | 作用 |
| --- | --- |
| [`__init__.py`](__init__.py) | 包入口。上一篇已精读，本讲只引用它的导入语句来串联四类文件。 |
| [`meson.build`](meson.build) | 本目录的构建脚本：编译 `convolve.pyx`、安装纯 Python 源、递归进入 `tests`。 |
| [`tests/meson.build`](tests/meson.build) | 测试子目录的构建脚本：安装测试代码与 `.npz` 参考数据。 |
| [`MANIFEST.in`](MANIFEST.in) | sdist 额外文件清单。 |
| `convolve.pyx` | 唯一的 Cython 扩展源，会被编译成二进制扩展模块 `convolve`。 |

本讲还会顺手引用四个 shim 文件（`basic.py` 等）以说明它们与 `_basic.py` 的成对关系，但其内部机制已在 `u1-l1` 讲过，这里不再重复。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开，并补一个测试安装子模块：

- 4.1 目录布局：核心 / 垫片 / 扩展 / 测试 四类文件
- 4.2 `meson.build`：编译扩展与安装纯 Python 源
- 4.3 `tests/meson.build`：测试代码与 `.npz` 数据的安装
- 4.4 `MANIFEST.in`：源码发行版的额外文件清单

### 4.1 目录布局：四类文件

#### 4.1.1 概念说明

打开 `scipy/fftpack/` 目录，会看到十来个文件。乍看杂乱，但其实可以干净地分成**四类**。掌握这套分类，是阅读任何 SciPy 子包的通用钥匙。

1. **核心实现模块（私有，下划线开头）**：真正干活的 `.py` 文件。下划线前缀是 Python 社区约定，表示「内部使用，别直接 import 我」。
2. **弃用垫片模块（shim，不带下划线）**：上一篇讲过的 `basic.py` 这类。它们成对地「伴随」每个核心模块存在。
3. **Cython 扩展源**：需要编译的 `.pyx`。
4. **测试与构建配置**：`tests/` 子目录、`meson.build`、`MANIFEST.in`。

#### 4.1.2 核心流程

整份目录可以按下表归类（基于本目录实际文件）：

| 类别 | 文件 | 说明 |
| --- | --- | --- |
| 包入口 | `__init__.py` | 聚合导出公共 API（`u1-l1` 已讲） |
| 核心实现（私有） | `_basic.py` | 复数/实数 FFT：`fft`、`ifft`、`fftn`、`rfft` 等 |
| 核心实现（私有） | `_realtransforms.py` | DCT/DST：`dct`、`dst`、`dctn` 等 |
| 核心实现（私有） | `_pseudo_diffs.py` | 伪微分算子：`diff`、`hilbert`、`shift` 等 |
| 核心实现（私有） | `_helper.py` | 辅助函数：`fftfreq`、`fftshift`、`next_fast_len` |
| 弃用垫片（shim） | `basic.py` / `helper.py` / `pseudo_diffs.py` / `realtransforms.py` | 与上面四个**一一成对**，拦截图省事的子模块导入 |
| Cython 扩展源 | `convolve.pyx` | 被编译成扩展模块 `convolve`，支撑伪微分算子的卷积后端 |
| 构建配置 | `meson.build` / `MANIFEST.in` | 编译安装规则 / sdist 清单 |
| 测试 | `tests/` | 测试代码与 `.npz` 参考数据 |

注意一个**关键的结构规律**：四个核心模块与四个 shim 是**严格成对**的——

```
_basic.py         ↔  basic.py          (shim)
_helper.py        ↔  helper.py         (shim)
_pseudo_diffs.py  ↔  pseudo_diffs.py   (shim)
_realtransforms.py↔  realtransforms.py (shim)
```

带下划线的是「新家」（实现），不带下划线的是「旧地址」（弃用垫片）。这正是 `u1-l1` 所讲机制在文件层面的体现：当你写 `from scipy.fftpack.basic import fft`，命中的是垫片 `basic.py` 的 `__getattr__`，从而触发 `DeprecationWarning`；而 `from scipy.fftpack import fft` 走的是 `__init__.py` 聚合导入，命中实现 `_basic.py`，合法且不报警。

#### 4.1.3 源码精读

先看 `__init__.py` 如何把四个核心模块聚合进来（`u1-l1` 已逐行分析，这里只回顾对应关系）：

[__init__.py:93-99](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/__init__.py#L93-L99)

```python
from ._basic import *
from ._pseudo_diffs import *
from ._helper import *
from ._realtransforms import *

# Deprecated namespaces, to be removed in v2.0.0
from . import basic, helper, pseudo_diffs, realtransforms
```

第 93-96 行聚合四个**带下划线的核心模块**；第 99 行显式导入四个**不带下划线的 shim**——注意这里用 `from . import basic, helper, ...`，目的是让 `scipy.fftpack.basic` 这个子模块对象存在（从而 `u1-l1` 讲的 `from scipy.fftpack.basic import fft` 能够被垫片拦截）。这两行代码，就是上表「成对关系」在入口文件里的注脚。

再看 Cython 扩展源 `convolve.pyx` 的开头，它揭示了一个重要事实：`convolve` 这个模块自己并不做底层 FFT 计算，而是把活儿转交给 DUCC 后端：

[convolve.pyx:1-13](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/convolve.pyx#L1-L13)

```python
from scipy.fft._duccfft.pyduccfft import r2r_fftpack
...
__all__ = ['destroy_convolve_cache', 'convolve', 'convolve_z',
           'init_convolution_kernel']


def destroy_convolve_cache():
    pass  # We don't cache anything, needed for compatibility
```

第 1 行从新版 `scipy.fft` 的 DUCC 后端导入 `r2r_fftpack`；第 12-13 行的 `destroy_convolve_cache` 函数体是空的（`pass`），注释写明「我们什么都不缓存，只是为了兼容而保留」。这说明 `convolve.pyx` 是一层**薄薄的 Cython 适配层**，真正的 FFT 内核已经迁移到 DUCC（这条线索会在单元 3「卷积后端」深入展开）。之所以仍用 Cython 而非纯 Python，是因为卷积核心里有一段紧凑的逐元素循环（见 `convolve.pyx` 第 54-68 行的 `swap_real_imag` 分支），编译后比纯 Python 快得多。

#### 4.1.4 代码实践

**实践目标**：用脚本枚举 `scipy/fftpack/` 目录里的文件，并自动归入四类。

**操作步骤**（**示例代码**，非项目原有文件）：

```python
import os, re

root = "scipy/fftpack"          # 按你的实际路径调整
core, shim, ext, other = [], [], [], []
for name in sorted(os.listdir(root)):
    if name in ("__init__.py",):
        other.append(name)
    elif name.startswith("_") and name.endswith(".py"):
        core.append(name)
    elif re.match(r"^[a-z_]+\.py$", name):   # 不带下划线的 .py（即 shim）
        shim.append(name)
    elif name.endswith(".pyx"):
        ext.append(name)
    else:
        other.append(name)

print("核心(私有):", core)
print("垫片(shim):", shim)
print("Cython扩展:", ext)
print("其它:      ", other)
```

**需要观察的现象**：`core` 应有四个 `_xxx.py`；`shim` 应有四个对应的不带下划线 `.py`；`ext` 只有 `convolve.pyx`。

**预期结果**：四类各归其位，且 core 与 shim 长度相等、名字一一对应（去掉下划线即匹配）。> 待本地验证：若你的工作副本是开发中的版本，列表可能略有出入，但成对结构应保持。

#### 4.1.5 小练习与答案

**练习 1**：`_basic.py` 和 `basic.py` 哪个是「真正实现 `fft`」的文件？为什么它们会成对存在？

> **参考答案**：`fft` 的真正实现 `_basic.py`（私有核心）；`basic.py` 是弃用垫片，仅为拦截 `from scipy.fftpack.basic import fft` 这类历史写法而保留，将在 v2.0.0 移除。成对存在是为了向后兼容。

**练习 2**：目录里唯一的 `.pyx` 文件叫什么？它会被编译成哪个扩展模块？

> **参考答案**：`convolve.pyx`，会被编译成名为 `convolve` 的二进制扩展模块（见 4.2 节 `meson.build` 的 `extension_module('convolve', ...)`）。

### 4.2 `meson.build`：编译扩展与安装纯 Python 源

#### 4.2.1 概念说明

理解了「有哪些文件」，下一个问题是「这些文件如何被编译和安装」。答案全在本目录的 [`meson.build`](meson.build) 里。这个文件只有 30 行，却做了三件事：

1. 把 `convolve.pyx` 编译成一个**扩展模块**（二进制）。
2. 把其余 9 个 `.py` 文件作为**纯 Python 源**安装。
3. 递归进入 `tests/` 子目录，继续处理它的 `meson.build`。

阅读 meson 脚本时，先认两个核心动作：`extension_module(...)`（构建需要编译的扩展）和 `install_sources(...)`（原样安装文件）。前者产出 `.so`，后者产出 `.py`。

#### 4.2.2 核心流程

```
meson.build 执行流程
├─ 1. py3.extension_module('convolve', …)      编译 convolve.pyx → convolve.so，并 install:true
├─ 2. python_sources = [ 9 个 .py 文件 ]         收集纯 Python 源（核心 4 个 + shim 4 个 + __init__）
├─ 3. py3.install_sources(python_sources, …)    把它们原样安装到 scipy/fftpack/
└─ 4. subdir('tests')                            递归处理 tests/meson.build
```

一个重要区分：**扩展模块是「编译产物」，纯 Python 源是「直接安装」**。前者要在构建期跑 Cython→C→机器码的整条流水线，后者只是「把文件复制到安装目录」。

#### 4.2.3 源码精读

先看扩展模块的编译规则：

[meson.build:1-8](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/meson.build#L1-L8)

```meson
py3.extension_module('convolve',
  cython_gen.process('convolve.pyx'),
  c_args: cython_c_args,
  dependencies: np_dep,
  link_args: version_link_args,
  install: true,
  subdir: 'scipy/fftpack'
)
```

逐项解读：

- `py3.extension_module('convolve', …)`：用 Meson 的 Python 模块（`py3`）声明一个名为 `convolve` 的扩展模块。
- `cython_gen.process('convolve.pyx')`：调用 Cython 生成器把 `.pyx` 转译成 `.c`，再交给 C 编译器。
- `c_args: cython_c_args`、`dependencies: np_dep`、`link_args: version_link_args`：编译参数、NumPy 依赖、链接参数。**注意这三个变量不在本文件里定义**，它们来自上层 `scipy` 的 `meson.build`，是整个项目共享的配置。
- `install: true`：构建出的 `.so` 要安装。
- `subdir: 'scipy/fftpack'`：安装到 Python 包的 `scipy/fftpack/` 路径下，这样 `import scipy.fftpack.convolve` 才能找到它。

再看纯 Python 源的安装：

[meson.build:11-27](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/meson.build#L11-L27)

```meson
python_sources = [
  '__init__.py',
  '_basic.py',
  '_helper.py',
  '_pseudo_diffs.py',
  '_realtransforms.py',
  'basic.py',
  'helper.py',
  'pseudo_diffs.py',
  'realtransforms.py'
]

py3.install_sources(
  python_sources,
  subdir: 'scipy/fftpack'
)
```

这份 `python_sources` 列表正好印证了 4.1 节的分类：1 个入口 + 4 个核心 + 4 个 shim = 9 个 `.py`。它们没有任何编译步骤，`install_sources` 只是「按原样安装」。**`convolve.pyx` 不在这个列表里**——因为它走的是上面的编译通道，而不是这里。

最后一行开启测试子目录的构建：

[meson.build:29](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/meson.build#L29)

`subdir('tests')` 让 Meson 进入 `tests/` 子目录并执行那里的 `meson.build`，把测试的安装逻辑接管过去（见 4.3 节）。

#### 4.2.4 代码实践

**实践目标**：验证「`convolve` 是编译扩展，而 `_basic` 是纯 Python」这一区分，并体会构建系统的产出差异。

**操作步骤**（**示例代码**，非项目原有文件）：

```python
import scipy.fftpack.convolve as conv
import scipy.fftpack._basic as basic

print("convolve 模块文件:", getattr(conv, "__file__", "（内置/无__file__）"))
print("_basic  模块文件:", basic.__file__)
```

**需要观察的现象**：`convolve.__file__` 的扩展名应为 `.so`（Linux/macOS）或 `.pyd`（Windows），表示它是编译后的二进制扩展；`_basic.__file__` 应以 `.py` 结尾，表示它是纯 Python 源。

**预期结果**：两条路径一个指向二进制、一个指向 `.py` 源文件，恰好对应 `meson.build` 里 `extension_module` 与 `install_sources` 两条不同通道。> 待本地验证：具体路径取决于你的安装方式（pip wheel / conda / 源码 `meson install`），但扩展名差异是稳定的判断依据。

#### 4.2.5 小练习与答案

**练习 1**：`convolve.pyx` 为什么不出现在 `python_sources` 列表里？

> **参考答案**：因为它需要编译。它由第 1-8 行的 `py3.extension_module('convolve', cython_gen.process('convolve.pyx'), …)` 单独处理，走的是「编译成扩展模块」的通道，而不是「原样安装纯 Python 源」的通道。

**练习 2**：`c_args: cython_c_args` 里的 `cython_c_args` 在当前这个 `meson.build` 里找不到定义，这是为什么？

> **参考答案**：它是上层 `scipy` 主 `meson.build` 定义的共享变量，本子目录的 `meson.build` 直接复用。Meson 里子项目录会继承父作用域里的变量。

### 4.3 `tests/meson.build`：测试代码与 `.npz` 数据的安装

#### 4.3.1 概念说明

`scipy/fftpack/tests/` 目录里既有测试脚本（`test_basic.py` 等），又有几个体积不小的二进制参考数据文件（`.npz`）。这些 `.npz` 是用 FFTW 生成的「标准答案」，测试时拿 `fftpack` 的输出和它们比对，从而验证正确性。要让测试在任何安装位置都能跑，这些数据文件必须**和测试脚本一起被安装**。这件事就由 `tests/meson.build` 负责。

#### 4.3.2 核心流程

```
tests/meson.build 执行流程
├─ 1. python_sources = [ test_*.py 测试脚本 ]
├─ 2. test_sources   = [ *.npz 参考数据 ]
└─ 3. py3.install_sources([python_sources, test_sources],
                          subdir:'scipy/fftpack/tests',
                          install_tag:'tests')     ← 关键：贴上 'tests' 标签
```

把「代码」和「数据」合在一个 `install_sources` 里一起安装，并用 `install_tag: 'tests'` 统一标记。

#### 4.3.3 源码精读

[tests/meson.build:1-21](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/tests/meson.build#L1-L21)

```meson
python_sources = [
  '__init__.py',
  'test_basic.py',
  'test_helper.py',
  'test_import.py',
  'test_pseudo_diffs.py',
  'test_real_transforms.py'
]

test_sources = [
  'fftw_double_ref.npz',
  'fftw_longdouble_ref.npz',
  'fftw_single_ref.npz',
  'test.npz'
]

py3.install_sources(
  [python_sources, test_sources],
  subdir: 'scipy/fftpack/tests',
  install_tag: 'tests'
)
```

两个列表合在一起安装到 `scipy/fftpack/tests/`：

- `python_sources`：6 个测试 `.py`（含一个空的 `__init__.py`，使 `tests` 成为可导入包）。
- `test_sources`：4 个 `.npz` 参考数据。注意命名透露了用途——`fftw_double_ref.npz` / `fftw_single_ref.npz` / `fftw_longdouble_ref.npz` 分别是 double/single/longdouble 三种精度的 FFTW 参考输出，`test.npz` 是通用测试数据。它们由同目录的 `gen_fftw_ref.py` 脚本生成（如需了解生成过程，可阅读该脚本）。

最后的关键参数是 `install_tag: 'tests'`：它给所有这些文件贴上 `tests` 标签。打包生产 wheel 时可用 Meson 的标签机制跳过它们，使发布的二进制包更精简；而开发或运行测试套件时则仍能取到这些数据。

#### 4.3.4 代码实践

**实践目标**：确认 `.npz` 参考数据确实随包安装，并能被测试代码在运行时定位到。

**操作步骤**（**示例代码**，非项目原有文件）：

```python
import os, scipy.fftpack.tests as t

test_dir = os.path.dirname(t.__file__)
npz = sorted(f for f in os.listdir(test_dir) if f.endswith(".npz"))
print("随包安装的 .npz 参考数据:", npz)
```

**需要观察的现象**：应列出 4 个 `.npz` 文件（`fftw_double_ref.npz` 等）。

**预期结果**：即便你只装了 `scipy` 而没下载源码，这些 `.npz` 也应出现在安装目录的 `scipy/fftpack/tests/` 下——这正是 `tests/meson.build` 第 17-21 行的功劳。> 待本地验证：某些精简 wheel（用 `--tags` 排除了 `tests`）可能不含这些文件，此时列表为空，可据此判断你装的是否为「带测试数据」的发行版。

#### 4.3.5 小练习与答案

**练习 1**：为什么要把 `.npz` 和 `test_*.py` 放进同一个 `install_sources` 调用，还要加 `install_tag: 'tests'`？

> **参考答案**：放在一起是因为测试脚本运行时需要读取这些参考数据（二者必须同进同退）；加 `install_tag: 'tests'` 是为了在打生产 wheel 时能按标签整体跳过它们，让发行包更小。

**练习 2**：`fftw_double_ref.npz` 这个名字里 `fftw` 和 `double` 分别暗示了什么？

> **参考答案**：`fftw` 表示参考数据由 **FFTW** 库生成（作为「标准答案」）；`double` 表示这是 **double 精度**（64 位浮点）下的参考输出。同目录还有 single / longdouble 精度版本。

### 4.4 `MANIFEST.in`：源码发行版的额外文件清单

#### 4.4.1 概念说明

`.py` 文件默认会被打进 sdist，但 C 源、Fortran 源、文档、测试数据等「非 Python 文件」需要显式声明，否则打包工具不会带上它们。`MANIFEST.in` 就是这份「额外清单」，用一组指令描述要纳入 sdist 的文件模式。阅读它能帮你理解一个模块在历史上依赖过哪些底层源码。

#### 4.4.2 核心流程

`MANIFEST.in` 的常用指令含义如下（本文件全部用到）：

| 指令 | 含义 |
| --- | --- |
| `include <模式>` | 纳入（当前目录下）匹配的单个/多个文件 |
| `recursive-include <目录> <模式>` | 递归纳入某子目录下所有匹配文件 |

构建 sdist 时，打包工具会先带上默认的 `.py` 等文件，再按这份清单补入额外文件。

#### 4.4.3 源码精读

[MANIFEST.in:1-6](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/MANIFEST.in#L1-L6)

```
recursive-include src *.c
recursive-include tests test_*.py
recursive-include dfftpack *.f doc doc.double
include NOTES.txt
include *.pyf
recursive-include djbfft-0.76 *
```

逐行解读：

- 第 1 行 `recursive-include src *.c`：纳入 `src/` 下所有 `.c` 文件（通常是 Cython/Fortran 转译生成的 C 中间产物）。
- 第 2 行 `recursive-include tests test_*.py`：纳入测试脚本。
- 第 3 行 `recursive-include dfftpack *.f doc doc.double`：纳入 `dfftpack/` 下的 Fortran 源（`.f`）与文档。
- 第 4 行 `include NOTES.txt`：纳入 `NOTES.txt`。
- 第 5 行 `include *.pyf`：纳入 `.pyf` 文件（`.pyf` 是 f2py 的接口定义文件，用来包装 Fortran）。
- 第 6 行 `recursive-include djbfft-0.76 *`：纳入 `djbfft-0.76/` 下全部文件。

**一个值得留意的观察**：`dfftpack`、`djbfft-0.76`、`src`、`NOTES.txt`、`.pyf` 这些路径在当前 `scipy/fftpack/` 目录里**已经不存在**了（可逐一确认，目录下并无这些条目）。这说明本文件保留了不少**Fortran 时代的痕迹**——`dfftpack` 是经典的 Fortran FFTPACK 源码目录，`djbfft-0.76` 是历史上捆绑的 DJ Bernstein FFT 库。随着 SciPy 把 FFT 后端迁移到 DUCC（见 4.1.3 节 `convolve.pyx` 的 import），这些 Fortran 源已从本目录移除，但 `MANIFEST.in` 仍遗留了对它们的引用。

> 这是一个很有教育意义的「考古」细节：**构建/打包文件常比实际代码变化得慢**，阅读它们能读到模块的演进史。但请注意：`MANIFEST.in` 中引用不存在的路径属于历史遗留，本讲不保证它在未来版本的行为；具体打包效果 > 待本地确认。

#### 4.4.4 代码实践

**实践目标**：核对 `MANIFEST.in` 引用的路径在当前目录里是否真实存在，体会「历史遗留」。

**操作步骤**（**示例代码**，非项目原有文件）：

```python
import os
root = "scipy/fftpack"            # 按实际路径调整
entries = os.listdir(root)
for legacy in ["src", "dfftpack", "djbfft-0.76", "NOTES.txt"]:
    print(f"{legacy:15} 存在? {legacy in entries}")
# 检查是否有 .pyf 文件
print("*.pyf 文件:", [f for f in entries if f.endswith(".pyf")])
```

**需要观察的现象**：这几项多数应为「不存在」，说明它们是历史遗留引用。

**预期结果**：`src`、`dfftpack`、`djbfft-0.76`、`NOTES.txt` 基本都返回 `False`，`.pyf` 列表为空。结合 `convolve.pyx` 第 1 行对 DUCC 的导入，你能得出结论：底层计算早已离开 Fortran FFTPACK、改用 DUCC，而 `MANIFEST.in` 没有同步更新。> 待本地验证：不同 SciPy 版本可能有差异，请以你本地副本为准。

#### 4.4.5 小练习与答案

**练习 1**：`MANIFEST.in` 与 `meson.build` 分别管什么阶段？

> **参考答案**：`MANIFEST.in` 管 **sdist 打包阶段**（决定源码发行包里多带哪些非 Python 文件）；`meson.build` 管 **构建与安装阶段**（编译扩展、安装 `.py` 源和测试数据到运行环境）。两者关注点不同。

**练习 2**：为什么 `MANIFEST.in` 里会出现 `dfftpack`、`djbfft-0.76` 这种当前已不存在的目录？

> **参考答案**：它们是 Fortran 时代 `fftpack` 依赖的底层源码目录（Fortran FFTPACK 与 DJB FFT）。后端迁移到 DUCC 后这些目录被移除，但 `MANIFEST.in` 没有同步清理，留下了历史痕迹。

## 5. 综合实践

把本讲四个最小模块串起来，完成下面这个「文件依赖与构建全景图」任务：

1. **分类盘点**：列出 `scipy/fftpack/` 下全部文件，按下表分类填空（可套用 4.1.4 的脚本）：

   | 类别 | 文件 |
   | ---- | ---- |
   | 包入口 | |
   | 核心实现（私有） | |
   | 弃用垫片（shim） | |
   | Cython 扩展源 | |
   | 构建配置 | |
   | 测试 | |

2. **画出依赖关系图**：用文本框图表达「谁被谁编译、谁被谁安装、谁调用谁」。下面是一个**示例答案骨架**（请据实补全）：

   ```
   构建期：
     convolve.pyx ──(Cython编译, meson.build:1-8)──▶ convolve(.so 扩展模块)
     9 个 .py     ──(原样安装, meson.build:24-27)──▶ scipy/fftpack/*.py
     tests/*.py + *.npz ──(install_tag:'tests')──▶ scipy/fftpack/tests/

   运行期调用链（关键一条）：
     scipy.fftpack._pseudo_diffs
        └─ scipy.fftpack.convolve (.so)
              └─ scipy.fft._duccfft.pyduccfft.r2r_fftpack   ← 真正的 FFT 内核
   ```

   要求：在图上明确标注「**编译产物**」（只有 `convolve`）与「**纯 Python 安装**」（其余 9 个 `.py`）。

3. **解释测试数据安装**：用自己的话写 2-3 句，回答——为什么 `tests/meson.build` 要把 `.npz` 和 `test_*.py` 一起安装？`install_tag: 'tests'` 又起到什么作用？（参考 4.3.5 的答案核对。）

4. **历史考古（选做）**：运行 4.4.4 的脚本，记录哪些 `MANIFEST.in` 引用的路径已不存在，并写一句话解释它反映了 `fftpack` 怎样的后端迁移史。

## 6. 本讲小结

- `scipy/fftpack/` 的文件可清晰分为四类：**包入口、核心实现（`_xxx.py`）、弃用垫片（`xxx.py`）、Cython 扩展（`convolve.pyx`）**，外加构建配置与测试。
- 四个核心模块与四个 shim **严格成对**（`_basic.py`↔`basic.py` 等）：带下划线是「新家」，不带下划线是拦截图省事导入的「旧地址垫片」（`u1-l1` 机制在文件层的体现）。
- [`meson.build`](meson.build) 用 `py3.extension_module('convolve', …)`（第 1-8 行）**编译** `convolve.pyx` 为扩展模块，用 `py3.install_sources(python_sources, …)`（第 24-27 行）**原样安装**其余 9 个 `.py`，并用 `subdir('tests')`（第 29 行）递归处理测试目录。
- [`tests/meson.build`](tests/meson.build) 把 `test_*.py` 与 `.npz` 参考数据（FFTW 生成的「标准答案」）一并安装到 `scipy/fftpack/tests/`，并贴 `install_tag: 'tests'` 以便生产 wheel 跳过。
- [`MANIFEST.in`](MANIFEST.in) 是 sdist 的额外文件清单；其中 `dfftpack`、`djbfft-0.76` 等当前已不存在的路径，是 Fortran FFTPACK 时代的历史遗留，反映了 `fftpack` 后端向 DUCC 的迁移。

## 7. 下一步学习建议

现在你已经掌握了 `fftpack` 的目录全貌和构建方式。接下来的两条学习路线可以根据兴趣选择：

- **按顺序推进单元 1**：下一篇 `u1-l3`（模块导出与公共 API 体系）会更细致地拆解 `__all__` 与五大功能分组，建议先读它，建立完整的 API 地图；之后 `u1-l4` 会带你真正动手跑一维 FFT。
- **顺藤摸瓜读卷积后端**：如果你对 4.1.3 里「`convolve.pyx` 委托 DUCC」这条线索感兴趣，可以在读完本单元后直接跳到单元 3，那里会逐行剖析 `convolve.pyx` 与伪微分算子的关系。

> 阅读提示：在进入下一篇前，建议先完成本讲「综合实践」的分类盘点和依赖关系图——亲手画一遍，比读十遍印象都深。
