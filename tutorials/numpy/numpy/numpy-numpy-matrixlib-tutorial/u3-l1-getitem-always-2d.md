# `__getitem__` 与「永远二维」的索引语义

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚为什么 `np.matrix` 必须重写 `__getitem__`，而普通 `ndarray` 不需要。
- 解释 `_getitem` 这个实例标志在 `__getitem__` 与 `__array_finalize__` 之间扮演的「握手」角色，以及它为什么必须放在 `try/finally` 里。
- 跟踪一次 `m[0]`、`m[:, 0]`、`m[0, 0]` 调用，画出从「基类取元素」到「收尾 reshape」的完整数据流。
- 根据 `index` 的结构（整数、切片、元组、列表），判定结果到底是行向量 `(1, N)`、列向量 `(N, 1)`、标量，还是保持二维的 `matrix`。

本讲是专家层的第一篇，承接 u2-l4 讲过的 `__array_finalize__` 子类化机制，把「永远二维」这条不变量具体落实到**索引**这条最容易掉维度的路径上。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 ndarray 的索引会「降维」

对普通 `ndarray`，用整数索引会消掉一个轴：

```python
a = np.array([[1, 2], [3, 4]])   # shape (2, 2)，ndim 2
a[0]        # shape (2,)，ndim 1  —— 第一行被「压扁」成一维
a[:, 0]     # shape (2,)，ndim 1  —— 第一列也被「压扁」成一维
a[0, 0]     # 标量 1，ndim 0
```

这是 ndarray 的基本索引（basic indexing）规则：**整数索引消轴，切片索引保轴**。对 `ndarray` 来说这毫无问题，因为它本就不承诺固定维度。

### 2.2 matrix 承诺「永远二维」

`np.matrix` 的核心契约是「无论怎么操作，结果都保持二维」。这对线性代数语义很自然——一行就是 `(1, N)` 的行向量，一列就是 `(N, 1)` 的列向量，单个元素在历史语义里也可以保持形状。可是上一节我们看到，基类 `ndarray.__getitem__` 天然就会把单行/单列压成一维。于是 `matrix` 必须在索引这条路径上「接管」收尾工作，把掉下去的维度补回来。

### 2.3 `__array_finalize__` 是「每次派生都跑」的收尾钩子

回顾 u2-l4：每当 NumPy 派生出一个新数组（视图、切片、copy、ufunc 输出……），都会自动调用子类的 `__array_finalize__(self, obj)`，其中 `self` 是新数组、`obj` 是派生它的父数组。`matrix` 正是借助这个钩子，把那些被压扁的中间结果重新补成二维。

那么问题来了：**既然 `__array_finalize__` 已经会补维度，为什么还要单独重写 `__getitem__`？** 这正是本讲要回答的核心问题。先记住这个悬念，我们到 4.1 再揭开。

> 关键术语速查：**基本索引（basic indexing）**= 用整数和切片触发的、返回视图的索引；**花式索引（fancy indexing）**= 用列表/数组触发的、返回副本的索引；**视图（view）**= 共享内存的新数组；**`__array_finalize__`**= 派生新数组时的收尾钩子。

## 3. 本讲源码地图

本讲只涉及两个文件，且业务代码集中在一个类里：

| 文件 | 作用 |
| --- | --- |
| [numpy/matrixlib/defmatrix.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py) | `matrix` 类定义。本讲精读 `__array_finalize__`（L172-L193）与 `__getitem__`（L195-L219）这一对「握手」方法。 |
| [numpy/matrixlib/tests/test_defmatrix.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_defmatrix.py) | 子包测试。本讲引用 `TestNewScalarIndexing`（L323-L385）中的若干用例作为行为契约。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，按「为什么需要 → 标志机制 → 短路配合 → 收尾判定」的顺序推进。

### 4.1 「永远二维」的索引难题：为什么光靠 `__array_finalize__` 不够

#### 4.1.1 概念说明

假设我们**没有**重写 `__getitem__`，只依赖 `__array_finalize__` 来补维度。那么对 `m = matrix([[1, 2], [3, 4]])`：

- `m[0]`：基类取第一行 → 得到 1 维视图 `(2,)` → `__array_finalize__` 把它补成 `(1, 2)` 行向量。✅ 看起来没问题。
- `m[:, 0]`：基类取第一列 → 同样得到 1 维视图 `(2,)` → `__array_finalize__` 也只能把它补成 `(1, 2)` 行向量。❌

问题暴露了：`__array_finalize__` 拿到的 `self` 是一个已经被压扁的 1 维数组，它**已经丢失了「我本来是一列」这个信息**——它只看到一个 `(2,)`，无从判断该补成 `(1, 2)` 还是 `(2, 1)`。于是列索引永远会被误补成行向量。

要正确区分行向量和列向量，必须知道**用户写的索引长什么样**（`m[0]` 还是 `m[:, 0]`），而这个信息只有 `__getitem__` 才拿得到。所以 `matrix` 必须重写 `__getitem__`，自己来做这件事；同时还要**阻止** `__array_finalize__` 在这次索引里抢先补维度，否则等 `__getitem__` 拿到结果时它已经是 2 维，判定逻辑根本不会触发。

这就是 `_getitem` 标志要解决的问题。

#### 4.1.2 核心流程

把两个方法看作一对协作者：

```text
用户写 m[:, 0]
        │
        ▼
matrix.__getitem__(index=(slice, 0))
   ① 把 self._getitem 置 True        ← 告诉 __array_finalize__「别插手」
   ② 调用 N.ndarray.__getitem__(self, index)
        │  (基类产生 1 维视图 (2,)，作为新 matrix 实例)
        │  └─▶ 触发 __array_finalize__(new_view, obj=self)
        │         ③ 看到 obj._getitem==True → 提前 return，不补维度
        │            于是 new_view 保持 1 维 (2,)
   ④ finally：把 self._getitem 复位 False
   ⑤ 自己根据 index 结构判定：第二轴是整数 → 这是列 → reshape((2,1))
        │
        ▼
   返回 (2,1) 列向量 matrix
```

下面三个模块分别拆解 ①④（标志）、③（短路）、⑤（判定）。

### 4.2 `_getitem` 标志与 `try/finally` 握手

#### 4.2.1 概念说明

`_getitem` 是一个**实例级、瞬态**的布尔标志，只在某一次 `__getitem__` 调用的执行窗口内为 `True`。它的唯一用途是给 `__array_finalize__` 发一个信号：「当前这次派生视图，是我 `__getitem__` 主动发起的，请你不要抢先补维度，把判定权交还给我。」

为什么必须用 `try/finally`？因为 `N.ndarray.__getitem__` 可能抛异常（比如索引越界），如果异常发生时 `_getitem` 没被复位，这个 matrix 实例就会**永久**带着 `_getitem=True` 的脏状态，后续任何对它或它派生视图的操作都会被 `__array_finalize__` 错误地短路，导致维度补不回来。`finally` 保证了无论基类取元素成功还是抛错，标志都会被清干净。

#### 4.2.2 核心流程

`__getitem__` 的「外壳」只有三步：

```text
1. self._getitem = True              # 升起旗帜
2. out = N.ndarray.__getitem__(self, index)   # 委托基类（try 保护）
3. self._getitem = False             # 无论成败都降下旗帜（finally）
```

#### 4.2.3 源码精读

设置与复位标志的代码在 `__getitem__` 开头：

[numpy/matrixlib/defmatrix.py:L195-L201](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L195-L201) —— 进入 `__getitem__` 先把 `self._getitem` 置 `True`，然后用 `try/finally` 包住基类取值，保证异常路径下也能在 `finally` 里复位为 `False`。

```python
def __getitem__(self, index):
    self._getitem = True

    try:
        out = N.ndarray.__getitem__(self, index)
    finally:
        self._getitem = False
```

注意 `out = N.ndarray.__getitem__(self, index)` 这一行显式调用的是**基类** `ndarray` 的实现，绕过了递归回 `matrix.__getitem__` 自身。这是关键：`matrix` 把「真正去内存里取元素」的脏活交给基类，自己只在结果出来之后做形状收尾。

### 4.3 `__array_finalize__` 的「索引短路」

#### 4.3.1 概念说明

回顾 u2-l4，`__array_finalize__` 的职责是把掉维的结果补回二维。但在「索引」这条路径上，我们**故意**不让它补，理由见 4.1。于是 `__array_finalize__` 的最前面两行实现了一个「短路」：

```python
self._getitem = False
if (isinstance(obj, matrix) and obj._getitem):
    return
```

这两行一起读：

1. **第一行 `self._getitem = False`**：给新派生的视图一个干净的默认值。任何一个新 matrix（无论是索引视图、运算结果、还是 copy）都从这里获得 `_getitem=False`，表示「我不是在一次 `__getitem__` 中诞生的」。
2. **第二行的 `return`**：仅当父对象 `obj` 本身是一个 `matrix`、并且它此刻正处于自己的 `__getitem__` 调用中（`obj._getitem` 为 `True`）时，**提前返回**，跳过后面所有的形状修补逻辑。这正是 4.2 里 `_getitem=True` 信号要触发的效果。

#### 4.3.2 核心流程

`__array_finalize__` 对索引视图的判定可以这样画：

```text
__array_finalize__(self=new_view, obj=parent)
    │
    ├─ self._getitem = False          # 给新视图默认标志
    │
    ├─ parent 是 matrix 且 parent._getitem==True ?
    │     ├─ 是 → return              # ★ 索引短路：不补维度，交还 __getitem__
    │     └─ 否 → 继续往下按 ndim 补维度（u2-l4 讲过的五条分支）
```

为什么条件里要有 `isinstance(obj, matrix)`？因为 `__array_finalize__` 也会被非 matrix 父对象触发（比如从纯 `ndarray` 视图得到一个 matrix 时，`obj` 可能是 `ndarray`，它根本没有 `_getitem` 属性）。加上 `isinstance(obj, matrix)` 这个短路求值前提，才能安全地访问 `obj._getitem` 而不触发 `AttributeError`。

#### 4.3.3 源码精读

[numpy/matrixlib/defmatrix.py:L172-L175](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L172-L175) —— `__array_finalize__` 的入口：先把新视图的 `_getitem` 初始化为 `False`，再判断父对象是否正处于索引中，若是则提前返回，不参与形状修补。

```python
def __array_finalize__(self, obj):
    self._getitem = False
    if (isinstance(obj, matrix) and obj._getitem):
        return
```

> 小结这条握手：`__getitem__` 升旗（`_getitem=True`）→ 基类取值产生视图 → 视图的 `__array_finalize__` 看到父对象的旗子 → 短路返回（视图保持降维）→ `__getitem__` 降旗 → 自己根据 `index` 做 reshape。`_getitem` 是两者之间**唯一**的通信信道。

### 4.4 `__getitem__` 的收尾 reshape：行向量、列向量、标量

#### 4.4.1 概念说明

短路之后，`out` 是基类返回的、未经补维的原始结果。它可能是：

| `out` 的形态 | 典型来源 | 处理方式 |
| --- | --- | --- |
| 不是 `ndarray`（numpy 标量） | `m[0, 0]`（全整数索引） | 直接返回该标量 |
| 0 维 `ndarray`（`ndim==0`） | `matrix(0)[0]`（从 `(1,1)` 取一行） | `out[()]` 取出标量 |
| 1 维 `ndarray`（`ndim==1`） | `m[0]`、`m[:, 0]` 等单行/单列 | reshape 为行 `(1, N)` 或列 `(N, 1)` |
| 2 维 `ndarray`（`ndim==2`） | `m[[0, 1]]`（花式选多行）、`m[0:2, 0:2]` | 原样返回（仍是 matrix） |

最精巧的是「1 维 → 行还是列」的判定。`__getitem__` 用两个线索决定：

- **线索一**：`index` 是不是一个长度大于 1 的序列（即用户写了形如 `m[a, b]` 的二元索引）。
- **线索二**：二元索引的**第二个分量**（列轴）是不是一个标量整数。

只有当「写了二元索引」**且**「列轴是单个整数」时，才认定用户在选**一整列**，结果 reshape 成列向量 `(N, 1)`；其余情况一律 reshape 成行向量 `(1, N)`。

#### 4.4.2 核心流程

收尾判定的伪代码：

```text
if out 不是 ndarray:        # numpy 标量
    return out               # m[0,0] → 标量

if out.ndim == 0:           # 0 维数组
    return out[()]           # 取出标量

if out.ndim == 1:            # 1 维：要判定行/列
    sh = out.shape[0]
    try:
        n = len(index)       # index 是元组才有 len
    except Exception:
        n = 0                # 整数索引没有 len → n=0
    if n > 1 and isscalar(index[1]):   # 写了二元索引且列轴是整数
        out = out.reshape((sh, 1))     # 列向量
    else:
        out = out.reshape((1, sh))     # 行向量
return out
```

用具体例子对照（`m = matrix([[1, 2], [3, 4]])`）：

| 表达式 | `index` | `n` | `index[1]` | `isscalar?` | 结果形状 |
| --- | --- | --- | --- | --- | --- |
| `m[0]` | `0`（int，无 `len`） | 0 | — | — | `(1, 2)` 行 |
| `m[:, 0]` | `(slice, 0)` | 2 | `0` | True | `(2, 1)` 列 |
| `m[0, :]` | `(0, slice)` | 2 | `slice` | False | `(1, 2)` 行 |
| `m[0, 0]` | `(0, 0)` | — | — | — | 标量（不走 reshape，因 `out` 非数组） |
| `m[[0, 1]]` | `[0, 1]`（列表） | — | — | — | `(2, 2)` 二维（`out.ndim==2`，不进 1 维分支） |

#### 4.4.3 源码精读

收尾逻辑紧跟在 `try/finally` 之后：

[numpy/matrixlib/defmatrix.py:L203-L219](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L203-L219) —— 对基类返回的 `out` 分四类收尾：非数组直接返回、0 维取标量、1 维按 `index` 结构判定行/列、2 维原样返回。

```python
if not isinstance(out, N.ndarray):
    return out

if out.ndim == 0:
    return out[()]
if out.ndim == 1:
    sh = out.shape[0]
    # Determine when we should have a column array
    try:
        n = len(index)
    except Exception:
        n = 0
    if n > 1 and isscalar(index[1]):
        out = out.reshape((sh, 1))
    else:
        out = out.reshape((1, sh))
return out
```

几处值得点出的细节：

- **`isscalar` 的导入**：`isscalar` 来自 [numpy/matrixlib/defmatrix.py:L8](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L8) 的 `from numpy._core.numeric import concatenate, isscalar`。`isscalar(0)` 为 `True`，而 `isscalar(slice(None))`、`isscalar([0, 1])` 都为 `False`——这正是区分「选一列」与「选一行/选多列」的关键。
- **`out[()]` 的含义**：对一个 0 维数组，`out[()]`（空元组索引）返回它包裹的 numpy 标量。这是 NumPy 里「拆箱 0 维数组」的标准写法。
- **`try: n = len(index) except Exception`**：这里用了一个宽泛的 `except Exception` 而不是 `except TypeError`，是为了同时兜住「整数没有 `len`」和「index 是其它不可 `len` 的对象」两种情况，统一归到 `n = 0`（即视为行向量）。

#### 4.4.4 代码实践

**实践目标**：亲手验证 `__getitem__` 的四类收尾，并用 `isinstance` 确认所有非标量结果都仍是 `matrix`，而非降级为 `ndarray`。

**操作步骤**：

1. 新建脚本 `getitem_demo.py`：

```python
# 示例代码：仅供演示 __getitem__ 的收尾规则
import warnings
import numpy as np

# 关掉每次构造都会发的 PendingDeprecationWarning，便于观察输出
warnings.simplefilter("ignore", PendingDeprecationWarning)

m = np.matrix([[1, 2], [3, 4]])

# 1. 单行索引 → 行向量
r = m[0]
assert r.shape == (1, 2), r.shape
assert isinstance(r, np.matrix)

# 2. 单列索引 → 列向量
c = m[:, 0]
assert c.shape == (2, 1), c.shape
assert isinstance(c, np.matrix)

# 3. 单元素索引 → 标量
e = m[0, 0]
assert e == 1
assert not isinstance(e, np.ndarray)   # 是 numpy 标量，不是数组

# 4. 花式选多行 → 保持二维 matrix
f = m[[0, 1], :]
assert f.shape == (2, 2)
assert isinstance(f, np.matrix)

print("全部断言通过")
```

2. 运行 `python getitem_demo.py`。

**需要观察的现象**：

- `m[0]` 打印出来是 `matrix([[1, 2]])`（注意是双层方括号，1 行 2 列），而不是 `matrix([1, 2])`。
- `m[:, 0]` 打印出来是 `matrix([[1], [3]])`（2 行 1 列）。
- `type(m[0, 0])` 是 `numpy.int64` 之类的标量类型，**不是** `numpy.matrixlib.defmatrix.matrix`。

**预期结果**：脚本输出「全部断言通过」。

**待本地验证**：若你的 NumPy 版本中 `m[0, 0]` 的精确标量类型不同（如 `numpy.int32`），断言 `not isinstance(e, np.ndarray)` 仍应成立——本实践只断言「不是数组」，不断言具体标量子类型。

#### 4.4.5 小练习与答案

**练习 1**：对 `m = np.matrix([[1, 2, 3], [4, 5, 6]])`，预测 `m[1, [0, 1, 0]]` 的形状和类型，并说明它为什么是行向量而不是列向量。

> **参考答案**：形状 `(1, 3)`，类型 `matrix`。因为 `index = (1, [0, 1, 0])`，`n = len(index) = 2 > 1`，但 `index[1] = [0, 1, 0]` 是列表，`isscalar([0, 1, 0])` 为 `False`，所以走 `else` 分支 reshape 成行向量 `(1, 3)`。这正是 test_defmatrix.py 中 `test_fancy_indexing` 验证的用例。

**练习 2**：如果把 `__array_finalize__` 里的 `if (isinstance(obj, matrix) and obj._getitem): return` 这两行整个删掉，`m[:, 0]` 会变成什么形状？为什么？

> **参考答案**：会变成 `(1, 2)` 行向量。删掉短路后，基类取列产生的 1 维视图 `(2,)` 会被 `__array_finalize__` 的 `ndim == 1` 分支补成 `(1, 2)`；于是回到 `__getitem__` 时 `out.ndim == 2`，连 1 维收尾分支都进不去，列向量信息彻底丢失。这正说明了 `_getitem` 握手不可或缺。

**练习 3**：为什么 `_getitem` 的复位要放在 `finally` 里，而不是直接写在 `out = N.ndarray.__getitem__(self, index)` 的下一行？

> **参考答案**：因为基类 `__getitem__` 可能抛异常（如索引越界 `IndexError`）。若不复位，异常抛出后该 matrix 实例的 `_getitem` 会永久停留在 `True`，之后任何从它派生的视图都会被 `__array_finalize__` 错误短路，形状再也补不回来，留下隐蔽的脏状态 bug。`finally` 保证无论成功还是异常都复位。

## 5. 综合实践

把本讲四个最小模块串起来，完成一个「索引行为对照实验」。

**任务**：对同一个底层数据，分别用 `np.ndarray` 和 `np.matrix` 包裹，系统对照它们的索引差异，并写一段文字解释每条差异背后的源码依据。

**操作步骤**：

```python
# 示例代码：ndarray 与 matrix 索引语义对照
import warnings
import numpy as np

warnings.simplefilter("ignore", PendingDeprecationWarning)

data = [[1, 2], [3, 4]]
a = np.array(data)          # 普通 ndarray
m = np.asmatrix(data)       # matrix（视图语义，无复制）

cases = [
    ("a[0]     ", lambda x: x[0]),
    ("a[:, 0]  ", lambda x: x[:, 0]),
    ("a[0, 0]  ", lambda x: x[0, 0]),
    ("a[[0,1]] ", lambda x: x[[0, 1]]),
]

for label, fn in cases:
    ra, rm = fn(a), fn(m)
    print(f"{label} | ndarray: shape={getattr(ra, 'shape', 'scalar'):<8} "
          f"| matrix: shape={getattr(rm, 'shape', 'scalar'):<8} "
          f"| matrix 类型={type(rm).__name__}")
```

**需要观察并解释的现象**（写成一段差异说明）：

1. `a[0]` 的 shape 是 `(2,)`（1 维），而 `m[0]` 的 shape 是 `(1, 2)`（2 维行向量）。差异来源：`__getitem__` 末尾的 `out.reshape((1, sh))`。
2. `a[:, 0]` 与 `a[0]` shape 完全一样（都是 `(2,)`，ndarray 无法区分行列），而 `m[:, 0]` 是 `(2, 1)`、`m[0]` 是 `(1, 2)`——matrix 能区分。差异来源：列向量判定条件 `n > 1 and isscalar(index[1])`。
3. `a[0, 0]` 与 `m[0, 0]` 都返回标量——这是两者**唯一相同**的索引语义，因为 `__getitem__` 对非数组结果直接返回。
4. `a[[0, 1]]` 与 `m[[0, 1]]` 都是 `(2, 2)`，但 `type(m[[0,1]])` 是 `matrix` 而 `type(a[[0,1]])` 是 `ndarray`——花式选多行结果天然 2 维，`__getitem__` 的 1 维分支不介入，仅靠 `__array_finalize__` 把子类型传递下来。

**预期结果**：脚本正常运行，打印出一张 shape 对照表；你能用本讲源码逐条解释每个差异。把这段差异说明写进你的学习笔记，作为「永远二维」不变量的佐证。

## 6. 本讲小结

- `matrix` 必须重写 `__getitem__`，因为基类 `ndarray.__getitem__` 会把单行/单列压成 1 维，而 `__array_finalize__` 单独无法区分该补成行还是列——只有 `__getitem__` 看得到原始 `index`。
- `_getitem` 是一个实例级、瞬态的布尔标志，充当 `__getitem__` 与 `__array_finalize__` 之间的握手信号：`__getitem__` 升旗 → `__array_finalize__` 短路不补维 → `__getitem__` 降旗后自己 reshape。
- 标志的复位必须放在 `try/finally` 的 `finally` 块里，否则基类取值抛异常时会留下永久脏状态。
- 收尾分四类：非数组直接返回、0 维用 `out[()]` 取标量、1 维按 `index` 结构判行/列、2 维原样返回。
- 列向量的判定条件是 `len(index) > 1 and isscalar(index[1])`——即「写了二元索引且列轴是单个整数」。
- 所有非标量索引结果都保持 `matrix` 类型，不会降级为 `ndarray`，这条性质由 `__array_finalize__` 传递子类型 + `__getitem__` 原地 reshape 共同保证。

## 7. 下一步学习建议

本讲把「永远二维」落实到了**索引**路径。建议接着学：

- **u3-l2 归约方法与 `_collapse` / `_align`**：归约（`sum`/`mean`/`max` 等）是另一条容易掉维的路径，看看 `matrix` 如何用 `keepdims=True` 配合 `_collapse`/`_align` 实现类似的「保二维 + 保朝向」语义，与本讲的 `reshape` 收尾形成对照。
- **u3-l3 矩阵属性 `T`/`H`/`I`/`A`/`A1`**：这些只读属性同样要返回正确朝向的 matrix 或裸 ndarray，理解它们时会再次用到本讲的「行/列朝向」直觉。
- **回头重读 u2-l4**：本讲的 `_getitem` 短路是 `__array_finalize__` 五条分支之外的「第六条隐式分支」，把两讲合起来读，你就能完整画出 matrix 的维度守护机制。

阅读源码时，建议把 [defmatrix.py 的 L172-L219](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L172-L219) 这 48 行作为一个整体来理解——它们是一个不可拆分的协作单元。
