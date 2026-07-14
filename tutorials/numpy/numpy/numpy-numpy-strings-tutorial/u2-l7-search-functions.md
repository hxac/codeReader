# 查找类函数：find/index/count/startswith/endswith

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 `numpy.strings` 里 `find / rfind / index / rindex / count / startswith / endswith` 这七个查找函数为什么长得几乎一模一样。
- 理解 **`MAX` 哨兵**：为什么公开参数 `end=None` 在进入 C 层之前必须被替换成一个巨大整数 `np.iinfo(np.int64).max`。
- 区分 `find`（找不到返回 `-1`）与 `index`（找不到抛 `ValueError`）在 **C 层** 到底是怎么用「返回 `-2`」来表达「我抛了异常」的。
- 看懂 `start / end` 区间是如何原样透传给 C 层、并由 CPython 兼容的 `adjust_offsets` 规范化的。

## 2. 前置知识

在进入源码之前，先回忆三件事：

1. **Python 原生字符串的查找方法**。`"hello".find("l")` 返回最低匹配位置 `2`，找不到返回 `-1`；`"hello".index("z")` 行为和 `find` 一样，但找不到会抛 `ValueError`。`"hello".count("l")` 统计非重叠出现次数；`startswith / endswith` 返回布尔值。本讲的七个函数就是这些方法的「数组版」。

2. **`start / end` 的切片语义**。`s.find(sub, start, end)` 只在 `[start, end)` 区间内查找；`end=None` 表示「一直找到字符串末尾」。`numpy.strings` 完全沿用这套语义。

3. **ufunc 与「输出宽度」**（来自 [u2-l6](u2-l6-compare-and-concat.md)）。`multiply` 之所以要在 Python 层预算输出宽度并显式传 `out`，是因为它的输出还是字符串、宽度未知。而本讲的查找函数输出的是 **整数（位置/次数）** 或 **布尔值**，宽度固定（`int64` / 平台默认 int / `bool`），所以 C 层能凭输入直接推断输出，Python 包装因此极薄——这就是它们「长得一样」的根本原因。

> 小提示：本讲的七个函数在 Python 层 **只用 `@set_module` 装饰、没有 `@array_function_dispatch` 的 dispatcher**（这点和 `multiply` 不同）。原因是它们真正的逻辑全在底层 ufunc 里，Python 包装只负责一件事：把 `end=None` 翻译成 `MAX`。装饰器机制详见 [u2-l4](u2-l4-decorators-and-dispatch.md)。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `numpy/_core/strings.py` | 七个查找函数的 Python 包装，以及 `MAX` 常量的定义。本讲的「门面」入口。 |
| `numpy/_core/src/umath/string_ufuncs.cpp` | C++ 层：把查找的 C 循环挂到 `numpy._core.umath` 的私有 ufunc 上（`init_string_ufuncs`）。决定输入/输出 dtype、决定哪个函数指针对应哪个 ufunc。 |
| `numpy/_core/src/umath/string_buffer.h` | C++ 层：真正的查找算法 `string_find / string_index / string_rindex / string_count`，以及把 `start/end` 规范化的 `adjust_offsets`。 |

一句话概括三者关系：

```
Python: find(a, sub, start, end=None)
   │   end=None  ──►  end = MAX          （strings.py）
   ▼
ufunc: _find_ufunc(a, sub, start, MAX)   （来自 numpy._core.umath）
   │   循环在 string_ufuncs.cpp 里注册    （string_findlike_loop）
   ▼
算法: string_find(buf1, buf2, start, end) （string_buffer.h）
```

## 4. 核心概念与源码讲解

### 4.1 MAX 哨兵：把 `end=None` 变成一个巨大整数

#### 4.1.1 概念说明

底层的 C ufunc 是一个 **4 入 1 出** 的算子：`(字符串, 子串, start, end) -> 结果`。它的 `start` 和 `end` 都是 `int64` 类型的数组参数——也就是说，C 层 **不理解 `None`**，它必须拿到两个具体的整数。

但 Python 的公开 API 为了和原生 `str` 用法一致，希望用户写 `find(a, sub)` 或 `find(a, sub, 2)`，省略 `end` 时表示「到末尾」。这就出现了一个「翻译」需求：**把 `end=None` 翻译成一个表示『尽可能远』的整数**。

NumPy 的做法非常朴素：直接用一个 **足够大的哨兵整数** 代替 `None`：

```python
MAX = np.iinfo(np.int64).max      # = 9223372036854775807 = 2^63 - 1
```

为什么 `MAX` 行得通？因为字符串再长，长度也远远小于 \( 2^{63}-1 \)。C 层拿到 `end = MAX` 后，会把 `end` **裁剪到字符串实际长度**（见 4.4 节的 `adjust_offsets`），于是「查到 `MAX`」等价于「查到末尾」。整个系统因此可以把 `None` 的特殊处理 **完全挡在 Python 层**，C 层永远只看到 4 个 `int64`，逻辑干净。

#### 4.1.2 核心流程

```
用户调用 find(a, "x", 0, None)
        │
        │  Python 包装：end = MAX（当 end is None）
        ▼
_find_ufunc(a, "x", 0, MAX)        # end 现在是一个普通 int64
        │
        │  C 层 adjust_offsets：if end > len: end = len
        ▼
等价于在 [0, len(a)) 全区间查找       # 即「查到末尾」
```

关键点：`MAX` 不是魔法，它只是「一个保证比任何字符串都长的数」，让 C 层的裁剪逻辑自然把它收敛到字符串长度。

#### 4.1.3 源码精读

`MAX` 在模块顶部定义，紧贴 `__all__`：

[numpy/_core/strings.py:93-93](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L93-L93) 定义 `MAX = np.iinfo(np.int64).max`，即 64 位有符号整数的最大值。

底层 ufunc 来自 `numpy._core.umath`，导入时改名成 `_xxx_ufunc`，强调「它们是 ufunc、不是普通函数」：

[numpy/_core/strings.py:41-56](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L41-L56) 把 `count / endswith / find / index / rfind / rindex / startswith` 这七个 ufunc 分别以 `_count_ufunc` 等名字导入。

注意：这些名字（`find`、`index`、`count` …）在 `numpy._core.umath` 里是 **NumPy 内部的私有 ufunc**（不在公开文档里），它们的循环是在 `string_ufuncs.cpp` 的 `init_string_ufuncs` 里挂上去的——4.3 节会看到具体怎么挂。

#### 4.1.4 代码实践

1. **实践目标**：验证 `MAX` 的值，并确认 `end=None` 与 `end=MAX` 行为完全一致。
2. **操作步骤**：

```python
import numpy as np
from numpy._core.strings import MAX   # 私有常量，仅用于学习

print(MAX)                 # 9223372036854775807
print(MAX == np.iinfo(np.int64).max)   # True

a = np.array(["hello world"])
print(np.strings.find(a, "world"))            # [6]   （end 省略）
print(np.strings.find(a, "world", 0, MAX))    # [6]   （显式传 MAX）
```

3. **需要观察的现象**：第 3、4 行输出相同，说明 Python 把省略的 `end` 等价成了 `MAX`。
4. **预期结果**：`[6]` 与 `[6]`。
5. **若无法运行**：上述命令需要可 import 的 NumPy；若环境无 NumPy，标注「待本地验证」并改为源码阅读：在 `strings.py` 里搜索 `end = end if end is not None else MAX`，确认七处用法一致。

#### 4.1.5 小练习与答案

- **练习**：为什么 NumPy 选 `np.iinfo(np.int64).max`，而不是 `np.iinfo(np.int32).max` 或 `float('inf')`？
- **答案**：因为底层 ufunc 的 `start/end` 形参类型是 `int64`（4.3 节会看到 `dtypes[2] = dtypes[3] = NPY_INT64`），必须用同类型能表示的最大值才能安全「撑满」；`float('inf')` 根本无法转成 `int64`，会在调用 ufunc 前就报类型错误。

---

### 4.2 七个查找函数的统一骨架

#### 4.2.1 概念说明

打开 `strings.py`，你会发现 `find / rfind / index / rindex / count / startswith / endswith` 这七个函数的函数体 **完全同构**——除了名字、文档、以及调用的那个 `_xxx_ufunc`，每一处都长这样：

```python
end = end if end is not None else MAX
return _xxx_ufunc(a, needle, start, end)
```

这里的 `needle` 是「子串 / 前缀 / 后缀」，对 `startswith` 叫 `prefix`、对 `endswith` 叫 `suffix`，对其它五个叫 `sub`。除此之外没有任何分支、没有任何 dtype 判断、没有任何缓冲区预计算。

> 这正是上一讲 [u2-l5](u2-l5-helpers-and-dtype-dispatch.md) 总结的 **「C 路径」**：输出 dtype 完全由 C 层循环注册时写死（整数或布尔），Python 层不需要预算宽度、不需要构造 `out`。对比 `multiply`（[u2-l6](u2-l6-compare-and-concat.md)）那套「`str_len` 预算 + `out` 必填」的三步流程，这里的薄壳就是「输出宽度固定」带来的红利。

#### 4.2.2 核心流程

每个查找函数的生命周期只有两步：

1. **归一化 `end`**：`end is None` → `MAX`，否则原样保留。
2. **转交 ufunc**：`return _xxx_ufunc(a, needle, start, end)`，四个位置参数一一对应 C 层的 4 个 `int64`/字符串输入。

没有第三步。所有的 dtype 分发（StringDType `'T'` / `bytes_` `'S'` / `str_` `'U'`）、所有的字符编码处理，都由底层 ufunc 在 C 层完成。

#### 4.2.3 源码精读

以 `find` 为代表，看这「两行体」：

[numpy/_core/strings.py:255-290](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L255-L290) 定义 `find(a, sub, start=0, end=None)`，函数体只有 `end = end if end is not None else MAX` 与 `return _find_ufunc(a, sub, start, end)` 两行。

其余六个函数是同一个模子，仅替换 ufunc 名：

| 函数 | 源码（含两行体） | 返回类型 |
| --- | --- | --- |
| `find` | [strings.py:256-290](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L256-L290) → `_find_ufunc` | 最低位置（找不到 `-1`） |
| `rfind` | [strings.py:294-333](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L294-L333) → `_rfind_ufunc` | 最高位置（找不到 `-1`） |
| `index` | [strings.py:337-367](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L337-L367) → `_index_ufunc` | 最低位置（找不到抛错） |
| `rindex` | [strings.py:371-401](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L371-L401) → `_rindex_ufunc` | 最高位置（找不到抛错） |
| `count` | [strings.py:405-446](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L405-L446) → `_count_ufunc` | 非重叠出现次数 |
| `startswith` | [strings.py:450-487](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L450-L487) → `_startswith_ufunc` | 布尔数组 |
| `endswith` | [strings.py:491-528](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L491-L528) → `_endswith_ufunc` | 布尔数组 |

这七个名字也都登记在模块的 `__all__` 里：

[numpy/_core/strings.py:77-78](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L77-L78) 把 `find, rfind, index, rindex, count, startswith, endswith` 列为公开的 UFuncs。

#### 4.2.4 代码实践

1. **实践目标**：用一个数组把七个函数一次性跑通，体会它们「同构」。
2. **操作步骤**：

```python
import numpy as np
a = np.array(["NumPy is a Python library", "Computer Science"])
print(np.strings.find(a, "e"))          # 各自第一个 'e' 的位置
print(np.strings.rfind(a, "e"))         # 各自最后一个 'e'
print(np.strings.count(a, "e"))         # 各自 'e' 的个数
print(np.strings.startswith(a, "C"))    # 是否以 'C' 开头
print(np.strings.endswith(a, "e"))      # 是否以 'e' 结尾
```

3. **需要观察的现象**：每个函数都对数组 **逐元素** 施加对应的 `str` 方法，返回与输入同 shape 的数组。
4. **预期结果**：`find` → `[12 7]`（注意 "NumPy is a Python library" 第一个 `e` 在 `library` 里，索引 16；"Computer Science" 第一个 `e` 在索引 7。请自行核对——这是观察数组逐元素行为的最好方式）。**实际数值请以本地运行为准**，这里重点在于「同一份代码、七个 ufunc」。
5. **若无法运行**：标注「待本地验证」，改为阅读 `strings.py:256-528` 七处函数体，确认它们除 ufunc 名外完全一致。

#### 4.2.5 小练习与答案

- **练习 1**：为什么这七个函数不需要像 `multiply` 那样在 Python 层预算输出宽度？
- **答案**：因为它们的输出是整数或布尔，宽度固定（`int64`/平台默认 int/`bool`），C 层 `resolve_descriptors` / 循环注册时已写死输出 dtype；而 `multiply` 的输出仍是字符串，宽度依赖内容，必须 Python 层提前算好。
- **练习 2**：如果把这七个函数的 `@set_module("numpy.strings")` 去掉，对外会有什么变化？
- **答案**：函数本身的 `__module__` 会变成 `numpy._core.strings`，于是 `help(np.strings.find)` / 文档系统会把它归到错误的模块；功能不受影响（详见 [u2-l4](u2-l4-decorators-and-dispatch.md) 关于 `set_module` 只改身份不改行为的说明）。

---

### 4.3 `find` 返回 -1，`index` 抛 ValueError：C 层用 `-2` 表达「出错」

#### 4.3.1 概念说明

这是本讲最精妙的一处设计。在 Python 里，`find` 与 `index` 的区别看起来是「返回值 vs 抛异常」——但 C 的 ufunc 循环不能「抛异常」，它只能 **返回一个状态码**。那 C 层怎么告诉上层「我抛了一个 `ValueError`」？

NumPy 的约定是：

- 算法函数（`string_find` 等）正常时返回位置（`>= 0`）或 `-1`（没找到）。
- 需要抛异常的版本（`string_index` / `string_rindex`）在「没找到」时，先 **主动设置一个 Python 异常**（`npy_gil_error(PyExc_ValueError, "substring not found")`），再返回一个 **特殊状态码 `-2`** 表示「我已经抛了异常，请中止」。
- 外层循环看到 `-2`，就立刻 `return -1` 中止整个 ufunc 调用，让上层把那个 `ValueError` 透传给 Python。

于是 `-1` 和 `-2` 分工明确：

| 返回值 | 含义 |
| --- | --- |
| `>= 0` | 找到了，这是位置 |
| `-1` | 没找到（仅 `find`/`rfind`/`count` 会返回它；`index`/`rindex` 不会把它交给外层） |
| `-2` | 我已设置好 Python 异常，请中止循环 |

#### 4.3.2 核心流程

```
string_index(buf1, buf2, start, end)
        │
        │  pos = string_find(...)         （先复用 find 的逻辑）
        │
        ├── pos == -1 ?
        │     ├── 是 → npy_gil_error(ValueError, "substring not found")
        │     │        return -2          （告诉循环「抛过异常了」）
        │     └── 否 → return pos
        ▼
string_findlike_loop:
        idx = function(...)
        if idx == -2: return -1           （循环中止，异常透传）
        *out = idx
```

**一个重要后果**：因为循环遇到 `-2` 立刻中止，`np.strings.index` 作用在数组上时，**只要有一个元素找不到子串，整个调用就会抛 `ValueError`、不会产出部分结果**。这与 `find`「找不到给 `-1`、安静返回整个数组」形成鲜明对比，是写数据处理代码时常踩的坑。

#### 4.3.3 源码精读

先看算法本体。`string_index` 直接复用 `string_find`，仅在「没找到」时换成分抛异常 + 返回 `-2`：

[string_buffer.h:938-949](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h#L938-L949) `string_index` 先调用 `string_find`，若结果为 `-1` 则用 `npy_gil_error(PyExc_ValueError, "substring not found")` 设置异常并返回 `-2`。开头注释明确写了「`string_index` returns -2 to signify a raised exception」。

而 `string_find` 本身「没找到」只是平平淡淡地返回 `-1`：

[string_buffer.h:846-859](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h#L846-L859) `string_find`：当可用区间装不下子串时 `return (npy_intp) -1`；空子串时返回 `start`（与 CPython 一致）。

`string_rindex` 是 `string_rfind` 的「抛错版」，同样的 `-2` 套路：

[string_buffer.h:1044-1055](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h#L1044-L1055) `string_rindex` 调 `string_rfind`，`-1` 时抛 `ValueError` 并返回 `-2`。

再看循环如何识别 `-2` 并中止：

[string_ufuncs.cpp:312-346](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L312-L346) `string_findlike_loop` 是 `find/rfind/index/rindex/count` **五个函数共用** 的循环：它从 `context->method->static_data` 取出当前元素对应的函数指针（`string_find` 或 `string_index` 等）逐元素调用；`if (idx == -2) return -1;` 这一行就是「发现子函数已抛异常，立刻中止整个 ufunc」。

而这五个函数之所以能共用同一个循环，是因为注册时把它们各自的函数指针通过 `static_data` 塞了进去：

[string_ufuncs.cpp:1601-1641](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1601-L1641) 用 `findlike_names[] = {"find","rfind","index","rindex","count"}` 和两个函数指针数组（ASCII 版 / UTF32 版）配对，在循环里把 `string_find / string_rfind / string_index / string_rindex / string_count` 逐一挂到同名 ufunc 上——同一个 `string_findlike_loop`、不同的函数指针。

输出 dtype 也在这一段写死：

[string_ufuncs.cpp:1597-1599](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1597-L1599) `dtypes[2] = dtypes[3] = NPY_INT64`（start/end）、`dtypes[4] = NPY_DEFAULT_INT`（输出位置/次数），印证了 4.2 节「输出宽度固定、不需要 Python 预算」的结论。

`startswith` / `endswith` 是另一组：它们共用 `string_startswith_endswith_loop`，用 `STARTPOSITION::FRONT/BACK` 区分前缀还是后缀，输出是 `NPY_BOOL`：

[string_ufuncs.cpp:1662-1685](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1662-L1685) `startswith/endswith` 的 dtype 设为输出 `NPY_BOOL`，二者仅靠 `startpositions[] = {FRONT, BACK}` 区分，复用同一个循环。

#### 4.3.4 代码实践（本讲核心实践）

1. **实践目标**：对比 `find` 返回 `-1` 与 `index` 抛 `ValueError`，并验证 `index` 在数组上的「一处缺失即整体失败」行为。
2. **操作步骤**：

```python
import numpy as np

a = np.array(["Computer Science", "hello world"])

# (1) find：找不到安静返回 -1
print(np.strings.find(a, "zzz"))        # [-1 -1]

# (2) index：找不到抛 ValueError
try:
    np.strings.index(a, "zzz")
except ValueError as e:
    print("ValueError:", e)             # substring not found

# (3) 关键对比：数组里只要有一个元素找不到，index 就整体失败
b = np.array(["has-x", "no x here"])
print(np.strings.find(b, "x"))          # [4 -1]   （有结果）
try:
    np.strings.index(b, "x")            # 第二个元素没有 'x'
except ValueError as e:
    print("index 仍抛错:", e)            # 即使第一个元素能找到
```

3. **需要观察的现象**：`find` 总是返回完整数组；`index` 在 (2) 和 (3) 都抛异常，且 (3) 不会因为「第一个元素能找到」就放过。
4. **预期结果**：如上注释；`index` 的异常信息为 `substring not found`。
5. **若无法运行**：标注「待本地验证」，改为阅读 [string_buffer.h:938-949](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h#L938-L949) 与 [string_ufuncs.cpp:330-336](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L330-L336)，用源码推导出上述行为。

#### 4.3.5 小练习与答案

- **练习 1**：为什么用 `-2` 而不是用别的值（比如 `-99`）作为「已抛异常」的哨兵？
- **答案**：因为正常返回值里最「像错误」的只有 `-1`（CPython 沿用多年的「找不到」标记），已被 `find` 占用；`-2` 是离 `-1` 最近、又不会与任何合法位置（`>= 0`）或「找不到」（`-1`）冲突的最小选择，便于阅读和维护。
- **练习 2**：`count` 在找不到子串时返回什么？它会抛异常吗？
- **答案**：返回 `0`，不抛异常。`count` 对应的是 `string_count`，它属于 `findlike` 五件套但语义是「计数」，没有任何「出错」分支，所以永远不会返回 `-2`。

---

### 4.4 `start/end` 区间透传与 CPython 兼容的 `adjust_offsets`

#### 4.4.1 概念说明

前面的章节都在讲「函数怎么转发」，这一节看 **C 层拿到 `start/end` 之后做了什么**。Python 的 `str.find(sub, start, end)` 对区间有一套与切片完全一致的规范化规则：负数 `end` 相对末尾计算、超出长度的 `end` 裁剪到长度、负数 `start` 也相对末尾计算。

NumPy 为了和 CPython **逐字符一致**，直接照搬了 CPython `bytes_methods.c` 里的 `adjust_offsets` 函数。这意味着 `np.strings.find(a, "x", -3, 100)` 和 `"....x".find("x", -3, 100)` 的区间语义完全相同——`MAX` 也是在这里被「收敛」成实际长度的。

#### 4.4.2 核心流程

`adjust_offsets(start, end, len)` 的规范化逻辑：

```
若 end > len :  end = len                  # MAX 在这里被裁掉
若 end < 0   :  end += len; 若仍 < 0 则 end = 0
若 start < 0 :  start += len; 若仍 < 0 则 start = 0
```

于是：

- `end = MAX` → `MAX > len` 恒成立 → `end = len`，即「查到末尾」。
- `end = -2`（长度 5 的串）→ `end = 3`，等价于切片 `[:3]`。
- `start = -2`（长度 5）→ `start = 3`。

规范化后再判断「可用区间是否装得下子串」：

\[ \text{可用区间长度} = end - start \quad\Longrightarrow\quad \text{若 } end - start < len(sub) \text{ 则找不到} \]

#### 4.4.3 源码精读

[string_buffer.h:825-844](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h#L825-L844) `adjust_offsets` 函数，注释里写明它取自 CPython 的 `bytes_methods.c`，目的是与 CPython 的 `str.find/rfind` 等对 `start/end` 的处理保持一致。

随后 `string_find` 调用它：

[string_buffer.h:850-859](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h#L850-L859) `string_find` 先 `adjust_offsets(&start, &end, len1)` 规范化区间，再用 `end - start < len2` 判断装不装得下子串，装不下直接返回 `-1`。

`string_count` 同样先 `adjust_offsets`：

[string_buffer.h:1062-1076](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h#L1062-L1076) `string_count` 规范化区间后，若装不下返回 `0`；空子串则返回 `end - start + 1`（与 CPython `"abc".count("") == 4` 一致）。

正因为 `start/end` 在 C 层被这样规范化，Python 层才能放心地把 `None` 直接换成 `MAX`——两者最终都会被 `adjust_offsets` 收敛到 `len`。

#### 4.4.4 代码实践

1. **实践目标**：用 `start/end` 限制查找区间，并验证结果与原生 `str` 方法完全一致。
2. **操作步骤**：

```python
import numpy as np

a = np.array(["Computer Science"])

# 限定区间：只在 [0, 8) = "Computer" 里找 "Science"
print(np.strings.rfind(a, "Science", 0, 8))     # [-1]  区间内没有

# 放开区间
print(np.strings.rfind(a, "Science", 0, None))  # [9]

# 与原生 str 对照
s = "Computer Science"
print(s.rfind("Science", 0, 8), s.rfind("Science", 0, len(s)))   # -1 9

# 负数 end 等价于切片语义
print(np.strings.find(a, "e", 0, -1))           # 在去掉最后一个字符的区间里找第一个 'e'
print("Computer Science".find("e", 0, -1))      # 应与上一行一致
```

3. **需要观察的现象**：`np.strings.rfind(...)` 的结果与 Python `str.rfind(...)` 的对应调用 **逐位相同**；负数 `end` 表现出切片语义。
4. **预期结果**：如注释所示（`-1`、`9`，以及两处 `find` 结果相同）。
5. **若无法运行**：标注「待本地验证」，改为阅读 `adjust_offsets`，手动代入 `end=-1, len=16` 推导出 `end = -1 + 16 = 15`，确认它等价于「排除最后一个字符」。

#### 4.4.5 小练习与答案

- **练习**：`np.strings.find(np.array(["abcdef"]), "f", 0, MAX)` 的执行路径里，`end` 一共经历了哪些取值？
- **答案**：Python 层 `end = MAX`；进入 ufunc 时仍为 `MAX`；C 层 `adjust_offsets` 因 `MAX > len(6)` 把它改成 `6`；最终在 `[0, 6)` 区间查找，找到 `"f"` 在位置 `5`。

---

## 5. 综合实践

把本讲四条主线串起来，做一个「迷你日志分析器」。

**任务**：给定一组日志字符串，用本讲的七个查找函数提取信息，并观察 `find` 与 `index` 在脏数据下的差异。

```python
import numpy as np

logs = np.array([
    "2026-07-14 INFO  starting server",
    "2026-07-14 WARN  disk almost full",
    "2026-07-14 ERROR disk failure",
])

# 1) 级别判定：startswith
print("ERROR 开头？", np.strings.startswith(logs, "2026"))    # 都以日期开头

# 2) 定位关键词：find（安全，找不到给 -1）
err_pos = np.strings.find(logs, "disk")
print("disk 位置：", err_pos)                                # [-1, 19, 20] 之类

# 3) 统计出现次数：count
print("'a' 出现次数：", np.strings.count(logs, "a"))

# 4) 限定区间查找：只在前 10 个字符里找 '-'
print("前 10 字符里的 '-' 数量：", np.strings.count(logs, "-", 0, 10))

# 5) 对比 index：只要有一行没有 'disk'，整体就抛错
try:
    np.strings.index(logs, "disk")
except ValueError as e:
    print("index 在脏数据下失败：", e)
```

**思考题（做完后回答）**：

1. 为什么第 5 步会失败？把失败那一行的 `"disk"` 改成每行都有的子串（比如 `"2026"`）后再试，是否成功？
2. 如果你想「对每行都查找、找不到就填 `-1`、绝不抛错」，应该选 `find` 还是 `index`？为什么？
3. 第 4 步如果把 `10` 换成 `MAX`，结果会如何变化？为什么？

> 参考答案：1. `index` 遇到第一个无 `"disk"` 的行就抛 `ValueError` 并中止；改成 `"2026"` 后每行都命中，返回每行的位置数组。2. 选 `find`，它在「找不到」时返回 `-1` 而非抛错，适合带缺失值的批量处理。3. 结果会统计 **整行**（而非前 10 字符）的 `'-'` 个数，因为 `MAX` 经 `adjust_offsets` 后被收敛成行长度。

## 6. 本讲小结

- 七个查找函数（`find/rfind/index/rindex/count/startswith/endswith`）共享一个极薄的 Python 骨架：**`end = end if end is not None else MAX` + 调用对应 `_xxx_ufunc`**，没有任何 dtype 分支。
- **`MAX = np.iinfo(np.int64).max`** 是把 `end=None` 翻译成 C 层 `int64` 的哨兵，C 层的 `adjust_offsets` 会把它收敛到字符串实际长度。
- `find` 与 `index` 的区别发生在 **C 层**：`string_index` 在找不到时主动设置 `ValueError` 并返回 **`-2`**，外层 `string_findlike_loop` 看到 `-2` 立即中止整个 ufunc——因此数组中 **任意一个元素** 找不到都会让 `index` 整体失败。
- `find/rfind/index/rindex/count` 五件套 **共用同一个循环**，靠函数指针区分；`startswith/endswith` 二件套 **共用另一个循环**，靠 `FRONT/BACK` 起点位置区分；输出 dtype 分别是 `NPY_DEFAULT_INT` 与 `NPY_BOOL`。
- `start/end` 的切片语义由照搬自 CPython 的 `adjust_offsets` 实现，保证 `np.strings` 的查找结果与原生 `str` 逐字符一致。

## 7. 下一步学习建议

- 下一讲 [u2-l8](u2-l8-case-and-vecstring.md) 将转向 **不走 ufunc** 的函数（`upper/lower/swapcase/...`、`mod/decode/encode/translate`），看它们为何改走 `_vec_string` 逐元素桥——与本讲的「全走 ufunc」形成对照。
- 若想深挖 C 层性能，建议接着读 [u3-l13](u3-l13-buffer-and-fastsearch.md)：`string_buffer.h` 的 `getchar` 模板与 `string_fastsearch.h` 的子串搜索算法，正是本讲 `string_find/string_count` 背后的加速原语。
- 想理解变长 `StringDType('T')` 下的查找，可提前翻阅 [u3-l14](u3-l14-stringdtype-ufuncs.md)：变长类型在 `stringdtype_ufuncs.cpp` 里有一套独立的同构循环（同样用 `string_find<ENCODING::UTF8>` 等算法）。
