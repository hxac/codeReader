# 以 Llama 为例理解完整模型实现

## 1. 本讲目标

本讲以 Llama 模型为范例，把第五单元第一讲（u5-l1 模型注册机制）和第三单元（u3-l1 基类框架、u3-l3 层模板、u3-l4 权重切分）里学过的「抽象机制」落到一个真实可跑的模型上。读完本讲，你应该能够：

- 看懂 `LlamaTpPartModel` 如何只填几个「插槽类」就把整个模型组装起来，自己几乎不写 `__init__`。
- 说出一个完整 Llama 模型由哪些文件协作构成（`model.py` / `layer_infer/` / `layer_weights/` / `infer_struct.py` / `triton_kernel/`）。
- 区分 prefill 与 decode 两阶段在层推理、注意力核、状态对象上的分叉点。
- 理解 Llama 权重如何用「元权重」描述 HF 命名，并按张量并行（TP）自动切分。
- 掌握 rotary embedding（RoPE）的多种初始化分支，知道 `rope_scaling` 是如何被分发到不同函数的。

本讲是「动手适配新模型」的前置——把一个最简单的 decoder-only 模型彻底看懂，后续的 MoE（u5-l4）、MLA（u5-l5）都只是在此基础上做局部替换。

## 2. 前置知识

本讲默认你已经掌握以下概念（前面讲义已建立）：

- **模型注册机制**（u5-l1）：`@ModelRegistry("llama")` 装饰器在 import 期把模型类登记进注册中心，框架凭 `config.json` 的 `model_type` 查表选出推理类。
- **TpPartBaseModel 框架**（u3-l1）：所有模型族的基类，采用「插槽 + 组装」设计，基类提供一条写死的初始化流水线，子类只填零件类。
- **层推理模板**（u3-l3）：`TransformerLayerInferTpl` 写死 prefill/decode 两条对称骨架，子类只需覆写 `_get_qkv`、注意力核等钩子方法。
- **权重与 TP 切分**（u3-l4）：`ROWMMWeight`（列并行，无需 all-reduce）与 `COLMMWeight`（行并行，需 all-reduce）是两种基本元权重，`hf_load_utils` 按 `.safetensors` 权重名分发。

如果你对上面任意一项还陌生，建议先回看对应讲义。本讲不再重复它们的原理，只关注「Llama 具体是怎么填这些槽位的」。

下面几个术语本讲会反复用到，先约定：

- **GQA（Grouped-Query Attention）**：query 头数多于 key/value 头数，多个 q 头共享同一组 k/v，以节省 KV Cache。Llama 默认 `num_key_value_heads == num_attention_heads`（即 MHA），但代码为 GQA 预留了支持。
- **RoPE（Rotary Position Embedding）**：旋转位置编码，把位置信息以旋转矩阵的形式乘进 q/k，使内积自然带相对位置。
- **RMSNorm**：LayerNorm 的一种轻量变体，只做缩放不做平移，Llama 全程用它。

## 3. 本讲源码地图

Llama 模型完全遵循「一模型一目录」的组织约定（见 u1-l3）。本讲涉及的文件如下：

| 文件 | 作用 |
| --- | --- |
| `lightllm/models/llama/model.py` | 模型主类 `LlamaTpPartModel`，填插槽 + 配置校验 + RoPE 初始化 |
| `lightllm/models/llama/infer_struct.py` | 推理状态对象 `LlamaInferStateInfo`，多挂 `position_cos/sin` 两个张量 |
| `lightllm/models/llama/layer_infer/transformer_layer_infer.py` | transformer 层推理，覆写 7 个钩子（qkv/o/ffn/两个 norm/两个注意力核） |
| `lightllm/models/llama/layer_infer/pre_layer_infer.py` | embedding 层推理（`wte_weight_` 查表 + TP all-reduce） |
| `lightllm/models/llama/layer_infer/post_layer_infer.py` | post 层推理（final_norm + lm_head 出 logits + TP all-gather） |
| `lightllm/models/llama/layer_weights/transformer_layer_weight.py` | transformer 层权重，声明 q/kv/o/gate_up/down 与两个 RMSNorm |
| `lightllm/models/llama/layer_weights/pre_and_post_layer_weight.py` | embedding、lm_head、final_norm 三个权重 |
| `lightllm/models/llama/triton_kernel/rotary_emb.py` | RoPE 的 Triton kernel |
| `lightllm/common/basemodel/basemodel.py` | 基类 `TpPartBaseModel`，提供六个插槽与初始化流水线（仅引用，不修改） |
| `lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py` | transformer 层推理模板骨架（仅引用） |

可以看到，Llama 目录把「推理（layer_infer）」「权重（layer_weights）」「算子（triton_kernel）」分得很清楚，这是 LightLLM 所有模型的通用范式。

## 4. 核心概念与源码讲解

### 4.1 Llama 模型：从注册到组件组装

#### 4.1.1 概念说明

u3-l1 讲过，`TpPartBaseModel` 采用「插槽 + 组装」的依赖注入设计：基类声明六个默认为 `None` 的类属性插槽（权重类 ×2、推理类 ×3、推理状态类 ×1），子类只需填入零件类、几乎不写 `__init__`。Llama 就是最典型的「填槽位」实现——它的 `LlamaTpPartModel` 本体几乎是空的，所有个性都靠「填哪几个类」和「覆写几个钩子」来表达。

Llama 还承担两个额外职责：配置校验（确保 GQA 头数能被 TP 整除）和 RoPE 初始化（这是它覆写 `_init_custom` 钩子的主要原因）。

#### 4.1.2 核心流程

Llama 模型类的工作可以分成三件事：

1. **注册与填槽**：装饰器登记，类体里把六个插槽指向 Llama 自己的零件类。
2. **配置处理**：`_init_config` 兜底 `num_key_value_heads`；`_verify_params` 做整除断言。
3. **自定义初始化**：覆写 `_init_custom`，根据 `rope_scaling` 分发到对应 RoPE 函数。

基类的初始化流水线（见 u3-l1）会在恰当的时机调用上面这些方法，Llama 只需要在被调到时做正确的事。

#### 4.1.3 源码精读

先看整类的注册与插槽，这是 Llama 模型最核心的「身份证」：

[lightllm/models/llama/model.py:21-37](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/model.py#L21-L37) —— `@ModelRegistry("llama")` 登记模型，类体六个插槽分别指向 Llama 自己的权重类、推理类、推理状态类；`__init__` 直接调父类，自己什么都不做。

把这六个插槽和基类声明对照看就一目了然。基类的插槽定义在：

[lightllm/common/basemodel/basemodel.py:47-58](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L47-L58) —— `TpPartBaseModel` 声明六个类属性插槽，全部默认 `None`，等待子类填入。

Llama 的六个零件分别来自这些 import（同文件第 5–11 行）：

| 插槽 | Llama 填的类 | 来源文件 |
| --- | --- | --- |
| `pre_and_post_weight_class` | `LlamaPreAndPostLayerWeight` | `layer_weights/pre_and_post_layer_weight.py` |
| `transformer_weight_class` | `LlamaTransformerLayerWeight` | `layer_weights/transformer_layer_weight.py` |
| `pre_layer_infer_class` | `LlamaPreLayerInfer` | `layer_infer/pre_layer_infer.py` |
| `post_layer_infer_class` | `LlamaPostLayerInfer` | `layer_infer/post_layer_infer.py` |
| `transformer_layer_infer_class` | `LlamaTransformerLayerInfer` | `layer_infer/transformer_layer_infer.py` |
| `infer_state_class` | `LlamaInferStateInfo` | `infer_struct.py` |

**这张表就是本讲的「索引页」**——后面四节分别打开这些零件。Llama 适配模型的工作量，本质上就是「把这张表填出来」。

接下来看配置处理。Llama 覆写了两个钩子：

[lightllm/models/llama/model.py:39-49](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/model.py#L39-L49) —— `_init_config` 先调父类（读 config.json、统一字段名），再 `_reset_num_key_value_heads` 兜底：老版 Llama 的 config 里没有 `num_key_value_heads`，此时令它等于 `num_attention_heads`（即标准 MHA）。

[lightllm/models/llama/model.py:51-55](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/model.py#L51-L55) —— `_verify_params` 做两条断言：加载格式只能是 HF 或 DS；`num_key_value_heads` 与 `num_attention_heads` 都必须能被 `tp_world_size_` 整除（否则 TP 切分会切不均，详见 u3-l4）。

最后是内存管理器。Llama 覆写了 `_init_mem_manager`，关键在于 KV 头数要按 TP 切：

[lightllm/models/llama/model.py:57-68](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/model.py#L57-L68) —— 用 `select_mem_manager_class()` 选内存管理器类，`head_num` 传的是 `num_key_value_heads // tp_world_size_`（每张 GPU 只存自己那一片 KV），`head_dim` 优先取 config 的 `head_dim`，层号还要加上 `get_added_mtp_kv_layer_num()`（为 MTP draft 模型预留 KV 层，普通推理为 0）。`select_mem_manager_class` 的选型逻辑见 u4-l1。

#### 4.1.4 代码实践

**实践目标**：验证「填槽位即组装模型」这句话，并建立文件索引。

**操作步骤**：

1. 打开 `lightllm/models/llama/model.py` 第 21–37 行，确认六个插槽分别填了哪个类。
2. 对照上面那张「插槽→类→文件」表，逐个 `import` 语句（第 5–11 行）确认每个零件类的来源文件真实存在。
3. 在每个零件类的类定义行旁边，用注释写上它对应哪个插槽（例如在 `LlamaTransformerLayerInfer` 类定义旁写 `# 对应 transformer_layer_infer_class`）。

**需要观察的现象**：你会发现 Llama 目录下的文件名与插槽语义一一对应，没有任何「多余」的类，也没有任何插槽悬空（填 `None`）。

**预期结果**：六个插槽全部被填满，文件组织完全契合「model.py + layer_infer/ + layer_weights/ + infer_struct.py」范式。

#### 4.1.5 小练习与答案

**练习 1**：如果要把 Llama 改成支持一个新的注意力变体（假设叫 `LlamaMyAttentionLayerInfer`），需要改 `model.py` 的哪一行？

**答案**：改 `transformer_layer_infer_class = LlamaTransformerLayerInfer` 这一行，把它指向新类即可。这就是插槽设计的价值——一行改动完成整层推理算法的替换。

**练习 2**：为什么 `_init_mem_manager` 里 `head_num` 用 `num_key_value_heads // tp_world_size_` 而不是 `num_attention_heads // tp_world_size_`？

**答案**：因为 KV Cache 只存 key/value 的头，GQA 模型下 k/v 头数少于 q 头数。每张 GPU 只需要保存自己那一片 k/v 头，所以用 KV 头数除以 TP。

### 4.2 层推理：transformer / pre / post 三类层的实现

#### 4.2.1 概念说明

u3-l3 讲过，整条前向由三类推理层串成：embedding 层（pre）、N 层 transformer、post 层（final_norm + lm_head 出 logits）。三类层共用模板方法模式：根基类立接口、`*Tpl` 模板类写死骨架留钩子、具体模型填钩子。

Llama 的三层推理类都是「薄壳」——它们继承模板，只覆写少数几个钩子。原因有二：一是 Llama 结构最简单（标准 RMSNorm + GQA + SwiGLU），没有特殊变形；二是模板骨架已经把 prefill/decode 的差异封装好了，子类只需在注意力核处分叉。

#### 4.2.2 核心流程

transformer 层推理模板的骨架（来自 u3-l3）是这样的对称结构：

```
context_forward(input):      # prefill 用
  x = _att_norm(input)
  o = context_attention_forward(x)   # _get_qkv → _post_cache_kv → _context_attention_kernel → _get_o
  input += o                         # 第一段残差
  x = _ffn_norm(input)
  ffn_out = _ffn(x)                  # gate_up → silu_and_mul → down
  input += ffn_out                   # 第二段残差

token_forward(input):        # decode 用，结构完全对称
  x = _att_norm(input)
  o = token_attention_forward(x)     # _get_qkv → _post_cache_kv → _token_attention_kernel → _get_o
  input += o
  x = _ffn_norm(input)
  ffn_out = _ffn(x)
  input += ffn_out
```

prefill 与 decode 的唯一差别在注意力核（`_context_attention_kernel` 处理一整段 token，`_token_attention_kernel` 让 1 个新 token 复用全历史 KV）以及 K/V 的来源（都从 `mem_manager` 读，详见 u3-l3 的「写后读」闭环）。

#### 4.2.3 源码精读

先看模板骨架本身，确认钩子调用点（这是 u3-l3 的内容，这里只引用关键几行）：

[lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py:56-99](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py#L56-L99) —— `context_forward` 与 `token_forward` 两条对称骨架，统一以「norm → attention → 残差 → norm → ffn → 残差」推进，钩子方法（`_att_norm`/`_get_qkv`/`_context_attention_kernel`/`_token_attention_kernel`/`_get_o`/`_ffn`）留给子类填。

现在看 Llama 怎么填这些钩子。首先是构造与 norm 绑定：

[lightllm/models/llama/layer_infer/transformer_layer_infer.py:16-38](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py#L16-L38) —— 构造函数算出 TP 后的各头数（`tp_q_head_num_`、`tp_k_head_num_` 等）与 `head_dim_`、`eps_`，再用 `partial` 把两个 norm 方法绑定到 `self`（这是 LightLLM 统一的 norm 复用手段）。

接着是两个注意力核——prefill 与 decode 的分叉点：

[lightllm/models/llama/layer_infer/transformer_layer_infer.py:40-67](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py#L40-L67) —— `_context_attention_kernel` 用 `prefill_att_state.prefill_att`、`_token_attention_kernel` 用 `decode_att_state.decode_att`，两者的 K/V 都从 `mem_manager.get_att_input_params(layer_index=...)` 读回（即 u3-l3 的「写后读」闭环），只是 prefill 处理一整段、decode 处理单个新 token。具体的后端选择（fa3/flashinfer/triton）见 u3-l5。

再看 QKV 投影，这里是 RoPE 的注入点：

[lightllm/models/llama/layer_infer/transformer_layer_infer.py:79-97](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py#L79-L97) —— `_get_qkv`：先 `_tpsp_allgather`（TPSP 混合并行用，见 u6-l2），再 `q_proj.mm` 与 `kv_proj.mm` 得到 q 和 cache_kv，随后对 q 和 k 调 `rotary_emb_fwd`（只对 k 的一段施加 RoPE，v 不旋转）。`need_dp_prefill_balance` 分支是 DP 负载均衡（u7-l3），普通推理不触发。

输出投影与 FFN：

[lightllm/models/llama/layer_infer/transformer_layer_infer.py:99-109](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py#L99-L109) —— `_get_o`：`o_proj.mm` 后调 `_tpsp_reduce`（行并行矩阵乘后必须 all-reduce，见 u3-l4）。

[lightllm/models/llama/layer_infer/transformer_layer_infer.py:111-129](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py#L111-L129) —— `_ffn` / `_ffn_tp`：SwiGLU 的实现——`gate_up_proj.mm` 一次算出 gate 与 up，再 `silu_and_mul_fwd` 做 `silu(gate) * up`，最后 `down_proj.mm`。`gate_up_proj` 列并行、`down_proj` 行并行，与 u3-l4 完全一致。

Llama 还顺手实现了 `overlap_tpsp_token_forward` / `overlap_tpsp_context_forward`（第 143–165 行），它们只是把两个 infer_state 各跑一次 `token_forward`/`context_forward`，用于 microbatch overlap 优化（见 u6-l2）。

最后看 pre 层和 post 层这两个薄壳：

[lightllm/models/llama/layer_infer/pre_layer_infer.py:17-28](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/pre_layer_infer.py#L17-L28) —— pre 层：`wte_weight_(input_ids)` 查 embedding 表得到向量；TP>1 时做 `all_reduce(SUM)`（embedding 沿词表维切，多 rank 拼回需求和）。

[lightllm/models/llama/layer_infer/post_layer_infer.py:61-90](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/post_layer_infer.py#L61-L90) —— post 层 `_token_forward`：先 `_slice_get_last_input` 取出每个请求最后/全部 token，`_norm` 后 `lm_head_weight_` 出 logits；TP>1 时用 `all_gather` 把各 rank 沿词表维切出的 logits 拼回完整词表。

`_slice_get_last_input` 是 post 层里稍复杂的一段，它按 prefill/decode/token_healing/return_all_prompt_logics 四种情况分别切：

[lightllm/models/llama/layer_infer/post_layer_infer.py:24-59](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/post_layer_infer.py#L24-L59) —— 四分支切片：prefill 且非 `return_all_prompt_logics` 时只取每个请求最后一个 token（即「下一个 token」的 logits）；token healing 模式单独处理；`return_all_prompt_logics` 时返回全部 token（用于 prompt logprobs）；decode 时取最后 `batch_size` 行。

#### 4.2.4 代码实践

**实践目标**：跟踪一次 prefill 中 Llama transformer 层的钩子调用顺序。

**操作步骤**：

1. 从模板骨架 `context_forward`（`transformer_layer_infer_template.py:67`）出发，记下它依次调用哪些 `_xxx` 钩子。
2. 对每个钩子，跳到 `transformer_layer_infer.py` 里找 Llama 的具体实现，记录它实际调了哪个权重（`layer_weight.xxx.mm`）或哪个 kernel。
3. 画出这样一张链路图：

```
context_forward
 ├─ _att_norm   → att_norm_weight_(RMSNorm)
 ├─ _get_qkv    → q_proj.mm + kv_proj.mm + rotary_emb_fwd
 ├─ _post_cache_kv → mem_manager.copy_kv_to_mem_manager
 ├─ _context_attention_kernel → prefill_att_state.prefill_att
 ├─ _get_o      → o_proj.mm + _tpsp_reduce
 ├─ _ffn_norm   → ffn_norm_weight_(RMSNorm)
 └─ _ffn        → gate_up_proj.mm + silu_and_mul_fwd + down_proj.mm
```

**需要观察的现象**：模板调用的钩子名与 Llama 覆写的方法名完全一一对应，没有多余方法，也没有遗漏钩子。

**预期结果**：你能不查资料地口述「一次 prefill transformer 层经过哪 7 个钩子、各自用了哪个权重」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_get_o` 之后要 `_tpsp_reduce`，而 `_get_qkv` 里的 `q_proj.mm` 之后不需要？

**答案**：`q_proj`/`kv_proj`/`gate_up_proj` 是列并行（`ROWMMWeight`），每个 rank 算出的是输出的不同列，结果天然不重叠，无需通信；`o_proj`/`down_proj` 是行并行（`COLMMWeight`），每个 rank 算出的是部分和，必须 all-reduce 求和才正确（见 u3-l4）。

**练习 2**：post 层的 `all_gather` 和 pre 层的 `all_reduce`，分别针对哪个被 TP 切分的维度？

**答案**：pre 层 embedding 沿词表维切，各 rank 查表得到部分向量，需 `all_reduce(SUM)` 求和；post 层 lm_head 也沿词表维切，各 rank 输出的是 logits 的不同词表段，需 `all_gather` 拼回完整词表。

### 4.3 权重：从 HF 命名到 TP 切分的元权重

#### 4.3.1 概念说明

u3-l4 讲过，权重是「两层结构」：外层 `TransformerLayerWeight`/`PreAndPostLayerWeight` 只是容器，靠反射自动发现并加载元权重；内层元权重（`ROWMMWeight`/`COLMMWeight`/`EmbeddingWeight` 等）才是真正存储、切分、校验的最小单元。

Llama 权重的工作量集中在两件事：① 给每个元权重起对 HF 权重名（让 `hf_load_utils` 能从 `.safetensors` 找到对应张量）；② 为每个线性层选对元权重类型（列并行还是行并行）。Llama 的实现非常规整，可作为新模型的模板。

#### 4.3.2 核心流程

transformer 层权重的初始化分三步：

1. `_parse_config`：从 config 读出头数、维度、`intermediate_size`。
2. `_init_weight_names`：按 HF 命名规则（`model.layers.{i}.self_attn.q_proj.weight` 等）拼出每个权重的名字。
3. `_init_weight` → `_init_qkv` / `_init_o` / `_init_ffn` / `_init_norm`：为每个权重选元权重类型并实例化。

元权重的切分规则（u3-l4 已讲）在 Llama 这里具体落地为：q/gate/up 用 `ROWMMWeight`（列并行），o/down 用 `COLMMWeight`（行并行），kv 用特殊的 `KVROWNMMWeight`。

#### 4.3.3 源码精读

先看 transformer 层权重的整体结构：

[lightllm/models/llama/layer_weights/transformer_layer_weight.py:19-24](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_weights/transformer_layer_weight.py#L19-L24) —— `_init_weight` 调用四个子初始化，分别建 qkv、o、ffn、norm 的元权重，结构清晰。

HF 命名拼接是关键，它决定了权重能不能被正确加载：

[lightllm/models/llama/layer_weights/transformer_layer_weight.py:36-60](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_weights/transformer_layer_weight.py#L36-L60) —— `_init_weight_names` 按 `model.layers.{layer_num_}.self_attn.q_proj.weight` 等 HF 命名拼出 q/k/v/kv/o、gate/up/gate_up/down、两个 layernorm 的名字。注意 q/k/v 的 bias 全部设为 `None`（Llama 的注意力投影无 bias）。

接着是真正选元权重类型的几个方法：

[lightllm/models/llama/layer_weights/transformer_layer_weight.py:62-81](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_weights/transformer_layer_weight.py#L62-L81) —— `_init_qkv`：`q_proj` 用 `ROWMMWeight`（列并行），`kv_proj` 用 `KVROWNMMWeight`——这是把 HF 里分开的 `k_proj` 和 `v_proj` 两个权重融合加载成一个权重对象（`weight_names=[k_name, v_name]`），既支持 GQA 的 KV 头数较少的情况，也让推理时一次 `mm` 同时算出 k 和 v。

[lightllm/models/llama/layer_weights/transformer_layer_weight.py:83-93](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_weights/transformer_layer_weight.py#L83-L93) —— `_init_o`：`o_proj` 用 `COLMMWeight`（行并行，需 all-reduce）。

[lightllm/models/llama/layer_weights/transformer_layer_weight.py:95-111](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_weights/transformer_layer_weight.py#L95-L111) —— `_init_ffn`：`gate_up_proj` 用 `ROWMMWeight`（`out_dims=[n_inter, n_inter]`，把 gate 和 up 融合加载）；`down_proj` 用 `COLMMWeight`。这种 gate/up 融合让 SwiGLU 一次 `mm` 出结果，与 4.2.3 节的 `silu_and_mul_fwd` 配对。

[lightllm/models/llama/layer_weights/transformer_layer_weight.py:113-124](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_weights/transformer_layer_weight.py#L113-L124) —— `_init_norm`：两个 norm 都用 `RMSNormWeight`（Llama 用 RMSNorm 而非 LayerNorm）。

`KVROWNMMWeight` 与 `ROWMMWeight` 都继承自 `MMWeightTpl`，定义在：

[lightllm/common/basemodel/layer_weights/meta_weights/mm_weight/rowmm_weight.py:11-41](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/mm_weight/rowmm_weight.py#L11-L41) —— `ROWMMWeight`（普通列并行）与 `KVROWNMMWeight`（专用于融合 k/v，内部处理 GQA 下 TP 多于 KV 头时的复制）。

再看 pre/post 层权重，它更短：

[lightllm/models/llama/layer_weights/pre_and_post_layer_weight.py:5-30](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_weights/pre_and_post_layer_weight.py#L5-L30) —— 建三个权重：`wte_weight_`（embedding，`EmbeddingWeight`，名为 `model.embed_tokens.weight`）、`lm_head_weight_`（`LMHeadWeight`，名为 `lm_head.weight`，若 `tie_word_embeddings=True` 则共享 embedding 权重）、`final_norm_weight_`（`RMSNormWeight`，名为 `model.norm.weight`）。

`tie_word_embeddings` 是权重 tying（权重共享）开关：有些模型（如 Qwen 部分版本）的 lm_head 不单独存权重，而是复用 embedding 矩阵，这里通过 `embedding_weight=self.wte_weight_` 把两者绑定。

#### 4.3.4 代码实践

**实践目标**：把 Llama 的每个权重名对应回 HuggingFace 的 `.safetensors`。

**操作步骤**：

1. 打开任意一份 Llama 的 HF 权重目录（或在线查看 `huggingface.co/<某 llama 模型>/tree/main` 的 `model.safetensors.index.json`）。
2. 对照 `_init_weight_names`（第 36–60 行）和 pre/post 权重（第 11–29 行）里拼出的名字，逐个在 index.json 中确认存在。
3. 列一张表，记录每个权重名 + 它在 LightLLM 里的元权重类型 + 切分方式（列并行/行并行/不切）。

**需要观察的现象**：HF 里的 `q_proj.weight`、`k_proj.weight`、`v_proj.weight` 是三个独立张量，但 LightLLM 把 k、v 融合成一个 `kv_proj`。

**预期结果**：你得到一张「HF 张量名 → LightLLM 元权重 → TP 切分方式」的完整对照表。

**待本地验证**：如果你本地没有 Llama 权重，可在线查看任一 Llama 模型的 `model.safetensors.index.json` 来核对权重名。

#### 4.3.5 小练习与答案

**练习 1**：HF 里 Llama 的 FFN 有 `gate_proj`、`up_proj`、`down_proj` 三个权重，LightLLM 是怎么把它们组织成两个元权重的？

**答案**：`gate_proj` 与 `up_proj` 融合成一个 `gate_up_proj`（`ROWMMWeight`，`weight_names=[gate_name, up_name]`，`out_dims=[n_inter, n_inter]`），`down_proj` 单独成 `down_proj`（`COLMMWeight`）。融合后一次 `mm` 同时算出 gate 和 up，再用 `silu_and_mul_fwd` 合并。

**练习 2**：`tie_word_embeddings=True` 时，`lm_head_weight_` 和 `wte_weight_` 是什么关系？

**答案**：`LMHeadWeight` 构造时传入 `embedding_weight=self.wte_weight_`，两者共享同一块存储——计算 logits 时直接用 embedding 矩阵做投影，不额外加载 lm_head 权重。这能省一份 `vocab_size × hidden_size` 的显存。

### 4.4 RoPE：rotary embedding 的多种初始化分支

#### 4.4.1 概念说明

RoPE（旋转位置编码）是 Llama 用的位置编码方案。它的直觉是：把 q（或 k）向量的相邻两个维度看成一对，按位置 m 施加一个旋转角度 \(\theta_m\)，使两个 token 的内积只依赖它们的相对位置。

对每一对维度 \((q_{2i}, q_{2i+1})\)，旋转公式为：

\[
\begin{aligned}
\text{out}_{2i}   &= q_{2i}\cos\theta_{m,i} - q_{2i+1}\sin\theta_{m,i} \\
\text{out}_{2i+1} &= q_{2i}\sin\theta_{m,i} + q_{2i+1}\cos\theta_{m,i}
\end{aligned}
\]

其中不同维度对应不同频率，频率由 base（`rope_theta`，Llama 默认 10000）决定：

\[
\theta_{m,i} = m \cdot \text{base}^{-2i/d}
\]

不同模型族为了支持更长上下文，对 RoPE 做了多种「缩放」变体（YaRN、NTK、Llama3、Su 等）。LightLLM 在 `_init_custom` 里用一个分发函数，根据 `config["rope_scaling"]` 的 `rope_type` 字段选用不同的初始化方法，预先把整张 `cos`/`sin` 表算好缓存到 GPU 上。

#### 4.4.2 核心流程

RoPE 初始化的分发逻辑（这是 Llama 覆写 `_init_custom` 的唯一目的）：

```
_init_custom():
  读 config["rope_scaling"]
  ├─ 为 None / type="default" / 含 mrope_section / type="mrope" → _init_to_get_rotary（默认 RoPE）
  ├─ type="yarn"     → _init_to_get_yarn_rotary
  ├─ type="dynamic"  → _init_to_get_dynamic_ntk_rotary
  ├─ type="su"       → _init_to_su_rotary
  ├─ type="llama3"   → _init_to_get_llama3_rotary
  └─ 其它             → 报错
```

所有分支的产物都是两个 GPU 张量：`self._cos_cached` 与 `self._sin_cached`，形状约为 `(max_seq_len, head_dim//2)`。推理时由 `LlamaInferStateInfo.init_some_extra_state` 按当前 `position_ids` 索引出本批次要用的 `position_cos`/`position_sin`。

#### 4.4.3 源码精读

先看分发函数，这是本节的核心：

[lightllm/models/llama/model.py:70-99](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/model.py#L70-L99) —— `_init_custom` 读 `rope_scaling`，先兼容新旧两种字段名（`rope_type` 或 `type`），再按取值分发到 5 个不同的初始化方法。注意它是在基类初始化流水线的 `_init_some_value` 之后被调用的（见下条引用），所以 `self.head_dim_` 此时已就绪。

基类流水线调用 `_init_custom` 的位置：

[lightllm/common/basemodel/basemodel.py:116-123](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L116-L123) —— `_init_custom()` 夹在 `_init_some_value`（设 `head_dim_`）与 `_load_hf_weights`（加载真实权重）之间；`_init_att_backend` 在其后。基类默认 `_init_custom` 是空实现（`basemodel.py:339-340`），Llama 覆写它来做 RoPE。

`head_dim_` 在 `_init_some_value` 里被设定（处理 `head_dim != hidden/heads` 的特例）：

[lightllm/common/basemodel/basemodel.py:244-248](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L244-L248) —— `_init_some_value` 算 `head_dim_`（默认 `n_embed // num_attention_heads`，优先取 config 的 `head_dim`），同时设 `tp_k_head_num_`。Llama 的 RoPE 函数都依赖 `self.head_dim_`。

默认 RoPE 初始化（最常用的分支）：

[lightllm/models/llama/model.py:101-140](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/model.py#L101-L140) —— `_init_to_get_rotary`：按公式算 `inv_freq = 1/base^(2i/d)`，再与位置序列 `t` 做外积得 `freqs`，最后 `cos`/`sin` 缓存为 GPU 张量。其中还支持 `partial_rotary_factor`（部分维度旋转）、NTK 外推（环境变量 `LIGHTLLM_NTK_ALPHA`）。

其余四个变体（`_init_to_get_yarn_rotary` 第 181–218 行、`_init_to_get_dynamic_ntk_rotary` 第 142–179 行、`_init_to_su_rotary` 第 220–266 行、`_init_to_get_llama3_rotary` 第 268–303 行）思路相近：都是修改 `inv_freq` 的算法以支持更长上下文，最终同样产出一个 `(max_seq_len, head_dim//2)` 的 `cos`/`sin` 表。YaRN 额外引入了 `mscale`（幅度缩放），Llama3 用波长阈值在高频/低频之间做平滑插值，Su 用短/长两组因子分段。

推理时如何用这张表：

[lightllm/models/llama/infer_struct.py:7-24](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/infer_struct.py#L7-L24) —— `LlamaInferStateInfo` 在基类基础上多挂 `position_cos`/`position_sin`；`init_some_extra_state` 调父类（算 `position_ids` 等）后，用 `torch.index_select` 从 `model._cos_cached`/`_sin_cached` 按本批次位置取出要用的 cos/sin。prefill 与 decode 取的形状略不同（prefill 按 `position_ids` 数量，decode 按 `b_seq_len` 数量）。

最后看 RoPE 的 Triton kernel，它实现了 4.4.1 节的旋转公式：

[lightllm/models/llama/triton_kernel/rotary_emb.py:67-75](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/triton_kernel/rotary_emb.py#L67-L75) —— kernel 核心两行 `out0 = q0*cos - q1*sin`、`out1 = q0*sin + q1*cos` 正是旋转公式；同样的逻辑也对 K 做一遍（`HAS_K` 分支）。kernel 是原地写回（`tl.store(Q + ...)`），所以 4.2.3 节 `rotary_emb_fwd` 调用后 q、k 直接被改写。

[lightllm/models/llama/triton_kernel/rotary_emb.py:121-164](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/triton_kernel/rotary_emb.py#L121-L164) —— `rotary_emb_fwd` 入口：按 head 数与序列长度切 grid，处理 `partial_rotary_factor`（只旋转前一部分维度），`HAS_K` 控制 K 是否一起旋转（v 不旋转，故无 v 参数）。

#### 4.4.4 代码实践

**实践目标**：跟踪 RoPE 从 config 选择分支到 kernel 执行的完整链路。

**操作步骤**：

1. 准备（或假设）两份不同的 Llama 系列模型 `config.json`：一份没有 `rope_scaling`（如 Llama-2），一份有 `rope_scaling: {"rope_type": "llama3", "factor": 8.0, ...}`（如 Llama-3.1-8K/128K）。
2. 对这两份 config，手工走一遍 `_init_custom` 的分发逻辑，判断各自会进入哪个 `_init_*` 分支。
3. 在 `_init_to_get_rotary`（默认分支）里，对照公式 \(\theta_{m,i}=m\cdot\text{base}^{-2i/d}\)，找到第 129–136 行 `inv_freq` 与 `freqs` 的计算，确认它与公式一致。
4. 追踪产物 `_cos_cached` 如何被 `LlamaInferStateInfo.init_some_extra_state` 索引、最终传入 `rotary_emb_fwd`。

**需要观察的现象**：没有 `rope_scaling` 的 config 走默认 `_init_to_get_rotary`；带 `rope_type=llama3` 的 config 走 `_init_to_get_llama3_rotary`，后者会按波长阈值改写 `inv_freq`。

**预期结果**：你能针对任意一份 Llama 系 `config.json`，准确说出它走哪个 RoPE 分支、产物表的形状。

**待本地验证**：实际选择哪个分支取决于模型的 `config.json`，建议用真实模型 config 核对。

#### 4.4.5 小练习与答案

**练习 1**：为什么 RoPE 的 cos/sin 表要在模型初始化时一次性算好缓存，而不是每个请求现算？

**答案**：因为 cos/sin 只依赖位置 m 和维度 i，与具体输入无关；预先算好整张表（覆盖到 `max_seq_len`），推理时只需 `index_select` 按位置取出，避免每次前向重复计算三角函数，省时省显存。

**练习 2**：`rotary_emb_fwd` 只对 q 和 k 施加旋转，不对 v 旋转，为什么？

**答案**：RoPE 的设计目标是让 `q·k` 的内积只依赖相对位置。把旋转同时施加到 q 和 k，在内积时旋转角相减得到相对位置；而 v 不参与位置编码（它是被 attention 权重加权的「值」），所以无需旋转。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「迷你适配」任务（纯源码阅读，不改代码）：

**任务**：假设你要为「结构上和 Llama 完全一样、只是用了 YaRN 长上下文缩放」的新模型写适配。请基于本讲对 Llama 的理解，列出：

1. **目录与文件清单**：新模型目录下应该有哪些文件？（提示：照搬 Llama 的范式，可大量复用基类与模板。）
2. **插槽映射表**：新模型的 `model.py` 里六个插槽分别填什么类？哪些可以直接复用 Llama 的实现，哪些需要新建？
3. **RoPE 分支**：新模型的 config 里 `rope_scaling` 大概长什么样？`_init_custom` 会分发到哪个函数？你需要为 RoPE 写新代码吗？
4. **权重对照**：新模型的 transformer 层权重，q/kv/o/gate_up/down 各用哪种元权重？与 Llama 是否相同？
5. **校验断言**：`_verify_params` 和 `_reset_num_key_value_heads` 能否直接照搬？

**参考答案要点**：

1. 文件清单：`model.py`、`infer_struct.py`、`layer_infer/{pre,post,transformer}_layer_infer.py`、`layer_weights/{pre_and_post,transformer}_layer_weight.py`、`triton_kernel/rotary_emb.py`。如果结构与 Llama 一致，多数文件可直接 import Llama 的类来复用。
2. 插槽：若仅 RoPE 不同，`layer_infer` 与 `layer_weights` 类可全部复用 Llama 的，只需新建一个 `Model` 子类覆写 `_init_custom`（或干脆复用 Llama 的 `_init_custom`，因为 YaRN 已被支持），`infer_state_class` 也可复用 `LlamaInferStateInfo`。
3. RoPE：`rope_scaling` 形如 `{"type": "yarn", "factor": 4.0, "original_max_position_embeddings": 2048}`，会分发到 `_init_to_get_yarn_rotary`。因为 Llama 已实现 YaRN，无需写新代码。
4. 权重：与 Llama 完全相同（q/gate_up 列并行，o/down 行并行，kv 用 `KVROWNMMWeight`）。
5. 校验：可完全照搬——结构相同意味着头数整除、GQA 兜底逻辑都适用。

这个练习的结论很关键：**在 LightLLM 里，一个「像 Llama」的新模型，适配成本几乎只取决于它与 Llama 在结构上的差异量；差异越小，需要写的代码越少。** 这正是插槽设计 + 模板方法模式的回报。

## 6. 本讲小结

- Llama 模型的本体几乎是空的——`LlamaTpPartModel` 只做三件事：用 `@ModelRegistry` 注册、填六个插槽类、覆写 `_init_custom` 做 RoPE。其余全靠基类流水线驱动。
- 一个完整 Llama 模型由 7 类文件协作构成：`model.py`（组装）、`infer_struct.py`（状态）、三层 `layer_infer`（推理）、两份 `layer_weights`（权重）、`triton_kernel/rotary_emb.py`（算子）。
- transformer 层推理是「薄壳」：模板写好 prefill/decode 对称骨架，Llama 只覆写 7 个钩子（两个 norm、`_get_qkv`、两个注意力核、`_get_o`、`_ffn`），唯一分叉在注意力核。
- 权重的核心是「HF 命名 + 元权重类型」：q/gate_up 用列并行 `ROWMMWeight`，o/down 用行并行 `COLMMWeight`，k/v 融合成 `KVROWNMMWeight`，gate/up 进一步融合成一个 `gate_up_proj`。
- RoPE 用 `_init_custom` 里的分发函数按 `rope_scaling.rope_type` 选择 5 种初始化方法之一，预先算好 `cos/sin` 表缓存到 GPU，推理时由 `LlamaInferStateInfo` 按位置索引、最终在 Triton kernel 里施加旋转。
- pre 层与 post 层分别负责 embedding 查表（TP all-reduce）和 lm_head 出 logits（TP all-gather），是 TP 通信在模型首尾的两个汇聚点。

## 7. 下一步学习建议

- **u5-l3 如何新增模型支持**：本讲是它的前置。下一讲会基于官方 `add_new_model.md` 指南，把本讲的「插槽 + 模板」思路落地成一套完整的新模型适配流程，建议对照本讲的 Llama 实现来读。
- **u5-l4 MoE 模型推理**：想看「结构与 Llama 不同时」适配工作量的变化，可读 Mixtral/Deepseek 的 MoE 层如何替换本讲的普通 FFN。
- **u5-l5 MLA 注意力实现**：想看「注意力核不同时」的变化，可读 Deepseek 的 MLA 如何替换本讲的 `_context/_token_attention_kernel`。
- **u6-l2 microbatch overlap**：本讲多次提到的 `overlap_tpsp_*_forward` 方法的用途在第六单元详解。
- **延伸阅读源码**：建议接着读 `lightllm/models/llama/triton_kernel/silu_and_mul.py`（SwiGLU 的算子实现），把 4.2.3 节的 FFN 链路补全。
