# 分布式策略 distribute

## 1. 本讲目标

学完本讲，读者应该能够：

- 说出 `tf.distribute.Strategy` 作为一个**策略模式（Strategy Pattern）**抽象的设计意图——同一份用户代码可以挂在不同的「分布式后端」上跑。
- 理解 `MirroredStrategy`（单机多卡）与 `MultiWorkerMirroredStrategy`/`CollectiveAllReduceStrategy`（多机）的本质差异：变量如何放置、梯度如何聚合。
- 把握「replica context（副本上下文）」与「cross-replica context（跨副本上下文）」两条执行线索，知道何时进入哪一种、为什么 `strategy.run` 和 `strategy.reduce` 分别落在两边。
- 认识底层 **collective 通信原语**（`collective_reduce` / all-reduce），看懂 `group_key`、`instance_key`、`group_size` 这些参数如何把多张卡「编排」到一次同步通信里。

本讲承接 u6-l1（Device/DeviceFactory 的设备抽象）与 u5-l1（自动微分产出的梯度），回答一个问题：**当一份模型要在多张卡、甚至多台机器上同时训练时，TF 是怎样把变量和梯度「分发」下去又「收拢」回来的？**

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**数据并行 vs 模型并行。** 本讲的策略都属于**数据并行（data parallelism）**：每张卡上放一份**完整**的模型副本（叫一个 replica / 副本），但各自喂**不同的数据切片**。模型并行（把一层拆到多卡）TF 目前不支持，故本讲不涉及。

**同步训练（sync training）。** 每个 step 内，所有副本各自前向、反向算出梯度，然后把各副本的梯度**聚合（reduce）**成一个统一结果，再用它更新**所有**副本的变量。聚合最常用的算法是 **all-reduce**：每个参与者都拿到聚合后的最终值。本讲的 `MirroredStrategy` 和 `CollectiveAllReduceStrategy` 都是同步训练。

**两种「镜像」对象。** 一个变量在多卡上有多份拷贝，这份「多份拷贝的统一句柄」就是 `MirroredVariable`；一个张量（如各副本的 loss 或梯度）每份值不同，这份「多份不同值的统一句柄」就是 `PerReplica` 值。`strategy.run` 负责「拆开（unwrap）」分发，`strategy.reduce` 负责「合拢（merge）」回收。这两组动作是理解全篇的钥匙。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [distribute_lib.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/distribute_lib.py) | Strategy 抽象基类与策略模式骨架：`StrategyBase`/`Strategy`、`scope`/`run`/`reduce`、`ReplicaContext`、`StrategyExtendedV2`。 |
| [mirrored_strategy.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/mirrored_strategy.py) | `MirroredStrategy`（单机多卡）及其 `MirroredExtended`：设备探测、变量镜像创建、跨设备通信后端选择。 |
| [collective_all_reduce_strategy.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/collective_all_reduce_strategy.py) | `MultiWorkerMirroredStrategy`（即 `CollectiveAllReduceStrategy`）：多机同步训练，between-graph + collective。 |
| [cross_device_ops.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/cross_device_ops.py) | 跨设备通信的策略抽象 `CrossDeviceOps` 及其实现：`CollectiveAllReduce`、`ReductionToOneDevice`、`NcclAllReduce` 等。 |
| [collective_ops.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/collective_ops.py) | Python 层对 C++ collective op 的薄封装，最底层的 all-reduce 原语 `all_reduce`。 |
| [distribute_utils.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/distribute_utils.py) | `create_mirrored_variable` 与变量类映射表 `VARIABLE_CLASS_MAPPING`。 |
| [values.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/values.py) | `MirroredVariable` / `SyncOnReadVariable` 等分布式变量实现。 |

---

## 4. 核心概念与源码讲解

### 4.1 Strategy 抽象：策略模式与三种上下文

#### 4.1.1 概念说明

`tf.distribute.Strategy` 是一个**抽象基类**，它把「分布式训练的具体方式」封装成可替换的策略。用户的训练代码（建模型、写 step、跑 `fit`）只面对 `Strategy` 的统一接口，**不感知**底层是单机多卡、多机 all-reduce 还是参数服务器。这就是经典的策略模式：接口固定，实现可换。

`Strategy` 对外暴露三个最关键的入口：

- `scope()`：进入后，**变量创建被策略拦截**，决定变量放在哪些设备上。
- `run(fn, args)`：把 `fn` **在每个副本上各跑一遍**，自动把 `PerReplica` 输入拆给对应副本。
- `reduce(op, value, axis)`：把各副本的返回值**聚合**成一个张量。

理解这三者之前，必须先理解 TF distribute 的**三种上下文（context）**：

| 上下文 | 进入方式 | 在做什么 |
| --- | --- | --- |
| **cross-replica（跨副本）** | `with strategy.scope():` | 跨副本操作，如变量放置、`reduce`、`scope` 内读 `MirroredVariable`。 |
| **replica（副本）** | `strategy.run` 内部 | 在某一个副本上跑 `fn`，此刻 `get_replica_context()` 有效，可调 `all_reduce`。 |
| **update（更新）** | `strategy.extended.update` | 把一个更新函数施加到某个变量的所有副本拷贝上。 |

用户默认处于「单副本的默认 replica context」，`scope()` 把你切到 cross-replica，`run()` 再把你切进每个副本。

#### 4.1.2 核心流程

```
用户代码
  │
  ├── with strategy.scope():        # 进入 cross-replica context
  │       ├── 变量创建被 _scope 拦截 → _create_variable → 镜像/分片
  │       └── get_strategy() 返回该 strategy
  │
  ├── strategy.run(fn, args=...)    # 分发
  │       └── call_for_each_replica(fn) → 每个副本上 fn 收到「自己那份」args
  │             └── fn 内 get_replica_context().all_reduce(...) ← replica context
  │
  └── strategy.reduce(op, value)    # 回收：把各副本 value 聚合到当前设备
          └── extended._reduce → CrossDeviceOps.reduce
```

整条链路是「**scope 放变量 → run 拆输入 → reduce 合输出**」的三段式。

#### 4.1.3 源码精读

`Strategy` 类本身只是 `StrategyBase` 的薄壳，所有逻辑在 `StrategyBase` 里。先看它的类文档对三段式的概括：

- [distribute_lib.py:1088-1090](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/distribute_lib.py#L1088-L1090)：`StrategyBase` 定义——「A state & compute distribution policy on a list of devices」，点名 `scope` 控制变量放置、`run` 在副本上下文执行、`reduce` 聚合结果。

`scope()` 的 docstring 把「进入 scope 发生了什么」讲得最清楚：

- [distribute_lib.py:1242-1258](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/distribute_lib.py#L1242-L1258)：进入 scope 后 strategy 被装为「当前策略」，进入 cross-replica context，**变量创建被策略拦截**——同步策略（Mirrored/TPU/MultiWorkerMirrored）在每副本复制变量，`ParameterServerStrategy` 则放到参数服务器；这一步用的是自定义 `tf.variable_creator_scope`。

`scope()` 把活转给 `extended._scope`，后者真正的「拦截器」是一个 `creator_with_resource_vars`：

- [distribute_lib.py:2530](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/distribute_lib.py#L2530)：在 scope 内任何 `tf.Variable(...)` 都会走 `self._create_variable(next_creator, **kwargs)`，`next_creator` 是被拦截前原本要创建变量的函数——这正是 u2-l3 讲过的「变量创建器栈」机制，策略把一个新创建器压到栈顶。
- [distribute_lib.py:2554](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/distribute_lib.py#L2554)：拦截靠 `variable_scope.variable_creator_scope(creator_with_resource_vars)` 完成。

`run()` 的实现极简，核心就一行 `call_for_each_replica`：

- [distribute_lib.py:1668-1673](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/distribute_lib.py#L1668-L1673)：`with self.scope()` 后调 `self._extended.call_for_each_replica(fn, args, kwargs)`，这就是「在每个副本上各跑一遍 fn、并把 PerReplica 输入拆开」的入口。

`reduce()` 把跨副本聚合委托给 `extended._reduce`：

- [distribute_lib.py:1784-1785](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/distribute_lib.py#L1784-L1785)：当 `axis=None`（只跨副本聚合、不再沿 batch 维求和）时，直接 `self._extended._reduce(reduce_op, value)`。

进入副本上下文后，`ReplicaContext.all_reduce` 是梯度聚合的真正入口：

- [distribute_lib.py:3621-3624](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/distribute_lib.py#L3621-L3624)：`all_reduce` 在 merge_call 分支里把每个副本的 value 通过 `batch_all_reduce` → `strategy.extended.batch_reduce_to` 送到 cross-replica 一侧去执行真正的 collective。注释点明因 `capture_call_time_value` 须维护「有 merge_call / 无 merge_call」两条分支。
- [distribute_lib.py:3445-3478](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/distribute_lib.py#L3445-L3478)：`merge_call` 的语义——把各副本（线程）的参数「合并」后，在 cross-replica 上下文里跑 `merge_fn(strategy, *args)`，从而允许跨副本通信。

> 一句话总结 4.1：`Strategy` 是策略模式的接口，`scope/run/reduce` 三段式分别负责「放变量、拆输入、合输出」，而跨副本通信统一由 `merge_call` 把控制权从 replica context「上交」到 cross-replica context。

#### 4.1.4 代码实践

**实践目标**：用一个最小例子验证「三种上下文」的存在，并观察 `get_replica_context()` 在 `run` 内外取值不同。

```python
# 示例代码：可本地运行（需至少可见 1 个设备即可，CPU 也可）
import tensorflow as tf

strategy = tf.distribute.MirroredStrategy(["CPU:0"])  # 单设备方便观察

print("scope 外 in_cross_replica_context:",
      tf.distribute.in_cross_replica_context())        # 默认 False（默认 replica context）

with strategy.scope():
    print("scope 内 in_cross_replica_context:",
          tf.distribute.in_cross_replica_context())    # True
    v = tf.Variable(1.0)
    print("变量类型:", type(v).__name__)               # MirroredVariable

@tf.function
def step():
    ctx = tf.distribute.get_replica_context()
    print("run 内 get_replica_context() 是否为 None:", ctx is None)  # False
    return tf.identity(1.0)

per_replica = strategy.run(step)
total = strategy.reduce("SUM", per_replica, axis=None)
print("reduce 后:", total)
```

**操作步骤**：1) 安装 `tensorflow`（CPU 版即可）；2) 保存为 `ctx_demo.py` 并运行 `python ctx_demo.py`。

**需要观察的现象**：scope 外默认不在 cross-replica context；进入 `scope()` 后变为 True；变量 `v` 是 `MirroredVariable`；`run` 内 `get_replica_context()` 不为 None。

**预期结果**：输出三处布尔值依次约为 `False / True`，变量类型含 `MirroredVariable`，`run` 内 ctx 非 None。若无 GPU 也能复现（CPU 被视为单设备）。**若运行报 `MirroredStrategy` 找不到设备，改为 `tf.distribute.OneDeviceStrategy("CPU:0")` 等价观察。** 待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `strategy.run` 里需要 `merge_call` 机制？直接在 replica context 里调 collective 行不行？

**答案**：replica context 里你只能看到「自己这一份」副本的数据，collective 通信需要所有副本协同参与、且需要知道全体设备列表，这是跨副本信息。`merge_call` 把各副本线程的参数汇聚到一处、切到 cross-replica context，由策略统一编排一次集体通信；在 replica context 里单独发起会因各副本执行顺序不确定而 hang。MirroredStrategy 在某些条件下也提供了「不经 merge_call、直接在单副本内发射 collective」的快路径（见 4.3），但那是图模式下的优化，默认语义仍依赖 merge_call。

**练习 2**：`scope()` 内创建的变量和 `scope()` 外创建的变量有何不同？

**答案**：scope 内变量被策略的 `_create_variable` 拦截，按策略规则镜像到各设备（MirroredStrategy 下得到 `MirroredVariable`，多份拷贝保持同步）；scope 外就是普通 `tf.Variable`，只存在于一处，不参与分布式同步——这正是文档强调「模型、优化器、指标必须在 scope 内创建」的原因。

---

### 4.2 MirroredStrategy 与 MirroredVariable：单机多卡的变量镜像

#### 4.2.1 概念说明

`MirroredStrategy` 是最常用的入门策略：**单机、多设备（通常是多 GPU）、同步训练**。它的核心承诺是——每个变量在每张卡上都有一份拷贝，且这些拷贝**始终保持相同**（mirrored / 镜像）。读变量时读本地那一份即可（无通信），写变量（如优化器更新）时**对每份拷贝施加相同的更新**，从而保持一致。

这带来一个关键推论：因为各副本的变量相同、但喂的数据不同，**前向/反向各算各的**，只有**梯度**需要在更新前跨副本聚合一次。这就是数据并行同步训练的标准形态：

\[
g = \sum_{i=0}^{N-1} g_i,\qquad \theta \leftarrow \theta - \eta \cdot \tfrac{g}{N}
\]

其中 \(g_i\) 是第 \(i\) 个副本的梯度，\(N\) 是 `num_replicas_in_sync`。求和那一步就是 all-reduce。

#### 4.2.2 核心流程

MirroredStrategy 的生命周期分两阶段：

```
构造期 MirroredStrategy(devices)
  └── MirroredExtended.__init__
        ├── 探测 devices（未指定则 all_local_devices 取所有 GPU）
        ├── _initialize_strategy → _initialize_single_worker（规范化设备名）
        └── _make_collective_ops_with_fallbacks  # 选跨设备通信后端
              ├── 全 CPU            → RING collective
              ├── 全物理 GPU         → NCCL（CollectiveAllReduce）
              ├── 混合/TF1/虚拟 GPU  → ReductionToOneDevice（回退）
              └── 默认 group_size = len(devices)

使用期
  scope() 内 tf.Variable(...) → _create_variable
        └── 在每个 device 上各 new 一个变量（replica 0 用初值，其余 copy 自 replica 0）
        └── create_mirrored_variable 包装成 MirroredVariable（ON_WRITE）

  fit/train_step 内更新变量 → assign → on_write_assign → _update
        └── _update_cross_replica → extended.update → 对每份拷贝施加同一 update_fn
```

#### 4.2.3 源码精读

`MirroredStrategy` 的类文档直接点明它的定位：单机、多副本、变量镜像。

- [mirrored_strategy.py:199-212](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/mirrored_strategy.py#L199-L212)：`MirroredStrategy`——「Synchronous training across multiple replicas on one machine」；不指定设备时用所有可见 GPU，找不到 GPU 则用 CPU；且明确「多机请用 `MultiWorkerMirroredStrategy`」。

`__init__` 极薄，真正逻辑在 `MirroredExtended`：

- [mirrored_strategy.py:285-290](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/mirrored_strategy.py#L285-L290)：构造一个 `MirroredExtended` 交给父类 `Strategy.__init__(extended)`。注意 `Strategy` 持有一个 `extended` 对象——这是「主体 + 扩展」的双对象设计，`Strategy` 做对外门面，`extended` 藏实现细节。

设备探测与初始化：

- [mirrored_strategy.py:313-342](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/mirrored_strategy.py#L313-L342)：`MirroredExtended.__init__` 里，eager 模式只允许单机（多机 in-graph 在 eager 下不支持，会忽略 `TF_CONFIG`），设备缺省时取 `all_local_devices()`；随后设默认通信实现为 NCCL，再 `_initialize_strategy(devices)`。
- [mirrored_strategy.py:358-371](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/mirrored_strategy.py#L358-L371)：`_initialize_strategy` 规范化设备名、去重，调 `_initialize_single_worker`，然后 `_make_collective_ops_with_fallbacks` 选出 `_cross_device_ops`（未显式指定就用 collective）。
- [mirrored_strategy.py:373-406](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/mirrored_strategy.py#L373-L406)：跨设备通信后端的选择逻辑——**全 CPU 用 RING、全物理 GPU 用 NCCL（`CollectiveAllReduce`）、其余（混合/TF1/虚拟 GPU）回退到 `ReductionToOneDevice`**。这一段决定了你的 all-reduce 走哪条物理路径。

**变量镜像的核心**：`_create_variable` 在每个设备上各建一个变量。

- [mirrored_strategy.py:517-556](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/mirrored_strategy.py#L517-L556)：`_create_variable` 里的 `_real_mirrored_creator` 遍历 `self._devices`，在每台设备 `with ops.device(d)` 下调一次 `next_creator(**kwargs)` 造出一个本地变量，收进 `value_list`；随后交给 `create_mirrored_variable` 包装成 `MirroredVariable`。
- [mirrored_strategy.py:493-515](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/mirrored_strategy.py#L493-L515)：`_get_variable_creator_initial_value`——replica 0 用用户给的 `initial_value`，其余副本的初值是「读 replica 0 的值再 `identity`」一份，**保证所有副本初值相同**。

包装与类映射在 `distribute_utils` 里：

- [distribute_utils.py:319-367](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/distribute_utils.py#L319-L367)：`create_mirrored_variable` 先调 `real_mirrored_creator` 得到 `value_list`，再按 `synchronization`（默认 `ON_WRITE`）从映射表选出变量类（`MirroredVariable`）构造出最终的分布式变量。
- [distribute_utils.py:480-484](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/distribute_utils.py#L480-L484)：`VARIABLE_CLASS_MAPPING` 把 `ON_WRITE` → `MirroredVariable`、`ON_READ` → `SyncOnReadVariable`。

变量类本体：

- [values.py:1196-1197](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/values.py#L1196-L1197)：`MirroredVariable`——「Holds a map from replica to variables whose values are kept in sync」。

**保持同步的写路径**（这是「镜像」二字落地的关键）：

- [values.py:846-857](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/values.py#L846-L857)：`DistributedVariable.assign` 转给 `values_util.on_write_assign`。
- [values_util.py:133-140](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/values_util.py#L133-L140)：`on_write_assign` 把真正的 `var.assign` 包成 `update_fn`，交给 `var._update`。
- [values.py:1006-1027](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/values.py#L1006-L1027)：`DistributedVariable._update`——按当前上下文分流：replica context 调 `_update_replica`（只更新本副本那份），cross-replica context 调 `_update_cross_replica`（更新所有副本）。
- [values.py:968-982](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/values.py#L968-L982)：`_update_cross_replica` 转给 `strategy.extended.update(self, update_fn, ...)`。
- [mirrored_strategy.py:804-817](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/mirrored_strategy.py#L804-L817)：`MirroredExtended._update` 遍历 `var.values`（每份拷贝），在各自设备上施加**同一个** `fn`——这正是「相同更新施加到每份拷贝」的实现，保证镜像一致性。

副本数就是设备数：

- [mirrored_strategy.py:884-886](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/mirrored_strategy.py#L884-L886)：`_num_replicas_in_sync` 直接等于 `len(self._devices)`。

> 一句话总结 4.2：MirroredStrategy 把变量在每个设备上各复制一份（`_create_variable`），所有拷贝初值相同（`_get_variable_creator_initial_value`），更新时对每份施加同一 `fn`（`MirroredExtended._update`）从而保持镜像一致；副本数即设备数。

#### 4.2.4 代码实践

**实践目标**：用 `MirroredStrategy` 包裹一个 Keras 训练过程，对照源码确认「变量被镜像、梯度被 all-reduce」。

```python
# 示例代码：需至少 2 个 GPU 才能真正多卡；单 GPU/CPU 也能跑（退化为 1 副本）
import tensorflow as tf
import numpy as np

strategy = tf.distribute.MirroredStrategy()      # 自动用所有可见 GPU
print("num_replicas_in_sync =", strategy.num_replicas_in_sync)

with strategy.scope():                            # 关键：模型/优化器必须在 scope 内
    model = tf.keras.Sequential([
        tf.keras.layers.Dense(8, input_shape=(4,)),
        tf.keras.layers.Dense(1)])
    model.compile(optimizer="adam", loss="mse")   # scope 内编译捕获策略

x = np.random.random((64, 4)).astype("float32")
y = np.random.random((64, 1)).astype("float32")
model.fit(x, y, epochs=1, batch_size=8 * strategy.num_replicas_in_sync)

# 观察镜像变量
w = model.layers[0].kernel
print(type(w).__name__)                           # MirroredVariable
print("拷贝数:", len(w.values))                    # == num_replicas_in_sync
```

**操作步骤**：1) 在多 GPU 机器上 `pip install tensorflow`；2) 运行上述脚本。

**需要观察的现象**：`num_replicas_in_sync` 等于可见 GPU 数；模型权重的类型是 `MirroredVariable`，其 `.values` 长度等于副本数；训练正常收敛。

**对照源码要回答的问题**（本实践的核心）：
1. **变量如何被镜像**——`with strategy.scope()` 内的 `Dense` 层建权重时，被 [distribute_lib.py:2530](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/distribute_lib.py#L2530) 拦截，走 [mirrored_strategy.py:517](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/mirrored_strategy.py#L517) 的 `_create_variable`，在每张卡各 new 一个变量（[mirrored_strategy.py:528-551](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/mirrored_strategy.py#L528-L551)），再由 [distribute_utils.py:366-367](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/distribute_utils.py#L366-L367) 包装成 `MirroredVariable`。
2. **梯度如何被镜像**——`fit` 内部走 `train_step` → `GradientTape` 求梯度（u5-l1）→ 优化器更新。`MirroredVariable.assign` 经 [values_util.py:133](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/values_util.py#L133) 的 `on_write_assign` → `_update_cross_replica` → [mirrored_strategy.py:804](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/mirrored_strategy.py#L804) 的 `_update`，**对每张卡的拷贝施加同一个更新**；而更新前的梯度聚合由 4.3 的 all-reduce 完成（Keras 在 `train_step` 里自动 reduce loss / 梯度）。

**预期结果**：多卡时 `len(w.values) == GPU 数`，且训练若干步后 `w.values[0]` 与 `w.values[1]` 数值一致（镜像同步）。**单卡环境下 `len(w.values)==1`，无法验证「多份」，但流程一致。** 待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 MirroredStrategy 要求模型与优化器必须在 `strategy.scope()` 内创建？

**答案**：scope 外创建的是普通 `tf.Variable`，只存在于一个设备，无法被策略镜像；优化器内部也有变量（如 Adam 的一阶/二阶矩 slot，见 u5-l5），不在 scope 内创建就不会镜像，多卡更新会失配。scope 内创建才能被 `_create_variable` 拦截、镜像到所有设备。

**练习 2**：`MirroredVariable` 读值时需要跨设备通信吗？

**答案**：不需要。`MirroredVariable` 的语义是各拷贝相同，读时直接取本地副本那份（replica context）或取 primary（cross-replica context，见 [values.py:1235-1238](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/values.py#L1235-L1238) 的 `_get_cross_replica` 返回 identity）。通信只发生在「写」需要聚合梯度时（如 `SyncOnReadVariable` 在读时才 reduce，那是另一种变量）。

---

### 4.3 collective 通信原语与 all-reduce

#### 4.3.1 概念说明

变量镜像解决了「状态分布」，梯度聚合需要「通信原语」。TF 把跨设备通信抽象成 `CrossDeviceOps`，再由若干实现填充：`CollectiveAllReduce`（基于 collective op，NCCL/RING）、`NcclAllReduce`（旧 NCCL）、`ReductionToOneDevice`（把所有值拷到一张卡上 reduce 再广播，朴素回退）。它们最终都落到 C++ 的 **collective op**。

一次 collective 通信由三个「身份证」唯一确定：

- `group_key`：标识「参与方集合」（哪些设备组成一组）。
- `instance_key`：标识「这一次具体通信」（同一组内的第几次 reduce）。
- `group_size`：参与方总数。

每个参与设备必须用**相同**的这三个键发起同一次 collective，运行时据此把跨设备/跨进程的 op「配对」起来。如果少一个参与方，这次 collective 会**永久阻塞（hang）**——这是分布式调试最常见、也最反直觉的坑。

#### 4.3.2 核心流程

```
用户层 get_replica_context().all_reduce(op, value)
  └── （merge_call 分支）batch_reduce_to
        └── CrossDeviceOps.batch_reduce
              └── reduce_implementation / batch_reduce_implementation
                    └── （MirroredStrategy 无 merge_call 快路径）
                        MirroredExtended._replica_ctx_all_reduce
                          └── CrossDeviceOps._all_reduce
                                └── collective_ops.all_reduce  (Python 薄封装)
                                      └── gen_collective_ops.collective_reduce  (C++ op)
```

#### 4.3.3 源码精读

最底层的 Python 封装：

- [collective_ops.py:19-68](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/collective_ops.py#L19-L68)：`all_reduce(t, group_size, group_key, instance_key, merge_op, final_op, ...)`——参数含义见其 docstring：`merge_op`（如 `'Add'`，两两合并）、`final_op`（如 `'Id'` 或除法，最终施加的运算）、`communication_hint`（`auto`/`ring`/`nccl`）。它直接返回 `gen_collective_ops.collective_reduce(...)` 这个 C++ op。

> 注：all-reduce 的「mean」语义通常用 `merge_op='Add'` + `final_op='Id'` 先求和，再在调用方除以 `group_size` 得到均值；这就是 4.2 公式里 \(g/N\) 的来源。

跨设备通信的策略抽象：

- [cross_device_ops.py:252-275](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/cross_device_ops.py#L252-L275)：`CrossDeviceOps` 抽象基类，统一 `reduce`/`batch_reduce` 接口，子类实现 `reduce_implementation`。它就是「跨设备通信」这一可替换策略的接口。

基于 collective op 的实现：

- [cross_device_ops.py:1045-1050](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/cross_device_ops.py#L1045-L1050)：`CollectiveAllReduce`——「All-reduce cross device ops using collective ops」，是 MirroredStrategy 默认（GPU/NCCL）与 MultiWorkerMirroredStrategy 共用的通信后端。
- [cross_device_ops.py:1071-1108](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/cross_device_ops.py#L1071-L1108)：构造里保存 `_group_size`、`_options`、`_collective_keys`，为每个设备建一个 `CollectiveReplicaLauncher`；并用 `self._lock = threading.Lock()` 守护所有 collective 发射——注释解释了多线程 eager 下若两组 collective 交错会死锁，必须串行化。
- [cross_device_ops.py:1116-1130](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/cross_device_ops.py#L1116-L1130)：`_all_reduce` 按 `replica_id` 取对应 launcher 发射；当 NCCL 无法确定性排序（`_limited_nccl`）且仅单个 value 时回退到 RING。

MirroredStrategy 接入 collective 的两个入口：

- [mirrored_strategy.py:819-842](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/mirrored_strategy.py#L819-L842)：`_replica_ctx_all_reduce`——图模式下不经 merge_call，直接在单副本内调 `self._get_cross_device_ops(value)._all_reduce(...)`；eager/TF1/需 merge_call 时回退到父类实现（注释说明 eager 下副本顺序执行，直接发 collective 会 hang）。
- [mirrored_strategy.py:763-787](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/mirrored_strategy.py#L763-L787)：`_reduce_to`——若是 mirrored 值且 MEAN 直接返回（无需通信）；否则用 `_get_cross_device_ops(value).reduce(...)`，必要时回退 `ReductionToOneDevice`。

基类的 merge_call 实现（即默认 eager 路径）：

- [distribute_lib.py:2839-2872](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/distribute_lib.py#L2839-L2872)：`StrategyExtendedV2._replica_ctx_all_reduce`——通过 `replica_context.merge_call(merge_fn)` 把控制权上交，`merge_fn` 调 `batch_reduce_to`。

> 一句话总结 4.3：跨设备通信是一棵可替换的策略树（`CrossDeviceOps` → `CollectiveAllReduce`/`ReductionToOneDevice`），叶子是 C++ collective op，每次通信由 `group_key`/`instance_key`/`group_size` 三元组唯一确定，少一个参与方即 hang。

#### 4.3.4 代码实践

**实践目标**：用 `ReplicaContext.all_reduce` 直观验证「sum 聚合后每个副本都拿到总和」。

```python
# 示例代码：单机即可（用 2 个虚拟设备）
import tensorflow as tf

strategy = tf.distribute.MirroredStrategy(["CPU:0"])  # 退化 1 副本便于观察不 hang
@tf.function
def step():
    ctx = tf.distribute.get_replica_context()
    v = tf.constant(3.0)
    return ctx.all_reduce(tf.distribute.ReduceOp.SUM, v)  # 每副本都拿聚合结果

print(strategy.run(step))   # PerReplica{0: <... numpy=3.0>}
```

> 想看到真正的「多副本相加」，把 `["CPU:0"]` 换成两张 GPU：`["GPU:0","GPU:1"]`，则每副本输入 3.0，sum 后每副本都得到 6.0。

**操作步骤**：保存运行；若有双卡则改设备列表观察聚合。

**需要观察的现象**：单副本时结果为 3.0；双卡时两个副本结果均为 6.0（all-reduce 让每个参与方都拿到总和）。

**预期结果**：如上。若设备不可用则降级为单副本观察。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`merge_op='Add'`、`final_op='Id'` 表示什么？如何得到「均值 all-reduce」？

**答案**：`merge_op='Add'` 表示两两用加法合并（即求和），`final_op='Id'` 表示合并完成后不再做额外运算。因此这组参数实现的是「sum all-reduce」。要得到均值，需在求和后由调用方再除以 `group_size`（即副本数），这正是同步训练里梯度取平均的来源。

**练习 2**：为什么 `CollectiveAllReduce.__init__` 里要加一把全局 `self._lock`？

**答案**：eager 多线程（每设备一线程）发射 collective 时，若两组 collective 在不同设备上的入队顺序交错，会形成循环等待而死锁（源码注释给出了具体交错示例）。锁把所有 collective 发射串行化，避免交错，从而规避死锁。

---

### 4.4 CollectiveAllReduceStrategy：从单机走向多机

#### 4.4.1 概念说明

`MultiWorkerMirroredStrategy`（公开名）即 `CollectiveAllReduceStrategy`（类名），把 MirroredStrategy 的思想从「单机多卡」扩展到「**多机、每机多卡**」，仍是同步训练、变量镜像，但底层通信跨越多台机器。

两者最关键的架构差异是**图复制方式**：

| | MirroredStrategy | CollectiveAllReduceStrategy |
| --- | --- | --- |
| 拓扑 | 单进程、单机多设备 | 多进程、多机 |
| 图复制 | **in-graph**：一张图里含所有副本的 op | **between-graph**：每个 worker 进程各建一张只含本地副本的图 |
| 通信范围 | 进程内跨设备 | 跨进程、跨机器（collective over network） |
| 配置 | 设备列表 | `TF_CONFIG` 环境变量 / `ClusterResolver` |
| `group_size` | `len(devices)` | 全体 worker 的设备**总数** |

between-graph 的好处是每个进程的图小、可独立扩展到成百上千 worker；代价是必须保证**每个 worker 跑完全相同的程序**——因为 collective 要求所有参与方都到场，任何按 `task_id` 分支的代码都极易导致 hang（源码 docstring 反复强调这点）。

#### 4.4.2 核心流程

```
每个 worker 进程:
  读 TF_CONFIG → ClusterResolver → 得到 cluster_spec + 本机 task_type/task_id
  CollectiveAllReduceStrategy(cluster_resolver)
    └── CollectiveAllReduceExtended
          ├── _num_workers = cluster 中 worker 总数
          ├── _num_devices_per_worker = 本机 GPU 数（0 则 CPU）
          └── group_size = _num_workers * _num_devices_per_worker   # 跨全体机器
  之后用法与 MirroredStrategy 完全一致：scope() / run() / reduce() / fit()
```

#### 4.4.3 源码精读

类定义与定位：

- [collective_all_reduce_strategy.py:56-64](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/collective_all_reduce_strategy.py#L56-L64)：`CollectiveAllReduceStrategy`——「synchronous training across multiple workers」，复刻所有变量与计算到每个本地设备，**区别在于用分布式 collective（all-reduce）让多 worker 协同**；要求在每个 worker 上启动同一程序并正确配置 `TF_CONFIG`。

构造与 group_size：

- [collective_all_reduce_strategy.py:170-203](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/collective_all_reduce_strategy.py#L170-L203)：`__init__(cluster_resolver, communication_options)` 构造 `CollectiveAllReduceExtended`，并设置两个关键度量——`num_workers` 与 `num_replicas_per_worker`。`group_size`（全体设备数）= worker 数 × 每 worker 设备数，这就是 between-graph 下跨机 collective 的参与方总数。

MirroredStrategy 的多机判定工具（CARS 复用同一思路）：

- [mirrored_strategy.py:54-91](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/mirrored_strategy.py#L54-L91)：`_is_device_list_single_worker` 通过解析设备串里的 `(job, task, replica)` 三元组判断是单机还是多机——本地设备的 job 必须是 `localhost`，远程设备必须带 `task`。
- [mirrored_strategy.py:94-107](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/mirrored_strategy.py#L94-L107)：`_cluster_spec_to_device_list` 把 cluster 规格展开成设备列表，每个 `chief`/`worker` 的每个 GPU 对应一个设备字符串——多机训练的「全局设备清单」由此生成。

> 一句话总结 4.4：CARS 把 MirroredStrategy 的同步镜像思想搬到多机，用 between-graph（每进程一张本地图）+ 跨机 collective（`group_size` = 全体设备数）实现；用法与 MirroredStrategy 几乎相同，但必须每机跑相同程序并配 `TF_CONFIG`。

#### 4.4.4 代码实践

**实践目标**：阅读型实践——在不真的开多机的前提下，对照源码理清「MirroredStrategy 与 CARS 的差异点」。

**操作步骤**：

1. 打开 [collective_all_reduce_strategy.py:170-203](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/collective_all_reduce_strategy.py#L170-L203)，确认它和 MirroredStrategy 一样继承 `distribute_lib.Strategy`、同样有 `scope/run/reduce`。
2. 思考：同一份用户训练代码，为何换一个 strategy 子类就能从单机切到多机？答：因为用户只依赖 `Strategy` 抽象接口，差异全藏在 `extended`（`CollectiveAllReduceExtended` vs `MirroredExtended`）和 `_cross_device_ops` 的 `group_size` 里。
3. 写一段「假多机」配置（不启动），理解 `TF_CONFIG`：

```json
{
  "cluster": {"worker": ["localhost:12345", "localhost:23456"]},
  "task": {"type": "worker", "index": 0}
}
```

**需要观察的现象**（通过阅读）：CARS 的 `group_size` 由 worker 数 × 每 worker GPU 数决定，而非 MirroredStrategy 的「本机设备数」。

**预期结果**：能用一句话说出两者差异——「MirroredStrategy 是 in-graph 单机多卡、group_size=本机设备数；CARS 是 between-graph 多机、group_size=全体设备数，二者用户代码相同、仅 strategy 类与配置不同」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 CARS 的 docstring 强烈建议「不要根据 `task_type`/`task_id` 写分支逻辑」？

**答案**：collective 通信要求所有 worker 都参与同一次 reduce。如果不同 worker 因分支走了不同的 all-reduce 路径，某些 worker 不发起那次 collective，发起方就会永远等不到对端而 hang。保持每机程序相同，才能保证集体通信严格配对。

**练习 2**：MirroredStrategy 与 CARS 的 `num_replicas_in_sync` 分别由什么决定？

**答案**：MirroredStrategy 的 `_num_replicas_in_sync = len(self._devices)`（本机设备数，见 [mirrored_strategy.py:885](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/mirrored_strategy.py#L885)）；CARS 的副本数 = worker 数 × 每 worker 设备数（全体参与同步的设备总数，见 [collective_all_reduce_strategy.py:198-203](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/collective_all_reduce_strategy.py#L198-L203)）。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个「**追踪一次 MirroredStrategy 训练步的全链路**」任务。

任务背景：你已经用 4.2.4 的代码跑通了一个 MirroredStrategy + Keras 的训练。现在请对照源码，把**一个训练步**里发生的事按顺序对号入座：

1. **变量放置**（4.2）：`with strategy.scope()` 内 `Dense` 建权重 → 被 [distribute_lib.py:2530](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/distribute_lib.py#L2530) 拦截 → [mirrored_strategy.py:517](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/mirrored_strategy.py#L517) 在每卡各建一份 → 包装成 `MirroredVariable`。
2. **数据分发**（4.1）：`fit` 内部把全局 batch 按 `num_replicas_in_sync` 切片，经 `experimental_distribute_dataset` 产生 `PerReplica` 输入，`strategy.run`（[distribute_lib.py:1668](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/distribute_lib.py#L1668)）把每副本的那一份喂给 `train_step`。
3. **前向+反向**（u5-l1/u5-l5）：每副本各自 `GradientTape` 求出本地梯度 \(g_i\)，此时各副本梯度**不同**。
4. **梯度聚合**（4.3）：Keras 的 `train_step` 对 loss/梯度做 `all_reduce`（[distribute_lib.py:3546](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/distribute_lib.py#L3546)），经 [mirrored_strategy.py:819](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/mirrored_strategy.py#L819) → [cross_device_ops.py:1116](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/cross_device_ops.py#L1116) → [collective_ops.py:19](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/ops/collective_ops.py#L19) 的 `collective_reduce`，每副本拿到 \(\sum g_i\)。
5. **变量更新**（4.2）：优化器 `apply_gradients` → `MirroredVariable.assign` → [mirrored_strategy.py:804](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/mirrored_strategy.py#L804) 的 `_update` 对每张卡的拷贝施加**同一个**更新 → 各拷贝保持一致。

**交付物**：用一张流程图（文字版即可）画出这条链路，并在每个节点标注对应的源码文件与行号；最后写一句话解释「为什么读变量不需要通信、而更新变量需要通信」。

**预期结果**：能完整复述「scope 放变量 → run 拆数据 → 各副本前向反向 → all-reduce 聚合梯度 → _update 同步更新」五步，并指出读值取本地拷贝故无通信、写值需保证多拷贝一致故需（先聚合再）施加同一更新。

## 6. 本讲小结

- `tf.distribute.Strategy` 是**策略模式**抽象：`scope/run/reduce` 三段式分别负责「放变量、拆输入、合输出」，用户代码不感知具体后端。
- 三种上下文——cross-replica / replica / update——决定了你能调用哪些 API；`merge_call` 是从 replica context「上交」到 cross-replica context、从而发起跨副本通信的桥梁。
- `MirroredStrategy` 是单机多卡同步训练：`_create_variable` 在每设备各建一份变量，`MirroredExtended._update` 对每份施加同一更新以保持镜像，副本数 = 设备数。
- 跨设备通信是可替换的 `CrossDeviceOps` 策略树，叶子是 C++ `collective_reduce` op；每次通信由 `group_key`/`instance_key`/`group_size` 唯一确定，缺一个参与方即 hang。
- `CollectiveAllReduceStrategy`（`MultiWorkerMirroredStrategy`）把同一思想搬到多机：between-graph（每进程一张本地图）+ 跨机 collective，`group_size` = 全体设备总数，须每机跑相同程序并配 `TF_CONFIG`。

## 7. 下一步学习建议

- **TPUStrategy / ParameterServerStrategy**：同为 `Strategy` 子类，但变量放置与通信模型截然不同（TPU 用 XCCL/collective、PS 用变量分片+异步）。读完本讲后对照它们的 `extended` 实现，能加深对策略模式的理解。建议阅读 `tensorflow/python/distribute/tpu_strategy.py` 与 `parameter_server_strategy.py`。
- **Keras 与 distribute 的结合点**：本讲多次提到 `train_step` 自动 reduce，建议结合 u5-l4/u5-l5 阅读 `keras/engine/training.py` 中 `train_step` 如何把 `GradientTape` 与 `strategy.run` 串联。
- **XLA 与 collective**：当 `fn` 被 XLA 编译时，`_use_merge_call` 会改变 all-reduce 路径（见 [mirrored_strategy.py:351-356](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/distribute/mirrored_strategy.py#L351-L356)），可在学完 u7（XLA/MLIR）后回看此处。
- **调试 hang**：本讲反复强调 collective 缺参与方会 hang，建议阅读 `tf.distribute` 官方「Troubleshooting」指南，并尝试用 `TF_ENABLE_GPU_GARBAGE_COLLECTION`、verbose 日志定位参与方不匹配问题。
