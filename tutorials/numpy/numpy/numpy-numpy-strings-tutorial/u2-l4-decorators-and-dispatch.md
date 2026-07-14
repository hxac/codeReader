# 装饰器与分发机制：set_module 与 array_function_dispatch

## 1. 本讲目标

学完本讲，你应当能够：

- 解释为什么 `np.strings.center` 打印出来「来自 `numpy.strings`」而不是它真正定义的 `numpy._core.strings`；
- 说清楚 `@set_module("numpy.strings")` 这个装饰器到底改了什么、为什么需要改；
- 理解 NEP-18 的 `__array_function__` 协议，以及 `@array_function_dispatch(dispatcher)` 如何让一个普通 Python 函数变成「可以被第三方数组类型拦截」的可分发函数；
- 说清楚 `dispatcher` 函数的职责——它只是声明「哪些参数是相关参数」，而不是真正干活；
- 理解为什么 `str_len`、`isalpha` 这类 ufunc 无法用装饰器装饰，而需要 `_override___module__()` 在 import 时直接改写属性。

本讲是第 2 单元（Python 包装层）的总纲。后面 l5–l11 每一篇讲的具体函数（比较、查找、对齐、裁剪、切分……）几乎都套用本讲讲的两件套装饰器。掌握本讲，后面就是「套同一个模子、换不同实现」。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 `__module__`：函数的「身份证」

每个 Python 函数对象都有一个 `__module__` 属性，记录它「定义在哪个模块」。它影响三件事：

- `help()`、文档系统、IDE 里显示的归属；
- 异常 traceback 里函数的显示名；
- 某些工具按模块名聚合 API（例如 NumPy 用 `ARRAY_FUNCTIONS` 集合登记所有公开函数）。

`numpy.strings` 是一个**门面命名空间**（见 u1-l1），真正的实现写在私有模块 `numpy._core.strings` 里。如果不做任何处理，`np.strings.center.__module__` 会是 `numpy._core.strings`——这对用户来说暴露了内部细节，也让「`numpy.strings` 是稳定公共 API」这件事在工具层面不可识别。所以需要把 `__module__` 改写成 `numpy.strings`。

### 2.2 NEP-18 与 `__array_function__`：让别人能接管你的函数

NumPy 的很多公开函数（比如 `np.concatenate`、`np.strings.multiply`）内部并不是单纯调用一个 ufunc，而是有一段 Python 编排逻辑（算缓冲区大小、构造输出数组、再调用 ufunc）。

如果一个第三方数组库（dask、cupy、sparse……）想让 `np.strings.multiply(my_dask_array, 3)` 返回一个 dask 数组而不是触发 NumPy 的实现，它需要一种机制：**「当 NumPy 函数发现参数里有我这种类型时，把整个调用交给我」**。这就是 [NEP-18](https://numpy.org/neps/nep-0018-array-function-protocol.html) 定义的 `__array_function__` 协议。

启用这个协议的方式，就是把函数用一个专门的包装类包起来——这就是 `@array_function_dispatch`。

### 2.3 两件事，互不冲突

不要把上面两件事混为一谈：

| 机制 | 解决的问题 | 由谁负责 |
| --- | --- | --- |
| `@set_module` | 改 `__module__`（身份/显示） | `numpy/_utils/__init__.py` |
| `@array_function_dispatch` | 启用 `__array_function__` 分发（行为） | `numpy/_core/overrides.py` |

它们恰好都会写一次 `__module__`（原因后讲），但目的不同。本讲会分别拆开。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `numpy/_core/strings.py` | `numpy.strings` 的真正实现层。本讲关注其中的 `_override___module__`、`array_function_dispatch` 的 partial 重绑定、所有 `*_dispatcher` 函数，以及装饰器的叠加写法。 |
| `numpy/_core/overrides.py` | `array_function_dispatch` 装饰器的实现，以及从 `numpy._utils` 转发出来的 `set_module`。 |
| `numpy/_utils/__init__.py` | `set_module` 的真正定义。 |
| `numpy/_core/src/multiarray/arrayfunction_override.c` | `_ArrayFunctionDispatcher` 的 C 实现，是分发流程真正「干活」的地方。本讲作为补充精读。 |

## 4. 核心概念与源码讲解

### 4.1 set_module：控制函数的「模块归属」

#### 4.1.1 概念说明

`set_module(module)` 是一个极简装饰器：它接收一个模块名字符串，把被装饰函数（或类）的 `__module__` 改成这个名字，然后把原对象原样返回。它不改函数行为，只改「身份证」。

在 `numpy.strings` 里它被写成 `@set_module("numpy.strings")`，作用就是把实现层（`numpy._core.strings`）里的函数对外伪装成「来自 `numpy.strings`」。

#### 4.1.2 核心流程

```text
set_module("numpy.strings") 返回 decorator
    └─ decorator(func):
         func.__module__ = "numpy.strings"   # 只改这一个属性
         return func                          # 原样返回，不包装
```

注意它**不创建新对象**：装饰前后的 `multiply` 是同一个函数对象，只是 `__module__` 被改写了。这和后面 `array_function_dispatch`（会用一个 C 对象把函数包起来）完全不同。

#### 4.1.3 源码精读

`set_module` 真正定义在 `numpy/_utils/__init__.py`（一个刻意不依赖 NumPy 主体的私有工具模块，避免循环导入）：

[numpy/_utils/__init__.py:17-38](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_utils/__init__.py#L17-L38) —— `set_module` 的实现。核心只有一行 `func.__module__ = module`（第 36 行），其余是对「类」对象额外保存原始模块到 `_module_source` 的细节，本讲用不到。

`overrides.py` 只是把它「转发」出来，方便 `strings.py` 集中从一个地方导入：

[numpy/_core/overrides.py:11](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/overrides.py#L11) —— `from numpy._utils import set_module  # noqa: F401`，`# noqa` 是因为这个文件自己不用它，只是再导出给别的模块。

在 `strings.py` 里，`set_module` 出现在每一处只做「直接转发到 ufunc」的函数上，例如 `find`：

[numpy/_core/strings.py:255-256](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L255-L256) —— `@set_module("numpy.strings")` 紧贴 `def find(...)`。`find` 没有任何 `__array_function__` 分发逻辑，它只是把 `end=None` 转成哨兵 `MAX` 再调用底层 `_find_ufunc`，所以只需要 `set_module` 改一下归属即可。

> 一个值得注意的细节：`strings.py` 还把本模块内的 `array_function_dispatch` 这个名字**重新绑定**为一个 `functools.partial`，预先绑死了 `module='numpy.strings'`：
>
> [numpy/_core/strings.py:95-96](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L95-L96) —— `array_function_dispatch = functools.partial(array_function_dispatch, module='numpy.strings')`。
>
> 这意味着本文件里每一次写 `@array_function_dispatch(dispatcher)`，装饰器内部都会自动把 `public_api.__module__` 设成 `'numpy.strings'`。于是对于那些「同时」被两个装饰器修饰的函数（如 `multiply`），`@set_module` 与 `array_function_dispatch` 在改写 `__module__` 上是**重复的**——一种「双保险」写法。但对于 `find`、`strip`、`slice` 这类**只有** `@set_module`、没有分发的函数，`@set_module` 是唯一改归属的地方，不可省略。

#### 4.1.4 代码实践

**实践目标**：验证 `@set_module` 改写了 `__module__`，且没有创建新对象。

**操作步骤**：

```python
import numpy as np
from numpy._core import strings as impl

print(np.strings.find.__module__)          # 预期: numpy.strings
print(impl.find.__module__)                # 预期: numpy.strings（同一个对象，归属已被改写）
print(np.strings.find is impl.find)        # 预期: True（门面只是转发，不复制）
```

**预期结果**：三行分别打印 `numpy.strings`、`numpy.strings`、`True`。第三行尤其重要——它证明 `set_module` 改的是「那个唯一函数对象」的属性，门面与实现层拿到的是同一个东西。

> 说明：`__module__` 是字符串字面值 `numpy.strings`。若你的 NumPy 版本不同，归属字符串含义一致即可。内存地址不会出现在这三行里，结果稳定可复现。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `@set_module("numpy.strings")` 从 `find` 上去掉，`np.strings.find.__module__` 会变成什么？

**参考答案**：变成 `numpy._core.strings`（即 `find` 真正被 `def` 出来的模块），因为 `find` 没有 `array_function_dispatch`，partial 重绑定那条路径对它不生效。

**练习 2**：`set_module` 为什么放在 `numpy/_utils/` 而不是 `numpy/_core/`？

**参考答案**：因为 `_utils` 被设计成「不依赖 NumPy 主体、可在任何地方安全导入」的私有工具集合（见该文件开头的模块 docstring）。把这种零依赖的小工具放进 `_core` 会引入循环导入风险。

---

### 4.2 array_function_dispatch：NEP-18 `__array_function__` 分发

#### 4.2.1 概念说明

`@array_function_dispatch(dispatcher)` 把一个普通 Python 函数 `implementation` 包装成一个**分发器对象** `_ArrayFunctionDispatcher`。这个对象对外表现得就像原函数（签名、文档、名字都通过 `functools.update_wrapper` 复制过来），但每次被调用时，它会在真正执行 `implementation` 之前，先做一次「有没有人要接管」的检查。

这就是 NEP-18。`dispatcher` 参数是一个**小函数**，它和 `implementation` 签名一致，作用只有一个：根据传入的参数，返回一个「相关参数」的元组——也就是「值得去检查 `__array_function__` 的那几个参数」。

#### 4.2.2 核心流程

一次 `np.strings.multiply(a, i)` 的调用，实际跑的是 `_ArrayFunctionDispatcher.__call__`（C 层 `dispatcher_vectorcall`），大致流程：

```text
用户调用 public_api(a, i)
   │
   ▼
1. 调用 dispatcher(a, i)  →  得到 relevant_args（例如 (a,)）
   │
   ▼
2. 用 get_implementing_args_and_methods(relevant_args)
   筛选出「实现了非默认 __array_function__ 的参数」
   │
   ├── 没有任何覆盖（最常见，全是 ndarray）:
   │     ▶ 直接调用 implementation(a, i)   ← 快速路径
   │
   └── 有覆盖（例如 dask 数组）:
         ▶ 收集 types、打包 args/kwargs
         ▶ 调用该参数的 __array_function__(func, types, args, kwargs)
           （若它返回 NotImplemented，再尝试下一个；全失败才报错）
```

关键点：

- 第 2 步里的「快速路径」很重要。绝大多数调用参数都是普通 `ndarray`（它的 `__array_function__` 是「默认」的），分发器发现没有任何覆盖，就**直接**调用 `implementation`，几乎零开销。
- 只有当某个相关参数的类型**重写**了 `__array_function__` 时，才会进入慢路径，把整个调用交给那个类型。
- `implementation` 始终是「没有覆盖时的最终实现」。它被挂在分发器对象上，可以通过 `._implementation` 属性拿到。

#### 4.2.3 源码精读

装饰器本身在 `overrides.py`：

[numpy/_core/overrides.py:108-177](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/overrides.py#L108-L177) —— `array_function_dispatch` 的完整实现。它返回一个内部 `decorator(implementation)`，真正干活的是这几行：

[numpy/_core/overrides.py:164-173](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/overrides.py#L164-L173) —— 这里是包装的核心。逐行说明：

- 第 164 行 `public_api = _ArrayFunctionDispatcher(dispatcher, implementation)`：用 C 对象把 `implementation` 包起来，`_ArrayFunctionDispatcher` 来自 `numpy._core._multiarray_umath`。
- 第 165 行 `functools.update_wrapper(public_api, implementation)`：把 `__name__`、`__doc__`、`__wrapped__` 等从原函数复制到包装对象上，让它「看起来」就是原函数。
- 第 170–171 行 `if module is not None: public_api.__module__ = module`：这就是 4.1.3 里提到的「partial 重绑定」最终生效的地方——`module` 已被预先绑成 `'numpy.strings'`，于是包装对象的 `__module__` 也被设对。
- 第 173 行 `ARRAY_FUNCTIONS.add(public_api)`：把这个公开函数登记进全局集合，NumPy 用它来追踪「哪些函数受 NEP-18 约束」。

装饰器还会做一件重要的事——**校验 dispatcher 与 implementation 的签名一致**：

[numpy/_core/overrides.py:86-105](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/overrides.py#L86-L105) —— `verify_matching_signatures`：要求 dispatcher 与 implementation 的形参名、`*args`/`**kwargs`、默认值个数完全一致；并且 dispatcher 的可选参数默认值**只能是 `None`**。这是一种「防呆」：逼着你把 dispatcher 写成与主函数同形参、只是把可选默认值统一成 `None`。

真正「分发」的运行时逻辑在 C 层。下面是补充精读（不必死记，理解流程即可）：

[numpy/_core/src/multiarray/arrayfunction_override.c:515-516](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/arrayfunction_override.c#L515-L516) —— 用收到的原始参数调用 `self->relevant_arg_func`（即 dispatcher），得到 `relevant_args`。

[numpy/_core/src/multiarray/arrayfunction_override.c:527-528](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/arrayfunction_override.c#L527-L528) —— `get_implementing_args_and_methods(relevant_args, ...)`：从相关参数里挑出真正实现了 `__array_function__` 的那些。

[numpy/_core/src/multiarray/arrayfunction_override.c:578-581](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/arrayfunction_override.c#L578-L581) —— 「没有任何覆盖」的快速路径：直接 `PyObject_Vectorcall(self->default_impl, ...)`，也就是直接跑 `implementation`。这就是为什么对普通 ndarray 几乎没有分发开销。

`_implementation` 这个属性正是在 C 层注册的 getset：

[numpy/_core/src/multiarray/arrayfunction_override.c:751-753](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/arrayfunction_override.c#L751-L753) —— `{"_implementation", ...}`，让你能绕过分发、直接拿到原始实现。

#### 4.2.4 代码实践

**实践目标**：亲手验证「分发真的会发生」，并看到「直接调用 implementation 会绕过分发」。

**操作步骤**：

```python
import numpy as np

f = np.strings.multiply

# 1) 包装对象的身份
print(f.__module__)                  # 预期: numpy.strings
print(type(f).__name__)             # 预期: _ArrayFunctionDispatcher
print(f._implementation.__name__)   # 预期: multiply

# 2) 自定义一个「会接管」的数组类型
class Intercepted(np.ndarray):
    def __array_function__(self, func, types, args, kwargs):
        return ("INTERCEPTED", func.__name__)

a = np.array(["x", "y"]).view(Intercepted)

# 3) 正常情况：相关参数全是普通 ndarray，走快速路径
print(np.strings.multiply(np.array(["a", "b"]), 3))
# 预期: array(['aaa', 'bbb'], dtype='<U3')

# 4) 当 a 是会接管的类型时，整个调用被拦截，implementation 不会执行
print(np.strings.multiply(a, 3))
# 预期: ('INTERCEPTED', 'multiply')
```

**预期结果**：第 3 步正常返回重复后的字符串数组；第 4 步**不会**去算 `aaa`，而是直接返回你的拦截元组——证明调用在第 2 步流程里被交给了 `Intercepted.__array_function__`，`multiply` 的实现体根本没跑。

**需要观察的现象**：把第 4 步的 `a` 换回普通 `np.array(["x","y"])`，拦截立即消失，回到正常计算——说明分发是由「参数类型是否重写 `__array_function__`」决定的，而不是函数本身。

> 待本地验证：上述「预期结果」是基于源码流程推断的（第 4 步依赖 ndarray 子类重写 `__array_function__` 后会被识别为「非默认」）。在你本机的 NumPy 上运行一次，确认拦截元组确实出现即可。

#### 4.2.5 小练习与答案

**练习 1**：为什么说「对普通 ndarray，`array_function_dispatch` 几乎零开销」？

**参考答案**：因为 C 层在确认所有相关参数都没有重写 `__array_function__`（全是默认的 ndarray 版本）后，会走第 578–581 行的快速路径，直接 `Vectorcall(implementation)`，跳过打包 args/kwargs、构造 `types` 元组等开销。

**练习 2**：`f._implementation` 和 `f` 有什么区别？

**参考答案**：`f` 是 `_ArrayFunctionDispatcher` 包装对象，调用它会触发 NEP-18 分发；`f._implementation` 是被它包住的原始 Python 函数，调用它**不会**触发分发，直接执行实现体。测试里常用 `func._implementation(...)` 来绕过分发、只测实现逻辑（见 `test_overrides.py` 第 395 行的用法）。

---

### 4.3 dispatcher 函数：声明哪些是「相关参数」

#### 4.3.1 概念说明

dispatcher 函数是 `@array_function_dispatch(dispatcher)` 的第一个参数。它的**唯一职责**是：用与主函数相同的形参，返回一个元组，列出「值得检查 `__array_function__` 的参数」。

它不计算结果、不校验类型、不做任何业务逻辑——纯粹是一份「相关参数声明」。把它单独拎出来，是因为：检查一个参数是否实现 `__array_function__` 是有成本的，NumPy 不想对每一个参数（包括像 `width=9`、`fillchar=' '` 这样的标量）都做检查，于是让你显式声明「只有这几个参数有可能是别人的数组类型」。

#### 4.3.2 核心流程

dispatcher 的写法是一个高度统一的套路：

```python
def _xxx_dispatcher(a, <与主函数同名的其余形参, 默认值全为 None>):
    return (<相关参数>,)          # 通常就是那几个「可能是数组」的参数
```

规则（由 `verify_matching_signatures` 强制）：

1. 形参名、顺序、`*args/**kwargs` 必须与主函数完全一致；
2. 主函数里有默认值的参数，在 dispatcher 里也必须有默认值，且**只能是 `None`**；
3. `return` 的元组里只放「需要被检查」的参数。

#### 4.3.3 源码精读

`strings.py` 里有一整套 `*_dispatcher`，可以列成一张对照表体会「相关参数」的差异：

| dispatcher | 源码位置 | 返回值 | 含义 |
| --- | --- | --- | --- |
| `_multiply_dispatcher(a, i)` | [strings.py:144-145](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L144-L145) | `(a,)` | 只检查字符串数组 `a`；`i` 是整数，永远不可能是别人的数组类型 |
| `_mod_dispatcher(a, values)` | [strings.py:213-214](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L213-L214) | `(a, values)` | `a` 和 `values` 都可能是数组，两个都检查 |
| `_join_dispatcher(sep, seq)` | [strings.py:1354-1355](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1354-L1355) | `(sep, seq)` | 分隔符和被连接序列都可能是数组 |
| `_just_dispatcher(a, width, fillchar=None)` | [strings.py:687-688](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L687-L688) | `(a,)` | 只检查 `a`；`width`/`fillchar` 是标量 |
| `_zfill_dispatcher(a, width)` | [strings.py:890-891](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L890-L891) | `(a,)` | 同上，`width` 是整数 |
| `_code_dispatcher(a, encoding=None, errors=None)` | [strings.py:531-532](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L531-L532) | `(a,)` | `encode`/`decode` 共用，只检查 `a` |
| `_unary_op_dispatcher(a)` | [strings.py:1080-1081](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1080-L1081) | `(a,)` | `upper`/`lower`/`swapcase`/`capitalize`/`title` 五个函数共用 |
| `_replace_dispatcher(a, old, new, count=None)` | [strings.py:1285-1286](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1285-L1286) | `(a,)` | 只检查 `a`；`old/new/count` 视为标量 |
| `_partition_dispatcher(a, sep)` | [strings.py:1532-1533](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1532-L1533) | `(a,)` | 只检查 `a`（注意：`sep` 没被列入） |
| `_translate_dispatcher(a, table, deletechars=None)` | [strings.py:1675-1676](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1675-L1676) | `(a,)` | 只检查 `a` |
| `_split_dispatcher(a, sep=None, maxsplit=None)` | [strings.py:1395-1396](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1395-L1396) | `(a,)` | 私有 `_split`/`_rsplit` 共用 |
| `_splitlines_dispatcher(a, keepends=None)` | [strings.py:1490-1491](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1490-L1491) | `(a,)` | 私有 `_splitlines` 用 |

读这张表能发现一个规律：**绝大多数 dispatcher 只返回 `(a,)`**——因为字符串函数里真正「可能大、可能是别人家数组类型」的，基本上只有那一个主字符串数组 `a`；其余的 `width`、`fillchar`、`sep`、`count`、`i` 等要么是标量，要么语义上是「配置」而非「数据」。少数像 `_mod_dispatcher`、`_join_dispatcher` 把第二个参数也列入，是因为那两个参数在语义上同样是「数据数组」，确实可能来自第三方库。

把 `multiply` 与 `mod` 的装饰放在一起对比，能最直观地看到 dispatcher 的作用：

[numpy/_core/strings.py:148-150](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L148-L150) —— `multiply` 的两层装饰。dispatcher 是 `_multiply_dispatcher`，只声明 `a` 相关。

[numpy/_core/strings.py:217-219](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L217-L219) —— `mod` 的两层装饰。dispatcher 是 `_mod_dispatcher`，声明 `a` 与 `values` 都相关。

> 设计直觉：dispatcher 返回什么，直接决定了「第三方数组类型出现在哪个位置才能接管这个函数」。把 `i` 排除在 `_multiply_dispatcher` 之外，意味着即使你把一个实现了 `__array_function__` 的整数数组当作 `i` 传进来，NumPy 也**不会**因为 `i` 而触发分发（只会因为 `a` 触发）。

#### 4.3.4 代码实践

**实践目标**：通过对比 `multiply` 与 `mod` 的 dispatcher，验证「相关参数」集合不同，会导致分发的触发条件不同。

**操作步骤**（源码阅读型实践）：

1. 打开 `numpy/_core/strings.py`，定位 4.3.3 表中列出的 `_multiply_dispatcher`（144 行）与 `_mod_dispatcher`（213 行）。
2. 观察：`_multiply_dispatcher(a, i)` 返回 `(a,)`；`_mod_dispatcher(a, values)` 返回 `(a, values)`。
3. 据此推断：对一个把 `__array_function__` 重写放在「第二个参数」上的调用，`multiply` 不会拦截，`mod` 会拦截。

**运行验证**（可选，进一步确认推断）：

```python
import numpy as np

class Hidden(np.ndarray):
    def __array_function__(self, func, types, args, kwargs):
        return "CAUGHT"

# 制造一个会接管的「数据数组」，放在不同位置
data = np.array([1, 2]).view(Hidden)

# multiply 只认 a 为相关参数；这里 a 是普通数组、i 才是 Hidden，
# 由于 i 不在 relevant_args 里，分发不会因 i 触发：
# （注意：下面这行会进入 multiply 实现体，实现体里对 i 的数值运算
#   反而可能触发其它函数的分发；仅据此说明 _multiply_dispatcher 本身
#   只声明了 a。更干净的做法见下方「源码结论」。）
```

**需要观察的现象 / 预期结果**：由于 `multiply` 实现体内部会对 `i` 做数值运算（`np.maximum(i, 0)` 等），直接用上面的 `Hidden` 当 `i` 会在实现体内部触发别处的拦截，现象会比较混乱。因此**推荐以「源码阅读」作为本实践的主要结论**：

- 结论 A：`_multiply_dispatcher` 只返回 `(a,)`，所以 `multiply` 的分发**只**由 `a` 的类型决定。
- 结论 B：`_mod_dispatcher` 返回 `(a, values)`，所以 `mod` 的分发可由 `a` **或** `values` 的类型触发。

> 待本地验证：如果你想用运行时干净地观察「相关参数」差异，最稳妥的办法是给 `a`（两个函数都列为相关参数的那个）套上 `__array_function__`，确认两者都会被拦截；再阅读源码理解「`i`/`values` 的差异」。直接拿 `Hidden` 当 `i` 会混入实现体内部其它分发的副作用，不建议作为主证据。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_just_dispatcher(a, width, fillchar=None)` 不把 `fillchar` 列入返回元组？`fillchar` 难道不能是一个数组吗？

**参考答案**：语义上 `fillchar` 是「一个填充字符」（实现里还强制 `str_len(fillchar) == 1`），它是个标量配置，不会被设计成「来自第三方库的大数组」。把它排除在相关参数外，避免对它做无意义的 `__array_function__` 检查。

**练习 2**：`verify_matching_signatures` 为什么要求 dispatcher 的可选参数默认值只能是 `None`？

**参考答案**：因为 dispatcher 不参与业务，它的默认值没有业务含义；统一规定成 `None` 是一种「占位」，既满足「与主函数默认值个数一致」的签名检查，又避免在 dispatcher 里写进真实业务默认值（那会让人误以为 dispatcher 也参与逻辑）。主函数的真实默认值（如 `center` 的 `fillchar=' '`）保留在 `implementation` 自己的签名里。

---

### 4.4 _override___module__：统一裸 ufunc 的模块归属

#### 4.4.1 概念说明

`strings.py` 里有两类「公开符号」：

1. **Python 函数**（`multiply`、`find`、`center`……）：用 `def` 写出来，可以用 `@set_module` / `@array_function_dispatch` 装饰。
2. **直接复用的 ufunc**（`equal`、`add`、`str_len`、`isalpha`……）：它们是从 `numpy._core.umath` 导入的 C 对象，在 C 扩展加载时就已经创建好，**不是** Python `def` 出来的。

问题在于：这些 ufunc 的 `__module__` 在 C 层创建时就被设成了 `numpy._core.umath`，而 `@set_module` 是装饰器——装饰器只能作用于「正在 `def` 的那一刻」的对象，无法回头去装饰一个早就存在、且从别处导入的对象。所以需要一个「在 import 时直接改属性」的手段，这就是模块级函数 `_override___module__()`。

#### 4.4.2 核心流程

```text
strings.py 被 import
   │
   ├─ 从 numpy._core.umath 导入 str_len / isalpha / ...（一批 ufunc）
   │
   ├─ 定义 _override___module__():
   │     for ufunc in [str_len, isalpha, ...]:
   │         ufunc.__module__  = "numpy.strings"   # 改身份证
   │         ufunc.__qualname__ = ufunc.__name__   # 顺便抹掉限定名前缀
   │
   └─ 立即调用 _override___module__()   # 模块级语句，import 时就执行
```

注意它直接**就地修改**这些 ufunc 对象的属性。因为这些 ufunc 是进程内单例，改一次就全局生效——从此无论谁导入，`str_len.__module__` 都是 `numpy.strings`。

#### 4.4.3 源码精读

[numpy/_core/strings.py:61-67](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L61-L67) —— `_override___module__` 的定义。它枚举了 10 个「判断类 + 长度」的 ufunc（`isalnum/isalpha/isdecimal/isdigit/islower/isnumeric/isspace/istitle/isupper/str_len`），把它们的 `__module__` 改成 `"numpy.strings"`，并把 `__qualname__` 设成与 `__name__` 相同。

[numpy/_core/strings.py:70](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L70) —— 模块加载时立即调用 `_override___module__()`。这是一条「裸」的模块级语句，没有 `if __name__ == ...` 的保护，所以每次 import 该模块都会执行一次（幂等：重复赋同一个值无副作用）。

> 为什么这 10 个 ufunc 要单独处理，而 `equal`/`add`/`multiply` 等不需要？看 `strings.py` 顶部的导入就知道：`equal`、`add` 等比较/拼接 ufunc 是直接作为公开名导出的（在 `__all__` 里），它们的 `__module__` 是否被改对，本讲不再展开；而被 `_override___module__` 列入清单的，是那些在 u1-l2 里提到过的「逐元素判断 + 长度」函数，它们需要以 `numpy.strings` 的面貌对外。判断一个 ufunc 是否已被改写，最直接的办法就是打印它的 `__module__`（见下方实践）。

#### 4.4.4 代码实践

**实践目标**：验证 `_override___module__` 已把这批 ufunc 的 `__module__` 改写成 `numpy.strings`。

**操作步骤**：

```python
import numpy as np

for name in ["str_len", "isalpha", "isdigit", "isspace", "isalnum",
             "islower", "isupper", "istitle", "isdecimal", "isnumeric"]:
    ufunc = getattr(np.strings, name)
    print(f"{name:10s} __module__={ufunc.__module__}  "
          f"type={type(ufunc).__name__}")
```

**预期结果**：每一行的 `__module__` 都是 `numpy.strings`，`type` 都是 `numpy.ufunc`。这印证了它们是 C 层 ufunc（不是 Python 函数），且 `__module__` 已被 `_override___module__` 改写。

**需要观察的现象**：对照 `numpy/_core/strings.py` 第 62–65 行那份清单，确认打印出的 10 个名字与清单完全一致；再打印一个**不在**清单里的符号（例如 `np.strings.equal`），观察它的 `__module__` 是否也是 `numpy.strings`，并思考它是通过哪条路径（装饰器 / 别的改写）获得这个归属的。

> 待本地验证：`equal` 等比较 ufunc 的 `__module__` 在不同 NumPy 版本可能略有差异，以你本机实际打印结果为准。

#### 4.4.5 小练习与答案

**练习 1**：为什么不能用 `@set_module("numpy.strings")` 去装饰 `str_len`？

**参考答案**：因为 `str_len` 是从 `numpy._core.umath` 导入的、在 C 扩展加载时就已经创建好的 ufunc 对象。装饰器语法 `@set_module(...)` 只能作用于「在本模块里 `def` 出来的那一刻」的对象；对一个导入进来的现成对象，没有「def 的那一刻」可挂靠，所以只能在 import 后用赋值语句直接改它的 `__module__`。

**练习 2**：`_override___module__()` 为什么要在模块加载时「立即调用」一次，而不是写成函数等别人来调？

**参考答案**：因为这个改写是「为了让 `numpy.strings` 这个命名空间对外自洽」的必要步骤，必须在符号被导出、被用户访问之前就完成。把它放在模块级、import 时立即执行，能保证任何方式导入 `numpy.strings` 时，这些 ufunc 的归属都已经是正确的，无需用户额外调用。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成下面这个「给一个新函数穿好两件套」的任务。

**任务背景**：假设你要往 `numpy.strings` 里新增一个（虚构的）函数 `mirror(a)`——它把每个字符串元素原地做「大小写翻转并重复一次」。请你仿照 `strings.py` 里 `swapcase` 的写法，写出它**应该**长什么样的「骨架」，并解释每一处装饰器/分发器的作用。

**要求**：

1. 写出 `_mirror_dispatcher`，让它只声明 `a` 为相关参数（参考 `_unary_op_dispatcher`）。
2. 写出 `mirror` 的装饰（`@set_module("numpy.strings")` + `@array_function_dispatch(_mirror_dispatcher)`），实现体里调用 `_vec_string(a, a.dtype, 'swapcase')` 之后再重复一次即可（不必真正可用，重在结构）。
3. 用文字回答：
   - 为什么 `mirror` 需要 `@array_function_dispatch`，而 4.1 里讲到的 `find` 不需要？
   - 如果把 `_mirror_dispatcher` 的返回值从 `(a,)` 改成 `()`（空元组），会发生什么？
   - `mirror` 的 `__module__` 会被设成什么？是哪一条语句最终生效的（`@set_module` 还是 partial 重绑定）？

**参考骨架（示例代码，非项目原有代码）**：

```python
# 示例代码：仅用于说明结构，不是 numpy 真实函数
def _mirror_dispatcher(a):
    return (a,)

@set_module("numpy.strings")
@array_function_dispatch(_mirror_dispatcher)
def mirror(a):
    """(虚构) 每个元素大小写翻转后再重复一次。"""
    a_arr = np.asarray(a)
    flipped = _vec_string(a_arr, a_arr.dtype, 'swapcase')
    return _vec_string(flipped, flipped.dtype, '__mul__', (2,))
```

**参考答案要点**：

- `mirror` 在实现体里做了 Python 层的编排（先 `swapcase` 再 `__mul__`），不是「一次性转发到单个 ufunc」，所以需要 `@array_function_dispatch`，让第三方数组类型能接管**整段**编排；`find` 只是把 `end=None` 换成哨兵再调用单个 ufunc，转发路径单一，靠 ufunc 自身的 `__array_ufunc__` 协议即可，不必再套一层分发。
- 把返回值改成 `()` 后，`mirror` 不再检查任何参数的 `__array_function__`，等价于「对所有类型都直接跑实现体」——它便失去了 NEP-18 分发能力，第三方数组类型无法接管它。
- `mirror.__module__` 是 `numpy.strings`。因为 partial 重绑定（4.1.3）使 `array_function_dispatch` 内部已执行 `public_api.__module__ = module`（overrides.py 第 170–171 行）；外层 `@set_module` 又设了一次相同值，两者一致，互为双保险。

## 6. 本讲小结

- `numpy.strings` 里每个公开函数都靠两件套装饰器对外「定型」：`@set_module` 管「身份证」（`__module__`），`@array_function_dispatch` 管「行为」（NEP-18 分发）。
- `set_module` 极简：只把 `func.__module__` 改成指定字符串，不创建新对象；它定义在零依赖的 `numpy/_utils/__init__.py`。
- `array_function_dispatch(dispatcher)` 用 C 对象 `_ArrayFunctionDispatcher` 把实现函数包起来：调用时先跑 `dispatcher` 拿「相关参数」，检查其中有没有重写 `__array_function__` 的类型——有则交给它，没有则走快速路径直接跑实现。原始实现可通过 `._implementation` 取回。
- `dispatcher` 函数只负责「声明哪些参数相关」，不参与业务；其签名必须与主函数一致、可选默认值只能是 `None`（由 `verify_matching_signatures` 强制）。绝大多数字符串函数的 dispatcher 只返回 `(a,)`。
- `strings.py` 用 `functools.partial` 把本模块的 `array_function_dispatch` 预先绑定了 `module='numpy.strings'`，使得每个被分发的函数 `__module__` 自动设对，与 `@set_module` 形成双保险。
- `_override___module__()` 处理的是「无法用装饰器装饰的裸 ufunc」（如 `str_len`、`isalpha`）：在 import 时直接改写它们的 `__module__` 与 `__qualname__`。

## 7. 下一步学习建议

本讲把「包装层是怎么搭起来的」讲透了。接下来建议：

- 进入 **u2-l5（通用辅助函数与 dtype 分发套路）**，看 `_get_num_chars`、`_to_bytes_or_str_array`、`_clean_args` 这三个辅助函数如何被各处复用——它们是理解后续具体函数实现的前提。
- 之后按 **u2-l6 → u2-l11** 逐类精读：比较/拼接（l6）、查找（l7）、大小写与 `_vec_string`（l8）、对齐填充（l9）、裁剪（l10）、切分切片（l11）。读每一篇时，先定位它的 `*_dispatcher` 和装饰器叠加方式，再进入实现体——你会发现套路完全一致。
- 想深入分发的运行时细节，可直读 `numpy/_core/src/multiarray/arrayfunction_override.c` 的 `dispatcher_vectorcall`（本讲 4.2.3 已带读关键行）。
