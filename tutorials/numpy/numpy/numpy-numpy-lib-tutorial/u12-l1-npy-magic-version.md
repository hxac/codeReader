# NPY 格式规范、魔数与版本

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 `.npy` 文件开头那 8 个字节（魔数 + 版本）分别是什么、各自的作用。
- 解释格式版本 1.0 / 2.0 / 3.0 的差异，特别是「header 长度字段从 2 字节变 4 字节」「ASCII 改 UTF-8」这两次升级分别是为了解决什么问题。
- 读懂 `_format_impl.py` 里的核心常量与工具函数：`MAGIC_PREFIX`、`MAGIC_LEN`、`ARRAY_ALIGN`、`_header_size_info`、`magic()`、`read_magic()`、`_check_version()`。
- 用标准库 `struct` 手写一段「魔数 + 版本 + header 长度」的字节流，再用 `numpy.lib.format.read_magic` 解析出来，验证自己理解无误。

本讲是「NPY/NPZ 二进制格式」单元（u12）的第一篇，只讲**文件头里魔数与版本这一段**，不涉及 header 字典内容（那是 u12-l2）和真正的数组读写（u12-l3）。

## 2. 前置知识

### 2.1 二进制文件格式为什么需要「魔数」

打开一个未知文件时，程序第一件事通常是判断「这到底是什么格式」。最通用的做法是在文件开头放一段**固定字节**，叫做 **magic number（魔数）**。比如：

- PNG 以 `\x89PNG` 开头。
- Java 的 `.class` 以 `0xCAFEBABE` 开头。
- gzip 以 `\x1f\x8b` 开头。

魔数的作用是**身份自证**：读文件的程序只要看前几个字节就能确认「这是我认识的格式」，不用管文件扩展名（扩展名可以随便改）。

numpy 的 `.npy` 格式选择了 6 字节魔数 `\x93NUMPY`：第一个字节是 0x93（一个不可打印的控制字符，用来和普通 ASCII 文本区分开），后面跟着人眼可读的 `NUMPY`。这样你用 `cat` 或十六进制工具看一个 `.npy` 文件时，能一眼认出它。

### 2.2 struct 与「格式描述符」

Python 标准库 `struct` 模块用来在「字节串」和「数值」之间转换。它用一串字符描述字节的布局，称为**格式描述符**，本讲会反复用到这几个：

| 描述符 | 含义 | 字节数 |
|--------|------|--------|
| `<` | 小端序（little-endian），放在最前面 | 0 |
| `B` | unsigned char（无符号字节） | 1 |
| `H` | unsigned short（无符号短整型） | 2 |
| `I` | unsigned int（无符号整型） | 4 |

举例：

- `struct.pack('<H', 128)` 把整数 128 按「小端 2 字节」打包，得到 `b'\x80\x00'`。
- `struct.calcsize('<I')` 返回 4，表示这个描述符占 4 字节。
- `struct.unpack('<H', b'\x80\x00')` 读回来，得到 `(128,)`。

记住 `<H` = 2 字节、`<I` = 4 字节，是理解版本 1.0 与 2.0 差异的关键。

### 2.3 承接前序讲义

本讲依赖 u1-l2 建立的「薄再导出层 vs 私有实现层」认知：`.npy` 格式的真正实现全部写在私有文件 [`_format_impl.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/format.py) 里，对外由只有 24 行的薄模块 [`format.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/format.py) 再导出名字。这和 `pad`、`stride_tricks` 等完全一致，本讲不再重复，默认你已经理解。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [numpy/lib/_format_impl.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_format_impl.py) | NPY/NPZ 格式的**全部实现**，包括本讲所有常量与函数。文件开头的模块文档串（module docstring）就是一份权威的格式规范。 |
| [numpy/lib/format.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/format.py) | 薄再导出模块，只有 24 行 `from ._format_impl import (...)`，把 `magic`/`read_magic`/`MAGIC_PREFIX` 等名字搬到公开命名空间。 |
| [numpy/lib/tests/test_format.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_format.py) | 格式的测试集，`test_read_magic` / `test_bad_magic_args` / `malformed_magic` 等是本讲代码实践的依据来源。 |

## 4. 核心概念与源码讲解

### 4.1 文件头常量：MAGIC_PREFIX、MAGIC_LEN 与 ARRAY_ALIGN

#### 4.1.1 概念说明

一个 `.npy` 文件的逻辑布局是这样的（按字节顺序）：

```
┌──────────────┬────────┬────────┬─────────────────┬───────────────────┐
│ MAGIC_PREFIX │ major  │ minor  │ HEADER_LEN 字段  │ header 字典文本    │
│  "\x93NUMPY" │ 1 字节 │ 1 字节 │  (2 或 4 字节)   │ (以 \n 结尾,空格填充) │
│    6 字节    │        │        │                  │                   │
└──────────────┴────────┴────────┴─────────────────┴───────────────────┘
```

前三段加起来构成「固定头部」，后面的 header 字典才是变长部分。本讲关心前四段。这里有两个关键常量：

- **`MAGIC_PREFIX`**：6 字节的魔数本体 `b'\x93NUMPY'`。
- **`MAGIC_LEN`**：注意它**不是** `len(MAGIC_PREFIX)`（那是 6），而是 `len(MAGIC_PREFIX) + 2`，也就是 8。它表示「魔数 + 主版本 + 副版本」这一整块固定头部的字节数。`read_magic` 一次就要读满这 8 字节。

第三个常量 **`ARRAY_ALIGN = 64`** 涉及「为什么 header 要用空格填充到 64 的整数倍」。原因是为了**内存映射（mmap）友好**：数据紧跟在 header 后面，而很多系统（比如 Linux）要求 `mmap` 的偏移量按页对齐。把 header 凑到 64 字节对齐，就能保证数组数据起始位置也对齐，便于 SIMD 指令和内存映射。这个对齐逻辑在 4.1.3 讲 `_wrap_header` 时会用到，本讲只需要知道它的取值。

#### 4.1.2 核心流程

写文件时构造固定头部：

```text
header_prefix = MAGIC_PREFIX + bytes([major, minor]) + struct.pack(fmt, header_len)
                       6 字节            2 字节            2 或 4 字节
```

读文件时：

```text
读 8 字节 = MAGIC_PREFIX(6) + major(1) + minor(1)
   ↓ 校验前 6 字节 == MAGIC_PREFIX
   ↓ 取后 2 字节作为 (major, minor)
再按版本对应的 fmt 读 HEADER_LEN 字段
```

#### 4.1.3 源码精读

常量集中定义在文件顶部：

[\_format_impl.py:177-183](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_format_impl.py#L177-L183) —— 定义格式相关的核心常量。

```python
EXPECTED_KEYS = {'descr', 'fortran_order', 'shape'}
MAGIC_PREFIX = b'\x93NUMPY'
MAGIC_LEN = len(MAGIC_PREFIX) + 2
ARRAY_ALIGN = 64  # plausible values are powers of 2 between 16 and 4096
BUFFER_SIZE = 2**18  # size of buffer for reading npz files in bytes
GROWTH_AXIS_MAX_DIGITS = 21  # = len(str(8*2**64-1)) hypothetical int1 dtype
```

逐行解读：

- `MAGIC_PREFIX` 就是那 6 字节魔数。
- `MAGIC_LEN = len(MAGIC_PREFIX) + 2 = 8`，把 major/minor 两个版本字节也算进「固定头部长度」。这是 `read_magic` 读的字节数。
- `ARRAY_ALIGN = 64`，注释说「合理的取值是 16 到 4096 之间的 2 的幂」，说明这是一个可调但通常不动的对齐粒度。
- `GROWTH_AXIS_MAX_DIGITS = 21`：写 header 时会预留一段空白，便于将来「原地改大数组」（append 数据）。21 是 `len(str(8*2**64-1))`，即一个 64 位机能表示的最大十进制位数，保证即使数组长到极限也够填。本讲只做了解，细节留到 u12-l2。

这些常量随后用在 `_wrap_header` 里做对齐填充（本讲只看对齐那一行）：

[\_format_impl.py:397-418](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_format_impl.py#L397-L418) —— 把 header 文本拼上「魔数+版本+长度+空格填充」。

```python
def _wrap_header(header, version):
    import struct
    assert version is not None
    fmt, encoding = _header_size_info[version]
    header = header.encode(encoding)
    hlen = len(header) + 1                                # +1 给末尾的 \n
    padlen = ARRAY_ALIGN - ((MAGIC_LEN + struct.calcsize(fmt) + hlen) % ARRAY_ALIGN)
    try:
        header_prefix = magic(*version) + struct.pack(fmt, hlen + padlen)
    except struct.error:
        msg = f"Header length {hlen} too big for version={version}"
        raise ValueError(msg) from None
    return header_prefix + header + b' ' * padlen + b'\n'
```

关注对齐那一行：`padlen = ARRAY_ALIGN - ((MAGIC_LEN + struct.calcsize(fmt) + hlen) % ARRAY_ALIGN)`。它把「魔数 + 长度字段 + header 文本」的总长度凑成 64 的整数倍，不足的部分用空格 `b' '` 填充，最后补一个换行 `\n`。`magic(*version)` 负责生成「魔数+版本」这 8 字节，下一节细讲。

#### 4.1.4 代码实践

**目标**：亲手确认 `MAGIC_PREFIX` 与 `MAGIC_LEN` 的值，并理解「+2」的含义。

**步骤**：

1. 在装好 numpy 的环境里启动 Python（或 Jupyter）。
2. 依次执行下面的代码。

```python
import numpy as np
from numpy.lib import format

print(repr(format.MAGIC_PREFIX))   # 期望 b'\x93NUMPY'
print(format.MAGIC_LEN)            # 期望 8
print(len(format.MAGIC_PREFIX))    # 期望 6，对比 MAGIC_LEN 多了 2
```

**预期结果**：

```
b'\x93NUMPY'
8
6
```

**需要观察的现象**：`MAGIC_LEN`（8）恰好比 `len(MAGIC_PREFIX)`（6）多 2，多的正是 major、minor 两个版本字节。`read_magic` 会一次性读满这 8 字节。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `MAGIC_PREFIX` 改成 `b'\x93NPY'`（少一个字母，5 字节），`MAGIC_LEN` 会变成多少？`read_magic` 还能正常工作吗？

**答案**：`MAGIC_LEN = len(MAGIC_PREFIX) + 2 = 5 + 2 = 7`。代码仍能运行（它只是读 7 字节），但读到的「前 5 字节」和真正的 `.npy` 文件（前 6 字节是 `\x93NUMPY`）对不上，`read_magic` 的 `magic_str[:-2] != MAGIC_PREFIX` 校验会失败并抛 `ValueError`。这正是魔数的防错作用。

**练习 2**：`ARRAY_ALIGN` 为什么不取 1（即不对齐）？

**答案**：取 1 就等于不填充，header 后面的数组数据起始位置会任意。这会破坏内存映射（`mmap` 在 Linux 上要求页对齐偏移）和 SIMD 对齐访问。注释也明确写了取值应为「2 的幂」。

---

### 4.2 三版本的格式差异：_header_size_info 字典

#### 4.2.1 概念说明

NPY 格式经历过两次升级，目前支持三个版本：

| 版本 | 发布目的 | HEADER_LEN 字段 | header 文本编码 | header 最大长度 |
|------|----------|-----------------|-----------------|-----------------|
| **1.0** | 最初的格式 | 2 字节（`<H`） | latin1（实际是 ASCII） | 65535 字节 |
| **2.0** | 解决「字段太多的结构化数组 header 超过 64KB」 | 4 字节（`<I`） | latin1 | 约 4 GiB |
| **3.0** | 解决「字段名含非 ASCII 字符（如中文、emoji）」 | 4 字节（`<I`） | **utf-8** | 约 4 GiB |

关键点：

- 1.0 → 2.0 的变化**只是 header 长度字段从 2 字节扩到 4 字节**（`<H` → `<I>`）。2 字节最大只能表示 65535，超长结构化数组的字段名拼起来会超过这个上限，于是换成 4 字节（最大约 4 GiB）。
- 2.0 → 3.0 的变化**只是 header 文本编码从 latin1 改成 utf-8**，长度字段不变。latin1 只能表示 256 个字符，遇到 Unicode 字段名会报 `UnicodeEncodeError`，于是升级编码。

numpy 写文件时默认策略是「能存就尽量用老版本」（兼容性最好）：先试 1.0，header 太长就退 2.0，遇到非 ASCII 字段名再退 3.0，每退一步都发一个 `UserWarning`。这套降级逻辑在 `_wrap_header_guess_version` 里（u12-l2 会讲）。

#### 4.2.2 核心流程

`_header_size_info` 把「版本 → (长度描述符, 编码)」的映射集中存成一个字典：

```text
_header_size_info = {
    (1, 0): ('<H', 'latin1'),   # 2 字节长度, ASCII 文本
    (2, 0): ('<I', 'latin1'),   # 4 字节长度, ASCII 文本
    (3, 0): ('<I', 'utf8'),     # 4 字节长度, UTF-8 文本
}
```

写文件时：用版本号查表 → 拿到 `(fmt, encoding)` → 用 `fmt` 打包 header 长度、用 `encoding` 编码 header 文本。

读文件时：先 `read_magic` 拿到版本号 → 用版本号查同一张表 → 按对应 `fmt` 读长度、按对应 `encoding` 解码文本。

**写读两端共用同一张表**，这是它最大的设计价值：新增一个版本，只需在表里加一行。

#### 4.2.3 源码精读

[\_format_impl.py:185-191](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_format_impl.py#L185-L191) —— 版本与 (长度格式, 编码) 的映射表。

```python
# difference between version 1.0 and 2.0 is a 4 byte (I) header length
# instead of 2 bytes (H) allowing storage of large structured arrays
_header_size_info = {
    (1, 0): ('<H', 'latin1'),
    (2, 0): ('<I', 'latin1'),
    (3, 0): ('<I', 'utf8'),
}
```

注释点明了 1.0 与 2.0 的本质差别：「2.0 用 4 字节（`I`）的 header 长度，代替 1.0 的 2 字节（`H`），以便存大型结构化数组」。

模块文档串里对三个版本有完整规范描述，建议对照阅读：

[\_format_impl.py:90-110](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_format_impl.py#L90-L110) —— 1.0 规范：魔数 6 字节、版本 2 字节、header 长度 2 字节（little-endian unsigned short）、header 字典按 64 字节对齐。

[\_format_impl.py:136-147](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_format_impl.py#L136-L147) —— 2.0 规范：唯一改动是 header 长度字段从 2 字节变 4 字节（little-endian unsigned int），上限扩到 4 GiB。

[\_format_impl.py:149-154](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_format_impl.py#L149-L154) —— 3.0 规范：把 ASCII（实际 latin1）换成 utf-8，支持 Unicode 字段名。

读 header 时，`_read_array_header` 同样查这张表：

[\_format_impl.py:635-643](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_format_impl.py#L635-L643) —— 读取时按版本查表，决定长度字段大小与解码方式。

```python
hinfo = _header_size_info.get(version)
if hinfo is None:
    raise ValueError(f"Invalid version {version!r}")
hlength_type, encoding = hinfo

hlength_str = _read_bytes(fp, struct.calcsize(hlength_type), "array header length")
header_length = struct.unpack(hlength_type, hlength_str)[0]
header = _read_bytes(fp, header_length, "array header")
header = header.decode(encoding)
```

注意 `struct.calcsize(hlength_type)`：对 1.0 是 2，对 2.0/3.0 是 4。读多少字节长度完全由版本决定，这就是「同一张表驱动读写两端」。

#### 4.2.4 代码实践

**目标**：验证三版本的长度字段字节数差异。

**步骤**：

```python
import struct
from numpy.lib._format_impl import _header_size_info

for ver, (fmt, encoding) in sorted(_header_size_info.items()):
    print(f"version {ver}: length field fmt={fmt}, "
          f"长度字段占 {struct.calcsize(fmt)} 字节, encoding={encoding}")
```

**预期结果**：

```
version (1, 0): length field fmt=<H, 长度字段占 2 字节, encoding=latin1
version (2, 0): length field fmt=<I, 长度字段占 4 字节, encoding=latin1
version (3, 0): length field fmt=<I, 长度字段占 4 字节, encoding=utf8
```

**需要观察的现象**：1.0 的长度字段是 2 字节，2.0 和 3.0 都是 4 字节；只有 3.0 的编码是 utf8，前两者是 latin1。这印证了「版本升级各只动一处」。

#### 4.2.5 小练习与答案

**练习 1**：一个 header 文本长度为 70 字节的数组，分别按 1.0 和 2.0 写，文件头（到 header 文本结束前）的固定部分分别占多少字节？

**答案**：固定部分 = `MAGIC_PREFIX`(6) + 版本(2) + 长度字段。1.0 长度字段 2 字节，共 6+2+2 = 10 字节；2.0 长度字段 4 字节，共 6+2+4 = 12 字节。（注意 header 文本本身还要按 64 对齐填充，这里只算固定部分。）

**练习 2**：假设要支持一个新版本 `(4, 0)`，要求 header 用 8 字节长度字段（`<Q`）且保持 utf-8 编码，需要改哪些地方？

**答案**：在 `_header_size_info` 里加一行 `(4, 0): ('<Q', 'utf8')`，并在 `_check_version`（见 4.4）的白名单里加上 `(4, 0)`。读写逻辑因为都查这张表，无需额外改动——这正是把映射抽成字典的好处。

---

### 4.3 magic() 与 read_magic()：读写魔数与版本号

#### 4.3.1 概念说明

这两个函数是魔数/版本字节的「写端」和「读端」，互为逆操作：

- **`magic(major, minor)`**：给定版本号，**生成**「魔数 + 版本」共 8 字节的字节串。用于写文件时拼头部。
- **`read_magic(fp)`**：从一个已打开的文件对象里**读取**前 8 字节，校验魔数正确后，返回 `(major, minor)` 版本元组。用于读文件时识别版本。

注意 `read_magic` 读完后，文件指针正好停在「魔数+版本」之后（即偏移 `MAGIC_LEN = 8` 的位置），接下来正好可以继续读 header 长度字段。这一点测试里有断言（见 4.3.4）。

#### 4.3.2 核心流程

**`magic(major, minor)`**：

```text
校验 0 <= major <= 255
校验 0 <= minor <= 255
返回 MAGIC_PREFIX + bytes([major, minor])
        b'\x93NUMPY'    b'\x01\x00' 等
```

为什么是 0~255？因为 major、minor 各占 1 字节（`B` 类型），1 字节无符号数范围就是 0~255。

**`read_magic(fp)`**：

```text
读 MAGIC_LEN(=8) 字节
若 前 6 字节 != MAGIC_PREFIX → 抛 ValueError("the magic string is not correct...")
取后 2 字节 → (major, minor)
返回 (major, minor)
```

读固定长度字节这件事本身不简单（文件流可能一次给不满），所以它委托给 `_read_bytes`，后者会循环读直到凑够字节数。

#### 4.3.3 源码精读

[\_format_impl.py:205-226](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_format_impl.py#L205-L226) —— `magic`：拼出 8 字节固定头部。

```python
@set_module("numpy.lib.format")
def magic(major, minor):
    if major < 0 or major > 255:
        raise ValueError("major version must be 0 <= major < 256")
    if minor < 0 or minor > 255:
        raise ValueError("minor version must be 0 <= minor < 256")
    return MAGIC_PREFIX + bytes([major, minor])
```

两个边界校验把版本号限制在 1 字节可表示范围内，然后用 `bytes([major, minor])` 把两个整数转成 2 字节字节串（如 `bytes([1, 0]) == b'\x01\x00'`），拼在魔数后面。装饰器 `@set_module("numpy.lib.format")` 把函数的 `__module__` 钉成公开模块名（即使用户从 `_format_impl` 直接导入，help 里也显示来自 `numpy.lib.format`），这是承接 u1-l2 讲过的「再导出分层」细节。

[\_format_impl.py:229-247](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_format_impl.py#L229-L247) —— `read_magic`：读 8 字节并校验魔数。

```python
@set_module("numpy.lib.format")
def read_magic(fp):
    magic_str = _read_bytes(fp, MAGIC_LEN, "magic string")
    if magic_str[:-2] != MAGIC_PREFIX:
        msg = "the magic string is not correct; expected %r, got %r"
        raise ValueError(msg % (MAGIC_PREFIX, magic_str[:-2]))
    major, minor = magic_str[-2:]
    return major, minor
```

逐行解读：

- `_read_bytes(fp, MAGIC_LEN, "magic string")` 严格读满 8 字节，读不够（比如文件太短）会抛 `EOF` 错误，错误信息里带上 `"magic string"` 这个上下文。
- `magic_str[:-2]` 取前 6 字节（魔数部分），和 `MAGIC_PREFIX` 比；不等就抛错，错误信息同时打印「期望值」和「实际值」，方便排查。
- `magic_str[-2:]` 取最后 2 字节，正好是 `(major, minor)`。注意这里 `major, minor` 是两个「字节整数」（0~255），不是字符。

读固定字节数的底层工具 `_read_bytes`：

[\_format_impl.py:1006-1031](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_format_impl.py#L1006-L1031) —— `_read_bytes`：循环读直到凑够 `size` 字节。

```python
def _read_bytes(fp, size, error_template="ran out of data"):
    data = b""
    while True:
        try:
            r = fp.read(size - len(data))
            data += r
            if len(r) == 0 or len(data) == size:
                break
        except BlockingIOError:
            pass
    if len(data) != size:
        msg = "EOF: reading %s, expected %d bytes got %d"
        raise ValueError(msg % (error_template, size, len(data)))
    else:
        return data
```

为什么要循环？因为文件流（尤其压缩流、网络流）的 `.read(n)` **不保证**一次返回 n 字节，可能返回更少。这里循环直到 `len(data) == size` 或遇到 EOF（`len(r) == 0`）。还处理了非阻塞 IO 的 `BlockingIOError`。这个函数是整个格式读取的基石，`read_magic` 和读 header、读数组数据都靠它。

#### 4.3.4 代码实践（本讲核心实践）

**目标**：用标准库 `struct` 手写一段「魔数 + 版本」字节，再用 `read_magic` 解析，验证自己理解的字节布局正确。这正是任务描述要求的实践。

**步骤**：

```python
import struct
from io import BytesIO
import numpy as np
from numpy.lib import format

# 1) 用 magic() 生成「正确」的 8 字节，作为对照
good = format.magic(2, 0)
print("magic(2,0) ->", good)          # b'\x93NUMPY\x02\x00'

# 2) 手写：魔数 6 字节 + 主版本 1 字节 + 副版本 1 字节
handmade = b'\x93NUMPY' + bytes([2, 0])
print("handmade   ->", handmade)
print("两者相等？", handmade == good)  # True

# 3) 把手写字节塞进 BytesIO，用 read_magic 解析
fp = BytesIO(handmade)
version = format.read_magic(fp)
print("解析出的版本:", version)          # (2, 0)
print("文件指针位置:", fp.tell())        # 8 == MAGIC_LEN

# 4) 故意写一个错误魔数（把 \x93 改成 \x92），看 read_magic 如何报错
bad = b'\x92NUMPY' + bytes([1, 0])
try:
    format.read_magic(BytesIO(bad))
except ValueError as e:
    print("错误魔数被拦截:", e)
```

**预期结果**：

```
magic(2,0) -> b'\x93NUMPY\x02\x00'
handmade   -> b'\x93NUMPY\x02\x00'
两者相等？ True
解析出的版本: (2, 0)
文件指针位置: 8
错误魔数被拦截: the magic string is not correct; expected b'\x93NUMPY', got b'\x92NUMPY'
```

**需要观察的现象**：

1. `magic(2,0)` 的输出确实是 8 字节，最后两字节是 `\x02\x00`（小端无所谓，因为这是两个独立单字节）。
2. 手写的 `b'\x93NUMPY' + bytes([2, 0])` 和 `magic()` 完全相等，证明你理解了字节布局。
3. `read_magic` 读完后文件指针恰好在 8（`MAGIC_LEN`），印证了「读完后指针停在固定头部之后」。
4. 魔数错一个字节（`\x93`→`\x92`）就被拦截，且错误信息同时给出「期望」和「实际」，这是 4.3.3 里 `magic_str[:-2] != MAGIC_PREFIX` 的效果。

> 这段实践和官方测试 `test_read_magic`（[test_format.py:818-837](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_format.py#L818-L837)）断言 `read_magic` 返回 `(1,0)`/`(2,0)` 且 `fp.tell() == format.MAGIC_LEN` 完全一致。错误魔数分支则对应 `test_read_magic_bad_magic`（[test_format.py:839-842](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_format.py#L839-L842)）和它的夹具 `malformed_magic`（[test_format.py:808-812](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_format.py#L808-L812)），后者列举了 `\x92NUMPY`、`\x93numpy`、`\x93MATLB` 等各种坏魔数。

#### 4.3.5 小练习与答案

**练习 1**：`format.magic(1, 256)` 会发生什么？为什么？

**答案**：抛 `ValueError("minor version must be 0 <= minor < 256")`。因为 minor 占 1 字节，256 超出 `bytes([x])` 能表示的范围（0~255）。官方测试 `test_bad_magic_args`（[test_format.py:851-855](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_format.py#L851-L855)）正是断言 `-1`、`256` 都会报错。

**练习 2**：`read_magic` 返回的 `major` 是什么类型？是字符串 `'2'` 还是整数 `2`？

**答案**：是整数 `2`。因为 `magic_str[-2:]` 取的是字节串的元素，`b'\x93NUMPY\x02\x00'[-2]` 得到整数 `2`（Python 3 里 `bytes` 索引返回 int）。后续 `major, minor` 被用作字典 `_header_size_info` 的键，如 `(2, 0)`，所以必须是整数。

---

### 4.4 _check_version：版本白名单校验

#### 4.4.1 概念说明

`_check_version(version)` 是一个**白名单守卫**：它检查传入的版本号是否在 numpy 支持的范围内。只要不是 `(1,0)`、`(2,0)`、`(3,0)` 或 `None`，就抛 `ValueError`。

它被用在两个时机：

- **写文件前**（`write_array`、`open_memmap` 的写分支）：确认用户显式指定的 `version=` 参数合法。
- **读文件后**（`read_array`、`open_memmap` 的读分支）：`read_magic` 读出版本后，立刻校验是不是认识的版本，防止读到未知版本继续瞎解析。

`None` 也算合法，它表示「不指定版本，让 numpy 自动挑最合适的」（写时用最老能存的版本，见 `_wrap_header_guess_version`）。

#### 4.4.2 核心流程

```text
若 version 不在 [(1,0), (2,0), (3,0), None] 中:
    抛 ValueError("we only support format version (1,0), (2,0), and (3,0), not <version>")
否则: 直接返回（无返回值，纯校验）
```

#### 4.4.3 源码精读

[\_format_impl.py:199-202](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_format_impl.py#L199-L202) —— `_check_version`：版本白名单。

```python
def _check_version(version):
    if version not in [(1, 0), (2, 0), (3, 0), None]:
        msg = "we only support format version (1,0), (2,0), and (3,0), not %s"
        raise ValueError(msg % (version,))
```

注意几个设计细节：

- 函数名带前导下划线 `_`，表示它是模块内部工具，不在薄模块 `format.py` 的再导出列表里（对比 4.3 的 `magic`/`read_magic` 是公开的）。这是承接 u1-l2 的「下划线区分可见性」惯例。
- 白名单是硬编码的列表字面量，和 `_header_size_info` 的键（`(1,0)/(2,0)/(3,0)`）是同一组。理论上可以直接写 `version not in _header_size_info and version is not None`，但这里显式列出更清晰，也允许 `None`。
- 它是**纯校验、无返回值**的函数，调用方靠「没抛错」来判断合法。

调用点举例（读分支）：

[\_format_impl.py:826-829](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_format_impl.py#L826-L829) —— `read_array` 读到魔数后立刻校验版本。

```python
version = read_magic(fp)
_check_version(version)
shape, fortran_order, dtype = _read_array_header(
        fp, version, max_header_size=max_header_size)
```

`read_magic` 读出 `version` 后，紧接着 `_check_version(version)` 把不认识的版本拦在解析 header 之前。这就解释了 4.3.4 实践里坏魔数分支测试用的 `bad_version_magic`（如 `b'\x93NUMPY\x01\x01'`、`b'\x93NUMPY\x00\x00'`，见 [test_format.py:800-806](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_format.py#L800-L806)）为何会在 `read_array` 时抛 `ValueError`——它们魔数正确、但版本号 `(1,1)`/`(0,0)` 不在白名单。

#### 4.4.4 代码实践

**目标**：验证白名单边界，并体会「读未知版本被拦在 header 解析之前」。

**步骤**：

```python
from io import BytesIO
import numpy as np
from numpy.lib import format
from numpy.lib._format_impl import _check_version

# 1) 合法版本不报错
for v in [(1, 0), (2, 0), (3, 0), None]:
    _check_version(v)          # 不抛错即合法
print("合法版本 (1,0)/(2,0)/(3,0)/None 全部通过")

# 2) 非法版本立即报错
for v in [(0, 0), (1, 1), (4, 0), (2, 1)]:
    try:
        _check_version(v)
    except ValueError as e:
        print(f"拦截 {v}:", e)

# 3) 构造一个「魔数正确但版本未知」的 .npy 头，read_array 应在 _check_version 处失败
#    (1, 1) 版本字节 = major=1, minor=1
fake = format.magic(1, 1) + b'\x00' * 20     # 魔数正确, 但版本 (1,1) 非法
try:
    format.read_array(BytesIO(fake))
except ValueError as e:
    print("read_array 拦截未知版本:", e)
```

**预期结果**：

```
合法版本 (1,0)/(2,0)/(3,0)/None 全部通过
拦截 (0, 0): we only support format version (1,0), (2,0), and (3,0), not (0, 0)
拦截 (1, 1): we only support format version (1,0), (2,0), and (3,0), not (1, 1)
拦截 (4, 0): we only support format version (1,0), (2,0), and (3,0), not (4, 0)
拦截 (2, 1): we only support format version (1,0), (2,0), and (3,0), not (2, 1)
read_array 拦截未知版本: we only support format version (1,0), (2,0), and (3,0), not (1, 1)
```

**需要观察的现象**：第 3 步里，即便我们用 `format.magic(1,1)` 造出了「魔数正确」的字节流，`read_array` 依然在 `_check_version` 处把它拦下，**不会**继续去解析后面那 20 个无意义字节。这说明版本校验是一道前置闸门。

> 如果你的 numpy 版本中 `read_array` 的报错信息措辞或行号与本讲不同，属于正常现象，以你本地的 `_format_impl.py` 为准。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `_check_version` 要把 `None` 也算合法？

**答案**：因为 numpy 的公开 API（`np.save` 等）允许用户不指定版本，此时传下来的 `version=None`，语义是「让 numpy 自动挑最老能存的版本」（见 `_wrap_header_guess_version`）。若 `None` 不合法，自动选版本就无法实现。

**练习 2**：如果未来 numpy 想废弃 1.0（只支持 2.0/3.0），改动会波及 `_check_version` 和 `_header_size_info` 吗？

**答案**：会同时波及两者。要从 `_check_version` 的白名单删掉 `(1,0)`，并从 `_header_size_info` 删掉 `(1,0): ('<H', 'latin1')` 这一行。但因为读路径是「`read_magic` → `_check_version` → 查 `_header_size_info`」，只要两处一致删掉，读 1.0 文件就会在 `_check_version` 处被干净地拦下。这正是把白名单和映射表分开但保持同步的意义。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个「逆向工程 `.npy` 文件头」的小任务。这个任务模拟「在不依赖 numpy 写入的前提下，看懂一个 `.npy` 文件开头到底装了什么」。

**任务**：自己生成一个真实的 1.0 版 `.npy` 字节流，逐字节解释前 12 字节（魔数 6 + 版本 2 + header 长度 2 + 留意对齐），然后只凭 `struct` 和常量把它「盲读」出来，最后用 `read_magic` + `_check_version` 复核。

**步骤**：

```python
import struct
from io import BytesIO
import numpy as np
from numpy.lib import format
from numpy.lib._format_impl import _check_version, _header_size_info

# (a) 让 numpy 写一个真实的 1.0 版数组到内存流
arr = np.array([[1, 2, 3], [4, 5, 6]], dtype='<f8')
buf = BytesIO()
format.write_array(buf, arr, version=(1, 0))
raw = buf.getvalue()

# (b) 逐字节盲读前 12 字节（只用 struct + 常量，不调用 numpy 的解析）
prefix = raw[:6]
major, minor = raw[6], raw[8-1] if False else raw[7]   # 显式取 [6] 和 [7]
# header 长度字段：1.0 用 '<H'，从第 8 字节开始读 2 字节
fmt_len, enc = _header_size_info[(1, 0)]
(hlen,) = struct.unpack(fmt_len, raw[8:8+struct.calcsize(fmt_len)])

print("魔数     :", prefix, "正确？", prefix == format.MAGIC_PREFIX)
print("版本     :", (major, minor), "== (1, 0)？", (major, minor) == (1, 0))
print("header 长度字段:", hlen, "字节")
print("header 长度字段占:", struct.calcsize(fmt_len), "字节 (应为 2)")
print("第 10 字节开始的 header 文本片段:", raw[10:10+40])

# (c) 用 numpy 官方解析复核
buf2 = BytesIO(raw)
ver = format.read_magic(buf2)         # 期望 (1, 0)
_check_version(ver)                   # 不抛错即合法
print("read_magic 解析:", ver, "指针位置:", buf2.tell(), "(应为 8 = MAGIC_LEN)")
```

**预期结果**（`header 文本片段` 一行因 numpy 版本略有差异，但关键断言应成立）：

```
魔数     : b'\x93NUMPY' 正确？ True
版本     : (1, 0) == (1, 0)？ True
header 长度字段: 64 字节
header 长度字段占: 2 字节 (应为 2)
第 10 字节开始的 header 文本片段: b"{'descr': '<f8', 'fortran_order': False"
read_magic 解析: (1, 0) 指针位置: 8 (应为 8 = MAGIC_LEN)
```

> 说明：`hlen` 通常正好等于 `ARRAY_ALIGN`（64），因为这个小数组的 header 文本很短，被填充到刚好 64 字节对齐。header 字典内容（`descr`/`fortran_order`/`shape`）是 u12-l2 的主题，本任务只要确认「魔数 + 版本 + 长度字段」这三段盲读正确即可。

**串联要点**：

- 模块 4.1 的 `MAGIC_PREFIX`/`MAGIC_LEN` 让你盲读出魔数与「固定头部 8 字节」。
- 模块 4.2 的 `_header_size_info` 让你知道 1.0 的长度字段是 `<H`（2 字节），从而正确读出 `hlen`。
- 模块 4.3 的 `read_magic` 复核你盲读的版本号，并确认指针停在 8。
- 模块 4.4 的 `_check_version` 把未知版本拦在解析之前。

如果无法本地运行，以上结果标注为「待本地验证」，但字节布局与函数行为均来自源码 [`_format_impl.py:177-247`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_format_impl.py#L177-L247) 与测试 [`test_format.py:818-855`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_format.py#L818-L855)，可据此手工推演。

## 6. 本讲小结

- `.npy` 文件以 6 字节魔数 `\x93NUMPY` 开头（`MAGIC_PREFIX`），紧跟 2 字节版本号 `(major, minor)`，这 8 字节合称固定头部，长度由常量 `MAGIC_LEN = len(MAGIC_PREFIX) + 2 = 8` 描述。
- 格式有三个版本：1.0（长度字段 2 字节 `<H`、latin1）、2.0（长度字段扩到 4 字节 `<I`，为存超长结构化数组）、3.0（编码改 utf-8，为存 Unicode 字段名）。每次升级**只改一处**，映射统一存在 `_header_size_info` 字典里，读写两端共用。
- `magic(major, minor)` 生成固定头部（校验 0~255 后拼 `MAGIC_PREFIX + bytes([major, minor])`）；`read_magic(fp)` 读 8 字节、校验魔数、返回版本元组，读完指针恰在 `MAGIC_LEN` 处。两者互逆。
- 底层 `_read_bytes` 用循环保证「读满指定字节数」，能应对流式读取返回不完整的情况，是所有读取的基石。
- `_check_version` 是版本白名单守卫，把未知版本拦在 header 解析之前，支持 `(1,0)/(2,0)/(3,0)/None`。
- `ARRAY_ALIGN = 64` 把 header 凑到 64 字节对齐，保证紧跟其后的数组数据起始位置利于内存映射与 SIMD 对齐（对齐填充发生在 `_wrap_header`）。

## 7. 下一步学习建议

- **继续本单元 u12-l2《header 序列化与 dtype 描述》**：本讲只读了「长度字段 = 64」，但那 64 字节里装的 header 字典（`{'descr': ..., 'fortran_order': ..., 'shape': ...}`）怎么构造、怎么用 `literal_eval` 安全解析、`dtype_to_descr`/`descr_to_dtype` 如何往返，是下一讲的主题。
- **然后 u12-l3《数组读写与内存映射》**：把 `write_array`/`read_array`/`open_memmap` 的主流程串起来，看 `read_magic` → `_check_version` → `_read_array_header` → 读数据 这条完整调用链，以及 `allow_pickle` 的安全权衡。
- **辅助阅读**：模块文档串 [\_format_impl.py:83-163](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_format_impl.py#L83-L163) 是一份权威且比 NEP-0001 更新的格式规范，建议完整读一遍；官方测试 [test_format.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_format.py) 里的 `test_read_magic` / `test_bad_magic_args` / `malformed_magic` / `bad_version_magic` 是理解魔数与版本边界行为的最佳范例。
