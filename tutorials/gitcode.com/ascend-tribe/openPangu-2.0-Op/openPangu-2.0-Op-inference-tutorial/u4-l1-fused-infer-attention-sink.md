# u4-l1 FusedInferAttentionSink：旗舰注意力融合算子

## 1. 本讲目标

本讲进入仓库中体量最大、复杂度最高的推理算子 `ai_infra_fused_infer_attention_sink`（后文简称 FIA Sink）。它的单个算子目录就有 52 个文件、约 2.2 万行代码，是第 2 单元所学「三层结构骨架」的满配版本。学完本讲，你应该能够：

1. 数清并说清 FIA Sink 的接口全貌：30 个输入（其中 27 个可选）、2 个输出、18 个属性是怎么组织的，`tiling_check_*` 系列文件如何用「单参数 → 存在性 → 特性交叉 → 多参数一致性」四级校验兜住这么大的参数空间。
2. 复述多分支 Tiling 的完整决策链：`RouteToFia` 如何按 dtype/rope 拆分/D 组合把请求路由到 GQA 或 MLA 模板，`FiaTilingRegistry` 如何按优先级轮询多个 tiling 类，18 位 TilingKey 又是如何编码出来的。
3. 说出 kernel 侧模板族的组织方式：一个入口函数如何通过 TilingKey 编译期分发到几十种 `FIAType` 模板实例，`FiaKernelNonQuant` 内部三个 service（cube/vector/flashdecode）如何按核类型分工。

## 2. 前置知识

### 2.1 什么是注意力、GQA 与 MLA

注意力是 Transformer 的核心计算：

\[
Attention(Q,K,V)=Softmax\left(\frac{QK^T}{\sqrt{d}}\right)V
\]

其中 \( Q \)、\( K \)、\( V \) 分别是查询、键、值矩阵，\( d \) 是每个注意力头的维度（head_dim）。为了避免内积值过大，除以 \( \sqrt{d} \) 缩放后再做 softmax 归一化。

- **GQA（Grouped-Query Attention）**：多个 query 头共享一组 KV 头，KV 头数 \( N_2 \) 少于 query 头数 \( N_1 \)，\( g = N_1 / N_2 \) 是组大小。这是大多数稠密注意力模型的结构。
- **MLA（Multi-head Latent Attention）**：DeepSeek 风格的结构，query/key 拆成「压缩部分 + rope 部分」，rope 单独成张量（`query_rope`/`key_rope` 输入）。判断依据很简单：这两个输入同时存在即走 MLA 路线。

### 2.2 什么是 attention sink

「sink」指序列开头的若干 token（如 system prompt）在注意力分布中天然占据高权重。推理时通常把前 `sink_number` 个 token 强制纳入注意力计算（不被滑窗/稀疏模式裁掉）。本算子的 ST 测试里能直观看到：mask 前 128 位保留、128 之后才按滑窗屏蔽。

### 2.3 推理场景的几个关键开关

| 概念 | 含义 |
| --- | --- |
| 全量 vs 增量 | 全量（Prompt）一次算完整个 prompt 序列；增量（Incre）每步只有 1 个新 query token，KV 已在 cache 里 |
| PageAttention（PA） | KV cache 按 block 分页存放，用 `block_table` 索引页；`block_size` 是页大小 |
| 布局（layout） | Q/KV 的内存排布：BSH、BNSD、BSND、TND、NTD、NZ 等，TND 的 T 是各 batch 序列长度累加和 |
| 量化 | KV 用 int8/int4 存储以省显存；本算子当前版本只支持「非量化」（FP16/BF16），量化输入只在校验层被识别后拒绝 |
| AIC / AIV | 昇腾芯片的 Cube 核（矩阵乘）与 Vector 核（向量/规约）。FIA Sink 用 AIC:AIV = 1:2 的混合核任务 |
| FlashDecode | 增量解码时把超长 KV 序列在 S2 维切开、多核并行算部分和再规约的加速技术 |

### 2.4 与前面讲义的衔接

第 2 单元以 `ai_infra_scatter_block_update` 为标本学过算子三层结构（u1-l3）、aclnn 两段式（u2-l2）、Tiling 七步框架（u2-l3）、AscendC kernel（u2-l4）。本讲会把每个知识点都升级到「几十路分支」的规模。特别要注意：**FIA Sink 没有用 u2-l3 讲的仓库公共 `TilingBaseClass` 七步框架，而是注意力族自建了一套 `FiaTilingBase` + `FiaTilingRegistry` 模板轮询框架**，两者思想同源（三态返回值 + 多模板轮询），实现各自独立。

## 3. 本讲源码地图

算子根目录：`ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/`，共 52 个文件。先给总账（这也是综合实践的答案基础）：

| 层 | 文件数 | 关键文件 |
| --- | --- | --- |
| op_api | 6 | `aclnn_ai_infra_fused_infer_attention_sink.cpp/.h`（V1 接口）、`..._v2.cpp/.h`（V2 接口）、`ai_infra_...cpp/.h`（L0 封装） |
| op_host | 22 | 见下表细分 |
| op_kernel | 12 | 见下表细分 |
| docs | 3 | V1/V2 aclnn 文档、`npu_fused_infer_attention_sink.md`（torch 接口文档） |
| tests | 8 | 1 个 ST、2 个 UT（tiling + infershape）、op_api UT、多个 CMakeLists |

op_host 的 22 个文件细分：

| 类别 | 文件 | 职责 |
| --- | --- | --- |
| 原型 | `_def.cpp`、`_infershape.cpp` | OpDef 签名注册、输出形状推导 |
| tiling 入口 | `_tiling.cpp/.h`、`_tiling_register.cpp`、`_tiling_v3.cpp/.h` | 框架注册、路由分流、V3 主流程 |
| tiling 辅助 | `_tiling_compile_info.h`、`_tiling_index.h`、`_tiling_info_parser.cpp/.h` | 编译信息缓存、输入索引常量、参数解析器 |
| 参数校验 | `_tiling_check.cpp/.h` + `_check_single_para.cpp` + `_check_existence.cpp` + `_check_feature.cpp` + `_check_consistency.cpp` | 四级校验体系（本讲 4.2） |
| tiling 模板 | `fia_tiling_nonquant_sink.cpp/.h`（GQA）、`fia_tiling_nonquant_mla_sink.cpp/.h`（MLA） | 两个真正干活的 tiling 类 |
| 回退 | `fallback_ai_infra_fused_infer_attention_sink.cpp` | 回退实现（u5-l3 详讲） |

op_kernel 的 12 个文件细分：

| 类别 | 文件 | 职责 |
| --- | --- | --- |
| 入口 | `ai_infra_fused_infer_attention_sink.cpp`（2301 行） | kernel 入口 + TilingKey 分发表 |
| key 与元数据 | `_tilingkey.h`、`_metadata.h` | TilingKey 宏表、metadata 张量布局 |
| kernel 主类 | `fia_kernel_nonquant_sink.h`（GQA）、`fia_kernel_nonquant_mla_sink.h`（MLA） | 调度主流程 |
| 基本块实现 | `fia_block_cube_nonquant_gqa_sink.h`、`fia_block_cube_nonquant_mla_sink.h`、`fia_block_cube_nonquant_sink.h`、`fia_block_vec_nonquant_sink.h`、`fia_block_vec_nonquant_mla_sink.h`、`fia_block_vec_flashdecode_sink.h` | cube/vector/flashdecode 三类基本块 |
| 公共设施 | `kernel_common_sink.h` | 跨核同步等公共原语 |

此外还依赖注意力族公共目录 `ascendc/src/ops-transformer/attention/common/` 下的 `op_host/fia_tiling_base.h`、`op_host/fia_tiling_templates_registry.h`、`op_kernel/fia_public_define.h`（`FIAType` 定义）等。

## 4. 核心概念与源码讲解

### 4.1 接口全貌：30 个输入的 OpDef 与 aclnn 两段式

#### 4.1.1 概念说明

FIA Sink 的接口是一个「超平面」：必选输入只有 `query`、`key`、`value`，其余 27 个张量输入全部可选（量化 scale、mask、rope、sink、metadata 等），外加 18 个属性。可选输入的存在与否本身就是「场景维度」：有没有 `block_table` 决定是否走 PageAttention，有没有 `query_rope`/`key_rope` 决定 MLA 还是 GQA。这就是为什么后面的校验和 Tiling 分支会爆炸式增长——每个可选输入都是一枚场景开关。

对照 u2-l2 学过的 aclnn 两段式：本算子的 op_api 层还是那个骨架（GetWorkspaceSize 同步准备 + 执行段异步下发），但多了一个 `GetMaxWorkspaceSize` 变体，专供 aclgraph 静态图在编译期取最大 workspace。

#### 4.1.2 核心流程

OpDef 注册的关键点（承接 u2-l1）：

1. `key`/`value` 是 `ParamType(DYNAMIC)` 的动态输入（TensorList），batch 维按 batch 展开成多个张量。
2. 每个输入的 `DataType({...})` 列表是一张**组合表**：query 一个下标、key 一个下标、value 一个下标……同一列下标组成一种合法 dtype 组合（如全 FP16、全 BF16、Q 为 FP16 + KV 为 INT8 等），FIA Sink 的 query 有 105 个组合。
3. 18 个属性大多带默认值（`pre_tokens=2147483647`、`input_layout="BSH"`、`sink_number=0`……），默认值即「未指定」。

aclnn 层的流程（比 u2-l2 多三件新东西）：

```text
GetMaxWorkspaceSize（aclgraph 用，转调 GetWorkspaceSize）
GetWorkspaceSize:
  1. TensorPreProcess: KV 是 INT32 存储的 INT4 时，viewShape 末维 ×8、dtype 改标 DT_INT4
  2. softmaxLseFlag=false 时伪造一个 0 元素占位张量，让 L0 调用签名统一
  3. CREATE_EXECUTOR 建执行器；输出为空张量则直接返回 0 workspace
  4. 连续化：query 等 7 个张量逐个 Contiguous；key/value 的 TensorList 逐个处理
     —— 关键差异：非连续张量不再拷贝，而是 CreateView 保留 stride 零拷贝下发
  5. l0op::AiInfraFusedInferAttentionSink(...) 把算子登记进 executor
  6. l0op::ViewCopy 把 L0 输出拷回用户输出张量
  7. GetWorkspaceSize 汇总 + ReleaseTo(executor)
执行段: CommonOpExecutorRun(workspace, workspaceSize, executor, stream)
```

#### 4.1.3 源码精读

**OpDef 的输入清单（节选）**。[ai_infra_fused_infer_attention_sink_def.cpp:L23-L44](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_def.cpp#L23-L44) 注册必选输入 `query`，`DataType` 列表 105 项即 105 种 dtype 组合；[L45-L69](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_def.cpp#L45-L69) 注册 `key`，注意 `ParamType(DYNAMIC)`——key/value 是 TensorList。

**sink 与 metadata 专属输入**。[ai_infra_fused_infer_attention_sink_def.cpp:L501-L525](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_def.cpp#L501-L525) 连续注册了 5 个本算子特有的输入：`metadata`（AICPU 元数据算子的产物，携带分核信息）、`learnable_sink`、`key_sink`、`key_rope_sink`、`value_sink`（sink token 的 KV 直接作为输入参与计算）。

**输出与属性**。[ai_infra_fused_infer_attention_sink_def.cpp:L526-L565](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_def.cpp#L526-L565) 注册两个输出 `attention_out`（REQUIRED）与 `softmax_lse`（REQUIRED 但 dtype 固定 FLOAT）以及 18 个属性，其中 `sink_number`、`batch_invariant`、`out_dtype` 是 FIA Sink 相对通用 IFA 的新增项。

**INT4 视图换算**。[aclnn_ai_infra_fused_infer_attention_sink.cpp:L42-L87](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_api/aclnn_ai_infra_fused_infer_attention_sink.cpp#L42-L87) 的 `TensorPreProcess`：宿主侧 INT4 是按 INT32 打包存储的（1 个 int32 装 8 个 int4，见 L40 的 `INT4_NUMS_IN_INT32 = 8`），这里把 TensorList 中每个 KV 张量的 viewShape 末维乘 8、dtype 改标为 `DT_INT4`，让下游统一按 int4 语义理解形状。

**softmax_lse 占位符**。[aclnn_ai_infra_fused_infer_attention_sink.cpp:L149-L170](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_api/aclnn_ai_infra_fused_infer_attention_sink.cpp#L149-L170) 在 `softmaxLseFlag == false` 时现场 `aclCreateTensor` 一个 shape 为 `{0}` 的空张量顶包——这样 L0 调用永远拿到合法指针，无需两套调用路径。

**非连续输入的 CreateView 处理**。[aclnn_ai_infra_fused_infer_attention_sink.cpp:L173-L205](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_api/aclnn_ai_infra_fused_infer_attention_sink.cpp#L173-L205) 与 u2-l2 的 scatter 算子不同：这里**连续张量**才走 `l0op::Contiguous`（占 workspace 拷贝），**非连续张量**反而走 `executor->CreateView` 零拷贝保留 stride——因为 PA 场景的 KV 非连续是常态（block 级不连续），拷贝代价太高，kernel 侧按 stride 偏移访问。[L249-L278](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_api/aclnn_ai_infra_fused_infer_attention_sink.cpp#L249-L278) 的 `ProcessTensorListContiguous` 再把逐张量处理包装成 TensorList 版本。

**两段式主体**。[aclnn_ai_infra_fused_infer_attention_sink.cpp:L555-L562](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_api/aclnn_ai_infra_fused_infer_attention_sink.cpp#L555-L562) 建 executor 并对空输出短路；[L565-L635](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_api/aclnn_ai_infra_fused_infer_attention_sink.cpp#L565-L635) 连续化后调 `l0op::AiInfraFusedInferAttentionSink` 登记 31+2 个参数；[L640-L652](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_api/aclnn_ai_infra_fused_infer_attention_sink.cpp#L640-L652) 用 `l0op::ViewCopy` 回拷输出并汇总 workspace；[L657-L664](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_api/aclnn_ai_infra_fused_infer_attention_sink.cpp#L657-L664) 执行段仍是熟悉的 `CommonOpExecutorRun`。

#### 4.1.4 代码实践

**实践目标**：不运行任何代码，靠「读签名 + 读文档」画出 FIA Sink 的场景矩阵。

**操作步骤**：

1. 打开 [ai_infra_fused_infer_attention_sink_def.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_def.cpp#L23-L565)，把 30 个输入按「必选 / 可选-场景开关 / 可选-量化 / 可选-sink」四类抄成清单。
2. 对照 [docs/npu_fused_infer_attention_sink.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/docs/npu_fused_infer_attention_sink.md#L1-L60) 的函数原型，把 torch 层参数名（如 `return_softmax_lse`）映射到 OpDef 属性名（`softmax_lse_flag`）。
3. 统计 query 输入 `DataType({...})` 列表的项数，验证是否 105 项。

**需要观察的现象 / 预期结果**：清单应呈现出「3 必选 + 一批场景开关 + 一批量化开关 + 5 个 sink/metadata 专属」的结构；query 的 dtype 列表项数为 105（数一遍 def 文件 L25-L42 的花括号内元素，可用 `grep -o "ge::DT_[A-Z0-9]*" | wc -l` 辅助）。文档里明确「使用本接口前必须先调用 `_npu_fused_infer_attention_sink_metadata` 获取分核信息」——这为下一讲（u4-l2 AICPU 元数据算子）埋下伏笔。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `softmax_lse` 在 OpDef 里是 REQUIRED 输出，torch 侧却有 `return_softmax_lse=False` 的用法？
**答案**：OpDef 面向图执行器，输出槽位必须齐备；torch/aclnn 层在 flag 为 false 时伪造一个 0 元素占位张量（`AiInfraFusedInferAttentionSinkProcessSoftmaxLse`），让两套场景共用同一条 L0 调用路径，执行完直接丢弃占位输出。

**练习 2**：`key` 输入的 `ParamType(DYNAMIC)` 与 `query` 的 `ParamType(REQUIRED)` 有什么行为差异？
**答案**：DYNAMIC 输入在 aclnn 层是 `aclTensorList`（一组张量），对应图上的动态输入节点（按 batch 展开），kernel 入口拿到的 `key` 指针指向 TensorList 描述结构；REQUIRED 是固定单张量。这也解释了为什么 aclnn 层要专门写 `ProcessTensorListContiguous` 来处理 KV。

**练习 3**：本算子对非连续 KV 用 CreateView 而不是 Contiguous，和 u2-l2 scatter 算子的原地更新用 CreateView，原因相同吗？
**答案**：动机不同但原理相同——都是「不想破坏原有内存布局」。scatter 是因为原地写入必须保留 stride/offset 才能写回正确位置；FIA 是因为 PA 场景 KV 的 block 级不连续是常态，拷贝代价高，kernel 侧有能力按 stride 访问，于是零拷贝下发。

### 4.2 参数校验：tiling_check 四级校验体系

#### 4.2.1 概念说明

30 个输入 × 各种 dtype/layout/量化组合，合法子集只是全空间的一小部分。FIA Sink 把校验从 tiling 计算中完全剥离，独立成 6 个文件、约 2700 行的校验子系统，入口是 `TilingCheck::Check(fiaInfo)`。设计纪律写在注释里：**「Check 函数只做校验，不能修改 fiaInfo 中的信息」**——校验与计算严格单向数据流，避免校验顺手改参数带来的隐藏耦合。

四级校验各管一段：

| 级别 | 文件 | 校验什么 | 例子 |
| --- | --- | --- | --- |
| ① 单参数 | `_tiling_check_single_para.cpp`（630 行） | 每个输入/属性**孤立地**看：dtype 在不在白名单、维度数对不对、属性值合法不合法 | `sparseMode` 只允许 0/1/2/3/4 |
| ② 存在性 | `_tiling_check_existence.cpp`（347 行） | 场景开关的**搭配**：MLA 场景必须给哪些输入、禁止给哪些 | 走 MLA 时 `query_rope`/`key_rope` 必须存在 |
| ③ 特性交叉 | `_tiling_check_feature.cpp`（717 行） | 跨参数的**业务规则**：head_dim 上限、NZ 布局对齐、mask 与 sparse_mode 交互、不支持特性清单 | `vHeadDim ≤ 512`、`ropeHeadDim ≤ 64` |
| ④ 多参数一致性 | `_tiling_check_consistency.cpp`（895 行） | 张量之间的**形状/(dtype)协变**：Q 与 attention_out 形状一致、KV 的 batch 对齐、sink 张量形状 | Q 与 Q_rope 的 B/N 必须一致 |

#### 4.2.2 核心流程

```text
TilingAiInfraFusedInferAttentionSinkV3(context)
  ├─ FiaInfoParser parser(context); parser.Parse(fiaInfo)   # 先解析：把 30 输入+18 属性浓缩成 FiaTilingInfo
  ├─ TilingCheck::Check(fiaInfo)                            # 再校验（只读 fiaInfo）
  │    └─ FiaTilingCheck::Process()
  │         ├─ Init()                        # 从 fiaInfo 拷贝到成员，供四级校验共享
  │         ├─ CheckSinglePara()             # ① 30 个 CheckSingleParaXxx 串行
  │         ├─ CheckParaExistence()          # ② 按 ropeMode 分流 MLA/GQA 两套存在性规则
  │         ├─ CheckFeature()                # ③ 按 ropeMode+quantMode 分流 GQA/MLA 特性规则
  │         └─ CheckMultiParaConsistency()   # ④ 形状一致性
  └─ FiaTilingRegistry::DoTilingImpl(context, &fiaInfo)     # 最后才算 tiling
```

任何一级失败立即 `GRAPH_FAILED`，错误信息经 `OPS_LOG_E` 带参数名与实际值输出。

#### 4.2.3 源码精读

**四级调度主体**。[ai_infra_fused_infer_attention_sink_tiling_check.cpp:L136-L153](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_tiling_check.cpp#L136-L153)：`Process()` 用一个 `if` 把四级串起来（L139-L143），`TilingCheck::Check` 是静态入口（L148-L153），注释再次强调只读约束。

**① 单参数级**。[ai_infra_fused_infer_attention_sink_tiling_check_single_para.cpp:L607-L630](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_tiling_check_single_para.cpp#L607-L630) 的 `CheckSinglePara` 把 `CheckSingleParaQuery` 到 `CheckSingleParaSparseMode` 共 30 个子检查按 `||` 短路串联；每个子检查复用 `tiling_check.h` 里模板化的 `CheckDtypeSupport`/`CheckDimNumSupport`/`CheckAttrValueSupport` 等工具（见 [ai_infra_fused_infer_attention_sink_tiling_check.h:L81-L110](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_tiling_check.h#L81-L110)），新增一个参数的校验只需加一个子函数并挂进链。

**② 存在性级**。[ai_infra_fused_infer_attention_sink_tiling_check_existence.cpp:L335-L347](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_tiling_check_existence.cpp#L335-L347)：先 `CheckRopeExistence` + `CheckDtypeAndSetQuantFlag`（这是唯一允许写状态的地方——判定 quantMode），再按 `ropeMode_ == ROPE_SPLIT` 分流到 `CheckParaExistenceMla()` 或 `CheckParaExistenceGqa()`。存在性规则大量使用 `CheckExistsByMap`/`CheckNotExistsByMap`（参数名到指针的 map 批量断言），代码极为紧凑。

**③ 特性交叉级**。[ai_infra_fused_infer_attention_sink_tiling_check_feature.cpp:L524-L556](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_tiling_check_feature.cpp#L524-L556) 的 `CheckFeatureHeadDim`：`vHeadDim ≤ 512`、`ropeHeadDim ≤ 64`，且 PA + NZ 布局时 headDim 必须 16 对齐——head_dim 相关的第一道闸门。[L557-L580](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_tiling_check_feature.cpp#L557-L580) 的 `CheckFeatureGqaNoquant`/`CheckFeatureGqa` 展示了按 quantMode 的再分流：当前版本量化模式直接被 `fiaSink Only Support NoQuant` 拦下。

**④ 一致性级**。[ai_infra_fused_infer_attention_sink_tiling_check_consistency.cpp:L881-L897](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_tiling_check_consistency.cpp#L881-L897) 的 `CheckMultiParaConsistency` 先 `SetFiaShapeCompare()` 建立形状比较器（check.h 中那排 `FiaTilingShapeCompare` 成员），再串联 `CheckQAndQRope`、`CheckKV`、`CheckAttenOut`、`CheckParamSinkShape`、`CheckMask`、`CheckSoftmaxLse` 等 10 项跨张量检查。

#### 4.2.4 代码实践

**实践目标**：以「sparse_mode 只支持 0~4」为例，走完一条校验规则从触发到报错的全链路（纯源码阅读型实践）。

**操作步骤**：

1. 在 [ai_infra_fused_infer_attention_sink_tiling_check_single_para.cpp:L596-L606](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_tiling_check_single_para.cpp#L596-L606) 找到 `CheckSingleParaSparseMode` 中的 `sparseModeList` 与 `CheckAttrValueSupport` 调用。
2. 回溯两级：它被 [L607-L630](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_tiling_check_single_para.cpp#L607-L630) 的 `CheckSinglePara` 调用，后者被 [tiling_check.cpp:L139-L143](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_tiling_check.cpp#L139-L143) 调用，再上是 [tiling_v3.cpp:L44-L47](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_tiling_v3.cpp#L44-L47)。
3. 在脑中代入 `sparse_mode=5`：确认 `VecContains(sparseModeList, 5)` 为 false → `CheckAttrValueSupport` 打 `OPS_LOG_E` → `CheckSinglePara` 返回 FAILED → `Process` 返回 FAILED → V3 主流程返回 `GRAPH_FAILED`。
4. 若本地有昇腾环境，可运行 ST（见 4.4.4）后故意把测试脚本中 `sparse_mode=4` 改成 5，观察日志里的报错参数名是否为 `sparseMode`。（无环境则标注：待本地验证。）

**需要观察的现象 / 预期结果**：报错日志应形如 `sparseMode only supports 0/1/2/3/4, but got 5`，且算子在 tiling 阶段（而非 kernel 阶段）即失败——这就是把校验前置到 host 侧的价值。

#### 4.2.5 小练习与答案

**练习 1**：为什么要把校验拆成四个文件而不是一个大函数？
**答案**：按「校验的性质」正交拆分——单参数规则互不依赖可机械罗列；存在性/特性规则随场景（MLA/GQA、量化与否）组合变化；一致性规则需要形状比较器。拆开后每类规则独立演化（如新增一种量化模式只改 existence + feature 两个文件），且 `Check` 只读 `fiaInfo` 的纪律容易维持。

**练习 2**：`CheckDtypeAndSetQuantFlag` 明明会设置 `quantMode_` 成员，为什么不算违反「Check 不能修改 fiaInfo」？
**答案**：它修改的是 `FiaTilingCheck` 自己的成员 `quantMode_`（校验器内部的路由状态），不改传入的 `FiaTilingInfo &fiaInfo_`。下游 tiling 模板读到的 fiaInfo 不受校验影响。

**练习 3**：如果新增第 5 种 sparse_mode，需要动哪些文件？
**答案**：`tiling_check_single_para.cpp` 的 `sparseModeList`；若该模式改变 mask 语义还要动 `tiling_check_feature.cpp` 的 `CheckFeatureGqaNoquantMask`/`CheckFeatureMask`；kernel 侧的 mask 处理与 metadata 算子的 sparse_mode 分支（下一讲）也可能要同步。OpDef 里 `sparse_mode` 属性本身是 Int 无枚举约束，不用改 def。

### 4.3 多分支 Tiling：路由 → V3 主流程 → 模板注册表轮询

#### 4.3.1 概念说明

FIA Sink 的 host 侧 tiling 不是「一个函数算到底」，而是一条三级流水：

1. **路由层**（`RouteToFia`）：快速判断这个请求能不能走 FIA 新路径（dtype、rope 拆分、D 组合白名单）。
2. **V3 主流程**（`TilingAiInfraFusedInferAttentionSinkV3`）：Parse → Check → DoTilingImpl 三步。
3. **模板注册表**（`FiaTilingRegistry`）：按 SOC 版本 + 算子名取出优先级排序的 tiling 模板链，逐个尝试直到某个模板认领。

这与 u2-l3 讲的仓库公共 `TilingBaseClass` 七步框架是**平行的两套实现**：注意力族自建了 `FiaTilingBase`（只有 `InitTilingInfo`/`IsCapable`/`DoOpTiling` 三个纯虚函数）和 `FiaTilingRegistry`（按 `priority` 的 map 排序轮询），思想同样是「三态返回值 + 多模板链」，但注册表按 SOC 版本再分了一层。

TilingData 本身也按职责切成 7 个结构体（基础参数/PA/mask/内切/worksapce/外切分核/FlashDecode），像一份多页施工图。

#### 4.3.2 核心流程

```text
框架触发 IMPL_OP_OPTILING(AiInfraFusedInferAttentionSink).Tiling(DoOpTiling...)
  └─ DoOpTilingAiInfraFusedInferAttentionSink   [tiling.cpp]
       ├─ 平台检查：ASCEND910_55 直接 FAILED（不支持）
       └─ TilingAiInfraFusedInferAttentionSink
            └─ RouteToFia(context)?                [tiling_v3.cpp]
                 ├─ SOC=310P → false
                 ├─ q/k dtype 必须 FP16/BF16 且相同
                 ├─ query_rope+key_rope 都在 → 先试 GQA 约束再试 MLA 约束
                 └─ 否则（无 rope）→ 只试 GQA 约束
                 true → TilingAiInfraFusedInferAttentionSinkV3
                     ├─ Parse(fiaInfo)         # 1100 行的解析器
                     ├─ Check(fiaInfo)         # 4.2 的四级校验
                     └─ FiaTilingRegistry.DoTilingImpl
                          ├─ 取 soc_version + op_type 的模板 map（priority 升序）
                          └─ 逐个模板 DoTiling:
                               InitTilingInfo → IsCapable?
                                 否 → GRAPH_PARAM_INVALID，试下一个
                                 是 → DoOpTiling:
                                        isMaxWorkspace? → CalcMaxWorkspaceSize
                                        否 → Split→FillTiling→CalcBlockDim
                                             →CalcScheduleMode→CalcWorkspaceSize
                                        GenTilingKey → SetBlockDim/SetTilingKey/
                                        SetWorkspaceSize/SetTilingData/SetScheduleMode
```

GQA 模板认领的条件（`IsCapable` + 路由 D 白名单）：

- D 组合 ∈ {（128,0,128）、（64,0,64）、（128,64,128）、（192,64,128）}（qkHeadDim, ropeHeadDim, vHeadDim）；
- 无 pse_shift/query_padding_size/kv_padding_size/quant_scale2/quant_offset2 时升级为高性能特化模板 `HIGH_PERFORMANCE_GQA`，否则用泛化模板 `GENERAL_GQA`。

MLA 模板的 D 组合只有一个：**（512, 64, 512）**——这正是 DeepSeek MLA 的标准维度。

优先级编码是三位数约定（注释原文）：百位=量化场景（0xx 非量化）、十位=结构（x0x MLA、x1x GQA）、个位=特化程度。所以 MLA 非量化 = 009，GQA 非量化 = 019，MLA 排在前面。

#### 4.3.3 源码精读

**框架注册**。[ai_infra_fused_infer_attention_sink_tiling_register.cpp:L20-L28](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_tiling_register.cpp#L20-L28) 用 `IMPL_OP_OPTILING(AiInfraFusedInferAttentionSink).Tiling(DoOpTiling...).TilingParse<...>(...)` 把 tiling 入口挂到框架，这是 u2-l3 所学注册方式的直接复用。

**入口与 SOC 拦截**。[ai_infra_fused_infer_attention_sink_tiling.cpp:L25-L36](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_tiling.cpp#L25-L36) 的 `TilingAiInfraFusedInferAttentionSink` 只做一件事：`RouteToFia` 通过才进 V3；[L38-L57](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_tiling.cpp#L38-L57) 的 `DoOpTiling...` 先拦 `ASCEND910_55`；[L59-L67](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_tiling.cpp#L59-L67) 还有 `DEVICE_IMPL_OP_OPTILING` 注册的 `DeviceDoOpTiling...`——tiling 下沉到设备侧执行的入口（u5-l4 专题）。

**V3 主流程**。[ai_infra_fused_infer_attention_sink_tiling_v3.cpp:L36-L50](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_tiling_v3.cpp#L36-L50)：Parse → Check → `FiaTilingRegistry::GetInstance().DoTilingImpl(context, &fiaInfo)` 三行主流程；L26 的 `REGISTER_TILING_DATA_CLASS` 把 TilingData 结构登记到框架。

**GQA 的 D 白名单**。[ai_infra_fused_infer_attention_sink_tiling_v3.cpp:L182-L200](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_tiling_v3.cpp#L182-L200) 的 `CheckGqaDSupport` 列出 4 种 D 组合；[L346-L360](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_tiling_v3.cpp#L346-L360) 的 `CheckMlaDSupport` 只认（512,64,512）。注意 `GetQkvD`（[L125-L180](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_tiling_v3.cpp#L125-L180)）要先按 input_layout 从不同维度位置把 D 抠出来——BSH 布局 D=H/N，NZ 布局 D=D1*D0。

**路由总闸**。[ai_infra_fused_infer_attention_sink_tiling_v3.cpp:L376-L414](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_tiling_v3.cpp#L376-L414) 的 `RouteToFia`：310P 拒绝 → dtype 必须非量化 → rope 拆分场景先 GQA 后 MLA、无 rope 场景只走 GQA，每条命中都打 `OPS_LOG_I` 日志（如 "FIA RopeSplit MLA No quant."），排查时可按日志确认走了哪条路。

**模板注册表**。[fia_tiling_templates_registry.h:L97-L128](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/common/op_host/fia_tiling_templates_registry.h#L97-L128) 的 `DoTilingImpl`：按 SOC 版本与 op_type 取 `std::map<int32_t, FiaTilingClassCase>`（priority 升序即调用序），逐个 `DoTiling`，只有返回 `GRAPH_PARAM_INVALID` 才继续轮询，其余返回值立即定案。[L186-L188](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/common/op_host/fia_tiling_templates_registry.h#L186-L188) 的 `REGISTER_TILING_TEMPLATE_FIA` 宏用静态对象在 main 前完成注册——与 u2-l1 的 `OP_ADD` 异曲同工。

**模板基类**。[fia_tiling_base.h:L42-L67](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/common/op_host/fia_tiling_base.h#L42-L67)：`FiaTilingBase::DoTiling` 固化 `InitTilingInfo → IsCapable → DoOpTiling`，注释明确三态返回值语义（SUCCESS 定案 / FAILED 中止 / PARAM_INVALID 换下一个类）。

**GQA 模板的认领与施工**。[fia_tiling_nonquant_sink.cpp:L113-L131](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/fia_tiling_nonquant_sink.cpp#L113-L131) 的 `IsCapable` 只认非量化 + Q/K 同 dtype + 非空张量；[L213-L235](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/fia_tiling_nonquant_sink.cpp#L213-L235) 的 `InitParams` 按 D 组合 + 特性开关选 `HIGH_PERFORMANCE_GQA` 或 `GENERAL_GQA`；[L698-L720](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/fia_tiling_nonquant_sink.cpp#L698-L720) 的 `DoOpTiling` 是施工总装：Split→FillTiling→CalcBlockDim→CalcScheduleMode→CalcWorkspaceSize→GenTilingKey→五个 Set；文件尾 [L725-L733](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/fia_tiling_nonquant_sink.cpp#L725-L733) 以 priority=19 注册到 ASCEND910B。MLA 版在 [fia_tiling_nonquant_mla_sink.cpp:L632-L636](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/fia_tiling_nonquant_mla_sink.cpp#L632-L636) 以 priority=9 注册——两处注释都写明了三位数优先级编码规则。

**TilingData 七件套**。[ai_infra_fused_infer_attention_sink_tiling.h:L35-L59](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_tiling.h#L35-L59) 是基础参数（b/n2/g/s1/s2/headDim、scaleValue、usedCoreNum、各种 flag）；[L63-L86](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_tiling.h#L63-L86) 是 PA 参数（blockSize、各 KV 的 stride 数组）与 mask 参数（preToken/nextToken/sinkNumber）；[L107-L140](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_tiling.h#L107-L140) 是外切分核数组（每个 AIC 核负责的 bN2End/gS1End/s2End，`FIA_MAX_AIC_CORE_NUM = 26`）与 FlashDecode 负载均衡参数；[L143-L151](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_tiling.h#L143-L151) 把 7 个结构体拼成一个 `AiInfraFusedInferAttentionSinkTilingData`。对照 u2-l3：宏还是 `BEGIN_TILING_DATA_DEF`/`TILING_DATA_FIELD_DEF` 那一套，只是规模从 4 个字段涨到 7 组几十个字段。

#### 4.3.4 代码实践

**实践目标**：用一张「决策树笔记」锁定一次典型调用会走哪条 tiling 路径（源码阅读型实践）。

**操作步骤**：

1. 选定场景：`q=bfloat16`、`kv=bfloat16`、无 `query_rope`/`key_rope`、`head_dim=128`、带 `block_table`。
2. 沿 [tiling_v3.cpp:L376-L414](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_tiling_v3.cpp#L376-L414) 的 `RouteToFia` 逐条件打勾：非 310P ✓；dtype BF16 且相等 ✓；无 rope 拆分 → `CheckGqaConstrain`。
3. 在 `CheckGqaConstrain`（[L280-L288](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_tiling_v3.cpp#L280-L288)）里分别核对 layout、D 组合（128,0,128）✓、特性（无 pse_shift 等）✓。
4. 结论应是：命中日志 `FIA GQA No quant.` → V3 → 注册表从 priority=9 的 `FiaTilingNonQuantMla` 先试（其 `IsCapable` 因 rope 模式不符返回 PARAM_INVALID）→ priority=19 的 `FiaTilingNonQuant` 认领 → 因特性开关全关升级 `HIGH_PERFORMANCE_GQA`。
5. 换一个场景重做：`query_rope`/`key_rope` 都给且 D=（512,64,512），确认这次是 MLA 模板直接认领。

**需要观察的现象 / 预期结果**：两种场景各得到一条完整决策路径；若在有环境机器上开 DEBUG 日志运行 ST 用例，应能在日志中看到 `FIA GQA No quant.` 与 `Do general op tiling success priority=19` 字样（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：MLA 模板 priority=9、GQA 是 19，为什么 MLA 要排前面？
**答案**：按三位数编码约定，十位区分结构（x0x=MLA、x1x=GQA），数值小者优先。MLA 是 rope 拆分的特化结构，先试特化模板可以让符合 MLA 特征的请求不被泛化 GQA 模板「抢走」；GQA 模板的 `IsCapable` 也不区分 rope 模式，若排前面会把 MLA 请求吞掉。本质是「特化优先、泛化兜底」。

**练习 2**：`FiaTilingNonQuant::ZeroTensorProcess` 把 `s2Size=0` 改成 1024，这算不算违反「Check 不能修改 fiaInfo」？
**答案**：不算——那条纪律只约束 Check 阶段；`ZeroTensorProcess` 发生在 tiling 模板的 `InitParams` 里，目的是给空 KV 序列一个合法默认值避免 matmul/softmax 的 tiling 计算除零，kernel 计算仍用真实 seqSize=0。两条纪律的作用域不同。

**练习 3**：如果不新增文件，如何新增一个「KV 伪量化」的 tiling 模板？
**答案**：写一个继承 `FiaTilingBase` 的新类（复用 `fia_tiling_nonquant_sink.cpp` 的结构），在文件尾用 `REGISTER_TILING_TEMPLATE_FIA(AiInfraFusedInferAttentionSink, 新类, {SOC列表}, 1xx)` 注册——按编码约定伪量化百位是 1，如 109；`IsCapable` 里写认领条件（如 KV 为 INT8）。注册表会自动把它排进轮询链。

### 4.4 Kernel 模板族：TilingKey 编码与三 service 协同

#### 4.4.1 概念说明

host 侧算出的 TilingKey 到了 kernel 侧变成**编译期分支**：CANN 的 kernel 编译框架为每个 TilingKey 生成一份独立的二进制变体，入口文件里 `#if (TILING_KEY_VAR == ...)` 预处理链在编译时只保留命中分支。FIA Sink 的 TilingKey 是 18 位十进制编码，宏名本身就是可读的场景串：

```text
QF16_KVF16_OUTF16_BNSD_KVNZ_PAGEDCACHE_FLASHDECODING_MLA_TILING
└Q/KV/OUT 的 dtype ┘└Q布局┘└KV布局┘└PA┘└调度模式┘└结构┘
= 105000000020300000
```

即：宏名 = 「Q dtype_KV dtype_OUT dtype_Q 布局_KV 布局_是否 PA_是否 FlashDecode_GQA/MLA」，`C1V1_` 前缀表示 CV 1:1 核型变体。

kernel 主体是模板类 `FiaKernelNonQuant<FIAT>`，模板参数 `FIAType` 把 dtype、布局、PA/FD 等场景全部编码为**编译期常量**，配合三个 service：

- `matmulService`（cube 核）：QK^T 与 PV 两次矩阵乘（MM1/MM2）；
- `vectorService`（vector 核）：softmax、scale、搬运等向量操作；
- `fdService`（flashdecode）：增量解码的并行分解与规约。

入口用 `KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_MIX_AIC_1_2)` 声明 1:2 的 AIC/AIV 混合核任务，`Process()` 里 `ASCEND_IS_AIV` 按核类型各取所需 service。

#### 4.4.2 核心流程

```text
kernel 入口 ai_infra_fused_infer_attention_sink(query, ..., softmaxLse, workspace, tiling)
  ├─ TPipe tPipe; user = GetUserWorkspace(workspace)
  ├─ KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_MIX_AIC_1_2)      # AIC:AIV = 1:2
  ├─ 编译期: #if (ORIG_DTYPE_QUERY == DT_FLOAT16) ...       # dtype 粗分
  │    TILING_KEY_IS(每个支持的 key)                        # 声明支持面
  │    #if (TILING_KEY_VAR == 具体key)                      # 编译期精分
  │        INVOKE_FIA_OP_GENERAL_IMPL(FiaKernelNonQuantMla,
  │            FiaBlockCubeNonQuantMla,       # cube 基本块
  │            FiaBlockVecNonQuantMla,        # vec 基本块
  │            FiaBlockVecFlashDecode,        # FD 基本块
  │            half, half, half, half,        # Q/KV/OUT/ORIGIN dtype
  │            true,  true,                   # PA, FD
  │            FIA_LAYOUT::BNSD, ..., FIA_LAYOUT::NZ)  # 布局
  │    → 实例化 op.Init(31 个 GM 指针, tiling_data, tiling, &tPipe); op.Process()
  └─ 运行期 Process():
       aiCoreIdx < usedCoreNum?
         ASCEND_IS_AIV → vectorService.InitBuffers/AllocEventID
         否则(AIC)     → matmulService.InitBuffers/AllocEventID
         FlashAttention()                    # 主流水：MM1→softmax→MM2 流水
         FreeEventID
       if constexpr (FLASH_DECODE) 且 fdFlag → FlashDecode()   # S2 切分规约
```

FIAType 模板（公共定义）把场景烙进类型系统：

```cpp
FIAType<Q_T, KV_T, OUT_T, ORIGIN_T, PAGE_ATTENTION, FLASH_DECODE,
        LAYOUT_T, ANTIQUANT_MODE, SHARED_PREFIX, KV_LAYOUT_T, SOFTMAX_WITH_BRC>
```

#### 4.4.3 源码精读

**TilingKey 宏表**。[ai_infra_fused_infer_attention_sink_tilingkey.h:L17-L46](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/ai_infra_fused_infer_attention_sink_tilingkey.h#L17-L46) 按注释分块罗列 MLA FP16 场景的全部 key：PA×{NZ,BNSD,BSH}×{MLA,FD}×{BNSD,BSH,TND} 再加 NonPA 系列；[L79-L140](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/ai_infra_fused_infer_attention_sink_tilingkey.h#L79-L140) 是 CV 1:1 核型的 `C1V1_` 变体。host 侧 `GenTilingKey` 与这张表必须逐位对齐（u2-l4 讲过「双侧硬编码一致」的纪律，这里放大到上百个 key）。

**host 侧 key 的拼装**。[fia_tiling_nonquant_sink.cpp:L133-L194](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/fia_tiling_nonquant_sink.cpp#L133-L194) 的 `GenTilingKey`：把 Q/KV/OUT dtype、Q 布局、KV 布局、PA、splitKv、softmaxBrcb、antiquantMode 各自映射成小整数，加权求和成 18 位 key（L184-L191）。`typeMap`（L158-L163）里 FP16=0、BF16=2、INT8=3、INT4=4——对照宏表里 BF16 key 的 `222220` 尾段即可理解编码含义。

**入口签名与混合核声明**。[ai_infra_fused_infer_attention_sink.cpp:L122-L168](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/ai_infra_fused_infer_attention_sink.cpp#L122-L168)：入口按 OpDef IO 顺序收 31 个 `__gm__` 指针 + workspace + tiling；L162-L168 建 `TPipe`、取用户 workspace、声明 `KERNEL_TYPE_MIX_AIC_1_2` 混合核任务。这印证 u2-l4 所说「参数布局是固定契约」。

**dtype 门控与 key 清单**。[ai_infra_fused_infer_attention_sink.cpp:L170-L318](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/ai_infra_fused_infer_attention_sink.cpp#L170-L318)：先 `#if (ORIG_DTYPE_QUERY == DT_FLOAT16) && ...` 按 dtype 粗分，再用上百行 `TILING_KEY_IS(...)` 声明本 dtype 下支持的 key 面（`TILING_KEY_IS` 宏由 CANN 编译框架提供，仓库内不定义；u2-l4 的 scatter 算子把它当运行期 `if` 用，这里作为编译期声明清单用）。

**编译期分发链**。[ai_infra_fused_infer_attention_sink.cpp:L320-L337](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/ai_infra_fused_infer_attention_sink.cpp#L320-L337)：`#if (TILING_KEY_VAR == QF16_KVF16_OUTF16_BNSD_KVNZ_PAGEDCACHE_MLA_TILING) || (== C1V1_...)` 命中后 `INVOKE_FIA_OP_GENERAL_IMPL(FiaKernelNonQuantMla, FiaBlockCubeNonQuantMla, FiaBlockVecNonQuantMla, FiaBlockVecFlashDecode, half, half, half, half, true, false, FIA_LAYOUT::BNSD, false, false, FIA_LAYOUT::NZ)`——一次展开同时指定 kernel 主类、三个基本块类和 9 个 FIAType 模板实参。`INVOKE_FIA_OP_GENERAL_IMPL` 宏本体在 [L72-L116](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/ai_infra_fused_infer_attention_sink.cpp#L72-L116)，GQA 专用版 `INVOKE_FIA_GQA_NO_QUANT_OP_IMPL` 在 [L39-L70](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/ai_infra_fused_infer_attention_sink.cpp#L39-L70)（单模板参数，直接用默认基本块）。TilingData 解包宏 [L118-L120](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/ai_infra_fused_infer_attention_sink.cpp#L118-L120) 封装 `GET_TILING_DATA_WITH_STRUCT`——就是 u2-l4 的 `GET_TILING_DATA` 的结构体版。

**FIAType 与布局枚举**。[fia_public_define.h:L40-L77](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/common/op_kernel/fia_public_define.h#L40-L77)：`FIA_LAYOUT` 枚举与 `FIAType` 模板——11 个模板参数（4 类型 + 2 布尔 + 2 布局 + 3 模式）把场景全部常量化。

**kernel 主类与三 service**。[fia_kernel_nonquant_sink.h:L39-L111](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_kernel_nonquant_sink.h#L39-L111)：`Init` 收全量参数，`Process` 驱动计算；L84-L106 从 FIAT 推导中间计算类型（如 `MM1_OUT_T` 在量化时是 int32、非量化是 float——中间累加高精度、按需降位）；L109-L111 声明 `matmulService`/`vectorService`/`fdService` 三个成员。[L118-L125](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_kernel_nonquant_sink.h#L118-L125) 定义 `SYNC_V0_C1_FLAG=6`、`SYNC_C1_V1_FLAG=7` 等跨核同步事件号——V（vector）与 C（cube）核按编号事件交替握手（u5-l2 专题展开）。

**运行期核分派**。[fia_kernel_nonquant_sink.h:L1276-L1299](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_kernel_nonquant_sink.h#L1276-L1299)：`Process()` 中 `aiCoreIdx < usedCoreNum` 内按 `ASCEND_IS_AIV` 分派 service 初始化，统一调 `FlashAttention()`；`if constexpr (FLASH_DECODE)` 是编译期裁剪——非 FD 模板的二进制里根本没有 FlashDecode 代码。

**ST 测试的组织**。[test_npu_fused_infer_attention_sink.py:L37-L50](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/tests/st/test_npu_fused_infer_attention_sink.py#L37-L50) 的 `supported_op_exec_sink_with` 用 float64 的 matmul+masked softmax 手写参考实现（L42 的 mask 正是「前 128 个 token 保留」的 sink 语义）；[L52-L57](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/tests/st/test_npu_fused_infer_attention_sink.py#L52-L57) 调 `torch.ops.custom.npu_fused_infer_attention_sink`；[L59-L89](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/tests/st/test_npu_fused_infer_attention_sink.py#L59-L89) 是完整 eager 用例：先调 `torch.ops.custom._npu_fused_infer_attention_sink_metadata` 生成 metadata（L73-L86），再算主算子，最后 `assertRtolEqual(..., 0.004, 0.004)` 对拍——u6-l2 将系统讲这套 ST 体系。

#### 4.4.4 代码实践

**实践目标**：从一个 TilingKey 宏反推它编译出的 kernel 是哪个模板实例（源码阅读型实践）。

**操作步骤**：

1. 在 [ai_infra_fused_infer_attention_sink_tilingkey.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/ai_infra_fused_infer_attention_sink_tilingkey.h#L24-L30) 任选一个 key，如 `QF16_KVF16_OUTF16_BNSD_KVBNSD_PAGEDCACHE_MLA_TILING = 105000000000200000`。
2. 在 kernel 入口 grep 该宏名，找到它所在的 `#if (TILING_KEY_VAR == ...)` 分支（约在 [ai_infra_fused_infer_attention_sink.cpp:L348](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/ai_infra_fused_infer_attention_sink.cpp#L348) 之后的 MLA BNSD 分支区）。
3. 抄下该分支 `INVOKE_FIA_OP_GENERAL_IMPL` 的实参表，标注每个实参含义（主类/cube 块/vec 块/FD 块/4 个 dtype/PA/FD/Q 布局/.../KV 布局）。
4. 反向验证：host 侧 `GenTilingKey` 用 `typeMap{FP16:0}`、`qLayoutMap{BNSD:0}`、`kvLayoutMap{BNSD:0}`、`paVal=2` 手工拼这个数，看能否得到 `105000000000200000`。
5. 有环境时可运行 `pytest ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/tests/st/test_npu_fused_infer_attention_sink.py -k eager` 观察对拍通过（待本地验证）。

**需要观察的现象 / 预期结果**：步骤 3 得到的实参表应与宏名逐段对应（half×4、PA=true、FD=false、Q 布局 BNSD、KV 布局 BNSD）；步骤 4 手工拼数与宏值一致，说明 host 编码与 device 宏表严格对齐。

#### 4.4.5 小练习与答案

**练习 1**：为什么用 `#if (TILING_KEY_VAR == ...)` 编译期分发，而不是运行期 `switch`？
**答案**：一是性能——分支在编译期消失，每个二进制变体里只有一份代码，寄存器分配、指令调度都按该场景最优；二是类型安全——模板实参（dtype、布局为编译期常量）只能在编译期绑定，`FIAType<half, ..., FIA_LAYOUT::NZ>` 无法作为运行期 switch 的目标。代价是二进制数量按 key 数量膨胀，由 CANN 编译框架和加载器管理。

**练习 2**：`if constexpr (FLASH_DECODE)` 与普通 `if (fdFlag)` 有什么区别？代码里为什么两个都有（[fia_kernel_nonquant_sink.h:L1294-L1298](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_kernel_nonquant_sink.h#L1294-L1298)）？
**答案**：`if constexpr` 在编译期裁剪——非 FD 模板实例里 FlashDecode 代码不存在，二进制更小；`fdFlag` 是运行期条件——即使编译进了 FD 能力（模板参数 FLASH_DECODE=true），是否真走 FD 还要由 tiling 下发的运行期信息（如序列长度、核负载）决定，同一份二进制可覆盖「长序列才启用 FD」的动态策略。

**练习 3**：`C1V1_` 前缀的 key 与普通 key 的 kernel 代码有什么关系？
**答案**：代码完全共享——入口的 `#if (TILING_KEY_VAR == 普通 key) || (TILING_KEY_VAR == C1V1_key)` 把两个 key 映射到同一个 `INVOKE_FIA_OP_GENERAL_IMPL` 实例。区别只在 TilingKey 编码中核型位不同，host 侧按 `cvRatio_`（AIV/AIC 核数比）选择发哪种 key，device 侧加载器据此挑选适配 CV 1:1 或 1:2 核型的二进制。

## 5. 综合实践

综合实践 = 本讲规格中的原始任务，分两问。

### 5.1 第一问：统计并归类 op_host 与 op_kernel 的文件

用两条命令统计（在仓库根目录执行）：

```bash
ls ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host | wc -l
ls ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel | wc -l
```

**预期结果**：op_host 22 个文件、op_kernel 12 个文件（与第 3 节源码地图的两张分类表一致）。归类时建议按「入口/校验/模板/基本块/公共设施」五档打标签，你会发现 op_host 的校验子系统（6 个文件约 2700 行）与模板子系统（4 个文件约 1600 行）占了半壁江山，而 op_kernel 中 9 个基本块/主类头文件每个都超 1000 行——host 侧是「宽而浅」的规则代码，device 侧是「窄而深」的计算代码。

### 5.2 第二问：新增一种 head_dim 组合要同步检查哪些文件

假设要支持 GQA 的新组合（qkHeadDim, ropeHeadDim, vHeadDim）=（256, 0, 256），按下表逐项核查（按数据流顺序）：

| # | 文件 | 检查点 |
| --- | --- | --- |
| 1 | [ai_infra_fused_infer_attention_sink_tiling_info_parser.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_tiling_info_parser.cpp) | 解析器是否能把 256 维正确抽到 `qkHeadDim`/`vHeadDim`（通常无需改，但 NZ 布局 D=D1*D0 的拆分逻辑要过一遍） |
| 2 | [ai_infra_fused_infer_attention_sink_tiling_check_feature.cpp:L524-L556](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_tiling_check_feature.cpp#L524-L556) | `CheckFeatureHeadDim` 的上限（512/64）与 NZ 16 对齐规则是否放行 256 |
| 3 | [ai_infra_fused_infer_attention_sink_tiling_v3.cpp:L182-L200](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_tiling_v3.cpp#L182-L200) | `CheckGqaDSupport` 的 D 组合白名单要**加一行**，否则 `RouteToFia` 直接拒绝、算子不可用 |
| 4 | [fia_tiling_nonquant_sink.cpp:L213-L235](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/fia_tiling_nonquant_sink.cpp#L213-L235) | `InitParams` 的特化模板判定条件：决定 256 走 `HIGH_PERFORMANCE_GQA` 还是 `GENERAL_GQA`（特化模板的基本块假设可能只对特定 D 成立，保守做法先只进泛化模板） |
| 5 | 同文件 L541、L553 附近 | `headDimAlign_` 参与 workspace 尺寸计算（`fdAccumOutSize`、`mm2ResSize`），确认 256 对齐后不会溢出 uint32 |
| 6 | [fia_kernel_nonquant_sink.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_kernel_nonquant_sink.h#L113-L126) | kernel 主类常量区：`N_BUFFER_M_BASIC_SIZE=256`、预取深度等与 D 相关的 UB 划分假设 |
| 7 | [fia_block_cube_nonquant_gqa_sink.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_cube_nonquant_gqa_sink.h) 与 [fia_block_vec_nonquant_sink.h](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_kernel/fia_block_vec_nonquant_sink.h) | cube/vector 基本块内对 headDim 的分块搬运、矩阵乘 M/N/K 假设（最重的改造点，若只是泛化模板可能天然支持） |
| 8 | [tests/ut/op_host/test_ai_infra_fused_infer_attention_sink_tiling.cpp](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/tests/ut/op_host/test_ai_infra_fused_infer_attention_sink_tiling.cpp) 与 [tests/st/test_npu_fused_infer_attention_sink.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/tests/st/test_npu_fused_infer_attention_sink.py) | 补一组 256 维用例：UT 验 tiling 字段，ST 验精度对拍 |

注意一个反直觉结论：**TilingKey 编码不含 head_dim 维度**（18 位里没有 D 的字段），所以新增 head_dim 组合**不需要**改 `ai_infra_fused_infer_attention_sink_tilingkey.h` 宏表和 kernel 入口的分发链——同一个 TilingKey 的二进制靠 TilingData 里的 `headDim` 字段（[tiling.h:L41](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_infer_attention_sink/op_host/ai_infra_fused_infer_attention_sink_tiling.h#L41)）在运行期携带真实 D。真正要动的是 host 白名单与校验（第 2/3/4 项），kernel 侧是否要改动取决于所选模板的基本块假设（第 6/7 项）。

**预期产出**：一张如上的核查清单 + 一段结论说明（哪些必须改、哪些视模板而定、哪些明确不用改）。本任务为纯源码分析，无需硬件即可完成；若要验证，可在改完后跑 `bash build.sh -n 'ai_infra_fused_infer_attention_sink'` 确认编译链路（待本地验证）。

## 6. 本讲小结

- **接口即场景平面**：FIA Sink 用 3 必选 + 27 可选输入 + 18 属性表达「全量/增量 × PA/非PA × GQA/MLA × 多布局 × 多 dtype」的推理场景空间；aclnn 层在 u2-l2 两段式骨架上新增了 INT4 视图换算、softmax_lse 占位符、GetMaxWorkspaceSize（aclgraph）与「非连续 KV 用 CreateView 零拷贝」四件套。
- **校验是独立子系统**：`tiling_check_*` 六文件按「单参数 → 存在性 → 特性交叉 → 多参数一致性」四级组织，纪律是 Check 只读 fiaInfo；报错在 tiling 阶段（host 侧）就带参数名拦截，不会拖到 kernel 才炸。
- **多分支 Tiling 是三级流水**：`RouteToFia` 按 dtype/rope/D 白名单快速路由 → V3 主流程 Parse/Check/DoTilingImpl → `FiaTilingRegistry` 按「SOC 版本 × 算子名 × priority」轮询 tiling 模板链（MLA=9 优先于 GQA=19，三位数优先级编码：百位量化、十位结构、个位特化度）；这与仓库公共 `TilingBaseClass` 七步框架平行，是注意力族自建的 `FiaTilingBase` 三虚函数体系。
- **TilingKey 是 18 位场景编码**：宏名即场景串（dtype_Q布局_KV布局_PA_调度_结构），host 的 `GenTilingKey` 与 kernel 的宏表双侧硬编码对齐；kernel 入口用 `TILING_KEY_IS` 声明支持面、`#if TILING_KEY_VAR ==` 编译期分发到具体 `INVOKE_FIA_OP_GENERAL_IMPL` 模板实例。
- **Kernel 是模板族 + 三 service**：`FIAType` 把 dtype/布局/PA/FD 烙成编译期常量；`FiaKernelNonQuant` 持 matmul（cube）/vector/flashdecode 三个 service，`Process()` 按 `ASCEND_IS_AIV` 分派、`if constexpr (FLASH_DECODE)` 编译期裁剪；跨核用编号事件（SYNC_V0_C1_FLAG 等）握手。
- **head_dim 不进 TilingKey**：新增 D 组合走 host 白名单 + 校验 + TilingData 运行期字段路线，不动 key 宏表——这是读代码读出来的关键结论，也是综合实践的答案。

## 7. 下一步学习建议

1. **下一讲 u4-l2（AICPU 算子特例：AttentionSink Metadata）**：本讲反复出现的 `metadata` 输入来自 `_npu_fused_infer_attention_sink_metadata` 前置算子——它不走 AICore 而走 AICPU，目录结构（`op_kernel_aicpu`、`op_graph`）与本讲完全不同，正好构成对照。
2. **横向扩展**：u4-l3 稀疏注意力家族会复用本讲的 `FiaTilingRegistry` 框架（`ai_infra_sparse_flash_attention_gqa` 也用 `REGISTER_TILING_TEMPLATE_FIA` 注册），读完本讲再看它会发现 tiling 骨架完全同构。
3. **纵深方向**：跨核同步原语与 AIV/AIC 流水细节在本讲只点到 `SYNC_*_FLAG`，u5-l2 将以 `kernel_common_sink.h` 与 `fia_block_vec_flashdecode_sink.h` 为标本专题展开；tiling 下沉（`DEVICE_IMPL_OP_OPTILING`）在 u5-l4。
4. **动手验证**：u6-l1 的 UT 框架可以在纯 CPU 上验证本讲的 tiling 逻辑（该算子自带 `tests/ut/op_host/test_ai_infra_fused_infer_attention_sink_tiling.cpp`），u6-l2 的 ST 体系则对应本讲 4.4.3 引用的精度对拍脚本。
