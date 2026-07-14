# fromfile：从二进制文件读取

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `numpy.rec.fromfile` 与上一讲 `numpy.rec.fromstring` 的关键差别：一个是「文件流」，一个是「内存缓冲」，二者在内存共享与拷贝语义上完全不同。
- 解释 `fromfile` 如何用 `hasattr(fd, 'readinto')` 这一个探测动作，同时区分「已经打开的文件对象」与「路径字符串 / pathlib.Path」两种输入。
- 读懂辅助函数 `get_remaining_size` 如何只用 `seek` + `tell` 就量出「从当前位置到文件尾还剩多少字节」。
- 推导 `shape` 含 `-1` 时按 `size // itemsize` 自动推断记录数的那段数学，并解释为什么字节数不足会抛 `ValueError`。
- 独立完成一次「写文件 → 读文件」的 record array 往返（round-trip）实践。

本讲承接 **u4-l2（fromstring）**，是二进制读取的最后一块拼图；它的调度入口将在 **u4-l4（array 总调度）** 里被串联起来。

## 2. 前置知识

阅读本讲前，请确认你已经了解：

- **结构化 dtype 与 itemsize**：一个结构化 dtype（如 `'f8,i4,S5'`）由若干字段拼接而成，`dtype.itemsize` 是一条完整记录占用的字节数。本例中 `f8` 占 8 字节、`i4` 占 4 字节、`S5` 占 5 字节，合计 17 字节。
- **文件对象的随机访问**：Python 的二进制文件对象（`open(path, 'rb')` 返回的对象、`io.BytesIO`、gzip 文件等）通常实现 `io.RawIOBase` / `io.BufferedIOBase` 接口，提供 `tell()`（报告当前位置）、`seek(offset, whence)`（移动读写指针）、`readinto(buf)`（把字节直接读入一块可写缓冲，返回实际读到的字节数）。
- **`seek` 的 whence 参数**：`seek(off, 0)` 绝对定位到文件头第 `off` 字节；`seek(off, 1)` 相对当前位置偏移 `off`；`seek(off, 2)` 相对文件尾偏移（`seek(0, 2)` 即跳到文件末尾）。
- **fromstring 的零拷贝语义（u4-l2）**：`fromstring` 用 `recarray(shape, descr, buf=datastring, offset=offset)` 直接复用 bytes 缓冲，结果数组与原缓冲**共享内存**（只读与否取决于缓冲）。本讲你会看到 `fromfile` 走了一条**截然相反**的路径。

> 提醒：和前面所有讲义一样，`numpy.rec` 子包只是再导出垫片，`fromfile` 的真实实现位于 `numpy/_core/records.py`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `numpy/_core/records.py` | 本讲主角。`fromfile`（行 838–939）、辅助函数 `get_remaining_size`（行 828–834）、`_deprecate_shape_0_as_None`（行 557–566）都在这里；文件头还导入了 `os`、`warnings`、`contextlib.nullcontext` 等本讲用到的工具。 |
| `numpy/_core/tests/test_records.py` | 测试用例。`test_recarray_fromfile`（行 92–109）覆盖「已打开文件对象 + BytesIO + array 调度」三路；`TestPathUsage.test_tofile_fromfile`（行 349–360）覆盖 `pathlib.Path` 路径输入。 |

## 4. 核心概念与源码讲解

### 4.1 fromfile 的整体流程与参数契约

#### 4.1.1 概念说明

`fromfile` 解决的问题是：**磁盘上有一段二进制字节流（通常由别的程序或 `ndarray.tofile` 写出），我知道它的结构化 dtype，想把它读成一个 record array。**

它和 `fromstring` 长得几乎一样，但数据来源不同：

| 维度 | `fromstring`（u4-l2） | `fromfile`（本讲） |
| --- | --- | --- |
| 数据来源 | 内存里的 `bytes` / `bytearray` / `memoryview` | 文件对象 或 路径 |
| 内存关系 | `recarray(..., buf=...)` **共享**缓冲，可能只读 | `recarray(shape, descr)` **新分配**，再 `readinto` 拷贝，结果独立可写 |
| shape 缺省推断 | `(len(buf) - offset) // itemsize` | `get_remaining_size(fd) // itemsize` |
| 拒绝 `str` | 是（C 层 `TypeError`） | 路径必须是 `str`/`os.PathLike`，但**数据**不能是文本模式文件 |

一句话总结：**`fromstring` 是「零拷贝视图」，`fromfile` 是「先分配后灌入」的拷贝读取。** 这个差别直接来自两者最后一段代码——`fromstring` 把缓冲当 `buf` 传进去，`fromfile` 却新建空数组再调 `readinto`。

#### 4.1.2 核心流程

`fromfile` 的执行可以拆成 6 个阶段，顺序严格：

```text
1. 参数契约：dtype 与 formats 至少给一个，否则 TypeError
2. shape 规整：None → (-1,)；int → (shape,)；shape=0 弃用转 None
3. 打开/复用文件：hasattr(fd,'readinto') ?
      是 → 已打开文件，用 nullcontext(fd) 直接用
      否 → 当作路径，open(os.fspath(fd),'rb')
4. 定位 + 量剩余：offset>0 时相对 seek；get_remaining_size(fd) 得到 size
5. 解析 dtype + 算 nbytes：
      itemsize = descr.itemsize
      shapeprod = prod(shape)
      若 shape 含 -1（shapesize<0）→ 推断该维 = size // -shapesize
      nbytes = shapeprod * itemsize
      nbytes > size → ValueError("Not enough bytes left in file")
6. 装填：recarray(shape, descr) 新分配；fd.readinto(_array.data) 灌入
      读到的字节数 != nbytes → OSError("Didn't read as many bytes as expected")
```

#### 4.1.3 源码精读

函数签名与文档（与 `fromstring` 同构，参数完全一致）：

[numpy/_core/records.py:838-881](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/rec/../_core/records.py#L838-L881) — `fromfile` 的定义与 docstring。注意 docstring 里给出的正是本讲综合实践要复现的例子：先 `np.empty(10,dtype='f8,i4,S5')`，再 `tofile`，再 `fromfile` 读回。

第一步——参数契约（与 `fromstring` 完全相同的守卫）：

[numpy/_core/records.py:883-884](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/rec/../_core/records.py#L883-L884) — `dtype` 与 `formats` 都为 `None` 时直接 `raise TypeError`，因为没有任何类型信息就无法解析二进制流。

整体尾段（分配 + 灌入 + 校验）将在 4.5 节精读，中间的 shape 规整、文件分发、剩余字节测量分别在 4.2 / 4.3 / 4.4 展开。

#### 4.1.4 代码实践

这是本讲的**主实践**，完整复现 docstring 里的「写文件 → 读文件」往返。

1. **实践目标**：亲手验证 `fromfile` 能把 `tofile` 写出的二进制原样读回，且字段值一致。
2. **操作步骤**：

   ```python
   import tempfile, os
   import numpy as np

   # 1) 准备一个 10 条记录的结构化数组
   a = np.empty(10, dtype='f8,i4,S5')
   a[0] = (1.5, 100, b'abcde')   # 只给第 0 条赋值，其余为未初始化内存

   # 2) 写到临时文件
   fd = tempfile.TemporaryFile()
   a.tofile(fd)

   # 3) 回到文件头，用 fromfile 读回
   _ = fd.seek(0)
   r = np.rec.fromfile(fd, formats='f8,i4,S5', shape=10)

   # 4) 验证第 0 条记录的三个字段
   print(r[0])            # 期望 (1.5, 100, b'abcde')
   print(r.f0[0], r.f1[0], r.f2[0])   # 1.5 100 b'abcde'
   print(r.dtype)         # [('f0','<f8'),('f1','<i4'),('f2','S5')]
   print(r.dtype.type)    # <class 'numpy.record'>  ← 二元 dtype 生效
   ```

3. **需要观察的现象**：`r[0]` 打印出 `(1.5, 100, b'abcde')`；`r.dtype.type` 是 `numpy.record`（因为内部用了 `(record, descr)` 二元 dtype，见 u3-l1）；`r.f0` 是 `float64` 列、`r.f2` 是 `bytes` 列。
4. **预期结果**：第 0 条三字段与写入值完全一致；其余 9 条是未初始化的「垃圾」字节（因为 `np.empty` 不清零），这正好说明 `fromfile` 是**逐字节原样搬运**，不做任何初始化。
5. 本地可运行，预期如上。

#### 4.1.5 小练习与答案

**练习 1**：如果把上例的 `shape=10` 改成 `shape=20`（文件里只有 10 条），会发生什么？

> **答案**：`nbytes = 20 * 17 = 340` 字节，而文件里只有 `10 * 17 = 170` 字节，`nbytes > size` 成立，抛 `ValueError: Not enough bytes left in file for specified shape and type.`。

**练习 2**：`fromfile` 不给 `dtype` 也不给 `formats` 会怎样？

> **答案**：在函数入口（行 883–884）即 `raise TypeError("fromfile() needs a 'dtype' or 'formats' argument")`，根本不会去碰文件。

---

### 4.2 get_remaining_size：用 seek/tell 量出剩余字节

#### 4.2.1 概念说明

`fromfile` 要判断「文件里还有多少字节可读」，但它**不知道**调用者把文件指针移到了哪里——用户可能已经 `seek` 到文件中段。因此它需要一个「从**当前**位置量到文件尾」的函数。这就是模块级的辅助函数 `get_remaining_size`。

它不是公开 API（不在 `__all__` 里），但思路经典：**记住当前位置 → 跳到尾 → 用差值算长度 → 跳回去**。整个过程对调用者透明，文件指针最终回到原位。

#### 4.2.2 核心流程

```text
pos = fd.tell()              # 记住当前指针位置
try:
    fd.seek(0, 2)            # whence=2：相对文件尾偏移 0 → 跳到末尾
    return fd.tell() - pos   # 末尾位置 - 原位置 = 剩余字节数
finally:
    fd.seek(pos, 0)          # whence=0：绝对跳回原位置（无论前面是否出错）
```

`finally` 保证即使中间抛异常，文件指针也会被还原——这是处理文件这类「带外部状态资源」时的稳健写法。

#### 4.2.3 源码精读

[numpy/_core/records.py:828-834](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/rec/../_core/records.py#L828-L834) — `get_remaining_size` 全文。只有 4 行有效代码，是「seek/tell 三明治」的标准实现。

注意它是**模块级函数**（不在任何类里），紧接在 `fromstring` 之后、`fromfile` 之前定义，专门给 `fromfile` 服务。它依赖文件对象实现了 `tell` 和 `seek`——这正是 docstring 里「The file object must support random access (i.e. it must have tell and seek methods)」的由来。

#### 4.2.4 代码实践

1. **实践目标**：用 `io.BytesIO` 直观看到 `get_remaining_size` 如何随指针位置变化。
2. **操作步骤**（源码阅读型，直接调用模块内函数）：

   ```python
   import io
   from numpy._core.records import get_remaining_size

   fd = io.BytesIO(b'x' * 100)   # 100 字节的内存文件
   print(get_remaining_size(fd))  # 100  （指针在 0）
   fd.seek(30, 0)
   print(get_remaining_size(fd))  # 70   （指针在 30，剩 70）
   print(fd.tell())               # 30   ← 指针被还原，没被破坏
   ```

3. **需要观察的现象**：第一次返回 100；`seek(30)` 后返回 70；最后 `fd.tell()` 仍是 30，证明函数把指针还原了。
4. **预期结果**：如上，三行输出依次为 `100`、`70`、`30`。
5. 本地可运行，预期如上。

#### 4.2.5 小练习与答案

**练习 1**：为什么用 `seek(0, 2)` 而不是 `seek(0)` 来到文件尾？

> **答案**：`seek(0)` 等价于 `seek(0, 0)`，是跳到**文件头**；`seek(0, 2)` 的 `whence=2` 表示相对**文件尾**偏移 0，才是跳到末尾。

**练习 2**：如果传入的文件对象不支持 `seek`（例如一个只能顺序读的网络流），会发生什么？

> **答案**：`fd.seek(0, 2)` 会抛 `AttributeError` 或 `io.UnsupportedOperation`；由于在 `try/finally` 里，`finally` 还会再尝试 `fd.seek(pos, 0)`，同样失败。这正是 docstring 要求「must support random access」的原因——`fromfile` 不支持只能顺序读的流。

---

### 4.3 readinto 探测与 nullcontext：区分文件对象与路径

#### 4.3.1 概念说明

`fromfile` 的第一个参数 `fd` 既可以是「已经打开的文件对象」，也可以是「路径字符串」或 `pathlib.Path`。函数需要用一个统一的方式区分二者，并在两种情况下都用 `with` 语句管理资源。

它选用的探测依据是 **`hasattr(fd, 'readinto')`**：

- 已打开的二进制文件对象（`open(...,'rb')`、`io.BytesIO`、`gzip.GzipFile`、`BufferedReader` 等）都实现了 `readinto` 方法 → 命中「文件对象」分支。
- `str` 路径、`pathlib.Path` 没有 `readinto` → 命中「路径」分支。

这里的关键工具是 `contextlib.nullcontext`：它是一个「什么都不做」的上下文管理器。当 `fd` 已经是打开的文件时，我们**不应该**在 `with` 结束时关闭它（那是调用者的责任），于是用 `nullcontext(fd)` 包一层，让 `with ctx as fd` 这一行对两种情况都成立，统一了代码结构。

> 术语解释：**nullcontext(x)** 返回的上下文管理器进入时把 `x` 原样交出（`as fd` 拿到的就是 `x`），退出时什么都不做。它常用来「在需要上下文管理器、但某些分支其实不需要打开/关闭资源」时抹平差异。

#### 4.3.2 核心流程

```text
if hasattr(fd, 'readinto'):        # 已打开的文件对象
    ctx = nullcontext(fd)           # 不负责关闭
else:                               # 当作路径
    ctx = open(os.fspath(fd), 'rb') # 自己打开，with 结束自动关闭

with ctx as fd:
    ...                             # 后续所有读取都在这个 with 内完成
```

`os.fspath(fd)` 把 `str` / `bytes` / `os.PathLike`（含 `pathlib.Path`）统一转成底层路径表示，所以 `pathlib.Path` 也能直接传。这一点被 `TestPathUsage` 测试专门覆盖（见 4.3.3）。

#### 4.3.3 源码精读

文件头导入了这两个工具（本讲 4.3 / 4.4 都依赖）：

[numpy/_core/records.py:4-7](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/rec/../_core/records.py#L4-L7) — `import os`、`import warnings`、`from contextlib import nullcontext`。

分发分支本体：

[numpy/_core/records.py:894-901](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/rec/../_core/records.py#L894-L901) — `hasattr(fd, 'readinto')` 为真走 `nullcontext(fd)`（注释引用了 GH issue 2504，说明这支持 `io.RawIOBase` / `io.BufferedIOBase`，如 gzip、BytesIO、BufferedReader）；否则 `open(os.fspath(fd), 'rb')` 自己打开。

对应的测试——路径输入走「自己打开」分支：

[numpy/_core/tests/test_records.py:349-360](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/rec/../_core/tests/test_records.py#L349-L360) — `TestPathUsage.test_tofile_fromfile`：把 `pathlib.Path` 直接传给 `fromfile`，验证它走 `open(os.fspath(fd),'rb')` 分支并正确读回。

上层 `np.rec.array` 同样用 `hasattr(obj,'readinto')` 把「已打开文件」分发到 `fromfile`：

[numpy/_core/records.py:1070-1071](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/rec/../_core/records.py#L1070-L1071) — `array` 遇到带 `readinto` 的对象即 `return fromfile(obj, dtype=dtype, shape=shape, offset=offset)`。注意这里只透传 `dtype/shape/offset`——`formats` 已在 `array` 入口被预先解析成 `dtype`（见 u4-l4）。

#### 4.3.4 代码实践

1. **实践目标**：用同一个 dtype，分别以「文件对象」和「`pathlib.Path`」两种方式调用 `fromfile`，验证两条分支都能工作。
2. **操作步骤**：

   ```python
   import tempfile, pathlib
   import numpy as np

   a = np.zeros(3, dtype='f8,i4,S5')
   a[0] = (3.14, 7, b'hello')

   # 方式 A：先打开文件对象，再传给 fromfile（走 nullcontext 分支）
   path = tempfile.mktemp(suffix='.bin')
   with open(path, 'wb') as f:
       a.tofile(f)
   with open(path, 'rb') as fd:
       r1 = np.rec.fromfile(fd, formats='f8,i4,S5', shape=3)

   # 方式 B：直接传 pathlib.Path（走 open(os.fspath(fd)) 分支）
   r2 = np.rec.fromfile(pathlib.Path(path), formats='f8,i4,S5', shape=3)

   print(r1[0])   # (3.14, 7, b'hello')
   print(r2[0])   # (3.14, 7, b'hello')
   ```

3. **需要观察的现象**：两种方式读到的第 0 条记录完全相同。方式 A 结束 `with` 后文件被调用者的 `with` 关闭；方式 B 的文件由 `fromfile` 内部 `with open(...)` 自动关闭。
4. **预期结果**：两次都打印 `(3.14, 7, b'hello')`。
5. 本地可运行，预期如上。

#### 4.3.5 小练习与答案

**练习 1**：为什么文件对象分支要用 `nullcontext(fd)`，而不是直接 `fd`？

> **答案**：为了统一成 `with ctx as fd:` 这一种写法。文件对象分支**不能**在 `with` 退出时关闭文件（文件是调用者打开的，生命周期归调用者管）；`nullcontext` 正是一个「进入原样返回、退出什么都不做」的上下文管理器，既套上了 `with` 的统一形式，又不会误关文件。

**练习 2**：如果把一个 `gzip.GzipFile` 打开的压缩文件对象传给 `fromfile`，会走哪条分支？

> **答案**：`gzip.GzipFile` 实现了 `readinto`，`hasattr(fd,'readinto')` 为真，走 `nullcontext(fd)` 分支，按普通已打开文件对象处理（注释里也把 gzip 列为典型例子）。

---

### 4.4 shape 的 -1 自动推断、字节数校验与 readinto 装填

#### 4.4.1 概念说明

`fromfile` 处理 `shape` 的方式与 `fromstring` 有一个重要不同：它支持 **`shape` 里出现一个 `-1`**，含义是「这一维的大小我不指定，请你按文件剩余字节数自动算出来」。这和 `numpy.reshape` 里 `-1` 的语义一致。

这里的关键在于：`fromfile` 不能像 `fromstring` 那样简单地 `(len(buf)-offset)//itemsize`，因为 `shape` 可以是多维的（比如 `shape=(3,-1)` 表示「3 组，每组多少条由文件决定」），且必须校验**字节数是否足够**。

#### 4.4.2 核心流程与数学推导

源码用「乘积的符号」来探测 `-1`，思路很巧。设结构化 dtype 的 `itemsize = I`，`shape` 规整后是一个元组（如 `(3, -1)` 或 `(-1,)`）。

令 \(P\) 为 shape 中**所有已知维度的乘积**（把 `-1` 当作「占位」先不乘进去的、其余维度的乘积）。若 shape 里含一个 `-1`，则整个元组的连乘积 `shapeprod` 会因为乘进了一个 `-1` 而变成负数：

\[
\text{shapeprod} = -P \quad(\text{当且仅当 shape 恰好含一个 } -1)
\]

于是程序用 `shapesize = shapeprod \cdot I < 0` 来判定「需要推断」。\(-\text{shapesize} = P \cdot I\) 正是「除未知维外，一份（沿未知维方向取 1）所需字节数」。文件剩余 `size` 字节能容纳的未知维大小就是：

\[
n = \left\lfloor \frac{\text{size}}{P \cdot I} \right\rfloor = \left\lfloor \frac{\text{size}}{-\text{shapesize}} \right\rfloor
\]

对应源码 `shape[shape.index(-1)] = size // -shapesize`。推断后重算 `shapeprod`（此时为正），得到最终字节数 `nbytes = shapeprod * I`，再做 `nbytes > size` 的不足校验。

```text
itemsize = descr.itemsize
shapeprod = prod(shape)                      # 含 -1 时为负
shapesize  = shapeprod * itemsize
if shapesize < 0:                            # 即 shape 含 -1
    shape[index_of_-1] = size // -shapesize  # 推断未知维
    shapeprod = prod(shape)                  # 重算（此时为正）
nbytes = shapeprod * itemsize
if nbytes > size: raise ValueError           # 字节不足
_array = recarray(shape, descr)              # 新分配（不共享文件内存）
nbytesread = fd.readinto(_array.data)        # 把字节直接灌入数组缓冲
if nbytesread != nbytes: raise OSError       # 实读 != 应读（被截断）
```

> 重要对比：`fromstring` 最后是 `recarray(shape, descr, buf=datastring, offset=offset)`（共享缓冲、可能只读）；`fromfile` 最后是 `recarray(shape, descr)` 再 `readinto`（新建可写数组、从文件拷贝）。这就是 4.1 表格里「内存关系」那一行的代码根源。

#### 4.4.3 源码精读

shape 规整（在打开文件之前，纯参数处理）：

[numpy/_core/records.py:887-892](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/rec/../_core/records.py#L887-L892) — `_deprecate_shape_0_as_None(shape)` 处理历史遗留的 `shape=0`；随后 `shape is None → (-1,)`（缺省即「自动推断记录数」），`int → (shape,)`（把标量包成一元元组）。

`_deprecate_shape_0_as_None` 本体（在 4.1 的 u4-l1/u4-l2 已多次提到，这里给出落点）：

[numpy/_core/records.py:557-566](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/rec/../_core/records.py#L557-L566) — `shape == 0` 时发 `FutureWarning`（`stacklevel=3` 指向用户代码）并返回 `None`，使旧代码「`shape=0` 表示推断」平滑迁移到 `shape=None`。

定位 + 量剩余 + 解析 dtype（进入 `with` 块后的头几步）：

[numpy/_core/records.py:903-915](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/rec/../_core/records.py#L903-L915) — `if offset > 0: fd.seek(offset, 1)`（注意 `whence=1`：**相对当前位置**偏移 `offset` 字节）；`size = get_remaining_size(fd)`；随后按 `dtype` 优先于 `format_parser` 解析出 `descr`，取 `itemsize`。

shape 推断主体（4.4.2 的数学落地）：

[numpy/_core/records.py:917-923](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/rec/../_core/records.py#L917-L923) — `shapeprod = sb.array(shape).prod(dtype=nt.intp)`；`shapesize < 0` 时用 `size // -shapesize` 替换那个 `-1`。注意 `shape.index(-1)` 只找**第一个** `-1`，因此「推断」只在恰有一个 `-1` 时数学上正确（多个 `-1` 会得到错误结果，最终多半被 `nbytes > size` 校验拦截）。

字节数校验：

[numpy/_core/records.py:925-931](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/rec/../_core/records.py#L925-L931) — `nbytes = shapeprod * itemsize`；若 `nbytes > size`，抛 `ValueError("Not enough bytes left in file for specified shape and type.")`。

装填与读完校验：

[numpy/_core/records.py:934-937](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/rec/../_core/records.py#L934-L937) — `_array = recarray(shape, descr)` 新分配（**不**传 `buf`，故不共享文件内存）；`fd.readinto(_array.data)` 把字节灌入数组底层缓冲，`_array.data` 是数组连续字节的 `memoryview`，必须可写——新分配的数组正满足；若实读字节数 `nbytesread != nbytes`（例如文件在记录中间被截断），抛 `OSError("Didn't read as many bytes as expected")`。

#### 4.4.4 代码实践

1. **实践目标**：体验 `shape=-1` 的自动推断，以及「字节数不足」报错。
2. **操作步骤**：

   ```python
   import io
   import numpy as np

   # itemsize = 8(f8)+4(i4)+5(S5) = 17 字节/条
   a = np.ones(7, dtype='f8,i4,S5')           # 7 条 = 119 字节
   buf = io.BytesIO(a.tobytes())

   # 用 shape=-1 让 fromfile 自己算条数
   r = np.rec.fromfile(buf, formats='f8,i4,S5', shape=-1)
   print(r.shape)          # (7,)  ← 119 // 17 = 7
   print(len(r))           # 7

   # 故意要 8 条（需要 136 字节，但只有 119）
   buf2 = io.BytesIO(a.tobytes())
   try:
       np.rec.fromfile(buf2, formats='f8,i4,S5', shape=8)
   except ValueError as e:
       print("被拦下：", e)   # Not enough bytes left in file ...
   ```

3. **需要观察的现象**：`shape=-1` 时自动得到 7 条；要 8 条时抛 `ValueError`。
4. **预期结果**：打印 `(7,)`、`7`，以及一行 `被拦下： Not enough bytes left in file for specified shape and type.`。
5. 本地可运行，预期如上。

#### 4.4.5 小练习与答案

**练习 1**：若文件有 119 字节（= 7×17），传 `shape=(3, -1)`，最终 `r.shape` 是什么？

> **答案**：已知维乘积 \(P=3\)，\(-\text{shapesize} = 3 \times 17 = 51\)，未知维 \(n = 119 // 51 = 2\)，所以 `r.shape = (3, 2)`，共 6 条（102 字节），尾部 17 字节（不足一组）被静默忽略——因为 `nbytes=102 <= size=119` 通过校验，但 `readinto` 只读 102 字节。注意：这里整除有余数时不会报错，剩余字节被丢弃，行为与 `fromstring` 的「尾部不足一条被丢弃」一致。

**练习 2**：`readinto` 之后为什么要再检查 `nbytesread != nbytes`？前面不是已经校验过 `nbytes <= size` 了吗？

> **答案**：`nbytes <= size` 只保证「按文件长度算够」，但 `readinto` 是一次真实 I/O，可能因为信号中断、管道提前关闭、文件在读取过程中被截断等原因返回**少于**请求的字节数。这个二次检查正是为了捕捉这类「长度够、但实际没读够」的异常情况，转成 `OSError`。

---

## 5. 综合实践

把本讲的「路径分发」「offset 跳过」「`-1` 推断」「readinto 装填」串起来，完成一个略带「真实数据格式」味道的小任务：模拟一个带**文件头**的二进制表。

**任务**：写一个文件，前 16 字节是「文件头」（任意填充），之后才是 5 条 `'i4,f8'` 记录。用 `fromfile` 跳过文件头、自动推断记录数，读回并验证。

```python
import tempfile, struct
import numpy as np

# 1) 准备数据：5 条 (id:int32, score:float64)
rec = np.array([(1, 9.5), (2, 8.0), (3, 7.5), (4, 6.0), (5, 5.5)],
               dtype='i4,f8')
header = b'HEADER_16BYTES'   # 正好 14 字节，补到 16
header = header.ljust(16, b'\x00')

fd = tempfile.TemporaryFile()
fd.write(header)             # 先写 16 字节头
rec.tofile(fd)               # 再写 5 条记录

# 2) 回到开头，用 offset=16 跳过文件头，shape=-1 自动推断
_ = fd.seek(0)
r = np.rec.fromfile(fd, formats='i4,f8', offset=16, shape=-1,
                    names='id,score')

print(r.dtype.names)   # ('id', 'score')
print(r.shape)         # (5,)   ← (5*12) // 12 = 5
print(r.id)            # [1 2 3 4 5]
print(r.score)         # [9.5 8.  7.5 6.  5.5]
print(r[0])            # (1, 9.5)
```

**验收点**：

1. `r.shape` 应为 `(5,)`——证明 `offset=16` 先相对 `seek` 跳过文件头、`get_remaining_size` 量到 `5×12=60` 字节、`-1` 推断出 5。
2. `r.id` 与 `r.score` 能用属性访问取到列——证明读回的是 record array（`(record, descr)` 二元 dtype 生效，见 u3-l1）。
3. 把 `offset=16` 改成 `offset=0` 再跑，第一条记录会变成「乱码」——因为前 12 字节其实是文件头 `'HEADER_16BYT'` 被当成了 `(int32, float64)`，由此体会 `offset` 相对定位的意义。

> 说明：`offset` 在源码里走 `fd.seek(offset, 1)`（`whence=1`，相对当前位置），所以实践里先 `fd.seek(0)` 把指针归零、再让 `offset=16` 相对偏移，等价于从绝对位置 16 开始读。

## 6. 本讲小结

- `fromfile` 与 `fromstring` 形参同构，但**内存语义相反**：前者 `recarray(shape, descr)` 新分配再 `readinto` 拷贝（结果独立可写），后者 `recarray(..., buf=...)` 共享缓冲（可能只读）。
- `hasattr(fd, 'readinto')` 一句探测同时区分「已打开文件对象」与「路径」；文件对象走 `nullcontext(fd)`（不负责关闭），路径走 `open(os.fspath(fd), 'rb')`（`with` 自动关闭），二者统一进同一个 `with` 块。
- `get_remaining_size` 用「记位 → `seek(0,2)` 到尾 → 差值 → `finally` 还原」的三明治写法量出从当前位置到文件尾的剩余字节 `size`，依赖文件支持随机访问（`tell`/`seek`）。
- `shape` 缺省为 `(-1,)`，意为自动推断记录数；含一个 `-1` 时通过「连乘积为负」探测，按 \(n = \lfloor \text{size} / (-\text{shapesize}) \rfloor\) 推断未知维。
- 两道安全闸：`nbytes > size` → `ValueError`（字节不足）；`readinto` 实读字节数 `!= nbytes` → `OSError`（读取被截断）。
- 上层 `np.rec.array` 用同一个 `hasattr(obj,'readinto')` 把已打开文件对象分发到 `fromfile`，但**不会**把路径字符串分发过来（路径需直接调 `fromfile`）。

## 7. 下一步学习建议

- 下一讲 **u4-l4（array：统一调度构造函数）** 会把 `fromfile` 与 `fromstring`/`fromrecords`/`fromarrays`/`recarray` 全部串到 `np.rec.array` 这一个总入口，建议读完后回头对照本讲 4.3.3 里 `array` 的文件分发分支，确认整张调度表闭合。
- 想加深对「`recarray(shape, descr)` 新分配」路径的理解，可复习 **u3-l1（recarray 的 `__new__` 与 `__array_finalize__）**，重点看 `buf=None` 时如何走类似 `empty` 的内存分配。
- 若想了解「只读缓冲 / 共享内存」的另一面，回到 **u4-l2（fromstring）** 对比 `buf=` 参数的零拷贝行为。
- 进阶可阅读 `numpy/_core/multiarray.py` 中 `ndarray.tofile` 的实现，理解 `fromfile` 的「逆操作」是如何把连续字节落盘的，从而完整掌握二进制 record array 的磁盘往返。
