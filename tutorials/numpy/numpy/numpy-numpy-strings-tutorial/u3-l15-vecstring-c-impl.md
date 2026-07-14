# _vec_string 的 C 实现：逐元素调用 Python 方法

## 1. 本讲目标

本讲是专家层（第 3 单元）的一篇。在 u2-l8 里我们已经知道，`numpy.strings` 里有那么一批函数（`upper`/`lower`/`mod`/`decode`/`encode`/`translate` 等）并没有专用的 C ufunc 循环，而是把活儿委托给一个叫 `_vec_string` 的「桥」。本讲就钻进这座桥的内部，看清它由 C 写成的三段式实现。

读完本讲，你应该能够：

1. 在 `multiarraymodule.c` 中定位 `_vec_string` 的注册项，并解释它如何根据**数组 dtype** 选出对应的 Python 字符串方法（`bytes` 方法还是 `str` 方法）。
2. 说清楚 `_vec_string` 如何根据**有没有额外参数**，在 `_vec_string_no_args`（快路径）和 `_vec_string_with_args`（慢路径）之间二选一。
3. 对比这两条路径的迭代器实现差异（单迭代器 vs 广播多迭代器），并据此估算 `_vec_string` 相对原生 ufunc 的性能代价，理解为什么 `upper`/`mod`/`decode` 至今仍依赖它。

## 2. 前置知识

本讲需要你已具备以下认知（来自前置讲义）：

- **三种字符串 dtype**（u1-l2）：`bytes_`（`'S'`）、`str_`（`'U'`）、变长 `StringDType`（`'T'`）。
- **输出 dtype 路径分类**（u2-l5）：C 路径（输出复用输入 dtype）、A 路径（预算宽度）、B 路径（object 中间态再还原）。
- **`_vec_string` 的 Python 侧定位**（u2-l8）：它是连接「NumPy 数组」与「Python 内建字符串方法」的通用桥；输出宽度不变的函数（如 `upper`）走 C 路径直接复用输入 dtype，输出宽度数据相关的函数（如 `mod`/`encode`）先用 `np.object_` 承接、再用 `_to_bytes_or_str_array` 还原。
- **C++ ufunc 循环注册**（u3-l12）：一个真正的 ufunc 在 C 层有紧凑的 typed loop、零 Python 回调；本讲会反复拿这个标准去对比 `_vec_string`。

补充一点 CPython 基础（不熟悉的读者可略读）：

- **`PyObject_GetAttr(type, name)`**：从类型对象上按名字取出一个属性，这里用来拿到绑定方法（如 `str.upper`）。
- **`PyArray_IterNew` / `PyArray_MultiIterFromObjects`**：NumPy 的迭代器 API。前者遍历单个数组的每一个元素；后者把多个数组**广播**到同一形状后同步遍历。
- **`PyObject_CallFunctionObjArgs(method, item, NULL)` / `PyObject_CallObject(method, tuple)`**：在 C 里调用一个 Python 可调用对象，前者用变长 C 参数，后者用一个 `tuple`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [numpy/_core/src/multiarray/multiarraymodule.c](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/multiarraymodule.c) | `_vec_string` 的全部 C 实现：总入口、`no_args` 快路径、`with_args` 慢路径，以及模块方法注册表。 |
| [numpy/_core/strings.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py) | Python 包装层。`upper`/`mod`/`encode`/`translate` 等函数在这里把请求转发给 `_vec_string`。 |
| [numpy/_core/multiarray.pyi](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/multiarray.pyi) | 类型存根，确认 `_vec_string` 是 `numpy._core.multiarray` 暴露的私有 C 函数。 |

> 说明：本仓库给出的永久链接 base 指向 `numpy/strings/` 子目录，但 `_vec_string` 的真实实现位于 `numpy/_core/` 下。为使链接可点击且正确，下面一律使用从仓库根算起的完整路径。

## 4. 核心概念与源码讲解

### 4.1 `_vec_string` 总入口：dtype 分发与快慢路径选择

#### 4.1.1 概念说明

`_vec_string` 是一个被注册成 Python 方法的 C 函数，它对外暴露在 `numpy._core.multiarray` 上，签名（按位置）为：

```
_vec_string(char_array, type, method_name, args_seq=None)
```

它要做三件事：

1. **解析参数**：把输入转成 NumPy 数组、取出目标输出 `dtype`、取出方法名字符串、取出（可选的）参数序列。
2. **按 dtype 选方法**：根据数组的 dtype，决定对每个元素应该调用 `bytes` 的方法还是 `str` 的方法。
3. **按有无参数选路径**：没有额外参数就走快路径 `_vec_string_no_args`，有参数就走慢路径 `_vec_string_with_args`。

这是一个典型的「分发器（dispatcher）」结构：总入口只负责「判断 + 路由」，真正干活的是下面两个 helper。

#### 4.1.2 核心流程

```text
_vec_string(char_array, type, method_name, args_seq)
  │
  ├─ PyArg_ParseTuple 解析四个位置参数
  │
  ├─ 按 dtype 解析 method：
  │     NPY_STRING (bytes_, 'S')            → PyBytes_Type 上取方法
  │     NPY_UNICODE (str_, 'U') 或
  │       StringDType ('T')                 → PyUnicode_Type 上取方法
  │     用户自定义字符串 dtype              → 其 scalar_type 上取方法
  │     其它                                 → 报错
  │
  └─ 按参数选路径：
        args_seq 为空 (None 或长度 0)  → _vec_string_no_args   (快)
        args_seq 非空序列              → _vec_string_with_args (慢)
        args_seq 非序列                → TypeError
```

一个关键细节：**`StringDType`（`'T'`）用的是 `str` 的方法**（`PyUnicode_Type`），而不是某套「StringDType 专用方法」。因为 StringDType 的标量类型就是 Python `str`，逐元素取出的就是普通的 `str` 对象，所以直接调 `str.upper`、`str.translate` 即可。

#### 4.1.3 源码精读

总入口函数定义在这里：

[_vec_string 总入口](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/multiarraymodule.c#L4132-L4200) — 注意三个参数里第一个 `dummy` 与第三个 `kwds` 都标记为 `NPY_UNUSED`，说明它虽然以 `METH_VARARGS | METH_KEYWORDS` 注册，但其实**不接受关键字参数**，只吃位置参数。

参数解析：

[PyArg_ParseTuple 解析四参数](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/multiarraymodule.c#L4143-L4148) — 格式串 `"O&O&O|O"` 表示：第一个用 `PyArray_Converter` 转数组、第二个用 `PyArray_DescrConverter` 转 dtype、第三个取方法名、第四个（`|` 之后）可选地取参数序列。

按 dtype 选方法：

[按 dtype 选 bytes/str 方法](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/multiarraymodule.c#L4150-L4167) — 这段就是上面流程图里「按 dtype 解析 method」的原文。注意 `NPY_DTYPE(PyArray_DTYPE(char_array)) == &PyArray_StringDType` 这一支专门把 StringDType 也导向 `str` 方法。第三分支调用的 `_is_user_defined_string_array` 是个保护性检查：

[_is_user_defined_string_array](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/multiarraymodule.c#L3854-L3876) — 只允许「标量类型是 `str`/`bytes` 子类型」的自定义 dtype 通过，否则报错。

快慢路径选择：

[按有无参数选择 no_args / with_args](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/multiarraymodule.c#L4173-L4185) — 判据有三：`args_seq == NULL`、或 `PySequence_Size(args_seq) == 0`，都走快路径 `_vec_string_no_args`；非空序列走 `_vec_string_with_args`；非序列报 `TypeError`。

模块方法注册：

[注册表项](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/multiarraymodule.c#L4762-L4764) — `_vec_string` 就是在这张模块方法表里登记的，标志 `METH_VARARGS | METH_KEYWORDS`。Python 侧 `from numpy._core.multiarray import _vec_string` 拿到的正是它。

类型存根也确认了它的可见性：

[multiarray.pyi 中的 _vec_string](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/multiarray.pyi#L255-L255) — 类型被刻意写成 `Callable[..., object]`，即「私有、签名不对外保证」。

#### 4.1.4 代码实践

**实践目标**：直接从 `numpy._core.multiarray` 导入私有的 `_vec_string`，分别构造无参和带参两种调用，观察它如何把请求分发到 Python 字符串方法。

**操作步骤**（示例代码）：

```python
import numpy as np
from numpy._core.multiarray import _vec_string  # 私有 API，仅用于学习

a = np.array(['a', 'B', 'c'])

# 1) 无参：对每个元素调用 str.upper —— 命中 _vec_string_no_args（快路径）
print(_vec_string(a, a.dtype, 'upper'))
# 预期：['A' 'B' 'C']（dtype 与输入相同，这里是 '<U1'）

# 2) 带参：对每个元素调用 str.center，宽度 3 —— 命中 _vec_string_with_args（慢路径）
print(_vec_string(a, np.dtype('<U3'), 'center', (3,)))
# 预期：[' a ' ' B ' ' c ']

# 3) bytes_ 数组会走 bytes 方法：b'ab'.upper() -> b'AB'
b = np.array([b'ab', b'cd'])
print(_vec_string(b, b.dtype, 'upper'))
# 预期：[b'AB' b'CD']
```

**需要观察的现象**：

- 第 1、3 步输出 dtype 与输入一致，对应 u2-l5 的「C 路径」（输出复用输入 dtype）。
- 第 2 步给出了显式输出 dtype `'<U3'`，因为 `center` 会改变宽度，必须由调用方告诉 `_vec_string` 结果要装进多宽的字段。

**预期结果**：如上注释所示。

**如果无法确定运行结果**：以上输出均为「待本地验证」，请在本地 `python` 中实际运行确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `StringDType`（`'T'`）的数组在 `_vec_string` 里调用的是 `str` 方法，而不是某套「StringDType 方法」？

**参考答案**：因为 StringDType 的标量类型就是 Python 内建的 `str`，`_vec_string` 逐元素把数据取出来得到的就是普通 `str` 对象，直接调 `str.upper`/`str.translate` 等即可，无需为 StringDType 单独实现一套方法。源码中 `NPY_DTYPE(...) == &PyArray_StringDType` 这一分支正是把 StringDType 也导向 `PyUnicode_Type`。

**练习 2**：`_vec_string` 注册时带了 `METH_KEYWORDS`，但它真的接受关键字参数吗？

**参考答案**：不接受。函数签名里 `kwds` 被标记为 `NPY_UNUSED`，且参数解析用的是 `PyArg_ParseTuple`（只解析位置参数），所以传关键字参数不会被识别。`METH_KEYWORDS` 在这里只是 CPython 调用约定的形式要求（3 参数签名的 C 函数），并非功能承诺。

---

### 4.2 `_vec_string_no_args`：无参快路径

#### 4.2.1 概念说明

当被调用的字符串方法**没有额外参数**（如 `str.upper`、`str.lower`、`str.swapcase`），`_vec_string` 就走这条快路径。函数自身的注释点明了它的存在理由：

> This is a faster version of _vec_string_args to use when there are no additional arguments to the string method. This doesn't require a broadcast iterator (and broadcast iterators don't work with 1 argument anyway).

换句话说，没有参数就不需要「广播」，于是可以用更轻的单迭代器，省掉构造广播迭代器、为每个元素分配 `tuple` 的开销。

#### 4.2.2 核心流程

```text
_vec_string_no_args(char_array, type, method)
  │
  ├─ in_iter  = PyArray_IterNew(char_array)   # 单数组扁平迭代器
  ├─ result   = SimpleNewFromDescr(同形状, type)  # 输出形状=输入形状
  ├─ out_iter = PyArray_IterNew(result)
  │
  └─ while 未遍历完：
        item       = ToScalar(in_iter 当前元素)   # 装箱成 str/bytes
        item_result= CallFunctionObjArgs(method, item, NULL)  # method(item)
        SETITEM(out_iter, item_result)            # 写回输出
        NEXT(in_iter); NEXT(out_iter)
```

注意三点：

1. **输出形状直接等于输入形状**（`PyArray_NDIM`/`PyArray_DIMS`），因为无参方法不会改变元素个数或形状，只可能改变每个字符串的内容/宽度。
2. 每个元素只做**一次** Python 回调：`method(item)`。
3. 仍然逐元素回到 Python 解释器——这是 `_vec_string` 系列相对 ufunc 的根本代价，快路径只是「相对慢路径更快」，并非「接近 ufunc」。

#### 4.2.3 源码精读

快路径完整定义：

[_vec_string_no_args](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/multiarraymodule.c#L4063-L4130) — 函数头注释解释了它为什么存在。

迭代器与输出分配：

[单迭代器 + 同形状输出](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/multiarraymodule.c#L4077-L4092) — 注意它用的是 `PyArray_IterNew`（单），而 `with_args` 用的是 `PyArray_MultiIterFromObjects`（多），这正是两条路径的名字「快/慢」的来源。

逐元素回调主循环：

[no_args 主循环](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/multiarraymodule.c#L4094-L4117) — `PyArray_ToScalar` 把当前指针处的数据装箱成 Python 标量对象，`PyObject_CallFunctionObjArgs(method, item, NULL)` 就是 `method(item)`，`PyArray_SETITEM` 把结果写回输出数组。`SETITEM` 失败会报 `result array type does not match underlying function`，这正是为什么 `upper` 在 Python 侧要把输出 dtype 设成与输入一致。

Python 侧的调用非常薄。以 `upper` 为例：

[strings.py 中 upper 的实现](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1118-L1119) — 只有 `_vec_string(a_arr, a_arr.dtype, 'upper')` 一行：不传 `args_seq`，所以命中本节的快路径，输出 dtype 直接复用输入 dtype（C 路径）。`lower`/`swapcase`/`capitalize`/`title` 的结构与之完全一致。

#### 4.2.4 代码实践

**实践目标**：验证 `upper` 等无参函数确实「输出 dtype = 输入 dtype」，从而确认它走的是本节的快路径且不经 object 中间态。

**操作步骤**（示例代码）：

```python
import numpy as np

for dt in ['<U5', 'S5', 'T']:
    a = np.array(['aB', 'Cd'], dtype=dt)
    out = np.strings.upper(a)
    print(dt, '->', out.dtype, '->', out.tolist())
```

**需要观察的现象**：三种 dtype 的输出 dtype 都与输入**完全相同**（`<U5`→`<U5`，`S5`→`S5`，StringDType→StringDType），元素被原地「大写化」。这说明宽度未变、没有 object 中间态，符合「C 路径 + no_args 快路径」的预期。

**预期结果**：输出 dtype 与输入一致，内容为 `['AB', 'CD']`。

**如果无法确定运行结果**：具体字符串渲染「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`_vec_string_no_args` 为什么不需要广播迭代器？

**参考答案**：因为无参方法只有一个输入数组，没有「其它参数数组」需要对齐/广播。直接用 `PyArray_IterNew` 单数组迭代器逐元素遍历即可；而广播多迭代器在只有 1 个输入时无法工作（注释明说），所以也无从使用。

**练习 2**：若一个元素的 `method(item)` 返回了与输出 `type` 不兼容的对象，会发生什么？

**参考答案**：`PyArray_SETITEM` 会失败，函数设置 `TypeError: result array type does not match underlying function` 并跳到 `err` 分支，释放已分配的迭代器与结果数组后返回 `NULL`（即向 Python 抛异常）。这就是为什么 Python 包装层必须给 `_vec_string` 一个宽度正确的输出 dtype。

---

### 4.3 `_vec_string_with_args`：带参广播迭代器路径

#### 4.3.1 概念说明

当被调用的字符串方法**带额外参数**（如 `str.center(width)`、`str.translate(table)`、`str.encode(encoding)`），`_vec_string` 就走这条慢路径。它的关键能力是**广播**：额外的参数本身也可以是数组，会被广播到与 `char_array` 相同的形状，然后**逐位置**配对调用。

例如 `np.strings.mod(np.array(['%d','x%d']), np.array([8, 64]))` 中，`values` 数组与 `char_array` 逐元素配对：`'%d' % 8`、`'x%d' % 64`。这套配对正是由广播多迭代器完成的。

#### 4.3.2 核心流程

```text
_vec_string_with_args(char_array, type, method, args)
  │
  ├─ nargs = len(args) + 1                      # +1 是 char_array 自己
  ├─ broadcast_args[0]   = char_array
  │  broadcast_args[1..] = args 的每一项
  │
  ├─ in_iter = PyArray_MultiIterFromObjects(broadcast_args, nargs)  # 广播到同一形状
  ├─ result  = SimpleNewFromDescr(广播形状, type)
  ├─ out_iter= PyArray_IterNew(result)
  │
  └─ while 未遍历完：
        args_tuple = PyTuple_New(n)             # 每个元素都要新建一个 tuple！
        for i in 0..n:
            args_tuple[i] = ToScalar(第 i 个迭代器当前元素)
        item_result = CallObject(method, args_tuple)   # method(*args_tuple)
        SETITEM(out_iter, item_result)
        NEXT(多迭代器); NEXT(out_iter)
```

与快路径相比，慢路径多了两样开销：

1. **广播多迭代器**：要把 `char_array` 与所有参数数组对齐到同一形状。
2. **每元素一个 `tuple`**：`PyTuple_New(n)` + 逐项 `ToScalar` 填充，调用完即弃。

同时注意 `nargs = PySequence_Size(args) + 1` 里的「+1」：广播参数列表的第 0 项是 `char_array` 本身，后续才是用户传入的参数；总数受 `NPY_MAXARGS` 限制。

#### 4.3.3 源码精读

慢路径完整定义：

[_vec_string_with_args](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/multiarraymodule.c#L3967-L4061)。

参数计数与广播列表组装：

[nargs 计数与 broadcast_args 填充](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/multiarraymodule.c#L3977-L3994) — 注意 `nargs = PySequence_Size(args) + 1`，且对 `NPY_MAXARGS` 做了上界检查，超过则报 `len(args) must be < NPY_MAXARGS - 1`。

广播迭代器与输出分配：

[MultiIter + 广播形状输出](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/multiarraymodule.c#L3995-L4012) — 输出形状取自广播结果的 `in_iter->dimensions`，所以当参数数组形状不同时会按 NumPy 广播规则得到统一形状。

逐元素「建 tuple + 回调」主循环：

[with_args 主循环](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/multiarraymodule.c#L4014-L4048) — 这里能看到「每元素一个 `PyTuple_New(n)`」的开销点：先建空 tuple，再从每个子迭代器取标量塞进去（`PyTuple_SetItem` 会「偷」引用），最后 `PyObject_CallObject(method, args_tuple)` 等价于 `method(*args_tuple)`。

Python 侧三个典型调用：

[strings.py 中 mod（带参 + object 中间态）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L251-L252) — `_vec_string(a, np.object_, '__mod__', (values,))`：输出 dtype 用 `np.object_` 承接（因为 `%` 的结果宽度数据相关），再用 `_to_bytes_or_str_array` 还原。注意方法名是 dunder `__mod__`，因为 `a % values` 走的是 `__mod__`。

[strings.py 中 encode（带参 + object 中间态）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L625-L627) — `_clean_args(encoding, errors)` 会把 `None` 及其后的参数砍掉，因此当不传 `encoding`/`errors` 时 `args` 为空，反而会落到 `no_args` 快路径；一旦传了编码就落到本节慢路径。

[strings.py 中 translate（str_/bytes_ 双分支）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1717-L1727) — `str_` 只传 `table` 一个参数，`bytes_` 则传 `[table] + deletechars`，这是因为 `str.translate(table)` 与 `bytes.translate(table, /, delete=b'')` 的方法签名不同，而非 dtype 还原的需要。

#### 4.3.4 代码实践

**实践目标**：用一个**参数本身是数组**的例子，验证 `_vec_string_with_args` 的广播配对行为。

**操作步骤**（示例代码）：

```python
import numpy as np

# 每个元素调用 str.center，宽度是一个「广播数组」
a = np.array(['a', 'bb', 'ccc'])
widths = np.array([3, 5, 7])      # 参数数组，与 a 同形状
out = np.char.center(a, widths)   # center 在 numpy.strings 是 ufunc；这里借用 _vec_string 思路：
from numpy._core.multiarray import _vec_string
print(_vec_string(a, np.dtype('U7'), 'center', (widths,)).tolist())
# 预期：[' a ' '  bb ' '  ccc ']（居中到各自宽度）
```

**需要观察的现象**：`widths` 与 `a` **逐位置配对**：`'a'` 居中到 3、`'bb'` 居中到 5、`'ccc'` 居中到 7。这正是广播多迭代器实现的「参数数组 vs 数据数组」逐元素对齐。

**预期结果**：每个字符串按对应宽度居中填充空格。

**如果无法确定运行结果**：具体渲染「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`np.strings.encode(a)`（不传编码）会落到哪条路径？为什么？

**参考答案**：会落到 `no_args` 快路径。因为 Python 侧用 `_clean_args(encoding, errors)` 把 `None` 参数都砍掉后 `args` 为空序列，而总入口的判据是「`args_seq` 为 `None` 或长度为 0」即走快路径。这也解释了为什么「同一个函数」会因是否传参而走不同路径。

**练习 2**：`_vec_string_with_args` 在主循环里为每个元素 `PyTuple_New(n)`，相比 `no_args` 多了什么？这套开销在大数组下意味着什么？

**参考答案**：多了两件事——(1) 用 `PyArray_MultiIterFromObjects` 把多个数组广播对齐；(2) 每个元素分配并填充一个长度为 `n` 的 `tuple`，调用后立即释放。大数组下这意味着 O(N) 次 tuple 分配 + O(N) 次 Python 回调，是它比 `no_args` 更慢、且远比原生 ufunc 慢的直接原因。

---

## 5. 综合实践

本实践贯穿三个最小模块，目标是让你亲手把「注册 → 分发 → 快慢路径 → 性能代价」这条链走通。

### 步骤 1：在 C 源码中定位注册项与分发逻辑（源码阅读型）

1. 打开 `numpy/_core/src/multiarray/multiarraymodule.c`，搜索 `{"_vec_string"`，找到它在模块方法表里的注册行（约 4762 行），确认其调用约定是 `METH_VARARGS | METH_KEYWORDS`。
2. 跳到 `_vec_string` 函数体（约 4132 行），画出三段逻辑：参数解析（4143）、按 dtype 选方法（4150）、按参数选路径（4173）。
3. 回答：一个 `StringDType` 数组调用 `'upper'`，`method` 取自哪个类型对象？一个 `bytes_` 数组呢？

> 参考答案：StringDType 取自 `PyUnicode_Type`（即 `str`）；`bytes_`（`NPY_STRING`）取自 `PyBytes_Type`（即 `bytes`）。

### 步骤 2：用计时实验估算性能影响

`_vec_string` 的根本代价是「逐元素回到 Python」。我们拿它和原生 ufunc 对比，直观感受差距。

```python
import numpy as np, timeit

n = 200_000
a = np.array(['aBcDe'] * n, dtype='<U5')

# A) _vec_string 快路径：upper
t_vec = timeit.timeit(lambda: np.strings.upper(a), number=5)

# B) 真正的 C ufunc：find（在 u2-l7 学过，零 Python 回调）
t_uf  = timeit.timeit(lambda: np.strings.find(a, 'c'), number=5)

# C) 纯 Python 列表推导作为参照
lst = a.tolist()
t_py = timeit.timeit(lambda: [s.upper() for s in lst], number=5)

print(f"vec_string upper : {t_vec:.3f}s")
print(f"ufunc find       : {t_uf:.3f}s")
print(f"python list comp : {t_py:.3f}s")
```

**需要观察的现象与预期**：

- `vec_string upper` 与 `python list comp` 在**同一量级**——两者都是「N 次 Python 方法调用」，`_vec_string` 只是省去了你手写循环、并负责好形状/dtype/装箱。
- `ufunc find` 应当**显著快于**前两者（通常快一到两个数量级），因为它在 C 层紧凑循环、零 Python 回调。

**结论**：`_vec_string` 系列函数的性能天花板被「逐元素 Python 回调」钉死，无法像 ufunc 那样向量化。这也是 `__all__` 注释把 `upper/lower/...` 标注为「Will gradually become ufuncs」（迟早会被改写成 ufunc），而 `mod/decode/encode/translate` 标注为「Will probably not become ufuncs」（输出宽度强数据相关，难以写成定宽 ufunc）的根本原因。可对照 [strings.py 的 __all__ 注释](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L82-L86) 阅读。

**如果无法确定运行结果**：绝对耗时「待本地验证」，但「vec ≈ python ≫ ufunc」的相对关系是稳定的。

### 步骤 3（进阶）：解释 encode 的双面性

阅读 [strings.py 中 encode 的实现](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L625-L627)，回答：为什么 `np.strings.encode(a)` 与 `np.strings.encode(a, 'utf-8')` 在 `_vec_string` 内部走的可能是不同路径？

> 参考答案：不传编码时 `_clean_args(encoding, errors)` 返回空列表 → 总入口判其为空 → 走 `no_args` 快路径；传了 `'utf-8'` 后列表非空 → 走 `with_args` 慢路径。同一公开函数，因参数不同而切换底层路径，这是 `_vec_string` 分发逻辑的一个有意思的副作用。

## 6. 本讲小结

- `_vec_string` 是 `numpy._core.multiarray` 暴露的一个 C 函数，在模块方法表里以 `METH_VARARGS | METH_KEYWORDS` 注册，实质只吃位置参数。它是连接「NumPy 数组」与「Python 字符串方法」的通用桥。
- 总入口做两件分发：按**数组 dtype** 决定取 `bytes` 方法还是 `str` 方法（`StringDType('T')` 也用 `str` 方法）；按**有无额外参数** 在 `no_args`（快）与 `with_args`（慢）两条路径间二选一。
- `_vec_string_no_args` 用单数组迭代器 `PyArray_IterNew`，输出形状等于输入形状，每元素一次 `method(item)` 回调，适合 `upper/lower/swapcase/capitalize/title` 这类宽度不变的函数。
- `_vec_string_with_args` 用广播多迭代器 `PyArray_MultiIterFromObjects`，支持参数数组逐位置配对，但每元素都要新建一个 `tuple`，开销更高；`mod/encode/translate` 等带参函数走它。
- 两条路径都「逐元素回到 Python」，性能被 Python 回调钉死，远不及原生 ufunc；这正是 `__all__` 注释区分「会变 ufunc」与「大概不会变 ufunc」的依据。
- Python 包装层的三种典型用法：宽度不变直接传输入 dtype（`upper`）、宽度数据相关用 `np.object_` 承接再还原（`mod`/`encode`）、方法签名差异导致参数个数不同（`translate` 的 str_/bytes_ 双分支）。

## 7. 下一步学习建议

- **u3-l16 测试体系与新增字符串 ufunc 的端到端实践**：把本讲的 `_vec_string` 函数（如 `split`）与真正的 ufunc 放在一起，梳理「把一个 `_vec_string` 函数迁移成 ufunc」需要改动 Python 包装、C 循环注册、`__all__`、测试的全链路。
- **回看 u2-l8**：结合本讲的 C 实现，重新理解「输出宽度能否仅由输入 dtype 决定」这条分界准则如何决定一个函数是走 `_vec_string` 还是 ufunc。
- **延伸阅读**：通读 `multiarraymodule.c` 中 `_vec_string` 周边（3854–4200 行），留意错误处理与引用计数（`Py_DECREF`/`Py_XDECREF`）的成对使用，这是 C 扩展健壮性的基本功。
