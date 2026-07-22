# RadixAttention 与基数树缓存

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 **RadixAttention** 相比传统 PagedAttention 在「前缀复用」上的优势，以及它为什么能自动提升多请求的缓存命中率。
- 读懂基数树（Radix Tree）的两个核心数据结构：`RadixKey`（键）与 `TreeNode`（节点），并理解「按公共前缀合并、按分歧分裂」的树形演化过程。
- 掌握 `RadixCache` 类的三个核心 API：`match_prefix`（找最长命中前缀）、`insert`（把新算出的 KV 写入树）、`evict`（显存不够时按策略淘汰叶子），以及 `inc_lock_ref`/`dec_lock_ref` 的引用计数保护机制。
- 理解 `RadixAttention.forward` 如何把树里查到的 KV 索引（`out_cache_loc`）交给具体的注意力后端，从而把「缓存层」与「计算层」解耦。

本讲对应大纲里的最小模块：**RadixCache 类**、**TreeNode / RadixKey**、**RadixAttention.forward**。

## 2. 前置知识

在进入正题前，先用通俗语言建立两个直觉。

**直觉一：为什么 LLM 推理需要 KV 缓存。** 自回归模型每生成一个 token，都要「重新看一遍」之前所有 token。如果每一步都把前面的全部重算一次，计算量会随序列长度平方增长。所以推理框架会把每层每头的 Key/Value（合称 **KV**）存在显存里复用，这就是 KV 缓存。注意，KV 缓存存的是「已经算过的中间结果」，下一次只需要算新 token 的 KV 追加进去即可。

**直觉二：很多请求共享公共前缀。** 想象你在跑一个客服机器人，每条请求都以同一段「系统提示词 + few-shot 示例」（可能几千 token）开头。如果每条请求都为这段相同的前缀重新计算一遍 KV，那就太浪费了。**前缀缓存（prefix caching）** 的目标就是：一旦某段前缀的 KV 算过一次，后续任何以这段开头的请求都直接复用，只算分歧之后的部分。

SGLang 用一棵**基数树（Radix Tree）**来自动管理这种前缀共享，并把这套机制命名为 **RadixAttention**。基数树的特点是：**公共前缀在树里只存一份**，从根到某节点路径上拼出的 token 序列，就是该节点对应的缓存内容。它本质上是一棵「按 token 序列前缀压缩」的多叉树（类似字典树 Trie 的压缩变种——Patricia Trie）。

> 本讲承接 u3-l2。在那里你已经认识了 `Req` 上的 `prefix_indices`（命中前缀的 KV 索引）、`last_node`（命中到的树节点）、`cache_protected_len` 等字段，以及 `ScheduleBatch` 上的 `out_cache_loc`。本讲就打开这些字段背后的那棵树。

## 3. 本讲源码地图

本讲涉及三个核心文件：

| 文件 | 作用 |
| --- | --- |
| [python/sglang/srt/mem_cache/radix_cache.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py) | 基数树的完整实现：`RadixKey`、`TreeNode`、`RadixCache`。是本讲的主角。 |
| [python/sglang/srt/mem_cache/base_prefix_cache.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/base_prefix_cache.py) | 前缀缓存的抽象基类 `BasePrefixCache`，定义统一接口和参数对象（`MatchPrefixParams`、`InsertParams`、`MatchResult` 等）。 |
| [python/sglang/srt/layers/radix_attention.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/layers/radix_attention.py) | 注意力层 `RadixAttention`，是模型里「用 KV 缓存做注意力」的统一入口，把请求委托给底层注意力后端。 |

一句话理解三者关系：`BasePrefixCache` 定下「该有哪些操作」的契约 → `RadixCache` 用一棵基数树实现这些操作 → `RadixAttention` 在前向计算时消费这棵树产出的索引。

## 4. 核心概念与源码讲解

### 4.1 基数树的数据结构：RadixKey 与 TreeNode

#### 4.1.1 概念说明

基数树由两类对象组成：

- **键（RadixKey）**：一段 token id 序列（加上可选的 `extra_key` 命名空间标签）。它代表「从父节点到本节点」这段路径上的 token。`RadixKey` 封装了「两段 token 序列的最长公共前缀有多长」「取第一个逻辑单元作为子节点查找键」等操作。
- **节点（TreeNode）**：树的节点。每个节点持有自己的 `key`（一段 token 序列）和 `value`（这段 token 对应的 KV 缓存索引，是一个 `torch.Tensor`）。根节点是空序列，所有缓存都挂在它下面。

为什么需要 `extra_key`？因为有时两段 token 完全一样但**不该共享** KV——例如不同的 LoRA 适配器、不同的采样盐值。`extra_key` 给键加了一个命名空间，`extra_key` 不同的条目永远不共享前缀节点。

#### 4.1.2 核心流程

一棵基数树的演化遵循三条规则：

1. **插入时沿公共前缀下沉**：从根开始，用 `RadixKey.child_key()` 取当前序列的首个逻辑单元，在子节点字典里查找；命中就进入子节点，把已匹配的前缀「消费掉」继续比对剩余序列。
2. **遇到部分匹配就分裂**：如果子节点存的 key 比当前剩余公共前缀长（即「在某个节点内部产生分歧」），就把该节点**分裂**成「公共前缀部分 + 分歧部分」两个节点，让分歧点暴露成一个树边界。
3. **剩余序列挂成新叶子**：消费完所有公共前缀后，如果还有剩余 token，就创建一个新叶子节点保存它们。

匹配（`match_prefix`）走的是前两条规则的前半段：沿树尽可能深地走，走到分歧或序列耗尽为止，沿途收集各节点的 `value`（KV 索引）。

#### 4.1.3 源码精读

先看键 `RadixKey` 的核心字段与构造：

[radix_cache.py:60-80 — RadixKey 的字段定义](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L60-L80)：`token_ids` 是原始 token 序列，`extra_key` 是命名空间（如 `lora_id`），`is_bigram`/`limit` 是给 EAGLE 投机解码和切片优化的标志位，初学可先忽略。

两个最关键的方法：

[radix_cache.py:162-196 — RadixKey.match 计算公共前缀长度](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L162-L196)：返回本键与 `other` 的最长公共前缀长度，并按 `page_size` 向下取整（分页对齐）。注意它没有用「逐 token 的 Python 循环」，而是用了**指数搜索（exponential search）+ 二分**：先成倍扩大窗口用 C 层切片比较快速跳过完全相同的长前缀，定位到分歧所在的小窗口后再二分——这是为了让长公共前缀的匹配不退化为 O(n) 的纯 Python 循环。

[radix_cache.py:198-208 — RadixKey.child_key 生成子节点查找键](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L198-L208)：取首个 `page_size` 个逻辑单元拼成可哈希的 dict 键，并带上 `extra_key` 命名空间。这就是父节点在 `children` 字典里找子节点用的「索引」。

再看节点 `TreeNode`：

[radix_cache.py:217-243 — TreeNode 的字段](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L217-L243)：关键字段含义：

- `children`：`defaultdict(TreeNode)`，子节点字典，键是 `child_key()`。
- `parent` / `key` / `value`：父节点、本段 token 序列、本段对应的 KV 索引张量。
- `lock_ref`：**引用计数**。>0 表示有在用请求引用，不能被淘汰（见 4.2）。
- `last_access_time` / `creation_time`：访问与创建时间，给 LRU 淘汰用。
- `hit_count`：命中次数，给优先级感知淘汰用。
- `host_value` / `host_ref_counter`：HiCache（把 KV 卸载到主机内存）相关，本讲先不展开，留到 u4-l4。
- `priority`：优先级，用于 priority-aware 淘汰（承接 u3-l3 的 `Req.priority`）。

[radix_cache.py:245-247 — evicted 属性](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L245-L247)：`value is None` 就算「已被淘汰」。也就是说，节点可以从树里逻辑保留（结构还在）但物理 KV 已被释放——这是 HiCache 卸载/重载的基础。

[radix_cache.py:276-277 — __lt__ 定义比较序](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L276-L277)：按 `last_access_time` 比较，这让 `TreeNode` 能直接丢进堆里做 LRU 淘汰（见 4.2 的 `evict`）。

#### 4.1.4 代码实践

**实践目标**：亲手用 `RadixCache.create_simulated()`（一个不需要真实显存池、专门给模拟/测试用的工厂方法）跑通一次插入与匹配，观察树结构如何演化。

**操作步骤**：

1. 进入仓库根目录，直接运行 `radix_cache.py` 文件末尾自带的 `__main__` 演示：

```bash
python python/sglang/srt/mem_cache/radix_cache.py
```

2. 这段代码见 [radix_cache.py:818-833 — 自带的演示入口](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L818-L833)，它依次插入 5 段序列，再对 `[1,2,3,13,14]` 做一次 `match_prefix`。

**需要观察的现象**：`pretty_print()` 打印出的树形结构。注意 `[1,2,3]` 和 `[1,2,4,5]` 会共享 `[1,2]` 这个前缀节点（在树里只存一份），然后在 `[3]` 与 `[4,5]` 处分叉成两个子节点。

**预期结果**（树形示意，节点标注为「key 长度 + token 片段」）：

```
root
└── [1,2]            # 三条序列共享的公共前缀
    ├── [3]          # 来自第一条 [1,2,3]
    └── [4,5]        # 来自 [1,2,4,5] 和 [1,2,4,5,6,7] 的公共部分
        └── [6,7]    # [1,2,4,5,6,7] 多出来的部分
[8,9,10,11,12]       # 与上面无公共前缀，另起一根下的子树
```

对 `[1,2,3,13,14]` 匹配时，会命中 `[1,2] → [3]`，返回长度 3 的命中前缀。具体打印格式以本地实际输出为准。

**注意**：若运行环境缺少 torch 等依赖会报 ImportError，此为环境问题而非代码问题；可在「待本地验证」前提下，仅做源码阅读型练习：对照 [radix_cache.py:706-759 的 _insert_helper](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L706-L759) 手工推导上面的树形。

#### 4.1.5 小练习与答案

**练习 1**：在 `[1,2,3]` 已经插入之后，再插入 `[1,2,3]`（完全重复），树会多出一个新节点吗？为什么？

> **答案**：不会新增节点。`_insert_helper` 会沿 `[1,2,3]` 完全走完已存在的路径（`prefix_len == len(node.key)`），key 被消费到空，直接返回，沿途只更新 `last_access_time` 和 `hit_count`（见 [radix_cache.py:740-744](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L740-L744)）。这正是前缀复用的体现。

**练习 2**：`RadixKey.match` 为什么不用简单的 `for i in range(n): if t0[i]!=t1[i]: break`？

> **答案**：那样在长公共前缀（几千 token 完全相同）上会是 O(n) 的纯 Python 循环，开销大。源码用指数搜索成倍跳步 + C 层切片比较，把「找第一个分歧点」降为 O(log n) 次 Python 层操作（见 [radix_cache.py:169-187](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L169-L187)），对命中率高的大规模服务很关键。

### 4.2 RadixCache 类：前缀匹配、插入与淘汰

#### 4.2.1 概念说明

`RadixCache` 是基数树的「管理者」，它对外暴露 `BasePrefixCache` 定义的统一接口（见 [base_prefix_cache.py:211-270 的抽象方法](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/base_prefix_cache.py#L211-L270)）。它把树结构与底层的显存池（`token_to_kv_pool_allocator` 负责 GPU 上 KV 张量的物理分配）解耦：

- 树里的 `value` 存的是**逻辑索引**（KV 在显存池里的位置编号）；
- 物理显存的真正分配/释放由 `token_to_kv_pool_allocator` 管（这部分细节在 u4-l2）。

这样设计的好处是：同一份物理 KV 可以被树里多个逻辑引用指向（前缀共享），引用计数（`lock_ref`）决定它能不能被回收。

`RadixCache` 还维护两个全局账本：

- `evictable_size_`：可被淘汰的 token 总数（`lock_ref==0` 的叶子节点贡献）。
- `protected_size_`：被引用保护、不可淘汰的 token 总数（`lock_ref>0` 的节点贡献）。

调度器在 prefill 前会查 `evictable_size()` 来判断「还有多少缓存可以腾出来给新请求」（承接 u3-l3 的 `PrefillAdder` 预算评估）。

#### 4.2.2 核心流程

一次请求的缓存交互流程（与调度器配合）：

```
请求到达
   │
   ├─ match_prefix(key)         # 1. 找最长命中前缀 → 拿到 device_indices 与 last_node
   │     返回: 命中的 KV 索引 + 命中到的树节点
   │
   ├─ 前向只算「分歧后」的 token  # 2. 命中部分复用，只对新 token 算 KV（省计算）
   │
   ├─ inc_lock_ref(last_node)   # 3. 给命中路径加引用锁，防止推理期间被淘汰
   │
   ├─ [生成中/结束]
   │
   ├─ cache_unfinished_req      # 4a. 未完成（如 chunked prefill 中途）：把已算 KV 写入树
   │   或 cache_finished_req    # 4b. 已完成：把 input+output 全部 KV 写入树，供后续复用
   │
   └─ dec_lock_ref(last_node)   # 5. 释放引用锁，节点重新变得可淘汰
```

淘汰则由调度器在显存紧张时显式触发 `evict(num_tokens)`：在 `evictable_leaves` 里按淘汰策略（默认 LRU）选最该删的叶子，释放其物理 KV 并从树里摘除。

#### 4.2.3 源码精读

**构造与重置。** [radix_cache.py:281-309 — RadixCache.__init__](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L281-L309)：注入显存池、页大小、淘汰策略等。注意它继承自 `SessionRadixCacheMixin, KVCacheEventMixin, BasePrefixCache`（[radix_cache.py:280](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L280)），会话缓存和事件上报是通过 mixin 混入的横切能力。

[radix_cache.py:331-353 — reset 初始化根节点](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L331-L353)：根节点 key 为空、`lock_ref=1`（永远不被淘汰），并预置一个空的 `_empty_match_result` 供 miss 时返回，避免反复分配空张量。

**match_prefix：找最长命中前缀。** [radix_cache.py:355-413 — match_prefix](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L355-L413)：先做 bigram 视图转换与 `page_aligned`（按页对齐截断），再调 `_match_prefix_helper` 沿树下行，最后把沿途节点的 `value` 用 `torch.cat` 拼成 `device_indices` 返回。核心下行逻辑在：

[radix_cache.py:650-674 — _match_prefix_helper 沿树下行](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L650-L674)：循环用 `child_key` 找子节点，用 `RadixKey.match` 算公共前缀；若公共前缀比子节点 key 短（分歧落在子节点内部），就调 `_split_node` 分裂子节点暴露分歧边界后停下；否则消费已匹配前缀继续下行。注意它沿途刷新 `last_access_time`——这是 LRU 的「最近访问」依据。

**_split_node：节点分裂。** [radix_cache.py:676-696 — _split_node](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L676-L696)：把原 `child` 拆成 `new_node`（公共前缀部分，继承原优先级与 lock_ref）+ 原 `child`（分歧后部分）。两侧的 `value` 各 `.clone()` 切片，互不影响。这一步「不复制物理 KV、只重新切分索引视图」，所以分裂很廉价。

**insert：写入新 KV。** [radix_cache.py:415-435 — insert](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L415-L435)：做 bigram/页对齐后调 `_insert_helper`。注意 [radix_cache.py:429-430](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L429-L430) 的兜底：若调用方没传 `value`（如测试），就用 token id 本身当 value，这正是 `create_simulated` 能跑的原因。

[radix_cache.py:706-759 — _insert_helper 核心写入循环](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L706-L759)：沿已有路径消费公共前缀并累加 `total_prefix_length`；遇到部分匹配同样分裂；走完公共前缀后若还有剩余 key，就 `TreeNode(priority=priority)` 新建叶子、`value.clone()` 存入、加入父节点的 `children`，并 `evictable_size_ += len(key)`、`_update_leaf_status` 更新叶子集合、`_record_store_event` 上报缓存事件。

**两个业务封装：cache_finished_req / cache_unfinished_req。** 调度器不直接调 `insert`，而是调这两个面向 `Req` 的高层方法。

[radix_cache.py:437-489 — cache_finished_req](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L437-L489)：把 `req.origin_input_ids + req.output_ids` 拼成完整序列，从 `req_to_token_pool` 取出这些 token 占用的 KV 索引，组装成 `RadixKey` 后 `insert` 进树。关键细节在 [radix_cache.py:471-474](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L471-L474)：插入后把「树里原本就有的重复索引」`free` 掉——因为同一份物理 KV 现在由树统一持有，请求私有副本可以释放，避免内存泄漏。最后 `dec_lock_ref(req.last_node)` 释放该请求在 prefill 时加的引用锁。

[radix_cache.py:490-556 — cache_unfinished_req](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L490-L556)：用于 chunked prefill（长 prompt 分块，承接 u7-l2）等「请求还没算完」的场景。它只把已算出的 `get_fill_ids()` 写入树，然后**重新 match_prefix 拿回最新索引**更新 `req.prefix_indices` / `req.last_node` / `req.cache_protected_len`，并完成引用锁的「旧节点 dec、新节点 inc」交接（[radix_cache.py:541-542](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L541-L542))。

**evict：按策略淘汰。** [radix_cache.py:565-592 — evict](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L565-L592)：把 `evictable_leaves` 按 `eviction_strategy.get_priority(node)` 建堆，循环弹出优先级最低的叶子，`free` 掉它的物理 KV、`_delete_leaf` 从树摘除；若某叶子的父节点因此变成无子且 `lock_ref==0` 的空节点，就把父节点也推进堆里继续淘汰（[radix_cache.py:585-587](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L585-L587))。默认策略是 LRU，`get_priority` 返回的就是 `last_access_time`（配合 `TreeNode.__lt__`）。

**lock_ref：引用计数保护。** [radix_cache.py:594-607 — inc_lock_ref](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L594-L607) 与 [radix_cache.py:609-628 — dec_lock_ref](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L609-L628)：从某节点一路向根遍历，`inc` 时把 `0→1` 的节点从「可淘汰」搬进「保护」（`evictable_size_` 减、`protected_size_` 加），`dec` 时反向。这保证了一条请求正在用的前缀路径不会被 `evict` 回收——这是 RadixAttention 正确性的关键。

#### 4.2.4 代码实践

**实践目标**：用 `create_simulated` 构造三条有部分公共前缀的「prompt」，跟踪每次 insert 后的命中与重算。

**操作步骤**：把下面这段「示例代码」（非项目原有，仅为演示 `create_simulated` API）存成 `trace_radix.py` 运行（需在能 import sglang 的环境）：

```python
# 示例代码：用模拟缓存观察前缀命中
from array import array
from sglang.srt.mem_cache.radix_cache import RadixCache, RadixKey
from sglang.srt.mem_cache.base_prefix_cache import InsertParams, MatchPrefixParams

tree = RadixCache.create_simulated()  # 不需要真实显存池

prompts = [
    array("q", [10, 11, 12, 13]),        # prompt A
    array("q", [10, 11, 12, 99, 100]),   # prompt B: 与 A 共享 [10,11,12]
    array("q", [10, 11, 55, 56]),        # prompt C: 与 A/B 只共享 [10,11]
]

for i, toks in enumerate(prompts):
    before = tree.match_prefix(MatchPrefixParams(key=RadixKey(token_ids=toks)))
    hit = len(before.device_indices)
    tree.insert(InsertParams(key=RadixKey(token_ids=toks)))
    print(f"prompt {i}: tokens={list(toks)} 命中前缀长度={hit} 需重算长度={len(toks)-hit}")
tree.pretty_print()
```

**需要观察的现象**：

- prompt A：第一次插入，命中 0，全部 4 个 token 重算。
- prompt B：命中 `[10,11,12]` 长度 3，只有 `[99,100]` 两个 token 需重算。
- prompt C：注意——它会命中 `[10,11]`（长度 2）而非 `[10,11,12]`，因为第三个 token `55` 与树里的 `12` 分歧，需要重算 `[55,56]`。**这里会触发一次 `_split_node`**：原本的 `[10,11,12]` 节点被分裂成 `[10,11]` + `[12]`，让 `55` 这个分歧点成为树边界。

**预期结果**：打印的三行命中长度依次为 `0 / 3 / 2`；`pretty_print` 显示根下有一个 `[10,11]` 公共节点，下挂 `[12,13]`（A）、`[12,...]`（B 侧）、`[55,56]`（C）等子节点。若环境无法运行，此为「待本地验证」，可对照 [radix_cache.py:706-759](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L706-L759) 手工推导同样结论。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `evict` 只从 `evictable_leaves` 里挑，而不是任意节点？

> **答案**：因为只有叶子节点（无未淘汰子节点的节点）且 `lock_ref==0` 才能安全释放。`_update_leaf_status`（[radix_cache.py:790-803](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L790-L803)）负责维护这个集合：一个节点只要还有在用的子节点或被引用，就不在可淘汰集合里。淘汰中间节点会破坏其下所有子节点共享的前缀，所以必须自下而上（叶子先删，删空后父节点晋升为新叶子，见 [radix_cache.py:585-587](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L585-L587)）。

**练习 2**：`cache_finished_req` 在 insert 之后为什么要 `free(kv_indices[...:result.prefix_len])`？

> **答案**：见 [radix_cache.py:471-474](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L471-L474)。`insert` 返回的 `prefix_len` 是「树里原本就命中、这次又用同样物理 KV 重复登记」的长度。这部分物理 KV 现在由树统一持有，请求自己那份私有索引副本就该归还显存池，否则同一份 KV 被计两份引用，造成内存泄漏。

**练习 3**：若一条请求正在 decode（生成中），它的前缀节点会被淘汰吗？

> **答案**：不会。请求在 prefill 命中前缀后会对 `last_node` 调 `inc_lock_ref`（[radix_cache.py:594-607](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L594-L607)），沿途路径 `lock_ref>0`，`_update_leaf_status` 会把它们移出 `evictable_leaves`。直到请求结束 `cache_finished_req` 里 `dec_lock_ref` 才重新变为可淘汰。

### 4.3 RadixAttention.forward：把缓存索引接入注意力计算

#### 4.3.1 概念说明

`RadixAttention` 是模型里每一层注意力子模块的统一壳。它本身**不计算注意力**，而是：

1. 持有该层的元信息（头数、头维度、layer_id 等）；
2. 在前向时，把 query/key/value 和当前批次的 `ForwardBatch` 交给**具体的注意力后端**（FlashInfer、FlashAttention、Triton、TRT-LLM 等，见 u5-l3）去算。

它和 RadixCache 的连接点是 `ForwardBatch.out_cache_loc`：调度器在 prefill/decode 前，已经把每个 token「应该把 KV 写到/读到显存池的哪个位置」算好填进了 `out_cache_loc`（这个位置正是 RadixCache 命中前缀 + 新分配槽拼接而来，承接 u4-l2 的内存池）。注意力后端在算注意力时，就是靠 `out_cache_loc` 去显存池里**读取命中的历史 KV**、**写入新 token 的 KV**。

所以「RadixAttention」这个名字的真正含义是：**注意力计算与基数树缓存是一体两面**——缓存层（RadixCache）负责「哪些 KV 已有、索引在哪」，计算层（RadixAttention + 后端）负责「用这些索引做真正的注意力」。

#### 4.3.2 核心流程

`RadixAttention.forward` 的分发逻辑（简化）：

```
forward(q, k, v, forward_batch, save_kv_cache, ...)
   │
   ├─ 整理 k/v 的形状（按头数 reshape）
   │
   ├─ 若处于「可分段 CUDA Graph / torch.compile」捕获态(extend 场景):
   │     走 unified_attention_with_output 系列自定义算子
   │     （内部仍调 attn_backend.forward，但套了图分段/PCG 对齐逻辑）
   │
   └─ 否则(如 decode 或非捕获态):
         直接 get_attn_backend().forward(q,k,v,self,forward_batch,...)
         —— 注意力后端用 forward_batch.out_cache_loc 读写 KV 池
```

注意力后端内部会：对 query 的新 token 计算 K/V，按 `out_cache_loc` 写入 KV 池；同时从池里取出该序列此前所有 token（包括 RadixCache 命中的前缀）的 K/V，做标准 scaled-dot-product attention。

#### 4.3.3 源码精读

**类定义与元信息。** [radix_attention.py:91-141 — RadixAttention.__init__](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/layers/radix_attention.py#L91-L141)：保存 `tp_q_head_num`/`tp_k_head_num`/`head_dim`/`scaling`/`layer_id` 等，并按 `quant_config` 创建量化权重。它是一个普通 `nn.Module`，权重由所属模型在外部绑定（SGLang 的模型把 Q/K/V 投影与 `RadixAttention` 分开，前者算出 q/k/v 张量，后者负责注意力）。

**forward 的形状整理。** [radix_attention.py:153-160 — k/v reshape](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/layers/radix_attention.py#L153-L160)：把展平的 k/v 按 `(token数, kv头数, 头维度)` reshape，为后端调用做准备。

**两条分发路径。** [radix_attention.py:162-280 — forward 主体分发](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/layers/radix_attention.py#L162-L280)：当处于 extend（prefill）且存在 tc-piecewise forward context（即可分段 CUDA Graph 捕获态）时，走 `unified_attention_with_output` / `unified_attention_with_output_and_lse` 等 `@register_custom_op` 自定义算子（[radix_attention.py:392-429](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/layers/radix_attention.py#L392-L429)），目的是让注意力能作为「分段点」参与 torch.compile 全图捕获；否则在 [radix_attention.py:271-280](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/layers/radix_attention.py#L271-L280) 直接 `get_attn_backend().forward(...)`。

**真正调用后端、且与缓存索引交互的核心。** [radix_attention.py:283-389 — _unified_attention_with_output_impl](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/layers/radix_attention.py#L283-L389)：这是自定义算子的实现，集中体现了「RadixAttention 如何消费 `out_cache_loc`」：

- [radix_attention.py:346-354](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/layers/radix_attention.py#L346-L354)：把 `forward_batch.out_cache_loc` **临时收窄到真实 token 数**（PCG 捕获时张量被 padding 到固定桶大小，这里切掉 padding 尾巴），再把预分配输出 `_attn_output` 也切到对应长度，喂给后端。
- [radix_attention.py:356-364](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/layers/radix_attention.py#L356-L364)：调用 `get_attn_backend().forward(...)`，后端正是靠这份收窄后的 `out_cache_loc` 去 KV 池读写命中前缀与新 token 的 KV。
- [radix_attention.py:365](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/layers/radix_attention.py#L365)：调用结束后把 `out_cache_loc` **还原**，保证 `ForwardBatch` 状态不被污染。
- [radix_attention.py:377-384](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/layers/radix_attention.py#L377-L384)：`_zero_padded_pcg_tail` 把 padding 区可能残留的 `torch.empty` 垃圾（NaN/Inf）清零，防止它经残差/MoE 路由/allreduce 污染真实结果。

> 这里的 `out_cache_loc` 就是 RadixCache 与 RadixAttention 之间的「数据接口」：RadixCache 产出命中前缀的索引，调度器把它们和新分配的槽拼成 `out_cache_loc`（细节在 u4-l2、u5-l2），RadixAttention 的后端再按这个索引去物理 KV 池读写。

#### 4.3.4 代码实践

**实践目标**：用源码阅读型实践，把「RadixCache 命中索引」与「RadixAttention 消费索引」串成一条完整调用链，定位二者的接口字段。

**操作步骤**：

1. 在 [radix_cache.py:437-489 的 cache_finished_req](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/radix_cache.py#L437-L489) 里找到「从 `req_to_token_pool.req_to_token` 取出 KV 索引」这一步，确认树里的 `value` 存的就是显存池索引。
2. 在 [base_prefix_cache.py:155-190 的 MatchResult](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/mem_cache/base_prefix_cache.py#L155-L190) 里确认 `match_prefix` 返回的 `device_indices` 就是这些索引的拼接。
3. 在 [radix_attention.py:346-354](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/layers/radix_attention.py#L346-L354) 找到 `forward_batch.out_cache_loc` 被收窄并交给后端的位置。

**需要观察的现象**：三个文件分别对应「写入索引」「读取索引」「消费索引」三个角色，三者通过「int64 索引张量」这个统一货币流通。

**预期结果**：你能画出这样一条数据流：

```
RadixCache.match_prefix → MatchResult.device_indices
        (命中前缀的 KV 池索引)
                    │
        调度器拼上新分配槽 → ForwardBatch.out_cache_loc
                    │
RadixAttention.forward → attn_backend.forward(out_cache_loc)
        (按索引读写物理 KV 池，做注意力)
```

> 中间「调度器如何把命中索引拼成 `out_cache_loc`」这一步本讲作为黑盒，留到 u4-l2（内存池）与 u5-l2（ForwardBatch）打开。当前只需理解两端契约即可。

#### 4.3.5 小练习与答案

**练习 1**：`RadixAttention.forward` 里为什么要对 `out_cache_loc` 先收窄、调用后还原，而不是直接用？

> **答案**：因为 PCG（可分段 CUDA Graph）捕获/回放时，张量被静态 padding 到固定桶大小（`num_tokens` 可能大于真实 token 数 `raw_num_tokens`），后端只能处理真实 token。所以临时切到 `[:real_query_num_tokens]`，调用完再还原，既满足后端的形状校验，又不破坏 `ForwardBatch` 的全局状态（见 [radix_attention.py:346-365](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/layers/radix_attention.py#L346-L365)）。

**练习 2**：如果一个模型有 32 层，会有 32 个 `RadixAttention` 实例吗？它们的 `layer_id` 各是多少？

> **答案**：是的，每个 decoder layer 持有一个 `RadixAttention`，`layer_id` 从 0 到 31。`layer_id` 用于在 forward context 的 `attention_layers[layer_id]` 里定位「当前是哪一层」（见 [radix_attention.py:307](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/layers/radix_attention.py#L307)），因为所有层共用同一份 KV 池，必须靠 layer_id 区分各层各自的 KV。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个贯穿任务。

**任务**：给定三条具有部分公共前缀的 prompt（用 token id 表示），手工构建 RadixCache 的树结构，并标注每次 insert 后哪些 token 命中缓存、哪些需要重新计算；再说明这些命中索引最终如何流到 `RadixAttention`。

**输入**（沿用 4.2.4 的示例，便于对照运行）：

```
A = [10, 11, 12, 13]
B = [10, 11, 12, 99, 100]
C = [10, 11, 55, 56]
```

**要求**：

1. **画树**：依次 insert A、B、C 后，画出最终的基数树，标出每个节点的 key 片段。特别注意 insert C 时触发的**节点分裂**：原本 `[10,11,12]` 会被分裂成 `[10,11]` + `[12]`。
2. **标命中与重算**：对每条 prompt 填表：

   | Prompt | 命中前缀（来自哪条已缓存请求） | 命中长度 | 需重算 token |
   |--------|------------------------------|---------|-------------|
   | A      | 无                           | 0       | 10,11,12,13 |
   | B      | A 的 [10,11,12]              | 3       | 99,100      |
   | C      | A/B 的 [10,11]（分裂后）      | 2       | 55,56       |

3. **接计算层**：用一句话说明，B 请求命中长度 3 的那 3 个 token 的 KV 索引，最终通过哪个字段（`ForwardBatch.out_cache_loc`）被 `RadixAttention` 的后端读取复用，从而**这 3 个 token 不再重新跑前向**。
4. **验证**：用 4.2.4 的 `create_simulated` 示例代码实际运行，比对命中长度是否为 `0 / 3 / 2`。

**参考答案要点**：

- 最终树形：root → `[10,11]` → 三个子节点 `[12,13]`（A 路径，B 共享其 `[12]` 后另挂 `[99,100]`）、`[55,56]`（C）。其中 `[12]` 与 `[13]` 因 C 的插入被分裂开。
- 第 3 问：命中 token 的 KV 已在树中（A 首次计算时写入），`match_prefix` 返回它们的 `device_indices`；调度器把这些索引拼进 `out_cache_loc`，注意力后端据此直接从 KV 池读取，故 B 不重算 `[10,11,12]` 的前向。
- 第 4 问：运行结果应与表格一致（环境不具备时标注「待本地验证」）。

## 6. 本讲小结

- **RadixAttention = 基数树前缀缓存 + 统一注意力壳**：用一棵按 token 前缀压缩的多叉树（`RadixCache`）自动复用公共前缀的 KV，再由 `RadixAttention` 把缓存索引接入注意力计算。
- **数据结构两层**：`RadixKey`（键，封装前缀匹配与子节点查找）+ `TreeNode`（节点，持有 key/value/lock_ref/时间戳/优先级）。公共前缀在树里只存一份，分歧处用 `_split_node` 分裂暴露边界。
- **三个核心 API**：`match_prefix`（找最长命中前缀，返回 KV 索引）、`insert`（写入新 KV，分裂/挂叶）、`evict`（按 LRU 等策略淘汰 `evictable_leaves`）。`cache_finished_req`/`cache_unfinished_req` 是面向 `Req` 的高层封装。
- **引用计数保护**：`inc_lock_ref`/`dec_lock_ref` 让正在用的前缀路径不可淘汰，保证推理正确性；`evictable_size_`/`protected_size_` 是调度器做预算评估的账本。
- **缓存与计算的接口**是 `ForwardBatch.out_cache_loc` 这个 int64 索引张量：RadixCache 产出命中索引，调度器拼成 `out_cache_loc`，`RadixAttention.forward` 的后端据此读写物理 KV 池。
- **统一契约**由 `BasePrefixCache` 定义（`MatchPrefixParams`/`InsertParams`/`MatchResult` 等），`RadixCache` 是其树形实现；这让 HiCache（u4-l4）等其他实现可以替换底层而不动上层。

## 7. 下一步学习建议

- **u4-l2（KV 内存池）**：本讲把 `token_to_kv_pool_allocator` 当黑盒，下一讲打开它，看 `ReqToTokenPool` 与 `TokenToKVPool` 如何物理分配 KV、`out_cache_loc` 到底如何由命中索引拼出来。
- **u4-l3（前缀缓存接口与淘汰策略）**：深入 `BasePrefixCache` 的统一接口与 `evict_policy` 的多种淘汰策略（LRU/优先级感知），把本讲的 `evict`/`lock_ref` 放到更大语境里。
- **u4-l4（HiCache）**：看 `TreeNode.host_value` 那套字段如何用于把 KV 卸载到主机内存/磁盘，实现更长上下文。
- **回看 u3-l3**：现在你已经懂了 `num_matched_prefix_tokens`、`evictable_size` 的来源，可以重新理解 `PrefillAdder` 的预算评估与 LPM 缓存感知调度为何能提升命中率。
