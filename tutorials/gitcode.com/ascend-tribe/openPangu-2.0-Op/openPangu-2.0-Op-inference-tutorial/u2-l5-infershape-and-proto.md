# u2-l5 InferShape 与算子 proto：输出形状如何推导

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 InferShape 函数是什么、给谁用：它是 op_host 层一个**可选**的推导实现，注册后供图引擎在构图阶段推导输出 shape 与 dtype，也可被 UT 框架在纯 CPU 上直调验证。
2. 掌握 `IMPL_OP_INFERSHAPE(OpClassName).InferShape(...).InferDataType(...)` 这一套注册与实现套路，并能区分 `*_infershape.cpp` 与 `*_proto.cpp` 两种命名的异同。
3. 看懂 matmul 公共库里更复杂的推导逻辑：广播、转置索引、未知维度（-1/-2）与 InferShapeRange（min/max 范围交集）。
4. 知道 InferShape 的单测文件长什么样、放在哪里、如何被 UT 框架加载执行，并能为自己的算子写 shape 用例。

一个先行的纠偏：本讲规划中曾提到 `Infershape4To5D` 这样的函数名——**仓库中不存在该函数**（全仓库 grep `4To5D` 无匹配）。仓库的真实套路是：每个算子写一个自定义命名的静态函数（如 `InferShapeLowerTriangularInverse`），再用宏注册。学本讲时请以真实函数名为准。

## 2. 前置知识

### 2.1 为什么需要「推导输出形状」

一个算子拿到输入张量后，输出张量的形状（shape）和数据类型（dtype）从哪来？两条路：

- **aclnn 直调路径**（第 2 单元前几讲的主角）：调用方在 op_api 层自己创建输出 tensor（`aclCreateTensor` 时就写明了 shape），所以输出形状由调用方给出，不需要推导函数。这就是为什么 `ai_infra_scatter_block_update` 这类只走 aclnn 的算子目录里**没有** infershape 文件。
- **图引擎路径**（GE / torchair 图模式）：构图时框架手里只有一张算子节点连成的图，下游节点的输入 shape 依赖上游节点的输出 shape。上游输出还没真正跑出来，框架必须**静态推算**出输出 shape，才能继续推导下游、分配内存。这个静态推算函数就是 InferShape。

本仓库 18 个算子目录中有 11 个注册了 `IMPL_OP_INFERSHAPE`（多为需要被图模式捕获的大算子），其余只走 aclnn 直调的算子可以不写。

### 2.2 几个必备概念

| 概念 | 含义 |
|---|---|
| shape | 张量每一维的长度，如 `{1, 1, 1, 64, 64}` 是一个 5 维张量 |
| dtype | 数据类型，如 `ge::DT_FLOAT`、`ge::DT_INT32`（与 def 文件里 `DataType({ge::DT_FLOAT})` 对应） |
| 未知维度 | 动态 shape 场景：`-1` 表示该维长度未知（`UNKNOWN_DIM`），`-2` 表示连维数都未知（`UNKNOWN_DIM_NUM`） |
| InferShapeRange | 不推一个确定 shape，而是推每维的 `[min, max]` 范围，多输入取交集 |
| OpDef 类名 | def 文件里 `class AiInfraLowerTriangularInverse : public OpDef` 的类名，它同时也是注册表的键，InferShape 注册时靠它对上号 |

### 2.3 与前面几讲的衔接

- u2-l1 讲过 def 文件：`OP_ADD(AiInfraLowerTriangularInverse)` 把算子**签名**注册进全局注册表。本讲的 InferShape 注册是**第二张表**——按同一个类名，把「shape/dtype 推导实现」注册进 op impl 空间注册表。
- u1-l2 讲过三个动态库：def 编入 `op_host_aclnnInner`（原型），而本讲的 infershape/proto 文件编入 `opsproto` 目标（即 `cust_opsproto_rt2.0.so` 的一部分）。
- u2-l3 的 Tiling 是「算出怎么切数据」；本讲的 InferShape 是「算出输出长什么样」。两者都在 host 侧、都不碰真实数据，只看形状元信息。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_host/ai_infra_lower_triangular_inverse_infershape.cpp` | 最简 infershape 标本：输出=输入的逐维拷贝 |
| `ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_host/chunk_gated_delta_rule_recurrence_proto.cpp` | proto 风格标本：多输入多输出、统一错误宏 |
| `ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_host/ai_infra_chunk_gated_delta_rule_recurrence_def.cpp` | 对照用 def：看输出声明与 proto 的索引常量如何对应 |
| `ascendc/src/ops-nn/matmul/common/op_host/matmul_common_infershape.cpp` | 公共推导库：广播、转置、bias、未知维度、InferShapeRange |
| `ascendc/src/ops-nn/matmul/ai_infra_matmul/op_host/ai_infra_matmul_infershape.cpp` | 公共库的消费侧：`IMPL_OP_INFERSHAPE(AiInfraMatmul)` 注册点 |
| `ascendc/src/tests/ut/framework_normal/common/infer_shape_context_faker.h` | UT 用例参数类 `InfershapeContextPara` 与上下文伪造器 |
| `ascendc/src/tests/ut/framework_normal/common/infer_shape_case_executor.cpp` | UT 执行器：查注册表、直调 infer_shape、断言结果 |
| `ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/tests/ut/op_host/test_lower_triangular_inverse_infershape.cpp` | 单输入单输出的 shape 测试范本 |
| `ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/tests/ut/op_host/test_chunk_gated_delta_rule_recurrence_infershape.cpp` | 多输入多输出的 shape 测试范本 |
| `ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/CMakeLists.txt` | 看 infershape 文件挂到 `opsproto` 目标 |
| `ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/tests/ut/op_host/CMakeLists.txt` | 看 shape 测试如何挂进 UT 构建 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**InferShape 函数**（4.1、4.3）、**proto 定义**（4.2）、**shape 测试**（4.4）。

### 4.1 InferShape 函数：最小实现套路

#### 4.1.1 概念说明

InferShape 函数是 host 侧一个纯 C++ 静态函数，签名固定为：

```cpp
ge::graphStatus InferShapeXxx(gert::InferShapeContext *context)
```

它通过 `context` 拿到所有输入的 shape（以及 attrs、输入 dtype 描述），把推导结果**写进** `context` 的输出 shape 槽位，返回 `GRAPH_SUCCESS` 或 `GRAPH_FAILED`。配套的还有 `InferDataType` 函数，负责输出 dtype。

注册宏 `IMPL_OP_INFERSHAPE(OpClassName)` 来自 CANN 头文件 `register/op_impl_registry.h`（不在本仓库内），它把这两个函数按 OpDef 类名登记进 op impl 空间注册表——UT 执行器里可以看到这张表的真实入口 `DefaultOpImplSpaceRegistryV2`（见 4.4.3）。

#### 4.1.2 核心流程

一次 InferShape 调用的通用骨架：

```text
1. 取输入 shape：GetInputShape / GetRequiredInputShape / GetDynamicInputShape / GetOptionalInputShape
2. 判空：shape 指针为空直接 GRAPH_FAILED（防御式，UT 与图引擎都可能给空）
3. （可选）校验：维数、特殊维是否符合算子约定，不符则报错返回
4. 推导：由输入 shape 计算输出 shape（逐维 SetDim）
5. 写回：outShape->SetDimNum(n) + outShape->SetDim(i, ...)，或直接 *outShape = *inShape
6. 返回 GRAPH_SUCCESS
```

配套 InferDataType：`context->SetOutputDataType(i, dtype)`，dtype 可以是写死的（如固定输出 FP32），也可以从输入 dtype 继承。

#### 4.1.3 源码精读

**入口与注册**：[ai_infra_lower_triangular_inverse_infershape.cpp:60-62](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_host/ai_infra_lower_triangular_inverse_infershape.cpp#L60-L62) 是整个文件的点睛之笔——`IMPL_OP_INFERSHAPE(AiInfraLowerTriangularInverse)` 的参数正是 def 文件里的 OpDef 类名，`.InferShape(...)` 与 `.InferDataType(...)` 链式挂上两个实现函数。**def 声明签名、infershape 提供推导，靠类名关联，这是三层结构里 def 与 proto 的分工边界。**

**InferShape 主体**：[ai_infra_lower_triangular_inverse_infershape.cpp:35-52](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_host/ai_infra_lower_triangular_inverse_infershape.cpp#L35-L52) 做的事：第 38 行用 `GetDynamicInputShape(X_INDEX, 0)` 取第 0 个输入第 0 个实例的 shape（注意它选的是「动态输入」接口，尽管该算子 def 里 x 是 REQUIRED——faker 与真实框架都兼容这种取法）；第 44-48 行先 `SetDimNum(DIM_LEN)` 定维数，再循环 `SetDim(i, xShape->GetDim(i))` 逐维拷贝——下三角矩阵求逆，输出形状等于输入形状，这是「同形算子」最典型的推导写法。

一个值得留意的细节：第 [40-42 行](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_host/ai_infra_lower_triangular_inverse_infershape.cpp#L40-L42) 检查输入维数是否为 5，但 if 块里**只有 `OP_LOGE` 打日志、没有 return**——即便维数不是 5，函数仍会继续把前 5 维拷给输出并返回成功。对比 4.2 中 chunk 算子的严格判错风格，可以看到仓库内新旧代码的严谨度并不一致，读源码时要留意这种「检查了但没拦住」的写法。

**InferDataType**：[ai_infra_lower_triangular_inverse_infershape.cpp:54-58](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/op_host/ai_infra_lower_triangular_inverse_infershape.cpp#L54-L58) 把输出 0 的 dtype 写死为 `DataType::DT_FLOAT`——因为该算子 def 里只支持 FP32，所以推导不需要看输入。

**取 shape 的四个 API**（本讲三个标本正好覆盖了不同接口）：

| API | 语义 | 使用处 |
|---|---|---|
| `GetInputShape(i)` | 取第 i 个输入的 shape | matmul |
| `GetRequiredInputShape(i)` | 取必选输入的 shape | chunk proto |
| `GetDynamicInputShape(i, n)` | 动态输入：第 i 个输入的第 n 个实例 | lower_triangular |
| `GetOptionalInputShape(i)` | 取可选输入，无则为 nullptr | matmul 的 bias |

#### 4.1.4 代码实践：源码阅读型

1. **实践目标**：确认「输出=输入」这类同形推导在仓库里的分布，并理解注册表查找键。
2. **操作步骤**：
   - 在 `ascendc/src` 下 grep `IMPL_OP_INFERSHAPE(`，记录全部 11 个注册点及所属算子；
   - 对每个算子打开文件，只看两件事：InferShape 里输出 shape 来自哪个输入；InferDataType 是写死还是继承输入；
   - 对照 u1-l1 的 18 个算子目录清单，找出**没有** infershape 的算子（如 scatter_block_update）。
3. **需要观察的现象**：有 infershape 的算子多为多输出、需图模式捕获的大算子；无 infershape 的算子输出 shape 完全由 aclnn 调用方给出。
4. **预期结果**：得到一张「算子 → 是否有 infershape → 输出 shape 规则」三列清单。grep 与读文件不依赖硬件，可直接完成。

#### 4.1.5 小练习与答案

**练习 1**：`InferShapeLowerTriangularInverse` 里 `outShape->SetDimNum(DIM_LEN)` 如果漏写，只循环 `SetDim`，会发生什么？

**答案**：输出 shape 槽位的维数没有被显式设置。`SetDim(i, ...)` 在维数不足时的行为依赖 gert::Shape 内部实现（通常不会自动扩容），轻则断言失败，重则读到未初始化的维度值。先 `SetDimNum` 再 `SetDim` 是安全顺序；更省事的写法是像 chunk proto 那样整体赋值 `*outShape = *inShape`（见 4.2.3）。

**练习 2**：为什么 `InferDataType4LowerTriangularInverse` 可以写死 `DT_FLOAT`，而 chunk 算子的 InferDataType 要从输入取？

**答案**：lower_triangular 的 def 只声明了 `DataType({ge::DT_FLOAT})` 一种组合，输出类型不可能变；chunk 算子虽然当前 def 也只有 FP32，但它的输出 dtype 语义上跟随输入（初始状态/中间结果与 value 同型），从输入继承（`GetRequiredInputDataType` → `SetOutputDataType`）能让将来扩展 dtype 时不用改推导代码。

### 4.2 proto 定义：`*_proto.cpp` 与 `*_def.cpp` 的关系

#### 4.2.1 概念说明

仓库里承载 InferShape 的文件有两种命名：

- `*_infershape.cpp`：如 lower_triangular_inverse、fused_infer_attention_sink、ai_infra_matmul 等 7 个算子；
- `*_proto.cpp`：如 chunk_gated_delta_rule_recurrence、sparse_flash_attention_gqa、esa_select_topk、quant_lightning_indexer 等 4 个算子。

「proto」是图算子**原型**（prototype）的旧称——早期 CANN 图算子的 shape/dtype 推导叫算子原型实现，后来逐渐改叫 infershape。两种文件**本质完全相同**：都实现 `IMPL_OP_INFERSHAPE`、都编入 `opsproto` 构建目标、都被同一张注册表管理。看到 `*_proto.cpp` 把它当 infershape 文件读即可。

它与 def 的分工：

| 文件 | 注册宏 | 编入目标 | 职责 |
|---|---|---|---|
| `*_def.cpp` | `OP_ADD` | `op_host_aclnnInner` 等 | 声明签名：几个输入输出、类型格式组合、SOC 配置 |
| `*_infershape.cpp` / `*_proto.cpp` | `IMPL_OP_INFERSHAPE` | `opsproto` | 实现推导：输出 shape 与 dtype 怎么算出来 |

#### 4.2.2 核心流程

```text
编译期：def 文件 → 原型库；proto/infershape 文件 → opsproto 库（u1-l2 的 cust_opsproto_rt2.0.so）
运行期注册：OP_ADD 把 OpDef 对象挂进 OpDef 注册表（键：类名字符串）
          IMPL_OP_INFERSHAPE 把推导函数挂进 OpImpl 空间注册表（键：同一个类名字符串）
运行期使用：图引擎/UT 拿算子类型名 → 查 OpImpl 注册表 → 取 infer_shape 函数指针 → 调用
```

#### 4.2.3 源码精读

**索引常量先行**：[chunk_gated_delta_rule_recurrence_proto.cpp:24-31](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_host/chunk_gated_delta_rule_recurrence_proto.cpp#L24-L31) 把输入输出下标定义成常量（`INITIAL_STATE_INPUT_INDEX = 0`、`ATTN_INTER_OUT_OUTPUT_INDEX = 1` 等）。这些下标必须与 def 文件里 `Input(...)`/`Output(...)` 的**声明顺序**一致——对照 [ai_infra_chunk_gated_delta_rule_recurrence_def.cpp:37-42](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_host/ai_infra_chunk_gated_delta_rule_recurrence_def.cpp#L37-L42)：三个输出依次是 `initial_state`（下标 0）、`attn_inter_out`（下标 1）、`v_new_out`（下标 2），与常量一一对应。def 与 proto 是靠「下标对齐」协作的，改动任何一边的顺序都会错位。

**多输出的形状赋值**：[chunk_gated_delta_rule_recurrence_proto.cpp:34-55](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_host/chunk_gated_delta_rule_recurrence_proto.cpp#L34-L55) 的 `SetShapeDim` 展示了比 lower_triangular 更规范的三段式：第 36-37 行取两个必选输入 shape；第 39-40、46-48 行用 `OPS_LOG_E_IF_NULL` 宏判空（打日志并返回 `GRAPH_FAILED`，比 4.1 里「只打日志不返回」严谨）；第 50-52 行用整体赋值 `*initialStateOutputShape = *initialStateInputShape;` 完成推导——语义是：输出的 initial_state 与输入的 initial_state 同形（递推前后的状态形状不变），attn_inter_out 与 v_new_out 都与 value 输入同形。

**统一错误处理宏**：[chunk_gated_delta_rule_recurrence_proto.cpp:57-68](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_host/chunk_gated_delta_rule_recurrence_proto.cpp#L57-L68) 入口函数把实际逻辑放进 `SetShapeDim`，用 `OPS_ERR_IF(条件, OPS_LOG_E(...), return ge::GRAPH_FAILED)` 宏包装「失败即日志+返回」，这是 u5-l3 将要系统讲的 ops_err 体系的典型用法。

**注册行**：[chunk_gated_delta_rule_recurrence_proto.cpp:85-87](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_host/chunk_gated_delta_rule_recurrence_proto.cpp#L85-L87) 与 lower_triangular 的注册行结构完全一致，只是函数不是 static（`InferShape_ChunkGatedDeltaRuleRecurrence`），命名风格从驼峰变成了下划线分隔——仓库内两种风格并存，阅读时认宏不认名。

**CMake 挂接对照**：lower_triangular 的 [CMakeLists.txt:39-41](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/CMakeLists.txt#L39-L41) 把 infershape 文件挂到 `opsproto` 目标，同文件第 [19-21 行](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/CMakeLists.txt#L19-L21) 把 def 挂到 `op_host_aclnnInner`；chunk 算子的 [op_host/CMakeLists.txt:39-41](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_host/CMakeLists.txt#L39-L41) 同样把 proto 挂 `opsproto`。**def 与 proto 的产物边界在这里看得最清楚。**

#### 4.2.4 代码实践：源码阅读型

1. **实践目标**：验证「def 下标顺序 = proto 索引常量」这条隐含契约。
2. **操作步骤**：
   - 打开 [ai_infra_chunk_gated_delta_rule_recurrence_def.cpp:23-36](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_host/ai_infra_chunk_gated_delta_rule_recurrence_def.cpp#L23-L36)，按 `Input(...)` 出现顺序给 7 个输入编号（0~6）；
   - 打开 proto 文件第 25-31 行，核对 `INITIAL_STATE_INPUT_INDEX = 0`、`VALUE_INPUT_INDEX = 2` 是否与 def 顺序吻合（def 第 3 个声明的正是 `value`）；
   - 再挑 `ai_infra_sparse_flash_attention_gqa_proto.cpp` 或 `ai_infra_esa_select_topk_proto.cpp` 做同样核对。
3. **需要观察的现象**：两边下标严格对齐；若有人交换 def 里两个输入的声明顺序而忘了改 proto，推导会静默读错输入。
4. **预期结果**：每个被核对算子得到一张「def 声明序号 → proto 常量名」对照表。纯静态阅读，无需硬件。

#### 4.2.5 小练习与答案

**练习 1**：如果把 chunk 算子 def 文件里 `initial_state`（输出）和 `attn_inter_out` 两个 `Output(...)` 声明对调，哪些地方必须同步修改？

**答案**：至少三处——proto 里的 `INITIAL_STATE_OUTPUT_INDEX`/`ATTN_INTER_OUT_OUTPUT_INDEX` 常量（或其赋值逻辑）、op_api 层创建输出 aclTensor 的顺序（aclnn 接口按 def 顺序输出）、kernel 入口参数顺序与 torch_ops_extension csrc 里解包多输出的顺序。这正是 u6-l3「新增算子 checklist」强调各层下标一致的原因。

**练习 2**：`*_proto.cpp` 与 `*_infershape.cpp` 在编译产物上有区别吗？

**答案**：没有。两者都通过 `target_sources(opsproto PRIVATE ...)` 编入 opsproto 目标（对应安装后的 `cust_opsproto_rt2.0.so`），注册宏也相同；区别只是文件命名习惯（proto 是旧称）与内部代码风格（错误宏、命名规范新旧不同）。

### 4.3 复杂推导：matmul 公共库、广播与 InferShapeRange

#### 4.3.1 概念说明

同形拷贝之外，真实的推导常常要做**广播**（broadcast）：两个输入维数不同、某些维为 1 时，输出取逐维「较大者」。广播规则（与 PyTorch 一致）：

\[ \dim^{out}_i = \begin{cases} \max(\dim^a_i,\ \dim^b_i) & \dim^a_i = \dim^b_i \text{ 或其中一方为 } 1 \\ \text{报错} & \text{两者都} > 1 \text{且不相等} \end{cases} \]

其中下标 \( i \) 从**最后一维**向左对齐，维数少的一方缺位视为 1。

更进一步，动态 shape 场景（def 里 `DynamicShapeSupportFlag(true)`，第 2 单元讲过）每维可能未知（-1）甚至维数未知（-2），此时推不出确定 shape，就要推 **InferShapeRange**：每维给一个 `[min, max]` 区间，多个输入的区间取交集来收窄。matmul 公共库 `matmul_common_infershape.cpp` 是这两套机制的完整教科书。

需要如实说明的一点：`InferShapeForBatchMatMul` / `InferShapeRangeForBatchMatMul` 这两个 common 库入口，grep 全仓库**没有任何调用点**——`ai_infra_matmul_infershape.cpp` 虽然 include 了该头文件（[ai_infra_matmul_infershape.cpp:17](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-nn/matmul/ai_infra_matmul/op_host/ai_infra_matmul_infershape.cpp#L17)），但注册的是自己的 [InferShapeForAiInfraMatmul（L210）](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-nn/matmul/ai_infra_matmul/op_host/ai_infra_matmul_infershape.cpp#L210)。batch 版入口是从 CANN 上游公共库继承的存量基建，读它的价值在于学套路（广播、range 交集、转置索引），而非追踪一条活跃调用链。同理，仓库内也**没有**算子通过 `.InferShapeRange(...)` 注册 range 推导——range 的「实现套路」在 common 库里完整可见，注册链在本仓库未出现，属 CANN 生态算子的惯用法。

#### 4.3.2 核心流程

batch matmul（\( A \cdot B \)，带转置与 bias）的推导流程：

```text
InferShapeForBatchMatMul:
1. 判空输入/输出 shape、attrs
2. 若任一输入维数未知(-2)：输出置为 {UNKNOWN_DIM_NUM}，直接成功（推无可推）
3. 读 adj_x1/adj_x2 转置属性 → 决定 m/k 在 A 中的下标、k/n 在 B 中的下标
4. 静态 shape 下校验 k 轴：A 的 k 维 == B 的 k 维，否则失败
5. InferBatch：对齐维数后，广播推 batch 维（1 可广播，-1 与 1 视为可对齐）
6. 输出倒数第 2 维 = m（来自 A），倒数第 1 维 = n（来自 B）
7. 有 bias 时 InferBias：bias 的最后一维参与 N 的确定与校验，batch 维同样广播
```

InferShapeRange 的推导流程：

```text
InferShapeRangeBatchMatMul:
1. Init：取 x1/x2/bias 的 min/max shape，转成 vector<pair<min,max>>
2. 维数对齐（前面补 {1,1}；-1 的无穷上界归一化为 int64 max）
3. batch 维逐维取交集（一方为 1 时按另一方广播）
4. m 取 x1 的 m 区间；k 两边取交集仅用于校验（不输出）
5. bias 参与 N 区间与 batch 区间的收窄
6. SetOutput：还原 -1 上界，写回 min/max shape
```

#### 4.3.3 源码精读

**广播 batch 维**：[matmul_common_infershape.cpp:113-139](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-nn/matmul/common/op_host/matmul_common_infershape.cpp#L113-L139) 的 `InferBatch`：先用 `valid_offset` 把较短输入的前缀对齐，再逐维比较——第 126 行「两者都 >1 且不相等则失败」正是广播的约束；第 129-136 行处理「一根轴为 1、另一根轴为 -1」的动态 shape 特例（1 与未知取另一方）。

**单维广播函数**：[matmul_common_infershape.cpp:141-163](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-nn/matmul/common/op_host/matmul_common_infershape.cpp#L141-L163) 的 `BroadcastBatchDim` 是上式的直接代码化：相等取值、一方为 1 取另一方、否则 `std::max` 兜底，配上 `CUBE_INNER_ERR_REPORT` 统一报错。

**m/k/n 与转置**：[matmul_common_infershape.cpp:254-283](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-nn/matmul/common/op_host/matmul_common_infershape.cpp#L254-L283)：非转置时 A 的 m 是倒数第 2 维、k 是最后一维；`trans_a` 为真则对调（第 260-264 行），B 侧同理。第 272-278 行在两边 k 都已知时校验相等；第 282-283 行写出 `out[m] = a[m]、out[n] = b[n]`——矩阵乘法的形状规则 \( (m \times k) \cdot (k \times n) = (m \times n) \) 落到代码就是这两行。

**未知维数的短路**：[matmul_common_infershape.cpp:399-403](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-nn/matmul/common/op_host/matmul_common_infershape.cpp#L399-L403) 在 `InferShapeForBatchMatMul` 入口处：任一输入是 `{-2}`（维数未知）时，输出直接置为 `{UNKNOWN_DIM_NUM}` 并成功返回——「推不动就诚实标记未知」，而不是硬算出一个错误 shape。

**range 的无穷界归一化**：[matmul_common_infershape.cpp:456-482](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-nn/matmul/common/op_host/matmul_common_infershape.cpp#L456-L482) 的 `InitializeRange`：把每维 `{a, -1}` 中的 `-1`（无穷）翻译成 `int64 max`，缺的 batch 维补 `{1, 1}`——先归一化成可比对的区间，后面才能统一取交集。

**区间交集**：[matmul_common_infershape.cpp:484-515](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-nn/matmul/common/op_host/matmul_common_infershape.cpp#L484-L515) 的 `GetBatchIntersection`：双方 min 都是 1 时按较大范围广播；否则取 `[max(min_a, min_b), min(max_a, max_b)]`，下界超过上界即「无交集」报错。\[ [l_a, h_a] \cap [l_b, h_b] = [\max(l_a, l_b),\ \min(h_a, h_b)] \] 这一行数学，就是第 505-506 行的两句代码。

**消费侧注册点**：[ai_infra_matmul_infershape.cpp:210](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-nn/matmul/ai_infra_matmul/op_host/ai_infra_matmul_infershape.cpp#L210) `IMPL_OP_INFERSHAPE(AiInfraMatmul).InferShape(InferShapeForAiInfraMatmul);`——注意它**只挂了 InferShape、没挂 InferDataType**（注释「no need to SetDataType in runtime」在第 [449 行](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-nn/matmul/common/op_host/matmul_common_infershape.cpp#L449)），说明 InferDataType 也是可选槽位。

#### 4.3.4 代码实践：手算验证型

1. **实践目标**：用真实代码的广播与转置规则，手算一组 batch matmul 的输出 shape。
2. **操作步骤**：
   - 设 A 形状 `(2, 1, 128, 64)`、`adj_x1=false`；B 形状 `(128, 32)`、`adj_x2=false`，无 bias；
   - 按 `InferShape` 流程走：`num_dim = max(4, 2) = 4`；`valid_offset = 4 - 2 = 2`；batch 维只有第 0、1 维（i < num_dim - 2），A 的第 0 维 2、B 缺位——写出输出；
   - 再把 `adj_x2=true` 重算一遍（此时 B 的 k 是最后一维 32？注意校验会失败），体会转置属性如何改变下标。
3. **需要观察的现象**：第一次应得到 `(2, 1, 128, 32)`；第二次 k 轴 `64 != 32` 应触发失败返回。
4. **预期结果**：两组手算结论与 `InferShapeForBatchMatMul` 的代码路径一致（待本地验证：可在有 UT 环境时把该用例写成 InferShapeRange/InferShape 的 faker 用例跑通）。

#### 4.3.5 小练习与答案

**练习 1**：`UNKNOWN_DIM(-1)` 与 `UNKNOWN_DIM_NUM(-2)` 的区别是什么？

**答案**：`-1` 表示「该维长度未知，但张量有几维是知道的」，如 `{-1, 128}` 表示二维、第二维 128；`-2` 表示「连几维都不知道」，在代码里通常表现为一维 shape `{-2}`。matmul 库对 `-2` 直接短路输出 `{-2}`（399-403 行），对 `-1` 则尽量与已知信息比对、广播收窄。

**练习 2**：为什么 InferShapeRange 里 k 轴「只校验、不输出」？

**答案**：矩阵乘法的 k 是收缩轴（被消耗的维度），不出现在输出里；但两个输入对 k 的区间必须相交（否则图本身不合法），所以取交集仅用于合法性校验。m、n 才是输出维度，分别从 x1、x2 的对应区间继承。

### 4.4 shape 测试：UT 无硬件验证 InferShape

#### 4.4.1 概念说明

InferShape 是纯 host 逻辑（只读 shape 元信息、不做计算），天然适合在**无昇腾硬件**的机器上单测。仓库的 UT 框架（`src/tests/ut/framework_normal`，u6-l1 会系统讲）为它提供了两个组件：

- **`InfershapeContextPara`**（用例参数包）：声明算子名、每个输入/输出的 TensorDescription（shape + dtype + format）和 attrs；
- **`ExecuteTestCase`**（执行器）：把参数包变成伪造的 `InferShapeContext`，从注册表查到该算子的 infer_shape 函数指针直调，再断言返回码与输出 shape。

对号入座第 2 单元的分工：def 靠注册表被发现（u2-l1），InferShape 同样靠注册表被发现——UT 执行器是这张 OpImpl 注册表最直接的「消费者」，看懂它就明白图引擎如何调用你的推导函数。

#### 4.4.2 核心流程

```text
写用例：
  1. 构造 InfershapeContextPara("算子类名", {输入描述...}, {输出描述...})
     - 输入 shape 写真实形状 {{min}, {max}}（静态 shape 时 min == max）
     - 输出 shape 写 {{}, {}} 留空，由被测函数填写
  2. 声明期望：expectResult（GRAPH_SUCCESS/GRAPH_FAILED）+ expectOutputShape（每组一个 vector）
  3. 调 ExecuteTestCase(para, expectResult, expectOutputShape)

执行器内部：
  faker 按参数构建 context → 查 DefaultOpImplSpaceRegistryV2 → 取 infer_shape 指针
  → 调用 → ASSERT_EQ(返回码) → EXPECT_EQ(输出shape, 期望)
```

#### 4.4.3 源码精读

**单输入用例范本**：[test_lower_triangular_inverse_infershape.cpp:31-46](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/tests/ut/op_host/test_lower_triangular_inverse_infershape.cpp#L31-L46)：第 33-41 行构造参数包——输入 shape 写成 `{{1,1,1,64,64}, {1,1,1,64,64}}`（gert::StorageShape 的原始/存储两个形状，静态 shape 时填两遍相同值），输出写 `{{}, {}}`；第 42-44 行声明期望输出 `{1, 1, 1, 64, 64}`；第 46 行一句 `ExecuteTestCase` 完成全部驱动。**注意第 34 行的算子名字符串 `"AiInfraLowerTriangularInverse"` 必须与 OpDef 类名完全一致**，否则注册表查不到实现。

**多输入多输出用例范本**：[test_chunk_gated_delta_rule_recurrence_infershape.cpp:38-68](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/tests/ut/op_host/test_chunk_gated_delta_rule_recurrence_infershape.cpp#L38-L68)：第 46-61 行按 def 声明顺序给出 7 个输入（含 `actual_seqlens` 用 `DT_INT32`）、3 个留空输出；第 62-66 行给出三组期望 shape，顺序对应 proto 的三个输出常量——用例本身就是「def 顺序 ↔ proto 索引 ↔ 期望输出」三方对齐的活文档。

**参数包与 faker**：[infer_shape_context_faker.h:19-89](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/tests/ut/framework_normal/common/infer_shape_context_faker.h#L19-L89) 定义 `InfershapeContextPara`：内嵌 [TensorDescription（L21-32）](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/tests/ut/framework_normal/common/infer_shape_context_faker.h#L21-L32) 持有 StorageShape/dtype/format；[InferShapeContextFaker（L91-151）](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/tests/ut/framework_normal/common/infer_shape_context_faker.h#L91-L151) 用 builder 风格（`NodeIoNum`/`IrInstanceNum`/`Attr`/`InputTensors`）拼出一个假 context——被测函数完全感知不到自己在测试里。

**执行器的关键三行**：[infer_shape_case_executor.cpp:80-83](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/tests/ut/framework_normal/common/infer_shape_case_executor.cpp#L80-L83)——第 80-81 行 `DefaultOpImplSpaceRegistryV2::GetInstance().GetSpaceRegistry()->GetOpImpl(opName)->infer_shape` **从注册表取函数指针**，第 83 行直调。这一段揭示了两件事：`IMPL_OP_INFERSHAPE` 注册的落点就是这张表；图引擎运行期调用 InferShape 走的是同一条查表路径。断言在 [infer_shape_case_executor.cpp:95-111](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/tests/ut/framework_normal/common/infer_shape_case_executor.cpp#L95-L111)：`ASSERT_EQ` 比返回码，`EXPECT_EQ` 逐输出比 shape。

**UT 的构建挂接**：[tests/ut/op_host/CMakeLists.txt:10-13](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/tests/ut/op_host/CMakeLists.txt#L10-L13) 用 `add_modules_ut_sources(UT_NAME ${OP_INFERSHAPE_MODULE_NAME} ...)` 把目录下源码收进 infershape UT 模块；执行入口是 `bash build.sh -u`（u1-l2 讲过 UT 分支，u6-l1 详述）。把新用例文件放进 `tests/ut/op_host/` 即被 GLOB 收编，无需改这个 CMakeLists。

#### 4.4.4 代码实践：为 my_add 写 InferShape 与三组 shape 用例（本讲主实践）

1. **实践目标**：把 4.1 的套路落地——为假想算子 `MyAdd`（两个输入 x、y 相加，输出广播后的形状）编写 infershape 实现与 UT 用例。
2. **操作步骤**：

   **第一步**：写 infershape（示例代码，仿照 chunk proto 的判空风格 + lower_tri 的逐维写法，不是仓库原有文件）：

   ```cpp
   // 示例代码：my_add_infershape.cpp
   #include "exe_graph/runtime/infer_shape_context.h"
   #include "exe_graph/runtime/shape.h"
   #include "register/op_impl_registry.h"

   namespace ops {
   static ge::graphStatus InferShapeMyAdd(gert::InferShapeContext *context)
   {
       const gert::Shape *xShape = context->GetInputShape(0);
       const gert::Shape *yShape = context->GetInputShape(1);
       OPS_LOG_E_IF_NULL(context, xShape, return ge::GRAPH_FAILED);
       OPS_LOG_E_IF_NULL(context, yShape, return ge::GRAPH_FAILED);

       auto outShape = context->GetOutputShape(0);
       const int64_t dimX = xShape->GetDimNum();
       const int64_t dimY = yShape->GetDimNum();
       const int64_t dimOut = std::max(dimX, dimY);
       outShape->SetDimNum(dimOut);
       // 从最后一维（倒数第 1 维）向左对齐做广播，缺位视为 1
       for (int64_t i = 0; i < dimOut; ++i) {
           int64_t dx = (i < dimX) ? xShape->GetDim(dimX - 1 - i) : 1;
           int64_t dy = (i < dimY) ? yShape->GetDim(dimY - 1 - i) : 1;
           if (dx != dy && dx != 1 && dy != 1) {
               OP_LOGE(context->GetNodeName(), "shape not broadcastable at dim -%ld: %ld vs %ld",
                       i + 1, dx, dy);
               return ge::GRAPH_FAILED;
           }
           outShape->SetDim(dimOut - 1 - i, std::max(dx, dy));
       }
       return GRAPH_SUCCESS;
   }

   static ge::graphStatus InferDataTypeMyAdd(gert::InferDataTypeContext *context)
   {
       // 输出 dtype 继承输入 0
       context->SetOutputDataType(0, context->GetInputDataType(0));
       return GRAPH_SUCCESS;
   }

   IMPL_OP_INFERSHAPE(MyAdd)
       .InferShape(InferShapeMyAdd)
       .InferDataType(InferDataTypeMyAdd);
   } // namespace ops
   ```

   **第二步**：写 UT（示例代码，仿照 test_lower_triangular_inverse_infershape.cpp，放在假想的 `tests/ut/op_host/test_my_add_infershape.cpp`）：

   ```cpp
   // 示例代码：test_my_add_infershape.cpp
   #include <gtest/gtest.h>
   #include "infer_shape_context_faker.h"
   #include "infer_shape_case_executor.h"

   TEST_F(MyAddProtoTest, same_shape)
   {
       gert::InfershapeContextPara para("MyAdd",
           {
               {{{16, 128}, {16, 128}}, ge::DT_FLOAT, ge::FORMAT_ND},
               {{{16, 128}, {16, 128}}, ge::DT_FLOAT, ge::FORMAT_ND},
           },
           {
               {{{}, {}}, ge::DT_FLOAT, ge::FORMAT_ND},
           });
       ExecuteTestCase(para, ge::GRAPH_SUCCESS, {{16, 128}});
   }

   TEST_F(MyAddProtoTest, broadcast_2d_1d)
   {
       gert::InfershapeContextPara para("MyAdd",
           {
               {{{16, 128}, {16, 128}}, ge::DT_FLOAT, ge::FORMAT_ND},
               {{{128}, {128}}, ge::DT_FLOAT, ge::FORMAT_ND},
           },
           {
               {{{}, {}}, ge::DT_FLOAT, ge::FORMAT_ND},
           });
       ExecuteTestCase(para, ge::GRAPH_SUCCESS, {{16, 128}});
   }

   TEST_F(MyAddProtoTest, broadcast_3d_2d)
   {
       // (4, 1, 64) + (8, 64)：右对齐 64==64，1 广播为 8，缺位广播为 4
       gert::InfershapeContextPara para("MyAdd",
           {
               {{{4, 1, 64}, {4, 1, 64}}, ge::DT_FLOAT, ge::FORMAT_ND},
               {{{8, 64}, {8, 64}}, ge::DT_FLOAT, ge::FORMAT_ND},
           },
           {
               {{{}, {}}, ge::DT_FLOAT, ge::FORMAT_ND},
           });
       ExecuteTestCase(para, ge::GRAPH_SUCCESS, {{4, 8, 64}});
   }

   TEST_F(MyAddProtoTest, not_broadcastable_failed)
   {
       // 反例：最后一维 128 vs 127，两者都大于 1 且不等，应失败
       gert::InfershapeContextPara para("MyAdd",
           {
               {{{16, 128}, {16, 128}}, ge::DT_FLOAT, ge::FORMAT_ND},
               {{{16, 127}, {16, 127}}, ge::DT_FLOAT, ge::FORMAT_ND},
           },
           {
               {{{}, {}}, ge::DT_FLOAT, ge::FORMAT_ND},
           });
       ExecuteTestCase(para, ge::GRAPH_FAILED, {});
   }
   ```

   **第三步**：有环境时把文件放进对应目录，用 `bash build.sh -u` 触发 UT 编译并运行 gtest；无环境时对照 4.4.3 的执行器代码，人工走一遍每个用例的推导循环，验证期望值。
3. **需要观察的现象**：三组正例返回 `GRAPH_SUCCESS` 且输出 shape 与手算一致；反例返回 `GRAPH_FAILED`（执行器在 `ASSERT_EQ(infershapeRet, expectResult)` 处通过后直接 return，不再比 shape）。
4. **预期结果**：四个用例全绿（待本地验证：编译运行依赖昇腾 UT 构建环境；`broadcast_3d_2d` 的期望 `(4, 8, 64)` 已按广播规则手算，可先在纸上核对）。

#### 4.4.5 小练习与答案

**练习 1**：UT 用例里输入 shape 为什么写成 `{{16, 128}, {16, 128}}` 两个花括号、输出却写 `{{}, {}}`？

**答案**：TensorDescription 的第一个成员是 `gert::StorageShape`，它由「原始形状 + 存储形状」两部分组成（静态 shape 时填两遍相同值，参考 chunk 用例第 49 行）。输入的形状是用例的前提，必须给全；输出的形状是被测函数要填的答案，留空 `{{}, {}}` 让 InferShape 去写，写完由 `EXPECT_EQ` 与期望比对。

**练习 2**：若把用例里的算子名写成 `"my_add"`（小写），执行器会发生什么？

**答案**：执行器用算子名查 `GetOpImpl("my_add")`，注册表里只有 `"MyAdd"`（OpDef 类名的字符串形式），查表返回空指针，第 83 行 `infershapeFunc(...)` 解引用空指针直接崩溃。算子名字符串必须与类名严格一致。

**练习 3**：为什么 InferShape 的 UT 可以在完全没有 NPU 的开发机上跑？

**答案**：InferShape 只读 shape 元数据并做整数运算，不碰设备内存；faker 在 host 内存里伪造出 `InferShapeContext`，执行器从注册表取函数指针直调，全程没有 aclrt/驱动调用。这也是 u6-l1 把 UT 框架定位为「无硬件验证」的原因。

## 5. 综合实践

**任务：给仓库里一个「没有 infershape 的算子」补一份推导设计与用例（纸面设计）**。

1. 选一个没有 infershape 文件的算子，例如 `ai_infra_scatter_block_update`（原地更新，输入输出同形）。
2. 打开它的 def 文件与 docs 文档，确定：有几个输入输出、输出 shape 应该是什么（scatter 场景输出即输入 `input`，形状 `{batch, seq_len, head_dim, kv_l}` 之类，以 def/docs 为准）。
3. 仿照 4.1 的 lower_triangular 写出 `ai_infra_scatter_block_update_infershape.cpp`：判空 → 输出 = input 输入形状 → 注册 `IMPL_OP_INFERSHAPE(AiInfraScatterBlockUpdate)`。
4. 在 `tests/ut/op_host/` 下仿照 4.4 写至少两个用例：一个常规 shape、一个极端 shape（如某维为 1），并给出一组你预期的失败用例（想一想：这个算子什么输入会推导失败？如果推导逻辑里加「indices 最后一维必须等于 update 倒数第二维」的校验，用例该怎么写）。
5. 在算子根 `CMakeLists.txt` 里指出你会把新文件挂到哪个目标（对照 4.2.3 的 `target_sources(opsproto PRIVATE ...)` 位置）。
6. 有环境时用 `bash build.sh -u` 跑通；无环境时把「def 顺序 ↔ 推导下标 ↔ 用例期望」三列对齐表写成笔记——这张表正是 u6-l3 综合实战的输入之一。

## 6. 本讲小结

- InferShape 是 op_host 层**可选**的推导实现：图引擎构图时靠它静态推算输出 shape/dtype；aclnn 直调场景输出形状由调用方给出，所以仓库 18 个算子目录只有 11 个注册了 `IMPL_OP_INFERSHAPE`。
- 套路固定：静态函数签名 `ge::graphStatus f(gert::InferShapeContext*)`，取输入 shape → 判空 → 推导 → `SetDimNum`/`SetDim` 写回；配套 `InferDataType` 可写死或继承输入；`IMPL_OP_INFERSHAPE(类名)` 链式注册，类名是 def 与 proto 的关联键。
- `*_proto.cpp` 与 `*_infershape.cpp` 本质相同（都编入 opsproto 目标），proto 是旧称；def 的 `Input/Output` 声明顺序就是 proto 里索引常量的取值依据，两边必须严格对齐。
- 复杂推导看 matmul 公共库：广播（1 可扩、不等即错）、转置属性改变 m/k/n 下标、未知维度 -1/-2 的短路、InferShapeRange 的区间交集 \([\max(l_a,l_b), \min(h_a,h_b)]\)；注意其 batch 入口在仓库内无调用点，读它学套路而非追调用链。
- shape 测试零硬件：`InfershapeContextPara`（输入给真形状、输出留空）+ `ExecuteTestCase`（faker 建上下文 → 查 `OpImplSpaceRegistryV2` 注册表 → 直调 → 断言返回码与 shape）；新用例丢进 `tests/ut/op_host/` 即被 GLOB 收编，`build.sh -u` 触发。

## 7. 下一步学习建议

本讲补齐了 op_host 的最后一块拼图（def → tiling → infershape），第 2 单元到此完结。接下来：

- **u3-l1（torch 注册）与 u3-l4（端到端调用链）**：看 InferShape 推出的 shape 如何被 torch_ops_extension 的 Meta 实现复用、图模式（torchair converter）如何经由本讲的注册表调用推导函数。
- **u5-l3（fallback 与错误处理）**：本讲两次出现 `OPS_ERR_IF` / `OPS_LOG_E_IF_NULL` / `CUBE_INNER_ERR_REPORT`，届时系统学习这套错误码体系。
- **u6-l1（UT 框架）**：本讲只用了 faker/executor 的「表层 API」，tiling 侧的 faker 与 case executor、`build.sh -u` 的完整目标体系在 u6-l1 展开。
- 延伸阅读：仓库内最复杂的 infershape 是 [ai_infra_fused_infer_attention_sink_infershape.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_infershape.cpp#L427)（u4-l1 精读该算子时再看），可体会几十个可选输入下推导函数怎么组织。
