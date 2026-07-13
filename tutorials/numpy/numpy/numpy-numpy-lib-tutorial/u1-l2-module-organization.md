# 模块组织与导入机制：再导出层与 dispatcher 模式

> 本讲是「numpy.lib 学习手册」的第二篇，承接 [u1-l1](u1-l1-overview.md)。
> 上一篇我们确立了三件事：`numpy.lib` 是「杂项函数库」、`__init__.py` 是总入口、`__all__` 是公开名单。
> 本讲要回答一个更具体的问题：**当你写下 `np.pad(...)` 时，这个函数到底是从哪个文件里冒出来的？为什么它写在一个私有的 `_impl` 文件里，却能被 `np.` 直接调用？**

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 解释 numpy.lib 的**两层结构**：「薄再导出模块」（如 `npyio.py`、`format.py`、`stride_tricks.py`）和「私有实现模块」（`_xxx_impl.py`）各自的角色，以及它们如何配合。
2. 读懂贯穿整个 numpy.lib 的**「dispatcher + impl 双函数」写法**：几乎所有公开函数都用 `@array_function_dispatch(_xxx_dispatcher)` 装饰，背后是 NEP-18 的 `__array_function__` 协议。
3. 理解 `__init__.py` 里的**模块级 `__getattr__`** 如何把「访问已移除别名」这种模糊失败，变成带迁移指引的清晰报错。
4. 拿任意一个公开函数（以 `np.pad` 为例），**画出从 `np.pad` 到真正实现函数、再到 `_dispatcher` 的完整调用链**。

本讲只读、不改源码。所有命令都是观察性的。

---

## 2. 前置知识

### 2.1 私有名与下划线约定

Python 的约定：以单下划线 `_` 开头的名字（如 `_arraypad_impl`、`_pad_dispatcher`）是**私有的**，属于内部实现，外部不应依赖，随时可能改。numpy.lib 把绝大多数**真正干活**的代码放在这种 `_xxx_impl.py` 文件里。

### 2.2 装饰器（decorator）一句话回顾

装饰器是一种「包在函数外面的包装」。写法如下：

```python
@some_decorator
def my_func(...):
    ...
```

它等价于 `my_func = some_decorator(my_func)`。也就是说，调用 `my_func(...)` 时，真正执行的是装饰器**返回的那个包装对象**，而不是你写的原始函数体。本讲会看到 `@array_function_dispatch(...)` 这个装饰器把原始 `pad` 函数包成了一个「带派发能力」的对象。

### 2.3 什么是 NEP-18 与 `__array_function__`

NEP-18 是一份 NumPy 增强提案，它定义了一个协议：**允许非 ndarray 的数组类型（比如 Dask、CuPy 的数组、或你自己的 ndarray 子类）「接管」numpy 函数的执行**。

机制很直白：如果一个对象定义了 `__array_function__` 方法，那么当它作为参数被传进某个 numpy 函数时，numpy 会优先问它「这件事你自己来，还是交给我（NumPy）默认实现？」。

`array_function_dispatch` 就是 numpy 为每个公开函数接上这套协议的**统一胶水**。你暂时不用理解协议的全部细节，只要记住：**它让「找到参数里的特殊数组」和「执行真正的计算」这两件事分开**。

> 一句话总结本节：私有 `_impl` 藏实现，装饰器包函数，`__array_function__` 让别的数组类型能插手。本讲就是把这三件事串起来。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 | 本讲用它讲什么 |
| --- | --- | --- |
| [`numpy/lib/npyio.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/npyio.py) | 薄再导出模块，只有 1 行 | 最典型的「再导出层」长什么样 |
| [`numpy/lib/format.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/format.py) | 薄再导出模块，24 行 | 批量再导出多个名字的写法 |
| [`numpy/lib/stride_tricks.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/stride_tricks.py) | 薄再导出模块，1 行 | 再导出层与 `__init__.py` 的关系 |
| [`numpy/lib/__init__.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/__init__.py) | 子包总入口 | 把所有模块（薄模块 + `_impl`）统一导入；`__getattr__` 兜底 |
| [`numpy/lib/_arraypad_impl.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraypad_impl.py) | `pad` 的真正实现 | dispatcher + impl 双函数写法的实例 |
| [`numpy/_core/overrides.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_core/overrides.py) | `array_function_dispatch` 的定义 | 装饰器到底做了什么 |
| [`numpy/__init__.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/__init__.py) | 顶层 numpy 总入口 | `_impl` 里的函数如何被「搬」到 `np.` 命名空间 |

> 注意：后三个文件不在 `numpy/lib/` 目录内，它们属于更外层或 `_core`，但是理解 lib 的导入链路绕不开它们。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **再导出模块** —— 薄模块与 `_impl` 的分离
2. **array_function_dispatch** —— dispatcher + impl 双函数写法
3. **__getattr__** —— 对已移除别名的兜底报错

---

### 4.1 再导出模块：薄模块与 `_impl` 的分离

#### 4.1.1 概念说明

numpy.lib 里有一个反复出现的设计模式：**「实现」和「对外名字」分开存放**。

- 真正的函数体写在私有的 `_xxx_impl.py` 里（例如 `_npyio_impl.py`、`_format_impl.py`、`_stride_tricks_impl.py`）。
- 对外暴露一个**几乎没有代码的「薄模块」**（例如 `npyio.py`、`format.py`、`stride_tricks.py`），它只做一件事：从对应的 `_impl` 里把名字「搬」过来。

为什么要这么做？因为这样可以让**实现自由演化**（甚至重构成 C），而**对外的导入路径保持稳定**。用户 `import numpy.lib.npyio` 拿到的接口是固定的，至于背后是纯 Python 还是 C，用户无感。

#### 4.1.2 核心流程

一个名字从被定义到能被导入，经过这样的链路：

```text
_xxx_impl.py 里定义函数  ──(薄模块再导出)──▶  xxx.py 暴露名字
        │
        └──(__init__.py 把 xxx / _xxx_impl 一起导入)──▶ numpy.lib.xxx
                                                        │
                                                        └──(顶层 numpy/__init__.py 再搬一次)──▶ np.xxx
```

这里有**两种**再导出方式，要区分清楚：

- **方式 A：有薄模块**。比如 `stride_tricks.py` 把 `_stride_tricks_impl.py` 的名字搬出来。用户既能 `numpy.lib.stride_tricks.as_strided`，也被顶层搬到 `numpy.lib.as_strided` 之外（注：`as_strided` 也直接进 `_stride_tricks_impl.__all__` 被顶层收集，见 4.1.3）。
- **方式 B：没有薄模块，函数直接住在 `_impl` 里**。比如 `pad` 根本没有一个 `arraypad.py` 薄模块，它就直接写在 `_arraypad_impl.py` 里，由顶层 `numpy/__init__.py` 直接 `from .lib._arraypad_impl import pad` 搬到 `np.pad`。

#### 4.1.3 源码精读

先看三个「薄模块」有多薄。[`numpy/lib/npyio.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/npyio.py#L1-L1) 整个文件只有一行：

```python
from ._npyio_impl import DataSource, NpzFile, __doc__  # noqa: F401
```

它把 `DataSource`、`NpzFile` 和文档字符串 `__doc__` 从私有的 `_npyio_impl` 搬过来，仅此而已。`# noqa: F401` 是告诉代码检查工具「这些名字看起来没用，但我是故意再导出的，别报警」。

[`numpy/lib/stride_tricks.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/stride_tricks.py#L1-L1) 同样只有一行：

```python
from ._stride_tricks_impl import __doc__, as_strided, sliding_window_view  # noqa: F401
```

稍复杂一点的是 [`numpy/lib/format.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/format.py#L1-L24)，它一次再导出二十来个名字（`magic`、`read_array`、`write_array`、`open_memmap` 等），但本质和上面两个完全一样：**自己不写任何实现**。

那么这些薄模块和 `_impl` 是怎么被装进 `numpy.lib` 命名空间的？看 [`numpy/lib/__init__.py` 的导入块](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/__init__.py#L16-L42)：

```python
# Private submodules
from . import (
    _arraypad_impl,
    _arraysetops_impl,
    ...
    _npyio_impl,
    ...
    format,
    ...
    npyio,
    scimath,
    stride_tricks,
)
```

注意：薄模块（`format`、`npyio`、`stride_tricks`、`scimath`）和私有 `_impl` 模块**混在同一个 `from . import (...)` 里一起被导入**。这一步之后，`numpy.lib.npyio`、`numpy.lib.format` 就都成了可访问的子模块。

再看**方式 B**——没有薄模块的 `pad`。顶层 [`numpy/__init__.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/__init__.py#L455-L456) 直接从私有 `_impl` 里取名字：

```python
from .lib import scimath as emath
from .lib._arraypad_impl import pad
```

这说明 `np.pad` 的真正出处就是 `numpy/lib/_arraypad_impl.py`，中间**没有**任何薄模块。顶层还用一个 `__all__` 收集机制把各 `_impl` 的公开名字汇总，见 [`numpy/__init__.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/__init__.py#L686-L686)：

```python
set(lib._arraypad_impl.__all__) |
```

而 `_arraypad_impl.py` 自己声明的公开名单只有 `pad` 一个，见 [`_arraypad_impl.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraypad_impl.py#L12-L12)：

```python
__all__ = ['pad']
```

所以「`pad` 是公开函数」这件事，是由 `_impl` 自己的 `__all__` 声明、再被顶层收集生效的，而不依赖任何薄模块。

#### 4.1.4 代码实践

**实践目标**：亲手验证「薄模块只是搬运工，实现藏在 `_impl` 里」。

**操作步骤**：

1. 在装有 numpy 的环境里启动 Python（版本应为本仓库对应的 NumPy 2.x）。
2. 运行下面这段**示例代码**：

```python
import numpy as np

# 1) 薄模块 npyio 里的 NpzFile，和私有 _npyio_impl 里的 NpzFile，是同一个对象吗？
from numpy.lib import npyio
print(npyio.NpzFile is np.lib._npyio_impl.NpzFile)   # 预期 True

# 2) np.pad 到底来自哪个文件？
print(np.pad.__module__)                              # 预期 numpy.lib._arraypad_impl
print(np.pad.__name__)                                # 预期 pad

# 3) 薄模块自己有没有实现？看它的源码文件路径
import numpy.lib.format as fmt
print(fmt.__file__)                                   # .../numpy/lib/format.py，且文件极小
```

**需要观察的现象**：第 1 步应打印 `True`，证明薄模块只是把同一个对象搬过来；第 2 步的 `__module__` 指向 `_arraypad_impl`，证明实现确实在私有模块里。

**预期结果**：三行分别打印 `True`、`numpy.lib._arraypad_impl`、`pad`、以及 `format.py` 的绝对路径。

> 待本地验证：不同 numpy 小版本下 `np.pad.__module__` 的字符串可能略有差异，但一定以 `_arraypad_impl` 结尾。

#### 4.1.5 小练习与答案

**练习 1**：`numpy.lib.format` 里再导出的 `magic` 函数，真正定义在哪个文件？  
**答案**：定义在 `numpy/lib/_format_impl.py`。`format.py` 只是用 `from ._format_impl import magic` 把它搬过来。

**练习 2**：为什么 `np.pad` 没有 `numpy/lib/arraypad.py` 这样的薄模块，却仍能被 `np.pad` 访问？  
**答案**：因为顶层 `numpy/__init__.py` 用 `from .lib._arraypad_impl import pad` 直接从私有 `_impl` 取名，并且通过 `set(lib._arraypad_impl.__all__)` 把 `pad` 纳入顶层公开命名空间。薄模块不是必需的，`__all__` 收集 + 顶层 `from ... import` 才是关键。

---

### 4.2 array_function_dispatch：dispatcher + impl 双函数写法

#### 4.2.1 概念说明

打开任意一个 `_impl` 文件，你会看到几乎每个公开函数都长成**「一对函数」**的样子：一个叫 `_xxx_dispatcher` 的辅助函数，加上被 `@array_function_dispatch(_xxx_dispatcher)` 装饰的真正实现函数。

`pad` 就是典型。它由两部分组成：

- **dispatcher**：`_pad_dispatcher(array, pad_width, mode=None, **kwargs)`，职责只有一个——**返回参与运算的数组参数**（这里是 `(array,)`），供 NEP-18 协议检查这些参数里有没有「想接管」的特殊数组。
- **implementation**：被装饰的 `pad(array, pad_width, mode='constant', **kwargs)`，是真正干活的实现。

为什么要拆成两个？因为 NEP-18 需要知道「该把这次调用交给谁」。dispatcher 就是回答「这次调用里，哪些参数是数组」的。只要知道了这一点，numpy 就能去问这些数组：你们当中有没有谁想用自己的 `__array_function__` 来处理？没有的话，就回退到默认实现 `pad`。

#### 4.2.2 核心流程

一次 `np.pad(arr, 2)` 的内部流转如下（伪代码）：

```text
np.pad(arr, 2)
   │
   ▼
_ArrayFunctionDispatcher 实例（装饰器返回的包装对象）
   │  1) 先调 _pad_dispatcher(arr, 2) 拿到「相关参数」= (arr,)
   │  2) 在这些参数里查找定义了 __array_function__ 的对象
   ▼
   ├─ 找到了特殊数组？ ──▶ 把调用转发给它的 __array_function__
   │
   └─ 没找到？ ──▶ 调用真正的实现函数 pad(arr, 2)（即 _impl 里写的那段）
```

关键点：**dispatcher 和实现函数的形参签名必须一致**（除了默认值），否则装饰器会在导入时直接报错。这是一种「编译期」的安全检查，防止两边参数对不上。

#### 4.2.3 源码精读

先看 `_arraypad_impl.py` 顶部如何把装饰器引进来，见 [`_arraypad_impl.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraypad_impl.py#L9-L9)：

```python
from numpy._core.overrides import array_function_dispatch
```

注意它来自 `numpy._core.overrides`，**不在 lib 里**——lib 只是「使用方」，定义在更底层的 `_core`。

接着看 dispatcher 与实现的「成对」写法，见 [`_arraypad_impl.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraypad_impl.py#L538-L547)：

```python
def _pad_dispatcher(array, pad_width, mode=None, **kwargs):
    return (array,)


@array_function_dispatch(_pad_dispatcher, module='numpy')
def pad(array, pad_width, mode='constant', **kwargs):
    """Pad an array. ..."""
```

两个要点：

1. `_pad_dispatcher` 的形参 `array, pad_width, mode=None, **kwargs` 与 `pad` 的形参 `array, pad_width, mode='constant', **kwargs` **一一对应**，只是默认值不同（dispatcher 用 `None`）。这正满足「签名一致、默认值用 None」的规则。
2. `@array_function_dispatch(_pad_dispatcher, module='numpy')` 把 `_pad_dispatcher` 作为 dispatcher 传进去，并指定 `module='numpy'`——这让包装后的函数对外显示 `__module__ == 'numpy'`，看起来就像顶层原生函数。

装饰器本身定义在 [`numpy/_core/overrides.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_core/overrides.py#L108-L109)：

```python
def array_function_dispatch(dispatcher=None, module=None, verify=True,
                            docs_from_dispatcher=False):
```

它的核心在返回的内部 `decorator` 里，见 [`overrides.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_core/overrides.py#L145-L175)。关键三行：

```python
if verify:
    verify_matching_signatures(implementation, dispatcher)   # 签名校验
...
public_api = _ArrayFunctionDispatcher(dispatcher, implementation)  # 包装成带派发能力的对象
funct.update_wrapper(public_api, implementation)              # 复制 __doc__/__name__
if module is not None:
    public_api.__module__ = module                            # 设成 'numpy'
```

真正「带派发能力」的对象是 `_ArrayFunctionDispatcher`（一个 C 实现的类）。它的文档字符串在 [`overrides.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_core/overrides.py#L35-L61) 里被描述为「Class to wrap functions with checks for `__array_function__` overrides」。也就是说，你调用 `np.pad(...)` 时，实际被调用的是这个 C 对象的 `__call__`，它内部决定是转发给特殊数组，还是回落到原始 `pad` 实现。

签名校验的逻辑在 [`verify_matching_signatures`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_core/overrides.py#L86-L100)：它比对 `implementation` 与 `dispatcher` 的参数名、可变参数、关键字参数、默认值个数是否吻合，不吻合就 `raise RuntimeError`。这就是为什么 dispatcher 的形参必须和实现函数严格对齐。

#### 4.2.4 代码实践

**实践目标**：亲手证明「`np.pad` 是一个被装饰器包装过的派发对象，原始实现藏在 `_implementation` 里」。

**操作步骤**：运行下面这段**示例代码**：

```python
import numpy as np
from numpy._core.overrides import _ArrayFunctionDispatcher

# 1) np.pad 到底是什么类型的对象？
print(type(np.pad))
# 预期：<class 'numpy._core._multiarray_umath._ArrayFunctionDispatcher'>

# 2) 装饰前的「原始实现」还能取到吗？
impl = np.pad._implementation
print(impl.__name__, impl.__module__)
# 预期：pad numpy.lib._arraypad_impl

# 3) dispatcher 在哪？_impl 模块里直接能拿到
from numpy.lib import _arraypad_impl
print(_arraypad_impl._pad_dispatcher(np.arange(3), 2))
# 预期：(array([0, 1, 2]),) —— 返回「相关数组参数」
```

**需要观察的现象**：第 1 步证明 `np.pad` 不是普通函数，而是 `_ArrayFunctionDispatcher`；第 2 步证明原始实现可经 `_implementation` 取回；第 3 步证明 dispatcher 只是把数组参数包成元组返回。

**预期结果**：如上注释所述。第 3 步返回的元组里装的就是你传入的那个数组（或其 array 化结果）。

> 待本地验证：`_implementation` 与 `_ArrayFunctionDispatcher` 属于内部 API，不同小版本可能改名；若取不到，说明你的 numpy 版本与本文档 HEAD 不一致，回到源码核对即可。

#### 4.2.5 小练习与答案

**练习 1**：dispatcher 函数 `_pad_dispatcher` 为什么只 `return (array,)`，而不返回 `pad_width`、`mode`？  
**答案**：dispatcher 的唯一职责是告诉协议「哪些参数是数组、需要被检查 `__array_function__`」。`pad_width` 和 `mode` 不是数组，不会有人想在它们上面接管运算，所以不必返回。

**练习 2**：如果把 `_pad_dispatcher` 的形参改成 `(array, pad_width)`（删掉 `mode` 和 `**kwargs`），导入时会发生什么？  
**答案**：会抛 `RuntimeError: implementation and dispatcher for ... have different function signatures`。因为 `verify=True`（默认）时，`verify_matching_signatures` 会比对两者签名，发现不一致就报错。这正是双函数写法的安全保障。

---

### 4.3 `__getattr__`：对已移除别名的兜底报错

#### 4.3.1 概念说明

NumPy 2.0 做了大量清理：很多过去能从 `numpy.lib` 访问的子模块（如 `numpy.lib.arraypad`、`numpy.lib.utils`）被**私有化**或**移除**了。如果代码还在用旧名字，最朴素的失败是 `AttributeError: module 'numpy.lib' has no attribute 'arraypad'`——这句话对用户毫无帮助，他不知道该改成什么。

`numpy.lib` 用 PEP 562 的**模块级 `__getattr__`** 把这件事变得友好：当访问一个**不存在**的属性时，Python 先调用 `__getattr__(名字)`，由它返回一个**带迁移指引**的错误信息。

#### 4.3.2 核心流程

```text
用户写 numpy.lib.emath
   │
   ▼ emath 不在 __all__、也不是已导入的子模块
lib.__getattr__('emath')
   │
   ├─ 名字 == 'emath'                  ──▶ 抛 AttributeError：「已移除，请改用 numpy.emath」
   ├─ 名字 in 私有化别名集合             ──▶ 抛 AttributeError：「已私有化，去主命名空间或查迁移指南」
   ├─ 名字 == 'arrayterator'           ──▶ 抛 AttributeError：「模块私有，请用 numpy.lib.Arrayterator」
   └─ 其它                              ──▶ 抛默认 AttributeError：「无此属性」
```

注意一个关键事实：`numpy.emath` 并没有消失，它现在是 `numpy.lib.scimath` 的别名。顶层 [`numpy/__init__.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/__init__.py#L455-L455) 写着：

```python
from .lib import scimath as emath
```

也就是说，旧路径 `numpy.lib.emath` 被砍了，但新路径 `numpy.emath`（=`numpy.lib.scimath`）还在。`__getattr__` 的报错正是要把用户从这个旧路径引导到新路径。

#### 4.3.3 源码精读

整个兜底逻辑在 [`numpy/lib/__init__.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/__init__.py#L61-L90)：

```python
def __getattr__(attr):
    # Warn for deprecated/removed aliases
    import warnings

    if attr == "emath":
        raise AttributeError(
            "numpy.lib.emath was an alias for emath module that was removed "
            "in NumPy 2.0. Replace usages of numpy.lib.emath with "
            "numpy.emath.",
            name=None
        )
    elif attr in (
        "histograms", "type_check", "nanfunctions", "function_base",
        "arraypad", "arraysetops", "ufunclike", "utils", "twodim_base",
        "shape_base", "polynomial", "index_tricks",
    ):
        raise AttributeError(
            f"numpy.lib.{attr} is now private. If you are using a public "
            "function, it should be available in the main numpy namespace, "
            "otherwise check the NumPy 2.0 migration guide.",
            name=None
        )
    elif attr == "arrayterator":
        raise AttributeError(
            "numpy.lib.arrayterator submodule is now private. To access "
            "Arrayterator class use numpy.lib.Arrayterator.",
            name=None
        )
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {attr!r}")
```

看这个集合：`"arraypad"`、`"utils"`、`"shape_base"`、`"index_tricks"` 等，正好对应 4.1 里那些被私有化的 `_impl` 模块（`_arraypad_impl`、`_utils_impl`、`_shape_base_impl`、`_index_tricks_impl`）的**旧公开名**。报错文案分三类，正好对应三种迁移策略：

- **emath**：改名了，去用 `numpy.emath`；
- **被私有化的子模块**：实现还在，但走主命名空间（`np.xxx`）或查迁移指南；
- **arrayterator**：子模块私有化了，但类还在，用 `numpy.lib.Arrayterator`。

注意每个 `AttributeError` 都传了 `name=None`。这是为了**抑制** Python 默认在报错信息里附带的「During handling of the above exception...」上下文——让用户只看到这条干净、有指导的提示。

#### 4.3.4 代码实践

**实践目标**：触发这些兜底报错，观察它们给出的迁移指引。

**操作步骤**：运行下面这段**示例代码**（预期会抛错，逐个 `try/except` 捕获并打印）：

```python
import numpy.lib

for name in ["emath", "arraypad", "utils", "arrayterator", "totally_fake"]:
    try:
        getattr(numpy.lib, name)
    except AttributeError as e:
        print(f"--- 访问 numpy.lib.{name} ---")
        print(e)
        print()
```

**需要观察的现象**：每个名字都会抛 `AttributeError`，但文案不同——`emath` 告诉你改用 `numpy.emath`；`arraypad`/`utils` 告诉你已私有化、去主命名空间或查迁移指南；`arrayterator` 告诉你用 `numpy.lib.Arrayterator`；`totally_fake` 给出默认的「无此属性」。

**预期结果**：打印出 5 段各不相同的错误信息，且都带有明确的「下一步该用什么」指引，而不是干巴巴的 `has no attribute`。

> 待本地验证：具体文案以你本地 numpy 版本为准；本仓库 HEAD（`b21650c4f6`）下文案如上。

#### 4.3.5 小练习与答案

**练习 1**：既然 `numpy.lib.emath` 被移除，那 `numpy.emath` 还能用吗？它背后是什么？  
**答案**：能用。顶层 `numpy/__init__.py` 里有 `from .lib import scimath as emath`，所以 `numpy.emath` 实际就是 `numpy.lib.scimath` 模块。砍掉的是 `numpy.lib.emath` 这个旧别名。

**练习 2**：为什么 `__getattr__` 里每个 `raise AttributeError(...)` 都要带 `name=None`？  
**答案**：为了抑制 Python 默认附加的异常链上下文（「During handling of the above exception, another exception occurred」），让用户只看到一条干净的、带迁移指引的提示。这是 PEP 562 场景下常见的「定向报错」技巧。

**练习 3**：访问 `numpy.lib.pad` 会触发上面的 `__getattr__` 吗？  
**答案**：不会。`pad` 不在那些已移除/私有化的别名集合里，`__getattr__` 只在属性**找不到**时才被调用。`pad` 走的是默认的 `else` 分支也到不了——因为 `pad` 根本不通过 `numpy.lib.pad` 暴露（它直接住在 `_arraypad_impl` 里，由顶层搬到 `np.pad`）。所以 `numpy.lib.pad` 会落到 `else`，报 `module 'numpy.lib' has no attribute 'pad'`。

---

## 5. 综合实践：画出 `np.pad` 的完整调用链

把本讲三个最小模块串起来，完成一个**源码阅读型实践**：追踪 `np.pad` 从被调用到真正执行的完整路径。

### 5.1 实践目标

用一张图和一小段验证代码，说清楚：

1. `np.pad` 这个名字来自哪里（再导出层）？
2. 它为什么是个「派发对象」而不是普通函数（dispatcher 模式）？
3. 真正干活的实现函数和 dispatcher 分别在哪？

### 5.2 操作步骤

**第一步：画调用链。** 根据本讲源码，`np.pad` 的链路如下（请自己对照源码核对每一跳）：

```text
np.pad                                       （顶层 numpy/__init__.py:456 导入）
  └─ 真身：numpy.lib._arraypad_impl.pad      （被装饰前的实现）
       ├─ 被 @array_function_dispatch(_pad_dispatcher, module='numpy') 包装
       │     └─ 包装对象类型：_ArrayFunctionDispatcher   （overrides.py 定义）
       └─ dispatcher：numpy.lib._arraypad_impl._pad_dispatcher  （返回 (array,)）
```

**第二步：用代码验证每一跳。** 运行这段**示例代码**：

```python
import numpy as np
from numpy.lib import _arraypad_impl

# 跳 1：np.pad 的来源
print("np.pad 的 module：", np.pad.__module__)
# 预期：numpy.lib._arraypad_impl   （注意：虽声明 module='numpy'，但 __module__ 反映真实出处）

# 跳 2：它是不是派发对象？
from numpy._core.overrides import _ArrayFunctionDispatcher
print("是派发对象？", isinstance(np.pad, _ArrayFunctionDispatcher))
# 预期：True

# 跳 3：原始实现与 dispatcher 都能从 _impl 取到
print("原始实现：", np.pad._implementation.__name__)
print("dispatcher：", _arraypad_impl._pad_dispatcher.__name__)
print("dispatcher 返回：", _arraypad_impl._pad_dispatcher(np.array([1, 2, 3]), 1))
# 预期：pad / _pad_dispatcher / (array([1, 2, 3]),)
```

### 5.3 需要观察的现象

- `np.pad.__module__` 指向 `numpy.lib._arraypad_impl`，证明实现确实在那个私有 `_impl` 里。
- `isinstance(..., _ArrayFunctionDispatcher)` 为 `True`，证明它不是裸函数，而是被装饰器包装的派发对象。
- `_pad_dispatcher` 调用后返回一个只含输入数组的元组，正是 NEP-18 需要的「相关参数」。

### 5.4 预期结果

如「操作步骤」注释所述。如果某一步与预期不符，最可能的原因是 numpy 小版本差异（`_implementation`、`_ArrayFunctionDispatcher` 等内部名可能调整）。**待本地验证**后，回到对应源码文件核对即可。

### 5.5 进阶（可选）

把同样的追踪方法用到**另一个**公开函数上，例如 `np.histogram`（实现也在 lib，文件是 `_histograms_impl.py`）：

1. 找到它的 `_histogram_dispatcher`（或对应 dispatcher 名）；
2. 确认它也用 `@array_function_dispatch(...)` 装饰；
3. 画出与 `pad` 类似的调用链图。

如果新函数的链路与 `pad` 一致，就说明这套「再导出 + dispatcher」模式确实贯穿整个 numpy.lib。

---

## 6. 本讲小结

- **两层结构**：numpy.lib 把实现藏在私有的 `_xxx_impl.py` 里，对外用**薄再导出模块**（`npyio.py`、`format.py`、`stride_tricks.py`，通常只有一两行 `from ._xxx_impl import ...`）暴露稳定接口；部分函数（如 `pad`）连薄模块都没有，直接由顶层 `numpy/__init__.py` 从 `_impl` 取名并经 `__all__` 收集进 `np.`。
- **双函数写法**：几乎每个公开函数都是「`_xxx_dispatcher` + 被 `@array_function_dispatch(...)` 装饰的实现」一对。dispatcher 负责返回参与运算的数组参数，实现负责真正计算；两者形参签名必须一致，否则导入期就报错。
- **装饰器真身**：`array_function_dispatch` 定义在 `numpy/_core/overrides.py`，它把函数包成 `_ArrayFunctionDispatcher`（C 实现）对象，接上 NEP-18 的 `__array_function__` 协议，并可通过 `module='numpy'` 改写对外显示的模块名。
- **兜底报错**：`numpy/lib/__init__.py` 的模块级 `__getattr__`（PEP 562）拦截 NumPy 2.0 移除/私有化的旧别名（`emath`、`arraypad`、`utils`、`arrayterator` 等），给出带迁移指引的 `AttributeError`，把模糊失败变成有指导的失败。
- **一句话串起来**：`_impl` 出实现 → 薄模块/顶层再导出给名字 → `array_function_dispatch` 给派发能力 → `__getattr__` 给迁移指引。这四件事共同构成了 numpy.lib 的导入与分发骨架。

---

## 7. 下一步学习建议

- **按文件巡游**：挑一个 `_impl` 文件（推荐 `_arraypad_impl.py` 或 `_shape_base_impl.py`），用本讲的「找 dispatcher + 找实现」方法，把里面所有公开函数的调用链都画一遍，巩固双函数写法的直觉。
- **进入功能层**：本讲之后，建议进入 [u3（形状与维度操作）](#) 系列，开始读 `_shape_base_impl.py` 里 `expand_dims`、`stack`、`split` 等具体函数的实现。届时你会发现，它们无一例外都套着 `@array_function_dispatch(...)`，本讲建立的认知会直接复用。
- **深入协议（进阶）**：如果对「别的数组类型如何接管 numpy 函数」感兴趣，可阅读 `numpy/_core/overrides.py` 全文，以及 NEP-18 原文（`doc/neps/nep-0018-array-function-protocol.rst`）。那是 dispatcher 模式背后的完整设计。
