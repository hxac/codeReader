# Prefix 缓存与 BlockTrie

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚在 Paged Attention 之上做「前缀缓存（prefix caching）」的动机与收益；
- 画出 `BlockTrie` 与 `Node` 的数据结构，解释为什么用 trie 来组织 KV 块；
- 读懂 `BlockTrie` 的三个核心动作 `match` / `allocate` / `evict` 是如何与调度器、块管理器协作的；
- 理解 `ref_count` 如何让多个请求安全地共享同一块 KV；
- 认识 `PrefixCacheStats` 如何统计命中率，以及为什么命中需要支持「回滚（rollback）」。

本讲聚焦**纯文本 / VLM 的 KV 前缀缓存路径**。`block_trie.py` 中还包含一大段针对 SSM（状态空间模型，如 Mamba）的「循环状态检查点」逻辑，属于更进阶的场景，本讲只在第 4.7 节给出索引，不展开。

## 2. 前置知识

### 2.1 Paged Attention 与分块 KV 缓存

在 u4-l5 我们已经建立：序列的 KV cache 不是一段连续显存，而是被切成一个个固定大小 `block_size` 的**块（block）**，序列只持有一张「块表（block table）」——即逻辑块号 → 物理 KV 块的指针列表。一个长度为 \(L\) 的序列占用

\[
\lceil L / \text{block\_size} \rceil
\]

个块。这是后续一切共享的基础：**只要两个序列指向同一个物理块，它们就共享了那一段 KV。**

### 2.2 前缀缓存要解决什么

很多请求有**公共前缀**：同一个 system prompt、同一份长文档、few-shot 的相同示例。如果每个请求都从头算一遍 prefill，既浪费算力又拉高首 token 延迟（TTFT）。前缀缓存的想法是：把已经算好的前缀 KV 块「留着」，后续请求只要前缀 token 完全相同，就直接复用这些块，跳过对应的 prefill 计算。

### 2.3 为什么用 trie（前缀树）

判断「两个请求有多少公共前缀」需要按块逐级比对。trie 天然表达这种「逐级前缀」关系：

- 根节点是空前缀；
- 每条边代表一个 `block_size` 长度的 token 块；
- 从根到某节点的路径，就对应一段确定的 token 序列。

用 trie，匹配公共前缀就是「从根往下走，能走多远算多远」；插入新算出的块就是「在当前路径末端长出孩子」。这比线性扫描所有已缓存序列高效得多。

### 2.4 关键术语速查

| 术语 | 含义 |
|------|------|
| `block_size` | 一个 KV 块容纳的 token 数 |
| `logical_blocks` | 序列的块表（逻辑块号序列） |
| `ref_count` | 一个物理块被引用的次数（序列引用 + trie 自身持有） |
| `Node` | trie 中的一条边，代表一个满 token 块 |
| `last_shared_node` | 序列当前在 trie 里「已经共享到的最深节点」 |
| `leaves` | trie 当前所有叶子节点集合（驱逐候选来源） |

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [lmdeploy/pytorch/paging/block_trie.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_trie.py) | 本讲主角。定义 `PrefixCacheStats`、`Node`、`BlockTrie`，实现前缀的匹配 / 插入 / 驱逐。 |
| [lmdeploy/pytorch/paging/scheduler.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py) | 调度器，是 `BlockTrie` 的唯一调用方，规定 `match → evict → allocate → 发布` 的顺序，并负责命中失败时的回滚。 |
| [lmdeploy/pytorch/messages.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/messages.py) | 定义 `PrefixCacheState`（每个序列的前缀缓存簿记）和 `SchedulerSequence.prefix_cache` 字段。 |
| [lmdeploy/pytorch/config.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py) | `CacheConfig.enable_prefix_caching` 开关（默认 `False`）。 |
| [lmdeploy/pytorch/paging/block_manager/base_block_manager.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_manager/base_block_manager.py) | 提供 `add_ref_count` / `update_access_time` / `free` 等「引用计数 + LRU」原子操作，`BlockTrie` 直接调它。 |
| [lmdeploy/pytorch/paging/eviction_helper/recompute_eviction_helper.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/eviction_helper/recompute_eviction_helper.py) | 显存不足时触发 `block_trie.evict(...)`，把缓存的前缀块驱逐掉腾地方。 |

## 4. 核心概念与源码讲解

### 4.1 前缀缓存的核心思想

#### 4.1.1 概念说明

把分块 KV 想象成一个「共享的对象存储」：每个物理块是一段不可变的 KV 张量。前缀缓存做的事是——

1. **命名**：用块内 token id（加上多模态内容的哈希，见 4.1.3）给每段前缀算一个唯一「键」；
2. **查表**：新请求来时，按块逐级在 trie 里查「这段前缀我之前算过没」；
3. **引用**：算过就给那些块「+1 引用」，把块号写进自己的块表，于是这个请求的 prefill 可以从命中点之后才开始；
4. **回收**：请求结束「-1 引用」；当某块再没人用、且显存紧张时，从 trie 叶子按 LRU 驱逐。

关键安全性来自**引用计数**：只要还有一个序列在用某块，`free` 就不会真正释放它（见 4.5.2）。所以多个请求共享同一前缀块是安全的。

#### 4.1.2 核心流程：一次 prefill 的前缀缓存协作

调度器在 `_schedule_prefill` 中严格按以下顺序使用 `BlockTrie`（见 [scheduler.py:1-39](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L1-L39) 的模块文档）：

```
1. block_trie.match(seq)      # 试探性匹配前缀：可能给 seq 挂上共享块、推进 step
2. 检查驱逐 / SSM 资源是否够  # 不够就回滚第 1 步
3. block_manager.allocate(seq) # 为「未命中」的尾部补发新块
4. block_trie.allocate(seq)    # 把新算出的满块挂回 trie（供后续请求复用）
5. _finish_prefix_cache_schedule(seq)  # 发布 cached_tokens 统计
```

注意第 1 步是**试探性（tentative）**的：`match()` 会立即修改序列状态（推进 step、挂共享块），但若随后资源检查失败，调度器必须把这次匹配**回滚**，让序列在下一轮干净地重排。回滚逻辑见 [scheduler.py:166-198](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L166-L198)。

#### 4.1.3 块的「键」如何构造

trie 节点的键不能只用 token id。对 VLM（视觉语言模型），两段相同的图像占位 token 可能背后是**不同的图片**。因此键里还要带上多模态内容的稳定哈希：

```python
# lmdeploy/pytorch/paging/block_trie.py:273-276
@staticmethod
def _make_key(tokens: np.ndarray, extra_hashes: PrefixCacheExtraHashes):
    """Make the trie lookup key from tokens plus multimodal identity."""
    return hash(('random', tuple(tokens), extra_hashes))
```

由于 `hash` 可能碰撞，查到节点后还会用 `_match_node` 逐字节复核 token 与 extra_hashes 是否真的相等（[block_trie.py:278-281](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_trie.py#L278-L281)）。这是「哈希定速 + 精确复核」的两段式查表。

> 多模态内容哈希怎么来的？见 [messages.py:46-64](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/messages.py#L46-L64) 的 `PrefixCacheMeta`，它记录每段多模态数据的 `(start, end, modality, content_hash)`，u9-l1 讲过这部分由视觉前端对像素做 SHA-256 填入。

### 4.2 Node：trie 节点结构

#### 4.2.1 概念说明

`Node` 是 trie 中的一条边，代表「一个 `block_size` 长度的满 token 块」。每个节点存四样东西：

- **身份信息**：`hash_key`（键）、`tokens`（块内 token 数组）、`extra_hashes`（多模态哈希），三者共同决定节点身份；
- **物理位置**：`block`，指向块管理器里的物理 KV 块号；
- **拓扑信息**：`children`（子节点字典）、`_parent`（父节点）、`num_matched`（从根到本节点的累计 token 数，即本节点对应的步数）；
- **可选的 SSM 状态字段**：`state_idx` / `state_ready` / `state_ref_count` 等，本讲不展开。

#### 4.2.2 核心流程：父子指针如何维护

`Node` 用一个 property 拦截 `parent` 赋值，自动维护父节点的 `children` 字典，保证「设置 parent ⟺ 进入父节点的 children」这件事不会被忘记：

```python
# lmdeploy/pytorch/paging/block_trie.py:151-162
@property
def parent(self):
    return self._parent

@parent.setter
def parent(self, val: 'Node'):
    old_parent = self._parent
    if old_parent is not None:
        old_parent.children.pop(self.hash_key)   # 先从旧父节点摘除
    if val is not None:
        val.children[self.hash_key] = self        # 再挂到新父节点
    self._parent = val
```

`__init__` 里把 `children` 初始化为空 dict、`_parent` 设为 `None`（[block_trie.py:125-149](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_trie.py#L125-L149)）。

另外 `__lt__` / `__le__` 恒返回 `True`（[block_trie.py:164-168](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_trie.py#L164-L168)），这是为了让 `Node` 能被放进 `heapq`（驱逐时按 `access_time` 建堆，Python 的堆要求元素可比较，元组比较会在 `access_time` 相等时回退到比第二个元素 `Node`，恒真避免了「Node 不可比较」的报错）。

#### 4.2.3 源码精读

[Node 类定义与字段：lmdeploy/pytorch/paging/block_trie.py:112-168](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_trie.py#L112-L168) —— 一个 trie 节点持有：身份（hash_key/tokens/extra_hashes）、物理块号（block）、累计步数（num_matched）、子节点字典（children）、父指针（_parent），以及一组 SSM 检查点字段。

可以画成：

```
Node
├── hash_key : int              # _make_key 的结果，作为父节点 children 字典的键
├── block    : int              # 指向物理 KV 块号（root 为 -1）
├── tokens   : np.ndarray       # 本块的 block_size 个 token id
├── extra_hashes : tuple        # 多模态内容哈希（纯文本为空元组）
├── num_matched : int           # 从根到本节点的累计 token 数（root 为 0）
├── children : dict[int, Node]  # hash_key -> 子节点
├── _parent  : Node | None
└── (state_idx / state_ready / ... : SSM 专用，本讲略)
```

#### 4.2.4 代码实践：画出 trie 结构

**实践目标**：把 `Node` 结构内化为一张图。

**操作步骤**：

1. 打开 [block_trie.py:112-168](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_trie.py#L112-L168)；
2. 假设 `block_size=4`，请求 A 的 token 是 `[s0..s7, a0..a3]`，请求 B 的 token 是 `[s0..s7, b0..b3]`（前 8 个 token 相同）；
3. 在纸上画出：root → Node₁（`s0..s3`）→ Node₂（`s4..s7`），Node₂ 有两个孩子 Node₃（`a0..a3`，A 独有）与 Node₄（`b0..b3`，B 独有）。

**预期结果**：你会看到一个标准的「共享前缀、分叉后缀」的 trie 形状，Node₁、Node₂ 被两个请求共享（`ref_count=2` 加上 trie 自身持有），这正是前缀缓存省算力的来源。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Node.parent` 要用 setter 而不是直接暴露 `_parent` 字段？

> **答案**：为了保证「节点挂到父节点的 children 字典」与「设置自身 _parent」是原子的、不会遗漏。如果直接赋值，很容易出现「改了 _parent 却忘了更新父节点的 children」，trie 拓扑就会断裂。setter 把这个不变式封装在一处。

**练习 2**：节点的 `num_matched` 字段有什么用？

> **答案**：它记录「从根到本节点累计匹配了多少 token」，等于 `block_size × 深度`。匹配命中后用它来推进序列的 `seq.set_step(num_matched)`，让 prefill 直接从命中点之后开始；SSM 检查点匹配时也用它作为「步数」索引。

### 4.3 BlockTrie：插入 / 匹配 / 驱逐

#### 4.3.1 概念说明

`BlockTrie` 是前缀缓存的所有者，聚合了三类资源：

```python
# lmdeploy/pytorch/paging/block_trie.py:185-204  （节选）
self.block_manager = block_manager
self.allocator = self.block_manager.allocator
self.block_size = cache_config.block_size
self.enable = self.cache_config.enable_prefix_caching
...
self._roots: dict[str, Node] = dict()   # 每个 adapter（LoRA）一个独立根
self.leaves: set[Node] = set()          # 叶子节点集合（驱逐候选）
self.stats = PrefixCacheStats()
```

几点要点：

- **`enable` 默认关**：`CacheConfig.enable_prefix_caching` 默认 `False`（[config.py:120](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L120)），用户要在 `PytorchEngineConfig(enable_prefix_caching=True)` 显式打开；并且若开了滑动窗口注意力（`window_size>1`）会被强制关闭（[config.py:139-141](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L139-L141)），因为滑动窗口下 KV 会被丢弃，前缀缓存无意义。
- **每个 adapter 一个根**：不同 LoRA 适配器下的 KV 不能混用，所以 `_roots` 是 `adapter_name → 根 Node` 的字典（[block_trie.py:262-266](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_trie.py#L262-L266) 的 `get_root` 按需懒创建）。
- **`leaves` 是驱逐快车道**：驱逐只在叶子（没有孩子的节点）上做，因为删叶子不会影响其他序列的前缀路径。

#### 4.3.2 核心流程：match / allocate / evict 三部曲

**match(seq) ——「我这段前缀你算过没？」**

从序列的 `last_shared_node`（没有就从 adapter 根）开始，按 `block_size` 步长逐块在 trie 里下走。每块算 key、查 children、复核 payload，走到走不动为止；命中的块「引用 +1」并写进序列块表，推进 step：

```python
# lmdeploy/pytorch/paging/block_trie.py:1098-1144  （核心循环 + 命中收尾，节选）
while num_matched + block_size < seq.num_valid_ids:
    start = num_matched
    end = num_matched + block_size
    curr_tokens = seq.history_cache[start:end]
    extra_hashes = self._get_block_extra_hashes(seq, start, end)
    key = self._make_key(curr_tokens, extra_hashes)
    if key not in curr.children:
        break
    child = curr.children[key]
    if not self._match_node(child, curr_tokens, extra_hashes):
        break
    matched_nodes.append(child)
    __match_success(child)            # curr=child; num_matched += block_size

...
if len(matched_blocks) > 0:
    matched_blocks = np.array(matched_blocks)
    self.allocator.update_access_time(matched_blocks)   # 刷新 LRU 时间
    self.allocator.add_ref_count(matched_blocks, 1)     # 引用 +1
    seq.logical_blocks.append(matched_blocks)           # 共享块挂进块表
    seq.set_step(num_matched)                           # 跳过已缓存前缀
```

注意循环条件 `num_matched + block_size < seq.num_valid_ids`：**最后一个不满 `block_size` 的尾巴块不参与匹配**，因为只有满块才有完整 KV 可复用（尾部块要么还没算完，要么会被本轮 forward 重算）。

**allocate(seq) ——「把新算出的满块挂回 trie」**

`match` 只负责复用已缓存的块；本轮 prefill 新算出的满块，要由 `allocate` 挂进 trie，变成后续请求可复用的资产。它从 `last_shared_node` 续接，对每个新满块：

- 若 key 已存在且不是「故意丢弃的私有块」，说明别的序列先插入了相同前缀 → **复用 trie 的块，释放本序列多分配的重复块**；
- 否则创建新 `Node` 挂上去。

```python
# lmdeploy/pytorch/paging/block_trie.py:1195-1228  （节选）
hash_key = self._make_key(curr_tokens, extra_hashes)
parent = node
if hash_key in parent.children:
    child = parent.children[hash_key]
    ...
    node = child
    self._try_cache_node_routed_experts(node, seq, start, end)
    if block != node.block:                 # 本序列分到了重复块
        free_blocks.append(block)           # 释放它
        logical_blocks[block_id] = node.block   # 改成共享 trie 块
        blocks.append(node.block)
else:
    node = Node(hash_key=hash_key, block=block, tokens=curr_tokens,
                num_matched=num_matched + block_size,
                extra_hashes=extra_hashes, ...)
    node.parent = parent                    # 自动挂进 parent.children
    blocks.append(node.block)
```

这里把新块统一 `add_ref_count(+1)`，把重复块 `free` 掉（[block_trie.py:1236-1239](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_trie.py#L1236-L1239)）。`free` 内部会把 ref_count 减 1，只有归零才真正回收（见 4.5.2）。

**evict(max_num_blocks) ——「显存不够了，按 LRU 踢叶子」**

当块管理器空闲块不足时，驱逐助手调用 `block_trie.evict(num_req)`（[recompute_eviction_helper.py:45](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/eviction_helper/recompute_eviction_helper.py#L45)）。驱逐只挑满足三个条件的叶子：①在 `leaves` 集合里且仍挂载；②`ref_count == 1`（只有 trie 自己持有，没有序列在用）；③没被 SSM 检查点钉住。按 `access_time` 建小顶堆，逐个弹出：

```python
# lmdeploy/pytorch/paging/block_trie.py:1247-1269  （摘叶子的内层函数，节选）
def __remove_leaf(leaves, evicted_blocks):
    while len(leaves) > 0:
        _, leaf = heapq.heappop(leaves)
        if leaf not in self.leaves:
            continue
        if not self._is_evict_candidate_leaf(leaf):
            self.leaves.discard(leaf)
            continue
        if self._is_pinned_state_checkpoint(leaf):
            continue
        if int(self.allocator.get_ref_count(leaf.block)) != 1:   # 必须无人共享
            continue
        break
    else:
        return False, None
    evicted_blocks.append(leaf.block)
    self.release_state_checkpoint(leaf)
    parent = leaf.parent
    if parent is not None:
        leaf.parent = None            # 摘除：触发从父节点 children 移除
    self.leaves.discard(leaf)
    return True, parent
```

删掉一个叶子后，它的父节点可能变成新叶子，于是把父节点补进堆继续（[block_trie.py:1305-1313](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_trie.py#L1305-L1313)）。最后统一 `allocator.free(evicted_blocks)` 真正回收（[block_trie.py:1317](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_trie.py#L1317)）。

#### 4.3.3 源码精读

- [BlockTrie 构造与状态：lmdeploy/pytorch/paging/block_trie.py:185-204](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_trie.py#L185-L204) —— 聚合 block_manager/allocator、`enable` 开关、按 adapter 的根表 `_roots`、叶子集 `leaves`、统计 `stats`。
- [match（纯文本/VLM 主路径）：lmdeploy/pytorch/paging/block_trie.py:1064-1158](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_trie.py#L1064-L1158) —— 逐块下走 trie，命中则 +1 引用、推进 step；含多模态 span 的安全裁剪 `clamp_prefix_cache_match_step`。
- [allocate：lmdeploy/pytorch/paging/block_trie.py:1160-1240](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_trie.py#L1160-L1240) —— 把新满块挂回 trie，遇到重复 key 则复用 trie 块并释放本序列的重复块。
- [evict：lmdeploy/pytorch/paging/block_trie.py:1242-1319](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_trie.py#L1242-L1319) —— LRU 驱逐叶子（`ref_count==1`），删叶后父节点递补。
- [调度器里的调用点：scheduler.py:663 / 716](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L649-L721) —— `_schedule_prefill` 中先 `match`、后 `block_manager.allocate`、再 `block_trie.allocate`、最后 `_finish_prefix_cache_schedule`。

#### 4.3.4 代码实践：用相同 system prompt 发两次请求，观察第二次命中

**实践目标**：亲手看到前缀缓存生效，第二次请求跳过公共前缀的 prefill。

**操作步骤**（需要本地有可用的 GPU 与一个小模型，否则按「源码阅读型」替代方案做）：

1. 启用前缀缓存创建 pipeline（**示例代码**）：

   ```python
   # 示例代码：需本地 GPU + 模型，待本地验证
   from lmdeploy import pipeline, PytorchEngineConfig, GenerationConfig

   backend = PytorchEngineConfig(enable_prefix_caching=True)
   pipe = pipeline('Qwen/Qwen2.5-7B-Instruct', backend_config=backend)

   sys_prompt = '你是一个严谨的中文助手。' * 50  # 制造一段较长的公共前缀
   msgs1 = [{'role': 'system', 'content': sys_prompt},
            {'role': 'user', 'content': '1+1=?'}]
   msgs2 = [{'role': 'system', 'content': sys_prompt},
            {'role': 'user', 'content': '2+2=?'}]

   pipe([msgs1, msgs2])  # 第二条的 system 前缀应命中第一条
   ```

2. 开启调试日志观察命中：

   ```bash
   LMDEPLOY_LOG_LEVEL=DEBUG python your_script.py 2>log.txt
   grep "Prefix-cache match" log.txt
   ```

**需要观察的现象**：日志里第二次请求会出现类似

```
Prefix-cache match: ... init_step=0 matched_step=<N> candidate_step=<N> clamped=False
```

`matched_step` 即命中的 token 数；与第一次相比，第二次的 prefill token 数应明显减少。

**预期结果**：`matched_step` 接近公共 system prompt 的 token 数（按 `block_size` 向下取整）。若看不到日志，说明：①没开 `enable_prefix_caching`；②`block_size` 较大而前缀太短不足一块；③两次请求不在同一进程 / 同一 adapter。

**替代方案（无需 GPU）**：阅读 [block_trie.py:1148-1158](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_trie.py#L1148-L1158) 的 `match` 末尾日志字段，写出 `init_step / matched_step / candidate_step / clamped` 各自含义，并在 [scheduler.py:914-923](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L914-L923) 找到 `prefix_cache_hit_rate` 是如何从 `block_trie.hit_rate()` 暴露到 `ScheduleMetrics` 的。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `match` 的循环条件是 `num_matched + block_size < seq.num_valid_ids`（严格小于），而不是 `<=`？

> **答案**：只有「完整的 `block_size` 个 token」才对应一块已算满、可被复用的 KV。序列末尾不足一块的尾巴要么尚未计算、要么要由本轮 forward 重新生成，不能复用。严格 `<` 保证了进入循环时 `[num_matched, num_matched+block_size)` 是一整块完整 token。

**练习 2**：`allocate` 里发现「key 已存在」时，为什么要把本序列的 `block` 释放掉、改用 trie 里那个 `node.block`？

> **答案**：说明另一个序列已经为这段相同前缀算过并挂进了 trie。如果本序列继续用自己的新块，就会出现「两份内容相同的 KV」，既浪费显存又让后续驱逐复杂化。复用 trie 块并释放重复块，让这段前缀真正被多序列共享（`ref_count` 上升），这正是前缀缓存省显存的关键。

**练习 3**：驱逐时为什么要求 `get_ref_count(leaf.block) == 1`？

> **答案**：`ref_count==1` 表示此刻只有 trie 自己持有这块、没有任何活跃序列在用它，删掉它不会破坏任何请求的前缀路径。若 `ref_count>1`，说明有序列正在共享它，强行释放会导致那些序列的 KV 被破坏。`free` 本身也内置了同样的保护——只有 ref 归零才真正回收物理块。

### 4.4 PrefixCacheStats：命中率与回滚

#### 4.4.1 概念说明

前缀缓存好不好用，要看「命中率」。`PrefixCacheStats` 是个极简的统计类，只记两个数：

```python
# lmdeploy/pytorch/paging/block_trie.py:86-101
@dataclass
class PrefixCacheStats:
    """Prefix caching stats."""
    num_query_tokens: int = 0
    num_hit_tokens: int = 0
    ...
    def hit_rate(self):
        return 0.0 if self.num_query_tokens <= 0 else float(self.num_hit_tokens) / self.num_query_tokens
```

命中率定义为

\[
\text{hit\_rate} = \frac{\text{num\_hit\_tokens}}{\text{num\_query\_tokens}}
\]

其中 `num_query_tokens` 是这次匹配**尝试**覆盖的 token 数，`num_hit_tokens` 是其中**真正命中**的 token 数（见 [block_trie.py:1148-1151](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_trie.py#L1148-L1151) 的 `_record_match_stats`）。每个 `BlockTrie` 实例持有一个 `stats`，命中率经 `hit_rate()` → `schedule_metrics` 暴露（[scheduler.py:914-923](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L914-L923)）。

#### 4.4.2 核心流程：为什么统计要能「回滚」

回忆 4.1.2：`match()` 是试探性的，命中后会立即累加统计；但若随后资源检查失败、调度器回滚了这次匹配，那这次命中**从未真正发生**，统计必须一起撤回。因此 `PrefixCacheStats` 提供了快照 / 恢复对：

```python
# lmdeploy/pytorch/paging/block_trie.py:96-101 / 210-221
def copy(self):
    """Copy stats for tentative-match rollback."""
    return PrefixCacheStats(num_query_tokens=self.num_query_tokens,
                            num_hit_tokens=self.num_hit_tokens)

def snapshot_stats(self):       # 匹配前快照
    if not self.enable:
        return None
    return self.stats.copy()

def restore_stats(self, snapshot):   # 回滚时恢复
    if snapshot is None:
        return
    self.stats.num_query_tokens = snapshot.num_query_tokens
    self.stats.num_hit_tokens = snapshot.num_hit_tokens
```

调度器在 `match` 前调 `snapshot_stats()` 存档（[scheduler.py:651-653](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L649-L660)），回滚时调 `restore_stats()`（[scheduler.py:166-190](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L166-L190) 的 `_rollback_unscheduled_prefix_match`）。

还有一个细节：被「recompute 驱逐」触发的重算工作，会设置 `suppress_match_stats=True`（[recompute_eviction_helper.py:37-38](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/eviction_helper/recompute_eviction_helper.py#L37-L45)），让 `_record_match_stats` 直接跳过（[block_trie.py:223-228](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_trie.py#L223-L228)）。原因是这些重算命中是「自我恢复」而非「用户请求受益」，不应美化公开的命中率指标。

#### 4.4.3 源码精读

- [PrefixCacheStats 全文：lmdeploy/pytorch/paging/block_trie.py:86-101](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_trie.py#L86-L101) —— 两个计数 + `reset/copy/hit_rate`。
- [快照与恢复：lmdeploy/pytorch/paging/block_trie.py:210-221](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_trie.py#L210-L221) —— 配合调度器的试探性匹配。
- [指标暴露到调度器：lmdeploy/pytorch/paging/scheduler.py:914-923](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L914-L923) —— `ScheduleMetrics(prefix_cache_hit_rate=self.block_trie.hit_rate(), ...)`。

#### 4.4.4 代码实践：阅读统计的字段含义

**实践目标**：理解命中统计在「试探—回滚」中的正确性。

**操作步骤**：

1. 读 [block_trie.py:223-228](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_trie.py#L223-L228) 的 `_record_match_stats`，确认它在 `suppress_match_stats` 为真时直接 return；
2. 读 [scheduler.py:200-227](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L200-L227) 的 `_try_prefix_match_for_prefill_gate`，看它如何「先 snapshot、再 match、不接受就 restore」。

**预期结果**：你能解释「为什么一次失败的试探匹配不会污染命中率」——因为统计要么被 `restore_stats` 撤回，要么自始至终被 `suppress_match_stats` 抑制。

#### 4.4.5 小练习与答案

**练习 1**：`hit_rate()` 在 `num_query_tokens <= 0` 时返回 0.0，为什么？

> **答案**：避免除零。当一个请求完全没有可匹配的 token（例如全是不足一块的尾巴，或前缀缓存未启用），`num_query_tokens` 可能为 0，此时命中率定义为 0 而非「未定义」更安全。

**练习 2**：为什么 recompute 驱逐路径要把 `suppress_match_stats` 设为 True？

> **答案**：recompute 驱逐会把被打断的序列重排，重排时它仍可能命中自己之前缓存的前缀。但这是「系统自我恢复」产生的命中，不代表用户请求因前缀缓存而受益；把它计入会虚高命中率，误导运维判断缓存收益，所以统计要抑制。

### 4.5 三个关键机制的深入理解

#### 4.5.1 引用计数让共享安全

`BlockTrie` 本身不直接管理物理显存，它只通过 `allocator`（即块管理器的 `LogicalAllocator`）调用三个原子操作：`add_ref_count`、`update_access_time`、`free`。三者都在 [base_block_manager.py:112-161](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_manager/base_block_manager.py#L112-L161)：

```python
# base_block_manager.py:150-152
def add_ref_count(self, blocks, value):
    np.add.at(self._log_mem.ref_count, blocks, value)   # 原子增减引用

# base_block_manager.py:158-161
def update_access_time(self, blocks):
    now = ...                      # 当前时间戳
    self._log_mem.access_time[blocks] = now   # 刷新 LRU 时间
```

`free` 的关键逻辑：先 `add_ref_count(-1)`，**只有 ref 归零的块才真正归还物理显存**（[base_block_manager.py:112-136](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_manager/base_block_manager.py#L112-L136)）：

```python
# base_block_manager.py:112-126
def free(self, blocks):
    self.add_ref_count(blocks, -1)
    self.update_access_time(blocks)
    ref_count = self.get_ref_count(blocks)
    freed_blocks = blocks[ref_count == 0]   # 仅真正归还无人引用的块
    ...
```

所以「共享块」对每个持有者都是 +1，释放时 -1，直到最后一个持有者离开才回收。这是 `match` 给命中块 +1、`allocate` 给新块 +1、驱逐只挑 `ref_count==1` 的叶子的共同基础。

#### 4.5.2 LRU 驱逐的「叶子优先」

驱逐策略本质是 **LRU over leaves**：

- 只删叶子（无孩子节点），删了不会断掉别人的前缀路径；
- 在叶子里按 `access_time` 最旧的先删；
- 删掉叶子后父节点可能变叶子，递补进候选堆。

`access_time` 在每次 `match`（命中复用）和 `free` 时刷新，所以「最近被复用过的前缀」更不容易被踢——这正是前缀缓存对热点 system prompt 友好的原因。

#### 4.5.3 多模态前缀的安全裁剪

`match` 命中后并非照单全收：若命中的位置落在某个多模态 span（图像 token）内部，会导致 forward 从图像中间启动而出错。因此 `match` 末尾用 `clamp_prefix_cache_match_step` 把命中点**裁回**到多模态 span 的安全边界（[block_trie.py:1115-1136](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_trie.py#L1115-L1136)）。这部分 u9-l1 已铺垫「多模态前缀缓存依赖 content_hash 且要求新式 preprocess」。

### 4.6 完整生命周期串联

把前面几节拼起来，一条序列从前缀缓存角度看是这样的：

```
序列进入 _schedule_prefill
   │
   ├─ 1. snapshot_stats()           # 存档统计，防回滚污染
   ├─ 2. block_trie.match(seq)      # 逐块走 trie，命中块 +1 引用、推进 step
   │      └─ 若资源不足 → _rollback_unscheduled_prefix_match + restore_stats
   ├─ 3. block_manager.allocate()   # 给未命中尾部补新块
   ├─ 4. block_trie.allocate(seq)   # 新满块挂回 trie；重复前缀则复用 trie 块
   └─ 5. _finish_prefix_cache_schedule(seq)  # 计算 seq.cached_tokens 上报

序列每步 decode
   └─ block_trie.allocate(seq)      # 新生成的满 decode 块也挂进 trie

显存不足
   └─ block_trie.evict(num_req)     # LRU 踢 ref_count==1 的叶子

序列结束 / 被驱逐
   └─ block_manager.free(seq)       # 引用 -1，归零才真正回收
```

### 4.7 关于 SSM 检查点（选读）

`block_trie.py` 有近三分之二的代码处理 SSM（Mamba 等状态空间模型）。这类模型除 KV 外还有「循环状态（recurrent state）」，**仅复用 KV 不够安全**——必须同时找到对应步的冻结状态快照才能命中。因此 `BlockTrie` 为这类模型走另一条 `match` 分支 `_match_state_checkpoint`（[block_trie.py:989-1062](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_trie.py#L989-L1062)），用稀疏检查点索引 + 全祖先链复核来保证一致。`Node` 上的 `state_idx / state_ready / state_ref_count` 字段、`PrefixCacheState` 上的 `restore_*` / `save_*` 字段都是为它服务的。这一路线由 `requires_state_checkpoint`（[block_trie.py:195](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_trie.py#L195)）开关，普通 LLM 不会触发，可暂不深究。

## 5. 综合实践

**综合任务**：用「源码阅读 + 推理验证」两条线，把本讲的知识串起来。

**源码线**（必做，无需 GPU）：

1. 打开 [block_trie.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_trie.py)，画出一张完整时序图：调度器 `_schedule_prefill` 中 `match → 驱逐检查 → block_manager.allocate → block_trie.allocate` 四步，标注每一步对 `ref_count` 与 `access_time` 的修改（读 [scheduler.py:649-721](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L649-L721) 与 [base_block_manager.py:112-161](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_manager/base_block_manager.py#L112-L161)）。
2. 回答：一次成功的命中，被复用块的 `ref_count` 从几变成几？`access_time` 是否被刷新？被复用块会不会进入 `leaves` 候选集？为什么？

**推理线**（有 GPU 时做，否则跳过并标注「待本地验证」）：

3. 用 `PytorchEngineConfig(enable_prefix_caching=True)` 起 pipeline，对同一长 system prompt 连发两个不同 user 问题；
4. 用 `LMDEPLOY_LOG_LEVEL=DEBUG` 抓 `Prefix-cache match` 日志，记录第二次的 `matched_step`；
5. 把 `enable_prefix_caching` 改成 `False` 重做，对比两次第二次请求的 prefill 耗时差异（可用 [lmdeploy/profiler.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/profiler.py) 或日志中的调度耗时）。

**预期结果**：源码线你能说清「命中：ref_count +1（trie 已持 1，现变 2）；access_time 刷新；该块因仍有序列在用，ref_count≠1，不会被驱逐」。推理线第二次请求的 `matched_step` 应为公共前缀的 token 数（按 `block_size` 取整），prefill 耗时显著下降。

## 6. 本讲小结

- 前缀缓存在 Paged Attention 的分块 KV 之上，把「相同前缀的 KV 块」留给后续请求复用，省 prefill 算力、降 TTFT；默认关闭，需 `enable_prefix_caching=True` 显式开启。
- `BlockTrie` 用一棵按「token 块 + 多模态哈希」为键的前缀树组织已缓存的块；`Node` 是一条边，持有块号、token、孩子字典与父指针。
- 三大动作：`match` 逐块下走 trie 复用命中块（+1 引用、推进 step）；`allocate` 把新满块挂回 trie、重复前缀则复用并释放冗余块；`evict` 按 LRU 踢 `ref_count==1` 的叶子。
- 安全性来自引用计数：`free` 只在 `ref_count` 归零时真正回收物理块，所以多序列共享同一前缀块是安全的。
- `PrefixCacheStats` 用 `num_query_tokens/num_hit_tokens` 算命中率；因 `match` 是试探性的，统计配 `snapshot/restore` 支持回滚，recompute 路径还会 `suppress_match_stats` 防虚高。
- SSM（状态空间模型）走另一条 `_match_state_checkpoint` 路径，需要 KV 与循环状态快照同时命中，本讲仅作索引。

## 7. 下一步学习建议

- **向上看调用方**：读 [scheduler.py:488-728](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L488-L728) 的 `_schedule_prefill` 全貌，理解前缀缓存与「批容量、token 预算、长上下文分块」三道准入门槛如何联动（承接 u4-l4）。
- **向下看存储**：读 [base_block_manager.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/block_manager/base_block_manager.py) 的 `LogicalAllocator` 与 `PhysicalAllocator`，理解 `ref_count`、`phy_map`、GPU/CPU 双池与 swap 是如何支撑「引用计数归零才回收」（承接 u4-l5）。
- **向服务层看**：读 `serve/openai/protocol.py` 的 `UsageInfo`，看 `cached_tokens` / 前缀命中数如何回传给 API 客户端（承接 u8-l1）。
- **进阶**：若你关心 Mamba 类模型，再回头精读 `block_trie.py` 的 `_match_state_checkpoint`、`reserve/commit/release_state_checkpoint_*` 一整套 SSM 检查点机制，以及 `EngineLoop._publish_forward_prefix_cache` 的发布时机。
