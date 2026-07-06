# u3-l2 权重绑定与张量布局

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 ds4 为什么要在 mmap 加载完成之后、推理开始之前，再做一次「权重绑定（weights binding）」，以及这次绑定把世界从「字符串查表」变成了什么。
- 读懂 `ds4.c` 里 `ds4_weights` / `ds4_layer_weights` 这两张「语义指针表」，并能解释为什么它们是「编译期定死、运行期填指针」的。
- 跟着 `weights_bind` → `weights_bind_output` → `weights_bind_layer` 这条调用链，列出**一个 transformer 层**绑定了哪些权重张量（attention 投影、shared expert、routed experts 的 gate/up/down），以及哪些张量是「按层条件可选」的。
- 理解 `weights_bind` 如何接住「分布式层裁剪（load_slice）」：每个进程只绑定属于自己的那一段层，token embedding 和 output head 只落在特定的 worker 上。
- 解释 `weights_validate_layout` 为什么在绑定之后还要再做一遍「类型 + 维度」的严格校验。

本讲承接 [u3-l1](u3-l1-gguf-mmap-loading.md) 的结论：`model_open` 已经把 GGUF 的元数据表和张量目录解析进了 `ds4_model`，每个张量都拿到了一个 `ds4_tensor`（含名字、维度、类型、绝对偏移）。本讲回答下一个问题：**这些按名字扁平排列的张量，怎么变成推理内核能直接用的 C 结构？** 本讲只盯「绑定」，不展开量化块的数学（那是 [u3-l4](u3-l4-quantization-formats.md)）和前向计算（那是 u4）。

## 2. 前置知识

### 2.1 从 u3-l1 接过来的两张表

[u3-l1](u3-l1-gguf-mmap-loading.md) 讲过，`model_open` 之后 `ds4_model` 里有两样东西与本讲直接相关：

1. **张量目录 `m->tensors[]`**：一个扁平数组，每个元素是一个 `ds4_tensor`，记录该张量的**名字**（如 `blk.5.attn_q_a.weight`）、维度、类型、`abs_offset`（在 mmap 区域里的绝对字节偏移）。权重的字节本身还留在 mmap 区域里，没有拷贝。
2. **按名字查找**：`model_find_tensor(m, name)` 就是对这张表做一次线性扫描，按字符串相等找到对应的 `ds4_tensor`。

也就是说，u3-l1 留给我们的世界是「**字符串 → 张量**」：你想要第 5 层的 query 投影，得拼出字符串 `"blk.5.attn_q_a.weight"` 再去表里找。本讲要做的，就是把这层字符串查询**一次性翻译掉**。

### 2.2 DeepSeek V4 一层里大概有哪些东西

为了看懂下面 `weights_bind_layer` 绑定的一大堆字段，先在脑子里建立 DeepSeek V4 单层的数据流骨架（细节留到 [u4-l1](u4-l1-deepseek-v4-architecture.md)）：

```
hidden
  │
  ├── attention 子层（MLA 压缩注意力）
  │     - hyper-connection（一种残差连接的变体，下文简称 hc）
  │     - 低秩 query/kv 投影（q_a → q_b、kv）
  │     - 输出投影（output_a → output_b）
  │     - 可选：compressor / indexer（长上下文压缩 KV 用）
  │
  └── FFN 子层（MoE）
        - hyper-connection
        - 路由器（gate_inp）：为每个 token 选若干 routed expert
        - routed experts（gate_exps / up_exps / down_exps，各有 N 个专家）
        - shared expert（gate_shexp / up_shexp / down_shexp，所有 token 共享）
```

记住这条骨架，下面的字段名（`attn_*`、`ffn_*`、`hc_*`、`*_exps`、`*_shexp`）就能对号入座。

### 2.3 「绑定（binding）」这个词的含义

在 ds4 语境里，**绑定** = 「把 GGUF 里按字符串命名的张量，填进一个字段名有语义的 C 结构体的指针字段」。绑定之后，推理代码不再写 `model_find_tensor(m, "blk.5.attn_q_a.weight")`，而是直接写 `layer->attn_q_a`。这是一次性的、在引擎打开时完成的翻译，做完之后字符串查询就「退役」了。源码里有一段注释把这件事说得很直白：

> After this section, the rest of the program addresses tensors by semantic fields such as `layer->attn_q_a` or `layer->ffn_gate_exps` rather than by string lookup.

## 3. 本讲源码地图

| 文件 | 本讲关心的部分 | 作用 |
|------|----------------|------|
| [ds4.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c) | `ds4_tensor` / 张量类型枚举、`ds4_layer_weights` / `ds4_weights` 结构、`model_find_tensor` / `required_tensor` / `required_tensorf` / `tensor_by_namef` 查找族、`weights_bind_output` / `weights_bind_layer` / `weights_bind` 绑定族、`weights_validate_layout` 校验、`ds4_engine_open` 里的调用点 | 本讲全部核心逻辑 |
| [ds4.h](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h) | `ds4_engine_options` 里的 `load_slice` / `load_layer_start` / `load_layer_end` / `load_output` 字段、`ds4_distributed_layers` 子结构 | 暴露给前端的「只加载一段层」开关 |
| [README.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md) | 关于 routed MoE 量化、PRO 分片（`pro-q4-layers00-30` / `pro-q4-layers31-output`）的说明 | 解释为什么要支持「只绑一段层」 |

## 4. 核心概念与源码讲解

### 4.1 权重绑定函数族

#### 4.1.1 概念说明

权重绑定这件事可以拆成两层：

- **底层查找原语**：给定一个张量名字（或名字模板），从 `ds4_model` 的张量目录里找出对应的 `ds4_tensor *`。这一层处理「找不到怎么办」——是直接 `exit(1)`（必需张量缺失），还是安静地返回 `NULL`（可选张量缺失）。
- **上层绑定函数**：按 DeepSeek V4 的语义，把一组张量填进 `ds4_weights` / `ds4_layer_weights`。这一层处理「哪些张量属于 output head、哪些属于某一层、哪些是条件可选的」。

ds4 把这两层分得很清楚，于是有了一组小而正交的函数，本节统称「**绑定函数族**」。

#### 4.1.2 核心流程

绑定的整体流程是：

```
ds4_engine_open
   └─ model_open(...)              # u3-l1：mmap + 解析张量目录，得到 m->tensors[]
   └─ config_validate_model(...)   # 校验 GGUF 元数据与编译期常量一致
   └─ weights_bind(&e->weights, &e->model, load_slice, start, end, ...)
         ├─ memset(w, 0, ...)                       # 先把整张指针表清零
         ├─ 绑 token_embd（按需）
         ├─ weights_bind_output(w, m, ...)          # output head
         ├─ for il in [start, end]:
         │      weights_bind_layer(&w->layer[il], m, il)   # 逐层
         └─ weights_validate_layout(w, start, end, ...)    # 类型 + 维度严格校验
```

关键设计：

1. **先 `memset` 清零**：所有指针字段初始化为 `NULL`，这样「这层不需要的张量」自然就是 `NULL`，后续校验和推理都能据此判断。
2. **必需 vs 可选**：必需张量用 `required_tensorf`（找不到就 `exit(1)`），可选张量用 `tensor_by_namef`（找不到返回 `NULL`）。
3. **绑定完立刻校验**：`weights_validate_layout` 会重新逐个检查类型和维度，确保「名字对上了，形状也得对」。

#### 4.1.3 源码精读

先看底层查找原语。`model_find_tensor` 是最朴素的一个——线性扫描张量目录，按字符串相等匹配（[ds4.c:2083-2092](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L2083-L2092)）：

```c
static ds4_tensor *model_find_tensor(const ds4_model *m, const char *name) {
    const size_t len = strlen(name);
    for (uint64_t i = 0; i < m->n_tensors; i++) {
        if (m->tensors[i].name.len == len &&
            memcmp(m->tensors[i].name.ptr, name, len) == 0) {
            return &m->tensors[i];
        }
    }
    return NULL;
}
```

这段代码做的事：遍历张量目录，找一个名字长度相同、字节内容也相同的张量；找不到返回 `NULL`。注意它**没有哈希、没有排序**，就是 O(n) 扫描——但因为绑定只在引擎打开时跑一次（几万个张量扫几十次），这点开销完全可以接受，换来的是极简实现。

在它之上包出「必需」和「按模板」两个变体（[ds4.c:3113-3134](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L3113-L3134)）：

```c
static ds4_tensor *required_tensor(const ds4_model *m, const char *name) {
    ds4_tensor *t = model_find_tensor(m, name);
    if (!t) { fprintf(stderr, "ds4: required tensor is missing: %s\n", name); exit(1); }
    return t;
}

static ds4_tensor *tensor_by_namef(const ds4_model *m, const char *fmt, uint32_t layer) {
    char name[128];
    int n = snprintf(name, sizeof(name), fmt, layer);
    if (n < 0 || (size_t)n >= sizeof(name)) ds4_die("tensor name is too long");
    return model_find_tensor(m, name);            // 找不到返回 NULL（可选）
}

static ds4_tensor *required_tensorf(const ds4_model *m, const char *fmt, uint32_t layer) {
    char name[128];
    int n = snprintf(name, sizeof(name), fmt, layer);
    if (n < 0 || (size_t)n >= sizeof(name)) ds4_die("tensor name is too long");
    return required_tensor(m, name);              // 找不到 exit(1)（必需）
}
```

要点：

- `tensor_by_namef` 把 `"blk.%u.attn_q_a.weight"` 这样的模板按层号格式化成真实名字（如 `"blk.5.attn_q_a.weight"`），再调用 `model_find_tensor`，**找不到返回 `NULL`**——用于可选张量。
- `required_tensorf` 同样格式化，但调用 `required_tensor`，**找不到就 `exit(1)`**——用于必需张量。
- 这组函数的命名约定（`required_*` = 必需 / 无 `required_` 前缀 = 可选）会在 `weights_bind_layer` 里反复出现，是阅读那段代码的钥匙。

再看顶层的 `weights_bind`，它是绑定族的总入口（[ds4.c:4070-4106](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L4070-L4106)）：

```c
static void weights_bind(
        ds4_weights     *w,
        const ds4_model *m,
        bool             load_slice,
        uint32_t         load_layer_start,
        uint32_t         load_layer_end,
        bool             require_output,
        bool             optional_output) {
    memset(w, 0, sizeof(*w));                 // (1) 整张指针表清零

    uint32_t start = 0;
    uint32_t end = DS4_N_LAYER - 1u;
    bool require_token_embd = true;
    if (load_slice) {                         // (2) 分布式：只绑一段层
        ...                                   //     详见 4.3
    } else {
        require_output = true;
        optional_output = false;              // 非分片：output head 必须有
    }

    if (require_token_embd) {
        w->token_embd = required_tensor(m, "token_embd.weight");
    } else {
        w->token_embd = model_find_tensor(m, "token_embd.weight");
    }
    weights_bind_output(w, m, require_output, optional_output);   // (3) output head

    for (uint32_t il = start; il <= end; il++) {
        weights_bind_layer(&w->layer[il], m, il);                 // (4) 逐层绑定
    }

    weights_validate_layout(w, start, end, require_token_embd, require_output);  // (5) 校验
}
```

这段总控代码体现了几条重要规则：

- **(1)** 先清零，保证「没绑的字段 = `NULL`」，下游可以安全判空。
- **(3)** output head 用 `weights_bind_output` 单独绑，因为它不属于任何一层（它是「最后一层之后」的那个分类头）。
- **(4)** 层是逐层独立绑定的，`weights_bind_layer` 每次只填 `w->layer[il]` 这一个元素。
- **(5)** 绑完立刻校验，把「名字找对了」升级成「形状也对」。

`weights_bind_output` 本身很短，体现「required / optional」两条分支（[ds4.c:4000-4019](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L4000-L4019)）：

```c
static void weights_bind_output(ds4_weights *w, const ds4_model *m, bool required, bool optional) {
    if (required) {
        w->output_hc_base   = required_tensor(m, "output_hc_base.weight");
        w->output_hc_fn     = required_tensor(m, "output_hc_fn.weight");
        w->output_hc_scale  = required_tensor(m, "output_hc_scale.weight");
        w->output_norm      = required_tensor(m, "output_norm.weight");
        w->output           = required_tensor(m, "output.weight");
        return;
    }
    if (!optional) return;                    // 既不必需也不可选：什么都不绑（保持 NULL）

    w->output_hc_base   = model_find_tensor(m, "output_hc_base.weight");   // 可选：找不到=NULL
    ...
    if (weights_have_partial_output_head(w) && !weights_have_output_head(w)) {
        ds4_die("partial output head in GGUF");   // output head 要么全有，要么全没有
    }
}
```

这里有个很实际的约束：**output head 不允许「半套」**。如果 GGUF 里只出现了 `output.weight` 却没有 `output_norm.weight`，`weights_have_partial_output_head` 会判定为「半套」并直接 `ds4_die`。这能挡住损坏或拼错的分布式分片。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「绑定族」里 `required_*` 与非 `required_` 前缀对应「必需 / 可选」两种行为。

**操作步骤**：

1. 打开 [ds4.c:3113-3134](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L3113-L3134)，对比 `tensor_by_namef` 与 `required_tensorf` 两个函数。
2. 在 `weights_bind_layer`（[ds4.c:4021-4066](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L4021-L4066)）里搜 `tensor_by_namef`，数一数有几处用了「可选」版本（答案应该是 `ffn_exp_probs_b` 那一处，对应 `blk.%u.exp_probs_b.bias`）。
3. 思考：为什么 `exp_probs_b` 要设计成可选，而同在一层的 `ffn_gate_inp` 却是必需的？

**需要观察的现象**：你会看到 `ffn_exp_probs_b = tensor_by_namef(...)`（可能为 `NULL`），而它周围几乎全是 `required_tensorf(...)`。

**预期结果**：`exp_probs_b` 是路由器的偏置项，某些量化/导出流程不保留它，所以设为可选；`ffn_gate_inp` 是路由器的主权重，缺了 MoE 就没法路由，所以必需。可选字段在结构体里以 `NULL` 形式存在，下游用 `tensor_expect_optional` 单独校验。

> 待本地验证：如果你手头有一份 GGUF，可用 gguf-tools 的张量列举工具确认 `exp_probs_b.bias` 是否存在；没有模型也无妨，本实践是纯源码阅读型。

#### 4.1.5 小练习与答案

**练习 1**：`model_find_tensor` 是 O(n) 线性扫描，为什么不做成哈希表？

**参考答案**：因为绑定只在 `ds4_engine_open` 时跑一次，之后推理全程用 `layer->attn_q_a` 这样的字段直访，不再查字符串。一次性、几万次扫描的开销相对模型加载（mmap 缺页、GPU 初始化）可忽略，线性扫描换来的是 10 行代码、零额外内存、零 bug 面。

**练习 2**：`required_tensorf` 找不到张量时调用 `exit(1)`，这种「直接退出」的策略对分布式分片友好吗？

**参考答案**：对「正常分片」友好——分片里**本该有**的张量缺失，说明分片损坏或拼错，立即退出比继续跑出错误结果好。但正因为如此，`weights_bind` 才需要 `load_slice` 这套机制（4.3 节）来告诉它「这段层里哪些张量是本该有的」，避免把「别的 worker 的层」误判成缺失。

---

### 4.2 层权重结构 ds4_layer_weights

#### 4.2.1 概念说明

`ds4_layer_weights` 是 DeepSeek V4 **一层** transformer 所需全部权重张量的「语义指针表」。它是一个纯结构体，字段名直接对应模型语义（`attn_q_a` = query 的低秩 A 矩阵、`ffn_down_exps` = routed experts 的 down 投影……），每个字段是一个 `ds4_tensor *`。

理解它的两个关键点：

1. **编译期定死、运行期填指针**：结构体的字段集合是写死在 C 里的，对应「DeepSeek V4 这个特定架构」。`weights_bind_layer` 只负责把这些字段填上正确的指针，不改字段集合。这也是 ds4「窄而精」（见 [u1-l1](u1-l1-project-overview.md)）的体现——它不为通用模型留扩展位，字段就是这一层的全部。
2. **一张表管一层，外层用数组**：`ds4_weights` 里有一个 `ds4_layer_weights layer[DS4_MAX_LAYER]` 数组，第 `il` 层就是 `layer[il]`。绑定是对每个 `il` 独立做的。

`ds4_tensor` 本身（来自 u3-l1 的解析）长这样（[ds4.c:1599-1614](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L1599-L1614)）：

```c
typedef struct {
    ds4_str name;
    uint32_t ndim;
    uint64_t dim[DS4_MAX_DIMS];
    uint32_t type;
    uint64_t rel_offset;
    uint64_t abs_offset;     // 在 mmap 区域的绝对偏移，推理时直接 map + abs_offset 寻址
    uint64_t elements;
    uint64_t bytes;
} ds4_tensor;
```

注意 `abs_offset`：绑定填进去的是指向 `ds4_tensor` 的指针，而真正的权重字节还在 mmap 区域里，靠 `model->map + tensor->abs_offset` 访问。绑定**不搬运数据**，只整理「目录卡片」。

#### 4.2.2 核心流程

一层的绑定由 `weights_bind_layer(l, m, il)` 完成，逻辑分四块：

```
weights_bind_layer(l, m, il)
  │
  ├─ (A) 始终绑定的 attention 张量
  │      hc_attn_fn/scale/base, attn_norm,
  │      attn_q_a, attn_q_a_norm, attn_q_b,   ← MLA 低秩 query
  │      attn_kv, attn_kv_a_norm,              ← MLA 低秩 kv
  │      attn_sinks, attn_output_a, attn_output_b
  │
  ├─ (B) 若 compress_ratio != 0：绑 compressor 张量
  │      attn_compressor_ape/kv/gate/norm
  │
  ├─ (C) 若 compress_ratio == 4：再绑 indexer 张量
  │      indexer_attn_q_b, indexer_proj, indexer_compressor_*
  │
  ├─ (D) 始终绑定的 FFN/MoE 张量
  │      hc_ffn_fn/scale/base, ffn_norm,
  │      ffn_gate_inp（路由器）, ffn_exp_probs_b（可选 bias）,
  │      ffn_gate_exps / ffn_up_exps / ffn_down_exps（routed experts）,
  │      ffn_gate_shexp / ffn_up_shexp / ffn_down_shexp（shared expert）
  │
  └─ (E) 若 il < DS4_N_HASH_LAYER：绑 ffn_gate_tid2eid（哈希路由表）
```

这里的 `compress_ratio` 是本讲的一个重要变量，它决定了**这一层的 KV 缓存被压缩到什么程度**，从而决定需要哪些额外张量。它的取值由 `ds4_layer_compress_ratio(il)` 从一张编译期表 `g_ds4_compress_ratios[]` 读出（[ds4.c:625-628](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L625-L628)）。那张表的「期望形状」由模型变体决定（[ds4.c:630-644](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L630-L644)）：

```
Flash 变体：
  il < 2            → ratio 0   （前两层不压缩，密集 KV）
  il >= 2 且为偶数  → ratio 4   （带 indexer 的浅压缩）
  il >= 2 且为奇数  → ratio 128 （深度压缩，无 indexer）

PRO 变体：
  il < 2            → ratio 128
  il >= 2 且为偶数  → ratio 4
  il >= 2 且为奇数  → ratio 128
```

> 这张压缩表的含义（为什么是 4 / 128、indexer 干什么）属于 [u4-l2](u4-l2-kv-cache-design.md) 的 KV 缓存设计。本讲只需记住：**ratio 决定了一层要绑哪些可选张量**——ratio==0 不绑 compressor，ratio==4 额外绑 indexer，ratio==128 只绑 compressor 不绑 indexer。

#### 4.2.3 源码精读

先看结构体定义（[ds4.c:3016-3062](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L3016-L3062)）。`ds4_layer_weights` 字段很多，但按上面的 (A)~(E) 分组就清晰了：

```c
typedef struct {
    /* (A) attention：始终存在 */
    ds4_tensor *hc_attn_fn, *hc_attn_scale, *hc_attn_base;   // hyper-connection
    ds4_tensor *attn_norm;
    ds4_tensor *attn_q_a, *attn_q_a_norm, *attn_q_b;          // MLA 低秩 query
    ds4_tensor *attn_kv, *attn_kv_a_norm;                     // MLA 低秩 kv
    ds4_tensor *attn_sinks;
    ds4_tensor *attn_output_a, *attn_output_b;                // 输出投影

    /* (B) compressor：ratio != 0 时存在 */
    ds4_tensor *attn_compressor_ape, *attn_compressor_kv,
               *attn_compressor_gate, *attn_compressor_norm;

    /* (C) indexer：ratio == 4 时存在 */
    ds4_tensor *indexer_attn_q_b, *indexer_proj,
               *indexer_compressor_ape, *indexer_compressor_kv,
               *indexer_compressor_gate, *indexer_compressor_norm;

    /* (D) FFN/MoE：始终存在 */
    ds4_tensor *hc_ffn_fn, *hc_ffn_scale, *hc_ffn_base;
    ds4_tensor *ffn_norm;
    ds4_tensor *ffn_gate_tid2eid;     /* (E) 仅 il < N_HASH_LAYER */
    ds4_tensor *ffn_gate_inp;          // 路由器
    ds4_tensor *ffn_exp_probs_b;       // 可选 bias
    ds4_tensor *ffn_gate_exps, *ffn_up_exps, *ffn_down_exps;     // routed experts
    ds4_tensor *ffn_gate_shexp, *ffn_up_shexp, *ffn_down_shexp;  // shared expert
} ds4_layer_weights;

typedef struct {
    ds4_tensor *token_embd;                                // 词嵌入
    ds4_tensor *output_hc_base, *output_hc_fn, *output_hc_scale,
               *output_norm, *output;                       // output head
    ds4_layer_weights layer[DS4_MAX_LAYER];                // 每层一张表
} ds4_weights;
```

> 上面省略了部分重复声明并做了分组注释，**仅为示例代码（便于阅读）**，原始字段顺序见源码链接。

注意三个 routed experts 张量（`ffn_gate_exps` / `ffn_up_exps` / `ffn_down_exps`）就是 MoE 的 gate/up/down——它们每个都打包了**全部 N 个专家**（三维张量，见下面校验里的 `DS4_N_EXPERT` 那一维），而不是 N 个独立张量。这是「routed MoE 占模型体积大头」（见 [u1-l2](u1-l2-model-and-quant-strategy.md)）在结构体层面的落点：三个字段，每个都极大。

再看 `weights_bind_layer` 是怎么按 ratio 条件填这些字段的（[ds4.c:4021-4066](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L4021-L4066)，关键片段）：

```c
static void weights_bind_layer(ds4_layer_weights *l, const ds4_model *m, uint32_t il) {
    const uint32_t compress_ratio = ds4_layer_compress_ratio(il);

    /* (A) 始终绑定的 attention（全是 required_tensorf） */
    l->hc_attn_fn      = required_tensorf(m, "blk.%u.hc_attn_fn.weight", il);
    l->attn_q_a        = required_tensorf(m, "blk.%u.attn_q_a.weight", il);
    l->attn_q_b        = required_tensorf(m, "blk.%u.attn_q_b.weight", il);
    l->attn_kv         = required_tensorf(m, "blk.%u.attn_kv.weight", il);
    l->attn_output_a   = required_tensorf(m, "blk.%u.attn_output_a.weight", il);
    l->attn_output_b   = required_tensorf(m, "blk.%u.attn_output_b.weight", il);
    /* ...其余 attention 必需张量... */

    /* (B) ratio != 0 才绑 compressor */
    if (compress_ratio != 0) {
        l->attn_compressor_ape  = required_tensorf(m, "blk.%u.attn_compressor_ape.weight", il);
        l->attn_compressor_kv   = required_tensorf(m, "blk.%u.attn_compressor_kv.weight", il);
        l->attn_compressor_gate = required_tensorf(m, "blk.%u.attn_compressor_gate.weight", il);
        l->attn_compressor_norm = required_tensorf(m, "blk.%u.attn_compressor_norm.weight", il);
    }
    /* (C) ratio == 4 才绑 indexer */
    if (compress_ratio == 4) {
        l->indexer_attn_q_b = required_tensorf(m, "blk.%u.indexer.attn_q_b.weight", il);
        l->indexer_proj     = required_tensorf(m, "blk.%u.indexer.proj.weight", il);
        /* ...其余 indexer 张量... */
    }

    /* (D) FFN/MoE */
    l->ffn_gate_inp    = required_tensorf(m, "blk.%u.ffn_gate_inp.weight", il);
    l->ffn_exp_probs_b = tensor_by_namef(m, "blk.%u.exp_probs_b.bias", il);   // 可选
    l->ffn_gate_exps   = required_tensorf(m, "blk.%u.ffn_gate_exps.weight", il);
    l->ffn_up_exps     = required_tensorf(m, "blk.%u.ffn_up_exps.weight", il);
    l->ffn_down_exps   = required_tensorf(m, "blk.%u.ffn_down_exps.weight", il);
    l->ffn_gate_shexp  = required_tensorf(m, "blk.%u.ffn_gate_shexp.weight", il);
    l->ffn_up_shexp    = required_tensorf(m, "blk.%u.ffn_up_shexp.weight", il);
    l->ffn_down_shexp  = required_tensorf(m, "blk.%u.ffn_down_shexp.weight", il);

    /* (E) 仅前若干层绑哈希路由表 */
    if (il < DS4_N_HASH_LAYER) {
        l->ffn_gate_tid2eid = required_tensorf(m, "blk.%u.ffn_gate_tid2eid.weight", il);
    }
}
```

读这段代码要抓住两点：

1. **条件分支决定 NULL 还是填指针**：当 `compress_ratio == 0` 时，整个 compressor / indexer 块被跳过，对应字段保持 `weights_bind` 开头 `memset` 留下的 `NULL`。这正是「结构体字段固定、按层填一部分」的实现方式。
2. **命名模板 `"blk.%u.xxx.weight"`**：DeepSeek V4 的 GGUF 沿用 llama.cpp 的 `blk.<层号>.<张量名>.weight` 约定，`%u` 就是层号 `il`。output head 和 token_embd 没有 `blk.` 前缀，因为它们不属于某一层。

最后看校验。`weights_validate_layout` 在绑定完成后，对每个张量重新检查**类型 + 每一维大小**（[ds4.c:3580-3638](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L3580-L3638)，片段）：

```c
for (uint32_t il = layer_start; il <= layer_end; il++) {
    const ds4_layer_weights *l = &w->layer[il];
    const uint32_t ratio = ds4_layer_compress_ratio(il);
    if (!weights_layer_has_required(l, il)) { /* 缺必需张量 → exit(1) */ }

    tensor_expect_layout(l->attn_q_a,       DS4_TENSOR_Q8_0, 2, DS4_N_EMBD,  DS4_N_LORA_Q, 0);
    tensor_expect_layout(l->attn_q_b,       DS4_TENSOR_Q8_0, 2, DS4_N_LORA_Q, q_dim, 0);
    tensor_expect_layout(l->attn_kv,        DS4_TENSOR_Q8_0, 2, DS4_N_EMBD,  DS4_N_HEAD_DIM, 0);
    ...
    /* routed experts：三维，第三维是专家数 DS4_N_EXPERT */
    tensor_expect_routed_expert(l->ffn_gate_exps, 3, DS4_N_EMBD,  DS4_N_FF_EXP, DS4_N_EXPERT);
    tensor_expect_routed_expert(l->ffn_up_exps,   3, DS4_N_EMBD,  DS4_N_FF_EXP, DS4_N_EXPERT);
    tensor_expect_routed_expert(l->ffn_down_exps, 3, DS4_N_FF_EXP, DS4_N_EMBD,  DS4_N_EXPERT);
    /* shared expert：二维（只有 1 个专家） */
    tensor_expect_layout(l->ffn_gate_shexp, DS4_TENSOR_Q8_0, 2, DS4_N_EMBD,  DS4_N_FF_EXP, 0);
    ...
}
```

这段校验把 DeepSeek V4 的架构知识「焊」进了代码：

- `attn_q_a` 是 `[N_EMBD, N_LORA_Q]` 的 Q8_0 张量（低秩 A），`attn_q_b` 是 `[N_LORA_Q, q_dim]`（低秩 B），二者相乘还原完整 query 投影——这就是 MLA 低秩的形状签名。
- routed experts 是**三维** `[d0, d1, N_EXPERT]`，而 shared expert 是**二维**（单个专家），用不同的校验函数（`tensor_expect_routed_expert` 还会检查它是不是「routed expert 量化类型」，即可能被压成 IQ2_XXS/Q2_K，而 shared expert 保持 Q8_0）。这正对应 [u1-l2](u1-l2-model-and-quant-strategy.md) 讲的「只压 routed、不压 shared」的非对称量化策略——绑定校验在结构层面守住了这条线。

#### 4.2.4 代码实践

**实践目标**（即本讲主实践）：追踪 `weights_bind_layer`，列出**一个 transformer 层**绑定的全部权重张量，并按 attention / compressor / indexer / FFN 分组。

**操作步骤**：

1. 打开 [ds4.c:4021-4066](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L4021-L4066)。
2. 自选一个层号，例如 `il = 6`（Flash 变体下 `il >= 2` 且为偶数 → ratio == 4，是最「全」的一层，会触发 compressor + indexer）。
3. 按 `compress_ratio = 4` 走一遍，把每个 `required_tensorf` / `tensor_by_namef` 调用对应的**字段名**和**GGUF 名字模板**填进下表。

| 分组 | 字段名 | GGUF 名字模板 | 必需/可选 |
|------|--------|---------------|-----------|
| attention | `attn_q_a` | `blk.6.attn_q_a.weight` | 必需 |
| attention | `attn_q_b` | `blk.6.attn_q_b.weight` | 必需 |
| attention | `attn_kv` | `blk.6.attn_kv.weight` | 必需 |
| attention | `attn_output_a` / `attn_output_b` | `blk.6.attn_output_*.weight` | 必需 |
| compressor | `attn_compressor_kv` | `blk.6.attn_compressor_kv.weight` | 必需（ratio≠0） |
| indexer | `indexer_proj` | `blk.6.indexer.proj.weight` | 必需（ratio==4） |
| FFN 路由 | `ffn_gate_inp` | `blk.6.ffn_gate_inp.weight` | 必需 |
| FFN 路由 | `ffn_exp_probs_b` | `blk.6.exp_probs_b.bias` | 可选 |
| routed experts | `ffn_gate_exps` / `ffn_up_exps` / `ffn_down_exps` | `blk.6.ffn_(gate|up|down)_exps.weight` | 必需 |
| shared expert | `ffn_gate_shexp` / `ffn_up_shexp` / `ffn_down_shexp` | `blk.6.ffn_(gate|up|down)_shexp.weight` | 必需 |

**需要观察的现象**：routed experts 是三个字段（gate/up/down），shared expert 也是三个（gate/up/down），但前者打包了 N 个专家（三维），后者只有 1 个专家（二维）。

**预期结果**：你能画出一张「第 6 层绑了哪些张量」的完整清单，并解释为什么 ratio==0 的层（如 Flash 的第 0、1 层）这张清单里没有 compressor/indexer 那几行。

**待本地验证**：如果你没有模型无法运行，以上纯靠源码阅读即可完成；若想验证字段确实存在于真实 GGUF，可用 gguf-tools 的张量列举功能（见 [u11-l1](u11-l1-gguf-generation-tools.md)）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ffn_gate_exps` 是**三维** `[N_EMBD, N_FF_EXP, N_EXPERT]`，而 `ffn_gate_shexp` 是**二维** `[N_EMBD, N_FF_EXP]`？

**参考答案**：routed MoE 有 `N_EXPERT` 个候选专家，每个 token 只激活其中少数几个，所以 gate/up/down 各需要 `N_EXPERT` 份权重，打包成第三维方便按专家索引。shared expert 只有一个、所有 token 共享，所以没有「专家」这一维。这也解释了为什么 routed experts 占体积大头——它是 shared 的 `N_EXPERT` 倍。

**练习 2**：假设有人误把一个 routed expert 张量（`ffn_gate_exps`）存成了 Q8_0 而不是项目要求的 IQ2_XXS，绑定阶段会报错吗？在哪报？

**参考答案**：绑定阶段（`weights_bind_layer`）只查名字、不查类型，所以不会立刻报。但紧接着的 `weights_validate_layout` 会调用 `tensor_expect_routed_expert`，它通过 `tensor_is_routed_expert_type(t->type)` 检查类型是否属于「routed expert 量化类型」（IQ2_XXS/Q2_K 等），Q8_0 不在其中，于是打印 `expected a routed expert quant type` 并 `exit(1)`。这就是「绑定只认名字、校验才认形状」的分工。

---

### 4.3 load_slice 与分布式层裁剪的衔接

#### 4.3.1 概念说明

DeepSeek V4 PRO 是 1.6T 参数的模型，单机放不下。ds4 支持把它**沿层切开**，每个进程只加载一段层（例如 README 里 PRO Q4 的两片：`pro-q4-layers00-30` 和 `pro-q4-layers31-output`）。这种「只加载一段层」的能力，落点就在 `weights_bind` 的 `load_slice` 参数上。

`load_slice` 改变绑定行为的三件事：

1. **层范围**：只绑 `[load_layer_start, load_layer_end]` 这一段，区间外的 `layer[il]` 保持 `memset` 的全 `NULL`。
2. **token embedding**：只有负责第 0 层的进程（`start == 0`）才需要 `token_embd`，其它进程把它当可选。
3. **output head**：只有负责「最后一层 + 输出」的进程才需要 output head；coordinator（协调者）角色会把它当「可选」来探测。

这三条合起来，让同一份 `weights_bind` 代码既能服务「单机整模型」，也能服务「分布式分片」。

#### 4.3.2 核心流程

在 `ds4_engine_open` 里，调用 `weights_bind` 之前会先把这几个参数算好（[ds4.c:25588-25637](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25588-L25637)）：

```
读 opt->load_slice / load_layer_start / end / load_output   （来自 ds4_engine_options）

if (分布式且设置了 layers 区间):
    load_slice        = true
    load_layer_start  = distributed.layers.start
    load_layer_end    = has_output ? UINT32_MAX : distributed.layers.end
    load_output       = distributed.layers.has_output
    load_output_optional = (role == COORDINATOR)      # 协调者只探测，不强制

weights_bind(&e->weights, &e->model, load_slice, start, end, load_output, optional)
```

注意 `load_layer_end == UINT32_MAX` 是一个哨兵值：在 `weights_bind` 内部它会被还原成 `DS4_N_LAYER - 1`（[ds4.c:4086](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L4086)），表示「一直到最后一层」。这样配置层时，拥有 output head 的 worker 不必知道总层数，写 `UINT32_MAX` 即可。

#### 4.3.3 源码精读

配置侧的字段在 `ds4_engine_options`（[ds4.h:116-120](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L116-L120)）：

```c
    bool load_slice;
    uint32_t load_layer_start;
    uint32_t load_layer_end;
    bool load_output;
    ds4_distributed_options distributed;
```

分布式子结构 `ds4_distributed_layers` 描述「我要哪一段层、要不要 output head」（[ds4.h:73-78](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L73-L78)）：

```c
typedef struct {
    uint32_t start;
    uint32_t end;
    bool has_output;     // 这一进程是否拥有 output head
    bool set;            // 是否显式设置了 layers 区间
} ds4_distributed_layers;
```

调用点的实际换算（[ds4.c:25588-25602](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25588-L25602)）：

```c
bool load_slice = opt->load_slice;
uint32_t load_layer_start = opt->load_layer_start;
uint32_t load_layer_end = opt->load_layer_end;
bool load_output = opt->load_output;
bool load_output_optional = false;
if (opt->distributed.role != DS4_DISTRIBUTED_NONE &&
    opt->distributed.layers.set)
{
    load_slice = true;
    load_layer_start = opt->distributed.layers.start;
    load_layer_end = opt->distributed.layers.has_output ?
                     UINT32_MAX : opt->distributed.layers.end;
    load_output = opt->distributed.layers.has_output;
    load_output_optional = opt->distributed.role == DS4_DISTRIBUTED_COORDINATOR;
}
```

然后在 `weights_bind` 内部，`load_slice` 控制层范围和 token_embd 的必需性（[ds4.c:4080-4098](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L4080-L4098)）：

```c
uint32_t start = 0;
uint32_t end = DS4_N_LAYER - 1u;
bool require_token_embd = true;
if (load_slice) {
    if (load_layer_start >= DS4_N_LAYER) ds4_die("invalid model load layer slice");
    start = load_layer_start;
    end = load_layer_end == UINT32_MAX ? DS4_N_LAYER - 1u : load_layer_end;  // 哨兵还原
    if (end >= DS4_N_LAYER || end < start) ds4_die("invalid model load layer slice");
    require_token_embd = (start == 0);   // 只有负责第 0 层的进程需要 token_embd
} else {
    require_output = true;
    optional_output = false;             // 非分片：output head 必须齐全
}

if (require_token_embd) {
    w->token_embd = required_tensor(m, "token_embd.weight");
} else {
    w->token_embd = model_find_tensor(m, "token_embd.weight");   // 分片中间进程：可能没有
}
```

这段有几个精巧之处：

- **`require_token_embd = (start == 0)`**：token embedding 只在最前面那个 worker 上是必需的；中间 worker 的分片里根本不含 `token_embd`，所以用 `model_find_tensor`（返回 `NULL` 也不报错）。
- **非分片强制 output head**：`else` 分支把 `require_output = true`，保证单机整模型加载时 output head 缺失会被立即抓出来。
- **校验也随之收窄**：`weights_validate_layout(w, start, end, ...)` 只校验 `[start, end]` 这段层，不会因为「别的 worker 的层在本进程是 NULL」而误报。注释把这条意图写得很明白（[ds4.c:3543-3545](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L3543-L3545)）：

> For distributed sliced GGUFs, only the advertised local layer range is required; token embedding and output head are validated when present.

#### 4.3.4 代码实践

**实践目标**：理解「同一个 `weights_bind`，单机和分布式走不同分支」。

**操作步骤**：

1. 阅读 README 关于 PRO 分片的说明（[README.md:116-125](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L116-L125)），看到 `pro-q4-layers00-30`（前半，无 output）与 `pro-q4-layers31-output`（后半，含 output）两个分片。
2. 打开 [ds4.c:4070-4106](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L4070-L4106)，分别模拟两种调用：
   - **进程 B（layers 31..末层，has_output=true）**：`load_slice=true, start=31, end=UINT32_MAX, require_output=true`。走一遍：`require_token_embd` 是真是假？哪些 `layer[il]` 被绑？
   - **进程 A（layers 0..30，has_output=false）**：`load_slice=true, start=0, end=30, require_output=false, optional_output=false`。走一遍：`token_embd` 必需吗？output head 会绑吗？

**需要观察的现象**：

- 进程 A：`start==0` → `require_token_embd=true` → 绑 `token_embd`；`has_output=false` → output head 全是 `NULL`；只绑 `layer[0..30]`。
- 进程 B：`start==31` → `require_token_embd=false` → `token_embd` 用 `model_find_tensor`（该分片里没有，得 `NULL`）；`has_output=true` → 绑 output head；绑 `layer[31..末层]`。

**预期结果**：两个进程的 `ds4_weights` 合起来才等于一个完整模型——A 出 token_embd + 前若干层，B 出后若干层 + output head。这就是「层切分映射到 weights_bind」的实际效果，也是 [u9-l3](u9-l3-distributed-architecture.md) 分布式架构的加载侧基础。

#### 4.3.5 小练习与答案

**练习 1**：为什么拥有 output head 的 worker 通常用 `load_layer_end = UINT32_MAX` 而不是写一个具体层数？

**参考答案**：这样配置时不必知道模型总层数 `DS4_N_LAYER`（它是运行期从 GGUF 元数据读出来的，配置层时不一定方便拿到）。`UINT32_MAX` 作为哨兵，在 `weights_bind` 内部被还原成 `DS4_N_LAYER - 1`，既安全又解耦了「配置层」与「知道总层数」。

**练习 2**：coordinator 角色把 `load_output_optional` 设为 true，意味着什么？

**参考答案**：coordinator 是分布式里的协调者，它自己不一定持有 output head（output head 在某个 worker 上），但它可能需要「探测」本地分片里有没有 output head。`optional=true` 让 `weights_bind_output` 用 `model_find_tensor`（找不到返回 `NULL`）而不是 `required_tensor`（找不到退出），这样 coordinator 即使没有 output head 也能正常启动。随后的 `weights_have_partial_output_head` 检查保证「要么全有、要么全无」，防止半套 output head 蒙混过关。

---

## 5. 综合实践

把本讲三块知识串起来：**模拟一次完整的「分片绑定」，画出该进程最终的 `ds4_weights` 布局图。**

设定：Flash 变体，分布式两进程，进程 A 负责 `layers 0..3` 且 `has_output=false`，进程 B 负责 `layers 4..末层` 且 `has_output=true`。假设 `DS4_N_HASH_LAYER = 3`（前 3 层有哈希路由表）。

任务：

1. 对进程 A 和进程 B，分别列出 `weights_bind` 执行后：
   - `token_embd` 字段是「指针」还是「NULL」？
   - `output` / `output_norm` 等 output head 字段是「指针」还是「NULL」？
   - `layer[0]` 到 `layer[末层]` 中，哪些被绑定了（非 NULL）、哪些是 NULL？
   - 对于被绑定的层，标出它的 `compress_ratio`（用 [ds4.c:630-644](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L630-L644) 的 Flash 规则：il<2→0，偶→4，奇→128），并据此判断它有没有 indexer 字段。
   - 哪些层会有 `ffn_gate_tid2eid`？（提示：`il < DS4_N_HASH_LAYER`）
2. 画出两个进程的 `ds4_weights` 布局对照图（可用表格：行=层号，列=进程 A / 进程 B，单元格=「已绑 / NULL」）。
3. 验证：两个进程合起来，是否覆盖了完整的 `token_embd` + 全部层 + output head，且没有重叠？

**参考结论**：

- 进程 A：`token_embd` = 指针（start==0）；output head 全 NULL；`layer[0..3]` 已绑，`layer[4..]` 全 NULL。其中 layer[0]、layer[1] 的 ratio==0（无 compressor/indexer），layer[2] ratio==128（有 compressor 无 indexer），layer[3] ratio==4（compressor + indexer 全有）。layer[0..2] 有 `ffn_gate_tid2eid`（il<3），layer[3] 没有。
- 进程 B：`token_embd` = NULL（start≠0）；output head = 指针（has_output）；`layer[0..3]` 全 NULL，`layer[4..末层]` 已绑。layer[4] ratio==4，layer[5] ratio==128……交替；这些层都 `il >= 3`，所以都没有 `ffn_gate_tid2eid`。
- 合并后：`token_embd` 由 A 提供，output head 由 B 提供，层 0..末层被 A、B 无缝拼接覆盖，无重叠。这正是分布式层切分能工作的充要条件。

> 待本地验证：本实践是源码阅读 + 推演型，无需运行模型。若你想在真实两机分布式上验证，可参考 [u9-l3](u9-l3-distributed-architecture.md) 的启动命令（需要 PRO 模型与两台机器）。

## 6. 本讲小结

- **绑定的本质是一次性翻译**：`weights_bind` 把 GGUF 里「按字符串扁平排列」的张量，一次性填进 `ds4_weights` / `ds4_layer_weights` 这两张「语义指针表」，之后推理全程用 `layer->attn_q_a` 这样的字段直访，字符串查询退役。
- **查找族正交于绑定族**：`model_find_tensor`（线性扫描）→ `required_tensor`（缺失则退出）/ `tensor_by_namef`（缺失返回 NULL）；命名前缀 `required_` 即代表「必需」，这是阅读 `weights_bind_layer` 的钥匙。
- **一层 = attention + 条件 compressor/indexer + MoE**：`weights_bind_layer` 按 `compress_ratio`（0 / 4 / 128）决定绑不绑 compressor 和 indexer；routed experts 是三维（含 N 个专家）、shared expert 是二维（单专家），体现「只压 routed、不压 shared」。
- **绑定只认名字，校验才认形状**：`weights_bind_layer` 只查名字，`weights_validate_layout` 紧随其后检查每个张量的类型和每一维大小，把 DeepSeek V4 的架构知识焊进校验。
- **load_slice 让同一份代码服务分布式**：`weights_bind` 通过层范围、`require_token_embd = (start==0)`、output head 的 required/optional 三条规则，让每个进程只绑自己的那一段层，单机和分布式共用一套绑定逻辑。

## 7. 下一步学习建议

- **下一讲 [u3-l3](u3-l3-tokenizer-and-chat-template.md)** 讲分词器与聊天模板：权重绑好后，输入文本如何被切成 token、再喂进 `token_embd`。建议接着读。
- **量化块的数学**：本讲多次提到 routed experts 被压成 IQ2_XXS / Q2_K，但没展开块结构。这块在 [u3-l4](u3-l4-quantization-formats.md)，配合 `gguf-tools/quants.c` 阅读。
- **前向计算如何用这张表**：本讲只整理了「指针表」，attention 低秩、MoE 路由到底怎么算，在 [u4-l1](u4-l1-deepseek-v4-architecture.md)（架构）和 [u4-l3](u4-l3-generation-and-sampling.md)（生成）。
- **compress_ratio 的 KV 含义**：本讲把 ratio 当作「决定绑哪些字段」的开关，它真正的含义（KV 缓存压缩、长上下文）在 [u4-l2](u4-l2-kv-cache-design.md)。
- **分布式加载的全貌**：本讲只讲了绑定侧的层切分，协议、流水线、worker 掉线恢复在 u9 单元（尤其 [u9-l3](u9-l3-distributed-architecture.md) 与 [u9-l4](u9-l4-distributed-protocol.md)）。
