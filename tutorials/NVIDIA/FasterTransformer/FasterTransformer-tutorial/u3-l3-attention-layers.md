# 注意力层：Unfused / Fused / TensorParallel

## 1. 本讲目标

在前一讲（u3-l2）里，我们已经读完了「融合多头注意力 kernel」`decoder_masked_multihead_attention`，理解了把 `QK^T → softmax → PV` 压进单个 CUDA kernel 能省下多少显存读写。但**一个 kernel 还不是一个「层」**：真实模型里跑的注意力，还要在 kernel 前后各做一次矩阵乘（QKV 投影、输出投影）、处理 bias、处理 padding、可能还要做张量并行通信。

本讲就站在 kernel 之上、模型之下，讲解 `layers/attention_layers/` 这一层抽象。读完本讲，你应当能够：

1. 说清「Unfused 注意力层」和「Fused 注意力层」**分别用哪些 kernel 和 GEMM 拼出注意力**，以及为什么 FT 要同时保留这两条路径。
2. 理解 `AttentionType` 枚举与 `getAttentionType()` 是**整个注意力层的运行期分发开关**，决定一笔推理走哪条路径。
3. 掌握 `GptContextAttentionLayer`（处理整段 prompt）与 `DecoderSelfAttentionLayer`（逐 token 自回归）的职责分工。
4. 看懂 `TensorParallel*AttentionLayer` 如何通过**继承单卡层 + 在末尾插入一次 all-reduce**实现张量并行，并能指出**唯一的通信点在哪里**。

## 2. 前置知识

在进入本讲前，请确保你理解以下概念（前几讲已建立）：

- **GEMM 与 cuBLAS**：矩阵乘 \(C = \alpha \cdot \mathrm{op}(A)\mathrm{op}(B) + \beta \cdot C\)，FT 用 `cublasMMWrapper::Gemm` / `stridedBatchedGemm` / `batchedGemm` 三种形式调起（见 u2-l3）。
- **invokeXxx 约定与融合 kernel**：每个 GPU 算子由 `__global__` kernel + host 启动函数 `invokeXxx` 组成，layer 只调 `invokeXxx`，绝不直接写 `<<<>>>`（见 u3-l1、u3-l2）。
- **Tensor / TensorMap**：FT 全库统一的非拥有张量描述符，`forward(TensorMap* output, TensorMap* input, const Weight*)` 是标准接口（见 u2-l1）。
- **注意力数学定义**：

\[ \mathrm{Attention}(Q,K,V) = \mathrm{softmax}\!\left(\frac{QK^{\top}}{\sqrt{d_k}} \odot M\right)V \]

其中 \(Q \in \mathbb{R}^{L_q \times d_k}\)、\(K,V \in \mathbb{R}^{L_{kv} \times d_k}\)，\(M\) 是 mask 矩阵（padding / causal）。本讲关注的是**如何用 cuBLAS GEMM 与 FT 自定义 kernel 把这个公式算出来**，并把它包装成一个可复用的「层」。

- **张量并行的列/行切分**：线性层权重若沿输出维切（列并行）则每卡算出互不重叠的部分、无需通信；若沿输入维切（行并行）则每卡算出**部分和**、需 all-reduce 才能得到正确结果（见 u2-l5 的权重切分）。

一个关键直觉：**注意力是「逐头独立」的**。如果把多头注意力按 head 切到多张卡上，每张卡只负责自己那几个 head 的完整 QKV 与 softmax，那么 attention 的核心计算天然不需要任何通信——通信只会发生在最后的输出投影上。这个直觉是理解 4.6 节张量并行的钥匙。

## 3. 本讲源码地图

本讲涉及的文件全部位于 `src/fastertransformer/layers/attention_layers/`：

| 文件 | 作用 |
|------|------|
| [BaseAttentionLayer.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/BaseAttentionLayer.h) | 定义 `AttentionType` 枚举、`getAttentionType()` 分发函数与所有注意力层的虚基类 `BaseAttentionLayer<T>` |
| [AttentionWeight.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/AttentionWeight.h) | 注意力权重容器，由 6 个 `DenseWeight`（Q/K/V/输出/ia3_key/ia3_value）组成 |
| [UnfusedAttentionLayer.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/UnfusedAttentionLayer.cc) / [.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/UnfusedAttentionLayer.h) | **朴素路径**：用 cuBLAS GEMM 显式算 QKV 投影、\(QK^\top\)、softmax、PV、输出投影 |
| [FusedAttentionLayer.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/FusedAttentionLayer.cu) / [.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/FusedAttentionLayer.h) | **融合路径**：QKV 投影后，把 \(QK^\top \to\) softmax \(\to PV\) 交给 TensorRT 的 `MHARunner` 融合 kernel |
| [DecoderSelfAttentionLayer.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/DecoderSelfAttentionLayer.cc) | GPT **逐 token 解码**的自注意力，复用 u3-l2 讲过的 `masked_multihead_attention` kernel 并写入 KV cache |
| [GptContextAttentionLayer.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/GptContextAttentionLayer.cc) / [.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/GptContextAttentionLayer.h) | GPT **首步处理整段 prompt** 的注意力，运行期在 fused 与 unfused 间二选一 |
| [TensorParallelUnfusedAttentionLayer.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/TensorParallelUnfusedAttentionLayer.cc) | 单卡 Unfused 层的张量并行包装：继承父类 + 末尾 all-reduce |
| [TensorParallelGptContextAttentionLayer.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/TensorParallelGptContextAttentionLayer.cc) | GPT Context 注意力层的张量并行包装，多出一个 `do_all_reduce_` 开关 |

> 提示：目录里还有 `DecoderCrossAttentionLayer`、`LongformerAttentionLayer`、`WindowAttention`（Swin 用）、`DisentangledAttentionLayer` 等，它们是面向特定模型的注意力变体，本讲不展开，但套路与下面讲的完全一致。

## 4. 核心概念与源码讲解

### 4.1 注意力层的分类与运行期分发：AttentionType

#### 4.1.1 概念说明

FT 的注意力层不是「一个类」，而是**一棵继承树**。同一份注意力数学公式，FT 提供了多种实现路径，因为「最快」的实现依赖硬件（sm 版本）、数据类型（FP16/INT8/FP8）、`size_per_head`、是否去 padding 等多个条件。为了在不同条件下自动选到最快的实现，FT 设计了一个枚举 `AttentionType` 和一个分发函数 `getAttentionType()`。

这是 FT 在「层」这一级体现的核心哲学：**把「选哪条实现路径」从模型代码里剥离出来，集中到一个根据运行期条件决策的函数里**。模型（如 Bert、GPT）只负责把 `attention_type` 作为输入张量传给层，由层内部去 dispatch。

#### 4.1.2 核心流程

`AttentionType` 一共有 4 个取值，由两个正交维度组成：

```
                  不去 padding (remove_padding=false)   去 padding (remove_padding=true)
融合 MHA kernel    FUSED_PADDED_MHA                      FUSED_MHA
朴素显式实现       UNFUSED_PADDED_MHA                    UNFUSED_MHA
```

- **Fused vs Unfused**：是否用 TensorRT 的融合多头注意力 kernel。
- **Padded vs Unpadded**：序列是否带 padding。去 padding（Effective FasterTransformer，见 u4-l2）会把多条不等长序列紧凑拼成一段连续 token，对应不同的 kernel。

`getAttentionType()` 的决策逻辑（伪代码）：

```
若 T==half 且 is_fuse:
    若 非 causal_mask（BERT/ViT 类）:
        若 (sm, size_per_head) 命中支持表 -> FUSED_MHA / FUSED_PADDED_MHA
    若 causal_mask（GPT 类）:
        若 环境变量 FMHA_ENABLE==ON 且 (sm, size_per_head) 命中:
            -> FUSED_MHA / UNFUSED_PADDED_MHA
否则 -> UNFUSED_MHA / UNFUSED_PADDED_MHA   # 兜底
```

注意 GPT 类（`causal_mask=true`）默认**不走融合路径**，必须显式设 `FMHA_ENABLE=ON` 才会尝试融合——因为 GPT 的序列长度任意、且融合 kernel 对 GPT 的支持受限。

#### 4.1.3 源码精读

枚举定义与 4 个取值（[BaseAttentionLayer.h:L33-L38](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/BaseAttentionLayer.h#L33-L38)）：

```cpp
enum class AttentionType {
    UNFUSED_MHA,
    UNFUSED_PADDED_MHA,
    FUSED_MHA,
    FUSED_PADDED_MHA
};
```

分发函数对 BERT/ViT（非 causal）的支持表判断（[BaseAttentionLayer.h:L55-L68](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/BaseAttentionLayer.h#L55-L68)），可见融合路径仅在 `size_per_head ∈ {32,64}`、特定 sm（75/80/86 等）下启用：

```cpp
if (std::is_same<T, half>::value && is_fuse) {
    if (!causal_mask) {
        if (!with_swin_relative_position_bias
            && (((sm == kSM_70 || sm == kSM_72) && size_per_head == 64)
                || ((sm == kSM_75 || sm == kSM_80 || sm == kSM_86)
                    && (size_per_head == 64 || size_per_head == 32)))) {
            return remove_padding ? AttentionType::FUSED_MHA : AttentionType::FUSED_PADDED_MHA;
        }
        ...
    }
}
```

GPT（causal）分支依赖环境变量（[BaseAttentionLayer.h:L70-L80](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/BaseAttentionLayer.h#L70-L80)）：

```cpp
else {  // GPT and its variants
    char* fused_qkv = std::getenv("FMHA_ENABLE");
    if (fused_qkv != nullptr && std::string(fused_qkv) == "ON") {
        if (... && (size_per_head == 32 || ... || size_per_head == 256)) {
            return remove_padding ? AttentionType::FUSED_MHA : AttentionType::UNFUSED_PADDED_MHA;
        }
    }
}
```

虚基类 `BaseAttentionLayer` 给出统一接口（[BaseAttentionLayer.h:L139-L159](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/BaseAttentionLayer.h#L139-L159)）：所有注意力层都实现 `forward(TensorMap*, TensorMap*, const AttentionWeight<T>*)`，并继承自 `BaseLayer`（持有 stream / cublas_wrapper / allocator，见 u3-l5）。

#### 4.1.4 代码实践

**实践目标**：搞清一台机器上一笔 BERT 推理实际会走哪条注意力路径。

**操作步骤**：

1. 打开 [BaseAttentionLayer.h:L46-L96](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/BaseAttentionLayer.h#L46-L96) 的 `getAttentionType` 模板。
2. 假设你的 GPU 是 Ampere（`sm == kSM_80`）、`size_per_head == 64`、`T = half`、`remove_padding = true`、`causal_mask = false`、`is_fuse = true`。
3. 顺着函数体手算返回值。

**需要观察的现象 / 预期结果**：应当命中第 60-61 行的条件，返回 `FUSED_MHA`。再把 `size_per_head` 改成 48（很多 GPT-J 变体用 48）重新手算——会落入函数末尾的兜底 `UNFUSED_MHA`。这说明融合路径对 `size_per_head` 的要求非常严格，这也是 FT 必须保留 Unfused 兜底的原因。

> 待本地验证：如果你手头有 GPU，可在调试时打印 `attention_type` 的整数值，对照枚举顺序（0/1/2/3）确认。

#### 4.1.5 小练习与答案

**练习 1**：`isFusedMHA(FUSED_PADDED_MHA)` 返回什么？为什么需要这个工具函数？
**答案**：返回 `true`。它（[BaseAttentionLayer.h:L113-L116](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/BaseAttentionLayer.h#L113-L116)）把「FUSED_MHA 和 FUSED_PADDED_MHA」统一识别为融合路径，模型代码只需调 `isFusedMHA(type)` 而不必关心 padding 维度。

**练习 2**：为什么 GPT 类模型默认不启用融合路径？
**答案**：GPT 的序列长度任意且融合 kernel 对 GPT 的支持受限（见注释 [BaseAttentionLayer.h:L40-L44](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/BaseAttentionLayer.h#L40-L44)），故默认走 Unfused，只有用户显式设 `FMHA_ENABLE=ON` 且 `size_per_head` 命中支持表时才尝试融合。

---

### 4.2 UnfusedAttentionLayer：朴素显式注意力路径

#### 4.2.1 概念说明

`UnfusedAttentionLayer` 把注意力公式**逐项显式展开**：每一步 `QK^T`、softmax、PV 都用一个独立的 cuBLAS GEMM 或 FT kernel 完成，中间结果完整存在显存里。它是**最通用、最易懂、也是最后兜底**的实现：任何 `size_per_head`、任何受支持的数据类型都能跑，代价是中间显存读写多。

它服务的是 BERT、ViT、T5-encoder 这类**编码器/上下文**注意力（一次性处理整段序列），与「逐 token」的 Decoder 自注意力（4.5 节）不同。

#### 4.2.2 核心流程

一次 `forward` 把「输入 → 注意力输出」拆成 7 步：

```
输入 from_tensor [token_num, d_model]
   │
   ①  QKV 投影：3 个 GEMM（或 batchedGemm）
   ▼  q_buf_/k_buf_/v_buf_  [token_num, hidden_units]
   ②  加 bias + 转置 [B,H,L,Dh]：invokeAddQKVBiasIA3Transpose
   ▼  q_buf_2_/k_buf_2_/v_buf_2_  [B, L, H, Dh]
   ③  QK^T：stridedBatchedGemm（每个 head 一个矩阵乘，带 1/√dk 缩放）
   ▼  qk_buf_  [B, H, L, L]
   ④  （可选）加相对位置 bias：invokeAddRelativeAttentionBias
   ⑤  masked softmax：invokeMaskedSoftmax
   ▼  qk_buf_（原地）
   ⑥  softmax · V：stridedBatchedGemm
   ▼  qkv_buf_  [B, L, H, Dh]
   ⑦  转置回 [token_num, hidden_units]：invokeTransposeQKV
   ▼  qkv_buf_2_
   ⑧  输出投影：GEMM（attention_output_weight）
   ▼
输出 hidden_features [token_num, d_model]
```

注意 ③ 和 ⑥ 都是**按 head 分组的批量矩阵乘**：`batch = request_batch_size * head_num`，每个矩阵是 \([L, L]\) 或 \([L, Dh]\)。这正是 cuBLAS `stridedBatchedGemm` 的用武之地（u2-l3）。

#### 4.2.3 源码精读

QKV 投影：优先用 `batchedGemm`（3 个矩阵拼一次调），否则退回 3 次独立 `Gemm`。判定由 `cublas_wrapper_->isFuseBatchGemm(3, n, m, k)` 决定（[UnfusedAttentionLayer.cc:L76-L134](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/UnfusedAttentionLayer.cc#L76-L134)）。下面是 batched 分支的关键片段：

```cpp
const bool is_batched_QKV_ = cublas_wrapper_->isFuseBatchGemm(3, n, m, k);
if (is_batched_QKV_) {
    const T* hA[]{/*query/key/value kernel*/, nullptr, from_tensor, ...};
    cudaMemcpyAsync((void*)batch_qkv_kernel_ptr_, hA, sizeof(T*) * 12, cudaMemcpyHostToDevice, stream_);
    cublas_wrapper_->batchedGemm(CUBLAS_OP_N, CUBLAS_OP_N, n, m, k,
                                 (const void* const*)batch_qkv_kernel_ptr_, n,
                                 (const void* const*)batch_qkv_input_ptr_,  k,
                                 (void* const*)batch_qkv_buf_ptr_,          n, 3);
}
```

\(QK^\top\) 的 stridedBatchedGemm（[UnfusedAttentionLayer.cc:L192-L207](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/UnfusedAttentionLayer.cc#L192-L207)）：注意 `CUBLAS_OP_T`（K 转置）、缩放因子 `scalar = 1/(√size_per_head * q_scaling_)`、batch 数为 `request_batch_size * head_num_`：

```cpp
float scalar = 1 / (sqrtf(size_per_head_ * 1.0f) * q_scaling_);
cublas_wrapper_->stridedBatchedGemm(CUBLAS_OP_T, CUBLAS_OP_N,
        request_seq_len, request_seq_len, size_per_head_,
        k_buf_2_, ..., q_buf_2_, ..., qk_buf_,
        request_seq_len, request_seq_len * request_seq_len,
        request_batch_size * head_num_, scalar);
```

masked softmax（[UnfusedAttentionLayer.cc:L215-L226](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/UnfusedAttentionLayer.cc#L215-L226)）把 mask 融进 softmax，避免单独写一遍 mask 加法：

```cpp
MaskedSoftmaxParam<T, T> param;
param.qk = qk_buf_;  param.attention_mask = attention_mask;
param.q_length = request_seq_len;  param.k_length = request_seq_len;
param.qk_scale = 1.0f;  // 注意：缩放已经在 ③ 里乘过了，这里 scale=1
invokeMaskedSoftmax(param, stream_);
```

softmax · V 的 stridedBatchedGemm 与最后的输出投影 GEMM（[UnfusedAttentionLayer.cc:L238-L306](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/UnfusedAttentionLayer.cc#L238-L306)）。

中间 buffer 全部由 `IAllocator::reMalloc` 申请且 `is_free_buffer_after_forward_` 控制是否每步释放（[UnfusedAttentionLayer.cc:L394-L410](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/UnfusedAttentionLayer.cc#L394-L410)），这是 u3-l5 将详讲的 buffer 生命周期模式。

#### 4.2.4 代码实践

**实践目标**：数清 Unfused 路径一共申请了哪些中间 buffer，估算它们的显存占用。

**操作步骤**：

1. 打开 [UnfusedAttentionLayer.cc:L394-L410](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/UnfusedAttentionLayer.cc#L394-L410) 的 `allocateBuffer`。
2. 列出 `q_buf_`、`k_buf_`、`v_buf_`、`q_buf_2_`（注意它还分出 k_buf_2_/v_buf_2_）、`qk_buf_`、`qkv_buf_`、`qkv_buf_2_`、`batch_qkv_kernel_ptr_` 的尺寸公式。
3. 代入 `batch=32, seq=128, head=12, size_per_head=64, FP16` 手算。

**需要观察的现象 / 预期结果**：

- `q_buf_/k_buf_/v_buf_` 各为 `B*L*hidden_units = 32*128*768` 个 FP16 ≈ 6 MiB，3 个共约 18 MiB。
- `qk_buf_` 为 `B*H*L*L = 32*12*128*128` 个 FP16 ≈ 12 MiB——这是**最大头**，也正说明朴素实现的瓶颈：要物化整个 \([B,H,L,L]\) 分数矩阵。
- 加起来单层临时显存约 50 MiB 量级，且每步都要读写。

> 这个估算会让你直观感受到 4.3 节融合路径「为什么快」：它根本不物化 `qk_buf_`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 ③ 的 `stridedBatchedGemm` 第一个操作是 `CUBLAS_OP_T`（K 转置），而 ⑥ 的 `softmax·V` 是 `CUBLAS_OP_N`？
**答案**：\(QK^\top\) 数学上需要对 K 求转置，故 K 用 `OP_T`；而 softmax 后乘 V 是 \(P \cdot V\) 不需要转置 V，故 `OP_N`。leading dimension 都按 cuBLAS 列主序约定填。

**练习 2**：步骤 ⑤ 里 `param.qk_scale = 1.0f`，但 softmax 公式里明明有 \(1/\sqrt{d_k}\)，这是不是 bug？
**答案**：不是。\(1/\sqrt{d_k}\) 的缩放已经在 ③ 的 `scalar` 里乘进 `qk_buf_` 了，所以 softmax 时 `qk_scale=1`，避免重复缩放。这是 FT 一贯的「把缩放前移到 GEMM 的 alpha」优化。

---

### 4.3 FusedAttentionLayer：TensorRT 融合 MHA 路径

#### 4.3.1 概念说明

`FusedAttentionLayer` 服务 BERT/ViT 这类编码器，结构与 Unfused 一模一样（同样是 QKV 投影 → 注意力 → 输出投影），唯一区别是**中间的「QK^T → softmax → PV」三步被换成一次 `MHARunner::run()`**。这个 `MHARunner` 来自 FT 内嵌的 TensorRT 融合多头注意力实现（`3rdparty/trt_fused_multihead_attention/qkvToContext.h`），它把分数矩阵、softmax 中间量全部留在共享内存/寄存器里，不物化到显存。

这和 u3-l2 讲过的 `decoder_masked_multihead_attention` 是**同一思想的两份实现**：一个面向 decoder（单 query），一个面向 context（整段序列）。

#### 4.3.2 核心流程

```
输入 from_tensor [token_num, d_model]
   │
   ①  QKV 投影：3 个 GEMM / batchedGemm（与 Unfused 完全相同）
   ▼  q_buf_/k_buf_/v_buf_  [token_num, hidden_units]
   ②  trt_add_QKV_bias：加 bias 并把 [3,token,H,size] 转成 [token,H,3,size]
   ▼  qkv_buf_
   ③  dispatcher_fp16->run(...)：★融合 MHA kernel★
        内部完成 QK^T + softmax + PV，写 qkv_buf_2_
   ▼  qkv_buf_2_  [token_num, hidden_units]
   ④  输出投影：GEMM（attention_output_weight）
   ▼
输出 hidden_features [token_num, d_model]
```

对比 4.2.2 的 8 步，这里只剩 4 步，且**没有 qk_buf_、没有 stridedBatchedGemm**。

#### 4.3.3 源码精读

构造时根据 `(sm, size_per_head)` 实例化融合 runner，否则直接抛异常（[FusedAttentionLayer.cu:L241-L248](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/FusedAttentionLayer.cu#L241-L248)）：

```cpp
if (((sm_ == kSM_70 || sm_ == kSM_86 || sm_ == kSM_80 || sm_ == kSM_75 || sm_ == kSM_72) && size_per_head_ == 64)
    || ((sm_ == kSM_86 || sm_ == kSM_80 || sm_ == kSM_75) && size_per_head_ == 32)) {
    dispatcher_fp16.reset(new FusedMHARunnerFP16v2(head_num_, size_per_head_, sm_, q_scaling_));
} else {
    throw std::runtime_error(std::string("[FT][ERROR] FusedAttentionLayer not support \n"));
}
```

融合注意力的实际调用只有两行（[FusedAttentionLayer.cu:L177-L181](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/FusedAttentionLayer.cu#L177-L181)）：

```cpp
int S = dispatcher_fp16->getSFromMaxSeqLen(request_seq_len);
FT_CHECK(dispatcher_fp16->isValid(S, false));
const int B = input_tensors->at("padding_offset").shape[0] - 1;
dispatcher_fp16->setup(S, B);
dispatcher_fp16->run(qkv_buf_, nullptr, padding_offset, attn_workspace_, qkv_buf_2_, stream_);
```

`setup(S, B)` 会根据序列长度动态选择内部 block 配置（这正是 u3-l2 提到的「按 seq_len 选档位」），`run()` 启动融合 kernel。

`trt_add_QKV_bias`（[FusedAttentionLayer.cu:L21-L50](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/FusedAttentionLayer.cu#L21-L50)）是 fusion 自带的 bias 加法 + 转置 kernel，注释写得很清楚：

```cpp
// Add bias, and then transpose from
// [3, valid_word_num, head, size] -> [valid_word_num, head, 3, size]
```

注意它用 `half2`（两个 FP16 打包）做向量化读写，是 FT 在 FP16 kernel 里的常见优化。

#### 4.3.4 代码实践

**实践目标**：对比 Fused 与 Unfused 的 `freeBuffer`，直观感受融合路径省下了哪些 buffer。

**操作步骤**：

1. 打开 [FusedAttentionLayer.cu:L299-L314](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/FusedAttentionLayer.cu#L299-L314) 的 `freeBuffer`，再对照 4.2.3 的 Unfused `freeBuffer`。
2. 列出两者释放的指针集合差集。

**需要观察的现象 / 预期结果**：

| buffer | Unfused | Fused |
|--------|:-------:|:-----:|
| q_buf_/k_buf_/v_buf_ | ✓（3 个） | ✓（3 个） |
| q_buf_2_（含 k2/v2） | ✓ | ✗ |
| **qk_buf_（分数矩阵）** | **✓（最大头）** | **✗（不物化）** |
| qkv_buf_ / qkv_buf_2_ | ✓ | ✓ |
| attn_workspace_ | ✗ | ✓（融合 kernel 工作区，通常很小） |

Fused 的 `allocateBuffer`（[FusedAttentionLayer.cu:L282-L297](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/FusedAttentionLayer.cu#L282-L297)）里完全没有 `qk_buf_`，这正是它省显存、省带宽的根因。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `FusedAttentionLayer` 构造时若 `(sm, size_per_head)` 不在支持表里就直接抛异常，而不是退回 Unfused？
**答案**：退回 Unfused 是**上层**（`getAttentionType`）的职责。`FusedAttentionLayer` 一旦被实例化，就代表模型已经决定走融合路径；若硬件不支持，根本不应该走到这里，所以用异常暴露「配置错误」而不是静默退回，避免性能不符合预期。

**练习 2**：`trt_add_QKV_bias` 用 `half2` 而非 `half` 访存，收益在哪？
**答案**：`half2` 一次读写 32 位（两个 FP16），对齐显存事务、提升带宽利用率，且可配合 `__hadd2` 等向量化指令。这是 FP16 kernel 的通用优化套路（见 u3-l1）。

---

### 4.4 Unfused 与 Fused 的取舍对比

把 4.2 和 4.3 放在一起，FT 同时保留两条路径的原因就清楚了：

| 维度 | UnfusedAttentionLayer | FusedAttentionLayer |
|------|----------------------|---------------------|
| 核心计算 | cuBLAS GEMM + softmax kernel | TensorRT 融合 MHA kernel |
| 中间 buffer | 多（含物化的 \([B,H,L,L]\) 分数矩阵） | 少（分数矩阵留共享内存） |
| 性能 | 通用、可接受 | 同形状下更快（省带宽） |
| `size_per_head` 限制 | 几乎任意 | 仅 {32, 64}（编码器）/有限档位 |
| 数据类型限制 | FP32/FP16/BF16/INT8/FP8 全支持 | 仅 FP16（及部分 FP8） |
| 适用场景 | 兜底、特殊形状、调试 | 主力加速路径（BERT/ViT 推理） |

一句话总结：**能用 Fused 就用 Fused，不能用时 Unfused 兜底**，这个选择由 `getAttentionType()` 在运行期自动完成，模型代码无需关心。

### 4.5 Decoder 自注意力与 GPT Context 注意力的职责分工

#### 4.5.1 概念说明

上面 4.2~4.4 讲的 Unfused/Fused 都是「**一次处理一整段序列**」的注意力（编码器风格）。但 GPT 这类自回归生成模型有两种截然不同的注意力形态，对应两个专门的层：

- **`GptContextAttentionLayer`**：在生成开始时处理**整段 prompt**（比如 128 个 token 一起进）。它的计算量和 BERT 一样是 \(O(L^2)\)，可以用 Fused 也可以用 Unfused，并且要**把这一步的 K/V 写进 KV cache** 供后续步骤复用。
- **`DecoderSelfAttentionLayer`**：在后续每一步只处理**新生成的 1 个 token**。它读 KV cache 里历史所有的 K/V，用 u3-l2 讲过的 `masked_multihead_attention` 单 query 融合 kernel 算出注意力，再把新 token 的 K/V 追加进 cache。

这就是 u6-l1 将详细讲的「Context / Decoder 两阶段分裂」在**注意力层**这一级的体现。本讲只需理解：**两个层各自只擅长一种形状，不可互换**。

#### 4.5.2 核心流程

**DecoderSelfAttentionLayer** 的 forward（[DecoderSelfAttentionLayer.cc:L458-L686](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/DecoderSelfAttentionLayer.cc#L458-L686)）只有三步：

```
① qkv_gemm：一次 GEMM，输入 [batch, d_model] → qkv_buf_ [batch, 3*local_hidden_units]
            （注意：Q/K/V 权重在权重加载时已拼成 [3*hidden, d_model]，故只需 1 次 GEMM）
② fusedQKV_masked_attention_dispatch：
            加 bias → rotary → 写 K/V cache → masked MHA → context_buf_
③ proj gemm：输出投影 GEMM，context_buf_ → attention_out [batch, d_model]
```

关键点：① 只做 **1 次** GEMM（合并的 QKV），而不是 Unfused 的 3 次；② 直接调用 u3-l2 的 `masked_multihead_attention(params, stream)`（[DecoderSelfAttentionLayer.cc:L143-L145](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/DecoderSelfAttentionLayer.cc#L143-L145)）。

**GptContextAttentionLayer** 的 forward（[GptContextAttentionLayer.cc:L24-L403](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/GptContextAttentionLayer.cc#L24-L403)）则在运行期按 `attention_type` 二选一（[GptContextAttentionLayer.cc:L194-L197](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/GptContextAttentionLayer.cc#L194-L197)）：

```cpp
if (attention_type == AttentionType::FUSED_MHA) {
    dispatcher_fp16->setup_causal_masked_fmha(request_seq_len, request_batch_size);
    dispatcher_fp16->run_causal_masked_fmha(qkv_buf_, cu_seqlens, qkv_buf_3_, true, stream_);
}
// 否则走 stridedBatchedGemm + invokeMaskedSoftmax 的朴素路径（is_final==false 分支内）
```

它额外做的一件重要事是**把 K/V 转置写进 cache**（[GptContextAttentionLayer.cc:L179-L191](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/GptContextAttentionLayer.cc#L179-L191)），`invokeTranspose4dBatchMajor` 把 `[B,H,L,Dh]` 重排成 cache 布局 `[B,H,Dh/x,PL+L,x]`，这正是 u6-l2 KV cache 内存布局的来源。

> 这两个层在权重容器里都引入了 `local_head_num_` / `local_hidden_units_`（见 [GptContextAttentionLayer.h:L36-L39](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/GptContextAttentionLayer.h#L36-L39)），`local_` 前缀就是「本卡负责的 head 数」，为张量并行预留——下一节解释。

#### 4.5.3 源码精读（Decoder 单 query 路径）

合并 QKV 的单次 GEMM（[DecoderSelfAttentionLayer.cc:L566-L577](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/DecoderSelfAttentionLayer.cc#L566-L577)）：

```cpp
cublas_wrapper_->Gemm(CUBLAS_OP_N, CUBLAS_OP_N,
                      3 * local_hidden_units_,  // n：Q/K/V 三段拼一起
                      batch_size,
                      d_model_,                 // k
                      attention_weights->query_weight.kernel,
                      3 * local_hidden_units_, attention_input, d_model_,
                      qkv_buf_, 3 * local_hidden_units_);
```

注意输出维度是 `3 * local_hidden_units_`，说明 Q/K/V 三个权重矩阵在加载时已被纵向拼接成一个 `[3*local_hidden, d_model]` 的大矩阵，于是 1 次 GEMM 就拿到 qkv。这是**解码路径独有的优化**（因为 batch=beam，M 很小，启动 3 次 GEMM 的开销占比高）。

`fusedQKV_masked_attention_dispatch` 把一堆参数打包成 `Masked_multihead_attention_params` 后调起 kernel（[DecoderSelfAttentionLayer.cc:L86-L145](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/DecoderSelfAttentionLayer.cc#L86-L145)），其中 `params.timestep = step + max_prefix_prompt_length - 1` 决定写 cache 的位置、`params.inv_sqrt_dh` 携带 \(1/\sqrt{d_k}\) 缩放。

#### 4.5.4 代码实践

**实践目标**：从输入张量形状反推「为什么 Decoder 用 1 次合并 GEMM，而 Context 用 3 次/batched」。

**操作步骤**：

1. 读 [DecoderSelfAttentionLayer.cc:L463-L482](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/DecoderSelfAttentionLayer.cc#L463-L482) 的注释，记下 `input_query` 的形状 `[batch_size, d_model_]`。
2. 读 [UnfusedAttentionLayer.cc:L27-L36](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/UnfusedAttentionLayer.cc#L27-L36) 的注释，记下 `input_query` 的形状 `[token_num, d_model]`（token_num = batch * seq）。
3. 思考：cuBLAS 启动一次 GEMM 有固定开销，当 M（矩阵行数）很大时这个开销可忽略，当 M 很小（Decoder 只算 1 个 token × batch）时就占比很高。

**需要观察的现象 / 预期结果**：Decoder 阶段 M 极小（`batch_size` 量级，通常 ≤ 32），3 次独立 GEMM 的 kernel 启动开销不可忽略，所以合并成 1 次；Context 阶段 M = `batch*seq` 很大（成百上千），启动开销可忽略，反而 3 次/batched 更灵活（能复用 BERT 的 unfused/fused 两条路径）。

> 待本地验证：可对比同一模型在「只解码 1 步」vs「处理 128 长度 prompt」下的 GEMM 数量与耗时占比（用 NVTX，见 u1-l5）。

#### 4.5.5 小练习与答案

**练习 1**：`GptContextAttentionLayer` 为什么要调用 `invokeTranspose4dBatchMajor` 把 K/V 写成 `[B,H,Dh/x,PL+L,x]` 这种奇怪布局？
**答案**：这是 KV cache 的存储布局（u6-l2 详讲）。`x` 是为对齐 tensor core 访存的拆分因子，把 `Dh` 拆成 `Dh/x` 与 `x` 两维，让后续 Decoder 步骤能以最高效的内存访问模式读取历史 K/V。

**练习 2**：`DecoderSelfAttentionLayer` 的 GEMM 输出维度是 `3 * local_hidden_units_`，其中 `3` 从哪来？
**答案**：Q、K、V 三个投影矩阵沿输出维拼接，故一份 `[3*local_hidden, d_model]` 权重 + 一次 GEMM 就同时算出 Q、K、V 三段，省去 2 次 kernel 启动。

---

### 4.6 TensorParallel 注意力层：列切分 + 行切分 + 一次 all-reduce

#### 4.6.1 概念说明

`TensorParallelUnfusedAttentionLayer` 和 `TensorParallelGptContextAttentionLayer` 是本讲最重要的「架构」知识点。它们实现张量并行的手法非常优雅，可以用一句话概括：

> **继承单卡层，构造时把 `head_num` 除以 `world_size`，forward 末尾加一次 all-reduce。**

为什么这样就够了？回到 §2 提到的关键直觉——**注意力逐头独立**。结合 Megatron 式切分：

1. **QKV 投影（列并行）**：把 `head_num` 个 head 按卡均分，每卡只持有自己那 `head_num/world_size` 个 head 的 Q/K/V 权重。每卡算出的 Q/K/V 只覆盖自己的 head，**互不重叠，无需通信**。
2. **注意力核心计算**：每卡用自己的 head 独立算 `QK^T → softmax → PV`，结果仍是「自己的 head」上的注意力输出，**无需通信**。
3. **输出投影（行并行）**：输出权重沿输入维（`hidden_units` 维）切分，每卡用自己的 `local_hidden_units` 算出**部分和**。要得到正确的完整输出，必须把所有卡的部分和相加——**这就是唯一的 all-reduce 通信点**。

所以整个注意力层只有**一次**通信，且在最末尾。`do_all_reduce_` / `enable_custom_all_reduce_` 开关还允许在某些情况下把这次 all-reduce 延后或换成自定义低延迟 kernel（u7-l3）。

#### 4.6.2 核心流程

以 `TensorParallelUnfusedAttentionLayer` 为例：

```
构造：调父类 UnfusedAttentionLayer 构造，但 head_num 传入 head_num / world_size
      （= local_head_num）；FT_CHECK(head_num % world_size == 0)

forward：
   ①（可选）准备自定义 all-reduce 的内部 buffer 交换
   ② 调父类 UnfusedAttentionLayer::forward(...)  ← 每卡独立算自己那几个 head
   ③ if (world_size > 1):
        ftNcclAllReduceSum(attention_out, attention_out, size, tensor_para_, stream_)
        或 custom_all_reduce_comm_->customAllReduce(...)
```

`TensorParallelGptContextAttentionLayer` 完全同构，只是父类换成 `GptContextAttentionLayer`，并多一个 `do_all_reduce_` 开关——当某层不是「需要立刻拿到完整输出」的位置时，可设 `do_all_reduce_=false` 把通信省掉或延后（序列并行优化的入口，u7-l1 详讲）。

#### 4.6.3 源码精读

`TensorParallelUnfusedAttentionLayer` 的构造（[TensorParallelUnfusedAttentionLayer.cc:L64-L95](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/TensorParallelUnfusedAttentionLayer.cc#L64-L95)）——注意传给父类的 `head_num / tensor_para.world_size_`：

```cpp
: UnfusedAttentionLayer<T>(max_batch_size, max_seq_len,
                           head_num / tensor_para.world_size_,  // ★ 列并行：每卡只负责这么多 head
                           size_per_head, d_model, q_scaling,
                           stream, cublas_wrapper, allocator,
                           is_free_buffer_after_forward, is_sparse),
  tensor_para_(tensor_para), ...
{
    FT_CHECK(head_num % tensor_para_.world_size_ == 0);  // 必须整除
}
```

forward 的核心（[TensorParallelUnfusedAttentionLayer.cc:L39-L61](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/TensorParallelUnfusedAttentionLayer.cc#L39-L61)）——先复用父类，再 all-reduce：

```cpp
UnfusedAttentionLayer<T>::forward(output_tensors, input_tensors, attention_weights);  // ② 每卡独立

T* attention_out = output_tensors->getPtr<T>("hidden_features");
if (tensor_para_.world_size_ > 1) {                                                  // ③ 唯一通信点
    if (!use_custom_all_reduce_kernel) {
        ftNcclAllReduceSum(attention_out, attention_out, size, tensor_para_,
                           UnfusedAttentionLayer<T>::stream_);
    } else {
        custom_all_reduce_comm_->customAllReduce(size, UnfusedAttentionLayer<T>::stream_);
        ...
    }
}
```

`TensorParallelGptContextAttentionLayer` 的对应代码（[TensorParallelGptContextAttentionLayer.cc:L46-L60](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/TensorParallelGptContextAttentionLayer.cc#L46-L60)）多了 `do_all_reduce_` 守卫和 NVTX 标记：

```cpp
GptContextAttentionLayer<T>::forward(output_tensors, input_tensors, attention_weights);

PUSH_RANGE("all reduce sum");
T* attention_out = output_tensors->getPtr<T>("hidden_features");
if (do_all_reduce_ && tensor_para_.world_size_ > 1) {
    if (!use_custom_all_reduce_kernel) {
        ftNcclAllReduceSum(attention_out, attention_out, size, tensor_para_,
                           GptContextAttentionLayer<T>::stream_);
    }
    ...
}
POP_RANGE;
```

#### 4.6.4 代码实践（本讲综合实践）

**实践目标**：画出 Unfused 与 Fused 两条注意力路径的完整数据流图，并标注 TensorParallel 版本插入 all-reduce 的精确位置。

**操作步骤**：

1. 准备一张白纸或绘图工具，横向画两个并列流程。
2. **左路（Unfused）**：按 4.2.2 的 7 步画出节点，每个节点标注：用的 GEMM/kernel 名 + 调用形式（`Gemm` / `stridedBatchedGemm` / `invokeXxx`）。可参考源码：
   - QKV：`batchedGemm` 或 3×`Gemm`（[UnfusedAttentionLayer.cc:L94-L134](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/UnfusedAttentionLayer.cc#L94-L134)）
   - \(QK^\top\)：`stridedBatchedGemm`（[L192-L207](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/UnfusedAttentionLayer.cc#L192-L207)）
   - softmax：`invokeMaskedSoftmax`（[L215-L226](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/UnfusedAttentionLayer.cc#L215-L226)）
   - \(PV\)：`stridedBatchedGemm`（[L238-L252](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/UnfusedAttentionLayer.cc#L238-L252)）
   - 输出投影：`Gemm`（[L296-L306](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/UnfusedAttentionLayer.cc#L296-L306)）
3. **右路（Fused）**：按 4.3.2 画，把 \(QK^\top\)+softmax+\(PV\) 合并成一个 `dispatcher_fp16->run()` 节点（[FusedAttentionLayer.cu:L177-L181](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/FusedAttentionLayer.cu#L177-L181)）。
4. 在**两条路径共同的「输出投影 GEMM」之后**，画一个红色虚线框 `ftNcclAllReduceSum`，并用箭头注明「仅在 `tensor_para.world_size > 1` 时执行」，引用 [TensorParallelUnfusedAttentionLayer.cc:L50-L60](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/TensorParallelUnfusedAttentionLayer.cc#L50-L60)。
5. 在 QKV 投影节点旁批注「列并行：`head_num/world_size`，无通信」；在输出投影节点旁批注「行并行：部分和，需 all-reduce」。

**需要观察的现象 / 预期结果**：

成品图应当清楚显示三件事：
- Unfused 比 Fused 多出 `qk_buf_` 物化节点和两次 `stridedBatchedGemm`。
- **两条路径的通信点位置完全相同**——都在输出投影之后、返回上层之前，且全层仅此一处。
- 通信量的字节数 = `size * sizeof(T)`，其中 `size = token_num * hidden_units`（输出张量的元素数），与 head 数无关——这正是 head 切分带来的好处。

> 待本地验证：如果有 2 张以上 GPU，可在 `FT_LOG_LEVEL=DEBUG` 下观察一次 forward 里 `ftNcclAllReduceSum` 被调用的次数，验证「每层仅 1 次通信」。

#### 4.6.5 小练习与答案

**练习 1**：为什么 QKV 投影是「列并行且无需通信」，而输出投影是「行并行且需 all-reduce」？
**答案**：QKV 投影沿输出维（head 维）切，每卡算出的是**不同的 head** 的 Q/K/V，集合起来才是完整结果，但因为后续注意力本就逐头独立，每卡直接用自己的 head 算下去即可，无需先合并；输出投影沿输入维（`hidden_units` 维）切，每卡算出的是**同一个输出的部分和**，数学上必须相加才正确，故需 all-reduce。

**练习 2**：`TensorParallelGptContextAttentionLayer` 的 `do_all_reduce_=false` 时会怎样？
**答案**：跳过 ③ 的 all-reduce，本卡返回「部分和」而非完整输出。这是一种延迟通信优化：若紧接的下一层也能在部分和上工作（或这是可推迟的位置），就能把两次 all-reduce 合并，减少通信次数（详见 u7-l1 序列并行）。

**练习 3**：`TensorParallelUnfusedAttentionLayer` 继承 `UnfusedAttentionLayer` 而不是持有它，这种设计的好处是什么？
**答案**：继承使得父类的全部计算逻辑（QKV、softmax、PV、buffer 管理）被原样复用，子类只需重写 `forward` 做「调用父类 + 加 all-reduce」的包装，避免代码重复；同时父类构造时拿到的是 `local_head_num`，所有内部 GEMM 维度自动适配，无需在父类里感知「是否并行」。

---

## 5. 综合实践

把本讲四条主线串起来，完成下面这个**「注意力层选型表 + 调用链追踪」**任务。

**任务**：假设你要在一个 4 卡 Ampere（sm_80）节点上跑 GPT 推理，`head_num=16, size_per_head=64, FP16, remove_padding=true`，`tensor_para_size=4`。请完成：

1. **选型**：用 [getAttentionType](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/BaseAttentionLayer.h#L45-L96) 判断：
   - Context 阶段（`causal_mask=true`）默认走哪种 `AttentionType`？如果设 `FMHA_ENABLE=ON` 呢？
   - Decoder 阶段用的是哪个层？它受 `AttentionType` 影响吗？
2. **追踪调用链**：写出 Context 阶段**单层**注意力从模型入口到 all-reduce 的完整调用链，标注每一步是 GEMM、kernel 还是通信。参考 [TensorParallelGptContextAttentionLayer.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/TensorParallelGptContextAttentionLayer.cc) → [GptContextAttentionLayer.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/GptContextAttentionLayer.cc)。
3. **切分验证**：计算每张卡实际负责几个 head（`local_head_num`），并指出 `FT_CHECK(head_num % world_size == 0)` 在本例是否通过。
4. **缓冲对比**：若把 Context 阶段从 Unfused 切到 Fused（设 `FMHA_ENABLE=ON`），列出 `qk_buf_` 这一块 buffer 是否还存在、为什么。

**参考要点**（请先自己作答再对照）：

1. Context 默认 `UNFUSED_MHA`（因 GPT 默认不融合）；设 `FMHA_ENABLE=ON` 且 `size_per_head=64∈{32,…,256}`、`sm_80` 命中 → `FUSED_MHA`。Decoder 用 `DecoderSelfAttentionLayer`，它直接调 `masked_multihead_attention` kernel，**不经过 `AttentionType` 分发**。
2. `TensorParallelGptContextAttentionLayer::forward` → 父类 `GptContextAttentionLayer::forward`（QKV `Gemm` → `invokeAddFusedQKVBiasTranspose` → `invokeTranspose4dBatchMajor` 写 cache → `stridedBatchedGemm`(QK^T) → `invokeMaskedSoftmax` → `stridedBatchedGemm`(PV) → `invokeTransposeQKV` → 输出投影 `Gemm`）→ 回到子类 `ftNcclAllReduceSum`。
3. `local_head_num = 16/4 = 4`，`16 % 4 == 0` 通过。
4. Fused 路径下 `qk_buf_` **不存在**，因为融合 kernel 把分数矩阵留在共享内存里，这正是融合省显存/省带宽的根本（对照 4.3.4 表格）。

## 6. 本讲小结

- FT 的注意力层是一棵继承树，统一接口 `forward(TensorMap*, TensorMap*, const AttentionWeight<T>*)`；走哪条路径由 [BaseAttentionLayer.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/BaseAttentionLayer.h) 的 `AttentionType` 枚举 + `getAttentionType()` 在运行期按 `(sm, size_per_head, remove_padding, causal_mask)` 自动决策。
- **Unfused** = cuBLAS GEMM + `invokeMaskedSoftmax` 显式展开，通用、全数据类型、任意 `size_per_head`，但要物化 \([B,H,L,L]\) 分数矩阵 `qk_buf_`。
- **Fused** = QKV 投影后交给 TensorRT `MHARunner` 融合 kernel，省掉分数矩阵的显存往返，更快但仅支持 FP16、`size_per_head ∈ {32,64}` 等受限形状。
- **GptContextAttentionLayer** 处理整段 prompt（可用 fused/unfused，并写 KV cache）；**DecoderSelfAttentionLayer** 处理逐 token 解码（单 query，1 次合并 QKV GEMM + u3-l2 的 `masked_multihead_attention` kernel）。两者不可互换。
- **TensorParallel** 注意力层 = 继承单卡层 + `head_num/world_size` 列切分 + 末尾一次 `ftNcclAllReduceSum`；全层**唯一通信点**在输出投影之后，因为输出投影是行并行（部分和）。
- `do_all_reduce_` 开关允许延迟/省略通信，是序列并行等高级优化的入口（u7-l1）。

## 7. 下一步学习建议

- **向上下延伸**：向上读 `models/bert/Bert.cc`（u4-l1）看注意力层如何被串进 transformer block；向下复习 u3-l2 的 `decoder_masked_multihead_attention` kernel，确认「层」与「kernel」的边界。
- **KV cache 布局**：本讲提到 `invokeTranspose4dBatchMajor` 写出的 `[B,H,Dh/x,L,x]` 布局，下一阶段读 u6-l2（KV Cache 机制）深入理解这个布局如何在解码步被追加与重排。
- **通信与并行**：本讲的 all-reduce 是张量并行的最小示例，u7-l1 会把 `NcclParam`、`ftNccl*` 原语与 FFN 层的切分一起系统讲；u7-l3 讲自定义 all-reduce kernel 如何在 DGX-A100 上进一步降低这次通信的延迟。
- **变体注意力**：目录里的 `LongformerAttentionLayer`、`WindowAttention`（Swin）、`DecoderCrossAttentionLayer` 是面向特定模型的注意力，可在读完 u4-l3（ViT/Swin）和 u5-l1（Decoder）后按需阅读，它们都复用了本讲建立的「layer = GEMM + kernel + 可选通信」骨架。
