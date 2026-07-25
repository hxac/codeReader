# 自动微分与 gradients

## 1. 本讲目标

学完本讲后，你应当能够：

- 用「反向模式（reverse-mode）自动微分」解释为什么训练一个标量 loss 时只需一次前向 + 一次反向就能得到对所有参数的梯度。
- 读懂图模式下 `tf.gradients` 是如何**构造一张反向梯度子图**的（`gradients_util._GradientsHelper` 的 BFS + pending count 算法）。
- 读懂 Eager 模式下 `tf.GradientTape` 是如何在前向执行时**把 op 录制到 tape 上**、再在 `.gradient()` 时**反向重放**的（`backprop.py`）。
- 理解「图模式走 Graph 节点、Eager 模式走 tape 条目，但二者共用同一份**梯度函数注册表**」这一统一设计。
- 能用 `GradientTape` 计算一个简单函数的梯度，并对照源码说清「前向 op 是如何被记录的」。

## 2. 前置知识

本讲假定你已经掌握前几讲的内容，特别是：

- **计算图与执行模型**（u3）：知道 Graph 由 Node/Edge 组成，知道 Eager 模式下 op 一调用就执行（见 [execute.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/execute.py) 的 `quick_execute`）。
- **tf.function / ConcreteFunction**（u3-l4）：知道 `@tf.function` 会把 Python 函数 trace 成一张图。
- **Python op 包装与 gen_\*_ops**（u4-l5）：知道用户调用的 `tf.*` 最终会落到由代码生成器产出的 `gen_*_ops.py` 包装函数。

此外需要一点多元微积分基础：链式法则（chain rule）。简单复习：若 \(L\) 是标量，\(y = f(x)\) 是某个 op，则

\[
\frac{\partial L}{\partial x} = \frac{\partial L}{\partial y}\cdot\frac{\partial y}{\partial x}
\]

若 \(x\) 同时喂给多个输出 \(y_i\)，则要把各路贡献相加：

\[
\frac{\partial L}{\partial x} = \sum_i \frac{\partial L}{\partial y_i}\cdot\frac{\partial y_i}{\partial x}
\]

本讲要讲的就是 TensorFlow 如何把这条法则自动化、规模化地应用到整张计算图上。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [tensorflow/python/ops/gradients.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/gradients.py) | 纯 re-export 的薄壳，把 `tf.gradients` 等符号重新导出，本身无逻辑。 |
| [tensorflow/python/ops/gradients_impl.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/gradients_impl.py) | `tf.gradients` 的对外入口，签名校验后委托给 `gradients_util`。 |
| [tensorflow/python/ops/gradients_util.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/gradients_util.py) | **本讲核心之一**：图模式反向微分算法 `_GradientsHelper`、BFS、pending count、聚合。 |
| [tensorflow/python/eager/backprop.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/backprop.py) | **本讲核心之二**：`GradientTape`、前向录制 `record_gradient`、梯度函数分发 `_gradient_function`。 |
| [tensorflow/python/eager/imperative_grad.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/imperative_grad.py) | Eager 反向求导的出口，把工作转交给 C++ 的 `TFE_Py_TapeGradient`。 |
| [tensorflow/python/eager/tape.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/tape.py) | tape 栈的 push/pop/watch 薄封装（底层是 C++）。 |
| [tensorflow/python/eager/backprop_util.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/backprop_util.py) | `IsTrainable`：判断一个 dtype/tensor 能否被微分。 |
| [tensorflow/python/framework/ops.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/framework/ops.py) | 全局梯度注册表 `_gradient_registry`、`RegisterGradient` 装饰器、`get_gradient_function`。 |
| [tensorflow/python/ops/math_grad.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/math_grad.py) | 各数学 op 的梯度函数实现示例（如 `_SquareGrad`）。 |
| [tensorflow/python/framework/python_op_gen.cc](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/framework/python_op_gen.cc) | 代码生成器：在每个生成的 op 包装里插入 `must_record_gradient` / `record_gradient` 调用。 |

> 阅读建议：先看 4.1 建立数学直觉和「梯度函数注册表」这个共享基石；4.2 和 4.3 分别讲图模式与 Eager 模式两条独立链路；4.4 揭示二者最终共用同一套梯度函数。

## 4. 核心概念与源码讲解

### 4.1 反向模式自动微分的数学直觉与共享梯度注册表

#### 4.1.1 概念说明

自动微分（Automatic Differentation, autodiff）不是数值微分（用 \((f(x+h)-f(x))/h\) 近似，有精度问题），也不是符号微分（把整张表达式展开成解析式，会指数爆炸）。它的做法是：**把计算拆成一个个原子 op，每个 op 局部地知道自己该如何求导，再按链式法则把局部导数组合起来。**

自动微分有两种模式：

- **前向模式（forward mode）**：从输入往输出传播 \(\partial y/\partial x\)。要得到对一个标量输出关于 \(n\) 个输入的梯度，需要跑 \(n\) 遍。
- **反向模式（reverse mode / backpropagation）**：从输出往输入传播 \(\partial L/\partial(\cdot)\)。**一次**前向 + **一次**反向就能得到标量 loss 对全部输入的梯度。

机器学习几乎总是「标量 loss 对百万参数求导」，所以 TensorFlow 选的是**反向模式**。本讲只讲反向模式。

反向模式的核心抽象是「**每个 op 配一个梯度函数 grad_fn**」。对于一个输出为 \(y\)、输入为 \(x_1,\dots,x_m\) 的 op，给定**上游梯度** \(\bar{y} = \partial L/\partial y\)，grad_fn 返回每个输入的**下游梯度** \(\bar{x_i} = \partial L/\partial x_i\)：

\[
\bar{x_i} = \bar{y}\cdot\frac{\partial y}{\partial x_i}
\]

grad_fn 把「\(y\) 如何依赖 \(x_i\)」这一局部知识封装起来，与全局的 \(L\) 无关。于是整条链路只需把上游梯度 \(\bar{y}\) 一路往回传即可。

#### 4.1.2 核心流程

TensorFlow 用一个**全局注册表**把「op 类型名 → grad_fn」存起来。两条求导链路（图模式 / Eager 模式）都会查这张表：

1. 程序员/框架为某个 op 写一个梯度函数，例如 `def _SquareGrad(op, grad): ...`。
2. 用 `@ops.RegisterGradient("Square")` 装饰它，装饰器把函数写入全局 `_gradient_registry`（一个以 op 类型字符串为键的字典）。
3. 求导时，对图/tape 中的每个 op，按其类型名 `lookup` 出对应的 grad_fn 并调用。

grad_fn 的**契约**固定不变（见装饰器文档）：一个有 \(m\) 个输入、\(n\) 个输出的 op，其 grad_fn 接受 1 个 `Operation` 加 \(n\) 个上游梯度张量，返回 \(m\) 个下游梯度张量。

#### 4.1.3 源码精读

全局注册表就是一个普通的 `Registry` 对象，键为字符串、值为 Python 可调用对象，`lookup` 找不到就抛 `LookupError`：[tensorflow/python/framework/registry.py:81-96](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/framework/registry.py#L81-L96)（`lookup` 按 name 取已注册对象）。

梯度注册表正是它的一个实例，模块级单例：[tensorflow/python/framework/ops.py:1750-1752](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/framework/ops.py#L1750-L1752)（`gradient_registry = _gradient_registry = registry.Registry("gradient")`）。

`RegisterGradient` 是一个装饰器类，`__call__` 里把被装饰函数注册进表里：[tensorflow/python/framework/ops.py:1755-1800](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/framework/ops.py#L1755-L1800)。注意它的文档（1770-1774 行）给出了契约示例——`"Sub"` 的 grad_fn 接受 `(unused_op, grad)` 返回 `(grad, negative(grad))`。

以 `Square`（\(y=x^2\)）为例，其梯度函数返回 \(2x\cdot\bar{y}\)：[tensorflow/python/ops/math_grad.py:697-704](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/math_grad.py#L697-L704)（`_SquareGrad` 读取前向 op 的输入 `x`，返回 `grad * 2 * x`）。`op.inputs[0]` 复用了前向 op 记录下来的输入，这就是 grad_fn 能拿到前向中间值的原因。

最后，`get_gradient_function(op)` 是查表的统一入口，先看 op 是否自定义了 `_gradient_function`，否则按类型名查注册表：[tensorflow/python/framework/ops.py:1843-1856](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/framework/ops.py#L1843-L1856)。本讲 4.2（图模式）和 4.4（Eager 模式）最终都会落到这里。

#### 4.1.4 代码实践

**实践目标**：亲手验证 grad_fn 的契约，并确认 `@RegisterGradient` 确实把函数放进了全局表。

**操作步骤**（示例代码，可在装有 `tensorflow` 的环境运行）：

```python
import tensorflow as tf
from tensorflow.python.framework import ops

# 1) 查注册表：Square 的 grad_fn 就是 math_grad._SquareGrad
grad_fn = ops.gradient_registry.lookup("Square")
print("Square 的梯度函数:", grad_fn.__name__)

# 2) 用 GradientTape 验证 y = x^2 在 x=3 处的梯度应为 2*x = 6
x = tf.constant(3.0)
with tf.GradientTape() as t:
    t.watch(x)
    y = x * x          # 前向：触发 "Mul"（也会被录制）
print("dy/dx =", t.gradient(y, x).numpy())   # 预期 6.0
```

**需要观察的现象**：`gradient_registry.lookup("Square")` 返回的不是 `None`，而是一个名为 `_SquareGrad` 的函数；梯度值约为 `6.0`。

**预期结果**：打印出 `_SquareGrad` 与 `6.0`。若环境未安装 tensorflow，则「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：一个 op 有 3 个输入、2 个输出，它的 grad_fn 应当接受几个参数、返回几个值？
**答案**：接受 `1 个 Operation + 2 个上游梯度`（对应 2 个输出），返回 `3 个下游梯度`（对应 3 个输入）。

**练习 2**：为什么反向模式比前向模式更适合深度学习？
**答案**：深度学习的 loss 通常是标量、参数却有上百万个；反向模式只需 1 次前向 + 1 次反向就能得到标量 loss 对全部参数的梯度，复杂度约为一次前向的常数倍，与参数个数无关；前向模式则要跑「参数个数」遍。

---

### 4.2 图模式符号反向微分：gradients_util._GradientsHelper

#### 4.2.1 概念说明

在 TF1 的图模式下，`tf.gradients(ys, xs)` **并不立即算出数值**，而是**在原图上追加一张「反向梯度子图」**——这就是源码注释里说的 "graph generation for computation of gradients"。你随后用 `Session.run` 执行这张新图，才得到真实梯度数值。这是**符号微分**：先构造符号化的求导计算，再统一执行。

> 注意：TF2 默认 Eager，`tf.gradients` 在 Eager 下会直接报错（见下文 520-522 行），必须用 `tf.GradientTape`。本节理解的是「图是怎么被反向构造的」，这套算法在 `@tf.function` trace 出的图内部同样适用。

#### 4.2.2 核心流程

`_GradientsHelper` 的算法可以概括为「**反向拓扑遍历 + 入度计数**」，分四步：

1. **框定子图**：用 `_PendingCount` 做两次 BFS，找出所有「从 xs 出发能到达、且能到达 ys」的 op（`between_ops`），并计算每个 op 的 `pending_count`——即它还有多少个下游消费点尚未把梯度传回来。
2. **播种**：用 `_DefaultGradYs` 给每个 `y` 填默认上游梯度（通常是全 1），用 `_SetGrad` 写入 grads 表。
3. **反向循环**：用一个队列，弹出 `pending_count==0` 的 op（其全部下游梯度已到齐）：
   - `_AggregatedGrads` 把该 op 各输出收到的多条梯度用 `AddN` 求和；
   - `ops.get_gradient_function(op)` 取出 grad_fn，调用 `grad_fn(op, *out_grads)` 得到对各输入的梯度 `in_grads`；
   - 对每个输入用 `_SetGrad` 把 `in_grads` 累加进去；
   - `_UpdatePendingAndEnqueueReady` 把各输入 op 的 pending_count 减一，减到 0 就入队。
4. **收集**：返回 `[_GetGrad(grads, x) for x in xs]`。

pending_count 的作用是处理「一个张量被多个 op 消费」的分叉——必须等所有下游分支的梯度都到齐并相加后，才能处理这个 op。这正是链式法则里那个求和 \(\sum_i\)。

#### 4.2.3 源码精读

入口 `tf.gradients` 只是签名校验 + 委托：[tensorflow/python/ops/gradients_impl.py:55-64](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/gradients_impl.py#L55-L64)（`@tf_export(v1=["gradients"])`，参数含 `gate_gradients`、`aggregation_method`、`stop_gradients` 等）。注意 `gradients.py` 本身只是把这里的 `gradients` 重新 re-export 的薄壳：[tensorflow/python/ops/gradients.py:17-26](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/gradients.py#L17-L26)。

真正的实现在 `_GradientsHelper`。开头第一步就是拒绝 Eager：[tensorflow/python/ops/gradients_util.py:520-522](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/gradients_util.py#L520-L522)（`executing_eagerly()` 时抛 `RuntimeError`，提示改用 `tf.GradientTape`）。

算法注释把思路讲得很清楚（从 ys 反向走、按 id 逆序访问、聚合后调 grad_fn）：[tensorflow/python/ops/gradients_util.py:579-592](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/gradients_util.py#L579-L592)。其中 `_PendingCount(...)` 完成子图框定与入度计数。

`_PendingCount` 内部：先用 `_MarkReachedOps` 从 xs 正向 BFS 标记可达 op：[tensorflow/python/ops/gradients_util.py:48-65](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/gradients_util.py#L48-L65)；再从 ys 反向 BFS，把「既被 xs 可达、又在通往 ys 路径上」的 op 收进 `between_ops`，并统计每个 op 被多少个 between-op 当作输入（即 `pending_count`）：[tensorflow/python/ops/gradients_util.py:103-134](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/gradients_util.py#L103-L134)。

播种阶段：`_DefaultGradYs` 在 `grad_ys=None` 时用全 1 张量作为初始上游梯度：[tensorflow/python/ops/gradients_util.py:169-182](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/gradients_util.py#L169-L182)（`array_ops.ones(shape(y))`）。

反向主循环：弹出就绪 op、聚合输出梯度、取 grad_fn：[tensorflow/python/ops/gradients_util.py:627-649](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/gradients_util.py#L627-L649)。若找不到 grad_fn，会抛出经典的 `LookupError: No gradient defined for operation ...`（677-688 行）。

实际调用 grad_fn 并校验返回数量：[tensorflow/python/ops/gradients_util.py:706-759](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/gradients_util.py#L706-L759)。注意 742-743 行的 `grad_fn(op, *out_grads)`——这正是 4.1 里定义的契约调用；`_MaybeCompile` 会把 XLA 相关逻辑接进来。

把算出的输入梯度写回、并推进队列：[tensorflow/python/ops/gradients_util.py:766-785](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/gradients_util.py#L766-L785)（`_SetGrad` 累加、`_UpdatePendingAndEnqueueReady` 减入度并入队）。最终取结果：[tensorflow/python/ops/gradients_util.py:789](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/gradients_util.py#L789)。

两个关键字典操作的实现：`_SetGrad` 把梯度追加到「某 op 某输出端口」的梯度列表里（多条梯度先攒成 list）：[tensorflow/python/ops/gradients_util.py:843-855](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/gradients_util.py#L843-L855)；`_GetGrad` 在最后取回结果，处理「ys 与 xs 不相连」时返回 None 或 zeros：[tensorflow/python/ops/gradients_util.py:867-887](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/gradients_util.py#L867-L887)。

多个梯度如何合并由 `AggregationMethod` 决定（默认 `ADD_N`，用一次 `AddN` 把所有项加起来）：[tensorflow/python/ops/gradients_util.py:952-996](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/gradients_util.py#L952-L996)，具体合并发生在 `_AggregatedGrads`：[tensorflow/python/ops/gradients_util.py:999-1037](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/gradients_util.py#L999-L1037)。

#### 4.2.4 代码实践

**实践目标**：用 TF1 兼容模式跑一次真正的「图模式 `tf.gradients`」，观察它构造出的反向子图节点。

**操作步骤**（示例代码，需在 TF2 下用 `tf.compat.v1`）：

```python
import tensorflow as tf
tf.compat.v1.disable_eager_execution()      # 切回图模式

a = tf.constant(3.0, name="a")
b = tf.constant(4.0, name="b")
c = tf.square(a, name="c")                    # c = a^2
d = tf.multiply(c, b, name="d")               # d = c * b，对 a 的梯度 = 2*a*b = 24

g = tf.gradients([d], [a])[0]                 # 构造反向子图（此时不算值）
print("梯度节点:", g.name)                     # 形如 gradients/mul_grad/Mul...

with tf.compat.v1.Session() as sess:
    print("梯度值:", sess.run(g))              # 预期 24.0
```

**需要观察的现象**：调用 `tf.gradients` 时**没有**立刻得到数值，而是返回一个 `Tensor`（一张新图节点）；只有 `sess.run(g)` 才算出 `24.0`。这印证了「图模式是符号化地追加反向子图」。

**预期结果**：打印出一个 `gradients/...` 开头的节点名和数值 `24.0`。TF2 默认 Eager，若忘记 `disable_eager_execution()` 会在 `tf.gradients` 处抛 `RuntimeError`。环境无 tensorflow 时「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`pending_count[op]` 的语义是什么？为什么需要它？
**答案**：它表示该 op 的输出还有多少个下游消费点没把梯度传回来。只有当它归零（所有下游梯度到齐并已聚合），这个 op 才「就绪」可以被反向处理。它保证了链式法则里「多条路径要在分叉点先求和」的正确顺序。

**练习 2**：为什么 `_GradientsHelper` 开头要在 `executing_eagerly()` 时抛错？
**答案**：因为这套算法依赖静态的 Graph 节点关系来构造反向子图；Eager 模式下没有常驻 Graph（op 一执行就消失），反向关系由 `GradientTape` 在前向录制时动态保存，所以图模式算法不适用，应改用 `tf.GradientTape`。

---

### 4.3 Eager 模式的梯度录制：backprop.GradientTape 与 tape 栈

#### 4.3.1 概念说明

Eager 模式下没有常驻 Graph，op 一执行就产生数值。那反向求导所需的前向信息（每个 op 的输入、输出、属性、以及它们的先后顺序）从哪里来？答案是 **Tape（磁带）**：在前向执行时，把每个被「监视」的 op **录制**到一条线程局部的 tape 上，反向求导时再倒带重放。

`tf.GradientTape` 是对外的上下文管理器。它的设计要点有三个：

- **tape 是一个栈**：可以嵌套（求高阶导数），内层 tape 只录制自己激活期间发生的 op。
- **按需录制**：只有当某个 op 至少有一个输入被「watch」时才被录入，避免无谓的内存开销。trainable Variable 默认自动 watch。
- **一次性 vs 持久**：默认 tape 在调用一次 `.gradient()` 后就释放资源；`persistent=True` 才能多次求导。

#### 4.3.2 核心流程

Eager 前向录制的完整链路：

1. `with tf.GradientTape() as t:` 进入时，`__enter__` → `_push_tape` 把一条新 tape 压入**线程级 tape 栈**（C++ 维护）。
2. 用户执行 op（如 `y = x * x`），其 `gen_*_ops.py` 包装在 eager 快路径执行完后，会检查 `if _execute.must_record_gradient():`，若为真就调用 `_execute.record_gradient(op_name, inputs, attrs, outputs)`。
3. `record_gradient` 把 `(op_name, inputs, attrs, outputs)` 交给 C++ 的 `TFE_Py_RecordGradient`，写入当前栈上所有激活的 tape。
4. `with` 退出时 `__exit__` → `_pop_tape` 把 tape 弹出（停止录制，但 tape 对象保留数据）。
5. 调 `t.gradient(target, sources)` 时，把 tape、target、sources 交给 `imperative_grad`（见 4.4）反向重放。

这里有个精妙的「**桩替换（monkey-patch）**」设计：`execute.py` 里默认的 `must_record_gradient` 恒返回 `False`、`record_gradient` 是空函数——这样在不导入 backprop 时录制完全零开销。一旦 `backprop.py` 被导入，它就把这两个名字替换成真正实现，录制才被激活。

#### 4.3.3 源码精读

`execute.py` 里的「空桩」：[tensorflow/python/eager/execute.py:134-142](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/execute.py#L134-L142)（`must_record_gradient` 返回 `False`，`record_gradient` 为 `pass`）。

代码生成器在每个生成的 op 包装里都插入了「检查 + 录制」两句：[tensorflow/python/framework/python_op_gen.cc:1401](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/framework/python_op_gen.cc#L1401) 与 [tensorflow/python/framework/python_op_gen.cc:1431](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/framework/python_op_gen.cc#L1431)。也就是说，所有 `gen_*_ops.py` 里的 op 函数执行后都会走 `if _execute.must_record_gradient(): _execute.record_gradient(...)`。

`backprop.py` 在模块加载时做桩替换，并定义真正的判定与录制函数：[tensorflow/python/eager/backprop.py:156-176](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/backprop.py#L156-L176)。其中 `_must_record_gradient` 调 `TFE_Py_TapeSetIsEmpty()` 判断当前线程是否至少有一条激活 tape（156-157 行）；`record_gradient` 调 `TFE_Py_RecordGradient` 把 op 信息写入 tape（160-172 行）；最后两行（175-176 行）把 `execute` 模块上的两个名字替换掉。

`GradientTape` 类定义与构造：[tensorflow/python/eager/backprop.py:704-819](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/backprop.py#L704-L819)。注意 815-819 行的字段：`_tape`（底层 C++ tape 句柄）、`_persistent`、`_watch_accessed_variables`、`_recording`。

进出上下文压/弹 tape：[tensorflow/python/eager/backprop.py:821-848](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/backprop.py#L821-L848)。`_push_tape`（831-842 行）首次创建 tape 时调 `tape.push_new_tape`，重入时调 `tape.push_tape`；`_pop_tape`（844-848 行）调 `tape.pop_tape`。

底层 tape 栈操作都是对 C++ 的薄封装：[tensorflow/python/eager/tape.py:32-45](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/tape.py#L32-L45)（`push_new_tape`/`push_tape`/`watch`）与 [tensorflow/python/eager/tape.py:107-109](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/tape.py#L107-L109)（`pop_tape`）。`Tape` 类本身只是 C++ 句柄的 Python 壳：[tensorflow/python/eager/tape.py:20-29](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/tape.py#L20-L29)。

`watch` 方法：变量走 `watch_variable`、普通张量走 `watch`：[tensorflow/python/eager/backprop.py:864-884](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/backprop.py#L864-L884)。它会用 `IsTrainable` 校验 dtype（只有浮点/复数等可微）。

`IsTrainable` 列出可微 dtype 集合：[tensorflow/python/eager/backprop_util.py:53-66](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/backprop_util.py#L53-L66)（`float16/32/64`、`complex64/128`、`bfloat16`、`resource`、`variant`）。

`.gradient()` 方法的核心是把工作交给 `imperative_grad`，并在非持久 tape 用完后置 `_tape=None` 释放资源：[tensorflow/python/eager/backprop.py:1066-1077](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/backprop.py#L1066-L1077)。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：用 `GradientTape` 计算一个简单函数的梯度，并对照 `backprop.py` 说明前向 op 是如何被记录以供反向求导的。

**操作步骤**（示例代码，可在装有 tensorflow 的环境运行）：

```python
import tensorflow as tf

x = tf.constant(3.0)
with tf.GradientTape() as t:   # __enter__ -> _push_tape：把 tape 压入线程栈
    t.watch(x)                 # 标记 x 为求导源（常量默认不被 watch）
    y = x * x                  # 前向：gen_math_ops 执行后检查
                               #   _execute.must_record_gradient() == True
                               #   于是 _execute.record_gradient("Mul", [x,x], {}, y)
                               #   把这条 op 记录写进 tape
dy_dx = t.gradient(y, x)       # imperative_grad -> TFE_Py_TapeGradient 反向重放
print("dy/dx =", dy_dx.numpy())  # 预期 6.0（d/dx x^2 = 2x）
```

**对照源码理解录制**：

1. `with ... as t` 触发 [backprop.py:821-824](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/backprop.py#L821-L824) 的 `__enter__` → `_push_tape`，tape 栈非空。
2. `x * x` 走 `gen_math_ops.multiply` 的 eager 快路径；因为该生成函数尾部有 [python_op_gen.cc:1401/1431](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/framework/python_op_gen.cc#L1401) 插入的录制代码，且此时 `must_record_gradient()` 返回 True，于是调用 [backprop.py:160-172](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/backprop.py#L160-L172) 的 `record_gradient`，把 `(op_name="Mul", inputs=[x,x], attrs, outputs=y)` 写入 tape。
3. `t.gradient(y, x)` 调 [backprop.py:1066-1072](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/backprop.py#L1066-L1072) 的 `imperative_grad`，由 C++ 倒着重放 tape，对 `"Mul"` 调用其梯度函数（见 4.4）。

**需要观察的现象**：把 `t.watch(x)` 注释掉再运行，`dy_dx` 会变成 `None`——因为常量 `x` 没被监视，`x*x` 不会被录入与 `x` 相关的反向路径。这正说明「按需录制」。

**预期结果**：`6.0`；注释 `watch` 后为 `None`。环境无 tensorflow 时「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `execute.py` 默认把 `must_record_gradient` 实现成恒返回 `False`？
**答案**：性能。绝大多数 op 执行时并没有激活 tape，录制是不必要的开销。默认返回 `False` 让生成代码里的 `if` 立刻短路；只有 `backprop.py` 被导入并做桩替换后，录制才真正启用。这是一种零成本抽象。

**练习 2**：下面代码 `g` 会是多少？为什么？
```python
x = tf.constant(2.0)
with tf.GradientTape() as t:
    y = x ** 2          # 注意：没有 t.watch(x)，x 是常量
g = t.gradient(y, x)
```
**答案**：`g` 是 `None`。常量 `x` 默认不被 watch（只有 trainable Variable 会自动 watch），所以 `x**2` 虽然执行了，但 `x` 不在 tape 的监视源里，求导结果为 `None`。要得到 `4.0` 需先 `t.watch(x)`。

---

### 4.4 反向求导的统一出口：imperative_grad 与 _gradient_function

#### 4.4.1 概念说明

4.3 讲清楚了「前向如何录制」。本讲讲「反向如何重放」，并揭示一个关键统一性：**图模式和 Eager 模式最终调用的是同一套梯度函数。**

差别只在于「遍历的数据结构」不同：

- 图模式（4.2）：遍历静态 Graph 的 Node/Edge，靠 `pending_count` 决定顺序。
- Eager 模式（本节）：遍历 tape 里录下的 op 条目，由 C++ 的 `TFE_Py_TapeGradient` 算出等价的「下游使用计数」并反向遍历。

但二者在每个 op 上调用的是同一个 grad_fn——来自 4.1 的全局 `_gradient_registry`。这意味着为某个 op 写一次梯度函数，图模式和 Eager 模式就都能用。

#### 4.4.2 核心流程

Eager 反向重放的过程：

1. `GradientTape.gradient` 把 `(tape, targets, sources, output_gradients)` 传给 `imperative_grad.imperative_grad`。
2. `imperative_grad` 仅做参数整理，核心转交 C++ `TFE_Py_TapeGradient`。
3. C++ 端：过滤 tape，只保留「从被 watch 的 sources 到 targets」路径上的 op；计算每个张量还有多少个下游使用（等价于 `pending_count`）；按反向拓扑逐个 op 调用 Python 端注册的回调 `_gradient_function`。
4. `_gradient_function` 用录下的 `(inputs, outputs, attrs)` 构造一个 `_MockOp`（假冒的 Operation），再 `ops._gradient_registry.lookup(op_name)` 取出 grad_fn 并调用 `grad_fn(mock_op, *out_grads)`。
5. 把返回的输入梯度按 tape 的结构往回送，直到 sources 全部拿到梯度。

`_MockOp` 之所以必要，是因为 grad_fn 的契约要求第一个参数是 `Operation`（要能 `.inputs`/`.outputs`/`.get_attr`），而 tape 里存的只是裸的输入输出张量和属性扁平列表，需要一个适配器把它们包装成 grad_fn 期望的形状。

#### 4.4.3 源码精读

`imperative_grad` 几乎只做转发：[tensorflow/python/eager/imperative_grad.py:29-73](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/imperative_grad.py#L29-L73)，真正干活的是 67-73 行的 `pywrap_tfe.TFE_Py_TapeGradient`。文件顶部还定义了 `VSpace` 这个 namedtuple（23-26 行），它抽象出「向量空间」上的聚合/求零/求一运算，使 C++ 反向算法与具体张量实现解耦。

C++ 端会回调的 Python 函数 `_gradient_function`：[tensorflow/python/eager/backprop.py:118-150](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/backprop.py#L118-L150)。它构造 `_MockOp`（136 行），从注册表查 grad_fn（137 行），找不到就返回全 `None`（138-139 行，表示该 op 不可微、梯度为 0/不传），否则调用 `grad_fn(mock_op, *out_grads)`（148 行）。

把这个回调注册给 C++ 的那句关键代码：[tensorflow/python/eager/backprop.py:153](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/backprop.py#L153)（`pywrap_tfe.TFE_Py_RegisterGradientFunction(_gradient_function)`）。

`_MockOp` 伪装成 `Operation`，提供 `inputs`/`outputs`/`type`/`get_attr`：[tensorflow/python/eager/backprop.py:92-115](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/backprop.py#L92-L115)。注意它的 `get_attr` 需要先查属性类型（`op_attr_type`），因为录下的 attrs 是扁平的 `[..., name, value, ...]` 列表，需要按类型还原（102-107 行）。

> 对照 4.2 的 [gradients_util.py:742-743](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/gradients_util.py#L742-L743)：图模式是 `grad_fn(op, *out_grads)`（op 是真正的 `Operation`），Eager 模式是 `grad_fn(mock_op, *out_grads)`（op 是 `_MockOp`）——**同一个 grad_fn，两种调用方**。这就是「统一」的落点。

#### 4.4.4 代码实践

**实践目标**：体会「同一个 grad_fn 服务两种模式」，并观察 tape 的录制控制。

**操作步骤**（示例代码）：

```python
import tensorflow as tf
from tensorflow.python.framework import ops

# 1) 不管图模式还是 Eager 模式，Square 都用同一个 _SquareGrad
print("Square grad:", ops.gradient_registry.lookup("Square").__name__)

# 2) stop_recording：临时停止录制，被包住的 op 不进 tape
x = tf.constant(3.0)
with tf.GradientTape() as t:
    t.watch(x)
    a = x * 2.0
    with t.stop_recording():      # 这段不录制
        b = a * a
    y = b + 1.0                   # y 依赖未录制的 b
print("with stop_recording, dy/dx =", t.gradient(y, x))   # 预期 None（路径断了）

# 3) 对照：不 stop_recording
with tf.GradientTape() as t2:
    t2.watch(x)
    a = x * 2.0
    b = a * a
    y = b + 1.0                  # y = (2x)^2 + 1，dy/dx = 8x = 24
print("normal, dy/dx =", t2.gradient(y, x).numpy())       # 预期 24.0
```

**需要观察的现象**：第 2 段得到 `None`（`b=a*a` 没录进 tape，反向路径在 `a→b` 处断开）；第 3 段得到 `24.0`。这说明 tape 的「是否录制」直接决定反向能否走通——它就是 Eager 模式的「图结构」。

**预期结果**：`None` 与 `24.0`。`stop_recording` 对应 [backprop.py:886-916](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/backprop.py#L886-L916)。环境无 tensorflow 时「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 Eager 反向需要 `_MockOp`，而图模式不需要？
**答案**：图模式下被求导的 op 本身就是 `Operation` 对象，天然有 `.inputs/.outputs/.get_attr`；Eager 模式下 tape 里只存了扁平的 `(inputs, outputs, attrs)`，没有现成的 `Operation`，所以要用 `_MockOp` 把这些数据包装成 grad_fn 期望的接口形状。

**练习 2**：如果某个 op 没有注册梯度函数，图模式和 Eager 模式分别会发生什么？
**答案**：图模式在 [gradients_util.py:677-688](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/gradients_util.py#L677-L688) 抛 `LookupError: No gradient defined for operation ...`；Eager 模式下 [_gradient_function](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/backprop.py#L137-L139) 查到 `grad_fn is None` 时返回全 `None`，表示该 op 视作常数、梯度不向后传（最终相关 source 的梯度为 `None` 或 0）。

## 5. 综合实践

把本讲知识串起来：**用 tape 验证一条多 op 链路的梯度，并解释每一步在源码里对应什么。**

考虑 \(L = \mathrm{sum}((w\cdot x + b)^2)\)，其中 \(w, x, b\) 都是向量。请完成：

1. 用 `tf.Variable` 创建 `w`、`b`（自动被 tape watch），用 `tf.constant` 创建输入 `x` 并手动 `tape.watch(x)`。
2. 在 `with tf.GradientTape() as t:` 内计算 `L`，打印 `L` 的值。
3. 用 `t.gradient(L, [w, b, x])` 一次得到三个梯度。
4. 用 `persistent=True` 的 tape 再算一次对 `w` 的**二阶导数**（即对 `grad_w` 再求一次关于 `w` 的梯度），验证嵌套 tape 与 persistent 的用法。
5. **源码对照**：在你的代码旁注释——
   - `t.watch(x)` 对应 [backprop.py:864-884](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/backprop.py#L864-L884)；
   - 前向每个 op（MatMul、Add、Square、Sum）被录入，对应 [python_op_gen.cc:1401/1431](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/framework/python_op_gen.cc#L1401) 插入的录制代码 + [backprop.py:160-172](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/backprop.py#L160-L172) 的 `record_gradient`；
   - `t.gradient(...)` 对应 [backprop.py:1066-1072](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/backprop.py#L1066-L1072) → [imperative_grad.py:67-73](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/imperative_grad.py#L67-L73) 的 `TFE_Py_TapeGradient`，它逐个 op 回调 [backprop.py:118-150](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/backprop.py#L118-L150) 的 `_gradient_function`，最终查 [ops.py:1843-1856](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/framework/ops.py#L1843-L1856) 的注册表拿到 `MatMul/Add/Square/Sum` 各自的 grad_fn。

**自检**：把 `Square` 换成任意一个你不知道梯度公式的 op（例如 `tf.math.square` → `tf.math.sin`），只要它注册了梯度，tape 就能正确求导——这正是「每个 op 自带局部 grad_fn + 链式法则自动组合」的威力。预期数值结果「待本地验证」。

## 6. 本讲小结

- 自动微分 ≠ 数值/符号微分；TF 用**反向模式**，一次前向 + 一次反向即可得到标量 loss 对全部参数的梯度。
- 每个 op 配一个 **grad_fn**，存于全局 `_gradient_registry`；grad_fn 的契约是 `(op, *上游梯度) → (*下游梯度)`，如 `_SquareGrad` 返回 `grad * 2 * x`。
- **图模式** `tf.gradients` 走 `_GradientsHelper`：用 `_PendingCount` 做 BFS 框定子图并算入度，再反向遍历、聚合（`AddN`）、调 grad_fn，**追加一张反向子图**（符号微分），Eager 下不可用。
- **Eager 模式** 走 `GradientTape`：`__enter__` 把 tape 压入线程栈；前向 op 经代码生成器插入的 `must_record_gradient`/`record_gradient` 调用被录入 tape（默认空桩，`backprop.py` 导入时桩替换激活）；`.gradient()` 转 `imperative_grad` → C++ `TFE_Py_TapeGradient` 反向重放。
- 录制是**按需**的：只有被 `watch` 的张量/Variable 才触发相关 op 录入；trainable Variable 自动 watch，常量需手动 `watch`。
- **两条链路共用同一套梯度函数**：图模式传真正的 `Operation`，Eager 模式传 `_MockOp`，但都调 `ops._gradient_registry.lookup(op_type)` 得到的同一个 grad_fn。

## 7. 下一步学习建议

- **下一讲 u5-l2（tf.data 输入流水线）** 会转向数据侧，但 `tf.function` + `GradientTape` 仍是默认执行模型，本讲的「录制 / 重放」心智模型继续适用。
- 若想深入，建议阅读：
  - [tensorflow/python/eager/imperative_grad.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/imperative_grad.py) 中 `VSpace` 的定义，理解 C++ 反向算法如何与张量实现解耦；
  - [tensorflow/python/ops/math_grad.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/math_grad.py) 中 `MatMul` / `Conv2D` 等更复杂 op 的 grad_fn，体会「前向复用 + 链式法则」的写法；
  - [tensorflow/python/ops/custom_gradient.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/custom_gradient.py)，看用户如何自定义一段前向 + 梯度，它会绕过注册表直接给 op 挂 `_gradient_function`。
- 为 u5-l5（Optimizer 与训练循环）做铺垫：训练循环里的 `tape.gradient(loss, vars)` 得到的梯度，正是本讲产出的结果，下一步就是把它交给 optimizer 去更新变量。
