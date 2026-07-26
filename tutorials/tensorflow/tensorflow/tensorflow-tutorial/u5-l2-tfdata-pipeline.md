# tf.data 输入流水线

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说清 `tf.data.Dataset` 到底是什么——它不是一堆数据，而是一条**可链式组合的「数据变换描述」**。
2. 解释为什么 `dataset.map(...).batch(...).prefetch(...)` 这种写法能工作：每一步都返回一个**新的 Dataset**，而不是就地修改。
3. 读懂 `dataset_ops.py` 中 `DatasetV2` 的核心抽象（`_variant_tensor`、`element_spec`、`UnaryDataset`、`__iter__`），并能在源码里定位 `map/batch/prefetch` 的真实实现位置。
4. 理解 C++ 侧 `core/data/captured_function` 的职责：用户传给 `map` 的那个 Python 函数是怎么被「打包」成一个可在图里反复执行、还能闭包外部张量的单元。
5. 用 `tf.data` 动手搭一条 `map → batch → prefetch` 流水线，并对照源码讲清每一步发生了什么。

本讲衔接 u3-l4（`tf.function`/`ConcreteFunction`）：你会看到 tf.data 怎样把 `tf.function` 追踪出来的函数挂到自己的变换里，所以「Eager 与 Graph 的桥梁」在这里再次出场。

## 2. 前置知识

在进入源码前，先用一段大白话建立直觉。机器学习训练时，CPU 读数据、做预处理，GPU 算前向反向。如果 GPU 每算完一个 step 才去等 CPU 准备下一批数据，GPU 大量时间在「空转等待」。

`tf.data` 解决的就是「怎么把『读数据 → 预处理 → 拼批 → 喂给模型』组织成一条流水线，让 CPU 和 GPU **重叠工作**」。它有三个核心思想，请先记住：

- **流式（streaming）**：数据不必一次性装进内存，按需逐个产出。
- **变换即组合（transformation as composition）**：读数据是一个 Dataset，对它做 `map` 得到一个新 Dataset，再 `batch` 又得到一个新 Dataset……每个 Dataset 都是「上一个 Dataset + 一道加工工序」的封装。
- **预取（prefetch）**：在消费者处理当前元素时，后台并发地准备好下一个元素，用内存换吞吐。

你需要回顾的两个前置概念（来自前面讲义）：

- **`tf.function` / `ConcreteFunction`（u3-l4）**：`map(f)` 里的 `f` 会被追踪成一个 `ConcreteFunction`。本讲会看到这条追踪结果如何被「捕获」进 Dataset。
- **op 与生成代码 `gen_*_ops`（u4-l5）**：每个 Dataset 变换（如 `map`）最终都调用一个由代码生成的 C++ op（如 `gen_dataset_ops.map_dataset`），这与普通 op 的生成路径是同一套机制。

此外，tf.data 里常出现一个常量 `tf.data.AUTOTUNE`，表示「让运行时自己调参数」（例如预取缓冲大小、并行度），无需手填。后面会看到它在源码里就是 `dataset_ops.AUTOTUNE`。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `tensorflow/python/data/ops/dataset_ops.py` | tf.data 的 Python 核心，定义 `DatasetV2` 抽象基类与所有变换方法的入口 | `DatasetV2`、`_variant_tensor`、`element_spec`、`UnaryDataset`、`__iter__`，以及 `map/batch/prefetch` 三个方法的委托 |
| `tensorflow/python/data/ops/map_op.py` | `Dataset.map` 的真正实现 | `_MapDataset` 如何把 `map_func` 追踪成函数并调用 `gen_dataset_ops.map_dataset` |
| `tensorflow/python/data/ops/batch_op.py` | `Dataset.batch` 的真正实现 | `_BatchDataset` 如何推导批后的 `element_spec` 并调用 `batch_dataset_v2` |
| `tensorflow/python/data/ops/prefetch_op.py` | `Dataset.prefetch` 的真正实现 | `_PrefetchDataset` 如何异步预取 |
| `tensorflow/python/data/ops/structured_function.py` | 把任意 Python 函数包装成「结构化输入输出」的函数对象 | `StructuredFunctionWrapper`——`map_func` 经它变成带 `captured_inputs` 的函数 |
| `tensorflow/core/data/captured_function.h` / `.cc` | C++ 侧「捕获函数」抽象 | `CapturedFunction`（静态定义）与 `InstantiatedCapturedFunction`（运行时执行） |

一句话概括全局：**Python 侧负责「描述与组合」，C++ 侧负责「真正地读数据、跑函数」**。`_variant_tensor` 就是两者之间的接线点——每个 Dataset 都用一个 `DT_VARIANT` 张量代表自己在 C++ 运行时里的那一半。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 `python.data.ops.dataset_ops`：Dataset 抽象与链式变换模型**——讲清「Dataset 是什么、为什么变换不就地修改」。
- **4.2 `python.data.ops.dataset_ops`（配合三个变换子文件）：`map / batch / prefetch` 的实现**——把最常用的三个变换拆到底。
- **4.3 `core.data.captured_function`：用户函数如何被「打包」与执行**——讲清 `map(f)` 里的 `f` 在 C++ 里是什么。

### 4.1 `python.data.ops.dataset_ops`：Dataset 抽象与链式变换模型

#### 4.1.1 概念说明

初学者最大的误解是：以为 `dataset = tf.data.Dataset.from_tensor_slices([1,2,3])` 之后，`dataset` 就「装着」1、2、3 这三个数。更准确的理解是：

> **Dataset 是一份「如何产出元素」的描述（recipe），而不是元素本身。**

这条描述由两类零件拼成：

- **源头（source）**：数据的最初来源（如内存里的张量、TFRecord 文件、生成器）。对应 `from_tensor_slices`、`TFRecordDataset` 等。
- **变换（transformation）**：对上游的每个（或每批）元素做加工，输出一个**新的** Dataset。对应 `map`、`batch`、`prefetch`、`shuffle`、`filter` 等。

源头的类描述在 `DatasetV2` 类的文档字符串里：

[tensorflow/python/data/ops/dataset_ops.py:143-150](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/data/ops/dataset_ops.py#L143-L150) —— 这段代码用三句话点明了 tf.data 的标准用法：

> 1. Create a source dataset from your input data.
> 2. Apply dataset transformations to preprocess the data.
> 3. Iterate over the dataset and process the elements.

第二行的 "Apply dataset transformations" 就是链式组合的精髓：**变换返回新对象，原对象不变**。这意味着 `d.map(f)` 不会动 `d`，而是造出一个 `d 的下游`；于是 `d.map(f).batch(b).prefetch(p)` 自然连成一条单向链。

链式模型还有个关键术语：**element（元素）** 与 **component（分量）**。一个元素可以是嵌套结构（tuple/dict），其中的每个叶子张量叫一个 component。`element_spec` 属性用一套 `tf.TypeSpec`（通常是 `TensorSpec`）精确描述「每个元素长什么样」。

#### 4.1.2 核心流程

用伪代码描述「构造一条流水线并取一个元素」的端到端流程：

```
# 1) 构造源头：产生一个 DT_VARIANT 张量代表 C++ 运行时里的 dataset 对象
src = TensorSliceDataset(tensors)      # 内部调用 gen_dataset_ops.tensor_slice_dataset

# 2) 链式变换：每一步包一层，返回新 Dataset，其 _variant_tensor = 新的 C++ dataset op
m   = _MapDataset(src, f)              # variant_tensor = gen_dataset_ops.map_dataset(src._variant_tensor, ...)
b   = _BatchDataset(m, batch_size)     # variant_tensor = gen_dataset_ops.batch_dataset_v2(m._variant_tensor, ...)
p   = _PrefetchDataset(b, buf)         # variant_tensor = gen_dataset_ops.prefetch_dataset(b._variant_tensor, ...)

# 3) 迭代：Python 的 for / next 经 OwnedIterator 驱动 C++ iterator，逐个 GetNext
for element in p:
    ...   # C++ 侧：prefetch 后台线程把 batch 准备好，主线程直接拿
```

注意链是「自顶向下构造、自底向上执行」：

- **构造期（Python）**：`p._variant_tensor` 这一棵 op 子树里，根是 `prefetch_dataset`，它的输入是 `batch_dataset_v2` 的输出，后者又以 `map_dataset` 的输出为输入……整条流水线被编码成一棵以 `_variant_tensor` 为根的 **op 子图**。
- **执行期（C++）**：迭代时从根（prefetch）的迭代器调 `GetNext`，prefetch 会去拉 batch，batch 去拉 map，map 去拉 source……数据自下而上流动。

这就是为什么 tf.data 天然能做「预取/并行」：整条链是一棵声明式的 op 子图，运行时可以按需在每一层插入后台线程。

#### 4.1.3 源码精读

**(1) `DatasetV2` 是抽象基类，强制子类提供 `element_spec`**

[tensorflow/python/data/ops/dataset_ops.py:137-142](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/data/ops/dataset_ops.py#L137-L142) —— 类声明。它同时继承了 `Iterable`（支持 `for`）、`Trackable`（可被 SavedModel 保存）、`CompositeTensor`（可作为张量在图里流动）。

[tensorflow/python/data/ops/dataset_ops.py:536-551](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/data/ops/dataset_ops.py#L536-L551) —— `element_spec` 是抽象属性，子类必须实现，否则直接 `raise NotImplementedError`。它返回「单个元素的类型规格」，是连接 Python 静态信息与运行时元素的桥梁。

**(2) 每个 Dataset 持有一个 `_variant_tensor`——通往 C++ 的钥匙**

[tensorflow/python/data/ops/dataset_ops.py:227-241](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/data/ops/dataset_ops.py#L227-L241) —— 构造函数。注意它要求子类**先造好 `variant_tensor` 再传进 `super().__init__`**，并存到 `self._variant_tensor_attr`。这个 `variant_tensor` 是一个 `DT_VARIANT` 张量，内部「装着」一个 C++ 的 `DatasetBase` 对象。

[tensorflow/python/data/ops/dataset_ops.py:265-271](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/data/ops/dataset_ops.py#L265-L271) —— `_variant_tensor` 属性只读，禁止重写。整个 tf.data 的 Python↔C++ 接线都靠它：上游 Dataset 把自己的 `_variant_tensor` 作为输入喂给「生成当前 Dataset 的那个 op」。

**(3) 两个骨架类：源头与「单输入变换」**

tf.data 把所有 Dataset 归成两类骨架，避免每个子类都重复写样板：

[tensorflow/python/data/ops/dataset_ops.py:4511-4515](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/data/ops/dataset_ops.py#L4511-L4515) —— `DatasetSource`：没有上游输入（`_inputs()` 返回空列表）。如 `from_tensor_slices`。

[tensorflow/python/data/ops/dataset_ops.py:4518-4526](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/data/ops/dataset_ops.py#L4518-L4526) —— `UnaryDataset`：恰好一个上游输入，存在 `self._input_dataset`。`map`、`batch`、`prefetch` 都继承它。这就是「链式变换」在源码里的物理形态：**每个变换类持有它的前驱 Dataset**。

[tensorflow/python/data/ops/dataset_ops.py:4529-4539](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/data/ops/dataset_ops.py#L4529-L4539) —— `UnaryUnchangedStructureDataset`：在 `UnaryDataset` 之上再加一条约定——输出元素的 `element_spec` 和输入完全一样（`prefetch` 就是这种，它不改结构，只改「何时产出」）。

> 推论：看一个变换类继承自 `UnaryDataset` 还是 `UnaryUnchangedStructureDataset`，就能立刻判断它**会不会改变元素的形状/类型**。`prefetch` 不改、`map`/`batch` 会改。

**(4) 怎么迭代？`__iter__` 通往 C++ 迭代器**

[tensorflow/python/data/ops/dataset_ops.py:488-504](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/data/ops/dataset_ops.py#L488-L504) —— `__iter__` 只在 eager 或 `tf.function` 内部可用，否则直接报错。它构造一个 `OwnedIterator`，后者在内部驱动 C++ 迭代器的 `GetNext`。也就是说，「构造链」是 Python 的活，「取数据」要落到 C++。

**(5) 一个公共参数包 `_common_args`**

[tensorflow/python/data/ops/dataset_ops.py:676-694](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/data/ops/dataset_ops.py#L676-L694) —— 几乎所有 `gen_dataset_ops.*` 都要 `metadata / output_shapes / output_types` 这组参数；这里用一个字典统一产出，子类直接 `**self._common_args` 展开即可。读源码时遇到 `**self._common_args` 就知道它是在补充这组元信息。

**(6) 源头示例：`from_tensor_slices`**

[tensorflow/python/data/ops/from_tensor_slices_op.py:49-54](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/data/ops/from_tensor_slices_op.py#L49-L54) —— `TensorSliceDataset.__init__` 末尾调用 `gen_dataset_ops.tensor_slice_dataset(...)` 造出 `_variant_tensor`，再 `super().__init__(variant_tensor)`。这正是「源头」的范式：把数据整理成张量列表，交给一个 C++ op 包装成 `DT_VARIANT`。

#### 4.1.4 代码实践

**实践目标**：验证「变换返回新对象、原对象不变」，并亲手看到链式结构。

**操作步骤**（待本地验证；这是源码阅读型 + 可运行型实践）：

```python
# 示例代码：可直接在装好 tf 的环境运行
import tensorflow as tf

src = tf.data.Dataset.from_tensor_slices([1, 2, 3, 4])   # 源头
m   = src.map(lambda x: x * 2)                            # 变换 1：得到新 Dataset

print("src 与 m 是否同一个对象：", src is m)               # 预期 False
print("src.element_spec =", src.element_spec)
print("m.element_spec   =", m.element_spec)               # 仍是 int32 标量，map 没改结构
```

**需要观察的现象**：

- `src is m` 为 `False`——证明 `map` 造了新对象，没动 `src`。
- 两个 `element_spec` 都是 `TensorSpec(shape=(), dtype=tf.int32)`——`map(lambda x: x*2)` 不改变元素结构。

**进一步（看 `_variant_tensor` 是 op）**：

```python
# 示例代码：窥探 _variant_tensor 背后的 op
print(m._variant_tensor.op.type)   # 预期类似 "MapDataset" / "MapDatasetV2"
print(m._variant_tensor.op.inputs[0].op.type)  # 上游 op，预期指向 TensorSliceDataset
```

**预期结果**：你能看到 `m._variant_tensor` 的 `op.type` 是一个 Map 类 op，且它的第一个输入连回 source。这就直观验证了 4.1.2 里「链 = op 子图」的结论。具体 op 名以本地版本为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `tf.data` 把每个变换设计成「返回新 Dataset」而不是「就地修改」？

参考答案：因为 Dataset 本质是「描述」，不可变的描述便于复用与组合——同一个 `src` 可以同时 `src.map(f1)` 和 `src.map(f2)` 得到两条不同的下游链；也让序列化（SavedModel）、图优化、回放都更可预测。若就地修改，则分支会被破坏。

**练习 2**：判断下列说法对错并说明理由：「`dataset.prefetch(2)` 会改变元素的数量或类型。」

参考答案：错。从源码看 `_PrefetchDataset` 继承 `UnaryUnchangedStructureDataset`（见 4.1.3），其 `element_spec` 直接返回 `self._input_dataset.element_spec`，结构与上游完全一致；`prefetch` 只改变「产出元素的时机」（后台提前准备），不改内容。

---

### 4.2 `map / batch / prefetch` 三大变换的实现

#### 4.2.1 概念说明

这三个是输入流水线最常用的变换，分别负责「预处理」「拼批」「重叠 CPU/GPU」。它们的共同点是：**`DatasetV2` 上的同名方法只是个薄壳，真正实现在独立的 `*_op.py` 文件里，最终都落到一个 `gen_dataset_ops.*` 的 C++ op。**

| 变换 | 干什么 | 是否改 `element_spec` | 对应类 | 对应 C++ op |
| --- | --- | --- | --- | --- |
| `map(f)` | 对每个元素应用 `f` | 是（由 `f` 的返回结构决定） | `_MapDataset` | `gen_dataset_ops.map_dataset` |
| `batch(n)` | 把连续 `n` 个元素拼成一个批 | 是（每个分量多一维） | `_BatchDataset` | `gen_dataset_ops.batch_dataset_v2` |
| `prefetch(k)` | 后台提前缓存 `k` 个元素 | 否 | `_PrefetchDataset` | `gen_dataset_ops.prefetch_dataset` |

注意 `batch` 后元素结构的变化：原来标量 `int32` 会变成 `int32` 向量（多了一个最外维）。批数（cardinality）也变了：设上游有 N 个元素、批大小为 B，则批数为：drop_remainder=False 时取向上取整 ceil(N/B)，drop_remainder=True 时取向下取整 floor(N/B)。

这也是 `batch` 文档反复强调的一点：当末批不足 B 时，最后一批的形状是动态的；若下游（如 XLA）要求静态已知形状，应设 `drop_remainder=True`。

#### 4.2.2 核心流程

三个变换在 Python 侧的流程几乎一致，可总结成一个模板：

```
def 某变换(self, 参数):
    return 某变换_op._某变换(self, 参数)      # 委托到独立文件

# 独立文件 _某变换_op.py:
def _某变换(input_dataset, 参数):
    class _某Dataset(UnaryDataset):
        def __init__(self, ...):
            self._input_dataset = input_dataset
            # 推导输出 element_spec（batch 要加一维，map 取 f 的输出结构）
            variant_tensor = gen_dataset_ops.某_dataset(
                input_dataset._variant_tensor,   # 把上游接进来
                ...,
                **self._common_args)
            super().__init__(input_dataset, variant_tensor)
    return _某Dataset(input_dataset, 参数)
```

关键设计意图有两点：

1. **委托到独立文件是为了打破循环依赖**。`dataset_ops.py` 与 `map_op.py` 互相引用，所以 `Dataset.map` 里采用**延迟导入**（在函数体内 `from ... import map_op`）。
2. **`input_dataset._variant_tensor` 作为 op 的输入**，这就是把「上游 Dataset」接到「下游 Dataset」的物理操作。

#### 4.2.3 源码精读

**(1) `prefetch`：最简单的变换，适合入门**

[tensorflow/python/data/ops/dataset_ops.py:1269-1270](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/data/ops/dataset_ops.py#L1269-L1270) —— `Dataset.prefetch` 方法体只有一行，委托给 `prefetch_op._prefetch`。

[tensorflow/python/data/ops/prefetch_op.py:24-28](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/data/ops/prefetch_op.py#L24-L28) —— `_prefetch`：注意当 `debug_mode.DEBUG_MODE` 开启时直接原样返回输入（调试模式下禁用并发，便于复现问题）；否则构造 `_PrefetchDataset`。

[tensorflow/python/data/ops/prefetch_op.py:31-52](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/data/ops/prefetch_op.py#L31-L52) —— `_PrefetchDataset.__init__`：把 `buffer_size` 转成 `int64` 张量（`None` 时取 `AUTOTUNE`），然后调用 `gen_dataset_ops.prefetch_dataset`，把 `input_dataset._variant_tensor` 作为输入接进来。注释 `We colocate the prefetch dataset with its input` 说明它显式把 prefetch 与上游放在同一设备，因为图模式下这种同设备放置不会自动发生。

**(2) `batch`：变换会改 `element_spec`**

[tensorflow/python/data/ops/dataset_ops.py:1915-1917](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/data/ops/dataset_ops.py#L1915-L1917) —— `Dataset.batch` 委托给 `batch_op._batch`（同样是延迟导入，注释里点明了循环依赖）。

[tensorflow/python/data/ops/batch_op.py:50-82](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/data/ops/batch_op.py#L50-L82) —— `_BatchDataset.__init__`。两处重点：

- [L68-74](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/data/ops/batch_op.py#L68-L74) —— 推导输出结构：对每个分量的 `component_spec` 调 `_batch(...)`。当 `drop_remainder` 为常量真时传入已知 `batch_size`，否则传 `None`——这正是「末批可能不足」导致最外维未知的根因。
- [L77-81](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/data/ops/batch_op.py#L77-L81) —— 调 `gen_dataset_ops.batch_dataset_v2`，同样把 `input_dataset._variant_tensor` 接为输入。

**(3) `map`：变换里夹着一个用户函数**

[tensorflow/python/data/ops/dataset_ops.py:2340-2342](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/data/ops/dataset_ops.py#L2340-L2342) —— `Dataset.map` 委托给 `map_op._map_v2`（TF2 路径；另有 `_map_v1` 用于旧 API）。

[tensorflow/python/data/ops/map_op.py:141-173](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/data/ops/map_op.py#L141-L173) —— `_MapDataset.__init__`。三个要点：

- [L157-161](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/data/ops/map_op.py#L157-L161) —— 把 `map_func` 包成 `StructuredFunctionWrapper`。这一步会把 Python 函数追踪成一个可执行函数对象（详见 4.3），并由此得到 `self._map_func.function` 和它的 `captured_inputs`（被函数闭包的外部张量）。
- [L164-172](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/data/ops/map_op.py#L164-L172) —— 调 `gen_dataset_ops.map_dataset`，传入三样：上游 `_variant_tensor`、`captured_inputs`、以及 `f=self._map_func.function`（函数本身）。注意第 166 行的 `self._map_func.function.captured_inputs`——这正是「闭包外部张量」被打包进 op 的地方。
- [L178-180](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/data/ops/map_op.py#L178-L180) —— `element_spec` 直接取 `self._map_func.output_structure`，即由用户函数的返回结构决定输出结构。这与 `prefetch`（原样透传上游结构）形成对照。

> 一个直觉总结：`prefetch` 只搬运不改结构、`batch` 机械地加一维、`map` 的结构完全由用户函数说了算。三者在源码里的差异，本质上就是「`element_spec` 怎么算」和「调哪个 `gen_dataset_ops.*`」。

#### 4.2.4 代码实践

**实践目标**：搭一条 `map → batch → prefetch` 流水线，对照源码确认每一步都产生新 Dataset、且 `element_spec` 按预期变化。

**操作步骤**（可运行，预期结果来自各方法的官方文档示例）：

```python
# 示例代码
import tensorflow as tf

ds = (tf.data.Dataset
      .range(8)                                   # [0,1,2,3,4,5,6,7]
      .map(lambda x: x * 10)                       # [0,10,...,70]
      .batch(3)                                    # [ [0,10,20], [30,40,50], [60,70] ]
      .prefetch(2))                                # 后台预取 2 个批

# 逐个元素类型规格的演变
src   = tf.data.Dataset.range(8)
step1 = src.map(lambda x: x * 10)
step2 = step1.batch(3)
print("map 后 :", step1.element_spec)   # TensorSpec(shape=(),  dtype=int32)
print("batch后:", step2.element_spec)   # TensorSpec(shape=(None,), dtype=int32)
                                       #  ^^^ 多了一维，且最外维未知(drop_remainder=False)

for batch in ds:
    print(batch.numpy())
```

**需要观察的现象**：

1. `map` 后 `element_spec` 仍是标量 `int32`（`x*10` 不改结构）。
2. `batch` 后 `element_spec` 变成 `shape=(None,)`——`None` 正对应 batch_op.py 第 73 行 `_batch(None)`：因为 `drop_remainder` 默认 False，末批可能不足 3。
3. 末批是 `[60, 70]`，只有 2 个元素（ceil(8/3) = 3 批，最后一批大小为 2）。
4. 若加 `drop_remainder=True`，则只剩 2 批（floor(8/3) = 2），且 `element_spec` 的最外维变成静态的 `3`。

**对照源码**：

- 看到 `step2.element_spec` 的 `None` 时，回头读 [batch_op.py:68-74](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/data/ops/batch_op.py#L68-L74)，就能解释它从哪来。
- 把 `drop_remainder` 改成 `True` 重跑，应看到最外维从 `None` 变 `3`，对应第 69 行 `_batch(constant_batch_size)` 分支。

#### 4.2.5 小练习与答案

**练习 1**：把上面流水线里的 `prefetch(2)` 换成 `prefetch(tf.data.AUTOTUNE)`，运行行为会有什么区别？从 [prefetch_op.py:37-40](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/data/ops/prefetch_op.py#L37-L40) 找依据。

参考答案：`buffer_size=None` 时取 `dataset_ops.AUTOTUNE`，并在 [L50](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/data/ops/prefetch_op.py#L50) 把 `legacy_autotune=(buffer_size == AUTOTUNE)` 设为真，表示缓冲区大小由运行时根据吞吐动态调整，而非固定 2。输出内容不变，但预取量随负载自适应。

**练习 2**：为什么 `Dataset.map` / `Dataset.batch` 都用「函数内延迟导入 `*_op`」而不是文件顶部导入？

参考答案：注释里写明是循环依赖（`dataset_ops → map_op/batch_op → dataset_ops`）。`dataset_ops.py` 体量很大且被这些变换文件依赖，反过来这些变换文件又实现了 `Dataset` 的方法，所以只能在调用点才导入，打破加载顺序上的环。

**练习 3**：若 `map` 的函数 `f` 返回一个形状为 `(3,)` 的向量，那么 `map(f).batch(4)` 之后单个元素的 `element_spec` 是什么？

参考答案：`map` 后元素是 `TensorSpec(shape=(3,), ...)`（由 `f` 的返回结构决定，见 map_op.py 的 `output_structure`）；`batch(4)` 在最外面再套一维，由于 `drop_remainder=False` 该维未知，故为 `TensorSpec(shape=(None, 3), ...)`。

---

### 4.3 `core.data.captured_function`：用户函数如何被「打包」与执行

#### 4.3.1 概念说明

`map(f)` 里的 `f` 是个普通 Python 函数。但 tf.data 的执行在 C++ 里（一条由 op 组成的流水线），C++ 没法直接「调用 Python 函数」。所以必须把 `f` 做两件事：

1. **追踪成图函数**：在 Python 侧用 `tf.function` 机制把 `f` 跑一遍，得到一个 `ConcreteFunction`（一个计算子图 + 签名）。这件事由 [structured_function.py:67-68](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/data/ops/structured_function.py#L67-L68) 的 `StructuredFunctionWrapper` 完成。
2. **打包「闭包变量」**：如果 `f` 引用了外部张量（例如 `y = tf.constant(7.); ds.map(lambda x: x + y)` 里的 `y`），这些张量必须和函数一起被「捕获」进 op，否则 C++ 执行时找不到它们。

第 2 点正是 `CapturedFunction` 要解决的核心问题。它把「函数 + 被捕获的外部张量」打包成一个整体。这就是它的名字 **captured（被捕获的）function（函数）** 的含义——源自编程语言里「闭包（closure）/ 捕获自由变量」的概念。

`CapturedFunction` 还区分两个阶段，理解这个区分是本模块的钥匙：

- **静态定义阶段 `CapturedFunction`**：函数名（`NameAttrList`）、它依赖的函数库、被捕获的张量列表——这些在构造期就固定不变。
- **运行时执行阶段 `InstantiatedCapturedFunction`**：要执行函数还需要「函数库运行时（`FunctionLibraryRuntime`）」和「函数句柄（`handle`）」，这些依赖具体的执行上下文，每次迭代才建立。

#### 4.3.2 核心流程

```
Python 侧:
  map(f) ──> StructuredFunctionWrapper(f) ──> ConcreteFunction + captured_inputs
                                              │
           gen_dataset_ops.map_dataset(input._variant_tensor,
                                       captured_inputs, f=ConcreteFunction)
                                    │  (这些被序列化进 op 的属性/额外输入)
                                    ▼
C++ 侧 (运行时):
  map_dataset 的 Iterator 初始化时:
    CapturedFunction::Create(ctx, metadata, "other_arguments", &out)
        └─ 从 op 的输入里把 captured 张量读出来 ─> captured_inputs_
    CapturedFunction::Instantiate(ctx, &instantiated)
        └─ 在 FunctionLibraryRuntime 里注册函数得到 f_handle_
        └─ 为每个 captured 张量确定它所在的设备
  每次 GetNext 取一个元素:
    instantiated->Run(ctx, {当前元素}, &rets)
        └─ OwnedArgsCallFrame(当前元素, &captured_inputs_)
        └─ lib_->RunSync(opts, f_handle, &frame)   # 真正执行用户函数
        └─ frame.ConsumeRetvals(rets)               # 取出返回值
```

一句话：**被捕获的张量作为「额外输入」与「当前元素」一起送进函数执行帧（CallFrame），再由 `FunctionLibraryRuntime` 执行那张被追踪出来的子图。**

#### 4.3.3 源码精读

**(1) Python 侧：`StructuredFunctionWrapper` 把函数追踪出来**

[tensorflow/python/data/ops/structured_function.py:265](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/data/ops/structured_function.py#L265) —— `self._function = fn_factory()`：`fn_factory` 来自 `trace_tf_function` 或 `trace_py_function`（[L255-263](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/data/ops/structured_function.py#L255-L263)），本质就是用 `tf.function` 把 `f` 追踪成一个 `ConcreteFunction`。得到的对象有 `.captured_inputs`（闭包的外部张量）和 `.add_to_graph(...)`（把函数注册进图）。这与 u3-l4 的 tracing 机制一脉相承。

**(2) C++ 侧：`CapturedFunction` 的静态定义**

[tensorflow/core/data/captured_function.h:150-152](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/data/captured_function.h#L150-L152) —— 类注释一语中的：`CapturedFunction` 封装「一个 TensorFlow 函数 + 它在用户程序里闭包捕获的参数」。

[tensorflow/core/data/captured_function.h:222-223](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/data/captured_function.h#L222-L223) —— 两个私有成员：`metadata_`（函数元数据，含函数名与函数库）与 `captured_inputs_`（被捕获的张量）。这两个字段就是「静态定义」的全部家当。

[tensorflow/core/data/captured_function.h:190-192](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/data/captured_function.h#L190-L192) —— `captured_inputs()` 访问器，把捕获的张量交出去。`map_op.py` 里传给 op 的 `captured_inputs` 最终就在这里被读回。

**(3) C++ 侧：构造——从 op 输入读出捕获张量**

[tensorflow/core/data/captured_function.cc:498-507](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/data/captured_function.cc#L498-L507) —— 第一个 `Create` 重载：用 `argument_name`（如 `"other_arguments"`）从 `OpKernelContext` 的输入列表里把捕获张量读出来，转交给第二个重载。

[tensorflow/core/data/captured_function.cc:510-517](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/data/captured_function.cc#L510-L517) —— 第二个 `Create` 重载：直接用调用方提供的 `captured_inputs` 构造对象。两个重载合起来表达了「捕获张量既可以现场从 op 输入拉取，也可以由上层显式传入」。

**(4) C++ 侧：实例化——拿到运行时句柄**

[tensorflow/core/data/captured_function.h:178-184](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/data/captured_function.h#L178-L184) —— `Instantiate` 声明：根据上下文产出一个 `InstantiatedCapturedFunction`。

[tensorflow/core/data/captured_function.h:236-239](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/data/captured_function.h#L236-L239) —— `InstantiatedCapturedFunction` 的注释把两阶段分工讲得很清楚：`CapturedFunction` 装的是「常量属性（函数名、捕获参数）」，而 `InstantiatedCapturedFunction` 装的是「运行时属性（`FunctionLibraryRuntime`、函数句柄）」，因为 Iterator 要在正常的 `OpKernel::Compute()` 上下文之外执行这些函数。

**(5) C++ 侧：执行——把元素与捕获张量一起送进函数**

[tensorflow/core/data/captured_function.h:245-246](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/data/captured_function.h#L245-L246) —— `Run` 声明：同步执行，`args` 是「当前元素」，`rets` 收返回值。

[tensorflow/core/data/captured_function.cc:834-861](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/data/captured_function.cc#L834-L861) —— `Run` 的核心。三行最关键：

- [L834-835](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/data/captured_function.cc#L834-L835) —— 构造 `OwnedArgsCallFrame`，它的两个数据源正是「当前元素 `args`」和「捕获张量 `captured_func_->captured_inputs()`」。这一步把两类输入拼成一帧。
- [L847 / L859](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/data/captured_function.cc#L847-L859) —— `lib_->RunSync(f_opts, f_handle_, &frame)`：由 `FunctionLibraryRuntime` 真正执行那张函数子图。
- [L861](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/data/captured_function.cc#L861) —— `frame.ConsumeRetvals(rets)`：把函数返回值取出来交给上游。

> 这就是「用户函数」在 C++ 里的完整生命：被捕获的张量在 `Create` 时从 op 输入读入并长期保存；每次取元素时，它们和当前元素一起进入 `CallFrame`，由 `FunctionLibraryRuntime` 执行。**捕获张量之所以能被反复使用，是因为它们被存在 `captured_inputs_` 里、每次 `Run` 都被重新拼进帧。**

#### 4.3.4 代码实践

**实践目标**：通过「闭包外部张量」这条线索，亲眼看到 `captured_inputs` 非空，从而验证 4.3 的机制。

**操作步骤**（可运行 + 源码阅读型）：

```python
# 示例代码
import tensorflow as tf

bias = tf.constant([100, 200, 300], dtype=tf.float32)   # 外部张量：会被函数闭包捕获

src = tf.data.Dataset.from_tensor_slices([[1., 2., 3.], [4., 5., 6.]])
md  = src.map(lambda x: x + bias)   # f 引用了外部的 bias —— bias 应当出现在 captured_inputs

# 窥探 map 内部追踪出的函数及其捕获输入
fn = md._map_func.function
print("函数对象:", type(fn).__name__)
print("captured_inputs 数量:", len(fn.captured_inputs))
for i, c in enumerate(fn.captured_inputs):
    print(f"  captured[{i}] =", c)
```

**需要观察的现象**：

- `captured_inputs` 不为空（预期至少 1 个），其中应能找到与 `bias`（`[100,200,300]`）对应的张量。这直接证明：函数闭包的外部张量被「捕获」进了 Dataset 的 map 变换。
- 把 `lambda x: x + bias` 改成 `lambda x: x + 1`（不引用外部张量），重跑后 `captured_inputs` 数量应减少（具体个数取决于追踪实现，以本地为准）。

**对照源码**：

- Python 侧的 `fn.captured_inputs` 来自 [structured_function.py:265](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/data/ops/structured_function.py#L265) 追踪出的函数对象；它被 [map_op.py:166](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/data/ops/map_op.py#L166) 传给 `gen_dataset_ops.map_dataset`。
- C++ 侧这批张量由 [captured_function.cc:498-507](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/data/captured_function.cc#L498-L507) 从 op 输入读出，存进 `captured_inputs_`，并在每次 [captured_function.cc:834-835](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/data/captured_function.cc#L834-L835) 的 `Run` 里与当前元素一起送进函数。

> 说明：`_map_func` 是内部属性，仅供学习窥探，不要在生产代码依赖它。若取不到属性，可改用「构造一个引用了外部张量的 map，迭代看结果正确」作为退阶实践（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：用一句话解释 `CapturedFunction` 与 `InstantiatedCapturedFunction` 的分工。

参考答案：`CapturedFunction` 保存「函数定义 + 被捕获的张量」这种**不随执行变化的静态属性**；`InstantiatedCapturedFunction` 在具体运行时上下文里绑定 `FunctionLibraryRuntime` 与函数句柄，负责**真正执行**函数。

**练习 2**：为什么「当前元素」和「捕获张量」要分开传入（`args` 与 `captured_inputs_`），而不是合并成一个列表？

参考答案：因为二者生命周期不同——捕获张量在整个迭代器存活期间不变（一次 `Create`/`Instantiate`），而当前元素每次 `GetNext` 都不同。分开存放后，每次 `Run` 只需把新的 `args` 与常驻的 `captured_inputs_` 拼进同一帧（见 [captured_function.cc:834](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/data/captured_function.cc#L834)），既避免重复拷贝，也让「函数闭包了哪些外部状态」清晰可查（`CheckExternalState`、序列化等都依赖这一区分）。

**练习 3**：如果 `map` 的函数没有引用任何外部张量，`captured_inputs_` 是否就一定为空？

参考答案：不一定。除了用户显式闭包的张量，`tf.function` 追踪时还可能因实现需要引入少量内部张量（例如来自外层图的状态、种子等）。但从用户视角，只要没有显式闭包外部数据，`captured_inputs` 通常很少甚至为空；确切数量以本地追踪结果为准（待本地验证）。

---

## 5. 综合实践

**任务**：搭一条贴近真实训练的输入流水线，并用本讲学到的源码知识解释每一步。

要求：

1. 用 `from_tensor_slices` 造一个含 12 个样本的「数据集」，每个样本是一个 `(feature, label)` 二元组（feature 是长度 4 的向量，label 是 0/1）。
2. 依次施加 `shuffle(buffer_size=5)` → `map(归一化/加噪声)` → `batch(4)` → `prefetch(tf.data.AUTOTUNE)`。
3. 在一个 `for` 循环里迭代，打印每个 batch 的形状。
4. 写一段文字说明：
   - 这条链里每一步分别对应哪个 `*_op.py` 文件、哪个 `gen_dataset_ops.*`；
   - `element_spec` 在 `map` 前后、`batch` 前后分别如何变化；
   - 为什么把 `prefetch` 放在最后（提示：它不改结构，且让「取下一批」与「处理当前批」重叠）。

参考骨架（示例代码，待本地验证）：

```python
import tensorflow as tf

features = tf.random.uniform((12, 4), minval=0, maxval=10)
labels   = tf.random.uniform((12,), minval=0, maxval=2, dtype=tf.int32)

ds = (tf.data.Dataset.from_tensor_slices((features, labels))
      .shuffle(buffer_size=5)
      .map(lambda x, y: (x / 10.0, y))     # 简单归一化
      .batch(4)
      .prefetch(tf.data.AUTOTUNE))

for fx, ly in ds.take(1):
    print("feature batch shape:", fx.shape)   # 预期 (4, 4)
    print("label   batch shape:", ly.shape)   # 预期 (4,)
```

验收要点：

- 你应能说出 `from_tensor_slices → tensor_slice_dataset`、`shuffle → shuffle_op/_shuffle → shuffle_dataset`、`map → map_op/_map_v2 → map_dataset`、`batch → batch_op/_batch → batch_dataset_v2`、`prefetch → prefetch_op/_prefetch → prefetch_dataset`。
- 你应能解释 `(feature, label)` 这种**嵌套结构元素**是如何被 `element_spec`（一个嵌套的 `TensorSpec` 元组）描述，并在 `batch` 后整体多出一维的。
- 你应能指出 `map` 的归一化函数里 `10.0` 这种 Python 字面量不进入 `captured_inputs`（它是常量、不是张量闭包），而若把某个 `tf.constant` 当权重引用进函数，它就会出现在 `captured_inputs` 里。

## 6. 本讲小结

- `tf.data.Dataset` 不是「数据容器」，而是「如何产出元素的描述」；变换返回**新** Dataset，从而自然支持链式组合。
- 每个 Dataset 持有一个只读的 `_variant_tensor`（`DT_VARIANT`），它是 Python 描述与 C++ 运行时之间的接线点；整条流水线在底层是一棵以 `_variant_tensor` 为根的 op 子图。
- `DatasetV2` 强制子类实现 `element_spec`；骨架类 `DatasetSource`（无上游）、`UnaryDataset`（单上游）、`UnaryUnchangedStructureDataset`（结构不变）把所有 Dataset 归成少数几类范式。
- `map/batch/prefetch` 在 `DatasetV2` 上只是薄壳，真正实现在独立的 `map_op/batch_op/prefetch_op.py`，最终都调用一个 `gen_dataset_ops.*`；延迟导入是为了打破循环依赖。
- `prefetch` 不改结构、`batch` 给每个分量加一维（末批可能未知 → 最外维 `None`，`drop_remainder=True` 可固化）、`map` 的结构由用户函数返回值决定。
- `map(f)` 的 `f` 经 `StructuredFunctionWrapper` 追踪成 `ConcreteFunction`；C++ 侧的 `CapturedFunction`（静态：函数 + 捕获张量）与 `InstantiatedCapturedFunction`（运行时：`FunctionLibraryRuntime` + 句柄）配合，在每次 `Run` 时把「当前元素」与「捕获张量」一起送进函数执行。

## 7. 下一步学习建议

- **顺着 `_variant_tensor` 往 C++ 深入**：阅读 `tensorflow/core/data/root_dataset.h/.cc` 与各变换的 C++ kernel（如 `tensorflow/core/kernels/data/` 下的 `map_op`、`batch_util`），看 C++ 侧的 `DatasetBase` 如何实现 `MakeIterator` 与 `GetNext`。这能把本讲的 Python↔C++ 接线补完整。
- **回到迭代器**：精读 `tensorflow/python/data/ops/iterator_ops.py` 中的 `OwnedIterator` 与 `Iterator`，理解 `__iter__` 之后 `next()` 是如何跨语言调用 C++ `IteratorBase::GetNext` 的（与 u3-l2 的 Session 执行链路是同类问题）。
- **函数执行链**：`CapturedFunction::Run` 调到的 `FunctionLibraryRuntime::RunSync` 正是 u3-l4 里 `ConcreteFunction` 在 C++ 侧被执行的入口，建议交叉阅读 `tensorflow/core/common_runtime/function.cc`。
- **性能与优化**：本讲只讲了「描述层」。tf.data 还有一套图优化（`tensorflow/core/data/rewrite_utils.*`、`optimization_options`）会在执行前重写这条链（如融合 map+batch）。学完本讲后再读这部分，能理解为什么 `tf.data` 能「自动」变快。
- 衔接后续单元：u5-l3（SavedModel）会涉及如何把带 `tf.data` 输入流水线的模型序列化，届时 `_variant_tensor` 与 `_as_serialized_graph` 会再次出场。
