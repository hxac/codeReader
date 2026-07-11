# MoE 模型推理

## 1. 本讲目标

Mixtral、DeepSeek-V2/V3 这类 **MoE（Mixture of Experts，混合专家）** 模型用「一组并行的小 FFN + 一个路由器」替换了普通 transformer 里的那一个 FFN，从而在大幅扩大参数量的同时把单次推理的计算量压在低位。本讲要回答的核心问题是：**这套替换在 LightLLM 的代码里到底落在哪一层、怎么实现？** 学完后你应该能够：

1. 说清楚 MoE 层与普通 FFN 层的**推理差异**：普通 FFN 是「每个 token 走同一个门」，MoE 是「每个 token 经路由器挑 top-k 个专家、各算各的再加权求和」。
2. 理解**专家路由（top-k gating）**的两种形态：Mixtral 的朴素 softmax top-k，以及 DeepSeek 的「分组 + 偏置」grouped top-k。
3. 掌握 **fused_moe 融合算子**的五步流水线（`moe_align` → `grouped_matmul(w1)` → `silu_and_mul` → `grouped_matmul(w2)` → `moe_sum_reduce`），并知道每一步对应 `lightllm/common/basemodel/triton_kernel/fused_moe/` 下的哪个文件。
4. 动手对比 llama 与 mixtral 的 transformer 层推理，亲自验证「MoE = 把模板的 `_ffn` 钩子换掉」这一结论。

本讲承接 [u5-l2 以 Llama 为例理解完整模型实现](./u5-l2-llama-model-walkthrough.md) 与 [u5-l3 如何新增模型支持](./u5-l3-add-new-model.md)。那里我们建立了两个关键认知：① transformer 层模板写死两条残差骨架，只把 `_ffn` 留成「必须由子类实现」的钩子；② llama 用一个稠密 FFN（gate_up → silu_and_mul → down）填这个钩子。本讲就来看 MoE 模型如何**用另一套实现填同一个钩子**。

## 2. 前置知识

在动手之前，请确认你已经理解下面几个概念（都来自前序讲义，这里只做最简回顾）：

- **`_ffn` 是模板钩子**（[u3-l3](./u3-l3-layer-infer-template.md)）：transformer 层模板的 `context_forward`/`token_forward` 在第二段残差处调用 `self._ffn(...)`，模板本身只把这个方法声明为 `raise Exception("need to impl")`。稠密模型（llama）与 MoE 模型（mixtral/deepseek2）的区别，**仅仅在于各自如何实现 `_ffn`**。
- **llama 的稠密 FFN**（[u5-l2](./u5-l2-llama-model-walkthrough.md)）：`gate_up_proj.mm(x)` → `silu_and_mul_fwd`（SwiGLU）→ `down_proj.mm`。三个权重 `gate_proj`/`up_proj`/`down_proj` 只有一份。
- **元权重与 TP 切分**（[u3-l4](./u3-l4-weights-and-tp-split.md)）：权重是「外层容器 + 内层元权重」两层结构，`ROWMMWeight`（列并行，无需 all-reduce）与 `COLMMWeight`（行并行，需 all-reduce）封装了切分细节。
- **模型即插槽**（[u5-l1](./u5-l1-model-registry.md)、[u3-l1](./u3-l1-tp-part-base-model.md)）：模型类靠填六个插槽组装，MoE 模型的「MoE 性」全部集中在 `transformer_weight_class`（专家权重）与 `transformer_layer_infer_class`（MoE 推理）这两个插槽里。

> 关键直觉：MoE 不改变 transformer 的整体骨架（注意力 + 两段残差照旧），它**只替换 FFN 那一段**。所以读懂 MoE，本质上就是读懂「`_ffn` 钩子的 MoE 版本」与「承载 N 个专家权重的 `FusedMoeWeight`」。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `lightllm/models/mixtral/model.py` | Mixtral 模型类：注册 + 填插槽，是最简 MoE 范例的入口 |
| `lightllm/models/mixtral/layer_infer/transformer_layer_infer.py` | Mixtral 的 MoE `_ffn`：路由 + 融合专家，结构最短最直观 |
| `lightllm/models/mixtral/layer_infer/_custom_ops.py` | Mixtral 用的朴素 top-k 路由（softmax + topk + 归一化），PyTorch 版 |
| `lightllm/models/mixtral/layer_weights/transformer_layer_weight.py` | Mixtral 权重：`moe_gate` 路由权重 + `FusedMoeWeight` 专家权重 |
| `lightllm/models/deepseek2/model.py` | DeepSeek-V2/V3 模型类：注册 EP 通信组，是当前**主力维护**的 MoE 实现 |
| `lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py` | DeepSeek MoE 推理：区分 dense/MoE 层、TP/EP 两种实现、共享专家、overlap |
| `lightllm/models/deepseek2/layer_weights/transformer_layer_weight.py` | DeepSeek 权重：按层判断 dense/MoE、加载共享专家、`FusedMoeWeight` |
| `lightllm/common/basemodel/layer_weights/meta_weights/fused_moe/fused_moe_weight.py` | `FusedMoeWeight`：N 个专家权重的容器，对外暴露统一的 `experts()` 接口 |
| `lightllm/common/basemodel/layer_weights/meta_weights/fused_moe/impl/triton_impl.py` | Triton 版 MoE 实现：`_select_experts`（路由）+ `_fused_experts`（融合专家） |
| `lightllm/common/basemodel/triton_kernel/fused_moe/topk_select.py` | 统一路由入口 `select_experts`：按 `use_grouped_topk` 分派朴素/分组路由 |
| `lightllm/common/basemodel/triton_kernel/fused_moe/grouped_topk.py` | DeepSeek 专用 grouped top-k Triton kernel（含 `e_score_correction_bias`） |
| `lightllm/common/basemodel/triton_kernel/fused_moe/grouped_fused_moe.py` | **fused_moe 主算子**：`fused_experts_impl` 五步流水线 + `grouped_matmul` |
| `lightllm/common/basemodel/triton_kernel/fused_moe/moe_sum_reduce.py` | 把 top-k 个专家输出按 token 加权求和归并 |
| `lightllm/common/basemodel/triton_kernel/fused_moe/grouped_fused_moe_ep.py` | EP（专家并行）版融合专家，走 DeepEP 的 all-to-all dispatch/combine |
| `lightllm/models/llama/layer_infer/transformer_layer_infer.py` | llama 稠密 `_ffn`/`_ffn_tp`，作为 MoE 的对照基准 |

> 一个重要的事实提示（详见 4.1.3）：Mixtral 的 `model.py` 与 `_ffn` 是讲解 MoE **概念**的最佳入口（短、直白），但它的 `_ffn` 函数体里引用的 `fused_experts_impl` 导入路径与 `experts.w1[0]` 访问方式相对当前的 `FusedMoeWeight` 已经**滞后**（当前 `FusedMoeWeight` 没有 `.w1` 属性，统一用 `.w13/.w2`）。因此本讲用 Mixtral 讲「MoE 长什么样」，用 **DeepSeek-V2/V3** 讲「MoE 在当前代码里真正怎么跑」。源码精读的关键结论请以 DeepSeek 路径为准。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**MoE 推理**（MoE 如何替换普通 FFN）、**专家路由**（top-k gating）、**fused_moe 算子**（融合专家的五步流水线）。

### 4.1 MoE 推理

#### 4.1.1 概念说明

普通 transformer 的每一层只有一个 FFN，所有 token 都走同一个 `gate_up → act → down`。**MoE** 把这一个 FFN 换成 \(E\) 个「专家」\(F_1, F_2, \dots, F_E\)（每个专家本身就是一个小 FFN），再加一个**路由器（router / gate）**：一个形状为 `[hidden, E]` 的小矩阵，把 token 映射成 \(E\) 个得分。每个 token 只激活得分最高的 \(k\) 个专家（top-k），把它们的输出按路由权重加权求和：

\[
\text{out}(x)=\sum_{i\in\text{topk}} g_i(x)\cdot F_i(x),\qquad g(x)=\text{softmax}(W_g x)
\]

这样虽然总参数量是稠密 FFN 的约 \(E/k\) 倍，但**单次推理每个 token 只动用 \(k\) 个专家**，计算量与一个普通 FFN 相当——这就是 MoE「参数量大、计算量小」的由来。

> 名词解释——**专家（expert）**：MoE 层里一个独立的 FFN（自带 gate/up/down 三权重）。**路由器（gate/router）**：决定 token 送进哪些专家的小线性层。**top-k**：每个 token 只选路由得分最高的 \(k\) 个专家（Mixtral 是 2，DeepSeek-V3 是 8）。**共享专家（shared expert）**：DeepSeek 特有的、对所有 token 都激活的额外专家，单独以稠密形式计算，最后加到 MoE 输出上。

#### 4.1.2 核心流程

MoE 不改 transformer 骨架，只把模板的 `_ffn` 钩子换成 MoE 版本。一次 MoE 前向（以 TP 模式、单卡视角）是：

```text
_ffn(x):                                    # MoE 版 _ffn（替换 llama 的稠密 _ffn）
  ├─ x = _tpsp_allgather(x)                 # TPSP：先 allgather 完整 hidden
  ├─ router_logits = moe_gate.mm(x)         # 路由器：[N, hidden] × [hidden, E] → [token, E]
  ├─ topk_weights, topk_ids = 路由(x, router_logits)   # 每个 token 选 k 个专家（见 4.2）
  ├─ out = fused_experts(x, experts权重, topk_weights, topk_ids)  # 融合专家五步（见 4.3）
  ├─（若有 shared expert）out += shared_output
  └─ return _tpsp_reduce(out)                # TPSP：行并行结果 all-reduce
```

与 llama 稠密 `_ffn` 对比，MoE 版多了「路由 + 选专家 + 按专家分组计算 + 归并」这几步，而 `gate/up/down` 从「一份」变成「E 份」。

DeepSeek 在此基础上还多两个维度：① **逐层判断 dense/MoE**——前几层是稠密 FFN，后面才切到 MoE；② **TP / EP 两种实现**——`--enable_ep_moe` 时走专家并行（专家分散到各 rank，用 DeepEP all-to-all 传递 token），否则走张量并行（每个 rank 持有全部专家的一片）。

#### 4.1.3 源码精读

**(a) MoE 是怎么「插」进 transformer 的：`_ffn` 钩子**

transformer 层模板在 prefill/decode 两条骨架里都在第二段残差处调用 `self._ffn(...)`，而模板自身把 `_ffn` 留成空钩子：

[lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py:53-54](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py#L53-L54) —— 模板的 `_ffn` 必须由子类实现：

```python
def _ffn(self, input, infer_state, layer_weight) -> torch.Tensor:
    raise Exception("need to impl")
```

[lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py:73-74](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py#L73-L74) —— 骨架在固定位置调用它，prefill（`context_forward`）与 decode（`token_forward`，见第 95-96 行）各调一次：

```python
input1 = self._ffn_norm(input_embdings, infer_state, layer_weight)
ffn_out = self._ffn(input1, infer_state, layer_weight)   # ← MoE 与稠密的分叉点
```

**对照基准——llama 用稠密 FFN 填这个钩子**：

[lightllm/models/llama/layer_infer/transformer_layer_infer.py:118-129](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py#L118-L129) —— 一次 matmul（gate+up 融合）+ SwiGLU + 一次 matmul（down），权重只有一份：

```python
def _ffn_tp(self, input, infer_state, layer_weight):
    input = input.view(-1, self.embed_dim_)
    up_gate_out = layer_weight.gate_up_proj.mm(input)
    ffn1_out = self.alloc_tensor((input.size(0), up_gate_out.size(1) // 2), input.dtype)
    silu_and_mul_fwd(up_gate_out, ffn1_out)      # SwiGLU 激活
    ffn2_out = layer_weight.down_proj.mm(ffn1_out)
    return ffn2_out
```

**MoE 版——Mixtral 用「路由 + 融合专家」填同一个钩子**：

[lightllm/models/mixtral/layer_infer/transformer_layer_infer.py:18-45](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/mixtral/layer_infer/transformer_layer_infer.py#L18-L45) —— 结构与上面一一对应，只是在两次 matmul 之外多了「路由 + 选专家」，权重从 `gate_up_proj`/`down_proj` 换成承载 E 个专家的 `experts`：

```python
def _ffn(self, input, infer_state, layer_weight):
    hidden_states = input.view(-1, self.embed_dim_)
    hidden_states = self._tpsp_allgather(input=hidden_states, infer_state=infer_state)
    router_logits = layer_weight.moe_gate.mm(hidden_states)          # 路由器打分
    topk_weights, topk_ids = fused_topk(                            # 选 top-k 专家
        hidden_states=hidden_states, gating_output=router_logits,
        topk=self.num_experts_per_tok, renormalize=self.renormalize,
        alloc_tensor_func=self.alloc_tensor)
    ffn2_out = fused_experts_impl(                                  # 融合专家（见 4.3）
        hidden_states=hidden_states, w1=..., w2=...,
        topk_weights=topk_weights, topk_ids=topk_ids, inplace=True, ...)
    return self._tpsp_reduce(input=ffn2_out, infer_state=infer_state)
```

> 准确性提示：Mixtral 这段 `_ffn` 里 `from lightllm.common.fused_moe.grouped_fused_moe import fused_experts_impl` 的导入路径，以及 `layer_weight.experts.w1[0]`/`experts.w2[0]` 的取法，相对当前 HEAD 已**滞后**——真实的融合专家算子在 `lightllm/common/basemodel/triton_kernel/fused_moe/grouped_fused_moe.py`，而当前 `FusedMoeWeight` 用 `.w13/.w2`（`WeightPack`）而非 `.w1[0]`。所以 Mixtral 这段代码更适合理解「MoE 的骨架长什么样」，**真正能跑通、被持续维护的是 DeepSeek 路径**（见 (c)）。下面 (b) 先看 Mixtral 的权重组织，(c) 再看 DeepSeek 的完整实现。

**(b) Mixtral 的 MoE 权重组装**

[lightllm/models/mixtral/layer_weights/transformer_layer_weight.py:33-60](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/mixtral/layer_weights/transformer_layer_weight.py#L33-L60) —— Mixtral 在 `_init_moe` 里建两样东西：一个路由权重 `moe_gate`，一个 `FusedMoeWeight` 专家集合：

```python
def _init_moe(self):
    inter_size = self.network_config_["intermediate_size"]
    self.moe_gate = ROWMMWeight(                       # 路由器：[hidden] × [E]
        in_dim=self.n_embed, out_dims=[self.n_routed_experts],
        weight_names=self.moe_gate_weight_name, ...)
    self.experts = FusedMoeWeight(                     # E 个专家的 gate/up/down
        gate_proj_name="w1", down_proj_name="w2", up_proj_name="w3",
        weight_prefix=f"model.layers.{self.layer_num_}.block_sparse_moe.experts",
        n_routed_experts=self.n_routed_experts, hidden_size=self.n_embed,
        moe_intermediate_size=inter_size, ...)
```

注意路由权重 `moe_gate` 显式设了 `tp_world_size=1`——**路由器不切分**，每个 rank 都用完整的路由矩阵给所有 token 打分；专家本身的 TP 切分交给 `FusedMoeWeight` 内部处理。

[lightllm/models/mixtral/model.py:18-30](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/mixtral/model.py#L18-L30) —— Mixtral 的模型类依然是「填插槽」，与 llama 唯一的不同只是 `transformer_weight_class` 换成了带专家的 `MixtralTransformerLayerWeight`、`transformer_layer_infer_class` 换成了 MoE 版推理类：

```python
@ModelRegistry("mixtral")
class MixtralTpPartModel(TpPartBaseModel):
    pre_and_post_weight_class = LlamaPreAndPostLayerWeight     # 复用 llama 的 pre/post
    transformer_weight_class = MixtralTransformerLayerWeight   # ★ 带 MoE 专家
    pre_layer_infer_class = LlamaPreLayerInfer
    post_layer_infer_class = LlamaPostLayerInfer
    transformer_layer_infer_class = MixtralTransformerLayerInfer  # ★ MoE 推理
    infer_state_class = LlamaInferStateInfo
```

这正是 [u5-l3](./u5-l3-add-new-model.md) 强调的「模型 = 子类填好的插槽」——MoE 并没有打破这个范式。

**(c) 当前主力实现：DeepSeek 的 dense/MoE 分层与 TP/EP 双实现**

DeepSeek-V2/V3 比 Mixtral 复杂得多：模型的前几层是稠密 FFN，后续层才是 MoE；并且 MoE 层还支持 TP 与 EP 两种并行。这两个「分叉」都在推理类的初始化里就决定好了。

[lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py:32-42](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py#L32-L42) —— 构造时按层号判断当前层是不是 MoE，并读出路由相关超参：

```python
self.is_moe = (
    network_config["n_routed_experts"] is not None
    and layer_num >= network_config["first_k_dense_replace"]      # 前几层稠密
    and layer_num % network_config.get("moe_layer_freq", 1) == 0  # 每 moe_layer_freq 层一个 MoE
)
self.n_shared_experts = network_config["n_shared_experts"]
self.num_experts_per_tok = network_config["num_experts_per_tok"]
self.norm_topk_prob = network_config["norm_topk_prob"]
self.n_group = network_config["n_group"]            # DeepSeek 分组路由的组数
self.topk_group = network_config["topk_group"]      # 每个 token 选用几个组
```

[lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py:63-71](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py#L63-L71) —— `_bind_ffn` 在初始化末尾把 `self._ffn` 绑成三种实现之一，模板骨架调用 `self._ffn` 时就会走到对应分支：

```python
def _bind_ffn(self):
    if self.is_moe:
        enable_ep_moe = get_env_start_args().enable_ep_moe
        if enable_ep_moe:
            self._ffn = self._ffn_ep_impl     # 专家并行：专家分散到各 rank
        else:
            self._ffn = self._ffn_tp_impl     # 张量并行：每 rank 持有全部专家的一片
    else:
        self._ffn = partial(LlamaTransformerLayerInfer._ffn, self)   # 稠密层：直接复用 llama 的
```

[lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py:214-240](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py#L214-L240) —— TP 模式的 MoE 主体 `_moe_ffn_tp`，是与 Mixtral 同构的「路由 + 融合专家 + 共享专家」，但路由与融合都委托给 `layer_weight.experts`（`FusedMoeWeight`）的统一接口 `experts(...)`：

```python
def _moe_ffn_tp(self, input, infer_state, layer_weight):
    hidden_states = input.view(-1, self.embed_dim_)
    # 若未启用共享专家融合，则单独把共享专家当稠密 FFN 算
    if self.n_shared_experts is not None and layer_weight.num_fused_shared_experts == 0:
        shared_output = LlamaTransformerLayerInfer._ffn_tp(self, hidden_states, infer_state, layer_weight)
    router_logits = layer_weight.moe_gate.mm(hidden_states.to(layer_weight.moe_gate.data_type_))
    layer_weight.experts.experts(                       # ★ 统一入口：内部完成「路由 + 融合专家」
        hidden_states, router_logits=router_logits,
        top_k=self.num_experts_per_tok, renormalize=self.norm_topk_prob,
        use_grouped_topk=self.n_group, topk_group=self.topk_group, num_expert_group=self.n_group)
    if self.n_shared_experts is not None and layer_weight.num_fused_shared_experts == 0:
        hidden_states.add_(shared_output)               # 共享专家结果加回去
    return hidden_states.view(num_tokens, hidden_dim)
```

注意 `experts()` 是**原地写回** `hidden_states`（`fused_experts` 用 `inplace=True`，见 4.3），所以没有左值赋值。`_ffn_tp_impl`（[第 270-279 行](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py#L270-L279)）只在外面再包一层 `_tpsp_allgather`/`_tpsp_reduce`，把 MoE 嵌进 TPSP 混合并行（见 [u6-l2](./u6-l2-microbatch-overlap-tpsp.md)）。

**(d) DeepSeek 的模型类：建 EP 通信组**

[lightllm/models/deepseek2/model.py:49-56](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/model.py#L49-L56) —— DeepSeek 在 `_init_custom` 里（RoPE 之外）调用 `dist_group_manager.new_deepep_group(...)`，把专家数、隐层维度、每 token 专家数等注册成一个 DeepEP 通信组——这是 `--enable_ep_moe` 时 all-to-all dispatch/combine 的前提：

```python
def _init_custom(self):
    self._init_to_get_yarn_rotary()
    dist_group_manager.new_deepep_group(
        self.config["n_routed_experts"], self.config["hidden_size"],
        self.config.get("num_experts_per_tok", 1),
        self.config.get("moe_intermediate_size", self.config.get("intermediate_size")))
```

#### 4.1.4 代码实践

**实践目标**：通过对比 llama 与 mixtral 的 transformer 推理类，亲眼确认「MoE = 换 `_ffn` 钩子」这一结论，并定位 MoE 与稠密在代码上的**唯一分叉点**。

**操作步骤**：

1. 打开 [lightllm/models/llama/layer_infer/transformer_layer_infer.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py)，确认它的钩子集合：`_att_norm`/`_ffn_norm`/`_get_qkv`/两个注意力核/`_get_o`/`_ffn`（与 [u5-l2](./u5-l2-llama-model-walkthrough.md) 一致）。
2. 打开 [lightllm/models/mixtral/layer_infer/transformer_layer_infer.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/mixtral/layer_infer/transformer_layer_infer.py)，发现它**只覆写了 `_ffn`**（其余注意力、归一化钩子全部继承自 `LlamaTransformerLayerInfer`）。
3. 打开 [lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py)，找到 `_bind_ffn`（[第 63 行](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py#L63)），看它如何把 `self._ffn` 绑成 dense / MoE-TP / MoE-EP 三种之一。
4. 回到模板 [transformer_layer_infer_template.py:73-74 与 95-96](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py#L73-L74)，确认 prefill/decode 骨架调用的是 `self._ffn`——所以无论 `_ffn` 被绑成什么，骨架代码一行都不用改。

**需要观察的现象**：

- Mixtral 的推理类比 llama **只多了一个 `_ffn` 的覆写**，其余钩子完全复用——MoE 的「全部复杂性」都被收拢进 `_ffn`。
- DeepSeek 的 `_bind_ffn` 把「这层是不是 MoE」「走 TP 还是 EP」两个决策在初始化期就固化成具体的函数绑定，运行期 `self._ffn(...)` 直接派发，没有额外分支开销。
- 模板骨架对稠密与 MoE **完全无感**：它只认 `self._ffn` 这个名字。

**预期结果**：你会得出结论——在 LightLLM 里实现一个 MoE 模型，注意力部分可以原样复用 llama，**真正要写的只有 `_ffn` 的 MoE 版本与承载专家的 `FusedMoeWeight`**；这与 [u5-l3](./u5-l3-add-new-model.md) 「结构偏离标准 transformer 的程度决定工作量」的论断一致。

> 待本地验证：第 2、3 步里「Mixtral 只覆写 `_ffn`」「DeepSeek 三分支绑定」建议你亲手用编辑器折叠/搜索确认，行号会随提交漂移。

#### 4.1.5 小练习与答案

**练习 1**：DeepSeek 为什么要在构造函数里判断 `self.is_moe`，而不是为稠密层和 MoE 层各写一个推理类？

**参考答案**：因为同一个模型（DeepSeek-V2/V3）**内部混合了两种层**——前 `first_k_dense_replace` 层是稠密 FFN，其后才是 MoE 层。这些层共享同一套注意力实现（MLA）、同一套权重容器结构，只有 FFN 段不同。用一个推理类 + `_bind_ffn` 按层号绑定不同 `_ffn`，既避免了重复实现注意力，又能让模板骨架对每一层统一调用 `self._ffn`。这正是模板方法模式的价值：把「变化的部分」（FFN）做成可替换钩子，把「不变的部分」（残差、注意力、KV 落池）固化在骨架里。

**练习 2**：Mixtral 的 `moe_gate`（路由权重）为什么显式设 `tp_world_size=1` 不做切分，而专家权重却要切分？

**参考答案**：路由器要为每个 token 在**全部** \(E\) 个专家里打分并选 top-k，所以每个 rank 都必须看到完整的 \(E\) 维得分，路由矩阵不能沿专家维切分（否则每个 rank 只能看到部分专家、无法做全局 top-k）。而专家本身是「互相独立」的 FFN，可以按张量并行把每个专家的 `gate/up`（列并行）、`down`（行并行）像普通 FFN 那样切开，由 `FusedMoeWeight` 内部的 `row_slicer`/`col_slicer` 完成。

### 4.2 专家路由

#### 4.2.1 概念说明

路由（expert routing / top-k gating）是 MoE 的灵魂：它决定**每个 token 送进哪 \(k\) 个专家**。LightLLM 里有两套路由实现：

- **朴素 top-k**（Mixtral 用）：`softmax(路由得分)` → 取 top-k → 归一化。逻辑直白，是一个 `[token, E]` 上的 softmax + topk。
- **分组 top-k（grouped top-k）**（DeepSeek-V2/V3 用）：先把 \(E\) 个专家分成 \(n\_group\) 组，**先在组间选 `topk_group` 个组**，再在选中的组里取 top-k 专家，并可叠加一个 `e_score_correction_bias` 偏置。这是 DeepSeek 为均衡专家负载而设计的「biased grouped top-k」。

> 名词解释——**`e_score_correction_bias`**：DeepSeek 给每个专家的一个可学习偏置项，加在路由得分上用来**纠正专家被选中的频率**（负载均衡）。朴素 top-k 没有这一项。**`norm_topk_prob`（renormalize）**：选出 top-k 后，是否把 \(k\) 个路由权重重新归一化到和为 1。

#### 4.2.2 核心流程

**朴素 top-k**（Mixtral 的 `_custom_ops.fused_topk`）：

```text
scores = softmax(router_logits)            # [token, E]
topk_weights, topk_ids = topk(scores, k)   # 每 token 取最大的 k 个
if renormalize: topk_weights /= topk_weights.sum()   # 归一化
```

**分组 top-k**（DeepSeek，Triton kernel `grouped_topk_kernel`）：

```text
scores = softmax(router_logits) + e_score_correction_bias   # 加偏置
group_scores = scores.view(token, n_group, E/n_group)       # 分组
               .topk(group_score_used_topk_num).sum(-1)     # 每组取前几名求和作为「组分」
group_topk_value = sort(group_scores)[topk_group - 1]       # 第 topk_group 大的组分作阈值
mask_scores = where(group_scores >= group_topk_value, scores, -inf)  # 只保留选中组里的专家
topk_weights, topk_ids = argsort(mask_scores)[:topk]        # 在选中组里取 top-k
if renormalize: topk_weights /= topk_weights.sum()
```

二者最终都产出两个形状为 `[token, k]` 的张量：`topk_weights`（float32，路由权重）与 `topk_ids`（int32/int64，被选中的专家编号）。下游的融合专家算子只认这两个张量，不关心路由是怎么选出来的——这就是路由与计算解耦的关键。

#### 4.2.3 源码精读

**(a) Mixtral 的朴素路由（PyTorch 版）**

[lightllm/models/mixtral/layer_infer/_custom_ops.py:15-46](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/mixtral/layer_infer/_custom_ops.py#L15-L46) —— 这是 LightLLM 里最直白的路由实现，用来理解概念最合适：

```python
def topk_softmax(topk_weights, topk_ids, token_expert_indicies, gating_output, topk=2):
    scores = torch.softmax(gating_output, dim=-1)
    topk_weights, topk_ids = torch.topk(scores, k=topk, dim=-1, sorted=False)
    return topk_weights, topk_ids

def fused_topk(hidden_states, gating_output, topk, renormalize, alloc_tensor_func=torch.empty):
    ...
    topk_weights, topk_ids = topk_softmax(..., gating_output.float(), topk)
    if renormalize:
        topk_weights = topk_weights / topk_weights.sum(dim=-1, keepdim=True)
    return topk_weights, topk_ids
```

> 准确性提示：这是 Mixtral 目录里**自带的、偏教学/旧式**的 PyTorch 版路由。当前主力 MoE（DeepSeek）并不用它，而是走下面的 `select_experts` 统一入口；那里的 `fused_topk`（[topk_select.py:27-48](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/fused_moe/topk_select.py#L27-L48)）会优先调用 sglang 的 `sgl_ops.topk_softmax` CUDA 算子，找不到才退化到 Triton `softmax_topk`。两处都叫 `fused_topk`，注意区分上下文。

**(b) DeepSeek 的统一路由入口 `select_experts`**

[lightllm/common/basemodel/triton_kernel/fused_moe/topk_select.py:126-160](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/fused_moe/topk_select.py#L126-L160) —— DeepSeek 的 `FuseMoeTriton._select_experts` 调用的就是它。它按 `use_grouped_topk` 分派到两种实现：

```python
def select_experts(hidden_states, router_logits, correction_bias, top_k,
                   use_grouped_topk, renormalize, topk_group, num_expert_group, scoring_func="softmax", ...):
    if use_grouped_topk:                       # DeepSeek：分组路由
        topk_weights, topk_ids = triton_grouped_topk(
            hidden_states, router_logits, correction_bias, topk=top_k, renormalize=renormalize,
            num_expert_group=num_expert_group, topk_group=topk_group, scoring_func=scoring_func, ...)
    elif custom_routing_function is None:      # 朴素路由（Mixtral 风格）
        topk_weights, topk_ids = fused_topk(hidden_states, router_logits, topk=top_k, renormalize=renormalize)
    ...
    return topk_weights, topk_ids
```

[lightllm/common/basemodel/layer_weights/meta_weights/fused_moe/impl/triton_impl.py:34-63](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/fused_moe/impl/triton_impl.py#L34-L63) —— Triton 版 MoE 实现里，`_select_experts` 把 `correction_bias`（即 DeepSeek 的 `e_score_correction_bias`）与分组参数透传给 `select_experts`，并在路由完成后处理 `routed_scaling_factor`（DeepSeek-V3 路由权重的全局缩放）：

```python
def _select_experts(self, input_tensor, router_logits, correction_bias, top_k, renormalize,
                    use_grouped_topk, topk_group, num_expert_group, scoring_func, ...):
    topk_weights, topk_ids = select_experts(
        hidden_states=input_tensor, router_logits=router_logits, correction_bias=correction_bias,
        use_grouped_topk=use_grouped_topk, top_k=top_k, renormalize=renormalize,
        topk_group=topk_group, num_expert_group=num_expert_group, scoring_func=scoring_func)
    if self.routed_scaling_factor != 1.0:
        topk_weights.mul_(self.routed_scaling_factor)
    ...
    return topk_weights, topk_ids
```

**(c) DeepSeek 分组路由 kernel 的关键逻辑**

[lightllm/common/basemodel/triton_kernel/fused_moe/grouped_topk.py:123-166](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/fused_moe/grouped_topk.py#L123-L166) —— 这是单个 token 的路由逻辑（一个 program 处理一个 token）。先算分（softmax 或 sigmoid），叠偏置，再分组选组：

```python
if IS_SIGMOID: old_scores = tl.sigmoid(hidden_states)
else:          old_scores = tl.softmax(hidden_states)
if HAS_CORRECTION_BIAS:
    scores = old_scores + tl.load(correction_bias_ptr + offs_n, ...)   # 叠 e_score_correction_bias
...
group_value = tl.sum(                                                    # 组分 = 组内 top-N 求和
    tl.where(..., tl.sort(group_scores, dim=1, descending=True), 0.0), axis=1)
sorted_group_value = tl.sort(group_value, descending=True)
group_topk_value = tl.sum(tl.where(offs_group == group_topk_num - 1, sorted_group_value, 0.0))  # 第 topk_group 大
mask_group_scores = tl.where(group_value >= group_topk_value, group_scores, -1e7)  # 只留选中组
```

随后 [第 185-201 行](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/fused_moe/grouped_topk.py#L185-L201) 对 `mask_scores` 做一次 `argsort`（用 bitonic 排序，见 [第 79-89 行](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/fused_moe/grouped_topk.py#L79-L89)），取出前 `topk_num` 个专家的权重与编号，可选地归一化后写回输出张量。

> 注意 `topk_select.py:145-148` 有一处特判：当 `topk_group==4, num_expert_group==8, top_k==8`（DeepSeek-V3 的标准配置）时，组分用「组内 top-2 求和」（`group_score_topk_num=2`）；否则用 top-1。这是为了匹配 DeepSeek-V3 官方的路由实现。

#### 4.2.4 代码实践

**实践目标**：把两种路由的「输入 → 输出」对齐，确认它们都产出同样形状的 `(topk_weights, topk_ids)`，从而理解「路由与计算解耦」。

**操作步骤**：

1. 在 [topk_select.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/fused_moe/topk_select.py) 里对比 `fused_topk`（[第 27-48 行](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/fused_moe/topk_select.py#L27-L48)）与 `grouped_topk`（[第 52-87 行](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/fused_moe/topk_select.py#L52-L87)）的返回类型，确认两者都返回 `(float32 [token,k], int32 [token,k])`。
2. 阅读 `grouped_topk`（PyTorch 参考版，[第 52-87 行](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/fused_moe/topk_select.py#L52-L87)），它用 `view + max + topk + scatter_ + masked_fill` 把「分组选组」表达得比 Triton kernel 更易懂，可作为理解 4.2.3(c) 的脚手架。
3. 追踪 `correction_bias` 的来源：从 [triton_impl.py __call__ 转发 correction_bias（第 129 行）](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/fused_moe/impl/triton_impl.py#L126-L130) 出发，回到 [fused_moe_weight.py 的 experts() 传 correction_bias（第 145 行）与 _create_weight 创建它（第 289-298 行）](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/fused_moe/fused_moe_weight.py#L289-L298)，看 `e_score_correction_bias` 这个张量是怎么从 HF 权重里加载的（键名 `mlp.gate.e_score_correction_bias`，见 [deepseek2 权重 _init_weight_names:57](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/layer_weights/transformer_layer_weight.py#L57)）。

**需要观察的现象**：

- 朴素与分组两种路由的**输出形状完全一致**，都是 `[token, k]` 的权重与编号——下游 `fused_experts` 对路由算法无感。
- `e_score_correction_bias` 是一个长度为 \(E\) 的一维向量（每专家一个偏置），在路由 kernel 里**加在 softmax 之后、分组之前**。
- DeepSeek-V3 的 `routed_scaling_factor` 是在路由**之后**乘到 `topk_weights` 上的（`triton_impl.py:62-63`），不在 kernel 内部。

**预期结果**：你会确认「路由」是一个**可独立替换的模块**——只要产出符合约定的 `(topk_weights, topk_ids)`，换一套路由算法（比如改成 sigmoid 评分、改成更多的组）完全不用动融合专家算子。这正是 LightLLM 把路由与 `fused_experts` 分成两步的设计动机。

> 待本地验证：第 2 步的 PyTorch 参考版 `grouped_topk` 非常适合在 CPU 上造一个小张量手算验证，建议有条件时实际跑一下，对照 Triton kernel 结果。

#### 4.2.5 小练习与答案

**练习 1**：DeepSeek 的 grouped top-k 为什么要「先选组、再在组里选专家」，而不是直接在全部专家里选 top-k？

**参考答案**：主要是为了**专家负载均衡**。直接全局 top-k 容易让少数「强势」专家被几乎所有 token 选中、其他专家闲置，既浪费参数又让显存/计算热点集中。DeepSeek 把专家分成若干组，先保证 token 的选择来自「足够多」的组（`topk_group`），从而把流量强制分散到更多组、更多专家上；`e_score_correction_bias` 进一步在训练中学习纠正各专家的被选频率。这是一种「结构性 + 可学习」的负载均衡策略。

**练习 2**：路由权重 `topk_weights` 最终在哪里、以什么方式作用到专家输出上？

**参考答案**：作用点在融合专家算子的**第二次 `grouped_matmul`（w2/down）**里，通过参数 `mul_routed_weight=True` 实现——它把每个 token-专家对的 `topk_weights` 直接乘到该专家 down 投影的输出上（见 4.3 的 `fused_experts_impl` 第二次 `grouped_matmul`）。随后 `moe_sum_reduce` 把同一 token 的 \(k\) 个专家加权输出**求和**，得到 \(\sum_i g_i \cdot F_i(x)\)。所以路由权重是「在 w2 之后、求和之前」乘上去的。

### 4.3 fused_moe 算子

#### 4.3.1 概念说明

选完专家后，要把每个 token 送进它挑中的 \(k\) 个专家各算一遍，再加权求和。**朴素实现**是循环 \(E\) 个专家、对每个专家挑出分配给它的 token 做一次 batched matmul——但这样会有大量小 kernel launch、且 token-to-expert 的分发/归并开销大。**fused_moe** 的做法是把这整个过程融合进一组 Triton kernel：

1. **`moe_align`**：按 `topk_ids` 把 token 重新分桶到各专家（产出「专家 → token 索引列表」的映射），让同一专家要算的 token 在内存里连续。
2. **`grouped_matmul`（w1，gate+up 融合）**：一个 kernel 跑完所有专家的第一次 matmul，每个 program block 处理「某专家 × 某 token 段」。
3. **`silu_and_mul`**：SwiGLU 激活（gate 与 up 相乘），与 llama 稠密版用的是同一个 `silu_and_mul_fwd`。
4. **`grouped_matmul`（w2，down）**：第二次 matmul，这次 `mul_routed_weight=True` 把路由权重乘上。
5. **`moe_sum_reduce`**：把每个 token 的 \(k\) 个专家输出沿 expert 维求和，得到最终 `[token, hidden]`。

> 名词解释——**`grouped_matmul`（分组矩阵乘）**：一次 kernel 调用完成「多个不同权重矩阵（各专家）× 各自的 token 段」的乘法，按 `(expert_id, m_block, n_block)` 三维 grid 并行，避免逐专家 launch。**`moe_align`**：把「token 选了哪些专家」这种稀疏、分散的信息，重排成「每个专家对应一段连续 token」的稠密索引，为 grouped_matmul 喂数据。

#### 4.3.2 核心流程

fused_moe 的五步流水线（对应 `fused_experts_impl`，TP 模式）：

```text
输入：hidden_states [M, hidden]，topk_weights/topk_ids [M, k]，专家权重 w1[E,2N,hidden]、w2[E,hidden,N]

for chunk in chunks(hidden_states, FFN_MOE_CHUNK_SIZE):     # 长序列分块，控制显存
  ① moe_align_fused(topk_ids, topk_weights)
       → expert_to_tokens[E, ...], expert_to_token_num[E]   # 每 expert 的 token 列表与计数
  ② grouped_matmul(x, w1, expert_to_tokens, mul_routed_weight=False)
       → cache1 [m, k, 2N]                                   # 每 token-k 的 gate∥up
  ③ silu_and_mul_fwd(cache1) → cache2 [m, k, N]             # SwiGLU：silu(gate)*up
  ④ grouped_matmul(cache2, w2, expert_to_tokens, mul_routed_weight=True)
       → cache3 [m, k, hidden]                               # down 投影，乘上路由权重
  ⑤ moe_sum_reduce(cache3) → out [m, hidden]                # k 个专家加权求和
```

> 第三步的 `silu_and_mul` 与 llama 稠密 FFN（[u5-l2](./u5-l2-llama-model-walkthrough.md)）用的是**同一个 kernel** `silu_and_mul_fwd`——MoE 与稠密 FFN 在「激活函数」这一步没有任何区别，区别只在外层的「分组计算 + 求和」。

#### 4.3.3 源码精读

**(a) `FusedMoeWeight`：N 个专家的统一容器**

路由之后、融合专家之前，要先看懂专家权重是怎么存的。[lightllm/common/basemodel/layer_weights/meta_weights/fused_moe/fused_moe_weight.py:127-155](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/fused_moe/fused_moe_weight.py#L127-L155) —— 对外的统一入口是 `experts()`，它把所有细节（路由权重、w13/w2、偏置、评分函数）打包转发给一个具体实现 `self.fuse_moe_impl`：

```python
def experts(self, input_tensor, router_logits, top_k, renormalize,
            use_grouped_topk, topk_group, num_expert_group, is_prefill=None, ...):
    return self.fuse_moe_impl(
        input_tensor=input_tensor, router_logits=router_logits,
        w13=self.w13, w2=self.w2, correction_bias=self.e_score_correction_bias,
        scoring_func=self.scoring_func, top_k=top_k, renormalize=renormalize,
        use_grouped_topk=use_grouped_topk, ...)
```

[lightllm/common/basemodel/layer_weights/meta_weights/fused_moe/fused_moe_weight.py:287-324](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/fused_moe/fused_moe_weight.py#L287-L324) —— 专家权重的实体是 `w13`（gate+up，形状 `[local_n_experts, intermediate, hidden]`）与 `w2`（down，形状 `[local_n_experts, hidden, intermediate]`），由 `quant_method.create_moe_weight` 按**本 rank 实际持有的专家数** `local_n_routed_experts` 分配（TP 时持有全部专家的一片，EP 时只持有分到本 rank 的那几个专家 + 冗余专家）：

```python
def _create_weight(self):
    intermediate_size = self.split_inter_size            # TP 切分后的 intermediate
    self.w13, w13_param_list = self.quant_method.create_moe_weight(
        out_dims=[intermediate_size, intermediate_size], in_dim=self.hidden_size,
        ..., num_experts=self.local_n_routed_experts)    # gate 与 up 各一份
    self.w2, _ = self.quant_method.create_moe_weight(
        out_dims=[self.hidden_size], in_dim=intermediate_size,
        ..., num_experts=self.local_n_routed_experts)
```

[lightllm/common/basemodel/layer_weights/meta_weights/fused_moe/fused_moe_weight.py:91-125](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/fused_moe/fused_moe_weight.py#L91-L125) —— TP 与 EP 下「本 rank 持有哪些专家」的差异就体现在 `_init_parallel_params`：TP 时 `local_expert_ids = range(E)`（每 rank 持全部专家的一片），EP 时按 `global_rank` 算出本 rank 的专家段并叠加冗余专家：

```python
if self.enable_ep_moe:
    n_experts_per_rank = self.n_routed_experts // self.global_world_size
    start_expert_id = self.global_rank_ * n_experts_per_rank
    self.local_expert_ids = list(range(start_expert_id, start_expert_id + n_experts_per_rank)) + self.redundancy_expert_ids
else:
    self.local_expert_ids = list(range(self.n_routed_experts + self.num_fused_shared_experts))
```

[lightllm/common/basemodel/layer_weights/meta_weights/fused_moe/fused_moe_weight.py:359-375](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/fused_moe/fused_moe_weight.py#L359-L375) —— 每个专家的 `gate/up`（w1/w3）用 `row_slicer`（列并行）切，`down`（w2）用 `col_slicer`（行并行）切，与 [u3-l4](./u3-l4-weights-and-tp-split.md) 元权重的切分规则一致，只不过这里**逐专家**循环：

```python
def _load_expert(self, expert_idx, local_expert_idx, weights):
    ...
    if w1_weight in weights:
        self.quant_method.load_weight(row_slice_func(weights[w1_weight]), self.w1_list[local_expert_idx])  # gate，列并行
    if w3_weight in weights:
        self.quant_method.load_weight(row_slice_func(weights[w3_weight]), self.w3_list[local_expert_idx])  # up，列并行
    if w2_weight in weights:
        self.quant_method.load_weight(col_slice_func(weights[w2_weight]), self.w2_list[local_expert_idx])  # down，行并行
```

**(b) Triton 实现：路由 + 融合专家**

[lightllm/common/basemodel/layer_weights/meta_weights/fused_moe/impl/triton_impl.py:109-148](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/fused_moe/impl/triton_impl.py#L109-L148) —— `FuseMoeTriton.__call__` 把「路由」与「融合专家」明确分成两步，正好对应 4.2 与本节：

```python
def __call__(self, input_tensor, router_logits, w13, w2, correction_bias, scoring_func,
             top_k, renormalize, use_grouped_topk, topk_group, num_expert_group, ...):
    topk_weights, topk_ids = self._select_experts(...)   # 步骤一：路由（4.2）
    output = self._fused_experts(                         # 步骤二：融合专家（本节）
        input_tensor=input_tensor, w13=w13, w2=w2,
        topk_weights=topk_weights, topk_ids=topk_ids, ...)
    return output
```

[lightllm/common/basemodel/layer_weights/meta_weights/fused_moe/impl/triton_impl.py:80-107](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/fused_moe/impl/triton_impl.py#L80-L107) —— `_fused_experts` 解出 `w13_weight`/`w2_weight`（以及 FP8 时的 scale），调用真正的五步流水线 `fused_experts`，并默认 `inplace=True`（结果直接写回 `input_tensor`，所以 `_moe_ffn_tp` 里没有左值赋值）：

```python
def _fused_experts(self, input_tensor, w13, w2, topk_weights, topk_ids, ...):
    w13_weight, w13_scale = w13.weight, w13.weight_scale
    w2_weight, w2_scale = w2.weight, w2.weight_scale
    use_fp8_w8a8 = w13_weight.dtype == torch.float8_e4m3fn
    from lightllm.common.basemodel.triton_kernel.fused_moe.grouped_fused_moe import fused_experts
    fused_experts(hidden_states=input_tensor, w1=w13_weight, w2=w2_weight,
                  topk_weights=topk_weights, topk_ids=topk_ids, inplace=True,
                  use_fp8_w8a8=use_fp8_w8a8, w1_scale=w13_scale, w2_scale=w2_scale)
    return input_tensor
```

**(c) 五步流水线本体 `fused_experts_impl`**

[lightllm/common/basemodel/triton_kernel/fused_moe/grouped_fused_moe.py:1044-1115](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/fused_moe/grouped_fused_moe.py#L1044-L1115) —— 这是 4.3.2 流程图的真实代码，按 chunk 循环，每段依次执行五步：

```python
for chunk in range(triton.cdiv(num_tokens, CHUNK_SIZE)):
    ...
    # ① 分桶：把 chunk 内 token 按 topk_ids 归到各专家
    moe_align_fused(expert_to_token_index=expert_to_tokens, expert_to_weight=expert_to_weights,
                    expert_token_num=expert_to_token_num, topk_ids=curr_topk_ids, topk_weights=curr_topk_weights)
    # ② 第一次 grouped_matmul：x × w1(gate∥up)
    grouped_matmul(curr_topk_ids.numel(), curr_hidden_states, a1_scale,
                   expert_to_token_num, expert_to_tokens, expert_weights=w1, topk_num=topk_num,
                   out=intermediate_cache1.view(-1, N), mul_routed_weight=False, ...)
    # ③ SwiGLU 激活
    silu_and_mul_fwd(intermediate_cache1.view(-1, N), intermediate_cache2.view(-1, N // 2), ...)
    # ④ 第二次 grouped_matmul：× w2(down)，这次把路由权重乘上
    grouped_matmul(curr_topk_ids.numel(), intermediate_cache2.view(-1, N // 2), a2_scale,
                   expert_to_token_num, expert_to_tokens, expert_weights=w2, topk_num=1,
                   out=intermediate_cache3.view(-1, w2.shape[1]), mul_routed_weight=True, ...)
    # ⑤ 把 k 个专家输出求和归并回 [m, hidden]
    moe_sum_reduce(intermediate_cache3, out_hidden_states[begin_chunk_idx:end_chunk_idx])
```

[lightllm/common/basemodel/triton_kernel/fused_moe/grouped_fused_moe.py:778-807](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/fused_moe/grouped_fused_moe.py#L778-L807) —— `grouped_matmul` 的入参揭示了两步 matmul 的差异：第一次 `mul_routed_weight=False`（纯 matmul），第二次 `mul_routed_weight=True`（顺带乘路由权重）。其入参 `expert_to_token_num`/`expert_to_token_index` 正是 `moe_align_fused` 的产物：

```python
def grouped_matmul(token_num_mul_topk_num, token_inputs, token_input_scale,
                   expert_to_token_num, expert_to_token_index, expert_to_weights,
                   expert_weights, expert_to_weights_scale, topk_num, out,
                   mul_routed_weight, use_fp8_w8a8, ...):
    """
    expert_to_token_num  形状 [expert_num]          —— 每个 expert 分到多少 token
    expert_to_token_index 形状 [expert_num, token*topk] —— 每个 expert 的 token 索引列表
    expert_weights       形状 [expert_num, out_dim, hidden_dim]
    out                  形状 [token_num * topk_num, out_dim]
    """
```

**(d) 归并求和 `moe_sum_reduce`**

[lightllm/common/basemodel/triton_kernel/fused_moe/moe_sum_reduce.py:39-46](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/fused_moe/moe_sum_reduce.py#L39-L46) —— 输入形状 `[token, k, hidden]`，沿 `k`（专家）维累加，写出 `[token, hidden]`，对应公式 \(\sum_{i\in\text{topk}} g_i\cdot F_i(x)\) 的最后一步求和（\(g_i\) 已在 ④ 里乘上了）：

```python
for token_index in range(token_start, token_end):
    accumulator = tl.zeros((BLOCK_DIM,), dtype=tl.float32)
    input_t_ptr = input_ptr + token_index * input_stride_0 + offs_dim
    for i in tl.range(0, topk_num, num_stages=NUM_STAGE):       # 沿 k 个专家累加
        tmp = tl.load(input_t_ptr + i * input_stride_1, mask=offs_dim < dim_end, other=0.0)
        accumulator += tmp
    tl.store(output_ptr + token_index * output_stride_0 + offs_dim, accumulator.to(...), ...)
```

**(e) EP 版：用 DeepEP all-to-all 替代 `moe_align`**

[lightllm/common/basemodel/triton_kernel/fused_moe/grouped_fused_moe_ep.py:258-277](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/fused_moe/grouped_fused_moe_ep.py#L258-L277) —— EP 模式下，本 rank 只持有部分专家，于是「分桶」从单卡的 `moe_align_fused` 升级成跨 rank 的 all-to-all `buffer.dispatch`：把每个 token 发给它选中专家所在的 rank，对端算完再 `combine` 收回。这是 DeepSeek-V3 多卡部署的关键路径：

```python
if is_prefill:
    qinput_tensor, input_scale = per_token_group_quant_fp8(hidden_states, block_size_k, dtype=w1.dtype)
    recv_x, recv_topk_idx, recv_topk_weights, handle, _ = buffer.dispatch(
        (qinput_tensor, input_scale), topk_idx=topk_idx, topk_weights=topk_weights,
        num_experts=num_experts, num_max_tokens_per_rank=...,
        expert_alignment=128, previous_event=previous_event, ...)
```

EP 版的 GEMM 用 `masked_group_gemm`/`prefilled_group_gemm`（见 [fused_moe_weight.py:209-239](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/fused_moe/fused_moe_weight.py#L209-L239)），并且 dispatch/combine 可以与注意力计算 overlap（见 [deepseek2 推理类的 overlap_tpsp_*_forward](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py#L291-L415)），这部分属于 [u6-l2](./u6-l2-microbatch-overlap-tpsp.md) 的性能优化主题。

#### 4.3.4 代码实践

**实践目标**：在 `grouped_fused_moe.py` 里把五步流水线「对号入座」，弄清每一步读什么、写什么，从而能独立解释一次 MoE FFN 的计算走向。

**操作步骤**：

1. 打开 [grouped_fused_moe.py 的 fused_experts_impl](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/fused_moe/grouped_fused_moe.py#L992-L1115)，按 `moe_align_fused` → `grouped_matmul(..., mul_routed_weight=False)` → `silu_and_mul_fwd` → `grouped_matmul(..., mul_routed_weight=True)` → `moe_sum_reduce` 的顺序定位五处调用。
2. 对照 [grouped_matmul 的入参文档](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/fused_moe/grouped_fused_moe.py#L796-L807)，确认 `expert_to_token_num`/`expert_to_token_index`（① 的产物）如何被 ② 和 ④ 复用——它们告诉 kernel「每个专家要算哪些 token」。
3. 在 [fused_moe_weight.py 的 experts()](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/fused_moe/fused_moe_weight.py#L127-L155) 与 [triton_impl.py 的 __call__](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/fused_moe/impl/triton_impl.py#L109-L148) 之间，串起从 DeepSeek `_moe_ffn_tp` 调 `experts(...)` → `fuse_moe_impl(...)` → `_select_experts` + `_fused_experts` → `fused_experts` → `fused_experts_impl` 的完整调用链。
4. 思考：为什么 `silu_and_mul_fwd` 这一步不需要知道任何「专家」信息？

**需要观察的现象**：

- ② 与 ④ 用的是**同一个** `grouped_matmul` 函数，区别仅在 `mul_routed_weight` 与 `topk_num`（② 的 `topk_num=k`，④ 的 `topk_num=1`）。
- `expert_to_token_*` 这些「分桶」张量在 ① 产出后，被 ②④ **复用**，不会重新分桶——分桶开销被摊薄。
- `silu_and_mul_fwd` 只对 `[m*k, 2N]` 的张量做逐元素 `silu(gate)*up`，输入输出形状里**根本没有专家维度**——它对每个 token-专家对独立作用，天然与「专家」无关。

**预期结果**：你会得到一张清晰的「数据流向表」：`hidden [M,H]` →（路由）→ `topk_ids [M,k]` →（① 分桶）→ `expert_to_tokens` →（② w1）→ `[M,k,2N]` →（③ 激活）→ `[M,k,N]` →（④ w2，乘路由权重）→ `[M,k,H]` →（⑤ 求和）→ `out [M,H]`。并能解释为什么 fused_moe 比朴素循环专家快：①把稀疏分发重排成连续访问，②④用 grouped GEMM 把多专家多 token 段塞进一个 kernel，整体大幅减少 launch 与显存搬运。

> 待本地验证：第 1 步的五处调用行号建议亲手核对；第 2 步的 `expert_to_token_index` 形状 `[E, token*topk]` 是理解分桶的关键，可结合 `moe_align_fused`（[第 388 行](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/fused_moe/grouped_fused_moe.py#L388)）的实现细读。

#### 4.3.5 小练习与答案

**练习 1**：第二次 `grouped_matmul`（w2）为什么要把 `mul_routed_weight` 设为 `True`，而第一次（w1）设为 `False`？能不能反过来，或者两次都不乘、最后统一乘？

**参考答案**：因为路由权重 \(g_i\) 语义上是「第 \(i\) 个专家整体输出的权重」，而 down 投影（w2）的输出正是「第 \(i\) 个专家对某 token 的最终输出」，所以乘在 w2 之后最自然、数值上也最合理（在 down 的输出域上缩放）。第一次 w1 的输出还只是中间态（gate/up 未激活相乘），此时乘路由权重没有物理意义。理论上也可以「两次都不乘、在 `moe_sum_reduce` 时把 \(g_i\) 作为加权系数」——但那样求和就要从「等权加法」变成「加权加法」，多一次乘法；而把它合并进 ④ 的 grouped_matmul，相当于**把这次乘法免费搭车**进已有 kernel，更高效。所以「在 w2 后乘」是语义正确与计算经济的双重选择。

**练习 2**：EP 模式下，`moe_align_fused` 这一步为什么被 `buffer.dispatch` 取代了？

**参考答案**：因为 TP 模式下所有专家都在本 rank，「分桶」只是在本机内存里把 token 按专家重排（`moe_align`）。而 EP 模式下**专家被分散到不同 rank**——本 rank 只持有部分专家，token 选中的专家很可能在别的 rank 上。于是「把 token 送到对应专家那里」必须是一次**跨 rank 的通信**，即 DeepEP 的 all-to-all `dispatch`（发出去）与 `combine`（收回来），替代了单机的 `moe_align`。这也解释了为什么 EP 模式的 GEMM 用 `prefilled_group_gemm`（已经按真实接收到的 token 数排好）、而 TP 用 `grouped_matmul`（用 `expert_to_token_*` 索引）。

## 5. 综合实践

**任务**：以 DeepSeek-V2/V3 的 MoE 层为对象，画出一次 TP 模式 MoE FFN 的**完整调用链与数据流向图**，并指出它与 llama 稠密 FFN 的「分叉点」与「相同点」各在哪里。

**操作步骤**：

1. **定位分叉点**：从模板 [transformer_layer_infer_template.py:73-74](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py#L73-L74) 的 `self._ffn(...)` 出发，沿 `_bind_ffn`（[deepseek2:63-71](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py#L63-L71)）找到 `_ffn_tp_impl` → `_moe_ffn_tp`。
2. **画路由段**：`moe_gate.mm` 产出 `router_logits` → `experts()` → `fuse_moe_impl.__call__` → `_select_experts` → `select_experts` →（DeepSeek 分组）`triton_grouped_topk`，产出 `topk_weights/topk_ids`。
3. **画计算段**：`_fused_experts` → `fused_experts` → `fused_experts_impl` 的五步：`moe_align_fused` → `grouped_matmul(w1)` → `silu_and_mul_fwd` → `grouped_matmul(w2, mul_routed_weight=True)` → `moe_sum_reduce`，原地写回 `hidden_states`。
4. **画收尾段**：可选的 `shared_output` 加和（[deepseek2:237-238](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py#L237-L238)）→ `_tpsp_reduce`（[deepseek2:277](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py#L277)）。
5. **对照 llama**：把上面的链路与 [llama `_ffn_tp`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py#L118-L129) 的 `gate_up_proj → silu_and_mul_fwd → down_proj` 并排，用两种颜色标出「相同部分」与「MoE 独有部分」。

**需要观察的现象**：

- **相同点**：注意力部分（MLA 见 [u5-l5](./u5-l5-mla-attention.md)）、两段残差、`silu_and_mul_fwd` 激活——这些 DeepSeek 与 llama 完全共享。
- **MoE 独有**：路由（`moe_gate` + top-k）、按专家分组计算（`moe_align` + `grouped_matmul` ×2）、求和归并（`moe_sum_reduce`）、可选共享专家、TPSP allgather/reduce 的位置。
- `silu_and_mul_fwd` 是**唯一一个稠密 FFN 与 MoE 都原样调用**的核心算子——它是两者共享的「最小公倍数」。

**验收标准**：你能脱稿画出这条调用链，能说出五步流水线每步的输入输出形状，能解释「为什么 MoE 只替换 `_ffn` 就够了」，并能指出 Mixtral 与 DeepSeek 在路由复杂度上的差异（朴素 top-k vs 分组 + 偏置）。

## 6. 本讲小结

- **MoE 只替换 `_ffn` 钩子**：transformer 层模板把 FFN 段留成 `raise Exception("need to impl")` 的 `_ffn` 钩子；llama 用稠密 FFN（`gate_up → silu_and_mul → down`）填它，Mixtral/DeepSeek 用「路由 + 融合专家」填它。骨架（注意力、两段残差、KV 落池）对所有模型完全共享。
- **MoE 推理的结构**：`_tpsp_allgather` → `moe_gate.mm` 出路由得分 → 选 top-k 专家 → `fused_experts` 融合计算 →（可选）加共享专家 → `_tpsp_reduce`。DeepSeek 还按层号区分 dense/MoE，并按 `--enable_ep_moe` 区分 TP/EP 两种实现，全部在 `_bind_ffn` 里提前绑定。
- **专家路由有两套**：Mixtral 的朴素 softmax top-k（`fused_topk`，PyTorch 版偏教学），DeepSeek 的分组 + 偏置 grouped top-k（`triton_grouped_topk`，带 `e_score_correction_bias`、`n_group`、`topk_group`，用于负载均衡）。两者产出同形的 `(topk_weights, topk_ids)`，下游对路由算法无感。
- **`FusedMoeWeight` 是 N 个专家的统一容器**：对外暴露 `experts()` 入口，内部按 TP/EP 决定本 rank 持有哪些专家，逐专家用 `row_slicer`（gate/up，列并行）/`col_slicer`（down，行并行）切分，并把实现策略委托给 `select_fuse_moe_impl`（Triton/DeepGemm/Marlin）。
- **fused_moe 是五步融合流水线**：`moe_align_fused`（分桶）→ `grouped_matmul(w1)` → `silu_and_mul_fwd`（SwiGLU，与稠密共用）→ `grouped_matmul(w2, mul_routed_weight=True)`（乘路由权重）→ `moe_sum_reduce`（按 token 求 k 个专家之和）。它把「循环专家」融合成一组 grouped GEMM，大幅减少 launch 与显存搬运。
- **EP 模式用 DeepEP all-to-all 替代单机分桶**：`--enable_ep_moe` 时专家分散到各 rank，`buffer.dispatch`/`combine` 跨 rank 传递 token，`masked_group_gemm`/`prefilled_group_gemm` 做分组 GEMM，并可与注意力 overlap；这是 DeepSeek-V3 多卡高性能部署的关键路径。
- **准确性提醒**：Mixtral 的 `_ffn` 是讲解 MoE 概念的简明入口，但其函数体里的 `fused_experts_impl` 导入路径与 `experts.w1[0]` 取法相对当前 HEAD 已滞后；当前主力维护、能跑通的 MoE 实现是 **DeepSeek-V2/V3** 路径，源码精读结论应以它为准。

## 7. 下一步学习建议

- 若想看 DeepSeek「连注意力都换掉」的深度定制，继续 [u5-l5 MLA 注意力实现](./u5-l5-mla-attention.md)：本讲的 MoE 替换了 FFN 段，而 MLA 替换了注意力段（含 KV 压缩与权重吸收），两者叠加才是完整的 DeepSeek 模型。
- 若想了解 EP 模式下「dispatch/combine 与注意力 overlap」的细节，进入 [u6-l2 microbatch overlap 与 TPSP 混合并行](./u6-l2-microbatch-overlap-tpsp.md)，那里会拆解 DeepSeek 推理类里 `overlap_tpsp_token_forward`/`overlap_tpsp_context_forward` 的双流交错执行（本讲 4.1.3(c) 提到的 `_0_hook`/`_1_hook` 就是 overlap 的接缝）。
- 若关心 MoE 的量化（DeepSeek-V3 的 FP8 block-wise 量化专家权重），可顺读 [u6-l3 FP8 KV Cache 量化](./u6-l3-fp8-kv-quant.md) 与 `fused_experts_impl` 里 `use_fp8_w8a8`/`w1_scale`/`w2_scale` 分支，以及 EP 版的 `per_token_group_quant_fp8` + DeepGemm 分组 FP8 GEMM（[grouped_fused_moe_ep.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/fused_moe/grouped_fused_moe_ep.py)）。
- 若你想动手新增一个 MoE 模型，回到 [u5-l3 如何新增模型支持](./u5-l3-add-new-model.md) 的三件套流程：MoE 模型的工作量主要集中在 `transformer_layer_weight.py`（声明 `moe_gate` + `FusedMoeWeight`，见本讲 4.1.3(b)）与 `transformer_layer_infer.py`（写 MoE 版 `_ffn`，见本讲 4.1.3(c)），其余文件（pre/post 层、attention、注册）多数可复用 llama/deepseek2。
