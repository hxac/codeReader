# Keras 高层 API

## 1. 本讲目标

学完本讲后，你应该能够：

- 理解 `tf.keras` 引擎的三层结构：`Layer`（层）→ `Model`（模型）→ `Functional`（函数式模型）的继承与职责划分。
- 读懂 `Layer.__call__` 这条核心入口：它如何在前/后处理中完成「按需 `build`、记录连通关系、在函数式构造模式下追踪出一张子图」。
- 说清楚用 Functional API（`Input` + 层调用 + `Model(inputs, outputs)`）搭一个模型时，层是如何被「拓扑排序」并组装进一张可重放的内部计算图的。
- 理解 `model.compile` 配置了什么，`train_step` 如何用 `GradientTape`（u5-l1）和 `optimizer.minimize` 完成一步训练，以及 `fit` 如何把这一切包进 `tf.function`（u3-l4）。
- 区分 Keras 三种建模方式（Sequential、Functional、子类化 Model）在源码层面的差异。

本讲承接 u2-l3（`add_weight` 底层就是创建 `tf.Variable`）与 u5-l1（`train_step` 内部就是 `GradientTape` + 反向自动微分）。

## 2. 前置知识

在进入源码前，先用通俗语言建立几个心智模型。

### 2.1 Keras 是「封装层」不是「新引擎」

`tf.keras` 没有重新实现一套计算引擎。它所有的计算最终都落到 u4 讲过的底层 op/kernel 上。Keras 做的是两件事：

1. **管理状态**：把「权重变量（`tf.Variable`）+ 前向计算逻辑」打包成一个可复用的对象——`Layer`。
2. **管理训练流程**：把「前向 + 损失 + 反向 + 指标」封装成一套标准循环——`Model.fit`。

所以读 Keras 源码的核心问题是：**用户写的 Python 是怎么被翻译成一连串底层 op 调用的，这些 op 产出的张量又是怎么串成一张图的。**

### 2.2 层的「构造」与「建造」分离

一个层有两个关键时机：

- **构造期**（`__init__`）：只记录「我想成为什么样的层」（比如 `units=10`、用什么激活函数），**不创建权重**。因为此时还不知道输入形状，无法确定权重矩阵的尺寸。
- **建造期**（`build(input_shape)`）：第一次真正拿到输入形状时，按需创建权重。这就是为什么你可以写 `Dense(10)` 而不必先告诉它输入维度。

这套「延迟创建」机制是本讲反复出现的主题。

### 2.3 两种调用语义

同一个 `layer(x)` 调用，根据 `x` 的类型会产生完全不同的行为：

- 若 `x` 是**真实数据张量**（EagerTensor）→ 立即计算，返回真实数值。
- 若 `x` 是 **`KerasTensor`**（由 `tf.keras.Input` 产生的「符号张量」）→ 不计算，而是**记录一层连通关系到一张正在生长的计算图里**，返回另一个符号张量。

第二种就是 **Functional 构造模式**，是理解 Functional 模型的钥匙。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tensorflow/python/keras/engine/base_layer.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/base_layer.py) | `Layer` 基类：所有层与模型的祖先。定义 `__call__`、`build`、`call`、`add_weight` 等核心契约。 |
| [tensorflow/python/keras/engine/node.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/node.py) | `Node` 类：记录「某次层调用」的输入/输出与上下游连通关系，是 Functional 图的「边」。 |
| [tensorflow/python/keras/engine/functional.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/functional.py) | `Functional` 类：用图（Input/Output + 一串层调用）定义的模型，把层按拓扑排序组装成内部图。 |
| [tensorflow/python/keras/engine/sequential.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/sequential.py) | `Sequential` 类：Functional 的特例，线性堆叠层。 |
| [tensorflow/python/keras/engine/training.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/training.py) | `Model` 类：在 `Layer` 之上增加 `compile`/`fit`/`evaluate`/`predict` 等训练与推理能力。 |
| [tensorflow/python/keras/layers/core.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/layers/core.py) | 内置层的实现集合，本讲以 `Dense` 作为具体例子。 |

继承关系一览（自底向上）：

```
module.Module
   └── Layer                 （base_layer.py：状态 + 前向）
          └── Model           （training.py：+ compile/fit）
                 └── Functional   （functional.py：图模型）
                        └── Sequential （sequential.py：线性堆叠）
```

---

## 4. 核心概念与源码讲解

### 4.1 Layer：状态与计算的最小封装（base_layer）

#### 4.1.1 概念说明

`Layer` 是 Keras 最核心的抽象。一个层 = **权重状态（变量）+ 前向计算逻辑（`call`）**。它同时是「层」和「模型」的共同祖先——`Model` 本身就是 `Layer` 的子类，只是多了训练方法。

用户使用层的标准三步：

1. `layer = MyLayer(...)`：构造，只存配置。
2. `y = layer(x)`：调用，框架会在第一次调用时自动 `build`（创建权重），然后执行 `call`。
3. 训练时 `layer.trainable_weights` 被优化器更新。

这里的关键设计是：**用户从不需要直接调 `build` 或 `call`，一律通过 `layer(x)` 这个统一入口，由 `__call__` 统一处理所有横切关注点**（形状检查、mask 传播、training 标志、连通性记录、autocast、autograph 转换……）。

#### 4.1.2 核心流程

`Layer.__call__` 的大致执行流程（函数式构造模式除外）：

```text
layer(x)
 ├─ _split_out_first_arg：把第一个参数 inputs 单独抽出（历史原因特殊对待）
 ├─ _in_functional_construction_mode?   ← 输入是 KerasTensor 时为真
 │     └─ 是：走 _functional_construction_call（见 4.2，仅记录图）
 ├─ 否（真实张量）：
 │     ├─ numpy/标量 → 转成 Tensor
 │     ├─ 处理 mask 传播、确定 training 模式
 │     ├─ 进入 call_context
 │     ├─ if not self.built: _maybe_build(inputs)   ← 按需创建权重
 │     ├─ 若开启 autocast：把输入 cast 到 compute_dtype
 │     └─ outputs = self.call(inputs, *args, **kwargs)  ← 真正的前向计算
 ├─ 活动正则化、设置 mask 元数据
 └─ return outputs
```

而 `build` 与 `call` 的契约是子类实现的两个钩子：

- `build(self, input_shape)`：在「第一次调用且尚未 built」时被框架自动调用，用于创建权重。默认实现只是把 `self.built = True`。
- `call(self, inputs, *args, **kwargs)`：前向计算逻辑，**子类必须覆盖**（默认实现原样返回输入）。

#### 4.1.3 源码精读

**`Layer` 类定义与构造器**。注意 `__init__` 里 `self.built = False`，并初始化了 `_trainable_weights`、`_inbound_nodes_value` 等关键容器：

[base_layer.py:97](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/base_layer.py#L97) —— `Layer` 继承自 `module.Module`（提供变量自动追踪）。

[base_layer.py:344-346](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/base_layer.py#L344-L346) —— `self.built = False`、`self._input_spec = None`，这是「延迟建造」的起点。

[base_layer.py:400-401](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/base_layer.py#L400-L401) —— `_inbound_nodes_value` / `_outbound_nodes_value`，函数式图中用于记录上下游连通关系的节点列表。

**`build` 与 `call` 的默认实现**：

[base_layer.py:445-462](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/base_layer.py#L445-L462) —— 默认 `build`：仅记录输入形状、置 `built = True`，不创建任何权重。

[base_layer.py:465-507](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/base_layer.py#L465-L507) —— 默认 `call`：原样返回输入（恒等层）。真正的计算逻辑由子类覆盖。

**`__call__` 的分叉**。这是整条入口最关键的一段。先判断是否处于函数式构造模式，再走两条不同分支：

[base_layer.py:981-983](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/base_layer.py#L981-L983) —— 若处于函数式构造模式，转交 `_functional_construction_call`，**不执行真实计算，只记录图**。

[base_layer.py:1034-1043](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/base_layer.py#L1034-L1043) —— 真实张量路径：`if not self.built: self._maybe_build(inputs)`，随后执行 `outputs = call_fn(inputs, ...)`。注意 eager 模式直接用 `self.call`，非 eager（图模式）则用 `self._autographed_call()`（用 AutoGraph 包装，见 u9-l2）。

**`_in_functional_construction_mode` 的判定**——极其简单：输入里只要有一个是 `KerasTensor`，就认为是函数式构造：

[base_layer.py:3254-3260](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/base_layer.py#L3254-L3260) —— 这就是「`Input` 产出的符号张量会触发建图」的根源。

**按需建造 `_maybe_build`**：

[base_layer.py:2649-2659](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/base_layer.py#L2649-L2659) —— 关键判断：`if not hasattr(self.build, '_is_default')`，即**只有当子类覆盖了 `build`** 时才真正调用它；无论如何最后都执行 `Layer.build(self, ...)` 保证 `built = True`。注意 `build` 在 `init_scope` 中执行，避免在图追踪期污染符号张量。

**`add_weight`：创建并登记变量**。这是层拥有权重的统一入口，承接 u2-l3 的 `tf.Variable`：

[base_layer.py:594-602](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/base_layer.py#L594-L602) —— 推断 dtype、把字符串形式的 initializer/regularizer/constraint 转成对象（`initializers.get(...)` 等「注册表 + 工厂」模式）。

[base_layer.py:684-689](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/base_layer.py#L684-L689) —— 创建出的变量按 `trainable` 标志分别塞进 `_trainable_weights` 或 `_non_trainable_weights`，并经 `backend.track_variable` 全局登记。这正是 `model.trainable_variables` 能收集到所有权重的来源。

**一个真实例子：`Dense` 层**。它把上面三个契约全部体现出来：

[core.py:1131-1160](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/layers/core.py#L1131-L1160) —— `Dense.__init__` 只存配置（`units`、initializer 等），不创建权重。

[core.py:1174-1193](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/layers/core.py#L1174-L1193) —— `Dense.build`：拿到输入最后一维 `last_dim` 后，用 `add_weight` 创建 `kernel`（形状 `[last_dim, units]`）和 `bias`，最后置 `built = True`。这完美展示了「为何要把权重创建推迟到 `build`」：构造期还不知道 `last_dim`。

[core.py:1224-1239](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/layers/core.py#L1224-L1239) —— `Dense.call`：核心就是一次 `gen_math_ops.MatMul(a=inputs, b=self.kernel)`，再 `bias_add`、再激活。最终落到的正是 u4 讲过的底层 op。

#### 4.1.4 代码实践

**实践目标**：亲手验证「构造期不创建权重、第一次调用才 build」这一延迟建造机制。

**操作步骤**（在装好 `tf-nightly` 或本仓库编译版的环境里运行，纯 Python）：

```python
import tensorflow as tf

d = tf.keras.layers.Dense(3)        # 仅构造
print("构造后 built =", d.built)     # 预期 False
print("权重数 =", len(d.weights))    # 预期 0

y = d(tf.zeros((4, 5)))             # 第一次调用，触发 build
print("调用后 built =", d.built)     # 预期 True
print("kernel 形状 =", d.kernel.shape)   # 预期 (5, 3)
print("bias 形状   =", d.bias.shape)     # 预期 (3,)
```

**需要观察的现象**：构造后 `built=False` 且无权重；调用一次后 `built=True`，`kernel` 形状 `[5, 3]` 由输入最后一维 `5` 与 `units=3` 共同决定。

**预期结果**：若一切正常，`d.kernel.shape == TensorShape([5, 3])`。若环境不可运行，**待本地验证**；但可对照源码确认：`build` 在 [core.py:1174](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/layers/core.py#L1174) 用 `shape=[last_dim, self.units]`，输入 `5` 维 → `[5, 3]`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Dense` 把权重创建放在 `build` 而不是 `__init__`？

**参考答案**：构造期不知道输入的最后一维 `last_dim`，无法确定 `kernel` 的形状 `[last_dim, units]`；只有第一次拿到真实输入形状时才能创建（见 [core.py:1168-1181](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/layers/core.py#L1168-L1181)）。

**练习 2**：`__call__` 里 `if not hasattr(self.build, '_is_default')` 这个判断（[base_layer.py:2650](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/base_layer.py#L2650)）的用意是什么？

**参考答案**：只有子类**覆盖**了 `build`（此时它不再是带 `_is_default` 标记的默认实现）时，框架才真正调用它去创建权重；否则跳过，避免对没有自定义 build 的层做无意义调用。

---

### 4.2 Functional Model：把层组装成一张可重放的图（functional + node）

#### 4.2.1 概念说明

当你写下：

```python
inputs = tf.keras.Input(shape=(4,))
x = tf.keras.layers.Dense(8, activation='relu')(inputs)
outputs = tf.keras.layers.Dense(2)(x)
model = tf.keras.Model(inputs, outputs)
```

这段代码运行时，**前两行层调用并没有做任何计算**。因为 `inputs` 是 `tf.keras.Input` 产出的「符号张量」（`KerasTensor`），触发了 4.1 讲的「函数式构造模式」。每次 `layer(x)` 只做一件事：跑一遍 `call` 来**探测输出形状**，然后**记录一条连通关系到 `Node` 里**，并返回一个新的符号张量。

最后一行 `tf.keras.Model(inputs, outputs)` 才真正把这些零散的层「编织」成一个模型：它会**沿着每个输出张量携带的 `_keras_history`（来源层 + 节点 + 输出索引）回溯整张图，做拓扑排序，得到 `layers` 列表和 `nodes_by_depth`**。此后模型调用 `model(x)` 时，不再是简单地把输入灌进第一层，而是**按深度顺序重放整张内部图**。

这就解释了 Functional 模型的两个关键特性：

- **可序列化**：结构是一张显式的图，能被 `get_config`/SavedModel 完整保存。
- **形状推断免费**：因为建图时已经探测过每个层的输出形状。

#### 4.2.2 核心流程

**建图阶段**（构造模型时）：

```text
Input(shape) → 产生 KerasTensor，并创建一个 InputLayer + 一个 Node（标记 is_input）
layer(tensor)
 └─ _functional_construction_call:
      ├─ _keras_tensor_symbolic_call → _infer_output_signature
      │     ├─ 在一张 scratch FuncGraph 里把 KerasTensor 转成 placeholder
      │     ├─ _maybe_build + call_fn(placeholders)   ← 探测输出（建权重！）
      │     └─ 把输出包回 KerasTensor
      └─ _set_connectivity_metadata → new Node(...)
            ├─ Node 挂到 self._inbound_nodes
            ├─ 对每个输入 KerasTensor：把它来源层的 _outbound_nodes 加上本 Node
            └─ 给每个输出张量打上 _keras_history = (layer, node_index, tensor_index)

Model(inputs, outputs)
 └─ _init_graph_network:
      ├─ 从 outputs._keras_history 收集 _output_layers
      ├─ 从 inputs._keras_history 收集 _input_layers
      ├─ _map_graph_network(inputs, outputs): 拓扑排序 → nodes_by_depth, layers
      └─ 标记 built = True（权重已在建图时随各层 build 创建）
```

**执行阶段**（调用模型时）：

```text
model(real_inputs)
 └─ Functional.call → _run_internal_graph:
      ├─ tensor_dict = {输入张量id: [真实输入]*使用次数}
      ├─ for depth in 深度降序:
      │     for node in 该深度的节点:
      │         args = node.map_arguments(tensor_dict)   ← 用真实张量替换符号张量
      │         outputs = node.layer(*args)              ← 重新调用对应层
      │         tensor_dict[输出id] = outputs
      └─ 收集 _output_layers 对应的张量作为模型输出
```

注意 `_run_internal_graph` 里 `node.layer(*args)` 重新走 `Layer.__call__`，但这次输入是真实张量，所以走的是 4.1 的「真实计算」分支——这就是「重放」。

#### 4.2.3 源码精读

**`_infer_output_signature`：在 scratch 图里探测输出**。

[base_layer.py:869-893](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/base_layer.py#L869-L893) —— 新建一个临时 `FuncGraph`，把 `KerasTensor` 转成 placeholder，`_maybe_build` 后执行 `call_fn`。这里 `call` 真的被执行了一次（用 placeholder），所以**权重在这一步就被创建了**。最后把输出包回 `KerasTensor` 返回。

**`_set_connectivity_metadata`：创建连通性节点**。

[base_layer.py:2582-2588](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/base_layer.py#L2582-L2588) —— 构造一个 `Node`，由它自己负责更新两侧的连通关系。

**`Node.__init__` 的连线**。

[node.py:98-109](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/node.py#L98-L109) —— 这是整张图的「胶水」：把 `self` 追加到本层的 `_inbound_nodes`；对本层每个输入 KerasTensor，把它来源层（`kt._keras_history.layer`）的 `_outbound_nodes` 追加 `self`；最后给本层每个输出张量打上 `_keras_history`。**`_keras_history` 是后续拓扑排序与重放的唯一线索。**

**`Functional._init_graph_network`：组装模型**。

[functional.py:182-195](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/functional.py#L182-L195) —— 从每个输出/输入张量的 `_keras_history` 提取来源层，分别填入 `_output_layers`、`_input_layers`。

[functional.py:198-205](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/functional.py#L198-L205) —— 调 `_map_graph_network` 做拓扑排序，得到 `_network_nodes`、`_nodes_by_depth`、以及作为 `_self_tracked_trackables` 的 `layers`（这使子层权重被自动追踪）。注意 [functional.py:156](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/functional.py#L156) 直接把 `self.built = True`——因为各层权重在前面建图时已经创建。

**`_map_graph_network`：拓扑排序**。

[functional.py:901-903](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/functional.py#L901-L903) —— 「depth」= 某节点到输出节点的层数距离。先从 outputs 出发反向遍历得到 `nodes_in_decreasing_depth`。

[functional.py:929-931](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/functional.py#L929-L931) —— 核心递推：一个节点的 depth = `max(它所有父节点的 depth) + 1`，从而把节点按「距输出远近」分层。`_nodes_by_depth` 就是 `{depth: [节点列表]}`。

[functional.py:982-990](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/functional.py#L982-L990) —— 一项重要的合法性检查：若某节点的输入无法从给定 inputs 计算出来，抛 `Graph disconnected` 错误。这就是「漏连一层」时报错的来源。

**`_run_internal_graph`：按深度重放**。

[functional.py:546-560](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/functional.py#L546-L560) —— 深度降序遍历每个节点，用 `node.map_arguments(tensor_dict)` 把符号输入替换成已计算的真实张量（见 [node.py:144-157](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/node.py#L144-L157)），再 `node.layer(*args)` 重新执行该层，把输出写回 `tensor_dict`。这就是 Functional 模型前向的本质。

**`Sequential`：Functional 的线性特例**。

[sequential.py:104-125](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/sequential.py#L104-L125) —— `Sequential.__init__` **故意跳过** `Functional.__init__`（`super(functional.Functional, self).__init__` 是 bad-super-call，意为调到更上层），因为此时还没有 Input/Output。它把 `_graph_initialized = False`，等第一次 `add` 一个带 `input_shape` 的层（或调用模型）时，再在 [sequential.py:266](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/sequential.py#L266) 的 `_build_graph_network_for_inferred_shape` 里真正调 `Functional._init_graph_network` 把线性栈变成一张图。

#### 4.2.4 代码实践

**实践目标**：对照源码看清「层调用在建图、`Model()` 在拓扑排序」。

**操作步骤**：

```python
import tensorflow as tf

inputs = tf.keras.Input(shape=(4,))
x = tf.keras.layers.Dense(8, name="d1", activation='relu')(inputs)
outputs = tf.keras.layers.Dense(2, name="d2")(x)
model = tf.keras.Model(inputs, outputs)

# 观察 1：模型自带一张拓扑排好序的层列表
print("layers =", [l.name for l in model.layers])   # 预期 ['d1', 'd2']

# 观察 2：每个输出张量都带着来源信息（_keras_history）
print("outputs[0] 来源层 =", outputs._keras_history.layer.name)  # 预期 d2

# 观察 3：调用模型 = 重放内部图
y = model(tf.zeros((3, 4)))
print("输出形状 =", y.shape)   # 预期 (3, 2)
```

**需要观察的现象**：`model.layers` 是按拓扑顺序排好的 `['d1', 'd2']`；`outputs` 的 `_keras_history.layer` 指向 `d2`；模型调用产生 `(3, 2)` 输出。

**预期结果**：以上均成立。若想深入，可在 [functional.py:546](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/functional.py#L546) 的循环里加日志（仅作阅读理解，不改源码），观察 `node.layer.name` 的执行顺序。

#### 4.2.5 小练习与答案

**练习 1**：如果故意写成 `model = tf.keras.Model(inputs, x)`（输出用了中间张量 `x` 而非 `outputs`），`d2` 这一层会发生什么？

**参考答案**：`d2` 不会出现在 `_map_graph_network` 回溯到的子图里（因为从输出 `x` 回溯不到 `d2`），于是它不会被收入 `model.layers`，其权重也不会被模型追踪——典型的「断开的层」。

**练习 2**：`_run_internal_graph` 为什么按「深度降序」遍历，而不是按 `model.layers` 列表顺序？

**参考答案**：深度降序保证了「计算某节点时，它的所有父节点（输入来源）都已算完并写入 `tensor_dict`」；而 `model.layers` 只是层对象的线性列表，不携带调用顺序与多输入/共享层信息，不足以正确重放一张任意图。

---

### 4.3 Model 与训练循环（training）

#### 4.3.1 概念说明

`Model`（[training.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/training.py)）在 `Layer` 之上增加了**训练与推理能力**：`compile`（配置）、`fit`（训练循环）、`evaluate`（评估）、`predict`（推理）。

理解训练循环只需抓住三个层次的解耦，这是 Keras 设计最优雅的地方：

| 层次 | 方法 | 职责 | 可否覆盖 |
| --- | --- | --- | --- |
| 单步数学逻辑 | `train_step(data)` | 一次前向 + 损失 + 反向 + 指标 | **鼓励覆盖** |
| 执行封装 | `make_train_function()` | 把 `train_step` 包进 `tf.function`、对接 distribute | 可覆盖 |
| 数据循环 | `fit(...)` | 遍历数据、回调、epoch、验证 | 一般不覆盖 |

也就是说，`fit` 默认实现是「for 每个 batch → 调 `train_function` → 触发回调」，而 `train_function` 默认是「`tf.function` 包裹的 `train_step`」。想自定义训练逻辑，覆盖 `train_step` 即可，无需重写 `fit`。

另一个巧妙设计是 `__new__` 里的**类替换（class swapping）**：当你写 `tf.keras.Model(inputs, outputs)`（传了 inputs/outputs），`Model.__new__` 检测到参数形态后，实际返回的是一个 `Functional` 实例而非普通 `Model`。这样同一个 `tf.keras.Model(...)` 调用，根据参数自动变成函数式模型。

#### 4.3.2 核心流程

`compile` 配置阶段：

```text
model.compile(optimizer, loss, metrics)
 ├─ _get_optimizer：把 optimizer 字符串/对象规整成 Optimizer 实例（必要时包 LossScaleOptimizer）
 ├─ compiled_loss  = LossesContainer(loss, loss_weights, output_names)
 ├─ compiled_metrics = MetricsContainer(metrics, weighted_metrics, ...)
 ├─ _configure_steps_per_execution(N)   ← 每个 tf.function 跑几个 batch
 ├─ _reset_compile_cache()              ← 清掉缓存的 train/test_function
 └─ _is_compiled = True
```

一步训练 `train_step`（承接 u5-l1 的自动微分）：

```text
train_step(data):
 ├─ x, y, sample_weight = unpack(data)
 ├─ with GradientTape() as tape:        ← 前向 + 录制
 │      y_pred = self(x, training=True)
 │      loss   = compiled_loss(y, y_pred, sample_weight, regularization_losses=self.losses)
 ├─ optimizer.minimize(loss, self.trainable_variables, tape=tape)  ← 反向 + 更新
 ├─ compiled_metrics.update_state(y, y_pred, sample_weight)
 └─ return {各 metric 的 result()}
```

`make_train_function` 封装阶段（承接 u3-l4 的 `tf.function`）：

```text
make_train_function():
 ├─ step_function(model, iterator):
 │      data = next(iterator)
 │      outputs = distribute_strategy.run(run_step)   ← 跑一步（含 train_counter+1）
 ├─ train_function = （按 steps_per_execution 包 1 步或多步）
 ├─ if not run_eagerly:
 │      train_function = def_function.function(train_function, experimental_relax_shapes=True)
 └─ cache 到 self.train_function
```

`fit` 主循环（简化）：

```text
fit(x, y, epochs, batch_size, callbacks):
 ├─ data_handler 把 x,y 变成可迭代的数据流（data_adapter）
 └─ for epoch in range(epochs):
        callbacks.on_epoch_begin
        for step, data in enumerate(data_handler):
            callbacks.on_train_batch_begin
            logs = self.train_function(iterator)   ← 1190 行
            callbacks.on_train_batch_end
        (可选) 验证、on_epoch_end
```

#### 4.3.3 源码精读

**类替换：`Model.__new__`**。

[training.py:128-131](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/training.py#L128-L131) —— `is_functional_model_init_params`：当参数是 `(inputs, outputs)` 形态时为真。

[training.py:215-222](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/training.py#L215-L222) —— `Model.__new__` 命中该条件时，**实际 `return functional.Functional(skip_init=True, ...)`**。这就是 `tf.keras.Model(inputs, outputs)` 得到的是 `Functional` 实例的原因。对子类化模型（`class M(tf.keras.Model)`）则走 `super().__new__`。

**`compile`：配置四件套**。

[training.py:568-582](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/training.py#L568-L582) —— `_validate_compile` 校验参数；`self.optimizer` 经 `_get_optimizer` 规整；`compiled_loss` / `compiled_metrics` 分别是 `LossesContainer` / `MetricsContainer`（它们负责多输出模型的损失/指标分发）；最后 `_is_compiled = True`。注意每次 `compile` 都会 `_reset_compile_cache()`，清掉之前缓存好的 `train_function`。

**`train_step`：一步训练的数学**。这是本讲与 u5-l1 的直接交汇点：

[training.py:799-804](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/training.py#L799-L804) —— `with backprop.GradientTape() as tape:` 里做前向 `self(x, training=True)` 和算 `loss`；出 with 后 `self.optimizer.minimize(loss, self.trainable_variables, tape=tape)` 完成反向传播与变量更新。这里的 `GradientTape` 正是 u5-l1 讲过的 Eager 自动微分机制；`optimizer.minimize` 内部会用 `tape.gradient` 取梯度再 `apply_gradients`（详见 u5-l5）。

[training.py:805-813](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/training.py#L805-L813) —— 更新指标并返回 `{metric_name: result}` 字典，这些值会传给 `on_train_batch_end` 回调。

**`make_train_function`：包进 `tf.function`**。

[training.py:839-854](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/training.py#L839-L854) —— `step_function` 取一个 batch、用 `distribute_strategy.run` 跑一步（支持分布式，见 u6-l4），并让 `_train_counter` 自增。

[training.py:870-873](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/training.py#L870-L873) —— 若非 `run_eagerly`，用 `def_function.function(...)`（即 `tf.function`）包裹整个 `train_function`。这一步把 Python 的 `train_step` 编译成图（u3-l4），是 Keras 训练快的关键。`experimental_relax_shapes=True` 让相同结构、不同 batch 大小的输入复用同一张 traced 图。

**`fit`：数据循环**。

[training.py:1189-1195](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/training.py#L1189-L1195) —— 每个 batch 的核心就一行：`logs = self.train_function(iterator)`，前后穿插 `on_train_batch_begin/end` 回调。`fit` 本身几乎不含数学，它是个「数据 + 回调 + 进度」的编排器。

#### 4.3.4 代码实践

**实践目标**：通过覆盖 `train_step`，验证「一步训练 = GradientTape + minimize」，并对照源码理解三层解耦。

**操作步骤**：

```python
import tensorflow as tf

# 用 Functional API 搭一个最小回归模型（对应本讲综合实践）
inp = tf.keras.Input(shape=(1,))
out = tf.keras.layers.Dense(1)(inp)
model = tf.keras.Model(inp, out)
model.compile(optimizer='sgd', loss='mse')

# 观察：类型是 Functional（因为 __new__ 做了类替换）
print("模型类型 =", type(model).__name__)   # 预期 Functional

# 训练一个 batch，对照 train_step：前向→loss→minimize
import numpy as np
x = np.array([[0.0], [1.0], [2.0], [3.0]], dtype='float32')
y = np.array([[-1.0], [1.0], [3.0], [5.0]], dtype='float32')  # y = 2x - 1
h = model.fit(x, y, epochs=50, verbose=0)
print("训练后 loss =", h.history['loss'][-1])
print("学到的 kernel/bias ≈", model.layers[1].kernel.numpy().ravel(),
      model.layers[1].bias.numpy())   # 预期接近 [2.0] 与 [-1.0]
```

**进阶**（源码阅读型实践）：自定义一个 `train_step` 打印每步 loss，验证它真的被 `fit` 调用：

```python
class MyModel(tf.keras.Model):
    def __init__(self):
        super().__init__()
        self.d = tf.keras.layers.Dense(1)
    def call(self, x):
        return self.d(x)
    def train_step(self, data):                       # 覆盖 4.3 讲的方法
        x, y = data_adapter.unpack_x_y_sample_weight(data) \
               if False else (data[0], data[1])       # 示例代码：简化 unpack
        with tf.GradientTape() as tape:
            y_pred = self(x, training=True)
            loss = self.compiled_loss(y, y_pred)
        self.optimizer.minimize(loss, self.trainable_variables, tape=tape)
        return {"loss": loss}
```

> 上述 `MyModel` 仅为说明覆盖点的**示例代码**（`data_adapter` 的真实导入见 [training.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/training.py) 顶部，真实 unpack 写法见 [training.py:797](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/training.py#L797)）。生产中应直接用 `data_adapter.expand_1d` / `unpack_x_y_sample_weight`。

**需要观察的现象**：`type(model).__name__ == 'Functional'`；训练 50 轮后 loss 显著下降，`kernel≈2.0`、`bias≈-1.0`。

**预期结果**：以上成立即说明 `train_step` 的 `GradientTape + minimize` 链路正确工作。若数值不收敛，**待本地验证**（可能与初始值/学习率有关，可增大 epochs）。

#### 4.3.5 小练习与答案

**练习 1**：为什么每次 `model.compile(...)` 之后，下一次 `fit` 会重新 trace `tf.function`？

**参考答案**：`compile` 末尾调用 `_reset_compile_cache()`（[training.py:581](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/training.py#L581)）把缓存的 `train_function` 置空，`make_train_function` 检测到 `self.train_function is None`（[training.py:836](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/training.py#L836)）就会重建并重新 trace。

**练习 2**：`run_eagerly=True` 时，`train_step` 还会被包进 `tf.function` 吗？

**参考答案**：不会。[training.py:870](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/training.py#L870) 的 `if not self.run_eagerly:` 条件不成立，`train_function` 保持为普通 Python 函数，每个 batch 都以 Eager 方式逐步执行——这正是调试自定义层/`train_step` 时的常用手段。

---

## 5. 综合实践

**任务**：用 Functional API 搭一个与两层 `Sequential` 等价的模型，并完整跑通「编译 → 训练 → 评估」，对照本讲三个模块的源码解释每一步发生了什么。

```python
import tensorflow as tf
import numpy as np

# === 方式 A：Functional API ===
inp = tf.keras.Input(shape=(4,), name="features")
h   = tf.keras.layers.Dense(8, activation='relu', name="hidden")(inp)
out = tf.keras.layers.Dense(1, name="logit")(h)
m_a = tf.keras.Model(inp, out, name="func")

# === 方式 B：等价的 Sequential ===
m_b = tf.keras.Sequential([
    tf.keras.Input(shape=(4,)),
    tf.keras.layers.Dense(8, activation='relu'),
    tf.keras.layers.Dense(1),
], name="seq")

for m in (m_a, m_b):
    m.compile(optimizer='adam', loss='mse', metrics=['mae'])

x = np.random.randn(64, 4).astype('float32')
y = np.random.randn(64, 1).astype('float32')

for m in (m_a, m_b):
    print("==", m.name, "==")
    print("type     :", type(m).__name__)          # A:Functional  B:Sequential
    print("layers   :", [l.name for l in m.layers])
    m.fit(x, y, epochs=3, batch_size=16, verbose=2)
    print("weights  :", [(w.name, w.shape) for w in m.weights])
```

**对照源码逐项解释**（请边运行边核对）：

1. **构造阶段**：方式 A 中 `Dense(8)(inp)` 触发函数式构造模式（[base_layer.py:981](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/base_layer.py#L981)），各层在 scratch 图里 `build` 出权重（[base_layer.py:891](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/base_layer.py#L891)），并由 `Node` 连线（[node.py:98](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/node.py#L98)）；`Model(inp, out)` 经 `__new__` 类替换（[training.py:217](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/training.py#L217)）得到 `Functional`，再拓扑排序（[functional.py:198](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/functional.py#L198)）。
2. **`type` 行**：方式 A 是 `Functional`，方式 B 是 `Sequential`（`Sequential` 是 `Functional` 子类）。
3. **`layers` 行**：两者都应得到两个 `Dense` 层，顺序与拓扑一致。
4. **`compile`**：配置 optimizer/loss/metrics（[training.py:571-576](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/training.py#L571-L576)）。
5. **`fit` 的每一步**：`train_function`（被 `tf.function` 包裹的 `train_step`）里 `self(x)` 走 `_run_internal_graph` 重放整图（[functional.py:546](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/functional.py#L546)），`GradientTape` 录制、`optimizer.minimize` 更新（[training.py:799-804](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/training.py#L799-L804)）。
6. **`weights` 行**：4 个变量（两个 Dense 各一个 kernel + bias），验证 `add_weight` → `_trainable_weights` 的登记链路（[base_layer.py:684-689](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/base_layer.py#L684-L689)）。

完成本任务后，你应当能用自己的话讲清：**一次 `model.fit` 背后，从 `Layer.__call__` 的分叉、到 Functional 图的拓扑排序与重放、再到 `train_step` 的 `GradientTape`，整条链路是如何衔接的。**

---

## 6. 本讲小结

- `Layer` 是「权重状态 + `call` 前向逻辑」的封装；统一入口 `__call__` 负责按需 `build`、mask/training 处理、以及在函数式构造模式下追踪子图。权重经 `add_weight` 创建并登记到 `_trainable_weights`/`_non_trainable_weights`（承接 u2-l3）。
- 「构造期只存配置、第一次调用才 `build`」是层的延迟建造范式，`Dense` 是典型范例：`build` 里才知道输入维度、才能定 `kernel` 形状。
- 函数式构造模式由「输入是否为 `KerasTensor`」触发（[base_layer.py:3254](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/base_layer.py#L3254)）：此时层调用不计算，只探测输出形状并经 `Node` 记录连通关系、给输出打 `_keras_history`。
- `Functional._init_graph_network` 沿 `_keras_history` 回溯，用 `_map_graph_network` 做拓扑排序，得到 `layers` 与 `nodes_by_depth`；模型调用时 `_run_internal_graph` 按深度降序重放各层。`Sequential` 是其线性特例。
- `Model` 在 `Layer` 之上增加训练能力，三层解耦：`train_step`（单步数学，含 `GradientTape`，承接 u5-l1）、`make_train_function`（包 `tf.function`，承接 u3-l4）、`fit`（数据 + 回调编排）。
- `Model.__new__` 的类替换使 `tf.keras.Model(inputs, outputs)` 自动得到 `Functional` 实例；`compile` 配置 optimizer/loss/metrics 并清空函数缓存。

## 7. 下一步学习建议

- **u5-l5 优化器与训练循环**：本讲只用了 `optimizer.minimize(loss, trainable_variables, tape=tape)`，下一讲会深入 `optimizer_v2` 内部如何用 `tape.gradient` 取梯度、如何维护 Adam 的一阶/二阶动量并 `apply_gradients`。
- **u6-l4 分布式策略**：`train_step` 里的 `distribute_strategy.run` 是分布式训练的入口，建议接着读 `distribute_lib` / `mirrored_strategy` 理解变量镜像与梯度聚合。
- **u5-l3 SavedModel**：Functional 模型之所以「可序列化」，正是因为它有显式的内部图；结合 `saving` 模块理解 `model.save` 如何把这张图连同变量一起落盘。
- **延伸阅读**：自定义层/模型可参考 `tensorflow/python/keras/layers/core.py` 中的 `Dense`、`Dropout`（含 `training` 分支）实现；想理解 `train_step` 的覆盖范式，可读源码注释中引用的官方指南「Customizing what happens in fit」。
