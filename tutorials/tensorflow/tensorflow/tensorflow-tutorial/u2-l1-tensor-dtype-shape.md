# Tensor、dtype 与 TensorShape

## 1. 本讲目标

本讲是「张量与基本概念」单元的第一讲。学完后你应该能够：

- 理解 **Tensor（张量）** 作为 TensorFlow 计算数据载体的本质，并说清它由「数据类型」与「形状」两个侧面刻画。
- 掌握 **DType（数据类型）** 的类型系统：知道有哪些类型、它们如何与 C++ 枚举 `types_pb2.DataType` 和 numpy 类型一一对应，以及 `tf.as_dtype()` 的转换规则。
- 看懂 **TensorShape（形状）** 的内部表示，理解「完全已知 / 部分已知 / 完全未知」三种静态形状，以及 `rank`、`dims`、`as_list()`、`is_compatible_with()` 等关键概念。
- 能够用源码中的类型构造几种不同 dtype 的标量/向量，打印其 TensorShape，并解释 shape 与 dtype 的关系。

承接 u1 单元：在 u1-l5 我们已知道 `Session` 是驱动图计算的抽象入口，而「图」和「会话」操作的最终对象就是**张量**。本讲把镜头从「执行入口」拉近到「数据本身」，为后续 u2-l2（`constant_op`）、u2-l4（`Operation` 与 `Tensor` 的对象关系）打基础。

## 2. 前置知识

在进入源码前，先用三段大白话建立直觉。

**什么是张量？** 你可以把张量想象成一个「带元数据的多维数组」。普通的 numpy 数组只有数据本身，而 TensorFlow 的张量除了数据，还额外携带两件关键信息：它存的是**什么类型**的数（dtype），以及它是**几维、每维多大**（shape）。这两条元数据决定了它在设备上占用多少内存、能和哪些 op 搭配。本讲的核心就是这两条元数据的 Python 定义。

**静态形状 vs 动态形状。** 在 Eager（立即执行）模式下，一个张量的形状在它被创建那一刻就完全确定了。但在「图构建 / `tf.function` 追踪（tracing）」阶段，有些维度的大小可能还不知道（比如「batch 维」在追踪时未知）。TensorFlow 因此设计了一套**静态形状**表示，允许某些维度用 `None`（未知）占位。这套表示就是 `TensorShape`。这一点是理解后面所有形状推理（shape inference）代码的钥匙。

**类型系统的「单例」思想。** TensorFlow 有几十种数据类型，但代码里不希望为同一个类型反复创建对象。于是它在内部维护一张「枚举值 → DType 对象」的全局表（intern table），全程序里同一个类型永远返回同一个对象。理解这个设计后，你会明白为什么 `t.dtype` 之间的比较可以直接用 `==`，而且速度很快。

## 3. 本讲源码地图

本讲涉及的关键文件，都属于 `tensorflow/python/framework/`，即 Python 侧的「框架基础」目录（在 u1-l2 中我们已定位过：`python/` 是 Python API 实现）。

| 文件 | 作用 |
| --- | --- |
| [tensorflow/python/framework/dtypes.py](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/dtypes.py) | 定义 `DType` 类与全部数据类型常量（`float32`、`int64`…），以及 numpy/C++ 枚举之间的互转表与 `as_dtype()`。 |
| [tensorflow/python/framework/tensor_shape.py](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/tensor_shape.py) | 定义 `Dimension`（V1 遗留）与 `TensorShape` 类，表示张量的静态形状，提供兼容性判断与形状运算。 |
| [tensorflow/python/framework/ops.py](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/ops.py) | 定义现代 TF2 中真正的 Tensor 类型 `EagerTensor` 的 Python 方法，其中的 `dtype`、`shape` 属性把本讲的两个概念接到张量上（张量本身的细节留到 u2-l4）。 |

> 说明：本讲深入精读前两个文件（对应最小模块 `python.framework.dtypes` 与 `python.framework.tensor_shape`）；`ops.py` 仅引用其 `dtype`/`shape` 属性来串接「张量」这一主题。

## 4. 核心概念与源码讲解

### 4.1 Tensor：dtype 与 shape 的宿主

#### 4.1.1 概念说明

在现代 TensorFlow 2.x 中，用户实际接触到的张量对象是 **`EagerTensor`**（在 C++ 中定义，Python 方法 mixin 在 `ops.py`）。一个张量对象身上最核心的两条元数据就是：

- `t.dtype`：一个 `DType` 对象，描述元素的数值类型。
- `t.shape`：一个 `TensorShape` 对象，描述每个维度的大小。

可以把它记成一个三元组（先忽略设备和名字）：

\[ \text{Tensor} \approx (\text{dtype},\ \text{shape},\ \text{数据缓冲区}) \]

本节的目标不是讲透 `EagerTensor`（那是 u2-l4 的任务），而是让你看到：**dtype 和 shape 不是凭空出现的，它们是张量对象的属性，而这两个属性的实现正是本讲后两节的内容。**

#### 4.1.2 核心流程

读取一个张量的 dtype / shape 的过程：

1. 张量对象内部存有一个 C++ 侧的「数据类型枚举」和「形状元组」。
2. 访问 `t.dtype` 时，Python 层用枚举值到 intern table 里查出**唯一的 DType 对象**返回。
3. 访问 `t.shape` 时，Python 层把 C++ 的形状元组**懒构造**成一个 `TensorShape` 对象并缓存。

注意「懒构造」与「缓存」：shape 对象只在第一次被访问时才创建，之后复用，避免每次 `.shape` 都构造新对象。

#### 4.1.3 源码精读

`EagerTensor.dtype` 属性直接查 intern table，注释也写明这是性能敏感路径：

```python
# tensorflow/python/framework/ops.py
@property
def dtype(self) -> dtypes.DType:
  # Note: using the intern table directly here as this is
  # performance-sensitive in some models.
  return dtypes._INTERN_TABLE[self._datatype_enum()]
```

这行代码的意义：张量把「我是哪种类型」以 C++ 枚举形式存在 `self._datatype_enum()`，再通过 `dtypes._INTERN_TABLE`（下一节会精读）映射回那个全局唯一的 `DType` 对象。永久链接：

- [tensorflow/python/framework/ops.py:L466-L470](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/ops.py#L466-L470) — `EagerTensor.dtype`：用 intern table 把 C++ 枚举转成 DType。

`EagerTensor.shape` 属性则是懒构造 + 缓存：

```python
# tensorflow/python/framework/ops.py
@property
def shape(self) -> tensor_shape.TensorShape:
  if self._tensor_shape is None:  # 还没缓存过
    try:
      # `_tensor_shape` 在 C 中定义的 EagerTensor 上声明
      self._tensor_shape = tensor_shape.TensorShape(self._shape_tuple())
    except core._NotOkStatusException as e:
      raise core._status_to_exception(e) from None
  return self._tensor_shape
```

- [tensorflow/python/framework/ops.py:L597-L608](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/ops.py#L597-L608) — `EagerTensor.shape`：首次访问时把 C++ 形状元组构造成 `TensorShape` 并缓存。

这两段代码就是「张量 = dtype + shape + 数据」的落地证据：dtype 与 shape 都是从 C++ 内核的原始信息里「翻译」出来的 Python 对象。

#### 4.1.4 代码实践

**实践目标：** 直观感受张量的 dtype 与 shape 属性来自哪里。

**操作步骤（示例代码，需在已安装 TensorFlow 的环境运行）：**

```python
import tensorflow as tf

t = tf.constant([[1, 2, 3], [4, 5, 6]])
print("dtype =", t.dtype)      # 预期 tf.int32
print("shape =", t.shape)      # 预期 TensorShape([2, 3])
```

**需要观察的现象：** `t.dtype` 打印出来是 `tf.int32`，`t.shape` 打印出来是 `TensorShape([2, 3])`。对照上面的源码，`tf.int32` 正是 intern table 里的那个 DType 对象，而 `TensorShape([2, 3])` 则是懒构造出来的形状对象。

**预期结果：**

```
dtype = <dtype: 'int32'>
shape = TensorShape([2, 3])
```

> 若本地未安装 TensorFlow，运行结果标注为「待本地验证」，但你可以对照源码逻辑推导出上述输出。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 `EagerTensor.dtype` 要「直接用 intern table」而不是 `dtypes.DType(self._datatype_enum())` 每次新建？

**参考答案：** intern table 保证同类型全程序只有一个 DType 对象，省去对象创建开销，也让 `t1.dtype == t2.dtype` 这种比较既快又可靠（下文 `__eq__` 会用到）。注释里「performance-sensitive」正是这个意思。

**练习 2：** 第二次访问 `t.shape` 时，会再次执行 `TensorShape(self._shape_tuple())` 吗？

**参考答案：** 不会。`self._tensor_shape` 在第一次构造后被缓存，之后 `shape` 属性直接返回缓存对象。

---

### 4.2 DType 类型系统（python.framework.dtypes）

#### 4.2.1 概念说明

`DType` 描述「张量元素是什么类型」。它本身定义在 `dtypes.py`，但**真正的 C++ 实现来自** `_dtypes.DType`（C 扩展），`dtypes.py` 里的 `DType` 是在它之上做的 Python 包装：

```python
# dtypes.py
class DType(_dtypes.DType, trace.TraceType, trace_type.Serializable, metaclass=DTypeMeta):
```

- [tensorflow/python/framework/dtypes.py:L51-L70](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/dtypes.py#L51-L70) — `DType` 类声明：继承 C 扩展 `_dtypes.DType`，表示「张量元素类型」。

每一个具体类型（如 `int32`）都由 C++ 侧的一个枚举值 `types_pb2.DT_INT32` 唯一标识，再包成一个 `DType` 对象。常见的类型对应关系如下表：

| Python 常量 | C++ 枚举 | 说明 | numpy 对应 |
| --- | --- | --- | --- |
| `tf.float32` | `DT_FLOAT` | 32 位单精度浮点（默认浮点） | `np.float32` |
| `tf.float64` / `tf.double` | `DT_DOUBLE` | 64 位双精度浮点 | `np.float64` |
| `tf.float16` / `tf.half` | `DT_HALF` | 16 位半精度浮点 | `np.float16` |
| `tf.bfloat16` | `DT_BFLOAT16` | 16 位 brain float（深度学习常用） | `ml_dtypes.bfloat16` |
| `tf.int32` | `DT_INT32` | 有符号 32 位整数（默认整数） | `np.int32` |
| `tf.int64` | `DT_INT64` | 有符号 64 位整数 | `np.int64` |
| `tf.uint8` | `DT_UINT8` | 无符号 8 位整数 | `np.uint8` |
| `tf.bool` | `DT_BOOL` | 布尔 | `np.bool_` |
| `tf.string` | `DT_STRING` | 变长字节串 | `object` |
| `tf.complex64` | `DT_COMPLEX64` | 64 位复数 | `np.complex64` |
| `tf.resource` | `DT_RESOURCE` | 可变资源句柄（Variable 用） | — |
| `tf.variant` | `DT_VARIANT` | 任意类型（运行时才知） | — |

#### 4.2.2 核心流程

类型系统的运作可以分成三层映射，理解了这三张表，就掌握了 `dtypes.py` 的骨架：

```
types_pb2.DataType 枚举  ──(1)──>  DType 对象实例 (uint8/int32/float32…)
        │                              │
        │                              ├──(2)──> _INTERN_TABLE : 枚举 -> DType（单例）
        │                              └──(3)──> _TYPE_TO_STRING / _STRING_TO_TF : 枚举 <-> 名字
        │
        ├──(4)──> _TF_TO_NP : 枚举 -> numpy
        └──(5)──> _NP_TO_TF : numpy -> DType
```

- **(1) 实例化**：`int32 = DType(types_pb2.DT_INT32)`，把一个枚举值包成对象。
- **(2) Intern table**：`_INTERN_TABLE[枚举] = 对象`，保证「同一枚举永远返回同一对象」。
- **(3) 字符串表**：让 `tf.as_dtype("float")` 这类按名字查找成为可能，并支持别名（`"half"→float16`、`"float"→float32`、`"double"→float64`）。
- **(4)(5) numpy 互转**：使 TF 类型与 numpy 类型互相转换，是和 numpy 数据无缝对接的基础。

对外提供 `as_dtype(type_value)` 作为统一入口，它按「DType 对象 / numpy.dtype / 字符串 / Python 类型 / 整数枚举」的顺序依次尝试转换。

#### 4.2.3 源码精读

**(1) 一个具体类型是如何被定义并导出的。** 以 `int32` 为例：

```python
# dtypes.py
int32 = DType(types_pb2.DT_INT32)
doc_typealias.document(obj=int32, doc="Signed 32-bit integer.")
tf_export("dtypes.int32", "int32").export_constant(__name__, "int32")
```

- [tensorflow/python/framework/dtypes.py:L344-L346](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/dtypes.py#L344-L346) — 定义 `int32`：用枚举 `DT_INT32` 构造 DType，并通过 `@tf_export` 注册为 `tf.int32`（详见 u1 单元对 `tf_export` 的讲解）。

`float32`/`float64`/`bool`/`bfloat16` 等都遵循同一模式，集中在标准类型包装区：

- [tensorflow/python/framework/dtypes.py:L307-L394](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/dtypes.py#L307-L394) — 标准类型包装区：逐个用 `DType(types_pb2.DT_*)` 定义并 `tf_export`。

**(2) Intern table（单例表）。** 这是 4.1 节 `t.dtype` 查的那张表：

```python
# dtypes.py
# Maintain an intern table so that we don't have to create a large
# number of small objects.
_INTERN_TABLE = {
    types_pb2.DT_HALF: float16,
    types_pb2.DT_FLOAT: float32,
    ...
    types_pb2.DT_RESOURCE: resource,
    types_pb2.DT_VARIANT: variant,
    ...
}
```

- [tensorflow/python/framework/dtypes.py:L555-L624](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/dtypes.py#L555-L624) — `_INTERN_TABLE`：枚举值 → DType 对象的全局单例表，是 dtype 比较与性能优化的核心。

**(3) 与 numpy 的互转。** `_NP_TO_TF`（numpy → TF）和 `_TF_TO_NP`（TF 枚举 → numpy）让数据能在两边流转：

```python
# dtypes.py（节选）
_NP_TO_TF = {
    np.float16: float16,
    np.float32: float32,
    np.int32: int32,
    np.int64: int64,
    np.bool_: bool,
    ...
}
```

- [tensorflow/python/framework/dtypes.py:L735-L769](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/dtypes.py#L735-L769) — `_NP_TO_TF`：numpy 类型 → TF DType 的映射表。
- [tensorflow/python/framework/dtypes.py:L791-L857](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/dtypes.py#L791-L857) — `_TF_TO_NP`：TF 枚举 → numpy 类型，`DType.as_numpy_dtype` 属性就是查这张表。

**(4) `as_numpy_dtype` 与 `base_dtype` 属性。** DType 提供一组属性来读取类型的各种「视图」：

```python
# dtypes.py
@property
def as_numpy_dtype(self):
  return _TF_TO_NP[self._type_enum]
```

- [tensorflow/python/framework/dtypes.py:L121-L124](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/dtypes.py#L121-L124) — `as_numpy_dtype`：返回该类型对应的 numpy 类型。
- [tensorflow/python/framework/dtypes.py:L96-L108](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/dtypes.py#L96-L108) — `base_dtype`：TF2 中普通类型返回自身，仅用于 TF1 的「引用类型」兼容（见下文小贴士）。

**(5) 相等判断。** 因为有 intern table，`DType` 的相等只需比较内部枚举：

```python
# dtypes.py
def __eq__(self, other):
  if other is None:
    return False
  if type(other) != DType:
    try:
      other = as_dtype(other)
    except TypeError:
      return False
  return self._type_enum == other._type_enum
```

- [tensorflow/python/framework/dtypes.py:L264-L275](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/dtypes.py#L264-L275) — `__eq__`：先把对方转成 DType，再比枚举值。

**(6) 统一入口 `as_dtype`。** 它按多种可能的输入形式逐级尝试：

```python
# dtypes.py（节选逻辑）
@tf_export("dtypes.as_dtype", "as_dtype")
def as_dtype(type_value):
  if isinstance(type_value, DType):
    return _INTERN_TABLE[type_value.as_datatype_enum]   # 已是 DType：返回单例
  if isinstance(type_value, np.dtype):
    return _NP_TO_TF[type_value.type]                   # numpy.dtype
  try:
    return _ANY_TO_TF[type_value]                       # 字符串/Python类型/整数枚举
  except (KeyError, TypeError):
    pass
  ...
  raise TypeError(...)                                  # 都不行就报错
```

- [tensorflow/python/framework/dtypes.py:L885-L945](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/dtypes.py#L885-L945) — `as_dtype`：把任意可识别的形式（DType / numpy / 字符串 / 整数枚举）统一转换成 DType 对象。

> 小贴士（TF1 遗留，了解即可）：源码里还有一批 `_ref` 类型（`float32_ref`、`int32_ref`…，枚举值 > 100）和 `base_dtype` / `is_compatible_with`，它们是为 TF1 的「引用类型 `tf.compat.v1.Variable`」服务的。TF2 的 `tf.Variable` 已不再用引用类型，所以日常代码中你只会遇到普通类型，`base_dtype` 对普通类型就是它自己。见 [dtypes.py:L519-L553](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/dtypes.py#L519-L553)。

#### 4.2.4 代码实践

**实践目标：** 亲手用 `dtypes.py` 中的类型构造不同 dtype 的张量，并验证类型系统的三张表。

**操作步骤（示例代码）：**

```python
import tensorflow as tf
import numpy as np

# (a) 用不同 dtype 构造标量/向量
a = tf.constant(3, dtype=tf.int64)         # 标量，int64
b = tf.constant([1.0, 2.0, 3.0])           # 向量，默认 float32
c = tf.constant([True, False])             # 向量，bool

for name, t in [("a", a), ("b", b), ("c", c)]:
    print(name, "dtype =", t.dtype, "| numpy dtype =", t.dtype.as_numpy_dtype)

# (b) 验证 as_dtype 的多种输入形式
print(tf.as_dtype("float"))        # 字符串 -> float32
print(tf.as_dtype(np.int32))       # numpy -> int32
print(tf.as_dtype(2))              # 整数枚举 -> float64

# (c) 验证 intern table：同一个类型是同一个对象
print(tf.constant(1).dtype is tf.float32)   # True（同一单例对象）
```

**需要观察的现象：**
- (a) 中 `a` 的 dtype 是 `int64`，`as_numpy_dtype` 是 `np.int64`；`b` 默认是 `float32`。
- (b) `as_dtype("float")` 返回 `float32`（注意不是 `float64`），`as_dtype(2)` 返回 `float64`。
- (c) `is` 判断为 `True`，证明 `tf.float32` 是全局单例。

**预期结果：**

```
a dtype = <dtype: 'int64'> | numpy dtype = <class 'numpy.int64'>
b dtype = <dtype: 'float32'> | numpy dtype = <class 'numpy.float32'>
c dtype = <dtype: 'bool'> | numpy dtype = <class 'numpy.bool_'>
<dtype: 'float32'>
<dtype: 'int32'>
<dtype: 'float64'>
True
```

> 若本地未安装 TensorFlow，运行结果标注为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1：** 为什么 `tf.constant([1.0, 2.0]).dtype` 是 `float32` 而不是 `float64`？

**参考答案：** Python 浮点字面量 `1.0` 在 numpy 里默认是 `float64`，但 TensorFlow 的「默认浮点类型」被设计为 `float32`（深度学习中最常用、显存占用更小）。`constant_op.py`（u2-l2 会精读）在推导 dtype 时，会把 Python/numpy 浮点收敛到 `tf.float32`。

**练习 2：** 给定 `dt = tf.int32`，不查表，说出 `dt.as_numpy_dtype`、`dt.base_dtype` 分别返回什么。

**参考答案：** `as_numpy_dtype` 返回 `np.int32`；`base_dtype` 对普通类型返回自身 `tf.int32`。

**练习 3：** `_INTERN_TABLE`、`_NP_TO_TF`、`_TF_TO_NP` 三张表分别解决什么问题？

**参考答案：** `_INTERN_TABLE` 解决「同一枚举 → 同一对象」的单例与比较问题；`_NP_TO_TF` 解决「numpy 数据送进 TF 时如何确定类型」；`_TF_TO_NP` 解决「TF 张量转成 numpy 时用哪种 numpy 类型」。三者共同支撑了 TF 与 numpy 的无缝互操作。

---

### 4.3 TensorShape：静态形状表示与运算（python.framework.tensor_shape）

#### 4.3.1 概念说明

`TensorShape` 表示一个张量的**静态形状**。它是「静态」的，意味着它描述的是「在构建/追踪图时已知的信息」，而不一定是运行时的真实尺寸。源码 docstring 把形状明确分成三类：

- **完全已知（fully-known）**：维度数已知，且每一维大小已知。例：`TensorShape([16, 256])`。
- **部分已知（partially-known）**：维度数已知，但某些维大小未知（用 `None` 表示）。例：`TensorShape([None, 256])`。
- **完全未知（unknown）**：连维度数都未知。例：`TensorShape(None)`。

- [tensorflow/python/framework/tensor_shape.py:L747-L817](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/tensor_shape.py#L747-L817) — `TensorShape` 类 docstring：明确三种静态形状，并对比 `tf.shape(t)` 的动态形状。

理解这三类的关键是 `TensorShape` 的内部存储：它只持有一个字段 `_dims`：

- `_dims is None` → 完全未知（连 rank 都不知道）。
- `_dims` 是一个元组，如 `(16, 256)` 或 `(None, 256)` → rank 已知；元组里的 `None` 表示那一维未知。

> 历史包袱：TF1 里每一维是一个 `Dimension` 对象（`Dimension(256)`），TF2 把每一维简化成**整数或 `None`**，`Dimension` 退化为兼容用。本节以 TF2 行为为准，但会顺带说明 `Dimension`。

#### 4.3.2 核心流程

**形状的「秩」与「元素总数」。** 设形状为 \((d_0, d_1, \dots, d_{r-1})\)，则：

- 秩（rank）\( r = \text{len}(\_dims) \)；当 `_dims is None` 时 rank 为 `None`。
- 元素总数（num_elements）：当且仅当所有维都已知时才有定义

\[ \text{num\_elements} = \prod_{i=0}^{r-1} d_i \]

只要有一维是 `None` 或 rank 未知，`num_elements()` 就返回 `None`。

**形状的「兼容性」（compatibility）。** 这是形状推理最常用的判断。两个形状兼容，意味着存在**一个完全已知的形状**能同时被两者表示。规则简化为：

- 若两边 rank 都已知但不相等 → 不兼容。
- 逐维比较：只有「两维都已知且不等」才算冲突；任意一维为 `None`（未知）都视为「可以让步」。
- `TensorShape(None)`（rank 未知）与任何形状都兼容。

> 注意 docstring 的提醒：兼容关系是**自反、对称，但不可传递**。例如 `[32,784]` 与 `None` 兼容，`None` 与 `[4,4]` 兼容，但 `[32,784]` 与 `[4,4]` 不兼容。

**形状的合并（merge）。** `merge_with` 把两个形状的信息合并：同维取「已知的那一个」，若两维都已知却不等则报错。这是 op 在做形状推理时「把多个输入的形状信息收敛起来」的基础。

#### 4.3.3 源码精读

**(1) 内部存储与构造。** `TensorShape` 只用 `_dims` 一个字段；构造函数把多种输入归一成「元组 / None」：

```python
# tensor_shape.py
class TensorShape(trace.TraceType, trace_type.Serializable):
  __slots__ = ["_dims"]

  def __init__(self, dims):
    if isinstance(dims, (tuple, list)):           # 最常见：[2,3] 或 (None,256)
      self._dims = tuple(as_dimension(d).value for d in dims)
    elif dims is None:                            # 完全未知
      self._dims = None
    elif isinstance(dims, tensor_shape_pb2.TensorShapeProto):
      ...                                          # 从 protobuf 反序列化
    elif isinstance(dims, TensorShape):
      self._dims = dims._dims                      # 拷贝
    ...
```

- [tensorflow/python/framework/tensor_shape.py:L818-L861](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/tensor_shape.py#L818-L861) — `__init__`：把 list/tuple/None/proto/TensorShape 统一归一为内部 `_dims`（元组或 None）。

注意：传入的每个元素都经过 `as_dimension(d).value`。`as_dimension` 会把整数原样、把 `None` 转成「未知 Dimension」、把 `Dimension` 取其 value：

- [tensorflow/python/framework/tensor_shape.py:L728-L744](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/tensor_shape.py#L728-L744) — `as_dimension`：把任意值（int / None / Dimension）归一为 `Dimension`，`TensorShape` 内部最终只存整数或 None。

**(2) rank 与 dims 属性。**

```python
# tensor_shape.py
@property
def rank(self):
  if self._dims is not None:
    return len(self._dims)
  return None

@property
def dims(self):   # 已废弃，建议用 as_list()
  if self._dims is None:
    return None
  return [as_dimension(d) for d in self._dims]
```

- [tensorflow/python/framework/tensor_shape.py:L892-L916](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/tensor_shape.py#L892-L916) — `rank`：返回维度数或 `None`；`dims`/`ndims` 为兼容旧 API 的访问器。

**(3) 元素总数。** 只有完全已知时才有意义，否则返回 `None`：

```python
# tensor_shape.py
def num_elements(self):
  if self.is_fully_defined():
    return functools.reduce(operator.mul, self.as_list(), 1)
  else:
    return None
```

- [tensorflow/python/framework/tensor_shape.py:L991-L996](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/tensor_shape.py#L991-L996) — `num_elements`：所有维连乘，等价于 \(\prod_i d_i\)；只要有一维未知就返回 `None`。

**(4) 兼容性判断（形状推理核心）。**

```python
# tensor_shape.py
def is_compatible_with(self, other):
  other = as_shape(other)
  if self._dims is not None and other._dims is not None:
    if self.rank != other.rank:
      return False
    for x_dim, y_dim in zip(self._dims, other._dims):
      if x_dim is not None and y_dim is not None and x_dim != y_dim:
        return False
  return True
```

逻辑就是 4.3.2 里描述的：rank 不等直接否；逐维只在「双方都已知且不等」时才否；`None`（未知）永远让步。

- [tensorflow/python/framework/tensor_shape.py:L1324-L1370](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/tensor_shape.py#L1324-L1370) — `is_compatible_with`：判断两个（可能部分未知的）形状是否存在公共的完全已知表示。

**(5) 完全已知判断与转列表。** op 在需要把形状喂给只接受具体尺寸的逻辑前，会先确认「完全已知」：

```python
# tensor_shape.py
def is_fully_defined(self):
  return (self._dims is not None and
          all(dim is not None for dim in self._dims))

def as_list(self):
  if self._dims is None:
    raise ValueError("as_list() is not defined on an unknown TensorShape.")
  return list(self._dims)
```

- [tensorflow/python/framework/tensor_shape.py:L1417-L1420](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/tensor_shape.py#L1417-L1420) — `is_fully_defined`：rank 已知且没有 `None` 维。
- [tensorflow/python/framework/tensor_shape.py:L1431-L1442](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/tensor_shape.py#L1431-L1442) — `as_list`：转成 Python 整数列表；对完全未知形状会抛错。

**(6) 约束秩：`with_rank`。** 形状推理常要求「至少/正好是几维」，`with_rank` 通过「与一个全是 `None` 但指定 rank 的形状 merge」来实现：

```python
# tensor_shape.py
def with_rank(self, rank):
  try:
    return self.merge_with(unknown_shape(rank=rank))
  except ValueError:
    raise ValueError("Shape %s must have rank %d" % (self, rank))
```

其中 `unknown_shape(rank=rank)` 构造 `[None]*rank`（rank 已知但每维未知）。把它和自身 merge：若自身 rank 不匹配就会在 `merge_with` 里冲突报错。

- [tensorflow/python/framework/tensor_shape.py:L1109-L1127](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/tensor_shape.py#L1109-L1127) — `with_rank`：用 merge 强制约束秩。
- [tensorflow/python/framework/tensor_shape.py:L1553-L1573](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/tensor_shape.py#L1553-L1573) — `unknown_shape`：构造一个「rank 已知、各维未知」的形状，是 `with_rank` 的工具。

> TF1/TF2 行为切换：`TensorShape[i]` 在 TF1 返回 `Dimension` 对象，在 TF2 返回整数或 `None`。由 `enable_v2_tensorshape()` / `_v2_behavior` 控制，TF2 默认开启 V2 行为。见 [tensor_shape.py:L38-L88](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/tensor_shape.py#L38-L88) 与 [tensor_shape.py:L863-L890](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/tensor_shape.py#L863-L890)。

#### 4.3.4 代码实践

**实践目标：** 用 `TensorShape` 验证「完全/部分/完全未知」三类形状，并亲手验证兼容性与元素总数。

**操作步骤（示例代码）：**

```python
import tensorflow as tf

s_full = tf.TensorShape([2, 3])          # 完全已知
s_part = tf.TensorShape([None, 3])       # 部分已知
s_unk  = tf.TensorShape(None)           # 完全未知

print("ranks:", s_full.rank, s_part.rank, s_unk.rank)        # 2, 2, None
print("fully_defined:", s_full.is_fully_defined(), s_part.is_fully_defined())  # True, False
print("num_elements:", s_full.num_elements(), s_part.num_elements())           # 6, None

# 兼容性：[2,3] 与 [None,3] 兼容；[2,3] 与 [2,3,4] 不兼容
print(s_full.is_compatible_with(s_part))                     # True
print(s_full.is_compatible_with(tf.TensorShape([2, 3, 4])))  # False
print(s_unk.is_compatible_with(s_full))                      # True（未知兼容一切）

# 合并：[None,3] merge [2,None] -> [2,3]
print(s_part.merge_with(tf.TensorShape([2, None])).as_list())  # [2, 3]

# 强制秩：给完全未知形状约束 rank=2
print(tf.TensorShape(None).with_rank(2))                     # TensorShape([None, None])
```

**需要观察的现象：**
- `s_unk.rank` 为 `None`，`s_part.num_elements()` 为 `None`（因为含未知维）。
- `[2,3]` 与 `[None,3]` 兼容（`None` 让步），但与 `[2,3,4]` 不兼容（rank 不同）。
- `merge_with` 把两边的已知信息合并成 `[2, 3]`。
- `with_rank(2)` 把「完全未知」提升为「rank=2、各维未知」。

**预期结果：**

```
ranks: 2 2 None
fully_defined: True False
num_elements: 6 None
True
False
True
[2, 3]
TensorShape([None, None])
```

> 若本地未安装 TensorFlow，运行结果标注为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1：** `TensorShape([None, None]).num_elements()` 返回什么？为什么？

**参考答案：** 返回 `None`。因为 `is_fully_defined()` 为 `False`（存在未知维），`num_elements()` 在不是完全已知时直接返回 `None`，而不是去算部分乘积。

**练习 2：** 给出两个形状，它们都和 `TensorShape(None)` 兼容，但彼此不兼容。

**参考答案：** 例如 `TensorShape([32, 784])` 与 `TensorShape([4, 4])`。二者都和完全未知的 `None` 兼容，但彼此 rank 虽相同、对应维 `32≠4`、`784≠4`，故不兼容。这正说明兼容关系不可传递。

**练习 3：** 为什么 `with_rank` 用 `merge_with(unknown_shape(rank=rank))` 实现，而不是直接检查 `len(self._dims)`？

**参考答案：** 因为自身可能是**完全未知**（`_dims is None`），这时 `len()` 会出错。`unknown_shape(rank)` 提供一个「rank 已知、各维未知」的形状作为锚点，再通过 merge 既能给完全未知形状「补上 rank」，又能在 rank 冲突时统一报错，逻辑更健壮。

## 5. 综合实践

把 dtype 与 shape 串起来。请你完成下面这个小任务，验证「张量 = dtype + shape + 数据」并理解静态/动态形状的差异。

**任务：** 在 `tf.function` 中观察「静态形状」与「动态形状」的差别。

```python
import tensorflow as tf

@tf.function
def report(t):
    # 静态形状：tracing 时已知的信息（可能含 None）
    static_shape = t.shape
    # 动态形状：运行时用 tf.shape(t) 得到的真实尺寸张量
    dynamic_shape = tf.shape(t)
    print("  static  =", static_shape)
    print("  dynamic =", dynamic_shape)
    return dynamic_shape

print("调用 1：完全已知的输入")
report(tf.constant([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]))

print("调用 2：用 TensorSpec 指定部分已知形状来 trace")
cf = report.get_concrete_function(tf.TensorSpec(shape=[None, 3], dtype=tf.float32))
print(cf(tf.constant([[1.0, 2.0, 3.0]])).numpy())   # batch=1
print(cf(tf.constant([[1.0, 2.0, 3.0],
                      [4.0, 5.0, 6.0],
                      [7.0, 8.0, 9.0]])).numpy())  # batch=3
```

**你需要结合本讲知识回答：**

1. 在「调用 1」里，`static_shape` 是什么？它是 `TensorShape` 还是 tensor？为什么 tracing 期就能知道？
2. 在「调用 2」里，`static_shape` 出现了 `None`，说明它是哪一类 `TensorShape`？而 `tf.shape(t)` 返回的 `dynamic_shape` 为什么能给出真实 batch 数？
3. 对照源码解释：`t.shape` 走的是 `EagerTensor.shape`（懒构造 `TensorShape`），而 `tf.shape(t)` 走的是另一个 op；两者一个静态、一个动态，分别服务于什么场景？

**参考要点：**

1. 调用 1 中 `static_shape = TensorShape([2, 3])`，是 `TensorShape` 对象。因为输入完全已知，tracing 时 op 的形状推理函数就能算出结果（形状推理见 u4-l3）。
2. 调用 2 中 `static_shape = TensorShape([None, 3])`，属「部分已知」；`None` 是 batch 维。`tf.shape(t)` 是一个**运行时 op**，在真正执行时才读取真实尺寸，所以能返回 `[1, 3]` 或 `[3, 3]`。
3. `t.shape` 服务于「编译期/追踪期就能做的判断」（如维度兼容、是否可 reshape）；`tf.shape(t)` 服务于「依赖真实数据才能决定的逻辑」（如按 batch 动态切片）。这正是 docstring 里「static vs dynamic」的现实体现。

## 6. 本讲小结

- 现代 TF2 的张量对象是 `EagerTensor`，它的 `dtype` 与 `shape` 是两条核心元数据，分别由 `dtypes.py` 和 `tensor_shape.py` 定义；`ops.py` 中的 `dtype`/`shape` 属性把 C++ 内核信息翻译成这两个 Python 对象。
- **DType** 用 C++ 枚举 `types_pb2.DataType` 唯一标识，每种类型包成一个 `DType` 对象；通过 `_INTERN_TABLE`（单例）、`_NP_TO_TF`/`_TF_TO_NP`（numpy 互转）、`as_dtype()`（统一入口）构成完整类型系统。
- **TensorShape** 内部只有一个 `_dims` 字段：`None` 表示完全未知，元组表示 rank 已知，元组里的 `None` 表示该维未知；由此区分「完全/部分/完全未知」三类静态形状。
- 形状推理的两个核心操作是 `is_compatible_with`（兼容性，自反对称但不可传递）与 `merge_with`（信息合并）；`num_elements` 仅在完全已知时有值，等于各维连乘。
- 要区分**静态形状** `t.shape`（`TensorShape`，tracing 期已知，可能含 `None`）与**动态形状** `tf.shape(t)`（运行时 op，给出真实尺寸）。

## 7. 下一步学习建议

- 下一讲 **u2-l2 常量 op 与 `constant_op`** 会展示 `tf.constant` 是如何**推导**出一个张量的 dtype 与 shape 的——你会发现本讲的 `as_dtype`、`TensorShape` 正是被它调用的底层零件。
- 之后 **u2-l4 Operation 与 Tensor 的 Python 表示** 会深入 `ops.py`，把本讲只点到为止的 `EagerTensor` 完整讲透，建立「op 产出 tensor」的对象关系。
- 想提前感受形状推理如何串起整张图，可在阅读本讲后跳读 [tensorflow/core/framework/common_shape_fns.h](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/framework/common_shape_fns.h)，那是 C++ 侧形状推导函数的集合，与本讲 `TensorShape` 的「兼容/合并」概念一一对应（完整讲解见 u4-l3）。
