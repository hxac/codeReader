# u5-l1 ContextBuilder 体系：上下文的构建与填充

## 1. 本讲目标

学完本讲，你应该能够：

1. 理解 metadef 为什么需要一套 ContextBuilder：框架侧（如 ge、单测）如何在宿主内存上「手工拼装」出一个合法的 `TilingContext` / `InferShapeContext` / `KernelRunContext`。
2. 掌握 Builder 模式构建上下文的内存布局策略：`ComputeNodeInfo` 字节流、`KernelRunContext` 头部 + 槽位数组、隐藏槽位的追加顺序。
3. 掌握 `ContextHolder` 与各 Builder 的协作关系：谁拥有内存、谁负责释放、生命周期约束是什么。
4. 能复用 `OpTilingContextBuilder` 构造一个最小 `TilingContext`，并读回写入的 shape / 属性 / tiling 结果做断言。

本讲是单元五的第一篇，承接 u3-l2（KernelRunContext 的 48 字节布局契约）与 u3-l3（TilingContext 的槽位下标公式）。那两讲回答的是「上下文长什么样、怎么读」；本讲回答的是「这块内存是谁、按什么顺序填出来的」。

## 2. 前置知识

在进入源码前，请确认理解以下概念（前几讲已建立，这里只做回顾）：

- **KernelRunContext**：一个变长的纯 C 结构体，头部是计数与扩展指针，尾部是 `values[1]` 柔性指针数组；槽序列「输入在前、输出在后」扁平排列（u3-l2）。
- **Chain / AsyncAnyValue**：槽位里的类型擦除值槽，`Set(void*, Deleter)` 负责登记数据指针和删除器（u3-l1）。
- **ComputeNodeInfo**：挂在 `compute_node_info` 扩展指针上的变长结构，携带算子类型/名称、IR 输入输出原型个数、实例个数、`CompileTimeTensorDesc` 数组和 `RuntimeAttrs` 属性区；属性不在槽序列中，统一从这里取（u3-l2）。
- **TilingContext 隐藏槽位**：compile_info、platform_info、deterministic 等以 `inputs + outputs + N` 的下标公式追加在输入段尾部；tiling 结果写入 `TilingOutputIndex` 枚举定义的输出槽位（u3-l3）。
- **Builder 模式**：一种构造复杂对象的写法——先用一连串链式调用收集参数（`a.X().Y().Z()`），最后一次 `Build()` 产出成品。链式调用靠每个方法返回对象自身引用实现。
- **pimpl（指向实现的指针）**：对外类只持有一个 `std::unique_ptr<Impl>`，真实字段藏在实现类里，保证对外头文件的 ABI 稳定（u4-l2 的 OpDef 已见过同样的手法）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `inc/external/base/context_builder/op_context_builder_base.h` | 所有算子上下文 Builder 的公共基类模板，声明 `OpType`/`OpName`/`IONum`/`IOInstanceNum`/`AppendAttr` 等链式接口 |
| `base/context_builder/op_context_builder_base.cc` | 上述接口的实现：把配置写进 `ContextBuilderImpl::op_info_` |
| `inc/external/base/context_builder/op_tiling_context_builder.h` | `OpTilingContextBuilder` 对外声明：tiling 专属配置项 + `Build()`，以及三个弱符号纯 C 接口 |
| `base/context_builder/op_tiling_context_builder.cc` | `BuildTilingContext()` 的槽位拼装逻辑，本讲核心 |
| `inc/external/base/context_builder/context_holder.h` | `ContextHolderVoid` 与模板 `ContextHolder<T>`：Build 的返回值，管理上下文全部资源 |
| `base/context_builder/op_context_builder_impl.h` | 内部实现层：`TilingInfo` 配置结构、`ContextHolderImpl` 资源池、`ContextBuilderImpl` 公共构建逻辑 |
| `base/context_builder/op_context_builder_impl.cc` | `CreateComputeNodeInfo`（属性/原型字节流）与 `BuildCtx`（裸内存上落盘 KernelRunContext） |
| `tests/ut/base/testcase/context_builder_unittest.cc` | 全部 Builder 的单测，本讲代码实践的蓝本 |

同目录下还有 `op_infer_shape_context_builder.*`、`op_infer_datatype_context_builder.*`、`op_infer_shape_range_context_builder.*`、`op_tiling_parse_context_builder.*`、`op_kernel_run_context_builder.*`，它们与 tiling 版共用同一套基类与 `BuildCtx`，只是各自追加不同的隐藏槽位。

## 4. 核心概念与源码讲解

### 4.1 OpContextBuilderBase：所有上下文 Builder 的公共基类

#### 4.1.1 概念说明

回顾 u3-l2 的结论：`KernelRunContext` 是一块「头部 + 槽位数组」的裸内存，派生类只是它的类型化视图。那么这块内存谁来填？答案分两层：

- **生产侧（框架）**：ge 在真正调用算子的 `TilingFunc` 之前，必须先在宿主内存上构造出完整的 `KernelRunContext`，填好输入 shape/tensor 指针、属性、平台信息等。
- **消费侧（算子）**：拿到的 `TilingContext *` 只是读这块内存。

ContextBuilder 就是生产侧的「装配流水线」。它把装配过程拆成两个阶段：

1. **配置阶段**：链式调用只做一件事——把参数记到一个内部结构 `OpInfo` / `TilingInfo` 里，不碰任何上下文内存。
2. **Build 阶段**：一次性分配内存、拼装 `ComputeNodeInfo` 和 `KernelRunContext`、填槽位，产出 `ContextHolder`。

基类用 CRTP（Curiously Recurring Template Pattern，奇异递归模板）支持子类链式调用：

```cpp
template <typename T>
class OpContextBuilderBase {
  T &OpType(const ge::AscendString &op_type);   // 返回 T&，而非基类引用
  ...
};
class OpTilingContextBuilder : public OpContextBuilderBase<OpTilingContextBuilder> { ... };
```

`T` 是子类类型，每个链式方法返回 `static_cast<T &>(*this)`，这样 `.OpType().CompileInfo()` 这种跨基类/子类的混合链式调用才能编译通过。

注意头文件注释中的明确约束：`IONum` 与 `IOInstanceNum` 互斥，只能调其一：

- `IONum(n, m)`：n 个输入 IR 原型、m 个输出 IR 原型，**每个原型的实例个数默认为 1**（无动态/可选输入的简单场景）。
- `IOInstanceNum(vector, vector)`：逐原型指定实例个数，用于 OPTIONAL/DYNAMIC 输入（如 Concat 的动态输入）。

#### 4.1.2 核心流程

配置阶段的内存中只有一个 `OpInfo`（见 `base/context_builder/op_info.h`），关键字段与来源对应关系：

```
OpInfo {
  op_type / op_name           <- OpType() / OpName()
  input_ir_num / output_ir_num      <- IONum() 或 IOInstanceNum() 的 vector 长度
  input_instance / output_instance  <- IOInstanceNum() 的 vector 本体
  input_instance_num / output_instance_num  <- 各实例数求和（物理槽位总数）
  input_tensor_descs / output_tensor_descs  <- InputTensors()/InputTensorDesc() 等填充
  attrs                       <- AppendAttr() 逐个追加
}
```

`IONum` 与 `IOInstanceNum` 的语义差异可用一个式子概括。设第 \( i \) 个输入原型的实例数为 \( a_i \)，则物理输入槽位总数：

\[ \text{input\_instance\_num} = \sum_{i=0}^{N-1} a_i, \quad N = \text{input\_ir\_num} \]

`IONum` 相当于固定 \( a_i = 1 \)；`IOInstanceNum` 则逐原型给定 \( a_i \)。

#### 4.1.3 源码精读

基类声明——CRTP 模板 + 全部链式接口（注意每个方法返回 `T &`）：

[inc/external/base/context_builder/op_context_builder_base.h:27-60](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/base/context_builder/op_context_builder_base.h#L27-L60)
这段代码定义了 `OpContextBuilderBase<T>` 模板：`OpType`/`OpName` 设置算子基本信息，`IONum` 设置 IR 原型个数（每个原型默认 1 个实例），`IOInstanceNum` 逐原型设置实例个数，三者都是返回 `T &` 的链式接口。

九种 `AppendAttr` 重载，覆盖 bool/int64_t/float/AscendString 及其 vector 组合：

[inc/external/base/context_builder/op_context_builder_base.h:62-83](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/base/context_builder/op_context_builder_base.h#L62-L83)
属性以「有序列表」方式追加：构造顺序就是 `ctx->GetAttrs()->GetXxx(index)` 的读取下标。头文件注释里给出了完整示例——`AppendAttr(attr0).AppendAttr(attr1)` 对应 `GetAttrs()->GetBool(0)`、`GetAttrs()->GetInt(1)`。

`IONum` 的实现——重复设置会被静默拒绝：

[base/context_builder/op_context_builder_base.cc:44-60](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/context_builder/op_context_builder_base.cc#L44-L60)
如果 `input_instance`/`output_instance` 已非空（说明之前调过 `IOInstanceNum`），只打一条 `GELOGW` 告警然后直接返回，不覆盖——这就是「互斥」的实现方式。否则把 `input_instance`/`output_instance` 全填 1，并同步扩容 `input_tensor_descs`。

`AppendAttr` 的实现——逐个转成 `ge::AnyValue` 存起来：

[base/context_builder/op_context_builder_base.cc:177-202](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/context_builder/op_context_builder_base.cc#L177-L202)
属性在配置阶段以 u2-l3 讲过的 16 字节类型擦除容器 `AnyValue` 暂存（`AnyValue::CreateFrom<T>`），Build 阶段才统一序列化进 `ComputeNodeInfo` 的属性区。`AscendString` 版本会先转 `std::string`。

模板显式实例化清单——六个 Builder 子类共用这一份实现：

[base/context_builder/op_context_builder_base.cc:242-247](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/context_builder/op_context_builder_base.cc#L242-L247)
`OpTilingContextBuilder`、`OpInferShapeContextBuilder`、`OpInferDataTypeContextBuilder`、`OpInferShapeRangeContextBuilder`、`OpTilingParseContextBuilder`、`OpKernelContextBuilder` 六个子类在此显式实例化基类模板。改基类的任何一个链式方法，六个子类同时受影响。

#### 4.1.4 代码实践

1. **实践目标**：验证 `IONum` 与 `IOInstanceNum` 的互斥语义。
2. **操作步骤**：阅读 `base/context_builder/op_context_builder_base.cc:44-60` 与 `63-86`，然后回答：如果先调 `IOInstanceNum({4}, {1})` 再调 `IONum(1, 1)`，`input_ir_num` 最终是多少？如果反过来先 `IONum(2, 1)` 再 `IOInstanceNum({4}, {1})` 呢？
3. **需要观察的现象**：对照源码中「是否检查 instance vector 非空」的判断位置。
4. **预期结果**：第一种顺序下 `IONum` 被拒绝，`input_ir_num` 保持 1（vector 长度）；第二种顺序下 `IOInstanceNum` 没有互斥检查、直接覆盖，`input_ir_num` 变为 1（新 vector 长度）。互斥检查只存在于 `IONum` 一侧——`IOInstanceNum` 会无条件覆盖。这一点从两个函数的代码结构即可读出，属源码阅读型实践，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `OpContextBuilderBase` 的链式方法返回 `T &` 而不是 `OpContextBuilderBase &`？

**答案**：返回基类引用的话，链上后续再调用子类专属方法（如 `CompileInfo`）就无法通过编译——基类引用看不到子类接口。CRTP 让每个方法 `static_cast<T &>(*this)` 返回子类引用，混合链式调用 `builder.OpType("X").CompileInfo(p)` 才能成立。

**练习 2**：`AppendAttr(int32_t(1))` 能编译通过吗？

**答案**：不能。九个重载里没有 `int32_t` 版本（只有 `int64_t`），`int32_t` 也无法隐式转换到其中任何一个而不产生歧义或匹配失败。调用时必须显式写成 `AppendAttr(int64_t(1))`——单测里正是这么写的（见 `tests/ut/base/testcase/context_builder_unittest.cc:404`）。这是刻意为之：把属性类型收窄到运行时属性系统（`RuntimeAttrs`）确定支持的几种。

### 4.2 OpTilingContextBuilder::Build：槽位拼装的全过程

#### 4.2.1 概念说明

`OpTilingContextBuilder` 在公共配置之外提供 tiling 专属配置：`CompileInfo`、`PlatformInfo`、`Deterministic`/`DeterministicLevel`、`TilingData`/`TilingDataSize`、`Workspace`、`SimtBlockDim`/`SimtGridDim`、`InputTensors`/`OutputTensors`。这些配置同样只写进内部的 `TilingInfo` 结构（[base/context_builder/op_context_builder_impl.h:25-34](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/context_builder/op_context_builder_impl.h#L25-L34)），真正的拼装发生在 `Build()`。

`Build()` 内部调用的 `BuildTilingContext()` 是本讲最核心的几十行代码，它把 u3-l3 讲的「槽位下标公式」从阅读视角反转为写入视角：

- u3-l3 说 `GetCompileInfo()` 之所以能工作，是因为 compile_info 位于「输入 + 输出 + 0」槽；
- 本讲看到的就是框架侧**按这个公式亲手把指针塞进对应槽位**的代码。

理解这一点后，TilingContext 的所有 Get 接口都不再神秘——它们与 Builder 的写入严格互为镜像。

#### 4.2.2 核心流程

`BuildTilingContext()` 的执行顺序（槽序列视角）：

```
输入(来自 InputTensors):   [0 .. in_size-1]        <- 用户配置的输入 Tensor
输出 shape/tensor:        [in_size .. in_size+out_size-1]   <- 用户配置的输出（物理上被追加到输入段之后）
隐藏槽位（顺序固定）：
  +0 CompileInfo          <- tiling_info_.compile_info_
  +1 PlatformInfo         <- tiling_info_.platform_info_
  +2 PrepareTilingFrameworkData  <- 固定 nullptr
  +3 Deterministic        <- 整数值 reinterpret_cast<void*>
  +4 DeterministicLevel   <- 同上
tiling 输出段（resize 到 kOutputNum = 11 个槽）：
  kOutputTilingData(3) / kOutputWorkspace(4) / kOutputSimtBlockDim(9) / kOutputSimtGridDim(10) 被填充，
  其余槽位为空
```

两点值得注意的设计：

1. **输出被搬进输入段尾部**：代码把 `output_values_`（用户设置的输出 shape/tensor）逐个 `emplace_back` 进 `input_values_`，随后才追加隐藏槽位。这与 u3-l2 讲的 `output_start = &values[input_size]` 完全一致——「输入在前输出在后」里的「输出」指的是 tiling 输出段，而算子的输出 shape 槽物理上排在隐藏槽位之前。最终的 `output_size`（即 tiling 输出段）是 `kOutputNum`。
2. **整数经 `reinterpret_cast<void*>` 进槽**：`deterministic` 是 `int32_t`，不是指针，直接把值强转成 `void *` 存入槽位；读取侧（u3-l3 的 `GetDeterministic()`）再转回整数。这是对 Chain 类型擦除的「值即指针」用法。

内存总量的计算公式（`BuildCtx` 中）：

\[ \text{size} = \text{sizeof(KernelRunContext)} + \text{sizeof(Chain *)} \times (\text{in\_size} + \text{out\_size}) \]

注意槽位数组里存的是**指向 Chain 的指针**，Chain 本体（`AsyncAnyValue`）另有独立的 `value_holder_` vector 存储，两者都由 `ContextHolderImpl` 持有。

#### 4.2.3 源码精读

tiling 专属配置项的声明（节选）：

[inc/external/base/context_builder/op_tiling_context_builder.h:25-48](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/base/context_builder/op_tiling_context_builder.h#L25-L48)
类继承 `OpContextBuilderBase<OpTilingContextBuilder>`，`CompileInfo`/`PlatformInfo`/`Deterministic` 等全部返回 `OpTilingContextBuilder &` 支持链式调用。注意每个方法的注释都强调：**设置的数据所有权归调用者**，调用者必须保证指针生命周期长于 Build 产生的 `ContextHolder`。

`TilingData` 与 `TilingDataSize` 两种写入风格的对比：

[inc/external/base/context_builder/op_tiling_context_builder.h:66-86](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/base/context_builder/op_tiling_context_builder.h#L66-L86)
`TilingData(ptr, deleter)` 使用外部内存（可带删除器）；`TilingDataSize(n)` 让 Builder 自己 `TilingData::CreateCap(n)` 分配并登记删除器，调用者不用管生命周期。两接口互斥，后调用者胜。

核心拼装逻辑 `BuildTilingContext()`：

[base/context_builder/op_tiling_context_builder.cc:23-47](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/context_builder/op_tiling_context_builder.cc#L23-L47)
按顺序做了六件事：① 校验 compile_info/platform_info 非空（缺失则 Build 失败返回空 holder）；② `CreateComputeNodeInfo` 拼装属性与原型信息；③ 把输出槽追加到输入段之后；④ 按固定顺序追加五个隐藏槽（compile/platform/prepare-framework-data/deterministic/deterministic-level）；⑤ 把输出段 resize 到 `TilingContext::kOutputNum` 并只在 TilingData/Workspace/SimtBlockDim/SimtGridDim 四个下标填值；⑥ `BuildCtx` 落盘。对照 u3-l3 的读取公式，写入与读取一一镜像。

一个工程细节——用 `static_assert` 锁实现类大小：

[base/context_builder/op_tiling_context_builder.cc:49-50](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/context_builder/op_tiling_context_builder.cc#L49-L50)
`OpTilingContextBuilderImpl` 只继承 `ContextBuilderImpl` 并新增 `BuildTilingContext` 方法、不加任何成员，`static_assert` 在编译期锁死「派生实现类不偷偷加字段」，保证 `static_cast` 往返安全。

`TilingDataSize` 的实现——自管生命周期的完整闭环：

[base/context_builder/op_tiling_context_builder.cc:97-107](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/context_builder/op_tiling_context_builder.cc#L97-L107)
`CreateCap` 分配容量为 n 的 `TilingData`，lambda 删除器负责 `delete[]`，之后交给 `SetTilingData` 登记。配合 [base/context_builder/op_context_builder_impl.h:105-110](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/context_builder/op_context_builder_impl.h#L105-L110) 的 `SetTilingData`——再次调用时先用旧删除器释放旧数据再登记新的，即「后调用覆盖前调用」的实现。

`InputTensors` 的双向记录——既记槽位又记描述：

[base/context_builder/op_tiling_context_builder.cc:127-139](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/context_builder/op_tiling_context_builder.cc#L127-L139)
每个输入 Tensor 除了指针进槽位（`impl_->Inputs`），还把它的 DataType/OriginFormat/StorageFormat/ExpandDimsType 抄进 `input_tensor_descs`——这些描述稍后会被写进 `ComputeNodeInfo` 的 `CompileTimeTensorDesc` 数组，供 `GetInputDesc` 读取。也就是说一个输入在上下文里有**两份记录**：槽位里的 Tensor 本体 + ComputeNodeInfo 里的编译期描述。

弱符号纯 C 接口（GE 与 metadef 包间前后兼容的技巧）：

[inc/external/base/context_builder/op_tiling_context_builder.h:140-175](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/base/context_builder/op_tiling_context_builder.h#L140-L175)
`gert_TilingContextBuilder_SetDeterministicLevel` 等三个接口声明为 `__attribute__((weak))` 的纯 C 函数：当 GE 侧链接的旧版 metadef 没有这个符号时不会报链接错误，运行时判空即可跳过。这样新增配置项不会破坏新旧包混布。实现在 [base/context_builder/op_tiling_context_builder.cc:168-207](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/context_builder/op_tiling_context_builder.cc#L168-L207)，做参数校验后转调 C++ 成员方法。

#### 4.2.4 代码实践

1. **实践目标**：对照源码手工推导一次槽位布局，验证与 u3-l3 的读取公式一致。
2. **操作步骤**：
   - 设某算子 `IONum(2, 1)`，即 2 个输入、1 个输出原型（各 1 实例）。
   - 按 `BuildTilingContext` 的顺序写出 values 数组每个下标的内容。
   - 再到 `inc/external/exe_graph/runtime/tiling_context.h` 中找到 `GetCompileInfo`、`GetPlatformInfo`、`GetDeterministic` 的实现，核对它们用的下标常量。
3. **需要观察的现象**：读取侧下标公式算出的位置，是否正好落在写入侧第 in+out、in+out+1、in+out+3 个槽上。
4. **预期结果**：布局为 `[in0, in1, out0(shape), compile_info, platform_info, nullptr, deterministic, deterministic_level, tiling_key, block_dim, atomic, tiling_data, workspace, cond, mode, ub, aicpu_dim, simt_block, simt_grid]`（tiling 输出段共 `kOutputNum = 11` 槽，多数为空）。若核对不一致，说明你漏算了「输出槽被追加到输入段尾部」这一步。本实践为源码阅读型，结论可完全静态推导，无需运行环境。

#### 4.2.5 小练习与答案

**练习 1**：如果只设置了 `CompileInfo` 忘了 `PlatformInfo`，`Build()` 会发生什么？

**答案**：`BuildTilingContext` 开头的 `GE_ASSERT_NOTNULL(tiling_info_.platform_info_, ...)` 断言失败，函数返回空的 holder（`ContextHolderImpl` 的 `unique_ptr` 为空），后续 `holder.GetContext()` 返回 `nullptr`。不会崩溃——失败语义是空值，与单测 `CreateKernelRunContextFailed`（缺 OpType 时 `GetContext()` 得到 nullptr）一致。

**练习 2**：先调 `TilingDataSize(100)` 再调 `TilingData(ptr)`，最终生效的是哪个？反过来呢？

**答案**：都以最后调用者为准。`TilingDataSize` 内部也走 `SetTilingData`，而 `SetTilingData`（op_context_builder_impl.h:105-110）会先释放旧值再登记新值。单测 `CreateTilingContextTilingDataSizeOK`（context_builder_unittest.cc:784-834）两种顺序都验证了：先 Size 后 Data 得到容量 120 的外部指针，先 Data 后 Size 得到容量 100 的自建指针。

**练习 3**：`Deterministic(int32_t)` 的值是怎么进入上下文、又怎么被 `GetDeterministic()` 读出来的？

**答案**：写入侧 `BuildTilingContext` 用 `reinterpret_cast<void *>(tiling_info_.deterministic_)` 把整数值本身当作「指针」塞进隐藏槽；读取侧把槽位里的 `void *` 再 `reinterpret_cast` 回整数。Chain 在这里只当一个 8 字节透明的值槽用，deleter 为空。

### 4.3 ComputeNodeInfo 与 BuildCtx：两段式内存落盘

#### 4.3.1 概念说明

`Build()` 的最后两步是两块独立的内存分配：

1. **`CreateComputeNodeInfo`**：把 `op_info_` 里的原型个数、实例个数、tensor 描述、属性列表，序列化成一段连续字节流，头部按 `ComputeNodeInfo` 解释。属性经 `bg::CreateAttrBufferWithAttrs` 从 `vector<AnyValue>` 压成扁平 buffer——这正是 u3-l2 里 `GetAttrs()` 能拿到 `RuntimeAttrs` 的原因。
2. **`BuildCtx`**：按头部公式分配 `KernelRunContext` 内存，填 `input_size`/`output_size`/`compute_node_info` 指针/`output_start`，再把每个槽位的 `values[i]` 指向独立的 `Chain` 本体，最后 `Chain::Set` 登记数据指针。

一个容易忽略的细节：`string_pool_`。`ComputeNodeInfo::Init` 需要算子名/类型的 `const char *`，而这些字符串必须活过 Build——所以 op_name/op_type 被复制进 `holder.string_pool_`（`vector<std::string>`），再把 `c_str()` 传给 `Init`。这就是字符串所有权的处理方式。

#### 4.3.2 核心流程

```
Build()
 ├─ BuildTilingContext()
 │   ├─ CreateComputeNodeInfo(holder)
 │   │   ├─ 校验 op_type/op_name/input_ir_num/output_ir_num 非空
 │   │   ├─ CreateAttrBufferWithAttrs(attrs) -> attr_buf
 │   │   ├─ ComputeNodeInfo::CalcSize(...) 算 total_size（含溢出检查）
 │   │   ├─ Init: 写头部 + op_name/op_type 存入 string_pool
 │   │   ├─ InitIOInstanceInfo: 每个原型写 InstanceStart/InstantiationNum
 │   │   ├─ InitCompileTimeTD: 每个输入/输出写 CompileTimeTensorDesc
 │   │   └─ memcpy_s: 属性 buffer 拷到 ComputeNodeInfo 尾部 RuntimeAttrs 区
 │   ├─ 追加隐藏槽位 / 填 tiling 输出段（见 4.2）
 │   └─ BuildCtx(holder)
 │       ├─ 分配 sizeof(KernelRunContext) + 8*(in+out) 字节
 │       ├─ 填头部: input_size/output_size/compute_node_info/output_start
 │       ├─ value_holder_ 中造 Chain，values[i] 指向各自 Chain
 │       └─ 逐槽 Chain::Set(data, deleter)
 └─ ContextHolderBuilder::Create(holder_impl) -> ContextHolder<TilingContext>
```

#### 4.3.3 源码精读

`CreateComputeNodeInfo` 的参数校验与属性序列化入口：

[base/context_builder/op_context_builder_impl.cc:103-115](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/context_builder/op_context_builder_impl.cc#L103-L115)
四个硬性前提：op_type、op_name 非空且输入输出 IR 原型数不为 0，任一不满足即 Build 失败。属性从 `vector<AnyValue>` 压成连续 buffer，这是 AppendAttr 配置到 RuntimeAttrs 读取之间的桥梁。

字节流拼装 `CreateComputeNodeInfoImpl`：

[base/context_builder/op_context_builder_impl.cc:59-102](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/context_builder/op_context_builder_impl.cc#L59-L102)
这段代码先 `CalcSize` 计算含头部、实例信息、tensor 描述、输出 AnchorInstanceInfo、属性区的总大小（带加法溢出检查），把 op_name/op_type 复制进 `string_pool_` 再 `Init` 头部，随后 `InitIOInstanceInfo` 写每个原型的 `InstanceStart`（起始物理下标）与 `InstantiationNum`——这正是 u3-l2「AnchorInstanceInfo 做 IR 索引到物理槽位翻译」的数据来源。最后 `memcpy_s` 把属性 buffer 拷进尾部，拷贝前还校验了 offset 不越过总长度。

`BuildCtx` 在裸内存上落盘 KernelRunContext：

[base/context_builder/op_context_builder_impl.cc:116-142](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/context_builder/op_context_builder_impl.cc#L116-L142)
分配 `sizeof(KernelRunContext) + sizeof(Chain *) * io_size` 字节；把首地址 `PtrToPtr` 成 `KernelContext *`（这正是 u3-l2 说的「裸内存 + reinterpret_cast 换视图」，只是这次由 Builder 亲自示范）；填四个头部字段（注意 `output_start = &values[input_size]` 是在算地址而非存下标）；为每个槽位造一个 Chain 本体并让 `values[i]` 指向它；最后逐槽 `Set(数据指针, deleter)`。至此，算子侧读到的上下文完全成型。

`InitIOInstanceInfo` 写入的「IR 索引 → 物理槽位」映射：

[base/context_builder/op_context_builder_impl.cc:23-40](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/context_builder/op_context_builder_impl.cc#L23-L40)
对第 i 个输入原型，`InstanceStart` 是它第一个实例的物理下标（前面所有原型实例数的前缀和），`InstantiationNum` 是实例个数。u3-l2/l3 讲的 REQUIRED/OPTIONAL/DYNAMIC 三类访问最终都靠这两个字段换算物理下标。

#### 4.3.4 代码实践

1. **实践目标**：验证 `ComputeNodeInfo` 中的实例信息由 `IOInstanceNum` 配置直接决定。
2. **操作步骤**：阅读单测 `CreateInferDataTypeContextOK`（[tests/ut/base/testcase/context_builder_unittest.cc:81-121](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/context_builder_unittest.cc#L81-L121)）：该用例 `IOInstanceNum({4}, {1})` 构造 Concat，断言 `GetIrInputsNum() == 1` 而 `GetInputsNum() == 4`。
3. **需要观察的现象**：IR 原型个数（1）与物理输入槽个数（4）为何不同，两者分别来自 `IOInstanceNum` 的哪个属性。
4. **预期结果**：`input_ir_num = vector 长度 = 1`，`input_instance_num = 求和 = 4`；`GetIrInputsNum` 读前者、`GetInputsNum` 读后者。这就是动态输入在 Builder 层的表达。源码阅读型实践，结论可从 `op_context_builder_base.cc:63-86` 直接推出。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `op_name`/`op_type` 必须复制进 `string_pool_`，而不能直接用调用者传入的 `AscendString`？

**答案**：`ComputeNodeInfo::Init` 只保存 `const char *`，不拥有字符串。调用者的 `AscendString`（及其内部 `std::string`）可能在 Build 后销毁，而上下文要活到算子执行完毕。复制进 holder 拥有的 `string_pool_` 后，指针指向的内存与上下文同寿命。

**练习 2**：`values[i]` 槽里存的是什么？Chain 本体又存在哪里？

**答案**：`values[i]` 存的是指向 Chain 的**指针**；Chain 本体（`AsyncAnyValue`）存在 `holder.value_holder_` 这个 `vector<Chain>` 里。所以一次 Build 实际产生三块内存：KernelRunContext 字节流、Chain 数组、ComputeNodeInfo 字节流，全部由 holder 统一持有。

### 4.4 ContextHolder：资源所有权与生命周期

#### 4.4.1 概念说明

`Build()` 不返回裸指针，而返回 `ContextHolder<TilingContext>`。为什么？因为一次 Build 产生了三块内存（上下文字节流、Chain 数组、ComputeNodeInfo 字节流）加一个字符串池，还可能登记了 tiling data 的删除器——这些资源的生命周期必须与上下文严格绑定。`ContextHolder` 就是它们的 RAII 容器：

- 构造时接管全部资源；
- 析构时先把每个 Chain `Set(nullptr, nullptr)`（触发已登记的 deleter，比如 `TilingDataSize` 建的 tiling data 释放），再由各 `unique_ptr`/`vector` 成员自动释放内存。

对外侧用两层包装隔离 ABI：`ContextHolderVoid`（类型无关、pimpl）+ 模板 `ContextHolder<T>`（只做一次 `static_cast`）。这样对外头文件不需要暴露任何内部结构。

生命周期契约（头文件注释反复强调）：**ContextHolder 的生命周期必须 ≥ 从它取出的所有指针的使用期**。输入 Tensor、compile_info 等外部指针归调用者所有，holder 只保证自己那部分内存。

#### 4.4.2 核心流程

```
Build()
  └─ ContextHolderBuilder::Create(unique_ptr<ContextHolderImpl>)
       -> ContextHolderVoid（移动接管 impl）
       -> ContextHolder<TilingContext>（包装）

holder.GetContext<T>()
  -> holder_void_.GetContext()
     -> ctx_holder_impl_->GetContext<void*>()   // reinterpret_cast 到目标类型

~ContextHolderImpl()
  -> 每个Chain Set(nullptr, nullptr)   // 触发 deleter（如自建 tiling data）
  -> unique_ptr / vector 成员析构        // 释放三块内存与字符串池
```

#### 4.4.3 源码精读

`ContextHolderVoid` 与模板 `ContextHolder` 的对外壳：

[inc/external/base/context_builder/context_holder.h:17-48](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/inc/external/base/context_builder/context_holder.h#L17-L48)
`ContextHolderVoid` 只有一个 `unique_ptr<ContextHolderImpl>` 成员（pimpl），可移动不可拷贝；`ContextHolder<T>` 包住它并提供 `GetContext()` 做一次 `static_cast`。类型擦除的代价仅仅是模板层的一层薄壳。

资源池 `ContextHolderImpl` 的全部家当：

[base/context_builder/op_context_builder_impl.h:38-75](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/context_builder/op_context_builder_impl.h#L38-L75)
四个成员正好对应四份资源：`context_holder_`（KernelRunContext 字节流）、`value_holder_`（Chain 数组）、`compute_node_info_holder_`（ComputeNodeInfo 字节流）、`string_pool_`（算子名/类型字符串），外加裸指针 `context_`。析构函数先把所有 Chain 置空以触发删除器，其余交给成员自动析构。移动构造/赋值把 `context_` 置 nullptr 防止旧对象析构后悬空。

`GetContext` 的判空失败语义：

[base/context_builder/context_holder.cc:15-18](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/context_builder/context_holder.cc#L15-L18)
impl 为空（Build 失败产生的空 holder）时 `GetContext` 断言失败返回 `nullptr`——这就是单测里 `EXPECT_EQ(ctx, nullptr)` 检验 Build 失败的通路。

内部工厂（仅 base 侧可见）：

[base/context_builder/context_holder_builder.h:15-23](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/base/context_builder/context_holder_builder.h#L15-L23)
`ContextHolderBuilder::Create` 是唯一能往 `ContextHolderVoid` 的私有成员里塞 impl 的入口（靠 friend 授权），把「构造合法 holder」的能力限制在库内部。

#### 4.4.4 代码实践

1. **实践目标**：验证 holder 的 RAII 释放路径。
2. **操作步骤**：阅读 `op_context_builder_impl.h:60-64` 的析构函数与 `op_tiling_context_builder.cc:97-107` 的 `TilingDataSize`，回答：用 `TilingDataSize(100)` 构建上下文后，让 holder 离开作用域，那块 tiling data 会被释放吗？如果改用 `TilingData(ptr)`（不带 deleter）呢？
3. **需要观察的现象**：deleter 登记链路——`SetTilingData` 存 `(ptr, deleter)`，Build 时进槽位，析构时 `Set(nullptr, nullptr)` 触发。
4. **预期结果**：`TilingDataSize` 场景会释放（自建内存自释放，这也是头文件建议优先用它的原因）；`TilingData(ptr)` 不带 deleter 时槽位 deleter 为空，析构不释放、指针归还调用者。源码阅读型实践。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `ContextHolder<T>` 用 `static_cast` 而 `ContextHolderImpl::GetContext<T>` 用 `reinterpret_cast`？

**答案**：`ContextHolderImpl` 里存的是 `KernelContext *`，转成任意目标上下文类型（如 `TilingContext *`）是「同一块内存换视图」，属指针的底层重解释，用 `reinterpret_cast`。而 `ContextHolderVoid::GetContext()` 返回 `void *`，`void *` 到具体类型指针的转换 `static_cast` 即可。两类转换都合法的前提是 u3-l2 建立的「继承链零新增数据成员」契约。

**练习 2**：如果 Build 之后立刻销毁 builder 对象（`OpTilingContextBuilder`），已 Build 出的上下文还可用吗？

**答案**：可用。Build 时 `BuildTilingContext()` 返回的 `ContextHolderImpl` 被移动进返回的 `ContextHolder`，与 builder 内部的 `impl_` 脱钩（builder 的 `impl_` 仍在但不再被引用）。真正决定上下文寿命的是 holder，不是 builder。

## 5. 综合实践

**任务**：为假想算子 `MyAdd`（2 输入 1 输出，1 个 int64 属性）编写一个可编译的单测，用 `OpTilingContextBuilder` 构建 `TilingContext`，驱动一个手写 TilingFunc，断言回读一致。

完整步骤：

1. **准备**：按 u1-l2 完成 `bash build.sh` 与 `bash tests/run_test.sh -u` 的环境（需先 source CANN 的 `set_env.sh` 设置 `ASCEND_HOME_PATH`）。
2. **编写测试**（示例代码，蓝本为 [tests/ut/base/testcase/context_builder_unittest.cc:364-481](https://github.com/gitcode.com/cann/metadef/blob/0005f5b3b38bed310be5f990f5b174941997bc7d/tests/ut/base/testcase/context_builder_unittest.cc#L364-L481) 的 `CreateTilingContextOK`）：

   ```cpp
   // 示例代码：可加入 tests/ut/base/testcase/context_builder_unittest.cc
   static ge::graphStatus MyAddTilingFunc(gert::TilingContext *ctx) {
     auto shape = ctx->GetInputShape(0);              // 读输入 shape
     if (shape == nullptr) { return ge::GRAPH_FAILED; }
     auto attrs = ctx->GetAttrs();
     if ((attrs == nullptr) || (attrs->GetInt(0) == nullptr)) { return ge::GRAPH_FAILED; }
     auto tiling_data = ctx->GetRawTilingData();      // 追加式写 tiling 结果
     if (tiling_data->Append<int64_t>(*(attrs->GetInt(0))) == nullptr) {
       return ge::GRAPH_FAILED;
     }
     ctx->SetBlockDim(1);
     return ge::GRAPH_SUCCESS;
   }

   TEST_F(UtestContextBuilder, MyAddTilingEndToEnd) {
     gert::StorageShape in_shape({16, 16}, {16, 16});
     gert::Tensor x(in_shape, {ge::FORMAT_ND, ge::FORMAT_ND, gert::ExpandDimsType()}, ge::DT_FLOAT);
     gert::Tensor y = x;
     gert::Tensor out = x;
     uint8_t compile_info[8] = {0};
     uint8_t platform_info[8] = {0};

     OpTilingContextBuilder builder;
     auto holder = builder.OpType("MyAdd")
                       .OpName("my_add_1")
                       .IONum(2, 1)
                       .AppendAttr(int64_t(42))
                       .CompileInfo(compile_info)
                       .PlatformInfo(platform_info)
                       .TilingDataSize(64)
                       .InputTensors({&x, &y})
                       .OutputTensors({&out})
                       .Build();
     auto ctx = holder.GetContext();
     ASSERT_NE(ctx, nullptr);

     // 断言一：写入的 shape/attr 能原样读回
     EXPECT_EQ(ctx->GetInputShape(0)->GetOriginShape(), in_shape.GetOriginShape());
     EXPECT_EQ(*(ctx->GetAttrs()->GetInt(0)), 42);

     // 断言二：真实 TilingFunc 能在构建出的上下文上运行并写回结果
     EXPECT_EQ(MyAddTilingFunc(ctx), ge::GRAPH_SUCCESS);
     EXPECT_EQ(ctx->GetRawTilingData()->GetCapacity(), 64U);
   }
   ```
3. **运行**：`tests/ut/base/CMakeLists.txt` 用 glob 收集 `tests/ut/base/testcase/*.cc`（见 u1-l2），把用例加进现有 `context_builder_unittest.cc` 后无需改 CMake，直接：

   ```bash
   bash tests/run_test.sh -u
   ```

   可用 `--gtest_filter=UtestContextBuilder.MyAddTilingEndToEnd` 缩小范围（具体过滤方式待本地验证）。
4. **预期结果**：两条断言全部通过——Builder 写入的 shape 与属性被上下文原样读回；TilingFunc 消费上下文并把 tiling 结果写进 `TilingDataSize` 分配的容器。若 `CompileInfo`/`PlatformInfo` 漏设，`GetContext()` 返回 `nullptr`，`ASSERT_NE` 会让你立刻看到。
5. 本实践的完整运行依赖本地 CANN 环境，无法在阅读环境执行，标注「待本地验证」；但代码中每个 API 均可在本讲引用的头文件中找到对应声明。

## 6. 本讲小结

- ContextBuilder 是框架侧的上下文装配流水线：链式调用只把配置写进 `OpInfo`/`TilingInfo`，`Build()` 才一次性分配三块内存（KernelRunContext 字节流、Chain 数组、ComputeNodeInfo 字节流）并填槽。
- `BuildTilingContext()` 与 u3-l3 的读取接口严格互为镜像：输出 shape 槽被追加到输入段尾部，五个隐藏槽（compile/platform/prepare-data/deterministic/deterministic-level）按固定顺序紧随其后，tiling 结果写入 `TilingOutputIndex` 枚举的 11 个输出槽。
- `InputTensors` 对每个输入做「双份记录」：Tensor 本体进槽位，其 dtype/format 描述抄进 ComputeNodeInfo 的 `CompileTimeTensorDesc`，分别服务运行时读取与编译期描述查询。
- `IONum`（每原型 1 实例）与 `IOInstanceNum`（逐原型指定实例数）互斥；`InitIOInstanceInfo` 写入的前缀和（InstanceStart）+ 实例数就是运行时「IR 索引 → 物理槽位」翻译的数据来源。
- `ContextHolder` 是全部构建资源的 RAII 容器，对外经 `ContextHolderVoid` pimpl 隔离 ABI；生命周期契约是 holder ≥ 一切从中取出的指针。
- 弱符号纯 C 接口（`gert_TilingContextBuilder_SetDeterministicLevel` 等）是 GE 与不同版本 metadef 包混布时的前后兼容手段。

## 7. 下一步学习建议

- 下一讲 u5-l2（插件管理：动态库加载与符号解析）将承接 u4-l5 的 so 加载话题，讲解 `PluginManager` 这一公共底座。
- 建议继续阅读的源码：
  - `base/context_builder/op_infer_shape_context_builder.cc` 与 `op_tiling_parse_context_builder.cc`——对照本讲，观察它们各自追加了哪些隐藏槽位，验证「同一套 BuildCtx、不同槽位配方」的分层设计。
  - `base/attr/attrs_to_buffer.h/.cc`——`CreateAttrBufferWithAttrs` 如何把 `vector<AnyValue>` 压成扁平属性字节流，是 u2-l3 AnyValue 知识的工程落点。
  - `inc/external/exe_graph/runtime/compute_node_info.h`——`CalcSize` 与 `Init` 的定义处，理解字节流的精确构成。
- 若你正在做二次开发：为 TilingContext 增加新槽位时，必须同时改 `TilingOutputIndex`（只能尾部追加）与 `BuildTilingContext` 的输出段填充，并同步评估 u5-l4 将讲的 ABI 兼容约束。
