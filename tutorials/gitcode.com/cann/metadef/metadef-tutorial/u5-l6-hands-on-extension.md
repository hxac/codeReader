# 综合实践：扩展 metadef 的完整闭环

## 1. 本讲目标

本讲是整个学习手册的收官之作。前面五个单元分别讲解了 metadef 的数据结构（单元二）、执行上下文（单元三）、算子注册（单元四）和工程机制（单元五前五讲），本讲把这些知识串成一条完整的链路：

1. 会用 `ops::OpDef` 链式 API 为一个假想算子 `MyAdd` 定义原型（输入、输出、属性）。
2. 会用 `gert::OpImplRegisterV2`（`IMPL_OP` 宏）为 `MyAdd` 注册 InferShape 与 Tiling 实现函数。
3. 会用 `gert::OpTilingContextBuilder` 在单测中构造出 `TilingContext`，直接驱动 Tiling 函数跑起来并验证结果。
4. 会用 `build.sh` / `tests/run_test.sh -u` 完成编译与测试验证。
5. 会对照 README 的检查清单评估一次改动是否破坏 ABI、是否应该修改 metadef。

## 2. 前置知识

本讲假设你已学完 u4-l2（OpDef）、u4-l4（OpImplRegistry）、u5-l1（ContextBuilder）。开始前请回忆三个关键结论：

- **原型与实现分离**：`OpDef` 描述「算子长什么样」（有哪些输入/输出/属性），`OpImplRegisterV2` 描述「算子怎么干活」（InferShape/Tiling 等函数指针）。二者在 so 被加载时经静态对象构造期注册进入各自的注册表（见 u4-l3、u4-l4）。
- **上下文是裸内存上的类型化视图**：`TilingContext` 等上下文类零新增数据成员，框架侧由 Builder 在一块按配方填好的裸内存上 `reinterpret_cast` 出视图；Builder 的写入接口与上下文的读取接口互为镜像（见 u3-l3、u5-l1）。
- **ABI 兼容是硬约束**：metadef 被大量预编译 `.so` 依赖，对外结构体布局、枚举取值、函数签名都不能随意改动（见 u5-l4）。

本讲新增的一个关键认知：**三类扩展点的代码不一定要写进 metadef 仓**。`OpDef`、`IMPL_OP`、`ContextBuilder` 都是头文件 + 动态库形式的公共接口，算子仓（如 ops-nn）在自己的 so 里使用它们即可；只有「新增公共能力」才需要改 metadef 本身。本讲的综合实践因此刻意写成「新增一个单测文件」——零源码修改即可完成端到端闭环，这正是评估「是否真的需要改 metadef」的第一步。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `inc/external/asc/register/op_def.h` | asc 体系算子原型定义：`OpDef`/`OpParamDef`/`OpAttrDef` 链式 API |
| `inc/external/asc/register/op_def_registry.h` | `OP_ADD` 宏：把 OpDef 定义与实现注册装配起来的三种编译模式 |
| `inc/external/register/op_impl_registry.h` | `OpImplRegisterV2` 链式注册类与 `IMPL_OP` 宏 |
| `inc/external/register/op_impl_kernel_registry.h` | `OpImplFunctions(V2)` 函数集结构体（注册的最终产物形态） |
| `inc/register/op_impl_registry_api.h` | C 导出的跨 so 取数接口（两步协议的第二步） |
| `inc/external/base/context_builder/op_tiling_context_builder.h` | 框架/测试侧构造 `TilingContext` 的 Builder 声明 |
| `base/context_builder/op_tiling_context_builder.cc` | Builder 实现：槽位配方与 `Build()` |
| `tests/ut/base/testcase/context_builder_unittest.cc` | 官方单测范例：如何用 Builder 构造 TilingContext 并断言 |
| `inc/external/exe_graph/runtime/tiling_context.h` | Tiling 函数读写的上下文（本讲引用其 `GetTilingData` 等接口） |
| `README.md` / `CONTRIBUTING.md` | 修改 metadef 的影响评估清单与贡献流程 |

## 4. 核心概念与源码讲解

### 4.1 模块一：OpDef——为 MyAdd 定义算子原型

#### 4.1.1 概念说明

`ops::OpDef` 是 asc 新体系下描述算子原型的类（见 u4-l2）。它回答三个问题：

- 这个算子有几个输入/输出？各自叫什么名字、接受哪些 `DataType`/`Format`？
- 这个算子有哪些属性（attr）？属性是必填还是可选、什么类型？
- 这个算子的元信息推导函数（InferShape/InferDataType/InferShapeRange）挂在哪？

所有配置方法都返回引用，支持链式调用；对外类只持有 `unique_ptr<OpDefImpl> impl_`（pimpl），真实字段藏在实现类里，保证加字段不破坏 ABI。

#### 4.1.2 核心流程

一个算子原型的定义流程：

```text
构造 OpDef("MyAdd")
  ├─ Input("x1").Input("x2")        // 按名 GetOrCreate，同名合并
  ├─ Output("y")
  ├─ Attr("axis").Int()              // 默认 required
  ├─ Attr("name").AttrType(OPTIONAL).String()
  ├─ SetInferShape(MyAddInferShape)  // 直接挂元信息推导函数指针
  └─ AICore().SetTiling(MyAddTiling) // tiling 函数挂在 AICore 子对象上
```

注意一个容易混淆的细节：`SetInferShape`/`SetInferDataType` 挂在 `OpDef` 本体上，而 `SetTiling` 挂在 `OpAICoreDef`（`AICore()` 返回的子对象）上——因为 tiling 是 AI Core 执行模式特有的阶段，而 shape/dtype 推导是所有执行模式共有的。

#### 4.1.3 源码精读

`OpDef` 类的公共接口——`Input`/`Output`/`Attr` 定义端口与属性，`SetInferShape`/`SetInferDataType` 挂推导函数，`AICore()` 取执行模式子对象：

[inc/external/asc/register/op_def.h:484-505](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_def.h#L484-L505)

这段声明定义了 OpDef 的全部公共链式入口：`OpParamDef &Input(const char *name)` 与 `Output`、`OpAttrDef &Attr(const char *name)`、三个 `SetInfer*` 函数挂接点，以及 `AICore()/AICPU()/HostCPU()/MC2()` 四种执行模式子对象。

端口可选性由 `Option` 枚举表达：

[inc/external/asc/register/op_def.h:59-65](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_def.h#L59-L65)

`IGNORE=0, OPTIONAL=1, REQUIRED=2, DYNAMIC=3, VIRTUAL=4`——同一个枚举既用于端口（`ParamType`）也用于属性（`AttrType`）。这组取值是生成器与 JSON 交付件的契约，只能尾部追加。

`OpParamDef` 的端口约束链式方法：

[inc/external/asc/register/op_def.h:179-202](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_def.h#L179-L202)

`ParamType` 声明可选性、`DataType`/`Format` 及其 `List` 变体声明合法取值集合（经 DFS 全排列展开为所有合法组合）、`ValueDepend` 声明值依赖、`Follow` 声明元信息跟随。

`OpAttrDef` 的属性类型方法：

[inc/external/asc/register/op_def.h:331-349](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_def.h#L331-L349)

每个属性类型都有「无参（必填无默认）」与「带默认值」两个重载，如 `Int(void)` 与 `Int(int64_t value)`；`AttrType(Option)` 可把默认 required 改为 OPTIONAL。底层存储用 AnyValue（见 u2-l3）。

`OP_ADD` 宏的默认分支（未定义 OP_PROTO_LIB/OP_TILING_LIB 时）只注册原型创建函数：

[inc/external/asc/register/op_def_registry.h:51-58](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_def_registry.h#L51-L58)

立即调用的 lambda 在静态初始化期执行，按 `OpDefRegisterV2` 是否非空（弱符号，见 u4-l3）选择 V2 裸函数指针或 V1 回调路径，把「算子名 → 创建函数」登记进 OpDefFactory。算子作者需要写一个与算子同名的 `OpDef` 子类，`OP_ADD(MyAdd)` 即完成登记。

#### 4.1.4 代码实践

**实践目标**：为假想算子 `MyAdd` 写出完整的原型定义（示例代码，不属于仓库原有代码）。

```cpp
// 示例代码：my_add_op_def.h —— 算子原型定义
#include "register/op_def_registry.h"   // 间接包含 op_def.h 与 op_def_factory.h

class MyAdd : public ops::OpDef {
 public:
  MyAdd() : OpDef("MyAdd") {
    // 两个必选输入、一个输出，均接受 FLOAT，格式不限
    Input("x1").DataType({ge::DT_FLOAT}).Format({ge::FORMAT_ND});
    Input("x2").DataType({ge::DT_FLOAT}).Format({ge::FORMAT_ND});
    Output("y").DataType({ge::DT_FLOAT}).Format({ge::FORMAT_ND});
    // 一个必填 int 属性 + 一个可选带默认值的 string 属性
    Attr("axis").Int();
    Attr("tag").AttrType(ops::OPTIONAL).String("none");
    // 元信息推导直接挂接（函数实现见 4.2.4）
    SetInferShape(MyAddInferShape);
    SetInferDataType(MyAddInferDataType);
    // tiling 函数挂在 AICore 子对象上
    AICore().SetTiling(MyAddTiling);
  }
};

OP_ADD(MyAdd);
```

**操作步骤**：

1. 通读 `op_def.h` 中 `OpDef`/`OpParamDef`/`OpAttrDef` 三个类的公共方法，确认上面每个链式调用都有对应声明。
2. 把这段定义与仓库内 `op_def_registry.h` 的 `OP_ADD` 三个分支对照，弄清：默认分支只注册原型；`OP_PROTO_LIB` 分支额外注册 InferShape 三件套；`OP_TILING_LIB` 分支额外注册 Tiling/TilingParse 与 opcheck 函数（见 u4-l6）。

**需要观察的现象 / 预期结果**：源码阅读型实践，无需运行。自查标准：能不查资料说出 `SetInferShape` 与 `SetTiling` 分别挂在哪个对象上、为什么。若想编译验证，可把该头文件包含进 4.3 节的单测一起编译。

#### 4.1.5 小练习与答案

**练习 1**：把 `Attr("tag")` 写成 `Attr("tag").String("none")` 而不带 `AttrType(ops::OPTIONAL)`，属性的可选性是什么？
**答案**：仍是 required。带默认值不等于 optional，必须显式调用 `AttrType(Option::OPTIONAL)` 才能改变（见 u4-l2 的结论，`op_def.h:331` 的 `AttrType` 是唯一入口）。

**练习 2**：为什么 `Input("x1")` 连续调用两次不会产生两个同名输入？
**答案**：`Input` 是按名 GetOrCreate 语义——查到同名端口则返回已有 `OpParamDef` 供继续链式配置，只有不存在时才新建（u4-l2 精读过的合并语义）。

### 4.2 模块二：OpImplRegisterV2——注册 MyAdd 的实现函数

#### 4.2.1 概念说明

`gert::OpImplRegisterV2` 是算子「行为」的注册入口（见 u4-l4）。它把一组阶段函数指针（InferShape、Tiling、TilingParse 等）与一个算子名绑定，最终物化为 `OpImplFunctionsV2` 结构体存入注册表。算子侧通常不直接构造它，而是用 `IMPL_OP` 宏——在 `OP_TILING_LIB` 编译模式下，`OP_ADD` 宏内部也会自动构造一个 `OpImplRegisterV2` 并搬运 `OpDef` 上挂接的函数（见 4.1.3 第二段引用的 `op_def_registry.h:35-45`）。

#### 4.2.2 核心流程

注册链路全景（综合 u4-l4 结论）：

```text
IMPL_OP(MyAdd) 展开为静态对象
  gert::OpImplRegisterV2("MyAdd")           // 构造临时对象
    .InferShape(fn1).Tiling(fn2)            // 链式配置临时对象
  拷贝构造到静态变量                          // 拷贝时"非空才覆盖"写入单例（注册时机）
so 被 dlopen → 静态对象构造 → 注册表就绪
框架两步取数：dlsym GetRegisteredOpNum → dlsym GetOpImplFunctionsV2
  → 跨 so 拷出 TypesToImplV2 数组 → 合并进 OpImplSpaceRegistryV2
```

两条必须写在同一条链上的原因：注册发生在「临时对象拷贝到静态变量」的瞬间（拷贝构造触发注册，u4-l4 精读结论），拆成两条链会导致后一条的配置丢失。

#### 4.2.3 源码精读

阶段函数指针类型——每种推导/执行阶段一个 `UINT32 (*)(XxxContext *)` 裸函数指针：

[inc/external/register/op_impl_registry.h:62-78](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/op_impl_registry.h#L62-L78)

`InferShapeKernelFunc`、`TilingKernelFunc`、`InferDataTypeKernelFunc` 等签名在 `OpImplRegisterV2` 与 `OpImplKernelRegistry` 中各有一份别名（后者注释标明是给其他仓的过渡别名），本讲 MyAdd 需要其中的 InferShape 与 Tiling 两种。

链式配置方法：

[inc/external/register/op_impl_registry.h:87-98](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/op_impl_registry.h#L87-L98)

`InferShape(...)`、`Tiling(tiling_func, max_tiling_data_size)`（默认 2048 字节）、`InferOutDataTypeSameWithFirstInput()`（第一种输入 dtype 推所有输出的快捷规则，注册了它就无需再写自定义 InferDataType）。

`IMPL_OP` 宏展开：

[inc/external/register/op_impl_registry.h:148-157](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/op_impl_registry.h#L148-L157)

借助 `__COUNTER__` 生成唯一变量名，构造静态 `OpImplRegisterV2` 对象——静态对象构造期即注册期（u4-l4 结论：全程无锁，依赖注册只发生在静态初始化期的时序约定）。

注册的最终产物 `OpImplFunctions` 函数集结构体：

[inc/external/register/op_impl_kernel_registry.h:148-167](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/op_impl_kernel_registry.h#L148-L167)

每个字段对应一个阶段函数指针（`infer_shape`、`tiling`、`tiling_parse` 等）或依赖位图（`inputs_dependency`、`host_inputs`）；MyAdd 的注册结果最终就是把这个结构体中 `infer_shape` 与 `tiling` 两个字段填上函数指针。

V2 结构体的 ABI 守护三件套：

[inc/external/register/op_impl_kernel_registry.h:204-216](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/op_impl_kernel_registry.h#L204-L216)

`st_size = sizeof(OpImplFunctionsV2)`（让旧版本代码能识别结构体变大）、`version = OP_IMPL_MAIN_VERSION`、尾部 `reserved_[500]`——从保留字段尾部切出新字段实现安全演进，是 u5-l4 讲过的标准 ABI 演进手法。

跨 so 取数的 C 协议（两步协议第二步）：

[inc/register/op_impl_registry_api.h:17-39](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/register/op_impl_registry_api.h#L17-L39)

`extern "C"` 导出的 `GetRegisteredOpNum` 与 `GetOpImplFunctionsV2`：框架先问数量、申请内存，再按 `TypesToImplV2`（算子名 + 函数集）数组整块拷出。C 链接约定避开 C++ 名修饰，保证不同编译器版本间可互调。

#### 4.2.4 代码实践

**实践目标**：写出 MyAdd 的 InferShape 与 Tiling 实现函数并注册（示例代码）。

```cpp
// 示例代码：my_add_impl.cc —— 实现函数 + 注册
#include "register/op_impl_registry.h"
#include "exe_graph/runtime/tiling_context.h"
#include "exe_graph/runtime/infer_shape_context.h"

// InferShape：element-wise 逐元素加，输出 shape 取输入 0
static ge::graphStatus MyAddInferShape(gert::InferShapeContext *context) {
  const auto input_shape = context->GetInputShape(0);   // 返回指针，失败为 nullptr
  if (input_shape == nullptr) {
    return ge::GRAPH_FAILED;                             // 空值失败语义（u3-l4）
  }
  *context->GetOutputShape(0) = *input_shape;            // 输出 shape 由框架预分配，直接改写
  return ge::GRAPH_SUCCESS;
}

// InferDataType：输出 dtype 与输入 0 相同（也可用 InferOutDataTypeSameWithFirstInput 替代）
static ge::graphStatus MyAddInferDataType(gert::InferDataTypeContext *context) {
  const auto input_dtype = context->GetInputDataType(0);
  if (input_dtype == ge::DT_UNDEFINED) {
    return ge::GRAPH_FAILED;
  }
  return context->SetOutputDataType(0, input_dtype);
}

// Tiling：把两个 int32 参数写进 TilingData 字节流
struct MyAddTilingData {
  int32_t blockSize;
  int32_t coreNum;
};

static ge::graphStatus MyAddTiling(gert::TilingContext *context) {
  auto *tiling_data = context->GetTilingData<MyAddTilingData>();  // 覆写式写入，登记 sizeof(T)
  if (tiling_data == nullptr) {
    return ge::GRAPH_FAILED;
  }
  tiling_data->blockSize = 128;
  tiling_data->coreNum = 32;
  auto *workspace = context->GetWorkspaceSizes(1);                // 申请 1 个 workspace 槽
  if (workspace != nullptr) {
    workspace->GetData()[0] = 0UL;                                 // 无 workspace 需求
  }
  return ge::GRAPH_SUCCESS;
}

// 注意：注册必须写在同一条链上（拷贝构造触发注册，u4-l4）
IMPL_OP(MyAdd)
    .InferShape(MyAddInferShape)
    .InferDataType(MyAddInferDataType)
    .Tiling(MyAddTiling);
```

**操作步骤**：

1. 对照 [op_impl_registry.h:87-98](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/op_impl_registry.h#L87-L98) 确认每个链式方法签名。
2. 打开 [tiling_context.h:395-411](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_context.h#L395-L411)，确认 `GetTilingData<T>()` 是「取 `GetRawTilingData()` 指针 + 按类型覆写」的模板封装，`GetRawTilingData` 读的正是输出槽 `kOutputTilingData`（枚举值 3，见 [tiling_context.h:174-174](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/exe_graph/runtime/tiling_context.h#L174)）。

**需要观察的现象 / 预期结果**：此文件独立编译需链接 metadef 目标（见 u1-l2 的四个产物目标）。本讲的推荐做法是不单独编译，而是并入 4.3 节的单测目标 `ut_metadef`（tests/ut/base/testcase 用 GLOB 自动收集 `*.cc`，零 CMake 改动，见 u5-l5）。`IMPL_OP` 的注册效果（函数进入单例）可用 4.3 节的驱动方式间接验证。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `IMPL_OP(MyAdd).InferShape(fn)` 和后续的 `.Tiling(fn2)` 拆成两条语句，会发生什么？
**答案**：`IMPL_OP` 展开的是静态对象的拷贝构造初始化，注册只发生在这一瞬间；第二条链作用在静态对象上时注册已完成，其配置不会进入注册表（u4-l4 的「必须同一条链」结论）。

**练习 2**：`OpImplFunctionsV2` 尾部的 `reserved_[500]` 有什么用？
**答案**：预留演进空间——未来新增阶段函数指针可从保留字段头部切出，旧代码通过 `st_size` 识别结构体实际大小，从而保持 ABI 兼容（u5-l4 的保留字段演进手法）。

### 4.3 模块三：OpTilingContextBuilder——单测驱动 Tiling 函数

#### 4.3.1 概念说明

写好了 Tiling 函数，如何验证？metadef 官方的做法是：用 `gert::OpTilingContextBuilder` 在宿主内存上装配出一个真实的 `TilingContext`，把上下文指针传给自己的 Tiling 函数，再断言 TilingData 内容。这就是 `tests/ut/base/testcase/context_builder_unittest.cc` 的核心套路，也是算子仓单测（ut 测试 tiling）的标准范式。

Builder 与上下文严格互为镜像（u5-l1 结论）：`InputTensors(...)` 对应 `GetInputTensor(i)`，`TilingDataSize(n)` 对应 `GetRawTilingData()`，`AppendAttr(v)` 对应 `GetAttrs()->GetXxx(i)`。

#### 4.3.2 核心流程

Builder 的填槽配方（u5-l1 精读结论的直接引用）：

```text
BuildTilingContext() 的槽位序列：
  [0 .. inputs_num-1]                       输入（shape/tensor 槽）
  [inputs_num]                              输出 0 的 shape
  + 5 个隐藏槽：compile_info → platform_info → prepare-data
               → deterministic → deterministic_level
  + tiling 输出段（kOutputNum 个槽，kOutputTilingData=3、kOutputWorkspace=4 ...）
```

使用流程：

```text
构造 StorageShape/Tensor → OpTilingContextBuilder()
  .OpName/.OpType/.IONum(输入数, 输出数).AppendAttr(...)
  .TilingDataSize(n).CompileInfo(p1).PlatformInfo(p2).Deterministic(0)
  .InputTensors({...}).OutputTensors({...})
  .Build() → ContextHolder<TilingContext>
holder.GetContext() → TilingContext*
调用 MyAddTiling(ctx) → 断言 GetRawTilingData() 内容
```

#### 4.3.3 源码精读

Builder 的类声明与所有权约定：

[inc/external/base/context_builder/op_tiling_context_builder.h:25-37](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/base/context_builder/op_tiling_context_builder.h#L25-L37)

继承 CRTP 基类 `OpContextBuilderBase<OpTilingContextBuilder>`（公共配置入口 `OpName/OpType/IONum/AppendAttr` 都在基类，见 u5-l1）；注意注释强调：所有传入指针的所有权归调用者，生命周期必须长于 Build 产出的 ContextHolder。

`TilingData` 与 `TilingDataSize` 的两种风格：

[inc/external/base/context_builder/op_tiling_context_builder.h:66-86](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/base/context_builder/op_tiling_context_builder.h#L66-L86)

`TilingData(ptr)` 是外部传入、自管生命周期；`TilingDataSize(n)` 让 Builder 内部 `TilingData::CreateCap(n)` 分配、ContextHolder 析构时用删除器释放——单测场景推荐后者，免管内存。

`Build()` 返回 RAII 的 ContextHolder：

[base/context_builder/op_tiling_context_builder.cc:155-160](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/context_builder/op_tiling_context_builder.cc#L155-L160)

转发到 `BuildTilingContext()` 拿到 holder 实现再包成类型安全的 `ContextHolder<TilingContext>`。

槽位配方的实现（Builder 写入侧）：

[base/context_builder/op_tiling_context_builder.cc:23-47](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/context_builder/op_tiling_context_builder.cc#L23-L47)

这段是配方本体：输出值并入输入段尾部（第 30-32 行）、五个隐藏槽按固定顺序追加（第 33-38 行，每行注释标明槽位含义）、tiling 输出段填 `kOutputTilingData`/`kOutputWorkspace`/`kOutputSimtBlockDim`/`kOutputSimtGridDim`（第 39-44 行），最后 `BuildCtx` 按头部公式落盘 KernelRunContext。把它与 u3-l3 的 TilingContext 读取代码并排读，就是「镜像关系」的最好注脚。

官方单测的链式构建范例：

[tests/ut/base/testcase/context_builder_unittest.cc:400-422](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/context_builder_unittest.cc#L400-L422)

`CreateTilingContextOK` 用例从 `OpName` 一路链到 `Build()`：`IONum(4, 1)` 声明 4 输入 1 输出、`AppendAttr` 按序追加 9 个属性、`CompileInfo/PlatformInfo/Deterministic` 填隐藏槽、`InputTensors/OutputTensors` 挂张量。随后的断言（同文件第 433-481 行）逐项验证镜像读取：`GetInputShape(0)`、`GetAttrs()->GetInt(0)`、`GetRawTilingData()` 等。

#### 4.3.4 代码实践

**实践目标**：新增一个单测文件 `tests/ut/base/testcase/my_add_tiling_unittest.cc`，用 Builder 构造 TilingContext 并驱动 4.2.4 的 `MyAddTiling` 函数。

**操作步骤**：

1. 在 `tests/ut/base/testcase/` 下新建文件（GLOB 自动收集，无需改 CMake，见 u5-l5）。以下为示例代码框架：

```cpp
// 示例代码：my_add_tiling_unittest.cc
#include <gtest/gtest.h>
#include "exe_graph/runtime/tiling_context.h"
#include "base/context_builder/op_tiling_context_builder.h"
// #include "my_add_impl.h"  // 4.2.4 的实现与注册，可并入本目录或头文件引入

class UtestMyAddTiling : public testing::Test {};

TEST_F(UtestMyAddTiling, TilingWritesTilingData) {
  gert::StorageShape x({2, 3}, {2, 3});
  gert::Tensor x_tensor(x, {ge::FORMAT_ND, ge::FORMAT_RESERVED, ExpandDimsType()},
                        TensorPlacement::kOnHost, ge::DT_FLOAT, nullptr);
  gert::StorageShape y({2, 3}, {2, 3});
  gert::Tensor y_tensor(y, {ge::FORMAT_ND, ge::FORMAT_RESERVED, ExpandDimsType()},
                        TensorPlacement::kOnHost, ge::DT_FLOAT, nullptr);

  uint8_t compile_info[8] = {0};
  uint8_t platform_info[8] = {0};

  gert::OpTilingContextBuilder builder;
  auto holder = builder.OpName("my_add")
                    .OpType("MyAdd")
                    .IONum(2, 1)                       // 2 输入、1 输出
                    .AppendAttr(int64_t(1))            // "axis"（required）
                    .TilingDataSize(sizeof(MyAddTilingData))
                    .CompileInfo(compile_info)
                    .PlatformInfo(platform_info)
                    .Deterministic(0)
                    .InputTensors({&x_tensor, &y_tensor})
                    .OutputTensors({&y_tensor})        // 输出仅占 shape 槽，复用 y 的形状
                    .Build();
  auto *context = holder.GetContext();
  ASSERT_NE(context, nullptr);

  // 驱动被测函数
  ASSERT_EQ(MyAddTiling(context), ge::GRAPH_SUCCESS);

  // 验证 tiling 结果（镜像读取）
  auto *raw = context->GetRawTilingData();
  ASSERT_NE(raw, nullptr);
  const auto *data = reinterpret_cast<const MyAddTilingData *>(raw->GetData());
  EXPECT_EQ(data->blockSize, 128);
  EXPECT_EQ(data->coreNum, 32);
}
```

2. 编译并运行（见 u1-l2）：

```bash
export ASCEND_HOME_PATH=/usr/local/Ascend/ascend-toolkit/latest   # 先 source set_env.sh
bash build.sh                    # 主构建（可选，验证全仓可编译）
bash tests/run_test.sh -u        # UT 模式：构建 ut_metadef 并用 ctest -L ut 执行
```

3. 过滤单个用例（gtest 常规用法）：在 `build_gcov/tests/ut/base/` 下直接运行 `./ut_metadef --gtest_filter=UtestMyAddTiling.*`（具体路径以 run_test.sh 输出为准，待本地验证）。

**需要观察的现象**：

- 新文件被自动编进 `ut_metadef`（GLOB 收集，无 CMake 改动）。
- `MyAddTiling` 被真实调用，断言通过说明 Builder 写入 → TilingContext 读取 → TilingData 写回 → 再读取的全链路自洽。

**预期结果**：`ctest` 报告 `UtestMyAddTiling.TilingWritesTilingData` Passed。若 4.2.4 的实现代码同时放入该目录，`IMPL_OP` 的静态注册也会随 `ut_metadef` 进程启动而完成，可在用例中通过 `gert::OpImplRegistry` 查询验证（查询接口用法见 u4-l4；具体断言写法待本地验证）。

**运行环境提示**：若本机无昇腾环境，`run_test.sh` 依赖的 stub 机制会替换 slog/mmpa 等库（u5-l5），普通 x86 服务器通常可直接跑通 UT；无法运行时，本实践退化为「源码阅读型」：对照 [context_builder_unittest.cc:364-481](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/context_builder_unittest.cc#L364-L481) 逐行核对上述框架中每个 Builder 调用与断言的对应关系。

#### 4.3.5 小练习与答案

**练习 1**：单测里为什么必须让 `compile_info`/`platform_info` 指针在 `Build()` 之后仍然存活？
**答案**：Builder 不拷贝这些数据，只把指针填进隐藏槽（[op_tiling_context_builder.cc:33-34](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/context_builder/op_tiling_context_builder.cc#L33-L34)），头文件注释明确要求调用者保证指针生命周期长于 ContextHolder（[op_tiling_context_builder.h:30-36](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/base/context_builder/op_tiling_context_builder.h#L30-L36)）。

**练习 2**：`TilingDataSize(sizeof(MyAddTilingData))` 与 `TilingData(ptr)` 有何区别？单测应选哪个？
**答案**：前者由 Builder 分配并交给 ContextHolder 管理（实现见 [op_tiling_context_builder.cc:97-107](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/context_builder/op_tiling_context_builder.cc#L97-L107)，含自定义删除器），后者由调用者传入并自管生命周期；二者互斥、后调用覆盖前者。单测选前者，免管内存。

### 4.4 模块四：改动影响评估——改 metadef 前的检查清单

#### 4.4.1 概念说明

本讲的实践刻意只新增了测试文件，没有动 metadef 源码。真实工作中，何时才真的需要改这个仓？README 给出了明确判据与检查清单，CONTRIBUTING 给出了流程要求。这一模块把它们整理成可执行的评估动作——这是每个想给 metadef 提 PR 的人的必经步骤。

#### 4.4.2 核心流程

```text
产生一个想法
  → 判据一：ge/ops 上层接口是否已能满足？（多数场景是）
  → 判据二：是否是 ge 与 ops 的共同需求（跨仓公共需求）？
  → 是 → 设计接口（保持 ABI 兼容）→ 写单测 → build.sh 验证 → run_test.sh -u
  → 提 Issue 方案讨论（新增特性必须先讨论）→ 提 PR（Conventional Commits + pre-commit）
```

#### 4.4.3 源码精读

README「什么时候需要修改 metadef」判据：

[README.md:42-61](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/README.md#L42-L61)

两条「通常不需要改」的理由（上层接口成熟、ABI 兼容风险）、四类典型修改场景（新增公共基础类型、扩展算子注册能力、修复公共接口问题、跨仓协作），以及修改前的四条注意事项（先在 ge/ops 验证需求、评估影响、保持 ABI 兼容、充分测试依赖组件）。

提交前检查清单：

[README.md:84-93](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/README.md#L84-L93)

六项自查：真实需求、ABI 兼容、新增单测、`bash tests/run_test.sh -u` 全通过、更新 API 文档、commit message 符合规范。本讲 4.3.4 的实践正好覆盖第 3、4 两项。

CONTRIBUTING 的流程要求：

[CONTRIBUTING.md:12-16](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/CONTRIBUTING.md#L12-L16)

非简单 bug 修复（新增特性、新增接口、新增配置、改流程）必须先通过 Issue 讨论方案，否则可能被拒；提交前建议使用 pre-commit 工具（本仓配置了 codespell、clang-format 等 hooks，见仓库 skill `pre-commit-check` 的说明）。

#### 4.4.4 代码实践

**实践目标**：给自己的「假想改动」做一次影响评估。

**操作步骤**：

1. 假想一个改动：例如「给 `OpImplFunctionsV2` 增加一个新的阶段函数指针 `my_new_stage`」。
2. 按 u5-l4 的知识回答：应加在 `reserved_` 保留字段之前还是之后？`st_size`/`version` 是否需要变化？`abi_compatibility_for_exe_graph_unittest.cc` 的硬编码偏移断言是否会拦截它？
3. 对照 [README.md:84-93](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/README.md#L84-L93) 逐项打勾，写出缺失项。
4. 检查是否需要同步更新 `docs/api/` 下的文档。

**需要观察的现象 / 预期结果**：源码阅读 + 分析型实践。预期结论：新字段应从 `reserved_` 尾部（头部）切出、`st_size` 保持 `sizeof` 自动更新、旧 so 因读到更大的 `st_size` 而走兼容路径；若直接在结构体中部插入字段则会同时破坏布局与偏移断言，被 ABI 测试拦截。

#### 4.4.5 小练习与答案

**练习 1**：算子作者想新增一个只在自家算子仓使用的属性类型，应该改 metadef 的 `OpAttrDef` 吗？
**答案**：不应该。`OpAttrDef` 已支持的类型集（Bool/Float/Int/String 及各 List，见 [op_def.h:332-347](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_def.h#L332-L347)）覆盖了常见需求；私有仓需求不满足 README 的「跨仓公共需求」判据，应优先在上层仓用已有类型组合表达（如 ListListInt），或在算子仓内自定义序列化方案。

**练习 2**：为什么 `run_test.sh -u` 全通过是提 PR 的硬性检查项？
**答案**：UT 构建注入 ASan/LSan 且按标签全量执行（u5-l5），既验证功能又拦截内存错误；metadef 被 ge/ops 直接依赖，任何回归都会向下游放大，所以仓级门槛高于普通项目。

## 5. 综合实践

**任务：MyAdd 端到端闭环**。把本讲三个模块的产物组装成一个完整交付：

1. **原型**：按 4.1.4 编写 `MyAdd` 的 `OpDef`（2 输入、1 输出、1 个 required int 属性、1 个 optional string 属性）。
2. **实现**：按 4.2.4 编写 `MyAddInferShape`、`MyAddInferDataType`、`MyAddTiling` 与 `IMPL_OP(MyAdd)` 注册链。
3. **验证一（tiling 驱动）**：按 4.3.4 新增 `my_add_tiling_unittest.cc`，用 `OpTilingContextBuilder` 构造上下文、驱动 `MyAddTiling` 并断言 TilingData 字段。
4. **验证二（shape 推导驱动）**：仿照同文件中 `CreateInferShapeContextOK` 用例（[context_builder_unittest.cc:185](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/context_builder_unittest.cc#L185)），用 `OpInferShapeContextBuilder` 构造 `InferShapeContext` 驱动 `MyAddInferShape`，断言输出 shape 等于输入 shape（Builder 头文件位于 `inc/external/base/context_builder/`，接口与 tiling 版同构）。
5. **构建与测试**：`bash build.sh` 验证全仓编译，`bash tests/run_test.sh -u` 验证两个新用例通过。
6. **复盘**：对照 README 检查清单（4.4.3）写一份评估：本次改动是否触碰了 metadef 源码？若把 `MyAdd` 的 blockSize/coreNum 改为从 `PlatformInfo` 读取，需要新增 metadef 接口吗？（答案：不需要，`GetPlatformInfo` 返回的指针由上层解释，metadef 只负责透传。）

预期结果：两 个 gtest 用例均 Passed，且全程未修改 metadef 任何对外头文件与实现——这本身就是对「什么才需要改 metadef」最直观的答案。

## 6. 本讲小结

- **三类扩展点分工清晰**：`OpDef` 定义「算子长什么样」（端口、属性、元信息推导挂接），`OpImplRegisterV2`/`IMPL_OP` 注册「算子怎么干活」（阶段函数指针集），`OpTilingContextBuilder` 在宿主侧装配上下文供测试或框架驱动这些函数。
- **注册与取数协议**：`IMPL_OP` 展开为静态对象、注册发生在拷贝构造瞬间且必须同链；跨 so 经 `GetRegisteredOpNum` → `GetOpImplFunctionsV2` 两步 C 协议拷出 `TypesToImplV2` 数组。
- **Builder 与上下文互为镜像**：`BuildTilingContext` 的槽位配方（输入段 + 5 隐藏槽 + tiling 输出段）与 `TilingContext` 的 `Get*` 读取一一对应，单测可据此离线驱动 tiling/infer 函数。
- **单测即闭环**：testcase 目录 GLOB 自动收集新文件，`run_test.sh -u` 一条命令完成编译、执行与 ASan 检查。
- **改仓门槛**：README 判据（跨仓公共需求 + ABI 兼容）与检查清单、CONTRIBUTING 的 Issue 先行流程，构成修改 metadef 前的完整评估闭环。
- **ABI 演进正解**：新字段从 `reserved_` 切出、`st_size`/`version` 自动上报，中部插入与改枚举取值都是禁区（u5-l4）。

## 7. 下一步学习建议

本讲是学习手册的最后一讲，此后建议转向「以仓读仓」：

1. **横向对照真实算子仓**：取一个 gitcode 上的 CANN 算子仓（如采用类似机制的公开仓），对照本讲的 MyAdd 骨架观察真实算子的 OpDef 定义、tiling 实现与单测组织，体会 metadef 接口在下游的工程化用法。
2. **回读 ge 仓的消费侧**：`OpImplSpaceRegistryV2` 的合并、`OppSoManager` 的加载（u4-l5）在图引擎侧如何被触发，把注册链路的全景补完。
3. **深入生成器**：`op_def.h` 中大量 `friend class Generator/OpProtoGenerator` 预留的生成器体系（原型 JSON/代码生成）值得作为专题阅读入口。
4. **实践建议**：把综合实践扩展成「给 MyAdd 增加 `IMPL_OP` 注册表的查询断言」，验证静态注册在 ut_metadef 进程内真实生效，作为对 u4-l4 拷贝构造注册机制的最终检验。
