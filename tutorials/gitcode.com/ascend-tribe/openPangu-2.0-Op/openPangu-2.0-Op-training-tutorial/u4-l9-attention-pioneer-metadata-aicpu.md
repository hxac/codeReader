# u4-l9 AttentionPioneerMetadata：AICPU 算子与 op_graph 形态

## 1. 本讲目标

本讲是 Attention 算子族（第 4 单元）的收官篇。前八讲的算子全部是 AICore 上的 Ascend C 算子，本讲换一个形态：**`ai_infra_attention_pioneer_metadata` 是整个仓库唯一的 AICPU 算子**（全仓库只有一个 `op_kernel_aicpu` 目录、一处 `REGISTER_CPU_KERNEL`）。它是 u4-l8 精读的 `ai_infra_attention_pioneer`（Ascend 950/arch35 新一代注意力）的"前置算子"，专门解决一个 u4-l8 遗留的问题：**变长场景下真实序列长度在设备侧，Host 侧 tiling 算不出精确分核方案**。

学完本讲，你应该能：

1. 说清 AICPU 算子与 AICore 算子的适用边界——什么样的逻辑该放 AICPU，什么样的计算该放 AICore。
2. 看懂 AICPU 算子的"注册三件套"：`op_graph` 的 `REG_OP` 原型头、`op_kernel_aicpu` 的 json 引擎绑定、`config.ini` 的门禁芯片声明，并理解它们与 Ascend C 算子 `_def.cpp`（`OpDef` + `OP_ADD`）的对应与差异。
3. 精读 AICPU Kernel 的六阶段流水（Prepare → CheckIsDecode → ParseAxisInfo → SetTilingParams → ComputeSplitNBSeq → GenMetaData），理解 `int32[1024]` metadata 张量的内存布局契约，以及它如何被 Attention Kernel 在设备侧回读。
4. 描述 `l0_` 前缀的 op_api 封装与 aclnn 两段式接口的分层（承接 u2-l5），特别是 aclnn 层**运行时回填 AIC/AIV 核数**这一独特设计。
5. 对比本仓库三种算子形态（AICore AscendC / AICPU / pypto Python 算子）的开发、编译、调试差异。

## 2. 前置知识

本讲默认你已读过 u1-l2（四层算子模型）、u2-l3（Tiling 概念）、u2-l5（aclnn 两段式接口）、u4-l8（AttentionPioneer 前向）。以下概念用三句话补齐：

- **AICore 与 AICPU**：昇腾芯片上有两类计算单元。AICore 是矩阵/向量加速单元（Cube 做 Matmul、Vector 做逐元素运算），跑的是 Ascend C 编译出的二进制 kernel，SPMD 多核并行；AICPU 是设备上附带的一颗**通用 CPU 核**，跑的是普通 C++ 编译出的 `.so`，串行执行、支持任意分支循环和指针操作。
- **Host 侧 Tiling 的盲区**：u2-l3 讲过，Tiling 在 Host 侧执行，只能看到**静态 shape 和属性**。当序列长度存在 device 侧张量里（TND 变长排布，Host 不做 D2H 拷贝以避免同步开销）时，Host tiling 只能按上限粗切。`ai_infra_attention_pioneer` 的精确分核因此被"下沉"到设备上执行——但不是在 AICore 上（那要写 Ascend C，控制流别扭），而是在 AICPU 上，用一个 C++ 算子读张量、算分核、写回一个小张量。
- **metadata 张量**：这个 AICPU 算子的输出是一根固定的 `int32[1024]` 张量，内容是"每个核分到哪些活"的调度表。它不是数据，而是一份**设备侧生成的 TilingData 替代品**——u4-l8 的 Attention Kernel 启动后第一件事就是读它。这与第 9 单元 u9-l2 要讲的 `tiling_sink` 是同一动机的两种实现路径，可对照理解。

一个术语澄清（承接 u4-l8 的纠偏）：本算子目录叫 `arch35`，但 `config.ini` 与 docs 都表明它对应 **Ascend 950（A5 代）**，不是 A3。头文件注释也写明 "A5 platform core numbers"。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/config.ini` | 门禁工程识别芯片版本用（ascend950），不影响执行逻辑 |
| `ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_graph/ai_infra_attention_pioneer_metadata_proto.h` | AICPU 算子原型：`ge::REG_OP` 声明输入/输出/属性 |
| `ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/arch35/ai_infra_attention_pioneer_metadata_aicpu.cpp` | AICPU Kernel 主体：六阶段计算分核方案 |
| `ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/arch35/ai_infra_attention_pioneer_metadata_aicpu.h` | Kernel 类声明、稀疏模式枚举、参数表 |
| `ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/arch35/ai_infra_attention_pioneer_metadata.json` | AICPU 引擎绑定：算子挂到 `DNN_VM_AICPU` 引擎与自定义内核库 |
| `ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_host/arch35/ai_infra_attention_pioneer_metadata.h` | metadata 内存布局契约（36 核 × 10 槽 + 16 个基础字段） |
| `ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_host/arch35/ai_infra_attention_pioneer_metadata_infershape.cpp` | InferShape/InferDataType：输出恒为 `int32[1024]` |
| `ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_api/l0_ai_infra_attention_pioneer_metadata.cpp` | L0 层：把算子以 AICPU 任务形式挂进 executor |
| `ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_api/aclnn_ai_infra_attention_pioneer_metadata.cpp` | aclnn 两段式接口 + 参数校验 + 运行时核数回填 |
| `ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_kernel_base.h` | 消费方：Attention Kernel 读 metadata 张量做精确分核 |

## 4. 核心概念与源码讲解

### 4.1 AICPU 算子适用边界：控制流密集 vs 计算密集

#### 4.1.1 概念说明

选 AICore 还是 AICPU，本质是给逻辑选处理器：

- **AICore（Ascend C）**：为稠密线性代数而生。Matmul/向量原语吞吐极高，但编程模型是 SPMD——每个核执行同一份代码、靠 `GetBlockIdx()` 区分身份；控制流（不定长循环、复杂分支、跨张量的串行依赖）写起来既别扭又低效。
- **AICPU（C++ Kernel）**：为控制流而生。继承 `aicpu::CpuKernel`、重写一个 `Compute(CpuKernelContext&)`，就是普通 C++：可以直接 `reinterpret_cast` 读数据指针、可以用 `std::vector`、可以做任意循环。代价是算不了大矩阵——吞吐比 AICore 低几个量级。

本算子恰好是 AICPU 的教科书场景：输入只有两根变长序列张量（几十个 int64），要跑一遍**贪心负载均衡**（大量小循环、比较、浮点目标值），输出只有 1024 个 int32。总数据量不到 4KB，但要动几千次分支——放 AICore 上写 Ascend C 是灾难，放 Host 上又拿不到 device 侧的序列长度。于是放 AICPU：**在设备上、以 CPU 串行逻辑，替 AICore 做一次"设备侧 tiling"**。

#### 4.1.2 核心流程

先看全景。一次 `npu_ai_infra_attention_pioneer_metadata` 调用的完整链路：

```
torch.ops.custom.npu_ai_infra_attention_pioneer_metadata(...)     # torch 侧封装
  └─ EXEC_NPU_CMD_V1(aclnnAiInfraAttentionPioneerMetadata, ...)   # csrc 桥接（u6-l2 讲过）
      └─ aclnnAiInfraAttentionPioneerMetadataGetWorkspaceSize     # 第一段：校验 + 回填核数
          └─ l0op::AiInfraAttentionPioneerMetadata                # L0：挂 AICPU 任务进 executor
              └─ ADD_TO_LAUNCHER_LIST_AICPU(...)                  # 记录为 AICPU 任务
      └─ aclnnAiInfraAttentionPioneerMetadata                     # 第二段：CommonOpExecutorRun 下发
          └─ AICPU 上执行 AiInfraAttentionPioneerMetadataKernel::Compute
              └─ 输出 metadata int32[1024] → 被 pioneer AICore Kernel 回读
```

与 AICore 算子（u1-l2 四层模型）逐层对照：

| 层 | AICore 算子（如 aggregate_hidden） | AICPU 算子（本算子） |
| --- | --- | --- |
| 原型注册 | `op_host/*_def.cpp`：`OpDef(...)` + `OP_ADD` | `op_graph/*_proto.h`：`ge::REG_OP(...).OP_END_FACTORY_REG` |
| Host 规划 | `op_host/*_tiling.cpp`：TilingContext、tilingKey、TilingData | 无 tiling（kernel 自己在设备上规划） |
| 设备执行 | `op_kernel/*.cpp`：Ascend C 模板类 | `op_kernel_aicpu/arch35/*_aicpu.cpp`：`CpuKernel::Compute` |
| 对外接口 | `op_api/aclnn_*.cpp`（多数算子缺省，由算子包生成） | `op_api/l0_*.cpp` + `aclnn_*.cpp` 成对入仓 |
| 引擎绑定 | def 中 `AICore().AddConfig("ascend910b", ...)` | json：`engine=DNN_VM_AICPU` + `opKernelLib=CUSTAICPUKernel` |

#### 4.1.3 源码精读

先看 Kernel 入口——与 Ascend C kernel 入口（`extern "C" __global__ __aicore__` + `TILING_KEY_IS` 分支，见 u2-l4）截然不同，就是一个普通的虚函数重写：

```cpp
uint32_t AiInfraAttentionPioneerMetadataKernel::Compute(CpuKernelContext &ctx)
{
    if (!Prepare(ctx))          { return KERNEL_STATUS_PARAM_INVALID; }
    CheckIsDecode();
    if (!ParseAxisInfo())       { return KERNEL_STATUS_PARAM_INVALID; }
    SetTilingParams();
    ComputeSplitNBSeq();
    if (!GenMetaData())         { return KERNEL_STATUS_PARAM_INVALID; }
    return KERNEL_STATUS_OK;
}
```

[AICPU Kernel 的 Compute 入口:L26-L41](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/arch35/ai_infra_attention_pioneer_metadata_aicpu.cpp#L26-L41) —— 整个算子的执行骨架就是这六个阶段：取参 → 判定 P/D 侧 → 解析序列长度 → 定 tile 尺寸 → 贪心分核 → 写 metadata。没有 tilingKey、没有模板分支、没有 TPipe/TQue，返回值就是 `KERNEL_STATUS_OK(0)` 或 `KERNEL_STATUS_PARAM_INVALID(1)`（这两个宏是本算子自带工具头定义的，见 [cpu_context_util.h:L26-L27](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/arch35/cpu_context_util.h#L26-L27)）。

类声明同样朴素：[ai_infra_attention_pioneer_metadata_aicpu.h:L38-L42](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/arch35/ai_infra_attention_pioneer_metadata_aicpu.h#L38-L42) 声明 `class AiInfraAttentionPioneerMetadataKernel : public CpuKernel`，基类来自 CANN 包的 `cpu_kernel.h`（编译时从 `pkg_inc/aicpu` 等目录引入，见 [op_kernel_aicpu/CMakeLists.txt:L21-L30](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/CMakeLists.txt#L21-L30)）。

还有一个细节值得注意：头文件里的 [ParamId 枚举:L85-L89](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/arch35/ai_infra_attention_pioneer_metadata_aicpu.h#L85-L89) 中，两个输入下标是 0/1，而输出 `META_DATA = 0`——**输入和输出的下标空间是各自独立的**（`ctx.Input(0..1)` 与 `ctx.Output(0)`），这与 Ascend C kernel 入口参数"输入输出排排坐"的约定不同。

#### 4.1.4 代码实践

**实践目标**：通过"目录形态对照"建立 AICPU 算子的结构直觉。

**操作步骤**：

1. 在仓库根目录执行 `find ascendc/src -type d -name op_kernel_aicpu`，确认全仓库只有 `ai_infra_attention_pioneer_metadata` 一个 AICPU 算子目录。
2. 执行 `ls ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/`，对照 u1-l1 讲过的 AICore 算子五件套（README/docs/op_host/op_kernel/tests），列出差异：这里没有 `op_kernel/`（设备实现在 `op_kernel_aicpu/`），多了 `op_graph/` 与 `config.ini`，`op_host/` 里只有 infershape、没有 tiling。
3. 打开 [ai_infra_attention_pioneer_metadata_aicpu.cpp:L26-L41](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/arch35/ai_infra_attention_pioneer_metadata_aicpu.cpp#L26-L41)，把六个阶段函数名抄成一张表，给每个阶段写一句中文注释（参考 4.3 节的详细讲解自查）。

**需要观察的现象**：AICPU 算子目录比 AICore 算子"少一层、多两件"——少 tiling 层，多 op_graph 原型头与 config.ini。

**预期结果**：得到一张「AICore 五件套 vs AICPU 目录」对照清单。此实践纯目录/源码阅读，无需 NPU，本讲义编写时已核实目录清单。

#### 4.1.5 小练习与答案

**练习 1**：如果把本算子的贪心分核逻辑写成 Host 侧 tiling（`_tiling.cpp`），会遇到什么障碍？

**答案**：Host tiling 通过 `gert::TilingContext` 只能拿到静态 shape 与属性（u2-l3），而分核算法需要 `actual_seq_lengths` 张量里每个元素的**值**（变长 batch 的真实序列长度，数据在 device 上）。Host 侧要么做 D2H 同步拷贝（打断流水、引入主机-设备同步开销），要么只能按 shape 上限粗切。AICPU 方案避免了这两者。

**练习 2**：为什么输出是 1024 个 int32 而不是直接把 per-batch 序列长度也存进去？（提示：看 docs 的返回值描述与头文件布局注释的差异）

**答案**：docs 说 metadata 包含 base 统计、per-core 切分索引与 per-batch 序列长度三类信息，但源码的权威布局（[ai_infra_attention_pioneer_metadata.h:L72-L81](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_host/arch35/ai_infra_attention_pioneer_metadata.h#L72-L81)）只定义了 `[0..359]` per-core 槽位与 `[360..375]` baseMetadata 共 376 个 int；`GenMetaData` 也只写这两段（见 4.3.3）。pioneer Kernel 需要原始序列长度时直接读 `actual_seq_lengths` 张量本身（metadata 里只存了它们的 dim 数），不必经由 metadata 中转。docs 的描述滞后于代码——读接口契约以源码为准，这是本手册反复验证的规律（u2-l5、u4-l1 同款结论）。

---

### 4.2 原型注册三件套：op_graph 的 REG_OP、aicpu json 与 config.ini

#### 4.2.1 概念说明

Ascend C 算子用 `OpDef` 类 + `OP_ADD` 宏注册原型（u2-l2），并用 `AICore().AddConfig("ascend910b", ...)` 声明"编译成 AICore 二进制、支持哪些芯片"。AICPU 算子走的是 CANN 的另一条注册通道，由三份小文件分工：

1. **`op_graph/*_proto.h`**：用 `ge::REG_OP` 链式 DSL 声明算子的输入/输出/属性原型——这是图引擎（GE）认识算子的方式，角色上等价于 `_def.cpp`。
2. **`op_kernel_aicpu/arch35/*.json`**：把算子名绑定到执行引擎（`DNN_VM_AICPU`）、内核库（`CUSTAICPUKernel`）与 `.so` 文件名——等价于 `AICore().AddConfig` 的"选择执行引擎"职责，但选的是 CPU 引擎。
3. **`config.ini`**：仓库自述"仅用于门禁工程识别算子适配芯片版本，不影响算子业务执行逻辑"——纯粹给 CI/门禁看的芯片清单。

#### 4.2.2 核心流程

`REG_OP` DSL 的声明顺序就是运行期下标顺序，这一点与 `_def.cpp` 的 `Input()` 链完全同构（u2-l2 讲过"声明顺序即索引"）：

```
REG_OP(AiInfraAttentionPioneerMetadata)
    .OPTIONAL_INPUT(actual_seq_lengths_q,   DT_INT64)   // Input(0)
    .OPTIONAL_INPUT(actual_seq_lengths_kv,  DT_INT64)   // Input(1)
    .OUTPUT(metaData_out,                   DT_INT32)   // Output(0)
    .REQUIRED_ATTR(num_heads_q, Int) ...               // 必选属性
    .ATTR(input_layout, String, "TND") ...             // 带默认值的可选属性
    .OP_END_FACTORY_REG(AiInfraAttentionPioneerMetadata)
```

三份文件 + infershape 共同构成"AICPU 算子的 Host 侧档案"：proto 声明接口 → infershape 定输出 → json 定引擎 → config.ini 定门禁芯片。注意 `op_host/` 里**没有 tiling 文件**——AICPU 算子的规划发生在 kernel 内部（设备上），Host 侧无事可做。

#### 4.2.3 源码精读

**proto 头**（完整的原型声明）：

[ai_infra_attention_pioneer_metadata_proto.h:L23-L42](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_graph/ai_infra_attention_pioneer_metadata_proto.h#L23-L42) —— 声明 2 个可选输入（int64）、1 个输出（int32）、4 个必选属性（`num_heads_q/kv`、`head_dim_qk/v`）、8 个带默认值的可选属性（`sparse_mode=0`、`pre_tokens/next_tokens=2147483647`、`input_layout="TND"`、`k_sink_num=0` 等），以及 3 个**必选**的平台属性：`soc_version(String)`、`aic_core_num(Int)`、`aiv_core_num(Int)`。平台信息被设计成属性从 aclnn 层灌进来（4.4 节详述），这是 AICPU 算子拿"运行环境参数"的惯用手法——它不像 Host tiling 有 `GetPlatformInfo()` 可查。

**json 引擎绑定**：

[ai_infra_attention_pioneer_metadata.json:L2-L13](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/arch35/ai_infra_attention_pioneer_metadata.json#L2-L13) —— 关键字段：`engine: DNN_VM_AICPU`（走 AICPU 引擎而非 AICore）、`kernelSo: libtransformer_aicpu_kernels.so`（本仓库 AICPU 内核编成的动态库）、`opKernelLib: CUSTAICPUKernel`（自定义 AICPU 内核库类别）、`functionName: RunCpuKernel`（库入口）、`flagAsync: False`。json 下方还逐一声明了 input0/input1/output0 的名字与类型，与 proto 一一对应。

**config.ini 门禁声明**：

[config.ini:L9-L12](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/config.ini#L9-L12) —— 段名 `[operater]`（源码原文如此，是 `operator` 的拼写笔误，门禁工具显然按原文匹配，阅读时不要"顺手修正"）、`op_name = ai_infra_attention_pioneer_metadata`、`aicore_versions = ascend950`。与 docs 产品表（仅 950PR/950DT 支持）互证。

**对照：Ascend C 算子的芯片声明长什么样**：

[ai_infra_aggregate_hidden_def.cpp:L83-L88](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp#L83-L88) —— `AICore().AddConfig("ascend910b", aicore_config)` / `AddConfig("ascend910_93", ...)` 后跟 `OP_ADD(AiInfraAggregateHidden)`。AICore 算子把"引擎 + 芯片"一起写进 def；AICPU 算子把"引擎"放 json、"芯片"放 config.ini（门禁用），职责拆得更散，读代码时要把三份文件串起来看。

**Host 侧唯一实现文件 infershape**：

[ai_infra_attention_pioneer_metadata_infershape.cpp:L21-L37](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_host/arch35/ai_infra_attention_pioneer_metadata_infershape.cpp#L21-L37) —— `InferShape` 把输出设为一维 `[AP_METADATA_SIZE]`（1024），`InferDataType` 设为 `DT_INT32`，然后用 `IMPL_OP_INFERSHAPE(AiInfraAttentionPioneerMetadata)` 注册。**输出 shape 与任何输入无关**——因为输出是协议缓冲，不是数据张量（综合实践里会带你推导这一点）。

#### 4.2.4 代码实践

**实践目标**：掌握 AICPU 算子原型的写法，能对照 proto 写出新的 REG_OP。

**操作步骤**：

1. 精读 [proto.h:L23-L42](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_graph/ai_infra_attention_pioneer_metadata_proto.h#L23-L42)，注意 `REQUIRED_ATTR` 与 `ATTR(name, Type, default)` 的区别。
2. 仿照它为假想算子 `AiInfraFooMetadata` 写一个 proto 头（示例代码，非仓库原有）：

```cpp
// 示例代码：假想 AICPU 算子的原型注册骨架
REG_OP(AiInfraFooMetadata)
    .REQUIRED_INPUT(x, TensorType({DT_INT64}))   // 必选输入写法
    .OPTIONAL_INPUT(x_kv, TensorType({DT_INT64}))
    .OUTPUT(foo_out, TensorType({DT_INT32}))
    .REQUIRED_ATTR(num_foo, Int)
    .ATTR(foo_mode, Int, 0)
    .REQUIRED_ATTR(soc_version, String)
    .REQUIRED_ATTR(aic_core_num, Int)
    .REQUIRED_ATTR(aiv_core_num, Int)
    .OP_END_FACTORY_REG(AiInfraFooMetadata)
```

3. 对照本算子 json，写出假想算子的 json（改 `opInfo` 内的算子名与 input/output 名，`engine`/`opKernelLib` 字段保持）。

**需要观察的现象**：proto 的输入/属性声明顺序，与 AICPU kernel 中 `ctx.Input(下标)`、`GetAttrValue(ctx, "属性名")` 的取用方式如何对齐（下标按输入顺序，属性按名字符串）。

**预期结果**：一份 proto + 一份 json 的假想算子骨架。纯文件编写，无需编译环境；如需编译验证需在 CANN 容器内挂接 `op_kernel_aicpu/CMakeLists.txt`，本讲不展开（综合实战见 u9-l4）。

#### 4.2.5 小练习与答案

**练习 1**：proto 里 `soc_version/aic_core_num/aiv_core_num` 被声明为 `REQUIRED_ATTR`（必选属性），但 torch 侧调用时用户并没有传这三个参数。它们从哪来？

**答案**：由 aclnn 层在运行期自动生成并灌入：[aclnn_ai_infra_attention_pioneer_metadata.cpp:L143-L160](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_api/aclnn_ai_infra_attention_pioneer_metadata.cpp#L143-L160) 先调 `aclrtGetResInCurrentThread` 拿当前线程被允许使用的 Cube/Vector 核数（"控核"场景），拿不到就回退 `GetCurrentPlatformInfo()` 的平台核数；`socVersion` 直接取 `npuInfo.GetSocLongVersion()`。随后这三个值经 L0 的 `OP_ATTR(...)` 写进任务（4.4 节）。这就是"必选属性由框架代填"的模式。

**练习 2**：`config.ini` 写了 `aicore_versions = ascend950`，一个 AICPU 算子为什么会有 "aicore_versions" 这种字段名？

**答案**：该字段只是门禁工程沿用的统一键名（文件头注释明确说"仅用于门禁工程识别算子适配芯片版本，不影响算子业务执行逻辑"），不代表算子跑在 AICore 上。算子真正的执行引擎由 json 的 `engine = DNN_VM_AICPU` 决定。这是读昇腾工程时常见的"历史命名陷阱"：字段名会撒谎，注释和 json 才是权威。

---

### 4.3 AICPU Kernel 精读：六阶段流水与 metadata 内存契约

#### 4.3.1 概念说明

Kernel 主体约 600 行纯 C++，做一件事：**把"每个核分到哪段工作"算清楚，写进 1024 个 int32**。先立起输出端的内存契约（这是与消费方 pioneer Kernel 的跨算子协议）：

- 平台常量：[ai_infra_attention_pioneer_metadata.h:L24-L28](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_host/arch35/ai_infra_attention_pioneer_metadata.h#L24-L28) 定义 A5 平台 36 个 AIC 核、72 个 AIV 核，\( \text{AP\_CV\_RATIO} = 72/36 = 2 \)（向量核与矩阵核 2:1 混排，与 u4-l3/u4-l8 讲的 AIC:AIV=1:2 混合核一致），metadata 总容量 `AP_METADATA_SIZE = 1024` 个 int32。
- 布局：每核占 10 个槽（`AP_SLOTS_PER_CORE`），36 核共 360 个 int；随后 16 个基础字段，共 376 个 int。任意"第 \(c\) 个核的第 \(f\) 个字段"的扁平下标为：

\[ \text{offset}(c, f) = c \times 10 + f \]

基础字段则从 `AP_BASE_OFFSET = 360` 起排（[L72-L81](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_host/arch35/ai_infra_attention_pioneer_metadata.h#L72-L81) 的注释即布局图）。
- 容量保护：[L92-L93](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_host/arch35/ai_infra_attention_pioneer_metadata.h#L92-L93) 的 `static_assert` 在编译期保证结构体不超 1024 个 int32——改字段数时忘了扩容量会直接编译失败，这是协议缓冲的好习惯。

#### 4.3.2 核心流程

六阶段的职责与关键产出：

| 阶段 | 函数 | 做什么 | 产出 |
| --- | --- | --- | --- |
| 1 取参 | `Prepare` | 绑定输入输出张量、读 15 个属性、算 \(g = N_1 / N_2\) | 成员变量就绪 |
| 2 判侧 | `CheckIsDecode` | 按 head_dim 判定 P 侧（prefill）还是 D 侧（decode/IFA） | `isDecode_` |
| 3 解析 | `ParseAxisInfo`/`ParseActualSeqLengths` | 从张量**值**还原每个 batch 的 Q/KV 长度，算 B/S1/S2/T1/T2 | `qSeqLens_`/`kvSeqLens_` |
| 4 定尺 | `SetTilingParams` | 定 sOuter×sInner tile 尺寸（P:128×128，D:64×128） | `sOuterSize_`/`sInnerSize_` |
| 5 分核 | `ComputeSplitNBSeq` | 统计总块数，按贪心把 (batch×head×sOuter) 行分给 36 核 | `splitCoreEnable_` 等三张表 |
| 6 落盘 | `GenMetaData` | 把三张表 + 16 个基础字段写进输出张量 | metadata[1024] |

阶段 5 的贪心负载均衡是算法核心。设单头总工作块数为 \(N_{blk}\)，参与切分的头数为 \(H\)（P 侧取 \(N_1\)，D 侧取 \(N_2\)），可用核数为 \(C\)，则每核的目标权重为：

\[ w_{\text{target}} = \frac{N_{blk} \times H}{C} \]

随后按 (batch, head, sOuter 行) 三层循环顺序遍历所有工作行，每行有 `actualInnerBlockNums` 个块；维护累积权重 `curWight`，当"把当前行塞进当前核导致的超载"（\(blocks - dif > dif\)，其中 \(dif = w_{target} \times (core+1) - curWight\)）超过"留给下一核的欠载"时，就开新核并记录该核的起始 `(b×H + head, sOuter)` 下标——这样每个核只靠"自己的起始下标 + 下一核的起始下标"就能圈出自己的工作区间，无需显式的区间表。

稀疏模式改变"一行能看到多少 KV 块"：`leftUpCausal`（mode 2）下每行能看到的块数随行号递增（因果三角），`band`（mode 4）下由 `pre_tokens/next_tokens` 定带宽；D 侧（decode）还要乘 GQA 组大小 \(g\) 换算到 KV 行坐标系。Sink Token（`k_sink_num=128`）会给每个有效行额外加上 \(\lceil kSinkNum / sInnerSize \rceil\) 个 sink 块。

#### 4.3.3 源码精读

**阶段 1 `Prepare`**——注意它把"必选/可选属性"用两个工具函数分开读（[cpu_context_util.h:L31-L41](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/arch35/cpu_context_util.h#L31-L41) 的 `GetAttrValue` 缺属性即报错；[L67-L75](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/arch35/cpu_context_util.h#L67-L75) 的 `GetAttrValueOpt` 缺省则保留默认值）：

[ai_infra_attention_pioneer_metadata_aicpu.cpp:L45-L89](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/arch35/ai_infra_attention_pioneer_metadata_aicpu.cpp#L45-L89) —— 绑定两个输入与输出张量并判空（`KERNEL_CHECK_NULLPTR`），读 4 个必选属性（头数/头维）与 11 个可选属性（含 aclnn 灌入的 `soc_version/aic_core_num/aiv_core_num`），校验核数非零、\(N_1 \% N_2 == 0\)，最后算出 \(g = N_1/N_2\)。防御式风格与 u3-l1 讲的 `OP_CHECK_IF` 同构，只是换成了 AICPU 侧的 `KERNEL_LOG_ERROR`。

**阶段 2 `CheckIsDecode`**：

[L93-L100](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/arch35/ai_infra_attention_pioneer_metadata_aicpu.cpp#L93-L100) —— 当 `headDimQK == 512 && headDimV == 512 && ropeHeadDim == 64` 时判定为 D 侧（IFA/MLA decode）。这正是 u4-l8 讲过的 D 侧签名（d=512、rope 分离 64 维），P 侧则是 192/128。D 侧的 Q 长度随后要乘 \(g\)（MQA/GQA 组展开，见下）。

**阶段 3 `ParseActualSeqLengths`**——全算子最"业务"的一段，处理两种排布的语义差异：

[L153-L160](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/arch35/ai_infra_attention_pioneer_metadata_aicpu.cpp#L153-L160) —— 直接把张量数据指针 cast 成 `const int64_t*` 读取（AICPU 才能这么干；AICore kernel 要 DataCopy 到 UB）。`isPA = blockSize_ > 0` 区分 Paged Attention。

[L164-L188](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/arch35/ai_infra_attention_pioneer_metadata_aicpu.cpp#L164-L188) —— 对原始数据做值校验：TND 前缀和格式必须非负且单调不降；KV 在非 PA 场景同样要求单调不降（PA 场景 KV 是逐 batch 实际值，允许任意非负）。这些约束与 docs 的"约束说明"一一对应。

[L193-L238](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/arch35/ai_infra_attention_pioneer_metadata_aicpu.cpp#L193-L238) —— TND 分支把前缀和差分成逐 batch 长度（`qSeqLens_[i] = qData[i] - qData[i-1]`），D 侧再乘 \(g\)；非 TND 分支直接读值。最后 [L237-L238](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/arch35/ai_infra_attention_pioneer_metadata_aicpu.cpp#L237-L238) 取原始数组的最后一个元素作为 \(T_1/T_2\)（TND 排布下的总 token 数，即 GM 跨 batch 步长）。

**阶段 5 `ComputeSplitNBSeq`**——贪心分核两阶段：

[L415-L438](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/arch35/ai_infra_attention_pioneer_metadata_aicpu.cpp#L415-L438) —— Phase 1：对每个 batch 调 `GetPreNextTokensLeftUp` + `FixParamWithRowInvalid` 修正无效行（并置 `needInit` 标志），再累加 `GetCalcBlockNumsOneHead` 的块数与 sink 块数，得到单头总块数。

[L441-L442](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/arch35/ai_infra_attention_pioneer_metadata_aicpu.cpp#L441-L442) —— 计算目标权重 \(w_{target} = N_{blk} \times H / C\)（浮点）。

[L462-L531](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/arch35/ai_infra_attention_pioneer_metadata_aicpu.cpp#L462-L531) —— Phase 2：三层循环（batch × head × sOuter 行），每行按稀疏模式算 `actualInnerBlockNums`（D 侧走 [L482-L495](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/arch35/ai_infra_attention_pioneer_metadata_aicpu.cpp#L482-L495) 的 GQA 坐标换算，P 侧走 [L497-L507](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/arch35/ai_infra_attention_pioneer_metadata_aicpu.cpp#L497-L507) 的带状区间计数），然后在 [L511-L521](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/arch35/ai_infra_attention_pioneer_metadata_aicpu.cpp#L511-L521) 做开核决策并记录新核的 `(bnStart, gs1Start)`。行间推进 `preTokensLeftUp -= sOuterSize; nextTokensLeftUp += sOuterSize`（[L527-L528](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/arch35/ai_infra_attention_pioneer_metadata_aicpu.cpp#L527-L528)）体现了"每往下走一行 Q，能看到的 KV 窗口整体右移一个 tile"的因果带直觉。

[L533-L540](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/arch35/ai_infra_attention_pioneer_metadata_aicpu.cpp#L533-L540) —— 收尾写"边界哨兵"：pioneer Kernel 会读 `bnStartIdx[aicIdx + 1]` 来确定自己区间的终点，所以要在 `[splitUsedCoreNum_]` 处写入全局末端的边界值——典型的"末尾多放一个元素"区间表示法。

**阶段 6 `GenMetaData`**：

[L545-L606](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/arch35/ai_infra_attention_pioneer_metadata_aicpu.cpp#L545-L606) —— 把输出张量数据指针 cast 成 `detail::APMetaData*` 直接按结构体写：先填 36 个核的槽位（L551-L555），再填 16 个基础字段（L557-L603），其中 [L581-L582](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/arch35/ai_infra_attention_pioneer_metadata_aicpu.cpp#L581-L582) 把实际向量核数写成 \( \text{usedCore} \times 2 \)（AIC:AIV=1:2 混排的换算），`needInit`（L603）标记存在无效行、Attention 侧需要先把输出刷零。

**注册收尾**：

[L608](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/arch35/ai_infra_attention_pioneer_metadata_aicpu.cpp#L608) —— `REGISTER_CPU_KERNEL(K_AI_INFRA_ATTENTION_PIONEER_METADATA, AiInfraAttentionPioneerMetadataKernel)` 把字符串算子名（注意是 `AiInfraAttentionPioneerMetadata`，大驼峰，与 proto 的 `REG_OP` 同名、与目录名 `ai_infra_attention_pioneer_metadata` 不同）绑到 Kernel 类，等价于 Ascend C 世界的 `IMPL_OP_OPTILING`/`OP_ADD`。

**消费方如何读这份契约**（跨算子对齐的现场）：

[ai_infra_attention_pioneer_kernel_base.h:L251](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_kernel_base.h#L251) —— pioneer Kernel 在 Init 时把 metaData 指针 `SetGlobalBuffer` 成 GM 上的 int32 视图；[L875-L916](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_kernel_base.h#L875-L916) 逐字段读出 `USED_CORE_NUM/S1_SIZE/T1_SIZE/needInit` 及每核的 `BN2_START_PTR/GS1_START_PTR`，据此反推本核工作区间。它引用的枚举来自 [ap_metadata_defs.h:L27-L37](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ap_metadata_defs.h#L27-L37)——一份**手工镜像**的 `PerCoreField/BaseMetaField`（源注释写明 "mirror ... in optiling"）。也就是说：metadata 的生产者（本算子 `op_host/arch35/ai_infra_attention_pioneer_metadata.h`）与消费者（pioneer 的 `ap_metadata_defs.h`）各持一份布局定义，**改布局必须两边同步改**，没有编译期保护（只有各自文件内的 static_assert）——这是阅读/二次开发时的高危点。顺带一提，生产者头文件里那组 `__aicore__ inline` 取偏移助手被 `#ifdef __CCE_AICORE__` 包住（[L101-L121](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_host/arch35/ai_infra_attention_pioneer_metadata.h#L101-L121)），AICPU 侧编译时并不可见，真正给 AICore 用的就是消费者那份镜像。

#### 4.3.4 代码实践

**实践目标**：用 Python 复刻简化版"阶段 3 + 阶段 5"，验证你对分核算法的理解（源码阅读型实践，CPU 即可运行，不涉及 NPU）。

**操作步骤**：

1. 先手算一个小案子（与 ST 用例 [test_ai_infra_attention_pioneer_metadata.py:L34-L43](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/tests/st/test_ai_infra_attention_pioneer_metadata.py#L34-L43) 同参数族）：P 侧、`sparse_mode=2`（leftUpCausal）、B=2、`S_q=[128,256]`、`S_kv=[128,256]`、`num_heads=4`、核数 36、tile 128×128。leftUpCausal 下第 \(i\) 个 sOuter 行覆盖 KV 块 \(0..i\)，所以 batch0 贡献 1 块、batch1 贡献 \(1+2=3\) 块，单头共 4 块，4 头共 16 块，\(w_{target}=16/36\approx0.44\)。
2. 写示例代码模拟 Phase 2 的贪心循环（示例代码，非仓库原有）：

```python
# 示例代码：简化版 ComputeSplitNBSeq（仅 P 侧 leftUpCausal，无 sink、无无效行修正）
import math
ceildiv = lambda a, b: (a + b - 1) // b

def split(sq, skv, heads, cores=36, s_outer=128, s_inner=128):
    rows = []                                # (batch, head, sOuter 行号, 该行块数)
    for b in range(len(sq)):
        for h in range(heads):
            for i in range(ceildiv(sq[b], s_outer)):
                rows.append((b, h, i, i + 1))  # leftUpCausal: 行 i 覆盖 KV 块 0..i
    total = sum(r[3] for r in rows)
    w_target = total / cores
    enable, bn, gs1, cur_core, cur_w = [0]*cores, [0]*cores, [0]*cores, 0, 0
    enable[0] = 1
    for b, h, i, blocks in rows:
        dif = w_target * (cur_core + 1) - cur_w
        if blocks - dif > dif and (bn or gs1):     # 超载则开新核（首行除外）
            cur_core += 1
            if cur_core < cores:
                enable[cur_core] = 1
                bn[cur_core] = b * heads + h
                gs1[cur_core] = i
        cur_w += blocks
    return total, enable, bn, gs1

total, enable, bn, gs1 = split([128, 256], [128, 256], heads=4)
print("total blocks:", total)                 # 预期 16
print("used cores:", sum(enable), "bnStart:", [x for x in bn if x], )
```

3. 运行脚本，检查三条不变量：① 所有行的块数之和等于 Phase 1 的 `total`；② `usedCoreNum = sum(enable) ≤ 36`；③ `bnStart/gs1Start` 按核序号单调不降（贪心顺序遍历的必然结果）。

**需要观察的现象**：总块数恰为手算的 16；由于 \(w_{target} < 1\)，贪心倾向把多数行压在前几个核上（每行只有 1~3 块，超载判定很难触发），`usedCoreNum` 会远小于 36——这解释了小 shape 场景下"用不满芯片"是算法的正常行为而非 bug。

**预期结果**：脚本输出满足三条不变量，且与手算的 total=16 一致。注意本脚本是**简化模型**（忽略了 `FixParamWithRowInvalid` 的无效行修正、sink 块累加、D 侧 GQA 换算），用于建立直觉；与 NPU 上真实 metadata 的逐值对比**待本地验证**（需按 u8-l4 的方式在 950 环境跑 ST 后 dump 张量）。

#### 4.3.5 小练习与答案

**练习 1**：P 侧与 D 侧的 tile 尺寸分别是什么？为什么 D 侧的 sOuter 更小？

**答案**：见 [SetTilingParams:L245-L256](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/arch35/ai_infra_attention_pioneer_metadata_aicpu.cpp#L245-L256)：P 侧 128×128，D 侧 64×128。D 侧是 decode 场景，单步 Q 极短（常为 1 token × G 头展开），M 维（sOuter 方向）天然细碎，用更小的 sOuter 让块粒度匹配短序列、减少空算；KV 维（sInner）两侧一致取 128，与 KV cache 按 block 组织的方式对齐。

**练习 2**：`FixParamWithRowInvalid` 会在什么情况下置 `needInit = true`？下游拿它做什么？

**答案**：见 [L293-L317](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/arch35/ai_infra_attention_pioneer_metadata_aicpu.cpp#L293-L317)：当某些 Q 行在稀疏窗口下**一行有效 KV 都够不着**（`nextTokensError/preTokensError > 0`）或该 batch 的 KV 长度为 0 时，这些行的输出是"无效行"，置 `needInit`。下游 pioneer Kernel 读到 `NEED_INIT`（[kernel_base.h:L888](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_kernel_base.h#L888)）后要先把对应输出位置刷零，否则无效行会残留未初始化数据——因为正常计算路径根本不会碰这些行。

**练习 3**：`GenMetaData` 里 `ACTUAL_CORE_NUMS` 为什么写成 `splitUsedCoreNum_ * AP_CV_RATIO`？

**答案**：见 [L580-L582](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/arch35/ai_infra_attention_pioneer_metadata_aicpu.cpp#L580-L582)：分核按 36 个 AIC（Cube）核规划（每个 AIC 区间再带 2 个 AIV 组成 1:2 混合核，u4-l3/u4-l8 的混合核模型），而下游 Vector 侧的任务分发按 AIV 核号进行，所以要把 Cube 核数 ×2 换算成"实际向量核数"。字段名带 ACTUAL 正是提醒这个换算后的语义。

---

### 4.4 op_api 两级封装：l0op 的 AICPU 下发与 aclnn 的核数回填

#### 4.4.1 概念说明

本算子的 `op_api/` 有四个文件，两级分工（承接 u2-l5 的 L0/L2 分层）：

- **L0（`l0_ai_infra_attention_pioneer_metadata.cpp/.h`，`namespace l0op`）**：把算子构造成一个 **AICPU 任务**挂进 executor。它与 AICore 算子的区别只在宏：`ADD_TO_LAUNCHER_LIST_AICPU` vs u2-l5 见过的 `ADD_TO_LAUNCHER_LIST_AICORE`。
- **L2（`aclnn_ai_infra_attention_pioneer_metadata.cpp/.h`，`extern "C"`）**：对外两段式 aclnn 接口。第一段做参数校验并**在运行期回填 AIC/AIV 核数**，第二段照旧 `CommonOpExecutorRun` 下发。

`l0_` 前缀文件的意义：多数 Ascend C 算子的 aclnn 源码由 CANN 的 op_build 工具从 `_def.cpp` 自动生成（u1-l4），不入仓；本算子因为要在 aclnn 层插入"查平台核数"的自定义逻辑，L0 与 L2 都手工入仓——这与 u2-l5 的 flash_attention_score_enhance（因自定义布局预处理而入仓）是同一个入仓理由：**需要手写适配逻辑的算子才把 op_api 源码放进仓库**。

#### 4.4.2 核心流程

```
aclnnAiInfraAttentionPioneerMetadataGetWorkspaceSize(...)
  ├─ CREATE_EXECUTOR()                     # 建 executor
  ├─ ParamsCheck(...)                      # 17 项参数校验
  ├─ GetCurrentPlatformInfo()              # 查 socVersion
  ├─ aclrtGetResInCurrentThread(CUBE/VECTOR)# 查本线程可用核数（控核）
  │    └─ 拿不到 → 平台默认 GetCubeCoreNum/GetVectorCoreNum
  ├─ l0op::AiInfraAttentionPioneerMetadata(...)  # 传入 socVersion + 核数
  │    └─ ADD_TO_LAUNCHER_LIST_AICPU(...)        # 记为 AICPU 任务
  └─ *workspaceSize = 0; ReleaseTo(executor)     # AICPU 无需 workspace

aclnnAiInfraAttentionPioneerMetadata(workspace, size, executor, stream)
  └─ CommonOpExecutorRun(...)              # 统一下发
```

注意两个"AICPU 特色"：`workspaceSize` 恒为 0（C++ Kernel 自己管理内存，不需要框架预留 workspace）；`static internal::AicpuTaskSpace space(...)` 声明任务空间类别。

#### 4.4.3 源码精读

**L0 层全貌**：

[l0_ai_infra_attention_pioneer_metadata.cpp:L27-L28](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_api/l0_ai_infra_attention_pioneer_metadata.cpp#L27-L28) —— `OP_TYPE_REGISTER(AiInfraAttentionPioneerMetadata)` 先注册算子类型。

[L29-L48](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_api/l0_ai_infra_attention_pioneer_metadata.cpp#L29-L48) —— 函数签名：19 个参数里 2 个输入张量、15 个标量（含 aclnn 灌入的 `socVersion/aicCoreNum/aivCoreNum`）、输出张量、executor；返回输出张量指针（空指针即失败信号）。

[L56-L73](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_api/l0_ai_infra_attention_pioneer_metadata.cpp#L56-L73) —— 核心的 `ADD_TO_LAUNCHER_LIST_AICPU`：四个子宏各司其职——`OP_ATTR_NAMES` 列出属性**名字符串表**（顺序与 `OP_ATTR` 的值表严格对齐，正是 proto 里那 15 个属性）、`OP_INPUT` 列输入张量、`OP_OUTPUT` 列输出、`OP_ATTR` 列属性**值**。名字表与值表按下标配对是这类宏最容易出错的地方。

**aclnn 第一段的校验**：

[aclnn_ai_infra_attention_pioneer_metadata.cpp:L40-L101](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_api/aclnn_ai_infra_attention_pioneer_metadata.cpp#L40-L101) —— `ParamsCheck` 把 docs 约束翻译成代码：张量判空、`DT_INT64` 类型检查（L64-65）、头数整除、`sparse_mode` 只许 2/4（L77-80）、\(-next\_tokens \le pre\_tokens\)（L83）、layout 只许 TND/TND_NTD（L86-89）、sink 只许 0/128（L92）。**注意一个文档矛盾**：docs 说序列张量"数据类型支持 int32"且示例用 `torch.int32`，但 proto 声明 `DT_INT64`、aclnn 校验 `DT_INT64`、AICPU kernel 读 `const int64_t*`——三处源码一致要求 int64，docs 示例若照抄会过不了校验。以源码为准。

**运行时核数回填（本算子最特别的逻辑）**：

[L143-L166](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_api/aclnn_ai_infra_attention_pioneer_metadata.cpp#L143-L166) —— 先取平台信息拿 `socVersion`；再调 `aclrtGetResInCurrentThread(ACL_RT_DEV_RES_CUBE_CORE/VECTOR_CORE, ...)` 查"当前线程被允许用的核数"（对应"控核"特性：容器/进程可被限制只用部分芯片资源），查到有效值就**覆盖调用方传入的核数参数**，查不到回退平台默认。然后把 socVersion 与核数一并传给 L0。torch 侧 csrc 传的 `aicCoreNum=32/aivCoreNum=64`（[npu_ai_infra_attention_pioneer_metadata.cpp:L38-L39](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/attention/ai_infra_attention_pioneer_metadata/csrc/npu_ai_infra_attention_pioneer_metadata.cpp#L38-L39)）只是占位值，到这里必然被替换——**核数的真值来源在 aclnn 层**，这是读这条调用链时的关键认知。

[L169-L171](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_api/aclnn_ai_infra_attention_pioneer_metadata.cpp#L169-L171) —— `*workspaceSize = 0` 后释放 executor：AICPU 任务的执行上下文全在 executor 里，无需额外 workspace。

**第二段**照旧是 4 行：

[L174-L179](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_api/aclnn_ai_infra_attention_pioneer_metadata.cpp#L174-L179) —— `aclnnAiInfraAttentionPioneerMetadata` 仅调 `CommonOpExecutorRun(workspace, workspaceSize, executor, stream)`，与 u2-l5 讲的两段式执行完全同构。

**torch 侧入口**（把链路接回 python）：

[npu_ai_infra_attention_pioneer_metadata.cpp:L41-L77](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/torch_ops_extension/omni_training_custom_ops/ops_transformer/attention/ai_infra_attention_pioneer_metadata/csrc/npu_ai_infra_attention_pioneer_metadata.cpp#L41-L77) —— TORCH_CHECK 前置校验（含 `block_size` 16 对齐、≤1024），按输入设备分配 `int32[1024]` 输出，再 `EXEC_NPU_CMD_V1(aclnnAiInfraAttentionPioneerMetadata, ...)` 桥到 aclnn（u6-l2 详述过该机制）。值得对比：csrc 层允许 `sparse_mode ∈ [0,4]`，而 aclnn 层只放行 2/4——**外宽内严**，最终约束以最内层为准，这也是读多层校验时的通用规律。

#### 4.4.4 代码实践

**实践目标**：理解 op_api UT 如何在无 NPU 环境下测这条链路（呼应 u3-l4 stub 机制与 u8 单元）。

**操作步骤**：

1. 阅读 [ut_stub_metadata.cpp:L15-L56](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/tests/ut/op_api/ut_stub_metadata.cpp#L15-L56)，列出它替身的三个外部符号及各自行为：`aclrtGetResInCurrentThread`（返回成功且 `*value=36`，恰好覆盖 aclnn L151/L156 的 `>0` 分支）、`l0op::AiInfraAttentionPioneerMetadata`（直接 `return metaData`）、`CommonOpExecutorRun`（返回 0）。
2. 回答：为什么 stub 把核数固定成 36 而不是 0？——若返回 0，aclnn 会走"回退平台默认核数"分支，`npuInfo.GetCubeCoreNum()` 这条路径也需要被 stub，测试就多一个替换点；给 36 让代码走"采纳控核值"分支，链路最短。
3. 对照 [tests/ut/op_api/](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/tests/ut/op_api/test_aclnn_ai_infra_attention_pioneer_metadata.cpp) 的用例，确认它断言的是 `GetWorkspaceSize` 返回 `ACLNN_SUCCESS` 这类**流程性结果**，而非 metadata 数值。

**需要观察的现象**：UT 对 AICPU 算子只验证"参数校验 + 任务构造"路径；metadata 的数值正确性只能靠 950 环境上的 ST（[tests/st/test_ai_infra_attention_pioneer_metadata.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/tests/st/test_ai_infra_attention_pioneer_metadata.py) 也只断言 shape/dtype）——与 u3-l4 的结论"桩只验证流程与分支，数值精度归 ST"一致。

**预期结果**：一张「stub 符号 → 被替掉的真实现 → 覆盖的代码分支」对照表。纯阅读实践，无需运行；若要实际跑 UT，需按 u8-l4 的 `build.sh -u` 流程在 CANN 容器内执行（本讲未运行，输出**待本地验证**）。

#### 4.4.5 小练习与答案

**练习 1**：`ADD_TO_LAUNCHER_LIST_AICPU` 的 `OP_ATTR_NAMES` 与 `OP_ATTR` 两个子宏是什么关系？写错顺序会发生什么？

**答案**：前者是属性名字符串数组，后者是属性值数组，两者**按下标一一配对**后写进 AICPU 任务（[L58-L68](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_api/l0_ai_infra_attention_pioneer_metadata.cpp#L58-L68)）。顺序错位不会编译失败——任务会带着"名字 A 配了名字 B 的值"下发给 kernel，`GetAttrValue(ctx, "num_heads_q", ...)` 读到错误数值，属于典型的静默出错；防御手段是加 L0_DFX 打印核对（本文件 L50-L54 正是这么做的）。

**练习 2**：为什么本算子 `workspaceSize = 0`，而 u2-l5 的 FA 算子需要真实的 workspace？

**答案**：workspace 是框架为 AICore kernel 的多核协作/中间数据预留的 device 内存（FA 的 drop_mask_adapter 就把展开后的掩码写进 workspace，见 u4-l3）。AICPU 算子是单线程 C++ 程序，中间状态用局部 `std::vector`（如 `qSeqLens_`）放在 AICPU 自己的栈/堆上，输入输出直连张量内存，不需要框架代管 workspace。

**练习 3**：docs 的调用示例用 `sparse_mode=4, pre_tokens=512, next_tokens=0`，`(-next_tokens) <= pre_tokens` 是否满足？若用户传 `pre_tokens=0, next_tokens=128` 呢？

**答案**：前者 \(-0 = 0 \le 512\) 满足。后者 \(-128 \le 0\) 也满足不等式本身，但语义上"band 下边界高于上边界"会在 kernel 侧被 `FixParamWithRowInvalid` 判为无效行（needInit）。aclnn 的校验（[L83](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_api/aclnn_ai_infra_attention_pioneer_metadata.cpp#L83)）只拦最基础的矛盾（\(-next > pre\) 时直接 `ACLNN_ERR_PARAM_INVALID`），更深的语义修正留给 kernel——分层校验的边界感。

---

## 5. 综合实践

综合实践分两部分，产出一份 markdown 文档（建议存为自己的学习笔记，不放进仓库）。

### 任务一：三种算子形态对比短文

以 **metadata（AICPU）**、**aggregate_hidden（AICore AscendC）**、**ai_infra_pypto_qat（pypto）** 为例，各列三条"开发 / 编译 / 调试"差异。参考提纲与答案要点（写短文时可展开成自己的话）：

**AICore AscendC —— `ai_infra_aggregate_hidden`**

1. **开发**：C++ 模板类 + Ascend C 原语（TPipe/TQue/DataCopy/Cast），Host 侧配 tiling.cpp（TilingContext → tilingKey/TilingData/blockDim），跨侧契约靠约定对齐（u2-l3/u2-l4）。
2. **编译**：bisheng 编出 per-soc 二进制；芯片白名单写在 def 的 [AICore().AddConfig("ascend910b"/"ascend910_93"):L83-L84](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/mome/ai_infra_aggregate_hidden/op_host/ai_infra_aggregate_hidden_def.cpp#L83-L84)，产物经 CPack 封装成 run 包（u1-l4）。
3. **调试**：行为由 Host tiling 与 Device kernel 两侧共同决定，tilingKey 失配会静默出错；流程靠 faker/stub UT，精度靠 ST（u8 单元）。

**AICPU —— `ai_infra_attention_pioneer_metadata`**

1. **开发**：继承 `aicpu::CpuKernel` 重写 [Compute(CpuKernelContext&):L26-L41](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/arch35/ai_infra_attention_pioneer_metadata_aicpu.cpp#L26-L41)，纯串行 C++，直接解引用数据指针、用 `std::vector/std::max`，无 tiling/tilingKey/Ascend C 原语。
2. **编译**：进 [op_kernel_aicpu/CMakeLists.txt:L11-L31](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_kernel_aicpu/CMakeLists.txt#L11-L31) 的 `add_aicpu_cust_kernel_modules`，产出 `libtransformer_aicpu_kernels.so`；引擎绑定靠 json（`DNN_VM_AICPU/CUSTAICPUKernel`），芯片清单靠 config.ini（仅门禁用）。
3. **调试**：单侧执行、心智模型就是普通 C++；控制流密集逻辑可用 Python/numpy 在 CPU 上复刻验证（见 4.3.4）；无跨侧 tilingKey 一致性问题，但有"输出布局与消费方镜像枚举需手工同步"的协议风险。

**pypto —— `ai_infra_pypto_qat`**

1. **开发**：Python DSL，[ai_infra_pypto_qat.py:L15-L21](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/pypto/src/ops-nn/quant/ai_infra_pypto_qat/op_code/ai_infra_pypto_qat.py#L15-L21) 用 `@pypto.frontend.jit` 装饰 kernel、以 `pypto.Tensor` 形状注解 + view/mul/div/round 原语组织计算（u7-l1 详述）。
2. **编译**：由 pypto 前端 JIT 生成设备代码，不走本仓库的 CMake/bisheng 链路，没有 `_def.cpp`/json/config.ini 那套注册件。
3. **调试**：Python 生态，kernel 与 wrapper 同文件（如 L21 kernel 与 L77 的 `ai_infra_qat_asymmetric_per_group` 封装），可直接用 torch 参考实现做逐值对比，ST 用 pytest 组织（u7-l4）。

写作要求：每条差异必须附一个源码事实（文件:行号或本讲义引用），不许写空泛的"性能不同、语言不同"。

### 任务二：metadata 输出的 shape 推导说明

依据 infershape 源码写一段"为什么输出永远是 `[1024]` 的 int32"：

1. [InferShape（infershape.cpp:L21-L27）](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_host/arch35/ai_infra_attention_pioneer_metadata_infershape.cpp#L21-L27) 只做两步：`SetDimNum(1)` 定一维，`SetDim(0, AP_METADATA_SIZE)` 定长度 1024——**从不读输入 shape**，输出与 B/S/N/D 全部无关。
2. dtype 由 [InferDataType（L29-L33）](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_host/arch35/ai_infra_attention_pioneer_metadata_infershape.cpp#L29-L33) 固定为 `DT_INT32`（调度表是整数下标与标志位）。
3. 容量合法性由编译期 [static_assert（metadata.h:L92-L93）](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_metadata/op_host/arch35/ai_infra_attention_pioneer_metadata.h#L92-L93) 兜底：实际结构 \(36 \times 10 + 16 = 376\) 个 int32 ≤ 1024，剩余 648 个是留给未来字段的余量。
4. 结论：这是一个**协议缓冲型输出**——框架在图编译期就能确定它的 shape（好性质：不引起重编译），真实内容（用几核、每核从哪开始）推迟到 AICPU 执行时才填。把这条性质与你从 4.1 练习 2 得到的 docs 滞后问题写在一起，作为"以源码为准"的案例积累。

## 6. 本讲小结

- **适用边界**：AICore 管稠密计算（Matmul/向量），AICPU 管控制流；`ai_infra_attention_pioneer_metadata` 是全仓库唯一 AICPU 算子，用串行 C++ 贪心分核解决"真实序列长度在 device 侧、Host tiling 算不了精确分核"的问题——本质是一次**设备侧 tiling**。
- **注册三件套**：`op_graph` 的 `REG_OP`（= `_def.cpp` 的角色）、aicpu json（= 引擎/内核库绑定，对应 `AICore().AddConfig` 的另一半职责）、`config.ini`（仅门禁识别芯片，字段名 `aicore_versions` 是历史命名陷阱）。`op_host/` 只有 infershape、没有 tiling。
- **Kernel 六阶段**：Prepare → CheckIsDecode（head_dim 512/512/64 判 D 侧）→ ParseAxisInfo（前缀和差分 + 单调性校验）→ SetTilingParams（P 128×128 / D 64×128）→ ComputeSplitNBSeq（目标权重 \(w_{target}=N_{blk}H/C\) 的贪心分核 + 边界哨兵）→ GenMetaData（写协议缓冲）。
- **内存契约**：metadata = `int32[1024]` 协议缓冲，布局为 36 核 × 10 槽 + 16 个基础字段（376 个 int），偏移公式 \( \text{offset}(c,f)=c\times10+f \)；生产者与消费者（pioneer 的 `ap_metadata_defs.h`）**各持一份镜像定义、须手工同步**——改布局的高危点。
- **op_api 分层**：L0 用 `ADD_TO_LAUNCHER_LIST_AICPU` 挂任务（名字表/值表按下标配对）；aclnn 第一段校验 17 项参数并**运行时回填 AIC/AIV 核数**（控核 API 优先、平台默认兜底），`workspaceSize=0`；第二段照旧 `CommonOpExecutorRun`。
- **两处"文档滞后"实证**：docs 声称序列张量支持 int32（源码三处一致要求 int64）；docs 的返回值描述与源码布局注释不一致。接口契约以 proto/aclnn/头文件为准。

## 7. 下一步学习建议

- **横向对照另一种"tiling 下沉"**：读 u9-l2（tiling_sink：设备侧 Tiling 下沉机制），比较"AICPU 算子产 metadata"与"tiling_sink_kernel 注册表"两种方案在注册方式、数据通路（张量 vs TilingData）上的取舍。
- **沿消费链继续**：带着 metadata 布局契约重读 [ai_infra_attention_pioneer_kernel_base.h:L875-L916](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_kernel_base.h#L875-L916)，验证 u4-l8 讲的"kernel 读 metadata 回填分核"每一行字段消费。
- **进入第 5 单元**：Attention 族（9 个算子）已全部走完，下一讲 u5-l1 转向 MHC 算子族，从算法背景（Sinkhorn 双随机矩阵）重新开始一个"文档 → tiling → kernel"的完整精读循环；如果更关心测试，也可先跳到 u8-l1 看 UT 框架如何同时服务这三种算子形态。
