# 昇腾自定义算子分层模型：op_def / op_api / op_host / op_kernel

## 1. 本讲目标

上一讲（u1-l1）我们建立了 openPangu 2.0 训练算子库的全景认知：仓库分 ascendc / pypto / triton 三大板块，ascendc 下按 attention / mhc / mome 三个算法家族组织了 19 个算子目录。本讲要解决的问题是：**打开任意一个算子目录后，里面的文件各司什么职？**

学完本讲，你应该能够：

1. 准确说出 op_def（原型注册）、op_host（Host 侧 Tiling/InferShape）、op_kernel（设备侧 Kernel）、op_api（对外 aclnn 接口）四层的职责边界。
2. 解释 aclnn 两段式接口、Tiling 切分、Ascend C Kernel 各自解决什么问题。
3. 走进任意一个算子目录，快速定位四层对应的文件，并判断哪些层缺失、由谁补齐。

这是阅读本仓库**最重要的心智模型**——后面所有讲义的源码精读，都在这四层的坐标系里展开。

## 2. 前置知识

### 2.1 算子、Host 与 Device

- **算子（Operator）**：深度学习框架中一个可调度的计算单元，比如矩阵乘、卷积、Attention。模型就是一张由算子节点组成的计算图。
- **Host 侧**：指服务器 CPU 侧。框架（PyTorch/图引擎）运行在 Host 上，负责构图、推导 shape、给任务排队。
- **Device 侧**：指昇腾 NPU 芯片侧。真正的大规模并行计算发生在这里。昇腾芯片上有大量 AI Core（向量/矩阵计算核），本仓库 kernel 代码中会进一步区分 AIC（Cube 矩阵核）与 AIV（Vector 向量核）。
- **GM 与 UB**：Global Memory 是设备上的全局显存（输入输出张量放这里）；Unified Buffer 是 AI Core 内部的高速缓存，kernel 计算前必须先把数据从 GM 搬进 UB。

### 2.2 为什么需要 Tiling

NPU 上一个算子的数据量（比如 `[S, B, H]` 的一个大张量）远超单个核的 UB 容量，也远超一份数据的并行需求。所以 Host 侧在下发计算前，必须先回答三个问题：

1. 数据怎么**切**？每块多大（block）、每个核处理哪一段？
2. 切法是哪一种？——用 **tilingKey** 编号告诉设备侧。
3. 需要多大的 **workspace**（GM 上的中转工作区）？

这一步规划就叫 **Tiling**，规划结果序列化成 **TilingData** 结构体，随任务一起下发。设备侧 kernel 拿到 TilingData 后按图施工。你可以把它类比成"切蛋糕前先规划：几个人分、每人几块、最后一块多大"。

### 2.3 aclnn 与两段式接口

**aclnn** 是昇腾 CANN 对外暴露的单算子 C 接口命名前缀（acl = Ascend Computing Language，nn = 算子网络）。每个 aclnn 算子接口拆成**两段**：

1. `aclnnXxxGetWorkspaceSize(...)`：根据输入描述（shape/dtype/属性）完成 shape 推导与执行计划构建，产出 `workspaceSize`（需要多大的工作区）和 `executor`（执行器，封装了整个计算流程）。
2. `aclnnXxx(workspace, workspaceSize, executor, stream)`：在指定 stream 上真正执行。

两段拆开的好处：第一段是纯规划（可以在 Host 并发地做），第二段才是异步下发。后面 4.5 节会看到真实签名。

### 2.4 承接上一讲

上一讲我们确认了每个算子目录的标准五件套：README、docs、op_host、op_kernel、tests（op_api 可选）。本讲就是把其中的 op_host（内含 `_def.cpp` 与 `_tiling.cpp` 两类文件）、op_kernel、op_api 这三个目录讲透，并用 mome 家族结构最简单的 `ai_infra_aggregate_hidden` 做解剖标本，用 attention 家族的 `flash_attention_score_enhance` 补充 op_api 层的完整实例。

## 3. 本讲源码地图

| 文件 | 所属层 | 作用 |
| --- | --- | --- |
| `ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp` | op_def | 声明算子原型：输入/输出、类型格式、按芯片注册 |
| `ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp` | op_host（tiling） | 校验输入、按核数切分数据、产出 TilingData/tilingKey/workspace |
| `ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.h` | op_host（tiling） | tiling 类与 TilingData 结构的声明 |
| `ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.cpp` | op_kernel | 设备侧 kernel 入口：按 tilingKey 分发到模板实现 |
| `ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.h` | op_kernel | Kernel 模板类：Init（绑地址/配 UB）+ Process（CopyIn→Compute→CopyOut） |
| `ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.h` | op_api | aclnn 两段式接口声明（FA 增强版，参数最全的例子） |
| `ascendc/src/ops-transformer/attention/flash_attention_score_enhance/docs/aclnnFlashAttentionVarLenScoreEnhanceV5.md` | docs | FA aclnn 接口的官方文档 |
| `ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/docs/aclnnAiInfraAggregateHidden.md` | docs | aggregate_hidden 的 aclnn 接口文档（注意：该算子没有 op_api 目录） |
| `ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mome/aggregate_hidden/csrc/npu_aggregate_hidden.cpp` | torch 扩展 | 在 PyTorch 侧调用 aclnn 的实例（用于解释缺失层由谁补齐） |

## 4. 核心概念与源码讲解

### 4.1 四层分层模型总览：一张职责地图

#### 4.1.1 概念说明

本仓库的每个 Ascend C 算子都由四层组成。它们是**四份职责不同的代码**，编译成**不同的产物**，在**不同时机**被执行：

| 层 | 目录/文件惯例 | 职责 | 运行位置 | 编译产物 |
| --- | --- | --- | --- | --- |
| **op_def（原型注册）** | `op_host/*_def.cpp` | 声明算子叫什么、有几个输入输出、类型/格式约束、支持哪些芯片 | Host（注册期） | 算子原型信息库 |
| **op_host（Tiling/InferShape）** | `op_host/*_tiling.cpp` 等 | 校验输入合法性；规划数据切分；产出 TilingData、tilingKey、workspaceSize | Host（每次下发前） | tiling 动态库 |
| **op_kernel（设备 Kernel）** | `op_kernel/*.cpp` | 用 Ascend C 写真正的并行计算，消费 TilingData | Device（NPU 核上） | `.o`/二进制 kernel |
| **op_api（aclnn 接口）** | `op_api/aclnn_*.h/.cpp` | 把上面三层包装成两段式 C 接口供外部调用 | Host（调用入口） | opapi 动态库符号 |

几个容易混淆的点：

- `_def.cpp` 虽然物理上放在 `op_host` 目录里，但职责上是独立的"原型注册层"，本讲单独把它称为 op_def。
- **aclnn、tiling、kernel 各自解决什么问题**？一句话：aclnn 解决"怎么被别人调用"（接口层），tiling 解决"这次调用怎么切分"（规划层），kernel 解决"切好的块怎么算"（执行层）。op_def 则是三者的"出生证明"，没有注册，后面三层都无法与框架对接。
- 本仓库并非每个算子四层齐全：很多算子没有 op_api 目录，4.5 节会解释这层缺失由谁补齐。

#### 4.1.2 核心流程

以一次完整的算子调用为例，四层的协作时序如下：

```text
调用方（torch 扩展 / CANN 图引擎）
  │
  ▼
[op_api 层] aclnnXxxGetWorkspaceSize(input 描述, 属性, ...)
  │   触发 shape 推导与执行计划构建
  ▼
[op_host 层] TilingForXxx(TilingContext)
  │   1. 读平台信息（核数、socVersion）
  │   2. 校验输入（shape/dtype/约束）
  │   3. CoreSplit：决定 baseH/baseB/baseS 与 blockDim
  │   4. 写 TilingData + SetTilingKey + 设置 workspaceSize
  ▼
[op_def 层]（注册期已完成）框架凭 OpDef 找到算子原型 → 匹配 kernel 二进制
  │
  ▼
[op_api 层] aclnnXxx(workspace, size, executor, stream)  下发执行
  │
  ▼
[op_kernel 层] extern "C" kernel 入口
      TILING_KEY_IS(key) 按 tilingKey 选分支
      → GET_TILING_DATA_WITH_STRUCT 解析 TilingData
      → Kernel<dtype>().Init(...) 搬入数据
      → Process() 分核并行计算 → 写回输出
```

#### 4.1.3 源码精读

先用 `ls` 视角看解剖标本 `ai_infra_aggregate_hidden` 的目录（本讲已实际核对磁盘文件）：

```text
ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/
├── README.md                       # 算子说明
├── CMakeLists.txt
├── docs/
│   └── aclnnAiInfraAggregateHidden.md    # aclnn 接口文档（op_api 缺失时它是唯一接口说明）
├── op_host/
│   ├── CMakeLists.txt
│   ├── ai_infra_aggregate_hidden_def.cpp       # ← op_def 层
│   ├── ai_infra_aggregate_hidden_tiling.cpp    # ← op_host 层（tiling 实现）
│   └── ai_infra_aggregate_hidden_tiling.h      # ← op_host 层（tiling 声明）
├── op_kernel/
│   ├── ai_infra_aggregate_hidden.cpp           # ← op_kernel 层（入口）
│   ├── ai_infra_aggregate_hidden.h             # ← Kernel 模板类
│   └── ai_infra_aggregate_hidden_common.h      # ← 切分策略基类
└── tests/
    ├── st/test_ai_infra_aggregate_hidden.py
    └── ut/op_host/test_ai_infra_aggregate_hidden_tiling.cpp
```

这个算子**没有 `op_api/` 目录**——但这不代表它没有 aclnn 接口。[docs/aclnnAiInfraAggregateHidden.md:L28-L47](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/docs/aclnnAiInfraAggregateHidden.md#L28-L47) 中完整声明了 `aclnnAiInfraAggregateHiddenGetWorkspaceSize` 与 `aclnnAiInfraAggregateHidden` 两段式原型，而真实的调用发生在 PyTorch 扩展层：[npu_aggregate_hidden.cpp:L31](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mome/aggregate_hidden/csrc/npu_aggregate_hidden.cpp#L31) 用 `EXEC_NPU_CMD_V1(aclnnAiInfraAggregateHidden, input, weight, mask, output)` 完成调用。也就是说，缺失的 op_api 源码由**已安装算子包在运行期提供的 aclnn 符号 + torch 扩展的动态解析宏**共同补齐（详见 4.5.3 节）。

#### 4.1.4 代码实践：四层文件对照表

**实践目标**：亲手核对若干算子目录，把"四层"从概念变成肌肉记忆，并学会回答"缺了哪层、由谁补齐"。

**操作步骤**：

1. 进入 `ascendc/src/ops-transformer/` 目录。
2. 任选三个算子目录（建议：`attention/lightning_indexer_enhance`、`mhc/manifold_constrained_hyper_connection_sinkhorn_enhance`、`mhc/ai_infra_sinkhorn_grad`，再加上标本 `mome/ai_infra_aggregate_hidden`）。
3. 对每个目录执行 `ls -R`（或用 Glob 工具），填写下面这张对照表：

| 算子目录 | op_def 文件 | op_host tiling 文件 | op_kernel 入口 | op_api 文件 | 缺失情况与补齐方式 |
| --- | --- | --- | --- | --- | --- |
| （填写） | （填写） | （填写） | （填写） | （填写） | （填写） |

4. 对缺失 op_api 的算子，在 `ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/` 下查找同名算子的 `csrc/*.cpp`，确认是否有 `EXEC_NPU_CMD_V1(aclnnXxx, ...)` 调用。

**需要观察的现象**：哪些算子四层齐全？缺失的层集中在哪一层？

**预期结果**（本讲已用 Glob 核对磁盘，可作为参考答案）：

| 算子目录 | op_def | op_host（tiling） | op_kernel 入口 | op_api | 缺失与补齐 |
| --- | --- | --- | --- | --- | --- |
| `mome/ai_infra_aggregate_hidden` | `op_host/ai_infra_aggregate_hidden_def.cpp` | `ai_infra_aggregate_hidden_tiling.cpp` + `.h` | `op_kernel/ai_infra_aggregate_hidden.cpp` | **缺失** | docs 声明 aclnn；torch 扩展 `mome/aggregate_hidden/csrc/npu_aggregate_hidden.cpp` 动态解析符号调用 |
| `attention/lightning_indexer_enhance` | `lightning_indexer_enhance_def.cpp` | `_tiling.cpp` + `.h`（另有 `_proto.cpp` 做 shape/属性推导） | `lightning_indexer_enhance.cpp` | **有**：`op_api/aclnn_lightning_indexer_enhance.h/.cpp` | 四层齐全，无需补齐 |
| `mhc/manifold_constrained_hyper_connection_sinkhorn_enhance` | `..._def.cpp` | `_tiling.cpp/.h` + `_tiling_base.cpp`（两级 tiling）+ `_proto.cpp` | `..._sinkhorn_enhance.cpp` | **缺失** | torch 扩展 `mhc/sinkhorn/csrc/npu_sinkhorn.cpp` 调 aclnn |
| `mhc/ai_infra_sinkhorn_grad` | `ai_infra_sinkhorn_grad_def.cpp` | `_tiling.cpp/.h` + `_tiling_base.cpp` | `ai_infra_sinkhorn_grad.cpp` | **缺失** | torch 扩展 `mhc/sinkhorn_grad/csrc/npu_sinkhorn_grad.cpp` 调 aclnn |

**结论规律**：本仓库中 op_def / op_host / op_kernel 三层几乎所有算子都有；op_api 只有部分 attention 算子自带源码，其余算子的 aclnn 适配统一走"安装期 opapi 符号 + torch_ops_extension 动态解析"路线。

#### 4.1.5 小练习与答案

**练习 1**：`_def.cpp` 放在 `op_host` 目录里，为什么本讲仍把它视为独立的一层？

答案：因为目录只是物理组织，职责才是分层依据。`_def.cpp` 只做原型注册（声明输入输出与芯片配置），不含任何 tiling 切分逻辑；它编译进的是算子原型信息，而 `_tiling.cpp` 编译成 tiling 动态库。二者被框架消费的时机也不同（注册期 vs 每次下发前）。

**练习 2**：如果只给你一个算子名，如何最快判断它有没有 op_api 源码？

答案：`ls ascendc/src/ops-transformer/<family>/<op>/op_api/` 看目录是否存在（也可查 `docs/` 下是否有 aclnn 文档）。不存在时到 `ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/` 下找对应 `csrc/`，看是否用 `EXEC_NPU_CMD_V1` 调用了以该算子命名的 aclnn 接口。

**练习 3**：四层中哪一层运行在 NPU 上？它从哪里拿到切分信息？

答案：op_kernel 运行在 NPU 的 AI Core 上。它通过入口函数的 `tiling` 参数（GM 地址）拿到 Host 侧序列化的 TilingData，用 `GET_TILING_DATA_WITH_STRUCT` 反序列化成结构体后使用。

### 4.2 原型注册层：`_def.cpp` 与 OpDef

#### 4.2.1 概念说明

框架要调度一个算子，首先得"认识"它：叫什么名字、要几个输入几个输出、每个张量允许什么数据类型/格式、哪些输入可以不传、能在哪些芯片上跑。这些静态描述就是**算子原型（OpDef）**，写在 `*_def.cpp` 里。它不包含任何计算逻辑，却决定了算子能否被编译系统收录、被图引擎匹配。

#### 4.2.2 核心流程

`_def.cpp` 的编写套路是固定的链式声明：

```text
class XxxOp : public OpDef
  ├── this->Input("名字")        声明基础原型的输入（类型/格式/必选可选）
  ├── this->Output("名字")       声明输出
  ├── OpAICoreConfig config      构造一份 AICore 配置（可再次声明类型/格式 + 编译开关）
  └── this->AICore().AddConfig("芯片名", config)   按芯片注册
最后：OP_ADD(XxxOp) 把类注册进全局注册表
```

注意本仓库的写法是"两级声明"：`this->Input(...)` 声明通用原型，`aicore_config.Input(...)` 为 AICore 后端再声明一份。当不同芯片需要不同约束时，可以构造多个 `OpAICoreConfig` 分别 `AddConfig`。

#### 4.2.3 源码精读

[ai_infra_aggregate_hidden_def.cpp:L20-L46](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp#L20-L46) 定义了 OpDef 类并声明三个输入、一个输出。以 input 为例：

```cpp
this->Input("input")
    .ParamType(REQUIRED)                                  // 必选输入
    .DataType({ge::DT_BF16, ge::DT_FLOAT16})              // 允许 bf16 / fp16（两元素与芯片配置一一对应）
    .Format({ge::FORMAT_ND, ge::FORMAT_ND})               // ND（任意维度）排布
    .UnknownShapeFormat({ge::FORMAT_ND, ge::FORMAT_ND})
    .AutoContiguous();                                    // 自动保证连续内存
```

而 mask 是可选输入的样例（[L36-L41](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp#L36-L41)）：`ParamType(OPTIONAL)` + `DataType({ge::DT_BOOL, ge::DT_BOOL})`。

[ai_infra_aggregate_hidden_def.cpp:L48-L84](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp#L48-L84) 构造 AICore 配置并注册到两款芯片：

```cpp
OpAICoreConfig aicore_config;
aicore_config.Input("input")...;   // 为 AICore 后端重复声明每个输入输出（同上，略）

aicore_config.DynamicCompileStaticFlag(true)
    .DynamicFormatFlag(true)
    .DynamicRankSupportFlag(true)
    .DynamicShapeSupportFlag(true)     // 声明支持动态 shape
    .NeedCheckSupportFlag(false)
    .PrecisionReduceFlag(true)
    .ExtendCfgInfo("coreType.value", "AiCore")                     // 跑在 AiCore 上
    .ExtendCfgInfo("jitCompile.flag", "static_false,dynamic_false"); // JIT 编译行为开关

this->AICore().AddConfig("ascend910b", aicore_config);   // Atlas A2 类芯片
this->AICore().AddConfig("ascend910_93", aicore_config); // Atlas A3 类芯片
```

这些以 `Flag` 结尾的开关向编译系统声明算子对动态 shape、动态格式、精度降级等的支持态度，具体语义由 CANN 注册框架定义，初学阶段只需看懂"这里在声明编译行为"。

最后，[ai_infra_aggregate_hidden_def.cpp:L88](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp#L88) 一行 `OP_ADD(AiInfraAggregateHidden);` 把这个类实例化并挂入全局注册表——这就是"算子的出生证明"。头文件来源见 [L16](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp#L16) 的 `#include "register/op_def_registry.h"`（CANN 提供的注册框架头）。

#### 4.2.4 代码实践

**实践目标**：体会"芯片支持矩阵"写在 `_def.cpp` 里。

**操作步骤**：

1. 在 `ascendc/src/ops-transformer/` 下执行（源码阅读型，无需 NPU）：

   ```bash
   grep -rn "AddConfig(" ascendc/src/ops-transformer --include="*_def.cpp" | grep -v "^Binary"
   ```

2. 统计每款芯片名（如 `ascend910b`、`ascend910_93`、`ascend950`）出现的算子数量。

**需要观察的现象**：哪些芯片名出现得最多？有没有算子只注册了一款芯片？

**预期结果**：`ascend910b` 与 `ascend910_93` 是本仓库最主流的两款注册目标（与 aggregate_hidden 一致）；具体计数待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：把 mask 改成必选输入需要动哪几处？

答案：至少两处——基础原型的 `this->Input("mask").ParamType(OPTIONAL)` 改为 `REQUIRED`，以及 `aicore_config.Input("mask")` 同步修改。同时 tiling 侧 `CheckMaskValid` 中"可选变量是否存在"的分支逻辑（见 4.3.3 节）以及 aclnn 调用方传参方式都要联动，这也说明四层之间靠"原型约定"耦合。

**练习 2**：`DataType({ge::DT_BF16, ge::DT_FLOAT16})` 里为什么是两个元素的列表？

答案：列表元素与 `AddConfig` 注册的芯片配置按位置对应，每个芯片一份允许的候选类型；本算子两款芯片都允许 bf16/fp16，所以各写一份。当前实现中列表长度与 `AddConfig` 的调用数（2 次）匹配。

**练习 3**：`OP_ADD` 与 4.3 节将见到的 `IMPL_OP_OPTILING` 有何不同？

答案：`OP_ADD` 注册的是**算子原型**（叫什么、长什么样）；`IMPL_OP_OPTILING` 注册的是**tiling 实现**（怎么切）。前者面向编译系统/图引擎匹配，后者把 tiling 函数挂到算子名上供下发时调用。

### 4.3 Host 侧规划层：`_tiling.cpp`（op_host）

#### 4.3.1 概念说明

tiling 是四层中最"烧脑"的一层，因为它要同时懂算法约束、懂硬件（核数/UB 大小）、懂数据排布。aggregate_hidden 的 tiling 代码把这件事组织得很清晰，是理想的入门标本：

- **校验**：输入是否满足约束（shape、dtype、S/B/H 范围）。
- **取平台信息**：有多少 AIV/AIC 核、socVersion 是什么。
- **切分（CoreSplit）**：决定 H/B/S 各维怎么分核。
- **落盘（DoTiling）**：把切分结果写进 TilingData，设置 tilingKey、blockDim、workspace。

#### 4.3.2 核心流程

入口函数 `TilingForAiInfraAggregateHidden`（[ai_infra_aggregate_hidden_tiling.cpp:L463-L475](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L463-L475)）把工作拆给两个类：

```text
TilingForAiInfraAggregateHidden(TilingContext *context)
  ├── AHInfoParser parser(context)          // "读与算"
  │     └── ParseAndCheck(ahInfo)
  │           ├── GetOpName()        取节点名（顺便打日志用）
  │           ├── GetNpuInfo()       核数 / socVersion 校验
  │           ├── CheckInputValid() / CheckWeightValid() / CheckMaskValid() / CheckOutputValid()
  │           ├── GetTilingKey()     按输入 dtype 选 tilingKey
  │           └── CoreSplit()        计算 baseH/baseB/baseS 与 blockDim
  └── AiInfraAggregateHiddenTiling(context).DoTiling(&ahInfo)   // "写"
        ├── context_->SetBlockDim(blockDim)          告诉调度器开几个核
        ├── workSpaces[0] = 100MB                    workspace 预留
        ├── tilingData_.set_xxx(...) + SaveToBuffer  序列化 TilingData
        └── context_->SetTilingKey(tilingKey)        指向设备侧 kernel 的哪个分支
```

CoreSplit 的切分策略（[L291-L353](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L291-L353)）可以概括为：**H 优先按 4096 全载切，剩余核分给 B，B 用满后才切 S**：

- H 份数：\( \text{baseHCnt} = \lceil H / 4096 \rceil \)（4096 是 UB 全载上限，见 [L63](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L63) 的 `H_SIZE_FULL`）；
- 特判：当 `baseHCnt` 不能整除核数时向上凑（例如 48 核时把 5 份凑成 6 份），避免有空核；
- B 份数：`coreNumH = aivNum / baseHCnt` 个剩余核再分给 B；B 切完还有富余（`baseB == 1`）才切 S；
- 总核数：\[ \text{blockDim} = \text{baseHCnt} \times \text{baseBCnt} \times \text{baseSCnt} \]

#### 4.3.3 源码精读

**取平台信息与芯片校验**：[ai_infra_aggregate_hidden_tiling.cpp:L79-L102](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L79-L102) 通过 `TilingContext` 拿 `PlatformInfo`，包装成 `platform_ascendc::PlatformAscendC` 后查询 AIV/AIC 核数与 socVersion：

```cpp
auto ascendcPlatform = platform_ascendc::PlatformAscendC(platformInfo_);
aivNum_ = static_cast<int64_t>(ascendcPlatform.GetCoreNumAiv());
uint32_t aicNum = ascendcPlatform.GetCoreNumAic();
...
socVersion_ = ascendcPlatform.GetSocVersion();
if ((socVersion_ != platform_ascendc::SocVersion::ASCEND910B) &&
    (socVersion_ != platform_ascendc::SocVersion::ASCEND910_93)) {
    OP_LOGE(opName_, "SOC Version[%d] is not support.", ...);   // 运行期芯片白名单
    return ge::GRAPH_FAILED;
}
```

注意这里与 `_def.cpp` 的 `AddConfig` 形成**双层芯片适配**：`AddConfig` 是编译期决定"为哪些芯片产出 kernel"，这里 `socVersion` 检查是运行期决定"当前卡能不能跑"。

**防御式校验**：[ai_infra_aggregate_hidden_tiling.cpp:L104-L152](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L104-L152) 的 `CheckInputValid` 是本仓库 tiling 校验的标准写法——取 desc/shape、判空、查类型、查维数、查范围，全部用 `OP_CHECK_IF` + `OP_LOGE` 宏（u3-l1 会专门讲这些宏）：

```cpp
OP_CHECK_IF(((inputType_ != ge::DT_FLOAT16) && (inputType_ != ge::DT_BF16)),
    OP_LOGE(opName_, "The data types of the input must be float16 or bfloat16."),
    return ge::GRAPH_FAILED);
...
OP_CHECK_IF(sSize_ > S_SIZE_LIMIT,   // S 上限 32K，见 L58
    OP_LOGE(opName_, "the max of S size only support 32K, but now is %d.", sSize_),
    return ge::GRAPH_FAILED);
```

范围常量集中在 [L58-L63](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L58-L63)：S ≤ 32K、B ≤ 8、384 ≤ H ≤ 24576、W = 3。

**tilingKey 的选择**：[ai_infra_aggregate_hidden_tiling.cpp:L282-L289](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L282-L289) 按 dtype 二选一：默认 `AGGREGATE_HIDDEN_BF16`，输入是 fp16 则换成 `AGGREGATE_HIDDEN_HALF`。这个编号随后在设备侧被 `TILING_KEY_IS` 消费（4.4.3 节）——**tilingKey 是 Host 侧与 Device 侧之间的"分支选择信号"**。

**输出契约（DoTiling）**：[ai_infra_aggregate_hidden_tiling.cpp:L426-L460](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L426-L460) 完成四件写出：

```cpp
context_->SetBlockDim(static_cast<uint32_t>(tilingInfo->blockDim));  // 1. 开核数
workSpaces[0] = DEFAULT_WORKSPACE_SIZE;                              // 2. workspace 预留 100MB
tilingData_.set_baseH(tilingInfo->baseH); /* ...13 个字段 */          // 3. 切分参数
tilingData_.SaveToBuffer(context_->GetRawTilingData()->GetData(),
                         context_->GetRawTilingData()->GetCapacity()); // 序列化进 TilingContext
context_->SetTilingKey(tilingInfo->tilingKey);                       // 4. 分支信号
```

**tiling 函数的注册**：文件末尾 [ai_infra_aggregate_hidden_tiling.cpp:L478-L480](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L478-L480)：

```cpp
IMPL_OP_OPTILING(AiInfraAggregateHidden)
    .Tiling(TilingForAiInfraAggregateHidden)
    .TilingParse<AiInfraAggregateHiddenCompileInfo>(TilingPrepareForAiInfraAggregateHidden);
```

这一行把 tiling 入口按算子名 `AiInfraAggregateHidden`（与 `_def.cpp` 的类名一致！）挂到注册表上——**四层之间靠算子名对齐**，这是在本仓库跨层追踪代码的关键线索。

#### 4.3.4 代码实践

**实践目标**：手工推演一次 CoreSplit，验证你读懂了切分逻辑。

**操作步骤**（纸上推演，源码阅读型）：

1. 设输入 `input[S, B, H] = [1024, 4, 8192]`，设备 AIV 核数 `aivNum_ = 50`（hypothetical，用于练习）。
2. 按 [L293-L294](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L293-L294) 计算 `baseHCnt = ceil(8192/4096) = 2`。
3. 走一遍 [L316-L345](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L316-L345)：求 `baseH/baseHTail`、`coreNumH = 50/2 = 25`、`baseB = ceil(4/25) = 1`、再判断是否切 S。
4. 用 [L349-L350](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L349-L350) 算出 blockDim。

**需要观察的现象**：`baseB == 1` 时会不会触发切 S？为什么？

**预期结果**：`baseB == 1` 说明 B 维没切满，`coreNumH(25) > bSize(4)` 有富余核，于是进入切 S 分支：`baseSCnt = 25/4 = 6`，`baseS = ceil(1024/6) = 171`（近似），blockDim = 2 × 4 × 6 = 48，仍有 2 核空闲。整个推导可在纸上完成，无需环境；与真实 48/50 核设备的对齐结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 tiling 里要做 `socVersion` 运行期检查，`_def.cpp` 里不是已经 `AddConfig` 了吗？

答案：`AddConfig` 只保证"编译产物包含哪些芯片的 kernel"，不阻止算子被错误地下发到未适配的卡上。运行期检查是最后一道防线，在错误配置的环境里尽早报错（`OP_LOGE` + `GRAPH_FAILED`），而不是跑出未定义行为。

**练习 2**：`TilingData` 里为什么不直接存 `platformInfo`，而只存切分结果（baseH 等）？

答案：TilingData 会按二进制原样传到设备侧，设备侧只需要"怎么切"的结论；平台信息属于 Host 侧决策输入，传过去没有消费者，还会浪费 tiling 区带宽。`GenerateInfo`（[L355-L377](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L355-L377)）组装的 `AHTilingInfo` 是 Host 内部结构，而 `DoTiling` 只挑了切分字段写入 TilingData。

**练习 3**：如果把 `DEFAULT_WORKSPACE_SIZE` 从 100MB 改成 10MB，可能发生什么？

答案：workspace 是 GM 上的中转区。本算子实际逻辑大多在 UB/寄存器完成，预留偏大属于保守值；但若某分支确实要用 workspace 存中间结果，缩小会导致写越界或执行失败。改参数后需要跑 UT/ST 验证（待本地验证），这也说明读 tiling 代码时要留意"预留值"与"实际消耗"的区别。

### 4.4 设备侧执行层：op_kernel 与 Ascend C Kernel

#### 4.4.1 概念说明

op_kernel 是唯一运行在 NPU AI Core 上的代码，用 **Ascend C**（昇腾的设备侧 C++ 方言）编写。它看到的不是 tensor 对象，而是 GM 地址；它的"函数"以 `__global__ __aicore__` 修饰，由调度器按 blockDim 启动到多个核上。本仓库 kernel 的通用骨架是：

- **入口函数**（`op_kernel/*.cpp`）：按 `TILING_KEY_IS` 选分支、反序列化 TilingData、实例化模板 Kernel 类。
- **Kernel 模板类**（`op_kernel/*.h`）：`Init()` 绑定 GM 地址、用 `TPipe` 分配 UB 队列；`Process()` 执行 CopyIn → Compute → CopyOut 流水。

#### 4.4.2 核心流程

```text
extern "C" __global__ __aicore__ void 算子名(GM_ADDR 输入..., GM_ADDR 输出..., GM_ADDR workspace, GM_ADDR tiling)
  ├── KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_AIV_ONLY)   声明任务类型（向量核任务）
  ├── TPipe pipe                                        UB 资源管理器
  ├── TILING_KEY_IS(0)?  → Kernel<bfloat16_t>          按类型实例化模板
  │   TILING_KEY_IS(1)?  → Kernel<half>
  ├── GET_TILING_DATA_WITH_STRUCT(TilingData, var, tiling)   把 GM 里的 tiling 反序列化成结构体
  └── Kernel op(&pipe, tilingData); op.Init(地址...); op.Process();

Init:   SetGlobalBuffer 绑 GM → InitBuffer 给 inQueueX/inQueueW/outQueue/y0/y1/y2 分 UB
Process: 搬 weight 进 UB 并 Cast 成 fp32 → 双层循环 (bIdx, sIdx):
           CopyIn(x) → Cast fp32 → Compute(卷积) → CopyOut(y2)
```

#### 4.4.3 源码精读

kernel 入口只有 19 行，是全仓库最精炼的"四层汇合点"：[ai_infra_aggregate_hidden.cpp:L24-L42](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.cpp#L24-L42)

```cpp
extern "C" __global__ __aicore__ void ai_infra_aggregate_hidden(
    GM_ADDR input, GM_ADDR weight, GM_ADDR mask, GM_ADDR output, GM_ADDR workspace, GM_ADDR tiling)
{
    KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_AIV_ONLY);   // 本算子只用向量核
    TPipe pipe;
    if (TILING_KEY_IS(AGGREGATE_HIDDEN_BF16)) {       // tilingKey==0（def 里允许 bf16）
        GET_TILING_DATA_WITH_STRUCT(AiInfraAggregateHiddenTilingData, tiling_data_in, tiling);
        const AiInfraAggregateHiddenTilingData *__restrict tilingData = &tiling_data_in;
        KernelAiInfraAggregateHidden<bfloat16_t> op(&pipe, tilingData);
        op.Init(input, weight, mask, output);
        op.Process();
    } else if (TILING_KEY_IS(AGGREGATE_HIDDEN_HALF)) { // tilingKey==1（fp16）
        ... KernelAiInfraAggregateHidden<half> op(&pipe, tilingData); ...
    }
}
```

逐条对应前面几层：

- 六个 `GM_ADDR` 参数的顺序 = `_def.cpp` 声明的 input/weight/mask/output，再追加 workspace 与 tiling——**参数表就是原型的展开**。
- `AGGREGATE_HIDDEN_BF16/HALF` 的取值 0/1 定义在 [L21-L22](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.cpp#L21-L22)，与 tiling 侧 [GetTilingKey](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L282-L289) 的赋值一一对上：**Host 写 key，Device 读 key**。
- `GET_TILING_DATA_WITH_STRUCT` 把 tiling 区的二进制还原成 `AiInfraAggregateHiddenTilingData`——正是 tiling 侧 `SaveToBuffer` 序列化的那份。

模板类与两段式生命周期在 [ai_infra_aggregate_hidden.h:L23-L52](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.h#L23-L52)：类继承自切分策略基类 `KernelAiInfraAggregateHiddenCutHBS<DTYPE>`（"Cut H/B/S"），`Init` 里 `SetGlobalBuffer` 把 GM 地址绑给成员，`InitBuffer` 用 TPipe 给输入/输出队列与三个 fp32 累加缓冲（y0/y1/y2，对应窗口 W=3 的三个历史行）分 UB：

```cpp
this->pipe_->InitBuffer(this->inQueueX, NUM_TWO, this->alignBaseH * sizeof(DTYPE)); // 双缓冲输入队列
this->pipe_->InitBuffer(this->y2, this->alignBaseH * sizeof(float));                 // fp32 累加缓冲
this->pipe_->InitBuffer(this->inQueueW, NUM_TWO, NUM_THREE * this->alignBaseH * sizeof(float)); // 3 行权重
```

[ai_infra_aggregate_hidden.h:L54-L102](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.h#L54-L102) 的 `Process` 展示了典型流水：先把 weight `DataCopyPad` 进 UB 并 `Cast` 成 fp32，然后双层循环内 `CopyIn → Cast → Compute → CopyOut`（每个 bIdx 先 `InitY` 恢复跨 S 块的历史行，这正是 tiling 切 S 后正确性的保障）。注意本算子以 fp32 做累加（bf16 输入先 `Cast`），这是训练算子保精度的常见手法。

#### 4.4.4 代码实践

**实践目标**：验证"TilingData 字段 ↔ kernel 消费"的对应关系。

**操作步骤**（源码阅读型，无需 NPU）：

1. 打开 [ai_infra_aggregate_hidden_tiling.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.h)，数一数 `BEGIN_TILING_DATA_DEF...END_TILING_DATA_DEF` 之间有多少字段（应有 ifMask/sSize/bSize/hSize/baseS/baseB/baseH/各 Tail/各 Cnt 共 13 个左右）。
2. 在 [ai_infra_aggregate_hidden.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_kernel/ai_infra_aggregate_hidden.h) 与 `_common.h` 中 grep `tilingData_->`，统计 kernel 实际读了哪些字段。
3. 列出"tiling 写了但 kernel 没读"与"kernel 读了"两张清单。

**需要观察的现象**：有没有字段只被 `Process` 间接使用（通过 `InitSharedData` 存进成员）？

**预期结果**：绝大多数字段被 `InitSharedData`/`CopyIn`/`InitY` 消费；若发现未被引用的字段，说明切分信息存在冗余（可作为你日后精简 tiling 的改进点）。具体清单待本地核对。

#### 4.4.5 小练习与答案

**练习 1**：入口函数为什么用 `extern "C"`？

答案：禁止 C++ 名字修饰，保证函数符号就是 `ai_infra_aggregate_hidden`，这样调度器/加载器能按名字找到 kernel 入口。C++ 重载会生成带参数编码的名字，破坏按名加载。

**练习 2**：`TILING_KEY_IS(0)` 与 `TILING_KEY_IS(1)` 两个分支代码几乎一样，只差模板参数。为什么不写成一个分支？

答案：tilingKey 的语义是"让 Host 能精确指定 Device 走哪条编译路径"。dtype 不同导致 UB 里元素宽度不同（bf16 2 字节 / fp16 2 字节但语义不同、Cast 目标不同），模板参数化后由编译器为每个实例生成代码；分支写法让"新增一种 dtype = 新增一个 tilingKey + 一个分支"的扩展路径非常清晰（u9-l4 综合实战会用到）。

**练习 3**：`KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_AIV_ONLY)` 说明什么？

答案：声明本 kernel 是纯向量核（AIV）任务，不占用 Cube 矩阵核。对照 4.3.3 节 tiling 只取了 `GetCoreNumAiv()` 的核数——两侧自洽：调度按 AIV 核数规划，kernel 也只跑在 AIV 上。

### 4.5 对外接口层：op_api 与 aclnn 两段式接口

#### 4.5.1 概念说明

op_api 层把 op_def + op_host + op_kernel 打包成 C 风格的 aclnn 两段式接口（回顾 2.3 节）。它做三件事：把 `aclTensor*` 等接口类型转换成框架认识的原型输入、触发 shape 推导与 tiling、构造并持有 `aclOpExecutor`。本仓库带 op_api 源码的主要是 attention 家族复杂算子；**没有 op_api 源码 ≠ 没有 aclnn 接口**，这是本节要建立的第二个关键认知。

#### 4.5.2 核心流程

```text
调用方
  ├── 第一段：aclnnXxxGetWorkspaceSize(全部输入/输出 aclTensor*, 标量属性...,
  │                                     uint64_t *workspaceSize, aclOpExecutor **executor)
  │        └── 内部完成类型转换、InferShape、Tiling，产出执行器
  ├── 调用方分配 workspace 内存
  └── 第二段：aclnnXxx(workspace, workspaceSize, executor, stream)
           └── 把执行器里的计算图下发到 stream
```

#### 4.5.3 源码精读

**参数最全的真实样例**是 FA 增强版的 aclnn 头文件：[aclnn_flash_attention_score_enhance.h:L24-L56](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.h#L24-L56) 声明第一段接口——输入张量（query/queryRope/key/keyRope/value、四种 mask、sink）、可选的 `aclIntArray*`（prefix/actualSeqQLen 等）、标量属性（scaleValue/keepProb/sparseMode/...）、四个输出张量，最后两个出参正是 `workspaceSize` 与 `executor`：

```cpp
aclnnStatus aclnnFlashAttentionVarLenScoreEnhanceV5GetWorkspaceSize(
    const aclTensor *query, ..., const aclTensor *sinkOptional,
    const aclIntArray *prefixOptional, ..., double scaleValue, ..., int64_t sparseMode, ...,
    const aclTensor *softmaxMaxOut, const aclTensor *softmaxSumOut,
    const aclTensor *softmaxOutOut, const aclTensor *attentionOutOut,
    uint64_t *workspaceSize, aclOpExecutor **executor);
```

第二段则固定四个参数（[aclnn_flash_attention_score_enhance.h:L61-L65](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.h#L61-L65)）：

```cpp
aclnnStatus aclnnFlashAttentionVarLenScoreEnhanceV5(
    void *workspace, uint64_t workspaceSize, aclOpExecutor *executor, const aclrtStream stream);
```

对照一个"极简"算子的 aclnn：[docs/aclnnAiInfraAggregateHidden.md:L33-L47](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/docs/aclnnAiInfraAggregateHidden.md#L33-L47) 中 aggregate_hidden 的第一段只有 4 个 `aclTensor*`（input/weight/maskOptional/output）加 2 个出参，没有任何标量属性——aclnn 参数表完全由算子原型（`_def.cpp`）决定。

**缺失 op_api 源码时如何补齐**：torch 扩展 [npu_aggregate_hidden.cpp:L31](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/mome/aggregate_hidden/csrc/npu_aggregate_hidden.cpp#L31) 直接发起两段式调用：

```cpp
EXEC_NPU_CMD_V1(aclnnAiInfraAggregateHidden, input, weight, mask, output);
```

这个宏定义在 [ops_common.h:L965-L976](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.h#L965-L976)，它**在运行期按符号名动态解析**两段函数：

```cpp
static const auto getWorkspaceSizeFuncAddr = GetOpApiFuncAddr(#aclnn_api "GetWorkspaceSize");
static const auto opApiFuncAddr = GetOpApiFuncAddr(#aclnn_api);
TORCH_CHECK(getWorkspaceSizeFuncAddr != nullptr && opApiFuncAddr != nullptr, ...);
```

也就是说，aclnn 符号由**安装到 CANN 环境的算子包（opapi 动态库）**提供，本仓库只需保证算子包编译安装成功（u1-l4 讲 build.sh 与 run 包），torch 扩展就能按名取用。`EXEC_NPU_CMD_V1` 随后完成"调第一段 → 分配 workspace → 调第二段"的完整串联（宏体后续行，[L977 起](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.h#L977)）。FA 增强版同样有 torch 侧适配（`ops_transformer/attention/flash_attention_score_enhance/csrc/` 下），说明"有无 op_api 源码"只影响 aclnn 适配代码写在哪里，不影响调用范式。

#### 4.5.4 代码实践

**实践目标**：从 aclnn 头文件反推算子原型的输入输出与属性。

**操作步骤**（源码阅读型）：

1. 打开 [aclnn_flash_attention_score_enhance.h:L24-L56](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/op_api/aclnn_flash_attention_score_enhance.h#L24-L56)，把参数分成四类填表：必选张量（`const aclTensor*` 无 Optional 后缀）、可选张量（带 `Optional`）、属性（`double/int64_t/char*`）、输出张量（`softmaxMaxOut` 等非 const 指向语义的输入输出参数）。
2. 与 [docs/aclnnFlashAttentionVarLenScoreEnhanceV5.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/flash_attention_score_enhance/docs/aclnnFlashAttentionVarLenScoreEnhanceV5.md) 中的参数说明逐个对照（文档对每个参数给了约束说明）。
3. 回答：哪些参数会透传给 tiling？（提示：sparseMode/inputLayout 这类影响切分与分支的属性必然进入 tiling 决策，u4-l2 会讲 FA tiling 如何消费它们。）

**需要观察的现象**：可选参数在类型系统上如何与必选参数区分？

**预期结果**：仅靠命名后缀 `Optional` 区分（类型相同都是指针），可空性是文档约定而非类型约束——这也解释了 4.3.3 节 tiling 侧为什么总要判空。

#### 4.5.5 小练习与答案

**练习 1**：为什么第一段接口不接收 `stream`，第二段才接收？

答案：第一段做规划（shape 推导、tiling、构造 executor），不向设备提交工作，与流无关；第二段把任务异步提交到指定 stream，必须知道流。拆开还允许一个 executor 的规划结果被复用提交。

**练习 2**：`aclOpExecutor **executor` 为什么是二级指针？

答案：它是出参：接口内部创建 executor 对象并把地址写回 `*executor`，调用方随后在第二段把这个句柄传回去。`workspaceSize` 同理（`uint64_t *`）。

**练习 3**：aggregate_hidden 的 aclnn 第一段连一个标量属性都没有，而 FA 有十几个。属性多寡由什么决定？

答案：由算法语义决定，最终由 `_def.cpp` 的原型（Attr 声明，FA 的 def 中有大量属性）与算子功能决定：卷积窗口 W 固定为 3 写死在约束里，而 FA 的 scale/dropout/layout/sparse 模式都是调用方可配置的语义参数。

## 5. 综合实践

**任务**：追踪一次 `aclnnAiInfraAggregateHidden(input, weight, mask, output)` 调用在四层中的完整旅程，产出一张标注版时序图。

设定输入：`input` shape `[S, B, H] = [1024, 4, 8192]`、dtype `DT_BF16`；`weight` shape `[3, 8192]`；`mask` 传入 `[4, 1024]` 的 bool 张量。

**操作步骤**：

1. **op_api 层**：确认该算子无 op_api 源码，写出它的两段式原型（从 [docs/aclnnAiInfraAggregateHidden.md:L33-L47](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/docs/aclnnAiInfraAggregateHidden.md#L33-L47) 抄录），并指出 torch 扩展用哪个宏、在哪一行发起调用。
2. **op_host 层**：依次回答——(a) `CheckInputValid` 是否全部通过（对照 [L58-L63](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L58-L63) 的五个限制常量）；(b) `GetTilingKey` 返回多少（dtype 是 BF16）；(c) 按 4.3.4 的方法算出 baseHCnt/baseB/baseSCnt/blockDim；(d) `DoTiling` 写出了哪四样东西。
3. **op_def 层**：说明框架凭哪个注册表项匹配到该算子，它为哪两款芯片注册了配置。
4. **op_kernel 层**：指出入口走 `TILING_KEY_IS` 的哪个分支、模板参数是什么、`Init`/`Process` 各做什么、`InitY` 为什么对切了 S 的场景必不可少。
5. 把以上结论画成一张文本时序图（参考 4.1.2 的格式，在每步标注对应源码文件与行号）。

**需要观察的现象/预期结果**：图中每一步都能落到一个真实文件的具体行号上；tilingKey 应为 0（`AGGREGATE_HIDDEN_BF16`），kernel 分支走 `KernelAiInfraAggregateHidden<bfloat16_t>`。若你在有 NPU 的环境中想实际验证，需先完成 u1-l3/u1-l4 的环境搭建与算子包安装，再通过 torch 扩展调用并抓取 `OP_LOGD` 的 tiling dump 日志（[tiling.cpp:L379-L396](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_tiling.cpp#L379-L396)）与你推演的切分值对照——日志实值待本地验证。

## 6. 本讲小结

- 一个 Ascend C 算子由四层组成：**op_def**（原型注册，`_def.cpp` + `OP_ADD`）、**op_host**（Host 侧校验与 Tiling，`_tiling.cpp` + `IMPL_OP_OPTILING`）、**op_kernel**（设备侧 Ascend C Kernel，`extern "C" __global__ __aicore__` 入口 + 模板类）、**op_api**（aclnn 两段式接口）。
- **四层靠算子名对齐**：`_def.cpp` 的类名、`IMPL_OP_OPTILING` 的名字、kernel 入口函数名、aclnn 符号名共享同一命名链路，这是跨层追踪代码的钥匙。
- **tilingKey 是 Host 与 Device 之间的分支信号**：Host 在 `GetTilingKey`/`SetTilingKey` 写入，Device 在 `TILING_KEY_IS` 读取；TilingData 经 `SaveToBuffer` 序列化、`GET_TILING_DATA_WITH_STRUCT` 反序列化。
- 芯片适配是**双层**的：`_def.cpp` 的 `AICore().AddConfig` 编译期注册 + tiling 的 `socVersion` 运行期白名单。
- 本仓库很多算子**没有 op_api 源码**：aclnn 接口由安装的算子包在运行期提供符号，torch_ops_extension 的 `EXEC_NPU_CMD_V1` 宏按名动态解析并串起两段调用。
- aggregate_hidden 是四层结构的最佳入门标本：入口仅 19 行，tiling 类职责分明（Parser 读算、Tiling 写出）。

## 7. 下一步学习建议

- 下一讲 **u1-l3（开发环境搭建）** 与 **u1-l4（编译与安装）**：把本讲读到的四层代码真正编译成 run 包装进 CANN 环境，亲手验证 aclnn 符号从何而来。
- 若想先把 aggregate_hidden 吃透，直接进入第二单元：**u2-l1（从文档读懂算子）** 会展开本讲引用的 docs 文档中的公式与约束；**u2-l2~u2-l4** 将分别精读本讲只是概览的 def/tiling/kernel 三层。
- 对本讲 4.5 节"动态符号解析"意犹未尽的读者，可提前浏览 `ascendc/torch_ops_extension/omni_training_custom_ops/csrc_base/ops_common.h` 的完整宏体（u6 单元会系统讲解 torch 扩展）。
- 对照阅读建议：拿 `attention/lightning_indexer_enhance`（四层齐全、含 `_proto.cpp` 与 op_api 实现）与 aggregate_hidden 比对，感受"简单算子"与"复杂算子"在四层上的体量差异。
