# u4-l7 SparseLightningIndexerGradKlLoss：KL 散度反向算子

## 1. 本讲目标

学完本讲，你应该能够：

1. **独立推导** LightningIndexer 训练损失 \( L(I)=\sum_t D_{KL}(p_t \| \text{Softmax}(I_t)) \) 的反向梯度链：\( dI = \text{Softmax}(I) - p \)，以及 \( dW \)、\( dS' \)、\( dQ_{index} \)、\( dK_{index} \) 的链式法则表达式，并用 `torch.autograd` 数值验证。
2. **读懂** `sparse_lightning_indexer_grad_kl_loss_enhance_tiling_general.cpp` 中「按 head/topk 解析维度 + 按因果行长做负载均衡分核」的切分逻辑，理解它与普通均分 tiling 的本质差别。
3. **讲清楚** 设备侧 `vector.h` 与 `service_cube.h` 的分工：Vector 核负责 softmax、KL loss、dW、ReLU 梯度等逐元素/归约运算，Cube 核负责 4 次 Mmad（P 打分、S' 打分、dK 前置、dQ），二者以 AIC:AIV=1:2 混合核 + 三级软件流水线协同。
4. 把本算子放回 lightning indexer 家族：`lightning_indexer_enhance`（前向出索引）→ `sparse_flash_attention_enhance`（用索引做稀疏注意力）→ 本算子（ indexer 单独训练时的反向+Loss 融合）。

## 2. 前置知识

### 2.1 KL 散度与「目标分布 vs 预测分布」

KL 散度度量两个概率分布的差异：

\[
D_{KL}(a \| b) = \sum_i a_i \log \frac{a_i}{b_i}
\]

它不对称、非负，当且仅当 \( a = b \) 时为 0。本算子中：

- **目标分布 \( p \)**：由主注意力（main attention）打分经全 head 求和后归一化得到——它代表「真正做了注意力计算的那些 token 的重要性」；
- **预测分布 \( q = \text{Softmax}(I) \)**：由 LightningIndexer 自己打出的 logits \( I \) 经 softmax 得到——它代表「indexer 认为哪些 token 重要」。

训练目标就是让 indexer 的判断逼近真实注意力分布，从而 TopK 选出的稀疏集合尽量不丢信息。

### 2.2 反向传播要复习的三件事

1. **softmax 的雅可比**：\( \partial q_j / \partial I_k = q_j(\delta_{jk} - q_k) \)。由此可推出（本讲 4.1 详细推导）\( \partial L / \partial I = q - p \)，形式上也可写成 \( q \cdot (1 - p/q) \)——这正是任务书中「dL/dlogits = p*(1 - q/p) 形式」的出处；而直接对 \( \log q \) 求导则是 \( -p \)。三种写法说的是同一件事。
2. **ReLU 的次梯度**：\( \partial \text{ReLU}(x) / \partial x = \mathbb{1}[x > 0] \)，反向就是把上游梯度乘一个 0/1 掩码（源码里的 `ReLUGrad`）。
3. **GQA 分组归约**：多组 query 头共享一组 KV 头（\( G = N_1 / N_2 \)）。本算子中 \( p \) 在 \( G=128 \) 个 query 头上求和再除以 \( G \)，\( I \) 在 \( G_{index}=64 \) 个 index 头上加权求和——两条链路的头数不同，是本算子接口里 query 与 query_index 分开传入的原因。

### 2.3 承接前几讲

- **u4-l6**：`lightning_indexer_enhance` 前向 = `ReLU(Q·Kᵀ) 打分 → 组内 W 加权求和 → TopK`，产出 `sparse_indices`。本算子就是它叠加 Loss 之后的反向。
- **u4-l5**：稀疏 FA 用 `sparse_indices` 收窄 softmax 分母；本算子的输入 `softmax_max / softmax_sum` 正是 FA 前向保存的中间量（复用思想与 u4-l4 反向 FA 一致）。
- **u2-l3 / u4-l2**：Tiling「Host 侧作战规划」的基本概念；本讲在其上新增**负载均衡分核**。
- **u4-l3 / u4-l6**：AIC（Cube）/AIV（Vector）混合核、`KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_MIX_AIC_1_2)`、`CrossCoreSetFlag/WaitFlag` 跨核同步、`runInfos[3]` 三槽环形缓冲软件流水线。

## 3. 本讲源码地图

所有路径相对于 `ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/`：

| 文件 | 作用 |
| --- | --- |
| `docs/npu_sparse_lightning_indexer_grad_kl_loss_enhance.md` | 算子说明书：功能、全部计算公式、接口约束（本讲的数学圣经） |
| `op_host/sparse_lightning_indexer_grad_kl_loss_enhance_def.cpp` | 原型注册：12 输入 / 4 输出 / 7 属性 |
| `op_host/sparse_lightning_indexer_grad_kl_loss_enhance_tiling.cpp` | tiling 入口 + `IMPL_OP_OPTILING` 注册 |
| `op_host/sparse_lightning_indexer_grad_kl_loss_enhance_tiling_general.h` | 本算子私有的 `TilingBase` 七步框架 + CompileInfo |
| `op_host/sparse_lightning_indexer_grad_kl_loss_enhance_tiling_general.cpp` | **核心切分实现**：校验、维度解析、负载均衡、tilingKey、workspace |
| `op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance.cpp` | 设备侧入口：按 tilingKey 实例化模板类 |
| `op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_template_tiling_key.h` | 模板参数声明（`ASCENDC_TPL_ARGS_DECL`），Host/Device 共用 |
| `op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_tiling.h` | TilingData 结构 + 输入输出索引常量 |
| `op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_base.h` | 编排层：Init/MainProcess/DeterProcess/GetRunInfo |
| `op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_vector.h` | **Vector 核实现**：Gather、P/SY、KL Loss、dW、ReLUGrad、ScatterAdd |
| `op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_vector2.h` | 收尾 Vector：dKeyIndex 落盘 + 确定性 loss 归约 |
| `op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_service_cube.h` | **Cube 核实现**：mm1/mm2/mm5/mm6 四次 Mmad |
| `tests/st/lightning_indexer_klloss_golden.py` | CPU/PyTorch 参考实现（golden），公式对照最佳材料 |

## 4. 核心概念与源码讲解

### 4.1 算子定位与数学推导：从文档公式到反向梯度链

#### 4.1.1 概念说明

`SparseLightningIndexerGradKLLossEnhance` 是 `LightningIndexerEnhance` 的反向算子，并**额外融合了 Loss 计算**——即一次调用同时产出 4 个结果：三路梯度 + loss 标量。为什么要把 Loss 融进来？因为 KL loss 对 logits 的梯度 \( q - p \) 恰好是所有三路梯度的公共上游，融合后 \( p \)、\( q \) 都在片上，无需再产生 loss 单独的反向算子。

文档对前向打分的定义（取 TopK 用的 value）：

\[
I_{t,:} = W_{t,:} \, @ \, \text{ReLU}\big(q_{t,:} @ (K_{:t,:})^T\big)
\]

其中 \( W \) 是第 \( t \) 个 token 的权重、\( q \) 是 \( G \) 个 query 头合轴后的矩阵、\( K \) 为前 \( t \) 行 key（因果）。训练损失：

\[
L(I) = \sum_t D_{KL}\big(p_{t,:} \,\|\, \text{Softmax}(I_{t,:})\big)
      = \sum_t \sum_i p_{t,i} \log \frac{p_{t,i}}{q_{t,i}}, \quad q = \text{Softmax}(I)
\]

#### 4.1.2 核心流程：梯度链推导

设单行 \( t \)：\( L = \sum_i p_i (\log p_i - \log q_i) \)，\( q = \text{Softmax}(I) \)。

**第一步**，对 \( \log q_j \) 直接求导：\( \partial L / \partial \log q_j = -p_j \)。

**第二步**，代入 softmax 雅可比 \( \partial q_i / \partial I_j = q_i(\delta_{ij} - q_j) \)：

\[
\frac{\partial L}{\partial I_j} = \sum_i \frac{\partial L}{\partial q_i} \frac{\partial q_i}{\partial I_j} = \sum_i \frac{-p_i}{q_i} \cdot q_i(\delta_{ij} - q_j) = -p_j + q_j \underbrace{\textstyle\sum_i p_i}_{=1} = q_j - p_j
\]

即 **\( dI = \text{Softmax}(I) - p \)**（等价写法 \( q \cdot (1 - p/q) \)，与任务书表述一致）。

**第三步**，沿前向 \( I = W \odot \) 组内加权和\( (\text{ReLU}(S')) \) 链式展开（\( S' = q_{index} @ (K_{index})^T \)）：

```
dI = q - p                                  # KL 对 logits 的梯度（shape: [K]）
dW[n]      = Σ_k dI[k] · ReLU(S')[n,k]      # dI @ ReLU(S')^T   （对每行输出 [Nidx1]）
dS'[n,k]   = dI[k] · W[n] · 1[S'[n,k] > 0]  # 乘 weight 再乘 ReLU 次梯度掩码
dQ_index   = dS' @ K_index_topk             # [Nidx1, K] @ [K, D]
dK_index   = dS'^T @ Q_index                # [K, Nidx1] @ [Nidx1, D] → 按 topk 索引 scatter 回 [S2, D]
```

注意 `dK_index` 的特殊性：梯度落在「被 TopK 选中的 key 行」上，而同一 key 行会被多个 query 选中，所以必须 **ScatterAdd 累加**，这也是反向比前向多出一步 gather/scatter 的原因。

#### 4.1.3 源码精读

文档中的四段公式（打分、损失、dI、dW/dq/dK）：

- [docs L17-L18](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/docs/npu_sparse_lightning_indexer_grad_kl_loss_enhance.md#L17-L18)：接口定位——LightningIndexer 的反向 + 融合 Loss；说明 LightningIndexer 筛选 TopK 存入 SparseIndices 以加速长序列训练/推理。
- [docs L21-L37](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/docs/npu_sparse_lightning_indexer_grad_kl_loss_enhance.md#L21-L37)：\( I \) 公式、\( L=\sum_t D_{KL}(p\|\text{Softmax}(I)) \)、\( p \) 的来历（主注意力全 head 求和 + 上下文方向 L1 正则）、\( D_{KL} \) 表达式。
- [docs L41-L57](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/docs/npu_sparse_lightning_indexer_grad_kl_loss_enhance.md#L41-L57)：\( dI = \text{Softmax}(I) - p \)、\( dW = dI @ \text{ReLU}(S)^T \)、\( dq = dS @ K \)、\( dK = dS^T @ q \)。

torch 参考实现 `tests/st/lightning_indexer_klloss_golden.py` 与文档逐条对应，是「公式 ↔ 代码」的对照标本：

- [golden.py L231-L244](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/tests/st/lightning_indexer_klloss_golden.py#L231-L244)：目标分布 \( p \)——gather key、`matmul` 打分、乘 scale、softmax、按 G 归约后 `/G`。
- [golden.py L253-L268](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/tests/st/lightning_indexer_klloss_golden.py#L253-L268)：预测分布链——`matmul`、`relu`、乘 weight、按 G_index 归约、softmax。
- [golden.py L270-L280](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/tests/st/lightning_indexer_klloss_golden.py#L270-L280)：KL loss——`1e-8` clip 防零、`log p - log s`、乘 \( p \)、求和。
- [golden.py L282-L303](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/tests/st/lightning_indexer_klloss_golden.py#L282-L303)：`q_sub_p = sSoftmaxRes - pReduceGRes` 即 \( dI \)；dW、ReLU 梯度掩码、dQ、`beforeScatterAdd`、`safe_scatter_add` 得 dK。

#### 4.1.4 代码实践

**实践目标**：用 `torch.autograd` 数值验证 4.1.2 的整条梯度链。

**操作步骤**（示例代码，任意有 PyTorch 的 CPU 环境可跑）：

```python
import torch
torch.manual_seed(0)
K, N_idx, D = 16, 8, 128          # topk、index 头数、index 维度
p = torch.softmax(torch.randn(K), -1)          # 目标分布（视为常量）
I = torch.randn(K, requires_grad=True)         # logits
W = torch.randn(N_idx, requires_grad=True)     # 每头权重
S = torch.randn(N_idx, K, requires_grad=True)  # index 打分矩阵 S'
Kidx = torch.randn(K, D, requires_grad=True)   # gather 后的 key_index
Qidx = torch.randn(N_idx, D, requires_grad=True)

def forward(I, W, S, Kidx, Qidx):
    q = torch.softmax(I, -1)                     # 预测分布
    loss = (p * (p.clamp_min(1e-8).log() - q.clamp_min(1e-8).log())).sum()
    relu = torch.relu(S)
    Ihat = (relu * W.unsqueeze(-1)).sum(0)       # 组内 W 加权求和
    dQ = torch.autograd.grad(Ihat.sum(), S, retain_graph=True)[0]  # 仅示意
    return loss, q, relu

loss, q, relu = forward(I, W, S, Kidx, Qidx)
grads = torch.autograd.grad(loss, [I, W, S], create_graph=False)

# 验证 1: dI = q - p（文档公式，也是 q*(1 - p/q)）
print(torch.allclose(grads[0], q - p, atol=1e-6))          # 期望 True
# 验证 2: 对 log q 的梯度 = -p
dl_dlogq = torch.autograd.grad(loss, I, create_graph=True)[0]  # dL/dI
# 验证 3: 手写 dW / dS'
dI = (q - p).detach()
dW_manual = dI @ relu.T                                     # [N_idx]
dS_manual = dI.unsqueeze(0) * W.unsqueeze(-1) * (relu > 0)  # [N_idx, K]
print(torch.allclose(grads[1], dW_manual, atol=1e-5))       # 期望 True
print(torch.allclose(grads[2], dS_manual, atol=1e-5))       # 期望 True
```

**需要观察的现象**：三个 `allclose` 均为 `True`；特别地 `grads[0]`（autograd 的 dL/dI）与 `q - p` 逐元素一致，且其元素和为 0（softmax 雅可比的性质）。

**预期结果**：验证通过即证明文档公式与链式法则一致；随后可带着「\( dI \) 在 kernel 里是哪一行算的」这个问题进入 4.4（答案：`vector.h` 的 `Sub`，见 4.4.3 第 3 步）。若在无 PyTorch 环境运行，标「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 \( dI \) 各元素之和恒为 0？
**答案**：\( \sum_j (q_j - p_j) = \sum_j q_j - \sum_j p_j = 1 - 1 = 0 \)，因为 \( p \)、\( q \) 都是概率分布。

**练习 2**：如果去掉 loss 中的 `1e-8` clip，什么输入会出问题？
**答案**：当某个 \( q_i \) 或 \( p_i \) 为 0（如被因果/无效 token 掩码置 -inf 后 softmax 得 0）时 `log(0) = -inf`，产生 NaN。kernel 侧 `VectorLoss` 用 `Maxs(..., 1e-8)` 做同样保护（见 4.4.3）。

**练习 3**：dK 为什么必须用 ScatterAdd 而不能直接覆盖写？
**答案**：TopK 索引来自多个 query 行且可能重复选中同一 key 行；直接覆盖会丢失其他行对该 key 的梯度，必须原子累加（kernel 侧 `SetAtomicAdd<T>` + `DataCopyPad`）。

---

### 4.2 op_host：接口契约与 tiling_general 的切分/负载均衡

#### 4.2.1 概念说明

Host 侧两件事：`_def.cpp` 声明**接口契约**（哪些张量、什么类型、哪些属性、支持哪些芯片），`tiling_general` 完成**切分规划**。本算子 tiling 的独特难点：**因果稀疏场景下每个 query 行的计算量不同**。

sparse_mode=3（右下角因果）下第 \( j \) 行的有效 key 长度是：

\[
\text{s2Real}(j) = \min\big(\text{AlignUp}(S_2 - S_1 + j + 1,\ 512),\ K\big)
\]

行号越大要处理的 key 越多。若把 \( B \times S_1 \) 行按数量均分给各 AIC 核，靠后的核分到的全是长行、靠前的核全是短行，负载严重不均。因此 tiling 引入**按工作量的负载均衡**：先算出每行的工作量数组，再做「均分粗调 + 前后双向扫描精调」，最终把每个核负责的**起始行号**写进 TilingData 的 `bS1Index[]`。

#### 4.2.2 核心流程

```
TilingSparseLightningIndexerGradKLLossEnhance (tiling.cpp 入口)
 └─ SparseLightningIndexerGradKLLossEnhanceTilingBase::DoTiling   # 七步模板方法
     1. GetShapeAttrsInfo   → CheckContext + AnalyzeAttrs/Dtype/Layout 三重校验
                              解析 B/S1/S2/N2/G/D/K/topk → baseParams
     2. GetPlatformInfo     → AIV/AIC 核数、UB/L1/L0C/L2 大小（或 CompileInfo 缓存）
     3. IsCapable           → 恒 true（单实现）
     4. DoOpTiling          → CalcTotalSize(B*S1 或 T1) → SetMultiCoreParamsRegbase(核数= min(total, aicNum))
                              → SoftMaxTilingFunc(softmax 高阶 API tiling)
                              → SetSparseParamsRegbase: InitSparseValidArray(每行工作量)
                                  → SetSparseStartIdx(粗调 + BalanceLoad/Balance4DLoad 精调 → bS1Index[])
                              → InitOutputSplit(dKeyIndex 清零任务按 AIV 均分)
     5. GetWorkspaceSize    → 乒乓×(P/SY gather + bmm1/bmm2/reluGrad/psySync)×核数 + scatterAdd + loss + 16MB 预留
     6. SetTilingKey(GET_TPL_TILING_KEY(hasRope, topKRange, layout, layout, sparseMode, deterministic))
     7. DumpTilingInfo      # OP_LOGD 打印 TilingData 内容
```

#### 4.2.3 源码精读

**接口契约（_def.cpp）**：

- [def.cpp L23-L84](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_host/sparse_lightning_indexer_grad_kl_loss_enhance_def.cpp#L23-L84)：12 个输入——必选 8 个（query/key/query_index/key_index/weight/sparse_indices/softmax_max/softmax_sum），可选 4 个（query_rope/key_rope/actual_seq_lengths_query/key，后两者带 `ValueDepend`）。注意 5 个 fp16/bf16 输入类型必须一致，`sparse_indices` 是 int32，`softmax_max/sum` 是 fp32。
- [def.cpp L85-L100](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_host/sparse_lightning_indexer_grad_kl_loss_enhance_def.cpp#L85-L100)：4 个输出——d_query_index / d_key_index / d_weight（fp16/bf16）+ loss（fp32 标量）。
- [def.cpp L101-L107](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_host/sparse_lightning_indexer_grad_kl_loss_enhance_def.cpp#L101-L107)：7 个属性——scale_value（必选 float）、layout（默认 TND）、sparse_mode（默认 3）、pre/next_tokens、deterministic、sparse_block_size。
- [def.cpp L108-L117](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_host/sparse_lightning_indexer_grad_kl_loss_enhance_def.cpp#L108-L117)：`OpAICoreConfig` 打开动态 shape/格式/维数等能力开关，`AddConfig("ascend910b")` 与 `AddConfig("ascend910_93")` 双芯片注册——与 docs 产品表（A2/A3 支持）互证。

**tiling 入口与七步框架**：

- [tiling.cpp L48-L69](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_host/sparse_lightning_indexer_grad_kl_loss_enhance_tiling.cpp#L48-L69)：入口函数构造 tiling 类并 `DoTiling()`；`IMPL_OP_OPTILING(...).Tiling(...).TilingParse<CompileInfo>(...)` 完成注册与编译期平台信息缓存。
- [tiling_general.h L52-L87](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_host/sparse_lightning_indexer_grad_kl_loss_enhance_tiling_general.h#L52-L87)：`TilingBase::DoTiling` 模板方法——七步顺序执行、末尾统一 `SetTilingKey`。它是 u3-l3 `common/include/tiling_base` 框架的**算子内私有副本**（本算子把框架直接搬进 op_host，牺牲复用换取独立演进）。

**维度与属性校验（按 head/topk 解析）**：

- [tiling_general.cpp L154-L177](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_host/sparse_lightning_indexer_grad_kl_loss_enhance_tiling_general.cpp#L154-L177)：`AnalyzeAttrs`——读 7 个属性，强制 `sparse_mode == 3`（L172-L174），否则报错返回。
- [tiling_general.cpp L220-L348](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_host/sparse_lightning_indexer_grad_kl_loss_enhance_tiling_general.cpp#L220-L348)：`AnalyzeDimLayout`——按 layout 维数分 TND/BSND 两条解析路径：TND 从 `actual_seq_qlen/klen` 的**差分**得出各 batch 的 S1/S2（L210-L217 的 `GetActualSeqLenData` 把累计值转逐 batch 长度），并校验累加和等于 T1/T2；两路都从 `query/key/query_index/sparse_indices` 的 shape 提取 \( G = N_1/N_2 \)、\( G_{index} \)、D、K，并做 **topk 约束**：`kSize ≤ 8192 且为 1024 的倍数`（L294-L298），据此选 `TopKRange::RANGE_0_2K / RANGE_2K_8K`（L299）——这就是「按 head/topk 切分」中 topk 维度进入 tilingKey 的方式。
- [tiling_general.cpp L351-L431](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_host/sparse_lightning_indexer_grad_kl_loss_enhance_tiling_general.cpp#L351-L431)：`AnalyzeDtype`——5 个 16bit 输入类型一致（fp16 全 fp16 / bf16 全 bf16），`sparse_indices` int32、`softmax_max/sum` fp32。
- [tiling_general.cpp L434-L815](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_host/sparse_lightning_indexer_grad_kl_loss_enhance_tiling_general.cpp#L434-L815)：`CrossShapeVerify`——所有输入输出按 B/S1/S2/N2 交叉对齐；硬编码 `N2=1`（MQA）、`query/key 的 D=512`（L520-L531）、`query_index/key_index 的 D=128`（L532-L546）、rope 的 `D=64`（L597-L609）。**注意 L565-L568 强制 `hasRope` 为真**（"rope can't be null"）——def 里 rope 是 OPTIONAL，但当前 tiling 实现要求必须传入，文档典型值也给出 qRope d=64；这是「读接口以 def+tiling 双重校验为准」的好例子。
- [tiling_general.cpp L429-L482（docs 规格表）](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/docs/npu_sparse_lightning_indexer_grad_kl_loss_enhance.md#L429-L482)：文档侧同一套约束（B 1~256、S1/S2 1~128K 不等长、N1∈{8..128}、Nidx1∈{8..64}、N2=1、D=512、Drope=64、topk∈{2048..8192} 列表、sparse_block_size 仅支持 1），与源码校验互证。

**因果行长与负载均衡（本算子 tiling 的灵魂）**：

- [tiling_general.cpp L955-L970](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_host/sparse_lightning_indexer_grad_kl_loss_enhance_tiling_general.cpp#L955-L970)：`GetS2RealSize`——`RIGHT_DOWN_CAUSAL` 时 `s2RealSize = s2Size - s1Size + s1Idx + 1`，512 对齐后与 kSize 取 min。
- [tiling_general.cpp L972-L1003](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_host/sparse_lightning_indexer_grad_kl_loss_enhance_tiling_general.cpp#L972-L1003)：`InitSparseValidArray`——为每个 (b, s1) 行填 `sparseValidArray[row] = 该行 s2RealSize`，即**每行工作量**。
- [tiling_general.cpp L1111-L1194](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_host/sparse_lightning_indexer_grad_kl_loss_enhance_tiling_general.cpp#L1111-L1194)：`SetSparseStartIdx`——TND 路径三步走：(a) L1124-L1130 按 `splitFactorSize` 均分做初始基线；(b) L1133-L1159 以「每核平均工作量」为目标的**贪心粗调**（每次拿最接近均值的行数，并动态重算剩余均值）；(c) L1163-L1166 循环调用 `BalanceLoad` **精调**（L1024-L1072：先从前往后把重核的行挪给轻核、再从后往前，直到最大核负载不再下降），最后与均分结果择优（L1168-L1173）写入 `sparseStartIdx`（即 TilingData 的 `bS1Index[]`）。BSND 路径则用更简单的 `Balance4DLoad`（L1074-L1108，累加逼近均值切换核边界）。L1188-L1192 把为 0 的边界补成总行数，保证尾核不越界。
- [tiling_general.cpp L1214-L1220](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_host/sparse_lightning_indexer_grad_kl_loss_enhance_tiling_general.cpp#L1214-L1220)：`SetMultiCoreParamsRegbase`——`coreNum = min(totalSize, aicNum)`、`splitFactorSize = CeilDivision(totalSize, coreNum)`。
- [tiling_general.cpp L1244-L1264](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_host/sparse_lightning_indexer_grad_kl_loss_enhance_tiling_general.cpp#L1244-L1264)：`DoOpTiling`——`SetBlockDim(coreNum)`；为 softmax 高阶 API 生成 tiling（kSize≤2048 用 32KB、否则 33KB tmp buffer，L1252-L1258）；调用 `SetSparseParamsRegbase` 与 `InitOutputSplit`。
- [tiling_general.cpp L1222-L1242](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_host/sparse_lightning_indexer_grad_kl_loss_enhance_tiling_general.cpp#L1222-L1242)：`InitOutputSplit`——dKeyIndex 总元素（T2·D 或 B·S2·D）按 AIV 核数均分，供设备侧清零/初始化（对应 u4-l5 讲过的「反向先清零再 ScatterAdd」套路）。

**tilingKey 与 workspace**：

- [tiling_general.cpp L1266-L1274](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_host/sparse_lightning_indexer_grad_kl_loss_enhance_tiling_general.cpp#L1266-L1274)：`GetTilingKey` 用 `GET_TPL_TILING_KEY` 把 6 个模板参数（hasRope/topKRange/layout×2/sparseMode/deterministic）编码进 tilingKey——与 u4-l5 稀疏 FA 相同的模板化 tilingKey 机制。
- [tiling_general.cpp L1276-L1302](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_host/sparse_lightning_indexer_grad_kl_loss_enhance_tiling_general.cpp#L1276-L1302)：`GetWorkspaceSize`——单核 = 乒乓×(P gather + SY gather + bmm1 + bmm2 + reluGrad + psySync)；多核再乘核数，加 ScatterAdd 缓冲（T2·D 或 B·S2·D）、loss 缓冲与 16MB 预留。字段与设备侧 `InitWorkspace`（4.3.3）一一对应，可互相核对。

**TilingData 结构**（Host 写、Device 读的契约）：

- [tiling.h L23-L40](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_tiling.h#L23-L40)：输入输出索引常量（`QUERY_INPUT_INDEX=0` … `LOSS_OUTPUT_INDEX=3`）——与 `_def.cpp` 声明顺序严格一致（呼应 u2-l2 的「Input 顺序即索引」）。
- [tiling.h L220-L238](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_tiling.h#L220-L238)：`SLIGradKLLossMultiCoreParams`——`bS1Index[MAX_CORE_NUM]`（MAX_CORE_NUM=25，L62）就是负载均衡的输出：每个 AIC 核的起始行号。
- [tiling.h L259-L265](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_tiling.h#L259-L265)：总 TilingData = baseParams + multiCoreParams + initOutputParams + vectorParams（两个 SoftMaxTiling）。

#### 4.2.4 代码实践

**实践目标**：亲手复现负载均衡，理解 `bS1Index[]` 的含义。

**操作步骤**（示例代码，纯 Python 模拟）：

1. 设 `S1=8, S2=16, K=4, aicNum=3`，按 `GetS2RealSize` 公式算出每行工作量 `w[j] = min(align_up(S2-S1+j+1, 512), K)`（本题全部会被 K 截断为 4，可把 512 对齐临时改为 1 观察未截断的阶梯）；
2. 先做均分：`split = ceil(8/3)=3`，核 0 得行 [0,3)、核 1 得 [3,6)、核 2 得 [6,8)，算每核总负载；
3. 实现简化版 `BalanceLoad`：只要挪一行能让最大核负载下降就挪；
4. 对比均分 vs 精调后的 `max(每核负载)` 与 `sum/3` 理想值的差距。

**需要观察的现象**：因 `GetS2RealSize` 的 512 对齐 + K 截断，短序列时各行工作量可能相同（负载均衡退化）；把 `S1/S2` 调大（如 S1=1024, S2=8192, K=8192）后前 511 行和后部行工作量差异明显，精调收益出现。

**预期结果**：精调后最大核负载 ≤ 均分结果；体会「为什么 kernel 不自己按 totalSize 均分，而要 Host 预先算 bS1Index」——因为行长信息在 Host 侧已知，设备侧逐行推算代价高且各核无法全局协商。纯逻辑模拟无需 NPU；「与真实 tiling 输出比对」待本地验证（可在 UT 中 dump `bS1Index`，参考 u8-l2 的 tiling UT 方法）。

#### 4.2.5 小练习与答案

**练习 1**：`topKRange` 为什么只有两档（0_2K / 2K_8K），不逐个 topk 值一档？
**答案**：tilingKey 的取值集合由 `ASCENDC_TPL_UINT_SEL` 显式枚举（见 4.3.3 的 template_tiling_key.h L39/L51，`UI_LIST, 0, 1`）；每多一档就多一组 kernel 实例编译产物。topk 影响的是 UB 分配策略（`UBAllocPolicy` 两特化，见 common.h L204-L238），两档足以覆盖 2048~8192 的规格集合，控制编译体积。

**练习 2**：`CalcTotalSize` 在 TND 与 BSND 下分别返回什么？
**答案**：TND 返回 `realT1Size`（各 batch 实际 S1 之和，即 T1），BSND 返回 `bSize * s1Size`（L1204-L1212）。两者都是「总 query 行数」——分核的基本单位是行而不是 batch。

**练习 3**：`sparse_block_size` 属性传 64 会怎样？
**答案**：def 默认值注释写「支持[1,128]且16对齐」（L107），但 docs 明确「当前仅支持 1，该参数不产生任何效果」；设备侧 `SLIGradKLLossConstInfo::sparseBlockSize` 恒为 1（common.h L137），gather/scatter 均按 1 语义实现。属性被读入 TilingData 但不改变行为。

---

### 4.3 op_kernel：模板化入口与 Cube+Vector 六阶段流水线

#### 4.3.1 概念说明

设备侧被拆成 **1 个编排类 + 3 个服务类**：

- `SparseLightningIndexerGradKLLossEnhanceBase`（base.h）：编排 Init/Process，持有三个服务对象；
- `SLIKLLossVectorService`（vector.h）：主 Vector 服务（V0/V1/V2 三个阶段）；
- `SLIKLLossVector2Service`（vector2.h）：收尾 Vector（dKeyIndex 落盘、确定性 loss 归约）；
- `SLITMatmulService`（service_cube.h）：Cube 服务（mm1/mm2/mm5/mm6）。

核型是 `KERNEL_TYPE_MIX_AIC_1_2`——1 个 AIC 配 2 个 AIV（subBlockIdx 0/1），一个 AIC 的 Cube 结果由两个 AIV 分头做向量后处理。主循环用 `runInfos[3]` 三槽环形缓冲做软件流水：第 i 轮迭代发射 V0(i)，同时处理 C1(i-1)、V1(i-1)、C2(i-1)，并排空 V2(i-2)——与 u4-l3 FA 前向的 extraInfo[3] 三槽流水同款设计。

#### 4.3.2 核心流程

```
MainProcess（非确定性路径）:
 for 每个本核负责的 (b, s1) 行:            # 边界来自 tiling 的 bS1Index[aicIdx] ~ bS1Index[aicIdx+1]
   GetRunInfo(taskId, b, s1)                # 填 runInfos[taskId%3]：行长/偏移/切分/mergeKv 判定
   runInfo0   = runInfos[taskId%3]      (i)
   runInfoNeg1= runInfos[(taskId+2)%3]  (i-1)
   runInfoNeg2= runInfos[(taskId+1)%3]  (i-2)
   AIV: CrossCoreWaitFlag(14)   / AIC: CrossCoreSetFlag(14)     # AIC↔AIV 握手
   V0:  vectorService.ProcessVector0(runInfo0)     # Gather/MergeKv：按 topk 索引聚合 key/key_index → workspace
   C1:  matmulService.ComputeMm1/ComputeMm2(runInfoNeg1)         # P 打分、S' 打分(带 ReLU)
   V1:  vectorService.ProcessVector1(runInfoNeg1)               # softmax p/q、KL loss、dI、dW、ReLUGrad
   C2:  matmulService.ComputeMm5/ComputeMm6(runInfoNeg1)         # dK 前置矩阵乘、dQ 矩阵乘
   V2:  vectorService.ProcessVector2(runInfoNeg2)               # ScatterAdd：dK 按 topk 索引累加回 scatterAddRes
 SyncAll
 vector2Service.ProcessVector2()                                # 收尾：fp32→fp16 cast 写 dKeyIndex；确定性时归约 loss
```

`DeterProcess`（deterministic=true）改为「行维度跨核轮转 + 定长循环」：每核处理 `bS1Idx = aicIdx, aicIdx+coreNum, ...`，补 2 轮空转排空流水，ScatterAdd 与 loss 走每核独立缓冲 + 固定顺序合并（u4-l4 讲过的确定性套路）。

#### 4.3.3 源码精读

**模板化入口**：

- [kernel cpp L23-L48](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance.cpp#L23-L48)：`SLI_OP_IMPL` 宏——实例化模板类、`GET_TILING_DATA_WITH_STRUCT` 解包 TilingData、按入口参数顺序传 12 输入 4 输出并 `Init + Process`。
- [kernel cpp L50-L101](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance.cpp#L50-L101)：`__global__ __aicore__` 入口。L70 `KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_MIX_AIC_1_2)` 声明 AIC:AIV=1:2 混合核；L75-L100 按 `ORIG_DTYPE_QUERY/KEY` 分 fp16/bf16 两分支——注意这里**没有 TILING_KEY_IS 长链 if/else**，tilingKey 的 6 个模板参数直接成为 C++ 模板实参（编译期由 tilingKey 选择实例，机制同 u4-l5）。
- [template_tiling_key.h L21-L44](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_template_tiling_key.h#L21-L44)：`LayoutType/TopKRange` 枚举与 `ASCENDC_TPL_ARGS_DECL` 声明 6 个模板参数的合法取值（HASROPE∈{0,1}、TOPK_RANGE/LAYOUT_QT/LAYOUT_KT∈{0,1}、SPARSE_MODE 恒 3、DETERMINISTIC∈{0,1}）；Host 的 `GET_TPL_TILING_KEY`（4.2.3）据此校验编码合法性。

**编排层 base.h**：

- [base.h L35-L130](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_base.h#L35-L130)：类声明——中间计算统一用 float（L38 注释「高精度模式」）；L98 `runInfos[3]` 三槽；L101-L103 三个服务对象成员。
- [base.h L239-L266](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_base.h#L239-L266)：`InitConstInfo`——AIV 上 `aivIdx=GetBlockIdx()`、`aicIdx=aivIdx/2`、`subBlockIdx=aivIdx%2`（1:2 混排的身份映射）；AIC 上 `aicIdx=GetBlockIdx()`。随后把 TilingData baseParams 拷进 constInfo。
- [base.h L269-L338](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_base.h#L269-L338)：`InitWorkspace`——workspace 按 `coreTotalOffset = aicIdx * 单核块` 分核；顺序排布 gatherPRes/gatherSYRes/bmm1/psySync/bmm2(+reluGm 别名)/reluGrad/bmm3/loss/scatterAdd（与 tiling `GetWorkspaceSize` 的预算字段一一对应）；L316-L336 AIV 分支把 scatterAddRes/bmm3/loss 初始化为 0（ScatterAdd 前置清零），末尾 `SyncAll()`。
- [base.h L539-L629](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_base.h#L539-L629)：`MainProcess`——L547-L548 用 `CalcMultiCoreOffset` 从 `bS1Index[aicIdx]/[aicIdx+1]` 反解本核的 (b,s1) 区间（消费 tiling 负载均衡的成果）；L552-L609 双层 b/s1 循环 + 三段流水（L580-L606，V0→(C1,V1,C2)→V2）；最后一个 batch 额外 2 轮空转排空（L564-L566）。L617-L628 收尾阶段切换到 vector2Service。
- [base.h L666-L743](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_base.h#L666-L743)：`GetRunInfo`——设备侧逐行复算：`GetActualSeqLens`（TND 下对 actual_seq_qlen 做差分）、`GetS2SparseLen`（L653-L663，与 Host `GetS2RealSize` 同公式：`Max(K_len - Q_len + s1Idx + 1, 0)`）、`s2RealSize = Min(kSize, s2SparseLen)`、k 方向按 `kBaseSize=2048` 切分出 `kLoopTimes/kTailSize`、各张量 GM 偏移、`mergeKv = (s2SparseLen > kSize)`（决定走 Gather 聚合路径还是直接稠密路径）、`calcP`（按 taskId 奇偶把 P/SY 任务分给两个 subBlock 的 AIV）。

**收尾 vector2.h**：

- [vector2.h L160-L228](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_vector2.h#L160-L228)：`ProcessVector2`——T2 维按 `avgCost` 均分到所有 AIV，乒乓搬运 scatterAddRes（fp32）→ `Cast(CAST_ROUND)` → 写 dKeyIndexGm（fp16/bf16）；L211-L227 确定性模式下 aivIdx==0 的核把各核 lossRes 求和写回 lossGm（非确定性路径直接原子加 lossGm，见 4.4.3 第 5 步）。

#### 4.3.4 代码实践

**实践目标**：验证「tiling 的 bS1Index 与 kernel 的消费互为逆操作」。

**操作步骤**：

1. 读 [base.h L392-L413](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_base.h#L392-L413) 的 `CalcMultiCoreOffset`：TND 用 `FindBIndex` 在 actual_seq 前缀里二分定位 batch，BSND 直接 `bS1Index / s1Size` 整除取模；
2. 手算一个 TND 例子：`actual_seq_qlen = [16, 32, 48]`（即 3 个 batch 长度 16/16/16，T1=48），假设 `bS1Index = [0, 20, 40]`，求 aicIdx=1 核的 `(bStart, s1Start, bEnd, s1End)`；
3. 对照 Host 侧 `SetSparseStartIdx` 的输出语义核对你的答案。

**需要观察的现象**：单核区间可能跨 batch（bStart < bEnd），`MainProcess` 因此有 L552 的 b 循环 + 每 batch 的起止行特判。

**预期结果**：aicIdx=1 核负责行 20~40 → bStart=1, s1Start=4, bEnd=2, s1End=8（跨 batch 1 的后 12 行与 batch 2 的前 8 行）。纯纸面推演即可完成。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `runInfos` 要 3 个槽而不是 2 个？
**答案**：一轮迭代同时触碰三个任务：发射 i、处理 i-1（C1/V1/C2）、排空 i-2（V2）。2 槽会让 i 的写与 i-2 的读冲突；3 槽保证任一时刻每个任务实例独占一个槽。

**练习 2**：`KERNEL_TYPE_MIX_AIC_1_2` 下 `GetBlockNum()` 返回的是 AIC 数还是 AIV 数？代码里如何区分？
**答案**：blockDim 取 AIC 核数（tiling `SetBlockDim(coreNum)`，coreNum 基于 aicNum）；AIV 侧用 `aivIdx = GetBlockIdx()`、`aicIdx = aivIdx / 2`、`subBlockIdx = aivIdx % 2`（base.h L241-L244）映射回所属 AIC 与子块身份，`GetTaskRation()` 则给出 1:2 比例（vector2.h L172 用它算 AIV 总数）。

**练习 3**：`DeterProcess` 与 `MainProcess` 的分核粒度差异是什么？
**答案**：MainProcess 每核拿**连续区间**（bS1Index[aicIdx]~[aicIdx+1]），DeterProcess 每核拿**跨核轮转的离散行**（`bS1Idx 从 aicIdx 开始步进 coreNum`，base.h L452）——轮转使各核迭代次数一致、每步任务同构，ScatterAdd/loss 合并顺序固定，从而 bit 级可复现。

---

### 4.4 vector.h：KL Loss 与三路梯度的向量实现

#### 4.4.1 概念说明

`SLIKLLossVectorService` 是本算子的数学核心。两个 AIV 子块分工：`calcP` 为真者算目标分布 P 链路，另一者算预测分布 SY 链路（`GetRunInfo` 里按 taskId 奇偶轮换），算完通过 `psySyncGm` workspace 交换结果——因为 \( dI = q - p \) 需要两条链路的结果在同一核内相遇。之后每个 kLoop（2048 一段）执行 `VectorDwDqDk`（dW/dS'）与 `VectorLoss`（loss 累加）。

关键设计：**\( p \) 的 softmax 复用主注意力 FA 保存的 softmax_max/softmax_sum**。`SimpleSoftMax` 高阶 API 允许外部传入已知的 max/sum，免去在线归约——正是 u4-l4「反向 FA 复用前向中间量」思想的再现，只是这里复用的是**另一个算子（FA 前向）**的输出。

#### 4.4.2 核心流程

```
ProcessVector1(runInfo):
  PreloadWeight          # weight 搬入 UB 并 cast 成 fp32（常驻，乒乓两份）
  if calcP:  VectorP     # 目标分布 p：bmm1(q@kᵀ)*scale → SimpleSoftMax(用FA的max/sum) → Σ_G → /G → psySync[0:K]
  else:      VectorSy    # 预测分布 q：bmm2(qi@kiᵀ 已带ReLU) * w → Σ_Gindex → softmax(max/exp/sum/div) → psySync[K:2K]
  for kLoop in kLoopTimes:
      VectorDwDqDk       # 读对侧 psySync: dI=q-p → dW=Σ_k dI·ReLU; dS'=dI·w·1[S'>0] 写 reluGradRes
      VectorLoss         # loss += Σ p·(log p - log q)，原子加到 lossGm / lossResGm
```

#### 4.4.3 源码精读（公式 ↔ 代码逐条对照）

**第 0 步：Gather/MergeKv（V0 阶段）**。[vector.h L406-L447](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_vector.h#L406-L447)：`ProcessVector0` 对 key（含 rope，576 维拼接送 mm1）与 key_index（128 维送 mm2）各跑一遍 `MergeKv`——按 `sparse_indices` 查 `GetRealS2Idx`（L634-L643）把选中的 key 行聚合到 workspace 连续区，供 Cube 做规则矩阵乘；`runInfo.mergeKv` 为假（因果长度 ≤ K）时跳过，Cube 直接按原位稠密取数（对应 4.5 的 Dense/Sparse 双路径）。

**第 1 步：目标分布 p（VectorP）**。[vector.h L1185-L1285](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_vector.h#L1185-L1285)：

- L1219 搬入 mm1 结果（`q@kᵀ`，fp32）；L1222 `Muls(scaleValue)` 完成 \( \text{scale}\cdot q k^T \)；
- L1239-L1242 从 **softmaxMaxGm/softmaxSumGm**（FA 前向输出）搬入该 token 的 max/sum，L1245-L1246 `Brcb` 广播到每行；
- L1250-L1259 `SimpleSoftMax` 借外部 max/sum 直接得 softmax 概率；
- L1262-L1271 `ReduceSum` 沿 N（G 个头）累加，L1277-L1278 `Muls(1/G)`——即 \( p = \frac{1}{G}\sum_g \text{softmax}(\cdot) \)；
- L1283 写 `psySyncGm[0:K]` 供对侧核读取。

**第 2 步：预测分布 q（VectorSy）**。[vector.h L1318-L1419](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_vector.h#L1318-L1419)：

- L1348 搬入 mm2 结果（`qi@kiᵀ`，Cube 侧 Fixpipe 已开 `reluEn`，见 4.5，故此处即 ReLU(S')）；
- L1352-L1359 逐行乘 `weightValue`（scalar 读 UB）；L1362-L1377 `ReduceSum` 沿 G_index 归约——即 \( I = \sum_{g'} w_{g'} \text{ReLU}(S'_{g'}) \)；
- L1393-L1411 手写 softmax 三件套：`ReduceMax` → `Sub` → `Exp` → `ReduceSum` → `Div` 得 \( q = \text{Softmax}(I) \)；
- L1417 写 `psySyncGm[K:2K]`。

**第 3 步：dI 与三路梯度（VectorDwDqDk）**。[vector.h L1469-L1599](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_vector.h#L1469-L1599)：

- L1492-L1502 按 `calcP` 从 `psySyncOtherGm` 读**对侧**结果（P 核读 q、SY 核读 p）——\( p \) 与 \( q \) 在此汇合；
- **L1507 `Sub(v1TmpUb, reduceSumYUbSingleK, reduceSumPUbSingleK, kRealSize)` 就是 \( dI = q - p \)**（文档 L41-L43 公式的落点）；
- L1528-L1544 循环每个 index 头：L1536 `Mul(dI, ReLU(S'))` 后 `Sum` 沿 K 归约得 \( dW \)（L1539-L1544，首个 kLoop 直写、后续累加，L1562-L1564 合并）；L1571-L1592 cast 后按 layout 偏移写出 dWeightGm；
- L1547-L1550 `Muls(dI, weight[n])` 得 \( dI \cdot W_n \)，L1553 调 `ReLUGrad`（[L1422-L1443](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_vector.h#L1422-L1443)：`CompareScalar(GT 0)` 生成 0/1 掩码 → `Select` 截断 → `Cast`），得 \( dS' = dI \cdot W \cdot \mathbb{1}[S'>0] \) 写 reluGradResGm——正是 golden.py L288 的 `relu_grad = q_sub_p * weight * (reluRes > 0)`。

**第 4 步：ScatterAdd（V2 阶段）**。[vector.h L1076-L1183](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_vector.h#L1076-L1183)：`ProcessVector2`——两个 AIV 各分一半 K，读 mm5 结果（\( dS'^T Q_{index} \)，[K, D]），`SetAtomicAdd<T>` 后按 `GetRealS2Idx` 反查真实 key 行号 `DataCopyPad` 累加进 scatterAddRes（L1112-L1113/L1182）；索引乱序、stride 溢出等场景退化为逐条 `ScatterAddCopyOutSingle`（L743-L751）。mergeKv 为假时本阶段直接返回（L1079-L1081），dK 由 Cube 侧 mm5 原子 Fixpipe 完成（见 4.5）。

**第 5 步：KL loss（VectorLoss）**。[vector.h L1602-L1713](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_vector.h#L1602-L1713)：

- L1644-L1654 两个 AIV 各分一段 K，从 resPSYTBuf/对侧 mm2TBuf 取回 \( p \) 与 \( q \)；
- **L1656-L1658 `Maxs(..., 1e-8)` 对 p、q 双双防零**（与 golden.py L273-L275 一致）；
- **L1661-L1662 `Log`，L1664 `Sub(log p - log q)`，L1666 `Mul(× p)`，L1668-L1684 `Sum` 累加**——四条指令就是 \( \sum_k p_k(\log p_k - \log q_k) \)；
- L1688-L1712 最后一个 kLoop 时开 `SetAtomicAdd<float>`，非确定性直加 lossGm，确定性写本核 lossResGm[aivIdx]（由 vector2 收尾求和，见 4.3.3）。

**调度层**。[vector.h L712-L740](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_vector.h#L712-L740)：`ProcessVector1`——等 Cube 的 C1 两个 flag → `PreloadWeight` → `calcP ? VectorP : VectorSy` → kLoop 循环 `VectorDwDqDk`（末轮通知 Cube 开始 mm5/mm6）+ `VectorLoss`。

**UB 分配**。[common.h L204-L238](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_common.h#L204-L238)：`UBAllocPolicy` 按 TopKRange 两档特化 13 块 UB 缓冲（gather/mm1/mm2/shared/resPSY/…），tilingKey 的 TOPK_RANGE 参数在这里兑现成不同的 UB 布局。

#### 4.4.4 代码实践（本讲核心实践）

**实践目标**：把 4.1 推导的每条公式钉到 `vector.h` 的具体行号，并理清 lightning indexer 家族的训练链路。

**操作步骤**：

1. 完成 4.1.4 的 autograd 验证脚本并跑通；
2. 填写下面的「公式 ↔ 实现对照表」（答案已给，请自行到源码点击行号核对）：

| 公式 | vector.h 实现位置 | 关键指令 |
| --- | --- | --- |
| \( p = \frac{1}{G}\sum_g \text{softmax}(\text{scale}\cdot qk^T) \) | [L1185-L1285](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_vector.h#L1185-L1285) | Muls→SimpleSoftMax→ReduceSum→Muls(1/G) |
| \( q = \text{Softmax}(\sum_{g'} w_{g'} \text{ReLU}(S')) \) | [L1318-L1419](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_vector.h#L1318-L1419) | Muls(w)→ReduceSum→ReduceMax/Exp/ReduceSum/Div |
| \( L = \sum p(\log p - \log q) \) | [L1602-L1713](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_vector.h#L1602-L1713) | Maxs(1e-8)→Log→Sub→Mul→Sum→AtomicAdd |
| \( dI = q - p \) | [L1507](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_vector.h#L1507) | Sub |
| \( dW = dI @ \text{ReLU}(S')^T \) | [L1536-L1544](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_vector.h#L1536-L1544) | Mul→Sum（沿 K 归约） |
| \( dS' = dI \cdot W \cdot \mathbb{1}[S'>0] \) | [L1547-L1554](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_vector.h#L1547-L1554) + ReLUGrad [L1422-L1443](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_vector.h#L1422-L1443) | Muls(w)→CompareScalar(GT)→Select→Cast |
| dK 的 ScatterAdd | [L1076-L1183](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_vector.h#L1076-L1183) | SetAtomicAdd→DataCopyPad |

3. 画出家族训练链路时序（一步训练）：

```
① lightning_indexer_enhance(前向)      → sparse_indices [B,S1,Nidx2,K]      (u4-l6)
② FA 前向(可稀疏)                       → softmax_max / softmax_sum (fp32)   (u4-l1/u4-l5)
③ 本算子(反向+Loss 融合)  ←──────────── 消费 ①的 indices + ②的 max/sum
   输出: d_query_index / d_key_index / d_weight / loss
④ 优化器用三路梯度更新 indexer 的 q_index/k_index/weights
```

**需要观察的现象**：第 2 步表格中每条公式都能在 100 行以内的函数里找到唯一落点；第 3 步链路里 ③ 同时依赖 ①（索引）与 ②（softmax 中间量），解释了 _def 里 12 个输入的来历。

**预期结果**：对照表与源码吻合；链路图能回答「softmax_max/sum 为什么是必选输入」。autograd 部分已在 4.1.4 完成；在真实 NPU 上与本算子输出比对属于 ST 范畴（可跑 [golden.py L316-L359](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/tests/st/lightning_indexer_klloss_golden.py#L316-L359) 的 TND 示例，注意其顶部 `import torch_npu/torchair` 需 NPU 环境，纯 CPU 需先去掉这两个 import——待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：`psySyncGm` 为什么大小是 `kSize * 2 * sizeof(float)` 且两个 AIV 各写一半？
**答案**：它承载 \( p \)（前 K）与 \( q \)（后 K）两份分布，分别由 calcP 核与 SY 核写入（VectorP L1283 写 `psySyncGm` 起始、VectorSy L1417 写 `psySyncGm[kSize]`）；dI 的计算核各需读对侧那份，故通过这片共享 workspace 完成核间交换（tiling `GetWorkspaceSize` 的 `psySyncSize = kSize*2*sizeof(float)` 与之对应）。

**练习 2**：`VectorLoss` 里为什么非确定性路径直接原子加 `lossGm`，确定性路径却先写 `lossResGm[aivIdx]`？
**答案**：原子浮点加法的合并顺序不确定，浮点非结合会导致每次运行 bit 不同；确定性模式把每核部分和存独立槽位，收尾由 vector2 的 aivIdx==0 按固定顺序 `Sum` 再写 lossGm（vector2.h L211-L227），保证可复现。

**练习 3**：`VectorP` 用 `SimpleSoftMax` 而 `VectorSy` 手写 max/exp/sum/div，为什么？
**答案**：VectorP 复用 FA 前向保存的 max/sum（不需要再归约 max/sum），`SimpleSoftMax` 恰好接受外部 max/sum 参数；VectorSy 的输入 \( I \) 没有现成中间量，必须在线算 max 与 sum，所以手写完整 softmax。

---

### 4.5 service_cube.h：四次 Mmad 的布局、乒乓与双路径

#### 4.5.1 概念说明

`SLITMatmulService` 不用高阶 Matmul API，而是**手写五级流水**（MTE2→MTE1→M→FIX）管理 L1/L0A/L0B/L0C，换来对小形状矩阵的极致控制。四次矩阵乘的数学角色：

| 矩阵乘 | 计算 | 形状（M×N，K 为归约轴） | 输出去向 |
| --- | --- | --- | --- |
| mm1 | \( \text{scale}\cdot q @ k_{topk}^T \)（P 打分） | G×K，K 轴=D(512+64) | bmm1Res → VectorP |
| mm2 | \( q_{index} @ k_{index,topk}^T \)（S' 打分，Fixpipe 开 ReLU） | G_index×K，K 轴=128 | bmm2Res（已 ReLU）→ VectorSy |
| mm5 | \( dS'^T @ q_{index} \)（dK 前置） | K×D，K 轴=G_index | mm5Res/scatterAddRes → V2 ScatterAdd |
| mm6 | \( dS' @ k_{index,topk} \)（dQ） | G_index×D，K 轴=K | dQueryIndexGm（直出） |

#### 4.5.2 核心流程

```
MmadInner（单次矩阵乘内核）:
  L1(A/B) --LoadData--> L0A/L0B --Mmad--> L0C --Fixpipe--> GM/UB
       ↑ 三组事件同步 MTE1_MTE2 / M_MTE1 / FIX_M，L0A/L0B/L0C 各 2 份乒乓
```

mm1 有 Dense/Sparse 双路径（`runInfo.mergeKv` 分派）：长序列（因果长度 > K）必须走 V0 聚合过的 workspace（Sparse），短序列直接按原位 key 稠密取数（Dense），省掉 gather 开销——与 u4-l5 稀疏 FA 的 C_TEMPLATE/V_TEMPLATE 分野一脉相承。

#### 4.5.3 源码精读

- [service_cube.h L25-L35](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_service_cube.h#L25-L35)：`MMParam`——单次 Mmad 的 M/N/K、左右转置、L0C 累积、原子加等开关，四次 mm 全靠改这份参数复用同一套 `MmadInner`。
- [service_cube.h L319-L351](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_service_cube.h#L319-L351)：`InitBuffers`——L1 布局：query 常驻（576×128，含 rope 拼接）、queryIndex/keyIndex 双缓冲、reluGrad 缓冲；L0A/L0B 各 32K、L0C 64K 双乒乓。
- [service_cube.h L514-L545](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_service_cube.h#L514-L545)：`MmadInner`——事件握手后 `LoadData` A/B 进 L0、`Mmad` 进 L0C；`isLeftTranspose/isRightTranspose` 决定装载方式；`cmatrixInitVal` 控制 L0C 首算清零还是累加（多段 K 归约时的累加器复用）。
- [service_cube.h L674-L763](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_service_cube.h#L674-L763)：`ComputeMm1Sparse`——query（含 rope）首批搬 L1 常驻；外层按 `s2LoopTimes`（1024 一段）等 V0 的 gather flag（L708 `CrossCoreWaitFlag(SYNC_V0_TO_C1_P_FLAG)`），内层 128 一段做 mm；D 轴 128 分片在 L0C 累积（`isL0CAccum`）后一次 Fixpipe 到 bmm1Res；结束发 `SYNC_C1_TO_V1_P_FLAG` 唤醒 VectorP。Dense 版（[L580-L671](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_service_cube.h#L580-L671)）结构相同但 B 矩阵直接从 keyGm/keyRopeGm 原位取；[L765-L772](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_service_cube.h#L765-L772) 按 `mergeKv` 分派。
- [service_cube.h L774-L851](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_service_cube.h#L774-L851)：`ComputeMm2`——同构的 S' 打分；**L840 `fixpipeParams.reluEn = true`**：ReLU 直接在 Fixpipe 出 L0C 时做掉，VectorSy 搬到的已是 ReLU(S')，省一次向量 pass。
- [service_cube.h L853-L922](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_service_cube.h#L853-L922)：`ComputeMm5`——\( dS'^T @ q_{index} \)：A=reluGrad（左转置，[K,G_index]）、B=query_index（右转置），得 [K,D]；等 V1 的 `SYNC_V1_TO_C2_DW_FLAG`（L868）；**L872-L882 输出目标三选一**：确定性或 mergeKv→写 mm5Res 等待 V2 做 ScatterAdd；非 mergeKv→直连 `scatterAddResGm` 并 `isAtomicAdd=true` 原子 Fixpipe（此时 V2 直接 return，见 4.4.3 第 4 步）。
- [service_cube.h L924-L1012](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_service_cube.h#L924-L1012)：`ComputeMm6`——\( dS' @ k_{index,topk} \)：与 mm5 复用 L1 里的 reluGrad 与 queryIndex（`IS_RELUGRAD_REUSE`，topKRange=0_2K 时 mm6 免重搬，L947-L953/L1007-L1009）；K 轴多段在 L0C 累积；L993-L998 Fixpipe 出栈时按 OUT_T 选 `F322F16/F322BF16` 量化，直接产出 fp16/bf16 的 dQueryIndex。
- [service_cube.h L548-L577](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_service_cube.h#L548-L577)：`ScatterAdd`——Fixpipe 出 L0C 的封装，可选原子加。

#### 4.5.4 代码实践

**实践目标**：核对四次 Mmad 的形状声明与数学定义一致。

**操作步骤**：

1. 打开 [service_cube.h L853-L866](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_service_cube.h#L853-L866)（mm5 参数）与 [L924-L935](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/op_kernel/sparse_lightning_indexer_grad_kl_loss_enhance_service_cube.h#L924-L935)（mm6 参数）；
2. 用文档典型值（G_index=64、K=2048、D=128）代入：mm5 是 `[K,D] 归约 G_index`（singleM=K 分片、singleN=D、singleK=G_index、双转置），mm6 是 `[G_index,D] 归约 K`（singleM=G_index、singleN=D、singleK=K 分片、右转置）；
3. 对照 golden.py 的 [L291](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/tests/st/lightning_indexer_klloss_golden.py#L291)（`d_query_index = relu_grad @ key_index_topk`）与 [L292](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/sparse_lightning_indexer_grad_kl_loss_enhance/tests/st/lightning_indexer_klloss_golden.py#L292)（`beforeScatterAdd = relu_grad^T @ query_index`）确认转置方向。

**需要观察的现象**：`isLeftTranspose/isRightTranspose` 的组合不是随意的——mm5 需要 \( dS'^T \)（左转置）把 K 换到 M 轴，mm6 需要 \( k_{index} \)（右转置）把 K 放进归约轴；转置由 `LoadData` 的 `ifTranspose` 硬件能力在装载 L0 时免费完成。

**预期结果**：四次 mm 的 (M,N,K) 与 4.5.1 表格、golden 公式三方一致。纸面推演即可；若要在 UT 里验证，可参照 `tests/ut/op_host/` 下已有的 tiling/infershape 用例（本算子无 kernel UT），「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：mm2 的 ReLU 放在 Fixpipe 而不是 Vector 里做，省了什么？
**答案**：省一次「L0C→GM 搬运 + GM→UB 搬运 + 向量 pass」；Fixpipe 硬件支持出栈激活，ReLU 与数据落盘同拍完成，VectorSy 搬到的直接是 ReLU(S')。

**练习 2**：为什么 mm5/mm6 能复用同一份 L1 里的 reluGrad 和 queryIndex？
**答案**：两者都以 \( dS' \)（reluGradRes）为一个操作数、且 queryIndex 已在 mm2 时驻留 L1；`IS_RELUGRAD_REUSE`（topKRange=0_2K 时为真）用事件 `RELU_GRAD_EVENT` 保证 mm6 读完前 mm5 不覆盖，L1 空间紧张的 2K~8K 档则退回重搬。

**练习 3**：非确定性 + 非 mergeKv 时，dK 的原子累加发生在哪一级？
**答案**：直接在 Cube 侧——mm5 的输出目标设为 `scatterAddResGm` 且 `isAtomicAdd=true`（L880-L881），Fixpipe 落盘即原子加；V2 的向量 ScatterAdd 被跳过（vector.h L1079-L1081）。这是「能用 Cube 原子写就不麻烦 Vector」的路径选择。

---

## 5. 综合实践

**任务：给本算子写一页「数值与实现双核对」笔记，并跑通一次 CPU 参考链路。**

1. **公式推导**：不看本讲，从 \( L=\sum p\log(p/q) \) 出发手推 \( dI=q-p \) 与三路梯度（对照 4.1.2 查漏）。
2. **数值验证**：跑 4.1.4 的 autograd 脚本，把三个 `allclose` 的实际输出贴进笔记。
3. **golden 链路**：复制 `tests/st/lightning_indexer_klloss_golden.py`，去掉顶部的 `import torch_npu/torchair`（仅 CPU 时），把 `__main__` 的 TND 用例规模改小（如 `headDimP=576, headDimSY=128, indexTopk=20` 不变、序列改短），运行 `python lightning_indexer_klloss_golden.py`，观察打印的各中间量 shape 是否与 4.4 的链路一致（待本地验证：脚本含 NPU 相关 import 与 `.npu()` 隐含假设，纯 CPU 可能需进一步裁剪）。
4. **实现定位**：完成 4.4.4 的对照表，逐行点开 permalink 核对。
5. **链路总结**：用一张图回答—— indexer 家族在一步训练中的调用顺序是什么？本算子的 12 个输入分别由谁产出？（答案：`lightning_indexer_enhance` 产 sparse_indices；FA 前向产 softmax_max/sum；query/query_rope/key/key_rope/query_index/key_index/weight 是 indexer 自身参数与前向输入；actual_seq_* 是变长元数据。）

## 6. 本讲小结

- LightningIndexer 用 KL 散度 \( D_{KL}(p \| \text{Softmax}(I)) \) 单独训练，其反向梯度链的核心是 \( dI = \text{Softmax}(I) - p \)（等价于 \( q(1-p/q) \)），loss 与三路梯度（dW/dQ_index/dK_index）因此融合进同一个算子。
- 目标分布 \( p \) 复用 FA 前向保存的 softmax_max/softmax_sum（`SimpleSoftMax` 带外部 max/sum），预测分布 \( q \) 由「Fixpipe 内置 ReLU 的 mm2 → 乘 W → G_index 归约 → 手写 softmax」在线计算；两分布在 `psySyncGm` workspace 交换后在 `VectorDwDqDk` 的 `Sub` 处汇合成 \( dI \)。
- tiling 的独门功夫是**按因果行长的负载均衡**：`InitSparseValidArray` 逐行估工作量，粗调 + `BalanceLoad` 双向精调产出 `bS1Index[]`，kernel 侧 `CalcMultiCoreOffset` 消费它反解本核行区间。
- 设备侧是 AIC:AIV=1:2 混合核上的六级流水：V0(Gather/MergeKv) → C1(mm1/mm2) → V1(softmax/loss/dW/ReLUGrad) → C2(mm5/mm6) → V2(ScatterAdd) → vector2(cast 落盘/loss 归约)，由 `runInfos[3]` 三槽环形缓冲驱动，确定性模式改跨核轮转 + 固定顺序合并。
- Cube 侧手写 L1/L0A/L0B/L0C 五级流水完成四次 Mmad，`MMParam` 一套参数四处复用；mm1 有 Sparse（走 gather workspace）/Dense（原位取 key）双路径，mm5/mm6 共享 reluGrad 与 queryIndex 的 L1 驻留。
- 接口以 `_def.cpp` + tiling 双重校验为准：sparse_mode 仅 3、D=512/D_index=128/Drope=64、N2=1(MQA)、topk∈{2048..8192} 且 1024 倍数；rope 在 def 里 OPTIONAL 但 tiling 强制存在——文档、def、tiling三方互证才能读准约束。

## 7. 下一步学习建议

- **补齐家族拼图**：回看 u4-l5 的 `sparse_flash_attention_enhance` tiling/kernel，体会本算子产出的 `sparse_indices` 在下游如何被消费（C_TEMPLATE/V_TEMPLATE 取数）。
- **进入 MHC 单元（u5）**：换一个算法族，对比 Sinkhorn 归一化的反向如何同样「保存中间量复用」（u5-l2 的 norm_out/sum_out 与本讲 softmax_max/sum 的复用是同构设计）。
- **测试视角**：读本算子 `tests/ut/op_host/` 的 tiling/infershape 用例与 `tests/st/test_npu_sparse_lightning_indexer_grad_klloss_enhance.py`，为 u8 单元（UT/ST 体系）做铺垫；尝试按 u8-l2 的方法为本算子补一个负载均衡断言用例。
- **源码延伸**：对比 `lightning_indexer_enhance` 前向的 `service_cube/service_vector`（u4-l6）与本讲的 `service_cube/vector`，标出前反向之间真正复用与重写的函数，体会「反向不是前向的镜像，而是围绕梯度链的重新组织」。
