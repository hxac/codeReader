# 字符串矩阵语法与 _convert_from_string

## 1. 本讲目标

本讲聚焦 `np.matrix` 的一个用起来很方便、但实现很值得读的入口：**用字符串描述一个矩阵**。

例如 `np.matrix('1 2; 3 4')` 直接得到一个 2×2 矩阵。这种语法对从 MATLAB 迁移过来的用户尤其友好。

学完本讲，你应当能够：

- 说出字符串矩阵语法的三种分隔符 `;`、`,`、空格各自的含义与优先级。
- 解释方括号 `[` `]` 为何会被默默剥离。
- 理解为什么解析每个元素时用的是 `ast.literal_eval` 而不是危险的 `eval`。
- 在源码中精确定位「行与行列数不一致时抛出 `ValueError`」的那几行代码。
- 对照源码，自己手写一个最小版的 `_convert_from_string`，并复现官方实现的报错信息。

---

## 2. 前置知识

在开始前，你需要先具备上一讲（u2-l1）建立的认知：

- `matrix` 类不写 `__init__`，所有构造工作都在 `__new__` 里完成，函数签名为 `matrix(data, dtype=None, copy=True)`。
- `__new__` 会按输入类型走不同分支：`matrix` 输入、`ndarray` 输入、字符串输入、通用 array_like 输入。**字符串分支**就是本讲的主角。

本讲还会用到几个 Python 基础概念，先一句话解释清楚：

- **字面量（literal）**：源码里直接写出来的常量，比如 `1`、`3.14`、`True`、`None`、`'abc'`、`[1, 2]`。它们「写出来是什么就是什么」，不需要查任何变量表。
- **`ast.literal_eval(s)`**：Python 标准库 `ast` 提供的「安全求值器」，它只把字符串 `s` 当作一个**字面量**来解析，能识别数字、布尔、字符串、列表、元组、字典等，但**拒绝**变量名、函数调用、运算表达式以外的可执行内容。它和 `eval` 的关键区别正是「安全」。
- **嵌套列表（nested list）**：形如 `[[1, 2], [3, 4]]` 的「列表的列表」，是描述二维结构最自然的方式，`np.array([[1,2],[3,4]])` 能直接吃掉它。

> 提醒：本讲只讲 `_convert_from_string`，它是 `matrix(...)` 字符串输入的解析器。numpy 里还有一个**名字很像但完全不同**的函数叫 `_from_string`，它是 `bmat(...)` 字符串输入的名字解析器，会在下一讲（u2-l3）专门讲。不要把两者搞混。

---

## 3. 本讲源码地图

本讲只涉及一个核心源码文件，以及一个测试文件作为实践依据。

| 文件 | 作用 |
| --- | --- |
| `numpy/matrixlib/defmatrix.py` | 全部业务实现。本讲关注其中两个位置：模块顶部的 `_convert_from_string` 函数，以及 `matrix.__new__` 里调用它的一行。 |
| `numpy/matrixlib/tests/test_defmatrix.py` | matrixlib 的测试。本讲会引用其中验证字符串解析与报错的用例。 |

> 一个容易忽略的细节：`_convert_from_string` 并不属于 `matrix` 类，它是模块级的「私有辅助函数」（名字以下划线开头）。它不直接产生 `matrix` 对象，而是先把字符串解析成一个 **Python 嵌套列表**，再把这个列表交回 `__new__` 继续处理。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，恰好对应 `_convert_from_string` 内部从上到下的四段逻辑：

1. `_convert_from_string` 函数全貌与它在 `__new__` 中的角色。
2. 字符串语法：分隔符优先级与方括号剥离。
3. `ast.literal_eval`：为什么不用 `eval`。
4. 行列校验：保证「矩阵是方的」。

### 4.1 `_convert_from_string` 函数全貌

#### 4.1.1 概念说明

当用户写 `np.matrix('1 2; 3 4')` 时，`__new__` 收到的 `data` 是一个字符串。`matrix` 本身不能直接拿字符串当数据源，它最终需要的是一个**数字二维结构**。所以需要一个「翻译官」：把人类友好的字符串翻译成 Python 能直接理解的嵌套列表 `[[1, 2], [3, 4]]`，然后才交给后续的 `np.array` 去定形。

这个翻译官就是 `_convert_from_string`。

两个关键认知：

- 它**不返回 ndarray，也不返回 matrix**，而是返回一个普通的 Python `list[list]`。把它变成矩阵是 `__new__` 下半段的工作。
- 它是「私有」的（下划线前缀），用户通常不会直接调用，但它支撑了 `matrix` 最便捷的构造方式。

#### 4.1.2 核心流程

整个函数可以概括为四步流水线：

```text
输入字符串 '1 2; 3 4'
   │
   ▼ 1) 剥离方括号 [ ]
   '1 2; 3 4'
   │
   ▼ 2) 按 ';' 分行   →  ['1 2', ' 3 4']
   │
   ▼ 3) 每行先按 ',' 分块、再按空格分元素、再 literal_eval 求值
   [[1, 2], [3, 4]]
   │
   ▼ 4) 校验每行列数都等于第一行 → 通过则返回
   返回 [[1, 2], [3, 4]]
```

#### 4.1.3 源码精读

先把整个函数看一遍（只有 18 行）：

[numpy/matrixlib/defmatrix.py:16-33](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L16-L33) —— `_convert_from_string` 的完整定义，负责把字符串解析成嵌套列表。

关键点逐条对照：

- 第 17-18 行 `for char in '[]': data = data.replace(char, '')`：把 `[` 和 `]` 两个字符无条件删掉，所以 `np.matrix('[[1, 2], [3, 4]]')` 也能正常工作。
- 第 20 行 `rows = data.split(';')`：分号分**行**。
- 第 22 行 `for count, row in enumerate(rows)`：逐行处理，`count` 记录这是第几行（0 起），后面校验要用。
- 第 27 行 `newrow.extend(map(ast.literal_eval, temp))`：对每个元素调用 `ast.literal_eval`，从字符串变成真实的 Python 数字/布尔值。
- 第 28-31 行：第一行记录列数 `Ncols`，之后的每一行都必须与之相等，否则抛 `ValueError("Rows not the same size.")`。

再看它在 `__new__` 里被调用的位置：

[numpy/matrixlib/defmatrix.py:147-148](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L147-L148) —— `__new__` 在判断出 `data` 是字符串后，调用 `_convert_from_string` 把它转成嵌套列表。

注意第 148 行的写法是 `data = _convert_from_string(data)`：它**用转换结果覆盖了 `data`**。从此往下，`data` 就不再是字符串，而是一个嵌套列表，于是能顺理成章地进入紧随其后的通用分支：

[numpy/matrixlib/defmatrix.py:150-152](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L150-L152) —— 把（已是嵌套列表的）`data` 交给 `np.array` 正式定形成 ndarray。

这就是「字符串分支」与「通用 array_like 分支」汇合的地方：`_convert_from_string` 把字符串「降级」成一个通用输入，让后面的代码不用为字符串再写一遍。

#### 4.1.4 代码实践

**实践目标**：验证 `_convert_from_string` 的返回值确实是普通 Python 嵌套列表，而不是 matrix 或 ndarray。

**操作步骤**：

1. 打开一个 Python 终端，导入 numpy 并抑制 `PendingDeprecationWarning`。
2. 直接读取 numpy 内部的 `_convert_from_string`（注意完整路径）。
3. 把一个字符串传给它，打印返回值的类型和内容。

**示例代码**（示例代码，可在本地 REPL 直接运行）：

```python
import warnings
warnings.simplefilter('ignore')          # 抑制 matrix 的弃用警告
import numpy as np
from numpy.matrixlib.defmatrix import _convert_from_string

result = _convert_from_string('1 2; 3 4')
print(type(result))   # <class 'list'>
print(result)          # [[1, 2], [3, 4]]
```

**需要观察的现象**：`type(result)` 是 `list`，而不是 `numpy.ndarray` 或 `numpy.matrixlib.defmatrix.matrix`。

**预期结果**：输出 `list` 和 `[[1, 2], [3, 4]]`。这印证了「函数只负责解析，不负责建矩阵」。

#### 4.1.5 小练习与答案

**练习 1**：既然 `_convert_from_string` 只返回嵌套列表，那字符串里的 `True` 最终是 Python 的 `bool` 还是 numpy 的布尔？请用上一节的方法把 `'True; False'` 传进去验证。

**参考答案**：是 Python 的 `bool`。`_convert_from_string('True; False')` 返回 `[[True], [False]]`，元素都是 `bool` 类型。要等到 `np.array` 那一步才会被转成 numpy 的布尔类型。

**练习 2**：`_convert_from_string` 是 `matrix` 类的方法吗？为什么它定义在类外面？

**参考答案**：不是类方法，是模块级函数。它定义在 `defmatrix.py` 顶部、`matrix` 类之前，因为它的产物是一个「与 matrix 无关」的通用嵌套列表，本身不依赖 `matrix` 的任何状态，写成模块级工具函数更合理。

---

### 4.2 字符串语法：分隔符优先级与方括号剥离

#### 4.2.1 概念说明

字符串矩阵语法只有三条规则，但理解它们的**优先级**很重要：

| 分隔符 | 作用 | 优先级 |
| --- | --- | --- |
| `;` | 分**行**（最高优先级，先切） | 1（最先） |
| `,` | 分**列块**（同一行内先于空格） | 2 |
| 空白（空格/制表符等） | 分**单个元素**（最低优先级，最后切） | 3（最后） |

另外，方括号 `[` 和 `]` 不承担任何分隔职责，它们只是「装饰」，在解析一开始就被删掉。

之所以这样设计，是因为它要同时满足两类写法：

- MATLAB 风格 `np.matrix('1 2; 3 4')`（空格分列、分号分行）。
- 类 Python 风格 `np.matrix('1,2;3,4')`（逗号分列、分号分行）。
- 甚至混着写 `np.matrix('1 2, 3 4; 5 6, 7 8')` 也行。

#### 4.2.2 核心流程

把一条字符串按优先级逐层拆开，可以用下面这张流程图理解（以 `'1 2, 3 4; 5 6, 7 8'` 为例）：

```text
原始: '1 2, 3 4; 5 6, 7 8'
  │  剥掉 [ ]
  ▼
  │  split(';')  → 2 行
  ▼
行0 '1 2, 3 4'        行1 '5 6, 7 8'
  │  split(',')         │  split(',')
  ▼  → ['1 2', ' 3 4']  ▼  → ['5 6', ' 7 8']
  │  每块 split()        │  每块 split()
  ▼  → ['1','2'],['3','4'] ▼ → ['5','6'],['7','8']
  │  extend 成一行        │  extend 成一行
  ▼                       ▼
[1, 2, 3, 4]          [5, 6, 7, 8]
```

每一层 split 都比上一层「更细」：`;` 切出粗的行，`,` 在行内切出块，空格在块内切出最小元素。

注意 `split()`（无参数）有一个很贴心的特性：它默认按任意空白切分，并且**会自动忽略首尾和连续的空白**。所以 `' 3 4'` 前面那个空格不会产生空元素，结果是干净的 `['3', '4']`。

#### 4.2.3 源码精读

方括号剥离在最开头，一行循环搞定：

[numpy/matrixlib/defmatrix.py:17-18](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L17-L18) —— 把 `[` 和 `]` 从字符串里删掉，使带方括号的写法也能被接受。

接着是「分行 → 分块 → 分元素」的三重循环：

[numpy/matrixlib/defmatrix.py:20-27](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L20-L27) —— 先按 `;` 分行，再按 `,` 分块，最后按空格分元素。

这里有一个值得玩味的细节：第 23 行 `trow = row.split(',')` 把一行切成若干「块」，第 26 行 `temp = col.split()` 把每块切成单个元素，然后第 27 行用 `extend` 把它们**展平追加**到 `newrow`。

也就是说，`,` 和空格在「列」这一层是**等价且可混用**的——它们都会把元素追加到同一行里。这正是 `np.matrix('1, 2 3')` 能得到 `[[1, 2, 3]]` 的原因：逗号切出的 `'1'` 和空格切出的 `'2'`、`'3'` 都进了同一行。

#### 4.2.4 代码实践

**实践目标**：亲手对比三种写法（空格、逗号、混合）是否得到同一个矩阵。

**操作步骤**：

1. 抑制警告后，分别用三种字符串构造 `matrix`。
2. 用 `np.array_equal` 两两比较。

**示例代码**（示例代码）：

```python
import warnings; warnings.simplefilter('ignore')
import numpy as np

a = np.matrix('1 2 3 4; 5 6 7 8')
b = np.matrix('1,2,3,4;5,6,7,8')
c = np.matrix('1 2, 3 4; 5 6, 7 8')

print(np.array_equal(a, b))   # True
print(np.array_equal(a, c))   # True
print(repr(a))
```

**需要观察的现象**：三种写法两两相等。

**预期结果**：两次都打印 `True`，`a` 为 `matrix([[1, 2, 3, 4], [5, 6, 7, 8]])`。

**待本地验证**：如果你的 numpy 版本与本文 HEAD（`b21650c4f6`）不同，repr 的排版可能略有差异，但相等性结论不变。

#### 4.2.5 小练习与答案

**练习 1**：`np.matrix('[1 2; 3 4]')` 会报错吗？为什么？

**参考答案**：不会报错。开头两行 `data.replace(char, '')` 把 `[` 和 `]` 删掉了，剩下 `'1 2; 3 4'` 再正常解析，结果是 `[[1, 2], [3, 4]]`。方括号只是装饰。

**练习 2**：如果有人写 `np.matrix('1,  2,   3')`（逗号后跟多个空格），会得到三个元素还是把多空格当成分隔产生空元素？

**参考答案**：得到三个元素 `[[1, 2, 3]]`。因为 `split(',')` 切出 `'1'`、`'  2'`、`'   3'`，随后 `col.split()`（无参）会忽略多余空白，得到干净的 `'2'`、`'3'`。这体现了 `split()` 与 `split(' ')`（会保留空串）的区别。

---

### 4.3 `ast.literal_eval`：为什么不用 eval

#### 4.3.1 概念说明

第 27 行对每个元素调用了 `ast.literal_eval`。这是整个解析器最值得讲的一行，因为它关系到**安全**。

考虑这样一个问题：如果用 Python 内置的 `eval` 去解析每个元素，会发生什么？

```python
eval('1')              # 1，没问题
eval('__import__("os").system("rm -rf ~")')  # 灾难
```

`eval` 会执行**任意** Python 表达式。如果用户传入的字符串（或者来自不可信数据源）里夹带了一段恶意代码，`eval` 就会照单全收地执行它。对于 `np.matrix` 这种被广泛使用的入口，把字符串直接丢给 `eval` 是一个典型的「代码注入」漏洞。

`ast.literal_eval` 则只接受 **Python 字面量**：数字、字符串、字节串、布尔、`None`、以及由它们组成的列表/元组/字典/集合，再允许一元正负号。一旦遇到变量名、函数调用、属性访问等「非字面量」结构，它立刻抛出 `ValueError`。

所以：

\[ \texttt{literal\_eval} \subsetneq \texttt{eval} \]

即 `literal_eval` 是 `eval` 的一个**严格子集**——它故意只保留「安全的字面量部分」，砍掉了所有可执行的「动作」。在本讲场景下，矩阵元素本来就只可能是数字或布尔，用 `literal_eval` 既够用又安全。

#### 4.3.2 核心流程

每个元素字符串进来后的命运：

```text
'1'        → literal_eval → 1        （int）
'3.14'     → literal_eval → 3.14     （float）
'True'     → literal_eval → True     （bool）
'1.'       → literal_eval → 1.0      （float）
'invalid'  → literal_eval → 抛 ValueError
```

值得注意的是 `literal_eval` 接受的类型比「数字」更宽：它接受任何合法字面量。这就是为什么 `np.matrix('True; False')` 能得到布尔矩阵——`True`/`False` 本身就是合法字面量。

#### 4.3.3 源码精读

`ast` 在文件顶部就已导入：

[numpy/matrixlib/defmatrix.py:3-3](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L3-L3) —— 导入标准库 `ast`，为后面的 `literal_eval` 做准备。

实际调用在解析循环里：

[numpy/matrixlib/defmatrix.py:26-27](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L26-L27) —— 第 26 行按空格切出单个元素，第 27 行对每个元素调用 `ast.literal_eval`，并用 `extend` 把求值结果追加到当前行。

写法 `map(ast.literal_eval, temp)` 很简洁：对 `temp` 里的每个字符串元素都套一次 `literal_eval`，得到一个迭代器，再 `extend` 进 `newrow`。

这行的报错行为也直接决定了 `matrix` 的对外错误类型。测试里专门有这条用例：

[numpy/matrixlib/tests/test_defmatrix.py:39-41](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_defmatrix.py#L39-L41) —— `test_exceptions` 断言 `matrix("invalid")` 抛出 `ValueError`。

这条断言之所以成立，正是因为 `ast.literal_eval("invalid")` 发现 `invalid` 是一个名字（Name）而非字面量，于是抛 `ValueError`。这个 `ValueError` 一路冒泡出 `_convert_from_string`、冒泡出 `__new__`，被测试捕获。

#### 4.3.4 代码实践

**实践目标**：体验 `ast.literal_eval`「接受什么、拒绝什么」，并验证 `matrix("invalid")` 确实抛 `ValueError`。

**操作步骤**：

1. 直接对若干字符串调用 `ast.literal_eval`，观察接受与拒绝。
2. 用 `np.matrix("invalid")` 复现报错。

**示例代码**（示例代码）：

```python
import ast
import warnings; warnings.simplefilter('ignore')
import numpy as np

for s in ['1', '3.14', 'True', 'None', '1.']:
    print(s, '->', ast.literal_eval(s))

for s in ['invalid', 'print(1)', '__import__("os")']:
    try:
        ast.literal_eval(s)
    except ValueError as e:
        print(f'{s!r} 被拒绝: {type(e).__name__}')

# 对外入口也复现同样的 ValueError
try:
    np.matrix('invalid')
except ValueError as e:
    print('matrix 报错类型:', type(e).__name__)
```

**需要观察的现象**：

- 前五个字符串都能被 `literal_eval` 正确求值。
- `invalid`、`print(1)`、`__import__("os")` 全部被拒绝（抛 `ValueError`，而不是真的去执行）。
- `np.matrix('invalid')` 抛的是 `ValueError`，与 `literal_eval` 的异常类型一致。

**预期结果**：安全字面量正常输出值；非字面量全部打印 `ValueError`；最后一行打印 `matrix 报错类型: ValueError`。其中 `print(1)` 和 `__import__("os")` **不会**被执行——这正是用 `literal_eval` 而非 `eval` 的意义。

**待本地验证**：不同 Python 版本下，`literal_eval` 拒绝非字面量时报错信息的具体措辞可能不同，但异常类型一定是 `ValueError`。

#### 4.3.5 小练习与答案

**练习 1**：`np.matrix('1. 2.; 3. 4.')`（注意每个数字后面有个点）得到的是什么类型元素的矩阵？

**参考答案**：浮点矩阵。`'1.'` 经 `ast.literal_eval` 解析成 Python `float` 的 `1.0`，于是最终矩阵的 dtype 是浮点型。这正是测试 `test_pow` 里 `matrix("1. 2.; 3. 4.")` 的用法。

**练习 2**：如果把第 27 行的 `ast.literal_eval` 换成 `eval`，`np.matrix('1 2; 3 4')` 还能正常工作吗？这样做有什么坏处？

**参考答案**：功能上仍然能工作（`eval('1')` 也得到 `1`）。坏处是丧失了安全性：一旦输入字符串来自不可信来源，`eval` 可能执行任意代码；此外 `eval` 还会受当前命名空间影响，行为更难预测。所以 numpy 选择更受限、更安全的 `ast.literal_eval`。

---

### 4.4 行列校验：保证「矩阵是方的」

#### 4.4.1 概念说明

一个合法的矩阵必须是「方」的——每一行的列数必须相同。`_convert_from_string` 用 `;` 自由分行、用 `,` 和空格自由分列，理论上完全可以拼出一个「参差不齐」的结构，比如：

```text
'1 2; 3 4 5'
   行0: 1 2      （2 列）
   行1: 3 4 5    （3 列）
```

这种「锯齿形」数据无法构成矩阵。所以解析器必须在产出之前做一次校验：以**第一行**的列数为基准，要求后续每一行列数都相等，否则立刻报错。

#### 4.4.2 核心流程

校验逻辑非常直白，可以用一个不变式（invariant）描述：

\[ \forall\, i \ge 1,\ \ \text{len}(\text{row}_i) = \text{Ncols},\quad \text{其中 } \text{Ncols} = \text{len}(\text{row}_0) \]

翻译成流程：

```text
处理第 0 行 → 记录 Ncols = 这行的元素个数
处理第 1 行 → 若 len(行) != Ncols → raise ValueError("Rows not the same size.")
处理第 2 行 → 同上
...
全部相等 → 把所有行收集进 newdata 返回
```

注意：基准列数来自**第一行**，所以错误信息永远是「这一行比第一行长/短了」。

#### 4.4.3 源码精读

校验就在主循环里，紧跟在每行元素求值之后：

[numpy/matrixlib/defmatrix.py:28-32](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L28-L32) —— 第一行（`count == 0`）记录列数 `Ncols`，其余各行与之比较，不等则抛 `ValueError("Rows not the same size.")`。

这段代码有几个值得品味的细节：

- 用的是 `enumerate(rows)` 提供的 `count`，所以无需单独维护一个「这是不是第一行」的布尔标志。
- `if count == 0 / elif` 的结构意味着：**只有非首行才会被校验**，第一行无条件成为基准。
- 校验在 `newdata.append(newrow)`（第 32 行）**之前**完成，所以一旦某行不合法，整张表都不会被返回，函数直接抛错退出。
- 报错信息是固定的字符串 `"Rows not the same size."`，这为我们在实践中断言提供了依据。

#### 4.4.4 代码实践

**实践目标**：直接复现这条报错，并确认它的异常类型与信息内容。

**操作步骤**：

1. 对 `np.matrix('1 2; 3 4 5')` 调用并捕获异常。
2. 打印异常类型与 `str(e)`。

**示例代码**（示例代码）：

```python
import warnings; warnings.simplefilter('ignore')
import numpy as np

try:
    np.matrix('1 2; 3 4 5')
except ValueError as e:
    print('类型:', type(e).__name__)
    print('信息:', str(e))
    assert str(e) == 'Rows not the same size.'
    print('断言通过 ✓')
```

**需要观察的现象**：捕获到 `ValueError`，且信息恰好是 `Rows not the same size.`。

**预期结果**：依次输出 `类型: ValueError`、`信息: Rows not the same size.`、`断言通过 ✓`。

**待本地验证**：异常文本属于 numpy 的公开行为，长期稳定；但若未来版本调整了措辞，断言可能需要相应更新。

#### 4.4.5 小练习与答案

**练习 1**：`np.matrix('1 2 3')` 只有一行，没有分号，会触发校验吗？

**参考答案**：不会触发校验分支，但能正常工作。因为没有 `;`，`split(';')` 只得到一行，循环只执行 `count == 0` 分支记录 `Ncols=3`，没有「后续行」可比较。结果是一个 1×3 矩阵 `[[1, 2, 3]]`。

**练习 2**：如果第一行是 `1 2 3`（3 列），第二行是 `4 5`（2 列），第三行是 `6 7 8`（3 列），函数在第几行就报错？

**参考答案**：在第二行（`count == 1`）就报错。校验在每行处理完后立即进行，第二行列数 `2 != 3`，立刻抛 `ValueError`，根本不会走到第三行。

---

## 5. 综合实践

本讲的综合实践是规格里指定的任务：**照源码手写一个最小版的 `_convert_from_string`**，要求正确处理 `1 2; 3 4` 与 `1,2;3,4`，并且对 `1 2; 3 4 5` 抛出和官方一致的错误信息。

### 5.1 实践目标

- 把本讲四个模块（方括号剥离、分隔符优先级、`ast.literal_eval`、行列校验）串成一个可运行的小函数。
- 用断言验证你的实现与官方 `np.matrix` 在三种输入下的行为一致（包括报错信息）。

### 5.2 操作步骤

1. 新建一个 `mini_matrix.py`，不依赖 numpy，只用标准库 `ast`。
2. 写一个 `mini_convert(data)` 函数，按本讲的四步流程实现。
3. 写一个 `mini_matrix_from_str(s)`，把 `mini_convert` 的结果传给 `np.array`（这一步对应官方 `__new__` 的下半段，可选）。
4. 跑下面的断言块，全部通过即算完成。

### 5.3 参考实现

下面是**示例代码**（你应当先自己写一版，再对照）：

```python
# mini_matrix.py  —— 示例代码，对应 numpy 的 _convert_from_string
import ast


def mini_convert(data: str):
    # 1) 剥离方括号
    for char in '[]':
        data = data.replace(char, '')

    # 2) 分号分行
    rows = data.split(';')
    newdata = []
    for count, row in enumerate(rows):
        # 3) 逗号分块、空格分元素、literal_eval 求值
        newrow = []
        for col in row.split(','):
            temp = col.split()
            newrow.extend(map(ast.literal_eval, temp))
        # 4) 行列校验：第一行定基准，其余必须相等
        if count == 0:
            ncols = len(newrow)
        elif len(newrow) != ncols:
            raise ValueError("Rows not the same size.")
        newdata.append(newrow)
    return newdata


if __name__ == '__main__':
    # 用例 1：空格分列
    assert mini_convert('1 2; 3 4') == [[1, 2], [3, 4]]
    # 用例 2：逗号分列
    assert mini_convert('1,2;3,4') == [[1, 2], [3, 4]]
    # 用例 3：混用
    assert mini_convert('1 2, 3 4; 5 6, 7 8') == [[1, 2, 3, 4], [5, 6, 7, 8]]
    # 用例 4：方括号应被剥离
    assert mini_convert('[[1, 2], [3, 4]]') == [[1, 2], [3, 4]]
    # 用例 5：布尔也能解析
    assert mini_convert('True; True; False') == [[True], [True], [False]]

    # 用例 6：参差行必须抛错，且信息与官方一致
    try:
        mini_convert('1 2; 3 4 5')
        raise AssertionError('应当抛 ValueError 却没有抛')
    except ValueError as e:
        assert str(e) == 'Rows not the same size.', f'信息不对: {e!r}'

    # 用例 7：非法 token 抛 ValueError（来自 literal_eval）
    try:
        mini_convert('invalid')
        raise AssertionError('应当抛 ValueError 却没有抛')
    except ValueError:
        pass

    print('全部断言通过 ✓')
```

### 5.4 需要观察的现象

- 前五个用例的返回值都是 Python 嵌套列表，且内容正确。
- 第六个用例抛出的 `ValueError` 信息**逐字符**等于官方的 `"Rows not the same size."`。
- 第七个用例确认非法 token 会抛 `ValueError`（这正是用 `literal_eval` 而非 `eval` 带来的行为）。

### 5.5 预期结果

运行 `python mini_matrix.py` 后输出 `全部断言通过 ✓`，无任何 `AssertionError`。

### 5.6 进阶（可选）

把你的 `mini_convert` 接上 numpy，对比与官方解析是否完全一致：

```python
import warnings; warnings.simplefilter('ignore')
import numpy as np

def mini_matrix_from_str(s):
    return np.array(mini_convert(s))

# 你的实现与官方 matrix 在数据上应当完全一致
assert np.array_equal(mini_matrix_from_str('1 2; 3 4'),
                      np.matrix('1 2; 3 4'))
```

如果这一步也通过，说明你已经完整复现了 `_convert_from_string` 的语义。

---

## 6. 本讲小结

- `_convert_from_string` 是模块级私有函数，作用是把字符串解析成 **Python 嵌套列表**（不是 ndarray/matrix），再交回 `__new__` 的通用分支统一处理。
- 字符串语法有三层分隔符，优先级为 `;`（分行）> `,`（分块）> 空格（分元素），且 `,` 与空格在「列」层等价、可混用；方括号 `[` `]` 一开始就被无条件剥离。
- 每个元素用 `ast.literal_eval` 求值，而不是 `eval`——前者只接受安全的 Python 字面量，从根本上杜绝代码注入；这也是 `matrix("invalid")` 抛 `ValueError` 的根因。
- 行列校验以第一行列数为基准（`Ncols`），任何后续行列数不等即在第 30-31 行抛 `ValueError("Rows not the same size.")`。
- 字符串分支与通用 array_like 分支在第 150-152 行汇合：`_convert_from_string` 把字符串「降级」成嵌套列表后，复用同一条 `np.array` 定形路径。

## 7. 下一步学习建议

本讲讲的是 `_convert_from_string`——**把字符串里的字面量翻译成数据**。下一讲 u2-l3 会讲另一个名字很像、但职责完全不同的函数 `_from_string`，它是 `bmat(...)` 的字符串解析器：当字符串里出现的是**变量名**（如 `np.bmat('A,B;B,A')`）而非数字时，`_from_string` 会在调用栈的局部/全局作用域里按名字查出对应的数组，再用 `concatenate` 拼成块矩阵。

建议你：

- 直接进入 u2-l3「bmat 块矩阵构造与 `_from_string` 名字解析」，重点对比 `_convert_from_string`（解析字面量）与 `_from_string`（解析变量名）的差别。
- 阅读源码时，留意 `bmat` 中 `sys._getframe().f_back` 如何回溯调用栈拿到作用域字典——这与本讲的「纯字符串解析」是两条不同的设计路线。
