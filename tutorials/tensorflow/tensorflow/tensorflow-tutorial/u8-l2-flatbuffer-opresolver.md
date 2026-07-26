# FlatBuffer 模型格式与 OpResolver

> 依赖前置讲义：[u8-l1 TFLite 架构与 Interpreter](u8-l1-tflite-architecture.md)。本讲假定你已掌握 TFLite 的「四件套」装配链路（`FlatBufferModel` → `InterpreterBuilder` + `OpResolver` → `Interpreter` → `AllocateTensors` → `Invoke`），以及 common.h 中 `TfLiteRegistration` 的 `init/free/prepare/invoke` 四个函数指针契约。

## 1. 本讲目标

在上一讲里，我们把推理拆成四件套时，只含糊地说了一句「`OpResolver` 把 op code 解析成 `TfLiteRegistration`」。本讲就来正面回答两个被刻意推迟的问题：

1. **模型在磁盘上长什么样？** 为什么 TFLite 偏偏选了 FlatBuffer，而不是桌面端 TF 用的 protobuf？
2. **op code 如何变成可执行代码？** 模型里只存了一个枚举或一个字符串名，运行时凭什么能找到对应的 C 函数？

学完本讲你应当能：

- 说清 FlatBuffer「零拷贝」的含义，以及它为何契合移动端「小内存、只读、反复加载」的场景。
- 读懂 `OpResolver` 抽象接口与其两个 `FindOp` 重载，理解「(op, version) → `TfLiteRegistration`」这张映射表的作用。
- 用 `MutableOpResolver` 的 `AddBuiltin` / `AddCustom`（以及现代推荐的 `AddOp`）描述「为一个自定义 op 注册 kernel」的完整步骤。
- 读懂 `flatbuffer_conversions` 的职责：它和 `OpResolver` 一起，把磁盘上的 FlatBuffer 字节翻译成运行时可用的 `TfLiteNode`。
- 把以上四者串成一条建图链路，看清 `InterpreterBuilder` 在哪一行把「函数指针」与「参数」拼到一个节点里。

## 2. 前置知识

- **序列化格式**：训练好的模型本质是「计算图 + 权重」，存成文件就要定一套「字节怎么排布」的规矩，这就是序列化格式。FlatBuffers 与 protobuf 都是序列化格式，只是取舍不同。
- **FlatBuffers vs protobuf（读这一步）**：protobuf 读取时要把整段字节 **parse** 成一棵临时对象树（每个对象 new 出来、逐字段填充），读写两端各存一份内存；FlatBuffers 的序列化字节**本身就是内存表示**，读取返回的是指向字节内部偏移的指针，**不分配、不拷贝、不解析**，按需访问。
- **builtin op 与 custom op**：TFLite 把算子分两类。**builtin（内建）** 有固定枚举码（如 `ADD`、`CONV_2D`）和固定 schema 参数表（如 `AddOptions`）；**custom（自定义）** 没有枚举码，靠字符串名（如 `"DoubleIt"`）标识，参数是一段用户自定义的裸字节（`custom_options`）。
- **`TfLiteRegistration`（u8-l1）**：TFLite 版的「kernel」，核心是 `init/free/prepare/invoke` 四个函数指针，外加 `builtin_code`、`custom_name`、`version` 三个身份字段。本讲核心问题就是：**模型里只存了「码」或「名字」，运行时怎么找到这组函数指针？**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `tensorflow/compiler/mlir/lite/schema/schema_v3.fbs` | 模型的 FlatBuffer **schema 定义**（人类可读 IDL）：`Model` / `SubGraph` / `Operator` / `OperatorCode` / `BuiltinOperator` 枚举。 |
| `tensorflow/compiler/mlir/lite/schema/schema_utils.cc` | `GetBuiltinCode()`：调和 schema 新旧两个 `builtin_code` 字段。 |
| `tensorflow/lite/core/api/op_resolver.h` | `OpResolver` **抽象基类**：定义 `FindOp` 契约与桥接函数 `GetRegistrationFromOpCode` 的声明。 |
| `tensorflow/lite/core/api/op_resolver.cc` | `GetRegistrationFromOpCode()` 实现：从 `OperatorCode` 取 code/version 再查 `OpResolver`。 |
| `tensorflow/lite/mutable_op_resolver.h` / `.cc` | `MutableOpResolver`：`OpResolver` 的可变实现，提供 `AddBuiltin`/`AddCustom`，内部两张哈希表。 |
| `tensorflow/lite/mutable_op_resolver_utils.h` / `.cc` | 现代注册助手 `AddOp()`：从不透明 `TfLiteOperator` 填充并注册。 |
| `tensorflow/lite/core/kernels/register.h` | `BuiltinOpResolver`：开箱即用、注册了全部内置 op 的解析器。 |
| `tensorflow/lite/core/api/flatbuffer_conversions.h` / `.cc` | FlatBuffer 字节 → 运行时结构体的**翻译层**：`ConvertTensorType`、`ParseAdd` 等一堆 `Parse*` 与分发器 `ParseOpData`。 |
| `tensorflow/lite/core/c/common.h` | `TfLiteRegistration` 结构体定义（被注册、被查找的对象）。 |
| `tensorflow/lite/core/interpreter_builder.cc` | 把模型 + `OpResolver` + `flatbuffer_conversions` 三者接起来的**建图总调度**。 |

## 4. 核心概念与源码讲解

> 一张图先建立全局视角——本讲四节正是这张图从上到下的四个环节：
>
> ```text
> .tflite 文件（FlatBuffer，零拷贝 mmap）            ← 4.1
>      │  Model.operator_codes[] 去重算子码表
>      │  SubGraph.operators[] 每个 op 持 opcode_index
>      ▼
> InterpreterBuilder 遍历算子码表：
>      GetRegistrationFromOpCode(opcode, op_resolver)  ← 4.2
>        ├─ builtin → FindOp(枚举, version)
>        └─ custom  → FindOp(名字, version)
>      结果缓存进 flatbuffer_op_index_to_registration_
>      ▼
> TfLiteRegistration*（init/prepare/invoke 函数指针）  ← 4.3（谁注册的？）
>      │
>      ├─ builtin：ParseOpData → ParseAdd/ParseConv2D… ← 4.4（参数从哪来？）
>      │            把 Options 翻译成 builtin_data（POD）
>      └─ custom：custom_options 字节流直接当 init_data 透传
>      ▼
> subgraph->AddNodeWithParameters(..., registration, builtin_data/init_data)
> ```

### 4.1 FlatBuffer 模型格式：算子的存储与零拷贝

#### 4.1.1 概念说明

u8-l1 讲过 `FlatBufferModel` 用 mmap 零拷贝加载模型。本节回答更细一层：**加载之后，算子在内存里到底长什么样，运行时怎么读它？**

FlatBuffers 的核心数据结构是 **table（表）**：一段字节里存一张「字段名→偏移」的 vtable 加数据区，缺省字段不占额外访问成本，字段顺序与读写无关。这让它在「写一次、读很多次、且只读」的场景下极具优势——而模型文件正是这种场景：训练完写一次，之后每次推理都要读。

#### 4.1.2 核心流程：模型的逻辑结构

`.tflite` 的根类型是 `Model`，schema 定义如下（节选关键表）：

[compiler/mlir/lite/schema/schema_v3.fbs:302-326](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/mlir/lite/schema/schema_v3.fbs#L302-L326) —— `Model` 是 `root_type`，含 `operator_codes`（所有用到的算子码，**去重**）、`subgraphs`、`buffers`（常量权重）。注释明说「kept in order because operators carry an index into this vector」「first entry [of buffers] is always an empty buffer」——算子码集中存一份、图里用整数下标引用，buffer 第 0 号恒空以保证默认下标合法。

[compiler/mlir/lite/schema/schema_v3.fbs:278-294](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/mlir/lite/schema/schema_v3.fbs#L278-L294) —— `SubGraph` 含 `tensors` / `inputs` / `outputs` / `operators`，第 0 号子图是主模型。

[compiler/mlir/lite/schema/schema_v3.fbs:262-276](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/mlir/lite/schema/schema_v3.fbs#L262-L276) —— 每个 `Operator` 只存 `opcode_index`（指向 `operator_codes`）、输入输出张量下标、`builtin_options`（内置 op 参数）或 `custom_options`（自定义 op 的裸字节）。**注意它不存算子类型字符串，只存一个整数下标。**

算子码本身由 `OperatorCode` 描述：

[compiler/mlir/lite/schema/schema_v3.fbs:255-260](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/mlir/lite/schema/schema_v3.fbs#L255-L260) —— `OperatorCode` 含 `builtin_code`（内置枚举）与 `custom_code`（自定义名字字符串）。

`BuiltinOperator` 是一张大枚举，`ADD = 0`、`CUSTOM = 32`：

[compiler/mlir/lite/schema/schema_v3.fbs:71-105](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/mlir/lite/schema/schema_v3.fbs#L71-L105) —— `CUSTOM = 32` 是特殊哨兵：当一个 op 的 `builtin_code == CUSTOM` 时，TFLite 改用 `custom_code` 字符串识别它。

串起来，模型加载后的内存视图是：

```text
.tflite 字节（mmap，零拷贝）
  └─ Model*（直接指向字节内部，不反序列化）
       ├─ operator_codes[]：去重算子码表（枚举 或 字符串）
       ├─ subgraphs[0].operators[]：每个算子 = opcode_index + inputs/outputs + options
       └─ buffers[]：常量权重（直接复用文件字节）
```

#### 4.1.3 源码精读：为什么适合移动端

把 FlatBuffer 的特性与手机/嵌入式约束一一对照：

| 移动端约束 | FlatBuffer 如何应对 |
| --- | --- |
| 内存小 | 模型可 `mmap` 映射进虚拟内存，**按页加载**，无需把整模型反序列化成对象树；常量权重直接复用文件字节。 |
| 启动要快 | 没有 parse 阶段，读取即指针解引用，O(1) 访问任意字段。 |
| 模型只读、反复推理 | 写慢/读快正好匹配——模型训练完只写一次。 |
| 二进制要小 | 算子码集中去重、字段缺省不占位，整体紧凑。 |

> **对照桌面端**：TF 桌面端用 protobuf 存 `GraphDef`（见 u3-l1）。protobuf 要 parse 出完整 `NodeDef` 对象树，对内存和启动时间都不友好；TFLite 换用 FlatBuffer，正是为了在手机上做到「小而快」。代价是 FlatBuffer 写入更复杂，但模型「写一次读无数次」，这个代价完全可接受。

> **关于 schema 的新旧字段**：`schema_utils.cc` 里你会看到 `op_code->builtin_code()` 和 `op_code->deprecated_builtin_code()` 两个字段。这是因为内置 op 越来越多，超过了早期 `int8`（最多 127 个）的容量，schema 引入了更宽的 `builtin_code`（`int32`）并保留旧的 `deprecated_builtin_code` 作向后兼容。`GetBuiltinCode` 用 `std::max` 取二者较大值来统一口径：

[compiler/mlir/lite/schema/schema_utils.cc:49-56](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/mlir/lite/schema/schema_utils.cc#L49-L56) —— 取新旧两个 builtin_code 的较大值，兼容旧模型。

#### 4.1.4 代码实践

**实践目标**：把「模型逻辑结构」从抽象概念变成可对照的源码地图。

**操作步骤（源码阅读型，无需编译）**：

1. 打开 `tensorflow/compiler/mlir/lite/schema/schema_v3.fbs`，定位 `table Model`（302 行）、`table SubGraph`（278 行）、`table Operator`（262 行）、`table OperatorCode`（255 行）、`enum BuiltinOperator`（71 行）。
2. 回答：一个只含两个 `ADD` 算子的小模型，`operator_codes[]` 数组里会有几个元素？
3. 确认 `enum BuiltinOperator` 里 `CUSTOM = 32`，并思考：为什么自定义 op 不再用更大的枚举值，而是改用字符串名？

**需要观察的现象 / 预期结果**：两个相同 `ADD` 的模型，`operator_codes[]` 只有 1 项，两个算子的 `opcode_index` 都指向 0；自定义 op 在 `OperatorCode` 里表现为 `builtin_code == CUSTOM` + `custom_code = "MyOp"`。

> ⚠️ 本步为纯源码阅读。若想真正打开一个 `.tflite` 查看其 FlatBuffer 内容，可用 TFLite 自带的 `tensorflow/lite/tools/visualize.py`（`python -m tensorflow.lite.tools.visualize model.tflite`），具体输出**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：FlatBuffer 相比 protobuf，读取时省掉了哪一步？这为何对移动端重要？
**答案**：省掉了「parse 成临时对象树」这一步（零拷贝）。移动端内存小、启动要快，零拷贝让模型可 mmap 按页加载、无需为整张图分配对象，显著降低峰值内存与加载延迟。

**练习 2**：为什么 `Operator` 里只存一个整数 `opcode_index`，而不是直接内嵌算子码？
**答案**：去重。一张图里同类算子（如多个 `CONV_2D`）复用同一个 `OperatorCode`，整数下标比重复存枚举+字符串更省空间。

---

### 4.2 OpResolver 抽象：从 op code 到 TfLiteRegistration

#### 4.2.1 概念说明

FlatBuffer 里的算子只是一个「名字」（枚举或字符串），**不携带任何可执行代码**。但 `invoke` 时显然要调用真正的 C 函数。于是需要一座桥：给定 (op 码, 版本)，返回一组函数指针（`init/prepare/invoke/free`）。这座桥就是 **`OpResolver`**。

这层间接带来三个关键好处：

1. **选择性注册（selective registration）**：可只把模型真正用到的算子链进二进制，其余不编译，大幅缩小移动端包体积。`OpResolver` 就是「链接哪些算子」与「模型里出现哪些算子」的缝合处。
2. **自定义算子**：用户可注册自己的 `CUSTOM` op。
3. **版本管理**：同一 op 有多版本（语义升级后 version +1），按 `(op, version)` 精确匹配。

把「查表」抽成抽象基类还有一个架构上的好处：**同一份模型可搭配不同运行时的 resolver**（完整版 TFLite、精简版 TFLite Micro、Google Play Services 版各自提供不同 kernel 子集）。模型依赖抽象接口，而非具体注册表——这是依赖倒置。

#### 4.2.2 核心流程

`OpResolver` 是纯抽象基类，只要求实现两个 `FindOp`：

```text
模型里的某个 OperatorCode
   ├─ builtin_code != CUSTOM  →  FindOp(BuiltinOperator 枚举, version)
   └─ builtin_code == CUSTOM  →  FindOp(custom_code 字符串, version)
                                        │
                                        ▔▔→ 返回 const TfLiteRegistration*（含 init/prepare/invoke/free）
```

`InterpreterBuilder` 在装配时调桥接函数 `GetRegistrationFromOpCode` 完成这步，并把结果按下标缓存，避免对同一算子码重复查表。

#### 4.2.3 源码精读

**① 抽象基类与 `FindOp` 契约**：

[lite/core/api/op_resolver.h:43-60](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/api/op_resolver.h#L43-L60) —— `OpResolver` 类声明，两个纯虚 `FindOp`：一个按内置枚举查，一个按自定义名字查。43-52 行的文档注释点明本质：*"the mechanism that ops being referenced in the flatbuffer model are mapped to executable function pointers (TfLiteRegistrations)"*，并强调返回的 `TfLiteRegistration` 生命期须长于任何用它构造的 `Interpreter`。

**② 桥接函数声明**：

[lite/core/api/op_resolver.h:215-221](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/api/op_resolver.h#L215-L221) —— `GetRegistrationFromOpCode` 接收 `OperatorCode*` + `OpResolver&`，输出 `const TfLiteRegistration**`，是把 schema 对象「翻译成查询、再查表」的胶水。

**③ 查表实现 `GetRegistrationFromOpCode`**：

[lite/core/api/op_resolver.cc:25-66](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/api/op_resolver.cc#L25-L66) —— 三分支逻辑：
- 先用 `GetBuiltinCode(opcode)` 统一新旧字段，取 `version`；
- 若 `builtin_code > BuiltinOperator_MAX`：报「op builtin_code out of range」，提示「旧二进制遇到新模型」；
- 若不是 `CUSTOM`：`op_resolver.FindOp(builtin_code, version)`，找不到则报错；
- 若是 `CUSTOM` 但无 `custom_code`：报错；否则 `FindOp(name, version)`。

注意 59-63 行对「找不到自定义 op」**不立即报错**的注释：「Do not report error for unresolved custom op, we do the final check while preparing ops.」——因为该自定义 op 可能稍后由 delegate 接管（见 u8-l3），故把最终判定推迟到 prepare。

**④ 返回的对象 `TfLiteRegistration`**（u8-l1 已讲结构，这里复用其身份字段）：

[lite/core/c/common.h:1238-1257](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/c/common.h#L1238-L1257) —— 三个身份字段 `builtin_code` / `custom_name` / `version`。注释写明「It is the responsibility of the registration binder to set this properly」——这个 binder 正是下一节的 `MutableOpResolver::AddBuiltin/AddCustom`。

#### 4.2.4 代码实践

**实践目标**：跟踪一次「算子码 → registration」的查找，验证 builtin 与 custom 两条路径的分叉点。

**操作步骤（源码阅读型）**：

1. 打开 [op_resolver.cc:25-66](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/api/op_resolver.cc#L25-L66)。
2. 假设模型有一个 `ADD`（builtin, version=1）：脑中走一遍，确认它命中 40-41 行的 `FindOp(builtin_code, version)`。
3. 假设模型有一个名为 `"DoubleIt"` 的 custom op（version=1）：确认它命中 57-58 行的 `FindOp(name, version)`。
4. 打开 [interpreter_builder.cc:263-291](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/interpreter_builder.cc#L263-L291)，确认 `GetRegistrationFromOpCode` 的返回值被 `push_back` 进 `flatbuffer_op_index_to_registration_` 这个「下标→registration」缓存向量。

**需要观察的现象**：`InterpreterBuilder` 把算子码表的解析结果缓存进一个 `vector<const TfLiteRegistration*>`（291 行），后续每个 `Operator` 只需用 `opcode_index` 做下标取出（见 345-353 行），避免对同一算子码重复查表。

**预期结果**：你能解释为什么要把「index→registration」单独建缓存——因为算子码表是去重的，一次解析、按下标复用，省掉大量字符串查找。

#### 4.2.5 小练习与答案

**练习 1**：`OpResolver` 为什么设计成抽象基类，而不是一个具体类？
**答案**：让「模型」与「具体 kernel 集合」解耦。同一份模型可搭配不同 `OpResolver` 实现（完整版、Micro 精简版、Play Services 版），各自提供能跑的 kernel 子集；模型只依赖抽象接口，遵循依赖倒置。

**练习 2**：一个 custom op 在 `GetRegistrationFromOpCode` 里查不到时，为什么不立即报致命错误？
**答案**：它可能稍后由某个 delegate（如 Flex delegate）接管。运行时先临时塞入一个未解析占位 op（`CreateUnresolvedCustomOp`），把最终判定推迟到 prepare 阶段。

---

### 4.3 MutableOpResolver：注册表实现与自定义 op 注册

#### 4.3.1 概念说明

`OpResolver` 只给契约，`MutableOpResolver` 是它最常用的**可变实现**：内部用两张哈希表存「(op, version) → `TfLiteRegistration`」，提供 `AddBuiltin` / `AddCustom` 逐个登记。它也是代码生成器生成「选择性注册解析器」的基类。在其之上，`BuiltinOpResolver` 是一个开箱即用的子类——构造函数里把**所有内置 op** 一次性注册好。

> **身份字段归谁填？** 4.2.3 提到 `TfLiteRegistration` 的 `builtin_code/custom_name/version` 由 registration binder 填。这个 binder 就是 `MutableOpResolver`：你在 kernel 里造的 `TfLiteRegistration` 通常只填四个函数指针，三个身份字段由 resolver 在登记时**强制覆写**，保证表里键与值一致。

#### 4.3.2 核心流程

```text
用户代码：
  MutableOpResolver resolver;
  resolver.AddBuiltin(BuiltinOperator_ADD, Register_ADD());   // Register_ADD() 返回 TfLiteRegistration*
  resolver.AddCustom("DoubleIt", &my_reg);
  InterpreterBuilder(model, resolver)(&interpreter);

内部存储：
  builtins_   : map<(BuiltinOperator, version), TfLiteRegistration>   // 值按值存储
  custom_ops_ : map<(string name, version),  TfLiteRegistration>
  other_op_resolvers_ : vector<const OpResolver*>                     // ChainOpResolver 回退链
```

**注册流程**（`AddBuiltin`/`AddCustom`）：拷贝传入的 `*registration`（按值存）→ 强制覆写身份字段 → 以 `(身份, version)` 为键写入对应表（覆盖同键旧值）。

**查找流程**（`FindOp`）：先查本地表，命中则返回表内元素地址（`&it->second`）；未命中再沿 `other_op_resolvers_` 链逐一回退；都没有返回 `nullptr`。

#### 4.3.3 源码精读

**① 类声明与 `FindOp` override**：

[lite/mutable_op_resolver.h:57-67](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/mutable_op_resolver.h#L57-L67) —— `MutableOpResolver : public OpResolver`。57-62 行的注释直接给出典型用法（注册 ADD + 自定义 op 再建图）。

**② 注册接口**：

[lite/mutable_op_resolver.h:69-96](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/mutable_op_resolver.h#L69-L96) —— `AddBuiltin`（69-79，支持单版本与版本区间）与 `AddCustom`（81-96）。注意 83-87、92-94 行的 Warning：新代码推荐用 `tflite::AddOp` 而非 `AddCustom`。

**③ 合并 / 链式**：

[lite/mutable_op_resolver.h:98-102](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/mutable_op_resolver.h#L98-L102) —— `AddAll` 把另一个 `MutableOpResolver` 的注册项**合并**（拷贝）进来；`ChainOpResolver`（protected，120 行）只存指针用于**回退查询**。

**④ 底层两张哈希表**（值按值存储的 `TfLiteRegistration`）：

[lite/mutable_op_resolver.h:143-152](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/mutable_op_resolver.h#L143-L152) —— `builtins_` 键 `(BuiltinOperator, int)`，`custom_ops_` 键 `(string, int)`，另有 `other_op_resolvers_`。

**⑤ `FindOp` 实现（先本表后链式）**：

[lite/mutable_op_resolver.cc:28-56](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/mutable_op_resolver.cc#L28-L56) —— 先 `builtins_.find`/`custom_ops_.find`，命中返回 `&it->second`（故返回指针生命期与 resolver 一致）；否则遍历 `other_op_resolvers_` 逐个问。

**⑥ `AddBuiltin` 的身份回填**：

[lite/mutable_op_resolver.cc:58-79](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/mutable_op_resolver.cc#L58-L79) —— 先 `TfLiteRegistration new_registration = *registration`（按值拷贝），再强制 `custom_name = nullptr; builtin_code = op; version = version`，最后 `builtins_[op_key] = new_registration`。61-66 行还容错：若工厂返回 `nullptr`（客户端库某些内置算子被裁掉），静默跳过。

`AddCustom` 同理，把 `builtin_code = BuiltinOperator_CUSTOM`、`custom_name = name`：

[lite/mutable_op_resolver.cc:89-99](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/mutable_op_resolver.cc#L89-L99) —— 自定义 op 的身份字段绑定逻辑。

> **为什么按值存 `TfLiteRegistration`？** 传入的指针常指向 `Register_XXX()` 返回的局部静态，生命期与调用点耦合。按值拷贝一份自持，`FindOp` 才能安全返回表内元素地址，且不依赖外部对象存活。

**⑦ 现代注册助手 `AddOp`**：新代码推荐用不透明的 `TfLiteOperator`（C ABI 稳定类型）描述算子，再由 `AddOp` 翻译成 `TfLiteRegistration` 并按 `custom_name` 是否非空决定走 `AddCustom` 还是 `AddBuiltin`：

[lite/mutable_op_resolver_utils.cc:24-41](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/mutable_op_resolver_utils.cc#L24-L41) —— 从 `TfLiteOperator` 取 `builtin_code`/`custom_name`/`version`，填 `registration_external`，再分流到 `AddCustom` 或 `AddBuiltin`。这是跨语言/跨二进制（如 Play Services 场景）注册算子的推荐入口；声明见 [mutable_op_resolver_utils.h:24-32](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/mutable_op_resolver_utils.h#L24-L32)。

**⑧ 开箱即用的 `BuiltinOpResolver`**：

[lite/core/kernels/register.h:31-38](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/kernels/register.h#L31-L38) —— `BuiltinOpResolver : public MutableOpResolver`，构造函数注册全部内置 op。33-36 行注释特意说明「故意不定义虚函数」以防对象切片（object slicing）；同文件还有 `BuiltinOpResolverWithXNNPACK`、`BuiltinOpResolverWithoutDefaultDelegates` 两个变体。

#### 4.3.4 代码实践（本讲核心实践任务）

**实践目标**：对照 `op_resolver.h` 与 `mutable_op_resolver.h`，动手写出「为一个自定义 op 注册 kernel」的最小骨架，并解释每步对应的源码行为。

**操作步骤（编写最小示例代码，待本地编译验证）**：

> 以下为「示例代码」（非项目原有文件），仿照官方指南 `tensorflow/lite/g3doc/guide/ops_custom.md` 的推荐写法。

第 1 步：实现自定义 op 的回调（至少要有 `invoke`）。

```cpp
// 示例代码：一个把所有元素乘 2 的 custom op
static TfLiteStatus MyPrepare(TfLiteContext* ctx, TfLiteNode* node) {
  // 输出形状 = 输入形状（具体 API 见 common.h，略）
  return kTfLiteOk;
}
static TfLiteStatus MyInvoke(TfLiteContext* ctx, TfLiteNode* node) {
  // 读输入、写「输入×2」到输出（略）
  return kTfLiteOk;
}
static TfLiteRegistration* Register_DoubleIt() {
  static TfLiteRegistration r = {nullptr, nullptr, MyPrepare, MyInvoke};
  return &r;
}
```

第 2 步：建 `MutableOpResolver` 并注册（官方推荐先用 `AddAll` 装上全部内置 op，再追加自定义 op）：

```cpp
// 示例代码（摘自 ops_custom.md 推荐写法）
tflite::ops::builtin::MutableOpResolver resolver;
resolver.AddAll(tflite::ops::builtin::BuiltinOpResolver());
resolver.AddCustom("DoubleIt", Register_DoubleIt(), /*version=*/1);
// 或现代写法：tflite::AddOp(&resolver, TfLiteOperatorCreate(...));
```

第 3 步：用这个 resolver 构造解释器：

```cpp
tflite::InterpreterBuilder builder(model, resolver);
std::unique_ptr<tflite::Interpreter> interpreter;
builder(&interpreter);
```

**对照源码解释现象**：

1. 第 2 步 `AddCustom` 后，对照 [mutable_op_resolver.cc:89-99](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/mutable_op_resolver.cc#L89-L99)：你传入的 `TfLiteRegistration` 被**按值拷贝**进 `custom_ops_`，`builtin_code` 被强制改写为 `BuiltinOperator_CUSTOM`、`custom_name` 改写为 `"DoubleIt"`、`version` 改写为 `1`。
2. 当模型里出现名为 `"DoubleIt"` 的算子时，对照 [op_resolver.cc:57-58](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/api/op_resolver.cc#L57-L58)：`GetRegistrationFromOpCode` 调 `FindOp("DoubleIt", 1)`，命中你注册的记录，返回 `MyPrepare`/`MyInvoke` 函数指针。
3. 解释 FlatBuffer 为何适合移动端：模型里只多存了一个字符串 `"DoubleIt"` 与整数 version，运行时按名查表即可，**无需把算子实现代码塞进模型文件**——kernel 代码住在宿主程序里。

**预期结果**：你能说清「自定义 op 的 kernel 代码住在宿主程序里（通过 `AddCustom` 注册进 resolver），模型文件里只存算子名字」这一分离设计，以及它如何支撑「同一模型、不同宿主提供不同实现」。

> ⚠️ 上述代码为示例骨架，未在本地编译运行；完整可编译的自定义 op 工程需配套 `BUILD` 目标与 `.tflite` 模型生成步骤，请以 `tensorflow/lite/g3doc/guide/ops_custom.md`、`tensorflow/lite/tools`、`tensorflow/examples` 下的完整示例为准（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `AddBuiltin`/`AddCustom` 在拷贝 registration 后要**强制覆写** `builtin_code`/`custom_name`/`version`，而不信任调用方传入的值？
**答案**：保证「表键」与「值对象自带身份」始终一致。调用方可能填错或没填，强制覆写能杜绝身份错配导致的查找错乱，使 `FindOp` 返回的对象自带正确身份。

**练习 2**：`version` 参数有什么用？
**答案**：算子语义/数据布局会演进（新增 fused activation、改量化方式）。新版本用更高 version 标识、对应新 kernel。模型导出时记录所用版本，运行时 `FindOp(op, version)` 据此挑出匹配 kernel，从而同时兼容新老模型。

**练习 3**：`AddAll` 和 `ChainOpResolver` 都能借用别的解析器，区别是什么？
**答案**：`AddAll` 是**合并**——把对方注册项拷进自己两张表（此后对方可销毁）；`ChainOpResolver` 是**回退链**——只存对方指针，本地查不到才去问对方（对方生命期须长于自己）。`AddAll` 的注册项优先级高于链式项。

---

### 4.4 flatbuffer_conversions：把 FlatBuffer 选项翻译成运行时参数

#### 4.4.1 概念说明

`OpResolver` 解决了「**谁来算**」（返回函数指针）。但每个算子还有「**用什么参数算**」——`ADD` 的 fused activation、`CONV_2D` 的 stride/padding、张量的 dtype。这些参数在 FlatBuffer 里是 schema 枚举与 `XxxOptions` 表，运行时却需要 C 结构体（`TfLiteAddParams`、`TfLiteType`...）。`flatbuffer_conversions` 就是这二者之间的**翻译层**。

与 `OpResolver` 的分工口诀：**`OpResolver` 管「谁来算」（函数指针），`flatbuffer_conversions` 管「用什么参数算」（数据）**。二者共同喂给 `InterpreterBuilder`，拼出一个 `TfLiteNode`（节点 = 函数指针 + 参数 + 输入输出张量）。

> **重要区别：custom op 不走这条路。** custom op 的参数是用户自定义的 `custom_options` 裸字节，运行时直接原样作为 `init_data` 传给 kernel 的 `init` 回调，由 kernel 自己解释——所以没有 `ParseXxx` 为它服务。

#### 4.4.2 核心流程

```text
InterpreterBuilder 遍历每个 Operator（读 registration->builtin_code）
  ├─ 若 custom：
  │     init_data = op->custom_options()->data()   // 裸字节透传，不走 ParseOpData
  │
  └─ 若 builtin：
        ParseOpData(op, op_type, ..., &builtin_data)
          └─ ParseOpDataTfLite(op, op_type, ...)    // 巨型 switch
                ├─ case ADD:     ParseAdd(...)      // 用 BuiltinDataAllocator 分配 TfLiteAddParams（POD）
                ├─ case CONV_2D: ParseConv2D(...)
                └─ ...（每个 builtin 一个 case）
        把 builtin_data 连同 registration 一起塞给 Subgraph 节点
```

两个要点：① 分配出的参数结构必须是 **POD**（平凡可析构），因为所有权会移交 C 扩展层、释放时不调析构函数；② 翻译对缺失 Options 采取「保留默认/留 TODO」的宽容策略，不轻易报错。

#### 4.4.3 源码精读

**① POD 分配器 `BuiltinDataAllocator`**：

[lite/core/api/flatbuffer_conversions.h:34-52](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/api/flatbuffer_conversions.h#L34-L52) —— `AllocatePOD<T>()` 用 `static_assert(std::is_trivially_destructible<T>::value)` 强制参数结构是 POD。注释点明原因：*"Interpreter's C extension part will take ownership so destructors will not be run during deallocation."* 所以参数结构不能有需析构的成员。

**② 类型翻译 `ConvertTensorType`**：

[lite/core/api/flatbuffer_conversions.cc:1038-1056](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/api/flatbuffer_conversions.cc#L1038-L1056) —— `switch` 把 schema 的 `TensorType_FLOAT32` 等映射到 `kTfLiteFloat32` 等。这是「schema 枚举 → 运行时枚举」的典型一一翻译。

**③ 单个算子的翻译 `ParseAdd`**：

[lite/core/api/flatbuffer_conversions.cc:1126-1149](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/api/flatbuffer_conversions.cc#L1126-L1149) —— 典型模板「分配 C 结构 → 用 FlatBuffer 访问器读 Options → 逐字段翻译 → 交出所有权」：用 `safe_allocator.Allocate<TfLiteAddParams>()` 分配 POD，`op->builtin_options_as_AddOptions()` 取 schema 参数，`ConvertActivation` 翻译激活枚举写入 `params->activation`，最后 `*builtin_data = params.release()`。注意 1141-1145 行注释：`schema_params == nullptr`（旧模型缺省）时不报错也不填默认值的历史权衡。

**④ 总分发器 `ParseOpDataTfLite`**：

[lite/core/api/flatbuffer_conversions.cc:196-202](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/api/flatbuffer_conversions.cc#L196-L202) —— 大 `switch` 里 `case BuiltinOperator_ADD: return ParseAdd(...)`，每个内置 op 一行分派。这就是「op 码 → 具体解析函数」的路由表。

**⑤ 外层 `ParseOpData` 的双实现**：

[lite/core/api/flatbuffer_conversions.cc:3014-3045](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/api/flatbuffer_conversions.cc#L3014-L3045) —— `ParseOpData` 在 TfLiteMicro（`TF_LITE_STATIC_MEMORY`）下直接返回 `kTfLiteError`（提示「请用算子专属的 Parse 函数」），桌面 TFLite 下转调 `ParseOpDataTfLite`。这条 `#ifdef` 分界线解释了为什么每个 `Parse*` 都单独导出：让 Micro 做选择性注册、只链入用到的解析函数。

**⑥ RAII 防泄漏包装**：

[lite/core/api/flatbuffer_conversions.cc:40-67](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/api/flatbuffer_conversions.cc#L40-L67) —— `SafeBuiltinDataAllocator` 把 `BuiltinDataAllocator` 包成 `unique_ptr<T, Deleter>`，解析中途出错（某行 `return kTfLiteError`）时已分配内存自动归还 allocator，避免泄漏。

**⑦ 建图主循环里的分叉（把全讲串起来）**：

[lite/core/interpreter_builder.cc:360-401](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/interpreter_builder.cc#L360-L401) —— 一眼看清 builtin 与 custom 的差别：先从 registration 读 `op_type`（361-362 行）；`CUSTOM` 分支把 `custom_options` 字节流当 `init_data`（373-391 行，含 `large_custom_options` 越界检查）；builtin 分支调 `ParseOpData` 翻译成 `builtin_data`（392-396 行）；最后 `subgraph->AddNodeWithParameters(..., init_data, init_data_size, builtin_data, registration)`（397-401 行）把函数指针与参数一起塞进节点。这一处正是「模型 + OpResolver + flatbuffer_conversions」三者的汇合点。

#### 4.4.4 代码实践

**实践目标**：以 `ADD` 为例走完「schema 参数 → 运行时参数」的翻译。

**操作步骤（源码阅读型）**：

1. 打开 [flatbuffer_conversions.cc 的 ParseAdd（1126-1149 行）](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/api/flatbuffer_conversions.cc#L1126-L1149)。
2. 在同文件找到 `ConvertActivation`（约 110 行起），看清 `ActivationFunctionType_RELU` → `kTfLiteActRelu` 的映射。
3. 追问：若模型的 `AddOptions` 缺省（`schema_params == nullptr`），`TfLiteAddParams` 的 `activation` 会是什么值？为什么源码选择「不报错也不填默认」？

**预期结果**：`TfLiteAddParams{}` 默认构造把 `activation` 零初始化为 `kTfLiteActNone`；源码不主动报错是为不破坏旧模型兼容行为。本步**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`flatbuffer_conversions` 和 `OpResolver` 各负责把什么翻译成什么？
**答案**：`OpResolver` 把「op 码 → `TfLiteRegistration`（函数指针）」；`flatbuffer_conversions` 把「schema `XxxOptions`/`TensorType` → 运行时 `TfLiteXxxParams`/`TfLiteType`（数据）」。一个管「谁来算」，一个管「用什么参数算」。

**练习 2**：为什么 `AllocatePOD` 要 `static_assert` 参数结构必须「平凡可析构」？
**答案**：参数所有权会移交 Interpreter 的 C 扩展部分，那里不调析构函数；若参数含需析构成员（如 `std::string`），会泄漏或崩溃，故编译期强制 POD。

**练习 3**：为什么 `ParseOpData` 在 TfLiteMicro 上直接返回错误、要求改用 `ParseAdd` 等单函数？
**答案**：Micro 极度关注二进制体积，不愿链入整张大 `switch`；单独导出每个 `Parse*` 后，Micro 可按模型实际用到的算子做选择性注册，只链入必要的解析函数。

---

## 5. 综合实践

**任务**：端到端跟踪一个含 `ADD`（builtin）算子的 `.tflite` 模型在 `InterpreterBuilder` 里的建图过程；再设想把它换成名为 `"DoubleIt"` 的 custom op，说明分别要在哪几个环节做改动。

**步骤（对照四层源码）**：

1. **模型层（4.1）**：确认 `ADD` 以一条 `OperatorCode{builtin_code=ADD, version=1}` 存于 `operator_codes[]`，每个 `ADD` 算子的 `Operator` 用 `opcode_index` 指向它，参数存为 `builtin_options_as_AddOptions()`。
2. **查表层（4.2）**：跟踪 [interpreter_builder.cc:269-291](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/interpreter_builder.cc#L269-L291)：`GetRegistrationFromOpCode` 命中 builtin 分支，调 `op_resolver.FindOp(BuiltinOperator_ADD, 1)` 取回 ADD 的 `TfLiteRegistration`，存进 `flatbuffer_op_index_to_registration_` 缓存。
3. **注册层（4.3）**：确认这个 ADD 的 registration 由默认 `BuiltinOpResolver`（`MutableOpResolver` 子类）在构造时通过 `AddBuiltin(BuiltinOperator_ADD, Register_ADD(), 1)` 注册进 `builtins_` 表。
4. **参数层（4.4）**：跟踪 [interpreter_builder.cc:392-401](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/interpreter_builder.cc#L392-L401)：因为是 builtin，调 `ParseOpData` → `ParseAdd`，把 `AddOptions` 翻译成 `TfLiteAddParams` 作 `builtin_data`，连同 registration 一起传给 `AddNodeWithParameters`。

**改造为 custom op `"DoubleIt"` 时要改的环节**：

- **模型层**：算子码改成 `OperatorCode{builtin_code=CUSTOM, custom_code="DoubleIt", version=1}`，参数改存为 `custom_options`（裸字节）。
- **查表层**：`GetRegistrationFromOpCode` 改走 custom 分支，调 `FindOp("DoubleIt", 1)`。
- **注册层**：你必须在宿主程序里手写 `resolver.AddCustom("DoubleIt", Register_DoubleIt(), 1)`（见 4.3.4 示例代码），否则查不到、建图失败。
- **参数层**：`InterpreterBuilder` 不再调 `ParseOpData`，而是把 `custom_options` 字节流直接当 `init_data` 透传给你的 `init` 回调（[interpreter_builder.cc:373-391](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/interpreter_builder.cc#L373-L391)）。

**验收标准**：你能对着上面四层源码，分别指出 builtin 与 custom 两条路径在哪一行分叉、各自如何取到 kernel 函数指针、如何取到参数。这就把本讲四个机制（FlatBuffer 格式 / OpResolver / MutableOpResolver / flatbuffer_conversions）串成了一条完整链路。

> **核心问题自测**：能用一句话回答「**TFLite 模型文件里只有名字和参数，凭什么能跑起来？**」——因为 `OpResolver` 提供「名字 → 函数指针」的注册表，`flatbuffer_conversions` 提供「字节 → 运行时参数」的翻译，二者在 `InterpreterBuilder` 里合成为一个可执行的 `TfLiteNode`。

## 6. 本讲小结

- `.tflite` 用 **FlatBuffers** 序列化，根类型 `Model` 含去重的 `operator_codes[]`、`subgraphs`、`buffers`；读取**零拷贝**、可 mmap 按页加载，契合移动端「小内存、只读、快启动」。`Operator` 只存整数 `opcode_index`，算子类型集中在 `OperatorCode`。
- builtin_code 的枚举值可能超过早期单字节容量，`GetBuiltinCode` 用 `std::max` 调和 schema 新旧两个 `builtin_code` 字段；`CUSTOM = 32` 是改用字符串名识别自定义 op 的哨兵。
- **`OpResolver`** 是「(op 码, version) → `TfLiteRegistration*`」的抽象桥，两个 `FindOp` 重载分别按枚举、按字符串查；`GetRegistrationFromOpCode` 是把 schema 对象翻译成查询、再查表的胶水，找不到自定义 op 时不立即报错（留待 delegate/prepare）。
- **`MutableOpResolver`** 用两张哈希表 `builtins_`/`custom_ops_`（值按值存储）+ 链式回退 `other_op_resolvers_` 实现；`AddBuiltin`/`AddCustom` 登记时强制覆写身份字段，保证键与对象身份一致；现代代码推荐 `AddOp`（基于不透明 `TfLiteOperator`）；`BuiltinOpResolver` 是注册了全部内置 op 的开箱即用子类。
- **`flatbuffer_conversions`** 负责 schema → 运行时的数据翻译：`ConvertTensorType` 翻 dtype，每个内置 op 一个 `Parse*` 把 `XxxOptions` 打包成 `TfLiteXxxParams`（POD，经 `AllocatePOD` 分配、`SafeBuiltinDataAllocator` 防泄漏），`ParseOpData` 用大 `switch` 分派。**custom op 不走这条路**，其 `custom_options` 字节流直接透传给 `init` 回调。
- 分工口诀：**`OpResolver` 管「谁来算」（函数指针），`flatbuffer_conversions` 管「用什么参数算」（数据）**；二者在 `InterpreterBuilder`（[interpreter_builder.cc:360-401](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/interpreter_builder.cc#L360-L401)）合成为可执行的 `TfLiteNode`。

## 7. 下一步学习建议

- **承接 u8-l3（TFLite 委托机制 delegates）**：本讲多次提到「自定义 op 找不到时不立即报错，留待 delegate」——下一讲讲 delegate 如何把可加速子图整体卸载到 GPU/NNAPI/XNNPACK，以及失败时如何回退到本讲这套 CPU kernel。本讲埋下的伏笔——`OpResolver` 里那组 `GetDelegateCreators` / `GetOpaqueDelegateCreators` 虚函数（[op_resolver.h:82-120](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/api/op_resolver.h#L82-L120)）正是为它服务的。理解了 `OpResolver`/`TfLiteRegistration`，才能看懂 delegate 如何「替换」一组节点的函数指针。
- **横向对照 u4（Op/Kernel 注册机制）**：桌面端 TF 用 `REGISTER_OP` + `REGISTER_KERNEL_BUILDER` 在启动期自动登记进全局注册表；TFLite 没有这种自动全局注册，而是要求用户显式构造 `OpResolver` 传给 `InterpreterBuilder`——这是为了支持选择性注册、缩小移动端二进制。对照两套机制能加深对「注册」本质的理解。
- **继续阅读源码**：对选择性注册感兴趣，可读 `tensorflow/lite/tools/` 下的生成工具，看它如何据一张模型自动生成只含必要算子的 `MutableOpResolver` 子类；想看可编译的自定义 op 工程模板，可读 `tensorflow/lite/g3doc/guide/ops_custom.md` 与 `tensorflow/examples`。
