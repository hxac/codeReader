# 切分与切片：partition / rpartition / slice

## 1. 本讲目标

本讲承接 [u2-l10](u2-l10-strip-functions.md) 的裁剪族，讲清 `numpy.strings` 里剩下的三个「把字符串拆开」的函数：`partition`、`rpartition`、`slice`。学完后你应当能够：

- 看懂 `partition` / `rpartition` 为什么用**结构化 dtype**（`f0` / `f1` / `f2` 三个字段）一次产出三段输出；
- 手算出一个具体输入下 `partition` 构造出的 `out_dtype` 字符串（形如 `"U5,U1,U8"`）；
- 理解 `slice` 如何用 `np._NoValue` 哨兵实现 Python `slice` 对象「只给一个参数当作 `stop`」的语义；
- 看懂 `slice` 怎样把 `None` 起止值按 `step` 的正负（且支持逐元素数组 `step`）转换成整数边界，最终委托给 C 层 ufunc `_slice`。

## 2. 前置知识

在进入源码前，先回顾三个概念。

1. **结构化 dtype（structured dtype）**。NumPy 允许一个数组的每个元素是一个「结构体」，结构体的每个字段（field）有自己的名字和子 dtype。例如 `np.dtype("U5,U1,U8")` 描述的元素由三个字段组成，默认字段名是 `f0`、`f1`、`f2`，分别是宽度 5、1、8 的定长 unicode。可以用 `out["f0"]`、`out["f1"]` 这样的写法把某个字段当成一个普通数组视图来读写。`partition` 正是用它来「一个数组装三段」。

2. **Python `slice` 对象的两义性**。CPython 里构造 `slice` 时，`slice(5)` 会被解释成「从 0 取到 5」（即 `slice(None, 5, None)`），而不是「从 5 取到末尾」。换句话说**只传一个位置参数时，它被当作 `stop`**，而不是 `start`。这是 CPython `slice_new` 的行为，`numpy.strings.slice` 必须模仿它。

3. **哨兵默认值**。一个函数想区分「用户没传这个参数」和「用户显式传了 `None`」时，不能用 `None` 当默认值（因为 `None` 本身有含义）。这时用一个**全局唯一的、用户不可能故意传**的对象当默认值，用 `is` 判断它，叫「哨兵（sentinel）」。本讲里这个哨兵就是 `np._NoValue`。

另外请带着前几讲的两个套路来读：

- **`MAX` 哨兵**（[u2-l7](u2-l7-search-functions.md)）：`MAX = np.iinfo(np.int64).max`，把「未指定 `end`」归一化成一个超大整数，C 层再收敛到字符串实际长度。`partition` 复用了它。
- **路径 A：`str_len` 预算宽度 + `empty_like` 开缓冲区 + C 层 ufunc 写入**（[u2-l5](u2-l5-helpers-and-dtype-dispatch.md)、[u2-l9](u2-l9-justify-and-pad.md)）：当输出宽度不能仅由输入 dtype 决定时，Python 层负责量尺寸、开缓冲区，再把 `out=` 交给 C 层 ufunc。`partition` 就是这条路径，只是它的 `out` 是结构化数组。

## 3. 本讲源码地图

本讲只涉及一个核心源码文件，外加一个哨兵定义文件：

| 文件 | 作用 |
| --- | --- |
| `numpy/_core/strings.py` | `partition` / `rpartition` / `slice` 的 Python 包装实现，以及共享的 `_partition_dispatcher`。 |
| `numpy/_globals.py` | `np._NoValue` 单例哨兵的定义（解释它为什么是全局唯一对象）。 |

注意：`numpy/strings/__init__.py`（门面）只是 `from numpy._core.strings import *` 转发（见 [u1-l1](u1-l1-overview-and-facade.md)），真正的逻辑都在 `numpy/_core/strings.py` 里。本讲所有行号都指向后者。

被这些函数调用、但**实现下沉在 C/C++ 层**的私有 ufunc（`_partition`、`_partition_index`、`_rpartition`、`_rpartition_index`、`_slice`）在 `numpy/_core/strings.py` 顶部从 `numpy._core.umath` 导入，它们的具体循环是 [u3-l12](u3-l12-cpp-ufunc-registration.md) 之后的内容，本讲只把它们当作「委托目标」看待，不深入。

## 4. 核心概念与源码讲解

### 4.1 partition：用结构化 dtype 一次产出三段

#### 4.1.1 概念说明

`str.partition(sep)` 把字符串在**第一个** `sep` 处切成三段 `(前半, sep, 后半)`；找不到 `sep` 时返回 `(原串, "", "")`。`numpy.strings.partition` 就是它的向量化版本：对数组里每个元素各切一次，返回**三个数组组成的元组**。

难点在于输出形状：每个元素都要产出一个三段结果，而且每一段的长度随数据变化、三段还各不相同。一个普通定长字符串数组装不下这种「一个元素变三段」的结构。NumPy 的解法是——**先用一个结构化 dtype 把三段打包进同一个数组的三个字段，让 C 层 ufunc 一次性写满，再把 `f0/f1/f2` 三个字段视图拆成元组返回**。

#### 4.1.2 核心流程

`partition`（定长 `str_` / `bytes_` 分支）的执行步骤：

1. 把 `a`、`sep` 都 `np.asanyarray` 成数组。
2. 若 `result_type(a, sep).char == "T"`（变长 StringDType，见 [u1-l2](u1-l2-three-string-dtypes.md)），直接走快速路径 `return _partition(a, sep)`，委托给 C 层——变长类型不需要预算宽度。
3. 定长分支：
   1. 把 `sep` 统一转成 `a.dtype`；
   2. 用 `_find_ufunc(a, sep, 0, MAX)` 找**第一个** `sep` 的位置 `pos`（`MAX` 是 u2-l7 的哨兵，表示「搜到末尾」）；找不到时 `pos < 0`；
   3. 用 `str_len` 量出 `a_len`、`sep_len`；
   4. 逐元素算三段的宽度：
      - 前半 `buffersizes1 = where(not_found, a_len, pos)`（找不到时整段都进前半）；
      - 后半 `buffersizes3 = where(not_found, 0, a_len - pos - sep_len)`（找不到时后半为空）；
      - 中间段宽度取 `sep_len.max()`（找不到则取最小宽度 1）；
   5. 把三段宽度各自 `.max()`，拼成结构化 dtype 字符串 `out_dtype = "U5,U1,U8"`（或 `S...`）；
   6. `np.empty_like(a, shape=广播shape, dtype=out_dtype)` 一次性开出三字段缓冲区；
   7. 调用 `_partition_index(a, sep, pos, out=(out["f0"], out["f1"], out["f2"]))`，让 C 层 ufunc 把三段分别写进三个字段，再把这三个字段视图作为元组返回。

#### 4.1.3 源码精读

装饰器与共享 dispatcher——`_partition_dispatcher` 只把 `a` 当作「相关参数」返回，意味着 `__array_function__`（NEP-18）覆盖时只看 `a` 的类型：

[_partition_dispatcher 与 partition 定义 — `numpy/_core/strings.py:1532-1538`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1532-L1538)

```python
def _partition_dispatcher(a, sep):
    return (a,)


@set_module("numpy.strings")
@array_function_dispatch(_partition_dispatcher)
def partition(a, sep):
```

StringDType 快速分流（变长类型直接委托 C 层，跳过下面的宽度预算）：

[partition 的 T 分支 — `numpy/_core/strings.py:1583-1584`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1583-L1584)

```python
    if np.result_type(a, sep).char == "T":
        return _partition(a, sep)
```

定长分支的核心——找位置、量长度、预算三段宽度。注意 `MAX` 与 `_find_ufunc` 的用法和 u2-l7 完全一致：

[partition 量尺寸 — `numpy/_core/strings.py:1586-1599`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1586-L1599)

```python
    sep = sep.astype(a.dtype, copy=False)
    pos = _find_ufunc(a, sep, 0, MAX)
    a_len = str_len(a)
    sep_len = str_len(sep)

    not_found = pos < 0
    buffersizes1 = np.where(not_found, a_len, pos)
    buffersizes3 = np.where(not_found, 0, a_len - pos - sep_len)

    out_dtype = ",".join([f"{a.dtype.char}{n}" for n in (
        buffersizes1.max(),
        1 if np.all(not_found) else sep_len.max(),
        buffersizes3.max(),
    )])
```

`out_dtype` 那一行是全函数的灵魂：把「前半最大宽度、中间最大宽度、后半最大宽度」分别拼成 `f"{char}{n}"`，再用逗号 `","` 连起来，得到的正是一个合法的结构化 dtype 字符串（例如 `"U5,U1,U8"`）。其中中间段那段 `1 if np.all(not_found) else sep_len.max()` 是个空间优化：**当所有元素都找不到分隔符时**，中间段永远是空串，所以只需要能装下空串的最小宽度 `1`；否则中间段必须容得下最长的真实分隔符，取 `sep_len.max()`。

最后开缓冲区并委托 C 层写入——`out["f0"]`、`out["f1"]`、`out["f2"]` 是结构化数组的三个字段视图，C 层 ufunc `_partition_index` 把三段分别填进去：

[partition 开缓冲并委托 — `numpy/_core/strings.py:1600-1602`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1600-L1602)

```python
    shape = np.broadcast_shapes(a.shape, sep.shape)
    out = np.empty_like(a, shape=shape, dtype=out_dtype)
    return _partition_index(a, sep, pos, out=(out["f0"], out["f1"], out["f2"]))
```

#### 4.1.4 代码实践

**实践目标**：手工算出 `partition` 对一个具体输入构造的 `out_dtype`，验证结构化 dtype 三段输出。

**操作步骤**（在装好本仓库版本 numpy 的环境里运行，下列预期结果来自源码 docstring 与手算）：

```python
import numpy as np

a = np.array(["Numpy is nice!"])          # dtype '<U14'，长度 14
sep = np.array([" "])                      # dtype '<U1'，长度 1

# 手算（定长 str_ 分支）：
#   pos = 第一次出现空格的下标 = 5
#   a_len = 14, sep_len = 1
#   not_found = False
#   buffersizes1 = 5            -> "Numpy"   宽 5
#   中间宽     = sep_len.max()=1 -> " "      宽 1
#   buffersizes3 = 14-5-1 = 8   -> "is nice!" 宽 8
#   out_dtype = "U5,U1,U8"
print(np.strings.partition(a, sep))
# 预期输出（与 docstring 一致）：
# (array(['Numpy'], dtype='<U5'),
#  array([' '], dtype='<U1'),
#  array(['is nice!'], dtype='<U8'))

# 亲手构造同样的结构化 dtype，理解 f0/f1/f2：
dt = np.dtype("U5,U1,U8")
buf = np.empty(1, dtype=dt)
buf["f0"][0] = "Numpy"
buf["f1"][0] = " "
buf["f2"][0] = "is nice!"
print(buf["f0"], buf["f1"], buf["f2"])
```

**需要观察的现象**：返回值是三个独立数组的元组；三个数组的 dtype 宽度分别是 5、1、8，正好等于你手算的 `out_dtype`。

**预期结果**：`partition` 的返回三段宽度与手算一致；结构化 dtype 的 `f0/f1/f1/f2` 视图能分别读写三段。若你的 numpy 版本与文档不同导致宽度不同，以实际运行为准（本仓库 HEAD 为 `9559a6b1ac`）。

**找不到分隔符时**（验证 `(原串, "", "")` 与宽度优化）：

```python
a2 = np.array(["abc", "de"])               # '<U3'
print(np.strings.partition(a2, "Z"))
# 手算：两元素都找不到 Z -> not_found 全 True
#   buffersizes1 = a_len = [3,2] -> max 3  -> "abc"/"de"
#   中间宽 = 全找不到 -> 取 1      -> ""     （U1 容纳空串）
#   buffersizes3 = 0             -> ""     （U0）
# 预期：(array(['abc','de'], dtype='<U3'),
#       array(['',''], dtype='<U1'),
#       array(['',''], dtype='<U0'))   <- 待本地验证空串字段宽度
```

> 说明：找不到时前半为整串、后半为空，这与 `str.partition` 语义一致；中间字段宽度落为 `1` 是上面 `np.all(not_found)` 分支的效果。具体后半 `U0` 是否合法以本地运行为准。

#### 4.1.5 小练习与答案

**练习 1**：把 `a = np.array(["a-b-c"])`、`sep = "-"` 代入 `partition`，手算 `pos`、三段宽度与 `out_dtype`。

**答案**：`pos = 1`（第一个 `-`），`a_len = 5`，`sep_len = 1`；前半宽 `1`（`"a"`）、中间宽 `1`（`"-"`）、后半宽 `5-1-1 = 3`（`"b-c"`）；`out_dtype = "U1,U1,U3"`。

**练习 2**：为什么 `partition` 不能像比较类 ufunc 那样直接复用输入 dtype，而非要预算宽度、开结构化 `out`？

**答案**：比较类（`equal` 等）输出是标量（bool），宽度与输入无关；`partition` 输出三段定长字符串，宽度取决于「分隔符出现位置」这一**运行时数据**，无法从输入 dtype 推断，所以必须由 Python 层用 `str_len` 量出尺寸、构造定长结构化缓冲区，再交给 C 层填充。

---

### 4.2 rpartition：从右切分，复用同一个 dispatcher

#### 4.2.1 概念说明

`str.rpartition(sep)` 与 `partition` 几乎一样，区别有二：它在**最后一个**（最右边）`sep` 处切开；找不到时返回 `("", "", 原串)`——空段在前，整段在后。`numpy.strings.rpartition` 同样向量化，并和 `partition` 共用一套结构化 dtype 套路。

#### 4.2.2 核心流程

`rpartition` 的定长分支与 `partition` **结构同构**，只有三处不同：

1. 找位置改用 `_rfind_ufunc`（找**最后**一次出现，对应 u2-l7 的 `rfind`）；
2. 找不到时三段的归属反过来——`buffersizes1 = where(not_found, 0, pos)`、`buffersizes3 = where(not_found, a_len, ...)`，即「前半空、后半整串」；
3. 委托的 C 层 ufunc 是 `_rpartition_index` 而非 `_partition_index`。

而 StringDType 分流、`out_dtype` 拼接、`empty_like` 开缓冲区、`out["f0/f1/f2"]` 拆元组这些步骤与 `partition` 逐字相同。`rpartition` 复用的 dispatcher 正是 `_partition_dispatcher`（同名函数，因为 `(a, sep)` 签名一致，相关参数也只需 `a`）。

#### 4.2.3 源码精读

`rpartition` 复用同一个 `_partition_dispatcher`（注意装饰器写的是 `_partition_dispatcher`，不是另写一个）：

[rpartition 定义 — `numpy/_core/strings.py:1605-1607`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1605-L1607)

```python
@set_module("numpy.strings")
@array_function_dispatch(_partition_dispatcher)
def rpartition(a, sep):
```

`rpartition` 的核心差异——`_rfind_ufunc` 找右端位置，且 `not_found` 时前半取 0、后半取 `a_len`：

[rpartition 量尺寸 — `numpy/_core/strings.py:1655-1662`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1655-L1662)

```python
    pos = _rfind_ufunc(a, sep, 0, MAX)
    a_len = str_len(a)
    sep_len = str_len(sep)

    not_found = pos < 0
    buffersizes1 = np.where(not_found, 0, pos)
    buffersizes3 = np.where(not_found, a_len, a_len - pos - sep_len)
```

对比 4.1.3 里 `partition` 的同名两行，就能看出「找不到时谁拿整串」正好相反，这正是 `str.partition` 与 `str.rpartition` 的语义差异。

#### 4.2.4 代码实践

**实践目标**：对照 `partition` 与 `rpartition` 在多分隔符输入下的差异。

```python
import numpy as np
a = np.array(['aAaAaA', '  aA  ', 'abBABba'])   # docstring 示例
print(np.strings.partition(a, 'A'))   # 在「第一个」A 处切
print(np.strings.rpartition(a, 'A'))  # 在「最后一个」A 处切
# rpartition 预期（与 docstring 一致）：
# (array(['aAaAa','  a','abB'], dtype='<U5'),
#  array(['A','A','A'],        dtype='<U1'),
#  array(['','  ','Bba'],      dtype='<U3'))
```

**需要观察的现象**：对 `'aAaAaA'`，`partition` 在下标 1 切（前半 `'a'`），`rpartition` 在下标 5 切（前半 `'aAaAa'`）。

**预期结果**：两函数返回的前/后段长度不同，但中间段都是分隔符本身；找不到的元素（如某元素不含 `A`）在 `rpartition` 里表现为前半空、后半整串。结果待本地验证。

#### 4.2.5 小练习与答案

**练习**：`rpartition` 为什么能直接复用 `partition` 的 `_partition_dispatcher`，而不需要写 `_rpartition_dispatcher`？

**答案**：dispatcher 的职责只是声明「哪些参数参与 NEP-18 分发」，它返回 `(a,)`，与函数体内部用 `find` 还是 `rfind` 无关。两个函数签名都是 `(a, sep)`，相关参数都是 `a`，所以共用一个 dispatcher 完全正确。

---

### 4.3 slice：用 `np._NoValue` 哨兵模仿 Python slice 语义

#### 4.3.1 概念说明

`numpy.strings.slice(a, start, stop, step)` 对数组里每个字符串做切片，语义对齐 Python 的 `slice(start, stop, step)` 对象。它要解决两件 Python 字符串方法没有的麻烦事：

1. **「只给一个参数当作 `stop`」**：像 `slice(5)` 表示「取到第 5 个」一样，`np.strings.slice(a, 5)` 也应表示「每个字符串取前 5 个字符」，而不是「从第 5 个取到末尾」。
2. **逐元素不同的 `start/stop/step`**：可以为不同元素指定不同切片（甚至不同的步长方向），因此这些参数都允许是数组，Python 层不能用简单的 `if`，而要用 `np.where` 做向量化判断。

为了解决第 1 点，`slice` 的签名把 `stop` 的默认值设成哨兵 `np._NoValue`，而不是 `None`——因为 `stop=None` 在切片里有真实含义（取到末尾）。

#### 4.3.2 核心流程

`slice` 的执行步骤（注释里两次明确引用了 CPython 的对应实现）：

1. **哨兵判断**：`if stop is np._NoValue:` 则把 `stop = start; start = None`。这模仿 CPython `slice_new`——「只传一个位置参数时，它当 `stop`」。
2. **归一化 `step`**：`step is None` → `1`；转成数组；必须是整数类型，否则 `TypeError`；任一元素为 0 → `ValueError("slice step cannot be zero")`。
3. **归一化 `start`**：`start is None` 时，按 `step` 正负取边界——正步长从 `0` 开始，负步长从字符串末尾开始（用 `np.iinfo(np.intp).max` 表示「尽量靠右」，C 层会收敛到实际长度）。用 `np.where(step < 0, ...)` 是因为 `step` 可能是数组，不同元素方向可能不同。
4. **归一化 `stop`**：`stop is None` 时同样按 `step` 正负取边界——正步长取到 `np.iinfo(np.intp).max`（末尾），负步长取到 `np.iinfo(np.intp).min`（开头）。
5. `return _slice(a, start, stop, step)`：交给 C 层 ufunc 真正执行逐元素切片。

第 3、4 步对应 CPython 的 `PySlice_Unpack`：它把 `None` 起止值按步长方向转成 `PY_SSIZE_T_MAX/MIN`，再由下层收敛到实际长度。`numpy.strings.slice` 把这套整数边界规则原样搬过来，只是用 `np.where` 让它支持数组 `step`。

#### 4.3.3 源码精读

先看装饰器——`slice` **只有 `@set_module`，没有 `@array_function_dispatch`**。也就是说它不参与 NEP-18 `__array_function__` 分发（不像 `partition`/`rpartition`），只有模块归属被改写：

[slice 定义 — `numpy/_core/strings.py:1729-1730`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1729-L1730)

```python
@set_module("numpy.strings")
def slice(a, start=None, stop=np._NoValue, step=None, /):
```

注意签名末尾的 `/`：`start`/`stop`/`step` 都是**仅位置参数**，这和 CPython `slice` 的调用习惯一致（`slice(2)`、`slice(1, 5, 2)` 都是按位置传）。

哨兵判断——模仿 `slice_new` 的「一个参数当 `stop`」：

[哨兵处理 — `numpy/_core/strings.py:1792-1796`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1792-L1796)

```python
    # Just like in the construction of a regular slice object, if only start
    # is specified then start will become stop, see logic in slice_new.
    if stop is np._NoValue:
        stop = start
        start = None
```

`step` 归一化与校验——模仿 `PySlice_Unpack`，并禁止 0 步长：

[step 归一化 — `numpy/_core/strings.py:1798-1805`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1798-L1805)

```python
    # adjust start, stop, step to be integers, see logic in PySlice_Unpack
    if step is None:
        step = 1
    step = np.asanyarray(step)
    if not np.issubdtype(step.dtype, np.integer):
        raise TypeError(f"unsupported type {step.dtype} for operand 'step'")
    if np.any(step == 0):
        raise ValueError("slice step cannot be zero")
```

`start`/`stop` 的 `None` → 整数边界转换（关键：用 `np.where` 因为 `step` 可能为数组，逐元素方向不同）：

[start/stop 归一化与委托 — `numpy/_core/strings.py:1807-1813`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1807-L1813)

```python
    if start is None:
        start = np.where(step < 0, np.iinfo(np.intp).max, 0)

    if stop is None:
        stop = np.where(step < 0, np.iinfo(np.intp).min, np.iinfo(np.intp).max)

    return _slice(a, start, stop, step)
```

读这张表最直观：

| 条件 | 正步长 (`step > 0`) | 负步长 (`step < 0`) |
| --- | --- | --- |
| `start is None` | `0`（从头） | `intp.max`（从尾，C 层收敛到 `len-1`） |
| `stop is None` | `intp.max`（到尾） | `intp.min`（到头） |

#### 4.3.4 关于哨兵 `np._NoValue`

`np._NoValue` 不是普通默认值，它是 `numpy/_globals.py` 里定义的**全局唯一单例**。该模块特意禁止 reload，以保证「无论 numpy 被重载多少次，`np._NoValue` 的对象身份（`id`）不变」，从而 `is` 判断永远可靠：

[_NoValue 单例定义 — `numpy/_globals.py:32-63`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_globals.py#L32-L63)

```python
class _NoValueType:
    """Special keyword value. ..."""
    __instance = None

    def __new__(cls):
        # ensure that only one instance exists
        if not cls.__instance:
            cls.__instance = super().__new__(cls)
        return cls.__instance
    ...

_NoValue = _NoValueType()
```

正因为它「全局唯一、用户不会故意传」，所以适合当哨兵：`slice` 用 `is`（不是 `==`）来判断「用户到底有没有传 `stop`」，把「没传」和「显式传 `None`」区分开。

#### 4.3.5 代码实践

**实践目标**：验证 `slice` 的三件事——单参数当 `stop`、负步长反转、逐元素不同切片。

```python
import numpy as np

# 1) 单参数当 stop：slice(a, 2) 取前 2 个字符
a = np.array(['hello', 'world'])
print(np.strings.slice(a, 2))          # 预期 ['he', 'wo']，dtype '<U5'

# 2) 负步长反转字符串：start/stop 都 None，step=-1
#    按 4.3.3 的表：start=None -> intp.max (从尾), stop=None -> intp.min (到头)
#    等价于 [::-1]，得到逐字符反转
print(np.strings.slice(a, None, None, -1))   # 预期 ['olleh', 'dlrow']

# 3) 逐元素不同切片（start/stop/step 都可为数组）
b = np.array(['hello world', 'γεια σου κόσμε'], dtype=np.dtypes.StringDType())
print(np.strings.slice(b, np.array([1, 2]), np.array([4, 5])))
# 预期（与 docstring 同思路）：['ell', 'α σ']  <- 具体字符待本地验证
```

**需要观察的现象**：

- 第 1 步：`slice(a, 2)` 与 `slice(a, 0, 2)` 结果相同（证明单参数被当作 `stop`）。
- 第 2 步：`slice(a, None, None, -1)` 得到字符级反转，与 `[:: -1]` 一致。
- 第 3 步：不同元素用了不同的 `start/stop`，返回逐元素不同长度的切片。

**预期结果**：第 1、2 步结果如上；第 3 步在 StringDType（变长）上返回变长结果。多字节 unicode 的具体字符切分以本地运行为准。

#### 4.3.6 小练习与答案

**练习 1**：为什么 `slice` 的 `stop` 默认值是 `np._NoValue` 而不是 `None`？

**答案**：因为 `stop=None` 在切片里有明确含义——「取到末尾」。如果默认值用 `None`，函数就无法区分「用户没传 `stop`」（应触发「单参数当 `stop`」规则）和「用户显式传了 `stop=None`」（应取到末尾）。哨兵 `np._NoValue` 是用户不可能故意传的值，用 `is` 判断即可精确区分这两种情况。

**练习 2**：`start` 归一化那一行为什么必须用 `np.where(step < 0, ...)`，而不能写 `if step < 0: start = ...`？

**答案**：因为 `step` 可以是**数组**——不同元素可以有不同的步长，甚至一正一负。`np.where` 对每个元素分别判断步长符号，给出逐元素的 `start` 边界；Python 的 `if step < 0` 对数组会抛「真假值不明确」错误，且无法表达逐元素差异。

---

## 5. 综合实践

把本讲三个函数串起来，完成一个小任务：**解析形如 `"key=value"` 的配置字符串数组**。

要求：

1. 用 `partition` 把每个字符串拆成 `(key, "=", value)`；
2. 用 `slice` 对 `value` 段做处理（例如去掉首尾、或只取前 N 个字符）；
3. 解释每一步 `partition` 返回的三段 dtype 宽度是怎么算出来的。

参考脚本（结果待本地验证）：

```python
import numpy as np

cfg = np.array(["host=localhost", "port=8080", "debug=true"])
key, eq, value = np.strings.partition(cfg, "=")
print(key, eq, value)
# 手算（以 'host=localhost' 为例）：
#   pos = 第一次 '=' 的下标 = 4
#   a_len = 14, sep_len = 1
#   前半宽 = 4 ('host')，中间宽 = 1 ('=')，后半宽 = 14-4-1 = 9 ('localhost')
#   全数组的 out_dtype 取三段各自 .max() -> 前半 max(4,4,5)=5, 中间 1, 后半 max(9,4,4)=9
#   即 "U5,U1,U9"

# 再用 slice 只取 value 的前 3 个字符
print(np.strings.slice(value, 3))   # 单参数当 stop -> 取前 3 个
```

把 `partition` 返回的三段宽度、与 `slice` 单参数语义对上，就说明你已经掌握本讲的两个核心套路：**结构化 dtype 三段输出** 与 **`np._NoValue` 哨兵**。

## 6. 本讲小结

- `partition` / `rpartition` 把每个字符串切成三段，靠**结构化 dtype**（`f0`/`f1`/`f2` 三字段）一次开一个缓冲区，让 C 层 ufunc `_partition_index` / `_rpartition_index` 写满，再拆成三元组返回——这是 u2-l5「路径 A」的变体（输出宽度数据相关，需 Python 层预算）。
- `partition` 用 `_find_ufunc`（第一个 `sep`），找不到时整段进前半；`rpartition` 用 `_rfind_ufunc`（最后一个 `sep`），找不到时整段进后半——二者复用同一个 `_partition_dispatcher`。
- `out_dtype` 形如 `"U5,U1,U8"`，由 `",".join(f"{char}{n}")` 拼出，宽度来自 `str_len` 逐元素量取后再 `.max()`；中间段在「全找不到」时优化为宽度 `1`。
- `slice` 用 `np._NoValue` 哨兵实现「只给一个参数当作 `stop`」，模仿 CPython `slice_new`；`None` 起止值按 `step` 正负转成 `intp.max/min` 边界，模仿 `PySlice_Unpack`，并用 `np.where` 支持**逐元素数组 `step`**。
- `slice` 只有 `@set_module`、**没有** `array_function_dispatch`，因此不参与 NEP-18 分发；它最终委托 C 层 ufunc `_slice`。
- StringDType（`'T'`）输入在 `partition`/`rpartition` 里走快速分流（直接委托 `_partition`/`_rpartition`，不预算宽度），与定长分支形成对照。

## 7. 下一步学习建议

本讲是 [u2 进阶层](u2-l4-decorators-and-dispatch.md) 最后一篇，Python 包装层的各类函数（比较/查找/大小写/对齐/裁剪/切分）已全部讲完。下一步建议：

1. **进入 u3 专家层**，看这些私有 C ufunc 循环到底怎么注册：先读 [u3-l12（C++ ufunc 循环注册）](u3-l12-cpp-ufunc-registration.md)，重点找 `_partition_index`、`_slice` 这类循环在 `string_ufuncs.cpp` 里的 `add_loop` 注册位置。
2. 想理解 `_slice` 如何处理变长 / 负步长的逐字符读写，配合 [u3-l13（string_buffer 与 fastsearch）](u3-l13-buffer-and-fastsearch.md) 的 `getchar` 原语。
3. 若对 StringDType 的变长快速分流好奇，直接看 [u3-l14（StringDType 专用 ufunc 循环）](u3-l14-stringdtype-ufuncs.md)。
