# Split-KV 缓冲与 Flash-Decoding 思想

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚**解码（decode）阶段为什么需要把长 KV 序列切成多段（split）**，以及它带来的并行收益。
- 看懂 FlashMLA 为 split-KV 准备的两块 float32 **accumulate 缓冲**（`lse_accum` / `o_accum`），并能解释它们的形状为什么由 `total_num_splits = batch_size + num_sm_parts` 决定。
- 掌握**局部 lse 与全局 lse 的关系**：每个 split 产出一个「只覆盖自己那一段 KV」的 log-sum-exp，combine kernel 用 rescale 公式把它们合并成一个全局 lse 和最终输出。
- 用纯 PyTorch 复现一遍 split-KV + logsumexp 合并，并验证它和整体 softmax 数值等价。

本讲是 u3-l4（dense decode 接口）的延续：u3-l4 讲了「sched_meta → 主 kernel → combine」三段式的编排骨架，本讲专门拆解其中**主 kernel 与 combine 之间的数据契约**——也就是 split-KV 缓冲。

## 2. 前置知识

阅读本讲前，最好已经了解以下概念（前几讲已建立）：

- **MLA 解码的形状约束**：`head_dim_k = 576`、`head_dim_v = 512`，KV 同源（V 只取 K 的前 512 维）。
- **Paged KV cache**：KV 不是一整条连续张量，而是由 `block_table` 索引一个个 `page_block_size = 64` 的块拼出来的。
- **online softmax / Flash Attention**：不分两步（先算 softmax 权重再加权），而是一边扫 KV 一边维护 `(m, l, O)` 三元组（最大值、指数和、未归一化输出），最后 `O / l` 得到结果。这点是理解 split 合并的前提。
- **log-sum-exp（lse）**：\(\mathrm{lse}(x) = \log\sum_i e^{x_i}\)，它能把「两段分别算好的指数和」无损拼成「整体的指数和」，是 split 合并的数学基石。
- **base-2 与 base-e**：硬件上用 `exp2f` / `log2f`（配合 `scale * log2(e)` 把自然底搬到 2 底）更快；对外返回的 lse 仍是自然底 e。本讲的 PyTorch 演示用自然底 e（更直观），讲源码时会标注 base-2 的位置。

> 一句话回顾 lse 的合并性质：若把 logits 分成两段 \(A\)、\(B\)，则
> \[ \mathrm{lse}(A \cup B) = \log\!\big(e^{\mathrm{lse}(A)} + e^{\mathrm{lse}(B)}\big). \]
> split-KV 合并本质就是在反复用这条公式。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [csrc/params.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/params.h) | 定义 `DecodingSchedMeta`、`DenseAttnDecodeParams`（含 split-KV 缓冲指针）、`CombineParams`——主 kernel 与 combine 之间的数据契约。 |
| [csrc/api/dense_decode.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h) | dense decode 接口函数：算 `num_sm_parts`、分配 accumulate 缓冲、依次启动 sched_meta kernel → 主 kernel → combine kernel。 |
| [csrc/sm90/decode/dense/splitkv_mla.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.h) | 主 kernel 的启动声明 `run_flash_splitkv_mla_kernel`。 |
| [csrc/sm90/decode/dense/splitkv_mla.cuh](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh) | 主 kernel 实现：根据 `is_no_split` 决定「直写最终 out/lse」还是「写 accumulate 缓冲」。 |
| [csrc/smxx/decode/combine/combine.cu](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/combine/combine.cu) | combine kernel：跨 split 归并 `lse_accum`/`o_accum`，单 split 早退。 |
| [csrc/smxx/decode/get_decoding_sched_meta/get_decoding_sched_meta.cu](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/get_decoding_sched_meta/get_decoding_sched_meta.cu) | tile scheduler：把 batch 的请求/块均衡切给 `num_sm_parts` 个 SM part，产出 `num_splits` 前缀和。 |

> 提示：combine 与 sched_meta 都在 `csrc/smxx/` 下，意味着它们是 **SM90 / SM100 两架构共用**的解码辅助 kernel；本讲虽以 SM90 dense decode 为例，但机制对所有 decode 路径通用。

## 4. 核心概念与源码讲解

### 4.1 split-KV 动机

#### 4.1.1 概念说明

解码阶段有一个天然的不平衡：**query 极少（通常 \(s_q = 1\)），KV 却很长**（上下文动辄几千到上万 token）。一次 decode 调用要算的 GEMM 是「小 Q × 大 K」和「小 P × 大 V」，单条序列的计算量不大，但要把整条长 KV 从显存搬进来。

如果一条长 KV 序列只交给**一个** SM partition 顺序处理，会出现两个问题：

1. **并行度浪费**：一张 H800 有 80 个 SM，\(s_q = 1\) 时单条序列只够喂饱极少数 SM，绝大多数 SM 闲置。
2. **尾延迟高**：长 KV 必须串行扫完，decode 的访存延迟无法被并行掩盖。

**split-KV（也叫 Flash-Decoding）**的思路就是：把同一条长 KV 序列**横向切成多段**，每段交给一个独立的 SM partition 并行计算，各自得到一个「只覆盖自己那一段」的局部结果，最后用一个轻量的 combine kernel 把所有局部结果合并成全局结果。

> 命名澄清：「Flash-Decoding」是社区对这种「切 KV 维度并行 + 合并」技术的称呼；FlashMLA 代码里用 `split` / `splitkv` / `sm_part`（SM partition）这些词描述同一件事。本讲统一称 **split-KV**。

#### 4.1.2 核心流程

把一条序列的 KV 切成多段后，整体流程是「**三段式**」（u3-l4 已提过，这里聚焦数据流）：

```
                 ┌──────────── get_decoding_sched_meta ────────────┐
  batch 的请求   │  把所有请求的块均衡切给 num_sm_parts 个 SM part   │
  + cache_seqlens├──────────────────────────────────────────────────┤
                 │  产出 tile_scheduler_metadata[num_sm_parts]      │
                 │       + num_splits[batch_size+1] (前缀和)         │
                 └──────────────────────┬───────────────────────────┘
                                        │
                ┌──────── flash_splitkv_mla kernel ─────────┐
                │ 每个 SM part 处理自己分到的一段 KV          │
                │ → 算出该段的 (局部 m, 局部 l, 局部 O)       │
                │   · 若该请求只被 1 个 part 处理：直写 out/lse│
                │   · 若被多个 part 切分：写 lse_accum/o_accum│
                └──────────────────────┬─────────────────────┘
                                       │
                ┌──────── combine kernel ─────────┐
                │ 按 num_splits 取出该 batch 的所有 split │
                │ · my_num_splits==1：直接 return（主 kernel 已写好）│
                │ · 否则：rescale 合并 → 写最终 out/lse │
                └──────────────────────────────────┘
```

关键点：**是否真的发生 split，取决于 tile scheduler 的分配结果**。一条短序列可能独占一个 part（无 split，直写最终结果）；一条超长序列会被切给多个 part（有 split，需要 combine 合并）。主 kernel 和 combine 都要**同时兼容这两种情况**，这正是下一节 accumulate 缓冲要解决的事。

#### 4.1.3 源码精读

**并行度 `num_sm_parts` 的来源**——它决定了「把 KV 切成多少份」的上界：

[csrc/api/dense_decode.h:78](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L78) 这一行计算 SM partition 数：

```cpp
int num_sm_parts = std::max(arch.num_sms / num_heads_k / cutlass::ceil_div(seqlen_q_ori*num_heads_q/num_heads_k, 64), 1);
```

含义：先把 SM 按 KV 头数 `num_heads_k` 分组（每个 KV 头占一组 SM），再除以「每 KV 头要处理的 query 块数（按 64 对齐）」。`std::max(..., 1)` 保证至少 1 个 part。`num_sm_parts` 越大，单条长 KV 能被切得越细、并行度越高。

**tile scheduler 的负载均衡**：[csrc/smxx/decode/get_decoding_sched_meta/get_decoding_sched_meta.cu:62](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/get_decoding_sched_meta/get_decoding_sched_meta.cu#L62) 先算每个 part 应承担的工作量：

```cpp
int payload = cutlass::ceil_div(total_num_blocks, num_sm_parts) + fixed_overhead_num_blocks;
```

随后 [L64-L99](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/get_decoding_sched_meta/get_decoding_sched_meta.cu#L64-L99) 按这个 `payload` 依次把请求/块「装进」每个 part：能装下整个请求就装下，装不下就把请求**切断**（产生一个 split），剩下的留给下一个 part。每次切断都会 `++now_n_split_idx` 并累加进 `cum_num_splits`，最终写入前缀和数组 `num_splits_ptr`。

> 本讲只需理解「scheduler 决定了哪些请求被切、切了几段」；split 的精确切分算法在 u4-l3 详讲。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认 `num_sm_parts` 如何随配置变化，建立「长 KV → 更多 split」的直觉。

**步骤**：

1. 打开 [csrc/api/dense_decode.h:78](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L78)。
2. 假设 H800（`num_sms = 80`）、`num_heads_k = 1`（典型 MLA decode）、`seqlen_q_ori = 1`、`num_heads_q = 128`：
   - `ceil_div(1*128/1, 64) = ceil_div(128,64) = 2`
   - `num_sm_parts = max(80 / 1 / 2, 1) = 40`
3. 把 `seqlen_q_ori` 想象成 1（DeepSeek 关闭 MTP 的真实解码），回答：`num_sm_parts` 主要是被 `num_sms` 撑大的，这就是长 KV 能被切成几十段并行的根。

**预期结果**：你应当能口算出典型 decode 配置下 `num_sm_parts` 在几十的量级，从而理解一条长 KV 会被切给几十个 part 并行处理。

#### 4.1.5 小练习与答案

**练习 1**：为什么 prefill 路径（u6/u7）没有 combine kernel，只有 decode 路径有？
> **答**：prefill 阶段 \(s_q\) 很大，单条序列的 query 足够多，靠切 query 维度（\(M\) 维）就能填满所有 SM，不需要再切 KV；而 decode 阶段 \(s_q\) 极小，必须切 KV 才能拿到并行度，切了 KV 就必须合并，所以只有 decode 才需要 combine。

**练习 2**：`num_sm_parts` 最小可以是多少？此时 split-KV 退化成什么？
> **答**：最小为 1（`std::max(..., 1)`）。此时每个请求独占唯一一个 part，`is_no_split` 恒为真，主 kernel 直写最终结果，combine 全程早退——split-KV 退化成「不切分」的普通 decode。

---

### 4.2 accumulate 缓冲

#### 4.2.1 概念说明

既然一条序列可能被切成多段，每段都会产出一个**局部结果**，我们就需要一块地方**先把所有局部结果存下来**，再由 combine 合并。这块「暂存局部结果」的显存就是 **accumulate 缓冲**，它有两块：

- `o_accum`：存每个 split 的（按段内归一化的）输出 \(O\)，形状 `[total_num_splits, num_heads, q_seq_per_hk, head_dim_v]`，dtype 是 **float32**（合并需要高精度，不能直接用 bf16）。
- `lse_accum`：存每个 split 的局部 log-sum-exp，形状 `[total_num_splits, num_heads, q_seq_per_hk]`，float32。

> 为什么是 float32？合并时要算 `exp(lse_a - lse_b)` 这种**差值的指数**，bf16 的精度会让相邻 split 的相对权重严重失真。主 kernel 内部用 bf16/fp16 算 GEMM，但**写 accumulate 缓冲时升精度到 float32**。

注意：accumulate 缓冲**只对「被切分的请求」使用**。没被切分的请求（单 split）主 kernel 直接把最终结果写进 `out` / `lse`，根本不碰 accumulate 缓冲。这就是「双写」设计。

#### 4.2.2 核心流程

accumulate 缓冲的生命周期：

```
接口函数 dense_attn_decode_interface
  │
  │  1. 算 total_num_splits = batch_size + num_sm_parts  （安全上界）
  │  2. 分配 lse_accum[total_num_splits, ...]、o_accum[total_num_splits, ...]
  │
  ├──► 主 kernel：被切分的 split 写 lse_accum / o_accum（base-2 lse）
  │                     单 split 请求直写 out / lse（base-e lse）
  │
  └──► combine kernel：按 num_splits[batch] 区间读 lse_accum / o_accum
                        合并 → 写最终 out / lse
```

这里有个核心问题：**`total_num_splits` 凭什么是 `batch_size + num_sm_parts`？** 它是所有 split 总数的**安全上界**，推导如下：

- 每个请求至少产生 1 个 split（就算不切分也有 1 段）→ 贡献 `batch_size` 个。
- 额外的 split 只来自「请求被某条 part 边界切断」。一共有 `num_sm_parts` 个 part，它们之间只有 `num_sm_parts - 1` 条内部边界；每条内部边界最多切断 1 个请求、多产生 1 个 split。
- 所以 split 总数 \( \le \) `batch_size + (num_sm_parts - 1)` \(<\) `batch_size + num_sm_parts`。

因此 `total_num_splits = batch_size + num_sm_parts` 足以装下所有 split，多出来的槽位只是浪费一点点显存（float32），换来了**在 scheduler 运行前就能确定缓冲大小**的便利。

#### 4.2.3 源码精读

**缓冲分配**在接口函数里：

[csrc/api/dense_decode.h:164-171](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L164-L171) 计算上界并分配两块 float32 缓冲：

```cpp
const int total_num_splits = batch_size + params.num_sm_parts;
at::Tensor lse_accum = torch::empty({total_num_splits, num_heads, q_seq_per_hk}, opts.dtype(at::kFloat));
at::Tensor out_accum = torch::empty({total_num_splits, num_heads, q_seq_per_hk, head_size_v}, opts.dtype(at::kFloat));
...
params.total_num_splits = total_num_splits;
params.softmax_lseaccum_ptr = lse_accum.data_ptr<float>();
params.oaccum_ptr = out_accum.data_ptr<float>();
```

**参数结构里的字段**：[csrc/params.h:52-58](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/params.h#L52-L58) 把缓冲指针和调度信息打包进 `DenseAttnDecodeParams`：

```cpp
DecodingSchedMeta *__restrict__ tile_scheduler_metadata_ptr;
int num_sm_parts;
int *__restrict__ num_splits_ptr;          // [batch_size+1]，前缀和

int total_num_splits;
float *__restrict__ softmax_lseaccum_ptr;  // [total_num_splits, h_k, q_seq_per_hk]
float *__restrict__ oaccum_ptr;            // [total_num_splits, h_k, q_seq_per_hk, d_v]
```

**`num_splits_ptr` 是前缀和**：长度 `batch_size+1`，`num_splits_ptr[b]` 是第 `b` 个请求的**起始 split 下标**，`num_splits_ptr[b+1]` 是**结束下标**（左闭右开）。这样 combine 只需读两个相邻元素就能知道「这个请求占用了哪几个 split 槽位」。它由 sched_meta kernel 写入（[get_decoding_sched_meta.cu:104-106](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/get_decoding_sched_meta/get_decoding_sched_meta.cu#L104-L106)）。

**主 kernel 的「双写」**：[csrc/sm90/decode/dense/splitkv_mla.cuh:1230-1262](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L1230-L1262) 根据 `is_no_split` 二选一：

```cpp
if (is_no_split) {
    store_o<T, true>(rO, gO, rL, ...);                 // 直写最终 out（bf16）
    gSoftmaxLse(i) = ... logf(cur_L) + sM(i)/M_LOG2E;  // base-e lse
} else {
    int split_idx = params.num_splits_ptr[batch_idx] + n_split_idx;  // 该 split 在缓冲里的下标
    ... gOAccum / gSoftmaxLseAccum ...                  // 写 accumulate 缓冲
    store_o<T, false>(rO, gOAccum, rL, ...);            // float32
    gSoftmaxLseAccum(i) = ... log2f(cur_L) + sM(i);     // base-2 lse
}
```

> 注意两种 lse 的底不同：直写路径用 `logf`（自然底 e）直接给最终值；accumulate 路径用 `log2f`（base-2），因为后续 combine 全程在 base-2 下做 `exp2f`/`log2f` 合并，更快。两种底最终都统一成 base-e 返回给调用方（见 4.3）。

**`split_idx` 的寻址**：被切分的请求，其第 `n_split_idx` 段在缓冲里的位置是 `num_splits_ptr[batch_idx] + n_split_idx`——正是靠前缀和把「batch 内的第几段」映射成「全局缓冲里的第几槽」。

#### 4.2.4 代码实践（手算型）

**目标**：验证 `total_num_splits = batch_size + num_sm_parts` 确实是 split 总数的安全上界。

**步骤**：

1. 设 `batch_size = 3`、`num_sm_parts = 2`，则 `total_num_splits = 5`。
2. 假设 scheduler 把 3 个请求切给 2 个 part 的结果如下（每行是一个 part 的工作）：
   - part 0：请求 0 全部 + 请求 1 的一半
   - part 1：请求 1 的另一半 + 请求 2 全部
3. 数一数 split 总数：请求 0 → 1 段，请求 1 → 2 段（被切断），请求 2 → 1 段，合计 **4 段**。
4. 前缀和 `num_splits = [0, 1, 3, 4]`（长度 `batch_size+1 = 4`）。

**预期结果**：实际 split 数 4 \( \le \) 上界 5，缓冲够用；且 `num_splits[b+1] - num_splits[b]` 恰好给出每个请求的段数（1, 2, 1）。

**待本地验证**：以上是按 scheduler 算法手推的结果；若要严格核对，可在真实环境构造该 batch 跑一次，打印 `num_splits` 张量对比。

#### 4.2.5 小练习与答案

**练习 1**：为什么 accumulate 缓冲用 float32，而最终 `out` 用 bf16？
> **答**：combine 要对多个 split 做 `exp(差值)` 的加权求和，差值对精度极敏感，必须 float32；最终合并完的单个结果精度足够，且为了和后续算子（如 RMSNorm/MLP）对齐，存回 bf16 节省显存与带宽。

**练习 2**：若 `num_sm_parts = 1`，`lse_accum` / `o_accum` 还会被写入吗？
> **答**：不会。`num_sm_parts = 1` 时所有请求都独占唯一 part，`is_no_split` 恒真，主 kernel 全走直写路径，accumulate 缓冲虽被分配但全程不被读写（combine 也会因 `my_num_splits == 1` 早退）。

---

### 4.3 局部 / 全局 lse 的关系

#### 4.3.1 概念说明

split-KV 最核心的数学问题是：**每个 split 只见过自己那一段 KV，怎么把它们的结果拼成「见过整条 KV」的结果？** 答案就是用 lse（log-sum-exp）做 rescale 合并。

设整条 KV 的 logits 为 \(s\)，被切成两段 \(A\)、\(B\)。每段各自跑了 online softmax，得到：

- 段内最大值：\(m_A = \max_{j\in A} s_j\)，\(m_B = \max_{j\in B} s_j\)
- 段内指数和：\(l_A = \sum_{j\in A} e^{s_j - m_A}\)，\(l_B = \sum_{j\in B} e^{s_j - m_B}\)
- 段内未归一化输出：\(O_A = \sum_{j\in A} e^{s_j - m_A} v_j\)，\(O_B = \sum_{j\in B} e^{s_j - m_B} v_j\)

注意每段都减了自己的最大值 \(m_A\) / \(m_B\)，所以两段的 \(O\) 「不在同一个尺度上」，不能直接相加。

#### 4.3.2 核心流程

**标准合并（Method A，FlashMLA 的 PyTorch 参考实现采用这种）**：

先求全局最大 \(m = \max(m_A, m_B)\)，再把两段 rescale 到这个统一尺度：

\[ l = e^{m_A - m}\,l_A + e^{m_B - m}\,l_B \]
\[ O = e^{m_A - m}\,O_A + e^{m_B - m}\,O_B \]

最终结果就是：

\[ \text{out} = O\,/\,l, \qquad \text{lse}_{\text{global}} = \log l + m \]

> 直觉：\(e^{m_A - m}\) 这个系数把「按 \(m_A\) 归一化的段」平移到「按全局 \(m\) 归一化」。合并后的 \(O/l\) 就是正确的全局 softmax 输出。

**FlashMLA combine kernel 的等价写法（Method B，全程 base-2）**：

FlashMLA 实际存的是 **base-2 的 lse** 和 **段内已归一化的 \(O\)**，合并公式等价变形为：

\[ \text{lse}_s = \log_2 l_s + m_s \quad(\text{每段}) \]
\[ g = \log_2\!\Big(\sum_s 2^{\,\text{lse}_s - M}\Big) + M,\quad M = \max_s \text{lse}_s \]
\[ \text{out} = \sum_s 2^{\,\text{lse}_s - g}\,O_s^{\text{norm}} \]

其中 \(O_s^{\text{norm}} = O_s / l_s\) 是段内已归一化的输出（见 4.2.3：主 kernel 在 accumulate 路径里就除了 `rL`）。两种写法**代数等价**——把 \(2^{\text{lse}_s} = l_s \cdot 2^{m_s}\) 代入 Method B，化简后正好得到 Method A 的 \(O/l\)。

#### 4.3.3 源码精读

combine kernel 的 grid 是 `[batch_size*s_q, 1, h_q/BLOCK_SIZE_M]`（[combine.cu:21](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/combine/combine.cu#L21)），即**每个 CTA 负责一个 (batch, q 行) 的若干头**。

**第 1 步：取出本请求的 split 区间，单 split 早退**——[combine.cu:36-41](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/combine/combine.cu#L36-L41)：

```cpp
const int start_split_idx = __ldg(params.num_splits_ptr + batch_idx);
const int end_split_idx   = __ldg(params.num_splits_ptr + batch_idx + 1);
const int my_num_splits = end_split_idx - start_split_idx;
if (my_num_splits == 1) {
    return;   // 主 kernel 已直写最终结果，无需合并
}
```

这正是「双写」与「combine 早退」的呼应：单 split 的请求，主 kernel 走 `is_no_split` 直写，combine 直接 return。

**第 2 步：跨 split 求 lse 的合并（Method B）**——[combine.cu:81-100](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/combine/combine.cu#L81-L100)：

```cpp
float max_lse = -INFINITY;
for (...) max_lse = max(max_lse, local_lse[i]);     // M = max_s lse_s（warp 内归约）
...
float sum_lse = 0;
for (...) sum_lse = sum_lse + exp2f(local_lse[i] - max_lse);   // Σ 2^(lse_s - M)
...
float global_lse = log2f(sum_lse) + max_lse;        // g = log2(Σ...) + M
if (lane_idx == 0)
    gLse(warp_idx) = global_lse / (float)M_LOG2E;   // base-2 → base-e，写最终 lse
```

**第 3 步：用 rescale 系数合并各段输出**——[combine.cu:114-145](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/combine/combine.cu#L114-L145)：

```cpp
for (...) {
    smem_buf[warp_idx][split] = exp2f(local_lse[i] - global_lse);  // 2^(lse_s - g)
}
...
for (int split = 0; split < my_num_splits; ++split) {
    float lse_scale = smem_buf[warp_idx][split];
    for (int i = ...) {
        result[i] += lse_scale * datas[i];          // Σ 2^(lse_s - g) · O_s
        ...                                          // 边累加边预取下一段 o_accum
    }
}
```

最后把 `result` 转成 bf16 写进最终 `out`。整段对应公式 \(\text{out} = \sum_s 2^{\text{lse}_s - g} O_s^{\text{norm}}\)。

> 一个精度细节：`global_lse` 求出来后还要除以 `M_LOG2E`（\(\log_2 e\)）才写进 `gLse`，把 base-2 转回 base-e，保证对外返回的 lse 与「不切分」的直写路径（也用 base-e）一致——两条路径对调用方完全等价。

**`MAX_SPLITS` 的编译期分派**：combine kernel 的 `MAX_SPLITS` 是模板常量，启动时按 `num_sm_parts` 选档（[combine.cu:165-185](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/combine/combine.cu#L165-L185) 的 `MLA_NUM_SPLITS_SWITCH`，挡位 32/64/96/128/160），这样 smem 里 `smem_buf[BLOCK_SIZE_M][MAX_SPLITS]` 才不会无谓开大。

#### 4.3.4 代码实践（可运行 · 纯 PyTorch）

**目标**：用纯 PyTorch 实现 split-KV + logsumexp 合并，验证它和整体 softmax 数值等价。**无需 GPU，CPU 即可运行。**

**操作步骤**：把下面脚本存成 `splitkv_demo.py` 并运行（`python splitkv_demo.py`）。

```python
# 示例代码：split-KV 合并的纯 PyTorch 复现（演示用，非项目源码）
import torch
torch.manual_seed(0)

d   = 64      # 演示用小维度；FlashMLA 真实是 head_dim_k=576 / head_dim_v=512
s_q = 1       # decode 场景：query 极少
s_k = 4096    # 长 KV
N   = 8       # split 数（类比 num_sm_parts 切出的段数）

q = torch.randn(s_q, d)
k = torch.randn(s_k, d)
v = torch.randn(s_k, d)
scale = 1.0 / d ** 0.5

# ===== 1. 整体 softmax 作为参考 =====
scores = (q @ k.t()) * scale              # [s_q, s_k]
o_ref  = torch.softmax(scores, -1) @ v    # [s_q, d]
lse_ref = torch.logsumexp(scores, -1)     # base-e, [s_q]

# ===== 2. split-KV：每段做 online softmax，只产出 (m_s, l_s, O_s) =====
m_parts, l_parts, O_parts = [], [], []
for idx in torch.chunk(torch.arange(s_k), N):
    s = (q @ k[idx].t()) * scale          # [s_q, len(idx)]
    m = s.amax(-1)                        # 段内最大 logit  m_s
    e = torch.exp(s - m)                  # 减段内最大值后取 exp
    l_parts.append(e.sum(-1))             # 段内指数和      l_s
    O_parts.append(e @ v[idx])            # 段内未归一化输出 O_s
    m_parts.append(m)

# ===== 3. 用 logsumexp 思路合并所有段（Method A）=====
m_stack = torch.stack(m_parts)            # [N, s_q]
l_stack = torch.stack(l_parts)            # [N, s_q]
O_stack = torch.stack(O_parts)            # [N, s_q, d]

m_g = m_stack.amax(0)                                      # 全局最大 logit
alpha = torch.exp(m_stack - m_g).unsqueeze(-1)             # e^(m_s - m_g), [N,s_q,1]
O_g = (alpha * O_stack).sum(0)                             # 全局未归一化输出
l_g = (torch.exp(m_stack - m_g) * l_stack).sum(0)          # 全局分母

o_split   = O_g / l_g                     # 归一化 → 最终输出
lse_split = torch.log(l_g) + m_g          # base-e 全局 lse

print("o   max abs diff:", (o_split - o_ref).abs().max().item())
print("lse max abs diff:", (lse_split - lse_ref).abs().max().item())
```

**需要观察的现象**：

- `o` 的最大绝对误差应在 \(10^{-6}\) 量级（float32 数值噪声）。
- `lse` 的最大绝对误差应在 \(10^{-6}\) 量级。
- 改大 `N`（切更多段）或改大 `s_k`（更长 KV），误差量级不应显著变化——这正是 split-KV「无损并行」的体现。

**预期结果**：两条 `max abs diff` 都极小（接近浮点精度上限），证明「切 KV → 各段 online softmax → logsumexp 合并」与「整体一次 softmax」数学等价。

> 进阶：把第 3 步改成 Method B（存 `lse_s = torch.log2(l_s) + m_s / torch.log2(torch.tensor(2.0))`... 用 base-2），结果应完全一致——这就对应 combine.cu 的实现。

#### 4.3.5 小练习与答案

**练习 1**：如果合并时不减全局最大值 \(m\)（即直接算 \(\sum_s e^{\text{lse}_s}\)），会出什么问题？
> **答**：会数值溢出。\(e^{\text{lse}_s}\) 本身可能极大（lse 是 logits 的 log-sum-exp，几十量级），几个加起来直接 inf。减去 \(M=\max_s \text{lse}_s\) 后最大项变成 \(e^0=1\)，其余 \(<1\)，既不溢出也不下溢。这就是 lse 合并总是「减最大值」的原因。

**练习 2**：combine kernel 里 `my_num_splits == 1` 时直接 `return`，为什么这样写仍正确？
> **答**：`my_num_splits == 1` 说明该请求只被 1 个 part 处理，主 kernel 对它走了 `is_no_split` 直写路径，已经把正确的最终 `out`（base-e lse、归一化输出）写进了 `out` / `lse`。combine 没有任何残留的局部结果要合并，直接 return 即可——若再写一次反而会覆盖正确结果。

**练习 3**：直写路径写 base-e 的 lse，accumulate 路径写 base-2 的 lse，两者最终怎么统一？
> **答**：combine kernel 在 [combine.cu:100](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/combine/combine.cu#L100) 把算出的 base-2 `global_lse` 除以 `M_LOG2E`（\(\log_2 e\)）转成 base-e 再写入 `gLse`。于是无论走哪条路径，对外返回的 `lse` 都是 base-e，调用方无需感知内部是否发生了 split。

## 5. 综合实践

把本讲三个模块串起来：**用 PyTorch 模拟一次完整的 split-KV decode 数据流，并对照最终接口的形状约定。**

任务：

1. 取 `batch_size = 4`、`s_q = 1`、`s_k = 8192`、`d = 64`、`num_heads_k = 1`、`num_heads_q = 128`、`num_sm_parts = 40`（模拟 H800）。
2. **模拟 scheduler**：自己设计一种简单切分（例如把每个请求的 `s_k` 均分给它分到的 part 数），生成一个前缀和数组 `num_splits[batch_size+1]`，使每个请求恰好被切成若干段。
3. **模拟主 kernel**：对每个 split 跑一次 4.3.4 里的段内 online softmax，把 `(lse_s, O_s_norm)` 存进你预先按 `total_num_splits = batch_size + num_sm_parts` 开好的 Python 列表（accumulate 缓冲的模拟）。
4. **模拟 combine**：对每个请求，按 `num_splits` 区间取出它的所有 split，用 4.3 的 rescale 公式合并成最终 `out` / `lse`。
5. **校验**：把每个请求的合并结果与「该请求整体一次 softmax」对比，打印最大误差。

验收标准：

- 所有请求的合并误差都在 float32 噪声量级。
- 你能指出：哪些请求是「单 split」（在你的模拟里 combine 应跳过），哪些是「多 split」（必须合并）。
- 你能解释 `total_num_splits` 缓冲里有哪些槽位是「未被使用」的（安全上界带来的冗余）。

> 这个练习把 4.1 的动机（为何切）、4.2 的缓冲（accumulate + 前缀和 + 上界）、4.3 的合并（rescale + lse）三件事一次性串起来，做完你就掌握了 split-KV 的完整数据契约。

## 6. 本讲小结

- **split-KV 是为了 decode 并行**：\(s_q\) 极小时靠切 KV 维度才能填满 SM；切了就必须合并，于是有了「主 kernel → accumulate 缓冲 → combine」三段数据流。
- **两块 float32 accumulate 缓冲**（`lse_accum` / `o_accum`）暂存每个 split 的局部结果，大小由安全上界 `total_num_splits = batch_size + num_sm_parts` 决定。
- **`num_splits[batch_size+1]` 是前缀和**，给 combine 提供「每个请求占哪几个 split 槽位」的左闭右开区间。
- **主 kernel 双写**：单 split 请求直写最终 `out`/`lse`（base-e），多 split 请求写 accumulate 缓冲（base-2、段内已归一化的 \(O\)）。
- **局部 → 全局 lse 靠 rescale 合并**：\(\text{out}=\sum_s 2^{\text{lse}_s-g} O_s^{\text{norm}}\)，与标准 online softmax 的 \(O/l\) 代数等价；单 split 由 combine 早退处理，两条路径对调用方一致。
- accumulate 用 float32、最终 out 用 bf16，是合并精度与存储带宽之间的取舍。

## 7. 下一步学习建议

- **u4-l2（combine kernel）**：本讲只点了 combine 的合并主流程，下一讲会逐段精读 combine.cu 的 grid 划分、`MAX_SPLITS` 分派、PDL（Programmatic Dependent Launch）与主 kernel 的接力、以及 `attn_sink` 对输出的缩放。
- **u4-l3（tile scheduler metadata）**：本讲把 scheduler 当黑盒（只用了它的 `num_splits` 输出），下一讲会拆开 `get_decoding_sched_meta.cu`，讲清楚负载均衡、`payload`、请求切断与空序列修正的完整算法。
- **回到 u3-l4 / u3-l3**：如果你还想看「主 kernel 内部如何在一个 split 里跑 online softmax（seesaw 调度）」，可以重温 u3-l3，它解释了本讲里 `(m_s, l_s, O_s)` 是怎么被算出来的。
- **延伸阅读**：Flash-Decoding 的原始思路可对照 FlashAttention-2 的 split-KV 描述；本讲证明的 Method A / Method B 等价性，是理解任何 split-KV 注意力实现的基础。
