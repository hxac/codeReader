# 目录结构与各文件职责

## 1. 本讲目标

上一讲（u1-l1）我们已经学会 `import numpy.fft` 并用 `fft`/`ifft` 跑通一次正反变换。本讲不再写新的变换代码，而是退一步**看清这个子包的「文件地图」**：

- 弄清楚 `numpy/fft/` 目录下到底有哪些文件、每个文件分别负责什么。
- 理解这个包是**三层结构**：入口层 → Python 逻辑层 → C++ 后端层，以及它们之间的 import 链。
- 区分 `.py`（真正实现的 Python 源码）与 `.pyi`（类型存根）的差别。
- 认识 `pocketfft` 这个 git 子模块在整个体系里的位置，以及为什么缺了它会编译失败。

学完后，你拿到这个目录里的任何一个文件，都能立刻说出它属于哪一层、被谁导入、又导入了谁。

## 2. 前置知识

本讲几乎不涉及傅里叶数学，主要讲工程结构。但你需要先建立以下几个概念（上一讲已铺垫）：

- **子包（subpackage）**：`numpy.fft` 是 NumPy 仓库里 `numpy/fft/` 这个目录对应的子包。Python 里，一个目录只要带 `__init__.py` 就被当作一个包。
- **import 的两种写法**：
  - `from . import _pocketfft` 表示「从当前包导入 `_pocketfft` 这个模块（即同目录下的 `_pocketfft.py`）」。
  - `from ._pocketfft import *` 表示「把 `_pocketfft` 模块里 `__all__` 列出的名字全部搬进当前命名空间」。
- **C 扩展模块（extension module）**：用 C/C++ 写、编译成 `.so`（Linux）/`.pyd`（Windows）的模块，导入方式和普通 `.py` 一样，但它的源不是文本而是编译产物。本包里真正的 FFT 计算就来自这样一个 C++ 扩展。
- **类型存根（stub，`.pyi`）**：只写函数签名、不写实现的文件，供类型检查器（如 mypy）和 IDE 补全使用，运行时不参与执行。
- **git 子模块（submodule）**：把另一个独立 git 仓库「嵌入」到当前仓库的某个子目录里。它需要单独 `git submodule update --init` 才会真正下载内容。

## 3. 本讲源码地图

先上一张「全文件清单」表，把 `numpy/fft/` 下的每个文件按层归类。本讲会逐个用到它们。

| 文件 | 所在层 | 语言 | 作用简述 |
|------|--------|------|----------|
| `__init__.py` | 入口层 | Python | 包入口：写模块 docstring、聚合导出、挂载 `test()` |
| `__init__.pyi` | 入口层 | 存根 | `__init__.py` 的类型签名 |
| `_pocketfft.py` | Python 逻辑层 | Python | 全部 14 个变换（fft/ifft/rfft/...）的主逻辑 |
| `_pocketfft.pyi` | Python 逻辑层 | 存根 | 14 个变换的类型签名（含 overload） |
| `_helper.py` | Python 逻辑层 | Python | 4 个 helper：`fftfreq`/`rfftfreq`/`fftshift`/`ifftshift` |
| `_helper.pyi` | Python 逻辑层 | 存根 | 4 个 helper 的类型签名 |
| `_pocketfft_umath.cpp` | C++ 后端层 | C++ | 注册 gufunc、实现 FFT 计算循环 |
| `pocketfft/` | 第三方库 | C++ 头文件 | git 子模块，提供 `pocketfft_hdronly.h` |
| `meson.build` | 构建配置 | Meson DSL | 编译 C++ 扩展、安装 Python 源 |
| `tests/` | 测试 | Python | `test_pocketfft.py`、`test_helper.py` |

## 4. 核心概念与源码讲解

### 4.1 三层架构与 import 链总览

#### 4.1.1 概念说明

`numpy.fft` 之所以值得拆开讲结构，是因为它采用了一个非常经典的设计：**接口和实现分离**。

- 你调用的是 `np.fft.fft` 这样的 Python 函数（接口）。
- 但真正一行行算 FFT 的代码不在 Python 里，而在一个用 C++ 编译出来的扩展模块里（实现）。

这样做的好处是：Python 层负责好用的参数处理、归一化、多维循环编排；C++ 层负责每一条一维 FFT 的极速计算。两者通过 NumPy 的 **ufunc（通用函数）/ gufunc（广义 ufunc）** 机制衔接。

整个包因此自然分成三层，自上而下依次「调用」：

```
入口层        __init__.py          （聚合导出、对外门面）
                 │  from ._pocketfft import *
                 │  from ._helper import *
                 ▼
Python 逻辑层 _pocketfft.py        （14 个变换：参数/norm/多维编排）
              _helper.py           （4 个频率/平移 helper）
                 │  from . import _pocketfft_umath as pfu
                 ▼
C++ 后端层    _pocketfft_umath.cpp  （gufunc 注册 + FFT 循环）  --编译-->  _pocketfft_umath 扩展
                 │  #include "pocketfft/pocketfft_hdronly.h"
                 ▼
第三方库      pocketfft/            （git 子模块，头文件库）
```

理解这张图，就理解了本包的骨架。后面四节（4.2–4.5）只是把这条链上的每个方块拆开看细节。

#### 4.1.2 核心流程

当你在终端写 `import numpy.fft` 时，发生的事情按层展开：

1. Python 找到 `numpy/fft/__init__.py` 并执行它。
2. `__init__.py` 执行 `from . import _helper, _pocketfft`，于是 Python 加载 `_helper.py` 和 `_pocketfft.py`。
3. 加载 `_pocketfft.py` 时，其中一句 `from . import _pocketfft_umath as pfu` 会去**导入 C++ 扩展模块** `_pocketfft_umath`（一个 `.so` 文件，由 `_pocketfft_umath.cpp` 在构建期编译而来）。
4. 这三步完成后，`__init__.py` 把 `_pocketfft` 和 `_helper` 的公开名字拼成 `numpy.fft.__all__`，对外暴露。

注意第 3 步：**如果这个 C++ 扩展没被编译出来（比如你只 clone 了源码没构建），`import numpy.fft` 就会直接失败**。这就是为什么构建系统（`meson.build`）在本包里如此关键。

#### 4.1.3 源码精读

入口文件里这段最关键，它定义了整个包的对外门面：

[__init__.py:203-213](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/__init__.py#L203-L213) —— 这 11 行做了四件事：先 `from . import _helper, _pocketfft` 触发两个子模块加载；再用 `from ... import *` 把它们的公开名字搬进来；接着用 `_pocketfft.__all__.copy()` 再 `+= _helper.__all__` 拼成最终 `numpy.fft.__all__`；最后挂上一个 `test` 函数（来自 `PytestTester`）。

```python
from . import _helper, _pocketfft
from ._helper import *
from ._pocketfft import *

__all__ = _pocketfft.__all__.copy()  # noqa: PLE0605
__all__ += _helper.__all__
```

为什么要 `.copy()`？因为 `__all__` 是 list，若直接 `+=` 会**修改 `_pocketfft` 自己的 `__all__`**，污染那个模块。`.copy()` 先复制一份再拼接，是安全的写法。

而 `_pocketfft.py` 里负责「向下打通 C++ 层」的那一句在：

[_pocketfft.py:48](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L48) —— `from . import _pocketfft_umath as pfu`，`pfu` 就是那个 C++ 扩展模块的别名。后文（4.2）你会看到 `pfu.fft`、`pfu.ifft` 等真正的计算 ufunc 都来自这里。

```python
from . import _pocketfft_umath as pfu
```

#### 4.1.4 代码实践

1. **目标**：亲眼确认三层 import 链确实存在。
2. **操作**：在已装好 NumPy 的环境里执行下面这段（这是「示例代码」，仅为观察用）：
   ```python
   import numpy.fft
   # 入口层是否真有两个子模块属性？
   print(hasattr(numpy.fft, '_pocketfft'))   # True
   print(hasattr(numpy.fft, '_helper'))      # True
   # _pocketfft 是否真的持有一个 _pocketfft_umath（C++ 扩展）？
   import numpy.fft._pocketfft as P
   print(type(P._pocketfft_umath))           # <class 'module'>
   # 这个扩展模块的文件后缀是 .so（编译产物），不是 .py
   print(P._pocketfft_umath.__file__)
   ```
3. **观察现象**：最后一行打印的路径应以 `.so`（Linux/macOS）或 `.pyd`（Windows）结尾，而不是 `.py`。这证明它是**编译出来的 C++ 扩展**，而非纯 Python。
4. **预期结果**：前三行为 `True`/`True`/`<class 'module'>`，最后一行是某个 `.so` 文件路径。
5. 若运行环境无 NumPy：**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `__init__.py` 里的 `__all__ = _pocketfft.__all__.copy()` 改成 `__all__ = _pocketfft.__all__`（去掉 `.copy()`），后续 `__all__ += _helper.__all__` 会有什么副作用？

**答案**：`__all__` 与 `_pocketfft.__all__` 会指向同一个 list，`+=` 会把 4 个 helper 名字也追加进 `_pocketfft.__all__`，污染该模块；调用方若访问 `_pocketfft.__all__` 会得到 18 个名字而非正确的 14 个。

**练习 2**：为什么 `_pocketfft_umath` 用 `import` 就能拿到，但它既不在 `numpy/fft/` 目录下以 `.py` 形式存在、也不在 `__all__` 里？

**答案**：因为它是一个由 `_pocketfft_umath.cpp` **编译生成的 C++ 扩展模块**（产物是 `.so`），Python 的 import 机制同样能加载它；它属于内部实现细节，不是对外公开 API，所以不进 `__all__`。

---

### 4.2 `_pocketfft.py`：14 个变换的 Python 主逻辑

#### 4.2.1 概念说明

`_pocketfft.py`（62KB，本包最大的源文件）是整个子包的「大脑」。上一讲你只用了其中的 `fft`/`ifft`，但这个文件其实定义了**全部 14 个变换**：

- 一维复数：`fft` / `ifft`
- 实数：`rfft` / `irfft`
- Hermitian：`hfft` / `ihfft`
- N 维：`fftn` / `ifftn` / `rfftn` / `irfftn`
- 二维（N 维的默认轴特化）：`fft2` / `ifft2` / `rfft2` / `irfft2`

文件顶部的 docstring 用一段「命名口诀」解释了这些名字的构成，值得记住：

```
i = inverse transform       （反变换）
r = transform of purely real data  （实输入）
h = Hermite transform       （Hermitian 变换）
n = n-dimensional transform （N 维）
2 = 2-dimensional transform （二维，只是 nD 的默认轴不同）
```

文件本身是纯 Python，但它**自己不算 FFT**——它只做参数处理、归一化、多维轴编排，然后把每一条一维计算交给 C++ 扩展 `pfu`。

#### 4.2.2 核心流程

`_pocketfft.py` 对所有一维变换采用一个**统一入口** `_raw_fft`，避免 14 个函数各写一遍：

1. 公开函数（如 `fft`、`ifft`）做参数预处理：`asarray(a)`、确定 `n`（`n is None` 时取 `a.shape[axis]`）、归一化方向。
2. 统一调用 `_raw_fft(a, n, axis, is_real, is_forward, norm, out)`。
3. `_raw_fft` 内部根据 `(is_real, is_forward)` 四种组合，挑选 C++ 扩展里对应的 ufunc：
   - 复数正向 → `pfu.fft`
   - 复数反向 → `pfu.ifft`
   - 实数正向 → `pfu.rfft_n_even` 或 `pfu.rfft_n_odd`（按 `n` 奇偶）
   - 实数反向 → `pfu.irfft`
4. N 维变换（`fftn` 等）则通过 `_raw_fftnd` 对每个轴反复回到第 3 步。

> 这些 ufunc 的具体行为会在 u3（一维核心流程）和 u5（C++ 后端）讲义里展开；本讲只关注「文件职责」。

#### 4.2.3 源码精读

文件开头先声明自己导出哪 14 个名字：

[_pocketfft.py:30-31](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L30-L31) —— 这正是 `__init__.py` 里 `_pocketfft.__all__` 的来源，14 个变换全部在列。

```python
__all__ = ['fft', 'ifft', 'rfft', 'irfft', 'hfft', 'ihfft', 'rfftn',
           'irfftn', 'rfft2', 'irfft2', 'fft2', 'ifft2', 'fftn', 'ifftn']
```

随后是它从 NumPy 核心借来的一批工具函数（用于参数处理、归一化、类型提升）：

[_pocketfft.py:36-46](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L36-L46) —— 注意这些都是从 `numpy._core` 来的通用工具，而非 FFT 专用：`asarray` 转数组、`result_type`/`sqrt`/`reciprocal` 算归一化系数、`overrides` 做 `__array_function__` 分发、`normalize_axis_index` 规范化轴号。

最后是它的「统一入口」签名，看一眼即可，细节留到 u3：

[_pocketfft.py:58-86](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L58-L86) —— `_raw_fft` 根据 `is_real` 和 `is_forward` 选择 C++ ufunc 的那段分支，是「Python 层只编排、C++ 层才算」这一分工的最直接证据。

```python
if is_real:
    if is_forward:
        ufunc = pfu.rfft_n_even if n % 2 == 0 else pfu.rfft_n_odd
        n_out = n // 2 + 1
    else:
        ufunc = pfu.irfft
else:
    ufunc = pfu.fft if is_forward else pfu.ifft
```

#### 4.2.4 代码实践

1. **目标**：验证 `_pocketfft.py` 里的 14 个变换确实都能从 `np.fft` 直接拿到，且它们都指向同一个文件。
2. **操作**：
   ```python
   import numpy.fft as F
   names = ['fft','ifft','rfft','irfft','hfft','ihfft',
            'fftn','ifftn','rfftn','irfftn','fft2','ifft2','rfft2','irfft2']
   files = {n: getattr(F, n).__code__.co_filename for n in names}
   from collections import Counter
   print(Counter(files.values()))
   ```
3. **观察现象**：所有 14 个函数的 `__code__.co_filename` 应指向**同一个 `_pocketfft.py`** 文件路径。
4. **预期结果**：`Counter` 只有一个键（`_pocketfft.py` 的绝对路径），计数为 14。
5. 若运行环境无 NumPy：**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`_pocketfft.py` 里有 14 个变换，但「二维」和「N 维」的关系是什么？

**答案**：二维变换（`fft2`/`ifft2`/`rfft2`/`irfft2`）只是 N 维变换（`fftn` 等）的**默认 `axes=(-2,-1)` 特化版**，文件 docstring 也明确写出 `2D routines are just nD routines with different default behavior`。

**练习 2**：`_pocketfft.py` 自己实现了 FFT 算法吗？

**答案**：没有。它只做参数处理、归一化（`fct`）和轴编排，真正计算委托给 C++ 扩展 `pfu`（即 `_pocketfft_umath`）里的 ufunc。

---

### 4.3 `_helper.py`：4 个频率与平移 helper

#### 4.3.1 概念说明

`_helper.py`（6.8KB）小而独立，提供 4 个**与具体变换算法无关**的辅助函数：

| 函数 | 作用 |
|------|------|
| `fftfreq(n, d)` | 给出 `fft` 输出每个频率箱对应的频率值（含正负频率） |
| `rfftfreq(n, d)` | 给出 `rfft` 输出对应的频率值（仅非负频率） |
| `fftshift(x, axes)` | 把零频从数组开头搬到中心（便于画频谱图） |
| `ifftshift(x, axes)` | `fftshift` 的逆操作 |

它们**完全不调用 C++ 扩展**，只用 NumPy 的 `arange`、`roll` 等基本操作实现，是纯 Python。这也是为什么它们放在单独一个文件里——逻辑上和变换主流程解耦。

#### 4.3.2 核心流程

- **`fftfreq`/`rfftfreq`**：根据长度 `n` 和采样间距 `d`，用整数切分生成正/负频率数组，再除以 `n*d` 换算成物理频率。
- **`fftshift`/`ifftshift`**：对指定轴做 `roll`（循环平移），平移量约为 `dim // 2`，从而把零频项搬到中间。`ifftshift` 的平移量与 `fftshift` 互补，保证两者互逆。

> 具体公式与奇偶长度差异留到 u2-l2 / u2-l3 讲义；本讲只关注「文件职责」。

#### 4.3.3 源码精读

[_helper.py:5-6](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.py#L5-L6) —— 注意它的依赖只有 `numpy._core` 的基本操作（`arange`/`asarray`/`empty`/`integer`/`roll`）和 `overrides`（用于命名空间归属），**没有任何对 `_pocketfft` 或 C++ 扩展的引用**，体现了它的独立性。

```python
from numpy._core import arange, asarray, empty, integer, roll
from numpy._core.overrides import array_function_dispatch, set_module
```

[_helper.py:10](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.py#L10) —— `_helper.py` 自己的 `__all__` 只有这 4 个名字，正是 `__init__.py` 里 `+= _helper.__all__` 的来源（14 + 4 = 18）。

```python
__all__ = ['fftshift', 'ifftshift', 'fftfreq', 'rfftfreq']
```

#### 4.3.4 代码实践

1. **目标**：确认这 4 个 helper 都来自 `_helper.py`，且与 14 个变换不在同一文件。
2. **操作**：
   ```python
   import numpy.fft as F
   for n in ['fftfreq','rfftfreq','fftshift','ifftshift']:
       print(n, getattr(F, n).__code__.co_filename.split('/')[-1])
   ```
3. **观察现象**：4 行都打印 `_helper.py`，而不是 `_pocketfft.py`。
4. **预期结果**：全部显示 `_helper.py`。
5. 若运行环境无 NumPy：**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `fftfreq`/`fftshift` 这类函数不和 14 个变换一起放在 `_pocketfft.py` 里？

**答案**：它们不依赖任何 FFT 算法实现，只是基于数组形状/索引的纯数组操作；单独成文件便于维护，也让「变换算法」与「频率/平移工具」的边界更清晰。

**练习 2**：`_helper.py` 是否会触发加载 C++ 扩展 `_pocketfft_umath`？

**答案**：不会。它的 import 只引用 `numpy._core`，与 `_pocketfft.py` 完全解耦；理论上即使 C++ 扩展不存在，`_helper` 模块自身仍可被加载（只是 `import numpy.fft` 整体仍会因 `_pocketfft` 失败而失败）。

---

### 4.4 `_pocketfft_umath.cpp` 与 `pocketfft` 子模块：C++ 后端

#### 4.4.1 概念说明

真正算 FFT 的代码在两个地方：

- **`_pocketfft_umath.cpp`**（15.7KB）：NumPy 自己写的 C++ 胶水层。它把 FFT 包装成 NumPy 的 **gufunc（广义 ufunc）**，负责处理数组遍历、缓冲、类型分发、异常转换；然后调用底层算法。
- **`pocketfft/`（git 子模块）**：第三方头文件库（来自 [mreineck/pocketfft](https://github.com/mreineck/pocketfft)），提供真正的高性能 FFT 算法实现，核心头文件是 `pocketfft_hdronly.h`（header-only，只含头文件、无需单独编译）。

两者关系：`_pocketfft_umath.cpp` `#include` 了子模块的头文件，于是算法代码在编译期被「贴」进扩展。**子模块一旦缺失，`#include` 找不到头文件，编译直接失败。**

#### 4.4.2 核心流程

构建期（由 `meson.build` 驱动）：

1. Meson 检查 `pocketfft/README.md` 是否存在（即子模块是否已 checkout）。
2. 把 `_pocketfft_umath.cpp` 编译成扩展模块 `_pocketfft_umath`（产物 `.so`），安装到 `numpy/fft/` 下。
3. 编译时，C++ 的 `#include "pocketfft/pocketfft_hdronly.h"` 把算法代码展开进扩展。

运行期：

4. `_pocketfft.py` 用 `from . import _pocketfft_umath as pfu` 加载这个 `.so`。
5. `pfu.fft`/`pfu.ifft`/`pfu.rfft_*`/`pfu.irfft` 等就是注册好的 gufunc，可像普通 NumPy ufunc 一样被调用。

#### 4.4.3 源码精读

[_pocketfft_umath.cpp:1-11](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft_umath.cpp#L1-L11) —— 文件头注明这是 pocketfft 的一部分、3-clause BSD 许可，作者是 Martin Reinecke（也正是子模块的维护者），点明了本文件与子模块的同源关系。

[_pocketfft_umath.cpp:24](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft_umath.cpp#L24) —— 这一行是 C++ 胶水层与第三方算法库的「缝合点」：`#include "pocketfft/pocketfft_hdronly.h"` 把子模块的头文件拉进来。注意前一行 `#define POCKETFFT_NO_MULTITHREADING` 关掉了算法库自带的内嵌多线程（NumPy 改用自己的线程调度）。

```cpp
#define POCKETFFT_NO_MULTITHREADING
#include "pocketfft/pocketfft_hdronly.h"
```

子模块本身在 `.gitmodules` 中登记：

[.gitmodules（pocketfft 段）](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/.gitmodules) —— `numpy/fft/pocketfft` 指向 `https://github.com/mreineck/pocketfft`。在你的本地 checkout 里这个目录可能是**空的**，因为子模块默认不会自动拉取，需要手动 `git submodule update --init`。

#### 4.4.4 代码实践

1. **目标**：确认 `_pocketfft_umath` 是 C++ 扩展而非 Python，并理解它对子模块的依赖。
2. **操作（源码阅读型）**：
   - 打开 `_pocketfft_umath.cpp`，找到第 24 行的 `#include`，确认它引用了 `pocketfft/pocketfft_hdronly.h`。
   - 在仓库根目录查看 `.gitmodules`，找到 `[submodule "numpy/fft/pocketfft"]` 段及其 `url`。
   - 用 `ls numpy/fft/pocketfft/` 观察本地该目录是否为空（取决于子模块是否已初始化）。
3. **观察现象**：若目录为空，说明子模块未 checkout；这正是 `meson.build` 第 6–8 行要专门检查的原因。
4. **预期结果**：能说清「`.cpp` 通过 `#include` 依赖子模块头文件，故缺子模块则编译失败」这条因果链。
5. 若想真正编译：需执行 `git submodule update --init`（**待本地验证**）。

#### 4.4.5 小练习与答案

**练习 1**：`pocketfft` 子模块里的 `pocketfft_hdronly.h` 是 header-only 库。这跟「需要单独编译」的库有什么区别？对 `meson.build` 有什么影响？

**答案**：header-only 库只需被 `#include`，源码在编译期直接展开进使用者的目标文件，不需要在 `meson.build` 里把它列为独立编译单元。所以 `meson.build` 只编译 `_pocketfft_umath.cpp` 一个文件即可。

**练习 2**：为什么 `meson.build` 要在编译前专门检查 `pocketfft/README.md` 是否存在？

**答案**：因为子模块默认不会自动 clone，若缺失会让 C++ 编译在 `#include` 阶段报一堆难懂的「找不到头文件」错误；提前用一个清晰的 error 信息（提示运行 `git submodule update --init`）能给用户更好的引导。

---

### 4.5 `.pyi` 类型存根、`meson.build` 与 `tests/` 测试目录

#### 4.5.1 概念说明

剩下三类文件各自承担「非主流程」但必不可少的职责：

- **`.pyi` 类型存根**：本包有 3 个——`__init__.pyi`、`_pocketfft.pyi`（34KB，含大量 overload）、`_helper.pyi`。它们只声明函数签名（参数类型、返回类型、重载），**不含实现**，供 mypy / IDE 使用。运行时 Python 完全不看它们。
- **`meson.build`**：构建脚本（用 Meson DSL 写）。它决定哪些文件被**编译**（C++）、哪些被**直接安装**（`.py`/`.pyi` 源文件），以及测试文件如何打 tag 安装。
- **`tests/`**：测试目录，含 `test_pocketfft.py`（25KB，变换的正确性、norm、out、线程安全、回归）、`test_helper.py`（6KB，helper 的正确性）和一个空的 `__init__.py`。

#### 4.5.2 核心流程

构建安装流程（`meson.build`）：

1. 编译 `_pocketfft_umath.cpp` → 扩展模块，安装到 `numpy/fft/`。
2. 直接安装 6 个 Python/存根源文件（`__init__.py`/`__init__.pyi`/`_pocketfft.py`/`_pocketfft.pyi`/`_helper.py`/`_helper.pyi`）。
3. 以 `install_tag: 'tests'` 单独安装 3 个测试文件到 `numpy/fft/tests/`。

类型存根的作用流程：

4. 写代码时，IDE / mypy 读取 `.pyi`，据此给出参数提示和类型检查；运行时，Python 解释器只执行 `.py`。

测试流程：

5. `numpy.fft.test()`（由 `__init__.py` 挂载的 `PytestTester`）会收集并运行 `tests/` 下的两个测试文件。

#### 4.5.3 源码精读

`meson.build` 的三段式结构：

[meson.build:6-8](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/meson.build#L6-L8) —— 子模块存在性检查，缺失则给出清晰错误。

```meson
if not fs.exists('pocketfft/README.md')
  error('Missing the `pocketfft` git submodule! Run `git submodule update --init` to fix this.')
endif
```

[meson.build:10-16](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/meson.build#L10-L16) —— **编译**：只有 `_pocketfft_umath.cpp` 这一个文件被编译成扩展模块 `_pocketfft_umath`，安装到 `numpy/fft` 子目录。

```meson
py.extension_module('_pocketfft_umath',
  ['_pocketfft_umath.cpp'],
  ...
  subdir: 'numpy/fft',
)
```

[meson.build:18-28](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/meson.build#L18-L28) —— **直接安装**：6 个 `.py`/`.pyi` 源文件原样拷贝到安装目录，不经过编译。

[meson.build:30-38](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/meson.build#L30-L38) —— **测试安装**：测试文件带 `install_tag: 'tests'`，意味着打包时可以单独排除（发行包通常不带测试）。

类型存根示例——`__init__.pyi` 显式重新导出 18 个名字：

[__init__.pyi:1-38](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/__init__.pyi#L1-L38) —— 它把 4 个 helper 和 14 个变换的名字再列一遍并给出 `__all__`，让类型检查器知道公开 API 的精确形状。注意它和 `__init__.py` 内容**完全不同**：前者是「聚合 import + 运行逻辑」，后者是「纯签名清单」。

测试目录的两大文件（仅看类结构，细节留到 u5-l3）：

- [test_pocketfft.py](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/tests/test_pocketfft.py) —— 含 `TestFFT1D`、`TestFFTThreadSafe` 等，覆盖变换正确性、norm、out 参数、整数/布尔输入、回归。
- [test_helper.py](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/tests/test_helper.py) —— 含 `TestFFTShift`、`TestFFTFreq`、`TestRFFTFreq` 等，覆盖 4 个 helper。

#### 4.5.4 代码实践

1. **目标**：用 `numpy.fft.test()` 跑一遍本包测试，验证 `tests/` 真的被纳入测试体系。
2. **操作**：
   ```bash
   python -c "import numpy.fft; numpy.fft.test()"
   ```
   或只跑 helper 测试：`python -m pytest $(python -c "import numpy.fft.tests.test_helper as m; print(m.__file__)")`
3. **观察现象**：终端输出 pytest 的收集与通过情况，能看到 `test_pocketfft.py`、`test_helper.py` 里的用例。
4. **预期结果**：测试通过（或跳过个别平台相关用例），无 failure。
5. 若环境无 NumPy 源码树或未安装：**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`meson.build` 里 `install_tag: 'tests'` 的作用是什么？

**答案**：给被安装的文件打上 `tests` 标签。打包/分发时可以用 `--tags` 过滤，把测试文件排除在发行 wheel 之外，从而减小发行包体积，同时保留开发时安装测试的能力。

**练习 2**：`.pyi` 文件会参与运行时的函数执行吗？删掉它程序还能跑吗？

**答案**：不参与。运行时 Python 只执行 `.py`，`.pyi` 仅用于类型检查与 IDE 补全。删掉 `.pyi` 后程序行为不变，但会丢失类型提示（mypy 报错、IDE 补全变弱）。

**练习 3**：本包有几个 `.pyi`？为什么 `__init__.py` 内容已经很少了，还要配一个 `__init__.pyi`？

**答案**：3 个（`__init__.pyi`、`_pocketfft.pyi`、`_helper.pyi`）。即便 `__init__.py` 运行逻辑简单，类型检查器仍需要一个地方精确声明「`numpy.fft` 对外暴露哪些名字及其类型」（尤其是 18 个名字的 `__all__`），`__init__.pyi` 就承担这个声明职责。

---

## 5. 综合实践

**任务**：画出 `numpy/fft/` 的文件依赖关系图，并标注每个文件的三类属性。这是本讲的核心交付物，把 4.1–4.5 的知识点串成一张图。

### 步骤

1. 阅读以下三处源码，确认 import 关系：
   - [__init__.py:203-205](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/__init__.py#L203-L205)：`__init__.py` → `_helper`、`_pocketfft`。
   - [_pocketfft.py:48](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L48)：`_pocketfft.py` → `_pocketfft_umath`（C++ 扩展）。
   - [_pocketfft_umath.cpp:24](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft_umath.cpp#L24)：`_pocketfft_umath.cpp` → `pocketfft/pocketfft_hdronly.h`（子模块）。
2. 为每个文件标注三列：**①纯 Python / C++ 扩展 / 存根**；**②是否带 `.pyi`**；**③所属层（入口 / Python 逻辑 / C++ 后端 / 第三方 / 构建 / 测试）**。
3. 画出依赖箭头（A → B 表示「A 加载/包含 B」）。

### 参考答案（你的图应至少包含这些节点与边）

```
                        [入口层]
                __init__.py  (纯Py, 有.pyi)
                 │   │
        from .import *    from .import *
                 │   │
            ┌────▼─── ▼────────────────────┐
            │        [Python 逻辑层]        │
   _helper.py (纯Py,有.pyi)   _pocketfft.py (纯Py,有.pyi)
            │                           │  from . import _pocketfft_umath as pfu
            │ (不依赖C++)                ▼
            │                   [C++ 后端层]
            │         _pocketfft_umath.cpp (C++扩展,无.pyi) --编译--> _pocketfft_umath.so
            │                           │  #include "pocketfft/pocketfft_hdronly.h"
            │                           ▼
            │                   [第三方库]
            │              pocketfft/ (git子模块, mreineck/pocketfft)
            │
   ┌────────┴───────── 横切关注点 ────────────────┐
   │  meson.build：编译 .cpp / 安装 .py+.pyi / 安装 tests │
   │  tests/：test_pocketfft.py + test_helper.py（被 numpy.fft.test() 调用）│
   └──────────────────────────────────────────────┘
```

**关键检查点**（自测）：

- `__init__.py` 导入了 2 个 Python 模块（`_helper`、`_pocketfft`），**没有**直接导入 C++ 扩展。
- 唯一接触 C++ 扩展的是 `_pocketfft.py`（通过 `pfu`）。
- `_helper.py` 与 C++ 层完全无依赖。
- C++ 扩展没有 `.pyi`（它是编译产物，类型由 `_pocketfft.pyi` 在 Python 侧描述）。
- 子模块 `pocketfft/` 只被 C++ 头文件包含，不被任何 Python 文件直接引用。

## 6. 本讲小结

- `numpy/fft/` 是一个清晰的三层结构：**入口层 `__init__.py` → Python 逻辑层 `_pocketfft.py`/`_helper.py` → C++ 后端 `_pocketfft_umath.cpp`**。
- `__init__.py` 用 `from ._pocketfft import *` 和 `from ._helper import *` 聚合导出，`__all__ = _pocketfft.__all__.copy()` 再 `+= _helper.__all__` 拼成 18 个公开名字（14 变换 + 4 helper）。
- `_pocketfft.py` 是最大文件，定义全部 14 个变换，但**自己不算 FFT**，只做参数处理与编排，真正计算委托给 C++ 扩展 `pfu`。
- `_helper.py` 提供 4 个纯 Python helper（`fftfreq`/`rfftfreq`/`fftshift`/`ifftshift`），与算法解耦、不依赖 C++ 层。
- 真正的 FFT 算法来自 `pocketfft/` 这个 git 子模块（mreineck/pocketfft），由 `_pocketfft_umath.cpp` 以 `#include "pocketfft/pocketfft_hdronly.h"` 方式缝合，`meson.build` 会检查子模块存在性。
- `.pyi` 类型存根（3 个）只服务类型检查/IDE，运行时不参与；`meson.build` 区分「编译」「直接安装」「带 tests 标签安装」三类文件。

## 7. 下一步学习建议

本讲已经把「文件地图」铺好。下一步建议：

- **若想先把数学基础打牢**：进入 **u2-l1（DFT 数学定义与实现约定）**，结合 `__init__.py` 顶部那段长 docstring，弄清 DFT/IDFT 公式、standard 频率排列、Hermitian 对称等约定。
- **若想直接深入变换主流程**：进入 **u3-l1（fft/ifft 与 _raw_fft 主流程）**，把本讲 4.2 里那个 `_raw_fft` 的分支选择逐行读懂。
- **配套阅读建议**：继续读 [`__init__.py` 的 docstring 段](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/__init__.py#L79-L170)（Implementation details / Real and Hermitian transforms / Higher dimensions），它把本包的所有数学约定一次讲清，是后续多篇讲义的共同依据。
