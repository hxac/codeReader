# 矩阵专用属性 T / H / I / A / A1

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 `matrix` 的五个只读属性 **T、H、I、A、A1** 各自的数学含义与返回类型；
- 解释 **H 属性「仅当 dtype 为复数类型才做共轭」** 的判定逻辑，并区分「dtype 是复数」与「某元素虚部恰好为零」这两件事；
- 理解 **I 属性按 `M == N` 在 `linalg.inv` 与 `linalg.pinv` 之间分发** 的策略，以及对奇异方阵抛 `LinAlgError`、对非方阵返回伪逆的行为；
- 看懂 **`getT = T.fget` 这类「把 property 的 getter 复用成方法」** 的兼容别名写法；
- 动手验证 **A / A1 返回的是脱壳后的 `ndarray`**（而非 `matrix`）。

## 2. 前置知识

本讲承接三篇讲义：u2-l1（`matrix.__new__` 构造与二维强制）、u2-l4（`__array_finalize__` 子类化机制）、u2-l5（`*` 重载为矩阵乘）。你需要记住三件事：

1. `matrix` 是 `ndarray` 的子类，且**始终二维**（见 [defmatrix.py:L74](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L74)，`class matrix(N.ndarray)`）。
2. `matrix` 并**没有重写 `transpose()` 与 `conjugate()`**，它们继承自 `ndarray`，返回同子类视图；新视图经 `__array_finalize__`（u2-l4）收尾后**仍是二维 `matrix`**。这正是 `.T` / `.H` 返回 `matrix` 而非 `ndarray` 的原因。
3. `matrix` 上的 `*` 是**矩阵乘**而非逐元素乘（u2-l5）。所以下文 `m.I * m` 是矩阵乘法。

再补三个直觉化的数学概念（先有直觉，再看源码）：

- **转置 \(A^\top\)**：行列互换，\((A^\top)_{ij} = A_{ji}\)。
- **共轭转置 \(A^H\)**（Hermitian 转置）：先取元素共轭再转置，\((A^H)_{ij} = \overline{A_{ji}}\)。实数的共轭是它自己，故**实矩阵 \(A^H = A^\top\)**。
- **逆 \(A^{-1}\)**：仅对方阵且非奇异存在，满足 \(A^{-1}A = AA^{-1} = I\)。方阵奇异或矩阵非方阵时不存在普通逆，需用**伪逆 \(A^+\)**（Moore-Penrose）。

## 3. 本讲源码地图

| 文件 | 本讲涉及的内容 |
| --- | --- |
| numpy/matrixlib/defmatrix.py | `matrix` 类全部实现；五个属性 `T/H/I/A/A1` 与五个 `get*` 兼容别名都在这一个文件里 |

本讲精读的代码锚点（均为 `defmatrix.py` 内）：

| 锚点 | 行号 | 作用 |
| --- | --- | --- |
| 模块导入 `N`、`isscalar` | L7-L8 | `N.ndarray`、`N.complexfloating` 等都来自这里 |
| `asmatrix` 函数 | L37 | I 属性把求逆结果包回 `matrix` |
| `I` 属性 | L798-L841 | 逆 / 伪逆分发 |
| `A` 属性 | L843-L871 | 脱壳为 `ndarray` |
| `A1` 属性 | L873-L900 | 脱壳并展平为一维 `ndarray` |
| `T` 属性 | L940-L971 | 转置（不共轭） |
| `H` 属性 | L973-L1006 | 共轭转置（按 dtype 判定） |
| `getT/getA/getA1/getH/getI` | L1008-L1013 | 兼容别名 |

## 4. 核心概念与源码讲解

### 4.1 T / H 属性：转置与共轭转置（含 complexfloating 判定）

#### 4.1.1 概念说明

`T` 与 `H` 是一对：都把矩阵「翻转」，区别只在翻不翻转复数的符号。

- `T`：纯转置，**永远不共轭**。文档字符串特意写明 `Does *not* conjugate!`。
- `H`：共轭转置。但实现里有一个**关键判定**——只有当矩阵的 dtype 是复数类型时才多做一次 `.conjugate()`，否则等价于 `.T`。

> 关键直觉：H 的「是否共轭」由 **dtype（数据类型）** 决定，而不是由「有没有元素带虚部」决定。一个 `complex128` 但虚部全为 0 的矩阵，依然会走共轭分支（只是共轭是空操作）；一个 `float64` 矩阵永远走非共轭分支。

#### 4.1.2 核心流程

`T` 的流程极其简单：

```text
m.T  →  m.transpose()  →  返回二维 matrix（行列互换）
```

`H` 的流程多一步 dtype 判定：

```text
m.H
  ├─ 若 dtype 是复数类型(complexfloating)
  │     → m.transpose().conjugate()   # 先转置，再逐元素取共轭
  └─ 否则（整数 / 浮点 / 布尔 等）
        → m.transpose()               # 退化成普通转置
```

数学上：

\[
(A^\top)_{ij} = A_{ji}, \qquad
(A^H)_{ij} = \overline{A_{ji}}
\]

对实矩阵 \(\overline{A_{ji}} = A_{ji}\)，所以 \(A^H = A^\top\)，这正是 `else` 分支省略 `.conjugate()` 的依据。

#### 4.1.3 源码精读

**`T` 属性** —— 一行委托给继承来的 `transpose()`：

```python
@property
def T(self):
    """Returns the transpose of the matrix.
    Does *not* conjugate!  For the complex conjugate transpose, use ``.H``."""
    ...
    return self.transpose()
```

见 [defmatrix.py:L940-L971](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L940-L971)：`matrix` 没有重写 `transpose`，调用的是 `ndarray.transpose()`，返回同子类视图，经 `__array_finalize__` 仍是二维 `matrix`。

**`H` 属性** —— 多了 dtype 判定：

```python
@property
def H(self):
    """Returns the (complex) conjugate transpose of `self`.
    Equivalent to ``np.transpose(self)`` if `self` is real-valued."""
    ...
    if issubclass(self.dtype.type, N.complexfloating):
        return self.transpose().conjugate()
    else:
        return self.transpose()
```

见 [defmatrix.py:L973-L1006](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L973-L1006)。逐点拆解这段判定的每一个细节：

- `self.dtype.type`：dtype 对象里**保存的是「标量类型类」**，例如 `float64`、`complex128`，而非字符串。对一个 `complex128` 矩阵，`self.dtype.type` 就是 `numpy.complex128`。
- `N.complexfloating`：这是 `numpy._core.numeric`（模块顶部 `import numpy._core.numeric as N`，见 [defmatrix.py:L7-L8](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L7-L8)）里定义的**所有复数浮点类型的共同基类**（涵盖 `complex64` / `complex128` / `clongdouble`）。
- `issubclass(self.dtype.type, N.complexfloating)`：用标准的「子类判定」来回答「这个矩阵是不是复数类型」。整数、浮点、布尔都会返回 `False`，从而落入 `else` 分支。
- **判定是 dtype 级别，不是数值级别**：所以 `np.matrix([1+0j, 2+0j])`（复数 dtype、虚部全零）也会走 `transpose().conjugate()` 分支，只是共轭不改变数值；而 `np.matrix([1, 2])`（float64）走 `else`。

> 名字由来：`H` 取自 Hermitian（厄米），\(A^H\) 在线性代数教材里就是共轭转置的通用记号。

#### 4.1.4 代码实践

1. **实践目标**：用实矩阵与复矩阵各跑一次 `.T` / `.H`，亲眼看 H 的行为差异。
2. **操作步骤**（示例代码，可直接运行）：

```python
import numpy as np

# 实矩阵：H 与 T 完全一致
rm = np.matrix([[1, 2], [3, 4]])
print("real.T:\n", rm.T)
print("real.H:\n", rm.H)
print("real T == H ?", np.all(rm.T == rm.H))   # 预期 True

# 复矩阵：H 会取共轭，T 不会
cm = np.matrix([[1+2j, 3-1j], [0+1j, 2+0j]])
print("complex.T:\n", cm.T)
print("complex.H:\n", cm.H)
print("complex T == H ?", np.all(cm.T == cm.H))   # 预期 False
print("H == conj(T) ?", np.all(cm.H == np.conjugate(cm.T)))  # 预期 True
```

3. **需要观察的现象**：实矩阵 `T == H`；复矩阵 `T != H`，且 `H` 的每个元素正好是 `T` 对应元素的共轭。
4. **预期结果**：实矩阵两行 `True`；复矩阵 `False` 与 `True`。
5. 进阶：构造一个 `complex` dtype 但虚部全零的矩阵 `np.matrix([1+0j, 2+0j])`，打印 `m.H is` 与 `m.T` 是否数值相等，并对照源码解释「它走的是哪一个分支」。

#### 4.1.5 小练习与答案

**练习 1**：对一个布尔/整数矩阵调用 `.H` 会发生共轭吗？
> **答案**：不会。`issubclass(np.bool_/np.int64, N.complexfloating)` 为 `False`，落入 `else` 分支，等价于 `.T`。

**练习 2**：`np.matrix([[1+2j]]).H` 等于多少？
> **答案**：等于 `matrix([[1-2j]])`。复数 \(1+2j\) 的共轭是 \(1-2j\)，且 1×1 矩阵转置后仍是自身。

---

### 4.2 I 属性：逆（inv）与伪逆（pinv）的分发

#### 4.2.1 概念说明

`I` 是最「重」的属性：它**真正调用线性代数求解器**来算矩阵的逆。`matrix` 的设计是：

- **方阵**（行数 `M` == 列数 `N`）：用 `numpy.linalg.inv` 算严格意义上的逆；矩阵奇异时抛 `numpy.linalg.LinAlgError`。
- **非方阵**（`M != N`）：用 `numpy.linalg.pinv` 算 **Moore-Penrose 伪逆**，永不抛错（伪逆总是存在）。

返回值统一再被 `asmatrix(...)` 包回 `matrix` 类型（保留二维）。

> 为什么区分方阵 / 非方阵？因为数学上只有方阵才谈「严格逆」。非方阵没有严格逆，但总存在唯一的伪逆 \(A^+\)，它满足四条 Moore-Penrose 条件：
>
> \[
> AA^+A = A,\quad A^+AA^+ = A^+,\quad (AA^+)^H = AA^+,\quad (A^+A)^H = A^+A
> \]

#### 4.2.2 核心流程

```text
m.I
  ├─ 取 M, N = m.shape
  ├─ if M == N:  func = numpy.linalg.inv     # 严格逆，奇异则抛 LinAlgError
  │   else:      func = numpy.linalg.pinv    # 伪逆，总成功
  └─ return asmatrix(func(m))                # 结果包回 matrix（二维）
```

注意：`func` 是**延迟导入**（`from numpy.linalg import inv as func` 写在函数体内）。好处是只有真正调用 `.I` 时才触发 `numpy.linalg` 的导入，避免子包初始化时的循环/额外开销。

#### 4.2.3 源码精读

```python
@property
def I(self):  # noqa: E743
    """Returns the (multiplicative) inverse of invertible `self`.
    ...
    Raises
    ------
    numpy.linalg.LinAlgError: Singular matrix
        If `self` is singular.
    ...
    """
    M, N = self.shape
    if M == N:
        from numpy.linalg import inv as func
    else:
        from numpy.linalg import pinv as func
    return asmatrix(func(self))
```

见 [defmatrix.py:L798-L841](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L798-L841)。几个细节值得圈出：

- **`# noqa: E743`**：pycodestyle 的 E743 规则警告「歧义函数名」——大写字母 `I` 容易和数字 `1`、小写 `l` 混淆。这里用注释显式抑制该告警，因为 `I` 取自数学里的单位矩阵/逆记号，是刻意为之。
- **`M, N = self.shape`**：matrix 恒二维，`shape` 必有两个元素。
- **`M == N` 选 `inv`，否则 `pinv`**：这就是本属性的核心分发。注意它**不检查方阵是否奇异**——`inv` 内部遇到奇异矩阵会自己抛 `LinAlgError`，正好对应文档里的 `Raises`。
- **`asmatrix(func(self))`**：`inv` / `pinv` 返回的是普通 `ndarray`，用模块级 `asmatrix`（见 [defmatrix.py:L37](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L37)，等价 `matrix(data, copy=False)`）把结果**零拷贝包回 `matrix`**，从而 `m.I` 的类型仍是 `matrix`。

#### 4.2.4 代码实践

1. **实践目标**：验证方阵逆（`inv` 分支）与非方阵伪逆（`pinv` 分支）的行为差异。
2. **操作步骤**（示例代码）：

```python
import numpy as np

# (a) 可逆方阵：走 inv 分支，m.I * m 近似单位阵
m = np.matrix([[1, 2], [3, 4]])
print(type(m.I))                       # 预期 <class 'numpy.matrix'>
print(np.allclose(m.I * m, np.eye(2))) # 预期 True（注意 * 是矩阵乘）
print(np.allclose(m.I, np.linalg.inv(m)))  # 预期 True

# (b) 奇异方阵：inv 内部抛 LinAlgError
s = np.matrix([[1, 2], [2, 4]])        # 两行成比例，奇异
try:
    s.I
except np.linalg.LinAlgError as e:
    print("奇异方阵抛错:", e)            # 预期 "Singular matrix"

# (c) 非方阵：走 pinv 分支，不抛错，且满足 Moore-Penrose 条件
ns = np.matrix([[1, 2, 3], [4, 5, 6]]) # 2x3，M != N
pi = ns.I
print(type(pi), pi.shape)              # 预期 matrix (3, 2)
print(np.allclose(pi, np.linalg.pinv(ns)))        # 预期 True —— 证明走的是 pinv
print(np.allclose(ns * pi * ns, ns))               # 预期 True —— MP 条件 1
```

3. **需要观察的现象**：(a) `m.I * m` ≈ 单位阵；`m.I` 与 `np.linalg.inv(m)` 完全一致。(b) 奇异方阵抛 `LinAlgError`。(c) 非方阵不抛错，且 `ns.I` 与 `np.linalg.pinv(ns)` 数值相等（这是「走了 pinv 分支」的最直接证据）。
4. **预期结果**：三段分别打印 `True`、`Singular matrix`、`True`。
5. 若你本地 numpy 版本对 `m.I * m` 与单位阵的数值误差敏感，可用 `np.allclose` 而非 `==` 比较（浮点逆元几乎不可能精确）。

#### 4.2.5 小练习与答案

**练习 1**：为什么非方阵的 `.I` 不会抛 `LinAlgError`？
> **答案**：因为 `M != N` 时走 `pinv` 分支，而 `numpy.linalg.pinv` 对任意矩阵都返回唯一的伪逆，不存在「无解」情形。

**练习 2**：`m.I` 的返回类型是什么？为什么是这个类型？
> **答案**：是 `matrix`。因为最后一步 `return asmatrix(func(self))` 把 `inv`/`pinv` 返回的 `ndarray` 用 `asmatrix` 包回了 `matrix`。

**练习 3**：若把 `asmatrix(func(self))` 改成 `func(self)`，会破坏 matrix 的哪条不变量？
> **答案**：会破坏「matrix 的运算结果仍是 matrix」这一约定——`.I` 将返回普通 `ndarray`，后续 `.T` / `*` 的矩阵语义也会随之丢失。

---

### 4.3 A / A1 属性：脱壳为 ndarray

#### 4.3.1 概念说明

`A` 与 `A1` 解决的是**反方向**的需求：前面 T/H/I 都在「保持 matrix 身份」，而 A/A1 则**主动把 matrix 脱壳回普通 `ndarray`**。

- `A`：返回与自身同形状的 `ndarray`（文档写「Equivalent to `np.asarray(self)`」）。
- `A1`：返回展平后的**一维** `ndarray`（文档写「Equivalent to `np.asarray(x).ravel()`」）。

这与 matrix 重写的 `ravel()`（u3-l4 会讲，返回 `(1, N)` 二维 matrix）有本质区别：`A1` 才是「真正降到一维」的出口。

#### 4.3.2 核心流程

```text
m.A   →  m.__array__()              →  二维 ndarray（同形状）
m.A1  →  m.__array__().ravel()      →  一维 ndarray（长度 = 元素总数）
```

`__array__()` 是 `ndarray` 协议方法：任何对象实现它就能被 `np.asarray` 识别为「可转成 ndarray 的东西」。对 matrix 实例调用继承来的 `__array__()`，会返回一份**脱去 matrix 子类壳的 base `ndarray`**（这正是 `np.asarray(self)` 内部所依赖的机制）。

#### 4.3.3 源码精读

**`A` 属性**：

```python
@property
def A(self):
    """Return `self` as an `ndarray` object.
    Equivalent to ``np.asarray(self)``."""
    ...
    return self.__array__()
```

见 [defmatrix.py:L843-L871](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L843-L871)。

**`A1` 属性**：

```python
@property
def A1(self):
    """Return `self` as a flattened `ndarray`.
    Equivalent to ``np.asarray(x).ravel()``."""
    ...
    return self.__array__().ravel()
```

见 [defmatrix.py:L873-L900](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L873-L900)。注意 `A1` = `A` 的结果再 `.ravel()`，但它在实现上并没有调用 `self.A`，而是直接 `self.__array__().ravel()`——两者等价，少一次属性查找。

> 内存提示：`__array__()` 通常返回与原矩阵**共享内存的 base `ndarray` 视图**，因此修改 `m.A` 的元素可能影响 `m` 本身。这一点可作为实践中的进阶观察项。

#### 4.3.4 代码实践

1. **实践目标**：确认 `A` / `A1` 返回 `ndarray`（而非 `matrix`），并看清形状。
2. **操作步骤**（示例代码）：

```python
import numpy as np

m = np.matrix(np.arange(12).reshape(3, 4))
print(type(m.A))   # 预期 <class 'numpy.ndarray'>
print(m.A.shape)   # 预期 (3, 4)

print(type(m.A1))  # 预期 <class 'numpy.ndarray'>
print(m.A1.shape)  # 预期 (12,)   —— 真正的一维

# 对比：matrix 自己的 ravel 返回 (1, N) 二维 matrix（u3-l4）
print(type(m.ravel()), m.ravel().shape)  # 预期 <class 'numpy.matrix'> (1, 12)
```

3. **需要观察的现象**：`type(m.A)` 与 `type(m.A1)` 都打印 `numpy.ndarray`；`m.A1.shape` 是一维 `(12,)`，而 `m.ravel()` 仍是二维 `(1, 12)` 的 matrix。
4. **预期结果**：如上注释所示。
5. 进阶：用 `np.may_share_memory(m, m.A)` 检验 `A` 是否与原矩阵共享内存（**待本地验证**，取决于 numpy 版本）。

#### 4.3.5 小练习与答案

**练习 1**：`m.A1` 和 `m.ravel()` 在返回类型和形状上有什么不同？
> **答案**：`A1` 返回**一维 `ndarray`**（shape `(N,)`）；`ravel()` 返回**二维 `matrix`**（shape `(1, N)`）。前者脱壳且降维，后者保型。

**练习 2**：为什么不写成 `return np.asarray(self)` 而用 `self.__array__()`？
> **答案**：二者等价——`np.asarray` 内部正是调用对象的 `__array__()` 协议来取得底层数组。直接调 `self.__array__()` 少一层函数调用，且语义更直白：取出 matrix 内部的 ndarray 视图。

---

### 4.4 getT / getH / getI / getA / getA1 兼容别名

#### 4.4.1 概念说明

`matrix` 的 API 早期是**方法风格**（`m.getT()`、`m.getI()` …），后来 numpy 统一改用**属性风格**（`m.T`、`m.I` …）。为了不破坏老代码，源码在类体末尾保留了一组别名，把每个属性的 getter **复用成同名方法**。

#### 4.4.2 核心流程

```text
T = property(...)          # 定义 property，其 .fget 就是 getter 函数对象
getT = T.fget              # 把 getter 函数对象赋给名字 getT
                           # 于是 m.getT() 等价于 (T 的 getter)(m) == m.T
```

关键在于：`property` 对象有一个 `.fget` 属性，**就是当初传给 `property` 的那个 getter 函数**。把它直接赋值给类属性 `getT`，`getT` 就成了一个普通的实例方法（getter 的第一个参数本来就是 `self`），因此 `m.getT()` 能正常工作并与 `m.T` 返回完全相同的对象。

#### 4.4.3 源码精读

```python
# kept for compatibility
getT = T.fget
getA = A.fget
getA1 = A1.fget
getH = H.fget
getI = I.fget
```

见 [defmatrix.py:L1008-L1013](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L1008-L1013)。注释 `# kept for compatibility` 直白说明了存在理由。

逐点说明：

- **`T.fget` 是什么**：`T` 是上面用 `@property` 装饰得到的 `property` 对象；`property.fget` 指向其 getter 函数（即 `def T(self): return self.transpose()` 这个函数对象）。
- **为什么 `getT` 能当方法用**：把函数对象赋给类属性后，它就成了普通的实例方法；调用 `m.getT()` 时，Python 的方法绑定机制会把 `m` 作为第一个参数 `self` 传入，恰好匹配 getter 的签名 `T(self)`。
- **等价性**：因此 `m.getT() is m.T`（不仅是相等，是**同一个返回值**，因为走的是同一个 getter）；`getI` / `getH` / `getA` / `getA1` 同理。文档示例里 `m.getI()`（见 [defmatrix.py:L828-L831](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L828-L831)）和 `m.I` 完全等价。
- **顺序无关**：这段别名写在 `H` 属性定义之后（L1013 用到 `I.fget`，而 `I` 在 L798 已定义），所以引用时各 property 都已存在，不会 `NameError`。

#### 4.4.4 代码实践

1. **实践目标**：验证 `m.getT()` 与 `m.T` 返回同一个对象，并理解 `.fget` 复用机制。
2. **操作步骤**（示例代码）：

```python
import numpy as np

m = np.matrix([[1, 2], [3, 4]])

# getT() 与 .T 走同一个 getter，返回同一对象
print(m.getT() is m.T)          # 预期 True
print(np.all(m.getI() @ m.I == m.I @ m.getI()))  # 预期 True（同一逆矩阵）

# 直接对照 .fget 复用
print(np.matrix.getT is np.matrix.T.fget)  # 预期 True
print(np.matrix.getI is np.matrix.I.fget)  # 预期 True
```

3. **需要观察的现象**：`m.getT() is m.T` 为 `True`，说明两者是同一对象；`np.matrix.getT` 与 `np.matrix.T.fget` 是同一个函数对象。
4. **预期结果**：三行均 `True`。
5. 进阶：自己定义一个玩具类，写一个 `@property`，再用 `getX = X.fget` 复用，调用 `obj.getX()` 验证这套写法对你自己的类也成立。

#### 4.4.5 小练习与答案

**练习 1**：`m.getT()` 和 `m.T` 哪个更快？为什么？
> **答案**：基本无差别。`getT` 就是 `T` 的 getter 函数本身，两者执行的是同一段代码；唯一区别是 `getT()` 多一次方法调用 + 圆括号，可以忽略不计。新代码应统一用 `m.T`。

**练习 2**：如果把 `getI = I.fget` 这一行删掉，会破坏什么？
> **答案**：会破坏向后兼容——所有调用 `m.getI()` 的老代码都会抛 `AttributeError`，但 `m.I` 不受影响。这正是「kept for compatibility」的含义。

---

## 5. 综合实践

把本讲五个属性串起来，完成下面这个贯穿任务（合并 spec 要求的四项验证）。

**任务**：对实矩阵、复矩阵、可逆方阵、非方阵各构造一个 `matrix`，一次性验证 H 的实/复差异、I 的 inv/pinv 分发、A/A1 的脱壳类型。

```python
import numpy as np

# 1) 实矩阵 vs 复矩阵的 .H 行为差异
rm = np.matrix([[1.0, 2.0], [3.0, 4.0]])
cm = np.matrix([[1+2j, 3-1j], [0+1j, 2+0j]])
assert np.all(rm.H == rm.T),                  "实矩阵 H 应等于 T"
assert not np.all(cm.H == cm.T),              "复矩阵 H 应不等于 T"
assert np.all(cm.H == np.conjugate(cm.T)),    "复矩阵 H 应等于 conj(T)"
print("[1] H 判定 OK")

# 2) 可逆方阵 m.I * m 近似单位阵（走 inv 分支）
m = np.matrix([[1.0, 2.0], [3.0, 4.0]])
assert np.allclose(m.I * m, np.eye(2)),       "方阵逆：m.I * m ≈ I"
assert np.allclose(m.I, np.linalg.inv(m)),    "证明走的是 inv"
print("[2] inv 分支 OK")

# 3) 非方阵 m.I 走 pinv 分支
ns = np.matrix([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])
assert ns.shape[0] != ns.shape[1],            "确认非方阵"
assert np.allclose(ns.I, np.linalg.pinv(ns)), "证明走的是 pinv"
assert np.allclose(ns * ns.I * ns, ns),       "Moore-Penrose 条件"
print("[3] pinv 分支 OK")

# 4) A / A1 返回 ndarray 而非 matrix
assert type(m.A)  is np.ndarray,              "A 应是 ndarray"
assert type(m.A1) is np.ndarray,              "A1 应是 ndarray"
assert m.A.shape  == (2, 2)
assert m.A1.shape == (4,)
print("[4] A/A1 脱壳 OK")

# 5) 额外：getT/getH/getI 与属性等价
assert m.getT() is m.T
assert np.allclose(m.getI(), m.I)
print("[5] 兼容别名 OK")

print("全部通过")
```

**自检要点**：

- 若第 2 步 `m.I * m` 的 `*` 被你误当成逐元素乘，结果会错——回顾 u2-l5 的运算符重载。
- 第 3 步是「证明走了 pinv 分支」最干净的方式：拿结果和 `np.linalg.pinv` 直接比对。
- 把第 1 步的复矩阵 dtype 改成 `float64`（去掉虚部），观察断言 `[1]` 中第二条会失败——因为这改变的是 dtype，从而改变 H 的分支。

## 6. 本讲小结

- **T / H**：`T` 永不共轭；`H` 按 `issubclass(self.dtype.type, N.complexfloating)` 判定，仅复数 dtype 才多做 `.conjugate()`，实矩阵退化为转置。判定是 **dtype 级别**，与元素数值无关。
- **I**：按 `M == N` 在 `numpy.linalg.inv`（方阵，奇异抛 `LinAlgError`）与 `numpy.linalg.pinv`（非方阵，伪逆）之间分发，结果用 `asmatrix` 包回 `matrix`；`inv`/`pinv` 是函数体内延迟导入。
- **A / A1**：通过 `self.__array__()` 主动脱壳为 `ndarray`；`A1` 再 `.ravel()` 降到一维——这是 matrix 上「真正降一维」的出口，区别于返回 `(1, N)` 二维 matrix 的 `ravel()`。
- **get\* 别名**：`getT = T.fget` 把 property 的 getter 函数对象复用成方法，纯粹为向后兼容，新代码应优先用属性形式 `m.T / m.H / m.I / m.A / m.A1`。
- 所有属性都建立在 u2-l4 的 `__array_finalize__` 之上：`transpose()` / `conjugate()` 等视图经它收尾仍保持二维 `matrix`，这是「属性返回 matrix」的底层保障。

## 7. 下一步学习建议

- 接着读 **u3-l4（形状方法 tolist / squeeze / flatten / ravel）**，对比 `m.A1`（真一维 ndarray）与 `m.ravel()`（`(1,N)` 二维 matrix）的内存共享与形状差异，把「降维」这一主题看透。
- 再读 **u3-l2（归约方法与 _collapse / _align）**，理解属性之外的「保形方法族」是如何用 `keepdims` 守住二维的。
- 若想验证伪逆的数学性质，可对照 `numpy/linalg/__init__.py` 与 `numpy/linalg/_pinv.py`（**待确认路径**）阅读 `pinv` 的实现，加深对 Moore-Penrose 四条件的理解。
- 建议把本讲的断言脚本保存为本地测试，在升级 numpy 版本时回归运行——`matrix` 处于 `PendingDeprecationWarning` 维护模式，未来行为可能微调。
