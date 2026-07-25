# Operation 与 Tensor 的 Python 表示

## 1. 本讲目标

学完本讲，你应当能够：

- 用一句话说清 `Operation`（计算动作）与 `Tensor`（数据）之间的对象关系：**一个 op 消费若干输入 Tensor、产出若干输出 Tensor**。
- 在 `tensorflow/python/framework/ops.py` 中准确定位 `Operation`、`SymbolicTensor`、`_EagerTensorBase` 三个类的定义，并知道用户眼中的 `tf.Tensor` 实际由哪几个类实现。
- 读懂 `Operation` 的核心属性 `inputs` / `outputs` / `name` / `type` / `graph` / `device`，理解其中哪些是 Python 实现、哪些来自 C++ 扩展。
- 看懂 `tensor.op` / `op.outputs` 这条**双向链接**，以及 `a + b` 这种 Python 运算符是如何被改写成一个真正的 `Operation` 的。

## 2. 前置知识

本讲默认你已经读过：

- **u1-l4 / u2-l1 / u2-l2**：知道 `import tensorflow as tf` 之后 `tf.*` 由各模块用 `@tf_export` 注册拼装；知道一个张量近似是「dtype + shape + 数据缓冲区」。
- **u2-l2（constant_op）**：知道 `tf.constant` 会按执行模式分派——Eager 模式立即产生 `EagerTensor`，Graph 模式则在默认图里建一个 `"Const"` 节点。

几个本讲会用到的术语，先用大白话解释：

- **计算图（Graph）**：把一次计算画成有向图。图里的**节点**是「做什么运算」，**边**是「运算之间流动的数据」。
- **Operation（op）**：图里的一个**节点**，代表一次计算动作，比如矩阵乘、加法。它**不存数据**，只描述「怎么算」。
- **Tensor（张量）**：图里的**边**，代表流动的数据。一个 Tensor 必然是**某个 op 的输出**。
- **C 扩展类型（C extension type）**：用 C/C++ 写好、注册进 Python 的类型（如 `PyOperation`、`PyTensor`）。它跑得快、能直接持有 C++ 内核指针，但 Python 这边只看到它的属性和方法。

> 关键直觉：**op 是动词，tensor 是名词**。`tf.matmul(a, b)` 这一句里，`matmul` 是 op（动作），`a`、`b` 和结果 `c` 都是 tensor（数据）。本讲要回答的核心问题就是：源码里这「动词」和「名词」是如何互相指认的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`tensorflow/python/framework/ops.py`](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/ops.py) | **本讲主角**。定义 `Operation`、`SymbolicTensor`、`_EagerTensorBase`，是「构造计算图」的核心模块（文件头注释即 `Classes and functions used to construct graphs.`）。 |
| [`tensorflow/python/framework/tensor.py`](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/tensor.py) | 定义抽象基类 `Tensor`（在 `ops.py` 里以 `tensor_lib` 名字导入）。它是所有「张量」共享的 Python 行为（`dtype`/`shape`/运算符）。 |
| [`tensorflow/python/client/tf_session_wrapper.cc`](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/client/tf_session_wrapper.cc) | C++ 桥接层。用 pybind11 把 `PyOperation`、`PyTensor` 注册成 Python 类型，并提供 `outputs`/`name`/`type`/`op` 等属性。 |
| [`tensorflow/python/ops/tensor_math_operator_overrides.py`](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/ops/tensor_math_operator_overrides.py) | 把 `+`、`-`、`==` 等 Python 运算符「接线」到 `tensor_lib.Tensor` 上，使其变成真正的 op。 |

> 说明：规格里写的「最小模块」是 `python.framework.ops`，本讲以此为主线。但因为现代 TF2 把 `Tensor` 基类拆到了 `tensor.py`、把属性实现留在了 C++ 桥接层，要讲清「op 与 tensor 的关系」就必须顺藤摸瓜读到这两个文件——这是**真实源码结构**决定的，不是额外发散。

## 4. 核心概念与源码讲解

### 4.1 「op 产出 tensor」心智模型与对象关系

#### 4.1.1 概念说明

TensorFlow 里几乎所有计算都遵循同一个心智模型：

> 一个 **Operation** 接收 **0 到 N 个输入 Tensor**，执行某种计算，产出 **0 到 M 个输出 Tensor**。

也就是说，**op 和 tensor 是「生产者—产品」关系**：

- 从 op 看：`op.inputs` 是它吃进去的 tensor 列表，`op.outputs` 是它吐出来的 tensor 列表。
- 从 tensor 看：每个 tensor 都有且只有一个**生产者** `tensor.op`，以及一个 `value_index` 表示「我是我这个 op 的第几个输出」。

用最简单的一行代码就能感受这种关系：

```python
c = tf.matmul(a, b)
# c 是一个 Tensor
# c.op 是一个 Operation，类型是 "MatMul"
# c.op.inputs == (a, b)   —— 它吃了 a 和 b
# c.op.outputs[0] == c    —— 它的第 0 个输出就是 c
```

#### 4.1.2 核心流程

一次「Python 调用 → 产生 op 和 tensor」的流程可以概括为：

```text
用户调用 tf.matmul(a, b)
        │
        ▼
1. 在「当前图」里新建一个 Operation 节点（类型 "MatMul"）
   - 记录输入：inputs = (a, b)
   - 记录属性：transpose_a、transpose_b 等
        │
        ▼
2. 节点根据「输出端点数量」产出 outputs 列表（MatMul 有 1 个输出）
        │
        ▼
3. 每个 output 是一个 Tensor，且自动记录：
   - tensor.op      = 刚才那个 Operation
   - tensor.value_index = 它是第几个输出
        │
        ▼
4. 把 outputs[0]（通常就一个）返回给用户，赋值给 c
```

注意第 3 步：**tensor 和 op 之间是双向指针**。这条双向链接是后续自动微分（u5-l1）、图优化、SavedModel 序列化能跑通的地基——它们都要沿着 `op.inputs` / `tensor.op` 在图里来回走。

#### 4.1.3 源码精读

先看 `Operation` 类的**官方定义文档**，它把上面整个心智模型浓缩成了一句话：

> `Operation` 是图中的一个节点，接收 0 或多个 `Tensor` 作为输入，产出 0 或多个 `Tensor` 作为输出。

见 [tensorflow/python/framework/ops.py:1150-1166](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/ops.py#L1150-L1166)：

```python
class Operation(pywrap_tf_session.PyOperation):
  """Represents a graph node that performs computation on tensors.

  An `Operation` is a node in a `tf.Graph` that takes zero or more `Tensor`
  objects as input, and produces zero or more `Tensor` objects as output.
  Objects of type `Operation` are created by calling a Python op constructor
  (such as `tf.matmul`) within a `tf.function` or under a `tf.Graph.as_default`
  context manager.

  For example, within a `tf.function`, `c = tf.matmul(a, b)` creates an
  `Operation` of type "MatMul" that takes tensors `a` and `b` as input, and
  produces `c` as output.
  ...
```

注意类签名 `class Operation(pywrap_tf_session.PyOperation)`：`Operation` **继承自一个 C 扩展类型 `PyOperation`**。这意味着它的部分能力（比如 `outputs`、`name`、`type`）直接来自 C++，而不是在 Python 里手写——这一点我们在 4.3 会展开。

而创建一个 `Operation` 的标准入口是类方法 `from_node_def`，它在末尾这样收尾（[tensorflow/python/framework/ops.py:1277-1279](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/ops.py#L1277-L1279)）：

```python
    c_op = _create_c_op(g, node_def, inputs, control_input_ops, op_def=op_def)
    self = Operation(c_op, SymbolicTensor)   # 注意第二个参数 SymbolicTensor
    self._init(g)
```

这里有两个关键信息：

1. 真正的 C++ 节点由 `_create_c_op` 建好（返回 `c_op` 句柄），Python 的 `Operation` 只是包一层。
2. 第二个参数 `SymbolicTensor` 是个**「张量工厂」**——告诉 Operation「你的输出端点要用哪个类来实例化」。这正是「op 产出 tensor」在源码里的落点：op 在创建时就被指定了「它产出的 tensor 是什么类型」。

#### 4.1.4 代码实践

**实践目标**：在源码里亲手定位 `Operation` 和「Tensor 相关类」，确认它们确实在 `ops.py` 中。

**操作步骤**：

1. 在仓库根目录用 Grep 查找类定义：
   ```bash
   grep -n "^class " tensorflow/python/framework/ops.py | grep -iE "operation|tensor"
   ```
2. 预期看到类似输出（行号以你本地为准）：
   ```
   260:class SymbolicTensor(pywrap_tf_session.PyTensor, tensor_lib.Tensor):
   358:class _EagerTensorBase(
   1150:class Operation(pywrap_tf_session.PyOperation):
   ```

**需要观察的现象**：

- 你会看到 `Operation`，但**找不到一个叫 `class Tensor` 的定义**。这就是现代 TF2 的一个重要事实：`ops.py` 里没有 `class Tensor`，只有 `SymbolicTensor`（图模式张量）和 `_EagerTensorBase`（Eager 张量的 Python 基类）。
- 真正的 `Tensor` 抽象基类在 `tensorflow/python/framework/tensor.py`，在 `ops.py` 顶部以 `tensor_lib` 名字导入。

**预期结果**：你能解释「为什么规格说『定位 Operation 与 Tensor 两个类』，但 `ops.py` 里却没有 `class Tensor`」——因为 `Tensor` 基类被重构到了 `tensor.py`，而用户实际拿到的张量对象是 `SymbolicTensor` 或 `EagerTensor`。这正是 4.2 要讲的内容。

#### 4.1.5 小练习与答案

**练习 1**：用一句话描述 `Operation` 与 `Tensor` 的关系。

> **参考答案**：`Operation` 是计算图里的计算节点（动词），它消费若干输入 `Tensor`、产出若干输出 `Tensor`；每个输出 `Tensor` 又通过 `.op` 反向指回它的生产者。二者互为「生产者—产品」。

**练习 2**：为什么 `from_node_def` 创建 `Operation` 时要传一个 `SymbolicTensor` 参数？

> **参考答案**：它告诉这个 op「你的输出端点应该实例化成哪种 Tensor 类」。这样 op 一被创建，就能自动产出正确类型的输出张量，无需调用方再手动转换。

---

### 4.2 Tensor 的 Python 表示：一个抽象基类，两个 C 实现

#### 4.2.1 概念说明

很多教程会告诉你「`tf.Tensor` 是一个类」。但在真实源码里，**`tf.Tensor` 是一张「接口契约」，由两个不同的具体类来实现**，取决于你处于哪种执行模式：

| 执行模式 | 用户拿到的对象 | 源码定义位置 | 继承关系 |
| --- | --- | --- | --- |
| Eager（默认，立即执行） | `EagerTensor` | C 扩展类型，Python 基类是 `ops.py` 的 `_EagerTensorBase` | `tensor_lib.Tensor`（+ C 内核） |
| Graph / `tf.function`（先建图后执行） | `SymbolicTensor` | `ops.py:260` | `pywrap_tf_session.PyTensor` + `tensor_lib.Tensor` |

两者的**共同基础**是 `tensor.py` 里的抽象基类 `Tensor`（在 `ops.py` 里叫 `tensor_lib.Tensor`）。它定义了所有张量**共享的 Python 行为**：`dtype` / `name` / `shape` 属性、`+ - * / ==` 等运算符、`eval()` 等。

> 为什么这么设计？因为 Eager 和 Graph 两种模式下，张量的「底层存储」完全不同（一个立即持有真实数值缓冲区，一个只是图里的一个符号占位），但**对用户暴露的 API 必须一致**——你写 `a + b` 或 `a.shape` 时不应关心现在是哪种模式。于是 TF 把公共 API 上提到 `tensor_lib.Tensor`，把模式差异下沉到两个 C 实现里。这是典型的「**接口与实现分离**」。

`tensor.py` 里 `Tensor` 类的文档说得很直白（[tensorflow/python/framework/tensor.py:139-207](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/tensor.py#L139-L207)）：它先讲清楚「一个 Tensor 有 dtype 和 shape」，然后提醒你在 Eager 模式下「你的 Tensor 实际上是 `EagerTensor` 类型」——这是内部细节，但能让你用到 `.numpy()`。

#### 4.2.2 核心流程

一个张量对象的「身份」可以这样理解（概念模型，非真实字段）：

```text
一个 tf.Tensor 对象  ≈  ( 产出它的 op, value_index, dtype, shape, [数据缓冲区] )
```

- 前 4 项两种模式都有；
- 「数据缓冲区」只有 Eager 模式真正持有（所以只有 `EagerTensor` 能直接 `.numpy()`）；
- Graph 模式的 `SymbolicTensor` 没有「当前数值」，它只是「图里第 `value_index` 条输出边」的符号代表，要等图被执行（u3-l2 Session）才有值。

`dtype` / `name` / `shape` 这三个属性在抽象基类里是这样声明的（[tensorflow/python/framework/tensor.py:413-447](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/tensor.py#L413-L447)）：

```python
  @property
  def dtype(self):
    """The `DType` of elements in this tensor."""
    return self._dtype

  @property
  def name(self):
    return self._name

  @property
  def shape(self) -> tensor_shape.TensorShape:
    """Returns a `tf.TensorShape` ..."""
    if self._shape_val is None:
      dims, unknown_shape = self._shape
      ...
    return self._shape_val
```

注意它们读取的是 `self._dtype` / `self._name` / `self._shape_val`——这些「下划线字段」由各自的 C 扩展子类（`PyTensor` 或 `EagerTensor`）在 C++ 层填好。基类只负责「统一对外口径」。

#### 4.2.3 源码精读

先看 `SymbolicTensor`——图模式张量（[tensorflow/python/framework/ops.py:259-268](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/ops.py#L259-L268)）：

```python
@tf_export("__internal__.SymbolicTensor")
class SymbolicTensor(pywrap_tf_session.PyTensor, tensor_lib.Tensor):
  """A symbolic tensor from a graph or tf.function."""

  def __new__(cls, op, value_index, dtype, unique_id=None) -> "SymbolicTensor":
    if unique_id is None:
      unique_id = uid()
    return pywrap_tf_session.PyTensor.__new__(
        SymbolicTensor, op, value_index, dtypes.as_dtype(dtype), unique_id)
```

读这段能得到三件事：

1. **多继承**：`SymbolicTensor` 同时继承 C 扩展类型 `PyTensor`（持有图节点句柄）和 Python 抽象基类 `tensor_lib.Tensor`（提供 `dtype`/运算符等 API）。这正是「C 实现 + Python 接口」的组合。
2. **构造参数**`(op, value_index, dtype)`：完美对应 4.1 的概念模型——一个符号张量就是「某个 op 的第 `value_index` 个输出」。
3. 它用 `__new__` 而非 `__init__`，因为 C 扩展类型通常要在 C 层分配内存。

再看 Eager 侧（[tensorflow/python/framework/ops.py:358-360](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/ops.py#L358-L360)）：

```python
class _EagerTensorBase(
    tensor_lib.Tensor, internal.NativeObj, core_tf_types.Value):
  """Base class for EagerTensor."""
```

`_EagerTensorBase` 同样以 `tensor_lib.Tensor` 为基，提供 Python 层的数值转换方法（`__int__`/`__float__`/`__bool__`/`_numpy()` 等，见 [tensorflow/python/framework/ops.py:362-395](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/ops.py#L362-L395)）。真正的 `EagerTensor` 是一个 C 扩展类型（由 eager 的 C++ 绑定创建），把 `_EagerTensorBase` 当作它的 Python 混入基类——所以你在交互式终端里会看到 `type(t)` 显示为 `...ops.EagerTensor`。

#### 4.2.4 代码实践

**实践目标**：亲眼看到「同一个 `tf.constant`，在两种模式下返回不同类型的对象」。

**操作步骤**（需要本地装好 tensorflow，或标注「待本地验证」）：

```python
import tensorflow as tf

# 1) 默认就是 Eager 模式
t = tf.constant([1, 2, 3])
print(type(t))            # 预期：<class '...EagerTensor'>
print(t.op.type)          # 预期：Const
print(t.numpy())          # 预期：[1 2 3]   （只有 EagerTensor 能直接 .numpy()）

# 2) 进入 Graph 模式：用 tf.function 触发 tracing
@tf.function
def f(x):
    return x + 1

# tracing 时函数体里的张量是 SymbolicTensor
tf.summary.trace_on(graph=True, profiler=False)
_ = f(tf.constant([1.0]))
print("traced")  # 在 f 内部打断点或加日志可见 SymbolicTensor
```

**需要观察的现象**：

- 第 1 段里 `type(t)` 是 `EagerTensor`，而 `t.op` 是一个 `Operation`（`type == "Const"`）——这验证了「Eager 模式下，tensor 也通过 `.op` 关联到产生它的 op」。
- 第 2 段在 `tf.function` 内部捕获到的张量会是 `SymbolicTensor`（可借由 `tf.identity` 加返回值打印确认）。

**预期结果**：你能用自己的话解释——用户写 `tf.constant(...)` 拿到的东西「都叫 tf.Tensor」，但底层类型随执行模式变化；这正是 4.2.1 表格里两种实现的体现。若本地未装 TF，可把上述脚本记为「待本地验证」并改为纯阅读型：对照 [ops.py:260](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/ops.py#L260) 与 [ops.py:358](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/ops.py#L358) 说出二者共同基类。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `SymbolicTensor` 和 `EagerTensor` 都要继承 `tensor_lib.Tensor`？

> **参考答案**：为了让用户在两种执行模式下写同一套 API（`a + b`、`a.shape`、`a.dtype`）。公共行为上提到基类 `tensor_lib.Tensor`，模式特有的存储差异下沉到各自 C 实现，是「接口与实现分离」。

**练习 2**：下面这句对不对？「`tf.constant([1,2,3])` 返回的对象，其 `type()` 一定是 `Tensor`。」

> **参考答案**：不对。默认 Eager 模式下它是 `EagerTensor`；在 `tf.function` tracing 期间则是 `SymbolicTensor`。两者都不是字面意义上的 `class Tensor`，而是 `tensor_lib.Tensor` 的子类/混入。

---

### 4.3 Operation 解剖：inputs / outputs 与运算符如何变成 op

#### 4.3.1 概念说明

上一节讲了「tensor 是什么」，这一节回到 `Operation` 本身。`Operation` 最常用的属性有六个，但它们**分布在两个地方实现**，这是初学者最容易困惑的点：

| 属性 | 实现位置 | 含义 |
| --- | --- | --- |
| `inputs` | **Python**（`ops.py`） | 这个 op 吃进去的输入 tensor 列表 |
| `outputs` | **C++ 扩展**（`tf_session_wrapper.cc`） | 这个 op 产出的输出 tensor 列表 |
| `name` | C++ 扩展 | 节点名（如 `"MatMul"` 之前的唯一标识） |
| `type` | C++ 扩展 | op 类型字符串（如 `"MatMul"`、`"AddV2"`） |
| `graph` | Python（`_init` 里赋值） + C++ 属性 | 该 op 所属的 `Graph` |
| `device` | Python + C++ | 该 op 被分配到的设备名 |

为什么拆成两半？因为 `inputs` 需要做**惰性缓存**和图一致性检查（Python 逻辑更灵活），而 `outputs` / `name` / `type` 直接读取 C++ 节点的字段即可（C++ 更快、且字段本就在内核里）。这种「**Python 做策略，C++ 做数据**」的分工在 TF 源码里随处可见。

此外还有一个把「Python 体验」和「op 模型」缝合起来的机制——**运算符重载**。你写 `a + b`，Python 实际上调用 `a.__add__(b)`；而 TF 在导入时把这个方法**接线**成了一个会创建 `Operation` 的函数。于是 `+`、`-`、`*`、`==` 这些 Python 写法，背后都变成了真正的图节点。这就是为什么你几乎不用手写 `tf.add(a, b)`，写 `a + b` 就够了。

#### 4.3.2 核心流程

**op 的 `outputs` 是怎么被造出来的**（C++ 侧，[tf_session_wrapper.cc](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/client/tf_session_wrapper.cc)）：

```text
Operation 被创建 (传入工厂类 SymbolicTensor)
        │
        ▼
_init_outputs():
  num_outputs = 该 C++ 节点的输出端点数
  for i in 0 .. num_outputs:
      dtype = 第 i 个端点的类型
      outputs.append( SymbolicTensor(self, i, dtype) )   # 造出第 i 个输出张量
        │
        ▼
于是 op.outputs[i] 就是一个 SymbolicTensor，
且它的 .op == self，.value_index == i   （双向链接成立）
```

**`inputs` 是怎么读出来的**（Python 侧）：

```text
@property inputs:
  if 还没缓存过:
      对 C++ 节点的每个「入边」端点，问图要回对应的 tensor 对象
      缓存到 self._inputs_val
  return self._inputs_val
```

**`a + b` 是怎么变成 op 的**：

```text
a + b
 → Python 调用 a.__add__(b)
 → 该 __add__ 在导入时被「接线」为 _add_dispatch_factory（见 tensor_math_operator_overrides.py）
 → math_ops._add_dispatch(a, b)
 → 创建一个加法类 Operation，返回它的 outputs[0]
```

#### 4.3.3 源码精读

**（1）`inputs`：Python 实现 + 惰性缓存**

[tensorflow/python/framework/ops.py:1562-1571](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/ops.py#L1562-L1571)：

```python
  @property
  def inputs(self) -> Sequence[tensor_lib.Tensor]:
    """The sequence of `Tensor` objects representing the data inputs of this op."""
    if self._inputs_val is None:
      self._inputs_val = tuple(
          self.graph._get_tensor_by_tf_output(i)
          for i in pywrap_tf_session.GetOperationInputs(self._c_op))
    return self._inputs_val
```

要点：

- 它调用 C 函数 `GetOperationInputs(self._c_op)` 拿到**原始的 C++ 入边端点**，再用 `graph._get_tensor_by_tf_output` 把每个端点翻译回 Python 的 `SymbolicTensor` 对象。
- 结果被缓存进 `self._inputs_val`，第二次访问就直接返回——**惰性求值 + 缓存**，避免反复跨 Python/C 边界。

**（2）`outputs` / `name` / `type`：C++ 扩展实现**

这些属性**不在** `ops.py` 的 `Operation` 类体里，而是由 C++ 桥接层注册到 `PyOperation` 上。见 [tensorflow/python/client/tf_session_wrapper.cc:956-984](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/client/tf_session_wrapper.cc#L956-L984)：

```cpp
  c_op.attr("outputs") = property_readonly([](py::handle handle) {
    return AsPyTfObjectData<PyOperation>(handle)->outputs;
  });
  ...
  c_op.attr("type") = property_readonly([](py::handle handle) {
    return AsPyTfObject<PyOperation>(handle)->type();
  });
  c_op.attr("name") = property_readonly([](py::handle handle) {
    return AsPyTfObject<PyOperation>(handle)->name();
  });
```

而 `outputs` 列表本身，是在 op 创建时由 `_init_outputs` 预先填好的——[tensorflow/python/client/tf_session_wrapper.cc:616-622](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/client/tf_session_wrapper.cc#L616-L622)：

```cpp
  void _init_outputs() {
    int num_outputs = TF_OperationNumOutputs(tf_op());
    for (int i = 0; i < num_outputs; ++i) {
      int dtype = TF_OperationOutputType(TF_Output{tf_op(), i});
      data->outputs.append(data->tensor_fn(AsPyObject(this), i, dtype));
    }
  }
```

这里的 `data->tensor_fn` 就是从 `Operation(c_op, SymbolicTensor)` 传进来的**张量工厂**（即 `SymbolicTensor`）。所以 4.1 里那个「第二个参数」的作用在这里兑现：op 用它造出每一个输出张量 `(op=self, value_index=i, dtype)`。

**（3）反向链接：`tensor.op` 与 `tensor.value_index`**

张量侧也注册了指向生产者的属性——[tensorflow/python/client/tf_session_wrapper.cc:1076-1091](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/client/tf_session_wrapper.cc#L1076-L1091)：

```cpp
    c_tensor.attr("op") =
        property_readonly([](py::handle handle) -> py::handle {
          auto& op = AsPyTfObjectData<PyTensor>(handle)->op;
          if (op.ptr() != nullptr) {
            return op.borrow();
          }
          return py::none();
        });
    ...
    c_tensor.attr("value_index") = property_readonly([](py::handle handle) {
      return AsPyTfObject<PyTensor>(handle)->value_index();
    });
```

于是双向链接完整闭环：`op.outputs[i].op is op`，且 `op.outputs[i].value_index == i`。

**（4）运算符重载：把 `+` 接到 op 上**

抽象基类先声明「哪些运算符允许被改写」（白名单）——[tensorflow/python/framework/tensor.py:208-246](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/tensor.py#L208-L246)：

```python
  # List of Python operators that we allow to override.
  OVERLOADABLE_OPERATORS = {
      "__add__", "__radd__", "__sub__", ... "__mul__", ... "__eq__", ...
  }
```

真正「接线」发生在另一个模块里。比如二元加法（[tensorflow/python/ops/tensor_math_operator_overrides.py:109-110](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/ops/tensor_math_operator_overrides.py#L109-L110)）：

```python
override_binary_operator.override_binary_operator_helper(
    _add_dispatch_factory, "add"
)
```

而 `_add_dispatch_factory` 内部最终走向创建加法 op（[tensorflow/python/ops/tensor_math_operator_overrides.py:25-28](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/ops/tensor_math_operator_overrides.py#L25-L28)）：

```python
def _add_dispatch_factory(x, y, name=None):
  ...
  return math_ops._add_dispatch(x, y, name=name)
```

一元运算符则直接挂上（[tensorflow/python/ops/tensor_math_operator_overrides.py:168-180](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/ops/tensor_math_operator_overrides.py#L168-L180)），例如 `__neg__` 直接接到 `gen_math_ops.neg`：

```python
tensor_lib.Tensor._override_operator("__neg__", gen_math_ops.neg)
tensor_lib.Tensor._override_operator("__eq__", _tensor_equals_factory)
```

底层的 `_override_operator` 只是把函数 `setattr` 到类上（[tensorflow/python/framework/tensor.py:788-790](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/tensor.py#L788-L790)）：

```python
  @staticmethod
  def _override_operator(operator, func):
    _override_helper(Tensor, operator, func)
```

> 一句话总结这条链：**`a + b` → `a.__add__(b)` → `_add_dispatch_factory` → `math_ops._add_dispatch` → 构造一个加法类 `Operation` → 返回它的 `outputs[0]`**。Python 的自然写法，就此无缝接入 TF 的「op 产出 tensor」模型。

#### 4.3.4 代码实践

**实践目标**：列出 `Operation` 的 `inputs` / `outputs`，验证双向链接，并画出一句话关系图。

**操作步骤**（Eager 模式即可，需要本地 TF；若无环境，按下方「源码阅读型」替代）：

```python
import tensorflow as tf

a = tf.constant([1.0, 2.0])
b = tf.constant([3.0, 4.0])
c = a + b                      # 等价于 tf.add(a, b)

op = c.op                      # 取出生产 c 的 Operation
print("op.type      =", op.type)          # 预期：AddV2 或 Add
print("op.name      =", op.name)
print("len(op.inputs) =", len(op.inputs)) # 预期：2
print("op.inputs[0] is a ?", op.inputs[0] is a or tf.equal(op.inputs[0], a).numpy())
print("op.outputs[0] is c ?", op.outputs[0] is c)
print("c.value_index   =", c.value_index) # 预期：0
print("c.op is op      ?", c.op is op)    # 预期：True —— 双向链接闭环
```

**需要观察的现象**：

- `op.inputs` 长度为 2，对应 `a`、`b` 两个输入张量。
- `op.outputs[0]` 就是 `c`，而 `c.op` 又指回 `op`、`c.value_index == 0`——这正是 4.3.3 里 `_init_outputs` 和 `tensor.op`/`value_index` 共同保证的双向链接。
- `op.type` 不是 `"Add"` 就是 `"AddV2"`（取决于版本/分发路径），它来自 C++ 字段，对应 `type()` 的实现。

**一句话关系图**（请把它抄到笔记里）：

```text
        inputs=(a,b)            outputs=(c)
   a ──────► [ Operation: type=AddV2 ] ──────► c
   b ──────►                       c.op = op
                                    c.value_index = 0
   关系：op 产出 tensor；tensor.op 反指 op。
```

**预期结果**：你能指着这张图解释——左边的 `a`、`b` 是 `op.inputs`，右边产出的 `c` 是 `op.outputs[0]`，而 `c` 又通过 `.op` / `.value_index` 反向指回 op。这就是「op 与 tensor 对象关系」的完整答案。

> **若本地无 TF 环境（源码阅读型替代实践）**：打开 [ops.py:1562](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/ops.py#L1562) 的 `inputs` 属性与 [tf_session_wrapper.cc:956](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/client/tf_session_wrapper.cc#L956) 的 `outputs` 属性，用文字写出「前者在 Python、缓存在 `_inputs_val`；后者在 C++、由 `_init_outputs` 预填」，并据此手绘上面那张关系图。

#### 4.3.5 小练习与答案

**练习 1**：`op.inputs` 和 `op.outputs` 分别在哪个文件里实现？为什么不全放一处？

> **参考答案**：`inputs` 在 `ops.py`（Python，带 `_inputs_val` 缓存与翻译逻辑），`outputs`/`name`/`type` 在 `tf_session_wrapper.cc`（C++ 扩展，直接读内核字段）。不全放 Python 是因为 `outputs` 等是纯字段读取，放 C++ 更快；不全放 C++ 是因为 `inputs` 需要惰性缓存和图对象翻译，Python 写更灵活。体现「Python 做策略，C++ 做数据」。

**练习 2**：写 `a + b` 和写 `tf.add(a, b)` 有区别吗？从源码看它们的关系是什么？

> **参考答案**：对用户几乎等价。`a + b` 触发 `a.__add__(b)`，而 `__add__` 在 [tensor_math_operator_overrides.py:109](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/ops/tensor_math_operator_overrides.py#L109) 被接线到 `_add_dispatch_factory`，最终也走 `math_ops._add_dispatch` 构造加法 op。`tf.add` 是更显式的同一件事。

**练习 3**：`op.outputs[0].op is op` 一定为 `True` 吗？依据是哪段源码？

> **参考答案**：是。`outputs` 列表由 [tf_session_wrapper.cc:616-622](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/client/tf_session_wrapper.cc#L616-L622) 的 `_init_outputs` 用「本 op + 索引 i」造出每个张量；而 `tensor.op` 属性（[tf_session_wrapper.cc:1076-1083](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/client/tf_session_wrapper.cc#L1076-L1083)）返回的正是这个生产者，所以两者是同一对象。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「**读懂一行代码的整条链**」的小任务。

**任务**：解释 `c = tf.matmul(a, b) + 1.0` 这一行在源码层面发生了什么。要求覆盖：

1. **产生的对象**：这一行最终产生了几个 `Operation`？几个输出 `Tensor`？分别是什么类型（Eager 默认模式下用户拿到的是什么类）？
2. **属性核对**：取最后结果 `c`，写出 `c.op.type`、`c.op.inputs` 的长度、`c.op.outputs[0]` 与 `c` 的关系、`c.value_index`。
3. **双向链接**：用一句话说明 `c.op` 与 `op.outputs` 是怎么在 C++ 层互相绑定的（指出 `_init_outputs` 与 `tensor.op` 两段代码）。
4. **运算符**：`+ 1.0` 这一步，是走了 4.3 讲的哪条「接线」路径？最终调用了哪个工厂函数？

**提示步骤**：

- 先用 `tf.constant` 造 `a`、`b`，跑通这一行并打印上面各项（参考 4.3.4 脚本）。
- 对照 [ops.py:1277-1279](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/ops.py#L1277-L1279) 解释「op 创建时传入 `SymbolicTensor` 工厂」如何决定输出类型。
- 对照 [tf_session_wrapper.cc:616-622](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/client/tf_session_wrapper.cc#L616-L622) 与 [tf_session_wrapper.cc:1076-1083](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/client/tf_session_wrapper.cc#L1076-L1083) 解释双向链接。
- 对照 [tensor_math_operator_overrides.py:109](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/ops/tensor_math_operator_overrides.py#L109) 与 [tensor_math_operator_overrides.py:25-28](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/ops/tensor_math_operator_overrides.py#L25-L28) 解释 `+` 的接线。

**预期产出**：一张标注了「op 类型 / inputs / outputs / value_index」的小关系图，加一段 5～8 行的文字解释。若本地无法运行，明确标注「待本地验证」，但文字解释与关系图必须基于真实源码完成。

## 6. 本讲小结

- **核心心智模型**：`Operation` 是图里的计算节点（动词），`Tensor` 是它产出/消费的数据（名词）；一个 op 有 `inputs`（输入 tensor）和 `outputs`（输出 tensor）。
- **`tf.Tensor` 不是单一类**：抽象基类 `tensor_lib.Tensor` 在 `tensor.py`；图模式由 `SymbolicTensor`（`ops.py:260`）实现，Eager 模式由 `EagerTensor`（Python 基类 `_EagerTensorBase`，`ops.py:358`）实现——两者共享基类以提供一致 API。
- **属性分工**：`inputs` 在 Python（带 `_inputs_val` 惰性缓存），`outputs`/`name`/`type` 在 C++ 扩展（`tf_session_wrapper.cc`），体现「Python 做策略，C++ 做数据」。
- **双向链接**：op 创建时 `_init_outputs` 用张量工厂（`SymbolicTensor`）逐个造出输出；每个张量又经 `tensor.op` / `value_index` 反向指回生产者，形成闭环。
- **运算符即 op**：`a + b` 通过 `_override_operator` 接线（`tensor_math_operator_overrides.py`）变成构造加法 op 的调用，Python 自然写法无缝接入 op 模型。
- **诚实提醒**：规格说「在 `ops.py` 中定位 Tensor 类」，但现代 TF2 已把 `Tensor` 基类迁到 `tensor.py`；`ops.py` 里能定位到的是 `SymbolicTensor` 与 `_EagerTensorBase`。这是真实源码与历史描述的差异，理解它本身就是一次重要的源码阅读收获。

## 7. 下一步学习建议

- **进入 u3（计算图与执行模型）**：本讲只讲了「op 和 tensor 的对象关系」，还没讲它们如何被**执行**。下一单元 u3-l1 会讲 `Graph` / `Node` / `Edge` 的 C++ 数据结构与序列化形式 `GraphDef`；u3-l2 会展开 `DirectSession::Run` 如何沿着 `inputs`/`outputs` 把整张图跑起来。本讲的 `op.inputs`/`op.outputs` 正是那张图的 Python 投影。
- **深入 op 注册**：本讲看到 `op.type` 是个字符串（如 `"AddV2"`）。这些类型是怎么被声明和注册到全局表的？看 u4-l1（`REGISTER_OP` 机制）与 u4-l2（`OpKernel` 与 `Compute`），你会明白 `Operation` 背后那个 C++ 节点真正「算」的时候调的是谁。
- **延伸阅读**：可对照 [`tensorflow/python/framework/ops.py`](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/ops.py) 中 `Graph` 类（约 2030 行起）的 `create_op` 系列方法，看「op 构造」是如何被 `Graph` 托管的；以及 [`tensor.py`](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/tensor.py) 中 `Tensor` 的 `OVERLOADABLE_OPERATORS` 完整白名单。
