# 大小写转换与 _vec_string 委托：upper/mod/decode/encode/translate

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `upper`/`lower`/`swapcase`/`capitalize`/`title` 为什么不走 C 层 ufunc，而是统一委托给 `_vec_string`。
- 在 C 源码层面讲清 `_vec_string` 是如何「逐元素调用 Python 字符串方法」的，以及它如何为 `bytes`/`str`/`StringDType` 三类元素挑选方法对象。
- 理解 `mod`/`decode`/`encode` 为什么要先产出 `object` 中间数组，再用 `_to_bytes_or_str_array` 还原成定长 dtype。
- 解释 `translate` 对 `str_` 与 `bytes_` 走两个分支的真正原因（**方法签名不同**，而非 dtype 还原）。
- 通过 `__all__` 里的注释，判断一个字符串函数「将来会变成 ufunc」还是「几乎不会」。

## 2. 前置知识

本讲承接 [u2-l5](./u2-l5-helpers-and-dtype-dispatch.md) 建立的三个辅助函数认知，并复用前面几讲的概念：

- **`_to_bytes_or_str_array`**：当结果必须先存进 `object` 中间数组时，用它测量真实宽度、把结果还原成定长 dtype（路径 B：模板给类型、数据给宽度）。
- **`_clean_args`**：把 Python 字符串方法里「不接受 `None`」的可选参数及其后续参数整体砍掉。
- **ufunc 与 `resolve_descriptors`**：ufunc 的输出 dtype 只能由输入 dtype 推断；这正是某些函数「难以变成 ufunc」的根因。
- **三种字符串 dtype**：变长 `StringDType`（`'T'`）、定长 `bytes_`（`'S'`，1 字符 = 1 字节）、定长 `str_`（`'U'`，1 字符固定 4 字节）。
- **Python 的两套字符串方法**：`str` 与 `bytes` 各有一套同名但签名可能不同的方法（如 `translate`）。

> 通俗一句话：本讲讲的是「那些暂时没有专用 C 循环、于是借 Python 字符串方法兜底」的一类函数，以及连接 NumPy 数组与 Python 方法的桥 `_vec_string`。

## 3. 本讲源码地图

| 文件 | 在本讲中的作用 |
| --- | --- |
| `numpy/_core/strings.py` | Python 包装层。本讲精读 `upper/lower/swapcase/capitalize/title`、`mod`、`decode`、`encode`、`translate` 共 9 个函数，以及它们共享的 dispatcher 与 `__all__` 注释。 |
| `numpy/_core/src/multiarray/multiarraymodule.c` | C 实现。`_vec_string` 是入口，内部按「有无参数」分派到 `_vec_string_no_args`（快路径）与 `_vec_string_with_args`（带广播的慢路径）。 |

永久链接约定：本讲引用的 commit 为 `9559a6b1ac93610711d8f1243f8c949fca4420bb`。

## 4. 核心概念与源码讲解

### 4.1 _vec_string：逐元素调用 Python 字符串方法的通用桥

#### 4.1.1 概念说明

前面几讲我们看到，`find`/`count`/`center`/`strip` 等函数背后都有专门的 C ufunc 循环（`string_ufuncs.cpp` / `stringdtype_ufuncs.cpp`）。但还有一类函数——大小写转换、格式化、编解码、字符映射——暂时没有 C 循环。它们都需要「对每个字符串元素调用一个 Python 字符串方法」，`_vec_string` 就是完成这件事的通用桥。

它的核心职责可以浓缩成一句：

> 给我一个数组、一个方法名（如 `'upper'`）、一个输出 dtype，我对每个元素取出 Python 标量、调用该方法、把结果写进输出数组。

为什么需要它？因为这类操作要么实现简单（大小写转换），要么输出宽度难以预测（编解码），NumPy 暂时没给它们写专用 ufunc 循环。先用 `_vec_string` 跑通功能，等将来有了 C 循环再逐步替换——这正是 `__all__` 注释里「gradually become ufuncs」的含义。

#### 4.1.2 核心流程

`_vec_string` 在 C 侧的执行流程（伪代码）：

```
_vec_string(char_array, out_dtype, method_name, args?):
    # 1. 按【元素类型】挑出 Python 方法对象（从类型上取，而非元素实例上取）
    if   元素是 bytes(S):                     method = bytes.method_name   # PyBytes_Type
    elif 元素是 unicode(U) 或 StringDType(T): method = str.method_name     # PyUnicode_Type
    elif 是用户自定义字符串 dtype:             method = scalar_type.method_name

    # 2. 按【有无额外参数】选择快慢两条路径
    if   无参数:     -> _vec_string_no_args      # 单数组迭代，method(item)
    else:           -> _vec_string_with_args     # 广播 + 构造参数元组，method(*args)
```

两个关键设计：

1. **方法对象从 `str` / `bytes` 类型上取**（`PyObject_GetAttr`），而不是从每个元素实例上取。这样循环里只剩 `call`，省掉了逐元素属性查找，是性能关键。
2. **快慢两条路径**。无参数时不需要广播，走更轻的 `_vec_string_no_args`；有参数时需要把 `char_array` 与参数数组广播对齐，走 `_vec_string_with_args`。

#### 4.1.3 源码精读

Python 侧的导入（`_vec_string` 来自 multiarray）：

[numpy/_core/strings.py:20](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L20) — 从 `numpy._core.multiarray` 导入 `_vec_string`，这是它唯一的来源。

C 侧主入口，解析参数并按元素类型挑选方法对象：

[numpy/_core/src/multiarray/multiarraymodule.c:4132-4171](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/multiarraymodule.c#L4132-L4171) — 用 `PyArg_ParseTuple` 解析 `(char_array, out_dtype, method_name, args?)`；随后 `NPY_STRING` 取 `PyBytes_Type` 上的方法，`NPY_UNICODE` 与 `StringDType` 取 `PyUnicode_Type` 上的方法。这段就是「`S` 走 bytes 方法、`U`/`T` 走 str 方法」的判定。

C 侧按「有无参数」分派快慢路径：

[numpy/_core/src/multiarray/multiarraymodule.c:4173-4185](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/multiarraymodule.c#L4173-L4185) — `args_seq` 为 `NULL` 或空序列时走 `_vec_string_no_args`，否则走 `_vec_string_with_args`，否则报 `'args' must be a sequence`。

无参快路径：

[numpy/_core/src/multiarray/multiarraymodule.c:4063-4130](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/multiarraymodule.c#L4063-L4130) — 单数组迭代器遍历，每个元素 `PyArray_ToScalar` 取出标量，`PyObject_CallFunctionObjArgs(method, item, NULL)` 调用方法，`PyArray_SETITEM` 写入输出。注释说明：广播迭代器「单参数时本就不能用」，所以无参场景另写一个更快的循环。

带参慢路径（含广播）：

[numpy/_core/src/multiarray/multiarraymodule.c:3967-4061](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/multiarraymodule.c#L3967-L4061) — 先用 `PyArray_MultiIterFromObjects` 把 `char_array` 与所有参数广播成一个多维迭代器；循环内为每个位置构造一个参数元组（`PyTuple_New(n)`），逐个 `PyArray_ToScalar` 填入，再 `PyObject_CallObject(method, args_tuple)` 调用。

模块方法表注册：

[numpy/_core/src/multiarray/multiarraymodule.c:4762-4764](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/multiarraymodule.c#L4762-L4764) — `_vec_string` 以 `METH_VARARGS | METH_KEYWORDS` 注册到 multiarray 模块，故 Python 侧 `from numpy._core.multiarray import _vec_string` 能直接拿到。

#### 4.1.4 代码实践

**目标**：用纯 Python 手动复现 `_vec_string_no_args` 的行为，确认它「逐元素调用 Python 字符串方法」。

**操作步骤**：

```python
import numpy as np

a = np.array(['a', 'B'])          # dtype='<U1'

# 手动模拟 _vec_string_no_args：对每个元素取 str 标量，调用 str.upper
method = str.upper                 # 等价于 C 里从 PyUnicode_Type 取方法
manual = np.array([method(s) for s in a], dtype=a.dtype)

# 对照官方实现
official = np.strings.upper(a)

print("手动: ", manual, manual.dtype)
print("官方: ", official, official.dtype)
print("完全一致:", np.array_equal(manual, official))
```

**需要观察的现象**：

- 手动结果 `['A' 'B']`，官方结果相同。
- 两者 dtype 都是 `<U1`——因为 `upper` 输出宽度不大于输入，直接复用了 `a_arr.dtype`。

**预期结果**：两个数组逐元素相等、dtype 相同。「完全一致」打印 `True`。

#### 4.1.5 小练习与答案

**练习 1**：为什么方法对象从 `str`/`bytes` **类型**上取（`PyObject_GetAttr(PyUnicode_Type, ...)`），而不是对每个元素做属性查找（`element.upper`）？

**参考答案**：逐元素属性查找会为每个元素产生一次 `getattr` 开销；而从类型上取一次绑定方法，循环里只剩 `PyObject_CallFunctionObjArgs`，是这条循环的性能关键。

**练习 2**：`_vec_string_with_args` 为什么要用 `PyArray_MultiIter` 做广播？

**参考答案**：因为额外参数（例如 `mod` 的 `values`）可能是与 `char_array` shape 不同的数组，需要先广播对齐，再逐元素配对成参数元组传给方法。无参时没有这层对齐需求，所以另走更轻的 `_vec_string_no_args`。

---

### 4.2 大小写转换族：upper/lower/swapcase/capitalize/title

#### 4.2.1 概念说明

`upper`/`lower`/`swapcase`/`capitalize`/`title` 这五个函数有一个共同点：**转换后字符串长度不超过原长度**（至少在定长 dtype 的语义里如此）。因此它们的输出 dtype 可以直接复用输入 dtype——`_vec_string(a_arr, a_arr.dtype, 'upper')` 里第三个参数传的就是 `a_arr.dtype`，不需要 `object` 中间态，也不需要 `_to_bytes_or_str_array`。

这一族函数体几乎一字不差，只差一个方法名字符串。理解其中一个，就理解了全部五个。

#### 4.2.2 核心流程

```
upper(a):
    a_arr = np.asarray(a)
    return _vec_string(a_arr, a_arr.dtype, 'upper')
        #              ^^^^^^^^^^ 输出 dtype 直接复用输入
```

五个函数的方法名分别是 `'upper'`/`'lower'`/`'swapcase'`/`'capitalize'`/`'title'`，其余完全一致。它们都用同一个 dispatcher `_unary_op_dispatcher`。

#### 4.2.3 源码精读

共享的一元 dispatcher：

[numpy/_core/strings.py:1080-1081](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1080-L1081) — `_unary_op_dispatcher(a)` 只把 `a` 声明为 NEP-18 的相关参数；五个函数共用它。

`upper` 的完整实现（其余四个结构相同）：

[numpy/_core/strings.py:1084-1119](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1084-L1119) — 函数体仅两行：`a_arr = np.asarray(a)` 后 `return _vec_string(a_arr, a_arr.dtype, 'upper')`。

其余四个函数（结构完全一致，只换方法名）：

- `lower`：[numpy/_core/strings.py:1122-1157](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1122-L1157)
- `swapcase`：[numpy/_core/strings.py:1160-1198](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1160-L1198)
- `capitalize`：[numpy/_core/strings.py:1201-1239](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1201-L1239)
- `title`：[numpy/_core/strings.py:1242-1282](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1242-L1282)

注意它们全部把 `a_arr.dtype` 直接当输出 dtype 传给 `_vec_string`。对 `StringDType`（`'T'`）数组，C 侧 `PyArray_SimpleNewFromDescr` 会据此创建变长输出数组；对定长 `S`/`U`，则创建同宽度的定长输出。

> 深入一点：定长 dtype 严格按字段宽度存储。若某字符大写后变长（Python 中 `'ß'.upper()` 返回 `'SS'`，长度为 2），写进定宽字段时会被截断。这是定长 dtype 字符串操作的固有局限，不是 bug。本讲实践会请你验证这一现象。

#### 4.2.4 代码实践

**目标**：验证五个函数的输出 dtype 等于输入 dtype，并观察定长 dtype 下「大写变长被截断」的边界。

**操作步骤**：

```python
import numpy as np

a = np.array(['a1b c', '1bca', 'bca1'])     # dtype='<U5'
for name in ('upper', 'lower', 'swapcase', 'capitalize', 'title'):
    fn = getattr(np.strings, name)
    out = fn(a)
    print(f"{name:11s} -> {out.tolist()}  dtype={out.dtype}")

# 边界：'ß'.upper() == 'SS'（长度 1 -> 2），定宽 U1 会截断
b = np.array(['ß'])                          # dtype='<U1'
print("upper(ß) 官方:", np.strings.upper(b))   # 观察是否被截断成 'S'
```

**需要观察的现象**：

- 前五个函数的输出 dtype 都是 `<U5`，与输入一致。
- `upper('ß')` 在定宽 `<U1` 字段里，由于变长被截断。

**预期结果**：前五行 dtype 均为 `<U5`。`upper(b)` 的结果（是否为 `'S'`）**待本地验证**——它取决于 `PyArray_SETITEM` 写入定宽字段时的截断行为。

#### 4.2.5 小练习与答案

**练习 1**：把 `upper` 改成 `_vec_string(a_arr, np.object_, 'upper')` 会发生什么？

**参考答案**：输出会变成 `object` 数组（每个元素是一个 Python `str`），而不是定长 `str_`/`bytes_`。正因为大小写转换长度不变，官方才直接传 `a_arr.dtype`，省掉了 object 中间态与 `_to_bytes_or_str_array` 的还原步骤。

**练习 2**：这五个函数为何共用 `_unary_op_dispatcher`？

**参考答案**：它们都是只接收 `a` 一个数组参数的一元操作，NEP-18 的「相关参数」集合相同（都是 `(a,)`），所以共用同一个 dispatcher 即可。

---

### 4.3 mod/decode/encode：输出宽度数据相关，走 object 中间态

#### 4.3.1 概念说明

与大小写族相反，`mod`/`decode`/`encode` 的输出宽度**无法仅由输入 dtype 预测**：

- `decode`：`bytes` 解码成 `str`，结果长度取决于编码（如 `cp037`）和实际字节内容。
- `encode`：`str` 编码成 `bytes`，结果长度取决于编码与字符内容。
- `mod`：`"%s"` 之类的格式化，结果长度取决于被插入的值。

因为 `_vec_string` 的输出 dtype 必须在创建输出数组前就确定，而这些函数的宽度要等「真正算完」才知道，所以它们采用两步走：先用 `np.object_` 作为输出 dtype，让每个结果以 Python 对象原样落进 object 数组；再用 `_to_bytes_or_str_array` 测量真实宽度，还原成定长 dtype（即 u2-l5 的路径 B：**模板给类型、数据给宽度**）。

#### 4.3.2 核心流程

```
mod(a, values):
    return _to_bytes_or_str_array(
        _vec_string(a, np.object_, '__mod__', (values,)),   # 注意方法名是 __mod__（dunder）
        a)                                                    # a 的 dtype 作为「类型模板」
```

两个要点：

1. `mod` 的方法名是 `'__mod__'`，**不是 `'mod'`**——`str` 没有 `.mod()` 方法，`a % values` 走的是 `str.__mod__`。
2. `decode` 的类型模板是 `np.str_('')`（结果必为 `str`），`encode` 的是 `np.bytes_(b'')`（结果必为 `bytes`）；而 `mod` 直接用 `a`（格式串的 dtype），因为 `%` 格式化的结果类型跟随格式串。

`decode`/`encode` 还用 `_clean_args(encoding, errors)` 清理可选参数：因为 `bytes.decode`/`str.encode` 的 `encoding`/`errors` 不接受 `None`，必须把它们及后续参数一起砍掉。

#### 4.3.3 源码精读

`mod`（注意 `'__mod__'` 与 `(values,)` 元组）：

[numpy/_core/strings.py:217-252](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L217-L252) — `return _to_bytes_or_str_array(_vec_string(a, np.object_, '__mod__', (values,)), a)`。`values` 作为单元素元组传入，C 侧走 `_vec_string_with_args` 把它与 `a` 广播。

`decode`（类型模板 `np.str_('')`）：

[numpy/_core/strings.py:535-581](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L535-L581) — `_vec_string(a, np.object_, 'decode', _clean_args(encoding, errors))` 的结果，用 `np.str_('')` 还原成定长 `str_`。

`encode`（类型模板 `np.bytes_(b'')`）：

[numpy/_core/strings.py:584-627](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L584-L627) — 与 `decode` 对称，最终用 `np.bytes_(b'')` 还原成定长 `bytes_`。

两个辅助函数（u2-l5 已详述，这里只标位置）：

- `_to_bytes_or_str_array`：[numpy/_core/strings.py:110-124](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L110-L124) — object 中间态还原成定长 dtype 的桥梁。
- `_clean_args`：[numpy/_core/strings.py:127-141](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L127-L141) — 砍掉 `None` 及其后续参数。

#### 4.3.4 代码实践

**目标**：观察 `decode` 的输出宽度由「实际结果」决定，而非由输入 dtype 决定。

**操作步骤**：

```python
import numpy as np

# 输入是 7 字节的 bytes_，但 cp037 解码后是 7 个字符的 str_ -> <U7
c = np.array([b'\x81\xc1\x81\xc1\x81\xc1', b'@@\x81\xc1@@', b'\x81\x82\xc2\xc1\xc2\x82\x81'])
print("input dtype:", c.dtype)                       # |S7
out = np.strings.decode(c, encoding='cp037')
print("output     :", out.tolist(), out.dtype)        # ['aAaAaA','  aA  ','abBABba']  <U7

# mod：输出宽度跟随 values 的实际内容，而非格式串的 dtype
fmt = np.array(["NumPy is a %s library"])             # <U20
print(np.strings.mod(fmt, values=["Python"]))         # <U25 —— 宽度由结果测量而来
```

**需要观察的现象**：

- `decode` 把 `|S7` 变成了 `<U7`——类型从 bytes 变 str，宽度由解码结果测量。
- `mod` 输出 `<U25`，比格式串 `<U20` 更宽，因为 `%s` 被替换成实际文本后变长了。

**预期结果**：与注释一致。若你换一个 `encoding`，输出宽度会随之改变——这正说明宽度是数据相关、无法预先由 dtype 推断。

#### 4.3.5 小练习与答案

**练习 1**：`mod` 为什么用 `'__mod__'` 而不是 `'mod'`？

**参考答案**：`str`/`bytes` 没有 `.mod()` 方法；`a % values` 这一运算在 Python 里由特殊方法 `__mod__` 实现，所以 `_vec_string` 必须用 dunder 名字 `'__mod__'` 才能从 `PyUnicode_Type`/`PyBytes_Type` 上取到对应方法。

**练习 2**：为什么这三个函数不像大小写族那样直接传 `a_arr.dtype`？

**参考答案**：它们的输出宽度是数据相关的（取决于编码、被插入的值等），在创建输出数组前无法确定；必须先用 `object` 中间态承接任意长度的结果，再用 `_to_bytes_or_str_array` 测量真实宽度并还原。

---

### 4.4 translate：str_ 与 bytes_ 方法签名不同导致的双分支

#### 4.4.1 概念说明

`translate` 对 `str_` 和 `bytes_` 走两个分支，但**不是因为 dtype 还原**——两条分支都把 `a_arr.dtype` 直接当输出 dtype。真正的原因是 `str.translate` 与 `bytes.translate` 的**方法签名不同**：

- `str.translate(table)`：只接收一个 `table`（字典或映射表），**不接受** `deletechars`。
- `bytes.translate(table, /, delete=b'')`：接收 `table` 和一个可选的 `delete`（即 `deletechars`）。

因此：

- 对 `str_`：只传 `(table,)` 一个元素的元组。
- 对 `bytes_`：传 `[table] + _clean_args(deletechars)`，把 `deletechars`（清理掉 `None` 后）作为第二个参数。

如果对 `str_` 也传 `deletechars`，会触发 `TypeError`（`str.translate` 不接受第二个参数）；如果对 `bytes_` 漏掉 `deletechars`，就丢了删除功能。所以必须按 dtype 分支。

#### 4.4.2 核心流程

```
translate(a, table, deletechars=None):
    a_arr = np.asarray(a)
    if str_:
        return _vec_string(a_arr, a_arr.dtype, 'translate', (table,))            # 1 个参数
    else:  # bytes_
        return _vec_string(a_arr, a_arr.dtype, 'translate',
                           [table] + _clean_args(deletechars))                    # 1 或 2 个参数
```

注意：两条分支的输出 dtype 都是 `a_arr.dtype`——即 `translate` **不**走 object 中间态，输出宽度跟随输入字段宽度（变长会截断）。

#### 4.4.3 源码精读

`translate` 全文：

[numpy/_core/strings.py:1679-1727](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1679-L1727) — 用 `issubclass(a_arr.dtype.type, np.str_)` 区分；`str_` 分支只传 `(table,)`，`bytes_` 分支传 `[table] + _clean_args(deletechars)`。

dispatcher：

[numpy/_core/strings.py:1675-1676](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1675-L1676) — `_translate_dispatcher(a, table, deletechars=None)` 只声明 `a` 为相关参数。

#### 4.4.4 代码实践（本讲主任务）

**目标 1**：用 `_vec_string` 的思路手动复现 `np.strings.swapcase`；**目标 2**：解释 `translate` 对 `str_` 与 `bytes_` 走不同分支的原因。

**操作步骤**：

```python
import numpy as np

# ---- 目标 1：手动复现 swapcase ----
a = np.array(['a', 'B'])                  # dtype='<U1'
# _vec_string(a_arr, a_arr.dtype, 'swapcase') 内部等价于：
manual = np.array([str.swapcase(s) for s in a], dtype=a.dtype)
print("手动 swapcase:", manual)            # ['A' 'b']
print("官方 swapcase:", np.strings.swapcase(a))
print("一致:", np.array_equal(manual, np.strings.swapcase(a)))

# ---- 目标 2：translate 的双分支 ----
# str_ 只接受一个 table；deletechars 对 str_ 无意义（会被忽略/报错）
s = np.array(['a1b c', '1bca', 'bca1'])                       # <U5
table_s = str.maketrans({'a': 'X'})                            # dict 形式 table
print("str_  translate:", np.strings.translate(s, table_s))    # 只传 table

# bytes_ 接受 table + deletechars（第二个参数）
b = np.array([b'a1b c', b'1bca', b'bca1'])                     # |S5
table_b = bytes.maketrans(b'a', b'X')
print("bytes translate(带删除):", np.strings.translate(b, table_b, deletechars=b' '))
# 删除空格：'a1b c' -> 'X1bc'（删除空格 + a->X）
```

**需要观察的现象**：

- 手动 `swapcase` 与官方结果完全一致（`['A' 'b']`）。
- `str_` 分支只传 `table`；`bytes_` 分支额外传了 `deletechars=b' '`，结果里空格被删除——证明 `bytes.translate` 用上了第二参数，而 `str.translate` 不会。

**为什么走不同分支（文字解释）**：因为 `str.translate(table)` 的签名只允许一个参数，`bytes.translate(table, delete=b'')` 的签名允许第二个删除集合参数。NumPy 必须按 dtype 选择传参个数，否则对 `str_` 传两个参数会报 `TypeError`。

**预期结果**：上述打印与注释一致。`str_` 那一行若强行多传 `deletechars`（直接调 `s[0].translate(table_s, b' ')`）会抛 `TypeError`——可自行验证。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `translate` 的 `str_` 分支也写成 `[table] + _clean_args(deletechars)`，会出什么问题？

**参考答案**：当用户对 `str_` 数组传了 `deletechars` 时，`_clean_args` 不会删掉它，于是 `_vec_string` 会用两个参数调用 `str.translate(table, deletechars)`，触发 `TypeError`（`str.translate` 不接受第二个位置参数）。这正是 `str_` 分支必须只传 `(table,)` 的原因。

**练习 2**：`translate` 为什么不需要 `_to_bytes_or_str_array`？

**参考答案**：两条分支都把 `a_arr.dtype` 直接当输出 dtype，让结果写入与输入同宽度的字段（变长则截断）。它不追求「按真实结果宽度还原」，所以不需要 object 中间态。

---

### 4.5 __all__ 注释与「能否变成 ufunc」的演进判断

#### 4.5.1 概念说明

`strings.py` 的 `__all__` 把本讲涉及的 9 个函数分成两组，并用注释标明演进方向：

- **`# _vec_string - Will gradually become ufuncs as well`**：`upper`/`lower`/`swapcase`/`capitalize`/`title`。
- **`# _vec_string - Will probably not become ufuncs`**：`mod`/`decode`/`encode`/`translate`。

判断的依据是本讲反复出现的那个原则：**输出宽度能否仅由输入 dtype 决定**。

- 大小写族：输出宽度 ≤ 输入宽度，输出 dtype 可直接复用输入 → 完全可以写一个 C ufunc 循环（`resolve_descriptors` 直接返回输入 dtype），所以「将来会」。
- `mod`/`decode`/`encode`：输出宽度数据相关，`resolve_descriptors` 无法仅凭输入 dtype 给出输出宽度 → 难以套用定长 ufunc 模型，所以「几乎不会」（`translate` 虽然目前复用输入 dtype，但其语义复杂、表/删除参数形态不一，也被归在此组）。

这条注释是阅读 `strings.py` 的「路线图」：它直接告诉你哪些函数仍是临时实现、哪些基本定型。

#### 4.5.2 核心流程

读 `__all__` 的注释分组，可以画一张归类表：

| 函数 | 当前实现 | 输出宽度 | 分组 | 演进方向 |
| --- | --- | --- | --- | --- |
| upper/lower/swapcase/capitalize/title | `_vec_string` 复用输入 dtype | 不变 | `gradually` | 将来变 ufunc |
| mod/decode/encode | `_vec_string` + object 中间态 + `_to_bytes_or_str_array` | 数据相关 | `probably not` | 几乎不会变 ufunc |
| translate | `_vec_string` 复用输入 dtype（双分支） | 跟随输入（变长截断） | `probably not` | 几乎不会变 ufunc |

#### 4.5.3 源码精读

`__all__` 及其分组注释：

[numpy/_core/strings.py:73-90](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L73-L90) — 注意第 82 行 `# _vec_string - Will gradually become ufuncs as well` 与第 85 行 `# _vec_string - Will probably not become ufuncs` 这两条注释，它们是本节判断的权威依据。

#### 4.5.4 代码实践

**目标**：用 `__all__` 注释自动归类，验证「输出宽度是否变化」与分组是否一致。

**操作步骤**：

```python
import numpy as np

a_u = np.array(['a1b c', '1bca', 'bca1'])           # <U5
gradually = ['upper', 'lower', 'swapcase', 'capitalize', 'title']
for name in gradually:
    out = getattr(np.strings, name)(a_u)
    print(f"{name:11s} 输入<U5 -> 输出{out.dtype}  宽度不变:{out.dtype.itemsize == a_u.dtype.itemsize}")

# mod/decode/encode 宽度会变
print("mod    宽度:", np.strings.mod(np.array(['%s']), ['Python']).dtype)  # 跟随结果
print("decode 宽度:", np.strings.decode(np.array([b'abc']), encoding='utf-8').dtype)
```

**需要观察的现象**：大小写族输出 dtype 宽度与输入一致；`mod`/`decode` 的宽度由结果决定。

**预期结果**：大小写族全部「宽度不变: True」，与 `gradually` 分组吻合。

#### 4.5.5 小练习与答案

**练习**：假如未来有人要把 `upper` 改造成真正的 ufunc，他的 `resolve_descriptors` 应该返回什么 dtype？

**参考答案**：直接返回输入 dtype（因为 `upper` 不改变字符串长度，输出字段宽度等于输入）。这也是它被归入「gradually become ufuncs」的原因——迁移到 ufunc 不存在「输出宽度无法预测」的障碍。

---

## 5. 综合实践

把本讲的几个关键点串成一个端到端的小任务。

**任务**：给定一个 `str_` 数组和一个 `bytes_` 数组，对同一个「概念操作」分别走 `_vec_string` 的两条不同路径，并解释差异。

```python
import numpy as np

s = np.array(['hello', 'WORLD'])        # str_  (<U5)
b = np.array([b'hello', b'WORLD'])      # bytes_(|S5)

# (1) 大小写：走 _vec_string_no_args，输出 dtype == 输入 dtype
print("str   upper:", np.strings.upper(s), np.strings.upper(s).dtype)
print("bytes upper:", np.strings.upper(b), np.strings.upper(b).dtype)

# (2) 编码/解码：走 _vec_string_with_args + object 中间态 + _to_bytes_or_str_array
enc = np.strings.encode(s, encoding='utf-8')          # str_  -> bytes_（宽度数据相关）
dec = np.strings.decode(b, encoding='utf-8')          # bytes_-> str_ （宽度数据相关）
print("encode:", enc, enc.dtype)
print("decode:", dec, dec.dtype)

# (3) translate：str_ 只传 table；bytes_ 传 table + deletechars
t_s = str.maketrans({'o': '0'})
t_b = bytes.maketrans(b'o', b'0')
print("str   translate:", np.strings.translate(s, t_s))
print("bytes translate:", np.strings.translate(b, t_b, deletechars=b'l'))
```

请逐项回答：

1. 第 (1) 组两个调用的 C 方法对象分别取自 `PyUnicode_Type` 还是 `PyBytes_Type`？为什么输出 dtype 与输入相同？
2. 第 (2) 组为什么必须经过 object 中间态？`encode` 的输出宽度为什么不是 5？
3. 第 (3) 组为什么 `str_` 和 `bytes_` 传参个数不同？

**参考要点**：

1. `str_` 取自 `PyUnicode_Type`、`bytes_` 取自 `PyBytes_Type`；大小写不改变长度，`_vec_string` 直接用 `a_arr.dtype` 创建同宽度输出。
2. 编解码输出宽度取决于编码与内容，无法预先由 dtype 推断；先入 object 数组再用 `_to_bytes_or_str_array` 测量宽度还原。`utf-8` 下这些 ASCII 字符 1 字节 = 1 字符，故宽度为 5（换成多字节字符就会不同）。
3. `str.translate` 只接受 1 个参数，`bytes.translate` 接受 `table` 与可选 `delete` 两个参数，故按 dtype 分支传参。

## 6. 本讲小结

- `_vec_string` 是连接 NumPy 数组与 Python 字符串方法的通用桥：对每个元素取标量、调用方法、写回输出数组。
- C 侧 `_vec_string` 按**元素类型**挑方法对象（`S`→`bytes`、`U`/`T`→`str`），按**有无参数**分派 `_vec_string_no_args`（快）与 `_vec_string_with_args`（带广播）。
- 大小写族（`upper`/`lower`/`swapcase`/`capitalize`/`title`）输出宽度不变，直接复用 `a_arr.dtype`，不经 object 中间态。
- `mod`/`decode`/`encode` 输出宽度数据相关，先用 `object` 中间态承接，再用 `_to_bytes_or_str_array` 测量真实宽度还原；`mod` 用的是 dunder 名 `'__mod__'`。
- `translate` 对 `str_`/`bytes_` 双分支是因**方法签名不同**（`str.translate` 一个参数、`bytes.translate` 两个参数），而非 dtype 还原。
- `__all__` 的两条注释（`gradually` / `probably not`）以「输出宽度能否由输入 dtype 决定」为分界，是判断函数演进方向的路线图。

## 7. 下一步学习建议

- 下一讲 [u2-l9 对齐与填充类](./u2-l9-justify-and-pad.md) 将进入「有专用 C ufunc 循环」的函数（`center`/`ljust`/`rjust`/`zfill`/`expandtabs`），你会看到「预计算最大宽度 + 构造定长 `out` + 调用 C 层 ufunc」这套与 `_vec_string` 截然不同的实现套路。
- 若想看 `_vec_string` 的 C 细节，重点再读 [multiarraymodule.c:3967-4199](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/multiarraymodule.c#L3967-L4199) 的三个函数，对照 `PyArray_ToScalar` / `PyArray_SETITEM` 的成对使用。
- 进阶（u3 单元）会下探到 `string_ufuncs.cpp` 与 `string_buffer.h`，届时可回头比较「大小写族将来如何从 `_vec_string` 迁移成真正的 C ufunc 循环」。
