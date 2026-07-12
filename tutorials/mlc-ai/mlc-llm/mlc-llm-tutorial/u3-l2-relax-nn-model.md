# 用 TVM Relax nn 编写模型

## 1. 本讲目标

本讲以 Llama 架构为样本，教你如何用 `tvm.relax.frontend.nn`（以下简称 **Relax nn**）这套「类 PyTorch 写法」来定义一个 MLC LLM 模型。学完后你应该能够：

1. 看懂一个 MLC 模型文件的整体骨架：从 `Config` 到顶层 `nn.Module` 的层层组合。
2. 说出 Llama 注意力层与 FFN 层用 Relax nn 是怎么写的，以及 QKV 融合、SwiGLU、GQA 在源码里长什么样。
3. 理解模型暴露给编译器与运行期的「执行入口」——`prefill` / `decode` / `batch_verify` / `embed` / `get_logits` / `create_paged_kv_cache`。
4. 认识 `get_default_spec` 这个「张量接口契约」的作用，以及它如何被编译主流程消费。

承接上一讲（u3-l1）：上一讲我们知道了 `MODELS` 注册表会把架构名 `llama` 绑定到一个 `Model` 信封，信封的 `model` 字段指向构造器 `LlamaForCausalLM`。本讲就钻进这个构造器，看一个模型到底是怎么「写」出来的。

## 2. 前置知识

- **nn.Module（神经网络模块）**：Relax nn 借鉴 PyTorch，用 `nn.Module` 表示一层或一整组层。子模块作为属性挂上去，`forward` 方法描述前向计算。和 PyTorch 不同的是，Relax nn 写出来的是「计算图 IR」，会被 TVM 进一步编译成各平台代码。
- **Tensor**：Relax nn 里的张量，带有形状（其中某些维度可以是符号，如 `"seq_len"`）和数据类型（如 `"float32"`、`"int32"`）。
- **prefill / decode / verify**：这是 LLM 推理的三个阶段。
  - **prefill（预填充）**：处理用户输入的整段 prompt，一次性算出每个位置的隐状态，并把它们写进 KV 缓存；只取最后一个 token 的 logits 用于生成第一个新 token。
  - **decode（解码）**：每步只喂入上一步生成的一个 token（序列长度为 1），增量更新 KV 缓存，产出下一个 token。
  - **verify（校验）**：用于推测解码（speculative decoding），一次喂入「草稿模型」猜出来的若干 token，由大模型批量校验哪些该接受。
- **KV 缓存（KV cache）**：注意力机制里，每个 token 的 Key/Value 可以缓存复用，避免对历史 token 重复计算。MLC 用分页式 KV 缓存（`PagedKVCache`），它同时把 RoPE 旋转与注意力融合在了一起（见后文）。
- **RoPE（旋转位置编码）**：Llama 用的位置编码方式，通过对 Q、K 做旋转来注入位置信息。本讲里你会看到 RoPE 参数是如何「挂」到 KV 缓存上的。

> 提示：本讲不要求你会写 TVM 的底层调度，只要能读懂「类 PyTorch」的模型定义即可。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/mlc_llm/model/llama/llama_model.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py) | Llama 模型定义主体：`LlamaConfig` 与从 `LlamaFFN` 到 `LlamaForCausalLM` 的全部类。本讲的主战场。 |
| [python/mlc_llm/model/model_utils.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/model_utils.py) | 跨模型共享的小工具，本讲用到的 `index_last_token`（取最后一个 token）就在这里。 |
| [python/mlc_llm/nn/kv_cache.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/nn/kv_cache.py) | `PagedKVCache` 的薄封装，定义了 `create_generic` 工厂方法（被模型的 `create_paged_kv_cache` 调用）。 |
| [python/mlc_llm/interface/compile.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py) | 编译主流程，本讲只看它如何「消费」`get_default_spec()`，从而理解 spec 契约的意义。 |

辅助引用（上一讲已介绍，本讲顺带用到）：[python/mlc_llm/model/model.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/model.py) 里 `"llama"` 注册项把 `model=llama_model.LlamaForCausalLM` 绑进注册表。

## 4. 核心概念与源码讲解

### 4.1 Relax nn 模型结构：从 Config 到顶层 Module

#### 4.1.1 概念说明

一个 MLC 模型文件通常由两部分组成：

1. **配置类（Config）**：一个 dataclass，字段直接对应 HuggingFace `config.json` 里的键（`hidden_size`、`num_attention_heads` 等）。它负责「读 config.json + 推断/校验缺失字段」。
2. **模型类（若干 nn.Module）**：吃一个 Config 实例，构造出整张网络。模块层层嵌套，最顶层一般是 `XxxForCausalLM`，它对外的「方法」就是推理引擎能调用的「执行入口」。

Llama 的嵌套关系是：

```
LlamaForCausalLM            # 顶层，对外暴露 prefill/decode/...
 └─ LlamaModel              # 模型主体：embedding + N 层 decoder + 最终 norm
     ├─ LlamaEmbedding      # token → 向量
     ├─ LlamaDecoderLayer × N
     │    ├─ LlamaAttention
     │    └─ LlamaFFN
     └─ nn.RMSNorm          # 最终归一化
 └─ lm_head（可选）          # hidden → vocab logits，与 embedding 共享权重时可省
```

#### 4.1.2 核心流程

定义一个模型的「套路」是：

1. 写 `XxxConfig`，列出从 `config.json` 读到的字段，在 `__post_init__` 里补全/校验派生字段（如 `head_dim`、`context_window_size`）。
2. 从最底层算子层开始往上写：`Attention` → `FFN` → `DecoderLayer` → `Model` → `ForCausalLM`。
3. 每个子模块在 `__init__` 里声明参数（`nn.Linear`、`nn.RMSNorm`、`nn.Embedding`），在 `forward` 里描述计算。
4. 在顶层 `ForCausalLM` 里，除了内部组合，还要定义一组「阶段方法」（`prefill`、`decode` 等），它们才是被编译导出的入口。
5. 实现 `get_default_spec()`，声明这些方法的输入张量形状/类型，作为给编译器的契约。

#### 4.1.3 源码精读

先看配置类。`LlamaConfig` 继承 `ConfigBase`，字段与 HF 一一对应，并保留了 `kwargs` 兜底多余字段：

[LlamaConfig 字段定义：L23-L44](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L23-L44) — 声明 `hidden_size`、`num_attention_heads`、`vocab_size`、`position_embedding_base`、`tensor_parallel_shards` 等字段，`kwargs` 收纳 config.json 里多余或别名的键。

[LlamaConfig.__post_init__：L46-L103](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L46-L103) — 这一长段做的事：① 若没给 `position_embedding_base` 就从 `kwargs["rope_theta"]` 取（否则默认 10000）；② 解析 `rope_scaling`（只接受 llama3 类型）；③ 若 `context_window_size` 为 0，就尝试从 `max_position_embeddings` / `max_sequence_length` 推断；④ 推断 GQA 的 `num_key_value_heads` 和 `head_dim`；⑤ 给 `prefill_chunk_size` 一个默认值（`min(context_window_size, 8192)`）。简言之：**config.json 里的「半成品」在这里被加工成「可直接用于建图」的完整配置**。

再看顶层 `LlamaForCausalLM` 的构造，它把主体 `LlamaModel` 装进来，并处理 `tie_word_embeddings`（是否让最后的分类头与 embedding 共享权重）：

[LlamaForCausalLM.__init__：L248-L280](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L248-L280) — 组装 `self.model = LlamaModel(config)`；若不共享权重就建独立的 `lm_head`；把一批派生属性（`num_hidden_layers`、`head_dim`、`rope_theta`、`tensor_parallel_shards`、`disaggregation`、`dtype`）缓存下来，后面 `create_paged_kv_cache` 要用；`_set_pp()` 给每个参数打上 `pipeline_stages` 标签（流水线并行的分段信息，本讲不展开）。

主体 `LlamaModel` 就是「embedding + N 层 decoder + 最终 norm」的标准组合，同时计算了流水线分段的边界：

[LlamaModel：L217-L244](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L217-L244) — `embed_tokens` 是 embedding 表；`layers` 是 `nn.ModuleList([LlamaDecoderLayer(config) for _ in range(num_hidden_layers)])`；`forward` 里逐层调用，在分段边界插入 `op_ext.pipeline_stage_boundary`（流水线通信点，单卡时是 no-op），最后过一层 `nn.RMSNorm`。

注意一个关键写法：`LlamaEmbedding("vocab_size", config.hidden_size)` 和 `nn.Linear(config.hidden_size, "vocab_size", bias=False)` 里出现了**字符串维度** `"vocab_size"`。这是 Relax nn 的符号维度——它表示「这个维度的大小不写死，由 spec / 运行期决定」。`vocab_size` 这种用法在量化时会按真实词表大小特化。

#### 4.1.4 代码实践（源码阅读型）

实践目标：在一张纸上（或注释里）画出 Llama 的「类嵌套树」，并标注每个类的行号范围。

操作步骤：

1. 打开 [llama_model.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py)。
2. 用搜索定位 7 个类：`LlamaConfig`、`LlamaFFN`、`LlamaEmbedding`、`LlamaAttention`、`LlamaDecoderLayer`、`LlamaModel`、`LlamaForCausalLM`。
3. 画出 4.1.1 里那张嵌套树，在每条边上写「子模块属性名」（如 `self.self_attn`、`self.mlp`、`self.input_layernorm`）。

需要观察的现象：注意 `LlamaForCausalLM` 不直接持有 `LlamaAttention`，而是通过 `self.model.layers[i].self_attn` 间接访问——这种「只组合、不重复声明」是 Relax nn 模型的典型组织方式。

预期结果：得到一张从 `LlamaForCausalLM` 一路下钻到 `nn.Linear` 的完整树。本步骤无需运行代码，属于源码阅读型实践。

#### 4.1.5 小练习与答案

**练习 1**：`LlamaConfig.__post_init__` 里，如果 config.json 同时没有 `context_window_size`、`max_position_embeddings`、`max_sequence_length`，会发生什么？

**答案**：会抛出 `ValueError`（[L72-L76](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L72-L76)），因为无法确定最大序列长度，建图无法继续。

**练习 2**：`position_embedding_base`、`rope_theta`、`self.rope_theta` 三者是什么关系？

**答案**：`rope_theta` 是 HF config.json 里的原始键名；若没显式给 `position_embedding_base`，`__post_init__` 会把 `kwargs["rope_theta"]` 赋给 `position_embedding_base`（[L47-L51](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L47-L51)）；顶层模块再把 `config.position_embedding_base` 存成 `self.rope_theta`（[L260](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L260)），供 `create_paged_kv_cache` 使用。三个名字指的是同一个 RoPE 基频。

### 4.2 注意力与 FFN 层

#### 4.2.1 概念说明

Llama 的两个核心子层都做了一些「工程优化」，MLC 把它们写得很紧凑：

- **注意力层（LlamaAttention）**：
  - **QKV 融合**：把 Q、K、V 三个投影合并成一个 `qkv_proj`，一次矩阵乘搞定，省访存、省 kernel 启动。
  - **GQA（分组查询注意力）**：当 `num_key_value_heads < num_attention_heads` 时，多组 Q 共享一组 K/V，减少 KV 缓存量。
  - **RoPE + Attention 融合进 KV 缓存**：模型代码里**看不到**显式的 RoPE 旋转，因为它被融合进了 `paged_kv_cache.attention_with_fused_qkv(...)` 这个算子。
- **FFN 层（LlamaFFN）**：Llama 用 **SwiGLU** 激活，需要两个「升维」投影（gate、up）和一个「降维」投影（down）。MLC 把 gate 和 up 合并成一个 `gate_up_proj`，输出通道是 `2 * intermediate_size`，算完再 split 成两半。

#### 4.2.2 核心流程

注意力前向（伪代码）：

```
def LlamaAttention.forward(hidden_states, paged_kv_cache, layer_id):
    qkv = qkv_proj(hidden_states)                 # [b, s, (h_q + 2*h_kv) * d]
    qkv = reshape(qkv, [b, s, h_q + h_kv + h_kv, d])
    out = paged_kv_cache.attention_with_fused_qkv( # RoPE + attn + reshape 全在这
              layer_id, qkv, num_q_heads, sm_scale = head_dim ** -0.5)
    out = reshape(out, [b, s, h_q * d])
    return o_proj(out)
```

FFN 前向（伪代码）：

```
def LlamaFFN.forward(x):
    concat = gate_up_proj(x)          # [.., 2*intermediate]
    x1, x2 = split(concat, 2, axis=-1)
    return down_proj( silu(x1) * x2 ) # SwiGLU: silu(gate) * up
```

注意 `sm_scale`（softmax 缩放）取 \(\text{head\_dim}^{-0.5}\)，即 \(\frac{1}{\sqrt{d}}\)，这是标准注意力的温度系数。

#### 4.2.3 源码精读

注意力层：

[LlamaAttention：L139-L170](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L139-L170) — 构造时按张量并行把头数除以 `tensor_parallel_shards`（[L142](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L142)、[L149](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L149)）；`qkv_proj` 输出通道是 `(num_q_heads + 2*num_kv_heads) * head_dim`，正是「Q + K + V」三段拼接（[L150-L154](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L150-L154)）。`forward` 里 QKV 投影 → reshape 成 `[b, s, h_total, d]` → 交给 `attention_with_fused_qkv`（RoPE、attention 都在里面）→ `o_proj`。**整个 attention 模块自身不出现 RoPE 调用**，RoPE 参数是在 `create_paged_kv_cache` 时塞进缓存对象的。

FFN 层：

[LlamaFFN：L106-L125](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L106-L125) — `gate_up_proj` 输出 `2 * intermediate_size`（gate、up 合并），`forward` 用 `op.split` 切成 `x1, x2`，再 `down_proj(op.silu(x1) * x2)` 实现 SwiGLU。`intermediate_size` 会先除以 `tensor_parallel_shards`（[L114](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L114)）。

Embedding 与「共享 lm_head」：

[LlamaEmbedding.lm_head_forward：L128-L136](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L128-L136) — 当 `tie_word_embeddings=True` 时，不另建 `lm_head`，而是直接把 embedding 权重转置后做矩阵乘（`permute_dims` + `matmul`，`out_dtype="float32"`），实现权重共享。

Decoder 层把 norm + attn + 残差 + norm + FFN + 残差 串起来，并给每个权重贴上「张量并行分片策略」：

[LlamaDecoderLayer：L173-L214](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L173-L214) — `forward` 是经典的「pre-norm + 残差」结构：`hidden = residual + attn(norm(hidden))`，再 `hidden = residual + mlp(norm(hidden))`。`_set_tp()` 用 `tp.ShardSingleDim(...)` 给 qkv_proj/o_proj/gate_up_proj/down_proj 标注沿哪个维度切分（[L181-L199](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L181-L199)）。注意 `qkv_proj` 用了 `segs=[q, k, v]`，因为 Q/K/V 三段长度不同，要分别均匀切；残差在多卡时用 `op.ccl_allreduce(out, "sum")` 做跨卡归约（[L211-L214](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L211-L214)）。

#### 4.2.4 代码实践（源码阅读型）

实践目标：理解「RoPE 不在 attention 模块里，而在 KV 缓存里」这一设计。

操作步骤：

1. 在 [LlamaAttention.forward](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L157-L170) 里搜索 `rope`、`rotate`、`sin`、`cos` 等关键字。
2. 你会发现**搜不到**——RoPE 的确不在这一层。
3. 再去看 4.3 节的 `create_paged_kv_cache`，确认 `rope_theta`、`rope_scaling` 是在那里传给 `PagedKVCache.create_generic` 的。
4. 对照 [python/mlc_llm/nn/kv_cache.py:13-L75](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/nn/kv_cache.py#L13-L75)，看 `attention_with_fused_qkv` 所属的 `PagedKVCache` 是怎么把 RoPE 配置吃进去的。

需要观察的现象：模型定义层「只写线性代数」，所有「位置编码 + 注意力核」都被收编进 `PagedKVCache` 这个对象。这是为了让编译器（u7/u8 会讲）能把 RoPE、attention、KV 写回融合成单一高效 kernel。

预期结果：能用自己的话说出「Llama 的 RoPE 在哪儿」——它存在于 KV 缓存的创建参数里，由 `attention_with_fused_qkv` 在运行时施加。本步骤属于源码阅读型实践，无需运行命令（待本地验证：若你想确认编译后 RoPE 真的被融合，可结合 u8 的融合 pass 进一步阅读）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `qkv_proj` 的输出通道是 `(num_q_heads + 2 * num_kv_heads) * head_dim`，而不是 `3 * num_q_heads * head_dim`？

**答案**：因为 Llama 支持 GQA（`num_kv_heads` 可能小于 `num_q_heads`），K 和 V 的头数比 Q 少，所以是「Q 头数 + 2 × KV 头数」。

**练习 2**：`sm_scale=self.head_dim**-0.5` 等价于哪个公式？

**答案**：等价于 \(\frac{1}{\sqrt{d}}\)，即注意力 softmax 前对 \(QK^{\top}\) 的缩放系数，\(d\) 就是 `head_dim`。

### 4.3 执行入口与 spec 契约

#### 4.3.1 概念说明

写完网络结构后，还要告诉编译器和推理引擎「这个模型有哪些可调用的函数、每个函数接受什么张量」。这部分由两层组成：

- **执行入口方法**：顶层 `LlamaForCausalLM` 上的一组方法，对应推理的各个阶段。最重要的是：
  - `embed(input_ids)`：token id → embedding 向量。
  - `prefill(input_embed, paged_kv_cache)`：处理 prompt，返回**最后一个 token** 的 logits。
  - `decode(input_embed, paged_kv_cache)`：单步解码，返回下一个 token 的 logits。
  - `batch_prefill / batch_decode / batch_verify`：批量版本（多个序列同时算）。
  - `create_paged_kv_cache(...)`：在开始时创建 KV 缓存对象。
  - `get_logits(hidden_states)`：把隐状态映射到词表 logits（lm_head）。
- **`get_default_spec()`**：一份「张量接口契约」。它声明每个入口方法的输入张量形状（含符号维度）、数据类型、`param_mode`（是否打包权重进函数）和 `effect_mode`。编译时 TVM 据此为每个方法生成一个 IR 函数。

#### 4.3.2 核心流程

模型被编译的链路（与 u7 编译接口衔接）：

```
LlamaForCausalLM(config)                      # 1. 构造模型
   .quantize[kind](...)  → 量化后的 model      # 2. 量化改图（u5 讲）
   .export_tvm(spec=model.get_default_spec())  # 3. 用 spec 把每个方法导出成 Relax IR
        → IRModule + named_params              #    （compile.py:163-166）
   ↓ TVM pass 流水线（u7/u8 讲）
   → 模型库 (.so / .tar / .wasm)
```

关键点：`get_default_spec()` 返回的 spec 决定了「会导出哪些函数、每个函数的入参长什么样」。运行期 C++ 引擎（u9 讲）就是按这些函数名去 model lib 里查找并调用的。

#### 4.3.3 源码精读

先看四个核心入口方法：

[embed / get_logits：L312-L325](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L312-L325) — `embed` 把 token id 交给 embedding 表（多卡时先广播 input_ids）；`get_logits` 根据 `tie_word_embeddings` 走共享权重或独立 `lm_head`，并强制把 logits 转成 `float32`（采样精度）。

[prefill / decode：L334-L347](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L334-L347) — 两者都先跑 `self.model(...)` 得到隐状态。**区别在于**：`prefill` 额外调用 `index_last_token`（[L338](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L338)）只取最后一个位置的隐状态，因为 prefill 阶段我们只关心「下一个 token」；`decode` 不需要，因为它的序列长度本来就是 1。

`index_last_token` 是个共享小工具，用 TVM 的 tensor 表达式把 `[b, s, d]` 切成 `[b, 1, d]`（取 `s-1` 位置）：

[model_utils.index_last_token：L7-L14](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/model_utils.py#L7-L14) — 注释说明它「保留历史 `index` 算子的形状/命名」，通过 `op.tensor_expr_op` 把一个 `te.compute` 包进 Relax。

批量三件套 `batch_prefill / batch_decode / batch_verify` 都委托给 `batch_forward`：

[batch_forward 与 batch_*：L287-L376](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L287-L376) — `batch_forward` 支持 `logit_positions`（只算指定位置的 logits，省计算），`batch_decode` / `batch_verify` 的入参形状不同（见 spec），但计算路径相同，所以都复用 `batch_forward`。`batch_verify` 用于推测解码的校验阶段。

再看 KV 缓存的创建——**RoPE 配置就是从这里传进去的**：

[create_paged_kv_cache：L396-L423](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L396-L423) — 调用 `PagedKVCache.create_generic(...)`，把以下信息打包：
- 结构信息：`attn_kind="mha"`、`num_hidden_layers`、按张量并行缩小后的 `num_attention_heads` / `num_key_value_heads`、`qk_head_dim` / `v_head_dim`；
- **RoPE 信息**：`rope_mode=RopeMode.NORMAL`、`rope_scale=1`、`rope_theta=self.rope_theta`（即 `position_embedding_base`）、`rope_scaling=self.rope_scaling`（llama3 的缩放配置）；
- 其它：`layer_partition`（流水线分段）、`enable_disaggregation`、`dtype`。

也就是说，**RoPE 的基频 `rope_theta` 和缩放策略 `rope_scaling` 是在「创建 KV 缓存」时一次性写死的**，之后每次 `attention_with_fused_qkv` 都按这套配置施加旋转。`create_generic` 内部最终发出一个 `mlc.create_paged_kv_cache_generic` 的 Relax 调用（[kv_cache.py:55-L66](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/nn/kv_cache.py#L55-L66)），这个调用会在编译流水线里被改写成具体的缓存实现（u8 的 `dispatch_kv_cache_creation` 讲）。

最后看 spec 契约本身：

[get_default_spec：L425-L542](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L425-L542) — 返回一个 `ModuleSpec`，为每个入口方法声明入参。以 `prefill` 为例（[L449-L456](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L449-L456)）：`input_embed` 形状是 `[1, "seq_len", hidden_size]`（`seq_len` 是符号维度），`paged_kv_cache` 用 `nn.spec.Object(PagedKVCache)` 表示一个对象参数；`$` 里的 `param_mode: "packed"` 表示该方法会把模型权重打包传入，`effect_mode: "none"` 表示无副作用状态。`decode` 的 `input_embed` 形状则是 `[1, 1, hidden_size]`（[L457-L463](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L457-L463)）——这正是「decode 每步只喂一个 token」的体现。`create_paged_kv_cache` 的五个入参直接是 `int`（[L530-L540](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L530-L540)）。

这份 spec 在编译主流程里被这样消费：

[compile.py 导出模型：L163-L166](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L163-L166) — `model.export_tvm(spec=model.get_default_spec(), allow_extern=True)` 一行，依据 spec 把 `prefill`、`decode`、`batch_verify`、`create_paged_kv_cache` 等方法各自翻译成一个 Relax 函数，拼成 IRModule 交给后续 pass 流水线。

#### 4.3.4 代码实践（本讲指定实践）

实践目标：熟练定位 `LlamaForCausalLM` 的四个核心方法，理解它们的输入输出，并说清 RoPE 配置从何而来。

操作步骤：

1. 打开 [llama_model.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py)，定位以下四个方法并各用一句话写出「输入 → 输出」：
   - `prefill`（[L334-L340](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L334-L340)）
   - `decode`（[L342-L347](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L342-L347)）
   - `create_paged_kv_cache`（[L396-L423](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L396-L423)）
   - `get_logits`（[L317-L325](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L317-L325)）
2. 针对 RoPE：在 `create_paged_kv_cache` 里找到传给 `PagedKVCache.create_generic` 的四个 RoPE 相关参数 `rope_mode`、`rope_scale`、`rope_theta`、`rope_scaling`（[L416-L419](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L416-L419)），并回溯它们的来源：
   - `self.rope_theta` ← `config.position_embedding_base` ← `__post_init__` 里的 `rope_theta`（[L260](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L260) 与 [L47-L51](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L47-L51)）；
   - `self.rope_scaling` ← `config.rope_scaling`（[L259](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L259)）。

参考答案（建议你先自己写再对照）：

- **`prefill`**：输入 prompt 的 embedding（`[1, seq_len, hidden]`）与一个 `PagedKVCache`；输出该 prompt 最后一个 token 的 logits（`[1, 1, vocab]`）和原样返回的缓存。
- **`decode`**：输入上一步生成 token 的 embedding（`[1, 1, hidden]`）与缓存；输出下一个 token 的 logits 和缓存。
- **`create_paged_kv_cache`**：输入五个 `int`（`max_batch_size` / `max_total_seq_len` / `prefill_chunk_size` / `page_size` / `support_sliding_window`，注意源码签名是 `tirx.Var`，编译期由引擎给定）；输出一个配置好 RoPE 与结构参数的 `PagedKVCache` 对象。
- **`get_logits`**：输入隐状态（`[..., hidden]`）；输出强制为 `float32` 的词表 logits（`[..., vocab]`）。
- **RoPE 如何传入**：`create_paged_kv_cache` 把 `rope_theta`（= `position_embedding_base`，源自 HF 的 `rope_theta`）和 `rope_scaling`（llama3 缩放）作为参数传给 `PagedKVCache.create_generic`，缓存在创建时就「记住」了 RoPE 配置；之后 `attention_with_fused_qkv` 在每次注意力计算时据此施加旋转。模型层不显式调用 RoPE。

需要观察的现象：`prefill` 与 `decode` 的计算主体几乎一样（都调 `self.model` 再 `get_logits`），唯一差别是 `prefill` 多了 `index_last_token`；`batch_decode` 与 `batch_verify` 也共用 `batch_forward`，差别仅在入参形状（见 spec）。

预期结果：能不依赖代码说出四个方法的输入输出，并解释 RoPE 参数的「来源链」。本步骤属于源码阅读型实践（待本地验证：若想运行，可在 u2-l3 的 chat CLI 流程中加日志观察 prefill 与 decode 的调用顺序与 token 数）。

#### 4.3.5 小练习与答案

**练习 1**：在 `get_default_spec` 里，为什么 `prefill` 的 `input_embed` 形状是 `[1, "seq_len", hidden_size]`，而 `decode` 是 `[1, 1, hidden_size]`？

**答案**：prefill 一次处理整段 prompt，序列长度可变（用符号维度 `"seq_len"`）；decode 每步只生成一个 token，序列长度恒为 1，所以写死 `1`。这直接对应两个阶段的语义差异。

**练习 2**：`get_default_spec` 里大部分方法标注 `param_mode: "packed"`，但 `create_paged_kv_cache` 和 `batch_select_last_hidden_states` 标的是 `"none"`，为什么？

**答案**：`create_paged_kv_cache` 只是按结构参数构造一个缓存对象，不依赖模型权重；`batch_select_last_hidden_states` 只做索引切片，也不需要权重。所以它们不需要把权重打包进函数，标 `"none"`。

**练习 3**：如果你要在 Llama 上加一种新的执行阶段（比如「只跑前几层做早期退出」），需要在模型里动哪些地方？

**答案**：至少三处：① 在 `LlamaForCausalLM` 上新增一个方法实现该阶段逻辑；② 在 `get_default_spec` 里为该方法补一条 spec（声明入参形状/类型/`param_mode`）；③ 确保运行期引擎（u9 的 C++ `Model` 接口）能从 model lib 里查到并调用这个新函数。这样编译期才会为它生成对应的 Relax 函数。

## 5. 综合实践

**任务：给 Llama 模型画一张「从 config.json 到可调用函数」的完整流转图，并用一段话向同伴解释。**

把本讲三个模块串起来：

1. **第一层（数据流入）**：从 HF `config.json` 出发，画出 `LlamaConfig.__post_init__` 如何把原始字段加工成 `head_dim` / `context_window_size` / `rope_theta` 等派生字段（参考 4.1）。
2. **第二层（结构组合）**：画出 `LlamaForCausalLM` → `LlamaModel` → `LlamaDecoderLayer` → (`LlamaAttention` + `LlamaFFN`) 的组合树，并在注意力节点旁标注「QKV 融合 + RoPE 交给 KV 缓存」（参考 4.2）。
3. **第三层（对外契约）**：画出 `get_default_spec` 列出的方法清单（`embed` / `prefill` / `decode` / `batch_verify` / `create_paged_kv_cache` / `get_logits`），并标出每个方法在 [compile.py:163-L166](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L163-L166) 被 `export_tvm` 翻译成 Relax 函数的箭头（参考 4.3）。

验收标准：

- 图上能回答这三个问题：① RoPE 的基频从哪个字段一路传到 KV 缓存？② prefill 和 decode 的代码几乎一样，差别在哪一行？③ 为什么模型里搜不到 `rope` 却依然有位置编码？
- 能指出至少两处「张量并行」的痕迹（`num_q_heads // tensor_parallel_shards`、`ShardSingleDim`、`ccl_allreduce`）。

本任务为源码阅读型，无需运行；若想动手，可在 u2-l2 的 `compile` 流程中加 `logging`，验证这些方法确实被导出。

## 6. 本讲小结

- MLC 模型用 Relax nn「类 PyTorch」地定义：`XxxConfig`（读 config.json + 派生校验）+ 一组层层嵌套的 `nn.Module`，顶层是 `LlamaForCausalLM`。
- 注意力层做了 **QKV 融合**（单个 `qkv_proj`）与 **GQA** 支持；FFN 层做了 **gate/up 融合** 的 SwiGLU；两者的张量并行分片用 `tp.ShardSingleDim` 标注。
- **RoPE 不在 attention 模块里显式出现**，而是在 `create_paged_kv_cache` 时把 `rope_theta` / `rope_scaling` 写进 `PagedKVCache`，再由 `attention_with_fused_qkv` 施加——这样编译器才能把 RoPE+attention+KV 融成单个 kernel。
- 执行入口是一组「阶段方法」：`embed` / `get_logits` / `prefill`（取最后 token）/ `decode`（单步）/ `batch_prefill` / `batch_decode` / `batch_verify`（推测解码校验）/ `create_paged_kv_cache`。
- `get_default_spec()` 是给编译器的**张量接口契约**，声明每个方法的形状/类型/`param_mode`，在 `compile.py` 的 `export_tvm(spec=...)` 处被消费，决定了会导出哪些 Relax 函数。
- 上一讲的 `MODELS["llama"].model = LlamaForCausalLM` 由此落地：拿到这个构造器，就有了配置、建图、阶段方法、spec 契约的完整闭环。

## 7. 下一步学习建议

- **横向对比其他模型**：阅读 [python/mlc_llm/model/](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/) 下其它架构（如 `mistral`、`qwen`、`gemma`），你会发现它们与 Llama 的骨架几乎一致，差异主要在注意力变体、激活函数和归一化层——这是巩固本讲最好的方式。
- **向下接 u5（量化）**：本讲的 `nn.Linear` / `nn.Embedding` 在量化阶段会被 visitor 替换成 `GroupQuantizeLinear` 等，理解了模型结构才能看懂量化改图。
- **向下接 u4（权重加载）**：模型里的参数名（`qkv_proj`、`gate_up_proj`）正是 `ExternMapping` 要做改名映射的对象——本讲的「融合投影」直接决定了权重映射为什么要做 QKV 拼接、gate/up 拼接。
- **向下接 u7（编译接口）**：去看 `compile.py` 里 `export_tvm` 之后 `_apply_preproc_to_params_and_check_pipeline` 如何处理这些 spec 导出的函数与参数，把「模型定义 → 编译产物」的链路补全。
