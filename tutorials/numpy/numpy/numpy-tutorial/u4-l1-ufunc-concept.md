# ufunc 概念与 frompyfunc

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚什么是通用函数（universal function，简称 ufunc），以及它「逐元素 + 广播」的执行模型为什么比 Python 循环快得多。
- 认出一个 ufunc 对象的关键属性：`nin`、`nout`、`nargs`、`ntypes`、`types`、`identity`，并理解它们在源码里对应哪些字段。
- 在源码层面追踪 `np.add`、`np.multiply` 这类内置 ufunc 是如何被装配并暴露到 `np.` 命名空间的。
- 读懂 `np.frompyfunc` 的 C 实现（`umathmodule.c`），理解为什么它包装出来的 ufunc 输出永远是 `object` dtype。
- 用 `frompyfunc` 把任意 Python 函数包装成 ufunc，并会调用 ufunc 的 `reduce` / `accumulate` / `outer` 方法。

## 2. 前置知识

在进入本讲前，请确认你已经掌握下面这些概念（它们在前置讲义中讲过）：

- **ndarray 与内存模型**：数组由 `data` 缓冲区 + `shape` + `strides` 三件套描述，`strides` 单位是字节（见 u1-l4、u4-l2）。本讲里 ufunc 的「逐元素」操作，本质上就是按 `strides` 在 `data` 缓冲区上推进。
- **dtype 与类型提升**：每个数组都有 dtype，运算时会按 NEP 50 规则求公共 dtype（见 u2-l2、u2-l3）。ufunc 的核心任务之一，就是根据输入 dtype 选出一条对应的 C 内层循环。
- **广播**：形状不同的数组按「从最右轴对齐、长度 1 的轴可拉伸」规则对齐，靠把对应轴的 stride 置 0 实现，零拷贝（见 u3-l4）。ufunc 接收任意可广播的输入，正是建立在这套机制之上。
- **顶层命名空间装配**：`np.add` 来自 C 扩展 `_multiarray_umath`，经 `_core/umath.py` → `_core/__init__.py` → `numpy/__init__.py` 三跳再导出（见 u1-l3）。

如果你对「为什么 `np.array([1,2,3]) + np.array([4,5,6])` 能得到逐元素相加的结果」还停留在直觉层面，本讲正好带你下钻到这条加法背后的统一机制——ufunc。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `numpy/_core/umath.py` | 一个**纯 Python 薄壳**，把 C 扩展 `_multiarray_umath` 里所有内置 ufunc 再导出，并用 `__all__` 列出公开名字。 |
| `numpy/_core/src/umath/umathmodule.c` | C 层。包含 `frompyfunc` 的实现 `ufunc_frompyfunc`，以及把任意 Python 可调用对象当内层循环用的 `object_ufunc_type_resolver`。 |
| `numpy/_core/src/umath/loops.c.src` | C 层。定义了 `PyUFunc_On_Om`——这是 `frompyfunc` 造出来的 ufunc 真正执行逐元素计算的那个「内层循环」。 |
| `numpy/_core/include/numpy/ufuncobject.h` | C 头文件。定义了 `PyUFuncObject_fields` 结构体（即 ufunc 对象的全部字段）和 `PyUFunc_PyFuncData`。 |
| `numpy/_core/src/umath/ufunc_object.c` | C 层。定义 ufunc 类型的方法表 `ufunc_methods`（`reduce`/`accumulate`/`outer` 等），以及 `reduce` 的参数解析。 |
| `numpy/_core/src/multiarray/multiarraymodule.c` | C 层。`frompyfunc` 这个名字实际是在这里注册成模块方法的。 |
| `numpy/_core/__init__.py` | 把 `umath` 子模块并入 `_core` 命名空间。 |
| `numpy/__init__.py` | 在第 260 行把 `frompyfunc` 等名字再导出到顶层 `np.`。 |

一句话总览：**ufunc 是 C 对象，由 `_multiarray_umath` 提供；`umath.py` 只是把它们搬运到 Python 命名空间；`frompyfunc` 是把这些 C 机制复用到「任意 Python 函数」上的桥梁。**

## 4. 核心概念与源码讲解

### 4.1 ufunc 是什么：逐元素 + 广播的执行模型

#### 4.1.1 概念说明

在纯 Python 里，要把两个列表逐元素相加，你会写：

```python
[a + b for a, b in zip(lst1, lst2)]
```

这有两层开销：一是每次循环都要在 Python 解释器里解释字节码；二是每个 `a + b` 都要动态查找 `int.__add__`、构造新的 Python 整数对象。

NumPy 的 ufunc（universal function，通用函数）用一套完全不同的模型：

- 它是一个**对数组逐元素作用**的函数，比如 `np.add`、`np.multiply`、`np.sin`。
- 它**自带广播**：输入形状不需要相同，只要可广播就能算。
- 它的真正计算在 **C 层的一条紧凑循环**里完成，循环体直接读写裸内存指针，没有 Python 对象参与（除非是 object dtype）。
- 它**统一管理类型**：给定输入 dtype，ufunc 会查表选出对应的那条 C 内层循环。

所以 `np.add(a, b)` 并不是「对每个元素调用 Python 的加法」，而是「把整个数组交给一条 C 循环」。这就是为什么同样的逐元素加法，NumPy 比 Python 列表推导快一到两个数量级。

#### 4.1.2 核心流程

一次 ufunc 调用的逻辑流程（伪代码）：

```text
ufunc(a, b, out=None)
  ├── 1. 把所有输入归一化为 ndarray（asanyarray）
  ├── 2. 用广播规则求出公共输出形状
  ├── 3. 类型解析（type resolution）：
  │        根据输入 dtype，从 functions[]/types[] 表里
  │        选出一条匹配的 C 内层循环
  ├── 4. 分配/复用输出数组
  └── 5. 在广播后的形状上跑选中的 C 内层循环
            for i in 0..N:
                out[i] = kernel(in0[i], in1[i], ...)
```

其中第 5 步的「kernel」就是 C 函数指针 `PyUFuncGenericFunction`。对 `np.add` 来说，`int64 + int64` 和 `float64 + float64` 是**两条不同的 kernel**，ufunc 会在第 3 步按 dtype 选对那一条。

如果把广播考虑进去，第 5 步的循环不是简单的 `for i in range(N)`，而是按 `strides` 推进指针：被广播（长度为 1）的轴其 stride 为 0，指针原地不动，于是同一元素被反复读取——这正是 u3-l4 讲过的零拷贝广播。

逐元素运算量 \(N\) 与循环次数成正比：

\[
T_{\text{ufunc}} \approx N \cdot t_{\text{kernel}} + C_{\text{setup}}
\]

其中 \(t_{\text{kernel}}\) 是一次 C kernel 的耗时（纳秒级），\(C_{\text{setup}}\) 是类型解析、广播、分配的固定开销。当 \(N\) 很大时，\(C_{\text{setup}}\) 可忽略，ufunc 接近「裸 C 速度」。

#### 4.1.3 源码精读

先看 ufunc 在 Python 层的「家」。`numpy/_core/umath.py` 整个文件就是一个再导出壳：

[umath.py:L9-L12](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/umath.py#L9-L12) — 从 C 扩展 `_multiarray_umath` 把所有对象 `*` 导入。注意文件顶部注释说得很清楚：v1.16 之后 multiarray 和 umath 两个 C 扩展合并成了同一个 `_multiarray_umath`，这个文件只是「复制出旧的 umath 命名空间」。

[umath.py:L44-L59](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/umath.py#L44-L59) — `__all__` 列表。你能看到 `add`、`multiply`、`sin`、`exp`、`matmul`、`frompyfunc` 等名字全在这里。**这就是「np.add 是什么」的最终答案：它是 `_multiarray_umath` 模块里的一个 C 对象，被这个列表声明为公开 API。**

为什么 `np.add` 能在 `np.` 下访问？链路是：

[_core/__init__.py:L93](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/__init__.py#L93) — `_core` 包 `from . import umath`，于是 `umath.add` 进了 `_core` 命名空间。

[numpy/__init__.py:L120-L121](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L120-L121) 与 [numpy/__init__.py:L260](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L260) — 顶层 `from ._core import (...)` 显式地把 `add`、`frompyfunc` 等名字搬进 `np.`。三跳再导出完成。

#### 4.1.4 代码实践

**目标**：直观感受 ufunc 与 Python 函数的区别。

```python
import numpy as np

print(type(np.add))        # <class 'numpy.ufunc'>
print(type(np.sin))        # <class 'numpy.ufunc'>
print(type(len))           # <class 'builtin_function_or_method'>
```

**操作步骤**：

1. 运行上面三行。
2. 观察 `np.add` 的类型是 `numpy.ufunc`，而内置 `len` 不是。

**预期结果**：`np.add`、`np.sin` 都是 `numpy.ufunc` 实例，说明 ufunc 是一种**独立的对象类型**，不是普通 Python 函数。

#### 4.1.5 小练习与答案

**练习 1**：下列哪些是 ufunc？`np.add`、`np.sum`、`np.exp`、`np.reshape`、`np.dot`。

**答案**：`np.add`、`np.exp` 是 ufunc（`type(...)` 为 `numpy.ufunc`）。`np.sum` 是归约函数（底层调用 ufunc 的 `reduce`，但本身不是 ufunc）。`np.reshape` 是形状操作函数。`np.dot` 在现代版本里也是 ufunc（可在 `umath.py` 的 `__all__` 里看到 `matmul`/`dot` 系列）。

**练习 2**：为什么 `np.add(a, b)` 比 `[x+y for x,y in zip(a,b)]` 快？

**答案**：前者把整个数组的逐元素加法交给一条 C 内层循环，循环体内直接操作裸内存指针，没有 Python 对象和动态分派开销；后者每步都要解释 Python 字节码、查找 `__add__`、构造新 Python 对象。

---

### 4.2 ufunc 公开命名空间（_core/umath.py）

#### 4.2.1 概念说明

很多人以为 `np.add`、`np.sin` 这些函数是某处用 `def add(...):` 写出来的 Python 函数。其实不是。它们是 **C 扩展模块 `_multiarray_umath` 在初始化时用代码生成器批量造出来的 C 对象**，`numpy/_core/umath.py` 这个文件**一行 Python 实现都没有**，它唯一的职责是「再导出」。

理解这一点很重要：当你 `import numpy as np` 时，`np.add` 这个对象早在 C 扩展被加载时就存在了；`umath.py` 只是给它取了个能在 Python 命名空间里访问的名字。

#### 4.2.2 核心流程

`umath.py` 的装配流程：

```text
import _multiarray_umath        # C 扩展被加载，所有内置 ufunc 已构造好
from ._multiarray_umath import *  # 把扩展模块的 __dict__ 整体倒进来
__all__ = [...]                   # 声明哪些名字是公开 API
```

而 C 扩展内部（`umathmodule.c` 的 `initumath`）做了真正的「造 ufunc」工作：

```text
initumath(m)
  ├── InitOperators(d)           # 代码生成：批量创建 add/multiply/sin... 并塞进模块字典 d
  ├── 设置常量 pi / e / euler_gamma
  ├── _PyArray_SetNumericOps(d)  # 把这些 ufunc 挂到 ndarray 的运算符上
  └── init_string_ufuncs(d) 等    # 注册字符串相关 ufunc
```

也就是说，`a + b` 能触发 `np.add` 这条 ufunc，是因为 `_PyArray_SetNumericOps` 把 `np.add` 注册成了 ndarray 的 `__add__` 实现。

#### 4.2.3 源码精读

[umath.py:L11-L12](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/umath.py#L11-L12) — `from . import _multiarray_umath` 与 `from ._multiarray_umath import *`。这两行是整个文件的灵魂：所有 ufunc 对象从这里来。

[umath.py:L18-L42](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/umath.py#L18-L42) — 注释写明「这些导入是为了向后兼容，不要改」（gh-11862）。里面除了 `_UFUNC_API`，还有一些以下划线开头的字符串处理辅助函数（`_strip`、`_replace` 等），它们给 `numpy.strings` 提供底层实现。

再看 C 侧批量造 ufunc 的入口：

[umathmodule.c:L39-L40](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/umathmodule.c#L39-L40) — `#include "funcs.inc"` 与 `#include "__umath_generated.c"`。注释说「自动生成的代码，定义所有 ufunc」。这意味着 `np.add` 等内置 ufunc 的构造代码是**构建时由脚本生成**的，不是手写的。

[umathmodule.c:L188-L190](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/umathmodule.c#L188-L190) — `InitOperators(d)` 是把生成好的 ufunc 注册进模块字典 `d` 的总入口。`d` 就是 `_multiarray_umath` 模块的 `__dict__`，也就是 `umath.py` 里 `from ... import *` 的来源。

#### 4.2.4 代码实践

**目标**：验证 `umath.py` 里没有 Python 实现，所有 ufunc 都来自 C 扩展。

```python
import numpy as np
import numpy._core.umath as umath

# 1) np.add 与 umath.add 是不是同一个对象？
print(np.add is umath.add)          # True

# 2) umath 模块里有多少个公开 ufunc？
print(len(umath.__all__))           # __all__ 列表长度

# 3) add 对象定义在哪个模块？
print(np.add.__module__ if hasattr(np.add, "__module__") else "n/a")
```

**操作步骤**：

1. 运行上述代码。
2. 第 1 行确认顶层 `np.add` 就是 `umath.add`，是同一个 C 对象。
3. 第 2 行打印 `__all__` 长度（约 90+ 个名字）。

**预期结果**：`np.add is umath.add` 为 `True`，证明它们指向同一个 C 对象，没有任何 Python 层副本。

#### 4.2.5 小练习与答案

**练习 1**：`numpy/_core/umath.py` 里有没有任何 `def add(...)` 之类的定义？

**答案**：没有。整个文件只有 `import` 和 `__all__`，没有任何函数定义。`np.add` 是 C 扩展 `_multiarray_umath` 提供的 C 对象。

**练习 2**：为什么 `a + b`（`a`、`b` 是 ndarray）会调用 `np.add` 这条 ufunc？

**答案**：`initumath` 里调用 `_PyArray_SetNumericOps(d)`，把 `np.add` 注册成 ndarray 类型的 `__add__`（以及 `+` 运算符）实现。所以 `a + b` 实际上触发 `np.add(a, b)` 这条 ufunc。

---

### 4.3 PyUFuncObject 结构与 nin / nout / identity 等属性

#### 4.3.1 概念说明

每个 ufunc 对象在 C 层都是一个 `PyUFuncObject` 结构体。你在 Python 里看到的那些属性——`nin`（输入个数）、`nout`（输出个数）、`nargs`（总参数数 = nin + nout）、`ntypes`（注册的循环种类数）、`types`（每条循环的输入输出 dtype 签名）、`identity`（归约时的单位元）——都是这个结构体字段的直接映射。

记住几个典型值：

- `np.add`：`nin=2, nout=1`，`identity=0`（加法的单位元是 0）。
- `np.multiply`：`nin=2, nout=1`，`identity=1`（乘法的单位元是 1）。
- `np.negative`：`nin=1, nout=1`，`identity=None`（一元运算没有归约单位元）。
- `np.logical_and`：`identity=True`（逻辑与的单位元是 True）。

`identity` 之所以重要，是因为它决定 ufunc 能否做 `reduce`：`np.add.reduce([1,2,3])` 从 0 开始累加得 6，这个「0」就是 `identity`。

#### 4.3.2 核心流程

ufunc 对象的关键字段及其用途：

```text
PyUFuncObject
  ├── nin, nout, nargs        # 参数个数；nargs 恒等于 nin+nout
  ├── identity                # 归约单位元（One/Zero/MinusOne/None/...）
  ├── functions[]             # 一组 C 内层循环函数指针（每条对应一种 dtype 组合）
  ├── types[]                 # 每条循环的 (in0_dtype, in1_dtype, ..., out_dtype) 字符编码
  ├── ntypes                  # functions / types 数组的条目数
  ├── type_resolver           # 给定输入，决定用哪条循环、输出什么 dtype
  └── core_* (gufunc 字段)    # 通用 ufunc（如 matmul）的核心维度签名
```

类型解析的直觉：调用 `np.add(int8_array, int8_array)` 时，`type_resolver` 会在 `types[]` 里找一行匹配 `(int8, int8, ?)` 的签名，选中对应的 `functions[k]` 作为内层循环。如果没有完全匹配，就走类型提升（u2-l3）找到一条可转换的循环。

#### 4.3.3 源码精读

[ufuncobject.h:L103-L113](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/include/numpy/ufuncobject.h#L103-L113) — `PyUFuncObject_fields` 结构体开头。注释明确写出 `nin`/`nout`/`nargs` 的含义，并自嘲「nargs 恒等于 nin+nout，为什么还要存？」（历史遗留）。

[ufuncobject.h:L114-L127](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/include/numpy/ufuncobject.h#L114-L127) — `identity` 字段（注释列出所有取值），以及 `functions`、`data`、`ntypes`。`functions` 是「一维核心循环数组」，`data` 是传给每条循环的附加数据指针。

[ufuncobject.h:L135-L139](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/include/numpy/ufuncobject.h#L135-L139) — `types` 是「类型编号数组，大小为 `nargs * ntypes`」，`doc` 是文档字符串。这解释了为什么 `np.add.types` 是一个字符串列表：每个字符串长度正好是 `nargs`（add 是 3：两个输入 + 一个输出）。

[ufuncobject.h:L277-L303](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/include/numpy/ufuncobject.h#L277-L303) — identity 的枚举常量定义：`PyUFunc_Zero=0`、`PyUFunc_One=1`、`PyUFunc_MinusOne=2`（给按位与用）、`PyUFunc_None=-1`（不可重排）、`PyUFunc_ReorderableNone=-2`、`PyUFunc_IdentityValue=-3`（用对象作单位元）。

[ufuncobject.h:L312-L316](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/include/numpy/ufuncobject.h#L312-L316) — `PyUFunc_PyFuncData` 结构体：`{int nin; int nout; PyObject *callable;}`。这个结构体是 `frompyfunc` 的核心载体，4.4 节会用到。

#### 4.3.4 代码实践

**目标**：把 `np.add` 的 Python 属性和 C 结构体字段一一对应。

```python
import numpy as np

u = np.add
print("nin      =", u.nin)        # 2
print("nout     =", u.nout)       # 1
print("nargs    =", u.nargs)      # 3
print("ntypes   =", u.ntypes)     # 注册了多少条 dtype 循环
print("identity =", u.identity)   # 0
print("types[:3]=", u.types[:3])  # 看前 3 条签名，每条长度 = nargs = 3
```

**操作步骤**：

1. 运行上述代码，记录每个值。
2. 对照源码 [ufuncobject.h:L103-L139](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/include/numpy/ufuncobject.h#L103-L139)，确认每个 Python 属性对应哪个 C 字段。
3. 注意 `types` 里每个字符串的长度都等于 `nargs`（3），印证「types 数组大小为 nargs * ntypes」。

**预期结果**：`nin=2, nout=1, nargs=3, identity=0`；`types` 中形如 `'???'`（bool）、`'bbb'`（byte）、`'ddd'`（double）等，每个字符串长度都是 3。

#### 4.3.5 小练习与答案

**练习 1**：`np.multiply.identity` 是多少？为什么？

**答案**：是 `1`。因为乘法的归约单位元是 1（`np.multiply.reduce([2,3,4])` 从 1 开始连乘得 24）。源码里 `np.multiply` 构造时传入 `PyUFunc_One`（=1）。

**练习 2**：`np.add.types` 里每个字符串为什么长度都是 3？

**答案**：因为 `np.add` 的 `nargs = nin + nout = 2 + 1 = 3`，每个签名字符串用 3 个字符分别表示两个输入和一个输出的 dtype 编码（如 `'lll'` 表示 long+long→long）。

---

### 4.4 frompyfunc 的 C 实现

#### 4.4.1 概念说明

内置 ufunc（`np.add`、`np.sin`）的每条内层循环都是手写/生成的 C 代码，性能极高但写起来麻烦。如果你只有一个普通的 Python 函数，想享受 ufunc 的「自动广播 + 逐元素」体验怎么办？

`np.frompyfunc(func, nin, nout)` 就是干这个的：它把**任意 Python 可调用对象**包装成一个 ufunc。包装出来的 ufunc 把每个元素的计算**回调**到你给的 Python 函数上。

代价是显然的：因为它最终要调用 Python 函数，所以**没有 C 内层循环的速度优势**——它的价值不在快，而在「统一接口」：包装后你可以对任意维度的数组、任意可广播的输入直接调用，还能用 `reduce`/`accumulate`/`outer`。

还有一个关键特点：**`frompyfunc` 造出的 ufunc 输入输出永远是 `object` dtype**。因为 Python 函数的返回值类型在编译期未知，ufunc 只能把它们当成 Python 对象存进 object 数组。

#### 4.4.2 核心流程

`frompyfunc(func, nin, nout)` 在 C 层（`ufunc_frompyfunc`）做的事：

```text
ufunc_frompyfunc(func, nin, nout, identity=None)
  ├── 1. PyArg_ParseTupleAndKeywords 解析参数：Oii|$O
  │        位置参数：function, nin, nout；可选关键字：identity
  ├── 2. PyCallable_Check(function)  # 必须可调用
  ├── 3. 取 function.__name__ 作为 ufunc 名字（取不到就用 "?"）
  ├── 4. 分配一块内存，里面放：
  │        - PyUFunc_PyFuncData{nin, nout, callable=function}
  │        - types[] 数组：全部填 NPY_OBJECT（nin+nout 个）
  │        - 名字字符串 + " (vectorized)"
  ├── 5. PyUFunc_FromFuncAndDataAndSignatureAndIdentity(...)  # 真正构造 ufunc
  │        内层循环表只有一个：pyfunc_functions = {PyUFunc_On_Om}
  ├── 6. self->obj = function       # 持有 Python 函数引用，防 GC
  └── 7. self->type_resolver = object_ufunc_type_resolver
              # 强制所有输入输出 dtype 为 NPY_OBJECT
```

被选中的那条唯一内层循环 `PyUFunc_On_Om`，做的事就是「对每个元素，调用一次 Python 函数」：

```text
PyUFunc_On_Om(args, dimensions, steps, func):
  n = dimensions[0]                 # 要算多少个元素
  for i in 0..n-1:
      arglist = tuple(输入[i] 的 nin 个 Python 对象)
      result = tocall(*arglist)       # ← 回调你的 Python 函数
      把 result 写回输出[i]
      各指针按 steps 推进
```

`steps[j]` 就是第 j 个操作数的 stride——广播的轴 stride 为 0，所以同一输入元素会被反复喂给函数。这正是「frompyfunc 自动支持广播」的实现原理。

#### 4.4.3 源码精读

[umathmodule.c:L43](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/umathmodule.c#L43) — `pyfunc_functions[] = {PyUFunc_On_Om};`。`frompyfunc` 造出的 ufunc 只有一条内层循环，就是这个 `PyUFunc_On_Om`。

[umathmodule.c:L45-L65](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/umathmodule.c#L45-L65) — `object_ufunc_type_resolver`：把**所有** `nin+nout` 个 dtype 都设成 `NPY_OBJECT`。这就是「frompyfunc 输出永远是 object dtype」的根本原因。

[umathmodule.c:L82-L89](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/umathmodule.c#L82-L89) — 参数解析 `"Oii|$O:frompyfunc"`：`O`=function、`i`=nin、`i`=nout，`|$O` 表示可选的关键字 `identity`。接着 `PyCallable_Check` 确保传入的是可调用对象，否则抛 `TypeError: function must be callable`。

[umathmodule.c:L130-L139](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/umathmodule.c#L130-L139) — 填充 `fdata->callable = function`，并把 `types[i] = NPY_OBJECT` 循环写入所有 `nargs = nin+nout` 个槽位。

[umathmodule.c:L148-L162](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/umathmodule.c#L148-L162) — 调 `PyUFunc_FromFuncAndDataAndSignatureAndIdentity` 真正构造 ufunc；随后 `self->obj = function` 持有函数引用防止被垃圾回收，`self->type_resolver = &object_ufunc_type_resolver` 覆盖默认类型解析。

注意一个有趣的事实：`frompyfunc` 的实现 `ufunc_frompyfunc` 写在 `umathmodule.c` 里，但它作为模块方法注册却不在 umath 模块，而在 multiarray：

[multiarraymodule.c:L4801-L4804](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/multiarraymodule.c#L4801-L4804) — 注释 `/* from umath */`，把 `ufunc_frompyfunc` 以 `"frompyfunc"` 的名字注册进 `_multiarray_umath` 模块的方法表。由于 v1.16 后 umath 和 multiarray 合并成同一扩展，这只是历史包袱。

最后看内层循环本体：

[loops.c.src:L326-L333](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/loops.c.src#L326-L333) — `PyUFunc_On_Om` 函数签名与开头：`n = dimensions[0]` 是元素总数；`tocall = data->callable` 取出你传进来的 Python 函数。

[loops.c.src:L344-L360](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/loops.c.src#L344-L360) — 核心循环：对每个元素，从 `ptrs[j]` 读出 `nin` 个 `PyObject*` 组成 `arglist`，然后 `PyObject_CallObject(tocall, arglist)` 调用你的函数得到 `result`。

[loops.c.src:L392-L394](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/loops.c.src#L392-L394) — 循环末尾 `ptrs[j] += steps[j]`：按 stride 推进每个操作数的指针。广播的轴 stride=0，指针不动，元素被复用。

#### 4.4.4 代码实践

**目标**：用 `frompyfunc` 包装一个 Python 函数，观察输出 dtype 与广播行为。

```python
import numpy as np

def myadd(a, b):
    return a + b

uf = np.frompyfunc(myadd, 2, 1)   # nin=2, nout=1
print(type(uf))                    # numpy.ufunc
print(uf.__name__)                 # 'myadd (vectorized)'

x = np.array([1, 2, 3])
y = np.array([10, 20, 30])
print(uf(x, y))                    # array([11, 22, 33], dtype=object)
print(uf(x, y).dtype)              # object   ← 关键：永远是 object

# 广播：x 形状 (3,)，把 100 当成标量广播
print(uf(x, 100))                  # array([101, 102, 103], dtype=object)
```

**操作步骤**：

1. 运行上述代码。
2. 确认 `uf(x, y).dtype` 是 `object`，印证 [umathmodule.c:L45-L65](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/umathmodule.c#L45-L65) 的 `object_ufunc_type_resolver`。
3. 注意 `uf.__name__` 带了 `" (vectorized)"` 后缀，对应源码 [umathmodule.c:L141-L142](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/umathmodule.c#L141-L142)。

**预期结果**：输出数组的 dtype 为 `object`；即便输入是 int 数组，结果也装在 object 数组里（元素仍是 Python int）。这就是 frompyfunc 与原生 ufunc 最直观的差别。

**注意**：因为元素是 object，后续数值运算会退化为 Python 速度，所以 frompyfunc 适合「接口统一」而非「提速」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `np.frompyfunc(f, 2, 1)(int_array, int_array)` 的结果是 object dtype 而不是 int64？

**答案**：因为 `ufunc_frompyfunc` 把 `self->type_resolver` 设成了 `object_ufunc_type_resolver`（[umathmodule.c:L161](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/umathmodule.c#L161)），后者把所有输入输出 dtype 强制设为 `NPY_OBJECT`。Python 函数的返回值类型无法在编译期确定，所以只能存进 object 数组。

**练习 2**：`np.frompyfunc(len, 1, 1)` 包出来的 ufunc，对一个字符串数组调用会得到什么？

**答案**：会逐元素调用 `len()`，返回每个字符串的长度，但结果 dtype 是 `object`（元素是 Python int）。若想要 int64 数组，应改用原生 ufunc `np.char.str_len` 或对结果 `.astype(int)`。

---

### 4.5 ufunc 方法 reduce / accumulate / outer 入口

#### 4.5.1 概念说明

ufunc 不仅是「逐元素运算」，它还自带一组**方法**，能把一个二元（或更多元）ufunc 变成更高阶的操作：

| 方法 | 作用 | 例子（用 `np.add`） |
| --- | --- | --- |
| `reduce` | 沿指定轴把数组归约成一个值 | `np.add.reduce([1,2,3,4])` → `10` |
| `accumulate` | 沿指定轴累积，保留中间结果 | `np.add.accumulate([1,2,3,4])` → `[1,3,6,10]` |
| `reduceat` | 在指定下标处分段归约 | 用于 group-by 风格聚合 |
| `outer` | 对两个输入做外积（所有元素两两组合） | `np.add.outer([1,2],[10,20])` → `[[11,21],[12,22]]` |
| `at` | 无缓冲的就地累加（用于稀疏更新） | `np.add.at(a, idx, vals)` |

这些方法在 Python 层就是 `np.add.reduce(...)` 这种调用。它们在 C 层是 ufunc **类型**的方法表里登记的函数。

#### 4.5.2 核心流程

`np.add.reduce(a, axis=0)` 的调用链：

```text
np.add.reduce(a, axis, dtype, out, keepdims, initial, where)
  ├── ufunc_methods 表里 "reduce" -> ufunc_reduce (C)
  ├── 解析参数：array / axis / dtype / out / keepdims / initial / where
  ├── 检查 ufunc->identity：若无单位元且未给 initial，则报错
  ├── 选定 axis（可负、可为 tuple）
  └── 在该轴上反复应用 add 这条 ufunc，累加成结果
```

其中 `identity`（见 4.3）决定归约的起始值：`np.add.reduce` 从 0 开始累加，这个 0 就是 `np.add.identity`。若一个 ufunc 的 `identity` 是 `None`（如 `np.subtract`），又没传 `initial`，则 `reduce` 会报错，因为减法没有可交换的单位元。

#### 4.5.3 源码精读

[ufunc_object.c:L6694-L6709](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/ufunc_object.c#L6694-L6709) — `ufunc_methods[]` 方法表：`"reduce"` → `ufunc_reduce`、`"accumulate"` → `ufunc_accumulate`、`"reduceat"` → `ufunc_reduceat`、`"outer"` → `ufunc_outer`、`"at"` → `ufunc_at`。这就是为什么**每个** ufunc 对象都能调 `.reduce()`——这些方法挂在 ufunc 类型上，所有 ufunc 共享。

[ufunc_object.c:L3730-L3737](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/ufunc_object.c#L3730-L3737) — `reduce` 的参数解析：`array`、`|axis`、`|dtype`、`|out`、`|keepdims`、`|initial`、`|where`。`|` 表示可选关键字。这正是 `np.add.reduce` 文档里那些参数的来源。

[ufuncobject.h:L306-L309](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/include/numpy/ufuncobject.h#L306-L309) — `UFUNC_REDUCE=0`、`UFUNC_ACCUMULATE=1`、`UFUNC_REDUCEAT=2`、`UFUNC_OUTER=3`。这些常量在 C 层区分不同的「ufunc method」分支。

#### 4.5.4 代码实践

**目标**：用同一个 ufunc 的不同方法做归约、累积、外积。

```python
import numpy as np

a = np.array([1, 2, 3, 4])

print(np.add.reduce(a))            # 10      = 1+2+3+4
print(np.add.accumulate(a))        # [1 3 6 10]
print(np.multiply.accumulate(a))   # [1 2 6 24]

# 二维数组沿不同轴归约
b = np.arange(12).reshape(3, 4)
print(np.add.reduce(b, axis=0))    # 形状 (4,)，每列求和
print(np.add.reduce(b, axis=1))    # 形状 (3,)，每行求和

# 外积
print(np.add.outer([1, 2], [10, 20, 30]))
# [[11 21 31]
#  [12 22 32]]

# identity 决定 reduce 的起始值
print("add.identity   =", np.add.identity)        # 0
print("multiply.identity =", np.multiply.identity) # 1

# 没有 identity 的 ufunc 不带 initial 不能 reduce
try:
    np.subtract.reduce([1, 2, 3, 4])
except Exception as e:
    print("subtract.reduce 报错:", type(e).__name__)  # ZeroDivisionError/ValueError 视版本
```

**操作步骤**：

1. 逐段运行，对照每行注释核对结果。
2. 对 `b`（3×4）分别沿 `axis=0` 和 `axis=1` 归约，观察输出形状。
3. 观察 `np.add.identity` 和 `np.multiply.identity`，对应 [ufuncobject.h:L277-L282](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/include/numpy/ufuncobject.h#L277-L282)。

**预期结果**：

- `reduce(b, axis=0)` 形状 `(4,)`（每列相加）；`reduce(b, axis=1)` 形状 `(3,)`（每行相加）。
- `add.identity` 为 `0`，`multiply.identity` 为 `1`。
- `subtract.reduce` 不给 `initial` 会报错（`identity` 是 None）。

**待本地验证**：`np.subtract.reduce([1,2,3,4])` 在你的 NumPy 版本里具体抛 `ValueError` 还是其它异常，请实际运行确认（不同版本错误信息可能不同）。

#### 4.5.5 小练习与答案

**练习 1**：`np.add.accumulate([1,1,1,1,1])` 和 `np.add.reduce([1,1,1,1,1])` 分别等于什么？

**答案**：`accumulate` 返回 `[1,2,3,4,5]`（保留每步累加结果）；`reduce` 返回 `5`（只返回最终总和）。两者用的是同一条 `np.add` ufunc，只是方法不同（`UFUNC_ACCUMULATE` vs `UFUNC_REDUCE`）。

**练习 2**：为什么 `np.subtract.reduce([10, 1, 2])` 需要小心？

**答案**：减法没有可交换/可结合的单位元（`identity` 为 `None`），所以不带 `initial` 时 `reduce` 会报错。即便用 `initial=10`，结果依赖计算顺序（`((10-10)-1)-2`），多轴归约时无法重排，这也是 `identity` 枚举里区分 `PyUFunc_None`（不可重排）和 `PyUFunc_ReorderableNone`（可重排）的原因。

---

## 5. 综合实践

把本讲三个核心知识点（frompyfunc 包装、object dtype、广播与方法）串起来，完成下面这个字符串处理任务。

**任务**：你有一个 object 数组，存放若干用户名。请：

1. 写一个 Python 函数 `greet(name, prefix)`，返回 `"Hello, <prefix> <name>!"`。
2. 用 `np.frompyfunc` 把它包装成 nin=2、nout=1 的 ufunc `greet_uf`。
3. 对一个名字数组和一个前缀（标量，靠广播）调用 `greet_uf`，得到逐元素问候语。
4. 用**普通列表推导**实现同样的功能，对比两种写法。
5. 验证 `greet_uf` 的输出 dtype 是 object，并解释原因。

```python
import numpy as np

# 1) 目标函数
def greet(name, prefix):
    return f"Hello, {prefix} {name}!"

# 2) 包装成 ufunc
greet_uf = np.frompyfunc(greet, 2, 1)
print(type(greet_uf), greet_uf.__name__)   # numpy.ufunc, 'greet (vectorized)'

# 3) 对数组 + 标量前缀调用（标量靠广播作用到每个元素）
names = np.array(["Alice", "Bob", "Charlie"])
result = greet_uf(names, "Mr.")
print(result)
# array(['Hello, Mr. Alice!', 'Hello, Mr. Bob!', 'Hello, Mr. Charlie!'], dtype=object)

# 4) 等价的列表推导写法
list_result = [greet(n, "Mr.") for n in names]
print(list_result)
# ['Hello, Mr. Alice!', 'Hello, Mr. Bob!', 'Hello, Mr. Charlie!']

# 5) dtype 检查
print(result.dtype)                        # object

# 进阶：把两个数组广播（名字 (3,) × 称谓 (2,) -> 外积风格）
titles = np.array(["Mr.", "Ms."])
print(greet_uf(names[:, None], titles[None, :]))
# 3×2 二维 object 数组，每个元素是 (name, title) 的问候语
```

**需要观察的现象**：

- `greet_uf(names, "Mr.")` 中 `"Mr."` 是标量，被广播到每个名字上——这正是 ufunc 自动广播的体现（底层靠 `PyUFunc_On_Om` 里 `steps=0` 复用同一元素）。
- 进阶部分用 `names[:, None]` 和 `titles[None, :]` 造出可广播的二维形状，`greet_uf` 直接给出 3×2 结果，无需你自己写双重循环。
- 输出永远是 object dtype：对应源码 `object_ufunc_type_resolver`（[umathmodule.c:L45-L65](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/umathmodule.c#L45-L65)）。
- 列表推导只能处理一维且需要手写循环；ufunc 写法天然支持任意维度和广播，且能直接接 `.reduce`/`.accumulate`/`.outer`。

**预期结果**：`greet_uf` 版本与列表推导版本的元素内容完全一致；但前者 `type` 是 `numpy.ufunc`、支持广播、输出 dtype 为 object，后者是普通 `list`。

**性能提示**：由于 frompyfunc 的内核要回调 Python 函数，它**不会**比列表推导快。它的价值是「把任意 Python 函数纳入 ufunc 体系」，从而免费获得广播、`reduce`、`outer` 等高阶用法。若追求速度，应寻找或编写原生 ufunc（见下一讲 u4-l3）。

## 6. 本讲小结

- **ufunc 是 C 对象，不是 Python 函数**。`np.add`、`np.sin` 都是 `numpy.ufunc` 实例，由 C 扩展 `_multiarray_umath` 在构建时批量生成，`numpy/_core/umath.py` 只做再导出。
- **执行模型是「逐元素 + 广播」**：给定输入 dtype，ufunc 通过类型解析选出一条 C 内层循环，在广播后的形状上按 strides 推进指针完成计算。
- **`PyUFuncObject` 结构体的字段就是你在 Python 里看到的属性**：`nin`/`nout`/`nargs`、`functions`/`types`/`ntypes`、`identity`、`type_resolver` 等。
- **`identity` 决定能否 reduce**：加法是 0、乘法是 1、减法是 None（不可归约）。它对应头文件里的 `PyUFunc_One`/`PyUFunc_Zero`/`PyUFunc_None` 等枚举。
- **`np.frompyfunc(func, nin, nout)` 把任意 Python 可调用对象包装成 ufunc**：它复用 ufunc 的广播与方法体系，但内层循环 `PyUFunc_On_Om` 会回调 Python 函数，且 `type_resolver` 强制所有输入输出为 `object` dtype，所以**它不追求速度，只追求接口统一**。
- **`reduce`/`accumulate`/`outer` 等方法挂在 ufunc 类型上**（C 层 `ufunc_methods` 表），每个 ufunc 都能用，本质是反复应用同一条 ufunc。

## 7. 下一步学习建议

本讲把 ufunc 当作「黑盒对象」来用，侧重概念与 Python 接口。接下来建议：

- **u4-l2 ndarray 内存模型（C 层）**：下钻到 `PyArrayObject_fields`，看清 ufunc 内层循环读写的 `data`/`dimensions`/`strides` 到底长什么样，本讲里的 `ptrs[j] += steps[j]` 就是在操作它们。
- **u4-l3 ufunc 内部实现与类型解析**：精读 `ufunc_object.c` 的 vectorcall 与 `ufunc_type_resolution.c`，看清「根据输入 dtype 选 C 循环」的完整算法，以及新一代的 `ArrayMethod` 机制。
- **u4-l4 归约、累积与方法分发**：深入 `reduction.c`，看清 `np.add.reduce` 在 C 层是怎么沿轴迭代、怎么处理 `keepdims`/`where`/`initial` 的。
- **u8-l3 自定义 dtype 与 DType API**：如果你想把 `frompyfunc` 这种「object dtype 回调」升级成「真正的 C 内层循环 + 自定义类型」，那就需要学习新的 ArrayMethod/strided loop 注册机制。

阅读建议：先把本讲的代码实践全部跑一遍，再带着「frompyfunc 为何输出 object」「reduce 的起始值从哪来」这两个问题进入下一讲，你会发现源码里的每一个字段都对应得上。
