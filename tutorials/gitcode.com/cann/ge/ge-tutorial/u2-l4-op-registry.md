# 算子注册与原型体系

## 1. 本讲目标

学完本讲，你应当能够：

- 说清「算子原型（OpProto）」与上一讲的「算子描述（OpDesc）」的区别——前者是**类型层面的契约**，后者是**节点层面的实例**。
- 看懂 `REG_OP` 宏如何用「链式静态注册」把一个算子的类型、输入、输出、属性、推导函数登记进系统。
- 理解 `OpDef` 这套更现代、更结构化的原型定义方式，以及它与 `REG_OP` 的关系。
- 说清 GE 仓与外部算子仓的职责边界：GE 只提供注册基础设施和加载器，真正的算子定义被编译成 `.so`，运行时由 `OpsProtoManager` 用 `dlopen` 加载。

本讲是 AscendIR 基石单元（u2）的收官，把上一讲「OpDesc 里那个 `type` 字符串」补全为「系统能理解的完整算子定义」。

## 2. 前置知识

在进入本讲前，你需要回顾以下两个概念（来自 u2-l1～u2-l3）：

- **四层对象模型**：`ComputeGraph → Node → OpDesc → GeTensorDesc`。一个 `Node` 通过 `GetOpDesc()` 拿到它的 `OpDesc`。
- **OpDesc 的 `type` 字段**：u2-l3 讲过，`OpDesc` 用一个字符串 `type`（如 `"Add"`、`"Conv2D"`）表示这个节点是哪一类算子，但它**只描述规格，不含计算实现**。

这就引出本讲要回答的核心问题：

> 当一个 `Node` 的 `OpDesc.type` 写着 `"Add"` 时，GE 怎么知道 `Add` 有几个输入、几个输出、需要哪些属性、输出 shape 怎么推导？

答案就是**算子原型注册体系**：在程序启动（或动态库加载）时，每个算子类型会把自己的一份「说明书」登记到一个全局注册表里；之后 GE 凡是遇到 `OpDesc.type`，就到这个注册表里查它的说明书。

这里有一个关键的对照关系，请务必记住：

| 层面 | 对象 | 回答的问题 | 归属 |
| --- | --- | --- | --- |
| 类型层面（本讲） | 算子原型 / OpProto | `Add` 这类算子的契约是什么？ | 注册表（全局，编译期/加载期写入） |
| 节点层面（u2-l3） | OpDesc | 图里这个具体节点的类型与属性值是什么？ | 图里的某个 Node（每节点一份） |

打个比方：算子原型是「菜谱」，`OpDesc.type` 是「点了一份哪道菜」，而注册表就是「菜单总册」。没有菜单总册，光报菜名是做不出菜的。

此外需要一点 C++ 前置：**静态对象的构造函数会在动态库被加载（`dlopen`）时执行**。GE 算子注册正是利用了这个语言特性——这也是为什么算子定义能被「打包成 `.so`、运行时按需加载」。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `inc/graph_metadef/external/graph/operator_reg.h` | 定义 `REG_OP` / `INPUT` / `OUTPUT` / `OP_END_FACTORY_REG` 等注册宏，是经典原型注册的入口。 |
| `inc/graph_metadef/external/graph/operator_factory.h` | 声明对外查询接口 `OperatorFactory` 与注册辅助类 `OperatorCreatorRegister`。 |
| `inc/graph_metadef/graph/operator_factory_impl.h` | `OperatorFactoryImpl` 的内部声明，持有各类注册信息的静态 map。 |
| `graph_metadef/graph/normal_graph/operator_factory.cc` | `OperatorCreatorRegister` 构造函数：把创建器写入注册表。 |
| `graph_metadef/graph/normal_graph/operator_factory_impl.cc` | 注册表的真正存储与查询实现。 |
| `graph_metadef/register/opdef/op_def.cc` | 新一代结构化原型 `OpDef` 的对外实现（Input/Output/Attr/SetInferShape）。 |
| `graph_metadef/register/opdef/op_def_impl.h` | `OpDefImpl` 结构：算子类型、输入输出、属性、AICore/AICPU 实现指针。 |
| `graph_metadef/register/opdef/op_def_factory.cc` | `OpDefFactory::OpDefRegister`：把结构化原型登记进工厂。 |
| `graph_metadef/graph/opsproto/opsproto_manager.cc` | `OpsProtoManager`：用 `dlopen` 加载外部算子仓 `.so`，触发注册。 |
| `examples/custom_op/compilable_add_custom/ge/add_custom.h` | 最简 `REG_OP` 示例：只声明输入输出。 |
| `examples/custom_op/ascendc_add_custom/add_custom_kernel/custom_op.cpp` | 完整自定义算子示例：`REG_OP` + 推导函数 + 自动映射注册。 |

> 说明：`register/op_def.h`、`register/op_def_factory.h`、`register/op_impl_registry.h` 等头文件在本仓库中**并不存在**，它们来自外部的 `register`/`gert` 包。这本身就是「GE 仓与算子仓解耦」的体现，第 4.4 节会展开。

## 4. 核心概念与源码讲解

本讲按三个最小模块组织：

1. **OpProto 原型**（4.1）：算子类型签名是什么。
2. **算子注册机制**（4.2、4.3）：经典 `REG_OP` 路径与新一代 `OpDef` 路径，如何把原型登记进注册表。
3. **GE 与算子仓协作**（4.4）：外部算子定义如何被打包、加载进 GE。

### 4.1 算子原型 OpProto：给 type 字段赋予意义

#### 4.1.1 概念说明

「算子原型（OpProto / Operator Prototype）」是一个算子**类型的完整契约**，回答四件事：

- **类型名**：这个算子叫什么（如 `Add`、`AddCustom`）。
- **输入**：有几个输入、每个输入叫什么名字、允许哪些数据类型/格式。其中又有必选输入 `INPUT`、可选输入 `OPTIONAL_INPUT`、动态数量输入 `DYNAMIC_INPUT`。
- **输出**：有几个输出、名字与类型约束，同样支持动态数量 `DYNAMIC_OUTPUT`。
- **属性（Attribute）**：算子需要哪些配置项（如卷积的 `strides`、`pads`），以及它们的默认值、是否必填。
- （常配套）**推导函数**：输出 shape / dtype 如何由输入推导，以及一个用于「按名字创建该算子」的工厂函数。

注意它与 `OpDesc` 的层次差别：原型是「这一类算子长什么样」的模板，`OpDesc` 是「图里这个具体节点填了哪些属性值」的实例。原型是「类定义」，`OpDesc` 是「对象」。

#### 4.1.2 核心流程

GE 运行期使用算子原型的流程可以概括为：

1. **加载期**：把含算子定义的 `.so` 载入进程，其内部的静态对象在载入瞬间执行注册，把每个算子类型的契约写入全局注册表。
2. **查询期**：当 GE 在某处需要「Add 的契约」时（例如根据 `OpDesc.type` 推导 shape、或根据名字构造一个 `Operator` 对象），就到注册表里按类型名查。
3. **应用期**：拿到契约后，校验某个节点的输入输出数量/类型是否合规、调用其推导函数等。

伪代码示意：

```text
加载 .so  →  静态对象构造  →  RegisterOperatorCreator("Add", creator)  →  全局 map["Add"] = creator
查询      →  OperatorFactory::CreateOperator(name, "Add")  →  在 map 里找到 creator  →  new Operator("Add")
```

#### 4.1.3 源码精读

一个算子原型长什么样？先看仓库自带的最简示例——`AddCustom` 只声明了输入输出：

[examples/custom_op/compilable_add_custom/ge/add_custom.h:17-24](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/examples/custom_op/compilable_add_custom/ge/add_custom.h#L17-L24)

这段代码读起来像「链式调用」，但它其实是**一连串宏展开**。`REG_OP(AddCustom)` 开头、`OP_END_FACTORY_REG(AddCustom)` 收尾，中间用 `.INPUT(...)` / `.OUTPUT(...)` 声明端口。第 4.2 节会拆解这些宏到底生成了什么。

更完整的例子是 `ascendc_add_custom`，它额外用 `DATATYPE` 约束了 `T` 这个「占位类型」的取值范围，并在文件后部用 `IMPL_OP(AddCustom).InferShape(...)` 绑定推导函数、用 `REG_AUTO_MAPPING_OP(AddCustom)` 做自动映射注册：

[examples/custom_op/ascendc_add_custom/add_custom_kernel/custom_op.cpp:80-86](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/examples/custom_op/ascendc_add_custom/add_custom_kernel/custom_op.cpp#L80-L86)

[examples/custom_op/ascendc_add_custom/add_custom_kernel/custom_op.cpp:126-128](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/examples/custom_op/ascendc_add_custom/add_custom_kernel/custom_op.cpp#L126-L128)

可以看到，一个完整的自定义算子原型由「类型签名（REG_OP 段）」+「推导实现（IMPL_OP 段）」两部分拼成。

#### 4.1.4 代码实践

**实践目标**：从实例反推「原型契约」包含哪些要素。

**操作步骤**：

1. 打开 `examples/custom_op/ascendc_add_custom/add_custom_kernel/custom_op.cpp`，定位 80～86 行的 `REG_OP(AddCustom)` 段。
2. 列出 `AddCustom` 的契约：类型名、输入端口名、输出端口名、占位类型 `T` 允许哪些 `DataType`。
3. 再打开 `examples/custom_op/compilable_add_custom/ge/add_custom.h`，对比两个示例的输入端口名差异（一个是 `x/y/z`，一个是 `x1/x2/y`）。

**需要观察的现象**：两份示例的算子类型都叫 `AddCustom`，但端口名不同——这说明**端口名是原型的一部分**，是算子契约的「键」，后续通过名字（如 `set_input_x`、`get_output_desc_y`）访问端口时必须与原型声明一致。

**预期结果**：你能用一句话写出 `AddCustom` 的原型契约，例如「2 个输入 `x`、`y`，1 个输出 `z`，类型 `T` 可取 float/int32/...，shape 由广播语义推导」。

#### 4.1.5 小练习与答案

**练习 1**：如果同一个算子类型 `Add` 在两个不同的 `.so` 里各注册了一次，会发生什么？

> **参考答案**：后注册的会与先注册的发生键冲突。GE 在 `OperatorFactoryImpl` 里用 `map<string, OpCreator>` 存储（见 4.2.3），同键重复写入会被注册逻辑处理（覆盖或报错，取决于是否开启 `SetRegisterOverridable`）。这也提示：算子定义应避免重复打包进多个被同时加载的 `.so`。

**练习 2**：`INPUT` 和 `OPTIONAL_INPUT` 在原型层面的区别是什么？

> **参考答案**：`INPUT` 是必选输入，算子运行时该端口必须连边；`OPTIONAL_INPUT` 是可选输入，可以不连（如某些带 mask 的算子）。这个「是否必选」的信息在注册时就被写进原型，供后续校验与推导使用。

### 4.2 算子注册机制：REG_OP 宏与静态注册

#### 4.2.1 概念说明

GE 的经典注册机制是一套**基于宏的链式静态注册**。其精妙之处在于：你写的 `.INPUT(x, T).OUTPUT(y, T)` 看起来是运行期链式调用，实际经过宏展开后，会变成一个**编译期生成的类**——每个端口对应一个私有注册函数，构造对象时依次调用它们，把端口信息写进基类 `Operator`。最后，`OP_END_FACTORY_REG` 会额外生成一个**静态对象**，它的构造函数把这个算子类型的「创建器」登记进全局注册表。

这里有两个关键设计：

- **静态对象构造做注册**：注册不需要显式调用 `register()`，而是在 `.so` 加载时由 C++ 静态对象自动完成。这是「插件式」架构的常见手法。
- **类型即类名**：`REG_OP(Add)` 会生成 `class Add`，算子类型名与类名严格一致，并用 `static_assert` 强制约束。

#### 4.2.2 核心流程

一个 `REG_OP(...) ... OP_END_FACTORY_REG(X)` 块展开后的逻辑链：

1. `REG_OP(X)` → 在 `namespace op` 下生成 `class X : public Operator`，其构造函数调用 `__X()`。
2. 每遇到 `.INPUT(p, t)` / `.OUTPUT(p, t)` / `.ATTR(p, ...)`，宏展开会结束上一个链段，并生成对应的 `__input_p()` / `__out_p()` / `__attr_p()` 私有函数，内部调用 `Operator::InputRegister("p", "t")` 等，把端口登记进基类。
3. `OP_END_FACTORY_REG(X)` → 生成一个静态变量 `OperatorCreatorRegister g_register_N("X", 创建函数)`。
4. 该静态变量构造时，调用 `OperatorFactoryImpl::RegisterOperatorCreator("X", 创建函数)`，把 `(类型名 → 创建器)` 写入全局 map。

```
REG_OP(X)            生成 class op::X，构造函数 → __X()
  .INPUT(a, T)  ──►  Operator::InputRegister("a", "T")   （把端口写进 Operator 基类）
  .OUTPUT(b, T) ──►  Operator::OutputRegister("b", "T")
OP_END_FACTORY_REG(X)──► static OperatorCreatorRegister g_register_N("X", []{ return X(name); })
                                              │ 构造时
                                              ▼
                          OperatorFactoryImpl::operator_creators_["X"] = 创建器
```

#### 4.2.3 源码精读

**第一步：`REG_OP` 宏生成算子类。** 它在 `op` 命名空间下定义一个继承 `Operator` 的类，构造函数都会调用 `__##x()` 这个由后续宏填充的函数：

[inc/graph_metadef/external/graph/operator_reg.h:279-301](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/external/graph/operator_reg.h#L279-L301)

链式语法之所以能成立，靠的是这个 `OpReg` 类——它的每个方法都返回 `*this`，于是 `.INPUT().OUTPUT()` 才能连续「点」下去：

[inc/graph_metadef/external/graph/operator_reg.h:240-277](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/external/graph/operator_reg.h#L240-L277)

**第二步：`INPUT` 宏把端口登记进 `Operator` 基类。** 注意宏体末尾的 `Operator::InputRegister(#x, #t)`，它把端口名和数据类型约束字符串真正写入基类；同时宏还在 `public` 区生成了 `set_input_x` / `get_input_desc_x` 等强类型访问方法：

[inc/graph_metadef/external/graph/operator_reg.h:385-425](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/external/graph/operator_reg.h#L385-L425)

`OUTPUT` 宏结构相同，核心调用是 `Operator::OutputRegister`：

[inc/graph_metadef/external/graph/operator_reg.h:469-492](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/external/graph/operator_reg.h#L469-L492)

**第三步：`OP_END_FACTORY_REG` 生成静态注册对象。** 这是最关键的「写入注册表」一步——它用一个静态 `OperatorCreatorRegister` 对象，在加载时自动执行注册（`PASTE(g_register, __COUNTER__)` 用 `__COUNTER__` 保证同一文件多次注册的静态变量名不冲突）：

[inc/graph_metadef/external/graph/operator_reg.h:633-644](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/external/graph/operator_reg.h#L633-L644)

**第四步：注册对象把创建器写入全局 map。** `OperatorCreatorRegister` 的构造函数直接委托给 `OperatorFactoryImpl::RegisterOperatorCreator`：

[graph_metadef/graph/normal_graph/operator_factory.cc:61-63](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/normal_graph/operator_factory.cc#L61-L63)

而注册表本体是 `OperatorFactoryImpl` 的两个静态 map——按算子类型名分别存 V1/V2 两版创建器：

[inc/graph_metadef/graph/operator_factory_impl.h:147-148](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/operator_factory_impl.h#L147-L148)

`RegisterOperatorCreator` 的实现确认了「按需创建 map、再插入」：

[graph_metadef/graph/normal_graph/operator_factory_impl.cc:202-206](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/normal_graph/operator_factory_impl.cc#L202-L206)

**第五步：查询期按类型名取出创建器并构造算子。** `CreateOperator` 先查 V2 表，再查 V1 表；`IsExistOp` 判断某类型是否已注册：

[graph_metadef/graph/normal_graph/operator_factory_impl.cc:62-79](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/normal_graph/operator_factory_impl.cc#L62-L79)

[graph_metadef/graph/normal_graph/operator_factory.cc:47-59](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/normal_graph/operator_factory.cc#L47-L59)

#### 4.2.4 代码实践

**实践目标**：亲手把一个 `REG_OP` 块「脑内展开」，验证它确实写入了注册表。

**操作步骤**：

1. 打开 `inc/graph_metadef/external/graph/operator_reg.h`，对照 4.2.3 的宏定义，把下面这段示例手工展开成等价的 C++ 类骨架：

   ```cpp
   // 示例代码（非项目原有）
   REG_OP(MyAdd)
       .INPUT(x, TensorType({DT_FLOAT}))
       .OUTPUT(y, TensorType({DT_FLOAT}))
       .OP_END_FACTORY_REG(MyAdd);
   ```

2. 展开要点：写出 `namespace op { class MyAdd : public Operator { ... }; }` 的构造函数、`__input_x()` / `__out_y()` 私有函数、以及 `OP_END_FACTORY_REG` 末尾的静态 `OperatorCreatorRegister` 变量。
3. 用下面的命令在仓库里确认「创建器最终落点」：

   ```bash
   grep -n "RegisterOperatorCreator" graph_metadef/graph/normal_graph/operator_factory_impl.cc
   ```

**需要观察的现象**：展开后你会发现——`.INPUT` / `.OUTPUT` 这些「点调用」在宏展开后变成了**对基类 `Operator` 的 `InputRegister` / `OutputRegister` 调用**，而真正的「类型 → 创建器」映射只在最末尾 `OP_END_FACTORY_REG` 处发生一次。

**预期结果**：你能画出从 `REG_OP(MyAdd)` 到 `operator_creators_["MyAdd"]` 的完整调用链。注意，本步是源码阅读型实践，**实际编译/运行需在本地配好 CANN 构建环境后验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `OP_END_FACTORY_REG` 要用 `__COUNTER__` 拼接静态变量名？

> **参考答案**：一个 `.so` 里可能有几十上百个 `REG_OP`，每个都会生成一个静态 `OperatorCreatorRegister` 对象。如果变量名固定（如都叫 `g_register`），就会在同一作用域重复定义导致编译错误。`__COUNTER__` 是编译器内置的递增计数宏，保证每次展开得到唯一变量名。

**练习 2**：宏体里大量出现 `ATTRIBUTED_DEPRECATED(...)` 标记，例如 `set_input_x_by_name`。这传递了什么信息？

> **参考答案**：它表示被标记的旧重载（如接受 `std::string` 的版本）已废弃，推荐使用新重载（如接受 `const char *` / `AscendString` 的版本）。GE 在做 API 现代化迁移时会保留旧接口并打上废弃标记，向后兼容但提示用户迁移。

### 4.3 算子注册机制：OpDef 工厂与结构化原型

#### 4.3.1 概念说明

`REG_OP`/`Operator` 是「老」的图 IR 原型系统，它擅长表达输入/输出/属性的**签名**，但计算实现（tiling、AICore kernel、AICPU、HostCPU）要靠另外的机制挂接。新一代 AscendC 自定义算子采用了一套更完整、更结构化的原型对象——`OpDef`（命名空间 `ops`）。

`OpDef` 的特点是**链式 Builder**风格：一个 `OpDef` 对象直接持有算子类型、输入输出列表、属性列表，以及 `AICore()` / `AICPU()` / `HostCPU()` 这些「实现块」，可以在一处把「签名 + 实现」都描述清楚。它最终通过 `OpDefFactory::OpDefRegister` 登记进另一个工厂表。

可以这么理解两套体系的关系：

| 维度 | `REG_OP` / `Operator` | `OpDef` |
| --- | --- | --- |
| 风格 | 宏生成类 + 静态注册 | 链式 Builder 对象 |
| 擅长 | IR 层签名（输入/输出/属性/推导函数） | 完整描述（签名 + AICore/AICPU/HostCPU 实现指针 + tiling） |
| 注册落点 | `OperatorFactoryImpl::operator_creators_` | `OpDefFactoryImpl`（经 `OpDefFactory::OpDefRegister`） |
| 主要使用者 | 经典图编译、内置算子原型 | AscendC 自定义算子、gert 运行时 |

两套体系**并存**，都服务于「让 GE 知道某算子类型的契约」这一目标，只是粒度不同。

#### 4.3.2 核心流程

`OpDef` 的使用与注册流程：

1. **构造**：`OpDef def("AddCustom")`，构造时创建内部实现 `OpDefImpl` 并记下类型名。
2. **链式描述**：`def.Input("x").Output("y").Attr("axis").SetInferShape(fn)`，逐项填充输入/输出/属性/推导函数。
3. **登记**：算子定义侧把构造好的 `OpDef` 连同一个 creator 函数交给 `OpDefFactory::OpDefRegister(name, creator)`，由 `OpDefFactoryImpl`（单例）保存 `(类型名 → creator)`。
4. **查询**：需要时 `OpDefFactory::OpDefCreate(name)` 取出并构造对应的 `OpDef`。

#### 4.3.3 源码精读

`OpDef` 构造时建立 `OpDefImpl` 并记下类型；`Input` / `Output` / `Attr` 都委托给内部实现：

[graph_metadef/register/opdef/op_def.cc:18-20](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/register/opdef/op_def.cc#L18-L20)

[graph_metadef/register/opdef/op_def.cc:32-42](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/register/opdef/op_def.cc#L32-L42)

`SetInferShape` 同样是委托——注意它的参数类型 `gert::OpImplRegisterV2::InferShapeKernelFunc`，说明 `OpDef` 直接对接 gert 运行时的推导接口：

[graph_metadef/register/opdef/op_def.cc:64-66](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/register/opdef/op_def.cc#L64-L66)

`OpDefImpl` 的成员清晰展示了「一个结构化原型都装了什么」：算子类型 `op_type`、输入输出（`op_params`）、属性列表 `attrs`，以及 AICore/AICPU/HostCPU/MC2 等实现块和各类推导函数指针：

[graph_metadef/register/opdef/op_def_impl.h:153-176](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/register/opdef/op_def_impl.h#L153-L176)

其中 `OpAICoreDefImpl` 还承载了 tiling 函数、编译信息创建/销毁函数、算子支持性检查等更贴近「实现」的字段：

[graph_metadef/register/opdef/op_def_impl.h:120-133](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/register/opdef/op_def_impl.h#L120-L133)

最后看登记入口——`OpDefFactory::OpDefRegister` 把名字与 creator 交给单例 `OpDefFactoryImpl`：

[graph_metadef/register/opdef/op_def_factory.cc:22-24](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/register/opdef/op_def_factory.cc#L22-L24)

> 提示：`OpDefFactoryImpl` 的实现位于外部包（`base/asc/opdef/`，本仓库不含），其单例结构、按名查询的机制与 `OperatorFactoryImpl` 思路一致。

#### 4.3.4 代码实践

**实践目标**：对照 `OpDefImpl` 的字段，理解「结构化原型比 IR 签名多带了什么」。

**操作步骤**：

1. 打开 `graph_metadef/register/opdef/op_def_impl.h` 的 153～176 行（`OpDefImpl`）和 120～133 行（`OpAICoreDefImpl`）。
2. 列出 `OpDef` 比 `REG_OP` 多描述的字段，例如 `tiling_func`、`op_chk_support`、`ci_creator` 等。
3. 思考：为什么 AscendC 自定义算子需要这些额外字段？

**需要观察的现象**：`OpDef` 不止描述「端口长什么样」，还描述「在 AICore 上怎么 tiling、怎么检查算子是否被支持」。这正是 AscendC 算子入图（u8-l3）所需的完整信息。

**预期结果**：你能用一句话概括——`OpDef` = 算子签名 + 各后端（AICore/AICPU/HostCPU）的实现函数指针。本步为源码阅读型实践，**不需要运行**。

#### 4.3.5 小练习与答案

**练习 1**：`OpDef` 的 `Input()` 返回 `OpParamDef &`，这是链式调用的关键。这种 Builder 风格相比 `REG_OP` 的宏风格有什么好处？

> **参考答案**：Builder 风格是真正的运行期对象操作，可被 IDE 索引、可调试、可条件化构建（如根据配置决定是否注册某个实现块）；宏风格则要靠预处理器展开，错误信息晦涩、难以调试。但宏风格胜在声明紧凑、贴近「类型定义」的书写习惯。

**练习 2**：`OpDefFactory::OpDefRegisterV2` 标注了 `__attribute__((weak))`（弱符号）。这有什么用？

> **参考答案**：弱符号允许该符号在链接时「可有可无」——如果某个外部包提供了 `OpDefRegisterV2` 的强定义就用它，否则使用弱定义（通常是空实现）。这让 GE 在不强制依赖新版本外部包的前提下，向前兼容旧的注册方式。

### 4.4 GE 与算子仓协作：OpsProtoManager 的 dlopen 加载模型

#### 4.4.1 概念说明

到这里有个关键问题：上面这些 `REG_OP` / `OpDef` 注册代码，**在哪里编译、何时执行**？

答案是：它们被编译进**算子仓的 `.so` 动态库**，而 GE 本身**不内置算子定义**。运行时，GE 通过 `OpsProtoManager` 把这些 `.so` 用 `dlopen` 加载进进程；加载的瞬间，`.so` 里的静态注册对象自动构造，把算子原型写进 `OperatorFactoryImpl` / `OpDefFactoryImpl`。

这就是 GE 仓与算子仓的**解耦边界**：

- **GE 仓（本仓库）提供**：注册基础设施（宏、`OperatorFactoryImpl`、`OpDefFactory`）、加载器（`OpsProtoManager`）、以及 graph/compiler/runtime 全链路。
- **算子仓（外部，独立仓库）提供**：每个算子的 `REG_OP`/`OpDef` 定义、推导函数、tiling、kernel 实现，编译成 `.so`。
- **契约**：双方通过一组头文件约定接口（如 `register/op_def.h`、`graph/operator_reg.h`），GE 加载 `.so` 时按这套接口调用。

为什么要这样解耦？因为算子数量庞大且频繁更新（每种硬件、每个版本都不同），把它们与 GE 编译器强耦合会拖慢构建、阻碍独立演进。解耦后，换一组算子只需换 `.so`，GE 代码不动。

#### 4.4.2 核心流程

`OpsProtoManager` 的加载流程：

1. **配置路径**：调用方传入选项 map，其中 `ge.opsProtoLibPath` 指明算子 `.so` 所在目录（支持 `:` 分隔多目录）。
2. **枚举 `.so`**：在 `lib/<os_type>/<cpu_type>/` 子目录下收集所有 `.so`，**过滤掉** `rt2.0.so`、`rt.so`（这些是运行时库，不是算子原型库）。
3. **逐个 `dlopen`**：用 `RTLD_NOW | RTLD_GLOBAL | RTLD_NODELETE` 加载每个 `.so`。
4. **触发注册**：`dlopen` 执行 `.so` 的静态初始化，`OperatorCreatorRegister` 等对象的构造函数把算子原型写入注册表。
5. **后续查询**：GE 编译/执行链路通过 `OperatorFactory` / `OpDefFactory` 查询已注册的算子。

```
ge.opsProtoLibPath = /xxx/op_proto/lib
        │
        ▼
GetOpsProtoSoFileList  ──►  枚举 *.so，排除 *rt2.0.so / *rt.so
        │
        ▼
LoadOpsProtoPluginSo   ──►  dlopen(so, RTLD_NOW|RTLD_GLOBAL|RTLD_NODELETE)
        │  触发 .so 内静态对象构造
        ▼
OperatorCreatorRegister 构造  ──►  OperatorFactoryImpl::operator_creators_[type] = creator
```

#### 4.4.3 源码精读

`OpsProtoManager::Initialize` 是加载入口：取 `ge.opsProtoLibPath` 选项，调用 `LoadBuiltinOpsPluginSo`，并用 `is_init_` 保证只初始化一次：

[graph_metadef/graph/opsproto/opsproto_manager.cc:28-48](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/opsproto/opsproto_manager.cc#L28-L48)

枚举 `.so` 的逻辑在 `GetOpsProtoSoFileList`：它把多目录路径按 `:` 切分，进入 `lib/<os>/<cpu>/` 子目录收集 `.so`，并用 `IsEndWith` 过滤掉运行时库 `rt2.0.so` / `rt.so`——这两类 `.so` 不是算子原型库，不能当原型加载：

[graph_metadef/graph/opsproto/opsproto_manager.cc:100-126](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/opsproto/opsproto_manager.cc#L100-L126)

真正的 `dlopen` 在 `LoadOpsProtoPluginSo`：注意它打印的 warning「Shared library will not be checked. Please make sure that the source of shared library is trusted.」——加载外部 `.so` 是有安全责任的，调用方必须保证 `.so` 来源可信；`handles_` 记录句柄以便 `Finalize` 时 `mmDlclose`：

[graph_metadef/graph/opsproto/opsproto_manager.cc:128-148](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/opsproto/opsproto_manager.cc#L128-L148)

析构时 `OpsProtoManager` 会调用 `OperatorFactoryImpl::ReleaseRegInfo()` 清理注册信息，再 `Finalize` 关闭所有 `.so` 句柄——这与「加载即注册」是对称的「卸载即清理」：

[graph_metadef/graph/opsproto/opsproto_manager.cc:73-76](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/opsproto/opsproto_manager.cc#L73-L76)

#### 4.4.4 代码实践

**实践目标**：把「算子仓 `.so`」与「GE 加载器」之间的契约讲清楚，并验证加载流程的过滤规则。

**操作步骤**：

1. 在 `graph_metadef/graph/opsproto/opsproto_manager.cc` 中找到 `ge.opsProtoLibPath` 这个字符串，确认它是配置算子库路径的键。
2. 跟踪 `Initialize → LoadBuiltinOpsPluginSo → GetOpsProtoSoFileList → LoadOpsProtoPluginSo` 这条调用链，标注每一步做什么。
3. 用下面的命令确认「过滤规则」实际存在：

   ```bash
   grep -n "rt2.0.so\|rt.so\|IsEndWith" graph_metadef/graph/opsproto/opsproto_manager.cc
   ```

4. 回答：为什么要把 `rt2.0.so` / `rt.so` 排除在外？

**需要观察的现象**：`GetOpsProtoSoFileList` 把目录里所有 `.so` 收集后，**专门排除了两类运行时库**，再把剩下的当作算子原型库去 `dlopen`。

**预期结果**：你能写出一段话解释——算子原型 `.so` 与运行时 `.so` 共处一目录，但只有前者应被 `OpsProtoManager` 当原型加载；后者由运行时子系统（见单元 6）自行加载，二者职责不同，故需过滤。本步为源码阅读型实践，**实际加载需在装有昇腾驱动与算子仓的本地环境验证**。

#### 4.4.5 小练习与答案

**练习 1**：`dlopen` 用了 `RTLD_NODELETE` 标志，意味着什么？为什么算子库要这样？

> **参考答案**：`RTLD_NODELETE` 表示 `dlclose` 时**不真正卸载** `.so`，其代码与静态对象在进程生命周期内常驻。算子库这么做是因为：注册表里的函数指针（推导、tiling 等）可能在整个进程周期被随时调用，若 `.so` 被卸载，这些指针就会变成悬空指针，导致崩溃。

**练习 2**：如果 `ge.opsProtoLibPath` 指向一个空目录，GE 还能正常编译图吗？

> **参考答案**：`LoadBuiltinOpsPluginSo` 在找不到任何 `.so` 时只打印 warning 并返回（见 `GetOpsProtoSoFileList` 后的判空），不会直接失败。但此时注册表里没有任何算子原型，GE 在解析/校验/推导时遇到任何算子类型都会查不到，最终在具体算子处理处报错。所以「加载成功」不等于「能用」，关键看是否加载到了所需的算子原型。

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「追踪一个算子类型从定义到可查询」的全链路任务。

**任务背景**：用户写了一个 AscendC 自定义算子 `AddCustom`，源码在 `examples/custom_op/ascendc_add_custom/add_custom_kernel/custom_op.cpp`。请追踪它从「源码定义」到「GE 能查询到」的完整路径。

**操作步骤**：

1. **定义层（4.1）**：阅读 `custom_op.cpp` 的 80～86 行与 126～128 行，写出 `AddCustom` 的原型契约（类型名、输入/输出端口名、占位类型 `T`、推导函数名）。
2. **注册层（4.2）**：说明这些 `REG_OP` / `IMPL_OP` 代码会被编译进哪个产物（提示：自定义算子仓的 `.so`），并对照 `operator_reg.h` 的 `OP_END_FACTORY_REG` 说明它在加载时生成了哪个静态对象、最终写入 `OperatorFactoryImpl` 的哪个 map。
3. **加载层（4.4）**：说明 GE 进程启动时，是谁、用什么方式、在什么条件下把这个 `.so` 加载进来（提示：`OpsProtoManager::Initialize` + `ge.opsProtoLibPath` + `dlopen`），加载后又如何触发第 2 步的静态注册。
4. **查询验证**：写出一段伪代码，用 `OperatorFactory::IsExistOp("AddCustom")` 或 `OpDefFactory::GetAllOp()` 验证该算子已被注册（标注「待本地验证」）。

**预期产出**：

- 一张含四个阶段的流程图：**源码定义 → 编译成 `.so` → `dlopen` 加载 → 静态注册写表 → 按名查询**。
- 一句话总结 GE 与算子仓的边界：GE 提供注册基础设施与加载器，算子定义外置于独立 `.so`，二者通过约定头文件解耦。

> 如果本地有 CANN 构建环境，可进一步把 `examples/custom_op/ascendc_add_custom` 实际编译、部署，并在 GE 日志中观察 `OpsProtoManager plugin load ... successfully.` 字样，验证加载确实发生（**待本地验证**）。

## 6. 本讲小结

- **算子原型（OpProto）是类型层面的契约**，回答「某类算子有几个输入/输出、需要哪些属性、如何推导」；它与节点层面的 `OpDesc`（实例）是「类定义 vs 对象」的关系。`OpDesc.type` 的意义，正来自原型注册表。
- **经典注册用 `REG_OP` 宏**：宏展开生成算子类，链式 `.INPUT/.OUTPUT/.ATTR` 把端口写入基类 `Operator`；`OP_END_FACTORY_REG` 生成静态 `OperatorCreatorRegister`，其构造函数把「类型 → 创建器」写入 `OperatorFactoryImpl` 的全局 map。
- **新一代 `OpDef` 是结构化 Builder**：一个对象同时承载签名（输入/输出/属性）与各后端实现指针（AICore/AICPU/HostCPU 的 tiling、推导、检查函数），经 `OpDefFactory::OpDefRegister` 登记进 `OpDefFactoryImpl`，主要服务 AscendC 自定义算子。
- **GE 仓与算子仓解耦**：GE 不内置算子定义，只提供注册基础设施与加载器 `OpsProtoManager`；算子定义被编译成 `.so`，运行时由 `dlopen` 加载，加载瞬间触发静态注册。换算子只换 `.so`，GE 代码不动。
- **查询入口**：`OperatorFactory::CreateOperator/IsExistOp` 与 `OpDefFactory::OpDefCreate/GetAllOp` 是按类型名取用已注册原型的对外接口。
- **安全与生命周期**：加载外部 `.so` 需保证来源可信；`dlopen` 用 `RTLD_NODELETE` 让算子库常驻，析构时 `ReleaseRegInfo` + `Finalize` 对称清理。

## 7. 下一步学习建议

本讲把「算子类型如何被系统认知」讲清楚了，接下来可以沿两条线推进：

- **向编译链路走（单元 3、4）**：带着「算子原型已注册」的前提，进入 u3-l1（解析器框架）看 parser 如何把外部模型里的算子转成 AscendIR 的 `Node`，再到 u4（编译四阶段）看编译器如何依据原型做 shape 推导（u5-l2）与校验。届时你会发现，shape 推导正是在查询算子原型里登记的推导函数。
- **向自定义算子开发走（单元 8）**：如果你更关心「自己写一个算子」，直接去 u8-l3（自定义算子入图）和 u8-l1（自定义融合 Pass），那里会基于本讲的 `REG_OP`/`OpDef` 注册机制，手把手把一个 AscendC 算子从开发做到入图编译执行。

建议你顺手做一件事巩固本讲：在仓库里用 `grep -rn "OP_END_FACTORY_REG\|OpDefRegister" examples/` 数一数示例里注册了多少种算子类型，直观感受「原型注册」在真实工程里的规模。
