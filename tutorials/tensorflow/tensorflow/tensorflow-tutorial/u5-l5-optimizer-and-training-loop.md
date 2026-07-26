# Optimizer 与训练循环

## 1. 本讲目标

本讲承接 u5-l4（Keras 高层 API）。在 u5-l4 里我们看到 `Model.train_step` 的最后一行是：

```python
self.optimizer.minimize(loss, self.trainable_variables, tape=tape)
```

这一行把「前向算出的 loss」变成「变量被更新」。但这个魔法到底由谁完成？答案就是本讲的主角——**Optimizer（优化器）**。

学完本讲，你应当能够：

1. 说清 Optimizer 的**两段式 API**：`compute_gradients`（算梯度）与 `apply_gradients`（把梯度写回变量）。
2. 理解 Optimizer 内部维护的**三类状态**：步数计数器 `iterations`、**slot 变量**（如 Adam 的一阶/二阶矩）、**hyper 超参数**（如学习率、β）。
3. 看懂真正的参数更新发生在哪个方法（`_resource_apply_dense`），并对照 `Adam` 读懂数学公式如何落地成代码。
4. 区分新版 `OptimizerV2`（Keras）与旧版 `tf.compat.v1.train.Optimizer`（V1）在设计上的关键差异。
5. 自己手写一个最小的「前向 → 梯度 → `apply_gradients`」训练循环。

---

## 2. 前置知识

阅读本讲前，建议你已经掌握以下概念（详见前置讲义）：

- **Tensor 与 Variable**（u2-l1、u2-l3）：Variable 是可训练的持久状态，本讲里被 Optimizer 反复 `assign` 改写。
- **Operation 与计算图**（u2-l4、u3-l1）：Optimizer 产出的「更新」在图模式下是一批 op，需要被 run 才真正生效。
- **自动微分与 GradientTape**（u5-l1）：Optimizer 不自己求导，它消费 `tape.gradient(...)` 的产物。理解「grad_fn 把 loss 变成梯度」是理解本讲的前提。
- **ResourceVariable**（u2-l3）：现代 TF 的变量底层都是 resource variable，其句柄（`.handle`）是 Optimizer 更新时真正操作的对象。

几个本讲会用到的术语先做通俗解释：

- **梯度下降（gradient descent）**：让参数沿着 loss 下降最快的方向（负梯度）走一小步，公式为 \(\theta \leftarrow \theta - \eta \cdot g\)，其中 \(\eta\) 是学习率、\(g\) 是梯度。Optimizer 就是这套公式的「执行器」，不同优化器只是把「裸梯度」做了不同的加工（加权平均、自适应缩放等）。
- **动量（momentum）/ 自适应（adaptive）**：很多优化器不止用当前这一步的梯度，还会维护历史梯度的滑动平均（动量）或平方梯度的滑动平均（自适应学习率），这些「历史信息」就存放在 **slot 变量**里。
- **slot（槽位变量）**：Optimizer 为每个被训练变量额外创建的、不可训练（`trainable=False`）的辅助变量。例如 Adam 为每个变量配一对 slot：一阶矩 `m` 和二阶矩 `v`。
- **hyper（超参数）**：优化器自己的配置参数，如学习率、β₁、β₂。在 `OptimizerV2` 里它们被存成变量（或 schedule），因此能被 checkpoint 保存。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tensorflow/python/keras/optimizer_v2/optimizer_v2.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/optimizer_v2/optimizer_v2.py) | 新版优化器基类 `OptimizerV2`。定义两段式 API、slot/hyper/iterations 三类状态、分布式更新入口。本讲的主线。 |
| [tensorflow/python/keras/optimizer_v2/adam.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/optimizer_v2/adam.py) | Adam 的具体实现。是读懂「基类如何被子类化」的最佳范例，含融合与非融合两版。 |
| [tensorflow/python/training/optimizer.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/training/optimizer.py) | 旧版 `tf.compat.v1.train.Optimizer` 基类。用于对比 V1/V2 的设计差异。 |
| [tensorflow/python/training/gradient_descent.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/training/gradient_descent.py) | V1 的 `GradientDescentOptimizer`，最简单的 V1 子类范例。 |
| [tensorflow/python/keras/engine/training.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/training.py) | Keras `Model.train_step`，展示训练循环如何调用 optimizer（衔接 u5-l4）。 |

---

## 4. 核心概念与源码讲解

### 4.1 OptimizerV2 的两段式入口：minimize / compute_gradients / apply_gradients

#### 4.1.1 概念说明

无论优化器多复杂，它的对外接口都可以拆成两步：

1. **算梯度**：给定 loss 和一组变量，得到「每个变量对应的梯度」。
2. **应用梯度**：根据梯度，按某种规则改写变量值。

`OptimizerV2` 把这两步分别命名为 `_compute_gradients` 和 `apply_gradients`，并提供一个把两者串起来的便捷方法 `minimize`。这一点在类的文档里说得很直白——「`minimize()` 同时负责算梯度和应用梯度；若你想在中间加工梯度，就分别调用 `tf.GradientTape` 和 `apply_gradients()`」。

需要注意一个 TF2 的设计取向：**梯度计算被鼓励放到 Optimizer 之外**。推荐用法是用户自己开 `tf.GradientTape`（承接 u5-l1），再把算好的梯度喂给 `apply_gradients`；`minimize` 只是为了向后兼容而保留的「一步到位」入口。

#### 4.1.2 核心流程

`minimize` 的全部逻辑就是两行——先算梯度，再应用：

```
minimize(loss, var_list, tape=None)
  └─ grads_and_vars = _compute_gradients(loss, var_list, tape)
  └─ return apply_gradients(grads_and_vars)
```

`_compute_gradients` 内部做了三件事：①确保有一个 `GradientTape`（没有就自己 new 一个）；②在 tape 上下文里求值 loss；③调 `tape.gradient(loss, var_list)` 得到梯度，打包成 `(grad, var)` 列表返回。

`apply_gradients` 是真正干重活的方法，它的大致流程是：

```
apply_gradients(grads_and_vars)
  ├─ filter_empty_gradients(...)          # 过滤掉梯度为 None 的项
  ├─ _create_all_weights(var_list)        # 首次调用时惰性创建 iterations/hyper/slot
  ├─ _prepare(var_list)                   # 预算每台设备的 apply_state（含 lr_t）
  ├─ _transform_unaggregated_gradients    # 聚合前的梯度变换（钩子）
  ├─ _aggregate_gradients                 # 跨副本求和（分布式）
  ├─ _transform_gradients                 # clipnorm/clipvalue/global_clipnorm + 用户 transformers
  └─ _distributed_apply(...)              # 逐变量分发到 _resource_apply_dense / _sparse
        └─ 末尾 self._iterations.assign_add(1)   # 步数 +1
```

#### 4.1.3 源码精读

`OptimizerV2` 的类声明与基类（它继承自 `Trackable`，所以能被 checkpoint 保存）：

[optimizer_v2.py:111-L111](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/optimizer_v2/optimizer_v2.py#L111) —— `class OptimizerV2(trackable.Trackable)`，Keras 优化器的基类，文档注明「不要直接用它，请实例化 SGD/Adam 等子类」。

`minimize` 方法极简，只做转发：

[optimizer_v2.py:532-L534](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/optimizer_v2/optimizer_v2.py#L532-L534) —— 先 `_compute_gradients`，再 `apply_gradients`，这就是「两段式」的全部含义。

`_compute_gradients` 负责准备 tape 并求梯度：

[optimizer_v2.py:569-L584](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/optimizer_v2/optimizer_v2.py#L569-L584) —— 若调用方没传 `tape` 就新建一个 `backprop.GradientTape()`；若是 callable loss 就在 tape 上下文里 `loss()` 求值；最后由 `_get_gradients` 调 `tape.gradient` 得到 `(grad, var)` 列表。

注意 `_get_gradients` 本身只是一层薄包装：

[optimizer_v2.py:464-L467](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/optimizer_v2/optimizer_v2.py#L464-L467) —— 直接 `tape.gradient(loss, var_list)` 然后 `zip` 成对。真正的求导发生在 GradientTape 内部（见 u5-l1），Optimizer 并不重写微分逻辑。

`apply_gradients` 的关键几行（创建权重、预算、变换、分发）：

[optimizer_v2.py:634-L668](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/optimizer_v2/optimizer_v2.py#L634-L668) —— 先 `filter_empty_gradients` 去掉空梯度；在 `ops.init_scope()` 里调 `_create_all_weights(var_list)` 惰性创建所有状态；`_prepare(var_list)` 算出 `apply_state`；依次做「未聚合变换 → 跨副本聚合 → clip/transformer 变换」；最后交给 `_distributed_apply`。

末尾的「跨副本聚合」与「clip」分别由两个可定制方法承担：

[optimizer_v2.py:473-L499](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/optimizer_v2/optimizer_v2.py#L473-L499) —— `_aggregate_gradients` 默认调 `self.gradient_aggregator`（跨设备求和）；`_transform_gradients` 按 `clipvalue → clipnorm → global_clipnorm → 用户 gradient_transformers` 的顺序依次套用。clip 系列是优化器构造时通过 `clipnorm/clipvalue/global_clipnorm` 参数配置的。

> 小贴士：`experimental_aggregate_gradients=False` 可关闭内置聚合，让你自己 all_reduce 后再喂梯度——这在自定义分布式训练里很有用（详见 u6-l4）。

#### 4.1.4 代码实践

**目标**：亲眼看到「minimize = compute_gradients + apply_gradients」的二段结构，并验证变量确实被更新了。

**操作步骤**（示例代码，可直接在装好 tf 的 Python 里运行）：

```python
import tensorflow as tf

var = tf.Variable(10.0)                       # 待优化变量
opt = tf.keras.optimizers.SGD(learning_rate=0.1)
loss_fn = lambda: (var ** 2) / 2.0            # d(loss)/d(var) == var，当前梯度=10

# 方式 A：一步到位
opt.minimize(loss_fn, var_list=[var])
print("A 之后 var =", var.numpy())            # 期望 10 - 0.1*10 = 9.0

# 方式 B：拆成两步，中间能看到梯度
with tf.GradientTape() as tape:
    loss = loss_fn()
grads_and_vars = opt._compute_gradients(loss_fn, var_list=[var])  # 仅用于观察
print("梯度 =", grads_and_vars[0][0].numpy())  # 期望 9.0（上一步后 var 变 9）
opt.apply_gradients(zip([tape.gradient(loss, [var])[0]], [var]))
```

**需要观察的现象**：
- 方式 A 后变量从 10 变成 9.0，正好是 \(\theta - \eta g = 10 - 0.1\times10\)。
- `_compute_gradients` 返回的元组里，第一个元素就是梯度张量，第二个是变量本身。

**预期结果**：变量按 `var -= lr * grad` 单调下降。如果你看到变量没变，多半是忘了 `minimize` 在 eager 模式下会立即执行（图模式才需要 `.run()`）。

> 注意：示例里直接调用了带下划线的 `_compute_gradients`，仅为观察内部结构；生产代码请用公开的 `GradientTape` + `apply_gradients`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 TF2 官方推荐「自己写 GradientTape + apply_gradients」而不是用 `minimize`？
**参考答案**：因为这样可以在「求导」与「更新」之间插入自定义逻辑（梯度裁剪、日志、梯度惩罚等）。`minimize` 把两步焊死，无法插入中间加工。

**练习 2**：`apply_gradients` 里的 `_create_all_weights` 为什么放在 `ops.init_scope()` 里调用？
**参考答案**：因为首次应用梯度时才需要创建 `iterations`、hyper 变量和 slot 变量；`init_scope` 保证这些变量的创建跳出函数图（function graph）的上下文，落到默认图/全局 eager 状态，避免每个 trace 出来的 ConcreteFunction 各自创建一份。

---

### 4.2 OptimizerV2 的状态三件套：iterations、slot 变量、hyper 超参数

#### 4.2.1 概念说明

优化器不是「无状态」的纯函数。除了被训练的模型变量，它自己也要维护状态，否则 Adam 的「历史矩」、步数衰减的学习率就无从谈起。`OptimizerV2` 把自己的状态分成三类：

1. **`iterations`**：一个 `int64` 标量变量，记录「这个优化器已经更新过多少步」。学习率 schedule、Adam 的偏差校正都依赖它。
2. **slot 变量**：为每个被训练变量额外开的辅助变量（不可训练）。键结构是「变量 → {slot 名 → 变量}」两层字典。
3. **hyper 超参数**：学习率、β₁、β₂ 等。它们可以是 Python 数值、张量，也可以是 `LearningRateSchedule` 或任意 callable，每次 `apply_gradients` 时取最新值。

这三类状态之所以都做成 `tf.Variable`，是因为它们要能被 checkpoint 保存与恢复——`OptimizerV2` 继承 `Trackable` 正是为了这个。

#### 4.2.2 核心流程

状态在「首次 `apply_gradients`」时被创建（`_create_all_weights`），之后每次复用：

```
_create_all_weights(var_list)
  ├─ self.iterations        # 首次访问时创建 iter 变量
  ├─ _create_hypers()       # 把数值型 hyper 物化成变量
  └─ _create_slots(var_list) # 子类 override，为每个 var 建 slot
```

hyper 的存取是一对镜像方法：

- `_set_hyper(name, value)`：构造期登记。若 value 是数值则先记下原值，待 `_create_hypers` 时再物化成变量；若已是变量/schedule/callable 则直接存。
- `_get_hyper(name, dtype)`：取用时，若还没物化则先 `_create_hypers`；对 callable/schedule 会调用它得到当前值。

slot 的创建入口是 `add_slot(var, slot_name)`：按「变量键 → slot 名」两层字典存放，新建的 slot 变量会被 `colocate` 到对应主变量所在设备（避免跨设备通信），并加入 `self._weights`。

#### 4.2.3 源码精读

构造函数初始化三个核心容器：

[optimizer_v2.py:372-L377](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/optimizer_v2/optimizer_v2.py#L372-L377) —— `self._hyper = {}`（超参数表）、`self._slots = {}`（slot 两层字典）、`self._weights = []`（所有优化器变量的有序清单）。

`iterations` 用「懒创建 property」实现，首次访问才建变量：

[optimizer_v2.py:982-L994](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/optimizer_v2/optimizer_v2.py#L982-L994) —— 若 `self._iterations is None` 则用 `add_weight` 建一个标量 `int64` 变量（`trainable=False`、`ONLY_FIRST_REPLICA` 聚合），并追加进 `self._weights`。后续每次 `apply_gradients` 末尾的 `assign_add(1)` 就是给它加步数。

hyper 的存与取：

[optimizer_v2.py:779-L807](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/optimizer_v2/optimizer_v2.py#L779-L807) —— `_set_hyper` 对 `Trackable` 类型还会顺便登记依赖；`_get_hyper` 先确保 `_create_hypers` 已跑过，再处理「schedule/callable 取当前值」「按 dtype cast」两种情况。

`_create_hypers` 把纯数值 hyper 物化成变量（schedule/callable 不物化）：

[optimizer_v2.py:961-L980](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/optimizer_v2/optimizer_v2.py#L961-L980) —— 遍历 `self._hyper`，跳过已是张量/变量/callable 的项，其余用 `add_weight(shape=[], trainable=False, ...)` 变量化。

> 妙处：`__getattribute__` 与 `__setattr__` 被重写，使得你可以像访问普通属性一样读写超参数——`opt.learning_rate = 0.05` 会被 `__setattr__` 路由到 `_set_hyper`，详见 [optimizer_v2.py:829-L860](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/optimizer_v2/optimizer_v2.py#L829-L860)。旧名 `lr` 也被兼容映射到 `learning_rate`。

slot 的创建：

[optimizer_v2.py:866-L924](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/optimizer_v2/optimizer_v2.py#L866-L924) —— `add_slot` 按 `_var_key(var)` 取/建二级字典，用主变量的形状与 dtype 创建一个 `trainable=False` 的变量，并在 `strategy.extended.colocate_vars_with(var)` 下创建以保证同设备。新建的 slot 进 `self._weights` 以便被 checkpoint 跟踪。

`_var_key` 决定「同一个主变量」的身份：图模式用 shared name，eager 模式用 unique id，分布式变量先取主容器，见 [optimizer_v2.py:1419-L1439](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/optimizer_v2/optimizer_v2.py#L1419-L1439)。

学习率的衰减/调度统一在 `_decayed_lr` 收口：

[optimizer_v2.py:1004-L1014](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/optimizer_v2/optimizer_v2.py#L1004-L1014) —— 若 `learning_rate` 是 `LearningRateSchedule`，就用 `self.iterations` 当作 `local_step` 调用它；若设了 legacy 的 `decay`，再做反比衰减。

#### 4.2.4 代码实践

**目标**：观察一个 Adam 优化器自带的「优化器变量」到底有哪些。

**操作步骤**：

```python
import tensorflow as tf

v = tf.Variable([1.0, 2.0])
opt = tf.keras.optimizers.Adam(learning_rate=0.1)
opt.minimize(lambda: tf.reduce_sum(v ** 2), var_list=[v])

for w in opt.weights:
    print(w.name, w.shape, w.numpy())
print("iterations =", opt.iterations.numpy())
print("slot names =", opt.get_slot_names())
print("m slot =", opt.get_slot(v, 'm').numpy())
```

**需要观察的现象**：`opt.weights` 会列出一长串变量——第一个是 `iter`（步数，值为 1），之后对每个主变量有 `m` 和 `v` 两个 slot（初始为 0，第一步后已有非零值）。

**预期结果**：对一个变量，Adam 有 `1(iter) + 1(var 的 m) + 1(var 的 v) = 3` 个优化器变量。这正是 adam.py 注释里说的「V2 optimizer has 2x + 1 variables」的由来（x 为主变量数）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 slot 变量要设 `trainable=False`？
**参考答案**：slot 是优化器内部的累积量（动量、二阶矩），它们应当被优化器按固定公式更新，而不是被另一层梯度下降训练。设 `trainable=False` 可避免它们进入 `model.trainable_variables`，从而不被 GradientTape 监视、不被任何优化器当作目标。

**练习 2**：把学习率从数值换成 `ExponentialDecay` schedule 后，`_create_hypers` 的行为有何不同？
**参考答案**：schedule 是 callable，`_create_hypers` 会跳过它（不物化成变量），改在 `_get_hyper`/`_decayed_lr` 时每次调用 `schedule(self.iterations)` 取当前值。这正是「学习率随步数变化」的实现机理。

---

### 4.3 真正的更新发生在哪：_resource_apply_dense 与 Adam 实例

#### 4.3.1 概念说明

前两节讲了 Optimizer 的「壳」——API、状态管理。但「把变量改一点点」的那条算式到底写在哪里？答案是两个由子类必须实现的抽象方法：

- `_resource_apply_dense(grad, var, apply_state)`：梯度是稠密 `Tensor` 时走这里。
- `_resource_apply_sparse(grad, var, indices, apply_state)`：梯度是稀疏 `IndexedSlices` 时走这里（典型场景是对 embedding 做 `tf.gather` 后的反向传播）。

基类 `OptimizerV2` 把这两个方法声明为抛 `NotImplementedError`，强迫子类实现。所以**写一个新优化器 = 继承 OptimizerV2 + 实现这两个方法（+ `_create_slots`/`get_config`）**。

#### 4.3.2 核心流程

先看分发：`apply_gradients` 最终走 `_distributed_apply`，里面的 `apply_grad_to_update_var` 根据「梯度是稠密还是稀疏」分流：

```
_distributed_apply → apply_grad_to_update_var(var, grad):
  if grad 是 IndexedSlices:
      _resource_apply_sparse_duplicate_indices(grad.values, var, grad.indices)
  else:
      update_op = _resource_apply_dense(grad, var, apply_state)
      if var.constraint: var.assign(var.constraint(var))   # 投影约束
```

之后所有 per-variable 的 `update_op` 被 `group` 到一起，并在末尾给 `iterations` 加 1。

再以 Adam 为例，它的数学更新规则（论文 Kingma & Ba 2014，采用「Section 2.1 之前」的 ε-hat 形式）为：

\[
lr_t = \text{learning\_rate} \cdot \frac{\sqrt{1 - \beta_2^t}}{1 - \beta_1^t}
\]

\[
m_t = \beta_1 m_{t-1} + (1 - \beta_1) g
\]

\[
v_t = \beta_2 v_{t-1} + (1 - \beta_2) g^2
\]

\[
\theta_t = \theta_{t-1} - lr_t \cdot \frac{m_t}{\sqrt{v_t} + \epsilon}
\]

其中 \(t\) 是步数（`local_step = iterations + 1`），\(\beta_1^t\)、\(\beta_2^t\) 用于**偏差校正**（抵消零初始化导致的早期偏小）。Adam 子类要做的事，就是把这套公式映射到 slot 变量 `m`/`v` 上。

#### 4.3.3 源码精读

基类的两个抽象方法（子类必须实现）：

[optimizer_v2.py:1239-L1251](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/optimizer_v2/optimizer_v2.py#L1239-L1251) —— `_resource_apply_dense`，注释写明 `handle` 是指向待更新变量的 resource 句柄，默认抛 `NotImplementedError`。

稀疏路径多一层「去重」保护：

[optimizer_v2.py:1253-L1300](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/optimizer_v2/optimizer_v2.py#L1253-L1300) —— `_resource_apply_sparse_duplicate_indices` 先用 `_deduplicate_indexed_slices` 把重复索引的梯度求和，再交给唯一的 `_resource_apply_sparse`，保证「重复索引先相加再更新」这一正确语义。

分发逻辑在 `_distributed_apply` 的内层函数里：

[optimizer_v2.py:684-L706](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/optimizer_v2/optimizer_v2.py#L684-L706) —— `apply_grad_to_update_var` 按 `grad` 是否为 `IndexedSlices` 分流；稠密路径调 `_resource_apply_dense`，若变量带 `constraint` 则在更新后再 `var.assign(var.constraint(var))` 做投影。

末尾把步数 +1：

[optimizer_v2.py:729-L739](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/optimizer_v2/optimizer_v2.py#L729-L739) —— eager 下直接 `self._iterations.assign_add(1)`；图模式下在 `control_dependencies([group(update_ops)])` 之后做，保证「先更新变量再记步」的顺序。

现在看 Adam 如何实现这套公式。构造期登记四个 hyper，epsilon 作为普通属性：

[adam.py:104-L118](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/optimizer_v2/adam.py#L104-L118) —— `_set_hyper` 登记 `learning_rate/beta_1/beta_2`（及 legacy `decay`），默认 `lr=0.001, β₁=0.9, β₂=0.999, ε=1e-7`。

`_create_slots` 为每个主变量建 `m`（一阶矩）和 `v`（二阶矩），开了 amsgrad 再加 `vhat`：

[adam.py:120-L129](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/optimizer_v2/adam.py#L120-L129) —— 注意两个**独立的 for 循环**，注释说这是为了「尊重 v1 的 slot 变量顺序」，从而保证 checkpoint 兼容。

偏差校正与有效学习率在 `_prepare_local` 里预先算好（每次 apply 前算一次，避免每个变量重复算）：

[adam.py:131-L154](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/optimizer_v2/adam.py#L131-L154) —— `local_step = iterations + 1`，`beta_1_power = β₁^t`、`beta_2_power = β₂^t`，有效学习率 `lr = lr_t * sqrt(1 - β₂^t)/(1 - β₁^t)`，连同 ε、`one_minus_beta_*` 一并塞进 `apply_state`。

稠密路径直接调一个融合 C++ kernel，把 m/v/var 的更新一次性完成：

[adam.py:166-L186](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/optimizer_v2/adam.py#L166-L186) —— `_resource_apply_dense` 取出 slot `m`/`v`，把 var/m/v 的句柄和预算好的系数一起喂给 `gen_training_ops.ResourceApplyAdam`。这个融合 op 等价于上面四条公式，但在 C++ 内一次遍历完成，省去多次 kernel 启动与中间张量。

> 对比：稀疏路径 [adam.py:203-L241](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/optimizer_v2/adam.py#L203-L241) 没有融合 kernel，而是用 `assign` + `_resource_scatter_add` 手写 \(m_t\)、\(v_t\) 的更新，再 `assign_sub` 更新 var——因为稀疏更新只触及部分行，无法套用稠密融合 op。

同文件里还有一个 `NonFusedAdam`，它用 `@def_function.function(jit_compile=True)` 把同样的数学包成 XLA 编译函数（[adam.py:422-L441](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/optimizer_v2/adam.py#L422-L441)），适合需要算子融合但又不想用固定 C++ kernel 的场景（承接 u7-l2 的 XLA/JIT）。

#### 4.3.4 代码实践

**目标**：手写一个最小训练循环，用 Adam 拟合一个简单目标，并对照源码说明每一步在做什么。

**操作步骤**：

```python
import tensorflow as tf

# 目标：让 var 逼近 0。loss = var^2/2，梯度 = var。
var = tf.Variable(5.0)
opt = tf.keras.optimizers.Adam(learning_rate=0.5)

for step in range(3):
    with tf.GradientTape() as tape:
        loss = (var ** 2) / 2.0
    grad = tape.gradient(loss, [var])[0]          # ① 前向 + 求梯度
    opt.apply_gradients([(grad, var)])            # ② 应用梯度
    m = opt.get_slot(var, 'm').numpy()            # ③ 观察一阶矩 slot
    print(f"step={step} var={var.numpy():.4f} grad={grad.numpy():.4f} m={m:.4f}")
print("总步数 iterations =", opt.iterations.numpy())
```

**需要观察的现象**：
- 第 1 步：`m` 从 0 变成 `(1-β₁)*grad = 0.1 * 5 = 0.5`，对应公式 \(m_t=(1-\beta_1)g\)（因为 \(m_{t-1}=0\)）。
- `var` 每步都在变小，但下降量受偏差校正影响（首步有效 lr 被 \(\sqrt{1-\beta_2^t}/(1-\beta_1^t)\) 放大）。
- 循环结束后 `iterations == 3`，说明 `apply_gradients` 每调用一次步数 +1。

**预期结果**：`var` 单调下降趋近 0；`m`、`v` 逐步累积；`iterations` 等于循环次数。

**对照源码**：把第 ② 步与 [adam.py:166-L186](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/optimizer_v2/adam.py#L166-L186) 对应——你传进去的 `grad` 就是 `ResourceApplyAdam` 的 `grad` 入参，`m`/`v` slot 即公式里的 \(m_{t-1}\)、\(v_{t-1}\)，融合 op 在内部把它们更新为 \(m_t\)、\(v_t\) 并改写 var。第 ③ 步看到的 `m` 正是 [adam.py:120-L129](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/optimizer_v2/adam.py#L120-L129) 建出来的 slot。

#### 4.3.5 小练习与答案

**练习 1**：为什么稠密路径用融合 `ResourceApplyAdam`，而稀疏路径却手写一堆 `assign`/`scatter_add`？
**参考答案**：稠密更新要触及变量的全部元素，融合 kernel 能把 m、v、var 三次读改写合并成一次遍历，性能最高；稀疏更新只改 `indices` 指定的若干行，没有现成的融合稀疏 kernel，只好用 `scatter_add` 只写 touched 行，避免全量改写。

**练习 2**：把 `_prepare_local` 里 `local_step = self.iterations + 1` 改成 `self.iterations`（不 +1）会怎样？
**参考答案**：偏差校正的 \(β^t\) 会整体「晚一步」，首步因 \(t=0\) 使 \(β^t=1\)、有效学习率为 0，导致第一步不更新。`+1` 是为了在「尚未自增的 iterations」上得到「本次更新是第 t 步」的正确编号。

---

### 4.4 V1 Optimizer 对照：processor、_apply_dense 与 _finish

#### 4.4.1 概念说明

`tf.compat.v1.train.Optimizer` 是 TF1 时代的优化器基类，至今仍随 `compat.v1` 提供。它的核心思想与 V2 一致（两段式 + slot），但有几处设计差异，理解这些差异有助于阅读老代码、迁移老模型，也能反衬出 V2 改进了什么。本节是最小模块 `python.training.optimizer` 的主线。

V1 最显著的两个特点：

1. **变量处理器（processor）抽象**：V1 时代同时存在「老式 ref variable」和「新式 resource variable」，还有普通 Tensor。V1 用一个 `_OptimizableVariable` 接口和若干 processor 子类来屏蔽这些差异，统一成「`target()` 取求导目标 + `update_op()` 产更新 op」。
2. **梯度门控（gate_gradients）与 `_finish` 收尾**：V1 显式支持 `GATE_NONE/OP/GRAPH` 三档并行度，并在所有 per-variable update op 产出后用一个 `_finish(update_ops, name_scope)` 收尾（默认 `control_flow_ops.group`）。

#### 4.4.2 核心流程

V1 的 `minimize` 同样是 `compute_gradients` + `apply_gradients`，但 `apply_gradients` 的内部结构与 V2 不同：

```
apply_gradients(grads_and_vars, global_step=None)
  ├─ 对每个 (g, v): g = convert_to_tensor_or_indexed_slices(g)
  │                p = _get_processor(v)            # 选 processor
  ├─ _create_slots(var_list)                        # 子类 override
  ├─ _prepare()                                     # 子类物化 tensor（如 lr_t）
  ├─ for (g, v, p): update_ops.append(p.update_op(self, g))
  │      └─ processor 内部按 g 类型调 _apply_dense / _resource_apply_dense / _apply_sparse...
  ├─ apply_updates = _finish(update_ops, name)      # 默认 group
  └─ if global_step: assign_add(global_step, 1)     # 注意：外部传入！
```

注意两个与 V2 的关键区别：①步数计数器不是优化器自带的，而是调用方传进来的 `global_step` 变量；②「调用哪个 apply 方法」由 processor 决定，而不是 V2 那样在 `_distributed_apply` 里按 `IndexedSlices` 直接判。

#### 4.4.3 源码精读

processor 接口与四种实现：

[optimizer.py:93-L104](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/training/optimizer.py#L93-L104) —— `_OptimizableVariable` 抽象基类，定义 `target()` 与 `update_op(optimizer, g)` 两个抽象方法。

四种 processor 分别对应四种「可优化对象」：

[optimizer.py:107-L201](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/training/optimizer.py#L107-L201) —— `_RefVariableProcessor`（老式 ref 变量，`update_op` 里按稠密/稀疏调 `_apply_dense`/`_apply_sparse_duplicate_indices`）、`_DenseReadResourceVariableProcessor`、`_DenseResourceVariableProcessor`（新式 resource 变量，调 `_resource_apply_dense`/`_resource_apply_sparse_duplicate_indices`）、`_TensorProcessor`（普通 Tensor，更新时直接抛 `NotImplementedError`）。每个 processor 的 `update_op` 还统一处理 `var.constraint` 投影。

`_get_processor` 按运行模式与变量类型选型：

[optimizer.py:222-L238](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/training/optimizer.py#L222-L238) —— eager 下 resource 变量走 `_DenseResourceVariableProcessor`；图模式下按 `op.type == "VarHandleOp"`、`isinstance Variable` 等判别。

V1 基类与 gate 常量：

[optimizer.py:242-L246](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/training/optimizer.py#L242-L246) —— `class Optimizer(trackable.Trackable)`；[optimizer.py:405-L408](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/training/optimizer.py#L405-L408) 定义 `GATE_NONE/GATE_OP/GATE_GRAPH` 三档。

`compute_gradients` 的双路径：

[optimizer.py:583-L644](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/training/optimizer.py#L583-L644) —— callable loss 走 `GradientTape`（兼容 TF2）；Tensor loss 走经典图模式 `gradients.gradients(...)`，并用 `gate_gradients` 控制并行度（`GATE_GRAPH` 时用 `control_flow_ops.tuple` 强制全算完再用）。

`apply_gradients` 的骨架：

[optimizer.py:739-L783](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/training/optimizer.py#L739-L783) —— `_create_slots` → `_prepare` → 循环 `processor.update_op(self, grad)` 攒 `update_ops` → `_finish(update_ops, name)` 收尾；若传了 `global_step` 则在其上 `assign_add(1)`。

`_finish` 默认实现就是 group：

[optimizer.py:1214-L1229](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/training/optimizer.py#L1214-L1229) —— `return control_flow_ops.group(*update_ops, name=name_scope)`。

V1 子类要实现的四个钩子（基类默认全抛 `NotImplementedError` 或空实现）：

[optimizer.py:1072-L1157](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/training/optimizer.py#L1072-L1157) —— `_create_slots`（空）、`_prepare`（空）、`_apply_dense`/`_resource_apply_dense`/`_apply_sparse`/`_resource_apply_sparse`（全抛 `NotImplementedError`）。

最简单的 V1 子类 `GradientDescentOptimizer`：

[gradient_descent.py:52-L82](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/training/gradient_descent.py#L52-L82) —— `_apply_dense` 直接调融合 op `gen_training_ops.apply_gradient_descent(var, lr, grad)`；`_resource_apply_dense` 对应 resource 版本；`_prepare` 把可能 callable 的学习率物化成 `_learning_rate_tensor`。这就是 \(\theta \leftarrow \theta - \eta g\) 的全部实现。

#### 4.4.4 V1 与 V2 的关键差异一览

| 维度 | V1 `train.Optimizer` | V2 `OptimizerV2`（Keras） |
| --- | --- | --- |
| 梯度计算 | `compute_gradients`（图模式用 `gradients.gradients`，callable 用 Tape） | 推荐用户自己用 `GradientTape`；`minimize` 内部 `_compute_gradients` 只做转发 |
| 变量抽象 | processor 机制（4 种 processor 屏蔽 ref/resource/Tensor） | 直接面向 ResourceVariable，无 processor 层 |
| 更新方法 | `_apply_dense`/`_resource_apply_dense`/`_apply_sparse`（多套） | `_resource_apply_dense`/`_resource_apply_sparse`（仅 resource） |
| 步数计数 | 外部传入 `global_step` 变量 | 内置 `iterations` 变量，自动 `assign_add(1)` |
| 收尾 | `_finish(update_ops)`（默认 `group`），可 override | `_distributed_apply` 内 group + 自增 iterations |
| 超参数 | 各子类自行存放（如 `_learning_rate`） | 统一 `_set_hyper`/`_get_hyper`，自动可序列化 |
| 并行控制 | `gate_gradients` 三档 | 无（依赖图调度） |
| 状态变量 | `_create_non_slot_variable`（按 graph 键） | `add_weight`/`add_slot`（Trackable 友好） |

一句话总结：**V2 把 V1 里「散落各处」的状态（步数、超参、slot）统一收进 Trackable 体系，删掉了 processor 与 gate 这两套为「图模式 + ref variable」服务的复杂机制，转而全面拥抱 resource variable 与 eager/function。**

#### 4.4.5 代码实践

**目标**：用 V1 的 `GradientDescentOptimizer` 完成一次更新，对照源码看清 `_apply_dense` → 融合 op 的路径。

**操作步骤**（源码阅读型实践，需阅读而非运行）：

1. 打开 [gradient_descent.py:52-L57](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/training/gradient_descent.py#L52-L57)，确认 `_apply_dense` 的全部内容就是 `gen_training_ops.apply_gradient_descent(var, lr, grad)`。
2. 回到 [optimizer.py:744-L759](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/training/optimizer.py#L744-L759)，看清这个 `_apply_dense` 是如何被 `processor.update_op(self, grad)` 间接调用的。
3. 写出调用链：`apply_gradients` → `processor.update_op` → `_apply_dense` → `apply_gradient_descent`（C++ op）。

**预期结果**：你能用一句话讲清「V1 里一条梯度是如何从 `apply_gradients` 流到一个 C++ 内核的」，并指出 processor 这一层是为兼容 ref variable 而存在的「中间人」。

> 待本地验证：若你环境中仍有 `tf.compat.v1`，可用 `tf.compat.v1.train.GradientDescentOptimizer(0.1)` 实跑一次更新，观察变量变化；否则以上为纯阅读型实践。

#### 4.4.6 小练习与答案

**练习 1**：V1 的 `_finish` 方法存在的意义是什么？为什么 V2 没有它？
**参考答案**：`_finish` 给子类一个机会在「所有 per-variable update op 都产出后」再追加全局 op（例如某些优化器需要在所有变量更新后再修正某个全局量）。V2 把这套逻辑直接写进 `_distributed_apply`（group update_ops + 自增 iterations），不再单独暴露钩子，简化了子类契约。

**练习 2**：为什么 V1 需要 `gate_gradients` 而 V2 取消了它？
**参考答案**：`gate_gradients` 解决的是图模式下「一个 op 对多个输入求梯度，梯度间可能相互依赖」的竞态。V2 默认在 eager/function 语义下运行，TF 的执行器与 XLA 已经能正确处理依赖关系，这一档位失去意义，故被移除。

---

### 4.5 训练循环的衔接：从 Keras train_step 到 optimizer.minimize

#### 4.5.1 概念说明

有了前面的基础，现在把 Optimizer 放回它的「使用场景」——训练循环。本节是最小模块 `python.keras.optimizer_v2.optimizer_v2` 与 u5-l4 的衔接点。一条训练循环无论手写还是由 `Model.fit` 驱动，核心都是反复执行同一个四拍：

```
for batch in dataset:
    ① 前向：loss = model(batch)            # 在 GradientTape 内
    ② 反向：grads = tape.gradient(loss, vars)
    ③ 更新：optimizer.apply_gradients(zip(grads, vars))
    ④ 度量：metrics.update_state(...)
```

Keras 的 `Model.fit` 把这四拍封装进 `train_step`，并在外层用 `make_train_function` 包上 `tf.function`/`tf.distribute.Strategy`（承接 u5-l4）。理解 `train_step` 就理解了「Optimizer 在整个训练流水线里的位置」。

#### 4.5.2 核心流程

```
Model.fit(dataset)
  └─ make_train_function() 把 train_step 包成 tf.function（缓存，compile 时清空）
       └─ train_step(data):
            ├─ with GradientTape(): loss = compiled_loss(model(x), y)
            ├─ optimizer.minimize(loss, trainable_variables, tape=tape)
            │      └─ (内部) _compute_gradients → apply_gradients → _resource_apply_dense
            └─ compiled_metrics.update_state(...)
```

关键观察：`train_step` 把**已经打开的 tape** 通过 `tape=tape` 传给 `optimizer.minimize`。这正是因为 4.1 节提到的——`_compute_gradients` 要求「若 loss 是 Tensor，必须传 tape」。

#### 4.5.3 源码精读

`train_step` 的核心六行：

[training.py:799-L805](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/training.py#L799-L805) —— 在 `GradientTape` 内做前向 `self(x, training=True)` 并算 `compiled_loss`；然后一行 `self.optimizer.minimize(loss, self.trainable_variables, tape=tape)` 完成反向 + 更新；之后更新 metrics。这正是「前向→反向→更新→度量」四拍的落地。

回到 Optimizer 侧，`minimize` 收到 tape 后的路径：

[optimizer_v2.py:536-L568](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/optimizer_v2/optimizer_v2.py#L536-L568) —— 因为 loss 已是 Tensor 且传了 tape，`_compute_gradients` 不再新建 tape，直接在传入的 tape 上下文里调 `_get_gradients` → `tape.gradient`。

于是整条链路贯通：

```
Model.fit → train_step → optimizer.minimize(loss, vars, tape)
   → _compute_gradients (tape.gradient)
   → apply_gradients → _distributed_apply
   → _resource_apply_dense (Adam: ResourceApplyAdam)
   → 变量被更新 + iterations + 1
```

#### 4.5.4 代码实践

**目标**：用「裸的训练循环」复现 `Model.fit` 一个 batch 的行为，确认你理解了 `train_step` 的每一拍。

**操作步骤**：

```python
import tensorflow as tf

# 一个最小模型 + 一个 batch
model = tf.keras.layers.Dense(1, kernel_initializer='zeros')
x = tf.constant([[1.0], [2.0], [3.0]])
y = tf.constant([[2.0], [4.0], [6.0]])     # y = 2x，待学权重≈2
loss_fn = tf.keras.losses.MeanSquaredError()
opt = tf.keras.optimizers.Adam(learning_rate=0.1)

# 手写 train_step：前向 → 梯度 → 更新 → 度量
with tf.GradientTape() as tape:
    y_pred = model(x)                         # ① 前向（在 tape 内）
    loss = loss_fn(y, y_pred)                 #    loss
grads = tape.gradient(loss, model.trainable_variables)   # ② 反向
opt.apply_gradients(zip(grads, model.trainable_variables))  # ③ 更新
print("loss =", loss.numpy(), "  kernel =", model.kernel.numpy())
```

**需要观察的现象**：每跑一遍这段代码，`loss` 下降、`kernel` 朝 2.0 收敛、`opt.iterations` 自增。

**对照源码**：这段代码与 [training.py:799-L805](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/engine/training.py#L799-L805) 一一对应——只是把 `optimizer.minimize(...)` 拆成了显式的 `tape.gradient` + `apply_gradients` 两步。把它包进 `@tf.function` 后，你就得到了一个等价于 `make_train_function` 的训练函数。

#### 4.5.5 小练习与答案

**练习 1**：如果把 `model(x)` 放在 `GradientTape()` 上下文**之外**会发生什么？
**参考答案**：前向计算不被 tape 记录，`tape.gradient(loss, ...)` 会返回 `None`（或全 None），`apply_gradients` 因拿不到梯度而无法更新。这正是「前向必须在 tape 内」的根因（u5-l1）。

**练习 2**：为什么 `train_step` 要把**已打开的 tape** 传给 `optimizer.minimize`，而不是让 minimize 自己 new 一个？
**参考答案**：因为 loss 张量是在「这个 tape」里产生的，只有同一个 tape 才能对它求导。若 minimize 自建新 tape，那个 tape 里没有任何被记录的 op，求出来的梯度必为 None。

---

## 5. 综合实践

**任务**：实现一个最小化的「自定义优化器」——**SGD with momentum**，把它接到第 4.5 节的训练循环里跑通，从而把本讲所有知识点串起来。

要求：

1. 继承 `tf.keras.optimizers.Optimizer`（即 `OptimizerV2`）。
2. 在 `_create_slots` 里为每个变量建一个名为 `'momentum'` 的 slot（初值 0）。
3. 在 `_resource_apply_dense` 里实现带动量的更新：

   \[
   m_t = \mu \cdot m_{t-1} + g
   \]
   \[
   \theta_t = \theta_{t-1} - \eta \cdot m_t
   \]

4. 实现 `get_config` 以支持序列化（参考 [adam.py:243-L253](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/keras/optimizer_v2/adam.py#L243-L253)）。

**参考实现骨架**（示例代码，待本地验证）：

```python
class MomentumSGD(tf.keras.optimizers.Optimizer):
    def __init__(self, learning_rate=0.01, momentum=0.9, name='MomentumSGD', **kwargs):
        super().__init__(name, **kwargs)
        self._set_hyper('learning_rate', kwargs.get('lr', learning_rate))
        self._set_hyper('momentum', momentum)

    def _create_slots(self, var_list):
        for var in var_list:
            self.add_slot(var, 'momentum')          # 对应 4.2 的 add_slot

    def _resource_apply_dense(self, grad, var, apply_state=None):
        coef = (apply_state or {}).get((var.device, var.dtype.base_dtype)) \
               or self._fallback_apply_state(var.device, var.dtype.base_dtype)
        m = self.get_slot(var, 'momentum')
        lr = coef['lr_t']
        mu = self._get_hyper('momentum', var.dtype.base_dtype)
        m.assign(m * mu + grad)                      # m_t = mu*m + g
        var.assign_sub(lr * m)                       # theta -= lr * m_t
        return m                                     # 返回一个 op/tensor

    def _prepare_local(self, var_device, var_dtype, apply_state):
        super()._prepare_local(var_device, var_dtype, apply_state)
        apply_state[(var_device, var_dtype)]['lr_t'] = \
            array_ops.identity(self._decayed_lr(var_dtype))  # 复用基类学习率衰减

    def get_config(self):
        config = super().get_config()
        config.update({
            'learning_rate': self._serialize_hyperparameter('learning_rate'),
            'momentum': self._serialize_hyperparameter('momentum'),
        })
        return config
```

**验证**：把它接到第 4.5.4 节的训练循环（替换 `Adam`）跑若干步，确认 loss 单调下降、`opt.get_slot(var, 'momentum')` 非零。成功后你就同时用到了本讲的：两段式 API（4.1）、slot 状态（4.2）、`_resource_apply_dense` 的真正更新（4.3）、与训练循环的衔接（4.5）。

---

## 6. 本讲小结

- Optimizer 的对外接口是**两段式**的：`compute_gradients`（算梯度）+ `apply_gradients`（写回变量），`minimize` 只是把两者串起来；TF2 鼓励自己用 `GradientTape`，以便在两步之间加工梯度。
- `OptimizerV2` 维护**三类状态**：步数 `iterations`、`add_slot` 创建的 slot 变量、`_set_hyper` 登记的超参数；三者都被做成 `tf.Variable` 以便 checkpoint 保存。
- **真正的参数更新**发生在子类实现的 `_resource_apply_dense`/`_resource_apply_sparse` 里。以 Adam 为例，稠密路径调融合 C++ kernel `ResourceApplyAdam`，稀疏路径手写 `assign`+`scatter_add`，两者实现同一套 \(m_t/v_t/\theta_t\) 公式。
- 旧版 `tf.compat.v1.train.Optimizer` 用 **processor 抽象 + `_finish` + `gate_gradients` + 外部 `global_step`** 组织更新；V2 删掉了这些为图模式/ref variable 服务的机制，把状态统一进 Trackable 体系。
- 在训练流水线里，Keras `Model.train_step` 把已打开的 `GradientTape` 连同 loss/变量传给 `optimizer.minimize`，于是「前向→反向→更新→度量」四拍贯通。

---

## 7. 下一步学习建议

- **u6-l1（Device 与 DeviceFactory）**：本讲多次出现 `colocate_vars_with(var)`、`_distributed_apply`，建议接着学设备抽象与分布式更新，理解「slot 与主变量为何必须同设备」。
- **u6-l4（分布式策略 distribute）**：`_aggregate_gradients`、`experimental_aggregate_gradients`、`distribution.extended.update` 都是为多副本训练服务的，下一站应当系统学习 `tf.distribute.Strategy`。
- **u5-l1（自动微分）**：若你对 `tape.gradient` 的内部仍有疑问，回头看 GradientTape 的录制与反向重放机制。
- **源码延伸**：阅读 `tensorflow/python/keras/optimizer_v2/` 下的 `gradient_descent.py`、`rmsprop.py`，对照本讲方法画出各自的 `_resource_apply_dense`；并尝试 `learning_rate_schedule.py`，理解 `_decayed_lr` 如何消费 schedule。
