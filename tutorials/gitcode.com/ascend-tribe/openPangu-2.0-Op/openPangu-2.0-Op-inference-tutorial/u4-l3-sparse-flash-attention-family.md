# 稀疏注意力家族：GQA、Pioneer 与 KV 量化

## 1. 本讲目标

上一讲（u4-l1/u4-l2）我们解剖了仓库中体量最大的稠密注意力算子 FusedInferAttentionSink。本讲把视野拉开，横向对比围绕「稀疏注意力（Sparse Attention）」组织起来的一个算子家族。学完本讲，你应该能够：

1. 说明 `sparse_indices`、`actual_seq_lengths_kv`、`sparse_block_size` 这些输入如何把注意力的计算范围从「全部 KV」缩小到「被选中的少量 block」，并理解它带来的收益与代价。
2. 说出家族中三个主算子——`sparse_flash_attention_gqa`、`sparse_flash_attention_pioneer`、`kv_quant_sparse_flash_attention`——在输入（rope 拆分、sink、metadata、量化）、数据类型与适用架构上的差异。
3. 理解「选块算子」（`quant_lightning_indexer`、`esa_select_topk`）与「元数据算子」（`sparse_flash_attn_metadata`）如何与主算子配合，构成一条完整的稀疏注意力推理流水线。
4. 能够独立画出这个家族的算子协作图（本讲综合实践）。

## 2. 前置知识

阅读本讲前，你应当已经学完：

- **u2-l3（Tiling 七步框架）**：知道 TilingData 是 host 下发给 device 的「施工图」。
- **u4-l1（FusedInferAttentionSink）**：知道注意力族算子使用自建的 `FiaTilingBase` / `FiaTilingRegistry` 框架（按 priority 轮询多个 tiling 模板），而非公共 `TilingBaseClass`。
- **u4-l2（AICPU 算子特例）**：知道 FIA Sink 的 metadata 算子跑在 AICPU 上，为主算子预计算分核数据。

本讲会用到几个术语，先统一解释：

| 术语 | 含义 |
| --- | --- |
| 稠密注意力 | 每个 query token 与**所有** KV token 计算相关性，计算量随序列长度平方增长 |
| 稀疏注意力 | 先用某种选块算法挑出「重要」的 KV block，只对这些 block 做注意力 |
| sparse_indices | 记录「挑中了哪些 block」的索引张量，是稀疏注意力的核心输入 |
| GQA | Grouped Query Attention，多个 query 头共享一组 KV 头 |
| MLA-absorb | 一种将 rope 部分与 nope 部分沿头维度拼接、且 K/V 共享同一份底层存储的注意力结构（见 pioneer 文档中 `attention_mode=2` 的说明） |
| Sink Token | 可学习的「吸收 token」，拼接在 key/value 前部参与计算，用于吸收多余的 attention score |
| Per-Tile-128 量化 | 每 128 个数据共享一个量化/反量化系数的 KV Cache 量化方案 |
| AICPU | NPU 上设备侧的通用 ARM CPU 核，适合跑无法在 AICore 上高效表达的逻辑（如读张量数值做分核决策） |

一句话直觉：**稀疏注意力 = 「选块算子」挑出 sparse_indices + 「主算子」只对选中的 block 做注意力 + （可选）「metadata 算子」预计算分核方案**。

## 3. 本讲源码地图

本讲涉及 6 个算子目录，全部位于 `ascendc/src/ops-transformer/attention/` 下：

| 算子目录 | 角色 | 本讲主要阅读的文件 |
| --- | --- | --- |
| `ai_infra_sparse_flash_attention_gqa` | 主算子·非量化 GQA 版（本讲精读标本） | `op_host/ai_infra_sparse_flash_attention_gqa_tiling.h`、`op_host/ai_infra_sfa_tiling_nonquant.h/.cpp`、`op_host/ai_infra_sparse_flash_attention_gqa_tiling.cpp`、`op_host/ai_infra_sparse_flash_attention_gqa_tiling_v3.cpp`、`op_host/ai_infra_sparse_flash_attention_gqa_tiling_register.cpp`、`op_api/aclnn_ai_infra_sparse_flash_attention_gqa.cpp`、`op_kernel/ai_infra_sparse_flash_attention_gqa.cpp`、`docs/npu_sparse_flash_attention_gqa.md`、`tests/st/test_npu_sparse_flash_attention_gqa_eager.py` |
| `ai_infra_sparse_flash_attention_pioneer` | 主算子·MLA-absorb 特化版 | `docs/npu_sparse_flash_attention_pioneer.md` |
| `ai_infra_kv_quant_sparse_flash_attention` | 主算子·KV 量化版 | `docs/npu_ai_infra_kv_quant_sparse_flash_attention.md`、`op_host/ai_infra_kv_quant_sparse_flash_attention_def.cpp`、`op_host/ai_infra_kv_quant_sparse_flash_attention_tiling.h` |
| `ai_infra_quant_lightning_indexer` | 选块算子·量化闪电索引器 | `docs/npu_ai_infra_quant_lightning_indexer.md` |
| `ai_infra_esa_select_topk` | 选块算子·block 压缩 TopK | `docs/npu_esa_select_topk.md`、`op_host/ai_infra_esa_select_topk_def.cpp`、`op_kernel/ai_infra_esa_select_topk.h` |
| `ai_infra_sparse_flash_attn_metadata` | 元数据算子（AICPU） | `docs/npu_ai_infra_sparse_flash_attn_metadata.md`、`op_kernel_aicpu/ai_infra_sparse_flash_attn_metadata_aicpu.h/.cpp` |

另外，torch 侧（`ascendc/torch_ops_extension/omni_custom_ops/ops_transformer/attention/`）中 `sparse_flash_attention_gqa`、`quant_lightning_indexer`、`esa_select_topk` 三个目录同时带有 `csrc` 与 `converter`，而 `ai_infra_sparse_flash_attention_pioneer`、`ai_infra_kv_quant_sparse_flash_attention`、`ai_infra_sparse_flash_attn_metadata` 只有 `csrc`（没有 torchair converter）。这一点在本讲 4.3 节会再次提及——「有 csrc 不等于有 fx2ge 图模式」，与 u3-l3 的结论一致。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

1. **稀疏索引**：sparse_indices 与实际序列长度如何缩小计算范围。
2. **主算子骨架**：SFA GQA 的 TilingData 与 tiling 模板注册链路。
3. **GQA/Pioneer/KV 量化变体**：三个主算子的输入差异对比。
4. **配套选块算子**：lightning indexer、esa topk 与 AICPU metadata。

### 4.1 稀疏索引：sparse_indices 如何缩小注意力计算范围

#### 4.1.1 概念说明

标准注意力对每个 query 都要和全部 \(S_2\) 个 KV token 做点积。当上下文长度达到数万 token 时，这个 \(O(S_1 \times S_2)\) 的计算成为瓶颈。稀疏注意力的思路是：先用一个轻量的「选块」阶段评估每个 KV block 的重要性，只保留最重要的若干 block 参与真正的注意力计算。

三个主算子的文档都用同一个公式描述这件事（以 GQA 版文档为例）：

\[
\text{softmax}\left(\frac{Q \tilde{K}^T}{\sqrt{d_k}}\right)\tilde{V}
\]

其中 \(\tilde{K}, \tilde{V}\) 不再是完整的 K/V，而是「基于某种选择算法（如 lightning_indexer_enhance）得到的重要性较高的 Key 和 Value」。也就是说，**稀疏性并不体现在算子内部的公式上，而体现在输入张量 \(\tilde{K}, \tilde{V}\) 的「取哪些行」由 `sparse_indices` 决定**。

#### 4.1.2 核心流程

`sparse_indices` 的契约（三个主算子文档一致）：

- 形状：`layout_query=BSND` 时为 `[B, Q_S, KV_N, sparse_size]`；`TND` 时为 `[Q_T, KV_N, sparse_size]`。
- 数据类型 `int32`，**每行的有效值必须排在前半部分，无效值（-1）排在后半部分**。
- 每个元素 `idx` 指向一个稀疏 block，对应 KV 的行区间 \([idx \times \text{sparse\_block\_size},\ (idx+1) \times \text{sparse\_block\_size})\)。
- `sparse_block_size=1` 为 Token-wise（逐 token 挑选）；大于 1（2 的幂，上限 128）为 Block-wise（整块挑选）。

一次稀疏注意力的执行流程可以概括为：

```text
for 每个 batch b、每个 query 行 s1、每个 KV 头 n2:
    读取 sparse_indices[b, s1, n2, :] 中的有效索引（遇 -1 截止）
    展开为 KV 行号列表 rows = ⋃ idx·block_size .. idx·block_size+block_size
    用 actual_seq_lengths_kv[b] 截断超出实际长度的行
    只取 K[b, rows]、V[b, rows]（PA 布局时经 block_table 二次映射）
    与 Q[b, s1] 计算 softmax(QK^T/√dk)·V
```

效果：如果 `sparse_count`（每行有效索引数）为 2048、`sparse_block_size` 为 1，那么无论 KV Cache 有 65536 行，每个 query 只需要与约 2048 行做注意力——计算量直接缩了一个数量级。代价是 KV 的读取从「连续大块搬运」变成「按索引离散跳读」，这正是文档里说「会引入大量的离散访存，造成数据搬运时间增加」的原因，也是这三个算子针对「离散访存做指令缩减与搬运聚合优化」的动机。

#### 4.1.3 源码精读

**证据一：ST 测试里的 CPU 参考实现直接展示了 sparse_indices 的语义。**

[test_npu_sparse_flash_attention_gqa_eager.py:L50-L64](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/tests/st/test_npu_sparse_flash_attention_gqa_eager.py#L50-L64)

```python
def gather_kv(k_tensor, v_tensor, sparse_indices, sparse_block_size, sparse_count, batch, n2_idx, s1_idx,
             cur_actual_seq_lengths_kv):
    s2_sparse = list()
    for sparse_id in sparse_indices:
        if sparse_id == -1:
            break                                  # 无效值在后半部分，遇 -1 即止
        begin_idx = sparse_id * sparse_block_size   # 索引 × block 大小 = KV 行起点
        end_idx = begin_idx + sparse_block_size \
                if begin_idx + sparse_block_size <= cur_actual_seq_lengths_kv else cur_actual_seq_lengths_kv
        s2_sparse.extend(np.arange(begin_idx, end_idx))  # 尾块用实际序列长度截断

    k_sparse = k_tensor[batch, n2_idx, s2_sparse, :]    # 按 KV 行号离散取行
    v_sparse = v_tensor[batch, n2_idx, s2_sparse, :]
    return k_sparse, v_sparse
```

这段「按索引 gather 出 \(\tilde K,\tilde V\)」的参考实现就是稀疏语义的最准确注脚：`sparse_id * sparse_block_size` 定位 block 起点，`actual_seq_lengths_kv` 截断尾块。

**证据二：因果性仍由 rightDownCausal mask 保证。**

同文件 [test_npu_sparse_flash_attention_gqa_eager.py:L66-L97](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/tests/st/test_npu_sparse_flash_attention_gqa_eager.py#L66-L97) 中的 `mask()` 函数在 gather 之后，把「KV 位置超过 `cur_actual_seq - cur_actual_seq_q + s1_idx + 1` 阈值」的列置为 \(-10^{12}\)（softmax 后近似为 0）。也就是说：稀疏选取负责「少算」，`sparse_mode=3`（rightDownCausal）的 mask 负责「不算未来」。

**证据三：稀疏几何参数被固化进 TilingData，随施工图下发 kernel。**

[ai_infra_sparse_flash_attention_gqa_tiling.h:L133-L137](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_host/ai_infra_sparse_flash_attention_gqa_tiling.h#L133-L137)

```cpp
// sfa 参数
BEGIN_TILING_DATA_DEF(SparseFlashAttentionSparseParams)
TILING_DATA_FIELD_DEF(int64_t, sparseBlockSize)
TILING_DATA_FIELD_DEF(uint32_t, sparseBlockCount)
END_TILING_DATA_DEF
```

`ai_infra_sfa_tiling_nonquant.cpp` 的 `FillTilingSparseFlashAttentionParams()` 把 host 侧解析出的两个值写进 TilingData（见 [ai_infra_sfa_tiling_nonquant.cpp:L450-L454](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_host/ai_infra_sfa_tiling_nonquant.cpp#L450-L454)），kernel 侧据此知道「一行索引对应多少 KV 行、一共最多多少个索引」，从而规划搬运循环。

#### 4.1.4 代码实践

1. **实践目标**：不依赖任何 NPU 环境，用纯 numpy 复现「稀疏索引 → gather → 稀疏注意力」的语义，验证你对 `sparse_indices` 契约的理解。
2. **操作步骤**：把下面的脚本保存为 `sparse_attn_sim.py` 并用 `python3 sparse_attn_sim.py` 运行（以下为**示例代码**，不是仓库原有文件）：

```python
import numpy as np

np.random.seed(0)
B, S1, N2, S2, D = 1, 2, 1, 4096, 64          # S2 远大于实际参与计算的行数
sparse_block_size, sparse_count = 16, 8        # 每行挑 8 个 16-token 的 block

q = np.random.rand(B, S1, D).astype(np.float32)
k = np.random.rand(B, S2, D).astype(np.float32)
v = np.random.rand(B, S2, D).astype(np.float32)

# 构造 sparse_indices：有效值在前，-1 填充在后（模拟 lightning indexer 输出）
idx = np.random.choice(S2 // sparse_block_size, sparse_count, replace=False) * sparse_block_size
indices = np.concatenate([np.sort(idx), -np.ones(S2 // sparse_block_size - sparse_count, dtype=np.int32)])
indices = np.broadcast_to(indices, (B, S1, N2, -1)).copy()

# 稀疏注意力（未加因果 mask 的简化版）
for b in range(B):
    for s in range(S1):
        rows = [r for i in indices[b, s, 0] if i >= 0
                for r in range(i, min(i + sparse_block_size, S2))]
        ks, vs = k[b, rows], v[b, rows]              # 参与计算的只有 ~128 行
        score = np.exp(ks @ q[b, s] - (ks @ q[b, s]).max())
        out = (score / score.sum()) @ vs
        print(f"参与注意力行数 = {len(rows)} / {S2}, out shape = {out.shape}")
```

3. **需要观察的现象**：输出中「参与注意力行数」是 128（= 8 × 16）而不是 4096。
4. **预期结果**：两行 query 均打印 `参与注意力行数 = 128 / 4096`；如果把 `sparse_count` 改为 0 之外的更小值（例如 4），行数按比例缩小。这就是稀疏索引带来的计算量收缩比的直接度量。本脚本纯 CPU 可运行。

#### 4.1.5 小练习与答案

**练习 1**：`sparse_indices` 为什么要求「有效值在前半部分、-1 在后半部分」，而不是允许 -1 散布在任意位置？

**答案**：因为 kernel 侧（以及参考实现 `gather_kv`）采用「遇 -1 即截止」的顺序扫描，这样可以只用一个循环变量就完成有效长度判断，无需先扫描一遍统计个数；同时也便于 host 侧 tiling 用 `sparseBlockCount` 一个标量描述上限。若允许散布，每个 block 的搬运判断都要逐元素检查。

**练习 2**：`sparse_block_size=16`、`actual_seq_lengths_kv[b]=100`、某个索引 `idx=6` 时，该 block 实际参与计算的 KV 行号范围是多少？

**答案**：起点 \(6\times16=96\)，理论终点 112，但被实际长度 100 截断，因此实际行号范围是 \([96, 100)\)，共 4 行。对应 `gather_kv` 中 `end_idx` 取 `cur_actual_seq_lengths_kv` 的分支。

**练习 3**：稀疏化之后为什么还需要 `sparse_mode=3` 的 mask？

**答案**：选块算法只保证「重要性」，不保证因果性——它可能选中位于当前 query 之后的 KV block。rightDownCausal mask 在 gather 出的稀疏列上再把「未来位置」置为 \(-10^{12}\)，两层机制各司其职：稀疏选取减计算量，因果 mask 保证自回归推理的正确性。

### 4.2 主算子骨架：SFA GQA 的 TilingData 与模板注册链路

#### 4.2.1 概念说明

`sparse_flash_attention_gqa`（下称 SFA GQA）是家族中结构最「标准」的主算子：非量化、GQA、复用注意力族自建的 `FiaTilingBase` 框架。理解它，pioneer 与 kv_quant 版就是「换输入、换 kernel 模板」的变体。

它的 op_host 目录比第 2 单元见过的 scatter 算子复杂得多，文件按职责分工：

| 文件 | 职责 |
| --- | --- |
| `*_tiling.h` | 定义 TilingData 的 8 个子结构体并聚合 |
| `*_tiling.cpp` | 框架入口：SOC 过滤 + 路由到 V3 主流程；另含 device 侧 tiling 入口 |
| `*_tiling_v3.cpp/.h` | V3 主流程：解析输入 → 校验 → 交 FiaTilingRegistry 轮询 |
| `ai_infra_sfa_tiling_nonquant.h/.cpp` | 非量化 tiling 模板类 `SfaTilingNonQuant` 的实现 |
| `*_tiling_register.cpp` | `IMPL_OP_OPTILING` 把入口函数登记给框架 |
| `*_tiling_check*.cpp` | 四类参数校验（单参数/存在性/特性交叉/一致性），与 u4-l1 讲过的 tiling_check 体系同构 |
| `*_split_core.h`、`*_info_parser.h/.cpp`、`*_compile_info.h`、`*_tiling_index.h` | 分核算法、输入解析、编译期缓存信息、IO 索引常量 |

#### 4.2.2 核心流程

SFA GQA 的一次 host 侧 tiling 调用链：

```text
aclnn 两段式（u2-l2 套路）
  └─ IMPL_OP_OPTILING(AiInfraSparseFlashAttentionGqa).Tiling(DoOpTiling...)   ← 框架登记
       └─ DoOpTilingAiInfraSparseFlashAttentionGqa        ← SOC 白名单过滤（ASCEND910_55 直接失败）
            └─ TilingAiInfraSparseFlashAttentionGqa       ← RouteToSfa 判定
                 └─ TilingAiInfraSparseFlashAttentionGqaV3
                      ├─ SfaInfoParser.Parse → 填 FiaTilingInfo
                      ├─ TilingCheck::Check  → 只读校验
                      └─ FiaTilingRegistry.DoTilingImpl    ← 按 priority 轮询 tiling 模板
                           └─ SfaTilingNonQuant::DoOpTiling
                                ├─ GetPlatformInfo（AIC/AIV 核数、libapi workspace）
                                ├─ InitParams / Split（内切 + 外切分核，产出 outerSplitParams）
                                ├─ FillTiling（把 8 组参数写入 TilingData）
                                ├─ CalcBlockDim（AIV:AIC = 2:1）
                                ├─ CalcWorkspaceSize / GenTilingKey
                                └─ SetBlockDim / SetTilingKey / SetWorkspaceSize / SetTilingData
```

这与 u4-l1 讲过的 FIA Sink 的 V3 主流程（Parse → Check → Registry 轮询）**完全同构**——注意力族共用这套自建框架。SFA GQA 目前只注册了一个模板 `SfaTilingNonQuant`。

#### 4.2.3 源码精读

**(1) TilingData：8 个子结构体的聚合。**

[ai_infra_sparse_flash_attention_gqa_tiling.h:L140-L149](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_host/ai_infra_sparse_flash_attention_gqa_tiling.h#L140-L149)

```cpp
BEGIN_TILING_DATA_DEF(AiInfraSparseFlashAttentionGqaTilingData)
TILING_DATA_FIELD_DEF_STRUCT(AiInfraSparseFlashAttentionGqaBaseParams, baseParams);
TILING_DATA_FIELD_DEF_STRUCT(AiInfraSparseFlashAttentionGqaPageAttentionParams, pageAttenParams);
TILING_DATA_FIELD_DEF_STRUCT(AiInfraSparseFlashAttentionGqaMaskParams, maskParams);
TILING_DATA_FIELD_DEF_STRUCT(AiInfraSparseFlashAttentionGqaWorkspaceParams, workspaceParams);
TILING_DATA_FIELD_DEF_STRUCT(AiInfraSparseFlashAttentionGqaInnerSplitParams, innerSplitParams);
TILING_DATA_FIELD_DEF_STRUCT(AiInfraSparseFlashAttentionGqaOuterSplitParams, outerSplitParams);
TILING_DATA_FIELD_DEF_STRUCT(AiInfraSparseFlashAttentionGqaFlashDecodeParams, fdParams);
TILING_DATA_FIELD_DEF_STRUCT(SparseFlashAttentionSparseParams, sfaParms);
END_TILING_DATA_DEF
```

八组参数各管一段：基础维度（b/n2/g/s1/s2/headDim 等，L35-L55）、PageAttention（blockSize，L59-L64）、mask（L67-L76）、workspace（L87-L94）、内切基本块 mBaseSize/s2BaseSize（L79-L84）、外切分核（L97-L103）、FlashDecode 规约（L106-L130）、稀疏参数（L133-L137）。

其中外切分核是「每个核负责一段任务区间」的落点：

[ai_infra_sparse_flash_attention_gqa_tiling.h:L97-L103](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_host/ai_infra_sparse_flash_attention_gqa_tiling.h#L97-L103)

```cpp
BEGIN_TILING_DATA_DEF(AiInfraSparseFlashAttentionGqaOuterSplitParams)
TILING_DATA_FIELD_DEF_ARR(uint32_t, SFA_MAX_AIC_CORE_NUM, bN2End)
TILING_DATA_FIELD_DEF_ARR(uint32_t, SFA_MAX_AIC_CORE_NUM, gS1End)
TILING_DATA_FIELD_DEF_ARR(uint32_t, SFA_MAX_AIC_CORE_NUM, s2End)
END_TILING_DATA_DEF
```

`bN2End/gS1End/s2End` 三个定长数组按核编号记录「该核负责的任务在 (batch×KV头, 分组×query序列, KV序列) 三个轴上的终点」，kernel 每个核用 `GetBlockIdx()` 查自己的区间——这就是 u2-l3「Host 只算计划、Device 只执行」在多核场景的形态。

**(2) 模板注册：priority 19 的编码含义。**

[ai_infra_sfa_tiling_nonquant.cpp:L564-L571](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_host/ai_infra_sfa_tiling_nonquant.cpp#L564-L571)

```cpp
// 值越小表示优先级越高. 对于SFA, 使用3位数表示优先级, 优先级编码含义为:
// 1. 百位代表非量化、伪量化、全量化等场景, 即: 0xx-非量化，1xx-伪量化, 2xx-全量化
// 2. 十位表示gqa、mla、泛化，即: x0x-mla, x1x-gpa, x2x-泛化
// 3. 个位代表特化模板到泛化模板的优先级排序
REGISTER_TILING_TEMPLATE_FIA(AiInfraSparseFlashAttentionGqa,
                             SfaTilingNonQuant,
                             std::vector<int32_t>({(int32_t)platform_ascendc::SocVersion::ASCEND910B}),
                             19);
```

优先级 19 = 「0xx 非量化」+「x1x gqa」+「9 泛化兜底」。这条注释是理解整个家族模板体系的钥匙：若未来出现伪量化（1xx）或 MLA（x0x）模板，只需以不同优先级注册，`FiaTilingRegistry` 会在 u4-l1 讲过的轮询机制里自动挑选。`IsCapable()` 则是模板的「能力自述」——[ai_infra_sfa_tiling_nonquant.cpp:L112-L134](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_host/ai_infra_sfa_tiling_nonquant.cpp#L112-L134) 中它检查 dtype 为 FP16/BF16 且 (qkHeadDim, ropeHeadDim, vHeadDim) 落在 {(128,0,128), (64,0,64), (192,64,128), (128,64,128)} 四个组合内，不满足即让位给其他模板。

**(3) V3 主流程与框架登记。**

[ai_infra_sparse_flash_attention_gqa_tiling_v3.cpp:L33-L47](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_host/ai_infra_sparse_flash_attention_gqa_tiling_v3.cpp#L33-L47)

```cpp
SFA_EXTERN_C ge::graphStatus TilingAiInfraSparseFlashAttentionGqaV3(gert::TilingContext *context)
{
    FiaTilingInfo sfaInfo;
    SfaInfoParser sfaInfoParser(context);
    if (sfaInfoParser.Parse(sfaInfo) != ge::GRAPH_SUCCESS) {
        return ge::GRAPH_FAILED;
    }
    // Check函数只做校验，不能修改sfaInfo中的信息
    if (TilingCheck::Check(sfaInfo) != ge::GRAPH_SUCCESS) {
        return ge::GRAPH_FAILED;
    }
    return FiaTilingRegistry::GetInstance().DoTilingImpl(context, &sfaInfo);
}
```

[ai_infra_sparse_flash_attention_gqa_tiling_register.cpp:L25-L34](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_host/ai_infra_sparse_flash_attention_gqa_tiling_register.cpp#L25-L34)

```cpp
IMPL_OP_OPTILING(AiInfraSparseFlashAttentionGqa)
    .TilingInputsDataDependency({optilingSfa::ACTUAL_SEQ_Q_INDEX,
                                 optilingSfa::ACTUAL_SEQ_KV_INDEX, ...},
                                {gert::TilingPlacement::TILING_ON_HOST, gert::TilingPlacement::TILING_ON_AICPU})
    .Tiling(optilingSfa::DoOpTilingAiInfraSparseFlashAttentionGqa)
    .TilingParse<...>(...)
```

注意 `TILING_ON_HOST` 与 `TILING_ON_AICPU` 并列声明：SFA 的 tiling 依赖 `actual_seq_lengths` 等输入张量的**数值**，这些值 Host 侧读不到，因此允许把 tiling 摆到设备侧执行（u5-l4 的 tiling sink 主题）。相应地，[ai_infra_sparse_flash_attention_gqa_tiling.cpp:L60-L70](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_host/ai_infra_sparse_flash_attention_gqa_tiling.cpp#L60-L70) 还用 `DEVICE_IMPL_OP_OPTILING` 注册了一个导出符号 `DeviceDoOpTilingAiInfraSparseFlashAttentionGqa`，供 tiling 下沉通道调用；同文件 L39-L58 的 `DoOpTiling...` 先按 SOC 版本过滤（`ASCEND910_55` 直接返回失败）再路由。

**(4) DoOpTiling：模板承重的四步。**

[ai_infra_sfa_tiling_nonquant.cpp:L537-L560](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_host/ai_infra_sfa_tiling_nonquant.cpp#L537-L560)

```cpp
ge::graphStatus SfaTilingNonQuant::DoOpTiling()
{
    if (GetPlatformInfo() != ge::GRAPH_SUCCESS) { return ge::GRAPH_FAILED; }
    InitParams();
    if (sfaInfo_->isMaxWorkspace) {          // aclgraph 静态 workspace 模式：按最大形状估
        CalcMaxWorkspaceSize(); GenTilingKey();
    } else {                                  // 正常模式：真实切分
        Split(); FillTiling(); CalcBlockDim(usedCoreNum_); CalcWorkspaceSize(); GenTilingKey();
    }
    if ((SetBlockDim(blockDim_) != ...) || (SetTilingKey(tilingKey_) != ...) ||
        (SetWorkspaceSize(workspaceSize_) != ...) || (SetTilingData(tilingData_) != ...)) {
        return ge::GRAPH_FAILED;
    }
    return ge::GRAPH_SUCCESS;
}
```

`Split()` 内部（L312-L339）先 `CalcInnerSize` 决定 KV 方向内切块大小 `sInnerSize_`（按 GQA 分组数 g 分 8192/4096/2048 三档，见 L190-L241 的注释——分组越多 workspace 越大，需降档控制 32MB 上限），再组装 `SfaSplitCoreBaseInfo` 调公共的 `SfaSplitCore()` 产出外切分核表。稀疏参数则由 `CreateSplitInput`（L263-L286）一并带入——分核算法同样要感知 `sparseMode/sparseBlockSize/sparseBlockCount`。

**(5) kernel 侧：按模板参数与 dtype 实例化。**

[op_kernel/ai_infra_sparse_flash_attention_gqa.cpp:L74-L110](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_kernel/ai_infra_sparse_flash_attention_gqa.cpp#L74-L110) 是 kernel 入口：模板参数 `<bool FLASH_DECODE, bool PAGE_ATTENTION, bool SOFTMAX_WITH_BRC, int LAYOUT_T, int KV_LAYOUT_T>` 把编译期场景固化，`__gm__` 参数表依次是 query/key/value/sparseIndices 与大量可选输入、attentionOut/softmaxLse 输出、workspace、tiling。函数体（L116-L153）拿到 `GetUserWorkspace(workspace)`、声明 `KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_MIX_AIC_1_2)`（AIC:AIV = 1:2 混合核，与 u4-l1 的结论一致），然后按 `ORIG_DTYPE_*` 编译期宏把 `SfaKernelNonQuant` 用 `FIAType<...>` 实例化成 half 或 bfloat16_t 版本：

```cpp
#if (ORIG_DTYPE_QUERY == DT_FLOAT16) && ...
    INVOKE_SFA_GQA_NO_QUANT_OP_IMPL(SfaKernelNonQuant, half, half, half, half,
                                    PAGE_ATTENTION, FLASH_DECODE, ...);
#endif
#if (ORIG_DTYPE_QUERY == DT_BF16) && ...
    INVOKE_SFA_GQA_NO_QUANT_OP_IMPL(SfaKernelNonQuant, bfloat16_t, ...);
#endif
```

真正的计算体在同目录 `ai_infra_sparse_flash_attention_kernel_nonquant.h`、`ai_infra_sparse_flash_attention_gqa_block_cube_nonquant*.h`、`ai_infra_sparse_flash_attention_gqa_block_vec_*.h`、`sparse_kv_block_service.h` 等头文件中，按「cube 核做 matmul、vec 核做 softmax/搬运、flashdecode 向量核做规约」的服务化拆分——与 u4-l1 的 FIA 三 service 结构同构，此处不再展开。

**(6) op_api 层仍是两段式。**

[aclnn_ai_infra_sparse_flash_attention_gqa.cpp:L189-L208](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/op_api/aclnn_ai_infra_sparse_flash_attention_gqa.cpp#L189-L208) 的 `aclnnAiInfraSparseFlashAttentionGqaGetWorkspaceSize` 只保留 5 个必选张量输入（query、key/value 为 TensorList、sparse_indices）+ 少量标量，其余 30 多个可选输入全部置 `nullptr` 占位后转调 inner 实现；L317-L323 的执行函数只做转发。另外 L42-L61 还有一个 `GetMaxWorkspaceSize` 变体，用假形状走一遍 tiling 取 workspace 上界——这是 aclgraph 静态图（u3-l3 提过）所需，与 `DoOpTiling` 中 `isMaxWorkspace` 分支呼应。

#### 4.2.4 代码实践

1. **实践目标**：验证 SFA GQA 的 tiling 逻辑可以在**无硬件**环境下用 UT 框架跑通（u6-l1 会系统讲该框架，这里先「尝鲜」）。
2. **操作步骤**：
   - 阅读 [tests/ut/op_host/test_sparse_flash_attention_gqa_tiling.cpp](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_gqa/tests/ut/op_host/test_sparse_flash_attention_gqa_tiling.cpp)，找出一组已注册的用例，记录它的 B/S1/S2/N/D、`sparseBlockSize`、`sparseBlockCount` 与断言方式。
   - 在已安装昇腾 CANN 开发套件的容器中执行（参照 u1-l2 的环境准备）：`bash build.sh -u --ophost 'ai_infra_sparse_flash_attention_gqa'`。
   - 若无硬件环境，改为纯源码跟踪：从 `IMPL_OP_OPTILING` 出发，沿本节 4.2.2 的调用链，在纸上写出每一步所在的文件与函数名。
3. **需要观察的现象**：UT 输出中 `SfaTilingNonQuant` 被选中执行，`SetTilingData` 成功；日志中出现 `SFA tilingKey_`、`SFA block dim` 等打点（对应 `GenTilingKey`/`CalcBlockDim` 中的 `OPS_LOG_I`）。
4. **预期结果**：编译并运行 UT 通过（**待本地验证**——本讲义编写环境无昇腾硬件，无法代跑；无环境时以完成调用链跟踪笔记为验收标准）。

#### 4.2.5 小练习与答案

**练习 1**：SFA GQA 的 TilingData 里为什么没有 `sparseIndices` 这个张量本身，只有 `sparseBlockSize/sparseBlockCount` 两个标量？

**答案**：TilingData 是 host→device 的序列化「施工图」，张量本体经 aclOpExecutor 的执行参数表下发，kernel 入口第 4 个 `__gm__` 参数就是 `sparseIndices`。TilingData 只携带 kernel 规划循环所需的**标量几何信息**（一个索引对应多少行、最多多少个索引），不复制数据。

**练习 2**：`REGISTER_TILING_TEMPLATE_FIA(..., 19)` 的 19 若改成 9，会发生什么？

**答案**：按注释的编码约定，9 = 「0xx 非量化」+「x0x mla」+「9」，即该模板会被归类为 MLA 模板。在 FiaTilingRegistry 按 priority 升轮询时它将排在真正 MLA 模板之前抢占注册位，语义类别与实现能力错配——这也说明优先级数值本身承载「场景分类」语义，不能随意改动。

**练习 3**：`KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_MIX_AIC_1_2)` 与 `CalcBlockDim` 中 `aivNum = 2U * coreNum` 呼应了什么事实？

**答案**：SFA 是 AIC（cube，做 QK^T 与 PV 两个 matmul）与 AIV（vector，做 softmax、scale、离散 KV 搬运）协同的混合核算子，AIV 数量按 AIC 的两倍配置参与 `CalcTschBlockDim`，与 u4-l1 在 FIA Sink 上看到的 AIC:AIV = 1:2 调度比一致。

### 4.3 GQA / Pioneer / KV 量化：三个主算子变体对比

#### 4.3.1 概念说明

三个主算子共享「稀疏注意力」内核思想，但面向不同的模型结构与量化策略：

- **GQA 版**：非量化、标准 GQA 分组，头维度组合最灵活（4 种），KV 布局支持 BSND/TND/PA_BSND。
- **Pioneer 版**：面向 MLA-absorb 结构（`attention_mode=2`）的特化——Q/K 沿 D 维拼接 nope+rope、K/V 共享同一份底层张量，新增 rope 拆分输入、sink token、metadata 输入。
- **KV 量化版**：在稀疏之上再叠加 KV Cache 量化（存 8bit、算前反量化），追求「存 8 算 8」的带宽收益。

#### 4.3.2 核心流程：输入差异的演进

对比三个算子的 torch 原型（均出自各自 docs）：

```text
GQA:  (query, key, value, sparse_indices, scale_value, *,
       actual_seq_lengths_query/kv, block_table, num_heads...,
       sparse_block_size, layout_query, layout_kv, sparse_mode, ...)

Pioneer: (query, key, value, sparse_indices, scale_value, *,
       block_table, actual_seq_lengths_query/kv,
       query_rope, key_rope,          ← MLA rope 拆分输入（新增）
       key_sink, value_sink,          ← 可学习 Sink Token（新增）
       meta_data,                     ← AICPU 预计算的分核元数据（新增）
       sparse_block_size, layout_query, layout_kv='PA_BSND', ...)

KV量化: (query, key, value, sparse_indices, scale_value,
       key_quant_mode, value_quant_mode, *,      ← 量化模式（新增，必选）
       key_dequant_scale, value_dequant_scale, block_table, ...,
       key_sink, value_sink, meta_data,          ← 同样有 sink 与 metadata
       quant_scale_repo_mode, tile_size, rope_head_dim, ...)
```

演进的主线非常清晰：**GQA 打底 → Pioneer 加 rope 拆分 + sink + metadata（面向 MLA 架构）→ KV 量化再加量化输入与混合存放约定（面向带宽）**。

#### 4.3.3 源码精读

**(1) Pioneer：K/V 一份数据两用，D 维拼接约定。**

[npu_sparse_flash_attention_pioneer.md:L91-L94](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attention_pioneer/docs/npu_sparse_flash_attention_pioneer.md#L91-L94) 的约束说明：

> 分离传输时参数 query 中的 D 和 key、value 的 D 值相等为 512，参数 query_rope 中的 D 和 key_rope 的 D 值相等为 64。
> 合并传输时参数 query 中的 D 和 key 的 D 值相等为 576，value 的 D 值为 512，参数 query_rope 和 key_rope 传 None。

配套的调用示例（同文件 L132-L147）给出直观代码：`combineQuery = torch.cat([query, query_rope], dim=-1)`（D: 512+64=576），而 `value = key[..., :512]`——**value 是 key 的前 512 维视图**，即「key 和 value 共享同一份底层张量数据」（L75 对 `attention_mode=2` 的解释）。sink 输入（L53-L55）则约束 `sink_num ∈ {0,128}`、key_sink 的 D=576、value_sink 的 D=512，与拼接约定严格对齐。

**(2) KV 量化：公式先反量化，D=656 的混合存放。**

[npu_ai_infra_kv_quant_sparse_flash_attention.md:L15-L18](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_kv_quant_sparse_flash_attention/docs/npu_ai_infra_kv_quant_sparse_flash_attention.md#L15-L18)

\[
\text{Attention}=\text{softmax}\left(\frac{Q\,\text{Dequant}({\tilde{K}^{INT8}},{Scale_K})^T}{\sqrt{d_k}}\right)\text{Dequant}(\tilde{V}^{INT8},{Scale_V})
\]

KV 以 INT8（A2/A3）或 float8_e4m3/hifloat8（A5）存储，计算前先按 Scale 反量化。最「反直觉」的是 [同文件 L97-L99](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_kv_quant_sparse_flash_attention/docs/npu_ai_infra_kv_quant_sparse_flash_attention.md#L97-L99) 的约束：

> 参数 query 中的 D 值为 576，即 nope+rope=512+64。
> 参数 key、value 中的 D 值为 656，即 nope+rope\*2+dequant\_scale\*4=512+64\*2+4\*4。

也就是说量化参数**不做独立张量**，而是被拼进 key 张量的最后一维（`quant_scale_repo_mode=1` combine 模式），示例代码 L141 的 `key = torch.cat((key, key_rope.view(torch.int8), antiquant_scale.view(torch.int8)), axis=3)` 演示了这种打包。这样做让离散跳读 KV block 时**数据和它的量化系数总在同一行**，一次搬运同时取回——这是对「稀疏离散访存」问题的量化侧对策。

**(3) kv_quant 的 OpDef：输入次序即 kernel 参数表。**

[ai_infra_kv_quant_sparse_flash_attention_def.cpp:L23-L81](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_kv_quant_sparse_flash_attention/op_host/ai_infra_kv_quant_sparse_flash_attention_def.cpp#L23-L81) 依次声明 `query, key, value, sparse_indices, key_dequant_scale, value_dequant_scale, block_table, actual_seq_lengths_query, actual_seq_lengths_kv, key_sink, value_sink, meta_data` 共 12 个输入与 1 个输出。与 [ai_infra_kv_quant_sparse_flash_attention_tiling.h:L28-L55](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_kv_quant_sparse_flash_attention/op_host/ai_infra_kv_quant_sparse_flash_attention_tiling.h#L28-L55) 中 `*_INPUT_INDEX` 常量一一对应（例如 `META_DATA_INPUT_INDEX = 11`、`ROPE_HEAD_DIM_ATTR_INDEX = 12`）——这正是 u2-l5 强调的「def 的 IO 声明顺序 = proto/tiling 索引常量」纪律在量化变体上的体现。该文件 L67-L68 还定义了 `QSFA_MAX_AIC_CORE_NUM = 26` 与 `METADATA_LIMIT = 1024`（metadata 输出固定 1024 个 int32，见 4.4.3）。

**(4) kernel 变体目录结构佐证架构差异。**

KV 量化版的 `op_kernel/` 按 `arch32/`（A2/A3，INT8）与 `arch35/`（A5，float8）分目录，arch35 下再细分 `vf/`（vector 基本块）与 `util_regbase.h` 等 9 个头文件；而 GQA 版的 kernel 是统一的非量化模板族。这印证了 u4-l7 将展开的「arch 特化」模式。

#### 4.3.4 代码实践

1. **实践目标**：仅凭三份 docs + OpDef，手工填写「三变体能力对照表」，训练从接口文档反推算子适用场景的能力。
2. **操作步骤**：
   - 逐一打开三份 docs（GQA：`npu_sparse_flash_attention_gqa.md` L23-L26 函数原型；Pioneer：L25；KV 量化：L24-L25），把下表空格补全：

| 维度 | GQA 版 | Pioneer 版 | KV 量化版 |
| --- | --- | --- | --- |
| KV 数据类型 | fp16/bf16 | fp16/bf16 | int8（A2/A3）/ float8_e4m3、hifloat8（A5） |
| query_rope/key_rope | 无此输入 | ______ | 无独立输入（拼在 D 内） |
| key_sink/value_sink | ______ | sink_num∈{0,128} | 有（D=576/512） |
| meta_data 输入 | 无 | 有 | ______ |
| layout_kv 取值 | BSND/TND/PA_BSND | ______ | BSND/TND/PA_BSND |
| key 的 D | 与 query 一致 | 576（合并）/512（分离） | ______ |
| 返回值 | (attention_out, softmax_lse) | ______ | 单 Tensor |

3. **需要观察的现象**：填表过程中会发现「哪些差异是架构性的（MLA 拼接、量化混合存放），哪些只是参数命名差异」。
4. **预期结果**：参考答案——GQA 版无 sink 输入；Pioneer 的 layout_kv 仅支持 `PA_BSND`；Pioneer 返回 `(attention_out, softmax_max, softmax_sum)` 三元组；KV 量化的 meta_data 输入为有（`META_DATA_INPUT_INDEX=11`）、key 的 D=656。

#### 4.3.5 小练习与答案

**练习 1**：Pioneer 文档说 value 可以是 `key[..., :512]` 的视图，为什么 KV 量化版不能这样做？

**答案**：Pioneer 的 key 是 fp16/bf16 连续存储，切片视图零拷贝可行；KV 量化版的 key 是 int8 量化数据且 D=656 中混有 rope 与反量化系数，value 的反量化系数与 key 的不同（`value_quant_mode` 与 `key_quant_mode` 各自独立），语义上两者就不是同一份内容的两个视图，示例中 `value = key.clone().npu()` 也体现了这一点。

**练习 2**：KV 量化版为什么把 `key_quant_mode`/`value_quant_mode` 设为**必选**参数，而 GQA 版没有量化参数？

**答案**：量化模式决定 kernel 侧反量化路径与模板选择（per-tile=2 是当前唯一取值，但接口层面预留扩展）；GQA 版是非量化算子，`SfaTilingNonQuant::IsCapable()` 只认 FP16/BF16 输入。必选参数让调用方显式声明进入的是量化执行路径，避免静默走错模板。

**练习 3**：torch 扩展目录里 `sparse_flash_attention_gqa` 带 `converter`，而 `ai_infra_sparse_flash_attention_pioneer`、`ai_infra_kv_quant_sparse_flash_attention` 只有 `csrc`，这与它们文档宣称的「支持图模式」矛盾吗？

**答案**：不矛盾。torch 侧有两条图路径（u3-l3）：fx2ge converter（torchair 转换表，需要 converter 目录）与 aclgraph 捕获（`torch.compile` reduce-overhead / `torch.npu.NPUGraph`，不需要 converter）。pioneer 与 kv_quant 的文档示例（如 pioneer 文档 L239-L256 的 `config.mode = "reduce-overhead"`、metadata 文档 L174-L206 的 `NPUGraph` 双流捕获）走的是 aclgraph 路径，因此没有 converter 目录也能入图。

### 4.4 配套选块算子：indexer、topk 与 AICPU metadata

#### 4.4.1 概念说明

主算子只解决「给定 sparse_indices 怎么算得快」。但 sparse_indices 从哪来？分核方案（meta_data）从哪来？家族为此配备了两类配角：

1. **选块算子**（产出 sparse_indices）：
   - `quant_lightning_indexer`：量化版闪电索引器，用低精度 matmul + ReLU + TopK 评估每个 KV token/block 的重要性。
   - `esa_select_topk`：把 key 序列分为 Initial/Local（必选）与 Middle（按块压缩后 TopK 选取）三段，产出 middle block 索引。
2. **元数据算子**（产出 meta_data）：`sparse_flash_attn_metadata`，AICPU 实现，把主算子的分核/切分方案预先算好，供 pioneer / kv_quant 的 `meta_data` 输入直接消费。

#### 4.4.2 核心流程

**indexer 的计算**（见其文档 L14 的公式，简化表述）：

\[ out = \text{Top-}k\left\{[1]_{1\times g}@\left[(W@K)\odot\text{ReLU}\left((Scale_Q@Scale_K^T)\odot(Q_{index}^{INT8}@(K_{index}^{INT8})^T)\right)\right]\right\} \]

流程三步：量化 Q×量化 K 得相关性 → 乘反量化系数并 ReLU 过滤负信号、加权 → 沿 g 维取 Top-k 索引。输入 query/key 均为 int8（D=128），输出 int32 索引，**直接作为主算子的 sparse_indices**（文档 L87 明确写出）。

**esa_select_topk 的计算**（文档 L11-L16）：把 KV 分为 Initial Tokens（开头必选）、Local Tokens（近处必选）、Middle Tokens（其余）；Middle 按 64 token 一块、每 16 token 压缩为 1 个表征（64→4），query 与压缩表征打分后取 topk 个 block；三部分并集参与注意力。它体现的是 NSA（Native Sparse Attention）式的「分层稀疏」策略。

**metadata 的计算**：输入只有形状/配置类参数（头数、序列长度张量、稀疏模式等，不含 Q/K/V 本体），AICPU kernel 据此跑一遍与主算子 kernel **完全同构**的分核算法，把结果写进 1024 元素的 int32 输出张量。

三者的协作（将在综合实践中画成图）：

```text
quant_lightning_indexer / esa_select_topk
        │ 产出
        ▼
  sparse_indices ──────────────┐
                               ├──►  pioneer / kv_quant 主算子 ──► attention_out
  sparse_flash_attn_metadata   │
        │ 产出 meta_data ──────┘
```

#### 4.4.3 源码精读

**(1) indexer：输出即主算子输入。**

[npu_ai_infra_quant_lightning_indexer.md:L87-L89](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_quant_lightning_indexer/docs/npu_ai_infra_quant_lightning_indexer.md#L87-L89)

> **out**（Tensor）：公式中的输出，作为 SparseFlashAttention 的输入 sparse_indices。数据格式支持 ND，数据类型支持 `int32`。

其 op_kernel 同样按 service_cube / service_vector / kernel / template_tiling_key 头文件族组织（与主算子一致的 AIC/AIV 服务化拆分），ST 测试 `test_npu_ai_infra_quant_lightning_indexer.py` 中 `_cpu_quant_lightning_indexer_golden` 提供了 CPU 参考实现，可用于理解数值语义。

**(2) esa_select_topk：压缩打分 + TopK。**

[ai_infra_esa_select_topk_def.cpp:L24-L54](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_esa_select_topk/op_host/ai_infra_esa_select_topk_def.cpp#L24-L54) 声明了 5 输入（query、key、三个 actual_seq 可选长度）1 输出（`topk_indices`）。kernel 头文件 [ai_infra_esa_select_topk.h:L137-L165](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_esa_select_topk/op_kernel/ai_infra_esa_select_topk.h#L137-L165) 中可见 `TopKInfo topkInfoData` 与 `topkInputLocal/topkIdLocal/topkValueLocal` 一组 UB 局部张量——分数计算在 cube/vec 上完成后，用 AscendC 的 TopK 原语在 UB 内完成排序选块，输出 int32 索引。文档 L47-L62 还区分了 Decode（qLen=1、b≤256）与 Prefill（qLen 可到 131072）两档约束，且明确 Decode 支持 aclgraph 入图、Prefill 不支持。

**(3) sparse_flash_attn_metadata：AICPU 上的「分核预演」。**

这是 u4-l2 讲过的 FIA Sink metadata 模式的姊妹实现，目录结构完全一致（`op_graph` 原型 + `op_kernel_aicpu` + `config.ini`）。核心证据：

[ai_infra_sparse_flash_attn_metadata_aicpu.h:L207-L242](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attn_metadata/op_kernel_aicpu/ai_infra_sparse_flash_attn_metadata_aicpu.h#L207-L242)

```cpp
// === Core splitting logic — 1:1 mapping to pioneer kernel functions ===
// Top-level dispatcher (pioneer InitCalcParams, line 552)
void CalcSplitPlan(...);
// Non-FD mode: basic block equal distribution (pioneer InitCalcParamsEach, line 852)
void SplitCoreNonFD(...);
// FD mode: work-item equal distribution (pioneer InitCalcParamsEachFD, line 561)
void SplitCoreFD(...);
// FD: vector load balancing (pioneer SplitFDTasks, line 749)
void SplitFDTasks(SplitResult &result);
```

注释直接说明：**这个 AICPU kernel 的分核函数与 pioneer 主算子 kernel 内部的同名函数逐行对应**（甚至标注了行号）。也就是说「不传 meta_data 时 kernel 自行分核」与「传 meta_data 时读预计算结果」是同一套算法的两种执行时机——把 device 侧的重复 tiling 计算搬到 AICPU 提前做，主算子 kernel 直接查表，从而「加速整体推理流程」（metadata 文档 L10 的原话）。

输出张量的布局在同文件 L23-L76 用枚举固化：`META_SIZE = 1024`，前 6 个是基础参数（`M_BASE_SIZE/S2_BASE_SIZE/...`），随后按 `FA_CORE_STRIDE = 7`（每核 7 个字段：bN2/gS1/s2 的 start/end 与 KV 起点）、`FD_CORE_STRIDE = 6`（FlashDecode 每核 6 个字段）、FD 向量核规约区、`NEED_INT` 区段展开。`metadata_type` 属性区分 `"SFA"`（pioneer 消费）与 `"QSFA"`（kv_quant 消费，见 aicpu.cpp L279-L285 的校验）。

[aclnn 元数据入口与注册]：[ai_infra_sparse_flash_attn_metadata_aicpu.cpp:L23-L54](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attn_metadata/op_kernel_aicpu/ai_infra_sparse_flash_attn_metadata_aicpu.cpp#L23-L54) 的 `Compute()` 依次 `Prepare → CalcSplitPlan → GenMetaData`；L120/L130 直接解引用 `actualSeqLengthsQ_/Kv_` 的 `GetData()`——这正是 u4-l2 的结论重演：分核依赖输入张量数值，Host 读不到，只能下沉到 AICPU。L926 `REGISTER_CPU_KERNEL(AI_INFRA_SPARSE_FLASH_ATTN_META_DATA, ...)` 完成注册。

最后看消费端的配合：[npu_ai_infra_sparse_flash_attn_metadata.md:L174-L206](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attn_metadata/docs/npu_ai_infra_sparse_flash_attn_metadata.md#L174-L206) 的图模式示例展示了**双流 + event 的生产者-消费者模式**——metadata 在 aicpu_stream 上计算并 `event.record`，主算子所在 aicore_stream `wait_event` 后再启动，与 u4-l2 讲过的 FIA Sink metadata 用法完全一致。

#### 4.4.4 代码实践

1. **实践目标**：算清 metadata 输出张量中「第 3 个核的 FA 分核区间」的地址偏移，理解 1024 元素的结构化布局。
2. **操作步骤**：
   - 打开 [ai_infra_sparse_flash_attn_metadata_aicpu.h:L67-L76](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/attention/ai_infra_sparse_flash_attn_metadata/op_kernel_aicpu/ai_infra_sparse_flash_attn_metadata_aicpu.h#L67-L76) 的 `MetaLayout` 常量与 L37-L55 的字段枚举。
   - 手工推导：核编号 `core=3`（0 基）的 `GS1_END` 字段、以及 FD 区第 3 组的 `S2_SPLIT_NUM_OF_FD_HEAD` 字段各自的绝对下标。
   - 用 Python 验证（**示例代码**，纯 CPU）：打印这两个下标，检查是否落在 [0, 1024) 内。
3. **需要观察的现象**：两个下标都不越界，且 FD 区起点（258）恰为 `FA_CORE_BASE(6) + 7 × 36`（36 = MAXAICNUM）。
4. **预期结果**：`GS1_END(core=3) = 6 + 7×3 + 3 = 30`；`S2_SPLIT_NUM_OF_FD_HEAD(第3组) = 258 + 6×3 + 2 = 278`。两个值均 < 1024。无需 NPU 环境。

#### 4.4.5 小练习与答案

**练习 1**：indexer 与 esa_select_topk 产出的索引有什么粒度差异？

**答案**：indexer 以 token/block 为单位（`sparse_block_size` 可为 1~128 的 2 的幂），直接服务于 SFA 主算子的 sparse_indices；esa_select_topk 以 64-token 的 block 为单位（`blk_size` 仅支持 64），且只对 Middle 段选块，Initial/Local 段无条件保留——它是「分段式」稀疏，前者是「全局打分」稀疏。

**练习 2**：为什么 metadata 算子跑在 AICPU 而不是像 indexer 一样跑在 AICore？

**答案**：metadata 的核心操作是读 `actual_seq_lengths_*` 张量数值、做负载均衡枚举与边界换算——分支极多、并行度低的标量逻辑，AICore 的向量/矩阵流水线不擅长；AICPU 是通用 ARM 核，适合这类控制流密集计算。这与 u4-l2 对 FIA Sink metadata 的分析结论相同。

**练习 3**：如果不给 pioneer 传 `meta_data` 会怎样？

**答案**：按 pioneer 文档 L57 的说明「不传则 kernel 自行计算分核数据」——功能不缺失，只是把分核计算留在了主算子 kernel 内部每次执行时做；传 metadata 则是把这些计算搬到 AICPU 提前/并行完成，属于性能优化而非正确性开关。

## 5. 综合实践

**任务：绘制稀疏注意力家族的「算子协作图」**（本讲规格中规定的代码实践任务）。

1. **实践目标**：把 4.1~4.4 的认知固化成一张可复查的图，明确家族中 5 个算子各自消费/生产哪些张量，且每个箭头都有 docs 接口签名佐证。
2. **操作步骤**：
   - 新建一个笔记文件，先抄下这张骨架图并补全所有 `?` 处的张量名：

```text
                       ┌──────────────────────────────┐
   query(int8,D=128) ─▶│  quant_lightning_indexer     │
   key(int8,D=128)   ─▶│  weights / *_dequant_scale   │──▶ ? (int32, [B,S1,N2,k])
                       └──────────────────────────────┘
                       ┌──────────────────────────────┐
   query / 压缩key    ─▶│  esa_select_topk             │──▶ ? (int32)
   blk/init/local/topk─▶│                             │
                       └──────────────────────────────┘
                       ┌──────────────────────────────┐
   头数/序列长度张量  ─▶│  sparse_flash_attn_metadata  │──▶ ? (int32, 1024 元素)
   稀疏配置/布局      ─▶│  (AICPU, metadata_type=?)   │    │
                       └──────────────────────────────┘    │ aicpu_stream + event
                                                           ▼
   query(D=576)  key/value(D=656, int8)  block_table ──▶ kv_quant_sparse_flash_attention ──▶ attention_out
   query(D=576)  combineKey/value视图    block_table ──▶ sparse_flash_attention_pioneer   ──▶ (out, max, sum)
   query(fp16)   key/value              block_table ──▶ sparse_flash_attention_gqa       ──▶ (out, softmax_lse)
        ▲                   ▲                    ▲
        └───────────────────┴────────────────────┘
          上三者共同的稀疏索引输入 = indexer / topk 的输出
   （GQA 版不消费 meta_data；pioneer/kv_quant 消费）
```

   - 为每条产出边标注证据：indexer 输出去向见其文档 L87「作为 SparseFlashAttention 的输入 sparse_indices」；metadata 输出去向见其文档 L62-L66「可直接传入 pioneer 的 metadata 参数 / 需与 pioneer 或 kv_quant 配合使用」；pioneer 输入签名见其文档 L25；kv_quant 输入次序见 def 文件 L23-L81。
   - 再补一张「消费矩阵」：行 = 5 个算子，列 = {sparse_indices, meta_data, key_sink, query_rope, block_table, actual_seq_lengths_kv}，格内填「生产/消费/不涉及」。
3. **需要观察的现象**：你会发现 `sparse_indices` 是全家族的「总枢纽」——两个选块算子生产它、三个主算子消费它；而 `meta_data` 只被 pioneer 与 kv_quant 消费（metadata 文档 L66 的约束也印证 GQA 不在其中）。
4. **预期结果**：完成一张带 5 个算子、至少 8 种张量的协作图 + 消费矩阵，每条边有「文件:行号」级证据。整个过程无需 NPU 硬件（纯文档与源码阅读）。
5. 有昇腾环境时可选进阶：仿照 metadata 文档 L74-L121 的示例，把 `npu_ai_infra_sparse_flash_attn_metadata` 与 `npu_ai_infra_sparse_flash_attention_pioneer` 串成最小流水线跑通（**待本地验证**）。

## 6. 本讲小结

- **稀疏索引是家族的枢纽**：`sparse_indices`（int32，有效值前置、-1 填充尾部）+ `sparse_block_size` 把注意力计算范围从全部 KV 缩到被选中的 block；`actual_seq_lengths_kv` 截断尾块，`sparse_mode=3` 的 rightDownCausal mask 保证因果性；ST 测试中的 `gather_kv`/`mask` 参考实现是理解语义的最佳材料。
- **SFA GQA 复用注意力族自建框架**：TilingData 聚合 8 个子结构（含稀疏参数 `sparseBlockSize/sparseBlockCount` 与按核记录任务区间的 `outerSplitParams` 数组）；tiling 链路为 `IMPL_OP_OPTILING → V3(Parse/Check) → FiaTilingRegistry 轮询 → SfaTilingNonQuant(prio=19)`；priority 三位数编码（量化场景/结构/特化度）是模板体系的组织原则。
- **tiling 依赖输入张量数值**：`TilingInputsDataDependency` 同时声明 `TILING_ON_HOST/TILING_ON_AICPU`，并提供 `DeviceDoOpTiling...` 导出符号，为 tiling 下沉（u5-l4）留好通道。
- **三个主算子是递进变体**：GQA（非量化打底）→ Pioneer（MLA-absorb：rope 拆分/合并传输、K/V 一份数据两用、sink、meta_data，layout_kv 仅 PA_BSND）→ KV 量化（int8/float8 KV、量化系数按 D=656 混拼进 key 行、per-tile-128）。
- **配角算子两类**：选块算子（quant_lightning_indexer 的量化 TopK、esa_select_topk 的 Initial/Middle/Local 分段压缩选块）产出 sparse_indices；AICPU metadata 算子以与 pioneer kernel「1:1 映射」的分核算法产出 1024 元素结构化 meta_data，用双流 + event 与主算子同步。
- **kernel 侧形态与 FIA Sink 同构**：`KERNEL_TYPE_MIX_AIC_1_2` 混合核、FIAType 模板实例化、cube/vec/flashdecode 服务化头文件拆分——学过 u4-l1 后这些模式可以直接迁移。

## 7. 下一步学习建议

本讲收尾了「核心推理算子族」的注意力篇章。建议接下来：

1. **u4-l4（因果卷积与 Delta Rule 递推）**：换一类注意力配套算子（线性注意力侧），对比它们与稀疏注意力家族在「状态缓存 + 增量推理」上的不同思路。
2. **u5-l2（AIV/AIC 协同与 FlashDecode）**：本讲只指出了 `KERNEL_TYPE_MIX_AIC_1_2` 与 flashdecode 头文件的存在，跨核同步原语与向量核并行分解的细节在那讲展开。
3. **u5-l4（Tiling Sink 与 AICPU 执行通道）**：本讲出现的 `TILING_ON_AICPU`、`DeviceDoOpTiling...`、`DEVICE_IMPL_OP_OPTILING` 是该讲的入口素材。
4. 若想继续横向阅读稀疏家族源码，推荐顺序：`ai_infra_sparse_flash_attention_gqa/op_kernel/sparse_kv_block_service.h`（离散 KV 搬运聚合的实现）→ `ai_infra_kv_quant_sparse_flash_attention/op_kernel/arch35/`（A5 特化的 regbase 路线）→ `ai_infra_sparse_flash_attn_metadata/op_kernel_aicpu/ai_infra_sparse_flash_attn_metadata_aicpu.cpp` 的 `SplitCoreFD`（与 pioneer kernel 行号对照着读，体会「同一算法、两种执行时机」）。
