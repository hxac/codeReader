# BERT 模型与 forward 主流程

## 1. 本讲目标

本讲是「编码器模型」单元的第一篇，带你进入 FasterTransformer（下文简称 FT）中**最经典、最适合入门**的模型实现——BERT。

读完本讲，你应当能够：

- 说清楚 FT 的 BERT 用「8 或 6 个 GEMM + 6 个 custom kernel」实现一个 transformer block 的来龙去脉。
- 看懂 `Bert::forward` 的主循环：从输入张量约定、`remove_padding` 预处理，到逐层 attention / LayerNorm / FFN 的调用顺序。
- 区分 `AttentionType` 的四个取值，理解 `remove_padding`（Effective FasterTransformer）开关如何改变 forward 的数据流。
- 理解 `bert_gemm` 离线调优的 7 个 GEMM 形状分别对应 block 里的哪一步计算。

本讲不深入 attention 层和 FFN 层的内部实现（那已经在 u3-l3、u3-l4 讲过），而是站在 **模型编排者（model）** 的视角，把它们串成完整的 BERT 前向。

## 2. 前置知识

本讲假定你已经掌握以下概念（对应前置讲义）：

- **GEMM 与 cuBLAS**（u2-l3）：FT 的几乎所有矩阵乘都通过 `cublasMMWrapper` 的 `Gemm` / `batchedGemm` / `stridedBatchedGemm` 完成；理解「低精度存储 + FP32 累加」「leading dimension」即可。
- **注意力层抽象**（u3-l3）：FT 的注意力层是一条继承树，由 `AttentionType` 枚举在 FUSED / UNFUSED × PADDED / UNPADDED 之间分派；张量并行注意力层在输出投影后做一次 `ftNcclAllReduceSum`。
- **FFN 层**（u3-l4）：FFN 是「两段 GEMM + 一段激活」，张量并行 FFN 采用「GEMM1 列切分 + GEMM2 行切分 + 末尾一次 all-reduce」。
- **Tensor / TensorMap / BertWeight**（u2-l1、u2-l5）：模型的 `forward` 接口统一接收 `TensorMap*`；BERT 权重是一棵「模型级 `BertWeight` → 每层 `BertLayerWeight` → `DenseWeight`」的指针树。

另外补充两个 BERT 本身的基础概念：

- **Encoder（编码器）**：BERT 只含 encoder，输入一段 token 序列，输出每个位置同等长度的隐状态，不做生成。因此 BERT 是「定长输入、定长输出」的模型，和后面要讲的 GPT（逐 token 生成）形成对比。
- **Transformer block**：BERT 由若干个堆叠的 transformer block 组成（如 BERT-base 是 12 层），每个 block 内部都执行「自注意力 + 残差 + LayerNorm + 前馈网络（FFN） + 残差 + LayerNorm」这组固定计算。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 |
| :--- | :--- |
| `src/fastertransformer/models/bert/Bert.h` | `Bert` 模板类的声明：成员变量、构造函数签名、两个重载的 `forward`。 |
| `src/fastertransformer/models/bert/Bert.cc` | **本讲核心**。`Bert::forward` 的完整实现，包括预处理、逐层循环、后处理。 |
| `src/fastertransformer/models/bert/bert_gemm.cc` | 离线 GEMM 调优工具的 `main`：解析命令行参数，按数据类型分派到 `generate_encoder_gemm_config<T>`。 |
| `src/fastertransformer/utils/gemm_test/encoder_gemm_func.cc` | `generate_encoder_gemm_config` 的实现：定义并枚举 BERT 的 7 个 GEMM 形状，挑选最优算法写入 `gemm_config.in`。 |
| `src/fastertransformer/models/bert/BertWeight.h` | BERT 权重容器：`bert_layer_weights`（每层）+ `post_transformer_layernorm_weights`（模型级末尾 LayerNorm）。 |
| `docs/bert_guide.md` | 官方 BERT 指南：给出 block 流程图、构造/输入/输出参数表、各种精度与 remove_padding 的运行方式。 |

> 提示：`Bert.cc` 的 forward 会调用 `UnfusedAttentionLayer` / `FusedAttentionLayer`（u3-l3）和 `FfnLayer`（u3-l4）。本讲只关心「在什么时机调用它们」，不展开它们内部。

## 4. 核心概念与源码讲解

### 4.1 BERT 模型架构与 `Bert` 类的构造

#### 4.1.1 概念说明

FT 的 BERT 在数学上与原版 BERT 等价，但在工程上做了大量优化。官方指南 ([bert_guide.md:55](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/bert_guide.md#L55)) 有一句关键总结：

> After optimization, FasterTransformer only uses **8 or 6 gemms** (blue blocks) and **6 custom CUDA kernels** (green blocks) to implement one transformer block.

这句话是理解整个 BERT 实现的钥匙。先解释「8 或 6」从何而来——一个标准 transformer block 里需要做的矩阵乘（GEMM）有：

| 计算步骤 | GEMM 个数 |
| :--- | :--- |
| Q / K / V 投影 | 3 个独立 GEMM，**或** 1 个 `batchCount=3` 的 batched GEMM |
| 注意力分数 \(QK^\top\) | 1 个 strided batched GEMM |
| 注意力输出 \(PV\) | 1 个 strided batched GEMM |
| 注意力输出投影（context → hidden） | 1 个 GEMM |
| FFN 升维（hidden → inter） | 1 个 GEMM |
| FFN 降维（inter → hidden） | 1 个 GEMM |

把 Q/K/V 算成 3 个独立 GEMM 时总共 **8 个**；把 Q/K/V 融成 1 个 batched GEMM 时总共 **6 个**。到底走哪条路径，是在**运行期**由 `cublas_wrapper_->isFuseBatchGemm(3, n, m, k)` 根据形状动态决定的（见 4.4 节）。这正是 `bert_gemm` 工具要**同时**为「单 QKV」和「batched QKV」两种形状都调优的原因。

「6 个 custom kernel」则是把那些不适合用 GEMM 表达的逐元素/归约操作（LayerNorm、masked softmax、残差相加、激活函数、布局转置）写成融合 CUDA kernel，从而省掉大量中间显存读写。这些 kernel 在 u3-l1 已有铺垫。

#### 4.1.2 核心流程

`Bert` 类的生命周期很简单：

1. **构造**：传入模型结构（`head_num`、`size_per_head`、`inter_size`、`num_layer`）、运行环境（`stream`、`cublas_wrapper`、`allocator`）、并行信息（`tensor_para`、`pipeline_para`）等，调用 `initialize()`。
2. **`initialize()`**：根据 `attention_type_` 和 `activation_type_`，`new` 出子层对象——一个 fused 注意力层（可选）、一个 unfused 注意力层、一个 FFN 层。
3. **`forward()`**：接收输入输出张量与权重，执行前向（4.2 ~ 4.4 详述）。
4. **析构**：`delete` 子层并 `freeBuffer()`。

派生量关系（承接 u1-l4）：
- `hidden_units_ = head_num_ * size_per_head_`
- `inter_size_` 通常为 `4 * hidden_units_`

#### 4.1.3 源码精读

`Bert` 类继承自 `BaseLayer`（u3-l5），声明了关键成员：注意它**同时持有一个 fused 和一个 unfused 注意力层指针**，因为运行期可能因输入不合法而临时回退。

[Bert.h:46-49](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.h#L46-L49) —— 两个注意力层 + 一个 FFN 层：

```cpp
BaseAttentionLayer<T>* unfused_attention_layer_ = nullptr;
BaseAttentionLayer<T>* fused_attention_layer_   = nullptr;
FfnLayer<T>*           ffn_layer_;
```

`initialize()` 按 `attention_type_` 与数据类型决定是否创建 fused 层，并按 `activation_type_` 创建对应的 TensorParallel FFN 变体：

[Bert.cc:25-39](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc#L25-L39) —— 只有 FP16 且 attention_type 为 FUSED 时才创建 fused 层（其余情况 fused 指针保持 `nullptr`）：

```cpp
if (std::is_same<T, half>::value
    && (attention_type_ == AttentionType::FUSED_MHA || attention_type_ == AttentionType::FUSED_PADDED_MHA)) {
    fused_attention_layer_ = new FusedAttentionLayer<T>(0, 0,
        head_num_ / tensor_para_.world_size_, size_per_head_, ...);
}
```

[Bert.cc:53-90](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc#L53-L90) —— FFN 层按激活类型（Gelu / Relu）实例化对应的 TensorParallel 变体（u3-l4 已讲过这种「模板方法 + 张量并行」组合）：

```cpp
if (activation_type_ == ActivationType::Gelu) {
    ffn_layer_ = new TensorParallelGeluFfnLayer<T>(0, 0, head_num_, size_per_head_,
        0 /*expert_num*/, inter_size_, tensor_para_, stream_, cublas_wrapper_, ...);
}
else if (activation_type_ == ActivationType::Relu) {
    ffn_layer_ = new TensorParallelReluFfnLayer<T>(...);
}
```

> 注意两点：① 注意力层构造时传入的是 `head_num_ / tensor_para_.world_size_`，即每张卡只负责一部分头（列切分，u3-l3）；FFN 层传入的是完整的 `head_num_`，因为 FFN 内部自己按 `tensor_para_` 切 `inter_size_`。② 第二个构造函数（无并行参数）会把 `tensor_para_`、`pipeline_para_` 默认成 `NcclParam(0, 1)`（单卡），这是单 GPU BERT 示例（u1-l4）使用的入口。

#### 4.1.4 代码实践

**实践目标**：确认「fused 注意力层并非总是被创建」，并理解创建条件。

**操作步骤**：

1. 打开 `src/fastertransformer/models/bert/Bert.cc`，定位到 `initialize()`（第 23 行起）。
2. 阅读第 25 行的 `if` 条件，列出 fused 层被创建所需的**全部**前提。

**需要观察的现象**：

- 条件包含 `std::is_same<T, half>::value`，说明 BF16 / FP32 不会创建 fused 层。
- 条件还要求 `attention_type_` 是 `FUSED_MHA` 或 `FUSED_PADDED_MHA`。

**预期结果**：如果你用 FP32（`data_type=0`）运行 `bert_example`，`fused_attention_layer_` 必然为 `nullptr`，forward 时一定走 unfused 分支。

#### 4.1.5 小练习与答案

**练习 1**：为什么 FT 要同时保留 fused 和 unfused 两个注意力层对象，而不是在构造时就二选一？

> **参考答案**：因为 fused 层对输入形状有限制（需 Turing 及以上、`size_per_head==64`，且序列长度需满足 `isValidSeqLen`）。即使构造时满足条件，运行期某次请求的序列长度也可能让 fused 层失效，此时 forward 里会**临时回退**到 unfused 层（见 4.3.3 的回退逻辑）。保留两个对象让这种回退零成本。

**练习 2**：`hidden_units_` 和 `inter_size_` 在 BERT-base（12 头、`size_per_head=64`）下分别等于多少？

> **参考答案**：`hidden_units_ = 12 * 64 = 768`；`inter_size_` 通常为 `4 * 768 = 3072`。

---

### 4.2 `forward` 的输入输出张量约定

#### 4.2.1 概念说明

FT 所有模型的 `forward` 都遵循「统一张量接口」约定（u2-l1）：输入输出用 `TensorMap`（按名字索引）承载。BERT 的输入输出非常简洁——**一段定长隐状态进、一段定长隐状态出**。这正是 encoder 模型的特征：不涉及 token id、不涉及生成步数、不涉及 KV cache。

理解张量形状约定是读懂 forward 主循环的前提，因为循环里所有的 buffer 大小、GEMM 的 `M` 维都用这些形状推导出来。

#### 4.2.2 核心流程

BERT 的输入输出（来自 [bert_guide.md:94-105](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/bert_guide.md#L94-L105)）：

| 方向 | 名字 | 形状 | 含义 |
| :--- | :--- | :--- | :--- |
| 输入 | `input_hidden_state` | `[batch, seq_len, hidden]` | 每个位置的隐状态（已过 embedding） |
| 输入 | `sequence_lengths` | `[batch]` | 每条句子**真实**长度（用于 mask 和去 padding） |
| 输出 | `output_hidden_state` | `[batch, seq_len, hidden]` | encoder 输出隐状态 |

其中 `hidden = head_num * size_per_head`。注意输入已经是隐状态而不是 token id——token 化与 embedding 在 BERT 模型之外完成（这也是为什么 `bert_example` 里直接随机生成 `input_hidden_state`）。

`forward` 有两个重载：旧的 `std::vector<Tensor>` 版本只是把向量包装成 `TensorMap` 再调用新版本，是兼容性遗留。

#### 4.2.3 源码精读

[Bert.cc:306-314](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc#L306-L314) —— 旧接口转新接口的适配层：

```cpp
void Bert<T>::forward(std::vector<Tensor>* output_tensors,
                      const std::vector<Tensor>* input_tensors,
                      const BertWeight<T>* bert_weights) {
    TensorMap input_tensors_map =
        TensorMap({{"input_hidden_state", input_tensors->at(0)}, {"sequence_lengths", input_tensors->at(1)}});
    TensorMap output_tensors_map = TensorMap({{"output_hidden_state", output_tensors->at(0)}});
    forward(&output_tensors_map, &input_tensors_map, bert_weights);
}
```

[Bert.cc:326-332](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc#L326-L332) —— 从输入张量读出 `batch` 与 `seq_len`，并做基本校验：

```cpp
const size_t request_batch_size = input_tensors->at("input_hidden_state").shape[0];
const size_t request_seq_len    = input_tensors->at("input_hidden_state").shape[1];
FT_CHECK(input_tensors->size() >= 2);
FT_CHECK(request_batch_size == input_tensors->at("sequence_lengths").shape[0]);
```

注意这里 `shape[0]`、`shape[1]` 直接对应表中的 `batch`、`seq_len`——这就是贯穿后续整个 forward 的两个核心尺寸。

#### 4.2.4 代码实践

**实践目标**：用伪代码构造一份合法的 BERT 输入。

**操作步骤**：参考 u2-l1 的 `TensorMap` 抽象，写一段伪代码（**示例代码**，非项目原有）：

```cpp
// 示例代码：构造 BERT 输入
const int batch = 32, seq = 32, hidden = 768;
T*      dev_hidden;   // 形状 [32,32,768]，已在 GPU 上
int*    dev_seqlen;   // 形状 [32]

TensorMap input{
    {"input_hidden_state", Tensor{MEMORY_GPU, getTensorType<T>(),
                                  {batch, seq, hidden}, dev_hidden}},
    {"sequence_lengths",   Tensor{MEMORY_GPU, TYPE_INT32, {batch}, dev_seqlen}}};
TensorMap output{
    {"output_hidden_state", Tensor{MEMORY_GPU, getTensorType<T>(),
                                   {batch, seq, hidden}, dev_out}}};

bert.forward(&output, &input, &bert_weight);
```

**需要观察的现象 / 预期结果**：`input_hidden_state` 必须是 3 维、`sequence_lengths` 必须是 1 维且第 0 维等于 batch，否则第 329-331 行的 `FT_CHECK` 会触发错误。这两个张量都必须在 GPU 上（`MEMORY_GPU`）。

#### 4.2.5 小练习与答案

**练习 1**：如果调用者把 `sequence_lengths` 放在 CPU 上，会发生什么？

> **参考答案**：forward 内部用 `input_tensors->at("sequence_lengths").getPtr<int>()` 取指针并直接传给 GPU kernel（如 `invokeGetPaddingOffset`），若指针实际指向 CPU 内存，kernel 会读到无效数据甚至段错误。FT 的张量接口**不自动搬运**数据，位置由调用方负责。

**练习 2**：为什么输入是「隐状态」而不是「token id」？

> **参考答案**：FT 的 BERT 只实现 transformer encoder 部分；embedding 查表（token → hidden）被视为前置步骤，由框架侧（PyTorch/TF）或示例代码完成。这样 BERT 模型本身保持纯粹，便于在不同 embedding 实现间复用。

---

### 4.3 `remove_padding` 预处理：四种 `AttentionType` 分支

#### 4.3.1 概念说明

`remove_padding`（即 Effective FasterTransformer，u4-l2 会专门讲）是 BERT 推理的一项关键加速。直觉是：当一个 batch 里句子的**平均长度**远小于**最大长度**时，padding 出来的那些位置全是无效计算。如果能在进入 transformer 之前**去掉 padding**（把有效 token 紧凑排列），计算量就从 `batch * max_seq_len` 降到「所有句子真实 token 总数」，最后再把结果**恢复**回带 padding 的形状即可。

BERT forward 用 `AttentionType` 枚举同时编码两个正交的开关：

| `AttentionType` | 注意力实现 | 是否去 padding |
| :--- | :--- | :--- |
| `UNFUSED_MHA` | cuBLAS GEMM（u3-l3 Unfused） | 是 |
| `UNFUSED_PADDED_MHA` | cuBLAS GEMM | 否 |
| `FUSED_MHA` | TensorRT 融合 kernel（u3-l3 Fused） | 是 |
| `FUSED_PADDED_MHA` | TensorRT 融合 kernel | 否 |

「是否 fused」决定注意力用哪条 kernel 路径；「是否去 padding」决定 forward 前后要不要做 compact / rebuild。两者正交，故有四种组合。

#### 4.3.2 核心流程

forward 进入逐层循环**之前**的预处理，按 `attention_type` 走四个分支之一，产出三个关键量：

1. `h_token_num`：去 padding 后的**有效 token 总数**（不去 padding 时等于 `batch * seq_len`）。
2. `attention_mask_`：形状 `[batch, seq, seq]` 的注意力掩码（仅 unfused 分支需要）。
3. `bert_input_ptr`：指向「紧凑排列」或「原始带 padding」的输入缓冲。

四个分支的差别可以浓缩成下表（对应 [Bert.cc:360-457](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc#L360-L457)）：

| 分支 | 调用的预处理 kernel | `h_token_num` | 输入指针 |
| :--- | :--- | :--- | :--- |
| `UNFUSED_MHA` | `invokeBuildEncoderAttentionMask` + `invokeGetPaddingOffset` + `invokeRemovePadding` | 真实 token 数 | 紧凑 buffer |
| `UNFUSED_PADDED_MHA` | `invokeBuildEncoderAttentionMask` | `batch*seq` | 原始输入 |
| `FUSED_MHA` | `invokeGetPaddingOffset` + `invokeRemovePadding` + `invokeGetTrtPaddingOffset` | 真实 token 数 | 紧凑 buffer |
| `FUSED_PADDED_MHA` | `invokeGetTrtPaddingOffset` | `batch*seq` | 原始输入 |

可以看到一个规律：**fused 分支不需要 `attention_mask_`**（掩码逻辑融进了 TensorRT kernel 内部，由 `trt_mha_padding_offset_` 描述），所以它不调用 `invokeBuildEncoderAttentionMask`。

#### 4.3.3 源码精读

先看运行期回退逻辑——fused 层可能因输入不合法而临时退化为 unfused：

[Bert.cc:341-350](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc#L341-L350) —— 当 fused 层不存在或序列长度不合法时，把 `FUSED_*` 改写成对应的 `UNFUSED_*`：

```cpp
if (fused_attention_layer_ == nullptr || fused_attention_layer_->isValidSeqLen(request_seq_len) == false) {
    if (attention_type == AttentionType::FUSED_MHA) {
        FT_LOG_WARNING("Because the input is invalid for fused mha, switch to unfused mha.");
        attention_type = AttentionType::UNFUSED_MHA;
    }
    else if (attention_type == AttentionType::FUSED_PADDED_MHA) {
        attention_type = AttentionType::UNFUSED_PADDED_MHA;
    }
}
```

注意这里改写的是**局部变量** `attention_type`，不改成员 `attention_type_`，所以下次请求若形状合法仍会尝试 fused。

再看「去 padding」分支 `UNFUSED_MHA` 的三步预处理：

[Bert.cc:361-392](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc#L361-L392) —— 构建 mask → 算出有效 token 数与偏移表 → 把有效 token 紧凑拷贝到 `bert_in_buffer_`：

```cpp
case AttentionType::UNFUSED_MHA: {
    invokeBuildEncoderAttentionMask(attention_mask_, /*seq_lens*/ ..., local_batch_size, request_seq_len, stream_);
    invokeGetPaddingOffset(h_pinned_token_num_ptr_, &h_token_num, padding_offset_,
                           /*seq_lens*/ ..., local_batch_size, request_seq_len, stream_);
    invokeRemovePadding(bert_in_buffer_,
                        input_tensors->at("input_hidden_state").getPtrWithOffset<T>(hidden_offset),
                        padding_offset_, h_token_num, head_num_ * size_per_head_, stream_);
    ...
    bert_input_ptr  = bert_in_buffer_;      // 紧凑 buffer
    bert_output_ptr = bert_out_buffer_;     // 输出也是紧凑的，最后再 rebuild
    break;
}
```

对比 `UNFUSED_PADDED_MHA`（不去 padding）：

[Bert.cc:393-407](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc#L393-L407) —— 不做 compact，直接把输入输出指针指向**用户传入的张量本身**：

```cpp
case AttentionType::UNFUSED_PADDED_MHA: {
    invokeBuildEncoderAttentionMask(attention_mask_, ...);
    h_token_num     = local_batch_size * request_seq_len;   // 含 padding
    bert_input_ptr  = input_tensors->at("input_hidden_state").getPtrWithOffset<T>(hidden_offset);
    bert_output_ptr = output_tensors->at("output_hidden_state").getPtrWithOffset<T>(hidden_offset);
    ...
}
```

最后，循环结束后还有对称的「恢复 padding」后处理（4.4.3 末尾会看到）。

#### 4.3.4 代码实践

**实践目标**：体会 `remove_padding` 对计算量的影响。

**操作步骤**：

1. 假设 `batch=32`、`seq_len=32`，但 32 条句子的真实长度都只有 8。
2. 分别计算 `UNFUSED_PADDED_MHA` 与 `UNFUSED_MHA` 下的 `h_token_num`。

**需要观察的现象**：

- 不去 padding：`h_token_num = 32 * 32 = 1024`。
- 去 padding：`h_token_num = 32 * 8 = 256`。

**预期结果**：block 内每个 GEMM 的 `M` 维（= `h_token_num`，见 4.5）从 1024 缩到 256，GEMM 计算量近似降到 1/4。这就是 Effective FasterTransformer 在「平均长度 ≪ 最大长度」时大幅加速的本质。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `FUSED_*` 分支不需要调用 `invokeBuildEncoderAttentionMask`？

> **参考答案**：fused 注意力（TensorRT 的 QKVToContext）把 softmax 的掩码逻辑融进了同一个 kernel，掩码信息通过 `trt_mha_padding_offset_`（由 `invokeGetTrtPaddingOffset` 生成）传入，不需要外部的 `[batch,seq,seq]` mask 矩阵。

**练习 2**：预处理里出现的 `ite`、`local_batch_size` 是做什么的？

> **参考答案**：这是 **pipeline parallel** 的机制。当 `pipeline_para_.world_size_ > 1` 时，一个大 batch 会被切成多份（`local_batch_size`），由 `iteration_num = batch / local_batch_size` 次迭代依次处理，每份在不同 pipeline 阶段间流转。单卡时 `local_batch_size == batch`，只迭代一次。

---

### 4.4 单个 transformer block 的 GEMM + kernel 流水（forward 主循环）

#### 4.4.1 概念说明

这是本讲的核心。整个 `for (l = 0; l < num_layer_; l++)` 循环体，就是一个 transformer block 的完整计算。无论 BERT 堆了多少层，**每一层的代码完全相同**（权重不同）。

一个 block（以 pre-layernorm 为例，FT 默认）的计算序列是：

```
for l in 0..num_layer:
    x_norm = LayerNorm(x, attn_ln)                 # custom kernel #1
    attn   = Attention(x_norm, mask, attn_weight)  # GEMM: QKV + QK^T + PV + output proj
    attn   = AllReduce(attn)                       # 仅 tensor_para>1 时
    attn   = x + bias + LayerNorm_for_ffn(attn)    # custom kernel #2（融合）
    ffn    = FFN(attn, ffn_weight)                 # GEMM: inter + output（含 gelu kernel）
    ffn    = AllReduce(ffn)                        # 仅 tensor_para>1 时
    x      = attn + bias_ffn_out(ffn)              # custom kernel #3
```

> 说明：post-layernorm（标准 BERT）的 LayerNorm 位置在残差之后，kernel 调用顺序略有不同，但「3 个融合 kernel + 注意力内部的 softmax/transpose kernel」合计约 6 个 custom kernel 的总量不变，这正是指南说的「6 custom kernels」。GEMM 则是 4.1.1 讲的 8 或 6 个。

#### 4.4.2 核心流程

把上面的伪代码与 FT 的 GEMM/kernel 对应起来（unfused、batched-QKV 路径，共 6 GEMM）：

| 步骤 | 类型 | 名称 | 由谁执行 |
| :--- | :--- | :--- | :--- |
| 1 | custom kernel | LayerNorm（注意力前） | `invokeGeneralLayerNorm`（Bert.cc） |
| 2 | GEMM (batched, batchCount=3) | Q/K/V 投影 | UnfusedAttentionLayer 内 `batchedGemm` |
| 3 | GEMM (strided batched) | \(QK^\top\) | UnfusedAttentionLayer 内 `stridedBatchedGemm` |
| 3.5 | custom kernel | masked softmax | UnfusedAttentionLayer 内 `invokeMaskedSoftmax` |
| 4 | GEMM (strided batched) | \(PV\) | UnfusedAttentionLayer 内 `stridedBatchedGemm` |
| 4.5 | custom kernel | transpose / add_bias | UnfusedAttentionLayer 内 |
| 5 | GEMM | 注意力输出投影（context→hidden） | UnfusedAttentionLayer 内 `Gemm` |
| 5.5 | NCCL | all-reduce（仅 TP>1） | Bert.cc `ftNcclAllReduceSum` |
| 6 | custom kernel | add_bias + residual + LayerNorm（FFN 前） | `invokeGeneralAddBiasResidualPreLayerNorm` |
| 7 | GEMM | FFN 升维（hidden→inter） | FfnLayer 内 GEMM1 |
| 7.5 | custom kernel | gelu 激活 | FfnLayer 内 activation kernel |
| 8 | GEMM | FFN 降维（inter→hidden） | FfnLayer 内 GEMM2 |
| 8.5 | NCCL | all-reduce（仅 TP>1） | FfnLayer 内 |
| 9 | custom kernel | add_bias + residual | `invokeAddBiasResidual` |

（若 Q/K/V 走 3 个独立 GEMM，则步骤 2 拆成 3 个，总 GEMM 数 6→8。）

#### 4.4.3 源码精读

逐层循环的外壳——`isValidLayerParallelId(l)` 过滤掉不归本 pipeline 阶段管的层：

[Bert.cc:459-464](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc#L459-L464)：

```cpp
for (uint l = 0; l < num_layer_; l++) {
    if (isValidLayerParallelId(l) == false) {
        continue;
    }
    T* from_tensor = l == 0 ? bert_input_ptr : bert_output_ptr;  // 第 0 层用输入，之后用上一层输出
    T* out_tensor  = bert_output_ptr;
```

注意 `from_tensor` 的取法：第 0 层读预处理后的 `bert_input_ptr`，之后每一层都把上一层的 `bert_output_ptr` 当输入——典型的「流式覆盖」写法，只需两块 buffer 交替。

**步骤 1：注意力前 LayerNorm（仅 pre-layernorm）**

[Bert.cc:481-493](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc#L481-L493)：

```cpp
if (layernorm_type_ == LayerNormType::pre_layernorm) {
    invokeGeneralLayerNorm(normed_from_tensor_, from_tensor,
        bert_weights->bert_layer_weights[l].attn_layernorm_weights.gamma,
        bert_weights->bert_layer_weights[l].attn_layernorm_weights.beta,
        layernorm_eps_, h_token_num, hidden_units_, (float*)nullptr, 0, stream_);
}
```

**步骤 2-5：注意力层 + all-reduce**

[Bert.cc:495-543](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc#L495-L543) —— 把输入打包成 `TensorMap` 传给子层，再按 `attention_type` 选择 fused/unfused 层，最后在 TP>1 时 all-reduce：

```cpp
TensorMap attn_input_tensors{
    {"input_query", Tensor{MEMORY_GPU, data_type, {h_token_num, hidden_units_},
        layernorm_type_ == pre ? normed_from_tensor_ : from_tensor}},
    {"attention_mask", Tensor{MEMORY_GPU, data_type,
        {local_batch_size, 1, request_seq_len, request_seq_len}, attention_mask_}}};
...
if (attention_type == FUSED_MHA || attention_type == FUSED_PADDED_MHA) {
    fused_attention_layer_->forward(&attn_output_tensors, &attn_input_tensors,
                                     &bert_weights->bert_layer_weights[l].attention_weights);
} else {  // UNFUSED_*
    unfused_attention_layer_->forward(&attn_output_tensors, &attn_input_tensors,
                                       &bert_weights->bert_layer_weights[l].attention_weights);
}
if (tensor_para_.world_size_ > 1) {
    ftNcclAllReduceSum(attn_out_buf_, attn_out_buf_, h_token_num * hidden_units_, tensor_para_, stream_);
}
```

> 这里再次体现了 u3-l3 的结论：注意力层是「列切分（QKV）+ 行切分（输出投影）」，全层唯一通信点在输出投影**之后**，所以 all-reduce 写在子层 `forward` 返回之后、由 `Bert.cc` 显式调用。

**步骤 6：注意力后残差 + FFN 前 LayerNorm（融合 kernel）**

[Bert.cc:557-575](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc#L557-L575) —— pre-layernorm 路径用一个融合 kernel 同时完成「加偏置 + 残差 + 下一层 LayerNorm」：

```cpp
else if (layernorm_type_ == LayerNormType::pre_layernorm) {
    invokeGeneralAddBiasResidualPreLayerNorm(
        attn_out_buf_, normed_attn_out_buf_, attn_out_buf_, from_tensor,
        bert_weights->bert_layer_weights[l].ffn_layernorm_weights.gamma,
        bert_weights->bert_layer_weights[l].ffn_layernorm_weights.beta,
        bert_weights->bert_layer_weights[l].attention_weights.attention_output_weight.bias,
        layernorm_eps_, h_token_num, hidden_units_, ...);
}
```

**步骤 7-8：FFN 层**

[Bert.cc:579-591](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc#L579-L591)：

```cpp
TensorMap ffn_input_tensors({{"ffn_input", Tensor{MEMORY_GPU, data_type,
    {h_token_num, hidden_units_},
    layernorm_type_ == pre ? normed_attn_out_buf_ : attn_out_buf_}}});
TensorMap ffn_output_tensors({{"ffn_output", Tensor{MEMORY_GPU, data_type,
    {h_token_num, hidden_units_}, out_tensor}}});
ffn_layer_->forward(&ffn_output_tensors, &ffn_input_tensors,
                    &bert_weights->bert_layer_weights[l].ffn_weights);
```

FFN 内部的 all-reduce（u3-l4）已在 `ffn_layer_->forward` 内部完成，所以 `Bert.cc` 这里不再单独 reduce FFN。

**步骤 9：FFN 后残差**

[Bert.cc:604-611](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc#L604-L611)：

```cpp
else if (layernorm_type_ == LayerNormType::pre_layernorm) {
    invokeAddBiasResidual(out_tensor, attn_out_buf_,
        bert_weights->bert_layer_weights[l].ffn_weights.output_weight.bias,
        h_token_num, hidden_units_, stream_);
}
```

**循环之后**：最后一个 pipeline 阶段做末尾 LayerNorm 和「恢复 padding」后处理。

[Bert.cc:625-671](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc#L625-L671)：

```cpp
if (pipeline_para_.rank_ == pipeline_para_.world_size_ - 1) {
    if (layernorm_type_ == pre_layernorm) {
        invokeGeneralLayerNorm(bert_output_ptr, bert_output_ptr,
            bert_weights->post_transformer_layernorm_weights.gamma, ...);  // 模型级末尾 LN
    }
    // 恢复 padding：把紧凑输出写回 [batch, seq, hidden]
    switch (attention_type) {
        case UNFUSED_MHA: case FUSED_MHA:
            invokeRebuildPadding(output..., bert_out_buffer_, padding_offset_, h_token_num, ...);
            break;
        case UNFUSED_PADDED_MHA: case FUSED_PADDED_MHA:
            break;  // 没去 padding，无需恢复
    }
}
```

> `post_transformer_layernorm_weights` 是 `BertWeight` 里唯一的**模型级**（非每层）权重（见 [BertWeight.h:42](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/BertWeight.h#L42)）。

#### 4.4.4 代码实践

**实践目标**：跟踪 `from_tensor` / `out_tensor` 在多层间的交替，理解「两块 buffer 流水」。

**操作步骤**：

1. 在 [Bert.cc:459-464](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc#L459-L464) 确认 `from_tensor`（第 0 层 = `bert_input_ptr`，其余 = `bert_output_ptr`）与 `out_tensor`（恒为 `bert_output_ptr`）。
2. 思考：为什么 `out_tensor` 总是 `bert_output_ptr`，而 `from_tensor` 在 `l>0` 时也变成 `bert_output_ptr`？这样会不会「读到刚写的数据」？

**需要观察的现象 / 预期结果**：这正是设计意图——每一层把结果写进 `bert_output_ptr`，下一层立刻把它当输入读，**原地流式更新**。因为 transformer 各层同构，只需要一块输出 buffer 反复覆写即可，省掉了为每层分配独立 buffer。第 0 层是特例（输入来自预处理后的 `bert_input_ptr`）。这是 FT 控制显存占用的常见手法。

#### 4.4.5 小练习与答案

**练习 1**：在 `tensor_para_.world_size_ == 1`（单卡）时，forward 里会出现几次 `ftNcclAllReduceSum` 调用？

> **参考答案**：0 次。注意力层后的 all-reduce 被 `if (tensor_para_.world_size_ > 1)` 包住不会执行（[Bert.cc:532](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc#L532)）；FFN 的 all-reduce 在 FfnLayer 内部，同样受 TP>1 判断保护。多卡时每层恰好 2 次（注意力 + FFN）。

**练习 2**：post-layernorm 与 pre-layernorm 两种模式下，`Bert.cc` 里 LayerNorm 类 kernel 的**位置**有何不同？

> **参考答案**：post-LN 把 LayerNorm 融在残差之后（`invokeAddBiasResidualLayerNorm`，第 546、594 行），即「先算注意力/FFN 再归一化」；pre-LN 把 LayerNorm 放在注意力/FFN **之前**（第 481 行），并在残差处用 `invokeGeneralAddBiasResidualPreLayerNorm` 把「残差 + 下一步的 LayerNorm」融合。两者的 GEMM 数量相同，只是 custom kernel 的编排不同。

---

### 4.5 GEMM 初始化：`bert_gemm` 与 7 个 GEMM 形状

#### 4.5.1 概念说明

承接 u2-l4：FT 采用「**离线调优 + 运行期查表**」为每个 GEMM 形状挑选最快的 cuBLAS 算法。BERT 的离线调优工具是 `bert_gemm`（可执行文件 `./bin/bert_gemm`），它的源码 `main` 在 `bert_gemm.cc`，真正的形状定义与算法搜索在 `encoder_gemm_func.cc`。

`bert_gemm` 本身**不在推理路径上**——它只在你部署新形状（新 batch/seq）之前手动跑一次，生成 `gemm_config.in`，模型构造时由 `cublasAlgoMap` 加载（u2-l4）。理解它的意义在于：**它定义了 BERT 一个 block 里到底有哪些 GEMM、各自的 (M,N,K) 是什么**。

#### 4.5.2 核心流程

`bert_gemm` 的命令行（[bert_gemm.cc:25-36](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/bert_gemm.cc#L25-L36)）：

```
./bin/bert_gemm batch_size seq_len head_number size_per_head data_type int8_mode [tensor_para_size] [is_append]
```

它的 `main` 做四件事：

1. 解析参数，校验 `head_num % tensor_para_size == 0`（TP 切头硬约束，承接 u2-l5）。
2. 算出调优所需显存 `calGemmTestBufSizeInByte`，`deviceMalloc` 一块 buffer。
3. 按 `int8_mode` / `data_type` 分派：INT8 走 `generate_encoder_igemm_config`，FP32/FP16/BF16 走 `generate_encoder_gemm_config<T>`。
4. `cudaFree` 释放 buffer。

`generate_encoder_gemm_config` 内部把 BERT 的所有 GEMM 归纳为 **7 个待调优形状**（`gemm_num = 7`），对每个形状枚举 cuBLAS 算法、各跑 100 次取均值、挑最快的写入配置文件。

#### 4.5.3 源码精读

[Bert.cc:25-58](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/bert_gemm.cc#L25-L58) 不需要逐行看，关键是 [encoder_gemm_func.cc:77-124](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm_test/encoder_gemm_func.cc#L77-L124) 定义的 7 个形状。这里 `M` 是序列维（`batch*seq` 或去 padding 后的 `token_num`），`N` 是输出特征维，`K` 是输入特征维：

```cpp
const int gemm_num = 7;
// gemm1 (i=0): from_tensor * weightQ/K/V   —— 单个 Q/K/V 投影
M[0] = batch_size * seq_len;  K[0] = head_num * size_per_head;        N[0] = (head_num/tp)*size_per_head;
// gemm2 (i=1): attr_output * inter_kernel  —— FFN 升维（GEMM1）
M[1] = M[0];  K[1] = head_num * size_per_head;  N[1] = 4*head_num*size_per_head/tp;
// gemm3 (i=2): inter_matmul * output_kernel —— FFN 降维（GEMM2）
M[2] = M[0];  K[2] = 4*head_num*size_per_head/tp;  N[2] = head_num*size_per_head;
// gemm4 (i=3): attention batched Gemm1     —— QK^T
M[3] = seq_len;  N[3] = seq_len;  K[3] = size_per_head;  batchCount[3] = batch_size*(head_num/tp);
// gemm5 (i=4): attention batched Gemm2     —— PV
M[4] = seq_len;  N[4] = size_per_head;  K[4] = seq_len;  batchCount[4] = batch_size*(head_num/tp);
// gemm6 (i=5): from_tensor * weight_QKV in BatchGemm —— 融合 QKV
M[5] = batch_size*seq_len;  N[5] = (head_num/tp)*size_per_head;  K[5] = head_num*size_per_head;  batchCount[5] = 3;
// gemm7 (i=6): attr * output_kernel        —— 注意力输出投影
M[6] = batch_size*seq_len;  K[6] = (head_num/tp)*size_per_head;  N[6] = head_num*size_per_head;
```

把这 7 个形状与 4.4.2 的步骤表对齐：

| 调优形状 | 对应计算 | block 步骤 |
| :--- | :--- | :--- |
| gemm1 (i=0) | 单个 Q/K/V 投影（3 个独立 GEMM 各用此形状） | 步骤 2（8-GEMM 路径） |
| gemm6 (i=5) | batched QKV（batchCount=3） | 步骤 2（6-GEMM 路径） |
| gemm4 (i=3) | \(QK^\top\) | 步骤 3 |
| gemm5 (i=4) | \(PV\) | 步骤 4 |
| gemm7 (i=6) | 注意力输出投影 | 步骤 5 |
| gemm2 (i=1) | FFN 升维 | 步骤 7 |
| gemm3 (i=2) | FFN 降维 | 步骤 8 |

可以清楚看到：**gemm1 与 gemm6 是同一个语义（QKV 投影）的两种实现**，所以「形状数=7」而「单路径 GEMM 数=6 或 8」并不矛盾。

> 调优细节（承接 u2-l4）：对前 3 个形状（i<3，即 gemm1/gemm2/gemm3）且为 FP16/BF16 时，还会额外用 `LtHgemmCustomFind` 在约 5000 个 cuBLASLt 组合里搜索（[encoder_gemm_func.cc:327-349](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gemm_test/encoder_gemm_func.cc#L327-L349)）；每个算法跑 `ites=100` 次取均值（第 136、216 行）。Ampere（sm_80/86）+ FP16 还会额外做 cusparseLt 稀疏 GEMM 调优（第 406 行起，受 `SPARSITY_ENABLED` 守卫）。

#### 4.5.4 代码实践

**实践目标**：亲手为 BERT 生成一份 GEMM 配置，并读懂其中一行。

**操作步骤**（**待本地验证**——需要先按 u1-l2 编译出 `./bin/bert_gemm`）：

1. 参考 [bert_guide.md:249-251](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/bert_guide.md#L249-L251) 运行：

   ```bash
   ./bin/bert_gemm 32 32 12 64 0 0 1 0
   # 参数：batch=32 seq=32 head=12 size_per_head=64 data_type=0(FP32) int8_mode=0 tp=1 is_append=0
   ```

2. 打开生成的 `gemm_config.in`，找到 `###` 分隔的算法字段行。

**需要观察的现象**：

- 终端会逐个打印 `GEMM test 0..6` 各形状的 `[M, K, N]` 与每个算法的耗时，最后给出 `fast_algo`。
- 配置文件每行形如 `32 32 12 64 0 ### <batchCount> <n> <m> <k> <algoId> ... <exec_time>`。

**预期结果**：你能把配置文件里某行的 `(m,n,k)` 与 4.5.3 表中的某个 GEMM 对应上（例如 `k=768, n=768` 对应 gemm1 单 QKV 投影）。若没有 GPU 环境，则改为阅读 `encoder_gemm_func.cc` 第 86-124 行，在纸上推出 BERT-base（12×64）在 `batch=32, seq=32, tp=1` 下 7 个形状的具体数值。

#### 4.5.5 小练习与答案

**练习 1**：为什么 gemm4（\(QK^\top\)）和 gemm5（\(PV\)）的 `batchCount` 是 `batch_size * (head_num/tp)`，而 gemm6（QKV）的 `batchCount` 是 3？

> **参考答案**：注意力的 \(QK^\top\) 与 \(PV\) 对「每个 batch、每个头」都要独立算一次（不同头之间不共享），所以 batch 维 = `batch * head_num/tp`；而 batched QKV 是把 Q、K、V 三个矩阵当成 batch 维为 3 的批量 GEMM 一次性算，batch 维 = 3。两者「batch」含义不同。

**练习 2**：在 `tensor_para_size=2`、`head_num=12` 下，gemm1 的 `N` 是多少？这反映了什么？

> **参考答案**：`N = (head_num/tp)*size_per_head = (12/2)*64 = 384`。这反映 QKV 投影是**列切分**——每张卡只算一半头的投影权重，输出特征维减半（u3-l3）。所以 TP=2 时每卡的 QKV GEMM 计算量是单卡的一半。

---

## 5. 综合实践

把本讲的知识串起来，完成下面这张「BERT block 计算流水表」。这是本讲的代码实践任务，也是检验你是否读懂 `Bert.cc` 的最好方式。

**任务**：对照 [bert_guide.md](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/bert_guide.md) 的 Fig.1 流程图与 [Bert.cc:459-623](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc#L459-L623) 的逐层循环，列出**单个 transformer block**（取 pre-layernorm、unfused、batched-QKV 路径）依次调用的 GEMM 和 kernel名称，按下表填写：

| 序号 | 类型 | 名称 | 计算的是什么 | 执行位置 |
| :--- | :--- | :--- | :--- | :--- |
| 1 | kernel | `invokeGeneralLayerNorm` | 注意力前归一化 | Bert.cc |
| 2 | GEMM | ? | Q/K/V 投影 | UnfusedAttentionLayer |
| 3 | GEMM | ? | \(QK^\top\) | UnfusedAttentionLayer |
| 4 | kernel | ? | masked softmax | UnfusedAttentionLayer |
| 5 | GEMM | ? | \(PV\) | UnfusedAttentionLayer |
| 6 | GEMM | ? | 注意力输出投影 | UnfusedAttentionLayer |
| 7 | kernel | `invokeGeneralAddBiasResidualPreLayerNorm` | 残差 + FFN 前 LN | Bert.cc |
| 8 | GEMM | ? | FFN 升维 | FfnLayer |
| 9 | kernel | ? | gelu 激活 | FfnLayer |
| 10 | GEMM | ? | FFN 降维 | FfnLayer |
| 11 | kernel | `invokeAddBiasResidual` | FFN 后残差 | Bert.cc |

**操作步骤**：

1. 把表中 `?` 补全（GEMM 名称可填语义，如「batched QKV」「strided batched QK^T」）。
2. 数一下 GEMM 总数，验证是否 = 6（batched QKV 路径）。
3. 进阶：若把第 2 步的 batched QKV 换成 3 个独立 GEMM，GEMM 总数应变成多少？为什么 `bert_gemm` 仍要同时调优 gemm1 和 gemm6 两种形状？

**预期结果 / 参考答案**：

- 第 2 步：batched QKV；第 3 步：strided batched \(QK^\top\)；第 5 步：strided batched \(PV\)；第 6 步：注意力输出投影 GEMM；第 8 步：FFN GEMM1（升维）；第 10 步：FFN GEMM2（降维）；第 9 步 kernel：gelu 激活 kernel。
- GEMM 总数 = 6。
- 换成独立 Q/K/V 后 GEMM 总数 = 8。`bert_gemm` 同时调优 gemm1（单 QKV 形状）和 gemm6（batched QKV 形状），是因为运行期 `UnfusedAttentionLayer` 会用 `isFuseBatchGemm` **动态**决定走哪条路径（[UnfusedAttentionLayer.cc:76-128](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/UnfusedAttentionLayer.cc#L76-L128)），两种形状的最优算法都得预先备好。

## 6. 本讲小结

- FT 的 BERT 用「**8 或 6 个 GEMM + 6 个 custom kernel**」实现一个 transformer block；8 与 6 的差别仅在于 Q/K/V 投影是 3 个独立 GEMM 还是 1 个 `batchCount=3` 的 batched GEMM，运行期由 `isFuseBatchGemm` 动态决定。
- `Bert::forward` 的张量接口极简：输入 `[batch, seq, hidden]` 隐状态 + `[batch]` 真实长度，输出 `[batch, seq, hidden]` 隐状态；统一用 `TensorMap` 承载。
- `AttentionType` 的四值（FUSED/UNFUSED × PADDED/UNPADDED）同时编码「注意力实现」与「是否去 padding」两个正交开关；`remove_padding` 把有效 token 紧凑排列，让 GEMM 的 `M` 维从 `batch*seq` 降到真实 token 数。
- forward 主循环对每一层执行：LayerNorm → 注意力（含 QKV/QK^T/softmax/PV/输出投影）→ all-reduce → 残差+LN → FFN（升维/gelu/降维）→ all-reduce → 残差；多层用「两块 buffer 原地流式覆写」。
- `bert_gemm` 工具为 7 个 GEMM 形状（gemm1/6 是 QKV 的两种实现，gemm2/3 是 FFN，gemm4/5 是 \(QK^\top\)/\(PV\)，gemm7 是输出投影）离线调优，产物 `gemm_config.in` 在模型构造时被 `cublasAlgoMap` 加载。
- pre-layernorm 与 post-layernorm 的 GEMM 数量相同，区别在 LayerNorm 类 kernel 的编排位置——FT 用一系列融合 kernel（`invokeGeneralAddBiasResidualPreLayerNorm` 等）把残差、加偏置、归一化压进单个 kernel。

## 7. 下一步学习建议

- **u4-l2（Effective FasterTransformer：去除 padding）**：本讲只是把 `remove_padding` 当作开关使用，下一讲会深入 `bert_preprocess_kernels` 里 `invokeGetPaddingOffset` / `invokeRemovePadding` / `invokeRebuildPadding` 的实现，以及为什么 fused MHA 能让去 padding 几乎零开销。
- **重读 u3-l3 / u3-l4**：现在你已经从模型视角看过了 attention 层和 FFN 层的调用时机，回头重读它们的内部实现（`UnfusedAttentionLayer.cc`、`FfnLayer.cc`）会有「拼图归位」的感觉。
- **对照阅读 `examples/cpp/bert/bert_example.cc`**（u1-l4 已读过入口）：现在可以重点看它如何 `new Bert`、构造 `BertWeight`、组装 `TensorMap` 并 `forward`，把本讲的模型构造与 forward 串成一条完整调用链。
- **延伸到 u4-l3（ViT 与 Swin）**：你会看到视觉编码器**复用**了几乎相同的 transformer block，只是输入构造不同（图像 patch embed vs token embedding），届时回看本讲的 block 流水会非常亲切。
