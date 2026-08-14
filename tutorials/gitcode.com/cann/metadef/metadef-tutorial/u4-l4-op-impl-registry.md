# OpImplRegistry 与 OpImplSpaceRegistry：算子实现注册

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 op_impl_registry 体系的四层分工：注册入口（OpImplRegisterV2）、本地注册表（OpImplRegistry）、C 导出接口（op_impl_registry_api.h）、聚合层（OpImplSpaceRegistry 与 OpImplRegistryHolder）。
2. 掌握 `IMPL_OP` 宏背后「链式配置 + 拷贝构造触发注册」的核心技巧，理解注册为什么发生在静态初始化期。
3. 逐项说出 `OpImplFunctionsV2` 中算子实现函数集（InferShape、Tiling、TilingParse 等）的函数指针类型与挂接方式。
4. 能写出一个最小的「假算子实现」：定义 InferShape 与 Tiling 函数，用链式 API 挂到 OpImplFunctions 上并注册进 registry，再用单测验证。

本讲承接 u4-l1（register 模块总览，静态对象构造期注册模式）与 u3-l2（ExtendedKernelContext，即本讲中各实现函数收到的上下文类型）。

## 2. 前置知识

- **算子实现函数集**：一个算子在图编译/执行的不同阶段需要不同的回调——推导输出形状（InferShape）、推导数据类型（InferDataType）、切分参数计算（Tiling）、编译期信息解析（TilingParse）等。metadef 把这些回统一组函数指针打包在一个结构体里，按算子类型（op_type，如 `"Add"`）索引。
- **静态对象构造期注册**（u4-l1、u4-l3 已建立）：在 .so 里放一个带构造/拷贝构造副作用的静态对象，so 被 `dlopen` 时进程会执行这些静态初始化代码，注册随之完成——不需要外部显式调用注册函数。
- **函数指针即 ABI 契约**：实现函数以 `UINT32 (*)(XxxContext *)` 这类裸 C++ 函数指针跨 so 传递，所以结构体布局（字段顺序、大小）就是 ABI，只能尾部追加。
- **Meyers 单例**：函数内 `static` 局部变量，首次执行到时构造，C++11 起由编译器保证线程安全（magic statics）。
- **一处勘误**：规格里的「op_impl_functions.cc 中定义的函数指针类型清单」实际有偏差——[base/registry/op_impl_functions.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_functions.cc) 实现的是三个 C 导出函数；真正的函数指针**类型定义**在 [inc/external/register/op_impl_registry.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/op_impl_registry.h)（`OpImplRegisterV2` 内的别名）和 [inc/external/register/op_impl_kernel_registry.h](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/op_impl_kernel_registry.h)（`OpImplFunctions`/`OpImplFunctionsV2` 结构体）。本讲按真实代码梳理。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `inc/external/register/op_impl_registry.h` | 注册入口：`OpImplRegisterV2` 链式 API、全部函数指针类型别名、`IMPL_OP` 宏 |
| `inc/external/register/op_impl_kernel_registry.h` | 函数集载体：`OpImplFunctions` / `OpImplFunctionsV2` 结构体与位标志操作 |
| `inc/register/op_impl_registry_base.h` | 本地注册表声明：`OpImplRegistryBase` 抽象接口 + `OpImplRegistry` 单例 |
| `base/registry/op_impl_registry.cc` | 单例实现 + 拷贝构造触发的 `RegisterOpImplToRegistry` 合并逻辑 |
| `base/registry/op_impl_functions.cc` | 三个 C 导出函数：`GetRegisteredOpNum` / `GetOpImplFunctions(V2)` |
| `inc/register/op_impl_registry_api.h` | C ABI 契约：`TypesToImpl(V2)` 结构体与导出函数原型 |
| `inc/register/op_impl_registry_holder_manager.h` | Holder：一个 so 的注册表快照 + 句柄生命周期管理 |
| `base/registry/op_impl_registry_holder_manager.cc` | Holder 实现：dlsym 取导出符号、按 so 内容去重 |
| `inc/register/op_impl_space_registry.h` | Space 聚合层声明：跨 so 合并查询入口 |
| `base/registry/op_impl_space_registry_v2_impl.cc` | Space 合并实现：`MERGE_FUNCTION` 宏逐函数融合 |

## 4. 核心概念与源码讲解

### 4.1 全景：四层分工（api / base / holder / space）

#### 4.1.1 概念说明

算子实现散落在很多个 so 里（内置算子包、自定义算子包、主程序自身）。metadef 用四层结构把它们组织起来：

1. **注册入口层**：算子作者写 `IMPL_OP(Add).InferShape(f1).Tiling(f2);`，一行代码完成注册。
2. **本地注册表层（base）**：每个 so 内部各有一个 `OpImplRegistry` 单例，存「op_type → 函数集」的 map。
3. **C 导出层（api）**：每个 so 对外暴露三个 C 函数（`GetRegisteredOpNum` 等），让**别的模块**能用 `dlsym` 读走这个 so 的全部注册内容——这是跨 so 边界的唯一通道，纯 C 接口保证 ABI 稳定。
4. **聚合层（holder + space）**：框架侧把每个加载的 so 的注册内容装进一个 `OpImplRegistryHolder`，再由 `OpImplSpaceRegistry` 把多个 Holder 按 op_type 逐函数融合成一张全局视图。

#### 4.1.2 核心流程

一次完整的「注册 → 被框架发现」流程：

```
算子 so 被编译
  └─ IMPL_OP(Add).InferShape(f).Tiling(g);   ← 静态对象初始化（静态初始化期）
       └─ 拷贝构造 OpImplRegisterV2(temp)
            └─ RegisterOpImplToRegistry()
                 └─ OpImplRegistry 单例 types_to_impl_["Add"] 填入函数指针

框架侧加载（运行期，见 u4-l5）
  └─ OpImplSpaceRegistry::AddSoToRegistry(so_path)
       ├─ mmDlopen(so)                       ← 触发 so 的静态初始化，注册完成
       ├─ Holder->GetOpImplFunctionsByHandle(handle)
       │    ├─ dlsym("GetRegisteredOpNum")   ← api 层导出符号
       │    └─ dlsym("GetOpImplFunctionsV2") ← 拉回 TypesToImplV2 数组
       └─ AddRegistry(holder)
            └─ MergeTypesToImpl()            ← 融合进 space 全局 map
```

#### 4.1.3 源码精读

分层边界在头文件包含关系上非常清晰。[inc/register/op_impl_registry_base.h:15-33](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/register/op_impl_registry_base.h#L15-L33) 定义了抽象接口 `OpImplRegistryBase`（两个纯虚查询：按 op_type 取函数集、取私有属性）和具体类 `OpImplRegistry`——注意它的私有成员只有一张 map 加 40 字节保留字段：

```cpp
struct OpImplRegistryBase : public OpImplKernelRegistry {
  virtual const OpImplFunctions *GetOpImpl(const ge::char_t *op_type) const = 0;
  virtual const OpImplRegisterV2::PrivateAttrList &GetPrivateAttrs(const ge::char_t *op_type) const = 0;
};
class OpImplRegistry : public OpImplRegistryBase {
  ...
 private:
  std::map<OpImplRegisterV2::OpType, OpImplRegistry::OpImplFunctionsV2> types_to_impl_;
  uint8_t reserved_[40] = {0U};  // Reserved field, do not directly use when only 8-byte left
};
```

`reserved_` 是 metadef 一贯的 ABI 手法：给单例类预留尾部空间，未来加字段不改类大小。

#### 4.1.4 代码实践

**实践目标**：建立四层心智地图，能对任意一个符号说出它属于哪一层。

**操作步骤**：

1. 打开上述 4.1.3 的链接，确认 `OpImplRegistry` 的成员构成。
2. 在仓库根目录执行 `git grep -n "GetOpImplFunctionsV2" -- inc base`，观察它同时出现在 api 头（声明）与 holder 实现（dlsym 消费）两处。
3. 画一张四层框图：注册入口 → base 单例 → api C 导出 → holder/space 聚合。

**需要观察的现象 / 预期结果**：api 层函数在 metadef 内部**没有任何框架侧调用者**（只有实现和 holder 的 dlsym 字符串引用）——它的消费者是跨 so 边界的动态符号查找，这正是「api 层为外部读者而设」的证据。待本地验证（依赖 grep 结果人工确认）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `GetOpImplFunctions` 用 `extern "C"` 导出而不是 C++ 修饰名？
**答案**：C++ 符号名经 name-mangling 后与编译器/版本相关，`dlsym` 按字符串查符号无法稳定命中；`extern "C"` 加 `__attribute__((visibility("default")))` 保证符号名就是 `GetOpImplFunctions`，任何编译器编译的 so 都能被统一找到（见 [inc/register/op_impl_registry_api.h:27-43](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/register/op_impl_registry_api.h#L27-L43)）。

**练习 2**：`OpImplRegistry` 与 u4-l3 的 `OpDefFactory` 都是单例 + map，它们存的「值」有什么本质区别？
**答案**：`OpDefFactory` 存的是**创建函数**（creator，返回算子原型 `OpDef` 的工厂函数），描述「算子长什么样」；`OpImplRegistry` 存的是**实现函数指针集**（`OpImplFunctionsV2`），描述「算子各阶段怎么算」。前者是原型，后者是行为。

### 4.2 OpImplRegisterV2 与 IMPL_OP 宏：注册入口

#### 4.2.1 概念说明

`gert::OpImplRegisterV2` 是算子作者唯一需要打交道的类：构造时传 op_type，然后用链式方法把各阶段实现函数逐个挂上，最后由**拷贝构造函数**把整套函数搬进 `OpImplRegistry` 单例。这个「拷贝即注册」的设计是本模块最精巧的一点。

#### 4.2.2 核心流程

`IMPL_OP(Add).InferShape(f).Tiling(g);` 的展开与执行（宏定义见 [inc/external/register/op_impl_registry.h:148-152](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/op_impl_registry.h#L148-L152)）：

```
static OpImplRegisterV2 op_impl_register_Add0 =     // __COUNTER__ 保证同文件可重名注册
    OpImplRegisterV2("Add")                          // ① 临时对象：new impl_，并在单例 map 里建空槽
        .InferShape(f)                               // ② 链式调用改写「临时对象」的 impl_->functions
        .Tiling(g);                                  //    每个方法返回 *this，继续链
    // ③ 初始化表达式是一个左值（链返回引用），拷贝不可省略：
    //    拷贝构造 OpImplRegisterV2(temp) 被调用
    //    → RegisterOpImplToRegistry(temp.impl_)
    //    → 单例 map["Add"] 的函数指针被逐项填入
```

三个关键推论：

- 注册发生在**静态对象初始化期**，即 so 加载时自动完成；
- 静态变量本身的 `impl_` 在拷贝后为 nullptr，后续再对它链式调用是安全的空操作（这就是 `impl_ != nullptr` 判空在所有 setter 里的作用）；
- 拷贝构造只搬运**非空**字段（见 4.2.3），所以「先 `.Tiling(f1)` 再另起一行 `.InferShape(f)`」分两个语句注册时，第二条语句作用在 impl_ 为空的对象上不会生效——必须写在同一条链上。

#### 4.2.3 源码精读

拷贝/移动构造是注册的触发点，[base/registry/op_impl_registry.cc:247-252](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_registry.cc#L247-L252)：

```cpp
OpImplRegisterV2::OpImplRegisterV2(const OpImplRegisterV2 &register_data) {
  RegisterOpImplToRegistry(register_data.impl_.get());   // 搬运源对象（临时对象）的全部函数
}
```

`RegisterOpImplToRegistry`（[base/registry/op_impl_registry.cc:22-122](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_registry.cc#L22-L122)）先 `CreateOrGetOpImpl` 拿到单例 map 引用，然后对约 20 个字段做「非空才覆盖」合并，例如（节选）：

```cpp
if (rd->functions.infer_shape != nullptr) {
  ss << "[InferShape]";
  funcs.infer_shape = rd->functions.infer_shape;
}
if (rd->functions.tiling != nullptr) {
  ss << "[Tiling]";
  funcs.tiling = rd->functions.tiling;
  funcs.max_tiling_data_size = rd->functions.max_tiling_data_size;  // tiling 的大小上限随 tiling 一起搬
}
```

构造函数本体（[base/registry/op_impl_registry.cc:205-244](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_registry.cc#L205-L244)）把所有函数指针置空、`max_tiling_data_size` 置为 `size_t` 上限，并在末尾 `(void)OpImplRegistry::GetInstance().CreateOrGetOpImpl(op_type);` 预建 map 槽位——这保证了「注册过但没挂任何函数」的 op_type 也能被 `GetOpImpl` 查到。

链式方法实现极其薄，例如 `Tiling`（[base/registry/op_impl_registry.cc:289-296](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_registry.cc#L289-L296)）只是写字段返回 `*this`；而 `TilingParse<T>` 模板（[inc/external/register/op_impl_registry.h:105-112](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/op_impl_registry.h#L105-L112)）额外注入编译期信息的创建/删除函数：

```cpp
template <typename T>
OpImplRegisterV2 &TilingParse(TilingParseFunc const tiling_parse_func) {
  return TilingParse(reinterpret_cast<KernelFunc>(tiling_parse_func),
                     CreateCompileInfo<T>, DeleteCompileInfo<T>);   // new T() / delete p 包装成函数指针
}
```

#### 4.2.4 代码实践

**实践目标**：亲眼验证「拷贝构造触发注册」与「非空才覆盖」两个行为。

**操作步骤**：

1. 阅读 [tests/ut/register/testcase/op_impl_registry_unittest.cc:145-166](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/register/testcase/op_impl_registry_unittest.cc#L145-L166) 的 `Register_Success_RegisterAll`：先断言 `GetOpImpl("TestFoo") == nullptr`，再一行 `IMPL_OP(TestFoo).InferShape(...).Tiling(...)...`，最后逐项断言函数指针非空。
2. 重点看同文件 496-499 行的用例：`IMPL_OP(TestConv2D).InferShape(TestInferShapeFunc1);` 后断言 `CreateOrGetOpImpl("TestConv2D").infer_shape == &TestInferShapeFunc1` 且 `tiling == nullptr`。
3. 想一个反例：把 `IMPL_OP(X).InferShape(f);` 拆成两条语句 `IMPL_OP(X); X.InferShape(f);`（语法上需先拿到静态对象名），预测结果。

**需要观察的现象 / 预期结果**：第 3 步中 `InferShape` 不会进入注册表——因为静态对象经拷贝构造后 `impl_` 为空，setter 判空直接跳过。**待本地验证**（读者可在 tests/ut/register/testcase/ 下仿照现有用例写一个验证，register 目录的 .cc 由 `GLOB_RECURSE` 自动收进 `ut_register` 目标，见 [tests/ut/register/CMakeLists.txt:22](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/register/CMakeLists.txt#L22)，随后 `bash tests/run_test.sh -u` 运行）。

#### 4.2.5 小练习与答案

**练习 1**：`IMPL_OP` 宏为什么要拼接 `__COUNTER__`？
**答案**：同一个 so 里可能对同一 op_type 在不同头文件/不同行各注册一部分函数（如分别注册 InferShape 和 optiling），宏展开的静态变量名必须唯一，否则重定义编译错误；`__COUNTER__` 每次展开自增，保证名字不冲突。

**练习 2**：注册全程没有加锁，为什么是线程安全的？
**答案**：因为注册只发生在 so 的静态初始化阶段，而 `dlopen` 依赖调用方串行调用（且 glibc 的 loader 自身持锁执行静态初始化）；运行期只剩读操作。这是与 u4-l3 OpDefFactory 相同的「时序换锁」约定。

**练习 3**：`InferOutDataTypeSameWithFirstInput()` 为什么不需要用户传函数？
**答案**：它是 metadef 预置的通用推导规则，内部直接把文件内的静态函数 `InferOutDataTypeSameWithFirstInputFunc` 挂到 `InferDataType` 槽位上（[base/registry/op_impl_registry.cc:124-139、285-287](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_registry.cc#L124-L139)）：取输入 0 的 dtype 逐个 `SetOutputDataType`。

### 4.3 OpImplFunctions / OpImplFunctionsV2：算子实现函数集

#### 4.3.1 概念说明

`OpImplKernelRegistry::OpImplFunctions`（V1）与 `OpImplFunctionsV2`（V2）就是注册表 map 的「值类型」——一个算子的全部行为挂点。它分四类内容：

1. **阶段函数指针**：infer_shape、infer_shape_range、infer_datatype、tiling、tiling_parse、compile_info_creator/deleter、gen_simplifiedkey、op_execute_func、calc_op_param、gen_task、check_support、op_select_format、exception_func 等；
2. **位标志（uint64_t 位图）**：inputs_dependency、host_inputs、tiling_dependency、output_shape_depend_compute、nullable_outputs_，用第 i 位表示第 i 个输入/输出具有某性质；
3. **元信息**：`max_tiling_data_size`、`private_attrs`（私有属性表，AnyValue 承值——承接 u2-l3）；
4. **ABI 守护**：V2 尾部的 `st_size`、`version` 与 4000 字节 `reserved_[500]`。

#### 4.3.2 核心流程

函数指针类型的统一签名模式是「一个上下文指针入参、UINT32 状态出参」：

\[
\text{StageFunc} : \text{Context}_{\text{stage}} \rightarrow \text{UINT32}
\]

上下文正是单元三讲过的 `InferShapeContext`、`TilingContext` 等——同一块 `KernelRunContext` 内存的类型化视图。位标志的编码规则：

\[
\text{flag} \mathrel{\oplus}= 1 \ll \text{index}, \quad \text{index} < 8 \times \text{sizeof}(\text{flag})
\]

即 64 位标志最多描述 64 个输入/输出，越界返回失败而不是截断。

#### 4.3.3 源码精读

类型别名集中在 [inc/external/register/op_impl_registry.h:62-84](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/op_impl_registry.h#L62-L84)，这是本讲的「函数指针清单」：

| 别名 | 签名（入参上下文） | 阶段 |
| --- | --- | --- |
| `InferShapeKernelFunc` | `InferShapeContext *` | 输出形状推导 |
| `InferShapeRangeKernelFunc` | `InferShapeRangeContext *` | 动态 shape 范围推导 |
| `InferDataTypeKernelFunc` | `InferDataTypeContext *` | 输出 dtype 推导 |
| `TilingKernelFunc` | `TilingContext *` | tiling 参数计算 |
| `TilingParseFunc` | `TilingParseContext *` | 编译期 json 解析 |
| `KernelFunc` | `KernelContext *` | 通用裸上下文 |
| `GenSimplifiedKeyKernelFunc` | `TilingContext *, char_t *` | tiling 简化 key 生成 |
| `OpExecFunc` / `OpExecPrepareFunc` / `OpExecLaunchFunc` | `OpExecute*Context *` | aclnn 执行路径 |
| `CompileInfoCreatorFunc` / `DeleterFunc` | `void *()` / `void (*)(void *)` | 编译信息生命周期 |
| `OP_CHECK_FUNC_V2` | `const OpCheckContext *, AscendString &` | check/support 类查询 |
| `OpCalcParamKernelFunc` / `OpGenTaskKernelFunc` | `ExeResGenerationContext *` | 执行资源生成 |
| `ExceptionDumpFunc` | `aclrtExceptionInfo *, void *` | 异常 dump 解析 |

结构体主体在 [inc/external/register/op_impl_kernel_registry.h:148-167](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/op_impl_kernel_registry.h#L148-L167)（V1 字段区）与 [inc/external/register/op_impl_kernel_registry.h:170-217](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/op_impl_kernel_registry.h#L170-L217)（V2 扩展区）。位标志的读写封装带着越界保护，例如 [inc/external/register/op_impl_kernel_registry.h:57-69](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/op_impl_kernel_registry.h#L57-L69)：

```cpp
bool IsInputDataDependency(const size_t index) const {
  if (index >= sizeof(inputs_dependency) * kByteBitCount) { return false; }
  return static_cast<bool>(inputs_dependency & static_cast<uint64_t>(1) << index);
}
```

V2 的 ABI 守护三件套（[inc/external/register/op_impl_kernel_registry.h:204-216](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/op_impl_kernel_registry.h#L204-L216)）：

```cpp
uint32_t st_size = sizeof(OpImplFunctionsV2);   // 自报结构体大小，供跨 so 版本探测
uint32_t version = OP_IMPL_MAIN_VERSION;        // 主版本号（当前为 2）
...
uint64_t reserved_[500] = {0U};                 // 尾部预留，新字段从这里"借"
```

V1/V2 互转通过继承切片完成（[base/registry/op_impl_registry.cc:156-173](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_registry.cc#L156-L173)）：`OpImplFunctionsV2` 公有继承 `OpImplFunctions`，向下转型 `static_cast<OpImplFunctions &>` 即丢掉 V2 新增字段。

#### 4.3.4 代码实践

**实践目标**：数清并归类函数集里的全部挂点。

**操作步骤**：

1. 打开 [inc/external/register/op_impl_kernel_registry.h:148-217](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/op_impl_kernel_registry.h#L148-L217)，把字段抄成四栏表格：函数指针 / 位标志 / 元信息 / ABI 守护。
2. 对照 [tests/ut/register/testcase/op_impl_registry_unittest.cc:116-143](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/register/testcase/op_impl_registry_unittest.cc#L116-L143) 的 `OpImplFunctionsTestConvert`：往 V2 结构体里塞哑指针（如 `(InferShapeKernelFunc)0x234567`），构造/赋值/切片回 V1 再断言字段保留情况。

**需要观察的现象 / 预期结果**：V2→V1→V2 一圈来回后，V2 专属字段（如 `infer_symbol_shape`）变回 nullptr，V1 字段保留——这正是切片语义。测试已固化该行为，可直接阅读断言确认，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `reserved_[500]` 要整体置零而不是不初始化？
**答案**：注册表内容会经 C 接口（`GetOpImplFunctionsV2`）跨 so 拷贝；未初始化的填充字节会把栈/堆上的脏数据带出边界，既是信息泄漏隐患，也会让「未使用字段必须为零」的跨版本约定失效。

**练习 2**：`max_tiling_data_size` 为什么跟着 `Tiling()` 的第二个参数走，而不是独立方法？
**答案**：它是 tiling 的配套元信息——框架需要据此为 tiling 输出预分配 buffer（默认 2048 字节，见 [inc/external/register/op_impl_registry.h:96](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/op_impl_registry.h#L96)）；没有 tiling 函数时它无意义，所以合并逻辑里两者绑定搬运（见 4.2.3 节选代码）。

### 4.4 OpImplRegistry 单例与 C 导出接口

#### 4.4.1 概念说明

base 层（单例 map）与 api 层（C 导出）共同回答一个问题：**外部如何拿走本 so 的全部注册内容？** 答案是两步协议：先调 `GetRegisteredOpNum()` 问数量，按数量分配数组，再调 `GetOpImplFunctions(V2)` 填数组。两步之间注册表若发生变化，第二个函数会以数量不匹配为由失败——一个朴素但有效的竞态防护。

#### 4.4.2 核心流程

```
调用方（框架 holder）                    本 so（api 层实现）
  num = GetRegisteredOpNum()        ──→  返回单例 map.size()
  buf = new TypesToImplV2[num]
  GetOpImplFunctionsV2(buf, num)    ──→  if (num != map.size()) 返回 GRAPH_FAILED
                                       否则遍历 map 逐项填 {op_type, funcs}
```

#### 4.4.3 源码精读

api 契约只有三个函数与两个 POD 结构体，[inc/register/op_impl_registry_api.h:17-43](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/register/op_impl_registry_api.h#L17-L43)：

```cpp
struct TypesToImplV2 {
  const char *op_type;                                        // 算子类型名（C 字符串）
  gert::OpImplKernelRegistry::OpImplFunctionsV2 funcs;        // 函数集整体搬运
};
extern "C" {
METADEF_FUNC_VISIBILITY size_t GetRegisteredOpNum(void);
METADEF_FUNC_VISIBILITY int32_t GetOpImplFunctions(TypesToImpl *impl, size_t impl_num);
METADEF_FUNC_VISIBILITY int32_t GetOpImplFunctionsV2(TypesToImplV2 *impl, size_t impl_num);
}
```

实现在 [base/registry/op_impl_functions.cc:47-63](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_functions.cc#L47-L63)：数量校验失败返回 `GRAPH_FAILED`，成功则 `impl[cnt].funcs = it.second;` 整结构体拷贝。V1 版本（[base/registry/op_impl_functions.cc:29-45](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_functions.cc#L29-L45)）多一步显式 `static_cast<OpImplFunctions &>` 切片降级。

单例本体在 [base/registry/op_impl_registry.cc:175-190](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_registry.cc#L175-L190)：Meyers 单例 + `types_to_impl_[op_type]`（下标访问即"无则建"）与 `find` 查询（未命中返回 nullptr，哨兵失败语义）。

#### 4.4.4 代码实践

**实践目标**：验证「两步协议」的数量校验行为。

**操作步骤**：

1. 阅读 [base/registry/op_impl_functions.cc:47-63](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_functions.cc#L47-L63)，找到数量不匹配时的返回值。
2. 在单测里（可仿照 `OpImplRegistryUT`）先注册一个 `IMPL_OP(MyOp)`，调用 `GetRegisteredOpNum()` 记为 n，再故意用 `GetOpImplFunctionsV2(buf, n + 1)` 调用。

**需要观察的现象 / 预期结果**：返回 `GRAPH_FAILED`，且 buf 内容未被写入（函数在校验处提前返回）。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`TypesToImplV2` 里存的是 `const char *` 而非 `AscendString`，为什么？
**答案**：这是跨 so 的 C 边界结构，`AscendString` 内含 `shared_ptr<std::string>`（u2-l2 讲过其跨 ABI 风险）；指针指向本 so 单例 map 的 key 存储区，只要 so 不被 dlclose 就有效——生命周期由 holder 持有 so 句柄来保证（见 4.5）。

**练习 2**：为什么需要 V1、V2 两个导出函数并存？
**答案**：兼容演进。旧算子 so 只导出 V1（无 `infer_symbol_shape` 等新字段），新框架按「先试 V2、找不到符号再回落 V1」的顺序探测（见 4.5.3 的 `kImplMenuVec`），并把 V1 结果升格为 V2（切片补默认值）。

### 4.5 Space 聚合层：Holder 与按 op_type 合并

#### 4.5.1 概念说明

单个 `OpImplRegistry` 只代表「一个 so 内的注册表」。真实进程里算子来自多个 so，且同一个算子的不同阶段函数可能分布在不同 so（如原型在一个包、tiling 在另一个包）。聚合层解决两个问题：

- **Holder（[inc/register/op_impl_registry_holder_manager.h:24-56](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/register/op_impl_registry_holder_manager.h#L24-L56)）**：一个 so 的注册快照 + 该 so 的 `dlopen` 句柄（析构时 `mmDlclose`），由 `OpImplRegistryHolderManager` 按 **so 文件内容**（而非路径）去重，避免同一 so 被两个路径加载两份。
- **Space（[inc/register/op_impl_space_registry.h:24-49](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/register/op_impl_space_registry.h#L24-L49)）**：持有若干 Holder，把它们的 map 逐 op_type、逐函数**融合**成一张 `merged_types_to_impl_`，并提供 `DefaultOpImplSpaceRegistry` 按 `OppImplVersion`（不同版本的 opp 包）维护多个 space。

#### 4.5.2 核心流程

合并规则（对每个函数字段）：

```
if 目标为空 and 源非空:  填入（首次注册，打 DEBUG 日志）
elif 两者都非空:         保留目标，打 WARNING「has been registered」
else:                    无操作
```

即「先到先得、重复告警、不覆盖」——与 u4-l3 OpConfigRegistry 的「非空覆盖」方向相反，这里保护的是**首个**注册者的语义。标量与 private_attrs 列表用同样规则（`MERGE_SCALAR` 宏）。

#### 4.5.3 源码精读

dlsym 探测菜单在 [base/registry/op_impl_registry_holder_manager.cc:147-161](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_registry_holder_manager.cc#L147-L161)：

```cpp
ImplMenu kImplMenuVec[] = {
  {ImplType::RT_V2_TYPE, "GetRegisteredOpNum", "GetOpImplFunctionsV2", ..., GetImplFunc<...V2>},
  {ImplType::RT_TYPE,    "GetRegisteredOpNum", "GetOpImplFunctions",   ..., GetImplFunc<...V1>},
  {ImplType::CT_TYPE,    "GetRegisteredOpCtNum", "GetOpCtImplFunctions", ..., GetCtImplFunc},
};
```

`GetOpImplFunctionsByHandle`（[base/registry/op_impl_registry_holder_manager.cc:163-204](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_registry_holder_manager.cc#L163-L204)）按菜单循环：dlsym 找数量函数 → 找取数函数 → 模板函数 `GetImplFunc` 调用导出函数并把结果插进 holder 的对应 map；V1 结果若存在而 V2 为空，则逐项升格进 `types_v2_to_impl_`（[base/registry/op_impl_registry_holder_manager.cc:195-201](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_registry_holder_manager.cc#L195-L201)）。

合并宏与逐字段融合在 [base/registry/op_impl_space_registry_v2_impl.cc:25-44](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_space_registry_v2_impl.cc#L25-L44)（`MERGE_FUNCTION`/`MERGE_SCALAR` 定义）与 [base/registry/op_impl_space_registry_v2_impl.cc:177-219](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_space_registry_v2_impl.cc#L177-L219)（对约 18 个函数指针逐个套用）。so 加载入口 `AddSoToRegistry`（[base/registry/op_impl_space_registry_v2_impl.cc:87-146](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_space_registry_v2_impl.cc#L87-L146)）：读文件内容作 key → `GetOrCreateOpImplRegistryHolder`（内容相同直接复用旧 holder，不再 dlopen）→ dlopen 成功后经 holder 拉取注册内容 → `AddRegistry` 融合。

查询兜底逻辑（[base/registry/op_impl_space_registry_v2_impl.cc:168-175](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_space_registry_v2_impl.cc#L168-L175)）很能体现两层关系：space 的融合表查不到时，回落到**本 so 自己的** `OpImplRegistry` 单例——例如 `IMPL_OP_INFER_SYMBOL_SHAPE`（[base/registry/op_impl_register_v2_impl.cc:14-27](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_register_v2_impl.cc#L14-L27)）就直接写进 `DefaultOpImplSpaceRegistry` 对应 space 的融合表，两种写入路径并存。

#### 4.5.4 代码实践

**实践目标**：追踪「so 从磁盘到融合表」的完整链路，标出每步入口函数。

**操作步骤**：

1. 从 [base/registry/op_impl_space_registry.cc:73-92](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_space_registry.cc#L73-L92) 的 `LoadSoAndSaveToRegistry`/`ConvertSoToRegistry` 入手：确认它按版本取/建 space，再组装 `OppSoDesc`（so 路径 + 包名）调用 `AddSoToRegistry`。
2. 沿 4.5.3 的调用链列出三个阶段的入口函数与返回值处理：
   - 路径搜索/内容读取：`GetBinDataFromFile`（失败 `GE_ASSERT_NOTNULL` 中止）；
   - 加载：lambda 内 `mmDlopen`（失败打印排障指引并返回 nullptr，**不抛异常**）；
   - 符号解析：`GetOpImplFunctionsByHandle`（V2 符号缺失仅 continue，V1 缺失才算失败）。
3. 阅读 [tests/ut/register/testcase/op_impl_space_registry_unittest.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/register/testcase/op_impl_space_registry_unittest.cc) 中关于重复注册的用例，对照「先到先得」规则。

**需要观察的现象 / 预期结果**：同一 so 内容第二次加载时命中 `GetOrCreateOpImplRegistryHolder` 的已有分支，日志出现 `so already loaded! ... no need dlopen`（[base/registry/op_impl_registry_holder_manager.cc:296-307](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_registry_holder_manager.cc#L296-L307)）。**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：Holder 按 so 内容（而非路径）做 key 去重，解决什么问题？
**答案**：同一个算子包可能被软链接、相对路径等不同路径引用多次；按内容 key 保证物理上同一份 so 只被 dlopen/融合一次，函数指针与静态变量不会重复注册。

**练习 2**：holder manager 的注释（[inc/register/op_impl_registry_holder_manager.h:98-105](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/register/op_impl_registry_holder_manager.h#L98-L105)）解释了为什么用 shared_ptr 而不是 weak_ptr，原因是什么？
**答案**：so 里除 op_impl 外还有其他自注册静态变量（如 `operator_infer_axis_type_info_funcs`），进程退出前才析构；若 holder 提早析构触发 dlclose，这些静态变量的析构会访问已卸载的代码。shared_ptr 让 holder 活到与 manager 单例同寿，显式延后 dlclose 时机（代码标注为临时规避方案）。

**练习 3**：`AddSoToRegistry` 末尾会对自定义包中无 tiling 函数的算子打 `[MissOpImplementation]` 警告，为什么不直接报错？
**答案**：并非所有算子都需要 tiling（如纯推理元信息类算子、或 tiling 由别的 so 提供——合并规则允许跨 so 拼装函数集）；这是给交付件做完整性**提示**的维测手段，不是硬校验。

## 5. 综合实践

**任务**：为假想算子 `MyAdd` 写出「最小假算子实现」并走通注册链路。完整代码如下（示例代码，读者可保存为 `tests/ut/register/testcase/my_add_impl_registry_unittest.cc`，该目录由 glob 自动纳入 `ut_register` 目标）：

```cpp
// 示例代码：最小假算子实现注册单测
#include "register/op_impl_registry.h"
#include "register/op_impl_registry_base.h"
#include <gtest/gtest.h>

namespace {
// 1. 两个实现函数：签名必须与类型别名完全一致（u3-l2/u3-l4 讲过的上下文视图）
ge::graphStatus MyAddInferShape(gert::InferShapeContext *context) {
  const auto input_shape = context->GetInputShape(0);       // 读输入 0 的 shape
  if (input_shape == nullptr) { return ge::GRAPH_FAILED; }
  auto output_shape = context->GetOutputShape(0);           // 框架预分配的输出槽
  *output_shape = *input_shape;                             // element-wise：输出形状 = 输入形状
  return ge::GRAPH_SUCCESS;
}
ge::graphStatus MyAddTiling(gert::TilingContext *context) {
  // 最小 tiling：只写 block dim。SetBlockDim 真实存在但已标注 deprecated（改用 SetSimdNumBlocks），
  // 此处仅演示「向 TilingContext 的输出槽位写结果」，见 tiling_context.h:251-254
  (void)context->SetBlockDim(32U);
  return ge::GRAPH_SUCCESS;
}
// 2. 注册：一条链完成挂接，静态初始化期生效
IMPL_OP(MyAdd)
    .InferShape(MyAddInferShape)   // 挂到 OpImplFunctionsV2::infer_shape
    .Tiling(MyAddTiling, 1024U);   // 挂到 tiling，同时登记 max_tiling_data_size=1024
}  // namespace

TEST(MyAddImplRegistryUT, RegisterAndQuery) {
  // 3. 验证：经单例 map 查回并逐项核对
  const auto *funcs = gert::OpImplRegistry::GetInstance().GetOpImpl("MyAdd");
  ASSERT_NE(funcs, nullptr);
  EXPECT_EQ(funcs->infer_shape, &MyAddInferShape);   // 函数地址一致 = 挂接成功
  EXPECT_EQ(funcs->tiling, &MyAddTiling);
  EXPECT_EQ(funcs->max_tiling_data_size, 1024U);
  EXPECT_EQ(funcs->infer_datatype, nullptr);          // 未注册的字段保持空
  // 4. 验证 api 层两步协议
  EXPECT_GE(gert::OpImplRegistry::GetInstance().GetAllTypesToImpl().size(), 1U);
}
```

步骤与检查点：

1. 对照 4.3 的函数指针清单确认两个函数的签名与 `InferShapeKernelFunc`、`TilingKernelFunc` 逐字符一致（返回 `ge::graphStatus` 即 `UINT32` 别名层面的兼容——若编译器报签名不匹配，按头文件别名修正）。
2. `bash build.sh` 确认整体编译不受影响，再 `bash tests/run_test.sh -u` 跑 `ut_register` 目标。
3. 观察注册日志：`RegisterOpImplToRegistry` 会打印 `register OP_IMPL : [InferShape][Tiling]`（见 [base/registry/op_impl_registry.cc:119-121](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/registry/op_impl_registry.cc#L119-L121)）。
4. 进阶：把 `IMPL_OP(MyAdd)` 改为直接构造 `gert::OpImplRegisterV2 reg("MyAdd2"); reg.InferShape(MyAddInferShape);`，预测查询结果差异（提示：回顾 4.2 的拷贝构造机制）。

运行结果**待本地验证**（依赖本地 Ascend 编译环境，见 u1-l2）。

## 6. 本讲小结

- op_impl_registry 体系分四层：注册入口 `OpImplRegisterV2`（链式 API + `IMPL_OP` 宏）、本地单例 `OpImplRegistry`（op_type → 函数集 map）、C 导出 api 层（`GetRegisteredOpNum`/`GetOpImplFunctions(V2)` 两步协议）、聚合层（Holder 管 so 快照与句柄，Space 跨 so 融合查询）。
- 注册的触发点是 `OpImplRegisterV2` 的**拷贝构造函数**：链式调用配置的是临时对象，拷贝到静态变量时经 `RegisterOpImplToRegistry` 以「非空才覆盖」语义写入单例，因此注册全部发生在静态初始化期，无需加锁。
- `OpImplFunctionsV2` 是一个自描述的函数指针集：约 20 个阶段函数指针（InferShape/Tiling/TilingParse/CheckSupport…）+ 5 个 uint64 位标志 + private_attrs（AnyValue 承值）+ `st_size`/`version`/`reserved_[500]` 三重 ABI 守护；V1↔V2 靠继承切片互转。
- Space 合并规则是「先到先得、重复告警、不覆盖」，同一算子的各阶段函数可以来自不同 so；查不到时回落本 so 的 `OpImplRegistry` 单例。
- 勘误：函数指针类型清单不在 `op_impl_functions.cc`（那里只有三个 C 导出函数的实现），而在 `inc/external/register/op_impl_registry.h` 的类型别名区与 `op_impl_kernel_registry.h` 的结构体定义。

## 7. 下一步学习建议

- 下一讲 u4-l5 将沿本讲的 `AddSoToRegistry` 继续向下，讲解 opp 包的目录结构、`opp_so_manager` 的 so 搜索策略与 `op_bin_info` 如何描述包内算子——本讲 4.5 的链路正是那一讲的入口。
- 推荐阅读源码：[tests/ut/register/testcase/op_impl_registry_holder_manager_unittest.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/register/testcase/op_impl_registry_holder_manager_unittest.cc)（holder 去重与句柄管理的行为规格）、[tests/ut/register/testcase/abi_compatibility_for_register_unittest.cc](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/register/testcase/abi_compatibility_for_register_unittest.cc)（注册结构体的布局守护，可与 u5-l4 呼应）。
- 若想看注册的另一条平行线（原型注册 vs 本讲的行为注册），回顾 u4-l3；若想理解实现函数收到的上下文细节，回顾单元三 u3-l2 ~ u3-l4。
