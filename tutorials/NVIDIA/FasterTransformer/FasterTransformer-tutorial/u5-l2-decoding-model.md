# Decoding 模型：端到端 beam search 与 sampling

## 1. 本讲目标

本讲聚焦 FasterTransformer（下称 FT）的 `Decoding` 类——**端到端自回归生成**的最小完整单元。学完本讲，读者应能够：

- 说清 `Decoder`（单步解码块，u5-l1）与 `Decoding`（端到端生成流程）的分工差异；
- 沿着 `Decoding::forward` 的「逐步生成主循环」走完一遍：embedding 查表 → 调用 `Decoder` 前向 → LayerNorm → logits GEMM → 动态解码；
- 解释 `beam_width > 1` 走 beam search、`beam_width == 1` 走 sampling 的分流机制，以及 `DynamicDecodeLayer` 如何作为统一接入点；
- 理解 `finished` 状态数组如何驱动 early stopping，跳过已完成序列的后续计算；
- 看懂 `decoding_gemm` 工具如何离线调优 6 个 GEMM 形状、生成 `gemm_config.in`。

本讲承接 u5-l1（`Decoder` 单步前向）与 u4-l2（去除 padding 的思想），是后续 u6（GPT/ParallelGpt 大模型推理）与 u8（动态解码策略）的直接前置。

## 2. 前置知识

### 2.1 自回归生成（autoregressive generation）

序列生成模型不能一次输出整句，而是**一个 token 一个 token 地生成**：每一步把「上一步生成的 token」喂回模型，得到「下一个 token 的概率分布」，再从中采样或挑选一个。循环往复，直到遇到结束符 `<eos>`（FT 里叫 `end_id`）或达到最大长度。

这与 u5-l1 的 `Decoder` 直接对应：`Decoder::forward` 一次只吃一个 token 的隐状态 `[batch, hidden]`，外层循环反复调用它就构成了「生成」。

### 2.2 从 logits 选 token 的两条路

模型每步输出的是整个词表上的未归一化分数 `logits`（形状 `[batch, vocab_size]`）。从 logits 选出下一个 token 主要有两类策略：

- **beam search（束搜索）**：同时保留 `beam_width` 条「当前最优部分序列」，每步从 `beam_width × vocab_size` 个候选里挑出新的 `beam_width` 条。确定性、偏贪心，适合翻译等有标准答案的任务。
- **sampling（采样）**：只保留 1 条序列，把 logits 转成概率后按概率随机抽样。又分 top-k（只在前 k 大概率里抽）和 top-p/nucleus（在累积概率达到 p 的最小集合里抽）。随机、富多样性，适合开放式文本生成。

FT 的设计是：**`beam_width > 1` 自动走 beam search，`beam_width == 1` 走 sampling**。这个分流就发生在 `Decoding` 里。

### 2.3 与 u5-l1 的衔接

u5-l1 讲的是 `Decoder`——一个 transformer 解码块的单步前向（self-attn → cross-attn → FFN），以及它如何把 K/V 写进 cache。本讲的 `Decoding` 则是「**循环驱动 `Decoder` + 每步选 token + 维护 `finished`**」的编排者。可以粗略类比为：

- `Decoder` = 引擎的一个气缸点火；
- `Decoding` = 整台发动机的曲轴 + 喷油 + 点火正时控制，把单步点火串成持续运转。

### 2.4 名词澄清：Decoder / Decoding / GPT

这三个名字在 FT 里容易混淆（`docs/decoder_guide.md` 的 Introduction 也特别提醒）：

| 名字 | 含义 | 源码位置 |
|------|------|----------|
| **Decoder** | 单个 transformer 解码块（含 self+cross attention） | `models/decoder/Decoder.cc` |
| **Decoding** | 端到端生成流程（循环 + 选 token） | `models/decoding/Decoding.cc` |
| **GPT** | 无 cross-attention 的纯解码生成（u6 讲） | `models/multi_gpu_gpt/` |

本讲的 `Decoding` 是 **encoder-decoder 架构**（如翻译）的端到端生成，输入需要 encoder 输出（memory）；GPT 是它的「无 memory」简化版，留到 u6。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/fastertransformer/models/decoding/Decoding.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoding/Decoding.h) | `Decoding` 类声明、成员缓冲区列表、`fallBackType` 模板（BF16 动态解码回退 FP32） |
| [src/fastertransformer/models/decoding/Decoding.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoding/Decoding.cc) | **本讲核心**：`initialize`、`allocateBuffer`、`forward` 生成主循环、收尾（gather_tree / 拷贝） |
| [src/fastertransformer/models/decoding/decoding_gemm.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoding/decoding_gemm.cc) | `decoding_gemm` 工具入口：解析参数、按数据类型分发到调优函数 |
| [src/fastertransformer/utils/gemm_test/decoding_gemm_func.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm_test/decoding_gemm_func.cc) | 真正的调优逻辑：枚举 6 个 GEMM 形状、遍历 cuBLAS/cuBLASLt 算法、挑最快写入 `gemm_config.in` |
| [src/fastertransformer/kernels/decoding_kernels.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoding_kernels.cu) | `decodingInitialize` 等辅助 kernel：初始化 `finished`、`output_ids`、`cum_log_probs` |
| [src/fastertransformer/layers/DynamicDecodeLayer.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/DynamicDecodeLayer.h) | 动态解码层声明：在 beam search / top-k / top-p 间运行期分发（细节留到 u8-l1） |
| [examples/cpp/decoding/decoding_example.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/decoding/decoding_example.cc) | C++ 示例：如何构造 `Decoding`、组装输入输出张量、warmup+计时 |
| [docs/decoder_guide.md](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/decoder_guide.md) | 官方指南：模型架构、Decoding 参数说明、运行命令 |

## 4. 核心概念与源码讲解

### 4.1 Decoding 是什么：端到端生成流程与初始化

#### 4.1.1 概念说明

`Decoding` 把「生成一整句」这件事封装成一次 `forward` 调用。从外部看，它的接口非常简洁：

- **输入**：encoder 的输出 `encoder_output`（即 memory，形状 `[batch*beam, mem_seq_len, mem_hidden]`）和源句长度 `encoder_sequence_length`。
- **输出**：`output_ids`（生成的 token 序列，形状 `[max_seq_len, batch, beam]`）、`parent_ids`（beam search 回溯用的父指针）、`sequence_length`（每条序列最终长度）。

但在内部，这一次 `forward` 实际上跑了一个 `for (step = 1; step < max_seq_len; step++)` 的循环，每步都：① 查 embedding ② 调一次 `Decoder::forward` ③ 算 logits ④ 选 token。

`Decoding` 的成员可以分成三块：

1. **子模块**：一个 `Decoder*`（单步解码器）和一个 `DynamicDecodeLayer*`（选 token 的策略层）。
2. **每步工作区**：`decoder_input_buf_`、`decoder_output_buf_`、`logits_buf_`、`finished_buf_` 等。
3. **跨步缓存**：`key_cache_`/`value_cache_`（self-attention 的 K/V cache）、`key_mem_cache_`/`value_mem_cache_`（cross-attention 的 memory cache）。

#### 4.1.2 核心流程

`Decoding` 的生命周期是标准的 FT 模型套路（承接 u3-l5 的 BaseLayer）：

```
构造函数  →  initialize()        // new 出 Decoder 和 DynamicDecodeLayer
forward() →  allocateBuffer()    // 按需申请所有工作区与 cache
          →  主循环（见 4.2）
          →  freeBuffer()
析构      →  delete decoder_ / dynamic_decode_layer_
```

一个关键细节：**BF16 模型会让动态解码层在 FP32 下运行**。这是通过 `fallBackType` 模板实现的——beam search/sampling 的 topk/排序对数值精度敏感，FT 选择把这部分固定在 FP32。

#### 4.1.3 源码精读

`fallBackType` 把动态解码的计算类型从模型类型 `T` 中分离出来：

[Decoding.h:L30-L38](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoding/Decoding.h#L30-L38) — 主模板把任意 `T`（含 `float` 与 `__nv_bfloat16`）映射到 `float`，只有 `half` 的特化保留 `half`。于是 `DynamicDecodeType` 在 BF16 模型下变成 `float`，注释 `fallback to fp32 dynamic decoder when bf16 specified` 即指此。

`initialize()` 创建两个子模块，注意 `Decoder` 的 `max_batch_size` 被放大了 `beam_width`` 倍：

[Decoding.cc:L28-L48](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoding/Decoding.cc#L28-L48) — 第 30 行 `max_batch_size_ * beam_width_`：beam search 时每条源句要同时维护 `beam_width` 条候选序列，所以「有效 batch」是 `batch × beam`。后续所有缓冲区都按 `batchxbeam` 计算。`DynamicDecodeLayer` 只需要词表大小和 `end_id`，因为它的职责就是「给 logits，选 token」。

`allocateBuffer()` 一次性申请全部工作区，体现「跨步复用、绝不每步 malloc」的思想（承接 u2-l2/u3-l5）：

[Decoding.cc:L51-L100](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoding/Decoding.cc#L51-L100) — 重点看几行：
- L55-57：`self_cache_size = num_layer * batchxbeam * max_seq_len * hidden_units`——self-attention 的 K/V cache 覆盖所有层、所有步；
- L83-91：四块大 cache（`key_cache_`/`value_cache_` 给 self-attn，`key_mem_cache_`/`value_mem_cache_` 给 cross-attn）；
- L85-89：仅当 `beam_width > 1` 才申请 `cache_indirections_[2]`——这是 beam search 专用的「cache 重排索引表」，双缓冲（两块交替使用），sampling 不需要。

#### 4.1.4 代码实践

**实践目标**：从外部看清 `Decoding` 的「外形尺寸」。

**操作步骤**：
1. 打开 [examples/cpp/decoding/decoding_example.cc:L157-L180](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/decoding/decoding_example.cc#L157-L180)，看 `Decoding<T>` 的构造实参。
2. 数一下构造参数里哪些是「模型结构」（head_num/size_per_head/inter_size/num_layer/vocab_size）、哪些是「解码策略」（beam_width/top_k/top_p/temperature/len_penalty/repetition_penalty）。

**需要观察的现象**：构造参数多达 21 个，但 `docs/decoder_guide.md` 指出「argument 5~11 是模型超参，确定后固定；18~22 是 CUDA 设置，也固定」——真正运行期会变的只有 beam_width 和几个采样参数。

**预期结果**：能说出 `Decoding` 把「**静态模型结构**」和「**动态解码策略**」同时塞进了构造函数（这是较老的设计；后续 ParallelGpt 改用运行期 `runtime_args`，见 u8-l1）。

**待本地验证**：示例中 `top_k=0, top_p=0.6, beam_width=1` 同时给出，实际生效的是哪个由 `DynamicDecodeLayer` 内部判断（top_k 非 0 走 top-k，否则 top_p 非 0 走 top-p）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Decoder` 的 `max_batch_size` 要乘以 `beam_width`？
**答案**：beam search 同时维护 `beam_width` 条候选序列，每条都独立跑一次单步解码，所以「有效并行度」是 `batch × beam`。

**练习 2**：BF16 模型下，`logits_buf_` 的元素类型是 `__nv_bfloat16` 还是 `float`？
**答案**：是 `float`。因为 `logits_buf_` 声明为 `DynamicDecodeType*`，而 BF16 经 `fallBackType` 回退到 `float`。

---

### 4.2 生成主循环：Decoding::forward 逐步推进

#### 4.2.1 概念说明

这是本讲的核心模块。`Decoding::forward(TensorMap*, TensorMap*, const DecodingWeight*)` 是真正的生成入口，它的主体是一个 `step` 循环。每一步都执行固定的 5 个动作：

1. **early stopping 判定**：若所有序列都 `finished`，提前跳出循环。
2. **embedding 查表**：用上一步选出的 `output_ids_buf_` 查词向量，加位置编码，得到本步 decoder 输入。
3. **Decoder 前向**：调用 u5-l1 的 `Decoder::forward`，产出隐状态并更新 K/V cache。
4. **logits 计算**：LayerNorm + 一次 GEMM，把隐状态映射到词表维度。
5. **动态解码**：把 logits 交给 `DynamicDecodeLayer`，选出本步 token、更新 `finished` 与 `cum_log_probs`。

#### 4.2.2 核心流程

主循环的伪代码（省略类型与边界）：

```
invokeDecodingInitialize(...)         // finished=false, output_ids=start_id, cum_log_probs 初始化
for step in 1 .. max_seq_len-1:
    # ---- ① early stopping ----
    把 finished_buf_ 拷回 CPU，求和
    if sum == batch*beam: break

    # ---- ② embedding 查表 ----
    decoder_input = EmbeddingLookup(output_ids_buf_[step-1]) + position_encoding[step-1]

    # ---- ③ Decoder 前向（更新 K/V cache 与 memory cache）----
    decoder_output, key_cache, value_cache, key_mem_cache, value_mem_cache =
        decoder.forward(decoder_input, encoder_output, finished, step, ...)

    # ---- ④ logits GEMM ----
    normed = LayerNorm(decoder_output, post_decoder_layernorm)
    logits = normed @ embedding_kernel^T   # [batch*beam, vocab_size_padded]

    # ---- ⑤ 动态解码（beam search 或 sampling）----
    output_ids_buf_[step], finished, parent_ids[step], cum_log_probs =
        dynamic_decode.forward(logits, finished, step, ...)

# ---- 收尾 ----
invokeMinusUnfinishedSeqlen(...)       # 修正未完成序列的长度计数
if beam_width > 1: invokeGatherTree(...)  # 回溯出每条 beam 的完整路径
else: 拷贝 output_ids
```

注意 `step` 从 1 开始（`output_ids_buf_` 的第 0 步存的是 `start_id`），到 `max_seq_len_ - 1` 结束——`max_seq_len_` 在构造时被加了 1（`max_seq_len + 1`，见 [Decoding.cc:L203](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoding/Decoding.cc#L203) 的注释 `allocater additional one to put the start token`），专门留一格放起始符。

#### 4.2.3 源码精读

主循环起点与 5 个动作的对应行号：

[Decoding.cc:L389-L401](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoding/Decoding.cc#L389-L401) — 循环头 `for (int step = 1; step < (int)max_seq_len_; step++)`；紧接着的 `cudaD2Hcpy` 把 `finished_buf_` 从 GPU 拷回 `h_finished_buf_`，CPU 上求和判定是否全部完成。这是**每步唯一的 CPU↔GPU 同步点**，因为 early stopping 的决策必须在 host 上做。

[Decoding.cc:L406-L418](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoding/Decoding.cc#L406-L418) — `invokeEmbeddingLookupPosEncodingPadCount`：用 `output_ids_buf_`（上一步选出的 token）查 `pre_decoder_embedding_table`，叠加 `position_encoding_table` 的第 `step-1` 行，乘 `sqrt(hidden_units)` 缩放，写入 `decoder_input_buf_`。

[Decoding.cc:L420-L444](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoding/Decoding.cc#L420-L444) — 组装 `decoder_input_tensors`（含 `finished_buf_`、`step`、`cache_indirections_`）与 `decoder_output_tensors`（含 4 块 cache 指针），调用 `decoder_->forward`。这一步复用了 u5-l1 讲过的单步 `Decoder`，K/V 被写进 cache 的第 `step` 槽位。

[Decoding.cc:L446-L456](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoding/Decoding.cc#L446-L456) — `invokeGeneralLayerNorm` 对 decoder 输出做最终 LayerNorm（用 `post_decoder_layernorm` 权重），结果进 `normed_decoder_output_buf_`。

[Decoding.cc:L458-L513](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoding/Decoding.cc#L458-L513) — logits GEMM，分 BF16 与非 BF16 两条路：
- BF16 分支（L464-L499）：用低精度输入 `CUDA_R_16BF`、**FP32 累加与输出**（`CUDA_R_32F`），再用 `invokeGenericActivation<IdentityActivation>` 把 embedding bias 加上；
- 非 BF16 分支（L501-L513）：直接用 `cublas_wrapper_->Gemm` 的 8 参重载。两条路都把 hidden 维（`hidden_units_`）投影到 `vocab_size_padded_` 维。

[Decoding.cc:L518-L559](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoding/Decoding.cc#L518-L559) — 把 logits、`finished`、`step`、各种采样参数打包成 `TensorMap`，调用 `dynamic_decode_layer_->forward`。输出侧写回 `output_ids_buf_`、`finished_buf_`、`parent_ids_buf_`、`cum_log_probs_` 以及 `tgt_cache_indirections`（下一步要用的 cache 重排表）。这一步的内部细节是 u8-l1 的主题。

> 关于 `vocab_size_padded_`：当 `T = half` 时，词表维度会被向上取整到 8 的倍数（[Decoding.cc:L221-L224](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoding/Decoding.cc#L221-L224)），目的是让 logits GEMM 的 N 维对齐到 tensor core 友好的宽度；若实际 `vocab_size` 不是 8 的倍数，`invokePaddingEmbedding`（L369-L377）会先把 embedding 权重补齐到 padded 维。

#### 4.2.4 代码实践

**实践目标**：把主循环的 5 个动作与源码行号一一对应。

**操作步骤**：
1. 打开 [Decoding.cc:L389-L560](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoding/Decoding.cc#L389-L560)。
2. 准备一张表，列出「动作 / 关键函数 / 起止行号 / 读了哪个缓冲 / 写了哪个缓冲」5 列。
3. 重点标注数据依赖：`decoder_input_buf_` → `decoder_output_buf_` → `normed_decoder_output_buf_` → `logits_buf_` 这条单向流水。

**需要观察的现象**：每步只读写同一组缓冲区（`decoder_input_buf_` 等），没有任何 `cudaMalloc`/`cudaFree`——所有显存在循环前由 `allocateBuffer` 一次性申请，循环内纯计算。

**预期结果**：得到一条清晰的「单步数据流」，并能解释为什么把 `step` 作为 CPU 标量传给 `Decoder`（用来定位 K/V cache 的写入槽位，承接 u5-l1）。

**待本地验证**：用 `FT_DEBUG_LEVEL=DEBUG` 运行 `decoding_example`，观察 `sync_check_cuda_error` 在每步插入的同步点（仅调试模式）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 early stopping 的求和判断必须在 CPU 上做，而不能写成一个 GPU kernel？
**答案**：因为「是否 break 出循环」是控制流决策，必须由 host（CPU）发起或终止 kernel 序列。FT 选择把 `finished_buf_` 拷回 host 求和，用 `if (sum == ...) break` 控制 for 循环。

**练习 2**：`max_seq_len_` 为什么比用户传的 `max_seq_len` 大 1？
**答案**：多出的一格用来存起始符 `start_id`（`output_ids_buf_` 的第 0 步），生成从 `step=1` 开始。

---

### 4.3 解码策略接入与 finished 控制

#### 4.3.1 概念说明

主循环的第 ⑤ 步把 logits 交给 `DynamicDecodeLayer`。这一层是「策略接入点」：它在内部同时持有 4 个后端——`online_beamsearch_decode_`、`beamsearch_decode_`、`topk_decode_`、`topp_decode_`，根据运行期参数动态选择启用哪个（细节是 u8-l1/u8-l2/u8-l3 的主题，本讲只看 `Decoding` 如何接入）。

至于「beam search vs sampling」的总分流，`Decoding` 用 `beam_width` 来决定：
- `beam_width > 1` → beam search，输出需要 `invokeGatherTree` 回溯路径；
- `beam_width == 1` → sampling，输出直接拷贝。

`finished` 是一个 `[batch*beam]` 的 `bool` 数组，记录每条序列是否已生成 `end_id`。它有两个作用：① 驱动 early stopping（4.2）；② 让已完成的序列在后续步骤里「跳过」无效计算。

#### 4.3.2 核心流程

**初始化阶段**（循环前）由一个 kernel 一次性写好：

```
invokeDecodingInitialize:
    finished[i]          = false
    sequence_length[i]   = max_input_length   // Decoding 里 max_input_length=0
    output_ids[i]        = start_id
    cum_log_probs[i]     = (i % beam_width == 0) ? 0.0 : -MAX
```

beam search 下 `cum_log_probs` 的初始化很巧妙：每条源句的第 0 条 beam 给 `0.0`，其余 `beam_width-1` 条给 `-MAX`（负无穷）。这样第一步选 token 时，同一个 `(batch, token)` 候选不会被重复选中 `beam_width` 次——只有第 0 条 beam 有有效累积概率，其余被压成「哑 beam」。

**每步**：`DynamicDecodeLayer::forward` 接收 `finished`，内部对 `finished==true` 的位置不再做真实采样（直接保留 `end_id`），从而跳过无效计算。

**收尾阶段**（循环后）：
1. `invokeMinusUnfinishedSeqlen`：因为循环里 `sequence_length` 会先把每步都计上，但对未在循环内自然结束的序列（被 `max_seq_len` 截断），需要把多算的 1 减回去。
2. beam search 走 `invokeGatherTree`：用 `parent_ids` 从扁平的 `[max_seq_len, batch*beam]` 表里回溯出每条 beam 的连贯 token 序列。
3. sampling 走 `cudaD2Dcpy`：直接把 `output_ids_buf_` 拷到输出。

#### 4.3.3 源码精读

初始化 kernel 的核心 4 行：

[decoding_kernels.cu:L38-L46](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoding_kernels.cu#L38-L46) — 一个 thread 处理一个 `batch*beam` 槽位，`finished=false`、`sequence_length=max_input_length`、`word_ids = sentence_ids[index/beam_width]`（同一源句的所有 beam 都从同一个 start token 开始）、`cum_log_probs` 按 `index % beam_width` 区分主/哑 beam。它在 `Decoding::forward` 的 [L353-L362](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoding/Decoding.cc#L353-L362) 被调用。

`DynamicDecodeLayer` 的 `setup` 在循环前调用一次，把采样参数「安装」进去：

[Decoding.cc:L340-L348](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoding/Decoding.cc#L340-L348) — 把 `random_seed`、`beam_search_diversity_rate`、`temperature`、`len_penalty`、`repetition_penalty`、`runtime_top_k`、`runtime_top_p` 打包成 `runtime_args` 传给 `setup`。注意 `random_seed=0` 固定，所以这个老版 `Decoding` 的 sampling 是**确定性**的（同一输入同一输出）；后续 ParallelGpt 才支持每请求独立随机种子。

beam search 专用的 cache 重排表双缓冲交替：

[Decoding.cc:L403-L404](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoding/Decoding.cc#L403-L404) — `src_indir_idx = (step-1)%2`、`tgt_indir_idx = 1 - src_indir_idx`。beam search 每步可能选出来自不同父 beam 的候选，导致 K/V cache 需要按新的 beam 顺序重排（详见 u6-l2）。两张表交替读写，避免读写冲突。sampling（`beam_width==1`）时这张表传 `nullptr`（见 L430 的三元判断）。

收尾的三段：

[Decoding.cc:L563-L564](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoding/Decoding.cc#L563-L564) — `invokeMinusUnfinishedSeqlen` 修正长度。

[Decoding.cc:L566-L588](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoding/Decoding.cc#L566-L588) — `if (beam_width_ > 1)` 分支调 `invokeGatherTree` 回溯路径；`else` 分支仅 `cudaD2Dcpy` 拷贝 `output_ids_buf_`。两者的偏移量都从 `+ batch_size * beam_width_` 开始，跳过第 0 步的 `start_id`。

[Decoding.cc:L591-L596](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoding/Decoding.cc#L591-L596) — 若调用方在输出张量里放了 `cum_log_probs`（可选），把每条序列的累积对数概率拷出去；常用于重排序或计算 perplexity。

#### 4.3.4 代码实践

**实践目标**：理解 `finished` 如何同时驱动 early stopping 和「跳过已完成序列」。

**操作步骤**：
1. 在 [Decoding.cc:L394-L401](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoding/Decoding.cc#L394-L401) 处确认 early stopping 的判定逻辑。
2. 跟踪 `finished_buf_` 在循环内的两个去向：
   - 作为 `decoder_input_tensors` 的第 4 个张量传给 `Decoder`（L424）；
   - 作为 `dynamic_decode_input_tensors` 的 `"finished"` 传给动态解码层（L544）。
3. 查阅 `docs/decoder_guide.md` 的 Optimization 一节（[L123](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/decoder_guide.md#L123) 附近），其中提到 fused attention kernel 利用 `finished` 跳过已完成序列。

**需要观察的现象**：当 batch 内序列长短不一时，先结束的序列并不会让整个 batch 停下——它们被标记 `finished=true` 后，后续步骤里 Decoder 与动态解码层都会基于 `finished` 掩码跳过它们，只有当 **全部** 序列完成才 break。

**预期结果**：能画出一张时序图：横轴是 `step`，纵轴是 batch 内各序列，用色块标出每条序列从「生成中」变「finished」的转折点，以及「所有序列 finished → 循环 break」的时刻。

**待本地验证**：实际运行时，由于示例用随机权重，生成的 token 基本不会触发 `end_id`，因此 early stopping 很少触发——这正好说明 finished 优化的收益在真实模型上才显著。

#### 4.3.5 小练习与答案

**练习 1**：beam search 第 0 步，为什么只有 `index % beam_width == 0` 的 beam 累积概率为 0，其余为 `-MAX`？
**答案**：防止第一步把同一个 token 选 `beam_width` 次。让只有主 beam 携带有效概率，哑 beam 被自然淘汰，从而在第 1 步就能展开出 `beam_width` 个**不同**的候选。

**练习 2**：sampling（`beam_width==1`）为什么不需要 `cache_indirections_` 和 `invokeGatherTree`？
**答案**：sampling 只维护 1 条序列，没有「按父 beam 重排 cache」的需求（cache 顺序恒定），也没有多条 beam 需要回溯路径——直接顺序拷贝 `output_ids` 即可。

**练习 3**：`invokeMinusUnfinishedSeqlen` 为什么只对「未完成」序列减 1？
**答案**：循环里每生成一个 token 都会 `+1` 计入 `sequence_length`。对已自然结束（遇 `end_id`）的序列，长度计数在结束那步就已正确；只有被 `max_seq_len` 截断、循环结束时仍未完成的序列，会因为「最后一步算了但没真正生成」而多算 1，需要减回。

---

### 4.4 decoding_gemm：GEMM 初始化与离线调优

#### 4.4.1 概念说明

承接 u2-l4 的「GEMM 算法自动调优」：同一个矩阵乘在不同 `(M,N,K)` 形状下，最快的 cuBLAS 算法不同。`Decoding` 的每一步涉及 6 个不同形状的 GEMM（QKV、attention 输出投影、cross-attention 的 K/V、FFN 两段、logits 投影），需要分别调优。

`decoding_gemm` 是一个**独立的可执行程序**，它不跑模型，只跑这 6 个 GEMM 的形状、遍历候选算法、把最快的写入 `gemm_config.in`。运行 `decoding_example` 之前要先跑它生成配置文件（[decoder_guide.md:L204-L227](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/decoder_guide.md#L204-L227)）。

#### 4.4.2 核心流程

```
decoding_gemm.cc::main
  ├─ 解析 9 个命令行参数（batch/beam/head/size_per_head/inter/vocab/max_mem_seq/mem_hidden/data_type）
  ├─ calDecodingGemmTestBufSizeInByte → 检查显存够不够
  └─ 按 data_type 分发到 generate_decoding_gemm_config<T>

generate_decoding_gemm_config<T>
  ├─ 枚举 6 个 GEMM 的 (M,N,K)：
  │    gemm0: from_tensor × weightQKV           M=batch*beam, K=hidden, N=3*hidden
  │    gemm1: attr × output_kernel              M=batch*beam, K=hidden, N=hidden
  │    gemm2: mem_tensor × weightK/V (cross)    M=batch*beam*max_mem_seq, K=mem_hidden, N=hidden
  │    gemm3: ffn gemm1                         M=batch*beam, K=hidden, N=inter
  │    gemm4: ffn gemm2                         M=batch*beam, K=inter, N=hidden
  │    gemm5: decoder_output × embedding_kernel M=batch*beam, K=hidden, N=vocab_padded
  ├─ 对每个 GEMM：
  │    for algo in startAlgo..endAlgo:          // 经典 cuBLAS 算法枚举
  │        跑 100 次取均值，记最快
  │    if 半精度: 再用 LtHgemmCustomFind 搜 ~5000 个 cuBLASLt 组合，取更快者
  └─ 把最优算法写进 gemm_config.in（### 前给人看，后给机器读）
```

6 个 GEMM 形状中，`gemm2` 的 M 维是 `batch*beam*max_mem_seq_len`（cross-attention 要对整个 encoder 输出算 K/V），其余 5 个的 M 维都是 `batch*beam`（query 序列长度为 1）。

#### 4.4.3 源码精读

`decoding_gemm.cc::main` 的参数解析与数据类型分发：

[decoding_gemm.cc:L22-L52](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoding/decoding_gemm.cc#L22-L52) — 注意它要求 `argc==10 || argc==11`，第 10 个可选参数 `is_append` 控制是覆盖还是追加写 `gemm_config.in`（追加多个 batch 的配置）。`data_type` 与全库套路一致：0=FP32, 1=FP16, 2=BF16。

6 个 GEMM 形状的精确定义：

[decoding_gemm_func.cc:L85-L127](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm_test/decoding_gemm_func.cc#L85-L127) — 每个 GEMM 都带一句人类可读注释（如 `strcpy(mess[0], "from_tensor * weightQKV")`），这些注释会被写进配置文件 `###` 之前。`gemm5` 的 N 维用 `ceil(vocab_size/8.)*8`，与 4.2 提到的 `vocab_size_padded_` 对齐逻辑一致。

算法搜索主循环：

[decoding_gemm_func.cc:L184-L233](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm_test/decoding_gemm_func.cc#L184-L233) — 对每个 GEMM，`for (algo = startAlgo; algo <= endAlgo; algo++)` 遍历经典 cuBLAS 算法（FP32 区间是 `CUBLAS_GEMM_DEFAULT..ALGO23`，半精度是带 `_TENSOR_OP` 的区间），每个算法跑 `ites=100` 次取均值（L139 的 `const int ites = 100`），用 `gettimeofday` 计时，记最快 `fast_algo`。

[decoding_gemm_func.cc:L236-L294](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm_test/decoding_gemm_func.cc#L236-L294) — 半精度（FP16/BF16）额外跑 `LtHgemmCustomFind`，在约 5000 个 cuBLASLt 算法组合里搜索（承接 u2-l4）。若 cuBLASLt 结果更快就写它的配置，否则写经典算法。FP32 只扫经典区间，不跑 cuBLASLt。

#### 4.4.4 代码实践

**实践目标**：跑通「生成配置 → 运行 decoding」的标准两步流程（在无 GPU 环境下做源码阅读型实践）。

**操作步骤**：
1. 对照 [docs/decoder_guide.md:L204-L227](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/decoder_guide.md#L204-L227)，写出两条命令：
   - `./bin/decoding_gemm 32 4 8 64 2048 30000 32 512 1`（生成 FP16 + beam=4 的配置）
   - `./bin/decoding_example 32 4 8 64 2048 30000 6 32 32 512 0 0.0 1`（用该配置跑 beam search）
2. 把 `decoding_gemm` 的 9 个参数逐一对应到 `generate_decoding_gemm_config` 里 6 个 GEMM 的 `M/N/K` 表达式，理解每个参数影响哪些 GEMM 的形状。
3. **思考**：为什么换 batch_size（如 32→128）后必须重新跑 `decoding_gemm`？

**需要观察的现象**：`decoding_gemm` 会在终端逐个打印 6 个 GEMM 的测试结果（`GEMM test 0: [M:.., K:.., N:..] ...`、`algo_X costs Y.YYYms`、`fast_algo Z costs W.WWWms`），最后写入 `gemm_config.in`。

**预期结果**：能解释「batch_size 出现在所有 6 个 GEMM 的 M 维，所以换 batch 会改变所有 GEMM 的最优算法，必须重新调优」。

**待本地验证**：上述命令的实际运行耗时与终端输出（本环境无 GPU，无法实跑）。

#### 4.4.5 小练习与答案

**练习 1**：6 个 GEMM 里，哪一个的 M 维与众不同？为什么？
**答案**：`gemm2`（cross-attention 的 K/V 投影），它的 M = `batch*beam*max_mem_seq_len`。因为 cross-attention 要对 encoder 的**整段**输出计算 K/V（序列长度 = `max_mem_seq_len`），而其余 GEMM 的 query 序列长度恒为 1（单步解码）。

**练习 2**：为什么 FP32 不跑 `LtHgemmCustomFind`，而 FP16/BF16 要跑？
**答案**：cuBLASLt 的 tensor-op 算法主要服务于半精度（FP16/BF16）的 tensor core 加速，对 FP32 收益有限；FP32 只扫经典 cuBLAS 算法区间即可。

---

## 5. 综合实践

**任务**：绘制 `Decoding::forward` 的完整时序图，并预测一个 batch 内序列长短不齐时的行为。

**步骤**：

1. **画主循环时序图**（纵向是 `step`，横向是 5 个动作）：
   - 在每一步标注：读 `output_ids_buf_[step-1]` → 写 `decoder_input_buf_` → 写 `decoder_output_buf_` + 4 块 cache → 写 `normed_decoder_output_buf_` → 写 `logits_buf_` → 写 `output_ids_buf_[step]` + `finished_buf_` + `parent_ids_buf_[step]`。
   - 用箭头标出 `finished_buf_` 每步从 GPU 拷回 CPU 的同步点。

2. **标注分流点**：
   - `beam_width > 1` 时，画出 `cache_indirections_[0]/[1]` 的双缓冲交替（`src = (step-1)%2`、`tgt = 1-src`）；
   - 收尾阶段，beam search 走 `invokeGatherTree`，sampling 走 `cudaD2Dcpy`。

3. **场景推演**：假设 `batch=4, beam=4, max_seq_len=32`，4 条源句分别在第 8、15、20、30 步生成 `end_id`。
   - 问：循环在第几步 break？
   - 问：第 8 步之后，先结束的那条源句的 4 条 beam 还参与计算吗？
   - 问：`cum_log_probs` 在第 0 步的 16 个槽位里，哪几个是 `0.0`，哪几个是 `-MAX`？

**参考答案**：
- break 发生在第 30 步（最后一条序列结束）；
- 第 8 步后，那条源句的 4 条 beam 的 `finished` 被置 true，Decoder 与动态解码层基于 `finished` 掩码跳过它们，但其他 3 条源句的 beam 仍正常计算，整个 batch 继续推进；
- `cum_log_probs` 的 16 个槽位中，`index = 0, 4, 8, 12`（即每条源句的第 0 条 beam）为 `0.0`，其余 12 个为 `-MAX`。

**延伸**（可选）：对照 [docs/decoder_guide.md:L121-L124](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/decoder_guide.md#L121-L124) 的 Optimization 一节，把图中能对应上的优化点（fused MHA、AddBiasResidualLayerNorm 融合、K/V cache 复用、跨层 buffer 复用）逐一圈出。

## 6. 本讲小结

- **`Decoding` 是端到端生成的编排者**：把「单步 `Decoder`（u5-l1）」循环驱动，加上 embedding 查表、logits 投影、动态解码，封装成一次 `forward` 调用，对外只见 `encoder_output` 进、`output_ids` 出。
- **主循环固定 5 个动作**：early stopping 判定 → embedding 查表 → `Decoder::forward`（更新 K/V cache）→ LayerNorm + logits GEMM → `DynamicDecodeLayer::forward`（选 token、更新 finished）。
- **`beam_width` 决定解码策略**：`> 1` 走 beam search（需 `cache_indirections_` 双缓冲重排 cache、收尾 `invokeGatherTree` 回溯路径）；`== 1` 走 sampling（直接拷贝 `output_ids`）。
- **`finished` 数组双重作用**：每步拷回 CPU 求和驱动 early stopping；同时作为掩码传给 Decoder 与动态解码层，让已完成序列跳过后续计算。
- **BF16 动态解码回退 FP32**：经 `fallBackType` 模板，`logits_buf_` 与 `DynamicDecodeLayer` 在 BF16 模型下用 `float`，保证 topk/排序的数值精度。
- **`decoding_gemm` 离线调优 6 个 GEMM**：枚举 QKV/输出投影/cross K-V/FFN 两段/logits 共 6 个形状，遍历 cuBLAS（半精度再加 cuBLASLt）算法，挑最快写入 `gemm_config.in`，换 batch/精度必须重跑。

## 7. 下一步学习建议

- **进入 u6-l1（ParallelGpt 架构）**：`Decoding` 是 encoder-decoder 的端到端生成；ParallelGpt 把它演进到纯 decoder 的大模型场景，拆出 `ParallelGptContextDecoder`（处理整段 prompt）与 `ParallelGptDecoder`（逐 token）两阶段。对比两者的循环结构会发现高度相似。
- **进入 u6-l2（KV Cache 机制）**：本讲提到 `cache_indirections_` 的双缓冲与 beam search 的 cache 重排，其底层 kernel（`invokeTransposeAxis01` 等）在 u6-l2 详讲。
- **进入 u8-l1（DynamicDecodeLayer）**：本讲把 `dynamic_decode_layer_->forward` 当作黑盒；u8-l1 打开它，讲清运行期如何在 beam search / top-k / top-p 间按 `runtime_args` 动态分发，以及 `runtime_arg_names_` 机制。
- **建议精读源码**：在进入 u6 前，重读 [Decoding.cc:L389-L597](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/decoding/Decoding.cc#L389-L597) 的完整主循环与收尾，它几乎是所有 FT 生成模型的「参考实现模板」。
