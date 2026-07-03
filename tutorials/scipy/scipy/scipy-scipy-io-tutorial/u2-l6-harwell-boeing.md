# Harwell-Boeing 稀疏矩阵格式（_harwell_boeing）

## 1. 本讲目标

Harwell-Boeing（以下简称 H-B）是科学计算领域一种「年高德劭」的稀疏矩阵交换格式，诞生于 1980 年代的 Harwell 实验室与波音公司合作。它用一个**纯文本头 + Fortran 格式化的数值区**来存放一个稀疏矩阵，至今仍是 Sparse Matrix Collection（矩阵收藏库）的常用格式之一。

本讲聚焦 `scipy.io._harwell_boeing` 子包，读完本讲你应当能够：

- 说出 H-B 文件「四行头 + 三段数据」的整体结构，以及 `MXTYPE` 三个字母各自编码的含义。
- 读懂 `HBInfo` 如何从文件解析头信息、又如何从稀疏矩阵反向生成头信息。
- 理解 `_fortran_format_parser` 里的 `FortranFormatParser` 如何把 `(3E14.5)`、`(26I3)` 这样的 Fortran 格式串解析成可执行的对象，并说明 `repeat` 字段如何决定「每行读几个数」。
- 跟踪 `hb_read` / `hb_write` 的完整调用链，理解其中 1 基索引到 0 基索引的转换。
- 完成一个稀疏矩阵写出 → 读回比对的 round-trip 实践。

本讲建立在 u2-l3（Matrix Market）已经建立的「稀疏矩阵交换格式」直觉之上。Matrix Market 存的是 COO 三元组 `(行, 列, 值)`，而 H-B 存的是 **CSC（压缩稀疏列）** 的 `指针 / 行索引 / 值` 三段——这是两者最大的结构差异。

## 2. 前置知识

### 2.1 稀疏矩阵与 CSC 存储

当一个矩阵里大部分元素都是 0 时，只存「非零元」能省下海量内存。存法有多种，本讲需要你理解 **CSC（Compressed Sparse Column，压缩稀疏列）**：

给定一个 \( m \times n \) 的矩阵 \( A \)，设它共有 \( \text{nnz} \) 个非零元。CSC 用三个一维数组表示：

- `indptr`（列指针）：长度 \( n+1 \)。`indptr[j]` 到 `indptr[j+1]-1` 给出第 \( j \) 列的非零元在 `indices` / `data` 中的下标范围。
- `indices`（行下标）：长度 \( \text{nnz} \)，存每个非零元的行号。
- `data`（值）：长度 \( \text{nnz} \)，存每个非零元的数值。

例如矩阵

\[
A = \begin{pmatrix} 1 & 0 & 4 \\ 0 & 0 & 5 \\ 2 & 3 & 6 \end{pmatrix}
\]

按列优先记录非零元，得到 `indices = [0,2, 2, 0,1,2]`、`data = [1,2, 3, 4,5,6]`、`indptr = [0,2,3,6]`。SciPy 的 `scipy.sparse.csc_array` / `csc_matrix` 就是这个结构。H-B 文件的「指针 / 索引 / 值」三段，正好对应 CSC 的这三段。

### 2.2 1 基索引与 0 基索引

Fortran / MATLAB / H-B 格式都从 **1** 开始数下标，而 Python / NumPy / C 从 **0** 开始。H-B 文件里写的是 1 基下标，scipy 读回时要减 1 转成 0 基，写出时要加 1。这是本讲反复出现的一个「+1 / −1」操作，务必留意。

### 2.3 Fortran 格式串

Fortran 用形如 `(26I3)`、`(3E23.15)` 的「格式串」描述一行文本如何切分成若干个定宽字段：

- `26I3`：一行 26 个整数（`I`），每个占 3 个字符宽。
- `3E23.15`：一行 3 个浮点数（`E` 表示科学计数法），每个占 23 字符宽，其中 15 位是有效数字。

H-B 文件的数值区就是按这种 Fortran 格式排版的。scipy 不调用真正的 Fortran 运行时，而是用一个手写的 `FortranFormatParser` 把格式串解析成 Python 对象，再驱动读写——这是本讲最值得精读的「小而完整」的解析器。

> 术语速查：**MXTYPE**（矩阵类型，3 个字母）、**nnz**（non-zero，非零元个数）、**assembled**（已组装，指矩阵已展开为行列形式，区别于 elemental「元素」形式）、**round-trip**（写出再读回，验证一致性）。

## 3. 本讲源码地图

本讲只涉及 `scipy/io/_harwell_boeing/` 这一个子包，外加顶层的导出与弃用包装：

| 文件 | 作用 |
| --- | --- |
| `scipy/io/_harwell_boeing/__init__.py` | 子包入口，导出 `hb_read` / `hb_write` |
| `scipy/io/_harwell_boeing/hb.py` | H-B 格式主体实现：`HBInfo`（头）、`HBFile`（文件包装）、`HBMatrixType`（矩阵类型）、`hb_read`/`hb_write`、`_read_hb_data`/`_write_data` |
| `scipy/io/_harwell_boeing/_fortran_format_parser.py` | Fortran 格式串解析器：`FortranFormatParser`、`IntFormat`、`ExpFormat`、`Tokenizer` |
| `scipy/io/_harwell_boeing/meson.build` | Meson 构建配置，把三个 `.py` 安装到 `scipy/io/_harwell_boeing` 子目录 |
| `scipy/io/__init__.py` | 顶层包，第 112 行 `from ._harwell_boeing import hb_read, hb_write` 把函数 re-export 到 `scipy.io` |
| `scipy/io/harwell_boeing.py` | 弃用包装模块（无前缀），在 SciPy 2.0 将被移除，靠 `__getattr__` 转发到 `_harwell_boeing`（详见 u4-l2） |

对外的公共入口只有两个：`scipy.io.hb_read` 与 `scipy.io.hb_write`。其余 `HBInfo`、`HBFile`、`FortranFormatParser` 等都是内部实现类，但它们是理解读写流程的关键。

## 4. 核心概念与源码讲解

### 4.1 H-B 文件结构与 CSC 存储模型

#### 4.1.1 概念说明

一个 H-B 文件由**四行文本头** + **三段数值数据**构成。四行头各司其职：

| 行 | 内容 | 字段切分 |
| --- | --- | --- |
| 第 1 行 | 标题 + 关键字 | 前 72 字符是 `title`，其后是 `key`（通常 8 字符） |
| 第 2 行 | 各段数据占多少**行** | 每 14 字符一段：`TOTCRD / PTRCRD / INDCRD / VALCRD`（再加可选的 `RHSCRD` 右端项行数） |
| 第 3 行 | 矩阵类型 + 维度 | 前 3 字符是 `MXTYPE`，空 11 格，再每 14 字符一段：`NROW / NCOL / NNZERO / NELTVL` |
| 第 4 行 | 三段各自的 Fortran 格式串 | `PTRFMT`、`INDFMT`、`VALFMT`，用空格分隔 |

头之后依次是三段数据：**列指针段**（pointer）、**行索引段**（indices）、**值段**（values）。这三段正是 CSC 的 `indptr / indices / data`，只是用了 1 基下标，并按第 4 行声明的 Fortran 格式排版成定宽文本。

`MXTYPE` 三个字母分别编码三个维度，`HBMatrixType` 类用三张字典把它们代码化。

#### 4.1.2 核心流程

把 H-B 文件「读成一个矩阵」的整体流程：

```text
hb_read(路径)
  └─ HBFile(file)
       └─ HBInfo.from_file(file)      # 读 4 行头，解析出维度/格式/类型
            ├─ 第1行 → title, key
            ├─ 第2行 → *_nlines (各段行数)
            ├─ 第3行 → mxtype, nrows, ncols, nnz   # mxtype 经 HBMatrixType.from_fortran 解析
            └─ 第4行 → 三个 Fortran 格式串
       └─ read_matrix() → _read_hb_data(file, header)
            ├─ 按 pointer_nbytes_full 读指针段 → np.fromstring → ptr
            ├─ 按 indices_nbytes_full 读索引段 → ind
            ├─ 按 values_nbytes_full 读值段     → val
            └─ csc_array((val, ind-1, ptr-1), shape=(nrows, ncols))   # 1基转0基
```

矩阵类型 `MXTYPE` 的三个字母含义如下表（来自 `HBMatrixType` 的字典）：

| 位置 | 含义 | 可选值 |
| --- | --- | --- |
| 第 1 字符 | 值类型 | R=real, C=complex, P=pattern, I=integer |
| 第 2 字符 | 对称结构 | S=symmetric, U=unsymmetric, H=hermitian, Z=skewsymmetric, R=rectangular |
| 第 3 字符 | 存储形式 | A=assembled, E=elemental |

> 注意：scipy 的实现**只支持一个子集**——`real` 或 `integer`、`unsymmetric`、`assembled`（即 `RUA` / `IUA`）。其它组合（复数、对称、元素形式）在读头时就会被拒绝。这点在 `hb.py` 顶部的 docstring 与各 `from_file` 校验中都有声明。

#### 4.1.3 源码精读

矩阵类型的编码与双向转换由 `HBMatrixType` 负责：

[_harwell_boeing/hb.py:L359-L378](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_harwell_boeing/hb.py#L359-L378)

这一段定义了三张「限定名 ↔ Fortran 字符」字典 `_q2f_type` / `_q2f_structure` / `_q2f_storage`，并用字典推导式反向生成 `_f2q_*`，从而支持「字符 → 限定名」（读）与「限定名 → 字符」（写）两个方向。例如 `real/unsymmetric/assembled` 对应字符串 `"RUA"`。

从文件读取 3 字符类型时调用 `from_fortran`：

[_harwell_boeing/hb.py:L384-L395](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_harwell_boeing/hb.py#L384-L395)

它先校验长度为 3，再用三个字符分别去 `_f2q_*` 字典里查，查不到就抛 `ValueError`。

反向把类型对象拼回 3 字符串（写头时用）：

[_harwell_boeing/hb.py:L409-L413](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_harwell_boeing/hb.py#L409-L413)

#### 4.1.4 代码实践

**实践目标**：亲手把一个 3×3 稀疏矩阵转成 CSC 的三段，并写出它对应的 `MXTYPE` 字符串，建立「CSC ↔ H-B 三段」的直觉。

```python
# 示例代码
import numpy as np
from scipy.sparse import csc_array
from scipy.io._harwell_boeing.hb import HBMatrixType

A = np.array([[1, 0, 4],
              [0, 0, 5],
              [2, 3, 6]], dtype=float)
csc = csc_array(A)

print("indptr  =", csc.indptr)   # [0 2 3 6]
print("indices =", csc.indices)  # [0 2 2 0 1 2]
print("data    =", csc.data)     # [1. 2. 3. 4. 5. 6.]
print("nnz     =", csc.nnz)      # 6

# 这是一个实数、非对称、已组装矩阵
mt = HBMatrixType("real", "unsymmetric", "assembled")
print("MXTYPE  =", mt.fortran_format)  # RUA
```

**需要观察的现象**：`indptr` 长度 = 列数 + 1；`indices` 与 `data` 长度 = nnz；按列优先看，第 0 列的非零元是 `(0,1)` 与 `(2,2)`，正对应 `indptr[0]=0, indptr[1]=2` 的范围 `[0,2)`。

**预期结果**：`MXTYPE` 为 `RUA`，三段数组与注释一致。

#### 4.1.5 小练习与答案

**练习 1**：若把上面的矩阵改成全是整数（`dtype=int`），`MXTYPE` 应该是哪三个字母？
**答案**：`IUA`（integer / unsymmetric / assembled）。

**练习 2**：一个对称矩阵只想存下三角，`MXTYPE` 第二个字母应该是什么？scipy 的 `hb_read` 能读它吗？
**答案**：第二个字母应是 `S`（symmetric）。但 scipy 实现会拒绝——`from_file` 中 `mxtype.structure == "unsymmetric"` 的校验不通过，抛 `ValueError`。

---

### 4.2 FortranFormatParser：把格式串变成可执行对象

#### 4.2.1 概念说明

H-B 文件的第 4 行声明了三段数据各自的 Fortran 格式串，例如 `(26I3)` 与 `(3E23.15)`。scipy 需要理解这些字符串，才能知道「每行读几个数、每个数占多宽、是什么类型」。

`_fortran_format_parser.py` 用一个**手写的词法分析器（Tokenizer）+ 递归下降风格的解析器（FortranFormatParser）**完成这件事，最终产出两种「格式对象」：

- `IntFormat`：整数格式，如 `(26I3)`、`(I4)`。
- `ExpFormat`：科学计数法浮点格式，如 `(3E23.15)`、`(E8.3E3)`。

> 注意：这个解析器**只支持 `I` 与 `E` 两类**，不支持 `F`（定点浮点）。H-B 的值段用 `E` 格式，指针与索引段用 `I` 格式，因此这两类已经够用。

#### 4.2.2 核心流程

解析 `(3E14.5)` 的流程如下：

```text
FortranFormatParser.parse("(3E14.5)")
  ├─ Tokenizer 把字符串切成 token 序列：
  │     LPAR  INT(3)  EXP_ID(E)  INT(14)  DOT  INT(5)  RPAR
  └─ _parse_format(tokens)
        ├─ 校验首尾是 LPAR / RPAR，剥掉
        ├─ 第一个 token 是 INT → repeat = 3
        ├─ 遇到 EXP_ID → 读 width=14, DOT, significand=5
        └─ 返回 ExpFormat(width=14, significand=5, repeat=3)
```

格式对象的三个关键字段决定了「每行如何排版」：

- `repeat`：一行放几个数（**这就是「每行读几个浮点数」的答案**）。
- `width`：每个数占几个字符。
- `significand`（仅 `ExpFormat`）：科学计数法的小数有效位数。

一行（不含换行）的字符数即 \( \text{repeat} \times \text{width} \)。例如 `(3E14.5)` 每行 3 个数、共 \( 3 \times 14 = 42 \) 字符。

`repeat` 还有一个聪明的自动算法：`IntFormat.from_number` / `ExpFormat.from_number` 先按数值范围算出每个数「最少需要多宽才不丢精度」，再用 `repeat = 80 // width` 让一行尽量塞满 80 列（Fortran 的传统行宽）。

#### 4.2.3 源码精读

先看整数格式的「按数值自动选宽」：

[_harwell_boeing/_fortran_format_parser.py:L37-L65](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_harwell_boeing/_fortran_format_parser.py#L37-L65)

`number_digits(n)` 用 \( \lfloor \log_{10}|n| \rfloor + 1 \) 算出位数，`width = 位数 + 1`（留一位给符号或 1 基偏移），负数再多留一位；最后 `repeat = 80 // width` 决定每行塞几个。`initial=1` 是为了在空数组（nnz=0）时也能给出合法格式（详见 gh-24082 的修复）。

浮点格式同理，但要兼顾指数部分的位数：

[_harwell_boeing/_fortran_format_parser.py:L97-L131](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_harwell_boeing/_fortran_format_parser.py#L97-L131)

它用 `np.finfo` 拿到浮点类型的精度与指数范围，算出 `width = 符号 + 整数位 + 点 + 小数位 + E + 指数符号 + 指数位数`，`repeat = floor(80 / width)`。

格式对象提供两个方向的「出口」属性。`fortran_format` 把对象重新拼回 Fortran 字符串（写头时用）：

[_harwell_boeing/_fortran_format_parser.py:L154-L162](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_harwell_boeing/_fortran_format_parser.py#L154-L162)

`python_format` 给出等价的 Python `%` 格式化串（写数据时用），注意宽度是 `width-1`（留一个前导空格作分隔）：

[_harwell_boeing/_fortran_format_parser.py:L164-L166](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_harwell_boeing/_fortran_format_parser.py#L164-L166)

词法分析由 `Tokenizer` 完成，它用一组正则按优先级逐个尝试匹配：

[_harwell_boeing/_fortran_format_parser.py:L182-L206](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_harwell_boeing/_fortran_format_parser.py#L182-L206)

最后是解析器主入口 `parse`，它把 token 序列交给 `_parse_format`，并把底层 `SyntaxError` 包装成更友好的 `BadFortranFormat`：

[_harwell_boeing/_fortran_format_parser.py:L236-L253](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_harwell_boeing/_fortran_format_parser.py#L236-L253)

> 细节：`parse` 把 `Tokenizer` 存在 `threading.local()` 里（第 234 行），这样同一个 `FortranFormatParser` 实例在多线程下各自维护游标，互不干扰。

核心语法解析 `_parse_format` 先剥括号、判断有无前导重复数，再按 `INT_ID` / `EXP_ID` 分派：

[_harwell_boeing/_fortran_format_parser.py:L266-L309](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_harwell_boeing/_fortran_format_parser.py#L266-L309)

#### 4.2.4 代码实践

**实践目标**：用 `FortranFormatParser` 解析 `(3E14.5)`，验证 `repeat` 如何决定「每行读几个浮点数」。

```python
# 示例代码（可直接运行）
from scipy.io._harwell_boeing._fortran_format_parser import (
    FortranFormatParser, IntFormat, ExpFormat, BadFortranFormat)

parser = FortranFormatParser()

f = parser.parse("(3E14.5)")
print(type(f).__name__, f)              # ExpFormat (3E14.5)
print("repeat      =", f.repeat)        # 3   → 每行 3 个浮点数
print("width       =", f.width)         # 14  → 每个 14 字符宽
print("significand =", f.significand)   # 5
print("fortran     =", f.fortran_format)# (3E14.5)
print("python      =", f.python_format) # %13.5E
print("一行字符数   =", f.repeat * f.width)  # 42

i = parser.parse("(26I3)")
print("repeat      =", i.repeat)        # 26  → 每行 26 个整数
print("width       =", i.width)         # 3

# 非法格式会被拒绝
try:
    parser.parse("(E4.)")
except BadFortranFormat as e:
    print("拒绝非法格式：", e)
```

**需要观察的现象**：`(3E14.5)` 的 `repeat=3`，`(26I3)` 的 `repeat=26`——`repeat` 正是每行容纳的数值个数；`python_format` 的宽度是 `width-1`。

**预期结果**：输出与注释一致；`(E4.)` 抛出 `BadFortranFormat`。

#### 4.2.5 小练习与答案

**练习 1**：`IntFormat.from_number(123456789)` 会得到什么 `repeat` 和 `width`？
**答案**：`number_digits(123456789)=9`，`width=10`，`repeat=80//10=8`，即 `(8I10)`。

**练习 2**：为什么 `ExpFormat.python_format` 用 `width-1` 而不是 `width`？
**答案**：留一个前导空格作字段分隔符，避免相邻数字粘连，同时配合 `np.fromstring(..., sep=' ')` 用空格切分读取。

---

### 4.3 HBInfo：四行头的解析与生成

#### 4.3.1 概念说明

`HBInfo` 是「头信息」对象，它把 H-B 文件的四行头抽象成一组属性（`title`、`*_nlines`、`mxtype`、`nrows`、`ncols`、`nnon_zeros`、三个格式对象等）。它提供两条互逆的构造路径：

- `HBInfo.from_file(fid)`：从已打开的文件读 4 行，解析成 `HBInfo`（**读**路径用）。
- `HBInfo.from_data(m, ...)`：从稀疏矩阵反推出头信息（**写**路径用，自动决定格式串与 `MXTYPE`）。

构造完成后，`dump()` 又能把 `HBInfo` 序列化回四行文本，供 `_write_data` 写出。

#### 4.3.2 核心流程

**读头**（`from_file`）逐行解析：

```text
第1行: title = line[:72]; key = line[72:]
第2行: 每14字符一段 → total/pointer/indices/values_nlines; 56:72 是 rhs_nlines（须为0）
第3行: line[:3] → MXTYPE（HBMatrixType.from_fortran）
       line[3:14] 须为11个空格
       line[14:28]/[28:42]/[42:56]/[56:70] → nrows/ncols/nnz/nelementals（须为0）
第4行: line.split() → 三个格式串 [PTRFMT, INDFMT, VALFMT]
```

**写头**（`from_data` + `dump`）反向构造：先把矩阵转 CSC，取 `indptr/indices/data`；按各自最大值用 `IntFormat.from_number` / `ExpFormat.from_number` 自动算出三个格式串；再统计每段需要多少行；最后由 `dump()` 用 `:14d`、`ljust` 等格式化成定宽四行。

`__init__` 在两条路径汇合处做一致性校验：用 `FortranFormatParser` 把三个格式串解析成 `IntFormat` / `ExpFormat`，并检查格式类型与 `MXTYPE` 的值类型是否匹配（例如 `ExpFormat` 只能配 `real`/`complex`，`IntFormat` 只能配 `integer`）。

#### 4.3.3 源码精读

读头主逻辑 `from_file`：

[_harwell_boeing/hb.py:L128-L212](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_harwell_boeing/hb.py#L128-L212)

注意几处校验：第 1 行至少 72 字符；第 2 行去掉尾空白至少 56 字符，且右端项行数（`rhs_nlines`）必须为 0；第 3 行的 `MXTYPE` 必须落在 `real/integer + unsymmetric + assembled` 子集内，`nelementals` 必须为 0。整数解析统一走 `_expect_int`：

[_harwell_boeing/hb.py:L301-L307](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_harwell_boeing/hb.py#L301-L307)

写头反向构造 `from_data`：

[_harwell_boeing/hb.py:L47-L127](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_harwell_boeing/hb.py#L47-L127)

关键点：指针与索引都 `+1` 再取最大值（因为 H-B 是 1 基）；值段对浮点用 `ExpFormat`、对整数用 `IntFormat`；`_nlines(fmt, size)` 用 `size // fmt.repeat`（向上取整）算出每段占用多少行。

构造时的一致性校验 `__init__`：

[_harwell_boeing/hb.py:L214-L285](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_harwell_boeing/hb.py#L214-L285)

这里用 `FortranFormatParser` 解析三个格式串，并按 `ExpFormat`/`IntFormat` 推断 `values_dtype`（`np.float64` 或 `int`）。同时还预算出每段的「满行字节数」`pointer_nbytes_full` 等（见下方 `_nbytes_full`），供数据读取按字节定位：

[_harwell_boeing/hb.py:L40-L43](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_harwell_boeing/hb.py#L40-L43)

公式 \( (\text{repeat} \times \text{width} + 1) \times (\text{nlines}-1) \) 给出「除最后一行（可能是半行）外，所有满行」的总字节数（`+1` 是每行的换行符）。

最后 `dump()` 把 `HBInfo` 序列化回四行定宽文本：

[_harwell_boeing/hb.py:L287-L298](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_harwell_boeing/hb.py#L287-L298)

#### 4.3.4 代码实践

**实践目标**：写出一个矩阵的 H-B 头，再把它读回，验证四行头的定宽切分。

```python
# 示例代码
from io import StringIO
from scipy.sparse import csc_array
from scipy.io import hb_write
from scipy.io._harwell_boeing.hb import HBFile, HBInfo

m = csc_array(([1., 2., 3.], ([0, 1, 2], [0, 1, 2])), shape=(3, 3))
buf = StringIO()
hb_write(buf, m)
print(buf.getvalue())

# 只读头，不读数据
buf.seek(0)
info = HBInfo.from_file(buf)
print("mxtype =", info.mxtype)        # HBMatrixType(real, unsymmetric, assembled)
print("shape  =", info.nrows, info.ncols)
print("nnz    =", info.nnon_zeros)
```

**需要观察的现象**：`buf.getvalue()` 的前四行是定宽文本——第 1 行标题占满 72 列再接 key；第 2 行是四个 `:14d` 数字；第 3 行以 `RUA` 开头；第 4 行是三个 Fortran 格式串。

**预期结果**：`mxtype` 为 `real/unsymmetric/assembled`，`shape = 3 3`，`nnz = 3`。具体头文本的列宽「待本地验证」（取决于 scipy 版本对 `dump` 的精确排版）。

#### 4.3.5 小练习与答案

**练习 1**：第 2 行的四个数字分别代表什么？为什么 `from_file` 要求第 2 行至少 56 字符？
**答案**：分别是总数据行数、指针段行数、索引段行数、值段行数。56 字符正好覆盖这 4 个 14 字符字段；56:72 的右端项行数可选，scipy 要求它为 0。

**练习 2**：`_nbytes_full` 公式里 `nlines - 1` 的「−1」是为了什么？
**答案**：因为最后一段最后一行可能是「半行」（没塞满 `repeat` 个数），所以只对前 `nlines-1` 个满行按字节数读取，最后一行改用 `readline()` 单独读。

---

### 4.4 数据区读写：_read_hb_data 与 _write_data

#### 4.4.1 概念说明

头解析完，剩下三段数据（指针、索引、值）需要按各自的 Fortran 格式读出或写入。scipy 的做法很务实：不逐字段解析，而是「按字节数一把读出满行 + readline 读半行」，再用 `np.fromstring(..., sep=' ')` 用空格一次性切成数组。写入则反过来，用 `python_format` 把每个数定宽格式化后按行写出。

`HBFile` 是一个轻量包装类，把「文件对象 + HBInfo」组合起来，对外暴露 `read_matrix()` / `write_matrix(m)`，让 `hb_read` / `hb_write` 的逻辑更干净。

#### 4.4.2 核心流程

**读数据**（`_read_hb_data`）：

```text
for 每段 (pointer/indices/values):
    满行字节 = content.read(*_nbytes_full)        # 读所有满行
    末半行  = content.readline()                  # 读最后半行
    数组    = np.fromstring(满行+末半行, dtype, sep=' ')
return csc_array((val, ind-1, ptr-1), shape=(nrows, ncols))   # 1基→0基
```

**写数据**（`_write_data`）：

```text
m = m.tocsc()                       # 统一转 CSC
fid.write(header.dump() + "\n")     # 先写四行头
write_array(indptr+1, pointer_nlines, pointer_format)   # +1 转 1 基
write_array(indices+1, indices_nlines, indices_format)
write_array(data, values_nlines, values_format)
```

`write_array` 内部先把 `repeat` 个 `python_format` 拼成「一行的模板」，再对每个满行用 `%` 格式化，末尾半行单独处理。

#### 4.4.3 源码精读

读取三段并组装 CSC：

[_harwell_boeing/hb.py:L310-L327](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_harwell_boeing/hb.py#L310-L327)

最关键的一行是末尾的 `csc_array((val, ind-1, ptr-1), ...)`——把 1 基的指针与索引减 1 转成 NumPy 的 0 基。注意源码注释提到「读取够快（≥85% 时间在 `np.fromstring`），但占内存」（第 15-16 行），因为要先拼成一个大字符串。

写入数据与四行头：

[_harwell_boeing/hb.py:L330-L356](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_harwell_boeing/hb.py#L330-L356)

`write_array` 的策略：用 `pyfmt * fmt.repeat` 生成满行模板，把数组 reshape 成 `(满行数, repeat)` 逐行写出；剩余元素单独拼一行。指针与索引写出时都 `+1`。

文件包装类 `HBFile`：

[_harwell_boeing/hb.py:L419-L463](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_harwell_boeing/hb.py#L419-L463)

构造时若没给 `hb_info`，就调用 `HBInfo.from_file(file)` 自动读头（读路径）；给了 `hb_info` 则直接用（写路径）。`read_matrix` / `write_matrix` 只是转调模块级函数 `_read_hb_data` / `_write_data`。

#### 4.4.4 代码实践

**实践目标**：跟踪一次 round-trip，确认写出与读回的 CSC 三段一致（行/列序不变）。

```python
# 示例代码
from io import StringIO
import numpy as np
from scipy.sparse import csc_array
from scipy.io import hb_read, hb_write

data = np.array([10., 20., 30., 40.])
rows = np.array([0, 2, 1, 3])
cols = np.array([0, 0, 2, 3])
m = csc_array((data, (rows, cols)), shape=(4, 4))

buf = StringIO()
hb_write(buf, m)
buf.seek(0)
m2 = hb_read(buf, spmatrix=False)

print("indptr  相等:", np.array_equal(m.indptr, m2.indptr))
print("indices 相等:", np.array_equal(m.indices, m2.indices))
print("data    相等:", np.array_equal(m.data, m2.data))
print(m2.toarray())
```

**需要观察的现象**：写出后的文本里，指针段写的是 `indptr+1`（1 基），读回时 `ind-1, ptr-1` 又转回 0 基，因此三段完全一致。

**预期结果**：三个 `相等` 均为 `True`，`toarray()` 还原出原始稀疏矩阵。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `write_array` 写指针和索引时要 `+1`，而 `_read_hb_data` 读回时要 `−1`？
**答案**：H-B 格式规定用 1 基下标（Fortran 惯例），而 NumPy/CSC 用 0 基。写出加 1、读回减 1，正好抵消，保证 round-trip 一致。

**练习 2**：源码注释说「读取占内存」，原因是什么？
**答案**：`_read_hb_data` 会把整段数据的字节先拼成一个大字符串，再交给 `np.fromstring` 解析，等于在内存里同时持有「文本 + 数组」两份。要更省内存需改用编译代码逐字段解析。

---

### 4.5 hb_read / hb_write：顶层入口与 spmatrix 迁移

#### 4.5.1 概念说明

`hb_read` 和 `hb_write` 是 `scipy.io` 暴露给用户的两个公共函数（顶层 `__init__.py` 第 112 行 re-export）。它们把「打开文件 → 构造 HBFile → 读/写矩阵 → 关闭文件」这套样板封装起来，并统一处理两类输入：路径字符串与已打开的文件对象（用 `hasattr(x, 'read')` / `hasattr(x, 'write')` 区分）。

`hb_read` 还牵涉到一个正在进行的迁移：SciPy 正把稀疏矩阵的默认返回类型从 `spmatrix`（旧）迁到 `sparray`（新）。因此 `hb_read` 多了一个 `spmatrix` 关键字，**默认值正在变化**——不传时会触发 `DeprecationWarning` 并返回 `csc_matrix`，未来（v1.20）默认改为返回 `csc_array`。

#### 4.5.2 核心流程

`hb_read` 流程：

```text
def hb_read(path_or_open_file, *, spmatrix=_NoValue):
    按 read 属性判断是文件对象还是路径，分别处理
    data = HBFile(fid).read_matrix()      # 内部已是 csc_array
    if spmatrix is _NoValue:               # 调用者没传 → 警告
        warn(DeprecationWarning); spmatrix = True
    return csc_matrix(data) if spmatrix else data
```

`hb_write` 流程：

```text
def hb_write(path_or_open_file, m, hb_info=None):
    m = m.tocsc()                          # 接受任意稀疏格式，统一转 CSC
    if hb_info is None:
        hb_info = HBInfo.from_data(m)      # 自动生成头
    按 write 属性判断文件对象/路径
    HBFile(fid, hb_info).write_matrix(m)
```

#### 4.5.3 源码精读

`hb_read`，注意 `spmatrix` 的弃用迁移逻辑：

[_harwell_boeing/hb.py:L466-L538](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_harwell_boeing/hb.py#L466-L538)

要点：用哨兵 `_NoValue`（而非 `True`）作默认值，从而能区分「调用者显式传 `spmatrix=True`」与「完全没传」。只有没传时才发 `DeprecationWarning`。警告用 `skip_file_prefixes=(os.path.dirname(__file__),)`，避免 scipy 内部调用自己也反复报警。

`hb_write`：

[_harwell_boeing/hb.py:L541-L592](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_harwell_boeing/hb.py#L541-L592)

它先把任意稀疏格式 `.tocsc()`，再按需用 `HBInfo.from_data` 生成头信息，最后写入。docstring 里的示例（`csr_array(eye(3))` 的 round-trip）就是最权威的用法示范。

顶层 re-export 的位置（印证这两个函数就是公共 API）：

[scipy/io/__init__.py:L112](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/__init__.py#L112)

#### 4.5.4 代码实践

**实践目标**：用一个真实的 H-B 文本（取自 scipy 自带测试）调用 `hb_read`，分别体验 `spmatrix=False`、`spmatrix=True`、以及不传时的行为差异。

```python
# 示例代码（SIMPLE 来自 scipy/io/_harwell_boeing/tests/test_hb.py）
from io import StringIO
import warnings
from scipy.io import hb_read

SIMPLE = """\
No Title                                                                |No Key
             9             4             1             4
RUA                      100           100            10             0
(26I3)          (26I3)          (3E23.15)
1  2  2  2  2  3  3  3  3  3  3  3  3  3  3  3  3  3  3  3  3  3  3  3  3  3
3  3  3  3  3  3  3  3  3  3  3  3  3  3  3  3  3  3  3  3  3  3  3  3  3  3
3  3  3  3  3  3  3  4  4  4  6  6  6  6  6  6  6  6  6  6  6  8  9  9  9  9
9  9  9  9  9  9  9  9  9  9  9  9  9  9  9  9  9  9  9  9  9  9  9  9 11
37 71 89 18 30 45 70 19 25 52
2.971243799687726e-01  3.662366682877375e-01  4.786962174699534e-01
6.490068647991184e-01  6.617490424831662e-02  8.870370343191623e-01
4.196478590163001e-01  5.649603072111251e-01  9.934423887087086e-01
6.912334991524289e-01
"""

m_arr = hb_read(StringIO(SIMPLE), spmatrix=False)
print("默认数组类型:", type(m_arr).__name__, m_arr.shape)   # csc_array (100,100)

m_mat = hb_read(StringIO(SIMPLE), spmatrix=True)
print("显式矩阵类型:", type(m_mat).__name__)

with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    hb_read(StringIO(SIMPLE))              # 不传 spmatrix
    print("是否触发 DeprecationWarning:",
          any(issubclass(x.category, DeprecationWarning) for x in w))
```

**需要观察的现象**：`spmatrix=False` 返回 `csc_array`，`spmatrix=True` 返回 `csc_matrix`；不传时返回 `csc_matrix` **并**触发一条 `DeprecationWarning`，提示默认值将在 v1.20 改变。

**预期结果**：输出 `csc_array (100, 100)`、`csc_matrix`，最后一行打印 `True`。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `hb_read` 用 `_NoValue` 作 `spmatrix` 的默认值，而不是直接用 `True`？
**答案**：为了区分「用户显式传 `spmatrix=True`」与「用户根本没传」。只有「没传」才需要发弃用警告；若默认值就是 `True`，就无法分辨这两种情况，会在用户已经显式选择时也误报警告。

**练习 2**：`hb_write` 接收的矩阵不一定是 CSC（可能是 CSR/COO/DOK 等），它是如何处理的？
**答案**：第 579 行 `m = m.tocsc(copy=False)` 先统一转成 CSC，再据此生成头信息与写出三段数据。这样用户可以传入任意稀疏格式。

## 5. 综合实践

把本讲五个最小模块串起来，完成一次「手工审视」的 round-trip。

**任务**：

1. 用 `scipy.sparse` 构造一个 5×5、约 30% 密度的随机稀疏矩阵（float64）。
2. 用 `hb_write` 把它写进一个 `io.StringIO`，**打印前 4 行头**，并用肉眼核对：
   - 第 1 行标题/key；
   - 第 2 行四个「行数」字段；
   - 第 3 行的 `MXTYPE`（应为 `RUA`）与 `nrows/ncols/nnz`；
   - 第 4 行三个 Fortran 格式串。
3. 用 `FortranFormatParser` 解析第 4 行的值段格式串，打印它的 `repeat`、`width`、`significand`，并算出「每个满行容纳几个浮点数」。
4. 用 `hb_read(..., spmatrix=False)` 读回，验证 `indptr/indices/data` 三段与原矩阵完全一致。

```python
# 示例代码（综合实践骨架）
from io import StringIO
import numpy as np
from scipy.sparse import random_array
from scipy.io import hb_read, hb_write
from scipy.io._harwell_boeing._fortran_format_parser import FortranFormatParser

m = random_array((5, 5), density=0.3).tocsc()
buf = StringIO()
hb_write(buf, m)
text = buf.getvalue()

print("=== 四行头 ===")
print("\n".join(text.splitlines()[:4]))

# 解析值段格式（第4行第3个 token）
valfmt = text.splitlines()[3].split()[2]
f = FortranFormatParser().parse(valfmt)
print("值段格式:", valfmt, "→ repeat:", f.repeat,
      "width:", f.width, "每行浮点数:", f.repeat)

buf.seek(0)
m2 = hb_read(buf, spmatrix=False)
print("round-trip 一致:",
      np.array_equal(m.indptr, m2.indptr)
      and np.array_equal(m.indices, m2.indices)
      and np.allclose(m.data, m2.data))
```

**预期结果**：四行头格式工整；值段格式的 `repeat`（如 3）正好是「每行浮点数」；round-trip 一致性为 `True`。具体格式串的数值「待本地验证」（取决于随机矩阵的非零元数值范围）。

## 6. 本讲小结

- H-B 文件 = **四行定宽文本头**（标题/key → 各段行数 → `MXTYPE`+维度 → 三个 Fortran 格式串）+ **三段数值数据**（列指针 / 行索引 / 值），本质是 CSC 存储的 1 基文本化。
- `HBMatrixType` 用三张字典把 `MXTYPE` 的三个字母（值类型 / 对称结构 / 存储形式）双向编码；scipy 只支持 `real`/`integer` + `unsymmetric` + `assembled` 这个子集。
- `FortranFormatParser` 是手写的「词法 + 语法」解析器，把 `(3E14.5)`、`(26I3)` 解析成 `ExpFormat`/`IntFormat`；其中 **`repeat` 字段决定每行读几个数**，`width` 决定每个数多宽。
- `HBInfo` 用 `from_file`（读头）与 `from_data`（写头时自动生成）两条互逆路径，`dump()` 负责序列化回四行文本；`_nbytes_full` 用 `repeat*width+1` 预算满行字节数。
- `_read_hb_data` 用「按字节读满行 + readline 读半行 + `np.fromstring` 切分」拿到三段，最后 `csc_array((val, ind-1, ptr-1))` 把 1 基转 0 基；`_write_data` 反向 `+1` 写出。
- `hb_read` / `hb_write` 是顶层公共入口，封装文件打开与 CSC 转换；`hb_read` 正在把默认返回类型从 `csc_matrix` 迁到 `csc_array`，用 `_NoValue` 哨兵精确区分「显式传参」与「未传参」。

## 7. 下一步学习建议

- **横向对比**：回到 u2-l3（Matrix Market），对照 `.mtx` 的 COO 三元组存储与本讲的 CSC 三段存储，理解「同样是稀疏矩阵交换格式，存储模型为何不同」。
- **跟进弃用机制**：本讲的 `hb_read`/`hb_write` 经顶层 `scipy.io.__init__.py` re-export，而 `scipy.io.harwell_boeing` 是无前缀的弃用包装。这一套 `_sub_module_deprecation` 机制将在 u4-l2（弃用命名空间）集中讲解。
- **稀疏返回类型迁移**：`spmatrix → sparray` 的迁移不止影响 H-B，u3-l3（loadmat/savemat）也会涉及类似的 `spmatrix` 关键字处理，建议两讲对照阅读。
- **继续阅读源码**：若想理解「为什么读取占内存、如何用编译代码加速」，可预习 u4-l1（Fast Matrix Market 的 C++ 后端），它展示了一种比 `np.fromstring` 更高效的定宽数值解析思路。
