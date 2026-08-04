# 前缀缓存（Prefix Caching）

## 1. 本讲目标

本讲在 u4-l4（PagedAttention 与 KV 缓存管理）已经建立的「按 block 分配显存、用 `ref_cnt` 管理共享」的基础上，回答一个关键问题：**当多个请求共享同一段前缀（例如同一个 system prompt）时，vLLM 如何避免重复计算这段前缀的 KV 缓存？**

学完本讲，你应该能够：

- 理解前缀缓存（prefix caching）带来的「计算 + 显存」双重节省，以及它为何几乎是「免费午餐」。
- 看懂 vLLM 的「块哈希链」机制：如何用一个哈希值唯一指纹一个前缀，从而实现 \(O(1)\) 的块级命中查找。
- 掌握 `KVCacheBlock` 上的 `block_hash` 字段、`cached_block_hash_to_block` 映射，以及一次缓存命中（cache hit）从「查找 → touch → 复用」的完整流程。
- 说清缓存何时被写入、何时被驱逐（eviction）、何时被显式失效（`cache_salt` 隔离 / `reset_prefix_cache`）。

## 2. 前置知识

本讲需要你已经掌握 u4-l4 的核心结论：

- KV 缓存被切成固定大小的 **block**（`block_size` 默认 16 个 token），物理 block 由 `BlockPool` 统一持有。
- 每个请求用一张「块表（block table）」把逻辑块号映射到物理 `block_id`。
- `KVCacheBlock` 用 **引用计数 `ref_cnt`** 记录当前有多少请求在用它；`ref_cnt` 归零的块回到 `FreeKVCacheBlockQueue` 空闲队列，可被重新分配。
- `KVCacheManager` 是调度器的高层入口，核心方法是 `get_computed_blocks` 与 `allocate_slots`。

补充几个术语：

- **prefill（预填充）**：处理输入 prompt、计算其 KV 缓存的过程，算力密集。
- **哈希（hash）**：把任意长度的数据映射成固定长度的「指纹」，常用于快速比较两份数据是否相同。
- **LRU（Least Recently Used，最近最少使用）**：一种缓存淘汰策略，空间不够时优先丢掉最久没被访问的条目。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `vllm/v1/core/kv_cache_utils.py` | 前缀缓存的「核心数据结构与算法」：`BlockHash` 类型、`hash_block_tokens` 块哈希函数、`request_block_hasher` 请求级哈希器、`BlockHashListWithBlockSize` 跨 block-size 视图。 |
| `vllm/v1/core/block_pool.py` | `BlockPool`：维护 `cached_block_hash_to_block`（哈希→块）映射，负责缓存写入 `cache_full_blocks`、命中查找 `get_cached_block`、`touch`、驱逐 `_maybe_evict_cached_block`、`free_blocks`。 |
| `vllm/v1/core/kv_cache_manager.py` | 调度器高层入口：`get_computed_blocks`（算命中）、`allocate_slots`（含 touch 与缓存）、`prefix_cache_lookup_enabled`、`record_prefix_cache_stats`。 |
| `vllm/v1/core/kv_cache_coordinator.py` + `single_type_kv_cache_manager.py` | `find_longest_cache_hit`：按 block 哈希逐块查表，返回最长的命中前缀。 |
| `vllm/v1/request.py` | `Request.block_hashes` 字段与 `update_block_hashes`：请求在创建/追加 token 时即时维护自己的块哈希链。 |
| `vllm/v1/core/sched/scheduler.py` | 调度器在调度 WAITING 请求时调用 `get_computed_blocks` 与 `allocate_slots`，把命中结果折算进 `num_computed_tokens`。 |
| `vllm/config/cache.py` | `enable_prefix_caching`（默认 `True`）与 `prefix_caching_hash_algo`（默认 `sha256`）。 |
| `docs/design/prefix_caching.md` | 官方设计文档，含数据结构、操作流程与端到端示例（本讲多处引用其图解思想）。 |

---

## 4. 核心概念与源码讲解

### 4.1 前缀缓存：动机与「省」的到底是什么

#### 4.1.1 概念说明

在很多真实场景里，大量请求会共享同一段前缀：

- 每个请求都带同一句 `system prompt`（例如「你是一个有用的助手……」）。
- RAG（检索增强生成）场景里，同一段长文档被反复喂给模型，后面接不同的提问。
- Agent / 多轮对话里，历史上下文在前缀中重复。

对这些请求，朴素做法是每次都重新跑一遍前缀的 prefill，重新计算并写入同样的 KV。这既浪费 GPU 算力，也浪费显存（同一份 KV 被存了好几份）。

**前缀缓存**的核心想法很简单：把已经算好、且写满一个 block 的 KV 缓存**保留下来**，当新请求的前缀与之相同时，直接复用这些 block，跳过对应的 prefill 计算。官方设计文档开宗明义：

> Prefix caching kv-cache blocks is a popular optimization … Since prefix caching is almost a free lunch and won't change model outputs …

它之所以是「免费午餐」有两个原因：

1. **不改变输出**：被复用的 block 内容与新请求算出来的完全一致（只要前缀 token 相同），所以生成结果不会变。
2. **自然契合 PagedAttention**：block 既是分配粒度，也正好是前缀复用的粒度——复用一个 block 就等于跳过 `block_size` 个 token 的 prefill。

它省的是两样东西：

- **计算**：命中部分不再进 GPU 做注意力前向。
- **显存**：多个请求共享同一物理 block，而不是各存一份（这正是 u4-l4 讲的 `ref_cnt` 共享机制的直接受益者）。

#### 4.1.2 核心流程

前缀缓存把一个请求的生命周期改造成了这样：

```text
新请求进入调度器(WAITING)
   │
   ▼
get_computed_blocks(request)        # 用块哈希链查表
   │  返回 (命中块列表, 命中 token 数)
   ▼
num_computed_tokens = 命中 token 数   # 命中部分视为「已算」
   │
   ▼
allocate_slots(...)                 # touch 命中块(防驱逐) + 分配新块
   │
   ▼
只对未命中的后缀做 prefill          # 省掉了前缀的计算
   │
   ▼
新填满的 block → cache_full_blocks   # 写回缓存，供后续请求复用
```

一句话：**前缀缓存 = 用一个哈希表把「满块」登记起来，新请求先查表，命中即复用、未命中才算。**

#### 4.1.3 源码精读

前缀缓存是否开启由 `CacheConfig.enable_prefix_caching` 控制，V1 中**默认就是开启的**：

[vllm/config/cache.py:93-95](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/cache.py#L93-L95) —— `enable_prefix_caching: bool = True`，同时默认哈希算法为 `sha256`。注意这个默认值意味着大多数 V1 部署天然享受前缀缓存，无需额外配置。

调度器真正调用查表的位置，是调度 WAITING 请求时：

[vllm/v1/core/sched/scheduler.py:745-766](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L745-L766) —— 当 `request.num_computed_tokens == 0`（即新请求尚未开始算）时，调用 `kv_cache_manager.get_computed_blocks(request)` 拿到本地命中块与命中 token 数，并把它折算成 `num_computed_tokens`。注意 `get_computed_blocks` 内部把 `max_cache_hit_length` 设为 `request.num_tokens - 1`（见下文 4.3.3），注释解释了原因：即便全部命中，也要重算最后一个 token 以拿到 logits。

#### 4.1.4 代码实践

> **实践目标**：用官方示例直观体会「前缀缓存让结果不变、但前缀只算一次」。

操作步骤（有 GPU 时）：

1. 打开示例 [examples/features/automatic_prefix_caching/prefix_caching_offline.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/examples/features/automatic_prefix_caching/prefix_caching_offline.py)。
2. 它先建一个**不开**前缀缓存的 `LLM`，对 `[prefix + p1, prefix + p2, ...]` 做一次 `generate`；再建一个 `enable_prefix_caching=True` 的 `LLM`，先用 `prefix + p1` 预热（让前缀的 KV 进缓存），再对全部 prompt 做一次 `generate`。
3. 运行：

   ```bash
   .venv/bin/python examples/features/automatic_prefix_caching/prefix_caching_offline.py
   ```

需要观察的现象：

- 两次生成的文本**完全相同**（脚本末尾会打印 `Generated answers are the same: True`）——验证「缓存不改变输出」。
- 第二次（开启缓存且预热过）整体耗时明显更短——前缀那部分 token 没有真正进 GPU 算。

预期结果：若无可运行 GPU 环境，则「待本地验证」；但「结果相同」这一点由设计保证（同一份 KV 内容），不依赖运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么说前缀缓存是「免费午餐」，而不是像量化那样可能带来精度损失？

> 参考答案：被复用的 block 存的就是该前缀 token 序列对应的 KV 张量，与新请求重新算出来的在数值上完全一致，因此输出不变；它只是跳过了重复的计算与重复的显存存储，不引入任何近似。

**练习 2**：前缀缓存「省」的两样东西分别是什么？

> 参考答案：① 计算量（命中部分跳过 prefill 前向）；② 显存（多个请求共享同一物理 block，依赖 `ref_cnt > 1`）。

---

### 4.2 块哈希链：用一个哈希值指纹整个前缀

#### 4.2.1 概念说明

要让「查表复用」可行，必须能快速回答：**这个 block 的内容，以前算过没有？** 最自然的做法是给每个满块算一个哈希，存进一张「哈希→块」的表。但有个陷阱：单独对「块内 token」做哈希不够——两个不同的前缀可能在某一个块里恰好出现相同的 token 片段，却属于完全不同的上下文。

vLLM 的解法是**链式哈希（chained hash）**，类似 Merkle 树的思想：每个块的哈希不仅依赖自己的 token，还依赖**父块的哈希**。这样一来，第 \(i\) 个块的哈希其实「吸收」了从第 0 块到第 \(i\) 块的全部信息，能够唯一指纹「截至第 \(i\) 块的整个前缀」。

官方文档用一张图说明（块大小为 4 个词）：

```text
Block 1: |<--- block tokens ---->|
Block 2: |<------- prefix ------>| |<--- block tokens --->|
Block 3: |<------------------ prefix -------------------->| |<--- block tokens ---->|
```

也就是说：

- Block 1 的哈希只由它自己的 token 决定。
- Block 2 的哈希由「Block 1 的哈希 + Block 2 的 token」决定。
- Block 3 的哈希由「Block 2 的哈希 + Block 3 的 token」决定（而 Block 2 的哈希又含 Block 1……）。

#### 4.2.2 核心流程

块哈希的计算公式可以写成递推：

\[
H_i = \mathrm{hash}\big(H_{i-1},\ \mathrm{tokens}_i,\ \mathrm{extra\_keys}_i\big)
\]

其中 \(H_0\) 的「父哈希」用全局种子 `NONE_HASH` 填充。`extra_keys` 是为了让块在特殊场景下依然唯一：

- **多模态**：同一串占位符 token（`<image>` 展开成若干 placeholder）背后可能是不同的图片，所以要把图片哈希 + 块内偏移塞进 `extra_keys`。
- **LoRA**：不同 LoRA 适配器对同一 token 算出的 KV 不同，要把 LoRA 名字塞进去。
- **cache_salt**：多租户隔离，只在第一个块（`start_token_idx == 0`）注入。
- **prompt_embeds**：直接传入的 embedding 也要参与哈希。

这套哈希在请求**创建时**和**每次追加 token 时**即时增量计算（只算新填满的块），由 `Request` 持有。

#### 4.2.3 源码精读

`BlockHash` 只是一个 `bytes` 的别名，用 `NewType` 区分开，避免和普通字节串混淆：

[vllm/v1/core/kv_cache_utils.py:41-44](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_utils.py#L41-L44) —— `BlockHash = NewType("BlockHash", bytes)`。

块哈希函数 `hash_block_tokens` 把「父哈希 + 本块 token 元组 + 额外键」一起喂给哈希函数，这正是上面递推式的直接实现：

[vllm/v1/core/kv_cache_utils.py:576-603](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_utils.py#L576-L603) —— 注意它把 token 转成 `tuple` 再哈希，且当 `parent_block_hash` 为空时退化为全局种子 `NONE_HASH`（该种子在 `init_none_hash` 中初始化，未设 `PYTHONHASHSEED` 时用随机值，见 [kv_cache_utils.py:99-114](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_utils.py#L99-L114)）。

`extra_keys` 的组装集中在 `generate_block_hash_extra_keys`：

[vllm/v1/core/kv_cache_utils.py:538-573](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_utils.py#L538-L573) —— 它合并 LoRA 名、多模态 `(mm_hash, offset_in_block)`、`cache_salt`（仅首块）、prompt embeds 哈希四类来源。多模态部分 `_gen_mm_extra_hash_keys` 还会把「图片在块内的起始偏移」纳入，这样同一张图落在不同块的占位符序列里，哈希也不同（见 [kv_cache_utils.py:430-494](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_utils.py#L430-L494)）。

请求级的哈希器 `get_request_block_hasher` 返回一个闭包，它只算「新填满的块」并链式追加，体现了增量计算：

[vllm/v1/core/kv_cache_utils.py:671-728](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_utils.py#L671-L728) —— 关键点：起始位置 `start_token_idx = len(request.block_hashes) * hash_block_size`（已算过多少块就从哪继续），`while` 循环里每填满一个 `hash_block_size` 的块就 `hash_block_tokens` 一次，并用前一块哈希作下一块的父哈希。

`Request` 在创建和追加 token 时调用这个哈希器：

[vllm/v1/request.py:204-209](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/request.py#L204-L209) 与 [vllm/v1/request.py:262-265](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/request.py#L262-L265) —— `block_hashes: list[BlockHash]` 初值为空，`update_block_hashes` 把哈希器新算出的块哈希 `extend` 进去。也就是说 `request.block_hashes[i]` 就是「截至第 `i` 个块的整个前缀」的指纹。

> 设计文档补充：默认算法 `sha256` 从 v0.11 起保证抗碰撞；可选 `sha256_cbor`（跨语言可复现）、`xxhash`/`xxhash_cbor`（更快但非密码学安全，多租户需谨慎）。详见 [docs/design/prefix_caching.md:21-31](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/prefix_caching.md#L21-L31)。

#### 4.2.4 代码实践

> **实践目标**：动手验证「父哈希参与，使得同样 token 在不同前缀下哈希不同」。

这是「源码阅读 + 心算型」实践，无需 GPU：

1. 阅读函数 `hash_block_tokens`（[kv_cache_utils.py:576-603](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_utils.py#L576-L603)）。
2. 假设块大小为 4，两个请求的前 4 个 token 不同、但第 5–8 个 token 恰好相同（都是 `[X,Y,Z,W]`）。
3. 按 \(H_i = \mathrm{hash}(H_{i-1}, \mathrm{tokens}_i, \cdot)\) 推演：请求 A 的第 2 块哈希 = `hash(H_A1, (X,Y,Z,W))`，请求 B 的第 2 块哈希 = `hash(H_B1, (X,Y,Z,W))`，而 \(H_{A1} \neq H_{B1}\)（因为前 4 个 token 不同）。

预期结果：两块内容相同但哈希不同，因此**不会**误命中。这解释了为什么单看块内 token 不够、必须链入父哈希。

> 想真正跑代码，可用 Python 自行调用 `hashlib.sha256` 模拟：取两个不同的 `H_A1/H_B1`，对同一 `(X,Y,Z,W)` 元组分别哈希，比较结果是否不同（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `extra_keys` 里要包含多模态的「块内偏移 `offset - start_token_idx`」，而不仅是图片哈希？

> 参考答案：一张大图会被展开成很多占位符 token，可能横跨多个块，且同一张图在不同请求里可能落在不同的块边界上。仅用图片哈希会让「相同图片、不同块内位置」的块哈希相同而误命中；纳入块内偏移可区分它们。

**练习 2**：`Request.block_hashes[i]` 到底指纹的是什么？

> 参考答案：它指纹的是「从第 0 块到第 `i` 块为止的整个前缀」（因为哈希链层层吸收父块哈希）。所以拿 `block_hashes[i]` 去查表，等价于问「这个长度为 `(i+1)*hash_block_size` 的前缀，以前算过没有」。

---

### 4.3 KVCacheBlock 与缓存命中机制

#### 4.3.1 概念说明

有了块哈希，还需要两样东西才能完成「命中」：

1. **一个可被缓存的块**：`KVCacheBlock` 上要能挂一个哈希，表示「我这个物理块当前缓存了哪个前缀」。
2. **一张查表**：从「块哈希」反向查到「物理块」，即 `cached_block_hash_to_block`。

注意区分两个方向：

- `Request.block_hashes`：**请求**知道自己前缀的哈希序列（前向：token → 哈希）。
- `cached_block_hash_to_block`：**BlockPool** 记录「哪些哈希已经被某个物理块缓存了」（反向：哈希 → 块）。

一次命中查找，就是用请求的 `block_hashes` 逐个去 `cached_block_hash_to_block` 里问「这个哈希在不在」，连续命中直到第一个 miss，得到**最长命中前缀**。这与 u4-l4 讲的 `ref_cnt` 共享直接挂钩：命中后通过 `touch` 把命中块的 `ref_cnt + 1`，让它不被驱逐，从而被本请求复用。

#### 4.3.2 核心流程

```text
get_computed_blocks(request)
   │  max_cache_hit_length = num_tokens - 1
   ▼
coordinator.find_longest_cache_hit(request.block_hashes, max_len)
   │  对每个 block_hashes[i]:
   │     用 make_block_hash_with_group_id(h, group_id) 组合键
   │     在 cached_block_hash_to_block 中查
   │     命中 → 收集该块，继续下一块
   │     未命中 → 停止
   ▼
返回 (命中块元组, 命中 token 数)
   │
   ▼  (之后在 allocate_slots 中)
block_pool.touch(命中块)   # ref_cnt+=1；ref_cnt 从 0→1 的块移出空闲队列
```

`group_id` 的存在是为了支持混合模型（hybrid）：不同 KV 缓存组（full attention / sliding window / mamba）各有自己的逻辑块序列，同一个 `BlockHash` 要搭配 `group_id` 才能定位到「哪一组的物理块」。`make_block_hash_with_group_id` 把哈希字节与 4 字节大端 `group_id` 拼成一个紧凑键。

#### 4.3.3 源码精读

`KVCacheBlock` 在 u4-l4 已介绍其 `block_id` / `ref_cnt` / 链表指针；本讲聚焦其**哈希相关字段**：

[vllm/v1/core/kv_cache_utils.py:117-138](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_utils.py#L117-L138) —— `_block_hash` 只在「块写满并被缓存」时才赋值；`_block_hash_num_tokens` 记录该哈希覆盖的 token 数（用于部分→满的晋升判断）。

赋值与清除哈希的方法：

[vllm/v1/core/kv_cache_utils.py:148-162](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_utils.py#L148-L162) —— `set_block_hash` 断言「块上还没有哈希」，`reset_hash` 在驱逐时把哈希清掉。

`BlockPool` 在构造时就建好查表 `cached_block_hash_to_block`（空）：

[vllm/v1/core/block_pool.py:183-191](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/block_pool.py#L183-L191) —— 同时预留了 `null_block`（块 id=0 的占位块，`is_null=True`，永不缓存）。

单块哈希查表 `get_cached_block`：

[vllm/v1/core/block_pool.py:198-223](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/block_pool.py#L198-L223) —— 对每个 `group_id` 拼 `BlockHashWithGroupId` 再 `get_one_block`；任意一组 miss 即整体返回 `None`。

最长命中前缀 `find_longest_cache_hit`（在 `single_type_kv_cache_manager` 里按注意力类型实现，基类签名在 `kv_cache_coordinator`）：

[vllm/v1/core/kv_cache_coordinator.py:369-377](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_coordinator.py#L369-L377) 是抽象接口；[vllm/v1/core/single_type_kv_cache_manager.py:547-593](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/single_type_kv_cache_manager.py#L547-L593) 的 docstring 说明：返回「所有组都命中的公共前缀」，且命中长度需对齐到 `alignment_tokens`（通常是 `block_size`）。当 prefix caching 关闭时，`KVCacheCoordinatorNoPrefixCache.find_longest_cache_hit` 直接返回空（[kv_cache_coordinator.py:424-432](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_coordinator.py#L424-L432)）。

`get_computed_blocks` 把上面串起来，并处理「全命中也要留最后一 token 重算」：

[vllm/v1/core/kv_cache_manager.py:229-295](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_manager.py#L229-L295) —— 关键三行：

```python
if not self.prefix_cache_lookup_enabled(request):
    return self.empty_kv_cache_blocks, 0, 0
max_cache_hit_length = request.num_tokens - 1   # 留最后一个 token 重算拿 logits
computed_blocks, num_new_computed_tokens, num_uncached = (
    self.coordinator.find_longest_cache_hit(request.block_hashes, max_cache_hit_length))
```

`prefix_cache_lookup_enabled` 的判定在 [kv_cache_manager.py:214-216](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_manager.py#L214-L216)：`enable_caching and not request.skip_reading_prefix_cache`——即请求也可通过 `skip_reading_prefix_cache` 单独禁用读缓存（例如要 prompt logprobs 时）。

命中的块随后在 `allocate_slots` 里被 `touch`：

[vllm/v1/core/block_pool.py:702-717](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/block_pool.py#L702-L717) —— `touch` 对每个命中块：若 `ref_cnt == 0`（说明它正待在空闲队列里，是驱逐候选），先把它从空闲队列移除，再 `ref_cnt += 1`。这正是「命中即防止被驱逐、并提高共享计数」的关键。

#### 4.3.4 代码实践

> **实践目标**：跟踪一次命中从「查表」到「`touch` 提升引用计数」的完整路径，理解 `ref_cnt` 与命中的关系。

源码阅读型实践：

1. 在 `get_computed_blocks`（[kv_cache_manager.py:229-295](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_manager.py#L229-L295)）里确认：返回的 `blocks` 来自 `create_kv_cache_blocks(computed_blocks)`，而 `computed_blocks` 来自 `find_longest_cache_hit`。
2. 在 `allocate_slots`（[kv_cache_manager.py:344-357](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_manager.py#L344-L357)）里找它对命中块调 `touch` 的位置（提示：在「Touch the computed blocks」步骤，对 `new_computed_blocks` 调 `block_pool.touch`）。
3. 结合 `touch`（[block_pool.py:702-717](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/block_pool.py#L702-L717)）回答：

   - 假设块 7 已被请求 A 缓存且 `ref_cnt == 0`（A 已结束、块被释放回队列但哈希还在）。请求 B 的前缀命中块 7。`touch` 之后块 7 的 `ref_cnt` 变成多少？它还在空闲队列里吗？

预期结果：`ref_cnt` 由 0 → 1，且块 7 被**移出**空闲队列（不再是被驱逐候选）。请求 B 复用块 7 而无需重新 prefill，也无需复制内容。

> 若无法实际运行，明确标注「待本地验证」；但 `ref_cnt` 变化与「移出空闲队列」是 `touch` 源码的直接语义，可由阅读确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `get_computed_blocks` 要把 `max_cache_hit_length` 设成 `num_tokens - 1` 而不是 `num_tokens`？

> 参考答案：若整条 prompt 全部命中、一个 token 都不剩，模型就没有「需要前向计算的位置」来产出 logits。留出最后一个 token 强制重算，才能拿到下一个 token 的 logits（注释里还指出这会触发整块重算，因为 `num_computed_tokens` 要按 block 对齐）。

**练习 2**：`make_block_hash_with_group_id` 为什么要拼 `group_id`？

> 参考答案：混合模型里同一逻辑前缀在 full-attention 组和 sliding-window 组分别对应**不同的物理块**（它们有各自的块表）。只用块哈希查表无法区分要复用哪一组的块，拼上 `group_id` 才能把哈希精确路由到对应组的物理块。

---

### 4.4 缓存的写入、驱逐与失效

#### 4.4.1 概念说明

前两节解决了「查」与「复用」，本节回答三个收尾问题：

1. **什么时候写**：一个块只有**写满 token** 后才会被登记进缓存（设计文档明确：*We only cache full blocks*）。写满发生在 prefill 推进或 decode 追加 token 的过程中。
2. **什么时候被驱逐（eviction）**：缓存块是「坐在」空闲队列里的——`ref_cnt==0` 但哈希还在。当需要分配新块、而空闲队列的队首恰好是个**已缓存**的块时，就要把它驱逐（清哈希、移出查表）才能复用其物理空间。驱逐顺序由 LRU 队列决定。
3. **什么时候显式失效**：
   - `cache_salt`：多租户隔离，不同 salt 的请求互不复用。
   - `reset_prefix_cache`：RLHF 更新权重后、或压测前手动清空。

关键直觉：缓存块和空闲块**共用同一个物理 block 池**。一个块要么「正在被请求使用（`ref_cnt>0`）」，要么「空闲但可能仍带哈希（待驱逐的 LRU 候选）」。前缀缓存并不额外占显存——它只是让本会被回收的块「晚一点被覆盖、且尽量复用」。

#### 4.4.2 核心流程

**写入（cache_full_blocks）**：当一个块新填满，调用 `cache_full_blocks`：

```text
对每个新满块 blk (第 i 个):
   block_hash = request.block_hashes[num_cached_blocks + i]
   若 blk 已有哈希(部分→满晋升): 先 _remove_cached_block_hashes(blk)
   _insert_block_hash(block_hash+group_id, blk, num_tokens)
     ↳ blk.block_hash 为空 → 直接 set_block_hash
     ↳ 否则记入 cached_block_hashes_by_block(处理重复块)
```

**驱逐（_maybe_evict_cached_block）**：分配新块时，若拿到的块带着哈希：

```text
get_new_blocks → 从空闲队列队首弹出
   对每个块: _maybe_evict_cached_block(block)
     ↳ _remove_cached_block_hashes: 从查表删键 + block.reset_hash()
```

**释放（free_blocks）**：请求结束时，把它的块按驱逐优先级放回队列尾部；`ref_cnt` 归零的块（带哈希）成为新的 LRU 候选。设计文档指出释放时按**逆序**入队——请求最后一个块哈希覆盖的 token 最多、最不易被别人复用，应排到队首优先淘汰。

#### 4.4.3 源码精读

`cache_full_blocks` 是写入入口：

[vllm/v1/core/block_pool.py:225-299](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/block_pool.py#L225-L299) —— 关键循环：先 `resolve_block_hashes` 把请求的哈希视图对齐到本组 `block_size`（见下文 `BlockHashListWithBlockSize`），再对每个新满块算 `block_hash_with_group_id`，处理「部分→满晋升」后 `_insert_block_hash` 登记进查表。

`_insert_block_hash` 处理「同一哈希被多个块缓存（重复块）」的情况：

[vllm/v1/core/block_pool.py:607-627](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/block_pool.py#L607-L627) —— 若块本身还没哈希就直接 `set_block_hash`；若已有（重复块），则把额外的 `(hash+group_id)` 记到 `cached_block_hashes_by_block[block_id]` 里，等请求释放时统一去重。

驱逐发生在分配新块时：

[vllm/v1/core/block_pool.py:647-700](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/block_pool.py#L647-L700) —— `get_new_blocks` 从队首弹块，开启缓存时对每块 `_maybe_evict_cached_block`，该函数（[block_pool.py:679-700](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/block_pool.py#L679-L700)）调 `_remove_cached_block_hashes` 清掉哈希。`_remove_cached_block_hashes`（[block_pool.py:571-590](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/block_pool.py#L571-L590)）既清块自身哈希，也清 `cached_block_hashes_by_block` 里的重复哈希，并 `block.reset_hash()`。

释放逻辑 `free_blocks`：

[vllm/v1/core/block_pool.py:719-742](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/block_pool.py#L719-L742) —— `ref_cnt -= 1`，归零的块按「是否带哈希」分流：**没有哈希的块先 prepend 到队尾**（最先被淘汰），**带哈希的块 append 到队尾**（作为 LRU 候选保留更久）。这保证纯空闲块优先被复用，缓存块尽量存活。

LRU 顺序的维护点在 `FreeKVCacheBlockQueue` 的 docstring：

[vllm/v1/core/kv_cache_utils.py:193-204](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_utils.py#L193-L204) —— 「队首是 LRU；若最近访问时间相同，哈希 token 更多（块链尾部）的排前面先淘汰」，靠释放时**逆序**入队实现。

显式失效 `reset_prefix_cache`：

[vllm/v1/core/block_pool.py:763-797](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/block_pool.py#L763-L797) —— 清空 `cached_block_hash_to_block`、`cached_block_hashes_by_block`，并对每个块 `reset_hash`。仅当除 null_block 外所有块都已空闲时才允许（否则日志告警返回 `False`）。

`cache_salt` 注入点（仅首块）：

[vllm/v1/core/kv_cache_utils.py:559-561](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_utils.py#L559-L561) —— `cache_salt_keys` 仅在 `start_token_idx == 0 and request.cache_salt` 时非空，被塞进首块哈希，使不同 salt 的请求整条前缀哈希都不同，从而互不复用（文档见 [docs/design/prefix_caching.md:86-100](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/prefix_caching.md#L86-L100)）。

> **进阶（混合模型）**：当不同 KV 缓存组的 `block_size` 不一致时，请求的块哈希按更细的 `hash_block_size` 计算，再由 `BlockHashListWithBlockSize` 在访问时**懒放大**到组的实际块大小——因为「细粒度哈希链中位于目标块边界的那个哈希，已经链过了整个前缀」，可直接当目标块哈希用。见 [kv_cache_utils.py:2211-2281](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_utils.py#L2211-L2281)。

#### 4.4.4 代码实践

> **实践目标**：复现设计文档的「Time 3」场景——前缀部分命中、后缀不命中，并说明驱逐时机。

源码阅读 + 推演型实践（块大小 4，共 10 块）：

1. 阅读文档示例 [docs/design/prefix_caching.md:210-234](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/prefix_caching.md#L210-L234)（Time 1–6 的端到端例子）。
2. 聚焦 Time 3：请求 1 有 14 个 prompt token，前 10 个与请求 0 相同。请回答：
   - 为什么只有前 **2 个块**（8 个 token）命中，第 3 个块虽然前 2 个 token 相同却不命中？
   - 这对应源码里的哪条规则？

   预期答案：因为缓存只登记**满块**；第 3 块请求 0 只填了 2 个 token 没写满，从未被 `cache_full_blocks` 登记过哈希，所以请求 1 查不到（规则见 `cache_full_blocks` 只处理 `num_full_blocks` 以内的块）。
3. 聚焦 Time 6：要分配新块时，空闲队列队首若是个**带哈希**的块（如块 3），会发生什么？

   预期答案：`get_new_blocks` → `_maybe_evict_cached_block` 把它的哈希清掉、移出查表，再复用其物理空间——这就是「缓存被驱逐」的时刻。换句话说，**驱逐发生在「需要新块、且 LRU 队首恰好是缓存块」之时**。

> 若想观察真实命中统计：vLLM 通过 Prometheus 暴露 `vllm:prefix_cache_queries`（查询的 token 总数）与 `vllm:prefix_cache_hits`（命中的 token 数），定义见 [vllm/v1/metrics/loggers.py:584-601](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/metrics/loggers.py#L584-L601)；命中率即两者之比。该统计由 `record_prefix_cache_stats`（[kv_cache_manager.py:218-227](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_manager.py#L218-L227)）在每个做过查表的请求上记录。

#### 4.4.5 小练习与答案

**练习 1**：为什么 vLLM「只缓存满块」？

> 参考答案：块是哈希、查找与复用的最小单位；一个没写满的「部分块」其内容还会继续增长，哈希不稳定，且复用它需要复杂的边界处理。等它写满再登记，哈希固定、查找 \(O(1)\)、复用即整块拿走，实现最简洁。

**练习 2**：请求结束时它的块都被立即「驱逐」吗？

> 参考答案：不会。`free_blocks` 只是把 `ref_cnt` 减一；归零的**带哈希**块被 append 到空闲队列尾部继续作为 LRU 缓存候选存活，直到将来某次分配需要新块、且它排到了队首，才被 `_maybe_evict_cached_block` 真正驱逐。所以「释放」≠「驱逐」。

**练习 3**：`cache_salt` 是如何做到「同租户可复用、跨租户隔离」的？

> 参考答案：`cache_salt` 只注入到首块哈希。由于哈希链层层吸收父哈希，首块哈希不同会让**整条前缀**的每一块哈希都不同；于是只有带相同 `cache_salt` 的请求才能查到彼此的缓存块，不同 salt 的请求互不复用，从而实现租户隔离，且不额外占显存。

---

## 5. 综合实践

设计一个贯穿本讲的小任务：**手工模拟两条共享 system prompt 的请求，走完「首请求写缓存 → 次请求命中 → 复用 → 释放 → 驱逐」全流程，并用指标验证。**

设定：块大小 `block_size = 4`，缓存共 10 个块，`temperature = 0`（保证两请求对相同后缀生成相同 token）。

1. **写缓存**：构造请求 R1，prompt = 一段 12 token 的公共 system prompt + 后缀 S1（≥4 token，凑满若干块）。送入引擎，跑完 prefill。请在脑中（或纸上）标出：R1 写满了哪几个块？这些块的 `block_hash` 来自 `R1.block_hashes` 的哪几项？（对应 `cache_full_blocks`）
2. **命中**：构造请求 R2，prompt = 同一段 12 token system prompt + 不同后缀 S2。调度 R2 时：
   - `get_computed_blocks(R2)` 用 `R2.block_hashes` 逐块查表，前 3 块（12 token）应全部命中。
   - 这 3 个命中块随后被 `touch`：`ref_cnt` 各 `+1`，且若原 `ref_cnt==0` 则移出空闲队列。
   - R2 只需对 S2 部分做 prefill。
3. **观察指标**：启动服务或离线 `LLM(enable_prefix_caching=True)`，依次提交 R1、R2。抓取 Prometheus 的 `vllm:prefix_cache_queries` 与 `vllm:prefix_cache_hits`，验证 R2 这次查询里命中 token 数 ≈ 12（system prompt 长度）。
4. **释放与驱逐**：让 R1、R2 都结束。它们的块经 `free_blocks` 回到空闲队列尾部（带哈希，成 LRU 候选）。若此时再来一条**完全不共享前缀**的长请求把缓存挤爆，被 `_maybe_evict_cached_block` 清掉哈希的会是哪些块？（答：LRU 队首、即最近最少访问的缓存块。）

> 若无 GPU：步骤 1–2、4 为纯源码阅读 + 推演（结论由本讲引用的源码语义保证）；步骤 3 的指标观察「待本地验证」。不要假装已运行。

完成本任务后，你应能流畅复述：**哈希链建立指纹 → 满块登记查表 → 新请求查表命中 → touch 复用 → 释放回 LRU → 分配时驱逐** 这条完整闭环。

## 6. 本讲小结

- 前缀缓存让共享前缀的请求**复用已算好的满块 KV**，同时省计算与省显存，且不改变输出——是契合 PagedAttention 的「免费午餐」，V1 中 `enable_prefix_caching` 默认开启。
- vLLM 用**链式块哈希** \(H_i = \mathrm{hash}(H_{i-1}, \mathrm{tokens}_i, \mathrm{extra\_keys}_i)\) 让每个满块哈希唯一指纹「截至该块的整个前缀」，请求在创建/追加 token 时由 `request_block_hasher` 增量维护 `Request.block_hashes`。
- `BlockPool` 维护反向查表 `cached_block_hash_to_block`（哈希+`group_id` → 物理块）；`get_computed_blocks` → `find_longest_cache_hit` 用请求哈希逐块查表得到最长命中前缀，命中块随后被 `touch`（`ref_cnt+1`、移出空闲队列）从而被复用。
- 缓存只登记**满块**（`cache_full_blocks`）；**驱逐**发生在分配新块且 LRU 队首恰为缓存块时（`_maybe_evict_cached_block`）；`free_blocks` 让带哈希的块作为 LRU 候选存活，「释放」≠「驱逐」。
- 可通过 `cache_salt`（注入首块）做多租户隔离，通过 `reset_prefix_cache` 在换权重/压测时显式清空；命中率由 Prometheus 的 `vllm:prefix_cache_{queries,hits}` 暴露。

## 7. 下一步学习建议

- **继续 u4 单元**：本讲是 u4（调度与 KV 缓存管理）的最后一篇。建议回头把 u4-l2～u4-l5 串起来读一遍调度器的 `schedule()`，确认「token 预算 + 前缀缓存命中 + 块分配 + 抢占」如何在一个 step 里协同。
- **进入 u5（模型执行链路）**：接下来可以学习命中后的「未命中后缀」是如何被 `ModelRunner` 真正送进 GPU 做一次前向的（u5-l3），届时你会看到 `num_computed_tokens` 如何决定 attention 的起始位置。
- **可观测性延伸**：若想更系统地看懂 `vllm:prefix_cache_*` 等指标如何被采集与暴露，可预习 u10-l2（指标与可观测性）。
- **进阶阅读**：混合模型（full + sliding window / mamba）下的分组命中与 `BlockHashListWithBlockSize` 的细粒度哈希，可结合 [docs/design/prefix_caching.md](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/prefix_caching.md) 与 `single_type_kv_cache_manager.py` 中各注意力类型的 `find_longest_cache_hit` 实现深入研究。
