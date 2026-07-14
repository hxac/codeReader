# fromstring：从二进制缓冲区构建 record array

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `numpy.rec.fromstring` 的输入到底是什么——它是「字节缓冲」而不是「字符串」，并解释为什么 `str` 会被拒绝。
- 理解「shape 自动推断」那条核心公式：当不显式给 `shape` 时，NumPy 用 `(len(datastring) - offset) // itemsize` 算出能装下几条记录。
- 看懂 `fromstring` 最后一步 `recarray(shape, descr, buf=datastring, offset=offset)` 是「零拷贝复用底层缓冲」，并因此带上了「缓冲只读则数组只读」的语义。
- 把这三点串起来：能从一段手写 `bytes` 解析出 record array，并预判它的形状、可写性与字段值。

本讲是 u4 单元的第二篇，承接 u4-l1（`fromrecords`，行式构建）与 u3-l1（`recarray.__new__` 与二元 dtype），聚焦「二进制缓冲」这一种数据来源。下一篇 u4-l3 会把同样的思路推广到「二进制文件」。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**(1) 什么是「二进制缓冲」？** 在 Python 里，`bytes`（如 `b'\x01\x02\x03abc'`）和 `bytearray`、`memoryview` 都属于「字节类对象（bytes-like object）」：它们在内存里就是一段连续的、按字节排列的原始数据。这与 `str`（如 `'abc'`）完全不同——`str` 是 Unicode 文本，其内存布局不是「一个字符一个字节」那么简单。本讲的 `fromstring` 只吃前者。

**(2) 结构化 dtype 把字节流「切片」。** 一个结构化 dtype（如 `'u1,u1,u1,S3'`）描述了一条记录里各字段占多少字节、如何解释这些字节。一条记录的总字节数记为 `itemsize`（上例中 \(1+1+1+3=6\) 字节）。给定一段字节流，NumPy 只要从起点开始每 `itemsize` 字节切一刀，就能还原出一条条记录。这正是「二进制 → record array」的全部秘密。

**(3) 「视图」与「拷贝」。** `fromstring` 返回的数组**不复制**字节流，而是把数组的数据指针直接指向传入缓冲的内存（即「视图」）。这意味着：若传入的是可变的 `bytearray`，改数组会改原缓冲；若传入的是不可变的 `bytes`，数组也是只读的。这一点和 `numpy.frombuffer` 的行为一致。

如果你对 `format_parser`、`(record, descr)` 二元 dtype、`recarray.__new__` 的 `buf` 参数还不够熟，建议先复习 u2-l1 与 u3-l1。

## 3. 本讲源码地图

本讲的真实实现全部在一个文件里：

| 文件 | 作用 |
| --- | --- |
| `numpy/_core/records.py` | `fromstring` 的完整实现（约 753–826 行），以及它依赖的 `recarray.__new__`（385–403 行）、`_deprecate_shape_0_as_None`（557–566 行）、`__all__` 导出（15–18 行）、总调度入口 `array` 中的 bytes 分支（1052–1053 行）。 |

提醒：你在代码里写的是 `np.rec.fromstring(...)`，但 `numpy/rec/__init__.py` 只是「再导出垫片」（`from numpy._core.records import *`），真实代码在 `numpy/_core/records.py`。本讲所有永久链接都指向后者。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**函数总览（吃什么、必填什么）**、**shape 自动推断**、**buf/offset 零拷贝复用与只读语义**。

### 4.1 `fromstring` 函数总览：bytes-only 与「dtype 必填」

#### 4.1.1 概念说明

`fromstring` 的职责用一句话概括：**把一段二进制字节缓冲，按一个结构化 dtype 解释成一个 record array**。

它有两个「硬约束」，都写在函数文档里：

1. **名字里虽然有 "string"，但它只接受 bytes-like 对象，明确拒绝 `str`。** 文档原话：「Note that despite the name of this function it does not accept `str` instances.」
2. **必须给出 `dtype` 或 `formats` 中的至少一个**，否则直接抛 `TypeError`。因为不告诉它「每条记录长什么样」，它根本无法切分字节流。

为什么必须 bytes-only？因为在 Python 3 里 `str` 是 Unicode 文本，它内存里的字节布局取决于内部编码，不是一段可以直接按 `itemsize` 切分的「裸字节」。把它当二进制缓冲会得到无意义的结果，所以 NumPy 选择直接拒绝。

#### 4.1.2 核心流程

`fromstring` 的执行过程非常线性，可以概括为「三步走」：

```text
1. 拿到类型描述 descr
     - 若 dtype 和 formats 都没给        -> 抛 TypeError
     - 若给了 dtype                      -> descr = np.dtype(dtype)
     - 否则                              -> descr = format_parser(formats,...).dtype

2. 算出形状
     - itemsize = descr.itemsize         （一条记录占多少字节）
     - shape = _deprecate_shape_0_as_None(shape)   （把 shape=0 译成 None）
     - 若 shape 是 None 或 -1            -> shape = (len(buf) - offset) // itemsize

3. 零拷贝构造
     - return recarray(shape, descr, buf=datastring, offset=offset)
```

注意第 3 步：它**不调用 `fromarrays`/`fromrecords`**，而是直接 `recarray(shape, descr, buf=...)`——这是 `fromstring` 与 u4-l1 的 `fromrecords` 在结构上最大的不同。后者要「装数据」，前者只是「给已有字节换个解释方式」。

#### 4.1.3 源码精读

先看函数签名与文档（含权威示例）：

[`numpy/_core/records.py:753-807`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/rec/../_core/records.py#L753-L807) —— 这是 `fromstring` 的完整定义起点。函数签名有 9 个参数：`datastring`（数据缓冲）、`dtype`/`formats`/`names`/`titles`/`aligned`/`byteorder`（类型描述，与 `format_parser` 一致）、`shape`、`offset`。文档里的示例就是本讲实践的依据（见下文 4.1.4）。

接下来是「类型描述必填」的守卫与 `descr` 的两条构造路径：

[`numpy/_core/records.py:809-815`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/rec/../_core/records.py#L809-L815) —— 第 809–810 行是「dtype 和 formats 都为空就抛 `TypeError`」的硬约束；第 812–815 行是 `descr` 的二选一：给了 `dtype` 就用 `sb.dtype(dtype)`（`sb` 是 `numpy._core.numeric` 模块的别名，`sb.dtype` 即 `np.dtype`），否则交给 `format_parser`。注意：当 `dtype` 与 `formats` 同时给出时，`dtype` 优先（第 812 行 `if dtype is not None` 先命中）。

关于「拒绝 str」有一个容易被忽略的细节：**`fromstring` 的函数体里没有任何一行 `isinstance(datastring, str)` 检查**。拒绝并不是在这里发生的——它发生在最后一步把 `str` 当 `buffer=` 传给 `ndarray.__new__` 时，C 层要求 `buffer` 是 bytes-like 而抛 `TypeError`。所以 `str` 能顺利通过第 809–825 行（因为 `len(str)` 是合法的），却在第 825 行的 `recarray(...)` 内部栽倒。理解这一点，你就能解释为什么报错信息是 `a bytes-like object is required, not 'str'`，而不是 fromstring 自己写的某条消息。

`fromstring` 是公开 API 之一，见导出清单：

[`numpy/_core/records.py:15-18`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/rec/../_core/records.py#L15-L18) —— `__all__` 把 `fromstring` 与 `record`、`recarray`、`format_parser`、`fromarrays`、`fromrecords`、`fromfile`、`array`、`find_duplicate` 一起导出，共 9 个符号。

而当你调用更上层的总调度 `np.rec.array` 并传入 `bytes` 时，它正是分发到 `fromstring`：

[`numpy/_core/records.py:1052-1053`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/rec/../_core/records.py#L1052-L1053) —— `array` 用 `isinstance(obj, bytes)` 判定，命中则 `return fromstring(obj, dtype, shape=shape, offset=offset, **kwds)`。也就是说 `np.rec.array(b'...', dtype=...)` 与 `np.rec.fromstring(b'...', dtype=...)` 等价。

#### 4.1.4 代码实践

**目标**：用一段手写 `bytes` 解析出 record array，并亲眼看到 `str` 被拒绝。

**操作步骤**：

```python
import numpy as np

a = b'\x01\x02\x03abc'
r = np.rec.fromstring(a, dtype='u1,u1,u1,S3')
print(repr(r))
print("names:", r.dtype.names)
print("fields:", r.f0, r.f1, r.f2, r.f3)

# 故意传 str，观察报错
np.rec.fromstring('\x01\x02\x03abc', dtype='u1,u1,u1,S3')
```

**需要观察的现象**：
- `a` 是 6 字节；dtype `'u1,u1,u1,S3'` 的 `itemsize` 也是 6（三个无符号字节各 1，加一个 3 字节字符串），正好对齐。
- 没给 `shape`，函数自动推断为 1 条记录。
- `str` 那一行会抛异常。

**预期结果**（这组输出就是 NumPy 源码 docstring 里的权威 doctest，见 [`numpy/_core/records.py:789-806`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/rec/../_core/records.py#L789-L806)，由 NumPy 测试套件保证）：

```
rec.array([(1, 2, 3, b'abc')],
        dtype=[('f0', 'u1'), ('f1', 'u1'), ('f2', 'u1'), ('f3', 'S3')])
names: ('f0', 'f1', 'f2', 'f3')
fields: [1] [2] [3] [b'abc']
TypeError: a bytes-like object is required, not 'str'
```

注意：dtype 里没有给 `names`，所以字段名自动补成 `f0, f1, f2, f3`（这正是 u2-l2 讲过的「默认命名 + `find_duplicate`」规则在 `format_parser`/`np.dtype` 里的体现）。`b'\x01\x02\x03'` 三个字节被解释成 `1, 2, 3`，`b'abc'` 被解释成长度 3 的字节串字段。

#### 4.1.5 小练习与答案

**练习 1**：`fromstring` 内部有没有一行专门写「`if isinstance(datastring, str): raise ...`」？为什么？

**参考答案**：没有。函数体里找不到对 `str` 的显式检查。拒绝发生在第 825 行把 `str` 当 `buffer` 传给 `ndarray.__new__` 时——C 层要求 buffer 是 bytes-like。所以报错信息来自 ndarray 而非 fromstring。

**练习 2**：如果同时传了 `dtype` 和 `formats`，以哪个为准？

**参考答案**：以 `dtype` 为准。第 812 行 `if dtype is not None:` 先命中，走 `descr = sb.dtype(dtype)`，`formats` 被忽略。

---

### 4.2 `descr.itemsize` 与 shape 自动推断

#### 4.2.1 概念说明

当调用者**不指定 `shape`**（传 `None`）或显式传 `-1` 时，`fromstring` 必须自己算出「这段字节里到底能装下几条记录」。这非常自然：一条记录占 `itemsize` 字节，跳过开头的 `offset` 字节后，剩下的字节能整除出几份，就是记录数。

这里有一个重要但容易被忽略的细节：**尾部不足一条记录的字节会被「静默丢弃」**，因为用的是整除 `//`。

#### 4.2.2 核心流程与数学

设字节缓冲总长度为 \(N\) 字节，跳过开头 `offset` 字节，每条记录占 \(s = \text{itemsize}\) 字节，则自动推断出的记录数为：

\[
n = \left\lfloor \frac{N - \text{offset}}{s} \right\rfloor
\]

对应源码就一行：

```python
if shape in (None, -1):
    shape = (len(datastring) - offset) // itemsize
```

几个要点：

- `len(datastring)` 对 `bytes`/`bytearray`/`memoryview` 都返回字节数（注意：对 `str` 也「能」返回字符数，这正是 str 能混过这行、却在后面 buffer 构造时失败的原因）。
- `//` 是整除，余数部分（不足一条记录的尾部字节）被丢弃，**不报错**。
- 这个推断出的 `shape` 是个整数（标量），表示「一维、n 条记录」。

此外还有一个历史包袱要处理：`shape=0`。在旧版 NumPy 里，`shape=0` 被当成「请帮我推断」。但 NumPy 2.x 把「整数 shape」统一当成单元素元组（即 `shape=0` 应表示「0 条记录」），二者冲突。于是 fromstring 调用 `_deprecate_shape_0_as_None` 把 `0` 翻译成 `None`，并发出 `FutureWarning`。

#### 4.2.3 源码精读

shape 推断的核心三行：

[`numpy/_core/records.py:817-823`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/rec/../_core/records.py#L817-L823) —— 第 817 行 `itemsize = descr.itemsize` 取出「一条记录的字节宽度」；第 820 行 `shape = _deprecate_shape_0_as_None(shape)` 处理 `shape=0` 的历史包袱；第 822–823 行就是上面的整除公式。注意第 822 行把 `None` 和 `-1` 一视同仁——这意味着 `shape=-1` 与不传 `shape` 完全等价，都触发自动推断。

`shape=0` 的弃用守卫：

[`numpy/_core/records.py:557-566`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/rec/../_core/records.py#L557-L566) —— `_deprecate_shape_0_as_None` 用 `shape == 0` 判定（注意是 `==` 不是 `is`，所以 `0.0` 也会命中），命中则发 `FutureWarning`（`stacklevel=3` 指向用户代码）并返回 `None`，让后续逻辑走自动推断；否则原样返回 `shape`。这个函数被 `fromarrays`/`fromrecords`/`fromstring`/`fromfile` 共用，是整个 rec 子包的公共守卫。

**关键对称性**：`offset` 在第 823 行参与 shape 推断，又在第 825 行（见 4.3）传给 `recarray` 的 `offset` 参数——用的是同一个值。所以「推断出的 shape」恰好覆盖「offset 之后」的完整记录区，二者不会错位。

#### 4.2.4 代码实践

**目标**：验证 shape 推断公式，并观察 offset 与尾部余数的处理。

**操作步骤**：

```python
import numpy as np

# 13 字节：1 字节垃圾头 + 2 条完整记录(各 6 字节)
b = b'\xff' + b'\x01\x02\x03abc' + b'\x04\x05\x06def'
print("len:", len(b))                       # 13
r = np.rec.fromstring(b, dtype='u1,u1,u1,S3', offset=1)
print("shape:", r.shape)                    # (13-1)//6 = 2
print(repr(r))

# 尾部不足一条记录的字节会被丢弃：7 字节，offset=0，itemsize=6 -> 只取 1 条，丢 1 字节
r2 = np.rec.fromstring(b'\x01\x02\x03abc\x00', dtype='u1,u1,u1,S3')
print("r2.shape:", r2.shape)                # 7//6 = 1
```

**需要观察的现象**：
- `len(b) == 13`，跳过 1 字节头后剩 12 字节，`12 // 6 == 2`，得到 2 条记录。
- 第二条记录的三个字段应是 `4, 5, 6`，字符串字段是 `b'def'`。
- `r2` 只有 7 字节，`7 // 6 == 1`，尾部那 1 字节被丢弃且**不报错**。

**预期结果**：
```
len: 13
shape: (2,)
rec.array([(1, 2, 3, b'abc'), (4, 5, 6, b'def')],
        dtype=[('f0', 'u1'), ('f1', 'u1'), ('f2', 'u1'), ('f3', 'S3')])
r2.shape: (1,)
```

（`shape` 与「尾部静默丢弃」由第 822–823 行的整除公式直接决定，是确定性结论；具体的 `repr` 文本格式与本讲 4.1.4 的权威 doctest 一致。）

#### 4.2.5 小练习与答案

**练习 1**：缓冲长 14 字节，`offset=2`，dtype 的 `itemsize=4`，自动推断的 shape 是多少？尾部丢弃多少字节？

**参考答案**：\((14-2) // 4 = 12 // 4 = 3\)，即 3 条记录。余数 \((14-2) \bmod 4 = 0\)，尾部丢弃 0 字节。

**练习 2**：`shape=-1` 和不传 `shape`，行为有区别吗？

**参考答案**：没有区别。第 822 行 `if shape in (None, -1):` 把两者一视同仁，都触发自动推断。

---

### 4.3 `recarray` 的 buf/offset 参数：零拷贝复用与只读语义

#### 4.3.1 概念说明

shape 算好之后，`fromstring` 的最后一步是：

```python
_array = recarray(shape, descr, buf=datastring, offset=offset)
return _array
```

这一步的关键是 `buf=datastring`。它告诉 `recarray.__new__`：**别新分配内存，直接把数组的数据指针指向 `datastring` 这块缓冲**（从 `offset` 字节处开始）。这就是「零拷贝视图」。

这条路径带来两个直接后果：

1. **不复制数据，省内存。** 数组与原缓冲共享同一段内存。
2. **只读语义继承自缓冲。** 如果 `datastring` 是不可变的 `bytes`，那么这段缓冲是只读的，生成的数组 `writeable=False`，往里写会抛 `ValueError`；如果传的是可变的 `bytearray`，数组可写，且写数组会改到原 `bytearray`。

这条「buf 视图 + 只读继承」的语义与 `numpy.frombuffer` 完全一致——事实上 `fromstring` 的 docstring 也把 `numpy.frombuffer` 列在 See Also 里。

#### 4.3.2 核心流程

`fromstring` 最后一步把工作完全委托给 `recarray.__new__` 的「buf 分支」：

```text
fromstring:  recarray(shape, descr, buf=datastring, offset=offset)
                          |
                          v
recarray.__new__:  buf is not None  ->  ndarray.__new__(cls, shape, (record, descr),
                                                          buffer=buf, offset=offset, ...)
```

注意 `recarray.__new__` 用的是 `(record, descr)` 这个**二元 dtype**（u3-l1 讲过）：它保持 `descr` 的结构不变，只把标量类型从 `void` 换成 `record`，从而让数组级 `r.f0` 和标量级 `r[0].f0` 的属性访问同时生效。

#### 4.3.3 源码精读

fromstring 的最后一行（零拷贝构造）：

[`numpy/_core/records.py:825-826`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/rec/../_core/records.py#L825-L826) —— `_array = recarray(shape, descr, buf=datastring, offset=offset)`，把推断好的 `shape`、解析好的 `descr`、原始缓冲 `datastring` 和读取起点 `offset` 一起交给 `recarray` 构造，然后直接返回。

`recarray.__new__` 的两条内存路径：

[`numpy/_core/records.py:396-402`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/rec/../_core/records.py#L396-L402) —— 第 396 行 `if buf is None:` 走「新分配」路径（类似 `np.empty`，内存未初始化）；`else` 走「复用缓冲」路径，把 `buffer=buf`、`offset=offset`、`strides=strides` 透传给 `ndarray.__new__`。`fromstring` 永远走 `else` 分支，因为它一定传了 `buf=datastring`。两条路径都统一用 `(record, descr)` 二元 dtype，保证标量类型是 `numpy.record`。

**只读语义从哪来？** `ndarray.__new__` 在拿到 `buffer=buf` 时，会检查这块缓冲是否可写。Python 的 `bytes` 对象是不可变的，其底层缓冲只读；`bytearray` 是可变的。NumPy 把这个「可写性」原样继承到生成数组的 `writeable` 标志上。所以「缓冲只读则数组只读」并不是 `fromstring` 自己实现的，而是 `ndarray` 对 buffer 参数的标准行为——`fromstring` 只是「透传」了缓冲。

#### 4.3.4 代码实践

**目标**：对比 `bytes`（只读）与 `bytearray`（可写）两种输入，体会「零拷贝 + 只读继承 + 共享内存」。

**操作步骤**：

```python
import numpy as np

a = b'\x01\x02\x03abc'

# (1) bytes 不可变 -> 数组只读
rb = np.rec.fromstring(bytes(a), dtype='u1,u1,u1,S3')
print("bytes -> writeable:", rb.flags.writeable)   # False
try:
    rb.f0[:] = 9
except ValueError as e:
    print("写只读数组:", e)

# (2) bytearray 可变 -> 数组可写，且与原缓冲共享内存
ba = bytearray(a)
rw = np.rec.fromstring(ba, dtype='u1,u1,u1,S3')
print("bytearray -> writeable:", rw.flags.writeable)   # True
rw.f0[:] = 9        # 改数组
print("ba after write:", ba)                           # 原缓冲也被改了
```

**需要观察的现象**：
- `bytes(a)` 生成的数组 `writeable=False`，写它抛 `ValueError`。
- `bytearray(a)` 生成的数组 `writeable=True`；把 `f0` 字段改成 9 后，原 `ba` 的第 0 字节也变成了 `0x09`——证明二者共享内存。

**预期结果**：
```
bytes -> writeable: False
写只读数组: assignment destination is read-only
bytearray -> writeable: True
ba after write: bytearray(b'\t\x02\x03abc')
```

（`writeable` 标志与「共享内存写回」由 `ndarray` 对 `buffer=` 的标准行为决定，机制确定；`ValueError` 的确切措辞、`bytearray` 写回后的确切字节「待本地验证」精确文本，但行为方向是确定的：只读缓冲不可写、可变缓冲写回原对象。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 `np.rec.fromstring(b'\x01...', dtype=...)` 的结果默认是只读的？

**参考答案**：因为传入的是 `bytes`（不可变），其底层缓冲只读。`recarray.__new__` → `ndarray.__new__(..., buffer=bytes对象)` 会把缓冲的只读性继承到数组的 `writeable` 标志上。

**练习 2**：想让 `fromstring` 的结果可写，且改动反映到原始数据上，该传什么类型的 `datastring`？

**参考答案**：传 `bytearray`（或其它可写的 bytes-like 对象，如可写的 `memoryview`）。它可写，`fromstring` 零拷贝复用它，因此数组可写且与原对象共享内存。

---

## 5. 综合实践

把本讲三个模块串起来，写一个「序列化—反序列化」的小练习，覆盖 itemsize、shape 推断、buf 零拷贝、只读语义。

**任务**：先用普通结构化 `ndarray` 构造一组成绩数据，用 `tobytes()` 序列化成字节；再用 `np.rec.fromstring` 反序列化成 record array，验证字段值一致；最后用 `bytearray` 包装同一段字节，证明「改 record array 会改原缓冲」。

```python
import numpy as np

# 1) 构造原始数据并序列化
grades_dtype = [('Name', 'U10'), ('Marks', 'f8'), ('Grade', 'i4')]
arr = np.array([('Sam', 33.3, 3), ('Mike', 44.4, 5)], dtype=grades_dtype)
raw = arr.tobytes()
print("raw len:", len(raw), "itemsize:", arr.dtype.itemsize)

# 2) fromstring 反序列化（自动推断 shape，复用 raw 缓冲）
rec = np.rec.fromstring(raw, dtype=grades_dtype)
print("shape:", rec.shape)          # 应为 2
print("names:", rec.dtype.names)    # ('Name', 'Marks', 'Grade')
print(rec.Name, rec.Marks)          # 字段值应与原数据一致

# 3) 用 bytearray 包装，证明零拷贝共享内存
ba = bytearray(raw)
rw = np.rec.fromstring(ba, dtype=grades_dtype)
rw.Marks[0] = 99.9
print("Marks[0] now:", rw.Marks[0]) # 99.9
# 检查原 bytearray 是否被改动：重新从 ba 解析一份，看 Marks[0]
chk = np.rec.fromstring(bytes(ba), dtype=grades_dtype)
print("Marks[0] from ba again:", chk.Marks[0])   # 也应是 99.9，证明共享内存
```

**自检要点**：
- `raw len` 应等于 `itemsize * 2`（shape 推断的依据）。
- 不传 `shape`，`rec.shape` 自动为 2。
- 第 3 步改 `rw.Marks[0]` 后，从同一 `bytearray` 重新解析出的 `chk.Marks[0]` 也变了——这就是「buf 零拷贝 + 共享内存」的直观证据。

（具体数值如 `raw len`、字段 `repr` 文本「待本地验证」精确输出，但 `shape`、names、共享内存行为由源码逻辑确定。）

## 6. 本讲小结

- `fromstring` 把一段 **bytes-like 缓冲**按结构化 dtype 解释成 record array；它**拒绝 `str`**，且拒绝不是来自显式检查，而是 `str` 被当 `buffer` 传给 `ndarray.__new__` 时由 C 层抛出。
- 它**必须**给出 `dtype` 或 `formats` 之一（`dtype` 优先），否则第 809–810 行直接 `TypeError`。
- 不给 `shape`（或给 `-1`）时，按 \(\lfloor (N-\text{offset})/\text{itemsize}\rfloor\) 自动推断记录数，**尾部余数被整除静默丢弃**；`shape=0` 经 `_deprecate_shape_0_as_None` 译成 `None` 并发 `FutureWarning`。
- 最后一步 `recarray(shape, descr, buf=datastring, offset=offset)` 走 `__new__` 的 **buf 分支**，零拷贝复用底层缓冲，并用 `(record, descr)` 二元 dtype 保证标量类型是 `numpy.record`。
- 「缓冲只读则数组只读」是 `ndarray` 对 `buffer=` 的标准行为：`bytes` 只读、`bytearray` 可写且与原对象共享内存。
- 上层 `np.rec.array` 遇到 `bytes` 输入时，正是分发到本函数（第 1052–1053 行）。

## 7. 下一步学习建议

本讲解决了「二进制缓冲 → record array」，缓冲已经在内存里。下一篇 **u4-l3 `fromfile`** 会把同样的思路推广到「二进制文件」：缓冲还在磁盘上，需要先打开文件、`seek` 跳过 `offset`、用 `get_remaining_size` 算剩余字节、处理 `shape=-1` 与字节数不足的校验。学完 u4-l3 后，再看 **u4-l4 `array` 总调度**，你会看到 `bytes`/文件/列表/`recarray` 等所有输入如何统一汇聚到 `fromstring`/`fromfile`/`fromrecords`/`fromarrays` 这几个构造函数。

建议同步阅读：`numpy/_core/records.py` 第 828 行起的 `get_remaining_size` 与 `fromfile`，对比它与本讲 shape 推断逻辑的异同。
