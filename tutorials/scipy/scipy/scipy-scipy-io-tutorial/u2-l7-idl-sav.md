# IDL save 文件读取（_idl.py）

## 1. 本讲目标

IDL（Interactive Data Language）是 ITT / NV5 Visual Information Systems 出品的一门科学可视化与数值计算语言，常用于天文、地球物理、遥感等领域。IDL 的 `SAVE` 过程可以把工作区里的变量「打包」成一个 `.sav` 二进制文件（俗称 save 文件），方便归档或在 IDL 会话之间传递。`scipy.io` 提供了一个**只读**的解析器 `readsav`，让你在 Python 里把这些变量取回来。

本讲聚焦 `scipy/io/_idl.py` 这一个文件，读完本讲你应当能够：

- 说出 IDL `.sav` 文件的「`SR` 签名 + record 流」整体结构，以及压缩文件是如何被透明展开的。
- 跟踪 `readsav` 的完整主流程：打开文件 → 校验签名 → （可能解压）→ 逐 record 读取 → 收集堆数据 → 解析指针 → 装进返回容器。
- 读懂 `_read_record` 如何按 record 类型分派，以及 `_read_typedesc` / `_read_arraydesc` / `_read_structdesc` / `_read_tagdesc` 这一组「描述符」如何用位标志编码「标量 / 数组 / 结构」。
- 理解 `_read_data` / `_read_array` / `_read_structure` 如何把字节重建为 numpy 标量、数组与 record array，并能解释其中**列主序 reshape** 与 **16 位类型非紧凑存储**两个细节。
- 掌握 IDL 的「指针 + 堆」机制：`Pointer` / `ObjectPointer` 占位符如何在 `_replace_heap` 的第二趟里被替换成真实数据，以及为什么两个指针能指向**同一个** Python 对象。
- 区分 `python_dict=False`（默认）时 `AttrDict` 的「大小写不敏感 + 属性 / 下标 / 调用」三种访问方式，与 `python_dict=True` 时普通 `dict` 的差异。

本讲承接 u2-l5（NetCDF3）建立的「自描述二进制格式 = 魔数 + 头 + 数据区」与「列主序（Fortran order）」直觉。IDL `.sav` 同样是大端序、列主序，但多了一层 record 偏移寻址与指针堆机制，复杂度更高一层。

## 2. 前置知识

### 2.1 record（记录）流式文件

IDL `.sav` 不是「一个头 + 一大块数据」的结构，而是一条** record（记录）流**：文件由若干 record 首尾相接（甚至带间隙）组成，每个 record 自带一个「下一个 record 在文件的哪个字节」的绝对偏移量。读取时不必假设 record 紧挨着排列，而是读完当前 record 后**直接 seek 到下一个 record 的位置**。这种设计让格式对填充字节、损坏区段有一定容忍度。

> 类比：这有点像链表——每个节点都写着「下一个节点的地址」，而不是数组那样按固定步长推进。

### 2.2 大端序（big-endian）

IDL `.sav` 的所有多字节整数都按**大端序**存储（最高有效字节在前）。`_idl.py` 里所有 `struct.unpack` 都用 `>` 前缀，`DTYPE_DICT` 里所有数值 dtype 也都以 `>` 开头。如果你忘了大小端，读出来的数字会变成荒谬的值。

### 2.3 列主序与维度反转

IDL 和 Fortran、MATLAB 一样是**列主序（column-major）**：多维数组在内存里「先走第 0 维、再走第 1 维」。NumPy 默认是**行主序（row-major）**。`.sav` 里维度按列主序存放（`dims[0]` 是最慢变化的维），读回时要 `dims.reverse()` 再 `reshape`，才能得到形状正确的 NumPy 数组。这一点在 u2-l2（Fortran）和 u2-l5（NetCDF）里已经反复出现。

### 2.4 指针与堆（heap）

IDL 有「堆指针」：一个指针变量本身不存数据，而是存一个**堆索引**，真正的数据放在堆里。这解决了两个需求：

- **共享**：多个指针可以指向同一份堆数据（修改一处、处处可见）。
- **递归 / 动态结构**：数组的元素、结构的字段都可以是指针，从而表达任意嵌套。

`readsav` 用两趟处理这件事：第一趟把所有 record（含堆数据 record）原样读进列表；第二趟先建立 `堆索引 → 数据` 的字典，再把变量里的指针占位符逐一替换成真实数据。正是因为堆是个字典，两个指向同一索引的指针替换后会变成**同一个** Python 对象。

> 术语速查：**record**（记录）、**rectype**（记录类型码）、**typecode**（IDL 数据类型码）、**typedesc**（类型描述符）、**heap**（堆）、**recarray**（NumPy 的记录数组 / 结构数组）、**大端序**、**VARSTART**（变量起始魔数，值为 7）。

## 3. 本讲源码地图

本讲的核心是 `scipy/io/_idl.py` 一个文件，外加顶层的导出与弃用包装：

| 文件 | 作用 |
| --- | --- |
| `scipy/io/_idl.py` | IDL `.sav` 读取的全部实现：常量表 `DTYPE_DICT` / `RECTYPE_DICT`、原子读取函数、描述符读取、`_read_record` / `_read_data` / `_read_array` / `_read_structure`、`Pointer` / `ObjectPointer`、`_replace_heap`、`AttrDict`、公共入口 `readsav` |
| `scipy/io/__init__.py` | 顶层包，第 111 行 `from ._idl import readsav` 把函数 re-export 到 `scipy.io` 命名空间 |
| `scipy/io/idl.py` | 弃用包装模块（无前缀），在 SciPy 2.0 将被移除，靠 `__getattr__` 转发到 `_idl`（详见 u4-l2） |
| `scipy/io/tests/data/*.sav` | 48 个测试数据文件，覆盖标量、1–8 维数组、结构体、指针、压缩、损坏指针等场景 |
| `scipy/io/tests/test_idl.py` | 测试套件，展示了各类 `.sav` 的预期读取结果，是理解格式行为最好的「规格说明书」 |

对外公共入口只有一个：`scipy.io.readsav`。文件里其余的类与函数都是内部实现细节，但它们正是理解「字节如何变成 Python 对象」的关键。

## 4. 核心概念与源码讲解

### 4.1 文件头、record 循环与 readsav 主流程

#### 4.1.1 概念说明

`readsav` 是本文件唯一的公共入口（`__all__ = ['readsav']`，[scipy/io/_idl.py:L30-L30](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L30-L30)），它把一个 `.sav` 文件读成一个装着所有变量的容器。理解它的关键是先认清 `.sav` 的**文件头**：

- 前 2 字节是**签名**，必须是 `b'SR'`（"Save Record" 的缩写），否则直接报错。
- 接下来 2 字节是**record 格式码** `recfmt`：
  - `b'\x00\x04'` —— 普通（未压缩）文件。
  - `b'\x00\x06'` —— 用 IDL `/compress` 选项写出的压缩文件。

对于压缩文件，`readsav` **不会**整文件解压，而是**逐 record 解压**到一个临时文件，再把这个临时文件当作普通文件继续读。这样做的原因是：record 的「下一个 record 偏移」是绝对字节位置，解压后体积变了，偏移必须**重写**，所以不能简单地对整个流做一次 `zlib.decompress`。

签名之后就是一条 record 流，一直读到 `END_MARKER` record 为止。

#### 4.1.2 核心流程

`readsav` 的整体流程（[scipy/io/_idl.py:L677-L917](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L677-L917)）：

```text
readsav(file_name, python_dict=False, ...)
  ├─ variables = AttrDict()            # 或普通 dict（当 python_dict=True）
  ├─ 打开文件，读 2 字节签名 → 必须是 b'SR'
  ├─ 读 2 字节 recfmt
  │     ├─ b'\x00\x04' → 普通，直接进入 record 循环
  │     └─ b'\x00\x06' → 压缩：逐 record 解压到临时文件，重写 nextrec
  ├─ while True:                       # record 主循环
  │     r = _read_record(f)
  │     records.append(r)
  │     if r['end']: break             # END_MARKER
  ├─ heap = { r['heap_index']: r['data']  for HEAP_DATA record }   # 第一趟收集堆
  ├─ for VARIABLE record:              # 第二趟解析指针
  │     _replace_heap(r['data'], heap)
  │     variables[r['varname']] = r['data']
  ├─ （可选）verbose 打印元信息
  └─ return variables
```

其中 record 的「下一个 record 偏移」是一个 **64 位整数**，由两个 32 位半字拼成（支持大于 4 GiB 的文件，由 `PROMOTE64` record 启用）：

\[
\text{nextrec} = \text{low}_{32} + \text{high}_{32} \times 2^{32}
\]

这个拼装在 `_read_record` 与压缩分支里各出现一次。

#### 4.1.3 源码精读

公共入口 `readsav` 的签名与四个参数（容器选择、压缩文件名、详细输出）：

[scipy/io/_idl.py:L677-L678](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L677-L678)

容器初始化——默认用大小写不敏感的 `AttrDict`，只有当 `python_dict=True` 或用户传入 `idict` 时才用普通 `dict`：

[scipy/io/_idl.py:L746-L751](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L746-L751)

签名校验（必须是 `b'SR'`）与 record 格式码读取：

[scipy/io/_idl.py:L756-L763](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L756-L763)

压缩分支的核心：先写一个新的未压缩头 `b'SR\x00\x04'`，然后循环读每个 record 的 `rectype` / `nextrec` / `unknown` 头部，遇到 `END_MARKER` 就收尾，否则把 `pos` 到 `nextrec` 之间的字节 `zlib.decompress` 解压、**重新计算**输出文件的 `nextrec`、再写回：

[scipy/io/_idl.py:L781-L828](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L781-L828)

注意第 815 行 `nextrec = fout.tell() + len(rec_string) + 12`：`12` 正好是每个 record 头部（4 字节 rectype + 8 字节 nextrec）的大小，所以新的 `nextrec` 精确指向「下一个 record 头」的位置。解压完后把 `f` 指向临时文件并 `seek(4)`（跳过 `b'SR'`，从 recfmt 之后开始），后续逻辑与普通文件完全一致。

record 主循环非常简短——一直读到带 `end` 标记的 record：

[scipy/io/_idl.py:L834-L842](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L834-L842)

两趟处理：先收集所有 `HEAP_DATA` record 成 `{heap_index: data}` 字典，再遍历 `VARIABLE` record，调用 `_replace_heap` 把指针替换成真实数据后存入容器：

[scipy/io/_idl.py:L844-L856](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L844-L856)

第 856 行 `variables[r['varname'].lower()] = r['data']` 把变量名**小写化**后存储——这是 IDL 变量名大小写不敏感的体现，也是 `AttrDict` 能用任意大小写访问的基础。

#### 4.1.4 代码实践

**实践目标**：用 `verbose=True` 读取一个真实 `.sav`，观察 `readsav` 打印的「record 类型统计」与「可用变量」，建立对 record 流的直觉。

```python
# 示例代码
from os.path import dirname, join as pjoin
import scipy.io as sio
from scipy.io import readsav

data_dir = pjoin(dirname(sio.__file__), 'tests', 'data')
sav_fname = pjoin(data_dir, 'scalar_float32.sav')

sav = readsav(sav_fname, verbose=True)
print("类型:", type(sav).__name__)
print("变量:", list(sav.keys()))
print("f32 =", sav.f32)
```

**操作步骤**：定位到 `scipy/io/tests/data` 目录（用 `scipy.io.__file__` 动态拼接，避免硬编码路径），对一个标量文件开 verbose 读取。

**需要观察的现象**：终端会先打印一段 `-----` 分隔的时间戳 / 版本信息，接着打印「Successfully read N records of which: - X are of type TIMESTAMP - Y are of type VARIABLE ...」，最后列出 `Available variables`。

**预期结果**：返回类型是 `AttrDict`；变量列表为 `['f32']`；`sav.f32` 是一个 `numpy.float32` 标量，值约为 `-3.1234567e+37`（与 `test_idl.py` 的 `test_float32` 断言一致）。

#### 4.1.5 小练习与答案

**练习 1**：为什么压缩文件要「逐 record 解压 + 重写 nextrec」，而不是对整个文件做一次 `zlib.decompress`？
**答案**：因为每个 record 的头部记录着「下一个 record 的**绝对字节偏移**」。整文件解压后体积改变，原始偏移全部失效；只有逐 record 解压并按输出文件的实际位置重新计算 `nextrec`，读取流程才能继续按偏移寻址。

**练习 2**：如果把一个 `.sav` 文件的前两个字节从 `SR` 改成 `XY`，`readsav` 会怎样？
**答案**：第 758–759 行的签名校验失败，抛出 `Exception("Invalid SIGNATURE: b'XY'")`，整个读取立即中止。

---

### 4.2 record 解析与类型 / 数组 / 结构描述符

#### 4.2.1 概念说明

record 流里的每条 record 都由 `_read_record` 负责解析。每个 record 的**公共头部**是固定的：

| 字段 | 字节数 | 含义 |
| --- | --- | --- |
| `rectype` | 4 | record 类型码，查 `RECTYPE_DICT` 得到名字 |
| `nextrec` | 8 | 下一个 record 的绝对偏移（两个 uint32 拼成 int64） |
| 未知 | 4 | 跳过的 4 字节 |

头部之后是**类型专属的正文**。`RECTYPE_DICT` 把类型码代码化（[scipy/io/_idl.py:L56-L69](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L56-L69)），其中和数据相关的只有两类：

- `VARIABLE`（码 2）：一个命名变量，正文是变量名 + 类型描述符 + 数据。
- `HEAP_DATA`（码 16）：堆里的一份匿名数据，正文是堆索引 + 类型描述符 + 数据。

其余 record 携带元信息：`TIMESTAMP`（时间戳）、`VERSION`（格式版本 / 架构 / 操作系统 / IDL 版本）、`IDENTIFICATION`（作者 / 标题 / idcode）、`NOTICE` / `DESCRIPTION`（声明与描述）、`END_MARKER`（结束）。

`VARIABLE` / `HEAP_DATA` 的正文核心是一个**类型描述符（typedesc）**，它用**位标志**告诉读取器「这个变量是标量、数组还是结构」：

| 标志 | 位 | 含义 |
| --- | --- | --- |
| `varflags & 2` | bit1 | 系统变量（scipy 未实现，直接报错） |
| `array` | `varflags & 4`（bit2） | 这是一个数组 |
| `structure` | `varflags & 32`（bit5） | 这是一个结构 |

如果是数组或结构，typedesc 还会附带**数组描述符（arraydesc）**和**结构描述符（structdesc）**，分别描述维度与字段。

#### 4.2.2 核心流程

`_read_record` 的分派逻辑（[scipy/io/_idl.py:L315-L420](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L315-L420)）：

```text
_read_record(f)
  ├─ 读公共头部：rectype / nextrec / 跳过 4 字节
  ├─ 把 rectype 码翻译成名字（查 RECTYPE_DICT）
  ├─ 按 rectype 名字分派：
  │     VARIABLE    → 读 varname；读 typedesc；按 typecode/标志读 data
  │     HEAP_DATA   → 读 heap_index；读 typedesc；按 typecode/标志读 data
  │     TIMESTAMP   → 跳过 4*256 字节；读 date / user / host
  │     VERSION     → 读 format / arch / os / release
  │     IDENTIFICATION → 读 author / title / idcode
  │     END_MARKER  → 置 record['end'] = True
  │     ...（NOTICE / DESCRIPTION / HEAP_HEADER 等）
  └─ f.seek(nextrec)        # 关键：跳到下一个 record，而非顺序读
```

`VARIABLE` / `HEAP_DATA` 正文里读 data 的分派又分三层（[scipy/io/_idl.py:L340-L361](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L340-L361)）：

```text
typecode == 0 且 nextrec == f.tell() → data = None      # NULL / 未定义变量
否则：
  读 VARSTART（必须等于魔数 7）
  if typedesc['structure']: data = _read_structure(...)   # 结构
  elif typedesc['array']:   data = _read_array(...)       # 数组
  else:                     data = _read_data(typecode)   # 标量
```

描述符自身的解析层层递归：

```text
_read_typedesc → typecode, varflags（位标志）
                 ├─ structure? → _read_arraydesc + _read_structdesc
                 └─ array?     → _read_arraydesc
_read_structdesc → 结构名、标签数、各标签 _read_tagdesc
                   ├─ 标签又是 array?   → _read_arraydesc
                   └─ 标签又是 structure? → 递归 _read_structdesc
```

这种「描述符里嵌描述符」的递归正是 IDL 结构体能表达任意嵌套（结构里套数组、数组里套结构）的原因。

#### 4.2.3 源码精读

record 公共头部读取与 64 位 `nextrec` 拼装：

[scipy/io/_idl.py:L318-L323](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L318-L323)

第 321 行 `nextrec += _read_uint32(f).astype(np.int64) * 2**32` 正是上文 \( \text{low} + \text{high}\times 2^{32} \) 公式的落地。结尾第 418 行 `f.seek(nextrec)` 实现了「按偏移跳到下一个 record」。

类型描述符 `_read_typedesc`——位标志解析的核心：

[scipy/io/_idl.py:L423-L440](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L423-L440)

`varflags & 4 == 4` 判断数组位、`varflags & 32 == 32` 判断结构位。注意位运算的优先级：`&` 的优先级低于 `==`，所以写法是 `varflags & 32 == 32`（等价于 `varflags & (32 == 32)` 即 `varflags & True`）——这其实是**依赖 Python 里 `True==1`** 的隐式写法，实际效果是「bit5 是否为 1」。这是一个值得留意的小细节。

数组描述符 `_read_arraydesc` 支持两种起始码：`8`（32 位）与 `18`（64 位，实验性）：

[scipy/io/_idl.py:L443-L486](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L443-L486)

它读出 `nbytes`（总字节数）、`nelements`（元素个数）、`ndims`（维数）、`dims`（各维长度列表）。64 位分支会发一条 `Using experimental 64-bit array read` 警告，说明这部分尚未广泛验证。

结构描述符 `_read_structdesc`——开头必须是魔数 9，之后读结构名、标签数、字节数，以及 `predef` 位标志（预定义 / 继承 / 超类）：

[scipy/io/_idl.py:L489-L540](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L489-L540)

两个关键点：第一，第 531 行 `STRUCT_DICT[structdesc['name']] = structdesc` 把首次遇到的完整结构定义**缓存**到模块级 `STRUCT_DICT`（[scipy/io/_idl.py:L72-L72](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L72-L72)）；当后续遇到 `predef=1`（预定义引用）时，直接从缓存取回（第 533–538 行），不必重复读取字段定义。第二，`inherits` / `is_super` 分支处理 IDL 的对象继承（`struct_inherit.sav` 测试覆盖）。

标签描述符 `_read_tagdesc`——同样用位标志标记标签是数组还是结构：

[scipy/io/_idl.py:L543-L559](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L543-L559)

> 一个阅读型观察：`_read_record` 第 396 行处理 `"COMMONBLOCK"`，但 `RECTYPE_DICT` 里码 1 对应的是 `"COMMON_VARIABLE"`（没有 `"COMMONBLOCK"` 这个名字）。这意味着现代 `.sav` 里即使出现公共块相关 record，也会落到第 414 行的 `else` 分支抛 `not implemented`。实践中极少遇到，测试套件也无覆盖——这是阅读源码时值得留意的一处「理论与实现的缝隙」。

#### 4.2.4 代码实践

**实践目标**：用 Python 手动读 `.sav` 文件的前若干字节，亲眼看到 `SR` 签名、recfmt、以及第一条 record 的 `rectype`，把抽象的「头部」落到具体字节。

```python
# 示例代码
import struct
from os.path import dirname, join as pjoin
import scipy.io as sio

data_dir = pjoin(dirname(sio.__file__), 'tests', 'data')
with open(pjoin(data_dir, 'scalar_float32.sav'), 'rb') as f:
    sig = f.read(2)               # 签名
    recfmt = f.read(2)            # record 格式码
    rectype = struct.unpack('>l', f.read(4))[0]   # 第一条 record 的类型码
    nextrec_lo = struct.unpack('>I', f.read(4))[0]
    nextrec_hi = struct.unpack('>I', f.read(4))[0]
    nextrec = nextrec_lo + nextrec_hi * 2**32

print("signature:", sig)          # b'SR'
print("recfmt   :", recfmt)       # b'\x00\x04'（普通）
print("rectype  :", rectype, "→ 通常 10 = TIMESTAMP")
print("nextrec  :", nextrec)
```

**操作步骤**：以二进制方式打开文件，按格式规格手工解析前 14 字节。

**需要观察的现象**：签名是 `b'SR'`，recfmt 是 `b'\x00\x04'`，第一条 record 的类型码通常是 `10`（`TIMESTAMP`，IDL 写文件时总会先写时间戳）。

**预期结果**：`nextrec` 是一个正整数（指向第二条 record 的起始字节），且 `nextrec_hi` 几乎总是 `0`（小文件用不到高 32 位）。

#### 4.2.5 小练习与答案

**练习 1**：`VARSTART` 必须等于几？为什么要校验它？
**答案**：必须等于 `7`（[scipy/io/_idl.py:L349-L351](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L349-L351)）。它是一个固定魔数，用来确认「这里确实开始变量数据」、读取位置没有错位；若不等于 7 说明格式解析已经偏离，应立即报错而非继续读出垃圾数据。

**练习 2**：`typecode == 0` 在什么情况下表示「NULL 变量」，什么情况下报错？
**答案**：当 `typecode == 0` **且** `nextrec == f.tell()`（即 record 正文已无剩余字节）时，判定为 NULL，`data = None`；若 `typecode == 0` 但后面还有字节，则抛 `ValueError("Unexpected type code: 0")`（[scipy/io/_idl.py:L340-L345](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L340-L345)）。

---

### 4.3 标量、数组与结构体的数据重建

#### 4.3.1 概念说明

描述符只告诉读取器「这是什么形状、什么类型」，真正把字节变成 Python / NumPy 对象的是三个函数：

- `_read_data(f, dtype)`：读**标量**（或作为数组 / 结构的基本元素）。它按 `DTYPE_DICT` 里的 typecode 分派，例如 typecode 4 是 `float32`、5 是 `float64`、6 是 `complex64`、9 是 `complex128`。
- `_read_array(f, typecode, array_desc)`：读**数组**。对紧凑数值类型用 `np.frombuffer` 一次性读入，对 16 位类型有特殊处理，对指针 / 字符串等异质类型则逐元素调用 `_read_data`。
- `_read_structure(f, array_desc, struct_desc)`：读**结构体**，产出一个 NumPy `recarray`（记录数组），每个字段对应结构的一个标签。

`DTYPE_DICT` 把 IDL 的 15 种类型码映射到 NumPy dtype（[scipy/io/_idl.py:L39-L53](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L39-L53)），全部带 `>`（大端）前缀，typecode 7/8/10/11 映射到 `|O`（object）——这些是字符串、指针等需要在 Python 层重建的类型。

#### 4.3.2 核心流程

`_read_array` 对不同 typecode 走三条不同路径（[scipy/io/_idl.py:L267-L312](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L267-L312)）：

```text
_read_array(f, typecode, array_desc)
  ├─ typecode ∈ {1,3,4,5,6,9,13,14,15}（紧凑数值）
  │     └─ np.frombuffer(f.read(nbytes), dtype=DTYPE_DICT[typecode])   # 一次性读
  ├─ typecode ∈ {2,12}（int16/uint16，非紧凑）
  │     └─ np.frombuffer(f.read(nbytes*2), dtype=...)[1::2]            # 每个值占 4 字节
  └─ 其它（指针 / 字符串 / 对象）
        └─ 逐元素 _read_data → np.array(..., dtype=object)
  最后：若 ndims>1，dims.reverse() 后 reshape（列主序 → 行主序）
```

**16 位类型的非紧凑存储**是最容易踩坑的细节：IDL 把每个 16 位整数放在一个 4 字节对齐的槽里（值在后 2 字节），所以读取 `nbytes*2` 字节、按 int16 切片后取 `[1::2]`（每个 4 字节槽的第二段）。

`_read_structure` 的流程（[scipy/io/_idl.py:L223-L264](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L223-L264)）：

```text
_read_structure(f, array_desc, struct_desc)
  ├─ nrows = array_desc['nelements']           # 结构体被「复制」了几份
  ├─ columns = struct_desc['tagtable']         # 字段定义列表
  ├─ 按字段构造 numpy recarray 的 dtype
  │     ├─ 字段是 structure/array → object dtype（异质）
  │     └─ 否则按 typecode 取 DTYPE_DICT
  ├─ 双重循环 nrows × columns：
  │     ├─ 字段是 structure → 递归 _read_structure
  │     ├─ 字段是 array     → _read_array
  │     └─ 否则             → _read_data
  └─ 若 ndims>1，reshape
```

> 注意：`_read_structure` 的产出是 **NumPy `recarray`**，而不是 Python `dict`。`recarray` 支持类似命名空间的字段访问（`s.scalars.a`），所以在使用体感上接近「嵌套 dict」，但底层是结构化数组。这也是为什么 `test_scalars` 里用 `s.scalars.a` 这种属性写法访问字段。

#### 4.3.3 源码精读

`_read_data` 按 typecode 分派读标量。注意几个特殊 typecode：1（byte）前要先读一个等于 1 的 int32 校验；7（字符串）走 `_read_string_data`；10 / 11 生成指针占位符：

[scipy/io/_idl.py:L181-L220](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L181-L220)

第 207–210 行：typecode 10 生成 `Pointer(_read_int32(f))`、typecode 11 生成 `ObjectPointer(_read_int32(f))`——读出的只是一个**堆索引占位符**，真正的数据要等 `_replace_heap` 第二趟替换（见 4.4 节）。

`_read_array` 的三条路径：

[scipy/io/_idl.py:L273-L301](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L273-L301)

第 289–290 行就是 16 位类型的「读 2 倍字节、取奇数下标」技巧。末尾第 304–307 行 `dims.reverse()` 后 `reshape`，正是列主序 → 行主序的转换。

`_read_structure` 构造 recarray dtype 的关键写法：

[scipy/io/_idl.py:L232-L256](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L232-L256)

第 235 与 238 行的 `((col['name'].lower(), col['name']), np.object_)` 是 NumPy 结构化 dtype 的「带标题字段」写法：第一个串是 **title**（小写名），第二个是 **name**（原始名）。这样同一个字段既可以用原始大小写访问，也可以用小写访问，配合 `AttrDict` 的大小写不敏感，让 IDL 的变量名与字段名大小写不敏感特性贯穿到底。

> 字符串相关的两个原子函数也值得一看：`_read_string`（[scipy/io/_idl.py:L158-L166](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L158-L166)）读「长度 + 内容 + 对齐」，用 `latin1` 解码；`_read_string_data`（[scipy/io/_idl.py:L169-L178](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L169-L178)）的长度被**写了两次**，用于 `DESCRIPTION` 等长文本字段。对齐由 `_align_32`（[scipy/io/_idl.py:L75-L81](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L75-L81)）完成——把文件位置 rounding 到 4 的倍数：

\[ \text{aligned}(p) = \begin{cases} p & p \bmod 4 = 0 \\ p + 4 - (p \bmod 4) & \text{otherwise} \end{cases} \]

#### 4.3.4 代码实践

**实践目标**：读取一个含结构体的 `.sav`，检查 `_read_structure` 产出的 `recarray` 的 dtype 与 shape，验证「结构体被重建为结构化数组」。

```python
# 示例代码
from os.path import dirname, join as pjoin
import scipy.io as sio
from scipy.io import readsav

data_dir = pjoin(dirname(sio.__file__), 'tests', 'data')
s = readsav(pjoin(data_dir, 'struct_scalars.sav'), verbose=False)

print("type(s.scalars) =", type(s.scalars).__name__)   # recarray
print("dtype           =", s.scalars.dtype)
print("字段名          =", s.scalars.dtype.names)
print("shape           =", s.scalars.shape)
print("a,b,c,d =", s.scalars.a, s.scalars.b, s.scalars.c, s.scalars.d)
```

**操作步骤**：读取 `struct_scalars.sav`（含一个有 6 个标量字段 a–f 的结构体），打印其类型、dtype、字段名、形状与若干字段值。

**需要观察的现象**：`type` 是 `recarray`（来自 `np.rec.recarray`）；`dtype.names` 形如 `('a','b','c','d','e','f')`，每个字段有独立的 dtype（int16 / int32 / float32 / float64 / object / complex64）；`shape` 是 `(1,)`（结构体复制了 1 份）。

**预期结果**：`s.scalars.a` 是 `array(1, dtype=int16)`、`s.scalars.b` 是 `array(2, dtype=int32)`、`s.scalars.c` 是 `array(3., dtype=float32)`（与 `test_scalars` 断言一致）。结构体并非 dict，而是字段可作属性访问的 `recarray`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 int16 / uint16 数组要「读 2 倍字节再取 `[1::2]`」，而 int32 / float32 不用？
**答案**：IDL 对 16 位类型不做紧凑存储，每个 16 位值占用一个 4 字节对齐的槽（值在后 2 字节）。而 int32 / float32 等本就是 4 字节类型，天然 4 字节对齐、紧凑排列，可以直接 `np.frombuffer` 一次读入。

**练习 2**：读取 `array_float32_2d.sav` 后，`s.array2d.shape` 应该是多少？IDL 里存的维度顺序与它有什么关系？
**答案**：`shape` 是 `(22, 12)`（见 `test_2d`）。IDL 按列主序存维度（最慢变化的维在前），`_read_array` 末尾 `dims.reverse()` 后再 `reshape`，把 `(12, 22)` 反转成 NumPy 的行主序形状 `(22, 12)`。

---

### 4.4 指针、堆与 AttrDict

#### 4.4.1 概念说明

IDL 的指针让一个变量「引用」堆里的数据。在 `.sav` 文件里，**变量 record** 存的是一个 `Pointer(index)` 占位符（typecode 10）或 `ObjectPointer(index)`（typecode 11），而真正的数据放在单独的 **`HEAP_DATA` record** 里，用 `heap_index` 标识。读取分两趟：

1. **收集堆**：把所有 `HEAP_DATA` record 装进字典 `heap = {heap_index: data}`。
2. **替换指针**：遍历每个变量，用 `_replace_heap` 把 `Pointer` 占位符换成 `heap[index]`。

这个设计有两个直接后果：

- **共享同一对象**：两个指针引用同一个 `heap_index`，替换后会指向字典里的**同一个** Python 对象——`test_pointers` 正是用 `s.c64_pointer1 is s.c64_pointer2` 验证这一点。
- **空指针与坏指针**：`index == 0` 表示空指针，替换成 `None`；若 `index` 不在 `heap` 里（损坏文件，gh-4613），发一条警告后也置 `None`。

读取完成后，所有变量装进一个容器。默认（`python_dict=False`）是 `AttrDict`——一个**大小写不敏感**、且同时支持**下标 / 属性 / 调用**三种访问方式的字典；`python_dict=True` 时则返回普通 `dict`（键全部小写）。

#### 4.4.2 核心流程

`_replace_heap` 是一个返回 `(replace, new)` 二元组的递归函数（[scipy/io/_idl.py:L562-L626](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L562-L626)）：

```text
_replace_heap(variable, heap)
  ├─ variable 是 Pointer
  │     ├─ while 仍是 Pointer：index==0 → None；index∈heap → 跟进；否则 warn + None
  │     ├─ 对解出的结果再递归一次（堆数据本身可能含指针）
  │     └─ return True, variable          # 顶层需替换
  ├─ variable 是 recarray / record
  │     └─ 逐元素递归，就地赋值；return False, variable
  ├─ variable 是 object ndarray
  │     └─ 逐元素递归，就地赋值；return False, variable
  └─ 其它 → return False, variable         # 标量无需处理
```

返回值的含义：`replace=True` 表示「调用者应该用 `new` 覆盖原变量」（用于 `readsav` 里 VARIABLE record 的顶层替换，[scipy/io/_idl.py:L851-L856](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L851-L856)）；`replace=False` 表示「已经就地改好了，无需覆盖」（用于数组 / 结构体内部的指针）。

`AttrDict` 的三种访问方式（[scipy/io/_idl.py:L629-L674](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L629-L674)）：

| 写法 | 等价于 | 说明 |
| --- | --- | --- |
| `d['Var']` | `d['var']` | 下标访问，键自动小写 |
| `d.Var` / `d.var` | `d['var']` | 属性访问，经 `__getattr__` |
| `d('VAR')` | `d['var']` | 调用访问，`__call__ = __getitem__` |
| `d.X = 5` | `d['x'] = 5` | 属性赋值，`__setattr__ = __setitem__` |

#### 4.4.3 源码精读

两个指针占位类——`Pointer` 与 `ObjectPointer` 都只持有一个 `index`：

[scipy/io/_idl.py:L145-L155](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L145-L155)

`_replace_heap` 的指针分支——`while` 循环跟进「指针的指针」，`index == 0` 为空指针，越界索引发警告：

[scipy/io/_idl.py:L564-L584](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L564-L584)

第 579 行对跟进后的结果再递归一次，是为了处理「堆数据本身又含指针」的情况（例如一个元素全是指针的数组）。recarray / record / object ndarray 的就地替换分支：

[scipy/io/_idl.py:L586-L622](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L586-L622)

`AttrDict` 的全部魔法——四个 dunder 方法互相委托：

[scipy/io/_idl.py:L660-L674](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L660-L674)

`__getitem__` / `__setitem__` 把键 `.lower()` 后再委托给 `dict`，实现大小写不敏感；`__getattr__` 把 `KeyError` 翻译成 `AttributeError`（这样 `d.missing` 抛的是属性错误，符合 Python 直觉）；`__setattr__ = __setitem__` 与 `__call__ = __getitem__` 用赋值复用方法，是 Python 里常见的「别名」写法。

> docstring 里的 doctest（[scipy/io/_idl.py:L630-L653](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L630-L653)）完整展示了 `AttrDict` 的三种访问与两种缺失异常，是理解它最快的入口；`test_idl.py` 的 `test_attrdict`（[scipy/io/tests/test_idl.py:L475-L482](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/tests/test_idl.py#L475-L482)）也直接测了它。

#### 4.4.4 代码实践

**实践目标**：读取 `scalar_heap_pointer.sav`，验证两个指针引用了**同一个** Python 对象；再用 `python_dict=True / False` 对比返回容器的差异。

```python
# 示例代码
from os.path import dirname, join as pjoin
import scipy.io as sio
from scipy.io import readsav

data_dir = pjoin(dirname(sio.__file__), 'tests', 'data')

# (1) 指针共享：两个变量指向同一份堆数据
s = readsav(pjoin(data_dir, 'scalar_heap_pointer.sav'), verbose=False)
print("pointer1 is pointer2 ?", s.c64_pointer1 is s.c64_pointer2)   # True

# (2) AttrDict 大小写不敏感 + 三种访问
print(s.c64_pointer1)          # 属性访问
print(s['C64_POINTER1'])       # 下标访问，大写也能命中
print(s('c64_pointer1'))       # 调用访问

# (3) python_dict=True 返回普通 dict
d = readsav(pjoin(data_dir, 'scalar_heap_pointer.sav'),
            python_dict=True, verbose=False)
print("type:", type(d).__name__)   # dict
print("keys:", list(d.keys()))     # 全小写键
```

**操作步骤**：先用默认的 `AttrDict` 读取，做指针身份（`is`）与三种访问测试；再用 `python_dict=True` 读取，对比返回类型与键的大小写。

**需要观察的现象**：`is` 比较为 `True`——两个指针替换后是同一个 `np.complex128` 对象；大小写不同的访问都能命中同一个值；`python_dict=True` 时返回 `dict`，且属性访问（`d.c64_pointer1`）会失败（普通 dict 没有这个属性）。

**预期结果**：`pointer1 is pointer2` 打印 `True`（与 `test_pointers` 一致）；`type` 在 `python_dict=True` 时为 `dict`。

#### 4.4.5 小练习与答案

**练习 1**：`null_pointer.sav` 里有个空指针变量 `point`，读取后它的值是什么？为什么？
**答案**：值是 `None`。空指针的 `index == 0`，`_replace_heap` 在第 568–569 行把 `index == 0` 的指针直接替换成 `None`（见 `test_null_pointer`，断言 `s.point is None`）。

**练习 2**：`AttrDict` 里 `d.missing`（不存在的属性）抛什么异常？为什么不是 `KeyError`？
**答案**：抛 `AttributeError`。因为 `__getattr__`（[scipy/io/_idl.py:L666-L671](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L666-L671)）把底层 `__getitem__` 抛出的 `KeyError` 捕获后重新抛成 `AttributeError`，这样「访问不存在的属性」就符合 Python 的一般直觉（属性缺失应是 `AttributeError`）。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成一次「端到端」的 `.sav` 读取巡览，覆盖普通文件、结构体、指针共享与压缩文件。

**任务**：

1. 用默认的 `AttrDict` 读取 `struct_arrays.sav`，列出变量、打印结构体字段的 dtype，并用属性访问取出某个数组字段，验证 `_read_structure` 重建出 `recarray`。
2. 读取 `scalar_heap_pointer.sav`，验证两个指针变量 `is` 同一个对象，说明这是 `_replace_heap` 用堆字典实现的共享。
3. 读取压缩文件 `various_compressed.sav`，说明它先被逐 record 解压到临时文件、再走普通流程，并检查其中一个 5 维数组的形状。
4. 用 `python_dict=True` 重新读取第 1 步的文件，对比返回类型与「能否用属性访问字段」。

```python
# 示例代码（综合实践骨架）
from os.path import dirname, join as pjoin
import scipy.io as sio
from scipy.io import readsav

data_dir = pjoin(dirname(sio.__file__), 'tests', 'data')

# (1) 结构体 → recarray
s = readsav(pjoin(data_dir, 'struct_arrays.sav'), verbose=False)
print("变量:", list(s.keys()))
print("arrays 类型:", type(s.arrays).__name__)        # recarray
print("字段:", s.arrays.dtype.names)                  # ('a','b','c','d')
print("arrays.a[0]:", s.arrays.a[0])                  # array([1,2,3], int16)
print("arrays.c[0]:", s.arrays.c[0])                  # 复数数组

# (2) 指针共享
p = readsav(pjoin(data_dir, 'scalar_heap_pointer.sav'), verbose=False)
print("共享:", p.c64_pointer1 is p.c64_pointer2)      # True

# (3) 压缩文件
c = readsav(pjoin(data_dir, 'various_compressed.sav'), verbose=False)
print("array5d.shape:", c.array5d.shape)              # (4, 3, 4, 6, 5)

# (4) python_dict=True 对比
d = readsav(pjoin(data_dir, 'struct_arrays.sav'), python_dict=True, verbose=False)
print("返回类型:", type(d).__name__)                  # dict
try:
    d.arrays                                         # 普通 dict 无属性访问
except AttributeError as e:
    print("属性访问失败（预期）:", e)
print("改用下标:", d['arrays'].a[0])
```

**需要观察的现象**：

- 第 1 步：`s.arrays` 是 `recarray`，字段 `a` 是 int16 数组、`c` 是 complex 数组、`d` 是 object（字符串）数组——同一个结构体的不同字段可以有完全不同的类型，这正是 `_read_structure` 为每个字段独立查 `DTYPE_DICT` 的结果。
- 第 2 步：`is` 比较为 `True`，说明 `c64_pointer1` 与 `c64_pointer2` 在 `_replace_heap` 后引用了堆字典里的同一个对象。
- 第 3 步：压缩文件能像普通文件一样读取，`array5d.shape` 为 `(4, 3, 4, 6, 5)`（见 `test_compressed`），证明逐 record 解压 + 重写 nextrec 对调用者完全透明。
- 第 4 步：`python_dict=True` 返回 `dict`，属性访问 `d.arrays` 抛 `AttributeError`，必须改用下标 `d['arrays']`。

**预期结果**：以上四步全部符合 `test_idl.py` 里 `test_arrays` / `test_pointers` / `test_compressed` 的断言。各字段的精确数值请以本地运行为准（数据文件本身随 SciPy 版本可能微调）。

**源码阅读型延伸**：用十六进制查看器（如 `xxd`）打开 `struct_arrays.sav`，找到结构描述符里的 `STRUCTSTART`（魔数 9）与各 `tagdesc`，对照 4.2 节的描述符布局，手工定位「字段名 a 出现在哪个字节」。这部分无法在 Python 层直接观察，属于加深理解的选做项。

## 6. 本讲小结

- IDL `.sav` 是一条 **record 流**：文件以 `SR` 签名 + 2 字节 recfmt 开头，之后每个 record 自带「下一个 record 的 64 位绝对偏移」，读取靠 `f.seek(nextrec)` 跳转而非顺序推进；压缩文件（recfmt `b'\x00\x06'`）被逐 record 解压到临时文件并重写偏移，再走普通流程。
- `readsav` 是唯一公共入口（顶层 re-export 于 `__init__.py` 第 111 行），主流程是「读 record 列表 → 收集 HEAP_DATA 成堆字典 → 对每个 VARIABLE 调 `_replace_heap` 解析指针 → 装进 `AttrDict` / dict」。
- record 正文里的**类型描述符**用位标志区分标量 / 数组 / 结构（`varflags & 4` 为数组、`& 32` 为结构），并递归地带出 arraydesc / structdesc / tagdesc，从而支持任意嵌套的结构体；`typecode == 0` 且无剩余字节表示 NULL 变量。
- `_read_data` / `_read_array` / `_read_structure` 把字节重建为 numpy 对象：紧凑数值用 `np.frombuffer`，**16 位类型非紧凑存储**（读 2 倍字节取 `[1::2]`），多维数组用 `dims.reverse()` 把列主序转成行主序；结构体产出的是 **`recarray`**（非 dict），字段名带小写 title 以支持大小写不敏感访问。
- 指针机制是**两趟设计**：`Pointer` / `ObjectPointer` 只存堆索引占位符，`HEAP_DATA` record 存真实数据；`_replace_heap` 用堆字典替换占位符，因而两个指向同一索引的指针会变成**同一个** Python 对象（`is` 为真），空指针（index 0）→ `None`，坏索引 → 警告 + `None`。
- `AttrDict` 是大小写不敏感字典，键全部小写化存储，`__getitem__` / `__getattr__` / `__call__` / `__setattr__` 四个 dunder 互相委托，实现「下标 / 属性 / 调用 / 赋值」四种统一访问；`python_dict=True` 时改返回普通 `dict`。

## 7. 下一步学习建议

- **横向对比列主序处理**：回到 u2-l2（Fortran `FortranFile`）与 u2-l5（NetCDF3），对照三者在「列主序 → 行主序」上的处理方式（`.T` / `order='F'` / `dims.reverse()+reshape`），巩固对 Fortran order 的直觉。
- **跟进弃用机制**：本讲的 `readsav` 经顶层 `scipy.io.__init__.py` re-export，而 `scipy.io.idl` 是无前缀的弃用包装模块。这一套 `__getattr__` + `_sub_module_deprecation` 的懒转发机制将在 u4-l2（弃用命名空间）集中讲解。
- **健壮性与异常处理**：本讲提到的「坏指针 → 警告 + None」「NULL 变量」「16 位非紧凑存储」都是健壮性设计。u4-l4（健壮性、错误处理与安全性取舍）会横切对比 scipy.io 各格式的异常体系与损坏文件处理，建议对照阅读。
- **继续阅读源码**：若你想检验对本讲的理解，可以尝试回答——为什么 `_replace_heap` 要返回 `(replace, new)` 二元组而不是直接就地修改？提示：顶层标量指针无法「就地」替换，而数组 / 结构体内部的指针可以。带着这个问题重读 [scipy/io/_idl.py:L562-L626](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_idl.py#L562-L626) 与 `readsav` 的第 851–856 行。
