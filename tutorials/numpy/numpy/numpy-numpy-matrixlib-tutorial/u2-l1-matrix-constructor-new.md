# matrix 构造函数 `__new__` 与二维强制

## 1. 本讲目标

本讲精读 `numpy.matrixlib.defmatrix.matrix` 的构造函数 `__new__`。读完本讲，你应当能够：

- 说清楚为什么 `matrix` 要重写 `__new__`、并用 `N.ndarray.__new__` 直接装配对象，而不是走普通的 `__init__`。
- 看懂 `__new__` 对四类输入（`matrix` / `ndarray` / 字符串 / 通用 array_like）的分发逻辑。
- 准确说出 `copy` 标志在这几类输入下分别产生了「同一个对象 / 视图 / 副本」中的哪一种。
- 解释 0 维、1 维输入如何被补成 `(1,1)` 与 `(1,N)`，以及超过二维时为什么会出现**两条不同**的报错消息。

本讲承接 [u1-l3](u1-l3-quickstart-and-testing.md) 中「构造三种方式 + 二维强制」的结论，把它们从「现象」下沉到「源码逐行」，并为下一讲 [u2-l4](u2-l4-ndarray-subclass-array-finalize.md)（`__array_finalize__` 子类化机制）埋下伏笔。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**① `np.matrix` 是 `ndarray` 的子类。** 它的全部「矩阵味」（强制二维、`*` 是矩阵乘、索引不降维）都来自对父类方法的重写。构造函数就是它「立规矩」的第一道关卡。

**② Python 的 `__new__` 与 `__init__` 分工不同。**
- `__new__(cls, ...)` 负责**创建并返回**一个实例对象（分配内存）。
- `__init__(self, ...)` 负责**初始化**那个已经创建好的对象。

对大多数类，你只写 `__init__` 就够了。但当对象是「不可变」的、或内存布局在创建时就必须定死时，就必须在 `__new__` 里把形状、dtype、缓冲区一次性确定下来——`ndarray` 正是这种对象。所以子类化 `ndarray` 时，关键往往在 `__new__`，而不是 `__init__`。

**③ 视图（view）与副本（copy）。**
- 视图：与原数组**共享同一块内存**，改一个另一个跟着变，用 `np.may_share_memory(a, b)` 可判定。
- 副本：另开一块内存，互不影响。

`copy` 标志的本质，就是在「我想省内存（要视图）」与「我想隔离数据（要副本）」之间做选择。

## 3. 本讲源码地图

本讲几乎只围绕一个文件展开：

| 文件 | 作用 |
| --- | --- |
| [numpy/matrixlib/defmatrix.py:119-170](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L119-L170) | `matrix.__new__` 全部主体，本讲的绝对主角 |
| [numpy/matrixlib/defmatrix.py:16-33](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L16-L33) | `_convert_from_string`，字符串输入的解析器（本讲只点到为止，详见 [u2-l2](u2-l2-string-parsing-convert-from-string.md)） |
| [numpy/matrixlib/defmatrix.py:172-193](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L172-L193) | `__array_finalize__`，本讲末尾会引用它来解释「ndarray 输入超过二维」的报错来源（详见 [u2-l4](u2-l4-ndarray-subclass-array-finalize.md)） |
| [numpy/matrixlib/tests/test_defmatrix.py:16-37](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_defmatrix.py#L16-L37) | `TestCtor`，构造函数的官方测试，本讲实践题的参照 |

此外，类开头有一行 `__array_priority__ = 10.0`（[numpy/matrixlib/defmatrix.py:117](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L117)），它不在本讲主线，会在 [u2-l4](u2-l4-ndarray-subclass-array-finalize.md) 详讲，这里只先知道它存在。

## 4. 核心概念与源码讲解

### 4.1 `matrix.__new__` 的总体结构：签名、警告与四路分发骨架

#### 4.1.1 概念说明

`matrix.__new__` 是 `matrix` 对象来到世间的「入口闸机」。无论你用 `np.matrix('1 2; 3 4')`、`np.matrix([[1,2],[3,4]])` 还是 `np.matrix(some_ndarray)`，最终都会调用它。它要一次性完成四件事：

1. **发弃用警告**：每次构造都提醒你 `matrix` 已不推荐使用。
2. **识别输入类型**，走不同的「加工流水线」。
3. **保证二维**：把 0/1 维补全、把 >2 维拒绝。
4. **返回一个 `matrix` 对象**。

#### 4.1.2 核心流程

`__new__` 的分发骨架可以画成下面这张流程图（伪代码）：

```text
matrix.__new__(cls, data, dtype=None, copy=True)
│
├─ 永远先发 PendingDeprecationWarning
│
├─ data 是 matrix 吗？      ──是──► 复用 / astype  （分支 A）
│
├─ data 是 ndarray 吗？     ──是──► data.view(matrix) 后按 copy 决定（分支 B）
│
├─ data 是 str 吗？         ──是──► _convert_from_string 解析成嵌套列表（分支 C）
│
└─ 其它（list / 标量 / 嵌套序列等）
        │
        N.array(data) 转成 ndarray
        再做「二维强制 + N.ndarray.__new__ 装配」（分支 D）
```

注意判断顺序：`matrix` 在 `ndarray` **之前**判（因为 `matrix` 也是 `ndarray`，先判 `matrix` 才不会被吞掉）；`str` 单独判，因为字符串要先解析成 Python 列表。

#### 4.1.3 源码精读

先看签名与那条无处不在的警告：

```python
def __new__(cls, data, dtype=None, copy=True):
    warnings.warn('the matrix subclass is not the recommended way to '
                  'represent matrices or deal with linear algebra ...',
                  PendingDeprecationWarning, stacklevel=2)
```

对应 [numpy/matrixlib/defmatrix.py:119-125](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L119-L125)——这段代码做了两件事：定下三参数签名 `(cls, data, dtype, copy)`，并在最开头**无条件**发出 `PendingDeprecationWarning`（这也是为什么 [u1-l1](u1-l1-project-overview.md) 里说「每次 `matrix(...)` 都会报警」，连 `asmatrix` / `bmat` 也不例外，因为它们内部都调用 `matrix(...)`）。`stacklevel=2` 让警告指向**调用者**那一行，而不是 numpy 内部。

紧接着就是四个 `if` 分支，骨架见 [numpy/matrixlib/defmatrix.py:126-148](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L126-L148)。本节只看骨架，每个分支的细节放到 4.2、4.3、4.4 展开。

#### 4.1.4 代码实践

**实践目标**：确认「无论怎么构造，都会进同一个 `__new__` 并报警」。

**操作步骤**：

```python
import warnings
import numpy as np

with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    np.matrix([[1, 2], [3, 4]])
    np.matrix("1 2; 3 4")
    np.asmatrix(np.arange(4).reshape(2, 2))
    print("捕获到的警告数：", len(w))
    print("类别：", w[0].category.__name__)
```

**需要观察的现象**：捕获到的警告数应该是 `3`，每条类别都是 `PendingDeprecationWarning`。

**预期结果**：三种构造方式各触发一次警告，证明它们都流经 `__new__` 顶部的 `warnings.warn`。基于源码（[L120-L125](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L120-L125)）可确定该结论，建议本地运行确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么分支 A（`isinstance(data, matrix)`）必须写在分支 B（`isinstance(data, N.ndarray)`）之前？

**参考答案**：因为 `matrix` 是 `ndarray` 的子类，`isinstance(some_matrix, N.ndarray)` 同样为 `True`。若先判 `ndarray`，所有 `matrix` 输入都会被错误地当成普通 `ndarray` 处理，跳过针对 `matrix` 的复用优化。

**练习 2**：`np.asmatrix` 调用 `matrix(data, dtype=dtype, copy=False)`（见 [numpy/matrixlib/defmatrix.py:70](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L70)）。这一调用会不会触发 `__new__` 顶部的警告？

**参考答案**：会。`asmatrix` 内部就是一次普通的 `matrix(...)` 调用，必然进入 `__new__` 并命中 `warnings.warn(..., PendingDeprecationWarning)`。

---

### 4.2 `__new__` vs `__init__`：为什么用 `N.ndarray.__new__` 直接装配

#### 4.2.1 概念说明

这是本讲最容易被初学者跳过、却最关键的一个设计点：`matrix` 类里**根本没有定义 `__init__`**（你可以搜一遍整个文件确认）。所有的构造工作都在 `__new__` 里完成，并且最终通过显式调用父类的 `N.ndarray.__new__(...)` 来「分配内存 + 设定形状/dtype/缓冲区」。

为什么要这样？因为 `ndarray` 的形状和数据布局在**对象诞生那一刻**就必须确定，事后无法在 `__init__` 里再改。`__new__` 是唯一能在「对象成型前」插手的钩子。

#### 4.2.2 核心流程

最终的装配发生在分支 D 末尾：

```python
ret = N.ndarray.__new__(cls, shape, arr.dtype, buffer=arr, order=order)
return ret
```

这一行的角色分工是：

| 参数 | 作用 |
| --- | --- |
| `cls` | 要创建的类，这里是 `matrix`（不是 `ndarray`），所以产出的对象天生就是 `matrix` |
| `shape` | 形状，已经过 4.4 节的「二维强制」处理过 |
| `arr.dtype` | 数据类型 |
| `buffer=arr` | **直接借用 `arr` 的内存**作为新对象的数据源，避免再拷贝一次 |
| `order=order` | 内存布局，`'C'`（行优先）或 `'F'`（列优先） |

`buffer=arr` 是这里的精髓：`__new__` 前面已经用 `N.array(data, ...)` 把数据规整成一个干净的 `arr`，这里把它的内存「过户」给新的 `matrix` 对象，既保证了类型正确，又省掉一次无谓的拷贝。

#### 4.2.3 源码精读

完整的「转数组 → 选 order → 装配」三步见 [numpy/matrixlib/defmatrix.py:150-170](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L150-L170)：

```python
# now convert data to an array
copy = None if not copy else True
arr = N.array(data, dtype=dtype, copy=copy)
ndim = arr.ndim
shape = arr.shape
if (ndim > 2):
    raise ValueError("matrix must be 2-dimensional")
elif ndim == 0:
    shape = (1, 1)
elif ndim == 1:
    shape = (1, shape[0])

order = 'C'
if (ndim == 2) and arr.flags.fortran:
    order = 'F'

if not (order or arr.flags.contiguous):
    arr = arr.copy()

ret = N.ndarray.__new__(cls, shape, arr.dtype, buffer=arr, order=order)
return ret
```

几个关键点：

- `copy = None if not copy else True`：当 `copy=False` 时把布尔值转成 `None`。这是为了适配 `np.array` 的语义——`np.array(data, copy=None)` 表示「能不拷贝就不拷贝」，比 `copy=False`（强制不拷贝，遇到必须拷贝的情况会报错）更宽容。
- `order` 的选择（[L162-L164](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L162-L164)）：默认行优先 `'C'`；只有当输入已经是二维且 `arr.flags.fortran` 为真（列优先连续）时，才改用 `'F'` 以保持原布局。
- `if not (order or arr.flags.contiguous):`（[L166-L167](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L166-L167)）：如果既不是 `'F'`、数据又不连续，就先 `arr.copy()` 拍成连续的，否则 `buffer=arr` 借内存时会出问题。注意字符串 `order` 非空时 `'C'` 也是「真值」，所以 `'C'` 走 `order='C'` 分支不会被这里重复拷贝。

> **关于「为什么没有 `__init__`」**：你可以用编辑器搜索整个 `defmatrix.py`，会发现类里只有 `__new__`、`__array_finalize__`、`__getitem__` 等，没有 `__init__`。这正是子类化 `ndarray` 的标准姿势——内存布局在 `__new__` 阶段一锤定音，`__init__` 无事可做，所以干脆不写。

#### 4.2.4 代码实践

**实践目标**：直观感受「`buffer=arr` 让新 matrix 与底层 ndarray 共享内存」。

**操作步骤**：

```python
import numpy as np

# 用一个已经连续的 ndarray 作为底层 buffer
base = np.array([10, 20, 30, 40])          # 1D
m = np.matrix(base)                         # 走分支 D：N.array(base) -> arr
print("shape:", m.shape)                    # (1, 4)
print("may_share_memory(base, m):", np.may_share_memory(base, m))
```

**需要观察的现象**：`m.shape` 是 `(1, 4)`（1 维被补成二维）；关注 `may_share_memory` 的布尔结果。

**预期结果**：形状必为 `(1, 4)`。是否共享内存取决于 `N.array(base, copy=None)` 是否复制——对已经是连续 ndarray 的输入，`copy=None` 通常不复制，因此 `may_share_memory` 多半为 `True`。这一内存共享结论建议本地运行确认（行为受 `np.array` 的 `copy=None` 规则约束）。

#### 4.2.5 小练习与答案

**练习 1**：如果把最后一行改成 `return N.ndarray.__new__(ndarray, shape, ...)`（注意第一个参数从 `cls` 换成 `ndarray`），会发生什么？

**参考答案**：产出的对象会是普通 `ndarray` 而非 `matrix`，丢失所有矩阵行为。第一个参数 `cls` 决定了「生出来的是什么类」，这正是把 `cls` 透传进去的意义。

**练习 2**：`order` 默认是 `'C'`。代码里 `if not (order or arr.flags.contiguous): arr = arr.copy()` 用的是 `order` 字符串的真值，而不是 `order == 'C'`。这样写有什么好处？

**参考答案**：`order` 要么是 `'C'` 要么是 `'F'`，二者都是非空字符串（真值），所以 `not (order or ...)` 只有在「`order` 为真」这一半为假时才可能整体为真，等价于「`order` 为假」——但代码里 `order` 永远非空。等价地，这一行实际只在 `arr` 不连续时才补一次拷贝，逻辑上等价于 `if order == 'C' and not arr.flags.contiguous: arr = arr.copy()`，用真值判断是为了与变量名解耦、保持简洁。

---

### 4.3 `copy` 标志：四类输入下的复制语义

#### 4.3.1 概念说明

`copy` 标志是 `matrix.__new__` 最容易让人踩坑的参数。它的默认值是 `copy=True`（见签名 [L119](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L119)），但「复制还是不复制」的结果**取决于你传的是哪类输入**。同一句 `copy=False`，在 matrix 输入、ndarray 输入、通用输入下含义并不完全相同。

#### 4.3.2 核心流程

下表把三类输入在 `copy` 真假下的产物一次讲清：

| 输入类型 | `copy=True` | `copy=False` |
| --- | --- | --- |
| `matrix`（分支 A） | `data.astype(dtype)`（始终是副本） | dtype 相同 → 返回**同一个对象** `data`；dtype 不同 → `astype`（副本） |
| `ndarray`（分支 B） | `data.view(matrix).copy()`（副本） | `data.view(matrix)`（**视图**，共享内存） |
| 字符串 / list / 标量（分支 D） | `N.array(data, copy=True)` | `N.array(data, copy=None)`（尽量不复制） |

核心结论：**只有「matrix 输入 + dtype 不变 + copy=False」会真正返回原对象本身**（`is` 判定为真）；**ndarray 输入 + copy=False** 得到的是共享内存的视图（`is` 为假，但 `may_share_memory` 为真）。

#### 4.3.3 源码精读

**分支 A（matrix 输入）**，见 [numpy/matrixlib/defmatrix.py:126-132](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L126-L132)：

```python
if isinstance(data, matrix):
    dtype2 = data.dtype
    if (dtype is None):
        dtype = dtype2
    if (dtype2 == dtype) and (not copy):
        return data          # 唯一「原对象返回」的出口
    return data.astype(dtype)  # astype 默认 copy=True，必为副本
```

注意 `astype(dtype)` 本身就带 `copy=True`，所以这一行**总是**产生副本——即使调用者传了 `copy=False`，只要 dtype 需要改变，物理上就必须复制。

**分支 B（ndarray 输入）**，见 [numpy/matrixlib/defmatrix.py:134-145](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L134-L145)：

```python
if isinstance(data, N.ndarray):
    if dtype is None:
        intype = data.dtype
    else:
        intype = N.dtype(dtype)
    new = data.view(cls)          # 先做视图（共享内存），类型变 matrix
    if intype != data.dtype:
        return new.astype(intype) # dtype 变 → 副本
    if copy:
        return new.copy()         # 显式 copy=True → 副本
    else:
        return new                # copy=False → 视图
```

这里的 `data.view(cls)` 是 numpy 子类化的惯用法：它新建一个 `matrix` 对象但**不复制数据**，且会触发 `__array_finalize__`（4.4 节会用到）。随后按 `copy` 决定是否再 `.copy()` 一次。

> 这正好解释了 [u1-l3](u1-l3-quickstart-and-testing.md) 里「`asmatrix` 返回视图、`matrix(arr)` 默认复制」的差异：`asmatrix` 就是 `matrix(data, copy=False)`，落到分支 B 的 `return new`（视图）；而 `matrix(arr)` 默认 `copy=True`，落到 `return new.copy()`（副本）。

**分支 D（通用输入）** 的 `copy` 处理见 [numpy/matrixlib/defmatrix.py:151-152](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L151-L152)，`copy=False` 被转成 `None` 交给 `np.array`，语义变成「能不复制就不复制」，比强制的 `False` 更稳。

#### 4.3.4 代码实践

**实践目标**：用 `is` 与 `may_share_memory` 两条线索，验证三类输入在 `copy` 下的不同产物。

**操作步骤**：

```python
import numpy as np
from numpy import matrix

# --- 分支 A：matrix 输入 ---
m1 = matrix([[1, 2], [3, 4]])
print("matrix(m1, copy=False) is m1 :", matrix(m1, copy=False) is m1)   # True
print("matrix(m1, copy=True)  is m1 :", matrix(m1, copy=True)  is m1)   # False

# --- 分支 B：ndarray 输入 ---
a = np.array([[1, 2], [3, 4]])
view = matrix(a, copy=False)
copy = matrix(a, copy=True)
print("copy=False shares mem:", np.may_share_memory(a, view))  # True
print("copy=True  shares mem:", np.may_share_memory(a, copy))  # False

# 修改原数组，观察谁跟着变
a[0, 0] = 99
print("view[0,0] =", view[0, 0], " copy[0,0] =", copy[0, 0])
```

**需要观察的现象**：第一个 `is` 为 `True`（分支 A 原对象返回）；第二个 `is` 为 `False`。分支 B 中视图与原数组共享内存、副本不共享；修改 `a[0,0]` 后 `view` 跟着变、`copy` 不变。

**预期结果**（基于 [L126-L145](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L126-L145) 源码推断）：`is` 分别为 `True` / `False`；`may_share_memory` 分别为 `True` / `False`；修改后 `view[0,0]` 为 `99`、`copy[0,0]` 仍为 `1`。建议本地运行确认。

#### 4.3.5 小练习与答案

**练习 1**：`matrix(m1, dtype=np.float64, copy=False)` 会返回 `m1` 本身吗？

**参考答案**：不会。因为 `dtype2(int) != dtype(float64)`，[L130](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L130) 的 `(dtype2 == dtype) and (not copy)` 条件不成立，落到 `return data.astype(dtype)`，得到一个 float64 的副本。

**练习 2**：为什么分支 B 里要先 `data.view(cls)` 再按 `copy` 决定 `.copy()`，而不是直接根据 `copy` 选择「`np.array(data, copy=...)`」？

**参考答案**：`view(cls)` 一步就把「类型变成 `matrix` + 触发 `__array_finalize__` 完成二维强制」都办了；之后 `.copy()` 只是再加一次物理拷贝。如果改用 `np.array(data, copy=...)`，还要再想办法把结果转成 `matrix` 类型并补二维，反而绕远路。这是子类化 `ndarray` 的标准套路。

---

### 4.4 二维形状强制：补全、拒绝与两条错误消息

#### 4.4.1 概念说明

「永远二维」是 `matrix` 的灵魂。构造阶段就把它落实为三条规矩：

- 0 维（标量）→ 补成 `(1, 1)`。
- 1 维（向量）→ 补成 `(1, N)`。
- 超过 2 维 → **拒绝**并抛错。

这里有一个特别容易被误记的点：**超过二维时报出的错误消息并不唯一**。对一个 3 维 `ndarray` 输入和一个 3 层嵌套 `list` 输入，你看到的报错文字是不同的，因为它们走的是不同的代码路径。本节把这个区别讲透。

#### 4.4.2 核心流程

分支 D 里的「二维强制」逻辑很直白，见 [numpy/matrixlib/defmatrix.py:155-160](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L155-L160)：

```python
if (ndim > 2):
    raise ValueError("matrix must be 2-dimensional")   # 消息①
elif ndim == 0:
    shape = (1, 1)
elif ndim == 1:
    shape = (1, shape[0])
```

但这条 `raise` 只在**分支 D（通用输入）**里才会执行。如果输入本身就是 `ndarray`（分支 B），代码在 `data.view(cls)`（[L139](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L139)）这一步就把形状校验交给了 `__array_finalize__`，那里的逻辑更复杂——它会**先把所有长度为 1 的维度挤掉**再判断：

```python
newshape = tuple(x for x in self.shape if x > 1)   # 去掉所有 1 维
ndim = len(newshape)
if ndim == 2:
    self._set_shape(newshape)      # 挤掉 1 维后正好二维 → 接受
    return
elif (ndim > 2):
    raise ValueError("shape too large to be a matrix.")  # 消息②
```

把「去 1 维后的维数」记作 \(d'\)，则 \(d' = \bigl|\{\,s_i \in \text{shape} : s_i > 1\,\}\bigr|\)。判定规则是：

\[
\text{分支 B 的二维强制} = \begin{cases}
\text{接受（挤掉 1 维后重塑）} & d' = 2 \\
\text{抛出消息②} & d' > 2
\end{cases}
\]

于是同一个「3 维输入」，根据输入类型会有两种结局：

| 输入 | 走的分支 | 报错消息 |
| --- | --- | --- |
| 嵌套 list `[[[1,2],[3,4]]]`（3 维 array_like） | 分支 D | `matrix must be 2-dimensional`（[L156](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L156)） |
| `np.arange(8).reshape(2,2,2)`（3 维 ndarray） | 分支 B → `__array_finalize__` | `shape too large to be a matrix.`（[L186](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L186)） |

> 补充一个有趣的副产物：正因为分支 B 会先挤掉长度为 1 的维度，像 `np.arange(4).reshape(2, 2, 1)` 这样的 3 维 ndarray（含一个 1 维）会被分支 B **接受**并重塑成 `(2, 2)`；而对应的嵌套 list 却会被分支 D **拒绝**。这也是为什么二维强制不能只靠 `__new__`，还要 `__array_finalize__` 兜底——后者将在 [u2-l4](u2-l4-ndarray-subclass-array-finalize.md) 详解。

#### 4.4.3 源码精读

补全逻辑见 [numpy/matrixlib/defmatrix.py:153-160](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L153-L160)。注意 `shape` 这个局部变量被重新赋值后，会传给末尾的 `N.ndarray.__new__(cls, shape, ...)`（[L169](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L169)），从而让最终对象直接以二维形态诞生。

`__array_finalize__` 里那条「去 1 维」的判定见 [numpy/matrixlib/defmatrix.py:179-186](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L179-L186)。本节只借它解释 ndarray 输入的报错来源，完整机制留给 [u2-l4](u2-l4-ndarray-subclass-array-finalize.md)。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：验证 0/1/2 维三条补全分支，并亲手区分「>2 维」的两条报错消息。

**操作步骤**：

```python
import numpy as np
from numpy import matrix

# 1) 三条补全分支
assert matrix([[1, 2, 3]]).shape == (1, 3)   # 已是 2 维，保持 (1,3)
assert matrix([1, 2, 3]).shape    == (1, 3)   # 1 维 list -> (1,3)
assert matrix(5).shape            == (1, 1)   # 0 维标量 -> (1,1)
print("0/1/2 维补全断言全部通过")

# 2) >2 维：list 输入走分支 D，消息①
try:
    matrix([[[1, 2], [3, 4]]])
except ValueError as e:
    print("list 3D 报错:", e)

# 3) >2 维：ndarray 输入走分支 B -> __array_finalize__，消息②
try:
    matrix(np.arange(8).reshape(2, 2, 2))
except ValueError as e:
    print("ndarray 3D 报错:", e)
```

**需要观察的现象**：三条 `assert` 全过；两次 `try` 捕获到 `ValueError`，但**消息文字不同**。

**预期结果**（基于源码 [L155-L160](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L155-L160) 与 [L179-L186](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L179-L186) 推断）：

- `list 3D 报错: matrix must be 2-dimensional`
- `ndarray 3D 报错: shape too large to be a matrix.`

> 注意：这与「`matrix(np.arange(8).reshape(2,2,2))` 会抛 `matrix must be 2-dimensional`」的直觉不同。原因是 ndarray 输入走分支 B，在 `data.view(cls)` 时由 `__array_finalize__` 先拦下，抛的是消息②。建议本地运行确认这两条消息的差异。

#### 4.4.5 小练习与答案

**练习 1**：`matrix(0)` 的形状是什么？为什么？

**参考答案**：`(1, 1)`。`0` 是标量，走分支 D，`N.array(0)` 的 `ndim == 0`，命中 [L157-L158](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L157-L158) 的 `shape = (1, 1)`。这也呼应官方测试 [test_defmatrix.py:359-362](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_defmatrix.py#L359-L362) 中 `matrix(0)` 的用法。

**练习 2**：`matrix(np.arange(4).reshape(2, 2, 1))` 会报错吗？

**参考答案**：不会报错，结果是 `(2, 2)` 的 matrix。因为它是 ndarray 输入，走分支 B，`__array_finalize__` 先把长度为 1 的维度挤掉（[L180](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L180)），剩下 `d' = 2`，于是 `_set_shape((2,2))` 接受。这是分支 B 与分支 D 行为差异的典型例子。

**练习 3**：如果把 `matrix(np.arange(6).reshape(2, 3))` 传进去，会走哪个分支、形状是什么？

**参考答案**：走分支 B（ndarray 输入），`data.view(matrix)` 得到 `(2, 3)` 的二维 matrix（`__array_finalize__` 见 `ndim==2` 直接 return）。形状保持 `(2, 3)`，不经过分支 D 的补全逻辑。

## 5. 综合实践

把本讲四条主线（分发、`copy`、二维强制、装配）串起来，写一个「输入探针」函数，判断任意输入会走哪条分支、最终形状如何、是否与原数据共享内存。

**任务**：

```python
import numpy as np
from numpy import matrix

def probe(data, copy=True):
    """探测 data 经 matrix(...) 构造后的形状与内存共享情况。"""
    try:
        m = matrix(data, copy=copy)
    except ValueError as e:
        return {"ok": False, "err": str(e)}
    base = data if isinstance(data, np.ndarray) else None
    return {
        "ok": True,
        "shape": m.shape,
        "shares_mem": (np.may_share_memory(base, m) if base is not None else "N/A"),
    }

# 待你填表的输入
cases = [
    ("标量",       5),
    ("1维 list",   [1, 2, 3]),
    ("2维 list",   [[1, 2], [3, 4]]),
    ("1维 ndarray", np.array([1, 2, 3])),
    ("2维 ndarray", np.array([[1, 2], [3, 4]])),
    ("3维 ndarray", np.arange(8).reshape(2, 2, 2)),
    ("3维 list",   [[[1, 2], [3, 4]]]),
    ("字符串",     "1 2; 3 4"),
]

for name, data in cases:
    print(f"{name:14s} copy=True  ->", probe(data, copy=True))
    print(f"{name:14s} copy=False ->", probe(data, copy=False))
```

**你要完成的**：

1. 运行上述脚本，记录每个 `case` 的 `shape` 与 `shares_mem`。
2. 对照 4.2–4.4 的源码，解释为什么：
   - 「2 维 ndarray + copy=False」的 `shares_mem` 是 `True`，而 `copy=True` 是 `False`。
   - 「3 维 ndarray」与「3 维 list」一个报 `shape too large to be a matrix.`、一个报 `matrix must be 2-dimensional`。
3. 把结论整理成一张「输入类型 × copy × 产物」的对照表。

**预期结果**：标量→`(1,1)`；1 维输入→`(1,N)`；2 维输入→保持二维；3 维输入→报错（且两条消息不同）；ndarray 输入在 `copy=False` 时共享内存。无法确定的内存共享细节标注「待本地验证」。

## 6. 本讲小结

- `matrix.__new__` 是构造闸机，先无条件发 `PendingDeprecationWarning`，再按「matrix / ndarray / str / 通用」四路分发。
- `matrix` 用 `__new__` 而非 `__init__` 构造，最终通过 `N.ndarray.__new__(cls, shape, dtype, buffer=arr, order=order)` 一次性定下形状、dtype 与内存布局，并用 `buffer=arr` 借用已规整好的内存。
- `copy` 标志的语义随输入类型而变：matrix 输入 + 同 dtype + `copy=False` 才返回原对象；ndarray 输入 + `copy=False` 得视图、`copy=True` 得副本。
- 二维强制把 0 维补成 `(1,1)`、1 维补成 `(1,N)`；超过二维被拒绝，但**报错消息有两条**——通用输入走 `matrix must be 2-dimensional`，ndarray 输入经 `__array_finalize__` 走 `shape too large to be a matrix.`。
- 分支 B 的 `data.view(cls)` 会触发 `__array_finalize__`，它还会「挤掉长度为 1 的维度」再做二维判定，这是下一讲 [u2-l4](u2-l4-ndarray-subclass-array-finalize.md) 的主角。

## 7. 下一步学习建议

- **必读下一讲 [u2-l4](u2-l4-ndarray-subclass-array-finalize.md)**：本讲反复提到的 `__array_finalize__` 是理解「matrix 如何在视图、运算后仍保二维」的关键，也是解释 `data.view(cls)` 副作用的唯一入口。
- **补充阅读 [u2-l2](u2-l2-string-parsing-convert-from-string.md)**：本讲对字符串输入只点了 `_convert_from_string`（[L16-L33](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L16-L33)），那讲会逐行讲清 `';'` 分行、`','`/空格分列、`ast.literal_eval` 与行宽校验。
- **回头验证**：用 [u1-l3](u1-l3-quickstart-and-testing.md) 介绍的 `np.matrixlib.test()` 或 `pytest numpy/matrixlib/tests/test_defmatrix.py::TestCtor` 跑一遍构造测试，对照本讲的分支说明。
