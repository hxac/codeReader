# Effective FasterTransformer：去除 padding

## 1. 本讲目标

上一讲（u4-l1）我们读完了 `Bert::forward` 的主循环，知道一个 transformer block 由「8 或 6 个 GEMM + 6 个 custom kernel」组成。但当时我们刻意回避了一个细节：**forward 里到底处理了多少个 token？**

本讲专门回答这个问题。读完本讲，你应当能够：

1. 说清楚「padding」在批处理 transformer 时为什么会带来大量**无效计算**，并用一个比例公式量化它。
2. 说清楚 Effective FasterTransformer 的核心思想——**把 padding 去掉（compact）、计算完再恢复（uncompact）**，以及这套 compact/uncompact 是由哪几个 `invokeXxx` kernel 完成的。
3. 读懂 `bert_preprocess_kernels.cu` 里 `padding_offset` 是如何构造的，并能用一个 2 句话的小例子手算出 offset 数组。
4. 说清楚 `Bert::forward` 在什么时机构建 attention mask、在什么时机去除/恢复 padding，以及为什么这些操作的开销「可以忽略」。
5. 理解 TensorRT 融合 MHA kernel 需要的那条「序列长度 offset」长什么样、为什么有 `[B+1]` 和 `[2B+1]` 两种形态。

---

## 2. 前置知识

本讲默认你已经掌握（否则请先读对应讲义）：

- **批处理与变长序列**：一个 batch 里的句子长度往往不一样，但张量必须是规整矩形，所以短句要补零（pad）到 batch 内最长句子的长度。这一讲里我们把「补出来的零」称为 **padding token**，它们对结果是没用的。
- **GEMM 与 token 维（u2-l3）**：BERT 里几乎所有计算都是矩阵乘。一次 GEMM 可以写成 `C[M,N] = A[M,K] · B[K,N]`，其中 `M` 就是 **token 数**。token 越多，GEMM 做的乘加越多——这就是 padding 会拖慢我们的根本原因。
- **Bert forward 的骨架（u4-l1）**：`forward` 接收 `[batch, seq_len, hidden]` 的隐状态和 `[batch]` 的真实长度，逐层跑 attention + FFN。本讲只关注 forward **首尾**的两段「预处理 / 后处理」逻辑。
- **`invokeXxx` 约定（u3-l1）**：每个算子 = 一个 `__global__` 设备 kernel + 一个 host 启动函数；layer 只调 `invokeXxx`，启动异步、立刻返回。
- **`IAllocator::reMalloc`（u2-l2）**：带账本的复用分配器，稳态命中 `REUSE` 时只 memset、不真正分配。

> 一个直觉：Effective FasterTransformer 不是「换了一个模型」，而是「换了一种**数据摆放方式**」。模型本身（权重、kernel）一模一样，只是把 padding token 从输入里挤掉，让 GEMM 的 `M` 变小。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| `docs/bert_guide.md` | 官方对 Effective FasterTransformer 的图文讲解（Fig.1 / Fig.2），是本讲概念的权威出处。 |
| `src/fastertransformer/kernels/bert_preprocess_kernels.h` | 预处理 kernel 的声明：`invokeGetPaddingOffset` / `invokeRemovePadding` / `invokeRebuildPadding` / `invokeBuildEncoderAttentionMask` / `invokeGetTrtPaddingOffset`。 |
| `src/fastertransformer/kernels/bert_preprocess_kernels.cu` | 上述 kernel 的真实 CUDA 实现，是本讲精读的重点。 |
| `src/fastertransformer/models/bert/Bert.cc` | `Bert::forward`，把「构建 mask + 去 padding + 跑层 + 恢复 padding」整套流程封装进一个 `switch(attention_type)`。 |
| `src/fastertransformer/layers/attention_layers/BaseAttentionLayer.h` | `AttentionType` 枚举与 `getAttentionType()`，决定本步 forward 走「去 padding」还是「保留 padding」路径。 |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1** Padding 带来的无效计算与 Effective FasterTransformer 的核心思想
- **4.2** compact / uncompact：`padding_offset` 的构造与去除/恢复 kernel
- **4.3** 融合 mask 构建与 TensorRT MHA 的序列长度 offset
- **4.4** 一切封装进 `forward`：`AttentionType` 分发与时机

### 4.1 Padding 带来的无效计算与 Effective FasterTransformer 的核心思想

#### 4.1.1 概念说明

假设一个 batch 有 `B=2` 个句子，真实长度分别是 `s_1=2`、`s_2=3`，batch 内最长 `S=4`。为了拼成规整张量，我们补零到 `S`：

```
padded 布局（按 token 展平，P 表示 padding）：
[t0, t1, P, P | t2, t3, t4, P]      ← 一共 B*S = 8 个位置，但只有 5 个有效
```

标准 BERT 会**对全部 8 个位置**做 LayerNorm、QKV 投影、FFN……，其中 3 个 padding 位置的计算完全是浪费。`docs/bert_guide.md` 把这一点说得很直白：

> For Effective FasterTransformer, the main idea is removing the padding of sentence to prevent computing the useless tokens. This method can save lots of time when the average sequence length of one batch is much smaller than the maximum sequence length.
> —— [docs/bert_guide.md:57](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/bert_guide.md#L57)

注意两个关键词：**average sequence length（平均长度 s̄）** 远小于 **maximum sequence length（最大长度 S）** 时收益才大。

#### 4.1.2 核心流程

Effective FasterTransformer 的做法是「**先把有效 token 挤到一起（compact），跑完 BERT 再摊回去（uncompact）**」：

```
输入 [B, S, H]  ──invokeRemovePadding──►  紧凑 [M_eff, H]  ──► 跑 N 层 transformer ──►
                                          (M_eff = Σs_i)                              │
                                                                                       ▼
输出 [B, S, H]  ◄──invokeRebuildPadding──  紧凑 [M_eff, H]
```

为什么这样能省？我们用 token 数来量化：

- 标准（保留 padding）：处理的 token 数 \( M_{\text{padded}} = B \cdot S \)
- Effective（去除 padding）：处理的 token 数 \( M_{\text{effective}} = \sum_{i=1}^{B} s_i = B \cdot \bar{s} \)

BERT 里的 GEMM（QKV 投影、attention 输出投影、FFN 两段）的计算量都与 `M`（token 数）成正比，所以：

\[
\frac{\text{Effective 的 GEMM 工作量}}{\text{标准 的 GEMM 工作量}}
= \frac{M_{\text{effective}}}{M_{\text{padded}}}
= \frac{\bar{s}}{S}
\]

举个例子：SQuAD 推理常把 `max_seq_len` 设到 `384`，但大量问题+段落其实只有几十个 token，若平均长度 `s̄ ≈ 32`，则比值 \( \bar{s}/S ≈ 32/384 ≈ 0.083 \)——也就是理论上**线性层最多可少做约 12 倍**的计算。attention 本身是 \( O(L^2) \) 的，padding 时每句都按 \( S^2 \) 算、去 padding 后按 \( s_i^2 \) 算，相对收益更大。

> 这就是 `bert_guide.md` 反复强调「average seq_len 远小于 max seq_len 时收益巨大」的数学根源。

#### 4.1.3 源码精读

`docs/bert_guide.md` 把这套优化分成两个子问题，并把它们的开销都说成「ignorable（可忽略）」：

> First, we need to remove the padding before BERT, and rebuild padding after leaving BERT to keep the shape of result. This is simple and only bring ignorable overhead. The second problem is the computing of multi-head attention. ... Because we can fuse these rebuilding/removing into other kernels, the additional overheads are also ignorable.
> —— [docs/bert_guide.md:57](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/bert_guide.md#L57)

这段话直接对应了我们在 4.2 / 4.3 / 4.4 要读的三块源码：

1. 「remove padding before / rebuild padding after」→ `invokeRemovePadding` / `invokeRebuildPadding`（4.2）。
2. 「multi-head attention 的 padding 问题」→ 两种解法：朴素解法是「进 attention 前重建 padding、出 attention 后再去掉」（Fig.1 第二条流程）；更优解法是直接用 TensorRT 的**融合 MHA kernel**，它天生支持去 padding 输入（4.3）。
3. 这些预处理在 v5.0 被「封装进 `Bert forward` 函数」（4.4）：

> In FasterTransformer v5.0, we refactor the codes, encapsulating the mask building and padding removing into the Bert forward function...
> —— [docs/bert_guide.md:49](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/bert_guide.md#L49)

#### 4.1.4 代码实践

**实践目标**：用真实数字体会「平均长度远小于最大长度」带来的收益差距。

**操作步骤**：

1. 打开 `docs/bert_guide.md` 的性能小节，找到 FP32 / FP16 下「标准 BERT（FT-OP）」与「Effective BERT（EFF-OP）」的耗时对比。
2. 复现它们用的是 `./bin/bert_example <bs> <layers> <seq> <heads> <sph> <dtype> <is_remove_padding>`，注意最后一个参数 `is_remove_padding`：

> To use the Effective FasterTransformer, we only need to set the `<is_remove_padding>` flag to 1
> —— [docs/bert_guide.md:346-353](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/bert_guide.md#L346-L353)

**需要观察的现象 / 预期结果**：guide 中给出的对照（batch=32, seq=32, 12 层，FP32）是：标准 `16.51 ms` → Effective `9.77 ms`。这里 `seq=32` 已经不大，且 example 里长度是随机的，所以收益温和；如果换成 SQuAD 的 `seq=384`、`predict_batch_size=8`，EFF 与标准 的差距会进一步拉大。

> ⚠️ 待本地验证：上述具体毫秒数来自 guide 文本，依赖当时的 V100/T4 机器与版本；在你自己的 GPU 上数字会不同，但「平均长度越小、Effective 越快」的趋势必然成立。

#### 4.1.5 小练习与答案

**练习 1**：batch=4，真实长度 `[8, 16, 4, 12]`，`max_seq_len=32`。去除 padding 后一个 batch 还剩多少 token？工作量降到原来的多少？

**答案**：有效 token \( = 8+16+4+12 = 40 \)；原来 \( = 4 \times 32 = 128 \)。比值 \( 40/128 = 0.3125 \)，即线性层最多只做约 31% 的计算。

**练习 2**：什么情况下 Effective FasterTransformer **几乎没收益**？

**答案**：当 batch 里所有句子长度都接近 `max_seq_len`（即 \( \bar{s} \approx S \)）时，去除 padding 省下的 token 极少，而 compact/uncompact 的额外开销反而成了净负担。所以固定长度场景（例如把所有输入都 pad 成等长）不适合开 `is_remove_padding`。

---

### 4.2 compact / uncompact：`padding_offset` 的构造与去除/恢复 kernel

#### 4.2.1 概念说明

「去除 padding」看似简单——把有效 token 挑出来挤一起——但难点在于：GPU 上做这件事需要一张**映射表**，告诉每个有效 token「你在原 padded 布局里的位置是几号」。这张表就是 `padding_offset`。一旦有了它，去 padding 和恢复 padding 都是一对「按下表搬运」的 elementwise kernel，互相是逆操作。

#### 4.2.2 核心流程

沿用 4.1.1 的例子：`B=2`，`s=[2,3]`，`S=4`，padded 布局按 token 展平为：

```
索引:  0   1   2   3 | 4   5   6   7
内容: t0  t1   P   P | t2  t3  t4   P
```

期望的紧凑布局（5 个有效 token）：`[t0, t1, t2, t3, t4]`。每个紧凑 token `bid` 对应的 padded 索引是：

| 紧凑下标 `bid` | 0 | 1 | 2 | 3 | 4 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 对应 token | t0 | t1 | t2 | t3 | t4 |
| padded 下标 `bid + padding_offset[bid]` | 0 | 1 | 4 | 5 | 6 |
| `padding_offset[bid]` | 0 | 0 | 2 | 2 | 2 |

`padding_offset` 的含义是「**到第 `bid` 个有效 token 为止，前面累计跳过了多少个 padding**」。注意它在每句话内部是常数（同一句话里 padding 都堆在末尾），跨句时阶跃。

构造它的算法本质是一个**前缀和（cumulative sum）**：遍历每句话，累计 `max_seq_len - s_i` 个 padding。

#### 4.2.3 源码精读

构造 `padding_offset` 的 kernel 在 `bert_preprocess_kernels.cu` 顶部，特点是**只启动 1 个 block、1 个线程**（`<<<1,1>>>`），串行跑一个 `O(B)` 的前缀和：

```cpp
// 串行前缀和：遍历每个句子，累计 padding 数，写到 tmp_mask_offset
__global__ void getPaddingOffsetAndCuSeqLensKernel(size_t* h_valid_word_num, int* tmp_mask_offset,
                                                   int* cu_seqlens, const int* sequence_length,
                                                   const int batch_size, const int max_seq_len) {
    int total_seq_len = 0, cum_offset = 0, index = 0;
    for (int i = 0; i < batch_size; i++) {
        const int seq_len = sequence_length[i];
        for (int j = 0; j < seq_len; j++) {
            tmp_mask_offset[index] = cum_offset;   // 这就是 padding_offset[]
            index++;
        }
        cum_offset += max_seq_len - seq_len;       // 这句话贡献的 padding 数
        total_seq_len += seq_len;
    }
    h_valid_word_num[0] = (size_t)total_seq_len;   // 有效 token 总数，回写给 host
}
```
> [bert_preprocess_kernels.cu:24-52](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/bert_preprocess_kernels.cu#L24-L52) —— 串行计算 `padding_offset` 与有效 token 总数。

为什么敢用单线程？因为 `batch_size` 通常很小（个位到几十），`O(B)` 的串行工作比开一堆 block 还省。更有意思的是它的 host 启动函数：

```cpp
void invokeGetPaddingOffsetAndCuSeqLens(...) {
    h_pinned_token_num[0] = 0;
    getPaddingOffsetAndCuSeqLensKernel<<<1, 1, 0, stream>>>(...);   // GPU 写 pinned 内存
    while (((volatile size_t*)h_pinned_token_num)[0] == 0) {};       // CPU 自旋等结果
    h_token_num[0] = h_pinned_token_num[0];
}
```
> [bert_preprocess_kernels.cu:54-69](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/bert_preprocess_kernels.cu#L54-L69) —— `<<<1,1>>>` 启动 + CPU 自旋等待。

这里 `h_pinned_token_num` 是 **pinned（页锁定）主机内存**，GPU 可以直接写、CPU 可以零拷贝读到。CPU 用一个 `while` 自旋等到非零——这是**整个 Effective 流程里唯一的 CPU↔GPU 同步点**。为什么必须同步？因为后面 GEMM 的 `M` 维（=有效 token 数）是**运行期才知道**的，host 必须拿到这个数才能配置 cuBLAS。这是「ignorable overhead」里最大的一笔，靠 `batch_size` 很小把它压住。

> ⚠️ 自旋等待会占满一个 CPU 核；它和 `sync_check_cuda_error` 一样是调试/正确性所需的妥协。

有了 `padding_offset`，去 padding 和恢复 padding 就是两个互逆的搬运 kernel：

```cpp
// 去除 padding：从 padded 的 src 搬到紧凑的 tgt
__global__ void remove_padding(T* tgt, const T* src, const int* padding_offset, const int n) {
    const int bid = blockIdx.x;
    const int src_seq_id = bid + padding_offset[bid];   // 紧凑 bid → padded 下标
    const int tgt_seq_id = bid;
    for (int i = threadIdx.x; i < n; i += blockDim.x)
        tgt[tgt_seq_id * n + i] = src[src_seq_id * n + i];
}
```
> [bert_preprocess_kernels.cu:240-258](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/bert_preprocess_kernels.cu#L240-L258) —— 启动 `<<<token_num, 256>>>`，一个 block 搬一个 token 的整行 hidden。

```cpp
// 恢复 padding：从紧凑的 src 散回 padded 的 dst（remove_padding 的精确逆操作）
__global__ void rebuild_sequence_length_padding(const T* src, T* dst, const int* padding_offset, const int n) {
    const int bid = blockIdx.x;
    const int dst_seq_id = bid + padding_offset[bid];
    const int src_seq_id = bid;
    for (int i = threadIdx.x; i < n; i += blockDim.x)
        dst[dst_seq_id * n + i] = src[src_seq_id * n + i];
}
```
> [bert_preprocess_kernels.cu:185-205](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/bert_preprocess_kernels.cu#L185-L205) —— 把紧凑结果按 `padding_offset` 散回 `[B*S, H]`。

这两个 kernel 的 `<<<grid, 256>>>` 里 `grid = token_num`（有效 token 数），每个 block 负责一整行 `hidden` 维（`n = head_num * size_per_head`），thread 按 `stride blockDim.x` 分摊 hidden 维度的搬运。它们都是纯内存搬运、零计算，所以 guide 才说 overhead ignorable。

> 这两个函数的声明在头文件里是模板：[bert_preprocess_kernels.h:64-70](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/bert_preprocess_kernels.h#L64-L70)，对 `float / half / __nv_bfloat16 / __nv_fp8_e4m3` 都做了显式实例化。

#### 4.2.4 代码实践

**实践目标**：手算一遍 `padding_offset`，确认你对 kernel 的理解。

**操作步骤**：

1. 取 `B=2`，`s=[2,3]`，`S=4`（就是本模块反复用的例子）。
2. 用纸笔模拟 `getPaddingOffsetAndCuSeqLensKernel` 的循环，写出 `tmp_mask_offset`（即 `padding_offset`）和 `h_valid_word_num`。
3. 再用你算出的 `padding_offset` 代入 `remove_padding`，验证紧凑结果确实是 `[t0,t1,t2,t3,t4]`。

**预期结果**：

- `padding_offset = [0, 0, 2, 2, 2]`，`h_valid_word_num = 5`。
- `remove_padding`：`bid=0→src 0`、`bid=1→src 1`、`bid=2→src 4`、`bid=3→src 5`、`bid=4→src 6`，即紧凑缓冲恰为 `[t0,t1,t2,t3,t4]`。✓
- `rebuild_sequence_length_padding` 是它的逆，把 `[t0..t4]` 散回 padded 布局的 0,1,4,5,6 号位。✓

> 这是纯源码阅读 + 手算型实践，不需要 GPU。

#### 4.2.5 小练习与答案

**练习 1**：`B=3`，`s=[3,3,3]`，`S=3`。`padding_offset` 是什么？此时 Effective 还有收益吗？

**答案**：没有 padding，`padding_offset = [0,0,0,0,0,0,0,0,0]`（9 个 0），`h_valid_word_num = 9 = B*S`。compact/uncompact 是纯开销、零收益，所以等长场景不该开 remove padding（与 4.1.5 结论一致）。

**练习 2**：为什么 `invokeGetPaddingOffsetAndCuSeqLens` 用 `<<<1,1>>>` 而不是一个并行 scan kernel？

**答案**：构造 `padding_offset` 的工作量是 `O(B)`（不是 `O(M_eff)`），`batch_size` 通常很小，单线程串行循环的开销远低于启动并行前缀和的调度成本；并且它顺带把 `total_seq_len` 写到 pinned 内存供 CPU 自旋读取，单线程写单点天然无数据竞争。

---

### 4.3 融合 mask 构建与 TensorRT MHA 的序列长度 offset

#### 4.3.1 概念说明

去除 padding 还带来一个副作用：**attention 的 mask 和「序列边界」信息丢了**。padding 布局下，attention 靠一张 `[B, S, S]` 的 0/1 mask 告诉每个位置「哪些 key 是有效的、哪些是 padding 要屏蔽」；compact 布局下，不同句子的 token 挤在一起，必须另外告诉 attention kernel「每句话从第几个 token 开始、到第几个结束」。这就引出两张表：

1. **`attention_mask`**：给朴素（unfused）attention 用，记录每个 key 列是否有效。
2. **`trt_mha_padding_offset`**：给 TensorRT 融合 MHA kernel 用，记录每句话的累积长度边界。

#### 4.3.2 核心流程

**attention mask 的构建**：朴素 attention 里，第 `i` 个 query 对第 `j` 个 key 的注意力分数，要先乘以 `mask[i,j]`（有效位置为 1，padding 位置为 0）再 softmax。FT 用一个 kernel 直接生成这张 `[B, 1, S, S]` 的 0/1 表：对第 `b` 句话，列号 `col < sequence_lengths[b]` 处填 1，否则填 0。

**TRT 序列长度 offset**：TensorRT 的融合 MHA kernel（u3-l2 讲过的 `QKVToContext`）不读 mask，而是读一个**累积长度数组**，它有两种形态：

- **去 padding 时**（compact 输入）：形状 `[B+1]`，内容是有效长度的前缀和 `[0, s_1, s_1+s_2, s_1+s_2+s_3, …]`。它把紧凑缓冲里的 token 切回 B 段。
- **保留 padding 时**（padded 输入）：形状 `[2B+1]`，内容形如 `[0, s_1, S, s_2+S, 2S, 2S+s_3, 3S]`——把每句话的有效段边界和 padded 段边界交替记录。

`docs/bert_guide.md` 用「把 padding 看成独立句子」来解释这种设计：

> When we remove the padding, the shape of the sequence length offset is [B+1] ... Namely, the sequence length offset records the sequence length for each sentence. When we have padding, we view the padding as some independent sentences.
> —— [docs/bert_guide.md:107](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/bert_guide.md#L107)

#### 4.3.3 源码精读

**mask 构建**——`buildEncoderAttentionMaskKernel`，一个 block 负责一句话，把 `[S,S]` 平铺给 256 个 thread 填 0/1：

```cpp
template<typename T>
__global__ void buildEncoderAttentionMaskKernel(T* attention_mask, const int* sequence_lengths, const int max_seq_len) {
    attention_mask += blockIdx.x * max_seq_len * max_seq_len;       // blockIdx.x == 第几句
    const int length = sequence_lengths[blockIdx.x];
    for (int i = threadIdx.x; i < max_seq_len * max_seq_len; i += blockDim.x) {
        int col_id = i % max_seq_len;
        attention_mask[i] = (col_id < length) ? (T)(1.0f) : (T)(0.0f);   // 列号 < 真实长度 → 1
    }
}
```
> [bert_preprocess_kernels.cu:71-97](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/bert_preprocess_kernels.cu#L71-L97) —— 注意它**只看列号** `col_id`（注释里被注释掉的 `row_id` 判断已废弃），生成的是 broadcast 形式的 mask。

启动配置 `<<<batch_size, 256>>>`。它在 `Bert::forward` 里只对**朴素 attention**（`UNFUSED_*`）调用，融合 MHA 路径不需要它。

**TRT offset 构建（去 padding 版）**——单线程前缀和，输出 `[B+1]`：

```cpp
__global__ void getTrtPaddingOffsetKernel(int* trt_mha_padding_offset, const int* sequence_length, const int batch_size) {
    extern __shared__ int tmp_offset[];
    if (threadIdx.x == 0) {
        tmp_offset[0] = 0;
        for (int i = 0; i < batch_size; i++)
            tmp_offset[i + 1] = tmp_offset[i] + sequence_length[i];   // 前缀和 [0, s1, s1+s2, ...]
    }
    __syncthreads();
    for (int i = threadIdx.x; i < batch_size + 1; i += blockDim.x)
        trt_mha_padding_offset[i] = tmp_offset[i];
}
```
> [bert_preprocess_kernels.cu:124-150](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/bert_preprocess_kernels.cu#L124-L150) —— 用 `extern __shared__` 做暂存，单线程算前缀和、其余 thread 协作写回。

**TRT offset 构建（保留 padding 版）**——重载版本，输出 `[2B+1]`：

```cpp
if (threadIdx.x == 0) {
    tmp_offset[0] = 0;
    for (int i = 0; i < request_batch_size; i++) {
        tmp_offset[i * 2 + 1] = tmp_offset[i * 2] + sequence_length[i];   // 有效段结束
        tmp_offset[i * 2 + 2] = request_seq_len * (i + 1);                // padded 段结束
    }
}
```
> [bert_preprocess_kernels.cu:152-183](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/bert_preprocess_kernels.cu#L152-L183) —— 交替记录「有效长度结束点」与「padded 长度结束点」，对应 guide 里的 `[0, s1, S, s2+S, 2S, …]`。

两个重载的声明在 [bert_preprocess_kernels.h:53-62](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/bert_preprocess_kernels.h#L53-L62)：三参版（带 `request_seq_len`）是 padded 路径、两参版是 compact 路径。

#### 4.3.4 代码实践

**实践目标**：手算两种 TRT offset，对照 guide 的公式确认一致。

**操作步骤**：取 `B=2`，`s=[2,3]`，`S=4`。

1. 代入**去 padding 版**（[L124-141](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/bert_preprocess_kernels.cu#L124-L141)），写出 `[B+1]` 数组。
2. 代入**保留 padding 版**（[L152-173](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/bert_preprocess_kernels.cu#L152-L173)），写出 `[2B+1]` 数组。

**预期结果**：

- 去 padding 版：`[0, 2, 5]`（即 `[0, s1, s1+s2]`）。
- 保留 padding 版：`[0, 2, 4, 7, 8]`（即 `[0, s1, S, S+s2, 2S]` = `[0, 0+2, 4, 4+3, 8]`），与 guide 的 `[0, s1, S, s2+S, 2S, …]` 公式完全吻合。

> 纯手算实践，无需 GPU。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `buildEncoderAttentionMaskKernel` 只判断 `col_id < length`，而不像标准 BERT 那样判断 `row < length && col < length`？

**答案**：因为 FT 的 mask 用在 softmax 之前，对 key 列屏蔽即可（padding key 在所有 query 行都不该被注意到）；只屏蔽列相当于把 mask broadcast 到所有 query 行，省掉一半判断、也更省显存。源码注释 `// TODO check this modification` 也印证这是 FT 对标准 mask 的简化。

**练习 2**：融合 MHA 路径（`FUSED_*`）在 `Bert::forward` 里有没有调用 `invokeBuildEncoderAttentionMask`？

**答案**：没有。融合 MHA kernel 自带 padding 处理，只读 `trt_mha_padding_offset`；只有朴素路径（`UNFUSED_*`）才需要显式的 `attention_mask`。这正是 guide 说的「用融合 kernel 后就不必担心 attention 的 padding 问题」。

---

### 4.4 一切封装进 `forward`：`AttentionType` 分发与时机

#### 4.4.1 概念说明

v5.0 之前，去 padding 的逻辑散落在调用方；v5.0 之后，FT 把「构建 mask + 去 padding + 跑层 + 恢复 padding」整套封装进了 `Bert::forward`。对调用方而言，**只要构造 `Bert` 时传入正确的 `attention_type`，forward 内部会自动决定是否 compact**。`attention_type` 由一个四值枚举决定：

```cpp
enum class AttentionType {
    UNFUSED_MHA,         // 朴素 attention + 去 padding（compact）
    UNFUSED_PADDED_MHA,  // 朴素 attention + 保留 padding
    FUSED_MHA,           // TensorRT 融合 MHA + 去 padding（compact）
    FUSED_PADDED_MHA     // TensorRT 融合 MHA + 保留 padding
};
```
> [BaseAttentionLayer.h:33-38](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/BaseAttentionLayer.h#L33-L38)

这四个值是「**注意力实现（FUSED/UNFUSED）**」与「**是否去 padding（带不带 PADDED）**」两个开关的正交组合，与 u4-l1 / u3-l3 一致。本讲关注的是后一个开关。

#### 4.4.2 核心流程

`attention_type` 不是用户手填的，而是由 `getAttentionType()` 根据「数据类型、`size_per_head`、GPU 架构 `sm`、`remove_padding`」自动推导。`bert_example` 把命令行最后一个参数 `is_remove_padding` 直接喂给它：

```cpp
AttentionType attention_type = getAttentionType<T>(size_per_head, getSMVersion(), is_remove_padding, seq_len);
```
> [bert_example.cc:111](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/examples/cpp/bert/bert_example.cc#L111)

进入 `Bert::forward` 后，整套预处理/后处理按 `attention_type` 分四条路径。下面用伪代码概括 forward 的首尾（中段的逐层循环已在 u4-l1 讲过）：

```
forward(input[B,S,H], seq_len[B]):
    对每个 micro-batch (ite):
        switch(attention_type):
          UNFUSED_MHA:          # 朴素 + compact
              build_attention_mask           # 生成 [B,1,S,S]
              get_padding_offset             # 算 padding_offset + token_num（CPU 同步）
              remove_padding  →  bert_in_buffer_[token_num, H]   # compact！
          UNFUSED_PADDED_MHA:   # 朴素 + 不 compact
              build_attention_mask
              token_num = B * S               # 直接用输入指针，不搬
          FUSED_MHA:            # 融合 + compact
              get_padding_offset
              remove_padding    →  bert_in_buffer_
              get_trt_padding_offset(B+1)     # 算 [B+1] 累积 offset
          FUSED_PADDED_MHA:     # 融合 + 不 compact
              get_trt_padding_offset(2B+1)    # 算 [2B+1] padded offset

        for l in layers:        # 所有层都在 compact（或 padded）缓冲上跑
            ...attention + FFN...            # u4-l1 已讲

        # 后处理：恢复 padding（仅 compact 路径需要）
        switch(attention_type):
          UNFUSED_MHA / FUSED_MHA:
              rebuild_padding  bert_out_buffer_ → output[B,S,H]   # uncompact！
          UNFUSED_PADDED_MHA / FUSED_PADDED_MHA:
              （什么都不做，因为输出本来就在 output 里）
```

**时机总结**：mask 构建 + 去 padding 在**逐层循环之前**做一次；恢复 padding 在**逐层循环之后**做一次；中间 N 层 transformer 完全跑在紧凑缓冲上，对 padding 无感知。

#### 4.4.3 源码精读

**首部分发**——`Bert::forward` 的 `switch(attention_type)`，四条分支：

`UNFUSED_MHA`（朴素 + 去 padding）：先建 mask，再算 offset，再 compact：

```cpp
case AttentionType::UNFUSED_MHA: {
    invokeBuildEncoderAttentionMask(attention_mask_, ...);          // [B,1,S,S]
    invokeGetPaddingOffset(h_pinned_token_num_ptr_, &h_token_num,
                           padding_offset_, ...);                   // CPU 同步拿 token_num
    invokeRemovePadding(bert_in_buffer_,                            // compact!
                        input_tensors->at("input_hidden_state").getPtrWithOffset<T>(hidden_offset),
                        padding_offset_, h_token_num, head_num_ * size_per_head_, stream_);
    padding_offset_tensor_ptr = new Tensor(MEMORY_GPU, TYPE_INT32, {h_token_num}, padding_offset_);
    bert_input_ptr  = bert_in_buffer_;                              # 层循环读紧凑缓冲
    bert_output_ptr = bert_out_buffer_;                             # 层循环写紧凑缓冲
    break;
}
```
> [Bert.cc:361-392](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc#L361-L392)

`UNFUSED_PADDED_MHA`（朴素 + 保留 padding）：建 mask 后**直接用输入指针**，`token_num = B*S`，不 compact：

```cpp
case AttentionType::UNFUSED_PADDED_MHA: {
    invokeBuildEncoderAttentionMask(attention_mask_, ...);
    h_token_num     = local_batch_size * request_seq_len;
    bert_input_ptr  = input_tensors->at("input_hidden_state").getPtrWithOffset<T>(hidden_offset);  # 直接用输入
    bert_output_ptr = output_tensors->at("output_hidden_state").getPtrWithOffset<T>(hidden_offset);# 直接写输出
    padding_offset_tensor_ptr = new Tensor(MEMORY_GPU, TYPE_INT32, {0}, nullptr);                   # 占位
    break;
}
```
> [Bert.cc:393-407](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc#L393-L407)

`FUSED_MHA`（融合 + 去 padding）：compact + 算 `[B+1]` 的 TRT offset：

```cpp
case AttentionType::FUSED_MHA: {
    invokeGetPaddingOffset(...);
    invokeRemovePadding(bert_in_buffer_, ...);                      // compact!
    invokeGetTrtPaddingOffset(trt_mha_padding_offset_, sequence_lengths, local_batch_size, stream_);  # 两参 → [B+1]
    padding_offset_tensor_ptr = new Tensor(MEMORY_GPU, TYPE_INT32, {local_batch_size + 1}, trt_mha_padding_offset_);
    bert_input_ptr  = bert_in_buffer_;
    bert_output_ptr = bert_out_buffer_;
    break;
}
```
> [Bert.cc:408-438](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc#L408-L438)

`FUSED_PADDED_MHA`（融合 + 保留 padding）：算 `[2B+1]` 的 TRT offset，不 compact：

```cpp
case AttentionType::FUSED_PADDED_MHA: {
    h_token_num = local_batch_size * request_seq_len;
    invokeGetTrtPaddingOffset(trt_mha_padding_offset_, sequence_lengths, local_batch_size, request_seq_len, stream_);  # 三参 → [2B+1]
    padding_offset_tensor_ptr = new Tensor(MEMORY_GPU, TYPE_INT32, {local_batch_size * 2 + 1}, trt_mha_padding_offset_);
    bert_input_ptr  = input_...;   bert_output_ptr = output_...;     # 直接用输入/输出
    break;
}
```
> [Bert.cc:439-453](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc#L439-L453)

**尾部后处理**——逐层循环之后，只有 compact 路径需要 `invokeRebuildPadding` 把紧凑结果散回 `[B,S,H]`：

```cpp
// post process (rebuild padding)
switch (attention_type) {
    case AttentionType::UNFUSED_MHA:          # fall through
    case AttentionType::FUSED_MHA:
        invokeRebuildPadding(output_tensors->at("output_hidden_state").getPtrWithOffset<T>(hidden_offset),
                             bert_out_buffer_, padding_offset_, h_token_num, head_num_ * size_per_head_, stream_);
        break;
    case AttentionType::UNFUSED_PADDED_MHA:   # 什么都不做
    case AttentionType::FUSED_PADDED_MHA:
        break;
}
```
> [Bert.cc:640-671](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc#L640-L671) —— 注释 `// post process (rebuild padding)` 点明了这一段就是 uncompact。

注意一个关键点：**compact 路径的层循环读写的是 `bert_in_buffer_` / `bert_out_buffer_`**，这两块 buffer 在 `allocateBuffer` 里按**最大**尺寸 `B*S*H` 预留（[Bert.cc:227-231](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc#L227-L231)），实际只用前 `token_num` 行——这是 u3-l5 讲过的「按上限分配、按实际使用」的 buffer 复用约定。

最后，`attention_type` 还可能被运行期**降级**：若 fused 层不可用或当前 `seq_len` 不被融合 kernel 接受（如 `size_per_head` 不是 32/64），forward 会把 `FUSED_*` 自动降级为 `UNFUSED_*`，但**「是否 compact」的属性保持不变**：

```cpp
if (fused_attention_layer_ == nullptr || fused_attention_layer_->isValidSeqLen(request_seq_len) == false) {
    if (attention_type == AttentionType::FUSED_MHA)         attention_type = AttentionType::UNFUSED_MHA;          // 仍去 padding
    else if (attention_type == AttentionType::FUSED_PADDED_MHA) attention_type = AttentionType::UNFUSED_PADDED_MHA; // 仍保留 padding
}
```
> [Bert.cc:341-350](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc#L341-L350) —— FUSED→UNFUSED 降级时，PADDED 后缀不变，即「去 padding」决策不被融合层可用性影响。

而 `getAttentionType` 自身会把「能不能用融合 kernel」和「要不要去 padding」一起算出来，例如 FP16 + 非 causal 的 BERT/ViT 场景：

```cpp
if (!causal_mask) {
    if (... sm/size_per_head 满足融合条件 ...) {
        return remove_padding ? AttentionType::FUSED_MHA : AttentionType::FUSED_PADDED_MHA;
    }
}
...
return remove_padding ? AttentionType::UNFUSED_MHA : AttentionType::UNFUSED_PADDED_MHA;
```
> [BaseAttentionLayer.h:55-95](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/BaseAttentionLayer.h#L55-L95) —— `remove_padding` 是个独立布尔，决定枚举带不带 `PADDED`；融合与否由另一组条件决定。

#### 4.4.4 代码实践

**实践目标**：把本讲的「时机」串起来，验证你对 forward 首/尾两段 `switch` 的理解。

**操作步骤**：

1. 打开 [Bert.cc:360-457](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc#L360-L457)（首部 `switch`）和 [Bert.cc:640-671](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/bert/Bert.cc#L640-L671)（尾部 `switch`）。
2. 对四个 `AttentionType` 各画一行小表，写出：① 是否调用 `invokeBuildEncoderAttentionMask`；② 是否调用 `invokeRemovePadding`；③ 调用的是两参还是三参的 `invokeGetTrtPaddingOffset`；④ 层循环的输入指针是 `bert_in_buffer_` 还是原始 `input`；⑤ 尾部是否调用 `invokeRebuildPadding`。

**预期结果**：

| AttentionType | build_mask | remove_padding | trt_offset | 层循环输入 | rebuild_padding |
| :--- | :---: | :---: | :---: | :---: | :---: |
| `UNFUSED_MHA` | ✅ | ✅ | — | `bert_in_buffer_` | ✅ |
| `UNFUSED_PADDED_MHA` | ✅ | ❌ | — | 原始 `input` | ❌ |
| `FUSED_MHA` | ❌ | ✅ | 两参 `[B+1]` | `bert_in_buffer_` | ✅ |
| `FUSED_PADDED_MHA` | ❌ | ❌ | 三参 `[2B+1]` | 原始 `input` | ❌ |

> 这张表是本讲的「总纲」：compact 与否完全由枚举名里有没有 `PADDED` 决定。

> ⚠️ 待本地验证：若你想跑通，需要先按 u1-l2 编译 FT，再按 guide 的 `./bin/bert_example 32 12 32 12 64 1 1`（最后一个 `1` 开启 remove padding）观察日志。

#### 4.4.5 小练习与答案

**练习 1**：为什么 compact 路径必须额外申请 `bert_in_buffer_` / `bert_out_buffer_`，而 padded 路径可以直接用 `input` / `output`？

**答案**：compact 后 token 数变少、布局也变了（紧凑 vs padded），不能原地覆写调用方的 `input`，所以需要两块中间 buffer 承接「层循环的输入/输出」；padded 路径布局与输入输出完全一致，直接把指针传进去即可，省掉这两块 buffer 和一次搬运。

**练习 2**：如果用户开了 `is_remove_padding=1`，但 GPU 是 FP32、`size_per_head=64`，`getAttentionType` 会返回什么？forward 还会去 padding 吗？

**答案**：FP32 不满足融合条件（融合 MHA 只支持 `half`/`fp8` 等特定类型），`getAttentionType` 走到最后的兜底 `return remove_padding ? AttentionType::UNFUSED_MHA : ...`，返回 `UNFUSED_MHA`。forward **仍然会去 padding**（走 4.2 的 `invokeRemovePadding`/`invokeRebuildPadding` + 朴素 attention + 显式 mask）。可见「去 padding」与「是否融合」是两个独立维度。

---

## 5. 综合实践

把本讲四个模块串成一个任务：**为一段假想输入，画出 Effective FasterTransformer 的完整数据流并标注每一步用的 kernel**。

设定：`B=2`，真实长度 `s=[2,3]`，`S=4`，`hidden=H`，FP16，`size_per_head=64`，Turing/Ampere GPU，`is_remove_padding=1`。

请完成：

1. **确定路径**：根据 `getAttentionType`（[BaseAttentionLayer.h:55-67](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/BaseAttentionLayer.h#L55-L67)）写出本场景的 `AttentionType`，并说明理由。
   - 参考答案：FP16 + `size_per_head=64` + Turing/Ampere + 非 causal → 满足融合条件，`remove_padding=true` → `FUSED_MHA`。
2. **画数据流**：从 `[B,S,H]=[2,4,H]` 输入开始，标出每一步调用的 `invokeXxx`、缓冲形状的变化，直到 `[2,4,H]` 输出。至少要包含：`invokeGetPaddingOffset`（含 CPU 同步取 `token_num=5`）→ `invokeRemovePadding` 得 `[5,H]` → `invokeGetTrtPaddingOffset`（两参）得 `[0,2,5]` → 跑 N 层（在 `[5,H]` 上）→ `invokeRebuildPadding` 散回 `[2,4,H]`。
3. **量化收益**：用 4.1.2 的公式算出本例 compact 后的 token 数与 padded token 数之比，说明 GEMM 工作量降到原来的多少。
   - 参考答案：\( M_{\text{effective}}=5,\ M_{\text{padded}}=8 \)，比值 \( 5/8=0.625 \)，即线性层最多只做约 62.5% 的计算。
4. **指出现实意义**：说明为什么这个 `s=[2,3]`、`S=4` 的小例子收益不明显，而 SQuAD（`S=384`、平均长度小）收益巨大——即回到 \( \bar{s}/S \) 这个比值上。

> 这是纯源码阅读 + 手算 + 画图型综合实践，无需 GPU；如果你想用 NVTX 可视化真实搬运，可结合 u1-l5 设置 `FT_NVTX=ON` 跑 `bert_example`，但具体耗时**待本地验证**。

---

## 6. 本讲小结

- **Padding = 无效计算**：批处理时把短句补到最长，padding 位置的 GEMM 全是浪费；收益正比于 \( \bar{s}/S \)，平均长度越小、最大长度越大，收益越夸张。
- **核心思想是 compact/uncompact**：进 BERT 前用 `invokeRemovePadding` 把有效 token 挤到一起，跑完用 `invokeRebuildPadding` 散回去；两者靠同一张 `padding_offset` 表（一个 `O(B)` 前缀和）互为逆操作。
- **`padding_offset` 的构造**：`getPaddingOffsetAndCuSeqLensKernel` 用单线程串行跑前缀和，并通过 pinned 内存 + CPU 自旋把有效 token 总数回传给 host——这是整个流程里唯一的 CPU↔GPU 同步点，因为后续 GEMM 的 `M` 维运行期才知道。
- **两种注意力实现两种 offset**：朴素 attention 用 `invokeBuildEncoderAttentionMask` 生成 `[B,1,S,S]` 的 0/1 mask；TensorRT 融合 MHA 不用 mask，改用 `invokeGetTrtPaddingOffset` 生成的累积长度数组（compact 时 `[B+1]`、padded 时 `[2B+1]`）。
- **封装进 forward**：v5.0 起，mask 构建 + 去/恢复 padding 全部收进 `Bert::forward` 的 `switch(attention_type)`；去 padding 在层循环**前**做一次、恢复在层循环**后**做一次，中间 N 层对 padding 无感知。
- **两维正交**：`AttentionType` = 「FUSED/UNFUSED」× 「去/不去 padding」；枚举名里带不带 `PADDED` 就决定了是否 compact，与是否融合相互独立，且 fused 不可用时会自动降级为 unfused 但保留 padding 决策。

---

## 7. 下一步学习建议

- **继续编码器主线**：下一讲 u4-l3「视觉编码器 ViT 与 Swin」会复用本讲的 compact/mask 机制——ViT 的 transformer block 与 BERT 几乎相同，但输入是图像 patch。届时你会看到 Effective FT 的思想如何迁移到视觉。
- **深入 attention kernel**：本讲只讲了 mask/offset 怎么生成，没讲融合 MHA kernel **内部**怎么消费这条 offset。建议结合 u3-l2「注意力 kernel：融合多头注意力」重读 `decoder_masked_multihead_attention`，理解 `[B+1]` offset 如何在 kernel 内部把紧凑缓冲切成多段。
- **量化与 Effective 的配合**：`bert_guide.md` 指出 INT8 要求序列长度是 32 的倍数，而 Effective FT 恰好能规避这一约束（compact 后没有 padding）——这是 u9-l1「INT8 量化推理」会展开的话题，读那篇时记得回看本讲的 compact 流程。
- **源码延伸阅读**：`bert_preprocess_kernels.cu` 末尾还有 FP8 专用的 `invokeQuantizeMatrixRebuildPadding`（[L416-463](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/bert_preprocess_kernels.cu#L416-L463)），它把「反量化 + rebuild padding」融进一个 kernel，是本讲 uncompact 思想在低精度下的进一步融合，可作为 u9-l3 FP8 讲义的预习材料。
