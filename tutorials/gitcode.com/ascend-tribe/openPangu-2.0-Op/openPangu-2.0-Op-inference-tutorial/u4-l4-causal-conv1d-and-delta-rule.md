# 因果卷积与 Delta Rule 递推：线性注意力配套算子

## 1. 本讲目标

本讲离开 softmax 注意力家族，进入**线性注意力（Gated Delta Net）推理流水线**的配套算子群。学完本讲，你应该能够：

1. 解释 `ai_infra_fused_causal_conv1d` 的 **conv_states 缓存机制**：它如何在增量推理中记住「上一次卷积看到的历史 token」，以及 `run_mode`、`block_size`、`conv_mode`、`inplace` 等参数如何支撑 prefill/decode/投机解码/APC 前缀缓存等多种场景。
2. 读懂 `ai_infra_chunk_gated_delta_rule_recurrence` 的 **chunk 化递推**：为什么线性注意力要分块、Host 侧 Tiling 只下发 8 个字段、Device 侧 Cube/Vector 两类核如何在一个 chunk 内流水协作完成状态递推。
3. 说明配套的下三角求逆算子 `ai_infra_lower_triangular_inverse` 在整条流水线中的位置。
4. 掌握「**通过 example 脚本验证线性注意力类算子**」的标准套路：NPU 算子与 CPU 参考实现对拍。

本讲是第 2 单元（三层结构、Tiling、Kernel）在真实算子上的第一次综合应用，也为 u5-l2（AIV/AIC 协同深入）铺垫素材。

## 2. 前置知识

### 2.1 为什么需要这些算子：Gated Delta Net 一句话版

softmax 注意力的代价是「每个新 token 要回看全部历史 KV」。线性注意力换了一种思路：把历史信息压缩进一个固定大小的**状态矩阵** \( S \)（可理解为「带遗忘门的记忆」），新 token 只需与 \( S \) 做一次矩阵乘，序列变长而计算量不变。openPangu 2.0 的推理结构中，这类模型的典型前处理是：

```text
输入 token
   │
   ▼
因果一维卷积 ai_infra_fused_causal_conv1d   ← 本讲 4.1（混合历史特征的短窗卷积，带状态缓存）
   │
   ▼
下三角求逆 ai_infra_lower_triangular_inverse ← 本讲 4.3（chunk 内解耦，产出 T 矩阵作用后的 K/V）
   │
   ▼
分块递推 ai_infra_chunk_gated_delta_rule_recurrence ← 本讲 4.2（沿 chunk 串行更新状态 S，产出中间结果）
   │
   ▼
后续注意力输出计算
```

### 2.2 增量推理与状态缓存

- **prefill / decode**：prefill 是「第一条 prompt 一次性算完」，序列长；decode 是「之后每次只来 1 个（或几个）新 token」。decode 阶段卷积窗口里大部分是**旧 token**，所以算子必须把上一次的末尾 token 存进缓存 `conv_states`，下次拼接续算——这就是「状态缓存」。
- **APC（Automatic Prefix Caching，前缀缓存）**：多条请求可能共享相同前缀。把前缀末尾的卷积状态按 block 缓存起来，新请求直接从命中的 block 处「热启动」，`cache_indices` 二维张量就是「每个请求对应哪些缓存槽位」的路由表。
- **MTP / 投机解码（PD 混部）**：一次猜测式生成多个候选 token 再验收，`num_accepted_tokens` 记录每条请求被接受的投机个数，卷积缓存要相应多留 m 个位置。

### 2.3 chunk（分块）

线性注意力的递推 \( S_{t+1} = f(S_t, \text{chunk}_t) \) 天然是**串行**的：块 t+1 依赖块 t 的状态。但每块内部的矩阵乘可以并行。因此工程上把序列切成大小为 `chunk_size` 的块（本仓库固定支持 32/64/128/256）：块间串行递推（本讲的 recurrence 算子聚焦这部分），块内大规模矩阵乘（提取到其他算子）。

### 2.4 需要回顾的前置概念

以下概念在第 2 单元已建立，本讲直接使用：TilingData「施工图」与 `GET_TILING_DATA` 解包（u2-l3/u2-l4）、`TILING_KEY_IS` 分支（u2-l4）、aclnn 两段式接口与 `CreateView`/`Contiguous`（u2-l2）、Kernel 类 `Init`/`Process` 两段式与 TPipe（u2-l4）、`torch.ops.custom` 调用链（u3-l4）。

## 3. 本讲源码地图

| 文件 | 层 | 作用 |
| --- | --- | --- |
| `ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/docs/npu_ai_infra_fused_causal_conv1d.md` | 文档 | 卷积算子的公式、参数表（含缓存读写的完整数学定义）与调用示例 |
| `ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_api/aclnn_ai_infra_fused_causal_conv1d.cpp` | op_api | aclnn 两段式接口：非连续输入处理 + L0 调用登记 |
| `ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_host/ai_infra_fused_causal_conv1d_tiling.h` | op_host | TilingData 字段定义、4 个 TilingKey 值、InfoParser/Tiling 类声明 |
| `ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_host/ai_infra_fused_causal_conv1d_tiling.cpp` | op_host | 参数校验、`GetTilingKey` 路由、切分计算 |
| `ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_kernel/ai_infra_fused_causal_conv1d.cpp` | op_kernel | kernel 入口：按 TilingKey 分派 3 个 kernel 类 |
| `ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_kernel/ai_infra_fused_causal_conv1d_common.h` | op_kernel | 三 个 kernel 类的公共基类（含 `FindBIdx` 二分） |
| `ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_kernel/ai_infra_fused_causal_conv1d_update.h` | op_kernel | decode/增量路径的 kernel 类 |
| `ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_kernel/ai_infra_fused_causal_conv1d_fn_cutbsd.h` | op_kernel | prefill 路径的 kernel 类之一（先切 Dim 再切序列） |
| `ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/docs/npu_chunk_gated_delta_rule_recurrence.md` | 文档 | 递推算子的公式、符号对照表与约束 |
| `ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_host/chunk_gated_delta_rule_recurrence_tiling.h` | op_host | Host 侧的形状/参数信息结构体（注意：TilingData 不在此文件） |
| `ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_host/chunk_gated_delta_rule_recurrence_tiling.cpp` | op_host | Tiling 主逻辑：校验、blockDim 与 enableMSplit 决策 |
| `ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_kernel/chunk_gated_delta_rule_recurrence_tilingdata.h` | op_kernel | TilingData 结构体（手写 POD，被 host 侧反向 include） |
| `ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_kernel/chunk_gated_delta_rule_recurrence_kernel.h` | op_kernel | Kernel 类：Cube/Vector 双流水实现（约 860 行） |
| `ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_kernel/ai_infra_chunk_gated_delta_rule_recurrence.cpp` | op_kernel | kernel 入口 |
| `ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/example/test_npu_chunk_gated_delta_rule_recurrence.py` | 示例 | NPU/CPU 对拍的完整使用示例（本讲主实践素材） |
| `ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/docs/npu_lower_triangular_inverse.md` | 文档 | 下三角求逆算子的接口说明 |

## 4. 核心概念与源码讲解

### 4.1 因果卷积缓存：ai_infra_fused_causal_conv1d

#### 4.1.1 概念说明

`ai_infra_fused_causal_conv1d` 对序列做**因果一维卷积**：输出第 i 个 token 只能看第 0..i 个 token（不能偷看未来），窗口宽度 K 固定为 3。它相较社区版 causal_conv1d 的增强点，全部围绕**增量推理的缓存**展开：

- `conv_states`：卷积状态缓存张量，形状 `[槽位数, stateLen, dim]`，**输入的同时也是输出**（原地更新）。`stateLen = K - 1 + m`：prefill 时 m=0（只存窗口需要的 2 个历史 token）；decode/PD 混部时 m∈[0,7]（多存投机候选）。
- `cache_indices`：一维表示未开 APC（每个 batch 一个缓存槽）；二维 `[batch, maxNumBlocks]` 表示开启 APC（每个 batch 一串块级缓存槽）。
- `num_computed_tokens` / `num_accepted_tokens`：区分「首次计算」（缓存清零起步）与「投机解码续算」（缓存多留 m 个 token）。
- `block_size`：APC 块大小（典型 128/256），决定前缀缓存填充的对齐粒度。
- `conv_mode`：0 为 Qwen3-Next 社区实现，1 为 Pangu V2 实现（含零填充重置等差异）。
- `inplace`：卷积结果是否原地写回 x（False 时经返回值输出新张量）。

一句话：**这个算子的本质不是卷积，而是「缓存读—拼—算—写」四件事与一次短窗卷积的融合**。

#### 4.1.2 核心流程

Host 侧（Tiling）先做一次「路由决策」，Device 侧按路由结果选择完全不同的 kernel 实现：

```text
Host 侧 GetTilingKey 路由：
  run_mode == 0（prefill，长序列）
      cuSeqLen < 核数 或 dim ≥ 4096  → TilingKey 0 (FN_CUTBSD)：核间先切 Dim 再切序列
      否则                            → TilingKey 1 (FN_CUTBS)：核间切序列、核内切 Dim
  run_mode == 1（decode/增量）
      x 为 3 维或 2 维                → TilingKey 2 (UPDATE_X_BSH)：按 batch 并行
Device 侧 kernel 入口：TILING_KEY_IS 对号入座，实例化对应 kernel 类
```

以增量路径（update）为例，单个 batch 的计算流程（与文档公式一一对应）：

1. **定位缓存行**：`readCacheLine = cacheIndices[batchId, initialStateIdx[batchId]]`（APC）/ `cacheIndices[batchId]`（非 APC）/ `batchId`（无）。
2. **缓存读取**：首次计算（`numComputedTokens == 0`）缓存视为全零；投机解码用 `offset = numAcceptedTokens - 1`；默认 `offset = C - (K-1)`，取出窗口前导。
3. **缓存拼接**：\[ \text{paddedInput}[i] = \begin{cases} \text{cachedState}[i], & i < \text{offset}+K-1 \\ x[i-(\text{offset}+K-1)], & \text{otherwise} \end{cases} \] 即「旧缓存 + 新 token」拼成连续序列。
4. **卷积**：\[ y[i, d] = \sum_{k=0}^{K-1} w[k, d] \cdot x'[i+k, d] \] 每个输出位置看连续 3 个输入。
5. **缓存更新**：把拼接序列的**末尾** M = min(C, Len) 个 token 写回缓存行末尾（滑窗语义：只留最近 stateLen 个）。
6. **APC 前缀填充（可选）**：按 `block_size` 对齐，把每个完整 block 末尾的 K-1 个 token 预写进该 batch 的后续缓存槽，下次即可从任意命中块热启动。
7. **零填充重置（conv_mode=1 时）**：投机未接受的位置输出置 0；**残差连接**（`residual_connection=1` 时）：\( y = x + y \)（实现上等价于把权重第 3 行加 1，见 4.1.3）。

#### 4.1.3 源码精读

**（a）aclnn 层：原地缓存张量必须 CreateView，只读输入走 Contiguous**

[aclnn_ai_infra_fused_causal_conv1d.cpp:L29-L53](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_api/aclnn_ai_infra_fused_causal_conv1d.cpp#L29-L53) 中，`CommonProcess` 先创建 executor，然后对两类张量区别对待：`x` 与 `convStatesRef` 用 `CreateView` 重建视图描述符（零拷贝保留 stride/offset），因为二者都是**要被原地写回的张量**；`weight` 及全部可选索引张量用 `l0op::Contiguous`（不连续则拷贝进 workspace）。这正是 u2-l2 总结的规则：「只读输入可拷贝，原地目标必须视图」的规模化应用——本算子有 12 个张量入参，处理手法完全模板化。

[aclnn_ai_infra_fused_causal_conv1d.cpp:L102-L131](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_api/aclnn_ai_infra_fused_causal_conv1d.cpp#L102-L131) 把全部参数打包交给 `l0op::AiInfraFusedCausalConv1d`（L0 封装）登记进 executor 下发列表，随后汇总 workspace 尺寸并 `ReleaseTo(executor)`——aclnn 第一段到此为止，第二段只是标准的 `CommonOpExecutorRun`（[L184-L189](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_api/aclnn_ai_infra_fused_causal_conv1d.cpp#L184-L189)）。

**（b）TilingData 与 4 个 TilingKey**

[ai_infra_fused_causal_conv1d_tiling.h:L95-L133](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_host/ai_infra_fused_causal_conv1d_tiling.h#L95-L133) 用 `BEGIN_TILING_DATA_DEF` 宏定义了 30 余个字段，可分四组读懂：

| 字段组 | 代表字段 | 含义 |
| --- | --- | --- |
| 核切分 | `coreDimAct`/`coreBatchAct`/`baseDim`/`tailDim`/`baseBatch`/`tailBatch` | dim 方向与 batch 方向各用几核、前核/尾核各处理多少（尾核兜余数） |
| 序列切分 | `blockFactor`/`blockFactorTail`/`blockCnt`/`cuSeqLength` | prefill 路径把拼接总长切成若干段分给核 |
| 非连续支持 | `xStride0/1`、`convStateStride0/1`、`stateLen` | kernel 按 stride 手工寻址，支持 dim 维 stride=1 的非连续张量 |
| 场景开关 | `blockSize`/`convMode`/`inplace`/`apcEnable`/`hasCacheIndices`/`hasNumAcceptedTokens`/`hasNumComputedTokens` | 把「哪些可选输入存在」烧进施工图，kernel 据此跳过整段逻辑 |

文件开头的 [L19-L22](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_host/ai_infra_fused_causal_conv1d_tiling.h#L19-L22) 定义了 4 个 TilingKey 常量：0=FN_CUTBSD、1=FN_CUTBS、2=UPDATE_X_BSH、3=UPDATE_X_TND。

**（c）TilingKey 路由：文档与代码的一个出入点**

[ai_infra_fused_causal_conv1d_tiling.cpp:L503-L518](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_host/ai_infra_fused_causal_conv1d_tiling.cpp#L503-L518) 的 `GetTilingKey()` 是本算子的「场景分拣台」：

- `run_mode==0` 且（`cuSeqLength < coreNum` 或 `dimSize >= 4096`）→ FN_CUTBSD（注释：先切 Dim，再切 cu_seq_len，见 [L522-L524](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_host/ai_infra_fused_causal_conv1d_tiling.cpp#L522-L524)）；
- `run_mode==0` 且序列够长、dim 不大 → FN_CUTBS（核间切 BS、核内切 Dim，见 [L560-L562](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_host/ai_infra_fused_causal_conv1d_tiling.cpp#L560-L562)）；
- `run_mode==1`（无论 x 是 3 维还是 2 维）→ UPDATE_X_BSH。

注意两点：其一，[docs/npu_ai_infra_fused_causal_conv1d.md:L353-L361](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/docs/npu_ai_infra_fused_causal_conv1d.md#L353-L361) 把 `run_mode` 标注为「历史遗留接口，暂不支持此字段」，但源码中它**仍然是最重要的路由开关**——再次印证 u1-l3 的纪律：文档可能滞后，以代码为准。其二，TilingKey 3（UPDATE_X_TND）已定义但当前 `GetTilingKey` 不会返回它，属于预留值。

最后 [L683-L723](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_host/ai_infra_fused_causal_conv1d_tiling.cpp#L683-L723) 的 `DoTiling()` 把全部字段 `set_*` 进 TilingData、`SaveToBuffer` 序列化，再 `SetTilingKey` + `SetBlockDim` 落账——与 u2-l3 讲的七步框架中「第 7 步 PostTiling」的职责一致。

**（d）kernel 入口：三个实现各管一段场景**

[ai_infra_fused_causal_conv1d.cpp:L28-L98](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_kernel/ai_infra_fused_causal_conv1d.cpp#L28-L98) 是标准入口：参数按 OpDef 声明顺序排列、末尾追加 `conv_states_out/output/workspace/tiling`；`GET_TILING_DATA_WITH_STRUCT` 解包施工图；随后三个 `TILING_KEY_IS` 分支分别实例化 `KernelAiInfraFusedCausalConv1dFnCutbsd` / `FnCutbs` / `Update` 模板类（按 `ORIG_DTYPE_X` 编译期实例化 half/bf16 两版），调用各自的 `Init` + `Process`。注意 `KERNEL_TASK_TYPE(TILING_KEY_FN_CUTBSD, KERNEL_TYPE_MIX_AIV_1_0)`（[L39](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_kernel/ai_infra_fused_causal_conv1d.cpp#L39)）标注该 kernel 纯向量核执行，与 delta rule 算子（纯 Cube+Vec 混合）形成对照。

**（e）公共基类：变长序列的 batch 定位**

[ai_infra_fused_causal_conv1d_common.h:L41-L85](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_kernel/ai_infra_fused_causal_conv1d_common.h#L41-L85) 是三个 kernel 类的公共基类，存放 GlobalTensor 句柄与 token/batch 游标。其中 [L47-L63](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_kernel/ai_infra_fused_causal_conv1d_common.h#L47-L63) 的 `FindBIdx` 值得一读：prefill 路径把所有变长序列拼成一条 `[cuSeqLen, dim]` 长条，某核分到的 token 起点可能落在任意 batch 中间，`FindBIdx` 对 `queryStartLoc`（各序列起始偏移，升序）做**二分查找**，返回该 token 所属的 batch 序号——这是变长推理算子的通用小技巧。

**（f）增量路径 Process：滑窗缓存的生命周期**

[ai_infra_fused_causal_conv1d_update.h:L103-L145](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_kernel/ai_infra_fused_causal_conv1d_update.h#L103-L145) 的 `Process()` 是 4.1.2 流程图的代码化：外层 `for idxB < curBatch_` 遍历本核负责的 batch（`readCacheLine == padSlotId` 则整段跳过——pad batch 不参与计算），内层逐 token 执行 `CopyInX → CopyOutXRunCache（写运行缓存）→ CopyOutXPreCache（写 APC 前缀缓存）→ ComputeAndUpdate（滚动更新 y0/y1 两个累加器）→ CopyOutY`。缓存写入与卷积计算在同一循环内交替完成，这就是「融合」的落点。

两个实现细节：

- [L58-L63](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_kernel/ai_infra_fused_causal_conv1d_update.h#L58-L63)：尾核判断 `curDim_ = (blockIdx+1) % coreDimAct == 0 ? tailDim : baseDim`；增量路径里 `seqLen_ = m + 1`（三维输入时每步固定处理 m+1 个 token）、`stateLen_ = windowSize - 1 + m = 2 + m`——正是文档「stateLen == K-1+m」的出处。
- [L148-L169](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_kernel/ai_infra_fused_causal_conv1d_update.h#L148-L169) `CopyInWeight`：权重先搬入 UB 并 `Cast` 成 float32，随后若 `residual == 1` 则执行 `Adds(weight[2*curDim], weight[2*curDim], 1.0f)`——**把残差连接编译进权重**（\( y = x + w_0 x_{i} + w_1 x_{i+1} + w_2 x_{i+2} = w_0 x_{i} + w_1 x_{i+1} + (w_2{+}1) x_{i+2} \)，x 本身就是最后一项输入），一个向量加法替代整段残差计算，很巧的算子内优化。

prefill 路径 `FnCutbsd` 的骨架类似但切分维度不同：[ai_infra_fused_causal_conv1d_fn_cutbsd.h:L54-L72](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_kernel/ai_infra_fused_causal_conv1d_fn_cutbsd.h#L54-L72) 中，核号被拆成 `baseDimIdx`（管哪段 dim）与 `baseblockFactorIdx`（管哪段 token），`tokenIdx = baseblockFactorIdx * blockFactor` 后用 `FindBIdx` 定位 batch，再从 `queryStartLoc` 读出该 batch 的起止——「先切 Dim 再切序列」的具象化。

#### 4.1.4 代码实践

**实践目标**：不运行代码，仅凭源码推演「同一份输入在不同参数下走哪条 kernel 路径」，训练 TilingKey 路由的读图能力。

**操作步骤**：

1. 打开 [docs/npu_ai_infra_fused_causal_conv1d.md:L475-L545](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/docs/npu_ai_infra_fused_causal_conv1d.md#L475-L545) 的调用示例，记下默认参数：`dim=512`、`cu_seq_len=53`、`run_mode=0`。
2. 对照 `GetTilingKey()` 的判断条件，回答：示例参数（假设核数 coreNum=50，可从 tiling.h 的 `MAX_DIM_CUTBSD = 4096` 反推边界）会命中哪个 TilingKey？实例化哪个 kernel 类？
3. 把 `run_mode` 改成 1、`x` 保持二维 `[53, 512]`，再回答一遍。
4. 若有昇腾环境（A2/A3）：把示例脚本中 `run_mode` 分别设为 0 和 1 各跑一次，在算子日志中观察 `TilingKey`（`DoTiling` 里有 `OP_LOGD` 输出 coreDimAct 等字段，见 tiling.cpp [L725-L729](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/op_host/ai_infra_fused_causal_conv1d_tiling.cpp#L725-L729)），验证你的推演。

**需要观察的现象**：run_mode=0 与 run_mode=1 走完全不同的 kernel 类；dim 从 512 加到 4096 以上时 prefill 内部又从 CUTBS 切换到 CUTBSD。

**预期结果**：步骤 2 命中 TilingKey 1（FN_CUTBS：cuSeqLen=53 ≥ 50 且 dim=512 < 4096），实例化 `KernelAiInfraFusedCausalConv1dFnCutbs`；步骤 3 命中 TilingKey 2（UPDATE_X_BSH），实例化 `KernelAiInfraFusedCausalConv1dUpdate`。第 4 步的日志验证**待本地验证**（需要真实 NPU 环境）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `conv_states` 在 aclnn 层用 `CreateView` 而不是 `Contiguous`？

答案：`conv_states` 是原地更新的张量——kernel 计算完要把新状态写回调用者传入的同一块内存。若用 `Contiguous` 拷贝一份连续副本，计算结果会写进副本，调用者的缓存原地不变，增量推理的「记忆」就断了。`CreateView` 零拷贝地保留原 storage 的 stride/offset，写视图即写原张量。

**练习 2**：`stateLen` 为什么是 `K - 1 + m` 而不是 `K + m`？

答案：窗口宽 K=3 的因果卷积，算第 i 个输出只需要 i、i+1、i+2 三个输入，其中前 2 个（K-1 个）可能来自上一轮的历史——所以缓存只需保存 K-1 个 token；m 是投机解码场景额外多接受的候选 token 数，它们也是「已见过的历史」，一并入缓存。

**练习 3**：TilingData 里为什么要放 `hasCacheIndices` 这类「输入是否存在」的 bool 型字段？

答案：TilingData 是 Host 到 Device 的唯一信息通道。kernel 是无状态的，不知道调用方传没传可选张量；Host 在 Tiling 阶段把「哪些可选输入存在、APC 是否开启、conv_mode 是多少」等场景信息烧进施工图，kernel 读一个字段就能跳过整段逻辑（如 update.h 中 `if (apcEnable_)` 才绑定 APC 相关 GlobalTensor），避免在 Device 侧做空指针判断。

### 4.2 分块递推：ai_infra_chunk_gated_delta_rule_recurrence

#### 4.2.1 概念说明

Gated Delta Net 的 prefill 要沿整个序列递推状态矩阵 \( S \)。直接逐 token 递推无法利用矩阵乘引擎，所以拆成 chunk：**块内的大矩阵乘没有时序依赖，可提取出去**；本算子只聚焦必须串行的核心递推。文档给出的「提取后」三步公式（[npu_chunk_gated_delta_rule_recurrence.md:L33-L41](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/docs/npu_chunk_gated_delta_rule_recurrence.md#L33-L41)）：

\[ O^{\text{inter}}_{t} = Q_{g,t}\, S_{t}^{\top} \]

\[ V^{\text{pseudo}}_{t} = \tilde{V}_{t} - K_{c,t}\, S_{t}^{\top} \]

\[ S_{t+1} = \gamma_{t} \odot S_{t} + (V^{\text{pseudo}}_{t})^{\top} K_{g,t} \]

输入张量与符号的对照（摘自文档符号表，写代码时按此命名对齐）：

| 输入张量 | 形状 | 数学含义 |
| --- | --- | --- |
| `initial_state` | `[b, hv, dv, dk]` | 状态 \( S \)，**入参原地修改**，递推结束即最终状态 |
| `kgexp` | `[hv, n_chunks, chunk_size, dk]` | 吸收衰减系数后的 Key（\( K_g \)） |
| `value` | `[hv, n_chunks, chunk_size, dv]` | 已乘下三角逆矩阵的 Value（\( \tilde{V} \)） |
| `k_cumdecay` | `[hv, n_chunks, chunk_size, dk]` | 乘了下三角逆与累积衰减的 Key（\( K_c \)） |
| `qgexp` | `[hv, n_chunks, chunk_size, dk]` | 吸收衰减系数后的 Query（\( Q_g \)） |
| `gexp` | `[hv, n_chunks, chunk_size, 1]` | 累积衰减系数 \( \gamma \)（每 chunk 只用最后一个值） |
| `actual_seqlens` | `[b]` | 每个 batch 的序列长度（chunk_size 的整数倍） |

输出两个张量：`attn_inter_out`（注意力的中间结果）与 `v_new_out`（pseudo value），供下游算子拼出最终输出。注意 K/V 类输入按 batch 顺序**紧凑排布**成块——`n_chunks` 维是所有 batch 的块拼在一起的，各 batch 的边界由 `actual_seqlens` 描述。

这个算子展示了两个「不同于教科书」的工程点：TilingData 不用宏而是手写 POD 结构体；一个 kernel 同时用 Cube 核（矩阵乘）与 Vector 核（逐元素）混跑。

#### 4.2.2 核心流程

Host 侧 Tiling 异常简单（本算子的切分几乎只由形状决定）：

```text
1. 解析形状：b/hv/dv/dk 取自 initial_state；n_chunks/chunk_size 取自 kgexp
2. 校验：dk=dv=128、chunk_size∈{32,64,128,256}、b≤65536、hv≤128、全部输入 float32
3. blockDim：
   batch*head ≥ AIC 核数   → 不切 M，blockDim = AIC 核数（每核若干 batch*head）
   否则                    → 在 M 轴（chunk_size 维）按 2 的幂切分补足并行度，
                             enableMSplit=1，blockDim = hv * mSplitNum
4. TilingKey 恒为 0（GenTilingKey 目前是占位实现）
```

Device 侧是本算子的精华——**Cube 与 Vector 两类核在一个 chunk 内流水协作**（入口标注 `KERNEL_TYPE_MIX_AIC_1_1`，Cube:Vector = 1:1）。对每个 (batch, head)，沿 chunk 串行执行：

```text
对 chunk t = start .. end-1:                # 串行：依赖上一块的 S
  Cube 核：
    C0  载入 k_cumdecay(→L1A)、S(→L1B)        ──┐ 向量核同时
    C1  vNewOut ← k_cumdecay @ Sᵀ  (v_prime)    │ V0  S ← S * γ      (Muls)
    C2  attnInter ← qgexp @ Sᵀ                  │ V1  vNewOut ← value − vNewOut (Sub)
    C3  S += vNewOutᵀ @ kgexp (AtomicAdd)     ──┘ （C3 要等 V1 把 v_new 算完）
```

四步 Cube 与两步 Vector 的对应关系恰好覆盖三条公式：C2 是 \( O^{\text{inter}} \)；C1+V1 合成 \( V^{\text{pseudo}} \)（C1 先把减项写进输出位置，V1 原地做减法）；V0+C3 合成 \( S_{t+1} \)（V0 先乘衰减，C3 用 Cube 的 AtomicAdd 把外积累加回 GM 的 S）。Cube 核内部还在 L1/L0A/L0B/L0C 四级存储间手工搬运矩阵（`LoadData`/`Mmad`/`Fixpipe`），并用双缓冲（`iter & 1` 切换两份 buffer）让搬运与计算重叠——这套手工 Cube 流水是 u5-l2 的主题，本讲只需看懂分工。

#### 4.2.3 源码精读

**（a）TilingData：手写 POD 的「另一种契约写法」**

[chunk_gated_delta_rule_recurrence_tilingdata.h:L21-L30](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_kernel/chunk_gated_delta_rule_recurrence_tilingdata.h#L21-L30) 没有用 u2-l3 讲的 `BEGIN_TILING_DATA_DEF` 宏族，而是直接手写了一个 8 字段的普通结构体：`batchNum/headNum/kDim/vDim/chunkNum/chunkSize/enableMSplit/mSplitNum`。Host 侧 tiling 实现通过 [chunk_gated_delta_rule_recurrence_tiling.cpp:L23](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_host/chunk_gated_delta_rule_recurrence_tiling.cpp#L23) `#include "../op_kernel/chunk_gated_delta_rule_recurrence_tilingdata.h"` **反向包含 kernel 目录的头文件**，保证两端看到同一份 POD 定义——这就是「TilingData 是 host/device 序列化契约」的另一种实现方式：不靠宏生成，靠直接共享结构体。kernel 入口则用 `REGISTER_TILING_DEFAULT(TilingData)` + `GET_TILING_DATA_WITH_STRUCT` 解包（见 (d)）。

对照来看，op_host 下同名的 [chunk_gated_delta_rule_recurrence_tiling.h:L32-L64](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_host/chunk_gated_delta_rule_recurrence_tiling.h#L32-L64) 只是 **Host 侧的工作变量**（`ShapeInfo` 存解析出的形状、`ParaInfo` 存 TilingContext 里取出的各张量 desc/shape 指针），不下发设备——与 conv1d 把工作变量和下发字段混在一个宏结构体里的做法不同。

**（b）enableMSplit 决策：并行度不足时切 M 轴**

[chunk_gated_delta_rule_recurrence_tiling.cpp:L246-L291](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_host/chunk_gated_delta_rule_recurrence_tiling.cpp#L246-L291) 的 `SetTilingData()` 先填形状字段，再做切分决策：

- `batchNum * headNum >= aicNum`：任务数多于核数，直接「每核认领若干 (batch, head)」，`enableMSplit=0`；
- 否则核有富余：从 `mSplitMaxNum = min(chunkSize, vDim)/16` 开始按 `i >>= 1` 向下找最大的 2 的幂，使 `headNum * i <= aicNum`，把每个 (batch, head) 任务的 M 维（chunk_size 方向）再切给多核；
- 还有一个利用率判断（[L278](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_host/chunk_gated_delta_rule_recurrence_tiling.cpp#L278)）：若 `batchHead > 2 * headNum * mSplitNum` 说明不切 M 核也够忙，退回不切。

[chunk_gated_delta_rule_recurrence_tiling.cpp:L263-L286](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_host/chunk_gated_delta_rule_recurrence_tiling.cpp#L263-L286) 里的 BASEM=16 是 Cube 单元 Mmad 的最小分型高度（16×32B fragment，kernel.h [L122-L125](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_kernel/chunk_gated_delta_rule_recurrence_kernel.h#L122-L125) 的 `fractionH = 16` 与之呼应），M 轴切分数必须是 16 的整数倍才有意义。

另见 [L231-L243](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_host/chunk_gated_delta_rule_recurrence_tiling.cpp#L231-L243)：workspace 只申请系统 workspace（「kernel 只有原地修改」，无需用户 workspace）；[L384-L390](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_host/chunk_gated_delta_rule_recurrence_tiling.cpp#L384-L390) 的 `GenTilingKey()` 是占位实现（恒 0），最后由 [L425-L427](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_host/chunk_gated_delta_rule_recurrence_tiling.cpp#L425-L427) 的 `IMPL_OP_OPTILING` 注册——它没有继承公共 `TilingBaseClass`，而是自写 `TilingChunkGatedDeltaRuleRecurrence` 入口，属于第 2 单元框架之外的「自建 tiling」写法。

**（c）Kernel：Cube/Vector 流水与原地状态更新**

[chunk_gated_delta_rule_recurrence_kernel.h:L29-L43](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_kernel/chunk_gated_delta_rule_recurrence_kernel.h#L29-L43) 声明 Kernel 类。成员分三组：GM 句柄（L50-L58）；Cube 专用 buffer——L1/L0A/L0B/L0C 四级（[L60-L87](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_kernel/chunk_gated_delta_rule_recurrence_kernel.h#L60-L87)，注释标明 256KB/64KB/128KB 等容量）；Vector 专用 UB buffer（[L89-L101](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_kernel/chunk_gated_delta_rule_recurrence_kernel.h#L89-L101)）。

[Init（L295-L326）](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_kernel/chunk_gated_delta_rule_recurrence_kernel.h#L295-L326) 绑定全部 GM 输入输出后，用 `if ASCEND_IS_AIV` 把同一份 Kernel 代码按核类型分流：向量核走 `VectorBufferInit`（UB + MTE2/MTE3/V 事件对），立方核走 `CubeBufferInit`（L1/L0 + MTE1/M/FIX 事件对）——**一份源码、两类核各取所需**，这是混合核 kernel 的常见写法。

任务分发与递推主循环在 [SingleBatchProcess（L765-L802）](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_kernel/chunk_gated_delta_rule_recurrence_kernel.h#L765-L802)：

- [L769-L779](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_kernel/chunk_gated_delta_rule_recurrence_kernel.h#L769-L779) 先判断「这个 (batch, head) 归不归我管」：不切 M 时 `(batch*headNum+head) % blockNum == blockIdx`；切 M 时按 `head + mSplit` 二维分派。
- [L789-L801](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_kernel/chunk_gated_delta_rule_recurrence_kernel.h#L789-L801) 对该序列的 chunk 逐个串行处理：向量核读 `gexp` 的**最后一个值**（`gOffset = (chunkOffset+1)*chunkSize - 1`，γ 每 chunk 只需标量一个）后执行 `SingleChunkVecProcess`；立方核执行 `SingleChunkCubeProcess`。

[RunProcess（L804-L831）](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_kernel/chunk_gated_delta_rule_recurrence_kernel.h#L804-L831) 负责「把紧凑排布的块展开回各 batch」：从 GM 读 `actualSeqlensGm_.GetValue(curBatch)` 得到该 batch 的 token 数，`assert + Trap` 三连检查（非负、被 chunk_size 整除、累计不超过 chunkNum），然后 `seqEndChunk = seqStartChunk + seqLen/chunkSize` 推进游标——**kernel 自己在 Device 侧做前缀和**。这也澄清了 `actual_seqlens` 的实际语义：每个元素是「该 batch 的长度」；而 [docs:L71-L84](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/docs/npu_chunk_gated_delta_rule_recurrence.md#L71-L84) 把它写成「[b+1] 的序列长度累加和、首值为 0」——文档与实现存在出入，以代码（及 4.3 的 example 用法）为准。

单 chunk 内的 Cube↔Vector 握手在 [SingleChunkCubeProcess（L661-L676）](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_kernel/chunk_gated_delta_rule_recurrence_kernel.h#L661-L676) 与 [SingleChunkVecProcess（L725-L763）](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_kernel/chunk_gated_delta_rule_recurrence_kernel.h#L725-L763)：`CrossCoreSetFlag/CrossCoreWaitFlag`（`SYNC_MODE_CUBE_VEC=2`，见 [L103-L104](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_kernel/chunk_gated_delta_rule_recurrence_kernel.h#L103-L104)）按 C0→V0、C1→V1、V1→C3 的顺序传递接力棒，例如 C3 前必须 `WaitFlag(SYNC_FLAG_V1C3)` 等 V1 把真正的 v_new 减出来。V0 的计算体（[L678-L699](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_kernel/chunk_gated_delta_rule_recurrence_kernel.h#L678-L699)）是教科书式的 CopyIn→Muls→CopyOut 三段双缓冲循环，把 S 逐 1024 元素批乘 γ 后**写回 GM 原地**——所以初始状态无需单独的输出张量。

**（d）kernel 入口**

[ai_infra_chunk_gated_delta_rule_recurrence.cpp:L20-L43](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_kernel/ai_infra_chunk_gated_delta_rule_recurrence.cpp#L20-L43) 的入口只有 20 余行：参数表里 `initialStateRef`（L29）是 OpDef 中 `initial_state_out` 输出的占位，但 `op.Init(...)` 并未使用它——**状态直接原地写回 initialState**。`KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_MIX_AIC_1_1)`（L37）声明 Cube:Vector = 1:1 的混合核任务；没有 `TILING_KEY_IS` 分支（TilingKey 恒 0），`enableMSplit` 改由 TilingData 字段在运行期分派（`Process()` 里 `if (params_->enableMSplit)` 走模板参数 `RunProcess<true/false>`，[L833-L839](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_kernel/chunk_gated_delta_rule_recurrence_kernel.h#L833-L839)）——与 conv1d 的「TilingKey 选类」形成对照：**编译期分支 vs 运行期字段分派**两种手段（u2-l4 讲 MHC 时已见过后者）。

#### 4.2.4 代码实践

**实践目标**：用纸笔推演 enableMSplit 决策，把 tiling 源码变成可复算的公式。

**操作步骤**：

1. 假设目标芯片 AIC 核数 `aicNum = 25`（AIC:AIV=1:1 的典型 A2 规格之一，具体核数待本地用 `PlatformAscendC::GetCoreNumAic()` 确认）。
2. 取 example 中的 eager01 用例参数：`b=3, hv=4, chunk_size=256, dv=dk=128`。计算 `batchHead = 3*4 = 12`，判断 `12 >= 25` 是否成立。
3. 若不成立，按源码计算：`mSplitMaxNum = min(256, 128)/16 = 8`；从 i=8 开始 `i >>= 1` 找满足 `hv * i <= aicNum`（`4*i <= 25` → i=4…不满足 8，i=4 满足？4*4=16 ≤ 25 成立，但先试 i=8：32 > 25 不行；i=4：16 ≤ 25 可以）→ `mSplitNum = 4`。
4. 再核对利用率判断：`batchHead(12) > 2 * headNum * mSplitNum(2*4*4=32)`？12 > 32 不成立 → `enableMSplit=1`，`blockDim = hv * mSplitNum = 16`。
5. 阅读kernel.h 的 `GetMRange<true>`（[L569-L579](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/op_kernel/chunk_gated_delta_rule_recurrence_kernel.h#L569-L579)），验证：切 M 时每个核处理 `mLength/mSplitNum = 256/4 = 64` 行，与 BASEM=16 对齐。

**需要观察的现象**：同一份输入，`b*hv` 越小越可能触发 M 轴切分；`chunk_size` 越小可切的份数越少（上限 `min(chunkSize, vDim)/16`）。

**预期结果**：步骤 2~4 的推演如上（enableMSplit=1、mSplitNum=4、blockDim=16）。真实核数下的结果**待本地验证**：可在有环境时给 `SetTilingData` 临时加日志打印（不改仓库源码，复制到自己的实验目录中修改），或运行 UT（`tests/ut/op_host/test_chunk_gated_delta_rule_recurrence_tiling.cpp` 存在，可用 u6-l1 将讲的 faker 框架在纯 CPU 上跑）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `gexp` 形状是 `[hv, n_chunks, chunk_size, 1]`，kernel 却只读每个 chunk 的最后一个值？

答案：状态衰减 \( S_{t+1} = \gamma_t S_t + \dots \) 中，γ 只在「块结束、状态传递给下一块」时作用一次，等效的衰减系数取块内最后一个位置的累积衰减即可；chunk 内部的逐 token 衰减已经被上游算子吸收进 `qgexp/k_cumdecay`（「吸收衰减系数后」的含义）等输入里。所以 kernel 用 `gOffset = (chunkOffset+1)*chunkSize - 1` 只取末值。

**练习 2**：V1 步骤 `vNewOut ← value − vNewOut` 是原地读改写输出张量，这依赖什么同步保证正确性？

答案：依赖 C1→V1 的跨核同步 `CrossCoreWaitFlag(SYNC_MODE_CUBE_VEC, ..., SYNC_FLAG_C1V1)`：V1 必须等 Cube 核把减项 `k_cumdecay @ Sᵀ` 完整写入 vNewOut（GM）之后才能读它做 Sub 并写回；同理 C3 要等 `SYNC_FLAG_V1C3` 确认 V1 已产出真正的 v_new 才能做状态累加。少了任何一环，读到的都是半成品。

**练习 3**：本算子的 TilingData 为什么可以只有 8 个字段，而 conv1d 需要 30 多个？

答案：递推算子的并行度完全由「batch × head（× 可选的 M 切分）」决定，且 kernel 内没有 UB 分块循环（矩阵搬运的块大小由 L1/L0 容量常量在编译期写死），所以 Host 只需告知形状与切分开关；conv1d 要在核内按 `baseDim/blockFactor` 分块搬数据，还要携带 stride、场景开关等，字段自然多。TilingData 字段数 = Device 侧「不可自行获得的信息量」。

### 4.3 使用示例与配套算子：example 对拍与下三角求逆

#### 4.3.1 概念说明

线性注意力类算子输入张量多（递推算子 7 入 3 出）、语义抽象（「吸收衰减系数的 Key」），光读接口文档很难确信理解正确。仓库给出的验证手段是 **example 对拍脚本**：同一份输入，CPU 上用纯 PyTorch 写参考实现，NPU 上调自定义算子，逐输出比较。这是接管一个新算子时最有效的学习方法。

配套算子 `ai_infra_lower_triangular_inverse` 负责下三角矩阵 \( T = I - L \)（I 单位阵，L 严格下三角）的求逆。它在流水线中的位置由递推算子文档的公式反推：\[ T_{t} = \left[I+\mathrm{strictLower}\left(\mathrm{diag}(\beta_{t})K_{t}K_{t}^{\top}\right)\right]^{-1}\mathrm{diag}(\beta_{t}) \]，而递推算子的输入 `value`/`k_cumdecay` 的文档描述正是「**乘上下三角逆矩阵**的 Value/Key 矩阵」——即 \(\tilde{V}=TV\)、\(\tilde{K}=TK\) 这步块内解耦先由 lower_triangular_inverse（及相邻算子）完成，递推算子消费其结果。两份文档的参数描述互相印证了这条流水线关系。

#### 4.3.2 核心流程

example 脚本的标准结构（也是仓库 ST 测试的同款套路）：

```text
1. imports：torch / torch_npu / torchair / omni_custom_ops（import 副作用挂载算子，u1-l4）
2. cpu_xxx()：纯 PyTorch 参考实现（逐 batch、逐 chunk 的朴素循环）
3. compare()：np.isclose 逐元素比较，一致率 ≥ 99% 判过（容差 rtol=0.005, atol=1e-4）
4. generic_test_func()：构造随机输入 → NPU 调用 → CPU 参考 → 三路断言
5. TestCase：同一 generic_test_func 换参数/换调用方式（eager / torch.compile 图模式）
```

#### 4.3.3 源码精读

**（a）CPU 参考实现：三行循环对应三条公式**

[test_npu_chunk_gated_delta_rule_recurrence.py:L23-L55](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/example/test_npu_chunk_gated_delta_rule_recurrence.py#L23-L55) 的 `cpu_chunk_gated_delta_rule_recurrence` 是理解算子语义的最佳注释——核心循环（L47-L53）：

```python
v_prime = (k_cumdecay[:, i]) @ last_recurrent_state.transpose(-1, -2)   # C1
attn_inter_out[:, i] = qgexp[:, i] @ last_recurrent_state.transpose(-1,-2)  # C2
v_new_out[:, i] = v_i - v_prime                                          # V1（C1 结果暂存后相减）
last_recurrent_state *= gexp[:, i, -1, :, None]                          # V0（取末值）
last_recurrent_state += v_new_out[:, i].transpose(-1, -2) @ kgexp[:, i]  # C3
```

五行 Python 与 4.2.2 的 C0~C3/V0~V1 一一对应，也再次印证 `actual_seqlens` 在这里是「每 batch 长度」（L45：`batch_end = batch_begin + actual_seqlens[batch_id] // chunk_size`，游标自行累加）。

**（b）数据构造与双模式调用**

[L66-L99](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/example/test_npu_chunk_gated_delta_rule_recurrence.py#L66-L99) 的 `generic_test_func` 展示了构造要点：`actual_seqlens` 先按「每 batch 的 chunk 数」随机（L70），求和得 `n_chunks`（L71），再乘 `chunk_size` 变成长度（L72）——张量按 `[hv, n_chunks, ...]` 紧凑排布；`initial_state` 先 `clone().detach()` 留底（L75），因为算子会原地改它，对拍要拿原始值喂 CPU 参考；三个输出各自过 `compare`（L94-L99）。

[L102-L135](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/example/test_npu_chunk_gated_delta_rule_recurrence.py#L102-L135) 的 TestCase 覆盖两种执行方式：`test_..._eager01/02` 直接 `torch.ops.custom.npu_chunk_gated_delta_rule_recurrence(*args)`（多输出按元组解包）；`test_..._graph` 用 `torch.compile(Net().npu(), backend=npu_backend, fullgraph=True)` 走 torchair 图模式，并设置 `keep_inference_input_mutations = True`（L120）——因为算子原地修改 `initial_state`，图编译器必须保留该 mutation 语义，这正是 u3-l3 讲的 converter/图模式适配在「带副作用的算子」上的关键配置。

**（c）lower_triangular_inverse：接口与约束**

[npu_lower_triangular_inverse.md:L8-L31](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/docs/npu_lower_triangular_inverse.md#L8-L31) 给出该算子的三个要点：输入 `x` 形状 `[a, b, c, m, n]`、float32，且必须满足 \( T = I - L \) 的结构（对角线恒为 1）；\( m = n \) 且取值固定为 32/64/128/256——**与递推算子的 chunk_size 合法集合完全一致**（chunk 内的 \( I+\mathrm{strictLower}(\cdot) \) 正是这种固定尺寸的下三角结构）；仅支持推理 Prefill 场景。调用示例（[L34-L68](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_lower_triangular_inverse/docs/npu_lower_triangular_inverse.md#L34-L68)）演示了标准构造法：随机矩阵 `np.tril(x, k=-1)` 取严格下三角，再 `eye - x` 拼出 \( I - L \)，验证用 \( T @ T^{-1} \approx I \)。它在 u3-l4 端到端复盘时已作为贯穿案例精读，本讲只关注其流水线角色。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：把「读 example → 整理张量表 → 仿写新算子调用」走一遍完整流程。

**操作步骤**：

1. **整理张量表**。精读 [test_npu_chunk_gated_delta_rule_recurrence.py:L23-L31](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/example/test_npu_chunk_gated_delta_rule_recurrence.py#L23-L31) 与 [L74-L81](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_chunk_gated_delta_rule_recurrence/example/test_npu_chunk_gated_delta_rule_recurrence.py#L74-L81)，在笔记里画一张 7 入 3 出的表格（名称、形状、dtype、数学含义、哪一步产生/消费）。用 4.2.1 的表格核对答案。
2. **仿写 conv1d 增量调用伪代码**。参照 conv1d 文档示例（[docs:L475-L545](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_fused_causal_conv1d/docs/npu_ai_infra_fused_causal_conv1d.md#L475-L545)），写一段「prefill 一次 + decode 两次」的最小脚本骨架（下方示例代码为本人编写的练习骨架，非仓库原有代码）：

```python
# 示例代码：练习骨架（非仓库原有）
import torch, torch_npu, omni_custom_ops

dim, width, batch = 512, 3, 1
state_len = width - 1                       # prefill: m=0，只缓存 2 个历史 token
weight = torch.randn(width, dim, dtype=torch.bfloat16).npu()
conv_states = torch.zeros(1, state_len, dim, dtype=torch.bfloat16).npu()  # 缓存冷启动
num_computed = torch.zeros(batch, dtype=torch.int32).npu()

def run(x, nct):
    return torch.ops.custom.npu_ai_infra_fused_causal_conv1d(
        x, weight, conv_states,
        num_computed_tokens=nct,
        residual_connection=1, conv_mode=1, inplace=False)

x1 = torch.randn(16, dim, dtype=torch.bfloat16).npu()        # 第 1 步：prefill 16 个 token
y1 = run(x1, num_computed)
num_computed += 16                                           # 已算 token 数推进

x2 = torch.randn(1, dim, dtype=torch.bfloat16).npu()         # 第 2、3 步：每次 1 个新 token
y2 = run(x2, num_computed); num_computed += 1
y3 = run(x2, num_computed); num_computed += 1
# 观察：conv_states 每次 run 后末尾内容变化；y 与「手写滑窗卷积 + 残差」对拍
```

3. **核对语义**（源码阅读型，无硬件可做）：对照 4.1.2 的流程回答——第 2 步 run 时走哪条 TilingKey 路径？`conv_states` 里此时的有效内容应该是哪些 token？若把 `residual_connection` 改 0，输出与手写对拍差多少？
4. **有环境时运行验证**：把骨架补全（x 为二维时须提供 `query_start_loc`）后在 NPU 上运行，并用 CPU 滑窗卷积参考实现对拍。

**需要观察的现象**：decode 两次调用后，`conv_states` 的内容等于最近 stateLen 个输入 token；输出 y 等于滑窗卷积加 x（残差开时）。

**预期结果**：第 3 步——`run_mode` 默认 0，若显式传 `run_mode=1` 才走 UPDATE_X_BSH 增量路径；两次 decode 间 `conv_states` 滑动前移一格。第 4 步数值对拍**待本地验证**（需要 A2/A3 环境；注意二维 x 必须传 `query_start_loc`，否则报参数错）。

#### 4.3.5 小练习与答案

**练习 1**：example 里为什么必须 `initial_state_input = initial_state.clone().detach()`？

答案：`npu_chunk_gated_delta_rule_recurrence` 原地修改 `initial_state`（它是递推状态本身）。若不先克隆留底，NPU 调用后张量已变成「最终状态」，再拿它喂 CPU 参考实现就用了错误的初值，对拍必然失真。`detach` 顺带切断与原张量的计算图关联。

**练习 2**：`compare` 用「一致率 ≥ 99%」而不是 `allclose` 全量通过，这种容差设计考虑了什么？

答案：线性注意力的递推会做大量浮点累加（尤其 C3 的 AtomicAdd 顺序不确定），NPU 与 CPU 的浮点求和顺序不同，逐位相等不可能；长序列上少量元素超出紧容差属于正常现象。按「99% 元素在 rtol=0.005/atol=1e-4 内」判过，兼顾了精度验收与浮点现实的平衡。

**练习 3**：lower_triangular_inverse 为什么限定 \( m = n \in \{32, 64, 128, 256\} \)？

答案：求逆只对方阵有意义（m=n）；而 32/64/128/256 恰是递推算子 chunk_size 的合法集合——这个下三角矩阵就是「chunk 内 token 间的因果解耦矩阵」\( I+\mathrm{strictLower}(\mathrm{diag}(\beta)KK^{\top}) \)，尺寸天然等于 chunk_size。尺寸枚举还让 kernel 可以按固定档位做分块求逆与循环展开。

## 5. 综合实践

**任务：画出 Gated Delta Net prefill 的「算子协作 + 张量流转」全景图，并做一次参数敏感度推演。**

1. **流水线图**：以本讲三个算子为节点，画出从原始 token 到注意力中间输出的数据流图。要求：每条边上标注张量名与形状（用符号 b/hv/n_chunks/chunk_size/dk/dv/cuSeqLen/dim 表达），并标注每个算子的「状态类张量」（conv_states、initial_state）在哪里被读、哪里被原地写。
2. **接力验证**：在图上回答三个问题——(a) `value` 与 `k_cumdecay` 头上的「乘上下三角逆矩阵」由谁完成？依据是什么（引用两份 docs 的原文）？(b) `actual_seqlens` 在递推算子 kernel 里如何被消费（引用 RunProcess 的行号）？(c) conv1d 的 APC 前缀缓存写入（`CopyOutXPreCache`）与普通缓存写入（`CopyOutXRunCache`）为何要分开写两份？
3. **参数敏感度**（纸面推演）：把 example 的 `chunk_size` 从 256 改为 64、`hv` 从 4 改为 32，重做 4.2.4 的 enableMSplit 计算（假设 aicNum=25）；再推演 `b=1, hv=1` 的极端情形会切到多少。
4. **有环境延伸**：在 NPU 上分别以 eager 与 `torch.compile(fullgraph=True)` 两种方式跑通 example（图模式注意 `keep_inference_input_mutations=True`），记录两种方式的输出一致率是否都在 99% 以上。

第 3 步预期：`b=3,hv=32,chunk_size=64` 时 `batchHead=96 ≥ 25`，不切 M，blockDim=25；`b=1,hv=1` 时 `mSplitMaxNum=min(64,128)/16=4`，i=4 时 `1*4≤25` 成立 → mSplitNum=4，但 `batchHead(1) > 2*1*4=8` 不成立 → enableMSplit=1、blockDim=4。第 4 步**待本地验证**。

## 6. 本讲小结

- `ai_infra_fused_causal_conv1d` 的核心不是卷积而是**缓存状态机**：`conv_states` 原地读写（aclnn 层必须 `CreateView`），`cache_indices` 二维即开启 APC 前缀缓存，`stateLen = K-1+m` 同时容纳滑窗历史与投机解码候选。
- conv1d 用 **TilingKey 路由**（0/1/2）在 Host 侧选择实现：prefill 长序列按「先切 Dim/先切序列」二选一，decode 增量走 UPDATE_X_BSH；`run_mode` 文档标注「不支持」但实为路由开关——文档滞后以代码为准。
- 递推算子把线性注意力拆成「块内可并行（提取出去）+ 块间必须串行（本算子）」；单 chunk 内 Cube 核做三次矩阵乘（C1/C2/C3）、Vector 核做两个逐元素操作（V0 衰减、V1 相减），靠 `CrossCoreSetFlag/WaitFlag` 接力，`gexp` 只取每 chunk 末值。
- 递推算子展示了 **TilingData 的另一种契约写法**：手写 8 字段 POD、host 反向 include kernel 头文件；以及「运行期字段分派（enableMSplit）」对照 conv1d 的「编译期 TilingKey 分支」。
- `initial_state` 是入参原地修改的递推状态，kernel 直接写回 GM；`actual_seqlens` 实际语义是「每 batch 长度」，kernel 在 Device 侧自行做前缀和（文档的 [b+1] 累加和描述与实现不符）。
- 学习新算子最有效的路径是 **example 对拍**：CPU 参考实现的几行循环往往就是算子公式的最直白翻译；`lower_triangular_inverse` 的尺寸约束 {32,64,128,256} 与 chunk_size 集合一致，印证它在流水线中承担块内解耦。

## 7. 下一步学习建议

- **u4-l1（FusedInferAttentionSink）**：如果说本讲算子是「小而精」，下一站是仓库体量最大的旗舰注意力算子——同样的「多场景路由」思想在 FIA 中膨胀为 18 位 TilingKey 与 FiaTilingRegistry 模板轮询，建议带着本讲的「路由—分派」视角去对比。
- **u5-l2（AIV/AIC 协同深入）**：本讲只看懂了 delta rule 的 Cube/Vector 分工逻辑，其 L1/L0A/L0B/L0C 四级存储手工搬运、`LoadData/Mmad/Fixpipe` 双缓冲流水、fraction 分型转置等硬核细节将在该讲系统展开（FIA 的 block_cube/block_vec 与本算子互为参照）。
- **源码延伸阅读**：`ai_infra_fused_causal_conv1d_fn_cutbs.h`（核内切 Dim 的另一种 prefill 策略，与 cutbsd 对照）；`tests/ut/op_host/test_ai_infra_fused_causal_conv1d_tiling.cpp`（无硬件验证 tiling 路由，衔接 u6-l1 的 faker 框架）；`tests/st/` 下两个算子的 ST 用例（与 example 的对拍套路同源，衔接 u6-l2）。
