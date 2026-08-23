# AttentionPioneer（arch35）：新一代注意力前向与反向

> 本讲对应的算子目录是 `ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer`（前向）与 `ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_backward`（反向）。
>
> **先做一个重要纠偏**：项目大纲里把 arch35 标注为 "A3"，但源码与官方文档一致表明：**arch35 目录面向的是 Davinci 3.5 架构（`NpuArch::DAV_3510`，`__CCE_AICORE__ == 310`），即 Ascend 950（950PR / 950DT）平台**；A3、A2 等平台在文档的支持矩阵中明确标记为不支持。本讲以源码为准，并在 4.1 中给出证据链。这也是一次很好的"文档交叉验证"示范：**讲义不能只抄大纲，必须回到源码求证**。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `ai_infra_attention_pioneer` 算子的定位：它是面向 Ascend 950 的"新一代" FlashAttention 前向算子，支持 MLA 的 P 侧（prefill/decode 统一，TND 布局）与 D 侧（decode，TND_NTD 布局 + Paged Attention）两大场景，并原生支持 Sink Token 稀疏注意力。
2. 跟踪完整的注册与调用链：`op_def` → `tiling_register` → `tiling_v2` → `entry_regbase` → `FAKernelNoquantMla`，理解 host 与 device 之间通过 **CANN 标准 TilingData 结构 `FlashAttentionScoreSimplifiedTilingData`** 传递切分参数的"适配器"模式。
3. 理解 12 参数 tilingKey 如何把 host 侧的算子形态编译成 kernel 模板参数，以及 `PARSE_PARAMS_NoQuant` 如何在 device 侧把它拆回来。
4. 精读混合核 kernel `FAKernelNoquantMla`：AIC:AIV = 1:2 的 Cube/Vector 分工、metadata 张量驱动的设备侧分核、L1 三缓冲 + UB 双缓冲的流水线设计。
5. 对比 `flash_attention_interface.cpp` 中 CUTLASS 风格的另一条实现路线，理解同一算子目录下"regbase 手写流水线"与"模板元编程组装"两种风格的取舍。
6. 通过综合实践，盘点反向算子 `matmul_modules` 的目录组织，写出一篇"前反向 kernel 模块复用"的说明。

本讲建立在 u4-l3（FA 前向 Kernel 的分块与在线 softmax）与 u3-l3（tiling_base 框架）之上，不再重复 FlashAttention 的基础原理与 tiling 框架的通用流程。

## 2. 前置知识

- **FlashAttention 在线 softmax（u4-l3 已讲）**：注意力 \( O = \mathrm{softmax}(S)\,V \)，其中 \( S = QK^\top/\sqrt{d} + M \)。分块计算时对每个 S2 块维护行最大值 \( m^{(t)} \) 与行和 \( \ell^{(t)} \)，用指数修正把历史输出缩放到新基准：
  \[
  m^{(t)} = \max\bigl(m^{(t-1)},\ \max_j s_j^{(t)}\bigr),\qquad
  o^{(t)} = e^{m^{(t-1)}-m^{(t)}} o^{(t-1)} + e^{s^{(t)}-m^{(t)}} v^{(t)}
  \]
- **Sink Token 注意力**：把序列开头的若干 token（如系统提示）单独切出来拼在 K/V 前面，等效于
  \[
  K_{\mathrm{eff}} = \begin{bmatrix} K_{\mathrm{sink}} \\ K \end{bmatrix},\quad
  V_{\mathrm{eff}} = \begin{bmatrix} V_{\mathrm{sink}} \\ V \end{bmatrix},\quad
  O = \mathrm{softmax}\!\bigl(QK_{\mathrm{eff}}^\top/\sqrt{d} + M\bigr)V_{\mathrm{eff}}
  \]
  本算子中 sink 由 `key_sink / key_rope_sink / value_sink` 三个可选输入承载（见 4.1）。
- **MLA（Multi-head Latent Attention）两种形态**：
  - **P 侧（PFA，prefill/decode 统一）**：TND 布局，Q/K 的 head 维 \( d = 192 \)（128 非 rope 部分 + 64 rope 部分拼在一起），V 的 \( d_v = 128 \)，\( N_q = N_{kv} \)（非 GQA），不支持 Paged Attention；
  - **D 侧（IFA，decode）**：TND_NTD 布局，\( d = 512 \)，\( d_v = 512 \)，\( N_{kv} = 1 \)，必须使用 Paged Attention（`block_table` + KV Cache），rope 部分单独存放（`query_rope / key_rope`，\( d_{rope} = 64 \)）。
- **混合核（AIC + AIV）**：Ascend C 里 Cube 核负责矩阵乘（MatMul），Vector 核负责逐元素/归约（softmax、exp、缩放）。`KERNEL_TYPE_MIX_AIC_1_2` 表示 1 个 Cube 核配 2 个 Vector 核的混合任务类型，`CV_RATIO = 2` 即这个比例。
- **TilingData 与 GET_TILING_DATA_WITH_STRUCT（u3-l3 已讲）**：host 侧 Tiling 函数把切分参数序列化，kernel 侧用宏取回同一结构。本讲的特殊之处在于：host 侧结构（`PromptFlashAiInfraAttentionPioneerTilingData`）与 device 侧结构（CANN 的 `FlashAttentionScoreSimplifiedTilingData`）**不是同一个**，中间隔了一层转换（见 4.3）。

## 3. 本讲源码地图

前向算子根目录：`ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer`

| 文件 | 作用 |
| --- | --- |
| `op_api/aclnn_ai_infra_attention_pioneer.h` | aclnn 两段式对外接口声明（GetWorkspaceSize + aclnn 执行） |
| `op_api/ai_infra_attention_pioneer.h/.cpp` | aclnn 实现（参数检查、executor 组装），本讲只作接口对照 |
| `op_host/ai_infra_attention_pioneer_def.cpp` | 算子原型定义：输入/输出/属性、dtype 列表、目标芯片注册 |
| `op_host/ai_infra_attention_pioneer_tiling_register.cpp` | `IMPL_OP_OPTILING` 注册 host tiling 入口 |
| `op_host/ai_infra_attention_pioneer_tiling.cpp` | tiling 薄封装（含 device tiling 导出符号） |
| `op_host/arch35/ai_infra_attention_pioneer_tiling_v2.h` | tiling 类声明：`AiInfraAttentionPioneerTilingV2` 及其子对象 |
| `op_host/arch35/ai_infra_attention_pioneer_tiling_v2.cpp` | **本讲主线 1**：DoOpTiling 校验、tiling 流水线、tilingKey 生成、TilingData 适配 |
| `op_host/arch35/ai_infra_attention_pioneer_tiling_struct.h` | host 侧枚举：InputLayout / TilingMod / SplitCoreMode |
| `op_kernel/ai_infra_attention_pioneer.cpp` | kernel 入口：12 个模板参数的 `extern "C"` 函数 |
| `op_kernel/arch35/ai_infra_attention_pioneer_entry_regbase.h` | **本讲主线 2**：按 tilingKey/dtype 分发到具体 kernel 实现 |
| `op_kernel/arch35/ai_infra_attention_pioneer_template_tiling_key_enum.h` | `PARSE_PARAMS_*` 宏：把合并模板参数拆成 constexpr |
| `op_kernel/arch35/ai_infra_attention_pioneer_kernel_noquant_mla.h` | **本讲主线 3**：`FAKernelNoquantMla` 混合核 kernel（Aligned576 MLA 路径） |
| `op_kernel/arch35/ai_infra_attention_pioneer_tiling_regbase.h` | device 侧 TilingData 镜像结构（TCubeTiling / SoftMaxTiling 等） |
| `op_kernel/arch35/ai_infra_attention_pioneer_common_regbase.h` | 公共常量、SparseMode/ImplMode 枚举、constexpr 工具 |
| `op_kernel/arch35/ap_metadata_defs.h` | metadata 张量的内存布局常量（与配套 AICPU 算子约定） |
| `op_kernel/flash_attention_interface.cpp` | **本讲主线 4**：CUTLASS 风格 `SplitFuse::FAInfer` 备选实现 |
| `docs/npu_ai_infra_attention_pioneer.md` | 官方约束文档：支持矩阵、两大场景参数、可运行示例 |

反向算子（仅综合实践中涉及）：`ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_backward/op_kernel/arch35/matmul_modules/`。

## 4. 核心概念与源码讲解

### 4.1 算子定位、目标芯片与 aclnn 接口

#### 4.1.1 概念说明

`ai_infra_attention_pioneer` 是 openPangu-2.0-Op 中最新一代注意力前向算子。它与传统 FA 算子的差别有三点：

1. **目标硬件是 Ascend 950（Davinci 3.5）**，混合核（AIC:AIV=1:2）编程模型从入口就确定下来；
2. **场景特化为 MLA**：P 侧（TND、d=192）与 D 侧（TND_NTD、d=512、Paged Attention）两条独立路径；
3. **Sink Token 原生支持**：`sparse_mode = 2`（leftUpCausal）与 `4`（band）配合 `sink_number`（0 或 128）。

另外它引入了一个**配套 AICPU 算子** `npu_ai_infra_attention_pioneer_metadata`，在设备侧产出一张 int32 的 metadata 张量（输入序号 28，shape `[1024]`），用来承载"依赖 `actual_seq_lengths`（设备侧张量）才能算出来"的切分信息——host tiling 拿不到设备张量的值，只能按最大 workspace 保守切分，真正精细的分核在 kernel 里读 metadata 完成（见 4.5）。

#### 4.1.2 核心流程

用户侧调用（aclnn 两段式）：

```text
aclnnAiInfraAttentionPioneerGetWorkspaceSize(输入张量组, 标量参数组, attentionOut, softmaxLse,
                                             &workspaceSize, &executor)   # 第一段：算 workspace
aclnnAiInfraAttentionPioneer(workspace, workspaceSize, executor, stream)  # 第二段：下发执行
```

host 内部链路：

```text
aclnn -> GE -> op_def(ascend950) -> TilingPrepareFor...(编译期)
                                  -> DoOpTiling(运行期) -> arch35 tiling_v2  -> entry_regbase -> kernel
```

#### 4.1.3 源码精读

**目标芯片注册**——整个算子只注册了 `ascend950` 一个目标，且 `OpAICoreConfig` 变量名就叫 `aicore_config_95`：

- [ai_infra_attention_pioneer_def.cpp:621](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/ai_infra_attention_pioneer_def.cpp#L621)：声明 `OpAICoreConfig aicore_config_95`，变量名里的 "95" 即 950。
- [ai_infra_attention_pioneer_def.cpp:2507](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/ai_infra_attention_pioneer_def.cpp#L2507)：`this->AICore().AddConfig("ascend950", aicore_config_95);` —— 全文件唯一一次 `AddConfig`，没有 `ascend910_93`/A3 的注册。
- [docs/npu_ai_infra_attention_pioneer.md:L5-L16](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/docs/npu_ai_infra_attention_pioneer.md#L5-L16)：产品支持矩阵，950PR/950DT 为 √，A3/A2 等为 ×。文档、def、tiling（4.2 将看到 `DAV_3510` 判断）、kernel（4.4 将看到 `__CCE_AICORE__ == 310` 判断）四处证据互相印证。

**算子原型要点**：

- [ai_infra_attention_pioneer_def.cpp:L26-L47](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/ai_infra_attention_pioneer_def.cpp#L26-L47)：query 为 REQUIRED，dtype 列表近 94 项（fp16/bf16/int8/fp8 等，量化路径由不同 tilingKey 分发）。
- [ai_infra_attention_pioneer_def.cpp:L562-L566](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/ai_infra_attention_pioneer_def.cpp#L562-L566)：`metadata` 输入（DT_INT32），即 4.1.1 所说的分核元数据。
- [ai_infra_attention_pioneer_def.cpp:L567-L581](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/ai_infra_attention_pioneer_def.cpp#L567-L581)：`key_sink / key_rope_sink / value_sink` 三个可选输入（FP16/BF16），Sink Token 的 K/V 载体。
- [ai_infra_attention_pioneer_def.cpp:L603-L619](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/ai_infra_attention_pioneer_def.cpp#L603-L619)：`softmax_lse` 输出（DT_FLOAT）与 `num_heads / scale / pre_tokens / next_tokens / input_layout / sparse_mode / block_size / softmax_lse_flag` 等属性。

**aclnn 接口**：

- [aclnn_ai_infra_attention_pioneer.h:L20-L59](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_api/aclnn_ai_infra_attention_pioneer.h#L20-L59)：`aclnnAiInfraAttentionPioneerGetWorkspaceSize` 声明——约 30 个可选张量/IntArray + `numHeads/scaleValue/preTokens/nextTokens/inputLayout/numKeyValueHeads/sparseMode/innerPrecise/blockSize/antiquantMode/softmaxLseFlag/...` 标量组 + 两个输出 + `workspaceSize/executor`。
- [aclnn_ai_infra_attention_pioneer.h:L61-L62](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_api/aclnn_ai_infra_attention_pioneer.h#L61-L62)：`aclnnAiInfraAttentionPioneer` 执行接口，与 u2 讲过的两段式约定一致。

#### 4.1.4 代码实践：读文档示例，画出"两次调用"时序

1. **实践目标**：理解 AttentionPioneer 需要先调 metadata 配套算子、再调主算子的两步时序。
2. **操作步骤**：
   - 打开 [docs/npu_ai_infra_attention_pioneer.md:L122-L267](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/docs/npu_ai_infra_attention_pioneer.md#L122-L267)，其中有两个完整 Python 示例（P 侧 TND + `sparse_mode=4`；D 侧 TND_NTD + block_table/KV Cache）。
   - 找到示例里 `npu_ai_infra_attention_pioneer_metadata` 的调用位置，记下它输出的 tensor 被传给主算子的哪个参数（应是 `metadata`）。
   - 画出时序：`metadata 算子 → 主算子 → attentionOut/softmaxLse`。
3. **需要观察的现象**：metadata 的输出 shape 是否固定为 `[1024]`；P 侧与 D 侧示例里 `input_layout`、`sparse_mode`、`block_table` 的取值差异。
4. **预期结果**：两条示例链路都能画通，且 D 侧示例必须出现 `query_rope/key_rope` 与 `block_table`。实际运行需要 950 硬件环境，**待本地验证**。

#### 4.1.5 小练习与答案

1. **练习**：为什么 `AddConfig` 只有 `ascend950` 一项，而 dtype 列表却有近 94 项？
   **答案**：dtype 列表覆盖量化（int8/fp8）与非量化（fp16/bf16）全部形态，运行期由 tilingKey 区分；而芯片目标只有 950 一个，因为该算子的混合核编程模型（`KERNEL_TYPE_MIX_AIC_1_2`、`__CCE_AICORE__ == 310`）只存在于 Davinci 3.5。
2. **练习**：`softmax_lse_flag` 为 false 时 `softmax_lse` 输出还存在吗？
   **答案**：输出在原型里仍声明（`def.cpp` L603，REQUIRED 且 DT_FLOAT），是否真正写内容由 tiling 的 `isSoftMaxLseEnable` 控制（见 4.3 的 `PFATilingDataconvert`）；用户不开启时可传空语义的占位张量，以接口文档约束为准。

### 4.2 host 侧 tiling：注册链路、场景校验与 12 参数 tilingKey

#### 4.2.1 概念说明

host tiling 要回答三个问题：**这次调用是哪个场景（P 侧/D 侧、是否 PA、是否 rope、是否空 KV）？块怎么切？编译成哪个 kernel 模板实例？** 本算子把答案编码成一个 12 参数 tilingKey，让 CCE 编译器在 `config` 值的组合空间里为每种形态生成一份特化代码——这是"用编译期展开换运行期分支"的典型手法，也是它叫 "Pioneer/新一代" 的工程特征之一。

#### 4.2.2 核心流程

```text
IMPL_OP_OPTILING(AiInfraAttentionPioneer)
  .Tiling(DoOpTilingAiInfraAttentionPioneer)              # 运行期入口
  .TilingParse<AiInfraAttentionPioneerCompileInfo>(...)   # 编译期解析

DoOpTilingAiInfraAttentionPioneer
  -> TilingAiInfraAttentionPioneerV2(context)
  -> AiInfraAttentionPioneerTilingV2::DoOpTiling          # 第一层：形状/布局/合法性校验
     ├─ 仅接受 TND / TND_NTD
     ├─ D ≤ 512
     └─ DAV_3510 平台放宽 int8 32 对齐 / D 16 对齐检查
  -> PromptFlashAiInfraAttentionPioneerTilingV2::DoSubOpTiling  # 第二层：真正的切分流水线（4.3）
```

tilingKey 的 12 个参数（`GET_TPL_TILING_KEY(inOutLayoutType, config, pseMode, quantMode, hasAttenMask, hasRope, isPa, isFd, emptyTensor, PFAMask, pFAMatMulType, enableKVPrefix)`）在 host 侧拼成一个整型，在 kernel 侧再被模板形参逐一接住（4.4）。

#### 4.2.3 源码精读

**注册链**：

- [ai_infra_attention_pioneer_tiling_register.cpp:L26-L29](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/ai_infra_attention_pioneer_tiling_register.cpp#L26-L29)：`IMPL_OP_OPTILING(AiInfraAttentionPioneer).Tiling(DoOpTilingAiInfraAttentionPioneer).TilingParse<...>(TilingPrepareForAiInfraAttentionPioneer);` —— 与 u3-l3 讲过的注册模式一致，本算子只是拆到了独立文件。
- [ai_infra_attention_pioneer_tiling.cpp:L27-L33](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/ai_infra_attention_pioneer_tiling.cpp#L27-L33)：`DoOpTilingAiInfraAttentionPioneer` 只是转发到 `TilingAiInfraAttentionPioneerV2`。
- [ai_infra_attention_pioneer_tiling.cpp:L35-L41](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/ai_infra_attention_pioneer_tiling.cpp#L35-L41)：`extern "C" __attribute__((visibility("default"))) DeviceDoOpTilingAiInfraAttentionPioneer` —— 额外导出了**设备侧 tiling** 符号，与 metadata 算子/GE 的设备 tiling 机制配合（区别于 u3 系列讲的纯 host tiling）。

**输入索引与场景裁剪**：

- [ai_infra_attention_pioneer_tiling_v2.cpp:L51-L79](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/arch35/ai_infra_attention_pioneer_tiling_v2.cpp#L51-L79)：输入索引常量表（QUERY=0、KEY=1、VALUE=2、ATTEN_MASK=4、ACTUAL_SEQ_Q=5、ACTUAL_SEQ_KV=6、BLOCK_TABLE=14、QUERY_ROPE=24、KEY_ROPE=25、METADATA=28、KEY_SINK=29、KEY_ROPE_SINK=30、VALUE_SINK=31）。
- [ai_infra_attention_pioneer_tiling_v2.cpp:L200-L231](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/arch35/ai_infra_attention_pioneer_tiling_v2.cpp#L200-L231)：`ConvertContextToParamsPFA` 中直接**拒绝** PSE、left padding、prefix、量化等特性——Pioneer 在 arch35 上先做窄场景优化，再逐项放开。
- [ai_infra_attention_pioneer_tiling_v2.cpp:L233-L239](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/arch35/ai_infra_attention_pioneer_tiling_v2.cpp#L233-L239)：`pse_type != 0` 被拒绝，同上。

**DoOpTiling 的三道闸门**：

- [ai_infra_attention_pioneer_tiling_v2.cpp:L327-L333](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/arch35/ai_infra_attention_pioneer_tiling_v2.cpp#L327-L333)：只接受 `TND` 与 `TND_NTD` 两种布局（host 侧枚举见 [ai_infra_attention_pioneer_tiling_struct.h:L19-L33](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/arch35/ai_infra_attention_pioneer_tiling_struct.h#L19-L33)，该枚举保留了全量布局以便扩展）。
- [ai_infra_attention_pioneer_tiling_v2.cpp:L372-L375](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/arch35/ai_infra_attention_pioneer_tiling_v2.cpp#L372-L375)：\( d \le 512 \) 的上限检查（DLIMIT=512）。
- [ai_infra_attention_pioneer_tiling_v2.cpp:L451-L464](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/arch35/ai_infra_attention_pioneer_tiling_v2.cpp#L451-L464)：`NpuArch::DAV_3510` 分支——950 平台放宽 int8 的 32 对齐与 D 的 16 对齐检查。这是"arch35 = Davinci 3.5"的第二处源码证据。

**tilingKey 生成**：

- [ai_infra_attention_pioneer_tiling_v2.cpp:L469-L481](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/arch35/ai_infra_attention_pioneer_tiling_v2.cpp#L469-L481)：`gen_tilingkey = GET_TPL_TILING_KEY(...12 个参数...)` 后 `context_->SetTilingKey(gen_tilingkey)`。这 12 个参数正是 kernel 入口的 12 个模板形参（见 [ai_infra_attention_pioneer.cpp:L27-L122](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/ai_infra_attention_pioneer.cpp#L27-L122) 的模板参数表）。

**稀疏模式与 MLA 判定**：

- [ai_infra_attention_pioneer_tiling_v2.cpp:L1387-L1436](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/arch35/ai_infra_attention_pioneer_tiling_v2.cpp#L1387-L1436)：`SetSparseModeData`——`pre/next_tokens` 钳位到 INT_MAX；`LEFT_UP`（sparse 2）强制 `pre=MAX, next=0`；`BAND`（sparse 4）置 `isBandMode`。加约束 \((-{\rm next\_tokens}) \le {\rm pre\_tokens}\)（文档 L96-L120）。
- [ai_infra_attention_pioneer_tiling_v2.cpp:L1533-L1536](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/arch35/ai_infra_attention_pioneer_tiling_v2.cpp#L1533-L1536)：`enablePFAMLA = (d == 192 && valueD == 128)` —— P 侧 MLA 的判定就是这两个维度，与文档约束一字不差。
- [ai_infra_attention_pioneer_tiling_v2.cpp:L3365-L3380](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/arch35/ai_infra_attention_pioneer_tiling_v2.cpp#L3365-L3380)：`SetAttenMaskCompressMode` 把 sparse 0/1 → NO_COMPRESS，2 → LEFT_UP，3 → RIGHT_DOWN，4 → BAND，交给 kernel 的 `attenMaskCompressMode` 字段。
- [ai_infra_attention_pioneer_tiling_v2.cpp:L3382-L3386](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/arch35/ai_infra_attention_pioneer_tiling_v2.cpp#L3382-L3386)：`SetLayoutType` 对 arch35 统一映射为 `LAYOUT_TND`（TND_NTD 的"NTD 输出"由 `transposeLayout` 字段另行表达，见 4.5 `ComputeConstexpr`）。

#### 4.2.4 代码实践：跟踪一次 tilingKey 的构成

1. **实践目标**：给定一组调用参数，手工推出 tilingKey 的 12 个分量。
2. **操作步骤**：
   - 取 4.1.4 中 P 侧示例的参数：`input_layout="TND"`、`sparse_mode=4`、无 attenMask、无 rope 输入之外的额外项。
   - 对照 [ai_infra_attention_pioneer_tiling_v2.cpp:L469-L481](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/arch35/ai_infra_attention_pioneer_tiling_v2.cpp#L469-L481)，逐项填写：inOutLayoutType=?、config（由 S1/S2/D/Dv 模板档位决定，见 4.4 的 `ConfigValue`）、pseMode=0、quantMode=0、hasAttenMask=false、hasRope=?、isPa=false、isFd=false、emptyTensor=false、PFAMask/pFAMatMulType=0、enableKVPrefix=false。
   - 再对 D 侧示例（TND_NTD + block_table）做一遍，观察 `isPa` 与 `hasRope` 翻转。
3. **需要观察的现象**：两个场景的 tilingKey 分量差异集中在 `inOutLayoutType / config / hasRope / isPa` 四项。
4. **预期结果**：能写出两张 12 列的表格。若想核对数值，可在 host 侧 tiling 日志（L482 的 `OP_LOGI` "The new template tilingkey is %llu"）中看到最终整型，**待本地验证**（需要打开算子日志级别并跑一次真实调用）。

#### 4.2.5 小练习与答案

1. **练习**：为什么 `DoOpTiling` 只允许 TND/TND_NTD，`InputLayout` 枚举却保留了 BSH/BNSD 等十余种？
   **答案**：枚举是跨 arch 共享的全集（其他 arch 的实现仍接受这些布局）；arch35 这一代先把场景收窄到 MLA 的 TND 系，后续版本再放开——枚举为前向兼容预留。
2. **练习**：`sparse_mode=2` 时 `pre_tokens/next_tokens` 会被怎样处理？
   **答案**：`SetSparseModeData` 强制 `pre=INT_MAX, next=0`（leftUpCausal 语义由 mask 压缩模式 LEFT_UP 表达，不再依赖 pre/next 数值），并钳位保证 \(-{\rm next}) \le {\rm pre}\)。
3. **练习**：`enablePFAMLA` 的判定条件是什么？为什么不用显式属性开关？
   **答案**：`d==192 && valueD==128`。维度组合本身就唯一刻画了 P 侧 MLA 形态，用维度推断可以少一个易错的人肉开关，也让上层框架零改动接入。

### 4.3 tiling 流水线与 Host↔Device 数据契约（TilingData 适配器）

#### 4.3.1 概念说明

这是本算子最有教学价值的设计。u3 系列讲的模式是"host 结构与 kernel 结构共用同一份头文件"；AttentionPioneer 则是：

- host 内部用**自己的私有结构** `PromptFlashAiInfraAttentionPioneerTilingData`（cube/softmax 两把切分刀 + 若干参数段）完成切分计算；
- 最终通过 `PFATilingDataconvert` 把约 50 个字段**抄写进 CANN 标准 `FlashAttentionScoreSimplifiedTilingData`**（`faTilingAdapter`），再整体写入 tiling buffer；
- kernel 侧用 `GET_TILING_DATA_WITH_STRUCT(FlashAttentionScoreSimplifiedTilingData, ...)` 取回。

好处：kernel 侧可以最大程度复用 CANN FA 家族的 regbase 基建（`inputParamsRegbase / multiCoreParamsRegbase / initOutputParams` 等），host 侧则保留自己的切分自由度。这与 u2-l4 的 aggregate_hidden 是同一思想（宿主结构 ↔ 设备结构解耦），但方向相反：那里是"聚合后下传"，这里是"转换成标准结构下传"。

#### 4.3.2 核心流程

`RunBigKernelTilingWithParams` 的流水线（顺序执行）：

```text
GetMaxWorkspaceFlag      # 恒走最大 workspace 模式（因为 actual_seq_lengths 是设备张量）
SetPlatMemoryInfo → SetAttributeInfo → CheckTensorInvalid
CheckEmptyTensor         # 空 KV → SetEmptyTensor 直接返回（kernel 侧走零输出路径）
CheckSingleAttribute → PFAMerge 的 gSize 调整 → CheckCrossoverAttribute
InitializeMaxWorkspace → SetTilingData → InferTilingMod → InferSplitCoreMode
InferConstantization → AdjustTilingData → GetEnableDN
ComputeTilingData        # cube/softmax 切分（TCubeTiling / SoftMaxTiling）
ComputeTilingKey         # numBlocks = CalcTschBlockDim(aivNum, aicNum, aivNum); workspace[0]=GetPFAWorkSpaceSize
SetQKVStartIdx
→ DoSubOpTiling：SetBlockDim + memset + PFATilingDataconvert + *tiling = faTilingAdapter
  + SetScheduleMode(BATCH_MODE_SCHEDULE)   # kernel 要 SyncAll，必须 batch 调度
```

#### 4.3.3 源码精读

- [ai_infra_attention_pioneer_tiling_v2.cpp:L244-L251](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/arch35/ai_infra_attention_pioneer_tiling_v2.cpp#L244-L251)：`GetMaxWorkspaceFlag` 注释写明 **"Always use max workspace mode"** —— host 拿不到设备侧真实序列长度，只能按最大形状申请 workspace；真实切分延后到 kernel 读 metadata（4.5）。
- [ai_infra_attention_pioneer_tiling_v2.cpp:L3526-L3636](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/arch35/ai_infra_attention_pioneer_tiling_v2.cpp#L3526-L3636)：`RunBigKernelTilingWithParams` 全流水线，其中 `CheckEmptyTensor` 命中即 `SetEmptyTensor` 返回（对应 kernel 的零输出快速路径，4.4）。
- [ai_infra_attention_pioneer_tiling_v2.cpp:L3346-L3363](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/arch35/ai_infra_attention_pioneer_tiling_v2.cpp#L3346-L3363)：`ComputeTilingKey` —— `numBlocksToBeSet = CalcTschBlockDim(aivNum, aicNum, aivNum)`（以 AIV 数为准、AIC 数约束），workspace[0] 取 `GetPFAWorkSpaceSize`。
- [ai_infra_attention_pioneer_tiling_v2.cpp:L3398-L3505](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/arch35/ai_infra_attention_pioneer_tiling_v2.cpp#L3398-L3505)：**`PFATilingDataconvert`** —— 逐字段抄写到 `faTilingAdapter.inputParamsRegbase`：`bSize/t1Size/t2Size/n2Size/gSize/s1Size/s2Size/dSize/dSizeV/dSizeRope=64/scaleValue/preTokens/nextTokens/sinkLength/attenMaskShapeType/attenMaskCompressMode/implMode=HIGH_PRECISION/sparseType/blockSize/blockTableDim2/paBlockNumSum/isGqa/isSoftMaxLseEnable/ropeHeadSize/headNumRatio...`。这份字段清单就是综合实践 (a) 的盘点对象。
- [ai_infra_attention_pioneer_tiling_v2.cpp:L3638-L3665](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/arch35/ai_infra_attention_pioneer_tiling_v2.cpp#L3638-L3665)：`DoSubOpTiling` 收尾——`SetBlockDim(numBlocksToBeSet)`、`memset(RawTilingData)`、`PFATilingDataconvert`、`*tiling = faTilingAdapter`（通过 `context_->GetTilingData<FlashAttentionScoreSimplifiedTilingData>()` 取 buffer）、`SetScheduleMode(BATCH_MODE_SCHEDULE)`（注释说明：kernel 使用 `SyncAll` 必须配 batch 调度模式）。
- kernel 侧镜像结构（同一结构在 device 头文件里的样子）：
  - [ai_infra_attention_pioneer_tiling_regbase.h:L1774-L1788](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_tiling_regbase.h#L1774-L1788)：`PromptFlashAiInfraAttentionPioneerTilingData`（host 私有结构的定义处），成员 = `bmm1TilingDataRect / bmm2TilingDataRect / promptAttentionBaseParams / promptAttentionSeqParams / promptAttentionSingleCoreParams / promptAttentionTensorSizeRect / promptAttentionInitOutputParams / softmaxTilingDataRect / softmaxFlashTilingDataRect`。
  - [ai_infra_attention_pioneer_tiling_regbase.h:L21-L332](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_tiling_regbase.h#L21-L332)：`pfatilingdata::TCubeTiling` —— Cube 侧 matmul 切分刀（M/N/Ka/Kb/singleCoreM/N/K/baseM/N/K/depthA1/B1/stepM/N/Ka/Kb/dbL0A/B/C/share* 等），语义同 u4-l3 讲过的 bmm 切分。
  - [ai_infra_attention_pioneer_tiling_regbase.h:L334-L495](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_tiling_regbase.h#L334-L495)：`SoftMaxTiling` —— Vector 侧 softmax 切分（srcM/K、outMaxM/K、splitM/K、reduceM/K、tail 处理）。
  - [ai_infra_attention_pioneer_tiling_regbase.h:L499-L565](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_tiling_regbase.h#L499-L565)：`TCubeTilingCopy / SoftMaxTilingCopy / TilingDataCopy` 宏——host 抄写结构时的"字段搬运工"。

#### 4.3.4 代码实践：TilingData 字段清单（本讲核心实践，先做单段版）

1. **实践目标**：为 `faTilingAdapter.inputParamsRegbase` 建立一张"字段 → 含义"对照表。
2. **操作步骤**：
   - 通读 [ai_infra_attention_pioneer_tiling_v2.cpp:L3398-L3505](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/arch35/ai_infra_attention_pioneer_tiling_v2.cpp#L3398-L3505)，按"维度类 / 标量类 / mask 类 / PA 类 / 开关类"分组记录每个赋值行。
   - 对每个不认识的字段（如 `headNumRatio`、`paBlockNumSum`），在 `op_kernel/arch35/ai_infra_attention_pioneer_tiling_regbase.h` 中搜索它的读取处，反推含义。
3. **需要观察的现象**：哪些字段的值来自用户输入（如 `scaleValue`），哪些来自切分计算（如 `paBlockNumSum`），哪些是硬编码（如 `dSizeRope=64`、`implMode=HIGH_PRECISION`）。
4. **预期结果**：得到约 50 行的表格（综合实践 (a) 会要求整理成正式清单）。纯阅读即可完成，无需硬件。

#### 4.3.5 小练习与答案

1. **练习**：为什么必须 `SetScheduleMode(BATCH_MODE_SCHEDULE)`？
   **答案**：kernel 的 `Process()` 在输出需要初始化时执行 `SyncAll()`（见 4.5），而 `SyncAll` 要求所有核同时到达栅栏；batch（同步块）调度模式保证同一 kernel 的核被一起调度，否则死锁风险。代码注释同样点明了这一因果。
2. **练习**：host 为什么"Always use max workspace"？
   **答案**：`actual_seq_lengths` 是设备侧张量，host tiling 读不到其数值，无法按真实序列长度精确分配 workspace/切分；于是按最大形状申请，把"按真实长度切分"交给 kernel 读 metadata 后完成。
3. **练习**：`TCubeTiling` 与 `SoftMaxTiling` 为什么是两把独立的刀？
   **答案**：bmm1/bmm2 跑在 Cube 核（按 M/N/K 切、受 L0A/L0B/L1 容量约束），softmax 跑在 Vector 核（按行块/列块切、受 UB 容量约束），两者的最小数据单元与容量模型完全不同，分开建模才能各自取到上界。

### 4.4 kernel 入口分发：entry_regbase 与模板参数解析

#### 4.4.1 概念说明

kernel 入口 `ai_infra_attention_pioneer.cpp` 只是一个 12 模板参数的 `extern "C"` 函数，真正的分发逻辑在 `arch35/ai_infra_attention_pioneer_entry_regbase.h` 的 `ai_infra_attention_pioneer_FIAS_regbase`。它做三级筛选：

1. **架构门**：`__CCE_AICORE__ == 310 && !__DAV_310R6__`（Davinci 3.5），并声明 `KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_MIX_AIC_1_2)`；
2. **空 KV 门**：`emptyTensor == true` → 直接走零输出 kernel 提前返回；
3. **dtype + config 门**：fp16 / bf16 各一套；其中 `dTemplateType == Aligned576`（即 512+64 对齐档，P 侧 MLA 的 d=576 对齐形态）走 `FAKernelNoquantMla`，`Aligned512` 与其他档位走 CANN 基类 `BaseApi::FlashAttentionScoreKernelInfer`。

`PARSE_PARAMS_NoQuant` 宏负责把 12 个合并模板参数拆成 `inputLayoutType / s1TemplateType / s2TemplateType / dTemplateType / dVTemplateType` 等 constexpr，供 `if constexpr` 分支使用。

#### 4.4.2 核心流程

```text
kernel(query, key, value, ..., tiling)          # 33 个 GM 参数
  └─ ai_infra_attention_pioneer_FIAS_regbase<12 个模板参数>(...)
       ├─ #if (__CCE_AICORE__ == 310) && (!__DAV_310R6__)
       │    KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_MIX_AIC_1_2)
       │    REGISTER_TILING_DEFAULT(FlashAttentionScoreSimplifiedTilingData)
       │    ├─ emptyTensor → ZeroOutPut 提前 return
       │    ├─ fp16:  PARSE_PARAMS_NoQuant(...)
       │    │        ├─ d==Aligned576 → FAKernelNoquantMla（本讲 4.5）
       │    │        ├─ d==Aligned512 → BaseApi::FlashAttentionScoreKernelInfer
       │    │        └─ 其他档位     → BaseApi::FlashAttentionScoreKernelInfer
       │    └─ bf16: 同上镜像
       └─ #endif
```

#### 4.4.3 源码精读

- [ai_infra_attention_pioneer.cpp:L27-L122](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/ai_infra_attention_pioneer.cpp#L27-L122)：kernel 入口，12 个模板参数（与 tilingKey 12 分量一一对应），把 33 个 GM 参数转发给 regbase 函数；注意文件开头 `#define FIA_ENABLE_MLA` 后才 include entry 头。
- [ai_infra_attention_pioneer_entry_regbase.h:L190-L201](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_entry_regbase.h#L190-L201)：`ai_infra_attention_pioneer_FIAS_regbase` 的 12 个模板形参：`inOutLayoutType / config / pseMode / quantMode / hasAttenMask / hasRope / isPa / isFd / emptyTensor / pFAMask / pFAMatMulType / enableKVPrefix`。
- [ai_infra_attention_pioneer_entry_regbase.h:L239-L241](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_entry_regbase.h#L239-L241)：架构门 + `KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_MIX_AIC_1_2)` + `REGISTER_TILING_DEFAULT(FlashAttentionScoreSimplifiedTilingData)` —— 混合核与标准 TilingData 在此锁定。这是"arch35 = C310"的第四处源码证据。
- [ai_infra_attention_pioneer_entry_regbase.h:L242-L249](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_entry_regbase.h#L242-L249)：`emptyTensor` 快速路径——按输出原始 dtype 选 `half` 或 `fp8_e4m3fn_t` 实例化 `PromptFlashAiInfraAttentionPioneerZeroOutPut`，直接清零返回。
- [ai_infra_attention_pioneer_entry_regbase.h:L252-L289](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_entry_regbase.h#L252-L289)：fp16 分支。`PARSE_PARAMS_NoQuant` 之后 `if constexpr (dTemplateType == DTemplateType::Aligned576)` → `INVOKE_FA_OP_IMPL_ASCEND950_KVSAME_BASEAPI(FAKernelNoquantMla, half, ...)`。注意 L287 的源码注释：**模板参数 `hasRope` 实际业务为 false，但模板需要其为 true，由 kernel 内部直接写入**——模板形态与业务语义解耦的非常规处理，读代码时容易被误导。
- [ai_infra_attention_pioneer_entry_regbase.h:L291-L351](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_entry_regbase.h#L291-L351)：`Aligned512` 与通用档位 → `BaseApi::FlashAttentionScoreKernelInfer`（D 侧 d=512 路径复用 CANN 基类实现）。L291 起先以 `constexpr` 算出 `vec1ResultSize / qkvSizeRsv2`（UB 预算），注释解释了为何必须先 constexpr（否则所有组合都会实例化，`hasRope=false` 时成员不存在导致编译失败）。
- [ai_infra_attention_pioneer_entry_regbase.h:L354-L454](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_entry_regbase.h#L354-L454)：bf16 完整镜像分支（结构与 fp16 一一对应）。
- [ai_infra_attention_pioneer_entry_regbase.h:L28-L62](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_entry_regbase.h#L28-L62)：`INVOKE_FA_OP_IMPL_ASCEND950_KVSAME_BASEAPI` 宏——`GET_TILING_DATA_WITH_STRUCT(FlashAttentionScoreSimplifiedTilingData, ...)` 取回 tiling（4.3 契约的 device 侧半边），声明 `CubeBlockType = FABlockCubeNoquantMla<...>` / `VecBlockType = BaseApi::FABlockVecInfer<...>`，构造 kernel 对象、Init、Process。
- [ai_infra_attention_pioneer_entry_regbase.h:L74-L79](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_entry_regbase.h#L74-L79)：通用路径里按 `g_coreType` 用 `std::conditional` 在同一份代码里选择 Cube 真实现/Dummy 与 Vector 真实现/Dummy——混合核下 AIC 与 AIV 跑同一个 kernel 符号，靠类型系统裁剪各自不需要的逻辑。
- [ai_infra_attention_pioneer_template_tiling_key_enum.h:L33-L41](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_template_tiling_key_enum.h#L33-L41)：`PARSE_PARAMS_NoQuant` 宏定义——用查表（`InOutLayoutTypeValue[...]`、`ConfigValue[config].s1/s2/d/dv`）把合并参数拆成 5 个 constexpr 模板档位。
- [ai_infra_attention_pioneer_common_regbase.h:L60-L71](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_common_regbase.h#L60-L71)：混合核关键常量 `CV_RATIO = 2`（1 AIC : 2 AIV）、`SYNC_MODE = 4`、mm1/mm2 结果的跨核事件号（7/8/9/10）、L0C/MLA 的 L0A/L0B 容量。
- [ai_infra_attention_pioneer_common_regbase.h:L72-L83](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_common_regbase.h#L72-L83)：device 侧 `SparseModeEnum`（含 `BAND_LEFT_UP_CAUSAL = 9` 等内部细分），与 host 侧 sparse 数值的映射在 tiling 阶段完成（4.2 的 `SetAttenMaskCompressMode`）。

#### 4.4.4 代码实践：数一数分发树

1. **实践目标**：画出 entry_regbase 的完整分发树并统计叶子数量。
2. **操作步骤**：
   - 从 [ai_infra_attention_pioneer_entry_regbase.h:L239](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_entry_regbase.h#L239) 开始，标出每个 `if constexpr` / `#if` 的谓词与叶子 kernel 类名。
   - 对每个叶子标注：对应哪个业务场景（P 侧 MLA / D 侧 PA / 空 KV / 通用）。
3. **需要观察的现象**：fp16 与 bf16 分支是否严格镜像；`emptyTensor` 路径在 dtype 门之前还是之后。
4. **预期结果**：一棵 2（dtype）× 3（Aligned576 / Aligned512 / 其他）+ 1（empty）的分发树，共 7 个叶子。纯阅读可完成。

#### 4.4.5 小练习与答案

1. **练习**：`Aligned576` 对应什么业务维度？
   **答案**：P 侧 MLA 的 D=576 对齐档（d=512 主干 + 64 rope 的对齐组合在 `ConfigValue` 表中的档位表示）；命中它即走 `FAKernelNoquantMla` 专用流水线。D=512 的 D 侧场景则命中 `Aligned512`，复用 CANN 基类。
2. **练习**：为什么 `INVOKE_PFA_NOQUANT_GENERAL_OP_IMPL...` 宏要分 CUBE/VECTOR 两个版本（`__DAV_C310_CUBE__` 与否）？
   **答案**：Cube 编译单元不需要 TilingData（tilingData 置 nullptr，见 L65-L66 的宏），Vector 编译单元才 `GET_TILING_DATA_WITH_STRUCT`；分开定义避免 Cube 侧实例化 Vector 专用的取 tiling 代码，减小二进制体积与编译时间。
3. **练习**：若把 L287 处的 `hasRope` 模板参数从 `true` 改为 `false`，会发生什么？
   **答案**：`FAKernelNoquantMla` 的成员/分支里有依赖 `hasRope=true` 才存在的字段（`dSizeRope` 等，见 4.5 `ComputeConstexpr` 的 `if constexpr (hasRope)` 分支），改回 false 会在编译期丢失这些字段导致编译失败或行为错误——这正是源码注释强调"模板需要其为 true"的原因。

### 4.5 FAKernelNoquantMla 精读：混合核、metadata 分核与三缓冲流水线

#### 4.5.1 概念说明

`FAKernelNoquantMla` 是 P 侧 MLA 专用前向 kernel，一个类同时跑在 AIC 与 AIV 上：

- **AIC（Cube）**：执行 `IterateBmm1 / IterateBmm2`（\( QK^\top \) 与 \( PV \) 两次矩阵乘）；
- **AIV（Vector）**：执行 `ProcessVec1 / ProcessVec2`（在线 softmax、rescale）；
- 两者通过 **UB 上的跨核双缓冲**（`bmm1Buffers / bmm2Buffers`，`SetCrossCore`）与 **L1 三缓冲**（`mm12Bmm2AL1Buffers`，`BuffersPolicy3buff`）组成软件流水；
- **分核信息在运行期从 metadata 张量读取**：`ReadMetadataForSplitCore` 把 host 塞不进去的"按真实序列长度切核"的结果（每核负责的 bn 起止、gs1 起止、实际 s1/s2/t1/t2）回填进 TilingData 的对应字段。

#### 4.5.2 核心流程

```text
Init()
  aivIdx = GetBlockIdx()                    # AIV 视角的全局块号
  AIV 分支: aicIdx = aivIdx >> 1             # 2 个 AIV 共享 1 个 AIC
  AIC 分支: aicIdx = aivIdx
  metadataGm.SetGlobalBuffer(metaData)
  ReadMetadataForSplitCore()                 # 回填 coreNum/bnStartIdx/sparseStartIdx/s1/s2/...
  AIV: vecBlock.CleanOutput()                # needInit 时清零输出
  AIC: cubeBlock.InitCubeBlock(...)          # matmul 初始化
  InitMMResBuf() + SetFlag3Buffer()          # UB/L1 缓冲与跨核事件
  InitInput()                                # 绑定 GM 输入（含 tensor list / PA 场景）

Process()
  needInit → SyncAll()
  aicIdx >= 实际核数 → return                 # metadata 决定的真实核数
  bn 循环（batch×n2 切片）
    gS1 循环（Q 分块）
      s2 循环（KV 分块，含 sink 追加块数）
        AIC: IterateBmm1(bmm1Buffers)   AIV: ProcessVec1(...)   # 流水段 1
        if taskId >= PRELOAD_N:
          AIC: IterateBmm2(bmm2Buffers) AIV: ProcessVec2(...)   # 流水段 2（滞后 PRELOAD_N 步）
  isFd（D 侧）: SyncAll → FlashDecodeCombine
```

sink 的处理体现在 s2 循环计数上：`s2LoopEndIdx += sinkBlockCnt`，其中 \( {\rm sinkBlockCnt} = \lceil {\rm sinkLength} / {\rm s2BaseSize} \rceil \)（L574），即把 sink 块当作 K/V 开头的额外分块参与同一条流水线。

#### 4.5.3 源码精读

**类与成员**：

- [ai_infra_attention_pioneer_kernel_noquant_mla.h:L29](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_kernel_noquant_mla.h#L29)：`class FAKernelNoquantMla` 声明（模板参数 `CubeBlockType / VecBlockType` 正是 4.4 宏中传入的两类）。
- [ai_infra_attention_pioneer_kernel_noquant_mla.h:L98-L100](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_kernel_noquant_mla.h#L98-L100)：`GlobalTensor<int32_t>` 成员 `metadataGm / actualSeqLengthsGm / actualSeqLengthsKVGm`——metadata 与真实序列长度都在设备侧。
- [ai_infra_attention_pioneer_kernel_noquant_mla.h:L144](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_kernel_noquant_mla.h#L144)：`BuffersPolicy3buff<BufferType::L1, SyncType::CROSS_CORE_SYNC_FORWARD> mm12Bmm2AL1Buffers` —— L1 上三缓冲 + 跨核前向同步，用于 bmm1 产出（A 侧矩阵）被 bmm2 消费的重叠。

**Init：身份识别与 metadata 回填**：

- [ai_infra_attention_pioneer_kernel_noquant_mla.h:L171-L177](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_kernel_noquant_mla.h#L171-L177)：`constInfo.aivIdx = GetBlockIdx()`；AIV 分支 `aicIdx = aivIdx >> 1`（1:2 映射），AIC 分支 `aicIdx = aivIdx`——`CV_RATIO=2` 在这里落地。
- [ai_infra_attention_pioneer_kernel_noquant_mla.h:L181-L190](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_kernel_noquant_mla.h#L181-L190)：绑定 `metadataGm`、调用 `ReadMetadataForSplitCore()`、`InitMMResBuf()`。
- [ai_infra_attention_pioneer_kernel_noquant_mla.h:L811-L864](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_kernel_noquant_mla.h#L811-L864)：`ReadMetadataForSplitCore` 全文——从 `metaPtr` 读 `USED_CORE_NUM / S1_OUTER_SIZE / TOTAL_SIZE / SPLIT_FACTOR_(TAIL_)SIZE / S1_SIZE / S2_SIZE / T1_SIZE / T2_SIZE / NEED_INIT`，写回 `mcParams.coreNum / s1OuterSize / totalSize / splitFactor*`，**覆写** `inputParams.s1Size / s2Size / t2Size / actualSeqLengths(KV)Size`，最后把每核的 `bnStartIdx[i] / sparseStartIdx[i]` 拷回（读 `usedCoreNum+1` 个边界供 `[aicIdx+1]` 访问）。
- [ap_metadata_defs.h:L23-L25](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ap_metadata_defs.h#L23-L25) 与 [ap_metadata_defs.h:L28-L55](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ap_metadata_defs.h#L28-L55)：metadata 布局——前 360 个 int32 是"每核 10 槽"（`AIC_CORE_NUM=36 × SLOTS_PER_CORE=10`），基字段从偏移 360 开始；文件头注释明确说明它与 AICPU 算子 `ai_infra_attention_pioneer_metadata` 的布局约定，"kept local to avoid cross-operator includes"。

**缓冲与事件**：

- [ai_infra_attention_pioneer_kernel_noquant_mla.h:L230-L255](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_kernel_noquant_mla.h#L230-L255)：`InitMMResBuf`——L1 定容 512K；`mm12RightSize` 按 3 份约 144K（共 432K）规划注释；UB 上 `mm1ResultSize*2 + mm2ResultSize*2`；L241/L247-L253：`SetFlag3Buffer()` 与 `bmm1Buffers/bmm2Buffers.Get().SetCrossCore()` ×2——**双缓冲 + 跨核**，AIC 写与 AIV 读各占一个 buffer 交替。
- [ai_infra_attention_pioneer_kernel_noquant_mla.h:L258-L266](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_kernel_noquant_mla.h#L258-L266)：`SetFlag3Buffer` 连发 3 组 `MTE1_MTE2` 事件，为三缓冲预置同步状态。
- [ai_infra_attention_pioneer_entry_regbase.h:L188](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_entry_regbase.h#L188)：`constexpr uint32_t L1BUFSIZE = 65536`，注释给出推导：D 最大 256 时 128×256×2 = 64K。
- [ai_infra_attention_pioneer_kernel_noquant_mla.h:L269-L315](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_kernel_noquant_mla.h#L269-L315)：`InitInput`——为 key/value 构造 `ListTensorDesc`（tensor list 场景），并按 `isKvContinuous` 分支设置读取方式。

**常量折叠**：

- [ai_infra_attention_pioneer_kernel_noquant_mla.h:L326-L521](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_kernel_noquant_mla.h#L326-L521)：`ComputeConstexpr` 把 `inputParamsRegbase` 的字段展开成几十个乘积形式的步长（`s1D / s2D / gD / n2D / bN2GD / ...`，L362-L394），`headNumRatio` 除法修正 GQA（L391-L394），PA 字段（`blockTableDim2 / blockSize / paBlockNumSum`，L500-L503），输出转置（`TND_NTD` 时 `attentionOutStride=0` 等，L507-L512），`isSoftmaxLseEnable`（L515）。这一段是 4.3 实践表的 device 侧消费者。

**主循环**：

- [ai_infra_attention_pioneer_kernel_noquant_mla.h:L524-L537](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_kernel_noquant_mla.h#L524-L537)：`Process()` 开头——`needInit` 时 `SyncAll<false>()`；`actualCoreNums` 取自 tilingData（isFd 时改为 `b*n2*splitKVNum`）；`aicIdx >= actualCoreNums` 直接返回（metadata 决定的真实核数可能小于 blockDim）。
- [ai_infra_attention_pioneer_kernel_noquant_mla.h:L546-L559](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_kernel_noquant_mla.h#L546-L559)：从 `multiCoreParamsRegbase.sparseStartIdx[aicIdx(+1)] / bnStartIdx[...]` 取本核负责的 bn 与 gs1 起止——正是 `ReadMetadataForSplitCore` 回填的那两个数组；L552-L555 的注释解释了边界核要多算一个 bn 的原因。
- [ai_infra_attention_pioneer_kernel_noquant_mla.h:L574](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_kernel_noquant_mla.h#L574)：`constInfo.sinkBlockCnt = (sinkLength + s2BaseSize - 1) / s2BaseSize;` —— sink 块数的向上取整，\( \lceil {\rm sinkLength}/{\rm s2BaseSize} \rceil \)。
- [ai_infra_attention_pioneer_kernel_noquant_mla.h:L576-L658](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_kernel_noquant_mla.h#L576-L658)：三层循环主体。L622-L626：`s2LoopEndIdx += sinkBlockCnt`（sink 拼进主循环）；L627-L630：本核最后一轮多跑 `PRELOAD_N` 步以冲刷流水线；L632-L643：AIC 执行 `IterateBmm1`，AIV 执行 `ProcessVec1`；L645-L653：`taskId >= PRELOAD_N` 后每轮再补一对 `IterateBmm2 / ProcessVec2`——**用固定深度的滞后实现 bmm1→softmax→bmm2 的软件流水**，`runInfo[taskId & 3]` 即 4 深 RunInfo 环形数组。
- [ai_infra_attention_pioneer_kernel_noquant_mla.h:L660-L666](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_kernel_noquant_mla.h#L660-L666)：isFd（D 侧 flash-decode）时 AIV 在全部核 `SyncAll()` 后执行 `FlashDecodeCompute`——把 splitKV 的部分结果归并成最终输出。

#### 4.5.4 代码实践：画一张"metadata 字段 → kernel 行为"映射图

1. **实践目标**：把 metadata 的每个基字段与它改变的 kernel 行为一一对应。
2. **操作步骤**：
   - 左列抄 [ap_metadata_defs.h:L37-L55](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ap_metadata_defs.h#L37-L55) 的 16 个 `BaseMetaField`；右列在 [ai_infra_attention_pioneer_kernel_noquant_mla.h:L811-L864](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_kernel_noquant_mla.h#L811-L864) 与 Process 中找到消费点。
   - 特别标出 `NEED_INIT` 的特殊逻辑：`t1Size != inputParams.t1Size` 时强制 `needInit=1`（L850-L853）。
3. **需要观察的现象**：哪些字段影响"核数/分工"（bnStartIdx、sparseStartIdx、USED_CORE_NUM），哪些影响"形状"（S1/S2/T1/T2_SIZE），哪些影响"输出初始化"（NEED_INIT）。
4. **预期结果**：一张三段式映射图（分工/形状/初始化）。纯阅读可完成。

#### 4.5.5 小练习与答案

1. **练习**：为什么 `PRELOAD_N` 滞后若干步才启动 bmm2？
   **答案**：bmm2 的输入是 bmm1 结果经 softmax（ProcessVec1）后的 P 矩阵，存在数据依赖；滞后固定步数让 bmm1 队列先积压若干块，bmm2 与后续 bmm1 重叠执行，形成流水；末尾多跑 `PRELOAD_N` 轮把队列冲空。
2. **练习**：`aicIdx = aivIdx >> 1` 说明什么？
   **答案**：`KERNEL_TYPE_MIX_AIC_1_2` 下全局块号按 AIV 编号，两个相邻 AIV 映射同一个 AIC；Cube 侧逻辑用 `aicIdx` 寻址 metadata 的每核槽位，保证 AIC:AIV=1:2 的配对消费同一份数据。
3. **练习**：metadata 前 360 个 int32 为什么按"36 核 × 10 槽"组织？
   **答案**：每核需要 `AIC_CORE_ENABLE / BN2_START_PTR / GS1_START_PTR` 等字段（当前用 3 个，其余预留到 10 个），36 是该平台 AIC 数上界；固定槽距让 kernel 用 `coreIdx * 10 + field` 一次算出地址，无需指针表。

### 4.6 另一条路线：flash_attention_interface.cpp 的 CUTLASS 风格 FAInfer

#### 4.6.1 概念说明

同一 op_kernel 目录下还有一个 `flash_attention_interface.cpp`，用 CUTLASS 风格的类型组装（`GemmShape / DispatchPolicy / Block::BlockMmad / Block::BlockEpilogue`）描述一个 `SplitFuse::FAInfer` kernel。它与 entry_regbase 那条"手写 regbase 流水线"是**两种工程路线**：

| 维度 | regbase 路线（entry_regbase → FAKernelNoquantMla） | CUTLASS 风格（SplitFuse::FAInfer） |
| --- | --- | --- |
| 组装方式 | 宏 + `if constexpr` 手选 kernel 类 | 类型别名层层拼接，策略（DispatchPolicy）注入 |
| 可读性 | 贴近硬件手动流水，细节全在源码 | 声明式，块形状/调度策略一目了然 |
| 复用面 | 依赖 CANN FA regbase 基建 | 依赖 Gemm/Epilogue 组件库 |

初学者读它最大的收获是：**注意力 kernel 可以被描述成"两个 GEMM + 三个 Epilogue"的组合**。

#### 4.6.2 核心流程

```text
FAInfer<模板参数: dtype/PagedCacheFlag/IS_FD/maskCategory/inLayout/lseMode/sinkMode>
  ├─ 类型组装：QK GEMM（BlockMmadQK） + 在线 softmax Epilogue
  │            + PV GEMM（BlockMmadPV） + RescaleO Epilogue + InitOutWhenZero Epilogue
  ├─ IS_FD ? FAInferKernel_FD : FAInferKernel_NonFD
  └─ 构造 FAIKernelParams{q,k,v,pse,mask,blockTables,actualQ/Kvseqlen,o,lse,workspace,tiling,sink}
       并以仿函数方式调用
```

#### 4.6.3 源码精读

- [flash_attention_interface.cpp:L21-L30](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/flash_attention_interface.cpp#L21-L30)：`FAInfer` 的模板参数——`InputDtypeQ/Kv`、`IntermCalcPrec`（中间精度）、`PagedCacheFlag`、`IS_FD`、`maskCategory`、`inLayout`、`lseMode`、`sinkMode`。注意 `sinkMode` 也是一等公民，Sink Token 在这条路线上同样内建。
- [flash_attention_interface.cpp:L46](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/flash_attention_interface.cpp#L46)：`using ArchTag = Arch::AtlasA2;` —— 该实现以 A2 级组件为基座（与 arch35 主线不是同一条目标链路），阅读时要区分。
- [flash_attention_interface.cpp:L70-L78](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/flash_attention_interface.cpp#L70-L78)：QK GEMM 的 L1/L0 tile（`GemmShape<Q_TILE_CEIL,128,128>` 与 `128,128,128`）+ `DispatchPolicyQK = Gemm::MmadAtlasA2FAIQK<PagedCacheFlag,false>` + `BlockMmadQK`。
- [flash_attention_interface.cpp:L80-L89](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/flash_attention_interface.cpp#L80-L89)：在线 softmax 的 Epilogue 策略 `EpilogueAtlasA2OnlineSoftmax<lseMode, sinkMode, maskCategory, IntermCalcPrec>` 拼成 `EpilogueOnlineSoftmax`。
- [flash_attention_interface.cpp:L91-L97](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/flash_attention_interface.cpp#L91-L97)：PV GEMM（`GemmShape<128,128,256>`）与 `BlockMmadPV`。
- [flash_attention_interface.cpp:L99-L110](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/flash_attention_interface.cpp#L99-L110)：`RescaleO`（在线 softmax 的历史输出缩放）与 `InitOutWhenZero`（全掩码行输出置零）两个 Epilogue。
- [flash_attention_interface.cpp:L111-L122](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/flash_attention_interface.cpp#L111-L122)：`std::conditional_t<IS_FD, FAInferKernel_FD, FAInferKernel_NonFD>` 选择是否带 `CombineScale`（flash-decode 归并）；最后打包 `FAIKernelParams` 并调用 kernel 仿函数。

#### 4.6.4 代码实践：用"组件清单"重述 kernel

1. **实践目标**：不看代码，凭组件名复述 `FAInfer` 的数据流。
2. **操作步骤**：读 [flash_attention_interface.cpp:L70-L117](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/flash_attention_interface.cpp#L70-L117)，把每个 `using` 换成一句中文（"计算 S=QK^T 的矩阵乘块，L1 分块 …×128×128"）。
3. **需要观察的现象**：QK 与 PV 两个 GEMM 的 K 维 tile 为何不同（128 vs 256）；`IS_FD` 引入的 `CombineScale` 对应 4.5 里 `FlashDecodeCompute` 的哪个职责。
4. **预期结果**：一段 6~8 句的数据流描述。纯阅读可完成。

#### 4.6.5 小练习与答案

1. **练习**：这条路线的 `ArchTag` 是什么，与 arch35 主线关系如何？
   **答案**：`Arch::AtlasA2`。它是仓库里保留的另一套组装路线（面向 A2 级组件库），与 `__CCE_AICORE__==310` 的 arch35 主线并存于同一目录，阅读时不要混为一谈。
2. **练习**：`EpilogueInitOutWhenZero` 解决什么问题？
   **答案**：当某行 Q 的所有 S2 块都被 mask 掉时，在线 softmax 分母为 0，输出无定义；该 Epilogue 在"累计值为零"时把输出行写成 0（并按 lseMode 写 lse），保证数值确定性。

## 5. 综合实践：为团队写一张「AttentionPioneer 架构速查卡」

把本讲内容串成一个交付物——一份 Markdown 速查卡，含三部分（对应大纲指定的三项练习）：

### 任务 A：TilingData 字段清单（盘点 `inputParamsRegbase`）

1. **实践目标**：产出"字段名 / 类型 / 取值来源 / 作用"四列表格，覆盖 [ai_infra_attention_pioneer_tiling_v2.cpp:L3398-L3505](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/arch35/ai_infra_attention_pioneer_tiling_v2.cpp#L3398-L3505) 中 `PFATilingDataconvert` 写入的全部字段（约 50 个）。
2. **操作步骤**：
   - 逐行抄录赋值语句，按组归类：维度组（bSize/t1Size/t2Size/n2Size/gSize/s1Size/s2Size/dSize/dSizeV/dSizeRope）、标量组（scaleValue/preTokens/nextTokens/sinkLength/implMode）、mask 组（attenMaskShapeType/attenMaskCompressMode/bandIndex）、PA 组（blockSize/blockTableDim2/paBlockNumSum/paLayoutType）、开关组（isGqa/isSoftMaxLseEnable/headNumRatio/isKvContinuous）。
   - 对每个字段在 [ai_infra_attention_pioneer_kernel_noquant_mla.h:L326-L521](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ai_infra_attention_pioneer_kernel_noquant_mla.h#L326-L521)（`ComputeConstexpr`）中找到消费点，验证你写的作用。
3. **需要观察的现象**：同一字段名在 host（写入）与 kernel（读取）两侧的语义是否一致；`dSizeRope` 固定写 64 的依据（文档 rope_head_dim=64，docs L96-L120）。
4. **预期结果**：一张可直接给新同事看的表格；它同时是 4.3 契约的"活文档"。

### 任务 B：前反向 kernel 模块复用说明（backward 的 matmul_modules）

1. **实践目标**：解释反向算子为什么把 matmul 相关代码组织成 `matmul_modules/` 目录，并说明它如何复用 AscendC matmul 的模块机制。
2. **操作步骤**：
   - 浏览 `ai_infra_attention_pioneer_backward/op_kernel/arch35/matmul_modules/` 的目录结构：`copy_cube_in/`（下分 `copy_cube_in_s1s2_align / copy_cube_in_s1s2_preload / copy_cube_in_s1s2_rope` 三个变体目录，每个目录内含 `fag_copy_cube_in_pre.h / fag_copy_cube_in_post.h`）、`cube_in_buffer/`（`fag_cube_in_buffer_pre/post.h`）、`cube_out_buffer/`、`fag_custom_matmul_policy.h`、`fag_flag_data.h`、`matmul_config.h`。
   - 精读 [fag_copy_cube_in_pre.h:L24-L47](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer_backward/op_kernel/arch35/matmul_modules/copy_cube_in/copy_cube_in_s1s2_align/fag_copy_cube_in_pre.h#L24-L47)：`FagCopyCubeInS1S2Pre` 继承 `CopyCubeInBase<IMPL, MM_CFG, INPUT_TYPE>`，用 `MATMUL_USE_MODULE_ON(CubeInBuffer/CopyCubeInParams/MatmulTensorInfo/DataCopyUtils, TAG)` 挂接 matmul 框架的模块。
   - 写一段说明：**pre/post 是"搬入前/搬入后"两个切点；align/preload/rope 是三种搬入策略变体；目录化组织 = 把可替换的搬入策略做成可独立编译的模块**，与前向 `FAKernelNoquantMla` 里手写 `InitInput`（4.5）形成对照——反向 matmul 多（bmm 需求更复杂），把策略下沉到模块层比在 kernel 里写 `if constexpr` 更可维护。
3. **需要观察的现象**：pre 与 post 两个文件类名的后缀差异；三个变体目录命名中的 `s1s2`（按 S1×S2 分块搬运）。
4. **预期结果**：一篇 300~500 字的"前反向 kernel 模块复用"说明，需点出 `MATMUL_USE_MODULE_ON` 的模块挂接机制与 pre/post 切点。

### 任务 C：布局与稀疏模式支持清单

1. **实践目标**：整理 arch35 前向算子"每个场景支持哪些 layout 与 sparse_mode"的矩阵。
2. **操作步骤**：
   - 以 [docs/npu_ai_infra_attention_pioneer.md:L96-L120](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/docs/npu_ai_infra_attention_pioneer.md#L96-L120) 约束为基准，源码逐条验证。
   - 输出表格：P 侧 = TND / D=192 / Dv=128 / N_q=N_kv / 非 PA / sparse_mode ∈ {2,4}；D 侧 = TND_NTD / D=512 / Dv=512 / N_kv=1 / 必须 PA（block_size>0）/ sparse_mode 仅 4 / 必传 query_rope 与 key_rope；公共 = sink_number ∈ {0,128}、rope_head_dim=64、fp16/bf16、\((-{\rm next\_tokens}) \le {\rm pre\_tokens}\)。
   - 用源码锚点背书每行：布局限制 [ai_infra_attention_pioneer_tiling_v2.cpp:L327-L333](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/arch35/ai_infra_attention_pioneer_tiling_v2.cpp#L327-L333)、D 上限 [L372-L375](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/arch35/ai_infra_attention_pioneer_tiling_v2.cpp#L372-L375)、MLA 判定 [L1533-L1536](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/arch35/ai_infra_attention_pioneer_tiling_v2.cpp#L1533-L1536)、sparse 映射 [L3365-L3380](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_host/arch35/ai_infra_attention_pioneer_tiling_v2.cpp#L3365-L3380)。
3. **需要观察的现象**：文档中"sparse 2 仅 P 侧支持"这类不对称限制，在源码中对应哪条分支/报错路径。
4. **预期结果**：一张"场景 × layout × sparse_mode × 必传输入"的核对表，每个单元格附一个源码链接。

三项任务全部为**源码阅读型实践**，无需硬件即可完成；若要在 950 环境运行 docs 示例核对行为，标注**待本地验证**。

## 6. 本讲小结

- **arch35 = Ascend 950（Davinci 3.5 / `__CCE_AICORE__==310`）**，不是大纲标注的 A3——证据链：`AddConfig("ascend950")` 唯一注册、文档支持矩阵、tiling 的 `DAV_3510` 分支、kernel 的 C310 门。
- host tiling 分两层：`DoOpTiling` 做场景校验（仅 TND/TND_NTD、D≤512、MLA 维度判定），`RunBigKernelTilingWithParams` 走完整切分流水线，并把形态编码为 **12 参数 tilingKey**。
- Host↔Device 契约是**适配器模式**：host 私有 `PromptFlashAiInfraAttentionPioneerTilingData` → `PFATilingDataconvert` → CANN 标准 `FlashAttentionScoreSimplifiedTilingData` → kernel `GET_TILING_DATA_WITH_STRUCT` 取回。
- kernel 按 `emptyTensor / dtype / dTemplateType(Aligned576 vs Aligned512/其他)` 分发；P 侧 MLA 走 `FAKernelNoquantMla`，D 侧复用 `BaseApi::FlashAttentionScoreKernelInfer`。
- `FAKernelNoquantMla` 是 **AIC:AIV=1:2 混合核**：AIC 做 bmm1/bmm2，AIV 做在线 softmax，靠 UB 跨核双缓冲 + L1 三缓冲 + `PRELOAD_N` 滞后组成软件流水；因 `actual_seq_lengths` 在设备侧，精确分核由 kernel 读 metadata 张量回填。
- Sink Token 通过 `s2LoopEndIdx += ⌈sinkLength/s2BaseSize⌉` 直接拼进主循环；`flash_attention_interface.cpp` 则展示了"两个 GEMM + 三个 Epilogue"的 CUTLASS 风格替代路线。

## 7. 下一步学习建议

- **反向算子（本仓库 u4-l9 或自行扩展）**：`ai_infra_attention_pioneer_backward` 与前向同构（op_host/op_kernel/op_api 三层 + arch35），重点读 `matmul_modules/` 的 pre/post 模块与 `kernel_dater.h / kernel_sink.h` 等变体，验证综合实践 B 的结论。
- **metadata 配套算子**：找到 `npu_ai_infra_attention_pioneer_metadata` 的 AICPU 实现（仓库外，CANN 侧），对照 [ap_metadata_defs.h](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/ai_infra_attention_pioneer/op_kernel/arch35/ap_metadata_defs.h#L21-L69) 理解"设备侧 tiling"的完整闭环。
- **CANN FA regbase 基建**：`op_kernel/arch35/ai_infra_attention_pioneer_tiling_regbase.h` 中大量结构与 CANN 的 FlashAttentionScoreSimplified 系列同名，可对照 CANN 开源仓库理解"标准结构 + 算子内镜像"的协作方式。
- **单元/系统测试**：`ai_infra_attention_pioneer/tests/` 下的 ut/st 用例是"约束矩阵"（综合实践 C）的可执行版本，跑通一轮 st 能把本讲所有静态结论变成动态验证（需 950 环境，**待本地验证**）。
