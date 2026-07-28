# GQA 前向/反向降级

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 **GQA（Grouped-Query Attention，分组查询注意力）** 与普通 MHA 在「张量形状」上的唯一本质区别——`head > head_kv`；
- 在 `attn_engine.py` 的分发逻辑中，一眼判断一组 `qkv_meta` 会落到哪条 `lower_*` 路径，并解释 GQA 训练路径的触发条件 `q_seqlen == kv_len 且 head > head_kv`；
- 理解 `lower_gqa.py` 与 `lower.py` 在 IR / 降级三件套上几乎完全相同，真正的 GQA 差异被推迟到了模板 `attn_gqa_tl.py` 里的「头索引整除」`by // groups`；
- 厘清一个最容易踩坑的命名问题：用户脚本与 benchmark 里的 `groupnum` 指 **KV 头数**，而生成 kernel 里的 `groups` 指 **组大小（每个 KV 头共享多少个 Q 头）**，二者互为倒数。

本讲承接 u3-l3（引擎分发、md5 缓存、importlib 加载），把「训练阶段、Q 头多于 KV 头」这条分支单独拆出来讲透。

## 2. 前置知识

在进入 GQA 之前，先回顾两个事实（来自 u1-l4 与 u3-l3）：

1. **注意力的在线分块骨架**：scores = Q@Kᵀ → score_mod → online_func（逐 KV 块递推行级状态 m/r 并重缩放 o）→ o = p@V。这条骨架与「几个 Q 头共享一个 KV 头」无关。
2. **引擎按形状分发**：`AttentionEngine.__init__` → `_compile_tl` → `_select_lower_template`，根据 `qkv_meta` 抽出的 `q_seqlen / kv_len / head / head_kv` 四个量，在 5 条降级路径里选一条。

为什么需要 GQA？标准 MHA 里 Q、K、V 头数相同，KV 的权重与激活随 `head` 线性增长，在推理时尤其要把 KV cache 全部驻留显存。GQA 让多个 Q 头**共享**同一组 K/V 头，从而在不显著掉点的前提下：

- 把 KV cache 与 KV 权重压到原来的 `head_kv / head`；
- 把 KV 侧 GEMM 的算力/访存也等比缩小。

代价是 KV 头变少，Q 头到 KV 头需要一个 `floor` 整除映射。这个映射，正是本讲全部「代码差异」的源头。

一个记号约定：设 Q 头数为 \(H\)（脚本里叫 `H` / `head`），KV 头数为 \(G\)（脚本里叫 `G` / `head_kv`），要求 \(H \ge G\) 且 \(G \mid H\)。定义**组大小**

\[ g = \frac{H}{G} \]

即每个 KV 头被 \(g\) 个 Q 头共享。任意 Q 头 \(h_q \in [0, H)\) 映射到 KV 头：

\[ h_{kv} = \left\lfloor \frac{h_q}{g} \right\rfloor = \left\lfloor \frac{h_q \cdot G}{H} \right\rfloor \]

当 \(G = H\) 时退化为 MHA；当 \(G = 1\) 时退化为 MQA（Multi-Query Attention）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `attention_engine/attn_engine/attn_engine.py` | 引擎总入口；`_select_lower_template` 负责按形状分发到 `lower_*`。 |
| `attention_engine/core/lower/lower_gqa.py` | GQA 训练（前向+反向）降级编排；与 `lower.py` 近似，模板换成 GQA 版。 |
| `attention_engine/core/lower/lower.py` | MHA 训练降级编排；作为对照基线，帮助你看清 `lower_gqa` 改了什么、没改什么。 |
| `attention_engine/core/template/tl_template/attn/attn_gqa_tl.py` | GQA 的 Jinja2 骨架；真正的「头索引整除」差异全在这里。 |
| `attention_engine/core/template/tl_template/attn/attn_tl.py` | MHA 的 Jinja2 骨架；对照用。 |
| `attn_script/gqa.py` | GQA 用户示例脚本；跑通因果/非因果 softmax + 前向反向。 |
| `attention_engine/benchmark/bench_utils.py` | `do_bench_attention`，其中 `groupnum` 形参用于构造 GQA 参考实现。 |

## 4. 核心概念与源码讲解

### 4.1 GQA 形状约束与 head 分组

#### 4.1.1 概念说明

GQA 的「形状约束」只有一句话：**Q 的头数严格大于 KV 的头数，且训练阶段 Q 与 KV 序列长度相等**。除此之外，Q、K、V 三个张量的维度布局、online 算法、score_mod 都和 MHA 完全一样。

为什么强调「训练阶段」？因为 AttentionEngine 把「训练/预填充」（`q_seqlen == kv_len`）与「解码」（`q_seqlen < kv_len`，通常 `q_seqlen == 1`）拆成不同降级路径。GQA 在这两类场景各有专属文件：

- 训练 GQA → `lower_gqa.py`（本讲）；
- 解码 GQA → `lower_decode_gqa.py`（下一讲 u4-l3）。

二者都姓「gqa」，但分块结构差异很大，不要混淆。

#### 4.1.2 核心流程

给定一组满足约束的 `qkv_meta`，引擎的处理流程：

1. 从 `qkv_meta` 抽出 `q_seqlen / kv_len / head / head_kv`；
2. 命中分支 `q_seqlen == kv_len 且 head > head_kv`；
3. 调 `lower_gqa.lower_tl`，其内部走和 `lower` 一模一样的「降级三件套 + kernel + mask + 模板渲染」；
4. 渲染时用的是 `attn_gqa_tl.py` 而非 `attn_tl.py`，于是 K/V 的加载下标从 MHA 的 `by` 变成了 `by // groups`；
5. md5 缓存 + importlib 加载，得到一个普通 PyTorch 算子 `mod(q, k, v)`，可前向可反向。

形状映射的对应关系（注意 K/V 的 head 维变小了）：

| 张量 | MHA 形状 | GQA 形状 |
| --- | --- | --- |
| Q | `[B, S, H, D]` | `[B, S, H, D]` |
| K | `[B, S, H, D]` | `[B, S, G, D]` |
| V | `[B, S, H, DV]` | `[B, S, G, DV]` |

#### 4.1.3 源码精读

先看引擎怎么抽取形状并命中 GQA 训练分支。`_select_lower_template` 从 `qkv_meta` 的三个 `meta_tensor` 读出关键维度：

[attention_engine/attn_engine/attn_engine.py:229-232](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L229-L232) —— `q_seqlen`/`kv_len` 来自 q 与 v 的序列维，`head` 来自 q，`head_kv` 来自 v（注意 v 的 head 维才是 KV 头数）。

紧接着 5 个互斥 `if` 中，GQA 训练分支是最后一条：

[attention_engine/attn_engine/attn_engine.py:316-332](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L316-L332) —— 条件 `q_seqlen == kv_len and head > head_kv`，命中后 `from core.lower.lower_gqa import lower_tl` 并把 `(B, head, seqlen, dimqk, dimv)` 传给它。

> 注意：`lower_tl` 接收的第二个位置参数是 `head`（即 Q 头数 H），而 KV 头数 G 并不显式传入——它会被推迟到模板里由 `H//G` 反推（见 4.3.3）。这是 GQA 降级的一个关键设计：降级层对「分组」无感，分组只在模板里出现。

再看用户脚本怎么声明这组形状。`gqa.py` 用三个 `meta_tensor` 构造 `qkv_meta`，q 是 H 头、k/v 是 G 头：

[attn_script/gqa.py:82-88](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/gqa.py#L82-L88) —— `B, H, G, S = 1, 128, 8, 8192`；`qkv_meta = (meta_tensor(B, H, S, D), meta_tensor(B, G, S, D), meta_tensor(B, G, S, DV))`。

把脚本值代入：`q_seqlen = kv_len = 8192`，`head = 128`，`head_kv = 8`，满足 `q_seqlen == kv_len 且 head > head_kv`，所以一定走 `lower_gqa`。组大小 \(g = H/G = 16\)。

#### 4.1.4 代码实践

**实践目标**：不下断点、不跑 GPU，纯靠手算确认分发与组大小。

**操作步骤**（源码阅读型，无需运行）：

1. 打开 `attn_engine.py:316`，把 `gqa.py` 的形状 `(B=1, H=128, G=8, S=8192)` 代入 `q_seqlen / kv_len / head / head_kv` 四个量；
2. 逐条口算前面 4 个 `if`（MLA / decode-gqa / decode-mha / train-mha），说明为什么都判否；
3. 算出 `groups = H // G = 16`、`heads_kv = H // groups = 8`。

**需要观察的现象**：你会发现只要 `head > head_kv` 且序列等长，就**不可能**落到 MHA 的 `lower`；GQA 与 MHA 的分流完全由 `head` 与 `head_kv` 是否相等决定。

**预期结果**：

- 分发命中 `lower_gqa`；
- `groups = 16`，`heads_kv = 8`；
- 若把 `G` 改成 `1`（MQA），`groups = 128`，`heads_kv = 1`；若把 `G` 改成 `H`（即 128），则 `head == head_kv`，分发会**回退到 MHA 的 `lower`**。

> 是否真能运行 `gqa.py` 取决于本机是否装好 TileLang + CUDA，**待本地验证**；但上面的分发判断只依赖形状，不需要运行。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `gqa.py` 的 `S`（q 序列长）改成 `1`，其余不变，会走哪条降级路径？

> 答案：`q_seqlen = 1 != kv_len = 8192`，且 `head(128) > head_kv(8)`，命中**第二条**分支「decode gqa」（`attn_engine.py:253`），`assert q_seqlen == 1` 成立，改走 `lower_decode_gqa`——即下一讲 u4-l3 的内容，而不再是本讲的训练路径。

**练习 2**：组大小 \(g\) 与 KV 头数 \(G\) 满足什么关系？为什么 `head` 必须能被 `head_kv` 整除？

> 答案：\(g = H / G\)，即 \(g \cdot G = H\)。模板里用整数除法 `by // groups` 做 Q 头→KV 头映射，要求每个 KV 头恰好被 \(g\) 个连续 Q 头共享，因此 \(H\) 必须被 \(G\) 整除，否则会出现「尾巴头」无法对齐。

---

### 4.2 `lower_gqa` 与 `lower` 的异同

#### 4.2.1 概念说明

GQA 的降级逻辑**没有**任何「GQA 专属」的符号 IR 改动。`lower_gqa.py` 的绝大部分内容——`shape_idx_map_sp`、`KernelOptionsBase`、`lower_kernel`、`lower_score_mod`、`lower_online_func`、`lower_custom_inputs`——与 `lower.py` 逐字符相同。换句话说，**分组这件事对降级层是透明的**。

`lower_gqa` 相对 `lower` 真正的改动只有三处，且都属于「外围配置」级别：

1. **模板路径**指向 `attn_gqa_tl.py`（这才是承载 GQA 索引差异的地方）；
2. **少了一个 `init_value_map_func`**（把 `-inf` 映射成 `-1e38` 防 NaN 的保护），GQA 版直接用原始初值；
3. **infer_mask 逻辑更简陋**——没有 `infer_mask_block_M/N` 与 `extern_block_mask` 参数，blocksparse 分支也少了几行处理。

这种「复制 + 改模板」的写法说明：在本项目的架构里，**变体之间的差异应尽量沉淀到模板层**，降级层保持单一职责（把用户函数降级成 IR 片段），从而最大化复用。

#### 4.2.2 核心流程

`lower_gqa.lower_tl` 与 `lower.lower_tl` 的主流程**完全一致**（详见 u3-l2）：

```
性能配置(tune) → kernel_options → lower_custom_inputs
              → lower_score_mod → lower_online_func
              → 算 output_idx_list → lower_kernel
              → mask 降级 → 选模板(TlAttnTemplate/TlBlockAttnTemplate) 渲染
```

区别只在最后一步「选模板」时，`TEMPLATE_PATH` 指向 GQA 骨架；以及上面提到的两处外围差异。

#### 4.2.3 源码精读

**差异一：模板路径。** `lower_gqa.py` 顶部用本文件路径推算出 GQA 模板的绝对路径：

[attention_engine/core/lower/lower_gqa.py:20-23](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_gqa.py#L20-L23) —— `TEMPLATE_PATH` 指向 `../template/tl_template/attn/attn_gqa_tl.py`。

这个 `TEMPLATE_PATH` 在 `lower_tl` 末尾被传给 `TlAttnTemplate(...)` / `TlBlockAttnTemplate(...)`：

[attention_engine/core/lower/lower_gqa.py:728-729](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_gqa.py#L728-L729) —— `tlattn_template(TEMPLATE_PATH, ...)`，于是渲染灌进去的是 GQA 骨架，而非 MHA 骨架。

**差异二：缺失的 `-inf`→`-1e38` 映射。** `lower.py` 在 `lower_online_func` 里用 `init_value_map_func` 把 online_rowscales 的初值做一次安全改写（注释 "avoid nan"），而 `lower_gqa.py` 直接用原始 `v.code.name`：

[attention_engine/core/lower/lower_gqa.py:335-338](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_gqa.py#L335-L338) —— `tl_init_value = v.code.name`，未做 `-inf → -1e38` 改写。

`gqa.py` 的 `OnlineSoftmax` 把 `m` 初值设为 `Var("-inf")`（`gqa.py:27`），所以在 GQA 路径下会直接生成 `T.fill(m, -inf)`。这是一个值得注意的**分叉落后点**：`lower_gqa` 是一条较早复制出去的分支，没跟上 `lower` 后续加的 NaN 保护。日常使用不一定会触发（取决于硬件对 `-inf` 的处理），但调试时若遇到 NaN，可从这里排查。

**差异三：infer_mask 更简陋。** `lower_gqa.lower_tl` 的签名没有 `infer_mask_block_M/N` 与 `extern_block_mask`：

[attention_engine/core/lower/lower_gqa.py:609-614](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_gqa.py#L609-L614) —— 形参里只有 `infer_mask=False`，没有块尺寸与外部块表参数。

对应的 infer_mask 分支用 `block_M = int(tune_output.block_M)` 直接取调优块尺寸，blocksparse 分支也只 `+1` 平移 `output_idx_list`（注意：`lower.py` 还会同时平移 `bwd_output_idx_list`，GQA 版没有）：

[attention_engine/core/lower/lower_gqa.py:711-745](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_gqa.py#L711-L745) —— 注释直写 "TODO: infer mask logic"，可见这部分尚未与 `lower` 对齐。

**关键结论：IR 与降级三件套 100% 共享。** `shape_idx_map_sp`、`KernelOptionsBase.add_output_tensor/add_input_tensor/add_intermediate_tensor`、`lower_kernel`、`CopyMap` 等在两个文件里逐字相同。这意味着你为 MHA 学到的所有 score_mod / online_func / custom_inputs 降级知识，可以**原封不动**迁移到 GQA。

#### 4.2.4 代码实践

**实践目标**：用 `diff` 把两个降级文件的差异看穿，建立「GQA = MHA 降级 + 换模板」的直觉。

**操作步骤**（命令行，只读）：

1. 在仓库根目录运行 `diff attention_engine/core/lower/lower.py attention_engine/core/lower/lower_gqa.py`；
2. 逐块审视 diff 输出，把改动分成三类：(a) 模板路径、(b) `init_value_map_func`、(c) infer_mask 相关；
3. 确认 `shape_idx_map_sp`、`KernelOptionsBase`、`lower_score_mod`、`lower_online_func`、`lower_custom_inputs`、`lower_kernel` **没有出现在 diff 里**。

**需要观察的现象**：diff 行数很少（约 30 行），且全部集中在文件顶部（模板路径）、`lower_online_func` 里一行初值改写、以及 `lower_tl` 尾部的 infer_mask 段；中间几百行降级核心代码完全一致。

**预期结果**：你会得出本节标题的结论——`lower_gqa` 是 `lower` 的「换模板 fork」，分组语义不在降级层。

#### 4.2.5 小练习与答案

**练习 1**：既然降级层对分组无感，那 `gqa.py` 里 `OnlineSoftmax` 的 `online_fwd` / `online_fwd_epilogue` 与 `mha.py` 里的有区别吗？

> 答案：没有。两个脚本里的 `OnlineSoftmax` 完全相同（行最大值 m、指数和 r、lse 递推），因为 online 算法只关心「单个 Q 头内部、沿 KV 序列的行级递推」，与「该 Q 头和谁共享 KV 头」无关。共享关系只影响加载 K/V 时取哪一列 KV 头。

**练习 2**：如果想在 GQA 里也获得 `lower.py` 的 `-inf → -1e38` NaN 保护，最少改哪里？

> 答案：把 `lower.py` 顶部的 `init_value_map_func` 函数复制到 `lower_gqa.py`，并把第 337 行的 `tl_init_value = v.code.name` 改成 `tl_init_value = init_value_map_func(v.code.name)`。注意这会改变生成代码的 md5，从而触发重新编译。

---

### 4.3 `attn_gqa_tl.py` 模板的索引差异

#### 4.3.1 概念说明

GQA 的所有「特殊性」都集中在模板 `attn_gqa_tl.py` 的两件事上：

1. **kernel 形参多了一个 `groups`**，并据此算出 KV 头数 `heads_kv = heads // groups`，K/V 的 buffer 形状用 `heads_kv` 而非 `heads`；
2. **加载/写回 K、V 时，头下标做整除**：Q 头 `by` → KV 头 `by // groups`（前向），反向里同理 `bx // groups`。

这两个改动让 Q 侧仍按 `heads` 全量并行（每个 Q 头一个线程块），但 K/V 侧只存 `heads_kv` 份；多个共享同一 KV 头的 Q 头会重复读同一块 K/V——这正是 GQA 用算力换显存的体现。

#### 4.3.2 核心流程

前向 kernel 的并行网格与数据流：

```
grid = (ceildiv(seq_len, block_M), heads, batch)   # 每个 (Q块, Q头, batch) 一个块
Q_shared  <- Q[bz, Q块, by,    :]                  # by 是 Q 头，直接用
for k in KV块:
    K_shared <- K[bz, KV块, by // groups, :]        # ← 整除映射到 KV 头
    scores   = Q_shared @ K_sharedᵀ
    ... score_mod / online_func ...
    V_shared <- V[bz, KV块, by // groups, :]        # ← 同样的整除映射
    acc_o    = acc_o * o_scale + p @ V_shared
epilogue: 写回 Output[bz, Q块, by, :]               # 输出仍是 H 个头
```

反向是对称的：网格换成 `(heads, ceildiv(seq_len, block_M), batch)`，梯度累加到 dK/dV 时也用 `// groups` 把多个 Q 头的梯度 atomic-add 进同一 KV 头。

#### 4.3.3 源码精读

**前向 kernel 签名与 KV 头数推导。** `groups` 是 kernel 形参（默认 1，即 MHA），`heads_kv = heads // groups` 决定 K/V 形状：

[attention_engine/core/template/tl_template/attn/attn_gqa_tl.py:17-24](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_gqa_tl.py#L17-L24) —— `def kernel(..., groups=1, ...): heads_kv = heads // groups; shape_k = [batch, seq_len_kv, heads_kv, dim_qk]; shape_v = [batch, seq_len_kv, heads_kv, dimv]`。

对比 MHA 模板，K/V 形状只有一个 `shape`/`shape_v`，head 维直接是 `heads`：

[attention_engine/core/template/tl_template/attn/attn_tl.py:51-56](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L51-L56) —— MHA 的 `shape = [batch, seq_len, heads, dim]`，Q/K/V 同形。

**前向并行网格与 Q 加载。** 网格第二维仍是 `heads`（Q 头全展开），Q 用 `by` 直接索引：

[attention_engine/core/template/tl_template/attn/attn_gqa_tl.py:48](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_gqa_tl.py#L48) —— `T.Kernel(T.ceildiv(seq_len, block_M), heads, batch) as (bx, by, bz)`。

[attn_script/../attn_gqa_tl.py:67](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_gqa_tl.py#L67) —— `T.copy(Q[bz, bx*block_M:(bx+1)*block_M, by, :], Q_shared)`，Q 头用 `by`。

**前向 K/V 加载的整除映射（GQA 的核心差异）。**

[attention_engine/core/template/tl_template/attn/attn_gqa_tl.py:79-80](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_gqa_tl.py#L79-L80) —— `T.copy(K[bz, k*block_N:(k+1)*block_N, by // groups, :], K_shared)`，K 头下标是 `by // groups`。

[attention_engine/core/template/tl_template/attn/attn_gqa_tl.py:94-95](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_gqa_tl.py#L94-L95) —— gemm 后 `T.copy(V[..., by // groups, :], V_shared)`，V 同样整除。

对照 MHA 模板，K/V 直接用 `by`：

[attention_engine/core/template/tl_template/attn/attn_tl.py:118](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L118) —— MHA 的 `T.copy(K[bz, k*block_N:(k+1)*block_N, by, :], K_shared)`，没有 `// groups`。

> 这就是「GQA 差异 = 把 K/V 的头下标 `by` 换成 `by // groups`」的全部内容。

**反向 kernel 签名同样带 `groups`。**

[attention_engine/core/template/tl_template/attn/attn_gqa_tl.py:190-197](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_gqa_tl.py#L190-L197) —— `def flashattn_bwd(..., groups=1): heads_kv = heads // groups; shape_k/shape_v 用 heads_kv`。

反向网格 `T.Kernel(heads, ...)`，头维是 `bx`，K/V 加载同样用 `bx // groups`：

[attention_engine/core/template/tl_template/attn/attn_gqa_tl.py:275-276](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_gqa_tl.py#L275-L276) —— `T.copy(K[bz, ..., bx // groups, :], K_shared)`、`T.copy(V[bz, ..., bx // groups, :], V_shared)`。

**反向梯度累加：多 Q 头归并到同一 KV 头。** dK/dV 的 head 维是 `heads_kv`，所以多个共享 Q 头的 dk/dv 通过 `atomic_add` 累加进同一 KV 头：

[attention_engine/core/template/tl_template/attn/attn_gqa_tl.py:345-348](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_gqa_tl.py#L345-L348) —— `T.atomic_add(dV[bz, ..., bx // groups, j], dv[i, j])`、`T.atomic_add(dK[bz, ..., bx // groups, j], dk[i, j])`。

> 这就是 GQA 反向比 MQA/MHA 多出来的「规约」：同一个 KV 头要接收来自 \(g\) 个 Q 头的梯度。

**`groups` 实参从哪来？接口层用 `H // G` 传入。** 前向接口从 `q.shape`/`v.shape` 读出 `H` 与 `G`，把 `H//G` 作为 `groups` 传给 kernel：

[attention_engine/core/template/tl_template/attn/attn_gqa_tl.py:356-366](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_gqa_tl.py#L356-L366) —— `_, N_CTXKV, G, D_HEAD_V = v.shape` 取出 G（KV 头数），`tl.profiler.cached(kernel, ..., H//G, is_causal, shared_fuse)` 把 `H//G` 绑定到 kernel 形参 `groups`。

反向接口同理：

[attention_engine/core/template/tl_template/attn/attn_gqa_tl.py:384-403](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_gqa_tl.py#L384-L403) —— 同样从 `v.shape` 取 `G`，`cached(flashattn_bwd, ..., H//G)`。

#### 4.3.4 代码实践：`groupnum` 的两副面孔

**实践目标**：厘清「脚本/benchmark 的 `groupnum`」与「kernel 的 `groups`」这组反向命名，并亲手追一遍头索引。

**背景——两套互为倒数的记号**：

| 出处 | 名字 | 含义 | `gqa.py` 取值 |
| --- | --- | --- | --- |
| 脚本 `gqa.py` / `do_bench_attention(groupnum=...)` | `groupnum` (G) | **KV 头数** | 8 |
| 生成 kernel `attn_gqa_tl.py` | `groups` | **组大小** = H/G | 16 |

关系：`groupnum × groups = H`（8 × 16 = 128）。

**操作步骤**（源码阅读型）：

1. 读 `do_bench_attention` 签名与 `groupnum` 默认值：

   [attention_engine/benchmark/bench_utils.py:1179-1184](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L1179-L1184) —— `groupnum=None` 时默认 `groupnum = H`（即不指定就是 MHA）；指定为 `G` 时即 KV 头数。

2. 看它如何用 `groupnum` 构造参考的 key/value：

   [attention_engine/benchmark/bench_utils.py:1194-1202](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L1194-L1202) —— `key = randn(B, S, groupnum, D)`、`value = randn(B, S, groupnum, DV)`，key/value 的 head 维 = `groupnum` = KV 头数。

3. 看参考实现（flash_attn fallback）如何反推组大小：

   [attention_engine/benchmark/bench_utils.py:1250-1258](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/benchmark/bench_utils.py#L1250-L1258) —— `num_head_groups = query.shape[2] // key.shape[2]` = H // groupnum，再做 `rearrange('b s (h g) d -> b s g h d', g=num_head_groups)`。这里的 `num_head_groups` 与 kernel 的 `groups` 是同一个量（组大小）。

4. 代入 `gqa.py` 数值：`groupnum = G = 8`（KV 头数），`groups = H//G = 16`（组大小）。对几个 Q 头手算 `by // groups`：

   | Q 头 `by` | `by // 16` = KV 头 |
   | --- | --- |
   | 0 | 0 |
   | 15 | 0 |
   | 16 | 1 |
   | 127 | 7 |

   即 Q 头 0–15 共享 KV 头 0，Q 头 16–31 共享 KV 头 1，……，Q 头 112–127 共享 KV 头 7。

**需要观察的现象**：你会发现 kernel 里凡是涉及 K/V 的下标都带 `// groups`，而凡是涉及 Q/Output 的下标都不带——因为输出仍是 `H` 个头，只有 K/V 是 `G` 份。

**预期结果**：

- `gqa.py` 传 `groupnum=8` → benchmark 造 `key/value` 为 8 头；
- 生成 kernel 接到 `groups=16` → `heads_kv = 128 // 16 = 8`，与参考一致；
- 若把脚本里 `G` 改成 `2`（对应规格里 `head=8, head_kv=2` 的小例子思路），则 `groups = H//G`，例如 `H=8, G=2` 时 `groups=4`，`by=0..3→KV头0`、`by=4..7→KV头1`。

> 实际运行 `gqa.py` 需要 TileLang + GPU，**待本地验证**；以上索引推导纯算术，不需运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么 GQA 前向网格第二维仍然是 `heads`（H），而不是 `heads_kv`（G）？

> 答案：因为输出 `Output` 的 head 维是 H（每个 Q 头都要算自己的 attention 输出）。若按 G 并行，一个 KV 头对应的多个 Q 头得分开处理，反而更复杂。按 H 并行让每个 Q 头独立完成「取自己那块 K/V → scores → online → p@V」，K/V 的复用通过 `by // groups` 自然实现。

**练习 2**：反向里 dK/dV 为什么必须用 `atomic_add`，而 MHA 反向可以避免？

> 答案：MHA 里每个 KV 头只对应一个 Q 头（1:1），梯度写回无竞争；GQA 里同一个 KV 头要接收 \(g\) 个 Q 头的 dk/dv，这些 Q 头在不同的线程块并行执行，写同一块 dK/dV 必然竞争，所以用 `atomic_add`。这是 GQA 在反向上比 MHA 多出的同步开销。

**练习 3**：若 `groups=1`，`attn_gqa_tl.py` 退化为哪种注意力？

> 答案：`groups=1` 时 `heads_kv = heads`，`by // 1 = by`，K/V 与 Q 一一对应——退化为标准 MHA。事实上 `groups` 默认值就是 1，所以这个模板在极限情况下与 MHA 模板行为一致（只是布局/接口细节略有不同）。

## 5. 综合实践

**任务**：把 `gqa.py` 改造成一个「最小 GQA 调试器」，在不依赖 GPU 运行的前提下，验证你对分发与索引的全部理解。

要求完成以下三件事：

1. **分发追踪**：写一个小函数，输入 `(B, H, G, S)`，复刻 `attn_engine.py:229-332` 的 `if/elif` 链，输出会调用的 `lower_*` 函数名。用它断言 `(1, 128, 8, 8192)` → `lower_gqa`、`(1, 8, 2, 4096)` → `lower_gqa`、`(1, 8, 8, 4096)` → `lower`（MHA）、`(1, 128, 8, 1)` → `lower_decode_gqa`。

2. **索引表生成**：写一段纯 Python，给定 `(H, G)`，打印每个 Q 头 `h_q` 对应的 KV 头 `h_q // (H//G)`，并统计每个 KV 头被几个 Q 头共享（应全等于 `H//G`）。

3. **导出代码核对（可选，需运行环境）**：参考 u3-l3 / u5-l6 提到的导出手法，把 `gqa.py` 生成的 TileLang 源码落盘（或直接读 `attn_engine/cache/<md5>.py`），用文本搜索确认：
   - kernel 形参里有 `groups=1`；
   - K/V 的 `T.copy` 里出现 `by // groups`（前向）与 `bx // groups`（反向）；
   - dK/dV 的 `atomic_add` 下标里出现 `// groups`。

   若本机无 GPU/TileLang，此项标注「待本地验证」，仅完成 1、2 两步即可。

**验收标准**：第 1 步的 4 个断言全过；第 2 步的统计显示「每个 KV 头共享数恒定 = H//G」；第 3 步（若运行）能 grep 到上述三处 `groups`/`// groups`。

## 6. 本讲小结

- GQA 与 MHA 的唯一形状区别是 `head > head_kv`（训练阶段还要求 `q_seqlen == kv_len`）；引擎据此在 `_select_lower_template` 分流到 `lower_gqa`。
- `lower_gqa.py` 是 `lower.py` 的「换模板 fork」：IR、降级三件套、`lower_kernel` 全部逐字相同，分组语义对降级层透明。
- `lower_gqa` 相对 `lower` 有三处外围差异：模板路径换成 `attn_gqa_tl.py`；缺少 `-inf → -1e38` 的 `init_value_map_func` NaN 保护；infer_mask 更简陋（无 `extern_block_mask` 等）。
- 真正的 GQA 差异全在模板：kernel 多一个 `groups` 形参，K/V 用 `heads_kv = heads // groups`，加载/写回 K/V 时头下标做 `by // groups`（前向）/ `bx // groups`（反向）整除。
- 反向 dK/dV 因多 Q 头共享同一 KV 头而改用 `atomic_add` 归并梯度，这是 GQA 相对 MHA 的额外同步开销。
- 命名陷阱：脚本/benchmark 的 `groupnum`（= G）是 **KV 头数**，kernel 的 `groups`（= H//G）是 **组大小**，二者互为倒数。

## 7. 下一步学习建议

- **u4-l3 解码场景**：当 `q_seqlen < kv_len`（典型 `q_seqlen == 1`）时，GQA 走 `lower_decode_gqa.py`，引入 split-kv 与 `num_split`，`final_rowscales` 会多出一个 split 维——建议接着读，对比「训练 GQA」与「解码 GQA」的分块结构差异。
- **u4-l4 MLA 解码**：MLA 是另一种 KV 共享（kv_shared），其 `combine` kernel 与本讲反向的 `atomic_add` 归并思路可对照阅读。
- **源码延伸**：如果想加深「差异沉到模板」的理解，可以 `diff` `attn_tl.py` 与 `attn_gqa_tl.py`，确认 GQA 改动是否也集中在 `groups` 形参与 `// groups` 下标这两处——这将强化你对本项目「降级层共享、模板层差异化」架构的直觉。
