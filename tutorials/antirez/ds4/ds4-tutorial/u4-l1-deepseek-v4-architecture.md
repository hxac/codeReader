# DeepSeek V4 架构总览

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 DeepSeek V4 一层 Transformer 内部到底走了哪几步，以及每一步对应的权重张量。
- 理解 **MLA（Multi-head Latent Attention，多头潜注意力）** 为什么要把 KV 压缩成一个共享的潜向量，以及它如何让 1M 上下文成为可能。
- 区分 **routed experts（路由专家）** 和 **shared expert（共享专家）**，理解 Mixture-of-Experts（MoE，混合专家）的路由、Top-k 选择与加权合并。
- 画出「输入 hidden → attention → router → routed + shared experts → 输出」的单层前向数据流图。

本讲只建立**架构全景**，不展开 KV 缓存的压缩细节（那是 u4-l2）、采样（u4-l3）和 GPU 实现（u5）。我们把镜头对准 ds4 里那份**最容易读懂的前向实现——CPU 参考路径**，因为它的每一步都和 Metal/CUDA/ROCm 后端做完全相同的数学。

## 2. 前置知识

在进入本讲前，请确认你已经理解（这些都在 u3-l2 建立）：

- **权重绑定**：ds4 在引擎打开时，把 GGUF 里按字符串命名的张量一次性填进 `ds4_layer_weights` 这张「语义指针表」。之后推理代码用 `layer->attn_q_a`、`layer->ffn_gate_exps` 这类字段直访，字符串查询退役。
- **量化格式**：权重侧有 Q2_K / Q4_K / IQ2_XXS，激活侧有 Q8_0。本讲只需知道「激活先量化成 Q8_0，再和量化权重做整数点积」这一骨架。
- **mmap 加载**：权重字节始终留在 mmap 映射区，推理代码用 `tensor_data(model, tensor)` 直接寻址。

下面几个术语本讲会反复用到，先给一句话解释：

| 术语 | 一句话解释 |
|---|---|
| **Embedding（嵌入）** | 把一个 token id 映射成一个固定维度的浮点向量（DeepSeek V4 Flash 是 4096 维）。 |
| **Transformer 层** | 一个可重复堆叠的块，由「注意力子层 + 前馈子层」组成。DeepSeek V4 Flash 堆了 43 层。 |
| **残差连接（Residual）** | 子层的输出会和输入相加（`y = x + sublayer(x)`），让信号能跨层流动。DeepSeek V4 把单条残差流换成了 4 条并行流，叫 **HC（Hyper-Connection）**。 |
| **MoE（混合专家）** | 前馈层不只有一个网络，而是有一池「专家」子网络，每个 token 只激活其中少数几个。 |
| **KV 缓存** | 注意力计算需要历史 token 的 Key/Value 向量；把它们缓存下来，避免每生成一个新 token 就重算一遍。 |

如果你对这些还感到陌生，建议先回看 u3-l2 的权重绑定部分。

## 3. 本讲源码地图

本讲涉及的源码都在仓库根目录的几个文件里：

| 文件 | 本讲用到的地方 | 作用 |
|---|---|---|
| `ds4.c` | 层权重结构、默认形状常量、CPU 参考前向 | **本讲的主战场**。这里既定义了「一层有哪些权重」，也写出了「一层怎么算」。 |
| `ds4.h` | `ds4_engine_layer_compress_ratio` 声明 | 暴露给前端的少量架构查询接口。 |
| `MODEL_CARD.md` | 架构与压缩注意力说明 | 官方模型卡片摘录，确认 ds4 实现与官方面板一致。 |
| `README.md` | 动机与设计哲学 | 解释「为什么这么做」。 |

一个关键定位锚点：CPU 参考路径的「一层前向」函数集中在 `ds4.c` 的第 6 千到 1 万行之间。本讲的源码精读几乎都在这个区间。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**MLA 注意力**、**MoE 专家**、**单层数据流**。三者合起来就是一次完整的层前向。

### 4.1 MLA 注意力

#### 4.1.1 概念说明

普通多头注意力（MHA）的痛点：每个 token、每个注意力头都要存一份自己的 Key 和 Value。设头数为 \(h\)、头维度为 \(d\)，则每存一个 token 的 KV 缓存就要 \(2 \times h \times d\) 个浮点数。DeepSeek V4 Flash 有 64 个头、每头 512 维，如果照搬 MHA，**每 token 的 KV 缓存就是 64 × 512 = 32768 维**。100 万 token 的上下文根本塞不进任何个人电脑。

**MLA（Multi-head Latent Attention，多头潜注意力）** 的核心想法：把 KV **压缩**成一个低维的「潜向量」存进缓存；真正用到注意力时，再让每个查询头各自从这个潜向量「读」出自己的 K 和 V。这样 KV 缓存只存压缩后的潜向量，体量大幅缩水。

DeepSeek V4 的实现更进一步——它让**所有 64 个查询头共享同一个 512 维的 KV 潜向量**（而不是每头各存一份）。这意味着：

- 每 token 的 KV 缓存只有 **1 × 512** 维（外加后面 u4-l2 要讲的压缩），而不是 64 × 512。
- 64 个查询头 \(Q_h\) 各自用不同的投影去「读」这同一份共享 KV，从而保留多头的表达能力。

这正是 ds4 形状常量 `n_head_kv = 1` 的含义：**1 个共享 KV 头**，被 64 个查询头共用。

> 术语提醒：MODEL_CARD 把 DeepSeek V4 的混合注意力设计称为 CSA（Compressed Sparse Attention）+ HCA（Heavily Compressed Attention）。其中「共享潜 KV」就是 MLA 的那部分；CSA/HCA 的滑动窗口 + 压缩行属于 u4-l2 的 KV 缓存设计，本讲先按下不表，只关注「一层之内注意力怎么算」。

#### 4.1.2 核心流程

给定一层的归一化输入向量 \(x\)（4096 维），MLA 注意力的流程是：

1. **Q 的两段式投影（低秩瓶颈）**：
   - 先降维：\(c_q = W_{q_a}\, x\)，把 4096 维压到 1024 维的潜向量（`n_lora_q = 1024`）。
   - 再升维：\(q = W_{q_b}\,\text{RMSNorm}(c_q)\)，把 1024 维展开成 \(64 \times 512\) 的完整查询。
   - 中间夹一个 **学到的 RMSNorm**（`attn_q_a_norm`），稳定潜向量。
2. **KV 的单段压缩投影**：
   - \(kv = \text{RMSNorm}(W_{kv}\, x)\)，把 4096 维压成 **单个 512 维** 的共享 KV 潜向量。这就是要存进缓存的那一份。
3. **解耦 RoPE（旋转位置编码）**：只对前 64 维（`n_rot = 64`）施加旋转位置编码，其余 448 维不动。这样做是为了让「带旋转的部分」和「压缩潜向量」能分开处理，保持潜向量的可压缩性。
4. **注意力打分**：对 64 个查询头中的每一个 \(q_h\)，与缓存的 KV 行做标准缩放点积注意力：
   \[
   \text{head}_h = \text{softmax}\!\left(\frac{q_h K^T}{\sqrt{d}}\right) V,\qquad d = 512
   \]
   注意所有头读的是**同一组 KV 行**。此外每头还带一个 **attention sink（注意力汇）** 常数，作为「不关注任何 token」时的兜底。
5. **分组输出投影**：把 64 个头的输出重新组织成 8 组，每组先压到 1024 维低秩，再统一投影回 4096 维。

#### 4.1.3 源码精读

**(1) 形状常量：64 头、1 个 KV 头、512 维**

DeepSeek V4 Flash 的全部架构尺寸写死在一份默认形状表里。注意 `n_head_kv = 1`——这就是「共享 KV」的代码落点：

[ds4.c:180-212](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L180-L212) —— `DS4_SHAPE_FLASH` 默认形状表，标定了头数、KV 头数、潜维度、专家数等。

关键几行（节选）：

```c
.n_head = 64,          // 64 个查询头
.n_head_kv = 1,        // 1 个共享 KV 头（MLA 的关键）
.n_head_dim = 512,     // 每头 512 维
.n_lora_q = 1024,      // Q 潜向量的低秩维度
```

**(2) Q 的两段式低秩投影**

Q 不是一次投影出来的，而是先降维到 1024、RMSNorm、再升维到 \(64\times512\)。`attn_q_a` 是降维矩阵，`attn_q_b` 是升维矩阵：

[ds4.c:6744-6760](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L6744-L6760) —— `layer_q_projection_normed_one`，Q 的两段式 MLA 投影。

```c
matvec_q8_0(qr, model, layer->attn_q_a, norm);          // 降维：4096 -> 1024
rms_norm_weight(qr_norm, qr, q_a_norm, q_rank, DS4_RMS_EPS);  // 学到的 RMSNorm
matvec_q8_0(q, model, layer->attn_q_b, qr_norm);        // 升维：1024 -> 64*512
head_rms_norm_inplace(q, DS4_N_HEAD, DS4_N_HEAD_DIM, DS4_RMS_EPS);
```

**(3) KV 的单段压缩投影**

KV 只投影出 **一个 512 维** 向量（不是每头一份）。`attn_kv` 是从 4096 到 512 的压缩矩阵：

[ds4.c:6781-6794](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L6781-L6794) —— `layer_kv_projection_normed_one`，把 4096 维压成单个 512 维共享 KV。

```c
matvec_q8_0(raw, model, layer->attn_kv, normed);        // 压缩：4096 -> 512
rms_norm_weight(kv, raw, kv_norm, DS4_N_HEAD_DIM, DS4_RMS_EPS);
```

函数上方的注释写得很直白：`/* KV projection has one KV head of width 512, followed by a learned RMSNorm. */`。

**(4) 缩放点积注意力（所有头共享同一组 KV）**

下面的循环是 MLA「共享 KV」最直接的证据：外层遍历 64 个查询头 `qh`，内层对**同一组 `kv_rows`** 打分。每个头只是用自己那段查询 `qh` 去点积同一批 KV 行：

[ds4.c:7045-7083](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L7045-L7083) —— `layer_attention_rows_one`，单 token 的多头注意力打分与加权求和。

```c
const float kq_scale = 1.0f / sqrtf((float)DS4_N_HEAD_DIM);
for (uint32_t h = 0; h < DS4_N_HEAD; h++) {
    const float *qh = q + (uint64_t)h * DS4_N_HEAD_DIM;
    float max_score = sinks[h];                 // attention sink 兜底
    for (uint32_t r = 0; r < n_kv; r++) {
        score[r] = dot_f32(qh, kv_rows + r*DS4_N_HEAD_DIM, DS4_N_HEAD_DIM) * kq_scale;
        ...
    }
    // softmax + 加权求和到 out_heads[h]
}
```

`sinks`（`attn_sinks`）是每头一个的常数，让模型在「不想关注任何历史 token」时有一个去处，这是长上下文稳定性的常用技巧。

**(5) 分组输出投影**

注意力输出不是直接一步投回 4096 维，而是先分成 8 组、每组压到 1024 维低秩、再统一升回 4096：

[ds4.c:7096-7112](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L7096-L7112) —— `layer_grouped_out_one`，把 64 头的输出分 8 组经低秩再投回。

```c
const uint32_t n_groups = 8;
matvec_q8_0_grouped_rows(low, model, layer->attn_output_a, heads, ...);  // 分组降维
matvec_q8_0(out, model, layer->attn_output_b, low);                       // 升回 4096
```

这种「分组 + 低秩」结构与 Q 投影的两段式瓶颈一脉相承，都是用低秩矩阵替代稠密大矩阵，既省算力也省权重存储。

#### 4.1.4 代码实践

**实践目标**：从源码里读出 MLA「共享 KV」这一设计，并用数字验证它省了多少 KV 缓存。

**操作步骤**：

1. 打开 [ds4.c:180-212](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L180-L212)，记下 `n_head`、`n_head_kv`、`n_head_dim` 三个值。
2. 打开 [ds4.c:6781-6794](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L6781-L6794)，确认 KV 投影只产出 `DS4_N_HEAD_DIM`（=512）维，而不是 `n_head * n_head_dim`。
3. 打开 [ds4.c:7045-7083](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L7045-L7083)，确认外层头循环和内层 KV 行循环是「64 头共用同一组 KV」。

**需要观察的现象（算一笔账）**：

- 假设没有 MLA（标准 MHA），每 token 的 KV 缓存维度 = \(2 \times n\_head \times n\_head\_dim = 2 \times 64 \times 512 = 65536\)（K 和 V 各一份）。
- ds4 实际存的 = \(1 \times n\_head\_dim = 512\)（K/V 共享一个潜向量）。
- 压缩比约为 \(65536 / 512 = 128\) 倍。

**预期结果**：你能用自己的话写出「MLA 把每 token 的 KV 从约 6.5 万维压到 512 维，所以 1M 上下文才装得下」这句话，并指出代码里 `n_head_kv = 1` 就是这个压缩的开关。这里**不需要运行模型**，纯源码阅读即可完成。

> 如果想看真实数值：函数 `layer_routed_moe_one` 等 forward 函数带一个 `trace` 形参，置真时会用 `print_vec_stats` 打印每层中间向量的统计（见 [ds4.c:7838-7842](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L7838-L7842)）。该 trace 路径由服务器 `--trace`（[ds4_server.c:11577](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11577)）等入口驱动，本讲暂不深入。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `n_head_kv` 从 1 改成 64（退回标准 MHA），KV 缓存会变大约多少倍？模型表达能力会变好还是变差？

**参考答案**：KV 缓存每 token 从 512 维变成 \(64 \times 512 = 32768\) 维，约 64 倍（若 K/V 分开则 128 倍）。理论上每头有独立 KV，表达能力略增，但代价是 1M 上下文几乎不可行——这正是 MLA 要避免的。

**练习 2**：Q 的投影为什么要分两段（先降到 1024，再升到 64×512），而不是直接一步投影？

**参考答案**：这是**低秩瓶颈（LoRA 式）**结构。直接投影需要 \(4096 \times 32768\) 的稠密矩阵；两段式用 \(4096 \times 1024\) + \(1024 \times 32768\)，参数量和算力都大幅减少，同时中间的 RMSNorm 起到稳定作用。代价是表达能力受限为秩 1024，但对这一层已经足够。

---

### 4.2 MoE 专家

#### 4.2.1 概念说明

普通 Transformer 的前馈子层（FFN）是**一个**稠密网络：对每个 token 都跑完整的三层 MLP（gate → up → down，配 SwiGLU 激活）。模型越大，这个 FFN 的参数就越大，而且**每个 token 都要算全部参数**。

**MoE（Mixture-of-Experts，混合专家）** 换了个思路：把那个大 FFN 拆成很多个小的「专家」FFN，再配一个 **router（路由器）**。每个 token 来了，router 只挑出少数几个专家来算，其余专家闲置。这样：

- **总参数**可以做得极大（DeepSeek V4 Flash 有 256 个专家，总参 284B）。
- 但每个 token 只**激活**少数专家（Flash 激活 6 个，总激活参 13B）。

这就是「284B 总参 / 13B 激活」的来历：参数多但不全用，所以又大又快。

DeepSeek V4 的 MoE 还多一个 **shared expert（共享专家）**：它是一个**永远被激活**的稠密专家，不管 router 选了谁，它都会参与计算。直觉上，routed experts 负责「特化」的知识（router 决定调谁），shared expert 负责「通用」的知识（每个 token 都要走）。

ds4 的量化策略（u1-l2、u3-l4）正是围绕这个区分展开的：**只把 routed experts 压到 2bit**（IQ2_XXS/Q2_K），而 shared expert 和所有投影保持高精度（Q8_0）。

#### 4.2.2 核心流程

给定归一化后的输入 \(x\)（4096 维），MoE 子层的流程：

1. **Router 打分**：用一个矩阵 \(W_{gate}\)（`ffn_gate_inp`）把 \(x\) 投成 256 个 logit，再变换成分数：
   \[
   p_i = \sqrt{\text{softplus}(z_i)},\qquad i = 1\dots 256
   \]
   这里用 `sqrt(softplus(·))` 而不是普通 softmax，是 DeepSeek 的设计选择。
2. **Top-k 选专家**：从 256 个分数里选出最高的 6 个（`n_expert_used = 6`）。这 6 个专家的分数再归一化成权重 \(w_k\)，乘上一个全局缩放 `expert_weight_scale = 1.5`。
   - **例外**：前 3 层（`n_hash_layer = 3`）不用 router 打分，而是用一张 **hash 表**（`ffn_gate_tid2eid`）直接按 token id 查出该用哪 6 个专家。这是一种省算力的「硬路由」。
3. **每个被选中的 routed expert 各算一遍**（标准的 gate/up/down SwiGLU FFN）：
   \[
   \text{mid} = \text{SiLU}(W_g^{(e)} x) \odot (W_u^{(e)} x),\qquad y_e = W_d^{(e)}\,\text{mid}
   \]
   其中 \(\text{SiLU}(x)=x\cdot\sigma(x)\)。门控值在激活前还会被 **clamp（截断）**，防止数值爆炸。
4. **加权求和**：把 6 个专家的输出按 router 权重加起来：
   \[
   \text{MoE}(x) = \sum_{k=1}^{6} w_k \, y_{e_k}
   \]
5. **加上 shared expert**：shared expert 用自己的 gate/up/down（`ffn_*_shexp`）算一遍同样的 SwiGLU FFN，输出直接加到上面：
   \[
   \text{FFN}(x) = \text{MoE}(x) + \text{Shared}(x)
   \]

#### 4.2.3 源码精读

**(1) 一层里有哪些 FFN 权重**

回顾 u3-l2 绑定的 `ds4_layer_weights`，FFN 相关的字段集中在一处。注意 routed experts（`*_exps`）和 shared expert（`*_shexp`）是**分开的两套权重**：

[ds4.c:3042-3051](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L3042-L3051) —— `ds4_layer_weights` 里的 FFN 字段（节选）。

```c
ds4_tensor *ffn_gate_inp;     // router 打分矩阵：4096 -> 256
ds4_tensor *ffn_gate_exps;    // 256 个专家的 gate 投影（routed）
ds4_tensor *ffn_up_exps;      // 256 个专家的 up 投影（routed）
ds4_tensor *ffn_down_exps;    // 256 个专家的 down 投影（routed）
ds4_tensor *ffn_gate_shexp;   // shared expert 的 gate 投影
ds4_tensor *ffn_up_shexp;     // shared expert 的 up 投影
ds4_tensor *ffn_down_shexp;   // shared expert 的 down 投影
```

注意 `ffn_gate_exps` 等是**三维**张量（含 N 个专家，见 u3-l2 的 `tensor_expect_routed_expert(..., 3, DS4_N_EMBD, DS4_N_FF_EXP, DS4_N_EXPERT)`），而 `ffn_gate_shexp` 是二维的（只有一个专家）。

**(2) Router 打分：sqrt(softplus(logit))**

router 对每个专家打出一个分数，变换是 `sqrt(softplus(·))`：

[ds4.c:7328-7339](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L7328-L7339) —— `layer_router_probs_one`，router 打分。

```c
matvec_any(logits, model, layer->ffn_gate_inp, x);     // 4096 -> 256 个 logit
for (uint32_t i = 0; i < DS4_N_EXPERT; i++) {
    probs[i] = sqrtf(softplus_stable(logits[i]));      // sqrt(softplus(·))
}
```

函数上方的注释点明：`/* Router scores use sqrt(softplus(logit)); normalization happens only after the six selected experts are known. */`——归一化只在选出 6 个专家**之后**做，不是对全部 256 个做 softmax。

**(3) Top-k 选 6 个专家**

选出分数最高的 6 个，再把它们的分数归一化成权重（乘以 `expert_weight_scale`）：

[ds4.c:7393-7431](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L7393-L7431) —— `layer_topk_selected_experts` + `layer_topk_selected_experts_from_probs`，Top-6 选择与权重归一化。

```c
topk_desc(selection, DS4_N_EXPERT, DS4_N_EXPERT_USED, selected);   // 选 6 个
float sum = 0.0f;
for (i in 6) { expert_weight[i] = probs[selected[i]]; sum += ...; }
for (i in 6) { expert_weight[i] = expert_weight[i] / sum * DS4_EXPERT_WEIGHT_SCALE; }
```

注意还有一个可选的 bias 项 `ffn_exp_probs_b`，会加到选择分数上影响「选谁」，但**不影响**最终的归一化权重——注释称之为「biased selection, unbiased weighting」（偏向性选择，无偏向加权）。

**(4) routed expert 的执行：SwiGLU + clamp + 加权累加**

选中专家后，对每个专家算 gate/up（SwiGLU 前半）、再 down 投影，按权重累加。`trace` 为真时的可读路径把每一步都摆出来：

[ds4.c:7486-7521](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L7486-L7521) —— routed expert 的逐专家计算（trace 路径，最易读）。

```c
for (uint32_t i = 0; i < DS4_N_EXPERT_USED; i++) {
    const uint32_t expert = selected[i];
    matvec_expert_pair_prequant(gate, up, ..., layer->ffn_gate_exps, layer->ffn_up_exps, xq, expert);
    // clamp gate/up，防止 SwiGLU 前数值爆炸
    mid[j] = silu(gate[j]) * up[j] * expert_weight[i];   // SwiGLU + router 权重
    matvec_expert_down(down, model, layer->ffn_down_exps, mid, expert);
    for (j) out[j] += down[j];                            // 累加到输出
}
```

代码里的注释明确：`/* DeepSeek V4 clamps routed expert gate/up values before SwiGLU and applies the router weight before the down projection. */`。

**(5) shared expert：永远激活的稠密 FFN**

shared expert 是单独一个 Q8_0 的 SwiGLU FFN，每个 token 无条件跑一遍，**不经过 router**：

[ds4.c:7184-7216](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L7184-L7216) —— `layer_shared_ffn_one`，shared expert 的 SwiGLU FFN。

```c
quantize_q8_0_activation(x, xq, xscale, in_dim);
matvec_q8_0_pair_prequant(gate, up, model, layer->ffn_gate_shexp, layer->ffn_up_shexp, xq, xscale);
swiglu(mid, gate, up, DS4_N_FF_EXP, DS4_SWIGLU_CLAMP_EXP);
matvec_q8_0(out, model, layer->ffn_down_shexp, mid);
```

注意它用的是 `*_shexp`（shared expert）权重，且没有 top-k、没有 router 权重——进来就算。

**(6) routed + shared 合并**

最终 FFN 输出就是两者简单相加，外层函数里只有一行：

[ds4.c:7854-7874](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L7854-L7874) —— `layer_ffn_one` 中调用 routed MoE、shared expert 并相加。

```c
layer_routed_moe_one(moe, model, layer, norm, il, token, DS4_SWIGLU_CLAMP_EXP, trace);
layer_shared_ffn_one(shared, model, layer, norm);
for (uint32_t i = 0; i < DS4_N_EMBD; i++) {
    ffn_out[i] = moe[i] + shared[i];     // routed + shared
}
```

#### 4.2.4 代码实践

**实践目标**：从源码确认「router 只选 6 个专家」和「shared expert 与 router 无关」，并理解为什么只压缩 routed experts。

**操作步骤**：

1. 打开 [ds4.c:180-212](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L180-L212)，记下 `n_expert = 256`、`n_expert_used = 6`、`n_expert_shared = 1`、`n_ff_exp = 2048`。
2. 打开 [ds4.c:7393-7431](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L7393-L7431)，确认 `topk_desc` 的第三个参数是 `DS4_N_EXPERT_USED`（=6）。
3. 打开 [ds4.c:7184-7216](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L7184-L7216)，确认 shared expert 的函数签名里**没有** `selected`/`expert_weight` 参数——它根本不看 router。

**需要观察的现象**：

- 一个 token 实际触发的 routed expert 数 = 6；加上 1 个 shared expert = 共 7 个小 FFN。
- 而 routed experts 总池 = 256 个，绝大多数（250 个）此刻闲置。
- 这 250 个闲置专家的权重，正是 SSD 流式（u9）和 2bit 量化要优化的对象。

**预期结果**：你能解释「为什么量化只针对 routed experts」——因为它们占了模型绝大部分体积（256 个专家 × 3 个矩阵），但每个 token 只用到 6 个，低精度对单 token 影响有限；而 shared expert 每个 token 都用，必须高精度。这一步同样是纯源码阅读，**待本地验证**的部分仅在你真机跑模型时才有意义。

#### 4.2.5 小练习与答案

**练习 1**：router 为什么用 `sqrt(softplus(·))` 而不是直接 softmax 全部 256 个专家？

**参考答案**：DeepSeek 的设计是「先打分、再 Top-k、最后只对选中的 6 个归一化」。如果先对 256 个 softmax，大量概率会分给没被选中的专家，选中专家的相对权重反而失真；`sqrt(softplus(·))` 保证分数非负且数值稳定，归一化推迟到选出 6 个之后才做，让选中专家的权重更准确。

**练习 2**：前 3 层为什么改用 hash 表选专家（`ffn_gate_tid2eid`），而不用 router 打分？

**参考答案**：前几层 token 语义还不丰富，router 打分不够稳定；用一张按 token id 预计算好的 hash 表直接查出专家，既省掉 router 的一次矩阵乘，又能保证早期层的路由确定性。代价是损失了一点自适应性，但仅限前 3 层（`n_hash_layer = 3`）。

---

### 4.3 单层数据流

#### 4.3.1 概念说明

把 4.1 的注意力和 4.2 的 MoE 串起来，就是一层 DeepSeek V4 的完整前向。但 DeepSeek V4 在「怎么串」上有一个与经典 Transformer 不同的设计：**HC（Hyper-Connection，超连接）**。

经典 Transformer 的残差是一条单线：

\[
x_{\text{out}} = x + \text{Sublayer}(\text{Norm}(x))
\]

DeepSeek V4 把它换成 **4 条并行流**（`n_hc = 4`），每条流都是 4096 维。每个子层（注意力或 FFN）的输入不再是单条残差，而是从这 4 条流里**学到一个混合**抽出来的；子层算完后，结果再按一个学到的「组合矩阵」**重新分配回** 4 条流。MODEL_CARD 把这套机制称为 **mHC（Manifold-Constrained Hyper-Connections，流形约束超连接）**，目的是让信号在 43 层之间传播得更稳。

每层的两个子层（attention、ffn）各自做一次「HC 抽出 → 子层 → HC 回写」，所以一层里有两次 HC 往返。

#### 4.3.2 核心流程

一层的前向（输入是 4 条 HC 流 `inp_hc`，输出也是 4 条 HC 流 `out_hc`）：

```
inp_hc (4 × 4096)
  │
  ├─【注意力子层】
  │    1. hc_pre：从 4 条流抽出 1 条 attention 输入 attn_cur，并算出 post/comb 混合系数
  │    2. RMSNorm(attn_norm)
  │    3. Q 两段式投影（MLA）+ KV 压缩投影（MLA）
  │    4. 解耦 RoPE（仅前 64 维）
  │    5. 多头注意力（共享 KV + attention sinks）
  │    6. 分组输出投影（8 组 → 1024 → 4096）
  │    7. hc_post：把结果按 post/comb 回写进 4 条流 → after_attn_hc
  │
  ├─【FFN 子层】
  │    8. hc_pre：从 4 条流抽出 1 条 ffn 输入 ffn_cur
  │    9. RMSNorm(ffn_norm)
  │    10. routed MoE（router → Top-6 → SwiGLU → 加权累加）
  │    11. shared expert（SwiGLU）
  │    12. ffn_out = routed + shared（可选：方向性引导投影）
  │    13. hc_post：回写进 4 条流 → out_hc
  │
  └─ out_hc (4 × 4096)  → 喂给下一层
```

43 层跑完后，4 条 HC 流再经过一个 `output_hc_head`（把 4 条流加权合并回单条 4096 维）、RMSNorm、词表投影，就得到 logits。

#### 4.3.3 源码精读

**(1) 一层的完整 CPU 前向：`layer_forward_self_one`**

这是本讲最重要的一段代码——它把 4.1 和 4.2 的每一步按真实顺序串起来。读这一段就等于读懂了「一层 DeepSeek V4」：

[ds4.c:10077-10130](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L10077-L10130) —— `layer_forward_self_one`，单 token 单层的 CPU 参考前向。

```c
// —— 注意力子层 ——
hc_pre_from_state_one(model, layer->hc_attn_fn, layer->hc_attn_scale,
                      layer->hc_attn_base, attn_residual, attn_cur, post, comb);
layer_attn_norm_one(attn_norm, model, layer, attn_cur);
layer_q_projection_normed_one(model, layer, attn_norm, q);    // MLA Q
layer_kv_projection_normed_one(model, layer, attn_norm, kv);  // MLA KV
rope_tail_layer_inplace(q, DS4_N_HEAD, DS4_N_HEAD_DIM, DS4_N_ROT, pos, il, false);  // 解耦 RoPE
rope_tail_layer_inplace(kv, DS4_N_HEAD_KV, DS4_N_HEAD_DIM, DS4_N_ROT, pos, il, false);
dsv4_fp8_kv_quantize_row_inplace_cpu(kv, DS4_N_HEAD_DIM, DS4_N_ROT);  // FP8 量化进缓存
layer_attention_one(heads, model, layer, q, kv);              // 共享 KV 注意力
rope_tail_layer_inplace(heads, DS4_N_HEAD, DS4_N_HEAD_DIM, DS4_N_ROT, pos, il, true); // 反旋转
layer_grouped_out_one(attn_out, model, layer, heads);         // 分组输出投影
hc_post_one(after_attn_hc, attn_out, attn_residual, post, comb, DS4_N_EMBD, n_hc);

// —— FFN 子层 ——
layer_ffn_one(out_hc, model, layer, after_attn_hc, il, token, NULL, 0.0f, false);
```

几个要点：

- `rope_tail_layer_inplace` 的最后一个参数 `false`/`true` 分别表示正向旋转与**反向旋转**。注意力输出后再做一次反旋转，是 MLA 解耦 RoPE 设计的一部分。
- `dsv4_fp8_kv_quantize_row_inplace_cpu` 把要存进缓存的 KV 量化成 FP8——这是 KV 缓存省内存的又一层手段（u4-l2 会展开）。
- FFN 子层整个被封装进 `layer_ffn_one`（见 4.2.3 第 6 点）。

**(2) HC 的「抽出」与「回写」**

`hc_pre` 从 4 条流里抽出子层要用的那一条输入，并算出两组系数：`post`（新结果注入各流的强度）和 `comb`（4×4 的流间组合矩阵）。`hc_post` 再用它们把子层输出回写进 4 条流：

[ds4.c:6463-6480](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L6463-L6480) —— `hc_pre_from_state_one`，从 4 条 HC 流抽出子层输入并算混合系数。

[ds4.c:6512-6531](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L6512-L6531) —— `hc_post_one`，把子层输出按 post/comb 回写进 4 条流。

```c
// hc_post 的核心：新输出 block_out 注入 + 旧 4 流按 comb 矩阵混合
for (dst in 4) {
    for (d in 4096) {
        float acc = block_out[d] * post[dst];
        for (src in 4) {
            acc += comb[dst + src*n_hc] * residual_hc[src*4096 + d];  // 4x4 组合
        }
        out_hc[dst*4096 + d] = acc;
    }
}
```

这正是 mHC「流形约束超连接」的代码面貌：不是简单的 `x + sublayer(x)`，而是一个学到的 4×4 线性混合。

**(3) 输入嵌入先复制成 4 条流**

最一开始，token 的嵌入向量被**复制 4 份**作为 4 条 HC 流的初值（4 条流一开始内容相同，之后才被各层差异化混合）：

[ds4.c:6504-6508](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L6504-L6508) —— `hc_from_plain_embedding`，把单条嵌入广播成 4 条 HC 流。

**(4) 43 层的循环**

一层算完得到 `next`，交换缓冲区继续下一层，共迭代 `DS4_N_LAYER`（=43）次：

[ds4.c:10141-10149](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L10141-L10149) —— `forward_first_token_cpu`，嵌入 → 43 层循环。

```c
embed_token_f16(model, weights, token, plain);
hc_from_plain_embedding(cur, plain, DS4_N_EMBD, DS4_N_HC);
for (uint32_t il = 0; il < DS4_N_LAYER; il++) {
    layer_forward_self_one(next, model, &weights->layer[il], cur, il, 0, token);
    float *tmp = cur; cur = next; next = tmp;   // 双缓冲交换
}
```

**(5) 压缩比与层的对应关系**

`ds4_expected_layer_compress_ratio` 严格按 MODEL_CARD 的描述给每层分配压缩比（前两层 0、之后偶数层 4、奇数层 128）。这决定了该层要不要绑定 compressor/indexer 那一组额外权重：

[ds4.c:625-644](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L625-L644) —— `ds4_layer_compress_ratio` 与 `ds4_expected_layer_compress_ratio`，逐层压缩比规则。

注意，本讲的注意力计算（4.1）展示的是「一层之内如何打分」，没有展开压缩行/indexer 的细节——那部分属于 KV 缓存如何组织历史，是 u4-l2 的主题。本讲的 `layer_attention_one` 调用时 `n_kv=1`（单 token），屏蔽了多行压缩的复杂度，让你先看清「一层之内的数学」。

#### 4.3.4 代码实践

**实践目标**：把本讲的三部分串起来，亲手画一张单层数据流图，并能在源码里给每个箭头找到对应的函数调用。

**操作步骤**：

1. 打开 [ds4.c:10077-10130](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L10077-L10130)（`layer_forward_self_one`）。
2. 准备一张纸或文本编辑器，按从上到下的顺序，为下列每个函数画一个方框，并用箭头连起来：
   - `hc_pre_from_state_one` → `layer_attn_norm_one` → `layer_q_projection_normed_one` + `layer_kv_projection_normed_one` → `rope_tail_layer_inplace` → `layer_attention_one` → `layer_grouped_out_one` → `hc_post_one`
   - 接着 → `layer_ffn_one`（内部展开为 `hc_pre` → norm → `layer_routed_moe_one` + `layer_shared_ffn_one` → 相加 → `hc_post`）
3. 在每个方框旁边标注它消费的权重字段（如 `attn_q_a`/`attn_q_b`、`ffn_gate_exps`、`ffn_gate_shexp`）。

**需要观察的现象**：

- 注意力子层和 FFN 子层**结构对称**：都是「hc_pre → norm → 计算 → hc_post」，只是中间的「计算」一个用 MLA，一个用 MoE。
- 整层只产生**一个**输出 `out_hc`（4×4096），它同时是下一层的输入。
- 没有任何一处出现「每头独立 KV」或「全部 256 专家都算」——这印证了 4.1 和 4.2 的两个核心节省。

**预期结果**：你得到一张类似 4.3.2 流程框图的图，并且图上每个方框都能点开本讲给出的源码链接对上号。这是「源码阅读型实践」，**待本地验证**的只是可选的真机 trace 输出。

#### 4.3.5 小练习与答案

**练习 1**：如果把 HC 的流数 `n_hc` 从 4 改成 1，数据流会退化成什么？

**参考答案**：退化成经典 Transformer 的单残差流：`hc_pre` 不做混合（只取那一条流），`hc_post` 退化成 `x + sublayer(x)` 的加法残差。模型仍能跑，但失去了 mHC 跨层信号稳定化的好处。

**练习 2**：一层的注意力子层里，`rope_tail_layer_inplace` 被调用了三次（q、kv、heads），最后一次参数是 `true`（反旋转）。为什么要对 attention 输出做反旋转？

**参考答案**：MLA 的解耦 RoPE 只作用在一部分维度上，用以让「带旋转的查询/键」和「不带旋转的压缩潜向量」可分离。注意力计算是在旋转后的空间里做的；为了让后续的输出投影（`attn_output_a/b`）和下一层仍在「未旋转的原空间」里工作，需要把注意力输出反旋转回去。这是 MLA 数学闭环的一部分。

## 5. 综合实践

**任务**：写一份「DeepSeek V4 一层前向」的逐行注释导览，要求每个关键步骤都给出 `ds4.c` 的精确行号链接，并回答以下三个问题。

1. **架构对照**：打开 [MODEL_CARD.md:23-64](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/MODEL_CARD.md#L23-L64)，找到「共享潜 KV / 64 头」与「routed + shared MoE」的官方描述，分别在 `ds4.c` 里指出对应的代码段（提示：`n_head_kv=1`、`layer_attention_rows_one`、`layer_routed_moe_one`、`layer_shared_ffn_one`）。
2. **算一笔账**：用 [ds4.c:180-212](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L180-L212) 的形状常量，计算一个 token 走完一层时：注意力子层激活了几个 4096→? 的矩阵乘？FFN 子层实际激活了几个专家的小 FFN？把两个数字写出来。
3. **画图**：基于 [ds4.c:10077-10130](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L10077-L10130)，画一张「输入 hidden → attention → router → routed+shared experts → 输出」的数据流图，标出 HC 抽出/回写的位置。

**完成标准**：第 1 题每个官方描述都能点到一个代码链接；第 2 题能说出注意力侧大约 4~5 个关键 matvec（q_a、q_b、kv、output_a、output_b）、FFN 侧 6 个 routed + 1 个 shared = 7 个小 FFN；第 3 题的图与本讲 4.3.2 的流程一致。这一步全部基于源码阅读，无需运行模型；若你想在真机上看每层中间向量的统计，可研究服务器 `--trace` 入口（[ds4_server.c:11577](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11577)）和 forward 函数的 `trace` 形参，这部分**待本地验证**。

## 6. 本讲小结

- DeepSeek V4 的一层 = **注意力子层（MLA）+ FFN 子层（MoE）**，两者都包裹在 **HC（4 条并行残差流）** 的「抽出 → 计算 → 回写」里。
- **MLA** 让所有 64 个查询头**共享一个 512 维 KV 潜向量**（`n_head_kv = 1`），把每 token 的 KV 缓存从约 6.5 万维压到 512 维，这是 1M 上下文的前提；Q 用两段式低秩投影，配解耦 RoPE 与 attention sinks。
- **MoE** 用 router（`sqrt(softplus(·))`）从 256 个 routed experts 里 Top-k 选 6 个激活，外加 1 个**永远激活**的 shared expert，最终 `FFN = routed + shared`；前 3 层用 hash 表硬路由。
- 一层的完整顺序在 [ds4.c:10077-10130](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L10077-L10130) 的 `layer_forward_self_one` 里一览无余；43 层循环在 `forward_first_token_cpu`。
- 本讲看的全是 **CPU 参考路径**，它和 Metal/CUDA/ROCm 后端做完全相同的数学，后端只是把同样的步骤向量化/并行化（u5 展开）。
- 量化只压 routed experts（占体积绝大多数但每 token 只用 6 个），shared expert 和所有投影保持高精度——这是架构决定的设计取舍。

## 7. 下一步学习建议

- **u4-l2（KV 缓存设计）**：本讲的注意力只展示了「一层之内如何打分」，但历史 token 的 KV 如何组织成滑动窗口 + 压缩行 + indexer，是下一讲的核心。学完你会真正理解 MODEL_CARD 里 CSA/HCA 的全貌。
- **u4-l3（生成与采样）**：一层跑完得到 logits 之后，如何采样出下一个 token（temperature/top_p/min_p）、如何 argmax，是接在本讲之后的自然一步。
- **u5（GPU 后端）**：本讲的 `layer_forward_self_one` 是 CPU 参考实现；想看同样的数据流如何在 Metal/CUDA 上变成「layer-major 图调度」和「张量常驻设备」，进入 u5。
- **延伸阅读**：动手对照 [MODEL_CARD.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/MODEL_CARD.md) 的 Architecture 一节，把官方术语（CSA/HCA/mHC）和本讲的代码名词（compress_ratio/HC/MLA）一一对应起来。
