# u4-l1 register 模块总览：算子注册的入口

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `domi` 命名空间的来历，以及它与 `ge` 命名空间的分工。
2. 掌握 register 模块三个基础头文件（`register.h`、`register_types.h`、`register_fmk_types.h`）各自的职责与包含关系。
3. 理解 `REGISTER_CUSTOM_OP` 宏背后「静态对象构造期注册」的机制。
4. 区分 AutoMapping 系列四个自动映射函数的适用场景与参数差异。
5. 识别 `ATTRIBUTED_DEPRECATED` 弃用宏，并理解 metadef 如何在不破坏 ABI 的前提下演进接口。

## 2. 前置知识

在进入本讲前，先回顾两个背景概念：

- **模型转换与适配插件**。CANN 在把第三方框架（TensorFlow/Caffe/ONNX 等）训练出的模型转成昇腾可执行的 OM 模型时，需要把「原始框架算子」翻译成「适配 AI 处理器的算子」。这套翻译逻辑由各框架的**适配插件**承担，而编写适配插件所用的接口，就是本讲要读的 `register.h`。官方文档对这套流程的描述见 [docs/zh/api/ge_namespace/opregistrationdata/overview.md](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/docs/zh/api/ge_namespace/opregistrationdata/overview.md)。
- **domi 命名空间**。`domi` 是 CANN 里一个比 `ge` 更古老的前缀（源自早期模型转换框架的命名），如今 metadef 中所有「模型转换/算子注册」相关的类型都放在 `namespace domi` 下，而图与张量类型放在 `namespace ge` 下。两套命名空间在同一个头文件里经常同时出现，读代码时要看清类名前面是哪个命名空间。

再承接前面讲义的两个知识点：

- **AscendString（u2-l2）**：`register.h` 中新式接口大量使用 `const char_t *` 与 `ge::AscendString`，动机正是跨 ABI 传递字符串，这一点你已经熟悉。
- **AnyValue 与类型擦除（u2-l3）**：注册体系里「算子属性」最终以 AnyValue 承载；本讲关注的则是**注册入口的形状**——即属性怎么被声明、回调怎么被挂接。

另外一个 C++ 背景知识：**静态对象的构造期注册**。在 `.cpp` 文件的全局作用域定义一个带构造函数的静态对象，它会在 `main` 之前（更准确地说是该共享库被 `dlopen`/加载时）执行构造函数。如果构造函数里把「自己」登记进某个全局注册表，就实现了无需显式调用、插件一加载即完成注册的机制。`REGISTER_CUSTOM_OP` 宏正是这个套路。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `inc/external/register/register.h` | 214 | register 模块总入口：AutoMapping 函数、回调类型别名、`FrameworkRegistry`、`OpRegistrationData`、`OpReceiver` 与 `REGISTER_CUSTOM_OP` 宏 |
| `inc/external/register/register_types.h` | 66 | 基础设施：可见性宏、弃用宏、老式张量格式枚举 `domiTensorFormat_t` |
| `inc/external/register/register_fmk_types.h` | 24 | AI 框架类型枚举 `domi::FrameworkType`（CAFFE/MINDSPORE/TENSORFLOW…） |
| `inc/external/register/register_error_codes.h` | 35 | `domi::Status` 与错误码宏（register.h 的依赖项） |
| `inc/external/graph/types.h` | 451 | 意外地，`domi::ImplyType` 与 `domi::char_t` 定义在这里 |

一个值得先记住的结论：**register 模块在本仓库中只有「声明」，没有「实现」**。`OpRegistrationDataImpl`、`FrameworkRegistryImpl` 在 `register.h` 中只做了前置声明（第 86–87 行），全仓库 grep 不到它们的实现。这套接口的实现位于 CANN 其他组件（模型转换/omg 侧，具体仓库待确认），metadef 扮演的是「对外稳定契约的提供者」。这与 u1-l3 讲过的「inc/external 声明、base 实现」惯例不同，是识别 metadef 边界的重要样本。

## 4. 核心概念与源码讲解

### 4.1 register_types.h：register 模块的地基

#### 4.1.1 概念说明

`register_types.h` 是 register 模块中「被所有人包含、自己几乎不包含别人」的最底层头文件。它只做三件事：

1. 定义**可见性宏** `FMK_FUNC_HOST_VISIBILITY` / `FMK_FUNC_DEV_VISIBILITY`，用于把符号以默认可见性导出共享库；
2. 定义**弃用宏** `ATTRIBUTED_DEPRECATED`，用于在不删除旧接口的前提下提示调用方迁移；
3. 定义老式张量格式枚举 `domiTensorFormat_t`。

#### 4.1.2 核心流程

可见性宏的展开逻辑：

```text
若定义了 HOST_VISIBILITY 且编译器是 GCC：
    FMK_FUNC_HOST_VISIBILITY  →  __attribute__((visibility("default")))
否则：
    FMK_FUNC_HOST_VISIBILITY  →  空
```

弃用宏的展开逻辑：

```text
若定义了 NO_METADEF_ABI_COMPATIABLE：
    ATTRIBUTED_DEPRECATED(replacement)  →  空        （彻底关闭弃用告警）
否则（GNUC）：
    ATTRIBUTED_DEPRECATED(replacement)  →  __attribute__((deprecated("Please use " #replacement " instead.")))
```

也就是说，正常编译时，调用被弃用接口的代码会得到一条「请改用 xxx」的编译告警；只有显式定义 `NO_METADEF_ABI_COMPATIABLE`（表示不再追求 ABI 兼容的内部构建）才会静默。

#### 4.1.3 源码精读

可见性宏定义（HOST/DEV 两套，分别面向宿主侧与设备侧编译）：

[inc/external/register/register_types.h:L14-L23](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/register_types.h#L14-L23)
这段代码根据编译器宏决定是否给符号加 `visibility("default")` 属性，register.h 中所有导出类都会挂上这两个宏。

弃用宏，分 `__GNUC__`（GCC/Clang）与 MSVC（`__declspec`）两条分支：

[inc/external/register/register_types.h:L24-L38](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/register_types.h#L24-L38)
这段代码把「弃用并指路」封装成宏：`ATTRIBUTED_DEPRECATED(新接口)` 挂在旧接口声明前，编译器就会在调用处报出带新接口名字的告警。注意 `#replacement` 的字符串化——告警文本里的新接口名是拼出来的。

老式张量格式枚举：

[inc/external/register/register_types.h:L45-L63](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/register_types.h#L45-L63)
这段代码定义 `domiTensorFormat_t`，从 `DOMI_TENSOR_NCHW = 0` 排到 `DOMI_TENSOR_RESERVED`。它是模型转换早期使用的格式枚举，与 u2-l1 讲过的 `ge::Format` 是两套体系：`ge::Format` 用位域编码且仍在演进，而 `domiTensorFormat_t` 是纯递增枚举、属于老接口遗留。取值顺序同样是 ABI 契约，不可插队。

#### 4.1.4 代码实践

1. **实践目标**：验证弃用宏的实际告警效果。
2. **操作步骤**：写一个三行的示例程序（示例代码，非项目原有代码）：

   ```cpp
   #include "register/register_types.h"
   using namespace domi;
   domiTensorFormat_t f __attribute__((unused)) = DOMI_TENSOR_ND;
   ```

   然后在仓库根目录执行预处理/编译检查（只编译不链接，产物写到 /tmp）：

   ```bash
   g++ -c -I inc/external -o /tmp/reg_types.o /tmp/test_reg_types.cc
   ```

   再写一段「调用被弃用接口」的代码来触发 `ATTRIBUTED_DEPRECATED`（可直接复用 4.3 节 `GetOmOptype()` 的 `std::string` 旧重载）。
3. **需要观察的现象**：编译能否通过；调用弃用接口时告警文本里是否出现了它推荐的新接口名。
4. **预期结果**：头文件自包含性检查通过、无告警；调用弃用重载时输出形如 `'xxx' is deprecated: Please use xxx instead` 的告警。完整效果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `register_types.h` 不直接 `#include` 任何 graph 头文件？

**答案**：它是 register 模块的最底层依赖，被所有 register 头文件包含。若它反过来依赖 graph 头文件，会把 graph 的传递依赖引入每一个 register 编译单元，拉长编译时间并制造循环依赖风险。metadef 的头文件分层遵循「底层头文件零依赖」原则。

**练习 2**：`NO_METADEF_ABI_COMPATIABLE` 这个名字暗示了什么工程策略？

**答案**：弃用告警本身是「保持 ABI 兼容的渐进迁移」工具——旧接口保留（二进制还在）、新代码被引导走新接口。定义该宏会关闭告警，等价于声明「本次构建不需要维持新旧并存」（例如内部已全量迁移后的清理构建）。这印证了 u1-l1 讲过的：metadef 因被大量已编译组件依赖，接口演进必须以「不破坏既有二进制」为前提。

### 4.2 register.h（上）：AutoMapping 函数族与回调类型别名

#### 4.2.1 概念说明

适配插件的核心工作是把原始框架算子的属性搬到目标算子上。如果两边属性**同名同义**，开发者不必手写搬运代码——直接把框架提供的**自动映射函数**挂为解析回调即可；属性名对不上或需要修正格式时，再在自己的回调里「先调自动映射、后做修正」。

`register.h` 提供两组维度的自动映射：

| 维度 | 取值 |
| --- | --- |
| 源算子表示 | `google::protobuf::Message *`（老，直接读框架原始模型）或 `ge::Operator`（新，统一表示） |
| 是否动态输入/输出 | 静态端口（一一映射）或动态端口（个数由属性决定） |

两两组合正好得到四个函数，其中两个老组合已被弃用。

#### 4.2.2 核心流程

一次典型的 TensorFlow 算子适配流程（伪代码）：

```text
ATC 读入 TF 模型
  → 遇到算子 "SoftplusGrad"
  → 按 (FrameworkType=TENSORFLOW, OriginOpType="SoftplusGrad") 查注册表
  → 找到 ParseParamsByOperatorFn 回调（这里直接是 AutoMappingByOpFn）
  → 调用回调：op_src(TF 算子) 的属性逐个复制到 op(昇腾算子)
  → 返回 domi::SUCCESS，继续下一个算子
```

若算子带动态输入（如 TF 的 MapStage），框架无法从原型直接知道实际端口个数，需要开发者通过 `dynamic_name_attr_value` 告诉它「端口名 + 存个数的属性名」，这就是 Dynamic 系列函数存在的意义。

#### 4.2.3 源码精读

register.h 的包含列表，展示了 register 模块与 graph 模块的依赖方向：

[inc/external/register/register.h:L14-L29](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/register.h#L14-L29)
这段代码包含标准库与 `graph/operator.h`、`graph/ascend_string.h`、`graph/types.h` 以及 register 自己的三个基础头文件。可以看出：register 依赖 graph（Operator/AscendString/types），graph 不反向依赖 register。

protobuf 只做前置声明、不包含 protobuf 头文件：

[inc/external/register/register.h:L44-L48](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/register.h#L44-L48)
这段代码只声明 `google::protobuf::Message` 是一个类，从而让下方函数签名可以用它的指针，而无需把 protobuf 头文件拖进 metadef 的公共接口——既控制了依赖，也避免 protobuf 版本差异造成的 ABI 风险。这也是老 AutoMappingFn 系列最终被弃用的原因之一：`Message*` 无法提供类型化的属性访问。

四个自动映射函数的声明：

[inc/external/register/register.h:L70-L79](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/register.h#L70-L79)
这段代码按顺序声明：`AutoMappingByOpFn`（Operator→Operator 静态映射）、`AutoMappingByOpFnDynamic`（Operator 版动态映射）、被弃用的 `AutoMappingFn`（protobuf→Operator 静态）、被弃用的 `AutoMappingFnDynamic`（protobuf 版动态）。注意两者描述动态端口的方式完全不同：ByOp 版用结构体数组（4.2 节下文），protobuf 版用 `std::map<string, pair<string,string>>`，map 的 key 为 `"in"`/`"out"`，value 为（端口名, 个数属性名）。

动态输入/输出描述结构体：

[inc/external/register/register.h:L53-L69](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/register.h#L53-L69)
这段代码定义 `DynamicType`（输入/输出标记）与 `DynamicInputOutputInfo`（port 名及其长度 + attr 名及其长度，全部是 `const char_t *` 原始指针——延续 u2-l2 讲过的跨 ABI 字符串策略，不带 std::string 进接口）。`AutoMappingByOpFnDynamic` 的第三个参数就是它的数组。

子图索引自动映射（两个重载）：

[inc/external/register/register.h:L80-L84](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/register.h#L80-L84)
这段代码声明 `AutoMappingSubgraphIndex`：把子图内 Data/NetOutput 节点的索引翻译成父算子输入/输出索引，第二个重载通过 `int32_t &` 出参返回 Status，表达能力更强。它们经由 `REGISTER_AUTOMAPPING_SUBGRAPH_IO_INDEX_FUNC` 宏注册（见 4.3 节）。

七个解析回调类型别名：

[inc/external/register/register.h:L89-L99](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/register.h#L89-L99)
这段代码用 `using` 定义了适配插件可挂接的全部回调形状：单算子解析（ParseParamFunc / ParseParamByOpFunc）、融合算子解析（FusionParseParamFunc / FusionParseParamByOpFunc，注意入参是 vector——多个原始算子融合成一个昇腾算子）、子图后处理（ParseSubgraphFunc / V2 版用 AscendString）、算子转子图（ParseOpToGraphFunc）。每个别名都是 `std::function`，新旧交替与 u2-l2 的结论一致：新接口用 `ge::AscendString` 替换 `std::string`。

官方文档中的真实用法（属项目文档内容）：

[docs/zh/api/ge_namespace/opregistrationdata/AutoMappingFnDynamic.md:L33-L45](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/docs/zh/api/ge_namespace/opregistrationdata/AutoMappingFnDynamic.md#L33-L45)
这段示例展示了 MapStage（动态输入）的注册：`value["in"] = pair("values", "fake_dtypes")` 表示「名为 values 的动态输入，其个数存在属性 fake_dtypes 里」，然后把整个注册链 `REGISTER_CUSTOM_OP → .FrameworkType → .OriginOpType → .ParseParamsFn → .ImplyType` 一次写完。

#### 4.2.4 代码实践

1. **实践目标**：精确对比本讲任务要求的两个函数——`AutoMappingByOpFn` 与 `AutoMappingFnDynamic` 的参数差异。
2. **操作步骤**：打开 [inc/external/register/register.h:L70-L79](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/register.h#L70-L79)，逐参数填写下表（答案已给出，建议先遮住右列自己填）：

   | 对比项 | `AutoMappingByOpFn` | `AutoMappingFnDynamic` |
   | --- | --- | --- |
   | 源算子类型 | `const ge::Operator &` | `const google::protobuf::Message *` |
   | 目标算子 | `ge::Operator &`（两个函数相同） | `ge::Operator &` |
   | 动态端口描述 | 无（静态一一映射） | `std::map<string, pair<string,string>>`，key 为 "in"/"out" |
   | 端口位置参数 | 无 | `int32_t in_pos = -1, int32_t out_pos = -1`（默认 -1 表示不指定） |
   | 弃用状态 | 未弃用（推荐） | 已弃用（推荐改用 `AutoMappingByOpFnDynamic`） |
3. **需要观察的现象**：两者在「源算子表示」上的本质差异（Operator vs protobuf Message），以及这一差异如何决定了 ByOp 系列能用强类型结构体 `DynamicInputOutputInfo`、而 protobuf 系列只能用 string map。
4. **预期结果**：能不看资料复述上表，并能说出每组「静态/动态 × Operator/protobuf」四个函数各自的位置（第 70、71、73、75 行起的声明）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `AutoMappingFn` 接收 `const google::protobuf::Message *` 指针而不是引用？

**答案**：protobuf 的 Message 是多态基类，实际传入的是各框架算子的派生 Message；用基类指针才能统一承载任意框架的原始算子对象。同时 metadef 不包含 protobuf 头文件（只前置声明），指针恰好是「不完整类型也能用」的形态——引用同样可以用于不完整类型，但指针还允许传空值表示无源算子的场景。

**练习 2**：`FusionParseParamByOpFunc` 的第一个参数为什么是 `const std::vector<ge::Operator> &` 而 `ParseParamByOpFunc` 是单个 `const ge::Operator &`？

**答案**：融合场景下，多个原始框架算子（例如 TF 的多个小算子）会被合并成一个昇腾算子，解析回调需要同时读到**所有**源算子的属性，因此入参是 vector；非融合场景只有一个源算子，单个引用即可。两者的输出都是同一个 `ge::Operator &`。

**练习 3**：`ParseSubgraphFunc` 与 `ParseSubgraphFuncV2` 只差在第一个参数类型（`const std::string &` vs `const ge::AscendString &`），为什么必须并存？

**答案**：`std::string` 出现在对外接口会引入标准库 ABI 脆弱性（u2-l2 讲过 `_GLIBCXX_USE_CXX11_ABI` 等问题），但直接删掉旧签名会破坏已编译插件（二进制不兼容）。于是旧签名挂 `ATTRIBUTED_DEPRECATED`、保留声明，新签名走 AscendString，这正是 metadef 接口演进的标准姿势。

### 4.3 register.h（下）：OpRegistrationData、OpReceiver 与注册宏

#### 4.3.1 概念说明

有了回调类型，还需要一个「把算子名、框架类型、回调打包登记」的载体，这就是 `domi::OpRegistrationData`；还需要一个「在库加载时机触发登记」的钩子，这就是 `domi::OpReceiver` 与 `REGISTER_CUSTOM_OP` 宏。三者合起来构成完整的注册链：

```text
REGISTER_CUSTOM_OP("SoftplusGrad")            ← 展开为一个静态 OpReceiver 对象
    .FrameworkType(TENSORFLOW)                ← 链式配置 OpRegistrationData
    .OriginOpType("SoftplusGrad")
    .ParseParamsByOperatorFn(AutoMappingByOpFn)
    .ImplyType(ImplyType::TVM);
        │ 插件 so 被 dlopen 时静态对象构造
        ▼
OpReceiver(reg_data) 把注册数据交给注册表（实现在 CANN 其他组件）
        ▼
模型转换时按 (FrameworkType, OriginOpType) 查回回调并调用
```

注意 `OpRegistrationData` 与单元四后续要讲的 `OpDef`（u4-l2）是**两套并行的注册体系**：前者面向「模型转换适配插件」（domi 老体系），后者面向「昇腾算子原型定义」（asc 新体系）。本讲聚焦前者。

#### 4.3.2 核心流程

`REGISTER_CUSTOM_OP(name)` 的三层宏展开（这是 C++ 中规避「同一行多次展开同名静态变量」的惯用法）：

```text
REGISTER_CUSTOM_OP(name)
  → REGISTER_CUSTOM_OP_UNIQ_HELPER(__COUNTER__, name)   // 用 __COUNTER__ 生成唯一编号
  → REGISTER_CUSTOM_OP_UNIQ(ctr, name)
  → static OpReceiver register_op##ctr = OpRegistrationData(name)
```

`__COUNTER__` 是编译器内置宏，每次展开自增，保证同一翻译单元里多次使用宏也不会重名。`OpRegistrationData(name)` 临时对象先被链式 `.` 调用填充，再交给 `OpReceiver` 的构造函数完成登记，随后临时对象析构——所以 `OpRegistrationData` 内部必须以 `shared_ptr<OpRegistrationDataImpl>` 持有真实数据（pimpl 惯例），拷贝的是控制权而不是内容。

#### 4.3.3 源码精读

实现类前置声明与注册数据类：

[inc/external/register/register.h:L86-L87](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/register.h#L86-L87)
这段代码前置声明 `OpRegistrationDataImpl` 与 `FrameworkRegistryImpl`，为 pimpl 模式做铺垫——真实实现类不在本仓库（前文已 grep 确认），本头文件只负责对外契约。

`OpRegistrationData` 的链式配置接口（节选关键部分）：

[inc/external/register/register.h:L121-L155](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/register.h#L121-L155)
这段代码声明注册数据类：构造函数接收算子名（`const char_t *` 新式与 `const std::string &` 旧式并存，后者弃用）；随后是返回自身引用的链式方法 `FrameworkType` / `OriginOpType`（可多次调用，登记多个原始算子名）/ `ParseParamsFn` 等四族解析回调挂接 / `ParseSubgraphPostFn` / `ImplyType`。全部返回 `OpRegistrationData &`，这就是 `REGISTER_CUSTOM_OP(...).A().B().C()` 链式语法的来源。

`domi::ImplyType` 的真实定义处（不在 register 目录！）：

[inc/external/graph/types.h:L433-L444](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/graph/types.h#L433-L444)
这段代码在 `namespace domi` 中定义 `ImplyType`（BUILTIN/TVM/CUSTOM/AI_CPU/HCCL 等执行形态）。它定义在 graph/types.h 而非 register 头文件中，register.h 依赖第 29 行 `#include "graph/types.h"` 才能用它——这是「头文件包含关系图」作业里最容易画错的一条边。同文件第 445 行还有 `using char_t = ge::char_t;`，即 domi 命名空间的 `char_t` 也是从这里来的。

配套的 Get 查询接口与 pimpl 成员：

[inc/external/register/register.h:L167-L192](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/register.h#L167-L192)
这段代码先给出 `InputReorderVector` / `ParseOpToGraphFn` 等剩余配置项，再是一组 Get 方法供注册表消费；私有部分是 `std::shared_ptr<OpRegistrationDataImpl> impl_`，并用 friend 把 `OpRegistry`、`OpRegistrationTbe`、`ge::TBEPluginManager` 列为友元，允许它们直接读取 impl。pimpl + shared_ptr 的组合让对外类大小恒为一个指针，改内部字段不破坏 ABI（与 u2-l2 讲过的 `AscendString` 唯一成员是 `shared_ptr<std::string>` 完全同一手法）。

注册表单例与静态注册钩子：

[inc/external/register/register.h:L101-L119](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/register.h#L101-L119)
这段代码声明 `FrameworkRegistry` 单例（删除拷贝、私有构造、`Instance()` 取实例、按 FrameworkType 存取子图索引映射函数，同样 pimpl 到 FrameworkRegistryImpl）与 `AutoMappingSubgraphIOIndexFuncRegister`——后者的构造函数就是 4.2 节 `REGISTER_AUTOMAPPING_SUBGRAPH_IO_INDEX_FUNC` 宏的触发点，又一个「静态对象构造期注册」案例。

OpReceiver 与命名空间桥接：

[inc/external/register/register.h:L194-L203](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/register.h#L194-L203)
这段代码声明 `OpReceiver`（构造函数接收 `OpRegistrationData &` 并完成登记，正是注册链的最后一环），随后在 `namespace ge` 中 `using` 出 `OpRegistrationData`/`OpReceiver` 两个别名——老代码写 `ge::OpRegistrationData` 也能编译，这是 metadef 平滑命名空间迁移的兼容层。

三个注册宏的最终定义：

[inc/external/register/register.h:L205-L211](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/register.h#L205-L211)
这段代码定义 `REGISTER_CUSTOM_OP`（三层 UNIQ_HELPER 展开出唯一命名的静态 OpReceiver）与 `REGISTER_AUTOMAPPING_SUBGRAPH_IO_INDEX_FUNC`（静态 AutoMappingSubgraphIOIndexFuncRegister 对象）。`__attribute__((unused))` 抑制「定义了却没人用」的告警——静态注册对象天生就「不被显式引用」。

FrameworkType 枚举（register.h 第 26 行包含的依赖头文件）：

[inc/external/register/register_fmk_types.h:L16-L22](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/register_fmk_types.h#L16-L22)
这段代码定义 `domi::FrameworkType`：CAFFE=0、MINDSPORE=1、TENSORFLOW=3、ANDROID_NN=4、ONNX=5，末位 FRAMEWORK_RESERVED。注意 2 被跳过（历史上被移除的框架，枚举值不能再复用——复用会让旧二进制里的模型解析错框架），这再次体现「枚举取值是 ABI 契约」。

domi::Status 与错误码（register.h 第 25 行包含的依赖头文件）：

[inc/external/register/register_error_codes.h:L17-L31](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/register_error_codes.h#L17-L31)
这段代码定义错误码位拼接宏（子系统 ID 移 24 位、模块 ID 移 16 位、低 16 位是值），以及 `domi::Status = uint32_t` 和 SUCCESS/FAILED/PARAM_INVALID。AutoMapping 函数族返回的正是这个 Status，注意它与 `ge::graphStatus`（u3 系列讲过）是两套错误码体系，不要混用比较。

#### 4.3.4 代码实践

1. **实践目标**：亲手验证 `REGISTER_CUSTOM_OP` 的宏展开结果，理解「静态对象构造期注册」。
2. **操作步骤**：
   1. 写一个示例文件 `/tmp/test_register_macro.cc`（示例代码，非项目原有代码）：

      ```cpp
      #include "register/register.h"

      domi::Status ParseSoftplusGrad(const ge::Operator &op_src, ge::Operator &op) {
        return domi::AutoMappingByOpFn(op_src, op);
      }
      REGISTER_CUSTOM_OP("SoftplusGrad")
          .FrameworkType(domi::TENSORFLOW)
          .OriginOpType("SoftplusGrad")
          .ParseParamsByOperatorFn(ParseSoftplusGrad)
          .ImplyType(domi::ImplyType::TVM);
      ```

   2. 只做预处理，观察宏展开（`-E` 只输出文本，不产生多余文件）：

      ```bash
      g++ -E -I inc/external /tmp/test_register_macro.cc | grep -A2 -B2 "register_op"
      ```

   3. 再做编译检查（`-c` 只编译不链接；不链接是因为 OpReceiver 的构造符号在 CANN 其他组件中，本仓库本就没有实现）：

      ```bash
      g++ -c -I inc/external -o /tmp/test_register_macro.o /tmp/test_register_macro.cc
      ```
3. **需要观察的现象**：预处理输出中 `REGISTER_CUSTOM_OP("SoftplusGrad")` 变成了什么样的静态变量定义；变量名里的编号来自哪里；编译阶段是否通过（链接阶段预期会报 OpReceiver/OpRegistrationData 相关符号未定义，属正常现象，恰好证明实现不在本仓库）。
4. **预期结果**：预处理输出可见形如 `static domi::OpReceiver register_op0 __attribute__((unused)) = domi::OpRegistrationData("SoftplusGrad").FrameworkType(...)...` 的完整链式表达式；`-c` 编译通过。完整效果待本地验证（本环境不执行编译）。

#### 4.3.5 小练习与答案

**练习 1**：`REGISTER_CUSTOM_OP` 为什么需要三层宏（`REGISTER_CUSTOM_OP` → `UNIQ_HELPER` → `UNIQ`）？

**答案**：为了让 `__COUNTER__` 在展开时被求值并拼进变量名。宏参数里的 `__COUNTER__` 如果只经过一层宏直接与 `##` 拼接，某些编译器不会先求值；多套一层「先作为参数传入、再拼接」的中间宏，可保证计数器值先固化再拼接。此外不同次调用得到 `register_op0`、`register_op1`……避免同一翻译单元内重名。

**练习 2**：`OpRegistrationData` 的友元列表里有 `ge::TBEPluginManager`，但 `register.h` 第 41 行只写了 `class TBEPluginManager;` 前置声明。这合法吗？为什么？

**答案**：合法。friend 声明本身只需要类名可被识别，不需要完整类型定义；真正访问其 private 成员发生在 TBEPluginManager 的成员函数里，而那部分代码在别的仓库编译时自然能看到完整的 OpRegistrationData 定义。这也是头文件之间「最小依赖」的又一体现。

**练习 3**：`FrameworkType` 枚举里没有值 2，如果新接一个框架，能把它填在 2 上吗？

**答案**：不能。枚举的数值一旦发布就成为二进制契约：已编译的老插件、老的序列化数据里 TENSORFLOW 恒为 3。若把新框架填在 2 且不挪动现有值，虽然不会挪动既有取值，但历史上 2 被跳过说明曾有框架被删除；正确做法永远是尾部追加（用 FRAMEWORK_RESERVED 之前的位置或继续递增），绝不复用或插入历史值。

## 5. 综合实践

**任务：绘制 register 头文件家族的包含关系图，并给出 AutoMapping 双函数的参数差异报告。**

1. **实践目标**：把本讲三个核心头文件 + 两个「意外来源」依赖的关系固化为一张图，完成本讲规格中的练习任务。
2. **操作步骤**：
   1. 对每个头文件执行 `grep -n '#include' <file>`，列出直接依赖；
   2. 追踪传递依赖，特别注意两条隐蔽边：`domi::ImplyType`/`domi::char_t` 来自 `graph/types.h`（不是 register 目录）；`domi::Status` 来自 `register_error_codes.h`；
   3. 画出如下结构（请自行补全每条边上的「被依赖符号」标签）：

      ```text
      register.h ──┬─> graph/operator.h        (ge::Operator, ge::Graph)
                   ├─> graph/ascend_string.h   (ge::AscendString)
                   ├─> graph/types.h           (domi::ImplyType, domi::char_t)   ← 隐蔽边
                   ├─> register_error_codes.h  (domi::Status, DECLARE_ERRORNO)   ← 隐蔽边
                   ├─> register_fmk_types.h    (domi::FrameworkType)
                   └─> register_types.h        (FMK_FUNC_*_VISIBILITY, ATTRIBUTED_DEPRECATED, domiTensorFormat_t)

      register_fmk_types.h ─> <string>（仅标准库）
      register_types.h     ─> 无 include（零依赖地基）
      ```

   4. 结合 4.2.4 节的表格，写一份 `AutoMappingByOpFn` vs `AutoMappingFnDynamic` 的参数差异报告（源算子类型、动态端口描述方式、默认参数、弃用状态四个维度）。
3. **需要观察的现象**：依赖图是否呈现「register_types.h 零依赖 → register_fmk_types.h 仅标准库 → register.h 依赖 graph」的严格分层；有没有出现 graph 反向依赖 register 的边（应该没有）。
4. **预期结果**：得到一张五个头文件、方向全部从 register 指向 graph/标准库的有向无环图，以及一份四维度差异表。全部结论可仅凭 `grep` 与阅读头文件在本地复现。

## 6. 本讲小结

- `domi` 命名空间承载模型转换时代的算子注册体系，`register.h` 是它的总入口；`ge` 命名空间的 `using` 别名（第 200–203 行）让两套命名空间可以平滑互操作。
- register 头文件严格分层：`register_types.h` 零依赖（可见性宏、弃用宏、老格式枚举）→ `register_fmk_types.h`（FrameworkType）→ `register.h`（依赖 graph 的 Operator/AscendString/types）。
- AutoMapping 四函数 = 「源算子表示（protobuf/Operator）× 端口形态（静态/动态）」，protobuf 系已弃用，推荐 ByOp 系；动态端口描述在两系中形态不同（结构体数组 vs string map）。
- `REGISTER_CUSTOM_OP` 通过 `__COUNTER__` 三层宏展开出静态 `OpReceiver` 对象，在 so 加载期构造并完成注册——静态注册模式与 u3-l5 的 TilingDef 注册同源。
- 关键实现（`OpRegistrationDataImpl`/`FrameworkRegistryImpl`、AutoMapping 函数体）不在本仓库，metadef 只提供头文件契约；`domi::ImplyType`、`domi::Status` 分别定义在 `graph/types.h` 与 `register_error_codes.h` 这两个容易画漏的位置。
- `ATTRIBUTED_DEPRECATED` + 保留旧签名 = metadef 在 ABI 约束下演进接口的标准手法（同一手法贯穿 AscendString、ParseSubgraphFuncV2、GetOmOptype）。

## 7. 下一步学习建议

本讲讲清了「模型转换适配插件」这条老注册链的入口。下一讲 **u4-l2「OpDef：算子原型定义」** 将进入昇腾算子原型这条新链：精读 `inc/external/asc/register/op_def.h` 的链式构建器 API，看它如何定义输入/输出/属性及各类约束，并与本讲的 `OpRegistrationData` 对照——两套链式语法形似而目的不同（一个描述「如何从外部框架翻译进来」，一个描述「算子本身长什么样」）。预习时可以先浏览 `inc/external/asc/register/` 目录结构，并回顾 u2-l3 的 AnyValue——OpDef 的属性默认值正是以 AnyValue 承载的。
