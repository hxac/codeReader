# 字段命名、标题、默认名与重复检测

## 1. 本讲目标

上一讲（u2-l1）我们顺着 `_parseFormats → _setfieldnames → _createdto` 把 `format_parser` 的三步流水线走了一遍，知道了「格式串怎么变成 `dtype`」。但当时对第二步 `_setfieldnames` 只是匆匆带过——它到底怎么处理「用户乱给的名字」？标题（`titles`）和名字（`names`）是不是一回事？重复检测查的到底是谁？字节序那张映射表里每个字符都什么含义？本讲就来把这些「上户口」的细节彻底讲透。

学完本讲，你应该能够：

- 说清 `names` 的**三种合法形态**（list / tuple / 逗号字符串）以及一种**非法形态**（其它类型抛 `NameError`），并解释为什么用 `type()` 而不是 `isinstance()` 判定；
- 推断「名字给多了」「名字给少了」「名字完全不给」三种情况下，最终字段名分别长什么样（特别是**少给时编号从你停下的地方继续**这条隐藏规则）；
- 讲明白 `find_duplicate` 的工作原理、它作为**公开 API** 的用法，以及一个关键的不对称：**它只查 `names`，不查 `titles`**；
- 读懂 `_byteorderconv` 这张 14 项映射表，并区分 `newbyteorder` 里「绝对」（`>`/`<`/`=`）与「相对交换」（`s`）两种字节序语义。

本讲仍只产出 `dtype`，不装数据（装数据是 u2-l3 `fromarrays` 的事）。真实实现全部在 [numpy/_core/records.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py)，`numpy/rec/__init__.py` 仅是再导出垫片（u1-l1 已讲）。

## 2. 前置知识

承接 u2-l1，你需要先记住三件事：

1. **`format_parser` 把翻译拆成三步**，`_setfieldnames` 是第二步：它消费第一步 `_parseFormats` 算出的 `_nfields`（字段数），产出 `_names` 和 `_titles` 两个等长列表，再交给第三步 `_createdto` 组装 `dtype`。
2. **`dtype.fields` 是一个字典**，键是字段名（有标题时标题也是键），值是 `(子dtype, 字节偏移[, 标题])`。正因为是字典，**键不能重复**——否则后写的会覆盖先写的，造成字段静默丢失。
3. **`sb` 是 `numpy._core.numeric` 的别名**，`sb.dtype` 就是你常用的 `np.dtype`；`sb` = "stride/shape buddy"（历史别名），本讲里 `sb.dtype` 和 `np.dtype` 完全等价。

再统一两个口径（本讲会反复用到）：

| 概念 | 含义 | 例子 |
|------|------|------|
| `names` | 每列的「正式名」，是 `dtype.names` 的来源 | `'a,b'` 或 `['a','b']` |
| `titles` | 每列的「别名/标题」，可与 `names` 并存，访问字段时两种名字都能用 | `['T1','T2']` |
| `f0/f1/...` | 默认名，用户没给或给少了时由系统补上 | `('f0','f1','f2')` |

## 3. 本讲源码地图

本讲只盯住一个文件里的四段代码：

- [numpy/_core/records.py:146-180](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L146-L180) — `format_parser._setfieldnames` 全文（本讲主角）。
- [numpy/_core/records.py:46-53](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L46-L53) — 模块级工具函数 `find_duplicate`。
- [numpy/_core/records.py:23-36](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L23-L36) — 字节序映射表 `_byteorderconv`。
- [numpy/_core/records.py:182-193](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L182-L193) — `_createdto`，它消费本讲的成果并应用字节序。

辅助确认 `find_duplicate` 是公开 API：

- [numpy/_core/records.py:15-18](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L15-L18) — `__all__` 列表，`find_duplicate` 在其中。

## 4. 核心概念与源码讲解

### 4.1 _setfieldnames 深入：names 的类型判定与截断/补齐边界

#### 4.1.1 概念说明

类型（`formats`）解析完、拿到字段数 `_nfields` 之后，`_setfieldnames` 要给每列「上户口」。这一步面对的用户输入五花八门：

- 有人传逗号字符串 `'a, b, c'`；
- 有人传列表 `['a','b','c']`；
- 有人传元组 `('a','b','c')`；
- 有人干脆不传，让系统自己取名；
- 还有人传错了类型（比如一个整数）。

`_setfieldnames` 的职责就是把这些杂乱的输入**归一化成两个长度恰好等于 `_nfields` 的列表**——`_names`（正式名）和 `_titles`（标题，没有就填 `None`）。归一化过程要处理四件事：**类型判定、空白清理、长度对齐（截断或补齐）、查重**。

#### 4.1.2 核心流程

```
_setfieldnames(names, titles)
│
├─ ① 判定 names 类型
│   ├─ type(names) 是 list 或 tuple → 直接用          （元组也算合法！）
│   ├─ isinstance(names, str)        → names.split(',')（逗号串切分）
│   └─ 其它（int/float/自定义类…）   → raise NameError
│
├─ ② 清理 + 截断
│   _names = [n.strip() for n in names[:_nfields]]     （去首尾空白；多了的丢弃）
│   （names 为空时：_names = []）
│
├─ ③ 补齐默认名
│   _names += ['f{i}' for i in range(len(_names), _nfields)]
│   注意：编号从 len(_names) 开始，不是从 0 开始！
│
├─ ④ 查重（交给 find_duplicate，见 4.2）
│   有重复 → raise ValueError
│
└─ ⑤ titles 同样「截断 + 补 None」，但【不查重】
```

关于第 ③ 步那条容易踩坑的规则：补齐用的不是 `f0, f1, ...`，而是 `f{len(_names)}, f{len(_names)+1}, ...`。也就是说，**如果你给了 2 个名字但有 4 个字段，缺的两个会被命名为 `f2`、`f3`，而不是 `f0`、`f1`**。用式子表示补齐数量：

\[
\text{补齐个数} = \max(0,\; \text{\_nfields} - \text{len}(\text{\_names}))
\]

而补齐的起始编号就是补齐前的 `len(_names)`。

#### 4.1.3 源码精读

先看类型判定这一段，它藏着两个细节：

- [records.py:150-160](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L150-L160) — names 的类型分支与截断：

```python
if names:
    if type(names) in [list, tuple]:
        pass
    elif isinstance(names, str):
        names = names.split(',')
    else:
        raise NameError(f"illegal input names {repr(names)}")
    self._names = [n.strip() for n in names[:self._nfields]]
else:
    self._names = []
```

两个细节：

1. **用的是 `type(names) in [list, tuple]`，不是 `isinstance`**。这意味着一个 `list` 的子类（比如 `class MyList(list)`）会被判定为「不是 list」——因为它走完前两个分支都不命中，最后抛 `NameError`。这是「精确类型匹配」与「鸭子类型」的区别，阅读源码时要留意。

   > 补充：`type(names) in [list, tuple]` 还顺带说明**元组 `tuple` 是被接受的**——你传 `('a','b')` 和传 `['a','b']` 效果一样。

2. **非法类型抛的是 `NameError`，不是 `TypeError`**。这多少有点反直觉（通常类型错误用 `TypeError`），但源码确实写的是 `NameError`。所以 `format_parser(['f8','i4'], 123)` 会抛 `NameError: illegal input names 123`。

接着看补齐与查重：

- [records.py:162-171](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L162-L171) — 默认名补齐 + 查重：

```python
# 默认名补齐：编号从 len(_names) 开始
self._names += [f'f{i}' for i in range(len(self._names), self._nfields)]
# 查重
_dup = find_duplicate(self._names)
if _dup:
    raise ValueError(f"Duplicate field names: {_dup}")
```

注意 `range(len(self._names), self._nfields)` 的起点是**补齐前的长度**。结合前面的截断 `names[:self._nfields]`，可以推出四种典型输入的结果：

| 输入 `names` | `_nfields` | 截断后 | 补齐后 `_names` |
|---|---|---|---|
| `['a','b','c','d']`（给多了） | 2 | `['a','b']`（`c,d` 丢弃） | `['a','b']` |
| `['a','b']`（刚好） | 2 | `['a','b']` | `['a','b']` |
| `['a']`（给少了） | 3 | `['a']` | `['a','f1','f2']` |
| `[]` 或不传 | 3 | `[]` | `['f0','f1','f2']` |

最后看标题处理（**注意：标题分支里没有 `find_duplicate`**）：

- [records.py:173-180](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L173-L180) — titles 的截断与补 `None`：

```python
if titles:
    self._titles = [n.strip() for n in titles[:self._nfields]]
else:
    self._titles = []
    titles = []
if self._nfields > len(titles):
    self._titles += [None] * (self._nfields - len(titles))
```

标题和名字走的是**几乎相同**的「截断 + 补齐」逻辑，唯一区别是：缺的标题补 `None`（表示这一列没有标题），而且**标题从不经过查重**。这个不对称是 4.2 节的重点。

#### 4.1.4 代码实践

> **实践目标**：亲手验证「类型判定」「给多截断」「给少补齐」三条边界，并触发 `NameError`。
>
> **操作步骤**：
>
> ```python
> import numpy as np
>
> # 1) 逗号串 vs 列表 vs 元组 —— 三者等价
> a = np.rec.format_parser(['f8','i4'], 'x, y').dtype.names
> b = np.rec.format_parser(['f8','i4'], ['x','y']).dtype.names
> c = np.rec.format_parser(['f8','i4'], ('x','y')).dtype.names   # 元组也行
> print('逗号串:', a, ' 列表:', b, ' 元组:', c)
>
> # 2) 给多了 → 静默截断（c,d 被丢）
> more = np.rec.format_parser(['f8','i4'], ['a','b','c','d']).dtype.names
> print('给多了:', more)
>
> # 3) 给少了 → 从 f1 开始补（不是 f0！）
> less = np.rec.format_parser(['f8','i4','f8'], ['a']).dtype.names
> print('给少了:', less)
>
> # 4) 非法类型 → NameError（不是 TypeError）
> try:
>     np.rec.format_parser(['f8','i4'], 123)
> except NameError as e:
>     print('非法类型:', type(e).__name__, '-', e)
> ```
>
> **需要观察的现象**：三种合法形态字段名完全一致；给多了只剩前两个；给少了缺的两列叫 `f1`、`f2`；非法类型抛 `NameError`。
>
> **预期结果**：
>
> ```
> 逗号串: ('x', 'y')  列表: ('x', 'y')  元组: ('x', 'y')
> 给多了: ('a', 'b')
> 给少了: ('a', 'f1', 'f2')
> 非法类型: NameError - illegal input names 123
> ```

#### 4.1.5 小练习与答案

**练习 1**：`format_parser(['f8','i4','f8','i4'], ['x','y'])` 的字段名是什么？为什么不是 `('x','y','f0','f1')`？

> **答案**：是 `('x','y','f2','f3')`。因为补齐的起始编号是补齐前的 `len(_names)=2`，所以缺的两列叫 `f2`、`f3`。源码里是 `range(len(self._names), self._nfields)` 即 `range(2, 4)`。

**练习 2**：如果我写了一个 `class MyList(list): pass`，然后用 `format_parser(['f8','i4'], MyList(['a','b']))`，会发生什么？

> **答案**：会抛 `NameError`。因为源码用 `type(names) in [list, tuple]` 做**精确类型**判定，`MyList` 的实例类型是 `MyList` 而不是 `list`，不命中；接着 `isinstance(names, str)` 也不命中；于是走到 `else` 抛 `NameError`。这是「精确匹配」与 `isinstance` 鸭子类型的差别。

---

### 4.2 find_duplicate：不对称的查重（只查 names）

#### 4.2.1 概念说明

`find_duplicate` 是一个**模块级的小工具函数**，作用是「找出列表里出现超过一次的元素」。它在 `_setfieldnames` 里被用来拦截重名字段。

为什么必须拦截重名？因为最终 `dtype.fields` 是一个**字典**，键就是字段名。如果两个字段同名，字典只会保留后写入的那一个——前一个字段会被**静默丢弃**，数据莫名其妙少了一列。这属于「不报错但结果错」的灾难性 bug，所以 `format_parser` 选择在源头就抛 `ValueError` 拒绝。

值得强调的是，`find_duplicate` 本身是个**通用工具**（输入任意列表、输出重复元素），它并不绑定 `format_parser`。事实上它被放进了 `__all__`，你可以直接 `np.rec.find_duplicate(...)` 调用它。

#### 4.2.2 核心流程

```
find_duplicate(lst)
    counts = Counter(lst)                 # 每个元素 → 出现次数
    return [x for x, n in counts.items() if n > 1]
```

用集合记号描述，它返回的是：

\[
\{\, x \mid \text{count}(x) > 1 \,\}
\]

但**返回的是列表、且保持首次出现顺序**（Python 3.7+ 字典保序，`Counter` 是 `dict` 子类）。对 `format_parser` 来说顺序无所谓——只要有任何一个重复元素，就立刻抛 `ValueError`。

一个**关键的不对称**：`_setfieldnames` 只对 `_names` 调用了 `find_duplicate`，**从没对 `_titles` 调用**。也就是说：

- 重名字段 → 被 `find_duplicate` 拦下，抛 `ValueError`；
- 重名标题 → **不会被 `find_duplicate` 拦下**，会一路溜达到 `_createdto`，交给底层 `sb.dtype` 处理（底层是否报错、如何报错，不在 `find_duplicate` 的职责范围内，属于 `np.dtype` 的行为，**待本地验证**）。

#### 4.2.3 源码精读

- [records.py:46-53](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L46-L53) — `find_duplicate` 全文：

```python
@set_module('numpy.rec')
def find_duplicate(list):
    """Find duplication in a list, return a list of duplicated elements"""
    return [
        item
        for item, counts in Counter(list).items()
        if counts > 1
    ]
```

阅读时注意三点：

1. **`Counter` 来自标准库 `collections`**（文件开头 `from collections import Counter`，[records.py:6](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L6)）。`Counter(list)` 一次遍历统计完所有元素，时间复杂度 \(O(n)\)。
2. **参数名 `list` 遮蔽了内置 `list`**。这在风格上不太好（会让人误以为只能传 `list` 类型），但因为函数体内没有用到内置 `list`，所以能正常工作——任意可迭代对象都能传。
3. **`@set_module('numpy.rec')`** 让它对外显示为 `numpy.rec.find_duplicate`，物理实现仍在 `_core/records.py`。

调用点只有一处（在 `_setfieldnames` 内），且**只传 `_names`**：

- [records.py:168-171](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L168-L171) — 命中重复即抛 `ValueError`：

```python
_dup = find_duplicate(self._names)
if _dup:
    raise ValueError(f"Duplicate field names: {_dup}")
```

`_dup` 非空时，报错信息会直接把重复的名字列表塞进去，比如 `['x','x']` 会得到 `Duplicate field names: ['x']`。

#### 4.2.4 代码实践

> **实践目标**：完成规格要求的两个验证——重名字段抛 `ValueError`、空 names 退化为 `f0,f1`；再单独体验 `find_duplicate` 这个公开工具。
>
> **操作步骤**：
>
> ```python
> import numpy as np
>
> # (规格实践 1) 故意用重复字段名 → ValueError
> try:
>     np.rec.format_parser(['f8','i4'], ['x','x'])
> except ValueError as e:
>     print('重复名:', e)
>
> # (规格实践 2) 空 names → 字段名变为 f0、f1
> empty = np.rec.format_parser(['f8','i4'], [], [])
> print('空 names:', empty.dtype.names)
>
> # 把 find_duplicate 当独立工具用（它是公开 API）
> print('工具调用:', np.rec.find_duplicate(['a','b','a','c','b','b']))
> ```
>
> **需要观察的现象**：重复名触发 `ValueError` 且信息里列出 `'x'`；空 names 时字段名变成 `('f0','f1')`；`find_duplicate` 返回 `['a','b']`（按首次出现顺序，`'c'` 只出现一次被过滤）。
>
> **预期结果**：
>
> ```
> 重复名: Duplicate field names: ['x']
> 空 names: ('f0', 'f1')
> 工具调用: ['a', 'b']
> ```

#### 4.2.5 小练习与答案

**练习 1**：`find_duplicate([1,1,2,3,3,3])` 返回什么？顺序由什么决定？

> **答案**：返回 `[1, 3]`。顺序由 `Counter`（`dict` 子类）的**插入顺序**决定，即元素在原列表中**首次出现**的顺序；与出现次数多少无关（`3` 出现 3 次、`1` 出现 2 次，但 `1` 先出现所以排在前面）。

**练习 2**：如果我给两个字段都设了相同的标题 `format_parser(['f8','i4'],['x','y'],['T','T'])`，`find_duplicate` 会拦下吗？

> **答案**：不会。`find_duplicate` 在 `_setfieldnames` 里**只被作用于 `_names`**，从不检查 `_titles`。所以重复标题能通过 `format_parser` 的查重关；至于它后续在 `_createdto` 里交给 `sb.dtype` 时会不会报错、报什么错，那是 `np.dtype` 的行为，本讲不展开（**待本地验证**）。这正是查重逻辑「不对称」的体现。

---

### 4.3 _byteorderconv：字节序映射表与 newbyteorder 的「绝对 vs 相对」

#### 4.3.1 概念说明

`_byteorderconv` 是一张**模块级的查表字典**，把用户可能写的各种「字节序写法」统一翻译成 `numpy.dtype.newbyteorder` 能接受的那几个规范字符。

「字节序」（byte order / endianness）指的是多字节数据（比如 8 字节的 `f8`）在内存里是「低字节在前」（小端 `<`）还是「高字节在前」（大端 `>`）。跨平台读写二进制数据时常常要指定它：在 x86 机器上默认是小端，但读取一段来自大端机器的二进制时就要显式声明 `>`。

这张表之所以存在，是因为历史上有**好几套字节序记法**：有人写 `'big'`/`'little'`，有人写 `'b'`/`'l'`，有人写 `'>'`/`'<'`，还有 `'native'`/`'swap'` 等。`_byteorderconv` 把它们都归一化成 numpy 那套。

#### 4.3.2 核心流程

`_byteorderconv` 本身不执行，真正用它的是 `_createdto` 的尾巴：

```
_createdto(byteorder)
│
├─ 先用 {names, formats, offsets, titles} 字典造好 dtype（各字段保留 formats 自带字节序）
│
├─ 若 byteorder 不是 None：
│   ├─ ch = byteorder[0]                 # 只取首字符！'big'→'b'，'little'→'l'
│   ├─ code = _byteorderconv[ch]         # 查表归一化；ch 不在表里 → KeyError
│   └─ dtype = dtype.newbyteorder(code)  # 统一改字节序（作用于所有字段，含嵌套）
│
└─ self.dtype = dtype
```

两个要点：

1. **只取首字符 `byteorder[0]`**。所以 `'big'`、`'b'`、`'>'` 三种写法等价（都映射到大端）。副作用是：传空串 `byteorder=''` 会在 `byteorder[0]` 处抛 `IndexError`；传表里没有的字符（如 `'?'`）会在查表处抛 `KeyError`。
2. **`newbyteorder` 的语义分两类**，这是最容易混淆的地方：
   - **绝对型**：`'>'`、`'<'`、`'='`——把字段**强制设成**指定字节序（`'='` 是「本机序」）；
   - **相对型**：`'s'`——把每个字段**当前的字节序翻转**（小端变大端、大端变小端）。

   区别在于：对一个「本来就混合了大小端」的 dtype，`'>'` 会把所有字段都变成大端；而 `'s'` 会把原来是小端的变大端、原来是大端的变小端。

#### 4.3.3 源码精读

- [records.py:23-36](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L23-L36) — `_byteorderconv` 全表：

```python
_byteorderconv = {'b': '>',
                  'l': '<',
                  'n': '=',
                  'B': '>',
                  'L': '<',
                  'N': '=',
                  'S': 's',
                  's': 's',
                  '>': '>',
                  '<': '<',
                  '=': '=',
                  '|': '|',
                  'I': '|',
                  'i': '|'}
```

整理成语义表更清楚：

| 输入字符（取首字符后） | 含义 | 映射到 | `newbyteorder` 语义 |
|---|---|---|---|
| `'b'`, `'B'` | big（大端） | `'>'` | 绝对：强制大端 |
| `'l'`, `'L'` | little（小端） | `'<'` | 绝对：强制小端 |
| `'n'`, `'N'`, `'='` | native（本机序） | `'='` | 绝对：本机字节序 |
| `'s'`, `'S'` | swap（交换） | `'s'` | **相对**：翻转当前字节序 |
| `'>'`, `'<'` | 已是规范形式 | 原样 | 绝对 |
| `'|'`, `'I'`, `'i'` | 不适用（单字节/字符串等） | `'|'` | 无变化 |

注意 `'I'`/`'i'` 映射到 `'|'`：`'|'` 在 numpy 里表示「字节序不适用」（比如 `u1`、`S5` 这种单字节或字节串类型），`newbyteorder('|')` 对它们是空操作。

再看使用点：

- [records.py:189-191](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L189-L191) — `_createdto` 应用字节序：

```python
if byteorder is not None:
    byteorder = _byteorderconv[byteorder[0]]
    dtype = dtype.newbyteorder(byteorder)
```

`dtype.newbyteorder(...)` 改的是 dtype 对字节序的**解释方式**（即字段子 dtype 上 `<`/`>` 标记），**不会搬运内存里的字节**。也就是说，它告诉 numpy「请把这段内存当作大端来读」，而不是「把内存里的字节翻转」。这一点在用 `fromstring`/`fromfile` 读二进制时尤其重要（u4-l2、u4-l3 会展开）。

#### 4.3.4 代码实践

> **实践目标**：对比「绝对 `'>'`」与「相对 `'s'`」，直观看到两者的差别；并确认 `'big'` 这种单词写法能用。
>
> **操作步骤**：
>
> ```python
> import numpy as np
>
> # 1) 不指定 byteorder：保留 formats 自带字节序（本机小端时 f8 显示为 <f8）
> d0 = np.rec.format_parser(['f8','i4'], ['a','b']).dtype
> print('默认   :', d0)
>
> # 2) byteorder='>'：强制大端（绝对）
> d1 = np.rec.format_parser(['f8','i4'], ['a','b'], byteorder='>').dtype
> print('强 >   :', d1)
>
> # 3) byteorder='big'：取首字符 'b' → '>'，与上面等价
> d2 = np.rec.format_parser(['f8','i4'], ['a','b'], byteorder='big').dtype
> print('big    :', d2)
>
> # 4) byteorder='s'：相对交换。本机小端时 <f8 翻转成 >f8
> d3 = np.rec.format_parser(['<f8','<i4'], ['a','b'], byteorder='s').dtype
> print('swap   :', d3)
> ```
>
> **需要观察的现象**：`d1`、`d2` 完全相同（都是 `>f8, >i4`），证明 `'>'` 与 `'big'` 等价；`d3` 也得到 `>f8, >i4`，因为输入本来就是小端 `<`、`'s'` 把它翻转成大端——在「全小端」的输入下，`'s'` 和 `'>'` 结果恰好相同。
>
> **预期结果**（本机为小端字节序时）：
>
> ```
> 默认   : dtype([('a', '<f8'), ('b', '<i4')])
> 强 >   : dtype([('a', '>f8'), ('b', '>i4')])
> big    : dtype([('a', '>f8'), ('b', '>i4')])
> swap   : dtype([('a', '>f8'), ('b', '>i4')])
> ```
>
> 若想看出 `'s'` 与 `'>'` 的真正区别，可构造**混合字节序**的输入 `['<f8','>i4']` 分别用 `'>'` 和 `'s'`：前者得到全 `>`，后者得到 `>f8, <i4`（各自翻转）。混合场景的精确 repr **待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`byteorder='little'`、`byteorder='<'`、`byteorder='l'` 三者等价吗？为什么？

> **答案**：等价。`_createdto` 取首字符：`'little'[0]='l'`，再查表 `_byteorderconv['l']='<'`；而 `'<'` 查表仍是 `'<'`；`'l'` 查表也是 `'<'`。三者最终都把 `newbyteorder` 的参数定为 `'<'`（强制小端）。

**练习 2**：如果传 `byteorder='?'`，会发生什么？传 `byteorder=''`（空串）呢？

> **答案**：`'?'` 时，`byteorder[0]='?'`，`_byteorderconv['?']` 查不到键，抛 `KeyError: '?'`。空串 `''` 时，`byteorder[0]` 取不到任何字符，抛 `IndexError: string index out of range`。两者都不是 `ValueError`，因为它们发生在查表/取字符阶段，不在 `_setfieldnames` 的校验里。

---

## 5. 综合实践

把本讲三个模块（`_setfieldnames` 的命名/补齐、`find_duplicate` 的查重、`_byteorderconv` 的字节序）串成一个「故意踩边界」的小任务：构造一个**少给名字、带标题、强制字节序**的 dtype，逐项核对内部状态，最后用一次重复名触发报错。

> **实践目标**：用一个 `format_parser` 调用同时验证「默认名从 `f2` 开始补」「标题与名字并存于 `dtype.fields`」「`byteorder` 统一改写所有字段」三件事，再用 `find_duplicate` 复盘重名检测。
>
> **操作步骤**：
>
> ```python
> import numpy as np
>
> # 4 个字段，只给 2 个名字 → 第 3、4 列应补成 f2、f3（不是 f0、f1）
> # 给 2 个标题（后两列无标题）；强制大端
> fp = np.rec.format_parser(
>     ['f8', 'i4', 'f8', 'i4'],
>     names=['lat', 'lon'],          # 少给 → 预期 f2, f3
>     titles=['Latitude', 'Longitude'],  # 只给前两列
>     byteorder='>',                  # big → '>'
> )
> d = fp.dtype
>
> print('names   :', d.names)
> print('dtype   :', d)
> print('标题键? :', 'Latitude' in d.fields, '名字键?', 'lat' in d.fields)
>
> # 复盘：重复名会被 find_duplicate 拦下
> try:
>     np.rec.format_parser(['f8','i4'], ['x','x'])
> except ValueError as e:
>     print('查重:', e)
> ```
>
> **需要观察的现象**：
> 1. `d.names` 是 `('lat','lon','f2','f3')`——后两列从 `f2` 开始补（4.1 的隐藏规则）；
> 2. `d.dtype` 里四个字段全是 `>` 大端（4.3 的 `byteorder='>'` 生效）；
> 3. `'Latitude'` 和 `'lat'` **都是** `d.fields` 的键（标题与名字并存）；
> 4. 末尾抛 `ValueError: Duplicate field names: ['x']`（4.2 的 `find_duplicate`）。
>
> **预期结果**：
>
> ```
> names   : ('lat', 'lon', 'f2', 'f3')
> dtype   : dtype([(('Latitude', 'lat'), '>f8'), (('Longitude', 'lon'), '>i4'), ('f2', '>f8'), ('f3', '>i4')])
> 标题键? : True 名字键? True
> 查重: Duplicate field names: ['x']
> ```
>
> 注意 `f2`、`f3` 两列在 `dtype` 里显示为 `('f2', '>f8')` 而非 `(('...', 'f2'), ...)`——因为它们没有标题。把 `byteorder='>'` 改成 `'s'` 再跑一次，在本机小端环境下结果应相同；改成 `'<'` 则会强制小端。

## 6. 本讲小结

- `_setfieldnames` 用 `type(names) in [list, tuple]` 做**精确类型**判定：list、tuple、逗号字符串合法；其它类型（含 `list` 子类）抛 `NameError`（注意不是 `TypeError`）。
- 长度对齐全靠 `names[:_nfields]`（多了静默丢弃）和 `range(len(_names), _nfields)`（少了补 `f{i}`，**编号从你停下的地方继续**，不是从 0）。
- `find_duplicate` 用 `collections.Counter` 找重复元素，返回首次出现顺序的列表；它是**公开 API**（`np.rec.find_duplicate`），但 `format_parser` **只对 `names` 查重、不对 `titles` 查重**——这是查重的不对称。
- 重名字段会被拦下抛 `ValueError("Duplicate field names: [...]")`，因为 `dtype.fields` 是字典、重名会导致字段静默丢失。
- `_byteorderconv` 是一张 14 项查表，把 `'b'/'l'/'n'/'s'/'>'/'<'/'='/'|'` 等各种写法归一化；`_createdto` 只取 `byteorder[0]` 再查表，所以 `'big'`、`'little'` 这种单词也能用，但未知字符抛 `KeyError`、空串抛 `IndexError`。
- `newbyteorder` 区分**绝对**（`>`/`<`/`=`）与**相对交换**（`s`）；它改的是字节序**解释**，不搬运内存字节。

## 7. 下一步学习建议

本讲把「字段名/标题/字节序」的边界讲透了，但产出的还只是 `dtype`。建议接下来：

- **u2-l3（fromarrays）**：看本讲确定的 `names` 如何被用来把「列方向」的数组列表装进 record array，并观察字段名最终挂在 `recarray` 上能被属性访问。
- **u3-l2（属性访问魔法）**：本讲提到「标题和名字都是 `dtype.fields` 的键」，下一单元会解释 `recarray.__getattribute__` 如何据此把 `arr.lat`、`arr.Latitude` 都映射到同一列。
- **u4-l2 / u4-l3（fromstring / fromfile）**：本讲的 `byteorder` 在读取**二进制**数据时才真正发力——读跨平台二进制文件时，字节序解释直接决定数值对不对。到时可以回头对照 `_byteorderconv` 与 `newbyteorder` 的「只改解释、不搬字节」特性。
- 想直接看字节序应用现场，可跳读 [records.py:189-191](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L189-L191)（`_createdto` 的 `newbyteorder` 调用）。
