# 比较与拼接类 ufunc：equal / add / multiply

## 1. 本讲目标

本讲聚焦 `numpy.strings` 里三类「输入是字符串、输出也是字符串或布尔值」的基础运算：

- **比较类**：`equal`、`not_equal`、`less`、`less_equal`、`greater`、`greater_equal`
- **拼接类**：`add`（字符串拼接）、`multiply`（字符串重复）

学完本讲，你应当能够：

1. 说清楚为什么比较类 ufunc 与 `add` 在 `numpy/_core/strings.py` 里**没有任何 Python 包装**，而是直接复用 numpy 顶层的 ufunc 对象。
2. 看懂 `multiply` 是这一组里**唯一的特例**——它是一个带 `@set_module` 与 `@array_function_dispatch` 的 Python 包装函数，内部再调用被改名为 `_multiply_ufunc` 的顶层 multiply ufunc。
3. 解释 `multiply` 的**双分支**结构：`StringDType`（`dtype.char == 'T'`）走 `a * i` 快速路径，定长 `str_`/`bytes_` 走「`str_len` 预算缓冲区 + 溢出保护 + 显式 `out`」路径。
4. 动手触发 `multiply` 的 `OverflowError`，并对比两种输入 dtype 走的是不同的检查位置。

## 2. 前置知识

本讲直接承接以下已建立的认知，不再重复细节：

- **u1-l1 / u1-l2**：`numpy.strings` 是门面，真正实现在 `numpy/_core/strings.py`；字符串输入有三种 dtype——变长 `StringDType`（`dtype.char == 'T'`）、定长 `bytes_`（`'S'`）、定长 `str_`（`'U'`，1 字符固定 4 字节）。
- **u2-l4**：每个 `numpy.strings` 函数身上常见的两件套——`@set_module('numpy.strings')` 管「身份」（改写 `__module__`），`@array_function_dispatch(dispatcher)` 管「行为」（NEP-18 `__array_function__` 分发）；裸 ufunc（如 `str_len`、`isalpha`）无法被装饰器装饰，靠模块加载时执行的 `_override___module__()` 就地改写 `__module__`。
- **u2-l5**：`_get_num_chars` 读取定长 dtype 的「字段容量」，与运行时长度（由 `str_len` 给出）是两回事。

本讲只需要再补一个最朴素的结论作为出发点：**NumPy 的顶层 `np.equal`、`np.add`、`np.multiply` 等本是面向数值的通用 ufunc**，它们之所以也能作用于字符串数组，是因为 C 层（`string_ufuncs.cpp` / `stringdtype_ufuncs.cpp`）在这些**已存在**的 ufunc 上额外挂载了字符串专用的循环。理解了这一点，本讲剩下的内容就是「Python 层如何把现成的 ufunc 暴露出来 / 或为它套一层壳」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [numpy/_core/strings.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py) | 本讲主战场：顶部 import 复用顶层 ufunc，`__all__` 转发，`multiply` 在此实现 |
| [numpy/_core/src/umath/string_ufuncs.cpp](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp) | C++ 层：把字符串循环挂到既有的 `equal`/`add`/`multiply` 等 ufunc 上（定长 `str_`/`bytes_`） |
| [numpy/_core/src/umath/stringdtype_ufuncs.cpp](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/stringdtype_ufuncs.cpp) | C++ 层：`StringDType`（变长）专用的 `multiply` 循环，含独立的溢出检查 |

> 说明：本系列首篇 u1-l1 已确认 `numpy.strings` 的实现都在 `numpy/_core/strings.py`，而非门面目录 `numpy/strings/`。因此本讲的永久链接指向 `numpy/_core/strings.py` 与两个 C++ 文件。

## 4. 核心概念与源码讲解

### 4.1 直接复用型 ufunc：equal / not_equal / less / greater / add

#### 4.1.1 概念说明

比较与 `add` 是 `numpy.strings` 里**最省事**的一组：Python 层一行实现都不写，直接把 numpy 顶层的 ufunc 对象「借」过来用。它们出现在 `numpy.strings` 命名空间里，完全是靠 import + `__all__` 转发。

这一组与我们之前见过的 `str_len`、`isalpha`（被 `_override___module__` 改写 `__module__`）以及本讲的 `multiply`（被 `@set_module` 改写）**不同**：比较类与 `add` 既没有被装饰器包裹，也不在 `_override___module__()` 的清单里。换句话说，它们就是顶层的 `np.equal`、`np.add` 本身，只是多了一条「别名」`np.strings.equal` 指向同一个对象。

> 术语澄清：本讲学习目标里提到「改写 `__module__`」，准确地说，它适用于 `multiply`（经 `@set_module`）和 `str_len`/`is*`（经 `_override___module__`）；比较类与 `add` 是「原样复用、不改写」。把这条边界讲清楚，正是本小节的任务。

#### 4.1.2 核心流程

一个比较/`add` 调用的完整路径：

1. 用户写 `np.strings.equal(a, b)`，名字解析到 `numpy._core.strings.equal`。
2. 该名字其实绑定着顶层 `numpy.equal` 这个 ufunc 对象（import 时建立）。
3. ufunc 按 dtype 选择循环：当输入是字符串数组时，命中 C 层挂载的字符串比较循环。
4. 返回布尔数组（比较类）或新的字符串数组（`add`）。

整个 Python 层没有任何额外工作，因此也没有 dispatcher、没有 `@set_module`。

#### 4.1.3 源码精读

**① 顶部 import：把顶层 ufunc 借进来**

[numpy/_core/strings.py:10-19](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L10-L19) 把比较六件套、`add` 直接从 `numpy` 顶层导入；注意 `multiply` 被改名为 `_multiply_ufunc`，留待 4.2 节使用：

```python
from numpy import (
    add,
    equal,
    greater,
    greater_equal,
    less,
    less_equal,
    multiply as _multiply_ufunc,
    not_equal,
)
```

这里 `add`、`equal`、`greater`、`greater_equal`、`less`、`less_equal`、`not_equal` 都用原名导入，意味着它们就是顶层对象本身。

**② `__all__` 把它们转发到门面**

[numpy/_core/strings.py:73-90](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L73-L90) 的 `__all__` 把这些名字标记为公共符号，门面 `numpy/strings/__init__.py` 的 `from numpy._core.strings import *` 因此能把它们再转发一次。第 75–76 行集中列出了比较与 `add`/`multiply`：

```python
__all__ = [
    # UFuncs
    "equal", "not_equal", "less", "less_equal", "greater", "greater_equal",
    "add", "multiply", ...
]
```

**③ 对比 `_override___module__`：它们不在改写名单里**

[numpy/_core/strings.py:61-70](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L61-L70) 只改写 `is*` 系列与 `str_len` 这 10 个 ufunc 的 `__module__`。仔细看清单——`equal`/`add`/`not_equal`/`less`/`greater` **都不在其中**：

```python
def _override___module__():
    for ufunc in [
        isalnum, isalpha, isdecimal, isdigit, islower, isnumeric, isspace,
        istitle, isupper, str_len,
    ]:
        ufunc.__module__ = "numpy.strings"
        ufunc.__qualname__ = ufunc.__name__
```

这就是「直接复用、不改写」的代码证据。

**④ 它们为什么对字符串「直接能跑」？——C 层挂载循环**

比较与 `add` 之所以能处理字符串数组，是因为 C 层把这些循环注册到了**已存在**的顶层 ufunc 上。以 `add` 为例，[numpy/_core/src/umath/string_ufuncs.cpp:1493-1504](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1493-L1504) 在名为 `"add"` 的既有 ufunc 上挂了 ASCII 与 UTF32 两个循环：

```cpp
if (init_ufunc(
        umath, "add", 2, 1, dtypes, ENCODING::ASCII,
        string_add_loop<ENCODING::ASCII>, string_addition_resolve_descriptors,
        NULL) < 0) { ... }
```

比较类的名字同样来自 [numpy/_core/src/umath/string_ufuncs.cpp:35-40](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L35-L40)，逐个挂到顶层 `equal`/`not_equal`/`less`/`less_equal`/`greater`/`greater_equal`：

```cpp
case COMP::EQ: return "equal";
case COMP::NE: return "not_equal";
case COMP::LT: return "less";
case COMP::LE: return "less_equal";
case COMP::GT: return "greater";
case COMP::GE: return "greater_equal";
```

所以 Python 层「不写一行」并非偷懒，而是因为底层早就把这些 ufunc 武装好了——C 层的注册细节会在 u3-l12 详讲，本讲只需知道「循环是挂在既有 ufunc 上」这一事实即可。

#### 4.1.4 代码实践

**实践目标**：亲手验证比较类与 `add` 是「同一个对象、未被本模块改写」。

**操作步骤**：

```python
import numpy as np

# 1. 验证 np.strings.equal 与 np.equal 是同一个对象
print(np.strings.equal is np.equal)        # 预期 True

# 2. 观察它们的 __module__（顶层 ufunc 的归属）
print(np.strings.equal.__module__)         # 顶层 ufunc 的 module，通常为 'numpy'
print(np.strings.equal is np.strings.not_equal)  # 预期 False（不同 ufunc）

# 3. 对比被改写过的 str_len
print(np.strings.str_len.__module__)       # 预期 'numpy.strings'（被 _override___module__ 改写）

# 4. add 对字符串生效
a = np.array(["foo", "bar"])
b = np.array(["!", "?"])
print(np.strings.add(a, b))                # 预期 ['foo!' 'bar?']，dtype '<U4'

# 5. 比较逐元素返回布尔
print(np.strings.equal(a, np.array(["foo", "baz"])))  # 预期 [ True False]
```

**需要观察的现象**：`np.strings.equal is np.equal` 为 `True`；`equal.__module__` 与 `str_len.__module__` 不同，前者保留顶层归属，后者被改写成 `numpy.strings`。

**预期结果**：上述注释中的值。若 `__module__` 的具体字符串在不同 NumPy 版本略有差异，以「`equal` 与 `str_len` 二者归属不同」这一对比为准——这正是 4.1.1 里那条边界的直接体现。

#### 4.1.5 小练习与答案

**练习 1**：`np.strings.add` 与 `np.strings.multiply` 谁是「裸 ufunc 直接复用」、谁是「Python 包装函数」？如何用一行代码区分？

**参考答案**：`add` 是裸 ufunc直接复用，`multiply` 是 Python 包装函数。可用 `type(...).__name__` 区分：`np.add`/`np.strings.add` 的类型是 `numpy.ufunc`，而 `np.strings.multiply` 的类型是 `_ArrayFunctionDispatcher`（被 `@array_function_dispatch` 包过）。也可以看 `hasattr(np.strings.multiply, "_implementation")` 是否为 `True`。

**练习 2**：为什么 `np.strings.equal` 不需要写 dispatcher，却仍然能参与 `__array_function__` 协议？

**参考答案**：比较类 ufunc 本身就是顶层 ufunc，ufunc 天然支持以类型自己的循环（`__array_ufunc__`）进行覆盖，这是 NEP-13 的 ufunc 机制，不依赖 `array_function_dispatch`。只有像 `multiply` 这样被包装成普通 Python 函数的，才需要 `array_function_dispatch` 来补上 NEP-18 的 `__array_function__` 入口。

---

### 4.2 multiply 与 _multiply_dispatcher：NEP-18 分发 + 双分支

#### 4.2.1 概念说明

`multiply(a, i)` 表示「把字符串重复 `i` 次」，行为等价于 Python 的 `a * i`，但是向量化的。它是本组里**唯一**的 Python 包装函数，原因在于定长字符串 dtype 下，输出长度依赖**运行时**才知道的重复次数，ufunc 没法从 dtype 反推输出宽度——所以必须在 Python 层先算好缓冲区、再把 `out` 显式喂给底层 ufunc（4.2.3 的源码会看到 C 层为此专门拒绝「不给 `out`」的调用）。

它身上挂了两个装饰器，正是 u2-l4 讲过的那套模子：

- `@set_module("numpy.strings")`：把 `__module__` 改成 `numpy.strings`，让帮助文档归属正确。
- `@array_function_dispatch(_multiply_dispatcher)`：用 dispatcher 声明「哪些参数相关」，启用 NEP-18 分发。

#### 4.2.2 核心流程

`multiply(a, i)` 的执行流程（定长分支是重点）：

1. `a = np.asanyarray(a)`、`i = np.asanyarray(i)`，统一成数组。
2. 校验 `i` 必须是整数 dtype，否则 `TypeError`；负数统一钳为 0（`np.maximum(i, 0)`）。
3. **分支判断**：
   - 若 `a.dtype.char == "T"`（`StringDType` 变长）：直接 `return a * i`，把活儿全交给 StringDType 的 C 循环（它内部自己做溢出检查）。
   - 否则（定长 `str_`/`bytes_`）进入下一步。
4. `a_len = str_len(a)` 得到每个元素的实际字符数。
5. **溢出保护**：若任意位置 `a_len * i` 会超过 `sys.maxsize`，抛 `OverflowError`。
6. `buffersizes = a_len * i`，取最大值拼出输出 dtype 字符串（如 `'U9'`）。
7. 用 `np.empty_like` 预分配 `out`，调用底层 `_multiply_ufunc(a, i, out=out)` 写入并返回。

#### 4.2.3 源码精读

**① dispatcher：只关心 `a`**

[numpy/_core/strings.py:144-145](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L144-L145) 的 `_multiply_dispatcher` 只返回 `(a,)`，意味着在 NEP-18 协议里，「重复次数 `i`」不被视为「需要参与覆盖的相关参数」，只有字符串数组 `a` 的类型才有机会接管：

```python
def _multiply_dispatcher(a, i):
    return (a,)
```

**② 主体：装饰器 + 函数签名**

[numpy/_core/strings.py:148-150](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L148-L150) 是两个装饰器与函数定义：

```python
@set_module("numpy.strings")
@array_function_dispatch(_multiply_dispatcher)
def multiply(a, i):
```

**③ StringDType 快速路径**

[numpy/_core/strings.py:190-199](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L190-L199) 先做参数清理，再判断变长 dtype：

```python
a = np.asanyarray(a)
i = np.asanyarray(i)
if not np.issubdtype(i.dtype, np.integer):
    raise TypeError(f"unsupported type {i.dtype} for operand 'i'")
i = np.maximum(i, 0)

# delegate to stringdtype loops that also do overflow checking
if a.dtype.char == "T":
    return a * i
```

注意注释「delegate to stringdtype loops that also do overflow checking」——`StringDType` 的溢出检查发生在 C 层 [numpy/_core/src/umath/stringdtype_ufuncs.cpp:138-143](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/stringdtype_ufuncs.cpp#L138-L143)：

```cpp
int overflowed = npy_mul_with_overflow_size_t(
        &newsize, cursize, factor);
if (overflowed || newsize > PY_SSIZE_T_MAX) {
    npy_gil_error(PyExc_OverflowError,
              "Overflow encountered in string multiply");
    goto fail;
}
```

这就是「`'T'` 分支连溢出检查也委托给 C」的含义。

**④ 定长分支：str_len 预算 + 溢出保护 + 显式 out**

[numpy/_core/strings.py:201-210](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L201-L210) 是本讲最核心的一段：

```python
a_len = str_len(a)

# Ensure we can do a_len * i without overflow.
if np.any(a_len > sys.maxsize / np.maximum(i, 1)):
    raise OverflowError("Overflow encountered in string multiply")

buffersizes = a_len * i
out_dtype = f"{a.dtype.char}{buffersizes.max()}"
out = np.empty_like(a, shape=buffersizes.shape, dtype=out_dtype)
return _multiply_ufunc(a, i, out=out)
```

逐行解读：

- `str_len(a)` 给出每个元素的真实字符数（对 `str_` 也已处理好「字节数 / 4」，见 u1-l2）。
- 溢出判定的数学含义见 4.3 节；其目的是保证下一行 `a_len * i` 在 `int64` 范围内不溢出。
- `out_dtype = f"{a.dtype.char}{buffersizes.max()}"` 把 dtype 字符（`'U'` 或 `'S'`）与「全数组最大输出宽度」拼成定长 dtype 字符串，例如 `str_` 输入重复到最长 12 个字符就是 `'U12'`。
- `np.empty_like(a, shape=buffersizes.shape, dtype=out_dtype)` 预分配形状与 `a` 广播后一致、宽度足够的输出数组。
- `_multiply_ufunc(a, i, out=out)` 调用顶层 multiply ufunc（即 4.1 import 进来的那个），把结果写进 `out`。

**⑤ 为什么必须显式给 `out`？**

[numpy/_core/src/umath/string_ufuncs.cpp:772-786](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L772-L786) 的 `string_multiply_resolve_descriptors` 在没有 `out`（`given_descrs[2] == NULL`）时直接报错：

```cpp
if (given_descrs[2] == NULL) {
    PyErr_SetString(
        PyExc_TypeError,
        "The 'out' kwarg is necessary when using the string multiply ufunc "
        "directly. Use numpy.strings.multiply to multiply strings without "
        "specifying 'out'.");
    ...
}
```

这段报错信息把「为什么需要 Python 包装」解释得明明白白：底层 ufunc 无法仅凭输入 dtype 推断输出宽度（宽度依赖运行时重复次数），于是要求调用方提供 `out`；而 `numpy.strings.multiply` 的职责正是替用户把 `out` 算好、建好。

#### 4.2.4 代码实践

**实践目标**：触发定长分支的 `OverflowError`，并对比 `StringDType` 输入走的是另一条分支。

**操作步骤**：

```python
import sys
import numpy as np

# ---- A. 定长 str_ 分支：触发 Python 层溢出检查 ----
a = np.array(["ab"])            # dtype '<U2'，str_len == 2
# sys.maxsize 在 64 位平台为 2**63 - 1
try:
    np.strings.multiply(a, sys.maxsize)
except OverflowError as e:
    print("str_ 分支抛出：", e)   # 预期 "Overflow encountered in string multiply"

# ---- B. StringDType 分支：走 a * i，溢出在 C 层 ----
b = np.array(["ab"], dtype=np.dtypes.StringDType())
print(b.dtype.char)             # 预期 'T'
try:
    np.strings.multiply(b, sys.maxsize)
except OverflowError as e:
    print("StringDType 分支抛出：", e)
```

**需要观察的现象**：

- A 用 `str_` 输入，`str_len == 2`，`i = sys.maxsize`。溢出判定 `2 > sys.maxsize / sys.maxsize`（即 `2 > 1`）成立，因此在 Python 层（[strings.py:204-205](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L204-L205)）抛出 `OverflowError`，**根本不会去分配数组**。
- B 因为 `dtype.char == 'T'`，函数在第 199 行就 `return a * i` 返回，绕过了 Python 层的预算与溢出检查；溢出最终由 C 层 [stringdtype_ufuncs.cpp:138-143](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/stringdtype_ufuncs.cpp#L138-L143) 检出，同样抛 `OverflowError`。

**预期结果**：两个分支都抛 `OverflowError`，但**检查位置不同**——这就是「同一种错误、两条代码路径」的差异。错误信息字符串若在不同版本略有差别，以「两侧都阻止了巨型分配」这一行为为准。

> 边界提示：若把 A 改成单字符 `np.array(["a"])`（`str_len == 1`），则 `1 > sys.maxsize / sys.maxsize` 即 `1 > 1` 为假，**不会**在 Python 层被判溢出；随后会去构造一个宽度约 `sys.maxsize` 的 `'U...'` 数组，转而在内存分配阶段失败。这条边界正好说明溢出判据是「严格大于」，见 4.3。

#### 4.2.5 小练习与答案

**练习 1**：`_multiply_dispatcher` 为什么返回 `(a,)` 而不是 `(a, i)`？如果改成 `(a, i)` 会有什么影响？

**参考答案**：dispatcher 返回的是「参与 NEP-18 `__array_function__` 覆盖的相关参数」。重复次数 `i` 通常是普通整数/整数数组，没有自定义 `__array_function__` 的必要；只把 `a` 声明为相关参数，意味着只有 `a` 的类型（如某个实现了 `__array_function__` 的字符串数组子类）能接管整个 `multiply`。若改成 `(a, i)`，则 `i` 的类型也会被询问是否要覆盖，对整数类型而言既无意义也会带来多余的开销。

**练习 2**：定长分支里，为什么 `i = np.maximum(i, 0)` 之后，溢出判定还要再用一次 `np.maximum(i, 1)`？

**参考答案**：`np.maximum(i, 0)` 是为了把负数重复次数钳成 0（语义上「重复负数次 = 空串」）。但在溢出判定里要做除法 `sys.maxsize / i`，若 `i` 取到 0 会触发除零；于是用 `np.maximum(i, 1)` 把分母至少抬到 1，既避免除零，又对 `i >= 1` 的真实情况不改变判据。

---

### 4.3 str_len 与缓冲区预计算：multiply 的溢出数学

#### 4.3.1 概念说明

定长分支里，`str_len` 与 `sys.maxsize` 共同支撑了「缓冲区预计算 + 溢出保护」。这里有两个互相牵制的目标：

1. 输出宽度 `buffersizes = a_len * i` 必须在 `int64` 范围内，否则连「乘法」本身都会无声溢出（得到错误的最大宽度，进而分配错误大小的数组）。
2. 想分配的数组总大小也不能超过地址空间。

NumPy 选择用 `sys.maxsize`（C 的 `Py_ssize_t` 上界，64 位平台上为 \(2^{63}-1\)）作为统一上限，在分配前一次性挡住。

`str_len` 在这里的角色是：**给出每个输入元素的真实字符数**，从而让「输出宽度」可以被预算。它本身是个 ufunc（`__module__` 被 `_override___module__` 改写成 `numpy.strings`），对三种字符串 dtype 语义统一（详见 u1-l2）。

#### 4.3.2 核心流程

溢出判定的数学形式。设 \(M = \text{sys.maxsize} = 2^{63}-1\)，对数组中每个位置 \(k\)，记字符数为 \(L[k]=\text{str\_len}(a)[k]\)、重复次数为 \(i[k]\)（已钳为非负）。源码里的判据是：

\[ \text{溢出} \iff \exists\, k:\ L[k] > \frac{M}{\max(i[k],\,1)} \]

由于所有量都非负，上式等价于：

\[ \exists\, k:\ L[k]\cdot \max(i[k],\,1) > M \]

对 \(i[k]\ge 1\) 的位置，\(\max(i[k],1)=i[k]\)，即 \(L[k]\cdot i[k] > M\)。这样后续 `buffersizes = a_len * i` 的每个元素都被保证 \(\le M\)，而 \(M = 2^{63}-1\) 恰好是 `int64` 的最大正值，因此该乘法不会溢出 `int64`——「先验算、再相乘」正是这段代码的安全保证。

#### 4.3.3 源码精读

[numpy/_core/strings.py:201-210](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L201-L210)（同 4.2.3 ④）。把关键三行单独标出：

```python
a_len = str_len(a)                                    # 每元素真实字符数
if np.any(a_len > sys.maxsize / np.maximum(i, 1)):    # 4.3.2 的判据
    raise OverflowError("Overflow encountered in string multiply")
buffersizes = a_len * i                               # 安全：已保证 ≤ M
```

而 `sys` 的导入在文件最顶 [numpy/_core/strings.py:6-7](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L6-L7)：

```python
import functools
import sys
```

`str_len` 本身来自 [numpy/_core/strings.py:22-58](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L22-L58) 的 `from numpy._core.umath import (... str_len ...)`，并在 [numpy/_core/strings.py:61-70](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L61-L70) 的 `_override___module__()` 里被改写 `__module__` 为 `numpy.strings`。

> 与 u2-l5 的呼应：u2-l5 总结过四条「输出 dtype 路径」。`multiply` 的定长分支属于 **A 路径**——「用 `str_len` 预算宽度，拼出定长 `out`，再交给 C 层 ufunc」。识别标志正是这里出现的 `str_len` 与 `f"{a.dtype.char}{...}"`。

#### 4.3.4 代码实践

**实践目标**：用小例子验证「输出宽度 = `str_len(a) * i` 的最大值」，并看清溢出判据的边界。

**操作步骤**：

```python
import sys
import numpy as np

# 1. 观察输出 dtype 宽度 = max(str_len * i)
a = np.array(["a", "bb", "ccc"])     # str_len = [1, 2, 3]
out = np.strings.multiply(a, 3)      # 期望宽度 max([3,6,9]) = 9
print(out)                            # ['aaa' 'bbbbbb' 'ccccccccc']
print(out.dtype, out.dtype.itemsize)  # '<U9'  36  （9 字符 × 4 字节）

# 2. 验证「严格大于」边界：str_len=1, i=M 刚好不溢出（但分配会失败）
M = sys.maxsize
a1 = np.array(["a"])                  # str_len = 1
# 判据：1 > M / M == 1  → False，Python 层不拦
print("会进入分配阶段吗？", 1 > (M / max(M, 1)))   # 预期 False

# 3. str_len=2, i=M：判据 2 > 1 为真 → Python 层拦下
print("str_len=2 会被拦吗？", 2 > (M / max(M, 1))) # 预期 True
```

**需要观察的现象**：步骤 1 中输出 dtype 的宽度恰为 `max(str_len * i)`，`itemsize` 是该宽度的 4 倍（`str_` 为 UCS4）；步骤 2、3 把 4.3.2 的判据用纯 Python 复算一遍，与源码逻辑一一对应。

**预期结果**：如上注释。步骤 2 中那行只是复算判据、**不要**真的去调 `np.strings.multiply(np.array(["a"]), M)`——否则会绕过 Python 层溢出检查、进入一次注定失败的巨型分配（见 4.2.4 的边界提示）。

#### 4.3.5 小练习与答案

**练习 1**：若把溢出判据里的 `sys.maxsize` 换成 `np.iinfo(np.int64).max`，结果会变吗？为什么？

**参考答案**：在 64 位平台上不会变。因为 `sys.maxsize`（`Py_ssize_t` 上界）与 `np.iinfo(np.int64).max` 都等于 \(2^{63}-1\)，二者数值相同，判据等价。语义上 `sys.maxsize` 更贴近「单次能分配/寻址的上界」，而 `np.iinfo(np.int64).max` 更贴近「`a_len * i` 作为 `int64` 不溢出」的上界——本场景下两者恰好重合。

**练习 2**：`str_len` 对 `bytes_`（`'S'`）和 `str_`（`'U'`）返回的都是「字符数」，但底层字节数不同。这对 `out_dtype` 的拼接有何影响？

**参考答案**：`out_dtype = f"{a.dtype.char}{buffersizes.max()}"` 用的是 `a.dtype.char`，对 `str_` 拼成 `'U...'`、对 `bytes_` 拼成 `'S...'`。`str_len` 统一返回字符数，对 `str_` 来说每个字符占 4 字节（故 `itemsize = 宽度 × 4`），对 `bytes_` 每字符 1 字节。因此「字符数」做宽度、「`dtype.char`」做类型，二者配合即可正确还原定长 dtype，无需在 `multiply` 里再关心 `//4` 的细节（那已被 `str_len` 与 dtype 体系吸收）。

## 5. 综合实践

把本讲的三块知识串起来：**追踪一次 `np.strings.multiply` 调用在两种 dtype 下的完整路径，并用源码行号佐证**。

任务步骤：

1. 准备两个等价输入：
   ```python
   import numpy as np
   s = np.array(["NumPy", "核心"])
   t = np.array(["NumPy", "核心"], dtype=np.dtypes.StringDType())
   ```
2. 对 `s`（`str_`）调用 `np.strings.multiply(s, 2)`：
   - 指出它走 [strings.py:201-210](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L201-L210) 的定长分支；
   - 手算 `str_len(s)`、`buffersizes`、`out_dtype`（提示：`'核心'` 是 2 个字符，重复 2 次得宽度 4；`'NumPy'` 重复 2 次得宽度 10；故 `out_dtype = 'U10'`）；
   - 与实际返回的 `out.dtype` 对照。
3. 对 `t`（`'T'`）调用 `np.strings.multiply(t, 2)`：
   - 指出它在 [strings.py:198-199](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L198-L199) 直接 `return a * i`，溢出检查落在 C 层 [stringdtype_ufuncs.cpp:138-143](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/stringdtype_ufuncs.cpp#L138-L143)；
   - 用 `t.dtype.char` 确认是 `'T'`。
4. 画一张「输入 dtype → Python 分支 → C 循环文件」的对照表，至少包含 `str_`、`bytes_`、`StringDType` 三行。
5. （选做）比较 `np.strings.equal` 与 `np.strings.multiply` 的类型：用 `type(...).__name__` 印证前者是 `numpy.ufunc`、后者是 `_ArrayFunctionDispatcher`，并解释这与 4.1/4.2 的结论如何呼应。

> 待本地验证：第 2 步中 `'核心'` 的字符数与最终 `out_dtype` 的确切取值，取决于你所用 Python/NumPy 对该码点的处理；以「`out_dtype` 宽度等于 `max(str_len(s) * 2)`」这一关系成立为准。

## 6. 本讲小结

- 比较六件套（`equal`/`not_equal`/`less`/`less_equal`/`greater`/`greater_equal`）与 `add` 是**直接复用** numpy 顶层 ufunc，Python 层零包装、零 dispatcher，也不在 `_override___module__` 名单里——它们能处理字符串，靠的是 C 层把循环挂到既有 ufunc 上。
- `multiply` 是这一组里**唯一的 Python 包装函数**，用 `@set_module` 改写 `__module__`、用 `@array_function_dispatch(_multiply_dispatcher)` 启用 NEP-18 分发。
- `_multiply_dispatcher` 只返回 `(a,)`，声明只有字符串数组 `a` 的类型才相关。
- `multiply` 有**双分支**：`dtype.char == 'T'`（`StringDType`）直接 `return a * i`；定长 `str_`/`bytes_` 则走 `str_len` 预算宽度 + 溢出保护 + 显式 `out` + `_multiply_ufunc`。
- 底层 multiply ufunc **无法自行推断输出宽度**，故其 `resolve_descriptors` 强制要求 `out`——这正是 `numpy.strings.multiply` 必须在 Python 层预算 `out` 的根本原因。
- 溢出判据 \(L\cdot i > \text{sys.maxsize}\) 既保护了 `a_len * i` 的 `int64` 乘法不溢出，也提前挡住了不可能成功的巨型分配。

## 7. 下一步学习建议

- **横向推进同类函数**：下一讲 u2-l7 进入查找类（`find`/`index`/`count`/`startswith`/`endswith`），它们同样走「装饰器 + 委托底层 `*_ufunc`」的套路，并引入 `MAX` 哨兵——可以与本讲的 `multiply` 对照阅读，巩固「Python 包装层」的通用模式。
- **纵向下探 C 层**：本讲反复引用的 `string_ufuncs.cpp` 与 `stringdtype_ufuncs.cpp` 将在 u3-l12、u3-l14 详讲。若想弄清「C 层如何把循环挂到既有 ufunc 上」「`StringDType` 循环与定长循环如何分工」，可提前跳读 `init_string_ufuncs` 与 `init_stringdtype_ufuncs`。
- **建议继续阅读的源码**：`numpy/_core/strings.py` 第 144–210 行（`multiply` 全家）、`numpy/_core/src/umath/string_ufuncs.cpp` 第 772–786 行（`out` 必填的报错）与第 1493–1531 行（`add`/`multiply` 循环注册）。
