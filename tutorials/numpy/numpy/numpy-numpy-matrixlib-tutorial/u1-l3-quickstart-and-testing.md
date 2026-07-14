# 快速上手 matrix/asmatrix/bmat 与运行测试

## 1. 本讲目标

学完本讲后，你应当能够：

- 用三种输入（ndarray、字符串、嵌套列表）快速创建一个 `np.matrix`；
- 说清楚 `matrix` 默认 **会复制数据**、而 `asmatrix` **不复制**（共享内存视图）这一关键区别，并能在代码里验证它；
- 用 `np.bmat` 把若干小矩阵拼成块矩阵，区分它的字符串、嵌套序列、ndarray 三条输入路径；
- 知道 `np.matrixlib.test()` 这个一行式测试入口是怎么装配出来的，并能真正运行 matrixlib 子包的测试。

本讲是入门层的最后一讲，承接 [u1-l1](u1-l1-project-overview.md)（认识 `np.matrix` 的定位与弃用警告）和 [u1-l2](u1-l2-package-structure-exports.md)（包结构与导出关系），把「认识」落实到「动手用」和「跑测试」上。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：`matrix` 是「永远是二维」的数组。**
普通 `ndarray` 可以是 0 维（标量）、1 维（向量）、2 维（矩阵）、甚至 N 维。而 `matrix` 强制把形状约束在二维：标量被补成 `(1,1)`，一维向量被补成 `(1,N)`，超过二维直接报错。这是 `matrix` 与 `ndarray` 最直观的差异。

**直觉二：「复制」还是「不复制」决定了改一处会不会影响另一处。**
当你把一个 `ndarray` 包装成 `matrix` 时，如果 **共享同一段内存**（视图），那么修改原数组，`matrix` 也会跟着变；如果 **复制了一份新数据**，两者就互不影响。`matrix(...)` 默认复制，`asmatrix(...)` 默认不复制——这是本讲最实用的一个区别。

**直觉三：测试入口其实就是一个普通函数。**
很多 numpy 子包都自带一个 `test()`，调用它就能跑该子包的全部测试。它的背后是 pytest，但被封装成了一个一行就能调用的函数。理解它能让你随时验证「我装的 numpy 是不是好的」以及「我改的东西有没有破坏既有行为」。

> 名词速查：
> - **视图（view）**：共享同一块内存数据的另一个数组对象，改一个另一个也变。
> - **`copy`**：另开一块内存，把数据原样抄一份，两边互不影响。
> - **块矩阵（block matrix）**：把若干小矩阵像拼瓷砖一样拼成的大矩阵。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [numpy/matrixlib/defmatrix.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py) | matrixlib 的全部业务代码：`matrix` 类、`asmatrix`、`bmat` 都在这里。 |
| [numpy/matrixlib/\_\_init\_\_.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/__init__.py) | 子包门面：转发导出，并装配 `test()` 入口。 |
| [numpy/\_pytesttester.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_pytesttester.py) | `PytestTester` 类的实现，`test()` 真正干活的代码在这里。 |
| [numpy/matrixlib/tests/test\_defmatrix.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_defmatrix.py) | matrixlib 的主测试文件，本讲用它来验证行为并作为实践依据。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：`matrix.__new__`（构造与二维强制）、`asmatrix`（视图语义）、`bmat`（块矩阵）、`PytestTester.test`（跑测试）。

### 4.1 matrix 的构造与二维强制：matrix.\_\_new\_\_

#### 4.1.1 概念说明

`matrix` 是 `ndarray` 的子类，但它有一个硬约束：**形状永远是二维**。构造一个 `matrix` 时，它可以接受三类输入：

1. 已经是 `matrix` 的对象；
2. 一个 `ndarray`；
3. 一个字符串（如 `'1 2; 3 4'`）或任意可被转成数组的对象（如嵌套列表 `[[1,2],[3,4]]`）。

`copy` 参数控制数据是否被复制。这部分逻辑全部集中在 `matrix.__new__` 里——注意是 `__new__` 而不是 `__init__`，因为 `ndarray` 这类内置类型的构造发生在 `__new__` 阶段（这是子类化 `ndarray` 的固定写法，下一阶段讲义会深入）。

#### 4.1.2 核心流程

`matrix.__new__(cls, data, dtype=None, copy=True)` 的执行流程可以用下面这段伪代码概括：

```
无条件发出 PendingDeprecationWarning          # 提醒：不建议再用 matrix
if data 是 matrix:
    若 dtype 一致且 copy=False: 原样返回 data   # 不复制
    否则: 返回 data.astype(dtype)              # 可能复制
elif data 是 ndarray:
    new = data.view(matrix)                    # 先做成 matrix 视图
    若 dtype 不同: 返回 new.astype(dtype)
    若 copy=True:  返回 new.copy()             # 复制一份
    否则:         返回 new                     # 不复制，共享内存
elif data 是 str:
    data = _convert_from_string(data)          # 字符串解析成嵌套列表

arr = numpy.array(data, dtype=dtype, copy=copy)
if arr.ndim > 2:  抛 ValueError("matrix must be 2-dimensional")
elif arr.ndim == 0: shape = (1, 1)             # 标量补二维
elif arr.ndim == 1: shape = (1, N)             # 一维补二维
用 numpy.ndarray.__new__(cls, shape, ...) 以 buffer 方式构造最终 matrix
```

最关键的两点是：**copy 标志在不同输入类型下含义不同**；以及**末尾把 0 维 / 1 维补成二维、把 >2 维直接拒绝**。

#### 4.1.3 源码精读

构造函数的整体位置在 [defmatrix.py:L119-L170](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L119-L170)，这段代码定义了 `matrix.__new__`，是本模块的核心。

第一步是无条件发出弃用警告（在 [u1-l1](u1-l1-project-overview.md) 已详述）：

[defmatrix.py:L119-L125](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L119-L125) —— 每次构造 `matrix` 都会发出 `PendingDeprecationWarning`。

接着是 `matrix` 输入分支：

```python
if isinstance(data, matrix):
    dtype2 = data.dtype
    if (dtype is None):
        dtype = dtype2
    if (dtype2 == dtype) and (not copy):
        return data
    return data.astype(dtype)
```

[defmatrix.py:L126-L132](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L126-L132) —— 当输入已经是 `matrix` 且 dtype 一致、`copy=False` 时，直接 `return data`，连视图都不新建；否则用 `astype` 转换（`astype` 总是返回新数据）。

然后是 `ndarray` 输入分支，这是理解 `copy` 行为的关键：

[defmatrix.py:L134-L145](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L134-L145) —— 先 `data.view(cls)` 做成 `matrix` 视图（这一步不复制数据）；如果 dtype 不同就 `astype`；否则根据 `copy` 决定是 `new.copy()`（复制）还是直接 `return new`（共享内存）。注意 `copy=True` 是默认值，所以 `np.matrix(arr)` 默认会复制。

字符串分支只有一行，把解析工作委托给 `_convert_from_string`（字符串语法的细节留到 [u2-l2] 讲义）：

[defmatrix.py:L147-L148](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L147-L148) —— 若 `data` 是字符串，先解析成嵌套列表，再走下面的通用数组转换路径。

最后是二维强制逻辑，这是 `matrix`「永远二维」的保证：

[defmatrix.py:L153-L160](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L153-L160) —— `ndim > 2` 抛 `ValueError("matrix must be 2-dimensional")`；`ndim == 0` 把形状改成 `(1,1)`；`ndim == 1` 把形状改成 `(1, N)`。

#### 4.1.4 代码实践

**实践目标**：亲手验证三种构造方式与二维强制规则。

把下面这段脚本存为 `try_ctor.py` 并运行（`warnings.simplefilter("ignore")` 用来屏蔽本讲不关心的弃用警告）：

```python
import warnings
warnings.simplefilter("ignore")  # 屏蔽 PendingDeprecationWarning，便于观察输出
import numpy as np

# 方式 1：字符串
a = np.matrix('1 2; 3 4')
# 方式 2：嵌套列表
b = np.matrix([[1, 2], [3, 4]])
# 方式 3：ndarray
c = np.matrix(np.array([[1, 2], [3, 4]]))

assert a.shape == b.shape == c.shape == (2, 2)
assert np.array_equal(a, b) and np.array_equal(b, c)

# 二维强制规则
assert np.matrix([[1, 2, 3]]).shape == (1, 3)      # 二维原样
assert np.matrix([1, 2, 3]).shape == (1, 3)        # 一维补成 (1, N)
assert np.matrix(5).shape == (1, 1)                # 标量补成 (1, 1)

# 超过二维必须报错
try:
    np.matrix(np.arange(8).reshape(2, 2, 2))
    raise AssertionError("应当抛出 ValueError")
except ValueError as e:
    assert "matrix must be 2-dimensional" in str(e)

print("4.1 全部断言通过")
```

**需要观察的现象**：前三个 `matrix` 形状都是 `(2,2)`；一维输入 `[1,2,3]` 变成 `(1,3)`；标量 `5` 变成 `(1,1)`；三维输入抛 `ValueError`。

**预期结果**：打印 `4.1 全部断言通过`。

#### 4.1.5 小练习与答案

**练习 1**：`np.matrix([[1],[2],[3]])` 的形状是什么？它是几行几列的「矩阵」？

> **答案**：`(3, 1)`，即 3 行 1 列的列向量——`matrix` 始终用二维表达，列向量是 `(N,1)` 而非一维数组。

**练习 2**：如果把 `__new__` 里 `ndim > 2` 的 `raise` 那行删掉，`np.matrix(np.arange(8).reshape(2,2,2))` 会发生什么？

> **答案**：不会再立即报错，而是带着 3 维形状继续往下走，最终构造出一个违反「永远二维」不变量的 `matrix`，后续很多方法（转置、`*` 矩阵乘等）都会行为异常。这正是这条校验存在的意义。

---

### 4.2 asmatrix 的视图语义（不复制）

#### 4.2.1 概念说明

`asmatrix(data, dtype=None)` 的官方定义就一句话：**等价于 `matrix(data, copy=False)`**。它的价值在于：当你手里已经有一个 `ndarray`，只是想临时以「矩阵」的身份去用它（比如用 `*` 做矩阵乘），又不想浪费内存去复制一份数据，就用 `asmatrix`。因为不复制，所以它返回的 `matrix` 和原 `ndarray` **共享同一段内存**，改一个另一个也变。

#### 4.2.2 核心流程

`asmatrix` 自身只有一个 `return` 语句，真正的「不复制」行为来自 `matrix.__new__` 的 `ndarray` 分支：

```
asmatrix(data):
    return matrix(data, copy=False)
        └─> __new__ 中 isinstance(data, ndarray) 分支
              └─> new = data.view(matrix); copy=False => return new（视图）
```

也就是说：`asmatrix` 的「不复制」= `__new__` 里 `copy=False` 时走 `return new`（[defmatrix.py:L144-L145](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L144-L145)）。

#### 4.2.3 源码精读

`asmatrix` 的定义与文档：

[defmatrix.py:L36-L70](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L36-L70) —— 函数体只有最后一行：`return matrix(data, dtype=dtype, copy=False)`。

文档里直接给出了「视图」的可运行示例，这是最权威的行为说明：

[defmatrix.py:L57-L67](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L57-L67) —— 把 `x[0,0]` 改成 5 后，`asmatrix(x)` 的结果 `m` 也变成了 5，证明两者共享内存。

`__new__` 里真正实现「不复制」的那两行：

[defmatrix.py:L142-L145](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L142-L145) —— `copy=True` 时 `return new.copy()`（复制），`copy=False` 时 `return new`（视图，共享内存）。

测试文件里也专门有一条用例固化了这一行为：

[test_defmatrix.py:L177-L181](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_defmatrix.py#L177-L181) —— `test_asmatrix`：`A[0,0] = -10` 后断言 `A[0,0] == mA[0,0]`，即 `asmatrix` 与原数组同步变化。

#### 4.2.4 代码实践

**实践目标**：对比 `asmatrix`（视图）与 `matrix`（复制）在修改原数组时的差异。

```python
import warnings
warnings.simplefilter("ignore")
import numpy as np

# asmatrix：不复制，共享内存
arr1 = np.arange(12).reshape(3, 4)
m_view = np.asmatrix(arr1)
arr1[0, 0] = -999
assert m_view[0, 0] == -999        # 视图同步变化

# matrix：默认复制，互不影响
arr2 = np.arange(12).reshape(3, 4)
m_copy = np.matrix(arr2)           # copy 默认 True
arr2[0, 0] = -999
assert m_copy[0, 0] == 0           # 不受影响，仍是原值
assert not np.may_share_memory(arr2, m_copy)

# 显式 copy=False 时，matrix 也变成视图
arr3 = np.arange(12).reshape(3, 4)
m_view2 = np.matrix(arr3, copy=False)
arr3[0, 0] = -999
assert m_view2[0, 0] == -999
assert np.may_share_memory(arr3, m_view2)

print("4.2 全部断言通过")
```

**需要观察的现象**：`asmatrix` 和 `matrix(..., copy=False)` 的结果会跟着原数组变；默认 `matrix(...)` 不会。`np.may_share_memory` 分别返回 `False` / `True`。

**预期结果**：打印 `4.2 全部断言通过`。

> 说明：`np.may_share_memory(a, b)` 是 numpy 提供的工具函数，返回 `True` 表示两者可能共享内存（视图），`False` 表示几乎肯定不共享。它正是用来区分「复制」与「视图」的标准手段。

#### 4.2.5 小练习与答案

**练习 1**：既然 `asmatrix` 不复制数据，那么对一个很大的 `ndarray` 反复调用 `asmatrix` 会带来内存开销吗？

> **答案**：几乎不会。每次调用只新建一个轻量的 `matrix` 对象头（视图），底层数据缓冲区始终是同一块，所以重复调用很廉价。

**练习 2**：`np.asmatrix(x)` 和 `x.view(np.matrix)` 在 `x` 是 `ndarray` 时结果一样吗？

> **答案**：在「不复制、dtype 一致」的前提下，二者都返回共享内存的 `matrix` 视图，效果等价——`__new__` 内部正是用 `data.view(cls)` 实现的（[defmatrix.py:L139](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L139)）。区别在于 `asmatrix` 还会处理字符串、嵌套列表等非 `ndarray` 输入，而 `x.view` 只适用于已是数组的情况。

---

### 4.3 bmat 块矩阵构造的三条路径

#### 4.3.1 概念说明

`bmat`（block matrix）用来把若干小矩阵像拼瓷砖一样拼成一个大矩阵。例如有 `A`、`B`、`C`、`D` 四个 2×2 矩阵，可以拼成

\[
\begin{bmatrix} A & B \\ C & D \end{bmatrix}
\]

`bmat` 接受三种形式的输入：

1. **字符串**：`'A, B; C, D'`，其中 `A`/`B`/`C`/`D` 是当前作用域里的变量名；
2. **嵌套序列**：`[[A, B], [C, D]]`；
3. **ndarray**：直接传一个已经拼好的数组。

无论走哪条路径，最终都会调用 `matrix(...)`，所以 `bmat` 的返回值一定是 `matrix`（也因此会触发 `PendingDeprecationWarning`）。

#### 4.3.2 核心流程

```
bmat(obj, ldict=None, gdict=None):
    if obj 是 str:
        if gdict 为 None: 自动从调用栈取 局部/全局 作用域
        else:             用调用方提供的 gdict/ldict
        return matrix(_from_string(obj, 全局, 局部))
    if obj 是 tuple/list:
        对 [[A,B],[C,D]]：逐行 concatenate(axis=-1)，再 concatenate(axis=0)
        若某一行本身就是 ndarray：直接 concatenate(obj, axis=-1)
    if obj 是 ndarray:
        return matrix(obj)
```

字符串路径最有意思：它要把 `'A, B; C, D'` 里的名字解析成实际变量。`;` 分行，`,` 分块，空格分元素；名字先在局部字典 `ldict` 里找，找不到再去全局字典 `gdict` 找，都找不到就抛 `NameError`。

#### 4.3.3 源码精读

`bmat` 的签名与文档：

[defmatrix.py:L1040-L1094](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L1040-L1094) —— 注意它的两个可选参数 `ldict` / `gdict`：只有当 `obj` 是字符串、且显式传了 `gdict` 时，这两个字典才生效；否则会自动回溯调用栈取作用域。

字符串分支：

[defmatrix.py:L1095-L1105](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L1095-L1105) —— 当 `gdict is None` 时，用 `sys._getframe().f_back` 拿到 **调用者** 的栈帧，再取其 `f_globals` / `f_locals`，这就是 `bmat('A,B')` 能“看见”你代码里 `A`、`B` 变量的原因。

嵌套序列分支：

[defmatrix.py:L1107-L1115](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L1107-L1115) —— `[[A,B],[C,D]]` 形式：先对每一行内部 `concatenate(axis=-1)`（横向拼），再把各行 `concatenate(axis=0)`（纵向拼）。若某一“行”本身就是 `ndarray`（即整体是 `[A, B]` 这种一维结构），则直接横向拼接。

ndarray 分支：

[defmatrix.py:L1116-L1117](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L1116-L1117) —— 直接 `return matrix(obj)`。

字符串名字解析的具体实现（`;` 分行、`,` 分块、空格分元素、先 `ldict` 后 `gdict`）：

[defmatrix.py:L1015-L1037](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L1015-L1037) —— `_from_string` 内部用 `try/except KeyError` 嵌套，先查局部字典，再查全局字典，两者都失败时 `raise NameError`。

#### 4.3.4 代码实践

**实践目标**：用字符串和嵌套序列两种方式构造同一个块矩阵，并验证它们相等。

```python
import warnings
warnings.simplefilter("ignore")
import numpy as np

A = np.asmatrix('1 1; 1 1')
B = np.asmatrix('2 2; 2 2')
C = np.asmatrix('3 4; 5 6')
D = np.asmatrix('7 8; 9 0')

# 方式 1：字符串（名字 A/B/C/D 自动从当前作用域解析）
m1 = np.bmat('A, B; C, D')
# 方式 2：嵌套序列
m2 = np.bmat([[A, B], [C, D]])

expected = np.bmat([[A, B], [C, D]])
assert m1.shape == (4, 4)
assert np.array_equal(m1, m2)

# 用 ldict 覆盖名字解析：让 'A' 指向另一个数组（2x2 的全 9 矩阵）
other = np.asmatrix('9 9; 9 9')
m3 = np.bmat('A, A; A, A', ldict={'A': other})   # [[other, other],[other, other]]
assert m3.shape == (4, 4)
assert np.array_equal(m3, np.full((4, 4), 9))    # 名字被 ldict 重定向 → 结果全 9
print("4.3 全部断言通过；块矩阵 m1 为：")
print(m1)
```

**需要观察的现象**：`m1` 与 `m2` 完全相等，都是 4×4；`m3` 因为 `ldict` 把 `A` 重定向到了全 9 矩阵，结果是全 9 的 4×4。

**预期结果**：打印 `4.3 全部断言通过` 及一个 4×4 的块矩阵。

> 说明：上面 `m3` 的断言写得稍复杂，只是为了用纯数组运算拼出对照结果。如果你不想纠结这行，可以直接 `print(m3)` 肉眼确认它是全 9 即可。

#### 4.3.5 小练习与答案

**练习 1**：`np.bmat('A,B;A,A', gdict={'A': B})`（注意是 `gdict` 而非 `ldict`）会怎样？

> **答案**：会抛 `TypeError`。因为 `bmat` 的约定是：只有当 `obj` 是字符串 **且显式传了 `gdict`** 时才使用自定义字典；而只传 `gdict` 不传 `ldict` 会导致 `ldict` 为 `None`，进入 `_from_string` 后访问 `ldict[col]` 时出错。测试 [test_defmatrix.py:L56](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_defmatrix.py#L56) 正是用 `assert_raises(TypeError, bmat, "A,A;A,A", gdict={'A': B})` 固化了这一点。

**练习 2**：`bmat([[A, B], [C, D]])` 里，为什么是先 `axis=-1`（横向）再 `axis=0`（纵向）？

> **答案**：外层列表的每个元素代表结果的一“行”，所以先把每行内部的 `[A, B]` 横向拼起来（`axis=-1`，即列方向），再把拼好的各行纵向叠起来（`axis=0`，即行方向），最终得到块矩阵布局。

---

### 4.4 用 PytestTester.test 运行子包测试

#### 4.4.1 概念说明

[u1-l2](u1-l2-package-structure-exports.md) 已经提过：`matrixlib/__init__.py` 用 `PytestTester(__name__)` 给子包装配了一个 `test` 对象，调用 `np.matrixlib.test()` 就能跑该子包下的全部测试。本节我们看这个 `test` 到底是什么、调用时发生了什么。

关键点：`test` 是 `PytestTester` 的一个 **实例**，而 `PytestTester` 实现了 `__call__`，所以实例可以像函数一样被「调用」——`np.matrixlib.test()` 本质上是执行 `PytestTester.__call__()`。

#### 4.4.2 核心流程

```
__init__.py:  test = PytestTester('numpy.matrixlib')   # 装配
调用 test(label='fast', verbose=1, ...):
    定位模块路径 numpy/matrixlib/
    组装 pytest 命令行参数（-l、-q、若干 -W 过滤、label 过滤等）
    特别地：过滤掉 matrix 的 PendingDeprecationWarning
    打印 numpy 版本与 CPU 特性信息
    code = pytest.main(pytest_args)
    return code == 0    # True 表示全部通过
```

两个值得注意的细节：

- 默认 `label='fast'`，会跳过带 `pytest.mark.slow` 标记的用例；传 `label='full'` 才跑全部。
- 它专门加了一条 `-W ignore:the matrix subclass is not`，**把 matrix 的 `PendingDeprecationWarning` 过滤掉**，否则每个构造 `matrix` 的用例都会触发警告甚至被「警告即错误」的 CI 配置打断。

#### 4.4.3 源码精读

装配发生在 `__init__.py`：

[__init__.py:L9-L12](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/__init__.py#L9-L12) —— `from numpy._pytesttester import PytestTester`、`test = PytestTester(__name__)`、`del PytestTester`。最后 `del` 是为了不把 `PytestTester` 这个类名暴露给子包使用者，只留下 `test` 这个可调用实例。

`PytestTester` 类本身定义在 matrixlib 之外：

[_pytesttester.py:L45-L75](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_pytesttester.py#L45-L75) —— 这是 `test()` 的实现类，文档明确说明了在每个子包 `__init__.py` 里装配它的标准写法。

真正干活的 `__call__` 方法：

[_pytesttester.py:L79-L80](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_pytesttester.py#L79-L80) —— 签名 `__call__(self, label='fast', verbose=1, extra_argv=None, doctests=False, coverage=False, durations=-1, tests=None)`，默认只跑 fast 用例。

专门为 matrix 过滤警告的两行：

[_pytesttester.py:L146-L150](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_pytesttester.py#L146-L150) —— 注释直说 “When testing matrices, ignore their PendingDeprecationWarnings”，对应 `-W ignore:the matrix subclass is not`。

最后运行 pytest 并把退出码转成布尔结果：

[_pytesttester.py:L173-L186](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_pytesttester.py#L173-L186) —— `--pyargs` 把模块名当包路径来收集测试；`pytest.main(pytest_args)` 真正执行；`return code == 0` 表示「全部通过」。

#### 4.4.4 代码实践

**实践目标**：运行 matrixlib 子包的测试，并理解输出。

最直接的方式是在命令行执行：

```bash
python -c "import numpy; result = numpy.matrixlib.test(); print('全部通过:', result)"
```

**需要观察的现象**：

1. 先打印一行 `NumPy version ...` 和 `NumPy CPU features: ...`（来自 `_show_numpy_info`）；
2. 接着是 pytest 的收集与运行结果，结尾形如 `N passed, M skipped in X.XX seconds`；
3. 最后打印 `全部通过: True`（或 `False`）。

**预期结果**：在一份正常的 numpy 安装上，应看到若干用例通过、`全部通过: True`。

> 待本地验证：测试是否通过取决于你本地 numpy 的构建状态与已安装的 pytest 版本。如果你是在 numpy 源码仓库内（开发模式，存在 `pytest.ini`），警告会被当成错误；matrixlib 的 `test()` 已经预先过滤掉了 matrix 自身的 `PendingDeprecationWarning`，所以不会因为「构造 matrix 就报警」而失败。

如果想只跑某一个测试类来对照源码，也可以直接用 pytest（等价于 `test(tests=[...])` 的一条更直接的路径）：

```bash
python -m pytest numpy/matrixlib/tests/test_defmatrix.py::TestCtor -v
```

#### 4.4.5 小练习与答案

**练习 1**：为什么 `np.matrixlib.test()` 不会因为「每次构造 matrix 都发 `PendingDeprecationWarning`」而失败？

> **答案**：因为 `PytestTester.__call__` 在组装 pytest 参数时，专门加了 `-W ignore:the matrix subclass is not` 把这条警告过滤掉了（[_pytesttester.py:L147-L148](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_pytesttester.py#L147-L148)）。

**练习 2**：`np.matrixlib.test()` 返回值的类型是什么？`True` 代表什么？

> **答案**：返回布尔值（`return code == 0`）。`True` 表示 pytest 退出码为 0，即没有用例失败；`False` 表示有失败或错误（[_pytesttester.py:L186](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_pytesttester.py#L186)）。

---

## 5. 综合实践

把本讲的四个模块串起来，完成下面这个贯通任务。

**任务**：构造一个 `matrix` 视图，验证它的视图语义；再用 `bmat` 拼一个块矩阵；最后跑一次子包测试确认你的 numpy 一切正常。

把下面的脚本存为 `u1_l3_practice.py`，在装有 numpy 与 pytest 的环境中运行：

```python
import warnings
warnings.simplefilter("ignore")           # 屏蔽 PendingDeprecationWarning，聚焦本任务
import numpy as np

# ---- 1. asmatrix 视图语义 ----
arr = np.arange(12).reshape(3, 4)
m = np.asmatrix(arr)                       # 不复制：共享内存
assert m.shape == (3, 4)
arr[0, 0] = -1
assert m[0, 0] == -1, "asmatrix 应与原数组同步变化（视图）"
assert np.may_share_memory(arr, m)

# ---- 2. matrix 默认复制 ----
arr2 = np.arange(12).reshape(3, 4)
m2 = np.matrix(arr2)                       # 默认 copy=True
arr2[0, 0] = -1
assert m2[0, 0] == 0, "matrix 默认复制，修改 arr2 不应影响 m2"
assert not np.may_share_memory(arr2, m2)

# ---- 3. bmat 拼块矩阵（字符串 vs 序列，两种路径结果应一致）----
A = np.asmatrix('1 2; 3 4')
B = np.asmatrix('5 6; 7 8')
block1 = np.bmat('A, B; A, B')
block2 = np.bmat([[A, B], [A, B]])
assert np.array_equal(block1, block2)
assert block1.shape == (4, 4)

print("综合实践断言全部通过。接下来在命令行运行：")
print('  python -c "import numpy; print(numpy.matrixlib.test())"')
```

**操作步骤**：

1. 运行 `python u1_l3_practice.py`，确认打印 `综合实践断言全部通过`；
2. 再在命令行执行 `python -c "import numpy; numpy.matrixlib.test()"`，观察测试输出。

**需要观察的现象**：

- 脚本前三步断言全部通过，证明你掌握了 `asmatrix`（视图）与 `matrix`（复制）的差异，以及 `bmat` 的两种等价写法；
- 命令行测试会先打印 numpy 版本信息，再打印 pytest 汇总（`N passed ...`）。

**预期结果**：脚本断言通过；测试输出以「全部通过」（退出码 0）收尾。

> 待本地验证：第 2 步的测试结果取决于本地 numpy/pytest 环境与是否处于源码仓库（开发模式）。若在源码树内运行，可改用官方推荐的 `spin test numpy/matrixlib`。

## 6. 本讲小结

- `matrix.__new__` 按 `matrix` / `ndarray` / 字符串三类输入分支处理，`copy` 标志决定是否复制数据，并在末尾把 0 维、1 维强制补成二维、把 >2 维直接拒绝。
- `asmatrix(data)` 等价于 `matrix(data, copy=False)`，对 `ndarray` 输入返回 **共享内存的视图**；而 `matrix(arr)` 默认 `copy=True` 会 **复制**，这是两者最实用的区别。
- `bmat` 有三条输入路径——字符串（按名字在局部/全局作用域解析）、嵌套序列（`[[A,B],[C,D]]`）、ndarray——最终都返回 `matrix`。
- `np.matrixlib.test()` 来自 `__init__.py` 里 `test = PytestTester(__name__)` 的装配；它封装了 pytest，默认跑 fast 用例，并预先过滤掉 matrix 的 `PendingDeprecationWarning`，返回布尔结果。
- `np.may_share_memory(a, b)` 是判断「视图还是复制」的标准工具；测试文件里的 `test_asmatrix` 用例正是用「改原数组、看 matrix 是否同步」来固化视图语义的。

## 7. 下一步学习建议

本讲已经让你能「用起来」并「跑测试」。接下来进入进阶层，深入 `matrix` 类的内部实现：

- **[u2-l1] matrix 构造函数 \_\_new\_\_ 与二维强制**：把本讲 4.1 节的 `__new__` 逐行读透，搞清楚 `N.ndarray.__new__(cls, ..., buffer=arr, order=order)` 这一步为什么用 buffer 方式构造。
- **[u2-l2] 字符串矩阵语法与 _convert_from_string**：本讲只用到了字符串语法的结果，下一讲会拆开 `ast.literal_eval` 的安全解析与行列长度校验。
- **[u2-l4] ndarray 子类化机制 \_\_array_finalize\_\_ 与 \_\_array_priority\_\_**：理解 `matrix` 之所以「在运算后仍是二维 matrix」的底层支柱，这是从「会用」走向「能二次开发」的关键一讲。

建议在进入 u2 之前，先把本讲的「综合实践」完整跑通，确保你对视图/复制、`bmat` 三条路径、`test()` 入口都有第一手的运行体感。
