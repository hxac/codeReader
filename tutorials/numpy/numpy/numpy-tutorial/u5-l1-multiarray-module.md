# multiarray 模块全貌

## 1. 本讲目标

本讲聚焦 NumPy 中一个常被忽略却至关重要的「桥」——`multiarray`。学完后你应当能够：

- 说清 `numpy._core.multiarray` 这个 Python 模块与 C 扩展 `_multiarray_umath` 之间的再导出关系，明白为什么 `np.array`、`np.ndarray` 最终都指向同一个 C 对象。
- 在 `multiarraymodule.c` 中找到函数注册表 `array_module_methods[]` 与模块初始化函数 `_multiarray_umath_exec`，看懂一个 C 函数是如何变成 `np.xxx` 可调用对象的。
- 看懂 `_core/__init__.py` 对 `import multiarray` 失败时的诊断逻辑，理解它在「C 扩展缺失 / 版本不兼容 / 旧版 NumPy 影子覆盖」三种情形下分别给出什么提示。
- 能跟踪一个函数（如 `set_typeDict`、`may_share_memory`）从 Python 命名空间一路下钻到 C 实现的完整链路。

本讲承接 u4-l3（ufunc 内部实现），把视角从「单个 ufunc 对象」抬升到「装载 ufunc、ndarray、dtype 的整个 C 模块」。

## 2. 前置知识

阅读本讲前，你应当已经了解：

- **ndarray / ufunc / dtype** 是 C 扩展里定义的内置类型，而非纯 Python 类（见 u1-l4、u4-l1、u2-l2）。
- **再导出（re-export）**：一个 Python 模块用 `from .xxx import *` 把别处定义的名字搬到自己命名空间，再用 `__all__` 声明公开集合（见 u1-l3）。
- **C 扩展模块**：用 C 写、被 Python 解释器当作模块加载的动态库（`.so` / `.pyd`）。它通过 `PyModuleDef` 描述自己，通过 `PyMethodDef[]` 注册函数，通过 `PyType_Ready` 注册类型。
- **`__array_function__` 协议（NEP-18）**：允许非 NumPy 数组（如 Dask、CuPy）重载 `np.concatenate` 等顶层函数。本讲只触碰它的「调度器」一侧，深入留到 u7-l2。
- **多阶段模块初始化（PEP 489）**：现代 C 扩展用 `m_slots` 里的 `Py_mod_exec` 代替旧的 `PyInit` 里直接初始化，把「创建模块对象」与「填充模块内容」分成两步。

一个关键术语需要先点明：**`_multiarray_umath`** 是真正的 C 扩展模块名（编译产物 `_multiarray_umath.*.so`），而 **`numpy._core.multiarray`** 是一个纯 Python 的薄壳模块，专门用来「伪装」成旧的 `multiarray` 命名空间以保持向后兼容。二者不是一回事，但前者是后者的全部内容来源。

## 3. 本讲源码地图

| 文件 | 作用 | 语言 |
|------|------|------|
| `numpy/_core/multiarray.py` | Python 薄壳：再导出 C 扩展的名字，并用调度装饰器包装部分函数 | Python |
| `numpy/_core/src/multiarray/multiarraymodule.c` | C 扩展入口：注册函数表、初始化类型、导出 C-API 胶囊 | C |
| `numpy/_core/__init__.py` | `_core` 包装配：包裹 `import multiarray` 的容错与诊断 | Python |
| `numpy/_core/overrides.py`（辅助） | 提供 `array_function_from_dispatcher` 调度装饰器 | Python |
| `numpy/_core/src/multiarray/descriptor.c`（辅助） | `array_set_typeDict` 的真实 C 实现，说明「注册表里的函数未必定义在本文件」 | C |

## 4. 核心概念与源码讲解

### 4.1 multiarray Python 封装：再导出与调度包装

#### 4.1.1 概念说明

历史上 NumPy 有两个独立的 C 扩展：`multiarray`（数组）和 `umath`（ufunc）。v1.16 起二者合并为单一的 `_multiarray_umath`。但大量外部代码仍写 `from numpy._core import multiarray`，于是 NumPy 用一个同名的纯 Python 文件来「复刻」旧命名空间——它的全部内容都来自 C 扩展，自己几乎不写算法。

这个薄壳做三件事：

1. **再导出**：`from ._multiarray_umath import *` 把 C 扩展暴露的名字搬过来。
2. **修 `__module__`**：把函数和 ufunc 的 `__module__` 改成 `'numpy'`，让 `repr`、pickle、文档都显示成「来自 numpy」而非「来自 `_multiarray_umath`」。
3. **调度包装**：对少数需要支持 `__array_function__` 的函数，用一个装饰器把 C 实现和一个 Python「调度函数」绑在一起。

#### 4.1.2 核心流程

```
_multiarray_umath (C 扩展)
   │  array, zeros, ndarray, dtype, may_share_memory, set_typeDict, ...
   │
   │  from ._multiarray_umath import *
   ▼
multiarray.py (Python 薄壳)
   │  ① 星号再导出 + 显式补私有名
   │  ② __all__ 声明公开集合
   │  ③ 改写 __module__ → 'numpy'
   │  ④ @array_function_from_c_func_and_dispatcher(C函数) 包装调度函数
   ▼
_core/__init__.py  →  numpy/__init__.py  →  np.<name>
```

注意第 ④ 步只作用于**少数**函数。大多数函数（如 `set_typeDict`、`array`、`zeros`）直接走第 ① 步星号再导出，没有任何 Python 包装——它们就是 C 函数本身。这个区别是本讲实践任务的关键。

#### 4.1.3 源码精读

先看薄壳的开头与再导出：

[numpy/_core/multiarray.py:1-12](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/multiarray.py#L1-L12) —— 模块 docstring 直言不讳地说明它的存在意义是「为向后兼容复刻旧命名空间」，核心动作是 `from ._multiarray_umath import *`。

[numpy/_core/multiarray.py:17-28](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/multiarray.py#L17-L28) —— 星号导入拿不到以 `_` 开头的私有名，故显式逐个导入 `_ARRAY_API`、`_reconstruct`、`from_dlpack` 等。注释提到 `_get_ndarray_c_version` 是「半公开」、故意不进 `__all__`。

[numpy/_core/multiarray.py:30-50](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/multiarray.py#L30-L50) —— `__all__` 列出全部公开名字，注意其中既有 `set_typeDict`（纯再导出），也有 `may_share_memory`（将被调度包装覆盖）。下面会看到，`may_share_memory` 在文件后半段被同名 Python 函数「重新定义」了。

[numpy/_core/multiarray.py:54-78](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/multiarray.py#L54-L78) —— 改写 `__module__`。比如 `array.__module__ = 'numpy'`，使得 `np.array.__module__` 显示 `'numpy'` 而非 `'_multiarray_umath'`。这一步对 pickle 与文档工具很重要：pickle 通过 `__module__` + `__qualname__` 定位反序列化入口。

[numpy/_core/multiarray.py:81-105](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/multiarray.py#L81-L105) —— `_override___module__()` 遍历一大串 ufunc 名字，把它们的 `__module__` 与 `__qualname__` 统一改成 `numpy`。注意它通过 `globals()` 取出已再导出的 ufunc 对象——这印证了 ufunc 也是从 C 扩展搬来的。

接下来是本模块唯一一点「逻辑」——调度装饰器：

[numpy/_core/multiarray.py:110-112](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/multiarray.py#L110-L112) —— 用 `functools.partial` 固定 `module='numpy'`、`docs_from_dispatcher=True`、`verify=False`。`verify=False` 是因为 C 函数没有可供 introspect 的 Python 签名，无法与调度函数比对参数表。

[numpy/_core/multiarray.py:115-194](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/multiarray.py#L115-L194) —— `empty_like` 的调度函数。注意它的函数体只有 `return (prototype,)`——它**不做计算**，只返回「参与 `__array_function__` 分派的参数」。真正的计算是装饰器第一个参数 `_multiarray_umath.empty_like`（C 函数）。docstring 之所以写在调度函数上，是因为 `docs_from_dispatcher=True` 会把它拷到最终公开对象上。

调度装饰器本身定义在 `overrides.py`：

[numpy/_core/overrides.py:180-188](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/overrides.py#L180-L188) —— `array_function_from_dispatcher` 只是把参数顺序翻转后转调 `array_function_dispatch`。

[numpy/_core/overrides.py:145-175](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/overrides.py#L145-L175) —— 真正的装饰器内层：用 `_ArrayFunctionDispatcher(dispatcher, implementation)` 把「调度函数」与「C 实现」绑成一个可调用对象，加入全局集合 `ARRAY_FUNCTIONS`，并用 `functools.update_wrapper` 让它伪装成 C 实现（继承 `__name__` 等）。`_ArrayFunctionDispatcher` 本身又是 C 扩展里的类型（见 [overrides.py:6-10](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/overrides.py#L6-L10)）。

> 小结：`multiarray.py` 里凡是带 `@array_function_from_c_func_and_dispatcher` 装饰器的函数，公开对象是「调度器 + C 实现」的复合体；其余名字则是 C 函数/类型的直接再导出。

#### 4.1.4 代码实践

**目标**：在 `multiarray.py` 中分别定位 `set_typeDict` 与 `may_share_memory` 的「来源方式」，体会两种不同的暴露路径。

**步骤**：

1. 在仓库根目录打开 Python（已构建好 NumPy 的环境），执行：
   ```python
   import numpy as np
   from numpy._core import multiarray as ma
   print(type(ma.set_typeDict), ma.set_typeDict.__module__)
   print(type(ma.may_share_memory), ma.may_share_memory.__module__)
   ```
2. 在 `numpy/_core/multiarray.py` 中用编辑器搜索 `def may_share_memory`，确认它有一个调度装饰器（4.1.3 已给出位置）。
3. 再搜索 `set_typeDict`，确认它**没有** `def` 定义，只出现在 `__all__`（第 48 行）与星号导入中。

**需要观察的现象**：

- `set_typeDict` 的类型是 `builtin_function_or_method`，`__module__` 显示 `numpy._core._multiarray_umath`（或 `numpy`，取决于版本修写时机）——它是 C 函数本体。
- `may_share_memory` 的类型是 `numpy._core._multiarray_umath._ArrayFunctionDispatcher`（一个 C 定义的调度器类型），而非 `builtin_function_or_method`。

**预期结果**：二者「来源方式」不同——`set_typeDict` 是纯再导出，`may_share_memory` 是调度包装。这解释了为什么前者没有 Python 层 docstring 控制权而后者有。

**待本地验证**：`__module__` 的具体字符串在不同 NumPy 2.x 小版本上可能略有差异，以你本机输出为准。

#### 4.1.5 小练习与答案

**练习 1**：`multiarray.py` 为什么要专门把 `from_dlpack.__module__` 改成 `'numpy'`？如果不改，会对哪个常见操作造成困扰？

> **答案**：`from_dlpack` 是零拷贝互操作入口，外部库常常按 `module + qualname` 拼装对它的引用（如 pickle、某些 dispatch 机制）。若 `__module__` 指向 `_multiarray_umath`，反序列化或按名查找时会找不到对象。改成 `'numpy'` 让它落在公开命名空间。

**练习 2**：`_override___module__()` 为什么不直接在 `__all__` 那批名字上循环，而是单独列了一份 ufunc 名单？

> **答案**：ufunc 对象需要同时改 `__module__` 与 `__qualname__` 两个属性（因为 ufunc 的 `__qualname__` 默认可能为空或带 C 内部名），普通函数只需改 `__module__`；且 ufunc 名单是确定的、与 `__all__` 集合不完全重合（`__all__` 里还有常量、类型等非 ufunc 项），故单独维护。

### 4.2 C 模块入口：multiarraymodule.c 的注册与初始化

#### 4.2.1 概念说明

`multiarraymodule.c` 是 `_multiarray_umath` 这个 C 扩展的源码主文件（5450 行）。它做四件事：

1. **注册函数**：用一张 `PyMethodDef[]` 表把 C 函数名映射到 `PyCFunction` 指针，声明调用约定（`METH_FASTCALL`、`METH_KEYWORDS` 等）。
2. **初始化类型**：在模块执行槽里调用 `PyType_Ready` 注册 `PyArray_Type`（ndarray）、`PyUFunc_Type`、`PyArrayDescr_Type`、`NpyIter_Type` 等类型对象。
3. **导出常量**：把 `MAXDIMS`、`ALLOW_THREADS`、`MAY_SHARE_BOUNDS` 等整型常量塞进模块字典。
4. **导出 C-API**：把两张函数指针表 `PyArray_API`、`PyUFunc_API` 封装成 `PyCapsule`，挂在 `_ARRAY_API`、`_UFUNC_API` 名下，供其他 C 扩展调用。

注意：**注册表里登记的函数，其实现未必都在本文件**。比如 `set_typeDict` 的注册在 `multiarraymodule.c`，但实现却在 `descriptor.c`。这是大型 C 代码库常见的「集中注册、分散实现」模式。

#### 4.2.2 核心流程

```
PyInit__multiarray_umath()          ← Python 解释器加载 .so 时调用
        │  返回 PyModuleDef_Init(&moduledef)   （只创建模块对象骨架）
        ▼
解释器按 m_slots 执行 Py_mod_exec:
        │
        ▼
_multiarray_umath_exec(m)           ← 真正的初始化
   ├─ npy_cpu_init()                CPU 特性探测
   ├─ initialize_numeric_types()    注册标量类型
   ├─ PyType_Ready(&PyArray_Type)   注册 ndarray
   ├─ ADDCONST(MAXDIMS) ...         塞常量
   ├─ PyDict_SetItemString(d,"ndarray",&PyArray_Type)  把类型挂到模块字典
   ├─ set_typeinfo(d)               暴露 typeinfo
   └─ PyCapsule_New(PyArray_API) → d["_ARRAY_API"]     导出 C-API
```

模块函数的调用路径（以 `np.may_share_memory` 为例）：

```
np.may_share_memory(a,b)
  → multiarray.may_share_memory  （_ArrayFunctionDispatcher，4.1 节）
  → _multiarray_umath.may_share_memory  （查 array_module_methods[]）
  → array_may_share_memory  (multiarraymodule.c:4325)
  → array_shares_memory_impl(... NPY_MAY_SHARE_BOUNDS, raise=0)
  → solve_may_share_memory(...)
```

#### 4.2.3 源码精读

先看模块定义与入口：

[numpy/_core/src/multiarray/multiarraymodule.c:5440-5450](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/multiarraymodule.c#L5440-L5450) —— `moduledef` 把模块名定为 `_multiarray_umath`，绑定 `m_methods = array_module_methods` 与 `m_slots = _multiarray_umath_slots`。`PyInit__multiarray_umath` 极简：只调 `PyModuleDef_Init`。这是 PEP 489 多阶段初始化的标准写法——`PyInit` 不做实质工作，真正的初始化推迟到 exec 槽。

[numpy/_core/src/multiarray/multiarraymodule.c:5428-5438](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/multiarraymodule.c#L5428-L5438) —— 插槽表声明：`Py_mod_exec` 指向 `_multiarray_umath_exec`；Python 3.12+ 声明「不支持子解释器多实例」；Python 3.13+ 声明「可在无 GIL 下运行」。

接着看函数注册表（本讲最核心的一张表）：

[numpy/_core/src/multiarray/multiarraymodule.c:4618-4733](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/multiarraymodule.c#L4618-L4733) —— `array_module_methods[]`。每一形如 `{"名字", (PyCFunction)C函数指针, 调用约定标志, NULL}`。重点看几条：

- `{"set_typeDict", (PyCFunction)array_set_typeDict, METH_VARARGS, NULL}`（[L4631-L4633](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/multiarraymodule.c#L4631-L4633)）——注册名 `set_typeDict` 指向 C 函数 `array_set_typeDict`。
- `{"may_share_memory", (PyCFunction)array_may_share_memory, METH_VARARGS | METH_KEYWORDS, NULL}`（[L4730-L4732](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/multiarraymodule.c#L4730-L4732)）——注册名 `may_share_memory` 指向 `array_may_share_memory`。
- `{"array", (PyCFunction)array_array, METH_FASTCALL | METH_KEYWORDS, NULL}`（[L4634-L4636](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/multiarraymodule.c#L4634-L4636)）——注意 `np.array` 用的是 `METH_FASTCALL`（向量快速调用），比 `METH_VARARGS` 的元组拆包更快。

调用约定标志的含义：

| 标志 | 含义 |
|------|------|
| `METH_NOARGS` | 不接受参数（除 self） |
| `METH_VARARGS` | 接收位置参数元组 |
| `METH_KEYWORDS` | 额外接收关键字字典 |
| `METH_FASTCALL` | 接收 `PyObject *const *args` 数组，避免元组分配 |

现在看 `may_share_memory` 的 C 实现，它体现了「注册在一个文件、实现可在同文件」的情况：

[numpy/_core/src/multiarray/multiarraymodule.c:4317-4328](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/multiarraymodule.c#L4317-L4328) —— `array_shares_memory` 与 `array_may_share_memory` 都是对 `array_shares_memory_impl` 的薄封装，区别仅在两个参数：`NPY_MAY_SHARE_EXACT`（精确求解）vs `NPY_MAY_SHARE_BOUNDS`（只查内存边界）；以及 `raise_exceptions=1` vs `0`。这正是 `np.shares_memory`（精确但可能很慢）与 `np.may_share_memory`（快但可能有假阳性）的语义差异源头。

[numpy/_core/src/multiarray/multiarraymodule.c:4204-4314](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/multiarraymodule.c#L4204-L4314) —— `array_shares_memory_impl` 的完整逻辑：解析 `self/other/max_work`，用 `PyArray_FROM_O` 把非数组对象转成数组（从而支持任意暴露数组接口的对象），释放 GIL 后调 `solve_may_share_memory`，再把 `mem_overlap_t` 枚举映射成 `True/False` 或抛 `TooHardError`。

而 `set_typeDict` 的实现**不在本文件**，体现了「分散实现」：

[numpy/_core/src/multiarray/descriptor.c:142-156](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/descriptor.c#L142-L156) —— `array_set_typeDict` 真正定义于此。它接收一个 dict，存入静态变量 `typeDict` 并自增引用。这个 `typeDict` 是 dtype 字符串名到标量类型的映射表，由 `_core/__init__.py` 在导入时调用 `set_typeDict(nt.sctypeDict)` 灌入（见 4.3.3）。函数声明则在 [descriptor.h:47](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/descriptor.h#L47)，`multiarraymodule.c` 通过 include 该头文件拿到原型，才能在注册表里引用它。

最后看模块执行槽 `_multiarray_umath_exec` 的关键片段：

[numpy/_core/src/multiarray/multiarraymodule.c:5027-5037](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/multiarraymodule.c#L5027-L5037) —— 用静态变量 `module_loaded` 实现「每进程只加载一次」的 opt-out，重复加载直接抛 `ImportError`（这个异常消息在 4.3 节会被 `_core/__init__.py` 特判放行）。

[numpy/_core/src/multiarray/multiarraymodule.c:5116-5128](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/multiarraymodule.c#L5116-L5128) —— 初始化转换表与数值类型后，`PyType_Ready(&PyArray_Type)` 把 ndarray 类型对象「就绪化」（填充 `tp_*` 槽、建立 MRO），再 `setup_scalartypes` 注册全部标量类型。

[numpy/_core/src/multiarray/multiarraymodule.c:5212-5236](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/multiarraymodule.c#L5212-L5236) —— `ADDCONST(NAME)` 宏把 `NPY_<NAME>` 整型常量转成 Python `int` 塞进模块字典，于是 `np.MAXDIMS`、`np.ALLOW_THREADS`、`np.MAY_SHARE_BOUNDS` 等就有了值。这些常量随后又被 `multiarray.py` 的 `__all__` 再导出。

[numpy/_core/src/multiarray/multiarraymodule.c:5238-5248](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/multiarraymodule.c#L5238-L5248) —— 把已就绪的类型对象挂到模块字典：`"ndarray" → PyArray_Type`、`"dtype" → PyArrayDescr_Type`、`"nditer" → NpyIter_Type`、`"broadcast" → PyArrayMultiIter_Type`。这就是 `np.ndarray`、`np.dtype` 等「类」的来源。

[numpy/_core/src/multiarray/multiarraymodule.c:5406-5420](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/multiarraymodule.c#L5406-L5420) —— 导出两张 C-API 表：`PyArray_API`、`PyUFunc_API` 各自封进 `PyCapsule`，挂到模块字典 `_ARRAY_API`、`_UFUNC_API`。其它 C 扩展（包括用户自己写的）正是通过 `import_array()` 取出这个胶囊，拿到 NumPy 的 C 函数指针表，从而能调用 `PyArray_SimpleNew` 等 API。这是 u8-l2（C-API）的入口。

#### 4.2.4 代码实践

**目标**：把 4.1.4 找到的 Python 名字与 C 注册表对上号，验证「注册表 → 实现」的映射，并体会「实现可跨文件」。

**步骤**：

1. 在 `multiarraymodule.c` 的注册表（[L4618 起](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/multiarraymodule.c#L4618)）中定位三行：
   - `set_typeDict` → `array_set_typeDict`（L4631）
   - `may_share_memory` → `array_may_share_memory`（L4730）
   - `shares_memory` → `array_shares_memory`（L4727）
2. 在仓库内搜索 `array_set_typeDict` 的定义位置：
   ```
   git grep -n "array_set_typeDict" -- 'numpy/_core/src/multiarray/*.c' 'numpy/_core/src/multiarray/*.h'
   ```
   预期发现定义在 `descriptor.c:143`、声明在 `descriptor.h:47`。
3. 搜索 `array_may_share_memory` 的定义：
   ```
   git grep -n "^array_may_share_memory" -- 'numpy/_core/src/multiarray/*.c'
   ```
   预期发现定义就在 `multiarraymodule.c:4325`。
4. 在 Python 中验证常量来自 `ADDCONST`：
   ```python
   import numpy as np
   print(np.MAY_SHARE_BOUNDS, np.MAY_SHARE_EXACT)   # 0 1
   print(np.MAXDIMS)                                 # 32（或当前上限）
   ```

**需要观察的现象**：

- `set_typeDict` 的注册与实现分处两个文件；`may_share_memory` 的注册与实现在同一文件。
- `np.MAY_SHARE_BOUNDS` 与 `np.MAY_SHARE_EXACT` 是两个小整数，恰好对应 4.2.3 中 `array_shares_memory` / `array_may_share_memory` 传给 `array_shares_memory_impl` 的第二参数。

**预期结果**：你能画出每个 Python 名字 → 注册表行 → C 实现函数 → 实现文件 的四列对照表。

**待本地验证**：`np.MAXDIMS` 的具体数值随版本可能调整（当前为 32）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `np.array` 用 `METH_FASTCALL | METH_KEYWORDS`，而 `set_typeDict` 只用 `METH_VARARGS`？

> **答案**：`np.array` 是热路径上的高频函数，`METH_FASTCALL` 直接接收 C 数组形式的参数，省去构造位置参数元组的开销；`set_typeDict` 仅在导入时调用一次，性能无关紧要，用最简单的 `METH_VARARGS`（接收元组）即可。

**练习 2**：`ADDCONST` 宏把 `NPY_MAXDIMS` 暴露为模块字典里的 `MAXDIMS`。如果有人误把同一行复制两次，会发生什么？

> **答案**：`PyDict_SetItemString` 是覆盖语义，第二次只是用同样的值覆盖一次，不会报错，但 `Py_DECREF(s)` 仍会执行，引用计数保持平衡。不过这属于无意义的重复，代码审查会剔除。

**练习 3**：`_multiarray_umath_exec` 为什么要先 `PyType_Ready(&PyArray_Type)`，再 `PyDict_SetItemString(d, "ndarray", &PyArray_Type)`？顺序能反过来吗？

> **答案**：`PyType_Ready` 负责填充类型的 `tp_*` 槽、建立 MRO、分配 `tp_basicsize` 等，未就绪的类型对象不可被 Python 代码安全使用。若先把它塞进模块字典，Python 代码可能在就绪前访问到它而触发未定义行为。故必须先就绪再暴露。

### 4.3 导入失败诊断：_core/__init__.py 的容错

#### 4.3.1 概念说明

`_core/__init__.py` 是 `_core` 包的装配入口。它最显眼的工作不是导入成功时的「装配」，而是导入**失败**时的「诊断」。NumPy 的 C 扩展加载失败是用户最常踩的坑之一——可能是没编译、Python 版本不匹配、平台 ABI 不一致，或者环境里混装了新旧 NumPy。这个文件把一团乱麻的 `ImportError` 翻译成一段带排查链接的人话。

它还做两件小事：在导入最早期设置 `OPENBLAS_MAIN_FREE` 环境变量绕开 OpenBLAS 线程亲和性问题；在导入成功后调 `set_typeDict` 把 dtype 字典灌进 C 层（衔接 4.2 的 `array_set_typeDict`）。

#### 4.3.2 核心流程

```
_core/__init__.py 执行
  ├─ 设置 OPENBLAS_MAIN_FREE（putenv，避免竞态）         [L9-L22]
  ├─ try: from . import multiarray                        [L23-L24]
  │    except ImportError:
  │      ├─ 若消息 == "cannot load module more than once" → 直接 raise（放行多阶段重入保护）
  │      ├─ 扫描 __path__ 下 _multiarray_umath* 文件
  │      │    ├─ 0 个  → "没编译成功"
  │      │    └─ N 个  → "编译了但不兼容当前 Python/平台"
  │      └─ 拼出大段诊断消息（含 Python/NumPy 版本、排查链接）后 raise  [L33-L85]
  ├─ finally: unsetenv 清理环境变量                       [L86-L91]
  ├─ from . import umath                                  [L93]
  ├─ 校验 multiarray 与 umath 都带 _multiarray_umath 属性  [L97-L105]（防旧 NumPy 影子）
  └─ multiarray.set_typeDict(nt.sctypeDict)               [L110]（灌 dtype 字典）
```

#### 4.3.3 源码精读

[numpy/_core/__init__.py:9-22](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/__init__.py#L9-L22) —— 在导入最早期用 `os.putenv` 设置 `OPENBLAS_MAIN_FREE=1`，阻止 OpenBLAS 把主线程绑核从而限制 Python 多线程/多进程只用一核。注释特别说明：用 `putenv` 而非更新 `os.environ`，是为了避免与 `unsetenv` 之间的竞态（gh-30627）。`env_added` 记录哪些键是本进程新加的，便于 finally 里精准清理。

[numpy/_core/__init__.py:23-31](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/__init__.py#L23-L31) —— `try: from . import multiarray`。注意特判：若异常消息恰好是 `"cannot load module more than once per process"`（来自 4.2.3 的 `module_loaded` 守卫），则直接 `raise`，不做诊断——这是子解释器重入时的合法 opt-out，不是真正的安装错误。

[numpy/_core/__init__.py:33-54](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/__init__.py#L33-L54) —— 区分两种失败：用 `os.listdir` 扫描 `__path__` 下以 `_multiarray_umath` 开头的文件。**找不到任何候选**→判定「没编译成功」；**找到候选但仍加载失败**→判定「编译了但不兼容」，并打印候选文件名、用 `cache_tag` 推断的 Python 标签、`sys.platform` 平台标签，帮助用户定位 ABI 不匹配。

[numpy/_core/__init__.py:58-85](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/__init__.py#L58-L85) —— 拼装诊断消息：包含 Python 版本与可执行路径、NumPy 版本（`__version__` 来自 `numpy.version`）、排查文档链接 `https://numpy.org/devdocs/user/troubleshooting-importerror.html`，并用 `raise ImportError(msg) from exc` 保留原始异常链。这段消息就是你平时 `import numpy` 失败时看到的长文。

[numpy/_core/__init__.py:86-91](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/__init__.py#L86-L91) —— `finally` 块用 `unsetenv` 清理自己加的环境变量，随后 `del` 掉临时名，保持命名空间干净。

[numpy/_core/__init__.py:97-105](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/__init__.py#L97-L105) —— 反影子校验：检查 `multiarray` 与 `umath` 都有 `_multiarray_umath` 属性。旧版 NumPy 的 `multiarray` 是独立 C 扩展、不带这个属性；若检测到，说明环境里混入了旧版 NumPy，提示「反复 uninstall 直到没有再 reinstall」。

[numpy/_core/__init__.py:107-110](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/__init__.py#L107-L110) —— 导入 `numerictypes` 拿到 `sctypeDict`，调 `multiarray.set_typeDict(nt.sctypeDict)` 把它灌进 C 层的 `typeDict` 静态变量（即 4.2.3 的 `array_set_typeDict`）。这一步把 Python 侧维护的「dtype 名→标量类型」表交给 C 层，使 C 的字符串→dtype 解析能查到它。这行代码是 4.1 与 4.2 两个模块的真正交汇点。

> 小结：`_core/__init__.py` 是「装配 + 诊断」双职责——成功时把 multiarray、umath、numerictypes 等拼起来并灌 dtype 字典；失败时把底层 `ImportError` 翻译成可操作的排查指引。

#### 4.3.4 代码实践

**目标**：在不破坏环境的前提下，观察诊断逻辑的两个侧面——成功路径下的 `set_typeDict` 灌注，以及失败路径下的消息结构。

**步骤**：

1. **成功路径**：在已装好 NumPy 的环境里执行：
   ```python
   import numpy as np
   from numpy._core import multiarray as ma
   # set_typeDict 已在 _core/__init__.py 导入时被调用过，typeDict 现已就位
   # 用一个 dtype 字符串验证 C 层能查到表
   print(np.dtype("float64"))     # 依赖 typeDict
   print(np.dtype("double"))      # 别名，也依赖 typeDict
   ```
2. **阅读型实践**：打开 `_core/__init__.py`，对照 4.3.3 的行号，在源码里标注出「OPENBLAS 设置 / 扫描候选文件 / 拼诊断消息 / 反影子校验 / 灌 typeDict」五段。
3. **失败路径（只读模拟，不要真去破坏环境）**：阅读 [L58-L85](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/__init__.py#L58-L85) 的消息模板，回答：消息里会包含哪四个对排查有用的信息项？

**需要观察的现象**：

- `np.dtype("double")` 能正常返回 `dtype('float64')`，说明 `sctypeDict` 里的别名已被 C 层 `typeDict` 收录——这正是 `set_typeDict` 调用的效果。
- 诊断消息模板包含：① 是否找到编译产物及候选文件名；② 排查文档链接；③ Python 版本与可执行路径；④ NumPy 版本号。

**预期结果**：你能解释为什么删掉 `multiarray.set_typeDict(nt.sctypeDict)` 这一行后，`np.dtype("double")` 这类基于别名的查询会失效。

**待本地验证**：不要真的去删源码行验证（本讲禁止改源码）；可通过阅读 `array_set_typeDict` 与 `typeDict` 的使用点推理得出结论。

#### 4.3.5 小练习与答案

**练习 1**：`_core/__init__.py` 为什么用 `try/except ImportError` 包裹 `from . import multiarray`，而不是让它直接抛？

> **答案**：直接抛的是 C 扩展底层的原始 `ImportError`，消息晦涩（如 `undefined symbol: ...`）。包裹后能扫描文件系统判断「没编译」还是「不兼容」，并附上 Python/NumPy 版本与排查链接，大幅降低用户排错成本。

**练习 2**：特判 `exc.msg == "cannot load module more than once per process"` 的目的是什么？

> **答案**：`multiarraymodule.c` 用 `module_loaded` 静态变量阻止同一进程二次加载 `_multiarray_umath`（多阶段初始化的 opt-out）。这种情况下抛 `ImportError` 是设计内的合法行为，不应被当成「安装错误」去拼诊断消息，故直接 `raise` 放行。

**练习 3**：`OPENBLAS_MAIN_FREE` 为什么用 `putenv` 设、`finally` 里 `unsetenv` 清，而不是写进 `os.environ`？

> **答案**：更新 `os.environ` 会触发其内部与 C 环境表的同步，在某些路径下与并发 `unsetenv` 存在竞态（gh-30627）。直接 `putenv` 操作 C 层 `environ`、用 `env_added` 列表记账、`finally` 里精准 `unsetenv`，规避了竞态，又保证只在导入窗口内生效，不污染用户后续环境。

## 5. 综合实践

把本讲三个模块串起来，完成一次「端到端调用链追踪」。以 `np.may_share_memory(a, b)` 为对象，画出从 Python 顶层到 C 实现的完整链路，并写出每一步对应的源码位置。

**任务**：

1. **顶层入口**：确认 `np.may_share_memory` 来自哪里。在 `numpy/__init__.py`（或其再导出链）中确认它最终来自 `numpy._core.multiarray`。
2. **薄壳层**：在 [multiarray.py:1400-1440](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/multiarray.py#L1400-L1440) 找到 `may_share_memory` 的调度函数，指出：
   - 装饰器第一个参数 `_multiarray_umath.may_share_memory` 是什么角色（实现 vs 调度）？
   - 函数体 `return (a, b)` 返回的元组有什么用（提示：`__array_function__` 分派的参数收集）？
3. **C 注册层**：在 [multiarraymodule.c:4730-4732](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/multiarraymodule.c#L4730-L4732) 找到注册表项，写出注册名、C 函数名、调用约定标志。
4. **C 实现层**：在 [multiarraymodule.c:4324-4328](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/multiarraymodule.c#L4324-L4328) 找到 `array_may_share_memory`，指出它传给 `array_shares_memory_impl` 的两个关键参数（`NPY_MAY_SHARE_BOUNDS` 与 `raise_exceptions=0`）如何决定「快但有假阳性」的语义。
5. **对比练习**：对 `np.set_typeDict` 做同样的追踪，指出它**没有**第 2 步的调度函数（纯星号再导出），且第 4 步的实现在 [descriptor.c:142-156](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/descriptor.c#L142-L156) 而非 `multiarraymodule.c`。
6. **装配层**：说明 `_core/__init__.py` 在哪一行调用了 `set_typeDict`（[L110](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/__init__.py#L110)），把 Python 侧 `sctypeDict` 灌进 C 层 `typeDict`。

**交付物**：一张五列对照表——`Python 名字 | 薄壳层位置 | C 注册表行 | C 实现函数 | 实现文件`，覆盖 `may_share_memory`、`shares_memory`、`set_typeDict`、`array` 四个名字。

**预期结果**：你会发现 `may_share_memory` 与 `shares_memory` 共享同一个 `array_shares_memory_impl`，仅在两个参数上不同；而 `set_typeDict` 走的是完全不同的「无调度 + 跨文件实现」路径。这种对照能帮你判断任意一个 `np.xxx` 函数该去哪里读源码。

## 6. 本讲小结

- `numpy._core.multiarray` 是一个纯 Python 薄壳，其内容几乎全部来自 C 扩展 `_multiarray_umath`，存在的意义是复刻 v1.16 合并前的旧 `multiarray` 命名空间以保向后兼容。
- 薄壳做三件事：星号再导出 C 名字、改写 `__module__` 为 `'numpy'`、对少数函数用 `array_function_from_c_func_and_dispatcher` 绑定 `__array_function__` 调度器。带装饰器的是「调度器+C实现」复合体，不带的则是 C 函数本体。
- `multiarraymodule.c` 用 `array_module_methods[]` 注册表把名字映射到 `PyCFunction`，调用约定（`METH_FASTCALL`/`METH_VARARGS`/`METH_KEYWORDS`）决定参数传递方式与性能。
- 模块初始化走 PEP 489 多阶段：`PyInit__multiarray_umath` 只创建骨架，`_multiarray_umath_exec` 槽做实质工作——`PyType_Ready` 注册类型、`ADDCONST` 塞常量、`PyDict_SetItemString` 暴露类型、`PyCapsule` 导出 `_ARRAY_API`/`_UFUNC_API` 两张 C-API 表。
- 注册表里的函数实现未必在本文件：`set_typeDict` 注册于 `multiarraymodule.c`、实现在 `descriptor.c`，体现「集中注册、分散实现」。
- `_core/__init__.py` 是装配+诊断双职责：成功时调 `set_typeDict` 把 dtype 字典灌进 C 层；失败时扫描文件系统区分「没编译 / 不兼容」并拼出带版本与链接的诊断消息，还特判多阶段重入守卫与旧版 NumPy 影子。

## 7. 下一步学习建议

- **u5-l2 数组打印与 dragon4 浮点格式化**：继续在 `_core` 子系统里走，看 `arrayprint.py` 如何用本讲再导出的 `format_longfloat`、`dragon4_positional` 等 C 函数。
- **u7-l2 `__array_function__` 与函数调度**：本讲只用了调度装饰器的「外壳」，下一讲深入 `_ArrayFunctionDispatcher` 的运行时分派逻辑与 `like=` 参数机制。
- **u8-l1 Meson 构建系统深入**：本讲提到 `_multiarray_umath.*.so` 是编译产物，u8-l1 讲它如何由 `meson.build` + Cython + C 编译器生成，以及 `_ARRAY_API` 胶囊的 ABI 版本如何受 `C_ABI_VERSION` 控制。
- **u8-l2 C-API 头文件体系**：本讲末尾的 `_ARRAY_API` 胶囊是其它 C 扩展调用 NumPy 的入口，u8-l2 讲 `import_array()` 如何取出这张表并用 `PyArray_SimpleNew` 等宏操作数组。
- 建议继续阅读：`numpy/_core/src/multiarray/multiarraymodule.c` 中 `_multiarray_umath_exec` 的剩余片段（`set_typeinfo`、`initialize_numeric_types`），以及 `descriptor.c` 中 `typeDict` 的查询点，巩固「Python 名字 → C 实现」的检索能力。
