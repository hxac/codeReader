# Variable 可训练变量

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说清楚 `tf.Variable` 与普通 `tf.Tensor` 的本质区别——**为什么它是有状态（stateful）的**。
2. 跟踪 `tf.Variable(x)` 从 Python 调用一路走到真正产生变量对象（`ResourceVariable`）的完整链路。
3. 解释「资源句柄（resource handle）」是什么，以及它如何把 Python 对象和底层的可变存储绑定起来。
4. 读懂 `assign` / `read_value` / `assign_add` 的实现，知道它们最终调用了哪个底层 op。
5. 理解为什么 `Variable` 能像 `Tensor` 一样参与加减乘除运算（算子重载机制）。

---

## 2. 前置知识

在进入本讲前，请确认你已经理解（来自 [u2-l1](u2-l1-tensor-dtype-shape.md) 和 [u2-l2](u2-l2-constant-op.md)）：

- **张量（Tensor）**：可以近似理解为「dtype + shape + 一块不可变的数据缓冲区」。`tf.constant` 创建的张量一经创建，值就不能改变。
- **dtype 与 TensorShape**：张量的两条核心元数据。
- **op 与 Python 包装**：Python 层的 `tf.*` 函数大多只是薄薄的包装，真正干活的是底层 C++ 内核。

本讲要回答一个关键问题：机器学习的模型参数（权重、偏置）在训练过程中**必须被不断更新**。如果所有张量都像 `constant` 一样不可变，每更新一次参数就要重建整个计算图，这显然不可行。`tf.Variable` 就是为了解决「**需要一个可以被反复读写的、持久化的存储**」这个问题而存在的。

> 关键直觉：`Tensor` 是**值**（value），创建后不可变；`Variable` 是**变量**（variable），像传统编程语言里的变量一样，可以 `assign` 改值、可以被多个计算共享引用。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `tensorflow/python/ops/variables.py` | 定义 `tf.Variable` 的**抽象基类** `Variable`、控制创建流程的元类 `VariableMetaclass`、以及把算子「像 Tensor 一样」重载到变量上的机制。注意：这里的方法几乎全是 `raise NotImplementedError`，真正实现在下一个文件。 |
| `tensorflow/python/ops/resource_variable_ops.py` | 变量的**真正实现** `ResourceVariable`，以及 `default_variable_creator_v2` 创建器、资源句柄（handle）的创建、`assign` / `read_value` 等具体方法。 |
| `tensorflow/python/framework/ops.py` | 提供 `_variable_creator_stack`（变量创建器栈），是「`tf.Variable(...)` 可以被策略替换实现」的支撑机制。 |

> 小提示：你会在 `variables.py` 里看到大量 `raise NotImplementedError`，这是**抽象基类**的标志。现代 TensorFlow 2.x 里，`tf.Variable(...)` 实际返回的是 `resource_variable_ops.py` 里的 `ResourceVariable` 对象。理解「抽象契约 + 具体实现」的两层结构是读懂本讲的关键。

---

## 4. 核心概念与源码讲解

### 4.1 变量是什么：状态化与可训练性

#### 4.1.1 概念说明

普通 `Tensor` 是**不可变的值**：你写下 `a = tf.constant([1,2,3])`，`a` 这块数据的值就固定了，任何「修改」实际上都只是创建了一个新张量。

`Variable` 不同，它维护的是**共享的、持久化的、可被程序反复修改的状态**。用源码注释里的话说：

> A variable maintains shared, persistent state manipulated by a program.

这有两层含义：

1. **持久化（persistent）**：变量的存储在创建后会一直存在，多次读取得到的是「当前最新值」，而不是某个固定快照。
2. **共享（shared）**：同一个变量可以被图里多个 op 引用；一次 `assign` 之后，所有引用它的 op 看到的都是新值。

此外，`Variable` 还有一个 `Tensor` 没有的属性：**可训练性（trainable）**。训练模型时，我们需要区分「需要被优化器更新的参数」和「只是计数器/状态的非参数变量」。`tf.GradientTape` 默认只会「监视（watch）」trainable 变量，为它们计算梯度。

#### 4.1.2 核心流程

一个变量的生命周期可以概括为：

```
创建(initial_value)  →  分配一块可变存储(handle)  →  把初值写入(assign)
        ↓
被图/ eager 引用  →  可随时 read_value 读取  /  assign 修改
        ↓
(若 trainable=True)  →  被 GradientTape 监视  →  训练中被 Optimizer 反复 assign
```

#### 4.1.3 源码精读

抽象基类 `Variable` 的 docstring 直接点明了变量的本质与「类型/形状一经确定就固定」的约定：

[variables.py:207-218](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/ops/variables.py#L207-L218) —— 这段类文档说明「变量维护共享、持久的状态；构造时给定 `initial_value`，此后类型和形状固定，只能用 assign 方法改值」。

注意 docstring 里给出的可训练性示例（`trainable=False` 的变量不会被 `GradientTape` 监视）。`trainable` 作为抽象属性声明在这里：

[variables.py:566-568](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/ops/variables.py#L566-L568) —— 抽象属性 `trainable`，具体取值由子类（`ResourceVariable`）提供。

`read_value` 和 `assign` 的**抽象契约**（签名 + 文档 + `raise NotImplementedError`）也定义在本文件：

[variables.py:547-556](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/ops/variables.py#L547-L556) —— `read_value` 契约：返回「当前上下文中读到的变量值」。

[variables.py:658-674](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/ops/variables.py#L658-L674) —— `assign` 契约：写入新值；`read_value=True` 时返回新值，`False` 时图模式返回赋值 op、eager 模式返回 `None`。

#### 4.1.4 代码实践

**实践目标**：用肉眼对比「不可变 Tensor」与「可变 Variable」的行为差异。

**操作步骤**（这是一个**源码阅读 + 心智建模**型实践，不需要运行）：

1. 打开 [variables.py:207-244](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/ops/variables.py#L207-L244) 的 docstring 示例。
2. 对照下面这段**示例代码**（非项目源码，仅用于说明），逐行写下你认为每一步发生了什么：

```python
# 示例代码（非项目源码）
import tensorflow as tf

c = tf.constant([1.0, 2.0])   # Tensor：值固定
v = tf.Variable([1.0, 2.0])   # Variable：分配可变存储，写入初值
v.assign([3.0, 4.0])          # 修改存储里的值
print(v.read_value())         # 读取当前最新值 -> [3.0, 4.0]
```

**需要观察的现象 / 预期结果**：

- `c` 上没有 `assign` / `read_value` 方法（它是 `Tensor`）。
- `v` 调用 `assign` 后，再 `read_value` 拿到的是新值——这正是「有状态」的体现。
- 如果你愿意本地运行，预期输出 `tf.Tensor([3. 4.], shape=(2,), dtype=float32)`。**待本地验证**具体打印格式。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `tf.constant` 创建的张量不能用来表示神经网络的权重？

> **参考答案**：权重在训练中要被优化器反复更新。`constant` 是不可变的值，每次「改值」都只能创建新张量，无法表达「同一块存储被多个 op 共享、并被原地更新」的语义。`Variable` 提供了可变、共享、持久化的存储，并通过 `assign` 实现原地更新，因此才适合做参数。

**练习 2**：`trainable=False` 的变量有什么典型用途？

> **参考答案**：用于记录不需要被梯度更新的状态，例如训练步数计数器（global step）、移动平均统计量等。设为 `False` 后 `GradientTape` 默认不会监视它，避免无意义的反向计算。

---

### 4.2 创建链路：从 tf.Variable 到 ResourceVariable

#### 4.2.1 概念说明

当你写下 `tf.Variable(x)` 时，它**并没有**直接 `new` 一个变量对象。TensorFlow 在这里用了一个巧妙的设计：**元类（metaclass）+ 创建器栈（creator stack）**。

这样做的目的是：让 `tf.distribute.Strategy`（分布式策略）等机制能够**拦截** `tf.Variable(...)` 的创建，把「普通的单卡变量」替换成「跨卡镜像的变量」，而调用方代码完全不用改。这是典型的「依赖注入 / 策略替换」思想。

#### 4.2.2 核心流程

```
tf.Variable(x)
   │  触发元类 __call__
   ▼
VariableMetaclass.__call__  ── 发现 _variable_call ──► Variable._variable_call
   │  遍历默认图上的 _variable_creator_stack
   ▼
default_variable_creator_v2  ── 栈空时的兜底创建器
   ▼
ResourceVariable(...)   ← 真正的变量类，返回给用户
```

栈是一个**后进先出**的列表，每个元素是一个 `(priority, creator)` 元组。`_variable_call` 从栈顶往栈底逐层包装：最外层的 creator 先被调用，它可以决定「自己创建」还是「调用 `next_creator` 委托给下一层」。当栈为空时，兜底的就是 `default_variable_creator_v2`。

#### 4.2.3 源码精读

元类 `VariableMetaclass` 拦截 `tf.Variable(...)` 的构造调用，优先交给 `_variable_call`：

[variables.py:195-204](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/ops/variables.py#L195-L204) —— `VariableMetaclass.__call__`：若类定义了 `_variable_call` 且它返回非 `None`，就用其结果替代正常实例化。

`_variable_call` 是一个 classmethod，它把创建工作转发给创建器栈。注意第一行 `if cls is not Variable: return None`——只有对基类 `Variable` 本身调用时才走这条注入路径，对子类（如 `ResourceVariable`）直接实例化，避免无限递归：

[variables.py:1340-1385](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/ops/variables.py#L1340-L1385) —— `_variable_call`：从兜底的 `default_variable_creator_v2` 出发，逐层用栈里的 creator 包装成 `previous_getter`，最后调用它。

`_variable_creator_stack` 定义在 `ops.py` 的 `Graph` 上，是一个线程本地的列表：

[ops.py:2308-2330](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/ops.py#L2308-L2330) —— `Graph._variable_creator_stack` 属性：懒初始化的线程本地列表，存储 `(priority, creator)` 元组。

栈空时的兜底创建器在 `variables.py` 里只是一个转发：

[variables.py:49-53](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/ops/variables.py#L49-L53) —— `default_variable_creator_v2` 转发到 `resource_variable_ops.default_variable_creator_v2`。

真正的创建器在 `resource_variable_ops.py`，它最终 `return ResourceVariable(...)`：

[resource_variable_ops.py:340-374](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/ops/resource_variable_ops.py#L340-L374) —— `default_variable_creator_v2`：把 kwargs 拆开后传给 `ResourceVariable`。

#### 4.2.4 代码实践

**实践目标**：验证 `tf.Variable(...)` 返回的对象的真实类型，并理解创建器栈的可替换性。

**操作步骤**（**源码阅读型**，可结合本地运行）：

1. 对照上面四个代码链接，画出「调用 → 元类 → 栈 → 创建器 → ResourceVariable」的关系图。
2. 若本地有 TensorFlow 环境，运行下面这段**示例代码**（非项目源码）观察类型：

```python
# 示例代码（非项目源码）
import tensorflow as tf
v = tf.Variable([1.0, 2.0])
print(type(v).__name__)   # 预期: ResourceVariable
```

**需要观察的现象 / 预期结果**：

- 打印出的类名应为 `ResourceVariable`，而非 `Variable`——证明抽象基类不是最终实例。
- 这解释了为什么 `variables.py` 里全是 `raise NotImplementedError`：契约在基类，实现在子类。**待本地验证**具体类型名。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_variable_call` 第一行要写 `if cls is not Variable: return None`？

> **参考答案**：创建器栈最终会调用 `ResourceVariable(...)` 来真正实例化。`ResourceVariable` 继承自 `Variable`，所以也会触发同一个元类 `__call__`。如果不加这个判断，就会无限递归（每次实例化 `ResourceVariable` 又回头走创建器栈）。判断「只有对 `Variable` 基类调用时才注入，对子类直接 `super().__call__` 正常实例化」打破了递归。

**练习 2**：分布式策略（如 `MirroredStrategy`）是如何做到「不改业务代码就替换变量实现」的？

> **参考答案**：它在 `strategy.scope()` 里向默认图的 `_variable_creator_stack` 压入一个自定义 creator。此后用户写的 `tf.Variable(...)` 会先命中这个 creator，由它决定创建一个跨卡镜像变量，而不是默认的单卡 `ResourceVariable`。这是用「创建器栈」实现的依赖注入。

---

### 4.3 资源句柄：Variable 与 Resource 的关联

#### 4.3.1 概念说明

现代 TensorFlow 的变量叫 **Resource**Variable，关键词是 **Resource（资源）**。这里的「资源」指的是一块**由运行时管理的、可变的状态单元**。

你可以这样理解：

- Python 里的 `Variable` 对象只是一个**外壳**，它持有的是一个「**句柄（handle）**」。
- **句柄**是指向那块可变存储的**引用 / 指针**（在图里它是一个特殊的 resource 类型张量）。
- `assign` / `read_value` 这些操作都是「拿着句柄去访问那块存储」。

这种「对象持句柄、句柄指存储」的设计有两个好处：

1. **可共享**：把同一个句柄传给多个 op，它们访问的就是同一份状态。
2. **图模式下可序列化**：句柄是图里的一个节点，多次执行之间状态得以保持。

> 对比 V1 的老式 `RefVariable`：它用「引用张量」实现可变性，在并发/分布式场景下容易出现数据竞争和难以分析的问题。`ResourceVariable` 用「资源句柄 + 显式的 read/assign op」替代，行为更可预测，这也是 TF2 默认采用它的原因。

#### 4.3.2 核心流程

构造变量时（`_init_from_args`）创建句柄并写入初值：

```
initial_value (Tensor) 
   │  转成 Tensor
   ▼
eager_safe_variable_handle(...)  ──►  var_handle_op(shape, dtype, shared_name)  => handle
   │
   ├─ eager 模式: assign_variable_op(handle, initial_value)   # 立刻写入
   └─ graph 模式: 构造 initializer_op(=assign) + is_initialized_op + 缓存的 read
```

#### 4.3.3 源码精读

句柄的核心创建逻辑是 `var_handle_op`：

[resource_variable_ops.py:173-179](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/ops/resource_variable_ops.py#L173-L179) —— `var_handle_op`：以 `shape` / `dtype` / `shared_name` 为参数，在运行时申请一块可变存储，返回指向它的 handle。

而 `eager_safe_variable_handle` 是它的上层封装，负责补上形状推断所需的 handle data：

[resource_variable_ops.py:201-210](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/ops/resource_variable_ops.py#L201-L210) —— `eager_safe_variable_handle`：从 `initial_value` 推断 dtype，调用 `var_handle_op` 产出句柄。

在 `_init_from_args` 里真正发起这次创建，并区分 eager / graph 两条写入路径：

[resource_variable_ops.py:2075-2080](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/ops/resource_variable_ops.py#L2075-L2080) —— 调用 `eager_safe_variable_handle` 拿到 handle。

[resource_variable_ops.py:2134-2145](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/ops/resource_variable_ops.py#L2134-L2145) —— **eager 模式**：直接 `assign_variable_op(handle, initial_value)` 把初值写进去，`initializer_op = None`（不需要延迟初始化）。

[resource_variable_ops.py:2096-2119](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/ops/resource_variable_ops.py#L2096-L2119) —— **graph 模式**：构造 `is_initialized_op`、`initializer_op`（一个延迟执行的 assign），以及一个供后续读取的 `read_variable_op`。

句柄通过 `handle` 属性暴露出来：

[resource_variable_ops.py:648-651](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/ops/resource_variable_ops.py#L648-L651) —— `handle` 属性：返回 `self._handle`，这是访问底层存储的「钥匙」。

#### 4.3.4 代码实践

**实践目标**：理解「Python 对象 = 外壳 + 句柄」，并区分 eager 与 graph 两种初始化时机。

**操作步骤**（**源码阅读型**）：

1. 阅读上面 6 处链接，回答：eager 模式下变量「何时被写入初值」？graph 模式下又是「何时」？
2. 用一句话写下「句柄」与「实际存储」的关系。

**需要观察的现象 / 预期结果**：

- eager 模式：构造 `tf.Variable` 的**那一行 Python 执行完**，初值就已经写进存储了（因为立刻 `assign_variable_op`）。
- graph 模式：构造时只**建图**（记录 initializer_op），真正写入要等到 Session 执行 initializer 后才发生。
- 句柄是「指向存储的引用」，多个 op 共享同一句柄即共享同一份状态。

#### 4.3.5 小练习与答案

**练习 1**：为什么 graph 模式下需要单独的 `is_initialized_op` 和 `initializer_op`，而 eager 模式下它们是 `None`？

> **参考答案**：graph 模式下，「构造变量」和「运行图」是分离的——构造只是往图里加节点，初值并不会立即计算。所以需要一个 `initializer_op`（本质是一次 assign），由用户在 `sess.run` 时显式触发；`is_initialized_op` 用来判断这块存储是否已被初始化过。eager 模式下「构造即执行」，初值在构造时已写入，自然不需要这些延迟初始化的 op。

**练习 2**：句柄（handle）和张量（Tensor）有什么关系？

> **参考答案**：句柄本身也是图里的一个张量，但它的 dtype 是特殊的 `DT_RESOURCE`，值是一个指向可变存储的不透明引用，而不是具体数值。读/写变量时，都是把这个 handle 作为输入交给 `read_variable_op` / `assign_variable_op`。

---

### 4.4 assign / read_value：读写状态的底层 op

#### 4.4.1 概念说明

变量的读写不是 Python 层的赋值，而是**真正的 op（操作）**：

- `read_value()`：读取当前值，返回一个新的 `Tensor`。
- `assign(value)`：写入新值，返回更新后的变量（或读取出的新值）。
- `assign_add(delta)` / `assign_sub(delta)`：在原值基础上加/减，返回新值。

这些方法最终都调用 `gen_resource_variable_ops` 里生成的底层 op（`read_variable_op` / `assign_variable_op` / `assign_add_variable_op`）。`gen_*` 前缀意味着它们是由 op 定义自动生成的 Python 绑定（参见后续 u4 单元），是通往 C++ 内核的最后一层 Python 代码。

#### 4.4.2 核心流程

读取与写入都以 handle 为中心：

```
读: read_value() -> _read_variable_op() -> read_variable_op(handle, dtype) -> Tensor
写: assign(v)    -> assign_variable_op(handle, value_tensor) -> (read_value? _lazy_read : op)
加: assign_add(d)-> assign_add_variable_op(handle, delta)   -> _lazy_read -> Tensor
```

注意 `assign` / `assign_add` 默认 `read_value=True`：它们会通过 `_lazy_read` 返回一个「绑定在赋值 op 之后」的读取结果，保证读到的是赋值之后的值；若 `read_value=False`，则图模式返回赋值 op 本身、eager 模式返回 `None`。

#### 4.4.3 源码精读

`read_value` 实际委托给 `_read_variable_op`，后者调用底层 `read_variable_op`：

[resource_variable_ops.py:871-884](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/ops/resource_variable_ops.py#L871-L884) —— `read_value`：包一层 `name_scope("Read")` 调 `_read_variable_op`，再 `identity` 以便放到当前设备上下文指定的设备上。

[resource_variable_ops.py:833-836](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/ops/resource_variable_ops.py#L833-L836) —— `_read_variable_op` 的核心：`gen_resource_variable_ops.read_variable_op(self.handle, self._dtype)`，即「拿句柄读 dtype 对应的存储」。

`assign` 先做形状兼容性校验，再调用底层 `assign_variable_op`：

[resource_variable_ops.py:1063-1102](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/ops/resource_variable_ops.py#L1063-L1102) —— `assign`：把 `value` 转 Tensor、校验 shape 兼容、调用 `assign_variable_op(handle, value_tensor)`；`read_value=True` 时用 `_lazy_read` 返回新值。

`assign_add` 结构类似，调用 `assign_add_variable_op`：

[resource_variable_ops.py:1028-1051](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/ops/resource_variable_ops.py#L1028-L1051) —— `assign_add`：`assign_add_variable_op(handle, delta)` 后默认 `_lazy_read`。

`_lazy_read` 返回一个特殊的 `_UnreadVariable`，它依附于某个父 op（如赋值 op），保证读取发生在该 op 之后：

[resource_variable_ops.py:1053-1061](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/ops/resource_variable_ops.py#L1053-L1061) —— `_lazy_read`：构造一个绑定到 `parent_op` 的 `_UnreadVariable`，建立控制依赖。

#### 4.4.4 代码实践

**实践目标**：动手创建变量、用 `assign` 修改、再用 `read_value` 读取，并解释「它为何不能像 constant 一样被当作不可变张量」。

**操作步骤**：

1. 若本地已安装 TensorFlow，运行下面这段**示例代码**（非项目源码）：

```python
# 示例代码（非项目源码）
import tensorflow as tf

# 1. 创建变量：分配可变存储，写入初值 [1, 2, 3]
v = tf.Variable([1, 2, 3], name="my_var")

# 2. 用 assign 修改存储里的值（底层调用 assign_variable_op）
v.assign([10, 20, 30])

# 3. 用 read_value 读取当前最新值（底层调用 read_variable_op）
print("当前值:", v.read_value())
print("dtype :", v.dtype)
print("shape :", v.shape)
print("trainable:", v.trainable)
print("handle 类型:", type(v.handle).__name__)
```

2. 对照本节的源码链接，逐条注释每一行对应调用了哪个底层 op。

**需要观察的现象 / 预期结果**：

- `read_value()` 输出 `[10 20 30]`，证明 `assign` 确实改了**同一块存储**。
- `v.dtype` / `v.shape` 与初值一致（类型、形状构造后固定）。
- `v.trainable` 默认为 `True`。
- `v.handle` 的类型名应包含 `Tensor`（句柄本身也是一种张量，dtype 为 resource）。
- **关于「为何不能当作不可变张量」**：因为变量的值随时可能被 `assign` / 优化器更新，`read_value()` 读到的只是一个**时刻的快照**；把它当成不可变值会让优化器无法更新参数，也会破坏「共享状态」语义。**待本地验证**具体打印格式。

**若无法运行**：退回到「源码阅读型实践」——对照 4.4.3 的四个链接，用自己的话复述 `v.assign(x); v.read_value()` 各自触发了哪一次底层 op，以及 `_lazy_read` 为什么要绑定控制依赖。

#### 4.4.5 小练习与答案

**练习 1**：`assign` 的 `read_value=True` 与 `read_value=False` 行为有何不同？

> **参考答案**：`read_value=True`（默认）会返回赋值**之后**的值（通过 `_lazy_read` 建立控制依赖，保证读到新值）；`read_value=False` 时不读值，图模式下返回赋值 op（用于只想触发赋值、不关心返回值的场景），eager 模式下返回 `None`。

**练习 2**：为什么说 `read_value` 返回的是一个「新的 Tensor」而不是变量本身？

> **参考答案**：`read_variable_op` 每次都从存储里**拷贝/读取**当前值，生成一个新的、不可变的 `Tensor`。这个 Tensor 只代表读取那一刻的快照，对它做运算不会写回变量；要改变量必须显式 `assign`。

---

### 4.5 像 Tensor 一样使用 Variable：算子重载与张量转换

#### 4.5.1 概念说明

`Variable` 虽然是「外壳 + 句柄」，但在代码里它能像 `Tensor` 一样直接参与运算，例如 `tf.matmul(w, x)` 里 `w` 是变量也能用。这背后是两套机制：

1. **算子重载（operator overloading）**：`Variable` 动态地把 `__add__` / `__mul__` / `__matmul__` 等方法绑定成「先 `value()` 读出张量，再调用对应的 Tensor 算子」。
2. **张量转换注册（tensor conversion）**：很多 op 内部会调用 `tf.convert_to_tensor`，把传入的对象统一转成 `Tensor`。变量通过注册自己的转换函数，能被自动转成「当前值的张量」。

#### 4.5.2 核心流程

```
v + x  ──(Variable.__add__)──►  tensor_lib.Tensor.__add__(v.value(), x)
                                       └─ v.value() 等价于一次 read（读当前值）
```

`_OverloadAllOperators` 会遍历 `Tensor.OVERLOADABLE_OPERATORS` 列表里的每一个算子名，为 `Variable` 生成对应的包装方法。

#### 4.5.3 源码精读

`_OverloadAllOperators` 负责批量绑定所有算子：

[variables.py:1123-1131](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/ops/variables.py#L1123-L1131) —— `_OverloadAllOperators`：遍历 `tensor_lib.Tensor.OVERLOADABLE_OPERATORS`，逐个调用 `_OverloadOperator`；切片 `__getitem__` 单独绑定到变量的切片辅助函数。

`_OverloadOperator` 为单个算子生成包装：核心是「先取 `a.value()`，再调用 Tensor 的同名算子」：

[variables.py:1133-1157](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/ops/variables.py#L1133-L1157) —— `_OverloadOperator`：定义 `_run_op(a, ...)` = `tensor_oper(a.value(), ...)`，然后用 `setattr` 绑到类上。注意 `__eq__` / `__ne__` 被故意排除（因为它们会在把变量放进集合/字典时被调用，调 `value()` 会和 GradientTape 产生无限递归）。

这行模块级语句在类定义之后执行，真正完成绑定：

[variables.py:1537](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/ops/variables.py#L1537) —— 模块加载时调用 `Variable._OverloadAllOperators()`，使所有算子在运行时生效。

`value()` 在 `BaseResourceVariable` 中返回（缓存的）读取结果：

[resource_variable_ops.py:653-658](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/ops/resource_variable_ops.py#L653-L658) —— `value()`：优先返回缓存的 `_cached_value`，否则调 `_read_variable_op()` 读取。

#### 4.5.4 代码实践

**实践目标**：验证「变量参与算术运算时，等价于先读值再做 Tensor 运算」。

**操作步骤**（**源码阅读型**，可结合本地运行）：

1. 阅读 4.5.3 的链接，确认 `v + x` 内部等价于 `tensor_lib.Tensor.__add__(v.value(), x)`。
2. （可选）本地运行下面这段**示例代码**（非项目源码）：

```python
# 示例代码（非项目源码）
import tensorflow as tf
v = tf.Variable([1.0, 2.0, 3.0])
y = v + tf.constant([10.0, 20.0, 30.0])   # 走 Variable.__add__
print(type(y).__name__, y)                 # 预期: 一个 Tensor（不是 Variable）
print(y.numpy())
```

**需要观察的现象 / 预期结果**：

- `y` 的类型应是 `Tensor`（`EagerTensor`），而非 `Variable`——因为算子返回的是读出值运算后的新张量。
- 输出值为 `[11., 22., 33.]`。**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `__eq__` / `__ne__` 被排除在自动重载之外？

> **参考答案**：把变量放进 `set` / 字典、或做成员判断时会隐式调用 `__eq__` / `__hash__`。如果 `__eq__` 走 `value()` 读取，在 `GradientTape` 上下文里会触发额外的记录甚至无限递归。所以这两个方法被单独处理（见 `__hash__` 抛出「unhashable」、`__eq__` 返回逐元素比较或对象同一性）。

**练习 2**：`v + x` 的结果为什么是 `Tensor` 而不是 `Variable`？

> **参考答案**：重载包装先 `v.value()` 读出一个 `Tensor`，再把它和 `x` 做张量加法，张量加法返回自然是 `Tensor`。变量只在被显式 `assign` 时才会改自己的存储，算术运算不会改变变量本身。

---

## 5. 综合实践

把本讲的知识串起来，完成下面这个「**手写最小训练步**」的任务（**源码阅读 + 心智建模为主**；本地有环境可实际运行）。

**任务背景**：训练的核心循环就是「前向 → 求梯度 → 用梯度 `assign_sub` 更新参数」。我们现在还不会自动微分（那是 [u5-l1](u5-l1-autodiff-gradients.md) 的内容），但可以**手动**给定梯度，模拟一次参数更新，从而验证「变量是有状态、可训练的」。

**步骤**：

1. 阅读并理解下列**示例代码**（非项目源码）：

```python
# 示例代码（非项目源码）
import tensorflow as tf

# 1) 用可训练变量表示一个参数 w，初值 0.0
w = tf.Variable(0.0, name="weight")
print("初始 w =", w.numpy())

# 2) 模拟「一次梯度下降更新」: w <- w - lr * grad
lr = 0.1
grad = 2.0          # 假装这是 GradientTape 算出来的梯度
w.assign_sub(lr * grad)   # 底层: assign_sub_variable_op(handle, delta)

print("更新后 w =", w.read_value().numpy())

# 3) 把 w 当作 Tensor 参与运算（走算子重载 -> w.value()）
y = w * tf.constant(3.0)
print("w*3 =", y.numpy())
```

2. 对照源码，为代码中每一处 `assign_sub` / `read_value` / `w * 3.0` 标注：
   - 它触发了 `resource_variable_ops.py` 里的哪个方法？
   - 最终调用了哪个 `gen_resource_variable_ops.*` 底层 op？
3. 用一段注释解释：**为什么 `w` 必须是 `Variable` 而不能用 `tf.constant`？**（提示：从「状态保持」「可被反复 `assign_sub` 更新」「被算子重载读取当前值」三方面回答。）

**预期结果**：

- 初始 `w = 0.0`；更新后 `w = 0.0 - 0.1 * 2.0 = -0.2`；`w*3 = -0.6`。
- 你应能回答：`assign_sub` → `assign_sub_variable_op`；`read_value` → `read_variable_op`；`w * 3.0` → 经 `Variable.__mul__` → `value()` → Tensor 的乘法。
- 注释要点：参数要在多次迭代间保持状态、被优化器原地更新，并随时以「当前值」参与前向计算——这些只有可变、共享、持久的 `Variable` 能提供，不可变的 `constant` 做不到。

**若无法运行**：仍完成第 2、3 步的源码对照与注释——这正是本讲最想训练的「读懂调用链」能力。

---

## 6. 本讲小结

- `tf.Variable` 维护**共享、持久、可变的状态**，与不可变的 `Tensor`（值）形成对比；类型与形状构造后固定，只能用 `assign` 系列方法改值。
- `tf.Variable(...)` 并不直接实例化，而是经元类 `VariableMetaclass` → `_variable_call` → **创建器栈** `_variable_creator_stack` → 兜底 `default_variable_creator_v2`，最终返回真正的 `ResourceVariable`；这套机制让分布式策略能透明替换变量实现。
- `variables.py` 里的 `Variable` 是**抽象基类**（方法多为 `raise NotImplementedError`），真正实现在 `resource_variable_ops.py` 的 `ResourceVariable`。
- 变量持有一个**资源句柄（handle）**——`var_handle_op` 产出的、指向可变存储的引用；`assign` / `read_value` 都是「拿句柄访问存储」的 op。
- eager 模式构造即写入初值；graph 模式构造时只建图，靠 `initializer_op` 延迟初始化。
- 通过 `_OverloadAllOperators`，`Variable` 把所有算子重载为「先 `value()` 读出、再做张量运算」，因此能像 `Tensor` 一样参与计算。

---

## 7. 下一步学习建议

- **下一步本单元**：阅读 [u2-l4 Operation 与 Tensor 的 Python 表示](u2-l4-operation-and-tensor.md)，把 `Operation`、`Tensor`、`Variable` 三者的对象关系补全——你会看到 `v.handle.op` 为什么返回的是一个 `Operation`。
- **关于算子重载的来源**：本讲提到算子最终调用 `tensor_lib.Tensor` 的方法，建议回头读 `tensorflow/python/framework/tensor.py` 里 `Tensor` 类的算子定义（后续讲义会覆盖）。
- **关于自动更新**：本综合实践里我们「手动」给梯度。真实的梯度由自动微分计算，那是 [u5-l1 自动微分与 gradients](u5-l1-autodiff-gradients.md) 的主题；学完那篇你会理解优化器的 `apply_gradients` 内部其实就是对变量做 `assign_sub`。
- **关于 op 与生成代码**：本讲反复出现 `gen_resource_variable_ops.*`，这些是 op 定义自动生成的 Python 绑定，其来龙去脉在 [u4 Op 与 Kernel 注册机制](u4-l1-op-registration.md) 单元展开。
