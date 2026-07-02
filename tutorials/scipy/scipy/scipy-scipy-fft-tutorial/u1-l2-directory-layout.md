# 目录结构与四层架构：从 `__init__` 到 ducc 核心

## 1. 本讲目标

上一讲（u1-l1）我们看懂了 `scipy.fft` 是什么、它对外暴露了哪些公共函数。这一讲我们要把目光**从「门面」移到「房间」**：打开 `scipy/fft/` 这个目录，看清楚里面每一份文件各管什么，以及一个普通的 `scipy.fft.fft(x)` 调用，要穿过几层「门」才能最终跑到 C++ 写的计算内核里。

学完本讲你应该能够：

1. 说出 `scipy/fft/` 目录下每个文件的职责。
2. 读懂 `meson.build` 安装清单，知道哪些文件会被装进你的 Python 环境、哪些是 C 扩展。
3. 在脑海里建立 **「公共 API → uarray 分派 → 后端 → ducc 核心」** 的四层心智模型。
4. 理解 `_basic.py` 与 `_basic_backend.py`、`_duccfft/` 这三组文件之间「同名却分工不同」的对应关系。

这是后续所有进阶讲义（分派机制、后端开发、计算核心）的地图。把这张地图记住，后面读任何一处源码都不会迷路。

## 2. 前置知识

本讲是 beginner 级别，不要求你会写 FFT，但有两个上一讲建立的认知要带上：

- **模块文档字符串与 `__all__`**：`scipy/fft/__init__.py` 顶部那段大注释只是「文档分类」，`__all__` 才是「对外契约」。它们决定了哪些名字能被 `from scipy.fft import *` 看到。
- **后端（backend）**：`scipy.fft` 的函数并不直接计算，而是把计算「外包」给某个后端；后端可以替换（默认是 scipy 自带的，也可以是 CuPy 等）。这正是本讲四层架构的由来。

另外补充两个本讲会用到的通用概念：

- **构建系统（build system）**：SciPy 用 **Meson** 作为构建工具。你可以把 `meson.build` 理解成一份「装修清单」：它告诉构建工具，哪些 `.py` 文件要原样安装（拷贝）到目标目录，哪些 `.cxx`（C++）文件要编译成可被 Python `import` 的扩展模块。
- **`functools.partial`**：Python 标准库工具，用来「冻结」一个函数的部分参数，从而派生出一个新函数。你会在 `_duccfft/basic.py` 里看到它被用来从同一个 `c2c` 函数派生出 `fft` 和 `ifft`。本讲只需要知道「partial = 把某个参数固定住，得到一个新名字的函数」即可。

## 3. 本讲源码地图

本讲围绕下面几个文件展开。先有个总体印象，后面再逐层精读。

| 文件 | 所在层 | 作用 |
|------|--------|------|
| [`__init__.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/__init__.py) | 公共 API | 包的索引：把各子模块的函数搬进 `scipy.fft` 命名空间 |
| [`meson.build`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/meson.build) | 构建 | 声明要安装的 Python 源文件，并进入 `_duccfft`、`tests` 子目录 |
| [`_basic.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py) | 公共 API | `fft/ifft/fftn/...` 的「对外签名 + docstring」，被 `_dispatch` 装饰成可分派函数 |
| [`_realtransforms.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_realtransforms.py) | 公共 API | `dct/dst/...` 的对外签名 |
| [`_fftlog.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_fftlog.py) | 公共 API | `fht/ifht` 快速 Hankel 变换的对外签名 |
| [`_helper.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_helper.py) | 公共 API | `fftfreq/fftshift/next_fast_len/...` 辅助函数 |
| [`_backend.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py) | 分派 + 后端 | 后端管理 API + 默认后端 `_ScipyBackend` |
| [`_basic_backend.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py) | 后端 | scipy 后端里 `fft/ifft` 的真正实现（桥接 numpy 与 ducc） |
| [`_realtransforms_backend.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_realtransforms_backend.py) | 后端 | scipy 后端里 `dct/dst` 的实现 |
| [`_debug_backends.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_debug_backends.py) | 后端 | 调试用后端（NumPy/Echo） |
| [`_duccfft/__init__.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/__init__.py) | 计算核心 | ducc 核心的包索引，汇聚 `basic/realtransforms/helper` |
| [`_duccfft/basic.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py) | 计算核心 | `c2c/r2c/c2r` 内核，调 C 扩展 `pyduccfft` |
| [`_duccfft/helper.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py) | 计算核心 | 输入预处理（浮点化、补零、归一化）+ `workers` 并行管理 |
| [`_duccfft/pyduccfft.cxx`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/pyduccfft.cxx) | 计算核心 | C++ 扩展（基于 ducc0 库的真正 FFT 计算） |

> 一个直观的命名规律（本讲的关键线索）：**文件名带 `_backend` 后缀的属于「后端层」，同名无后缀的属于「公共 API 层」**。例如 `_basic.py` 是对外签名，`_basic_backend.py` 是同名函数的真正实现。最终两者都汇入 `_duccfft/` 这个计算核心。

## 4. 核心概念与源码讲解

### 4.1 目录结构：每一份文件的职责

#### 4.1.1 概念说明

`scipy/fft/` 不是一个「一个大文件装天下」的目录。它把职责拆得很细：**对外给人看的签名**、**内部真正干活的实现**、**把两者粘合起来的分派机制**、**底层的 C++ 计算**，分别放在不同的文件里。这种拆分的好处是：想换一个计算后端（比如用 GPU）时，只需要替换「后端层」和「核心层」，对用户完全透明。

我们先把目录里「是谁」搞清楚，「怎么连」（分派与构建）留到 4.2 和 4.3。

#### 4.1.2 核心流程

按职责把文件分成四堆（本讲的四层架构雏形）：

```
公共 API 层：  __init__.py  _basic.py  _realtransforms.py  _fftlog.py  _helper.py
分派/后端层：  _backend.py  _basic_backend.py  _realtransforms_backend.py
              _fftlog_backend.py  _debug_backends.py
计算核心层：   _duccfft/  (basic.py helper.py realtransforms.py pyduccfft.cxx)
测试层：       tests/  (test_basic.py mock_backend.py ...)
构建文件：     meson.build  _duccfft/meson.build  tests/meson.build
```

注意几个容易混淆的点：

- `__init__.py` **不写算法**，它只是一份「搬运清单」，把子模块里已经定义好的函数搬进 `scipy.fft` 这个名字空间。
- `_basic.py` 体积很大（约 64KB），但**大部分是 docstring 和示例**，真正的逻辑只有「被装饰成可分派函数」那几行。
- `_duccfft/` 是一个**子包**（子目录），它有自己的 `__init__.py` 和 `meson.build`，是独立的计算核心。

#### 4.1.3 源码精读

先看包索引 `__init__.py` 的「搬运」部分。这段 `import` 决定了 `scipy.fft` 里能看到哪些函数，以及它们各自来自哪个子模块：

[`__init__.py:L86-L97`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/__init__.py#L86-L97) —— 把五大类函数从各子模块搬进命名空间。注意最后一行：`set_workers/get_workers` 来自 `_duccfft.helper`，说明「计算核心」也向上贡献了公共 API。

```python
from ._basic import (fft, ifft, fft2, ... hfftn, ihfftn)
from ._realtransforms import dct, idct, dst, idst, dctn, idctn, dstn, idstn
from ._fftlog import fht, ifht, fhtoffset
from ._helper import (next_fast_len, prev_fast_len, fftfreq, rfftfreq, fftshift, ifftshift)
from ._backend import (set_backend, skip_backend, set_global_backend, register_backend)
from ._duccfft.helper import set_workers, get_workers
```

再看 `_duccfft/__init__.py`，它和顶层 `__init__.py` 是同样的「搬运工」角色，只不过搬运的范围是计算核心内部：

[`_duccfft/__init__.py:L1-L9`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/__init__.py#L1-L9) —— 用 `from .basic import *` 等三条语句，把内核函数（`fft`/`rfft`/`dct`/`set_workers`…）汇聚到 `_duccfft` 这个名字下，供后端层调用。

```python
""" FFT backend using pyduccfft """
from .basic import *
from .realtransforms import *
from .helper import *
```

最后看一眼计算核心里「真正算 FFT」的内核长什么样。`_duccfft/basic.py` 中的 `fft`/`ifft` 不是手写的，而是用 `functools.partial` 从同一个 `c2c` 函数派生出来的，最终调用 C 扩展 `pfft.c2c`（`pfft` 是 `pyduccfft` 的别名）：

[`_duccfft/basic.py:L31-L37`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L31-L37) —— `c2c` 内核调用 C 扩展 `pfft.c2c`；`fft`（forward=True）与 `ifft`（forward=False）由它派生。这就是计算的最底层。

```python
    return pfft.c2c(tmp, (axis,), forward, norm, out, workers)

fft = functools.partial(c2c, True)
fft.__name__ = 'fft'
ifft = functools.partial(c2c, False)
ifft.__name__ = 'ifft'
```

#### 4.1.4 代码实践（源码阅读型）

**目标**：亲手验证「公共函数最终落在哪个文件」的对应关系，巩固命名规律。

**操作步骤**：

1. 在项目根目录用编辑器同时打开 `scipy/fft/__init__.py` 与 `scipy/fft/_basic.py`。
2. 选定一个函数，例如 `fft`。它在 `__init__.py` 第 86 行的 `from ._basic import (...)` 中被搬入，说明对外签名在 `_basic.py`。
3. 在 `_basic.py` 中搜索 `def fft`，确认它的函数体几乎只返回 `return (Dispatchable(x, np.ndarray),)`（见 [`_basic.py:L168`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L168)），并没有真正算 FFT。
4. 接着打开 `_basic_backend.py`，搜索 `def fft` —— 你会发现这里**没有** `def fft`，而是有 `_execute_1D`（后面 4.3 会讲为何如此）。
5. 最后打开 `_duccfft/basic.py`，确认 `fft` 在这里由 `partial(c2c, True)` 派生。

**需要观察的现象**：`fft` 这个名字**在三处文件都出现**，但身份完全不同——`__init__.py` 是搬运、`_basic.py` 是对外签名（空壳）、`_duccfft/basic.py` 是真实现。这正是「四层架构」带来的同名现象。

**预期结果**：你会得到一张「同名三身」的对照，深刻体会到读源码时**先看文件所在的层**再读代码的重要性。

#### 4.1.5 小练习与答案

**练习 1**：`fhtoffset` 这个函数在 `__init__.py` 的文档分类里被归到「Helper functions」，但它实际从哪个模块 `import` 进来？

**答案**：从 [`__init__.py:L91`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/__init__.py#L91) 的 `from ._fftlog import fht, ifht, fhtoffset` 可见，它来自 `_fftlog`，而不是 `_helper`。这正是上一讲强调的「文档功能分类 ≠ 代码物理出处」。

**练习 2**：目录里有两份 `basic.py`（`_basic.py` 和 `_duccfft/basic.py`），它们各自属于哪一层？

**答案**：`_basic.py` 属于公共 API 层（对外签名 + docstring，函数体是空壳）；`_duccfft/basic.py` 属于计算核心层（真正调用 C 扩展 `pyduccfft` 计算 FFT）。

---

### 4.2 meson 构建：哪些文件会被安装

#### 4.2.1 概念说明

光看 `.py` 文件还不够，我们还要知道**这些文件如何变成你电脑上能 `import scipy.fft` 的东西**。这就需要看构建文件 `meson.build`。

Meson 是 SciPy 的构建系统。`meson.build` 文件里最关键的两类指令是：

- `py3.install_sources(...)`：把 Python 源文件**原样拷贝**到安装目录。
- `py3.extension_module(...)`：把 C/C++ 源文件**编译**成 Python 扩展模块（一个 `.so`/`.pyd` 文件），让你能 `import` 它。

`subdir(...)` 则表示「进入子目录，执行那个目录里的 `meson.build`」，是一种递归组织方式。

#### 4.2.2 核心流程

`scipy/fft/meson.build` 的逻辑非常简洁，可以概括成三步：

```
1. 列出本目录要安装的 10 个 Python 文件（python_sources）
2. 把它们安装到 scipy/fft 子目录
3. subdir('_duccfft')  -> 进入计算核心子目录构建
   subdir('tests')     -> 进入测试子目录构建
```

值得注意的是：**这份清单里没有 `__init__.py` 之外任何「真正算 FFT」的纯算法文件**——因为真正的计算是 C++ 扩展，它在 `_duccfft/meson.build` 里通过 `py3.extension_module('pyduccfft', 'pyduccfft.cxx', ...)` 单独编译。

#### 4.2.3 源码精读

[`meson.build:L1-L21`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/meson.build#L1-L21) —— 顶层构建清单：10 个 Python 文件 + 进入两个子目录。

```meson
python_sources = [
  '__init__.py', '_backend.py', '_basic.py', '_basic_backend.py',
  '_debug_backends.py', '_fftlog.py', '_fftlog_backend.py',
  '_helper.py', '_realtransforms.py', '_realtransforms_backend.py'
]

py3.install_sources(python_sources, subdir: 'scipy/fft')

subdir('_duccfft')
subdir('tests')
```

对照 [`_duccfft/meson.build`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/meson.build) 里的两段，可以看到计算核心的构建方式完全不同——它既编译 C++ 扩展，又安装 Python 胶水文件：

```meson
py3.extension_module(
    'pyduccfft',
    'pyduccfft.cxx',
    dependencies: [fft_deps, pybind11_dep, duccfft_dep],
    install: true,
    subdir: 'scipy/fft/_duccfft',
)

python_sources = ['__init__.py', 'basic.py', 'helper.py', 'LICENSE.md', 'realtransforms.py']
py3.install_sources(python_sources, subdir: 'scipy/fft/_duccfft')
```

这里能学到两个细节：

- `pyduccfft.cxx` 通过 `pybind11_dep`（pybind11 库）和 `duccfft_dep`（真正的 ducc0 算法库）编译成 `pyduccfft` 扩展模块——这就是 4.1 里 `pfft.c2c` 的来源。
- `LICENSE.md` 也被安装，因为 ducc0 是第三方库，需要随包分发许可证。

#### 4.2.4 代码实践（源码阅读型）

**目标**：从构建清单反推「安装后磁盘上长什么样」。

**操作步骤**：

1. 打开 `scipy/fft/meson.build` 与 `scipy/fft/_duccfft/meson.build`。
2. 列出两份清单里 `python_sources` 的所有文件名。
3. 在你本地安装好的 SciPy 环境里，找到 `scipy/fft` 的实际安装路径（可在 Python 里执行下面的脚本）。

**需要观察的现象**：安装目录里的 `.py` 文件集合，应当与 `meson.build` 的 `python_sources` 完全一致；`_duccfft/` 目录下还应该多出一个编译产物 `pyduccfft*.so`（Windows 上是 `.pyd`）。

**预期结果**：示例查询脚本（可直接运行）：

```python
# 示例代码：查看本地安装的 scipy.fft 实际文件布局
import scipy.fft, os, pathlib
pkg_dir = pathlib.Path(scipy.fft.__file__).parent
for p in sorted(pkg_dir.rglob("*")):
    if p.is_file():
        print(p.relative_to(pkg_dir))
```

> 待本地验证：`_duccfft` 子目录下是否真的存在一个 `pyduccfft` 开头的扩展模块文件（`.so` / `.pyd`）。

#### 4.2.5 小练习与答案

**练习 1**：为什么顶层 `scipy/fft/meson.build` 的 `python_sources` 里**没有** `pyduccfft.cxx`？

**答案**：因为 `pyduccfft.cxx` 是 C++ 源码，需要被**编译**成扩展模块，而不是被原样拷贝。它的编译声明在 [`_duccfft/meson.build`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/meson.build) 的 `py3.extension_module(...)` 里。顶层清单只列纯 Python 文件。

**练习 2**：`subdir('_duccfft')` 这一行的作用是什么？如果删掉它会怎样？

**答案**：它告诉 Meson「进入 `_duccfft` 子目录并执行那里的 `meson.build`」，从而把计算核心的构建纳入整体构建。删掉它会导致 `pyduccfft` 扩展不会被编译，安装后 `import scipy.fft` 会在调用任何变换时因找不到 `_duccfft.pyduccfft` 而报错。

---

### 4.3 分层架构：一次调用穿过四层

#### 4.3.1 概念说明

现在把 4.1 的「文件职责」和 4.2 的「构建」合起来，看最关键的问题：**当用户写下 `scipy.fft.fft(x)` 时，调用栈是怎样穿过四层的？**

四层从上到下是：

1. **公共 API 层**：`_basic.py` 的 `fft`。它只是「签名 + 文档」，函数体声明「我是可分派的，唯一可被后端替换的参数是 `x`」。
2. **分派层（uarray）**：一个独立于本目录的库 `scipy._lib.uarray`，根据「当前激活了哪个后端」把调用路由出去。本目录里 `_backend.py` 的 `_ScipyBackend` 是「默认后端的入口」。
3. **后端层**：`_basic_backend.py` 的 `_execute_1D` 等。它负责把数组在 numpy 与其他数组库（array API）之间协调，然后调用计算核心。
4. **计算核心层**：`_duccfft/` 下的内核，最终落到 C 扩展 `pyduccfft` 真正算 FFT。

为什么非要分四层？因为这样**算法（ducc）、分派规则（uarray）、数组兼容（backend）三者解耦**：换 GPU 后端只动后端层，换算法只动核心层，用户代码一行都不用改。

#### 4.3.2 核心流程

以 `scipy.fft.fft(x)` 为例，调用穿透四层的路径（伪代码）：

```
用户调用 scipy.fft.fft(x)
        │  (第1层 公共API)
        ▼
_basic.fft —— 被 @_dispatch 装饰，函数体只 return (Dispatchable(x, np.ndarray),)
        │  uarray 拿到这个声明，去查「当前激活的后端」
        │  (第2层 分派)
        ▼
_backend._ScipyBackend.__ua_function__(method=fft, args=(x,), kwargs=...)
        │  按方法名 'fft' 在三个 *_backend 模块里查找实现
        ▼
_basic_backend.fft(...) → 内部调 _execute_1D('fft', _duccfft.fft, x, ...)
        │  (第3层 后端：is_numpy? 走 duccfft 直连)
        ▼
_duccfft.fft  (= partial(c2c, True)) → 预处理(_asfarray/_fix_shape/_normalization)
        │  (第4层 计算核心)
        ▼
pyduccfft.c2c(...)   ← C++ 扩展，真正算 FFT
```

一个关键设计：**第 1 层和第 2、3、4 层之间没有直接函数调用**，而是通过 uarray 的「按名字查找」解耦。这意味着后端甚至可以是完全另一个库（如 CuPy），只要它实现了同样的协议。

#### 4.3.3 源码精读

**第 1 层 —— 公共 API 是如何变成「可分派空壳」的。** 看装饰器 `_dispatch`：

[`_basic.py:L18-L28`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L18-L28) —— `@_dispatch` 用 `generate_multimethod` 把普通函数变成 uarray 多方法；`fft` 的函数体（L168）只返回「可替换参数」声明，不做任何计算。

```python
def _dispatch(func):
    """Function annotation that creates a uarray multimethod from the function"""
    return generate_multimethod(func, _x_replacer, domain="numpy.scipy.fft")

@xp_capabilities(allow_dask_compute=True)
@_dispatch
def fft(x, n=None, axis=-1, norm=None, overwrite_x=False, workers=None, *, plan=None):
    """Compute the 1-D discrete Fourier Transform. ..."""
    ...
    return (Dispatchable(x, np.ndarray),)   # 见 _basic.py:L168
```

要点：`domain="numpy.scipy.fft"` 是所有 `scipy.fft` 函数共享的「频道名」，后端正是通过声明同一个 domain 来接入的。

**第 2+3 层 —— 默认后端按方法名查找实现。** 看 `_ScipyBackend`：

[`_backend.py:L8-L34`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L8-L34) —— `_ScipyBackend` 声明 domain，并在 `__ua_function__` 里依次到 `_basic_backend` / `_realtransforms_backend` / `_fftlog_backend` 三个模块按 `method.__name__` 找实现；`_named_backends` 把字符串 `'scipy'` 映射到这个类。

```python
class _ScipyBackend:
    __ua_domain__ = "numpy.scipy.fft"

    @staticmethod
    def __ua_function__(method, args, kwargs):
        fn = getattr(_basic_backend, method.__name__, None)
        if fn is None:
            fn = getattr(_realtransforms_backend, method.__name__, None)
        if fn is None:
            fn = getattr(_fftlog_backend, method.__name__, None)
        if fn is None:
            return NotImplemented
        return fn(*args, **kwargs)

_named_backends = {'scipy': _ScipyBackend}
```

**第 3 层 —— 后端桥接 numpy 与计算核心。** 看 `_execute_1D`：

[`_basic_backend.py:L27-L49`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py#L27-L49) —— `is_numpy` 分支直接把数组交给 `_duccfft.fft`（即 ducc 核心）；非 numpy 数组则尝试 `xp.fft`，再不行就转成 numpy 算完转回。这是后端层最核心的「分流」逻辑。

```python
def _execute_1D(func_str, duccfft_func, x, n, axis, norm, overwrite_x, workers, plan):
    xp = array_namespace(x)
    if is_numpy(xp):
        x = np.asarray(x)
        return duccfft_func(x, n=n, axis=axis, norm=norm,
                            overwrite_x=overwrite_x, workers=workers, plan=plan)
    ...
    x = np.asarray(x)
    y = duccfft_func(x, n=n, axis=axis, norm=norm)
    return xp.asarray(y)
```

**第 4 层 —— 计算核心。** 4.1.3 已展示 `_duccfft/basic.py` 的 `fft = partial(c2c, True)` 与 `pfft.c2c(...)`。把它和上面的 `_execute_1D` 串起来：后端层传入的 `duccfft_func` 正是 `_duccfft.fft`，于是计算最终落到 C 扩展 `pyduccfft`。

#### 4.3.4 代码实践（综合型：画一张模块依赖图）

**目标**：亲手把四层架构画出来，作为本讲综合实践的预热（第 5 节会进一步完整化）。

**操作步骤**：

1. 在一张纸上或一个文本文件里，画四个纵向的方框，从上到下标注：公共 API 层、分派层、后端层、计算核心层。
2. 对 `scipy.fft.fft` 这个调用，在每个方框里填入对应的文件:函数：
   - 公共 API：`_basic.py:fft`
   - 分派：`scipy._lib.uarray` + `_backend.py:_ScipyBackend`
   - 后端：`_basic_backend.py:_execute_1D`
   - 核心：`_duccfft/basic.py:c2c` → `pyduccfft`
3. 用箭头连起来，并在每个箭头上标注「靠什么连接」（第 1→2 层靠 `domain` 与 `Dispatchable`；第 2→3 层靠 `method.__name__` 查找；第 3→4 层靠 `duccfft_func` 参数）。
4. 对照 4.3.3 的源码，校验你画的连接依据是否真实存在。

**需要观察的现象**：第 1 层与第 2/3/4 层之间**没有 import 式的直接调用**，而是通过「约定（domain + 方法名）」间接相连——这是整个架构最反直觉、也最精妙的地方。

**预期结果**：得到一张清晰的「四层 + 三种连接方式」依赖图，能把任何一个 `scipy.fft.*` 函数定位到对应的四层文件。

> 待本地验证：你可以试着用 `scipy.fft.fft([1,2,3,4])` 配合调试器单步进入，观察调用栈是否真的经过 `_ScipyBackend.__ua_function__` 再到 `_execute_1D`。

#### 4.3.5 小练习与答案

**练习 1**：`_ScipyBackend.__ua_function__` 里，为什么要在**三个**不同的 `*_backend` 模块里依次查找方法？

**答案**：因为 `scipy.fft` 的公共函数按数学性质分散在三个模块：FFT 类（`_basic_backend`）、DCT/DST 类（`_realtransforms_backend`）、Hankel 类（`_fftlog_backend`）。`__ua_function__` 收到的方法名（如 `fft` 或 `dct` 或 `fht`）需要到对应的实现模块去找，找不到就返回 `NotImplemented`（见 [`_backend.py:L20-L29`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_backend.py#L20-L29)）。

**练习 2**：如果有人写了一个新的 GPU FFT 库并希望它成为 `scipy.fft` 的后端，他需要实现哪两件事才能被分派层接受？

**答案**：(1) 声明 `__ua_domain__ = "numpy.scipy.fft"`，与公共 API 的 domain 一致；(2) 实现 `__ua_function__(method, args, kwargs)`，对支持的方法名返回计算结果，对不支持的方法返回 `NotImplemented` 以便优雅回退。这正是 `_ScipyBackend` 示范的协议（具体在 u8 单元会动手实现）。

**练习 3**：第 3 层 `_execute_1D` 里，为什么要区分 `is_numpy(xp)` 分支？

**答案**：因为 ducc 核心（`pyduccfft`）只能直接吃 numpy 数组。对 numpy 输入走直连最快；对非 numpy 数组（如 CuPy/PyTorch），则要么借用该数组库自带的 `xp.fft`，要么转成 numpy 算完再转回原类型（见 [`_basic_backend.py:L27-L49`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py#L27-L49)）。

## 5. 综合实践：绘制完整的「四层架构模块依赖图」

把第 4 节三处实践合起来，完成本讲的主任务：**用注释在一张图里同时标注四层边界、文件归属与连接方式**。

建议你创建一个 `fft_layers.md`（注意：不要写进本讲义目录，写在你能自由编辑的位置即可），产出类似下面的依赖图（你可以按真实源码补全更多函数）：

```text
======================== 第1层：公共 API ========================
  __init__.py  (搬运：from ._basic import fft ... ; from ._duccfft.helper import set_workers)
       │
       ├── _basic.py:        fft ifft fftn rfft hfft ...   ← @_dispatch 空壳 (L168: return (Dispatchable(x,...),))
       ├── _realtransforms.py: dct dst dctn dstn ...        ← 同样是 @_dispatch 空壳
       ├── _fftlog.py:        fht ifht fhtoffset
       └── _helper.py:        fftfreq fftshift next_fast_len ...   ← 真实逻辑（无分派）
                            │
                  连接方式 = domain="numpy.scipy.fft" + Dispatchable
                            ▼
======================== 第2层：分派 (uarray) =====================
  scipy._lib.uarray.generate_multimethod   (按 domain 路由)
  _backend.py: _ScipyBackend  __ua_domain__="numpy.scipy.fft"
                            │
                  连接方式 = 按 method.__name__ 在三个模块里查找
                            ▼
======================== 第3层：后端 =============================
  _backend.py:             set_backend / set_global_backend / register_backend (后端管理)
  _basic_backend.py:       _execute_1D / _execute_nD  (is_numpy? ducc 直连 : xp.fft : 转np)
  _realtransforms_backend.py: _execute
  _fftlog_backend.py:      fht/ifht 实现
  _debug_backends.py:      NumPyBackend / EchoBackend (调试)
                            │
                  连接方式 = 把 _duccfft.* 内核作为参数传入 (duccfft_func)
                            ▼
======================== 第4层：计算核心 =========================
  _duccfft/__init__.py     (汇聚 basic/realtransforms/helper)
  _duccfft/basic.py:       c2c r2c c2r → fft=partial(c2c,True)
  _duccfft/helper.py:      _asfarray _fix_shape _normalization _workers / set_workers get_workers
  _duccfft/realtransforms.py: dct/dst 内核
  _duccfft/pyduccfft.cxx:  C++ 扩展 (pfft.c2c ...) ← 真正算 FFT
```

**验收标准**：

1. 图中**每一个公共函数**都能沿箭头追到第 4 层的具体文件。
2. 四层之间的**三种连接方式**（domain+Dispatchable / 方法名查找 / 参数传入）都在图上写明。
3. 能指出哪些公共函数**不经过分派**（提示：`_helper.py` 里的 `fftfreq` 等是直接实现的，对比 [`_helper.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_helper.py) 与 `_basic.py` 的差异）。

> 待本地验证：第 3 点可以用 `hasattr(scipy.fft.fftfreq, '__wrapped__')` 或检查其是否带 uarray 多方法属性来验证它不走分派。

## 6. 本讲小结

- `scipy/fft/` 按**职责**而非「功能」拆分文件：公共 API 层（`_basic.py` 等）只放签名与文档，真正算法藏在 `_duccfft/` 计算核心里。
- 命名规律：**`_xxx.py` 是对外签名，`_xxx_backend.py` 是后端层同名实现**，两者最终汇入 `_duccfft/`。
- `meson.build` 区分两类安装：纯 Python 文件用 `install_sources` 原样拷贝；C++ 计算（`pyduccfft.cxx`）用 `extension_module` 编译成扩展。
- 四层架构为：**公共 API → uarray 分派 → 后端 → ducc 核心**，相邻层靠「domain+Dispatchable / 方法名查找 / 参数传入」三种方式解耦连接。
- 第 1 层函数体只 `return (Dispatchable(x, np.ndarray),)`，本身不算 FFT；计算由第 4 层的 `pyduccfft` C 扩展完成。
- 这种分层让「换算法」「换数组库」「换后端」彼此独立，是后续分派机制（u4 单元）与自定义后端（u8 单元）能成立的基础。

## 7. 下一步学习建议

本讲建立了「地图」，接下来建议按以下顺序深入：

1. **先跑通一次真实调用**：进入 u1-l3《导入、运行与第一次调用》，亲手验证 `fft/ifft` 的可逆性，把本讲的四层架构在运行时感受一遍。
2. **再看公共 API 的参数细节**：u2 单元会逐个讲解 `fft/rfft/fftn` 的参数（`n`/`norm`/`axis`/`workers`），那时你会频繁回到 `_basic.py` 这一层的 docstring。
3. **想理解「分派」的魔法**：直接跳到 u4-l1《uarray 多方法与 `_dispatch`》，那里会拆解本讲第 1↔2 层的连接机制。
4. **想看清「计算核心」**：u5 单元专门讲 `_duccfft`，包括 4.1.3 里出现的 `functools.partial` 派生与输入预处理。

一句话记住本讲：**`scipy.fft` 的目录就是一张四层地图，`__init__` 是索引、`_basic` 是门面、`_backend` 是调度、`_duccfft` 是引擎。**
