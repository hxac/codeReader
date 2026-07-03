# NetCDF3 文件模型与读写（_netcdf.py）

## 1. 本讲目标

本讲深入 `scipy.io._netcdf`（对外暴露为 `scipy.io.netcdf_file` / `scipy.io.netcdf_variable`），这是 scipy.io 提供的 NetCDF3（Classic / 64-bit offset）纯 Python 读写器。学完本讲你应当能够：

- 说清 NetCDF3 文件「自描述二进制」的本质：魔数、header 区、data 区如何组织，以及 dimensions / variables / attributes 三段式数据模型。
- 看懂 `TYPEMAP` / `FILLMAP` / `REVERSE` 三张映射表如何把 NetCDF 类型码、numpy dtype、填充值串起来。
- 掌握 `netcdf_file` 的 `createDimension` / `createVariable` 与读写主流程，理解记录维度（record dimension）的特殊地位。
- 掌握 `netcdf_variable` 的 `isrec` 判定、`__getitem__` / `__setitem__` 的 mask-and-scale 机制。
- 理解 `mmap=True` 默认读取模式为何会让「文件生命周期」与「数组生命周期」绑死，以及 `close()` 为何要发警告。

## 2. 前置知识

- **二进制文件与字节序**：NetCDF3 规定所有多字节整数都按**大端序（big-endian）**存储。numpy 里 `>i` 表示大端 4 字节整数、`>q` 表示大端 8 字节整数。本讲会反复出现这种 dtype 字符串。
- **维度（dimension）与变量（variable）**：你可以把 NetCDF 想象成一个「带元数据的多维数组仓库」。维度是坐标轴的长度（如 `latitude` 长度 73），变量是挂在若干维度上的数组（如温度变量挂在 `(time, latitude, longitude)` 上）。
- **记录维度（record dimension）**：一种「可无限增长」的特殊维度，通常是时间。后面会看到它在文件布局里有独立的「记录区」。
- **结构化 dtype（structured dtype）**：numpy 允许定义类似 C 结构体的 dtype，如 `np.dtype({'names':['a','b'], 'formats':['<i4','<f8']})`。`_netcdf` 用它把多个记录变量并排打包成一个 rec array。
- 前置讲义 **u2-l2**（FortranFile）介绍了「二进制 record + 长度标记 + 字节序」的基本套路，本讲是它的进阶：NetCDF3 不只是「一串记录」，而是「带目录的二进制数据库」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [_netcdf.py](_netcdf.py) | 全部实现都在这一个文件里：常量表、`netcdf_file`、`netcdf_variable` |
| [tests/test_netcdf.py](tests/test_netcdf.py) | 配套测试，含 `make_simple` 工厂、`test_maskandscale`、`test_mmaps_segfault` 等 |
| [tests/data/example_1.nc](tests/data/example_1.nc) | 经典格式样本，含 `time`、`lat` 变量，用于 mmap/segfault 回归测试 |
| [tests/data/example_2.nc](tests/data/example_2.nc) | 带 `scale_factor` / `add_offset` / `missing_value` 的 `Temperature` 变量 |
| [tests/data/example_3_maskedvals.nc](tests/data/example_3_maskedvals.nc) | 专门测 `_FillValue` / `missing_value` / NaN / char / 2-D 等各种掩码场景 |

> 说明：`scipy.io.netcdf_file` 是顶层 re-export（见 u1-l1），真正的实现在带下划线的 `_netcdf.py`。文件末尾 [_netcdf.py:1102-L1103](_netcdf.py#L1102-L1103) 还保留了旧别名 `NetCDFFile = netcdf_file` / `NetCDFVariable = netcdf_variable`，供老代码兼容。

## 4. 核心概念与源码讲解

### 4.1 NetCDF3 数据模型与二进制文件布局

#### 4.1.1 概念说明

NetCDF（Network Common Data Form）是地学/气象领域常用的「自描述（self-describing）二进制数据格式」。所谓自描述，是指文件内部除了原始数值，还带着一份「目录」：有哪些维度、每个维度多长、有哪些变量、每个变量挂哪些维度、是什么类型、数据从文件第几个字节开始。这样读取程序不必预先知道文件结构，读一遍头就能定位任意变量。

scipy 的 `_netcdf` 只支持 **NetCDF3**（Classic 与 64-bit offset 两种），**不支持 NetCDF4**（那需要 HDF5 后端，官方推荐用 `netcdf4-python`）。

NetCDF3 的数据模型有三段：

1. **dimensions（维度）**：名字 → 长度。最多有一个维度是「unlimited」（记录维度），长度记为 `None`。
2. **variables（变量）**：名字 → 一个多维数组，附带它使用的维度名列表、数据类型、若干属性，以及数据在文件中的偏移 `begin` 和占用字节数 `vsize`。
3. **attributes（属性）**：键值对元数据，可挂在文件全局（global attributes），也可挂在单个变量上（如 `units`、`_FillValue`）。

#### 4.1.2 核心流程

NetCDF3 文件在磁盘上是 `header 区 + data 区` 的布局：

```text
┌────────────────────────── 文件 ──────────────────────────┐
│ magic: 'CDF' (3 字节) + version_byte (1 字节, 1 或 2)     │
│ numrecs: 记录维度的当前记录数 (大端 int32/int64)           │
│ dim_array:   维度列表  (空则写 ABSENT = 8 个 0 字节)       │
│ gatt_array:  全局属性列表(空则写 ABSENT)                    │
│ var_array:   变量列表   (空则写 ABSENT)                     │
│   └─ 每个变量: 名字/维度id列表/属性/nc_type/vsize/begin    │
│ ─────────────── 以上为 header（目录） ─────────────────── │
│ data 区: 非记录变量数据，随后是记录区（各记录变量并排）     │
└────────────────────────────────────────────────────────────┘
```

关键约定：

- 每个数都按**大端序**写。
- 列表段用一个 4 字节「标签」开头：维度段是 `NC_DIMENSION = 0x0000000a`、属性段是 `NC_ATTRIBUTE = 0x0000000c`、变量段是 `NC_VARIABLE = 0x0000000b`；若该段为空，则写 `ABSENT = 8 个 0 字节`。
- 字符串以「4 字节长度 + 内容 + 补齐到 4 字节倍数的 `\x00`」打包。
- `vsize`：非记录变量为「元素数 × 元素字节数，向上取整到 4 的倍数」；记录变量为「单条记录占用的字节数」。记录区总大小 `recsize` = 所有记录变量 `vsize` 之和，乘以记录数 `numrecs`。

记录区的「每个记录」里，多个记录变量是**并排**存放的，所以读取时要把它们组装成一个结构化数组（rec array）。

#### 4.1.3 源码精读

读取入口 `_read` 先校验魔数与版本字节，再依次读四段：

[_netcdf.py:598-L609](_netcdf.py#L598-L609) — 校验 `b'CDF'` 魔数、读 1 字节版本号，然后顺序读 `numrecs / dim_array / gatt_array / var_array`：

```python
magic = self.fp.read(3)
if not magic == b'CDF':
    raise TypeError(f"Error: {self.filename} is not a valid NetCDF 3 file")
self.__dict__['version_byte'] = frombuffer(self.fp.read(1), '>b')[0]
self._read_numrecs()
self._read_dim_array()
self._read_gatt_array()
self._read_var_array()
```

`_read_dim_array` 解析维度段，注意记录维度长度为 `None`：

[_netcdf.py:614-L624](_netcdf.py#L614-L624) — 读标签校验后循环读每个维度的「名字 + 长度」，长度为 0 时记作 `None`（即记录维度）：

```python
length = self._unpack_int() or None  # None for record dimension
self.dimensions[name] = length
self._dims.append(name)  # preserve order
```

空段用 `ABSENT` 表示，对应的标签常量定义在文件顶部：

[_netcdf.py:49-L65](_netcdf.py#L49-L65) — `ABSENT`（8 个 0，表示「空列表」）、`ZERO`（4 个 0）、`NC_DIMENSION/NC_ATTRIBUTE/NC_VARIABLE` 三个段标签，以及各类型的默认填充值 `FILL_*`。

#### 4.1.4 代码实践

**实践目标**：亲手验证 NetCDF3 文件确实是「魔数 + 版本 + 大端整数」的二进制，而不是黑盒。

**操作步骤**：

```python
# parse_header.py —— 示例代码（非项目原有）
import struct
from scipy.io import netcdf_file
import scipy.io as sio, os

datadir = os.path.join(os.path.dirname(sio.__file__), 'tests', 'data')
path = os.path.join(datadir, 'example_1.nc')

with open(path, 'rb') as f:
    head = f.read(8)
magic = head[:3]                       # 应为 b'CDF'
version = struct.unpack('>b', head[3:4])[0]   # 应为 1 (Classic)
numrecs = struct.unpack('>i', head[4:8])[0]   # 记录数
print('magic=', magic, 'version=', version, 'numrecs=', numrecs)

# 用官方 reader 对照
with netcdf_file(path, mmap=False) as nc:
    print('version_byte=', nc.version_byte, 'dims=', dict(nc.dimensions))
```

**需要观察的现象**：`magic` 打印出 `b'CDF'`，`version` 为 `1`；官方 reader 的 `nc.version_byte` 与你手解的 `version` 完全一致。

**预期结果**：手动解析的魔数/版本号与 `netcdf_file` 读出的 `version_byte` 相同。具体的 `numrecs` 与各维度长度取决于该样本文件内容，若不确定可标注**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_read` 在魔数不匹配时抛 `TypeError` 而不是 `ValueError`？

**答案**：这是为了和 `numpy` 的「不是我能读的格式」语义对齐——`TypeError` 表示「这个对象/文件根本不是 NetCDF3 类型」，让上层（如尝试多种格式的代码）能按异常类型分流。

**练习 2**：`version_byte` 等于 1 和 2 分别对应什么？它在后续解析里影响哪一步？

**答案**：1 = Classic 格式，2 = 64-bit offset 格式。它影响变量数据偏移 `begin` 的读取宽度：见 [_netcdf.py:747](_netcdf.py#L747) 的 `begin = [self._unpack_int, self._unpack_int64][self.version_byte-1]()`——v1 读 4 字节 int32，v2 读 8 字节 int64，从而支持大于 2 GiB 的文件。

---

### 4.2 类型映射常量：TYPEMAP / FILLMAP / REVERSE

#### 4.2.1 概念说明

NetCDF3 只支持 6 种外部类型（`byte/char/short/int/float/double`），每种在文件里用一个 4 字节大端类型码表示（如 `NC_INT = b'\x00\x00\x00\x04'`）。`_netcdf` 用三张表把「NetCDF 类型码 ↔ numpy dtype ↔ 默认填充值」打通：

- `TYPEMAP`：NetCDF 类型码 → `(numpy typecode, itemsize)`，用于**读取**时把磁盘类型翻译成 numpy。
- `REVERSE`：`(numpy typecode, itemsize)` → NetCDF 类型码，用于**写入**时把 numpy dtype 翻译回磁盘类型。
- `FILLMAP`：NetCDF 类型码 → 该类型的默认填充字节，用于给未写满的记录补填充值。

#### 4.2.2 核心流程

读取流程：磁盘 4 字节 → 查 `TYPEMAP` 得 `(typecode, size)` → 拼 `f'>{typecode}'` 大端 dtype → `frombuffer` 解析。

写入流程：用户给的 numpy dtype → 取 `(char, itemsize)` → 查 `REVERSE` 得类型码写入磁盘；若 `(char, itemsize)` 不在 `REVERSE` 里（如 `int64`/`uint64`），直接报错——NetCDF3 不支持。

#### 4.2.3 源码精读

[_netcdf.py:67-L92](_netcdf.py#L67-L92) — 三张映射表的定义：

```python
TYPEMAP = {NC_BYTE: ('b', 1), NC_CHAR: ('c', 1), NC_SHORT: ('h', 2),
           NC_INT: ('i', 4), NC_FLOAT: ('f', 4), NC_DOUBLE: ('d', 8)}

FILLMAP = {NC_BYTE: FILL_BYTE, NC_CHAR: FILL_CHAR, ...}

REVERSE = {('b', 1): NC_BYTE, ('B', 1): NC_CHAR, ('c', 1): NC_CHAR,
           ('h', 2): NC_SHORT, ('i', 4): NC_INT, ('f', 4): NC_FLOAT,
           ('d', 8): NC_DOUBLE,
           # 来自 asarray(1).dtype.char 和 asarray('foo').dtype.char
           ('l', 4): NC_INT, ('S', 1): NC_CHAR}
```

注意 `REVERSE` 里多出来的两条：

- `('l', 4): NC_INT` —— 在某些平台上 `np.array(1).dtype.char` 是 `'l'`（long），但它和 `'i'` 一样是 4 字节整数，所以也映射到 `NC_INT`。
- `('S', 1): NC_CHAR` —— Python 字符串转 numpy 后是 `'S'` 类型（bytes），映射成 `NC_CHAR`，这样全局字符串属性才能被当作 char 写出。

`createVariable` 正是用 `REVERSE` 做「合法性校验」：

[_netcdf.py:383-L389](_netcdf.py#L383-L389) — 把用户 dtype 转成 `(typecode, size)`，查不到就抛 `ValueError`，查到则强制转大端序：

```python
typecode, size = type.char, type.itemsize
if (typecode, size) not in REVERSE:
    raise ValueError(f"NetCDF 3 does not support type {type}")
data = empty(shape_, dtype=type.newbyteorder("B"))   # 'B' = big-endian
```

#### 4.2.4 代码实践

**实践目标**：亲手验证「不支持的类型会被拒」，并理解 `int64` 为何不行。

**操作步骤**：

```python
# check_types.py —— 示例代码（非项目原有）
from io import BytesIO
from scipy.io import netcdf_file
import numpy as np
from scipy.io._netcdf import REVERSE, TYPEMAP

# 查表：numpy int32 / float64 各对应哪个 nc 类型码？
print('int32  ->', REVERSE[('i', 4)])     # b'\x00\x00\x00\x04' = NC_INT
print('float64 ->', REVERSE[('d', 8)])    # b'\x00\x00\x00\x06' = NC_DOUBLE

# NetCDF3 不支持 int64 / uint64
with netcdf_file(BytesIO(), 'w') as f:
    f.createDimension('time', 4)
    try:
        f.createVariable('bad', 'int64', ('time',))
    except ValueError as e:
        print('rejected:', e)
```

**需要观察的现象**：查表打印出 `NC_INT` / `NC_DOUBLE` 的 4 字节码；尝试创建 `int64` 变量时抛出 `ValueError: NetCDF 3 does not support type ...`。

**预期结果**：与 `tests/test_netcdf.py` 的 `test_write_invalid_dtype`（[tests/test_netcdf.py:289-L301](tests/test_netcdf.py#L289-L301)）断言一致——`int64`/`uint64` 必须被拒。

#### 4.2.5 小练习与答案

**练习 1**：`REVERSE` 里为什么同时有 `('c',1)` 和 `('S',1)` 都映射到 `NC_CHAR`？

**答案**：`'c'` 是 numpy 的单字符类型（老式），`'S'` 是 bytes 字符串类型。两者在 NetCDF3 里都没有独立类型，统一用 `NC_CHAR` 表示，这样无论用户传 `dtype='c'` 还是 Python 字符串，都能正确写出。

**练习 2**：读取时若磁盘上的类型码不在 `TYPEMAP` 里会发生什么？

**答案**：`TYPEMAP[nc_type]` 会抛 `KeyError`。由于 NetCDF3 规范只定义这 6 种类型，遇到未知码通常意味着文件损坏或其实是 NetCDF4 文件被误当 v3 读。

---

### 4.3 netcdf_file：文件对象与读写主流程

#### 4.3.1 概念说明

`netcdf_file` 是用户接触 NetCDF3 文件的入口对象。它持有：

- `dimensions`（名字 → 长度的 dict）、`variables`（名字 → `netcdf_variable` 的 dict）。
- 全局属性（直接作为对象属性访问，如 `f.history = '...'`）。
- 底层文件指针 `self.fp`、可选的 mmap 缓冲 `self._mm_buf`。
- 写出时需要的内部记账：`_dims`（保持维度顺序）、`_recs`（记录数）、`_recsize`（记录区每条记录大小）。

它提供 `createDimension` / `createVariable` / `flush`（= `sync`）/ `close`，并支持 `with` 上下文和「append 模式」。

#### 4.3.2 核心流程

**写入主流程**（`flush` → `_write`）：

```text
flush() 若 mode in 'wa':
  seek(0)
  写 'CDF' + version_byte
  _write_numrecs()   # 扫描所有记录变量，取最大记录数作为 numrecs
  _write_dim_array()
  _write_gatt_array()
  _write_var_array():
      两遍扫描变量：
        第 1 遍 _write_var_metadata: 写名字/维度/属性/nc_type/vsize，
                  begin 先写占位 0，记录 _begin 位置
        第 2 遍 _write_var_data:   写真正数据前，seek 回 _begin 把真实偏移回填
```

这个「先写占位 `begin`、写完数据再回填」是二进制格式里常见的两遍写法，因为写 metadata 时还不知道数据区会落在哪个偏移。

**读取主流程**（`__init__` → `_read`）已在 4.1 讲过；`_read_var_array` 还要处理「记录变量并排」的组装（见 4.3.3）。

#### 4.3.3 源码精读

`createDimension` 限制「只有第一个维度可以是无限维度」：

[_netcdf.py:343-L347](_netcdf.py#L343-L347)：

```python
if length is None and self._dims:
    raise ValueError("Only first dimension may be unlimited!")
self.dimensions[name] = length
self._dims.append(name)
```

`_write_var_array` 把变量排序「非记录变量在前、记录变量在后」：

[_netcdf.py:455-L461](_netcdf.py#L455-L461) — 记录变量的 sortkey 是 `(-1,)`（确保排到最后），非记录变量按其 shape 排：

```python
def sortkey(n):
    v = self.variables[n]
    if v.isrec:
        return (-1,)
    return v._shape
variables = sorted(self.variables, key=sortkey, reverse=True)
```

随后 [_netcdf.py:463-L473](_netcdf.py#L463-L473) 先写所有变量 metadata，再用记录变量的 `vsize` 之和算出 `_recsize`，最后才写数据。

`_write_var_metadata` 计算 `vsize`（非记录变量向上取整到 4 字节；记录变量取单条记录大小），并用「占位 begin」记下需要回填的位置：

[_netcdf.py:491-L508](_netcdf.py#L491-L508)：

```python
if not var.isrec:
    vsize = var.data.size * var.data.itemsize
    vsize += -vsize % 4                # 向上取整到 4 的倍数
else:                                  # 记录变量
    vsize = var.data[0].size * var.data.itemsize
    ...
self.variables[name].__dict__['_vsize'] = vsize
self._pack_int(vsize)
# 占位 begin，稍后回填真实偏移
self.variables[name].__dict__['_begin'] = self.fp.tell()
self._pack_begin(0)
```

`_write_var_data` 在写真实数据前，先 `seek` 回 `_begin` 把占位的 0 替换成真实偏移 `the_beguine`：

[_netcdf.py:510-L521](_netcdf.py#L510-L521)：

```python
the_beguine = self.fp.tell()
self.fp.seek(var._begin)
self._pack_begin(the_beguine)      # 回填真实偏移
self.fp.seek(the_beguine)
if not var.isrec:
    self.fp.write(var.data.tobytes())
```

读取侧 `_read_var_array` 区分「记录变量」与「非记录变量」：非记录变量直接按 `begin/vsize` 切片读取；记录变量则把它们的 dtype 收集进一个结构化 dtype，最后用一次 `frombuffer` 读出整段记录区再拆给各变量：

[_netcdf.py:668-L700](_netcdf.py#L668-L700) — 记录变量累加 `_recsize`、收集到 `dtypes`；非记录变量按 mmap 或普通读法取数据：

```python
if shape and shape[0] is None:  # record variable
    rec_vars.append(name)
    self.__dict__['_recsize'] += vsize
    ...
    dtypes['names'].append(name)
    dtypes['formats'].append(str(shape[1:]) + dtype_)
else:  # not a record variable
    a_size = reduce(mul, shape, 1) * size
    ...
```

当存在多个记录变量、且某变量类型为 `byte/char/short` 时，由于记录需要补齐到 4 字节边界，代码会插入一个虚拟字段 `_padding_{var}` 来吃掉对齐字节：

[_netcdf.py:679-L684](_netcdf.py#L679-L684)：

```python
if typecode in 'bch':
    actual_size = reduce(mul, (1,) + shape[1:]) * size
    padding = -actual_size % 4
    if padding:
        dtypes['names'].append(f'_padding_{var}')
        dtypes['formats'].append(f'({padding},)>b')
```

#### 4.3.4 代码实践

**实践目标**：完整走一遍「创建维度 → 创建变量 → 写值 → 关闭 → 读回」的 round-trip，这是本讲的核心动手任务。

**操作步骤**：

```python
# roundtrip_nc.py —— 示例代码（非项目原有）
import numpy as np
from scipy.io import netcdf_file

# 1) 写
with netcdf_file('demo.nc', 'w') as f:
    f.history = 'Created for u2-l5 practice'
    f.createDimension('latitude', 5)
    temp = f.createVariable('temperature', 'f', ('latitude',))
    temp.units = 'degC'
    temp[:] = np.array([10.0, 12.5, 15.0, 17.5, 20.0], dtype='>f4')

# 2) 读（mmap=True 是文件名的默认）
with netcdf_file('demo.nc', 'r', mmap=True) as f:
    print('history  =', f.history)
    print('dims     =', dict(f.dimensions))
    print('vars     =', list(f.variables.keys()))
    t = f.variables['temperature']
    print('units    =', t.units)
    print('shape    =', t.shape)
    print('data     =', t[:].copy())   # copy 以便关文件后还能用
```

**需要观察的现象**：写出后 `demo.nc` 是二进制；读回时 `history`、`temperature.units` 都是 `bytes`（如 `b'degC'`），`shape == (5,)`，数据与写入值一致。

**预期结果**：`t[:]` 打印出 `[10. 12.5 15. 17.5 20.]`（大端 float32 解码回正常值）。注意 `createVariable('temperature', 'f', ...)` 里的 `'f'` 会经 `REVERSE` 映射到 `NC_FLOAT`，写出的数据是大端序。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_write_var_data` 要分两步「先 seek 到 `_begin` 回填、再 seek 回来写数据」？

**答案**：写变量 metadata 时数据区还没开始写，无法知道真实偏移；只能先写占位 0 并记下位置，等轮到写该变量数据时，当前位置就是真实偏移，再 `seek` 回去把占位 0 覆盖成真实值。这是二进制格式「header 先行、data 在后」的必然结果。

**练习 2**：把一个变量定义在「无限维度」上，和定义在固定长度维度上，写出时 `vsize` 含义有何不同？

**答案**：固定维度的 `vsize` 是整个数组字节数（向上取整到 4）；无限（记录）维度的 `vsize` 是**单条记录**的字节数，文件总记录区大小 = `numrecs × Σvsize(所有记录变量)`。

---

### 4.4 netcdf_variable：变量对象、记录维度与 mask-and-scale

#### 4.4.1 概念说明

`netcdf_variable` 是「住在文件里的数组」的包装。它持有底层 `data`（一个 numpy 数组，或 mmap 上的 view）、类型码、形状、维度名列表、以及用户属性。它实现了 `__getitem__` / `__setitem__`，所以可以像 numpy 数组一样用 `t[:]` 读写。

两个只读 property 值得注意：

- `shape`：返回 `self.data.shape`（只读，不能改）。
- `isrec`：判断该变量是否「记录变量」（第一维是无限维度）。

`maskandscale=True` 时，读写会自动应用 `_FillValue` / `missing_value` 掩码与 `scale_factor` / `add_offset` 线性变换——这是气象数据里极常见的「把整型存储的物理量还原成真实值」的约定。

#### 4.4.2 核心流程

**`isrec` 判定**：`bool(self.data.shape) and not self._shape[0]`——有形状且第一维长度为 0/None 才算记录变量。

**读取（`__getitem__`，maskandscale=True）**：

\[ \text{physical} = (\text{raw} \times \text{scale\_factor}) + \text{add\_offset} \]

并把等于 `missing_value` / `_FillValue` 的位置掩码成 `numpy.ma` 缺失。

**写入（`__setitem__`，maskandscale=True）**：做反变换

\[ \text{raw} = (\text{physical} - \text{add\_offset}) / \text{scale\_factor} \]

把缺失值填成 `_FillValue`，非浮点类型还要四舍五入。

**记录变量自动扩容**：给记录变量赋值时，若索引超出当前记录数，`__setitem__` 会自动 `resize` 底层数组，这就是「时间维度可无限增长」的实现。

#### 4.4.3 源码精读

`isrec` 的定义非常精炼：

[_netcdf.py:887-L897](_netcdf.py#L887-L897)：

```python
@property
def isrec(self):
    return bool(self.data.shape) and not self._shape[0]
```

> 注意 `_shape` 是 createVariable 时传入的「逻辑形状」（记录维度处为 `None`），而 `self.data.shape` 是底层数组的实际形状。`tests/test_netcdf.py:379` 的 `test_zero_dimensional_var` 专门断言 `v.isrec is False`（零维变量不是记录变量）。

`__getitem__` 在 maskandscale 关闭时直接返回底层切片；开启时做掩码与缩放：

[_netcdf.py:977-L993](_netcdf.py#L977-L993)：

```python
def __getitem__(self, index):
    if not self.maskandscale:
        return self.data[index]
    data = self.data[index].copy()
    missing_value = self._get_missing_value()
    data = self._apply_missing_value(data, missing_value)
    scale_factor = self._attributes.get('scale_factor')
    add_offset = self._attributes.get('add_offset')
    if add_offset is not None or scale_factor is not None:
        data = data.astype(np.float64)
    if scale_factor is not None:
        data = data * scale_factor
    if add_offset is not None:
        data += add_offset
    return data
```

`_get_missing_value` 规定 `_FillValue` 优先于 `missing_value`：

[_netcdf.py:1053-L1071](_netcdf.py#L1053-L1071) — NetCDF 标准赋予 `_FillValue` 特殊语义，`missing_value` 只是兼容老数据集。

`__setitem__` 对记录变量做自动扩容：

[_netcdf.py:1008-L1027](_netcdf.py#L1008-L1027) — 若赋值索引超出当前记录数，按 `(recs,) + self._shape[1:]` 重算形状并 resize：

```python
if self.isrec:
    ...
    if recs > len(self.data):
        shape = (recs,) + self._shape[1:]
        try:
            self.data.resize(shape)
        except ValueError:
            dtype = self.data.dtype
            self.__dict__['data'] = np.resize(self.data, shape).astype(dtype)
self.data[index] = data
```

> 「resize 失败就退回 `np.resize`」是因为 mmap 来的或非连续数组可能无法原地 resize。

#### 4.4.4 代码实践

**实践目标**：用官方测试数据 `example_2.nc` 体验 mask-and-scale，对比开/关的差异。

**操作步骤**：

```python
# mask_scale.py —— 示例代码（非项目原有）
import scipy.io as sio, os
from scipy.io import netcdf_file

datadir = os.path.join(os.path.dirname(sio.__file__), 'tests', 'data')
path = os.path.join(datadir, 'example_2.nc')

with netcdf_file(path, maskandscale=True) as f:
    T = f.variables['Temperature']
    print('missing_value =', T.missing_value)
    print('scale_factor  =', T.scale_factor)
    print('add_offset    =', T.add_offset)
    data = T[:].copy()
    print('masked? ', repr(data))           # numpy.ma MaskedArray
    print('compressed    =', data.compressed())

with netcdf_file(path, maskandscale=False) as f:
    T2 = f.variables['Temperature']
    print('raw      =', T2[:].copy())        # 原始整型，无掩码无缩放
```

**需要观察的现象**：`maskandscale=True` 时返回 `numpy.ma` 掩码数组，`missing_value=9999` 的位置被掩掉，其余值被 `×0.01 + 20` 还原；`maskandscale=False` 时返回原始整型，能看到 `9999` 这种填充值原样存在。

**预期结果**：与 `tests/test_netcdf.py:460-L473` 的 `test_maskandscale` 断言一致——`missing_value == 9999`、`add_offset == 20`、`scale_factor == np.float32(0.01)`，还原后的值与 `np.linspace(20,30,15)`（掩去 >99 的点）吻合。

#### 4.4.5 小练习与答案

**练习 1**：`isrec` 表达式里 `bool(self.data.shape)` 这一段是为了排除什么情况？

**答案**：排除零维变量（标量）。零维数组 `data.shape == ()`，`bool(())` 为 `False`，所以零维变量即使 `_shape[0]` 不存在也不会被误判为记录变量（见 `test_zero_dimensional_var`）。

**练习 2**：若一个变量同时有 `_FillValue` 和 `missing_value` 且值不同，`maskandscale=True` 读取时用哪个？

**答案**：用 `_FillValue`。见 `_get_missing_value` 的优先级，以及 `tests/test_netcdf.py:517-L525` 的 `test_read_withFillValueAndMissingValue`——`missing_value` 被当作普通值保留，只有 `_FillValue` 触发掩码。

---

### 4.5 mmap 读取模式与文件生命周期陷阱

#### 4.5.1 概念说明

`mmap`（memory map）把磁盘文件映射进进程虚拟内存，读取时不真正 `read`，而是按需缺页加载。`_netcdf` 在「文件名 + 只读」时默认 `mmap=True`，把整个文件映射成一个 `int8` 大数组 `self._mm_buf`，每个变量的数据就是它上面的一个 view。

好处是**零拷贝、按需读、可处理超大文件**；代价是：变量数组和磁盘文件绑死了——只要还有数组引用着这片 mmap，文件就**不能干净关闭**（关了 mmap，数组就指向失效内存，会 segfault）。这是 NetCDF3 读取里最容易踩的坑。

#### 4.5.2 核心流程

```text
__init__:
  若 filename 是真实文件 且 mode=='r'  -> mmap 默认 True
  若 filename 是 file-like(BytesIO 等)-> mmap 默认 False
  若 mode != 'r'                       -> 强制 mmap=False（写文件不能只读映射）
  mmap=True 时:
     self._mm = mmap.mmap(fp.fileno(), 0, ACCESS_READ)
     self._mm_buf = np.frombuffer(self._mm, dtype=np.int8)   # 整个文件的 int8 视图

读取变量:
  data = self._mm_buf[begin:begin+size].view(dtype=dtype_).reshape(shape)   # 零拷贝 view

close():
  若 self._mm_buf 还有弱引用活着 -> 发 RuntimeWarning，不关 mmap
  否则 -> 安全关闭 mmap 和文件
```

#### 4.5.3 源码精读

`__init__` 里 mmap 的判定逻辑：

[_netcdf.py:238-L256](_netcdf.py#L238-L256) — file-like 默认不 mmap（且没有 `fileno` 时报错）；真实文件默认 mmap；非读模式强制关 mmap：

```python
if hasattr(filename, 'seek'):  # file-like
    ...
    if mmap is None:
        mmap = False
    elif mmap and not hasattr(filename, 'fileno'):
        raise ValueError('Cannot use file object for mmap')
else:  # 字符串文件名
    ...
    if mmap is None:
        mmap = True
if mode != 'r':
    mmap = False        # Cannot read write-only files
```

mmap 缓冲的建立：

[_netcdf.py:268-L272](_netcdf.py#L268-L272)：

```python
self._mm = None
self._mm_buf = None
if self.use_mmap:
    self._mm = mm.mmap(self.fp.fileno(), 0, access=mm.ACCESS_READ)
    self._mm_buf = np.frombuffer(self._mm, dtype=np.int8)
```

读取时变量数据直接是 `_mm_buf` 的 view（零拷贝）：

[_netcdf.py:691-L693](_netcdf.py#L691-L693)：

```python
if self.use_mmap:
    data = self._mm_buf[begin_:begin_+a_size].view(dtype=dtype_)
    data = data.reshape(shape)
```

`close()` 用 `weakref` 检查是否还有数组引用着 mmap，若有则只发警告、不真正关 mmap：

[_netcdf.py:288-L315](_netcdf.py#L288-L315)：

```python
def close(self):
    if hasattr(self, 'fp') and not self.fp.closed:
        try:
            self.flush()
        finally:
            self.variables = {}
            if self._mm_buf is not None:
                ref = weakref.ref(self._mm_buf)
                self._mm_buf = None
                if ref() is None:
                    self._mm.close()              # 安全关闭
                else:
                    warnings.warn(
                        "Cannot close a netcdf_file opened with mmap=True, when "
                        "netcdf_variables or arrays referring to its data still exist...",
                        category=RuntimeWarning, stacklevel=2)
            self._mm = None
            self.fp.close()
__del__ = close
```

> 注意 `__del__ = close`（[_netcdf.py:315](_netcdf.py#L315)）：对象被垃圾回收时也会触发 close，所以即便忘了显式关闭，通常也能在 GC 时收尾——但若此时还有数组活着，照样会发警告。

#### 4.5.4 代码实践

**实践目标**：亲手复现「mmap 数组还活着就无法干净关闭」的警告，并学会用 `.copy()` 解绑。

**操作步骤**：

```python
# mmap_lifetime.py —— 示例代码（非项目原有）
import warnings, scipy.io as sio, os
from scipy.io import netcdf_file

datadir = os.path.join(os.path.dirname(sio.__file__), 'tests', 'data')
path = os.path.join(datadir, 'example_1.nc')

# 场景 A：取了 view 又不 copy，关闭时会警告
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    f = netcdf_file(path, mmap=True)
    lat = f.variables['lat'][:]        # 这是 mmap 上的 view，没 copy
    f.close()                          # 数组还活着 -> 触发 RuntimeWarning
    print('收到警告:', any(issubclass(x.category, RuntimeWarning) for x in w))
    print('lat.sum() 仍可用 =', lat.sum())   # 因为 mmap 没被真关
    del lat                             # 现在才没人引用了

# 场景 B：先 copy，就能安全关闭
with netcdf_file(path, mmap=True) as f:
    lat = f.variables['lat'][:].copy()  # 拷贝到主存
print('关闭后仍可用:', lat.sum())        # 文件已干净关闭，数组照常可用
```

**需要观察的现象**：场景 A 关闭时打印「收到警告: True」，并且控制台出现 `RuntimeWarning: Cannot close a netcdf_file opened with mmap=True...`；场景 B 无任何警告，关闭后数组仍可正常求和。

**预期结果**：与 `tests/test_netcdf.py:350-L370` 的 `test_mmaps_segfault` 行为一致——只要还有数组引用，close 就只警告不真关，从而避免 segfault；先 `.copy()` 则彻底解绑。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `mode != 'r'` 时强制 `mmap = False`？

**答案**：因为 mmap 建立时用的是 `ACCESS_READ`（只读映射），写模式需要可写文件句柄；而且写入流程要 `seek(0)` 重写整个 header，与只读 mmap 语义冲突，所以写/追加模式一律走普通 `read/write`。

**练习 2**：`close()` 里为什么先把 `self._mm_buf = None`，再去检查 `weakref.ref(self._mm_buf)()`？

**答案**：先把自己这个强引用置空，剩下的引用只能是用户手里持有的数组。`weakref.ref(...)` 在置空后立刻构造，若返回 `None` 说明「除了 netcdf_file 自己，没人引用这片缓冲」，可以安全关 mmap；若非 `None` 说明用户的数组还指着它，强行关会导致这些数组指向失效内存，所以改发警告。

---

## 5. 综合实践

把本讲的知识串起来，完成一个「带记录维度的迷你气象数据集」全流程：

```python
# weather_dataset.py —— 示例代码（非项目原有）
import numpy as np
from scipy.io import netcdf_file

with netcdf_file('weather.nc', 'w') as f:
    f.title = 'Mini weather dataset'          # 全局属性
    f.createDimension('latitude', 3)          # 固定维度
    f.createDimension('longitude', 4)         # 固定维度
    f.createDimension('time', None)           # 记录维度（必须第一个以外？实际只要 length=None 即记录维度）

    lat = f.createVariable('latitude', 'f', ('latitude',))
    lat[:]  = np.array([-30.0, 0.0, 30.0], dtype='>f4')
    lat.units = 'degrees_north'

    # 温度挂在 (time, latitude, longitude) —— 记录变量
    temp = f.createVariable('temperature', 'f', ('time', 'latitude', 'longitude'))
    temp.units = 'degC'
    temp.missing_value = -9999.0
    # 写两条时间记录（触发记录变量自动扩容）
    temp[0] = np.ones((3, 4), dtype='>f4') * 20.0
    temp[1] = np.ones((3, 4), dtype='>f4') * 21.0

# 读回并自检
with netcdf_file('weather.nc', 'r') as f:
    print('title     =', f.title)
    print('dims      =', dict(f.dimensions))           # time 应为 None
    print('vars      =', list(f.variables.keys()))
    t = f.variables['temperature']
    print('isrec     =', t.isrec)                       # True
    print('shape     =', t.shape)                       # (2, 3, 4)
    data = t[:].copy()                                  # copy 解绑 mmap
    print('record 0 mean =', data[0].mean())            # 20.0
    print('record 1 mean =', data[1].mean())            # 21.0
```

**自检要点**：

1. `dimensions['time']` 读回是 `None`（记录维度），`temperature.isrec` 为 `True`。
2. 赋值 `temp[0]`、`temp[1]` 触发了 `__setitem__` 里的记录扩容逻辑，最终 `t.shape == (2, 3, 4)`。
3. 用 `.copy()` 取数据，确保 `with` 块退出后数组仍可用（验证你理解了 4.5 的 mmap 生命周期）。
4. 用十六进制工具（如 `xxd weather.nc | head`）看文件头，能看到 `CDF` 魔数、维度段、变量段——把 4.1 的二进制布局落到眼前。

> 若某些细节（如 `temp[0]` 写入后是否需要先 flush）行为不确定，请标注**待本地验证**，不要假装已运行。

## 6. 本讲小结

- NetCDF3 是「自描述二进制」格式：`CDF` 魔数 + 版本字节 + numrecs + 维度/属性/变量三段 header + data 区，全程大端序，空段用 `ABSENT`（8 个 0）表示。
- `TYPEMAP`（读）/ `REVERSE`（写）/ `FILLMAP`（填充）三张表把 NetCDF 6 种外部类型与 numpy dtype、默认填充值打通；不在 `REVERSE` 里的类型（如 `int64`）会被 `createVariable` 拒绝。
- `netcdf_file` 用「两遍写」：先写 metadata（`begin` 占位 0），写数据时再 seek 回填真实偏移；记录变量排在最后，记录区按 `numrecs × Σvsize` 组织。
- `netcdf_variable` 通过 `isrec` 判定记录变量，`__getitem__/__setitem__` 在 `maskandscale=True` 时做 `_FillValue`（优先于 `missing_value`）掩码与 `scale_factor/add_offset` 线性缩放，记录变量赋值时自动扩容。
- mmap 默认开启（只读 + 真实文件名），变量数据是零拷贝 view；但「数组活着就不能干净关文件」，`close()` 用 `weakref` 检测并以 `RuntimeWarning` 提示用户先 `.copy()`。

## 7. 下一步学习建议

- **横向对比**：本讲的 NetCDF3 是「带目录的二进制数据库」，对比 u2-l2 的 FortranFile（裸 record 流）和 u2-l3 的 Matrix Market（纯文本），体会「自描述格式 vs 非自描述格式」在 API 设计上的差异。
- **进阶到 MATLAB 子系统**：下一单元 u3 将进入结构更复杂的 MATLAB `.mat` 子系统（v4/v5 + Cython 底层），那里同样有「header + data + 类型码 + 字节序」的主题，但多了压缩、cell/struct 递归、Cython 加速等机制，可作为本讲思想的「重量级升级版」。
- **安全与健壮性**：本讲的 mmap 生命周期陷阱、`int64` 类型拒绝、损坏文件 `TypeError`/`ValueError` 等，将在 u4-l4「健壮性、错误处理与安全性取舍」里作为 scipy.io 横切主题统一讨论，建议学完 u3 后回顾。
- **源码延伸阅读**：若需要 NetCDF4 / 分组 / 压缩等现代特性，官方推荐外部库 `netcdf4-python`（`_netcdf.py` 文档里多次提及），其 API 与本模块相似，可平滑迁移。
