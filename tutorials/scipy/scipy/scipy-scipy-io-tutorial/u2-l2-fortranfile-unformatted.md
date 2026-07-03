# Fortran 无格式顺序文件（FortranFile）

## 1. 本讲目标

科学计算领域有大量历史沉淀下来的 Fortran 程序，它们产出的二进制文件需要被 Python 读取，反之 Python 计算的结果有时也要回写给 Fortran。`scipy.io` 通过 `FortranFile` 类专门处理这类文件。

学完本讲你应该能够：

- 说出 Fortran **无格式顺序文件（unformatted sequential file）** 的二进制 record 结构，并能用十六进制/十进制工具肉眼验证。
- 熟练使用 `FortranFile.write_record` / `read_record` / `read_ints` / `read_reals` 完成一个写入—关闭—读回的 round-trip。
- 理解为什么读多维数组时要用 `reshape(..., order='F')` 或 `.T`，避免行列顺序错乱。
- 区分 `FortranEOFError`（正常读到文件尾）与 `FortranFormattingError`（记录被截断/损坏）这两种异常的含义与触发场景。

## 2. 前置知识

在开始前，建议你先具备以下概念（不熟悉也没关系，下面会顺带解释）：

- **格式化（formatted）vs 无格式（unformatted）**：Fortran 的 `WRITE` 语句如果带格式串（如 `WRITE(1, '(I4)')`），输出的是人类可读的文本；如果 `OPEN` 时指定 `FORM='unformatted'` 且 `WRITE` 不带格式串，则直接把内存里的字节写到磁盘，这就是“无格式”。本讲只讨论后者。
- **顺序（sequential）vs 直接（direct）**：顺序访问像流水账，一条接一条读写；直接访问按记录号随机跳转。本讲只讨论顺序访问。
- **record（记录）**：Fortran 顺序文件的基本单位，对应一次 `WRITE(1) a, b, c` 调用写出的全部数据。
- **列主序（column-major / Fortran order）** 与 **行主序（row-major / C order）**：多维数组在内存中的存放顺序。这是本讲最容易踩坑的点，第 4.2 节会重点讲。
- **numpy dtype 的字节序**：如 `<i4` 表示小端 4 字节整型、`>f8` 表示大端 8 字节浮点。`scipy.io.wavfile` 那一讲（u2-l1）也涉及字节序，可以对照阅读。

承接前置讲义 u1-l3：你已经跑通过 `scipy.io.wavfile` 的 round-trip，理解“输入→处理→输出”闭环。本讲换一种格式，但思路一致——先理解二进制结构，再读源码，最后动手验证。

## 3. 本讲源码地图

本讲只围绕一个核心源文件展开：

| 文件 | 作用 |
| --- | --- |
| [scipy/io/_fortran.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_fortran.py) | `FortranFile` 类及两个异常类的全部实现，约 350 行纯 Python。 |

配套的支撑材料：

| 文件 | 作用 |
| --- | --- |
| [scipy/io/__init__.py:108](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/__init__.py#L108) | 把 `FortranFile`、`FortranEOFError`、`FortranFormattingError` 提升到 `scipy.io` 顶层命名空间（即为什么你能 `from scipy.io import FortranFile`）。 |
| [scipy/io/tests/test_fortran.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/tests/test_fortran.py) | 单元测试，含正常 round-trip 与各类损坏文件的用例，是理解“正确行为”的最佳参考。 |
| [scipy/io/tests/data/fortran-*.dat](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/tests/data) | 由真实 gfortran 生成的测试数据文件，文件名编码了 dtype 与维度（如 `fortran-si4-1x3x5.dat` = 小端 i4、1×3×5）。 |

注意：实现放在带下划线前缀的 `_fortran.py`（私有实现模块），这正是 u1-l2 讲过的 SciPy 公共/私有模块约定；`FortranFile` 通过顶层 re-export 暴露给用户。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1** record 结构：一条记录在磁盘上长什么样。
- **4.2** `FortranFile` 的读写：`write_record` / `read_record` 等方法如何对应到这种结构，以及多维数组的列序陷阱。
- **4.3** 异常体系：`FortranEOFError` 与 `FortranFormattingError` 如何区分“正常结束”与“损坏文件”。

### 4.1 record 结构：前后夹一个长度标记

#### 4.1.1 概念说明

Fortran 的无格式顺序文件不是把所有数据连续堆在一起，而是切成一条条 **record**，每条 record 对应一次 `WRITE` 调用。为了让读取方知道一条 record 有多长，gfortran、ifort 等编译器采用一个简单约定：**在数据前后各写一份长度标记**。

也就是说，磁盘上的一条 record 的字节布局是：

\[ \text{record} = \underbrace{L}_{\text{前长度标记}} \; \underbrace{b_1 b_2 \cdots b_L}_{\text{L 字节的纯数据}} \; \underbrace{L}_{\text{后长度标记}} \]

其中 \(L\) 是这条 record 的数据字节数。前后两个标记必须相等，读取方据此校验完整性。这个“前后夹一个长度”的机制在源码 docstring 里有明确说明。

> 小知识：这个设计是为了支持 Fortran 的 `BACKSPACE` 语句（回退一条记录）。前标记让你向前读，后标记让你向后退。`scipy.io.FortranFile` 只支持“前后都有标记”这种最常见的形式。

#### 4.1.2 核心流程

读取一条 record 时，解析器的动作是：

1. 读 4 字节（默认 `header_dtype=np.uint32`）作为 **前标记**，得到 \(L\)。
2. 向后读 \(L\) 字节纯数据。
3. 再读 4 字节作为 **后标记**，与前标记比对，不一致则报错。

写入一条 record 时反过来：

1. 先写 4 字节前标记 \(L\)。
2. 写 \(L\) 字节数据。
3. 再写 4 字节后标记 \(L\)。

#### 4.1.3 源码精读

我们直接看一份真实测试数据的字节布局来验证。文件 `fortran-si4-1x3x5.dat` 存的是一个 1×3×5、dtype 为小端 int32 的数组。用 `od` 把前若干字节按单字节无符号十进制展开：

```
0000000  60   0   0   0   0   0   0   0   5   0   0   0  10   0   0   0
0000016   1   0   0   0   6   0   0   0  11   0   0   0   2   0   0   0
0000032   7   0   0   0  12   0   0   0   3   0   0   0   8   0   0   0
0000048  13   0   0   0   4   0   0   0   9   0   0   0  14   0   0   0
0000064  60   0   0   0
```

逐段拆解：

- **字节 0–3**：`60 0 0 0`，按小端 uint32 读就是 \(60\)。这是前标记。
- **字节 4–63**：60 字节纯数据，每 4 字节一个 int32：`0, 5, 10, 1, 6, 11, 2, 7, 12, 3, 8, 13, 4, 9, 14`，共 15 个元素（1×3×5=15），对应 `np.arange(15)` 的列序排列。
- **字节 64–67**：`60 0 0 0`，又是 \(60\)，后标记。前后一致，校验通过。

数据字节数验证：\(15 \times 4 = 60\)，正好等于标记值。这份结构正是 `FortranFile` 假设的格式。

源码中描述这一点的位置在类 docstring：

> These files are broken up into records of unspecified types. The size of each record is given at the start ... and the data is written onto disk without any formatting. Fortran compilers supporting the BACKSPACE statement will write a second copy of the size to facilitate backwards seeking.
>
> ——[scipy/io/_fortran.py:46-L56](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_fortran.py#L46-L56)

#### 4.1.4 代码实践

实践目标：用肉眼 + Python 验证 record 结构。

1. 找到测试数据目录（沿用 u1-l3 学到的动态定位法）。
2. 用 `od` 或 Python 读取文件首尾 4 字节，确认前标记 == 后标记 == 数据段长度。

```python
# 示例代码：手动解析 record 结构（非项目原有代码）
import os
import numpy as np
import scipy.io

data_dir = os.path.join(os.path.dirname(scipy.io.__file__), "tests", "data")
fn = os.path.join(data_dir, "fortran-si4-1x3x5.dat")

with open(fn, "rb") as f:
    raw = f.read()

pre  = np.frombuffer(raw[:4],   dtype="<u4")[0]   # 前标记
post = np.frombuffer(raw[-4:],  dtype="<u4")[0]   # 后标记
payload = len(raw) - 8                              # 去掉前后标记的数据字节数

print("前标记 =", pre, " 后标记 =", post, " 数据字节 =", payload)
```

需要观察的现象与预期结果：

- `pre == post == 60`，且 `payload == 60`。
- 三者相等，说明这是一个完整的、前后标记一致的单条 record。
- 若你把 `dtype` 换成 `">u4"`（大端），会得到 `1610612736` 这种荒谬的大数——这正是大端文件被当小端读时的典型症状，提醒你字节序必须和生成文件的平台匹配。

> 待本地验证：上面的 `print` 输出依赖你机器上的 SciPy 安装路径，数值结论应如上所述。

#### 4.1.5 小练习与答案

**练习 1**：如果一个 record 的前标记是 60、后标记是 40，`FortranFile` 会怎样？
**答案**：在 `read_record` 末尾会抛 `ValueError('Sizes do not agree in the header and footer for this record - check header dtype')`（见 4.2.3 引用的 [L282-L285](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_fortran.py#L282-L285)）。注意这是一个普通 `ValueError`，不是 `FortranFormattingError`——它代表“标记自相矛盾”，通常意味着 `header_dtype` 选错了（比如把 8 字节标记的文件当 4 字节读）。

**练习 2**：为什么前后要写两份相同的长度标记？只写一份行不行？
**答案**：前标记供“向前读”用，后标记供 `BACKSPACE` 回退用。只写前标记也能顺序读，但无法高效回退；`scipy.io.FortranFile` 选择只支持“双标记”格式以保证两种方向都安全。

### 4.2 FortranFile 的写入与读取

#### 4.2.1 概念说明

`FortranFile` 把一个文件对象包装成“按 record 读写”的接口。它的公开方法很精简：

- `write_record(*items)`：写一条 record（可含多个数组）。
- `read_record(*dtypes, dtype=None)`：读一条 record，按指定 dtype 解析。
- `read_ints(dtype='i4')` / `read_reals(dtype='f8')`：分别默认按 int4 / real8 读，本质是 `read_record` 的快捷方式。
- `close()`，以及上下文管理器 `__enter__` / `__exit__`。

最关键的概念是 **列序**。Fortran 数组按列主序存内存，NumPy 默认按行主序（C order）存。同一段字节，用两种序去解释会得到转置关系的结果。所以多维数组的互操作必须显式处理顺序。

#### 4.2.2 核心流程

写入一条 record（`write_record`）：

1. 把每个 `item` 转成 `np.asarray`。
2. 累加所有 `item.nbytes` 得到总字节数 \(L\)。
3. 写前标记 \(L\) → 依次写每个数组的原始字节 → 写后标记 \(L\)。

读取一条 record（`read_record`）：

1. 读前标记得到 `first_size`。
2. 把传入的若干 dtype 的 `itemsize` 求和得 `block_size`。
3. 计算 `num_blocks = first_size // block_size`，若除不尽则报错（数据无法被 dtype 整除）。
4. 对每个 dtype 用 `np.fromfile` 读 `num_blocks` 个元素。
5. 读后标记 `second_size`，与 `first_size` 比对。

整除关系：

\[ \text{num\_blocks} = \left\lfloor \frac{\text{first\_size}}{\text{block\_size}} \right\rfloor, \qquad \text{remainder} = \text{first\_size} \bmod \text{block\_size} \]

要求 \(\text{remainder} = 0\)，否则记录尺寸与声明的 dtype 不匹配。

#### 4.2.3 源码精读

**写入**——`write_record` 把“前标记 + 数据 + 后标记”三步写得很直白：

```python
items = tuple(np.asarray(item) for item in items)
total_size = sum(item.nbytes for item in items)

nb = np.array([total_size], dtype=self._header_dtype)

nb.tofile(self._fp)          # 前标记
for item in items:
    item.tofile(self._fp)    # 各数组的原始字节
nb.tofile(self._fp)          # 后标记
```

完整代码见 [scipy/io/_fortran.py:160-L168](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_fortran.py#L160-L168)。注意 `item.tofile` 写的是 NumPy **内存布局的原始字节**，也就是 C order（行主序）。这正是 docstring 提醒“多维数组需要你自己转置”的原因：

> Note that data in multidimensional arrays is written in row-major order --- to make them read correctly by Fortran programs, you need to transpose the arrays yourself when writing them.
>
> ——[scipy/io/_fortran.py:153-L157](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_fortran.py#L153-L157)

**读取**——`read_record` 先读前标记，再做整除校验，再用 `np.fromfile` 读数，最后校验后标记：

```python
first_size = self._read_size(eof_ok=True)        # 前标记，允许 EOF
...
block_size = sum(dtype.itemsize for dtype in dtypes)
num_blocks, remainder = divmod(first_size, block_size)
if remainder != 0:
    raise ValueError(...)                          # 记录尺寸不能被 dtype 整除
...
for dtype in dtypes:
    r = np.fromfile(self._fp, dtype=dtype, count=num_blocks)
    if len(r) != num_blocks:
        raise FortranFormattingError("End of file in the middle of a record")
    ...
second_size = self._read_size()
if first_size != second_size:
    raise ValueError('Sizes do not agree in the header and footer ...')
```

核心片段见 [scipy/io/_fortran.py:251-L285](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_fortran.py#L251-L285)。两个值得留意的点：

- `np.fromfile(self._fp, ...)` 直接从底层文件对象读，读到的元素个数 `len(r)` 若小于 `num_blocks`，说明文件在 record 中途就结束了 → 抛 `FortranFormattingError`（4.3 节详述）。
- 多 dtype（混合 record，如 `read_record('<f4', '<i4')`）走的是另一条分支：要求 `first_size == block_size`，即用户必须给出每个变量的精确尺寸，因为“Fortran 不会把混合类型交错存放，无法猜测各数组大小”。见 [L261-L266](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_fortran.py#L261-L266)。

**快捷方法**——`read_ints` / `read_reals` 只是一行委托：

```python
def read_ints(self, dtype='i4'):
    return self.read_record(dtype)
```

见 [scipy/io/_fortran.py:293-L314](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_fortran.py#L293-L314) 与 [L316-L337](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_fortran.py#L316-L337)。默认 `'i4'` 对应 Fortran 的 `INTEGER*4`，`'f8'` 对应 `real*8`（双精度）。

#### 4.2.4 代码实践

实践目标：完成一个 int32 一维数组 + 二维 float64 数组的 round-trip，并正确处理列序。

```python
# 示例代码
import numpy as np
from scipy.io import FortranFile

# 1) 写两条 record
with FortranFile("demo.dat", "w") as f:
    f.write_record(np.array([1, 2, 3, 4, 5], dtype=np.int32))
    # 二维数组：写 a.T（转置），让磁盘字节呈 Fortran 列序
    a = np.linspace(0, 1, 20).reshape(5, 4)   # shape (5,4)，C order
    f.write_record(a.T)                        # 写 (4,5) 的 C-order 字节 == (5,4) 的列序字节

# 2) 关闭后重新打开读回
with FortranFile("demo.dat", "r") as f:
    ints = f.read_ints(np.int32)                       # 第 1 条 record
    flat = f.read_reals(np.float64)                    # 第 2 条 record，得到 20 个一维浮点
    mat  = flat.reshape((5, 4), order="F")             # 按 Fortran 列序还原成 (5,4)

print("ints =", ints)
print("mat 与原数组一致？", np.array_equal(mat, a))
```

操作步骤与预期结果：

1. 写入时对二维数组做了 `.T`，读回时用 `reshape(..., order="F")`——这两步是配套的，缺一不可。
2. 预期 `ints == [1 2 3 4 5]`，`np.array_equal(mat, a)` 为 `True`。
3. 若你只写 `f.write_record(a)`（不转置）却仍用 `order="F"` 读回，`mat` 会是 `a` 的转置——这就是最常见的列序踩坑。

> 待本地验证：运行后 `demo.dat` 大小应为 196 字节——int record 为 4+20+4=28 字节，float record 为 4+160+4=168 字节，合计 28+168=196（关键点是每条 record 的前后标记各占 4 字节）。可自行核对。

#### 4.2.5 小练习与答案

**练习 1**：为什么测试 `test_fortranfiles_write`（[test_fortran.py:59-L83](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/tests/test_fortran.py#L59-L83)）里写的是 `f.write_record(data.T)` 而不是 `data`？
**答案**：因为 `data` 是 C order 的 NumPy 数组，`.T` 后再按 C order 写出的字节，正好等于 `data` 按 Fortran 列序写出的字节。这样写出的文件才能被真正的 Fortran 程序按列序正确读回，也与 gfortran 生成的参考文件逐字节一致。

**练习 2**：`read_record('(2,3,5)f8')` 这种带形状的 dtype 字符串是怎么起作用的？
**答案**：NumPy 支持子数组 dtype，`(2,3,5)f8` 表示“每个元素本身是一个 2×3×5 的 float64 块”。`read_record` 读到的 `r.shape == (1,) + (2,3,5)`，当 `num_blocks == 1` 时会把最外层块维挤掉，直接得到 `(2,3,5)` 数组（见 [L274-L279](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_fortran.py#L274-L279)）。再 `.T` 一次就还原成原始 `(5,3,2)`。

### 4.3 异常体系：正常 EOF 与损坏文件的区分

#### 4.3.1 概念说明

`_fortran.py` 定义了两个语义不同的异常：

- **`FortranEOFError`**：文件在 record 边界上“干净地”结束了——也就是说，上一条 record 已读完，再尝试读下一条时读不到任何字节。这是**正常**情况，通常用 `try/except` 优雅结束读取循环。
- **`FortranFormattingError`**：文件在 record **内部**结束了——前标记声称有 \(L\) 字节数据，但还没读完就撞到 EOF，或者前标记本身就残缺。这是**异常**情况，说明文件被截断或损坏。

两者都同时继承 `TypeError` 和 `OSError`。继承 `OSError` 符合“文件 I/O 错误”的直觉；继承 `TypeError` 则是为了**向后兼容**——旧版本代码直接抛 `TypeError`，有些用户写了 `except TypeError:`，保留这条继承链不会让他们的代码漏接异常。

#### 4.3.2 核心流程

读取时区分这两种情况的“判官”是私有方法 `_read_size`：

```text
_read_size(eof_ok=False):
    读 n = header_dtype.itemsize 字节
    若 读到空 且 eof_ok=True   -> FortranEOFError   （干净的 EOF）
    若 读到不足 n 字节          -> FortranFormattingError （标记本身残缺）
    否则 返回该长度
```

注意只有 `read_record` 第一次读前标记时传 `eof_ok=True`（允许在 record 之间遇到 EOF）；读后标记、以及 `read_record` 内部读数据时都不允许 EOF，撞到就抛 `FortranFormattingError`。

#### 4.3.3 源码精读

异常类定义非常简短：

```python
class FortranEOFError(TypeError, OSError):
    """Indicates that the file ended properly."""
    ...

class FortranFormattingError(TypeError, OSError):
    """Indicates that the file ended mid-record."""
    ...
```

见 [scipy/io/_fortran.py:13-L21](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_fortran.py#L13-L21) 与 [L24-L30](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_fortran.py#L24-L30)。docstring 一句话点破区别：`ended properly` vs `ended mid-record`。

判别逻辑在 `_read_size`：

```python
def _read_size(self, eof_ok=False):
    n = self._header_dtype.itemsize
    b = self._fp.read(n)
    if (not b) and eof_ok:
        raise FortranEOFError("End of file occurred at end of record")
    elif len(b) < n:
        raise FortranFormattingError(
            "End of file in the middle of the record size")
    return int(np.frombuffer(b, dtype=self._header_dtype, count=1)[0])
```

见 [scipy/io/_fortran.py:127-L135](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_fortran.py#L127-L135)。另外，在 `read_record` 用 `np.fromfile` 读数据时，若实际读到的元素不足，也会抛 `FortranFormattingError("End of file in the middle of a record")`（[L271-L273](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_fortran.py#L271-L273)）——这是“前标记完好、但数据段被截断”的情形。

测试用例把这两种情况都覆盖了：

- `test_fortran_eof_ok`（[test_fortran.py:264-L276](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/tests/test_fortran.py#L264-L276)）：写两条 record，读三条，第三次读到 `FortranEOFError`。
- `test_fortran_eof_broken_record`（[test_fortran.py:311-L324](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/tests/test_fortran.py#L311-L324)）：写两条 record 后用 `f.truncate(size-20)` 截掉末尾 20 字节，第二条 record 数据段读不完 → `FortranFormattingError`。

#### 4.3.4 代码实践

实践目标：分别触发 `FortranEOFError` 与 `FortranFormattingError`，体会两者区别。

```python
# 示例代码
import numpy as np
from scipy.io import FortranFile, FortranEOFError, FortranFormattingError

# 先造一个正常的两条-record 文件
with FortranFile("err.dat", "w") as f:
    f.write_record(np.array([1, 2, 3, 4, 5], dtype=np.int32))
    f.write_record(np.linspace(0, 1, 30, dtype=np.float64))

# 情形 A：正常读完后再读 -> FortranEOFError
with FortranFile("err.dat", "r") as f:
    f.read_ints(np.int32)
    f.read_reals(np.float64)
    try:
        f.read_reals(np.float64)            # 没有第三条 record
    except FortranEOFError as e:
        print("情形A 正常EOF:", type(e).__name__, "-", e)

# 情形 B：截断第二条 record 的数据段 -> FortranFormattingError
import os
sz = os.path.getsize("err.dat")
with open("err.dat", "r+b") as raw:
    raw.truncate(sz - 20)                    # 砍掉末尾 20 字节

with FortranFile("err.dat", "r") as f:
    f.read_ints(np.int32)
    try:
        f.read_reals(np.float64)            # 第二条数据段读不完
    except FortranFormattingError as e:
        print("情形B 损坏文件:", type(e).__name__, "-", e)
```

需要观察的现象与预期结果：

- 情形 A 打印 `FortranEOFError`，对应“文件正常结束、没有更多 record”。
- 情形 B 打印 `FortranFormattingError`，对应“record 中途被截断”。
- 关键区别：A 是预期的循环终止条件，B 是真正的数据损坏。所以实践中常见写法是 `while True: try: f.read_record(...) except FortranEOFError: break`，而 `FortranFormattingError` 应当让它向上传播或显式报告。

> 待本地验证：截断字节数（这里是 20）只要大于 0 且小于一条 record 的数据长度，就能稳定触发 `FortranFormattingError`；若截断恰好落在 record 边界上，则可能改触发 `FortranEOFError`，可自行实验。

#### 4.3.5 小练习与答案

**练习 1**：为什么这两个异常要继承 `TypeError`？
**答案**：纯为向后兼容。历史上 `FortranFile` 用 `TypeError` 表示“文件结束”，部分用户代码用 `except TypeError:` 捕获。即使新版本引入了更精确的异常类，保留 `TypeError` 父类能避免那些老代码漏接异常。docstring 里写得很明白：`Descends from TypeError for backward compatibility.`（[L27-L29](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_fortran.py#L27-L29)）。

**练习 2**：如果一个文件的前标记读出来是 `0xff`（255）这样的“垃圾值”，会触发哪个异常？
**答案**：只要 4 字节标记本身能完整读出（`len(b) == n`），`_read_size` 不会报错，而是返回这个垃圾长度。接着 `read_record` 会尝试用 `np.fromfile` 读那么多元素，结果要么元素不足触发 `FortranFormattingError`（"End of file in the middle of a record"），要么整除校验失败触发 `ValueError`。测试 `test_fortran_bogus_size`（[test_fortran.py:296-L308](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/tests/test_fortran.py#L296-L308)）验证的就是这种情形，预期正是 `FortranFormattingError`。

## 5. 综合实践

设计一个小任务，把 record 结构、读写、列序、异常处理串起来：**用 Python 生成一个文件，假装它是某个 Fortran 程序的输出，再用 `FortranFile` 读回，最后破坏它观察异常**。

任务背景：假设一个 Fortran 程序在每一步迭代中写出“步号（int32）+ 当前残差（float64）+ 一个 3×3 的状态矩阵（float64，Fortran 列序）”。你要在 Python 侧复现这种输出格式并正确读回。

```python
# 示例代码
import os
import numpy as np
from scipy.io import FortranFile, FortranEOFError, FortranFormattingError

step   = np.array([7], dtype=np.int32)
resid  = np.array([1.25e-3], dtype=np.float64)
state  = np.arange(9, dtype=np.float64).reshape(3, 3)   # 3x3 状态矩阵

# (1) 写：每个 WRITE 对应一条 record；多维数组记得 .T
with FortranFile("sim.dat", "w") as f:
    f.write_record(step, resid)      # 一条混合 record：int + double
    f.write_record(state.T)          # 一条纯数组 record（列序字节）

# (2) 读：混合 record 给两个 dtype；数组 record 读回后按列序 reshape
with FortranFile("sim.dat", "r") as f:
    rec = f.read_record("<i4", "<f8")          # -> (step_array, resid_array)
    s = rec[0][0]
    r = rec[1][0]
    mat = f.read_reals(np.float64).reshape((3, 3), order="F")

    print(f"step={s}, resid={r}")
    print("state 还原正确？", np.array_equal(mat, state))

    try:
        f.read_record()                         # 已无更多 record
    except FortranEOFError as e:
        print("正常结束:", e)

# (3) 破坏：截掉末尾，制造 FortranFormattingError
sz = os.path.getsize("sim.dat")
with open("sim.dat", "r+b") as raw:
    raw.truncate(sz - 12)

with FortranFile("sim.dat", "r") as f:
    f.read_record("<i4", "<f8")
    try:
        f.read_reals(np.float64)                # 第二条 record 数据不全
    except FortranFormattingError as e:
        print("检测到损坏:", e)
```

验收标准：

1. 第 (2) 步打印 `step=7, resid=0.00125`，`state 还原正确？ True`，并打印 `正常结束: ...`（`FortranEOFError`）。
2. 第 (3) 步打印 `检测到损坏: ...`（`FortranFormattingError`）。
3. 能解释清楚：为什么写 `state.T`、读 `reshape((3,3), order="F")` 是配套的；为什么 `read_record("<i4", "<f8")` 必须两个 dtype 都给、且 `first_size` 必须正好等于 12。

## 6. 本讲小结

- Fortran 无格式顺序文件由若干 **record** 串接，每条 record 是「前长度标记 + 纯数据 + 后长度标记」三段，前后标记必须相等。
- `FortranFile` 用 `header_dtype`（默认 `np.uint32`，4 字节）解释长度标记；标记的字节数与字节序必须与生成文件的平台一致。
- `write_record` 写出的是 NumPy 的 C order（行主序）原始字节；多维数组与 Fortran 互操作时，写要 `.T`、读要 `reshape(..., order="F")` 或 `.T`，这一对操作是配套的。
- `read_record` 用「前标记 ÷ dtype 总尺寸」做整除校验，多 dtype 混合 record 必须精确给出每个变量尺寸；`read_ints` / `read_reals` 是它的快捷封装。
- `FortranEOFError` = 文件在 record 边界干净结束（正常，用于终止读取循环）；`FortranFormattingError` = record 内部被截断或标记残缺（损坏）。两者都继承 `TypeError` 与 `OSError`，继承 `TypeError` 仅为向后兼容。

## 7. 下一步学习建议

本讲你已经掌握了一种“带长度标记的二进制 record”格式。接下来建议：

- **横向对比 WAV**：回到 u2-l1，对比 `wavfile.py` 的 RIFF chunk 结构。你会发现 chunk 也是「ID + 长度 + 数据」的变体，但没有“后长度标记”——体会不同格式在“如何界定数据边界”上的取舍。
- **继续本单元**：下一讲 u2-l3 将进入 Matrix Market 文本格式（`_mmio.py`），从二进制切到文本解析，对比两种 I/O 风格。
- **深入异常设计**：如果你对“异常继承链与向后兼容”感兴趣，可以提前看 u4-l4（健壮性与错误处理），那里会横切对比 scipy.io 各模块的异常体系。
- **阅读源码建议**：再通读一遍 [_fortran.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_fortran.py)，重点对照 [test_fortran.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/tests/test_fortran.py) 里每个用例对应 `read_record` / `_read_size` 的哪一条分支——这是把“源码—行为—测试”三者对齐最有效的方式。
