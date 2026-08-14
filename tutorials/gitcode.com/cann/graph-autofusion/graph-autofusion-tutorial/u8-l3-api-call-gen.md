# u8-l3 api_call 算子调用生成

> 本讲为 update 版本，基于 HEAD `2b9c5c2a` 重建。相对上一版（HEAD `00627d97`），`git diff 00627d97..2b9c5c2a` 显示 api_call 目录的变化集中在：`where_api_call.cpp`、`compare_api_call.cpp`、`logical_not_api_call.cpp` 等调用生成器统一接入了 dtype 感知的 CV 融合对齐工具，`api_call_utils.h/cpp` 新增了 `IsCVFusionStage` / `GetTensorDtypeSize` / `GenBlockAlignNExpr` / `GetCVAlignedSize` 四个公共接口。本讲以当前代码为准完整讲解，并重点覆盖这些新机制。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清「一个融合图中的算子节点，如何变成 kernel 源码里的一行 AscendC 调用语句」这条完整链路。
2. 理解 `ApiCallFactory` 工厂 + 自注册模式如何把 ASCIR 算子类型映射到对应的 `ApiCall` 生成器类。
3. 掌握 elewise 家族（unary / binary / where / compare / logical_not）调用生成器的内部结构与差异。
4. 了解 `api_call_utils` 公共工具的职责，特别是本次新增的 CV 融合场景 dtype 感知块对齐接口 `GetCVAlignedSize`。
5. 理解 `AscendCApiRegistry` 如何把设备端函数定义（`*_str.h` 原始字符串）按需拼进生成的 kernel 源码。

## 2. 前置知识

- **api_call 是什么**：在 u8-l2 中我们看到，`Kernel::Generate` 会遍历融合图节点，为每个算子生成一段 C++ 调用语句。生成这段语句的策略类就叫 **ApiCall**——它不计算任何数值，只负责「打印」出形如 `Exp(y[off], x[off], n);` 的设备端代码字符串。
- **AscendC 双端协作**（承接 u5-l3）：api_call 层生成「调用语句」，`autofuse/ascendc/api/*.h` 提供「函数定义」。定义被 sed 包成原始字符串字面量 `*_str.h`，注册进 `AscendCApiRegistry`，生成 kernel 时按图中算子按需取出拼进源码。
- **CV 融合**（承接 u8-l2）：Cube（矩阵乘）与 Vector 算子融合为一个 kernel 时，Vector 侧算子消费的是 Cube 留在 UB 上的输出。此时 Vector API 的搬运粒度必须按 dtype 做统一的 32 字节块对齐，否则不同 dtype 的 N 方向长度会破坏块对齐约束。
- **符号表达式**（承接 u6/u7）：因为支持动态 shape，生成语句里的长度、偏移都是 `SizeExpr` / `CombinedExpression` 符号表达式，由 `tpipe.tiler` 负责翻译成最终字符串。
- **`TmpBufDesc` 与 tmp_buf**（承接 u5-l2）：部分算子（如 Where、Compare、Div）的设备端实现需要一个临时缓冲，其 id 在 reg_func sizing 阶段确定，生成调用时通过 `tpipe.tmp_buf_<id>` 引用。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `autofuse/codegen/api_call/utils/api_call_factory.h` | ApiCall 工厂与自注册模板，`CreateApiCallObject` 是节点→生成器的总入口 |
| `autofuse/codegen/codegen_kernel_loop.h` | `ApiCall` 抽象基类、`ApiCallContext` / `ComputeStage` / `ApiScene` 定义 |
| `autofuse/codegen/api_call/elewise/unary_api_call.h/.cpp` | 最简单的单输入调用生成器 |
| `autofuse/codegen/api_call/elewise/binary_api_call.h/.cpp` | 双输入调用生成器，含标量换位、brc-inline 分支 |
| `autofuse/codegen/api_call/elewise/where_api_call.h/.cpp` | 三输入 Where 生成器，五种场景分支，本次更新的重点文件 |
| `autofuse/codegen/api_call/elewise/compare_api_call.cpp`、`logical_not_api_call.cpp` | 本次接入 CV 对齐工具的另外两个生成器 |
| `autofuse/codegen/api_call/utils/api_call_utils.h/.cpp` | 公共工具：循环参数结构体、DMA 参数、CV 对齐表达式等 |
| `autofuse/codegen/ascendc_api_registry.h/.cpp` | 设备端函数定义的注册与查询单例 |
| `autofuse/ascir/generator/v1_ascir_codegen_impl.h` | ASCIR 算子到 `ApiCall` 类名 / API 名 / 头文件的映射来源 |
| `autofuse/tests/ut/codegen/api_call/test_codegen_where_api_call.cpp` | Where 生成器的 gtest 单测，可作行为参照 |

api_call 目录按算子家族分子目录组织：`elewise/`（逐元素）、`datacopy/`（load/store）、`reduce/`、`broadcast/`、`concat/`、`transpose/`、`gather/`，公共工具收敛在 `utils/`。

## 4. 核心概念与源码讲解

### 4.1 api_call 工厂：从图节点到生成器对象

#### 4.1.1 概念说明

融合图上每个算子节点类型不同（Exp、Add、Where、Load……），但 kernel 生成主流程（u8-l2 的 `Kernel::Generate`）只想用统一接口「给我这个节点的调用语句」。为此 codegen 采用**工厂 + 自注册**模式：

- 每个生成器类在自己的 .cpp 末尾用 `static ApiCallRegister<T> register_xxx("XxxApiCall")` 注册类名→构造函数；
- 工厂 `ApiCallFactory` 持有 `class_name → creator` 的 map；
- 「节点类型 → 类名」的映射则来自 ASCIR 注册体系（u5-l1 的 `AscIrImpl` 三元组中 codegen 侧实现的 `GetApiCallName()` / `GetApiName()`）。

这样三段接力：**ASCIR 算子类型 → AscIrCodegenImpl（给出类名与 API 名）→ ApiCallFactory（造出 ApiCall 对象）→ Generate（打印调用语句）**。

#### 4.1.2 核心流程

```
Kernel::Generate 遍历节点
  └─ CreateApiCallObject(node)
       ├─ GetAscIrCodegenImpl(node->GetType())   # 取 ASCIR codegen 实现
       ├─ impl->GetApiCallName()                  # 如 "WhereApiCall"
       ├─ impl->GetApiName()                      # 如 "Where"
       └─ ApiCallFactory::Create(class_name, api_name)
            └─ creator_map_[class_name](api_name) # new WhereApiCall("Where")
```

#### 4.1.3 源码精读

工厂本体，注意线程安全的双检注册与查询：

[autofuse/codegen/api_call/utils/api_call_factory.h:L25-L72](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/api_call/utils/api_call_factory.h#L25-L72) —— `ApiCallFactory` 单例：`Create(class_name, api_name)` 加锁查 `creator_map_`，找不到打告警返回 nullptr；内嵌 `Registerar` 类供注册方使用，重复注册静默忽略（幂等）。

[autofuse/codegen/api_call/utils/api_call_factory.h:L74-L85](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/api_call/utils/api_call_factory.h#L74-L85) —— `ApiCallRegister<T>` 模板：构造时生成 lambda `new T(name)` 并交给工厂。每个生成器 .cpp 末尾的 `static ApiCallRegister<...>` 全局对象在 main 之前完成注册，这与 u5-l1 讲过的 ASCIR 自注册是同一个套路。

[autofuse/codegen/api_call/utils/api_call_factory.h:L87-L96](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/api_call/utils/api_call_factory.h#L87-L96) —— `CreateApiCallObject(node)` 是总入口：先查 ASCIR codegen 实现，再取 `GetApiCallName()`（生成器类名）与 `GetApiName()`（设备端 API 名）去工厂造对象。

映射的来源在 ASCIR 侧，例如 Where 与 Div：

[autofuse/ascir/generator/v1_ascir_codegen_impl.h:L1497-L1504](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/ascir/generator/v1_ascir_codegen_impl.h#L1497-L1504) —— `WhereAscIrCodegenImpl::GetApiCallName()` 返回 `"WhereApiCall"`，即把 Where 算子绑到 Where 生成器类。

[autofuse/ascir/generator/v1_ascir_codegen_impl.h:L1199-L1205](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/ascir/generator/v1_ascir_codegen_impl.h#L1199-L1205) —— Div 的实现返回 `"BinaryApiCall"` 并声明 `LoadApiHeaderFiles` 加载 `scalar_div.h`。Add/Sub/Mul 等大量双输入算子都共享同一个 `BinaryApiCall` 类，只是 `api_name` 不同——「一个生成器类服务一族 API」是 api_call 目录复用的基本手法。

所有生成器的公共基类与上下文：

[autofuse/codegen/codegen_kernel_loop.h:L76-L117](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_kernel_loop.h#L76-L117) —— `ApiCall` 抽象基类：核心虚函数是 `Init(node)`（解析节点属性、准备上下文）和 `Generate(tpipe, current_axis, inputs, outputs, result)`（打印调用语句），还有 `PreProcess/PostProcess` 等钩子。子类只需覆写自己关心的部分。

[autofuse/codegen/codegen_kernel_loop.h:L144-L145](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_kernel_loop.h#L144-L145) —— 成员 `api_call_context`（携带 scene/stage，见 4.3.3）与 `tmp_buf_id`（reg_func 阶段确定的临时缓冲 id 表）。**每个 ApiCall 对象自带生成上下文**，这是本次 CV 融合改动能以最小侵入落地的关键。

#### 4.1.4 代码实践

1. **实践目标**：亲手验证「算子类型 → 生成器类名」的映射表。
2. **操作步骤**：
   - 在仓库根目录执行 `grep -n 'GetApiCallName' autofuse/ascir/generator/v1_ascir_codegen_impl.h | head -20`，记下每个实现返回的类名；
   - 再执行 `grep -rn 'static ApiCallRegister<' autofuse/codegen/api_call/ | head -20`，列出实际注册进工厂的类名。
3. **需要观察的现象**：两份清单应能一一对应；统计哪些类名被多个算子共享（如 `BinaryApiCall`、`UnaryApiCall`、`CompareApiCall`）。
4. **预期结果**：注册类名集合 ⊇ 映射表引用的类名集合；若某算子映射到不存在的类名，工厂 `Create` 会打 `Cannot find node type ... in inner map` 告警并返回 nullptr（见 factory L36）。
5. 以上为纯源码阅读型实践，无需运行环境。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `CreateApiCallObject` 要传两个名字（class_name 与 api_name），而不是只传 class_name？
**答案**：一个生成器类服务一族 API（如 `BinaryApiCall` 同时服务 Add/Sub/Mul/Div），类名只决定「用哪个生成器」，`api_name` 决定「打印出的设备端函数名」（`this->api_name_` 直接进入生成语句）。二者是「策略」与「数据」的关系。

**练习 2**：新增一个双输入算子（逻辑同 Add）时，需要新写一个 ApiCall 子类吗？
**答案**：不需要。只要在 ASCIR 注册（u5-l1）的 codegen 实现里 `GetApiCallName()` 返回 `"BinaryApiCall"`、`GetApiName()` 返回新 API 名即可复用现有生成器。

### 4.2 elewise 调用生成：unary / binary / where / compare

#### 4.2.1 概念说明

elewise（逐元素）家族是融合图里数量最多的一类算子。它们的调用语句结构高度相似——输出、若干输入、一个元素个数——但细节差异决定了生成器复杂度的阶梯：

| 生成器 | 输入数 | 特殊分支 | 复杂度 |
| --- | --- | --- | --- |
| `UnaryApiCall` | 1 | 无 | 一行语句 |
| `BinaryApiCall` | 2 | 标量换位、双标量/单标量/无标量三分支、Div/Sub 需 tmp_buf、brc-inline | 中 |
| `CompareApiCall` | 2 | CMPMODE 模板参数、Extend 版带循环轴 | 中 |
| `WhereApiCall` | 3 | 五种场景分支（无循环/双标量/x2 标量/x3 标量/普通） | 高 |

#### 4.2.2 核心流程

以 `BinaryApiCall::Generate` 为例：

```
Generate(tpipe, axis, inputs, outputs)
  ├─ generalized_brc_inline_scene? → BrcInlineGenerate（广播内联路径）
  ├─ inputs[0] 是标量而 inputs[1] 不是? → 交换 x1/x2（scheduler 无法调换 Data/Scalar 顺序）
  ├─ 双输入皆标量   → api_names(y[..], (dtype)v1, (dtype)v2);
  ├─ 恰一输入为标量 → Div/Sub 需 tmp_buf_<id>；DivExtend/SubExtend 不需要；其余 api_names(...)
  └─ 皆非标量      → api_name(y[..], x1[..], x2[..], n);
```

`WhereApiCall::Generate` 则先做四步准备（`PrepareInputsAndOutputs` → `GetTempBufferId` → `RegisterBasicDumpParam` → `GenerateLoopParams`），再按 `outer_repeats` 是否为空、x2/x3 是否标量分派到五个私有 `GenerateXxxCase`。

#### 4.2.3 源码精读

最简的 unary——整个生成器核心只有一条语句：

[autofuse/codegen/api_call/elewise/unary_api_call.cpp:L28-L42](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/api_call/elewise/unary_api_call.cpp#L28-L42) —— `UnaryApiCall::Generate` 打印 `api_name_(y[向量偏移], x[向量偏移], x.actual_size);`。偏移由 `tpipe.tiler.TensorVectorizedOffset(current_axis, tensor)` 按当前循环轴计算（动态 shape 下是符号表达式），元素个数用 `x.actual_size`。

binary 的标量三分支：

[autofuse/codegen/api_call/elewise/binary_api_call.cpp:L36-L48](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/api_call/elewise/binary_api_call.cpp#L36-L48) —— 标量换位：注释写明「input[0] 为 Data、input[1] 为 Scalar 时 scheduler 无法调换顺序，需要 codegen 调换」，因为设备端标量版 API 约定标量在后。

[autofuse/codegen/api_call/elewise/binary_api_call.cpp:L70-L101](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/api_call/elewise/binary_api_call.cpp#L70-L101) —— 三分支：双标量走 `api_name(...)`（注意函数名带 s 后缀的是标量版 AscendC API）；单标量时 `Div`/`Sub` 因设备端实现需要中间临时缓冲，额外传 `tpipe.tmp_buf_<id>`（id 查自 `tmp_buf_id`，即 u5-l2 reg_func 埋下的 `TmpBufDesc` 契约）；皆非标量走普通向量化调用。

binary 的广播内联分支：

[autofuse/codegen/api_call/elewise/binary_api_call.cpp:L111-L177](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/api_call/elewise/binary_api_call.cpp#L111-L177) —— `BrcInlineGenerate`：当输入需要广播（brc）时，不先做一次 broadcast 搬运，而是生成 `BinaryBrcInlineApiWithTwoVectorizedAxis<dtype>(...)` 调用，把广播语义折叠进计算 API，并传入 `&AscendC::api_name` 函数指针让设备端包装器回调真正的计算函数。是否走该路径由 `Init` 中的 `IsGeneralizeBrcInlineScene` 判定（L179-L183）。

where 的五分支主体：

[autofuse/codegen/api_call/elewise/where_api_call.cpp:L278-L335](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/api_call/elewise/where_api_call.cpp#L278-L335) —— `WhereApiCall::Generate` 主入口：先 `PrepareInputsAndOutputs` 绑定 x1/x2/x3/y 四个张量引用，`GetTempBufferId` 取临时缓冲，`GenerateLoopParams`（内部复用 4.3 的 `GenerateVectorizedAxisMergeStatus`）算出循环参数，随后按 `outer_repeats` 空/双标量/单标量/普通五路分派。注意 L302-L309 会断言 x2 与 x3 的 dtype 名一致。

[autofuse/codegen/api_call/elewise/where_api_call.cpp:L72-L96](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/api_call/elewise/where_api_call.cpp#L72-L96) —— 无循环场景 `GenerateNoLoopCase`。**本次更新的落点之一**：L94 的元素个数从旧版的 `x1.actual_size` 改为 `GetCVAlignedSize(this->api_call_context, y, x1.actual_size.Str())`——CV 融合场景下按输出 dtype 做 32 字节块对齐（详见 4.3）。

**本次 diff 在各生成器中的共同模式**（where/compare/logical_not 等）：

- **dtype 感知**：旧代码把块单位硬编码为 `ONE_BLK_SIZE / sizeof(float)`（即假设 fp32），新代码先 `Tensor::DtypeName(y.dtype, dtype_name)` 再打印 `ONE_BLK_SIZE / sizeof(dtype_name)`，使 half/bf16 等 dtype 下块单位随实际类型变化（见 where_api_call.cpp L105-L120 等各分支）。
- **CV 对齐**：所有 `actual_size` / `ActualSize(param.cal_count)` 的打印点统一包上 `GetCVAlignedSize(...)` 或 `IsCVFusionStage(...) ? GenBlockAlignNExpr(...) : 原值`，非 CV 场景行为不变（见 [logical_not_api_call.cpp:L47-L50](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/api_call/elewise/logical_not_api_call.cpp#L47-L50) 与 [compare_api_call.cpp:L63-L93](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/api_call/elewise/compare_api_call.cpp#L63-L93)）。
- **头文件瘦身**：where/compare 等生成器删除了一批不再直接使用的 include 与 using，公共能力收敛到 `api_call_utils.h`。

#### 4.2.4 代码实践

1. **实践目标**：对比 unary 与 binary 两个生成器，量化「标量与广播支持」带来的复杂度差异，并验证 where 的生成行为。
2. **操作步骤**：
   - 对照阅读 [unary_api_call.cpp:L28-L42](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/api_call/elewise/unary_api_call.cpp#L28-L42) 与 [binary_api_call.cpp:L29-L104](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/api_call/elewise/binary_api_call.cpp#L29-L104)，列出后者多处理的场景清单；
   - 阅读 [test_codegen_where_api_call.cpp:L29-L167](https://github.com/gitcode.com/cann-graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/tests/ut/codegen/api_call/test_codegen_where_api_call.cpp#L29-L167)：测试用 `af::AscGraph` 手工搭 3 个 Load + 1 个 Where，调 `call.Init(where)` 与 `call.Generate(tpipe, current_axis, result)`，断言 `result` 精确等于期望字符串（如 `Where(local_3[0], local_0[0], local_1, local_2, local_0_actual_size, tmp_buf_0);\n`）；
   - 若本地可编译，运行：`sh build.sh --module=autofuse_framework --impl=cpp --ut -j 8`（UT 二进制为 `test_codegen`，可用 `--gtest_filter=WhereApiCallTest.*` 过滤）。
3. **需要观察的现象**：断言中的期望语句不含 `GetCVAlignedSize` 的对齐括号——因为 UT 场景 `api_call_context` 默认为 `kDefault`（非 CV），`GetCVAlignedSize` 原样返回表达式，这正好验证了「非 CV 场景行为不变」的门禁设计。
4. **预期结果**：能列出的差异至少包括：标量换位、双/单/无标量三分支、Div/Sub 的 tmp_buf 依赖、brc-inline 分支、以及 dtype 名获取。UT 全绿（本地无法运行时标注：待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`BinaryApiCall` 里 `Div`/`Sub` 与 `DivExtend`/`SubExtend` 在单标量分支的差别是什么？
**答案**：前者额外传 `tpipe.tmp_buf_<id>`（设备端标量版实现需要中间临时缓冲，id 来自 reg_func 阶段的 `TmpBufDesc`），后者不需要（Extend 版设备端实现自带处理）。

**练习 2**：`WhereApiCall::Generate` 为什么在 L302-L309 断言 x2、x3 的 dtype 名一致？
**答案**：Where 的语义是从 x2/x3 中按条件选值，输出 dtype 与两个候选分支必须一致；若不一致说明上游图构造出错，尽早 GE_ASSERT 失败好过生成错误代码在设备端出错。

**练习 3**：brc-inline 分支为什么传 `&AscendC::api_name` 函数指针两次？
**答案**：`BinaryBrcInlineApiWithTwoVectorizedAxis` 是通用广播包装器，需要在运行期回调真正的计算函数；两个输入各可能处于广播侧，包装器按 `input_idx_2_brc_inline` 标志对两侧分别使用传入的函数指针（当前两侧传同一计算函数）。

### 4.3 api_call_utils 公共工具：循环参数与 CV 对齐

#### 4.3.1 概念说明

多个生成器都要解决同样的子问题：如何把多个输入/输出的轴信息合并成一组统一的循环参数（外层 repeats、内层 strides、元素个数）？如何生成 DMA 搬运参数？这些公共逻辑收敛在 `api_call_utils`。本次更新又在这里新增了一组 **CV 融合对齐工具**，成为 where/compare/logical_not 等生成器的统一入口。

#### 4.3.2 核心流程

**向量轴合并**（where/binary/compare 共用）：

```
GenerateVectorizedAxisMergeStatus(inputs, outputs, merge_info, tpipe)
  # 检查各输入输出在向量轴上是否连续（CheckAxisContinuous）
  # 连续则合并出统一的 merge_repeats / strides → SaveApiLoopAxisParams → ApiLoopParams
```

**CV 对齐表达式**（本次新增）：

```
GetCVAlignedSize(context, tensor, size_expr)
  ├─ IsCVFusionStage(context)?  # context.stage != kDefault，即处于 CV 融合两阶段之一
  │    └─ GenBlockAlignNExpr(tensor, size_expr)
  │         ├─ align = 32 / dtype_size       # 一个 32 字节块能容纳的元素数
  │         └─ 返回 "((expr + align - 1) / align * align)"   # 向上取整到块边界
  └─ 否则原样返回 size_expr
```

即生成语句中的 N 被改写为 \[ \lceil n / a \rceil \cdot a \]，其中 \( a = 32 / \text{dtype\_size} \)。

#### 4.3.3 源码精读

公共数据结构与函数声明：

[autofuse/codegen/api_call/utils/api_call_utils.h:L19-L101](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/api_call/utils/api_call_utils.h#L19-L101) —— `DataCopyParams`（repeats/gm_strides/ub_strides，服务 load/store）、`DmaParams` 及其表达式版 `DmaParamsExpr`（block_count/block_len/stride/offset 五元组）、`ApiLoopParams`（外层 repeats + 各输入输出 strides + cal_count + 倒数第二轴 stride）、`VectorizedAxisLoopMergeStatus`（合并中间态）。

[autofuse/codegen/api_call/utils/api_call_utils.h:L118-L128](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/api_call/utils/api_call_utils.h#L118-L128) —— 函数声明区，**L125-L128 是本次新增的四个接口**：`IsCVFusionStage`、`GetTensorDtypeSize`、`GenBlockAlignNExpr`、`GetCVAlignedSize`。

新接口实现：

[autofuse/codegen/api_call/utils/api_call_utils.cpp:L577-L602](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/api_call/utils/api_call_utils.cpp#L577-L602) —— 四个函数的实现：`IsCVFusionStage` 只判断 `context.stage != kDefault`；`GetTensorDtypeSize` 用 `GetSizeByDataType` 取字节数并做合法性检查（含 DT_INT4 这类位数编码值 >= `kDataTypeSizeBitOffset` 的拒绝）；`GenBlockAlignNExpr` 生成向上对齐表达式（32 / dtype_size 为对齐粒度，dtype 未知时防御性原样返回）；`GetCVAlignedSize` 是门禁组合。

上下文从哪来：

[autofuse/codegen/codegen_kernel_loop.h:L56-L74](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_kernel_loop.h#L56-L74) —— `ApiScene`（kDefault / kCVFuseUBLoad：load 输入是 Cube 留在 UB 的输出）与 `ComputeStage`（kDefault / kCVFuseStage1：Cube 输出生命周期之内 / kCVFuseStage2：之外）两个枚举合成 `ApiCallContext`。

[autofuse/codegen/codegen_kernel_loop.cpp:L441-L449](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_kernel_loop.cpp#L441-L449) —— 上下文的填充点：Load 节点输入 tensor id 等于 `cube_output_tensor_id` 时置 `scene = kCVFuseUBLoad`；再按节点拓扑 id 与 Cube 输出生命周期边界的大小关系分到 Stage1/Stage2。这与 u8-l2 讲过的「kernel 按 Stage1/Stage2 两遍 Generate」相呼应（[codegen_kernel.cpp:L4290](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_kernel.cpp#L4290) 与 L4313 两处调用）。

**为什么必须对齐**：CV 融合时 Cube 的输出留在 UB，其 N 方向物理布局按 dtype 做了 32 字节块对齐（u8-l2 的 `BlkAlign`）；Vector 侧消费它的 API 若按未对齐的 `actual_size` 读，跨越块边界的访问会错位。`GenBlockAlignNExpr` 把 N 向上取整到块边界，保证与 Cube 侧布局一致——且对齐粒度随 dtype 变化（fp32 对齐 8 个元素，fp16 对齐 16 个），这正是「dtype 感知」的含义。

#### 4.3.4 代码实践

1. **实践目标**：手推 `GenBlockAlignNExpr` 在不同 dtype 下的输出表达式。
2. **操作步骤**：
   - 阅读 [api_call_utils.cpp:L590-L598](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/api_call/utils/api_call_utils.cpp#L590-L598)；
   - 对 fp32（size=4）与 fp16（size=2）分别手写输入 `n = local_0_actual_size` 时的返回字符串。
3. **需要观察的现象**：对齐粒度分别是 32/4=8 与 32/2=16。
4. **预期结果**：fp32 返回 `((local_0_actual_size + 8 - 1) / 8 * 8)`；fp16 返回 `((local_0_actual_size + 16 - 1) / 16 * 16)`（逐字符与源码模板核对）。
5. 纯源码阅读型实践，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `GetCVAlignedSize` 不直接判断 `isCVFusion()`（scene 维度），而用 stage 维度的 `IsCVFusionStage`？
**答案**：scene 只标记「load 吃的是 Cube 的 UB 输出」这一类节点；而对齐需求覆盖 CV 融合 kernel 中 Stage1 与 Stage2 两个阶段的全部 Vector 计算（它们都以 Cube 布局为基础），stage != kDefault 恰好刻画「当前处于 CV 融合编译流程中」。用 scene 判断会漏掉不直接吃 Cube 输出、但同处融合 kernel 的算子。

**练习 2**：`GenBlockAlignNExpr` 在 `GetTensorDtypeSize` 失败时为什么返回原表达式而不是报错？
**答案**：防御性设计——对齐是 CV 场景的性能/正确性增强，工具函数无法确定 dtype 时退回原始语义（不对齐），让调用链继续走；真正的 dtype 错误会在生成器里 `Tensor::DtypeName` 的 `GE_CHK_STATUS_RET` 处显式失败。

### 4.4 ascendc_api_registry：设备端函数定义的注册与查询

#### 4.4.1 概念说明

api_call 生成的是「调用语句」，但生成的 kernel 源码要能独立编译，还必须内嵌被调函数的**定义**。`AscendCApiRegistry` 就是「API 头文件名 → 源码内容」的注册表：启动期把 u5-l3 讲过的 `*_str.h` 原始字符串头登记进单例，生成期按图中算子实际用到的头文件按需取出、去重后拼进 kernel 源码。这样不用把整个 ascendc/api 全部塞进每个 kernel。

#### 4.4.2 核心流程

```
进程启动
  └─ ascendc_api_registry.cpp 匿名命名空间 Register 全局对象构造
       └─ 把 41 个 *_str.h 包装成 unordered_map<头文件名, 内容>
       └─ AscendCApiRegistry::GetInstance().RegisterApi(map)
Kernel::GenerateKernelByNode 遍历节点
  └─ impl->LoadApiHeaderFiles(is_dynamic)        # ASCIR 实现声明需要的头（如 "scalar_div.h"）
       └─ GetFileContent(header_str)              # 查注册表
       └─ kernel_file_ptr 集合去重后 ss << file   # 拼进 kernel 源码
```

#### 4.4.3 源码精读

[autofuse/codegen/ascendc_api_registry.h:L19-L31](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/ascendc_api_registry.h#L19-L31) —— 单例接口只有两个：`GetFileContent(api_name)`（查不到返回空串引用）与 `RegisterApi(map)`。极小的门面。

[autofuse/codegen/ascendc_api_registry.cpp:L20-L45](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/ascendc_api_registry.cpp#L20-L45) —— 注册的构造方式：每个 `*_str.h`（如 `where_str.h`、`compare_v2_str.h`）被 `#include` 进一个 `const std::string` 的花括号初始化器——原始字符串字面量直接成为字符串内容，这是 u5-l3 讲过的 sed 封装产物。

[autofuse/codegen/ascendc_api_registry.cpp:L200-L221](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/ascendc_api_registry.cpp#L200-L221) —— `api_to_file` map 的收尾与 `RegisterApi` 调用：键是头文件名（`"where.h"`、`"scalar_div.h"`……），正是 4.1.3 中 `LoadApiHeaderFiles` 返回的同名字符串——**两端靠头文件名字符串对接**。全局对象 `api_register`（L221）保证 main 之前完成注册。

[autofuse/codegen/ascendc_api_registry.cpp:L224-L239](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/ascendc_api_registry.cpp#L224-L239) —— `GetInstance`（Meyers 单例）与 `GetFileContent` / `RegisterApi` 实现。

[autofuse/codegen/codegen_kernel.cpp:L3690-L3698](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_kernel.cpp#L3690-L3698) —— 消费点：`GenerateKernelByNode` 对每个节点取 `impl->LoadApiHeaderFiles(is_dynamic)`，逐个 `GetFileContent`，空内容或已拼过（`kernel_file_ptr` 指针去重）则跳过，否则追加进 kernel 源码流。v35 平台外还包了一层 `__DAV_C310__/__NPU_ARCH__` 宏保护。

#### 4.4.4 代码实践

1. **实践目标**：走通「Where 算子 → WhereApiCall → where.h 定义」三段链路的最后一环。
2. **操作步骤**：
   - 在 [v1_ascir_codegen_impl.h:L1497-L1530](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/ascir/generator/v1_ascir_codegen_impl.h#L1497-L1530) 附近找到 `WhereAscIrCodegenImpl::LoadApiHeaderFiles` 的返回值（应为 `"where.h"`）；
   - 在 [ascendc_api_registry.cpp:L195-L218](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/ascendc_api_registry.cpp#L195-L218) 确认 `{"where.h", kAscendcWhereStr}` 已登记；
   - 执行 `ls autofuse/ascendc/api/ | grep -i where` 与 `grep -rn "where_str" autofuse/ --include=CMakeLists.txt`，找到源头 `where.h` 与生成 `where_str.h` 的构建规则。
3. **需要观察的现象**：三处出现的是同一个字符串 `"where.h"`。
4. **预期结果**：确认「ASCIR 声明需求 → registry 供货 → kernel 源码内嵌定义」闭环成立。
5. 纯源码阅读型实践，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：registry 的键为什么用头文件名（如 `"where.h"`）而不是 API 函数名？
**答案**：一个头文件通常含一族相关定义（函数 + 辅助类型），按头文件为粒度既与 ASCIR 侧 `LoadApiHeaderFiles` 的声明方式天然对齐，又能一次嵌入整组依赖，避免函数级的依赖细拆。

**练习 2**：`GetFileContent` 查不到时返回 `kEmpty` 空串而不是抛错，消费端会发生什么？
**答案**：`GenerateKernelByNode` 检查 `file.empty()` 后 continue 跳过（codegen_kernel.cpp L3695）；若该定义真的被调用语句引用，后续独立编译 kernel 时会报未定义符号——错误后移但不会被吞掉。

## 5. 综合实践

**任务：为「假设的新算子 BitwiseXor 接入 CV 融合」写出完整修改清单并验证理解。**

背景：`BitwiseXor` 是双输入逐元素算子，设备端封装已存在于 `autofuse/ascendc/api/bitwise.h`（含对应的 `*_str.h`）。要求它支持 CV 融合场景。

步骤：

1. **注册链路**：对照 u5-l1 的 `REG_ASC_IR` 与 4.1.3 的映射机制，写出 codegen 实现中 `GetApiCallName()`（提示：返回 `"BinaryApiCall"` 即可复用）、`GetApiName()`、`LoadApiHeaderFiles()` 三个返回值。
2. **复用判断**：`BinaryApiCall` 的单标量分支（binary_api_call.cpp L70-L96）没有为 `BitwiseXor` 出现的 tmp_buf 特判——判断它落在哪个 else 分支，生成语句会是什么形态。
3. **CV 对齐检查**：grep 检查 `binary_api_call.cpp` 中 `actual_size` 的打印点是否已包 `GetCVAlignedSize` / `GenBlockAlignNExpr`；若某处未包，说明该路径在 CV 融合 kernel 中的潜在风险。
4. **验证**：参照 `autofuse/tests/ut/codegen/api_call/test_codegen_binary_api_call.cpp` 的结构，列出你将新增的 gtest 用例清单（至少覆盖：皆非标量、单标量、CV stage 下 N 对齐三种）。

预期产出：一份包含「ASCIR 注册三返回值 + 复用分支判断 + 对齐覆盖检查结论 + 用例清单」的笔记。如需实际运行 UT：`sh build.sh --module=autofuse_framework --impl=cpp --ut -j 8`（注意 `-j 8` 防 OOM，见 u1-l3）。上板验证需按 u1-l4 安装 .run 包并配置环境，标注：待本地验证。

## 6. 本讲小结

- **三段接力**：ASCIR 算子类型 → `AscIrCodegenImpl`（给出 `GetApiCallName`/`GetApiName`/`LoadApiHeaderFiles`）→ `ApiCallFactory` 造出生成器对象 → `Generate` 打印 AscendC 调用语句；一个生成器类（如 `BinaryApiCall`）服务一族 API。
- **自注册模式**：每个生成器 .cpp 末尾的 `static ApiCallRegister<T>` 全局对象在 main 之前把 creator 登记进工厂单例，与 ASCIR 注册同套路。
- **elewise 复杂度阶梯**：unary 一行语句；binary 增加标量换位、三分支、Div/Sub 的 tmp_buf、brc-inline；where 达五分支并复用 `GenerateVectorizedAxisMergeStatus` 合并循环轴。
- **本次更新的主题是 dtype 感知 CV 对齐**：`api_call_utils` 新增 `GetCVAlignedSize` 等四个接口，按 `ApiCallContext.stage` 门禁，在 CV 融合场景把 N 向上取整到 `32/dtype_size` 个元素的块边界，非 CV 场景原样返回、行为不变。
- **registry 闭环**：设备端函数定义经 `*_str.h` 原始字符串注册进 `AscendCApiRegistry`，kernel 生成期按 `LoadApiHeaderFiles` 声明按需取出、指针去重后内嵌，两端靠头文件名字符串对接。
- **UT 是行为的精确快照**：`test_codegen_where_api_call.cpp` 直接断言生成的语句字符串，默认上下文下不含对齐括号，恰可验证门禁正确性。

## 7. 下一步学习建议

- 下一讲（u9-l1）转向 compiler 对外接口：`pyautofuse.cpp` 的 pybind 绑定与 `compile_adapter.py` 的 host/device 编译编排，看本讲生成的 kernel 源码如何被送进编译管线。
- 建议继续精读：`autofuse/codegen/api_call/datacopy/load_api_call.cpp`（DMA 参数生成的最完整样本，大量使用 4.3 的 `CalculateDmaParams`/`CreateDmaCall`），以及 `autofuse/codegen/codegen_kernel_loop.cpp` 的 Loop 生成（`ApiCall` 语句如何嵌进循环骨架）。
- 若对 v35 平台的 regbase 类算子（如 IndirectLoad 的 SIMD/SIMT）感兴趣，可预习 u11-l4 的 `reg_indirect_load_api_call.cpp`，它展示了 api_call 模式在寄存器基址寻址算子上的扩展。
