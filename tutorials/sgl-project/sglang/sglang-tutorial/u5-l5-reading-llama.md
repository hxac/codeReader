# 精读一个模型：LlamaForCausalLM

## 1. 本讲目标

前面几讲我们分别看过了「前向执行路径」（u5-l1）、注意力后端的「可插拔机制」（u5-l3）和「权重加载」（u5-l4）。这些组件最终都要装进一个**具体的模型实现**里才会被真正调用。本讲就把这些散落的知识串起来，挑 SGLang 里最经典、被复用最广的一个模型实现——`LlamaForCausalLM`——做一次端到端精读。

读完本讲，你应该能够：

1. 说清一个 SGLang 模型由哪些层组件构成（Embedding、DecoderLayer、Attention、MLP、LM Head），以及它们如何拼成一次前向。
2. 看懂 `LlamaAttention` 里 `RadixAttention` **替换**了 HuggingFace（下文简称 HF）原生 `SelfAttention` 的哪一段逻辑，又保留了哪一段。
3. 理解 SGLang 模型现在通过 `get_parallel()` 访问器读取并行配置（如 `tp_size`、`enable_dp_lm_head`），并知道这种读法背后的机制。
4. 能对照 HF transformers 的 Llama 实现，读懂 SGLang 版本的差异，并能定位模型是如何被 `registry` 注册与发现的。

## 2. 前置知识

在进入源码前，先用通俗语言把几个关键概念过一遍。

**Transformer 解码器（decoder-only）模型。** Llama 系列是 decoder-only 架构：输入一串 token id，先做词嵌入（Embedding）变成向量，再依次穿过 N 个「解码层（DecoderLayer）」，最后用一个 LM Head 把隐藏状态映射回词表概率。每个解码层内部包含一个「自注意力（Self-Attention）」子层和一个「前馈（MLP）」子层，各自配一个 RMSNorm 与残差连接。

**GQA（Grouped-Query Attention）。** 早期注意力里 Query/K/V 的头数相同（MHA）；为了省显存，Llama-2 70B 之后通常让 K/V 的头数（`num_kv_heads`）少于 Query 的头数（`num_heads`），多个 Query 头共用一组 K/V，这就是 GQA。极端情况下 `num_kv_heads=1` 就是 MQA。

**张量并行（Tensor Parallelism, TP）。** 把同一层的权重矩阵沿某个维度切到多张 GPU 上：`qkv_proj`/`gate_up_proj` 按列切（ColumnParallel），`o_proj`/`down_proj` 按行切（RowParallel）并在输出处做一次 all-reduce。这样每张卡只算一部分，最后拼回完整结果。本讲会看到 SGLang 用 `QKVParallelLinear`/`MergedColumnParallelLinear`/`RowParallelLinear` 表达这些切分。

**Paged KV 缓存与前缀缓存。** HF 的注意力把 K/V 存在 `past_key_values` 里，由模型自己管；SGLang 不让模型管 KV，而是由调度器外部的内存池（见 u4-l2）按「物理槽」管理，并把「这一批新 token 应该写到哪些物理槽」通过 `forward_batch.out_cache_loc` 告诉模型。`RadixAttention` 正是连接「注意力计算」和「这套 paged/前缀缓存体系」的那层壳（见 u4-l1）。

**RuntimeContext 与命名空间访问器。** 自配置体系重构后，运行期配置被快照成若干「命名空间袋（config bag）」（见 u2-l5）。模型里读并行拓扑/并行配置，统一走 `get_parallel()` 返回的 `ParallelContext`，而不是直接读 `ServerArgs`。本讲会专门讲清这条访问路径。

> 承接提示：本讲依赖 u5-l3（注意力后端如何被选择）与 u5-l4（权重如何被加载对齐）。如果你还没读这两讲，建议先浏览它们的结论再回来。

## 3. 本讲源码地图

本讲涉及三个核心文件：

| 文件 | 作用 |
| --- | --- |
| [python/sglang/srt/models/llama.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py) | Llama 模型实现。定义 `LlamaMLP`/`LlamaAttention`/`LlamaDecoderLayer`/`LlamaModel`/`LlamaForCausalLM` 五个类，是本讲主角。 |
| [python/sglang/srt/layers/radix_attention.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/layers/radix_attention.py) | `RadixAttention` 的定义。它是替换 HF 原生注意力核心计算的那一层壳，并桥接到 paged KV 缓存与注意力后端。 |
| [python/sglang/srt/models/registry.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/registry.py) | 模型注册表。负责扫描 `models/` 下所有模块、把架构名（如 `LlamaForCausalLM`）映射到模型类。 |
| [python/sglang/srt/runtime_context.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py) | `ParallelContext` 与 `get_parallel()` 所在文件，解释模型里并行配置的读法。 |

## 4. 核心概念与源码讲解

### 4.1 模型骨架：LlamaForCausalLM 与 LlamaModel

#### 4.1.1 概念说明

一个 SGLang 模型分两层：

- 外层 `LlamaForCausalLM`：对应 HF 的 `LlamaForCausalLM`，负责持有 LM Head、`LogitsProcessor`、`Pooler`，并提供 `forward` 与 `load_weights` 等顶层接口。
- 内层 `LlamaModel`：对应 HF 的 `LlamaModel`，负责 Embedding、一摞解码层 `self.layers`、最终 RMSNorm `self.norm`。

这种「外壳 + 主干」的拆分和 HF 完全一致，方便我们做对照阅读。`LlamaForCausalLM` 是**入口类**：`ModelRunner`（见 u5-l1）持有的 `model` 对象就是它的实例，调度器算一次前向时调用的就是它的 `forward`。

#### 4.1.2 核心流程

一次前向 `LlamaForCausalLM.forward(input_ids, positions, forward_batch)` 的流程用伪代码表示：

```
hidden_states = self.model(input_ids, positions, forward_batch, ...)   # 走主干
if 是最后一张 PP 卡:
    return self.logits_processor(input_ids, hidden_states, self.lm_head, forward_batch)
else:
    return hidden_states   # 流水线中间卡把隐藏状态交给下一阶段
```

主干 `LlamaModel.forward` 内部：

```
if 是第一张 PP 卡:
    hidden_states = embed_tokens(input_ids)   # 词嵌入
for i in [start_layer, end_layer):            # 只跑分配给本卡的那几层
    hidden_states, residual = layers[i](positions, hidden_states, forward_batch, residual)
if 是最后一张 PP 卡:
    hidden_states = norm(hidden_states, residual)   # 最终 RMSNorm
return hidden_states
```

注意两个细节：一是**流水线并行（PP）**的存在——本卡不一定拥有全部层，`start_layer`/`end_layer` 圈出本卡负责的层段，非本卡的层用 `PPMissingLayer` 占位（见下文）；二是残差连接被做进了 RMSNorm 的两参数重载 `norm(hidden_states, residual)` 里（融合 Add+RMSNorm），所以你看到的不是 `hidden_states + residual`。

#### 4.1.3 源码精读

入口类的构造，先把主干、LM Head、logits 处理器搭起来：

[llama.py:509-543](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L509-L543) 中文说明：`LlamaForCausalLM.__init__` 构造模型。其中 `self.model` 是主干（`LlamaModel`）；`tie_word_embeddings` 为真时（如 Llama-3.2 1B）`lm_head` 直接复用 embedding 权重以省显存，否则建独立的 `ParallelLMHead`。这里出现了本讲的重点之一——`get_parallel().enable_dp_lm_head`（第 530 行），它决定 LM Head 是否使用 DP 注意力组（数据并行下让每张卡的 lm_head 独立），稍后在 4.3 节详解。

顶层前向把主干结果交给 logits 处理器：

[llama.py:553-587](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L553-L587) 中文说明：`LlamaForCausalLM.forward`。先调主干拿 `hidden_states`；若是最后一张 PP 卡且不是 embedding 任务，就用 `self.logits_processor` 把隐藏状态过 `lm_head` 算出 logits（封装进 `LogitsProcessorOutput`），否则走 `pooler` 返回句向量。

主干的构造与 PP 分层：

[llama.py:390-402](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L390-L402) 中文说明：`LlamaModel.__init__` 用 `make_layers` 按层下标 lambda 构造一摞 `LlamaDecoderLayer`，并把 PP 的 `pp_rank`/`pp_size` 传进去做层段切分，返回 `layers` 列表与 `start_layer`/`end_layer`。

[llama.py:375-383](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L375-L383) 中文说明：只有第一张 PP 卡才真正建 `embed_tokens`（`VocabParallelEmbedding`），其余卡用 `PPMissingLayer()` 占位以省显存。

主干前向的层循环：

[llama.py:431-456](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L431-L456) 中文说明：`LlamaModel.forward` 的核心循环。只遍历 `[start_layer, end_layer)` 范围的层；最后一卡在循环后做最终 `norm`，非最后一卡则把 `hidden_states`/`residual` 打包成 `PPProxyTensors` 交给下一阶段。

#### 4.1.4 代码实践

**实践目标：** 在源码层面画出一次前向的调用链，确认「外壳 → 主干 → 解码层」的层次关系。

**操作步骤：**

1. 打开 [llama.py:553](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L553)，找到 `LlamaForCausalLM.forward`。
2. 跟进 `self.model(...)`，定位到 [llama.py:410](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L410) 的 `LlamaModel.forward`。
3. 在 `LlamaModel.forward` 中找到层循环 `for i in range(self.start_layer, self.end_layer)`，确认它调用 `self.layers[i](...)`，即 `LlamaDecoderLayer`。
4. 在 [llama.py:630-636](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L630-L636) 注意 `start_layer`/`end_layer` 是 `@property`，透传自 `self.model.start_layer`。

**需要观察的现象：** 调用链 `LlamaForCausalLM.forward → LlamaModel.forward → LlamaDecoderLayer.forward`；且非首卡/非尾卡分别走 `PPMissingLayer` 与 `PPProxyTensors` 路径。

**预期结果：** 你应当能在脑中画出一张「Embedding → N×DecoderLayer → Norm → LM Head/Pooler」的纵向流水图，并标注 PP 切分点。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 `embed_tokens` 在非首 PP 卡上要换成 `PPMissingLayer`，而不是直接不建？

**参考答案：** 因为权重加载时用的是「按名字对齐」的机制（`named_parameters()`），且 PP 各卡会从同一份检查点里过滤出自己负责的层（见 [llama.py:742-745](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L742-L745) 的 `filter_pp_weights`）。`PPMissingLayer` 让模块结构在不同卡上保持一致，避免因模块缺失而触发 `named_parameters` 找不到名字或 TP 通信对不齐的问题，同时不占显存。

**练习 2：** `tie_word_embeddings=True` 时，`lm_head` 是怎么来的？

**参考答案：** 直接让 `self.lm_head = self.model.embed_tokens`（[llama.py:522-523](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L522-L523)），即共享输入嵌入矩阵作为输出投影，省一份 `[vocab, hidden]` 的权重；加载时则跳过独立的 `lm_head.weight`（见 [llama.py:697-698](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L697-L698)）。

### 4.2 解码层与 LlamaAttention：RadixAttention 替换了什么

#### 4.2.1 概念说明

`LlamaDecoderLayer` 是积木块：它把「自注意力子层 + MLP 子层」用残差与 RMSNorm 串起来，结构和 HF 一致。真正值得精读的是 `LlamaAttention`——这里藏着 SGLang 与 HF 最大的差异。

HF 的 `LlamaAttention.forward` 大致是：

```
q = q_proj(hidden); k = k_proj(hidden); v = v_proj(hidden)   # 三个独立投影
q, k = rotary_emb(q, k)                                      # 旋转位置编码
attn = sdpa(q, k, v, past_kv)                                # 标准注意力 + 自管 KV 缓存
output = o_proj(attn)
```

SGLang 的 `LlamaAttention.forward` 是：

```
qkv = qkv_proj(hidden); q,k,v = split(qkv)                  # 融合投影 + TP 切分
q, k = rotary_emb(positions, q, k)                           # 旋转位置编码
attn = self.attn(q, k, v, forward_batch)                     # ← RadixAttention 接管
output = o_proj(attn)
```

关键区别在第三步：HF 用 `sdpa`（PyTorch 内置的纯计算）并自己管 KV；SGLang 把「注意力核心计算 + KV 缓存读写」整体交给一个 `RadixAttention` 实例 `self.attn`。换句话说，**`RadixAttention` 替换的是 HF 里 `sdpa` 这一段（Q·Kᵀ 缩放 → softmax → 与 V 相乘得到 context），以及与之绑定的 KV 缓存管理；它没有替换 q/k/v 投影、RoPE、o_proj。**

为什么要这样切？因为 SGLang 的 KV 缓存由调度器外部的 paged 内存池统一管理（u4-l2），还需要支持前缀缓存（u4-l1）和可插拔注意力后端（u5-l3）。把这些事从模型里抽出来交给 `RadixAttention`，模型代码就只关心「算 Q/K/V、施加 RoPE、做投影」，与具体后端（FlashInfer / FlashAttention / Triton / TRT-LLM 等）彻底解耦。

#### 4.2.2 核心流程

注意力单步的缩放原理（仅供回顾，本讲不展开数值计算）：

\[
\text{Attention}(Q,K,V) = \text{softmax}\!\left(\frac{QK^{\top}}{\sqrt{d_k}}\right) V
\]

其中缩放因子 \(\sqrt{d_k}\) 对应代码里的 `scaling = head_dim ** -0.5`。LlamaAttention 在构造时把 `scaling` 传给 `RadixAttention`，后端真正算注意力时再用它。

`LlamaAttention` 构造期的职责拆成四块：

1. **头数与 GQA 推导**：根据全局 `tp_size` 把 `total_num_heads`/`total_num_kv_heads` 折算成本卡负责的 `num_heads`/`num_kv_heads`。
2. **投影层**：`qkv_proj`（融合的 QKV，ColumnParallel）、`o_proj`（RowParallel）。
3. **位置编码**：`rotary_emb = get_rope(...)`。
4. **注意力壳**：`self.attn = RadixAttention(num_heads, head_dim, scaling, num_kv_heads=..., layer_id=...)`。

`LlamaDecoderLayer.forward` 的残差编排则把 RMSNorm 融合进来：

```
if residual is None:                          # 第一层
    residual = hidden_states
    hidden_states = input_layernorm(hidden_states)
else:
    hidden_states, residual = input_layernorm(hidden_states, residual)   # 融合 Add+RMSNorm
hidden_states = self_attn(positions, hidden_states, forward_batch)
hidden_states, residual = post_attention_layernorm(hidden_states, residual)
hidden_states = mlp(hidden_states)
```

#### 4.2.3 源码精读

注意力的头数推导与本卡切分（也是 `get_parallel().tp_size` 的第一个使用点）：

[llama.py:155-168](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L155-L168) 中文说明：用全局张量并行大小 `tp_size` 折算本卡的 `num_heads` 与 `num_kv_heads`。当 KV 头数 ≥ TP 时按 TP 切分 KV 头；当 KV 头数 < TP 时（GQA/MQA）则把 KV 头复制到多卡上。这段逻辑决定了后续 K/V 的形状与通信量。

融合投影与输出投影：

[llama.py:181-196](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L181-L196) 中文说明：`qkv_proj` 用 `QKVParallelLinear` 把 q/k/v 三个投影融合成一个权重并按列切分（TP 友好）；`o_proj` 用 `RowParallelLinear` 按行切分并在输出处 all-reduce。这与 HF 的三个独立 `q_proj/k_proj/v_proj` 是最直观的差异。

实例化 RadixAttention（替换 HF 的 sdpa）：

[llama.py:206-214](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L206-L214) 中文说明：把注意力核心计算 + KV 缓存读写封装成 `self.attn = RadixAttention(...)`。注意传入的是本卡折算后的 `num_heads`/`num_kv_heads`、缩放 `scaling`、以及 `layer_id`（后端靠它找到该层在全局注意力表里的位置）。

前向里 Q/K/V 的准备（投影 + 拆分 + RoPE）：

[llama.py:216-220](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L216-L220) 中文说明：`forward_prepare_native` 做融合投影、按 `q_size/kv_size` 拆出 q/k/v，并对 q/k 施加旋转位置编码。注意 **v 不施加 RoPE**，且 RoPE 发生在交给 `RadixAttention` 之前——所以 `RadixAttention` 收到的是已经旋转好的 q、k。

把核心计算交给 RadixAttention：

[llama.py:259-261](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L259-L261) 中文说明：`attn_output = self.attn(q, k, v, forward_batch)`，再把结果过 `o_proj`。这一行就是「RadixAttention 替换 HF sdpa」的落点。

解码层的残差与子层编排：

[llama.py:345-360](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L345-L360) 中文说明：`LlamaDecoderLayer.forward`。首层用单参数 RMSNorm，其后用两参数重载（融合 Add+RMSNorm）更新残差；自注意力与 MLP 各自被一层 post-norm 包裹。这套残差写法与 HF 一致，差异只在子层内部。

再看 `RadixAttention.forward` 把计算路由到哪：

[radix_attention.py:143-152](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/layers/radix_attention.py#L143-L152) 中文说明：`RadixAttention.forward` 的签名。它收 q/k/v 与 `forward_batch`，内部根据是否处于 tc-piecewise 编译图分流，但最终都汇到「具体后端」。

[radix_attention.py:271-280](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/layers/radix_attention.py#L271-L280) 中文说明：未进入编译图时的默认分支，直接 `get_attn_backend().forward(q, k, v, self, forward_batch, ...)`。`self`（RadixAttention 实例）携带 `layer_id`/`scaling`/`k_scale` 等元信息，后端据此读写正确的 paged KV 槽位（即 u4-l2 讲的 `out_cache_loc`）。这就是「模型不碰 KV，全交给后端 + forward_batch」的实现。

> 补充：MLP 子层 `LlamaMLP`（[llama.py:67-116](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L67-L116)）同样是「融合 + TP 切分」：HF 的 `gate_proj`/`up_proj` 在 SGLang 合成 `gate_up_proj`（`MergedColumnParallelLinear`），再经 `SiluAndMul` 激活、`down_proj`（`RowParallelLinear`）。这条替换思路与 qkv 融合一致，不再赘述。

#### 4.2.4 代码实践

**实践目标：** 精确标注 `RadixAttention` 替换了 HF 注意力的哪一段，并验证 v 不参与 RoPE。

**操作步骤：**

1. 对照 HF transformers 的 `LlamaAttention`（[vllm 适配来源](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L17-L18) 注释指向的 vLLM 版本即可），列出 `q_proj/k_proj/v_proj/o_proj/rotary_emb` 与 SGLang 的对应关系。
2. 在 [llama.py:216-220](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L216-L220) 确认 `self.rotary_emb(positions, q, k)` 只返回 `q, k`，`v` 原样透传。
3. 在 [llama.py:259](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L259) 确认「注意力核心 + KV 缓存」整体由 `self.attn` 承担。
4. 跟进 [radix_attention.py:271-280](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/layers/radix_attention.py#L271-L280)，确认它最终调用的是「某个注意力后端」的 `forward`，而不是模型内置的 sdpa。

**需要观察的现象：** SGLang 的 `LlamaAttention` 里**找不到**任何 `softmax`、`F.scaled_dot_product_attention`、`past_key_values` 之类的字样——它们都搬进了 `RadixAttention` 与后端。

**预期结果：** 得到一张三列对照表（HF 算子 / SGLang 算子 / 是否被 RadixAttention 接管）。结论应是：投影、RoPE、o_proj **不**被接管；softmax-QKV 核心计算与 KV 缓存读写**被** `RadixAttention` 接管。

> 待本地验证：若你在本地装了 HF transformers，可对比 `transformers.models.llama.modeling_llama.LlamaAttention` 与本文件，确认上面三列对照表。

#### 4.2.5 小练习与答案

**练习 1：** 为什么 `RadixAttention` 需要传入 `layer_id`，而 HF 的 sdpa 不需要？

**参考答案：** 因为 SGLang 的 KV 缓存是全局的 paged 池（`TokenToKVPool`，见 u4-l2），每一层的 K/V 在池里占用独立的槽段；注意力后端按 `layer_id` 索引 `context.attention_layers[layer_id]`（见 [radix_attention.py:306-307](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/layers/radix_attention.py#L306-L307)）才能读写正确那一层的 KV。HF 把 KV 存在模型自己持有的 `past_key_values` 里，天然按层隔离，所以不需要显式 id。

**练习 2：** 一个 GQA 模型 `num_heads=32, num_kv_heads=8`，在 `--tp 4` 下，本卡 `num_heads` 和 `num_kv_heads` 各是多少？

**参考答案：** `num_heads = 32 // 4 = 8`；KV 头数 8 ≥ TP 4 且整除，故 `num_kv_heads = 8 // 4 = 2`（每卡 2 个 KV 头，被 4 个 Q 头共用）。可对照 [llama.py:155-168](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L155-L168) 的推导分支验证。

### 4.3 get_parallel() 并行配置访问

#### 4.3.1 概念说明

这是本次更新最值得留意的变化：`llama.py` 里**所有并行相关配置都不再读 `ServerArgs`/`get_server_args()`**，而是统一走 `get_parallel()`。在本次重构前的 diff 里，第 530 行还是 `get_server_args().enable_dp_lm_head`，现在已改成 `get_parallel().enable_dp_lm_head`，并且 `get_server_args` 已从 import 中彻底移除。

为什么要这样改？因为配置体系重构后（见 u2-l5），运行期状态收敛到一个进程级单例 `RuntimeContext`，其中并行拓扑/并行配置被封装成 `ParallelContext`。模型读并行信息有两类来源，但**对调用方透明地统一在 `get_parallel()` 这一个入口**：

- **活的拓扑事实（live facts）**：`tp_size`、`tp_rank`、`world_size` 等——它们反映「此刻进程组真实是多少」，会随分布式初始化而变，所以是 `@property`，实时查 `parallel_state`。
- **并行配置叶子（config leaves）**：`enable_dp_lm_head`、`nccl_port`、`pp_max_micro_batch_size` 等——它们来自发布（publish）时的并行配置袋，是静态快照，通过 `__getattr__` 兜底返回。

#### 4.3.2 核心流程

`get_parallel()` 的解析规则（在 `ParallelContext` 内部）：

```
attr = 你要读的名字 (如 "tp_size" / "enable_dp_lm_head")
if attr 是某个 @property:        # 活拓扑优先
    return 该 property (实时查 parallel_state)
elif attr 在已发布的 parallel 配置袋里:
    return 袋里的值              # 静态配置叶子
else:
    raise AttributeError
```

也就是说，同名时「活拓扑」胜出（`tp_size` 既是 property 又可能在袋里有同名字段，此时以 property 为准），其余配置叶子走袋。这条「同名即同值」的不变量在分布式初始化完成后成立。

`llama.py` 里共有 **三个** `get_parallel()` 调用点，覆盖了模型对并行信息的全部需求。

#### 4.3.3 源码精读

调用点一：构造注意力时读 `tp_size`（活拓扑）：

[llama.py:155-155](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L155-L155) 中文说明：`tp_size = get_parallel().tp_size`。`tp_size` 是 `ParallelContext` 的 `@property`，实时查 `parallel_state.get_tensor_model_parallel_world_size()`，用来折算本卡头数。

调用点二/三：加载 KV cache 量化缩放时读 `tp_size`/`tp_rank`（活拓扑）：

[llama.py:461-463](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L461-L463) 中文说明：`load_kv_cache_scales` 里同样用 `get_parallel().tp_size` 与 `get_parallel().tp_rank`，把缩放因子按 TP 切片加载到对应层。

调用点四：构造 LM Head 时读 `enable_dp_lm_head`（配置叶子）：

[llama.py:527-531](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L527-L531) 中文说明：`use_attn_tp_group=get_parallel().enable_dp_lm_head`。`enable_dp_lm_head` 不是 property，所以由 `ParallelContext.__getattr__` 从已发布的并行配置袋里取出，决定 LM Head 是否走 DP 注意力组。

回到访问器与解析机制本身：

[runtime_context.py:1007-1008](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py#L1007-L1008) 中文说明：`get_parallel()` 返回进程级单例 `_PARALLEL`（一个 `ParallelContext`）。

[runtime_context.py:160-174](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py#L160-L174) 中文说明：`tp_size`/`tp_rank` 是 `@property`，走 `self._v(...)`（支持 `override` 临时覆盖）查 `parallel_state`。

[runtime_context.py:126-140](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py#L126-L140) 中文说明：`__getattr__` 兜底——凡不是 property 也不是 slot 的名字（如 `enable_dp_lm_head`），就到已发布的并行配置袋里查；查不到就抛 `AttributeError`。

> 关键结论：写新模型时，读 `tp_size`/`tp_rank`/`pp_size` 这类「当前真实并行度」用 `get_parallel().xxx`；读 `enable_dp_lm_head` 这类「并行相关开关」也用 `get_parallel().xxx`。两者入口一致，区别只在内部是查活拓扑还是查配置袋。不要再写 `get_server_args().xxx`。

#### 4.3.4 代码实践

**实践目标：** 在 `llama.py` 里把所有并行配置读取点找全，并区分每个点读的是「活拓扑」还是「配置叶子」。

**操作步骤：**

1. 用搜索定位 `llama.py` 中所有 `get_parallel()`：预期命中第 155、462、463、530 行。
2. 对每个命中点判断类别：
   - `tp_size`（155、462）、`tp_rank`（463）→ 在 [runtime_context.py:169-174](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py#L169-L174) 是 `@property` → 活拓扑。
   - `enable_dp_lm_head`（530）→ 不在 property 列表 → 经 `__getattr__` 取自配置袋 → 配置叶子。
3. 对照本次更新的 diff：把 `get_parallel().enable_dp_lm_head` 还原成旧写法 `get_server_args().enable_dp_lm_head`，体会「为何旧写法在新架构下会拿到可能已过时的快照」。

**需要观察的现象：** `llama.py` 的 import 段已**不再**出现 `get_server_args`（见 [llama.py:53](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L53)，只 import 了 `get_parallel`）。

**预期结果：** 一张表，列出「行号 / 读的字段 / 活拓扑还是配置叶子 / 用途」。结论：4 个读取点里，3 个是活拓扑（`tp_size`×2、`tp_rank`×1），1 个是配置叶子（`enable_dp_lm_head`）。

#### 4.3.5 小练习与答案

**练习 1：** 如果未来要让模型在运行期临时把 `tp_size` 当成 1（例如某个单卡子图），应该怎么改？能直接 `get_parallel().tp_size = 1` 吗？

**参考答案：** 不能裸赋值。`ParallelContext` 提供了上下文管理器 `override(**kwargs)`（见 [runtime_context.py:146-158](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py#L146-L158)），用 `with get_parallel().override(tp_size=1): ...` 临时强制并在退出时恢复；`_v` 会优先读 `_overrides` 里的值。这是运行期改并行信息的唯一受控入口。

**练习 2：** 为什么把 `enable_dp_lm_head` 也放到 `get_parallel()` 而不是单独一个 `get_model()`？

**参考答案：** 因为 `enable_dp_lm_head` 本质是「并行拓扑相关」的配置（决定 LM Head 走哪个进程组），逻辑上属于 `parallel` 命名空间，而不是「模型结构」配置。`ParallelContext` 把「并行拓扑事实」和「并行相关配置叶子」统一在一个访问器下，让模型代码不必关心它来自 property 还是配置袋——这是命名空间袋设计的初衷（见 u2-l5）。

### 4.4 模型注册：registry 如何发现 LlamaForCausalLM

#### 4.4.1 概念说明

模型实现写好后，还得让框架「按名字找得到」。HF 在 `config.json` 里写 `architectures: ["LlamaForCausalLM"]`，SGLang 启动时读取它，再用一张「架构名 → 模型类」的映射表查出该实例化哪个类。这张表就是 `ModelRegistry`，建表的方式是**自动扫描** `models/` 目录。

每个模型文件用 `EntryClass` 这个约定暴露自己：扫描器看到模块里有 `EntryClass`（可以是单个类，也可以是列表），就把列表里的每个类按「类名」注册进表。`llama.py` 的 `EntryClass` 同时注册了 `LlamaForCausalLM` 及几个直接继承它的变体（`Phi3ForCausalLM`、`InternLM3ForCausalLM`、`IQuestCoderForCausalLM`），这样这些架构名都能命中同一份实现。

#### 4.4.2 核心流程

注册与解析流程：

```
# 启动期（registry.py 模块加载时）
ModelRegistry.register("sglang.srt.models")
  └─ import_model_classes 扫描 models/ 下每个 .py
       └─ 若模块有 EntryClass: 按类名登记进 dict
# 请求期
读 config.json 的 architectures (如 ["LlamaForCausalLM"])
  └─ resolve_model_cls(architectures)
       └─ 依次试表，命中即返回 (类, 架构名); 全不中则把 TransformersForCausalLM 兜底放最后
```

兜底机制：`_normalize_archs` 会把「请求里没被表收录的架构」补一个 `TransformersForCausalLM` 放到列表末尾，作为最后退路（通用 transformers 后端）。

#### 4.4.3 源码精读

模型文件侧的声明：

[llama.py:921-926](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L921-L926) 中文说明：`EntryClass` 列出本文件对外暴露的模型类。`Phi3ForCausalLM` 等都只是 `class XxxForCausalLM(LlamaForCausalLM): pass`（[llama.py:909-918](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L909-L918)），复用 Llama 实现，仅换个架构名。

注册表的扫描入口：

[registry.py:130-134](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/registry.py#L130-L134) 中文说明：模块加载时立即 `ModelRegistry.register("sglang.srt.models")`，扫描整个 models 包；若设了环境变量 `SGLANG_EXTERNAL_MODEL_PACKAGE`，再注册一个外部包（`overwrite=True` 允许覆盖内置实现，方便用户替换）。

逐模块导入并登记：

[registry.py:111-125](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/registry.py#L111-L125) 中文说明：`import_model_classes` 遍历包内模块，跳过被 `SGLANG_DISABLED_MODEL_ARCHS` 禁用的架构；导入失败时默认忽略（非 strict），读取模块的 `EntryClass`，按类名写入映射，并断言无重名。

请求期按架构名解析：

[registry.py:80-91](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/registry.py#L80-L91) 中文说明：`resolve_model_cls` 把请求架构归一化（未收录的补 `TransformersForCausalLM` 兜底），依次查表，命中即返回 `(模型类, 架构名)`；全不中则抛「不支持」错误并列出全部已支持架构。

> 与权重加载的衔接：解析到 `LlamaForCausalLM` 后，加载器会实例化它并调用 `load_weights`（见 u5-l4）。`load_weights` 内部按 `SGLANG_ENABLE_WEIGHT_LOADER_V2` 在 legacy 内联映射（[llama.py:661-732](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L661-L732)）与 v2（`AutoWeightsLoader` 递归派发，[llama.py:734-770](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L734-L770)）之间分发。两套路径都依赖 `stacked_params_mapping`（[llama.py:534-541](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L534-L541)）把 HF 的 `q_proj/k_proj/v_proj` 重映射到融合后的 `qkv_proj`——这正是 4.2 节「融合投影」在加载侧的对偶。

#### 4.4.4 代码实践

**实践目标：** 跟踪一个架构名从 `config.json` 到模型实例化的完整路径。

**操作步骤：**

1. 找一个本地 Llama 模型的 `config.json`，确认里面有 `"architectures": ["LlamaForCausalLM"]`。
2. 在 [registry.py:131](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/registry.py#L131) 确认扫描发生在模块加载期。
3. 模拟解析：在 Python 里执行（示例代码，需在已安装 sglang 的环境运行）：

   ```python
   # 示例代码：仅演示解析逻辑，不实例化模型
   from sglang.srt.models.registry import ModelRegistry
   cls, arch = ModelRegistry.resolve_model_cls(["LlamaForCausalLM"])
   print(cls, arch)   # 预期: <class '...llama.LlamaForCausalLM'> LlamaForCausalLM
   ```

4. 把架构名换成 `["LlamaForCausalLM", "SomeUnknownArch"]`，观察 `_normalize_archs` 是否在末尾补上 `TransformersForCausalLM`。

**需要观察的现象：** `resolve_model_cls(["LlamaForCausalLM"])` 返回的类正是 `llama.py` 里 `EntryClass` 的第一个；未知架构会被归一化补兜底项。

**预期结果：** 你能讲清「`config.json` 的 architectures → `resolve_model_cls` → `LlamaForCausalLM` 类 → 实例化 → `load_weights`」这条链。

> 待本地验证：第 3、4 步的运行结果取决于本地是否已安装 sglang 与 transformers；若环境不具备，可改为纯源码阅读——在 `resolve_model_cls` 与 `_normalize_archs` 里手动推演返回值。

#### 4.4.5 小练习与答案

**练习 1：** 如果你想为 `LlamaForCausalLM` 打补丁，提供一份自定义实现，应该怎么做？

**参考答案：** 写一个外部包（含自己的 `EntryClass`，类名仍是 `LlamaForCausalLM`），然后设置环境变量 `SGLANG_EXTERNAL_MODEL_PACKAGE` 指向它；启动时 [registry.py:133-134](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/registry.py#L133-L134) 会以 `overwrite=True` 注册，覆盖内置实现。

**练习 2：** `Phi3ForCausalLM` 为什么要单独注册，而不是直接用 `LlamaForCausalLM`？

**参考答案：** 因为 Phi-3 的 `config.json` 里 `architectures` 写的是 `Phi3ForCausalLM`。注册表按架构名查找，必须有一个同名条目才能命中；而它的实现与 Llama 完全一致，所以用 `class Phi3ForCausalLM(LlamaForCausalLM): pass` 零成本复用，仅多注册一个名字（见 [llama.py:909-910](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L909-L910)）。

## 5. 综合实践

把本讲四个模块串成一个任务：**为「SGLang 版 Llama 与 HF 版 Llama 的差异」写一份带源码定位的差异说明书。**

要求：

1. **结构对照（承接 4.1、4.2）：** 列一张表，逐项对照 HF `LlamaForCausalLM`/`LlamaModel`/`LlamaDecoderLayer`/`LlamaAttention`/`LlamaMLP` 与 SGLang 同名类的字段/子模块。重点标注三处融合（`qkv_proj`、`gate_up_proj`、Add+RMSNorm）和一处接管（`RadixAttention` 替换 sdpa）。
2. **RadixAttention 边界（承接 4.2）：** 用一段话+两个源码链接说明：进入 `RadixAttention` 之前模型做了什么（投影 + 拆分 + RoPE），`RadixAttention` 内部做了什么（路由到后端 + 读写 paged KV），出来之后模型做了什么（`o_proj`）。引用 [llama.py:216-261](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/llama.py#L216-L261) 与 [radix_attention.py:271-280](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/layers/radix_attention.py#L271-L280)。
3. **并行配置读法（承接 4.3）：** 列出 `llama.py` 全部 4 个 `get_parallel()` 调用点（行号 + 字段 + 活拓扑/配置叶子 + 用途），并写明为何不再用 `get_server_args()`。
4. **注册闭环（承接 4.4）：** 画一条从 `config.json` 的 `architectures` 到 `load_weights` 的调用链，标注 `resolve_model_cls`、`EntryClass`、`stacked_params_mapping` 三个关键点。

完成后，这份说明书应能让你（或同事）在不打开 HF 源码的情况下，仅凭 SGLang 源码就讲清「Llama 在 SGLang 里长什么样、和 HF 差在哪、配置从哪来、是怎么被找到的」。

## 6. 本讲小结

- SGLang 的 Llama 采用「外壳 `LlamaForCausalLM` + 主干 `LlamaModel`」的 HF 同构分层，额外内建流水线并行（`PPMissingLayer`/`PPProxyTensors`）与融合 Add+RMSNorm 残差。
- **`RadixAttention` 替换了 HF 注意力里的 sdpa 核心计算与 KV 缓存管理**；投影、RoPE、`o_proj` 仍留在 `LlamaAttention` 里，且 v 不参与 RoPE。
- 三处融合提升 TP 友好度：`qkv_proj`（QKV 融合）、`gate_up_proj`（gate+up 融合）、Add+RMSNorm；加载侧靠 `stacked_params_mapping` 把 HF 的分离权重重映射到融合参数。
- 并行配置统一走 `get_parallel()`：`tp_size`/`tp_rank` 是活拓扑 `@property`，`enable_dp_lm_head` 是配置叶子（`__getattr__` 取自配置袋）；`get_server_args()` 已从模型里移除。
- 模型经 `EntryClass` 约定被 `ModelRegistry` 自动扫描注册，请求期由 `resolve_model_cls` 按 `config.json` 的 `architectures` 解析；未知架构兜底到 `TransformersForCausalLM`。

## 7. 下一步学习建议

- **横向对比更多模型：** 读完 Llama 后，建议精读 [python/sglang/srt/models/qwen2.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/models/qwen2.py) 等，它们大多以 Llama 为模板，差异主要在 RoPE 缩放、归一化或 MoE。对比能巩固「SGLang 模型的标准组件结构」。
- **深入注意力后端：** 本讲把 `RadixAttention` 当作黑盒，建议回到 u5-l3，精读 `attention_registry` 如何按硬件/模型选后端，以及某个具体后端（如 FlashInfer）如何消费 `forward_batch.out_cache_loc`。
- **新增一个模型：** 当你能默写出 Llama 的组件结构后，参考 u12-l2（新增模型：注册与实现）动手为一个小架构添加支持，把「实现 + 注册 + 权重映射」三件事完整走一遍。
- **配置体系闭环：** 若对 `get_parallel()` 的解析机制意犹未尽，结合 u2-l5 通读 `runtime_context.py` 的四层结构与 `publish(role)` 流程，理解模型读到的配置袋是如何从只读 `ServerArgs` 快照而来的。
