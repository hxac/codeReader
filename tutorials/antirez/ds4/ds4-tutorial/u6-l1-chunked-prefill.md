# 分块 prefill 主路径

## 1. 本讲目标

本讲是「高级推理路径」单元的第一讲。在前面的单元里，你已经知道了 prefill（一次性把提示喂进 KV 缓存）和 decode（自回归生成）的区别（u1-l5），也知道了 Metal 后端用「layer-major 图调度」把一次 prefill 录进一个 command buffer（u5-l2）。但那里只讲了「一层之内怎么算」，没有回答一个工程上非常关键的问题：

> 当提示有 3 万个 token（远超一个 chunk）时，ds4 怎么把它喂进去？而且为什么改一个环境变量 `DS4_METAL_PREFILL_CHUNK`，不仅会改变速度，还会改变 **KV checkpoint / logit 路径**？

学完本讲你应该能：

1. 说清楚一个长 prompt 是怎么被切成固定大小的 chunk、每个 chunk 又怎么对齐到「绝对边界」的。
2. 说清楚为什么所有 chunk 能复用同一张「layer-major 推理图」，而不必为长 prompt 单独构造巨大的图。
3. 说清楚 chunk 边界为什么必须保持「绝对」，以及这如何决定了 KV checkpoint、logit 写出、磁盘冷存的时机。
4. 解释 `DS4_METAL_PREFILL_CHUNK` 这个旋钮调大调小会发生什么，以及为什么官方测试向量必须把它钉死在 2048。

## 2. 前置知识

本讲默认你已经掌握以下概念（来自前面的讲义），这里只用一句话回顾：

- **prefill 与 decode**（u1-l5）：prefill 是把一整段提示一次性填进 KV 缓存，决定「首 token 延迟」；decode 是逐 token 自回归生成，决定「生成速度」。两者走的是不同的图。
- **layer-major 图**（u5-l2）：Metal 后端把「一个 chunk 的所有 token」依次穿过第 0 层、第 1 层……直到 output head，整个流程录进一个 command buffer。一次 chunk = 一个 command buffer = 全部 43 层。
- **KV 缓存的三段结构**（u4-l2）：每层 KV 由 `raw` 滑动窗口（最近 128 token 的精确 KV）、`compressor`（按 ratio 压缩的长期记忆行）、`indexer`（ratio-4 层的打分小行）组成。compressor 攒满 `ratio` 个 token 才吐一行，这个「正在攒的半窗口」叫 **compressor frontier（压缩前沿）**，它不能廉价回退。
- **session 同步与前缀复用**（u2-l3）：`ds4_session_sync` 当新 prompt 是旧 checkpoint 的前缀时只 prefill 后缀；后缀 ≥ 4 token 走「续 prefill 图」，否则逐 token decode。
- **checkpoint**（u2-l3）：session 用 `checkpoint`（KV 当前对应的 token 序列）+ `checkpoint_valid` 刻画状态，是服务器能否落盘复用 KV 的依据。

一个贯穿全讲的直觉：**chunk 不是 UI 进度条，它是一道「闸」**。在 ds4 里，chunk 边界同时是「推理图的提交点」「KV checkpoint 的推进点」「logit 的写出点」「磁盘冷存的对齐点」。所以改 chunk 大小，改的不是表面上的进度粒度，而是整条「KV 与 logit 的落地路径」。

## 3. 本讲源码地图

本讲只涉及两个文件，但会精确到行：

| 文件 | 作用 |
| --- | --- |
| `ds4.c` | 引擎核心。chunk 大小的决定函数、chunk 循环、layer-major 图、session 同步里的 checkpoint 推进，全在这里。 |
| `README.md` | `Benchmarking` 段说明了默认 chunk、`DS4_METAL_PREFILL_CHUNK` 旋钮与「改 chunk 会改 KV checkpoint/logit 路径」这一官方结论；`Disk KV Cache` 段说明了冷存的 chunk 对齐策略。 |

辅助参考（不在本讲展开，但会点到）：`tests/test-vectors/README.md` 说明了官方向量为何要把 chunk 钉死在 2048。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**chunk 切分与对齐**、**layer-major 图复用**、**绝对边界保持**。三者层层递进，最后合起来回答实践任务里那个问题。

### 4.1 chunk 切分与对齐

#### 4.1.1 概念说明

「prefill 一个 3 万 token 的提示」最朴素的实现，是把 3 万个 token 当成一批，一次穿完整个 43 层网络。但这会有两个问题：

1. **显存爆掉**：attention 在一个 chunk 内要做「chunk 中每个 token 看它前面所有 token」的计算，中间张量的体积随 chunk 长度增长。一个 3 万 token 的巨型 chunk 会要求一块与之匹配的临时显存。
2. **图无法复用**：每条不同长度的 prompt 都要构造一张不同大小的图，编译/调度开销大。

ds4 的做法是 **分块（chunked）prefill**：把长 prompt 切成固定大小（默认 4096 token）的小块，**每块独立跑一次完整的 layer-major 前向**，块与块之间共享同一份持久 KV 缓存。这样：

- 单块显存开销被 `chunk_cap` 封顶，与 prompt 总长无关。
- 所有块复用同一张「最长为 chunk_cap」的图。

关键在于「固定大小」不是随便切的——它必须**对齐到绝对边界**，否则压缩窗口的行最终化顺序会和「冷启动一次性 prefill」不一致，导致同一个 prompt 走不同路径得到细微不同的 KV。这一点在 4.3 展开。

#### 4.1.2 核心流程

先用伪代码画出 chunk 大小是怎么定下来的（对应 `ds4_prefill_cap_for_prompt`）：

```
ds4_prefill_cap_for_prompt(prompt_len, requested_chunk):
    if prompt_len <= 0: return 1
    cap = prompt_len                      # 默认：整段当一块
    if requested_chunk != 0:
        cap = requested_chunk             # 调用方显式指定优先
    else:
        env = DS4_METAL_PREFILL_CHUNK
        if env 有效且 > 0:
            cap = env                     # 环境变量其次
        elif prompt_len > 4096:
            cap = (PRO ? 8192 : 4096)     # 都没给：长 prompt 用默认档
    cap = clamp(cap, 1, prompt_len)
    return cap
```

再看真正的 chunk 循环（对应 `metal_graph_prefill_chunked_range`），核心是**对齐**那一步：

```
chunk_cap = g->prefill_cap
if start != 0 and chunk_cap > raw_cap:    # 续 prefill：块不能超过滑动窗口
    chunk_cap = raw_cap
for pos0 = start; pos0 < end; :
    local_cap = chunk_cap
    if start != 0 and prefill_cap != 0:
        mod = pos0 % prefill_cap          # 当前位置离上一个绝对边界的偏移
        if mod != 0:
            to_boundary = prefill_cap - mod
            if to_boundary < local_cap:
                local_cap = to_boundary   # 砍到下一个绝对边界
    chunk = min(end - pos0, local_cap)    # 本块长度
    跑一次 layer-major 前向(pos0, chunk)
    pos0 = pos0 + chunk
```

那个 `mod = pos0 % prefill_cap` 是全讲的灵魂：**续 prefill 的块，会被砍到下一个 `prefill_cap` 的整数倍位置**，保证「冷启动 prompt 在位置 4096 切一刀，续 prefill 也在位置 4096 切一刀」。这就是「绝对边界对齐」的字面含义。

#### 4.1.3 源码精读

先看 chunk 大小怎么决定：

[ds4.c:8330-8354](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L8330-L8354) — `ds4_prefill_cap_for_prompt`。优先级是「调用方 `requested_chunk` > 环境变量 `DS4_METAL_PREFILL_CHUNK` > 默认档」。注意两个细节：环境变量 `v <= 0` 时直接 `return cap`（即返回 `prompt_len`，等于「整段当一块」），这正是 README 说的 `DS4_METAL_PREFILL_CHUNK=0` 含义；默认档只在 `prompt_len > 4096` 时才启用 PRO=8192/Flash=4096，短 prompt 根本不切。

再看真正的 chunk 循环，重点是开头两行对 `chunk_cap` 的钳制：

[ds4.c:20997-20999](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L20997-L20999) — `chunk_cap = g->prefill_cap`，但当 `start != 0`（即这是「续 prefill 一段后缀」而非「冷启动」）时，把 `chunk_cap` 砍到 `g->raw_cap`（滑动窗口大小）。直觉：续 prefill 只需要把 raw 窗口填满并往下压压缩行，没必要再用一整块 4096 那么大的临时显存。

然后是对齐循环本体：

[ds4.c:21012-21027](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L21012-L21027) — `for (pos0 = start; pos0 < end; )` 循环。其中 [21019-21025](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L21019-L21025) 就是上面伪代码里的「砍到绝对边界」：`mod = pos0 % g->prefill_cap`，若不在边界上，本块最多跑到 `to_boundary = prefill_cap - mod`。最后一行 `chunk = remaining < local_cap ? remaining : local_cap` 处理「尾巴块不足一个 cap」的情况。

最后，这个函数头上有一段极其重要的注释，点明了「绝对边界对齐」的目的：

[ds4.c:20946-20953](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L20946-L20953) — 「续 prefill 的块对齐到与冷启动 prompt 相同的绝对 `prefill-cap` 边界，**这样压缩窗口与行的最终化在缓存前缀之后遵循同样的时间表**」。这句话直接预告了 4.3 要讲的「边界保持」。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「chunk 大小的决定优先级」与「续 prefill 的对齐砍切」。

**操作步骤**（源码阅读型，无需 GPU）：

1. 打开 `ds4.c:8330`，对照本讲伪代码，回答：
   - 若用户没设环境变量、也没传 `requested_chunk`，一个 3000 token 的 prompt 会得到 `cap` 是多少？（提示：看 `prompt_len > 4096` 这个条件。）
   - 若设了 `DS4_METAL_PREFILL_CHUNK=0`，函数返回什么？这意味着切几块？
2. 打开 `ds4.c:21012` 的循环，假设冷启动 `prefill_cap=4096`、prompt=10000 token，手算：
   - 第 1 块 `[0, 4096)`、第 2 块 `[4096, 8192)`、第 3 块 `[8192, 10000)`。每块都恰好在 `prefill_cap` 整数倍处起切吗？
3. 再假设这是「续 prefill」，`start=5000`、`prefill_cap=4096`：第一块的 `mod = 5000 % 4096 = 904`，所以第一块会被砍到 `4096 - 904 = 3192` 长，即跑到位置 `8192`（下一个 4096 整数倍）。

**需要观察的现象**：续 prefill 的第一块通常比 `chunk_cap` 短，因为它要先「追平」到绝对边界；从第二块起才恢复满块。

**预期结果**：续 prefill 的块边界集合 `{8192, 12288, ...}` 永远是 `prefill_cap` 的整数倍，且这个集合与「冷启动整段 prefill」的块边界集合**完全相同**。这正是「绝对边界」的含义。

> 待本地验证：第 3 步的手算结果。如果你有 GPU 机器，可设 `DS4_METAL_GRAPH_PREFILL_PROFILE=1`（见 [ds4.c:21001](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L21001)）跑一次长 prompt，从 stderr 的 `gpu chunked prefill start=... chunk=...` 行核对 chunk 长度。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ds4_prefill_cap_for_prompt` 在 `requested_chunk` 和环境变量都没给时，要用 `prompt_len > 4096` 作为是否切分的门槛，而不是无脑切？

**答案**：短 prompt（≤4096）一块就能塞进 layer-major 图的临时显存，且少一次「块间同步」开销；只有长 prompt 才值得付出切分与对齐的成本。门槛 4096 与默认 `prefill_cap` 对齐，使「刚好 4096 的 prompt」走单块快路径。

**练习 2**：续 prefill 时，为什么要把 `chunk_cap` 砍到 `raw_cap`（[ds4.c:20998](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L20998)）？

**答案**：续 prefill 是在已有 KV 前缀上追加一小段后缀。raw 滑动窗口只有 128 行，suffix 的 attention 临时张量只与 raw 窗口 + 已压缩行交互，用一整块 4096 那么大的临时显存是浪费；用 `raw_cap`（128）封顶既够用又省显存。

---

### 4.2 layer-major 图复用

#### 4.2.1 概念说明

切完块，下一个问题是：每个 chunk 怎么算？答案就是 u5-l2 讲过的 **layer-major 图**——「一个 chunk 的 token 先全穿过第 0 层，再全穿过第 1 层，……最后到 output head」。本讲不重复一层的内部数学，只讲一个长 prompt 的多个 chunk 如何**复用同一张图**。

复用的前提是这张图是「range-capable（区间能力）」的：它的入口接受 `start` 和 `n_tokens` 两个参数，能处理 `[start, start+n_tokens)` 这段任意区间，只要 `n_tokens ≤ prefill_cap`。于是 chunk 循环每轮只要换 `start` 和 `chunk` 两个实参就能复跑同一张图，不必为每个 chunk 重新构造或编译图。

为什么不能反过来「token-major」（一个 token 穿完全部 43 层，再换下一个 token）？因为那样长 prompt 就退化成「一个 token 一次图步」的慢循环，正好是 ds4 想避免的。源码注释把这一点说得很直白。

#### 4.2.2 核心流程

一次长 prompt 的 prefill 在图层面的流程：

```
对每个 chunk [pos0, pos0+chunk):
    metal_graph_prefill_layer_major(start=pos0, n_tokens=chunk, logits=?)
        ├── 上传本 chunk 的 prompt token 子段
        ├── warmup 本 chunk 相关内核
        ├── 第 0 层: attention + ffn  （复用同一组常驻张量）
        ├── 第 1 层: attention + ffn
        ├── ...
        ├── 第 42 层: attention + ffn
        └── output head → logits（仅本 chunk 是最后一块时才写）
```

关键复用点：

- **常驻张量**：`batch_*` 系列张量（attention、ffn、router 等的中间结果）在图初始化时按 `prefill_cap` 大小分配一次，所有 chunk 共用，不会每块重建。
- **图结构**：每个 chunk 走的是「同一份算子连接」，只是 `start`/`n_tokens` 不同，dispatch 的线程网格随 `chunk` 缩放。

#### 4.2.3 源码精读

先看图里 batch 张量区那段经典注释，它定义了「layer-major」：

[ds4.c:10427-10430](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L10427-L10430) — 「Prefill 是 layer-major：一个 chunk 的 prompt token 先穿过第 0 层、再第 1 层……更新同一份 decode 也在用的持久缓存。把它和 decode 分开，**避免对长 prompt 跑一串单 token 图步的慢循环**。」这一句同时解释了「为什么是 layer-major」和「为什么和 decode 分图」。

再看 `metal_graph_prefill_layer_major` 的入口校验，它就是「range-capable 图」的合同：

[ds4.c:20310-20324](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L20310-L20324) — 函数签名带 `start` 和 `n_tokens`，第一行 `if (n_tokens == 0 || n_tokens > g->prefill_cap) return false` 就是复用的硬约束：**只要块长不超过 `prefill_cap`，同一张图就能处理任意区间**。随后 `metal_graph_upload_prompt_tokens(g->prefill_tokens, prompt, start, n_tokens)` 把本块对应的 token 子段上传到那块被所有 chunk 共用的 `prefill_tokens` 张量。

然后回到 chunk 循环里对它的调用：

[ds4.c:21029-21039](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L21029-L21039) — 每轮循环调用一次 `metal_graph_prefill_layer_major`，传入本块的 `pos0` 和 `chunk`。注意 `chunk_logits` 的取值——这是本讲的另一个重点，留在 4.3 讲。

最后看 README 对「复用同一张图」的官方表述：

[README.md:575-577](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L575-L577) — 「Chunked Metal prefill 对每个 chunk 复用同一张 range-capable 的 layer-major 图，保持绝对的 compressor/indexer 边界，**同时避免了旧的 per-layer chunk dispatch 路径**。」这说明现在的设计是「一张图复用」取代了历史上「按层切分 dispatch」的旧路径。

#### 4.2.4 代码实践

**实践目标**：确认 batch 张量是按 `prefill_cap` 一次性分配、被所有 chunk 共用的。

**操作步骤**（源码阅读型）：

1. 在 `ds4.c` 中定位 [ds4.c:10431-10479](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L10431-L10479) 这片 `batch_*` 张量字段（attention 的 `batch_attn_*`、ffn 的 `batch_ffn_*`、router 的 `batch_router_*`、routed expert 的 `batch_routed_*`）。
2. 用 `Grep` 搜这些字段在图初始化函数里的 `alloc`/分配调用，确认它们的大小由 `prefill_cap` 决定，而不是由某个具体 chunk 长度决定。
3. 对照 [ds4.c:20329](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L20329) 的 `metal_graph_upload_prompt_tokens(g->prefill_tokens, prompt, start, n_tokens)`，确认每次只是往同一块 `prefill_tokens` 张量里**覆盖写入**本块的 token 子段，而不是新分配。

**需要观察的现象**：所有 `batch_*` 中间张量在图生命期内只分配一次；每个 chunk 跑完不会 free/realloc 它们。

**预期结果**：图初始化成本与 chunk 数量无关；chunk 循环只付出「上传 token + dispatch 算子」的边际成本。这正是「复用同一张图」省下的开销。

#### 4.2.5 小练习与答案

**练习 1**：`metal_graph_prefill_layer_major` 的入口为什么必须有 `n_tokens <= g->prefill_cap` 这条硬约束（[ds4.c:20322](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L20322)）？

**答案**：因为所有 `batch_*` 中间张量是按 `prefill_cap` 的大小分配的。一个超过 `prefill_cap` 的块会写出这些张量的边界，破坏显存安全。这条约束也正是「长 prompt 必须切块」的根本原因——单块不能超 cap。

**练习 2**：如果有人想「优化」成 token-major（一个 token 穿完 43 层再换下一个），从注释看会损失什么？

**答案**：会退化成「长 prompt = 一串单 token 图步的慢循环」（[ds4.c:10427-10430](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L10427-L10430)）。layer-major 把一个 chunk 内的 43 层算子录进同一 command buffer，GPU 能连续调度、隐藏延迟；token-major 则每 token 都要完整走一遍图调度，等于把 prefill 退化成 decode 的速度。

---

### 4.3 绝对 compressor/indexer 边界保持

#### 4.3.1 概念说明

前两模块讲了「怎么切、怎么算」。本模块讲为什么**切的位置不能乱动**——这是回答实践任务（「为什么改 chunk 会改 KV checkpoint/logit 路径」）的钥匙。

回忆 u4-l2：compressor 每 `ratio` 个 token 才吐一行压缩 KV，正在攒的那 `ratio` 个 token 是「半窗口」，叫 compressor frontier，**它没有廉价回退**。这意味着：compressor 在「token 序列的哪个绝对位置」吐行，决定了这行的内容。如果冷启动 prompt 在位置 4096 吐一行、续 prefill 却在位置 4096 落在某个块的中间，两边的半窗口边界对不齐，就会得到细微不同的 KV。

所以 ds4 强制：**所有 chunk 边界都对齐到 `prefill_cap` 的整数倍**（4.1 的 `mod` 砍切就是为此）。这样无论 prompt 是「冷启动一次跑完」还是「先跑前缀、再续 prefill 后缀」，chunk 边界集合永远相同，compressor/indexer 的行最终化时间表也永远相同，KV 状态也就**与切分方式无关**地确定。

但「与切分方式无关」有个前提：`prefill_cap` 本身不变。一旦你改了 `DS4_METAL_PREFILL_CHUNK`，`prefill_cap` 变了，绝对边界的位置就变了，于是：

1. **compressor/indexer 的行最终化时间表变了**——同一位置之前可能不是行边界，现在成了；浮点累加顺序不同，KV 有细微差异。
2. **KV checkpoint 的推进点变了**——这是最直接的影响，见下面 4.3.3。
3. **logit 的写出点变了**——见下面 4.3.3。

这就是 README 那句「Changing the chunk changes the KV checkpoint/logit path」的全部含义。

#### 4.3.2 核心流程

把「chunk 边界 = checkpoint 推进点」的链条画清楚：

```
chunk 循环每跑完一块 (pos0 → chunk_end):
    发 progress 事件 "prefill_chunk", current=chunk_end
        ↓
ds4_session_note_prefill_progress 收到事件:
    checkpoint.len = chunk_end           # 把 checkpoint 推到块尾
    checkpoint_valid = true              # 标记为可信、可落盘
        ↓
ds4-server 可在此刻把 checkpoint 存为磁盘 KV 快照
```

也就是说：**checkpoint 只在 chunk 边界推进，不会在块中间推进**。chunk 大小 = checkpoint 推进的粒度。chunk 越大，可落盘的前缀点越稀疏；chunk 越小，落盘点越密集。

同时，logit 的写出也挂在块边界上：

```
对每个 chunk:
    chunk_logits = (有 progress 回调 或 本块是最后一块) ? logits : NULL
    metal_graph_prefill_layer_major(..., chunk_logits)
```

只有在写出 logit 的块上，output head 才会真正计算 logit。于是「logit 在哪个块上算」也由 chunk 切分决定——这就是「logit 路径」。

#### 4.3.3 源码精读

先看 progress 回调如何把 chunk 边界变成 checkpoint：

[ds4.c:26670-26680](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L26670-L26680) — `ds4_session_note_prefill_progress`。当事件是 `"prefill_chunk"` 且 `current` 在范围内时，把 `checkpoint.len` 设为 `current`（即块尾位置），逐 token 拷贝到 `checkpoint`，置 `checkpoint_valid = true`，并清掉 MTP draft。**这就是「chunk 边界 = checkpoint 推进点」的直接实现**。

这个回调的妙处，源码在另一处有一段关键注释点明：

[ds4.c:19596-19601](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L19596-L19601) — 「`prefill_chunk` 不仅仅是 UI 进度：`ds4_session_sync()` 用它来推进 live checkpoint，而 ds4-server 可能会保存那个 checkpoint。decode 式 prefill 只读最后一个 token 的 logit，所以只在末尾报告一个可缓存的 chunk。」这段注释直接把「chunk 事件」「checkpoint」「服务器落盘」三者焊在一起。

再看 logit 在哪块写：

[ds4.c:21028](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L21028) — `float *chunk_logits = (progress || chunk_end == end) ? logits : NULL;`。两条件任一为真就写 logit：`chunk_end == end`（最后一块必然要给最终 logit）；`progress` 非空（即 session 同步在用 checkpoint 回调时）也写——因为 checkpoint 推进点可能被落盘，而磁盘快照需要 logit 紧随 checkpoint tokens（这点 u8-l3 的 payload 序列化会展开）。**所以一旦走 session 同步路径，每个 chunk 都会算 logit**，logit 计算次数随 chunk 数线性增长——这就是「改 chunk 改 logit 路径」的运行时表现。

然后看 compressor 那边的「绝对边界」快路径，印证 chunk 对齐与 ratio 对齐的耦合：

[ds4.c:17840-17843](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L17840-L17843) — `aligned_chunk = (pos0 % ratio) == 0u && (n_tokens % ratio) == 0u`。当一块恰好在 `ratio` 边界上起止时，走一条「整块压缩」快路径，一次性算出 `comp_chunk = n_tokens / ratio` 行。这条快路径是否命中，取决于 chunk 边界与 `ratio` 边界是否对齐——而 chunk 边界又由 `prefill_cap` 决定。改 `prefill_cap`，就可能让原本命中快路径的块落回慢路径，从而改变浮点累加路径与 KV。

最后是 README 的官方结论与冷存策略：

[README.md:569-574](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L569-L574) — 默认 4096-token chunk；`DS4_METAL_PREFILL_CHUNK=N` 可比较别的尺寸（如 2048 以匹配官方向量路径），`=0` 则整段当一批；**「Changing the chunk changes the KV checkpoint/logit path, so compare it as an explicit run configuration」**——这就是本讲实践任务的出处。

[README.md:1146-1150](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L1146-L1150) — 冷存（cold save）**故意裁掉一小段 token 后缀并对齐到一个 chunk 边界**，以规避 BPE 边界重切带来的失配；默认对齐到 2048-token chunk。注意：磁盘 KV store 的对齐粒度（2048）和推理图的 `prefill_cap`（默认 4096）是两个独立的数，但二者必须协调——落盘的边界必须是推理图能精确复现的 checkpoint 点，所以官方向量测试干脆把 `DS4_METAL_PREFILL_CHUNK` 也钉到 2048（见 [tests/test-vectors/README.md:49](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/test-vectors/README.md#L49)），让两者完全一致。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：亲手回答任务里的问题——「为什么改 `DS4_METAL_PREFILL_CHUNK` 会改变 KV checkpoint/logit 路径」。

**操作步骤**（源码阅读 + 推演型，无需 GPU 即可完成结论部分）：

1. 读 [README.md:541-577](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L541-L577) 的 Benchmarking 段，划出「Changing the chunk changes the KV checkpoint/logit path」这句。
2. 在 `ds4.c` 里跟踪一条因果链，把下面三个「为什么」逐一对应到代码行：
   - **为什么改 checkpoint 路径**：chunk 越小 → chunk 数越多 → [ds4.c:21012](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L21012) 循环跑更多轮 → 每轮 [ds4.c:26673-26676](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L26673-L26676) 推进 checkpoint 一次 → 可落盘的前缀点越密。
   - **为什么改 logit 路径**：session 同步路径下 `progress` 非空，[ds4.c:21028](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L21028) 让每块都算 logit → chunk 数变了，logit 计算次数与位置都变。
   - **为什么连 KV 内容都会细微变**：`prefill_cap` 变 → 绝对边界变 → [ds4.c:17840](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L17840) 的 `aligned_chunk` 命中情况变 → compressor 行最终化的浮点累加路径变。
3. 做一个对比表（待本地验证），假设同一个 30000-token prompt：

   | `DS4_METAL_PREFILL_CHUNK` | chunk 数 | checkpoint 推进点 | 每次冷存可对齐的前缀 |
   | --- | --- | --- | --- |
   | 0（整段一批） | 1 | 仅末尾 | 仅末尾 |
   | 2048 | ~15 | 每 2048 token | 每 2048（与官方向量一致）|
   | 4096（默认） | ~8 | 每 4096 token | 受默认 2048 冷存对齐约束 |
   | 8192 | ~4 | 每 8192 token | 同上，但中途 checkpoint 更稀疏 |

**需要观察的现象**：改 chunk 不改最终生成的 token（数学上 KV 在「绝对边界对齐」前提下应一致到浮点误差），但**改的是中途可落盘/可恢复的粒度、logit 的计算次数、以及 compressor 快路径的命中情况**。

**预期结果**：你能用一句话向别人解释——「chunk 边界同时是 KV checkpoint 推进点、logit 写出点和磁盘冷存对齐点，所以 chunk 大小不是性能旋钮而是路径旋钮；要复现官方向量就必须把 chunk 钉成和他们一样的 2048。」

> 待本地验证：第 3 步对比表中 chunk 数与 checkpoint 点。有 GPU 的读者可设 `DS4_METAL_PREFILL_CHUNK=2048` 与 `=4096` 各跑一次同一 prompt，用 `--dump-logprobs` 观察中途 logit 是否因路径不同而出现数值漂移。

#### 4.3.5 小练习与答案

**练习 1**：session 同步路径下，为什么每个 chunk 都要算 logit（[ds4.c:21028](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L21028) 里 `progress` 非空导致 `chunk_logits` 非空），而不是只在最后一块算？

**答案**：因为每个 chunk 边界都会推进 checkpoint（[ds4.c:26673-26676](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L26673-L26676)），而 checkpoint 是可能被 ds4-server 落盘复用的。落盘的 KV 快照需要「checkpoint tokens + 紧随其后的 logits」一起存（见 u8-l3 的 payload 序列化），所以每个潜在落盘点都必须有有效 logit，不能等到最后一块才算。

**练习 2**：官方测试向量为什么要把 `DS4_METAL_PREFILL_CHUNK` 钉死在 2048（[tests/test-vectors/README.md:49](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/test-vectors/README.md#L49)），而不是用默认 4096？

**答案**：因为改 chunk 会改 KV/logit 路径（本模块核心结论）。官方向量是在某个特定 chunk 下生成的 logits 切片；要在本地复现到 bit 级一致，就必须用完全相同的 chunk 让绝对边界、compressor 快路径命中、checkpoint 推进点全部对齐。2048 还与磁盘冷存的默认对齐粒度一致，进一步减少路径分歧。

**练习 3**：假如把 `prefill_cap` 调到极大（比如超过 raw_cap 的几百倍），仅从 [ds4.c:20998](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L20998) 看，冷启动和续 prefill 的行为会有什么不对称？

**答案**：冷启动（`start == 0`）会用这个极大的 `chunk_cap`，单块临时显存极大，可能 OOM；而续 prefill（`start != 0`）会被砍到 `raw_cap`，不受影响。这说明 `prefill_cap` 主要约束的是冷启动长 prompt 的块大小，续 prefill 有独立的、更小的封顶。

---

## 5. 综合实践

把三个模块串起来，完成下面这个「chunk 旋钮影响分析」小任务：

**场景**：你要给团队写一份内部备忘，标题是《调整 `DS4_METAL_PREFILL_CHUNK` 的全部副作用》。请基于本讲源码，回答以下问题，每条都给出 `ds4.c` 或 `README.md` 的行号依据：

1. 默认值是多少？在什么条件下才会启用默认？（依据 [ds4.c:8346-8348](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L8346-L8348)）
2. 把它从 4096 调到 2048，**速度**上会怎么变？（提示：chunk 数翻倍 → 循环轮数翻倍 → [ds4.c:21012](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L21012) 的固定开销翻倍，但单块更小、临时显存更省、attention 内层更短。净效应待本地 benchmark。）
3. 把它从 4096 调到 2048，**正确性/可复现性**上会怎么变？（提示：绝对边界变了 → compressor 快路径命中变了 → KV 细微变化；checkpoint 推进点变了 → 落盘前缀粒度变了。参考 [ds4.c:17840](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L17840) 与 [README.md:573](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L573)。）
4. 设成 `0`（整段一批）什么时候安全、什么时候危险？（提示：看 [ds4.c:20322](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L20322) 的 `n_tokens > prefill_cap` 校验和 batch 张量大小。）
5. 在分布式推理里，`--dist-prefill-chunk` 默认也是 4096（见 [README.md:435-437](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L435-L437)）。本讲的「chunk = checkpoint 推进点」逻辑在分布式下还成立吗？（提示：分布式走的是 `ds4_dist_session_sync`，[ds4.c:26707-26716](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L26707-L26716)，pipeline chunk 与 KV checkpoint 的关系留给 u9-l4 展开。）

**交付物**：一张「旋钮 → 副作用」对照表，把性能、显存、可复现性、落盘粒度四个维度列清楚。这张表就是你之后做 benchmark（u10-l5）和贡献回归（u11-l4）时的判断依据。

> 待本地验证：第 2、3 条的量化结论。本任务只需把因果链和代码依据写准确，数值效应留给实际 benchmark。

## 6. 本讲小结

- **chunk 切分与对齐**：长 prompt 被切成默认 4096（PRO 8192）的块，由 `ds4_prefill_cap_for_prompt` 决定大小（优先级：调用方 > `DS4_METAL_PREFILL_CHUNK` > 默认档）；续 prefill 的块还会被 `mod = pos0 % prefill_cap` 砍到下一个绝对边界，并封顶到 `raw_cap`。
- **layer-major 图复用**：每个 chunk 跑一次 `metal_graph_prefill_layer_major`——「chunk 全穿第 0 层、再全穿第 1 层……」；所有 chunk 复用同一张 range-capable 图与同一组按 `prefill_cap` 分配的 `batch_*` 常驻张量，单块不得超 `prefill_cap`。
- **绝对边界保持**：chunk 边界被强制对齐到 `prefill_cap` 整数倍，使冷启动与续 prefill 的 compressor/indexer 行最终化时间表完全一致，KV 与切分方式无关。
- **chunk = 路径旋钮**：chunk 边界同时是 KV checkpoint 推进点（`ds4_session_note_prefill_progress`）、logit 写出点（`progress` 非空则每块算 logit）和磁盘冷存对齐点；所以改 `DS4_METAL_PREFILL_CHUNK` 改的不是性能而是整条 KV/logit 落地路径，复现官方向量必须钉死 2048。
- **本讲只讲「单机、单图」的 chunked prefill**；分布式下的 chunk 流水线（coordinator/worker 之间 chunk N 与 chunk N+1 重叠）是 u9-l4 的内容，MTP 投机解码是 u6-l2 的内容。

## 7. 下一步学习建议

- **u6-l2（MTP 投机解码）**：本讲的 chunk 循环只在「填 KV」，生成阶段另有投机解码路径；建议接着看 draft token 如何在已填好的 KV 上被验证接受/拒绝，以及为什么 MTP 状态不随磁盘 KV 持久化。
- **u6-l3（功耗管理与方向性引导）**：如果你想看 `--power` 如何在「层/token 之间插睡眠」，它会回到本讲的 layer-major 图层面，在层与层之间插入节流点。
- **u9-l4（分布式协议、路由与流水线）**：本讲末尾的综合实践第 5 题留下的悬念——分布式下 chunk 变成了「跨机流水线的一环」，A 在 chunk N+1 的层片上工作时 B 还在 chunk N，这条流水线与本讲的「单图复用」是两种不同的复用粒度。
- **u10-l5（速度基准 ds4-bench）**：本讲反复提到的「benchmark」就是 ds4-bench；它会用「保存/恢复 KV 快照再续 prefill」的手法精确测量每个前沿的增量 prefill 速度，正好用上本讲的 checkpoint 边界概念。
- **源码再读建议**：重读 [ds4.c:20946-21068](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L20946-L21068) 这一整段 `metal_graph_prefill_chunked_range`，把本讲三个模块的代码行串成一条完整调用链，这是本讲最值得内化的一段源码。
