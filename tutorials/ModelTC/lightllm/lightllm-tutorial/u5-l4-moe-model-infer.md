# MoE 模型推理

## 1. 本讲目标

在 u5-l2 中，我们以 Llama 为例走完了一个**稠密（dense）**模型的完整推理。本讲把视角从「一个固定的 FFN」切换到「一篮子可选的 FFN」——即 **MoE（Mixture of Experts，混合专家）**。学完本讲你应当掌握：

- 说清 MoE 层与普通 FFN 层在**计算结构**上的本质差异，以及它在推理框架中「替换」普通 FFN 的方式。
- 理解**专家路由（expert routing）**：门控（gate）打分、top-k 选择、权重归一化这三步如何决定「每个 token 由哪几个专家处理」。
- 掌握 LightLLM 的 **fused_moe 算子族**：`fused_experts_impl` 内部分组矩阵乘（grouped GEMM）、SiLU 融合、跨专家求和等阶段各自承担什么计算。
- 认识 DeepSeek-V2/V3 在标准 top-k 之上引入的**分组选择（grouped top-k）**、**共享专家（shared experts）**与 **EP（专家并行）**等进阶机制。

本讲是 u5-l5（MLA 注意力）的前置，因为 DeepSeek 系列同时用到 MoE 与 MLA，理解 MoE 后才能拼出完整的 DeepSeek 推理图。

## 2. 前置知识

阅读本讲前，请确认你已了解（这些都在前几讲建立）：

- **FFN / MLP**：Transformer 每层在注意力之后都有一个前馈网络，Llama 用 SwiGLU 结构，即 `down_proj( SiLU(gate·x) ⊙ up·x )`，对应 `_ffn_tp` 里的 `gate_up_proj → silu_and_mul → down_proj` 三步。
- **TPSP 混合并行**：`_tpsp_allgather` / `_tpsp_reduce` 这一对通信原语，用于把注意力/MoE 的输入在 TP 组间收集、输出再规约（见 u6-l2）。
- **模板方法模式**：基类 `LlamaTransformerLayerInfer` 写好了残差骨架，子类只覆写 `_ffn` 等钩子（见 u3-l3、u5-l2）。
- **Triton kernel**：LightLLM 把性能关键路径写成 Triton，前面讲过采样、注意力等 kernel，本讲的 fused_moe 也是同类。

**MoE 的核心直觉**：稠密 FFN 对每个 token 都做一模一样的全部计算，参数量大、算力贵。MoE 把一个大 FFN 拆成 \(E\) 个小 FFN（「专家」），每个 token 只激活其中 \(k\) 个（\(k \ll E\)），从而在「总参数量」很大的同时，把「单 token 的实际计算量」压到接近一个小模型。代价是需要一个**路由器（router）**来为每个 token 选择专家，且权重存储与访存模式更复杂。

## 3. 本讲源码地图

本讲涉及的关键文件按「模型层 / 算子层 / 权重层」三组列出：

| 文件 | 作用 |
| --- | --- |
| [lightllm/models/mixtral/model.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/mixtral/model.py) | Mixtral 模型本体，几乎为空，只填插槽 + 注册 |
| [lightllm/models/mixtral/layer_infer/transformer_layer_infer.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/mixtral/layer_infer/transformer_layer_infer.py) | Mixtral 层推理：覆写 `_ffn`，串联 gate→topk→fused_experts |
| [lightllm/models/mixtral/layer_infer/_custom_ops.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/mixtral/layer_infer/_custom_ops.py) | Mixtral 自带的 PyTorch 版 `fused_topk`（参考实现） |
| [lightllm/models/mixtral/layer_weights/transformer_layer_weight.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/mixtral/layer_weights/transformer_layer_weight.py) | Mixtral 权重：在 Llama 权重基础上把 FFN 换成 `FusedMoeWeight` |
| [lightllm/models/deepseek2/model.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/model.py) | DeepSeek-V2/V3 模型本体（继承 Llama） |
| [lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py) | DeepSeek 层推理：分组 top-k、共享专家、EP/TP 双路径 |
| [lightllm/common/basemodel/triton_kernel/fused_moe/grouped_fused_moe.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/fused_moe/grouped_fused_moe.py) | **核心**：`fused_experts_impl` 分组矩阵乘主流程 |
| [lightllm/common/basemodel/triton_kernel/fused_moe/softmax_topk.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/fused_moe/softmax_topk.py) | 单 kernel 版 softmax+topk（mixtral 用） |
| [lightllm/common/basemodel/triton_kernel/fused_moe/grouped_topk.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/fused_moe/grouped_topk.py) | 分组 top-k kernel（DeepSeek 用） |
| [lightllm/common/basemodel/triton_kernel/fused_moe/topk_select.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/fused_moe/topk_select.py) | 选择函数总入口 `select_experts`，按模型分流 |
| [lightllm/common/basemodel/triton_kernel/fused_moe/moe_silu_and_mul.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/fused_moe/moe_silu_and_mul.py) | 融合的 SiLU·mul 激活 kernel |
| [lightllm/common/basemodel/triton_kernel/fused_moe/moe_sum_reduce.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/fused_moe/moe_sum_reduce.py) | 跨被选专家的加权求和 kernel |
| [lightllm/common/basemodel/layer_weights/meta_weights/fused_moe/fused_moe_weight.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/fused_moe/fused_moe_weight.py) | `FusedMoeWeight`：把所有专家权重打包、选择计算后端 |

> 提示：fused_moe 目录下还有 `grouped_fused_moe_ep.py`（EP 路径）、`append_shared_expert_topk.py`（共享专家并入）、`deepep_scatter_gather.py`（DeepEP 分发收集）等，本讲会点到，深入留作练习。

## 4. 核心概念与源码讲解

### 4.1 MoE 推理：把单个 FFN 换成一篮子专家

#### 4.1.1 概念说明

稠密 FFN 的计算（Llama 的 `_ffn_tp`）可以写成：

\[
y = W_{down}\,\big(\,\text{SiLU}(W_{gate}\,x)\ \odot\ W_{up}\,x\,\big)
\]

每个 token 都用**同一组** \(W_{gate}, W_{up}, W_{down}\)。MoE 则把它换成：

\[
y = \sum_{i \in \mathcal{S}(x)} \tilde{p}_i(x)\ \cdot\ \text{FFN}_i(x)
\]

其中：

- \(E\) 是专家总数，每个专家 \(\text{FFN}_i\) 是一套独立的小 FFN（同样 gate/up/down 三件套）。
- \(\mathcal{S}(x)\) 是路由器为 token \(x\) 选出的 \(k\) 个专家集合（\(k \ll E\)，例如 Mixtral \(E=8,k=2\)，DeepSeek-V3 \(E=256,k=8\)）。
- \(\tilde{p}_i(x)\) 是每个被选专家的**融合权重**，通常由门控打分经 softmax + top-k + 归一化得到。

因此 MoE 推理在框架层的「落点」非常清晰：**它就是替换掉 Transformer 层里的 `_ffn` 钩子，其余（注意力、残差、归一化）原样复用稠密模型基类。** 这正是 LightLLM 用模板方法模式的价值——子类只换一个 `_ffn`，就完成了从 Llama 到 Mixtral 的改造。

#### 4.1.2 核心流程

一个 MoE 层的前向可以拆成两段——**路由**与**专家计算**：

```
输入 hidden_states (num_tokens, hidden_dim)
        │
        ▼  ① 路由 router_logits = moe_gate(hidden_states)
        │
   ┌────┴─────── 分支：选专家 ─────────────────┐
   │  softmax(或 sigmoid) → top-k → 归一化      │
   │  得到 topk_ids (num_tokens, k)             │
   │       topk_weights (num_tokens, k)         │
   └────┬──────────────────────────────────────┘
        ▼  ② 专家计算 fused_experts
        │  按专家把 token 重排 (moe_align)
        │  分组 GEMM₁: x @ W_gate_up^e   (per expert)
        │  SiLU·mul 激活
        │  分组 GEMM₂: · @ W_down^e       (per expert，乘 topk_weight)
        │  跨 k 个专家求和 (moe_sum_reduce)
        ▼
输出 (num_tokens, hidden_dim)
```

注意：① 产出的只是「选了谁、权重多少」的轻量张量；② 才是真正的大块矩阵乘。LightLLM 把 ② 做成一个**融合**实现 `fused_experts_impl`，避免为每个专家单独起 kernel、避免中间张量反复落盘。

#### 4.1.3 源码精读：MoE 层如何挂在 Llama 基类上

先看模型本体有多薄。Mixtral 的 `MixtralTpPartModel` 继承 `TpPartBaseModel`，除了插槽，只做了 rotary 初始化：

[lightllm/models/mixtral/model.py:L18-L49](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/mixtral/model.py#L18-L49) —— 用 `@ModelRegistry("mixtral")` 注册；把 `transformer_layer_infer_class` 指向 `MixtralTransformerLayerInfer`、`transformer_weight_class` 指向 `MixtralTransformerLayerWeight`，其余插槽（pre/post 推理类、权重类、infer state）直接复用 Llama。也就是说，Mixtral 相对 Llama 的全部「MoE 性」都集中在这两个类里。

真正的 MoE 推理在层推理类。`MixtralTransformerLayerInfer` 继承 `LlamaTransformerLayerInfer`，**只覆写 `_ffn`**：

[lightllm/models/mixtral/layer_infer/transformer_layer_infer.py:L18-L45](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/mixtral/layer_infer/transformer_layer_infer.py#L18-L45) —— 这段代码完整刻画了 MoE 的两段式：

1. `_tpsp_allgather`：TPSP 混合并行下，先把各 rank 的部分 hidden 收集全。
2. `router_logits = layer_weight.moe_gate.mm(hidden_states)`：门控线性层，输出形状 `(num_tokens, num_local_experts)`。
3. `fused_topk(...)`：softmax + top-k + 归一化（见 4.2）。
4. `fused_experts_impl(...)`：融合专家计算（见 4.3），传入 `w1`（gate_up）、`w2`（down）、`topk_weights`、`topk_ids`。
5. `_tpsp_reduce`：把各 rank 的部分输出规约。

对比稠密 Llama 的 `_ffn`：

[lightllm/models/llama/layer_infer/transformer_layer_infer.py:L111-L129](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py#L111-L129) —— Llama 的 `_ffn_tp` 是 `gate_up_proj.mm → silu_and_mul → down_proj.mm` 三行，因为它只有一套 FFN 权重；而 Mixtral 把这三步**搬进了 `fused_experts_impl` 内部，并对每个专家各做一次**。这就是「MoE 替换普通 FFN」的字面含义：调用点从 `layer_weight.gate_up_proj` 换成了 `layer_weight.experts` + `fused_experts_impl`。

权重侧的对应改造同样轻量。`MixtralTransformerLayerWeight` 继承 Llama 权重，把 `_init_ffn` 重定向到 `_init_moe`：

[lightllm/models/mixtral/layer_weights/transformer_layer_weight.py:L30-L60](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/mixtral/layer_weights/transformer_layer_weight.py#L30-L60) —— 关键两点：`moe_gate` 用 `ROWMMWeight(..., tp_world_size=1)`，即门控**不做张量并行**（每个 rank 都持有完整 gate，独立为每个 token 打分）；专家本体用 `FusedMoeWeight`，把所有专家的 `gate_proj(w1)/down_proj(w2)/up_proj(w3)` 打包到一起。注意第 46 行有一处运行期断言 `assert get_env_start_args().enable_ep_moe, "Mixtral only support tp mode."`（其提示信息与断言条件语义相反，属历史遗留，阅读时以断言条件为准）。

> 结论：MoE 模型在 LightLLM 里的「适配成本」很低——继承 Llama，覆写一个 `_ffn`，再加一份把 FFN 权重换成 `FusedMoeWeight` 的权重类即可。这和 u5-l3 讲的「新增模型 = 填骨架钩子」一脉相承。

#### 4.1.4 代码实践

**实践目标**：直观确认「MoE 层 = 稠密 `_ffn_tp` 的 per-expert 推广」。

**操作步骤**：

1. 打开 `lightllm/models/llama/layer_infer/transformer_layer_infer.py`，阅读 `_ffn_tp`（L118-L129）的三行 SwiGLU。
2. 打开 `lightllm/common/basemodel/triton_kernel/fused_moe/grouped_fused_moe.py`，定位 `fused_experts_impl`（L992 起），找出与「gate_up→silu_and_mul→down」对应的三段调用。
3. 列一张对照表：Llama 的哪一行 ↔ Mixtral/fused_experts 里的哪一段。

**需要观察的现象**：`fused_experts_impl` 内部会出现两次 `grouped_matmul`（一次对应 `gate_up_proj`，一次对应 `down_proj`），中间夹一次 `silu_and_mul_fwd`，末尾一次 `moe_sum_reduce`——这正是把 Llama 的三步 FFN「按专家批量重复」后的形态。

**预期结果**：你应能写出类似下面的映射（答案见 4.3.4）：

| Llama 稠密 FFN | fused_experts_impl 内对应 |
| --- | --- |
| `gate_up_proj.mm(input)` | 第一次 `grouped_matmul(... expert_weights=w1 ...)` |
| `silu_and_mul_fwd(up_gate_out, ffn1_out)` | `silu_and_mul_fwd(cache1, cache2)` |
| `down_proj.mm(ffn1_out)` | 第二次 `grouped_matmul(... expert_weights=w2, mul_routed_weight=True)` |
| （无） | `moe_sum_reduce(...)` 跨专家求和 |

> 待本地验证：若你能在本地跑通一个小 Mixtral，可在门控后打印 `topk_ids` 的直方图，观察每个专家被命中的频次是否大致均衡（MoE 负载均衡话题见 4.4）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Mixtral 的 `moe_gate` 要设 `tp_world_size=1`，而专家本体却参与张量并行？

**参考答案**：门控必须为**每个 token 选出全局一致的专家集合**，若各 rank 用被切片的 gate 会得到不一致的路由结果，导致各 rank 计算的是不同专家、无法规约；因此 gate 不切分、每 rank 完整一份。而专家本体的权重矩阵很大，沿中间维切分到各 rank 才能省显存，且各 rank 各算各的那一片、最后 `_tpsp_reduce` 求和即可还原完整结果。

**练习 2**：如果把 `num_experts_per_tok` 从 2 改成 1，MoE 层的输出语义会变成什么？

**参考答案**：每个 token 只激活 1 个专家，路由退化为「硬路由（hard routing）」，`moe_sum_reduce` 退化为对单专家结果的恒等映射（求和只有一项）。计算更省，但表达能力下降、负载更易不均衡。

---

### 4.2 专家路由：门控打分与 top-k 选择

#### 4.2.1 概念说明

路由要回答一个问题：**对当前 token，挑哪 \(k\) 个专家、各给多大权重？** 标准做法是：

1. **打分**：门控线性层把 hidden（维度 \(d\)）映射到 \(E\) 维 logits \(g \in \mathbb{R}^E\)。
2. **归一化为概率**：\(p = \text{softmax}(g)\)（DeepSeek 也可选 `sigmoid`）。
3. **top-k**：取概率最大的 \(k\) 个专家，记其下标为 `topk_ids`、概率为 `topk_weights`。
4. **再归一化**：把被选 \(k\) 个的概率重新归一为和为 1（`renormalize=True`），即
   \[
   \tilde{p}_i = \frac{p_i}{\sum_{j \in \mathcal{S}} p_j},\quad i \in \mathcal{S}
   \]
   这保证融合权重的尺度与专家输出的尺度稳定。

DeepSeek-V2/V3 在此基础上还做了**分组选择**：把 \(E\) 个专家分成 \(G\) 组，先选「最好的若干组」，再在被选组里挑 top-k 专家。这是一种无需辅助损失的负载均衡策略（auxiliary-loss-free load balancing），避免少数专家被过度使用、其余闲置。

#### 4.2.2 核心流程

```
mixtral（简单 top-k）:
  router_logits --softmax--> scores --topk(k)--> ids/weights --renorm--> topk_weights/topk_ids

deepseek（分组 top-k）:
  router_logits --(+correction_bias)--> scores
     --按 group 求 group_score(组内 top-2 之和)--> 选 top-k_g 个组
     --屏蔽未选组专家--> 在剩余里 topk(k)--> renorm
```

#### 4.2.3 源码精读

**Mixtral 的参考实现（PyTorch 版，易读）**：`_custom_ops.py` 里的 `fused_topk` / `topk_softmax`：

[lightllm/models/mixtral/layer_infer/_custom_ops.py:L15-L46](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/mixtral/layer_infer/_custom_ops.py#L15-L46) —— `topk_softmax` 就三行：`softmax → torch.topk(k) → 返回`；`fused_topk` 负责分配输出张量、调用 `topk_softmax`、再做一次 `topk_weights / sum` 的归一化。这是「打分→选专家→归一化」的最直白表达。

> 注意：Mixtral 的 `_ffn` 实际 import 的是**这份** PyTorch 版 `fused_topk`（见 `transformer_layer_infer.py` 第 6 行 `from ..._custom_ops import fused_topk`）。它易于理解，但每拍都启动多个 PyTorch op、对 decode 这种 token 数极少的场景不划算。性能版本是单 kernel 的 `softmax_topk`：

**Triton 单 kernel 版 `softmax_topk`**：

[lightllm/common/basemodel/triton_kernel/fused_moe/softmax_topk.py:L6-L63](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/fused_moe/softmax_topk.py#L6-L63) —— 一个 kernel 处理一行（一个 token）：先整行减最大值、`exp`、求和得到分母（即手写 softmax），再循环 `top_k` 次，每次取当前最大值下标作为被选专家、计算概率、然后用 `tl.where(offsets == idx, -inf, values)` 把刚选中的位置「屏蔽」掉以选下一个。若 `RENORM` 为真，末尾再除以这 \(k\) 个概率之和。这就把「softmax + top-k + 归一化」三步融进了单个 kernel，避免多次访存。

> 这份 `softmax_topk` 被通用入口 `select_experts`（4.2.3 末）在「非分组」分支下使用（`topk_select.py` 里 `fused_topk` 内部优先用 sgl_ops，否则回落到 `softmax_topk`）。

**DeepSeek 的分组 top-k**：先用一个等价的 PyTorch 版讲清逻辑：

[lightllm/common/basemodel/triton_kernel/fused_moe/topk_select.py:L52-L87](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/fused_moe/topk_select.py#L52-L87) —— `grouped_topk` 的四步：

1. `scores = softmax(gating_output)`（或 sigmoid）；
2. `group_scores = scores.view(n, G, -1).max(dim=-1)`：每组取组内最大值作为「组的代表分」；
3. `group_idx = topk(group_scores, topk_group)`：选出得分最高的 `topk_group` 个组，构造组掩码；
4. 把未选中组里的专家分数置 0（`masked_fill`），再 `topk(topk)` 选出最终 \(k\) 个专家，从**原始** `scores` 上 gather 权重并归一化。

这套逻辑的 Triton 版（DeepSeek 实跑路径）在 `grouped_topk.py`，把上述步骤融进单 kernel `grouped_topk_kernel`，并支持 `e_score_correction_bias`（DeepSeek-V3 的偏置校正）与 sigmoid 打分：

[lightllm/common/basemodel/triton_kernel/fused_moe/grouped_topk.py:L93-L202](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/fused_moe/grouped_topk.py#L93-L202) —— 注意它对每组先用 `tl.sort(..., descending=True)` 取组内前几名之和作为 `group_value`（V3 用组内 top-2 之和，见 `GROUP_SCORE_USED_TOPK_NUM`），再选 `group_topk_num` 个组，最后用 `argsort` 在掩码后的分数上选 top-k。该 kernel 的 host 端封装是 `triton_grouped_topk`（L205-L265）。

**选择函数总入口**：`select_experts` 按 `use_grouped_topk` 在两条路径间分流：

[lightllm/common/basemodel/triton_kernel/fused_moe/topk_select.py:L126-L179](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/fused_moe/topk_select.py#L126-L179) —— DeepSeek（`use_grouped_topk=True`）走 `triton_grouped_topk`；其余走 `fused_topk`（→ `softmax_topk`）。末尾有一段「autotune warmup 时把 topk_ids 随机化」的逻辑，是为了在自动调参预热阶段让分组矩阵乘遇到更均匀的负载分布，避免调出来的配置只在某种路由分布上最优。

#### 4.2.4 代码实践

**实践目标**：在源码层面区分「简单 top-k」与「分组 top-k」两种路由。

**操作步骤**：

1. 在 `_custom_ops.py` 的 `topk_softmax` 上，手动推演一个 \(E=8, k=2\) 的样例：给定 logits，写出 softmax 后的 8 个概率，圈出 top-2，再写出归一化后的权重。
2. 在 `topk_select.py` 的 `grouped_topk` 上，假设 \(E=8, G=4\)（每组 2 专家）、`topk_group=2`、`topk=2`，推演一遍：先选 2 个组，再在被选 4 个专家里挑 2 个。
3. 对比两者，回答：分组选择相比直接 top-k，主要改变了什么？

**预期结果**：分组选择**强制专家分散在不同的组里**，避免某几个专家被反复选中、从而起到负载均衡作用；代价是可能略损失「全局最优」的 top-k。

> 待本地验证：上述两步推演建议用纸笔完成；若想验证，可写一个独立的小脚本调用 `softmax_topk` 与 `triton_grouped_topk`，喂相同的 `gating_output`，对比 `topk_ids` 差异。

#### 4.2.5 小练习与答案

**练习 1**：`renormalize=True` 时，被选 \(k\) 个专家的权重之和是多少？为什么需要这一步？

**参考答案**：和为 1。因为 softmax 后只取了 \(k\) 个，它们的概率之和小于 1；若不归一化，MoE 输出的整体尺度会随 \(k\) 和具体专家分布变化，导致训练 / 推理数值不稳定。归一化后，输出尺度与稠密 FFN 可比。

**练习 2**：DeepSeek 的分组 top-k 里，`group_score_used_topk_num`（V3 设为 2）的作用是什么？

**参考答案**：它是「评价一个组好坏」时，取组内前几名专家分数之和。V3 取组内 top-2 之和作为组的代表分，比只取 max 更能反映该组的整体实力，使组选择更稳健（见 `topk_select.py` L145-L160 对 V3 配置 `topk_group==4, num_expert_group==8, top_k==8` 的特判）。

---

### 4.3 fused_moe 算子：一次融合的分组矩阵乘

#### 4.3.1 概念说明

选出专家后，真正的计算是「对每个被选 token-专家对，跑一遍那个专家的小 FFN」。朴素实现是双重循环：对每个专家，挑出分配给它的 token，做两次 matmul，再把结果加回。问题在于：

- 专家数 \(E\) 很大（DeepSeek 256），逐个起 kernel 调度开销高；
- 每个 token 只激活 \(k\) 个专家，逐专家的 batch（分配到的 token 数）很小，小矩阵乘 GPU 利用率低；
- 中间张量（每个 token-专家对的隐层输出）体积 \(O(\text{tokens} \times k \times \text{intermediate})\) 很大，反复分配/落盘代价高。

LightLLM 的 `fused_experts_impl` 用**分组矩阵乘（grouped GEMM）**解决：先把所有 token-专家对**按专家重排**，让同一专家的 token 连续排布，再用一个 `grouped_matmul` kernel 一次性算完所有专家的矩阵乘（kernel 内部按专家切分 grid），中间激活也用融合 kernel，最后用一个 kernel 把 \(k\) 个专家的结果加权求和。整个过程只有寥寥几个 kernel launch。

#### 4.3.2 核心流程

`fused_experts_impl` 对输入分块（`CHUNK_SIZE = 32*1024` 个 token 一块）循环，每块内五步：

```
对每个 chunk:
  ① moe_align_fused:  按 topk_ids 把 token 重排到「按专家分组」的索引表
                      expert_to_tokens[E, k*tokens] / expert_to_weights / expert_token_num[E]
  ② grouped_matmul(w1, mul_routed_weight=False):
        per-expert:  cache1[t,k] = (W_gate^e x) ⊕ (W_up^e x)      # 拼成 2N 维
  ③ silu_and_mul_fwd: cache2[t,k] = SiLU(gate) ⊙ up                # 2N → N
  ④ grouped_matmul(w2, mul_routed_weight=True):
        per-expert:  cache3[t,k] = W_down^e cache2[t,k] · topk_weight[t,k]
  ⑤ moe_sum_reduce:   out[t] = Σ_k cache3[t,k]                     # 跨专家求和
```

关键张量形状（设隐层 `N = 2 * intermediate`）：

- `cache1`：`(tokens, k, N)` —— gate_up 拼接后的输出；
- `cache2`：`(tokens, k, N//2)` —— 激活后，恢复到 intermediate 维；
- `cache3`：`(tokens, k, hidden)` —— down 投影后回到 hidden 维；
- `out`：`(tokens, hidden)` —— 对 k 求和后的最终输出。

#### 4.3.3 源码精读

`fused_experts_impl` 主体（去掉 scale/bias 等可选参数后）：

[lightllm/common/basemodel/triton_kernel/fused_moe/grouped_fused_moe.py:L992-L1115](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/fused_moe/grouped_fused_moe.py#L992-L1115) —— 这就是上面流程图的代码实现。要点逐段对应：

- L1026-L1035：分配 `intermediate_cache1/2/3`（即 cache1/2/3），并把 cache1 与 cache3 的底层显存做**共享复用**（`intermediate_cache13_shared` 切两段视图），因为它们的存活区间不重叠，省一块大显存。
- L1044-L1055：对 `num_tokens` 按 `FFN_MOE_CHUNK_SIZE=32*1024` 分块处理，避免一次性把 `(tokens, k, N)` 中间张量撑爆显存。
- L1056-L1065：调用 `moe_align_fused` 生成「专家 → token 索引表」`expert_to_tokens`、对应权重 `expert_to_weights`、每专家 token 计数 `expert_token_num`。这张表是分组 GEMM 的核心：它告诉 kernel「第 \(e\) 个专家要处理哪些 token」。
- L1067-L1083：第一次 `grouped_matmul`，`expert_weights=w1`、`mul_routed_weight=False`、输出写进 `cache1`。即每个专家的 gate_up 投影，**不乘**路由权重（留到下一步之后）。
- L1085-L1091：`silu_and_mul_fwd(cache1 → cache2)`，逐元素 `SiLU(gate)⊙up`，2N 维压回 N 维。
- L1093-L1110：第二次 `grouped_matmul`，`expert_weights=w2`、`mul_routed_weight=True`、输出 `cache3`。这一步在 down 投影的同时把 `topk_weights` 乘上去（`expert_to_weights_scale` 即路由权重）。
- L1112-L1114：`moe_sum_reduce(cache3 → out)`，沿 `k` 维把 \(k\) 个专家的结果相加，得到每个 token 的最终 FFN 输出。

**激活 kernel `silu_and_mul_fwd`**：

[lightllm/common/basemodel/triton_kernel/fused_moe/moe_silu_and_mul.py:L117-L176](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/fused_moe/moe_silu_and_mul.py#L117-L176) —— 它支持 `blocked`（`[gate0,gate1,...,up0,up1,...]`）与 `interleaved`（`[gate0,up0,gate1,up1,...]`）两种内存布局，因为 w1 把 gate 与 up 拼接的方式可能不同；核心计算是 `gate = gate / (1+exp(-gate))`（即 SiLU）后 `up * gate`。它带有 autotuner（`@autotune`），会为不同 token 数自动挑最优的 `BLOCK_M/BLOCK_N/num_warps/NUM_STAGES`。

**求和 kernel `moe_sum_reduce`**：

[lightllm/common/basemodel/triton_kernel/fused_moe/moe_sum_reduce.py:L70-L108](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/fused_moe/moe_sum_reduce.py#L70-L108) —— 对形状 `(tokens, k, hidden)` 的输入沿 `k` 累加成 `(tokens, hidden)`。kernel 内层 `for i in range(topk_num)` 把 \(k\) 个专家的结果累加进 `accumulator`，同样带 autotune。

**对齐 kernel `moe_align`** 的作用用其 docstring 最能说明：

[lightllm/common/basemodel/triton_kernel/fused_moe/grouped_fused_moe.py:L64-L99](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/fused_moe/grouped_fused_moe.py#L64-L99) —— 给定 `topk_ids = [[0,1,2],[0,3,1],[3,1,4]]`（3 token 各选 3 专家），输出一张 `[expert_num, token_num*topk_num]` 的 0/1 矩阵，行是专家、列是「token-专家槽位」，1 表示该槽位属于本专家。`grouped_matmul` 据此把对应 token 的 hidden 拉到一起做矩阵乘。

> 这套 fused 实现**与权重的量化方式解耦**：`FusedMoeWeight` 在内部按 `quant_method` 选择具体计算后端（`FuseMoeTriton`、`deepgemm_impl`、`marlin_impl` 等）。无量化时 `FuseMoeTriton._fused_experts` 直接转调本讲的 `fused_experts`：

[lightllm/common/basemodel/layer_weights/meta_weights/fused_moe/impl/triton_impl.py:L80-L107](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/fused_moe/impl/triton_impl.py#L80-L107) —— 注意它先做 `select_experts`（路由），再 `_fused_experts`（计算），并在传入时自动判断 `use_fp8_w8a8`（权重是 `float8_e4m3fn` 时启用 FP8 路径，衔接 u6-l3 的量化）。

#### 4.3.4 代码实践

**实践目标**：把 fused_moe 的五个阶段与 Llama 稠密 FFN 严格对应起来。

**操作步骤**：

1. 打开 `grouped_fused_moe.py` 的 `fused_experts_impl`（L992-L1115）。
2. 在源码旁标注每个阶段对应的数学运算，重点确认：
   - 第一次 `grouped_matmul` 的 `out=intermediate_cache1.view(-1, N)` 与 Llama 的 `gate_up_proj.mm` 对应；
   - `silu_and_mul_fwd` 与 Llama 的 `silu_and_mul_fwd` 是**同一个 kernel**（被两处复用）；
   - 第二次 `grouped_matmul` 的 `mul_routed_weight=True`，说明路由权重在哪一步乘上去；
   - `moe_sum_reduce` 是稠密 FFN 里**没有**的额外步骤。
3. 回答：为什么第一次 `grouped_matmul` 的 `mul_routed_weight=False`，而第二次是 `True`？

**预期结果**：路由权重 `topk_weights` 只在**第二次矩阵乘（down 投影）**时乘入，第一次（gate_up 投影）不乘。原因：gate_up 投影是「为每个 token-专家对算隐层」，与权重无关；只有当各专家都把隐层映射回 hidden 维、准备「融合」时，才需要乘以每个专家的融合权重 \(\tilde{p}_i\)，再加总。把它放在第二次 matmul 内部一起做，省一次访存。

> 待本地验证：若你修改 `mul_routed_weight` 的取值重新运行（仅作学习用，勿提交），观察输出尺度变化——把它设为 `False` 会丢掉权重归一化、输出幅度异常。

#### 4.3.5 小练习与答案

**练习 1**：`intermediate_cache13_shared` 为什么能把 cache1 和 cache3 放在同一段显存上？

**参考答案**：cache1 存活于「第一次 grouped_matmul 之后、silu_and_mul 之前」；cache3 存活于「第二次 grouped_matmul 之后、moe_sum_reduce 之前」，两者生命周期不重叠，且最大长度都 ≤ `M * topk * max(N, hidden)`。把它们映射到同一段物理显存的两个视图，可省下一块与中间隐层等大的显存。这是显存复用（buffer reuse）的典型手法。

**练习 2**：`CHUNK_SIZE = 32*1024` 分块处理解决了什么问题？

**参考答案**：中间张量体积是 \(O(\text{tokens} \times k \times \text{intermediate})\)，prefill 时 tokens 可能很大（数万），一次性分配会撑爆显存。分块把单次处理的 token 数限制在 32k 以内，把峰值显存压住，代价是外层多一个循环。

---

### 4.4 DeepSeek 的进阶：分组选择、共享专家与 EP 并行

#### 4.4.1 概念说明

DeepSeek-V2/V3 在标准 MoE 之上多了三件事，理解它们就理解了 DeepSeek 推理与 Mixtral 的差异：

- **分组选择（grouped top-k）**：已在 4.2 讲，用于负载均衡。
- **共享专家（shared experts）**：除 \(k\) 个被路由命中的专家外，还有 \(n\_shared\_experts\) 个**对所有 token 都激活**的专家。直觉是「把通用知识放进共享专家、把专门知识放进路由专家」，减少路由专家的重复负担。LightLLM 还支持把它「融合」进路由专家集合一起算（`enable_fused_shared_experts`）。
- **稠密层 + MoE 层混合**：DeepSeek 的前若干层（`first_k_dense_replace`）是普通稠密 FFN，之后每隔 `moe_layer_freq` 层才是 MoE。即一个模型里两种层共存。

此外在并行上：

- **TP 模式**（`enable_ep_moe=False`）：专家权重沿中间维切到各 TP rank，走本讲的 `fused_experts_impl`（`_ffn_tp_impl`）。
- **EP 模式**（`enable_ep_moe=True`，专家并行）：每个 rank 只持有**一部分专家**，token 需要在 rank 间「分发到持有对应专家的 rank、算完再收集回来」（DeepEP），走 `_ffn_ep_impl` / `_moe_ffn_edp`。

#### 4.4.2 核心流程

DeepSeek 层推理在初始化时就按层号决定本层是否 MoE，并把 `_ffn` 绑到对应实现：

```
__init__:
  is_moe = (n_routed_experts != None) and (layer_num >= first_k_dense_replace)
           and (layer_num % moe_layer_freq == 0)
  _bind_ffn():
    if is_moe:
        _ffn = enable_ep_moe ? _ffn_ep_impl : _ffn_tp_impl
    else:
        _ffn = 稠密 Llama._ffn
```

TP 模式下的 MoE 前向（`_moe_ffn_tp`）：

```
if 有共享专家 and 未融合: shared_out = 稠密 Llama._ffn_tp(hidden)   # 共享专家当成普通 FFN 算
router_logits = moe_gate.mm(hidden)
experts(hidden, router_logits, top_k, ...)                          # 4.2 + 4.3 的融合计算
if 有共享专家 and 未融合: hidden += shared_out                      # 共享专家结果加回
return hidden
```

#### 4.4.3 源码精读

DeepSeek 层推理类初始化与绑定：

[lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py:L32-L71](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py#L32-L71) —— 注意 `is_moe` 的三个条件（专家数非空、超过首个稠密层、满足 moe 频率）；`_bind_ffn` 据此把 `_ffn` 绑到 EP / TP / 稠密三种实现之一。绑定用的是把方法**赋值给实例属性**（`self._ffn = self._ffn_ep_impl`），这样模板骨架调用 `self._ffn(...)` 时就直连到具体实现，省去运行期分支判断。

权重侧的 `is_moe` 判定与共享专家融合开关：

[lightllm/models/deepseek2/layer_weights/transformer_layer_weight.py:L25-L43](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/layer_weights/transformer_layer_weight.py#L25-L43) —— `num_fused_shared_experts` 仅当「开启融合共享专家且不是 EP」时才非 0。融合的含义是：共享专家不再单独算，而是当作「第 \(E\) 个额外专家」追加进路由专家集合（见 `append_shared_expert_topk.py`），让一次 fused_experts 同时算完路由专家与共享专家。

TP 模式 MoE 前向：

[lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py:L214-L240](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py#L214-L240) —— `_moe_ffn_tp` 与 Mixtral 的 `_ffn` 结构几乎一致（gate → experts → reduce），区别仅在于：①多了 `n_shared_experts` 分支（未融合时单独算一次稠密 FFN 作为共享专家、再相加）；②调用的是 `layer_weight.experts.experts(...)` 这个统一入口，由 `FusedMoeWeight` 内部根据 `quant_method` 与 `enable_ep_moe` 选后端（4.3.3 末）。它把 `n_group`、`topk_group` 等分组参数透传进去，触发 4.2 的分组 top-k。

EP 模式 MoE 前向：

[lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py:L242-L289](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py#L242-L289) —— `_moe_ffn_edp` / `_ffn_ep_impl` 不再调用本讲的 `fused_experts_impl`，而是调用 `experts.dispatch / masked_group_gemm / combine`（基于 DeepEP）。EP 的核心是把「按专家分组」变成「按专家**跨 rank** 分发」：本 rank 把不属于自己专家的 token 发出去、收下属于自己的 token、本地用 grouped GEMM 算完、再把结果组合回去。`_ffn_ep_impl` 因此**不做** `_tpsp_allgather/_tpsp_reduce`（注释明说「EP 本身就是一种 SP 兼容」）。

> EP 路径还有低延迟版本（decode 用 `low_latency_dispatch/masked_group_gemm/low_latency_combine`，见同文件 L291-L415 的 `overlap_tpsp_token_forward`），把两个 batch 的 MoE 计算与 DeepEP 通信交错重叠，隐藏 dispatch/combine 的延迟。这部分属于 u6-l2 的 overlap 优化范畴，本讲点到为止。

模型本体的 DeepSeek 特化：

[lightllm/models/deepseek2/model.py:L17-L56](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/model.py#L17-L56) —— 继承 `LlamaTpPartModel`（所以 RoPE、注意力之外几乎都白嫖 Llama），只在 `_init_custom` 里建 DeepEP 通信组（`dist_group_manager.new_deepep_group`，参数是专家数、hidden、top-k、intermediate），并把 MLA 相关维度读进 `infer_struct`（MLA 留给 u5-l5）。注意 `_init_some_value` 里 `tp_k_head_num_=1, tp_v_head_num_=0`——这是 MLA 的特征，不是本讲重点。

#### 4.4.4 代码实践

**实践目标**：在 DeepSeek 层推理中追踪「稠密层 / TP-MoE 层 / EP-MoE 层」三态切换。

**操作步骤**：

1. 在 `deepseek2/layer_infer/transformer_layer_infer.py` 的 `__init__`（L20-L56）找到 `is_moe` 表达式，列出它依赖的三个 config 字段。
2. 在 `_bind_ffn`（L63-L71）确认三种绑定分支。
3. 打开一份 DeepSeek-V3 的 `config.json`（可从 HuggingFace 仓库或本仓库 `test/` 下的测试配置寻找），查出 `first_k_dense_replace`、`moe_layer_freq`、`n_routed_experts`、`n_shared_experts`、`num_experts_per_tok`、`n_group`、`topk_group` 的值。
4. 回答：对于 DeepSeek-V3，第 0 层是稠密还是 MoE？第 3 层呢？

**预期结果**：DeepSeek-V3 通常 `first_k_dense_replace=1`、`moe_layer_freq=1`，即第 0 层稠密、第 1 层起全部是 MoE；`n_group=8, topk_group=4, num_experts_per_tok=8`，正好匹配 `topk_select.py` 里 V3 的特判分支（L147-L148）。

> 待本地验证：具体 config 值请以你实际拿到的 `config.json` 为准；若仅做源码阅读，可跳过数值核对，重点确认「层号 → 是否 MoE」的判定逻辑。

#### 4.4.5 小练习与答案

**练习 1**：`enable_fused_shared_experts` 开启后，共享专家的权重发生了什么？

**参考答案**：共享专家不再以独立的 `gate_up_proj/down_proj` 加载（见权重类 `_init_moe` 里 `if self.num_fused_shared_experts == 0: self._load_mlp(...shared_experts...)`），而是被「重命名」并并入路由专家集合，作为第 \(E\) 个额外专家（`_rename_shared_experts` 把 `mlp.shared_experts` 改名到 `mlp.experts.<E>`）。运行时通过 `append_shared_expert_topk` 把共享专家的 id 追加进每个 token 的 `topk_ids`，使其在同一个 fused_experts 里被算掉，省一次独立 FFN 的 kernel。

**练习 2**：为什么 `_ffn_ep_impl` 不需要 `_tpsp_allgather/_tpsp_reduce`？

**参考答案**：EP（专家并行）本身已经把「不同 rank 处理不同专家」天然地当作一种切分，dispatch/combine 阶段已经完成了 token 在 rank 间的分发与结果回收，等价于完成了 SP 所需的全收集与全规约；再叠一层 `_tpsp_allgather/reduce` 会重复通信，故省略（代码注释 L284-L285 明确说明）。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成一次「**从稠密 FFN 到 MoE 的完整对照阅读**」：

1. **基线**：在 `llama/layer_infer/transformer_layer_infer.py` 的 `_ffn_tp` 旁画一张「gate_up → silu_and_mul → down」的三步图，标注每步的张量形状。
2. **路由**：在纸上为一个假想 token 推演 Mixtral 的 `fused_topk`（softmax→topk→renorm），再推演 DeepSeek 的 `grouped_topk`（softmax→选组→组内 topk→renorm），对比得到的 `topk_ids`。
3. **融合计算**：打开 `grouped_fused_moe.py` 的 `fused_experts_impl`，把五个阶段（moe_align_fused → grouped_matmul(w1) → silu_and_mul_fwd → grouped_matmul(w2, 乘权重) → moe_sum_reduce）逐一标注在第 1 步的三步图上，指出 MoE 比稠密 FFN 多出的两件事——「按专家重排 token」与「跨专家加权求和」。
4. **进阶**：在 `deepseek2/layer_infer/transformer_layer_infer.py` 里追踪 `_bind_ffn` 的三态分支，写清「稠密层 / TP-MoE / EP-MoE」各自调用的下游函数（`Llama._ffn_tp` / `fused_experts_impl` / DeepEP 的 dispatch+group_gemm+combine）。

**产出**：一份三栏对照表——「阶段 / Llama 稠密 FFN 对应代码 / Mixtral+DeepSeek MoE 对应代码」，以及一张标了三种 `_ffn` 绑定分支的 DeepSeek 层流程图。

> 待本地验证：若条件允许，启动一个 Mixtral 服务（`--model_dir <mixtral>` 并按权重类断言设置 `--enable_ep_moe`），发送一条请求确认服务可跑；再用 `nsys` 之类工具抓一拍 decode，观察 fused_moe 相关 kernel 在时间轴上的占比，印证「MoE 是 decode 阶段的主要计算之一」。

## 6. 本讲小结

- **MoE = 把一个稠密 FFN 换成一篮子专家**；在 LightLLM 里它仅替换 Transformer 层的 `_ffn` 钩子，注意力、残差、归一化全部复用稠密基类（Mixtral 层推理类只覆写 `_ffn`）。
- **专家路由**分三步：门控打分 → softmax/sigmoid → top-k → 归一化；Mixtral 用简单 top-k，DeepSeek 用分组 top-k（先选组、再选专家）做负载均衡。
- **fused_moe 算子**（`fused_experts_impl`）把「per-expert 双矩阵乘 + SiLU + 加权求和」融成五个 kernel：`moe_align_fused`（按专家重排 token）→ `grouped_matmul(w1)` → `silu_and_mul_fwd` → `grouped_matmul(w2，乘路由权重)` → `moe_sum_reduce`（跨专家求和）。
- 路由权重 `topk_weights` 只在**第二次 grouped_matmul**（down 投影）乘入，第一次（gate_up）不乘；这是稠密 FFN 没有的维度。
- **DeepSeek 三特化**：稠密层与 MoE 层混合（由 `first_k_dense_replace`/`moe_layer_freq` 控制）、共享专家（可融合进路由专家集合）、TP / EP 两条并行路径（EP 走 DeepEP 的 dispatch/combine，不再做 `_tpsp` 通信）。
- 量化与计算后端解耦：`FusedMoeWeight` 按 `quant_method` 在 Triton / DeepGEMM / Marlin / FP8 等后端间选择，无量化时落到本讲的 `fused_experts`。

## 7. 下一步学习建议

- **u5-l5（MLA 注意力）**：DeepSeek 同时使用 MoE 与 MLA，读完 MLA 即可拼出完整的 DeepSeek-V2/V3 推理图；本讲已多次预告 `q_lora_rank`、`kv_lora_rank` 等字段，正好衔接。
- **u6-l2（microbatch overlap 与 TPSP）**：本讲反复出现的 `_tpsp_allgather/_tpsp_reduce`、DeepSeek EP 的 `overlap_tpsp_token_forward`（MoE 计算与 DeepEP 通信的交错重叠）将在那里系统讲解。
- **u6-l3（FP8 量化）**：`fused_experts_impl` 的 `use_fp8_w8a8` 与 `w1_scale/w2_scale` 参数、`FuseMoeTriton` 里对 `float8_e4m3fn` 的判断，是 MoE + FP8 的入口。
- **继续阅读源码**：`fused_moe/grouped_fused_moe_ep.py`（EP 路径的 grouped GEMM）、`append_shared_expert_topk.py`（共享专家如何并入 top-k）、以及 `grouped_matmul` kernel 本体（L481-L991，本讲只讲了它的调用，未展开其内部按专家切 grid 的实现），可作为深入 fused_moe 的练习。
