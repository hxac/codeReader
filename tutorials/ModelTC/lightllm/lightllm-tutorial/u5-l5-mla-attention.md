# MLA 注意力实现

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 **MLA（Multi-head Latent Attention，多头潜变量注意力）** 相对标准 MHA/GQA 到底压缩了什么、为什么能压缩而不掉精度。
- 在 `deepseek2` 模型源码中，指认出「压缩 KV」「权重吸收（weight absorption）」「解压 KV」这三件事分别由哪些文件、哪些类、哪些方法负责。
- 看懂 DeepSeek-V2/V3 一层 transformer 的注意力部分在 prefill 和 decode 两条路径上的差异：**prefill 走解压（CC 方法），decode 走吸收**。
- 把 MLA 与本手册前面讲过的「KV 内存管理（u4-l1）」「注意力后端机制（u3-l5）」「MoE（u5-l4）」串联起来，理解 DeepSeek 这一族模型的完整推理图。

本讲是 u5-l4（MoE 模型推理）的后续，两者合在一起就构成了 DeepSeek-V2/V3 的全部注意力 + FFN。

## 2. 前置知识

在进入 MLA 之前，先回顾三个你已经掌握的概念（前序讲义已建立）：

1. **标准注意力的 KV Cache**。一次推理里，每个历史 token 都要为每一层、每个 KV 头存一对向量 \(K\) 和 \(V\)。每生成一个新 token，新 token 的 \(Q\) 要和**所有历史 token 的 \(K\)** 算点积得到注意力权重，再用权重对**所有历史 token 的 \(V\)** 做加权求和。所以 KV Cache 的大小直接决定能放下多长的上下文、能服务多少并发请求。

2. **KV 内存管理器（u4-l1）**。LightLLM 用 `MemoryManager` 持有一块 4 维张量 `kv_buffer`，形状为 `(layer_num, size+1, head_num, head_dim)`，按 token 分配「索引」、登记进 `req_to_token_indexs`。`kv_buffer[layer_index]` 就是某一层所有 token 的 KV。

3. **注意力后端（u3-l5）**。注意力算子被抽象成可替换的 `BaseAttBackend`，prefill 和 decode 各选一个后端（fa3 / flashinfer / triton），由 `AttControl` 这个入参开关告诉后端当前是普通注意力、MLA 还是 NSA。

如果上面三点你还觉得陌生，建议先回到 u4-l1 和 u3-l5 复习。本讲要回答的核心问题是：**DeepSeek 为什么敢把 KV Cache 缩小到原来的几十分之一？答案就是「把 KV 先压成一个潜变量，要用时再展开」。**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `lightllm/models/deepseek2/model.py` | DeepSeek-V2/V3 模型本体，继承 Llama，填入 MLA 相关的插槽（内存管理器、注意力后端、MLA 维度参数）。 |
| `lightllm/models/deepseek2/infer_struct.py` | 推理状态类，**几乎为空**——MLA 不需要额外状态字段，潜变量都住在 `mem_manager.kv_buffer` 里。 |
| `lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py` | MLA 的**推理核心**：`_get_qkv`（投影出潜变量）、`_context_attention_kernel`（prefill 解压）、`_token_attention_kernel`（decode 吸收）、`_decompress_kv`（CC 解压）、`_get_o`（吸收后的输出投影）。 |
| `lightllm/models/deepseek2/layer_weights/transformer_layer_weight.py` | MLA 权重结构：`kv_a_proj_with_mqa`（下投影压缩）、`k_b_proj`/`v_b_proj`（上投影，按头拆分）、`cc_kv_b_proj`（prefill 用的合并上投影）。 |
| `lightllm/models/deepseek2/triton_kernel/sample_kv.py` | prefill 时把潜变量从 `kv_buffer` 里按请求 gather 出来的 Triton kernel。 |
| `lightllm/common/kv_cache_mem_manager/deepseek2_mem_manager.py` | DeepSeek 专用内存管理器：`kv_buffer` 每个槽位只放 **1 个头**的潜变量；`get_att_input_params` 直接返回整层 buffer。 |
| `lightllm/common/basemodel/attention/create_utils.py` | MLA 注意力后端选择：`get_mla_prefill_att_backend_class` / `get_mla_decode_att_backend_class`。 |
| `lightllm/common/basemodel/attention/triton/mla.py` | Triton 版 MLA 后端：把 q/kv 拆成 nope 和 rope 两段后调用对应 kernel。 |
| `lightllm/common/basemodel/triton_kernel/mla_att/` | MLA 专用 Triton kernel：`prefill_att/context_flashattention_nopad_with_v.py`、`decode_att/gqa_flash_decoding.py`（两阶段 flash decoding）。 |

> 提示：`infer_struct.py` 只有 5 行，看似「没内容」，但这恰恰是 MLA 的一个特点——它的状态不在 Python 对象里，而在显存 buffer 里。这一点后面会反复用到。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 MLA 注意力**：MLA 是什么，DeepSeek 模型如何组装它、如何选注意力后端。
- **4.2 KV 压缩**：潜变量是怎么压出来的，内存管理器的形状如何因此改变。
- **4.3 MLA 算子**：权重吸收（decode）与解压（prefill）两条计算路径，以及 Triton kernel。

### 4.1 MLA 注意力

#### 4.1.1 概念说明

标准多头注意力（MHA）里，每个 KV 头都独立存自己的 \(K\)、\(V\)。DeepSeek 的 MLA 观察到一件事：**同一层的 \(K\) 和 \(V\) 往往是高度冗余的，可以先用一个下投影矩阵 \(W_{DKV}\) 把隐藏状态压成一个低维的「联合潜变量」\(c_{KV}\) 存起来，等真正算注意力时，再用上投影矩阵 \(W_{UK}\)、\(W_{UV}\) 把它分别展开成 \(K\) 和 \(V\)**。

用公式说更清楚。标准注意力的某一个头：

\[
K = x \cdot W_{UK}, \quad V = x \cdot W_{UV}
\]

而 MLA 在 **cache 阶段只存**：

\[
c_{KV} = \mathrm{RMSNorm}(x \cdot W_{DKV}) \in \mathbb{R}^{d_c}
\]

其中 \(d_c = \texttt{kv\_lora\_rank}\)（DeepSeek-V3 取 512）。真正要用 \(K\)、\(V\) 时，再上投影：

\[
K = c_{KV} \cdot W_{UK}, \quad V = c_{KV} \cdot W_{UV}
\]

此外，DeepSeek 还把一部分位置编码（RoPE）单独拎出来：\(K\) 拆成「不旋转的 nope 部分」和「要旋转的 rope 部分」。rope 部分**不参与压缩**，单独存（因为 RoPE 是位置相关的，压进潜变量会破坏可分解性）。所以最终每个 token 实际缓存的是：

\[
\text{cache} = [\,c_{KV}\ (\text{长度 } d_c)\,;\; k_{\text{rope}}\ (\text{长度 } d_r)\,]
\]

这是 MLA 的全部存储代价——**每个 token 只存一个长度为 \(d_c + d_r\) 的向量，且只有「1 个头」**，与查询头数 \(n_h\) 无关。

> 名词解释：
> - **潜变量（latent）**：被压缩后的低维表示 \(c_{KV}\)。
> - **下投影 / 上投影**：把高维压到低维叫下投影，把低维还原回高维叫上投影，类似 LoRA 的 low-rank 思路，所以代码里叫 `q_lora_rank` / `kv_lora_rank`。
> - **nope / rope**：nope = no position embedding（不做旋转的部分），rope = rotary position embedding（做旋转的部分）。

#### 4.1.2 核心流程

DeepSeek 一层 MLA 注意力的整体流程（与标准 transformer 层结构相同，只是注意力内部换成 MLA）：

```
输入 hidden_states
  ├─ input_layernorm
  ├─ _get_qkv：投影出 q（含 nope+rope）和 潜变量 cache_kv
  │     · q 走 q_a_proj → q_a_layernorm → q_b_proj（低秩两段投影）
  │     · cache_kv 走 kv_a_proj_with_mqa → kv_a_layernorm（只压一次）
  │     · 对 q_rope 和 k_rope 施加 RoPE
  ├─ _post_cache_kv：把潜变量 cache_kv 写进 mem_manager.kv_buffer[当前层]
  ├─ 注意力（两条路径二选一，见 4.3）
  │     · prefill：_context_attention_kernel（解压 CC 方法）
  │     · decode：_token_attention_kernel（权重吸收）
  ├─ _get_o：输出投影 o_proj（decode 时先做 v_b_proj 还原 V）
  └─ 残差连接
```

注意力后端的选择发生在模型初始化期。普通模型用 `get_prefill_att_backend_class`，MLA 模型用专门的 `get_mla_prefill_att_backend_class` / `get_mla_decode_att_backend_class`，后者只会从 MLA 专用后端表里挑。

#### 4.1.3 源码精读

先看模型本体如何声明「我是一个 MLA 模型」。`Deepseek2TpPartModel` 继承自 Llama，几乎不写新逻辑，只填插槽：

[lightllm/models/deepseek2/model.py:17-35](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/model.py#L17-L35) — 注册为 `deepseek_v2` / `deepseek_v3`，指定权重类、推理类、状态类；并覆写 `_init_att_backend` 改用 **MLA 专用**的注意力后端工厂。

关键的 MLA 维度参数在 `_init_some_value` 里读出：

[lightllm/models/deepseek2/model.py:37-47](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/model.py#L37-L47) — 读取 `qk_nope_head_dim`、`qk_rope_head_dim`、`q_lora_rank`、`kv_lora_rank`、`v_head_dim`，并算出 `head_dim_ = kv_lora_rank + qk_rope_head_dim`（这就是单 token 潜变量的总维度）。注意第 39-40 行 `tp_k_head_num_ = 1`、`tp_v_head_num_ = 0`——MLA 在 TP 下「只有 1 个 KV 头、0 个 V 头」，这是它与 GQA 的本质区别。

注意力后端的选择逻辑在 `create_utils.py`。MLA 有一张独立的后端映射表：

[lightllm/common/basemodel/attention/create_utils.py:47-53](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/create_utils.py#L47-L53) — `mla_data_type_to_backend` 只列了普通精度下的 `triton` / `fa3` / `flashinfer` 三种 MLA 后端，比普通注意力表少（MLA 目前不支持 int4/int8 KV，FP8 KV 走另一套 Deepseek3 管理器）。

[lightllm/common/basemodel/attention/create_utils.py:113-130](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/create_utils.py#L113-L130) — `get_mla_prefill_att_backend_class` / `get_mla_decode_att_backend_class`，逻辑与普通版一致（按优先级 + 子进程 validate），只是查的是 MLA 表。prefill 默认偏 `fa3`、decode 默认偏 `flashinfer`，全部失败兜底 `triton`。

至于推理状态类，确实「没什么可看」：

[lightllm/models/deepseek2/infer_struct.py:1-7](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/infer_struct.py#L1-L7) — `Deepseek2InferStateInfo` 只继承 `LlamaInferStateInfo`，没加任何字段。原因：潜变量 KV 不是存在 infer_state 里的 Python 张量，而是存在 `mem_manager.kv_buffer` 里，infer_state 只持有 `mem_manager` 的引用就够了。

#### 4.1.4 代码实践

> **实践目标**：确认 DeepSeek 模型确实「把自己标成 MLA」，并理清 MLA 相关的类继承关系。
>
> **操作步骤**（源码阅读型，无需 GPU）：
> 1. 打开 `lightllm/models/deepseek2/model.py`，确认 `Deepseek2TpPartModel` 继承自 `LlamaTpPartModel`（说明它复用 Llama 的全部基类流水线，只覆写 MLA 差异点）。
> 2. 在同一文件里找出三个被覆写的方法：`_init_att_backend`、`_init_some_value`、`_init_mem_manager`。
> 3. 全仓库搜索 `get_mla_prefill_att_backend_class`，确认它只在 `model.py` 的 `_init_att_backend` 里被调用。
>
> **需要观察的现象 / 预期结果**：你会看到 DeepSeek 把「我是一个 MLA 模型」这件事，集中表达在三个覆写里——换注意力后端工厂、改 KV 头数为 1、改内存管理器的 head 形状。其余逻辑（残差、norm、MoE 的 FFN）全部复用 Llama/基类。这正是 LightLLM「插槽 + 模板」设计（见 u5-l2、u5-l3）的威力：新增一种注意力机制只需替换几个插槽。
>
> 如果想验证运行期行为（待本地验证）：用 DeepSeek-V2/V3 的 `--model_dir` 启动服务，观察启动日志中 `Auto-selected ... backend (validated)` 和 `mem_manager class: Deepseek2MemoryManager` 两行是否出现。

#### 4.1.5 小练习与答案

**练习 1**：MLA 模型为什么把 `tp_v_head_num_` 设成 0？标准 GQA 模型这个值会是多少？

**参考答案**：因为 MLA 不在 cache 里存 \(V\) 本身，只存潜变量 \(c_{KV}\)，\(V\) 是用 \(W_{UV}\)（代码里的 `v_b_proj`）在需要时从潜变量现算的。所以 KV buffer 里没有「V 头」这一维，`tp_v_head_num_ = 0`。标准 GQA 模型里这个值等于 KV 头数（如 Llama-3 的 8）。

**练习 2**：`Deepseek2InferStateInfo` 几乎是空的，那「当前请求的潜变量 KV」到底存在哪里？

**参考答案**：存在 `infer_state.mem_manager.kv_buffer[layer_index]` 里，按 token 索引访问；`infer_state` 只通过 `mem_manager` 引用间接持有它。

### 4.2 KV 压缩

#### 4.2.1 概念说明

MLA 的核心收益是 **KV Cache 体积大幅缩小**。我们用 DeepSeek-V3 的真实配置算一笔账：

| 量 | 符号 | 取值（V3） |
| --- | --- | --- |
| 查询头数 | \(n_h\) | 128 |
| nope 头维度 | \(d_{\text{nope}}\) | 128 |
| V 头维度 | \(d_v\) | 128 |
| 潜变量维度 | \(d_c\) (`kv_lora_rank`) | 512 |
| rope 维度 | \(d_r\) (`qk_rope_head_dim`) | 64 |

如果是标准 MHA，每 token、每层的 KV Cache 体积是（\(K\) 和 \(V\) 各一份）：

\[
n_h \times (d_{\text{nope}} + d_v) = 128 \times (128 + 128) = 32{,}768
\]

而 MLA 每 token、每层只缓存一个潜变量加一段 rope：

\[
d_c + d_r = 512 + 64 = 576
\]

压缩比：

\[
\frac{576}{32{,}768} \approx 1.76\%
\]

也就是说 **KV Cache 缩到不到原来的 1/57**。这正是 DeepSeek 能服务超长上下文、超大并发的一个关键原因——同样显存能放下多得多的 token。

> 注意：MLA 用更小的 cache 换来了「用时要做一次上投影」的额外计算。所以 MLA 是 **以算换存** 的设计，在长上下文、高并发场景下非常划算。

#### 4.2.2 核心流程

KV 压缩在推理时涉及两步：

1. **写入（每层每 token 做一次，prefill 和 decode 都做）**：`_get_qkv` 把隐藏状态压成潜变量 `cache_kv`（长度 \(d_c + d_r\)），`_post_cache_kv` 把它写进 `kv_buffer[当前层]` 的对应槽位。
2. **读出（注意力计算时）**：prefill 用 `sample_kv` 把潜变量按请求 gather 出来再解压；decode 直接拿 `get_att_input_params` 返回的整层 buffer，配合吸收后的 \(Q\) 算注意力。

因为每 token 只存 1 个「头」，内存管理器的形状也变了：`head_num=1`、`head_dim = d_c + d_r`。

#### 4.2.3 源码精读

先看压缩是怎么发生的。`_get_qkv` 里，对隐藏状态先做一次联合下投影，再分别上投影出 \(Q\) 和潜变量：

[lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py:176-200](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py#L176-L200) — 这是 `q_lora_rank is not None` 的分支（DeepSeek-V2/V3 走这里）。`qkv_a_proj_with_mqa_` 一次算出 `[q_a（低秩中间量）, kv_a（潜变量前身）]`；潜变量部分 reshape 成 `(-1, 1, kv_lora_rank + qk_rope_head_dim)`，对前 `kv_lora_rank` 维做 `kv_a_layernorm_`，对 rope 部分施加 RoPE，最后返回 `(q, cache_kv)`。

> 注意第 188 行的 `view(-1, 1, ...)`——第二个维度是 **1**，对应「1 个 KV 头」，这是 MLA 的标志。

把潜变量写进显存池的逻辑来自模板基类（u3-l3 已讲过 `_post_cache_kv` 的「写后读」闭环）：

[lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py:35-42](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py#L35-L42) — `_post_cache_kv` 调用 `mem_manager.operator.copy_kv_to_mem_manager`，把 `cache_kv` 按 `mem_index`（分配到的 token 槽位）写进 `kv_buffer[当前层]`。

内存管理器的形状定义在 DeepSeek 专用管理器里：

[lightllm/common/kv_cache_mem_manager/deepseek2_mem_manager.py:28-29](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/deepseek2_mem_manager.py#L28-L29) — `kv_buffer = torch.empty((layer_num, size + 1, head_num, head_dim))`。对 MLA，`head_num=1`、`head_dim=kv_lora_rank + qk_rope_head_dim`，所以每 token 只占 `1 × (d_c+d_r)` 个元素，正是 4.2.1 算出的 576。

而 `model.py` 创建管理器时就是这么传的：

[lightllm/models/deepseek2/model.py:61-72](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/model.py#L61-L72) — `head_num=1`、`head_dim=kv_lora_rank + qk_rope_head_dim`，并按模型族通过 `select_mem_manager_class` 选中 `Deepseek2MemoryManager`（见 `mem_utils.py` 的 `issubclass(model_class, Deepseek2TpPartModel)` 分支）。

读出时，MLA 不像普通注意力那样把 K、V 分两块取，而是把整层潜变量 buffer 当作 K 传进去：

[lightllm/common/kv_cache_mem_manager/deepseek2_mem_manager.py:21-23](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/deepseek2_mem_manager.py#L21-L23) — `get_att_input_params` 直接返回 `self.kv_buffer[layer_index]`，不做任何拆分。拆 nope/rope 的事交给注意力后端。

#### 4.2.4 代码实践

> **实践目标**：亲手算一遍 MLA 的 KV Cache 压缩比，并在源码里确认管理器形状。
>
> **操作步骤**：
> 1. 找一份 DeepSeek-V3 的 `config.json`（HuggingFace 上 `deepseek-ai/DeepSeek-V3` 的仓库），读出 `kv_lora_rank`、`qk_rope_head_dim`、`qk_nope_head_dim`、`v_head_dim`、`num_attention_heads`、`num_hidden_layers`。
> 2. 按本节 4.2.1 的公式，算出「MLA 每 token 每层字节数」与「等价 MHA 每 token 每层字节数」（注意 bf16 每元素 2 字节）。
> 3. 在 `deepseek2_mem_manager.py` 的 `_init_buffers` 与 `model.py` 的 `_init_mem_manager` 里，对照确认 `head_num=1`、`head_dim` 就是 `kv_lora_rank + qk_rope_head_dim`。
>
> **预期结果**：每 token 每层 MLA 体积约为 `576 × 2 = 1152` 字节，等价 MHA 约为 `32768 × 2 = 65536` 字节，比值约 1.76%。若你的 `config.json` 数值与上文不同，请以你读到的真实值为准重新计算。
>
> 如果无法联网取 config，可标注「待本地验证」，仅完成源码形状对照即可。

#### 4.2.5 小练习与答案

**练习 1**：为什么 rope 部分（\(k_{\text{rope}}\)）不一起压进潜变量 \(c_{KV}\)？

**参考答案**：因为 RoPE 是「按位置旋转」的操作，它会**破坏上投影的可分解性**。MLA 能省算的关键是 \(Q \cdot K^T = Q \cdot (W_{UK} c_{KV})^T = (Q W_{UK}^T) \cdot c_{KV}^T\)，即可以把 \(W_{UK}\) 吸收进 \(Q\)。一旦 \(K\) 里混入了位置相关的旋转，这个等式就不再成立。所以 rope 部分必须单独存、单独参与点积。

**练习 2**：`get_att_input_params` 在 MLA 里返回的是 K、是 V、还是潜变量？

**参考答案**：返回的是**潜变量 buffer**（同时含 nope 潜变量和 rope 段），由后续注意力后端拆分。它既不是最终的 K 也不是最终的 V。

### 4.3 MLA 算子

本模块是 MLA 最巧妙、也是本讲最重要的部分：**prefill 走解压（CC 方法），decode 走权重吸收**。这两条路径对应不同数学等价变形，目的是在各自场景下让计算量最小。

#### 4.3.1 概念说明

先写标准 MLA 注意力的数学形式（省略 rope、softmax 缩放）：

\[
O = \mathrm{softmax}\!\left(\frac{Q_{\text{nope}} \cdot K_{\text{nope}}^T}{\sqrt{d}}\right) V
\]

其中 \(K_{\text{nope}} = c_{KV} \cdot W_{UK}^T\)、\(V = c_{KV} \cdot W_{UV}^T\)。代入后：

\[
Q_{\text{nope}} \cdot K_{\text{nope}}^T = Q_{\text{nope}} \cdot W_{UK} \cdot c_{KV}^T = \underbrace{(Q_{\text{nope}} \cdot W_{UK})}_{Q'} \cdot c_{KV}^T
\]

这里出现两条等价路径：

- **解压路径（展开 K、V）**：先用 \(W_{UK}\)、\(W_{UV}\) 把 \(c_{KV}\) 还原成完整的 \(K\)、\(V\)，再做标准注意力。
- **吸收路径（折叠进 Q）**：先把 \(W_{UK}\) 吸收进 \(Q\) 得到潜空间里的 \(Q'\)，直接拿 \(Q'\) 对潜变量 \(c_{KV}\) 算注意力分数，得到对 \(c_{KV}\) 的加权求和 \(u\)，最后再统一乘 \(W_{UV}\) 还原成输出。

两条路径在数学上等价，但计算量随「查询 token 数 \(m\)」与「历史 token 数 \(n\)」变化：

- **prefill**：\(m\) 很大（一整段 prompt，可能几千 token），\(n\) 也大。如果走吸收，要为每个查询 token 都算一次 \(Q' = Q_{\text{nope}} \cdot W_{UK}\)（一次 \([m, d_{\text{nope}}] \times [d_{\text{nope}}, d_c]\) 的大矩阵乘）。不如**先把 \(c_{KV}\) 解压成 \(K, V\)（一次 \([n, d_c] \times [d_c, n_h(d_{\text{nope}}+d_v)]\) 的 matmul）**，之后所有查询 token 共享这份解压后的 K、V。LightLLM 把这种「合并上投影」叫做 **CC 方法**（代码里的 `cc_kv_b_proj_`、`enable_cc_method`）。
- **decode**：\(m = 1\)（每次只生成 1 个 token），\(n\) 可能很大。此时走吸收最划算：只算一个 \(Q' = q_{\text{nope}} \cdot W_{UK}\)（即代码里的 `k_b_proj_.bmm(q_nope)`），然后直接对潜变量 \(c_{KV}\) 做 flash decoding，最后用 `v_b_proj` 还原。这避免了把整段潜变量都解压成多头的巨大中间张量。

> 名词解释：
> - **权重吸收（weight absorption）**：把上投影矩阵 \(W_{UK}\) 「吸收」进查询 \(Q\)，使注意力直接在潜空间完成。
> - **CC 方法**：LightLLM 内部命名，指 prefill 时把 \(W_{UK}\) 和 \(W_{UV}\) 合并成 `cc_kv_b_proj` 对潜变量**整体解压**的路径。可通过环境变量 `DISABLE_CC_METHOD` 关闭，关闭后退回吸收路径。

#### 4.3.2 核心流程

两条路径的伪代码：

```
# prefill（CC 解压路径）
sample_kv: 从 kv_buffer 按请求 gather 出 sampled_compressed_kv (n, d_c) 和 sampled_k_rope (n, d_r)
sampled_kv_nope = cc_kv_b_proj(sampled_compressed_kv)   # (n, n_h*(d_nope+d_v)) 解压
k_nope, v = split(sampled_kv_nope)                       # 拆出 K 和 V
o = context_attention(q_nope, q_rope, k_nope, k_rope, v) # 标准注意力，K/V 多头

# decode（吸收路径）
q' = k_b_proj(q_nope)            # (1, n_h, d_c)  把 W_UK 吸收进 Q
kv = get_att_input_parameters()  # 整层潜变量 buffer (含 nope 潜变量 + rope)
o_latent = gqa_flash_decoding(q'_nope=q', q_rope, kv_nope=kv[...,:d_c], kv_rope=kv[...,d_c:])  # 潜空间注意力
o = v_b_proj(o_latent)           # (1, n_h, d_v)  最后用 W_UV 还原
o = o_proj(o)                    # 输出投影
```

两条路径最终都调用一个「分 nope/rope 两段」的注意力 kernel，区别只在 K、V 是「解压后的多头」还是「潜空间单头」。

#### 4.3.3 源码精读

**A. prefill 解压路径（CC 方法）**

prefill 注意力入口 `_context_attention_kernel` 先调 `_decompress_kv` 把潜变量解压成多头 K、V，再交给 prefill 后端：

[lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py:73-93](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py#L73-L93) — `_decompress_kv` 返回 `(k_nope, k_rope, v)`，然后 `prefill_att(q, k=(k_nope, k_rope), v=v, att_control=AttControl(mla_prefill=True, ...))`。注意 `AttControl(mla_prefill=True)` 这个开关，它告诉后端走 MLA 专用的 prefill 实现。

解压的核心是 `_decompress_kv`：

[lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py:115-147](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py#L115-L147) — 三步：① 用 `sample_kv`（Triton kernel）按请求把潜变量从 `kv_buffer[当前层]` gather 成连续张量；② 用 `cc_kv_b_proj_.mm` 把潜变量（\(d_c\) 维）解压成 `qk_nope_head_dim + v_head_dim` 每头的多头表示；③ `torch.split` 拆成 `k_nope` 和 `v`。这里的 `cc_kv_b_proj_` 就是合并了 \(W_{UK}\)、\(W_{UV}\) 的上投影。

`sample_kv` 的 Triton kernel 负责高效 gather：

[lightllm/models/deepseek2/triton_kernel/sample_kv.py:43-55](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/triton_kernel/sample_kv.py#L43-L55) — 通过 `req_to_token_indexs` 把每个请求的 token 槽位索引翻译成 `kv_buffer` 里的实际位置，把 nope 段（前 `kv_lora_rank` 维）和 rope 段（后 64 维）分别写到输出张量。

**B. decode 吸收路径**

decode 注意力入口 `_token_attention_kernel` 把 \(W_{UK}\) 吸收进 \(Q\)：

[lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py:95-113](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py#L95-L113) — 把 \(Q\) 拆成 `q_nope`、`q_rope`；对 `q_nope` 做 `layer_weight.k_b_proj_.bmm(q_nope.transpose(0,1))`，**这一步就是权重吸收**（把 \(W_{UK}\) 乘进 \(Q\)，得到潜空间查询，维度从 `d_nope` 降到 `d_c`）；然后从 `mem_manager.get_att_input_parameters(layer_index=self.layer_num_)` 取整层潜变量，交给 `decode_att`，`AttControl(mla_decode=True,...)`。

吸收后的还原发生在输出投影 `_get_o`：

[lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py:202-212](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py#L202-L212) — 若 `input.shape[2] == self.kv_lora_rank`（说明输入还在潜空间，即 decode 走了吸收），先用 `v_b_proj_.bmm(...)`（\(W_{UV}\)）把潜空间输出还原成 `v_head_dim`，再 `o_weight_.mm(...)` 做输出投影。`bmm` 是按头做的批量矩阵乘（`ROWBMMWeight`，见下文权重结构）。

**C. 权重结构：kv_b_proj 的「合并 / 拆分」双形态**

MLA 最容易绕晕的是 `kv_b_proj`（上投影）在代码里有三种形态：原始合并权重、按头拆出的 `k_b_proj`/`v_b_proj`、以及 prefill 用的 `cc_kv_b_proj`。加载时一次拆分，推理时按路径取用：

[lightllm/models/deepseek2/layer_weights/transformer_layer_weight.py:67-76](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/layer_weights/transformer_layer_weight.py#L67-L76) — `_split_kv_b_proj` 把合并的 `kv_b_proj`（形状 `n_h × (d_nope+d_v) × d_c`）沿第二维拆成 `k_b_proj`（`n_h × d_nope × d_c`，即 \(W_{UK}\)）和 `v_b_proj`（转置成 `n_h × d_c × d_v`，即 \(W_{UV}\)）。

[lightllm/models/deepseek2/layer_weights/transformer_layer_weight.py:95-115](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/layer_weights/transformer_layer_weight.py#L95-L115) — `load_hf_weights` 加载 `kv_b_proj.weight` 后立即拆分，把拆出的两份分别以 `k_b_proj.weight`、`v_b_proj.weight` 的名字塞回 weights dict，供后续元权重按名加载。

[lightllm/models/deepseek2/layer_weights/transformer_layer_weight.py:155-178](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/layer_weights/transformer_layer_weight.py#L155-L178) — `k_b_proj_`、`v_b_proj_` 是按头的 `ROWBMMWeight`（decode 吸收用，bmm 调用）；`cc_kv_b_proj_` 是合并的 `ROWMMWeight`（prefill 解压用，普通 mm 调用），仅在 `enable_cc_method` 为真时创建。三者底层都指向同一份 HF 权重 `kv_b_proj.weight`，只是切分/合并方式不同。

**D. 注意力后端：分 nope/rope 两段算**

Triton 版 MLA 后端把 \(Q\) 拆成 `q_nope`、`q_rope`（rope 固定 64 维）后，调用分块 kernel：

[lightllm/common/basemodel/attention/triton/mla.py:39-69](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/triton/mla.py#L39-L69) — `_mla_prefill_att` 调用 `context_attention_fwd_with_v`，分别传入 `q_nope/q_rope/k_nope/k_rope/v`。注意第 49 行 `qk_rope_head_dim = 64` 是硬编码——因为 DeepSeek 的 rope 维度固定为 64。

[lightllm/common/basemodel/attention/triton/mla.py:133-159](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/triton/mla.py#L133-L159) — `_mla_decode_att` 把潜变量 kv 拆成 `kv_nope`（前 `d_c` 维，潜空间）和 `kv_rope`（后 64 维），调用 `gqa_token_decode_attention_flash_decoding`。

prefill 的底层 Triton kernel 显式接收 5 个独立张量（q_nope、q_rope、k_nope、k_rope、v）：

[lightllm/common/basemodel/triton_kernel/mla_att/prefill_att/context_flashattention_nopad_with_v.py:10-39](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/mla_att/prefill_att/context_flashattention_nopad_with_v.py#L10-L39) — `_fwd_kernel_with_v` 的入参里 `Q_nope`/`Q_rope`/`K_nope`/`K_rope`/`V` 各自带独立 stride，kernel 内部把 nope 和 rope 的点积分开做再相加，从而支持「nope 在多头/潜空间、rope 单头」的混合维度。

decode 走经典的两阶段 flash decoding（先分块算局部 softmax，再归约）：

[lightllm/common/basemodel/triton_kernel/mla_att/decode_att/gqa_flash_decoding.py:14-67](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/mla_att/decode_att/gqa_flash_decoding.py#L14-L67) — `gqa_token_decode_attention_flash_decoding` 接收 `q_nope`（潜空间，`q_head_num × d_c`）、`q_rope`（`q_head_num × 64`）、`kv_nope`、`kv_rope`，先调 `MlaDecodeAttentionKernelConfig.try_to_get_best_config` 选运行参数，再分 stage1（分块计算 `mid_o` / `mid_o_logexpsum`）和 stage2（跨块归约出 `o_tensor`）。第 20-22 行的注释揭示了吸收后查询的维度：`q_head_num, kv_lora_rank = q_nope.shape[1], q_nope.shape[2]`——查询已经在潜空间了。

#### 4.3.4 代码实践

> **实践目标**：把「吸收」和「解压」两条路径在源码里走通，能对着代码说出每一步的矩阵形状变化。这是本讲 practice_task 的核心。
>
> **操作步骤**（源码阅读 + 形状标注型，无需 GPU）：
> 1. 打开 `layer_infer/transformer_layer_infer.py`，定位 `_token_attention_kernel`（decode 吸收）。
> 2. 在纸上或注释里标注 decode 的张量形状演变（设 V3 配置：`tp_q_head_num=128`、`d_nope=128`、`d_c=512`、`d_r=64`、`d_v=128`，单 token）：
>    - `q` 进来时：`(1, 128, 128+64)` —— 1 token、128 头、(nope+rope)
>    - 拆分后 `q_nope`: `(1, 128, 128)`，`q_rope`: `(1, 128, 64)`
>    - `k_b_proj_.bmm(q_nope)`：`(1, 128, 128) × (128, 128, 512) → (1, 128, 512)` —— **吸收完成，查询降到潜空间**
>    - 潜变量 `kv`: `(n, 1, 576)`，拆成 `kv_nope (n,1,512)`、`kv_rope (n,1,64)`
>    - `gqa_flash_decoding` 输出：`(1, 128, 512)` —— 仍在潜空间
>    - `_get_o` 中 `v_b_proj_.bmm`：`(1, 128, 512) × (128, 512, 128) → (1, 128, 128)` —— **还原回 V 空间**
>    - `o_proj`：`(1, 128×128) × ... → (1, hidden_size)`
> 3. 再定位 `_decompress_kv`（prefill 解压），对比标注 prefill 的形状演变，重点看 `cc_kv_b_proj_.mm` 把 `(n, 512)` 解压成 `(n, 128×(128+128))`。
> 4. 在 `layer_weights/transformer_layer_weight.py` 的 `_init_qkvo` 里，确认 `k_b_proj_`、`v_b_proj_` 用 `ROWBMMWeight`（按头 bmm），而 `cc_kv_b_proj_` 用 `ROWMMWeight`（整体 mm）。
>
> **需要观察的现象 / 预期结果**：你会清楚看到——decode 里 \(W_{UK}\)（`k_b_proj`）作用在 \(Q\) 上、\(W_{UV}\)（`v_b_proj`）作用在注意力输出上；prefill 里 `cc_kv_b_proj` 一次性作用在潜变量 \(c_{KV}\) 上。两者用同一份 HF 权重的不同切分形态。
>
> 如果想验证运行期形状（待本地验证）：可在 `_token_attention_kernel` 的 `k_b_proj_.bmm(...)` 后临时加一行 `assert q_nope.shape[2] == self.kv_lora_rank`，用 DeepSeek 权重跑一次 decode 验证（修改源码仅为本地验证，勿提交）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 prefill 选「解压（CC 方法）」而 decode 选「吸收」？反过来不行吗？

**参考答案**：prefill 查询 token 数 \(m\) 大，若走吸收，要为每个查询 token 都算一次 \(Q' = Q_{\text{nope}} W_{UK}\)（一次大 matmul），且吸收后注意力中间张量是多头潜空间表示，开销大；而解压只需对 \(n\) 个历史 token 做一次 `cc_kv_b_proj`，所有查询 token 共享解压结果，更划算。decode 时 \(m=1\)，吸收只需算一个 \(Q'\)，且能避免把整段潜变量都解压成多头的巨大中间张量，所以吸收更划算。反过来不是不行（设 `DISABLE_CC_METHOD=ON` 可让 prefill 也走吸收），只是在各自典型场景下次优。

**练习 2**：`k_b_proj`、`v_b_proj`、`cc_kv_b_proj` 三者底层是不是同一份权重？为什么要维护三个？

**参考答案**：是同一份 HF 权重 `self_attn.kv_b_proj.weight` 的不同切分形态。`cc_kv_b_proj` 是合并形态（供 prefill 整体 mm 解压），`k_b_proj`、`v_b_proj` 是按头拆出的形态（供 decode 的 bmm 吸收）。维护三个是为了让两条路径都能拿到「形状最顺手」的权重，避免运行时反复 reshape/transpose。

**练习 3**：`softmax_scale` 在 MLA 里为什么是 `(qk_nope_head_dim + qk_rope_head_dim) ** (-0.5)`，而不是单纯 `d_nope ** (-0.5)`？

**参考答案**：因为 MLA 注意力分数是 nope 部分点积与 rope 部分点积**相加**后的结果，等效参与点积的维度是 \(d_{\text{nope}} + d_{\text{rope}}\)（V3 是 128+64=192），所以缩放因子按总维度算。若启用了 YaRN 长上下文（`mscale_all_dim`），还会再乘一个 `mscale` 修正（见 `model.py` 的 `_init_to_get_yarn_rotary` 与 `__init__` 中的 scale 计算）。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个**「MLA 注意力一层推理全追踪」**任务：

> **任务**：假设你要向同事解释 DeepSeek-V3 一层 transformer 的 MLA 注意力在 prefill 和 decode 时分别怎么算，请产出一份「带形状的流程说明」。

具体要求：

1. **画两张数据流图**（文字版即可）：
   - Prefill 路径：`hidden_states → _get_qkv → cache_kv(潜变量) → _post_cache_kv → kv_buffer`；以及 `_decompress_kv(sample_kv + cc_kv_b_proj) → k_nope/k_rope/v → context_attention_fwd_with_v → o`。
   - Decode 路径：`hidden_states → _get_qkv → cache_kv → _post_cache_kv → kv_buffer`；以及 `q → split(q_nope/q_rope) → k_b_proj(吸收) → q'(潜空间) → gqa_flash_decoding(对潜变量) → u → v_b_proj(还原) → o_proj → o`。
2. **在每条边上标注张量形状**（用 V3 配置，单 batch；prefill 用一段长度为 \(L\) 的 prompt）。
3. **指出三个关键代码位置**：吸收发生在哪一行、解压发生在哪一行、潜变量落池发生在哪个模板方法。
4. **回答一个延伸问题**：如果把 `DISABLE_CC_METHOD` 设为 `ON`，prefill 还能正常跑吗？会改走哪条路径？（提示：看 `enable_cc_method` 控制了哪个权重的创建，以及没有它时 `_decompress_kv` 能否成立。）

> 预期产出：一份 1-2 页的说明，能让一个没读过源码的人理解「MLA = 压着存 + 两种方式展开算」。若无法跑真实模型验证形状，可在关键形状处标注「待本地验证」，但代码位置引用必须准确。

## 6. 本讲小结

- **MLA 的本质**是用一个低维潜变量 \(c_{KV}\)（长度 `kv_lora_rank`）替代多头 K、V 缓存，外加一段不参与压缩的 rope（`qk_rope_head_dim`），把每 token 的 KV 体积从 \(n_h(d_{\text{nope}}+d_v)\) 压到 \(d_c + d_r\)，V3 实测约缩到 1/57。
- **模型组装层面**，`Deepseek2TpPartModel` 几乎不写新逻辑，只覆写 `_init_att_backend`（换 MLA 后端工厂）、`_init_some_value`（KV 头数设 1、读 MLA 维度）、`_init_mem_manager`（`head_num=1`、`head_dim=d_c+d_r`）三个插槽。
- **存储层面**，潜变量不在 infer_state 里，而在 `Deepseek2MemoryManager.kv_buffer[layer]`，每 token 一个「1 头」槽位；`get_att_input_params` 直接返回整层 buffer。
- **算子层面**有两条等价路径：**prefill 走 CC 解压**（`_decompress_kv` + `cc_kv_b_proj`，把潜变量整体展开成多头 K、V），**decode 走权重吸收**（`k_b_proj` 把 \(W_{UK}\) 吸进 \(Q\)，注意力在潜空间完成，`v_b_proj` 最后还原）。
- **权重层面**，同一份 `kv_b_proj` 权重在加载时被拆成 `k_b_proj`/`v_b_proj`（按头 bmm，decode 用）和 `cc_kv_b_proj`（合并 mm，prefill 用）三种形态，由 `enable_cc_method` 控制。
- **后端层面**，MLA 有一张独立的后端映射表（`mla_data_type_to_backend`），prefill 偏 fa3、decode 偏 flashinfer、兜底 triton；Triton kernel 显式把 nope/rope 拆成两段独立点积再相加。

## 7. 下一步学习建议

- **横向对比 NSA**：本讲讲了 MLA，u3-l5 提到的 NSA（原生稀疏注意力）是另一套注意力变体。可阅读 `lightllm/common/basemodel/attention/nsa/` 下的 `flashmla_sparse.py`，对比它与 MLA 在 KV 组织上的差异。
- **纵向深入 FP8 MLA**：u6-l3 讲 FP8 KV 量化，而 DeepSeek-V3 还有一套 `Deepseek3_2MemoryManager` + `FP8PerTokenGroupQuantDeepseek3_2MemoryManager`（见 `mem_utils.py` 的 `fp8kv_dsa` 分支），把潜变量再量化到 FP8，是 MLA + 量化的极致显存优化，值得作为进阶阅读。
- **回到完整推理图**：把本讲（MLA 注意力）与 u5-l4（MoE FFN）合起来，对照 `Deepseek2TransformerLayerInfer` 的 `overlap_tpsp_token_forward` / `overlap_tpsp_context_forward`，看 attention 与 MoE 如何在 microbatch overlap 下交错执行，这是 DeepSeek 推理性能优化的关键。
- **PD 分离下的 MLA 迁移**：u7-l1 讲 PD 分离与 KV 迁移，其中 `mla_page_io`（见 `deepseek2_mem_manager.py` 的 import）正是为 MLA 潜变量的分页迁移设计的，可作为「MLA 如何跨节点搬家」的延伸阅读。
