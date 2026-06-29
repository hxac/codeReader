# LowLevelCallable 与 C 回调机制

## 1. 本讲目标

SciPy 里很多数值例程（数值积分 `quad`、图像滤波 `generic_filter`、几何变换 `geometric_transform` 等）的底层是用 C 或 Fortran 写的，它们在工作循环里需要**反复调用用户提供的那个函数**。本讲要回答三个问题：

1. 为什么把一个普通 Python 函数传给这些底层例程会很慢？`LowLevelCallable` 又是怎么解决这个性能问题的？
2. `LowLevelCallable` 支持哪几种「绑定方式」（ctypes / cffi / PyCapsule / Cython），它们的统一抽象是什么？
3. 一个 `LowLevelCallable` 从 Python 侧传进去，到底是怎么被 C/Fortran 底层代码「认出来并直接当 C 函数指针调用」的？

学完后你应该能够：读懂 `scipy/_lib/_ccallback.py` 和 `_ccallback_c.pyx` 的实现，理解 `ccallback.h` 里 `ccallback_prepare`/`ccallback_release` 的「thunk 分发」设计，并能动手用 ctypes 构造一个 `LowLevelCallable` 交给 `quad` 使用。

## 2. 前置知识

- **回调（callback）**：一段代码（A）在运行时把「要做什么」交给另一段代码（B）决定。比如 `quad` 不知道你要积分什么函数，于是它定义「给我一个能算 `f(x)` 的东西，我负责循环求积」——你传进去的函数就是回调。
- **Python 对象的开销**：在 Python 里，`f(1.0)` 不是一次简单的函数跳转。解释器要把 `1.0` 包装成 `PyFloatObject`、查方法表、调用、再把返回值转换回 `double`，全程还要持有 **GIL**（全局解释器锁）。这套机制灵活，但单次开销在微秒级。
- **C 函数指针**：C 语言里一个函数就是一块内存地址，调用它只是「跳到那个地址执行」，开销在纳秒级，无需创建对象、无需 GIL。
- **GIL（Global Interpreter Lock）**：CPython 的全局锁，任意时刻只有一个线程能执行 Python 字节码。底层 C 代码在调用 Python 回调前必须先「拿回 GIL」。
- **ctypes / cffi**：Python 标准库 `ctypes` 和第三方库 `cffi` 都能让你在 Python 里拿到「C 函数的地址」并描述它的签名。
- **PyCapsule**：CPython 提供的一种「装着一个 `void*` 指针」的轻量对象，常用于跨模块传递 C 指针。Cython 编译的模块会把导出的 C 函数放进名为 `__pyx_capi__` 的 PyCapsule 字典里。
- 本讲承接 [u2-l1](u2-l1-lib-util-helpers.md) 提到的 `_lib` 私有共享工具箱：`LowLevelCallable` 正是 `_lib` 里「被多个子包共同依赖」的公共抽象，因此它被提升到顶层 `scipy` 命名空间。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [scipy/_lib/_ccallback.py](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_ccallback.py) | 纯 Python 侧的 `LowLevelCallable` 类，把 ctypes/cffi/PyCapsule 三种来源统一成一个对象 |
| [scipy/_lib/_ccallback_c.pyx](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_ccallback_c.pyx) | Cython 桥接层 `_ccallback_c`，提供 `get_raw_capsule`/`get_capsule_signature`/`check_capsule` 等「操作 PyCapsule」的底层能力 |
| [scipy/_lib/src/ccallback.h](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/src/ccallback.h) | C 头文件，定义 `ccallback_t` 结构与 `ccallback_prepare`/`ccallback_release`/`ccallback_obtain` 三件套，是「thunk 分发」的核心 |
| [scipy/_lib/src/_test_ccallback.c](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/src/_test_ccallback.c) | 官方「最佳实践范例」与测试，演示如何手写 entry-point + thunk |
| [scipy/integrate/__quadpack.h](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/integrate/__quadpack.h) | `quad` 的 C 侧实现，**真实消费** `LowLevelCallable` 的范例：声明签名表、调 `ccallback_prepare`、跑 Fortran QUADPACK |
| [scipy/_lib/tests/test_ccallback.py](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/tests/test_ccallback.py) | 四种绑定方式 × 三种 caller 的交叉测试，是理解用法最好的入口 |

---

## 4. 核心概念与源码讲解

### 4.1 为什么需要 LowLevelCallable：从「回调开销」说起

#### 4.1.1 概念说明

设想你调用 `scipy.integrate.quad(math.sin, 0, math.pi)` 求积分。`quad` 的核心算法来自 Fortran 库 **QUADPACK**，它在一个循环里自适应地选取大量采样点 `x_i`，每取一个点就要算一次 `f(x_i)`。

问题在于：QUADPACK 是 Fortran/C 代码，而 `math.sin` 是 Python 可调用对象。每一次「C 调 Python」都要做下面这些事：

1. 暂时让出并行执行资格，**拿回 GIL**；
2. 为输入 `x_i` 创建一个 `PyFloatObject`；
3. 通过 `PyObject_CallFunction` 触发 Python 调用；
4. 把返回的 Python 对象**转回** C 的 `double`；
5. 释放 GIL。

这个「打包—调用—拆包」的过程，单次开销远大于一次纯 C 函数调用。当 `quad` 对一个函数求值成千上万次时，**大部分时间都花在 Python/C 的胶水层上，而不是积分算法本身**。

`LowLevelCallable` 的思路很简单：**如果用户的回调本身就是一段编译好的 C 代码（一个函数指针），那就别走 Python，直接用这个指针当 C 函数调用**。这样每次求值只是一次普通的 C 函数跳转，没有对象创建、没有 GIL。

> 直觉：普通 Python 回调像是「每次都要打电话（拿 GIL、造对象）请 Python 帮忙算」；`LowLevelCallable` 像是「直接把工人（C 函数指针）派到现场，自己干，不用打电话」。

#### 4.1.2 核心流程

底层例程面对回调时，统一采用「**thunk 分发**」模式（这套模式在 [scipy/_lib/src/_test_ccallback.c:10-35](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/src/_test_ccallback.c#L10-L35) 的注释里被明确称为 best-practices）：

1. **入口函数（entry point）**：从 Python 接收回调对象，调用 `ccallback_prepare` 把它「解析」成一个 `ccallback_t` 结构（结构里记录了「是 C 函数还是 Python 函数」「函数指针在哪」「用户数据指针在哪」）。
2. **第三方库循环**：在 `Py_BEGIN_ALLOW_THREADS`/`Py_END_ALLOW_THREADS` 之间（即**释放 GIL** 的状态下）调用 Fortran/C 库代码。
3. **thunk（转换桩）**：第三方库期望的回调签名是固定的（比如 `double(double, int*, void*)`）。SciPy 写一个签名匹配的 thunk，由它来判断——
   - 如果用户给的是 **C 函数指针**：thunk 直接以 C 方式调用，**全程无需 GIL**；
   - 如果用户给的是 **Python 函数**：thunk 拿回 GIL，走「打包—调用—拆包」流程。
4. **收尾**：调用 `ccallback_release` 释放引用。

伪代码如下：

```text
def entry_point(user_callback, x):
    prepare(callback, user_callback)        # 解析成 ccallback_t
    with nogil:                              # 释放 GIL，跑底层库
        result = fortran_library(x, thunk, &callback)
    release(callback)
    return result

cdef double thunk(a, err, data):            # 第三方库期望的固定签名
    cb = <ccallback_t*> data
    if cb.c_function != NULL:               # 用户给了 C 指针 → 直接调，无 GIL
        return cb.c_function(a, err, cb.user_data)
    else:                                    # 用户给了 Python 函数 → 拿 GIL 再调
        with gil: return float(cb.py_function(a))
```

关键收益：**当用户提供 C 函数指针时，整个求值循环完全绕开 Python**，GIL 也被释放，因而既快又能在多线程下并行。

#### 4.1.3 源码精读

这个 thunk 的真实 Cython 版本就在 `_ccallback_c.pyx` 里（它既是桥接层，也是教学用的范例）：

[scipy/_lib/_ccallback_c.pyx:130-157](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_ccallback_c.pyx#L130-L157) —— `test_thunk_cython`：判断 `callback.c_function` 是否为空，分别走「直接调 C 指针（nogil）」或「拿 GIL 调 Python 函数」两条路；这正是上面伪代码的真实实现。

而对应的 C 版范例（更完整地展示了 GIL 处理）在：

[scipy/_lib/src/_test_ccallback.c:76-124](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/src/_test_ccallback.c#L76-L124) —— `test_thunk_simple`：Python 分支里用 `PyGILState_Ensure()`/`PyGILState_Release()` 拿回 GIL 再调用；C 分支则按 `signature->value` 选择不同的函数指针签名直接调用，全程不碰 GIL。

#### 4.1.4 代码实践

**实践目标**：直观感受「C 调 Python」的开销，建立对性能问题的感性认识。

**操作步骤**（源码阅读型 + 可选运行）：

1. 打开 [scipy/_lib/src/_test_ccallback.c:76-107](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/src/_test_ccallback.c#L76-L107)，数一数「Python 分支」里为了算一次 `f(a)` 创建/释放了多少个 `PyObject`、调用了几次 `PyGILState_*`。
2. （可选运行）写一段脚本，让 `quad` 分别积分普通 Python 函数和一个 `LowLevelCallable`，用 `timeit` 对比：

```python
# 示例代码：对比普通 Python 回调与 LowLevelCallable 的耗时
import ctypes, ctypes.util, math, timeit
import numpy as np
from scipy import integrate, LowLevelCallable

# 取到 libm 里的 sin（一个真实的 C 函数指针）
lib = ctypes.CDLL(ctypes.util.find_library('m') or 'libm.so')
sin_c = lib.sin
sin_c.restype = ctypes.c_double
sin_c.argtypes = (ctypes.c_double,)

llc = LowLevelCallable(sin_c)          # 低层回调：double(double)
n_eval = 200
t_c  = timeit.timeit(lambda: integrate.quad(llc, 0, np.pi), number=n_eval)
t_py = timeit.timeit(lambda: integrate.quad(math.sin, 0, np.pi), number=n_eval)
print(f"LowLevelCallable: {t_c:.3f}s   python: {t_py:.3f}s")
```

**需要观察的现象**：`LowLevelCallable` 路径通常更快（具体倍数**待本地验证**，与硬件、BLAS 线程、求值次数有关）。

**预期结果**：两条路径算出的积分值都应接近 `2.0`（`∫₀^π sin x dx = 2`）；耗时上 LowLevelCallable 一般更省。

#### 4.1.5 小练习与答案

**练习 1**：为什么 thunk 在「C 分支」不需要拿 GIL，而「Python 分支」必须拿？

**答案**：C 分支调的是纯 C 函数指针，既不创建 Python 对象、也不执行 Python 字节码，因此不需要 GIL；Python 分支要调用 Python 可调用对象、创建/解析 `PyObject`，这些操作 CPython 要求必须持有 GIL 才能进行。

**练习 2**：把一个普通 `def f(x): return x*x` 传给 `quad`，它在 thunk 里走的是哪条分支？

**答案**：Python 分支。`ccallback_prepare` 发现它是个 `PyCallable` 而非 `LowLevelCallable`/PyCapsule，于是把指针存在 `callback->py_function`，`c_function` 留空，thunk 据此走 Python 分支。

---

### 4.2 LowLevelCallable 类与三种绑定方式

#### 4.2.1 概念说明

`LowLevelCallable` 是一个**纯 Python 类**，它的职责是：**把「一个 C 函数指针 + 可选的用户数据指针 + 签名」打包成一个不可变对象**，让下游的 C 代码能从中取出原始指针。

它支持的「函数来源」有四种，但本质上归为三类（PyCapsule 是统一落点）：

| 来源 | 如何拿到指针 | 签名从哪来 |
| --- | --- | --- |
| **ctypes** 函数指针 | `ctypes.cast(func, c_void_p).value` 得到整数地址 | 由 `restype`/`argtypes` 自动拼出，如 `"double (double)"` |
| **cffi** 函数指针 | `ffi.cast('uintptr_t', func)` 得到地址 | 由 `ffi.typeof(func)` 得到，如 `"double (*)(double, void *)"` |
| **PyCapsule** | 直接就是指针容器 | capsule 的 **name 字段**就是签名串，如 `"double (double, void *)"` |
| **Cython** 导出函数 | 经 `from_cython` 取 `module.__pyx_capi__[name]`（一个 PyCapsule） | 同 PyCapsule，name 是 Cython 自动生成的签名 |

之所以要支持这么多来源，是因为用户拿到 C 函数指针的途径各不相同：标准库只有 ctypes；想跨语言互操作可能用 cffi；用 Cython 写扩展则直接有 `__pyx_capi__`。`LowLevelCallable` 把这些异构来源「归一」成一个对象。

#### 4.2.2 核心流程

`LowLevelCallable.__new__` 做两件事：① 调 `_parse_callback` 把「异构来源」解析成一个**归一化的 PyCapsule**（胶囊里装指针、name 装签名、context 装用户数据）；② 把 `(归一化胶囊, 原始function, 原始user_data)` 存进一个 `tuple`。注意它**继承自 `tuple`**——这是个精心设计的技巧：C 侧的 `ccallback_prepare` 可以直接用 `PyTuple_GetItem(obj, 0)` 取出那个归一化胶囊，而无需关心 Python 侧的属性查找。

```
LowLevelCallable(func, user_data=None, signature=None)
        │
        ▼
_parse_callback(func, user_data, signature)
        │  按 isinstance 分派：
        ├── 是 ctypes PyCFuncPtr？ → _get_ctypes_func → (地址, 签名)
        ├── 是 cffi CData？         → _get_cffi_func  → (地址, 签名)
        ├── 是 PyCapsule？          → 原样，name 当签名
        └── 否则报错
        │
        ▼
_ccallback_c.get_raw_capsule(地址, 签名, 用户数据上下文)
        │
        ▼
返回一个新 PyCapsule（指针=地址, name=签名串, context=user_data）
        │
        ▼
tuple.__new__(LowLevelCallable, (归一化胶囊, func, user_data))
```

#### 4.2.3 源码精读

类的定义与不可变设计在 [scipy/_lib/_ccallback.py:26-108](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_ccallback.py#L26-L108)。几个要点：

- [scipy/_lib/_ccallback.py:102](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_ccallback.py#L102)：`__slots__ = ()` 让对象不可变（配合继承 `tuple`）。
- [scipy/_lib/_ccallback.py:104-108](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_ccallback.py#L104-L108)：`__new__` 里注释「We need to hold a reference to the function & user data, to prevent them going out of scope」——这是 ctypes/cffi 指针的生命周期关键：**底层 C 指针本身不持有引用，一旦原始 ctypes/cffi 对象被回收，指针就悬空**，所以必须把它们一起存进 tuple 保活。

归一化分派逻辑在 [scipy/_lib/_ccallback.py:155-183](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_ccallback.py#L155-L183) —— `_parse_callback`：依次用 `isinstance` 判断 PyCFuncPtr / CData / PyCapsule，最后用 `_ccallback_c.get_raw_capsule` 产出归一化胶囊。`user_data` 同样按 ctypes/cffi/PyCapsule 三类分派（[L171-L181](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_ccallback.py#L171-L181)）。

Cython 来源的便捷构造在 [scipy/_lib/_ccallback.py:128-153](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_ccallback.py#L128-L153) —— `from_cython`：从 `module.__pyx_capi__[name]` 取出 Cython 导出的 PyCapsule，再交给普通的 `__new__`。

ctypes 签名的自动拼接在 [scipy/_lib/_ccallback.py:190-204](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_ccallback.py#L190-L204)（`_get_ctypes_func`）与 [scipy/_lib/_ccallback.py:207-226](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_ccallback.py#L207-L226)（`_typename_from_ctypes`）：后者把 `c_double`→`"double"`、`c_void_p`→`"void *"`、`LP_c_int`→`"int *"`，再把 `restype` 和 `argtypes` 拼成 `"double (double, int *, void *)"` 这样的签名串。

> 一句话：**ctypes/cffi 是「地址 + 我替你猜签名」，PyCapsule 是「地址和签名都现成装在一起」**，`_parse_callback` 把它们统一成后者。

#### 4.2.4 代码实践

**实践目标**：亲手用四种方式构造出等价的 `LowLevelCallable`，理解它们的统一性。

**操作步骤**（源码阅读型）：

1. 阅读 [scipy/_lib/tests/test_ccallback.py:57-71](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/tests/test_ccallback.py#L57-L71) 的 `FUNCS` 字典——同一个「加 1」函数被分别以 `capsule`/`cython`/`ctypes`/`cffi` 四种方式构造。
2. 注意它们最终都通过 `LowLevelCallable(func, user_data)` 走同一条 `_parse_callback`（[test_ccallback.py:102-103](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/tests/test_ccallback.py#L102-L103)），再用同一个 `caller` 调用——这印证了「四源归一」。

**需要观察的现象**：无论哪种来源，`caller(func, 1.0)` 都返回 `2.0`，`caller(func2, 1.0)`（带 `user_data=2.0`）都返回 `3.0`。

**预期结果**：四种构造方式在行为上完全等价，区别只在「指针和签名是怎么来的」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `LowLevelCallable` 要继承 `tuple` 而不是普通 `object`？

**答案**：为了让 C 侧能以「零成本、稳定」的方式取出归一化胶囊——`PyTuple_GetItem(obj, 0)`。`tuple` 是 CPython 里布局最稳定的内置类型之一，比属性查找（`tp_getattro`）更快也更可控；同时 `__slots__=()` + 继承 `tuple` 还顺带得到不可变性。

**练习 2**：如果构造 `LowLevelCallable` 后，把传入的 ctypes 函数指针对象从作用域里删掉，会发生什么？

**答案**：会埋下「悬空指针」隐患。原始 ctypes/cffi 对象被回收后，其底层函数地址可能失效，而 `LowLevelCallable` 内部归一化胶囊仍记着这个地址。正因如此 `__new__` 才特意把原始 `function` 存进 tuple **保活**（见 [L104-L108](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_ccallback.py#L104-L108) 注释）。

---

### 4.3 签名约定与匹配机制

#### 4.3.1 概念说明

`LowLevelCallable` 不是「随便给个 C 函数就能用」。每个底层例程都有一张**它认识的签名表**——比如 `quad` 认识 `double(double)`、`double(double, void*)`、`double(int, double*)`、`double(int, double*, void*)` 这几种；`generic_filter` 则认识 `int(double*, npy_intp, double*, void*)`。

「签名」是一段**类 C 原型字符串**，例如 `"double (double, int *, void *)"`。它扮演两个角色：

1. **协议契约**：告诉底层「我应该按什么参数类型去调你」。
2. **多签名选择**：当例程支持多个签名时，签名串就是「路由 key」——底层用「胶囊的 name」去签名表里查匹配项，查到才知道该按哪种参数布局调用。

这是整个机制里最需要精确对齐的一环：**签名串必须逐字符匹配**（连 `*` 两边的空格都要对）。否则底层会抛 `ValueError: Invalid scipy.LowLevelCallable signature ...`。

#### 4.3.2 核心流程

签名在三个环节流转：

```
构造阶段（Python 侧）
   signature 来源：显式传入 / ctypes自动拼 / PyCapsule的name
        │ 存进归一化胶囊的 name 字段
        ▼
读取阶段（Python 侧 .signature 属性）
   LowLevelCallable.signature → _ccallback_c.get_capsule_signature(胶囊)
        │ 读 PyCapsule_GetName
        ▼
匹配阶段（C 侧）
   ccallback_prepare 用胶囊 name 与「例程的签名表」逐项 strcmp
        ├── 命中 → 记下 signature 指针，按其 value 字段决定调用方式
        └── 全不命中 → ccallback__err_invalid_signature 抛错
```

#### 4.3.3 源码精读

**签名表长什么样** —— 看 `quad` 的 C 侧 [scipy/integrate/__quadpack.h:62-76](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/integrate/__quadpack.h#L62-L76) —— `quadpack_call_signatures[]`，正是 docstring 里那四种签名（外加 `short`/`long` 的等价变体）。每项第二个字段（如 `CB_1D`/`CB_ND_USER`）是「路由值」，thunk 据此选择如何组织参数。

**Python 侧怎么读签名** —— [scipy/_lib/_ccallback.py:121-123](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_ccallback.py#L121-L123) —— `signature` 属性委托给 `_ccallback_c.get_capsule_signature`，后者在 [scipy/_lib/_ccallback_c.pyx:89-94](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_ccallback_c.pyx#L89-L94) 里就是 `PyCapsule_GetName` 取胶囊名并解码成 ASCII 字符串。

**C 侧怎么匹配签名** —— [scipy/_lib/src/ccallback.h:222-236](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/src/ccallback.h#L222-L236)：用 `for (sig = signatures; sig->signature != NULL; ++sig)` 遍历签名表，`strcmp(name, sig->signature)` 逐项比对；全不命中则调 [ccallback.h:101-139](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/src/ccallback.h#L101-L139) 的 `ccallback__err_invalid_signature`，把「期望的签名列表」拼进错误信息——这正是为什么你会看到一条很详细的报错。

**测试如何验证报错信息** —— [scipy/_lib/tests/test_ccallback.py:120-151](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/tests/test_ccallback.py#L120-L151) —— `test_bad_callbacks` 用一个签名表里没有的 `*_bc` 函数，断言抛 `ValueError` 且错误信息里同时包含「实际签名」和「期望签名之一」。

> 另有一个 `signature` 覆盖机制：构造时可显式传 `signature=`，强行指定签名串（见 [test_ccallback.py:154-164](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/tests/test_ccallback.py#L154-L164) 的 `test_signature_override`）。传错签名会在匹配阶段被拒绝。

#### 4.3.4 代码实践

**实践目标**：亲手触发一次签名不匹配，读懂报错。

**操作步骤**：

1. 构造一个签名是 `"double (double, double, int *, void *)"` 的 ctypes 指针（即测试里的 `plus1b`，多一个 `b` 参数）。
2. 把它包成 `LowLevelCallable` 传给只认识「单参数」签名的 caller，观察报错。

```python
# 示例代码：观察签名不匹配的报错
from scipy._lib import _ccallback_c as cy
from scipy._lib._ccallback import LowLevelCallable

llc = LowLevelCallable(cy.plus1b_ctypes)   # 签名带额外 double 参数
print("实际签名:", llc.signature)           # double (double, double, int *, void *)
# 用一个只认单参数签名表的 caller 去调（会抛 ValueError）
# import scipy._lib._test_ccallback as tcc
# tcc.test_call_simple(llc, 1.0)  # 取消注释可看到 Invalid signature 报错
```

**需要观察的现象**：`llc.signature` 显示带额外参数的签名；调用时抛出 `ValueError`，信息里列出「期望的签名」清单。

**预期结果**：报错信息形如 `Invalid scipy.LowLevelCallable signature "double (double, double, int *, void *)". Expected one of: ['double (double, int *, void *)', ...]`（**待本地验证**具体清单内容）。

#### 4.3.5 小练习与答案

**练习 1**：`"double (double, void *)"` 和 `"double(double,void*)"` 算同一个签名吗？

**答案**：不算。匹配用的是逐字符 `strcmp`（[ccallback.h:228](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/src/ccallback.h#L228)），空格必须一致。签名表里写的是带空格的标准格式（如 `"double (double, void *)"`），所以你构造时拼出来的字符串必须与之完全相同。

**练习 2**：ctypes 自动拼签名时，`ctypes.POINTER(ctypes.c_int)` 会被拼成什么？

**答案**：`"int *"`。`_typename_from_ctypes` 先识别 `LP_c_int` 记一级指针，剥掉 `LP_`，再剥掉 `c_` 得 `int`，最后补一个 ` *`（见 [scipy/_lib/_ccallback.py:207-226](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_ccallback.py#L207-L226)）。

---

### 4.4 C 层 ccallback.h：prepare / release / obtain 三件套与真实消费

#### 4.4.1 概念说明

前面三节都在讲「Python 侧怎么打包」。本节讲「C 侧怎么拆包并执行」，这是 `_ccallback_c` 和 `ccallback.h` 的核心。

`ccallback.h` 提供三个函数和一个结构体，构成最小的回调运行时：

- **`ccallback_t`** 结构（[ccallback.h:47-64](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/src/ccallback.h#L47-L64)）：记录 `c_function`（C 指针）、`py_function`（Python 对象）、`user_data`（用户数据指针）、`signature`（命中的签名项）、`error_buf`（错误跳转缓冲）、`prev_callback`（用于线程局部存储的嵌套回调链）。
- **`ccallback_prepare`**：把用户对象解析进 `ccallback_t`——是 Python/cffi/PyCapsule 还是普通 Python 可调用，分别填不同字段。
- **`ccallback_release`**：释放 `py_function` 引用、恢复线程局部状态。
- **`ccallback_obtain`**：thunk 用来「取回当前回调」——当第三方库的回调签名里**没有地方传 `user_data`** 时，thunk 靠它从线程局部存储（TLS）拿到 `ccallback_t`。

「prepare/release 成对出现」和「obtain 配合 TLS」是这套设计的两大工程要点。

#### 4.4.2 核心流程

以 `quad` 为例，一次完整调用的 C 侧时序：

```
Python: quad(LowLevelCallable(sin), 0, π)
   │  原样把 func 传给编译扩展
   ▼
C: _quadpack._qagse(func, ...)   ← scipy/integrate/__quadpack.h 里实现
   │
   ├─ init_callback(&cb, func, args)
   │     └─ ccallback_prepare(&cb, quadpack_call_signatures, func, CCALLBACK_OBTAIN)
   │           · 命中签名 "double (double)"
   │           · cb.c_function = sin 的地址；cb.user_data = NULL
   │           · 因 CCALLBACK_OBTAIN：把 &cb 存进线程局部 _active_ccallback
   │
   ├─ Py_BEGIN_ALLOW_THREADS              ← 释放 GIL
   │     Fortran QUADPACK 循环求值……
   │        每个采样点 x：调 quad_thunk(x)
   │           quad_thunk: cb = ccallback_obtain()  ← 从 TLS 取回
   │                      因 cb.c_function != NULL → 直接 C 调用，无 GIL
   ├─ Py_END_ALLOW_THREADS                ← 拿回 GIL
   │
   └─ free_callback(&cb) → ccallback_release(&cb)
```

两个标志位决定行为（[ccallback.h:28-34](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/src/ccallback.h#L28-L34)）：

- `CCALLBACK_OBTAIN`：开启 TLS，让 thunk 能用 `ccallback_obtain` 取回回调。用于「第三方库签名没给 user_data 槽位」的场景。
- `CCALLBACK_PARSE`：允许直接传裸 ctypes 指针（旧版兼容），由 C 侧调 `_parse_callback` 转换。新代码应直接传 `LowLevelCallable`。

#### 4.4.3 源码精读

**结构的定义** —— [scipy/_lib/src/ccallback.h:47-64](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/src/ccallback.h#L47-L64)：`ccallback_t` 的所有字段。注意 `info`/`info_p` 是「thunk 可自由使用的备用槽」，`quad` 就用 `info_p` 存多变量积分的参数数组（见 [__quadpack.h:125](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/integrate/__quadpack.h#L125)）。

**prepare 的核心分派** —— [scipy/_lib/src/ccallback.h:202-253](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/src/ccallback.h#L202-L253)：

- [L202-L209](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/src/ccallback.h#L202-L209)：`PyCallable_Check` 命中 → 走 Python 分支，填 `py_function`、`c_function=NULL`。
- [L210-L253](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/src/ccallback.h#L210-L253)：是 `LowLevelCallable`（tuple 且首元素是 PyCapsule）→ 从胶囊取指针、匹配签名、填 `c_function`/`user_data`/`signature`。这里 `PyTuple_GetItem(callback_obj, 0)` 正是「继承 tuple」的回报。

**TLS 与 obtain** —— [scipy/_lib/src/ccallback.h:70-89](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/src/ccallback.h#L70-L89)：`_active_ccallback` 是线程局部变量，`ccallback_obtain` 直接返回它。prepare 时若带 `CCALLBACK_OBTAIN`，会把当前回调压栈（`prev_callback`）后写入 TLS，release 时恢复——这保证了**同线程嵌套回调**（如递归积分）和**多线程并发**都安全（[test_ccallback.py:167-196](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/tests/test_ccallback.py#L167-L196) 的 `test_threadsafety` 正是验证这一点）。

**最佳实践范例** —— [scipy/_lib/src/_test_ccallback.c:168-198](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/src/_test_ccallback.c#L168-L198)（`test_call_simple`，带 `user_data` 槽位）与 [scipy/_lib/src/_test_ccallback.c:201-231](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/src/_test_ccallback.c#L201-L231)（`test_call_nodata`，用 `CCALLBACK_OBTAIN` + `ccallback_obtain`，演示「没 user_data 槽位时怎么办」）。`test_call_nonlocal`（[L234-L270](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/src/_test_ccallback.c#L234-L270)）还演示了用 `setjmp`/`longjmp` 做「出错时非局部跳转」的最后手段。

**真实消费者 quad** —— [scipy/integrate/__quadpack.h:131-169](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/integrate/__quadpack.h#L131-L169)（`init_callback`）与 [scipy/integrate/__quadpack.h:194-213](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/integrate/__quadpack.h#L194-L213)（`free_callback` 与 thunk 取回）。这里能清楚看到：`init_callback` 默认用 `CCALLBACK_OBTAIN`（[L137](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/integrate/__quadpack.h#L137)），对裸 ctypes 对象额外加 `CCALLBACK_PARSE` 并切到 legacy 签名表（[L155-L159](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/integrate/__quadpack.h#L155-L159)），最后 `ccallback_prepare`（[L161](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/integrate/__quadpack.h#L161)）。Python 侧 `quad` 只是把 `func` 原样透传：[scipy/integrate/_quadpack_py.py:626](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/integrate/_quadpack_py.py#L626)。

#### 4.4.4 代码实践

**实践目标**：跟踪 `quad` 消费 `LowLevelCallable` 的完整调用链，把 4.1～4.4 串起来。

**操作步骤**（源码阅读型 + 跟踪）：

1. 从 Python 入口 [scipy/integrate/_quadpack_py.py:626](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/integrate/_quadpack_py.py#L626) 出发，确认 `func` 被原样传给 `_quadpack._qagse`。
2. 跳到 C 侧 [scipy/integrate/__quadpack.h:131-169](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/integrate/__quadpack.h#L131-L169)（`init_callback`），确认它调用 `ccallback_prepare`，并用了 `CCALLBACK_OBTAIN`。
3. 跳到 [scipy/_lib/src/ccallback.h:165-275](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/src/ccallback.h#L165-L275)（`ccallback_prepare`），定位「LowLevelCallable 分支」[L210-L253](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/src/ccallback.h#L210-L253)——它从胶囊取指针并匹配签名。
4. （可选运行）在 `quad` 调用前后各打一行日志，确认积分值正确：

```python
# 示例代码：验证 quad + LowLevelCallable 端到端正确
import ctypes, ctypes.util
import numpy as np
from scipy import integrate, LowLevelCallable

lib = ctypes.CDLL(ctypes.util.find_library('m') or 'libm.so')
lib.cos.restype = ctypes.c_double
lib.cos.argtypes = (ctypes.c_double,)
llc = LowLevelCallable(lib.cos)               # double(double)
val, err = integrate.quad(llc, 0, np.pi/2)    # ∫cos = 1
print(f"结果={val:.10f}  误差估计={err:.2e}")   # 期望 ≈ 1.0
```

**需要观察的现象**：积分值约为 `1.0`，误差估计极小。

**预期结果**：`val ≈ 1.0`，与 `∫₀^{π/2} cos x dx = 1` 一致（**待本地验证**具体数值精度）。

#### 4.4.5 小练习与答案

**练习 1**：`quad` 的 `init_callback` 为什么默认带 `CCALLBACK_OBTAIN`？

**答案**：因为 QUADPACK 的 Fortran 求值回调签名里没有专门的 `user_data` 槽位能一路传递 `ccallback_t` 指针，thunk 只能靠 `ccallback_obtain()` 从线程局部存储取回当前回调。`CCALLBACK_OBTAIN` 正是用来在 prepare 时把回调写进 TLS、在 release 时恢复的开关（[ccallback.h:259-267](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/src/ccallback.h#L259-L267)）。

**练习 2**：`ccallback_prepare` 怎么判断一个对象「是 `LowLevelCallable`」？

**答案**：它在首次需要时动态 `import scipy._lib._ccallback` 并取出 `LowLevelCallable` 类型对象缓存起来，再用 `PyObject_TypeCheck(obj, lowlevelcallable_type)` 做类型检查（[ccallback.h:172-185](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/src/ccallback.h#L172-L185) 与 [L211](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/src/ccallback.h#L211)）。这样 C 头文件不必硬依赖 Python 类，保持了「纯 C、无魔法」的设计原则。

---

## 5. 综合实践

**任务**：从「拿到一个 C 函数」到「让 `quad` 高效调用它」，完整走一遍 `LowLevelCallable` 的链路，并量化性能收益。

**步骤**：

1. **准备 C 函数**：用 ctypes 加载系统数学库 `libm`，取出 `sin` 和 `exp` 两个 C 函数指针，并设置好 `restype`/`argtypes`（参考 4.1.4 的脚本）。
2. **包装**：用 `LowLevelCallable(sin_c)` 和 `LowLevelCallable(exp_c)` 分别包装，打印它们的 `.signature` 属性，确认是 `"double (double)"`。
3. **验证正确性**：用 `quad` 计算 `∫₀^π sin x dx`（期望 `2.0`）和 `∫₀^1 e^x dx`（期望 `e-1 ≈ 1.71828`），同时用纯 Python 版 `math.sin`/`math.exp` 各算一遍，确认两者结果一致。
4. **量化性能**：用 `timeit` 对「LowLevelCallable 版」与「纯 Python 版」各跑 200 次，记录并比较耗时。
5. **延伸（选做）**：把 `LowLevelCallable` 的 `signature` 显式改成签名表里没有的字符串（如 `"bad signature"`），调用 `quad`，观察并解释报错来自 4.3 描述的哪一步。

**验收标准**：

- 正确性：两种路径结果一致且符合解析解；
- 性能：LowLevelCallable 路径**不慢于**纯 Python 路径（多数情况更快；具体倍数**待本地验证**）；
- 能解释 4.3.4 的报错链路：构造阶段写入胶囊 name → C 侧 `ccallback_prepare` 的签名 `strcmp` 匹配失败 → `ccallback__err_invalid_signature` 抛错。

## 6. 本讲小结

- `LowLevelCallable` 解决的是「C/Fortran 底层例程反复调用用户函数」时的 **Python 回调开销**：把一个 C 函数指针直接交给底层，让求值循环绕开 Python 对象创建和 GIL。
- 它是一个**继承 `tuple` 的不可变类**，把 ctypes / cffi / PyCapsule / Cython 四种异构来源「归一」成一个内部 PyCapsule（指针+签名+用户数据），原始对象被一起存进 tuple 以**保活**底层指针。
- **签名**是类 C 原型字符串（如 `"double (double, int *, void *)"`），既是协议契约也是路由 key；C 侧用**逐字符 `strcmp`** 与例程的签名表匹配，不匹配会抛带期望清单的 `ValueError`。
- C 侧运行时是 `ccallback.h` 的 **prepare/release/obtain 三件套 + `ccallback_t` 结构 + 线程局部存储**；thunk 按 `c_function` 是否为空决定「直接 C 调用」还是「拿 GIL 调 Python」。
- `quad`（[__quadpack.h](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/integrate/__quadpack.h)）是这套机制**最真实的消费者**：Python 侧只透传 `func`，C 侧用 `CCALLBACK_OBTAIN` + `ccallback_obtain` 在 Fortran 循环里取回回调。
- `LowLevelCallable` 被提升到顶层 `scipy` 命名空间（[scipy/__init__.py:82](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L82)），它的导入还兼任 u1-l3 提到的「编译扩展模块金丝雀检查」。

## 7. 下一步学习建议

- **继续往下读 C 层**：精读 [scipy/_lib/src/_test_ccallback.c](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/src/_test_ccallback.c) 的三种 caller（simple/nodata/nonlocal），理解 `user_data` 槽位、TLS obtain、`setjmp` 非局部跳转三种场景的取舍。
- **看另一个消费者**：`scipy.ndimage` 的 `generic_filter` / `geometric_transform`（[scipy/ndimage/_filters.py](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/ndimage/_filters.py)）使用不同的签名（`int(double*, npy_intp, double*, void*)`），对比它和 `quad` 的签名表差异，体会「签名即协议」。
- **动手写一个 Cython 回调**：参考 [scipy/_lib/_ccallback_c.pyx:179-201](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_ccallback_c.pyx#L179-L201) 的 `plus1_cython`，写一个 `cdef` 函数并用 `LowLevelCallable.from_cython` 包装，体会 `__pyx_capi__` 这条路径——这会自然衔接 [u13-l1](u13-l1-adding-compiled-extensions.md)「用 Cython 添加底层函数」。
- **回到构建**：`_ccallback_c.pyx` 和 `_test_ccallback.c` 是怎么被 meson 编译进 SciPy 的？这要回到 [u1-l2](u1-l2-build-system-and-source-build.md) 的构建系统与 [u13-l1](u13-l1-adding-compiled-extensions.md) 的扩展注册。
