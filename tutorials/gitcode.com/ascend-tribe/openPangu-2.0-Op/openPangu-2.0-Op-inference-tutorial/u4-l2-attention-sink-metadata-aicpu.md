# u4-l2 AICPU 算子特例：AttentionSink Metadata

## 1. 本讲目标

上一讲（u4-l1）我们解剖了仓库中体量最大的 AICore 算子 FusedInferAttentionSink（FIA Sink）。本讲把镜头转向它的「影子搭档」——`ai_infra_fused_infer_attention_sink_metadata`，仓库中仅有的两个 AICPU 算子之一。它不走 AICore，而是跑在昇腾设备的通用 CPU 核上，用纯 C++ 计算出主算子的多核调度方案。

学完本讲，你应该能够：

1. 说出 AICPU 算子与 AICore 算子在目录结构、原型注册方式、实现语言、构建产物上的全部关键差异。
2. 读懂 `op_graph/*_proto.h` 的 `REG_OP` 流式原型声明，并理解它与 `op_host/*_def.cpp` 的 OpDef 是「同一职责、两套写法」。
3. 读懂 `op_kernel_aicpu` 下的 CpuKernel 实现：它如何直接读取设备上的张量数值、如何用代价模型做多核切分、如何把结果按结构体覆盖写进输出张量。
4. 理解 `*_aicpu.json` 中 `engine`/`kernelSo` 等字段的含义，以及 AICPU kernel 被交叉编译成 ARM 动态库 `libtransformer_aicpu_kernels.so` 的完整构建链。
5. 掌握「元数据算子（AICPU）生产 → 主算子（AICore）消费」的协作模式，包括两侧共享同一份头文件的单一事实源设计和双流 + event 的同步方式。

## 2. 前置知识

### 2.1 昇腾芯片上不只有 AI Core

在第 2 单元我们反复强调 AI Core 的两级内存（GM/UB）、AIV 向量核与 AIC 立方核。但一颗昇腾芯片上除了 AI Core 阵列，还有一个**通用 CPU 核**（aarch64 架构，类似一颗 ARM CPU），在 CANN 术语中称为 AI CPU。它：

- 不能跑 AscendC 的向量/立方指令，但能跑标准 C++ 代码；
- 能像普通程序一样访问设备上的张量数据（拿到的是裸指针）；
- 适合做「逻辑复杂、数据量小、控制流多」的工作。

CANN 允许自定义算子声明为 AICPU 类型，此时算子的计算体不再编译成 AscendC kernel，而是编译成一份 ARM 共享库，由设备的 CPU 核执行。本讲的 metadata 算子正是这样一类特例。

### 2.2 为什么分核调度要搬到 AICPU

回忆 u2-l3：普通 AICore 算子的 Tiling 在 Host 侧完成，主要依据是 shape 和属性。但 FIA Sink 的负载均衡有一个特殊需求——`actual_seq_lengths_kv`（每个 batch 的真实 KV 序列长度）是**张量数值**，不是 shape。要按「每个 batch 的真实长度」算每个核分多少活，就必须读到这些 int64 的值。

把这件事放到 Host 侧做，需要把设备数据拷回主机，破坏流式执行；放到 AICPU 做，算子自己就能读张量数据，算完直接把「施工图」写在设备内存里，主算子的 AICore kernel 开工时照着读即可。文档对它的定位一句话讲清：

> 为 FusedInferAttentionSink 算子生成元数据（tiling 信息），用于后续 attention 计算的多核调度、内存管理和计算策略决策。

见 [docs/npu_fused_infer_attention_sink_metadata.md:L12-L16](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink_metadata/docs/npu_fused_infer_attention_sink_metadata.md#L12-L16)。

### 2.3 需要回顾的前置概念

- u2-l1 的 OpDef（`op_host/*_def.cpp`）与 u2-l3 的 TilingBaseClass 七步框架——本讲要反复与它们对照。
- u2-l2 的 aclnn 两段式接口与 `ADD_TO_LAUNCHER_LIST_AICORE`——本讲会见到 AICPU 变体。
- u3-l2 的 `EXEC_NPU_CMD_V1`——metadata 算子在 PyTorch 侧的调用同样经过它。
- u4-l1 的 FIA Sink 主算子——metadata 的唯一消费者。
- 术语：**aclgraph**，即把一串算子调用捕获成整图执行的模式（类似 CUDA Graph）。文档声明本算子「支持 aclgraph 入图」，这正是它做成独立算子而非框架内部步骤的原因之一。

## 3. 本讲源码地图

| 文件 | 层 | 作用 |
| --- | --- | --- |
| `ai_infra_fused_infer_attention_sink_metadata/op_graph/ai_infra_fused_infer_attention_sink_metadata_proto.h` | 原型 | 用 `ge::REG_OP` 声明算子输入/输出/属性（替代 AICore 算子的 `*_def.cpp`） |
| `ai_infra_fused_infer_attention_sink_metadata/op_kernel_aicpu/ai_infra_fused_infer_attention_sink_metadata_aicpu.h` | 计算层 | CpuKernel 类声明 + 分核算法的数据结构（SplitResult/BaseInfo 等） |
| `ai_infra_fused_infer_attention_sink_metadata/op_kernel_aicpu/ai_infra_fused_infer_attention_sink_metadata_aicpu.cpp` | 计算层 | CpuKernel 实现：读数据 → 校验 → 分核 → 写 metadata，约 2000 行 |
| `ai_infra_fused_infer_attention_sink_metadata/op_kernel_aicpu/ai_infra_fused_infer_attention_sink_metadata_aicpu.json` | 计算层 | 告诉运行时该算子由哪个引擎、哪个 so 实现 |
| `ai_infra_fused_infer_attention_sink_metadata/op_api/l0_ai_infra_fused_infer_attention_sink_metadata.cpp` | 接口层 | L0 封装：把算子登记进 executor 的 AICPU 下发列表 |
| `ai_infra_fused_infer_attention_sink_metadata/op_api/aclnn_ai_infra_fused_infer_attention_sink_metadata.cpp` | 接口层 | aclnn 两段式接口 V1（另有 `_v2.cpp` 变体） |
| `ai_infra_fused_infer_attention_sink_metadata/op_host/ai_infra_fused_infer_attention_sink_metadata_infershape.cpp` | 原型层 | InferShape：输出固定 1024 个 int32 |
| `ai_infra_fused_infer_attention_sink_metadata/CMakeLists.txt` | 构建 | 调 `add_modules_sources_aicpu` 收集各层源码 |
| `ai_infra_fused_infer_attention_sink_metadata/config.ini` | 构建 | 门禁识别芯片版本（ascend910b / ascend910_93） |
| `ai_infra_fused_infer_attention_sink/op_kernel/ai_infra_fused_infer_attention_sink_metadata.h` | 共享契约 | **主算子目录下**的元数据布局定义，两侧共同 include |
| `attention/common/aicpu/cpu_context_util.h` | 工具 | `GetAttrValue/GetAttrValueOpt` 属性读取辅助 |
| `ascendc/cmake/func.cmake` | 构建 | `add_modules_sources_aicpu` 宏、ARM 交叉编译与 json 合并逻辑 |
| `ai_infra_fused_infer_attention_sink_metadata/tests/st/test_npu_fused_infer_attention_sink_metadata.py` | 测试 | 双流 + event 的生产者-消费者真实示范 |

> 注意两点目录差异：一是 AICPU 算子没有 `op_kernel/`，取而代之的是 `op_kernel_aicpu/`；二是没有 `op_host/*_def.cpp` 与任何 `*_tiling.cpp`，原型声明挪到了 `op_graph/` 下。仓库中另一个同构的 AICPU 算子是 `ai_infra_sparse_flash_attn_metadata`（u4-l3 会再遇到它）。

## 4. 核心概念与源码讲解

### 4.1 op_graph：用 REG_OP 声明 AICPU 算子原型

#### 4.1.1 概念说明

AICore 算子的原型（签名）由 `op_host/*_def.cpp` 中继承 `OpDef` 的类声明，经 `OP_ADD` 宏静态注册（u2-l1）。AICPU 算子改用另一套更古老的注册体系：在 `op_graph/` 目录放一个 `*_proto.h`，用 GE（Graph Engine）命名空间的 `REG_OP` 流式接口描述算子原型。两者职责相同——告诉框架「这个算子叫什么、有哪些输入输出和属性」——但写法、注册通道、下游消费者都不同。

#### 4.1.2 核心流程

```text
REG_OP(算子名)
    .OPTIONAL_INPUT(输入名, TensorType({允许的dtype}))
    .OUTPUT(输出名, TensorType({允许的dtype}))
    .REQUIRED_ATTR(属性名, 类型)          // 必选属性，无默认值
    .ATTR(属性名, 类型, 默认值)            // 可选属性，带默认值
    .OP_END_FACTORY_REG(算子名)
```

CMake 侧由 `add_modules_sources_aicpu` 宏把 `op_graph/*_proto*.h` 收集进 `${GRAPH_PLUGIN_NAME}_proto_headers` 目标（见 [func.cmake:L1035-L1038](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/func.cmake#L1035-L1038)），而不是 OpDef 那条编进 `cust_opsproto_rt2.0.so` 的通道。宏里甚至有一条保护：若 AICPU 算子目录里出现了 `*_def*.cpp` 又没有显式声明 OPTYPE，直接报致命错误——见 [func.cmake:L1023-L1032](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/func.cmake#L1023-L1032)，等于在构建系统层面禁止两种原型写法混用。

#### 4.1.3 源码精读

整个原型声明只有 20 余行：[ai_infra_fused_infer_attention_sink_metadata_proto.h:L23-L45](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink_metadata/op_graph/ai_infra_fused_infer_attention_sink_metadata_proto.h#L23-L45)

```cpp
REG_OP(AiInfraFusedInferAttentionSinkMetadata)
    .OPTIONAL_INPUT(actual_seq_lengths_q, TensorType({DT_INT64}))
    .OPTIONAL_INPUT(actual_seq_lengths_kv, TensorType({DT_INT64}))
    .OUTPUT(metaData_out, TensorType({DT_UINT32}))
    .REQUIRED_ATTR(num_heads_q, Int)
    ...
    .ATTR(input_layout, String, "TND")
    .ATTR(sink_num, Int, 0)
    .REQUIRED_ATTR(soc_version, String)
    .REQUIRED_ATTR(aic_core_num, Int)
    .REQUIRED_ATTR(aiv_core_num, Int)
    .OP_END_FACTORY_REG(AiInfraFusedInferAttentionSinkMetadata)
```

逐段解读：

- **输入只有两个且都是可选的**：`actual_seq_lengths_q/kv`。对比主算子 FIA Sink 的 3 必选 + 27 可选输入，这个算子根本不接收 Q/KV 张量——它不看数据内容（除序列长度外），只看「形状参数 + 调度参数」。
- **输出只有一个** `metaData_out`（DT_UINT32），即那张「施工图」。
- **属性里有三个特殊的必选属性**：`soc_version`、`aic_core_num`、`aiv_core_num`。普通 AICore 算子从来不需要用户告诉它核数——Host 侧 Tiling 框架会通过 `GetPlatformInfo` 自动拿（u2-l3）。而 AICPU 算子不走 Tiling 框架，平台信息只能当作属性显式传入（4.4 节会看到 aclnn 层如何自动填）。
- 命名对齐：`REG_OP(AiInfraFusedInferAttentionSinkMetadata)` 这个字符串是全链路的钥匙——它必须与 AICPU kernel 里 `REGISTER_CPU_KERNEL` 的字符串、json 的键完全一致（见 4.2.3 与 4.3.3），这是 u3-l4「多次命名对齐」结论在 AICPU 算子上的再现。

#### 4.1.4 代码实践

**目标**：把 REG_OP 声明翻译成与 u2-l1 OpDef 同风格的「参数表」，体会两套写法的对应关系。

**步骤**：

1. 打开上文的 proto.h 全文，准备三张空表：输入表、输出表、属性表。
2. 输入表列为「名称 / 必选性 / dtype」；属性表列为「名称 / 类型 / 默认值」。
3. 对照 `ai_infra_scatter_block_update` 的 def 文件（u2-l1 精读过），写出 scatter 的同款三张表。
4. 在两张属性表里分别圈出「平台/硬件相关」的属性。

**需要观察的现象**：scatter 的属性全部是业务语义（索引数、批大小等）；metadata 的属性里混入了 `soc_version/aic_core_num/aiv_core_num` 三个平台属性，且全部 REQUIRED。

**预期结果**：两张表的输入输出行数都很少，属性表 metadata 有 20 行（4 + 3 个必选、13 个可选带默认值）。REG_OP 的 `.ATTR(x, T, d)` 与 OpDef 的属性默认值语义一一对应；`.OPTIONAL_INPUT` 对应 OpDef 的 `ParamType::OPTIONAL`。本实践为纯源码阅读，结论可直接从两份文件验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `actual_seq_lengths_q/kv` 声明为 OPTIONAL_INPUT，而 `num_heads_q` 等却是 REQUIRED_ATTR？

**答案**：序列长度张量在 BSH/BSND 等 padding 布局下可以不传（此时用 `batch_size` 与最大长度推），所以是可选输入；而 head 数、head 维度没有合理默认值，缺了无法计算分核，所以是必选属性。aclnn 层的 `ParamsCheck` 与 kernel 层的 `CheckSingleParaInputLayout` 都会按布局校验这个约束（TND/NTD 布局下序列长度张量必须传）。

**练习 2**：`REG_OP` 声明的 `metaData_out` 是 DT_UINT32，但后文 infershape 写的是 DT_INT32，这是矛盾吗？

**答案**：不算功能性矛盾，但是一处命名不一致（u1-l3 说过「文档/声明可能滞后，以代码为准」）。两者都是 4 字节整型；共享头文件里 `FIASINK_METADATA_T = int32_t` 而结构体字段用 `uint32_t`，csrc 层用 `at::ScalarType::Int` 分配。宽度一致所以运行无误，但阅读时要意识到同一张 tensor 在不同层有 uint32/int32 两种称谓。

**练习 3**：如果把 AICPU 算子的 proto.h 误删，构建会在哪一步失败？

**答案**：不会在 `add_modules_sources_aicpu` 的 proto 收集处失败（那只是 `file(GLOB)` 后的 `if` 判断，缺文件就跳过），而是要到下游依赖算子原型符号的编译环节（如 infershape 的 `IMPL_OP_INFERSHAPE(AiInfraFusedInferAttentionSinkMetadata)` 找不到原型声明）或打包校验时暴露。这提示 GLOB 式收集是「宽松收集、延迟报错」。

### 4.2 op_kernel_aicpu：跑在设备 CPU 上的纯 C++ kernel

#### 4.2.1 概念说明

`op_kernel_aicpu/ai_infra_fused_infer_attention_sink_metadata_aicpu.cpp` 是一个约 2000 行的**标准 C++ 程序**：没有 `__global__ __aicore__`、没有 TPipe/TQue、没有 GET_TILING_DATA。它继承 CANN AICPU 框架的 `CpuKernel` 基类，实现一个 `Compute(CpuKernelContext &ctx)` 入口，通过 `REGISTER_CPU_KERNEL` 注册。

与 AICore kernel 的本质区别有三条：

1. **没有 Tiling**。目录里没有任何 tiling 文件，CMake 宏检测不到 tiling 源码时会生成一个空的 `optiling_stub.cpp` 占位（[func.cmake:L972-L986](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/func.cmake#L972-L986)）。这个算子的产物不是「被 TilingData 指挥的 kernel」，而是**替别人算 TilingData 的 kernel**。
2. **能直接读张量数值**。`ctx.Input(...)->GetData()` 返回裸指针，kernel 用 `static_cast<const int64_t*>` 解引用读取每个 batch 的真实序列长度——这是 Host 侧 Tiling 做不到或不方便做的事。
3. **输出是结构体覆盖写**。它把输出张量的内存直接 `reinterpret` 成 `FiaSinkMetaData*` 结构体逐字段填值，而不是逐元素计算。

#### 4.2.2 核心流程

`Compute` 的主流程（对照源码 [ai_infra_fused_infer_attention_sink_metadata_aicpu.cpp:L24-L60](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink_metadata/op_kernel_aicpu/ai_infra_fused_infer_attention_sink_metadata_aicpu.cpp#L24-L60)）：

```text
Compute(ctx)
 ├─ Prepare(ctx)            # 取输入/输出张量句柄，读 4 必选 + 15 可选属性
 │    ├─ ParamsParse()      # 单参数校验 + 轴信息解析（B/S1/S2/G/N2 从哪来）
 │    └─ ParamsCheck()      # 特性校验 + 多参数一致性校验（sink/layout/seq 配合）
 ├─ CheckIsMla()            # rope_head_dim=64 且 D=512 且 N2=1 时走 MLA 分支
 ├─ CalcInnerSizeMla/Gqa()  # 定 S2 方向基本块 sInnerSize_ 与 M 方向 mBaseSize_
 ├─ CreateSplitInputMla/Gqa # 组装 BaseInfo/SplitParam（含读取序列长度数值）
 ├─ SplitCore(...)          # 代价模型 + 贪心分配，得多核切分方案 SplitResult
 └─ GenMetaData(res)        # 把 SplitResult 逐字段写进输出张量（结构体覆盖）
```

其中最核心的是 `SplitCore` 的搜索框架（[L1974-L2017](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink_metadata/op_kernel_aicpu/ai_infra_fused_infer_attention_sink_metadata_aicpu.cpp#L1974-L2017)）：

1. 先按 `mBaseSize/s2BaseSize` 把每个 batch 切成基本块，统计总块数与总代价；
2. 核数候选区间取 \(\ [\sqrt{totalBlockNum},\ \min(物理核数,\ totalBlockNum)]\ \)；
3. 对每个候选核数 `i` 调 `CalcSplitPlan` 做一次「按 batch → 按行 → 按块 → 强制兜底」的四级贪心分配，保留 `maxCost`（最慢核开销）最小的方案；
4. 若产生了 FlashDecode 归约任务，再调 `SplitFD` 把归约负载摊到向量核。

单块代价用线性模型估计（[L1240-L1247](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink_metadata/op_kernel_aicpu/ai_infra_fused_infer_attention_sink_metadata_aicpu.cpp#L1240-L1247)）：

\[
\text{cost}(M, S_2) = 6\left\lceil \frac{M}{16} \right\rceil + 10\left\lceil \frac{S_2}{64} \right\rceil
\]

即 M 轴按 16 对齐的代价系数是 6，S2 轴按 64 对齐的系数是 10——两个经验系数刻画「行方向的 matmul 开销」与「列方向的访存开销」的相对权重。

#### 4.2.3 源码精读

**入口与注册**。类声明与 `Compute` 覆写见 [ai_infra_fused_infer_attention_sink_metadata_aicpu.h:L241-L245](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink_metadata/op_kernel_aicpu/ai_infra_fused_infer_attention_sink_metadata_aicpu.h#L241-L245)；文件末尾一行完成注册：[ai_infra_fused_infer_attention_sink_metadata_aicpu.cpp:L2019](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink_metadata/op_kernel_aicpu/ai_infra_fused_infer_attention_sink_metadata_aicpu.cpp#L2019)

```cpp
REGISTER_CPU_KERNEL(AI_INFRA_FUSED_INFER_ATTENTION_SINK_METADATA, AiInfraFusedInferAttentionSinkMetadataKernel);
```

这个字符串常量与 REG_OP 名、json 键三方对齐。

**直接读取张量数值**。`CreateSplitInputMla` 中把 `actual_seq_lengths` 的 int64 数据逐个搬进 `BaseInfo`：[L233-L248](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink_metadata/op_kernel_aicpu/ai_infra_fused_infer_attention_sink_metadata_aicpu.cpp#L233-L248)

```cpp
const int64_t *s1Ptr = static_cast<const int64_t *>(actualSeqLengths_->GetData());
for (uint32_t i = 0; i < bSize; ++i) {
    baseInfo.actualSeqS1Size.emplace_back(s1Ptr[i]);   // 逐 batch 读真实长度
}
```

这正是「AICPU 才能做的事」的实证：分核算法的输入不是 shape，而是每个 batch 的真实 token 数。

**基本块大小的一堆经验值**。`CalcInnerSizeMla`（[L109-L169](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink_metadata/op_kernel_aicpu/ai_infra_fused_infer_attention_sink_metadata_aicpu.cpp#L109-L169)）里 `sInnerSize_` 在 512/256/128 之间按 batchInvariant、BAND 稀疏模式、sink 数量、SWA 窗口、FlashDecode、PageAttention blockSize 对齐等近十种条件调整，每处都有中文注释解释动机（如「将基本块大小固定为 256，确保不同 shape 下 reduceSum 累加序的一致性」）。读不懂某个分支时，注释就是最好的文档。

**结构体覆盖写输出**。`GenMetaData`（[L62-L107](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink_metadata/op_kernel_aicpu/ai_infra_fused_infer_attention_sink_metadata_aicpu.cpp#L62-L107)）：

```cpp
optiling::detail::FiaSinkMetaData *metaDataPtr =
    (optiling::detail::FiaSinkMetaData *)metaData_->GetData();     // 输出张量内存直接当结构体
metaDataPtr->aicMetadata[i][BN2_END_PTR_INDEX] = splitRes.bN2End[i];  // 每核的 BN2 结束点
...
metaDataPtr->baseMetadata[USED_CORE_NUM_INDEX] = splitRes.usedCoreNum; // 全局信息
```

`FiaSinkMetaData` 的布局定义在**主算子目录**的共享头文件里（4.5 节展开），本文件第 13 行直接相对路径 include 进来：[ai_infra_fused_infer_attention_sink_metadata_aicpu.cpp:L13-L15](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink_metadata/op_kernel_aicpu/ai_infra_fused_infer_attention_sink_metadata_aicpu.cpp#L13-L15)

```cpp
#include "../../ai_infra_fused_infer_attention_sink/op_kernel/ai_infra_fused_infer_attention_sink_metadata.h"
#include "../../common/aicpu/cpu_context_util.h"
```

属性读取用的小工具 `GetAttrValue/GetAttrValueOpt` 定义在 [cpu_context_util.h:L31-L76](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/common/aicpu/cpu_context_util.h#L31-L76)：必选版读不到就报错返回 false，可选版读不到就保留成员默认值（如 `aicCoreNum_ = 24U`、`inputLayout_ = "TND"`，见 [ai_infra_fused_infer_attention_sink_metadata_aicpu.h:L351-L368](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink_metadata/op_kernel_aicpu/ai_infra_fused_infer_attention_sink_metadata_aicpu.h#L351-L368)）——这与 aclnn 层「必选属性缺失直接失败」的表现不同层呼应。

**json 描述文件**。同目录的 [ai_infra_fused_infer_attention_sink_metadata_aicpu.json:L1-L15](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink_metadata/op_kernel_aicpu/ai_infra_fused_infer_attention_sink_metadata_aicpu.json#L1-L15) 只有一个键（算子名）和一段 `opInfo`：

| 字段 | 值 | 含义 |
| --- | --- | --- |
| `engine` | `DNN_VM_AICPU` | 该算子调度到 AICPU 引擎执行（而非 AICore） |
| `kernelSo` | `libtransformer_aicpu_kernels.so` | 实现所在的动态库（4.3 节的构建产物） |
| `opKernelLib` | `CUSTAICPUKernel` | 算子库归类：自定义 AICPU kernel |
| `functionName` | `RunCpuKernel` | 库内统一入口，由 CANN 按算子名分发到 `REGISTER_CPU_KERNEL` 的实现 |
| `flagAsync` | `False` | 标记为非异步执行模式 |
| `userDefined` | `True` | 用户自定义算子 |
| `computeCost` / `workspaceSize` | `100` / `100` | 交给调度器的参考量（具体调度语义以 CANN 官方文档为准，待确认） |

**构建侧**。算子根 [CMakeLists.txt:L9-L16](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink_metadata/CMakeLists.txt#L9-L16) 只有一句关键调用 `add_modules_sources_aicpu(DEPENDENCIES ai_infra_fused_infer_attention_sink)`——DEPENDENCIES 声明对主算子的依赖（因为 include 了它的头文件，必须保证主算子目录先被处理）。宏内部用 `file(GLOB op_kernel_aicpu/*_aicpu*.cpp)` 收集 kernel 源码（[func.cmake:L989-L998](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/cmake/func.cmake#L989-L998)）。json 的合并与 so 的链接在 4.3 展开。

#### 4.2.4 代码实践

**目标**：不依赖硬件，手工推演一次 `GenMetaData` 写出的内存布局，建立「输出张量 = 结构体」的具体手感。

**步骤**：

1. 打开共享头文件 [ai_infra_fused_infer_attention_sink_metadata.h:L24-L32](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/ai_infra_fused_infer_attention_sink_metadata.h#L24-L32)，记下三个尺寸常量：`AIC_CORE_NUM=36`、`AIV_CORE_NUM=72`、`FIASINK_META_SIZE=1024`。
2. 计算结构体三段区域的下标区间：
   - AIC 区：\( 36 \times 10 = 360 \) 个 uint32，下标 \([0, 360)\)；
   - AIV 区：\( 72 \times 3 = 216 \) 个，下标 \([360, 576)\)；
   - BASE 区：10 个，下标 \([576, 586)\)。
3. 假设某次运行 `usedCoreNum = 5`、`cvRatio = 2`，回答：`GenMetaData` 会写 AIC 区的前几行？AIV 区的前几行？`baseMetadata[USED_CORE_NUM_INDEX]` 的绝对下标是多少？
4. 用头文件里的 `static_assert`（[L97](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/ai_infra_fused_infer_attention_sink_metadata.h#L97)）验证结构体总大小不超过 1024 × 4 字节。

**需要观察的现象**：586 个 uint32 只占输出张量 1024 个元素的一半多一点，尾部留白；`aicMetadata[i][...]` 的写入循环上界是 `usedCoreNum` 而非 36，未使用的核的行不会被写。

**预期结果**：步骤 3 的答案是——写 AIC 区第 0~4 行（5 行）、AIV 区第 0~9 行（`usedCoreNum * 2 = 10` 行）、BASE 区全 10 项；`USED_CORE_NUM_INDEX` 绝对下标 = 576 + 4 = 580（用 [L81-L87](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/ai_infra_fused_infer_attention_sink_metadata.h#L81-L87) 的 `GetBaseMetaAbsIndex` 公式 \(360 + 216 + 4 = 580\)）。以上为纯源码推导；若在真机运行 docs 示例并 `print(meta_data[580])` 应得到 usedCoreNum，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：AICPU kernel 里为什么可以写 `s1Ptr[i]` 直接解引用设备张量，而 AscendC kernel 读 GM 数据必须走 `DataCopy/GlobalTensor`？

**答案**：AICPU kernel 运行在设备的通用 CPU 核上，`GetData()` 返回的地址对它而言就是可直接访问的内存（经 AICPU 运行时映射），所以能当普通指针用；AscendC kernel 运行在 AI Core 上，GM 与 UB 是两级分离的内存，必须经 MTE 通道搬运（u2-l4）。执行引擎决定了访问数据的方式。

**练习 2**：`Compute` 开头为什么有 `if (s2Size == 0) { s2Size = 1024; }`（[L32-L39](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink_metadata/op_kernel_aicpu/ai_infra_fused_infer_attention_sink_metadata_aicpu.cpp#L32-L39)）？

**答案**：空 tensor（KV 长度为 0）的退化场景。若按 0 参与后续 `CalcInnerSize/SplitCore`，除法与块数计算会产生 0 块/除零等异常，影响 matmul/softmax 的 tiling 推导；注释明确说明「作为默认值完成后续计算，kernel 计算使用真实的 seqSize=0」。这是一种「哨兵值保护计算、真实值另行传递」的惯用法。

**练习 3**：`SplitCore` 为什么要从 \(\sqrt{totalBlockNum}\) 开始枚举核数，而不是直接用满全部物理核？

**答案**：块数少的时候开满核反而劣化——每核至少分一块，核间同步与 FD 归约的开销会超过并行收益；枚举区间下界取 \(\sqrt{N}\) 是「块数开方」这个经典负载均衡启发式的近似（源码 [L1996-L1998](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink_metadata/op_kernel_aicpu/ai_infra_fused_infer_attention_sink_metadata_aicpu.cpp#L1996-L1998) 用 `sqrt(totalBlockNum + 0.25f) + 0.5f` 实现四舍五入）。对每个候选核数算一遍贪心方案再取 maxCost 最小者，代价可接受（块数上限有限）而收益是负载更均衡。

### 4.3 l0 接口与 aclnn：AICPU 算子的两段式调用

#### 4.3.1 概念说明

AICPU 算子的 op_api 层与 AICore 算子（u2-l2）结构完全同构：`aclnn_*.cpp`（对外两段式接口）+ `l0_*.cpp`（L0 封装），且同样编进 `cust_opapi.so`。唯一分叉点在 L0 这一层：AICore 算子用 `ADD_TO_LAUNCHER_LIST_AICORE` 把算子挂进 AICore 下发列表，AICPU 算子用 `ADD_TO_LAUNCHER_LIST_AICPU` 挂进 AICPU 通道——executor 落到设备时会按 proto 的引擎声明（json 的 `engine` 字段）把它路由到 CPU 核。

#### 4.3.2 核心流程

从 PyTorch 侧到 AICPU kernel 的完整链：

```text
torch.ops.custom._npu_fused_infer_attention_sink_metadata(...)   # Python
  └─ csrc: 构造 1024×int32 输出张量 → EXEC_NPU_CMD_V1            # wheel 包
       └─ aclnnAiInfraFusedInferAttentionSinkMetadataGetWorkspaceSize
            ├─ 读 PlatformInfo（SOC 版本、AIC/AIV 核数），钳制用户传参
            └─ l0op::AiInfraFusedInferAttentionSinkMetadata(...)
                 └─ ADD_TO_LAUNCHER_LIST_AICPU(...)               # 登记进 executor
  └─ aclnnAiInfraFusedInferAttentionSinkMetadata(workspace,...)   # 第二段：按流执行
       └─ 运行时按 json 路由 → libtransformer_aicpu_kernels.so
            └─ RunCpuKernel → REGISTER_CPU_KERNEL 注册的 Compute  # 设备 CPU 核
```

#### 4.3.3 源码精读

**L0 封装**：[l0_ai_infra_fused_infer_attention_sink_metadata.cpp:L27-L93](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink_metadata/op_api/l0_ai_infra_fused_infer_attention_sink_metadata.cpp#L27-L93)。第一行 `OP_TYPE_REGISTER(AiInfraFusedInferAttentionSinkMetadata)` 注册算子类型；核心是这段（[L75-L91](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink_metadata/op_api/l0_ai_infra_fused_infer_attention_sink_metadata.cpp#L75-L91)）：

```cpp
static internal::AicpuTaskSpace space("AiInfraFusedInferAttentionSinkMetadata");
auto ret = ADD_TO_LAUNCHER_LIST_AICPU(
    AiInfraFusedInferAttentionSinkMetadata,
    OP_ATTR_NAMES({"num_heads_q", ..., "aic_core_num", "aiv_core_num"}),
    OP_INPUT(actualSeqLengthsOptional, actualSeqLengthsKvOptional),
    OP_OUTPUT(metaData),
    OP_ATTR(numHeadsQ, ..., aicCoreNum, aivCoreNum));
```

与 u2-l2 的 AICORE 版对照：多了 `AicpuTaskSpace`（AICPU 任务的地址空间声明），`OP_ATTR_NAMES` 里的属性名字符串必须与 proto 声明一字不差——这是「proto ↔ l0」的命名对齐点。

**aclnn 第一段的两个特殊点**：[aclnn_ai_infra_fused_infer_attention_sink_metadata.cpp:L60-L165](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink_metadata/op_api/aclnn_ai_infra_fused_infer_attention_sink_metadata.cpp#L60-L165)

其一，`ParamsCheck` 是空壳（[L37-L58](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink_metadata/op_api/aclnn_ai_infra_fused_infer_attention_sink_metadata.cpp#L37-L58) 只 `return ACLNN_SUCCESS`)。对比 scatter 算子在 aclnn 层做 NotNull/Empty/Dtype 三步检查，这里把全部校验下沉到了 AICPU kernel 内部（4.2 节的 `CheckSingleParam/ParamsCheck`）——因为 kernel 反正要逐值读张量，校验与计算在同一处更内聚。

其二，第一段主动查平台信息并钳制用户传参（[L127-L137](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink_metadata/op_api/aclnn_ai_infra_fused_infer_attention_sink_metadata.cpp#L127-L137)）：

```cpp
const op::PlatformInfo &npuInfo = op::GetCurrentPlatformInfo();
uint32_t aicCoreNum = npuInfo.GetCubeCoreNum();
uint32_t aivCoreNum = npuInfo.GetVectorCoreNum();
if (aicCoreNumOptional <= 0 || aicCoreNumOptional > (int64_t)aicCoreNum) {
    aicCoreNumOptional = (int64_t)aicCoreNum;   // 非法值一律回退为平台真实核数
}
```

这正是 4.1 节「proto 里三个平台必选属性」的答案：属性由 aclnn 层自动采集 `GetSocLongVersion()` 与核数填入，用户无须（也不应）手工提供。第二段与普通算子无异（[L167-L174](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink_metadata/op_api/aclnn_ai_infra_fused_infer_attention_sink_metadata.cpp#L167-L174) 走 `CommonOpExecutorRun`），且 `*workspaceSize = 0`（[L162](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink_metadata/op_api/aclnn_ai_infra_fused_infer_attention_sink_metadata.cpp#L162)）——AICPU 算子不需要 workspace，输出张量本身承载全部结果。

**V1/V2 两个入口**。仓库还提供 `aclnn_ai_infra_fused_infer_attention_sink_metadata_v2.cpp`，其第一段最终调用的是**同一个** `l0op::AiInfraFusedInferAttentionSinkMetadata`（[aclnn_ai_infra_fused_infer_attention_sink_metadata_v2.cpp:L141](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink_metadata/op_api/aclnn_ai_infra_fused_infer_attention_sink_metadata_v2.cpp#L141)）。谁在选？答案是 wheel 侧的 csrc。

**csrc 侧**：[npu_fused_infer_attention_sink_metadata.cpp（torch_ops_extension）](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/fused_infer_attention_sink_metadata/csrc/npu_fused_infer_attention_sink_metadata.cpp#L44-L98)。PrivateUse1 实现先就地分配输出（[L44-L51](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/fused_infer_attention_sink_metadata/csrc/npu_fused_infer_attention_sink_metadata.cpp#L44-L51)：`at::zeros_symint({1024}, Int, kPrivateUse1)`），再按 `batch_invariant` 分派（[L55-L98](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/fused_infer_attention_sink_metadata/csrc/npu_fused_infer_attention_sink_metadata.cpp#L55-L98)）：True 走 `aclnnAiInfraFusedInferAttentionSinkMetadataV2`，False 走 V1。Meta 实现只建形状不计算（[L104-L134](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/fused_infer_attention_sink_metadata/csrc/npu_fused_infer_attention_sink_metadata.cpp#L104-L134)），两个 `TORCH_LIBRARY_IMPL` 注册（[L139-L148](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/fused_infer_attention_sink_metadata/csrc/npu_fused_infer_attention_sink_metadata.cpp#L139-L148)）——注意 schema 名带下划线前缀 `_npu_fused_infer_attention_sink_metadata`，按 torch 惯例这是「内部算子」标记，与 docs 中的调用名一致。

#### 4.3.4 代码实践

**目标**：推演「用户乱传核数」时系统的自我保护行为，并核对 docs 示例中的一个隐藏细节。

**步骤**：

1. 打开 [docs/npu_fused_infer_attention_sink_metadata.md:L81-L124](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink_metadata/docs/npu_fused_infer_attention_sink_metadata.md#L81-L124) 的调用示例，找到 `aicnum = 32`、`aivnum = 0` 两行。
2. 对照 aclnn 第一段的钳制代码（上文 L127-L137），写出 `aiv_core_num=0` 实际传到 AICPU kernel 的值。
3. 再读一遍钳制代码的第二个条件 `aivCoreNumOptional > static_cast<int64_t>(aicCoreNum)`，思考：aiv 的上限拿 aic 的核数来比，语义上对吗？

**需要观察的现象**：文档示例里 `aivnum = 0` 这个「看似非法」的输入不会报错，而是被静默替换为平台真实向量核数。

**预期结果**：步骤 2——`0 <= 0` 命中第一个条件，`aivCoreNumOptional` 被改写为 `npuInfo.GetVectorCoreNum()`，即用户传 0 等于「让框架自己查」。步骤 3——从语义看 aiv 的合法上限应当与 `aivCoreNum` 比较，源码却写了 `aicCoreNum`，这更像笔误（可作为代码评审观察点）；由于第二个条件只影响「传入过大值」的场景，实际危害有限。本实践为源码推演，真机行为**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 metadata 算子的 `GetWorkspaceSize` 把 `*workspaceSize` 设为 0，而 FIA Sink 主算子需要大 workspace？

**答案**：workspace 是 AICore kernel 的中间缓冲需求（u1-l3 讲过 scatter 的 Contiguous 也占 workspace）。metadata 算子的全部输出就是 1024 个整型，直接写进输出张量即可；且它跑在 CPU 核上，内存分配走 AICPU 运行时，不走 executor 的 workspace 机制。

**练习 2**：`ADD_TO_LAUNCHER_LIST_AICPU` 与 `ADD_TO_LAUNCHER_LIST_AICORE`（u2-l2）的共同点是什么？

**答案**：两者都把「算子名 + 输入输出描述符 + 属性值」登记进 `aclOpExecutor` 的下发列表而非立即执行，真正的执行发生在第二段按 stream 异步下发时——这保证了 aclnn 两段式的统一语义。区别只在登记的目标通道（AICPU/AICore），运行时据此选择执行引擎。

**练习 3**：用户在 Python 侧把 `batch_invariant=True` 传进去，链路上哪些文件会被波及？

**答案**：csrc 的 `npu_fused_infer_attention_sink_metadata_npu` 据此选 `aclnnAiInfraFusedInferAttentionSinkMetadataV2`（V1 的 l0 调用里 `batchInvariant` 实参被硬编码为 `false`，见 aclnn V1 [L138-L159](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink_metadata/op_api/aclnn_ai_infra_fused_infer_attention_sink_metadata.cpp#L138-L159) 中的字面量 `false`）；属性透传到 AICPU kernel 后影响 `CalcInnerSizeMla/Gqa`（batchInvariant 时 `sInnerSize_` 固定 256 以保证 reduceSum 累加序一致，见 [L116-L118](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink_metadata/op_kernel_aicpu/ai_infra_fused_infer_attention_sink_metadata_aicpu.cpp#L116-L118)）以及 `SplitCore` 中是否做 FD 分配（`supportFD && !batchInvariant` 才走按块分配）。

### 4.4 生产者—消费者：metadata 如何被主算子消费

#### 4.4.1 概念说明

metadata 算子的价值不在自身，而在它与 FIA Sink 主算子的配合：它算出的 `FiaSinkMetaData` 是主算子 AICore kernel 的「施工图」，替代（或补充）了 Host 侧 Tiling 的职责。这套配合有三个精巧设计：

1. **单一事实源**：元数据内存布局定义在主算子目录的 `op_kernel/ai_infra_fused_infer_attention_sink_metadata.h`，生产者（AICPU kernel）与消费者（主算子的多个 kernel 头文件）include 同一份，布局永不失配，且用 `static_assert` 在编译期保证总大小装得进 1024 个元素。
2. **张量即信道**：metadata 的输出张量直接作为主算子 aclnn 的一个可选输入传入（`metaDataOptional`），两个算子间没有隐藏全局状态，天然适配 aclgraph 整图捕获。
3. **流同步**：AICPU 与 AICore 可能跑在不同流上，用 event 做先生产后消费的次序约束。

#### 4.4.2 核心流程

```text
┌─────────── aicpu_stream ───────────┐   ┌─────────── aicore_stream ───────────┐
│ metadata 算子（CPU 核）              │   │ FIA Sink 主算子（AI Core）            │
│  写 FiaSinkMetaData 到输出张量       │   │  Init 时按 GetXxxMetaAbsIndex 读同一  │
│        │                            │   │  张量，得每核处理范围与 FD 归约方案    │
│        └── event.record ────────────┼──▶│ wait_event 后才启动                   │
└────────────────────────────────────┘   └─────────────────────────────────────┘
```

消费侧的读取方式：主算子 kernel 把 metadata 张量绑成 `metadataGm`（GlobalTensor），用共享头文件提供的三个内联函数按「核号 + 字段号」取绝对下标：

\[
\text{AIC 下标} = \text{coreIdx} \times 10 + \text{metaIdx}
\]
\[
\text{AIV 下标} = 360 + \text{coreIdx} \times 3 + \text{metaIdx},\quad
\text{BASE 下标} = 576 + \text{metaIdx}
\]

#### 4.4.3 源码精读

**共享契约**（主算子目录）：[ai_infra_fused_infer_attention_sink_metadata.h:L24-L32](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/ai_infra_fused_infer_attention_sink_metadata.h#L24-L32) 定义尺寸，[L35-L61](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/ai_infra_fused_infer_attention_sink_metadata.h#L35-L61) 定义 23 个字段的下标常量（AIC 10 个、AIV 3 个、BASE 10 个），[L69-L87](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/ai_infra_fused_infer_attention_sink_metadata.h#L69-L87) 是三个取绝对下标的函数（注意被 `#ifdef __CCE_AICORE__` 包住——只在 AscendC 侧编译，AICPU 侧用不到它们），[L89-L97](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/ai_infra_fused_infer_attention_sink_metadata.h#L89-L97) 是结构体与 `static_assert`。特别注意 `AIC_CORE_NUM = 36` 是**写死的最大核数**，不是运行时核数——metadata 张量按最大核数预留空间，实际有效性由 `baseMetadata[USED_CORE_NUM_INDEX]` 界定。

**消费点一：主算子 aclnn 接口签名**。FIA Sink 的 aclnn 声明中有 `metaDataOptional`（[aclnn_ai_infra_fused_infer_attention_sink.cpp:L311](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_api/aclnn_ai_infra_fused_infer_attention_sink.cpp#L311)，即 27 个可选输入之一）；主算子文档参数表对它的描述是「该 Tensor 包含了后续 FusedInferAttentionSink 计算所需的各种配置和调度信息」，UINT32/ND，示例代码中 `metaDataShape = {1024}`，见 [aclnnAiInfraFusedInferAttentionSink.md:L442-L448](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/docs/aclnnAiInfraFusedInferAttentionSink.md#L442-L448) 与 [L1317](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/docs/aclnnAiInfraFusedInferAttentionSink.md#L1317)。

**消费点二：主算子 kernel 读取**。MLA 模板 kernel 在 Init 阶段读 BASE 区拿全局参数（[fia_kernel_nonquant_mla_sink.h:L230-L249](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_kernel_nonquant_mla_sink.h#L230-L249)）：

```cpp
usedCoreNum = metadataGm.GetValue(optiling::GetBaseMetaAbsIndex(optiling::USED_CORE_NUM_INDEX));
constInfo.mBaseSize  = metadataGm.GetValue(optiling::GetBaseMetaAbsIndex(optiling::M_BASE_SIZE_INDEX));
constInfo.s2BaseSize = metadataGm.GetValue(optiling::GetBaseMetaAbsIndex(optiling::S_INNER_SIZE_INDEX));
```

再按自己的核号读 AIC 区拿「我处理到哪」（[L606-L608](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_kernel_nonquant_mla_sink.h#L606-L608)）：`bN2End/gS1End/s2End` 三个右开区间端点；FD 归约信息在 [L764-L797](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_kernel_nonquant_mla_sink.h#L764-L797) 从 AIC/AIV 两区读出。GQA 模板 `fia_kernel_nonquant_sink.h` 有完全同构的读取代码（如 [L253](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_kernel_nonquant_sink.h#L253) 起）。对照 4.2.3 的 `GenMetaData`：写的字段与读的字段一一套齐——`BN2_END_PTR_INDEX` 写 `splitRes.bN2End[i]`，读侧即 `constInfo.bN2End`，这就是「施工图」的全部含义。

**消费点三：ST 测试的双流编排**。[test_npu_fused_infer_attention_sink_metadata.py:L53-L112](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink_metadata/tests/st/test_npu_fused_infer_attention_sink_metadata.py#L53-L112) 是全仓库最直观的生产者-消费者示范：

```python
aicore_stream = torch.npu.current_stream()
aicpu_stream = torch.npu.Stream()
event = torch.npu.Event()
with torch.npu.graph(g):
    with torch_npu.npu.stream(aicpu_stream):
        metadata = torch.ops.custom._npu_fused_infer_attention_sink_metadata(...)  # 生产
        event.record(aicpu_stream)
    with torch_npu.npu.stream(aicore_stream):
        aicore_stream.wait_event(event)                                            # 次序约束
        npu_result, softmax_lse = torch.ops.custom.npu_fused_infer_attention_sink(
            ..., meta_data=metadata)                                               # 消费
```

最后与 CPU 参考实现做 `assertRtolEqual` 精度对拍（[L110-L112](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink_metadata/tests/st/test_npu_fused_infer_attention_sink_metadata.py#L110-L112)）。

#### 4.4.4 代码实践

**目标**：吃透双流时序，并推演破坏同步条件时的后果。

**步骤**：

1. 精读 ST 测试 L68-L107，按时间轴列出：metadata 算子在哪个流执行、event 在哪记录、主算子在哪等待、`meta_data=metadata` 如何把生产者输出接到消费者输入。
2. 回答假设题：若删掉 `aicore_stream.wait_event(event)` 这一行，程序行为会怎样？
3. 追加思考：若两个算子放在**同一个流**里（不用 event），还需要显式同步吗？

**需要观察的现象**：event 是唯一的生产→消费次序保证；张量传递本身不携带同步语义。

**预期结果**：步骤 2——不同流之间没有默认次序，主算子可能读到尚未写完的 metadata（竞态），表现为偶发的调度错乱或输出错误，且难以复现；步骤 3——同一流内算子按下发顺序串行执行，天然有序，event 可以省去（代价是 AICPU 与 AICore 不能重叠安排）。以上为语义推演，真机复现**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么共享头文件放在**主算子**的 `op_kernel/` 下，而不是 metadata 算子自己的目录或公共 common 目录？

**答案**：这份布局的主人是主算子的 kernel（多个模板头文件都要读它），metadata 算子只是「代笔」。放在主算子目录使依赖方向清晰：metadata 算子通过 CMake 的 `DEPENDENCIES ai_infra_fused_infer_attention_sink` 声明这条单向依赖（构建顺序随之保证）。若放进 common，会割裂「主算子消费字段」与「字段定义」的就近关系。

**练习 2**：`AIC_CORE_NUM = 36` 写死，如果未来芯片 AIC 核数超过 36 会怎样？

**答案**：`FiaSinkMetaData` 的 `aicMetadata[36][10]` 会越界写坏 AIV 区数据（`GenMetaData` 的循环上界是 `usedCoreNum`，而 `usedCoreNum <= aicCoreNum_` 来自平台查询）。防线是 [L97](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/ai_infra_fused_infer_attention_sink_metadata.h#L97) 的 `static_assert` 只保证「结构体 ≤ 1024 元素」，不保证「核数 ≤ 数组容量」——适配新 SOC 时这类容量常量是需要显式排查的隐患点（呼应 u6-l4 的 SOC 适配清单）。

**练习 3**：metadata 算子与 u2-l3 的 Host 侧 Tiling，产出物有什么异同？

**答案**：同——都在算「数据怎么切、每个核干什么」，产出都是一份下发给 kernel 的调度描述。异——Host Tiling 的产物是 TilingData，经框架序列化后由 kernel 入口 `GET_TILING_DATA` 解包，生命周期在单次算子调用内；metadata 的产物是普通张量，显式在算子间传递、可进 aclgraph、甚至可被用户缓存复用（同形状同长度只算一次）。这个差异正是第 5 单元 u5-l4「tiling 下沉」思想的雏形——把调度计算从 host 搬到 device、从框架内隐式步骤变成显式算子。

## 5. 综合实践

制作一张 **AICore 算子（ai_infra_scatter_block_update）vs AICPU 算子（ai_infra_fused_infer_attention_sink_metadata）** 全维度对比表，并完成消费链路佐证。这是本讲的收官任务，做完后你对「仓库里的算子有两条技术路线」会有落地的认知。

### 5.1 任务一：三维度对比表

按下表模板填充（「示例」行已给出答案，其余行自己完成）：

| 维度 | 对比项 | AICore：scatter_block_update | AICPU：sink_metadata |
| --- | --- | --- | --- |
| 目录 | 计算层目录名 | op_kernel/ | **示例：op_kernel_aicpu/** |
| 目录 | 原型文件 | op_host/*_def.cpp | ? |
| 目录 | 切分文件 | op_host/*_tiling.h/.cpp | ?（提示：func.cmake 会生成什么占位） |
| 目录 | 计算层特有文件 | —（无） | ?（.json 的作用） |
| 入口 | kernel 入口符号 | extern "C" __global__ __aicore__ | ? |
| 入口 | 参数来源 | x/indices/update + workspace + TilingData | ? |
| 入口 | 读输入数据方式 | DataCopy GM→UB 后计算 | ? |
| 入口 | 注册宏 | —（CMake 收集编译） | ? |
| 构建产物 | kernel 编译器 | 毕昇 bisheng（AscendC） | ?（提示：func.cmake L1073 的工具链变量名） |
| 构建产物 | kernel 载体 | 随 cust_opmaster/cust_opapi 等 so 下发的 AscendC 二进制 | ?（so 名 + 安装目录） |
| 构建产物 | 调度信息配置 | —（或 op_host/config 下 json/ini） | ?（json 合并成什么、装到哪） |
| 构建产物 | 平台声明 | OpDef AddConfig(ascend910b...) | ?（config.ini） |

**方法**：scatter 侧回看 u1-l3/u2 系列讲义与源码；metadata 侧用本讲 4.1-4.3 的引用。逐行给出「文件路径 + 行号」佐证，不凭记忆填。

### 5.2 任务二：消费链路佐证

回答「metadata 的输出被主算子的哪个输入消费」，要求三级证据链：

1. **接口级**：在主算子 aclnn 声明中找到该可选输入的形参名与位置（提示：`aclnn_ai_infra_fused_infer_attention_sink.cpp` L311 一带）。
2. **文档级**：在主算子 `docs/aclnnAiInfraFusedInferAttentionSink.md` 参数表中找到它的类型、格式与描述，并核对示例中 `metaDataShape` 的值。
3. **运行级**：在 ST 测试中指出张量如何从生产者的返回值流到消费者的关键字参数。

再用一句话说明：为什么这个输入是**可选**的？（提示：不传时主算子走什么路径？结合 u4-l1 讲的 Host 侧 FiaTiling 框架回答；把你的判断标注「推演，待本地验证」。）

### 5.3 自查清单

- [ ] 对比表 12 行每行都有文件级佐证，没有「凭印象」的格子。
- [ ] 能口头说出三次命名对齐在本算子上的三个位置（REG_OP 名、REGISTER_CPU_KERNEL 字符串、json 键）。
- [ ] 能在白纸上画出 `FiaSinkMetaData` 的三段内存布局与下标公式。
- [ ] 能解释双流 + event 时序，以及为什么 aclgraph 场景特别需要这种显式算子化设计。

## 6. 本讲小结

- AICPU 算子是仓库中的特例路线：原型用 `op_graph/*_proto.h` 的 `ge::REG_OP` 声明（无 OpDef/def.cpp），计算体是 `op_kernel_aicpu/` 下的纯 C++ `CpuKernel`，跑在设备的通用 ARM CPU 核上，编译产物是交叉编译出的 `libtransformer_aicpu_kernels.so` + 合并生成的 `cust_aicpu_kernel.json`。
- `*_aicpu.json` 是运行时的路由表：`engine=DNN_VM_AICPU` + `kernelSo` 告诉框架去哪个库找 `RunCpuKernel` 入口，再按 `REGISTER_CPU_KERNEL` 的算子名分发。
- op_api 层与 AICore 算子同构（aclnn 两段式 + l0 封装），分叉只在 `ADD_TO_LAUNCHER_LIST_AICPU`；aclnn 第一段自动采集平台信息（SOC 版本与 AIC/AIV 核数）填充必选属性并钳制用户传参，`workspaceSize` 恒为 0。
- metadata 算子的核心价值：AICPU 能直接读设备张量数值（每个 batch 的真实序列长度），据此用代价模型 \(6\lceil M/16\rceil + 10\lceil S_2/64\rceil\) 与多核数枚举做负载均衡，把结果按 `FiaSinkMetaData` 结构体覆盖写进输出张量。
- 生产者-消费者协作：主算子目录下的共享头文件是布局单一事实源，主算子 kernel 用 `GetAIC/AIV/BaseMetaAbsIndex` 按核号读字段；Python/ST 层用双流 + event 保证先生产后消费，且整条链可被 aclgraph 捕获。
- 阅读任何昇腾算子目录前，先看计算层目录名（`op_kernel` vs `op_kernel_aicpu`）与有无 `*_aicpu.json`，即可立刻判断它走哪条路线。

## 7. 下一步学习建议

下一讲（u4-l3）进入稀疏注意力家族，你会再次遇到同构的 AICPU 算子 `ai_infra_sparse_flash_attn_metadata`（其 CMake 同样用 `add_modules_sources_aicpu`，依赖 `ai_infra_sparse_flash_attention_pioneer`），可以趁热对比它与本讲的异同，检验本讲的对比表是否通用。

若想继续深挖本讲的两条支线：

1. **AICPU 执行通道的系统化设计**：第 5 单元 u5-l4 的 tiling_sink 公共模块（`tiling_aicpu_task`、`device_op_impl_registry`），它把「调度计算下沉设备侧」做成了框架级机制，本讲的 metadata 算子是它的算子级前身。
2. **多核并行与 FlashDecode**：第 5 单元 u5-l2 精读 FIA Sink 的 AIV/AIC 协同 kernel，你会看到本讲 `SplitCore/SplitFD` 算出的 `fdRes` 系列字段在 kernel 侧如何驱动跨核归约。
