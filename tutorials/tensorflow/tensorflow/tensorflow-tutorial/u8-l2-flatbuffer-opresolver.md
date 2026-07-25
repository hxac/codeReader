# FlatBuffer 模型格式与 OpResolver

> 依赖前置讲义：[u8-l1 TFLite 架构与 Interpreter](u8-l1-tflite-architecture.md)（本讲假定你已了解 TFLite 的五步流水线、`FlatBufferModel` 的零拷贝 mmap，以及 `TfLiteRegistration` 的 init/prepare/invoke 四回调）。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清楚一个 `.tflite` 文件里的算子是以什么数据结构、什么方式被存储与读取的，并解释 FlatBuffer 为何适合移动端。
2. 解释 `OpResolver` 这层抽象的作用：它是「模型里的算子码」与「真正能执行的函数指针集合」之间的桥梁。
3. 读懂 `MutableOpResolver` 的注册表实现，知道 `AddBuiltin` / `AddCustom` 内部到底做了什么，并据此为一个**自定义 op** 注册 kernel。
4. 理解 `flatbuffer_conversions` 的职责：把 builtin 算子在 FlatBuffer 里的「选项（Options）」翻译成运行时消费的 C 参数结构（`builtin_data`）。
5. 把三者串成一条完整的建图链路：模型 → opcode → registration → `builtin_data` / `init_data` → Subgraph 节点。

## 2. 前置知识

- **FlatBuffers 是什么**：和 Protocol Buffers 同属「二进制序列化」方案，但有一个关键区别——FlatBuffers **不需要反序列化（unpack）**。数据在文件里是什么内存布局，读进内存后就是什么布局，访问字段时按偏移量直接读取。这正是 TFLite 「零拷贝 mmap」的基础：把 `.tflite` 文件 mmap 进内存后，无需拷贝解析就能直接读出算子、张量、权重。
- **builtin op 与 custom op**：TFLite 把算子分成两类。**builtin（内建）** 算子有一个固定的枚举码，如 `BuiltinOperator_ADD`、`BuiltinOperator_CONV_2D`，参数也用固定的 schema（如 `AddOptions`、`Conv2DOptions`）存储。**custom（自定义）** 算子没有枚举码，靠字符串名字（如 `"AWESOME"`）标识，参数是用户自定义的字节流（`custom_options`）。
- **函数指针集合 `TfLiteRegistration`**：在 u8-l1 已经讲过，TFLite 的每个算子 kernel 是一组 C 函数指针（`init`/`free`/`prepare`/`invoke`）。本讲要解决的核心问题是：**模型里只存了「算子码」或「算子名字」，运行时怎么找到对应的那组函数指针？** 答案就是 `OpResolver`。
- **opcode（算子码）与 version（版本）**：同一个算子可能有多个版本（v1/v2），版本不同对应不同的 kernel 实现，因此「算子码 + 版本」才是一组函数指针的唯一键。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tensorflow/lite/core/api/op_resolver.h](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/api/op_resolver.h) | 定义 `OpResolver` 抽象基类、`FindOp` 契约，以及「从 opcode 取 registration」的工具函数 `GetRegistrationFromOpCode` 的声明。 |
| [tensorflow/lite/core/api/op_resolver.cc](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/api/op_resolver.cc) | 实现 `GetRegistrationFromOpCode`：把 FlatBuffer 里的 `OperatorCode` 翻译成一次 `FindOp` 调用。 |
| [tensorflow/lite/mutable_op_resolver.h](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/mutable_op_resolver.h) | `OpResolver` 的「可变」实现，声明 `AddBuiltin`/`AddCustom` 等注册 API 与内部两张哈希表。 |
| [tensorflow/lite/mutable_op_resolver.cc](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/mutable_op_resolver.cc) | 实现 `FindOp`、`AddBuiltin`、`AddCustom`，是注册表的真正逻辑所在。 |
| [tensorflow/lite/core/api/flatbuffer_conversions.h](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/api/flatbuffer_conversions.h) / [.cc](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/api/flatbuffer_conversions.cc) | 把 builtin 算子的 FlatBuffer Options 翻译成运行时 C 参数结构（`builtin_data`），并提供 `ConvertTensorType`、`ParseOpData` 等。 |
| [tensorflow/lite/core/c/common.h](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/c/common.h) | 定义 `TfLiteRegistration` 结构体（被注册、被查找的对象）。 |
| [tensorflow/lite/core/interpreter_builder.cc](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/interpreter_builder.cc) | 把模型、OpResolver、flatbuffer_conversions 三者接起来的「建图总调度」。 |

---

## 4. 核心概念与源码讲解

> 三者关系一张图：
>
> ```
>   .tflite 文件（FlatBuffer）
>        │  operator_codes[] 表  +  operators[] 列表
>        ▼
>   InterpreterBuilder 遍历 operators
>        │  每个 op 持有 opcode_index 指向 operator_codes 表
>        ▼
>   GetRegistrationFromOpCode(opcode, op_resolver)     ← 第 4.2 节
>        │  builtin? → FindOp(enum, version)
>        │  custom?  → FindOp(name, version)
>        ▼
>   TfLiteRegistration*（init/prepare/invoke 函数指针）  ← 第 4.3 节
>        │
>        ├── 若 builtin：ParseOpData(op, ...)           ← 第 4.4 节
>        │      把 Options 翻译成 builtin_data（C 结构）
>        │
>        └── 若 custom：直接把 custom_options 字节流当作 init_data
>        ▼
>   subgraph->AddNodeWithParameters(..., registration)
> ```

### 4.1 FlatBuffer 模型格式：算子的存储与零拷贝

#### 4.1.1 概念说明

u8-l1 已经讲过 `FlatBufferModel` 用 mmap 零拷贝加载模型。本节要回答的是更细一层的问题：**加载之后，算子在内存里到底长什么样，运行时怎么读它？**

TFLite 的模型 schema（由 FlatBuffer IDL 生成 `schema_generated.h`）大致是如下嵌套结构：

```
Model
├── operator_codes[]   ← 一张「算子码表」，去重后的算子定义
│     每个 OperatorCode = { builtin_code, version, custom_code }
├── subgraphs[]
│     每个 SubGraph = { tensors[], inputs[], outputs[], operators[] }
│           每个 Operator = { opcode_index, inputs[], outputs[],
│                              intermediates[], builtin_options | custom_options }
└── buffers[]          ← 张量权重数据（零拷贝指向 mmap 区）
```

两个关键设计：

1. **算子码表 `operator_codes[]` 是去重的**。模型里若有 100 个 `ADD` 算子，`operator_codes[]` 里只存 1 条 `ADD` 的定义，100 个 `Operator` 各自用 `opcode_index`（一个整数）指向它。这正是 `InterpreterBuilder` 要先遍历 `operator_codes[]` 建立「index → registration」缓存的原因。
2. **算子参数分两套存储**。builtin 算子的参数用强类型的 `builtin_options`（如 `Conv2DOptions`，含 stride、padding 等）；custom 算子的参数用一段裸字节 `custom_options`（语义由自定义 op 自己解释）。

#### 4.1.2 核心流程

模型加载与读取的流程：

1. `FlatBufferModel::BuildFromFile` 把文件 mmap 进内存，得到一个指向 `Model` 根节点的指针，**全程不反序列化**。
2. 任意时刻要读某个算子时，通过 FlatBuffer 生成的访问器（如 `op->opcode_index()`、`opcode->version()`、`op->builtin_options_as_Conv2DOptions()`）按偏移量现场读取，返回的是指向 mmap 区的指针，不是拷贝。
3. 由于 builtin_code 的枚举值可能超过 127（早期 schema 用单字节存），TFLite 提供安全访问器 `GetBuiltinCode(opcode)` 统一处理新老格式。

#### 4.1.3 源码精读

`GetRegistrationFromOpCode` 里这两行就是「从 FlatBuffer 读算子身份」的典型写法——`GetBuiltinCode` 取算子码，`opcode->version()` 取版本，`opcode->custom_code()` 取自定义名字：

```cpp
auto builtin_code = GetBuiltinCode(opcode);
int version = opcode->version();
```

完整逻辑见 [tensorflow/lite/core/api/op_resolver.cc:25-66](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/api/op_resolver.cc#L25-L66) —— 这段代码是本讲的「中心枢纽」，我们在 4.2 节展开。

而 `InterpreterBuilder` 里取 `operator_codes` 表、并按 `opcode_index` 去查 registration 的循环，见 [tensorflow/lite/core/interpreter_builder.cc:258-292](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/interpreter_builder.cc#L258-L292)，其中 `model_->operator_codes()` 就是 FlatBuffer 访问器返回的去重算子码表。

> 为什么 FlatBuffer 适合移动端？一句话：**零拷贝、低内存、快启动**。传统序列化要把整个文件解析成对象树，内存翻倍且启动慢；FlatBuffers 直接在文件字节上按偏移读，模型可以大于可用内存（mmap 按需分页），冷启动几乎无需解析时间，这对手机、嵌入式这类「内存与电量都紧张」的设备至关重要。

#### 4.1.4 代码实践

**实践目标**：亲眼看到一个 `.tflite` 文件里算子是如何以 `operator_codes[]` + `operators[]` 的方式组织的。

**操作步骤（源码阅读型，无需编译）**：

1. 在仓库里找到 schema 的可读定义：`tensorflow/lite/schema/schema.fbs`（FlatBuffer IDL 文件），阅读其中 `table Model`、`table SubGraph`、`table Operator`、`table OperatorCode` 的字段定义。
2. 对照阅读 `tensorflow/lite/core/api/flatbuffer_conversions.cc` 中任意一个 `ParseXxx` 函数（例如 4.4 节将精读的 `ParseConv2D`），观察它如何用 `op->builtin_options_as_Conv2DOptions()` 这种 FlatBuffer 访问器现场读取参数。
3. 在 `interpreter_builder.cc` 中确认 `op->opcode_index()`（指向算子码表的下标）与 `op->custom_options()`（自定义算子的字节流）这两个字段的用法。

**需要观察的现象**：`schema.fbs` 里 `Operator` 表并没有直接存「算子类型」，而是只存了一个 `opcode_index` 整数；算子类型信息集中在 `OperatorCode` 表里。

**预期结果**：你能用自己的话回答「为什么 TFLite 要把算子类型抽成一张去重的 `operator_codes[]` 表，而不是每个算子各存一份类型字符串」。

> ⚠️ 本实践为源码阅读型，不涉及运行命令；若你想真正打开一个 `.tflite` 文件查看其 FlatBuffer 内容，可使用 TFLite 自带的 `tensorflow/lite/tools/visualize.py`（`python -m tensorflow.lite.tools.visualize model.tflize`），具体输出以本地运行为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`Operator` 表里的 `opcode_index` 指向 `Model.operator_codes[]`。如果模型里有 3 个 `CONV_2D`、2 个 `ADD`，`operator_codes[]` 表里通常有几条记录？

> **答案**：2 条（一条 `CONV_2D`、一条 `ADD`）。算子码表是去重的，多个同类算子共用同一条 `OperatorCode`。若某算子有不同 version，则每个 version 各占一条。

**练习 2**：为什么读取算子参数用 `op->builtin_options_as_Conv2DOptions()` 这样的访问器，而不是先把整个模型反序列化成一个 C++ 对象树？

> **答案**：FlatBuffers 的核心特性就是「不反序列化」。访问器按偏移量现场读字段、返回指向 mmap 区的指针，既省内存（不复制）又省启动时间（不解析），这对移动端至关重要。

---

### 4.2 OpResolver 与 FindOp：从 opcode 到函数指针

#### 4.2.1 概念说明

模型里只存了「算子码 + 版本」（builtin）或「算子名字 + 版本」（custom），而真正能干活的是一组 `init/prepare/invoke` 函数指针（`TfLiteRegistration`）。这两者之间的「查表翻译」就是 `OpResolver` 的职责。

`OpResolver` 是一个**抽象基类**，只规定契约，不规定实现。它只要求子类实现两个纯虚的 `FindOp`：

- 按 builtin 枚举码查：`FindOp(BuiltinOperator op, int version)`
- 按 custom 名字查：`FindOp(const char* op, int version)`

为什么把「查表」单独抽成一层接口？因为同一份模型可能跑在不同的运行时上——完整版 TFLite、精简版 TFLite Micro、Google Play Services 里的 TFLite——它们各自能提供的 kernel 集合不同。模型不变，换一个 `OpResolver` 实现就能换一套底层 kernel。这正是依赖倒置：**模型依赖抽象的 OpResolver，而不是具体的 kernel 注册表**。

#### 4.2.2 核心流程

从 FlatBuffer 的 `OperatorCode` 取到 `TfLiteRegistration` 的完整判定流程（实现在 `GetRegistrationFromOpCode`）：

```
输入：OperatorCode* opcode
  │
  ├─ builtin_code = GetBuiltinCode(opcode)
  ├─ version     = opcode->version()
  │
  ├─ 若 builtin_code 超出枚举范围 → 报错（"用旧 binary 跑新模型？"）
  │
  ├─ 若 builtin_code != CUSTOM：
  │     registration = op_resolver.FindOp(builtin_code, version)
  │     找不到 → 报错（"Didn't find op for builtin opcode ..."）
  │
  ├─ 若是 CUSTOM 但 custom_code 为空 → 报错
  │
  └─ 否则（合法 custom）：
        name = opcode->custom_code()
        registration = op_resolver.FindOp(name, version)
        找不到 → 返回错误（但延迟到 prepare 阶段最终判定，可能被 delegate 解析）
```

注意最后一处细节：custom op 找不到时，`GetRegistrationFromOpCode` **不立即报致命错误**（代码注释明确写道 "Do not report error for unresolved custom op, we do the final check while preparing ops"）。因为某些 custom op 可能稍后由 delegate（如 Flex）接管。`InterpreterBuilder` 据此对未解析的 custom op 临时塞入一个 `CreateUnresolvedCustomOp` 占位（见 [interpreter_builder.cc:277-289](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/interpreter_builder.cc#L277-L289)）。

#### 4.2.3 源码精读

**① 抽象基类与 FindOp 契约**：[tensorflow/lite/core/api/op_resolver.h:53-61](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/api/op_resolver.h#L53-L61)

```cpp
class OpResolver {
 public:
  /// Finds the op registration for a builtin operator by enum code.
  virtual const TfLiteRegistration* FindOp(tflite::BuiltinOperator op,
                                           int version) const = 0;
  /// Finds the op registration of a custom operator by op name.
  virtual const TfLiteRegistration* FindOp(const char* op,
                                           int version) const = 0;
```

这就是 `OpResolver` 的全部核心契约——两个纯虚 `FindOp`。注释里那句话点明了它的本质：*"the mechanism that ops being referenced in the flatbuffer model are mapped to executable function pointers"*。

**② FindOp 返回的对象 `TfLiteRegistration`**：[tensorflow/lite/core/c/common.h:1184-1281](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/c/common.h#L1184-L1281)

被查到的 `TfLiteRegistration` 结构体的核心字段（节选）：

```cpp
typedef struct TfLiteRegistration {
  void* (*init)(TfLiteContext* context, const char* buffer, size_t length);  // L1210
  void (*free)(TfLiteContext* context, void* buffer);
  TfLiteStatus (*prepare)(TfLiteContext* context, TfLiteNode* node);         // L1222
  TfLiteStatus (*invoke)(TfLiteContext* context, TfLiteNode* node);          // L1228
  // ...
  int32_t builtin_code;     // builtin 码；custom 时为 BuiltinOperator_CUSTOM
  const char* custom_name;  // custom 名字；builtin 时为 null
  int version;              // 算子版本
  // ...
} TfLiteRegistration;
```

记住三个身份字段 `builtin_code` / `custom_name` / `version`——它们是 `FindOp` 的查表键，也是 4.3 节 `AddBuiltin`/`AddCustom` 要回填的字段。

**③ 查表的实现 `GetRegistrationFromOpCode`**：[tensorflow/lite/core/api/op_resolver.cc:25-66](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/api/op_resolver.cc#L25-L66)

关键三分支节选：

```cpp
if (builtin_code > BuiltinOperator_MAX) {
  // 码超范围：很可能用旧 binary 跑新模型
  status = kTfLiteError;
} else if (builtin_code != BuiltinOperator_CUSTOM) {
  *registration = op_resolver.FindOp(builtin_code, version);   // builtin 路径
} else if (!opcode->custom_code()) {
  status = kTfLiteError;                                        // custom 无名字
} else {
  const char* name = opcode->custom_code()->c_str();
  *registration = op_resolver.FindOp(name, version);            // custom 路径
}
```

#### 4.2.4 代码实践

**实践目标**：跟踪一次「算子码 → registration」的查找，验证 builtin 与 custom 两条路径的分叉点。

**操作步骤（源码阅读型）**：

1. 打开 [op_resolver.cc:25-66](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/api/op_resolver.cc#L25-L66)。
2. 假设模型里有一个 `ADD`（builtin）算子，version=1：在脑中走一遍，确认它命中第 40-41 行的 `FindOp(builtin_code, version)`。
3. 假设模型里有一个名为 `"AWESOME"` 的 custom 算子，version=1：确认它命中第 57-58 行的 `FindOp(name, version)`。
4. 打开 [interpreter_builder.cc:269-291](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/interpreter_builder.cc#L269-L291)，确认 `GetRegistrationFromOpCode` 的返回值被存进 `flatbuffer_op_index_to_registration_` 这个「下标→registration」缓存向量。

**需要观察的现象**：`InterpreterBuilder` 把对 `operator_codes[]` 表的解析结果缓存进一个 `vector<const TfLiteRegistration*>`，后续每个 `Operator` 只需用 `opcode_index` 做下标取出，避免对同一个算子码重复查表。

**预期结果**：你能解释为什么 `InterpreterBuilder` 要把「index→registration」的映射单独建一个缓存，而不是每遇到一个算子就调一次 `GetRegistrationFromOpCode`。（答案：因为算子码表是去重的，一次解析、按下标复用，省掉大量字符串查找。）

#### 4.2.5 小练习与答案

**练习 1**：`OpResolver` 为什么设计成抽象基类，而不是一个具体类？

> **答案**：为了让「模型」与「具体 kernel 集合」解耦。同一份模型可以搭配不同的 `OpResolver` 实现（如完整版、Micro 精简版、Play Services 版），各自提供能跑的 kernel 子集。模型只依赖抽象接口，遵循依赖倒置原则。

**练习 2**：一个 custom op 在 `GetRegistrationFromOpCode` 里查不到时，为什么不立即报致命错误？

> **答案**：因为它可能稍后被某个 delegate（如 Flex delegate，用于跑 TF 原生 op）接管。运行时先临时塞入一个未解析占位 op，把最终判定推迟到 prepare 阶段。

---

### 4.3 MutableOpResolver：注册表实现与自定义 op 注册

#### 4.3.1 概念说明

`OpResolver` 只给了契约，`MutableOpResolver` 是它最常用的具体实现，也是用户注册自定义 op 时直接打交道的类。它内部维护**两张哈希表**：

- `builtins_`：键是 `(BuiltinOperator 枚举, version)`，值是 `TfLiteRegistration`。
- `custom_ops_`：键是 `(算子名字 string, version)`，值是 `TfLiteRegistration`。

之所以分两张表，是因为 builtin 用整数枚举查、custom 用字符串查，键类型不同。两者统一通过 `version` 共同组成复合键，因此同一个算子的 v1 与 v2 可以各自注册不同的 kernel。

`MutableOpResolver` 对外暴露三组核心 API：

- `AddBuiltin(op, registration, version)`：注册一个 builtin 算子的某个版本。
- `AddCustom(name, registration, version)`：注册一个 custom 算子的某个版本。
- `FindOp(...)`（继承自 `OpResolver`）：查表，先查自己，再查通过 `ChainOpResolver` 链接进来的其它 resolver。

> 提示：`mutable_op_resolver.h` 的注释里直接给出了典型用法（注册 ADD + 自定义 op，再用 `InterpreterBuilder` 建图），见 [tensorflow/lite/mutable_op_resolver.h:57-62](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/mutable_op_resolver.h#L57-L62)。对新代码，官方更推荐 `mutable_op_resolver_utils.h` 里的 `AddOp`（它接受 ABI 稳定的 `TfLiteOperator`），见 [tensorflow/lite/mutable_op_resolver_utils.h:24-32](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/mutable_op_resolver_utils.h#L24-L32)。

#### 4.3.2 核心流程

**注册流程（`AddBuiltin` / `AddCustom`）**：

```
AddBuiltin(op, registration, version):
  1. 拷贝一份 *registration（按值存入 map，不持有调用方指针）
  2. 回填身份字段：
       new_registration.custom_name = nullptr
       new_registration.builtin_code = op
       new_registration.version     = version
  3. 以 (op, version) 为键写入 builtins_（覆盖同键旧值）
  4. 置 may_directly_contain_user_defined_ops_ = true
```

`AddCustom` 同理，只是回填成 `builtin_code = BuiltinOperator_CUSTOM`、`custom_name = name`，并以 `(name, version)` 为键写入 `custom_ops_`。

**查找流程（`FindOp`）**：

```
FindOp(op, version):
  1. 在 builtins_ 里查 (op, version)；命中则返回
  2. 否则遍历 other_op_resolvers_（链式 resolver），逐个问它们有没有
  3. 都没有 → 返回 nullptr
```

这里的「身份回填」非常关键：`AddBuiltin`/`AddCustom` **不信任调用方传入的 `builtin_code`/`custom_name`/`version`**，而是根据注册方式强制覆写。这样无论调用方怎么填，注册表里的 `TfLiteRegistration` 身份字段一定与它在表里的键一致——后续 `FindOp` 返回的对象自带正确身份，避免错配。

#### 4.3.3 源码精读

**① 两张哈希表与复合键**：[tensorflow/lite/mutable_op_resolver.h:143-153](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/mutable_op_resolver.h#L143-L153)

```cpp
typedef std::pair<tflite::BuiltinOperator, int> BuiltinOperatorKey;  // (枚举, version)
typedef std::pair<std::string, int> CustomOperatorKey;               // (名字, version)

std::unordered_map<BuiltinOperatorKey, TfLiteRegistration, ...> builtins_;
std::unordered_map<CustomOperatorKey,  TfLiteRegistration, ...> custom_ops_;
std::vector<const OpResolver*> other_op_resolvers_;                  // 链式 resolver
```

注意值类型是 `TfLiteRegistration`（按值），不是指针——注册时是拷贝。

**② `AddBuiltin` 的身份回填**：[tensorflow/lite/mutable_op_resolver.cc:58-79](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/mutable_op_resolver.cc#L58-L79)

```cpp
TfLiteRegistration new_registration = *registration;   // 按值拷贝
new_registration.custom_name = nullptr;                // 强制回填身份
new_registration.builtin_code = op;
new_registration.version = version;
auto op_key = std::make_pair(op, version);
builtins_[op_key] = new_registration;                  // 覆盖同键旧值
may_directly_contain_user_defined_ops_ = true;
```

`AddCustom` 的对应回填见 [tensorflow/lite/mutable_op_resolver.cc:89-99](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/mutable_op_resolver.cc#L89-L99)：`builtin_code = BuiltinOperator_CUSTOM`、`custom_name = name`。

**③ `FindOp` 的「先本表后链式」查找**：[tensorflow/lite/mutable_op_resolver.cc:28-56](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/mutable_op_resolver.cc#L28-L56)

```cpp
const TfLiteRegistration* MutableOpResolver::FindOp(tflite::BuiltinOperator op,
                                                    int version) const {
  auto it = builtins_.find(std::make_pair(op, version));
  if (it != builtins_.end()) return &it->second;          // 1. 先查本表
  for (const OpResolver* other : other_op_resolvers_) {   // 2. 再查链式 resolver
    const TfLiteRegistration* result = other->FindOp(op, version);
    if (result != nullptr) return result;
  }
  return nullptr;                                          // 3. 都没有
}
```

**④ 测试里的「最小自定义 registration」**：[tensorflow/lite/mutable_op_resolver_test.cc:28-40](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/mutable_op_resolver_test.cc#L28-L40) 给出了构造一个 `TfLiteRegistration` 的最简写法——只用指定设计器初始化的几个字段：

```cpp
TfLiteStatus DummyInvoke(TfLiteContext* context, TfLiteNode* node) {
  return kTfLiteOk;
}
TfLiteRegistration* GetDummyRegistration() {
  static TfLiteRegistration registration = {
      .init = nullptr, .free = nullptr,
      .prepare = nullptr, .invoke = DummyInvoke,
  };
  return &registration;
}
```

而注册一个 custom op 并验证能查到，见 [tensorflow/lite/mutable_op_resolver_test.cc:138-147](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/mutable_op_resolver_test.cc#L138-L147)：`resolver.AddCustom("AWESOME", GetDummyRegistration())` 之后，`FindOp("AWESOME", 1)` 能取回 registration，且其 `builtin_code == BuiltinOperator_CUSTOM`、`version == 1`。

#### 4.3.4 代码实践（本讲核心实践任务）

**实践目标**：对照 `op_resolver.h` 与 `mutable_op_resolver.h`，动手写出「为一个自定义 op 注册 kernel」的最小代码骨架，并解释每一步对应的源码行为。

**操作步骤（编写最小示例代码，待本地编译验证）**：

> 以下为「示例代码」（非项目原有文件），仿照 `mutable_op_resolver_test.cc` 的 `GetDummyRegistration` 与官方注释里的用法模板编写。

第 1 步：实现你的自定义 op 的四个回调（至少要有 `invoke`）。

```cpp
// 示例代码：一个把所有元素乘 2 的 custom op
static TfLiteStatus MyPrepare(TfLiteContext* ctx, TfLiteNode* node) {
  // 输出形状 = 输入形状
  return ctx->ResizeTensor(ctx, node->outputs[0],
                           TfLiteIntArrayCopy(node->inputs[0]->dims));
}

static TfLiteStatus MyInvoke(TfLiteContext* ctx, TfLiteNode* node) {
  const TfLiteTensor* in = node->inputs[0];
  TfLiteTensor* out = node->outputs[0];
  // ...（此处省略真正的元素 ×2 计算）
  return kTfLiteOk;
}

static TfLiteRegistration* Register_DoubleIt() {
  static TfLiteRegistration r = {
      .init = nullptr, .free = nullptr,
      .prepare = MyPrepare, .invoke = MyInvoke,
  };
  return &r;
}
```

第 2 步：建一个 `MutableOpResolver`，注册你的 custom op。

```cpp
tflite::MutableOpResolver resolver;
resolver.AddCustom("DoubleIt", Register_DoubleIt(), /*version=*/1);
```

第 3 步：用这个 resolver 构造解释器。

```cpp
tflite::InterpreterBuilder builder(model, resolver);
std::unique_ptr<tflite::Interpreter> interpreter;
builder(&interpreter);
```

**需要观察/解释的现象（对照源码回答）**：

1. 第 2 步调用 `AddCustom` 后，对照 [mutable_op_resolver.cc:89-99](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/mutable_op_resolver.cc#L89-L99)：你传入的 `TfLiteRegistration` 被**按值拷贝**进 `custom_ops_`，且其 `builtin_code` 被强制改写为 `BuiltinOperator_CUSTOM`、`custom_name` 被改写为 `"DoubleIt"`、`version` 被改写为 `1`。
2. 当模型里出现一个名为 `"DoubleIt"` 的算子时，对照 [op_resolver.cc:57-58](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/api/op_resolver.cc#L57-L58)：`GetRegistrationFromOpCode` 会调 `FindOp("DoubleIt", 1)`，最终命中你在第 2 步注册的那条记录，返回你写的 `MyPrepare`/`MyInvoke` 函数指针。
3. 解释 FlatBuffer 为何适合移动端：模型里只多存了一个字符串 `"DoubleIt"` 与整数 version，运行时按这个名字查表即可，无需把算子实现代码塞进模型文件。

**预期结果**：你能说清「自定义 op 的 kernel 代码住在宿主程序里（通过 `AddCustom` 注册进 resolver），模型文件里只存算子名字」这一分离设计，以及它是如何支撑「同一模型、不同宿主可提供不同实现」的。

> ⚠️ 上述代码为示例骨架，未在本地编译运行；完整可编译的自定义 op 工程需要配套的 `BUILD` 目标与 `.tflite` 模型生成步骤，请以官方 `tensorflow/examples` 或 `tensorflow/lite/tools` 下的完整示例为准（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `AddBuiltin`/`AddCustom` 要在拷贝 registration 后**强制覆写** `builtin_code`/`custom_name`/`version` 三个字段，而不是直接信任调用方传入的值？

> **答案**：为保证「表里的键」与「值对象自带的身份字段」始终一致。调用方可能传了不匹配的身份字段（或干脆没填），强制覆写能杜绝因身份错配导致的查找错乱，使 `FindOp` 返回的对象自带正确、可信的身份。

**练习 2**：`version` 参数有什么用？为什么同一个算子要支持多个版本？

> **答案**：算子的语义或数据布局会演进（如新增 fused activation、改变量化方式）。新版本用更高 version 标识，对应新的 kernel 实现。模型在导出时记录所用版本，运行时 `FindOp(op, version)` 据此挑出匹配的 kernel，从而在同一套代码里同时兼容新老模型。

**练习 3**：`FindOp` 找不到时，`MutableOpResolver` 还会去 `other_op_resolvers_` 里找。这个「链式 resolver」机制（`ChainOpResolver`）解决了什么问题？

> **答案**：允许把多个 resolver 组合成一个逻辑 resolver。比如把「内置 builtin 算子的 resolver」与「某厂商扩展算子的 resolver」链在一起，宿主无需把所有注册代码塞进同一个 resolver 对象，实现注册来源的模块化组合。

---

### 4.4 flatbuffer_conversions：把 FlatBuffer 选项翻译成运行时参数

#### 4.4.1 概念说明

到这里，我们解决了「找到函数指针」（`OpResolver`）。但一个 builtin 算子光有函数指针还不够——`Conv2D` 需要知道 stride、padding、dilation；`Add` 需要知道 fused activation。这些参数在 FlatBuffer 里以 schema 定义的结构（`Conv2DOptions`、`AddOptions`）存储，而 kernel 的 `init`/`prepare` 回调期望读到的是运行时用的 **C 结构体**（`TfLiteConvParams`、`TfLiteAddParams`）。

`flatbuffer_conversions` 就是这二者之间的翻译层，职责有三：

1. **类型翻译**：把 FlatBuffer 的 `TensorType` 枚举翻译成运行时的 `TfLiteType`（`ConvertTensorType`）。
2. **选项翻译**：把每个 builtin 算子的 FlatBuffer Options 翻译成对应的 `TfLiteXxxParams` C 结构（一堆 `ParseXxx` 函数 + 总分发器 `ParseOpData`）。
3. **枚举翻译**：把 padding、activation 等子枚举翻译成运行时常量（`ConvertPadding`、`ConvertActivation`）。

注意一个重要区别：**custom op 不走这条路**。custom op 的参数是用户自定义的字节流 `custom_options`，运行时直接把它原样作为 `init_data` 传给 kernel 的 `init` 回调，由 kernel 自己解释——所以没有 `ParseXxx` 为它服务。

#### 4.4.2 核心流程

builtin 算子参数从 FlatBuffer 到运行时的翻译流程：

```
InterpreterBuilder 遍历每个 Operator
  │
  ├─ 取 registration（由 4.2/4.3 节得到），读 registration->builtin_code
  │
  ├─ 若 custom：
  │     init_data = op->custom_options()->data()   // 裸字节，直接透传
  │     （不走 ParseOpData）
  │
  └─ 若 builtin：
        ParseOpData(op, op_type, ..., &builtin_data)
           │
           └─ ParseOpDataTfLite(op, op_type, ...)   // 巨型 switch
                 ├─ case CONV_2D:  ParseConv2D(...)
                 │     用 BuiltinDataAllocator 分配 TfLiteConvParams（POD）
                 │     读 op->builtin_options_as_Conv2DOptions()
                 │     逐字段翻译（ConvertPadding / ConvertActivation）
                 │     写入 builtin_data
                 ├─ case ADD:      ParseAdd(...)
                 ├─ case MUL:      ParseMul(...)
                 └─ ...（每个 builtin 一个 case）
        把 builtin_data 连同 registration 一起塞给 Subgraph 节点
```

两个要点：①分配出的参数结构必须是 **POD**（plain old data），因为它的所有权会移交给 C 扩展层，析构时不会调析构函数（见 `BuiltinDataAllocator::AllocatePOD` 的 `static_assert(std::is_trivially_destructible<T>)`）。②翻译过程对缺失的 Options 采取「保留默认/留 TODO」的宽容策略，不轻易报错（见各 `ParseXxx` 里的 `else` 注释）。

#### 4.4.3 源码精读

**① POD 分配器 `BuiltinDataAllocator`**：[tensorflow/lite/core/api/flatbuffer_conversions.h:34-52](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/api/flatbuffer_conversions.h#L34-L52)

```cpp
template <typename T>
T* AllocatePOD() {
  static_assert(std::is_trivially_destructible<T>::value,
                "Builtin data structure must be POD.");
  void* allocated_memory = this->Allocate(sizeof(T), alignof(T));
  return new (allocated_memory) T();
}
```

注释点明原因：*"Interpreter's C extension part will take ownership so destructors will not be run during deallocation."*——所以参数结构必须是平凡可析构的 POD。

**② 类型翻译 `ConvertTensorType`**：[tensorflow/lite/core/api/flatbuffer_conversions.cc:1038-1116](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/api/flatbuffer_conversions.cc#L1038-L1116) 是一个把 schema `TensorType` 枚举逐 case 映射到运行时 `TfLiteType` 的大 switch，节选：

```cpp
case TensorType_FLOAT32: *type = kTfLiteFloat32; return kTfLiteOk;
case TensorType_INT8:    *type = kTfLiteInt8;    return kTfLiteOk;
case TensorType_INT64:   *type = kTfLiteInt64;   return kTfLiteOk;
// ...
default: *type = kTfLiteNoType; /* 报错 */ return kTfLiteError;
```

**③ 单个算子的翻译 `ParseAdd`**：[tensorflow/lite/core/api/flatbuffer_conversions.cc:1126-1149](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/api/flatbuffer_conversions.cc#L1126-L1149)

```cpp
TfLiteStatus ParseAdd(const Operator* op, ErrorReporter* error_reporter,
                      BuiltinDataAllocator* allocator, void** builtin_data) {
  // ...
  auto params = safe_allocator.Allocate<TfLiteAddParams>();        // 分配 POD
  const AddOptions* schema_params = op->builtin_options_as_AddOptions();
  if (schema_params != nullptr) {
    params->activation =
        ConvertActivation(schema_params->fused_activation_function());  // 翻译枚举
    params->pot_scale_int16 = schema_params->pot_scale_int16();
  }
  *builtin_data = params.release();
  return kTfLiteOk;
}
```

这是一个典型的「分配 C 结构 → 用 FlatBuffer 访问器读 Options → 逐字段翻译 → 交出所有权」模板。再看一个字段更多的 `ParseConv2D`：[tensorflow/lite/core/api/flatbuffer_conversions.cc:1344-1376](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/api/flatbuffer_conversions.cc#L1344-L1376)，它翻译了 padding、stride、dilation、activation、quantized_bias_type 等字段。

**④ 总分发器 `ParseOpData` 与巨型 switch**：`ParseOpData` 是对外的统一入口（[tensorflow/lite/core/api/flatbuffer_conversions.cc:3014-3045](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/api/flatbuffer_conversions.cc#L3014-L3045)），它转调内部函数 `ParseOpDataTfLite`（[tensorflow/lite/core/api/flatbuffer_conversions.cc:163-1034](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/api/flatbuffer_conversions.cc#L163-L1034)），后者是一个覆盖全部 builtin 算子码的 `switch (op_type)`：

```cpp
case BuiltinOperator_ADD:    return ParseAdd(op, error_reporter, allocator, builtin_data);
case BuiltinOperator_CONV_2D:return ParseConv2D(op, error_reporter, allocator, builtin_data);
case BuiltinOperator_MUL:    return ParseMul(op, error_reporter, allocator, builtin_data);
// ... 每个 builtin 一个 case
```

注意 `ParseOpData` 在 `TF_LITE_STATIC_MEMORY`（Micro）下被禁用——Micro 为了省 Flash 只链接它真正用到的那些 `ParseXxx`，所以每个 `ParseXxx` 才被单独定义而非内联在 switch 里（见代码里反复出现的注释 *"used as part of the selective registration for the OpResolver implementation in micro"*）。这是 TFLite 为嵌入式做的体积优化。

**⑤ 建图主循环里的分叉**：[tensorflow/lite/core/interpreter_builder.cc:370-401](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/interpreter_builder.cc#L370-L401) 一眼看清 builtin 与 custom 的差别：

```cpp
void* builtin_data = nullptr;
const char* init_data = nullptr;
size_t init_data_size = 0;
if (op_type == BuiltinOperator_CUSTOM) {
  if (op->custom_options()) {
    init_data = reinterpret_cast<const char*>(op->custom_options()->data());  // custom：透传字节
    init_data_size = op->custom_options()->size();
  }
  // ...（large_custom_options 分支略）
} else {
  MallocDataAllocator malloc_allocator;
  TF_LITE_ENSURE_STATUS(ParseOpData(op, op_type, error_reporter_,
                                    &malloc_allocator, &builtin_data));        // builtin：翻译成 C 结构
}
subgraph->AddNodeWithParameters(..., init_data, init_data_size, builtin_data, registration);
```

#### 4.4.4 代码实践

**实践目标**：以 `Conv2D` 为例，亲手画出「FlatBuffer Options → 运行时 C 结构」的字段映射表，理解翻译层到底翻译了什么。

**操作步骤（源码阅读型）**：

1. 打开 [flatbuffer_conversions.cc 的 ParseConv2D（L1344-L1376）](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/api/flatbuffer_conversions.cc#L1344-L1376)。
2. 在 `tensorflow/lite/schema/schema.fbs` 中找到 `table Conv2DOptions`，列出它的字段。
3. 在 `tensorflow/lite/core/c/builtin_op_data.h` 中找到 `TfLiteConvParams` 结构体，列出它的字段。
4. 把三者填进下面的映射表。

**需要填写的映射表（示例骨架）**：

| FlatBuffer `Conv2DOptions` 字段 | 经哪个转换函数 | 运行时 `TfLiteConvParams` 字段 |
| --- | --- | --- |
| `padding` | `ConvertPadding` | `params->padding` |
| `stride_w` | （直接赋值） | `params->stride_width` |
| `stride_h` | （直接赋值） | `params->stride_height` |
| `fused_activation_function` | `ConvertActivation` | `params->activation` |
| `dilation_w_factor` | （直接赋值） | `params->dilation_width_factor` |
| `quantized_bias_type` | `ConvertTensorType` | `params->quantized_bias_type` |
| ……（请补全） | | |

**需要观察的现象**：有些字段是「直接赋值」（数值类型一致，原样拷贝），有些字段必须经过 `ConvertPadding`/`ConvertActivation`/`ConvertTensorType` 这类枚举翻译——因为 FlatBuffer schema 的枚举与运行时 C 头里的枚举是**两套独立的编号**，不能直接互转。

**预期结果**：你能解释「为什么需要 flatbuffer_conversions 这一层」——因为 schema 的枚举/结构与运行时的枚举/结构是两套定义，且参数结构必须是 POD 以便跨 C/C++ 边界移交所有权。

> 若想验证翻译结果，可阅读 [tensorflow/lite/core/api/flatbuffer_conversions_test.cc](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/api/flatbuffer_conversions_test.cc) 中的断言，确认某个 Options 翻译后 `builtin_data` 里各字段的期望值。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `BuiltinDataAllocator::AllocatePOD` 要求 `T` 必须是 `std::is_trivially_destructible`？

> **答案**：因为分配出的参数结构的所有权会移交给 Interpreter 的 C 扩展层，释放时不会调用 C++ 析构函数。若允许非平凡析构类型，成员（如 `std::string`）会泄漏。所以只允许 POD 结构。

**练习 2**：custom op 的参数为什么不经过 `ParseOpData`？

> **答案**：custom op 的参数语义由 op 作者自定义，没有统一 schema，无法写成通用的 `ParseXxx`。运行时直接把 `custom_options` 字节流原样作为 `init_data` 交给 kernel 的 `init` 回调，由 kernel 自己解释这段字节。

**练习 3**：为什么每个 `ParseXxx`（如 `ParseAbs`、`ParseCeil`）即使函数体只是 `return kTfLiteOk;` 也要单独定义成函数，而不是直接写在 switch 里？

> **答案**：为了支持 TFLite Micro 的选择性注册（selective registration）。Micro 只把它实际用到的算子的 `ParseXxx` 链接进二进制以省 Flash。如果都内联在 switch 里，就无法按算子粒度裁剪。

---

## 5. 综合实践

**任务**：端到端跟踪一个含 `ADD`（builtin）算子的 `.tflite` 模型在 `InterpreterBuilder` 里的建图过程，再设想把它换成名为 `"DoubleIt"` 的 custom op，说明分别要在哪几个地方做改动。

**步骤**：

1. **模型层**：确认 `ADD` 在模型里以一条 `OperatorCode{builtin_code=ADD, version=1}` 存于 `operator_codes[]`，每个 `ADD` 算子的 `Operator` 用 `opcode_index` 指向它，参数存为 `builtin_options_as_AddOptions()`。（对照 4.1 节）
2. **查表层**：跟踪 [interpreter_builder.cc:269-291](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/interpreter_builder.cc#L269-L291)：`GetRegistrationFromOpCode` 命中 builtin 分支，调 `op_resolver.FindOp(BuiltinOperator_ADD, 1)` 取回 ADD 的 `TfLiteRegistration`，存进 `flatbuffer_op_index_to_registration_`。（对照 4.2 节）
3. **注册层**：确认这个 ADD 的 registration 是由默认的 `BuiltinOpResolver`（`MutableOpResolver` 子类）在启动时通过 `AddBuiltin(BuiltinOperator_ADD, tflite::ops::builtin::Register_ADD(), 1)` 注册进 `builtins_` 表的。（对照 4.3 节）
4. **参数层**：跟踪 [interpreter_builder.cc:392-395](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/interpreter_builder.cc#L392-L395)：因为是 builtin，调 `ParseOpData` → `ParseAdd`，把 `AddOptions` 翻译成 `TfLiteAddParams`，作为 `builtin_data` 传入 `AddNodeWithParameters`。（对照 4.4 节）

**改造为 custom op `"DoubleIt"` 时要改的地方**：

- **模型层**：算子码改成 `OperatorCode{builtin_code=CUSTOM, custom_code="DoubleIt", version=1}`，参数改存为 `custom_options`（裸字节）。
- **查表层**：`GetRegistrationFromOpCode` 改走 custom 分支，调 `FindOp("DoubleIt", 1)`。
- **注册层**：你必须在宿主程序里手写 `resolver.AddCustom("DoubleIt", Register_DoubleIt(), 1)`（见 4.3.4 的示例代码），否则查不到、建图失败。
- **参数层**：`InterpreterBuilder` 不再调 `ParseOpData`，而是把 `custom_options` 字节流直接当 `init_data` 透传给你的 `init` 回调。

**验收标准**：你能对着上面四层源码，分别指出 builtin 与 custom 两条路径在哪一行分叉、各自如何取到 kernel 函数指针、如何取到参数。这就把本讲的三个机制（FlatBuffer 格式 / OpResolver / flatbuffer_conversions）串成了一条完整链路。

## 6. 本讲小结

- `.tflite` 文件用 FlatBuffers 存储，算子类型集中在去重的 `operator_codes[]` 表里，每个 `Operator` 用 `opcode_index` 指向它；mmap 后零拷贝按偏移读取，无需反序列化——这是移动端低内存、快启动的关键。
- `OpResolver` 是「算子码 → 函数指针」的抽象查表接口，只有两个纯虚 `FindOp`（按枚举、按名字）；它让模型与具体 kernel 集合解耦，同一模型可搭配不同运行时的 resolver。
- `MutableOpResolver` 用两张哈希表 `builtins_`/`custom_ops_`（键均为「身份 + version」复合键）实现注册表；`AddBuiltin`/`AddCustom` 会拷贝 registration 并强制回填身份字段，保证键与对象身份一致。
- 自定义 op 的注册入口就是 `AddCustom`，模型里只存算子名字，kernel 代码住在宿主程序里——这正是「同一模型、不同宿主提供不同实现」的基础。
- `flatbuffer_conversions` 负责把 builtin 算子的 FlatBuffer Options 翻译成运行时 POD 结构 `builtin_data`；custom op 不走这条路，其 `custom_options` 字节流直接透传给 `init` 回调。
- `InterpreterBuilder` 是把「模型 + OpResolver + flatbuffer_conversions」三者接起来的建图总调度：先解析算子码表建「index→registration」缓存，再逐算子取 registration、按 builtin/custom 分别取参数，最后塞进 Subgraph 节点。

## 7. 下一步学习建议

- 本讲建立的「算子解析 → registration」心智模型，是理解 **delegate（委托）机制** 的前提。下一讲 [u8-l3 TFLite 委托机制 delegates](u8-l3-tflite-delegates.md) 将讲解 GPU/NNAPI/XNNPACK 等 delegate 如何把一部分子图替换成自己的 `TfLiteRegistration`——你会看到 `OpResolver` 里那组 `GetDelegateCreators`/`GetOpaqueDelegateCreators` 虚函数（本讲已埋下伏笔）正是为它服务的。
- 如果你对「选择性注册、按需裁剪二进制体积」感兴趣，可继续阅读 `tensorflow/lite/core/create_op_resolver_with_selected_ops.cc` 与 Micro 相关的 selective registration 实现，理解 `may_directly_contain_user_defined_ops_` 这个标志在裁剪校验中的作用。
- 想看一个真正可编译的自定义 op 工程模板，可在 `tensorflow/lite/tools` 与 `tensorflow/examples` 下查找完整示例，补齐本讲 4.3.4 中省略的 `BUILD` 目标与模型生成步骤。
