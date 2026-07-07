# ParallelGpt 架构：ContextDecoder 与 Decoder 分裂

## 1. 本讲目标

本讲是「GPT 与大模型推理」单元的第一篇。学完之后，你应当能够：

- 说清 FasterTransformer（下文简称 FT）为什么把 GPT 的一次生成拆成 **context 阶段**和 **decoder 阶段**两部分，以及这种拆分的性能动机。
- 读懂 `ParallelGpt` 类的构造参数与关键字段（`beam_width`、`tensor_para`、`int8_mode`、`attention_type` 等），知道它们如何决定运行期行为。
- 跟着 `ParallelGpt::forward` 的主循环，看清「先跑一次 context decoder 写满初始 KV cache，再循环跑单步 decoder 逐 token 生成」的编排顺序。
- 区分 `ParallelGptContextDecoder` 和 `ParallelGptDecoder` 各自使用的自注意力实现，理解它们为何不可互换。

本讲承接 u5-l2（`Decoding` 端到端生成）与 u3-l3（注意力层 Unfused/Fused/TensorParallel），是后续 u6-l2（KV cache）、u6-l3（交互式生成）的直接前置。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自前置讲义）：

- **自回归生成**：GPT 每一步只生成一个新 token，把它拼回输入再生成下一个，循环往复直到遇到结束符或达到长度上限。
- **KV cache**：为了避免每一步都重算历史 token 的 Key/Value，FT 把每层的 K、V 缓存下来，下一步只算新 token 的 K/V 并追加。
- **Tensor parallel（TP）/ Pipeline parallel（PP）**：TP 把一层的权重按列/行切分到多卡、末尾 all-reduce；PP 把不同层分到不同卡组，按 micro-batch 流水。
- **`TensorMap` 接口**：FT 模型的统一 `forward(output_tensors, input_tensors, weights)` 入参形式（u2-l1）。
- **融合 masked MHA kernel**：decoder 单 query 场景下把 `QK^T→softmax→PV` 压进单 kernel（u3-l2）。
- **`AttentionType` 枚举**：FUSED/UNFUSED × PADDED/UNPADDED 四值（u3-l3、u4-l1）。

几个本讲会用到的术语：

- **context**：用户给的输入 prompt（一段 input ids），是生成的起点。
- **session_len / memory_len**：session_len 是整个交互会话允许的最大时间步；memory_len 是 KV cache 实际保留的最大长度（可小于 session_len 以省显存）。
- **beam_width**：>1 走 beam search，==1 走 sampling/greedy。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| [src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.h) | `ParallelGpt` 类声明：成员字段、构造函数、`forward` 接口、它持有的三个子组件指针。 |
| [src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc) | `ParallelGpt` 实现：`initialize`、`allocateBuffer`、`forward` 主循环（编排 context 与 decoder 两阶段）。 |
| [src/fastertransformer/models/multi_gpu_gpt/ParallelGptContextDecoder.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptContextDecoder.cc) | context 阶段：一次性处理整段 prompt，使用多 token 的 `TensorParallelGptContextAttentionLayer`，并写入初始 KV cache。 |
| [src/fastertransformer/models/multi_gpu_gpt/ParallelGptDecoder.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptDecoder.cc) | decoder 阶段：每步只处理 1 个 token，使用单 query 的 `TensorParallelDecoderSelfAttentionLayer`（融合 masked MHA）。 |
| [docs/gpt_guide.md](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md) | 官方 GPT 指南：Workflow 两阶段示意图与文字说明、Optimization、Inference Options。 |

## 4. 核心概念与源码讲解

### 4.1 两阶段分裂的设计动机

#### 4.1.1 概念说明

GPT 与 BERT、encoder-decoder 不同：它接收一段 input ids 作为 **context**，再自回归地生成 response。官方指南把这件事描述为：

> GPT receive some input ids as context, and generates the respective output ids as response.

朴素做法是「一步一步生成」：第 0 步喂 prompt 的第 1 个 token，第 1 步喂第 2 个……直到 prompt 结束才开始真正生成。这样做有一个明显浪费——处理 prompt 的每一步里，self-attention 的 **query 序列长度都是 1**，但必须把已见过的 K/V 重新参与计算。更关键的是：**处理 prompt 这件事，本可以把整段 prompt 当成一个「序列长度 = max_input_length」的批次一次性算完**，从而用上 Tensor Core 友好的 cuBLAS 矩阵乘。

于是 FT 把整个生成 workflow 拆成两段：

1. **context 阶段**：一次性吃下整段 prompt，跑完所有 transformer 层，顺带把每一层的 K/V cache **写满**到 prompt 长度。
2. **decoder 阶段**：从 prompt 最后一个 token 的隐状态出发，每步只生成 1 个新 token，把新 token 的 K/V **追加**进 cache。

#### 4.1.2 核心流程

官方文档用一句话点明了这两段「操作相似、但 self-attention 的张量形状不同」，因此必须用两套实现：

> The operations of these two parts are similar, but the shapes of tensors in the `SelfAttention` is different. So, we use 2 different implementations to handle two different cases.

差异落在 query 序列长度上：

| 阶段 | query 序列长度 | 选用的自注意力实现 | 原因 |
| :--- | :--- | :--- | :--- |
| context 阶段 | `max_input_length`（多 token） | cuBLAS GEMM（`TensorParallelGptContextAttentionLayer`） | 序列长，用 Tensor Core 矩阵乘吞吐最高 |
| decoder 阶段 | 恒为 1（单 token） | custom fused masked MHA kernel（`TensorParallelDecoderSelfAttentionLayer`） | query=1 时 GEMM 退化成矩阵-向量，专用融合 kernel 反而更快 |

这就是「为什么要拆开」的根本答案：**两个阶段的最优 kernel 选择不同**。如果硬用 decoder 的单 query 融合 kernel 去处理整段 prompt，每个 token 都得串行跑一遍，完全用不上 Tensor Core 的批量矩阵乘；反之如果用 context 的 cuBLAS 路径去跑每步 1 个 token，GEMM 形状极差（M=1），同样低效。

#### 4.1.3 源码精读

文档对两阶段拆分的完整说明（含图 Fig 2）：

[FasterTransformer 把 GPT workflow 拆成 context 与 auto-regressive 两段，并解释为何用两套自注意力实现](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md#L95-L97)

> `DecoderSelfAttention` 中 query 长度恒为 1，所以用 custom fused masked multi-head attention kernel；`ContextSelfAttention` 中 query 长度等于 max input length，所以用 cuBLAS 以利用 Tensor Core。

`ParallelGpt` 把这两个实现分别委托给两个子对象，在头文件里就能看到这三件「法宝」：

[`ParallelGpt` 持有的三个子组件指针：context decoder、decoder、dynamic decode](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.h#L85-L87)

```cpp
ParallelGptDecoder<T>*        gpt_decoder_;         // decoder 阶段（逐 token）
ParallelGptContextDecoder<T>* gpt_context_decoder_; // context 阶段（整段 prompt）
DynamicDecodeLayer<float>*    dynamic_decode_layer_;// 每步选 token（beam/sampling）
```

这三者在 `initialize()` 中被 `new` 出来（详见 4.3），`forward` 的职责就是按正确顺序调度它们。

#### 4.1.4 代码实践

**实践目标**：用一张图把「两阶段 + 两种注意力」的关系固化下来。

**操作步骤**：

1. 打开 `docs/gpt_guide.md` 的 Fig 1（GPT workflow）与 Fig 2（两种 self-attention 对比）。
2. 在纸上画出如下流程：
   - 输入 `input_ids [batch, max_input_length]` → embedding → **ContextSelfAttention（query 长度 = max_input_length，走 cuBLAS）** → FFN → … → 输出整段隐状态，同时把每层 K/V 写入 cache。
   - 取 prompt 最后一个 token 的隐状态 → **DecoderSelfAttention（query 长度 = 1，走融合 masked MHA kernel）** → FFN → logits → DynamicDecode 选出下一个 token → 把新 token 的 K/V 追加进 cache → 回到下一步。
3. 在两个注意力框旁边各标注一句「为什么用这个 kernel」。

**需要观察的现象**：你会清楚地看到，KV cache 是两阶段的「交接物」——context 阶段写满它，decoder 阶段读它并向后追加。

**预期结果**：得到一张「prompt 一次过 → KV cache 写满 → 单步循环追加」的两段式流程图，并在两个注意力节点旁写明 kernel 选择理由。

#### 4.1.5 小练习与答案

**练习 1**：如果把 context 阶段也改用 decoder 的单 query 融合 kernel（逐 token 串行处理 prompt），主要损失是什么？
**答**：损失了对 Tensor Core 批量矩阵乘的利用——多 token 本可以打包成一次大 GEMM，串行化后变成 N 次小 kernel 启动 + 矩阵-向量运算，吞吐大幅下降。

**练习 2**：decoder 阶段能否改用 context 的 cuBLAS 路径？为什么 FT 不这么做？
**答**：理论上可以，但不划算。decoder 每步 query 长度恒为 1，GEMM 的 M 维为 1，cuBLAS 在这种形状下效率很低；专用融合 masked MHA kernel 把 `QK^T→softmax→PV` 压进单 kernel、让中间结果留在共享内存，反而更快。

---

### 4.2 ParallelGpt 的构造与关键字段

#### 4.2.1 概念说明

`ParallelGpt` 是一个模板类 `template<typename T>`（T 通常是 `float`/`half`/`__nv_bfloat16`），继承自 `BaseLayer`。它的「初始化配置」由一长串构造参数传入——这些参数（配合若干成员变量）就是决定模型运行期行为的关键字段，等价于其它推理框架里常见的「init params / initialized params」结构体。理解这些字段是读懂 `forward` 的前提。

注意：在当前仓库源码里，这些字段以**构造函数参数 + 成员变量**的形式直接存在于 `ParallelGpt` 中（外部 example/triton 层会把它们收集后逐个传入构造函数），并没有一个名为 `GptInitilizedParams` 的结构体定义在 `src/` 下——若你在别处看到这个名字，指的是「这一组初始化配置」的概念集合，而不是某个具体 struct。

#### 4.2.2 核心流程

关键字段分四组：

| 组别 | 字段 | 含义 |
| :--- | :--- | :--- |
| 模型结构 | `head_num_`、`size_per_head_`、`inter_size_`、`num_layer_`、`vocab_size_` | transformer 的形状参数；`hidden_units_ = head_num_ * size_per_head_`。 |
| 生成策略 | `beam_width`（从 output shape 读）、`top_k_`、`top_p_`、`temperature_`、`len_penalty_`、`repetition_penalty_` | 决定 beam search 还是 sampling；这些大多已「deprecated, move to input」（可被运行期 input 覆盖）。 |
| 并行 | `tensor_para_`、`pipeline_para_`（`NcclParam`） | TP/PP 的 rank 与 world_size。 |
| 精度/特性 | `int8_mode_`（0=FP，1=weight-only PTQ，2=w8a8）、`attention_type_`、`sparse_`、`enable_custom_all_reduce_`、`shared_contexts_ratio_` | 量化等级、注意力实现、稀疏、自定义 all-reduce、共享上下文去重比例。 |

一个重要的派生量是 `vocab_size_padded_`：为了让 TP 下 embedding 矩阵按卡均分且对齐到 8（half/bf16），它在构造函数里被向上取整。

#### 4.2.3 源码精读

构造函数签名（节选，完整签名很长）：

[`ParallelGpt` 构造函数：beam_width、head_num/size_per_head、tensor_para、pipeline_para、int8_mode、attention_type、shared_contexts_ratio 等关键字段](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.h#L187-L223)

构造函数体里对 `vocab_size_padded_` 的推导——TP 切分后按 8 对齐（half/bf16 才需要）：

[`vocab_size_padded_` = 每卡词表向上取整再乘以 TP world_size，half/bf16 下对齐到 8](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc#L343-L351)

```cpp
int local_vacab_size = ceil(vocab_size_ / 1.f / tensor_para_.world_size_);
if (std::is_same<half, T>::value
#ifdef ENABLE_BF16
    || std::is_same<__nv_bfloat16, T>::value
#endif
) {
    local_vacab_size = ceil(local_vacab_size / 8.f) * 8;  // 对齐到 8
}
vocab_size_padded_ = (size_t)local_vacab_size * tensor_para_.world_size_;
```

`int8_mode_` 的三档语义在头文件中默认为 0，在构造时由外部传入：

[`int8_mode_` 成员：0=不量化，1=weight-only PTQ，2=权重+激活量化（SmoothQuant）](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.h#L72-L73)

这三个量化档位会一路传给 context decoder 与 decoder，决定它们内部走哪条 kernel 分支（详见 u9 量化单元）。

#### 4.2.4 代码实践

**实践目标**：把构造参数与运行期行为对应起来。

**操作步骤**：

1. 打开 `ParallelGpt.h` 第 187–223 行的构造函数签名，逐个参数标注它属于上表的哪一组。
2. 打开 `docs/gpt_guide.md` 的「Constructor of GPT」表格（约 127–155 行），对照官方对每个参数的英文说明。
3. 回答：`int8_mode=2` 会让 context decoder 内部多分配哪两块 buffer？（提示：见 `ParallelGptContextDecoder.cc` 的 `allocateBuffer`，`attention_query_dynamic_scale_` 与 `ffn_intermediate_dynamic_scale_`，标注为 `int8_mode_ == 2` 时才申请）。

**需要观察的现象**：你会看到 `int8_mode` 不只是「换个数据类型」，而是会改变 buffer 分配与注意力层输入类型（`activation_in_type = int8_mode_ == 2 ? TYPE_INT8 : data_type`）。

**预期结果**：写出一份「构造参数 → 字段分组 → 运行期影响」的三列对照表。

#### 4.2.5 小练习与答案

**练习 1**：`vocab_size_ = 50257`、`tensor_para_.world_size_ = 4`、T=`half`，求 `vocab_size_padded_`。
**答**：每卡 `ceil(50257/4) = 12565`，half 下对齐到 8 得 `12568`，再乘 4 = `50272`。即 padding 多出 15 个无用词表项，换取每卡词表整除且 8 对齐。

**练习 2**：`beam_width` 不在构造函数的关键路径里被强制要求，它实际是从哪里读出来的？
**答**：从 `output_tensors->at("output_ids").shape[1]` 读出（见 `ParallelGpt.cc` 第 641 行 `const size_t beam_width = output_tensors->at("output_ids").shape[1];`）。>1 走 beam search，==1 走 sampling。

---

### 4.3 ParallelGpt::forward 主循环：两阶段的编排

#### 4.3.1 概念说明

`ParallelGpt::forward` 是整个 GPT 推理的「总指挥」。它本身不做 transformer 计算，而是：

1. 解析 input/output 张量，确定 `batch_size`、`beam_width`、`max_input_length`、`session_len`、`memory_len`；
2. 分配 KV cache 等大块 buffer；
3. **context 阶段**：如果 `max_input_length > 1`（或有 prompt learning），调一次 `gpt_context_decoder_->forward`，写满初始 KV cache；
4. **decoder 阶段**：进入 `for (step_ = step_start; step_ < gen_len; step_++)` 主循环，每步调一次 `gpt_decoder_->forward`，再做 logits GEMM 与 `dynamic_decode_layer_->forward` 选 token；
5. 收尾：`setOutputTensors` 把内部 buffer 整理成用户要的输出形状。

它对外提供两个 `forward` 重载：老的 `std::vector<Tensor>` 版本只是把向量包装成 `TensorMap` 后转调 `TensorMap` 版本。

#### 4.3.2 核心流程

主循环的伪代码（省略 prompt learning、pipeline 通信、shared context 等分支）：

```
forward(output, input, weights):
    解析 batch_size / beam_width / max_input_length
    确定 session_len / memory_len
    allocateBuffer(...)              # 申请 KV cache、logits buffer 等
    dynamic_decode_layer_->setup()  # 准备采样表

    # ===== Context 阶段（仅当 max_input_length > 1 等）=====
    if max_input_length > 1 or has_prompt:
        embedding_lookup(整段 prompt)              # [batch*beam, max_input_len, hidden]
        build attention_mask
        gpt_context_decoder_->forward(             # 一次跑完所有层
            out={decoder_output, key_cache, value_cache, last_token_hidden},
            in ={decoder_input, attention_mask, input_lengths},
            weights)
        invokeDecodingInitialize(...)              # 初始化 finished/sequence_length

    # ===== Decoder 阶段（逐 token）=====
    step_start = continue_gen ? initial_step : max_input_length
    for step_ = step_start .. gen_len-1:
        embedding_lookup(上一步选出的 token id)     # [batch*beam, hidden]（单 token）
        gpt_decoder_->forward(                     # 单步、追加 K/V 到 cache
            out={decoder_output, key_cache, value_cache},
            in ={decoder_input, finished, step, masked_tokens, ...},
            weights)
        logits = GEMM(decoder_output, embedding_kernel)   # 投影到词表
        if TP>1: ftNcclAllGather + transpose 拼回完整 logits
        dynamic_decode_layer_->forward(...)        # beam/sampling 选 token，更新 finished
        if 所有 microbatch 都停了: break

    setOutputTensors(...)                          # 整理输出（gather_tree 等）
```

两个关键的时间概念：

- `step_start`：context 阶段已经把 prompt 的 `0..max_input_length-1` 步写进了 cache，所以 decoder 从 `step = max_input_length` 开始；交互式续写（`continue_gen`）时从上次的 `step_` 接着来。
- `gen_len`：用户要求生成的总长度（含 prompt）。循环跑到 `gen_len` 或全部 `finished` 即停。

KV cache 的形状在这里被确定，它是两阶段共享的「交接缓冲」：

```text
key_cache   : [num_layer / PP, batch*beam, local_head_num, size_per_head/x, memory_len, x]
value_cache : [num_layer / PP, batch*beam, local_head_num, memory_len, size_per_head]
```

其中 `local_head_num = head_num / TP`，`x = 16/sizeof(T)`（half 时为 8），是 Tensor Core 友好的分块常量。

#### 4.3.3 源码精读

`forward` 的 `TensorMap` 重载是真正的实现，开头先做参数校验与维度解析：

[`forward` 入口：校验 input/output 张量并解析 batch_size、beam_width、max_input_length](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc#L627-L645)

KV cache 的形状定义（context 与 decoder 共用同一块 `key_cache_`/`value_cache_`）：

[self_k_cache_shape / self_v_cache_shape：两阶段共用的 KV cache 布局](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc#L808-L815)

context 阶段的入口条件——「有 prompt 或输入长度 > 1」才需要跑 context decoder：

[context 阶段的触发条件：has prompt 或 max_input_length > 1](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc#L955-L956)

context decoder 的调用——注意输出里同时带 `key_cache`/`value_cache`（写满 prompt 段）与 `last_token_hidden_units`（交给 decoder 做下一步输入）：

[context 阶段：把整段 prompt 交给 gpt_context_decoder_，输出含 key_cache/value_cache 与最后 token 的隐状态](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc#L1090-L1091)

```cpp
gpt_context_decoder_->forward(
    &decoder_output_tensors, &decoder_input_tensors, &gpt_weights->decoder_layer_weights);
```

decoder 阶段的起点 `step_start`：

[step_start：context 阶段已写入 0..max_input_length-1，decoder 从此接续](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc#L1192-L1192)

decoder 主循环：

[decoder 主循环 for (step_ = step_start; step_ < gen_len; step_++)](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc#L1201-L1201)

循环内先做单 token 的 embedding lookup（仅 rank 0，因为词表查找轻量）：

[decoder 每步：对上一步选出的 token id 做 embedding lookup 得到单 token 隐状态](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc#L1265-L1278)

然后调单步 decoder，输出里的 `key_cache`/`value_cache` 会在正确的 `step` 槽位被追加：

[decoder 每步：调 gpt_decoder_->forward，单 token 前向并追加 K/V 到 cache](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc#L1346-L1347)

随后是 logits GEMM（投影到词表，TP>1 时 all-gather + transpose 拼回完整词表）：

[logits GEMM：把 decoder 隐状态投影到 vocab_size_padded（TP=1 路径）](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc#L1374-L1391)

最后是动态解码选 token（细节留 u8）：

[dynamic_decode_layer_->forward：beam search/sampling 选出下一个 token，更新 finished](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc#L1501-L1501)

early stopping——所有 microbatch 都停了就跳出循环：

[generation_should_stop 为真时 break，提前结束生成](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc#L1578-L1580)

#### 4.3.4 代码实践

**实践目标**：在源码里把两阶段的「交接点」逐行标出来。

**操作步骤**：

1. 在 `ParallelGpt.cc` 中定位三处：
   - context decoder 调用（约 1090 行）；
   - `step_start` 定义（约 1192 行）；
   - decoder 调用（约 1346 行）。
2. 在每一处旁边用注释写：「这里写/读 KV cache 的哪一段」。
3. 思考：为什么 context 阶段在循环外只调一次，而 decoder 阶段在循环内每步调一次？

**需要观察的现象**：context 调用的输出 `TensorMap` 里 `key_cache`/`value_cache` 的形状第 0 维是 `num_layer_/PP`，说明它一次写满所有层；decoder 调用同样带这两个张量，但每步只写入 `step_` 对应的一个槽位（由 decoder 内部按 `step` 偏移定位）。

**预期结果**：你能在源码上清晰指出「prompt 段 cache 在第 1090 行被写满，生成段 cache 在第 1346 行被逐步追加」，并用 `step_start` 解释两段不重叠。

#### 4.3.5 小练习与答案

**练习 1**：若 `max_input_length == 1`（用户只给 1 个起始 token），context 阶段还会跑吗？
**答**：不会走 `gpt_context_decoder_` 那条主分支（条件 `max_input_length > 1` 不满足），代码会落到 `else if (max_input_length == 1)` 分支，仅做 `invokeDecodingInitialize` 与 tile 输入，然后直接进入 decoder 循环。见 `ParallelGpt.cc` 第 1149 行附近。

**练习 2**：decoder 主循环里 `fill_caches_only`（第 1204 行）是什么意思？
**答**：它是交互式续写（`continue_gen`）时的一种特殊状态——当 `step_ < max_context_len` 时，本轮的新输入其实仍属于上轮未处理完的 context，此时只把 K/V 填进 cache、不做 logits 投影与采样（`generation_should_stop` 被屏蔽），等过了 context 段才真正开始生成。

---

### 4.4 ContextDecoder 与 Decoder：两种自注意力的分工

#### 4.4.1 概念说明

`ParallelGptContextDecoder` 和 `ParallelGptDecoder` 的整体结构很像：都是「对每一层做 pre-LN → self-attention → 残差+LN → FFN → 残差」，都支持 adapter、MoE、TP/PP。它们的**唯一本质差异**在 self-attention 层用的实现不同，这正是 4.1 所说「kernel 选择不同」的落地。

- **ContextDecoder** 用 `TensorParallelGptContextAttentionLayer`：query 是 `max_input_length` 个 token，走 cuBLAS 批量 GEMM，能 remove-padding（UNPADDED MHA），还能做 shared contexts 去重。
- **Decoder** 用 `TensorParallelDecoderSelfAttentionLayer`：query 恒为 1 个 token，走 u3-l2 的融合 masked MHA kernel，靠 `step` 定位 cache 写入槽位。

#### 4.4.2 核心流程

两者 `forward` 的输入张量形状对比一目了然：

| 项 | ContextDecoder 输入 | Decoder 输入 |
| :--- | :--- | :--- |
| `decoder_input` | `[batch, seq_len, hidden]`（整段 prompt） | `[local_batch_size, hidden]`（单 token，无 seq 维） |
| 步进控制 | 无 `step`，整段一次算 | 有 `step`（CPU 标量），定位 cache 写入槽 |
| 额外输入 | `attention_mask [batch,1,seq,seq]`、可选 `padding_offset`/`cu_seqlens` | `finished`、`sequence_lengths`、`masked_tokens`、`total_padding_tokens` |
| 输出 | `decoder_output [batch,seq,hidden]` + `key_cache`/`value_cache`（写满 prompt 段）+ `last_token_hidden_units` | `decoder_output [local_batch,hidden]` + `key_cache`/`value_cache`（追加 1 步） |

ContextDecoder 还多两件 Decoder 没有的事：

1. **remove padding**：进层前 `invokeRemovePadding` 把有效 token 压紧、出层后 `invokeRebuildPadding` 散回（承接 u4-l2）。
2. **last token 提取**：因为 prompt 段的 `decoder_output` 形状是 `[batch, seq, hidden]`，而 decoder 阶段只需要最后一个 token 的隐状态，所以 context decoder 结束时用 `invokeLookupHiddenStateOfLastToken` 把它挑出来交给 decoder。

#### 4.4.3 源码精读

ContextDecoder 在 `initialize` 里创建多 token 的注意力层：

[ContextDecoder 用 TensorParallelGptContextAttentionLayer（多 token，走 cuBLAS）](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptContextDecoder.cc#L29-L43)

ContextDecoder 的 `forward` 签名与张量约定（注意 `decoder_input` 带 `seq_len` 维）：

[ContextDecoder::forward：输入 decoder_input [batch, seq_len, hidden]，输出含 key/value_cache 与 last_token_hidden_units](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptContextDecoder.cc#L308-L333)

ContextDecoder 的逐层循环——第一层前视情况 remove padding：

[ContextDecoder 逐层循环；UNPADDED MHA 时在第 0 层前 invokeRemovePadding 压紧 token](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptContextDecoder.cc#L421-L439)

ContextDecoder 的自注意力输出——同时写 `key_cache`/`value_cache`（整段 prompt）：

[ContextDecoder 自注意力输出张量：hidden_features + 整段 prompt 的 key_cache/value_cache](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptContextDecoder.cc#L539-L546)

ContextDecoder 收尾——从整段隐状态里挑出最后 token 的隐状态交给 decoder：

[ContextDecoder 用 invokeLookupHiddenStateOfLastToken 提取最后 token 隐状态](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptContextDecoder.cc#L836-L845)

对比之下，Decoder 在 `initialize` 里创建的是单 query 的注意力层：

[Decoder 用 TensorParallelDecoderSelfAttentionLayer（单 token，融合 masked MHA kernel）](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptDecoder.cc#L27-L39)

Decoder 的 `forward`——`decoder_input` 没有 seq 维，只有 `[local_batch_size, hidden]`：

[Decoder::forward：单 token 输入 [local_batch_size, hidden]，靠 step 定位 cache 写入槽](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptDecoder.cc#L257-L294)

Decoder 的逐层循环与 K/V 写入——每步只追加 1 个时间步到 cache：

[Decoder 逐层循环；自注意力输出 key_cache/value_cache 仅追加当前 step 的 1 个 token](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptDecoder.cc#L316-L347)

正因为每步只算 1 个 token，Decoder 单步开销远小于 ContextDecoder 一次跑完整段 prompt——这也是大模型推理中「第一步延迟（context）远高于后续每步延迟（decoder）」的根因。

#### 4.4.4 代码实践

**实践目标**：用源码证据说明「两个 decoder 不可互换」。

**操作步骤**：

1. 分别打开 `ParallelGptContextDecoder.cc` 第 29 行与 `ParallelGptDecoder.cc` 第 27 行，对比二者 `new` 的注意力层类型。
2. 在 `ParallelGptContextDecoder.cc` 找到 `decoder_input` 的形状断言（含 `seq_len` 维），在 `ParallelGptDecoder.cc` 找到它的形状（不含 `seq_len` 维）。
3. 回答：如果把 `gpt_decoder_` 误传给 context 阶段调用，会在哪一行因为形状不匹配而出错？

**需要观察的现象**：ContextDecoder 的输入张量第 1 维是 `seq_len`（多 token），Decoder 的输入张量第 0 维直接是 `local_batch_size`（单 token 隐状态）。两者的 self-attention 层也分别面向「多 token 批量 GEMM」与「单 query 融合 kernel」。

**预期结果**：你能写出一句结论——「ContextDecoder 与 Decoder 的输入张量形状、self-attention 实现都不同，因此它们分别绑定到 context 阶段与 decoder 阶段，不可互换；这正是 4.1 所述 kernel 选择不同的具体落地。」

#### 4.4.5 小练习与答案

**练习 1**：ContextDecoder 输出的 `last_token_hidden_units` 为什么是必须的？
**答**：ContextDecoder 一次算完整段 prompt，输出形状是 `[batch, seq_len, hidden]`；而 decoder 阶段只需要 prompt 最后一个 token 的隐状态作为下一步输入。所以 context decoder 用 `invokeLookupHiddenStateOfLastToken` 把它挑出来，避免 decoder 阶段再去整段隐状态里翻找。

**练习 2**：ContextDecoder 支持 remove padding，Decoder 为什么不需要？
**答**：Decoder 每步只处理 1 个 token（query 长度恒为 1），本来就没有「序列内 padding」的概念；padding 的问题只出现在「一条序列里有多个 token、短句被补齐」的场景，即 context 阶段。所以 remove padding 只在 ContextDecoder 里出现。

---

## 5. 综合实践

把本讲全部内容串起来，完成下面这个「两阶段流程追踪」任务：

1. **画图**：画一张完整的 `ParallelGpt::forward` 数据流图，要求包含：
   - 输入 `input_ids [batch, max_input_length]`；
   - embedding → **ContextDecoder**（标注：多 token、cuBLAS、remove padding、写满 KV cache 的 `0..max_input_length-1` 段、输出 `last_token_hidden_units`）；
   - `step_start = max_input_length` 处的循环入口；
   - **Decoder 循环**（标注：单 token、融合 masked MHA、每步追加 1 个时间步到 cache、logits GEMM、DynamicDecode 选 token）；
   - early stop 与 `setOutputTensors` 收尾。
2. **标注交接物**：在图上用高亮标出 KV cache，并写明它「由 context 写满、由 decoder 追加」。
3. **解释拆分动机**：在图旁用两句话写清「为什么必须拆两段」——分别从 query 序列长度与最优 kernel 选择的角度。
4. **源码对齐**：在图上每个框旁标注对应的源码行号（context 调用 ≈ `ParallelGpt.cc:1090`、`step_start` ≈ `ParallelGpt.cc:1192`、decoder 调用 ≈ `ParallelGpt.cc:1346`、dynamic decode ≈ `ParallelGpt.cc:1501`）。

> 提示：第 1、2、4 步都是源码阅读型任务，不需要 GPU 即可完成；第 3 步的答案可以直接引用 4.1.2 的对比表。如果你有可运行的 FT 环境，还可以用 `FT_NVTX=ON` 跑一次 `multi_gpu_gpt_example`，在 Nsight Systems 时间轴上观察「第一步（context）明显慢于后续每步（decoder）」的现象来验证你的图。

## 6. 本讲小结

- FT 把 GPT 生成拆成 **context 阶段**（一次处理整段 prompt、写满 KV cache）与 **decoder 阶段**（逐 token 自回归、追加 KV cache），根本原因是两个阶段 query 序列长度不同、**最优 kernel 选择不同**。
- context 阶段 query 长度 = `max_input_length`，走 cuBLAS 批量 GEMM（`TensorParallelGptContextAttentionLayer`）；decoder 阶段 query 长度恒为 1，走融合 masked MHA kernel（`TensorParallelDecoderSelfAttentionLayer`）。
- `ParallelGpt` 是编排者，持有 `gpt_context_decoder_`、`gpt_decoder_`、`dynamic_decode_layer_` 三件法宝；其构造参数（`beam_width`、`tensor_para`、`int8_mode`、`attention_type`、`shared_contexts_ratio` 等）决定运行期行为。
- KV cache 是两阶段的交接缓冲：形状为 `[num_layer/PP, batch*beam, local_head_num, ..., memory_len, ...]`，context 写满 `0..max_input_length-1`，decoder 从 `step_start = max_input_length` 起追加。
- `forward` 主循环 `for (step_ = step_start; step_ < gen_len; step_++)` 内每步做：embedding lookup → 单步 decoder → logits GEMM（TP>1 时 all-gather）→ dynamic decode 选 token → 检查 early stop。
- ContextDecoder 比 Decoder 多两件事：remove padding（压紧/散回有效 token）与 last token 提取（把最后 token 隐状态挑出来交给 decoder）。

## 7. 下一步学习建议

- **u6-l2 KV Cache 机制与拼装**：本讲只讲了 cache 的形状与「写满/追加」的时序，下一讲深入 cache 在 beam search 下按 beam id 重排（`cache_indirections_`、`invokeTransposeAxis01` 等）的细节。
- **u6-l3 交互式生成与共享上下文**：本讲提到的 `continue_gen`、`session_len`/`memory_len`、`shared_contexts_ratio_` 与 `invokeFindContextDups`/`invokeCompactInputs` 将在那里展开。
- **u8 动态解码**：`DynamicDecodeLayer` 如何在 beam search / top-k / top-p 间分发，是本讲每步「选 token」环节的深入。
- **延伸阅读**：直接对照 `docs/gpt_guide.md` 的「Optimization」与「Inference Options」两节，以及 `images/gpt/gpt.png`、`gpt_context.png`、`parallelgpt.png` 三张官方配图，把本讲自己画的图与官方图对照修正。
