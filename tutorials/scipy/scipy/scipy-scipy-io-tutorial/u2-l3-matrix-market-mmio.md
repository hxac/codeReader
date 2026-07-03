# Matrix Market 格式：纯 Python 实现（_mmio.py）

## 1. 本讲目标

学完本讲后，你应该能够：

- 读懂 Matrix Market（`.mtx`）文本文件的「文件头 + 注释 + 尺寸行 + 数据行」四段结构，并说出 `format` / `field` / `symmetry` 三个字段各自的取值含义。
- 理解 `scipy.io._mmio.MMFile` 这个纯 Python 类如何用一组常量（`FORMAT_*` / `FIELD_*` / `SYMMETRY_*`）把格式规范「代码化」，以及 `mminfo` / `mmread` / `mmwrite` 三个模块级函数如何转发到这个类。
- 跟踪 `MMFile.info` 解析文件头、`_parse_body` 按 `array`（稠密）或 `coordinate`（稀疏 COO）两种格式读正文、`_write` 与 `_get_symmetry` 自动探测对称性的完整调用链。
- 区分 `array` 与 `coordinate` 两种存储方式，并理解为什么对称矩阵只存下三角（含对角线）即可。

## 2. 前置知识

在进入源码前，先建立几个直觉。本讲承接 [u1-l3](u1-l3-quick-start-examples.md) 已经建立的两个认知：**命名空间包**（`scipy.io` 是命名空间，子模块要经 `scipy.io._mmio` 访问）和 **round-trip（往返一致性）**（写出去再读回来应当数值相等）。

### 什么是稀疏矩阵与 COO 格式

一个矩阵如果大多数元素是 0，就称为**稀疏矩阵**。只存非零元素的位置和值，可以大幅节省内存。COO（COOrdinate）是最直观的稀疏存储：用三个等长数组 `row`、`col`、`data` 记录每个非零元的行列下标和数值。例如

\[
A=\begin{bmatrix}0&0&3\\0&0&0\\5&0&0\end{bmatrix}
\]

在 COO 下表示为 `row=[0,2]`、`col=[2,0]`、`data=[3,5]`。Matrix Market 的 `coordinate` 格式就是 COO 的文本化版本。

### 什么是「对称性」

对方阵 \(A\)（\(m=n\)）有三种常见的对称关系：

- **对称**（symmetric）：\(A_{ij}=A_{ji}\)。
- **反对称 / 斜对称**（skew-symmetric）：\(A_{ij}=-A_{ji}\)，且对角线全为 0。
- **厄米特**（hermitian）：\(A_{ij}=\overline{A_{ji}}\)（共轭转置等于自身），实对称矩阵是它的特例。

利用对称性，只需要存储下三角（含对角线）的元素，上三角可由对称关系推导出来。这是 Matrix Market 节省存储的核心手段。

### 纯 Python 实现的含义

`scipy.io` 里 Matrix Market 有**两套实现**：本讲的 `_mmio.py` 是早期**纯 Python** 实现，逻辑清晰、便于阅读；`_fast_matrix_market`（C++ 后端）是 1.12 版之后加入的高性能实现（详见 [u4-l1](u4-l1-fast-matrix-market-cpp.md)）。顶层 `scipy.io.mmread` 等函数默认走 C++ 后端，但本讲聚焦纯 Python 版本——它是理解格式规范最好的教材，也是 C++ 后端的行为参考。两者的公共接口完全一致（测试里用同一个 fixture 同时跑两套实现）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [scipy/io/_mmio.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py) | Matrix Market 的纯 Python 实现。定义模块级函数 `mminfo` / `mmread` / `mmwrite` 和核心类 `MMFile`。本讲几乎全部内容都来自这个文件。 |
| [scipy/io/tests/test_mmio.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/tests/test_mmio.py) | Matrix Market 测试套件。它用一个 fixture（第 27 行）让同一批用例**同时**跑 `_mmio` 和 `_fast_matrix_market` 两套实现，可用来验证行为。 |

> 说明：与 WAV/IDL/NetCDF 等格式不同，仓库里**没有** `.mtx` 测试数据文件。Matrix Market 的测试和文档示例都用内存里的字符串（`io.StringIO`）或临时文件构造输入，因此本讲的实践也采用这种方式。

---

## 4. 核心概念与源码讲解

### 4.1 Matrix Market 格式规范与 MMFile 常量体系

#### 4.1.1 概念说明

Matrix Market（矩阵市场）是美国 NIST 维护的一种**纯文本**矩阵交换格式，扩展名通常是 `.mtx`。它的设计目标是「人能读、工具好解析」，常用于科学计算领域共享稀疏矩阵（例如经典的稀疏矩阵集 SuiteSparse）。

一个 `.mtx` 文件由四部分构成：

1. **首行（header）**：固定形如 `%%MatrixMarket matrix <format> <field> <symmetry>`，三个关键词描述矩阵的存储方式、数值类型和对称性。
2. **注释行**：以 `%` 开头，可任意多行，解析时跳过。
3. **尺寸行**：一行整数。
   - `array` 格式：`rows cols`。
   - `coordinate` 格式：`rows cols entries`（`entries` 是非零元个数）。
4. **数据行**：取决于 `format` 和 `field`。

三个关键词的取值：

| 关键词 | 取值 | 含义 |
|--------|------|------|
| `format` | `coordinate` / `array` | 稀疏（COO）/ 稠密（逐元素列存） |
| `field` | `real` / `complex` / `integer` / `unsigned-integer` / `pattern` | 数值类型；`pattern` 表示只有位置没有值（值为隐式 1） |
| `symmetry` | `general` / `symmetric` / `skew-symmetric` / `hermitian` | 是否利用对称性压缩存储 |

`pattern` 字段比较特殊：它只描述「哪些位置有非零元」，不记录具体数值，常用于图论里的邻接矩阵（只关心连边）。读取时这些位置的值被赋为 1。

#### 4.1.2 核心流程

`MMFile` 类用一组**类常量**把上面的规范固化下来，并在读写时用它们做校验。整个设计是「常量即规范」：

```
FORMAT_VALUES  = ('coordinate', 'array')
FIELD_VALUES   = ('integer', 'unsigned-integer', 'real', 'complex', 'pattern')
SYMMETRY_VALUES= ('general', 'symmetric', 'skew-symmetric', 'hermitian')
```

每个取值集合都配一个 `_validate_*` 校验方法：传入非法值就抛 `ValueError`。这样无论读还是写，只要走 `MMFile`，格式合法性就有保证。

`field` 到 numpy dtype 的映射由字典 `DTYPES_BY_FIELD` 完成，是读写两端「文本类型 ↔ 数组类型」的桥梁：

```
FIELD_INTEGER  -> 'intp'    （平台原生 int）
FIELD_UNSIGNED -> 'uint64'
FIELD_REAL     -> 'd'       （float64）
FIELD_COMPLEX  -> 'D'       （complex128）
FIELD_PATTERN  -> 'd'       （读出为 float64，值全 1）
```

#### 4.1.3 源码精读

模块的公共导出清单（[_mmio.py:L23](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L23)）：

```python
__all__ = ['mminfo', 'mmread', 'mmwrite', 'MMFile']
```

格式常量定义在 [_mmio.py:L291-L335](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L291-L335)，三组取值与校验、dtype 映射都在这里：

```python
# format values
FORMAT_COORDINATE = 'coordinate'
FORMAT_ARRAY = 'array'
FORMAT_VALUES = (FORMAT_COORDINATE, FORMAT_ARRAY)

# field values
FIELD_INTEGER = 'integer'
FIELD_UNSIGNED = 'unsigned-integer'
FIELD_REAL = 'real'
FIELD_COMPLEX = 'complex'
FIELD_PATTERN = 'pattern'
FIELD_VALUES = (FIELD_INTEGER, FIELD_UNSIGNED, FIELD_REAL, FIELD_COMPLEX,
                FIELD_PATTERN)

# symmetry values
SYMMETRY_GENERAL = 'general'
SYMMETRY_SYMMETRIC = 'symmetric'
SYMMETRY_SKEW_SYMMETRIC = 'skew-symmetric'
SYMMETRY_HERMITIAN = 'hermitian'
SYMMETRY_VALUES = (SYMMETRY_GENERAL, SYMMETRY_SYMMETRIC,
                   SYMMETRY_SKEW_SYMMETRIC, SYMMETRY_HERMITIAN)

DTYPES_BY_FIELD = {FIELD_INTEGER: 'intp',
                   FIELD_UNSIGNED: 'uint64',
                   FIELD_REAL: 'd',
                   FIELD_COMPLEX: 'D',
                   FIELD_PATTERN: 'd'}
```

校验方法（如 [_mmio.py:L296-L300](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L296-L300)）就是把值丢进取值元组里比对：

```python
@classmethod
def _validate_format(self, format):
    if format not in self.FORMAT_VALUES:
        msg = f'unknown format type {format}, must be one of {self.FORMAT_VALUES}'
        raise ValueError(msg)
```

`MMFile` 用 `__slots__` 限定实例只能有六个属性（[_mmio.py:L254-L259](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L254-L259)），分别对应文件头解析出的六个量：`_rows / _cols / _entries / _format / _field / _symmetry`，并通过只读 `@property` 暴露，避免外部误改。

#### 4.1.4 代码实践

**实践目标**：亲手写一段合法的 Matrix Market 文本，确认它的四段结构。

**操作步骤**：

1. 新建一个文本文件 `demo.mtx`，写入如下内容（一个 5×5、7 个非零元的稀疏实矩阵，来自 `mminfo` 的官方文档示例）：

   ```
   %%MatrixMarket matrix coordinate real general
   % 这是一段注释，会被解析时跳过
   5 5 7
   2 3 1.0
   3 4 2.0
   3 5 3.0
   4 1 4.0
   4 2 5.0
   4 3 6.0
   4 4 7.0
   ```

2. 对照本节讲的结构，标出：首行（`%%MatrixMarket matrix coordinate real general`）、注释行（`%` 开头）、尺寸行（`5 5 7`）、数据行（每行 `row col value`，注意是**1 基**下标）。

**需要观察的现象**：首行的四个字段分别对应 `matrix` / `coordinate` / `real` / `general`；尺寸行 `5 5 7` 表示 5 行 5 列、7 个非零元；数据行下标从 1 开始（最大下标 5 等于矩阵尺寸）。

**预期结果**：这段文本就是 `mminfo` 文档示例（[_mmio.py:L60-L79](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L60-L79)）所用的输入，下一节的 `mminfo` 会原样解析它。

#### 4.1.5 小练习与答案

**练习 1**：如果首行写成 `%%MatrixMarket matrix coordinate pattern general`，数据行还需要写数值吗？为什么？

> **参考答案**：不需要。`field=pattern` 表示只有位置、没有值。读取时这些位置的值会被赋为 1（见 [_mmio.py:L755-L756](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L755-L756)，`V = ones(entries, dtype='int8')`）。因此数据行只需 `row col` 两列。

**练习 2**：`FIELD_PATTERN` 映射到的 dtype 是 `'d'`（float64），但读取时 `V` 却初始化为 `int8`，这两者矛盾吗？

> **参考答案**：不矛盾。`DTYPES_BY_FIELD` 决定的是**最终** `coo_array` 的 dtype（float64）；而读取循环里临时用的 `V` 用 `int8` 只是为了省内存（值恒为 1），构造 `coo_array((V, (I, J)), ..., dtype=dtype)` 时会自动提升到 float64。

---

### 4.2 mminfo 与文件头解析（MMFile.info）

#### 4.2.1 概念说明

`mminfo(source)` 是「只看头、不读数据」的轻量函数：它只解析文件的前几行，返回一个六元组 `(rows, cols, entries, format, field, symmetry)`，告诉你这个矩阵长什么样、有多大、用什么方式存。它适合在真正读取大矩阵之前先探查一下规模（比如判断要不要稀疏存储）。

它本身只有一行，直接转发到 `MMFile.info`（[_mmio.py:L81](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L81)）。真正的解析逻辑在类方法 `info` 里。

#### 4.2.2 核心流程

`info` 的解析流程是一条**逐行状态机**：

```
1. 打开文件（_open：支持 .mtx / .mtx.gz / .mtx.bz2，也支持传入已打开的文件对象）
2. 读首行，split 成 5 段，校验是否以 '%%MatrixMarket' 开头、第二段是否为 'matrix'
3. 把 format 规范化（小写 -> 常量值 'array' 或 'coordinate'）
4. 跳过所有以 '%' 开头的注释行
5. 跳过空行
6. 读到第一个非注释非空行 = 尺寸行：
     - array    -> split 成 2 段：rows, cols ；entries = rows*cols
     - coordinate -> split 成 3 段：rows, cols, entries
7. 返回 (rows, cols, entries, format, field.lower(), symmetry.lower())
```

注意 `info` 只关心 `field` 和 `symmetry` 的**字符串值**（直接从首行取），并不校验它们是否合法——合法性校验发生在写路径和 `_validate_*` 里。

#### 4.2.3 源码精读

模块级转发函数 `mminfo`（[_mmio.py:L33-L81](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L33-L81)）：

```python
def mminfo(source):
    """..."""
    return MMFile.info(source)
```

类方法 `info` 的核心（[_mmio.py:L348-L424](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L348-L424)）。首行解析与校验（[_mmio.py:L380-L386](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L380-L386)）：

```python
line = stream.readline()
mmid, matrix, format, field, symmetry = \
    (asstr(part.strip()) for part in line.split())
if not mmid.startswith('%%MatrixMarket'):
    raise ValueError('source is not in Matrix Market format')
if not matrix.lower() == 'matrix':
    raise ValueError("Problem reading file header: " + line)
```

跳过注释行与空行的两个 `while` 循环（[_mmio.py:L396-L404](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L396-L404)）——注意它同时识别 `%` 字符和其 ASCII 码 37，因为读上来可能是 `bytes` 也可能是 `str`：

```python
while line:
    if line.lstrip() and line.lstrip()[0] in ['%', 37]:
        line = stream.readline()
    else:
        break
# skip empty lines
while not line.strip():
    line = stream.readline()
```

尺寸行的分支解析（[_mmio.py:L406-L417](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L406-L417)）：`array` 期望 2 段、`coordinate` 期望 3 段，段数不对就报错：

```python
if format == self.FORMAT_ARRAY:
    if not len(split_line) == 2:
        raise ValueError("Header line not of length 2: " + line.decode('ascii'))
    rows, cols = map(int, split_line)
    entries = rows * cols
else:
    if not len(split_line) == 3:
        raise ValueError("Header line not of length 3: " + line.decode('ascii'))
    rows, cols, entries = map(int, split_line)
```

文件打开辅助 `_open`（[_mmio.py:L427-L487](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L427-L487)）做了两件值得记住的事：(1) 用 `os.fspath` 区分「路径字符串」和「已打开的文件对象」，后者直接返回、不负责关闭；(2) 读路径下自动尝试 `.mtx` / `.mtx.gz` / `.mtx.bz2` 三种扩展名，并对 gzip/bz2 调用对应的标准库解压流。

#### 4.2.4 代码实践

**实践目标**：用 `mminfo` 解析上一节手写的 `demo.mtx`，并对照源码确认返回的六元组。

**操作步骤**：

```python
from io import StringIO
from scipy.io._mmio import mminfo

text = '''%%MatrixMarket matrix coordinate real general
 % 注释行
 5 5 7
 2 3 1.0
 3 4 2.0
 3 5 3.0
 4 1 4.0
 4 2 5.0
 4 3 6.0
 4 4 7.0
 '''

print(mminfo(StringIO(text)))
```

**需要观察的现象**：即使中间夹了一行注释、若干空格，`mminfo` 也能正确跳过，只返回头部元信息。

**预期结果**：输出 `(5, 5, 7, 'coordinate', 'real', 'general')`。这与官方文档断言（[_mmio.py:L78-L79](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L78-L79)）一致。

**额外验证**：把首行的 `coordinate` 改成 `array`、尺寸行改成 `2 4`，再调用 `mminfo`，应得到 `(2, 4, 8, 'array', 'real', 'general')`——注意 `array` 的 `entries` 被自动算成 `rows*cols`。

#### 4.2.5 小练习与答案

**练习 1**：把首行改成 `%%MatrixMarket vector coordinate real general`（`matrix` 改成 `vector`），调用 `mminfo` 会发生什么？

> **参考答案**：抛 `ValueError: Problem reading file header: ...`。因为 [_mmio.py:L385-L386](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L385-L386) 校验第二段必须小写等于 `'matrix'`，`scipy.io` 只支持矩阵，不支持 Matrix Market 的 vector/matrix 其他对象类型。

**练习 2**：为什么 `info` 跳过注释时要同时判断 `['%', 37]` 两种？

> **参考答案**：`stream` 可能是文本模式（读到 `str`，首字符是 `'%'`）也可能是二进制模式（读到 `bytes`，首字符是整数 ASCII 码 37，因为 `'%'` 的 ASCII 就是 37）。`_open` 默认以 `'rb'` 打开文件，所以读上来往往是 `bytes`。两种都判断才能兼容。

---

### 4.3 mmread 与正文解析（_parse_body）

#### 4.3.1 概念说明

`mmread(source)` 把整个 `.mtx` 读成一个矩阵对象。返回类型取决于文件里的 `format`：

- `array`（稠密）→ 返回 numpy `ndarray`。
- `coordinate`（稀疏）→ 返回 `scipy.sparse.coo_array`（或 `coo_matrix`，见下文 `spmatrix` 参数）。

`mmread` 同样只是转发到 `MMFile().read(source)`（[_mmio.py:L140](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L140)），`read` 再分两步：`_parse_header`（复用 `info`）填好六个属性，`_parse_body` 按格式读数据。

#### 4.3.2 核心流程

`_parse_body` 根据 `format` 走两条不同的分支。

**array 分支**（稠密，列主序逐元素读）：Matrix Market 的 `array` 格式按**列**存放所有元素（先第 0 列自上而下，再第 1 列……）。代码用一个 `(i, j)` 游标按这个顺序填一个 `zeros((rows, cols))` 数组。若文件声明了对称性，读到下三角元素 `(i, j)` 时，顺便按对称关系把镜像位置 `(j, i)` 也填上：

\[
\text{symmetric: } A_{ji}=A_{ij},\quad
\text{skew: } A_{ji}=-A_{ij},\quad
\text{hermitian: } A_{ji}=\overline{A_{ij}}
\]

**coordinate 分支**（稀疏 COO）：预分配三个长度为 `entries` 的数组 `I / J / V`，逐行读 `row col [value]`。关键三步：

1. 文件里的下标是 **1 基**，读完统一 `I -= 1; J -= 1` 转成 0 基（[_mmio.py:L792-L793](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L792-L793)）。
2. 若有对称性，把「非对角线」的下三角元素镜像出一份补到上三角（`symmetric` 直接复制、`skew` 取负、`hermitian` 取共轭），见 [_mmio.py:L795-L809](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L795-L809)。
3. 用 `(V, (I, J))` 构造 `coo_array`。

`pattern` 字段没有值列，`V` 初始化为全 1。

#### 4.3.3 源码精读

`read` 方法（[_mmio.py:L569-L618](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L569-L618)）先解析头和正文：

```python
stream, close_it = self._open(source)
try:
    self._parse_header(stream)
    data = self._parse_body(stream)
finally:
    if close_it:
        stream.close()
```

末尾有一段 `spmatrix` 弃用处理（[_mmio.py:L604-L618](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L604-L618)）：默认会把稀疏结果包成 `coo_matrix`（旧的稀疏矩阵），但 1.18 起弃用，1.20 将默认返回 `coo_array`（稀疏数组）。想要今天就拿到数组，传 `mmread(src, spmatrix=False)`。

coordinate 分支读循环（[_mmio.py:L746-L793](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L746-L793)），跳注释、按下标填数组、超出 `entries` 报错：

```python
I = zeros(entries, dtype='intc')
J = zeros(entries, dtype='intc')
# ... 按 field 选 V 的 dtype ...
for line in stream:
    if not line or line[0] in ['%', 37] or not line.strip():
        continue
    if entry_number+1 > entries:
        raise ValueError("'entries' in header is smaller than number of entries")
    l = line.split()
    I[entry_number], J[entry_number] = map(int, l[:2])
    if not is_pattern:
        # ... 按 field 解析 V[entry_number] ...
    entry_number += 1
if entry_number < entries:
    raise ValueError("'entries' in header is larger than number of entries")

I -= 1  # adjust indices (base 1 -> base 0)
J -= 1
```

对称性镜像展开（[_mmio.py:L795-L809](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L795-L809)）：先用 `mask = (I != J)` 选出非对角元，再拼接：

```python
if has_symmetry:
    mask = (I != J)       # off diagonal mask
    od_I = I[mask]
    od_J = J[mask]
    od_V = V[mask]

    I = concatenate((I, od_J))
    J = concatenate((J, od_I))

    if is_skew:
        od_V *= -1
    elif is_herm:
        od_V = od_V.conjugate()

    V = concatenate((V, od_V))

a = coo_array((V, (I, J)), shape=(rows, cols), dtype=dtype)
```

这段很巧妙：镜像元的行下标用原来的列下标 `od_J`、列下标用原来的行下标 `od_I`，正好实现转置位置；值则按对称类型取负或共轭。对角元（`I == J`）不镜像，避免重复。

#### 4.3.4 代码实践

**实践目标**：读回 4.1 节的 `coordinate real general` 文本，观察读出的稀疏矩阵与原文一一对应。

**操作步骤**：

```python
from io import StringIO
from scipy.io._mmio import mmread

text = '''%%MatrixMarket matrix coordinate real general
 5 5 7
 2 3 1.0
 3 4 2.0
 3 5 3.0
 4 1 4.0
 4 2 5.0
 4 3 6.0
 4 4 7.0
 '''

m = mmread(StringIO(text), spmatrix=False)   # 拿到 coo_array，避免弃用警告
print(type(m))
print(m.toarray())
```

**需要观察的现象**：文件里下标是 1 基（`2 3` 表示第 2 行第 3 列），读出后变成 0 基（`toarray()` 里 `1.0` 出现在 `[1, 2]` 位置）。

**预期结果**：与官方文档（[_mmio.py:L131-L138](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L131-L138)）一致：

```
array([[0., 0., 0., 0., 0.],
       [0., 0., 1., 0., 0.],
       [0., 0., 0., 2., 3.],
       [4., 5., 6., 7., 0.],
       [0., 0., 0., 0., 0.]])
```

**额外验证（对称展开）**：构造一个 `coordinate real symmetric` 的最小文本，只写下三角：

```
%%MatrixMarket matrix coordinate real symmetric
 3 3 4
 1 1 2.0
 2 1 3.0
 3 1 5.0
 3 3 7.0
```

读回后 `toarray()` 应得到一个完整对称矩阵（`[1,0]` 和 `[0,1]` 都是 3.0，`[2,0]` 和 `[0,2]` 都是 5.0），非零元个数从 4 翻倍成 7（对角元 1 个不镜像 + 非对角 3 个镜像 = 4+3）。**待本地验证**：你亲手 `mmread(...).nnz` 应等于 7。

#### 4.3.5 小练习与答案

**练习 1**：为什么 coordinate 读完后要做 `I -= 1; J -= 1`，而 array 分支不需要？

> **参考答案**：Matrix Market 规定 coordinate 格式的下标是 1 基（数学习惯），而 numpy/scipy 用 0 基，所以要整体减 1（[_mmio.py:L792-L793](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L792-L793)）。array 分支不存下标——元素的位置完全由「列主序读取顺序」隐式决定，没有下标需要平移。

**练习 2**：如果文件头声明 `entries=7`，但正文只写了 5 个非零元，`mmread` 会怎样？

> **参考答案**：抛 `ValueError("'entries' in header is larger than number of entries")`。读循环结束后会检查 `entry_number < entries`（[_mmio.py:L788-L790](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L788-L790)）。反过来正文比声明多，则在循环内（[_mmio.py:L772-L774](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L772-L774)）抛「smaller than」的错。

---

### 4.4 mmwrite 与对称性自动探测（_write + _get_symmetry）

#### 4.4.1 概念说明

`mmwrite(target, a, comment='', field=None, precision=None, symmetry=None)` 把一个稠密 `ndarray` 或稀疏矩阵 `a` 写成 `.mtx`。它最智能的地方在于**自动推断**：调用者不必指定 `field` 和 `symmetry`，函数会看数组本身的 dtype 和数值关系来决定。

- **格式（format）推断**：输入是稀疏矩阵（`scipy.sparse`）→ 写 `coordinate`；否则（list/ndarray/有 `__array__`）→ 写 `array`。
- **字段（field）推断**：按 numpy dtype 的 `kind`：`'i'→integer`、`'u'→unsigned-integer`、`'f'→real`、`'c'→complex`。
- **对称性（symmetry）推断**：调用 `_get_symmetry(a)` 实地检查矩阵是否满足对称/反对称/厄米特关系。

#### 4.4.2 核心流程

写入主流程（`_write`）：

```
1. 判断 format：稀疏 -> coordinate；否则 array（并校验是 2 维）
2. 若调用者给了 field，按 field 强制转换 dtype；否则由 kind 推断 field
3. 若 precision 为 None：float32 用 8 位有效数字，其余用 16 位
4. 若 symmetry 为 None：调用 _get_symmetry(a) 自动探测
5. 校验 format/field/symmetry 合法
6. 写首行 '%%MatrixMarket matrix <format> <field> <symmetry>'
7. 写注释（每行前加 '%'）
8. 按 format 写尺寸行 + 数据行
```

`_get_symmetry` 的探测逻辑：对方阵同时维护三个布尔旗标 `issymm / isskew / isherm`，遍历下三角的每对 `(A[i,j], A[j,i])`，逐对收紧：

\[
\text{issymm} \mathrel{-\!\!=} (A_{ij}\ne A_{ji}),\quad
\text{isskew} \mathrel{-\!\!=} (A_{ij}\ne -A_{ji}),\quad
\text{isherm} \mathrel{-\!\!=} (A_{ij}\ne \overline{A_{ji}})
\]

只要三个旗标全灭就提前 `break`。最后按 **symmetric > skew > hermitian > general** 的优先级返回（因为实对称矩阵同时满足 hermitian，但「symmetric」更具体）。

写对称矩阵时（[_mmio.py:L942-L947](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L942-L947)），只保留下三角（`coo.row >= coo.col`），上三角元素丢弃——因为读回时会由对称关系重建。

#### 4.4.3 源码精读

`mmwrite` 转发到 `MMFile().write(...)`（[_mmio.py:L145-L249](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L145-L249)），后者再调 `_write`（[_mmio.py:L818-L969](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L818-L969)）。

format 与 field 推断（[_mmio.py:L820-L870](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L820-L870)）：

```python
if isinstance(a, list) or isinstance(a, ndarray) or \
   isinstance(a, tuple) or hasattr(a, '__array__'):
    rep = self.FORMAT_ARRAY
    a = asarray(a)
    if len(a.shape) != 2:
        raise ValueError('Expected 2 dimensional array')
    ...
else:
    if not issparse(a):
        raise ValueError(f'unknown matrix type: {type(a)}')
    rep = 'coordinate'
```

```python
if field is None:
    kind = a.dtype.kind
    if kind == 'i':
        ...
        field = 'integer'
    elif kind == 'f':
        field = 'real'
    elif kind == 'c':
        field = 'complex'
    elif kind == 'u':
        field = 'unsigned-integer'
    else:
        raise TypeError('unexpected dtype kind ' + kind)

if symmetry is None:
    symmetry = self._get_symmetry(a)
```

对称性探测 `_get_symmetry`（[_mmio.py:L490-L552](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L490-L552)）。注意非方阵直接判 `general`，且 `isherm` 只有复数 dtype（`'FD'`）才初始为 True：

```python
m, n = a.shape
if m != n:
    return MMFile.SYMMETRY_GENERAL
issymm = True
isskew = True
isherm = a.dtype.char in 'FD'
```

稀疏输入会先做一个快速预筛——下三角与上三角非零元个数不等就一定是 `general`（[_mmio.py:L500-L506](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L500-L506)），避免昂贵的逐元素比较：

```python
if issparse(a):
    a = a.tocoo()
    (row, col) = a.nonzero()
    if (row < col).sum() != (row > col).sum():
        return MMFile.SYMMETRY_GENERAL
```

返回优先级（[_mmio.py:L546-L552](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L546-L552)）：

```python
if issymm:
    return MMFile.SYMMETRY_SYMMETRIC
if isskew:
    return MMFile.SYMMETRY_SKEW_SYMMETRIC
if isherm:
    return MMFile.SYMMETRY_HERMITIAN
return MMFile.SYMMETRY_GENERAL
```

数值格式化由 `_field_template` 按 field + precision 生成 printf 风格模板（[_mmio.py:L555-L562](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L555-L562)），例如 real 用 `'%.16e\n'`、complex 用 `'%.16e %.16e\n'`（实部虚部空格分隔）。

写对称稀疏时丢上三角（[_mmio.py:L942-L947](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L942-L947)）：

```python
if symmetry != self.SYMMETRY_GENERAL:
    lower_triangle_mask = coo.row >= coo.col
    coo = coo_array((coo.data[lower_triangle_mask],
                    (coo.row[lower_triangle_mask],
                     coo.col[lower_triangle_mask])),
                    shape=coo.shape)
```

#### 4.4.4 代码实践

**实践目标**：构造一个 5×5 实对称矩阵，用 `mmwrite` 写出（观察 symmetry 字段被自动判为 `symmetric`），再 `mmread` 读回比较；同时完成规格要求的手写 coordinate 文本 + `mminfo` 解析。

**操作步骤**：

```python
import numpy as np
from io import BytesIO, StringIO
from scipy.io._mmio import mmwrite, mmread, mminfo

# 1) 构造对称矩阵（下三角随机，再 A = L + L.T - diag）
L = np.tril(np.array([[2, 0, 0, 0, 0],
                      [1, 3, 0, 0, 0],
                      [4, 0, 5, 0, 0],
                      [0, 6, 0, 7, 0],
                      [8, 0, 9, 0, 10.0]]))
A = L + L.T - np.diag(np.diag(L))   # 严格对称

buf = BytesIO()
mmwrite(buf, A, precision=3)
print(buf.getvalue().decode('latin1'))     # 观察首行 symmetry 字段

# 2) 读回比较
buf.seek(0)
B = mmread(buf, spmatrix=False)
print("round-trip 一致：", np.allclose(A, B))

# 3) 手写一段 coordinate 文本，用 mminfo 解析元信息
coord_text = '''%%MatrixMarket matrix coordinate integer general
 4 4 5
 1 1 1
 2 2 2
 3 3 3
 4 4 4
 2 4 9
 '''
print(mminfo(StringIO(coord_text)))
```

**需要观察的现象**：

- 第 1 步打印出的文件首行应为 `%%MatrixMarket matrix array real symmetric`（因为输入是稠密 ndarray → `array`；实数 → `real`；满足对称 → `symmetric`）。注意 `array` 格式按列主序写出，且因对称只写下三角（含对角），数据行数 = \(5+4+3+2+1=15\)。
- 第 2 步 `np.allclose` 为 `True`。
- 第 3 步 `mminfo` 返回 `(4, 4, 5, 'coordinate', 'integer', 'general')`。

**预期结果**：以上三项均成立。`mmwrite` 的官方文档示例（[_mmio.py:L179-L246](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L179-L246)）展示了 array/coordinate/hermitian 三种写法下的精确文本输出，可对照核对你看到的首行与数据排列。

> 若想观察稀疏对称只存下三角，把上面 `A` 转成 `scipy.sparse.coo_array(A)` 再 `mmwrite`：首行会变成 `coordinate real symmetric`，正文只写下三角非零元，下标 1 基。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：把一个复数厄米特矩阵（`z = z.conj().T`）传给 `mmwrite`，不指定 `symmetry`，首行的 symmetry 会是什么？为什么不是 `symmetric`？

> **参考答案**：会是 `hermitian`。`_get_symmetry` 里 `issymm` 对复矩阵通常很快变 False（因为 `A[i,j] != A[j,i]` 当虚部非零时成立），但 `isherm`（`A[i,j] == conj(A[j,i])`）保持 True。即使两个都为真，[_mmio.py:L546-L552](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L546-L552) 的优先级是 symmetric 先判断——但对严格厄米特（非实对称）的复矩阵，`issymm` 为 False，所以落到 `hermitian`。实对称矩阵才会被判为 `symmetric`。

**练习 2**：`mmwrite` 对一个 `int64` 且数值超过 `intp` 范围（在 32 位平台上）的数组会怎样？

> **参考答案**：抛 `OverflowError("mmwrite does not support integer dtypes larger than native 'intp'.")`。见 [_mmio.py:L831-L833](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L831-L833) 与 [_mmio.py:L858-L862](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L858-L862)，用 `can_cast(a.dtype, 'intp')` 做容量检查。

**练习 3**：为什么写对称稀疏矩阵时，源码用 `coo.row >= coo.col`（下三角含对角）而不是 `>`（严格下三角）？

> **参考答案**：因为对角线元素必须保留。反对称矩阵的对角线理论上是 0（代码里读时强制填 0），但对称/厄米特矩阵的对角线元素是真实数据。`>=` 把对角元一起留下，读回时对角元不再被镜像（`mask = (I != J)` 排除了对角），避免重复。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「手写 → 探测 → 写入 → 读回」的完整闭环。

**任务**：用一段最小的 Python 脚本，验证 `_mmio.py` 的核心行为。

```python
import numpy as np
from io import StringIO
from scipy.io._mmio import mminfo, mmread, mmwrite

# (a) 手写一个 hermitian 复矩阵的 coordinate 文本（只写下三角，1 基下标）
herm_text = '''%%MatrixMarket matrix coordinate complex hermitian
 3 3 4
 1 1 2.0 0.0
 2 1 1.0 2.0
 3 1 4.0 -3.0
 3 3 2.5 0.0
 '''
print("mminfo =", mminfo(StringIO(herm_text)))
H = mmread(StringIO(herm_text), spmatrix=False)
print("H =\n", H.toarray())
print("是否厄米特：", np.allclose(H.toarray(), H.toarray().conj().T))

# (b) 用 mmwrite 写一个实对称稠密矩阵，观察首行
A = np.array([[2, 1, 4],
              [1, 3, 0],
              [4, 0, 5.0]])
import io as _io
buf = _io.BytesIO()
mmwrite(buf, A, precision=2)
print("\n--- 写出的 .mtx ---")
print(buf.getvalue().decode('latin1'))

# (c) 读回并比对
buf.seek(0)
B = mmread(buf, spmatrix=False)
print("round-trip 一致：", np.allclose(A, B))
```

**需要观察与解释的要点**：

1. **(a)** 读回的 `H` 是 3×3 复矩阵；下三角 4 个元素经 hermitian 展开后补出上三角（`[0,1]` 由 `[1,0]=1+2j` 取共轭得到 `1-2j`），对角元不重复。`H` 满足 `H == H.conj().T`。
2. **(b)** 写出的首行为 `%%MatrixMarket matrix array real symmetric`；因对称只写下三角（含对角），数据按**列主序**排列，共 \(3+2+1=6\) 个数。
3. **(c)** round-trip 一致为 `True`。

这个任务一次性覆盖了：文件头四段结构（4.1）、`mminfo` 头解析（4.2）、`mmread` 正文解析与对称展开（4.3）、`mmwrite` 自动推断对称性（4.4）。

## 6. 本讲小结

- Matrix Market 是**纯文本**矩阵交换格式，文件分四段：首行 `%%MatrixMarket matrix <format> <field> <symmetry>`、注释行、尺寸行、数据行。
- `MMFile` 用 `FORMAT_*` / `FIELD_*` / `SYMMETRY_*` 三组类常量把格式规范代码化，配 `_validate_*` 校验；`DTYPES_BY_FIELD` 把 field 字符串映射到 numpy dtype。
- `mminfo` / `mmread` / `mmwrite` 三个模块级函数都只是转发到 `MMFile.info` / `MMFile().read` / `MMFile().write`，真正逻辑在类方法里。
- `array`（稠密，列主序逐元素）与 `coordinate`（稀疏 COO，1 基下标）是两条不同的解析/写入分支；coordinate 读完后 `I -= 1; J -= 1` 转 0 基。
- 对称性（symmetric / skew / hermitian）只存下三角含对角，读回时由镜像关系补全上三角（取负或共轭），写时丢上三角。
- `_get_symmetry` 实地探测三个对称旗标，按 symmetric > skew > hermitian > general 的优先级返回；`mmwrite` 据此自动决定 `field` 与 `symmetry`，调用者通常无需手填。

## 7. 下一步学习建议

- 本讲的 `_mmio.py` 是**纯 Python** 实现，逻辑清晰但速度有限。下一站建议读 [u4-l1](u4-l1-fast-matrix-market-cpp.md)，看 `scipy.io._fast_matrix_market` 的 C++ 后端如何用多线程大幅加速 `mmread` / `mmwrite`，以及它与纯 Python 版本如何共用同一套测试（`test_mmio.py` 的 fixture）。
- 如果你想看另一种「文本 + 稀疏」格式，可对比 [u2-l6](u2-l6-harwell-boeing.md) 的 Harwell-Boeing 格式——它同样存稀疏矩阵，但用 Fortran format 字符串驱动解析，与 Matrix Market 的逐行 split 思路形成对照。
- 想理解 scipy.io 整体的公共/私有模块约定（为什么实现藏在 `_mmio`、顶层 `mmio.py` 只是弃用包装），可回到 [u1-l2](u1-l2-directory-and-build.md)。
- 直接阅读源码时，建议按本讲的顺序：先读 [_mmio.py:L291-L335](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L291-L335)（常量）→ `info`（[_mmio.py:L348-L424](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L348-L424)）→ `_parse_body`（[_mmio.py:L682-L815](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L682-L815)）→ `_write`（[_mmio.py:L818-L969](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L818-L969)），最后再看 `_get_symmetry`（[_mmio.py:L490-L552](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/_mmio.py#L490-L552)），这条线最符合「先规范、再读、再写、最后探测」的认知顺序。
