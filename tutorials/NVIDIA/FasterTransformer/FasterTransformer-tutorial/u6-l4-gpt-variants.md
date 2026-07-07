# 模型变体：GPT-J / GPT-NeoX / BART / T5

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说清 GPT-J、GPT-NeoX 与标准 GPT 在「位置编码」「残差结构」上的本质差异，并能在源码里定位这些差异的开关。
2. 理解 rotary position embedding 的两种实现风格（GPT-J 交错式 vs GPT-NeoX rotate-half），知道它们为何被同一个 kernel 用一个 `neox_rotary_style` 布尔值区分。
3. 区分 BART / T5 这类 encoder-decoder 模型与 GPT 家族 decoder-only 模型在 FasterTransformer（下称 FT）里的编排方式——前者用「外部 encoder + 单阶段 `*Decoding`」，后者复用 ParallelGpt 的「context + decoder 两阶段」。
4. 看懂「位置编码 / 是否有 encoder / 激活函数 / layernorm 位置」这四个维度，独立判断任意一个 transformer 变体该套用哪一套实现。

本讲承接 u6-l1（ParallelGpt 的 context/decoder 两阶段分裂）与 u5-l2（Decoding 的端到端生成循环）。如果你已经理解了 ParallelGpt 的两阶段动机，本讲要回答的核心问题是：**当位置编码换成 rotary、当残差结构换成并行、当模型从 decoder-only 变成 encoder-decoder，FT 的代码骨架哪些要改、哪些可以原封不动复用。**

## 2. 前置知识

本讲默认你已经掌握以下概念（前序讲义已建立）：

- **decoder-only vs encoder-decoder**：GPT 系列只有一个 decoder 栈，prompt 直接喂给 decoder；BART/T5 有独立的 encoder 处理源序列，decoder 再对 encoder 输出做 cross-attention。
- **context 阶段与 decoder 阶段**（u6-l1）：FT 把 GPT 一次生成拆成「一次处理整段 prompt」和「逐 token 自回归」两段，因为两段 query 长度不同、最优 kernel 不同。
- **Pre-LN 与并行残差**：标准 transformer 是串行的（先 attention 残差、再 FFN 残差，两次）；GPT-J 采用并行残差（attention 和 FFN 从同一份 LayerNorm 输出同时分支，结果相加，只一次残差）。
- **KV cache 与 cross-attention 的 memory cache**（u5-l1）：decoder 自注意力的 K/V cache 随步增长；encoder-decoder 还多一份对 encoder 输出的 memory cache，仅在首步计算。
- **rotary position embedding（旋转位置编码）**：用旋转矩阵把「绝对位置」编码进 query/key 向量，使内积自然反映相对位置，无需额外位置向量相加。

下表先给出本讲要对比的五个模型的总览（细节会在后续模块逐个展开，这张表也是综合实践的产出）：

| 维度 | GPT | GPT-J | GPT-NeoX | BART | T5 |
| :-- | :-- | :-- | :-- | :-- | :-- |
| 位置编码 | 学习式绝对 | rotary（交错式） | rotary（rotate-half） | 绝对 / relative（可配） | relative attention bias |
| 是否有 encoder | 否 | 否 | 否 | 是 | 是 |
| 激活函数 | gelu | gelu | gelu | gelu（可配） | relu / gated-gelu |
| layernorm 位置 | Pre-LN | 并行（单 LN） | Pre-LN 或并行（可配） | Pre-LN（含 mBART 变体） | Pre-LN + RMSNorm |

## 3. 本讲源码地图

本讲涉及的关键文件（按「编排层 → 计算层」自顶向下）：

| 文件 | 作用 |
| :-- | :-- |
| [models/gptj/GptJ.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/gptj/GptJ.cc) | GPT-J 的模型编排：context/decoder 两阶段主循环，结构与 ParallelGpt 几乎一致，差异在构造参数 `rotary_embedding_dim_`、`neox_rotary_style_`。 |
| [models/gptj/GptJDecoder.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/gptj/GptJDecoder.cc) | GPT-J 单步解码层，体现「并行残差」：attention 与 FFN 共用一份 normed 输入，残差用 `invokeAddBiasAttentionFfnResidual` 一次性合并。 |
| [models/gptneox/GptNeoX.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/gptneox/GptNeoX.cc) | GPT-NeoX 编排，比 GptJ 多一个 `use_gptj_residual_` 开关，用于在并行/串行残差间切换。 |
| [models/gptneox/GptNeoXDecoder.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/gptneox/GptNeoXDecoder.cc) | GPT-NeoX 单步解码层，`use_gptj_residual_` 分支决定走并行还是串行残差路径。 |
| [models/bart/BartDecoding.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bart/BartDecoding.cc) | BART 端到端生成：吃 `encoder_output`，逐 token 调 `BartDecoder`（含 cross-attention），单阶段循环。 |
| [models/t5/T5Decoding.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/t5/T5Decoding.cc) | T5 端到端生成：与 BartDecoding 同构，差异在 relative bias、RMSNorm、MoE 支持。 |
| [kernels/unfused_attention_kernels.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/unfused_attention_kernels.cu) | rotary 实现的入口之一，`neox_rotary_style` 分支区分两种旋转方式。 |
| [kernels/decoder_masked_multihead_attention.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention.h) | 融合 MHA 的 POD 参数结构，`rotary_embedding_dim` 与 `neox_rotary_style` 在此声明。 |
| docs/gptj_guide.md / gptneox_guide.md / t5_guide.md / bart_guide.md | 各模型的构造参数表、运行说明。 |

## 4. 核心概念与源码讲解

### 4.1 变体全景：decoder-only 与 encoder-decoder 的两套编排

#### 4.1.1 概念说明

FT 仓库里 `models/` 下有十几个模型目录，但它们的「编排骨架」只有两套：

1. **decoder-only 编排**（GPT / GPT-J / GPT-NeoX）：只有一个 decoder 栈。prompt 本身就是上下文，必须先用一次「context 阶段」把整段 prompt 跑完、写满初始 KV cache，再进入「decoder 阶段」逐 token 生成。这正是 u6-l1 讲的 ParallelGpt 两阶段分裂。
2. **encoder-decoder 编排**（BART / T5）：有独立的 encoder。源序列由 encoder（如 `T5Encoder`/`BartEncoder`，不在本讲范围）一次性编码成 `encoder_output`，再交给 `*Decoding` 类做生成。此时「context 阶段」的职责被外部 encoder 接管，`*Decoding` 只剩单阶段的逐 token 生成循环。

> 一句话记忆：**GPT 家族把「编码 prompt」和「生成」都塞进一个模型，靠 context/decoder 两阶段区分；BART/T5 把「编码」外包给 encoder，「生成」只剩单阶段 `*Decoding`。**

#### 4.1.2 核心流程

两套编排的生成主循环其实是同构的，差异只在「有没有 context 阶段」和「decoder 里有没有 cross-attention」：

```
GPT / GPT-J / GPT-NeoX（decoder-only）:
    [context 阶段]  整段 prompt → gpt_context_decoder_  → 写满 KV cache
    for step in [max_input_len, max_output_len):       # decoder 阶段
        embedding_lookup → gpt_decoder_(单 token) → logits GEMM → dynamic_decode → early stop

BART / T5（encoder-decoder）:
    [外部 encoder]   源序列 → encoder → encoder_output（提前算好）
    for step in [1, max_seq_len]:                       # 单阶段生成
        embedding_lookup → decoder_(单 token, 含 cross-attn 到 memory cache) → logits GEMM → dynamic_decode → early stop
```

注意两套循环里「embedding lookup → decoder → logits GEMM → dynamic_decode → early stop」这五步完全一致，这就是它们共享 ParallelGpt 基础设施（`DynamicDecodeLayer`、`invokeGatherTree`、`cache_indirections_` 双缓冲等）的根基。

#### 4.1.3 源码精读

**GPT-J 的两阶段主循环**与 ParallelGpt 几乎逐行对应，先在 context 阶段调 `gpt_context_decoder_`，再进入 decoder 循环调 `gpt_decoder_`：

[GptJ.cc:792](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/gptj/GptJ.cc#L792) —— decoder 阶段逐步生成循环：

```cpp
for (int step = max_input_length; step < (int)max_output_seq_len; step++) {
    ...
    gpt_decoder_->forward(&decoder_output_tensors, &decoder_input_tensors, ...);
    ...
    dynamic_decode_layer_->forward(&dynamic_decode_output_tensors, &dynamic_decode_input_tensors);
}
```

context 阶段在 [GptJ.cc:699-700](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/gptj/GptJ.cc#L699) 调用 `gpt_context_decoder_->forward`，与上述 decoder 循环共享同一份 `key_cache_`/`value_cache_`。

**对比 BART 的单阶段循环**：BART 没有 context 阶段，循环直接从 `step = 1` 开始，且每步只调一个 `decoder_`（内含 cross-attention）：

[BartDecoding.cc:475](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bart/BartDecoding.cc#L475) —— 单阶段生成循环（注意上界是 `<=`，从 `max_input_length=1` 起）：

```cpp
const int max_input_length = 1;
...
for (int step = max_input_length; step <= (int)max_seq_len; step++) {
    ...
    decoder_->forward(&decoder_output_tensors, &decoder_input_tensors, ...);
    ...
    dynamic_decode_layer_->forward(&dynamic_decode_output_tensors, &dynamic_decode_input_tensors);
}
```

BART/T5 的输入是 `encoder_output`（而非 `input_ids`），见 [BartDecoding.cc:362-366](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bart/BartDecoding.cc#L362) 对 `encoder_output` shape `[batch, mem_max_seq_len, encoder_d_model]` 的校验。这就是 encoder-decoder 与 decoder-only 在接口上最直观的区别。

#### 4.1.4 代码实践

**实践目标**：用源码阅读确认两套编排的「context 阶段是否存在」。

**操作步骤**：

1. 打开 `src/fastertransformer/models/gptj/GptJ.h`，确认 `GptJ` 持有 `gpt_context_decoder_` 和 `gpt_decoder_` 两个成员。
2. 打开 `src/fastertransformer/models/bart/BartDecoding.h`，确认 `BartDecoding` 只持有 `decoder_` 一个解码成员（没有 context decoder）。
3. 对比两者的 `forward` 主循环起始步：GptJ 是 `step = max_input_length`（context 阶段已处理 prompt），BartDecoding 是 `step = 1`。

**需要观察的现象**：GptJ 的 context 分支只在 `max_input_length > 1` 时才触发（见 [GptJ.cc:604](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/gptj/GptJ.cc#L604)），而 BART 永远跳过这一步。

**预期结果**：你能用一句话说清——「GPT-J 把 prompt 编码内化成 context 阶段，BART 把 prompt 编码外化成独立 encoder」。待本地验证：若无 GPU 环境，可只做源码阅读。

#### 4.1.5 小练习与答案

**练习 1**：T5Decoding 的 `forward` 循环是两阶段还是单阶段？它为什么不需要 context 阶段？

**参考答案**：单阶段（[T5Decoding.cc:723](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/t5/T5Decoding.cc#L723) `for (step = max_input_length; step <= max_seq_len)`，其中 `max_input_length=1`）。不需要 context 阶段是因为源序列已由独立的 T5Encoder 编码成 `encoder_output`，decoder 只需逐 token 生成并对 encoder memory 做 cross-attention。

**练习 2**：GPT-J 与 BART 都用 `DynamicDecodeLayer` 做 token 选择，这说明两套编排共享了哪一层基础设施？

**参考答案**：共享了「动态解码层」（beam search / sampling 的统一入口）以及其下游的 `cache_indirections_`、`invokeGatherTree` 等机制——这与模型是 decoder-only 还是 encoder-decoder 无关。

---

### 4.2 GPT-J：rotary 位置编码与并行残差结构

#### 4.2.1 概念说明

GPT-J（EleutherAI 的 6B 模型）相对标准 GPT 有两个标志性改动：

1. **rotary position embedding**：不再用学习式的 `position_encoding_table` 与 embedding 相加，而是在 query/key 投影之后，对每个 head 的前 `rotary_embedding_dim` 维做旋转。旋转只作用在 Q、K 上（不影响 V），使 `QK^T` 的内积自然成为相对位置的函数。
2. **并行残差（parallel residual）**：一个 transformer block 里只有**一次** LayerNorm（pre-layernorm），attention 和 FFN **同时**从这份 normed 输出分支，最后把 `layer_input + attention输出 + FFN输出` 一次性相加。这与标准 transformer 的「LN→attn→残差→LN→ffn→残差」串行结构不同，也省了一次 LayerNorm。

GPT-J 的 rotary 采用**交错式（interleaved）**：把每 head 的 `rotary_embedding_dim` 维按相邻两两一组 `(x0,x1),(x2,x3),...` 旋转，FT 里用 `neox_rotary_style = false` 表示。

#### 4.2.2 核心流程

单个 GPT-J decoder block（单步）的计算流程：

```
x = layer_input
h = LayerNorm(x, pre_layernorm)          # 仅一次 LayerNorm
attn_out = SelfAttention(h)              # 与 FFN 共用 h（并行）
ffn_out  = GeluFFN(h)                    # 与 attn 共用 h（并行）
layer_output = x + (attn_out + ffn_out + ffn_bias)   # 单次残差合并
```

数学上，对每一对旋转维 \( (x_{2i}, x_{2i+1}) \) 和旋转角度 \( \theta_i = \text{pos}\cdot 10000^{-2i/d} \)：

\[
\begin{bmatrix} x'_{2i} \\ x'_{2i+1} \end{bmatrix}
=
\begin{bmatrix} \cos\theta_i & -\sin\theta_i \\ \sin\theta_i & \cos\theta_i \end{bmatrix}
\begin{bmatrix} x_{2i} \\ x_{2i+1} \end{bmatrix}
\]

这就是交错式 rotary 的本质。注意残差合并是 `reduceSum`，在张量并行（TP>1）下需要一次 `ftNcclAllReduceSum` 把各卡的局部和归约。

#### 4.2.3 源码精读

**并行残差的证据**在 `GptJDecoder::forward`：attention 的输入 `input_query` 与 FFN 的输入 `ffn_input` **指向同一块** `decoder_normed_input_`，且全程只调用一次 `invokeGeneralLayerNorm`。

[GptJDecoder.cc:263-272](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/gptj/GptJDecoder.cc#L263) —— 单次 pre-LayerNorm：

```cpp
invokeGeneralLayerNorm(decoder_normed_input_, layer_input,
                       gpt_decoder_layer_weight->at(l).pre_layernorm_weights.gamma,
                       gpt_decoder_layer_weight->at(l).pre_layernorm_weights.beta, ...);
```

[GptJDecoder.cc:298-302](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/gptj/GptJDecoder.cc#L298) —— FFN 输入复用同一份 `decoder_normed_input_`（并行证据）：

```cpp
TensorMap ffn_input_tensors(
    {{"ffn_input", Tensor{MEMORY_GPU, data_type, {local_batch_size, hidden_units_}, decoder_normed_input_}}});
```

[GptJDecoder.cc:304-312](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/gptj/GptJDecoder.cc#L304) —— 单次残差合并 `invokeAddBiasAttentionFfnResidual`（把 attn + ffn + bias + 残差一次相加）：

```cpp
invokeAddBiasAttentionFfnResidual(layer_output, ffn_output_, self_attn_output_, layer_input,
                                  gpt_decoder_layer_weight->at(l).ffn_weights.output_weight.bias, ...);
```

> 对比标准 GPT（ParallelGptDecoder）：标准 GPT 在 attention 后有一次残差、在 FFN 后又有一次残差，共两次 `invokeAddBiasResidual`，且 attention 与 FFN 之间有一次中间 LayerNorm。GPT-J 把它压成一次——这是「并行结构」在源码层的判据。

**rotary 的开关**在模型构造时传入。GptJ 把 `rotary_embedding_dim_` 与 `neox_rotary_style_` 透传给 context decoder 与 decoder：

[GptJ.cc:49-64](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/gptj/GptJ.cc#L49) —— 构造时把 rotary 参数传给子 decoder（`neox_rotary_style_` 对 GPT-J 取 false）：

```cpp
gpt_decoder_ = new GptJDecoder<T>(0, head_num_, size_per_head_, inter_size_, num_layer_,
                                  rotary_embedding_dim_, neox_rotary_style_, ...);
```

rotary 的真正数学在融合 MHA kernel 内部，由参数结构 `rotary_embedding_dim` / `neox_rotary_style` 控制，见模块 4.3 的精读。

#### 4.2.4 代码实践

**实践目标**：从源码确认 GPT-J 是「并行残差」而非「串行残差」。

**操作步骤**：

1. 在 `GptJDecoder.cc` 中搜索 `invokeAddBiasResidual`（标准串行残差用的 kernel）——你会发现它**不存在**于该文件。
2. 再搜索 `invokeAddBiasAttentionFfnResidual`——这是并行残差专用 kernel，应只出现一次。
3. 数 `invokeGeneralLayerNorm` 的调用次数：每层应只有 1 次（pre_layernorm），而标准 GPT 每层有 2 次。

**需要观察的现象**：GPT-J 每层「1 次 LayerNorm + 1 次合并残差」，标准 GPT 每层「2 次 LayerNorm + 2 次残差」。

**预期结果**：写出一句结论「GPT-J 的 attention 与 FFN 共享 normed 输入、合并残差，是并行结构」。待本地验证：纯源码阅读即可完成。

#### 4.2.5 小练习与答案

**练习 1**：GPT-J 的并行残差在 TP>1 时，为什么残差合并后还需要一次 `ftNcclAllReduceSum`？

**参考答案**：并行结构下 `layer_output = x + attn_out + ffn_out` 的 attn/ffn 输出都是行并行 GEMM 的局部和（FFN 输出投影沿 vocab/hidden 维行切分），各卡只持有部分和，必须 all-reduce 才能还原完整结果。串行结构则把这次通信拆到 attn 后、ffn 后各一次。

**练习 2**：rotary 旋转作用在 Q、K、V 中的哪几个？为什么？

**参考答案**：只作用在 Q 和 K。因为 rotary 的目的是让 `QK^T` 的内积成为相对位置的函数；V 不参与 query-key 匹配，旋转 V 无意义。

---

### 4.3 GPT-NeoX：rotate-half rotary 与可配置残差

#### 4.3.1 概念说明

GPT-NeoX（EleutherAI 的 20B 模型）与 GPT-J 同属「rotary + 并行」家族，但有两点关键不同：

1. **rotate-half 风格的 rotary**（`neox_rotary_style = true`）：把每 head 的 `rotary_embedding_dim` 维**对半切**成前后两段 \( (x_0,\dots,x_{d/2-1}) \) 和 \( (x_{d/2},\dots,x_{d-1}) \)，让前段与后段交叉旋转：

\[
\text{rotate\_half}(x) = (-x_{d/2:d},\; x_{0:d/2})
\]

\[
x' = x\odot\cos\theta + \text{rotate\_half}(x)\odot\sin\theta
\]

这与 GPT-J 的交错式数学上等价（都是同一个 rotary 变换），但权重在内存里的排列不同，因此实现路径不同，需要单独的 kernel 分支。

2. **可配置残差**：GPT-NeoX 的实现用一个布尔开关 `use_gptj_residual_` 在「并行残差（GPT-J 式）」与「串行残差（标准 Pre-LN 式）」之间切换。**GPT-NeoX-20B 官方权重用的就是并行式**（`use_gptj_residual=1`），但 FT 的代码同时支持两种，以适配不同来源的 checkpoint。

#### 4.3.2 核心流程

GPT-NeoX 单步解码层根据 `use_gptj_residual_` 走两条不同的数据流：

```
若 use_gptj_residual_ == true（并行，GPT-NeoX-20B 默认）:
    h = LN(x, pre_layernorm)
    h2 = LN(x, post_attention_layernorm)        # 第二个 LN（NeoX 并行式有两个 LN）
    attn_out = SelfAttention(h)
    ffn_out  = GeluFFN(h2)
    layer_output = AllReduce(x + attn_out + ffn_out + bias)

若 use_gptj_residual_ == false（串行，标准 Pre-LN）:
    h = LN(x, pre_layernorm);  attn_out = SelfAttention(h)
    x = x + attn_out + bias                    # 第一次残差（融合在下个 LN 里）
    h = LN(x, post_attention_layernorm);  ffn_out = GeluFFN(h)
    layer_output = x + ffn_out + bias          # 第二次残差
```

注意 GPT-NeoX 的「并行式」与 GPT-J 略有不同：GPT-NeoX 并行式有**两个** LayerNorm（`pre_layernorm` 给 attention、`post_attention_layernorm` 给 FFN），而 GPT-J 并行式只有一个。这是两者 checkpoint 结构的差异在代码上的反映。

#### 4.3.3 源码精读

**可配置残差的分支**是理解 GPT-NeoX 的核心，全在 `GptNeoXDecoder::forward`：

[GptNeoXDecoder.cc:298-309](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/gptneox/GptNeoXDecoder.cc#L298) —— `use_gptj_residual_ == true` 分支：对 attention 输出后，再做一次 `post_attention_layernorm` 供 FFN 使用（并行式有两个 LN）：

```cpp
if (use_gptj_residual_) {
    invokeGeneralLayerNorm(decoder_normed_input_, layer_input,
                           ...->post_attention_layernorm_weights.gamma, ...);
}
```

[GptNeoXDecoder.cc:310-328](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/gptneox/GptNeoXDecoder.cc#L310) —— `use_gptj_residual_ == false` 分支：走串行残差，用 `invokeGeneralAddBiasResidualPreLayerNorm` 把「attn 残差 + 下一个 pre-LN」融合：

```cpp
else {
    invokeGeneralAddBiasResidualPreLayerNorm(self_attn_output_, decoder_normed_input_, ...);
}
```

[GptNeoXDecoder.cc:339-365](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/gptneox/GptNeoXDecoder.cc#L339) —— 残差合并同样二选一：并行式 `invokeAddBiasAttentionFfnResidual` + all-reduce；串行式 `invokeAddBiasResidual`：

```cpp
if (use_gptj_residual_) {
    invokeAddBiasAttentionFfnResidual(layer_output, ffn_output_, self_attn_output_, layer_input, ..., tensor_para_.world_size_, stream_);
    if (tensor_para_.world_size_ > 1) {
        ftNcclAllReduceSum(layer_output, layer_output, local_batch_size * hidden_units_, tensor_para_, stream_);
    }
}
else {
    invokeAddBiasResidual(layer_output, self_attn_output_, ..., stream_);
}
```

注释里作者点明了并行式在 TP 下的等价改写：`reduceSum(ffn + attn + bias + input/TP_size)`，把 input 也并入 all-reduce，从而让 `layer_input` 与 `layer_output` 复用同一块 buffer。

**rotate-half rotary 的 kernel 分支**：rotary 真正发生在融合 MHA kernel 内，由 `neox_rotary_style` 决定走哪条路径。先看参数声明：

[decoder_masked_multihead_attention.h:89-91](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention.h#L89) —— rotary 参数在 POD 结构里的声明：

```cpp
// The per-head latent space reserved for rotary embeddings.
int  rotary_embedding_dim = 0;
bool neox_rotary_style    = false;
```

[unfused_attention_kernels.cu:1426-1465](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/unfused_attention_kernels.cu#L1426) —— `neox_rotary_style` 分支：false 走交错式直接旋转，true 走「写共享内存 → 转置 → 旋转 → 写回」的 rotate-half：

```cpp
if (!neox_rotary_style) {
    mmha::apply_rotary_embedding(q, k, tidx, rotary_embedding_dim, dst_kv_seq_idx);   // 交错式
}
else {
    // rotate-half：把前半/后半经共享内存转置重排后再旋转
    const int half_rotary_dim = rotary_embedding_dim / 2;
    ...
    mmha::vec_from_smem_transpose(q, q_smem, transpose_idx, smem_pitch);
    mmha::apply_rotary_embedding(q, k, transpose_idx / tidx_factor, rotary_embedding_dim, dst_kv_seq_idx);
    mmha::write_smem_transpose(q, q_smem, transpose_idx, smem_pitch);
}
```

> 关键直觉：两种风格的旋转数学完全相同，差异只在「权重在 `head_dim` 维的物理排列」。GPT-J 把要配对旋转的两维放相邻位置（交错），GPT-NeoX 把它们放首尾两半（rotate-half）。所以 NeoX 风格需要一次共享内存转置把两半「凑到一起」再旋转，这正是上面 `vec_from_smem_transpose` 的用途，也比 GPT-J 多了一道共享内存往返。

`gptneox_config.ini` 也证实 GPT-NeoX-20B 用并行式：`use_gptj_residual=1`、`rotary_embedding=24`（在 `size_per_head=96` 中只旋转前 24 维），见 [docs/gptneox_guide.md:163-167](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gptneox_guide.md#L163)。

#### 4.3.4 代码实践

**实践目标**：亲手切换 GPT-NeoX 的两种残差模式，观察 kernel 调用差异。

**操作步骤**：

1. 在 `GptNeoXDecoder.cc` 中定位 `use_gptj_residual_` 的三处分支（LN 选择、FFN 输出 buffer 选择、残差合并）。
2. 假设把 `use_gptj_residual_` 设为 false，列出每层会调用哪些 kernel：应变成 `invokeGeneralLayerNorm`(pre) → attention → `invokeGeneralAddBiasResidualPreLayerNorm` → `invokeGeneralLayerNorm`(post) → FFN → `invokeAddBiasResidual`。
3. 对照 `examples/cpp/gptneox/gptneox_config.ini` 中 `use_gptj_residual` 字段，确认配置项与代码开关的对应关系。

**需要观察的现象**：同一个 `GptNeoXDecoder::forward`，开关一翻转，kernel 序列从「1 次合并残差 + all-reduce」变成「2 次串行残差、无额外 all-reduce（通信被拆进各层 all-reduce）」。

**预期结果**：能用一句话概括「GPT-NeoX 通过 `use_gptj_residual_` 在并行/串行残差间复用同一份 forward」。待本地验证：若无 GPU，纯阅读即可。

#### 4.3.5 小练习与答案

**练习 1**：既然交错式与 rotate-half 数学等价，为什么 FT 要保留两套实现而不是统一成一种？

**参考答案**：因为两种风格的「权重的物理排列」不同，直接对应不同来源的 checkpoint（GPT-J 的权重按交错排布、GPT-NeoX 按首尾两半排布）。统一排列需要在加载权重时做一次内存重排；FT 选择在 kernel 内用转置适配，避免改权重，两种实现各走各的最快路径。

**练习 2**：GPT-NeoX 的并行式有两个 LayerNorm，而 GPT-J 的并行式只有一个。这说明两者 checkpoint 在结构上差了什么？

**参考答案**：GPT-NeoX-20B 为 attention 与 FFN 各自配了一个 pre-LayerNorm（`pre_layernorm` + `post_attention_layernorm`），GPT-J 共用一个 LayerNorm 喂给两者。即两者的 LayerNorm 权重数量不同，加载时必须按各自结构组织。

---

### 4.4 BART：encoder-decoder 的端到端生成

#### 4.4.1 概念说明

BART（及其多语言版 mBART）是 encoder-decoder 模型，本讲的 `BartDecoding` 类只负责「decoder 侧的端到端生成」。它的输入不是 `input_ids`，而是已经由 BartEncoder 编码好的 `encoder_output`（源句的隐状态序列）。每一步生成时，decoder 要做两类注意力：

- **self-attention**（masked）：对已生成的 token，用随步增长的 self K/V cache。
- **cross-attention**：对 encoder 输出，用一份**只算一次**的 memory cache（`key_mem_cache_`/`value_mem_cache_`）。

BART 的位置编码默认是学习式绝对位置（与标准 BERT/GPT 类似），但 FT 的实现把位置编码类型做成了可配置（`position_embedding_type`，可走 absolute 或 relative），并在生成前用 `invokeBuildRelativeAttentionBias` 预建一份 `relative_attention_bias_`，以便支持 relative 变体。激活函数与 layernorm 类型也是构造参数（`activation_type`、`layernorm_type`），方便适配不同 BART 变体。

#### 4.4.2 核心流程

`BartDecoding::forward` 的端到端流程（承接 u5-l2 的 Decoding 思路）：

```
输入: encoder_output [batch, mem_seq_len, d_model], encoder_sequence_length [batch]
1. (可选) invokeTileEncoderResults: 把 encoder_output 按 beam_width 复制展开
2. invokeBuildRelativeAttentionBias: 预建 relative bias [head, seq+1, seq+1]
3. invokeDecodingInitialize: 初始化 finished / sequence_length / cum_log_probs
for step in [1, max_seq_len]:
    4. embedding_lookup (decoder_input_buf_)
    5. invokeGeneralT5LayerNorm(pre_decoder_layernorm): BART 在 word+pos embedding 后接一层 LN
    6. BartDecoder.forward: 内含 masked self-attn + cross-attn + FFN，更新 self/mem cache
    7. logits GEMM (post_decoder_embedding, tie_word_embeddings 时乘 1/sqrt(d_model))
    8. DynamicDecodeLayer.forward: 选 token、更新 finished / cache_indirection
    9. early stop: 若全部 finished 则跳出
10. setOutputTensors: gather_tree / transpose 回输出形状
```

注意第 5 步：BART/mBART 在 embedding lookup 之后、进 decoder 之前有一次 LayerNorm（`pre_decoder_layernorm`），这是 BART 与 T5 的一个细节差异（见模块 4.5）。

#### 4.4.3 源码精读

[BartDecoding.cc:73-77](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bart/BartDecoding.cc#L73) —— 同时分配 self K/V cache 与 cross-attention memory cache（encoder-decoder 的标志）：

```cpp
const size_t self_cache_size = (num_layer_ / pipeline_para_.world_size_) * batchxbeam * (max_seq_len + 1)
                               * (hidden_units_ / tensor_para_.world_size_);
const size_t mem_cache_size  = (num_layer_ / pipeline_para_.world_size_) * batchxbeam * max_mem_seq_len
                               * (hidden_units_ / tensor_para_.world_size_);
```

[BartDecoding.cc:425-433](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bart/BartDecoding.cc#L425) —— 预建 relative attention bias（按 `position_embedding_type` 决定是否启用）：

```cpp
invokeBuildRelativeAttentionBias(relative_attention_bias_,
                                 decoding_weights->absolute_or_relative_position_embedding,
                                 head_num_, (max_seq_len + 1), num_bucket_, false, max_distance_,
                                 decoding_weights->position_embedding_type, stream_);
```

[BartDecoding.cc:505-512](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bart/BartDecoding.cc#L505) —— BART 在 embedding lookup 后接一层 LayerNorm（注释明确说明这是 BART/mBART 的特点）：

```cpp
// BART/mBART has a layernorm after word + positional embedding
invokeGeneralT5LayerNorm(decoder_input_buf_ + d_model_offset, decoder_input_buf_ + d_model_offset,
                         decoding_weights->pre_decoder_layernorm.gamma, ...);
```

[BartDecoding.cc:565-566](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bart/BartDecoding.cc#L565) —— 每步调用 BartDecoder，它的输出包含 self K/V cache 与 memory cache 四组：

```cpp
decoder_->forward(&decoder_output_tensors, &decoder_input_tensors, &decoding_weights->decoder_layer_weights);
```

其中 `decoder_output_tensors` 含 `key_cache_`、`value_cache_`（self）、`key_mem_cache_`、`value_mem_cache_`（memory），见 [BartDecoding.cc:545-553](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bart/BartDecoding.cc#L545)。BartDecoder 内部对一个 block 执行 `pre-LN → masked self-attn → cross-attn → FFN`，与 u5-l1 讲的单步 Decoder 同构，只是多一组 cross-attention 权重与 memory cache。

构造函数里，BART 把 `activation_type_`、`layernorm_type_`、`tie_word_embeddings_`、`q_scaling_` 都做成可配参数，见 [BartDecoding.cc:243-246](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bart/BartDecoding.cc#L243)。

#### 4.4.4 代码实践

**实践目标**：从 cache 分配确认 BART 是 encoder-decoder。

**操作步骤**：

1. 在 `BartDecoding.cc` 的 `allocateBuffer` 里搜索 `mem_cache`，确认它同时持有 self cache 和 memory cache（GPT 系列只有 self cache）。
2. 在 `forward` 里确认每步 `decoder_->forward` 的输出张量组里同时含 `key_cache`/`value_cache`（self）与 `key_mem_cache`/`value_mem_cache`（memory）。
3. 搜索 `cross_attention` 相关：确认 BART 支持把 cross-attention 权重导出（输出张量 `cross_attentions`），这是 decoder-only 模型不可能有的输出。

**需要观察的现象**：BART 的 cache 体积 = `2*self_cache + 2*mem_cache`（K/V 各两份），而 GPT 只有 `2*self_cache`。

**预期结果**：一句话结论——「memory cache 的存在是 encoder-decoder 在显存布局上区别于 decoder-only 的判据」。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：BART 的 memory cache 为什么只在生成开始前计算一次，而 self cache 每步都要追加？

**参考答案**：memory cache 是 encoder 输出经 cross-attention 投影后的 K/V，encoder 输出在整个生成过程中不变，首步算完即可复用；self cache 对应已生成的 token 序列，每生成一个新 token 就追加一项，故随步增长（见 u5-l1）。

**练习 2**：BART 与 GPT 都用 `DynamicDecodeLayer` 选 token，但 BART 的循环从 `step=1` 开始、GPT-J 从 `step=max_input_length` 开始，原因是什么？

**参考答案**：BART 的「源序列处理」由独立 encoder 完成，decoder 的 prompt 只有一个起始符（`max_input_length=1`），故从 step=1 逐 token 生成；GPT-J 的 prompt 是要生成内容的上文，必须先经 context 阶段处理，故 decoder 阶段从 `max_input_length` 起步。

---

### 4.5 T5：relative position bias、RMSNorm 与 MoE 扩展

#### 4.5.1 概念说明

T5 是另一个 encoder-decoder 模型，在 BART 基础上有三处鲜明差异：

1. **relative position bias**：T5 不给 token 加绝对位置向量，而是在 attention 的 `QK^T` 上**加一份可学习的相对位置偏置**。这份偏置由「相对距离 → bucket id → bias」两步得到，bucket 划分按对数距离（近处细、远处粗），由 `num_bucket` 与 `max_distance` 控制。
2. **RMSNorm（T5LayerNorm）**：T5 的 LayerNorm **不减均值**，只用 RMS 归一化（`x / sqrt(mean(x²)+ε) * γ`），比标准 LayerNorm 少一次求和。FT 里对应 `invokeGeneralT5LayerNorm`。
3. **MoE 与 adapter 扩展**：T5Decoding 构造函数接收 `expert_num_`、`moe_k_`、`moe_layer_index_`，支持 mixture-of-experts FFN（部分层换成 MoE），还支持 `ia3_tasks`、`adapter_config` 等。

T5 的生成循环与 BART 同构（都是单阶段、从 `step=1` 起、含 cross-attention），差异全在 decoder 内部的归一化、位置偏置与 FFN 形态。

#### 4.5.2 核心流程

T5 单步生成（差异点用 ★ 标出）：

```
for step in [1, max_seq_len]:
    embedding_lookup
    ★ 不在 embedding 后加 LN（区别于 BART），位置编码走 relative bias
    T5Decoder.forward:
        pre-LN(RMSNorm) → masked self-attn(+relative bias) → cross-attn → FFN(★ 可为 MoE)
    logits GEMM (tie_word_embeddings 时乘 1/sqrt(d_model))
    ★ post_decoder_layernorm(RMSNorm) 后再投影
    DynamicDecodeLayer.forward
    early stop
```

RMSNorm 的数学：

\[
\text{RMSNorm}(x) = \frac{x}{\sqrt{\frac{1}{d}\sum_{i=1}^{d} x_i^2 + \epsilon}} \cdot \gamma
\]

相对标准 LayerNorm 省去了 \(\mu = \frac{1}{d}\sum x_i\) 与 \((x-\mu)\) 两步，计算更轻。

#### 4.5.3 源码精读

[T5Decoding.cc:673-682](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/t5/T5Decoding.cc#L673) —— T5 的 relative position bias 预建（与 BART 共用 `invokeBuildRelativeAttentionBias`，但 T5 默认 `position_embedding_type == relative`）：

```cpp
invokeBuildRelativeAttentionBias(relative_attention_bias_,
                                 decoding_weights->absolute_or_relative_position_embedding,
                                 head_num_, (max_seq_len + 1), num_bucket_, false, max_distance_,
                                 decoding_weights->position_embedding_type, stream_);
```

[T5Decoding.cc:813-821](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/t5/T5Decoding.cc#L813) —— logits 投影前用 RMSNorm（`invokeGeneralT5LayerNorm`）做 post-decoder 归一化（对比 BART 的 mBART 分支才有 post LN）：

```cpp
invokeGeneralT5LayerNorm(normed_decoder_output_buf_ + d_model_offset, decoder_output_buf_ + d_model_offset,
                         decoding_weights->post_decoder_layernorm.gamma, ...);
```

[T5Decoding.cc:28-48](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/t5/T5Decoding.cc#L28) —— T5Decoder 构造时透传 MoE 参数（`expert_num_`、`moe_k_`、`moe_layer_index_`）与 adapter 配置，这是 T5 区别于 BART 的扩展能力：

```cpp
decoder_ = new T5Decoder<T>(0, head_num_, size_per_head_, inter_size_, d_model_, num_layer_,
                            expert_num_, moe_k_, layernorm_eps_, moe_layer_index_, ..., adapter_config_);
```

[T5Decoding.cc:781-783](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/t5/T5Decoding.cc#L781) —— T5 还支持 ia3 微调（按 task 注入低秩适配），BART 无此输入：

```cpp
if (has_ia3_tasks) {
    decoder_input_tensors.push_back(input_tensors->at("ia3_tasks").slice({local_batch_size}, id_offset));
}
```

> BART 与 T5 的代码相似度极高（`allocateBuffer`、生成循环、`dynamic_decode` 组装几乎逐行对应），这正是 FT 「同一套基础设施 + 差异点开关」设计哲学的体现：把 relative bias、RMSNorm、MoE、activation/layernorm 类型都做成可配，于是 BART 与 T5 能共用绝大部分代码。

#### 4.5.4 代码实践

**实践目标**：用 `diff` 思维对比 BartDecoding 与 T5Decoding，量化两者差异。

**操作步骤**：

1. 同时打开 `BartDecoding.cc` 与 `T5Decoding.cc` 的 `initialize()`，对比 `decoder_` 的构造参数：T5 多出 `expert_num_`、`moe_k_`、`moe_layer_index_`、`adapter_config_`。
2. 对比 `forward` 主循环里 embedding 之后那段：BART 有 `invokeGeneralT5LayerNorm(pre_decoder_layernorm)`，T5 直接进 decoder（T5 的归一化在 decoder 内部各层 + 末尾 post_decoder_layernorm）。
3. 对比 logits 投影前的归一化：T5 无条件 `invokeGeneralT5LayerNorm(post_decoder_layernorm)`，BART 仅 mBART 分支才做。

**需要观察的现象**：两份文件骨架几乎一致，差异集中在「构造参数」「embedding 后是否加 LN」「post-LN 是否无条件」三处。

**预期结果**：列出 3 条以上的「BART vs T5」源码差异点。待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：T5 的 RMSNorm 与标准 LayerNorm 相比，少算了哪一项？对精度与速度各有什么影响？

**参考答案**：少了「减均值」这一步（不做中心化，只做缩放归一化）。速度上少一次求和与减法、kernel 更轻；精度上对大多数任务影响可忽略，但在某些对中心化敏感的分布下数值特性略有不同——这正是 T5 原作者的设计取舍。

**练习 2**：T5 的 relative position bias 是加在 `QK^T` 上的，而不是像 GPT-J 那样把位置编进 Q/K 向量本身。这两种思路各有什么优劣？

**参考答案**：T5 的 bucket 偏置是离散、显式的，可解释性强、实现简单，但需要一张 `[head, seq, seq]` 的偏置表，长序列显存开销大；rotary 把位置融进 Q/K，无需额外偏置表、长度外推性更好，但实现需要专门的旋转 kernel。FT 里两者并存：GPT-J/NeoX 走 rotary，T5/BART 走 bias 表。

---

## 5. 综合实践

制作一张完整的「五模型对比表」，并据此回答一个工程选型问题。

### 任务

**第一步（填表）**：补全下表，每个单元格必须能在一处源码或文档里找到依据。前三行已在第 2 节给出，请你为每个单元格标注「依据来源」（文件:行 或 docs 文件名）。

| 维度 | GPT | GPT-J | GPT-NeoX | BART | T5 | 依据 |
| :-- | :-- | :-- | :-- | :-- | :-- | :-- |
| 位置编码 | 学习式绝对 | rotary(交错) | rotary(rotate-half) | 绝对/relative(可配) | relative bias | _自行填写_ |
| 是否有 encoder | 否 | 否 | 否 | 是 | 是 | _自行填写_ |
| 激活函数 | gelu | gelu | gelu | gelu(可配) | relu/gated-gelu | _自行填写_ |
| layernorm 位置 | Pre-LN | 并行(单LN) | Pre-LN 或并行(可配) | Pre-LN(含mBART变体) | Pre-LN + RMSNorm | _自行填写_ |
| 残差结构 | 串行 | 并行 | 并行或串行(可配) | 串行 | 串行 | _自行填写_ |
| cache 种类 | self KV | self KV | self KV | self KV + memory KV | self KV + memory KV | _自行填写_ |

**第二步（选型）**：假设你要部署一个「源语言→目标语言」的翻译模型，且源句平均长度远小于 batch 内最长句。请基于上表回答：

1. 应该选 decoder-only（GPT 家族）还是 encoder-decoder（BART/T5）？为什么？
2. 若选 encoder-decoder，BART 与 T5 在「长序列显存」「归一化速度」「位置编码长度外推」三个角度各有何取舍？

### 预期结果

- 一张填满依据的对比表（至少 6 行 × 6 列）。
- 选型结论应包含：encoder-decoder 更适合翻译（有显式源端编码与 cross-attention）；T5 的 relative bias 在长序列下偏置表显存大但外推性好、RMSNorm 更快；BART 实现更接近标准 transformer、易于适配。

> 本实践无需 GPU，纯源码阅读与文档查阅即可完成；若你本地已编译 FT，可额外用 `examples/cpp/gptneox/gptneox_config.ini` 验证 `use_gptj_residual`、`rotary_embedding` 字段的真实取值。

## 6. 本讲小结

- FT 的 transformer 变体只有两套编排骨架：decoder-only 用「context + decoder 两阶段」（GPT/GPT-J/GPT-NeoX），encoder-decoder 用「外部 encoder + 单阶段 `*Decoding`」（BART/T5），两者的生成主循环（embedding → decoder → logits → dynamic_decode → early stop）同构。
- GPT-J 的标志是「rotary（交错式）+ 并行残差」：每层只有一次 LayerNorm、attention 与 FFN 共用 normed 输入、用 `invokeAddBiasAttentionFfnResidual` 单次合并残差。
- GPT-NeoX 用 `use_gptj_residual_` 开关在并行/串行残差间切换（GPT-NeoX-20B 用并行式，但并行式有两个 LayerNorm，与 GPT-J 的一个不同），其 rotary 是 rotate-half 风格。
- 两种 rotary 风格（`neox_rotary_style`）数学等价，差异只在权重物理排列，故 kernel 内用「是否经共享内存转置」区分。
- BART/T5 是 encoder-decoder：同时持有 self K/V cache 与 cross-attention memory cache（后者只算一次），输入是 `encoder_output` 而非 `input_ids`，循环从 `step=1` 起。
- BART 与 T5 代码高度同构，差异集中在「relative bias 默认开启」「RMSNorm（`invokeGeneralT5LayerNorm`）」「MoE/adapter/ia3 扩展」；这体现 FT「同一套基础设施 + 差异点开关」的设计哲学。

## 7. 下一步学习建议

- **深入 rotary 与融合 MHA**：本讲只触及 rotary 的开关，其数学与共享内存转置的细节在 u3-l2（注意力 kernel）已铺垫；建议重读 `decoder_masked_multihead_attention_utils.h` 里 `apply_rotary_embedding` 的多类型模板，理解为何要为每种向量类型特化。
- **深入 encoder-decoder 的 cross-attention**：本讲的 `BartDecoder`/`T5Decoder` 内部结构与 u5-l1 的单步 Decoder 同构，建议接着读 u5-l1，把 cross-attention 的 K/V 来源（memory cache vs self cache）彻底搞清。
- **动态解码**：本讲所有模型都把「选 token」交给 `DynamicDecodeLayer`，其 beam search / sampling 分发逻辑在 u8 单元（u8-l1/u8-l2/u8-l3）。
- **量化与部署变体**：GPT-J/GPT-NeoX/T5 都有对应的量化（INT8/FP8）与 Triton backend 版本，可在 u9（量化）与 u10（框架集成）继续；尤其 `gpt_fp8`、`gptj` 的 Triton backend 是本讲变体的工程落地。
- **新增模型**：若你要新增一个 transformer 变体，先读 `templates/adding_a_new_model/README.md`（u11-l2），本讲的「差异点开关」模式正是新增模型时复用现有 decoder、只改 rotary/LN/residual 开关的范本。
