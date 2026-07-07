# Decoder 模型：单步自注意力与交叉注意力

## 1. 本讲目标

本讲聚焦 FasterTransformer（下称 FT）中 `models/decoder/Decoder` 这一个类——它是**自回归解码的最小计算单元**，回答三个问题：

1. 一个 transformer **Decoder** block 和 BERT 的 encoder block 在结构上差在哪里？（多了一个**交叉注意力**）
2. Decoder 为什么是「**单步**」的？它如何在每一步把已经生成的 token（self-attention）和编码器的整段输出（cross-attention）结合起来？
3. 它复用了 u3-l3、u3-l4 已经讲过的哪些层和 kernel？

学完后你应该能够：

- 画出单步 Decoder 的数据流：`pre-LN → masked self-attention → cross-attention → FFN`，并标注每一步的残差连接。
- 说清楚 self-attention 用的「**K/V cache**」与 cross-attention 用的「**memory cache**」是两个不同的缓存，更新策略也不同。
- 在源码里定位到 Decoder 复用 BERT 组件的位置（FFN、layernorm、add_residual、融合 masked MHA kernel）。

## 2. 前置知识

本讲默认你已掌握下面两篇讲义的内容（术语不再重复解释）：

- **u3-l3 注意力层**：知道 FT 的注意力层有 Unfused / Fused 两条路径，以及 `DecoderSelfAttentionLayer` 是「逐 token、单 query、复用 `masked_multihead_attention` 融合 kernel」的那条路径。
- **u3-l4 FFN 层**：知道 FFN 是「两段 GEMM + 一段激活」，FT 用模板方法模式派生出 `GeluFfnLayer / ReluFfnLayer / SiluFfnLayer` 等变体，差别只在激活函数。

此外，用通俗语言补三个本讲要用的概念：

- **Encoder-Decoder 架构**：像机器翻译这类任务有两个阶段。**编码器（encoder）**把源句子的所有 token 一次性吃进去，产出一段「浓缩了源句含义」的向量序列，称为 **encoder memory**（或 memory）。**解码器（decoder）**再逐个生成目标语言的 token；生成第 `t` 个 token 时，它既要「看自己已经生成的前 `t-1` 个 token」，也要「看 encoder memory」。前者靠 **self-attention**，后者靠 **cross-attention**。
- **自回归（autoregressive）与「单步」**：decoder 一次只生成一个 token，第 `t` 步的输入是第 `t-1` 步的输出。所以 decoder 的 `forward` 一次只处理「长度为 1 的一段 query」，这叫**单步解码**。把单步 decoder 在外层循环里反复调用（每步生成一个 token），就是下一篇 u5-l2 要讲的 `Decoding`。
- **causal mask（因果掩码）**：生成第 `t` 个 token 时，decoder 的 self-attention 只允许看到第 `0..t` 个位置，禁止「偷看未来」。这个限制在单步解码里几乎是免费的（详见 4.4）。

一句话区分三个容易混淆的名字（与 `docs/decoder_guide.md` 的 [Introduction](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/decoder_guide.md#L36-L40) 一致）：

| 名字 | 指什么 | 对应源码 |
| --- | --- | --- |
| **Decoder** | 一层 transformer decoder block（self-attn + cross-attn + FFN） | `models/decoder/Decoder.cc` |
| **Decoding** | 端到端翻译/生成流程（embedding + 多层 Decoder + beam search/sampling） | `models/decoding/Decoding.cc` |
| **GPT** | 只有 decoder、没有 cross-attention 的生成模型 | `models/multi_gpu_gpt/` |

本讲只讲第一行的 **Decoder**。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [Decoder.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoder/Decoder.h#L34-L84) | `Decoder<T>` 类声明：成员变量、三层子层指针、`forward` 接口。 |
| [Decoder.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoder/Decoder.cc#L159-L321) | 本讲主角。`initialize` 创建三个子层，`forward` 串起单步前向。 |
| [DecoderLayerWeight.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoder/DecoderLayerWeight.h#L26-L208) | 单层权重的容器：`pre/self/cross` 三组 layernorm 权重 + self/cross 两组注意力权重 + FFN 权重。 |
| [DecoderSelfAttentionLayer.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/DecoderSelfAttentionLayer.cc#L458-L686) | 单步 masked self-attention：一次合并 QKV 的 GEMM + 融合 masked MHA kernel + 输出投影。 |
| [DecoderCrossAttentionLayer.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/DecoderCrossAttentionLayer.cu#L865-L1025) | cross-attention：query 来自 decoder，K/V 来自 encoder memory（首步计算后缓存）。 |
| [docs/decoder_guide.md](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/decoder_guide.md#L55-L81) | 官方对 Decoder 输入/输出张量与参数的权威说明。 |

辅助但重要的复用件（u3 已讲，本讲会引用）：

- [AttentionWeight.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/AttentionWeight.h#L23-L31)：self/cross 注意力共用的权重结构 `query/key/value/attention_output` 四组 `DenseWeight`。
- [FfnWeight.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnWeight.h#L23-L30)：FFN 权重结构。
- `decoder_masked_multihead_attention.{h,cu}`：u3-l2 精读过的融合 masked MHA kernel，被 self-attention 直接复用。

---

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：

- **4.1** Decoder 的定位：encoder-decoder 架构里的单步计算单元
- **4.2** 三个子层与对 BERT 组件的复用
- **4.3** `forward` 主流程精读
- **4.4** 单步 self-attention：masked MHA + K/V cache
- **4.5** cross-attention：encoder memory 作为 K/V

### 4.1 Decoder 的定位：单步计算单元

#### 4.1.1 概念说明

`Decoder` 是一个「**给定第 `t` 步的输入向量，算出下一步要用的隐状态**」的函数。它内部包含 `num_layer_` 个结构相同的 decoder block，每个 block 长这样（对照 [decoder_guide.md 的 Workflow](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/decoder_guide.md#L44-L49) 的 Fig.1 红框）：

```
            ┌─────────────────────────────────────────────┐
输入 x_t ──▶│ pre-LN → Self-Attn(masked) → + 残差         │
            │ → pre-LN → Cross-Attn(K/V=encoder memory)   │
            │           → + 残差 → pre-LN → FFN → + 残差   │──▶ 输出隐状态
            └─────────────────────────────────────────────┘
                         （上面这个框就是一层 Decoder，重复 num_layer 次）
```

关键点：**输入只有一个 token 的隐状态**（shape 是 `[batch, hidden]`，没有序列维），不是一整句话。因为它「一次只走一步」。

#### 4.1.2 核心流程

一次 `Decoder::forward` 的宏观流程：

1. 校验输入张量数量（7 个输入、5 个输出）。
2. 按当前 batch 分配 workspace buffer。
3. 对 `l = 0 .. num_layer_-1` 逐层执行：
   - pre-LN
   - masked self-attention（写入/读取 self 的 K/V cache）
   - 融合 `AddBias + 残差 + 下一层 pre-LN`
   - cross-attention（读取 encoder memory，首步写入 memory cache）
   - 融合 `AddBias + 残差 + 下一层 pre-LN`
   - FFN
   - `AddBias + 残差`
4. 可选地释放 workspace。

注意层与层之间用同一块 `decoder_layer_output_` buffer **原地流式覆写**（除首层读输入、末层写输出外），这是 FT 一贯的「省显存」做法（decoder_guide 的 [Optimization 第 2 点](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/decoder_guide.md#L121-L124) 说得很直白：96 层模型只需 1/96 的 buffer）。

#### 4.1.3 源码精读

`forward` 的开头就锁定了「单步」语义——输入第 0 个张量 `decoder_input` 的 shape 是 `[batch_size, hidden_dimension]`，**没有序列维**，这就是「一个 token」：

```cpp
// input tensors:
//      decoder_input [batch_size, hidden_dimension],
//      encoder_output [batch_size, mem_max_seq_len, memory_hidden_dimension],
//      encoder_sequence_length [batch_size],
//      finished [batch_size],
//      step [1] on cpu
//      sequence_lengths [batch_size]
//      cache_indirection [local_batch_size / beam_width, beam_width, max_seq_len]
```

见 [Decoder.cc:L164-L180](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoder/Decoder.cc#L164-L180)。其中 `step [1] on cpu` 是个 CPU 标量，告诉当前是第几步，self-attention 用它定位「该把新 K/V 写进 cache 的哪个槽位」。

#### 4.1.4 代码实践

**实践目标**：确认 Decoder 是单步接口。

**操作步骤**：

1. 打开 [Decoder.cc 的 forward](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoder/Decoder.cc#L159-L183)。
2. 阅读顶部 L164–L180 的输入/输出张量注释。
3. 对比 u4-l1 里 BERT 的输入（`[batch, seq_len, hidden]`）。

**需要观察的现象**：Decoder 输入第 0 维之后直接是 `hidden_dimension`，而 BERT 是 `seq_len, hidden`。

**预期结果**：你能用一句话说明「Decoder 一次只吃一个 token 的 embedding，BERT 一次吃一整句」。

> 待本地验证：若你已编译 FT，可在 `examples/cpp/decoding.cc` 里加一行日志打印 `decoder_input.shape`，确认第二维就是 `hidden_units` 而非序列长度。

#### 4.1.5 小练习与答案

**练习 1**：Decoder 的 `forward` 为什么要接收一个 `step` 参数？BERT 的 forward 接收吗？

**答案**：`step` 告诉 self-attention 当前是第几步，用于把新算出的 K/V 写进 cache 的正确槽位、以及在做 causal mask 时确定「已经生成了几个 token」。BERT 是一次性处理整句、每次都重算注意力，不需要 cache，也就不需要 `step`。

**练习 2**：`cache_indirection` 这个输入张量是给谁用的？（提示：和 beam search 有关）

**答案**：给 self-attention 用的。beam search 每步会重排 beam，已生成 token 的 K/V cache 需要按新的 beam 顺序「指回」旧的 cache 槽位，`cache_indirection` 就是这层映射表（u6-l2 KV cache 会详讲）。

---

### 4.2 三个子层与对 BERT 组件的复用

#### 4.2.1 概念说明

Decoder 类只做「编排」，真正的计算全部委托给三个成员子层。看 [Decoder.h:L47-L49](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoder/Decoder.h#L47-L49)：

```cpp
BaseAttentionLayer<T>* self_attention_layer_;
BaseAttentionLayer<T>* cross_attention_layer_;
FfnLayer<T>*           ffn_layer_;
```

这三个指针指向的类**全部是 BERT/通用基础设施里已有的层**，Decoder 没有新写任何 kernel：

| 子层 | 实际类型 | 出处 | 与 BERT 的关系 |
| --- | --- | --- | --- |
| self_attention_layer_ | `DecoderSelfAttentionLayer<T>` | u3-l3 | 复用 u3-l2 的融合 `masked_multihead_attention` kernel，BERT 的 `DecoderSelfAttentionLayer` 同源 |
| cross_attention_layer_ | `DecoderCrossAttentionLayer<T>` | 本讲 4.5 | BERT 没有，是 encoder-decoder 独有的 |
| ffn_layer_ | `ReluFfnLayer<T>` | u3-l4 | 与 BERT 的 FFN 是**同一个 `FfnLayer` 模板**，只是激活函数固定为 ReLU |

也就是说：**Decoder = (BERT 同款 self-attn) + (新增 cross-attn) + (BERT 同款 FFN)**。这是 FT「kernel/layer/model 三层复用」的典型范例（见 u1-l3）。

#### 4.2.2 核心流程

`initialize()` 在构造函数里 `new` 出这三个子层，把 stream / cublas_wrapper / allocator 一并下发：

```cpp
self_attention_layer_  = new DecoderSelfAttentionLayer<T>(...);
cross_attention_layer_ = new DecoderCrossAttentionLayer<T>(...);
ffn_layer_             = new ReluFfnLayer<T>(..., 0 /*expert_num*/, ...);
```

注意 FFN 选的是 **`ReluFfnLayer`**（见 [Decoder.cc:L40-L49](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoder/Decoder.cc#L40-L49)），即默认用 ReLU 激活——这是早期 transformer-base / OpenNMT 风格解码器的配置。这与现代 GPT 用 GeLU/SiLU 不同，但底层 `FfnLayer` 是同一份代码（u3-l4）。

#### 4.2.3 源码精读

完整的 `initialize` 见 [Decoder.cc:L22-L50](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoder/Decoder.cc#L22-L50)。三个子层共享同一份 `cublas_wrapper_` 与 `allocator_`，所以 GEMM 和显存分配都在 Decoder 的统一环境里。

#### 4.2.4 代码实践

**实践目标**：在源码里定位「Decoder 复用了 BERT 的哪些组件」。

**操作步骤**：

1. 打开 [Decoder.h 的 #include 区](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoder/Decoder.h#L21-L30)，列出它 include 的 layer 头文件。
2. 用 `grep -rn "ReluFfnLayer" src/fastertransformer/layers/` 看 `ReluFfnLayer` 定义在哪。

**需要观察的现象**：Decoder.h include 了 `FfnLayer.h`、`DecoderCrossAttentionLayer.h`、`DecoderSelfAttentionLayer.h`，以及 `add_residual_kernels.h`、`layernorm_kernels.h`。

**预期结果**：你会确认 FFN 与 layernorm/add_residual kernel 都是跨模型共享的通用件，Decoder 没有自己的 kernel 源文件（`models/decoder/` 下只有 `.cc/.h`，没有 `.cu`）。

#### 4.2.5 小练习与答案

**练习**：如果把 Decoder 的 FFN 激活从 ReLU 换成 GeLU，需要改 kernel 吗？

**答案**：不需要。`FfnLayer` 用模板方法模式把激活做成可替换的虚函数（u3-l4），只需把 `initialize()` 里的 `new ReluFfnLayer<T>(...)` 换成 `new GeluFfnLayer<T>(...)`，激活由 `genericActivation` 分发到对应 kernel，forward 主体完全不变。

---

### 4.3 `forward` 主流程精读

#### 4.3.1 概念说明

`forward` 把三个子层按 **Pre-LN（先做 LayerNorm 再进子层）** 的顺序串起来，每个子层后面跟一个残差连接。Pre-LN 与 Post-LN 的区别是：

- **Post-LN**（原始 transformer）：`x_{out} = LN(x + SubLayer(x))`
- **Pre-LN**（FT Decoder 采用）：`x_{out} = x + SubLayer(LN(x))`

Pre-LN 对自回归解码更稳定，也便于把「加偏置 + 残差 + 下一步要用的 LN」融合进单个 kernel。

#### 4.3.2 核心流程

一层 Decoder 的伪代码（对应 [Decoder.cc:L206-L316](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoder/Decoder.cc#L206-L316)）：

```
# 0. 选输入/输出指针（首层读 input，末层写 output，中间层读写同一块 buffer）
decoder_input  = (l == 0)             ? external_input : decoder_layer_output_
decoder_output = (l == num_layer_-1)  ? external_output: decoder_layer_output_

# 1. pre-LN（用 pre_layernorm_weights）
decoder_normed_input_ = LayerNorm(decoder_input, pre_layernorm_weights)

# 2. masked self-attention，输出 self_attn_output_，同时更新 self 的 K/V cache
self_attn_output_ = SelfAttention(query=decoder_normed_input_,
                                  K/V cache, step, sequence_lengths)

# 3. 融合：self_attn_output_ += bias + 残差(decoder_input)；同时算下一子层的 pre-LN
(self_attn_output_, normed_self_attn_output_) =
        AddBiasResidualPreLayerNorm(self_attn_output_, residual=decoder_input,
                                    self_attn_layernorm_weights)

# 4. cross-attention，query=normed_self_attn_output_，K/V=encoder memory（memory cache）
cross_attn_output_ = CrossAttention(query=normed_self_attn_output_,
                                    encoder_output, encoder_sequence_length, step)

# 5. 融合：cross_attn_output_ += bias + 残差(self_attn_output_)；算下一子层 pre-LN
(cross_attn_output_, normed_cross_attn_output_) =
        AddBiasResidualPreLayerNorm(cross_attn_output_, residual=self_attn_output_,
                                    cross_attn_layernorm_weights)

# 6. FFN，输入是 normed_cross_attn_output_
decoder_output = FFN(normed_cross_attn_output_, ffn_weights)

# 7. 加偏置 + 残差（cross_attn_output_）
decoder_output = AddBiasResidual(decoder_output, residual=cross_attn_output_,
                                 ffn_output_bias)
```

第 3、5、7 步是三次残差连接，分别围绕 self-attn、cross-attn、FFN。注意第 3、5 步用的 `invokeGeneralAddBiasResidualPreLayerNorm` 是一个**融合 kernel**——它一次完成「加偏置、加残差、再为下一个子层做 LayerNorm」，省掉两次显存往返（decoder_guide [Optimization 第 1 点](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/decoder_guide.md#L121-L124) 强调的「fuse many small operations into one kernel」）。

#### 4.3.3 源码精读

pre-LN（第 1 步）调用通用 layernorm kernel：

[Decoder.cc:L220-L230](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoder/Decoder.cc#L220-L230) —— 对 `decoder_input` 做 LayerNorm，权重取自本层的 `pre_layernorm_weights`，输出写到 `decoder_normed_input_`。

self-attention 前向（第 2 步）：

[Decoder.cc:L247-L249](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoder/Decoder.cc#L247-L249) —— 把组装好的 `self_attention_input_tensors` / `self_attention_output_tensors` 交给 `self_attention_layer_->forward(...)`。注意这里子层的输入输出用的是 **`TensorMap`**（按名字索引），虽然 Decoder 顶层接口仍是老的 `std::vector<Tensor>`（[L160-L162](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoder/Decoder.cc#L160-L162)），但内部已转换成 `TensorMap` 喂给新式子层（u2-l1）。

融合 AddBias+残差+pre-LN（第 3 步）：

[Decoder.cc:L251-L268](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoder/Decoder.cc#L251-L268) —— 一行 `invokeGeneralAddBiasResidualPreLayerNorm(...)` 同时产出 `self_attn_output_`（带残差的 self-attn 结果）和 `normed_self_attn_output_`（喂给 cross-attn 的 pre-LN 结果）。

cross-attention 前向（第 4 步）：

[Decoder.cc:L282-L284](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoder/Decoder.cc#L282-L284) —— `cross_attention_layer_->forward(...)`，其 query 是 `normed_self_attn_output_`。

FFN + 最后一次 AddBiasResidual（第 6、7 步）：

[Decoder.cc:L307-L314](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoder/Decoder.cc#L307-L314) —— `ffn_layer_->forward(...)` 后跟一行 `invokeAddBiasResidual(...)`。这里 FFN 第二段的 bias 没在 FFN 内部相加，而是融合进这个残差 kernel（与 u3-l4 讲的「b2 融合进下游残差 kernel」完全一致）。

> 小细节：循环里有个看似无用的 `int tmp_0 = 0;`（[L232](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoder/Decoder.cc#L232)），它不影响逻辑，可理解为历史遗留的调试占位。

#### 4.3.4 代码实践

**实践目标**：把伪代码与真实源码逐行对上。

**操作步骤**：

1. 打开 [Decoder.cc 的层循环 L206-L316](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoder/Decoder.cc#L206-L316)。
2. 在 4.3.2 的伪代码七步旁边，分别写下对应的源码行号。

**需要观察的现象**：每次子层调用之后都紧跟一个 `sync_check_cuda_error();`（如 [L268](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoder/Decoder.cc#L268)、[L303](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoder/Decoder.cc#L303)、[L315](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoder/Decoder.cc#L315)）。

**预期结果**：你会发现 `sync_check_cuda_error()` 在 Release 模式下是空操作，只有 `FT_DEBUG_LEVEL=DEBUG` 时才插入 `cudaDeviceSynchronize()`（u1-l5）。这是 FT 在异步 kernel 链里插的「可选检查点」。

#### 4.3.5 小练习与答案

**练习 1**：一层 Decoder 一共有几次残差连接？分别围绕哪个子层？

**答案**：三次。分别围绕 self-attention、cross-attention、FFN，对应源码的 `invokeGeneralAddBiasResidualPreLayerNorm`（self）、`invokeGeneralAddBiasResidualPreLayerNorm`（cross）、`invokeAddBiasResidual`（FFN）。

**练习 2**：为什么第 3、5 步用 `...PreLayerNorm`，而第 7 步只用 `AddBiasResidual`？

**答案**：第 3、5 步后面还要进下一个子层，需要顺手把下一个子层的 pre-LN 算出来，所以融合进 `AddBiasResidualPreLayerNorm`；第 7 步是本层最后一步，后面要么进下一层（下一层开头会自己再做 pre-LN），要么直接输出，不需要再带一个 LN，所以只做 `AddBiasResidual`。

---

### 4.4 单步 self-attention：masked MHA + K/V cache

#### 4.4.1 概念说明

self-attention 的 query/k/value 全部来自 decoder 自己。单步解码时 query 长度恒为 1，但 attention 要看「到目前为止生成的所有 token」，所以 K、V 不能每步重算——必须把历史 K/V 存起来，这就是 **K/V cache**。

FT 在这里把 u3-l2 精读过的融合 `masked_multihead_attention` kernel 直接拿来用：一次 kernel 调用同时完成「把新 K/V 写进 cache → 用全部历史 K/V 算 attention → 输出 context」。在单 query 场景下，causal mask 是**结构性免费**的（循环上界就是当前步数，天然不看未来，u3-l2 已论证）。

#### 4.4.2 核心流程

`DecoderSelfAttentionLayer::forward` 三段式（[.cc:L458-L686](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/DecoderSelfAttentionLayer.cc#L458-L686)）：

1. **QKV GEMM**：一次合并的 GEMM 把 `input_query [batch, d_model]` 投影成 `[batch, 3*hidden]`（Q/K/V 拼在一起），写到 `qkv_buf_`。
2. **融合 masked MHA**：`fusedQKV_masked_attention_dispatch(...)` → `masked_multihead_attention(params, stream)`，把新 K/V 写进 `key_cache/value_cache` 并算出 `context_buf_`。
3. **输出投影 GEMM**：把 `context_buf_` 经 `attention_output_weight` 投影回 `d_model` 维。

#### 4.4.3 源码精读

QKV 合并 GEMM（第 1 步）：

[DecoderSelfAttentionLayer.cc:L566-L577](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/DecoderSelfAttentionLayer.cc#L566-L577) —— 注意 `n = 3 * local_hidden_units_`，即 Q、K、V 三个投影矩阵在权重维拼接，一次 GEMM 出 QKV。这就是 u3-l3 提到的「decoder 单 query 用 1 次合并 QKV GEMM」。

融合 masked MHA（第 2 步）：

[DecoderSelfAttentionLayer.cc:L581-L614](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/DecoderSelfAttentionLayer.cc#L581-L614) —— `fusedQKV_masked_attention_dispatch` 把大量参数填进 `Masked_multihead_attention_params`，最后调用 `masked_multihead_attention(params, stream)`（[L144](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/DecoderSelfAttentionLayer.cc#L143-L145)）。这正是 u3-l2 讲的融合 kernel，self/cross 用 `CROSS_ATTENTION` 模板参数区分。

输出投影（第 3 步）：

[DecoderSelfAttentionLayer.cc:L667-L678](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/DecoderSelfAttentionLayer.cc#L667-L678) —— `attention_output_weight.kernel` 把 `context_buf_` 投影回 `d_model`。

#### 4.4.4 代码实践

**实践目标**：理解「self-attention 的 K/V cache 在哪里被写入」。

**操作步骤**：

1. 在 Decoder 的 `forward` 里找到 self-attention 的输出张量组装：[Decoder.cc:L235-L246](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoder/Decoder.cc#L235-L246)。
2. 注意 `key_cache` / `value_cache` 用 `getPtrWithOffset(self_key_cache_offset)` 取出「本层对应的切片」。

**需要观察的现象**：`self_key_cache_offset` 的计算（[L210-L213](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoder/Decoder.cc#L210-L217)）是 `l × (后面所有维度的乘积)`，即第 `l` 层在 `output_tensors->at(1)` 这块大 buffer 里的偏移。

**预期结果**：你会明白——所有层的 K/V cache 是**一块连续大 buffer**，按层偏移切分；每层的 self-attention 只读写自己那一段，互不干扰。

> 待本地验证：可在 `fusedQKV_masked_attention_dispatch` 入口加日志打印 `step` 和 `key_cache` 指针，确认每步把新 K/V 写到 `step` 对应槽位。

#### 4.4.5 小练习与答案

**练习**：为什么 self-attention 的 QKV 投影能合并成一次 GEMM，而 cross-attention 不能？

**答案**：self-attention 的 Q/K/V 三个矩阵的输入都是同一个 `input_query`，所以可以把三个 `[hidden, hidden]` 的权重纵向拼成 `[hidden, 3*hidden]`，一次 GEMM 出 QKV。cross-attention 的 Q 来自 decoder、K/V 来自 encoder memory，输入不同，没法合并，必须分开做（见 4.5）。

---

### 4.5 cross-attention：encoder memory 作为 K/V

#### 4.5.1 概念说明

cross-attention（也叫 encoder-decoder attention）是 decoder 区别于 BERT/GPT 的核心：它的 **query 来自 decoder**，但 **K 和 V 来自 encoder 的输出（encoder memory）**。这样 decoder 在生成每个 token 时，都能「查阅」源句子的全部信息。

由于 encoder memory 在整个生成过程中不变，K/V 只需在**第 1 步**算一次并缓存进 `key_mem_cache / value_mem_cache`（称为 **memory cache**），之后每步直接读，不必重算。这与 self-attention 的 K/V cache（每步都要追加新 token）形成鲜明对比。

#### 4.5.2 核心流程

`DecoderCrossAttentionLayer::forward`（[.cu:L865-L1025](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/DecoderCrossAttentionLayer.cu#L865-L1025)）：

1. **Query GEMM**：把 `attention_input [batch, d_model]` 投影成 `q_buf_ [batch, hidden]`。
2. **首步构造 memory cache**：`if (step == 1)` 时，用 `key_weight.kernel` / `value_weight.kernel` 对整段 `encoder_output [batch, mem_max_seq_len, mem_hidden]` 做 GEMM，得到 K、V 写进 `key_mem_cache` / `value_mem_cache`。
3. **cross-attention 计算**：`cross_attention_dispatch(...)` 用 query 去查询 memory cache，输出 `context_buf_`。
4. **输出投影 GEMM**：`attention_output_weight.kernel` 把 `context_buf_` 投影回 `d_model`。

#### 4.5.3 源码精读

Query GEMM（第 1 步）：

[DecoderCrossAttentionLayer.cu:L902-L912](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/DecoderCrossAttentionLayer.cu#L902-L912) —— 注意 K 维是 `d_model_`（decoder 侧），输出 `hidden_units_`。

首步构造 memory cache（第 2 步）：

[DecoderCrossAttentionLayer.cu:L914-L977](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/DecoderCrossAttentionLayer.cu#L914-L977) —— `if (step == 1)` 守卫下，对 encoder memory 做 K 投影和 V 投影。`is_batch_major_cache_` 为真时会先写到临时 buffer 再用 `transpose_4d_batch_major_memory_kernelLauncher` 转置成 cache 友好的布局（[L927-L928](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/DecoderCrossAttentionLayer.cu#L927-L928)）。关键结论：**只在第 1 步算，之后所有步复用**。

cross-attention kernel（第 3 步）：

[DecoderCrossAttentionLayer.cu:L988-L1009](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/DecoderCrossAttentionLayer.cu#L988-L1009) —— `cross_attention_dispatch` 内部按 `is_batch_major_cache_` 分两条路：非 batch-major 走手写的 `cross_attention_kernel`（[L54-L165](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/DecoderCrossAttentionLayer.cu#L54-L165)），batch-major 走 `cross_multihead_attention`（与 self 同源的融合 kernel 系列）。注意 `cross_attention_kernel` 里同样有 `if (step == 1)` 把 K_bias/V_bias 加进 cache（[L105-L110](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/DecoderCrossAttentionLayer.cu#L105-L110)、[L153-L160](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/DecoderCrossAttentionLayer.cu#L153-L160)），偏置也只在首步注入。

输出投影 GEMM（第 4 步）：

[DecoderCrossAttentionLayer.cu:L1011-L1021](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/DecoderCrossAttentionLayer.cu#L1011-L1021)。

#### 4.5.4 代码实践

**实践目标**：对比两类 cache 的更新时机。

**操作步骤**：

1. 在 [DecoderCrossAttentionLayer.cu:L914](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/DecoderCrossAttentionLayer.cu#L914) 找到 `if (step == 1)`，确认 memory cache 只在首步写。
2. 回到 4.4 的 self-attention，确认 self 的 K/V cache 是**每步**都追加（在融合 kernel 内部按 `step` 写入，没有 `if (step==1)` 守卫）。

**需要观察的现象**：cross-attention 的 memory cache 写入有 `step == 1` 条件；self-attention 的 K/V cache 写入没有这个条件。

**预期结果**：你能填出下表：

| | self-attention K/V cache | cross-attention memory cache |
| --- | --- | --- |
| K/V 来源 | decoder 已生成 token | encoder memory |
| 写入时机 | 每步追加一个 token | 仅第 1 步，之后只读 |
| 大小是否随步增长 | 是 | 否（固定为 `mem_max_seq_len`） |

#### 4.5.5 小练习与答案

**练习 1**：如果 encoder 输出的源句长度 `mem_max_seq_len = 100`，解码 50 步，cross-attention 的 memory cache 一共被写入几次？

**答案**：1 次（仅 `step == 1`）。后面 49 步全部复用首步算好的 K/V，这也是 cross-attention 在长序列生成里很省算力的原因。

**练习 2**：Decoder 的 `forward` 输出张量里，`key_cache/value_cache`（index 1/2）和 `key_mem_cache/value_mem_cache`（index 3/4）分别对应哪种 attention 的 cache？

**答案**：index 1/2 是 **self-attention** 的 K/V cache（每层、每步增长）；index 3/4 是 **cross-attention** 的 memory cache（每层、首步填充后只读）。对应 [Decoder.cc:L175-L180](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoder/Decoder.cc#L175-L180) 的输出注释。

---

## 5. 综合实践

**任务**：对照 `Decoder.cc` 与 `docs/decoder_guide.md`，画一张**单步 Decoder 数据流图**，并标注它复用了 BERT 的哪些层。

### 步骤

1. **读官方说明**：打开 [decoder_guide.md 的 Decoder 小节](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/decoder_guide.md#L55-L81)，把 6 个输入、5 个输出的名字和 shape 抄下来。
2. **画主干**：照 4.3.2 的伪代码，画出一条「`decoder_input` → pre-LN → Self-Attn → +残差 → pre-LN → Cross-Attn → +残差 → pre-LN → FFN → +残差 → `decoder_output`」的链路，三条残差线要画清楚。
3. **标 cache**：在 Self-Attn 旁边画出 `key_cache/value_cache`（每步追加），在 Cross-Attn 旁边画出 `key_mem_cache/value_mem_cache`（标「首步写入、之后只读」），并把 `encoder_output` 作为 Cross-Attn 的 K/V 来源画进来。
4. **标复用件**：在图上用不同颜色或图例标出哪些组件复用自 BERT：
   - FFN = `ReluFfnLayer`（与 BERT 同款 `FfnLayer`）
   - 融合 masked MHA kernel（u3-l2，self-attn 用）
   - `invokeGeneralLayerNorm` / `invokeGeneralAddBiasResidualPreLayerNorm` / `invokeAddBiasResidual`（通用 layernorm/残差 kernel）
   - `AttentionWeight` / `LayerNormWeight` / `FfnWeight` 权重结构
5. **核对**：把图上每个箭头与 [Decoder.cc:L206-L316](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoder/Decoder.cc#L206-L316) 的源码行一一对应。

### 预期产出

一张包含以下要素的图（手绘或软件画均可）：

- 一层 Decoder 的纵向数据流（含三次残差）。
- 两类 cache 的不同更新策略。
- 至少 4 处「复用自 BERT」的标注。

### 进阶（可选）

阅读 [DecoderLayerWeight.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoder/DecoderLayerWeight.h#L139-L144) 的成员（`pre_layernorm_weights` / `self_attention_weights` / `self_attn_layernorm_weights` / `cross_attention_weights` / `cross_attn_layernorm_weights` / `ffn_weights`），在你画的图上每个子层旁边标出它用的是哪一组权重。注意 self-attention 权重里只有 `query_weight`（合并的 QKV）和 `attention_output_weight`，而 cross-attention 权重里 Q/K/V 是分开的三组（[AttentionWeight.h:L23-L31](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/AttentionWeight.h#L23-L31)）——这与 4.4.5 的练习结论吻合。

## 6. 本讲小结

- **Decoder 是单步计算单元**：`forward` 一次只吃一个 token 的隐状态 `[batch, hidden]`，配合 CPU 标量 `step` 工作；自回归生成靠外层（u5-l2 的 `Decoding`）反复调用它。
- **结构 = self-attn + cross-attn + FFN**：比 BERT 多了一个 cross-attention；采用 Pre-LN，每个子层后跟一次残差，共三次残差连接。
- **三类组件全部复用**：FFN 用与 BERT 同源的 `FfnLayer`（默认 `ReluFfnLayer`），self-attn 用 u3-l2 的融合 `masked_multihead_attention` kernel，layernorm/残差用通用 kernel——Decoder 自己没有 `.cu` kernel 文件。
- **两类 cache 语义不同**：self-attention 的 K/V cache 每步追加新 token；cross-attention 的 memory cache 仅首步由 encoder memory 计算并写入，之后只读。
- **接口形态**：Decoder 顶层仍是老式 `std::vector<Tensor>`，但内部已用 `TensorMap` 与新式子层对接；所有层的 cache 共用一块连续大 buffer，按层偏移切分。
- **融合优化**：`AddBias + 残差 + 下一步 pre-LN` 三合一进单个 kernel，是 FT 在 decoder 上的关键省访存手段。

## 7. 下一步学习建议

- **下一篇 u5-l2（Decoding 模型）**：把本讲的「单步 Decoder」放进一个生成循环，加上 embedding、位置编码、beam search/sampling 与 `finished` 判断，就是端到端的 `Decoding`。重点关注它如何调用 `Decoder::forward`、如何在每步处理 beam 重排。
- **后续 u6-l1（ParallelGpt）**：GPT 没有 cross-attention，但把「context 阶段（处理整段 prompt）」与「decoder 阶段（逐 token）」拆成两个类，是对本讲单步思想在大模型上的扩展——可对比 `ParallelGptDecoder` 与本讲 `Decoder` 的异同。
- **延伸阅读源码**：若想深入 cache 在 beam search 下的重排，直接看 u6-l2 要讲的 `kernels/gpt_kernels.cu`（`invokeTransposeAxis01` 等），并回过头理解本讲 self-attention 接收的 `cache_indirection` 输入。
