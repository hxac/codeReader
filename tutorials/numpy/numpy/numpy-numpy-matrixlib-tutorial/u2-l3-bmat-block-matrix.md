# bmat 块矩阵构造与 _from_string 名字解析

## 1. 本讲目标

本讲聚焦 `numpy.matrixlib.defmatrix.py` 中的两个函数：`bmat` 与它私底下依赖的 `_from_string`。学完后你应该能够：

- 区分 `bmat` 的三种输入路径（字符串、嵌套序列、`ndarray`），并知道每种路径在源码里走哪条分支。
- 理解字符串路径中 `_from_string` 用 `;` 分行、`,` 分块、空格分元素的"三层分隔符"规则。
- 掌握 `ldict` / `gdict` 参数的作用，以及 `bmat` 默认如何用 `sys._getframe().f_back` 回溯调用栈去"偷取"调用者作用域里的变量。
- 看懂 `concatenate(..., axis=-1)` 与 `concatenate(..., axis=0)` 在块拼接中分别对应"水平拼接"与"垂直拼接"。

本讲承接 [u2-l2 字符串矩阵语法与 _convert_from_string](u2-l2-string-parsing-convert-from-string.md)。上一讲的 `_convert_from_string` 把字符串里的 token 当**字面量**求值（`ast.literal_eval`），本讲的 `_from_string` 则把 token 当**变量名**去作用域里查表——两者名字相近、分隔符结构相同，但职责完全不同，务必区分。

## 2. 前置知识

- **块矩阵（block matrix）**：把若干小矩阵像拼瓷砖一样拼成一个大矩阵。例如把 \(A,B,C,D\) 四块拼成
  \[
  \begin{bmatrix} A & B \\ C & D \end{bmatrix}
  \]
  其中 \(A\) 与 \(B\) 必须行数相同（横向并排），\(A\) 与 \(C\) 必须列数相同（纵向堆叠）。
- **`numpy.concatenate`**：沿指定轴把多个数组首尾相接。对二维数组而言，`axis=0` 是纵向（行方向）拼接，`axis=-1`（即 `axis=1`，最后一轴）是横向（列方向）拼接。
- **栈帧（stack frame）与作用域**：Python 每调用一次函数就在调用栈上压入一个"栈帧"，帧里挂着这次调用的局部变量（`f_locals`）和所在模块的全局变量（`f_globals`）。`sys._getframe()` 能拿到当前帧，`.f_back` 能回退到调用者的帧——这正是 `bmat` 能"看见"你写在调用处变量（如 `A`、`B`）的原理。
- **`matrix` 类**：`bmat` 的返回值始终是 `matrix`，它是强制二维的 `ndarray` 子类（见 [u2-l1](u2-l1-matrix-constructor-new.md)）。`bmat` 内部拼接出的是一个普通 `ndarray`，最后再用 `matrix(...)` 包一层。

## 3. 本讲源码地图

本讲只涉及一个源码文件，但其中两段是核心：

| 文件 | 关键符号 | 行号 | 作用 |
| --- | --- | --- | --- |
| `numpy/matrixlib/defmatrix.py` | `_from_string` | 1015–1037 | 把 `"A,B;C,D"` 这样的字符串解析成一棵"块矩阵的二维表"，并在作用域里查每个名字对应的数组。 |
| `numpy/matrixlib/defmatrix.py` | `bmat` | 1040–1117 | 对外入口，按 str / list-tuple / ndarray 三类输入分发，最终返回 `matrix`。 |
| `numpy/matrixlib/defmatrix.py` | `concatenate` 导入 | 8 | 从 `numpy._core.numeric` 引入拼接函数，是 `_from_string` 与 `bmat` 共用的"胶水"。 |
| `numpy/matrixlib/defmatrix.py` | `sys` 导入 | 4 | 为 `sys._getframe()` 引入标准库模块。 |
| `numpy/matrixlib/tests/test_defmatrix.py` | `TestCtor` | 16–60 | 覆盖 `bmat` 的三种输入与 `ldict`/`gdict` 覆盖行为，是本讲实践的"标准答案"。 |

辅助对照（非本讲重点，仅作对比）：`_convert_from_string`（16–33 行）服务于 `matrix.__new__` 的字符串分支，用 `ast.literal_eval` 求字面量；本讲的 `_from_string` 服务于 `bmat` 的字符串分支，查变量名。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**bmat 的三条输入路径**、**_from_string 的名字解析**、**作用域捕获 sys._getframe**、**concatenate 与轴方向**。

### 4.1 bmat 函数：三条输入路径的分发

#### 4.1.1 概念说明

`bmat`（build matrix）的目标是：给你若干**已经存在**的数组，把它们按"格子布局"拼成一个二维 `matrix`。它的输入 `obj` 可以是三种形态：

1. **字符串**：如 `'A,B;C,D'`，里面的 `A`/`B`/`C`/`D` 是变量名，`bmat` 会在调用者的作用域里找到它们对应的数组。
2. **嵌套序列**（`list` 或 `tuple`）：如 `[[A,B],[C,D]]`，外层是"行"，内层是每行里的"块"。
3. **`ndarray`**：直接当作已经拼好的二维数组，包成 `matrix` 返回。

`bmat` 的返回值统一是 `matrix` 类型。

#### 4.1.2 核心流程

```
bmat(obj, ldict=None, gdict=None):
    if obj 是 str:          # 路径 1：字符串
        取/构造作用域字典 glob_dict, loc_dict
        return matrix(_from_string(obj, glob_dict, loc_dict))

    if obj 是 list 或 tuple: # 路径 2：嵌套序列
        若 obj 是"扁平的一排数组"（首元素就是 ndarray）:
            return matrix(concatenate(obj, axis=-1))   # 一次性横向拼
        否则（[[A,B],[C,D]] 这种二维布局）:
            每一行内 concatenate(row, axis=-1)         # 横向拼
            再 concatenate(所有行, axis=0)              # 纵向拼
            return matrix(...)

    if obj 是 ndarray:      # 路径 3：已是数组
        return matrix(obj)
```

注意三个分支的判断顺序：先 `str`，再 `(tuple, list)`，最后 `ndarray`。因为 `bmat` 没有写兜底的 `else`，若 `obj` 既不是字符串、又不是序列/数组（比如传一个整数），函数会"滑过"所有分支，**隐式返回 `None`**——这是一个值得注意的边界行为。

#### 4.1.3 源码精读

先看 `bmat` 的函数签名与三个分发分支：[numpy/matrixlib/defmatrix.py:1040-1117](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L1040-L1117)（这是 `bmat` 的完整定义，含 docstring）。

签名 `def bmat(obj, ldict=None, gdict=None)` 中，`ldict`/`gdict` 只有在字符串路径下才可能被用到：

字符串分支：[numpy/matrixlib/defmatrix.py:1095-1105](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L1095-L1105)

```python
if isinstance(obj, str):
    if gdict is None:
        frame = sys._getframe().f_back
        glob_dict = frame.f_globals
        loc_dict = frame.f_locals
    else:
        glob_dict = gdict
        loc_dict = ldict
    return matrix(_from_string(obj, glob_dict, loc_dict))
```

这段做了两件事：决定从哪里取变量（栈帧 vs 显式字典），再把字符串交给 `_from_string`。栈帧机制留到 4.3 节细讲。

嵌套序列分支：[numpy/matrixlib/defmatrix.py:1107-1115](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L1107-L1115)

```python
if isinstance(obj, (tuple, list)):
    # [[A,B],[C,D]]
    arr_rows = []
    for row in obj:
        if isinstance(row, N.ndarray):  # not 2-d
            return matrix(concatenate(obj, axis=-1))
        else:
            arr_rows.append(concatenate(row, axis=-1))
    return matrix(concatenate(arr_rows, axis=0))
```

这里有一个容易忽略的细节：循环里只要发现"某一行本身就是 `ndarray`"（而不是一个装着数组的列表），就立刻 `return matrix(concatenate(obj, axis=-1))`——把整个 `obj` 当成"一排横向排列的块"一次性水平拼接。源码注释 `# not 2-d` 的意思是：此时 `obj` 并非"二维嵌套"结构。由于它在第一次命中时就 `return`，所以真正决定走哪条子路径的是**第一行的类型**：

- `bmat([[A,B],[C,D]])`：第一行 `[A,B]` 是 `list` → 走 `else`，逐行横拼再纵拼。
- `bmat([A, E])`：第一行 `A` 是 `ndarray` → 走 `if`，整体横拼。

最后是 ndarray 分支：[numpy/matrixlib/defmatrix.py:1116-1117](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L1116-L1117)

```python
if isinstance(obj, N.ndarray):
    return matrix(obj)
```

它不做任何拼接，只是委托给 `matrix.__new__`（见 [u2-l1](u2-l1-matrix-constructor-new.md)），把现成的二维数组包成 `matrix`。docstring 里给出的例子 `np.bmat(np.r_[np.c_[A, B], np.c_[C, D]])` 走的就是这条——先用 `r_`/`c_` 拼好，再交给 `bmat` 包装。

#### 4.1.4 代码实践

**实践目标**：验证三种输入路径产出同一个块矩阵。

**操作步骤**（把下面的脚本存为 `bmat_three_paths.py` 并运行；本讲未替你执行，请本地验证）：

```python
import warnings, numpy as np
warnings.simplefilter("ignore", PendingDeprecationWarning)  # 屏蔽 matrix 弃用警告

A = np.array([[1, 1], [1, 1]])
B = np.array([[2, 2], [2, 2]])
C = np.array([[3, 4], [5, 6]])
D = np.array([[7, 8], [9, 0]])

m_str = np.bmat('A,B; C,D')        # 路径 1：字符串
m_lst = np.bmat([[A, B], [C, D]])  # 路径 2：嵌套序列
m_arr = np.bmat(np.r_[np.c_[A, B], np.c_[C, D]])  # 路径 3：ndarray

print(m_str)
print(type(m_str))
assert np.array_equal(m_str, m_lst)
assert np.array_equal(m_str, m_arr)
```

**需要观察的现象**：三种构造方式打印出的数值布局一致，类型都是 `<class 'numpy.matrix>`。

**预期结果**：

```
[[1 1 2 2]
 [1 1 2 2]
 [3 4 7 8]
 [5 6 9 0]]
```

**待本地验证**：在不可运行本讲代码的环境里，以上输出为基于源码的推断，请以本地实际运行为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ndarray` 分支放在三个 `isinstance` 判断的最后一个，而不是第一个？

**答案**：因为判断有"子集"关系。字符串和序列都不是 `ndarray`，但顺序上要把更"特化"的判断（`str`、`(tuple,list)`）放前面，避免把一个本该走拼接逻辑的 `ndarray` 输入错误地直接包成 `matrix`。事实上即便调换顺序，对纯 `ndarray` 输入结果也一样；但把 `ndarray` 放最后是一种防御性写法——它作为"兜底"承接所有不是字符串/序列的数组输入。

**练习 2**：`np.bmat(np.array([[1,2],[3,4]]))` 返回什么？它做了拼接吗？

**答案**：返回 `matrix([[1,2],[3,4]])`，类型是 `matrix`。它没有做任何拼接，只是走了第三条分支 `return matrix(obj)`，把这个现成数组包成了 `matrix`。

---

### 4.2 _from_string：字符串中的变量名解析

#### 4.2.1 概念说明

当 `bmat` 收到一个字符串，比如 `'A,B;C,D'`，它的任务不是去解析数字，而是把 `A`/`B`/`C`/`D` 这些 token 当作**变量名**，到调用者的作用域里找出它们各自绑定的数组，再按字符串描述的布局拼起来。这件事由模块级私有函数 `_from_string` 完成。

关键对比（务必与 [u2-l2](u2-l2-string-parsing-convert-from-string.md) 区分）：

| 函数 | 服务对象 | token 的含义 | 求值方式 |
| --- | --- | --- | --- |
| `_convert_from_string` | `matrix.__new__` | 字面量（数字） | `ast.literal_eval` |
| `_from_string` | `bmat` | 变量名 | 字典查找 `ldict`/`gdict` |

两者的分隔符结构完全一样（`;` 分行、`,` 分块、空格分元素），但 `_from_string` **不调用** `ast.literal_eval`，所以 `'1 2; 3 4'` 里的 `1`/`2` 会被当成变量名去查找（找不到就报 `NameError`），而不是当成数字。

#### 4.2.2 核心流程

`_from_string` 的输入是一个描述布局的字符串、一个全局字典、一个局部字典；输出是一个**已拼好的 `ndarray`**（注意不是 `matrix`，包装成 `matrix` 是 `bmat` 干的）。流程：

```
_from_string(str, gdict, ldict):
    rows = str.split(';')               # 1. ';' 分行
    for row in rows:
        tokens = row 用 ',' 切片后再对每片 split()   # 2. ',' 分块、空格分元素
        for token in tokens:
            thismat = ldict[token] 失败则 gdict[token]  # 3. 查名字
            失败 → raise NameError
        rowtup.append(concatenate(本行所有块, axis=-1))  # 4. 行内横拼
    return concatenate(所有行, axis=0)              # 5. 行间纵拼
```

三层分隔符的优先级：`;` > `,` > 空格。其中 `,` 与空格在"同一行内"是**等价**的分隔手段，可以混用：`'A B;C D'` 与 `'A,B;C,D'` 解析出的二维布局完全相同。

#### 4.2.3 源码精读

完整函数：[numpy/matrixlib/defmatrix.py:1015-1037](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L1015-L1037)

```python
def _from_string(str, gdict, ldict):
    rows = str.split(';')
    rowtup = []
    for row in rows:
        trow = row.split(',')
        newrow = []
        for x in trow:
            newrow.extend(x.split())
        trow = newrow
        coltup = []
        for col in trow:
            col = col.strip()
            try:
                thismat = ldict[col]
            except KeyError:
                try:
                    thismat = gdict[col]
                except KeyError as e:
                    raise NameError(f"name {col!r} is not defined") from None

            coltup.append(thismat)
        rowtup.append(concatenate(coltup, axis=-1))
    return concatenate(rowtup, axis=0)
```

逐段说明：

- **参数名遮蔽**：第一个形参叫 `str`，遮蔽了内置 `str`。函数内 `str.split(';')` 调用的是这个参数（即传入的字符串）的 `split` 方法。能跑，但不是好风格——阅读时别被它迷惑。`bmat` 调用时传的是 `_from_string(obj, glob_dict, loc_dict)`，所以 `str` 实参就是用户给的布局字符串。
- **三层切分**：先 `split(';')` 得到若干"行字符串"；每行再 `split(',')` 得到若干"块字符串"；每个块再 `split()`（按任意空白）得到若干 token。所以 `'A,B;C D'` → 行 `['A,B', 'C D']` → token `[['A','B'], ['C','D']]`。
- **名字查找的优先级**：[numpy/matrixlib/defmatrix.py:1027-1033](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L1027-L1033) 先查 `ldict`（局部），再查 `gdict`（全局），都找不到就抛 `NameError("name 'X' is not defined")`。`from None` 显式掐断了异常链，让你只看到干净的 `NameError`。这与 Python 普通名字解析"局部→全局"的顺序一致。
- **两次拼接**：[numpy/matrixlib/defmatrix.py:1036-1037](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L1036-L1037) 行内 `concatenate(coltup, axis=-1)`（横拼），最后 `concatenate(rowtup, axis=0)`（纵拼）。`concatenate` 在文件顶部导入：[numpy/matrixlib/defmatrix.py:8](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L8)。

#### 4.2.4 代码实践

**实践目标**：手写一个最小版 `_from_string`，验证三层分隔符与名字查找。

**操作步骤**：

```python
import numpy as np

def my_from_string(s, gdict, ldict):
    rows = s.split(';')
    rowtup = []
    for row in rows:
        tokens = []
        for x in row.split(','):
            tokens.extend(x.split())
        coltup = [ldict.get(t, gdict[t]) for t in tokens]   # 查名字
        rowtup.append(np.concatenate(coltup, axis=-1))      # 行内横拼
    return np.concatenate(rowtup, axis=0)                   # 行间纵拼

A = np.array([[1, 1], [1, 1]])
B = np.array([[2, 2], [2, 2]])
scope = {'A': A, 'B': B}

print(my_from_string('A,B;B,A', {}, scope))
print(my_from_string('A B;B A', {}, scope))   # 空格与逗号等价
```

**需要观察的现象**：两行输出完全相同，说明 `,` 与空格在行内等价；最终结果是 `A B / B A` 的块布局。

**预期结果**：

```
[[1 1 2 2]
 [1 1 2 2]
 [2 2 1 1]
 [2 2 1 1]]
```

**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`np.bmat('1 2; 3 4')` 会得到 `matrix([[1,2],[3,4]])` 吗？

**答案**：不会。`bmat` 的字符串路径把 `1`/`2`/`3`/`4` 当**变量名**去作用域查找，而你的作用域里通常没有叫 `1` 的变量（且标识符不能以数字开头），所以会抛 `NameError("name '1' is not defined")`。要构造字面量矩阵应使用 `np.matrix('1 2; 3 4')`（走 `_convert_from_string`，见 [u2-l2](u2-l2-string-parsing-convert-from-string.md)）。

**练习 2**：把名字故意拼错，例如作用域里只有 `A` 却写 `np.bmat('AA')`，会报什么错、在哪一行抛出？

**答案**：抛 `NameError("name 'AA' is not defined")`，由 [numpy/matrixlib/defmatrix.py:1033](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L1033) 的 `raise NameError(...)` 抛出。因为 `ldict` 和 `gdict` 都查不到 `'AA'`，两个 `KeyError` 都命中。

---

### 4.3 作用域捕获：sys._getframe 与 ldict/gdict

#### 4.3.1 概念说明

`bmat('A,B')` 之所以能"自动"找到你在调用处定义的变量 `A`、`B`，是因为它默认会去**调用 `bmat` 的那一层栈帧**里取局部变量和全局变量。这是一种"反射式"的行为，靠标准库 `sys._getframe()` 实现。

你也可以用两个参数**显式**提供作用域，而不依赖栈帧：

- `ldict`：局部作用域字典（替代 `frame.f_locals`）。
- `gdict`：全局作用域字典（替代 `frame.f_globals`）。

但这里有一个反直觉的开关逻辑（docstring 也写明了）：`ldict` 和 `gdict` **只在字符串路径、且 `gdict is not None` 时**才被采用；只要 `gdict is None`，`bmat` 就无视 `ldict`，转而用栈帧。

#### 4.3.2 核心流程

```
if gdict is None:
    frame = sys._getframe().f_back     # 回退到调用 bmat 的那一帧
    glob_dict = frame.f_globals         # 调用者的全局变量
    loc_dict = frame.f_locals           # 调用者的局部变量
else:
    glob_dict = gdict                   # 用显式提供的字典
    loc_dict = ldict
```

一个关键的"坑"：如果调用者传了 `gdict=某字典` 却**没传** `ldict`，那么 `ldict` 保持默认值 `None`，于是 `_from_string` 内部执行 `thismat = ldict[col]` 时就是对 `None` 做下标——抛 `TypeError: 'NoneType' object is not subscriptable`。所以"只要给了 `gdict`，就必须同时给 `ldict`（哪怕是空字典）"。

#### 4.3.3 源码精读

栈帧捕获发生在字符串分支内部：[numpy/matrixlib/defmatrix.py:1096-1103](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L1096-L1103)

```python
if gdict is None:
    # get previous frame
    frame = sys._getframe().f_back
    glob_dict = frame.f_globals
    loc_dict = frame.f_locals
else:
    glob_dict = gdict
    loc_dict = ldict
```

逐点说明：

- `sys` 在文件顶部导入：[numpy/matrixlib/defmatrix.py:4](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L4) `import sys`。
- `sys._getframe()` 返回 `bmat` 自己的栈帧；`.f_back` 回退一层，得到**调用 `bmat` 的那一帧**（注释 `# get previous frame` 正是这个意思）。所以你在交互式解释器或函数里写的局部变量 `A`、`B`，都来自这一帧的 `f_locals`。
- 一旦 `gdict` 非 `None`，`ldict`/`gdict` 原样赋给 `loc_dict`/`glob_dict`，栈帧被完全绕开。

官方测试明确覆盖了这条"坑"：[numpy/matrixlib/tests/test_defmatrix.py:43-60](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_defmatrix.py#L43-L60)（即 `test_bmat_nondefault_str`）。其中：

```python
assert_(np.all(bmat("A,A;A,A", ldict={'A': B}) == Aresult))           # 只给 ldict → gdict 仍 None → 走栈帧，ldict 被忽略！
assert_raises(TypeError, bmat, "A,A;A,A", gdict={'A': B})              # 只给 gdict → ldict=None → TypeError
assert_(
    np.all(bmat("A,A;A,A", ldict={'A': A}, gdict={'A': B}) == Aresult))  # 两个都给 → 生效
```

第一行尤其值得玩味：只传 `ldict` 时 `gdict` 仍是 `None`，于是 `bmat` **忽略你给的 `ldict`**，回头去栈帧里找 `A`——这正是 docstring 里 `ldict ... Ignored if ... gdict is None` 的含义。

#### 4.3.4 代码实践

**实践目标**：用 `ldict` 覆盖名字解析，并复现"只给 `gdict` 抛 `TypeError`"的行为。

**操作步骤**：

```python
import warnings, numpy as np
warnings.simplefilter("ignore", PendingDeprecationWarning)

A = np.array([[1, 2], [3, 4]])
A_alt = np.array([[9, 9], [9, 9]])

# 1) 用 ldict 把 'A' 解析成另一个数组（注意：只给 ldict 时 gdict=None，ldict 会被忽略，走栈帧！）
#    要让 ldict 真正生效，必须同时给 gdict：
r1 = np.bmat('A', ldict={'A': A_alt}, gdict={})
print("ldict 覆盖生效：\n", r1)
assert np.array_equal(r1, A_alt)   # 覆盖成功

# 2) 只给 gdict、不给 ldict → 复现 TypeError
try:
    np.bmat('A', gdict={'A': A_alt})
except TypeError as e:
    print("捕获到预期异常：", e)
```

**需要观察的现象**：第 1 步打印出的是 `A_alt`（全 9），说明 `ldict` 在配了 `gdict={}` 后生效；第 2 步抛出 `TypeError: 'NoneType' object is not subscriptable`。

**预期结果**：`r1` 等于 `[[9,9],[9,9]]`；第 2 步捕获到 `TypeError`。

**待本地验证**。

> 提示：本实践的"标准答案"对应测试文件 [numpy/matrixlib/tests/test_defmatrix.py:54-58](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_defmatrix.py#L54-L58)，可直接对照阅读。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `bmat` 要用 `sys._getframe().f_back` 而不是 `sys._getframe()`？

**答案**：`sys._getframe()`（无参数）返回的是**当前**帧，也就是 `bmat` 自己的栈帧——那里只有 `obj`/`ldict`/`gdict` 这些 `bmat` 的局部变量，并没有用户写的 `A`/`B`。`.f_back` 回退到调用 `bmat` 的那一帧，才能拿到用户作用域里的变量。

**练习 2**：在模块顶层（全局作用域）调用 `np.bmat('A,B;B,A')`，变量 `A`/`B` 是从 `f_locals` 还是 `f_globals` 找到的？

**答案**：在模块顶层，`f_locals` 和 `f_globals` 实际上指向**同一个字典**（模块的全局命名空间），所以从哪个找到都一样。`_from_string` 先查 `loc_dict`（即 `f_locals`）就能命中。在函数内部调用时两者才不同，此时局部变量在 `f_locals`，模块级变量在 `f_globals`。

---

### 4.4 concatenate 与块拼接的轴方向

#### 4.4.1 概念说明

无论是字符串路径里的 `_from_string`，还是序列路径里的 `bmat`，最终的物理拼接都委托给 `numpy.concatenate`。理解 `bmat` 的关键，就是搞清楚两次拼接各自沿哪个轴：

- **行内拼接（横向，并排）**：`concatenate(coltup, axis=-1)`。对二维数组，`axis=-1` 就是最后一轴（列方向），把同一行的几块左右并排。要求各块**行数相同**。
- **行间拼接（纵向，堆叠）**：`concatenate(rowtup, axis=0)`。沿第 0 轴（行方向）把几行上下堆叠。要求各（已横拼的）行**列数相同**。

设四块形状为 \(A:(m,n),\ B:(m,p),\ C:(q,n),\ D:(q,p)\)，则拼出的块矩阵形状为：

\[
\text{shape} = (m+q,\ n+p)
\]

#### 4.4.2 核心流程

字符串路径（`_from_string`）的两次拼接：

```
对每一行：concatenate(本行各块, axis=-1)   → 得到一个横拼的"宽行"
全部行：  concatenate(各行, axis=0)         → 上下堆叠成最终数组
```

序列路径（`bmat`）的两次拼接结构完全对称，区别只在"扁平一排数组"这一特殊情况下会退化为只做一次横向拼接：

- `bmat([[A,B],[C,D]])`：逐行 `concatenate([A,B], axis=-1)`、`concatenate([C,D], axis=-1)`，再 `concatenate([row0,row1], axis=0)`。
- `bmat([A, E])`（扁平）：直接 `concatenate([A,E], axis=-1)`，只横拼一次。

#### 4.4.3 源码精读

`concatenate` 的导入：[numpy/matrixlib/defmatrix.py:8](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L8)

```python
from numpy._core.numeric import concatenate, isscalar
```

`_from_string` 里的两次拼接：[numpy/matrixlib/defmatrix.py:1036-1037](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L1036-L1037)

```python
rowtup.append(concatenate(coltup, axis=-1))   # 行内横拼
return concatenate(rowtup, axis=0)            # 行间纵拼
```

`bmat` 嵌套序列分支里的对应拼接：[numpy/matrixlib/defmatrix.py:1112-1115](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L1112-L1115)

```python
if isinstance(row, N.ndarray):  # not 2-d
    return matrix(concatenate(obj, axis=-1))   # 扁平情况：只横拼
else:
    arr_rows.append(concatenate(row, axis=-1)) # 每行横拼
return matrix(concatenate(arr_rows, axis=0))   # 行间纵拼
```

注意 `axis=-1` 对二维数组等价于 `axis=1`，但用 `-1` 更通用——即便将来传入更高维的块（虽然 `matrix` 最终会强制二维），也能沿"最后一条轴"并排。

#### 4.4.4 代码实践

**实践目标**：亲手触发尺寸不匹配的拼接错误，定位是"行内"还是"行间"出了问题。

**操作步骤**：

```python
import warnings, numpy as np
warnings.simplefilter("ignore", PendingDeprecationWarning)

A = np.array([[1, 1], [1, 1]])      # (2,2)
B = np.array([[2, 2, 2], [2, 2, 2]])  # (2,3) —— 行数与 A 相同，可横拼
C = np.array([[3], [3]])            # (2,1) —— 行数相同但与横拼结果列数不同

# 行内横拼：A(2,2) 与 B(2,3) 行数相同 → OK
ok = np.bmat([[A, B]])
print("行内横拼 OK：\n", ok)        # 形状 (2,5)

# 行间纵拼：第一行 [A,B] 拼成 (2,5)，第二行 [C] 拼成 (2,1)，列数不同 → 报错
try:
    np.bmat([[A, B], [C]])
except ValueError as e:
    print("行间纵拼失败：", e)
```

**需要观察的现象**：第一次 `bmat([[A,B]])` 成功，形状为 `(2, 5)`；第二次因第二行 `[C]` 拼出的列数（1）与第一行列数（5）不一致，`concatenate(..., axis=0)` 抛 `ValueError`。

**预期结果**：第一段打印 `(2,5)` 的矩阵；第二段捕获到 `ValueError`，信息形如 `all the input array dimensions for the concatenation axis must match exactly`。

**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`bmat([[A,B],[C,D]])` 中，是"同行的块"需要列数相同，还是"同列的块"需要列数相同？

**答案**：都不是字面意义上的"列数相同"。正确约束是：**同一行内**的块（如 `A`、`B`）必须**行数相同**（才能 `axis=-1` 横拼）；**不同行之间**（横拼后的各行）必须**列数相同**（才能 `axis=0` 纵拼）。换句话说，横拼看行数、纵拼看列数。

**练习 2**：把 `axis=-1` 全部改成 `axis=1`，对二维输入结果会变吗？

**答案**：不会变。对二维数组，最后一轴就是第 1 轴，`axis=-1` 与 `axis=1` 完全等价。源码用 `-1` 只是为了表达"沿块的最后一条轴并排"这一意图，更具弹性。

---

## 5. 综合实践

把本讲四个模块串起来：用三种方式构造同一个块矩阵，再练习作用域覆盖与错误定位。

```python
import warnings, numpy as np
warnings.simplefilter("ignore", PendingDeprecationWarning)

A = np.array([[1, 2], [3, 4]])
B = np.array([[5, 6], [7, 8]])

# 任务 1：三种输入路径结果一致
m1 = np.bmat('A,B;B,A')
m2 = np.bmat([[A, B], [B, A]])
m3 = np.bmat(np.r_[np.c_[A, B], np.c_[B, A]])
assert np.array_equal(m1, m2) and np.array_equal(m1, m3)
print("三种路径一致：\n", m1)

# 任务 2：用 ldict 覆盖名字 'A'（必须同时给 gdict 才生效）
A2 = np.zeros((2, 2), dtype=int)
m4 = np.bmat('A,B;B,A', ldict={'A': A2, 'B': B}, gdict={})
print("覆盖 A 后：\n", m4)
# 左上、右下两块应变成全 0

# 任务 3：复现"只给 gdict 抛 TypeError"
try:
    np.bmat('A', gdict={'A': A2})
    print("未抛异常（异常！）")
except TypeError:
    print("如预期抛出 TypeError：给 gdict 必须同时给 ldict")
```

完成后再做一件事：把 `m1` 的结果画成块布局草图，标出 `A`/`B` 各自落在哪几个角，对照 [numpy/matrixlib/defmatrix.py:1036-1037](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L1036-L1037) 的两次 `concatenate` 解释"为什么是这个布局"。

## 6. 本讲小结

- `bmat` 按 `obj` 的类型走三条路径：字符串交给 `_from_string`、嵌套序列走两次 `concatenate`、`ndarray` 直接包装成 `matrix`；返回值统一是 `matrix`。
- `_from_string` 用三层分隔符 `;`（分行）> `,`（分块）> 空格（分元素）解析布局，把每个 token 当**变量名**在 `ldict`/`gdict` 里查找——这与 `matrix.__new__` 用的 `_convert_from_string`（把 token 当**字面量**用 `ast.literal_eval` 求值）职责不同，切勿混淆。
- 默认情况下 `bmat` 用 `sys._getframe().f_back` 回溯到调用者栈帧，从 `f_locals`/`f_globals` 取变量；只有当 `gdict is not None` 时才改用显式的 `ldict`/`gdict`。
- 一个易错点：只传 `gdict` 不传 `ldict` 会让 `ldict=None`，在 `_from_string` 里对 `None` 下标而抛 `TypeError`；只传 `ldict` 则因 `gdict is None` 被**忽略**，回头走栈帧。
- 物理拼接统一由 `numpy.concatenate` 完成：行内 `axis=-1`（横拼，要求行数相同），行间 `axis=0`（纵拼，要求列数相同）。
- 官方测试 `TestCtor.test_basic` 与 `test_bmat_nondefault_str`（`tests/test_defmatrix.py`）覆盖了三种输入路径与 `ldict`/`gdict` 覆盖行为，是本讲所有断言的"标准答案"。

## 7. 下一步学习建议

- 下一讲 [u2-l4 ndarray 子类化机制 __array_finalize__ 与 __array_priority__](u2-l4-ndarray-subclass-array-finalize.md) 会解释 `bmat` 末尾那个 `matrix(...)` 包装在子类层面到底做了什么——`concatenate` 产出的本是普通 `ndarray`，为何包一层 `matrix(...)` 就能"继承"二维约束。
- 若想看 `bmat` 之外更现代的块拼接方案，可阅读 NumPy 顶层 `np.block`（`bmat` docstring 的 `See Also` 就指向它）——它对 N 维数组通用、返回普通 `ndarray`、且不依赖变量名字符串，是官方推荐的替代品。
- 想巩固"作用域与栈帧"的直觉，可回头对照本讲 `sys._getframe().f_back`，结合 Python 标准库文档理解 `f_locals`/`f_globals`/`f_back` 三者的关系。
