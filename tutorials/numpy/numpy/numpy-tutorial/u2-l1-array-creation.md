# 数组创建方式全览

## 1. 本讲目标

学完本讲后，你应该能够：

- 区分 NumPy 中各类数组创建函数的用途与底层实现差异（`array` / `asarray` / `empty` / `zeros` / `ones` / `full` / `arange` / `linspace`）。
- 理解 `array()` 对「任意输入」的归一化过程，即 `array_like` 是如何变成 `ndarray` 的。
- 读懂创建函数在 Python 层与 C 层的分工：哪些是 C 扩展里的原语，哪些是 Python 层用「`empty` + `copyto`」拼出来的薄封装。
- 在源码中精确定位一个创建函数，并用永久链接引用它。
- 通过代码实践，亲眼看到 `asarray` 与 `array(copy=True)` 在「是否复制内存」上的根本区别。

## 2. 前置知识

本讲默认你已经读过 **u1-l4 ndarray 初体验与核心属性**，知道：

- `ndarray` 是 C 扩展 `_multiarray_umath` 中定义的内置类型，经 `multiarray.py` → `_core/__init__.py` → `numpy/__init__.py` 三跳再导出为 `np.ndarray`。
- `shape`、`strides`、`dtype`、`flags` 是数组的核心属性，其中 `strides` 单位是字节，`flags` 描述内存布局（如 `C_CONTIGUOUS`、`OWNDATA`、`WRITEABLE`）。

本讲会反复用到两个概念，先在这里澄清：

- **array_like**：NumPy 文档里频繁出现的术语，指「任何能被 `np.array` 转成 ndarray 的对象」。Python 标量、列表、元组、嵌套列表、另一个 ndarray、实现了 `__array__` 协议的对象都算 array_like。它不是一个具体类型，而是一个「鸭子类型」约定。
- **视图（view）与拷贝（copy）**：视图与原数组共享同一段底层数据缓冲区，改一个另一个跟着变；拷贝则独立拥有一份内存。是否共享内存可以用 `np.shares_memory(a, b)` 判断。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `numpy/_core/multiarray.py` | 再导出 C 扩展 `_multiarray_umath` 中的创建原语（`array`/`asarray`/`empty`/`zeros`/`arange`），是 Python 与 C 的桥梁。 |
| `numpy/_core/multiarray.pyi` | 类型存根文件，给出 `array`/`asarray`/`asanyarray` 的精确签名与默认参数。 |
| `numpy/_core/numeric.py` | 定义 `ones`/`full`/`identity`/`zeros_like` 等「组合型」创建函数，并从 `multiarray` 导入底层原语。 |
| `numpy/_core/function_base.py` | 定义 `linspace`/`logspace`/`geomspace` 数值区间函数（注意：它们不在 `numeric.py` 里）。 |
| `numpy/_core/_asarray.py` | 定义 `require`，并集中说明 `as*array` 家族的归一化意图；本身从 `multiarray` 导入 `array`/`asanyarray`。 |
| `numpy/_core/src/multiarray/ctors.c` | C 层构造路径：`PyArray_NewFromDescr`、`PyArray_FromAny`、`PyArray_Empty`、`PyArray_Zeros`、`PyArray_Arange` 的真正实现。 |

> 一个容易踩坑的点：很多人凭直觉以为 `np.arange`、`np.zeros`、`np.linspace` 都在 `numeric.py` 里。实际上 `arange`/`zeros`/`empty` 是 C 扩展原语，而 `linspace` 在 `function_base.py`。本讲会逐一在源码中证实。

## 4. 核心概念与源码讲解

### 4.1 创建函数的分层与显式创建

#### 4.1.1 概念说明

NumPy 的数组创建函数看起来很多，但按「实现位置」可以分成三层：

1. **C 原语层**：`array`、`asarray`、`asanyarray`、`empty`、`zeros`、`arange`。它们直接由 C 扩展 `_multiarray_umath` 提供，能最直接地操作内存。Python 里没有它们的 `def`，只有再导出。
2. **Python 组合层**：`ones`、`full`、`identity`、`zeros_like`、`ones_like`、`full_like`。它们在 `numeric.py` 中用「先 `empty` 申请一块未初始化内存，再用 `copyto` 填充」的方式拼出来。本质是 C 原语的薄封装。
3. **数值区间层**：`linspace`、`logspace`、`geomspace`。它们在 `function_base.py` 中，用 `arange` 加 broadcasting 运算算出等间距样本。

理解这个分层的意义在于：当你想知道某个创建函数「到底做了什么」时，能立刻去对的地方找源码，而不是在 `numeric.py` 里空找。

#### 4.1.2 核心流程

「Python 组合层」的统一套路是：

```text
shape + dtype  ──►  empty(...)        # 申请未初始化内存（快，但不清零）
                    │
                    └─►  copyto(arr, fill_value, casting='unsafe')   # 广播填充
                              │
                              └─►  返回 arr
```

`empty` 负责拿内存，`copyto` 负责填值。`copyto` 本身也是 C 函数，会把 `fill_value` 广播到整个数组，并按 `casting` 规则做类型转换。

`linspace` 的套路则不同：它不填常量，而是先算出步长 `step = (stop - start) / (num - 1)`，再用 `arange(0, num)` 乘以 `step`、加上 `start`，得到等间距序列。

#### 4.1.3 源码精读

**C 原语从哪里来。** `numeric.py` 顶部从 `multiarray` 导入了一批名字，其中就包含 `arange`、`array`、`asanyarray`、`asarray`、`empty`、`zeros`、`copyto`：

[numpy/_core/numeric.py:15-60](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numeric.py#L15-L60) —— 这段 `from .multiarray import (...)` 说明 `arange`/`array`/`asarray`/`empty`/`zeros`/`copyto` 都不是在 `numeric.py` 里定义的，而是来自 C 扩展。

而 `multiarray.py` 本身只是 `_multiarray_umath` 的再导出壳子，它的 `__all__` 列表里赫然列着这些名字：

[numpy/_core/multiarray.py:30-50](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/multiarray.py#L30-L50) —— `__all__` 包含 `array`、`asarray`、`asanyarray`、`empty`、`zeros`、`arange` 等，证实它们由 C 扩展 `_multiarray_umath` 提供，`multiarray.py` 只是把名字搬过来。

**Python 组合层：`ones`。** `ones` 没有自己去清零内存，而是先 `empty` 再 `copyto` 填 1：

[numpy/_core/numeric.py:227-234](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numeric.py#L227-L234) —— `a = empty(shape, dtype, order, ...)` 申请未初始化内存，`multiarray.copyto(a, 1, casting='unsafe')` 把 1 广播填进去。`casting='unsafe'` 表示允许任意类型转换（比如默认 float64 数组填整数 1）。

**Python 组合层：`full`。** `full` 比 `ones` 多一步：当调用者没指定 `dtype` 时，要先从 `fill_value` 推断 dtype：

[numpy/_core/numeric.py:382-387](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numeric.py#L382-L387) —— `fill_value = asarray(fill_value)` 把填充值变成数组以读取其 dtype，然后同样 `empty` + `copyto`。注意 `np.full((2,2), [1,2])` 这种「fill_value 是数组」的用法正是靠 `copyto` 的广播能力实现的。

**Python 组合层：`zeros_like`。** 它没有直接调用 C 的 `zeros`，而是 `empty_like` + `copyto` 从一个长度为 1 的 `zeros` 数组广播填充：

[numpy/_core/numeric.py:161-167](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numeric.py#L161-L167) —— `z = zeros(1, dtype=res.dtype)` 只造一个零元素，再靠 `copyto` 广播到整个 `res`。注释说这样做是为了让字符串 dtype 也能得到正确的「零值」（空串）。

**Python 组合层：`identity`。** 它直接转发给 `eye`：

[numpy/_core/numeric.py:2213-2217](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numeric.py#L2213-L2217) —— `identity` 几乎只是 `eye(n, dtype=dtype)` 的别名。

**数值区间层：`linspace`。** `linspace` 在 `function_base.py`（不在 `numeric.py`），它的核心是用 `arange` 生成下标再缩放：

[numpy/_core/function_base.py:123-155](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/function_base.py#L123-L155) —— 先算 `div = num - 1`（`endpoint=True` 时），再用 `_array_converter` 把 `start`/`stop` 归一化成数组，最后 `y = _nx.arange(0, num, dtype=dt, device=device).reshape(...)` 生成 `0..num-1` 的下标向量。后面的 `y *= step; y += start` 把它映射到 `[start, stop]` 区间。

[numpy/_core/function_base.py:184-193](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/function_base.py#L184-L193) —— `y += start` 平移，`y[-1, ...] = stop` 强制把最后一个点钉在 `stop`（避免浮点误差导致终点不精确），`_nx.floor(y, out=y)` 处理整数 dtype 的情况。这就是为什么 `np.linspace(2, 3, 5)` 总能精确地以 3.0 结尾。

#### 4.1.4 代码实践

**实践目标**：验证「Python 组合层创建函数确实共享同一套 `empty` + `copyto` 套路」。

**操作步骤**：

1. 打开本仓库 `numpy/_core/numeric.py`，跳到 `ones`（约第 172 行）与 `full`（约第 325 行）。
2. 对照阅读 `zeros_like`（约第 100 行）与 `identity`（约第 2181 行）。
3. 在本地装好 NumPy 后运行下面这段「示例代码」：

```python
# 示例代码：观察 ones / full / identity 的产出
import numpy as np

print(np.ones((2, 3), dtype=np.int64))
print(np.full((2, 2), 7.0))
print(np.identity(3))
```

**需要观察的现象**：`ones` 产出全 1 的整数矩阵；`full` 产出全 7.0 的浮点矩阵；`identity` 产出单位阵。

**预期结果**：三个函数返回值都是 C 连续（`flags['C_CONTIGUOUS'] is True`）、`OWNDATA` 为 `True` 的全新数组。因为它们底层都走 `empty` 申请新内存。

**待本地验证**：不同 NumPy 版本 / 平台下 `empty` 返回的未初始化内存内容（若你直接打印 `np.empty(3)`）是随机的，不应假设具体数值。

#### 4.1.5 小练习与答案

**练习 1**：`np.ones(5)` 默认 dtype 是什么？为什么？

**参考答案**：默认 `float64`。因为 `ones` 调用 `empty(shape, dtype, ...)`，而 `dtype=None` 时 `empty`（C 层）会取默认浮点类型 `float64`。这与 `np.array([1,2,3])` 默认推断出 `int64` 不同——`ones`/`zeros`/`empty`/`full` 在 `dtype=None` 时一律给 `float64`，而 `array` 会根据数据内容推断。

**练习 2**：`np.linspace(0, 10, 5)` 与 `np.arange(0, 10, 2.5)` 都生成 5 个点吗？

**参考答案**：`linspace(0, 10, 5)` 生成 `[0, 2.5, 5, 7.5, 10]` 共 5 个点，**包含**终点 10。`arange(0, 10, 2.5)` 生成 `[0, 2.5, 5, 7.5]` 共 4 个点，**不含**终点 10（半开区间）。两者语义不同：`linspace` 按「点数」采样，`arange` 按「步长」采样。

---

### 4.2 array_like 归一化：asarray / asanyarray / require

#### 4.2.1 概念说明

很多 NumPy 函数的签名里写着 `a : array_like`，意味着它们能接受列表、元组、标量、另一个数组等五花八门的输入。但底层 C 代码只能操作 `ndarray`，所以在进入真正的计算前，必须先把 `array_like` 归一化成 `ndarray`。这一步由 `as*array` 家族负责：

- `np.asarray(a)`：把 `a` 转成 ndarray；**如果 `a` 已经是合适 dtype 的 ndarray，则不复制**，直接返回原对象。
- `np.asanyarray(a)`：同上，但**放行 ndarray 子类**（如 `np.ma.MaskedArray`、`np.matrix`），不强制降级成基类 `ndarray`。
- `np.require(a, requirements=...)`：在 `asanyarray` 基础上，保证结果满足指定内存布局要求（C 连续、F 连续、对齐、可写、拥有数据等），不满足就复制一份。

这一层的核心矛盾是「**何时复制**」。复制意味着新内存、更安全但更慢；不复制意味着共享内存、更快但改一处会影响另一处。NumPy 用 `copy` 参数来控制：

- `copy=True`：总是复制。
- `copy=False`：绝不复制，需要复制时反而报错。
- `copy=None`：**仅在必要时复制**（这是 `asarray`/`asanyarray` 的默认行为）。

#### 4.2.2 核心流程

`asarray` 与 `array` 的关系可以近似写成：

```python
# 概念伪代码（非真实源码）
def asarray(a, dtype=None, order=None, copy=None):
    return array(a, dtype=dtype, order=order, copy=copy, subok=False)

def asanyarray(a, dtype=None, order=None, copy=None):
    return array(a, dtype=dtype, order=order, copy=copy, subok=True)
```

关键差异在两个参数：

| 函数 | `copy` 默认 | `subok` | 行为 |
| --- | --- | --- | --- |
| `np.array` | `True`（总复制） | `False` | 保守：永远给你一份新内存的基类 ndarray |
| `np.asarray` | `None`（按需复制） | `False` | 高效：能不复制就不复制，但强制基类 |
| `np.asanyarray` | `None`（按需复制） | `True` | 高效且放行子类 |

`require` 的流程则是：先 `asanyarray`，再逐条检查 `requirements`，哪条不满足就 `arr.copy(order)` 复制一份。

#### 4.2.3 源码精读

**签名与默认值。** 类型存根 `multiarray.pyi` 给出精确签名，这是判断默认参数最可靠的依据：

[numpy/_core/multiarray.pyi:595-605](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/multiarray.pyi#L595-L605) —— `np.array` 的 `copy: bool | _CopyMode | None = True`，`subok: bool = False`。即 `np.array` 默认**总是复制**且强制基类。

[numpy/_core/multiarray.pyi:1017-1025](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/multiarray.pyi#L1017-L1025) —— `np.asarray` 的 `copy: bool | None = None`，即「按需复制」。注意 `asarray` 不暴露 `subok` 参数，因为它在 C 层把 `subok` 硬编码为 `False`。

[numpy/_core/multiarray.pyi:1059-1067](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/multiarray.pyi#L1059-L1067) —— `np.asanyarray` 同样 `copy: bool | None = ...`，但它在 C 层 `subok=True`，所以会放行子类。

**`_asarray.py` 的真实职责。** 很多人以为 `_asarray.py` 实现了 `asarray`，其实没有——它只定义了 `require`，并把 `array`/`asanyarray` 从 `multiarray` 导入进来：

[numpy/_core/_asarray.py:6-9](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_asarray.py#L6-L9) —— `from .multiarray import array, asanyarray`，且 `__all__ = ["require"]`。这说明 `asarray`/`asanyarray` 本体在 C 扩展里，`_asarray.py` 只是「`as*array` 家族 + `require`」的概念归集地。

**`require` 的实现。** `require` 用一个 `POSSIBLE_FLAGS` 字典把用户传入的各种写法（`'C'`/`'C_CONTIGUOUS'`/`'CONTIGUOUS'` 都映射到 `'C'`）归一化，然后逐条检查：

[numpy/_core/_asarray.py:12-19](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_asarray.py#L12-L19) —— `POSSIBLE_FLAGS` 把同义的标志名归一成单字母键。

[numpy/_core/_asarray.py:101-127](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_asarray.py#L101-L127) —— 没有要求时直接 `asanyarray(a, dtype=dtype)` 返回（可能不复制）；有要求时调 `array(a, dtype=dtype, order=order, copy=None, subok=subok)`，再用 `for prop in requirements: if not arr.flags[prop]: return arr.copy(order)`——哪条 flag 不满足就复制一份满足它的。这就是 `require` 名字的由来：保证结果「满足要求」。

**`__module__` 改写。** `multiarray.py` 把这些 C 函数的 `__module__` 改成 `'numpy'`，所以你在交互式环境看到的是 `numpy.asarray` 而不是 `numpy._core.multiarray.asarray`：

[numpy/_core/multiarray.py:59-64](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/multiarray.py#L59-L64) —— `array.__module__ = 'numpy'`、`asarray.__module__ = 'numpy'`、`asanyarray.__module__ = 'numpy'`，这是「再导出」时常见的模块归属改写。

#### 4.2.4 代码实践

**实践目标**：亲眼看到 `asarray` 在「输入已是合适 ndarray」时不复制，而 `array(copy=True)` 一定复制。

**操作步骤**：运行下面这段「示例代码」：

```python
# 示例代码：asarray 与 array(copy=True) 的复制行为
import numpy as np

base = np.arange(10)               # base 拥有自己的内存，OWNDATA=True

a_view  = np.asarray(base)        # copy=None，按需复制 -> 不复制
a_copy  = np.array(base, copy=True)  # 强制复制

print("view shares memory with base?", np.shares_memory(a_view, base))  # 预期 True
print("copy shares memory with base?", np.shares_memory(a_copy, base))  # 预期 False

# 改 base，观察谁跟着变
base[0] = 99
print("a_view[0] =", a_view[0])   # 预期 99（共享内存）
print("a_copy[0] =", a_copy[0])   # 预期 0（独立内存）
```

**需要观察的现象**：`a_view` 与 `base` 共享内存（改 `base[0]` 后 `a_view[0]` 也变 99）；`a_copy` 与 `base` 不共享内存（`a_copy[0]` 仍是 0）。

**预期结果**：`np.shares_memory(a_view, base)` 为 `True`，`np.shares_memory(a_copy, base)` 为 `False`。

**结论**：`asarray` 默认 `copy=None`，在 C 层走「输入已是 ndarray 且无 dtype/order 约束」的快速路径，直接返回原对象（见 4.3.3 的 `PyArray_FromAny_int` 快速路径）；`array(copy=True)` 则强制走复制路径。

#### 4.2.5 小练习与答案

**练习 1**：`np.asarray([1, 2, 3])` 会复制吗？

**参考答案**：会。输入是 Python 列表，不是 ndarray，必须新申请内存把元素搬进去。「按需复制」的「需」在这里成立了——不复制没法得到 ndarray。`copy=None` 只在「输入已经是合适的 ndarray」时才省掉复制。

**练习 2**：为什么 `np.ma.MaskedArray` 用户更希望库函数内部用 `asanyarray` 而非 `asarray`？

**参考答案**：`asarray` 的 `subok=False` 会把 `MaskedArray` 降级成普通 `ndarray`，从而丢失掩码信息；`asanyarray` 的 `subok=True` 会放行子类，保留掩码。所以处理子类时用 `asanyarray` 更安全。

---

### 4.3 底层 C 构造路径：ctors.c

#### 4.3.1 概念说明

前面两层（C 原语再导出、Python 组合封装）最终都要落到一个问题上：**C 代码到底怎么造出一个 ndarray？** 答案在 `numpy/_core/src/multiarray/ctors.c`。这个文件是 NumPy 数组构造的「总装车间」，里面有几个核心函数：

- `PyArray_NewFromDescr`：给定 dtype、shape、strides、数据指针，造一个 ndarray。这是最底层的「组装」原语。
- `PyArray_FromAny`：把任意 Python 对象转成 ndarray——`array()`/`asarray()` 最终都走这里。它负责 `array_like` 的真正归一化。
- `PyArray_Empty` / `PyArray_Zeros`：分别对应 `empty` 和 `zeros` 的 C 实现。
- `PyArray_Arange`：`arange` 的 C 实现。
- `PyArray_FromBuffer` / `PyArray_FromIter` / `PyArray_FromFile` / `PyArray_FromString`：从现有数据（缓冲区、迭代器、文件、字符串）构造数组的「from-*」家族。

理解这一层后，你会明白 `empty` 与 `zeros` 为什么一个快一个稍慢、`asarray` 为什么能「不复制」。

#### 4.3.2 核心流程

**`empty` vs `zeros` 的差别在「一个 flag」**：

```text
PyArray_Empty  ──► PyArray_NewFromDescr_int( flags = 0                 )  # 不清零
PyArray_Zeros  ──► PyArray_NewFromDescr_int( flags = _NPY_ARRAY_ZEROED )  # 清零
```

`PyArray_NewFromDescr_int` 是真正的内存分配入口。当 `flags` 里带 `_NPY_ARRAY_ZEROED` 时，分配器走 `calloc` 风格的零填充路径；否则只 `malloc`，内存内容未定义。这就是 `empty` 比 `zeros` 快的原因——它省掉了清零那一步。

**`arange` 的长度计算**：给定 `start`、`stop`、`step`，元素个数为

\[
\text{length} = \left\lceil \frac{\text{stop} - \text{start}}{\text{step}} \right\rceil
\]

C 代码用 `_arange_safe_ceil_to_intp` 做向上取整并防溢出。算出长度后，先写前两个元素 `start` 与 `start+step`，再调 dtype 自带的 `fill` 函数用等差递推填满剩余位置（避免反复浮点乘法累积误差）。

**`PyArray_FromAny` 的快速路径**：如果输入已经是 ndarray、且没有 dtype/flags/深度约束，直接 `Py_NewRef(op)` 返回原对象——不复制。这就是 `asarray` 不复制的 C 层根因。

#### 4.3.3 源码精读

**`PyArray_NewFromDescr`：底层组装原语。**

[numpy/_core/src/multiarray/ctors.c:952-974](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/ctors.c#L952-L974) —— 接收 `subtype`（数组子类型）、`descr`（dtype 描述符）、`nd`/`dims`/`strides`（形状与步长）、`data`（数据指针，可为 NULL 表示新分配）、`flags`。它只是个薄壳，转调 `PyArray_NewFromDescrAndBase` → `PyArray_NewFromDescr_int`。几乎所有创建函数最终都会汇聚到这里。

**`PyArray_FromAny`：array_like 归一化的总入口。**

[numpy/_core/src/multiarray/ctors.c:1457-1491](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/ctors.c#L1457-L1491) —— `array()`/`asarray()` 在 C 层都调它。先用 `PyArray_ExtractDTypeAndDescriptor` 把 `newtype`（用户指定的 dtype）拆成 descr + DType 元类，再转给 `PyArray_FromAny_int`。

[numpy/_core/src/multiarray/ctors.c:1505-1523](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/ctors.c#L1505-L1523) —— `PyArray_FromAny_int` 的快速路径：当 `in_descr == NULL && in_DType == NULL && flags == 0 && min_depth == 0 && PyArray_Check(op)` 时，直接 `return Py_NewRef(op)`。即「输入已经是 ndarray 且无任何约束」时，只增加引用计数、不复制。文件注释明确写道这是「Fast path」。这正是 4.2.4 中 `asarray` 不复制的根因。

**`PyArray_Zeros_int`：带零填充的分配。**

[numpy/_core/src/multiarray/ctors.c:2967-2992](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/ctors.c#L2967-L2992) —— 调 `PyArray_NewFromDescr_int(..., _NPY_ARRAY_ZEROED)`。第 2989 行那个 `_NPY_ARRAY_ZEROED` 标志就是 `zeros` 比 `empty` 多做的「清零」工作的全部来源。

**`PyArray_Empty_int`：不清零的分配。**

[numpy/_core/src/multiarray/ctors.c:3025-3060](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/ctors.c#L3025-L3060) —— 同样调 `PyArray_NewFromDescr`，但**不**传 `_NPY_ARRAY_ZEROED`，所以数据缓冲区是未初始化的。唯一的安全处理在第 3052-3057 行：当 dtype 是对象类型（`PyDataType_REFCHK` 为真，即元素是 `PyObject*`）时，把所有元素初始化为 `None`，避免悬挂指针。对于数值 dtype，则完全不管——这就是 `np.empty(3)` 会打印出「随机」值的根因。

**`PyArray_Arange`：等差数列的 C 实现。**

[numpy/_core/src/multiarray/ctors.c:3094-3168](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/ctors.c#L3094-L3168) —— 先算 `delta = stop - start`、`tmp_len = delta/step`，再 `_arange_safe_ceil_to_intp(tmp_len)` 得到长度 `length`（第 3104-3121 行）。第 3128 行用 `PyArray_New` 分配长度为 `length` 的一维数组。随后把第 0 位写 `start`、第 1 位写 `start+step`（第 3139-3150 行），最后第 3165 行 `funcs->fill(...)` 用 dtype 自带的等差填充函数把剩余位置填满——这种「先写两个种子，再递推」的方式比「每个位置都算 `start + i*step`」更省且数值更稳。

#### 4.3.4 代码实践

**实践目标**：从行为层面验证 `empty` 的内存未初始化、`zeros` 的内存被清零。

**操作步骤**：运行下面这段「示例代码」：

```python
# 示例代码：对比 empty 与 zeros 的内存初值
import numpy as np

z = np.zeros(5, dtype=np.float64)
e = np.empty(5, dtype=np.float64)

print("zeros:", z)        # 预期全 0.0
print("empty:", e)        # 可能是任意值（待本地验证）
print("zeros flags OWNDATA:", z.flags['OWNDATA'])  # 预期 True
print("empty flags OWNDATA:", e.flags['OWNDATA'])  # 预期 True
```

**需要观察的现象**：`zeros` 永远是 `[0. 0. 0. 0. 0.]`；`empty` 的内容不可预测（可能是 0，也可能是上一次释放留下的残留值）。

**预期结果**：`zeros` 全 0；两者 `OWNDATA` 均为 `True`（各自拥有独立内存）。

**待本地验证**：`empty` 的具体数值无法预测，不要在代码里依赖它为 0。如果想看「未初始化」的效果，可以连续多次 `np.empty(8)` 观察到不同结果。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `np.empty(1000000)` 比 `np.zeros(1000000)` 略快？

**参考答案**：`empty` 走 `PyArray_Empty_int`，调 `PyArray_NewFromDescr` 时不带 `_NPY_ARRAY_ZEROED`，分配器只需 `malloc` 拿到内存即返回；`zeros` 走 `PyArray_Zeros_int`，多传一个 `_NPY_ARRAY_ZEROED` 标志，分配器需要把整块内存清零。大数组上清零的代价显著，所以 `empty` 更快。

**练习 2**：`np.asarray(np.arange(5))` 在 C 层走了哪条路径？为什么没有复制？

**参考答案**：`asarray` 的 `copy=None`，在 C 层进入 `PyArray_FromAny_int` 的快速路径（ctors.c 第 1519-1523 行）：输入已是 ndarray、无 dtype/flags/深度约束，于是直接 `Py_NewRef(op)` 返回原数组，仅增加引用计数，不分配新内存、不复制数据。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个贯穿任务：**用 5 种方式创建等价的 0-9 整数数组，比较它们的内存布局与是否共享内存，并解释 `asarray` 与 `array(copy=True)` 的区别。**

**操作步骤**：

1. 用以下 5 种方式各创建一个内容为 `[0, 1, 2, 3, 4, 5, 6, 7, 8, 9]` 的整数数组：

```python
# 示例代码：5 种创建等价数组的方式
import numpy as np

a1 = np.array([0, 1, 2, 3, 4, 5, 6, 7, 8, 9])                  # 显式列表
a2 = np.arange(10)                                             # 数值区间（C 原语）
a3 = np.linspace(0, 9, 10, dtype=np.int64)                     # 数值区间（function_base）
a4 = np.asarray(a1)                                            # 归一化（按需复制 -> 不复制）
a5 = np.array(a1, copy=True)                                   # 显式强制复制

for name, a in [("a1 array", a1), ("a2 arange", a2),
                ("a3 linspace", a3), ("a4 asarray(a1)", a4),
                ("a5 array(a1,copy=True)", a5)]:
    print(f"{name:24s} dtype={a.dtype} "
          f"C_CONTIGUOUS={a.flags['C_CONTIGUOUS']} "
          f"OWNDATA={a.flags['OWNDATA']}")

print("shares(a1, a4):", np.shares_memory(a1, a4))   # 预期 True
print("shares(a1, a5):", np.shares_memory(a1, a5))   # 预期 False
print("shares(a1, a2):", np.shares_memory(a1, a2))   # 预期 False
```

2. 逐项核对每个数组的 `flags`（`C_CONTIGUOUS`、`OWNDATA`）与两两之间 `np.shares_memory` 的结果。
3. 回答三个问题：
   - 哪几个数组 `OWNDATA=True`？哪一个是 `OWNDATA=False`？为什么？
   - `a4` 与 `a1` 为什么共享内存？结合 4.3.3 的 `PyArray_FromAny_int` 快速路径解释。
   - `a5` 与 `a1` 为什么不共享内存？结合 4.2.3 中 `np.array` 的 `copy=True` 默认值解释。

**需要观察的现象**：

- `a1`/`a2`/`a3`/`a5` 都 `OWNDATA=True`（各自拥有独立内存）。
- `a4` 的 `OWNDATA=False`——它只是 `a1` 的视图，并不拥有数据，数据归 `a1` 所有。
- `shares_memory(a1, a4)` 为 `True`，其余涉及 `a1` 的组合为 `False`。

**预期结果**：

| 数组 | dtype | C_CONTIGUOUS | OWNDATA | 与 a1 共享内存 |
| --- | --- | --- | --- | --- |
| a1 array | int64 | True | True | — |
| a2 arange | int64 | True | True | False |
| a3 linspace | int64 | True | True | False |
| a4 asarray(a1) | int64 | True | **False** | **True** |
| a5 array(a1,copy=True) | int64 | True | True | False |

> 说明：`a1` 的 dtype 在 64 位平台上为 `int64`，32 位平台可能为 `int32`；`linspace` 用 `dtype=np.int64` 显式指定，故 `a3` 恒为 `int64`。若你的 `a1` 是 `int32`，比较时请统一 dtype 以免 `shares_memory` 之外还掺入 dtype 差异。

**结论解释**：

- `asarray(a1)`：`copy=None`，C 层走 `PyArray_FromAny_int` 快速路径（输入已是合适 ndarray、无约束），直接 `Py_NewRef` 返回原对象，因此 `a4` 与 `a1` 共享内存、`OWNDATA=False`。
- `array(a1, copy=True)`：`copy=True` 强制复制，C 层必走分配新内存 + 拷贝数据的路径，因此 `a5` 独立、`OWNDATA=True`、不与 `a1` 共享内存。
- 这就是「按需复制」与「总是复制」的差别：`asarray` 把「是否复制」的决定权交给运行时条件，`array(copy=True)` 则无条件复制。

## 6. 本讲小结

- NumPy 创建函数分三层：C 原语（`array`/`asarray`/`empty`/`zeros`/`arange`，来自 `_multiarray_umath`）、Python 组合（`ones`/`full`/`identity`/`*_like`，在 `numeric.py` 用 `empty`+`copyto` 拼成）、数值区间（`linspace`/`logspace`/`geomspace`，在 `function_base.py`）。
- `arange`/`zeros`/`empty` 不在 `numeric.py`，而是 C 扩展原语，经 `multiarray.py` 再导出；`linspace` 在 `function_base.py`——不要凭函数名猜文件。
- `array_like` 归一化由 `as*array` 家族负责：`np.array` 默认 `copy=True`（总复制、`subok=False`）；`np.asarray` 默认 `copy=None`（按需复制、`subok=False`）；`np.asanyarray` 默认 `copy=None` 且放行子类（`subok=True`）。
- `_asarray.py` 只定义 `require`，`asarray`/`asanyarray` 本体在 C 扩展；`require` 靠 `POSSIBLE_FLAGS` 归一化需求、逐条检查 flag、不满足就 `copy`。
- C 层 `ctors.c` 是总装车间：`PyArray_NewFromDescr` 是底层组装原语，`PyArray_FromAny` 是 `array()`/`asarray()` 的总入口（含「已是 ndarray 则不复制」的快速路径）。
- `empty` 与 `zeros` 在 C 层的差别只是 `PyArray_NewFromDescr_int` 是否带 `_NPY_ARRAY_ZEROED` 标志；`arange` 用 `ceil((stop-start)/step)` 算长度后「写两个种子 + `fill` 递推」填满。

## 7. 下一步学习建议

- 下一讲 **u2-l2 dtype 与标量类型体系** 将深入 `dtype` 本身：`kind`/`itemsize`/`byteorder` 等属性、`np.floating`/`np.number` 等抽象层级。本讲反复出现的 `dtype` 参数与 `casting='unsafe'` 的真正含义会在那里讲清。
- 想理解 `copyto` 的广播与类型转换规则，可先读 **u2-l3 类型转换、提升规则与精度**（NEP 50）。
- 想看 `PyArray_NewFromDescr_int` 内部如何真正分配内存（含小对象缓存与 hugepage），可留到 **u9-l1 内存管理、分配缓存与 memmap**。
- 建议同步阅读官方文档 `doc/source/user/basics.creation.rst`（仓库内），它从用户视角归纳了本讲涉及的创建函数，与本讲源码视角互补。
