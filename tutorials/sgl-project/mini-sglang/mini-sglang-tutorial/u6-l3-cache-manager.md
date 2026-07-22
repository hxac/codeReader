# CacheManager：页分配、回收与淘汰

## 1. 本讲目标

本讲是 KV Cache 管理单元（第 6 单元）的收尾篇。前两讲我们分别讲了两层：

- u6-l1 讲了**池存储**（`MHAKVCache`）和**前缀缓存接口**（`BasePrefixCache`）。
- u6-l2 讲了**基数树索引层**（`RadixPrefixCache`）如何把共享前缀压缩存储。

但池和基数树之间还缺一个「调度员」：谁来决定某个请求的某段 KV 写进池子的哪几个页？算完后哪些页该归还、哪些该插回基数树供别人复用？显存不够时又该淘汰谁？这个调度员就是本讲的主角——`CacheManager`。

学完本讲，你应当能够：

1. 说清 `free_slots` 为什么按**页对齐的 token 下标**而不是页号来管理空闲显存。
2. 复述 `allocate_paged` 如何用 `div_ceil` 算出「还需要新分配哪些页」，并在空闲页不足时触发基数树淘汰。
3. 画出 `cache_req` 中 valid/allocated 四段缓存区域的边界，解释 `finished=True` 与 `finished=False` 的差异。
4. 解释 `lazy_free_region` 为什么把释放动作延迟到一批请求处理完才一次性合并回 `free_slots`。

---

## 2. 前置知识

### 2.1 一页 KV cache 到底是什么

回顾 u6-l1：KV cache 池是一块巨大的 GPU 张量，按**页（page）**切分。一页容纳 `page_size` 个 token 的 K/V。池子可以看作一维铺开的「token 槽位」序列，第 `i` 个 token 槽位存一个 token 的 K/V。于是「一页」就是连续的 `page_size` 个 token 槽位，**页的起始下标**一定是 `page_size` 的整数倍。

本讲里反复出现的 `free_slots`，存的就是这些「页起始下标」。例如 `page_size = 2` 时，`free_slots` 形如 `[0, 2, 4, 6, ...]`，每个数代表一整页（2 个连续 token 槽位）是空闲的。

### 2.2 page_table 是请求视角到池子视角的翻译表

回顾 u4-l4 / u5-l1：`page_table` 是一张形状为 `(max_running_req + 1, aligned_max_seq_len)` 的二维表。`page_table[table_idx, pos]` 的含义是——「第 `table_idx` 个请求、序列第 `pos` 个位置的 token，它的 K/V 存在池子的哪个 token 槽位」。

`CacheManager` 的核心工作之一，就是在分配页之后把对应的池子下标写进这张表，让后续的注意力后端能据此找到 KV。

### 2.3 前缀缓存的两桶计数

回顾 u6-l2：基数树节点靠 `ref_count` 分成两桶——`ref_count > 0` 是 **protected**（受保护，不可淘汰），`ref_count == 0` 是 **evictable**（可淘汰）。`SizeInfo` 把这两桶的 token 数加起来就是 `total_size`。本讲的 `available_size`、淘汰触发都依赖这个计数。

### 2.4 一条贯穿全讲的术语约定

全篇请牢记三个「长度」语义，它们都来自 `core.py` 的 `Req`（见 u2-l1）：

| 字段 | 含义 |
|---|---|
| `cached_len` | KV 已经算好/可复用的 token 数（含命中前缀） |
| `device_len` | 本轮前向要把 KV 算到哪个位置（逻辑游标） |
| `cache_handle.cached_len` | 本次匹配/插入时，前缀缓存认领的长度 |

`CacheManager` 的所有边界计算，都是在这几个长度之间画线。

---

## 3. 本讲源码地图

本讲几乎全部围绕一个文件展开，另有两个文件提供「调用方」与「被调方」的上下文：

| 文件 | 作用 |
|---|---|
| [python/minisgl/scheduler/cache.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py) | **本讲核心**。`CacheManager` 与辅助函数 `_write_page_table`，管理页的分配、回收、淘汰与延迟释放。 |
| [python/minisgl/scheduler/scheduler.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py) | 调用方。`_prepare_batch` 调 `allocate_paged`；`_process_last_data` 在 `lazy_free_region` 内调 `cache_req`；`_free_req_resources` 以 `finished=True` 调 `cache_req`。 |
| [python/minisgl/kvcache/radix_cache.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/radix_cache.py) | 被调方。`insert_prefix` / `evict` / `lock_handle` 的真正实现，`CacheManager` 通过 `self.prefix_cache` 转发。 |
| [tests/core/test_cache_allocate.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/core/test_cache_allocate.py) | 测试。聚焦 `_allocate` 触发淘汰后页对齐与无重叠的不变量。 |

> 提示：本讲只讲**调度器侧的页簿记**。池存储细节看 u6-l1，基数树节点结构看 u6-l2，不要在本讲陷入这两层。

---

## 4. 核心概念与源码讲解

### 4.1 CacheManager：池与索引层之间的页簿记员

#### 4.1.1 概念说明

`CacheManager` 是一个**纯 CPU 的簿记对象**：它自己不持有任何 KV 张量（真正的 K/V 在池 `MHAKVCache` 里，挂在全局 `Context` 上），只持有两样东西——

1. `free_slots`：一维张量，记录「池子里当前空闲的页起始下标」。
2. `prefix_cache`：一个 `BasePrefixCache`（默认 radix，可换 naive），即 u6-l2 的索引层。

它对外暴露的职责可以归纳成一句话：**把「请求的逻辑 token 位置」翻译成「池子里的物理 token 下标」，并维护这些下标在 `free_slots` 与基数树之间的流转**。分配是把下标从 `free_slots` 搬给请求；回收是把下标还回 `free_slots`；淘汰是在 `free_slots` 不够时从基数树里抢；插回是把算好的下标登记进基数树供复用。

#### 4.1.2 核心流程

```text
                    ┌──────────────────────────────────────┐
                    │           KV Cache 池 (GPU)           │
                    │  按 page_size 切成一页页的 token 槽位  │
                    └───────────────┬──────────────────────┘
                                    │ 物理下标
              ┌─────────────────────┴──────────────────────┐
              │            CacheManager (CPU)              │
              │                                            │
              │   free_slots  ←──────分配/回收/淘汰─────►  │
              │   (空闲页起始下标)                          │
              │                                            │
              │   prefix_cache  ◄── insert/evict/lock ──►  │
              │   (基数树索引层, 复用前缀)                  │
              └─────────────────────┬──────────────────────┘
                                    │ 写 page_table
                                    ▼
                          page_table[table_idx, pos]
                          = 该 token 的池子下标
```

四个最小模块各管一个动作：

- **4.1（本节）**：构造 `free_slots`、提供 `available_size` 与 `match_req`/`lock`/`unlock`。
- **4.2**：`allocate_paged` + `_allocate`——按需分页，不够就淘汰。
- **4.3**：`cache_req`——前向算完后把结果插回基数树并释放多余页。
- **4.4**：`lazy_free_region`——把一整批释放延迟合并。

#### 4.1.3 源码精读

先看构造与几个只读/转发方法：

[python/minisgl/scheduler/cache.py:16-25](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py#L16-L25) 构造 `CacheManager`。关键一行是 `torch.arange(num_pages) * page_size`——它生成 `[0, page_size, 2*page_size, ...]`，即所有页的起始下标，初始全部空闲。注意它存的是**页起始的 token 下标**而非页号，这是为了让后续写 `page_table` 时能直接当作 token 下标用，省去一次乘法。

`available_size` 是准入判断的命脉：

[python/minisgl/scheduler/cache.py:32-34](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py#L32-L34) 可用 token 数 = 基数树里可淘汰的 token 数 + 空闲页折算的 token 数。`PrefillAdder` 正是用它来判定「这个请求加进来会不会 OOM」（见 u4-l3）。

\[ \text{available\_size} = \text{evictable\_size} + |\text{free\_slots}| \times \text{page\_size} \]

注意它**只算 evictable 那一桶**：被锁住（protected）的前缀虽然物理上还在池里，但不能算作可用，因为别人正在用。

`match_req` 把请求交给基数树做前缀匹配：

[python/minisgl/scheduler/cache.py:27-30](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py#L27-L30) 注意它匹配的是 `input_ids[: input_len - 1]`，**故意排除最后一个 token**。原因是：最后一个 token 是本轮要生成下一个 token 的「查询位」，它的 KV/logits 必须在本轮 prefill 里现算，只有它**之前**的 token 才构成可复用前缀。`lock` / `unlock` 只是转发给基数树的 `lock_handle`（见 u6-l2，只改尺寸计数、不动树结构）：

[python/minisgl/scheduler/cache.py:36-40](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py#L36-L40)。

#### 4.1.4 代码实践

**实践目标**：确认 `free_slots` 的初始形态与 `available_size` 的构成。

**操作步骤**：阅读上面引用的 `__init__` 与 `available_size`，然后打开测试文件 [tests/core/test_cache_allocate.py:23-28](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/core/test_cache_allocate.py#L23-L28) 看如何用一个 `torch.empty((1,))` 的假 `page_table` + CPU radix cache 构造一个可独立测试的 `CacheManager`。

**需要观察的现象**：构造后 `len(cm.free_slots)` 应等于 `num_pages`，且（当 `page_size > 1` 时）每个元素都是 `page_size` 的倍数。

**预期结果**：`available_size` 初始等于 `num_pages * page_size`（此时 `evictable_size = 0`）。待本地验证（无 GPU 也能跑该测试，因为它用的是 CPU radix cache）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `free_slots` 存 token 下标（如 `0, 2, 4`）而不是页号（`0, 1, 2`）？

> **答案**：因为 `page_table` 的每个格子需要的就是「池子里的 token 下标」。存 token 下标后，分配结果可以直接写进 `page_table`，不必每次再做一次 `page_idx * page_size` 换算，也方便 `_page_to_token` 把一页展开成一串连续 token 下标。

**练习 2**：`available_size` 为什么只加 `evictable_size` 而不加 `protected_size`？

> **答案**：protected 的前缀正被某个在途请求锁着使用，若算作可用就可能被新请求挤占，导致在途请求读到被覆盖的 KV。只有 evictable（无人引用）那桶才能安全地被淘汰回收。

---

### 4.2 allocate_paged：按需分页与触发淘汰

#### 4.2.1 概念说明

当一个 batch 即将前向时，调度器先调 `_prepare_batch`，其中一步是 `allocate_paged`：给 batch 里每个请求**补齐它本轮要新算的那段 token 所需的页**。注意「补齐」——命中前缀的那段已经有页了（在 u4-l3 的 `_try_allocate_one` 里通过 `handle.get_matched_indices()` 写进了 `page_table`），这里只分配「从 `cached_len` 到 `device_len`」这段**新增**部分所需的页。

分配若发现 `free_slots` 不够，就触发基数树淘汰：从可淘汰的前缀里抢回足够的页，再继续分配。

#### 4.2.2 核心流程

对每个请求，先把长度换算成「页号区间」：

\[ \text{first\_page} = \lceil \text{cached\_len} / \text{page\_size} \rceil, \quad \text{last\_page} = \lceil \text{device\_len} / \text{page\_size} \rceil \]

需要的新页数 \( = \text{last\_page} - \text{first\_page} \)（若为正）。然后：

```text
allocate_paged(reqs):
  对每个 req 累计 needed_pages, 记录 (table_idx, first_page, last_page)
  若 needed_pages > 0:
      pages = _allocate(needed_pages)        # 不够则淘汰
      tokens = _page_to_token(pages)          # 页起始 → 连续 token 下标
      _write_page_table(page_table, tokens, allocation_info, page_size)
```

`_allocate` 的淘汰分支：

```text
_allocate(needed_pages):
  if needed_pages > len(free_slots):
      缺口 = (needed_pages - free_pages) * page_size   # 按 token 计
      evicted = prefix_cache.evict(缺口)               # 返回被回收的 token 下标
      free_slots += evicted[::page_size]               # 取每页起始，加回 free_slots
  从 free_slots 头部切下 needed_pages 个返回
```

#### 4.2.3 源码精读

[python/minisgl/scheduler/cache.py:42-53](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py#L42-L53) `allocate_paged` 主体。`div_ceil(req.cached_len, self.page_size)` 是 `first_page`，`div_ceil(req.device_len, ...)` 是 `last_page`。

这里有一个**容易看走眼的关键点**：为什么用 `div_ceil`（向上取整）而不是 `//`？想象 `cached_len` 不是页对齐的情形（分块续算时会出现，见 u4-l3 的 `ChunkedReq`）。假设 `page_size = 2`、`cached_len = 5`：

- 已经缓存的是位置 `[0, 5)`，共 5 个 token。
- `div_ceil(5, 2) = 3`，即 `first_page = 3`，对应池子下标 `[6, 7]`。

这意味着位置 `5` 所在的那一页（页 2，下标 `[4, 5]`）**被视为已经拥有**——因为它在**上一块**续算时就已经被分配过（上一块的 `last_page = div_ceil(5, 2) = 3`，分配了页 0、1、2）。所以 `div_ceil` 的真正作用是：**跨越 `cached_len` 边界的那一页，已经在上一轮被本请求拥有，本轮从它的下一页开始分配**。不变量是「一页一旦分给某请求，就随 `cached_len` 增长一直归它所有」。

[python/minisgl/scheduler/cache.py:106-113](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py#L106-L113) `_allocate`。空闲不足时按 `page_size` 把「缺的 token 数」换算成淘汰请求量，调 `prefix_cache.evict`。`evict` 返回的是 token 下标数组（来自被摘除的基数树叶子的 `_value`），再用 `[::page_size]` 取每页起始加回 `free_slots`——因为基数树节点天然页对齐（u6-l2 的 `align_down` 保证），所以这里取页起始是安全的。

[python/minisgl/scheduler/cache.py:119-124](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py#L119-L124) `_page_to_token` 把页起始展开成连续 token 下标：`page_size == 1` 时直接返回；否则用广播 `[p, p+1, ..., p+page_size-1]`。

[python/minisgl/scheduler/cache.py:127-146](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py#L127-L146) `_write_page_table` 把分配到的 token 下标写进 `page_table[table_idx, pos]`。它用 `pin_memory` 的 CPU 缓冲先填好 `table_idx` 与 `position` 两列，再 `non_blocking=True` 拷到 GPU 做一次花式赋值 `page_table[table_idxs, offsets] = allocated`。这是 CPU 端准备元数据、再批量上传 GPU 的典型手法（呼应 u4-l1 的 overlap scheduling）。

调用点在调度器：

[python/minisgl/scheduler/scheduler.py:204-206](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L204-L206) `_prepare_batch` 在 pad_batch 之后、构造注意力元数据之前调 `allocate_paged`——顺序很重要：注意力后端 `prepare_metadata` 依赖已经写好的 `page_table`。

#### 4.2.4 代码实践

**实践目标**：理解淘汰后页对齐不被破坏。

**操作步骤**：阅读 [tests/core/test_cache_allocate.py:61-80](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/core/test_cache_allocate.py#L61-L80)。该测试先把所有空闲页 `_allocate(num_pages)` 耗尽，再 `insert_prefix` 放 2 页可淘汰数据进基数树，然后 `_allocate(1)` 触发淘汰。

**需要观察的现象**：淘汰后 `allocated` 与 `free_slots` 的每个元素仍是 `page_size` 的倍数（`_assert_all_page_aligned`）。

**预期结果**：即使淘汰路径返回的是「token 下标」，经 `[::page_size]` 处理后 `free_slots` 依然全页对齐。可在本地 `pytest tests/core/test_cache_allocate.py -q` 运行（CPU 即可）。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`allocate_paged` 里 `last_page > first_page` 的判断有什么用？

> **答案**：若 `cached_len` 与 `device_len` 落在同一页（即本请求本轮没有跨入新页），则 `last_page == first_page`，`needed_pages` 不增加、也不记入 `allocation_info`——这个请求本轮不需要新页，直接跳过。

**练习 2**：为什么 `_allocate` 淘汰时按 `(needed_pages - free_pages) * page_size` 而不是直接 `needed_pages - free_pages` 向基数树要空间？

> **答案**：基数树的 `evict(size)` 接口以 **token 数**为单位（见 u6-l1 的 `BasePrefixCache.evict` 契约），而 `needed_pages - free_pages` 是页数，必须乘 `page_size` 换算成 token 数。返回值再除回页（`[::page_size]`）才能并回以页为粒度的 `free_slots`。

---

### 4.3 cache_req：把结果插回前缀缓存并释放尾部

#### 4.3.1 概念说明

一轮前向算完、采样出 token 后，`CacheManager` 要「结账」：这条请求本轮新算的 KV 已经写进池子了（由 `store_kv` 完成），现在要决定——

- 这些 KV 的**池子下标**要不要登记进基数树，好让后续相同前缀的请求复用？
- 哪些下标是**多余的**（重复或无法凑成整页），应当释放回 `free_slots`？

这就是 `cache_req(req, finished=...)` 的职责。它是连接「计算」与「缓存复用」的关口。它有两个调用场景，由 `finished` 区分：

- `finished=False`：prefill 刚算完首 token、请求还要继续 decode（来自 `_process_last_data`）。
- `finished=True`：请求彻底结束或被 abort，资源全释放（来自 `_free_req_resources`）。

#### 4.3.2 核心流程

源码顶部那段注释是本节的「地图」，把 `[0, req.cached_len)` 切成四段看待。先看 valid（有效计算）视角，再看 allocated（如何处置）视角：

```text
位置轴:  0 ───────────── req.cached_len
         │   old_handle │  本轮新算  │
         │   .cached_len│            │
         ├──────────────┼────────────┤
valid:   │←── 可复用 ──→│← 新算有效 →│   注意力可读/写
         ├──────────────┼────────────┤
处置:    │  保留(命中)  │ 已被别人缓存 │← _free
         │              ├────────────┤
         │              │ 新插入基数树 │← 归 new_handle
         │              ├────────────┤
         │              │ 尾部<1页    │← finished=True: _free
         │              │             │   finished=False: 保留并锁
```

四个边界点：`old_handle.cached_len`、`cached_len`（insert 返回的「插入前已在缓存里的长度」）、`new_handle.cached_len`（= `align_down(req.cached_len, page_size)`，页对齐）、`req.cached_len`（可能非页对齐）。

步骤：

```text
cache_req(req, finished):
  insert_ids   = input_ids[: req.cached_len]          # 本轮算出的完整前缀
  page_indices = page_table[table_idx, : req.cached_len]  # 对应的池子下标
  old_handle   = req.cache_handle                     # 匹配时拿到的、被锁住的 handle
  cached_len, new_handle = prefix_cache.insert_prefix(insert_ids, page_indices)
  unlock(old_handle)                                  # 用完, 解锁
  _free(page_indices[old_handle.cached_len : cached_len])   # 重复部分, 释放防泄漏
  if finished:
      _free(page_indices[new_handle.cached_len :])    # 尾部释放
  else:
      req.cache_handle = new_handle                   # 更新 handle 供 decode
      lock(new_handle)                                # 继续锁住
```

#### 4.3.3 源码精读

[python/minisgl/scheduler/cache.py:55-79](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py#L55-L79) `cache_req` 全文，注释就是上面那张图的文字版。

第 70 行 `insert_prefix` 的返回值需要对照基数树实现细看：

[python/minisgl/kvcache/radix_cache.py:136-146](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/radix_cache.py#L136-L146) `insert_prefix` 先 `align_down(len(input_ids), page_size)` 得到 `insert_len`（**只缓存页对齐部分**），再走树。返回 `InsertResult(prefix_len, handle)`，其中 `prefix_len` 是「插入前树里已经命中的长度」，`handle.cached_len = insert_len`（页对齐）。

> 「插入前已命中」是什么意思？想象两条请求 A、B 几乎同时 prefill 同一段长前缀（overlap scheduling 下完全可能）。A 先 `cache_req` 把 `[0, 100)` 插进树；B 算完后也来插 `[0, 100)`，此时树里已经有了，`prefix_len = 100`。对 B 而言，它**本轮新算**了 `[old_handle.cached_len, 100)` 这段、下标也写进了池子，但这段其实已经被 A 登记过了——于是第 74 行 `_free(page_indices[old_handle.cached_len:cached_len])` 把 B 自己写的这份**重复下标**释放掉，避免同一段 KV 占两份池子（泄漏）。这正是注释里 `[old_handle.cached_len, cached_len)` 那段「We must free them to avoid memory leak」的含义。

第 75-79 行是 `finished` 分支：

- `finished=True`：尾部 `[new_handle.cached_len, req.cached_len)` 不足一页、无法插入基数树（基数树只收页对齐），请求又结束了，直接 `_free` 释放。
- `finished=False`：请求还要 decode，尾部这页得**保留**给后续 token 继续写入，于是把 `req.cache_handle` 更新为 `new_handle` 并 `lock` 住（变 protected，防被淘汰）。

两个调用点印证了 `finished` 的语义：

[python/minisgl/scheduler/scheduler.py:163-164](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L163-L164) prefill 非分块请求算完首 token 后，以 `finished=False` 调 `cache_req`——把前缀插回树、锁住新 handle，准备进入 decode。

[python/minisgl/scheduler/scheduler.py:200-202](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L200-L202) `_free_req_resources` 以 `finished=True` 调 `cache_req`——先归还 `table_idx` 槽位，再把整条前缀插回树供复用、释放尾部。abort 路径也走这里。

#### 4.3.4 代码实践

**实践目标**：亲手在「位置轴」上标出四段边界，验证释放的页确实页对齐。

**操作步骤**：假设 `page_size = 4`，某 prefill 请求命中前缀 8 个 token（`old_handle.cached_len = 8`），本轮把 `device_len` 推到 14（`req.cached_len = 14`）。按 `cache_req` 逻辑计算：
1. `insert_prefix` 后 `new_handle.cached_len = align_down(14, 4) = 12`。
2. 假设没有并发请求抢先插入，则 `cached_len`（insert 返回的「已在缓存」长度）= `old_handle.cached_len = 8`。
3. `_free(page_indices[8:8])` = 释放空集（无重复）。
4. `finished=False`：保留 `[12, 14)` 这 2 个尾部 token 所在的页，锁住 `new_handle`。

**需要观察的现象**：若把场景改成 `finished=True`，则 `_free(page_indices[12:])` 会释放尾部那不足一页的段。

**预期结果**：无论哪种分支，被 `_free` 的区间起点（`old_handle.cached_len`、`new_handle.cached_len`）都是页对齐的，所以 `[::page_size]` 取出的都是合法页起始。待本地验证（可仿照 `test_cache_allocate.py` 写一个最小脚本）。

#### 4.3.5 小练习与答案

**练习 1**：`finished=False` 时为什么要 `lock(new_handle)`？

> **答案**：请求马上要进入 decode，会继续读写 `new_handle` 覆盖的那些页。若不锁，这些页属于 evictable，可能被后续请求的 `_allocate` 淘汰掉，导致 decode 读到被覆盖的 KV。锁住后它们进入 protected 桶，`available_size` 也不会再把它算作可用。

**练习 2**：`insert_prefix` 为什么只插入 `align_down(len, page_size)` 而不插全部？

> **答案**：基数树以页为最小单位（节点 `_value` 是页对齐的下标块）。不足一页的尾部无法对齐，既不能复用也不能被 `evict` 按页回收，所以干脆不插入；它的去留在 `cache_req` 里由 `finished` 决定。

---

### 4.4 lazy_free_region：延迟到批末才合并回收

#### 4.4.1 概念说明

`_process_last_data` 会**在一轮里循环处理多条请求**，每条都可能调 `cache_req`，进而触发若干次 `_free`。如果每次 `_free` 都立刻 `torch.cat` 进 `free_slots`，会发生两件事：

1. **性能抖动**：`torch.cat` 每次都新建一个张量并拷贝全部元素，循环里反复 cat 是 \(O(n^2)\) 的累赘。
2. **碎片化**：频繁增删让 `free_slots` 反复伸缩。

`lazy_free_region` 用一个上下文管理器把 `_free` **临时换成「只收集不合并」**的版本，等整批处理完，在 `finally` 里做**一次** `torch.cat` 把所有回收页合并回 `free_slots`。

#### 4.4.2 核心流程

```text
进入 lazy_free_region:
  self._free = lazy_free          # 实例属性遮蔽类方法
  lazy_free_list = []
  ── yield (执行 _process_last_data 的循环) ──
    每次 cache_req 调 _free(indices):
        lazy_free_list.append(indices[::page_size])   # 只攒页起始
退出 (finally):
  del self._free                  # 删除实例属性 → 回落到类方法 _free
  self.free_slots = torch.cat([free_slots] + lazy_free_list)   # 一次性合并
```

#### 4.4.3 源码精读

[python/minisgl/scheduler/cache.py:93-104](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py#L93-L104) `lazy_free_region`。这里有一个**很 Pythonic 的小技巧**值得细看：

- `self._free = lazy_free`：给**实例**打补丁，新增一个同名实例属性，它会**遮蔽**类上定义的方法（[python/minisgl/scheduler/cache.py:115-118](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py#L115-L118) 的 `_free`）。
- `del self._free`：删除实例属性后，`self._free` 的查找会沿 MRO **回落到类方法**，于是退出上下文后 `_free` 自动「恢复原样」，无需显式备份。

`lazy_free` 内部 `indices[::self.page_size]` 与真实 `_free` 完全一致——都是取页起始。差别只在：真实版立刻 cat 进 `free_slots`，延迟版只 append 到列表。

为什么这样是**安全**的？延迟期间不会有 `_allocate` 发生：`_process_last_data` 是「处理上一批结果」阶段，不调度新批；下一次 `_allocate` 发生在下一轮循环的 `_schedule_next_batch → _prepare_batch → allocate_paged`，而那之前 `lazy_free_region` 的 `finally` 早已把 `free_slots` 合并完毕。所以回收在下一次分配前一定可见。

调用点印证了这点：

[python/minisgl/scheduler/scheduler.py:146-164](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L146-L164) `with self.cache_manager.lazy_free_region():` 包住整个结果处理循环，循环内的 `cache_req`（包括对 finished 请求的 `_free_req_resources → cache_req`）都走延迟路径。

> 注意一个例外：[python/minisgl/scheduler/scheduler.py:190-195](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L190-L195) 的 `AbortBackendMsg` 走 `_process_one_msg`，它在 `lazy_free_region` **之外**调用 `_free_req_resources`，此时 `cache_req` 用的是**真实** `_free`，立即释放。这没问题——abort 是单条、即时的资源回收，不需要批合并。

#### 4.4.4 代码实践

**实践目标**：验证「实例属性遮蔽 + del 回落」的恢复机制。

**操作步骤**：写一段最小示例代码（**示例代码**，非项目代码）：

```python
class C:
    def f(self):
        return "class method"

c = C()
print(c.f())            # class method
c.f = lambda: "patched" # 实例属性遮蔽
print(c.f())            # patched
del c.f                 # 删除实例属性
print(c.f())            # class method  ← 回落到类
```

**需要观察的现象**：`del` 之后 `c.f()` 恢复为类方法，无需提前保存旧引用。

**预期结果**：这正是 `lazy_free_region` 退出时 `del self._free` 能自动恢复真实 `_free` 的原理。可在本地任意 Python 环境运行验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么延迟回收不会让下一次 `_allocate` 看到「过时」的 `free_slots`？

> **答案**：`lazy_free_region` 的 `finally` 在 `_process_last_data` 返回前执行，而 `_allocate` 只在之后的 `_schedule_next_batch` 里才发生。所以合并一定先于下一次分配完成，`free_slots` 对 `_allocate` 是最新的。

**练习 2**：如果某次 `cache_req` 在 `lazy_free_region` 内抛了异常，会发生什么？

> **答案**：`finally` 仍会执行——`del self._free` 恢复方法，`torch.cat` 把已收集的页合并回 `free_slots`。这正是用 `try/finally` 而非裸 `yield` 的意义：保证簿记状态不因异常而卡在「补丁未撤」的中间态。

---

## 5. 综合实践

**任务**：追踪一条 prefill 请求从「算完首 token」到「插入缓存」的全过程，画出哪些 page 被插入基数树、哪些被释放，并对比 `finished=True/False`。

**背景设定**：`page_size = 4`。请求 R 的 prompt 长度 10，命中前缀 4 个 token，故 `old_handle.cached_len = 4`。本轮 prefill 把 `device_len` 推到 10，`complete_one` 后 `req.cached_len = 10`。

**操作步骤**：

1. 打开 [python/minisgl/scheduler/scheduler.py:138-167](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L138-L167)，确认 R 不是 `ChunkedReq`，故进入第 163 行 `batch.is_prefill` 分支，以 `finished=False` 调 `cache_req`。
2. 在 [cache.py:55-79](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py#L55-L79) 逐步推演：
   - `insert_ids = input_ids[:10]`，`page_indices = page_table[R.table_idx, :10]`。
   - 假设无并发请求抢先插入，`insert_prefix` 返回 `cached_len = old_handle.cached_len = 4`（「插入前已命中 4」），`new_handle.cached_len = align_down(10, 4) = 8`。
   - 第 74 行 `_free(page_indices[4:4])`：释放空集（无重复）。
   - `finished=False`：保留尾部 `[8, 10)`（2 个 token，不足一页）所在的页，`req.cache_handle = new_handle` 并 `lock`。
3. **被插入基数树的 page**：`[old_handle.cached_len, new_handle.cached_len) = [4, 8)` 这一整页（池子下标为 `page_indices[4:8]`）由 `insert_prefix` 登记进树，归 `new_handle`。
4. **被释放的 page**：本次为空。
5. **对比 `finished=True`**：若 R 此刻结束（走 `_free_req_resources`），第 76 行 `_free(page_indices[new_handle.cached_len:]) = _free(page_indices[8:])` 会释放尾部那不足一页的段（`[8, 10)` 所在页的起始下标）。

**需要观察的现象**：`finished=False` 时尾部页被「锁住保留」给 decode；`finished=True` 时尾部页被「释放」回 `free_slots`（经延迟合并）。

**预期结果**：你能用一张位置轴图，标出 `[0,4)` 命中保留、`[4,8)` 新插入树、`[8,10)` 尾部（保留或释放取决于 finished）三段，并解释 `div_ceil` / `align_down` 保证了所有边界页对齐。待本地验证（可仿照 `tests/core/test_cache_allocate.py` 构造 CPU `CacheManager`，手动调 `insert_prefix` + `cache_req` 打印 `free_slots` 变化）。

**延伸思考**：把场景改成「两条相同 prompt 的请求在 overlap 下先后 `cache_req`」，第二条的 `cached_len`（insert 返回值）会等于 `new_handle.cached_len`，于是第 74 行 `_free(page_indices[old:cached_len])` 会释放掉它重复写入的那段——验证你理解了「防泄漏」分支。

---

## 6. 本讲小结

- `CacheManager` 是池存储（GPU）与基数树索引层（CPU）之间的**页簿记员**：用 `free_slots`（页对齐的 token 下标数组）管空闲页，用 `prefix_cache` 管可复用前缀，把请求逻辑位置翻译成池子下标写进 `page_table`。
- `available_size = evictable_size + |free_slots| × page_size`，只算可淘汰那桶，是 prefill 准入判断的命脉。
- `allocate_paged` 用 `div_ceil` 把 `cached_len→device_len` 换算成「需新分配的页区间」，正确跳过跨边界的已拥有页；`_allocate` 在空闲不足时按 token 数向基数树 `evict`，返回值再 `[::page_size]` 取页起始并回 `free_slots`。
- `cache_req` 把算完的 KV 插回基数树，按 `old_handle.cached_len / cached_len / new_handle.cached_len / req.cached_len` 四个边界决定：重复段释放防泄漏、新段入树、尾部按 `finished` 决定保留（锁住）或释放。
- `lazy_free_region` 用「实例属性遮蔽类方法 + `del` 回落」把一整批 `_free` 延迟到批末一次性 `torch.cat`，兼顾性能与簿记安全，且 `try/finally` 保证异常下也能恢复。
- 全程不变量：`free_slots` 永远页对齐，`free_pages + cache_pages == num_pages`，由 `check_integrity` 在调度器空闲时校验。

---

## 7. 下一步学习建议

- **横向打通调度全链路**：回到 [scheduler.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py)，把 u4（Prefill/Decode 调度）与本讲串成「调度 → 分配 `allocate_paged` → 前向 → `cache_req` 结账 → 延迟回收」一条完整回路。
- **纵向深入注意力后端**：`page_table` 被 `allocate_paged` 写好后，是被 u7 的注意力后端（`prepare_metadata`）读取的。建议接着读 u7-l1（注意力后端抽象）与 u7-l2（FlashInfer 后端），看 `page_table` 如何变成 paged attention 的寻址索引。
- **补充阅读**：重读 [kvcache/base.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/base.py) 的 `BasePrefixCache` 契约（`insert_prefix` / `evict` / `lock_handle` 的返回值语义）与 [radix_cache.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/radix_cache.py) 的树操作，巩固本讲对边界对齐与淘汰的理解。
- **动手验证**：仿照 [tests/core/test_cache_allocate.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/core/test_cache_allocate.py) 写一个针对 `cache_req` 的最小测试，断言一次 prefill 后 `free_slots + cache_pages` 恒等于 `num_pages`（即 `check_integrity` 的不变量）。
