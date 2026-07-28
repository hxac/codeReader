# MLA 解码与 combine kernel

## 1. 本讲目标

本讲承接 u4-l3（解码场景的 split-kv 与 combine kernel），进入一个特殊且重要的解码变体——**MLA（Multi-head Latent Attention）解码**。学完本讲，读者应该能够：

- 理解 MLA 的 q/kv 结构：为何一个「总维度」`D` 会被拆成内容维 `DV` 和位置编码维 `D_pe`，以及为什么 K、V 共用同一份潜变量（`kv_shared`）。
- 掌握 `lower_decode_mla.py` 如何用 `dimqk - dimv` 推导出 `PE_DIM`，并把 `DIM`/`PE_DIM` 注入手写模板。
- 逐步说清 combine kernel 如何把多个 split 的 `lse` 用「`lse_max → exp → sum → log`」合并成每个 split 的 `o_scale`，并把符号描述（`OnlineSoftmax.combine`）与手写 kernel（`mla_decode_tl.py`）逐行对应。

## 2. 前置知识

在进入 MLA 之前，请确认你已经掌握以下概念（本讲不再重复细节）：

- **解码场景与 split-kv（u4-l3）**：当 `q_seqlen < kv_len` 时，Query 方向几乎没有并行度，于是把 KV 序列切成 `num_split` 段、用 grid 第三维 `sid` 换并行。每个 split 只扫一段 KV，因此 `final_rowscales`（`lse`）必须各自落盘，全局形状多出一个 `num_split` 维；最后由一个 **combine kernel** 把多段部分结果合并。
- **OnlineFunc 的四段方法（u2-6）**：`online_fwd`（循环内递推）/ `online_fwd_epilogue`（收尾算 `o` 与 `lse`）/ `forward`（反向重算）/ `backward`（算 `dscores`）。本讲新增第五个方法 **`combine`**，专门负责 split-kv 的合并。
- **换底技巧（u2-l4）**：GPU 上 `exp(x)` 常写成 `exp2(x * log2e)`、`log(x)` 写成 `log2(x) * ln2`，以借用快速的二进制指数指令。MLA 解码模板里所有指数/对数都落在 **log2 域**，这是理解 combine 代码的关键。

> 名词速查：**MLA**（DeepSeek 提出的潜注意力）把 KV 压缩成一份共享潜变量，外加一份小的旋转位置编码部分；**kv_shared** 在本框架里特指「K 与 V 共用同一份潜变量、并带一段 PE」的解码路径。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `attn_script/mla_decode.py` | 用户层示例。定义 `score_mod`、`OnlineSoftmax`（含 `combine`），用 `kv_shared=True` 构造引擎并调用 `mod(q, q_pe, KV, k_pe)`。 |
| `attention_engine/core/lower/lower_decode_mla.py` | MLA 解码的降级函数。极简：只填 `DIM`/`PE_DIM` 等形状字段，渲染手写模板，**不做**符号降级。 |
| `attention_engine/core/template/tl_template/attn/mla_decode_tl.py` | 手写的 TileLang 模板。含 `flash_attn`（单 kernel）、`flash_attn_split`（split-kv）、`combine`（合并）三个 macro。 |
| `attention_engine/attn_engine/attn_engine.py` | 引擎入口。`_select_lower_template` 在 `kv_shared=True` 时优先分发到 `lower_decode_mla`；`OnlineFunc` 基类定义了 `combine` 的默认签名。 |

> 一个贯穿本讲的关键认知：**MLA 解码模板是手写的**，不走 `lower_score_mod`/`lower_online_func` 那条符号降级链。用户的 `OnlineSoftmax.combine` 是「意图描述」，模板里的 `combine` macro 是「性能实现」，二者通过数学等价性对应——这正是本讲要打通的对应关系。

## 4. 核心概念与源码讲解

### 4.1 MLA 结构与 kv_shared

#### 4.1.1 概念说明

标准注意力里，每个头的 K 和 V 是两份独立的张量。MLA 的核心节省在于：**把 KV 压缩成一份共享的潜变量**——同一份 `KV` 张量既当 Key（参与打分 `q @ k^T`），又当 Value（参与加权求和 `p @ v`）。这样 KV cache 的体积大幅缩小，对解码（每个 token 都要读全部 KV cache）尤其友好。

但只有内容潜变量还不够：注意力打分还需要**位置编码**（rotary PE）来区分 token 顺序。MLA 的做法是给 Q 和 K 各配一段**单独的、低维的位置编码部分** `q_pe`/`k_pe`，它只参与打分、不参与输出。于是整个 MLA 解码的数据结构是：

- 内容部分：`Q`（dim = `DV`）、`KV`（dim = `DV`，K/V 共用）
- 位置部分：`Q_pe`（dim = `D_pe`）、`K_pe`（dim = `D_pe`，只用于打分）

总维度满足 `D = DV + D_pe`（示例里 `576 = 512 + 64`）。引擎里只要看到 `kv_shared=True`，就走这条 MLA 解码路径。

#### 4.1.2 核心流程

引擎在 `_select_lower_template` 里按下标抽取形状后，**第一个判定就是 `kv_shared`**，优先级最高：

```text
抽取 q_seqlen / kv_len / head / head_kv
if kv_shared:                      # ① MLA 解码（本讲）
    lower_decode_mla(...)  → 返回 (tl_code, None)
elif q_seqlen != kv_len and head > head_kv:   # ② GQA 解码
elif q_seqlen != kv_len and head == head_kv:  # ③ MHA 解码
elif q_seqlen == kv_len and head == head_kv:  # ④ MHA 训练
elif q_seqlen == kv_len and head > head_kv:   # ⑤ GQA 训练
```

注意 MLA 分支返回的 block_mask 是 `None`——因为解码期 `q_seqlen=1`，掩码退化为逐 KV token 的一维稠密向量，且 MLA 示例 `mask_mod=None`（全attend）。

#### 4.1.3 源码精读

引擎把 `kv_shared` 置于分发优先级最高位，并传入 `headq`（Q 头数）与 `head_kv`（KV 头数，MLA 通常为 1）：

[attention_engine/attn_engine/attn_engine.py:L234-L250](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L234-L250) —— `kv_shared=True` 时优先导入并调用 `lower_decode_mla.lower_tl`，把 batch / headq / head_kv / kv_len / dimqk / dimv 依次传入，返回 `(tl_code, None)`。

用户侧用 `kv_shared=True` 构造引擎，`mod` 接收 **4 个张量** `q, q_pe, KV, k_pe`，正好对应「内容 + 位置」的拆分：

[attn_script/mla_decode.py:L88-L108](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mla_decode.py#L88-L108) —— 主程序声明 `D=576`、`DV=512`，`qkv_meta` 里 q/k 的声明维度为 `D`、v 的维度为 `DV`，并用 `kv_shared=True` 构造 `AttentionEngine`。

[attn_script/mla_decode.py:L110-L118](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mla_decode.py#L110-L118) —— 运行期把内容向量 `q`(dim=DV) 与位置向量 `q_pe`(dim=D-DV) 分开传入；`KV` 既作 K 又作 V，`k_pe` 只作打分用的位置 Key。

模板里最能体现「kv_shared」的一处：**同一个 `KV_shared` 被用了两次**——先用转置做打分（当 K），再用非转置做输出累加（当 V）。以 split 版为例：

[attention_engine/core/template/tl_template/attn/mla_decode_tl.py:L150-L172](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/mla_decode_tl.py#L150-L172) —— 第一个 `T.gemm(Q_shared, KV_shared, acc_s, transpose_B=True)` 把 KV 当 Key 算分数；循环结束后 `T.gemm(acc_s_cast, KV_shared, acc_o)` 又把**同一份** `KV_shared` 当 Value 累加进输出。这就是「K/V 共享」在代码层面的直接证据。

> 小提示：`kv_group_num = heads // kv_head_num`，MLA 解码断言 `kv_head_num == 1`，即所有 Q 头共享**同一个** KV 头，由 `cur_kv_head = by // (kv_group_num // block_H)` 把 head-block 映射到那唯一的 KV 头。

#### 4.1.4 代码实践

1. **实践目标**：从代码层面确认「KV 共享」与「4 张量输入」。
2. **操作步骤**：打开 `mla_decode_tl.py` 的 `flash_attn_split`，在 `KV_shared` 被使用处各做一处标记；再打开 `mla_decode.py` 的 `__main__`，确认 `mod(q, q_pe, KV, k_pe)` 的实参顺序与模板形参 `Q, Q_pe, KV, K_pe` 一致。
3. **需要观察的现象**：`KV_shared` 在打分 gemm（`transpose_B=True`）和输出 gemm（无 transpose）中是**同一个 shared buffer**；而 `K_pe_shared` 只出现在打分 gemm，从不参与输出。
4. **预期结果**：你会得出结论——MLA 的输出维度等于 `DV`（内容维），位置维 `D_pe` 完全不出现在输出里。
5. 若无法在 GPU 上运行，「待本地验证」可改为纯阅读型：在 `ref_program` 里找到 `query = torch.concat([q, q_pe], dim=-1)` 与 `out = einsum(attention, kv, ...)`，确认 `kv` 同时充当 key 与 value，而 `k_pe` 拼进 key 后**不再**用于求 `out`。

#### 4.1.5 小练习与答案

- **练习 1**：如果把 `kv_head_num` 从 1 改成 2（其余不变），模板里哪一行会首先报错？
  - **答案**：`assert kv_head_num == 1, "kv_head_num must be 1"`（`mla_decode_tl.py` 第 35 行）。当前模板假设所有 Q 头共享唯一一个 KV 头。
- **练习 2**：为什么 MLA 解码对 KV cache 友好？
  - **答案**：因为 K/V 共用同一份潜变量 `KV`（内容维 `DV`），需要缓存的只有 `KV` 与一段很小的位置 `k_pe`（`D_pe` ≪ `DV`），相比分别存 K 和 V 体积近乎减半；解码每步都要全量读 KV cache，省下的直接变成显存带宽收益。

---

### 4.2 PE 维拆分：DIM 与 PE_DIM

#### 4.2.1 概念说明

MLA 打分不是一次 gemm 算完，而是**两段拼接**：

\[ \text{score} = \underbrace{Q \cdot KV^{\top}}_{\text{内容打分，维 }DV} + \underbrace{Q_{pe} \cdot K_{pe}^{\top}}_{\text{位置打分，维 }D_{pe}} \]

内容部分用「大头」维 `DV` 算语义相似度，位置部分用「小头」维 `D_pe` 注入顺序信息。两段都加到同一个 `acc_s` 累加器里，后续的 online softmax 一视同仁。

框架用两个模板字段描述这次拆分：

- `DIM`：内容维，等于 `dimv`（即 v 的维度 `DV`）。它同时是 Q 内容维、KV 维，**也是输出维**。
- `PE_DIM`：位置维，等于 `dimqk - dimv`（即 `D - DV`）。只参与打分。

#### 4.2.2 核心流程

降级函数 `lower_decode_mla.lower_tl` 极其精简——它**不调用** `lower_score_mod`/`lower_online_func`，只是把形状字段算好、灌进手写模板：

```text
输入: dimqk = qkv_meta[0].shape[3]   # 声明的 q 总维 = D
      dimv  = qkv_meta[2].shape[3]   # v 维 = DV
计算: DIM    = dimv                   # 内容维
      PE_DIM = dimqk - dimv           # 位置维 = D - DV
渲染: mla_decode_tl.py  （填充 BATCH/HEADS/KV_HEAD_NUM/KV_CTX/DIM/PE_DIM）
```

这里有一个**容易踩坑的不对称**：`qkv_meta[0]` 声明的 q 维是「总维 `D`」，但运行期真正传入的 `Q` 内容张量维是 `DV`（`PE_DIM` 那部分作为独立的 `q_pe` 传入）。也就是说，`dimqk` 只是用来**推导** `PE_DIM`，模板里的 `Q` 形参用的是 `dim = DIM = DV`。

#### 4.2.3 源码精读

降级函数把 `DIM=str(dimv)`、`PE_DIM=str(dimqk-dimv)` 写进 `lowerOutput`：

[attention_engine/core/lower/lower_decode_mla.py:L25-L38](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_decode_mla.py#L25-L38) —— `DIM=str(dimv)`、`PE_DIM=str(dimqk-dimv)`，连同 `BATCH/HEADS/KV_HEAD_NUM/KV_CTX` 一起渲染 `mla_decode_tl.py` 模板；注意它**没有**调用任何 `lower_*` 符号降级函数。

模板里 `flashattn` 的 scale 用 `dim + pe_dim` 做归一化，并预先乘上 `log2e`（换底）：

[attention_engine/core/template/tl_template/attn/mla_decode_tl.py:L27-L44](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/mla_decode_tl.py#L27-L44) —— `scale = (1.0 / (dim + pe_dim))**0.5 * 1.44269504`（`log2e`），说明 softmax 用**总维** `DV + D_pe` 归一化；形参里 `Q`/`KV` 用 `dim`，`Q_pe`/`K_pe` 用 `pe_dim`，正是拆分的体现。

两段打分 gemm 拼接（split 版）：

[attention_engine/core/template/tl_template/attn/mla_decode_tl.py:L149-L157](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/mla_decode_tl.py#L149-L157) —— 先 `T.gemm(Q_shared, KV_shared, acc_s, transpose_B=True)` 算内容打分（维 `dim`），紧接着 `T.gemm(Q_pe_shared, K_pe_shared, acc_s, transpose_B=True)` 把位置打分（维 `pe_dim`）**累加**到同一个 `acc_s`，对应公式 \(Q\cdot KV^\top + Q_{pe}\cdot K_{pe}^\top\)。

#### 4.2.4 代码实践

1. **实践目标**：验证 `PE_DIM = dimqk - dimv` 的推导链与两段 gemm 的拼接。
2. **操作步骤**：在 `mla_decode.py` 中确认 `D=576`、`DV=512`，故 `D-DV=64`；再读 `lower_decode_mla.py` 第 32 行 `PE_DIM=str(dimqk-dimv)`，确认 `dimqk` 来自 `qkv_meta[0].shape[3]=576`、`dimv` 来自 `qkv_meta[2].shape[3]=512`，得 `PE_DIM=64`。
3. **需要观察的现象**：`flash_attn_split` 里 `acc_s` 先后被两个 `T.gemm` 写入——第一段形状隐含 `[block_H, dim]×[dim, block_H]`，第二段 `[block_H, pe_dim]×[pe_dim, block_H]`，输出都落到同一 `[block_H, block_N]` 的 `acc_s`。
4. **预期结果**：`PE_DIM=64`、`DIM=512`，且 softmax 的 `scale` 用 `dim+pe_dim=576` 归一化（即 `\sqrt{D}`）。
5. 数值正确性「待本地验证」：可对照 `ref_program` 里 `scale = (dim + pe_dim)**0.5`、`scores = einsum(query, key, ...)`，其中 `query=torch.concat([q, q_pe],-1)`、`key=torch.concat([kv, k_pe],-1)`——参考实现把两段 **concat 后一次 einsum**，与模板「两次 gemm 累加」在数学上等价。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `scale` 用 `dim+pe_dim` 而不是只用 `dim`？
  - **答案**：因为有效打分向量的总长度是 `DV + D_pe`（内容段 + 位置段拼接），softmax 归一化应除以 \(\sqrt{DV + D_{pe}}\)，与 `ref_program` 的 `scale=(dim+pe_dim)**0.5` 一致。
- **练习 2**：`qkv_meta[0]` 声明 q 维为 `D=576`，但运行期 `Q` 张量维是 `512`，这种「不一致」为什么不报错？
  - **答案**：`dimqk` 只在编译期用于推导 `PE_DIM = dimqk - dimv`；模板形参 `Q` 的实际维度是 `DIM = dimv = 512`，与运行期 `Q` 一致。`576` 只是「逻辑总维」，从不直接对应某个运行期张量的最后一维。

---

### 4.3 combine kernel：split-kv 的行规约合并

#### 4.3.1 概念说明

承接 u4-l3：split-kv 把 KV 切成 `num_split` 段并行，每段各自跑一遍 online softmax，产出：

- `Output_partial[:, :, :, s, :]`：第 `s` 段的**已归一化**部分输出（已除以该段的分母）。
- `glse[:, :, s, :]`：第 `s` 段的 **lse**（log-sum-exp，对数分母）。

combine kernel 的任务：把这些「局部归一化」的结果重新拼成全局 softmax 输出。这是一个**行规约**问题——对每个 `(batch, head)`，沿 `num_split` 维做一次 log 域的归并。

设第 `s` 段的 lse 为 \(\ell_s\)、部分输出为 \(O_s\)（已除以该段分母），则全局输出为：

\[ \ell_{\text{sum}} = \mathrm{LSE}(\ell_1,\dots,\ell_S) = M + \ln\!\sum_s e^{\ell_s - M}, \quad M=\max_s \ell_s \]

\[ \alpha_s = e^{\ell_s - \ell_{\text{sum}}}, \qquad O = \sum_s \alpha_s \, O_s \]

\(\alpha_s\) 就是注入到第 `s` 段部分输出的 **`o_scale`**。直觉上：lse 越大的段（分母越大、未归一化概率质量越大）权重越高；所有 \(\alpha_s\) 之和为 1，保证合并后仍是合法的 softmax 期望。

#### 4.3.2 核心流程

用户用 `OnlineSoftmax.combine` 这一**静态方法**描述上述数学（用自然对数 `exp`/`log` 表达意图），模板则手写等价的 **log2 域** kernel（用 `exp2`/`log2`）。二者的步骤一一对应：

```text
符号描述 (mla_decode.py OnlineSoftmax.combine)        kernel 实现 (mla_decode_tl.py combine)
─────────────────────────────────────────────────      ───────────────────────────────────────
lse_max  = lse.get_reduce("max")                     ─► for k: lse_max_local = max(lse_max_local, glse[...,k])
row_sum  = (lse - lse_max).exp()                      ─► lse_logsum += exp2(glse_k - lse_max)
row_sum_sum = row_sum.get_reduce("sum")               ─► （上一行累加即求和）
lse_sum  = row_sum_sum.log() + lse_max                ─► lse_logsum = log2(lse_logsum) + lse_max
o_scale  = (lse - lse_sum).exp()                      ─► scale_local = exp2(glse_k - lse_logsum)
                                                              o_accum += Output_partial_k * scale_local
```

> 为何 kernel 用 `exp2`/`log2` 而符号描述用 `exp`/`log`？因为 `glse` 落盘时存的就是 **log2 域** 的 lse（见 4.3.3 的 `T.log2(logsum) + scores_max*scale`）。在 log2 域里 `exp2(a-b)` 等价于自然域的「按比值缩放」，整条链路自洽。符号描述用 `exp/log` 是为了表达**数学意图**，让读者一眼看出这是 log-sum-exp 归并。

#### 4.3.3 源码精读

先看用户层符号描述——`OnlineSoftmax.combine` 用 `get_reduce` 做行规约：

[attn_script/mla_decode.py:L57-L65](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mla_decode.py#L57-L65) —— `lse_max = lse.get_reduce("max")`（沿 split 维求最大）、`row_sum = (lse-lse_max).exp()`、`row_sum_sum = row_sum.get_reduce("sum")`（求和）、`lse_sum = row_sum_sum.log()+lse_max`（稳定 log-sum-exp）、`o_scale = (lse-lse_sum).exp()`（每段缩放因子）。

> `get_reduce` 的语义见 [core.py:L262-L270](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L262-L270)：丢掉最后一维做行规约，支持 `sum`/`max`/`abssum`。这里 `lse` 沿 `num_split` 维规约，正合 combine 的需要。

`combine` 在 `OnlineFunc` 基类里有默认实现（返回占位 `o_scale`），MLA 解码由用户在子类里覆写：

[attention_engine/attn_engine/attn_engine.py:L94-L105](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L94-L105) —— 基类 `combine(final_rowscales)` 默认返回一个名为 `o_scale` 的占位 `SymbolScalar`，说明「split-kv 合并」是可选能力，只有需要时才由用户覆写。

接着看落盘 lse 的 log2 域转换（这决定了 combine 必须用 `exp2`/`log2`）：

[attention_engine/core/template/tl_template/attn/mla_decode_tl.py:L173-L177](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/mla_decode_tl.py#L173-L177) —— 先 `acc_o /= logsum`（段内归一化），再把 `logsum[i] = T.log2(logsum[i]) + scores_max[i]*scale` 存进 `glse`，即 `glse` 保存的是 **log2 域 lse**。

最后是 combine macro 本体，三段循环对应符号描述的四个表达式：

[attention_engine/core/template/tl_template/attn/mla_decode_tl.py:L181-L216](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/mla_decode_tl.py#L181-L216) —— ① `for k in T.serial(num_split): lse_max_local = max(..., glse[...,k])` 求 `lse_max`；② `for k in T.Pipelined(num_split): lse_logsum += exp2(glse_k - lse_max)` 求 \(\sum e^{\ell_s-M}\)；③ `lse_logsum = log2(lse_logsum) + lse_max` 得 `lse_sum`；④ 再一轮 `serial` 用 `scale_local = exp2(glse_k - lse_logsum)` 把每段 `Output_partial` 加权累加成最终 `Output`。

入口 `main_split` 把 split kernel 与 combine 串联；`num_split==1` 时退化为无 combine 的 `main_no_split`：

[attention_engine/core/template/tl_template/attn/mla_decode_tl.py:L218-L246](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/mla_decode_tl.py#L218-L246) —— `main_split` 先调 `flash_attn_split` 再调 `combine`；`if num_split > 1: return main_split else: return main_no_split`，说明 combine 仅在真正切分时才出现。

#### 4.3.4 代码实践（本讲主实践）

1. **实践目标**：把符号描述 `OnlineSoftmax.combine` 与手写 `combine` macro 逐行对上，验证它们是同一个 log-sum-exp 归并。
2. **操作步骤**：
   - 打开 [mla_decode.py:L57-L65](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mla_decode.py#L57-L65) 的符号 `combine`，写下 4 个中间量：`lse_max`、`row_sum_sum`、`lse_sum`、`o_scale`。
   - 打开 [mla_decode_tl.py:L199-L216](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/mla_decode_tl.py#L199-L216) 的 kernel，找到对应的 4 段：`lse_max_local`、`lse_logsum_local`（累加前）、`lse_logsum_local`（`log2+lse_max` 后）、`scale_local`。
   - 在一张表里填入「符号表达式 ↔ kernel 代码」的映射。
3. **需要观察的现象**：符号版用 `exp`/`log`，kernel 版用 `exp2`/`log2`，但**结构完全同构**；`get_reduce("max")` 对应 kernel 里第一个 `serial` 循环，`get_reduce("sum")` 对应 `lse_logsum_local += ...` 的累加。
4. **预期结果**：得出结论——combine 的数学是 \(\alpha_s = \exp(\ell_s - \mathrm{LSE}(\ell_{1..S}))\)，kernel 用 log2 域实现是因为 `glse` 本身就存在 log2 域；两份代码描述同一件事。
5. 数值验证「待本地验证」：若能在 GPU 上跑 `mla_decode_tl.py` 的 `__main__`（`profiler.assert_allclose(ref_program, rtol=0.01, atol=0.01)`），把 `num_split` 在 `get_configs()` 的 `[1,2,4,8]` 间切换，应观察到 `num_split>1` 走 `main_split`+`combine`、`num_split==1` 走 `main_no_split`，且对参考程序误差均在 `0.01` 内——这反向证明 combine 的合并是数值正确的。

#### 4.3.5 小练习与答案

- **练习 1**：combine 里为什么先求 `lse_max` 再做 `exp(... - lse_max)`，而不是直接 `exp(lse)`？
  - **答案**：为了数值稳定性（log-sum-exp 的标准技巧）。`lse` 可能是较大的正值，直接 `exp(lse)` 会溢出；减去最大值 `M` 后指数恒 ≤ 0，最后再把 `M` 加回 `lse_sum`，结果不变但避免溢出。
- **练习 2**：若 `num_split==1`，combine 还会执行吗？为什么？
  - **答案**：不会。`flashattn` 在 `num_split>1` 时返回 `main_split`（含 combine），否则返回 `main_no_split`（直接 `flash_attn` 单 kernel，段内已归一化、无需跨段合并）。只有一个 split 时 \(\alpha_1 = e^0 = 1\)，combine 退化为恒等，故省略。
- **练习 3**：符号 `combine` 用 `exp/log`，kernel 用 `exp2/log2`，为什么结果一致？
  - **答案**：`glse` 落盘时存的是 log2 域 lse（`T.log2(logsum)+scores_max*scale`）。在 log2 域里，比值缩放 \(2^{\ell_s - \ell_{\text{sum}}}\) 与自然域 \(\exp(\ell_s - \ell_{\text{sum}})\) 在「同一域内做差再指数」的语义下等价；符号版用 `exp/log` 只是表达数学意图，kernel 沿用同一域以保证自洽。

---

## 5. 综合实践

**任务**：手动模拟一次 `num_split=2` 的 combine 合并，把符号 `combine`、手写 kernel、参考数学三者串起来。

假设某 `(batch, head)` 上两个 split 的 lse（自然域）为 \(\ell_1 = \ln 2\)、\(\ell_2 = \ln 3\)，对应部分输出为 \(O_1\)、\(O_2\)（均已段内归一化）。

1. 按 `OnlineSoftmax.combine` 的式子手算：
   - \(M = \max(\ell_1, \ell_2) = \ln 3\)
   - \(\text{row\_sum\_sum} = e^{\ell_1 - M} + e^{\ell_2 - M} = e^{\ln(2/3)} + e^0 = 2/3 + 1 = 5/3\)
   - \(\ell_{\text{sum}} = \ln(5/3) + \ln 3 = \ln 5\)
   - \(\alpha_1 = e^{\ln 2 - \ln 5} = 2/5\)，\(\alpha_2 = e^{\ln 3 - \ln 5} = 3/5\)
2. 验证 \(\alpha_1 + \alpha_2 = 1\)，写出合并结果 \(O = \tfrac{2}{5}O_1 + \tfrac{3}{5}O_2\)。
3. 打开 [mla_decode_tl.py:L199-L216](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/mla_decode_tl.py#L199-L216)，把这 4 步标注到对应代码行（注意 kernel 在 log2 域，但「求 max→做差→指数→求和→log→做差→指数」的结构与上面完全相同）。
4. 反思：为什么 \(\ell_2\) 更大的段获得了更大权重（\(3/5 > 2/5\)）？因为 lse 是对数分母，\(\ell_2\) 更大意味着第 2 段的未归一化概率质量更大，理应在全局输出里占更高比例。

> 这个手算练习不需要 GPU，但能让你彻底理解 combine 不是「简单平均」，而是「按各段概率质量做 log 域加权」。

## 6. 本讲小结

- MLA 解码用 `kv_shared=True` 触发，**同一份 `KV` 潜变量既当 Key 又当 Value**（在模板里被两次 `T.gemm` 复用），外加一段只参与打分的位置编码 `q_pe`/`k_pe`。
- 维度被拆成 `DIM = dimv`（内容/输出维 `DV`）与 `PE_DIM = dimqk - dimv`（位置维 `D_pe`），打分由两次 gemm 累加完成 \(Q\cdot KV^\top + Q_{pe}\cdot K_{pe}^\top\)，softmax 用总维 \(\sqrt{DV+D_{pe}}\) 归一化。
- `lower_decode_mla.lower_tl` 极简：只算 `DIM`/`PE_DIM` 并渲染手写模板，**不**走符号降级链——这是 MLA 解码与 MHA/GQA 解码最大的工程差异。
- split-kv 的合并由 combine kernel 完成：沿 `num_split` 维做 log-sum-exp 归并，得到每段 `o_scale = exp(lse_s - lse_sum)`，再加权累加各段部分输出。
- 用户的 `OnlineSoftmax.combine`（自然域 `exp/log`）与手写 `combine` macro（log2 域 `exp2/log2`）**数学等价、结构同构**，差异仅因 `glse` 存在 log2 域。
- `num_split==1` 时 combine 退化为恒等，模板直接走无合并的 `main_no_split`。

## 7. 下一步学习建议

- **横向对比**：回到 u4-l3 的 `lower_decode_gqa`，比较「GQA 解码 combine」与「MLA 解码 combine」——前者由 `lower_online_func` 走符号降级生成合并代码，后者是手写模板。思考：为什么 MLA 选择手写？（提示：两段 gemm 的打分结构难以用现有符号 IR 表达。）
- **CuTe 后端**：进入 u5-l2「MLA 解码的 CuTe kv_shared 后端」，看 CuTe 如何用 paged-kv（`block_table`/`cache_seqlens`）与 `get_mla_metadata` 计算 `num_split`，与本文 tl 后端的 `num_split` 配置空间（`get_configs`）对照。
- **深入阅读**：精读 `mla_decode_tl.py` 的 `ref_program`（第 266–306 行），它是理解 MLA 数值语义的最佳参考实现——`concat([q,q_pe])` 后一次 einsum，把本讲的「两次 gemm 累加」还原成最朴素的矩阵乘，便于做正确性对齐。
