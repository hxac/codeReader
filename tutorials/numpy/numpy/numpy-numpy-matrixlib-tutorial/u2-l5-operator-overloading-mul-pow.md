# 运算符重载：`*` 改为矩阵乘法、`**` 改为矩阵幂

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `np.matrix` 的 `*` 与普通 `ndarray` 的 `*` 在语义上的本质区别（矩阵乘 vs 逐元素乘）。
- 逐行读懂 `matrix.__mul__` 的三条分支，理解它为什么对 `ndarray`/`list`/`tuple` 先做 `asmatrix` 再 `dot`，而对标量直接 `dot`。
- 解释 `__rmul__`（反向乘）与 `__imul__`（就地乘 `*=`）的实现技巧，尤其是 `self[:] = ...` 如何保住对象的 `matrix` 类型。
- 说明 `__pow__` 如何把 `**` 委托给 `numpy.linalg.matrix_power`，以及为何 `m ** -1` 表示求逆。
- 解释 `__rpow__` 返回 `NotImplemented` 这一**显式拒绝**的设计，从而明白为什么 `2 ** m` 会抛 `TypeError`。

## 2. 前置知识

在进入源码前，先建立三点直觉。

**第一，Python 的二元运算符是「双向尝试」的。** 当你写 `a * b`，Python 会先调用 `a.__mul__(b)`；如果它返回内置常量 `NotImplemented`（注意：这只是一个哨兵对象，不是异常），Python 才会回头调用 `b.__rmul__(a)`。如果两个都返回 `NotImplemented`，Python 就抛 `TypeError: unsupported operand type(s)`。`**` 运算符走的是 `__pow__` / `__rpow__`，规则相同。理解这条「双向尝试 + NotImplemented 协议」是读懂本讲全部四个方法的关键。

**第二，`ndarray` 的 `*` 是逐元素乘（element-wise product，又叫 Hadamard 积），`matrix` 的 `*` 是矩阵乘（matrix product）。** 这是 `matrix` 子类最著名、也最「危险」的语义改写：同一个 `*` 符号，换了数据类型含义就变了。NumPy 2.x 之后官方推荐用普通 `ndarray` 配合 `@`（`matmul`）运算符做矩阵乘，正是因为 `matrix` 的这种符号歧义容易引入难以察觉的 bug。

**第三，`np.matrix` 是 `ndarray` 的子类。** 它在 [defmatrix.py:117](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L117) 设置了 `__array_priority__ = 10.0`，在二元 ufunc 运算中通常「赢过」普通 `ndarray`。但本讲的 `*` / `**` 是通过**显式重写特殊方法**实现的，会直接走 `matrix.__mul__` / `matrix.__pow__`，绕过 ufunc 与优先级机制——这一点我们在 4.1 节会再强调。如果你对 `__array_finalize__` 与 `__array_priority__` 还不熟，建议先读上一讲「u2-l4 ndarray 子类化机制」。

矩阵乘的数学定义：设 \(A\) 是 \(M \times K\) 矩阵，\(B\) 是 \(K \times N\) 矩阵，则乘积 \(C = AB\) 是 \(M \times N\) 矩阵，其中

\[
C_{ij} = \sum_{k=1}^{K} A_{ik}\, B_{kj}.
\]

关键约束是**内维必须匹配**（\(A\) 的列数等于 \(B\) 的行数），否则报错——这正是 `matrix *` 与逐元素 `*` 最直观的差异。

## 3. 本讲源码地图

本讲只涉及一个源码文件，但会引用其中多个片段和一个测试文件：

| 文件 / 片段 | 作用 |
| --- | --- |
| [defmatrix.py:221-244](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L221-L244) | 四个运算符方法 `__mul__` / `__rmul__` / `__imul__` / `__pow__` / `__ipow__` / `__rpow__`，本讲主角。 |
| [defmatrix.py:7-13](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L7-L13) | 顶部导入：`N`（即 `numpy._core.numeric`）、`isscalar`，以及从 `numpy.linalg` 引入的 `matrix_power`。 |
| [defmatrix.py:36-70](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L36-L70) | `asmatrix`：`__mul__` 内部用它把「数组形态」的右操作数包装成二维 matrix。 |
| [tests/test_defmatrix.py:216-272](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_defmatrix.py#L216-L272) | `TestAlgebra`：覆盖 `*`、`**`、`*=`、`**=` 及 `__rpow__`/`__mul__` 返回 `NotImplemented` 的行为。 |
| [tests/test_defmatrix.py:388-396](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_defmatrix.py#L388-L396) | `TestPower`：验证 `matrix_power` 会**保持输入子类型**（ndarray→ndarray，matrix→matrix）。 |

## 4. 核心概念与源码讲解

### 4.1 `__mul__`：把 `*` 改写成矩阵乘法

#### 4.1.1 概念说明

`matrix` 子类重写 `*` 的目的，是让线性代数写法更接近数学课本和 MATLAB：`A * B` 就是矩阵相乘。但 Python 的 `*` 在 `ndarray` 上是逐元素乘，这两种语义在符号上完全相同、结果却截然不同，是 `np.matrix` 被官方弃用的核心原因之一。

`matrix.__mul__` 用「右操作数是什么类型」来决定如何计算，一共三条分支。理解它的关键是：**只要右操作数「长得像数组」`（ndarray`/`list`/`tuple`），就先用 `asmatrix` 把它规整成二维 matrix，再交给 `np.dot` 做矩阵乘；如果是标量，就直接让 `np.dot` 处理标量缩放；其余情况显式返回 `NotImplemented`，把决定权交还 Python。**

#### 4.1.2 核心流程

`a * b`（其中 `a` 是 matrix）的执行流程：

```
matrix.__mul__(a, b)
├─ b 是 (ndarray, list, tuple)?
│   └─ 是 → return np.dot(a, asmatrix(b))   # 把 b 升成二维 matrix 再相乘
├─ b 是标量，或 b 没有 __rmul__?
│   └─ 是 → return np.dot(a, b)             # 标量缩放
└─ 否则 → return NotImplemented              # 交还 Python（最终抛 TypeError）
```

两个细节值得注意：

1. **`asmatrix(b)` 把一维数组升成行向量。** `asmatrix([1, 1])` 等价于 `matrix([1,1])`，由于上一讲学过的「二维强制」，形状会被补成 `(1, 2)`。所以 `matrix * 一维数组` 时，那个一维数组被当成**行向量**参与 `np.dot`。这正是源码注释 `# This promotes 1-D vectors to row vectors` 的含义。
2. **`np.dot` 对两个二维数组做矩阵乘。** 当 `a`、`b` 都是二维时，`np.dot` 退化为标准的矩阵乘法，等价于 `a @ b`，并按内维匹配做校验。

#### 4.1.3 源码精读

[defmatrix.py:221-227](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L221-L227) 是 `__mul__` 的全部实现，只有 7 行：

```python
def __mul__(self, other):
    if isinstance(other, (N.ndarray, list, tuple)):
        # This promotes 1-D vectors to row vectors
        return N.dot(self, asmatrix(other))
    if isscalar(other) or not hasattr(other, '__rmul__'):
        return N.dot(self, other)
    return NotImplemented
```

配套的导入在文件顶部：[defmatrix.py:7-8](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L7-L8) 把 `numpy._core.numeric` 起别名为 `N`，并从中导出 `isscalar`：

```python
import numpy._core.numeric as N
from numpy._core.numeric import concatenate, isscalar
```

而 [defmatrix.py:36-70](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L36-L70) 定义的 `asmatrix` 最后一行 `return matrix(data, dtype=dtype, copy=False)`，说明它返回的就是一个共享内存（不复制）的二维 `matrix` 视图。

逐行解释：

- 第一条 `if`：右操作数是 `ndarray`（含其子类，自然也含 `matrix`）、`list` 或 `tuple` 时，先 `asmatrix(other)` 规整成二维，再 `N.dot(self, ...)`。注意 `matrix` 本身也是 `ndarray` 的子类，所以 `matrix * matrix` 同样走这条分支。
- 第二条 `if`：`isscalar(other)` 判定标量（如 `3`、`3.0`），或者 `other` 连 `__rmul__` 都没有（说明它根本不参与乘法协议），就直接 `N.dot(self, other)`——`np.dot` 遇到标量会做整体缩放，结果相当于 `self * 3`。
- 兜底 `return NotImplemented`：对于 `object()` 这种「既不是数组也不是标量、却定义了 `__rmul__`」的对象，显式拒绝，让 Python 去尝试对方的 `__rmul__`，若仍不行则抛 `TypeError`。

> **与上一讲的衔接**：因为 `matrix` **显式定义**了 `__mul__`，`matrix * matrix` 会直接调用本方法，**不会**经过 ufunc 机制，因此 `__array_priority__` 在这里并不参与裁决——优先级裁决的是没有显式重写特殊方法的 ufunc 运算。

#### 4.1.4 代码实践

**实践目标**：亲眼看 `matrix` 的 `*` 与 `ndarray` 的 `*` 在相同数据上产生完全不同的结果。

**操作步骤**（保存为 `mul_compare.py` 后用 `python mul_compare.py` 运行）：

```python
import numpy as np

mA = np.matrix([[1, 2], [3, 4]])
v = np.matrix([1, 1]).T          # (2,1) 列向量
print("matrix * 列向量 =\n", mA * v)

A = np.array([[1, 2], [3, 4]])
v1d = np.array([1, 1])           # 一维
print("ndarray * 一维向量 =\n", A * v1d)
```

**需要观察的现象**：

- `mA * v` 得到 `(2,1)` 的列向量，值是 `[[3], [7]]`——这正是矩阵—向量积。
- `A * v1d` 得到 `(2,2)` 的数组，值是 `[[1, 2], [3, 4]]`——`v1d` 被**广播**到每一行，做的是逐元素乘。

**预期结果**：

```
matrix * 列向量 =
 [[3]
 [7]]
ndarray * 一维向量 =
 [[1 2]
 [3 4]]
```

这一对比直观说明了「同符号、不同语义」。你可以再断言一句确认形状：

```python
assert (mA * v).shape == (2, 1)
assert (A * v1d).shape == (2, 2)
```

（环境就绪时可本地运行验证；若尚未安装构建好的 NumPy，标注「待本地验证」。）

#### 4.1.5 小练习与答案

**练习 1**：`np.matrix([[1, 2], [3, 4]]) * [1, 1]`（右操作数是 Python `list`）会得到什么？为什么？

**参考答案**：会**抛错**（`ValueError: shapes (2,2) and (1,2) not aligned`）。因为 `[1, 1]` 命中第一条分支，被 `asmatrix` 升成 `(1, 2)` 的**行向量**，随后 `np.dot((2,2), (1,2))` 内维 `2 != 1`，矩阵乘不成立。这正是「一维数组被当成行向量」的副作用：想表示列向量得自己写 `.T`。

**练习 2**：为什么 `__mul__` 里要把 `matrix` 输入也归入 `isinstance(other, (N.ndarray, ...))` 这一支，而不是单列一支？

**参考答案**：因为 `matrix` 是 `ndarray` 的子类，`isinstance(m, N.ndarray)` 为真，所以天然被这一支捕获。源码无需为 `matrix` 单列分支即可正确处理，这也体现了「子类 isa 基类」在分支判断上的简化作用。

---

### 4.2 `__rmul__` 与 `__imul__`：反向乘法与就地乘法

#### 4.2.1 概念说明

这一节覆盖两个配套方法：

- **`__rmul__`（反向乘）**：当 `*` 的**左**操作数不知道怎么和 `matrix` 相乘时（最典型的就是标量 `3`，因为 `int` 不认识 `matrix`），Python 回头调用右操作数的 `__rmul__`。`matrix` 在这里实现成 `np.dot(other, self)`，使 `3 * mA` 表达「标量缩放」。
- **`__imul__`（就地乘，对应 `*=`）**：实现 `mA *= x`。它**没有**重新计算后再返回新对象，而是用 `self[:] = self * other` 把结果**写回自身**，从而保住对象的 `matrix` 类型与同一身份（`id`）。

#### 4.2.2 核心流程

**反向乘** `3 * mA`：

```
(3).__mul__(mA)   →  NotImplemented   # int 不认识 matrix
mA.__rmul__(3)    →  np.dot(3, mA)    # 标量缩放，注意参数顺序：other 在前
```

注意 `__rmul__` 里 `np.dot` 的参数顺序与 `__mul__` 相反（`other, self` 而非 `self, other`），因为反向乘法表达的是 `other * self`，要保证矩阵乘的左右顺序正确。

**就地乘** `mA *= other`：

```
__imul__:
    self[:] = self * other   # ① 右侧 self*other 走 __mul__ 得到结果
    return self              # ② 用切片赋值写回 self，保住类型与身份
```

`self[:] = ...` 是个关键技巧：它是**对已存在的 `self` 对象做元素级写入**（触发 `ndarray.__setitem__`），而不是让 `self` 这个名字指向新对象。这样无论右侧算出什么类型，`self` 始终保持 `matrix` 类型，且 `id(self)` 不变。

#### 4.2.3 源码精读

[defmatrix.py:229-234](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L229-L234) 给出两个方法的全部代码：

```python
def __rmul__(self, other):
    return N.dot(other, self)

def __imul__(self, other):
    self[:] = self * other
    return self
```

测试 [tests/test_defmatrix.py:236-240](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_defmatrix.py#L236-L240) 同时验证了反向乘与就地乘：

```python
assert_(np.allclose((3 * mA).A, (3 * A)))   # 3 * mA 走 __rmul__
mA2 = matrix(A)
mA2 *= 3                                     # 走 __imul__
assert_(np.allclose(mA2.A, 3 * A))
```

第一行 `3 * mA`：`int.__mul__` 返回 `NotImplemented`，转而调用 `mA.__rmul__(3)` = `np.dot(3, mA)`，得到标量缩放后的结果。第二、三行 `mA2 *= 3`：调用 `__imul__`，等价于 `mA2[:] = mA2 * 3`，原地改成 `3 * A`。

#### 4.2.4 代码实践

**实践目标**：验证 `3 * mA`（反向乘）与 `mA *= 3`（就地乘）的值与身份。

**操作步骤**：

```python
import numpy as np

mA = np.matrix([[1., 2.], [3., 4.]])
print("3 * mA =\n", 3 * mA)        # 反向乘，走 __rmul__

m2 = mA.copy()
old_id = id(m2)
m2 *= 3                            # 就地乘，走 __imul__
print("after *= 3 :\n", m2)
print("identity preserved:", id(m2) == old_id)
print("is still matrix:", isinstance(m2, np.matrix))
```

**需要观察的现象**：

- `3 * mA` 结果是 `[[3, 6], [9, 12]]`，即整体放大 3 倍。
- `m2 *= 3` 之后值同样放大 3 倍，且 `id(m2)` 与之前**相同**（就地修改，没有产生新对象），类型仍是 `matrix`。

**预期结果**：

```
3 * mA =
 [[ 3.  6.]
 [ 9. 12.]]
after *= 3 :
 [[ 3.  6.]
 [ 9. 12.]]
identity preserved: True
is still matrix: True
```

> 说明：`m2 *= 3` 能保住类型，全靠 `__imul__` 里的 `self[:] = ...` 写回；如果把 `self[:] = ...` 改成 `self = self * other`，`self` 只是一个局部变量重新绑定，对象的类型与身份都会丢失——你可以把这个对比作为思考点（不必改源码）。

#### 4.2.5 小练习与答案

**练习 1**：`__rmul__` 里为什么是 `np.dot(other, self)` 而不是 `np.dot(self, other)`？

**参考答案**：因为 `__rmul__(self, other)` 表达的运算是 `other * self`（反向），矩阵乘不满足交换律，顺序必须与表达式一致。若写成 `np.dot(self, other)`，`other * self` 的结果就会被错算成 `self * other`，对非交换的场景会出错。

**练习 2**：把 `__imul__` 的 `self[:] = self * other` 改成 `return self * other` 会带来什么问题？

**参考答案**：`+=` / `*=` 这类**就地**运算符的契约是「修改并返回同一对象」。改成 `return self * other` 后，虽然返回值正确，但**原对象 `self` 没有被修改**，`id` 也变了，违反就地语义；如果调用方写了 `m *= 3`，`m` 会被重新绑定到新对象，原本指望的「就地」效果丢失。

---

### 4.3 `__pow__` 与 `matrix_power`：`**` 改为矩阵幂

#### 4.3.1 概念说明

`ndarray` 的 `**` 是逐元素幂：`A ** 2` 把每个元素平方。`matrix` 的 `**` 是**矩阵幂**：`m ** 2 = m @ m`，`m ** 3 = m @ m @ m`，负指数表示先求逆再幂（`m ** -1` 即矩阵的逆）。这是 `matrix` 类继 `*` 之后的第二个核心语义改写。

实现上 `matrix.__pow__` 非常薄——只有一行，把全部工作委托给 `numpy.linalg.matrix_power`。关键特性是：`matrix_power` **保持输入的子类型**，传进去是 `matrix` 就返回 `matrix`，是普通 `ndarray` 就返回 `ndarray`。所以 `m ** k` 自然得到 `matrix`。

#### 4.3.2 核心流程

`m ** k`（`m` 是 matrix）：

```
matrix.__pow__(m, k)  →  matrix_power(m, k)
```

`matrix_power` 的约定（来自 `numpy.linalg`）：

- 要求 `m` 是**方阵**（行数 = 列数），否则报错。
- 指数 `k` 必须是**整数**（Python `int` 或 NumPy 整数标量类型），否则报 `TypeError`。
- `k == 0` 返回同阶单位阵；`k > 0` 是连乘；`k < 0` 是「逆的连乘」（先求逆，再做 `|k|` 次连乘）。
- 返回值类型跟随输入子类型。

#### 4.3.3 源码精读

[defmatrix.py:236-237](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L236-L237) 是 `__pow__` 的全部代码：

```python
def __pow__(self, other):
    return matrix_power(self, other)
```

`matrix_power` 来自文件顶部 [defmatrix.py:13](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L13) 的导入：

```python
from numpy.linalg import matrix_power
```

> 注释说明（[defmatrix.py:11-13](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L11-L13)）：`matrix_power` 虽不在 `__all__` 里，但历史上曾定义在本模块，故保留导入以兼容旧代码。

「保持子类型」这一行为有专门的测试 [tests/test_defmatrix.py:388-396](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_defmatrix.py#L388-L396)：

```python
class TestPower:
    def test_returntype(self):
        a = np.array([[0, 1], [0, 0]])
        assert_(type(matrix_power(a, 2)) is np.ndarray)   # ndarray → ndarray
        a = asmatrix(a)
        assert_(type(matrix_power(a, 2)) is matrix)        # matrix → matrix
```

`matrix_power` 内部在迭代连乘时使用 `dot` 这类保子类型的运算，因此子类型得以贯穿整个计算。也正因如此，`__pow__` 才可以一行了事，无需自己再包一层 `asmatrix`。

此外，就地幂 `__ipow__`（对应 `**=`）与 `__imul__` 完全同构，[defmatrix.py:239-241](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L239-L241)：

```python
def __ipow__(self, other):
    self[:] = self ** other
    return self
```

同样用 `self[:] = ...` 保类型、保身份。测试 [tests/test_defmatrix.py:242-253](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_defmatrix.py#L242-L253) 的 `test_pow` 同时覆盖 `**`、`**=` 与负指数（求逆）：

```python
m2 **= 2                  # 就地平方
mi **= -1                 # 就地求逆
assert_array_almost_equal(m2, m**2)
assert_array_almost_equal(np.dot(mi, m), np.eye(2))   # mi * m ≈ 单位阵
```

#### 4.3.4 代码实践

**实践目标**：验证 `m ** 3` 等于 `m * m * m`（连乘三次），并确认返回类型仍是 `matrix`。

**操作步骤**：

```python
import numpy as np

m = np.matrix([[1, 2], [3, 4]])
cube = m ** 3
manual = m * m * m

print("m ** 3 =\n", cube)
print("m * m * m =\n", manual)
print("equal:", np.array_equal(cube, manual))
print("type of m**3:", type(cube).__name__)

# 逆：m ** -1 应与 m.I 一致
print("m**-1 ≈ m.I:", np.allclose((m ** -1), m.I))
```

**需要观察的现象**：

- `m ** 3` 与 `m * m * m` 完全相等，值为 `[[37, 54], [81, 118]]`。
- `type(cube)` 是 `matrix`，证明 `matrix_power` 保住了子类型。
- `m ** -1` 与 `m.I` 数值一致（后者是「逆/伪逆」属性，下一阶段讲义会详讲）。

**预期结果**：

```
m ** 3 =
 [[37 54]
 [81 118]]
m * m * m =
 [[37 54]
 [81 118]]
equal: True
type of m**3: matrix
m**-1 ≈ m.I: True
```

（若环境未就绪，标注「待本地验证」。）

#### 4.3.5 小练习与答案

**练习 1**：`np.matrix([[1, 2], [3, 4]]) ** 2.5` 会发生什么？为什么？

**参考答案**：会抛 `TypeError`（`matrix_power` 要求整数指数）。因为 `__pow__` 直接把 `2.5` 透传给 `matrix_power`，而 `matrix_power` 内部明确校验指数必须是整数，非整数即拒绝。线性代数里「矩阵的非整数幂」需要矩阵对角化等额外机制，不在 `matrix_power` 的职责范围内。

**练习 2**：`__pow__` 为什么不像 `__mul__` 那样对 `other` 的类型做分支判断？

**参考答案**：因为 `matrix_power` 已经把「方阵校验、整数指数校验、正负幂语义、保子类型」全部封装好了，`__pow__` 只需原样转发即可；任何非法输入都会在 `matrix_power` 内部抛出合适的异常。相比 `__mul__` 需要区分数组/标量/其它三类右操作数，幂运算的右操作数只是一个标量指数，无需多分支。

---

### 4.4 `__rpow__` 与 `NotImplemented`：为什么 `2 ** matrix` 会报错

#### 4.4.1 概念说明

`__rpow__` 处理的是「反向幂」，即左操作数不是 `matrix` 的情形：`2 ** m`。在线性代数里，「标量的矩阵次幂」并没有一个像 `m ** k` 那样标准、唯一公认的定义（它涉及矩阵指数/对数等更专门的理论），因此 `matrix` 选择**显式拒绝**——让 `__rpow__` 返回 `NotImplemented`。

这里的精髓在于理解 `NotImplemented` 协议的最终后果：

```
2 ** m
├─ (2).__pow__(m)   → NotImplemented   # int 不认识 matrix
└─ m.__rpow__(2)    → NotImplemented   # matrix 也主动拒绝
→ 两个都 NotImplemented  →  Python 抛 TypeError
```

也就是说，「我（matrix）不支持被当作指数」通过返回 `NotImplemented` 表达，而不是抛异常；Python 在两端都失败后才统一抛 `TypeError`。这种写法把「是否能算」的决定权留给协议层，更优雅，也方便第三方子类扩展。

#### 4.4.2 核心流程

`base ** m`（`base` 非 matrix，如 `int`/`float`）：

1. Python 调用 `type(base).__pow__(base, m)`，例如 `int.__pow__(2, m)` → 返回 `NotImplemented`。
2. Python 回头调用 `type(m).__rpow__(m, base)` → `matrix.__rpow__` 直接返回 `NotImplemented`。
3. 两端都返回 `NotImplemented`，Python 无法处理，抛 `TypeError: unsupported operand type(s) for ** ...`。

#### 4.4.3 源码精读

[defmatrix.py:243-244](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L243-L244) 只有两行，是个「显式拒绝」：

```python
def __rpow__(self, other):
    return NotImplemented
```

测试 [tests/test_defmatrix.py:261-272](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_defmatrix.py#L261-L272) 的 `test_notimplemented` 同时验证了两处「拒绝」：

```python
def test_notimplemented(self):
    '''Check that 'not implemented' operations produce a failure.'''
    A = matrix([[1., 2.], [3., 4.]])
    # __rpow__
    with assert_raises(TypeError):
        1.0 ** A                       # __rpow__ 返回 NotImplemented → TypeError
    # __mul__ with something not a list, ndarray, tuple, or scalar
    with assert_raises(TypeError):
        A * object()                   # __mul__ 返回 NotImplemented → TypeError
```

两段合起来正好说明本讲的两个「拒绝点」：

- `1.0 ** A`：`__rpow__` 返回 `NotImplemented`，导致 `TypeError`。
- `A * object()`：`__mul__` 三条分支都不命中，走到兜底 `return NotImplemented`，对方也没有 `__rmul__` 能处理 `matrix`，最终 `TypeError`。

#### 4.4.4 代码实践

**实践目标**：亲手触发 `2 ** m` 的 `TypeError`，并定位到 `__rpow__`。

**操作步骤**：

```python
import numpy as np

m = np.matrix([[1, 2], [3, 4]])

# ① 反向幂：应当抛 TypeError
try:
    result = 2 ** m
    print("no error, result =", result)
except TypeError as e:
    print("TypeError:", e)

# ② 对照：正向幂正常工作
print("m ** 2 =\n", m ** 2)

# ③ 确认 __rpow__ 确实返回 NotImplemented 哨兵
print("__rpow__ returns:", m.__rpow__(2))
```

**需要观察的现象**：

- `2 ** m` 抛出 `TypeError`，提示 `**` 的操作数类型不支持。
- `m ** 2` 正常返回矩阵平方。
- `m.__rpow__(2)` 直接返回 `NotImplemented`（在终端里会打印成 `NotImplemented`）。

**预期结果**：

```
TypeError: unsupported operand type(s) for ** or pow(): 'int' and 'matrix'
m ** 2 =
 [[ 7 10]
 [15 22]]
__rpow__ returns: NotImplemented
```

（不同 Python 版本下 TypeError 文案可能略有差异；现象一致即可。）

#### 4.4.5 小练习与答案

**练习 1**：如果想让 `2 ** m` 返回某种结果，应该改 `__rpow__` 还是 `__pow__`？为什么不能简单删掉 `__rpow__`？

**参考答案**：应该改 `__rpow__`（赋予它一个有意义的实现）。不能简单删掉 `__rpow__`：`matrix` 继承自 `ndarray`，删除自定义的 `__rpow__` 后会回落到 `ndarray.__rpow__`，其行为未必是你想要的；显式返回 `NotImplemented` 才是「干净地拒绝、把决定权交给协议」的正确做法。

**练习 2**：`__rpow__` 返回 `NotImplemented` 与直接 `raise TypeError` 相比，有什么好处？

**参考答案**：返回 `NotImplemented` 是协议层的「我处理不了，请尝试对方」，Python 会继续尝试 `base.__pow__` 的其它路径（包括对方可能的 `__array_ufunc__`/`__array_priority__` 协商）。直接 `raise TypeError` 会**强行中断**，剥夺对方接手的机会，也不符合 Python 数据模型的惯例。`NotImplemented` 让类型系统更具可组合性。

---

## 5. 综合实践

把本讲的四个机制串起来，完成一个小任务：**用 `np.matrix` 复现一段线性代数计算，并验证每一步的语义与类型**。

设

\[
A = \begin{pmatrix} 1 & 2 \\ 3 & 4 \end{pmatrix}, \quad b = \begin{pmatrix} 1 \\ 1 \end{pmatrix}.
\]

要求：

1. 用 `np.matrix` 构造 `A` 和列向量 `b`（注意 `b` 要构造为 `(2,1)`，可用 `np.matrix([1, 1]).T`）。
2. 计算 `y = A * b`，断言 `y` 的形状是 `(2, 1)`、值是 `[[3], [7]]`，且 `type(y) is np.matrix`。
3. 计算 `P = A ** 2`，断言它等于 `A * A`，且 `type(P) is np.matrix`。
4. 用 `A ** -1` 求逆，断言 `(A ** -1) * A` 近似单位阵（用 `np.allclose(..., np.eye(2))`）。
5. 把 `A *= 2`（就地放大 2 倍）后再做第 2 步，断言 `y` 也放大 2 倍（`[[6], [14]]`），并断言 `A` 的 `id` 在 `*=` 前后不变。
6. 最后断言 `2 ** A` 抛 `TypeError`。

参考脚本（可直接运行验证）：

```python
import numpy as np

A = np.matrix([[1, 2], [3, 4]])
b = np.matrix([1, 1]).T

y = A * b
assert y.shape == (2, 1)
assert np.array_equal(y, [[3], [7]])
assert type(y) is np.matrix

P = A ** 2
assert np.array_equal(P, A * A)
assert type(P) is np.matrix

assert np.allclose((A ** -1) * A, np.eye(2))

old_id = id(A)
A *= 2
assert id(A) == old_id                 # 就地，身份不变
y2 = A * b
assert np.array_equal(y2, [[6], [14]])  # 随 A 放大 2 倍

try:
    2 ** A
    raise AssertionError("应当抛 TypeError")
except TypeError:
    pass

print("全部断言通过 ✓")
```

> 这个综合任务同时覆盖了 `__mul__`（第 2、4、5 步）、`__pow__`（第 3、4 步）、`__imul__`（第 5 步）和 `__rpow__`（第 6 步），把本讲的四个最小模块连成了一条可运行的主线。

## 6. 本讲小结

- `matrix.__mul__` 用三条分支把 `*` 改写成矩阵乘：数组形态右操作数先 `asmatrix` 再 `np.dot`，标量直接 `np.dot` 缩放，其余返回 `NotImplemented`；一维右操作数会被升成 `(1,N)` 行向量。
- `__rmul__` 实现 `other * self` 为 `np.dot(other, self)`，参数顺序与 `__mul__` 相反；`3 * mA` 正是走这条反向通道完成标量缩放。
- `__imul__` / `__ipow__` 用 `self[:] = self * other` / `self ** other` 的「切片写回」技巧，就地修改并保住 `matrix` 类型与对象身份。
- `__pow__` 一行委托给 `numpy.linalg.matrix_power`：要求方阵与整数指数，支持负指数（求逆），并保持输入子类型。
- `__rpow__` 显式返回 `NotImplemented` 拒绝「标量的矩阵次幂」，与 `int.__pow__` 同样失败后，Python 统一抛 `TypeError`——这是 `NotImplemented` 协议的标准用法。
- 因为 `matrix` 显式重写了 `__mul__`/`__pow__`，`matrix * matrix`、`m ** k` 直接走这些特殊方法，**绕过** ufunc 与 `__array_priority__` 机制。

## 7. 下一步学习建议

本讲聚焦「运算符改写」，接下来可以从两个方向继续：

- **向「保形」深入**：运算符产出的结果仍要满足「永远二维」的不变量，这依赖 `__array_finalize__` 与 `__getitem__`。建议进入「u3-l1 `__getitem__` 与永远二维的索引语义」，看 `matrix * matrix` 之后的收尾是如何被守护的。
- **向「归约与属性」铺开**：矩阵的 `sum`/`max`/`argmax` 等方法、以及 `T`/`H`/`I`/`A`/`A1` 属性，是 `matrix` 子类另一类重写。可接着读「u3-l2 归约方法与 `_collapse`/`_align`」和「u3-l3 矩阵专用属性 T/H/I/A/A1」。

源码阅读上，建议把 [defmatrix.py:221-244](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L221-L244) 这 24 行与 [numpy/_core/numeric.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_core/numeric.py) 里的 `dot`、以及 `numpy.linalg.matrix_power` 的实现对照阅读，体会「薄薄一层子类方法 + 通用底层函数」的分层设计。
