# FlatBuffer 模型格式与 OpResolver

> 单元 u8 · 边缘部署 TFLite · 第 2 讲
> 依赖：u8-l1（TFLite 架构与 Interpreter）

## 1. 本讲目标

上一讲（u8-l1）我们建立了 TFLite 的「四件套」推理心智模型：`FlatBufferModel → InterpreterBuilder + OpResolver → Interpreter → AllocateTensors/Invoke`，并知道算子在运行时就是一组 C 函数指针（`TfLiteRegistration`）。但当时我们留下了两个关键问题没有回答：

1. `.tflite` 文件里的算子是怎么「存」下来的？运行时又是怎么把文件里的一条记录，找到对应的 C 函数指针的？
2. 我们自己的算子（builtin 或自定义的 custom op）怎么注册进运行时？

本讲就回答这两个问题。读完本讲，你应当能够：

- 说清 `.tflite` 文件用 FlatBuffer 存储**什么**、为什么这种存储对移动端友好（零拷贝、mmap）；
- 看懂 `OpResolver` 这个「算子查表」抽象，理解它如何把 `OperatorCode` 映射到 `TfLiteRegistration`；
- 掌握 `MutableOpResolver` 的两张哈希表（`builtins_` / `custom_ops_`）与 `AddBuiltin` / `AddCustom` 的注册写法；
- 理解 `flatbuffer_conversions` 如何把算子的配置（builtin_options）翻译成 C 运行时能用的参数结构体；
- 写出为一个自定义 op 注册 kernel 的最小代码。

## 2. 前置知识

在进入源码前，先用三段话补齐基础概念。

**FlatBuffer 是什么。** FlatBuffer 和 Protocol Buffers（protobuf）同属「序列化库」，作用都是把结构化数据变成一段字节流以便存储/传输。但二者有一个根本差别：protobuf 反序列化时需要把整段字节**解析、重建**成一棵内存对象树（要分配、要拷贝）；而 FlatBuffer 的序列化结果**本身**就是可直接访问的数据布局——你拿到一段字节缓冲区后，不需要解析、不需要拷贝，通过代码生成器产出的「访问器方法」就能**直接按偏移量读字段**。这就是所谓的「零拷贝（zero-copy）」。对手机/嵌入式这种内存小、启动要快的设备，这一点非常关键。

**`TfLiteRegistration` 是什么。** 这是 TFLite 里「一个算子的实现」的载体，定义在 [tensorflow/lite/core/c/common.h:1184-1281](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/c/common.h#L1184-L1281)。它的核心是四个函数指针 `init / free / prepare / invoke`（[common.h:1210-1228](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/c/common.h#L1210-L1228)）——分别对应「算子一次性初始化 / 释放 / 输入尺寸变化时重算输出尺寸 / 真正计算」。它还携带 `builtin_code`（[common.h:1244](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/c/common.h#L1244)）、`custom_name`（[common.h:1252](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/c/common.h#L1252)）、`version`（[common.h:1257](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/c/common.h#L1257)）三个身份字段。本讲的主角 `OpResolver`，做的事情就是「**给定一个算子的身份，返回它对应的 `TfLiteRegistration`**」。

**builtin op 与 custom op 的区别。** TFLite 把算子分成两类：「内置算子（builtin）」用一个枚举值标识（如 `BuiltinOperator_ADD`、`BuiltinOperator_CONV_2D`），这些是 TFLite 官方支持、跨平台语义统一的；「自定义算子（custom）」则用一个**字符串名字**标识（如 `"NumericVerify"`、`"AudioSpectrogram"`），通常是用户或特定场景自己写的。这个区分贯穿整条查找链路，请记牢：**builtin 用枚举查、custom 用名字查**。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
|------|------|---------|
| `tensorflow/compiler/mlir/lite/schema/schema.fbs` | `.tflite` 文件的 FlatBuffer 模式定义（数据格式说明书） | 说清「文件里装了什么」 |
| `tensorflow/lite/core/api/op_resolver.h` | `OpResolver` 抽象基类、`GetRegistrationFromOpCode` 声明 | 算子查表的「接口契约」 |
| `tensorflow/lite/core/api/op_resolver.cc` | `GetRegistrationFromOpCode` 实现 | 把文件里的 `OperatorCode` 翻译成一次 `FindOp` 调用 |
| `tensorflow/lite/mutable_op_resolver.h` / `.cc` | `OpResolver` 的可写实现 | 真正存放「身份→注册」映射的两张哈希表 |
| `tensorflow/lite/core/api/flatbuffer_conversions.cc` | 把 FlatBuffer 里的算子配置翻译成 C 参数结构体 | 查到注册后还要「配参数」 |
| `tensorflow/lite/kernels/add.cc` | `Register_ADD()` 等内置 kernel 工厂 | 一个真实 kernel 注册的样例 |
| `tensorflow/lite/kernels/register_ref.cc` | `BuiltinRefOpResolver` 构造函数 | 一口气注册全部内置算子的实战范例 |
| `tensorflow/lite/core/interpreter_builder.cc` | `InterpreterBuilder` 的建图逻辑 | 查表的调用方，把全流程串起来 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. FlatBuffer 模型格式（`.tflite` 里装了什么）
2. `OpResolver` 抽象接口（怎么查表）
3. `MutableOpResolver` 实现（表怎么存、怎么写）
4. `flatbuffer_conversions`（查到之后怎么配参数）

### 4.1 FlatBuffer 模型格式：`.tflite` 文件里装了什么

#### 4.1.1 概念说明

一个 `.tflite` 文件就是一段被 FlatBuffer 序列化后的字节流，它的结构由模式文件 `schema.fbs` 严格规定。要理解 TFLite 怎么找到算子，必须先搞清楚文件里到底存了哪几张「表」。

FlatBuffer 的核心数据结构叫 **table**（类似 protobuf 的 message）。`.tflite` 里最顶层的 table 是 `Model`，它**不是**一张大平面表，而是一个分层的容器：

```
Model
 ├─ operator_codes : [OperatorCode]   ← 全模型唯一的「算子身份清单」
 ├─ subgraphs      : [SubGraph]        ← 计算图（第 0 个是主图）
 │    └─ SubGraph
 │         ├─ tensors   : [Tensor]     ← 张量描述（shape/type/权重索引）
 │         └─ operators : [Operator]   ← 按执行顺序排列的算子
 └─ buffers        : [Buffer]          ← 权重等大块数据
```

这里有一个 TFLite 特意做的设计：**算子的「身份」和「出现」是分离的**。

- `OperatorCode` 表（[schema.fbs:1522-1531](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/lite/schema/schema.fbs#L1522-L1531)）记录的是「这个模型用到了哪些算子类型」。它有三个关键字段：`builtin_code`（内置枚举或 `CUSTOM`）、`custom_code`（自定义算子的名字字符串）、`version`（版本号）。整个模型里所有算子类型**去重后**存在 `Model.operator_codes` 这个数组里。
- `Operator` 表（[schema.fbs:1559-1570](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/lite/schema/schema.fbs#L1559-L1570)）记录的是「这个算子节点具体怎么连」。它**不**直接写自己的类型，而是只存一个 `opcode_index`——一个指向 `operator_codes` 数组的整数下标；外加 `inputs` / `outputs`（输入输出张量的下标）、`builtin_options`（该算子的配置）、`custom_options`（自定义算子的私有字节）。

> 为什么要把身份和出现分离？因为一个模型可能有一百个 `CONV_2D`，但它们共用同一种「算子身份」。把身份抽出来存一份，每个节点只用一个整数引用，既省空间，也方便运行时「**把每个 opcode_index 预先解析成一次 `TfLiteRegistration`，之后所有同类型的节点共用这一个注册对象**」——这正是 4.2 里 `interpreter_builder` 做的优化。

#### 4.1.2 核心流程：运行时如何零拷贝访问模型

```
.tflite 文件（磁盘）
     │  mmap / 读入内存
     ▼
flatbuffers::Verifier 校验完整性（可选）
     │
     ▼
GetModel(buffer)  →  返回 Model*（不拷贝，直接指向 buffer 内部偏移）
     │  通过生成器产出的访问器按需读字段
     ▼
model->operator_codes()  →  const Vector<OperatorCode>*（零拷贝）
model->subgraphs()->Get(0)->operators()->Get(i)  →  Operator*（零拷贝）
op->opcode_index()  →  int（下标）
op->builtin_options_as_AddOptions()  →  AddOptions*（零拷贝，仍是 buffer 内的视图）
```

关键点：**上面这一串调用全程没有为「图结构」分配/拷贝任何大块内存**。所有返回的指针都指向原始 buffer 内的偏移量。这就是「零拷贝」在 TFLite 里的真实形态。

唯一需要「拷贝」的是算子的**配置参数**（`builtin_options`）——因为 C 内核期待的是一个普通的 C 结构体（如 `TfLiteConvParams`），而不是 FlatBuffer 对象。这件事由 4.4 的 `ParseOpData` 负责，它只为「配置」这一小块数据分配内存，权重数据（`buffers`）仍然零拷贝。

#### 4.1.3 源码精读

`Model` 顶层表的字段（[schema.fbs:1697-1708](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/lite/schema/schema.fbs#L1697-L1708)）：这段定义了 `operator_codes`（算子身份清单）与 `subgraphs`（计算图列表）这两个本讲最关心的顶层字段——前者是 4.2 查表的输入来源，后者承载实际算子节点。

`Tensor` 表（[schema.fbs:249-263](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/lite/schema/schema.fbs#L249-L263)）：注意它的 `buffer` 字段是「指向 root 处 `buffers` 表的下标」——这意味着张量的**权重数据**单独存在 `buffers` 数组里，张量描述只持有一个整数引用。这种「描述与数据分离」正是 mmap 零拷贝能成立的根基：运行时让 `buffers` 对应的字节段直接映射进内存，kernel 通过指针读，无需反序列化。

#### 4.1.4 代码实践：观察一个真实 `.tflite` 的 FlatBuffer 结构

1. **实践目标**：直观看到「身份清单」和「算子节点」分离的存储方式，验证零拷贝。
2. **操作步骤**：
   - 在 `tensorflow/lite` 目录下用 `Glob` 搜 `*.tflite` 找任意测试模型，或参照官方文档用 `tf.lite.TFLiteConverter` 转换一个极简 Keras 模型得到 `.tflite`。
   - 安装 `flatbuffers` Python 包并用 schema 生成 Python 绑定：对 `tensorflow/compiler/mlir/lite/schema/schema.fbs` 执行 `flatc --python`，生成 `tflite/` 模块。
   - 写一段 Python：用 `open(...,'rb').read()` 读入文件字节，再 `tflite.Model.GetRootAsModel(buf, 0)` 解析，遍历 `model.OperatorCodesLength()`、`model.Subgraphs(0).OperatorsLength()`，打印每个 operator 的 `OpcodeIndex()`。
3. **需要观察的现象**：`OperatorCodes` 的数量通常**远小于** `Operators` 的数量（例如 100 个 conv 节点，但 `operator_codes` 里可能只有几种类型）。
4. **预期结果**：你会清楚地看到「身份去重存一份、节点用下标引用」的结构，从而理解 4.2 里为什么能「按下标缓存注册」。
5. 如果无法本地生成 schema，明确写「待本地验证」，但源码侧的结构仍可通过阅读 `schema.fbs` 确认无误。

#### 4.1.5 小练习与答案

**练习 1**：为什么不直接在每个 `Operator` 里写完整的算子类型，而要抽出 `OperatorCode` + `opcode_index`？

> **答案**：去重省空间是次要原因；主因是让运行时可以把「每个 opcode_index → `TfLiteRegistration`」预先解析一次并缓存，成百上千个同类节点共用同一个注册对象，避免重复查表。

**练习 2**：FlatBuffer 的「零拷贝」在 TFLite 里到底零拷贝了什么、没零拷贝什么？

> **答案**：零拷贝的是**图结构与权重数据**（`Model/SubGraph/Operator/Tensor` 及 `buffers` 都直接映射原始 buffer）。没有零拷贝的是算子的**配置参数**（`builtin_options`），它由 `ParseOpData` 拷成一个小 C 结构体供 kernel 使用。

---

### 4.2 OpResolver 抽象接口：算子怎么「查表」

#### 4.2.1 概念说明

现在文件读得动了，下一个问题是：**给定一个 `OperatorCode`，怎么找到它对应的 `TfLiteRegistration`（那组 `init/prepare/invoke` 函数指针）？**

答案是引入一个「查表」抽象——`OpResolver`。它的接口极其精简，[op_resolver.h:43-53](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/api/op_resolver.h#L43-L53) 的注释一语道破它的职责：

> 把 flatbuffer 模型里引用到的 op，映射到可执行的函数指针（`TfLiteRegistration`）。

它只要求子类实现两个纯虚 `FindOp`（[op_resolver.h:55-60](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/api/op_resolver.h#L55-L60)），分别对应两类算子：

| 重载 | 入参 | 用途 |
|------|------|------|
| `FindOp(BuiltinOperator op, int version)` | 内置枚举 + 版本 | 查内置算子 |
| `FindOp(const char* op, int version)` | 名字字符串 + 版本 | 查自定义算子 |

注意两个细节：① 入参都带 `version`——同一个算子可以有多个版本（语义演进），`(身份, 版本)` 才是完整键；② 返回的是**指针**，注释特别强调「返回的 `TfLiteRegistration` 的生命周期必须长于用它创建的任何 `InterpreterBuilder`/`Interpreter`」——查表返回的是共享对象的地址，不是拷贝。

#### 4.2.2 核心流程：`GetRegistrationFromOpCode` 桥接文件与查表

`OpResolver::FindOp` 期待的是「枚举或名字」，但文件里给出的是 `OperatorCode` 这张 FlatBuffer 表。把二者衔接起来的，是 [op_resolver.cc:25-66](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/api/op_resolver.cc#L25-L66) 的 `GetRegistrationFromOpCode`。它的逻辑可以画成：

```
读 OperatorCode
   │
   ├─ builtin_code = GetBuiltinCode(opcode)
   │
   ├─ 若 builtin_code > MAX   → 报错「用了新模型的旧二进制？」
   │
   ├─ 若 builtin_code != CUSTOM
   │      → registration = FindOp(builtin_code, version)   // 内置分支
   │
   ├─ 若 == CUSTOM 但无 custom_code  → 报错
   │
   └─ 否则（CUSTOM + 有名字）
          → name = opcode->custom_code()->c_str()
          → registration = FindOp(name, version)            // 自定义分支
```

这就是「builtin 用枚举查、custom 用名字查」这条铁律在代码里的落点。

#### 4.2.3 源码精读

接口契约 `FindOp`（[op_resolver.h:55-60](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/api/op_resolver.h#L55-L60)）：这是整个查表体系的「两个入口」，`OpResolver` 除此之外的方法（`GetDelegateCreators` / `GetOpaqueDelegateCreators`，见 [op_resolver.h:89-120](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/api/op_resolver.h#L89-L120)）都是可选的 delegate 旁路（与 u8-l3 直接相关，本讲先不展开）。理解了这两个 `FindOp`，就理解了 `OpResolver` 的本质。

桥接函数的内置分支（[op_resolver.cc:40-41](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/api/op_resolver.cc#L40-L41)）：`*registration = op_resolver.FindOp(builtin_code, version);`——读出枚举与版本，直接转交给 resolver。

桥接函数的自定义分支（[op_resolver.cc:51-58](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/api/op_resolver.cc#L51-L58)）：先确认 `custom_code` 非空，再 `name = opcode->custom_code()->c_str()` 取出名字字符串，调 `FindOp(name, version)`。

调用方的优化（[interpreter_builder.cc:269-292](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/interpreter_builder.cc#L269-L292)）：`InterpreterBuilder::BuildLocalIndexToRegistrationMapping` 遍历模型的 `operator_codes`，**每个 opcode 只查一次表**，结果存进 `flatbuffer_op_index_to_registration_` 向量。之后图中每个算子节点只靠 `opcode_index` 就能 O(1) 取到注册——这正是 4.1「身份去重」设计带来的红利。注意 CUSTOM 未解析时它不立刻报错，而是塞一个占位 `CreateUnresolvedCustomOp`，留给后续 delegate 机会（[interpreter_builder.cc:277-289](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/interpreter_builder.cc#L277-L289)）。

#### 4.2.4 代码实践：跟踪一次查表决策

1. **实践目标**：亲手在源码里走一遍「一个 builtin op 和一个 custom op 分别走哪条分支」。
2. **操作步骤**：
   - 打开 [op_resolver.cc:25-66](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/api/op_resolver.cc#L25-L66)。
   - 假设模型里有个 `CONV_2D`（builtin）：写下 `GetBuiltinCode` 返回 `BuiltinOperator_CONV_2D`，命中第 40 行分支。
   - 假设模型里有个 `"NumericVerify"`（custom）：写下 `builtin_code == BuiltinOperator_CUSTOM`，命中第 56-58 行分支。
3. **需要观察的现象**：两条分支最终都归结为「调 `FindOp` 的某一个重载」。
4. **预期结果**：你能用一句话概括——`GetRegistrationFromOpCode` 的全部职责就是把 `OperatorCode`「翻译」成一次合适的 `FindOp` 调用。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `FindOp` 的返回类型是 `const TfLiteRegistration*` 而不是 `TfLiteRegistration`？

> **答案**：因为查到的是**共享的注册对象**（往往是个 `static` 变量，见 4.3 的 `Register_ADD`）。返回指针避免拷贝整组函数指针，也让多个 Interpreter 共用同一份注册；代价是注册对象的生命周期必须由「注册方」保证足够长。

**练习 2**：`GetRegistrationFromOpCode` 为什么要对 `builtin_code > BuiltinOperator_MAX` 单独报错（[op_resolver.cc:33-39](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/api/op_resolver.cc#L33-L39)）？

> **答案**：这是向前兼容的护栏。如果模型是用**更新版本**的 TFLite 转换的（枚举值更多），而运行时是**旧二进制**，枚举就会越界。提早报错给出清晰提示「Are you using old TFLite binary with newer model?」，比让它静默查到 `nullptr` 更友好。

---

### 4.3 MutableOpResolver：表怎么存、怎么写

#### 4.3.1 概念说明

`OpResolver` 只给接口，真正的「表」由子类 `MutableOpResolver` 提供（[mutable_op_resolver.h:57-66](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/mutable_op_resolver.h#L57-L66)）。它的注释里给了一段典型用法，本讲就围绕它展开：

```cpp
MutableOpResolver resolver;
resolver.AddBuiltin(BuiltinOperator_ADD, Register_ADD());
resolver.AddCustom("CustomOp", Register_CUSTOM_OP());
InterpreterBuilder(model, resolver)(&interpreter);
```

`MutableOpResolver` 内部用**两张独立的哈希表**分别存两类算子（[mutable_op_resolver.h:146-151](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/mutable_op_resolver.h#L146-L151)）：

| 哈希表 | 键 | 值 |
|--------|-----|-----|
| `builtins_` | `(BuiltinOperator 枚举, version)` | `TfLiteRegistration` |
| `custom_ops_` | `(string 名字, version)` | `TfLiteRegistration` |

为什么要分两张表？因为键的类型不同（枚举 vs 字符串），用两张表各自配合适的哈希函数最直接。这也呼应了 `FindOp` 的两个重载——内置查 `builtins_`，自定义查 `custom_ops_`。

#### 4.3.2 核心流程：注册与查找

**注册**（`AddBuiltin` / `AddCustom`）的共同套路是「拷一份传入的 `TfLiteRegistration`，**回填三个身份字段**，再写入对应的表」：

```
AddBuiltin(op, registration, version):
  new_registration = *registration        // 按值拷贝
  new_registration.custom_name = nullptr  // builtin 没有名字
  new_registration.builtin_code = op      // 回填枚举
  new_registration.version  = version     // 回填版本
  builtins_[(op, version)] = new_registration

AddCustom(name, registration, version):
  new_registration = *registration
  new_registration.builtin_code = BuiltinOperator_CUSTOM
  new_registration.custom_name  = name
  new_registration.version      = version
  custom_ops_[(name, version)]  = new_registration
```

这里有个极易被忽略、却很关键的设计：`common.h` 里 `TfLiteRegistration` 的注释说「`builtin_code` / `custom_name` / `version` 由**注册绑定者**负责正确设置」（[common.h:1242-1257](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/c/common.h#L1242-L1257)）。而看 [add.cc:495-501](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/kernels/add.cc#L495-L501) 的 `Register_ADD()`，它返回的 `TfLiteRegistration` 里这三个字段其实是 **0 / nullptr / 0**（[add.cc:454-456](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/kernels/add.cc#L454-L456)）。也就是说，**kernel 工厂函数不关心自己的身份，身份由 `AddBuiltin` 在注册时回填**。这样同一个 `Register_ADD()` 既能注册成 `BuiltinOperator_ADD`，也方便复用。这个「身份由注册者赋予」的分工，是理解整个注册机制的钥匙。

**查找**（`FindOp`）的实现也很朴素：先查自己的表，查不到就**链式**去问 `other_op_resolvers_` 里挂载的别的 resolver（[mutable_op_resolver.cc:34-39](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/mutable_op_resolver.cc#L34-L39)）。这条链是 `ChainOpResolver`（[mutable_op_resolver.h:113-120](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/mutable_op_resolver.h#L113-L120)）挂上去的，允许你「先用自己的算子，没有再去问底层 resolver」，是组合复用的手段。

#### 4.3.3 源码精读

`AddBuiltin` 的回填逻辑（[mutable_op_resolver.cc:58-79](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/mutable_op_resolver.cc#L58-L79)）：注意第 67-71 行先 `new_registration = *registration` 按值拷贝，再设 `custom_name=nullptr`、`builtin_code=op`、`version=version`，最后 `builtins_[op_key] = new_registration`。第 78 行还把 `may_directly_contain_user_defined_ops_` 置真——用于标记「这个 resolver 可能含 BuiltinOpResolver 之外的算子」，是 Google Play Services 场景的安全判定。

`AddCustom` 的回填逻辑（[mutable_op_resolver.cc:89-99](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/mutable_op_resolver.cc#L89-L99)）：与 builtin 对称，区别是设 `builtin_code = BuiltinOperator_CUSTOM`、`custom_name = name`，写入 `custom_ops_`。

带版本区间的重载（[mutable_op_resolver.h:77-79](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/mutable_op_resolver.h#L77-L79) 与 [mutable_op_resolver.cc:81-87](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/mutable_op_resolver.cc#L81-L87)）：`AddBuiltin(op, reg, min_version, max_version)` 就是在 `[min, max]` 闭区间内对每个 version 各调一次单版本重载。这就是为什么 `BuiltinOpResolver` 能一次性注册一个算子的多个版本。

实战范例（[register_ref.cc:292-294](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/kernels/register_ref.cc#L292-L294)）：`AddBuiltin(BuiltinOperator_ADD, Register_ADD_REF(), 1, 5)`——把 ADD 注册成 1~5 版本。同一个文件里 [register_ref.cc:578-586](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/kernels/register_ref.cc#L578-L586) 则用 `AddCustom` 注册了 `"NumericVerify"` / `"Mfcc"` / `"AudioSpectrogram"` / `"TFLite_Detection_PostProcess"` 四个自定义算子，是「真实模型里 custom op 长什么样」的最佳参照。

#### 4.3.4 代码实践：为一个自定义 op 注册 kernel

> 这是本讲的主实践任务：对照 `op_resolver.h` 与 `mutable_op_resolver.h`，说明如何为一个自定义 op 注册 kernel。

1. **实践目标**：掌握 custom op 注册的最小代码骨架，并解释每一步对应源码里的哪个机制。
2. **操作步骤**：
   - 第一步——**写 kernel**。参照 [add.cc:447-461](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/kernels/add.cc#L447-L461) 的 `Register_ADD_REF()`，写一个返回 `TfLiteRegistration` 的工厂函数，把 `init/free/prepare/invoke` 四个回调接上，**身份三字段留空**（由注册方回填）。下面是一段示例代码（非项目原有，仅示意骨架）：

     ```cpp
     // 示例代码：自定义 op "MyDouble" 的 kernel 工厂
     TfLiteRegistration* Register_MY_DOUBLE() {
       static TfLiteRegistration r = {
           /*init=*/MyInit, /*free=*/MyFree,
           /*prepare=*/MyPrepare, /*invoke=*/MyInvoke,
           /*profiling_string=*/nullptr,
           /*builtin_code=*/0, /*custom_name=*/nullptr,
           /*version=*/0, /*registration_external=*/nullptr,
           /*async_kernel=*/nullptr};
       return &r;
     }
     ```
   - 第二步——**注册到 resolver**。新代码里推荐用 `mutable_op_resolver_utils.h` 的 `AddOp`（[mutable_op_resolver_utils.h:24-26](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/mutable_op_resolver_utils.h#L24-L26)）；传统写法（也是本讲解剖的主线）用 `AddCustom`：

     ```cpp
     // 示例代码：把自定义 op 挂到一个 MutableOpResolver
     tflite::MutableOpResolver resolver;
     resolver.AddCustom("MyDouble", Register_MY_DOUBLE());
     ```
   - 第三步——**对照模型**。你的 `.tflite` 里对应算子的 `OperatorCode` 必须是 `builtin_code = CUSTOM`、`custom_code = "MyDouble"`，这样 [op_resolver.cc:57-58](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/api/op_resolver.cc#L57-L58) 才会用名字 `"MyDouble"` 命中你注册的那一项。
3. **需要观察的现象**：注册时名字字符串、查表时名字字符串、模型里 `custom_code` 三者必须**完全一致**；任何一个不匹配，`FindOp` 返回 `nullptr`，运行时报「Didn't find op」。
4. **预期结果**：你能说清——注册 custom op = 「写一个填好四个回调的 `TfLiteRegistration` 工厂 + 用 `AddCustom(name, factory())` 把它写进 `custom_ops_` 表」，仅此而已。
5. 如果不在本地编译 TFLite，可只完成「源码阅读」部分：在 [register_ref.cc](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/kernels/register_ref.cc) 找到一个 `AddCustom` 调用，确认其名字与某个 `Register_*` 工厂、与模型里 `custom_code` 三方对应。无法本地编译时明确标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`Register_ADD()` 返回的 `TfLiteRegistration` 里 `builtin_code` 是 0，但运行时查 ADD 却能正确识别它是 `BuiltinOperator_ADD`，为什么？

> **答案**：因为身份在 `AddBuiltin` 注册时被回填（[mutable_op_resolver.cc:69](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/mutable_op_resolver.cc#L69)）。kernel 工厂只管「怎么算」，身份由注册者负责赋予，二者解耦。

**练习 2**：`AddBuiltin(op, reg, 1, 5)` 之后，`builtins_` 表里多了几条记录？

> **答案**：5 条，键分别是 `(op,1)…(op,5)`。带区间的重载本质是循环调单版本重载（[mutable_op_resolver.cc:81-87](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/mutable_op_resolver.cc#L81-L87)）。

**练习 3**：`FindOp` 在自己的表里没找到时会发生什么？

> **答案**：会遍历 `other_op_resolvers_` 链上的其它 resolver 继续找（[mutable_op_resolver.cc:34-39](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/mutable_op_resolver.cc#L34-L39)）；这条链由 `ChainOpResolver` 挂上，实现 resolver 的组合复用。

---

### 4.4 flatbuffer_conversions：查到之后怎么「配参数」

#### 4.4.1 概念说明

到目前为止，`FindOp` 返回的 `TfLiteRegistration` 只告诉我们「这个算子用哪几个 C 函数」，但**没有**告诉算子「你的具体配置是什么」——比如一个 `CONV_2D` 究竟是 SAME 还是 VALID padding、stride 多少、有没有 fused activation。这些配置在文件里以 FlatBuffer 的 `builtin_options` 形式存在（[schema.fbs:1568](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/lite/schema/schema.fbs#L1568)），但 C kernel 期待的是一个普通 C 结构体（如 `TfLiteConvParams`）。

填补这道鸿沟的，就是 [flatbuffer_conversions.cc](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/api/flatbuffer_conversions.cc)。它做三件事：

1. 把 FlatBuffer 里的枚举（padding、activation、TensorType 等）翻译成运行时枚举（`ConvertPadding` / `ConvertActivation` / `ConvertTensorType`）；
2. 为每种算子分配一个运行时参数结构体，把 `builtin_options` 里的字段逐一填进去（`ParseXxx` 家族）；
3. 用一个大 `switch` 按 `BuiltinOperator` 枚举分派到对应的 `ParseXxx`（`ParseOpDataTfLite`）。

注意：这一步是**会分配小内存**的（为参数结构体分配），这是 4.1.2 里说的「唯一需要拷贝」的部分。它拷的只是配置（几十字节），不是权重（几 MB）。

#### 4.4.2 核心流程：ParseOpDataTfLite 的分派

```
拿到 Operator* op 和它的 BuiltinOperator op_type
        │
        ▼
ParseOpDataTfLite(op, op_type, ...)   // flatbuffer_conversions.cc:163
        │  switch(op_type)
        ├─ case ADD      → ParseAdd(op, ...)       // 填 TfLiteAddParams
        ├─ case CONV_2D  → ParseConv2D(op, ...)    // 填 TfLiteConvParams
        ├─ case MUL      → ParseMul(op, ...)
        ├─ case SLICE    → return kTfLiteOk        // 无参数结构体
        ├─ case CUSTOM   → return kTfLiteOk        // custom 的参数另走 custom_options
        └─ ...
```

每个 `ParseXxx` 的写法高度一致，以 `ParseAdd` 为例（[flatbuffer_conversions.cc:1126-1149](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/api/flatbuffer_conversions.cc#L1126-L1149)）：① 用 `SafeBuiltinDataAllocator` 分配一个 `TfLiteAddParams`（RAII，失败自动回收，见 [flatbuffer_conversions.cc:40-67](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/api/flatbuffer_conversions.cc#L40-L67)）；② 用 `op->builtin_options_as_AddOptions()` 拿到 FlatBuffer 视图（零拷贝）；③ 把字段（fused activation、pot_scale_int16）翻译后填进 params；④ `*builtin_data = params.release()` 交还裸指针给运行时。

custom op 不走这套——它们的参数是 `Operator.custom_options`（一段任意字节，[schema.fbs:1569](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/lite/schema/schema.fbs#L1569)），由 kernel 的 `init` 回调自己解释（这正是 `TfLiteRegistration::init` 的 `buffer/length` 形参的用途，见 [common.h:1185-1210](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/c/common.h#L1185-L1210)）。所以 `ParseOpDataTfLite` 里 `case BuiltinOperator_CUSTOM` 直接 `return kTfLiteOk`（[flatbuffer_conversions.cc:997](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/api/flatbuffer_conversions.cc#L997)）——它无事可做。

#### 4.4.3 源码精读

分派中枢（[flatbuffer_conversions.cc:163-198](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/api/flatbuffer_conversions.cc#L163-L198)）：`ParseOpDataTfLite` 的开头，`switch (op_type)` 按 `BuiltinOperator` 把控制权交给各 `ParseXxx`。注意 `ADD`（第 196-198 行）转给 `ParseAdd`，而很多「无参数」算子（SLICE、EQUAL、TRANSPOSE 等）则集中 fallthrough 到第 1029 行的 `return kTfLiteOk`。

`ParseConv2D`（[flatbuffer_conversions.cc:1344-1376](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/api/flatbuffer_conversions.cc#L1344-L1376)）：填一个比 ADD 复杂得多的 `TfLiteConvParams`——padding（经 `ConvertPadding`）、stride、dilation、fused activation、quantized_bias_type（经 `ConvertTensorType`）。这段是「builtin_options → C 结构体」的标准范例。

类型翻译表（[flatbuffer_conversions.cc:1038-1116](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/api/flatbuffer_conversions.cc#L1038-L1116)）：`ConvertTensorType` 把 schema 的 `TensorType_FLOAT32`、`TensorType_INT8` 等枚举，一对一翻成运行时的 `kTfLiteFloat32`、`kTfLiteInt8`。两个枚举值可能不同，所以不能直接强转，必须查表。

安全分配器（[flatbuffer_conversions.cc:40-67](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/api/flatbuffer_conversions.cc#L40-L67)）：`SafeBuiltinDataAllocator` 用 `unique_ptr` + 自定义 deleter 包装 `BuiltinDataAllocator`，保证「分配了 params 但因模型非法没成功写到 `builtin_data`」时不泄漏——这是工业级代码里很值得学习的健壮性细节。

#### 4.4.4 代码实践：跟踪 Conv2D 的参数翻译

1. **实践目标**：把「文件里的 conv 配置 → C 参数结构体」这一步在源码里走通。
2. **操作步骤**：
   - 打开 [flatbuffer_conversions.cc:1344-1376](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/api/flatbuffer_conversions.cc#L1344-L1376)。
   - 假设模型里一个 conv 的 `Conv2DOptions` 是 `padding=SAME, stride_w=2, stride_h=2, fused_activation=RELU`。
   - 逐行标注：`ConvertPadding(SAME)` → `kTfLitePaddingSame`；`stride_w()` → `params->stride_width=2`；`ConvertActivation(RELU)` → `params->activation=kTfLiteActRelu`。
3. **需要观察的现象**：每个 FlatBuffer 字段都要经一个 `ConvertXxx` 或直接赋值，才能落到 C 结构体。
4. **预期结果**：你会得出结论——`flatbuffer_conversions` 的本质就是「**为每种算子写一个适配函数，把 schema 视图转成 C 结构体**」，没有计算，只有搬运与枚举翻译。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `case BuiltinOperator_CUSTOM` 在 `ParseOpDataTfLite` 里直接返回 `kTfLiteOk`？

> **答案**：因为自定义算子的参数不是 `builtin_options`，而是 `custom_options`（一段私有字节，见 [schema.fbs:1569](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/lite/schema/schema.fbs#L1569)），由 kernel 自己的 `init` 回调解释。`ParseOpDataTfLite` 只管 builtin，所以无事可做。

**练习 2**：`ConvertTensorType` 为什么不直接 `static_cast`？

> **答案**：schema 的 `TensorType` 枚举与运行时 `TfLiteType` 枚举是**两套独立编号**（例如 `TensorType_INT4` 和 `kTfLiteInt4` 的整数值未必相同），必须显式查表映射，强转会错位。

## 5. 综合实践

把四个模块串成一条完整链路。请用文字（必要时配伪代码）回答：

> **一个 `CONV_2D` 算子节点，从 `.tflite` 文件到被 `Interpreter::Invoke()` 真正执行，中间经历了哪些与本讲相关的步骤？**

预期你能按顺序讲清下面每一环，并给出对应的源码位置：

1. **读文件**（4.1）：`.tflite` 被 mmap，`GetModel()` 零拷贝得到 `Model*`；该节点是 `subgraphs[0].operators[i]`，其 `opcode_index` 指向 `operator_codes[k]`，后者 `builtin_code = CONV_2D`。
2. **建索引**（4.2）：[interpreter_builder.cc:269-292](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/interpreter_builder.cc#L269-L292) 对 `operator_codes[k]` 调 `GetRegistrationFromOpCode`（[op_resolver.cc:40-41](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/api/op_resolver.cc#L40-L41)），命中 builtin 分支。
3. **查表**（4.3）：`FindOp(CONV_2D, version)` 在 `MutableOpResolver::builtins_` 中找到注册（[mutable_op_resolver.cc:28-41](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/mutable_op_resolver.cc#L28-L41)）；这个注册是当初 `BuiltinRefOpResolver` 构造时用 `AddBuiltin(BuiltinOperator_CONV_2D, Register_CONVOLUTION_REF(), 1, 8)` 写进去的（参见 [register_ref.cc:255-257](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/kernels/register_ref.cc#L255-L257)）。
4. **配参数**（4.4）：用 `ParseConv2D`（[flatbuffer_conversions.cc:1344-1376](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/api/flatbuffer_conversions.cc#L1344-L1376)）把 `Conv2DOptions` 翻成 `TfLiteConvParams`，连同注册一起挂到节点。
5. **执行**：`Invoke` 时按 `execution_plan` 取到该节点的 `TfLiteRegistration`，调它的 `prepare`/`invoke`——这就回到了 u8-l1 讲的四件套。

**进阶（可选）**：再为一条「自定义 op `MyDouble`」走一遍同样的链路，对比两处不同——查表走 `FindOp(const char*, version)` 的 custom 分支、配参数不走 `ParseOpDataTfLite` 而由 kernel `init` 自解释 `custom_options`。

## 6. 本讲小结

- `.tflite` 用 FlatBuffer 存储，**图结构与权重零拷贝**（直接 mmap），只有算子配置这几十字节需要被 `ParseOpData` 拷成 C 结构体；`OperatorCode`（身份，去重）与 `Operator`（节点，用 `opcode_index` 引用身份）是分离设计。
- `OpResolver` 是「算子查表」抽象，核心是两个 `FindOp` 重载——**builtin 用枚举查、custom 用名字查**，且都带 `version`；`GetRegistrationFromOpCode` 把文件里的 `OperatorCode` 翻译成一次合适的 `FindOp` 调用。
- `MutableOpResolver` 用两张哈希表 `builtins_` / `custom_ops_` 落地，键都是 `(身份, version)`；`AddBuiltin` / `AddCustom` 在写入前会**回填** `builtin_code / custom_name / version` 三个身份字段——所以 kernel 工厂（如 `Register_ADD`）本身不必关心身份。
- `InterpreterBuilder` 对每个 `operator_codes` 只查一次表并缓存成「下标→注册」向量，图里成百上千个同类节点共用同一份注册。
- `flatbuffer_conversions` 负责「builtin_options → C 参数结构体」的翻译，用 `ParseOpDataTfLite` 大 switch 分派；custom op 不走这条路，其参数由 kernel 自己的 `init` 解释 `custom_options`。
- 注册一个自定义 op 的最小步骤：写一个填好 `init/free/prepare/invoke` 的 `TfLiteRegistration` 工厂 → 用 `AddCustom(name, factory())` 写进 resolver → 确保模型里 `custom_code` 名字三者一致。

## 7. 下一步学习建议

- **本单元下一讲 u8-l3（TFLite 委托机制 delegates）** 将在本讲基础上展开：delegate 提供了 `FindOp` 之外的**第二条**算子解析路径（`GetDelegateCreators` / `GetOpaqueDelegateCreators`，见 [op_resolver.h:89-120](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/api/op_resolver.h#L89-L120)），把可加速子图从默认 kernel 路径「拐走」交给 GPU/NNAPI/XNNPACK。理解了本讲的注册/查表，再看 delegate 的图分区会非常自然。
- **建议继续阅读的源码**：
  - `tensorflow/lite/kernels/register.cc`（`BuiltinOpResolver`，与 `register_ref.cc` 对照，看「优化版」与「参考版」两套注册的差异）；
  - `tensorflow/lite/mutable_op_resolver_utils.cc`（新版推荐的 `AddOp` 实现，理解它如何基于 `TfLiteOperator` 这个 ABI 稳定类型注册）；
  - `tensorflow/lite/core/api/op_resolver_internal.h`（`MayContainUserDefinedOps` 等「内部口」，理解 Google Play Services 场景的安全判定）。
- 若想横向对照，可回顾 u4-l1（TF 的 `REGISTER_OP` / `OpRegistry`）：桌面端 TF 用「C++ 静态全局变量自动注册」，而 TFLite 出于二进制体积考量，改用「显式调用 `AddBuiltin`/`AddCustom`」——这是同一类问题在两种部署形态下的不同取舍。
