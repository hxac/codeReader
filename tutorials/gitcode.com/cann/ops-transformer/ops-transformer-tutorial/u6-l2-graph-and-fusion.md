# u6-l2 op_graph 与图融合入门

## 1. 本讲目标

上一讲（u6-l1）我们走完了「从零开发一个 AICore 算子」的七步流水线，但那条流水线主要服务 Eager（aclnn 直调）形态。本讲把视角切换到**图模式**：

1. 理解 GE（Graph Engine）图模式下，算子原型（proto）与 graph infer（InferDataType）各自承担什么职责。
2. 读懂 fusion_pass（图融合 pass）的注册、版本守卫与节点替换逻辑。
3. 能清晰区分 op_graph 层与 op_host / op_api 层的边界：哪些代码归图模式，哪些归 Eager，哪些两边共用。

学完本讲，你应当能回答：**「一个算子想在图模式下跑起来，比 Eager 模式多交付什么？」**

## 2. 前置知识

本讲默认你已完成 u2 系列与 u6-l1，以下概念直接复用，不再从零解释：

- **Eager 与 Graph 两条调用路径**（u2-l4）：Eager 是 aclnn 两段式直调（GetWorkspaceSize + Run）；Graph 是把算子组装成计算图，交给 GE（Graph Engine，CANN 的图编译执行引擎）整图编译执行。
- **五层目录范式**（u1-l2）：op_host（信息库/infershape/tiling）、op_api（aclnn 接口）、op_kernel（AscendC 核函数）、op_graph（图模式交付件）、tests/examples。
- **REG_OP / IMPL_OP 宏**（u2-l2、u2-l4）：def 文件用 `OP_ADD` 系宏注册算子信息库；图模式下则是 `REG_OP` 注册 IR 原型、`IMPL_OP` 注册图推导函数。

再补充两个本讲新概念，先用直觉解释：

- **算子原型（proto）**：图是一堆「节点 + 边」。GE 看到 `AddExample` 节点时，需要知道"这个算子有几个输入、几个输出、各自允许什么 dtype、有哪些属性"——这份「接口说明书」就是 proto。它不包含任何计算逻辑，只是**让 GE 认识这个算子**。
- **图融合 pass**：GE 在编译图时会跑一系列优化 pass，把图中的节点改写、合并或替换成更优的等价形式。算子库可以注册自己的 pass，例如"在 A5 芯片上，把 `DistributeBarrier` 节点替换成能力更强的 `DistributeBarrierExtend` 节点"。这就是 fusion_pass 目录的用途。

## 3. 本讲源码地图

| 文件/目录 | 作用 |
|---|---|
| [docs/zh/develop/graph_develop_guide.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/graph_develop_guide.md) | 图模式适配官方指南：讲清入图交付件清单 |
| [examples/add_example/op_graph/add_example_proto.h](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_graph/add_example_proto.h) | 教学算子的 proto：REG_OP 注册 IR 原型 |
| [examples/add_example/op_graph/add_example_graph_infer.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_graph/add_example_graph_infer.cpp) | 教学算子的 graph infer：InferDataType 注册 |
| [examples/add_example/op_graph/CMakeLists.txt](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_graph/CMakeLists.txt) | op_graph 构建入口：一行 `add_graph_plugin_sources()` |
| [cmake/obj_func.cmake](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/obj_func.cmake) | `add_graph_plugin_sources` 宏：按文件名约定自动收集 proto 与 fusion_pass 源码 |
| [mc2/distribute_barrier/op_graph/fusion_pass/distribute_barrier_fusion_pass.h](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/distribute_barrier/op_graph/fusion_pass/distribute_barrier_fusion_pass.h) / [.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/distribute_barrier/op_graph/fusion_pass/distribute_barrier_fusion_pass.cpp) | 仓库内真实、注释完整的 fusion_pass 样例 |
| [attention/flash_attention_score/op_graph/flash_attention_score_proto.h](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_graph/flash_attention_score_proto.h) | 工业级算子 proto：20 输入 + 大量属性 |
| [attention/flash_attention_score/op_host/flash_attention_score_infershape.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_host/flash_attention_score_infershape.cpp) | 工业级算子 InferShape/InferDataType 的实际落点（在 op_host，而非 op_graph） |

## 4. 核心概念与源码讲解

### 4.1 op_graph 层：proto 声明与 graph_infer

#### 4.1.1 概念说明

op_graph 目录解决的问题是：**让 GE 认识你的算子，并能在图编译期推导出输出的 shape 与 dtype**。

对比 Eager 模式可以看清分工：

- Eager 模式下，调用的第一段 `GetWorkspaceSize` 内部会走 op_host 的 infershape 和 tiling，输出 shape/dtype 在**运行时**由算子信息库（def 文件注册）兜底推导，调用方通常已知输出形状。
- 图模式下，GE 拿到的是一张完整的图，它必须在**整图编译期**为每个节点推导输出 shape 与 dtype，才能给下游节点分配合适的 TensorDesc、做内存复用和融合优化。这就需要两样东西：
  1. **proto（IR 原型）**：算子的接口说明书——输入/输出/属性的名字、个数、dtype 白名单。
  2. **graph infer（InferDataType）**：输出 dtype 的推导函数。

官方指南 [graph_develop_guide.md:L7-L15](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/graph_develop_guide.md#L7-L15) 给出的入图最小交付件清单是：

```text
${op_name}
├── op_host
│   └── ${op_name}_infershape.cpp   # InferShape：运行时推导输出 shape
├── op_graph
│   ├── CMakeLists.txt
│   ├── ${op_name}_graph_infer.cpp  # InferDataType：运行时推导输出 dataType
│   └── ${op_name}_proto.h          # 算子原型：供图优化与融合阶段识别算子
```

注意指南开头的这句话（[L5](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/graph_develop_guide.md#L5)）：图模式**不需要 aclnn 适配**——即不需要 op_api 层。这与 u2-l4 讲过的 GE 示例（`test_geir_add_example.cpp` 编译期不链接算子库）互为印证。

#### 4.1.2 核心流程

一个算子节点在 GE 图中的「被认识 → 被推导」流程：

1. **构图**：调用方（或框架）用 `OperatorFactory` 按 proto 声明实例化算子节点、连边组图（u2-l4 的 GE 示例就是这条路径）。
2. **图编译**：GE 解析图，遇到 `AddExample` 类型节点时，查 proto 声明校验输入个数与 dtype 是否在白名单内。
3. **shape/dtype 推导**：GE 依次调用该算子注册的 `InferShape`（在 op_host 的 infershape 文件中）与 `InferDataType`（在 op_graph 的 graph_infer 文件中，工业级算子也可能与 InferShape 写在同一个文件，见 4.3），逐节点填出全图的 TensorDesc。
4. **优化与调度**：GE 跑融合 pass（见 4.2），随后做 tiling 与任务调度——tiling 对调用方不可见（u2-l4 已讲）。

用伪代码总结 proto 与两个推导函数的关系：

```text
proto.h        = 「我有哪些端口和属性」（静态，编译期校验依据）
InferShape     = 「我的输出长什么形状」（动态，逐节点推导）
InferDataType  = 「我的输出是什么类型」（动态，逐节点推导）
```

#### 4.1.3 源码精读

**proto：REG_OP 声明**。[add_example_proto.h:L35-L39](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_graph/add_example_proto.h#L35-L39)：

```cpp
REG_OP(AddExample)
    .INPUT(x1, TensorType({DT_FLOAT, DT_INT32}))
    .INPUT(x2, TensorType({DT_FLOAT, DT_INT32}))
    .OUTPUT(y, TensorType({DT_FLOAT, DT_INT32}))
    .OP_END_FACTORY_REG(AddExample)
```

这段代码在 `ge` 命名空间里向 GE 注册了 `AddExample` 的 IR 原型：两个必选输入、一个输出，各自允许 float32/int32。链式 DSL 的常用关键字在指南的表格里有完整对照（[graph_develop_guide.md:L100-L108](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/graph_develop_guide.md#L100-L108)）：`INPUT` / `OPTIONAL_INPUT` / `REQUIRED_ATTR` / `ATTR`（带默认值）/ `OUTPUT`。

注意 proto 与 def 文件（u2-l2）是**两份独立的接口声明**：def 面向算子信息库与 aclnn 校验，proto 面向 GE 图编译。二者描述的是同一个算子，需要人工保持一致。

**graph_infer：InferDataType 注册**。[add_example_graph_infer.cpp:L24-L36](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_graph/add_example_graph_infer.cpp#L24-L36)：

```cpp
static ge::graphStatus InferDataTypeAddExample(gert::InferDataTypeContext *context)
{
    // 设置输出的dtype
    ge::DataType sizeDtype = context->GetInputDataType(IDX_0);
    context->SetOutputDataType(IDX_0, sizeDtype);
    return GRAPH_SUCCESS;
}

IMPL_OP(AddExample).InferDataType(InferDataTypeAddExample);
```

加法输出 dtype 与输入一致，所以推导逻辑就是「取输入 0 的 dtype，设给输出 0」。注册方式是 `IMPL_OP(算子名).InferDataType(函数)`——对比 op_host 侧 infershape 文件用的 `IMPL_OP_INFERSHAPE(算子名).InferShape(函数)`（[graph_develop_guide.md:L50](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/graph_develop_guide.md#L50)），两个宏各管一个推导维度。

**CMake：按文件名约定自动收集**。op_graph 的构建入口只有一行（[add_example/op_graph/CMakeLists.txt:L11](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_graph/CMakeLists.txt#L11)）：

```cmake
add_graph_plugin_sources()
```

这个宏定义在 [cmake/obj_func.cmake:L737-L768](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/obj_func.cmake#L737-L768)，关键逻辑是按命名约定 GLOB 收源码（[L756-L759](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/obj_func.cmake#L756-L759)）：

```cmake
file(GLOB GRAPH_PLUGIN_SRCS
    ${SOURCE_DIR}/*_graph_plugin*.cpp
    ${SOURCE_DIR}/fusion_pass/*fusion_pass*.cpp
)
```

即：`*_graph_plugin*.cpp` 与 `fusion_pass/*fusion_pass*.cpp` 会被自动编进图插件库；`*_proto*.h` 只作为头文件集合登记（[L765-L767](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/obj_func.cmake#L765-L767)），供安装后供 GE/框架包含。宏里同样有 `ASCEND_OP_NAME` 裁剪逻辑（[L744-L753](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/obj_func.cmake#L744-L753)），与 u1-l4 讲的「不传即全量」一致。**这解释了文件命名规范为什么是硬约束**：`add_example_graph_infer.cpp` 若改名去掉 `_graph_plugin` 风格的关键字，将不会被图插件目标收集。教学算子的 fusion_pass 子目录只有一个空 CMakeLists（内容为许可证头），就是给将来预留的占位。

#### 4.1.4 代码实践

1. **实践目标**：亲手为 add_example 的 proto 增加一个可选输入，验证「proto 是编译期接口说明书」。
2. **操作步骤**：
   - 阅读 `examples/add_example/op_graph/` 下三个文件 + `graph_develop_guide.md` 全文；
   - 在 [add_example_proto.h:L36-L38](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_graph/add_example_proto.h#L36-L38) 的两个 `INPUT` 之间插入一行：
     ```cpp
     .OPTIONAL_INPUT(bias, TensorType({DT_FLOAT}))
     ```
   - 执行 `bash build.sh --opgraph --ops=add_example` 重新编译图插件库。
3. **需要观察的现象**：编译能通过（proto 只是声明，不含实现）；对照 u2-l4 的 `test_geir_add_example.cpp`，它按端口顺序构图，未连接的可选输入不影响现有示例。
4. **预期结果**：图插件库编译成功；新增的可选输入在 GE 构图时可连可不连。若进一步想让 kernel 真正消费这个 bias，还需改 def 与 kernel——proto 只管「接口存在」。
5. 若无 NPU 环境，编译 `--opgraph` 不需要硬件（u1-l3 的编译态结论），本实践可完整执行。

#### 4.1.5 小练习与答案

**练习 1**：proto 声明的 dtype 白名单与 def 文件（u2-l2）的 dtype 白名单是什么关系？不一致会怎样？

**答案**：二者是同一算子接口在两个世界的独立声明——def 面向算子信息库与 aclnn 第一段校验（Eager 路径），proto 面向 GE 图编译校验（Graph 路径）。框架不会自动同步它们；若不一致，可能出现「Eager 能跑 dtype X、图模式被 proto 拦截（或反之）」的隐性能力差异。u2-l1 讲过「源码才是事实」，这里同理：评估算子能力要两条路径分别核对。

**练习 2**：`add_graph_plugin_sources()` 靠什么决定把哪些文件编进图插件库？新增一个 fusion_pass 文件需要改 CMake 吗？

**答案**：靠文件名 GLOB 约定：`*_graph_plugin*.cpp` 和 `fusion_pass/*fusion_pass*.cpp`（cmake/obj_func.cmake:756-759）。新增符合命名的 fusion_pass 文件**不需要**改任何 CMake，放进 `fusion_pass/` 子目录即可被自动收集。

**练习 3**：为什么 graph_develop_guide 说图模式「不需要 aclnn 适配」？

**答案**：aclnn（op_api 层）是 Eager 直调的前台，做参数翻译与两段式下发；图模式下 GE 直接以 IR 节点为粒度调度算子，tiling/执行由框架在图编译阶段驱动 op_host 与 op_kernel 完成，调用方不接触 aclnn 接口，所以 op_api 层对图模式不是必需交付件。

### 4.2 图融合 pass（fusion_pass）

#### 4.2.1 概念说明

GE 编译一张图时会经过多个优化阶段，算子库可以通过 `REG_FUSION_PASS` 注册自定义改写逻辑，在图上做等价变换。典型动机：

- **按硬件代际路由到更强变体**：如 A5 上把 `DistributeBarrier` 换成带 context 输入的 `DistributeBarrierExtend`；
- **版本升级的兼容层**：如 u5-l4 讲过的 dispatch/combine「V2→V3」pass——用户图里画的是 V2 节点，pass 在编译期把它替换成 V3 实现，老图零改动享受新内核；
- **把多个小节点合成一个大节点**：经典融合语义（本仓库的 fusion_pass 以前两类为主）。

#### 4.2.2 核心流程

一个 fusion_pass 的骨架：

```text
继承 ge::fusion::FusionBasePass，重写 Run(graph, passContext)
  ├── 守卫 1：CANN 版本宏（编译期，#if CANN_VERSION_NUM >= ...）
  ├── 守卫 2：运行时 ge_compiler 版本 / 目标 SoC 判断（不满足则空跑返回 SUCCESS）
  ├── 遍历 graph 节点，按类型/属性匹配目标节点
  ├── 创建替换节点（OperatorFactory 按目标算子 proto 实例化）
  ├── 拷贝属性、按端口映射迁移数据边与控制边
  └── RemoveNode 摘除原节点
REG_FUSION_PASS(Pass名).Stage(注册阶段)   // 挂到 GE 的某个编译阶段
```

关键设计点：**pass 失败不应毁掉整张图**——匹配不到或替换单个节点失败时记日志、跳过该节点继续，这是所有样本的共同风格。

#### 4.2.3 源码精读

以仓库内注释最完整的 [distribute_barrier_fusion_pass.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/distribute_barrier/op_graph/fusion_pass/distribute_barrier_fusion_pass.cpp) 为样本。

**头文件：类与版本守卫**。[distribute_barrier_fusion_pass.h:L14-L21](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/distribute_barrier/op_graph/fusion_pass/distribute_barrier_fusion_pass.h#L14-L21)：

```cpp
#define GRAPH_FUSION_SUPPORT_VERSION 90000000
#if CANN_VERSION_NUM >= GRAPH_FUSION_SUPPORT_VERSION
#include "ge/fusion/pass/pattern_fusion_pass.h"

namespace ops {
class __attribute__((visibility("default"))) DistributeBarrierFusionPass : public ge::fusion::FusionBasePass {
public:
    ge::graphStatus Run(ge::GraphPtr &graph, ge::CustomPassContext &pass_context) override;
```

两层保险的第一层在这里：整套 pass 代码包在 `#if CANN_VERSION_NUM >= 90000000` 里，用老版本 toolkit（9.0.0 之前）编译时整个文件退化为空，不依赖新版 GE 头文件。

**cpp：pass 的意图注释**。[L12-L20](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/distribute_barrier/op_graph/fusion_pass/distribute_barrier_fusion_pass.cpp#L12-L20) 的头注释直接说明了这个 pass 做什么：「将图中的 DistributeBarrier 节点 1:1 替换为 DistributeBarrierExtend（仅 A5）」，并点出了两个工程细节——与 D&C 的 V2→V3 pass 共享 context Const 节点、控制边需先迁移再删节点以避免成环。读工业代码先读这种意图注释，性价比极高。

**Run：三重守卫 + 遍历替换**。[L229-L246](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/distribute_barrier/op_graph/fusion_pass/distribute_barrier_fusion_pass.cpp#L229-L246)：

```cpp
ge::graphStatus DistributeBarrierFusionPass::Run(ge::GraphPtr &graph, ge::CustomPassContext &passContext)
{
    ...
    // 9.0.0 版本前运行降级空跑
    int32_t geCompilerVersion = 0;
    aclsysGetVersionNum("ge_compiler", &geCompilerVersion);
    if (geCompilerVersion < GRAPH_FUSION_SUPPORT_VERSION) {
        return ge::GRAPH_SUCCESS;
    }
    // 仅 A5 支持 DistributeBarrierExtend
    if (!IsTargetPlatformNpuArch(FUSION_PASS_NAME.c_str(), NPUARCH_A5)) {
        return ge::GRAPH_SUCCESS;
    }
```

第二、三层守卫是**运行时**的：宿主机 toolkit 的 ge_compiler 版本、目标芯片是否 A5。任一不满足就原样保留节点、空跑返回——图照常编译，只是不做这次优化。随后 [L249-L296](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/distribute_barrier/op_graph/fusion_pass/distribute_barrier_fusion_pass.cpp#L249-L296) 遍历 `graph->GetDirectNode()`，按节点类型 `DistributeBarrier` 和 `group` 属性过滤，为每个通信域建（或复用）一个 context Const 节点，再逐节点执行替换；单个节点失败仅 `OPS_LOG_E` 后 `continue`，不拖垮整图。

**注册：挂到 GE 编译阶段**。[L300](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/distribute_barrier/op_graph/fusion_pass/distribute_barrier_fusion_pass.cpp#L300)：

```cpp
REG_FUSION_PASS(DistributeBarrierFusionPass).Stage(GetDistributeBarrierFusionPassStage());
```

`Stage` 决定 pass 在 GE 流水线的哪个阶段执行。stage 选择函数（[L45-L53](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/distribute_barrier/op_graph/fusion_pass/distribute_barrier_fusion_pass.cpp#L45-L53)）再次体现版本兼容：新 toolkit 用 `kCompatibleInherited`，老的回退 `kBeforeInferShape`。u5-l4 讲过的 dispatch/combine V2→V3 pass（如 [moe_distribute_dispatch_v2_to_v3_fusion_pass.cpp:L352](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/moe_distribute_dispatch_v2/op_graph/fusion_pass/moe_distribute_dispatch_v2_to_v3_fusion_pass.cpp#L352) 的 `REG_FUSION_PASS(MoeDistributeDispatchV2FusionPass)`）就是同一套机制在业务上的应用：老版本节点在图编译期被静默升级。

#### 4.2.4 代码实践

1. **实践目标**：走读一个真实 fusion_pass，理解「守卫 + 匹配 + 替换」三段式。
2. **操作步骤**：
   - 通读 `distribute_barrier_fusion_pass.cpp` 的 `Run` → `FusionNode` → `CreateFusionNode`/`AddEdge` 调用链（L229-L227 区间）；
   - 重点看 `AddEdge` 中控制边迁移（[L167-L179](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/distribute_barrier/op_graph/fusion_pass/distribute_barrier_fusion_pass.cpp#L167-L179)）与「先摘输出边、删节点、再重连」的顺序（[L180-L215](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/mc2/distribute_barrier/op_graph/fusion_pass/distribute_barrier_fusion_pass.cpp#L180-L215)），对照头注释解释为什么要这个顺序；
   - 再打开 `mc2/moe_distribute_dispatch_v2/op_graph/fusion_pass/` 与 `gmm/grouped_matmul/op_graph/fusion_pass/` 各看一眼 `REG_FUSION_PASS` 行，确认模式一致。
3. **需要观察的现象**：三个 pass 的 `Run` 都是「守卫 → 遍历 → 匹配 → 替换/跳过」；`gmm/grouped_matmul/tests/ut/op_graph/` 下还有 fusion_pass 的 UT，说明 pass 本身也可被单测。
4. **预期结果**：能独立说出这个 pass 在非 A5 平台、老 CANN、老 HCCL 三种情况下分别如何优雅退出（分别对应 L236-L241、L236-L238 的 geCompilerVersion 检查、L291-L294 的 HCOMM_VERSION_NUM 分支）。
5. 本实践为源码阅读型，无需 NPU。

#### 4.2.5 小练习与答案

**练习 1**：fusion_pass 的三层「守卫」分别作用在什么时刻？

**答案**：① `#if CANN_VERSION_NUM >= 90000000` 是**编译期**守卫（老 toolkit 连头文件都没有）；② `aclsysGetVersionNum("ge_compiler", ...)` 是**运行时（宿主 toolkit）**守卫，防止编译期与运行环境版本错配；③ `IsTargetPlatformNpuArch(..., NPUARCH_A5)` 是**运行时（目标芯片）**守卫，决定本次图编译是否适用该优化。

**练习 2**：为什么 pass 替换单个节点失败时选择 `continue` 而不是返回 `GRAPH_FAILED`？

**答案**：fusion pass 是图优化，不是功能正确性的一部分。原节点本身是合法可执行的（如 `DistributeBarrier` 在非 A5 上就是最终形态），替换失败退回原节点，整图仍能正确编译运行；返回 FAILED 反而会让一次本可成功的图编译整体失败，把「锦上添花」变成「单点故障」。

### 4.3 op_graph 与 op_host / op_api 的边界

#### 4.3.1 概念说明

初学者最容易混淆的问题是：「InferShape / InferDataType 到底归 op_host 还是 op_graph？」用 flash_attention_score 这个同时支持 eager 与 graph 的工业级算子来回答最清楚。三层边界：

| 层 | 服务对象 | 典型内容 | FA 中的落点 |
|---|---|---|---|
| op_api | 仅 Eager（aclnn 直调） | 参数校验漏斗、两段式接口 | `op_api/aclnn_flash_attention_score.cpp`（u4-l2） |
| op_host | Eager 与 Graph **共用** | 算子信息库 def、InferShape、tiling | `op_host/flash_attention_score_infershape.cpp`、`*_tiling.cpp` |
| op_graph | 仅 Graph | proto、graph infer、fusion_pass | `op_graph/flash_attention_score_proto.h` |

也就是说：**op_host 是两条路径的共同地基；op_api 与 op_graph 是各自路径的「前台」，互不依赖**。指南把 infershape 放在 op_host、graph_infer 放在 op_graph 的目录约定，是教学算子的「标准摆放」；工业级算子还允许把 InferDataType 与 InferShape 写在同一个 op_host 文件里联合注册。

#### 4.3.2 核心流程

「一个算子同时交付 eager 与 graph」的文件视图：

```text
flash_attention_score/
├── op_api/    ── aclnn*.cpp        只被 Eager 调用
├── op_host/   ── def / infershape / tiling   被 Eager 第一段与 GE 图编译共同消费
├── op_graph/  ── proto.h           只被 GE 消费（+ 可选 fusion_pass/）
├── op_kernel/ ── AscendC 核函数     两条路径最终执行的是同一份
└── tests/examples/
```

#### 4.3.3 源码精读

**FA 的 InferDataType 落在 op_host**。[flash_attention_score_infershape.cpp:L172-L203](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_host/flash_attention_score_infershape.cpp#L172-L203)：

```cpp
ge::graphStatus InferDataTypeFlashAttentionScore(gert::InferDataTypeContext *context)
{
    ...
    context->SetOutputDataType(0, DT_FLOAT);   // softmax_max 恒为 float
    context->SetOutputDataType(1, DT_FLOAT);   // softmax_sum 恒为 float
    if (dtype == DT_FLOAT8_E5M2 || dtype == DT_FLOAT8_E4M3FN || dtype == DT_HIFLOAT8) {
        ...
        context->SetOutputDataType(INDEX_2, ge::DT_BF16);  // 量化输入时输出升为 bf16
        context->SetOutputDataType(INDEX_3, ge::DT_BF16);
        return GRAPH_SUCCESS;
    }
    context->SetOutputDataType(INDEX_2, dtype);            // 常规输入时与输入同型
    context->SetOutputDataType(INDEX_3, dtype);
    return GRAPH_SUCCESS;
}

IMPL_OP_INFERSHAPE(FlashAttentionScore).InferShape(InferShapeFlashAttentionScore)
                                        .InferDataType(InferDataTypeFlashAttentionScore);
```

对比 add_example 的「拷贝输入 dtype」，FA 的推导是真实的业务逻辑：多输出各自不同规则，且依赖输入 dtype 与属性分支（FP8 输入输出升精度为 BF16，呼应 u4-l5 的量化模式）。注册宏是 `IMPL_OP_INFERSHAPE(...)` 链式挂两个推导函数——**注册机制不关心文件放在哪个目录，CMake 收集与目录范式才是摆放依据**（op_host 侧由 `add_modules_sources` 按文件名含 `infershape` 等约定收集，u2-l1）。

**FA 的 proto：工业级规模**。[flash_attention_score_proto.h:L75-L112](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_graph/flash_attention_score_proto.h#L75-L112)：

```cpp
REG_OP(FlashAttentionScore)
    .INPUT(query, TensorType({DT_FLOAT16, DT_BF16, DT_FLOAT32, ...}))
    ...
    .OPTIONAL_INPUT(real_shift, TensorType({DT_FLOAT16, DT_BF16, DT_FLOAT32}))
    .OPTIONAL_INPUT(drop_mask, TensorType({DT_UINT8}))
    ...（共 17 个 OPTIONAL_INPUT）
    .REQUIRED_ATTR(head_num, Int)
    .REQUIRED_ATTR(input_layout, String)
    ...
    .OP_END_FACTORY_REG(FlashAttentionScore)
```

与 add_example 的 3 端口相比，FA 的 proto 有 20 个输入端口（3 必选 + 17 可选）和十余个属性，且 proto 上方的注释块（[L31-L73](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/attention/flash_attention_score/op_graph/flash_attention_score_proto.h#L31-L73)）逐项说明每个可选输入的 dtype 与语义——这就是「接口说明书」该有的样子。同时注意：**FA 的 op_graph 目录只有这一个 proto 头文件**，没有 graph_infer、没有 fusion_pass——它的 InferDataType 已在 op_host 注册，不需要第二份；没有图改写需求就不建 fusion_pass。op_graph 目录「缺层」的语义与 u1-l2 讲的算子级缺层一致：只交付实际需要的部分。

#### 4.3.4 代码实践（本讲综合实践前置版）

1. **实践目标**：形成「eager + graph 双交付」的完整清单意识。
2. **操作步骤**：
   - `ls attention/flash_attention_score/op_graph/` 与 `ls examples/add_example/op_graph/`，对比目录内容差异；
   - 在 FA 仓库目录下全局搜索 `InferDataType`，确认它只出现在 op_host 的 infershape 文件中；
   - 对照 graph_develop_guide.md 的交付件清单，逐项勾出 FA 满足/超出的项。
3. **需要观察的现象**：FA 的 op_graph 比 add_example 的**更小**（只有 proto），因为它的推导注册已由 op_host 承担、且无融合需求。
4. **预期结果**：得出结论「op_graph 的最小必需件是 proto；graph_infer 与 fusion_pass 是按需交付」。
5. 纯目录走读，无需任何环境。

#### 4.3.5 小练习与答案

**练习 1**：为什么 add_example 把 InferDataType 单独放 op_graph，而 FA 放在 op_host 的 infershape 文件里？两种摆法矛盾吗？

**答案**：不矛盾。注册宏 `IMPL_OP(...).InferDataType(...)` / `IMPL_OP_INFERSHAPE(...)` 决定函数挂到哪个算子上，与文件所在目录无关。指南的目录摆放是教学范式（清晰隔离）；FA 把 InferShape 与 InferDataType 放同一文件，是因为两者推导逻辑高度耦合（都依赖 layout/量化分支），放一起更好维护。CMake 收集按文件名约定走，两种摆法都能被正确编译。

**练习 2**：如果只交付 proto 而不注册任何 InferShape/InferDataType，图模式会发生什么？

**答案**：GE 能识别并校验该算子节点的接口，但编译期无法为它推导输出 TensorDesc——下游节点的 shape/dtype 断链，整图编译失败或输出描述未知。所以指南把 infershape（op_host）+ graph_infer（op_graph）+ proto 并列为入图三交付件。

## 5. 综合实践

**任务：写一份《AddExample 双模式交付清单》并做一次微型扩展。**

1. 通读 `examples/add_example/op_graph/` 三个文件与 `docs/zh/develop/graph_develop_guide.md`，用自己的话写两段说明：
   - proto 声明（`REG_OP`）为图模式提供了什么；
   - graph_infer（`InferDataType`）与 op_host 的 `InferShape` 如何配合完成图编译期的输出推导。
2. 观察 `attention/flash_attention_score/op_graph/`（只有 proto）与 `mc2/distribute_barrier/op_graph/`（proto + gen_task + fusion_pass）两种形态，结合 u4-l2（FA 的 op_api 层）与 u5-l4（dispatch V2→V3 pass），写一段 300 字左右的总结：**「一个算子要同时支持 eager 与 graph 两种交付，需要额外维护哪些内容」**。至少覆盖：proto 与 def 的双份接口声明及一致性负担、InferShape/InferDataType 注册、（可选）fusion_pass 及其版本/SoC 守卫、op_api 与 op_graph 的互不依赖关系。
3. 微型扩展（可选，需编译环境）：按 4.1.4 的步骤给 proto 加一个 `OPTIONAL_INPUT(bias, ...)`，`bash build.sh --opgraph --ops=add_example` 编译通过后，在总结中记录这次改动波及的文件数——直观体会「proto 只是说明书」。

## 6. 本讲小结

- op_graph 层是图模式的交付层：**proto（REG_OP）是算子给 GE 的接口说明书**，**graph_infer（IMPL_OP + InferDataType）负责图编译期推导输出 dtype**；InferShape 仍在 op_host（`IMPL_OP_INFERSHAPE`）。
- 图模式入图三交付件：op_host 的 infershape、op_graph 的 graph_infer 与 proto；**不需要 aclnn（op_api）适配**。
- op_graph 构建靠 `add_graph_plugin_sources()` 的文件名 GLOB 约定（`*_graph_plugin*.cpp`、`fusion_pass/*fusion_pass*.cpp`、`*_proto*.h`），新增符合命名的文件零 CMake 改动。
- fusion_pass 继承 `ge::fusion::FusionBasePass` 重写 `Run`，经 `REG_FUSION_PASS(...).Stage(...)` 注册到 GE 编译阶段；工业样本（distribute_barrier）展示了编译期宏、运行时 ge_compiler 版本、目标 SoC 三重守卫与「单节点失败仅跳过」的鲁棒风格。
- 层边界：op_host 是 Eager 与 Graph 的共同地基；op_api 只服务 Eager；op_graph 只服务 Graph；两路径最终执行同一份 op_kernel。
- 工业级算子的 op_graph 可以只有 proto（如 FA），graph_infer 与 fusion_pass 都是按需交付。

## 7. 下一步学习建议

- 下一讲 u6-l3 将进入 **ONNX 插件框架**：ONNX 模型中的算子如何经插件映射到本仓库算子——proto 注册的 IR 原型正是插件落图时的接口依据，本讲知识直接承接。
- 想加深图编译理解，建议走读 `gmm/grouped_matmul/tests/ut/op_graph/test_grouped_matmul_transpose_fusion_pass.cpp`，看 fusion_pass 如何被单测（承接 u7-l1 的 UT 体系）。
- 结合 u2-l4 的 `test_geir_add_example.cpp` 重跑一次 GE 示例，把「构图 → proto 校验 → infer 推导 → 执行」在脑中串成一条完整链路。
