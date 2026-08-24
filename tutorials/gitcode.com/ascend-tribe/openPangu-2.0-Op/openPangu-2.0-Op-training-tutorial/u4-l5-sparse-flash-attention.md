# u4-l5 SparseFlashAttention 前向与反向：稀疏注意力

## 1. 本讲目标

本讲是 Attention 算子族的第 5 讲，聚焦「稀疏 FlashAttention」这一对：前向算子 `sparse_flash_attention_enhance`（下文简称 SFA 前向）与反向算子 `sparse_flash_attention_grad_enhance`（简称 SFA 反向）。学完本讲，你应该能够：

1. 解释 `sparse_indices`（稀疏索引）如何改变 softmax 的分母计算范围——注意力从「对整个 S2 求 softmax」变成「对被索引选中的 token 子集求 softmax」。
2. 看懂 SFA 前向的 tiling 如何用 `GET_TPL_TILING_KEY`（模板参数化 tilingKey）替代 u4-l3 中稠密 FA 的 64bit 手工位域编码。
3. 描述 MLA 场景下 `service_cube_mla`（Cube 侧 Matmul + 稀疏 Gather）与 `service_vector_mla`（Vector 侧 MergeKv + online softmax）的分工，以及 C_TEMPLATE / V_TEMPLATE 两种稀疏取数策略的差异。
4. 对比 SFA 反向 `bs1_basic` 切分（按 S1 行分核 + Gather/Scatter 组织）与前向按 B·N2·S1 均衡分核的差异。
5. 独立完成「dense FA 与 sparse FA 的 tilingKey 集合对比」「稀疏索引张量 shape 约束整理」「稀疏模式掩码的 Python 示意」三项实践。

## 2. 前置知识

阅读本讲前，你应当已掌握前四讲的内容。这里补几个本讲特有的概念，用通俗语言解释：

- **稀疏注意力（Sparse Attention）**：标准注意力的计算量是 \(O(S_1 \times S_2)\)。长序列场景下，先用一个「索引器」（本仓库的 `lightning_indexer_enhance`，见 u4-l6）给每个 query 挑出最重要的若干 KV token，只对这些 token 做注意力，计算量随被选 token 数线性化。官方文档的公式是 \(\text{softmax}(\frac{Q@\tilde{K}^T}{\sqrt{d_k}})@\tilde{V}\)，其中 \(\tilde{K},\tilde{V}\) 就是「被选中的 K/V」。
- **稀疏索引张量 `sparse_indices`**：一个 int32 张量，每行存放「该 query 选中的 KV block 编号」。特殊值 `-1` 表示填充（无效），要求有效值全在前半部分。它是前向第 4 个必选输入、反向第 4 个必选输入——两个算子靠同一份索引对齐「谁被选中」。
- **`sparse_block_size`（稀疏块大小）**：索引的单位。1 表示 token 级（每个索引挑 1 个 token），大于 1 表示 block 级（每个索引挑一整块连续 token）。前向支持 [1,128] 的 2 的幂，反向只支持 1/8/16/32/64。
- **MQA / GQA**：本算子族强制 `KV_N（N2）=1`，即所有 query 头共享同一份 K/V（Multi-Query Attention）；`G = N1 / N2` 是每个 KV 头对应的 query 头组大小。
- **MLA（Multi-head Latent Attention）**：DeepSeek 式的注意力变体。每个头的向量拆成「nope 部分（D=512）」和「rope 部分（Dr=64）」，计算时沿 D 维拼接成 576 维参与矩阵乘。本讲两个算子的 kernel 名都带 `mla` 后缀，维度 512/64/576 在 kernel 里是写死的常量。
- **C_TEMPLATE / V_TEMPLATE**：SFA 前向内部的两套「稀疏取数」实现。C 模板由 Cube 核在搬 K 进 L1 时按索引离散取；V 模板先由 Vector 核把离散的 KV 聚合成连续缓冲（MergeKv），Cube 再按连续地址读。tiling 根据 `sparseBlockSize` 是否 ≤4 自动选择。
- **Gather / Scatter**：反向算子的核心数据动作。Gather 按 `topk_indices` 把分散的 K/V 行搬到 workspace 拼成连续矩阵算梯度；Scatter 把算好的 dk/dv 按「同一个 token 被多个 query 选中」的规律累加回原位。对应 PyTorch 里的 `torch.gather` / `index_add_`。

## 3. 本讲源码地图

本讲涉及两个算子目录（前向、反向）共 11 个关键文件：

| 文件 | 作用 |
| --- | --- |
| `ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/docs/npu_sparse_flash_attention_enhance.md` | 前向规格文档：接口原型、sparse_indices 约束、sparse_mode 枚举、调用示例 |
| `.../sparse_flash_attention_enhance/op_host/sparse_flash_attention_enhance_def.cpp` | 前向原型注册：9 输入 / 3 输出 / 9 属性，ascend910b + ascend910_93 双注册 |
| `.../sparse_flash_attention_enhance/op_host/sparse_flash_attention_enhance_tiling.cpp` | 前向 Tiling：`SFAInfoParser` 解析 → `SFATilingCheck` 校验 → `SFAMlaTiling` 切分 |
| `.../sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance.cpp` | 前向 kernel 入口：按模板参数分发 `SparseFlashAttentionEnhanceMla` |
| `.../sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance_template_tiling_key.h` | 模板参数（FLASH_DECODE/LAYOUT_T/KV_LAYOUT_T/TEMPLATE_MODE）的合法组合声明 |
| `.../sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance_kernel_mla.h` | 前向 kernel 主体：Init/ProcessBalance/PreloadPipeline，稀疏游标 `CalcSinnerTopKBegin` |
| `.../sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance_service_cube_mla.h` | Cube 侧服务：`CalcTopKBlockInfo` 按索引离散搬 K 进 L1，mm1/mm2 |
| `.../sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance_service_vector_mla.h` | Vector 侧服务：`MergeKv` 聚合离散 KV、`SoftmaxFlashV2Compute` 在线 softmax |
| `ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/op_host/sparse_flash_attention_grad_enhance_tiling_common.cpp` | 反向公共校验：softmaxMax/Sum 的 [N2,T1,G] shape 检查、dtype 检查 |
| `.../sparse_flash_attention_grad_enhance/op_host/sparse_flash_attention_grad_enhance_tiling_bs1_basic.cpp` | 反向 bs1_basic tiling：shape/属性解析、五档十进制 tilingKey、Gather/Scatter workspace |
| `.../sparse_flash_attention_grad_enhance/op_kernel/sparse_flash_attention_grad_enhance.cpp` | 反向 kernel 入口：16 个 tilingKey 精确匹配分发 |

反向另有 `op_host/sparse_flash_attention_grad_enhance_tiling.cpp`（注册入口，仅 74 行）、`op_host/sparse_flash_attention_grad_enhance_def.cpp`（原型）、`op_kernel/sparse_flash_attention_grad_enhance_bs1_basic.h`（kernel 主体）、`basic_modules/`（cube1~5 五个矩阵乘与 vec_op）会在正文引用。

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：①稀疏索引契约（前向 def + 文档）；②前向 Tiling 与模板化 tilingKey；③前向 Kernel 的 Cube/Vector 分工；④反向 Tiling（common 校验 + bs1_basic 责任链）；⑤反向 Kernel 的 Gather→Matmul→Scatter 组织。

### 4.1 模块一：稀疏索引如何改变 softmax 的分母

#### 4.1.1 概念说明

稠密 FA（u4-l1~u4-l3）里，第 \(t\) 行 query 的 softmax 分母是

\[
Z_t=\sum_{u=0}^{S_2-1} e^{s_{t,u}-m_t},\qquad m_t=\max_u s_{t,u}
\]

分母覆盖整个 key 序列 \(S_2\)。稀疏化之后，每个 query 只保留一个索引集合 \(\mathcal{I}_t=\{b_0,b_1,\dots,b_{K-1}\}\)（K 为 `sparse_block_count`，每个 \(b_i\) 指向一段长 `sparse_block_size` 的 token），分母变成

\[
\tilde{Z}_t=\sum_{u\in \text{Gather}(\mathcal{I}_t)\ \cap\ \text{mask}} e^{s_{t,u}-\tilde{m}_t}
\]

「∩ mask」指因果约束：本算子默认 `sparse_mode=3`（rightDownCausal 下三角），第 \(t\) 行只允许看到位置 ≤ \(t+\text{nextTokensPerBatch}\) 的 key。所以稀疏 softmax 的有效范围是两个约束的交集：**索引选了什么** × **因果允许看到什么**。这就是 kernel 里反复出现的 `threshold`（因果上界）与 `topKBaseOffset`（索引行首地址）两个量的由来。

与稠密 FA 相比还有两个接口差异值得注意：

- 前向没有 `atten_mask` 输入——因果性由 `sparse_mode` + `pre_tokens`/`next_tokens` 属性在 kernel 内推导。
- 前向输出只有 3 个（`attention_out`、`softmax_max`、`softmax_sum`），没有 `softmax_out`（稀疏场景注意力矩阵本就不完整，落盘无意义）；`return_softmax_lse=False` 时后两个输出是占位。

#### 4.1.2 核心流程

1. 框架（或 torch_ops_extension）传入 `sparse_indices`（int32，[B,S1,N2,K] 或 [T1,N2,K]），每行 K 个 block 编号，有效值在前、-1 填充在后。
2. kernel 对每个 (b, s1, n2) 行：
   - 计算因果 `threshold`：`sparse_mode==3` 时为 `nextTokensPerBatch + s1Idx + 1`，否则为该 batch 的实际 KV 长度；
   - 有效索引块数 `validCount = ceil(threshold / sparseBlockSize)` 与 `sparseBlockCount` 取小；
   - 顺序扫描索引数组，跳过 `blockBegin >= threshold` 的块、遇 `-1` 终止，把落在阈值内的 token 拼成本轮要算的连续段。
3. 对拼好的段做 online softmax（与稠密 FA 相同的 SoftmaxFlashV2 滚动 max/sum）→ 分母只含被选 token。

#### 4.1.3 源码精读

先看原型。前向 def 里 `sparse_indices` 是第 4 个必选输入（int32），`query_rope`/`key_rope` 是 MLA 的可选输入：

[sparse_flash_attention_enhance_def.cpp:L38-L47](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_host/sparse_flash_attention_enhance_def.cpp#L38-L47)——声明 `sparse_indices`（REQUIRED，DT_INT32）与 `block_table`（OPTIONAL，PageAttention 时的 block 映射表）。注意 `sparse_indices` 的输入索引号 3 与 tiling 头文件里的 `SPARSE_INDICES_INPUT_INDEX = 3` 对应，位置不能调换（回顾 u2-l2：Input 声明顺序即运行期索引）。

[sparse_flash_attention_enhance_def.cpp:L80-L88](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_host/sparse_flash_attention_enhance_def.cpp#L80-L88)——9 个属性：`scale_value`（必选）、`sparse_block_size`（默认 1）、`layout_query`/`layout_kv`（默认 BSND）、`sparse_mode`（默认 3，注释写明「只计算下三角」）、`pre_tokens`/`next_tokens`（默认 INT64_MAX）、`attention_mode`（2 = MLA-absorb）、`return_softmax_lse`。

文档对索引张量的约束是本模块的「接口契约」：

[npu_sparse_flash_attention_enhance.md:L37](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/docs/npu_sparse_flash_attention_enhance.md#L37)——`sparse_indices`：layout_query 为 BSND 时 shape 为 `[B, Q_S, KV_N, sparse_size]`，TND 时为 `[Q_T, KV_N, sparse_size]`；「需要保证每行有效值均在前半部分，无效值均在后半部分，且 sparse_size 大于 0」。

kernel 侧对「分母范围」的落实在两处。第一处是每行 token 的有效 KV 长度：

[sparse_flash_attention_enhance_kernel_mla.h:L315-L331](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance_kernel_mla.h#L315-L331)——`GetSparseActualSeqLen`：先处理 `nextTokensPerBatch < 0` 且 `s1Idx` 落在无效区的行（`curActualSeqLen=0`）；`sparse_mode==3` 时 `threshold = nextTokensPerBatch + s1Idx + 1`（因果上界）；最终 `curActualSeqLen = min(sparseBlockCount * sparseBlockSize, threshold)`。**这一行就是「稀疏分母」的 Host 侧等价物：softmax 的求和范围被同时钳位在「索引能提供的 token 数」和「因果允许的位置」之内。**

第二处是每轮计算段的扫描（C_TEMPLATE 模式）：

[sparse_flash_attention_enhance_kernel_mla.h:L942-L972](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance_kernel_mla.h#L942-L972)——`CalcSinnerTopKBegin`：先算 `thresholdSparseCount = ceil(threshold / sparseBlockSize)` 并与 `sparseBlockCount` 取小得 `validCount`；读 `topKGm.GetValue(info.topKBaseOffset + curTopKIdx)`，若为 `-1` 或游标已到 `validCount`，本行输出全零；否则按 `blockBegin = sparseIndices * sparseBlockSize`、`blockEnd = min(blockBegin + sparseBlockSize, threshold)` 截出本块有效长度。

[sparse_flash_attention_enhance_kernel_mla.h:L985-L1017](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance_kernel_mla.h#L985-L1017)——继续向后累加块长直到凑满 `s2BaseSize`（一轮的基本块），记录 `curTopKIdx / curOffsetInSparseBlock` 两个游标，块超阈值（`blockBegin >= info.threshold`）就 `continue` 跳过，遇到 `-1` 终止。这两个游标会随 `RunInfo` 传给 Cube 服务做离散搬运。

`topKBaseOffset` 的计算在 `CalcParams` 里，按三种 layout 分别推出当前 (b, s1, n2) 对应索引行的首地址：

[sparse_flash_attention_enhance_kernel_mla.h:L685-L703](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance_kernel_mla.h#L685-L703)——同一处也按 `sparse_mode==3` 与否设置 `threshold`（L685-L689），并在 BSND / TND / BNSD 三种排布下计算 `topKBaseOffset`（L690-L703）。

反向算子消费同一份索引，其文档公式把「先 Gather 再反传」写得很直白：

[npu_sparse_flash_attention_grad_enhance.md:L19-L55](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/docs/npu_sparse_flash_attention_grad_enhance.md#L19-L55)——`selectedKey = Gather(key, topkIndices[i])`、`selectedValue = Gather(value, topkIndices[i])`，随后三阶段：①`dP = dO@V^T`、`dV = P^T@dO`；②`dS = P @ (dP - FlashSoftmaxGrad(dO,O))`；③`dQ = dS@K/√d`、`dK = dS^T@Q/√d`。注意 `FlashSoftmaxGrad(dO,O)` 即 u4-l4 讲过的行修正项 `rowsum(dO⊙O)`，反向同样不需要重建完整 softmax 分母——它直接用前向存下的 `softmax_sum` 重建 P。

#### 4.1.4 代码实践

**实践目标**：用 CPU 上的 PyTorch 复现「稀疏索引 + 因果阈值」如何裁剪 softmax 分母，验证你对分母范围的理解。

**操作步骤**（示例代码，可在任何有 PyTorch 的 CPU 环境运行）：

```python
import torch

torch.manual_seed(0)
B, S1, S2, D, K, BLK = 1, 4, 16, 8, 3, 4   # BLK 即 sparse_block_size
q = torch.randn(S1, D)
k = torch.randn(S2, D)
# 每行选 K=3 个块，每块 4 个 token；故意放一个越界块和 -1 填充
idx = torch.tensor([[1, 3, -1, -1],   # 有效:块1,块3
                    [0, 2, 3, -1],    # 块3 起点恰好贴住因果边界
                    [3, -1, -1, -1],
                    [1, 1, 2, -1]])   # 重复块：允许吗？看 assert
s1Len = S2                                # dense 场景 query 与 key 等长

def sparse_attn(q, k, idx, blk, s1Len):
    S1, D = q.shape
    out = torch.zeros(S1, D)
    smax = torch.full((S1,), -torch.inf)
    ssum = torch.zeros(S1)
    for t in range(S1):
        # 1) 因果阈值：sparse_mode=3，右下对齐下三角
        nextTokensPerBatch = s1Len - S1
        threshold = nextTokensPerBatch + t + 1
        # 2) 按 idx 行 Gather，块被 threshold 截断，-1 终止
        rows = []
        for b in idx[t].tolist():
            if b < 0:
                break
            begin = b * blk
            if begin >= threshold:
                continue                       # 整块越界，跳过
            rows.append(k[begin:min(begin + blk, threshold)])
        if not rows:
            continue                           # 分母为空 -> 输出保持 0
        ks = torch.cat(rows)                   # \tilde{K}
        s = (q[t] @ ks.T) / (D ** 0.5)
        p = torch.softmax(s, dim=-1)           # 分母只含被选 token
        out[t] = p @ ks
        smax[t] = s.max()
        ssum[t] = torch.exp(s - smax[t]).sum()
    return out, smax, ssum

out, smax, ssum = sparse_attn(q, k, idx, BLK, s1Len)
assert out.shape == (S1, D) and (out[0] == 0).all() or True
print(out)
```

**需要观察的现象**：
- 第 0 行（t=0，threshold=13）：块 1（token 4~7）和块 3（token 12~15 → 截成 12~12）参与，其余全被裁掉——softmax 分母只有 5 项，而不是 16 项。
- 把某行索引全填 `-1`，该行输出为 0、`softmax_max=-inf`，对应 kernel 里 `DealActSeqLenIsZero` 的全零输出路径。
- 重复块（第 3 行的 `[1,1,2,-1]`）在参考实现里会被算两次；对照 [npu_sparse_flash_attention_enhance.md:L37](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/docs/npu_sparse_flash_attention_enhance.md#L37) 文档并未禁止重复，索引生成方（lightning indexer）通常保证有序去重——**真实 kernel 对重复块的行为待本地验证**。

**预期结果**：输出 shape 正确；手工检查 t=0 行的分母项数等于被截断后拼接的 ks 行数。NPU 上与 `torch.ops.custom.npu_sparse_flash_attention_enhance` 的对比**待本地验证**（需要 A2/A3 环境与已安装的算子包）。

#### 4.1.5 小练习与答案

**练习 1**：`sparse_mode=3` 时，若某行 query 的所有索引块都指向 `blockBegin >= threshold` 的位置，kernel 走哪条路径？输出是什么？
**答案**：`CalcSinnerTopKBegin` 的 for 循环里 `blockBegin >= info.threshold` 的块被 `continue` 跳过，`sparseLen` 保持 0；当 `curTopKIdx == 0 && sparseLen == 0` 时调用 `DealActSeqLenIsZero`（[sparse_flash_attention_enhance_kernel_mla.h:L1023-L1025](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance_kernel_mla.h#L1023-L1025)），由 AIV 核把该行输出写成全零。

**练习 2**：为什么 SFA 前向输出没有 `softmax_out`（稠密 FA 的 P 矩阵输出），反向也照样能工作？
**答案**：反向文档公式显示它需要的是 P，而 P 可由 `Q@K^T` 重算并经 `softmax_max/softmax_sum` 归一化重建（u4-l4 的核心结论）；稀疏场景 P 本身只覆盖被选 token，落盘意义有限，所以前向只输出 `attention_out + softmax_max + softmax_sum` 三个张量。

**练习 3**：`sparse_block_size=1` 与 `sparse_block_size=64` 在「分母粒度」上有什么区别？
**答案**：=1 是 token 级稀疏，每个索引精确控制 1 个 token 进分母；=64 是 block 级稀疏，选一个索引就带上 64 个连续 token（因果边界处按 `threshold` 截断），粒度粗但索引张量小 K 倍。文档 [npu_sparse_flash_attention_enhance.md:L53-L55](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/docs/npu_sparse_flash_attention_enhance.md#L53-L55) 对应 Token-wise / Block-wise 两种场景说明。

### 4.2 模块二：前向 Tiling 与「模板化 tilingKey」

#### 4.2.1 概念说明

u4-l2/u4-l3 里稠密 FA 的 tilingKey 是 Host 侧用约 20 个参数手工组装的 64bit 位域，kernel 入口再用 `TILING_KEY_IS` 反查。SFA 前向换了一种更工程化的做法：**把 kernel 的分支变量声明成「模板参数」，tilingKey 由 CANN 的 `GET_TPL_TILING_KEY` 宏按声明自动编码**。好处是「kernel 有几个编译期分支」与「tilingKey 有几个合法值」由同一份声明文件约束，不会出现位域错位这种静默错误。

四个模板参数（见 [sparse_flash_attention_enhance_template_tiling_key.h:L31-L37](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance_template_tiling_key.h#L31-L37)）：

| 参数 | 含义 | 取值 |
| --- | --- | --- |
| `FLASH_DECODE` | 是否启用 S2 核间切分（FlashDecoding） | 0/1（当前 tiling 恒传 0） |
| `LAYOUT_T` | query 布局 | BSND=0 / TND=1 |
| `KV_LAYOUT_T` | key/value 布局 | BSND=0 / TND=1 / PA_BSND=2 |
| `TEMPLATE_MODE` | 稀疏取数模板 | C_TEMPLATE=0 / V_TEMPLATE=1 |

#### 4.2.2 核心流程

前向 tiling 是经典三段式（对比 u2-l3 的 aggregate_hidden，结构一致但规模更大）：

```
TilingSparseFlashAttentionEnhance(context)
 ├─ SFAInfoParser::Parse      # 取 shape/attr/平台信息 -> SFATilingInfo
 ├─ SFATilingCheck::Process   # dtype/layout/shape/存在性 全量校验
 └─ SFAMlaTiling::DoOpTiling  # 真正的切分
     ├─ GetPlatformInfo()     # AIV/AIC 核数、libapi workspace
     ├─ InitParams()          # 选 C/V 模板；空 tensor 兜底
     ├─ Split()               # s2 内切 512；mBaseSize=g
     ├─ FillTiling()          # 填 5 个 TilingData 子结构
     ├─ CalcBlockDim()        # AIC:AIV=1:2 -> blockDim
     ├─ GetWorkspaceSize()    # mm1/vec1/mm2/vec2 + V模板聚合缓冲
     └─ GenTilingKey()        # GET_TPL_TILING_KEY(0, layoutQ, layoutKV, isV)
```

其中 `InitParams` 的模板选择逻辑：`s2Size != 0 && sparseBlockSize <= 4` 用 V_TEMPLATE，否则 C_TEMPLATE——**块越小、离散度越高，越值得先做一次聚合**。

#### 4.2.3 源码精读

[sparse_flash_attention_enhance_tiling.cpp:L487-L502](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_host/sparse_flash_attention_enhance_tiling.cpp#L487-L502)——tiling 入口：Parse → Check → DoOpTiling 三步，任何一步失败直接 `GRAPH_FAILED`。

[sparse_flash_attention_enhance_tiling.cpp:L268-L280](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_host/sparse_flash_attention_enhance_tiling.cpp#L268-L280)——`InitParams`：按 `sparseBlockSize <= 4` 选 V/C 模板（L270-L274）；核数取 AIC 数（L276）；空 tensor 时把 `s2Size` 兜底成 1024 避免后续 softmax/matmul tiling 除零（`ZeroTensorProcess`，L256-L266）。

[sparse_flash_attention_enhance_tiling.cpp:L300-L320](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_host/sparse_flash_attention_enhance_tiling.cpp#L300-L320)——`CalcInnerSize`：S2 内切基准 512，`sInnerLoopTimes/sInnerSizeTail` 由实际 s2 推出，并按 32 字节基本块对齐。注释说明了 FlashDecode 场景下 256 阈值的均分特判（当前 `splitKVFlag_` 为 false，属预留）。

[sparse_flash_attention_enhance_tiling.cpp:L423-L454](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_host/sparse_flash_attention_enhance_tiling.cpp#L423-L454)——`GetWorkspaceSize`：按 `preLoadNum(=2) × 每核 × 各阶段结果大小` 累加；L448-L452 两行是 V_TEMPLATE 专属：`4 * 512 * (512+64) * 2` 的 KV 聚合缓冲（4 buf × s2Base 512 × (D512+Dr64) × sizeof(half)）与有效长度缓存，与 kernel 侧 `kvMergeGm_` 的绑定（见 4.3.3）一一对应——**workspace 的每一项都能在 kernel Init 里找到消费者**。

[sparse_flash_attention_enhance_tiling.cpp:L456-L464](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_host/sparse_flash_attention_enhance_tiling.cpp#L456-L464)——`CalcBlockDim`：AIC 用 `usedCoreNum_`，AIV 取两倍，经 `CalcTschBlockDim` 折算 blockDim。这是 AIC:AIV=1:2 混合核的标配（回顾 u4-l3）。

[sparse_flash_attention_enhance_tiling.cpp:L243-L254](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_host/sparse_flash_attention_enhance_tiling.cpp#L243-L254)——`GenTilingKey`：`GET_TPL_TILING_KEY(0U, layoutQuery, layoutKV, isVTemplate)`。注意它**不含 dtype**——fp16/bf16 由 kernel 入口的 `ORIG_DTYPE_*` 宏在编译期分流（见 4.3.3），这与稠密 FA 把 dtype 编进 key 的做法不同。

[sparse_flash_attention_enhance_template_tiling_key.h:L41-L69](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance_template_tiling_key.h#L41-L69)——`ASCENDC_TPL_SEL` 声明 4 组合法组合：{BSND query} × {BSND/PA_BSND kv} × C 模板、{BSND} × {BSND/PA} × V、{TND} × {TND/PA} × C、{TND} × {TND/PA} × V。结合 L45/L52 的选择列表可读出隐含约束：**query 为 BSND 时 kv 不能是 TND；PA_BSND 只出现在 kv 侧**——这与 tiling 校验 `GetKvLayout` 里「非 PA 时 layoutKV 必须等于 layoutQ」的运行期检查互为镜像。

注册（与 u2-l3 的 `IMPL_OP_OPTILING` 同款机制）：

[sparse_flash_attention_enhance_tiling.cpp:L2013-L2015](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_host/sparse_flash_attention_enhance_tiling.cpp#L2013-L2015)——`.Tiling(TilingSparseFlashAttentionEnhance).TilingParse<...>(...)` 把 tiling 函数与编译期平台解析钩子登记进 CANN 注册表。

TilingData 结构分 5 个子结构（baseParams / splitKVParams / singleCoreParams / singleCoreTensorSize / innerSplitParams），定义在 [sparse_flash_attention_enhance_tiling.h:L146-L201](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_host/sparse_flash_attention_enhance_tiling.h#L146-L201)，其中 `baseParams` 携带 `sparseBlockSize/sparseBlockCount`（L162-L163）——稀疏参数完整下发到设备侧。

#### 4.2.4 代码实践

**实践目标**：整理「dense FA（u4-l3）vs sparse FA 前向 vs sparse FA 反向」的 tilingKey 编码差异表，并用源码行号佐证。

**操作步骤**（源码阅读型实践）：
1. 打开 u4-l3 讲过的 `flash_attention_score_enhance_tiling.cpp`（稠密 FA），找到它组装 64bit tilingKey 的 `SetTilingKey` 调用链，数一数参与编码的参数个数（layout、dtype、sparseMode、Bmm1Format 等约 20 个）。
2. 打开本讲的 [sparse_flash_attention_enhance_tiling.cpp:L243-L254](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_host/sparse_flash_attention_enhance_tiling.cpp#L243-L254) 与 [sparse_flash_attention_enhance_template_tiling_key.h:L41-L69](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance_template_tiling_key.h#L41-L69)，列出合法组合。
3. 打开反向的 [sparse_flash_attention_grad_enhance_tiling_bs1_basic.cpp:L240-L264](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/op_host/sparse_flash_attention_grad_enhance_tiling_bs1_basic.cpp#L240-L264) 与 kernel 入口 [sparse_flash_attention_grad_enhance.cpp:L80-L128](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/op_kernel/sparse_flash_attention_grad_enhance.cpp#L80-L128)，核对十进制编码。

**参考产出表**（读者应能自行推出并核对该表）：

| 算子 | 编码方式 | 合法值集合 | 决定因子 |
| --- | --- | --- | --- |
| dense FA 前向（u4-l3） | 64bit 位域手工组装 | 约 20 个位段组合 | layout、dtype、sparseMode、Bmm1Format… |
| SFA 前向 | `GET_TPL_TILING_KEY` 模板参数自动编码 | 4 组组合 × dtype 编译期分流 | FLASH_DECODE(恒0)、LAYOUT_T、KV_LAYOUT_T、TEMPLATE_MODE |
| SFA 反向 | 十进制逐位乘 10 累加 | 10000~11111 共 16 个 | 1{atten}{rope}{BSND}{deterministic} |

反向编码推演：初值 10 → atten?+1 → ×10 → rope?+1 → ×10 → bsnd?+1 → ×10 → det?+1，故 `10000`=无任何开关、`11111`=全开，kernel 入口逐一 `TILING_KEY_IS` 匹配。注意 [sparse_flash_attention_grad_enhance_tiling_bs1_basic.cpp:L499-L518](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/op_host/sparse_flash_attention_grad_enhance_tiling_bs1_basic.cpp#L499-L518) 里 rope 缺失会直接报错，所以 6 个「无 rope」的 key（如 10000、11000）当前实际不可达，属防御性分支。

**需要观察的现象**：三种编码方式的「非法值防护」位置不同——稠密 FA 靠位段本身合法，SFA 前向靠 `ASCENDC_TPL_SEL` 白名单（`GET_TPL_TILING_KEY` 内部校验），SFA 反向靠 kernel 入口 16 连 `if` 都不命中则静默返回。

**预期结果**：表格 3 行填完，且每个 key 都能指到源码行号。无需 NPU 环境。

#### 4.2.5 小练习与答案

**练习 1**：`layout_query='TND', layout_kv='BSND'` 的组合为什么非法？两处源码证据是什么？
**答案**：tiling 校验 [sparse_flash_attention_enhance_tiling.cpp:L1728-L1731](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_host/sparse_flash_attention_enhance_tiling.cpp#L1728-L1731) 明确「非 PA_BSND 时 layoutKV 必须与 layoutQ 相同」；模板白名单 [sparse_flash_attention_enhance_template_tiling_key.h:L49-L54](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance_template_tiling_key.h#L49-L54) 的 TND 组合里 KV 只允许 TND/PA_BSND。文档约束说明（[npu_sparse_flash_attention_enhance.md:L86](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/docs/npu_sparse_flash_attention_enhance.md#L86)）是第三处证据。

**练习 2**：`GET_TPL_TILING_KEY` 的第一个参数 `0U` 是什么？为什么不是 1？
**答案**：那是 `FLASH_DECODE` 模板参数的值。tiling 侧 `InitParams` 从不置位 `splitKVFlag_`，`FillTilingSplitKVMla` 里 `!splitKVFlag_` 时把 `splitKVParams.s2` 置 0（[sparse_flash_attention_enhance_tiling.cpp:L372-L374](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_host/sparse_flash_attention_enhance_tiling.cpp#L372-L374)），即 FlashDecoding 路径已实现（`IsFlashDecode`、FD workspace 计算）但当前未启用，属「已备而未用」。

### 4.3 模块三：前向 Kernel——kernel_mla 与 cube/vector 双 service

#### 4.3.1 概念说明

前向 kernel 的骨架与 u4-l3 的稠密 FA 相似（AIC:AIV=1:2 混合核、三级流水、SoftmaxFlashV2），新东西是**稀疏取数被拆成两个可替换的策略类**：

- `SFAMatmulService`（Cube 侧）：负责 mm1（Q@K^T 打分）与 mm2（P@V），并且持有 `topKGm`——C_TEMPLATE 模式下它按索引「边算边离散搬 K」。
- `SFAVectorService`（Vector 侧）：负责 online softmax（ProcessVec1L）、输出整理（ProcessVec2L），并且 V_TEMPLATE 模式下先执行 `MergeKv`——把离散选中的 K/V 聚合进 `kvMergeGm_` 连续缓冲。

两条路线对比：

| | C_TEMPLATE | V_TEMPLATE |
| --- | --- | --- |
| 谁搬 K | Cube 核（CopyInMm1BToL1 / DataCopyPA） | Vector 核（MergeKv → kvMergeGm_） |
| 搬运方式 | 按 `idInTopK × sparseBlockSize + offset` 逐段搬进 L1 | 先聚合成 512 行连续区，Cube 整块读 |
| 适用 | sparseBlockSize > 4 或空 tensor | sparseBlockSize ≤ 4（token 级细粒度） |
| 额外 workspace | 无 | `4*512*(512+64)*2*核数` 聚合缓冲 + 有效长度缓存 |

#### 4.3.2 核心流程

一个 (b, n2, s1 块) 单元的执行（`ProcessBalance` 外层循环）：

```
for bN2 ∈ [bN2Start, bN2End]:            # 本核分到的 batch×kvHead 片段
    GetActualSeqLen; GetPreNextTokensLeftUp   # 因果参数
    for gS1 ∈ [gS1Start, gS1End]:         # m 方向按 mBaseSize(=g) 步进
        GetSparseActualSeqLen             # min(K*BLK, threshold)
        s2SplitNum = ceil(curActualSeqLen / s2BaseSize)
        for s2Loop ∈ [0, s2LoopTimes + 2):    # +2 是流水线排空的空转迭代
            PreloadPipeline(...)          # 三槽环形缓冲:
                extraInfo0: 本轮   V:MergeKv / C:WaitVec→ComputeMm1
                extraInfo2: 上轮   V:ProcessVec1L(softmax) / C:ComputeMm2
                extraInfo1: 上上轮 V:ProcessVec2L(写出 attention_out)
```

注意与 u4-l3 稠密 FA 的 `extraInfo[3]` 三槽一致，但这里的流水线粒度是「稀疏段」（每段最多 s2BaseSize=512 个被选 token），不是连续 S2。

#### 4.3.3 源码精读

kernel 入口（对比 u2-l4 的 aggregate_hidden 入口，分支依据从 `TILING_KEY_IS` 换成了**模板参数**）：

[sparse_flash_attention_enhance.cpp:L46-L64](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance.cpp#L46-L64)——`template <int FLASH_DECODE, int LAYOUT_T, int KV_LAYOUT_T, int TEMPLATE_MODE>` 的 `__global__ __aicore__` 入口，声明 14 个 GM 参数（顺序即 def 声明顺序 + workspace + tiling）；L62 `KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_MIX_AIC_1_2)` 声明 1:2 混合核。

[sparse_flash_attention_enhance.cpp:L67-L88](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance.cpp#L67-L88)——dtype 分流：`ORIG_DTYPE_QUERY == DT_FLOAT16` 时实例化 `SFAType<half,...>`，否则 `bfloat16_t`。这就是 4.2 说的「dtype 不进 tilingKey」的另一半机制。

[sparse_flash_attention_enhance.cpp:L22-L44](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance.cpp#L22-L44)——`SFA_OP_IMPL` 宏：`GET_TILING_DATA_WITH_STRUCT` 解包 TilingData → 实例化 `SparseFlashAttentionEnhanceMla<SFAType<...>>` → `Init(...) + Process()`。与 aggregate_hidden 的入口三件事（选分支/解包/实例化）完全同构。

主体类与角色划分：

[sparse_flash_attention_enhance_kernel_mla.h:L53-L84](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance_kernel_mla.h#L53-L84)——`SparseFlashAttentionEnhanceMla` 模板类声明：中间计算统一 `float`（高精度模式，L56），成员 `SFAMatmulService matmulService; SFAVectorService vectorService;`（L141-L142）即 Cube/Vector 两服务。类内常量 L108-L112 写死 `kvHeadNum=1`、`headDim=512`、`headDimRope=64`——MLA 专用 kernel 的标志。

[sparse_flash_attention_enhance_kernel_mla.h:L416-L422](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance_kernel_mla.h#L416-L422)——`Init` 开头按 AIV/AIC 归一化核号：AIV 的 `GetBlockIdx()/2` 得到所属 aiCoreIdx（两个 Vector 核配一个 Cube 核），AIC 直接用自身序号。这是 1:2 混合核下「同一逻辑核」的编号约定。

[sparse_flash_attention_enhance_kernel_mla.h:L457-L485](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance_kernel_mla.h#L457-L485)——workspace 内存排布（L458-L459 的注释画出 `|Q--|mm1Res(存S)|vec1Res(存A1,A2)|mm2Res(存O)|vec2ResGm` 及按核交错的布局）；`TEMPLATE_MODE == V_TEMPLATE` 时额外绑定 `kvMergeGm_`（每核 `512*576*4` 元素、4 份缓冲）与 `kvValidSizeGm_`（L479-L485），对应 tiling workspace 的最后两项。

[sparse_flash_attention_enhance_kernel_mla.h:L495-L518](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance_kernel_mla.h#L495-L518)——按核类型初始化两服务：AIV 侧 `vectorService.InitVec0/1/2GlobalTensor(...)`（V 模式的 Vec0 负责 MergeKv 所需的 kvMerge/kvValidSize/kRope/key/blockTable 绑定）；AIC 侧 `matmulService.InitMm1/Mm2GlobalTensor` 与 `InitPageAttentionInfo(kvMergeGm_, blockTableGm, topKGm, ...)`——`topKGm`（稀疏索引）同时交给两个服务。

主循环与流水线：

[sparse_flash_attention_enhance_kernel_mla.h:L785-L840](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance_kernel_mla.h#L785-L840)——`ProcessBalance`：三层循环（bN2 → gS1 → s2），L810-L811 在每个 gS1 块开头调 `GetSparseActualSeqLen`（注释：TopK 值 sparse 完后的 ActualSeqLengthKV），L817-L818 用它算 `s2SplitNum`，L830-L835 的 s2 循环带 `extraLoop=2` 个排空迭代并维护 `curTopKIdx / curOffsetInSparseBlock` 两个跨迭代的稀疏游标。

[sparse_flash_attention_enhance_kernel_mla.h:L851-L898](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance_kernel_mla.h#L851-L898)——`PreloadPipeline`：`CalcParams + CalcSinnerTopKBegin` 填本轮 RunInfo；随后 V 模式下 AIV 先 `MergeKv` 并置 `syncV0C1` 通知 Cube（L874-L878），AIC `WaitFlag(syncV0C1)` 后 `ComputeMm1`（L868-L872）；上一轮做 `ProcessVec1L`（AIV，softmax）/`ComputeMm2`（AIC）；上上轮 AIV `ProcessVec2L` 写出。三级流水与 u4-l3 的「发射 bmm1(i)、softmax(i-1)、bmm2(i-2)」同构，但第一级多了「聚合离散 KV」这一步。

Cube 服务的离散搬运（C_TEMPLATE 的核心）：

[sparse_flash_attention_enhance_service_cube_mla.h:L590-L628](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance_service_cube_mla.h#L590-L628)——`CalcTopKBlockInfo`：当前块剩余量不够就沿索引数组找下一个块（跳过 `-1` 终止、跳过 `blockBegin >= threshold`），更新 `curTopKIdx / idInTopK / curOffsetInSparseBlock / copyRowCnt` 四元组。这是「把稀疏索引流翻译成连续搬运段」的状态机。

[sparse_flash_attention_enhance_service_cube_mla.h:L739-L805](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance_service_cube_mla.h#L739-L805)——`ComputeMm1` 内 `while (copyFinishRowCnt < nL1Size)` 搬运循环：PA 场景用 `DataCopyPA` 按 blockTable + 稀索引定位（L746-L777）；非 PA 场景直接算 GM 偏移 `keyOffset += (idInTopK * sparseBlockSize + curOffsetInSparseBlock) * kvHeadNum * headDim`（L781-L791）再 `CopyInMm1BToL1`。**「离散访存」三个字在这两段落了地：每次搬的行数由 `copyRowCnt` 决定，段与段之间 GM 地址不连续。**

Vector 服务的聚合与 softmax：

[sparse_flash_attention_enhance_service_vector_mla.h:L894-L945](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance_service_vector_mla.h#L894-L945)——`MergeKv`：按 `2*sparseBlockSize` 步长扫索引（`GetRealS2Idx` 把稀疏位置映射回真实 s2 索引，无效返回负值即停止），`CopyInKv` 聚合进 UB 再 `CopyOutMrgeResult` 写到 `kvMergeGm_` 的 `loop%4` 号缓冲；两个 subBlock（GetSubBlockIdx）各负责一半。L947-L974 处理尾部不足部分补零对齐，L977-L987 把本轮有效长度写入 `kvValidSizeGm_` 供 Cube 侧读取。

[sparse_flash_attention_enhance_service_vector_mla.h:L569-L611](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance_service_vector_mla.h#L569-L611)——`SoftmaxFlashV2Compute`：调用 `SoftmaxFlashV2<T, true, true, false, false, SFA_SOFTMAX_FLASHV2_CFG_WITHOUT_BRC>`（L595），配置结构体定义在 [sparse_flash_attention_enhance_common.h:L24-L25](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance_common.h#L24-L25)（isCheckTiling=false、输出不做广播）。稀疏段之间的 max/sum 滚动合并逻辑与稠密 FA 完全一致——**稀疏化只改「算哪些列」，不改 online softmax 本身**。

#### 4.3.4 代码实践

**实践目标**：画出前向 kernel 的「tilingKey 分发表 + 主流水线调用图」，并标注稀疏游标的流转。

**操作步骤**（源码阅读型实践）：
1. 从 [sparse_flash_attention_enhance_template_tiling_key.h:L41-L69](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance_template_tiling_key.h#L41-L69) 的 4 组 `ASCENDC_TPL_ARGS_SEL` 出发，结合入口的 fp16/bf16 两分支（[sparse_flash_attention_enhance.cpp:L67-L88](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance.cpp#L67-L88)），列出实际会实例化的模板个数（4 组 × 2 dtype = 8 个 `SparseFlashAttentionEnhanceMla` 实例）。
2. 跟踪 `curTopKIdx / curOffsetInSparseBlock` 两个变量的完整生命周期：声明（[L828-L829](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance_kernel_mla.h#L828-L829)）→ 传入 `PreloadPipeline` → `CalcSinnerTopKBegin` 推进 → 写进 `RunInfo.curTopKIdx/curOffsetInSparseBlock` → Cube 侧 `CalcTopKBlockInfo` 继续消费。
3. 仿照 u4-l3 的实践，为 V_TEMPLATE 的一轮写伪代码：`MergeKv(i) → ComputeMm1(i) → ProcessVec1L(i-1)/ComputeMm2(i-1) → ProcessVec2L(i-2)`。

**需要观察的现象**：V_TEMPLATE 下 AIC 在 `ComputeMm1` 前必须 `CrossCoreWaitFlag(syncV0C1)`（等 Vector 聚合完），C_TEMPLATE 下则等的是 `syncV0C1` 以外的既有同步——两种模板的核间同步依赖不同。

**预期结果**：得到一张 8 实例的分发表与一张标注稀疏游标的数据流图。无需 NPU 环境。

#### 4.3.5 小练习与答案

**练习 1**：`InitCalcParamsEach`（[L525-L600](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance_kernel_mla.h#L525-L600)）做的是 Host 侧 tiling 哪项工作的 Device 侧版本？
**答案**：按 `totalBaseNum = Σ actBatchS1`（各 batch 有效 query 数之和）把 B×N2×S1 的任务均分到各核，得到本核的 `[bN2Start, bN2End] × [gS1Start, gS1End]` 区间——相当于 Host 侧 `CalcBlockDim` 只定核数、真正的任务切分在设备侧每核自行完成（这与 aggregate_hidden 的「Host 算好三 Cnt、Device 取余反解」是同一问题的两种解法）。

**练习 2**：为什么 V_TEMPLATE 的聚合缓冲大小是 `512 * (512+64)` 而不是 `512 * 576 * 2`（half 元素）？
**答案**：聚合区按「nope 部分 512 行 × headDim 512」与「rope 部分 512 行 × headDimRope 64」两块分开摆放（见 `MergeKv` 里 `kvMergeGm_[...*512*576 + s2GmOffset*headDim]` 与 `+ 512*headDim + s2GmOffset*headDimRope` 两个写点，[L961-L968](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance_service_vector_mla.h#L961-L968)），总量 `512*(512+64)=512*576` 个 KV_T 元素/份缓冲；`4` 是缓冲份数（`loop % MERGE_CACHE_GM_BUF_NUM`），乘 `sizeof(half)=2` 才是字节数——tiling 侧 L450 的注释逐项对应。

**练习 3**：kernel 里 `headDim=512 / headDimRope=64` 写死，若产品需要 D=192 的 MLA 变体会怎样？
**答案**：`ComputeMm1` 里 `kSize=576 / kL1Size=288 / kL0Size=96` 的切分注释明确写着「mla 专用 这里不考虑 d 泛化」（[sparse_flash_attention_enhance_service_cube_mla.h:L644-L649](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance_service_cube_mla.h#L644-L649)），且 tiling 的 `CalcUbBmm` 以 `headDimAlign` 参与预算——D 泛化需要同时改 kernel 常量、L1/L0 切分与 tiling 的 UB 预算三处，是一項结构性改动而非改一个常量。

### 4.4 模块四：反向 Tiling——tiling_common 校验与 bs1_basic 责任链

#### 4.4.1 概念说明

反向 tiling 的组织方式和 u4-l4 的稠密 FA 反向一样走 **tiling_base 责任链**（u3-l3）：注册入口不直接实现切分，而是交给 `TilingRegistry` 按优先级逐个尝试已注册的模板。当前只注册了一个 `SparseFlashAttentionGradEnhanceBasicTiling`（优先级 1），即 **bs1_basic** 模板——名字里的 bs1 指「以 S1 行（batch×s1 或 t1）为最小分核单位」。

与前向 tiling 相比有三个结构差异：

1. **参数面更大**：反向要吃前向的全部输出（`out`、`softmax_max`、`softmax_sum`）加上 `d_out`，共 12 输入 5 输出。
2. **校验拆成两层**：`tiling_common.cpp` 提供 shape/dtype 公共校验函数（softmax 中间量的 [N2,T1,G] 约束），`bs1_basic.cpp` 的 `GetBaseShapeInfo` 再做算子特有约束（block size 白名单、topk 上限、n1 白名单）。
3. **workspace 里出现 Gather/Scatter 专区**：反向要先聚合 K/V 再算、再把 dk/dv 散射回去，workspace 规划围绕这两个动作展开。

#### 4.4.2 核心流程

```
IMPL_OP_OPTILING(SparseFlashAttentionGradEnhance)
  └─ TilingSparseFlashAttentionGradEnhance
      └─ TilingRegistry::DoTilingImpl        # 责任链调度
          └─ SparseFlashAttentionGradEnhanceBasicTiling (优先级 1, IsCapable()=true)
              ├─ GetShapeAttrsInfo -> GetBaseShapeInfo   # 12 输入 shape + 8 属性全量校验
              ├─ GetPlatformInfo                           # 核数/UB/L1/L0/L2
              ├─ DoOpTiling = DoSftTiling + DoBlockTiling + DoCastTiling
              ├─ DoLibApiTiling                            # SoftMaxTilingFunc 等高阶 API
              ├─ GetWorkspaceSize                          # gather/scatter/dq/dk/dv 分区
              └─ PostTiling + GetTilingKey(10000~11111)
```

#### 4.4.3 源码精读

[sparse_flash_attention_grad_enhance_tiling.cpp:L27-L30](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/op_host/sparse_flash_attention_grad_enhance_tiling.cpp#L27-L30)——注册入口只有一行：`TilingRegistry::GetInstance().DoTilingImpl(context)`。对比前向 tiling 入口的「三段直调」，这里是 u3-l3 责任链模式的教科书样本。

[sparse_flash_attention_grad_enhance_tiling.cpp:L32-L67](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/op_host/sparse_flash_attention_grad_enhance_tiling.cpp#L32-L67)——`TilingPrepareForSparseFlashAttentionGradEnhance`：TilingParse 钩子在编译期把 AIV/AIC 核数、UB/L1/L0A/L0B/L0C/L2 大小写进 `SparseFlashAttentionGradEnhanceCompileInfo`，供运行期（拿不到 platformInfo 时）兜底读取（见 bs1_basic 的 `GetPlatformInfo` L63-L75）。

公共校验：

[sparse_flash_attention_grad_enhance_tiling_common.cpp:L21-L48](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/op_host/sparse_flash_attention_grad_enhance_tiling_common.cpp#L21-L48)——`CheckTndSoftmaxMaxShape`：TND 布局下 `softmax_max` 必须是 3 维且等于 `[N2, T1, G]`（`G=N1/N2`）；`CheckTndSoftmaxSumShape`（L50-L77）同款。这是 u4-l1「前向中间量被反向消费」在 shape 层的互锁。

[sparse_flash_attention_grad_enhance_tiling_common.cpp:L162-L210](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/op_host/sparse_flash_attention_grad_enhance_tiling_common.cpp#L162-L210)——`CheckTndShapeValid`（串联 max/sum/attentionIn 三项 shape 检查）与 `CheckDtypeValid`（softmax 必须 fp32、attentionIn 与 query 同 dtype）。`CheckAttentionInShape`（L79-L121）还校验 attentionIn 的 D 维必须等于 value 的 D 维。

bs1_basic 的算子特有约束：

[sparse_flash_attention_grad_enhance_tiling_bs1_basic.cpp:L421-L462](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/op_host/sparse_flash_attention_grad_enhance_tiling_bs1_basic.cpp#L421-L462)——`selected_block_count` 取自 `indicesShape` 最后一维（L432，与输入 shape 直接耦合，**不是属性**）；`selected_block_size` 只允许 1/8/16/32/64（L436-L447）；token 级（=1）时 topk ≤ 8192，块级时 `count*size ≤ 16384`（L449-L462）。

[sparse_flash_attention_grad_enhance_tiling_bs1_basic.cpp:L464-L475](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/op_host/sparse_flash_attention_grad_enhance_tiling_bs1_basic.cpp#L464-L475)——`sparse_mode` 只认 0（无 mask）或 3（rightDownCausal）：mode 0 时 `attenEnable=false`，mode 3 时 true——这个 bool 直接进 tilingKey 的万位。

[sparse_flash_attention_grad_enhance_tiling_bs1_basic.cpp:L499-L518](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/op_host/sparse_flash_attention_grad_enhance_tiling_bs1_basic.cpp#L499-L518)——rope 输入当前是**硬性要求**：qRope/kRope 任一为空直接 `GRAPH_FAILED`（尽管 def 里它们是 OPTIONAL——def 声明能力、tiling 收紧约束，分工与 u2-l2 讲过的一致）。

[sparse_flash_attention_grad_enhance_tiling_bs1_basic.cpp:L547-L556](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/op_host/sparse_flash_attention_grad_enhance_tiling_bs1_basic.cpp#L547-L556)——MQA 白名单：`n2 必须为 1` 且 `n1 ∈ {1,2,3,4,6,8,12,16,24,32,48,64,96,128}`（实现方式是「n1%3==0 时除以 3 后须为 2 的幂」的位运算判断）。

切分与 tilingKey：

[sparse_flash_attention_grad_enhance_tiling_bs1_basic.cpp:L108-L136](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/op_host/sparse_flash_attention_grad_enhance_tiling_bs1_basic.cpp#L108-L136)——`DoOpTiling`：`singleM = G`（一个 query 头组作为 M 基本块）、`singleN = 128`（PER_LOOP_BLOCK_SIZE，一轮处理的被选 token 数），随后 Sft/Block/Cast 三段切分。**「bs1」的含义就在这里：M 方向一次只处理一个 s1 行的 G 个头。**

[sparse_flash_attention_grad_enhance_tiling_bs1_basic.cpp:L266-L283](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/op_host/sparse_flash_attention_grad_enhance_tiling_bs1_basic.cpp#L266-L283)——`DoBlockTiling`：`nNums = TND ? t1 : b*s1`（S1 行总数），与 AIC 核数取小得 `usedCoreNum`，再算 `formerCoreProcessNNum`（前多少核多处理一行）——**按 S1 行一维分核**，与前向按 B·N2·S1 块均衡分核（4.3.3 练习 1）形成对比。

[sparse_flash_attention_grad_enhance_tiling_bs1_basic.cpp:L154-L214](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/op_host/sparse_flash_attention_grad_enhance_tiling_bs1_basic.cpp#L154-L214)——`GetWorkspaceSize`：L179-L187 规划 Gather 区（`selectedK/VWorkspaceLen`，K 区 4 份缓冲、V 区乒乓）；L172-L173 mm12 乒乓区；L198-L199 ScatterAdd 累加区（`24 * 2 * count * size * (dAlign+d2Align) * fp32`——「每个 s1 做完做 scatter add 累加，workspace 开 DB」）；L207-L211 把 dq/dk/dv workspace 偏移写进 `postTilingData`。

[sparse_flash_attention_grad_enhance_tiling_bs1_basic.cpp:L240-L264](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/op_host/sparse_flash_attention_grad_enhance_tiling_bs1_basic.cpp#L240-L264)——`GetTilingKey` 十进制编码（4.2.4 已推演）。

[sparse_flash_attention_grad_enhance_tiling_bs1_basic.cpp:L608](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/op_host/sparse_flash_attention_grad_enhance_tiling_bs1_basic.cpp#L608)——`REGISTER_OPS_TILING_TEMPLATE(SparseFlashAttentionGradEnhance, SparseFlashAttentionGradEnhanceBasicTiling, 1)`：u3-l3 的注册宏，优先级 1。将来若加「多 S1 批量模板」，只需再注册一个更高优先级实现并在 `IsCapable` 里做能力自判。

#### 4.4.4 代码实践

**实践目标**：为稀疏索引输入整理一份「shape 约束卡」，并指出前反向约束的差异点。

**操作步骤**（文档 + 源码交叉型实践）：
1. 从 [npu_sparse_flash_attention_enhance.md:L37](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/docs/npu_sparse_flash_attention_enhance.md#L37) 抄出前向约束：BSND `[B, Q_S, KV_N, sparse_size]` / TND `[Q_T, KV_N, sparse_size]`，int32，有效值在前半部分。
2. 从反向文档 [npu_sparse_flash_attention_grad_enhance.md:L71](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/docs/npu_sparse_flash_attention_grad_enhance.md#L71) 与源码 [sparse_flash_attention_grad_enhance_tiling_bs1_basic.cpp:L421-L462](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/op_host/sparse_flash_attention_grad_enhance_tiling_bs1_basic.cpp#L421-L462) 抄反向约束。
3. 制作对照表并标注「谁在源码里强制」。

**参考产出**：

| 约束项 | 前向 | 反向 | 强制位置 |
| --- | --- | --- | --- |
| dtype | int32 | int32（文档表格写 INT64，与 def 的 DT_INT32 矛盾，以 def 为准） | 前向 def L38-L42 / 反向 def L45-L50 |
| shape（BSND） | [B,S1,N2,K] | [B,S1,N2,K] | 文档 + kernel topKBaseOffset 公式 |
| shape（TND） | [T1,N2,K] | [T1,N2,K] | 同上 |
| K（count） | >0 | >0 且 block=1 时 ≤8192、否则 count×size ≤16384 | 反向 tiling L449-L462 |
| block size | [1,128] 且 2 的幂 | 1/8/16/32/64 | 前向文档 L53 / 反向 tiling L436-L447 |
| 填充语义 | -1 在后半部分 | 同 | 前向 kernel CalcSinnerTopKBegin L957/L987 |

**需要观察的现象**：反向文档参数表把 sparse_indices 的数据类型写成 INT64，而 def 与 tiling 都按 int32 处理（`topkIndices.to(torch.int32)` 的示例也印证）——**接口契约以 def + 源码为准，文档表格存在笔误**（这是 u2-l1/u4-l1 反复强调的读文档方法论）。

**预期结果**：一张 6 行约束表，每行有源码行号佐证。无需 NPU 环境。

#### 4.4.5 小练习与答案

**练习 1**：反向 `IsCapable()` 恒返回 true（[L102-L106](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/op_host/sparse_flash_attention_grad_enhance_tiling_bs1_basic.cpp#L102-L106)），那注册成责任链还有什么意义？
**答案**：为扩展留位。u4-l4 的稠密 FA 反向注册了 9 个模板（优先级 10~16000），SFA 反向当前只有一种实现所以恒命中；但入口已经走 `TilingRegistry`，后续增加「大 S1 批量模板」「确定性专用模板」时无需改注册入口，符合 u3-l3 讲的「开闭」设计。

**练习 2**：`GetWorkspaceSize` 里 `workspaces[0] += 24 * PING_PONG * count * size * (dAlign + d2Align) * B32` 的 24 是什么？
**答案**：ScatterAdd 缓冲按「24 份 × 乒乓 × 块数 × 块大小 × (D+D2 对齐) × fp32」开空间——24 是每份 ScatterAdd 任务可覆盖的 s1 行分组数上限（与 `singleM=G、N1≤128、G≤128` 的规格配合），注释「每个 s1 做完，做 scatter add 累加，workspace 开 DB」说明这是 dk/dv 散射累加的滚动缓冲。其精确语义**待确认**（源码无进一步注释），读者可结合 `ScatterAddByS1`（kernel 侧 L470-L497）的调用频率反推。

### 4.5 模块五：反向 Kernel——Gather → cube1~5 → ScatterAdd

#### 4.5.1 概念说明

反向 kernel 的计算骨架与 u4-l4 稠密 FA 反向相同（5 次矩阵乘 + softmax 反传），但**组织方式完全不同**：稠密反向按「S1×S2 块」组织任务；SFA 反向按「单个 s1 行（bs1）」组织——每个核拿到若干 s1 行，对每行先 Gather 它选中的 K/V 段，算完五行公式后把 dk/dv Scatter 回去。核心动机：稀疏场景不同 s1 行选中的 token 集合不同，无法共享一个大 S2 块；而 dk/dv 的累加天然是「多对一散射」，必须显式处理。

五个矩阵乘被拆进 `basic_modules/cube_modules/cube1~5.h`，每个文件的头部注释直接写明公式与 L0 缓冲布局：

| 模块 | 公式 | 输出 |
| --- | --- | --- |
| cube1 | s = q * k^T | 重算打分矩阵 |
| cube2 | dp = dy * v^T | dP |
| cube3 | dq = ds * k | dQ |
| cube4 | dk = s^T * q | dK |
| cube5 | dv = p^T * dy | dV |

（dS 由 Vector 侧按 `dS = P ⊙ (dP − rowsum(dO⊙O))` 算出，不是矩阵乘——与 u4-l4 的结论一致。）

#### 4.5.2 核心流程

```
kernel 入口 (16 个 TILING_KEY_IS 分发, AIC:AIV=1:2)
 └─ SelectedAttentionGradBasic::Process
     ├─ AIC 分支:
     │   for i < processBS1ByCore:            # 本核的 s1 行(跨核步进)
     │     GetTndSeqLen; GetActualSelCount    # 该行有效索引块数
     │     for blkCntOffset += selectedCountOffset:   # 每轮 128 个被选 token
     │       CubeCompute: cube12(打分+dP) -> cube345(dq/dk/dv)
     │       CrossCoreSetFlag(SCATTER_SYNC_FLAG)      # 通知 AIV 可散射
     └─ AIV 分支:
         for 同样的行循环:
           VecCompute: GatherKV(聚合 K/V 到 workspace) + softmax 反传 + dS
           ScatterAddByS1: 把 dk/dv 按 topk 索引累加回 GM
         SyncAll
         SparseFlashAttentionGradEnhancePost(opCast): fp32 -> fp16/bf16 收尾转换
```

#### 4.5.3 源码精读

[sparse_flash_attention_grad_enhance.cpp:L58-L78](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/op_kernel/sparse_flash_attention_grad_enhance.cpp#L58-L78)——kernel 入口：19 个 GM 参数（12 输入 + 5 输出 + workspace + tiling），L78 声明 1:2 混合核。注意**没有 workspace 之前的 user 前缀参数**，`GetUserWorkspace` 在宏内取（L26）。

[sparse_flash_attention_grad_enhance.cpp:L24-L56](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/op_kernel/sparse_flash_attention_grad_enhance.cpp#L24-L56)——`INVOKE_SELECTED_ATTENTION_BASIC_IMPL` 宏：把 5 个布尔模板参（ATTEN_ENABLE / HAS_ROPE / IS_BSND / DETERMINISTIC_ENABLE）与输入类型一起绑进 `SFAG_TYPE`，实例化 `SelectedAttentionGradBasic` 后 `Process(...)`。

[sparse_flash_attention_grad_enhance.cpp:L80-L128](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/op_kernel/sparse_flash_attention_grad_enhance.cpp#L80-L128)——fp16 分支的 16 连 `TILING_KEY_IS(10000..11111)`（L131-L181 是 bf16 的同款）。与前向「模板参数分发」不同，这里回到 u2-l4 讲过的显式 key 匹配。

主体：

[sparse_flash_attention_grad_enhance_bs1_basic.h:L218-L327](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/op_kernel/sparse_flash_attention_grad_enhance_bs1_basic.h#L218-L327)——`Process` 的 AIC 半边：L266-L268 行循环 `t1Index = cubeBlockIdx + usedCoreNum * i`（跨步分配 s1 行）；L272 `GetActualSelCount` 算该行有效块数；L273-L285 块循环内 `UpdateGmOffset + CubeCompute`，`changeS1` 时做核间同步（确定性模式下还有专门的 `SCATTER_CUBE_SYNC_FLAG` 握手，L277-L283）；L308-L317 流水排空（把最后一轮 runInfo 补算完）。

[sparse_flash_attention_grad_enhance_bs1_basic.h:L330-L443](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/op_kernel/sparse_flash_attention_grad_enhance_bs1_basic.h#L330-L443)——AIV 半边：同款行循环里 L364 `VecCompute`（内含 GatherKV 与 softmax 反传）；L401-L417 尾部先 `ScatterAddByS1` 再补最后一轮 `vecOp.Process`；L436 `SyncAll` 后 L439-L442 实例化 `SparseFlashAttentionGradEnhancePost` 做**输出 dtype 收尾转换**（fp32 workspace → fp16/bf16 输出，`Post` 类在 `sparse_flash_attention_grad_enhance_post.h`，并按 `castUsedCoreNum` 用满 AIV 核）。

[sparse_flash_attention_grad_enhance_bs1_basic.h:L446-L465](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/op_kernel/sparse_flash_attention_grad_enhance_bs1_basic.h#L446-L465)——`CubeCompute`：先 `CrossCoreWaitFlag(CUBE_WAIT_VEC_GATHER_*)` **等 Vector 把 K/V 聚合好**，再 `cube12Process`（打分+dP），乒乓切换后补 `cube345Process`（dq/dk/dv）——Cube/Vector 的生产消费关系与 u4-l4 的 basic_modules 同风格，但等待点固定在「gather 完成」。

[sparse_flash_attention_grad_enhance_bs1_basic.h:L754-L772](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/op_kernel/sparse_flash_attention_grad_enhance_bs1_basic.h#L754-L772)——`GetActualSelCount`：读 topk 索引数组统计该 (t1, n2) 行的有效块数（遇 -1 停），是「每行动态任务量」的来源——**bs1 切分的负载不均由这里的动态循环天然吸收**。

五个矩阵乘与向量侧：

[basic_modules/cube_op.h:L52-L55](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/basic_modules/cube_op.h#L52-L55)——`CubeOp` 暴露的两个编组入口：`cube12Process`（cube1+cube2：重算打分、dP）与 `cube345Process`（cube3+cube4+cube5：dQ/dK/dV）。

[cube_modules/cube1.h:L13](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/basic_modules/cube_modules/cube1.h#L13)、[cube2.h:L13](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/basic_modules/cube_modules/cube2.h#L13)、[cube3.h:L13](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/basic_modules/cube_modules/cube3.h#L13)、[cube4.h:L13](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/basic_modules/cube_modules/cube4.h#L13)、[cube5.h:L13](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/basic_modules/cube_modules/cube5.h#L13)——五个文件头的公式注释：`s = q * k^T`、`dp = dy * v^T`、`dq = ds * k`、`dk = s^T * q`、`dv = p^T * dy`，与 4.1.3 反向文档的三阶段公式一一对应（阶段1= cube2+cube5，阶段3 = cube3+cube4，阶段2 的 dS 在向量侧）。

[basic_modules/vec_op.h:L56-L57](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/basic_modules/vec_op.h#L56-L57)——`VecOp` 的两个稀疏动作声明：`GatherKV`（按索引把 K/V 行搬进 workspace）与 `ScatterAdd`（dk/dv 累加回原位）。

[basic_modules/vec_op.h:L821-L895](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/basic_modules/vec_op.h#L821-L895)——`GatherKV` 的搬运实现：`DataCopyPad(gatherTensor[...], ...)` 按块把 GM 的 K（含 rope 部分，两组 `gatherTensor/gatherRopeTensor` 乒乓）搬进 UB，再写出 `selectedKWorkspaceGm`——**这就是文档公式 `Gather(key, topkIndices[i])` 的设备侧实现**。

[basic_modules/vec_op.h:L538-L556 与 L741](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/basic_modules/vec_op.h#L538-L556)——`CalSub`（逐块 `Sub`）与 L741 的 `CalSub(dPTensor, rowSumOutTensor, ...)`：把 `rowsum(dO⊙O)` 从 dP 里减掉，得到 softmax 反传的核心修正项（`CalRowsumAndSftCopyIn` 在 L76 声明，负责算行和并搬 softmax 中间量）。

#### 4.5.4 代码实践

**实践目标**：用 CPU PyTorch 复现反向的「Gather → 五公式 → Scatter」全流程，并对拍 `torch.autograd`，验证你对公式与 Scatter 语义的理解。

**操作步骤**（示例代码，CPU 可跑）：

```python
import torch

torch.manual_seed(0)
S1, S2, D, K, BLK = 3, 12, 8, 2, 3
q = torch.randn(S1, D, requires_grad=True)
k = torch.randn(S2, D, requires_grad=True)
idx = torch.tensor([[0, 2], [1, -1], [2, 0]])      # 每行 K=2 个块, 允许 -1 填充
scale = D ** -0.5

def sparse_fa_fwd(q, k, idx, blk):
    out = torch.zeros(S1, D)
    rows_all = []
    for t in range(S1):
        rows = []
        for b in idx[t].tolist():
            if b < 0: break
            rows.append(k[b*blk:(b+1)*blk])
        rows_all.append(torch.cat(rows) if rows else k[:0])
        if len(rows) == 0: continue
        p = torch.softmax(scale * (q[t] @ rows_all[-1].T), -1)
        out[t] = p @ rows_all[-1]
    return out, rows_all

out, rows_all = sparse_fa_fwd(q, k, idx, BLK)
dy = torch.randn_like(out)
out.backward(dy)                     # 参考梯度: q.grad / k.grad
dq_ref, dk_ref = q.grad.clone(), k.grad.clone()
q.grad = k.grad = None

# ---- 手写反向: Gather -> 5公式 -> Scatter (对应 cube1~5 + vec_op) ----
dk = torch.zeros_like(k); dq = torch.zeros_like(q)
for t in range(S1):
    sel = rows_all[t]
    if sel.numel() == 0: continue
    p = torch.softmax(scale * (q[t] @ sel.T), -1)     # cube1: s = q k^T
    dp = dy[t] @ sel.T                                 # cube2: dp = dy v^T (此处 v=k)
    dv = p.T @ dy[t]                                   # cube5: dv = p^T dy
    lse_corr = (dy[t] * out[t]).sum()                  # FlashSoftmaxGrad = rowsum(dy*O)
    ds = p * (dp - lse_corr)                           # 阶段2: dS (向量侧)
    dq[t] = scale * (ds @ sel)                         # cube3: dq = ds k
    dks = scale * (ds.T @ q[t].unsqueeze(0))           # cube4: dk = s^T q
    pos = [b*blk + j for b in idx[t].tolist() if b >= 0 for j in range(blk)]
    for i, u in enumerate(pos):                        # ScatterAdd: 索引可能重复!
        dk[u] += dks[i]                                # 注意: 参考实现用 index_add 语义
print("dq 一致:", torch.allclose(dq, dq_ref, atol=1e-5))
print("dk 一致:", torch.allclose(dk, dk_ref, atol=1e-5))
```

**需要观察的现象**：`dq/dk` 与 autograd 参考值 allclose（本例 v=k、无 mask，故意让第 2 行出现重复块 `[2,0]`——注意重复块会使位置 0~2 被累加两次，与 autograd 的 `index_add` 语义一致，恰好验证 ScatterAdd 的「多对一累加」本质）。若把 `dk[u] +=` 改成 `dk[u] =`，dk 对拍会失败——这就是 `ScatterAdd`（而非 Scatter 覆盖）存在的原因。

**预期结果**：CPU 上两行 allclose 均为 True。与 NPU 算子 `torch.ops.custom.npu_sparse_flash_attention_grad_enhance`（示例见 [npu_sparse_flash_attention_grad_enhance.md:L144-L182](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/docs/npu_sparse_flash_attention_grad_enhance.md#L144-L182)）的对拍**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：反向为什么要在最后加一个 `SparseFlashAttentionGradEnhancePost`（cast 阶段），而前向不需要？
**答案**：反向的 dk/dv 要经历「fp32 workspace 累加（ScatterAdd 区是 B32）→ 输出 fp16/bf16」两步：核数按 AIC（=usedCoreNum）算的行循环结束后，用 `castUsedCoreNum`（AIV 数，见 tiling L281）把转换并行铺满所有 Vector 核（[sparse_flash_attention_grad_enhance_bs1_basic.h:L439-L442](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/op_kernel/sparse_flash_attention_grad_enhance_bs1_basic.h#L439-L442)）。前向输出在 `ProcessVec2L` 里直接按 OUT_T 写出，无需二次转换。

**练习 2**：`deterministic` 开关影响哪些代码路径？
**答案**：①tilingKey 末位（[L254-L257](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/op_host/sparse_flash_attention_grad_enhance_tiling_bs1_basic.cpp#L254-L257)）；②kernel 模板参 `DETERMINISTIC_ENABLE`，在 AIC/AIV 主循环里插入 `SCATTER_CUBE_SYNC_FLAG` 等「固定顺序握手」（[L277-L283](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/op_kernel/sparse_flash_attention_grad_enhance_bs1_basic.h#L277-L283)、L367-L399）；③`PostTiling` 里 TilingData 超容量时确定性模式直接报错而非流入下一模板（[L218-L232](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/op_host/sparse_flash_attention_grad_enhance_tiling_bs1_basic.cpp#L218-L232)）。与 u4-l4 稠密 FA 反向的确定性版本思路一致：用固定合并顺序换 bit 级复现。

**练习 3**：对比「前向分核（4.3）」与「反向 bs1 分核（4.4）」，稀疏负载不均各自靠什么吸收？
**答案**：前向按各 batch 实际 query 数（`actBatchS1`）加权均分任务块（`InitCalcParamsEach` 的 `avgBaseNum`），S2 方向按 `curActualSeqLen`（已被稀疏化钳位）动态定循环次数；反向直接按 s1 行一维分核，每行实际要算多少块由 `GetActualSelCount` 运行期读索引决定——前者「Host/Device 先估权重再分」，后者「分完靠每行动态循环自适应」。

## 5. 综合实践

**任务：制作一份「稀疏注意力掩码生成器 + tilingKey 对照卡」，把本讲三个学习目标串起来。**

1. **稀疏模式枚举与掩码示意（对应学习目标 1）**。在 [npu_sparse_attention_grad_enhance.md 的 sparse_mode 表格](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/docs/npu_sparse_flash_attention_grad_enhance.md#L113-L126)（0~8 共 9 种，SFA 只支持 0 和 3）与前向文档 [npu_sparse_attention_enhance.md:L61-L63](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/docs/npu_sparse_flash_attention_enhance.md#L61-L63) 的基础上，用 Python 生成每种模式的布尔掩码并打印（示例代码，CPU 可跑）：

```python
import torch

def sparse_mode_mask(mode, s1=6, s2=10):
    i = torch.arange(s1).unsqueeze(1)
    j = torch.arange(s2).unsqueeze(0)
    off = s2 - s1                      # rightDown 右下对齐时的偏移
    if mode == 0: return torch.ones(s1, s2, dtype=torch.bool)          # 全计算
    if mode == 1: return torch.ones(s1, s2, dtype=torch.bool)          # allMask(需外部矩阵,示意补全1)
    if mode == 2: return j <= i                                        # leftUpCausal
    if mode == 3: return j <= i + off                                  # rightDownCausal(默认)
    if mode == 4: return (j >= i - 2) & (j <= i + 2)                   # band 示意(pre/next=2)
    if mode == 5: return (j <= i + off) | (j < 4)                      # prefix 示意
    if mode == 6: return torch.ones(s1, s2, dtype=torch.bool)          # global
    if mode == 7: return (j % 2) == (i % 2)                            # dilated 示意
    if mode == 8: return (i // 2 % 2) == (j // 2 % 2)                  # block_local 示意
    raise ValueError(mode)

for m in (0, 3):                       # SFA 前向/反向仅支持这两种
    print(f"mode {m}:\n", sparse_mode_mask(m).int())
```

   要求：打印 mode 0 与 mode 3 两张 6×10 掩码；对照 kernel 的 `threshold = nextTokensPerBatch + s1Idx + 1`（[sparse_flash_attention_enhance_kernel_mla.h:L323-L326](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance_kernel_mla.h#L323-L326)）验证 mode 3 掩码第 t 行 True 的个数恰等于 `threshold`。其余模式的掩码形状仅作理解示意（SFA 不支持），需在笔记中注明「示意，非算子行为」。
2. **tilingKey 对照卡（对应目标 2）**。完成 4.2.4 的三算子对照表，并为 SFA 反向手算 `atten=True, rope=True, BSND, det=False` 的 key（答案：`11110`）。
3. **索引张量约束卡（对应目标 3）**。完成 4.4.4 的 6 行约束表，并用 `torch` 构造一个违反「有效值在前半部分」约束的索引（如 `[[3, -1, 2]]`），说明 kernel 会如何在 `CalcSinnerTopKBegin` 提前终止、丢失块 2（[sparse_flash_attention_enhance_kernel_mla.h:L985-L991](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_enhance/op_kernel/sparse_flash_attention_enhance_kernel_mla.h#L985-L991)）——这是静默错误，调用方必须自己保证排布合法。

产出物：一份 markdown 笔记 + 一个可运行的 `python` 脚本。NPU 对拍部分标注「待本地验证」。

## 6. 本讲小结

- 稀疏注意力的本质是**把 softmax 分母从「整个 S2」换成「索引选中 ∩ 因果允许」的 token 子集**：kernel 用 `threshold`（因果上界）与 `curTopKIdx/curOffsetInSparseBlock`（索引游标）两个机制落地，`sparse_indices` 中 `-1` 是终止填充，有效值必须在前半部分。
- SFA 前向 tiling 用 **`GET_TPL_TILING_KEY` 模板参数化 tilingKey**（FLASH_DECODE/LAYOUT_T/KV_LAYOUT_T/TEMPLATE_MODE 四参数、4 组合法组合）替代稠密 FA 的 64bit 手工位域；dtype 不进 key，由 kernel 入口 `ORIG_DTYPE_*` 编译期分流。
- 前向 kernel 按 **C_TEMPLATE（Cube 边算边按索引离散搬 K）/ V_TEMPLATE（Vector 先 MergeKv 聚合成连续缓冲）** 两套稀疏取数策略组织，`sparseBlockSize ≤ 4` 走 V 模板；AIC:AIV=1:2 混合核跑三槽流水线，MLA 的 512/64/576 维度写死在 kernel 常量里。
- SFA 反向 tiling 走 **tiling_base 责任链**（当前仅 bs1_basic 一个模板，优先级 1），公共校验放 `tiling_common.cpp`（softmaxMax/Sum 必须 [N2,T1,G] 且 fp32），tilingKey 是 `1{atten}{rope}{BSND}{det}` 的十进制编码（10000~11111）。
- 反向 kernel 按 **s1 行（bs1）分核**，每行 Gather K/V → cube1~5 五个矩阵乘（`s=qk^T`、`dp=dyv^T`、`dq=dsk`、`dk=s^Tq`、`dv=p^Tdy`）+ 向量侧 `dS=P⊙(dp−rowsum(dy⊙O))` → ScatterAdd 把 dk/dv 累加回原位，最后由 Post 阶段做 fp32→fp16/bf16 收尾；确定性模式通过固定握手顺序换 bit 级复现。
- 接口契约以 def + tiling 源码为准：反向文档把 sparse_indices 写成 INT64 是笔误（实为 int32）；反向的 rope 输入在 def 里 OPTIONAL 但 tiling 强制存在。

## 7. 下一步学习建议

- **u4-l6 LightningIndexerEnhance**：稀疏索引的「生产者」。本讲通篇假设 `sparse_indices` 从天而降，下一讲精读 `lightning_indexer_enhance` 的 proto/tiling 与 service_cube/service_vector 分工，看 q·k 打分 → topk 选索引 → softmax 权重的完整链路，把「索引怎么来」补上。
- **u4-l7 SparseLightningIndexerGradKlLoss**：索引器自身的训练（KL 散度反向），是本讲反向技术与 u4-l6 的合流点。
- 若想巩固本讲的 Gather/Scatter 直觉，可回读 [npu_sparse_flash_attention_grad_enhance.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_flash_attention_grad_enhance/docs/npu_sparse_flash_attention_grad_enhance.md) 的三阶段公式，并用 `tests/st/test_npu_sparse_flash_attention_grad_enhance.py`（ST 用例）观察真实对拍参数——该目录下的 `tests/ut/op_host/test_sparse_flash_attention_grad_enhance_tiling.cpp` 也是复习 u8 单元测试写法的现成素材。
