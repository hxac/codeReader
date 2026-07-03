# 讲义标题：MatFileReader 基类与字节序处理

## 1. 本讲目标

本讲是 MATLAB `.mat` 子系统的**第二讲**，承接 u3-l1（你已经知道「打开文件 → 判版本 → 选 reader」的入口链路）。学完本讲你应该能够：

- 解释 `_miobase.py` 里抽象基类 `MatFileReader`/`MatVarReader` 为所有版本 reader 定义了哪些**通用接口与可配置开关**，以及这些开关之间的**覆盖优先级**。
- 读懂 `_byteordercodes.py` 如何把 `'native'`/`'BIG'`/`'<'` 等多种字符串「别名」统一归一化成 numpy 的 `<`（小端）/ `>`（大端）字节码。
- 理解模板方法（template method）模式：基类给一个返回 native 字节序的默认 `guess_byte_order`，而 `MatFile4Reader`/`MatFile5Reader` 各自覆写它去**从文件头真实探测**字节序。
- 掌握 `docfiller`（即 `scipy._lib.doccer.filldoc`）如何用一个共享的 `doc_dict`，把重复出现的参数说明**注入**多个函数与类的 docstring。

本讲**不**展开 `loadmat`/`savemat` 的主流程（那是 u3-l3），也**不**深入 v4/v5 的具体二进制读写（u3-l4~u3-l6）。本讲只回答一个问题：**所有 reader 都继承的那个基类，到底提供了什么、约定了什么**。

## 2. 前置知识

阅读本讲前，建议你已经具备以下认知（u3-l1 已建立）：

- **`.mat` 是一族格式**：v4（无全局头）、v5/v7（128 字节全局头）、v7.3（实为 HDF5，SciPy 不实现）。`mat_reader_factory` 按主版本号把字节流分发给 `MatFile4Reader`/`MatFile5Reader`。
- **文件对象（file-like object）**：任何实现了 `read`/`seek`/`tell` 的对象都算，`MatFileReader` 持有的 `mat_stream` 就是这样一个字节流。

下面补充四个本讲要用到的新概念：

- **字节序（endianness / byte order）**：多字节数值（如 4 字节的 `int32`）在内存/文件里按什么顺序排列。设一个 32 位整数 \(V\)，其第 \(i\) 个字节（从 0 开始）的取值：

  \[
  \text{byte}_i^{\,\text{little}}(V) = (V \gg 8i)\ \&\ \text{0xFF}, \qquad \text{byte}_i^{\,\text{big}}(V) = (V \gg 8(3-i))\ \&\ \text{0xFF}
  \]

  即小端序把**最低有效字节放最前**（`0x01020304` 存成 `04 03 02 01`），大端序相反（存成 `01 02 03 04`）。读取时若解释顺序搞错，数值会面目全非。numpy 用 `<` 表示小端、`>` 表示大端、`=` 表示「本机默认」。
- **抽象基类与模板方法**：基类只定义「应该有哪些方法」（接口），具体实现交给子类。其中「模板方法」特指：基类提供一个**可被覆写的钩子**（如 `guess_byte_order`），基类的默认实现是「偷懒」版本，子类覆写成「认真」版本。这样上层调用 `self.guess_byte_order()` 时无需关心具体是哪个子类。
- **`x and y or z` 三元表达式**：Python 早期没有 `y if x else z` 语法时常用的写法，等价于「`x` 真 → 取 `y`，`x` 假 → 取 `z`」。本讲的 `native_code = sys_is_le and '<' or '>'` 就是这种古早写法。
- **docstring 占位与装饰器**：Python 的 `%(key)s` 是旧式字符串格式化占位符（类似 `%` 格式化的字典形式）。`@docfiller` 是一个**装饰器**：它拿到被装饰函数的 `__doc__`，把里面的 `%(load_args)s` 这类占位符替换成共享字典里的真实文本，再写回 `__doc__`。

## 3. 本讲源码地图

本讲涉及两个核心源码文件，外加两处「使用方」佐证：

| 文件 | 作用 |
|------|------|
| [scipy/io/matlab/_miobase.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_miobase.py) | 四个异常/警告类、`doc_dict` 与 `docfiller`、抽象基类 `MatVarReader`/`MatFileReader`，以及字节序相关的辅助函数 `convert_dtypes`/`read_dtype`。 |
| [scipy/io/matlab/_byteordercodes.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_byteordercodes.py) | 字节序字符串归一化工具：`native_code`/`swapped_code`、`aliases` 别名表、`to_numpy_code()`。 |
| [scipy/io/matlab/_mio4.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio4.py) | `MatFile4Reader`：覆写 `guess_byte_order`，从 v4 的 `MOPT` 探测字节序。 |
| [scipy/io/matlab/_mio5.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio5.py) | `MatFile5Reader`：覆写 `guess_byte_order`，从 v5 头的 `'IM'/'MI'` 探针探测字节序。 |

本讲的层次关系（基类在上，子类与工具在下）：

```
                MatFileReader  (基类：__init__ 开关 + guess_byte_order 默认 + ...)
                       ▲
            ┌──────────┴──────────┐
     MatFile4Reader        MatFile5Reader      ← 各自覆写 guess_byte_order()
            │                      │
            └──► VarReader4   VarReader5(.pyx)  ← 实现 MatVarReader 接口

   MatFileReader.__init__  ──►  boc.to_numpy_code()   (byteordercodes 工具)
   @docfiller 装饰的函数   ──►  doccer.filldoc(doc_dict) (doccer 模块)
   所有 reader 的错误      ──►  MatReadError / MatWriteError / ...Warning
```

> 注意：本讲刻意**避开** `_miobase.py` 里的 `matfile_version`/`_get_matfile_version`（u3-l1 已精读），只讲这两个函数**周边**的基础设施——它们恰好都依赖本讲要讲的异常类与字节序工具。

## 4. 核心概念与源码讲解

### 4.1 字节序工具 `_byteordercodes.py`

#### 4.1.1 概念说明

`.mat` 文件可能是小端（多数 x86 机器）也可能是大端（旧 SPARC/PowerPC 平台）生成的。读取时必须知道文件是哪种字节序，才能把字节流正确解释成数值。但用户（或上层代码）描述字节序的「说法」五花八门：`'little'`、`'<'`、`'le'`、`'native'`、`'BIG'`、`'>'`……

`_byteordercodes.py` 就是一个**归一化层**：把这些五花八门的字符串全部翻译成 numpy 唯一认的两个字节码 `<`（小端）或 `>`（大端）。它不读文件，只做字符串 → 字节码的纯映射。

模块导出四样东西：`sys_is_le`（本机是不是小端）、`native_code`（本机原生字节码）、`swapped_code`（本机的相反字节码）、`to_numpy_code(code)`（把任意合法字符串转成 `<`/`>`）。

#### 4.1.2 核心流程

```
to_numpy_code(code):
   code = code.lower()                     # 统一小写，'BIG' → 'big'
   if code is None        → return native_code
   if code in aliases['little']   → return '<'
   elif code in aliases['big']    → return '>'
   elif code in aliases['native'] → return native_code
   elif code in aliases['swapped']→ return swapped_code
   else                           → raise ValueError
```

其中 `aliases` 是一张「同义词分组表」：

| 组名 | 含义 | 接受的写法 |
|------|------|-----------|
| `little` | 小端 | `'little'`, `'<'`, `'l'`, `'le'` |
| `big` | 大端 | `'big'`, `'>'`, `'b'`, `'be'` |
| `native` | 本机默认 | `'native'`, `'='` |
| `swapped` | 本机的相反 | `'swapped'`, `'S'` |

> 关键技巧：`code.lower()` 在**最前面**执行一次，所以 `'BIG'`/`'Big'`/`'big'`、`'LE'`/`'le'` 都能命中，无需在 `aliases` 里枚举大小写变体。

#### 4.1.3 源码精读

本机字节序判定与两个常量：

[_byteordercodes.py:15-17](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_byteordercodes.py#L15-L17) —— `sys_is_le` 来自 `sys.byteorder`；`native_code` 用 `sys_is_le and '<' or '>'` 这个古早三元写法：本机小端则 `native_code='<'`，否则 `'>'`。`swapped_code` 取反。

同义词分组表：

[_byteordercodes.py:19-22](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_byteordercodes.py#L19-L22) —— 四个元组分别收集小端/大端/本机/反本机的各种字符串写法，`to_numpy_code` 用 `code in aliases['little']` 这类成员测试做归类。

归一化函数本体：

[_byteordercodes.py:25-75](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_byteordercodes.py#L25-L75) —— 第 62 行先 `code.lower()`，第 63-72 行按 `aliases` 四组依次匹配并返回 `<`/`>`/`native_code`/`swapped_code`，第 73-75 行对无法识别的输入抛 `ValueError`。docstring 里给了完整的可运行示例。

#### 4.1.4 代码实践

1. **目标**：验证 `to_numpy_code` 把各种写法都归一化成 `<`/`>`，并体会 `native_code`/`swapped_code` 随本机变化。
2. **步骤**：运行下面的代码。
3. **预期现象**：同义词返回同一字节码；`'native'` 与 `native_code` 一致。
4. **预期结果**：见注释。**待本地验证**（在小端机器上运行）。

```python
# 示例代码
from scipy.io.matlab._byteordercodes import (to_numpy_code, native_code,
                                             swapped_code, sys_is_le)
print('本机小端 ?', sys_is_le)            # 多数机器为 True
print('native_code =', native_code)       # 小端机器 -> '<'
print('swapped_code =', swapped_code)     # 小端机器 -> '>'

for s in ['little', '<', 'le', 'BIG', '>', 'be', 'native', '=', 'swapped', 'S']:
    print(f'{s!r:12} -> {to_numpy_code(s)!r}')
# 预期（小端机器）：
#   'little'     -> '<'
#   '<'          -> '<'
#   'le'         -> '<'
#   'BIG'        -> '>'      ← 大小写不敏感
#   '>'          -> '>'
#   'be'         -> '>'
#   'native'     -> '<'      ← 等于 native_code
#   '='          -> '<'
#   'swapped'    -> '>'      ← 等于 swapped_code
#   'S'          -> '>'
```

#### 4.1.5 小练习与答案

**练习 1**：`to_numpy_code('BIG')` 为什么能正常工作，而 `aliases` 字典里并没有列出 `'BIG'`？

**答案**：因为函数第 62 行先执行了 `code = code.lower()`，把 `'BIG'` 转成 `'big'` 后再去 `aliases['big']` 里匹配，因此只需在别名表里存小写形式即可。

**练习 2**：如果传入一个无法识别的字符串如 `'sideways'`，会发生什么？

**答案**：四个 `aliases` 分组都不命中，落到第 73-75 行，抛 `ValueError(f'We cannot handle byte order sideways')`。

---

### 4.2 异常与警告类

#### 4.2.1 概念说明

读写 `.mat` 涉及二进制解析，必然会出现各种「文件坏了」「格式不认识」的情况。`_miobase.py` 在文件最开头定义了四个轻量类，构成 MATLAB 子系统的**错误词汇表**：

- `MatReadError`：读取时出错（如文件被截断、损坏、版本不可识别）。
- `MatWriteError`：写入时出错。
- `MatReadWarning`：读取时的非致命问题（如某个变量无法解析但可跳过）。
- `MatWriteWarning`：写入时的非致命问题。

它们是**专门化**的：`MatReadError`/`MatWriteError` 继承 `Exception`，`MatReadWarning`/`MatWriteWarning` 继承 `UserWarning`。这样上层代码可以用 `except MatReadError` 精准捕获 MATLAB 读取错误，而不会误伤其它异常；也可以用 `warnings.filterwarnings(..., category=MatReadWarning)` 单独控制这类警告的显示。

#### 4.2.2 核心流程

```
class MatReadError(Exception):    ...   # 读取错误
class MatWriteError(Exception):   ...   # 写入错误
class MatReadWarning(UserWarning):...   # 读取警告
class MatWriteWarning(UserWarning):...  # 写入警告

__all__ = ['MatReadError', 'MatReadWarning', 'MatWriteError', 'MatWriteWarning']
```

注意 `__all__` **只**导出这四个类。这意味着：同文件里的 `MatFileReader`/`MatVarReader`/`matdims` 等虽然能被 `from scipy.io.matlab._miobase import MatFileReader` 访问到（它们是模块级名字），但**不算**这个模块的「公共导出名」——这是 SciPy 用 `__all__` 划分公共/私有的惯例（与 u1-l1、u3-l1 一致）。

#### 4.2.3 源码精读

四个类定义（极简，只承载类型语义）：

[_miobase.py:20-32](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_miobase.py#L20-L32) —— 两个 `Error` 继承 `Exception`，两个 `Warning` 继承 `UserWarning`，每个只有一行 docstring，没有任何方法。它们的全部价值在于「类型本身」。

`__all__` 只列四个：

[_miobase.py:16-18](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_miobase.py#L16-L18) —— 限定 `from ... import *` 只带走这四个错误/警告类。

> 真实使用场景：u3-l1 讲过的 `_get_matfile_version` 在检测到「文件不足 20 字节」或「前 20 字节全 0」时，抛的就是本节的 `MatReadError`（见 [_miobase.py:236-239](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_miobase.py#L236-L239)）。也就是说，那套版本判定逻辑的「报错出口」正是这里定义的类。

#### 4.2.4 代码实践

1. **目标**：亲手触发一次 `MatReadError`，确认它的类型与继承关系。
2. **步骤**：用一个空字节流喂给 `_get_matfile_version`（它内部会抛 `MatReadError`）。
3. **预期现象**：抛出 `MatReadError`，且 `isinstance(e, Exception)` 为真。
4. **预期结果**：见注释。**待本地验证**。

```python
# 示例代码
import io
from scipy.io.matlab._miobase import (_get_matfile_version, MatReadError,
                                      MatWriteError, MatReadWarning, MatWriteWarning)

# 一个「空文件」的字节流
empty = io.BytesIO(b'')
try:
    _get_matfile_version(empty)
except MatReadError as e:
    print('捕获到:', type(e).__name__, '|', e)
    print('是 Exception 吗 ?', isinstance(e, Exception))      # True
    print('是 MatWriteError 吗 ?', isinstance(e, MatWriteError))  # False（读≠写）

# 顺带确认四个类的继承
print(MatReadError.__mro__[1].__name__)    # Exception
print(MatReadWarning.__mro__[1].__name__)  # UserWarning
```

#### 4.2.5 小练习与答案

**练习 1**：为什么 `MatReadWarning` 继承的是 `UserWarning` 而不是 `Exception`？

**答案**：因为它是「警告」而非「异常」。Python 的警告体系（`warnings` 模块）以 `Warning` 及其子类（如 `UserWarning`）为类别；继承 `UserWarning` 才能用 `warnings.filterwarnings('ignore', category=MatReadWarning)` 单独控制它，且默认不会中断程序流程。

**练习 2**：`from scipy.io.matlab._miobase import *` 之后，能用到 `MatFileReader` 吗？

**答案**：不能（直接通过 `*` 导入拿不到）。因为模块的 `__all__` 只列了四个错误/警告类，`MatFileReader` 不在其中。若要用它，必须显式写 `from scipy.io.matlab._miobase import MatFileReader`。

---

### 4.3 docfiller：文档复用机制

#### 4.3.1 概念说明

`loadmat`、`whosmat`、`MatFileReader.__init__`、`MatFile5Reader.__init__`……这些函数/方法**共享**一大批相同的参数（`byte_order`、`mat_dtype`、`squeeze_me`、`chars_as_strings`、`matlab_compatible`、`struct_as_record`……）。如果每个函数的 docstring 都把这些参数说明复制粘贴一遍，既冗长又难维护——改一处要改很多处。

`docfiller` 解决的就是这个「文档重复」问题：把公共参数的说明集中放在一个字典 `doc_dict` 里，各函数的 docstring 里只写一个**占位符** `%(load_args)s`，然后用 `@docfiller` 装饰器在定义时把占位符**展开**成真实文本。从此参数说明只维护一份。

它的实现是 `doccer.filldoc(doc_dict)`（来自 `scipy._lib.doccer`）。

#### 4.3.2 核心流程

```
1) 定义 doc_dict = {'load_args': '...byte_order...\n...mat_dtype...\n...',
                    'file_arg': '...', 'struct_arg': '...', ...}

2) docfiller = doccer.filldoc(doc_dict)
        └─ filldoc 内部：把 doc_dict 里每段文本「去公共缩进」(unindent_dict)
        └─ 返回一个装饰器

3) @docfiller
   def __init__(self, ..., mat_dtype=False, ...):
       '''
       ...
       %(load_args)s        ← 占位符
       '''

   → 装饰器执行时：__init__.__doc__ 里的 %(load_args)s 被替换成 doc_dict['load_args']
   → 最终 __doc__ 变成完整的、带参数说明的文档
```

`doccer.filldoc` 默认会做一步「去缩进」处理（`unindent_params=True`）：因为 `doc_dict` 里的文本为了在字典里排版会有缩进，注入到不同位置时需要先把缩进抹平，再由目标 docstring 自行决定缩进。

#### 4.3.3 源码精读

参数文档片段的集中仓库：

[_miobase.py:35-85](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_miobase.py#L35-L85) —— `doc_dict` 收纳了 `file_arg`/`append_arg`/`load_args`/`struct_arg`/`matstream_arg`/`long_fields`/`do_compression`/`oned_as`/`unicode_strings` 等键。其中 `load_args`（第 44-59 行）这一段最为关键——它一次性描述了 `byte_order`/`mat_dtype`/`squeeze_me`/`chars_as_strings`/`matlab_compatible` 五个读取选项，被 `loadmat`/`whosmat`/`MatFileReader.__init__`/`MatFile5Reader.__init__` 共用。

构造装饰器：

[_miobase.py:87](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_miobase.py#L87) —— `docfiller = doccer.filldoc(doc_dict)` 产出一个绑定了 `doc_dict` 的装饰器，本文件内所有 `@docfiller` 都用它。

`filldoc` 的实现（佐证其工作机制）：

[scipy/_lib/doccer.py:263-289](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/doccer.py#L263-L289) —— 第 280-281 行先对 `docdict` 做去缩进；返回的 `decorate` 函数（第 283-287 行）读取被装饰对象的 `__doc__`，调用 `docformat(doc, docdict)` 把 `%(key)s` 替换成对应文本后写回 `__doc__`。注意第 285 行 `func.__doc__ or ""` 处理了 `-OO` 优化下 `__doc__` 为 `None` 的边界。

实际使用方（验证 `load_args` 确实被多处复用）：

[_mio.py:349-360](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio.py#L349-L360) —— `whosmat` 用 `@docfiller`，docstring 里写 `%(file_arg)s`、`%(append_arg)s`、`%(load_args)s`、`%(struct_arg)s` 四个占位符，展开后就是完整的参数文档。

[_miobase.py:361-377](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_miobase.py#L361-L377) —— `MatFileReader.__init__` 同样用 `@docfiller`，docstring 里只有 `%(load_args)s` 一个占位符。

#### 4.3.4 代码实践

1. **目标**：肉眼对比「占位符模板」与「展开后的真实 docstring」，确认 `docfiller` 做了替换。
2. **步骤**：打印 `MatFileReader.__init__` 的 `__doc__`，看其中是否已包含 `byte_order` 等参数说明（而源码模板里只有 `%(load_args)s`）。
3. **预期现象**：打印出的文档里能看到 `byte_order : str or None` 等字样，说明占位符已被展开。
4. **预期结果**：见注释。**待本地验证**。

```python
# 示例代码
from scipy.io.matlab._miobase import MatFileReader

doc = MatFileReader.__init__.__doc__ or ''
print('占位符 %(load_args)s 是否还残留 ?', '%(load_args)s' in doc)   # 预期 False（已展开）
print('是否展开了 byte_order 说明 ?', 'byte_order' in doc)            # 预期 True
print('是否展开了 squeeze_me 说明 ?', 'squeeze_me' in doc)            # 预期 True
# 打印前几行，可以看到展开后的「Parameters」段落
print(doc[:400])
```

#### 4.3.5 小练习与答案

**练习 1**：如果某天 SciPy 想给 `loadmat` 增加一个新参数 `foo`，并希望它的文档同时出现在 `loadmat`/`whosmat`/`MatFileReader.__init__` 三处，用 `docfiller` 机制该怎么做？

**答案**：只需在 `doc_dict` 里新增一段（比如加到 `load_args` 文本中，或新建一个键 `foo_arg`），三个函数的 docstring 里已经引用了 `%(load_args)s`（或可改为 `%(foo_arg)s`），装饰器会在定义时自动展开——文档只改一处，三处同步。

**练习 2**：`doccer.filldoc` 默认为什么要对 `doc_dict` 做「去缩进」（`unindent_params=True`）？

**答案**：因为 `doc_dict` 里的文本片段为了在字典字面量里对齐，会带有公共缩进；而这些片段要被注入到**不同函数**的 docstring 不同位置，各处缩进语境不同。先去掉片段自身的公共缩进，得到「顶格」文本，再让目标 docstring 的缩进去包裹它，才能保证注入后排版正确。

---

### 4.4 MatVarReader：变量读取器抽象接口

#### 4.4.1 概念说明

一个 `.mat` 文件里有**多个变量**，每个变量又是一段二进制（含一个头 + 数据）。`MatFileReader` 负责「文件级」的事（打开流、持有读取选项），而「逐个变量」的解析交给一个更小的对象——**变量读取器（var reader）**。

`MatVarReader` 就是这类对象的**抽象接口**：它规定「任何变量读取器都必须能做两件事」：

1. `read_header()`：从流里读出**当前变量的头**（名字、类型、形状、是否复数……）。
2. `array_from_header(header)`：根据上一步的头，读出**真正的数组**。

这是一个**纯接口**：`_miobase.py` 里的 `MatVarReader` 这三个方法（含 `__init__`）方法体都是 `pass`，不干任何实事。它的作用是「立规矩」，真正的实现由各版本子类提供：v4 用 `_mio4.VarReader4`（纯 Python），v5 用 `VarReader5`（Cython，见 u3-l7）。

#### 4.4.2 核心流程

```
MatVarReader 接口（抽象）：
   __init__(self, file_reader)        # 持有上层 file_reader 的引用
   read_header(self)      -> header   # 读当前变量的头
   array_from_header(self, header) -> ndarray   # 依头读数组

实现方：
   _mio4.VarReader4        (纯 Python，读 v4 变量)
   _mio5_utils.VarReader5  (Cython，读 v5 变量，性能关键)
```

`MatFile4Reader.initialize_read()`（u3-l4 会详讲）正是把 `VarReader4(self)` 存到 `self._matrix_reader`，之后所有「读下一个变量」的操作都委托给这个对象。

#### 4.4.3 源码精读

抽象接口本体（方法体都是 `pass`）：

[_miobase.py:337-348](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_miobase.py#L337-L348) —— `MatVarReader` 定义了 `__init__(self, file_reader)`、`read_header(self)`、`array_from_header(self, header)` 三个方法，每个都只有 docstring、方法体 `pass`。它是一份「契约」，靠鸭子类型而非 `abc.ABC` 强制。

v4 的具体实现（佐证接口被实现）：

[_mio4.py:107](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio4.py#L107) —— `class VarReader4:` 实现了 `read_header`/`array_from_header` 等方法，构造器形如 `VarReader4(file_reader)`，与抽象接口的签名一致。

v5 的具体实现（Cython，性能关键）：

[_mio5_utils.pyx:141](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio5_utils.pyx#L141) —— `cdef class VarReader5:` 同样实现这套接口，但用 Cython 编写以加速 v5 复杂矩阵的解析（详见 u3-l7）。

> 设计要点：`MatVarReader` 没有用 `abc.abstractmethod`。这是一种「靠文档约定、靠鸭子类型」的轻量抽象——子类只要「恰好」提供了同名方法即可，不必显式继承 `MatVarReader`。好处是 Cython 类、纯 Python 类都能无负担地接入。

#### 4.4.4 代码实践

1. **目标**：确认 `MatVarReader` 的接口方法体是空的（纯契约），并观察 v4 reader 内部确实持有一个实现该接口的对象。
2. **步骤**：检视 `MatVarReader` 的方法源码，再构造一个 `MatFile4Reader` 并触发 `initialize_read`，看 `self._matrix_reader` 的类型。
3. **预期现象**：抽象方法体为 `pass`；v4 reader 的 `_matrix_reader` 是 `VarReader4`。
4. **预期结果**：**源码阅读型实践**。**待本地验证**。

```python
# 示例代码
import inspect
from scipy.io.matlab._miobase import MatVarReader

# (a) 抽象接口的方法体都是 pass
for name in ['read_header', 'array_from_header']:
    src = inspect.getsource(getattr(MatVarReader, name))
    print(f'--- MatVarReader.{name} ---')
    print(src.strip())   # 预期：只有 docstring + pass

# (b) 真实 v4 reader 内部持有一个实现了该接口的 VarReader4
import os, scipy.io as sio
from scipy.io.matlab._mio import mat_reader_factory
data_dir = os.path.join(os.path.dirname(sio.__file__), 'matlab', 'tests', 'data')
MR, _ = mat_reader_factory(os.path.join(data_dir, 'testdouble_4.2c_SOL2.mat'))
MR.initialize_read()
print('var reader 类型 =', type(MR._matrix_reader).__name__)   # 预期 VarReader4
MR.mat_stream.close()
```

#### 4.4.5 小练习与答案

**练习 1**：`MatVarReader` 的方法体都是 `pass`，那它存在的意义是什么？

**答案**：它是「接口契约」与「文档载体」——通过这个类及其 docstring 告诉所有实现者「一个合格的变量读取器必须提供 `read_header` 和 `array_from_header`」。它不强制（没用 `abc`），但任何想接入的类都会照此实现。

**练习 2**：为什么 v5 的变量读取器 `VarReader5` 用 Cython 而不是纯 Python？

**答案**：因为 v5 格式复杂（cell/struct/稀疏/压缩元素等），逐元素解析是性能热点。用 Cython 能把热点循环编译成 C，显著加速；同时它仍遵守 `MatVarReader` 的接口，上层无感知（这正是 u3-l7 的主题）。

---

### 4.5 MatFileReader：文件读取器基类与可配置开关

#### 4.5.1 概念说明

`MatFileReader` 是所有 reader 的**根基类**：`MatFile4Reader` 和 `MatFile5Reader` 都继承它。它做两件事：

1. **集中管理读取选项**：把 `byte_order`/`mat_dtype`/`squeeze_me`/`chars_as_strings`/`matlab_compatible`/`struct_as_record`/`simplify_cells` 等开关统一存为实例属性，供后续解析时查阅。
2. **定义可覆写的钩子**：基类给一个「偷懒」的 `guess_byte_order`（直接返回本机字节序），子类覆写成「认真」的版本（从文件头真实探测）。

源码文件里紧挨着类定义上方有一段**架构注释**（`Note on architecture`），点明了读取时存在**三组参数**的分层：

- **文件级参数（file read parameters）**：对整个文件、每个变量都生效，如 `mat_stream`/`byte_order`/`chars_as_strings`/`squeeze_me`/`struct_as_record`——这些正是 `MatFileReader.__init__` 存储的。
- **变量级参数（header）**：只对「当前正在读的这一个变量」生效，如 `is_complex`/`mclass`/`var_stream`——由 `MatVarReader.read_header` 产出，作为数据对象在函数间传递。
- **元素级参数（element read parameters）**：对矩阵里的每个元素（如 cell 数组的一个 cell）生效，目前只有 `mat_dtype`，会传给 `mio_utils` 里的后处理函数。

理解这个分层，就能理解为什么 `mat_dtype` 既出现在 `__init__` 又会在元素读时被传递——它本质是个「元素级」选项，但默认值在文件级设定。

#### 4.5.2 核心流程

`MatFileReader.__init__` 的选项处理有一套**覆盖优先级**（后执行的覆盖先执行的）：

```
__init__(mat_stream, byte_order=None, mat_dtype=False, squeeze_me=False,
         chars_as_strings=True, matlab_compatible=False, struct_as_record=True,
         verify_compressed_data_integrity=True, simplify_cells=False):

  1) self.mat_stream = mat_stream; self.dtypes = {}

  2) byte_order 解析（优先级：显式传入 > 猜测）：
        if not byte_order:  byte_order = self.guess_byte_order()   # 调钩子
        else:               byte_order = boc.to_numpy_code(byte_order)
        self.byte_order = byte_order

  3) self.struct_as_record = struct_as_record        # 先按入参设（默认 True）

  4) 选项预设（优先级：matlab_compatible > 单独开关）：
        if matlab_compatible:  self.set_matlab_compatible()   # 覆盖 mat_dtype/squeeze_me/chars_as_strings
        else:                  按入参设 squeeze_me/chars_as_strings/mat_dtype

  5) simplify_cells 兜底覆盖（优先级最高，最后执行）：
        if simplify_cells:
            self.squeeze_me = True
            self.struct_as_record = False
```

于是三个开关的最终取值遵循如下优先级：

| 开关 | 优先级（高 → 低） |
|------|------------------|
| `squeeze_me` | `simplify_cells` > `matlab_compatible` > 入参 `squeeze_me` |
| `struct_as_record` | `simplify_cells` > 入参 `struct_as_record` |
| `mat_dtype` | `matlab_compatible` > 入参 `mat_dtype` |
| `chars_as_strings` | `matlab_compatible` > 入参 `chars_as_strings` |

`matlab_compatible` 和 `simplify_cells` 都是「预设快捷方式」，区别是方向相反：`matlab_compatible` 让结果**尽量像 MATLAB 原生加载**（不 squeeze、不转字符串、保留 dtype），`simplify_cells` 让结果**尽量像普通 Python 嵌套 dict**（强制 squeeze、struct 不用 record）。

#### 4.5.3 源码精读

架构注释（理解三组参数分层）：

[_miobase.py:89-131](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_miobase.py#L89-L131) —— `Note on architecture`：阐明「文件级 / 变量级（header）/ 元素级」三组参数的分层，并点名 `mat_dtype` 属于元素级、由 `mio_utils` 后处理。

基类构造器（选项存储与覆盖逻辑）：

[_miobase.py:361-397](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_miobase.py#L361-L397) —— 第 381-385 行处理 `byte_order`（无则调 `guess_byte_order`，有则 `to_numpy_code` 归一化）；第 386 行无条件设 `struct_as_record`；第 387-392 行的 `if matlab_compatible / else` 实现预设覆盖；第 394-397 行 `simplify_cells` 兜底覆盖 `squeeze_me`/`struct_as_record`。

`matlab_compatible` 预设的实现：

[_miobase.py:399-403](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_miobase.py#L399-L403) —— `set_matlab_compatible` 把 `mat_dtype=True`、`squeeze_me=False`、`chars_as_strings=False`。注意它**不**碰 `struct_as_record`（依赖入参默认值 `True`）。

字节序探测钩子（基类的「偷懒」默认实现）：

[_miobase.py:405-407](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_miobase.py#L405-L407) —— 基类 `guess_byte_order` 直接返回 `boc.native_code`（本机字节序），不读文件。docstring 里的类说明（第 356-358 行）点名：子类**必须**覆写 `guess_byte_order` 与 `matrix_getter_factory` 才能用。

子类覆写（v4：从 `MOPT` 整数探测）：

[_mio4.py:328-338](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio4.py#L328-L338) —— `MatFile4Reader.guess_byte_order` 读文件开头的 `MOPT` 4 字节整数：值为 0 → 小端；值为负或超 5000 → 说明被字节交换过，取本机的反序；否则取本机序。

子类覆写（v5：从 `'IM'/'MI'` 探针探测）：

[_mio5.py:209-215](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio5.py#L209-L215) —— `MatFile5Reader.guess_byte_order` seek 到偏移 126 读 2 字节：等于 `b'IM'` → 小端 `'<'`，否则大端 `'>'`。这与 u3-l1 讲的「字节序探针」是同一组字节。

> 这就是**模板方法模式**的典型应用：`__init__` 里写 `self.guess_byte_order()`，基类提供「返回本机序」的保底实现；子类各自覆写成「读文件头探测」。上层代码无需知道具体子类，调用的都是同一个钩子名。

辅助函数（消费字节序码）：

[_miobase.py:134-154](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_miobase.py#L134-L154) —— `convert_dtypes(dtype_template, order_code)` 用 `np.dtype(val).newbyteorder(order_code)` 把一组 dtype 模板按当前字节序重新生成，子类（如 `MatFile4Reader.initialize_read`）正是用它把类型表刷成文件的字节序。

#### 4.5.4 代码实践

1. **目标**：观察「预设覆盖单独开关」的真实效果——同一份文件，不同选项组合下 `squeeze_me`/`mat_dtype` 的最终取值不同。
2. **步骤**：分别用单独开关、`matlab_compatible`、`simplify_cells` 构造 reader，打印内部开关。
3. **预期现象**：`matlab_compatible=True` 会把 `mat_dtype` 刷成 `True`；`simplify_cells=True` 会把 `squeeze_me` 刷成 `True`、`struct_as_record` 刷成 `False`。
4. **预期结果**：见注释。**待本地验证**。

```python
# 示例代码
import os, scipy.io as sio
from scipy.io.matlab._mio4 import MatFile4Reader
from scipy.io.matlab._byteordercodes import native_code

data_dir = os.path.join(os.path.dirname(sio.__file__), 'matlab', 'tests', 'data')
path = os.path.join(data_dir, 'testdouble_4.2c_SOL2.mat')

def show(tag, **kw):
    f = open(path, 'rb')
    MR = MatFile4Reader(f, **kw)
    print(f'{tag:28} byte_order={MR.byte_order!r} squeeze_me={MR.squeeze_me} '
          f'mat_dtype={MR.mat_dtype} struct_as_record={MR.struct_as_record} '
          f'chars_as_strings={MR.chars_as_strings}')
    f.close()

show('默认')                          # squeeze=F mat_dtype=F struct=T chars=T ; byte_order 由 guess 探测
show('squeeze_me=True', squeeze_me=True)
show('matlab_compatible=True', matlab_compatible=True)   # mat_dtype 被刷成 True
show('simplify_cells=True', simplify_cells=True)         # squeeze=T struct=F
show('byte_order=BIG', byte_order='BIG')                 # byte_order 归一化为 '>'
```

#### 4.5.5 小练习与答案

**练习 1**：如果同时传 `matlab_compatible=True` 和 `squeeze_me=True`，最终的 `squeeze_me` 是什么？

**答案**：是 `False`。因为 `matlab_compatible=True` 会走 `set_matlab_compatible()`，把 `squeeze_me` 强制设为 `False`，覆盖掉用户传入的 `True`——这正是「预设优先级高于单独开关」的体现。

**练习 2**：如果同时传 `matlab_compatible=True` 和 `simplify_cells=True`，`squeeze_me`/`struct_as_record` 最终是什么？

**答案**：`squeeze_me=True`、`struct_as_record=False`。因为 `simplify_cells` 的覆盖在代码里**最后**执行（第 394-397 行），优先级最高，会盖掉 `matlab_compatible` 设的值。

**练习 3**：基类的 `guess_byte_order` 返回什么？为什么子类必须覆写它？

**答案**：返回 `boc.native_code`（本机字节序），完全不读文件。若不覆写，就读不懂「与本机字节序相反」的文件（比如小端机器读大端 `.mat`）；子类覆写后能从文件头真实探测出文件的字节序。

---

## 5. 综合实践

把本讲五个最小模块串起来：先用 `to_numpy_code` 把字节序字符串归一化，再通过一张表把 `MatFileReader.__init__` 的六个开关讲清，最后用一段实验验证「预设覆盖单独开关」的优先级。这个任务覆盖 byteordercodes、异常类、docfiller、MatVarReader、MatFileReader 全部知识点。

**任务 1：六个读取开关的作用表**

阅读 [_miobase.py:361-397](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_miobase.py#L361-L397) 的 `MatFileReader.__init__`，结合 docstring（[_miobase.py:44-66](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_miobase.py#L44-L66) 的 `load_args`/`struct_arg`），填写下表（答案已给出，请你对照源码确认每一行）：

| 开关 | 默认值 | 作用 | 谁能覆盖它 |
|------|--------|------|-----------|
| `squeeze_me` | `False` | 是否压缩掉数组中长度为 1 的维度（如 `1x3` → `(3,)`） | `matlab_compatible`（设 False）、`simplify_cells`（设 True） |
| `chars_as_strings` | `True` | 是否把 MATLAB char 数组转成 Python/NumPy 字符串数组 | `matlab_compatible`（设 False） |
| `mat_dtype` | `False` | 是否按「MATLAB 加载时的 dtype」返回（而非保存时的 dtype，如把 int8 升成 double） | `matlab_compatible`（设 True） |
| `struct_as_record` | `True` | 是否把 MATLAB struct 加载成 NumPy record array（True）或 object array（False，复刻 SciPy 0.7.x 行为） | `simplify_cells`（设 False） |
| `matlab_compatible` | `False` | **预设快捷方式**：让结果尽量像 MATLAB 原生加载（等价于 `squeeze_me=False`+`chars_as_strings=False`+`mat_dtype=True`，并依赖 `struct_as_record=True`） | — |
| `simplify_cells` | `False` | **预设快捷方式**：把 cell/struct 嵌套结构简化成普通嵌套 dict（强制 `squeeze_me=True`+`struct_as_record=False`） | — |

**任务 2：用 `to_numpy_code` 归一化字节序字符串**

```python
# 示例代码
from scipy.io.matlab._byteordercodes import to_numpy_code, native_code, swapped_code

# 把 'native'/'BIG' 等「人类写法」转成 numpy 字节码
samples = ['native', '=', 'BIG', '>', 'little', 'le', 'swapped', 'S']
for s in samples:
    print(f'{s!r:10} -> numpy 字节码 {to_numpy_code(s)!r}')

print('本机 native_code =', native_code, '| 反序 swapped_code =', swapped_code)
# 预期（小端机器）：
#   'native'   -> '<'
#   '='        -> '<'
#   'BIG'      -> '>'
#   '>'        -> '>'
#   'little'   -> '<'
#   'le'       -> '<'
#   'swapped'  -> '>'
#   'S'        -> '>'
```

**任务 3：验证优先级（把五个模块的知识用上）**

下面的脚本同时验证：byteordercodes 的归一化（`byte_order='BIG'` → `'>'`）、`matlab_compatible` 预设、`simplify_cells` 预设的覆盖、以及 reader 内部确实持有一个实现 `MatVarReader` 接口的对象。

```python
# 示例代码：综合实践
import os, scipy.io as sio
from scipy.io.matlab._mio4 import MatFile4Reader
from scipy.io.matlab._byteordercodes import to_numpy_code

data_dir = os.path.join(os.path.dirname(sio.__file__), 'matlab', 'tests', 'data')
path = os.path.join(data_dir, 'testdouble_4.2c_SOL2.mat')

def probe(tag, **kw):
    f = open(path, 'rb')
    MR = MatFile4Reader(f, **kw)
    print(f'{tag:26} byte_order={MR.byte_order} squeeze={MR.squeeze_me} '
          f'mat_dtype={MR.mat_dtype} struct_as_record={MR.struct_as_record}')
    f.close()

probe('默认')                                  # byte_order 由 guess_byte_order 探测
probe('显式 BIG', byte_order='BIG')            # 验证 to_numpy_code 归一化为 '>'
probe('matlab_compatible', matlab_compatible=True)  # mat_dtype 被 set_matlab_compatible 刷成 True
probe('simplify_cells', simplify_cells=True)        # squeeze=T, struct_as_record=F
probe('compatible + simplify', matlab_compatible=True, simplify_cells=True)
# 最后一行：simplify_cells 最后执行，squeeze=T(struct=F)，但 mat_dtype 仍是 compatible 设的 True

# 顺带验证 MatVarReader 接口被实现
f = open(path, 'rb'); MR = MatFile4Reader(f); MR.initialize_read()
print('var reader =', type(MR._matrix_reader).__name__)   # 预期 VarReader4
f.close()
```

**观察要点**：

1. 「显式 BIG」一行 `byte_order` 显示为 `'>'`，证明 `to_numpy_code` 把 `'BIG'` 归一化成了 numpy 大端码——这正是 `__init__` 第 384 行 `boc.to_numpy_code(byte_order)` 的作用。
2. 「matlab_compatible」一行 `mat_dtype` 变成 `True`，而用户并没有传 `mat_dtype=True`——这是 `set_matlab_compatible()` 的覆盖效果。
3. 「simplify_cells」一行 `squeeze_me=True`、`struct_as_record=False`，与「matlab_compatible」相反，证明两个预设方向不同、且 `simplify_cells` 优先级最高。
4. 最后一行同时开两个预设：`squeeze_me`/`struct_as_record` 听 `simplify_cells`（后执行），`mat_dtype` 听 `matlab_compatible`——三个开关分属不同覆盖链，互不干扰。

**预期结果**（由源码逻辑推得，**待本地验证**）：

- 默认：`byte_order` 为本机探测结果，`squeeze=False mat_dtype=False struct_as_record=True`。
- 显式 BIG：`byte_order='>'`，其余同默认。
- matlab_compatible：`mat_dtype=True`、`squeeze=False`、`chars_as_strings=False`。
- simplify_cells：`squeeze=True`、`struct_as_record=False`。
- var reader：`VarReader4`。

## 6. 本讲小结

- `_byteordercodes` 是字节序**归一化层**：`to_numpy_code` 先 `lower()` 再按 `aliases` 同义词表，把 `'native'`/`'BIG'`/`'<'`/`'le'` 等各种写法统一成 numpy 的 `<`/`>`；`native_code`/`swapped_code` 随本机字节序而定。
- `_miobase.py` 顶部定义了四个轻量类 `MatReadError`/`MatWriteError`/`MatReadWarning`/`MatWriteWarning`，是 MATLAB 子系统的错误/警告词汇表；模块 `__all__` 只导出这四个。
- `docfiller = doccer.filldoc(doc_dict)` 是文档复用装饰器：把公共参数说明集中在 `doc_dict`，函数 docstring 里用 `%(load_args)s` 占位，定义时自动展开——一份说明多处复用。
- `MatVarReader` 是变量读取器的**纯接口**（方法体 `pass`），约定 `read_header`/`array_from_header`；v4 由 `VarReader4` 实现、v5 由 Cython `VarReader5` 实现。
- `MatFileReader` 是所有 reader 的根基类：`__init__` 集中存储读取选项，选项间有覆盖优先级（`simplify_cells` > `matlab_compatible` > 单独开关）；`guess_byte_order` 是模板方法钩子，基类返回本机序，`MatFile4Reader`/`MatFile5Reader` 各自覆写成「从文件头真实探测」。

## 7. 下一步学习建议

本讲只把「reader 的共性骨架」搭起来了，还没有真正调用 `loadmat` 读完一个变量。建议按以下顺序继续：

1. **u3-l3 loadmat / savemat / whosmat 主流程**：精读 `_mio.py` 三个公共函数的完整调用链，重点理解本讲的 `squeeze_me`/`struct_as_record`/`simplify_cells` 等开关如何**改变返回结构**（struct 是 dict 还是 record array、cell 是否被简化）。
2. **u3-l4 MAT v4 读写**：进入第一个具体版本。对照本讲的 `MatFile4Reader.guess_byte_order`、`convert_dtypes`、`VarReader4`，看它们在真实 v4 解析里如何被调用。
3. **u3-l5 / u3-l6 MAT v5 读 / 写**：看 `MatFile5Reader` 如何覆写 `guess_byte_order`、如何用 `matrix_getter_factory`（基类 docstring 点名的另一个必须覆写的方法）。
4. **u3-l7 Cython 底层**：回到本讲提到的 `VarReader5`（Cython），理解为什么 v5 的变量读取要下沉到 Cython。
5. 若对「错误处理」感兴趣，可提前扫一眼 u4-l4——那里会系统对比各格式（含 MATLAB 的四个异常/警告类）在损坏输入下的行为。
