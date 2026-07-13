# Pickle 向后兼容：eager 绑定与 _reconstruct

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 pickle 在反序列化时如何用「模块路径 + 名字」定位到重建函数（`find_class` 契约）。
- 解释为什么 `_reconstruct`、`_ufunc_reconstruct`、所有 ufunc、以及 `_dtype_from_pep3118` 必须在垫片模块顶部**立即绑定**（eager binding），而不能走第 2 单元那种惰性 `__getattr__`。
- 识别 `numpy/core/_internal.py`、`numpy/core/multiarray.py`、`numpy/core/_multiarray_umath.py` 三个文件里的 eager 绑定虽然写法不同，但服务的是**同一个目的**：让旧 pickle 在不报警、不报错的前提下完成还原。
- 亲手构造一个「改了模块路径、旧 pickle 仍能还原」的最小兼容垫片。

## 2. 前置知识

本讲是第 3 单元（兼容性工程）的第一篇，默认你已经学完第 1、2 单元。这里回顾几条最关键的认识，本讲会直接拿来用：

- **`numpy.core` 是垫片**：NumPy 2.0 把真正的 `core` 改名成私有的 `numpy._core`，留在 `numpy/core/` 下的文件几乎都只是「转发器」（见 u1-l1、u1-l2）。
- **惰性转发的骨架**：纯转发垫片靠模块级 `__getattr__`（PEP 562），在找不到名字时才去 `import` 真模块、用 `getattr(真模块, name)` 取值，并调用 `_utils._raise_warning` 抛 `DeprecationWarning`（见 u2-l1、u2-l2）。
- **两种判存在写法**：`numeric.py` 用 sentinel 哨兵对象，`umath.py`/`records.py` 用 `None` 作默认值、`if ret is None` 判断（见 u2-l2）。本讲的三个特殊垫片用的就是 `None` 写法。
- **报警但不阻断**：`_raise_warning` 是「纯副作用」函数，报警之后仍然正常返回对象（见 u2-l3）。

本讲要回答的新问题是：**惰性 `__getattr__` 在 pickle 场景下会出什么问题？numpy 用什么手段绕开它？**

### 2.1 pickle 的「reduce」与「find_class」契约（先有直觉）

这是本讲的核心前置概念，先用大白话讲清楚。

pickle 序列化一个对象时，对自定义对象调用它的 `__reduce_ex__(protocol)`，得到一个元组，最常见的形状是：

```python
(callable, args, state)
```

反序列化时，pickle 做的事情等价于：

```python
obj = callable(*args)        # 用 callable 造一个空壳对象
obj.__setstate__(state)      # 再把状态填进去
```

问题来了：序列化时 pickle 拿到的是一个**函数对象** `callable`，它要把这个函数「写进字节流」；反序列化时它又得「从字节流里把函数找回来」。pickle 的做法是：不存函数本身，只存**两个字符串**——

- `module`：函数所在的模块路径（即 `callable.__module__`）；
- `name`：函数在模块里的名字（即 `callable.__qualname__` 或 `__name__`）。

反序列化时，pickle 用 `pickle.find_class(module, name)` 还原这个函数，它内部的逻辑近似于：

```python
def find_class(module, name):
    __import__(module)            # 导入模块
    mod = sys.modules[module]     # 拿到模块对象
    return getattr(mod, name)     # 在模块上取名字
```

> 关键结论：**只要「模块路径 + 名字」还能解析到同一个对象，旧 pickle 就能被还原**，哪怕这个对象现在已经搬家了——只要在旧地址留一个能找到它的垫片即可。这正是本讲要讲的全部内容。

## 3. 本讲源码地图

本讲聚焦 `numpy/core/` 下三个「特殊垫片」（带 eager 绑定的，见 u1-l2 的分类）。它们是全目录里唯三在模块顶部除了 `__getattr__` 之外还**主动绑死一些名字**的文件。

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [numpy/core/_internal.py:L1-L27](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_internal.py#L1-L27) | 转发到 `numpy._core._internal` 的垫片 | eager 绑定 `_reconstruct`（给 NumPy <1.0 的旧 pickle）和 `_dtype_from_pep3118`（给 pybind11） |
| [numpy/core/multiarray.py:L1-L25](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/multiarray.py#L1-L25) | 转发到 `numpy._core.multiarray` 的垫片 | eager 绑定 `_reconstruct`、`scalar`（给 NumPy ≥1.0 的旧 pickle）和 `_ARRAY_API`（ABI，下一讲细讲） |
| [numpy/core/_multiarray_umath.py:L1-L22](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_multiarray_umath.py#L1-L22) | 转发到 `numpy._core._multiarray_umath` 的垫片 | 用 `for` 循环把真模块里**所有 ufunc** 批量 eager 绑定 |
| [numpy/core/__init__.py:L11-L20](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py#L11-L20) | 包入口 | eager 定义 `_ufunc_reconstruct`（给 1.20 前的旧 ufunc pickle） |

辅助证据（不在 `numpy/core/` 下，但能佐证「模块路径被写进 pickle」这一事实）：

- [numpy/_core/multiarray.py:L52-L55](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/multiarray.py#L52-L55)：真实现里把 `_reconstruct.__module__` 显式设成 `numpy._core.multiarray`。
- [numpy/_core/src/multiarray/methods.c:L1826-L1831](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/methods.c#L1826-L1831)：ndarray 在 C 层的 reduce 把 callable 写成 `numpy._core._multiarray_umath._reconstruct`。
- [numpy/_core/tests/test_multiarray.py:L5334-L5337](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/tests/test_multiarray.py#L5334-L5337)：测试里硬编码的旧 pickle 字节流，字面写着 `numpy.core._internal\n_reconstruct`。
- [numpy/_core/tests/test_ufunc.py:L213-L215](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/tests/test_ufunc.py#L213-L215)：旧 ufunc pickle 字节流，写着 `numpy.core\n_ufunc_reconstruct`。

## 4. 核心概念与源码讲解

### 4.1 pickle 的还原契约：模块路径 + 名字

#### 4.1.1 概念说明

上一节我们已经讲了 `find_class(module, name)`。这里把它和本讲的主线接起来。

NumPy 2.0 做了一件危险的事：把 `numpy.core` 整个搬到了 `numpy._core`。这意味着所有「把模块路径写成 `numpy.core.xxx`」的旧 pickle，在 2.0 上都会出现「模块路径找不到对应对象」的风险。如果放任不管，反序列化会在 `getattr(numpy.core.xxx, name)` 这一步失败，旧 pickle 直接作废。

numpy 的解法分两类：

1. **新 pickle 写新路径**：真实现里把关键重建函数的 `__module__` 显式设成新路径（`numpy._core.xxx`），让今后产生的 pickle 一律写新地址。
2. **旧 pickle 仍能还原**：在旧地址（`numpy.core.xxx`）保留垫片，并保证垫片能「无副作用地」把对象取出来。

第 2 类正是本讲重点。它引出了本讲最核心的一句话——

> **pickle 反序列化是「等不起警告」的。**

为什么？因为反序列化是 `pickle.find_class` 内部的一次 `getattr`，它发生在用户的 `pickle.loads()` 调用里。如果垫片走的是第 2 单元的惰性 `__getattr__`，每次 `getattr(numpy.core.multiarray, "_reconstruct")` 都会触发一次 `DeprecationWarning`。更糟的是：`__getattr__` 只在「正常属性查找失败」时才被调用——也就是说，只要垫片没有在 `__dict__` 里预先放好这个名字，pickle 每还原一个数组都要报警一次，而且这些警告的调用者其实不是「用户」，而是 pickle 内部。

更隐蔽的风险是：`_raise_warning` 的 `stacklevel=3` 是按「用户 → `__getattr__` → `_raise_warning` → `warn`」这三帧算出来的（见 u2-l3）。但 pickle 场景下调用链变成「用户 → `pickle.loads` → `find_class` → `__import__` → …… → `__getattr__` → `_raise_warning` → `warn`」，帧数对不上，`stacklevel=3` 会把警告归因到完全错误的位置。

所以结论很自然：**凡是会被 pickle 写进字节流的「重建函数 + 名字」，都必须在垫片顶部 eager 绑定，让它们直接出现在模块的 `__dict__` 里，从而绕开 `__getattr__`、绕开警告、绕开 stacklevel 错位。**

#### 4.1.2 核心流程

把上面的取舍画成一张对照表：

| 访问方式 | 走 `__dict__`? | 走 `__getattr__`? | 触发警告? | 适合 pickle? |
|---------|---------------|------------------|----------|-------------|
| 惰性转发（普通废弃属性） | 否（缺失） | 是 | 是（每次） | 否 |
| eager 绑定（重建函数） | 是（预先放好） | 否 | 否 | 是 |

eager 绑定的本质，就是在模块顶部执行一段**只跑一次**的代码（模块顶层代码因 `sys.modules` 缓存只执行一次，见 u2-l1），把关键对象写进 `globals()`：

```python
# 伪代码：eager 绑定的通用骨架
from numpy._core import 真模块

for 名字 in 一组必须免警告的名字:
    globals()[名字] = getattr(真模块, 名字)   # 写进本模块 __dict__
```

此后 `getattr(本垫片, 名字)` 命中 `__dict__`，根本不会触发 `__getattr__`，自然不报警、不报错、不依赖 stacklevel。这就是「eager」相对于「lazy」的全部含义。

#### 4.1.3 源码精读

先看一个铁证：旧 pickle 字节流里到底写了什么路径。numpy 的测试文件里硬编码了一批历史 pickle，直接证明了「模块路径被写进字节流」这件事：

[numpy/_core/tests/test_multiarray.py:L5334-L5337](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/tests/test_multiarray.py#L5334-L5337) 是一个「version 0」的旧数组 pickle，开头字节 `b"\x80\x02cnumpy.core._internal\n_reconstruct\n..."` 用 pickle 的 `c` 指令（`GLOBAL`）声明了重建函数的模块路径和名字——**字面就是 `numpy.core._internal._reconstruct`**。注意这是 NumPy 1.0 之前的写法。

而 [numpy/_core/tests/test_multiarray.py:L5405-L5406](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/tests/test_multiarray.py#L5405-L5406) 的另一个旧 pickle 写的是 `cnumpy.core.multiarray\n_reconstruct`，即 NumPy 1.0 之后的写法。同一批测试里两种路径并存，正好说明 `_reconstruct` 在历史上换过一次家。

再看新 pickle 写哪里。真实现侧 [numpy/_core/multiarray.py:L52-L55](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/multiarray.py#L52-L55) 显式注释「For backward compatibility, make sure pickle imports these functions from here」，并把：

```python
_reconstruct.__module__ = 'numpy._core.multiarray'
scalar.__module__ = 'numpy._core.multiarray'
```

这样**今后**产生的 pickle 都写新路径 `numpy._core.multiarray`。

而 C 层的 ndarray reduce 则把 callable 直接定位到 C 模块：[numpy/_core/src/multiarray/methods.c:L1826-L1831](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/methods.c#L1826-L1831)：

```c
mod = PyImport_ImportModule("numpy._core._multiarray_umath");
...
obj = PyObject_GetAttrString(mod, "_reconstruct");
```

即新 pickle 的 reduce 元组第一个元素解析为 `numpy._core._multiarray_umath._reconstruct`。

把这三段连起来看，结论非常清楚：**pickle 把对象的「出生地址」硬编码进字节流，而 numpy 在新旧两条地址线上都安排了能解析到同一对象的入口——新地址靠 `__module__` 改写，旧地址靠 `numpy.core.*` 垫片。**

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到「模块路径 + 名字」被写进 pickle 字节流。
2. **操作步骤**：运行下面这段「示例代码」（非项目原有代码）。

   ```python
   import pickle, numpy as np
   a = np.array([1, 2, 3], dtype=np.int8)
   data = pickle.dumps(a, protocol=2)
   print(data)                       # 看完整字节流
   print(b"_reconstruct" in data)    # True：重建函数名被写进去了
   print(b"numpy._core" in data)     # True：新地址
   ```
3. **需要观察的现象**：字节流里能找到 `_reconstruct` 这个名字，以及 `numpy._core._multiarray_umath`（或 `numpy._core.multiarray`）这段模块路径。
4. **预期结果**：`_reconstruct` 名字一定出现；模块路径是新的 `numpy._core.*`，而不是旧的 `numpy.core.*`（因为这是用当前 numpy 新生成的 pickle）。
5. 若想看「旧地址」长什么样，直接去看 4.1.3 引用的测试字节流即可，不必本地复现。

#### 4.1.5 小练习与答案

**练习 1**：如果 `numpy/core/multiarray.py` 既不改写 `__module__`、又不在垫片里 eager 绑定 `_reconstruct`，会发生什么？

**参考答案**：旧 pickle 字面写着 `numpy.core.multiarray._reconstruct`，反序列化时 `find_class("numpy.core.multiarray", "_reconstruct")` 走垫片的惰性 `__getattr__`，会抛 `DeprecationWarning`，且 stacklevel 因 pickle 调用链多帧而错位；如果该名字真缺失还会直接 `AttributeError`，旧 pickle 作废。

**练习 2**：为什么不能干脆删掉 `numpy.core`，让 `find_class` 直接去新模块找？

**参考答案**：因为字节流里写死的是旧路径 `numpy.core.*`，`find_class` 只会去 `numpy.core` 这个模块上取名字，不会自动猜测「它可能搬家到 `numpy._core` 了」。删掉旧模块 = 旧 pickle 无法还原。垫片就是用来回答「旧地址还在，我把你转发过去」。

---

### 4.2 _reconstruct 的两层历史绑定：_internal.py 与 multiarray.py

#### 4.2.1 概念说明

为什么 numpy 有**两个** `_reconstruct`？这是历史包袱：

- **NumPy 1.0 之前**：数组 pickle 的重建函数放在 `numpy.core._internal._reconstruct`。
- **NumPy 1.0 之后**：改放在 `numpy.core.multiarray._reconstruct`（C 实现）。

两类旧 pickle 在真实世界里都还存在（有人十年前存的 `.npy`/pickle 文件还在硬盘上）。所以 numpy 必须在**两个旧地址**都保留一个能用的 `_reconstruct`。这就是 [numpy/core/_internal.py](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_internal.py) 和 [numpy/core/multiarray.py](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/multiarray.py) 各自 eager 绑定一个 `_reconstruct` 的原因。

注意：这俩**不是同一个函数**。`_internal.py` 里的 `_reconstruct` 是一个纯 Python 的「历史复刻」，注释明确说它只为了兼容 1.0 前的 pickle；`multiarray.py` 里绑定的是真实现（C 实现）的 `_reconstruct`。

#### 4.2.2 核心流程

两个文件的 eager 绑定写法不同，但目的相同：

- `_internal.py`：手写一个 Python 函数 `_reconstruct`，**定义在垫片顶部**（定义即绑定，函数名直接进 `__dict__`）。
- `multiarray.py`：用 `for` 循环把真实现里的 `_reconstruct`、`scalar` 两个名字拷进 `globals()`。

两条路都让这些名字在垫片被 import 时就出现在 `__dict__` 里，后续访问永不触发 `__getattr__`。

#### 4.2.3 源码精读

先看 `_internal.py` 的全貌（这是个短文件，值得整段读）：

[numpy/core/_internal.py:L4-L16](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_internal.py#L4-L16)：

```python
# Build a new array from the information in a pickle.
# Note that the name numpy.core._internal._reconstruct is embedded in
# pickles of ndarrays made with NumPy before release 1.0
# so don't remove the name here, or you'll break backward compatibility.
def _reconstruct(subtype, shape, dtype):
    from numpy import ndarray
    return ndarray.__new__(subtype, shape, dtype)

# Pybind11 (in versions <= 2.11.1) imports _dtype_from_pep3118 from the
# _internal submodule, therefore it must be importable without a warning.
_dtype_from_pep3118 = _internal._dtype_from_pep3118
```

这段信息量很大，逐句拆：

- 注释 4-8 行是**最重要的历史交代**：`numpy.core._internal._reconstruct` 这个名字「被嵌进 1.0 之前的 pickle」，删了就会破坏向后兼容。这正是 4.1 讲的 `find_class` 契约。
- `def _reconstruct(...)` 定义在模块顶部，等价于 `globals()["_reconstruct"] = <函数>`。它是一个**纯 Python 复刻**：用 `ndarray.__new__` 造一个空壳数组（pickle 随后会用 `__setstate__` 把数据填进去）。它不转发任何东西，是垫片里少数「真有实现」的函数。
- `_dtype_from_pep3118 = _internal._dtype_from_pep3118` 是另一个 eager 绑定，但目的不同（见 4.2.5）：pybind11 ≤2.11.1 在初始化期会 `from numpy.core._internal import _dtype_from_pep3118`，这种 import 也等不起警告（第三方库没料到导入会报警）。

再看 `multiarray.py` 的 eager 绑定：

[numpy/core/multiarray.py:L3-L11](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/multiarray.py#L3-L11)：

```python
# these must import without warning or error from numpy.core.multiarray to
# support old pickle files
for item in ["_reconstruct", "scalar"]:
    globals()[item] = getattr(multiarray, item)

# Pybind11 (in versions <= 2.11.1) imports _ARRAY_API from the multiarray
# submodule as a part of NumPy initialization, therefore it must be importable
# without a warning.
_ARRAY_API = multiarray._ARRAY_API
```

- 这个 `for` 循环把真实现 `multiarray` 里的 `_reconstruct` 和 `scalar` 两个名字，显式塞进垫片的 `globals()`。注释直说「to support old pickle files」——和 `_internal.py` 同一回事，只是这里的 `_reconstruct` 是 C 实现版（不是 `_internal.py` 里那个 Python 版）。
- `_ARRAY_API` 的 eager 绑定（第 11 行）属于另一类兼容（C-ABI，且要抛 `ImportError`），留给下一讲 u3-l2，本讲先记住「它也是 eager 绑定的」。

对比这两个文件，可以总结一条**判定 eager 绑定的判据**（呼应 u1-l2 用 `ast` 判定纯转发 vs 特殊垫片）：凡是垫片顶层除了 `def __getattr__` 之外，还有 `def _reconstruct`、`globals()[...] = ...`、`_xxx = 真模块.xxx` 这类**赋值给模块全局变量**的语句，它就是「特殊垫片」，被赋值的名字就是「等不起警告、必须 eager」的名字。

最后注意这两个垫片的 `__getattr__` 仍用 u2-l2 讲过的 **`None` 写法**：

[numpy/core/multiarray.py:L13-L22](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/multiarray.py#L13-L22)：

```python
def __getattr__(attr_name):
    from numpy._core import multiarray
    from ._utils import _raise_warning
    ret = getattr(multiarray, attr_name, None)
    if ret is None:
        raise AttributeError(...)
    _raise_warning(attr_name, "multiarray")
    return ret
```

也就是说：`_reconstruct`、`scalar` 走 eager（命中 `__dict__`，不进这个函数、不报警）；其它所有废弃属性走 `__getattr__`（进这个函数、报警）。两条通路并存于同一文件。

#### 4.2.4 代码实践

1. **实践目标**：直接用测试里硬编码的旧 pickle 字节流，验证 `numpy.core._internal._reconstruct` 这条 1.0 前的旧路径在当前 numpy 上仍然能还原数组。
2. **操作步骤**：把 [numpy/_core/tests/test_multiarray.py:L5332-L5341](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/tests/test_multiarray.py#L5332-L5341) 的 `test_version0_int8` 用例抄成一段「示例代码」直接跑：

   ```python
   import pickle, warnings, numpy as np
   from numpy.testing import assert_equal

   s = (
       b"\x80\x02cnumpy.core._internal\n_reconstruct\nq\x01cnumpy\n"
       b"ndarray\nq\x02K\x00\x85U\x01b\x87Rq\x03(K\x04\x85cnumpy\n"
       b"dtype\nq\x04U\x02i1K\x00K\x01\x87Rq\x05(U\x01|NNJ\xff\xff\xff"
       b"\xffJ\xff\xff\xff\xfftb\x89U\x04\x01\x02\x03\x04tb."
   )
   with warnings.catch_warnings(record=True) as w:
       warnings.simplefilter("always")
       p = pickle.loads(s, encoding='latin1')   # 反序列化旧 pickle
   print(repr(p))                                # 期望 array([1,2,3,4], dtype=int8)
   assert_equal(np.array([1,2,3,4], dtype=np.int8), p)

   # 关键断言：还原过程【没有】产生 DeprecationWarning
   assert not any(issubclass(x.category, DeprecationWarning) for x in w), \
       "还原旧 pickle 不应报警，但报了：" + repr([str(x.message) for x in w])
   print("OK: 旧 pickle 成功还原且无弃用警告")
   ```
3. **需要观察的现象**：`p` 是一个 `int8` 的 `[1,2,3,4]` 数组；捕获到的警告列表里**没有任何** `DeprecationWarning`。
4. **预期结果**：打印 `OK: 旧 pickle 成功还原且无弃用警告`。这正是 `_internal.py` 顶部 eager 定义 `_reconstruct` 带来的效果——若把它注释掉，`pickle.loads` 会改走 `__getattr__` 并报警，甚至失败。
5. 如果本地 numpy 不是 2.x（没有 `_core`），这段会报错——属于环境问题，可标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`_internal.py` 的 `_reconstruct` 和 `multiarray.py` 绑定的 `_reconstruct` 为什么不能合并成一个？

**参考答案**：它们对应两个不同历史时期写进 pickle 的模块路径（`numpy.core._internal` vs `numpy.core.multiarray`），`find_class` 按字节流里的精确路径找模块，两个路径都得各自能解析到 `_reconstruct`。而且 `_internal` 版是纯 Python 复刻（只为兼容 1.0 前），`multiarray` 版是 C 真实现，两者实现也不同。

**练习 2**：`_dtype_from_pep3118`（`_internal.py` 第 16 行）和 `_reconstruct` 都是 eager 绑定，但原因不同。请说出区别。

**参考答案**：`_reconstruct` 是为了 **pickle 还原**（字节流写了旧路径）；`_dtype_from_pep3118` 是为了 **pybind11 ≤2.11.1 的初始化期 import**（第三方扩展在 numpy 初始化时会 `from numpy.core._internal import _dtype_from_pep3118`，这种 import 若报警会污染/中断第三方库启动）。共同点是「调用方等不起 DeprecationWarning」。

---

### 4.3 ufunc 的批量 eager 绑定：_multiarray_umath.py 的遍历

#### 4.3.1 概念说明

数组只有一个 `_reconstruct`，手写 eager 绑定很轻松。但 ufunc（通用函数，如 `np.add`、`np.cos`、`np.matmul`）有**几十上百个**，而且它们也会被写进 pickle——pickle 一个 ufunc 时，写的是「模块路径 + ufunc 名字」。

ufunc 的「出生地址」在老版本里是 `numpy.core._multiarray_umath`（因为 ufunc 都是在这个 C 扩展里被创建的）。所以旧 ufunc pickle 字面写着 `numpy.core._multiarray_umath.<ufunc名>`。要还原它们，就得让 `numpy.core._multiarray_umath` 这个垫片上，每一个 ufunc 名字都能免警告地取到。

手写几十个 `globals()["add"] = ...` 既丑又易漏。numpy 用了一个优雅的循环：**遍历真模块的所有公开名字，凡是 `ufunc` 类型的就绑进 `globals()`**。

#### 4.3.2 核心流程

遍历绑定算法（伪代码）：

```
输入：真模块 M = numpy._core._multiarray_umath
for item in dir(M):
    attr = getattr(M, item)
    if isinstance(attr, ufunc):     # 只挑 ufunc
        globals()[item] = attr      # 绑进垫片 __dict__
```

为什么「只挑 ufunc」而不是全部绑定？因为非 ufunc 的名字（比如 `_ARRAY_API` 这种 ABI 符号）需要走**另一套**特殊处理（下一讲），不能简单绑进来。用类型过滤能精确地只覆盖「会被 pickle、且需要免警告」的那一批。

#### 4.3.3 源码精读

[numpy/core/_multiarray_umath.py:L1-L9](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_multiarray_umath.py#L1-L9)：

```python
from numpy import ufunc
from numpy._core import _multiarray_umath

for item in _multiarray_umath.__dir__():
    # ufuncs appear in pickles with a path in numpy.core._multiarray_umath
    # and so must import from this namespace without warning or error
    attr = getattr(_multiarray_umath, item)
    if isinstance(attr, ufunc):
        globals()[item] = attr
```

逐句拆：

- `from numpy import ufunc`：引入 `ufunc` 类型，仅用于下面的 `isinstance` 判断。
- `for item in _multiarray_umath.__dir__()`：遍历真模块的所有公开名字（含 `__dir__()` 返回的，等价于 `dir()`）。注意是 `__dir__()` 不是 `__dict__`，因为真模块里有些名字是动态暴露的、不一定在 `__dict__` 顶层。
- 注释直接点题：「ufuncs appear in pickles with a path in numpy.core._multiarray_umath, and so must import from this namespace without warning or error」——这就是 4.1 讲的 `find_class` 契约的直接体现。
- `if isinstance(attr, ufunc): globals()[item] = attr`：只把 ufunc 绑进来。

那么新 ufunc pickle 写的是哪条路径？看真实现侧的 reduce 注册：

[numpy/_core/__init__.py:L164-L170](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/__init__.py#L164-L170)：

```python
def _ufunc_reduce(func):
    # Report the `__name__`. pickle will try to find the module. ...
    return func.__name__
```

配合 [numpy/_core/__init__.py:L194](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/__init__.py#L194) 的 `copyreg.pickle(ufunc, _ufunc_reduce)`：当前 numpy 序列化 ufunc 时只返回它的 `__name__`（一个字符串），pickle 据此用 `find_class(ufunc.__module__, name)` 还原。builtin ufunc 的 `__module__` 在 2.0 是 `numpy._core.umath`，所以**新** pickle 写 `numpy._core.umath.<name>`。但**旧** pickle 写的是 `numpy.core._multiarray_umath.<name>`，这正是本垫片要兼容的对象。

而更古老的 ufunc pickle（1.20 之前）甚至不用 `find_class` 直接定位 ufunc，而是先定位一个「重建函数」`numpy.core._ufunc_reconstruct`，再把模块名和 ufunc 名当参数传进去。这条路径由包入口的 eager 定义守护：

[numpy/core/__init__.py:L11-L20](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py#L11-L20)：

```python
# We used to use `np.core._ufunc_reconstruct` to unpickle.
# This is unnecessary, but old pickles saved before 1.20 will be using it,
# and there is no reason to break loading them.
def _ufunc_reconstruct(module, name):
    mod = __import__(module, fromlist=[name])
    return getattr(mod, name)
```

注释明确：「old pickles saved before 1.20 will be using it」。它定义在 `__init__.py` 顶部（eager），名字直接进包的 `__dict__`，所以旧 ufunc pickle 走 `find_class("numpy.core", "_ufunc_reconstruct")` 时命中 `__dict__`，不进包级 `__getattr__`、不报警。还原时它再动态 `__import__(module)` 去新地址取真 ufunc。

测试铁证在 [numpy/_core/tests/test_ufunc.py:L213-L215](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/tests/test_ufunc.py#L213-L215)：

```python
astring = (b"cnumpy.core\n_ufunc_reconstruct\np0\n"
           b"(S'numpy._core.umath'\np1\nS'cos'\np2\n...)")  # 简写
assert_(pickle.loads(astring) is np.cos)
```

这段字节流用 `c` 指令声明 `numpy.core._ufunc_reconstruct`，参数是模块 `numpy._core.umath` 和名字 `cos`——也就是「在旧地址找重建函数，让重建函数去新地址取真对象」。完美印证了垫片的设计意图。

#### 4.3.4 代码实践

1. **实践目标**：观察一个 ufunc pickle 的字节流，并验证「遍历绑定」真的把所有 ufunc 塞进了垫片 `__dict__`。
2. **操作步骤**（「示例代码」）：

   ```python
   import pickle, warnings, numpy as np

   # (a) 看新 ufunc pickle 写的模块路径
   data = pickle.dumps(np.cos)
   print("cos pickle:", data)            # 期望出现 numpy._core.umath 和 cos
   print("写的是新地址:", b"numpy._core.umath" in data)

   # (b) 验证 _multiarray_umath 垫片已经 eager 绑定了大量 ufunc
   import numpy.core._multiarray_umath as m
   ufs = [n for n in dir(m) if isinstance(getattr(m, n), np.ufunc)]
   print("垫片里 eager 绑定的 ufunc 数量:", len(ufs))
   print("前 5 个:", ufs[:5])

   # (c) 访问这些 ufunc 不应产生 DeprecationWarning（因为是 eager 绑定）
   with warnings.catch_warnings(record=True) as w:
       warnings.simplefilter("always")
       _ = m.add          # 命中 __dict__，不走 __getattr__
   assert not any(issubclass(x.category, DeprecationWarning) for x in w), \
       "eager 绑定的 ufunc 不该报警"
   print("OK: ufunc 访问无警告")
   ```
3. **需要观察的现象**：(a) 字节流含 `numpy._core.umath` 和 `cos`；(b) `dir(m)` 里能列出大量 ufunc（远不止一两个），且数量与 `numpy._core._multiarray_umath` 里的 ufunc 数一致；(c) 访问 `m.add` 不报警。
4. **预期结果**：三个断言/打印全部符合。其中 (b) 的数量通常在 **80+**（视 numpy 版本而定），证明那个 `for` 循环确实批量绑定了所有 ufunc。
5. 数量随版本变化属正常，重点看「数量远大于 1」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_multiarray_umath.py` 用 `isinstance(attr, ufunc)` 过滤，而不是把真模块的全部名字都绑进来？

**参考答案**：非 ufunc 的名字里包含 `_ARRAY_API`/`_UFUNC_API` 这类 C-ABI 符号，它们需要走「抛 ImportError」的特殊分支（见 u3-l2）；还有一些内部符号不该随便再导出。用 `isinstance` 精确锁定「会被 pickle、且需要免警告」的 ufunc 子集，既覆盖了兼容需求，又不破坏其它符号的特殊处理。

**练习 2**：`_ufunc_reconstruct`（`__init__.py`）和 `_multiarray_umath` 的 ufunc 遍历绑定，分别守护哪个年代的 ufunc pickle？

**参考答案**：`_ufunc_reconstruct` 守护 1.20 之前的 ufunc pickle（那种 pickle 先定位重建函数，再把「模块名+ufunc 名」当参数传进去）；`_multiarray_umath` 的遍历绑定守护 1.20 之后、2.0 之前的 ufunc pickle（那种 pickle 直接写 `numpy.core._multiarray_umath.<ufunc名>`，靠 `find_class` 取对象）。两者年代不同，写法不同，但都是「旧地址免警告取对象」。

---

## 5. 综合实践

把本讲三个机制（eager 绑定重建函数、eager 绑定 ufunc、改模块路径仍能还原）串起来，亲手为一个**虚构的**「包改名」场景做一个最小兼容垫片。

**背景设定**：你的旧库叫 `mymath`，里面的 `mymath._math` 模块定义了类 `Vec` 和一个函数 `add`。现在你把库重构成了 `_mymath`（真实现搬家了），但老用户硬盘上还有用旧库 pickle 的 `Vec` 对象。你需要在旧地址 `mymath._math` 留一个垫片，让旧 pickle 仍能还原。

**操作步骤**（全部为「示例代码」，需自行建好对应目录结构）：

1. 先建「新包」`_mymath`（真实现）：

   ```python
   # _mymath/__init__.py  （空）
   # _mymath/_math.py
   class Vec:
       def __init__(self, x): self.x = x
       def __eq__(self, o): return isinstance(o, Vec) and self.x == o.x
   def add(a, b): return Vec(a.x + b.x)
   # 让【新】pickle 把还原函数写在【新】地址
   Vec.__module__ = "_mymath._math"
   ```

2. 写一个旧风格的 reduce 函数，并**故意把它的模块路径写成旧地址** `mymath._math`（模拟老版本 pickle 字节流里写死的路径）：

   ```python
   # 关键：把还原函数的 __module__ 设成【旧】地址
   def _reconstruct(x):
       return Vec(x)
   _reconstruct.__module__ = "mymath._math"
   ```

3. 生成一个旧 pickle（用上面的 reduce），观察它的字节流里写的是旧地址 `mymath._math._reconstruct`：

   ```python
   import pickle
   v = Vec(7)
   # 自定义 reduce，让 pickle 把还原函数写成旧地址的 _reconstruct
   Vec.__reduce__ = lambda self: (_reconstruct, (self.x,))
   s = pickle.dumps(v)
   print(b"mymath._math" in s)           # True：旧路径被写进字节流
   ```

4. 现在「搬家」：新包 `_mymath` 已就位；在旧地址 `mymath/_math.py` 放一个**垫片**，仿照 `numpy/core/_internal.py` 的写法——eager 定义 `_reconstruct`，并给其它属性做惰性转发（带弃用警告）：

   ```python
   # mymath/__init__.py  （空）
   # mymath/_math.py  （垫片，仿 numpy/core/_internal.py）
   import warnings
   from _mymath import _math as _real

   def _reconstruct(x):          # eager 绑定：定义即进 __dict__，pickle 还原免警告
       return _real.Vec(x)

   def __getattr__(name):
       warnings.warn(f"mymath._math is deprecated, use _mymath._math",
                     DeprecationWarning)
       return getattr(_real, name)
   ```

5. 验证三件事（对应本讲的三个核心结论）：

   ```python
   # (a) 旧 pickle 仍能还原对象（eager 绑定生效，find_class 在旧地址找到 _reconstruct）
   v2 = pickle.loads(s)
   assert v2 == Vec(7)
   # (b) 还原过程不报警（因为 _reconstruct 走的是 __dict__，不是 __getattr__）
   #     用 warnings.catch_warnings(record=True) 包住 pickle.loads 断言无 DeprecationWarning
   # (c) 访问废弃属性仍会报警（惰性 __getattr__ 只管非 eager 的名字）
   ```

**需要观察的现象与预期结果**：

- 旧 pickle 能还原出 `Vec(7)`——证明 eager 绑定让旧地址仍可解析。
- 还原过程**没有** `DeprecationWarning`——证明 eager 绑定绕开了 `__getattr__`。
- 单独访问 `mymath._math.add`（非 eager 的名字）**会**报警——证明惰性转发仍在工作，eager 只是个别豁免。

如果第 4 步你把 `def _reconstruct` 删掉、改成「全部走 `__getattr__`」，第 5 步 (b) 就会失败（还原时报 `DeprecationWarning`，甚至因 stacklevel 错位而归因错误）。亲手做这个「删了再坏」的反例，是理解本讲最有效的方式。

> 说明：第 3 步用 Python 手工生成「写死旧路径」的 pickle 需要自定义 `__reduce__` 并改 `__module__`。如果觉得麻烦，也可以直接参照 4.2.4 里从测试文件复制旧 pickle 字节流的做法，把 `mymath._math._reconstruct` 的字节流手工拼出来。

## 6. 本讲小结

- pickle 反序列化靠 `find_class(module, name)` 还原重建函数，**模块路径被硬编码进字节流**；对象搬家后，旧地址必须仍能解析到同名对象，否则旧 pickle 作废。
- numpy 用两条手段保兼容：**新 pickle** 靠把 `__module__` 改写成 `numpy._core.*`（如 `_core/multiarray.py` 第 54-55 行）；**旧 pickle** 靠 `numpy/core/*` 垫片。
- 「等不起警告」的对象必须 **eager 绑定**（写进垫片 `__dict__`），从而绕开惰性 `__getattr__`、绕开 `DeprecationWarning`、绕开 `stacklevel` 错位。这是特殊垫片区别于纯转发垫片的本质（呼应 u1-l2）。
- `_reconstruct` 有两层历史绑定：`_internal.py`（Python 复刻，兼容 NumPy <1.0 的 pickle）和 `multiarray.py`（绑定 C 实现 `_reconstruct`/`scalar`，兼容 NumPy ≥1.0 的 pickle）。
- `_multiarray_umath.py` 用 `for item in __dir__(): if isinstance(attr, ufunc): globals()[item] = attr` 的遍历写法，把所有 ufunc 批量 eager 绑定，兼容「字面写 `numpy.core._multiarray_umath.<ufunc>`」的旧 pickle。
- `_ufunc_reconstruct`（`__init__.py`）守护更古老的（1.20 前）ufunc pickle；`_dtype_from_pep3118`/`_ARRAY_API` 的 eager 绑定同理——都是「调用方等不起警告」，只是调用方分别是 pickle 和 pybind11。

## 7. 下一步学习建议

- 本讲只讲了「eager 绑定让它不报警」，但 `_multiarray_umath.py` 和 `multiarray.py` 里对 `_ARRAY_API`/`_UFUNC_API` 的处理更进一步——**直接抛 `ImportError`**。为什么这两个符号要用异常而非警告？请进入下一讲 **u3-l2《C-API/ABI 兼容：_ARRAY_API 守卫与 NumPy 1.x/2.x 冲突》**，那里会讲 NumPy 1.x 与 2.x 的 ABI 不兼容、`traceback.format_stack` 的使用，以及为什么「硬拦」比「软警告」更安全。
- 如果你想更系统地理解 pickle 协议本身，建议读 Python 标准库 `pickle` 的源码（重点是 `find_class` 和 `load_*`/`save_*` 系列方法），再用本讲的视角回看 numpy 的 `_reconstruct`，会有「豁然开朗」的感觉。
- 想看 numpy 还为 pickle 做了哪些兼容，可以读 [numpy/_core/__init__.py:L173-L198](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/__init__.py#L173-L198)（`_DType_reconstruct` 与 `copyreg.pickle` 注册），那里对 dtype 类型的 pickle 兼容与本讲同源。
