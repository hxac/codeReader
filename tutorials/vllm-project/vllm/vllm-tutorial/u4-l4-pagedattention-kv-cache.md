# PagedAttention 与 KV 缓存管理

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 PagedAttention 为什么把 KV 缓存切成固定大小的 **block**，以及它解决了哪两类显存碎片问题。
- 读懂 `block_size` 这个参数的含义、来源，以及它如何同时决定分配粒度与前缀缓存粒度。
- 解释 `KVCacheBlock` 的引用计数（`ref_cnt`）如何支撑「多个请求共享同一物理块」。
- 说清 `BlockPool`（块池）、`KVCacheManager`（缓存管理器）、`KVCacheBlocks`（分配结果）三者各自的职责与协作方式。
- 跟踪一个请求从 `allocate_slots` 到拿到新 block 的完整流程。

本讲只讲「块是如何被分配、释放、共享的」，**不讲**前缀缓存的 hash 命中机制（那是 u4-l5 的主题），也不讲调度器本身的选请求逻辑（已在 u4-l2 讲过）。

## 2. 前置知识

- **KV 缓存**：Transformer 在自回归生成时，每生成一个 token，都要拿当前 token（query）去和历史所有 token 的 Key/Value 做注意力。为了避免每步重算历史 K/V，vLLM 把它们缓存下来，这就是 KV 缓存。序列越长，KV 缓存越大，它是显存的主要消费者之一。
- **block（块）**：vLLM 把整个 KV 缓存显存区域切成等大的小块，每块存放固定数量（`block_size`，默认 16）个 token 的 K/V。本讲的 block 指「vLLM 分页 KV 块」，不是 GPU 线程块。
- **块表（block table）**：类比操作系统页表。一个请求的逻辑第 `i` 个块，通过块表映射到一个物理 block_id。GPU 上的注意力内核靠块表找到 K/V 的真实物理位置。
- **prefill / decode**：prefill 是处理 prompt 的首计算（算力密集），decode 是逐个生成 token（带宽密集）。两者都会不断往 KV 缓存里追加。
- 本讲承接 u4-l2：调度器 `Scheduler.schedule()` 每步决定「算哪些请求、各算几个 token」后，会把具体「显存够不够、要不要分配新块」的问题委托给 KV 缓存管理器；当管理器返回「块不够」时，调度器就会触发抢占（preemption）。本讲就是把这段委托的**被委托方**拆开讲透。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [vllm/v1/core/kv_cache_manager.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_manager.py) | 定义 `KVCacheManager`（调度器直接对话的高层管理器）和 `KVCacheBlocks`（分配结果对象）。 |
| [vllm/v1/core/block_pool.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/block_pool.py) | 定义 `BlockPool`（真正持有所有 `KVCacheBlock` 的块池）与 `BlockHashToBlockMap`（前缀缓存哈希表）。 |
| [vllm/v1/core/kv_cache_utils.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_utils.py) | 定义 `KVCacheBlock`（单块元数据）、`FreeKVCacheBlockQueue`（空闲块双向链表），以及 `block_size` 的解析逻辑。 |
| [vllm/v1/core/kv_cache_coordinator.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_coordinator.py) | `KVCacheCoordinator`：夹在 `KVCacheManager` 和具体单类型管理器之间，按 KV cache group 分派分配请求。 |
| [vllm/v1/core/single_type_kv_cache_manager.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/single_type_kv_cache_manager.py) | `SingleTypeKVCacheManager`：某一种注意力类型（如全注意力）的最底层块管理，真正调用 `BlockPool.get_new_blocks`。 |
| [docs/design/paged_attention.md](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/paged_attention.md) | PagedAttention 的历史设计文档，用来理解「block」这个概念的动机与 GPU 内核视角。 |

> 分层一句话：调度器只认识 `KVCacheManager`；`KVCacheManager` 把活儿派给 `coordinator`；`coordinator` 再分派给若干 `SingleTypeKVCacheManager`；后者才真正向 `BlockPool` 取块/还块。

## 4. 核心概念与源码讲解

### 4.1 block size 与 PagedAttention 的分块思想

#### 4.1.1 概念说明

朴素做法是「给每个请求预先分配一段**连续**的、长度等于 `max_model_len` 的显存」。这有两个致命问题：

1. **内部碎片（internal fragmentation）**：你为最坏情况（最长序列）预留空间，但绝大多数请求远达不到 `max_model_len`，预留下来的大片显存白白浪费。
2. **外部碎片（external fragmentation）**：请求长短不一，各自占用一段连续显存后，它们之间会留下无法被复用的「缝隙」。请求来来去去，显存被切得支离破碎，最终即使总量够也分配不出来。

PagedAttention 借鉴操作系统的**虚拟内存分页**：把 KV 缓存显存切成大量等大的 **block**，每个 block 存放固定数量（`block_size`）个 token 的 K/V；一个请求用多少 token 就按需申请多少个 block，用一个**块表**把「逻辑第 i 块」映射到「物理 block_id」。

这样：
- 块是等大的，所有空闲块都在同一个池子里，**任何空闲块都能服务任何请求**——外部碎片被彻底消除。
- 每个请求最多只浪费「最后一个没装满的块」，内部碎片被压到极小。

#### 4.1.2 核心流程

设一个请求实际使用了 \(L\) 个 token，块大小为 \(B\)。

- **朴素方案**：每请求预留 `max_model_len` 槽位，浪费约

\[
\text{waste}_{\text{naive}} \approx (\text{max\_model\_len} - L)
\]

且不同请求间会产生不可复用的外部碎片。

- **PagedAttention**：只需 \(\lceil L / B \rceil\) 个块，每请求内部碎片最多

\[
\text{waste}_{\text{paged}} \le B - 1 \quad\text{（一个没装满的块）}
\]

外部碎片为 0。整批请求的总浪费从「与 `max_model_len` 同量级」降到「与 `block_size` 同量级」。

块表映射关系为：

\[
\text{逻辑块 } i \;\xrightarrow{\text{block\_table}}\; \text{物理 block\_id}
\]

GPU 注意力内核据此找到 K/V 的真实物理地址（见 [paged_attention.md](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/paged_attention.md) 中对 block 的定义）。

#### 4.1.3 源码精读

`block_size` 默认来自 `CacheConfig`（默认 16，见 u3-l3）。在 V1 中它经过一层解析：

[kv_cache_utils.py:606-668](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_utils.py#L606-L668) 中的 `resolve_kv_cache_block_sizes` 负责把 `cache_config.block_size` 解析为两个量：`scheduler_block_size`（调度对齐粒度，单组时即 `block_size * dcp`）和 `hash_block_size`（前缀缓存哈希粒度）。

```python
bs = cache_config.block_size * dcp          # 单组情况
...
scheduler_block_size = math.lcm(*group_block_sizes)
```

真正持有该值的，是每个单类型管理器（[single_type_kv_cache_manager.py:73-79](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/single_type_kv_cache_manager.py#L73-L79)）：

```python
self.scheduler_block_size = scheduler_block_size
self.block_size = kv_cache_spec.block_size
...
if dcp_world_size > 1:
    self.block_size *= dcp_world_size
```

`block_size` 同时是「分配的粒度」和「前缀缓存命中的粒度」——这正是 PagedAttention 一箭双雕的地方。

设计文档 [paged_attention.md:104-109](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/paged_attention.md#L104-L109) 对 block 的描述（注意：该文档描述的是历史 GPU 内核，但其 block 概念仍是当前 V1 Python 层管理的基础）：

> 每个 block 存放固定数量（`BLOCK_SIZE`）个 token 的 K/V；若 block_size=16、head_size=128，则一个 head 的一块可存 `16 * 128 = 2048` 个元素。

#### 4.1.4 代码实践

**实践目标**：用具体数字体会 PagedAttention 对碎片的改善。

**操作步骤**（源码阅读 + 手算，无需 GPU）：

1. 打开 [kv_cache_utils.py:606-668](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_utils.py#L606-L668)，确认单组、`dcp=1` 时 `scheduler_block_size` 就等于 `cache_config.block_size`。
2. 假设 `max_model_len=2048`，`block_size=16`，有 4 个请求分别生成 8、200、1000、2048 个 token。
3. 分别用朴素方案（每请求预留 2048 槽）和 PagedAttention 方案（按 `⌈L/16⌉` 取块）手算总浪费槽位数。

**需要观察的现象 / 预期结果**：

- 朴素方案浪费约 \(3\times2048 + (2048-2048)\) 量级的内部碎片，且伴随外部碎片。
- PagedAttention 方案每个请求浪费 ≤ 15 个槽（一个不满的块），总浪费仅 4 个不完整的块，且无外部碎片。

**待本地验证**：可在本地构造一个最小 `CacheConfig` 并打印 `resolve_kv_cache_block_sizes` 的返回值，确认 `scheduler_block_size`。

#### 4.1.5 小练习与答案

**练习 1**：为什么把 `block_size` 设得很大（比如 4096）不好？设得很小（比如 1）又有什么代价？

> **答案**：太大 → 单个请求最多浪费 `block_size - 1` 槽变大，内部碎片回升，且前缀缓存命中粒度变粗（必须凑满一大块才能复用）；太小 → 块表变长、内核寻址开销和 Python 侧簿记开销上升。默认 16 是在碎片、命中率与开销之间的折中。

**练习 2**：PagedAttention 消除了哪类碎片、保留了哪类碎片？

> **答案**：消除了外部碎片（块等大、统一池化），把内部碎片从「与 max_model_len 同量级」压到「最多 block_size - 1 个槽」。

---

### 4.2 BlockPool：块池的分配、释放与缓存命中

#### 4.2.1 概念说明

`BlockPool` 是**真正持有所有物理块**的对象。它在初始化时一次性创建全部 `num_gpu_blocks` 个 `KVCacheBlock`，之后所有「分配/释放/缓存命中」操作都是在这个固定集合上做标记，**绝不新建或销毁块对象**。

它维护两套数据结构：

- **空闲块队列** `free_block_queue`：一个双向链表，按淘汰顺序（LRU）排列所有「当前可被分配或可被驱逐」的块。取块从队首取，归还的块按淘汰优先级放回。
- **前缀缓存哈希表** `cached_block_hash_to_block`：把「块内容的哈希」映射到「物理块」，用于前缀缓存命中（u4-l5 详讲，本讲只需知道它存在）。

#### 4.2.2 核心流程

`KVCacheBlock` 的核心是**引用计数** `ref_cnt`：

```text
分配 get_new_blocks(n):  从队首弹 n 个块 → 每个 ref_cnt: 0 → 1
命中 touch(blocks):      若 ref_cnt==0(在空闲队列里) 则先从队列移除 → ref_cnt += 1
释放 free_blocks(...):   每个 ref_cnt -= 1 → 归零时放回空闲队列
```

引用计数是「多个请求共享同一物理块」的基础：当两个请求有相同前缀，它们对同一个块各持有一份引用，`ref_cnt=2`；只有当**所有**引用都释放（`ref_cnt` 归零），块才真正回到可被回收的空闲队列。

`null_block`（块 id=0）是一个特殊的占位块，用于滑动窗口等场景中「逻辑上存在但不实际存数据」的位置，它不会被真正释放。

#### 4.2.3 源码精读

`BlockPool` 在构造时把所有块造好，并搭好双向链表和哈希表（[block_pool.py:162-196](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/block_pool.py#L162-L196)）：

```python
self.num_gpu_blocks = num_gpu_blocks
self.blocks: list[KVCacheBlock] = [
    KVCacheBlock(idx) for idx in range(num_gpu_blocks)
]
self.free_block_queue = FreeKVCacheBlockQueue(self.blocks)
self.cached_block_hash_to_block: BlockHashToBlockMap = BlockHashToBlockMap()
...
self.null_block = self.free_block_queue.popleft()   # 占用 id=0
self.null_block.is_null = True
```

`KVCacheBlock` 的元数据（[kv_cache_utils.py:117-176](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_utils.py#L117-L176)）：`block_id`、`ref_cnt`、`_block_hash`（满块缓存哈希）、`prev/next_free_block`（链表指针）、`is_null`。

**分配**：`get_new_blocks` 从队首弹出 n 个块，逐个把 `ref_cnt` 从 0 置 1；若开启了缓存，还要先把块上残留的缓存哈希清除（即「驱逐」这个块在哈希表里的旧记录）（[block_pool.py:647-677](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/block_pool.py#L647-L677)）：

```python
ret: list[KVCacheBlock] = self.free_block_queue.popleft_n(num_blocks)
if self.enable_caching:
    for block in ret:
        self._maybe_evict_cached_block(block)   # 清掉旧哈希记录
        assert block.ref_cnt == 0
        block.ref_cnt += 1
```

**命中（touch）**：前缀缓存命中复用一个块时，把它的引用计数 +1；若它原本在空闲队列里（`ref_cnt==0`），先从队列移除（[block_pool.py:702-717](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/block_pool.py#L702-L717)）：

```python
for block in blocks:
    if block.ref_cnt == 0 and not block.is_null:
        self.free_block_queue.remove(block)
    block.ref_cnt += 1
```

**释放**：`free_blocks` 把每个块 `ref_cnt -= 1`，归零的块按淘汰优先级放回队列尾部；没有哈希的块（永远不会命中前缀缓存）会被优先放到队首附近以先被回收（[block_pool.py:719-742](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/block_pool.py#L719-L742)）：

```python
for block in ordered_blocks:
    block.ref_cnt -= 1
    if block.ref_cnt == 0 and not block.is_null:
        if block.block_hash is None and self.enable_caching:
            blocks_without_hash.append(block)   # 先回收
        else:
            blocks_with_hash.append(block)
self.free_block_queue.prepend_n(blocks_without_hash)
self.free_block_queue.append_n(blocks_with_hash)
```

**容量查询**：`get_num_free_blocks`（[block_pool.py:799-805](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/block_pool.py#L799-L805)）返回链表长度，`get_usage`（[block_pool.py:807-818](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/block_pool.py#L807-L818)）换算成 0~1 的使用率（减去占位的 null block）。

空闲块队列 `FreeKVCacheBlockQueue`（[kv_cache_utils.py:184-320](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/v1/core/kv_cache_utils.py#L184-L320)）刻意不用 Python 内置 `deque`，而是手写双向链表，目的就是支持 `remove`（从链表中间摘除一个块）在 \(O(1)\) 完成——前缀缓存命中时把块从空闲队列摘出正是这种操作。

#### 4.2.4 代码实践

**实践目标**：理解 `ref_cnt` 如何驱动块的分配、共享与回收。

**操作步骤**（源码阅读型实践）：

1. 读 [block_pool.py:647-677](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/block_pool.py#L647-L677) 的 `get_new_blocks`，确认分配只是「弹链表 + ref_cnt++」。
2. 读 [block_pool.py:702-717](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/block_pool.py#L702-L717) 的 `touch`，画出一个「块从空闲队列被命中复用」的状态变化。
3. 读 [block_pool.py:719-742](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/block_pool.py#L719-L742) 的 `free_blocks`，回答：两个请求共享一个块（ref_cnt=2），其中一个请求结束释放，这个块会回到空闲队列吗？

**预期结果**：不会。`free_blocks` 只把 `ref_cnt -= 1`，此时 ref_cnt 由 2→1，仍 >0，块继续被另一个请求持有，不回空闲队列；只有当第二个请求也释放、ref_cnt 归零，块才被回收。

**待本地验证**：若本地能跑测试，可在 `tests/v1/core/` 下找到 block_pool 相关单测，断点观察 `ref_cnt` 变化。

#### 4.2.5 小练习与答案

**练习 1**：`BlockPool` 在运行过程中会新建 `KVCacheBlock` 对象吗？为什么这么设计？

> **答案**：不会。所有块在 `__init__` 时一次性建好（`num_gpu_blocks` 个）。之后分配/释放只是改 `ref_cnt` 和链表指针。这避免了运行期频繁的对象创建/销毁（GC 压力），也让 block_id 与物理显存位置保持稳定、块表只增不改。

**练习 2**：为什么 `FreeKVCacheBlockQueue` 不直接用 `collections.deque`？

> **答案**：前缀缓存命中需要从队列**中间**摘除某个块（`touch` 里的 `remove`），`deque` 做不到 \(O(1)\)。手写双向链表让任意位置摘除都是 \(O(1)\)。

---

### 4.3 KVCacheManager：高层管理与分配主流程

#### 4.3.1 概念说明

`KVCacheManager` 是**调度器直接对话的对象**。它自己不持有块细节，而是组合了一个 `coordinator`（分派器）和一个 `block_pool`（块池），向调度器暴露几个高语义方法：

- `get_computed_blocks(request)`：查这个请求能命中多少前缀缓存块（返回命中块和命中的 token 数）。
- `allocate_slots(request, num_new_tokens, ...)`：为请求要新算的 token 分配块；**这是核心入口**，返回新分配的块，或返回 `None` 表示显存不足。
- `free(request)`：请求结束（或被抢占）时释放它持有的全部块。
- `usage`：当前块使用率（驱动调度器的 watermark 判断）。

#### 4.3.2 核心流程

调度器在 `schedule()` 里对每个想调度的请求依次调用 `allocate_slots`。它内部按「三段式」工作（见源码注释 [kv_cache_manager.py:428-435](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_manager.py#L428-L435)）：

```text
allocate_slots(request, num_new_tokens):
  1. 先释放已滑出注意力窗口的旧块（remove_skipped_blocks），并按需检查整序列能否装下
  2. 调 get_num_blocks_to_allocate 算「还需几个块」;
     若 (需块数 + watermark) > 空闲块数 → 返回 None（显存不足，交给调度器去抢占）
  3. allocate_new_computed_blocks：touch 命中的前缀缓存块（ref_cnt++）
  4. allocate_new_blocks：向 BlockPool 取新块（ref_cnt++）
  5. cache_blocks：把刚写满的块登记进哈希表，供将来命中
  返回 KVCacheBlocks（新分配块的封装）
```

当 `allocate_slots` 返回 `None`，调度器就会抢占（preempt）优先级最低的 running 请求——把它占的块全释放回池子，腾出空间（见 u4-l2）。所以「块不够 → 返回 None → 调度器抢占」是一条关键链路。

#### 4.3.3 源码精读

`KVCacheManager.__init__` 组装 coordinator 和 block_pool，并计算 watermark（[kv_cache_manager.py:151-178](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_manager.py#L151-L178)）：

```python
self.coordinator = get_kv_cache_coordinator(...)
self.block_pool = self.coordinator.block_pool
...
# watermark: 留出若干空闲块做缓冲，避免频繁抢占
assert watermark >= 0.0
self.watermark_blocks = int(watermark * kv_cache_config.num_blocks)
```

`allocate_slots` 是核心。它先算需求量并做容量判断（[kv_cache_manager.py:510-527](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_manager.py#L510-L527)）：

```python
num_blocks_to_allocate = self.coordinator.get_num_blocks_to_allocate(...)
available_blocks = self.block_pool.get_num_free_blocks() - reserved_blocks
required_blocks = num_blocks_to_allocate + watermark_blocks
if required_blocks > available_blocks:
    return None          # 显存不足
```

容量够，则依次 touch 命中块、取新块、缓存满块（[kv_cache_manager.py:529-565](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_manager.py#L529-L565)）：

```python
self.coordinator.allocate_new_computed_blocks(...)       # touch 命中块
new_blocks = self.coordinator.allocate_new_blocks(...)   # 取新块
...
num_tokens_to_cache = min(total_computed_tokens + num_new_tokens, request.num_tokens)
self.coordinator.cache_blocks(request, num_tokens_to_cache)  # 登记满块
return self.create_kv_cache_blocks(new_blocks)
```

`get_num_blocks_to_allocate` 本身在 coordinator 里按 group 分派（[kv_cache_coordinator.py:130-190](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_coordinator.py#L130-L190)），最终落到 `SingleTypeKVCacheManager.get_num_blocks_to_allocate`，核心计算是 `cdiv(num_tokens, block_size)` 减去已持有块数（[single_type_kv_cache_manager.py:178-200](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/single_type_kv_cache_manager.py#L178-L200)）：

```python
num_required_blocks = cdiv(num_tokens, self.block_size)
...
return max(num_required_blocks - num_req_blocks, 0)
```

最底层真正取块的是 `SingleTypeKVCacheManager.allocate_new_blocks`（[single_type_kv_cache_manager.py:330-369](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/single_type_kv_cache_manager.py#L330-L369)），它调用 `self.block_pool.get_new_blocks(num_new_blocks)` 把块挂到请求的 `req_to_blocks` 列表上：

```python
num_required_blocks = cdiv(num_tokens, self.block_size)
num_new_blocks = num_required_blocks - len(req_blocks)
...
new_blocks = self.block_pool.get_new_blocks(num_new_blocks)
req_blocks.extend(new_blocks)
```

`free` 把请求的所有块归还（[kv_cache_manager.py:567-578](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_manager.py#L567-L578)），最终走到 `BlockPool.free_blocks`，按逆序释放以让尾部块优先被淘汰。

#### 4.3.4 代码实践

**实践目标**：跟踪一个请求「申请新 block」的完整调用链。

**操作步骤**（调用链追踪型实践）：

1. 在调度器入口定位调用点：[scheduler.py:576-586](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L576-L586)，调度器循环调用 `kv_cache_manager.allocate_slots(...)`，若返回 `None` 就去抢占别的请求。
2. 进入 [kv_cache_manager.py:344-565](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_manager.py#L344-L565) 的 `allocate_slots`，依次标注「算需求量→容量判断→touch 命中→取新块→缓存」五步。
3. 跟到 [single_type_kv_cache_manager.py:330-369](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/single_type_kv_cache_manager.py#L330-L369)，再跟到 [block_pool.py:647-677](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/block_pool.py#L647-L677) 的 `get_new_blocks`。
4. 用一句话写出整条链路。

**预期结果**：`Scheduler.allocate_slots` → `KVCacheManager.allocate_slots` → `coordinator.allocate_new_blocks` → `SingleTypeKVCacheManager.allocate_new_blocks` → `BlockPool.get_new_blocks`（弹链表、ref_cnt++）。

**待本地验证**：可在 `allocate_slots` 入口与 `get_new_blocks` 出口各加一行日志（标注 request_id、返回的 block_id 列表），跑一个短 prompt 观察每步分配了哪些块。

#### 4.3.5 小练习与答案

**练习 1**：`allocate_slots` 为什么需要 `watermark_blocks`？去掉它会怎样？

> **答案**：watermark 是「宁可少用，也要留点空闲块」的安全垫。若无 watermark，空闲块接近 0 时，新请求一旦进来就可能立刻装不下，导致频繁抢占、来回换出，吞吐下降。留 buffer 让系统有喘息空间，减少抖动。

**练习 2**：`allocate_slots` 返回 `None` 后，调度器会做什么？为什么？

> **答案**：调度器会抢占优先级最低的 running 请求（释放其全部块回池子、进度清零回 WAITING），腾出空间再重试 `allocate_slots`。因为块不够时唯一能「凭空变出空间」的办法就是把别人的块换出去——用计算换显存。

---

### 4.4 KVCacheBlocks：调度器与缓存管理器之间的接口

#### 4.4.1 概念说明

`KVCacheBlocks` 是 `KVCacheManager.allocate_slots` 的**返回值**，也是调度器与缓存管理器之间的**契约对象**。它把「这一步新分配了哪些块」封装好，对调度器**隐藏**了管理器内部的数据结构（coordinator、single-type manager、`req_to_blocks` 等）。调度器只需要这些块的整数 ID，去组装发给 GPU worker 的块表。

#### 4.4.2 核心流程

```text
allocate_slots(...) → 返回 KVCacheBlocks
    ↓ .get_block_ids()
tuple[list[int], ...]   # 外层=kv cache group，内层=该组的 block_id 列表
    ↓
写入 SchedulerOutput，跨进程发给 worker，worker 据此搭建块表
```

它支持把两次分配结果拼接（`__add__`），也支持产出一个「空」对象（`new_empty`）——后者由管理器预构建并复用，避免反复创建空对象带来的 GC 开销。

#### 4.4.3 源码精读

`KVCacheBlocks` 是个 dataclass，核心字段是按 group 分组的块元组（[kv_cache_manager.py:32-53](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_manager.py#L32-L53)）：

```python
@dataclass
class KVCacheBlocks:
    """分配结果，作为 Scheduler 与 KVCacheManager 的接口，
    对 Scheduler 隐藏 KVCacheManager 的内部数据结构。"""
    blocks: tuple[Sequence[KVCacheBlock], ...]
    # blocks[i][j] = 第 i 个 kv_cache_group 的第 j 个块
```

`get_block_ids` 把块对象转成整数 ID（[kv_cache_manager.py:64-91](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_manager.py#L64-L91)）：

```python
def get_block_ids(self, allow_none=False) -> tuple[list[int], ...] | None:
    if allow_none and all(len(group) == 0 for group in self.blocks):
        return None
    return tuple([blk.block_id for blk in group] for group in self.blocks)
```

`__add__` 用 `itertools.chain` 按 group 拼接两次分配（[kv_cache_manager.py:55-62](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_manager.py#L55-L62)）。管理器还在初始化时预构建一个空对象供复用（[kv_cache_manager.py:185-187](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_manager.py#L185-L187)）：

```python
self.empty_kv_cache_blocks = KVCacheBlocks(
    tuple(() for _ in range(self.num_kv_cache_groups))
)
```

调度器拿到分配结果后，正是通过 `get_block_ids` 取出 ID 装进 `SchedulerOutput`，例如 [scheduler.py:1069](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L1069) 处 `self.kv_cache_manager.get_blocks(request_id)` 再 `.get_block_ids()` 的用法。

#### 4.4.4 代码实践

**实践目标**：理解 `KVCacheBlocks` 在调度器与管理器之间扮演的「隔离层」角色。

**操作步骤**：

1. 读 [kv_cache_manager.py:32-114](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_manager.py#L32-L114)，列出它的全部公开方法。
2. 在 [scheduler.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py) 中搜索 `get_block_ids`，观察调度器是否**只**通过这个方法消费分配结果，而不直接碰 `coordinator` / `req_to_blocks`。
3. 回答：如果管理器内部把 `req_to_blocks` 从 `dict` 改成别的结构，调度器需要改动吗？

**预期结果**：调度器只依赖 `KVCacheBlocks.get_block_ids()` 返回的整数 ID，不接触管理器内部结构，所以管理器内部重构不影响调度器——这正是接口隔离的价值。

**待本地验证**：可在 `get_block_ids` 加断点/日志，打印每次返回的 block_id 列表，对照一个 step 内各请求实际写入的块。

#### 4.4.5 小练习与答案

**练习 1**：`KVCacheBlocks.blocks` 为什么外层是「group」维度、内层才是「块」维度，而不是反过来？

> **答案**：因为不同 kv_cache_group（如全注意力层、Mamba 层）可能有**不同**的 block_size，每个 group 各自管理自己的块。以 group 为外层，能干净地表达「这个请求在每个 group 里分别分到了哪些块」。目前各 group 块数相同，但这个结构为将来「不同 group 不同 block_size」留了余地（见字段注释）。

**练习 2**：为什么管理器要预构建并复用一个 `empty_kv_cache_blocks`？

> **答案**：很多请求在某个 step 没有新分配的块（比如纯 decode 只用已有块），如果每次都 `new` 一个空对象会造成不必要的 GC 压力。预构建一个不可变空对象反复复用，既省开销又保证语义一致（见 [kv_cache_manager.py:180-187](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_manager.py#L180-L187) 的注释）。

## 5. 综合实践

**任务**：用一张图把「一个请求如何申请并持有 KV 块、显存不足时如何被换出」串起来。

请按下列步骤完成（全程基于源码阅读，可辅以日志验证）：

1. **画显存布局**：画一个池子，里面是 N 个等大的 block；标出 `free_block_queue`（双向链表）、`cached_block_hash_to_block`（哈希表）、`null_block`。
2. **走一遍分配**：假设一个全新请求（prompt 40 个 token，`block_size=16`，无前缀命中）：
   - 算出需要 `⌈40/16⌉ = 3` 个块；
   - 标出 [block_pool.py:647-677](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/block_pool.py#L647-L677) `get_new_blocks` 弹出 3 个块、`ref_cnt` 各变 1；
   - 标出这 3 个块的 ID 经 `KVCacheBlocks.get_block_ids` 进入块表发给 worker。
3. **走一遍共享**：再来一个请求，前 32 个 token 与前者完全相同：
   - 指出 [block_pool.py:702-717](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/block_pool.py#L702-L717) `touch` 让前 2 个块的 `ref_cnt` 变 2，无需重复计算这 32 个 token 的 K/V。
4. **走一遍换出**：假设显存满了，新请求 `allocate_slots` 返回 `None`：
   - 指出调度器抢占某 running 请求，调用 [kv_cache_manager.py:567-578](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_manager.py#L567-L578) `free`，其块经 `BlockPool.free_blocks` 把 `ref_cnt` 减 1、归零的块放回空闲队列尾部；
   - 被抢占请求进度清零，等下次重新 prefill。

**产出**：一张标注了 `ref_cnt` 变化与块流向的状态图，配一段说明「PagedAttention 如何用统一块池 + 引用计数同时消除碎片并支持共享」。这正是 u4-l2 中「调度器在显存不足时抢占」背后真正的显存机制。

## 6. 本讲小结

- **PagedAttention 的核心**是把 KV 缓存切成等大 block，用块表做逻辑→物理映射，**消除外部碎片**、把内部碎片压到「最多 block_size - 1 个槽」。
- **`block_size`**（默认 16）同时是分配粒度和前缀缓存命中粒度，由 `resolve_kv_cache_block_sizes` 解析、由 `SingleTypeKVCacheManager` 持有。
- **`BlockPool`** 是块的真正所有者：构造时一次性建好全部块，靠 `ref_cnt`（引用计数）和 `free_block_queue`（双向链表）管理分配/释放/淘汰，运行期不新建块对象。
- **引用计数**是共享的基础：`get_new_blocks` 让 ref_cnt 0→1，`touch` 在命中时 +1，`free_blocks` 在 -1 归零时才回收到队列。
- **`KVCacheManager`** 是调度器的高层接口，`allocate_slots` 按「算需求→判容量→touch 命中→取新块→缓存满块」五步工作；块不足返回 `None` 触发调度器抢占。
- **`KVCacheBlocks`** 是分配结果的契约对象，通过 `get_block_ids` 把块对象转成整数 ID 送给 worker，向调度器隐藏管理器内部结构。

## 7. 下一步学习建议

- **下一讲 u4-l5 前缀缓存（Prefix Caching）**：本讲只提到 `cached_block_hash_to_block` 和 `touch`，下一讲会讲透 block hash 如何计算、`BlockHashList` 如何链式哈希、命中与驱逐的时机。建议读 [vllm/v1/core/kv_cache_utils.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/kv_cache_utils.py) 中 `get_block_hash` 与 [docs/design/prefix_caching.md](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/prefix_caching.md)。
- **u5-l2 GPU Worker**：本讲的块 ID 经块表发给 worker 后，worker 如何用块表在 GPU 上真正读写 KV 缓存、`num_gpu_blocks` 如何由 profiling 决定，将在 worker 讲展开。
- **u8-l1 注意力后端**：块表最终被注意力内核消费（paged attention kernel），届时可以看到 `block_table[logical]` 如何定位物理 K/V——与本讲的块表呼应。
