# 字符串 dtype 与数组基础

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `numpy.char` 操作所依赖的字符串 `dtype` 家族：`str_`、`bytes_`、`character`、`object_`，以及它们各自的存储方式。
- 解释「定宽（fixed-width）字符串」的含义，并能根据字符数推算出一个 `str_` 或 `bytes_` 数组的 `itemsize`（每个元素占多少字节）。
- 读懂 `chararray` 构造函数与 `char.array()` 工厂函数中关于 `itemsize` / `unicode` 的源码逻辑，尤其是源码里那句“对 Unicode 要除以 4”的注释。
- 理解 `object` 数组与定宽字符串数组的取舍，知道为什么官方推荐用 `str_` / `bytes_` 而不是 `chararray`。

本讲承接 [u1-l1](u1-l1-module-overview.md) 建立的「char（门面）→ defchararray（实现）→ strings（现代 ufunc）」三层模型，把视线从“模块怎么导出”下沉到“字符串到底在内存里长什么样”。

## 2. 前置知识

- **dtype（数据类型）**：NumPy 数组里每个元素的类型。比如 `int64`、`float64`、`bool`。本讲关注的是“字符串类”的 dtype。
- **itemsize**：一个数组元素在内存中占用的**字节数**。可以通过 `arr.itemsize` 读取。
- **定宽（fixed-width）**：每个元素占用的字节数相同。比如一个“最多 5 个字符”的字符串数组，不管你存的是 `"a"` 还是 `"abcde"`，每个槽位都占同样大小的内存，短的用空白补齐。
- **字节序字符**：dtype 字符串里的小写字母前缀，例如 `<` 表示小端（little-endian），`|` 表示“不适用字节序”（单字节类型）。`<U5` 里的 `U` 表示 Unicode 字符串，`|S3` 里的 `S` 表示字节串（bytes-string）。
- **UCS-4**：NumPy 在内存里存放 `str_`（Unicode）字符串时，每个码点（character）固定用 **4 个字节**编码，而不是可变长度。这是本讲“为什么是 4 倍”的关键。

如果你对 `np.array`、`dtype`、`itemsize` 完全陌生，建议先动手运行 `np.array([1,2,3]).dtype` 和 `np.array([1,2,3]).itemsize` 建立直觉，再继续往下读。

## 3. 本讲源码地图

本讲几乎只围绕一个文件展开：

| 文件 | 作用 |
| --- | --- |
| [numpy/_core/defchararray.py](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py) | `numpy.char` 的真正实现。本讲关注其中的字符串 dtype 类型导入、`chararray` 构造函数、以及 `array()` 工厂里 `itemsize` 的自动推断逻辑。 |
| [numpy/char/\_\_init\_\_.py](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/__init__.py) | 门面模块。本讲只用它来回顾 `chararray` 在 2.5 被弃用的背景。 |

> 提醒：本讲用到的“数组 dtype”是通过普通的 `np.array(..., dtype=...)` 创建的**普通 ndarray**，它**不会**触发 `chararray` 的弃用警告。`chararray` / `np.char.array` / `np.char.asarray` 在 NumPy 2.5 已被弃用，访问时会发出 `DeprecationWarning`。本讲末尾的“迁移建议”会再次强调这一点。

## 4. 核心概念与源码讲解

### 4.1 字符串 dtype 家族（dtype 基础）

#### 4.1.1 概念说明

NumPy 里能装“文本”的 dtype 不止一种。理解它们的差异，是用好 `numpy.char` 的前提：

| dtype | 类型对象 | 含义 | 每字符字节数 |
| --- | --- | --- | --- |
| `str_`（`<U`） | `numpy.str_` | 定宽 Unicode 字符串，UCS-4 编码 | **4** |
| `bytes_`（`|S`） | `numpy.bytes_` | 定宽字节串（8-bit） | **1** |
| `object_`（`O`） | Python 对象 | 每个元素是一个 Python `str`/`bytes` 对象指针 | 不定长 |
| `character` | 抽象父类 | `str_` 和 `bytes_` 的共同基类，仅用于 `isinstance` 判断 | —— |

`defchararray` 在文件顶部就明确导入了这几个类型，它们是整个模块的“地基”：

```python
from .numerictypes import bytes_, character, str_
```

——这是 [defchararray.py:38](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L38)，把 `bytes_`、`character`、`str_` 三个类型对象引入当前模块。

此外，模块文档里早就给出了官方建议：要用字符串数组，优先选 `object_`、`bytes_`、`str_`，再用 `numpy.char` 的自由函数做向量化操作（而不是用 `chararray`）：

> [defchararray.py:6-10](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L6-L10)：建议使用 `dtype` 为 `object_`、`bytes_` 或 `str_` 的数组，并搭配 `numpy.char` 自由函数。

> 📌 补充：NumPy 还有一个更新的 `StringDType`（变长字符串 dtype），在 `partition` 等函数的文档里会出现。它和本讲的定宽 `str_`/`bytes_` 不同，属于更现代的方案，本讲不展开。

#### 4.1.2 核心流程

判断一个数组“是不是字符串数组”的常用判据：

```text
arr.dtype.type 是 str_ / bytes_ 的子类吗？
  ├─ 是  → 是“定宽字符数组”，可被 numpy.char / numpy.strings 的 ufunc 高效处理
  └─ 否  → 若 dtype.type 是 object_，则是 object 数组（元素是 Python 对象指针）
```

在源码里，`character` 这个抽象基类正是用来做这种判断的：`issubclass(arr.dtype.type, character)` 为真，就说明这是一个定宽字符串（`str_` 或 `bytes_`）数组。

#### 4.1.3 源码精读

`defchararray` 模块的文档开头（[defchararray.py:1-17](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1-L17)）概括了整个模块的定位：一组向量化字符串操作函数，并且强调 `chararray` 仅为兼容旧版 Numarray 而保留。

`__all__` 列出了所有公共函数（[defchararray.py:40-50](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L40-L50)），可以看到名字几乎都是字符串方法（`upper`、`center`、`find`、`split` …），它们都期望输入是 `str_` 或 `bytes_` dtype 的数组。

#### 4.1.4 代码实践

1. **实践目标**：亲手看到四种“文本 dtype”长什么样。
2. **操作步骤**：

   ```python
   import numpy as np

   for dt in [np.str_, np.bytes_, object]:
       a = np.array(["ab", "cd"], dtype=dt)
       print(dt, "| dtype =", a.dtype, "| dtype.type =", a.dtype.type)
   ```

3. **需要观察的现象**：三次循环分别打印出 `str_` / `bytes_` / `object` 对应的 dtype 字符串。
4. **预期结果**：`str_` 显示为 `<U2`，`bytes_` 显示为 `|S2`，`object` 显示为 `object`。`dtype.type` 分别是 `<class 'numpy.str_'>`、`<class 'numpy.bytes_'>`、`<class 'object'>`。
5. 如果你的 NumPy 版本显示略有差异，以本地实际输出为准（待本地验证）。

#### 4.1.5 小练习与答案

- **练习 1**：`np.array(["x"]).dtype.type` 是 `character` 的子类吗？
  - **答案**：是。`str_` 继承自 `character`，`issubclass(np.str_, np.character)` 为 `True`。
- **练习 2**：为什么 `numpy.char` 的函数大多要求输入是 `str_`/`bytes_`，而不是 `object`？
  - **答案**：因为 `str_`/`bytes_` 是定宽的连续内存，能在 C 层用 ufunc 做向量化；`object` 数组里只是 Python 对象指针，无法走快速路径。

---

### 4.2 定宽字符串的内存布局与 itemsize（dtype 基础）

#### 4.2.1 概念说明

“定宽”是 `str_` / `bytes_` dtype 最核心的特征：**整个数组的每个元素都占用同样多的字节**，这个字节数就是 `itemsize`。

- 对 `bytes_`（`|S`）：每个字符 1 字节。`|S3` 表示每个元素最多 3 个字节 → `itemsize = 3`。
- 对 `str_`（`<U`）：每个码点（Unicode 字符）**固定 4 字节**（UCS-4）。`<U5` 表示每个元素最多 5 个字符 → `itemsize = 5 × 4 = 20` 字节。

所以同样的“字符数”，Unicode 的字节数是字节串的 **4 倍**。这正是本讲要解释的关键现象。

短于槽位的字符串会被**右侧补空字符**（`str_` 补 `\x00` 空字符，`bytes_` 同理），超长的会被**截断**。这就是“定宽”的代价：要么浪费空间，要么丢失内容。

#### 4.2.2 核心流程

`itemsize` 与字符数的关系可以写成一个公式：

\[
\text{itemsize}(\text{字节}) = \text{字符数} \times c,\qquad
c = \begin{cases} 4 & \text{若 dtype 为 } \texttt{str\_} \text{（UCS-4）}\\[2pt] 1 & \text{若 dtype 为 } \texttt{bytes\_} \end{cases}
\]

举两个本讲实践会遇到的例子：

\[
\texttt{<U5}:\quad \text{itemsize} = 5 \times 4 = 20 \text{ 字节}
\]

\[
\texttt{|S3}:\quad \text{itemsize} = 3 \times 1 = 3 \text{ 字节}
\]

内存布局示意（`<U5` 数组，2 个元素，每个 20 字节，连续存放）：

```text
[ 元素0: 20 字节（最多 5 个 UCS-4 码点，右侧补 \x00） ][ 元素1: 20 字节 ][ ... ]
```

#### 4.2.3 源码精读

`itemsize` 的“4 倍”关系在 `array()` 工厂函数里有一段最直白的注释。当输入已经是一个 `str_` 数组、且调用者没有显式指定 `itemsize` 时，源码会把底层 ndarray 的 `itemsize`（字节数）“换算”回字符数，于是除以 4：

```python
if itemsize is None:
    itemsize = obj.itemsize
    # itemsize is in 8-bit chars, so for Unicode, we need
    # to divide by the size of a single Unicode character,
    # which for NumPy is always 4
    if issubclass(obj.dtype.type, str_):
        itemsize //= 4
```

——[defchararray.py:1319-1325](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1319-L1325)。

注意这里有两套“itemsize 语义”在打架，读懂这段就能彻底理解本讲：

- 底层 ndarray 的 `obj.itemsize`：**字节数**。`<U5` 的 ndarray，`itemsize` 是 20。
- `chararray` 构造函数与 `array()` 的 `itemsize` 形参：**字符数**。`chararray((3,), itemsize=5)` 表示每个元素 5 个字符。

所以源码要把“字节”除以 4，换算成“字符”，才能传给 `chararray` 构造函数。注释里的 “for NumPy is always 4” 就是 UCS-4 的固定 4 字节/字符。

#### 4.2.4 代码实践（本讲主实践）

1. **实践目标**：构造 `<U5` 与 `|S3` 两个数组，实测 `itemsize`，并对照源码注释解释“4 倍”。
2. **操作步骤**：

   ```python
   import numpy as np

   u = np.array(["abcde", "xy"], dtype="<U5")   # Unicode，最多 5 字符
   b = np.array([b"abc", b"x"], dtype="|S3")     # bytes，最多 3 字符

   print("U5 dtype:", u.dtype, "itemsize:", u.itemsize, "nbytes:", u.nbytes)
   print("S3 dtype:", b.dtype, "itemsize:", b.itemsize, "nbytes:", b.nbytes)
   print("倍数:", u.itemsize / b.itemsize * (3/5))  # 折算成“每字符字节”
   print("str_ 每字符字节:", u.itemsize / 5)
   print("bytes_ 每字符字节:", b.itemsize / 3)
   ```

3. **需要观察的现象**：`u.itemsize` 与 `b.itemsize` 的具体数值；以及“每字符字节”分别是多少。
4. **预期结果**：
   - `u.itemsize == 20`（5 字符 × 4 字节），`u.nbytes == 40`（2 个元素）。
   - `b.itemsize == 3`（3 字符 × 1 字节），`b.nbytes == 6`（2 个元素）。
   - `str_` 每字符 4.0 字节，`bytes_` 每字符 1.0 字节 —— 正好 4 倍。
5. **结合源码的解释**：`str_` 在 NumPy 内部用 UCS-4，每个 Unicode 码点固定占 4 字节；`bytes_` 是 8-bit，每个字符 1 字节。因此同样字符数下，`str_` 的 `itemsize` 是 `bytes_` 的 4 倍，这与 [defchararray.py:1321-1324](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1321-L1324) 的注释“which for NumPy is always 4”完全一致。
6. 数值是源码逻辑确定的，但请以本地运行结果为准（若本地结果与预期不符，多半是输入字符串长度与 dtype 不一致导致）。

#### 4.2.5 小练习与答案

- **练习 1**：`np.array(["你好"], dtype="<U2").itemsize` 是多少？（“你好”是 2 个字符）
  - **答案**：`2 × 4 = 8` 字节。Unicode 字符数与具体字符无关，都是按码点数 × 4 计算。
- **练习 2**：把 `"abcdefghij"`（10 字符）存进 `dtype="<U5"` 会怎样？
  - **答案**：会被**截断**为 5 个字符 `"abcde"`，因为定宽槽位只放得下 5 个码点。

---

### 4.3 chararray 构造函数源码精读（chararray 构造）

#### 4.3.1 概念说明

`chararray` 是 `ndarray` 的一个子类，专门用来“方便地查看”字符串/Unicode 数组。它在普通 `str_`/`bytes_` 数组之上加了三件事（见 [defchararray.py:424-434](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L424-L434)）：

1. 取值（索引）时自动剥离元素**末尾空白**；
2. 比较运算符自动剥离末尾空白后再比较；
3. 把字符串方法（`.endswith` 等）和中缀运算符（`+`、`*`、`%`）作为向量化操作暴露出来。

> ⚠️ `chararray` 在 NumPy 2.5 已弃用（见 [defchararray.py:412-414](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L412-L414)），访问 `np.char.chararray` 会触发 `DeprecationWarning`。本节我们只读源码、理解它的 `dtype`/`itemsize` 设计，不去新建它。

#### 4.3.2 核心流程

`chararray.__new__` 构造一个新数组的过程：

```text
入参：shape, itemsize(字符数, 默认1), unicode(默认False)
  ├─ unicode=True  → dtype = str_        （U）
  ├─ unicode=False → dtype = bytes_      （S）
  ├─ itemsize = int(itemsize)            （强制转 Python int，避免 NumPy 整数的坑）
  └─ ndarray.__new__(cls, shape, (dtype, itemsize), ...)
        ↑ 关键：把 dtype 和“字符数”打包成 (dtype, itemsize) 传给底层
```

注意 `(dtype, itemsize)` 这种写法：它告诉 NumPy “用这个 dtype，但把每个元素的宽度设成 `itemsize` 个该 dtype 的单位”。对 `str_` 来说一个单位是 4 字节，所以 `(str_, 5)` 就是每个元素 20 字节。

#### 4.3.3 源码精读

构造函数本体：

```python
def __new__(cls, shape, itemsize=1, unicode=False, buffer=None,
            offset=0, strides=None, order='C'):
    if unicode:
        dtype = str_
    else:
        dtype = bytes_

    # force itemsize to be a Python int, since using NumPy integer
    # types results in itemsize.itemsize being used as the size of
    # strings in the new array.
    itemsize = int(itemsize)
    ...
    if buffer is None:
        self = ndarray.__new__(cls, shape, (dtype, itemsize),
                               order=order)
```

——[defchararray.py:550-571](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L550-L571)。

几个要点：

- `unicode` 参数决定 dtype 是 `str_` 还是 `bytes_`，默认 `False`（即默认字节串）。
- `itemsize = int(itemsize)` 这一行有个注释解释了“为什么要强制转 Python int”：如果传入的是 NumPy 整数类型（比如 `np.int64(5)`），它的 `.itemsize` 属性是 8，会被误当成字符串长度。这个细节体现了源码对边界情况的谨慎处理。
- 形参 `itemsize` 的含义在文档里写得很清楚：[defchararray.py:513-514](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L513-L514)——“Length of each array element, **in number of characters**”。注意是“字符数”，不是字节数。

构造函数文档里还给了一个直观看 `itemsize` 作用的例子（[defchararray.py:534-547](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L534-L547)）：默认 `chararray((3,3))` 是 `|S1`（每元素 1 字节），改成 `itemsize=5` 后变成 `|S5`。

此外，`__array_finalize__` 会做一次 dtype 合法性校验：

```python
def __array_finalize__(self, obj):
    # The b is a special case because it is used for reconstructing.
    if self.dtype.char not in 'VSUbc':
        raise ValueError("Can only create a chararray from string data.")
```

——[defchararray.py:590-593](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L590-L593)（其中 `'VSUbc'` 分别对应各种字符串/字节 dtype 的 kind 字符：`V`=void、`S`=bytes、`U`=unicode、`b`/`c` 为历史/重建用途）。只有 dtype 属于这些字符类型，才允许被“包装”成 `chararray`。

> 说明：`__array_finalize__`、`__getitem__`、`__array_wrap__` 等“子类化三件套”的完整剖析放在 [u3-l1](u3-l1-chararray-subclass.md)，本节只需知道它们保证了 `chararray` 始终持有字符串 dtype。

#### 4.3.4 代码实践

1. **实践目标**：用源码阅读的方式，确认 `itemsize` 形参是“字符数”而非“字节数”。
2. **操作步骤**（**源码阅读型**，因为 `chararray` 已弃用，不建议真的去 new 它）：
   - 打开 [defchararray.py:550-571](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L550-L571)。
   - 想象调用 `chararray((2,), itemsize=5, unicode=True)`：`dtype` 取 `str_`，最终传给 `ndarray.__new__` 的是 `(str_, 5)`。
   - 推算：底层每个元素占用 `5 × 4 = 20` 字节。
3. **需要观察的现象**：你能不运行代码，仅凭源码推出结果。
4. **预期结果**：`chararray((2,), itemsize=5, unicode=True)` 等价于一个 `dtype='<U5'`、`itemsize=20` 的数组。
5. 如果你确实想运行验证，可写 `import warnings; warnings.simplefilter("ignore"); import numpy as np; c = np.char.chararray((2,), itemsize=5, unicode=True); print(c.dtype, c.itemsize)`，应看到 `<U5` 和 `20`（**待本地验证**；忽略弃用警告仅为调试用途）。

#### 4.3.5 小练习与答案

- **练习 1**：`chararray.__new__` 为什么要把 `itemsize` 用 `int()` 包一层？
  - **答案**：防止传入 NumPy 整数类型时，`.itemsize`（=8）被误当作字符串宽度。见 [defchararray.py:557-560](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L557-L560) 的注释。
- **练习 2**：`chararray((3,), itemsize=1, unicode=False)` 得到的 dtype 字符串是什么？
  - **答案**：`unicode=False` → `bytes_`，`itemsize=1` → 每元素 1 字节，即 `|S1`。

---

### 4.4 array() 工厂与 itemsize 自动推断（chararray 构造）

#### 4.4.1 概念说明

实际使用中很少直接 `chararray(...)`，而是用 `np.char.array()` / `np.char.asarray()` 这两个工厂函数。它们的本领是**输入归一化**：不管你传进来的是 Python `str`/`bytes`、列表、`object` 数组，还是已有的 `str_`/`bytes_` 数组，都能转成一个 `chararray`，并在你没有指定 `itemsize` 时**自动推断**字符宽度。

> ⚠️ 同样地，`array`/`asarray` 在 2.5 已弃用（[defchararray.py:1225-1227](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1225-L1227)）。我们读它的目的，是理解“itemsize 是字符数还是字节数”在工程里是怎么被处理的。

#### 4.4.2 核心流程

`array(obj, itemsize=None, copy=True, unicode=None, order=None)` 对不同输入的分支（精简版）：

```text
obj 是 str/bytes      → 按字符串长度推断 itemsize，用 buffer 构造
obj 是 list/tuple     → 先 asnarray(obj) 转成普通数组，再走下面
obj 是 str_/bytes_ 数组 → 直接 view 成 chararray；itemsize 默认取 obj.itemsize
                        若是 str_，还要 itemsize //= 4（字节→字符）   ★本讲重点
obj 是 object 数组     → 没有 itemsize 时先 tolist()，交给底层自动定宽
其余                  → 用 narray(obj, dtype=(dtype,itemsize)) 构造
```

`asarray` 只是把 `array` 的 `copy` 固定为 `False`（[defchararray.py:1428-1429](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1428-L1429)），尽量不拷贝。

#### 4.4.3 源码精读

最关键的“除以 4”分支（`obj` 已是 `str_`/`bytes_` 数组时）：

```python
if isinstance(obj, ndarray) and issubclass(obj.dtype.type, character):
    # If we just have a vanilla chararray, create a chararray
    # view around it.
    if not isinstance(obj, chararray):
        obj = obj.view(chararray)

    if itemsize is None:
        itemsize = obj.itemsize
        # itemsize is in 8-bit chars, so for Unicode, we need
        # to divide by the size of a single Unicode character,
        # which for NumPy is always 4
        if issubclass(obj.dtype.type, str_):
            itemsize //= 4
```

——[defchararray.py:1313-1325](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1313-L1325)。

这段是本讲“4 倍”解释的**直接来源**。读法：

1. `obj.itemsize` 是底层 ndarray 的字节数（`<U5` → 20）。
2. 但 `chararray` 体系里的 `itemsize` 一律指**字符数**。
3. 所以 Unicode 要 `÷4` 把字节换算回字符；`bytes_` 本来就是 1 字节/字符，不用除。

另一个有趣的分支是 `object` 数组的处理：

```python
if isinstance(obj, ndarray) and issubclass(obj.dtype.type, object):
    if itemsize is None:
        # Since no itemsize was specified, convert the input array to
        # a list so the ndarray constructor will automatically
        # determine the itemsize for us.
        obj = obj.tolist()
        # Fall through to the default case
```

——[defchararray.py:1347-1353](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1347-L1353)。当输入是 `object` 数组且没给 `itemsize` 时，源码把它转回 Python list，再交给底层 `ndarray` 构造函数去“扫描所有元素、按最长字符串定宽”。这正体现了 object 数组与定宽数组的衔接：object 数组本身没有固定宽度，需要显式“定宽”一次。

工厂函数最后把结果 `.view(chararray)`（[defchararray.py:1360-1364](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1360-L1364)），所以输出类型是 `chararray` 而非普通 ndarray。

#### 4.4.4 代码实践

1. **实践目标**：用 `array()` 工厂的源码逻辑，预测不同输入的输出 dtype。
2. **操作步骤**（**源码阅读型 + 可选运行**）：
   - 阅读分支表与上面的源码片段，对下面三种输入分别**预测**输出 dtype 与 `itemsize`：
     - `np.char.array(['hello', 'world', 'numpy', 'array'])`
     - `np.char.array(np.array(['ab', 'cdef']))`（输入已是 `str_` 数组）
     - `np.char.array(np.array(['ab', 'cdef'], dtype=object))`（输入是 object 数组）
   - 然后运行验证（需用 `warnings.simplefilter("ignore")` 压住弃用警告）。
3. **需要观察的现象**：你预测的 dtype/`itemsize` 与实际是否一致。
4. **预期结果**（依据源码）：
   - 第 1 个：`['hello',...]` 最长 5 字符 → `<U5`，`itemsize=20`。这正是文档示例 [defchararray.py:1291-1293](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1291-L1293) 的结果。
   - 第 2 个：输入 `str_` 数组最长 4 字符 → `<U4`，`itemsize=16`（走 `view` + 可能的 `astype`）。
   - 第 3 个：object 数组先 `tolist()` 再定宽 → `<U4`，`itemsize=16`。
5. 上述 dtype 由源码逻辑决定，但实际运行时受版本影响，请以本地结果为准（**待本地验证**）。

#### 4.4.5 小练习与答案

- **练习 1**：为什么 `array()` 在处理 `str_` 输入时要 `itemsize //= 4`，而处理 `bytes_` 输入时不用？
  - **答案**：因为 `itemsize` 形参统一表示“字符数”。`str_` 每字符 4 字节，要把字节数除以 4；`bytes_` 每字符 1 字节，字节数即字符数，无需换算。
- **练习 2**：`asarray` 与 `array` 的唯一代码差异在哪？
  - **答案**：`asarray` 把 `copy` 固定为 `False` 后调用 `array`（[defchararray.py:1428-1429](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1428-L1429)），因此输入已是合适数组时尽量不拷贝。

---

## 5. 综合实践

把本讲的 `str_` / `bytes_` / `itemsize` / `object` 四个概念串起来，完成下面这个小任务（**全程用普通 ndarray，不触发弃用警告**）：

> **任务**：给定一组名字 `['Al', 'Barbara', 'C']`，请：
>
> 1. 用 `dtype='<U8'`（足够放下最长的 "Barbara"+余量，这里取 8）构造一个 Unicode 数组 `names`；打印 `names.dtype`、`names.itemsize`、`names.nbytes`，并用本讲公式解释 `itemsize`。
> 2. 再用 `dtype=object` 构造一个 `names_obj`；比较 `names.itemsize` 与 `names_obj.itemsize`，说明为什么 object 数组的 `itemsize` 通常是 8（一个指针的大小）而不是字符串长度。
> 3. 调用一次 `numpy.strings` 的向量化函数（例如 `np.strings.upper(names)`），观察输出 dtype；并思考：如果把 `names_obj`（object 数组）传给 `np.strings.upper` 会怎样（**待本地验证**）。

参考思路（示例代码）：

```python
import numpy as np

names = np.array(['Al', 'Barbara', 'C'], dtype='<U8')   # 8 字符 × 4 = 32 字节/元素
print(names.dtype, names.itemsize, names.nbytes)         # <U8 32 96

names_obj = np.array(['Al', 'Barbara', 'C'], dtype=object)
print(names_obj.dtype, names_obj.itemsize)               # object 8（64 位系统指针大小）

print(np.strings.upper(names))                           # 向量化大写，输出仍是 <U8
```

要点自检：

- `names.itemsize` 应为 32（= 8 × 4），印证“Unicode 每字符 4 字节”。
- `names_obj.itemsize` 应为 8（指针），印证 object 数组存的是引用而非定宽字符。
- 向量化字符串函数在 `str_` 数组上能高效运行，这正是一开始官方推荐 `str_`/`bytes_` 的原因。

## 6. 本讲小结

- `numpy.char` 操作面向的是**定宽字符串数组**，核心 dtype 是 `str_`（Unicode，`<U`）和 `bytes_`（字节串，`|S`），二者都继承自抽象基类 `character`。
- `itemsize` 是每个元素占用的**字节数**：`str_` 每字符 4 字节（UCS-4），`bytes_` 每字符 1 字节，所以同样字符数下 `str_` 的 `itemsize` 是 `bytes_` 的 **4 倍**。
- 源码里 `array()` 工厂的 `itemsize //= 4`（[defchararray.py:1321-1325](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1321-L1325)）正是把“字节”换算回“字符”，因为 `chararray` 体系的 `itemsize` 形参一律指字符数。
- `chararray.__new__` 用 `(dtype, itemsize)` 把“类型 + 字符宽度”打包交给底层 ndarray，并对 `itemsize` 强制 `int()` 以规避 NumPy 整数的陷阱（[defchararray.py:557-571](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L557-L571)）。
- `object` 数组存的是 Python 对象指针（`itemsize≈8`），可变长但无法走 C 层向量化；定宽 `str_`/`bytes_` 数组连续紧凑、可向量化，代价是定宽补齐/截断。
- `chararray` / `array` / `asarray` 在 NumPy 2.5 已弃用，新代码应直接用 `str_`/`bytes_` 普通数组搭配 `numpy.char` 或 `numpy.strings` 自由函数。

## 7. 下一步学习建议

- 下一讲 [u1-l3 公共 API 全貌与第一次向量字符串操作](u1-l3-public-api-and-first-ops.md) 会基于本讲的 dtype 基础，带你跑通第一次元素级（element-wise）向量化字符串操作（`upper`、`add`、`center`、`multiply`），并观察这些函数如何根据输入 dtype 决定输出 dtype。
- 想深入 `itemsize` 推断的读者，可继续精读 [defchararray.py:1296-1364](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1296-L1364) `array()` 的完整分支。
- 对 `chararray` 子类化机制（`__array_finalize__` / `__getitem__` 的 `rstrip` / `__array_wrap__`）感兴趣的读者，可预习 [u3-l1 chararray 的 ndarray 子类化机制](u3-l1-chararray-subclass.md)。
