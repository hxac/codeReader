# KV Cache 机制与拼装

## 1. 本讲目标

学完本讲后，你应该能够：

1. 画出 FasterTransformer（FT）中 GPT 推理时 K/V cache 的**精确内存布局**，并能解释 key cache 与 value cache 为什么采用不同形状。
2. 说清 cache 的**写入（append）时机**：context 阶段一次性写满整段 prompt，decoder 阶段每步追加一个 token。
3. 理解 beam search 选出新 beam 后，FT **为什么不物理拷贝**整个 KV cache，而是用一张 `cache_indirection`（间接寻址表）来"逻辑重排"，并能定位构建这张表的 kernel。
4. 认识 `gpt_kernels.cu` 中的一组 cache 辅助 kernel（`invokeTransposeAxis01`、`invokeTileGptInputs`、`invokeCompactInputs`、`invokeUnCompactCaches` 等）各自负责什么。

本讲承接 u6-l1（ParallelGpt 的 context/decoder 两阶段分裂）与 u3-l2（融合 masked MHA kernel），把"KV cache 这个贯穿两阶段的交接缓冲"彻底讲透。

## 2. 前置知识

- **自回归生成与重复计算问题**：GPT 生成第 t 个 token 时，需要用当前 query 去和**所有历史 token 的 K、V** 做注意力。如果每一步都重新计算前面所有 token 的 K/V，复杂度是 \(O(t^2)\)。KV cache 的做法是把每层算出的 K、V 存下来，下一步直接复用，把每步成本压回 \(O(t)\)。
- **beam search 基本概念**：每步保留 `beam_width` 条"半成品"序列（称为 beam）。新一步在每条 beam 上展开词表，再从候选中收缩回 `beam_width` 条。收缩意味着"新 beam b 的历史，来自旧 beam `parent(b)`"，这正是 KV cache 需要重排的根源。
- **`memory_len` 与 `session_len`**：FT 里 `memory_len` 是 attention 模块实际缓存的窗口长度，`session_len` 是整轮交互式生成的最大长度。当 `memory_len < session_len` 时窗口会滑动，省显存但可能掉精度（见 `docs/gpt_guide.md` 中 `memory_len` 条目）。
- **张量并行（TP）切分**：注意力头被均分到各卡，每卡只持有 `local_head_num = head_num / tensor_para.world_size` 个头的 cache。
- **`x = 16 / sizeof(T)`**：为了一次读满 16 字节（一个向量化 load），FT 把 key cache 最后一维按 `x` 打包。FP16 时 `x = 8`，FP32 时 `x = 4`。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc) | 顶层编排者：分配整块 KV cache 与 `cache_indirections_` 双缓冲，把 src/tgt 间接表在 decoder 与 dynamic decode 之间来回倒。 |
| [src/fastertransformer/models/multi_gpu_gpt/ParallelGptDecoder.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptDecoder.cc) | decoder 阶段：每层把新 token 的 K/V append 进 cache，并按层/iteration 计算偏移。 |
| [src/fastertransformer/models/multi_gpu_gpt/ParallelGptContextDecoder.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptContextDecoder.cc) | context 阶段：一次性把整段 prompt 的 K/V 写入 cache。 |
| [src/fastertransformer/kernels/gpt_kernels.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/gpt_kernels.cu) / [.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/gpt_kernels.h) | cache 与 GPT 解码相关的辅助 kernel：transpose、tile、compact/uncompact、padding mask 等。 |
| [src/fastertransformer/kernels/decoder_masked_multihead_attention.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention.h) | 融合 masked MHA kernel 的参数结构，含 `cache_indir` 字段——beam 重排的真正落点。 |
| [src/fastertransformer/layers/attention_layers/DecoderSelfAttentionLayer.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/DecoderSelfAttentionLayer.cc) | 单步自注意力层：从输入张量取出 `cache_indirection`，传给融合 kernel。 |
| [src/fastertransformer/layers/beam_search_layers/BaseBeamSearchLayer.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/BaseBeamSearchLayer.cu) | 构建下一步 `tgt_cache_indirection` 的 kernel（`update_indir_cache_kernel`）。 |

---

## 4. 核心概念与源码讲解

### 4.1 KV Cache 的存储布局

#### 4.1.1 概念说明

KV cache 是一份按"层 × 序列方向 × 头 × 每头维度"组织的巨型 GPU 缓冲。理解它的关键是三点：

1. **每层各存一份**：transformer 有 `num_layer` 层，每层的 self-attention 都有自己的 K/V cache，互不共享。因此 cache 的第一维是 `num_layer`（更准确说是 `num_layer / pipeline_para.world_size`，因为流水并行把层分到了不同 rank）。
2. **K 与 V 形状不同**：这是 FT 里最容易被忽视的细节。value cache 用"自然"布局 `[head, time, dim]`，而 key cache 被重排成 `[head, dim/x, time, x]`。
3. **K/V 共享一块连续显存**：`value_cache_ = key_cache_ + self_cache_size`，两块贴在一起，分配时一次 `reMalloc` 出 `2 * self_cache_size`。

#### 4.1.2 核心流程：为什么 K 要重排

注意力计算里，Q 要和所有历史 K 做点积。对单个 query 头来说，需要的访问模式是"沿时间轴扫一遍 K"。GPU 上最划算的访存是把"时间轴"放进**连续地址**里，并用向量化 load 一次取 16 字节。

设每头维度 `Dh = size_per_head`，定义 \( x = 16 / \text{sizeof}(T) \)。把 `Dh` 拆成 `Dh/x` 与 `x`，再把时间维 `time` 放到 `x` 之前，就得到 key cache 形状 `[batch, head, Dh/x, time, x]`。这样地址 `[..., t, :]` 连续排布 `x` 个元素（16 字节），刚好一次 load 取满。value cache 则相反——它在 `PV` 步骤里是"按 head 整块读"，所以保留 `[head, time, dim]` 自然布局更顺手。

这两个形状在源码注释里写得很清楚（注意 V 没有 `// x` 那一维）：

[key_cache / value_cache 形状注释 — ParallelGptDecoder.cc:277-278](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptDecoder.cc#L277-L278)

```
//      key_cache   [num_layer, batch_size, head_num, size_per_head // x, memory_len, x]
//      value_cache [num_layer, batch_size, head_num, memory_len, size_per_head]
```

context 阶段的注释表述一致（`local_head_num` 是 TP 切分后本卡持有的头数）：

[ParallelGptContextDecoder.cc:318-319](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptContextDecoder.cc#L318-L319)

#### 4.1.3 源码精读：整块分配与 `x` 因子

cache 在 `ParallelGpt::allocateBuffer` 里一次性分配，K 和 V 共享一块连续显存：

[K/V 共享连续缓冲 — ParallelGpt.cc:109-110, 134-135](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc#L109-L110)

```cpp
const size_t self_cache_size =
    (num_layer_ / pipeline_para_.world_size_) * batchxbeam * memory_len
    * hidden_units_ / tensor_para_.world_size_;
...
key_cache_   = (T*)(allocator_->reMalloc(key_cache_,   sizeof(T) * self_cache_size * 2, true));
value_cache_ = key_cache_ + self_cache_size;
```

读法：

- `num_layer_ / pipeline_para_.world_size_`：流水并行下本 rank 负责的层数（PP 与 TP 正交，见 u2-l5）。
- `batchxbeam = batch_size * beam_width`：序列维被乘上 beam 宽度——**每条 beam 在 cache 里都有自己的"行"**。
- `hidden_units_ / tensor_para_.world_size_`：TP 下每卡只缓存自己那份头（`local_head_num * size_per_head`）。
- `* 2`：K 和 V 拼在一起一次申请。

`x` 这个常量在 `uncompact_caches` kernel 里直接算出来，可作旁证：

[x = 16/sizeof(T) — gpt_kernels.cu:894](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/gpt_kernels.cu#L894)

```cpp
const int x_size = 16 / sizeof(T);
```

#### 4.1.4 代码实践

**实践目标**：用具体数字感受 cache 的尺寸，并验证 K 与 V 形状不同但占用相同。

**操作步骤**：

1. 假设 `num_layer=24, batch_size=2, beam_width=4, memory_len=1024, head_num=16, size_per_head=64, tensor_para=1, pipeline_para=1`，FP16（`sizeof(T)=2`）。
2. 计算 `self_cache_size = 24 * (2*4) * 1024 * (16*64) = 201326592` 个元素。
3. 换算成字节：`201326592 * 2 ≈ 384 MiB`（这是 K 一份；V 另一份同样 384 MiB，共 768 MiB）。
4. 验证 K 形状 `[24, 8, 16, 64/x=32, 1024, x=8]` 与 V 形状 `[24, 8, 16, 1024, 64]` 元素总数都等于 `self_cache_size`。

**预期现象**：两个形状"长得很不像"，但元素总数一致——这正是同一份逻辑数据的不同物理排布。**待本地验证**：可以在 `ParallelGpt::allocateBuffer` 处加一行 `FT_LOG_INFO("kv cache bytes = %lu", sizeof(T) * self_cache_size * 2);` 实跑确认。

#### 4.1.5 小练习与答案

**练习 1**：把 `beam_width` 从 4 改成 1，cache 占用如何变化？
**答案**：`batchxbeam` 减小为原来的 1/4，cache 总占用也降为 1/4。这解释了为什么 greedy（beam=1）比 beam search 省显存。

**练习 2**：为什么 K 要把 `time` 维放到 `x` 前面，而不是保持 `[head, time, dim]`？
**答案**：注意力 QK^T 沿 time 轴扫描 K，把 time 放进连续地址并以 `x` 打包，能让每个 CUDA 线程用一次 16 字节向量化 load 取到一个时间步的 `x` 个 K 分量，最大化显存带宽利用率。

---

### 4.2 Cache 的写入（append）：context 阶段与 decoder 阶段

#### 4.2.1 概念说明

KV cache 的"拼装"分两种节奏（呼应 u6-l1 的两阶段分裂）：

- **context 阶段**：一次吃进整段 prompt（`max_input_length` 个 token），把这一整段的 K/V 一次性写进 cache 的前 `input_length` 个时间槽。
- **decoder 阶段**：每步只吃 1 个 token，把它的 K/V 写进 cache 在 `step` 处的那一个时间槽。

两阶段的写法不同，但**输出都是同一块 `key_cache_` / `value_cache_`**——context 写满 `0..input_length-1`，decoder 从 `step_start` 起往后追加。cache 就是两阶段之间的交接缓冲。

#### 4.2.2 核心流程：偏移计算

不管哪个阶段，写 cache 都不是顶层的 `ParallelGpt` 自己做，而是交给 self-attention 层。顶层只负责算出"这一层、这一批 batch、在 cache 大数组里的起始偏移"，然后把一个带偏移的指针 `k_cache.getPtrWithOffset<T>(cache_offset)` 传下去。

偏移由两部分相加：

- **层偏移**：第 `l` 层的 cache 在本 rank 的起点。decoder 阶段为 `(l - first_layer_id) * 全部batch * 每batch步长`；context 阶段为 `(l - first_layer_id) * request_batch_size * cache_stride_per_batch`。
- **iteration 偏移**：当 batch 太大、被拆成多个 micro-iteration（`ite`）处理时，当前 iteration 的起点。

实际"写"动作发生在 attention 层内部：context 阶段 attention 用 cuBLAS 批量 GEMM 算出整段 K/V 再 reshape 写入；decoder 阶段融合 masked MHA kernel 在算注意力的同时把新 K/V 写进 `step` 槽位。

#### 4.2.3 源码精读

**decoder 阶段的偏移**：注意它把 `k_cache.shape` 从第 1 维开始（跳过 `num_layer`）逐维相乘，得到"一整层"的字步长，再乘以本 rank 内的层序号；接着加上 `ite` 偏移。

[Decoder cache 偏移计算 — ParallelGptDecoder.cc:379-394](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptDecoder.cc#L379-L394)

```cpp
size_t cache_offset = l - getFirstLayerParallelId();
for (auto t = k_cache.shape.begin() + 1; t != k_cache.shape.end(); ++t) {
    cache_offset *= *t;                       // 跳过 num_layer 维，得到单层字步长
}
size_t ite_cache_offset = ite * local_batch_size;
for (auto t = k_cache.shape.begin() + 2; t != k_cache.shape.end(); ++t) {
    ite_cache_offset *= *t;                    // 单个 micro-batch 的步长
}
cache_offset += ite_cache_offset;
...
{"key_cache",   Tensor(MEMORY_GPU, data_type, self_k_cache_size,
                       k_cache.getPtrWithOffset<T>(cache_offset))},
{"value_cache", Tensor(MEMORY_GPU, data_type, self_v_cache_size,
                       v_cache.getPtrWithOffset<T>(cache_offset))},
```

`step`（CPU 标量）作为输入张量传给 self-attention 层（[ParallelGptDecoder.cc:370](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptDecoder.cc#L370)），由融合 kernel 决定把新 K/V 写到哪个时间槽。

**context 阶段的偏移**：写法更直白，先算"每 batch 的步长 = `hidden/Tp * max_seq_len`"，再乘层和 iteration：

[Context cache 偏移计算 — ParallelGptContextDecoder.cc:527-537](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptContextDecoder.cc#L527-L537)

```cpp
const size_t cache_stride_per_batch = hidden_units_ / tensor_para_.world_size_ * max_seq_len;
const size_t cache_layer_offset =
    (l - getFirstLayerParallelId()) * request_batch_size * cache_stride_per_batch;
const size_t ite_cache_offset = ite * local_batch_size * cache_stride_per_batch;
const size_t cache_offset     = cache_layer_offset + ite_cache_offset;

T* k_cache_ptr = use_shared_contexts ? k_cache_layer_ : k_cache.getPtrWithOffset<T>(cache_offset);
T* v_cache_ptr = use_shared_contexts ? v_cache_layer_ : v_cache.getPtrWithOffset<T>(cache_offset);
```

> 注意 `use_shared_contexts` 分支：当启用共享上下文优化（u6-l3 详讲）时，K/V 先写到一块临时的 `k_cache_layer_`，再由 `invokeUnCompactCaches` 散回正式 cache（见 4.4.3）。

#### 4.2.4 代码实践

**实践目标**：跟踪一次 context→decoder 的 cache 写入轨迹。

**操作步骤**：

1. 在 [ParallelGptContextDecoder.cc:534](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptContextDecoder.cc#L534) 处打印 `cache_offset` 与 `l`，观察 context 阶段每层的起点。
2. 在 [ParallelGptDecoder.cc:387](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptDecoder.cc#L387) 处同样打印 `cache_offset`、`l` 和 `step`，观察 decoder 阶段每步写入的槽位。
3. 对比两阶段同一层 `l` 的 `cache_offset`：应当相等——证明两阶段写的是**同一块**层的 cache。

**预期结果**：context 阶段对层 `l` 写满了 `[0, input_length)` 槽；decoder 第 `step` 步只动了 `step` 那一个槽。**待本地验证**（需可运行的 GPU 环境与 GPT checkpoint）。

#### 4.2.5 小练习与答案

**练习**：为什么 context 和 decoder 不合并成一个 kernel？
**答案**：两阶段 query 序列长度相差悬殊（context 是 `max_input_length`，decoder 恒为 1），最优 kernel 选择不同：context 走 cuBLAS 批量 GEMM，decoder 走融合 masked MHA。强行合并会让其中一个阶段用上次优 kernel。这正是 u6-l1 强调的"分裂动机"。

---

### 4.3 Beam Search 下的 Cache 重排：indirection 机制（不物理拷贝）

#### 4.3.1 概念说明：为什么 beam search 会"打乱"cache

朴素想象 beam search 的 cache：第 t 步收缩后，"新 beam 0"可能来自"旧 beam 2"。那么新 beam 0 在读历史 K/V 时，必须去读**旧 beam 2 那一行** cache。如果老老实实物理拷贝，每步要把 `num_layer × batch × beam × memory_len × hidden` 这么大的 buffer 按 `parent` 重排一遍——这是天文数字的显存搬运。

FT 的优雅做法是**间接寻址（indirection）**：cache 数组本身一个字节都不动，额外维护一张 `[batch, beam, memory_len]` 的整型表 `cache_indirection`。融合 attention kernel 读历史 K/V 时，不直接用 `cache[beam, t]`，而是查表 `cache[ cache_indir[beam, t], t ]`。这样"重排"退化成"改一张小整型表"，成本近乎为零。

#### 4.3.2 核心流程：双缓冲 src/tgt

FT 用**两个**间接表 `cache_indirections_[0]` 与 `cache_indirections_[1]`，构成双缓冲，每步交替使用：

```
step t:
  decoder 读  cache_indirections_[src]      （attention 用它指引 cache 读取）
  dynamic decode 算出 parent_ids
  dynamic decode 写 cache_indirections_[tgt] （基于 src + parent_ids）
  swap(src, tgt)
step t+1:
  旧 tgt 变成新 src，重复
```

构建 `tgt` 的规则（核心一行）：

- 对历史时间步 `time < step`：`tgt[batch, beam, time] = src[batch, parent(beam), time]`——新 beam 继承其父 beam 的历史。
- 对当前时间步 `time == step`：`tgt[batch, beam, step] = beam`——新 token 的 K/V 自然写进自己的槽。

#### 4.3.3 源码精读

**(1) 双缓冲分配**：仅当 `beam_width > 1` 才分配，两块挨在一起：

[cache_indirections 双缓冲 — ParallelGpt.cc:136-139](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc#L136-L139)

```cpp
if (beam_width > 1) {
    cache_indirections_[0] =
        (int*)(allocator_->reMalloc(cache_indirections_[0], sizeof(int) * batchxbeam * memory_len * 2, true));
    cache_indirections_[1] = cache_indirections_[0] + batchxbeam * memory_len;
}
```

**(2) decoder 读取时传入 src 表**：注意 `beam_width > 1` 才插入：

[src 表传入 decoder — ParallelGpt.cc:1321-1326](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc#L1321-L1326)

**(3) src/tgt 在 dynamic decode 的输入输出上对称出现**：decoder 用 src，dynamic decode 既吃 src 又产 tgt：

[dynamic decode 的 src/tgt 表 — ParallelGpt.cc:1450-1454, 1486-1490](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc#L1450-L1454)

```cpp
{"src_cache_indirection",
 Tensor{MEMORY_GPU, TYPE_INT32, {local_batch_size, beam_width, memory_len},
        cache_indirections_[src_indir_idx] + id_offset * memory_len}},
...
{"tgt_cache_indirection",
 Tensor{MEMORY_GPU, TYPE_INT32, {local_batch_size, beam_width, memory_len},
        cache_indirections_[tgt_indir_idx] + id_offset * memory_len}},
```

**(4) 融合 kernel 真正用上它**：参数结构里的字段，注释直说"用于 beam sampling"：

[cache_indir 参数 — decoder_masked_multihead_attention.h:65-69](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention.h#L65-L69)

```cpp
T* k_cache = nullptr;   // The cache for the Ks. Size >= B x L x D.
T* v_cache = nullptr;   // The cache for the Vs. Size >= B x L x D.
// The indirections to use for cache when beam sampling.
const int* cache_indir = nullptr;
```

self-attention 层从输入张量取出它，并据此推断 `beam_width`：

[DecoderSelfAttentionLayer 取出 cache_indir — DecoderSelfAttentionLayer.cc:493, 505](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/DecoderSelfAttentionLayer.cc#L493-L505)

```cpp
const int* cache_indir = input_tensors->getPtr<int>("cache_indirection", nullptr);
...
const int beam_width = cache_indir != nullptr ? input_tensors->at("cache_indirection").shape[1] : 1;
```

**(5) 构建 tgt 表的 kernel**：这是 beam 重排的真正落点。`beam_ids` 即 `parent_ids`。注意第 50 行的三元表达式——历史步继承父 beam，当前步写自己：

[update_indir_cache_kernel — BaseBeamSearchLayer.cu:45-50](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/BaseBeamSearchLayer.cu#L45-L50)

```cpp
const int src_beam = beam_ids[batch_id * beam_width + beam_id];   // parent(beam)

const uint tgt_offset = batch_id * beam_width * max_seq_len + beam_id * max_seq_len + time_step_circ;
const uint src_offset = batch_id * beam_width * max_seq_len + src_beam      * max_seq_len + time_step_circ;

tgt_indir_cache[tgt_offset] = (time_step == step) ? beam_id : src_indir_cache[src_offset];
```

其启动器在 beam search 层的 forward 末尾被调用（`beam_width > 1` 时）：

[update_indir_cache_kernelLauncher 调用 — BaseBeamSearchLayer.cu:264-278](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/BaseBeamSearchLayer.cu#L264-L278)

> **关键结论（直接回答实践任务）**：beam search 第 t 步的 KV cache "重排"，**在 `gpt_kernels.cu` 里找不到一个负责拷贝 cache 的 `invokeXxx`**——这是设计意图。FT 故意用间接寻址避免物理拷贝，真正的"重排逻辑"是 `BaseBeamSearchLayer.cu` 里的 `update_indir_cache_kernel`（构建 tgt 间接表），以及融合 MHA kernel 内部对 `cache_indir` 的 gather 读取。`gpt_kernels.cu` 里唯一真正"拷贝 cache"的 kernel 是 `invokeUnCompactCaches`，但它服务的是"共享上下文去重"场景（见 4.4.3），不是 beam search。

#### 4.3.4 代码实践

**实践目标**：解释 beam 重排为什么必须做，并指出谁负责。

**操作步骤**：

1. 阅读上面 [BaseBeamSearchLayer.cu:45-50](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/beam_search_layers/BaseBeamSearchLayer.cu#L45-L50) 的 kernel，假设 `batch=1, beam=2, step=3`，`parent_ids = [1, 0]`（新 beam0 来自旧 beam1，新 beam1 来自旧 beam0），`src_indir` 在历史步是恒等映射。
2. 手算 `tgt[0, 0, 0..2]` 与 `tgt[0, 1, 0..2]`：应分别等于 `src[0, 1, 0..2]` 和 `src[0, 0, 0..2]`。
3. 在 [gpt_kernels.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/gpt_kernels.cu) 中用编辑器搜索 `cache`，确认**没有**专门给 beam search 物理拷贝 K/V 的函数，只有 `invokeUnCompactCaches`（共享上下文用）。

**预期结果**：你将清楚看到 beam 重排的"指挥"在 `BaseBeamSearchLayer`，"执行"（按表读 cache）在融合 MHA kernel 内部，二者都不在 `gpt_kernels.cu`。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果改成"每步物理拷贝整块 cache 按 parent 重排"，单步额外开销量级是多少？
**答案**：约 `num_layer/PP × batch × beam × memory_len × hidden/TP` 个元素的全显存读写，等于一整份 cache 的大小。相比之下间接表只有 `batch × beam × memory_len` 个 int，相差 `num_layer × hidden` 倍。

**练习 2**：`cache_indirection` 为什么用双缓冲而不是单缓冲原地更新？
**答案**：因为构建 `tgt[batch, beam, t] = src[batch, parent(beam), t]` 时，多个新 beam 可能指向同一个父 beam，原地覆写会破坏尚未读取的源数据。双缓冲让读 src、写 tgt 完全分离，下一步再交换指针。

---

### 4.4 gpt_kernels 中的 cache 辅助 kernel：transpose / tile / compact / uncompact

#### 4.4.1 概念说明

`gpt_kernels.cu` 里有一组与 cache、输入拼装相关的辅助 kernel，它们不直接参与 attention，但支撑着 cache 的正确组装与 TP 下的布局还原：

| kernel | 作用 | 典型场景 |
|--------|------|----------|
| `invokeTransposeAxis01` | 交换三维张量的第 0、1 轴 | TP>1 时 logits all-gather 后还原布局 |
| `invokeTileGptInputs` / `invokeTileGptPromptInputs` | 把 `[batch, len]` 复制成 `[batch*beam, len]` | beam search 时给每条 beam 准备一份输入 |
| `invokeCompactInputs` / `invokeUnCompactOutputs` | 共享上下文去重的紧凑/还原 | 多请求共用同一 system prompt |
| `invokeUnCompactCaches` | 把紧凑计算的 K/V 散回正式 cache | 共享上下文的 cache 写回 |
| `invokeFindContextDups` | 找出重复的上下文 | 共享上下文优化的预处理 |
| `invokeLookupHiddenStateOfLastToken` | 取出每条序列最后一个 token 的隐状态 | context→decoder 交接 |
| `invokeMaskPaddingTokens` | 标记 padding 位置为 masked | 避免 padding 参与 attention |

#### 4.4.2 核心流程：transpose 与 tile

**`invokeTransposeAxis01`**：把 `[dim0, dim1, dim2]` 转成 `[dim1, dim0, dim2]`，逐元素重排。它在 TP>1 的 GPT 里有个关键用途——logits all-gather 后还原布局。TP>1 时每卡只算词表的一段，all-gather 后形状是 `[Tp, batch*beam, local_vocab]`，需要转成 `[batch*beam, Tp, local_vocab]` 才能当作连续的 `[batch*beam, vocab]` 读取。

[transposeAxis01 kernel — gpt_kernels.cu:296-310](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/gpt_kernels.cu#L296-L310)

```cpp
// out[out_idx] = in[in_idx]，把 [d0,d1,d2] -> [d1,d0,d2]
out[input_dim1_index * dim0 * dim2 + input_dim0_index * dim2 + input_dim2_index] =
    in[input_dim0_index * dim1 * dim2 + input_dim1_index * dim2 + input_dim2_index];
```

[ParallelGpt.cc 中的调用 — ParallelGpt.cc:1428-1433](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGpt.cc#L1428-L1433) 正是 logits 还原。

**`invokeTileGptInputs`**：beam search 时每条 beam 需要一份相同的输入 id 序列。该 kernel 把 `[batch, max_input_length]` 复制成 `[batch*beam, max_input_length]`——每条序列连续复制 `beam_width` 份。它内部直接委托给 `invokeTileGptPromptInputs`（`prefix_prompt` 传 `nullptr`）：

[invokeTileGptInputs — gpt_kernels.cu:550-569](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/gpt_kernels.cu#L550-L569)

底层 kernel 用 `grid(batch, beam)` 二维 grid，每个 block 负责一个 `(batch, beam)`：

[tileGptPromptInputs kernel — gpt_kernels.cu:496-515](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/gpt_kernels.cu#L496-L515)

#### 4.4.3 核心流程：compact / uncompact（共享上下文）

当多个请求共享同一段 system prompt（见 u6-l3），FT 先用 `invokeFindContextDups` 找出重复，再用 `invokeCompactInputs` 只对去重后的 `compact_size` 条序列跑 context decoder，最后用 `invokeUnCompactOutputs` / `invokeUnCompactCaches` 把结果散回每条请求。

`invokeUnCompactCaches` 是这一节里**唯一真正搬运 K/V cache 的 kernel**：它把每层临时存在 `k_cache_layer_` 的紧凑 cache，按 `batch_to_compact_idx` 散回正式的 `[batch, head, ...]` cache。注意它内部要处理 K 与 V 的不同布局（`handle_k` 分支用 `x_size` 重排，V 分支用自然布局）：

[uncompact_caches kernel — gpt_kernels.cu:877-927](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/gpt_kernels.cu#L877-L927)

```cpp
const int x_size = 16 / sizeof(T);
...
if (handle_k) {
    const int i0 = idx % (x_size * seq_len);
    const int i1 = (idx / (x_size * seq_len)) % (num_heads * size_per_head / x_size);
    dst_offset = i1 * max_seq_len * x_size + i0;          // K 的 [head, dim/x, time, x] 布局
} else {
    const int i0 = idx % (size_per_head * seq_len);
    const int i1 = (idx / (size_per_head * seq_len)) % (num_heads);
    dst_offset = i1 * max_seq_len * size_per_head + i0;    // V 的 [head, time, dim] 布局
}
```

它在 context decoder 的层循环内、每层 attention 之后被调用（`use_shared_contexts` 时）：

[ParallelGptContextDecoder 调用 invokeUnCompactCaches — ParallelGptContextDecoder.cc:548-568](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/models/multi_gpu_gpt/ParallelGptContextDecoder.cc#L548-L568)

#### 4.4.4 源码精读：其它辅助 kernel

- **`invokeLookupHiddenStateOfLastToken`**：context 阶段算完整段 prompt 后，decoder 第一步只需要"最后一个 token"的隐状态作为输入。该 kernel 按 `input_lengths[b]-1` 取出每条序列的末 token 隐状态：[gpt_kernels.cu:437-452](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/gpt_kernels.cu#L437-L452)。
- **`invokeMaskPaddingTokens`**：构造 `masked_tokens` 数组，让融合 MHA 跳过 padding 位置：[gpt_kernels.cu:1035-1050](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/gpt_kernels.cu#L1035-L1050)。
- **`invokeFindContextDups`**：用三角配对比较所有 `(i,j)` 请求的 input_ids 是否完全相同，配合 `generate_dups_indices` 生成去重索引：[gpt_kernels.cu:580-693](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/gpt_kernels.cu#L580-L693)。

#### 4.4.5 代码实践

**实践目标**：给每个辅助 kernel 找到它的"调用现场"，确认它属于哪条链路。

**操作步骤**：

1. 用 `grep` 在 `src/fastertransformer/models/multi_gpu_gpt/` 下搜索 `invokeTransposeAxis01`、`invokeTileGptInputs`、`invokeUnCompactCaches`、`invokeLookupHiddenStateOfLastToken`，记录各自的调用文件与上下文。
2. 对每个 kernel 写一句话："它服务于 [context/decoder/动态解码] 阶段的 [布局还原/输入复制/cache 散回/交接] 需求"。

**预期结果**：你会发现 `invokeTransposeAxis01` 服务 logits TP 还原，`invokeTileGptInputs` 服务 beam 输入复制，`invokeUnCompactCaches` 服务共享上下文 cache 散回，`invokeLookupHiddenStateOfLastToken` 服务 context→decoder 交接——四者互不重叠。**待本地验证**。

#### 4.4.6 小练习与答案

**练习 1**：`invokeTileGptInputs` 复制输入 id，那 KV cache 是否也需要类似的"初始 tile"？
**答案**：不需要。context 阶段对 `batch*beam` 条输入直接计算并写入 cache，每条 beam 的 cache 行天然独立。tile 只针对"输入 id"这种小数据，cache 的"复制"由 indirection 在读取时完成（见 4.3）。

**练习 2**：`invokeUnCompactCaches` 与 beam search 的 cache 重排有什么本质区别？
**答案**：前者是**物理搬运**（紧凑 buffer → 正式 cache），因为紧凑计算省了一半 context，必须把结果真实写回；后者是**逻辑重排**（只改间接表，cache 不动），因为每步都重排代价太大。两者解决不同问题，互不替代。

---

## 5. 综合实践

**任务**：用一张完整的"KV cache 生命周期图"把本讲串起来。

请画出（或用文字描述）一次 `batch=2, beam=4` 的 GPT 推理，要求标注：

1. **布局**：cache 大数组的形状 `[num_layer/PP, batch*beam, local_head_num, ...]`，K 与 V 的不同尾维。
2. **context 阶段**（第 0 步）：`invokeTileGptInputs` 把 `[2, L]` 输入 tile 成 `[8, L]` → context decoder 一次性写入 `8` 行 cache 的 `[0, L)` 槽 → `invokeLookupHiddenStateOfLastToken` 取末 token。
3. **decoder 阶段**（第 1、2…步）：每步融合 MHA kernel 用 `cache_indir` 读取（`beam>1` 时）历史 K/V、把新 K/V 写进 `step` 槽。
4. **beam 重排**（每步）：`update_indir_cache_kernel` 用 `parent_ids` 从 `src` 表构建 `tgt` 表，src/tgt 双缓冲交替。
5. **辅助 kernel 的落点**：`invokeTransposeAxis01`（logits TP 还原）、`invokeUnCompactCaches`（仅共享上下文时）。

完成后，对照本讲各节源码链接自查每个箭头是否有对应代码支撑。**待本地验证**：若环境允许，可在关键函数加 `FT_LOG_DEBUG` 并设 `FT_LOG_LEVEL=DEBUG`（见 u1-l5）实跑确认调用顺序。

## 6. 本讲小结

- KV cache 形状为 `[num_layer/PP, batch*beam, local_head_num, ...]`，**K 与 V 尾维不同**：K 是 `[dim/x, time, x]` 便于向量化扫描，V 是 `[time, dim]` 自然布局；`x = 16/sizeof(T)`。
- K/V **共享一块连续显存**，大小 `(num_layer/PP) × batch×beam × memory_len × hidden/TP`，TP 切头、PP 切层。
- cache 的写入分两阶段：**context 一次写满 prompt**，**decoder 每步 append 一个 token**；两阶段通过层偏移 + iteration 偏移定位到同一块层的 buffer，`step` 决定时间槽。
- beam search 的 cache "重排"**不物理拷贝**，而是用 `cache_indirection` 间接表 + 双缓冲；构建 tgt 表的 kernel 是 `BaseBeamSearchLayer.cu` 的 `update_indir_cache_kernel`，真正按表读 cache 的是融合 MHA kernel。
- `gpt_kernels.cu` 提供一组辅助 kernel：`invokeTransposeAxis01`（logits TP 还原）、`invokeTileGptInputs`（beam 输入复制）、`invokeUnCompactCaches`（共享上下文 cache 物理散回，**唯一真正搬运 cache 的 kernel**）、`invokeLookupHiddenStateOfLastToken`（context→decoder 交接）。
- 区分两个易混点：beam 重排靠**逻辑 indirection**，共享上下文靠**物理 compact/uncompact**——二者服务于不同优化，不可混为一谈。

## 7. 下一步学习建议

- **u6-l3 交互式生成、共享上下文与流式生成**：深入 `invokeFindContextDups` / `invokeCompactInputs` / `invokeUnCompactCaches` 的完整链路，理解多轮对话如何复用上一轮 KV cache。
- **u3-l2 注意力 kernel**：回到融合 masked MHA kernel 的 `template.hpp`，看它内部如何用 `cache_indir` 做 gather，把"间接寻址"从概念落实到线程级。
- **u8-l2 Beam search 层**：理解 `parent_ids` 是如何从 top-k 候选里产生的——这是 cache 重排的"上游输入"。
- 继续精读：`docs/gpt_guide.md` 的 `memory_len`/`session_len`/interactive generation 章节，对照 `ParallelGpt.cc` 的 `forward` 主循环把整条生成链路走通。
