# 通用辅助函数与 dtype 分发套路

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `_get_num_chars` 解决的「字符数 vs 字节数」混淆问题，以及为什么 `str_` 数组要 `itemsize // 4`；
- 解释 `_to_bytes_or_str_array` 为什么存在——它是 `_vec_string` 产生的 `object` 中间态与「正确的 `str_`/`bytes_`/`StringDType` 数组」之间的还原桥梁；
- 说清楚 `_clean_args` 的作用——为「逐元素委托给 Python 字符串方法」清理掉不能传 `None` 的可选参数；
- 看到任何一个 `numpy.strings` 函数时，能立刻判断它的输出 dtype 是怎么定的（本讲归纳出四条路径）。

本讲是第 2 单元的「工具箱」篇。u2-l4 讲了装饰器（身份与分发），本讲讲三个被各处函数反复调用的**私有辅助函数**，以及它们共同支撑的「按 dtype 分发」套路。掌握本讲后，后面 l6–l11 每一篇具体函数（比较、查找、对齐、裁剪、切分）你都能一眼归类。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 三种字符串 dtype 与「宽度」的不同含义

`numpy.strings` 处理三种字符串 dtype（详见 u1-l2）：

| dtype | `dtype.char` | 存储 | 1 个字符占多少字节 | `itemsize` 与字符数的关系 |
| --- | --- | --- | --- | --- |
| `bytes_` | `'S'` | 定长，每字符 1 字节 | 1 | `itemsize == 字符数` |
| `str_` | `'U'` | 定长 UCS4，每字符 4 字节 | 4 | `itemsize == 字符数 × 4` |
| `StringDType` | `'T'` | 变长（内部 UTF-8 动态存储） | 不定 | `itemsize` 是记录头大小，**不代表字符数** |

关键陷阱：对 `str_` 数组，`itemsize` 是字节数，不是字符数。比如 `np.array(['abc'])` 的 dtype 是 `<U3`，`itemsize` 是 `12`（3 字符 × 4 字节），但「这个字段能装几个字符」的答案是 `3`。本讲的三个辅助函数里有两个都在处理这个 `//4`。

### 2.2 两种「字段宽度」：容量 vs 运行时长度

- **容量（capacity）**：dtype 字段本身能装多少字符，由 dtype 决定。`<U3` 的容量是 3。
- **运行时长度（length）**：数组里某个字符串**实际**有几个字符。`np.array(['ab', 'c'])` 是 `<U2`（容量 2），但 `str_len` 运行时给出 `[2, 1]`。

`_get_num_chars` 读的是**容量**（从 `itemsize` 反推），`str_len` 算的是**运行时长度**。两者不可混淆——u1-l2 已经强调过这一点，本讲会看到它如何影响输出 dtype 的构造。

### 2.3 为什么需要 `object` 中间态

`numpy.strings` 里有些操作（`mod`/`decode`/`encode`/`join` 等）本质上是「逐元素调用 Python 的 `str`/`bytes` 方法」。这些方法的输出长度**无法事先预测**（比如 `decode` 的输出长度取决于编码结果），而且不同元素的长度还不一样。

NumPy 的定长数组要求所有元素等宽。于是只能先把这些「长度参差不齐的 Python 字符串对象」暂存进一个 `dtype=object` 的数组（每个槽位是一个 Python `str`/`bytes` 对象），等全部算完，再统一找出最大宽度、构造一个定长 dtype 一次性转回去。这个「转回去」的动作就是 `_to_bytes_or_str_array`。

## 3. 本讲源码地图

本讲只涉及一个源码文件，但会反复跳读它的不同区域：

| 区域 | 位置 | 作用 |
| --- | --- | --- |
| 三个辅助函数 | [strings.py:99-141](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L99-L141) | `_get_num_chars` / `_to_bytes_or_str_array` / `_clean_args` 的定义 |
| 模块级常量与 partial | [strings.py:93-96](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L93-L96) | `MAX` 哨兵、`array_function_dispatch` 预绑 `module` |
| `multiply` | [strings.py:196-210](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L196-L210) | 「预算宽度 + 构造定长 out」的代表（路径 A） |
| `mod` / `decode` / `encode` | [strings.py:251-252](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L251-L252)、[strings.py:579-581](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L579-L581)、[strings.py:625-627](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L625-L627) | 「object 中间态 + 还原」的代表（路径 B） |
| `upper` | [strings.py:1118-1119](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1118-L1119) | 「输出复用输入 dtype」的代表（路径 C） |
| `_split` / `_rsplit` / `_splitlines` | [strings.py:1440-1441](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1440-L1441)、[strings.py:1486-1487](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1486-L1487)、[strings.py:1528-1529](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1528-L1529) | 「object 中间态但不还原」的代表（路径 D） |
| `translate` | [strings.py:1717-1727](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1717-L1727) | `_clean_args` 的典型用法，并按 dtype 分两个分支 |

> 说明：本讲把文件简称为 `strings.py`，完整路径是 `numpy/_core/strings.py`（`numpy.strings` 是它的门面，见 u1-l1）。

## 4. 核心概念与源码讲解

### 4.1 `_get_num_chars`：抽象「每字段字符数」

#### 4.1.1 概念说明

很多地方需要知道「一个 dtype 字段能装几个字符」——比如要把一串 Python 字符串塞进一个定长数组，得先算出最大字符数，再据此构造 `<U{N}` 或 `|S{N}`。

问题在于：`str_` 的 `itemsize` 是**字节数**（每字符 4 字节），直接用 `itemsize` 当字符数会错 4 倍。`_get_num_chars` 就是把「`str_` 要 `//4`、其余直接用 `itemsize`」这个小规则封装起来，让调用方不必每次都记得这个坑。

注意它只对**定长** dtype（`'S'`/`'U'`）有意义。对变长 `StringDType('T')`，`itemsize` 是固定的记录头大小、和字符数无关——所以这个函数**不该**用于 `'T'`。

#### 4.1.2 核心流程

```text
_get_num_chars(a):
  if a.dtype.type 是 np.str_ 的子类:     # 'U' → UCS4，每字符 4 字节
      return a.itemsize // 4
  else:                                  # 'S' 等 → 每字符 1 字节
      return a.itemsize
```

它读的是 `a.dtype.type`（标量类型类，如 `np.str_`/`np.bytes_`），用 `issubclass(..., np.str_)` 判断；返回的是 dtype 的**容量**（字段宽度），不是运行时长度。

#### 4.1.3 源码精读

> [numpy/_core/strings.py:99-107](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L99-L107) —— `_get_num_chars` 全文。docstring 直说它的目的就是「抽象掉 unicode 数组要 `itemsize / 4` 这件事」。

一个值得注意的事实：在整个 `strings.py` 里，`_get_num_chars` **只有一个调用点**——就在下一节的 `_to_bytes_or_str_array` 里（[strings.py:124](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L124)）。其他需要「str_ 宽度」的地方（如 `multiply`、`center`）并不调用它，因为它们用 `str_len` 在**字符单位**下算宽度，再直接拼 `f"{a.dtype.char}{宽度}"`——宽度本来就是字符数，无需 `//4`。换句话说：**当你在「字符单位」下工作时不需要 `_get_num_chars`；只有当你手上只有一个刚推断出来的数组、要从它的 `itemsize` 反推字符数时，才需要它。**

#### 4.1.4 代码实践

**实践目标**：亲手验证 `str_` 的 `itemsize` 是字符数的 4 倍，并区分「容量」与「运行时长度」。

```python
import numpy as np
from numpy._core.strings import _get_num_chars   # 私有函数，仅供学习

u = np.array(['abc', 'de'])      # dtype '<U3'（容量 3），itemsize 12
s = np.array([b'abc', b'de'])    # dtype '|S3'（容量 3），itemsize 3

print(u.dtype, u.itemsize, _get_num_chars(u))   # 预期: <U3 12 3
print(s.dtype, s.itemsize, _get_num_chars(s))   # 预期: |S3 3 3

# 对照：str_len 给的是「运行时实际长度」，不是容量
print(np.strings.str_len(u))                    # 预期: [3 2]
```

**操作步骤**：直接运行上述脚本（需能 import 私有函数；学习用途，生产代码勿依赖私有 API）。

**需要观察的现象**：`u.itemsize` 是 `12` 而 `_get_num_chars(u)` 是 `3`——证明 `//4` 生效；`str_len(u)` 返回 `[3 2]`，第二个元素 `2` 小于容量 `3`，说明运行时长度与容量是两回事。

**预期结果**：如注释所示。`_get_num_chars` 对两种 dtype 都给出「容量」`3`；`str_len` 给出「实际长度」`[3 2]`。

#### 4.1.5 小练习与答案

**练习 1**：为什么对 `str_` 数组要 `itemsize // 4`，而 `bytes_` 不用？

**参考答案**：`str_` 是 UCS4 编码，每个字符固定占 4 字节，`itemsize` 是字节数，所以要除以 4 才得到字符数；`bytes_` 每字符 1 字节，`itemsize` 本身就等于字符数，无需除。

**练习 2**：把一个 `StringDType`（`'T'`）数组传给 `_get_num_chars`，结果有意义吗？为什么？

**参考答案**：没有意义。`'T'` 的标量类型不是 `np.str_` 的子类，函数会走 `else` 分支返回 `itemsize`；但 `StringDType` 的 `itemsize` 是固定的记录/指针头大小，与实际字符数无关。这正是 `_get_num_chars` 只服务于定长 dtype 的原因——变长类型在 `_to_bytes_or_str_array` 里有专门的分支（见 4.2）。

---

### 4.2 `_to_bytes_or_str_array`：把 `object` 中间态还原回正确 dtype

#### 4.2.1 概念说明

如 2.3 所述，`mod`/`decode`/`encode`/`join` 这类操作先用 `_vec_string(a, np.object_, 方法, 参数)` 得到一个 `dtype=object` 的中间数组（每个槽是一个 Python `str`/`bytes`）。但 `numpy.strings` 的公开契约要求返回**类型正确**的数组（`str_`/`bytes_`/`StringDType`）。

`_to_bytes_or_str_array(result, output_dtype_like)` 就是这最后一步还原：它从中间态 `result` 里**读出实际数据的最大宽度**，再按一个「模板」`output_dtype_like` 指定的**dtype 类**重建出正式 dtype，把数据 `astype` 过去。模板决定了「要变成哪种字符串 dtype」，数据决定了「要多宽」。

#### 4.2.2 核心流程

```text
_to_bytes_or_str_array(result, output_dtype_like):
  output_dtype_like = np.asarray(output_dtype_like)        # 把模板规范化成数组
  if result.size == 0:                                     # 空数组特判
      return result.astype(output_dtype_like.dtype)        # 直接转 dtype，保住 shape
  ret = np.asarray(result.tolist())                        # list[str] → 自动推断 <U{max} / |S{max}
  if 模板 dtype 是 StringDType:
      return ret.astype(StringDType)                       # 变长，不需要宽度参数
  return ret.astype(type(模板dtype)(_get_num_chars(ret)))   # 定长：模板的 dtype 类 + ret 的字符数
```

三个要点：

1. **空数组特判**：`result.tolist()` 对空数组会得到 `[]`，`np.asarray([])` 会退化成 `shape=(0,)` 的 `float64` 数组——既丢了原 shape 又丢了字符串 dtype。所以空数组直接 `astype` 模板 dtype，绕开 `tolist`。
2. **StringDType 分支**：变长 dtype 没有「宽度」这个参数，直接 `astype` 过去即可，**不需要** `_get_num_chars`。
3. **定长分支**：用 `_get_num_chars(ret)` 从刚推断出的 `ret` 的 `itemsize` 反推字符数，配合模板的 dtype 类重建一个定宽 dtype（如 `<U7`），再 `astype`。

#### 4.2.3 源码精读

> [numpy/_core/strings.py:110-124](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L110-L124) —— `_to_bytes_or_str_array` 全文。注意第 117-120 行的空数组特判，注释明说「对空数组调 asarray & tolist 会丢 shape 信息」。

「模板」在不同函数里取值不同，决定了输出 dtype 的取向：

| 函数 | 调用 | 模板 `output_dtype_like` | 含义 |
| --- | --- | --- | --- |
| `decode` | [strings.py:579-581](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L579-L581) | `np.str_('')` | 解码结果总是 `str_` |
| `encode` | [strings.py:625-627](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L625-L627) | `np.bytes_(b'')` | 编码结果总是 `bytes_` |
| `mod` | [strings.py:251-252](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L251-L252) | `a`（输入本身） | 输出跟随输入 dtype |
| `_join` | [strings.py:1391-1392](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1391-L1392) | `seq` | 输出跟随被连接序列的 dtype |

可以看到，模板要么是一个「目标标量」（`np.str_('')`/`np.bytes_(b'')`，强制指定输出类型），要么直接拿输入数组当模板（输出跟随输入）。这是「dtype 分发」的一种典型手法：**输出类型由模板说了算，输出宽度由数据说了算**。

#### 4.2.4 代码实践

**实践目标**：手动复现 `_to_bytes_or_str_array` 的还原过程，看清 `object` 中间态如何变成定宽 `str_` 数组，并验证空数组特判。

```python
import numpy as np
from numpy._core.strings import _to_bytes_or_str_array

# 1) 模拟 _vec_string 产出的 object 中间态（长度不一的 Python str）
obj = np.array(['aAaAaA', '  aA  ', 'abBABba'], dtype=object)
restored = _to_bytes_or_str_array(obj, np.str_(''))
print(restored.dtype, restored.dtype.itemsize)   # 预期: <U7 28   (7 字符 × 4 字节)

# 2) 空数组：保住 shape，不会退化成 (0,) float
empty = np.array([], dtype=object).reshape(0, 3)
out = _to_bytes_or_str_array(empty, np.str_(''))
print(out.shape, out.dtype)                       # 预期: (0, 3) <U...
```

**操作步骤**：运行脚本，重点看两件事——`restored` 的 `itemsize` 是 `28`（= 7×4，证明走了 `//4`）；`out.shape` 是 `(0, 3)` 而非 `(0,)`（证明空数组特判保住了 shape）。

**需要观察的现象**：`restored.dtype` 是 `<U7`，宽度等于数据里最长那个字符串 `'abBABba'` 的长度 7；`itemsize` 恰好是 7×4=28。

**预期结果**：如注释所示。

> 待本地验证：`<U7` 的 `itemsize` 在不同字节序下都是 28（UCS4 定长，与字节序无关）；空数组分支返回的 dtype 具体宽度取决于模板（`np.str_('')` 经 `np.asarray` 后的宽度），但 shape 一定被保留——在你本机运行确认即可。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `result.size == 0` 时必须特判，不能统一走 `tolist` 那条路？

**参考答案**：因为 `result.tolist()` 对空数组返回 `[]`，`np.asarray([])` 会推断成 `shape=(0,)`、`dtype=float64`，既丢失了原数组的 shape（如 `(0,3)` 变 `(0,)`），又丢失了「这是字符串」的信息。特判里直接 `result.astype(模板dtype)`，保留 `result` 原本的 shape，只把 dtype 换成模板的。

**练习 2**：为什么 `StringDType` 分支（第 122-123 行）不调用 `_get_num_chars`？

**参考答案**：`StringDType` 是变长存储，dtype 本身不带「宽度」参数——每个元素按自己的实际长度动态分配。所以无需算字符数，直接 `ret.astype(StringDType)` 即可。`_get_num_chars` 只对需要指定宽度的定长 dtype（`str_`/`bytes_`）才有意义。

---

### 4.3 `_clean_args`：为委托 Python 字符串方法清理 `None` 参数

#### 4.3.1 概念说明

`numpy.strings` 的一些函数会「逐元素调用 Python 的 `str`/`bytes` 方法」（通过 `_vec_string`，见 u2-l8、u3-l15）。这些 Python 方法的可选参数**并不统一用 `None` 表示默认值**。比如：

- `str.split(sep, maxsplit)`：`maxsplit` 必须是整数，传 `None` 会抛 `TypeError`；
- `bytes.translate(table, delete)`：`delete` 必须是 bytes 对象，不能传 `None`。

而 `numpy.strings` 的公开签名为了与 Python 保持一致，把这些参数默认值设成 `None`（如 `split(a, sep=None, maxsplit=None)`）。于是在委托给底层方法前，必须把 `None` 参数（以及它**之后**的所有位置参数）删掉，否则底层方法会因为收到 `None` 而报错。`_clean_args` 就是干这个的。

#### 4.3.2 核心流程

```text
_clean_args(*args):
  newargs = []
  for chk in args:
      if chk is None: break        # 遇到第一个 None 就停
      newargs.append(chk)
  return newargs                   # 返回 None 之前的「前缀」
```

关键是「**以及它之后的**」：一旦某个位置参数是 `None`，它和它后面的所有参数都被丢弃。这是安全的，因为这些字符串方法的可选参数都是「从左到右依次可选」，不可能出现「前面默认、后面必填」的签名——所以 `None` 一出现，后面也不会有有意义的值。

#### 4.3.3 源码精读

> [numpy/_core/strings.py:127-141](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L127-L141) —— `_clean_args` 全文，docstring 直说「许多 Python 字符串操作的可选参数不用 `None` 表示默认值，因此需要移除所有 `None` 参数及其后续参数」。

它在文件里的用法可以列成一张表，体会「哪些参数需要清理」：

| 函数 | 调用 | 清理了什么 | 所属路径 |
| --- | --- | --- | --- |
| `decode` | [strings.py:580](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L580) | `_clean_args(encoding, errors)` | B（object 还原） |
| `encode` | [strings.py:626](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L626) | `_clean_args(encoding, errors)` | B（object 还原） |
| `_split` | [strings.py:1441](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1441) | `[sep] + _clean_args(maxsplit)` | D（object 不还原） |
| `_rsplit` | [strings.py:1487](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1487) | `[sep] + _clean_args(maxsplit)` | D（object 不还原） |
| `_splitlines` | [strings.py:1529](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1529) | `_clean_args(keepends)` | D（object 不还原） |
| `translate`(bytes) | [strings.py:1726](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1726) | `[table] + _clean_args(deletechars)` | C（输出复用输入 dtype） |

两个细节值得注意：

1. **`split` 里 `sep` 不进 `_clean_args`**：`[sep] + _clean_args(maxsplit)` 把 `sep` 单独拎出来直接传。因为 `str.split(None)` 是**合法且语义明确**的（按任意空白切分），`sep` 不需要被清理；而 `maxsplit=None` 非法，必须清理。
2. **`translate` 按 dtype 分两个分支**（[strings.py:1717-1727](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1717-L1727)）：`str.translate` 只接受 `table` 一个参数，所以 `str_` 路径直接 `(table,)`，根本不需要 `_clean_args`；`bytes.translate` 还接受 `deletechars`（且不能是 `None`），所以 `bytes_` 路径才用 `[table] + _clean_args(deletechars)`。这是「同一个函数名、不同 dtype 走不同参数集」的典型案例。

#### 4.3.4 代码实践

**实践目标**：直接调用 `_clean_args` 验证它的「截断」行为，并用 `str.split` 证明「不清理 `None` 就会报错」。

```python
from numpy._core.strings import _clean_args

print(_clean_args(' ', None))    # 预期: [' ']       —— maxsplit=None 被丢弃
print(_clean_args(' ', 1))       # 预期: [' ', 1]
print(_clean_args(None, 1))      # 预期: []           —— 遇 None 即止，后面全丢

# 对照：直接给 str.split 传 maxsplit=None 会出错
try:
    'a b c'.split(' ', None)
except TypeError as e:
    print('TypeError:', e)       # 预期: TypeError: 'NoneType' object cannot be interpreted as an integer
```

**操作步骤**：运行脚本。前三个 `print` 验证截断规则；最后一段证明「不清理 `None`」的后果。

**需要观察的现象**：`_clean_args(None, 1)` 返回 `[]`（不是 `[None]` 也不是 `[None, 1]`），证明「遇 `None` 即止、其后全丢」；`'a b c'.split(' ', None)` 抛 `TypeError`，正是 `_clean_args` 要避免的。

**预期结果**：如注释所示。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_split` 里写成 `[sep] + _clean_args(maxsplit)`，而不是 `_clean_args(sep, maxsplit)`？

**参考答案**：因为 `sep=None` 对 `str.split` 是合法的（表示按任意空白切分），不能被清理掉；只有 `maxsplit=None` 非法、需要清理。所以把 `sep` 单独放在前面直接传，只对 `maxsplit` 调 `_clean_args`。如果用 `_clean_args(sep, maxsplit)`，当 `sep=None` 时会把 `sep` 也丢掉，改变语义。

**练习 2**：`_clean_args` 的「遇 `None` 即止、其后全丢」规则，在什么前提下才安全？

**参考答案**：前提是「底层方法的可选参数从左到右依次可选，不存在 `None` 之后还有必填位置参数」的签名。本模块涉及的 `str`/`bytes` 方法（`split`/`rsplit`/`splitlines`/`translate`/`encode`/`decode`）都满足这一前提，所以截断是安全的。若拿它去清理一个「`f(a, b=None, c)`（`c` 必填）」式的方法，就会错误地丢掉 `c`。

---

### 4.4 dtype 分发套路：输出 dtype 是怎么定的

#### 4.4.1 概念说明

把 4.1–4.3 串起来，你会发现 `numpy.strings` 所有函数面临的核心问题其实只有一个：**输出数组的 dtype（尤其是宽度）怎么定？** 围绕这个问题的答案，文件里的函数可以归成四条路径。记住这四条路径，任何一个函数你都能一眼归类，也知道它会用到哪几个辅助函数。

#### 4.4.2 核心流程

```text
某 strings 函数要产生输出，输出 dtype 怎么定？

路径 C（输出复用输入 dtype）：upper / lower / swapcase / capitalize / title / translate
  输出长度恒等于（不超过）输入长度
  → _vec_string(a, a.dtype, 方法[, 参数])          # 直接把输入 dtype 当输出 dtype
  → 不需要 object 中间态，不需要 _get_num_chars

路径 A（可预测变长，C ufunc 写定长 out）：multiply / replace / center·ljust·rjust / zfill / expandtabs / partition
  输出长度可事先算出
  → 用 str_len 预计算最大宽度
  → out_dtype = f"{a.dtype.char}{最大宽度}"          # 'S'/'U' 拼字符数；'T' 走专用循环
  → np.empty_like(..., dtype=out_dtype)
  → 调 C 层 ufunc 直接写入 out
  → 不需要 _to_bytes_or_str_array / _get_num_chars

路径 B（不可预测变长，object 中间态 + 还原）：mod / decode / encode / join
  输出长度无法预测，或本质是 Python 方法
  → _vec_string(a, np.object_, 方法, _clean_args(...))   # object 中间态
  → _to_bytes_or_str_array(result, 模板)                  # 还原（_get_num_chars 在此出场）

路径 D（返回 object 数组，不还原）：_split / _rsplit / _splitlines
  每个元素是「长度不一的列表」，根本无法定宽
  → _vec_string(a, np.object_, 方法, _clean_args(...))    # 就停在这里，返回 object 数组
  → 这正是它们目前不在 numpy.strings 公开命名空间的原因
```

#### 4.4.3 源码精读

四条路径各看一个代表，对照它们的「输出 dtype 决定方式」：

**路径 C** —— `upper`：

> [numpy/_core/strings.py:1118-1119](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1118-L1119) —— `_vec_string(a_arr, a_arr.dtype, 'upper')`。输出 dtype 直接复用 `a_arr.dtype`，既无 `object` 中间态，也不调任何辅助函数。`translate` 的两个分支（[strings.py:1719-1720](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1719-L1720) 与 [strings.py:1722-1727](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1722-L1727)）也是把 `a_arr.dtype` 当输出 dtype，只是 `bytes_` 分支多带了 `_clean_args` 过的 `deletechars`。

**路径 A** —— `multiply`：

> [numpy/_core/strings.py:207-210](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L207-L210) —— `buffersizes = a_len * i`（`a_len = str_len(a)`，字符单位）；`out_dtype = f"{a.dtype.char}{buffersizes.max()}"`；`np.empty_like(..., dtype=out_dtype)`；最后 `_multiply_ufunc(a, i, out=out)`。注意宽度 `buffersizes.max()` 已经是字符数，所以拼 `f"U{N}"` 无需 `//4`。`replace`（[strings.py:1344-1348](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1344-L1348)）是同一套路：先算 `buffersizes`，再 `out_dtype = f"{arr.dtype.char}{buffersizes.max()}"`。

**路径 B** —— `decode` / `mod`：

> [numpy/_core/strings.py:579-581](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L579-L581) —— `_to_bytes_or_str_array(_vec_string(a, np.object_, 'decode', _clean_args(encoding, errors)), np.str_(''))`。两步：先用 `np.object_` 中间态逐元素 `decode`，再用模板 `np.str_('')` 还原成 `str_`。`mod`（[strings.py:251-252](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L251-L252)）同理，模板是输入 `a` 本身。

**路径 D** —— `_split`：

> [numpy/_core/strings.py:1440-1441](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1440-L1441) —— `return _vec_string(a, np.object_, 'split', [sep] + _clean_args(maxsplit))`，注释直说「这会返回一个大小不一的列表数组，所以保持 object 数组」。没有 `_to_bytes_or_str_array` 这一步。

一张「辅助函数出场位置」对照表收尾：

| 辅助函数 | 路径 C | 路径 A | 路径 B | 路径 D |
| --- | --- | --- | --- | --- |
| `_get_num_chars` | ✗ | ✗ | ✓（在 `_to_bytes_or_str_array` 内） | ✗ |
| `_to_bytes_or_str_array` | ✗ | ✗ | ✓ | ✗ |
| `_clean_args` | △（仅 `translate` 的 bytes 分支） | ✗ | ✓ | ✓ |

#### 4.4.4 代码实践

**实践目标**：对同一个 `str_` 输入分别调用三条路径的代表函数，观察输出 dtype，并验证你对路径的归类。

```python
import numpy as np

a = np.array(['abc', 'de'])                  # <U3
print(np.strings.upper(a).dtype)             # 预期: <U3   → 路径 C（输出==输入）
print(np.strings.center(a, 5).dtype)          # 预期: <U5   → 路径 A（预算宽度 5）
print(np.strings.mod(np.array(['%s']), a).dtype)  # 预期: <U3 → 路径 B（object 还原，模板=输入）

b = np.array([b'abc', b'de'])                 # |S3
print(np.strings.decode(b, encoding='ascii').dtype)  # 预期: <U3 → 路径 B（模板=np.str_）
```

**操作步骤**：运行脚本，把每个输出的 dtype 与「路径预言的宽度」对照。

**需要观察的现象**：`upper` 输出仍是 `<U3`（容量不变）；`center(a, 5)` 输出 `<U5`（宽度被预计算成 5）；`mod` 与 `decode` 的输出宽度来自实际数据（这里恰好都是 3）。

**预期结果**：如注释所示。

> 待本地验证：`center` 对 `'de'` 居中到宽度 5 会得到形如 `'  de '`（具体左右分布以本机为准），但 dtype 一定是 `<U5`；`mod` 的模板是输入 `a`，故输出 dtype 跟随 `a`——在你本机确认输出 dtype 与注释一致即可。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `multiply` 走路径 A 而不走路径 B（即不用 `_to_bytes_or_str_array`）？

**参考答案**：因为 `multiply` 的输出长度可以**事先算出**——`str_len(a) * i` 就是每个元素的输出字符数，取最大值即得输出宽度。于是可以直接构造定宽 `out`、让 C 层 ufunc 写入，比「先攒 object 中间态再统一还原」更高效。路径 B 只在长度不可预测时才用。

**练习 2**：`decode` 为什么不能走路径 A？

**参考答案**：`decode` 的输出是解码后的字符串，其长度取决于编码结果（不同编码、不同字节序列解码出的字符数不同），**无法仅凭输入 dtype 和 `str_len` 预测**。所以只能逐元素调 `bytes.decode` 算完，攒成 object 中间态，再用 `_to_bytes_or_str_array` 统一还原。

---

## 5. 综合实践

**任务**：以 `np.strings.encode` 为例，把「路径 B」的两步 dtype 流转手工拆开复现，最后与一次性调用结果对照，画出它的调用链与 dtype 变化表。

`encode` 的实现只有两行（[strings.py:625-627](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L625-L627)）：

```python
return _to_bytes_or_str_array(
    _vec_string(a, np.object_, 'encode', _clean_args(encoding, errors)),
    np.bytes_(b''))
```

把它拆成三步手动跑：

```python
import numpy as np
from numpy._core.strings import _to_bytes_or_str_array, _clean_args
from numpy._core.multiarray import _vec_string

a = np.array(['aAaA', 'abBA'])              # <U4

# 步骤 1：_clean_args 清理可选参数（这里 encoding='cp037'，errors=None 被丢掉）
args = _clean_args('cp037', None)
print('清理后参数:', args)                   # 预期: ['cp037']

# 步骤 2：_vec_string 逐元素调 bytes.encode，攒成 object 中间态
obj = _vec_string(a, np.object_, 'encode', args)
print('中间态 dtype:', obj.dtype)            # 预期: object
print('中间态内容:', obj.tolist())           # 预期: [b'\x81\xc1\x81\xc1', b'\x81\x82\xc2\xc1']

# 步骤 3：_to_bytes_or_str_array 用模板 np.bytes_(b'') 还原成 bytes_
out = _to_bytes_or_str_array(obj, np.bytes_(b''))
print('还原后 dtype:', out.dtype, 'itemsize:', out.dtype.itemsize)  # 预期: |S4 4

# 对照：一次性调用应与手工三步完全一致
direct = np.strings.encode(a, encoding='cp037')
print('与一次性调用一致:', np.array_equal(out, direct), out.dtype == direct.dtype)  # 预期: True True
```

**需要观察的现象与预期结果**：

1. `_clean_args('cp037', None)` 把 `errors=None` 丢掉，只剩 `['cp037']`——证明 `encode` 即使用户不传 `errors`，底层 `bytes.encode` 也不会收到非法的 `None`。
2. 中间态 `obj` 是 `dtype=object`，每个元素是一个 `bytes` 对象（长度可能不一）。
3. 还原后 `out.dtype` 是 `|S4`（`bytes_`，宽度 4，itemsize 4）——模板 `np.bytes_(b'')` 决定了「变成 bytes_」，数据决定了「宽度 4」。
4. 手工三步的结果与 `np.strings.encode` 一次性结果**完全一致**（值与 dtype 都相等）。

**dtype 流转表**（请填完）：

| 阶段 | 数据形态 | dtype | 决定者 |
| --- | --- | --- | --- |
| 输入 `a` | 定长 str 数组 | `<U4` | 用户 |
| `_clean_args` 后 | 参数列表 `['cp037']` | — | `None` 被清理 |
| `_vec_string` 后 | object 中间态 | `object` | 固定用 `np.object_` |
| `_to_bytes_or_str_array` 后 | 定长 bytes 数组 | `|S4` | 模板定类型、数据定宽度 |

> 待本地验证：`cp037` 是标准库自带的 EBCDIC 编码，`'aAaA'` 编码后应为 `b'\x81\xc1\x81\xc1'`。若你的环境缺少该编码，可换成 `'ascii'` 重跑——dtype 流转结论不变（输出仍是 `|S4`）。

## 6. 本讲小结

- `_get_num_chars` 把「`str_` 要 `itemsize // 4`、其余直接用 `itemsize`」封装成一个返回「字段容量」的辅助函数；它只对定长 dtype 有意义，且全文件仅被 `_to_bytes_or_str_array` 调用一次。
- `_to_bytes_or_str_array` 是 `object` 中间态与正式 dtype 之间的还原桥梁：用「模板」决定输出 dtype 类、用实际数据决定宽度；空数组特判保 shape，StringDType 分支跳过宽度计算。
- `_clean_args` 为「逐元素委托 Python 字符串方法」清理不能传 `None` 的可选参数及其后续参数；`split` 里 `sep` 不进它、`maxsplit` 才进，是因为 `sep=None` 合法而 `maxsplit=None` 非法。
- 四条 dtype 分发路径：C（输出复用输入 dtype）、A（预算宽度 + C ufunc 写定长 out）、B（object 中间态 + `_to_bytes_or_str_array` 还原）、D（object 中间态但不还原，故未公开）。
- 三个辅助函数只在路径 B（及 translate 的 bytes 分支、路径 D 的参数清理）出场；路径 A 靠 `str_len` + `f"{char}{宽度}"`，路径 C 靠直接复用输入 dtype——理解这点就能一眼归类任何函数。

## 7. 下一步学习建议

- 下一篇 **u2-l6（比较与拼接类 ufunc）** 会深入路径 A 的代表 `multiply`：重点看它的 `StringDType('T')` 快速路径（`return a * i`）与 `sys.maxsize` 整数溢出保护，是路径 A 在变长 dtype 下的变体。
- 路径 A 里 `out_dtype = f"{a.dtype.char}{...}"` 拼出来的定宽 dtype 最终由 **C 层 ufunc 循环**写入——这些循环如何注册、如何按 `ENCODING` 分发，留到 **u3-l12（C++ ufunc 循环注册）**。
- 路径 B/D 里的 `_vec_string` 是连接「Python 字符串方法」与「NumPy 数组」的通用桥，它的 C 实现留到 **u3-l15（`_vec_string` 的 C 实现）**；在那之前，**u2-l8** 会先从 Python 层讲清楚哪些函数走 `_vec_string`、为什么。
- 建议带着本讲的「四条路径」去读 `strings.py` 任一函数：先看它有没有 `_to_bytes_or_str_array`（→ B）、有没有 `out_dtype = f"..."`（→ A）、是不是 `_vec_string(a, a.dtype, ...)`（→ C）、是不是直接返回 object（→ D）。
