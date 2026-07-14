# 类型检查与标量判定：real/imag/is*/nan_to_num/fix

## 1. 本讲目标

本讲聚焦 `numpy/lib/_type_check_impl.py` 与 `numpy/lib/_ufunclike_impl.py` 两个文件，讲解 numpy 中一组「类型检查与标量判定」工具。学完后你应当能够：

- 区分「基于值」的逐元素判定（`iscomplex`/`isreal`）与「基于类型」的整体判定（`iscomplexobj`/`isrealobj`），不再混淆。
- 用 `real`/`imag` 安全地取出任意输入的实部、虚部，并理解其 `try/except` 兜底机制。
- 用 `isposinf`/`isneginf` 精确识别正负无穷，看懂它们「`isinf` + `signbit`」的组合原理。
- 用 `nan_to_num` 把 `inf`/`-inf`/`nan` 替换成有限值，并理解它如何对复数分别处理实虚部。
- 用 `common_type` 推断一组数组的「公共浮点类型」，看懂其精度提升表。
- 用 `mintypecode`/`typename` 在「类型字符」与「类型描述」之间互转，并知道 `fix`/`typename` 已在 NumPy 2.5 弃用。

## 2. 前置知识

在阅读本讲前，建议你已经建立以下概念（前面几讲反复出现）：

- **dtype 与类型字符**：每个 numpy 数组都有一个 `dtype`，它可以用一个字符简写，例如 `f`=单精度浮点、`d`=双精度浮点、`F`=复数单精度、`D`=复数双精度、`i`=整数、?`=布尔`。可用 `arr.dtype.char` 取到。
- **`complexfloating` 与 `inexact`**：numpy 的类型层级里，`inexact` 是所有「非精确」（浮点）类型的基类，`complexfloating` 是复数浮点的基类。`issubclass(t, complexfloating)` 是判断「是不是复数类型」的标准姿势。
- **NEP-18 dispatcher + impl 双函数写法**：本讲几乎所有公开函数都装饰了 `@array_function_dispatch(_xxx_dispatcher)`，dispatcher 只负责把参与运算的数组参数收集成元组、供 `__array_function__` 协议拦截，真正的逻辑写在被装饰的函数体里（详见 u1-l2）。
- **IEEE 754 浮点特殊值**：`inf`（正无穷）、`-inf`（负无穷）、`nan`（非数）是浮点标准里的特殊值；`nan` 的关键特性是「不自等」（`nan != nan`），所以检测它要用专门的 `isnan` 而非相等比较。
- **`signbit`**：一个判断「符号位是否被置位」的运算。`+inf` 的符号位为 0、`-inf` 的符号位为 1，这是区分正负无穷的廉价手段。

> 一个贯穿全讲的核心认知：**「值」与「类型」是两件事**。一个 `complex128` 类型的数组，即使每个元素的虚部都是 0，它的**类型**依然是复数。很多判定函数只看其中一面，混淆它们是初学者最常踩的坑。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲引用的关键函数 |
| --- | --- | --- |
| [numpy/lib/_type_check_impl.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_type_check_impl.py) | 类型检查主实现，收纳实虚部提取、值/类型判定、非有限值清洗、类型推断与字符映射 | `real`、`imag`、`real_if_close`、`iscomplex`、`isreal`、`iscomplexobj`、`isrealobj`、`nan_to_num`、`common_type`、`mintypecode`、`typename` |
| [numpy/lib/_ufunclike_impl.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_ufunclike_impl.py) | 「类 ufunc」标量运算，函数都支持 `out=` 输出参数 | `fix`、`isposinf`、`isneginf` |

两个文件之间有一条关键依赖：`_type_check_impl.py` 在文件顶部就 `from ._ufunclike_impl import isneginf, isposinf`，`nan_to_num` 内部正是用这两个函数来定位正负无穷的位置。因此本讲的模块顺序安排为：先讲 `_ufunclike_impl` 的 `isposinf`/`isneginf`/`fix`，再讲依赖它们的 `nan_to_num`。

## 4. 核心概念与源码讲解

### 4.1 实虚部提取与近实数还原：real / imag / real_if_close

#### 4.1.1 概念说明

复数 \( z = a + b\mathrm{j} \) 由实部 \( a \) 与虚部 \( b \) 组成。numpy 提供三个相关工具：

- `np.real(val)`：返回实部；若 `val` 本身就是实数，则原样返回。
- `np.imag(val)`：返回虚部；若 `val` 是实数，虚部视为 0。
- `np.real_if_close(a, tol=100)`：当复数数组的所有虚部都「接近 0」时，把它「降级」回实数数组。

这三个函数要解决的问题是：很多输入可能是 Python 标量、可能是 ndarray、也可能是第三方数组（只有 `.real`/`.imag` 属性）。`real`/`imag` 的设计目标是「**对任何长得像数组的东西都能用**」，而不强制要求输入是 ndarray。

#### 4.1.2 核心流程

`real` 与 `imag` 的实现极简，共享同一种「鸭子类型」流程：

```
def real(val):
    try:
        return val.real          # 优先走属性，覆盖 ndarray/标量/第三方数组
    except AttributeError:
        return asanyarray(val).real   # 兜底：转成 ndarray 再取 .real
```

要点：

1. **优先用属性**：只要对象有 `.real` 属性就直接返回，零拷贝、保留子类类型。
2. **属性兜底**：若对象没有 `.real`（如 Python 列表、元组），用 `asanyarray` 包一层再取。
3. `imag` 与之完全对称，只是把 `.real` 换成 `.imag`。

`real_if_close` 的流程稍长，但仍是线性逻辑：

```
def real_if_close(a, tol=100):
    a = asanyarray(a)
    if 不是复数类型: return a              # 实数直接返回
    if tol > 1: tol = finfo(type).eps * tol   # tol 是「机器 ε 的倍数」
    if all(absolute(a.imag) < tol): a = a.real  # 虚部全够小 → 取实部
    return a
```

关键约定：当 `tol > 1` 时，`tol` 被解释为「**机器 ε 的倍数**」（即相对容差）；当 `tol <= 1` 时，它被当作**绝对容差**直接使用。

#### 4.1.3 源码精读

`real` 的鸭子类型实现（注意 `try/except AttributeError` 的兜底）：

[_type_check_impl.py:122-125](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_type_check_impl.py#L122-L125) —— 先试 `val.real` 属性，失败再退回 `asanyarray(val).real`。

`imag` 与之完全对称：

[_type_check_impl.py:166-169](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_type_check_impl.py#L166-L169) —— 取虚部，结构同上。

`real_if_close` 的容差判定与降级（注意 `tol > 1` 这条分支线，它把「倍数」换算成「绝对值」）：

[_type_check_impl.py:542-551](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_type_check_impl.py#L542-L551) —— 非复数直接返回；`tol>1` 时乘以 `finfo(type_).eps` 折算；所有 `|imag| < tol` 时返回 `a.real`。

`finfo` 来自 `numpy._core.getlimits`，它给出某种浮点类型的机器常数（最大值、最小值、`eps` 等），`eps` 是「1.0 与下一个可表示浮点数之差」，约 \(2.22\times10^{-16}\)（双精度）。

#### 4.1.4 代码实践

**实践目标**：观察 `real`/`imag` 对不同输入的返回类型，并用 `real_if_close` 把「几乎为实数」的复数数组降级。

操作步骤（示例代码，可在本地 REPL 运行）：

```python
import numpy as np

# 1. 对复数数组取实虚部
a = np.array([1+2j, 3+4j])
print(np.real(a), np.imag(a))      # [1. 3.] [2. 4.]

# 2. 对纯实数输入：real 原样返回，imag 给 0
print(np.real(3.5))                # 3.5
print(np.imag(3.5))                # 0.0

# 3. 对 Python 列表（无 .real 属性）走兜底
print(np.real([1, 2, 3]))          # [1. 2. 3.]（先 asanyarray 再取 .real）

# 4. real_if_close：虚部极小则降级为实数
z = np.array([2.1 + 4e-14j, 5.2 + 3e-15j])
print(np.real_if_close(z, tol=1000))   # [2.1 5.2]（实数数组）
print(np.real_if_close(z, tol=10))     # 仍是复数（容差不够大）
```

**需要观察的现象**：

- 第 1 步返回的是浮点数组（`.real` 把复数实部抽成 float）。
- 第 2 步 `np.real(3.5)` 返回 Python float `3.5` 而非数组（因为 `3.5.real` 就是它自己）。
- 第 4 步 `tol=1000` 时虚部 \(4\times10^{-14}\) 小于 `1000 * eps ≈ 2.2e-13`，触发降级；`tol=10` 时不满足，保持复数。

**预期结果**：上述注释即为预期输出。若把 `4e-14j` 改成 `4e-13j`，`tol=1000` 也将无法降级（因为 \(4\times10^{-13} > 2.2\times10^{-13}\)）。

#### 4.1.5 小练习与答案

**练习 1**：`np.real([1, 2, 3])` 走的是 `try` 分支还是 `except` 分支？为什么？

> **答案**：走 `except AttributeError` 分支。Python 列表没有 `.real` 属性，所以 `val.real` 抛 `AttributeError`，随后 `asanyarray(val).real` 兜底。

**练习 2**：`real_if_close` 的 `tol=100` 在双精度下对应的绝对容差约为多少？

> **答案**：\(100 \times \varepsilon \approx 100 \times 2.22\times10^{-16} \approx 2.22\times10^{-14}\)。所有 \(|\text{imag}| < 2.22\times10^{-14}\) 的元素都视为「够小」。

---

### 4.2 逐元素值判定：iscomplex 与 isreal

#### 4.2.1 概念说明

`iscomplex(x)` 与 `isreal(x)` 都是**逐元素**判定，返回与 `x` 同形的**布尔数组**。它们看的是元素的**值**（value），不是 dtype：

- `iscomplex(x)`：元素「**是复数**」= 该元素的虚部不为 0。
- `isreal(x)`：元素「**是实数**」= 该元素的虚部为 0。

注意这里的命名容易误导：`iscomplex` 并不是问「元素类型是不是复数」，而是问「元素的虚部是不是非零」。于是对一个 `complex128` 数组，`1+0j` 这样的元素在 `iscomplex` 眼里是 `False`，在 `isreal` 眼里是 `True`。

#### 4.2.2 核心流程

`isreal` 的实现是一行：

```
def isreal(x):
    return imag(x) == 0
```

即「虚部等于 0」即为实数。它复用了上一节的 `imag`，因此天然支持任何能取虚部的输入。

`iscomplex` 稍复杂，因为它要分两种 dtype：

```
def iscomplex(x):
    ax = asanyarray(x)
    if 是复数浮点类型:
        return ax.imag != 0          # 复数：虚部非零才算复数
    res = zeros(ax.shape, bool)
    return res[()]                    # 非复数：全部不是复数，返回全 False
```

两条分支的依据是「非复数类型的元素根本不可能有非零虚部」，所以直接返回全 `False`（零成本）。

#### 4.2.3 源码精读

`iscomplex` 的两分支判定（注意 `issubclass(ax.dtype.type, _nx.complexfloating)` 这条类型判断）：

[_type_check_impl.py:207-211](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_type_check_impl.py#L207-L211) —— 复数类型走 `ax.imag != 0`；其余类型构造 `zeros(shape, bool)` 再 `res[()]` 还原成标量（若输入是标量）。

> 细节：`res[()]` 的作用是把 0 维数组还原成 Python/numpy 标量。当 `x` 是标量时，`zeros((), bool)` 是 0 维数组，`[()]` 取出其中的标量 `False`，使 `np.iscomplex(3.0)` 直接返回标量 `False` 而非 0 维数组。

`isreal` 的一行实现，直接委托 `imag`：

[_type_check_impl.py:262](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_type_check_impl.py#L262) —— `return imag(x) == 0`。

#### 4.2.4 代码实践

**实践目标**：直观感受「值判定」的语义，特别注意 `1+0j` 这类「类型是复数、虚部为零」的元素。

```python
import numpy as np

a = np.array([1+1j, 1+0j, 4.5, 3, 2, 2j], dtype=np.complex128)
print(np.iscomplex(a))   # [ True False False False False  True]
print(np.isreal(a))      # [False  True  True  True  True False]

# 纯实数类型数组：iscomplex 必然全 False
b = np.array([1.0, 2.5, -3.0])
print(np.iscomplex(b))   # [False False False]
print(np.isreal(b))      # [ True  True  True]
```

**需要观察的现象**：

- `1+0j` 的类型是 `complex128`，但因为虚部为 0，`iscomplex` 给 `False`、`isreal` 给 `True`。
- `2j` 虚部为 2，`iscomplex` 给 `True`、`isreal` 给 `False`。
- 纯实数类型数组上 `iscomplex` 恒为全 `False`（走快路）。

**预期结果**：上述注释即预期输出。

> ⚠️ `isreal` 对字符串/对象数组行为可能反直觉（见源码 docstring 的例子）。这是因为它本质是 `imag(x) == 0`，对字符串数组的 `imag` 会退化为字符串与 0 的比较。生产代码中应避免对非数值数组使用 `isreal`。

#### 4.2.5 小练习与答案

**练习 1**：`np.iscomplex(np.array([1+0j, 2+3j]))` 的结果是什么？为什么 `1+0j` 是 `False`？

> **答案**：结果是 `[False, True]`。因为 `iscomplex` 看的是虚部是否非零：`1+0j` 虚部为 0 故 `False`，`2+3j` 虚部为 3 故 `True`。

**练习 2**：为什么 `iscomplex` 对非复数类型数组要单独走 `zeros(...)` 快路，而不是统一写成 `imag(x) != 0`？

> **答案**：性能与正确性兼顾。非复数类型（整数、实数浮点）根本不可能有非零虚部，直接返回全 `False` 可省去构造虚部数组和逐元素比较；此外对某些非数值类型，`imag(x) != 0` 可能触发意外的类型提升或比较语义，单独短路更安全。

---

### 4.3 类型层面判定：iscomplexobj 与 isrealobj

#### 4.3.1 概念说明

与上一节「逐元素看值」不同，`iscomplexobj(x)` 与 `isrealobj(x)` 是**整体判定**：它们看的是数组（或对象）的 **dtype 类型**，返回一个**标量布尔值**（不是布尔数组）。

- `iscomplexobj(x)`：`x` 的 dtype **是复数类型**（或含复数元素）→ `True`。哪怕所有元素虚部都是 0，只要类型是复数，就返回 `True`。
- `isrealobj(x)`：`x` 的 dtype **不是复数类型** → `True`。它就是 `not iscomplexobj(x)`。

这是本讲最重要的一组对比：**`isreal`/`iscomplex` 看「值」，`isrealobj`/`iscomplexobj` 看「类型」**。混淆这两组是初学者最常踩的坑。

#### 4.3.2 核心流程

`iscomplexobj` 的逻辑：

```
def iscomplexobj(x):
    try:
        type_ = x.dtype.type          # 有 dtype：直接取类型
    except AttributeError:
        type_ = asarray(x).dtype.type  # 无 dtype：转 ndarray 再取
    return issubclass(type_, complexfloating)
```

它只问一件事：**元素的类型是不是 `complexfloating` 的子类？** 与元素的具体数值无关。

`isrealobj` 则是它的逻辑取反：

```
def isrealobj(x):
    return not iscomplexobj(x)
```

#### 4.3.3 源码精读

`iscomplexobj` 的类型提取与子类判定：

[_type_check_impl.py:299-304](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_type_check_impl.py#L299-L304) —— `try` 取 `x.dtype.type`，失败退回 `asarray(x).dtype.type`，最后 `issubclass(type_, _nx.complexfloating)`。

`isrealobj` 直接委托（注意它就是一行取反）：

[_type_check_impl.py:354](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_type_check_impl.py#L354) —— `return not iscomplexobj(x)`。

> 这两个函数在 numpy 内部被反复复用。例如下一节的 `common_type` 用 `iscomplexobj(a)` 决定是否把结果升级为复数；`nan_to_num` 用 `issubclass(xtype, _nx.complexfloating)` 决定是否对实虚部分别清洗。它们是「类型分流」的基础设施。

#### 4.3.4 代码实践

**实践目标**：把「值判定」与「类型判定」并排对比，牢记 `1+0j` 在两组函数下的不同表现。

```python
import numpy as np

x = np.array([1+0j, 2+0j])      # 类型是 complex128，但虚部全是 0

# 值判定（逐元素布尔数组）
print(np.iscomplex(x))           # [False False] —— 虚部都为 0
print(np.isreal(x))              # [ True  True] —— 虚部都为 0

# 类型判定（标量布尔）
print(np.iscomplexobj(x))        # True —— 类型是复数
print(np.isrealobj(x))           # False —— 类型是复数，所以「不是实数对象」

# 纯实数类型
y = np.array([1.0, 2.0])
print(np.iscomplexobj(y))        # False
print(np.isrealobj(y))           # True

# 对非数组对象：isrealobj 也接受，但语义是「假定数组输入」
print(np.isrealobj('A string'))  # True（字符串无复数类型）
print(np.isrealobj(None))        # True
```

**需要观察的现象**：

- 同一个 `x = [1+0j, 2+0j]`：`iscomplex` 全 `False`，但 `iscomplexobj` 是 `True`。这是「值 vs 类型」最直观的对照。
- `isrealobj` 对字符串、`None` 都返回 `True`——因为它假定输入是数组，非数组被视为「没有复数类型」。docstring 明确警告这一点。

**预期结果**：上述注释即预期输出。

#### 4.3.5 小练习与答案

**练习 1**：已知 `z = np.array([3, 1+0j, True])`（由列表构造，会被提升为 `complex128`），`iscomplexobj(z)` 与 `iscomplex(z)` 分别返回什么？

> **答案**：`iscomplexobj(z)` 返回 `True`（结果 dtype 是 `complex128`）；`iscomplex(z)` 返回 `[False, False, False]`（三个元素的虚部都是 0）。这正是「类型是复数，但值上没有非零虚部」的典型场景。

**练习 2**：为什么说「`isrealobj` 不等于 `not isreal`」？

> **答案**：二者维度不同。`isreal(x)` 返回**与 `x` 同形的布尔数组**，逐元素看虚部是否为 0；`isrealobj(x)` 返回**一个标量布尔**，只看 dtype 是不是复数。对 `np.array([1+0j])`，`isreal` 给 `[True]`，`isrealobj` 给 `False`，两者没有直接的逻辑取反关系。

---

### 4.4 类 ufunc 标量运算：fix / isposinf / isneginf

> 这一节的三个函数都来自 `_ufunclike_impl.py`。文件名 `_ufunclike` 意为「像 ufunc」，它们的共同特征是：接受 `x` 与可选的 `out=` 输出参数，可就地写入结果。

#### 4.4.1 概念说明

- `np.fix(x)`：把浮点数向零方向取整（truncate toward zero），即 `3.7→3.0`、`-2.9→-2.0`。**注意：NumPy 2.5 起弃用，官方建议改用 `np.trunc`**（更快且符合 Array API 标准）。
- `np.isposinf(x)`：逐元素判断「是不是正无穷」，返回布尔数组。
- `np.isneginf(x)`：逐元素判断「是不是负无穷」，返回布尔数组。

`isposinf`/`isneginf` 要解决的问题是：`np.isinf(x)` 只能告诉你「是不是无穷」，但分不清正负。要区分正负，最稳的办法不是比较 `x > 0`（`nan > 0` 是 `False`，可能误判），而是查**符号位** `signbit`。

#### 4.4.2 核心流程

`fix` 的实现就是一行委托（加上弃用警告）：

```
def fix(x, out=None):
    warnings.warn(...)            # 弃用提示
    return nx.trunc(x, out=out)   # 直接转交给 trunc
```

`isposinf` 与 `isneginf` 共享同一思路——「`isinf` 且符号位符合」：

```
def isposinf(x, out=None):
    is_inf = isinf(x)
    signbit = ~signbit(x)         # 取反：正数的符号位未被置位
    return logical_and(is_inf, signbit, out)

def isneginf(x, out=None):
    is_inf = isinf(x)
    signbit = signbit(x)          # 不取反：负数的符号位被置位
    return logical_and(is_inf, signbit, out)
```

两者唯一的差别是 `signbit` 是否取反（`~`）。`signbit` 返回「符号位是否为 1」：

| 输入 | `signbit` | 含义 |
| --- | --- | --- |
| `+inf` | `False` | 正 |
| `-inf` | `True` | 负 |
| `nan` | `False`（通常） | —— |

于是 `isposinf(+inf) = isinf(True) ∧ ~signbit(False→True) = True`；`isposinf(-inf) = True ∧ ~True = False`，正是所要的。

#### 4.4.3 源码精读

`fix` 的弃用警告与对 `trunc` 的委托：

[_ufunclike_impl.py:65-72](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_ufunclike_impl.py#L65-L72) —— 发出 `DeprecationWarning`，然后 `return nx.trunc(x, out=out)`。

`isposinf` 的「`isinf` + 取反符号位」组合：

[_ufunclike_impl.py:134-142](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_ufunclike_impl.py#L134-L142) —— `is_inf = isinf(x)`；`signbit = ~signbit(x)`；对复数等无符号位的类型抛 `TypeError`；否则 `logical_and(is_inf, signbit, out)`。

`isneginf` 与之对称，只是符号位不取反：

[_ufunclike_impl.py:204-212](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_ufunclike_impl.py#L204-L212) —— `signbit = signbit(x)`（不取反），`logical_and(is_inf, signbit, out)`。

> 细节：两者都在 `~signbit(x)` / `signbit(x)` 外层套了 `try/except TypeError`，因为复数类型没有符号位概念，对复数调用 `signbit` 会抛 `TypeError`，函数会把它转译成一条更友好的报错信息。这就是 docstring 里「first argument has complex values 会报错」的来源。

三个函数共用同一个 dispatcher：

[_ufunclike_impl.py:14-15](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_ufunclike_impl.py#L14-L15) —— `_dispatcher(x, out=None)` 返回 `(x, out)`，把两个参数都交给 NEP-18 协议。

#### 4.4.4 代码实践

**实践目标**：用 `isposinf`/`isneginf` 区分正负无穷，并观察 `fix` 的弃用警告。

```python
import warnings
import numpy as np

x = np.array([-np.inf, -1.0, 0.0, 1.0, np.inf, np.nan])

print(np.isposinf(x))   # [False False False False  True False]
print(np.isneginf(x))   # [ True False False False False False]
# 注意 nan 既不是 posinf 也不是 neginf

# 用 out= 就地写入（注意输出会被当作 0/1）
out = np.empty(x.shape, dtype=int)
np.isposinf(x, out=out)
print(out)              # [0 0 0 0 1 0]

# fix：向零取整（会触发 DeprecationWarning）
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    print(np.fix([2.1, 2.9, -2.1, -2.9]))   # [ 2.  2. -2. -2.]
    print("弃用警告:", w[0].category.__name__)  # DeprecationWarning
```

**需要观察的现象**：

- `np.nan` 在 `isposinf` 和 `isneginf` 下都是 `False`——它不是无穷。
- `out=out` 写入 `int` 数组时，`True/False` 被存成 `1/0`。
- `fix` 对正负数都向零靠拢（区别于 `floor` 总是向负无穷）。

**预期结果**：上述注释即预期输出。`fix` 的弃用警告在 NumPy 2.5+ 会出现；建议新代码直接用 `np.trunc`。

> 若本地 numpy 版本低于 2.5，`fix` 不会发弃用警告——这属于「待本地验证」的版本相关行为。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `isposinf` 用 `~signbit(x)` 而不是 `x > 0` 来判断正无穷？

> **答案**：因为 `nan` 的任何比较都返回 `False`，用 `x > 0` 不会误判 `nan` 为正无穷（这点恰好没问题），但对 `+0.0`/`-0.0` 这类边界值，`signbit` 能区分符号位而比较运算不能。更重要的是 `signbit` 直接读 IEEE 754 符号位，语义精确、无歧义，是与 `isinf` 组合的最稳妥方式。

**练习 2**：`np.fix(-2.9)` 和 `np.floor(-2.9)` 结果有何不同？

> **答案**：`fix(-2.9) = -2.0`（向零取整），`floor(-2.9) = -3.0`（向负无穷取整）。`fix` 等价于 `trunc`，正数 behave like `floor`、负数 behave like `ceil`。

---

### 4.5 非有限值清洗：nan_to_num

#### 4.5.1 概念说明

`np.nan_to_num` 是把数组里的「非有限值」替换成有限值的工具，常用于清洗数据后再喂给不能容忍 `inf`/`nan` 的算法。它的替换规则：

- `nan` → 默认 `0.0`（可用 `nan=` 自定义）。
- `+inf` → 默认「该 dtype 能表示的最大有限浮点数」（可用 `posinf=` 自定义）。
- `-inf` → 默认「该 dtype 能表示的最小（最负）有限浮点数」（可用 `neginf=` 自定义）。
- 对**复数 dtype**，实部与虚部**分别**应用上述规则。
- 对**非浮点类型**（整数、布尔等），不做任何替换直接返回——因为它们不可能含 `inf`/`nan`。

#### 4.5.2 核心流程

```
def nan_to_num(x, copy=True, nan=0.0, posinf=None, neginf=None):
    x = array(x, subok=True, copy=copy)      # 统一成 ndarray，保留子类
    xtype = x.dtype.type
    isscalar = (x.ndim == 0)

    if 不是 inexact（浮点）类型:
        return x[()] if isscalar else x       # 整数/布尔直接返回

    iscomplex = 是否复数类型
    dest = (x.real, x.imag) if iscomplex else (x,)   # 复数则实虚部分开处理
    maxf, minf = finfo(x.real.dtype).max, .min       # 默认上下界
    if posinf is not None: maxf = posinf
    if neginf is not None: minf = neginf

    for d in dest:
        用 copyto 把 nan 位置写成 nan（默认 0）
        用 copyto 把 +inf 位置写成 maxf
        用 copyto 把 -inf 位置写成 minf
    return x[()] if isscalar else x
```

四个关键设计：

1. **类型短路**：非浮点类型直接返回，省去无意义的工作。
2. **复数拆实虚**：把 `(x.real, x.imag)` 当成两个独立的浮点数组分别清洗，循环体对两者各跑一次。
3. **默认界值来自 `finfo`**：`_getmaxmin` 用 `finfo(t).max/.min` 给出该浮点类型的最大/最小有限值。
4. **`copyto(where=...)`**：所有替换都是「按掩码就地写入」，掩码由 `isnan`/`isposinf`/`isneginf` 给出——后两者正是上一节从 `_ufunclike_impl` 导入的函数。

#### 4.5.3 源码精读

`nan_to_num` 的主流程（注意对复数 `dest = (x.real, x.imag)` 的拆分，以及对 `inexact` 的短路）：

[_type_check_impl.py:464-487](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_type_check_impl.py#L464-L487) —— `_nx.array(x, subok=True, copy=copy)` 规整输入；非 `inexact` 提前返回；复数则 `dest=(x.real,x.imag)`；循环内三次 `copyto` 分别写 `nan`/`maxf`/`minf`。

默认上下界的获取（用 `getlimits.finfo`）：

[_type_check_impl.py:358-361](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_type_check_impl.py#L358-L361) —— `_getmaxmin(t)` 返回 `finfo(t).max, finfo(t).min`。

以及本文件顶部对 `isposinf`/`isneginf` 的导入，说明依赖关系：

[_type_check_impl.py:17](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_type_check_impl.py#L17) —— `from ._ufunclike_impl import isneginf, isposinf`，`nan_to_num` 内部第 482-483 行正是用这两个函数定位正负无穷。

> 复数处理的小细节：循环里 `isnan(d)` / `isposinf(d)` / `isneginf(d)` 中的 `d` 先是 `x.real` 后是 `x.imag`。对 `complex(np.inf, np.nan)` 这样的元素，实部 `inf` 被替换成 `maxf`、虚部 `nan` 被替换成 `0`，最终得到 `maxf + 0j`。这就是 docstring 里复数例子的来源。

#### 4.5.4 代码实践

**实践目标**：清洗一个含 `inf`/`-inf`/`nan` 的数组，自定义替换值，并验证复数的实虚部分别处理。

```python
import numpy as np

x = np.array([np.inf, -np.inf, np.nan, -128, 128], dtype=np.float64)

# 1. 默认替换：inf→最大有限值, -inf→最小有限值, nan→0
print(np.nan_to_num(x))
# [ 1.79769313e+308 -1.79769313e+308  0.00000000e+000 -1.28000000e+002  1.28000000e+002]

# 2. 自定义替换值
print(np.nan_to_num(x, nan=-9999, posinf=33333333, neginf=33333333))
# [ 3.3333333e+07  3.3333333e+07 -9.9990000e+03 -1.2800000e+02  1.2800000e+02]

# 3. 复数：实虚部分别清洗
y = np.array([complex(np.inf, np.nan), np.nan, complex(np.nan, np.inf)])
print(np.nan_to_num(y))
# [maxf+0.j, 0.+0.j, 0.+maxf.j]

# 4. copy=False 就地修改（仅当无需类型转换拷贝时）
z = np.array([np.nan, 1.0])
np.nan_to_num(z, copy=False)
print(z)   # [0. 1.]   —— z 本身被改了

# 5. 整数数组：直接返回，不做任何事
i = np.array([1, 2, 3])
print(np.nan_to_num(i))   # [1 2 3]
```

**需要观察的现象**：

- 默认 `inf` 被替换成 `1.7976931348623157e+308`（即 `np.finfo(np.float64).max`）。
- 复数 `complex(np.inf, np.nan)` 实部 `inf`→`maxf`、虚部 `nan`→`0`，结果是 `maxf + 0j`。
- `copy=False` 时原数组被就地改写。
- 整数数组原样返回（不可能含非有限值）。

**预期结果**：上述注释即预期输出。`maxf` 的具体数值约为 `1.7976931348623157e+308`。

#### 4.5.5 小练习与答案

**练习 1**：`np.nan_to_num(np.array([1, 2, 3]))`（整数数组）会做什么？为什么？

> **答案**：原样返回 `[1, 2, 3]`。因为整数类型不属于 `inexact`，函数在 [_type_check_impl.py:469-470](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_type_check_impl.py#L469-L470) 处提前返回，根本不进入替换循环——整数不可能表示 `inf`/`nan`。

**练习 2**：把 `posinf=` 设成一个数组（而非标量）会发生什么？

> **答案**：`copyto(d, maxf, where=idx_posinf)` 会按位置广播写入，因此 `posinf` 可以是和 `x` 同形的数组，实现「逐位置自定义替换」。这正是 docstring 里 `posinf=np.array([...])` 例子的原理。

---

### 4.6 浮点类型推断与代码映射：common_type / mintypecode / typename

#### 4.6.1 概念说明

这一节的三个函数都在「类型」层面工作，但用途各异：

- `np.common_type(*arrays)`：找出一组数组的「**公共浮点类型**」——能安全容纳所有输入的最小浮点类型。返回一个**类型对象**（如 `numpy.float64`）。注意：结果**总是浮点**，即使输入全是整数（整数至少提升到 `float64`）。
- `np.mintypecode(typechars, typeset='GDFgdf', default='d')`：在一组「类型字符」里挑出「**最小可安全转换**」的那个，返回一个**字符**（如 `'d'`）。它是面向「只需要一个字符」的老式接口。
- `np.typename(char)`：把一个类型字符翻译成**人类可读描述**（如 `'d'` → `'double precision'`）。**注意：NumPy 2.5 起弃用，建议改用 `numpy.dtype.name`。**

#### 4.6.2 核心流程

**`common_type`** 用一张「精度表」做提升：

```
array_precision = {float16:0, float32:1, float64:2, longdouble:3,
                   complex64:1, complex128:2, clongdouble:3}
array_type = [[float16, float32, float64, longdouble],      # 实数行
              [None, complex64, complex128, clongdouble]]   # 复数行

def common_type(*arrays):
    is_complex = False; precision = 0
    for a in arrays:
        t = a.dtype.type
        if iscomplexobj(a): is_complex = True
        if 是整数: p = 2                       # 整数按 float64(精度2) 计
        else: p = array_precision[t]
        precision = max(precision, p)          # 取最大精度
    return array_type[1][precision] if is_complex else array_type[0][precision]
```

核心规则：

1. 遍历所有数组，记录「是否出现过复数」与「最高精度」。
2. **整数统一按精度 2（即 `float64`）计入**——这就是「整数输入至少返回 `float64`」的来源。
3. 最终从 `array_type` 的复数行或实数行取出对应精度的类型。

**`mintypecode`** 用一个按「元素大小降序」排列的字符串做选择：

```
_typecodes_by_elsize = 'GDFgdfQqLlIiHhBb?'   # 从大到小（复数优先，再实数，再整数）

def mintypecode(typechars, typeset='GDFgdf', default='d'):
    typecodes = (每个输入转成字符)
    intersection = {出现在 typeset 里的字符}
    if 没有交集: return default
    if 'F' in 交集 and 'd' in 交集: return 'D'   # 复数单精度+实数双精度 → 复数双精度
    return min(intersection, key=_typecodes_by_elsize.index)  # 取「最大」的那个
```

`min(..., key=index)` 选出在字符串里**最靠前**（即**最大**）的类型——因为要「安全容纳」所有输入，必须取其中最大的那个。

**`typename`** 就是字典查表：

```
_namefromtype = {'d': 'double precision', 'f': 'single precision', ...}
def typename(char):
    warnings.warn(...)            # 弃用提示
    return _namefromtype[char]
```

#### 4.6.3 源码精读

`common_type` 的精度提升主循环（注意整数按 `p=2` 计入与 `iscomplexobj` 分流）：

[_type_check_impl.py:698-721](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_type_check_impl.py#L698-L721) —— 遍历取 `t = a.dtype.type`；`iscomplexobj(a)` 置 `is_complex`；整数 `p=2` 否则查 `array_precision`；`precision=max`；末尾按 `is_complex` 从 `array_type` 的对应行取类型。对无 `dtype` 的输入抛 `TypeError` 并建议改用 `result_type`。

精度表与类型表本身：

[_type_check_impl.py:646-654](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_type_check_impl.py#L646-L654) —— `array_type` 两行（实数/复数）、`array_precision` 把每种浮点/复数类型映射到 0-3 的精度等级。

`mintypecode` 的字符选择（含 `'F'+'d'→'D'` 特例与 `min(..., key=index)`）：

[_type_check_impl.py:71-78](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_type_check_impl.py#L71-L78) —— 求交集；空则返回 `default`；`'F'` 与 `'d'` 同在则返回 `'D'`；否则取「在 `_typecodes_by_elsize` 中下标最小（即最大）」的字符。排序常量见 [_type_check_impl.py:23](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_type_check_impl.py#L23)。

`typename` 的弃用警告与字典查表：

[_type_check_impl.py:634-640](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_type_check_impl.py#L634-L640) —— 发 `DeprecationWarning`，`return _namefromtype[char]`。字典 `_namefromtype` 定义在 [_type_check_impl.py:556-578](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_type_check_impl.py#L556-L578)。

> `mintypecode` 与 `common_type` 的差异：前者返回**字符**、后者返回**类型对象**；前者是通用「最小可容纳字符」接口（含整数），后者专门面向浮点提升（整数被强制升到 `float64`）。新代码通常用 `np.result_type` 取代二者，但理解它们的精度提升逻辑有助于读懂老接口与 numpy 内部。

#### 4.6.4 代码实践

**实践目标**：用 `common_type` 推断一组不同类型数组的公共类型，并对比 `mintypecode` 与 `typename`。

```python
import numpy as np

# 1. common_type：纯浮点
print(np.common_type(np.arange(2, dtype=np.float32)))                # <class 'numpy.float32'>
print(np.common_type(np.arange(2, dtype=np.float32), np.arange(2)))   # <class 'numpy.float64'>
# 第二个例子：float32 + 默认 int64(=整数,精度2) → float64

# 2. common_type：出现复数 → 复数结果
print(np.common_type(np.arange(4), np.array([45, 6.j]), np.array([45.0])))  # <class 'numpy.complex128'>

# 3. common_type：输入必须是数组（有 .dtype），不能传 dtype/标量
try:
    np.common_type(np.float32)
except TypeError as e:
    print("TypeError:", "result_type" in str(e))   # True（错误信息建议改用 result_type）

# 4. mintypecode：在 typeset 里挑最大可容纳字符
print(np.mintypecode(['d', 'f', 'S']))   # 'd'（'S'不在typeset，d>f）
x = np.array([1.1, 2-3.j])
print(np.mintypecode(x))                 # 'D'（数组是复数双精度）

# 5. typename：字符 → 描述（NumPy 2.5 起弃用，会发警告）
import warnings
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    print(np.typename('d'))   # double precision
    print(np.typename('F'))   # complex single precision
```

**需要观察的现象**：

- `common_type` 遇到整数会把精度顶到 2（`float64`）；遇到复数则切换到复数行。
- `common_type` 拒绝裸 `dtype`/标量输入，错误信息会建议用 `np.result_type`。
- `mintypecode(['d','f','S'])` 中 `'S'`（字符串）不在默认 `typeset='GDFgdf'` 里，被忽略，最后在 `'d'`、`'f'` 中挑较大的 `'d'`。
- `typename` 在 2.5+ 触发 `DeprecationWarning`，新代码应改用 `np.dtype('d').name`（返回 `'float64'`）。

**预期结果**：上述注释即预期输出。

> 若本地 numpy < 2.5，`typename` 不会发弃用警告——属版本相关行为，待本地验证。

#### 4.6.5 小练习与答案

**练习 1**：`np.common_type(np.array([1], dtype=np.int8), np.array([1], dtype=np.int64))` 返回什么？为什么不是 `int64`？

> **答案**：返回 `numpy.float64`。因为 `common_type` 的结果**总是浮点**：两个整数数组都按精度 `p=2`（`float64`）计入，`max(2,2)=2`，从实数行取 `array_type[0][2] = float64`。整数永远不被返回。

**练习 2**：`np.mintypecode(['F', 'd'])` 为什么返回 `'D'` 而不是 `'F'` 或 `'d'`？

> **答案**：因为 `'F'`（复数单精度）与 `'d'`（实数双精度）同时出现时，需要一个既能容纳复数、又有双精度精度的类型，即 `'D'`（复数双精度）。这是 [_type_check_impl.py:76-77](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_type_check_impl.py#L76-L77) 的硬编码特例：`'F'` 与 `'d'` 任意组合 → `'D'`。

**练习 3**：要用一个字符查「双精度浮点」的人类可读名字，2.5+ 推荐怎么写？

> **答案**：弃用 `np.typename('d')`，改用 `np.dtype('d').name`，返回 `'float64'`（现代 dtype 名称，而非旧的 `'double precision'` 描述串）。

## 5. 综合实践

把本讲的 `nan_to_num` 与 `common_type` 串起来，模拟一个真实的数据清洗场景。

**任务背景**：你从某个传感器读到两组数据（可能有缺失/溢出），需要先清洗非有限值，再推断它们的公共浮点类型以便统一存储。

**操作步骤**（示例代码）：

```python
import numpy as np

# 两组带噪数据：含 nan / inf
a = np.array([1.0, np.nan, 3.0, np.inf, 5.0], dtype=np.float32)
b = np.array([10.0, 20.0, -np.inf, 40.0, np.nan], dtype=np.float64)

# 步骤 1：清洗前先看哪些位置有问题
print("a 的正无穷位置:", np.isposinf(a))     # [False False False  True False]
print("b 的负无穷位置:", np.isneginf(b))     # [False False  True False False]
print("a/b 的 nan 位置:", np.isnan(a), np.isnan(b))

# 步骤 2：用 nan_to_num 清洗，自定义替换值
a_clean = np.nan_to_num(a, nan=-1.0, posinf=999.0, neginf=-999.0)
b_clean = np.nan_to_num(b, nan=-1.0, posinf=999.0, neginf=-999.0)
print("清洗后 a:", a_clean)   # [  1.  -1.   3. 999.   5.]
print("清洗后 b:", b_clean)   # [ 10.  20. -999.  40.  -1.]

# 步骤 3：推断公共浮点类型（决定统一存储用什么 dtype）
common = np.common_type(a_clean, b_clean)
print("公共类型:", common)    # <class 'numpy.float64'>（float32 + float64 → float64）

# 步骤 4：统一转换并验证类型判定
merged = np.array([a_clean, b_clean], dtype=common)
print("合并数组 dtype:", merged.dtype)        # float64
print("是否复数对象:", np.iscomplexobj(merged))  # False
print("是否实数对象:", np.isrealobj(merged))     # True
```

**需要观察的现象与预期结果**：

1. 清洗前用 `isposinf`/`isneginf`/`isnan` 精确定位每种非有限值——这正是 4.4 与 4.5 节函数的典型用法。
2. `nan_to_num` 把 `nan`→`-1`、`+inf`→`999`、`-inf`→`-999`，输出全部是有限值。
3. `common_type(float32, float64)` 返回 `float64`（取较高精度）；若加入一个复数数组，结果会变成 `complex128`。
4. 合并后 `iscomplexobj` 为 `False`、`isrealobj` 为 `True`——这是 4.3 节「类型判定」的验证。

> 进阶：把 `b` 换成 `np.array([...], dtype=np.complex64)` 重跑步骤 3，观察 `common_type` 如何切换到复数行；再用 `np.real_if_close(merged, tol=...)` 尝试把虚部极小的复数结果降级回实数（4.1 节）。

## 6. 本讲小结

- **值判定 vs 类型判定**是本讲的核心分水岭：`iscomplex`/`isreal` 逐元素看「虚部是否为零」返回布尔数组；`iscomplexobj`/`isrealobj` 看 dtype 返回标量布尔，二者不可混用。
- `real`/`imag` 用 `try: val.real except AttributeError: asanyarray(val).real` 的鸭子类型，对 ndarray、标量、列表、第三方数组都能工作；`real_if_close` 用「`tol` 倍机器 ε」的相对容差把虚部极小的复数降级为实数。
- `isposinf`/`isneginf` 的本质是 `logical_and(isinf(x), ±signbit(x))`，靠 IEEE 754 符号位区分正负无穷；二者都在 `_ufunclike_impl.py`，并被 `nan_to_num` 导入复用。
- `nan_to_num` 走「类型短路 → 复数拆实虚 → 三次 `copyto` 按掩码替换」流程，默认界值来自 `finfo`，可逐位置自定义替换值。
- `common_type` 用精度表（整数计为 `float64`、结果恒为浮点）推断公共类型；`mintypecode` 用按大小降序的字符表挑「最大可容纳」字符；二者新代码多用 `np.result_type` 替代。
- `fix`（→`trunc`）与 `typename`（→`dtype.name`）自 NumPy 2.5 起弃用，新代码应迁移；`fix` 的弃用是因为 Array API 标准采用 `trunc`。

## 7. 下一步学习建议

- **承接复数域运算**：本讲的 `real_if_close` 涉及「复数转实数」，下一讲 u10-l2 多项式与 poly1d 会大量处理复数根与系数，建议接着学。
- **向科学数学域延伸**：u11-l1 的 `scimath` 讲解如何在「结果可能含 nan/inf」时切换到复数域，与本讲的 `nan_to_num`（清洗非有限值）形成「检测—清洗—域切换」的完整链条。
- **深入类型系统**：想系统理解 dtype 提升，可阅读 `numpy/_core/numerictypes.py` 与 `np.result_type`/`np.promote_types` 的实现，对照本讲 `common_type`/`mintypecode` 的老式精度表，体会 numpy 类型系统的演进。
- **源码阅读建议**：把 `nan_to_num` 的 `for d in dest` 循环与 `isposinf`/`isneginf` 的 `signbit` 实现对照阅读，理解「跨文件复用」如何让 `_type_check_impl` 与 `_ufunclike_impl` 协作。
