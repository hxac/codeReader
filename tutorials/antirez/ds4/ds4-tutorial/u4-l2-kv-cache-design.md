# KV 缓存设计：滑动窗口 + 压缩/indexer

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 DeepSeek V4 一层 Transformer 的 KV 缓存由**哪三块**拼成：raw 滑动窗口、compressor 压缩行、ratio-4 indexer 选择掩码，以及它们各自解决什么问题。
- 理解 **raw sliding-window KV** 为什么是一个固定容量（默认 128 行）的环形缓冲，写满后如何「滑窗」。
- 掌握 **compressor（压缩器）** 如何把每 `ratio` 个 token 压成一行压缩 KV，以及什么叫 **compressor frontier（压缩前沿）**——为什么它必须和行计数器一起被序列化。
- 理解 **ratio-4 indexer（索引器）** 为什么只出现在 ratio-4 层、它如何用 top-k 选择把「每生成一个 token 要打分的压缩行数」从一个随上下文增长的量，压成一个常数。
- 能够解释：**为什么这套设计让 1M 上下文在本地成为可能**（存储靠 compressor 砍倍数，计算靠 indexer 封顶）。

本讲只讲 **KV 缓存的数据结构与生命周期**，对应 ds4 里那份最容易读懂的 **CPU 参考路径**（Metal/CUDA/ROCm 后端做完全相同的数学）。采样在 u4-l3，分块 prefill 的边界细节在 u6-l1，磁盘持久化格式在 u8。

## 2. 前置知识

进入本讲前，请确认你已经理解（这些都在前置讲义建立）：

- **MLA 压缩注意力**（u4-l1）：DeepSeek V4 每层的 KV 被压成一个共享的 512 维潜向量（`n_head_kv=1`，`n_head_dim=512`）。所以本讲里「一行 KV」就是 **512 个 float**，而不是传统注意力里「每个 head 一组 K/V」。这是 KV 缓存能做得这么小的前提。
- **权重绑定**（u3-l2）：推理代码用 `layer->attn_compressor_kv`、`layer->indexer_proj` 这类语义字段直访权重，不再查字符串。compressor 和 indexer 各有一组专属权重。
- **session 与 checkpoint**（u2-l3）：`ds4_session` 是一条可变推理时间线，持有 KV 缓存与 logits；`checkpoint` 是 KV 当前对应到的 token 序列。本讲讲的「KV 缓存」就住在 session 里。
- **每层压缩比可不同**：DeepSeek V4 不是每层都压缩、也不是每层压同样狠。这个 ratio 来自 GGUF 元数据，下面会讲它怎么按层交替取 0 / 4 / 128。

一个贯穿全讲的关键直觉：**KV 缓存的开销 = 存储开销 + 每生成一个 token 的注意力计算开销**。这两笔账要分开算，raw 窗口、compressor、indexer 分别管其中一部分。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ds4.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c) | KV 缓存结构定义、CPU 参考路径的 compressor / indexer / 混合注意力实现，以及 session 序列化（DSV4 payload）。 |
| [ds4.h](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h) | 公共 API：`ds4_engine_layer_compress_ratio`（按层查压缩比）、snapshot 结构体。 |
| [README.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md) | 「Disk KV Cache」一节给出 DSV4 payload 的字节布局，是理解「为什么 raw 只存最后一窗、compressed 要存全部」的权威说明。 |

涉及的关键代码点（本讲会逐个精读）：

- KV 缓存结构体 `ds4_layer_cache` / `ds4_kv_cache`。
- 初始化 `kv_cache_init`、滑窗 `kv_cache_push_raw`、压缩行追加 `kv_cache_push_comp`。
- 压缩器 `compressor_decode_one`、池化 `compressor_pool_decode_state`。
- 混合注意力 `layer_attention_mixed_one`、indexer 选择 `indexer_allowed_decode_one`。
- 按层压缩比 `ds4_expected_layer_compress_ratio` / `ds4_layer_compress_ratio`。

## 4. 核心概念与源码讲解

DeepSeek V4 一层的 KV 缓存，逻辑上是一张「时间线」：每个被处理过的 token 都会在缓存里留下痕迹。但「留下什么痕迹」分三种，对应三个最小模块。

先用一张表建立全局印象（设上下文 `ctx` 远大于 128，ratio-4 层为例）：

| 模块 | 存的是什么 | 容量 / 行数 | 每生成一个 token 是否要打分 | 解决的问题 |
| --- | --- | --- | --- | --- |
| raw 滑动窗口 | 每个 token 的完整 512 维 KV | 固定 128 行（环形） | 是，全部行都参与 | 近期上下文的**精确**记忆 |
| compressor 压缩行 | 每 `ratio` 个 token 压成 1 行 512 维 | `ctx/ratio` 行（跨整条前缀） | ratio-4 层会被 indexer 筛选；ratio-128 层全部参与 | 长期记忆的**存储压缩** |
| ratio-4 indexer | 一组 128 维的小压缩行 + 一个 top-k 选择器 | `ctx/4` 行（128 维） | 用来给压缩行打分，但本身不参与最终注意力 | 把每 token 的**计算量封顶** |

下面逐个拆开。

### 4.1 滑动窗口 KV（raw sliding-window KV ring）

#### 4.1.1 概念说明

最朴素的 KV 缓存就是「把每个历史 token 的 K/V 都存下来」。但 DeepSeek V4 要跑 1M 上下文，1M × 512 维 × float 的 KV 每层就要 2GB，43 层根本放不下。

raw 滑动窗口是最简单也最精确的一块：**只保留最近 128 个 token 的完整 KV**。它的容量固定（`DS4_N_SWA = 128`），与上下文长度无关，所以无论你跑 4K 还是 1M，这块的内存和计算量都不变。它保证了「刚刚说过的内容」永远被逐字精确地注意（dense attention）。

容量固定 + 顺序写入 → 天然适合「环形缓冲（ring buffer）」：写满后不是腾整块内存，而是把最老的一行丢掉、新行接在末尾。

#### 4.1.2 核心流程

每个 token 进入一层时，先算出它的 KV（512 维），然后调用 `kv_cache_push_raw` 把它推进 raw 缓存：

1. 若缓存**未满**（`n_raw < cap_raw`）：直接写在下标 `n_raw` 处，计数器 +1。
2. 若缓存**已满**：用 `memmove` 把全体行向前挪一格（丢掉第 0 行），再把新行写在最后一格。

因为 MLA 把每层 KV 压成单个 512 维向量，这里「一行」就是连续 512 个 float；滑动一格 = 移动 `512 × sizeof(float)` 字节。

#### 4.1.3 源码精读

raw 窗口的状态字段住在每层缓存结构体里：

[ds4.c:8300-8316](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L8300-L8316) 定义 `ds4_layer_cache`：`raw_kv` 是行数组指针，`n_raw` 是当前行数，`cap_raw` 是环形容量；后面那一组 `attn_comp_*` / `index_comp_*` 字段是下一节要讲的 compressor 和 indexer。

整个 session 的 KV 缓存就是「每层一个 `ds4_layer_cache`」的数组，外加一个公用的 `head_dim`：

[ds4.c:8318-8321](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L8318-L8321) 定义 `ds4_kv_cache`，内含 `ds4_layer_cache layer[DS4_MAX_LAYER]`。

初始化时，**每一层**都会分到一个 raw 窗口（无论它的压缩比是多少）：

[ds4.c:8492-8496](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L8492-L8496) 在 `kv_cache_init` 的循环里，先给 `cap_raw = raw_cap`、`raw_kv` 分配 `raw_cap * DS4_N_HEAD_DIM` 个 float，再处理压缩比。`raw_cap` 默认就是 `DS4_N_SWA`（128），由 `ds4_default_raw_cap` 给出（[ds4.c:8323-8328](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L8323-L8328)）。

滑窗的写入逻辑非常短，看注释就能懂：

[ds4.c:8540-8554](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L8540-L8554) —— 注释明确写了「Once full, it slides by one row」；满之前的分支直接追加，满之后的分支 `memmove` 整体前移再写末尾。注意一个细节：写入时每个分量都过了 `f16_to_f32(f32_to_f16(...))`，即 **raw KV 实际以 fp16 精度落盘**（用 float 存储、但取值被量化到 fp16），这是省内存的常用手段。

> 小贴士：为什么是 128？因为 DeepSeek V4 的注意力设计为「最近 128 token 精确注意 + 更早的内容走压缩记忆」。`n_swa=128`（sliding-window attention）写进了 GGUF 元数据 `deepseek4.attention.sliding_window`，并在加载时被校验（见 u3-l1 的配置校验）。

#### 4.1.4 代码实践

**目标**：亲手验证 raw 窗口的「滑窗」行为，理解满 128 行之后老数据被丢弃。

**操作步骤**：

1. 打开 [ds4.c:8540-8554](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L8540-L8554)，确认 `cap_raw` 默认是 128（顺着 `kv_cache_init` → `ds4_default_raw_cap` → `DS4_N_SWA` 追踪）。
2. 想象连续推入 130 个 token：前 128 次走「未满」分支，`n_raw` 一路涨到 128；第 129、130 次走 `memmove` 分支，token 0、1 被挤出。
3. 阅读混合注意力函数 [ds4.c:8872-8876](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L8872-L8876)：注意 raw 行的循环是 **0 到 `n_raw`，无任何跳过**，印证「raw 窗口是 dense（全量）注意力」。

**需要观察的现象 / 预期结果**：

- raw 缓存的内存占用恒为 `128 × 512 × 4 字节 ≈ 256KB/层`，与上下文长度无关。
- 若你在 CPU 后端用 `--inspect` 之外的真实推理跑一段长文本，可以用调试器观察 `cache->layer[il].n_raw` 在前 128 个 token 线性增长、之后恒为 128。
- 上述结论**待本地验证**（具体数值取决于你实际加载的模型变体，但「128 后恒定」由代码结构保证）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `DS4_N_SWA` 改成 64，raw 窗口的内存和精度分别会怎么变？

**参考答案**：每层 raw 内存减半（约 128KB/层）；精度不变（仍是 fp16 落盘的 512 维向量），但模型只能精确注意最近 64 个 token 而非 128——这是模型结构超参，随意改会破坏与官方实现的 logits 对齐，不能单独改。

**练习 2**：raw 窗口用 `memmove` 整体前移来滑窗，每写一个 token 要搬 128 行。为什么这里不担心性能？

**参考答案**：因为「行」只有 512 个 float，128 行 = 64KB 的 `memmove`，对现代 CPU 是纳秒级；而且这只发生在 decode（逐 token 生成）阶段，不是 prefill 的批量热路径。GPU 后端用的是张量视图加偏移，连这 64KB 搬运都省了。

---

### 4.2 压缩 KV 与 compressor frontier

#### 4.2.1 概念说明

raw 窗口只记最近 128 个 token，那 128 个 token **之前**的内容怎么办？答案是把更早的 token **按块压缩**：每 `ratio` 个连续 token，用一个学到的池化算子压成 **1 行**压缩 KV（仍是 512 维）。这块压缩行组成的数组 `attn_comp_kv` 就是「长期记忆」，它跨越整条前缀、不滑窗。

为什么压缩能省内存？因为每 `ratio` 个 token 只留 1 行：

\[ \text{压缩行数} = \left\lceil \frac{\text{已处理 token 数}}{\text{ratio}} \right\rceil \]

ratio 越大，长期记忆越省、也越「模糊」。DeepSeek V4 的折中是**按层交替**用不同 ratio（见 4.2.3），让有的层记得粗（ratio 128）、有的层记得细（ratio 4）。

这里有一个关键概念 **compressor frontier（压缩前沿）**：压缩是「攒够 `ratio` 个 token 才吐出一行」的流式过程。正在攒、但还没攒满的那半窗口状态，就叫 frontier。它很小（`ratio` 行 × 512 维），但**不可丢**——丢了你就没法接着攒下一行。所以做 checkpoint 时，除了已完成的压缩行，还得把 frontier 和「当前攒了几个」一起存下来。

#### 4.2.2 核心流程

每个 token 进入一个「有压缩」（ratio ≠ 0）的层时，`compressor_decode_one` 做这几件事：

1. 把当前 token 的归一化向量 `x` 投影成一对行：`kv_cur`（要进 KV 的内容）和 `sc_cur`（每个维度的「重要性分数」，由 gate 权重给出）。
2. 给分数加上 **APE（绝对位置编码）**：`sc_cur[j] += ape[j][pos % ratio]`。注意位置是「块内位置」`pos % ratio`，因为同一块内的 `ratio` 个 token 共享一套位置编码。
3. 把这一对行写进 frontier 的第 `pos % ratio` 槽（一个 `ratio` 行的滚动小缓冲）。
4. 判断 `should_compress = ((pos + 1) % ratio == 0)`：只有攒满一整块（第 ratio 个 token 落位）才真正压缩。
5. 攒满时，对这 `ratio` 行做**逐维 softmax 池化**，得到 1 行 512 维的压缩 KV，再 RMSNorm、RoPE、FP8 量化，最后 `kv_cache_push_comp` 追加进 `attn_comp_kv`。

逐维 softmax 池化（对维度 `j`，块内有 `ratio` 个 token，分数 `s_r`、内容 `v_r`）：

\[ \text{out}_j = \frac{\sum_{r=0}^{ratio-1} \exp(s_r - s_{\max})\, v_{r,j}}{\sum_{r=0}^{ratio-1} \exp(s_r - s_{\max})}, \qquad s_{\max} = \max_r s_r \]

也就是「这一维度上，谁的分高就多听谁的」，每个维度独立做一次 softmax 加权平均。这比简单取平均/取max保留了更多信息。

ratio-4 层还有个细节：它的 frontier 有 **两条 lane**（一条主 lane、一条副 lane，副 lane 偏移在 `head_dim` 之后），所以池化时主副 lane 一起进 softmax。ratio-128 层只有一条 lane。这个差别在 `compressor_pool_decode_state` 里用 `coff = (ratio==4)?2:1` 控制（[ds4.c:8616](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L8616)）。

#### 4.2.3 源码精读

先看「每层 ratio 是怎么定的」。`ds4_expected_layer_compress_ratio` 给出编译期期望的按层布局：

[ds4.c:630-644](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L630-L644) —— 读这段可以得出布局规律：

- **Flash**：第 0、1 层 ratio = 0（**只有 raw 窗口，不压缩**）；从第 2 层起，**偶数层 ratio = 4，奇数层 ratio = 128**。
- **PRO**：第 0、1 层 ratio = 128；之后同样偶数 4、奇数 128。

也就是说 43 层里：2 层纯 raw、约一半层 ratio-4（细记忆 + indexer）、约一半层 ratio-128（粗记忆）。这个布局不是 ds4 瞎定的，而是从 GGUF 元数据 `deepseek4.attention.compress_ratios` 读出来、并和上面这个期望值**逐层比对**，对不上就 `exit(1)`：

[ds4.c:3796-3830](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L3796-L3830) 是 `validate_compress_ratio_metadata`，把元数据数组填进全局 `g_ds4_compress_ratios[DS4_MAX_LAYER]`；运行时再通过 [ds4.c:625-628](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L625-L628) 的 `ds4_layer_compress_ratio(il)` 按层查。公共 API 还把它暴露给前端：`ds4_engine_layer_compress_ratio`（[ds4.h:155](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L155)）。

压缩缓存的分配在 `kv_cache_init` 里按 ratio 条件进行：

[ds4.c:8498-8522](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L8498-L8522) —— 关键点：

- `ratio != 0` 才分配 `attn_comp_kv`（完成的压缩行，容量 `ctx/ratio + 2`）和 frontier（`attn_state_kv` / `attn_state_score`，大小 `coff*ratio` 行）。
- `ratio == 4` 额外分配 indexer 的压缩缓存 `index_comp_kv`（128 维）和它的 frontier（下一节讲）。
- frontier 的分数 `attn_state_score` 初始化为 `DS4_NEG_INF`（表示「这个槽还没填」），这样池化时未填的槽天然被 softmax 忽略。

压缩器本体（流式更新 + 攒满才吐行）：

[ds4.c:8665-8719](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L8665-L8719) 是 `compressor_decode_one` 的核心。重点读这几行：

- `pos_mod = pos % ratio`、`row = ratio==4 ? ratio+pos_mod : pos_mod`：算出当前 token 写进 frontier 的哪个槽（ratio-4 的主 lane 在后半区）。
- `should_compress = ((pos+1) % ratio == 0)`：**攒满才压缩**的判定。
- 投影 `kv_cur`/`sc_cur` → 加 APE → 写进 frontier。
- `if (!should_compress) return false;`：没攒满就直接返回，**不吐压缩行**。
- 攒满才调 `compressor_pool_decode_state` 池化、RMSNorm、RoPE、量化，返回 `true` 让调用方把结果推进 `attn_comp_kv`。

调用方（单 token 注意力）拿到压缩行后的处理：

[ds4.c:9263-9279](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L9263-L9279) 调用 `compressor_decode_one`，仅当它返回 `true`（吐出新压缩行）时才 `kv_cache_push_comp` 追加。`kv_cache_push_comp`（[ds4.c:8556-8561](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L8556-L8561)）是个简单的「按下标追加、越界即 `die`」，和 raw 的滑窗完全不同——**压缩行永不淘汰**，因为后面的 sparse 注意力可能回看任意一行。

最后，关于「frontier 必须随 checkpoint 序列化」，最清楚的说明在 GPU 路径的结构体注释里（数学和 CPU 路径一致）：

[ds4.c:10331-10342](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L10331-L10342) 注释直说：`layer_attn_state_kv/score`（以及 ratio-4 的 `layer_index_state_kv/score`）就是「compressor frontiers for the next compressed row」，做 checkpoint 或部分回退时**必须和行计数器一起快照**。这也是 u2-l3 里「压缩 KV 前沿无法廉价回退、中间改写须重建」的根因。

#### 4.2.4 代码实践

**目标**：从源码确认「压缩行数 = token 数 / ratio」与「frontier 是半窗口状态」，并用 README 的字节布局印证。

**操作步骤**：

1. 在 [ds4.c:8500](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L8500) 读 `comp_cap = ctx_size / ratio + 2`：确认一个 ratio-4 层在 1M 上下文下最多约 25 万行压缩 KV；一个 ratio-128 层约 7812 行。
2. 在 [ds4.c:8681-8683](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L8681-L8683) 读 `pos_mod`、`row`、`should_compress` 三行，画一张「token 0..3 在 ratio=4 层如何轮流落位、第 4 个 token 触发吐行」的时序图。
3. 打开 [README.md:1091-1120](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L1091-L1120)（DSV4 payload 字节布局）：注意头部第 12 字段 `live raw rows serialized below`，以及正文里「For compressed layers: live compressed KV rows **and compressor frontier** tensors」——印证 raw 只存最后一窗、compressed 要存**全部行 + frontier**。

**需要观察的现象 / 预期结果**：

- 你应当能解释：序列化时 raw 行只写 128 行（最后一窗），而 compressed 行要写 `n_comp` 行外加 frontier——因为 raw 注意力是滑窗（老行必然已被挤出），而压缩注意力的 sparse 选择可能命中前缀任意一行。
- 「frontier 随行计数器一起存」这个结论可以直接从 README 的「compressor frontier tensors」一句读出。
- 上述为源码阅读型实践，**无需运行**即能完成。

#### 4.2.5 小练习与答案

**练习 1**：为什么 ratio-128 层的压缩缓存比 ratio-4 层「省」，但记忆更模糊？

**参考答案**：ratio-128 把 128 个 token 压成 1 行，行数只有 ratio-4 的 1/32，存储省 32 倍；但池化窗口更大，单个 token 的影响被稀释得更厉害，所以是「粗粒度长期记忆」。DeepSeek 让两种层交替出现，兼顾省内存与细记忆。

**练习 2**：compressor 的分数 `sc_cur` 是怎么参与池化的？为什么用 softmax 而不是简单平均？

**参考答案**：`sc_cur`（由 gate 权重 + APE 算出）充当「这一 token 在每个维度上的重要性」，池化时按维度做 softmax 加权平均。softmax 让「这一维度上更重要的 token」主导输出，比平均更能保留关键信息——这正是 DeepSeek 用「learned compressor」而非朴素下采样的原因。

---

### 4.3 ratio-4 indexer：把每 token 的注意力计算封顶

#### 4.3.1 概念说明

compressor 解决了「存不下」的问题，但带来一个新问题：ratio-4 层在 1M 上下文下有约 25 万行压缩 KV。如果每生成一个 token 都要对这 25 万行逐一打分做注意力，生成速度会塌掉——**这就是 indexer 要解决的问题**。

indexer（索引器）只在 ratio-4 层出现（ratio-128 层没有，因为它们行数本就少）。它做两件事：

1. **维护一套更小的「索引压缩行」** `index_comp_kv`：和 attention 压缩行用同一个 compressor 机制，但用专属权重（`indexer_compressor_*`）投影到一个**更小的 128 维空间**（`DS4_N_INDEXER_HEAD_DIM = 128`，而 attention 压缩行是 512 维）。
2. **为当前 query 选 top-k 行**：用这套小表示，给全部压缩行打一个廉价分数，挑出 `top_k`（Flash 512 / PRO 1024，即 `DS4_N_INDEXER_TOP_K`）行，生成一个布尔掩码 `comp_allowed[]`。最终 attention 只对被选中的压缩行打分。

效果是：**每生成一个 token，对一个 ratio-4 层的压缩注意力打分行数从 `n_comp`（随上下文增长）变成 `top_k`（常数 512/1024）**。这就是「indexer 进一步压缩」的本质——它压缩的不是存储（压缩行仍全部留着，因为以后别的 token 可能要回看），而是**每个 token 的注意力计算宽度**。

> 这是一种「检索 + 稀疏注意力」的思路：用一个便宜的 retriever 先粗筛，再用昂贵的全注意力精算少数行。和纯 dense attention 比，它牺牲了一点精度（漏选的行完全看不到），换来在超长上下文下可承受的计算量。

#### 4.3.2 核心流程

在一个 ratio-4 层、生成单个 token 时：

1. 先照常推进 raw 窗口、跑 attention compressor（可能吐出一行新的 `attn_comp_kv`）。
2. 再跑 **indexer compressor**（同一函数 `compressor_decode_one`，喂 `indexer_compressor_*` 权重、128 维），可能吐出一行新的 `index_comp_kv`。
3. 调 `indexer_allowed_decode_one`：用当前 query 给所有 `index_comp_kv` 行打分，选 top-k，得到 `comp_allowed[]`。
4. 调 `layer_attention_mixed_one`：对 **raw 全部行 + 被允许的压缩行** 做注意力；未被允许的压缩行打分置 `-inf`，直接跳过。

indexer 的打分公式（对压缩行 `c`，64 个头 `h`，每头 128 维，`relu` 截断负值，头权重 `w_h`）：

\[ \text{score}_c = \sum_{h=0}^{63} w_h \cdot \max\!\bigl(0,\ \langle \text{kv}_c,\ q_h \rangle\bigr), \qquad w_h = \frac{\text{proj}_h(x)}{\sqrt{128 \times 64}} \]

其中 `q_h` 由当前 query 的低秩表示经 `indexer_attn_q_b` 投影 + RoPE + QAT 旋转得到；`w_h` 由当前 token 表示经 `indexer_proj` 投影得到。最终按 `score_c` 从大到小取前 `top_k` 行。

#### 4.3.3 源码精读

ratio-4 层在注意力里同时跑 attention compressor 和 indexer compressor：

[ds4.c:9281-9304](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L9281-L9304) —— 先用 `indexer_compressor_*` 权重和 `DS4_N_INDEXER_HEAD_DIM`（128 维）跑一遍 `compressor_decode_one`，结果推进 `index_comp_kv`（[ds4.c:9295](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L9295)）；紧接着调 `indexer_allowed_decode_one` 得到掩码 `comp_allowed`。

indexer 的 top-k 选择本体：

[ds4.c:9094-9154](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L9094-L9154) —— 重点读：

- [ds4.c:9106-9110](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L9106-L9110)：`top_k = min(DS4_N_INDEXER_TOP_K, n_comp)`；若压缩行数还没到 `top_k`，就**全部允许**（早期上下文 indexer 不起筛选作用）。
- [ds4.c:9118-9135](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L9118-L9135)：算 query（`indexer_attn_q_b` + RoPE + QAT）、算头权重（`indexer_proj`），逐行算 `score_c`（上面那个公式，relu + 加权和）。
- [ds4.c:9138-9148](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L9138-L9148)：朴素 top-k（循环 `top_k` 次、每次扫一遍取最大），把选中的行在 `allowed[]` 里置 `true`。

最终混合注意力如何用这个掩码：

[ds4.c:8877-8885](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L8877-L8885) 是 `layer_attention_mixed_one` 里**压缩行**那段循环——注意 raw 行那段循环（[ds4.c:8872-8876](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L8872-L8876)）没有掩码，全部参与；压缩行这段才有 `if (comp_allowed && !comp_allowed[r]) { score = -inf; continue; }`。这正对应「raw 全量 + compressed 稀疏」的设计。还值得注意的是 [ds4.c:8861-8862](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L8861-L8862) 引入的 **attention sink**：`max_score` 初值取 `sinks[h]`，`denom` 里也加了一项 `exp(sinks[h]-max)`，这是 DeepSeek V4 注意力的稳定技巧，和 indexer 无关但出现在同一函数里。

最后回到存储：indexer 的压缩缓存容量与 attention 压缩缓存相同（都是 `ctx/4 + 2`），只是每行从 512 维降到 128 维，所以 indexer 缓存约为 attention 压缩缓存的 1/4 体积。README 指出在 1M 上下文下「compressed indexer alone will be like 22GB」（[README.md:825-829](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L825-L829)），是 KV 总开销里的大头——这部分是「为了让生成在 1M 下可承受」必须付的存储代价。

#### 4.3.4 代码实践（对应总实践任务）

**目标**：解释 **indexer 相比 compressor 进一步压缩了什么，以及为什么这对 1M 上下文至关重要**。

**操作步骤**：

1. 对照阅读 compressor（[ds4.c:8665-8719](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L8665-L8719)）与 indexer（[ds4.c:9094-9154](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L9094-L9154)）：注意 indexer 复用了同一个 `compressor_decode_one` 来产 `index_comp_kv`，但额外多了一步「打分 + top-k」。
2. 阅读 README 的 DSV4 payload 段（[README.md:1091-1120](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L1091-L1120）和 [README.md:825-829](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L825-L829)）：注意 indexer 的压缩行（`ratio-4 indexer row counts`、`indexer compressed rows and indexer frontier`）是单独存储的一段。
3. 写下你的结论（见下方「预期结果」）。

**需要观察的现象 / 预期结果**（这是本讲的「标准答案」）：

- **compressor 压缩的是「时间轴 / 存储」**：每 `ratio` 个 token → 1 行，把 KV 行数（从而存储）砍掉一个倍数。它产出的是**密集的长期记忆**（attention 仍要对所有压缩行打分）。
- **indexer 压缩的是「注意力宽度 / 计算」**：在所有压缩行里只挑 `top_k` 行真正参与注意力，把「每生成一个 token 对该层要打的分行数」从 `n_comp`（随上下文增长，1M 下 25 万）封顶到 `top_k`（常数 512/1024）。它产出的是一个**稀疏选择掩码**，存储并不减少（压缩行仍全留）。
- **为什么对 1M 至关重要**：在 1M 上下文下，ratio-4 层有约 25 万行压缩 KV。若没有 indexer，每生成一个 token 都要对 25 万行逐一打分，decode 速度会随上下文线性恶化、最终不可用。indexer 把这个成本变成常数，**才让「在 1M 上下文里边记边生成」在本地硬件上可行**。存储侧则由 compressor 的按 ratio 折叠 + raw 的固定窗口共同压住（README 实测 1M 约 26GB，其中 indexer 压缩行约 22GB）。
- 一句话区分：**compressor 让你存得下，indexer 让你算得动**。

#### 4.3.5 小练习与答案

**练习 1**：ratio-128 层为什么没有 indexer？

**参考答案**：ratio-128 层在 1M 下只有约 7812 行压缩 KV，对它们逐一打分的成本可接受，没必要再加一层 retriever；而且 ratio-128 是「粗粒度长期记忆」，更倾向于让所有行都参与注意力以保证覆盖。indexer 专门服务 ratio-4 这种「行数多、但每行粒度细」的层。

**练习 2**：indexer 选 top-k 时，如果某个真正相关的压缩行没被选中，会发生什么？

**参考答案**：那行在该 token 的注意力里被置 `-inf`、完全不参与，相当于模型「看不见」它。这是稀疏注意力为换取计算效率付出的精度代价。DeepSeek 用一个学到的 retriever（`indexer_attn_q_b` + `indexer_proj`）来尽量选准，并在前缀较短（`n_comp <= top_k`）时直接全选以避免漏选。

**练习 3**：序列化一个 ratio-4 层的 checkpoint 时，indexer 相关的状态要存哪几样？

**参考答案**：三样——(1) 已完成的 indexer 压缩行 `index_comp_kv[0..n_index_comp]`；(2) indexer 的 frontier（`index_state_kv` / `index_state_score`）；(3) 行计数器 `n_index_comp`。对应 README DSV4 payload 里的「ratio-4 indexer row counts」+「indexer compressed rows and indexer frontier tensors」。

## 5. 综合实践

把三个模块串起来，完成下面这个「源码追踪 + 推理」小任务。

**背景**：假设你在 ratio-4 的第 10 层（Flash），上下文已经 prefill 到第 1,000,000 个 token，现在要生成第 1,000,001 个 token。请回答：

1. **raw 窗口**此刻存的是哪 128 个 token？写出 `n_raw` 的值和它在 `layer_attention_mixed_one` 里的打分方式（是否全量）。
2. **attention compressor** 此刻大约有多少行 `attn_comp_kv`？这些行会全部参与注意力吗？为什么？
3. **indexer** 在这一步会做什么？它产生的 `comp_allowed[]` 长度大约是多少、其中 `true` 的个数是多少？最终这个 token 在该层的「有效注意力行数」大约是多少？

**操作建议**：

- 用 [ds4.c:8492-8522](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L8492-L8522) 推各缓存的容量；
- 用 [ds4.c:8851-8911](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L8851-L8911) 推注意力打分行数；
- 用 [ds4.c:9106-9110](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L9106-L9110) 推 top-k 个数。

**参考答案**：

1. raw 窗口存第 999,873 ~ 1,000,000 这 128 个 token；`n_raw = 128`；在 `layer_attention_mixed_one` 里 raw 行全量打分（无掩码），贡献 128 行。
2. attention compressor 约 `1,000,000 / 4 = 250,000` 行；这些行**不会**全部参与注意力——会被 indexer 的掩码筛掉大部分。
3. indexer 给约 250,000 行打分，`comp_allowed[]` 长度约 250,000，其中 `true` 的个数 = `DS4_N_INDEXER_TOP_K`（Flash 为 512）。最终有效注意力行数 ≈ 128（raw）+ 512（被选中的压缩行）= **640 行**，与上下文长度无关——这正是 1M 上下文下生成仍可承受的根本原因。

> 进阶：你可以把这套追踪推广到 ratio-128 层（第 4.3.5 练习 1 的结论：无 indexer，全部约 7812 行参与注意力），体会两种层在「计算量」上的差异。

## 6. 本讲小结

- DeepSeek V4 一层的 KV 缓存由三块拼成：**raw 滑动窗口**（每层都有，固定 128 行，dense 注意力）、**compressor 压缩行**（ratio ≠ 0 的层有，跨整条前缀、不淘汰）、**ratio-4 indexer**（仅 ratio-4 层有，稀疏 top-k 选择）。
- **raw 滑动窗口**是个 128 行的环形缓冲，写满后整体前移一格；它存最近 128 token 的精确 KV（fp16 落盘），内存与上下文长度无关。
- **compressor** 把每 `ratio` 个 token 流式压成 1 行（逐维 softmax 池化 + RMSNorm + RoPE + FP8），只在攒满一整块时才吐行；正在攒的半窗口状态叫 **frontier**，必须随行计数器一起序列化。
- 按层 ratio 来自 GGUF 元数据并被严格校验：Flash 第 0/1 层 ratio 0，之后偶数 4 奇数 128；PRO 第 0/1 层 ratio 128，之后同样偶数 4 奇数 128。
- **ratio-4 indexer** 用一套 128 维的小压缩行给所有压缩行打廉价分、选 top-k（Flash 512 / PRO 1024），把每 token 的压缩注意力打分行数从「随上下文增长」封顶为常数；它压缩的是**计算**而非存储。
- 一句话：**compressor 让你存得下长上下文，indexer 让你在长上下文里算得动**；二者加上固定容量的 raw 窗口，共同支撑 1M 上下文（README 实测约 26GB）。

## 7. 下一步学习建议

- **u4-l3（生成与采样）**：本讲只到「一层注意力算完得到 heads」，下一步看 logits 如何变成下一个 token——temperature / top_p / min_p 过滤与 argmax。
- **u6-l1（分块 prefill）**：本讲的 compressor frontier 在 prefill 跨 chunk 时如何保持「绝对边界」、为什么改 `DS4_METAL_PREFILL_CHUNK` 会改变 KV checkpoint 路径，是进阶话题。
- **u8-l1 / u8-l3（磁盘 KV 与 DSV4 序列化）**：本讲多次提到的「frontier 必须随 checkpoint 序列化」，其落盘格式就在这里展开——13 个 u32 头、逐层 raw/compressed/indexer 行与 frontier。
- **u5（GPU 后端）**：本讲用的是 CPU 参考路径；Metal/CUDA/ROCm 后端做相同数学，但 raw 环用张量视图、compressor/indexer 在 GPU 内核里跑，是「同算法不同工程」的好对照。
