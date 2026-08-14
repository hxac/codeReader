# 综合实践：扩展 metadef 的完整闭环

## 1. 本讲目标

本讲是整个学习手册的收官之作。前面五个单元我们分别学过了：

- OpDef 原型定义（u4-l2）：算子「长什么样」；
- OpDefFactory 注册与查询（u4-l3）：原型如何被框架发现；
- OpImplRegistry 实现注册（u4-l4）：算子「怎么算」（InferShape/Tiling 等函数集）如何挂接；
- ContextBuilder（u5-l1）：框架如何在宿主内存上装配出 TilingContext 等执行上下文；
- ABI 兼容（u5-l4）与单元测试（u5-l5）：改动如何守住契约、如何验证。

本讲把这几块串成一条完整链路：**为一个假想算子 MyAdd 走完「原型定义 → 实现函数注册 → 用 Builder 构建上下文驱动 Tiling 函数的单测 → build.sh 编译 → run_test.sh 验证」的端到端闭环**。学完后你应该能：

1. 综合运用 OpDef 定义、实现注册与上下文构建三类能力，独立完成一次算子侧扩展；
2. 说出一次扩展需要触碰的全部文件位置（本讲实践中只需在 `tests/ut/base/testcase/` 新增一个文件）；
3. 掌握 README 给出的「修改 metadef 前的影响评估清单」，判断一处改动是否破坏 ABI。

## 2. 前置知识

本讲默认你已完成 u4-l2、u4-l4、u5-l1（依赖讲义），这里只用三句话唤醒记忆，不再重复细节：

- **原型（OpDef）与实现（OpImpl）是两套并行注册**：OpDef/OpParamDef/OpAttrDef 描述算子的输入输出与属性（静态「户口本」）；`gert::OpImplRegisterV2` 注册 InferShape/Tiling 等裸函数指针（动态「行为」）。两者靠 `op_type` 字符串关联。
- **上下文是裸内存上的类型化视图**：TilingContext 等类零新增数据成员，框架用 Builder 在宿主内存上按固定配方填槽，算子侧函数拿到的 `TilingContext *` 只是对这块内存的一种解释方式。因此**Builder 写入与 Context 读取严格互为镜像**——Builder 怎么填，算子就怎么读。
- **一切发生在静态初始化期**：`IMPL_OP`/`OP_ADD` 宏展开为静态对象，注册靠构造函数完成，so 被 dlopen 的瞬间生效。这决定了注册代码必须写在同一条链上、不能依赖运行时顺序。

如果你对以上任何一句感到陌生，请先回看对应讲义。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲视角 |
| --- | --- | --- |
| `inc/external/asc/register/op_def.h` | OpDef/OpParamDef/OpAttrDef 链式定义 API | 模块 4.1：定义 MyAdd 的原型 |
| `inc/external/asc/register/op_def_registry.h` | `OP_ADD` 宏：原型进入工厂 + 实现搬运 | 模块 4.1：理解一行宏背后的两条注册 |
| `inc/external/register/op_impl_registry.h` | `OpImplRegisterV2` 链式注册 + `IMPL_OP` 宏 | 模块 4.2：挂接 InferShape/Tiling 函数 |
| `inc/register/op_impl_registry_api.h` | 纯 C 导出的两步取数协议 | 模块 4.2：注册结果如何跨 so 被取走 |
| `inc/external/base/context_builder/op_tiling_context_builder.h` | TilingContext 的 Builder 声明 | 模块 4.3：宿主侧装配上下文 |
| `inc/external/base/context_builder/op_context_builder_base.h` | CRTP 公共基类（OpType/IONum/AppendAttr） | 模块 4.3：链式配置入口 |
| `base/context_builder/op_tiling_context_builder.cc` | Builder 实现：填槽配方 | 模块 4.3：配方与 u3-l3 读取镜像 |
| `tests/ut/base/testcase/context_builder_unittest.cc` | Builder 单测，本讲实践的模板 | 综合实践：照此驱动 Tiling 函数 |
| `README.md` / `CONTRIBUTING.md` | 修改检查清单、commit 规范 | 模块 4.4：影响评估 |

## 4. 核心概念与源码讲解

### 4.1 模块一：用 OpDef 定义算子原型

#### 4.1.1 概念说明

原型定义回答的是「MyAdd 这个算子在 IR 里长什么样」：几个输入、几个输出、哪些属性、类型与格式约束是什么。metadef 的 asc 新体系把这件事做成三层链式 API：

- `ops::OpDef`：算子整体，持有 `Input`/`Output`/`Attr` 的 GetOrCreate 入口；
- `ops::OpParamDef`：一个输入/输出端口的约束（`ParamType`/`DataType`/`Format`/`ValueDepend` 等）；
- `ops::OpAttrDef`：一个属性的约束（`AttrType`/`Bool`/`Int`/`ListInt` 等类型方法）。

对外类一律是只持有 `std::unique_ptr<Impl>` 的 pimpl 薄壳，这是 ABI 防线（u4-l2 讲过）。

#### 4.1.2 核心流程

一个假想算子 MyAdd（两个输入、一个输出、一个 required 属性 + 一个 optional 属性）的定义流程：

```text
构造 OpDef("MyAdd")
  ├─ .Input("x1") → OpParamDef：REQUIRED + DataType{DT_FLOAT} + Format{ND}
  ├─ .Input("x2") → OpParamDef：同上
  ├─ .Output("y") → OpParamDef：REQUIRED + DataType{DT_FLOAT} + Format{ND}
  ├─ .Attr("axis")   → OpAttrDef：Int()           （默认 required）
  └─ .Attr("mode").AttrType(OPTIONAL).String("default")  （可选 + 默认值）
```

注意两点：`Input`/`Attr` 是**按名 GetOrCreate**——同名重复调用是合并不是新增；属性默认 required，只有显式 `AttrType(OPTIONAL)` 才改变。

#### 4.1.3 源码精读

`OpDef` 类的公开链式入口，注意第 490–492 行三个按名取端口/属性的方法都返回引用以支持链式调用：

[inc/external/asc/register/op_def.h:L484-L505](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_def.h#L484-L505)

上面这段声明了 `Input`/`Output`/`Attr` 三个 GetOrCreate 入口，以及 `SetInferShape`（L494）——原型对象上也能暂存推导函数，供 `OP_ADD` 宏搬运（见下文）。第 505 行的 `EnableFallBack` 与第 504 行的 `FormatMatchMode` 是格式匹配与回退开关，本讲不展开。

`OpAttrDef` 的类型方法族（`Bool`/`Int`/`String`/`ListInt` 等），带参重载即默认值：

[inc/external/asc/register/op_def.h:L325-L350](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_def.h#L325-L350)

第 331 行 `AttrType(Option attr_type)` 用于把默认 required 改为 OPTIONAL；第 336–337 行 `Int(void)` 与 `Int(int64_t)` 的区别是后者带默认值。`Option` 枚举（IGNORE/OPTIONAL/REQUIRED/DYNAMIC/VIRTUAL）定义在第 59 行：

[inc/external/asc/register/op_def.h:L59-L65](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_def.h#L59-L65)

最后看 `OP_ADD` 宏在 `OP_TILING_LIB` 分支的展开——这是「一行宏同时完成原型注册与实现搬运」的关键证据：

[inc/external/asc/register/op_def_registry.h:L28-L47](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/asc/register/op_def_registry.h#L28-L47)

这段立即调用的 lambda 在静态初始化期：构造原型对象 `opType op(#opType)`（L35），创建 `OpImplRegisterV2`（L37），把原型上暂存的 Tiling 函数搬运到实现注册器（L38 `impl.Tiling(op.AICore().GetTiling())`），再登记四类 opcheck 函数（L40–43），最后用「拷贝构造触发注册」收尾（L45，u4-l4 讲过该技巧）。理解了它，你就明白为什么算子作者写一个类 + 一行 `OP_ADD(MyAdd)` 就同时完成了两类注册。

#### 4.1.4 代码实践

1. **实践目标**：不借助任何工具，手写 MyAdd 的完整原型定义代码。
2. **操作步骤**：打开 `inc/external/asc/register/op_def.h`，对照 `OpDef`/`OpParamDef`/`OpAttrDef` 三个类的公开方法，在纸上或编辑器里写出 MyAdd 的构造函数体（输入 x1/x2、输出 y、required 属性 axis、optional 属性 mode）。
3. **需要观察的现象**：你写出的每一个链式调用的返回类型都是 `OpParamDef &` / `OpAttrDef &` / `OpDef &`，且方法名能在头文件中逐一对上。
4. **预期结果**：得到类似「示例代码」章节 5.1 的第一段代码。注意 `Int()` 有两个重载，带默认值时选哪个。

本实践为源码阅读型，无需运行（「待本地验证」的综合实践在第 5 节）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SetInferShape` 定义在 `OpDef` 上，而最终生效却要靠 `OpImplRegisterV2`？

**答案**：`OpDef` 上的 `SetInferShape`（op_def.h L494）只是把函数指针**暂存**在原型对象里；真正写入进程级注册表的是 `OP_ADD` 宏展开体中的 `impl.InferShape(op.GetInferShape())`（op_def_registry.h L23）或用户手写的 `IMPL_OP` 链。原型对象是「草稿」，注册器才是「生效」。

**练习 2**：如果把 `.Attr("axis")` 写成 `.Attr("axis").AttrType(ops::OPTIONAL)`，但忘了写 `Int(int64_t)` 默认值，这个属性还是合法的吗？

**答案**：合法。optional 与默认值是两个正交维度：`AttrType(OPTIONAL)` 只声明「建图时可以不填」，默认值是「不填时取什么」。没有默认值的 optional 属性，算子实现侧读到它时必须容忍缺失（u2-l3 讲过 AnyValue 空值语义）。

### 4.2 模块二：用 OpImplRegisterV2 注册算子实现

#### 4.2.1 概念说明

实现注册回答「MyAdd 怎么算」。metadef 把算子的生命周期拆成约 20 个阶段（InferShape、Tiling、TilingParse、CheckSupport……），每个阶段是一个**裸函数指针**，统一挂在一个 `OpImplRegisterV2` 对象上，再以 `op_type` 为 key 写入本地单例。本讲只关心其中两个：

```cpp
using InferShapeKernelFunc = UINT32 (*)(InferShapeContext *);  // 形状推导
using TilingKernelFunc = UINT32 (*)(TilingContext *);          // tiling 计算
```

#### 4.2.2 核心流程

```text
IMPL_OP(MyAdd)                                    // 展开为 static OpImplRegisterV2 对象
  .InferShape(MyAddInferShape)                    // 暂存到临时对象
  .Tiling(MyAddTiling, 2048)                      // 第二参数为 max_tiling_data_size
  ;                                               // 拷贝到静态变量 → 构造函数写入单例（非空才覆盖）

so 被 dlopen → 静态对象构造完毕 → 注册表就绪
框架侧 dlsym 两步取数：
  GetRegisteredOpNum()  → 拿到条数 N
  GetOpImplFunctionsV2(buffer, N) → 把 N 个 {op_type, funcs} 拷给框架
```

#### 4.2.3 源码精读

函数指针类型与链式注册方法：

[inc/external/register/op_impl_registry.h:L62-L97](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/op_impl_registry.h#L62-L97)

第 62、64 行是两个阶段函数签名；第 87、96 行是挂接入口，注意 `Tiling` 的第二个参数 `max_tiling_data_size = 2048`——框架据此为 TilingData 输出槽预留容量（呼应 u3-l5）。第 95 行 `InferOutDataTypeSameWithFirstInput()` 是内置推导规则，注册后可免写自定义 InferDataType。

`IMPL_OP` 宏三段展开，最终落地为 `__COUNTER__` 去重的静态对象：

[inc/external/register/op_impl_registry.h:L148-L157](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/register/op_impl_registry.h#L148-L157)

第 148–149 行是关键：静态变量名拼入 `__COUNTER__`，因此**同一文件里可以多次使用 `IMPL_OP` 而不重名**；`static` 保证符号不跨编译单元冲突。

注册结果跨 so 传递的两步协议，纯 C 导出：

[inc/register/op_impl_registry_api.h:L17-L39](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/register/op_impl_registry_api.h#L17-L39)

第 22–25 行 `TypesToImplV2` 是 `{op_type 字符串, 函数集}` 的 POD 拷贝单元；第 37–39 行三个 `extern "C"` 函数用了 `METADEF_FUNC_VISIBILITY`（default 可见性，L31–35）确保符号能被 dlsym 找到。这套协议的意义（u4-l4/u4-l5 讲过）是不让 `std::map` 等 STL 类型跨 so 边界。

#### 4.2.4 代码实践

1. **实践目标**：确认 `IMPL_OP(MyAdd)` 展开后确实是「静态对象 + 构造期注册」，并梳理 MyAdd 需要注册的最小函数集。
2. **操作步骤**：
   - 对任意 cpp 文件执行 `g++ -E -I inc/external -I pkg_inc test.cpp`（或直接人工展开宏），观察 `IMPL_OP(MyAdd)` 变成的静态变量定义；
   - 在 `inc/external/register/op_impl_registry.h` 中数一数 `OpImplRegisterV2` 的公开链式方法，圈出 MyAdd 必须的两个。
3. **需要观察的现象**：预处理输出中出现了 `static gert::OpImplRegisterV2 op_impl_register_MyAdd0 = gert::OpImplRegisterV2("MyAdd")` 字样（计数后缀可能不同）。
4. **预期结果**：MyAdd 最小只需 `.InferShape(...)` 与 `.Tiling(...)` 两项；其余阶段（InferFormat、GenSimplifiedKey 等）都有默认空实现，缺省不影响闭环。

如果本地没有编译环境，本步骤可改为纯阅读宏定义完成，「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `IMPL_OP` 必须独占一条语句、后续 `.InferShape(...).Tiling(...)` 必须写在同一行链上？

**答案**：注册发生在 `OpImplRegisterV2` 的**拷贝构造**时刻（把临时对象的函数集搬进静态对象并写入单例，u4-l4）。如果链被拆成两条语句，中间会产生额外的临时对象拷贝，且「非空才覆盖」语义可能提前触发，导致注册内容不完整或重复。

**练习 2**：`GetOpImplFunctionsV2` 为什么用 `int32_t` 返回值、`size_t` 入参，而不是直接返回 `std::vector`？

**答案**：这是 C 接口（`extern "C"`），不能出现 STL 类型——跨 so 传递 STL 容器的布局依赖编译器与标准库版本，正是 u5-l4 讲的 ABI 大忌。框架先 `GetRegisteredOpNum()` 拿条数、自己分配数组，再由 `GetOpImplFunctionsV2` 填充，全部用 POD 类型。

### 4.3 模块三：用 OpTilingContextBuilder 构建上下文并驱动 Tiling 函数

#### 4.3.1 概念说明

前两个模块是「算子作者视角」；本模块切到「框架/测试视角」：算子的 Tiling 函数签名是 `UINT32 (*)(TilingContext *)`，要在单测里直接驱动它，就必须自己造出一个合法的 `TilingContext`。这就是 `gert::OpTilingContextBuilder` 的用途——它把 u5-l1 讲的「在裸内存上填槽」配方封装成链式 API：

- 公共部分（CRTP 基类）：`OpType`/`OpName`/`IONum`/`AppendAttr`；
- Tiling 专属：`CompileInfo`/`PlatformInfo`/`TilingData`/`TilingDataSize`/`Workspace`/`SimtBlockDim`/`InputTensors`/`OutputTensors`；
- `Build()` 返回 `ContextHolder<TilingContext>`，RAII 持有整块内存。

#### 4.3.2 核心流程

Builder 的填槽配方（与 u3-l3 的读取公式严格镜像）：

```text
Build()
  ├─ 校验 compile_info / platform_info 非空（缺失即失败）
  ├─ CreateComputeNodeInfo：把 AppendAttr 的属性序列化进 RuntimeAttrs
  ├─ 槽位序列 = [输入 tensors ...] + [输出 shapes ...]   ← 输出追加到输入段尾部
  │              + 5 个隐藏槽（compile_info / platform_info /
  │                prepare-data / deterministic / deterministic-level）
  ├─ tiling 输出段固定 kOutputNum 槽：
  │     kOutputTilingData / kOutputWorkspace / kOutputSimtBlockDim / kOutputSimtGridDim ...
  └─ BuildCtx：按 KernelRunContext 头部公式在裸内存上落盘，返回 ContextHolder
```

算子侧 `GetInputShape(0)` 读到的正是 `InputTensors` 放进来的第 0 个张量的形状——写与读互为镜像。

#### 4.3.3 源码精读

Builder 的全部专属配置接口声明：

[inc/external/base/context_builder/op_tiling_context_builder.h:L25-L137](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/base/context_builder/op_tiling_context_builder.h#L25-L137)

注意第 76 行 `TilingData(const gert::TilingData *, Deleter)` 与第 86 行 `TilingDataSize(size_t)` 互斥、后调用者覆盖前者（L82 注释）；第 121 行 `InputTensors` 的注释说明了数据依赖算子必须给 Host 侧有效地址、非数据依赖算子可传空 TensorData。第 136 行 `Build()` 是唯一出口。

填槽配方的实现，即上面流程图的代码化：

[base/context_builder/op_tiling_context_builder.cc:L23-L47](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/context_builder/op_tiling_context_builder.cc#L23-L47)

逐行对照：L33–38 依序追加五个隐藏槽（注释标明用途）；L39–44 把 tiling 输出槽按 `TilingContext::kOutput*` 枚举下标安放（这些枚举即 u3-l3 讲的 TilingOutputIndex）；L45 `BuildCtx` 完成裸内存落盘。L49–50 的 `static_assert(sizeof(...) == sizeof(ContextBuilderImpl))` 是防止 Builder 自身意外增肥的守护。

`TilingDataSize` 的所有权捷径——让 ContextHolder 接管 TilingData 生命周期：

[base/context_builder/op_tiling_context_builder.cc:L97-L107](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/context_builder/op_tiling_context_builder.cc#L97-L107)

`TilingData::CreateCap` 按容量分配，lambda 删除器在 holder 析构时 `delete[]`（L100–104）。单测里若想让算子侧 `GetRawTilingData()` 拿到可写缓冲，用 `TilingDataSize` 最省心。

CRTP 基类的公共配置入口（属性追加的顺序契约写在注释里）：

[inc/external/base/context_builder/op_context_builder_base.h:L35-L83](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/base/context_builder/op_context_builder_base.h#L35-L83)

第 50 行 `IONum` 与第 60 行 `IOInstanceNum` 互斥；第 67–70 行注释给出了「`AppendAttr` 的顺序 == `GetAttrs()->GetInt(0)` 的下标」这一镜像契约——综合实践中读取属性时要用到。

现成的单测模板（本讲综合实践直接照抄其骨架）：

[tests/ut/base/testcase/context_builder_unittest.cc:L400-L422](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/context_builder_unittest.cc#L400-L422)

这是一段完整的 Builder 链式调用：`OpName→OpType→IONum(4,1)→九连 AppendAttr→TilingData→Workspace→CompileInfo→Deterministic→PlatformInfo→SimtBlockDim/SimtGridDim→InputTensors→OutputTensors→Build()`。随后 L424–481 用 `holder.GetContext()` 拿到 `TilingContext *` 并逐项 EXPECT 验证镜像关系（如 L434 `GetInputShape(0)` 与 L459 `GetAttrs()->GetInt(0)`）。

#### 4.3.4 代码实践

1. **实践目标**：跑通「Builder 构建 → Context 读取」的最小验证。
2. **操作步骤**：
   - 执行 `bash tests/run_test.sh -u`（u1-l2 讲过，产物为 `build_gcov/` 下的 `ut_metadef`）；
   - 用 `--gtest_filter='UtestContextBuilder.*Tiling*'` 只跑 tiling 相关用例。
3. **需要观察的现象**：`CreateTilingContextViewOK`、`CreateTilingContextViewWithTensorV2OK` 等用例 PASS。
4. **预期结果**：所有 tiling builder 用例绿色通过，证明写读镜像契约成立。若本地无环境则「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：如果 `Build()` 前忘记调用 `CompileInfo`，会发生什么？

**答案**：`BuildTilingContext` 第一行就是 `GE_ASSERT_NOTNULL(tiling_info_.compile_info_, ...)`（op_tiling_context_builder.cc L24），空指针断言失败直接报错返回空 holder，`GetContext()` 得到 nullptr。失败语义是「显式报错」而非静默构造残缺上下文。

**练习 2**：为什么文件末尾还有三个 `__attribute__((weak))` 的纯 C 函数（如 `gert_TilingContextBuilder_SetSimtBlockDim`）？

**答案**：GE 与 metadef 是独立发版的两个 so，GE 侧若链接了旧版 metadef（没有 `SimtBlockDim` 这个新方法），直接调用 C++ 方法会因符号缺失而加载失败；弱符号 C 接口允许「有定义则调用、无定义则为空不调用」，是新旧混布时的兼容通道（u5-l1 讲过，头文件 L152–171 有注释）。

### 4.4 模块四：修改 metadef 前的影响评估清单

#### 4.4.1 概念说明

本讲实践「恰好」不需要改动任何对外头文件——新文件只落在 `tests/`。但真实工作中扩展 metadef（例如给 OpImplRegisterV2 加一个新阶段函数）必须先过影响评估。README 把这件事写成硬性流程。

#### 4.4.2 核心流程

```text
需求分析（来自 ge/ops 公共需求）
  → 设计接口（保持 ABI 兼容）
  → 实现代码（inc/ 头文件）
  → 编写单元测试（tests/）
  → 本地构建验证（bash build.sh）
  → 提交 PR（关联 ge/ops 验证）
```

#### 4.4.3 源码精读

README 的开发流程图与检查清单：

[README.md:L75-L93](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/README.md#L75-L93)

六条检查项中与本讲最相关的是第 2 条「保持对外接口的 ABI 兼容性」与第 4 条「所有测试通过（`bash tests/run_test.sh -u`）」。结合 u5-l4 的知识，评估一处改动是否破坏 ABI 的快速判据：

| 改动类型 | 是否破坏 ABI | 正确做法 |
| --- | --- | --- |
| 给对外 POD 结构体中间插成员 | 是 | 只能尾部追加，或切 `reserved_` 保留字段 |
| 给对外类加虚函数 | 是（vtable 布局变化） | 用 `static_assert(is_standard_layout)` 会拦住 |
| 改枚举已有取值 | 是（数值是契约） | 只能尾部追加新值 |
| 新增重载/新增类/新增头文件 | 否 | 正常演进路径 |
| 只改 `base/` 下实现 | 否（符号签名不变时） | 重编译即可，但需全量跑单测 |

CONTRIBUTING 对「新特性必须先 Issue 讨论」的要求：

[CONTRIBUTING.md:L16](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/CONTRIBUTING.md#L16)

第 26 行起的表格规定了 commit message 格式 `<类型>: <简短描述>`（feat/fix/docs/test……），综合实践中提交测试文件时应使用 `test: ...`。

#### 4.4.4 代码实践

1. **实践目标**：为综合实践中要写的 MyAdd 测试文件做一次「假想」影响评估。
2. **操作步骤**：对照上面的表格逐项打勾——新增 `tests/ut/base/testcase/my_add_unittest.cc` 属于哪一行？它触碰 `inc/external` 了吗？
3. **需要观察的现象**：结论应为「只新增测试文件、不改任何对外头文件，不属于 ABI 敏感改动，检查清单第 2/3/4 项分别对应：不适用 / 新增了单测 / run_test.sh 可验证」。
4. **预期结果**：形成一次评估记录（可直接写进未来 PR 描述）。

#### 4.4.5 小练习与答案

**练习 1**：假如为支持新硬件要给 `TilingKernelFunc` 增加一个出参，直接改函数指针签名可以吗？

**答案**：不可以。函数指针类型定义在对外头文件 `op_impl_registry.h` 中，改签名会改变 `OpImplFunctionsV2` 结构体布局（u4-l4 讲过它有 st_size/version/reserved 三重守护），所有已编译的算子 so 全部失效。正解是并列新增一个 V3 函数指针字段（尾部追加）或全新结构体，老路径保留。

**练习 2**：为什么 README 要求「在 ge 或 ops 仓验证需求确实存在」才允许改 metadef？

**答案**：metadef 处于依赖链最底层（README L13 的架构图），ge 和全部算子仓都依赖它。一个「只有单仓需要」的接口放进 metadef 会让所有组件共同承担 ABI 风险，收益却只有一方——所以公共性是准入门槛。

## 5. 综合实践

**任务**：为假想算子 MyAdd 完成端到端闭环——原型定义、实现函数（InferShape + Tiling）、用 OpTilingContextBuilder 构建上下文驱动 Tiling 函数的单测，并用构建脚本验证。

### 5.1 示例代码（本节代码均为「示例代码」，非项目原有文件）

在 `tests/ut/base/testcase/` 下新建 `my_add_unittest.cc`（该目录被 GLOB 自动收集，零 CMake 改动，见 u5-l5）：

```cpp
// 示例代码：tests/ut/base/testcase/my_add_unittest.cc
#include "gtest/gtest.h"
#include "register/op_impl_registry.h"                       // IMPL_OP / OpImplRegisterV2
#include "exe_graph/runtime/tiling_context.h"
#include "exe_graph/runtime/infer_shape_context.h"
#include "base/context_builder/op_tiling_context_builder.h"

namespace {
// ---- 第 1 步：实现函数（裸函数指针，签名取自 op_impl_registry.h L62/L64）----
ge::graphStatus MyAddInferShape(gert::InferShapeContext *context) {
  const auto *shape = context->GetInputShape(0);            // 读输入 0 的 shape
  if (shape == nullptr) {
    return ge::GRAPH_FAILED;                                 // 空值失败语义（u3-l4）
  }
  auto *output = context->GetOutputShape(0);                 // 框架预分配，直接改写
  output->MutableShape() = shape->GetOriginShape();          // element-wise：输出 = 输入
  return ge::GRAPH_SUCCESS;
}

ge::graphStatus MyAddTiling(gert::TilingContext *context) {
  const auto *shape = context->GetInputShape(0);
  if (shape == nullptr) {
    return ge::GRAPH_FAILED;
  }
  context->SetBlockDim(1);                                   // 最简 tiling：单 block
  const int64_t elem_num = shape->GetOriginShape().GetShapeSize();
  context->SetTilingKey(elem_num % 2);                       // 用奇偶性演示 tiling key
  return ge::GRAPH_SUCCESS;
}

// ---- 第 2 步：注册（静态初始化期完成，IMPL_OP 宏见 op_impl_registry.h L151）----
IMPL_OP(MyAdd).InferShape(MyAddInferShape).Tiling(MyAddTiling);
}  // namespace

// ---- 第 3 步：用 Builder 构造上下文，直接驱动 Tiling 函数 ----
class UtestMyAdd : public testing::Test {};

TEST_F(UtestMyAdd, TilingFuncDrivenByBuilder) {
  gert::Shape shape{2, 3, 4};
  gert::StorageShape ss(shape, shape);
  gert::Tensor x1(ss, {ge::FORMAT_ND, ge::FORMAT_RESERVED, gert::ExpandDimsType()},
                  gert::TensorPlacement::kOnHost, ge::DT_FLOAT, nullptr);
  gert::Tensor x2 = x1;
  gert::Tensor y(ss, {ge::FORMAT_ND, ge::FORMAT_RESERVED, gert::ExpandDimsType()},
                 gert::TensorPlacement::kOnHost, ge::DT_FLOAT, nullptr);
  uint8_t platform_info[] = {1};                             // 假平台信息，内容不限

  gert::OpTilingContextBuilder builder;
  auto holder = builder.OpType("MyAdd")
                    .IONum(2, 1)                             // 2 输入 1 输出（与原型一致）
                    .AppendAttr(int64_t(0))                  // 属性 axis（与原型 Attr 顺序一致）
                    .CompileInfo(platform_info)              // 必填，缺失 Build 会失败（见 4.3.5）
                    .PlatformInfo(platform_info)
                    .TilingDataSize(128)                     // holder 接管 TilingData 生命周期
                    .InputTensors({&x1, &x2})
                    .OutputTensors({&y})
                    .Build();

  auto *ctx = holder.GetContext();
  ASSERT_NE(ctx, nullptr);
  // ---- 直接调用注册的 Tiling 函数：闭环完成 ----
  ASSERT_EQ(MyAddTiling(ctx), ge::GRAPH_SUCCESS);
  EXPECT_EQ(ctx->GetBlockDim(), 1U);
  EXPECT_EQ(ctx->GetInputShape(0)->GetOriginShape().GetShapeSize(), 24);  // 2*3*4
}
```

骨架完全来自 `context_builder_unittest.cc` L400–422 的真实写法，仅参数换成 MyAdd 场景。

### 5.2 操作步骤与验证

1. `bash build.sh`——确认主编译链不受影响（新文件在 `tests/` 下，默认 `ENABLE_METADEF_UT=off` 时甚至不参与编译，天然满足 ABI 评估）；
2. `bash tests/run_test.sh -u`——UT 模式构建 `ut_metadef` 并执行；
3. `./build_gcov/.../ut_metadef --gtest_filter='UtestMyAdd.*'` 只跑新增用例；
4. 观察要点：`GetInputShape(0)` 读到的 shape 是否等于 `InputTensors` 放入的 `{2,3,4}`（写读镜像）；`MyAddTiling` 内部对 `ctx` 的每次读取（`GetInputShape`/`SetBlockDim`）是否都能命中 Builder 填的槽位。

预期结果：用例 PASS。若编译报 `op_def.h` 相关错误，检查 include 路径是否遗漏 `inc/external`；若 `holder.GetContext()` 为 nullptr，回看 4.3.5 练习 1（通常是漏了 `CompileInfo`/`PlatformInfo`）。本示例未在环境中实际执行，运行结果**待本地验证**。

### 5.3 进阶（可选）

把第 3 步从「直接调函数」升级为「查注册表再调用」：仿照 u4-l4 的链路，通过 `OpImplRegistry` 单例按 `"MyAdd"` 查出函数集再调用，验证 `IMPL_OP` 的静态注册确实生效（注意：注册生效依赖静态对象被链接，ut_register 目标用 whole-archive 保证了这一点，在 ut_metadef 下更稳妥的做法是直接调用函数指针）。

## 6. 本讲小结

- 一次算子侧扩展 = **原型定义（OpDef 三件套）+ 实现注册（IMPL_OP 链）+ 上下文驱动（ContextBuilder）** 三块拼图，靠 `op_type` 字符串与「Builder 写入 / Context 读取互为镜像」两个契约粘合。
- `OP_ADD` 宏一行完成两类注册：原型进 OpDefFactory、原型上暂存的函数被搬运进 OpImplRegisterV2——理解宏展开就理解了整个注册体系。
- `OpTilingContextBuilder` 的填槽配方（输入段 + 5 隐藏槽 + kOutputNum 个 tiling 输出槽）与 TilingContext 的读取公式逐槽对应，单测驱动 Tiling 函数是验证算子实现最轻量的方式。
- metadef 的扩展底线是影响评估：本讲实践只新增 `tests/` 文件、零 ABI 风险；一旦触碰 `inc/external`，必须过 README 六条检查清单，POD 布局、枚举取值、函数指针签名都是不可动的契约。
- 验证闭环固定为 `bash build.sh` + `bash tests/run_test.sh -u`，新增测试文件放入 `tests/ut/base/testcase/` 即被自动收集。

## 7. 下一步学习建议

本讲义已覆盖学习手册全部单元，接下来建议：

1. **回到真实消费方**：带着本讲的心智模型去读 [ge](https://gitcode.com/cann/ge) 仓中调用 `OpTilingContextBuilder::Build` 的框架代码，验证「宿主侧配方」在生产环境长什么样；
2. **读一个真实算子**：在 ops-nn / ops-math 任一仓找一个使用 `IMPL_OP` + `OP_ADD` 的算子，对照 u4-l2~u4-l5 与本讲，画出它的「原型—实现—注册—被发现」全链路图；
3. **参与贡献**：按 CONTRIBUTING 流程，从一个 `docs:` 或 `test:` 类型的小 PR 开始（例如为本讲发现的文档问题提 Issue），体验完整贡献闭环。
