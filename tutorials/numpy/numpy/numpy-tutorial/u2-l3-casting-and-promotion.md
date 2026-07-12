# 类型转换、提升规则与精度（NEP 50）

## 1. 本讲目标

学完本讲后，你应该能够：

- 区分「类型转换（casting）」和「类型提升（promotion）」这两件事，并掌握 `astype`、`can_cast` 的用法与五种 casting 级别。
- 用 `promote_types` / `result_type` / `min_scalar_type` 预测两个或多个 dtype 运算后的结果类型。
- 读懂 NumPy 2.x 的提升规则 **NEP 50**：尤其是「Python 标量只看 kind、不看精度/数值」这条核心规则，以及它在 C 层 `convert_datatype.c` 中的真正实现。
- 理解为什么 NumPy 2.x 不再有「提升策略开关」，并知道精度溢出是如何通过 `seterr`/`errstate` 报告的。
- 用 `_dtype.py` 的 kind 工具检视任意 dtype 的归类。

> 关于源码位置的说明：本讲的提升/转换函数并不在 `numerictypes.py` 里（那里只有类型层次判别 `issubdtype`/`isdtype`，见上一讲）。真正的提升/转换函数是 C 扩展提供的，经 `numpy/_core/multiarray.py` 再导出，核心算法在 C 文件 `numpy/_core/src/multiarray/convert_datatype.c` 中。本讲全部依据这些真实文件展开。

## 2. 前置知识

在进入本讲前，建议你已经掌握（见 u2-l2）：

- **dtype 三层对象**：标量类型（如 `np.float64`）、dtype 实例（如 `np.dtype("float64")`）、DType 类（如 `np.dtypes.Float64DType`）。
- **kind 字符**：每个数值 dtype 都有一个 `kind`，常见取值是 `b`(bool)、`i`(有符号整数)、`u`(无符号整数)、`f`(浮点)、`c`(复数)、`S`(字节串)、`U`(字符串)、`O`(object)。可以用 `arr.dtype.kind` 读取。
- **itemsize**：dtype 占用的字节数，`float64` 的 itemsize 是 8。

两个本讲要反复用到的核心概念：

- **类型转换（casting）**：把一个数组从 dtype A **显式地**变成 dtype B，比如 `arr.astype(np.float32)`。关键问题是：这次转换「安不安全」？会不会丢信息？NumPy 用 5 个级别来回答。
- **类型提升（promotion）**：当两个不同 dtype 的数组（或标量）一起运算（`+`、`*`、…）时，NumPy 要决定结果用什么 dtype。这个「找一个能同时容纳双方的公共类型」的过程叫提升。

一句话区分：转换是**你主动指定目标**，提升是**NumPy 替你算出来的结果类型**。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [numpy/_core/numeric.py](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numeric.py) | 定义 Array API 风格的 `np.astype(x, dtype)` 函数。 |
| [numpy/_core/multiarray.py](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/multiarray.py) | 再导出 C 扩展里的 `can_cast`、`result_type`、`min_scalar_type`、`promote_types`。 |
| [numpy/_core/include/numpy/ndarraytypes.h](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/include/numpy/ndarraytypes.h) | 定义 5 个 casting 级别枚举 `NPY_NO_CASTING`…`NPY_UNSAFE_CASTING`。 |
| [numpy/_core/src/multiarray/convert_datatype.c](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/convert_datatype.c) | 提升算法的 C 实现：`PyArray_PromoteTypes` 与 `PyArray_ResultType`（NEP 50 弱类型机制）。 |
| [numpy/_core/_dtype.py](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_dtype.py) | dtype 的字符串/表示工具，含 `kind`→名称的映射 `_kind_to_stem`。 |
| [numpy/_core/_ufunc_config.py](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_ufunc_config.py) | 浮点错误报告配置：`seterr`/`geterr`/`errstate`/`setbufsize`。 |
| [numpy/__init__.py](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py) | 导入时处理 `NPY_PROMOTION_STATE`（已废弃，仅告警）。 |
| [doc/source/reference/arrays.promotion.rst](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/doc/source/reference/arrays.promotion.rst) | NEP 50 提升规则的官方文档（带提升格示意图）。 |

## 4. 核心概念与源码讲解

### 4.1 类型转换：astype 与五种 casting 级别

#### 4.1.1 概念说明

「转换」回答的问题是：**从 dtype A 到 dtype B，允许吗？安全吗？** NumPy 用 5 个递进的级别来描述：

| 级别 | 含义 | 典型允许的转换 |
|------|------|----------------|
| `no` | 完全不允许转换 | A 和 B 必须完全相同 |
| `equiv` | 仅允许字节序变化 | 小端 int32 ↔ 大端 int32 |
| `safe` | 只允许「保值」转换 | int8 → int64、int32 → float64 |
| `same_kind` | safe + 同 kind 内的窄化 | float64 → float32 |
| `unsafe` | 任意转换，可丢数据 | float64 → int8 |

`can_cast(from, to, casting="safe")` 就是按某个级别判断「能不能转」；`astype` 则在底层用这些级别来决定一次显式转换是否可行。

#### 4.1.2 核心流程

- `np.astype(x, dtype, copy=True)`：把数组 `x` 转成 `dtype`，默认总是复制。
- 它最终调用 ndarray 方法 `x.astype(dtype, copy=copy)`（C 层实现）。当 `copy=False` 且目标 dtype 与原 dtype 相同时，直接返回原数组（共享内存），不复制。
- `np.can_cast(np.int32, np.int64)` → `True`（safe）；`np.can_cast(complex, float)` → `False`（complex 无法无损转 float）。

#### 4.1.3 源码精读

`np.astype` 的函数形态（Array API 兼容）定义在 `numeric.py`：

[numpy/_core/numeric.py:2615-2678](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numeric.py#L2615-L2678) — 这是函数版的 `astype`：校验输入必须是 ndarray 或标量，校验 device，最后把活儿交给 ndarray 方法 `x.astype(dtype, copy=copy)`。

关键收尾一行：

```python
return x.astype(dtype, copy=copy)
```

五种 casting 级别在 C 头文件里定义为枚举：

[numpy/_core/include/numpy/ndarraytypes.h:254-264](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/include/numpy/ndarraytypes.h#L254-L264) — `NPY_NO_CASTING=0`、`NPY_EQUIV_CASTING=1`、`NPY_SAFE_CASTING=2`、`NPY_SAME_KIND_CASTING=3`、`NPY_UNSAFE_CASTING=4`。`can_cast` 的 `casting` 字符串就是在这里被翻译成数字参与判断的。

`can_cast` 的 Python 层是个极薄的派发器：

[numpy/_core/multiarray.py:603-662](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/multiarray.py#L603-L662) — 注意它的函数体只有 `return (from_,)`，真正干活的是装饰器 `@array_function_from_c_func_and_dispatcher(_multiarray_umath.can_cast)` 绑定的同名 C 函数。这种「Python 壳 + C 核心」的写法在下一节会反复出现。它的 docstring（L617-625）正是上表五个级别的官方定义。

#### 4.1.4 代码实践

1. **实践目标**：直观感受五种 casting 级别与 `astype` 的 `copy` 语义。
2. **操作步骤**：
   ```python
   import numpy as np
   a = np.array([1, 2, 3], dtype=np.int32)

   # can_cast 在不同级别下的判定
   print(np.can_cast(np.int32, np.int64))            # True  (safe)
   print(np.can_cast(np.float64, np.int32, "unsafe"))# True  (unsafe 才允许)
   print(np.can_cast(complex, float))                # False

   # astype 与 copy
   b = a.astype(np.int32, copy=False)
   print(np.shares_memory(a, b))   # True：同 dtype + copy=False → 不复制
   c = a.astype(np.int32)          # 默认 copy=True
   print(np.shares_memory(a, c))   # False：总是复制
   ```
3. **需要观察的现象**：`copy=False` 仅在「目标 dtype 与原 dtype 相同」时才真正共享内存；否则仍会新建数组。
4. **预期结果**：按注释标注的 `True/False`。其余为标准提升结果。
5. 本地验证：上述 `can_cast` 返回值请实际运行确认；行为可在源码 docstring 中核对。

#### 4.1.5 小练习与答案

- **练习**：`np.can_cast('i8', 'f4')` 返回什么？为什么？`np.can_cast('i8', 'f4', 'same_kind')` 呢？
- **答案**：`'i8'→'f4'`（int64→float32）在 `safe` 下是 `False`，因为 64 位整数里有值（如 2^53 以上）无法精确表示成 float32；`same_kind` 也仍是 `False`——`same_kind` 只放宽「同 kind 内的窄化」（如 float64→float32），而 int→float 跨 kind，不在 `same_kind` 范围内，需要 `unsafe`。

---

### 4.2 提升规则 Python 层：promote_types / result_type / min_scalar_type

#### 4.2.1 概念说明

提升（promotion）的目标是：给定两个（或多个）dtype，找一个**最小的公共 dtype**，使双方都能安全转过去。NumPy 给了三个相关函数：

- `np.promote_types(type1, type2)`：**两个** dtype 之间的公共类型。纯 C 实现。
- `np.result_type(*arrays_and_dtypes)`：**任意多个**数组/dtype 的结果类型，并且会考虑「Python 标量的弱类型」规则（见 4.3）。它是 `+`/`*` 等 ufunc 运算实际用来确定输出 dtype 的函数。
- `np.min_scalar_type(value)`：对一个标量值，返回能装下它的最小 dtype（如 `min_scalar_type(10) → uint8`）。

#### 4.2.2 核心流程

提升可以理解成在一个二维「类型格」上取上确界：

- 一个维度是 **kind**：`bool < 整数 < 浮点 < 复数`（结果 kind 取两者中更高者）。
- 另一个维度是 **精度（位宽）**：结果精度要足够大，不能让任何一方丢值。

形式上，对两个 dtype \(d_1, d_2\)：

\[
\text{promote}(d_1, d_2) = \min\{\, d \mid d_1 \leq_{\text{safe}} d \;\text{且}\; d_2 \leq_{\text{safe}} d \,\}
\]

其中「\(d_1 \leq_{\text{safe}} d\)」表示「\(d_1\) 可以 safe-转成 \(d\)」。直观例子：

- `int8` 与 `int64` → `int64`（精度取大者，kind 相同）。
- `int8` 与 `uint8` → `int16`（同位宽的有符号/无符号互不包含，最小公共是 int16）。
- `int64` 与 `uint64` → `float64`（没有任何整数 dtype 能同时容纳 uint64 的全部值与负数，于是「升级」到 float64）。
- `int64` 与 `float16` → `float64`（int64 的精度要求浮点至少 64 位）。

完整格示意图见 [doc/source/reference/arrays.promotion.rst:135-163](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/doc/source/reference/arrays.promotion.rst#L135-L163)。

#### 4.2.3 源码精读

先看一个关键事实：`promote_types` **没有 Python 函数体**，它是直接从 C 扩展 `_multiarray_umath` 导入的，只把 `__module__` 改成 `'numpy'`：

[numpy/_core/multiarray.py:47](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/multiarray.py#L47) — `promote_types` 出现在 `__all__` 里。

[numpy/_core/multiarray.py:74](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/multiarray.py#L74) — `promote_types.__module__ = 'numpy'`。也就是说 `np.promote_types is np._core.multiarray.promote_types`，二者都指向 C 函数。

它的 C 入口和注册在 `multiarraymodule.c`：

[numpy/_core/src/multiarray/multiarraymodule.c:3573-3592](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/multiarraymodule.c#L3573-L3592) — `array_promote_types` 解析参数后调用真正的算法 `PyArray_PromoteTypes(d1, d2)`（第 3592 行 `ret = (PyObject *)PyArray_PromoteTypes(d1, d2);`）。

而 `result_type`、`min_scalar_type`、`can_cast` 用的是「派发器壳」模式——Python 函数体看起来「什么都没做」：

[numpy/_core/multiarray.py:713-748](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/multiarray.py#L713-L748) — `result_type` 的函数体只有 `return arrays_and_dtypes`。装饰器 `@array_function_from_c_func_and_dispatcher(_multiarray_umath.result_type)` 做两件事：(1) 收集参数里实现了 `__array_function__` 的对象（用于 NEP-18 调度，见 u7-l2）和 `like=` 支持；(2) 在没有重载时，调用 C 函数 `_multiarray_umath.result_type`。

[numpy/_core/multiarray.py:665-710](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/multiarray.py#L665-L710) — `min_scalar_type` 同样的壳，docstring 给出例子：`min_scalar_type(10) → uint8`、`min_scalar_type(-260) → int16`、`min_scalar_type(3.1) → float16`。

> 真正的算法都不在 Python 里。下一节我们直接读 C。

#### 4.2.4 代码实践

1. **实践目标**：用三个函数预测 dtype，体会「取上确界」。
2. **操作步骤**：
   ```python
   import numpy as np
   print(np.promote_types(np.int8, np.int64))    # int64
   print(np.promote_types(np.int8, np.uint8))    # int16
   print(np.promote_types(np.int64, np.uint64))  # float64
   print(np.promote_types(np.int64, np.float16)) # float64

   # result_type：多个参数
   print(np.result_type(np.int32, np.complex64)) # complex128
   print(np.min_scalar_type(10))                 # uint8
   ```
3. **需要观察的现象**：注意 `int32 + complex64` 不是 `complex64` 而是 `complex128`——因为 complex64 的实/虚部只有 float32 精度，装不下 int32 的全部值。
4. **预期结果**：按注释。
5. 待本地验证：请实际运行确认每个返回值。

#### 4.2.5 小练习与答案

- **练习 1**：`np.promote_types('>i8', '<c8')`（一个大端 int64、一个小端 complex64）结果是什么字节序？
- **答案**：`complex128`，且是**本机字节序**。`promote_types` 总是返回本机序类型（见 `numpy/_core/tests/test_numeric.py:1197-1203` 的 `test_promote_types_endian` 断言）。
- **练习 2**：为什么 `np.promote_types(np.int64, np.uint64)` 是 `float64` 而不是某个整数类型？
- **答案**：没有任何 NumPy 整数 dtype 能同时表示 uint64 的最大值（\(2^{64}-1\)）和负数；而 float64 的尾数有 53 位、加上它能表示远大于 \(2^{64}\) 的量级，被提升规则「破例」当作可接受结果（见 `arrays.promotion.rst:159-163`）。

---

### 4.3 NEP 50 在 C 层的真正实现：弱类型（weak）Python 标量

#### 4.3.1 概念说明

NEP 50 是 NumPy 2.0 起的新提升规则，核心一句话：

> **两个 NumPy dtype 运算永不丢精度；但 Python 标量（`int`/`float`/`complex`）参与运算时，NumPy 只看它的 kind，忽略它的精度和具体数值。**

这条规则带来了两个直接后果，也是 NEP 50 相对老规则（NumPy 1.x 的「基于数值的提升」）的最大变化：

1. **低精度数组与 Python 标量运算，保持数组 dtype**：
   ```python
   arr = np.array([3, 5, 7], dtype=np.int16)
   arr + 10          # 结果仍是 int16（而不是被提升成 int64）
   ```
   因为 `10` 是 Python int，只贡献 kind=int，精度由数组 `int16` 决定。

2. **Python 标量值装不下时报错或溢出**：
   ```python
   np.int8(1) + 1000   # 1000 超出 int8 范围 → OverflowError
   np.int8(100) + 100  # 结果超出 int8 → 得到 -56 并给出 RuntimeWarning
   ```

老规则（1.x）会先算 `min_scalar_type(1)`（= `uint8`），再把数组 int8 与之提升成 `int16`，于是 `arr_int8 + 1` 的结果 dtype 会「悄悄」变成 int16，令人意外。NEP 50 取消了这种「因数值而变」的行为。详见 [doc/source/reference/arrays.promotion.rst:66-92](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/doc/source/reference/arrays.promotion.rst#L66-L92)。

#### 4.3.2 核心流程

NEP 50 把 Python 标量称为**弱类型（weak type）**：它在提升时退化为一个「抽象 DType」——

- Python `int`    → 抽象 `PyArray_PyLongDType`
- Python `float`  → 抽象 `PyArray_PyFloatDType`
- Python `complex`→ 抽象 `PyArray_PyComplexDType`

抽象 DType **只有 kind、没有 descriptor（没有具体位宽）**。提升时：

1. 数组携带「真实 descriptor」（如 int8）；Python 标量只携带「抽象 DType」。
2. 用 `PyArray_PromoteDTypeSequence` 求所有 DType 的公共 DType。
3. 若结果是抽象 DType（说明全是 Python 标量，没有数组约束），就给它配一个默认 descriptor（如 Python int 默认 int64）。
4. 若结果 DType 是参数化的（如字符串/struct），再用 `common_instance` 合并具体参数。

伪代码：

```
对每个输入：
  若是 Python 标量：记 (abstract_DType, descriptor=None)
  否则：            记 (concrete_DType, descriptor=真实dtype)
common = promote_DType_sequence(所有 DType)
若 common 是抽象的：common = default_descriptor(common)
对参数化 common：用 common_instance 合并各 descriptor
```

#### 4.3.3 源码精读

NEP 50 的核心实现就在 `PyArray_ResultType`：

[numpy/_core/src/multiarray/convert_datatype.c:1661-1670](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/convert_datatype.c#L1661-L1670) — 这是 NEP 50 的「心脏」。它检查数组上的标志位：

```c
if (PyArray_FLAGS(arrs[i]) & NPY_ARRAY_WAS_PYTHON_INT) {
    all_DTypes[i_all] = &PyArray_PyLongDType;       /* 抽象 DType */
}
else if (PyArray_FLAGS(arrs[i]) & NPY_ARRAY_WAS_PYTHON_FLOAT) {
    all_DTypes[i_all] = &PyArray_PyFloatDType;
}
else if (PyArray_FLAGS(arrs[i]) & NPY_ARRAY_WAS_PYTHON_COMPLEX) {
    all_DTypes[i_all] = &PyArray_PyComplexDType;
}
else {
    all_descriptors[i_all] = PyArray_DTYPE(arrs[i]); /* 真实 descriptor */
    all_DTypes[i_all] = NPY_DTYPE(all_descriptors[i_all]);
}
```

注意上一行 [convert_datatype.c:1660](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/convert_datatype.c#L1660) 给 Python 标量把 descriptor 设成 `NULL`——这正是「只贡献 kind、不贡献精度」的代码体现。

随后 [convert_datatype.c:1677-1678](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/convert_datatype.c#L1677-L1678) 调 `PyArray_PromoteDTypeSequence` 求公共 DType；如果结果是抽象的，[convert_datatype.c:1683-1692](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/convert_datatype.c#L1683-L1692) 给它配默认 descriptor。

两两提升的原子操作 `PyArray_PromoteTypes` 在同文件：

[numpy/_core/src/multiarray/convert_datatype.c:1074-1126](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/convert_datatype.c#L1074-L1126) — 关键三步：(1) L1080-1090 相同输入的快速路径（保 metadata）；(2) [L1092](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/convert_datatype.c#L1092) `PyArray_CommonDType` 求两个 DType 的公共 DType 类；(3) [L1121](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/convert_datatype.c#L1121) `common_instance` 求公共实例（合并位宽/字节序等参数）。这套 `CommonDType` + `common_instance` 的两段式，就是「先定 kind、再定精度」的 C 实现。

#### 4.3.4 代码实践（本讲主实践任务）

1. **实践目标**：预测 `np.arange(3, dtype=np.int8) + 1` 与 `np.arange(3, dtype=np.int8) + np.int8(1)` 的结果 dtype，并用 NEP 50 解释。
2. **先预测，再验证**：
   ```python
   import numpy as np
   a = np.arange(3, dtype=np.int8)

   r1 = a + 1            # 1 是 Python int（弱类型）
   r2 = a + np.int8(1)   # np.int8(1) 是「强类型」NumPy 标量

   print(r1.dtype, r2.dtype)
   ```
3. **预期结果与解释**：两者**都是 `int8`**。
   - `a + 1`：`1` 是 Python int，按 NEP 50 退化为抽象 `PyLongDType`（只有 kind=int、无精度）。与 `int8` 提升时，精度由 `int8` 决定，结果是 `int8`。这正是 [arrays.promotion.rst:85-87](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/doc/source/reference/arrays.promotion.rst#L85-L87) 里 `arr_int16 + 10` 保持 `int16` 的同类例子。
   - `a + np.int8(1)`：双方都是「强类型」NumPy int8，`promote_types(int8, int8) = int8`。
   - **差异不在当前 dtype（两者现在相同），而在机制与历史**：`+1` 走弱类型路径（Python 标量贡献 kind），`+np.int8(1)` 走强类型路径（双方都带 descriptor）。在 NumPy 1.x 的老规则下，`+1` 会先算 `min_scalar_type(1)=uint8`，再 `promote_types(int8, uint8)=int16`，于是第一个表达式会得到 `int16`，第二个是 `int8`——NEP 50 正是为了消除这种「同一段代码因数值不同而 dtype 不同」的陷阱。
4. **延伸观察**（体会「装不下就报错/溢出」）：
   ```python
   # np.int8(1) + 1000      # → OverflowError（1000 超出 int8）
   print(np.int8(100) + 100)  # → -56，并伴随 RuntimeWarning
   ```
   见 [arrays.promotion.rst:103-119](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/doc/source/reference/arrays.promotion.rst#L103-L119)。
5. 待本地验证：请实际运行确认 `r1.dtype`、`r2.dtype` 以及溢出行为的告警文字。

#### 4.3.5 小练习与答案

- **练习 1**：`np.array([1], dtype=np.float32) + 10.0` 和 `np.array([1], dtype=np.float32) + np.float32(10.0)` 的结果 dtype 分别是什么？
- **答案**：都是 `float32`。前者 `10.0` 是 Python float（弱类型），只贡献 kind=float；后者是强类型 float32。两者都由数组的 float32 决定精度。
- **练习 2**：`np.int16(1) + 1.0` 的结果 dtype 是什么？为什么不是 float32？
- **答案**：`float64`。当 Python float 与 NumPy 整数运算时，结果恒为 `float64`（见 [arrays.promotion.rst:94-98](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/doc/source/reference/arrays.promotion.rst#L94-L98)），因为 Python float 的「默认精度」被定义为 float64。
- **练习 3**：`np.result_type(7, np.array([1], np.float32))` 与 `np.result_type(type(7), np.array([1], np.float32))` 有何不同？
- **答案**：前者 `float32`（`7` 是 Python int 值，弱类型，不影响精度）；后者 `float64`（`int` 是 Python **类型类**，会被转成默认整数 int64 再参与提升）。见 [arrays.promotion.rst:211-221](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/doc/source/reference/arrays.promotion.rst#L211-L221)。

---

### 4.4 dtype 工具：用 kind/str 检视提升结果（_dtype.py）

#### 4.4.1 概念说明

提升出结果后，我们常需要把它「说清楚」——是哪种 kind、字符串怎么写。`numpy/_core/_dtype.py` 提供了 dtype 的字符串化与归类工具，是 `dtype.__str__`、`dtype.name` 背后的实现。它本身不做提升，但能帮你**检视和命名**提升结果。

#### 4.4.2 核心流程

- 每个 dtype 有 `kind`（单字符）和 `itemsize`（字节数）。
- `_kind_to_stem` 把 kind 字符映射成「词根」：`u→uint`、`i→int`、`f→float`、`c→complex`、`b→bool` 等。
- `dtype.name`（如 `float64`）= 词根 + 位宽（`8 * itemsize`）。

#### 4.4.3 源码精读

[numpy/_core/_dtype.py:8-20](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_dtype.py#L8-L20) — `_kind_to_stem` 字典，kind 字符到名称词根的映射。

[numpy/_core/_dtype.py:23-29](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_dtype.py#L23-L29) — `_kind_name(dtype)`：取 `dtype.kind`，查表返回词根；未知 kind 抛 `RuntimeError`。`dtype.name` 的「词根」部分就来自这里（见同文件 `_name_get`）。

[numpy/_core/_dtype.py:32-40](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_dtype.py#L32-L40) — `__str__`：决定 `str(np.dtype(...))` 的形式。对原生数值类型返回 `dtype.name`（如 `float64`），对小端序返回带字节序的短串（如 `<f8`）。

判别「dtype 属于哪一族」则在上一讲见过的 `numerictypes.py`：

[numpy/_core/numerictypes.py:411-474](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numerictypes.py#L411-L474) — `issubdtype(arg1, arg2)`：把参数归一化成标量类型后调 `issubclass`，故 `np.issubdtype(np.float64, np.floating)` 为 `True`，但 `np.issubdtype(np.float64, np.float32)` 为 `False`（同族不同宽不互为子类）。

#### 4.4.4 代码实践

1. **实践目标**：写一个工具函数，给定任意数组，报告其 dtype 的 kind、位宽、所属族。
2. **操作步骤**：
   ```python
   import numpy as np

   def describe(arr):
       d = arr.dtype
       family = None
       for fam in (np.integer, np.floating, np.complexfloating):
           if np.issubdtype(d, fam):
               family = fam.__name__; break
       if family is None and d.kind == 'b':
           family = 'bool'
       return dict(kind=d.kind, bits=d.itemsize*8, family=family,
                   name=d.name, str=d.str)

   for a in [np.array([1], np.int8),
             np.array([1], np.uint64),
             (np.arange(3, np.int8) + 1),                 # 4.3 的 r1
             np.promote_types(np.int64, np.float16)]:     # float64
       print(describe(np.array([0], dtype=a) if isinstance(a, np.dtype) else a))
   ```
3. **需要观察的现象**：4.3 的 `r1` 应被报告为 `kind='i', bits=8, family='integer'`；`promote_types(int64,float16)` 应是 `kind='f', bits=64, family='floating'`。
4. **预期结果**：与上一节提升结论一致。
5. 待本地验证：请实际运行确认 family 判定。

#### 4.4.5 小练习与答案

- **练习**：为什么 `str(np.dtype('float64'))` 在大多数平台显示成 `float64`，而 `str(np.dtype('<f8'))` 显示成 `<f8`？
- **答案**：`__str__`（`_dtype.py:37`）对「原生数值类型且本机字节序」走 `dtype.name` 分支返回长名 `float64`；一旦显式带非本机字节序（`<`/`>`），就走短串分支返回 `<f8`（见 `_scalar_str` 中 `dtype.byteorder not in ('=','|')` 的判断，`_dtype.py:152`）。

---

### 4.5 提升策略：为什么 NEP 50 不再有「开关」，以及精度错误的报告

#### 4.5.1 概念说明

需要澄清一个常见误解：**NumPy 2.x 不存在「提升策略开关」**。在 2.0 过渡期曾有过环境变量 `NPY_PROMOTION_STATE`（取值如 `weak`、`weak_and_warn`）帮助用户从老规则迁移，但它是临时设施，自 NumPy 2.2 起已被移除、设了也只告警。如今 NEP 50（`weak`）是**唯一**的提升模型。

那么「精度」相关的配置在哪？在于**浮点错误报告**：当转换或运算发生溢出/除零/非法值时，NumPy 用 `seterr`/`errstate` 控制是「忽略 / 警告 / 抛错」。这正好覆盖 4.3 里 `np.int8(100)+100` 那种「溢出得到 -56 并 RuntimeWarning」的行为。

#### 4.5.2 核心流程

- `np.seterr(all='warn')`：把所有浮点错误（含整数运算的溢出，整数按浮点方式处理）设为告警。
- `with np.errstate(over='raise'): ...`：在作用域内把溢出设为抛 `FloatingPointError`，离开后恢复。
- 自 2.0 起 `errstate` 线程/asyncio 安全（用 `contextvar` 实现）。

#### 4.5.3 源码精读

[numpy/__init__.py:912-917](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L912-L917) — 导入时检查 `NPY_PROMOTION_STATE`，只要不是默认的 `"weak"` 就发 `UserWarning`，明确写着「was a temporary feature for NumPy 2.0 transition and is ignored after NumPy 2.2」。这就是「没有开关」的代码证据。

错误报告配置在 `_ufunc_config.py`：

[numpy/_core/_ufunc_config.py:19-108](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_ufunc_config.py#L19-L108) — `seterr`：设置 divide/over/under/invalid 四类错误的处理方式。注意 docstring 说明「整数标量类型（如 int16）的运算也像浮点一样受这些设置影响」——这正是 4.3 中 `np.int16(32000)*3` 溢出会告警/抛错的依据。

[numpy/_core/_ufunc_config.py:386-489](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_ufunc_config.py#L386-L489) — `errstate` 上下文管理器：`__enter__` 里 [L486](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_ufunc_config.py#L486) `_extobj_contextvar.set(extobj)` 设当前错误策略，`__exit__` 里 `reset` 恢复。2.0 起它因此线程安全。

#### 4.5.4 代码实践

1. **实践目标**：用 `errstate` 把一次整数溢出从「告警」改成「抛错」，验证 `seterr` 与提升溢出的关系。
2. **操作步骤**：
   ```python
   import numpy as np

   # 默认：标量溢出会告警
   print(np.int8(100) + 100)   # -56 + RuntimeWarning

   # 改成抛错
   try:
       with np.errstate(over='raise'):
           np.int8(100) + 100
   except FloatingPointError as e:
       print("caught:", e)
   ```
3. **需要观察的现象**：作用域外是「结果 -56 + 告警」；作用域内直接抛 `FloatingPointError`，离开后恢复告警。
4. **预期结果**：按注释。
5. 待本地验证：告警文字与异常消息请实际运行确认。注意：数组运算（如 `np.array(100, np.uint8) + 100`）默认**不**告警，只有标量运算才告警（见 [arrays.promotion.rst:132-133](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/doc/source/reference/arrays.promotion.rst#L132-L133)）。

#### 4.5.5 小练习与答案

- **练习**：设了 `os.environ["NPY_PROMOTION_STATE"]="weak_and_warn"` 再 `import numpy`，会发生什么？能让你回到「老提升规则」吗？
- **答案**：不能。自 2.2 起该变量被忽略，只会触发一个 `UserWarning`（`numpy/__init__.py:913-917`），提升行为依然是 NEP 50。它从来只是 2.0 迁移期的临时告警工具。

---

## 5. 综合实践

设计一个「类型侦探」小程序，把本讲知识串起来：给定两个操作数，预测并验证它们做 `+` 运算后的 dtype，并解释依据。

```python
import numpy as np

def investigate(a, b):
    # 1. 用 result_type 预测（考虑 Python 标量弱类型）
    predicted = np.result_type(a, b)
    # 2. 实际运算得到真实 dtype
    actual = (np.asarray(a) + np.asarray(b)).dtype
    print(f"result_type -> {predicted}")
    print(f"actual add  -> {actual}")
    print(f"agree: {predicted == actual}")
    # 3. 解释：双方是否有一方是 Python 标量？
    is_py = lambda x: isinstance(x, (int, float, complex))
    if is_py(a) or is_py(b):
        print("rule: NEP 50 弱类型——Python 标量只贡献 kind，精度由 NumPy 侧决定")
    else:
        print("rule: 两个 NumPy dtype——promote_types 取最小公共类型，不丢精度")

# 案例 A：低精度数组 + Python 标量 → 保持数组 dtype
investigate(np.array([1,2,3], dtype=np.int16), 10)
# 案例 B：两个 NumPy dtype
investigate(np.array([1], dtype=np.uint8), np.array([1], dtype=np.int8))   # → int16
# 案例 C：Python float + NumPy 整数 → float64
investigate(np.array([1], dtype=np.int16), 2.0)
# 案例 D：大数装不下 → 先观察 result_type，再观察运算是否报错
investigate(np.array([1], dtype=np.int8), 1000)  # result_type 给 int8，但 + 会 OverflowError
```

要求：
1. 对每个案例，先用本讲规则口头预测 `result_type` 的输出，再运行核对。
2. 对案例 D，解释为什么 `np.result_type(np.array([1],np.int8), 1000)` 能返回 `int8`、但实际 `+` 运算却抛 `OverflowError`（提示：`result_type` 只看 kind 不看数值，而运算要把 `1000` 真正塞进 int8 时才发现放不下）。
3. 用 4.4 的 `describe` 函数打印每个案例结果 dtype 的 kind/位宽/族，确认与你的提升推理一致。

> 待本地验证：案例 D 的 `OverflowError` 触发条件请实际运行确认；这与 [arrays.promotion.rst:103-112](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/doc/source/reference/arrays.promotion.rst#L103-L112) 的 `np.int8(1) + 1000` 一致。

## 6. 本讲小结

- **转换 vs 提升**：`astype`/`can_cast` 是「显式转换」，用 5 个级别（`no`/`equiv`/`safe`/`same_kind`/`unsafe`，见 `ndarraytypes.h:254-264`）描述安全性；`promote_types`/`result_type` 是「运算时自动求公共类型」。
- **Python 壳 + C 核心**：`promote_types` 是纯 C 函数（`multiarray.py:74` 改 `__module__`），`result_type`/`can_cast`/`min_scalar_type` 是「派发器壳」（函数体看似空，真身在 `_multiarray_umath`）。
- **NEP 50 核心**：Python 标量是「弱类型」，只贡献 kind、不贡献精度——其 C 实现是 `PyArray_ResultType` 里对 `NPY_ARRAY_WAS_PYTHON_INT/FLOAT/COMPLEX` 标志的判断，用抽象 DType 且 `descriptor=NULL`（`convert_datatype.c:1660-1670`）。
- **后果**：低精度数组与 Python 标量运算保持数组 dtype；Python 标量值装不下时报 `OverflowError` 或溢出告警。
- **没有提升开关**：`NPY_PROMOTION_STATE` 自 2.2 起被忽略，仅告警（`__init__.py:912-917`）；精度相关配置实际在浮点错误报告 `seterr`/`errstate`（`_ufunc_config.py`）。
- **dtype 工具**：`_dtype.py` 的 `_kind_to_stem`/`_kind_name` 把 kind 字符映射成名称词根，用于命名和检视提升结果。

## 7. 下一步学习建议

- **向 ufunc 内部走**：本讲的提升是 ufunc（如 `np.add`）运算前的「类型解析」步骤。下一单元 u4-l3「ufunc 内部实现与类型解析」会讲 `PyUFuncObject` 如何根据输入 dtype 选择具体 C 循环，与本讲的 `result_type` 直接衔接。
- **读 C 源码建议**：先读 `convert_datatype.c` 的 `PyArray_PromoteTypes`（L1074）和 `PyArray_ResultType`（L1618），再追 `PyArray_CommonDType` 与 `common_instance`，理解「先定 kind、再定精度」的两段式。
- **读 NEP 原文**：[doc/neps/nep-0050-scalar-promotion.rst](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/doc/neps/nep-0050-scalar-promotion.rst) 给出了从老规则迁移的全部动机与边界情况，配合 `arrays.promotion.rst` 的提升格示意图一起看。
- **进阶（u8-l3）**：当你学到自定义 dtype/DType API 时，会再次遇到 `__common_dtype__` 和 cast 安全级别——届时回看本讲的 `PyArray_CommonDType` 会有更深理解。
