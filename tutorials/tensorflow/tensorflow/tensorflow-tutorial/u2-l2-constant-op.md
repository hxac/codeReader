# 常量 op 与 constant_op

## 1. 本讲目标

学完本讲后，你应该能够：

- 画出 `tf.constant([1, 2, 3])` 从 Python 调用到真正产生张量的完整调用链。
- 区分**「Python 层 op 包装」**与**「底层 op / 内核」**这两层概念：`tf.constant` 只是一层薄薄的 Python 函数，真正干活的是后面的 C++ 内核。
- 说清 TF2 默认的 **Eager（立即执行）** 路径和传统的 **Graph（图模式）** 路径在创建常量时分别走了哪条代码、各自产生了什么样的对象（一个是 `EagerTensor`，一个是图里的 `Const` 节点）。
- 解释 `tf.constant` 是如何自动**推导 dtype 与 shape** 的（比如为什么 `[1, 2, 3]` 出来是 `int32` 而不是 `int64`）。
- 理解 `tf.constant` 与 `tf.convert_to_tensor` 为什么「本质相同」，以及它们通过**注册表（registry）**统一在一起的机制。

本讲只聚焦一个最小模块：`python.framework.constant_op`。它是我们在 u2-l1 学到的「dtype + shape + 数据缓冲区」这一张量模型的**第一手构造现场**。

## 2. 前置知识

本讲是 u2-l1（Tensor、dtype 与 TensorShape）的直接后续，请确保你已经理解：

- **张量（Tensor）的三个要素**：dtype（元素类型）、shape（形状）、数据缓冲区（实际的数值）。`tf.constant` 就负责把一个 Python 值变成具备这三要素的张量。
- **`DType` 与 `TensorShape`**：u2-l1 精读了 `dtypes.py` 和 `tensor_shape.py`。本讲你会看到 `tf.constant` 如何在内部推导出这两个对象。
- **Eager 执行模式**：TF2 默认「立即执行」——写一行 op 代码立刻得到结果张量，而不是先把 op 攒进一张图里。这一点在本讲至关重要，因为 `constant_op.py` 恰恰在 Eager 和 Graph 两条路径之间分叉。

如果你还没建立「张量 = dtype + shape + buffer」的心智模型，建议先回到 u2-l1 复习。本讲不再重复 dtype/shape 本身的设计，而是讲「它们是怎么被一次 `tf.constant` 调用推算出来的」。

> 一个容易混淆的点：源码里有两类名字很像的概念——
> - **Tensor**：承载计算数据的对象（u2-l1 的主角）。
> - **Operation / op**：产生 Tensor 的计算节点。
>
> 本讲的关键正是揭示 `tf.constant` 这个 Python 函数，如何在两种执行模式下分别「构造一个 Tensor」（Eager）或「构造一个产生 Tensor 的 op 节点」（Graph）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tensorflow/python/framework/constant_op.py](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/constant_op.py) | 本讲主角。`tf.constant` 的 Python 包装实现，包含 Eager 与 Graph 两条路径的入口与分支逻辑。 |
| [tensorflow/python/framework/ops.py](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/ops.py) | 框架核心。提供 Graph 路径用的 `_create_graph_constant` / `_create_op_internal`、Eager 路径用的 `EagerTensor` 类型，以及 `convert_to_tensor`。 |
| [tensorflow/python/framework/tensor_util.py](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/tensor_util.py) | `make_tensor_proto` 所在文件，负责把 Python/numpy 值序列化成 `TensorProto`，并在此处完成 dtype/shape 的推导与「降精度」决策。 |
| [tensorflow/python/framework/constant_tensor_conversion.py](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/constant_tensor_conversion.py) | 为内置类型（list、tuple、普通对象）注册「转成常量」的转换函数，把 `tf.convert_to_tensor` 路由回 `constant_op.constant`。 |

阅读建议：先抓住 `constant_op.py` 这一条主线，`ops.py` 与 `tensor_util.py` 是它在两条分支上分别调用的「底层」，`constant_tensor_conversion.py` 则解释了 `tf.constant` 与 `tf.convert_to_tensor` 的关系。

## 4. 核心概念与源码讲解

### 4.1 Python 包装层：tf.constant → _constant_impl 的双分支

#### 4.1.1 概念说明

当你在代码里写下 `tf.constant([1, 2, 3])` 时，你调用的并不是某个庞大的 C++ 内核，而是一个**很薄的 Python 函数**。这个函数的职责只有两个：

1. **整理参数**：接收 `value`、`dtype`、`shape`、`name` 等 Python 参数。
2. **分派到正确的执行路径**：判断当前是 Eager 模式还是 Graph 模式，然后把活儿交给对应的底层实现。

这就是「Python 层 op 包装」的含义——`tf.constant` 本身不做计算，它只负责把 Python 世界的数据，按当前执行模式，翻译成内核能理解的形式。理解了这一点，你就不会再误以为「`tf.constant` 里藏着把数组变成张量的算法」；算法在更深一层。

#### 4.1.2 核心流程

整个分派逻辑可以浓缩为下面的伪代码：

```
tf.constant(value, dtype, shape, name)        # 公开 API（TF2 版）
        │
        ▼
_constant_impl(value, dtype, shape, name,
               verify_shape=False,
               allow_broadcast=True)
        │
        ├── ctx.executing_eagerly() 为真？   ── 是 ──► _constant_eager_impl(...)
        │                                                    │
        │                                                    ▼
        │                                          convert_to_eager_tensor(...)
        │                                          → 构造一个 EagerTensor（立即得到值）
        │
        └── 否（Graph 模式） ──────────────────► ops._create_graph_constant(...)
                                                     │
                                                     ▼
                                          在默认图里创建一个 "Const" 节点，
                                          把值序列化成 TensorProto 嵌进图的属性
```

关键在于：**同一个 Python 入口，根据执行模式走向两条完全不同的底层**。这是理解 `constant_op.py` 全篇的「钥匙」。

#### 4.1.3 源码精读

公开 API 有两个：`constant_v1`（TF1 兼容版，支持 `verify_shape`）和 `constant`（TF2 默认版）。两者都只是把参数转发给同一个 `_constant_impl`，区别仅在于 `verify_shape` 和 `allow_broadcast` 两个开关：

[constant_op.py:110-113](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/constant_op.py#L110-L113) — `constant_v1`：TF1 风格入口，允许严格校验形状（`verify_shape`），且不允许广播。

[constant_op.py:176-177](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/constant_op.py#L176-L177) — `constant`：TF2 默认入口，关闭 `verify_shape`、开启 `allow_broadcast`。我们日常用的 `tf.constant` 就是它。

真正的分派发生在这里：

[constant_op.py:277-291](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/constant_op.py#L277-L291) — `_constant_impl`：通过 `ctx.executing_eagerly()` 判断当前模式，走 Eager 分支 `_constant_eager_impl`，否则走 Graph 分支 `ops._create_graph_constant`。

注意第 282 行 `ctx.executing_eagerly()` 这个判断——它是整篇的分水岭。在 TF2 默认环境下它返回 `True`，所以 `tf.constant([1, 2, 3])` 默认走的是 Eager 路径（见 4.2）。只有当你显式进入图上下文（如 `tf.compat.v1.Graph().as_default()` 或被 `@tf.function` trace 时），它才会走 Graph 路径（见 4.3）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「同一个 `tf.constant` 在两种模式下走不同分支」。

**操作步骤**（假设本地已安装 TensorFlow）：

```python
# 示例代码
import tensorflow as tf
from tensorflow.python.eager import context

# 1) 默认 Eager 模式
print("executing_eagerly:", context.context().executing_eagerly())
c = tf.constant([1, 2, 3])
print(type(c).__name__, c)          # 期望: EagerTensor

# 2) 强制 Graph 模式（TF1 风格）
with tf.compat.v1.Graph().as_default():
    print("executing_eagerly:", context.context().executing_eagerly())
    g = tf.constant([1, 2, 3])
    print(type(g).__name__, g.op.type)   # 期望: Tensor Const
```

**需要观察的现象**：Eager 模式下产物是 `EagerTensor` 且直接打印出数值；Graph 模式下产物是一个图里的 `Tensor`，它的 `.op.type` 是字符串 `"Const"`，本身没有立即求出的值。

**预期结果**：第一种情况打印 `EagerTensor` 和数组；第二种情况打印 `Tensor` 和 `Const`。

> 若本地未安装或无法运行，可改为**源码阅读型实践**：在 `constant_op.py` 的 `_constant_impl`（第 277–291 行）旁标注「Eager 分支 / Graph 分支」，说明 `executing_eagerly()` 是开关。结果标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`tf.constant` 和 `tf.compat.v1.constant`（即 `constant_v1`）在参数上有什么关键差别？

**参考答案**：`constant_v1` 多一个 `verify_shape` 参数（默认 `False`，可设为 `True` 强制形状一致），且内部 `allow_broadcast=False`；TF2 的 `constant` 没有 `verify_shape`，且 `allow_broadcast=True`，允许标量/长度为 1 的向量广播到指定 `shape`。

**练习 2**：为什么 `constant_op.py` 的两个公开函数都把实际逻辑放进 `_constant_impl`，而不是各自实现一遍？

**参考答案**：为了消除重复——两条公开 API 的差异只是 `verify_shape`/`allow_broadcast` 两个开关，真正的「Eager/Graph 分派」逻辑完全相同。抽出 `_constant_impl` 既避免了代码重复，也保证两条入口的行为可被一处统一维护。

---

### 4.2 Eager 路径：把值直接包成 EagerTensor

#### 4.2.1 概念说明

在 TF2 默认的 Eager 模式下，「创建常量」的含义非常直白：把一个 Python 值（标量、列表、numpy 数组等）**立刻**包装成一个 C++ 对象 `EagerTensor`，并放到当前设备上。这里**没有**任何图节点被创建，也**没有**「先攒起来以后执行」的概念——调用返回时，张量的值就已经算好了。

`EagerTensor` 是一个 **C 扩展类型**（由 `pywrap_tfe` 在导入时动态生成），它直接持有底层 C++ 的张量句柄。所以这一路径的「底层」不是另一个 Python 函数，而是 C++ 的 `EagerTensor` 构造器。

#### 4.2.2 核心流程

```
_constant_eager_impl(ctx, value, dtype, shape, verify_shape)
        │
        ▼
convert_to_eager_tensor(value, ctx, dtype)        # 关键中间步骤
        │
        ├── 若 value 已是 EagerTensor：检查 dtype，原样返回
        ├── 若 value 是 numpy 数组：先 copy()（防止用户改原数组影响张量）
        └── 否则：调用 C++ 构造器构造一个 EagerTensor
                │
                ▼
        ops.EagerTensor(value, ctx.device_name, dtype)   ← 真正产生张量的底层
        │
        ▼
（回到 _constant_eager_impl：若指定了 shape 且不一致，再 reshape / fill）
```

也就是说，对于最常见的 `tf.constant([1, 2, 3])`（不指定 `shape`），**真正产生张量的那一行就是 `ops.EagerTensor(value, ctx.device_name, dtype)`**。

#### 4.2.3 源码精读

Eager 分支的入口：

[constant_op.py:294-300](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/constant_op.py#L294-L300) — `_constant_eager_impl`：先用 `convert_to_eager_tensor` 把值变成 EagerTensor，如果没指定 `shape` 就直接返回；指定了 `shape` 才走后续的 reshape/fill 逻辑。

真正「把值变成 EagerTensor」的函数：

[constant_op.py:74-107](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/constant_op.py#L74-L107) — `convert_to_eager_tensor`：这是 Eager 路径的核心。它处理三种情况（numpy 数组先复制、已是 EagerTensor 直接返回、其余交给构造器），最后一行 `return ops.EagerTensor(value, ctx.device_name, dtype)` 才是真正产生张量的地方。

注意第 91–95 行：如果传入的是 `numpy.ndarray`，函数会显式 `value.copy()`。注释解释了原因——EagerTensor 可能与输入数组**共享底层内存**，不复制的话用户改原数组就能「偷偷改动」一个看似不可变的张量。这是一个体现「不可变性」语义设计的细节。

`EagerTensor` 这个类型本身是在 `ops.py` 里由 C 扩展动态生成的：

[ops.py:730-731](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/ops.py#L730-L731) — `EagerTensor = ...TFE_Py_InitEagerTensor(_EagerTensorBase)`：以 Python 的 `_EagerTensorBase` 为基类，由 C 扩展工厂生成最终的 `EagerTensor` 类型。所以 `ops.EagerTensor(...)` 实际调用的是 C++ 构造逻辑，Python 层到此为止。

#### 4.2.4 代码实践（本讲主实践任务）

**实践目标**：追踪 `tf.constant([1, 2, 3])` 的调用链，写出它在默认 Eager 模式下最终调用了哪个底层函数来真正产生张量。

**操作步骤**：

1. 打开 `tensorflow/python/framework/constant_op.py`，定位 `constant`（第 176 行）。
2. 跟着 `return _constant_impl(...)`（第 273 行）跳到 `_constant_impl`（第 277 行）。
3. 因为默认 Eager，跳到 `_constant_eager_impl`（第 294 行）。
4. 顺着 `_constant_eager_impl` 第一行 `convert_to_eager_tensor(value, ctx, dtype)`（第 298 行）跳到 `convert_to_eager_tensor`（第 74 行）。
5. 读到最后 `return ops.EagerTensor(value, ctx.device_name, dtype)`（第 107 行）。

**需要观察的现象**：整条链上没有任何 `tf.Graph`、没有 `Const` 字样，最终落到一个 C 扩展类型的构造调用。

**预期结果**：调用链为
\[
\texttt{tf.constant} \;\to\; \texttt{\_constant\_impl} \;\to\; \texttt{\_constant\_eager\_impl} \;\to\; \texttt{convert\_to\_eager\_tensor} \;\to\; \texttt{ops.EagerTensor(value, device, dtype)}
\]
即**最终调用 `ops.EagerTensor(...)` 这个 C++ 扩展构造器**来真正产生张量。由于没传 `shape`，`_constant_eager_impl` 在第 299–300 行直接原样返回它。

> 结果标注：底层构造行为正确性「待本地验证」（可在 Python 里 `print(type(tf.constant([1,2,3])))` 看到 `EagerTensor`）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `convert_to_eager_tensor` 在传入是 numpy 数组时要先 `copy()`？

**参考答案**：因为 EagerTensor 可能和原数组共享底层内存缓冲区。如果不复制，用户随后修改原 numpy 数组就会改变这个「本应不可变」的 EagerTensor，破坏张量不可变性语义并引发难以排查的 bug。

**练习 2**：`tf.constant(some_eager_tensor)`（传入一个已有的 EagerTensor）会重复构造一个新的 EagerTensor 吗？

**参考答案**：不会。`convert_to_eager_tensor` 第 96–100 行检测到 `value` 已是 `EagerTensor` 时，只在 dtype 不一致时报错，否则原样返回。这也呼应了 `tf.constant` 文档里说的「对 eager Tensor 无副作用，甚至透传梯度」。

---

### 4.3 Graph 路径：创建 Const 节点并把值嵌进图

#### 4.3.1 概念说明

当处于 Graph 模式（比如 `@tf.function` 在 trace 时，或 TF1 的 `tf.Graph` 上下文），「创建常量」的含义和 Eager 完全不同：它**不在当下算出值**，而是在默认图里**新增一个类型为 `"Const"` 的节点**，并把常量的值序列化成一个 `TensorProto`，作为这个节点的**属性（attr）**嵌进图结构里。

这就是 `tf.constant` 名字里「constant」的真正来源——值被「钉死」在图的 `Const` 节点里，运行时再也不会改变。相比之下，`tf.fill` 是在运行时计算填充，二者在 `constant` 的文档里有明确对比。

#### 4.3.2 核心流程

```
ops._create_graph_constant(value, dtype, shape, name, ...)
        │
        ├── g = get_default_graph()                 # 拿到当前默认图
        ├── tensor_proto = tensor_util.make_tensor_proto(value, ...)   # 值 → TensorProto（含 dtype/shape 推导）
        ├── attrs = {"value": tensor_proto, "dtype": ...}              # 嵌进节点属性
        └── g._create_op_internal("Const", [], [dtype], attrs=attrs, name=name).outputs[0]
                          │
                          ▼
                  在图里建一个无输入、名为 "Const" 的 Operation，
                  取它的第一个输出 Tensor 返回
```

关键点：`Const` 节点**没有输入**（`inputs=[]`），它的「输入」其实是写死在 `value` 属性里的数据。这一节点的 op 类型字符串就是 `"Const"`，可以用 `is_constant` 通过 `op.type == "Const"` 来判定。

#### 4.3.3 源码精读

Graph 分支的实现在 `ops.py`（不在 `constant_op.py` 里，但由 `_constant_impl` 直接调用）：

[ops.py:333-346](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/ops.py#L333-L346) — `_create_graph_constant`：先取默认图，再用 `tensor_util.make_tensor_proto` 把值序列化成 `TensorProto`，把它和 dtype 一起打包成 `attrs`，最后调用 `g._create_op_internal("Const", [], ...)` 建节点，并 `.outputs[0]` 取出产出的 Tensor。

注意第 346 行 `g._create_op_internal("Const", [], [dtype_value.type], ...)`——第二个参数 `[]` 表示这个 op 没有输入张量，常量的「数据」全靠 `attrs["value"]` 这个 `TensorProto` 携带。

真正建节点的底层方法：

[ops.py:2775-2792](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/ops.py#L2775-L2792) — `Graph._create_op_internal` 的核心：用 `node_def = _NodeDef(op_type, name, attrs)` 构造节点定义，再 `Operation.from_node_def(...)` 创建 `Operation` 对象并登记到图里。`Const` 节点就是这样被「写」进图的。

判断一个 tensor 是不是常量，靠的就是 op 类型字符串：

[constant_op.py:326-331](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/constant_op.py#L326-L331) — `is_constant`：取 tensor 的 `.op`，判断 `op.type == "Const"`。这印证了 Graph 路径产出的就是类型为 `"Const"` 的图节点。

#### 4.3.4 代码实践

**实践目标**：在 Graph 模式下创建一个常量，观察它是一个 `Const` 节点，且值嵌在节点的属性里。

**操作步骤**：

```python
# 示例代码
import tensorflow as tf

with tf.compat.v1.Graph().as_default():
    c = tf.constant([1, 2, 3], name="my_const")
    op = c.op
    print("op.type:", op.type)                 # 期望: Const
    print("inputs:", [i.name for i in op.inputs])   # 期望: [] （无输入）
    # 取出嵌在节点属性里的值并还原
    proto = op.get_attr("value")
    print("recovered:", tf.make_ndarray(proto))    # 期望: [1 2 3]
```

**需要观察的现象**：`op.type` 是 `"Const"`，`inputs` 为空列表，而 `get_attr("value")` 能取回序列化的 `TensorProto`，经 `tf.make_ndarray` 还原成 `[1 2 3]`。

**预期结果**：节点无输入、类型为 `Const`、值作为属性可被读回。

> 若无法运行，可改为**源码阅读型实践**：在 `_create_graph_constant`（ops.py 第 333–355 行）旁批注「`attrs["value"]` 即嵌进 Const 节点的 TensorProto」，并指出 `inputs=[]`。结果标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`Const` 节点为什么没有 `inputs`？

**参考答案**：因为常量的值在**构图时**就已经确定，不需要依赖任何其他张量作为输入。它的数据直接以 `TensorProto` 的形式写在节点的 `value` 属性里（见 `_create_graph_constant` 的 `attrs`），所以 `inputs=[]`。

**练习 2**：同样是「造一个全 -1 的张量」，`tf.constant(-1.0, shape=[2,3])` 和 `tf.fill([2,3], -1.0)` 在 Graph 模式下产生的节点结构有何不同？

**参考答案**：`tf.constant` 产生一个 `Const` 节点，值（6 个 -1.0）全部嵌在节点属性里；`tf.fill` 产生的是一个 `Fill` op 节点，shape 和 value 是它的输入，要到**运行时**才计算填充。前者图更大但无运行开销，后者图更小但每次执行都要算。

---

### 4.4 dtype 与 shape 的自动推导（以及 tf.constant ≈ tf.convert_to_tensor）

#### 4.4.1 概念说明

无论是 Eager 还是 Graph 路径，都会遇到同一个问题：当用户**不指定** `dtype` 和 `shape` 时，`tf.constant` 怎么知道 `[1, 2, 3]` 该是什么类型、什么形状？

答案是统一的：**借助 numpy 来推导**。TensorFlow 会把 Python 值转成 numpy 数组，然后用 numpy 数组的 dtype/shape 作为依据，并施加两条 TF 特有的「偏好」规则：

- Python/numpy 默认整数是 `int64`，但 TF **偏好 `int32`**（只要不损失精度）。
- Python/numpy 默认浮点数是 `float64`，但 TF **偏好 `float32`**。

这两条规则解释了一个初学者常困惑的现象：`tf.constant([1, 2, 3])` 的 dtype 是 `int32`，而不是 numpy 默认的 `int64`。

此外，本节还会澄清一个重要的 API 事实：**`tf.constant` 与 `tf.convert_to_tensor` 本质相同**。`tf.constant` 的文档直言「它与 `tf.convert_to_tensor` 没有根本区别」。二者之所以能统一，是因为 TF 维护了一个**张量转换注册表（tensor conversion registry）**。

#### 4.4.2 核心流程

dtype/shape 推导（Graph 路径的 `make_tensor_proto`，Eager 路径在 C++ 侧用同样规则）：

```
make_tensor_proto(value, dtype=None, shape=None, ...)
        │
        ├── value → numpy 数组 nparray          （python list / scalar 先转 numpy）
        ├── 若 dtype 为 None：
        │       - nparray.dtype==float64 → 降为 float32
        │       - nparray.dtype==int64   → 降为 int32（仅在无损时）
        ├── shape 为 None 时：shape = nparray.shape
        └── 把 nparray 的字节 + shape + dtype 打包成 TensorProto
```

`tf.convert_to_tensor` 的统一机制：

```
tf.convert_to_tensor(value, ...)
        │
        ▼
tensor_conversion_registry.convert(value, ...)        # 按类型查表
        │
        ├── 对 list / tuple / 普通对象：命中 _constant_tensor_conversion_function
        │           │
        │           ▼
        │   回调 constant_op.constant(value, ...)     ← 转一圈又回到 tf.constant！
        └── 对已经是 Tensor 的：原样透传
```

也就是说，`tf.convert_to_tensor([1,2,3])` 最终也会调到 `constant_op.constant([1,2,3])`，二者共用同一套实现。

#### 4.4.3 源码精读

dtype/shape 推导的「降精度」决策写在 `tensor_util.py`：

[tensor_util.py:663-671](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/tensor_util.py#L663-L671) — `make_tensor_proto` 中的偏好规则：`float64` 降为 `float32`（无条件），`int64` 降为 `int32`（仅当 `np.array_equal` 确认无损时才降）。这正是 `[1,2,3]` 得到 `int32` 的根因。

shape 的来源：

[tensor_util.py:694-698](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/tensor_util.py#L694-L698) — 当 `shape` 为 None 时，直接用 numpy 数组的 `nparray.shape`。所以 `[1,2,3]` 的 shape 是 `(3,)`，完全来自 numpy。

注册表如何把 `convert_to_tensor` 路由回 `constant_op.constant`：

[constant_tensor_conversion.py:23-29](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/constant_tensor_conversion.py#L23-L29) — `_constant_tensor_conversion_function`：它的实现就是一行 `return constant_op.constant(v, dtype=dtype, name=name)`。

[constant_tensor_conversion.py:40-45](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/constant_tensor_conversion.py#L40-L45) — 把这个函数注册给 `(list, tuple)` 和兜底的 `object` 类型。

而 `tf.convert_to_tensor` 本身只是查表分发：

[ops.py:799-815](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/ops.py#L799-L815) — `convert_to_tensor`：把工作整体委托给 `tensor_conversion_registry.convert(...)`。由于 list/tuple/object 已被注册为「调 `constant_op.constant`」，所以对 `[1,2,3]` 而言，`convert_to_tensor` 与 `constant` 走的是同一条路。

> 这也解释了 `constant` 文档里那句「This function is not fundamentally different from `tf.convert_to_tensor`」——二者在底层汇合。区别仅在 API 表面：`tf.constant` 多一个 `shape` 参数，且不允许 symbolic tensor 透传；`tf.convert_to_tensor` 没有 `shape`、允许 symbolic tensor 透传。

#### 4.4.4 代码实践

**实践目标**：观察 dtype/shape 推导规则，并验证 `tf.constant` 与 `tf.convert_to_tensor` 的等价性。

**操作步骤**：

```python
# 示例代码
import tensorflow as tf
import numpy as np

# 1) dtype 推导：默认 int32 / float32
print(tf.constant([1, 2, 3]).dtype)        # 期望: <dtype: 'int32'>
print(tf.constant([1.0, 2.0]).dtype)       # 期望: <dtype: 'float32'>
print(tf.constant(np.array([1,2,3], dtype=np.int64)).dtype)  # 期望: <dtype: 'int64'>（已是 ndarray，不再降）
print(tf.constant([1, 2, 3]).shape)        # 期望: (3,)

# 2) 显式指定 dtype 会做类型转换
print(tf.constant([1, 2, 3], dtype=tf.float64).dtype)  # 期望: <dtype: 'float64'>

# 3) 等价性：convert_to_tensor 对 list 路由回 constant
a = tf.constant([1, 2, 3])
b = tf.convert_to_tensor([1, 2, 3])
print(a.dtype == b.dtype, a.shape == b.shape, tf.reduce_all(a == b).numpy())  # 期望: True True True
```

**需要观察的现象**：Python 列表 `[1,2,3]` 得到 `int32`；而**已经是** `int64` 的 numpy 数组保持 `int64`（因为 ndarray 分支不会再降，见 `make_tensor_proto` 第 634–638 行）；`tf.constant` 与 `tf.convert_to_tensor` 结果一致。

**预期结果**：dtype 为 `int32`/`float32`/`int64`，shape 为 `(3,)`，二者等价性全为 `True`。

> 结果标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `tf.constant([1, 2, 3])` 是 `int32`，但 `tf.constant(np.array([1,2,3], dtype=np.int64))` 是 `int64`？

**参考答案**：前者是 Python 列表，`make_tensor_proto` 走「先转 numpy」分支，触发「int64→int32 无损则降」的偏好规则；后者已经是 `int64` 的 ndarray，走 ndarray 分支（第 634–638 行），当用户没指定 dtype 时**原样保留**，不再降级。即「降精度」只对自动转换出来的默认 dtype 生效。

**练习 2**：用一句话说明 `tf.constant` 与 `tf.convert_to_tensor` 的关系，以及它们在 API 表面的关键差别。

**参考答案**：二者底层汇合——`convert_to_tensor` 通过张量转换注册表，把 list/tuple/普通对象路由回 `constant_op.constant`，所以本质相同；差别在表面：`tf.constant` 多一个 `shape` 参数、且拒绝 symbolic tensor（会把值嵌进图），而 `tf.convert_to_tensor` 没有 `shape`、允许 symbolic tensor 透传。

## 5. 综合实践

**任务**：把本讲四条主线串起来——追踪调用链、对比两种执行模式、观察 dtype/shape 推导、验证 `convert_to_tensor` 等价性。

**操作步骤**：

```python
# 示例代码
import tensorflow as tf
from tensorflow.python.eager import context

# === 第一步：默认 Eager 路径 ===
c = tf.constant([1, 2, 3])
print("[Eager] type:", type(c).__name__, "| dtype:", c.dtype, "| shape:", c.shape)
# 对照 4.2：产物应是 EagerTensor，底层调用 ops.EagerTensor(...) 构造

# === 第二步：Graph 路径，观察 Const 节点 ===
with tf.compat.v1.Graph().as_default():
    g = tf.constant([1, 2, 3], name="c")
    print("[Graph] op.type:", g.op.type, "| inputs:", [i.name for i in g.op.inputs])
    print("[Graph] recovered:", tf.make_ndarray(g.op.get_attr("value")))
    # 对照 4.3：op.type==Const，inputs 为空，值嵌在 value 属性里

# === 第三步：dtype/shape 推导 + convert_to_tensor 等价性 ===
print("[Infer] int list dtype:", tf.constant([1,2,3]).dtype)        # int32
print("[Infer] float list dtype:", tf.constant([1.0]).dtype)        # float32
same = tf.reduce_all(tf.constant([1,2,3]) == tf.convert_to_tensor([1,2,3]))
print("[Equiv] constant == convert_to_tensor:", same.numpy())       # True
```

**需要观察的现象与对应章节**：

| 现象 | 对应章节 |
| --- | --- |
| Eager 产物是 `EagerTensor`，底层是 `ops.EagerTensor(...)` | 4.2 |
| Graph 产物是 `Const` 节点、无输入、值在属性里 | 4.3 |
| `[1,2,3]`→`int32`、`[1.0]`→`float32` | 4.4 |
| `tf.constant` 与 `tf.convert_to_tensor` 结果相同 | 4.4 |

**预期结果**：能用自己的话复述「同一个 `tf.constant` 入口，在 Eager 下落到 `ops.EagerTensor` 构造器，在 Graph 下落到 `_create_graph_constant` 建一个 `Const` 节点；dtype/shape 由 numpy 推导并施加 int32/float32 偏好」。

> 若本地无法运行，请把上述脚本当作「阅读指南」：在 `constant_op.py` 里逐行对照标注每一步对应的源码位置，并把运行结果标注「待本地验证」。

## 6. 本讲小结

- `tf.constant` 是一层**薄薄的 Python 包装**，自身不做计算，只负责整理参数并按执行模式分派。
- 分派的开关是 `ctx.executing_eagerly()`：真→Eager 路径，假→Graph 路径（`_constant_impl`）。
- **Eager 路径**：经 `_constant_eager_impl` → `convert_to_eager_tensor`，最终调用 C++ 构造器 `ops.EagerTensor(value, device, dtype)` 立即产生张量，不建图。
- **Graph 路径**：经 `ops._create_graph_constant`，在默认图里建一个无输入的 `"Const"` 节点，值序列化成 `TensorProto` 嵌在节点 `value` 属性里。
- **dtype/shape 推导**借助 numpy，并施加「int64→int32（无损时）、float64→float32」的偏好规则，这就是 `[1,2,3]` 默认是 `int32` 的原因。
- **`tf.constant` 与 `tf.convert_to_tensor` 本质相同**：后者通过张量转换注册表把 list/tuple/普通对象路由回 `constant_op.constant`。

## 7. 下一步学习建议

- **下一步讲义 u2-l3（Variable 可训练变量）**：本讲的 `tf.constant` 产出的是**不可变**张量；`tf.Variable` 则引入了**可变、可训练**的状态（`assign`/`read`）。学完本讲再看 Variable，能更清楚地对照「不可变常量 vs 可变变量」。
- **u2-l4（Operation 与 Tensor 的 Python 表示）**：本讲已出现 `g.op`、`.outputs[0]`、`op.type`、`op.inputs`，下一讲将系统讲解 `ops.py` 里 `Operation` 与 `Tensor` 两个类的关系，把「op 产出 tensor」的心智模型彻底打通。
- **延伸阅读**：若你想进一步理解 Graph 路径里节点是如何被序列化、运行的，可先记下 `_create_op_internal` 与 `NodeDef` 这两个关键词，它们将在 u3（计算图与执行模型）单元被完整展开。
