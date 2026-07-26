# tf.function、ConcreteFunction 与 def_function

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `tf.function` 装饰器返回的到底是什么对象，以及它为什么叫「多态函数（PolymorphicFunction）」。
- 区分 **`Function`（多态函数）** 与 **`ConcreteFunction`（具体函数）** 这两个概念，理解「一个多态函数内部可能藏着多个具体函数」。
- 描述 **tracing（追踪）** 这一核心动作：TF 如何把一个普通 Python 函数「跑一遍」从而建出一张计算图。
- 看懂 **缓存机制**：为什么同一个被 `@tf.function` 装饰的函数，用相同形状/类型的张量调用多次只追踪一次，而换一种 Python 值就要重新追踪。
- 顺着真实源码走完从 `f(x)` 调用、到查找/创建 `ConcreteFunction`、再到执行图的完整链路。

承接上一讲 u3-l3（Eager 执行模式）：Eager 模式下每个 op 立即执行；本讲讲解的 `tf.function` 正是连接 Eager 与 Graph 两个世界的「桥梁」——它在 Eager 时代重新引入了图，但对外仍然像普通 Python 函数一样调用。

## 2. 前置知识

阅读本讲前，建议你已经理解：

- **Tensor / op / Graph**（u2-l1、u2-l4、u3-l1）：一个 op 产出 Tensor，多个 op 用边连成有向图 `tf.Graph`。
- **Eager 执行模式与 Context**（u3-l3）：op 一被调用立即执行并返回真实数值。
- **Python 装饰器**：`@tf.function` 本质是 `f = tf.function(f)`，把原函数包成一个新对象。
- **字典 / 哈希** 的基本概念，因为「缓存」就是「以输入类型为键、以具体函数为值」的查表。

一个直觉性的心智模型先放在这里，后面会反复用到：

> `tf.function` 像一个**带缓存的编译器**。你第一次用某种「输入类型」调用它时，它把你的 Python 函数「翻译」成一张 TF 图并缓存起来；以后再用「兼容的输入类型」调用，就直接复用那张图，跳过翻译。这个「翻译」动作就叫 tracing。

## 3. 本讲源码地图

本讲的关键在于：规格里点名的 `def_function.py` 和 `function.py` **在当前版本已经被重构成兼容性 shim（垫片）文件**，真正的实现被搬到了 `tensorflow/python/eager/polymorphic_function/` 目录。本讲会同时讲清 shim 与真实实现。

| 文件 | 作用 |
| --- | --- |
| `tensorflow/python/eager/def_function.py` | **shim 文件**（28 行）。仅把符号从 `polymorphic_function/` 重新导出，向后兼容旧路径。 |
| `tensorflow/python/eager/function.py` | **shim 文件**（37 行）。重新导出 `ConcreteFunction`、`AtomicFunction` 等。 |
| `tensorflow/python/eager/polymorphic_function/polymorphic_function.py` | **`tf.function` 的真实实现**。定义 `function()` 装饰器和 `Function` 类。 |
| `tensorflow/python/eager/polymorphic_function/concrete_function.py` | **`ConcreteFunction` 的真实实现**。一张可执行、可求导的图函数。 |
| `tensorflow/python/eager/polymorphic_function/tracing_compilation.py` | **追踪引擎**。把 Python 函数编译成图、负责缓存查找与新建。 |
| `tensorflow/core/function/polymorphism/function_cache.py` | **`FunctionCache`**：以「执行上下文 + 函数类型」为键的缓存容器。 |
| `tensorflow/core/function/trace_type/default_types.py` | **`Literal` 等 TraceType**：决定「什么算同一种输入类型」的规则。 |
| `tensorflow/python/types/core.py` | 抽象基类 `PolymorphicFunction` / `ConcreteFunction`，定义类型契约。 |

## 4. 核心概念与源码讲解

### 4.1 tf.function 与 Function：多态函数的入口

#### 4.1.1 概念说明

直接看一段最常见的用法：

```python
import tensorflow as tf

@tf.function
def add(x, y):
    return x + y

add(tf.constant([1, 2]), tf.constant([3, 4]))  # 第一次调用
add(tf.constant([5, 6]), tf.constant([7, 8]))  # 第二次调用，形状相同
```

这里有一个关键问题：`add` 还是一个普通 Python 函数吗？不是。`@tf.function` 把它替换成了一个 `Function` 对象。`Function` 是一种**多态函数（PolymorphicFunction）**——「多态」指的是它可以根据不同的输入类型，对应到**多个**不同的底层图实现。

为什么需要这层抽象？因为 Eager 模式虽然调试方便，但每次调用都走 Python 解释器、逐个 op 派发，开销大且无法做跨 op 的全局优化。`tf.function` 的目标是在保留 Eager 「像普通函数一样调用」体验的同时，把真正的计算编译成图，从而获得**速度**（图可被 Grappler/XLA 优化）和**可序列化**（图可存入 SavedModel、可部署到 C++ 端、TFLite 端）。

两个抽象基类定义了类型契约，先认清它们：

- `PolymorphicFunction`：可调用对象，**根据输入类型自动选择/创建**合适的图特化。`tf.function` 产出的 `Function` 就是它的实现。

[tensorflow/python/types/core.py:L191-L205](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/types/core.py#L191-L205) —— 抽象基类 `PolymorphicFunction`，关键契约是必须实现 `get_concrete_function(*args, **kwargs)`，即「给我一组输入类型，返回一个对应的 ConcreteFunction」。

- `ConcreteFunction`：**封装了原始图函数定义、并支持在 `GradientTape` 下求导**的图函数。它背后就是一张 `tf.Graph`。

[tensorflow/python/types/core.py:L170-L183](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/types/core.py#L170-L183) —— 抽象基类 `ConcreteFunction`，关键契约是 `inference_fn`（返回前向推理用的 `AtomicFunction`）。

一句话区分：**`Function` 是「调度器 + 缓存」，`ConcreteFunction` 是「一张真正能跑的图」**。`Function.__call__` 的工作就是挑出（或新建）合适的 `ConcreteFunction` 并执行它。

#### 4.1.2 核心流程

`tf.function` 装饰器到 `Function` 对象的创建流程：

```
@tf.function  ──>  调用 function(func=None, ...) 返回 decorated  ──>
  decorated(inner_function):
      name = inner_function.__name__
      return tf_decorator.make_decorator(
          inner_function,
          decorator_func=Function(inner_function, name, ...))
```

要点：
1. `tf.function` 既能 `@tf.function` 直接装饰，也能 `@tf.function(input_signature=...)` 带参装饰。后者要求 `func=None`，返回一个「等待被装饰的函数」的装饰器。
2. 真正构造的核心是 `Function(inner_function, name, ...)`。`tf_decorator.make_decorator` 只是让返回值「看起来还像原函数」（保留 `__name__`、`__doc__`、签名等），便于回溯和文档工具识别。
3. `Function.__init__` 在此刻**并不会追踪**，只是把原函数和配置存起来，并创建一个空的缓存。

#### 4.1.3 源码精读

先确认 shim 的事实——这就是规格点名的 `def_function.py` 全部内容：

[tensorflow/python/eager/def_function.py:L24-L28](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/def_function.py#L24-L28) —— `def_function.py` 只是把 `Function`、`function`、`_tf_function_counter` 从 `polymorphic_function.polymorphic_function` 重新导出。文件头注释明说「Supports old symbols supplied by this file while the code is refactored」。所以本模块真正要读的是 `polymorphic_function.py`。

`function()` 装饰器内部构造 `Function` 的位置：

[tensorflow/python/eager/polymorphic_function/polymorphic_function.py:L1657-L1673](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/polymorphic_function/polymorphic_function.py#L1657-L1673) —— `decorated` 取出函数名，用 `tf_decorator.make_decorator` 包裹 `Function(...)`。注意它把 `input_signature`、`autograph`、`jit_compile` 等参数透传给 `Function`。

`Function` 类的定义与构造：

[tensorflow/python/eager/polymorphic_function/polymorphic_function.py:L453-L530](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/polymorphic_function/polymorphic_function.py#L453-L530) —— `class Function(core.PolymorphicFunction, trackable.Trackable)`，构造函数保存 `_python_function`、解析出函数类型 `_function_type`、创建缓存 `_function_cache = function_cache.FunctionCache()`，但**不触发任何追踪**。

特别留意这几行：

- L489 `self._lock = threading.RLock()`：追踪是个昂贵且有状态的过程，用可重入锁保证多线程下「同一时刻只有一个线程在追踪」。
- L494 `self._function_cache = function_cache.FunctionCache()`：本讲的「主角」之一，所有已追踪出的 `ConcreteFunction` 都存这里。
- L495 `self._function_captures = capture_container.FunctionCaptures()`：追踪闭包变量时用。
- L523-525 `_created_variables`/`_variable_creation_config`/`_no_variable_creation_config` 都为 `None`：这些「首次调用」相关的状态，是后面理解「变量只能创建一次」的钥匙。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `@tf.function` 装饰后，`add` 不再是普通函数，而是一个 `Function`（多态函数）对象，且构造时**没有**立刻建图。

**操作步骤**（源码阅读型）：

1. 打开 [polymorphic_function.py 的 `Function.__init__`](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/polymorphic_function/polymorphic_function.py#L462-L530)，确认构造函数体里没有任何「执行 `python_function`」或「调用 `trace_function`」的语句——它只做赋值和缓存初始化。
2. 在本地（已安装 tensorflow 的环境）运行下面这段最小示例：

```python
import tensorflow as tf

@tf.function
def add(x, y):
    return x + y

print(type(add))                      # 期望：<class '...polymorphic_function.Function'>
print(callable(add))                  # 期望：True
print(add._function_cache)            # 期望：一个 FunctionCache 对象
print(len(add._function_cache))       # 期望：0（还没调用，缓存为空）
```

**需要观察的现象**：装饰发生在 `import` / 函数定义阶段，此时缓存长度为 0，说明**装饰 ≠ 追踪**。

**预期结果**：`type(add)` 指向 `Function`，`len(add._function_cache)` 为 0。（注：访问 `_function_cache` 属于私有属性，仅用于学习观察，不同版本可能变化；若报错可改用 `add.experimental_get_tracing_count()` 返回 0 来间接验证。）

> 待本地验证：上述 `print` 的精确输出取决于你的 TF 版本，重点看「类型是 Function、初始追踪计数为 0」。

#### 4.1.5 小练习与答案

**练习 1**：`@tf.function` 和 `@tf.function(jit_compile=True)` 在「返回的对象类型」上有什么区别？

**参考答案**：返回的对象**类型相同**，都是 `Function`（多态函数）。区别在于 `Function` 内部记录的 `_jit_compile=True`，这个标志会在后续追踪时把图交给 XLA 编译（见 4.3.3），并不改变 `Function` 这个外壳。

**练习 2**：为什么 `Function.__init__` 里要创建 `_function_cache` 但不在构造时就追踪？

**参考答案**：构造时根本不知道调用者会用什么输入类型来调用；只有真正调用 `add(x, y)` 时，输入的 dtype/形状/Python 值才能确定，这时才有追踪的依据。提前追踪既无输入可依据，又会白白浪费资源。这是一种**惰性（lazy）**策略：把昂贵的编译推迟到真正需要的那一刻。

---

### 4.2 ConcreteFunction：一张图、一份特化

#### 4.2.1 概念说明

如果说 `Function` 是「调度器」，那么 `ConcreteFunction` 就是调度器管理的一个个「成品」。**一个 `ConcreteFunction` 背后绑定且仅绑定一张 `tf.Graph`**，这张图是为某一组**特定的输入类型**而追踪出来的。

回看 `tf.function` 文档里的精辟总结：

> Internally, `PolymorphicFunction` may contain multiple `ConcreteFunction`s, each specialized to arguments with different data types or shapes... `tf.function` treats any pure Python values as opaque objects (best thought of as compile-time constants), and builds a separate `tf.Graph` for each set of Python arguments that it encounters.

这句话信息量很大，拆成三条规则记牢：

1. **Tensor 输入**按「dtype + 形状」归类：相同 dtype/形状（或更宽泛的兼容形状）复用同一个 `ConcreteFunction`。
2. **纯 Python 值**（`int`、`bool`、`str` 等）被视为「编译期常量」，**每个不同的值**都会被烤进图里，从而产生新的 `ConcreteFunction`。
3. `Function` 内部维护的是一张「输入类型 → ConcreteFunction」的表。

这就是为什么本讲的实践任务里「调用两次不同输入」会发生有趣的事情——取决于「不同」是指张量形状不同，还是 Python 值不同。

#### 4.2.2 核心流程

`ConcreteFunction` 是如何被「造」出来的？它从一张追踪好的 `FuncGraph` 包装而来：

```
FuncGraph（追踪产物，见 4.3）
   └─> atomic_function.from_func_graph(...)   生成 AtomicFunction（一个 FunctionDef 包装）
         └─> ConcreteFunction(atomic_fn)      持有图、捕获输入、推理/前向/反向函数
```

`ConcreteFunction` 内部并存几组函数对象，对应不同用途：

- **推理函数 `_inference_function`**：纯前向，当没有 `GradientTape` 监视时直接用它，最快。
- **前向 + 反向函数对**：当有 `GradientTape` 在监视时使用，负责把中间结果记录下来供反向求导（这与 u5-l1 自动微分衔接）。

执行一张 `ConcreteFunction` 最终落到 `_call_flat`，它会把展平后的输入张量加上「捕获输入（闭包变量）」一起送进 C++ 端的原子函数执行。

#### 4.2.3 源码精读

先看 `function.py` shim，确认 `ConcreteFunction` 的真实位置：

[tensorflow/python/eager/function.py:L26-L28](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/function.py#L26-L28) —— shim 从 `concrete_function` 模块重新导出 `ConcreteFunction`，从 `atomic_function` 导出 `AtomicFunction`/`from_func_graph`。

`ConcreteFunction` 类与工厂方法：

[tensorflow/python/eager/polymorphic_function/concrete_function.py:L1018-L1078](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/polymorphic_function/concrete_function.py#L1018-L1078) —— `class ConcreteFunction(core.ConcreteFunction, trackable.Trackable)`。注意几个字段：
- L1041 `self._func_graph = atomic_fn.graph`：绑定那张图。
- L1042-1045 `self._captured_inputs`：闭包捕获的输入（如函数体里用到的外部 `tf.Variable`、外部张量）。
- L1065-1071 `_delayed_rewrite_functions` 与 `_inference_function`：前向/反向函数对，以及被缓存的推理函数。

`from_func_graph` 工厂方法：

[tensorflow/python/eager/polymorphic_function/concrete_function.py:L1073-L1078](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/polymorphic_function/concrete_function.py#L1073-L1078) —— 输入一张 `FuncGraph`，先用 `atomic_function.from_func_graph` 造出 `AtomicFunction`，再包成 `ConcreteFunction`。这是追踪引擎产出 `ConcreteFunction` 的标准入口。

真正执行图的 `_call_flat`：

[tensorflow/python/eager/polymorphic_function/concrete_function.py:L1317-L1336](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/polymorphic_function/concrete_function.py#L1317-L1336) —— 关键三段：
- L1317 `args = tensor_inputs + captured_inputs`：把「调用者传入的张量」与「闭包捕获的输入」拼接成完整输入。
- L1318-1322：如果没有 tape 在监视且处于 eager，走最快路径 `self._inference_function.call_preflattened(args)` 直接推理。
- L1323-1335：否则用 `_select_forward_and_backward_functions` 选出前向/反向函数对，在 `call_flat` 执行并 `record` 供求导。

`_inference_function.call_preflattened` / `call_flat` 来自 `AtomicFunction`，它们最终把函数调用转成 C++ 端的 `PartitionedCall`/`StatefulPartitionedCall` op——这就是 Python 图函数与 C++ 执行器之间的桥。

#### 4.2.4 代码实践

**实践目标**：用 `get_concrete_function` 亲手取出一个 `ConcreteFunction`，确认它背后是一张 `tf.Graph`。

**操作步骤**（可运行示例）：

```python
import tensorflow as tf

@tf.function
def square(x):
    return x * x

# 显式按输入类型取出一个具体函数（不需要真实数值，TensorSpec 就够）
cf = square.get_concrete_function(tf.TensorSpec(shape=(3,), dtype=tf.float32))

print(type(cf))                  # 期望：ConcreteFunction
print(isinstance(cf.graph, tf.Graph))  # 期望：True
print(cf.structured_input_signature)   # 期望：((TensorSpec(...)), {})
print(cf.structured_outputs)           # 期望：一个 TensorSpec
```

**需要观察的现象**：`get_concrete_function` 用 `TensorSpec`（形状+dtype）就能触发一次追踪，**不需要传入真实张量数值**——这正好印证了「按输入类型特化」。

**预期结果**：`cf.graph` 确实是一个 `tf.Graph` 实例；输入签名里是一个 `TensorSpec(shape=(3,), dtype=float32)`。

> 待本地验证：`structured_input_signature` 的精确嵌套结构以本地输出为准。

#### 4.2.5 小练习与答案

**练习 1**：`ConcreteFunction` 和 `Function` 谁包含谁？

**参考答案**：`Function`（多态函数）**包含**若干个 `ConcreteFunction`。`Function` 是对外暴露的可调用对象；每次以新输入类型调用时，`Function` 可能在其缓存中新增一个 `ConcreteFunction`。`get_concrete_function` 就是把这个内部成品「拿给你看」的入口。

**练习 2**：为什么 `ConcreteFunction` 要区分「推理函数」和「前向/反向函数对」？

**参考答案**：为了性能。绝大多数调用并不需要求导（如推理、预测），此时直接跑轻量的 `_inference_function` 即可，省去记录中间状态的开销；只有当 `GradientTape` 在监视时，才需要更重的前向/反向函数对来支撑反向传播。这是一种「按需付出代价」的设计。

---

### 4.3 Tracing：把 Python 函数「跑」成图

#### 4.3.1 概念说明

**tracing（追踪）** 是 `tf.function` 的核心动作：在追踪期间，TF 临时进入一种「图构建模式」，把传给函数的输入替换成**占位符（placeholder）**，然后**真正执行一遍你的 Python 函数**。函数体里遇到的每一个 TF op 都不会立即算出数值，而是往当前 `FuncGraph` 里**登记一个节点**；函数返回时，这些节点连成的图就是 `ConcreteFunction` 的图。

这里有一个最容易踩坑的点（文档反复强调）：

> Python operations run only once, at trace time.

也就是说，函数体里的**纯 Python 副作用**（`print`、往 `list` 里 `append`、修改全局变量等）**只在追踪那一刻执行一次**，之后复用图时这些副作用不会再次发生。要让「副作用」随每次调用发生，必须用 TF op（如 `tf.print`）来表达。

#### 4.3.2 核心流程

追踪的完整链路（位于 `tracing_compilation.py`）：

```
Function.__call__(args)
  └─ Function._call(args)
       ├─ 首次调用：_initialize(args)        # 见 4.3.3，变量创建特化
       └─ tracing_compilation.call_function(args, tracing_options)
            └─ trace_function(args, tracing_options)
                 └─ _maybe_define_function(args, tracing_options)
                      ├─ (1) 计算本次输入的「查找类型」 lookup_func_type
                      ├─ (2) function_cache.lookup(...)  # 命中就直接返回
                      └─ (3) 未命中：_create_concrete_function(...)
                            ├─ func_graph_from_py_func(...)  # 真正执行 Python 函数建图
                            └─ function_cache.add(concrete_function, ...)
```

三个关键点：

1. **查找类型（lookup type）**：把本次调用的每个输入，转换成一个 `TraceType` 对象（张量→按 dtype+shape，Python 值→`Literal`）。
2. **缓存查找**：拿这个查找类型去 `FunctionCache` 里问「有没有兼容的现成图？」。命中则零成本复用。
3. **新建**：未命中时，才真正执行追踪、建图、入缓存。

#### 4.3.3 源码精读

`Function.__call__` 的总入口（4.1 已见过 `Function` 类，这里看它的调用逻辑）：

[tensorflow/python/eager/polymorphic_function/polymorphic_function.py:L808-L836](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/polymorphic_function/polymorphic_function.py#L808-L836) —— 注意 L811-813：如果全局开关 `run_functions_eagerly` 打开了（`tf.config.run_functions_eagerly(True)`），就直接执行原 Python 函数、**完全不建图**——这是留给调试用的「逃生舱」。否则 L835-836 才进入 `self._call(...)`。

`_call` 的分支（首次 vs 后续）：

[tensorflow/python/eager/polymorphic_function/polymorphic_function.py:L855-L925](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/polymorphic_function/polymorphic_function.py#L855-L925) —— 关键分支：
- L858-865：先把 `args/kwds` 规范化（canonicalize）。
- L866-887：若 `_created_variables` 已被赋值（首次调用已完成）或 `_variable_creation_config` 已存在，直接走缓存路径 `tracing_compilation.call_function(...)`。
- L889-892：否则这是**第一次调用**，进入 `_initialize(args, kwds)` 做一次性初始化。

追踪引擎的「心脏」`_maybe_define_function`：

[tensorflow/python/eager/polymorphic_function/tracing_compilation.py:L188-L292](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/polymorphic_function/tracing_compilation.py#L188-L292) —— 务必看清三段：
- L205-212：把输入规范化。
- L229-236：计算 `lookup_func_type`（查找类型）和 `lookup_func_context`。
- L238-246：**缓存查找**——`function_cache.lookup(lookup_func_type, current_func_context)`，命中就直接 `return`，跳过下面所有建图工作。

真正建图的 `_create_concrete_function`：

[tensorflow/python/eager/polymorphic_function/tracing_compilation.py:L295-L353](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/polymorphic_function/tracing_compilation.py#L295-L353) —— L310-320 `func_graph_module.func_graph_from_py_func(...)` 是追踪的「执行器」：它创建一个 `FuncGraph`，把输入换成占位符，然后**实际调用一次 `python_function`**，函数体里的 op 自动登记进图。L340-349 再用 `ConcreteFunction.from_func_graph` 把图包装成 `ConcreteFunction`。

> 顺带一提 AutoGraph：`_generate_tracing_options` 里若 `self._autograph` 为真，会先把原函数经 `autograph_util.py_func_from_autograph` 转换（把 `if/while/for` 翻译成图 op），再交给追踪。这与 u9-l2 AutoGraph 讲义衔接。

#### 4.3.4 代码实践

**实践目标**：用真实代码感受「Python 副作用只发生一次」，从而理解 tracing 的含义。

**操作步骤**（可运行示例）：

```python
import tensorflow as tf

calls = []  # 普通 Python list

@tf.function
def f(x):
    calls.append(x)        # 警告：只在 tracing 时执行！
    print("tracing!", x)   # 警告：只在 tracing 时打印！
    return x + 1

f(tf.constant(1.0))   # 第 1 次调用：触发 tracing
f(tf.constant(2.0))   # 第 2 次调用：同形状，复用图，不再 tracing
f(tf.constant(3.0))   # 第 3 次调用：同形状，复用图，不再 tracing

print("list 长度 =", len(calls))         # 期望：1（只 append 了一次）
print("tracing 次数 =", f.experimental_get_tracing_count())  # 期望：1
```

**需要观察的现象**：
- `"tracing!"` 只打印一次，且打印的 `x` 是一个 `Tensor`（占位符），而不是数值 `1.0`。
- `calls` 列表长度为 1，尽管函数被调用了 3 次。
- `experimental_get_tracing_count()` 返回 1。

**预期结果**：上面的三个「期望」都会成立，直观证明「Python 副作用只在追踪时发生一次」。

> 待本地验证：在本地运行确认输出；若把第 3 次调用换成 `f(tf.constant([1.0, 2.0]))`（形状不同），`tracing!` 会再打印一次，tracing 计数变为 2。

#### 4.3.5 小练习与答案

**练习 1**：下面函数，`tf.config.run_functions_eagerly(True)` 后行为有何变化？

```python
@tf.function
def g(x):
    print("hi")
    return x*2
```

**参考答案**：开启 `run_functions_eagerly` 后，`__call__` 走 [polymorphic_function.py:L811-L813](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/polymorphic_function/polymorphic_function.py#L811-L813) 的「直通」分支，每次调用都像普通函数一样执行，`"hi"` 每次都打印、每次都立即返回数值。完全不建图、不追踪。这用于调试——代价是失去图优化和性能。

**练习 2**：为什么 `tf.function` 要求「`tf.Variable` 只能在第一次调用时创建」？（提示：看 `_initialize`）

**参考答案**：因为变量的创建会改变图的结构（要插入变量句柄、初始化 op 等）。`_initialize` 在首次调用时构造了**两套**追踪配置——`VARIABLE_CREATION`（允许建变量）和 `NO_VARIABLE_CREATION`（禁止建变量）。首次追踪建好变量后，之后所有调用都走「禁止建变量」的配置（[polymorphic_function.py:L700-L715](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/polymorphic_function/polymorphic_function.py#L700-L715)）。若第二次调用又新建变量，会导致同一个 `Function` 对应的变量集合不确定，图无法复用，所以直接抛错。最佳实践是把 `tf.Variable` 创建在 `tf.function` 外部。

---

### 4.4 缓存与重用：FunctionCache 与 TraceType

#### 4.4.1 概念说明

本模块回答本讲最核心的问题：**「什么算同一种输入类型」**，从而决定缓存命中还是重追踪。答案藏在两个东西里：

- **`FunctionCache`**：物理容器，一张「键 → ConcreteFunction」的表。
- **`TraceType`**：键的「取值规则」，决定每个输入被抽象成什么类型。

把输入归类的规则（这是必须记住的）：

| 输入种类 | 被抽象成的 TraceType | 命中/重追踪规则 |
| --- | --- | --- |
| `tf.Tensor` / `tf.Variable` | 该张量的 `TensorSpec`（dtype+形状） | 相同 dtype、相同（或更具体但兼容的）形状 → 命中 |
| 纯 Python `int`/`bool`/`str`/`None` | `Literal(value)` | **值完全相等**才命中；不同值 → 重追踪 |
| 自定义 Python 对象 | 其 `__tf_tracing_type__` 返回的类型（常为 `Weakref`） | 默认按对象身份判定 |

尤其注意 `Literal`：它的相等判定是「值相等」，且**无法泛化**——所以传 `f(1)` 和 `f(2)` 会追踪两次。这就是「把 Python 值当作编译期常量」的底层原因。

#### 4.4.2 核心流程

缓存命中判定的本质是一次「类型派发（dispatch）」。给定本次输入的查找类型 \( t \) 和执行上下文 \( c \)，缓存做的是：

\[
\text{dispatch}(t) = \text{在已存储类型 } S \text{ 中，找一个 } s \in S \text{ 使得 } t \text{ 是 } s \text{ 的子类型}
\]

其中「子类型」由 `TraceType.is_subtype_of` 定义。若找到，返回对应的 `ConcreteFunction`；否则重追踪。

用通俗的话：

- 你之前用 `TensorSpec(shape=None)`（形状未知）追踪过 → 之后传任何具体形状的张量都是它的「子类型」，命中。
- 反过来，你之前用 `TensorSpec(shape=(3,))` 追踪过 → 之后传 `shape=(4,)` 不是子类型，**不命中**，重追踪。
- Python 值 `1` 和 `2` 对应的 `Literal` 互不为子类型（只有相等才算），所以必重追踪。

`reduce_retracing=True` 时，缓存会尝试**泛化（generalize）**查询类型——例如把 `shape=(3,)` 放宽成 `shape=None`，从而让后续不同形状的调用都命中同一个图。但 `Literal` 拒绝泛化（不同值永远不合并）。

#### 4.4.3 源码精读

`FunctionCache` 是个很小的容器，建议整段读：

[tensorflow/core/function/polymorphism/function_cache.py:L30-L90](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/function/polymorphism/function_cache.py#L30-L90) —— 几个要点：
- L36-41：内部两张表——`_primary`（`(上下文, 函数类型) → 函数`）和 `_dispatch_dict`（`上下文 → TypeDispatchTable`，用于派发查询）。
- L43-52 `lookup`：在对应上下文的派发表里查 `dispatch(function_type)`，找到就用 `_primary` 取出函数；找不到返回 `None`。
- L67-79 `add`：把新函数按其 `function_type` 存进两张表。
- L81-90 `generalize`：用于 `reduce_retracing` 时把查询类型放宽。

`Literal` 为何无法泛化：

[tensorflow/core/function/trace_type/default_types.py:L64-L69](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/function/trace_type/default_types.py#L64-L69) —— `Literal.is_subtype_of` 只在 `self == other`（值相等）时为真；`most_specific_common_supertype` 也只有在所有类型都相等时才返回 `self`，否则 `None`。这两条共同决定了「不同 Python 值永远无法合并到一个图」。

张量的类型从哪来？`TypeSpec.__tf_tracing_type__` 直接返回 `self`：

[tensorflow/python/framework/type_spec.py:L581-L583](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/framework/type_spec.py#L581-L583) —— 即 `TensorSpec` 自己就充当它的 `TraceType`，按其 dtype/形状判等。这就是「张量按 dtype+形状复用图」的来源。

#### 4.4.4 代码实践

**实践目标**：对照 `def_function.py`/`polymorphic_function.py`，验证「同形状复用、不同 Python 值重追踪」这条规则，并观察缓存增长。

**操作步骤**（可运行示例）：

```python
import tensorflow as tf

@tf.function
def fn(x, n):
    return x * n      # x 是张量，n 是 Python int

t = tf.constant([1.0, 2.0, 3.0])

fn(t, 2)   # 第 1 次：tracing（x=float32(3,), n=Literal(2)）
fn(t, 2)   # 第 2 次：同类型，命中缓存，不 tracing
fn(t, 3)   # 第 3 次：n=Literal(3)，不同值，重新 tracing

print("tracing 次数 =", fn.experimental_get_tracing_count())  # 期望：2
print("缓存大小 =", len(fn._function_cache))                  # 期望：2
```

**需要观察的现象**：
- 第 1、3 次调用触发 tracing，第 2 次不触发。
- 最终 tracing 计数为 2，缓存里有两个 `ConcreteFunction`（分别对应 `n=2` 和 `n=3`）。
- 若把第 3 次改成 `fn(tf.constant([4.0, 5.0, 6.0]), 2)`（仍是 float32、形状 (3,)、n=2），则**不**触发新 tracing——形状相同但数值不同，对张量而言算同一类型。

**预期结果**：`experimental_get_tracing_count()` 为 2。

> 待本地验证：精确的 `_function_cache` 长度以本地为准（私有属性，版本间可能调整）；重点是「同形状张量不重追踪、不同 Python 值重追踪」。

#### 4.4.5 小练习与答案

**练习 1**：下面代码会追踪几次？

```python
@tf.function
def h(x):
    return x + 1
h(tf.constant([1, 2]))      # int32, shape (2,)
h(tf.constant([3, 4, 5]))   # int32, shape (3,)
h(tf.constant([1.0, 2.0]))  # float32, shape (2,)
```

**参考答案**：**3 次**。前两次虽然都是 `int32`，但形状 `(2,)` 与 `(3,)` 互不为子类型（默认不做形状泛化），所以各自追踪一次；第三次 dtype 变成 `float32`，再追踪一次。若想减少重追踪，可设 `@tf.function(reduce_retracing=True)` 让形状被放宽。

**练习 2**：把一个很大的 numpy 数组当作 Python 值传进 `tf.function`，会有什么隐患？

**参考答案**：它会被当作 `Literal`（或对应的捕获），整份「烤」进图里，作为编译期常量。这不仅让图变大、占用内存，还会因为「值不同就重追踪」导致每次传不同数组都重新建图。正确做法是把数据作为 `tf.Tensor`/`tf.constant`（张量输入）传入，这样只按 dtype+形状归类，不会把数值固化进图。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「追踪日志器」小任务：

**任务**：写一个被 `@tf.function` 装饰的函数 `scale_add(x, scale, bias)`，其中 `x` 是张量、`scale` 和 `bias` 是 Python 浮点数。要求：

1. 在函数体里加一行 `tf.print("running as graph op:", x)`（注意是 `tf.print` 不是 `print`），再对照普通 `print` 观察两者区别。
2. 用下面四种方式调用，并**预测**每种是否会触发新的 tracing：

```python
x = tf.constant([1.0, 2.0, 3.0])
scale_add(x, 2.0, 1.0)   # (a)
scale_add(x, 2.0, 1.0)   # (b)
scale_add(x, 3.0, 1.0)   # (c)
scale_add(x, 2.0, 2.0)   # (d)
```

3. 调用结束后，打印 `scale_add.experimental_get_tracing_count()`，验证你的预测。
4. 用 `get_concrete_function` 取出其中一个 `ConcreteFunction`，确认它的 `graph` 是 `tf.Graph`，并对照本讲源码说明这次「取具体函数」走的是 [_get_concrete_function_garbage_collected](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/polymorphic_function/polymorphic_function.py#L1208-L1261) → `tracing_compilation.trace_function` 这条链。

**预期分析**（先自己判断，再运行验证）：
- (a) 触发 tracing，计数 1。
- (b) 与 (a) 输入类型完全相同（`Literal(2.0)`、`Literal(1.0)`、float32(3,)），命中缓存，不触发新 tracing。
- (c) `scale` 变成 `Literal(3.0)`，不同 Python 值 → 重追踪，计数 2。
- (d) `bias` 变成 `Literal(2.0)`，不同 Python 值 → 重追踪，计数 3。
- `tf.print` 每次调用都会执行（它是图 op），而若你加的是普通 `print`，则只在 tracing 时执行。

> 待本地验证：在本地运行，对照 `experimental_get_tracing_count()` 的实际值与你手算的预测。

**反思题**：如果想让 (c)、(d) 不再触发重追踪，应该把 `scale`/`bias` 改成什么形式的输入？为什么？

（提示：把它们改成张量输入 `tf.constant(2.0)`，于是它们按 dtype（float32 标量）而非 `Literal` 归类，所有调用复用同一个图。）

## 6. 本讲小结

- `@tf.function` 把普通函数包成一个 `Function`（多态函数 `PolymorphicFunction`），它是对外的可调用对象；规格点名的 `def_function.py`/`function.py` 已是 **shim**，真实实现位于 `polymorphic_function/` 目录。
- `Function` 内部持有一个 `FunctionCache`，管理多个 `ConcreteFunction`；每个 `ConcreteFunction` 背后绑定**一张** `tf.Graph`，是为某一组输入类型特化出来的。
- **tracing** 是核心动作：在临时图模式下「跑一遍」Python 函数，函数体里的 op 登记成图节点；**纯 Python 副作用只在追踪时发生一次**。
- 缓存命中由 **`TraceType`** 决定：张量按 dtype+形状归类，Python 值按 `Literal`（值相等）归类——这正是「同形状复用、不同 Python 值重追踪」的根因。
- 执行链路：`Function.__call__` → `_call`（首次走 `_initialize`）→ `tracing_compilation.call_function` → `_maybe_define_function`（先查缓存，未命中才建图）→ `ConcreteFunction._call_flat`（送入 C++ 原子函数执行）。
- 调试逃生舱：`tf.config.run_functions_eagerly(True)` 会让 `tf.function` 完全绕过建图、直接像普通函数执行。

## 7. 下一步学习建议

- **自动微分（u5-l1）**：本讲多次提到「前向/反向函数对」和 `GradientTape`。下一站应深入 `backprop.py`，看 `ConcreteFunction` 的前向/反向函数对是如何在 `GradientTape` 下被选中并记录中间状态的。
- **SavedModel（u5-l3）**：`ConcreteFunction` 是可序列化的图函数，正是 SavedModel 保存「带签名的函数」的载体；建议接着读 `saved_model/save.py`，看 `get_concrete_function` 的产物如何被写入磁盘。
- **源码延伸阅读**：
  - [tracing_compilation.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/polymorphic_function/tracing_compilation.py) 的 `_create_concrete_function` 与 `func_graph_from_py_func`，理解「执行 Python 函数建图」的细节。
  - [function_cache.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/function/polymorphism/function_cache.py) 配合 `type_dispatch.py`，理解 `is_subtype_of`/`generalize` 如何支撑 `reduce_retracing`。
  - `tensorflow/python/eager/polymorphic_function/polymorphic_function_test.py`，里面有大量关于「重追踪规则」的断言，是验证你理解的最佳佐证。
