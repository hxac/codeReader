# MAT v4 读写（_mio4.py）

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 MATLAB v4 `.mat` 文件的二进制布局：每个变量 = 一个 20 字节定长头 + 变量名 + 列主序数据。
- 用十进制分解 `MOPT` 这个头字段，解释它如何同时编码「字节序 / 数据类型 / 矩阵类」三件事。
- 读懂 `VarReader4` 如何按 `mclass`（full / char / sparse）分支重建 numpy 数组，以及复数矩阵「实部块 + 虚部块」的非交错存储。
- 读懂 `MatFile4Reader` 的「猜字节序 → 套模板 → 顺序遍历变量」三步流程。
- 理解写入侧 `VarWriter4` / `MatFile4Writer` 如何从 numpy 数组反推 v4 头，以及 `oned_as` 如何决定 1-D 数组存成行向量还是列向量。

本讲只讲 **v4** 一种格式，且不涉及 Cython 底层（那是 u3-l7 的内容），全部逻辑都在纯 Python 文件 [`matlab/_mio4.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio4.py) 里。

## 2. 前置知识

在进入 v4 之前，请确认你已掌握以下概念（这些在 u3-l1、u3-l2、u3-l3 已建立，这里只做一句话回顾）：

- **`.mat` 是一族格式**：v4、v5/v7、v7.3（HDF5）。`_get_matfile_version` 用文件头判断属于哪一种，本讲只处理判定结果为 `(0, 0)` 的 v4。
- **工厂分发**：[`mat_reader_factory`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio.py#L66-L77) 在主版本号为 0 时返回 `MatFile4Reader`，为 1 时返回 `MatFile5Reader`。
- **基类 `MatFileReader`**（在 `_miobase.py`）集中存放 `mat_stream`、`byte_order`、`squeeze_me`、`chars_as_strings` 等文件级读取选项，并提供抽象钩子 `guess_byte_order` 让子类覆写。
- **列主序（Fortran order）**：MATLAB 与 Fortran 一样按列存储，numpy 默认按行存储。v4 数据块要用 `order='F'` 读、用 `.tobytes(order='F')` 写，否则矩阵会「转置」。
- **`docfiller`**：用 `%(load_args)s` 这类占位符在函数定义时展开共享的参数文档（u3-l2）。

另外，几个 numpy 小知识会在源码里反复出现：

- `np.ndarray(shape=..., dtype=..., buffer=raw_bytes, order='F')`：直接把一段字节包装成数组，零拷贝。
- `dtype.str` 形如 `'<f8'`：第 1 个字符是字节序（`<` 小端 / `>` 大端 / `|` 无所谓），其后是类型码与字节数。所以 `dtype.str[1:]` 能剥掉字节序，得到与平台无关的类型串（如 `'f8'`）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`matlab/_mio4.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio4.py) | 本讲主角。v4 的读写器、头常量、类型映射全部在此。 |
| [`matlab/_miobase.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_miobase.py) | 提供基类 `MatFileReader`、工具函数 `convert_dtypes` / `read_dtype` / `matdims` / `arr_to_chars` / `arr_dtype_number`。 |
| [`matlab/_mio_utils.pyx`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio_utils.pyx) | Cython 工具 `squeeze_element` / `chars_to_strings`，被读路径调用做后处理。 |
| [`matlab/_mio.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio.py) | `savemat` 在 `format='4'` 时构造 `MatFile4Writer`；`loadmat` 经工厂拿到 `MatFile4Reader`。 |

## 4. 核心概念与源码讲解

### 4.1 MAT v4 文件结构与 MOPT 头编码

#### 4.1.1 概念说明

v4 是 `.mat` 三个版本里**最简单**的一种。它没有 v5 那个 128 字节的全局头，也没有 v7.3 的 HDF5 容器——整个文件就是「一个变量接一个变量」顺序拼接，每个变量的布局是：

```
┌──────────────────────┐
│  20 字节定长头 header │   ← mopt, mrows, ncols, imagf, namlen（各 4 字节 int32）
├──────────────────────┤
│  namlen 字节的变量名  │   ← 以 '\0' 结尾、不足补 '\0'
├──────────────────────┤
│  数据块（列主序）      │   ← 复数：实部块紧跟虚部块
└──────────────────────┘
```

理解 v4 的关键是**头里的 `mopt` 字段**：它是一个十进制整数，但每一位十进制数字都各司其职。MATLAB 4 的设计者把「字节序 / 保留位 / 数据类型 / 矩阵类」这四件事，压缩进了一个 4 字节整数里。

#### 4.1.2 核心流程

`mopt` 的十进制分解：

\[ \text{MOPT} = M\times 1000 + O\times 100 + P\times 10 + T \]

| 位 | 含义 | 取值 |
| --- | --- | --- |
| `M`（千位） | 字节序 / 机器表示 | 0=小端 IEEE，1=大端 IEEE，2=VAX D-float，3=VAX G-float，4=Cray |
| `O`（百位） | 保留位 | 必须为 0 |
| `P`（十位） | 数据类型码 | 0=double, 1=single, 2=int32, 3=int16, 4=uint16, 5=uint8 |
| `T`（个位） | 矩阵类码 | 0=full, 1=char, 2=sparse |

所以一个「小端、双精度、满矩阵」的变量，`MOPT = 0×1000+0×100+0×10+0 = 0`，写到 4 字节里就是 `00 00 00 00`。

> 💡 **回到 u3-l1**：还记得 `_get_matfile_version` 是怎么认出 v4 的吗？它读前 4 字节，发现「里面有 0 字节」就判定为 v4。原因正是这里——最常见的 v4 变量 `MOPT=0`，或任何 `MOPT` 取值较小（如 int32 的 `MOPT=20`）时，4 字节里都会出现至少一个 `0`。两个讲义在这里闭环。

#### 4.1.3 源码精读

数据类型码与 numpy dtype 的对应关系，用一张模板字典 `mdtypes_template` 代码化（`P` 作 key）：

[_mio4.py:37-50](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio4.py#L37-L50) —— 把 6 个类型码各自映射到一个 numpy dtype 字符串；特殊的 `'header'` 键直接定义了那 5 个 int32 字段的 structured dtype。

注意 `miDOUBLE=0`、`miSINGLE=1`……这些常量既是 `mdtypes_template` 的 key，也是 `MOPT` 解码出来的 `P` 值，两者共享同一套数字：

[_mio4.py:30-35](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio4.py#L30-L35) —— 6 个类型码常量。

矩阵类码 `mxFULL_CLASS=0` / `mxCHAR_CLASS=1` / `mxSPARSE_CLASS=2`：

[_mio4.py:66-83](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio4.py#L66-L83) —— 三个矩阵类常量，外加 `order_codes`（M 位到字节序名的映射）和 `mclass_info`（类码到可读名 `'double'/'char'/'sparse'`）。

写入侧还需要一张**反向表** `np_to_mtypes`：给定 numpy 类型串，反查它该用哪个 `P`：

[_mio4.py:52-64](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio4.py#L52-L64) —— 注意复数（`c8`/`c16`/`c32`）也映射到 `miDOUBLE`/`miSINGLE`，因为 v4 不用专门的「复数类型码」，而是靠 `imagf` 标志位区分；字符串 `S1` 映射到 `miUINT8`，因为 v4 字符就是按字节存的。

#### 4.1.4 代码实践

**目标**：把 MOPT 的「十进制分解」变成可手动验证的事实。

**步骤**：

1. 在 Python 里手算几个 MOPT 值对应的字节序列。
2. 预测它们的含义。

```python
# 示例代码（非项目源码）
import numpy as np

def decode_mopt(mopt):
    M, rest = divmod(mopt, 1000)
    O, rest = divmod(rest, 100)
    P, rest = divmod(rest, 10)
    T = rest
    return dict(M=M, O=O, P=P, T=T)

# 小端 double full 矩阵
print(decode_mopt(0))       # {'M':0,'O':0,'P':0,'T':0}  → LE, double, full
# 小端 int32 full 矩阵
print(decode_mopt(20))      # {'M':0,'O':0,'P':2,'T':0}  → LE, int32, full
# 大端 double sparse 矩阵
print(decode_mopt(1002))    # {'M':1,'O':0,'P':0,'T':2}  → BE, double, sparse

# MOPT=0 写成小端 int32 就是四个 0 字节，这正是 v4 的"指纹"
print(np.array(0, dtype='<i4').tobytes())   # b'\x00\x00\x00\x00'
```

**预期结果**：`MOPT=0` 的字节表示确实是 4 个零字节，呼应 u3-l1 的版本判定逻辑。

### 4.2 VarHeader4 与变量头解析

#### 4.2.1 概念说明

读一个 v4 变量分两步：先解析头，再按头里的信息读数据。`VarHeader4` 就是「头解析结果的容器」——一个纯数据对象，把从 20 字节头里提取出的 `name / dtype / mclass / dims / is_complex` 一次性打包，供后续读数据的函数使用。

`VarReader4` 则是真正干活的「变量读取器」，它的 `read_header` 方法负责把字节流开头那 20 字节 + 变量名，翻译成一个 `VarHeader4`。

#### 4.2.2 核心流程

`VarReader4.read_header` 的流程：

```
读 20 字节 header（用 self.dtypes['header'] 这个 structured dtype）
   ↓
读 namlen 字节作为变量名，strip 掉 '\0'
   ↓
校验 mopt ∈ [0, 5000]，否则报"字节序搞反了"
   ↓
divmod 逐位拆出 M / O / P / T
   ↓
dims = (mrows, ncols)；is_complex = (imagf == 1)；dtype = self.dtypes[P]
   ↓
返回 VarHeader4(name, dtype, T, dims, is_complex)
```

注意 `self.dtypes` 已经被套上了正确的字节序（在 4.4 节的 `initialize_read` 里完成），所以这里读出来的 `mopt`、`mrows` 等都是「人类可读」的正确数值。

#### 4.2.3 源码精读

`VarHeader4` 本身极简，且**写死** `is_logical=False, is_global=False`：

[_mio4.py:89-104](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio4.py#L89-L104) —— 注释 `# Mat4 variables never logical or global` 点明：v4 格式根本没有这两个标志位，这是 v4 比 v5「瘦」的体现之一。

`read_header` 的精华是 MOPT 拆解与校验：

[_mio4.py:117-141](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio4.py#L117-L141) —— 关键点：`mopt < 0 or mopt > 5000` 时抛 `ValueError`（提示可能是字节序问题，因为一个合理的 MOPT 最大也就 `4*1000+0*100+5*10+2 = 4052`）；`O != 0` 也抛错（保留位必须为 0）；`M not in (0, 1)` 时只发 `UserWarning` 而非报错——因为 VAX/Cray 这些古怪字节序 SciPy 读不动，但仍尝试返回数据。

#### 4.2.4 代码实践

**目标**：用项目自带的 v4 测试文件，验证 `read_header` 的拆解。

**步骤**：

```python
# 示例代码
import numpy as np
from scipy.io.matlab._mio4 import MatFile4Reader
import os

data_dir = os.path.join(os.path.dirname(__import__('scipy.io').__file__),
                        'matlab', 'tests', 'data')
# 这是一个 v4 文件（文件名里的 _4.2c 表示 MATLAB 4.2c 生成）
path = os.path.join(data_dir, 'testdouble_4.2c_SOL2.mat')

with open(path, 'rb') as f:
    reader = MatFile4Reader(f)
    reader.initialize_read()
    hdr = reader._matrix_reader.read_header()
    print('name =', hdr.name)
    print('dims =', hdr.dims)
    print('is_complex =', hdr.is_complex)
    print('mclass =', hdr.mclass)   # 0=full, 1=char, 2=sparse
```

**需要观察的现象**：`hdr.dims` 是 `(mrows, ncols)`，`hdr.mclass` 多半是 `0`（full 矩阵）。

**预期结果**：成功打印出变量名（如 `testdouble`）与其二维尺寸。

> 说明：直接调用内部 API 仅用于学习观察；正式读写请用 `loadmat` / `savemat`。

### 4.3 VarReader4：按类型重建 numpy 数组

#### 4.3.1 概念说明

拿到 `VarHeader4` 后，下一步是按 `mclass` 读取真正的数据块。v4 有三种矩阵类，读取策略各不相同：

- **full（数值满矩阵）**：最常见。按 `dims × itemsize` 读一块字节，包装成列主序数组；若是复数，再读一块虚部，拼成复数组。
- **char（字符矩阵）**：本质是按字节存的 latin-1 文本，读出后转成 `'U1'` 字符数组。
- **sparse（稀疏矩阵）**：v4 用一种特殊的「(N+1)×3 / ×4」三元组布局存稀疏矩阵，读完要还原成 `coo_array`。

#### 4.3.2 核心流程

`array_from_header` 是分支入口：

```
mclass == mxFULL_CLASS   → read_full_array
mclass == mxCHAR_CLASS   → read_char_array（+ 可选 chars_to_strings）
mclass == mxSPARSE_CLASS → read_sparse_array
最后：若 squeeze_me → squeeze_element 压缩长度为 1 的维度
```

最底层的数据搬运是 `read_sub_array`，它就是「读 N 字节 → 包成 order='F' 数组」：

```
num_bytes = ∏(dims) × dtype.itemsize     # reduce(mul, ...) 算总字节数
校验 num_bytes 不超过 _MAX_INTP（平台指针上限）
从流里 read(num_bytes)，校验读到的长度 == num_bytes（否则文件被截断）
np.ndarray(shape=dims, dtype=dt, buffer=buffer, order='F')
若 copy=True 则 .copy()（因为 buffer 通常来自 read 只读内存）
```

复数读取要注意**非交错存储**（v4/v5 都是这样，与 HDF5 的交错存储不同）：

```
实部块 = read_sub_array(hdr)      # dims 个元素
虚部块 = read_sub_array(hdr)      # 再 dims 个元素
结果   = 实部块 + 虚部块 * 1j
```

#### 4.3.3 源码精读

`array_from_header` 分发：

[_mio4.py:143-158](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio4.py#L143-L158) —— 注意 sparse 分支**直接 return**，跳过下面的 `squeeze_me` 后处理（注释说 squeeze 对稀疏没意义）。其它两类读完后才视情况 `squeeze_element`。

`read_sub_array` 是核心搬运工：

[_mio4.py:160-197](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio4.py#L160-L197) —— 用 `reduce(mul, hdr.dims, np.int64(dt.itemsize))` 算字节数，刻意用 `np.int64` 累乘以避免大数组（>2GB）溢出；读到长度不足时给出非常友好的错误信息，建议用户「先用 `whosmat` 列变量、再用 `variable_names` 指定加载」。

`read_full_array` 处理复数：

[_mio4.py:199-219](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio4.py#L199-L219) —— 复数时用 `copy=False` 连读两次避免多余拷贝，最后 `res + res_j*1j` 一次合成。

`read_char_array` 走 latin-1：

[_mio4.py:221-237](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio4.py#L221-L237) —— 先按 `hdr.dtype`（uint8）读字节，`.tobytes().decode('latin-1')`，再重新包成 `'U1'` 数组。如果上层 `chars_as_strings=True`，`array_from_header` 会再调 `chars_to_strings` 把最后一维拼成字符串。

`read_sparse_array` 最巧妙，v4 稀疏格式注释里讲得很细：

[_mio4.py:239-279](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio4.py#L239-L279) —— 数据存成 `(N+1)×3`（实）或 `(N+1)×4`（复）的二维数组：前 N 行是 `(行索引, 列索引, 值[, 虚部])`，最后一行是 `(行数, 列数, 0)`。读代码把最后一行当形状信息，前 N 行做 `1 基→0 基` 索引转换（`I -= 1; J -= 1`），最后组装 `coo_array`。**复数稀疏不设 `imagf`**，是否复数只能靠「列数是 3 还是 4」判断——这是 v4 格式的一个怪癖。

#### 4.3.4 代码实践

**目标**：观察复数 v4 文件的「实部块 + 虚部块」非交错存储。

**步骤**：

```python
# 示例代码
import numpy as np, os
from scipy.io import loadmat

data_dir = os.path.join(os.path.dirname(__import__('scipy.io').__file__),
                        'matlab', 'tests', 'data')
# testcomplex_4.2c_SOL2.mat 是 MATLAB 4.2c 生成的复数 v4 文件
m = loadmat(os.path.join(data_dir, 'testcomplex_4.2c_SOL2.mat'))
for k, v in m.items():
    if k.startswith('__'):
        continue
    print(k, type(v), v.dtype, v.shape)
```

**需要观察的现象**：某个变量的 `dtype` 是 `complex128`。

**预期结果**：能正确还原复数，说明 `read_full_array` 的两块拼接生效。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `read_sub_array` 里要用 `order='F'`？去掉会怎样？

> **答案**：v4 数据是列主序存的。若用 numpy 默认的 `order='C'`（行主序）去解释同一段字节，二维矩阵的形状会正确、但元素排列相当于转置后的结果，行列会错位。

**练习 2**：复数 sparse 矩阵在 v4 里没有设 `imagf`，那读取代码靠什么区分实/复？

> **答案**：靠存储矩阵的**列数**：3 列为实（行、列、值），4 列为复（行、列、实部、虚部）。见 `read_sparse_array` 里 `if res.shape[1] == 3` 的分支。

### 4.4 MatFile4Reader：字节序探测与变量遍历

#### 4.4.1 概念说明

`MatFile4Reader` 继承自基类 `MatFileReader`，是 v4 读取的「文件级」对象。它解决三个文件级问题：

1. **这个文件是什么字节序？** —— `guess_byte_order`。
2. **把那张类型模板套上字节序，准备好 dtype 字典。** —— `initialize_read`。
3. **顺序遍历文件里的每个变量。** —— `get_variables` / `list_variables`。

#### 4.4.2 核心流程

读取一个 v4 文件的全流程（由 `loadmat` 触发）：

```
loadmat(file)
  → mat_reader_factory 判定版本，返回 MatFile4Reader(stream, **kwargs)
  → MatFile4Reader.__init__ 调基类 __init__，里面会调 guess_byte_order()
  → get_variables(variable_names):
        seek(0); initialize_read()           # 套字节序、建 VarReader4
        while not end_of_stream():
            hdr, next_pos = read_var_header() # 读头 + 算下一个变量位置
            if 想要这个变量:
                mdict[name] = read_var_array(hdr)
            seek(next_pos)                    # 跳到下一个变量
```

`guess_byte_order` 的探测逻辑很巧妙：它**用本机字节序**读偏移 0 处的那个 int32（也就是 `MOPT`），然后：

- 若等于 0 → 文件是小端（LE）；
- 若 < 0 或 > 5000 → 一定是被字节序搞反了，返回与本机相反的序；
- 否则 → 没被搞反，返回本机序。

为什么 `== 0` 就一定是小端？因为最常见的 v4 变量 `MOPT=0`，**只有当文件是小端、且本机也按小端读**时，才会读出干净的 0；大端读小端的 4 个零字节仍会是 0……实际上这里依赖一个经验事实：合法的 `MOPT` 数值范围很小（0 到约 4052），用本机序读出来若落在 `[1, 5000]` 且非 0，就认为没搞反。

#### 4.4.3 源码精读

`guess_byte_order`：

[_mio4.py:328-338](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio4.py#L328-L338) —— 注意它 `seek(0)` 读完再 `seek(0)` 复位，是无副作用的。`SYS_LITTLE_ENDIAN` 在文件顶部用 `sys.byteorder == 'little'` 判定（[第 28 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio4.py#L28)）。基类 `_miobase.py` 的 `guess_byte_order` 默认返回本机序，这里子类覆写为真实探测。

`initialize_read` 把模板套上字节序：

[_mio4.py:340-346](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio4.py#L340-L346) —— `convert_dtypes(mdtypes_template, self.byte_order)`（定义在 [`_miobase.py:134`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_miobase.py#L134)）会对模板里每个 dtype 调 `.newbyteorder(order)`，产出一份「带正确字节序的 dtype 字典」，赋给 `self.dtypes`。之后 `VarReader4` 读头、读数据都查这张表。

`read_var_header` 不只读头，还算出**下一个变量的绝对位置**：

[_mio4.py:348-369](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio4.py#L348-L369) —— `remaining_bytes = ∏(dims) × itemsize`，复数（且非 sparse）再 `×2`。`next_position = 当前 tell + remaining_bytes`。这样即便某个变量我们不读，也能 `seek` 跳过它。

`get_variables` 的主循环：

[_mio4.py:389-418](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio4.py#L389-L418) —— 支持按名筛选：`variable_names` 不为空时，命中的变量从待取列表里移除，列表空了就提前 `break`，避免读完整文件。

`list_variables`（供 `whosmat` 用）只取形状不取数据：

[_mio4.py:420-434](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio4.py#L420-L434) —— 对 sparse 变量，`shape_from_header` 甚至只读最后一行就拿到 `(rows, cols)`，不必把整个稀疏三元组读完（见 [第 292-307 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio4.py#L292-L307)）。

#### 4.4.4 代码实践

**目标**：亲手验证字节序探测。

**步骤**：

```python
# 示例代码
import numpy as np, os
from scipy.io.matlab._mio4 import MatFile4Reader

data_dir = os.path.join(os.path.dirname(__import__('scipy.io').__file__),
                        'matlab', 'tests', 'data')
path = os.path.join(data_dir, 'testdouble_4.2c_SOL2.mat')
with open(path, 'rb') as f:
    reader = MatFile4Reader(f)
    print('detected byte_order =', reader.byte_order)   # 基类 __init__ 已调 guess_byte_order
```

**预期结果**：在主流小端机器上打印 `'<'`。

> **待本地验证**：若你在某些大端环境运行，结果可能是 `'>'`；以你机器的实际输出为准。

### 4.5 VarWriter4 / arr_to_2d：从 numpy 数组写回 v4

#### 4.5.1 概念说明

写入是读取的逆过程：给定一个 numpy 数组和一个变量名，生成「头 + 名字 + 列主序数据」三段字节。`VarWriter4` 负责单个变量的序列化，和 `VarReader4` 一一对应。

但写入有一个读取没有的约束：**v4 只能存二维数组**。numpy 数组可能是 0-D（标量）、1-D（向量）、或 ≥3-D。`arr_to_2d` 这个工具函数负责把任意数组规整成二维，并对 ≥3-D 报错。

另一个写入特有的问题是 **1-D 数组该存成行向量 `(1, N)` 还是列向量 `(N, 1)`**？这就是 `oned_as` 参数（`'row'` / `'column'`）控制的事。

#### 4.5.2 核心流程

`VarWriter4.write(arr, name)` 的分发：

```
scipy.sparse.issparse(arr)?  → write_sparse   （必须先判，否则 np.asarray 会退化成 object 数组）
arr.dtype.type 是 object/void? → 报 TypeError
arr.dtype.type 是 str_/bytes_? → write_char
否则                          → write_numeric
```

`write_numeric` 的流程：

```
arr = arr_to_2d(arr, oned_as)            # 规整成二维
imagf = (dtype.kind == 'c')              # 复数标志
P = np_to_mtypes[dtype.str[1:]]          # 反查类型码；查不到就退化成 double
write_header(name, arr.shape, P, mxFULL_CLASS, imagf)
if imagf:
    write_bytes(arr.real)                # 先实部
    write_bytes(arr.imag)                # 再虚部
else:
    write_bytes(arr)
```

`write_bytes` 统一用 `.tobytes(order='F')`，保证列主序。

`arr_to_2d` 借助基类的 `matdims` 把任意形状规整：

```
标量 ()     → (1, 1)
1-D 长度 N  → oned_as='row' 时 (1, N)；'column' 时 (N, 1)
≥3-D        → raise ValueError（v4 存不下）
```

#### 4.5.3 源码精读

`arr_to_2d`：

[_mio4.py:437-458](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio4.py#L437-L458) —— `matdims`（在 [`_miobase.py:259`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_miobase.py#L259)）负责把 numpy 形状翻译成 MATLAB 维度，`arr_to_2d` 在此基础上拒绝 >2 维。

`write_header` 是写入侧的「MOPT 合成器」，与读取侧的 `read_header` 互逆：

[_mio4.py:472-503](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio4.py#L472-L503) —— 关键两行：`M = not SYS_LITTLE_ENDIAN`（本机小端则 M=0，否则 M=1）；`header['mopt'] = M*1000 + O*100 + P*10 + T`，正是 4.1 节那个公式的正向计算。`namlen = len(name) + 1`（给结尾的 `'\0'` 留位置）。

`write_numeric` 的类型退化策略：

[_mio4.py:534-554](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio4.py#L534-L554) —— 用 `arr.dtype.str[1:]`（剥字节序）查 `np_to_mtypes`。若 numpy 类型不在表里（如 `float16`、`int8`、`uint32`、`uint64`），就 `astype('f8')` 或 `astype('c128')` 退化到 double，并设 `P=miDOUBLE`。这就是为什么存一个 `int8` 数组再读回来会变成 `float64`。

`write_char` 把字符串重编码为 latin-1：

[_mio4.py:556-574](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio4.py#L556-L574) —— 字符矩阵固定用 `P=miUINT8, T=mxCHAR_CLASS`；unicode 字符串先 `encode('latin-1')`。所以含非 latin-1 字符的字符串存进 v4 会丢字符。

`write_sparse` 是 `read_sparse_array` 的逆，把 COO 三元组拼成 `(N+1)×3` 布局：

[_mio4.py:576-598](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio4.py#L576-L598) —— `arr.tocoo()` 转 COO，索引 `+= 1` 转 1 基，最后一行填形状。与读取端严格对称。

#### 4.5.4 代码实践

**目标**：观察「类型退化」——存一个 `int8` 数组，读回来变成 `float64`。

**步骤**：

```python
# 示例代码
import numpy as np
from scipy.io import savemat, loadmat
import tempfile, os

tmp = tempfile.mktemp(suffix='.mat')
savemat(tmp, {'a': np.array([1, 2, 3], dtype=np.int8)}, format='4')
print(loadmat(tmp)['a'].dtype)   # float64，不是 int8
os.remove(tmp)
```

**需要观察的现象**：读回的 dtype 是 `float64`。

**预期结果**：印证 `write_numeric` 里「查不到类型码就退化到 double」的逻辑。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `VarWriter4.write` 里 sparse 的判断必须放在 `np.asarray(arr)` **之前**？

> **答案**：`np.asarray` 作用于一个 `scipy.sparse` 矩阵时，会把它包成 dtype=object 的 0 维对象数组，丢失稀疏结构。所以必须先用 `scipy.sparse.issparse(arr)` 拦截，走 `write_sparse` 分支。

**练习 2**：一个 numpy `float16` 数组用 v4 存再读，dtype 会是什么？为什么？

> **答案**：`float64`。`np_to_mtypes` 表里没有 `'f2'`，`write_numeric` 走 `except KeyError` 分支，`astype('f8')` 退化到 double，`P=miDOUBLE`。

### 4.6 MatFile4Writer：put_variables 与 oned_as

#### 4.6.1 概念说明

`MatFile4Writer` 是 v4 写入的「文件级」对象，对应读取侧的 `MatFile4Reader`。它非常薄：持有一个文件流和一个 `oned_as` 设置，核心方法 `put_variables(mdict)` 把一个 `{名字: 数组}` 字典逐个写出。

v4 **没有文件级全局头**（这点和 v5 截然不同），所以 `put_variables` 直接从第一个变量开始写，文件头相关的参数被忽略——注释里明确说明这个参数「只为与 v5 版本的方法签名兼容」而保留。

#### 4.6.2 核心流程

```
savemat(file, mdict, format='4', oned_as='row')
  → 构造 MatFile4Writer(file_stream, oned_as)
        put_variables(mdict):
            self._matrix_writer = VarWriter4(self)
            for name, var in mdict.items():
                self._matrix_writer.write(var, name)
```

`oned_as` 的传递链：`savemat(oned_as='row')` → `MatFile4Writer.__init__(oned_as)` → `VarWriter4.__init__` 里 `self.oned_as = file_writer.oned_as` → `write_numeric` 里 `arr_to_2d(arr, self.oned_as)`。

#### 4.6.3 源码精读

`MatFile4Writer.__init__` 的默认处理：

[_mio4.py:601-608](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio4.py#L601-L608) —— `oned_as is None` 时回退到 `'row'`。注意这与基类 `matdims` 的默认 `'column'` **不同**，`savemat` 顶层默认也是 `'row'`，三者协同保证「存进去是行向量」。

`put_variables`：

[_mio4.py:610-632](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio4.py#L610-L632) —— 注释 `# there is no header for a matlab 4 mat file` 说明：`write_header` 形参被有意忽略。循环体只有两行：建一个 `VarWriter4`，然后对字典里每对 `(name, var)` 调 `write`。

`savemat` 的路由（在 `_mio.py`）：

[_mio.py:306-337](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio.py#L306-L337) —— `format='4'` 分支会先拒绝 `long_field_names`（v4 不支持），再构造 `MatFile4Writer(file_stream, oned_as)`。

#### 4.6.4 代码实践

**目标**：对比 `oned_as='row'` 与 `'column'` 写同一个 1-D 数组，文件尺寸相同但读回形状不同。

**步骤**：

```python
# 示例代码
import numpy as np, os
from scipy.io import savemat, loadmat
import tempfile

a = np.arange(5)                       # 1-D，shape (5,)
rowf = tempfile.mktemp(suffix='.mat')
colf = tempfile.mktemp(suffix='.mat')
savemat(rowf, {'a': a}, format='4', oned_as='row')
savemat(colf, {'a': a}, format='4', oned_as='column')

ra = loadmat(rowf)['a']
ca = loadmat(colf)['a']
print('row   -> shape', ra.shape)      # (1, 5)
print('col   -> shape', ca.shape)      # (5, 1)
print('bytes equal?', os.path.getsize(rowf) == os.path.getsize(colf))  # True
os.remove(rowf); os.remove(colf)
```

**需要观察的现象**：两个文件字节数相同（数据量一样），但读回形状分别是 `(1, 5)` 与 `(5, 1)`，差异只来自头里的 `mrows/ncols`。

**预期结果**：印证「`oned_as` 只改变头里的尺寸字段，不改变数据块」。

#### 4.6.5 小练习与答案

**练习 1**：`MatFile4Writer.put_variables` 接受一个 `write_header` 参数却完全不用，为什么？

> **答案**：v4 格式没有文件级全局头，所以无事可做。保留这个形参是为了让 `put_variables` 在 v4 和 v5 两个 writer 上有相同的签名，方便上层（`savemat`）统一调用。

**练习 2**：为什么 `MatFile4Writer.__init__` 里 `oned_as=None` 要回退成 `'row'`，而不是 `'column'`？

> **答案**：因为 MATLAB 默认向量是行向量（如 `1:5` 得到行向量），`savemat` 的默认也是 `'row'`。三处（`savemat` / `MatFile4Writer` / 用户预期）统一为行向量，避免读回时意外得到列向量。

## 5. 综合实践

把本讲所有知识点串起来：**写一个 v4 文件 → 手动解析它的头 → 读回比对**。

**任务**：

1. 用 `savemat(..., format='4')` 写入两个变量：一个二维 `float64` 矩阵，一个 1-D 数组（先用 `oned_as='row'`，再用 `'column'` 各写一份）。
2. 用 `struct` 或 `numpy` 手动解析写出文件的前 20 字节，验证 `mopt / mrows / ncols / imagf / namlen` 五个字段。
3. 用 `loadmat` 读回，比对数值一致性（注意 1-D 数组的形状差异，需要 `squeeze_me=True` 或手动 reshape）。

**参考实现**：

```python
# 示例代码
import numpy as np
import struct
from scipy.io import savemat, loadmat
import tempfile, os

# 1) 写入
mat2d = np.array([[1.0, 2.0, 3.0],
                  [4.0, 5.0, 6.0]])      # (2, 3) double
vec = np.arange(5)                        # 1-D

f_row = tempfile.mktemp(suffix='.mat')
f_col = tempfile.mktemp(suffix='.mat')
savemat(f_row, {'M': mat2d, 'v': vec}, format='4', oned_as='row')
savemat(f_col, {'M': mat2d, 'v': vec}, format='4', oned_as='column')

# 2) 手动解析第一个变量的 20 字节头
with open(f_row, 'rb') as f:
    raw = f.read(20)
mopt, mrows, ncols, imagf, namlen = struct.unpack('<5i', raw)
print(f'mopt={mopt} mrows={mrows} ncols={ncols} imagf={imagf} namlen={namlen}')
# 期望: mopt=0, mrows=2, ncols=3, imagf=0, namlen=2 (字母 'M' + '\0')
# 之后再读 namlen=2 字节应得到 b'M\x00'
name = open(f_row, 'rb').read(22)[20:22]
print('name bytes =', name)               # b'M\x00'

# 3) 读回比对
r_row = loadmat(f_row)
r_col = loadmat(f_col)
print('M equal?', np.array_equal(r_row['M'], mat2d))         # True
print('v(row) shape =', r_row['v'].shape)                    # (1, 5)
print('v(col) shape =', r_col['v'].shape)                    # (5, 1)
print('v(row) equal after squeeze?',
      np.array_equal(np.squeeze(r_row['v']), vec))           # True

os.remove(f_row); os.remove(f_col)
```

**预期结果**：

- 手动解析得到的 `mopt=0, mrows=2, ncols=3, imagf=0, namlen=2`，与 `mat2d` 的形状、double 类型完全吻合。
- `M` 矩阵读回后数值完全一致。
- 1-D 数组 `v` 在 `row` 模式读回是 `(1, 5)`、`column` 模式是 `(5, 1)`；squeeze 后都等于原 `vec`。

> **待本地验证**：如果你的机器是大端，`mopt` 会是 `1000`（`M=1`），手动解析时把 `<5i` 换成 `>5i`。

## 6. 本讲小结

- v4 是最简单的 `.mat`：无全局头，文件就是「20 字节头 + 变量名 + 列主序数据」的顺序拼接。
- 头里的 `mopt` 是一个十进制压缩整数 `M*1000 + O*100 + P*10 + T`，同时编码字节序、数据类型、矩阵类；最常见的 double-full 矩阵 `mopt=0`，这正解释了 u3-l1 用「前 4 字节含 0」判定 v4 的原理。
- `VarReader4` 按 `mclass` 三分支重建数组：full（含「实部块+虚部块」非交错复数）、char（latin-1）、sparse（`(N+1)×3/4` 三元组，复数靠列数判定）。
- `MatFile4Reader` 三步走：`guess_byte_order` 用本机序读 MOPT 探测字节序 → `initialize_read` 把模板套上字节序 → `get_variables` 顺序遍历并用 `next_position` 跳过不要的变量。
- 写入侧 `VarWriter4` 是读取的逆过程，`write_header` 正向合成 MOPT；`np_to_mtypes` 查不到的类型会退化到 double，所以 `int8`/`float16` 读回都变 `float64`。
- `MatFile4Writer.put_variables` 极薄，v4 无文件级头；`oned_as='row'/'column'` 只改变头里的 `mrows/ncols`，数据块不变，读回形状不同但字节数相同。

## 7. 下一步学习建议

- 本讲只覆盖了 v4。下一讲 **u3-l5（MAT v5 读取）** 将进入复杂得多的 v5：128 字节全局头、`tag + data` 的 data element 结构、压缩元素、cell/struct 的递归读取，以及小元素 SDE 优化。建议先复习本讲的「复数非交错存储」「列主序」「按类分支重建」三个概念，它们在 v5 里依然成立。
- 若想了解读路径用到的 `squeeze_element` / `chars_to_strings` 后处理细节，可先读 [`_mio_utils.pyx`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio_utils.pyx)，它也为 v5 服务。
- 想看 v4 的更多边界用例，可读 [`matlab/tests/test_mio.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/tests/test_mio.py) 中 `format='4'` 相关的用例，以及 u4-l3 对测试数据命名约定的讲解。
