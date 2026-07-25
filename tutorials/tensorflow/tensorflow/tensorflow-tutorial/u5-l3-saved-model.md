# SavedModel 序列化与加载

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚一个 SavedModel 在磁盘上到底由哪些文件组成，以及每个文件装了什么。
- 跟踪 `tf.saved_model.save` 从 Python 对象到磁盘文件的完整保存链路，理解「对象图」「MetaGraph」「checkpoint」三者的分工。
- 理解 `@tf.function` / `ConcreteFunction` 是如何被序列化成 protobuf（`SavedConcreteFunction` + `FunctionDef`），又如何在加载时被还原成可调用对象。
- 跟踪 `tf.saved_model.load` 如何把磁盘文件重建为一个可调用的 Python 对象，并把变量权重从 checkpoint 灌回。
- 能够自己保存并加载一个带签名的模型，并对照源码解释「图、变量、签名」分别落到了哪里。

本讲承接 [u3-l4](u3-l4-tf-function-and-concretefunction.md) 的 `tf.function` / `ConcreteFunction` 机制：正是那一讲建立的「函数即图」模型，才让函数可以被序列化进 SavedModel。

## 2. 前置知识

在进入源码前，先用一句话建立直觉：

> **SavedModel = 一棵对象树 + 一份计算图（含函数库） + 一份变量权重（checkpoint） + 一组对外签名（SignatureDef）。**

下面几个概念本讲会反复用到，先做最简解释：

- **Trackable / 可跟踪对象**：TensorFlow 里几乎所有「带状态、可保存」的东西（`tf.Variable`、`tf.Module`、`tf.keras.Layer`、`@tf.function` 装饰的方法）都继承自 `Trackable`。它们之间靠属性引用连成一棵**对象图**。`tf.train.Checkpoint` 保存的也是这棵树。
- **ConcreteFunction**：`@tf.function` 追踪出来的一份「定死输入签名、绑定一张 `tf.Graph`」的具体函数（见 u3-l4）。它是 SavedModel 里「函数」的最小搬运单元。
- **MetaGraphDef**：一个 protobuf 消息，容纳 `GraphDef`（计算图节点）、`FunctionDefLibrary`（函数库）、`SignatureDef`（签名）、`SaverDef`（保存器）、`asset_file_def`（外部资源）等。它是 TF1 / C++ / 服务端加载的「老接口」。
- **SavedObjectGraph（对象图）**：TF2 新增的另一块 protobuf，把 Python 对象树本身（每个节点的类型、子节点引用、函数引用）序列化进来，让 Python 侧能在加载时**重建出与保存时结构一致的对象**。
- **capture（捕获张量）**：一个 `ConcreteFunction` 在追踪时从外部「闭包」进来的张量（典型是变量）。序列化时必须把这些捕获张量也记下来，否则函数加载后无法运行。

一句话区分两块 protobuf 的职责：**MetaGraphDef 管「怎么算」（图 + 签名），SavedObjectGraph 管「Python 对象长什么样」（对象树 + 函数映射）**。这是理解全篇的钥匙。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tensorflow/python/saved_model/save.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/save.py) | 保存主逻辑：`tf.saved_model.save` 入口、对象图遍历、MetaGraph 生成、对象图序列化、checkpoint 写盘。 |
| [tensorflow/python/saved_model/load.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/load.py) | 加载主逻辑：`tf.saved_model.load` 入口、`Loader` 类负责重建对象图、重新接线函数、恢复 checkpoint。 |
| [tensorflow/python/saved_model/function_serialization.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/function_serialization.py) | 函数序列化：把 `ConcreteFunction` / `Function` 变成 protobuf。 |
| [tensorflow/python/saved_model/function_deserialization.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/function_deserialization.py) | 函数反序列化：把 protobuf 变回可调用的 `ConcreteFunction` / `RestoredFunction`。 |
| [tensorflow/python/saved_model/builder_impl.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/builder_impl.py) | TF1 风格的 `SavedModelBuilder`，本讲中主要充当 asset（外部资源）的搬运与重命名工具。 |
| [tensorflow/python/saved_model/path_helpers.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/path_helpers.py) | 拼 `variables/`、`assets/` 等子目录路径的小工具。 |
| [tensorflow/cc/saved_model/constants.h](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/cc/saved_model/constants.h) | 定义 `variables`、`assets`、`saved_model.pb` 等字面常量，是磁盘格式的「真相源」。 |

## 4. 核心概念与源码讲解

### 4.1 SavedModel 的磁盘结构与两大序列化目标

#### 4.1.1 概念说明

训练好的模型要在「另一个进程、另一台机器、另一种语言」里复现，就必须把三样东西同时搬走：

1. **计算的结构**：图节点、函数（哪些 op、怎么连、函数怎么调函数）。
2. **可学习的状态**：变量（权重、优化器动量等）的数值。
3. **对外的契约**：签名（输入叫什么、形状/dtype 是什么、输出叫什么），让服务端知道怎么喂输入、取输出。

TensorFlow 把这三样分别落到磁盘的不同位置：

```
my_saved_model/
├── assets/                         # 外部资源（词表等），可选
├── variables/
│   ├── variables.data-?????-of-?????   # 变量数值（TensorBundle 分片）
│   └── variables.index                 # 变量索引
└── saved_model.pb                   # 计算图 + 函数库 + 签名 + 对象图，全在这一个 protobuf
```

这段结构直接来自官方 README：

[README.md:36-46](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/README.md#L36-L46) 描述了 SavedModel 目录的标准结构。

而目录名、文件名这些「魔法字符串」的真相源在 C++ 头文件里：

[constants.h:21-66](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/cc/saved_model/constants.h#L21-L66) 定义了 `kSavedModelAssetsDirectory = "assets"`、`kSavedModelVariablesDirectory = "variables"`、`kSavedModelVariablesFilename = "variables"`、`kSavedModelFilenamePb = "saved_model.pb"`、`kSavedModelSchemaVersion = 1` 等。Python 侧通过 `pywrap_saved_model.constants` 引用同一份常量。

> **关键结论一**：变量数值**不在** `saved_model.pb` 里，而在 `variables/` 子目录（一个标准 checkpoint）。`saved_model.pb` 只装「结构信息」（图、函数、签名、对象图）。这与 [u5-l2](u5-l2-tfdata-pipeline.md) 的 `_variant_tensor` 类似——**描述与数据分离**。

#### 4.1.2 核心流程

`saved_model.pb` 里反序列化出来是一个 `SavedModel` protobuf，其顶层结构是：

```
SavedModel (文件 saved_model.pb)
└── meta_graphs[]            # 可有多份（按 tag 区分），TF2 save 一般只写 1 份
    └── MetaGraphDef
        ├── meta_info_def    # tags（如 "serve"）、TF 版本号
        ├── graph_def        # GraphDef：扁平 NodeDef 列表（见 u3-l1）
        │   └── library      # FunctionDefLibrary：所有 ConcreteFunction 的 FunctionDef
        ├── saver_def        # TF1 兼容的保存器描述
        ├── signature_def    # SignatureDef 映射：{签名名 -> {inputs, outputs}}
        ├── asset_file_def   # 外部资源引用
        └── object_graph_def # SavedObjectGraph：TF2 的对象树（节点类型 + 子节点 + 函数）
```

也就是说，**同一个 MetaGraphDef 里同时存了两套表示**：

- 一套是面向 **TF1 / C++ / 服务端** 的扁平 `graph_def` + `signature_def`（靠 placeholder 名字 + SignatureDef 调用）。
- 一套是面向 **TF2 Python** 的 `object_graph_def`（靠对象树重建出 `tf.Module`、变量、`@tf.function`）。

保存时两套都写，加载时根据用途各取所需。这是后面源码会反复印证的「双轨制」。

#### 4.1.3 源码精读

Python 侧用 `path_helpers.py` 把这些常量拼成路径，例如变量目录与变量前缀：

[path_helpers.py:23-40](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/path_helpers.py#L23-L40) —— `get_or_create_variables_dir` / `get_variables_path` 说明变量文件前缀就是 `<export_dir>/variables/variables`（TensorBundle 会自动加 `.data-*` / `.index` 后缀）。

[path_helpers.py:43-55](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/path_helpers.py#L43-L55) —— `get_or_create_assets_dir` 拼出 `<export_dir>/assets`。

Schema 版本与文件名常量在保存时被直接引用：

[save.py:1468-1469](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/save.py#L1468-L1469) 设置 `saved_model_schema_version`（= 1）；

[save.py:1504-1509](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/save.py#L1504-L1509) 把序列化后的 `SavedModel` protobuf 以 `saved_model.pb` 为文件名**原子写入**（注释特别强调这必须是最后一次文件操作，因为外部程序靠 `saved_model.pb` 是否存在来判断「整个 SavedModel 写完了」）。

#### 4.1.4 代码实践

1. **实践目标**：直观看到 SavedModel 的磁盘产物。
2. **操作步骤**（示例代码，需本地运行）：

```python
import tensorflow as tf

class Adder(tf.Module):
    @tf.function(input_signature=[tf.TensorSpec(shape=[], dtype=tf.float32)])
    def add(self, x):
        return x + self.w          # 引用一个变量，确保 variables/ 非空

m = Adder()
m.w = tf.Variable(3.0)
tf.saved_model.save(m, "/tmp/adder")
```

3. **需要观察的现象**：运行后用文件浏览器或 `ls -R /tmp/adder` 查看目录，应看到 `saved_model.pb`、`variables/variables.index`、`variables/variables.data-*`。
4. **预期结果**：与 4.1.1 的目录树一致；若没有 asset，则无 `assets/` 子目录。
5. 若你的 TF 版本行为不同，以本地实际输出为准（待本地验证）。

#### 4.1.5 小练习与答案

- **练习 1**：为什么变量数值要单独放在 `variables/` 而不是塞进 `saved_model.pb`？
  - **答**：`saved_model.pb` 是一份「结构描述」protobuf，追求稳定、可跨版本；变量数值可能很大、可分片，且需要高效随机访问，因此用专门的 TensorBundle checkpoint 格式存储，便于增量保存/恢复与多设备分片。
- **练习 2**：`saved_model.pb` 必须是最后写入的文件，为什么？
  - **答**：外部消费者（如另一进程）常用「`saved_model.pb` 是否存在」作为「整个 SavedModel 写入完成」的标志（见 load 的 docstring）。若它先写、checkpoint 后写，消费者就可能加载到一个缺权重的半成品。

---

### 4.2 保存主链路：`tf.saved_model.save` → 对象图 + MetaGraph + checkpoint

#### 4.2.1 概念说明

`tf.saved_model.save(obj, export_dir, signatures=...)` 接收一个 `Trackable` 对象（通常是 `tf.Module` 或 Keras 模型），输出一个目录。它要做三件事，且分别落到 protobuf 的不同位置：

- 把 `obj` 及其子对象（变量、函数、子模块）遍历成一棵**对象图**，序列化进 `object_graph_def`。
- 把签名函数（`signatures`）追踪成图，连同所有函数一起放进 MetaGraphDef 的 `graph_def.library` 与 `signature_def`。
- 把所有变量的数值写进 `variables/`（一个 checkpoint）。

核心抽象有两个：

- **`_AugmentedGraphView`**：在标准 checkpoint 的对象图之上「增强」，额外把 `.signatures` 等属性挂到根对象，并统一枚举函数（保证每次拿到的函数视图一致）。
- **`_SaveableView`**：对要保存的对象做一次「冻结快照」——因为有些对象的属性是每次访问都动态新建的（会产生不同对象），保存期间必须基于一个稳定视图操作。

#### 4.2.2 核心流程

```
save(obj, export_dir, signatures)                       # 公共入口
  └─ save_and_return_nodes(...)                          # 真正干活
       ├─ _build_meta_graph(obj, signatures, options)    # 建图阶段
       │    ├─ _AugmentedGraphView(obj)                  # 构造增强对象图
       │    ├─ canonicalize_signatures(signatures)       # 归一化签名
       │    ├─ _SaveableView(graph_view, options)        # 冻结快照
       │    ├─ _fill_meta_graph_def(...)                 # 填 MetaGraphDef：追踪签名图、放函数、写 saver
       │    └─ _serialize_object_graph(...)              # 填 object_graph_def：对象节点 + 函数序列化
       ├─ object_saver.save(variables_path)              # 写 checkpoint（变量数值）
       ├─ builder_impl.copy_assets_to_destination_dir()  # 拷贝 asset 到 assets/
       └─ 原子写入 saved_model.pb                        # 最后一步
```

伪代码层面，最关键的对应关系是：

```
object_graph_def  ←  _serialize_object_graph(saveable_view)   # 对象树 + 函数
graph_def.library ←  ConcreteFunction 的 FunctionDef          # 函数体
signature_def     ←  _generate_signatures(...)                # 签名
variables/*       ←  object_saver.save(...)                   # 变量数值
```

#### 4.2.3 源码精读

**公共入口**只是转发，并记录指标：

[save.py:1241-1434](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/save.py#L1241-L1434) —— `@tf_export("saved_model.save")` 装饰的 `save` 函数。它的 docstring 给了非常完整的签名语义说明（包括 Keras 模型如何导出），值得通读。函数体（L1431-L1434）仅 `metrics.IncrementWriteApi(...)` 后调 `save_and_return_nodes(...)`。

**真正干活**的是 `save_and_return_nodes`，它先建图、再写盘：

[save.py:1466-1467](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/save.py#L1466-L1467) 调 `_build_meta_graph` 得到 `(meta_graph_def, exported_graph, object_saver, asset_info, ...)`；

[save.py:1473-1479](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/save.py#L1473-L1479) 用 `object_saver.save(variables_path)` 把变量数值写成 checkpoint —— 这就是 `variables/` 目录的来源；

[save.py:1480-1481](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/save.py#L1480-L1481) 拷贝 asset；

[save.py:1504-1509](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/save.py#L1504-L1509) 最后原子写入 `saved_model.pb`。

**建图阶段** `_build_meta_graph_impl` 把上面三件事串起来：

[save.py:1590-1601](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/save.py#L1590-L1601) 构造 `_AugmentedGraphView`，若用户没传 `signatures` 则自动找一个可导出的 `@tf.function` 作为默认签名（`find_function_to_export`），再 `canonicalize_signatures` 归一化；

[save.py:1604-1615](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/save.py#L1604-L1615) 构造 `_SaveableView` 与 `object_saver`，然后调 `_fill_meta_graph_def`（填 MetaGraph）——注意此处 `create_saver=not options.experimental_skip_saver` 控制 TF1 兼容 saver 是否生成；

[save.py:1635-1638](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/save.py#L1635-L1638) 调 `_serialize_object_graph` 填 `object_graph_def`。两者填好后塞进同一个 `meta_graph_def`。

**对象图序列化** `fill_object_graph_proto` + `_write_object_proto` 是「按节点类型分发」的典型：

[save.py:1171-1213](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/save.py#L1171-L1213) —— `_write_object_proto` 用一连串 `isinstance` 判断对象类型，分别写入 protobuf 的不同 `kind` 字段：`asset.Asset` → `proto.asset`；资源变量 → 委托 `obj._write_object_proto`；`def_function.Function`（多态函数）→ `serialize_function`；`ConcreteFunction` → `serialize_bare_concrete_function`；`_CapturedTensor` → `proto.captured_tensor`；`CapturableResource` → `proto.resource`；其余 → `user_object`（含 Keras 这类注册类型）。这段就是「Python 对象 → protobuf」的总分发器。

**MetaGraph 的填充** `_fill_meta_graph_def` 负责 TF1/C++ 那套表示：

[save.py:966-970](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/save.py#L966-L970) 新建一张 `exported_graph`，在其中 `map_resources`（把变量映射成图里的资源句柄 op）并 `_generate_signatures`（为每个签名造 placeholder、调函数、产出 `SignatureDef`）；

[save.py:1039-1051](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/save.py#L1039-L1051) 把 `graph_def`、`tags`（`SERVING`）、TF 版本、`asset_file_def`、`signature_def` 全部填进 `meta_graph_def`。

> **关键结论二**：保存路径里有两套「图」——`_fill_meta_graph_def` 生成的是给 TF1/C++ 用的扁平 `graph_def` + `signature_def`；`_serialize_object_graph` 生成的是给 TF2 Python 用的 `object_graph_def`。两者都进同一个 `saved_model.pb`。

#### 4.2.4 代码实践

1. **实践目标**：对照源码确认「图、变量、签名」分别被写到哪里。
2. **操作步骤**：在 4.1.4 的保存脚本基础上，加上：

```python
import tensorflow as tf
import os

# 用 Keras 的 Functional 模型，它会自带签名（无需手写 input_signature）
x = tf.keras.layers.Input((4,), name="x")
y = tf.keras.layers.Dense(5, name="out")(x)
model = tf.keras.Model(x, y)
tf.saved_model.save(model, "/tmp/sm")

# 1) 变量：检查 variables/ 子目录
print("variables:", os.listdir("/tmp/sm/variables"))
# 2) 签名 + 图：用内置工具读取 saved_model.pb，不加载权重
m = tf.saved_model.load("/tmp/sm")
print("signatures:", list(m.signatures.keys()))      # 应含 'serving_default'
print("vars:", [v.name for v in m.variables])        # 模型权重
```

3. **需要观察的现象**：`variables/` 下有 `.index` 与 `.data-*`；`m.signatures` 是一个映射，键为 `serving_default`。
4. **预期结果**：变量数值在 `variables/`（对应 [save.py:1473-1479](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/save.py#L1473-L1479)）；签名在 `saved_model.pb` 的 `signature_def`（对应 [save.py:1050-1051](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/save.py#L1050-L1051)）；图在 `saved_model.pb` 的 `graph_def`。
5. 待本地验证（不同 Keras/TF 版本签名键名可能略有差异）。

#### 4.2.5 小练习与答案

- **练习 1**：`_write_object_proto` 里，`tf.Variable` 和 `@tf.function` 分别被写进 protobuf 的哪个字段？
  - **答**：资源变量经 `obj._write_object_proto` 写入 `proto.variable`（一种内置 kind）；`@tf.function`（`def_function.Function`）经 `serialize_function` 写入 `proto.function`，而它名下的每个 `ConcreteFunction` 作为节点以 `bare_concrete_function` 形式出现，函数体本身进 `graph_def.library`。
- **练习 2**：如果保存时用户没传 `signatures` 会怎样？
  - **答**：`_build_meta_graph_impl` 会调 `find_function_to_export` 自动找一个已追踪的 `@tf.function` 作为默认签名（见 docstring）；若找到多个或不唯一，则仅作为 `tf.saved_model.load` 后可调用的属性，不生成默认服务签名。

---

### 4.3 ConcreteFunction 的序列化：function_serialization.py

#### 4.3.1 概念说明

一个 `@tf.function` 可能对应多个 `ConcreteFunction`（每种输入签名一份，见 u3-l4）。序列化时必须把以下信息都存下，加载后才能复原成「可调用、能取到正确捕获变量」的函数：

- **函数体**：图节点 → 这部分作为 `FunctionDef` 放进 `graph_def.library`（与 u3-l1 的 GraphDef 机制一致）。
- **结构化输入/输出签名**：`structured_input_signature` / `structured_outputs`（嵌套结构、`TensorSpec`），用 `nested_structure_coder` 编码进 `SavedConcreteFunction`。
- **捕获张量的绑定（bound_inputs）**：函数引用了哪些外部对象（典型是变量），加载时要重新接线。这是最关键也最容易出错的一环。
- **FunctionSpec**：原始 Python 函数的 `fullargspec`、`input_signature`、`jit_compile` 等，用于加载后恢复「按 Python 调用约定分派到具体 ConcreteFunction」的能力。

#### 4.3.2 核心流程

```
serialize_concrete_function(cf, node_ids):
    for capture in cf.captured_inputs:
        bound_inputs.append(node_ids[capture])     # 捕获张量 → 对象图节点 id
    proto.canonicalized_input_signature = encode(cf.structured_input_signature)
    proto.output_signature            = encode(cf.structured_outputs)
    proto.bound_inputs                = bound_inputs

serialize_function(fn, concrete_functions):       # 多态函数
    proto.function_spec = _serialize_function_spec(fn.function_spec)
    proto.concrete_functions = [cf.name, ...]      # 指向各 ConcreteFunction 名
```

`bound_inputs` 存的是「对象图节点 id」（整数），而不是张量名——这一点决定了加载时函数与变量是靠**对象图拓扑**重新连上的，而非靠字符串名字。

#### 4.3.3 源码精读

**ConcreteFunction 序列化**：

[function_serialization.py:62-85](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/function_serialization.py#L62-L85) —— `serialize_concrete_function`。注意 L65-L75 的 `try/except KeyError`：若某个捕获张量在 `node_ids` 里找不到（即它「从根对象不可达」，比如函数依赖了一个没挂到任何属性上的变量），会抛出明确的 `KeyError` 提示用户「该状态对象未分配为被序列化对象的属性」。这正是 docstring 里 `test_captures_unreachable_variable` 的来源。

**多态 Function 序列化**：

[function_serialization.py:134-142](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/function_serialization.py#L134-L142) —— `serialize_function` 只存 `function_spec` + 一串 `concrete_function` 名字指针；真正的函数体分散在各 ConcreteFunction 的 `FunctionDef` 中。

**FunctionSpec 编码**：

[function_serialization.py:27-59](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/function_serialization.py#L27-L59) —— `_serialize_function_spec` 把 `fullargspec`（注意 L39-L46 刻意丢弃 annotations）、`input_signature`、`jit_compile` 三态（`None/True/False` → `DEFAULT/ON/OFF`）编码成 proto。

**一个微妙但重要的处理——缓存变量的包装**：

[function_serialization.py:145-220](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/function_serialization.py#L145-L220) —— `wrap_cached_variables`。如果一个 ConcreteFunction 捕获的是变量的「缓存读张量」（`_cached_variable`，即 `var.read_value()` 的缓存快照，值是固定的），直接存会把快照值固化、加载后无法跟随变量。于是这里构造一个外层包装函数，把捕获从「缓存读张量」改写成「变量本身（`read_value()` op）」，使加载后函数引用的是可变变量。保存侧在 `_AugmentedGraphView._maybe_uncache_variable_captures`（[save.py:208-220](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/save.py#L208-L220)）发现这种情况时会调用它。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：用「读源码 + 看测试断言」的方式确认 `bound_inputs` 的含义。
2. **操作步骤**：
   - 打开 [function_serialization.py:62-85](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/function_serialization.py#L62-L85)，找到 `bound_inputs.append(node_ids[capture])`。
   - 在仓库里搜索加载侧如何消费 `bound_inputs`：`restore_captures`（`from tensorflow.core.function.capture import restore_captures`，见 [load.py:25](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/load.py#L25)）与 `Loader._setup_function_captures`（[load.py:395-403](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/load.py#L395-L403)）。
3. **需要观察的现象**：保存时 `bound_inputs = 对象图节点 id 列表`；加载时 `inputs = [nodes[node_id] for node_id in proto.bound_inputs]`，即用重建出来的对象（变量）回填捕获。
4. **预期结果**：理解「函数与变量的连接靠对象图节点 id，不靠名字」。
5. 待本地验证（可在此函数加日志观察 `node_ids[capture]` 的值）。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `serialize_concrete_function` 在捕获张量找不到对应节点时要报错，而不是静默跳过？
  - **答**：捕获张量通常是函数依赖的变量或常量；若它从根对象不可达，加载后函数就缺少了它依赖的状态，调用必然出错。提前报错能给出清晰的可操作提示（把该对象挂到某个属性上）。
- **练习 2**：`SavedFunction`（多态函数）的 protobuf 自己存函数体吗？
  - **答**：不存。它只存 `function_spec` 与一组 `concrete_function` 名字指针；真正的函数体（图节点）存放在各 ConcreteFunction 对应的 `FunctionDef` 里，归 `graph_def.library`。

---

### 4.4 加载主链路：`tf.saved_model.load` → 重建对象图 + 恢复 checkpoint

#### 4.4.1 概念说明

`tf.saved_model.load(export_dir)` 是 `save` 的逆过程，但它有一个根本不同：**加载是「按需重建 Python 对象」，而不是恢复原始类**。例如你存的是 `tf.keras.Model`，加载回来的根对象通常是一个 `AutoTrackable`（不是 Keras Model，没有 `.fit`）。这是因为 SavedModel 存的是「对象结构 + 类型标识」，而非 Python 类本身。

加载的三个核心动作：

1. **读 protobuf**：解析 `saved_model.pb`，取出 `object_graph_def`（TF2）或回退到 TF1 路径。
2. **重建对象图**：按依赖拓扑顺序，逐个把 protobuf 节点「复活」成 Python 对象（变量、函数、用户对象），并用子节点引用把属性重新连上。
3. **恢复 checkpoint**：把 `variables/` 里的数值灌回重建出的变量。

核心抽象是 `Loader` 类。

#### 4.4.2 核心流程

```
load(export_dir)                              # 公共入口
  └─ load_partial(export_dir, filters=None)
       ├─ parse_saved_model_with_debug_info() # 读 saved_model.pb
       ├─ if meta_graph 有 object_graph_def:  # TF2 SavedModel
       │     Loader(object_graph, saved_model, ...)  # 真正加载
       │       ├─ load_function_def_library()  # FunctionDef → ConcreteFunction
       │       ├─ _generate_ordered_node_ids() # 按依赖排序节点
       │       ├─ _load_all()
       │       │    ├─ _load_nodes()           # 逐节点重建对象
       │       │    ├─ _load_edges()           # 连属性边
       │       │    ├─ _setup_remaining_functions()  # 给函数接上捕获
       │       │    └─ _load_checkpoint_save_and_restore_functions()
       │       └─ _restore_checkpoint()        # 把 variables/ 灌回
       └─ else: load_v1_in_v2.load(...)        # TF1 SavedModel 走老路径
```

#### 4.4.3 源码精读

**公共入口与 TF1/TF2 分流**：

[load.py:820-913](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/load.py#L820-L913) —— `load` 仅 `load_partial(...)["root"]`。

[load.py:1019-1073](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/load.py#L1019-L1073) —— `load_partial` 的分流核心：L1019-L1020 判断 `HasField("object_graph_def")`，是则走 TF2 的 `Loader`（L1042），否则走 `load_v1_in_v2.load`（L1070）。`is_tf2_saved_model`（[load.py:1111-1145](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/load.py#L1111-L1145)）正是用「有没有 object_graph_def」来判定一个 SavedModel 是 TF2 还是 TF1。

**`Loader.__init__`：先加载函数库，再拓扑排序，再重建**：

[load.py:160-164](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/load.py#L160-L164) 调 `function_deserialization.load_function_def_library` 把 `graph_def.library` 里的每个 `FunctionDef` 变成（尚无捕获的）`ConcreteFunction`，存进 `self._concrete_functions`；

[load.py:221](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/load.py#L221) `_generate_ordered_node_ids` 按依赖排序节点（依赖在前）；

[load.py:223-231](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/load.py#L223-L231) `_load_all()` 重建对象与连线，随后 `_restore_checkpoint()` 灌权重。

**逐节点重建 `_recreate`：按类型分发**：

[load.py:620-655](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/load.py#L620-L655) 先查 `registration.get_registered_class_name`（注册类型，如 Keras），再回退到 `_BUILT_IN_REGISTRATIONS`（[load.py:75-78](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/load.py#L75-L78) 定义了 `asset`/`resource`/`constant` 三类内置），仍找不到则走 `_recreate_default`。

[load.py:657-674](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/load.py#L657-L674) —— `_recreate_default` 用一个 `factory` 字典按 `kind` 分派：`user_object` → `_recreate_user_object`；`function` → `_recreate_function`；`variable` → `_recreate_variable`；`bare_concrete_function` → `_recreate_bare_concrete_function`；`captured_tensor` → `_get_tensor_from_fn`。这与保存侧 `_write_object_proto` 的分发**严格对称**。

**变量的重建**：

[load.py:765-808](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/load.py#L765-L808) —— `_recreate_variable` 用一个 `uninitialized_variable_creator` 创建**未初始化**的 `UninitializedVariable`（只按 proto 恢复 shape/dtype/trainable，**不填数值**），数值留给后面 `_restore_checkpoint` 统一灌入。这样变量的「结构」与「数值」解耦，与保存侧「变量在 checkpoint、结构在 pb」一一对应。

**函数的接捕获**：

[load.py:395-403](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/load.py#L395-L403) —— `_setup_function_captures` 用 `proto.bound_inputs`（对象图节点 id）取回已重建的对象，再 `restore_captures.restore_captures(concrete_function, inputs)` 把它们重新绑定为函数的捕获输入。这一步实现了 4.3 所述的「靠节点 id 重连」。

**checkpoint 恢复**：

[load.py:548-562](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/load.py#L548-L562) —— `_restore_checkpoint` 用 `checkpoint.TrackableSaver` 指向 `variables/` 子目录恢复，默认 `assert_existing_objects_matched()`（要求所有对象都匹配到 checkpoint 项）；`allow_partial_checkpoint` 时放宽为 `expect_partial()`。

> **关键结论三**：加载是「结构在内存里用 protobuf 重建对象，数值最后从 checkpoint 灌回」的两段式过程。变量先以未初始化形态被创建，函数的捕获靠对象图节点 id 重新接线。

#### 4.4.4 代码实践

1. **实践目标**：验证「加载返回的对象不是原始类，但变量与签名可用」。
2. **操作步骤**：在 4.1.4 保存后，加载并检查类型与可调用性：

```python
import tensorflow as tf
loaded = tf.saved_model.load("/tmp/adder")
print(type(loaded).__name__)                 # 通常是 _UserObject / AutoTrackable，而非 Adder
print(loaded.w.numpy())                      # 变量数值应被恢复（= 3.0）
print(tf.round(loaded.add(tf.constant(2.0)))) # 可调用，结果 ≈ 5.0
# 也可通过签名调用（Keras/带签名模型）
# sig = loaded.signatures["serving_default"]
```

3. **需要观察的现象**：`type(loaded)` 不是你保存时的 `Adder` 类；但 `loaded.w` 的数值被正确恢复，`loaded.add` 仍可调用。
4. **预期结果**：对应 [load.py:708-717](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/load.py#L708-L717) 的 `_recreate_base_user_object`（生成一个全新的 `_UserObject(AutoTrackable)` 子类），与 [load.py:548-562](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/load.py#L548-L562) 的 checkpoint 恢复。
5. 待本地验证。

#### 4.4.5 小练习与答案

- **练习 1**：为什么加载出的变量要先创建成「未初始化」，再由 checkpoint 恢复？
  - **答**：对象的重建顺序由对象图拓扑决定，未必与变量在 checkpoint 里的存储顺序一致；且 protobuf 只携带结构（shape/dtype）。统一用 `_restore_checkpoint` 在所有对象建好后一次性灌入数值，能避免「先随机初始化再覆盖」的浪费，也便于 `assert_existing_objects_matched` 校验完整性。
- **练习 2**：保存一个 Keras `Model`，加载后它还有 `.fit` 方法吗？为什么？
  - **答**：没有。`tf.saved_model.load` 默认用 `_recreate_base_user_object` 重建为 `AutoTrackable`，只保留 `.variables`、`.trainable_variables`、`__call__`、`.signatures` 等。要恢复成可训练的 Keras 模型需用 `tf.keras.models.load_model`（它依赖 Keras 的注册类型机制 `revived_types`）。

---

### 4.5（补充模块）builder_impl.py 的辅助职责与 TF1 双轨

#### 4.5.1 概念说明

[builder_impl.py](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/builder_impl.py) 提供的是 **TF1 风格**的 `SavedModelBuilder`：基于「显式 Session + collection + SignatureDef」构建 SavedModel。TF2 的 `tf.saved_model.save` **并不直接用它来建图**（用的是 4.2 的 `_build_meta_graph`），但会复用它提供的两个工具函数来处理 asset：

- `copy_assets_to_destination_dir`：把外部资源文件拷进 `assets/`。
- `get_asset_filename_to_add`：给重名 asset 生成唯一文件名。

#### 4.5.2 核心流程

TF1 builder 的典型用法（见类 docstring）：

```
builder = Builder(export_dir)
builder.add_meta_graph_and_variables(sess, tags=[...], signature_def_map=...)
builder.add_meta_graph(tags=[...])      # 后续仅加图，不再存变量
builder.save()                          # 写 saved_model.pb
```

它强制一个不变量（[builder_impl.py:349-352](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/builder_impl.py#L349-L352) 与 [builder_impl.py:271-274](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/builder_impl.py#L271-L274)）：变量只能在**第一个** `add_meta_graph_and_variables` 中存一次，之后只能用 `add_meta_graph` 追加「共享同一组变量」的更多 MetaGraph。这正是「一个 SavedModel 可含多个按 tag 区分的 MetaGraph、共享变量」的设计来源。

#### 4.5.3 源码精读

[builder_impl.py:806-839](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/builder_impl.py#L806-L839) —— `copy_assets_to_destination_dir`：对每个 asset 取 basename，拷到 `<export_dir>/assets/` 下。这是 `save.py` L1480-L1481 在 TF2 保存时调用的函数。

[builder_impl.py:717-753](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/builder_impl.py#L717-L753) —— `get_asset_filename_to_add`：当两个 asset 同名但内容不同时，自动加 `_1`、`_2` 后缀消歧；同文件则复用。

#### 4.5.4 代码实践（阅读型）

阅读 [builder_impl.py:48-92](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/builder_impl.py#L48-L92) 的类 docstring，理解 TF1「多 MetaGraph 共享变量」的工作流，并对照本讲 4.2 的 TF2 单步 `save` 流程，体会两者差异：TF1 显式管理 Session 与 collection，TF2 自动遍历对象树。

#### 4.5.5 小练习与答案

- **练习**：TF2 的 `tf.saved_model.save` 会调用 `builder_impl` 的哪两个工具函数？
  - **答**：`copy_assets_to_destination_dir`（拷贝 asset 到 `assets/`）与 `get_asset_filename_to_add`（在 `save.py` 的 `_add_asset_info` 里用于生成目标文件名）。`SavedModelBuilder` 类本身在 TF2 保存路径中不被使用。

## 5. 综合实践

把本讲四个模块串起来：保存一个**带变量、带签名、带一个 asset（词表文件）** 的小模型，加载后验证三要素都被正确搬运。

```python
import tensorflow as tf, os

# 1) 准备一个 asset（模拟词表文件）
vocab_path = "/tmp/vocab.txt"
with open(vocab_path, "w") as f:
    f.write("hello\nworld\n")

class Serving(tf.Module):
    def __init__(self):
        self.w = tf.Variable(2.0)                       # 变量
        self.vocab = tf.saved_model.Asset(vocab_path)   # asset

    @tf.function(input_signature=[tf.TensorSpec([], tf.float32)])
    def serve(self, x):
        return x * self.w

m = Serving()
tf.saved_model.save(
    m, "/tmp/serving_model",
    signatures=m.serve,                  # 显式指定签名
)

# 2) 加载并逐项核对
loaded = tf.saved_model.load("/tmp/serving_model")
print("var:", loaded.w.numpy())                       # 2.0  —— 来自 variables/
print("sig:", list(loaded.signatures.keys()))         # ['serving_default'] —— 来自 signature_def
print("asset:", loaded.vocab.asset_path.numpy())      # 指向 assets/vocab.txt
print("out:", loaded.signatures["serving_default"](
    x=tf.constant(3.0)))                               # 6.0 —— 函数体来自 graph_def.library
print("files:", sorted(os.listdir("/tmp/serving_model")))
print("vars dir:", os.listdir("/tmp/serving_model/variables"))
```

**对照源码，解释三要素去向**：

| 要素 | 磁盘位置 | 保存源码 | 加载源码 |
| --- | --- | --- | --- |
| 变量 `w` 数值 | `variables/variables.*` | [save.py:1473-1479](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/save.py#L1473-L1479) | [load.py:548-562](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/load.py#L548-L562) |
| 签名 `serve` | `saved_model.pb` 的 `signature_def` | [save.py:1050-1051](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/save.py#L1050-L1051) | `loaded.signatures`（`Loader` 重建） |
| 函数体（乘法） | `saved_model.pb` 的 `graph_def.library` | [save.py:1153-1159](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/save.py#L1153-L1159) | [load.py:160-164](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/load.py#L160-L164) |
| asset 词表 | `assets/vocab.txt` | [save.py:1480-1481](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/save.py#L1480-L1481) | `_BUILT_IN_REGISTRATIONS["asset"]` |
| 对象结构 | `saved_model.pb` 的 `object_graph_def` | [save.py:1635-1638](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/save.py#L1635-L1638) | [load.py:1020-1043](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/saved_model/load.py#L1020-L1043) |

> 若运行中 asset 路径或签名键名与预期不符，以本地实际输出为准（待本地验证）。

## 6. 本讲小结

- SavedModel 目录 = `saved_model.pb`（结构）+ `variables/`（变量数值）+ `assets/`（外部资源）；魔法字符串定义在 [constants.h](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/cc/saved_model/constants.h#L21-L66)。
- `saved_model.pb` 里**双轨并存**：`MetaGraphDef`（`graph_def` + `signature_def`，给 TF1/C++/serving）与 `object_graph_def`（给 TF2 Python 重建对象）。
- 保存主链路：`save` → `save_and_return_nodes` → `_build_meta_graph`（建 MetaGraph + 对象图）+ `object_saver.save`（写 checkpoint）+ 原子写 `saved_model.pb`。
- `_write_object_proto`（保存）与 `_recreate_default`（加载）是严格对称的「按类型分发」总入口。
- `ConcreteFunction` 的捕获张量用**对象图节点 id**（`bound_inputs`）记录，加载时靠拓扑重新接线，而非靠名字。
- 加载是两段式：先按 protobuf 重建对象（变量先建为未初始化），再用 `variables/` checkpoint 统一灌入数值；返回对象默认是 `AutoTrackable` 而非原始类。

## 7. 下一步学习建议

- 阅读 [u5-l4](u5-l4-keras-high-level-api.md) Keras 高层 API，理解 Keras 如何在 SavedModel 之上通过 `revived_types` 注册机制实现「加载即还原成 Keras 模型」，补全本讲 4.4.5 练习 2 留下的缺口。
- 结合 [u5-l2](u5-l2-tfdata-pipeline.md) 体会 `_variant_tensor` 与 `object_graph_def` 都体现了「描述与数据分离」的同一设计哲学。
- 进阶可阅读 `tensorflow/python/saved_model/revived_types.py`、`registration.py`，理解自定义类如何通过 `@register` 装饰器让加载端能还原出原始 Python 类与行为。
