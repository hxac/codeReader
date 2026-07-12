# 前缀缓存与 Radix Tree

## 1. 本讲目标

上一讲（u10-l1）我们看到了 `ModelObj` 暴露的分页 KV cache 接口，理解了「page + 页表 + 空闲页池」如何像虚拟内存一样管理显存。本讲要回答一个更上层的问题：**当多个请求的前缀相同（例如共用同一段 system prompt）时，能不能把已经算好的 KV 直接复用，而不是每个请求都从头 prefill 一遍？**

答案是「前缀缓存（prefix cache）」。学完本讲，你应当能够：

- 说清楚前缀缓存解决什么问题、为什么底层要选「基数树（radix tree）」这种数据结构。
- 读懂 `PrefixCacheObj` 的抽象接口与 `PrefixCacheMatchedResult` 四个字段，并区分「新增 / 复用 / 分叉」三种命中结果。
- 理解 `PagedRadixTree` 是如何用「分页 + 循环缓冲 + 左孩子右兄弟」存下成千上万条 token 序列的，以及 `MatchPrefix / ForkSequence / Extend / RollBack` 的真实行为。
- 读懂 `InsertSequence` 的「匹配 → 复用 → 分叉」三段决策，知道 sliding window 模式下的额外约束。
- 理解请求结束后 KV 不会立即释放，而是进入「回收 LRU 队列」，并在显存紧张时由 `TryFreeMemory` 按「最旧优先」驱逐。
- 用 Python 侧的 `PagedRadixTree` 包装类跑一个最小实验，亲手观察 fork 与 match 的行为。

## 2. 前置知识

本讲是 **advanced** 层，假定你已经读过：

- **u10-l1 分页 KV 缓存模型接口**：知道 page、`page_size`、`AddNewSequence / ForkSequence / PopNFromKVCache`、`CreateKVCache` 是怎么回事。本讲讲的「前缀缓存」是 KV cache **之上** 的一层逻辑：它决定「这条序列的 KV 要不要留着、能不能给别人复用」，而 KV cache 本身只负责「按 page 物理存放」。
- **u9-l3 请求生命周期与状态机**：知道一个请求有 `internal_id`、会经历 waiting → running → finished，结束后会被引擎「回收」。
- **u9-l2 事件-动作循环**：知道 `Step()` 是引擎心跳，prefill/decode 等都是 Action；本讲会提到 `NewRequestPrefill`、`BatchDecode` 等 Action 是前缀缓存与驱逐的调用点。

几个通俗概念先建立直觉：

- **前缀（prefix）**：一条 token 序列的开头部分。两条序列只要前 k 个 token 相同，它们前 k 个 token 对应的 K、V 就是完全一样的（注意力是因果的，前 k 个 token 的 KV 不依赖后面的 token）。
- **基数树（radix tree）**：一种按公共前缀压缩存储的树。把每条 token 序列按公共前缀挂到树上，相同前缀只存一份——这正是「多个请求共享同一段 KV」所需要的索引结构。
- **写时复制（copy-on-write）**：两条序列共享前缀时，KV 不复制，只多挂一个「名字」；之后某条序列继续往后写，再分叉出新分支。这就是 `ForkSequence` 的本质。
- **LRU（Least Recently Used）**：缓存满了时优先扔掉「最久没用过」的那一条。

## 3. 本讲源码地图

本讲集中在 C++ 引擎侧的四个文件，外加两个 Python 锚点用于动手实验：

| 文件 | 作用 |
| --- | --- |
| [cpp/serve/prefix_cache.h](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/prefix_cache.h) | `PrefixCacheObj` 抽象接口、`PrefixCacheMatchedResult` 结果结构、两个工厂方法。 |
| [cpp/serve/prefix_cache.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/prefix_cache.cc) | 真正的实现 `PrefixCacheImpl`（基于 radix tree）和 `NoPrefixCache`（空实现）。包含匹配决策、回收、LRU 驱逐、lazy commit。 |
| [cpp/serve/radix_tree.h](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/radix_tree.h) | `PagedRadixTreeObj` 抽象接口。 |
| [cpp/serve/radix_tree.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/radix_tree.cc) | 分页基数树的全部实现：`RadixPage`、内存池、`MatchSequence / ForkSequence / ExtendSequence / RollBackSequence / SplitPage / MergePage`。 |
| [cpp/serve/engine_actions/new_request_prefill.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/new_request_prefill.cc) | 前缀缓存的**消费方**：新请求到达时调 `InsertSequence`，根据返回结果决定 fork/reuse/add。 |
| [cpp/serve/engine.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc) | 引擎构造时创建 `PrefixCache`，并把「删除 KV」的回调注入进去。 |
| [python/mlc_llm/serve/radix_tree.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/radix_tree.py) | `PagedRadixTree` 的 Python 包装类，是本讲动手实验的入口。 |
| [tests/python/serve/test_radix_tree.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/tests/python/serve/test_radix_tree.py) | radix tree 的单元测试，提供可复现的行为断言。 |

一句话概括层次关系：**`PrefixCache`（策略层）→ `PagedRadixTree`（索引层）→ KV cache（物理存储层）**。前缀缓存靠 radix tree 找到「谁和我共享前缀」，再指挥 KV cache 做 fork/复用/删除。

## 4. 核心概念与源码讲解

### 4.1 前缀缓存要解决什么：PrefixCache 抽象与匹配结果

#### 4.1.1 概念说明

一次 LLM 推理最贵的部分是 **prefill**：要把整段 prompt 喂进模型、算出每一层每一 token 的 K、V，并存进 KV cache。如果两个请求的开头一模一样（比如都带着同一份长长的 system prompt + few-shot 示例），那它们前缀部分的 KV 是**完全相同**的，重复计算纯属浪费——既浪费算力，也浪费显存带宽（prefill 是计算密集，但批量服务时访存也很可观）。

**前缀缓存**就是把这层浪费省掉：把「已经算过、还留在显存里」的序列登记下来，新请求来了先问一句「我的前缀有没有人算过？」，命中就把现成 KV 接上，只 prefill 剩下的尾巴。

为什么索引结构选 **radix tree（基数树）**？因为我们需要按 **token 序列的前缀** 查找，而基数树天生就是「按公共前缀压缩存储 + 按前缀匹配」的结构。同一棵子树上的所有序列共享从根到该子树的前缀，找「最长公共前缀」就是一次自根向下的路径匹配。

#### 4.1.2 核心流程：匹配结果的三种命中

新请求到达时，引擎调 `InsertSequence` 把它的 prompt tokens 喂进前缀缓存，拿到一个 `PrefixCacheMatchedResult`。这个结果结构有四个字段，恰好对应「该怎么复用」的三种决策：

[cpp/serve/prefix_cache.h:L38-L56](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/prefix_cache.h#L38-L56) 定义了这四个字段：

- `prefilled_offset`：命中的前缀长度（即「这么多 token 的 KV 已经现成了」），是核心信息。
- `forked_seq_id`：要**从哪条活跃序列分叉**（copy-on-write）。活跃序列还在被别的请求用，只能 fork 不能动。
- `reused_seq_id`：要**直接接管哪条已回收序列**（连 KV 带名字一起拿走）。
- `reused_seq_pop_last_tokens`：接管的那条回收序列可能比新请求长，多出来的尾巴要 `PopN` 掉。

三种命中（互斥）：

| 场景 | `prefilled_offset` | `forked_seq_id` | `reused_seq_id` | 引擎动作 |
| --- | --- | --- | --- | --- |
| 完全没命中 | 0 | -1 | -1 | 新建空序列，全量 prefill |
| 命中活跃序列前缀 | >0 | 具体序号 | -1 | `ForkSequence`（写时复制）|
| 命中回收序列前缀 | >0 | -1 | 具体序号 | 接管该序列，必要时 `PopN` 截尾 |

#### 4.1.3 源码精读：抽象接口与两种实现

`PrefixCacheObj` 是一个纯虚接口（策略模式的抽象基类），定义了前缀缓存对外的全部能力：

[cpp/serve/prefix_cache.h:L69-L126](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/prefix_cache.h#L69-L126) 列出了核心方法，按生命周期可分四组：

- **写入**：`InsertSequence`（新请求登记）、`ExtendSequence` + `CommitSequenceExtention`（生成出新 token 后追加）、`RollBackSequence`（撤销末尾若干 token，用于推测解码拒收）。
- **回收/驱逐**：`RecycleSequence`（请求结束，序列进回收池）、`TryFreeMemory`（显存紧张，驱逐最旧回收序列）。
- **查询**：`HasSequence`、`Mode`、`Reset`。

接口有两个实现，构成策略模式（见 [cpp/serve/prefix_cache.cc:L345-L426](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/prefix_cache.cc#L345-L426) 的 `NoPrefixCache` 与 [L22-L43](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/prefix_cache.cc#L22-L43) 的 `PrefixCacheImpl`）：

- `NoPrefixCache`：`InsertSequence` 永远返回「全 miss」（`{0, -1, -1, 0}`），其余方法要么空操作要么直接 `LOG(FATAL)`。对应配置 `prefix_cache_mode = "disable"`。
- `PrefixCacheImpl`：真正基于 `PagedRadixTree` 的实现，对应 `"radix"`（默认）。

两种模式由引擎在构造时按配置选择，工厂方法在 [cpp/serve/prefix_cache.cc:L428-L438](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/prefix_cache.cc#L428-L438)。注意 `CreateRadixPrefixCache` 接受一个 `remove_callback` 回调——**当某条序列被真正删除时，前缀缓存通过它通知 KV cache 同步释放物理 page**。这个回调在引擎里的注入见 [cpp/serve/engine.cc:L436-L444](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L436-L444)：

```cpp
if (engine_config->prefix_cache_mode == PrefixCacheMode::kRadix) {
  n->estate_->prefix_cache = PrefixCache::CreateRadixPrefixCache(
      static_cast<size_t>(engine_config->prefix_cache_max_num_recycling_seqs),
      [engine_ptr = n.get()](int64_t seq_id) {
        RemoveRequestFromModel(engine_ptr->estate_, seq_id, engine_ptr->models_);  // 删 KV
        engine_ptr->estate_->id_manager.RecycleId(seq_id);                          // 回收序号
      });
} else if (engine_config->prefix_cache_mode == PrefixCacheMode::kDisable) {
  n->estate_->prefix_cache = PrefixCache::CreateNoPrefixCache();
}
```

> 关键认知：**前缀缓存只是「索引 + 调度」，物理 KV 由回调联动释放**。`PrefixCacheImpl` 自己不持有任何 GPU 张量，它只维护 token 序列的拓扑（谁和谁共享前缀）；真正删显存的是 `RemoveRequestFromModel`。

#### 4.1.4 代码实践：把接口当 API 表来读

1. **实践目标**：建立「`PrefixCacheObj` 是一张方法表，两个实现是两种策略」的直觉。
2. **操作步骤**：
   - 打开 `cpp/serve/prefix_cache.h`，把 `InsertSequence / RecycleSequence / TryFreeMemory / ExtendSequence / CommitSequenceExtention / RollBackSequence` 这六个方法在 `PrefixCacheImpl`（prefix_cache.cc）和 `NoPrefixCache`（同文件末尾）里各找到一遍。
   - 数一数：`NoPrefixCache` 里有多少方法体是「空 / 直接 FATAL」。
3. **需要观察的现象**：`NoPrefixCache` 的 `InsertSequence` 永远返回 `{0, -1, -1, 0}`，意味着禁用前缀缓存时，每个请求都走「新建空序列 + 全量 prefill」，与你关闭前缀缓存后 TTFT（首 token 延迟）变长的体感一致。
4. **预期结果**：你能用一句话说出「开/关前缀缓存只换一个 `PrefixCacheObj` 实现，引擎其余代码不变」。

### 4.2 PagedRadixTree：用分页基数树存 token 路径

#### 4.2.1 概念说明

普通基数树的节点「前缀长度」可变，但这样每个节点的内存大小不一致，频繁 new/delete 开销大。MLC 用的是**分页基数树（paged radix tree）**：

- 每个节点（叫 `RadixPage`）能存**固定数量**（`kPageCapacity_ = 64`）个 token，固定大小，统一从一个内存池领用。
- 因为词表可能很大，一个节点可能有非常多孩子，所以用**「左孩子 + 右兄弟」（left-child right-sibling）**表示成二叉树，省去孩子指针数组。
- page 内部用**循环缓冲（circular buffer）**存 token，便于从头/尾增删（推测解码会频繁 rollback 末尾）。
- 每个 page 上挂一个**序列 ID 链表** `seq_ids`：记录「哪些序列恰好结束在本 page」。一条序列的逻辑位置就是「从根到它所在 page 的路径，拼上各 page 里的 token」。

为什么 page 内存布局刻意设计成 `int32_t` 数组？因为 token 本身就是 `int32_t`，把指针、元信息和 token 放进同一个连续 `int32_t` 数组，能减少内存碎片、提高缓存命中率（见 `RadixPage::kDataOffset` 的注释）。

#### 4.2.2 核心流程：匹配、分叉、扩展、回滚

四条核心操作的真实行为（务必区分「逻辑序列」和「物理 page」）：

- **MatchPrefix（查找最长公共前缀）**：从 root 出发，沿「孩子首 token = 当前 token」逐层下钻，每个 page 内部再逐 token 比较，直到第一个不匹配的位置。返回 `(matched_offset, 所有共享此前缀的序列 id)`。
- **ForkSequence（分叉，写时复制）**：不拷贝任何 token！只在 fork 位置对应的 page 上**多挂一个 seq_id**。若 fork 点落在某 page 中间，先把该 page `SplitPage` 切成两半，再在新挂的 seq_id。
- **ExtendSequence（扩展）**：先看末尾 page 还能不能塞（有空位且是叶子就原地 Extend），塞不下就 `Allocate` 新 page 接上。
- **RollBackSequence（回滚末尾 N 个）**：从末尾 page 往前回收，空了且无孩子的 page 直接 Free 回池；若停在 page 中间则 `SplitPage` 后截断。

所有这些操作的物理后果只有两类：**挂/摘 seq_id**（fork 复用）和 **Allocate/Free page**（增删节点）。**永远不会逐 token 拷贝数据**——这是 fork 能 O(1) 的根本原因，也是上一讲 `ForkSequence` 不占额外 KV 预算的索引层基础。

#### 4.2.3 源码精读：RadixPage 与两个内存池

先看 `RadixPage` 的字段与循环缓冲访问：

[cpp/serve/radix_tree.cc:L137-L164](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/radix_tree.cc#L137-L164) 是 page 的结构定义与 `operator[]`：

```cpp
struct RadixPage {
  RadixPage* parent;
  RadixPage* first_child;     // 左孩子
  RadixPage* next_sibling;    // 右兄弟
  SequenceIDNode* seq_ids;    // 本 page 上结束的序列 id 链表
  size_t capacity;            // kPageCapacity_ = 64
  size_t offset;              // 循环缓冲起点
  size_t length;              // 已存 token 数
  // operator[] 用 (i + offset) % capacity 实现循环缓冲
  int32_t& operator[](size_t i) {
    return reinterpret_cast<int32_t*>(this)[kDataOffset + (i + offset) % capacity];
  }
};
```

注意 `reinterpret_cast<int32_t*>(this)[kDataOffset + ...]`：它把整个 page 当成一个 `int32_t` 数组，跳过头部元信息（`kDataOffset` 个 int32 槽），后面就是 token 数据。这就是「page 内存布局就是 int 数组」的实现。

page 内的逐 token 前缀比较在 [cpp/serve/radix_tree.cc:L341-L347](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/radix_tree.cc#L341-L347)（`RadixPage::MatchPrefix`）：取 `min(length, prefix_length)` 逐位比，返回首个不匹配位置或整段匹配。

再看两个对象池。`RadixPagePool` 和 `SequenceIDNodePool` 都用「成块申请 + 空闲下标栈」避免频繁 malloc，容量查询在 [cpp/serve/radix_tree.cc:L396-L400](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/radix_tree.cc#L396-L400)：

```cpp
size_t FreeCapacity() { return free_page_indices_.size() * kPageCapacity_; }
```

即「空闲 page 数 × 64」= 还能再存多少 token。

最关键的是 `PagedRadixTreeImpl` 自身的状态——一张「序列 id → 它结束在哪个 page」的表：

[cpp/serve/radix_tree.cc:L460-L477](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/radix_tree.cc#L460-L477)，核心字段是 `seq2page`（`unordered_map<int32_t, RadixPage*>`）。有了它，任何对序列的操作都能 O(1) 定位到「序列尾巴所在的 page」，再沿 `parent` 往上走就是整条序列。

`MatchPrefix` 的对外实现 [cpp/serve/radix_tree.cc:L514-L520](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/radix_tree.cc#L514-L520) 把内部 `MatchSequence` 的结果（停在哪个 page、匹配多长）翻译成「匹配长度 + 该 page 子树里所有序列」：

```cpp
std::pair<size_t, std::vector<int64_t>> MatchPrefix(const std::vector<int32_t>& tokens) {
  const int32_t* prefix = tokens.data();
  size_t length = tokens.size();
  auto [page, offset, in_page_offset] = MatchSequence(root, prefix, length);
  if (!offset) return std::make_pair(0, std::vector<int64_t>());
  return std::make_pair(offset, page->FindAllChildSequence());  // 子树里所有序列都共享此前缀
}
```

`MatchSequence`（[L779-L798](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/radix_tree.cc#L779-L798)）是自根向下的逐层匹配；`FindAllChildSequence`（[L246-L256](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/radix_tree.cc#L246-L256)）用回调遍历子树收集所有 seq_id——因为这些序列都以本 page 为前缀，自然都是「可复用」的候选。

`ForkSequence` 的写时复制精髓在 [cpp/serve/radix_tree.cc:L547-L565](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/radix_tree.cc#L547-L565)：

```cpp
void ForkSequence(int64_t seq_id, int64_t parent_seq_id, size_t forked_offset) {
  // ... 校验 ...
  size_t length = GetSequenceLength(parent_seq_id);
  for (RadixPage* page = seq2page[parent_seq_id]; page; page = page->parent) {
    if (forked_offset > length - page->length) {
      if (forked_offset < length) {
        page = SplitPage(page, forked_offset + page->length - length);  // 落在 page 中间才切
      }
      page->AddSequence(seq_id_node_pool, seq_id);  // 仅挂一个 seq_id，不拷贝 token！
      seq2page[seq_id] = page;
      return;
    }
    length -= page->length;
  }
}
```

整条 fork 路径上**没有任何 token 被复制**，唯一的写操作是 `AddSequence`——往 page 的链表头插一个 `SequenceIDNode`（[L185](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/radix_tree.cc#L185)）。这就是「fork 不额外占 KV 预算」在索引层的实现：fork 出的新序列和父序列共享同一批 page。

`SplitPage` / `MergePage`（[L743-L771](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/radix_tree.cc#L743-L771) 与 [L717-L733](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/radix_tree.cc#L717-L733))是「在 page 边界切开/合并」的工具，保证「序列总是结束在 page 边界」这一不变量（见 `RadixPage` 结构体上方注释 L134-L136）。

#### 4.2.4 代码实践：用 Python 包装类亲手玩 radix tree

前缀缓存的索引层 `PagedRadixTree` 有完整的 Python 包装 [python/mlc_llm/serve/radix_tree.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/radix_tree.py)，方法名就是 FFI 函数名（注册在 radix_tree.cc 末尾 `TVM_FFI_STATIC_INIT_BLOCK`）。我们可以直接用它做实验。

1. **实践目标**：亲眼看到「fork 不拷贝、match 返回最长公共前缀」。
2. **操作步骤**：在装好 `mlc_llm` 的环境里运行（复刻自 [tests/python/serve/test_radix_tree.py:L90-L99](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/tests/python/serve/test_radix_tree.py#L90-L99) 的 `test_fork_2`）：

   ```python
   from mlc_llm.serve import PagedRadixTree

   prt = PagedRadixTree()
   prt.add(0)
   prt.extend(0, [0, 1, 2, 3])   # 序列 0 = [0,1,2,3]
   prt.fork(1, 0, 3)             # 序列 1 从序列 0 的前 3 个 token 分叉
   prt.extend(1, [4])            # 序列 1 = [0,1,2,4]
   prt.fork(2, 0, 3)             # 序列 2 从序列 0 的前 3 个 token 分叉
   prt.extend(2, [5])            # 序列 2 = [0,1,2,5]

   print(list(prt.get(1)))       # [0, 1, 2, 4]
   print(prt.match([0, 1, 2, 4]))  # 期望 (4, (1,))
   print(prt.match([0, 1, 2, 5]))  # 期望 (4, (2,))
   ```
3. **需要观察的现象**：
   - `prt.get(1)` 返回 `[0,1,2,4]`——序列 1 并没有拷贝 `[0,1,2]`，而是通过 fork 共享了序列 0 的前 3 个 token 所在的 page，再把 `4` 追加到新 page。
   - `match([0,1,2,4])` 返回 `(4, (1,))`：匹配长度 4（整个前缀都在树里），命中的序列是 1。换成 `[0,1,2,5]` 则命中序列 2。这正是 `InsertSequence` 找「最长公共前缀」的底层调用。
4. **预期结果**：输出与注释一致。若 `import mlc_llm` 失败，说明 C++ 侧 `libmlc_llm.so` 未正确加载（回顾 u1-l3 的安装验证），需先解决安装。
5. 若本地无法运行，明确标注「待本地验证」，但 `test_radix_tree.py` 是官方测试，可直接 `pytest tests/python/serve/test_radix_tree.py` 观察通过情况。

#### 4.2.5 小练习与答案

**练习 1**：`RadixPage::operator[]` 为什么要用 `(i + offset) % capacity` 而不是直接 `[i]`？
**答**：因为 page 内部是**循环缓冲**。`offset` 是数据起点，当从头或尾增删 token 时只移动 `offset`/`length` 而不搬移数据，`(i + offset) % capacity` 让逻辑下标 i 映射到物理槽位，使头尾增删都是 O(1)——这对推测解码频繁 rollback 末尾很关键。

**练习 2**：`ForkSequence` 校验 `forked_offset > 0` 且 `forked_offset <= length`（[L550-L552](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/radix_tree.cc#L550-L552)），为什么不允许 `forked_offset == 0`？
**答**：fork 表示「从父序列的某个位置开始共享」，位置 0 意味着什么都不共享，没有意义；空序列应当用 `AddSequence` 在 root 创建，而不是 fork。

### 4.3 InsertSequence：匹配 → 复用 → 分叉三段决策

#### 4.3.1 概念说明

`PagedRadixTree` 只提供「找前缀 + 改树」的原子能力，而**「命中后到底走哪条路」**的策略在上层 `PrefixCacheImpl::InsertSequence` 里。它要权衡三件事：

1. **能否直接接管一条已回收序列？** 这是最省的——连 KV 都不用 fork，直接拿来用。但前提是该回收序列和新请求「足够像」。
2. **不行的话，能否从一条活跃序列 fork？** fork 是 copy-on-write，活跃序列还在被别人用，只能挂名不能动。
3. **都不行，老老实实新建空序列、全量 prefill。**

还有一个工程要点：`InsertSequence` 收到的是**完整 prompt tokens**，但它会在匹配前 `pop_back()` 掉最后一个 token（见 [prefix_cache.cc:L57](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/prefix_cache.cc#L57)）。这样匹配出来的前缀永远不含最后一个 token，**保证至少有一个 token 要被 prefill**——模型必须有至少一个 token 流过前向才能产出下一个 token 的 logits，避免「整条 prompt 全部命中、无 token 可算」的退化。

#### 4.3.2 核心流程：三段决策

```
InsertSequence(seq_id, prompt_tokens):
  1) CommitSequenceExtention()          # 先把之前 lazy 的 extend 落树
     tokens = prompt_tokens[:-1]        # 去掉最后一个 token
     (matched_offset, matched_seqs) = radix_tree_.MatchPrefix(tokens)

  2) 若 matched_offset == 0:            # 完全没命中
        AddSequence(seq_id); return {0,-1,-1,0}

  3) 在 matched_seqs 里挑「最佳」:
     若开启 sliding window:
        只能精确匹配（长度相等）的回收序列 → 直接接管
     否则（无 sliding window）:
        a. 贪心找「最短」回收序列，且 matched_offset > 0.9 * shortest_len
           → 接管它，必要时 RollBackSequence 截尾
        b. 否则在 matched_seqs 里找 fork_offset 最大的活跃序列 → ForkSequence
        c. 否则 fallback 到新建空序列
```

第 3 步里有一个关键阈值 `0.9`：只有当新请求的前缀长度达到候选回收序列的 90% 以上，才值得「接管 + 截尾」，否则截掉太多尾巴不划算，不如直接 fork。

#### 4.3.3 源码精读

整体实现在 [cpp/serve/prefix_cache.cc:L49-L142](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/prefix_cache.cc#L49-L142)。先看「完全没命中」与匹配前置：

[cpp/serve/prefix_cache.cc:L56-L66](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/prefix_cache.cc#L56-L66)：先 `pop_back` 再 `MatchPrefix`，没有命中就 `AddSequence` 并返回全 miss。

非 sliding window 分支里「贪心接管最短回收序列」的核心判断在 [cpp/serve/prefix_cache.cc:L102-L112](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/prefix_cache.cc#L102-L112)：

```cpp
if (shortest_recycling_seq_id != -1 && matched_offset > shortest_recycling_seq_length * 0.9) {
  ReuseRecyclingSequence(shortest_recycling_seq_id);
  if (shortest_recycling_seq_length > matched_offset) {
    // 回收序列更长，回滚多余的尾巴以匹配新序列
    radix_tree_->RollBackSequence(shortest_recycling_seq_id,
                                  shortest_recycling_seq_length - matched_offset);
  }
  return PrefixCacheMatchedResult{matched_offset, -1, shortest_recycling_seq_id,
                                  shortest_recycling_seq_length - matched_offset};
}
```

注意它选「最短」回收序列的原因（注释 [L86-L88](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/prefix_cache.cc#L86-L88)）：截掉的尾巴越短，浪费越少。

若不满足接管条件，则 fork 一条**无 sliding window** 的匹配序列（[L113-L135](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/prefix_cache.cc#L113-L135)）。注意 fork 分支会跳过带 sliding window 的序列（[L120-L122](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/prefix_cache.cc#L120-L122)），注释说明这是当前分页 KV cache 实现的限制。

> **sliding window 的特殊约束**（[L72-L84](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/prefix_cache.cc#L72-L84)）：开启滑动窗口注意力时，回收序列的复用被限制为「长度精确相等」，且不允许 rollback 截尾。因为滑动窗口下 KV 的有效范围会随序列推进而变化，截尾会破坏窗口语义。

#### 4.3.4 消费方：new_request_prefill 如何用匹配结果

`InsertSequence` 只返回「该怎么办」的描述，真正动手在 KV cache 上的是 `NewRequestPrefill` 这个 Action。[cpp/serve/engine_actions/new_request_prefill.cc:L297-L340](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/new_request_prefill.cc#L297-L340) 三分支与上面的结果一一对应：

```cpp
PrefixCacheMatchedResult result = estate->prefix_cache->InsertSequence(
    rsentry->mstates[0]->internal_id, tokens, /*sliding_window...*/, /*sink...*/);

if (result.prefilled_offset == 0) {
  // 全 miss：每个模型新建空序列
  for (Model model : models_) model->AddNewSequence(rsentry->mstates[0]->internal_id);
} else if (result.forked_seq_id != -1) {
  // fork：在 KV cache 侧也做 copy-on-write fork
  for (Model model : models_)
    model->ForkSequence(result.forked_seq_id, rsentry->mstates[0]->internal_id,
                        result.prefilled_offset);
} else {
  // reuse：接管回收序列的 id，必要时 PopN 截尾
  estate->id_manager.RecycleId(rsentry->mstates[0]->internal_id);
  rsentry->mstates[i]->internal_id = result.reused_seq_id;
  if (result.reused_seq_pop_last_tokens > 0)
    model->PopNFromKVCache(rsentry->mstates[0]->internal_id, result.reused_seq_pop_last_tokens);
}
// 把命中的前缀从「待 prefill 输入」里弹掉，只 prefill 剩余部分
if (result.prefilled_offset)
  PopPrefillInputData(rsentry->mstates[i], result.prefilled_offset);
```

> 注意「两层 fork」的对应：radix tree 的 `ForkSequence`（挂 seq_id，索引层）和 `Model::ForkSequence`（KV cache 页表层的 copy-on-write，物理层）。两者一起完成「新请求无缝复用前缀 KV」，这正是上一讲 u10-l1 讲过的 `ForkSequence` 的真正调用场景。

#### 4.3.5 小练习与答案

**练习 1**：为什么接管回收序列时要选「最短」而不是「最长」？
**答**：因为接管后若回收序列比新请求长，多出的尾巴要 `RollBackSequence` + KV cache `PopN` 截掉。选最短的候选，截掉的尾巴最少，浪费的计算和显存最小（见 [L86-L88](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/prefix_cache.cc#L86-L88) 注释）。

**练习 2**：`matched_offset` 是 5，但 `forked_seq_id == -1` 且 `reused_seq_id != -1`，引擎会在 KV cache 上做什么？
**答**：把新请求的 `internal_id` 改成 `reused_seq_id`（接管），然后因为已有 5 个 token 的前缀，从待 prefill 输入里弹掉这 5 个 token，只 prefill 剩余部分；若 `reused_seq_pop_last_tokens > 0` 还要 `PopNFromKVCache` 截掉回收序列多出来的尾巴（见 [new_request_prefill.cc:L326-L346](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/new_request_prefill.cc#L326-L346)）。

### 4.4 回收、LRU 驱逐与 lazy commit

#### 4.4.1 概念说明

请求生成完最后一个 token 后，它的 KV cache **不立即释放**。因为下个请求很可能和它共享前缀（同一 system prompt、同一 few-shot），留着就能被复用。但显存有限，不能无限留，于是有三个机制配合：

- **回收（Recycle）**：请求结束，序列从 `kActive` 转 `kRecycling`，挂进一个 LRU 队列等着被复用或驱逐。
- **LRU 驱逐（TryFreeMemory）**：当 prefill/decode 发现显存不够，先尝试驱逐「最久未命中」的回收序列，把它的 KV 还回去；还不够再考虑抢占（preempt）运行中的请求。
- **lazy commit**：decode/draft 每个 step 生成的新 token 通过 `ExtendSequence` 暂存，等本 step 结束统一 `CommitSequenceExtention` 真正写进 radix tree，避免半截状态污染索引。

#### 4.4.2 核心流程

**回收的两态机**（[prefix_cache.cc:L281-L292](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/prefix_cache.cc#L281-L292)）：

- `kActive`：正在服务某请求，只能被 fork（不能被接管或删除）。
- `kRecycling`：请求已结束，可被 fork **或** 被 reuse（被 reuse 时转回 `kActive`）。

```
请求结束 RecycleSequence(lazy=true):
  state: kActive → kRecycling
  挂进 LRU（lru_counter 递增）
  若回收队列已满 → 先 TryFreeMemory() 腾一个位置

显存紧张 TryFreeMemory():
  取 LRU 时间戳最小的（最旧）回收序列
  radix_tree_->RemoveSequence(seq_id)         # 从索引树摘除
  remove_callback_(seq_id)                    # 触发 KV cache 释放 page + 回收 id
  state/seq2page/sliding_window 全部擦除
  返回 true（成功腾出空间）；若无回收序列可删，返回 false
```

#### 4.4.3 源码精读

**RecycleSequence** 在 [cpp/serve/prefix_cache.cc:L191-L216](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/prefix_cache.cc#L191-L216)。它有一个 `lazy` 开关，对应两种语义：

```cpp
void RecycleSequence(int64_t seq_id, bool lazy = true) final {
  // ...校验状态为 kActive...
  if (lazy && max_num_recycling_seqs_ != 0) {
    if (recycling_seq_lrus_.size() == max_num_recycling_seqs_) {
      TVM_FFI_ICHECK(TryFreeMemory());  // 满了就先驱逐一个最旧的
    }
    seq_states_.at(seq_id) = SequenceState::kRecycling;
    ++lru_counter_;
    recycling_seq_lrus_.emplace(seq_id, lru_counter_);            // seq → 时间戳
    reversed_recycling_seq_lrus_.emplace(lru_counter_, seq_id);   // 时间戳 → seq
  } else {
    // 立即删除：直接从树摘除并触发回调释放 KV
    radix_tree_->RemoveSequence(seq_id);
    if (remove_callback_ != nullptr) remove_callback_(seq_id);
    // ...擦除状态...
  }
}
```

两个哈希表互为反查：`recycling_seq_lrus_`（seq→时间戳）和 `reversed_recycling_seq_lrus_`（时间戳→seq），这样既能 O(1) 查某序列的时间戳，又能 O(1) 找「时间戳最小的最旧序列」。

**TryFreeMemory** 在 [cpp/serve/prefix_cache.cc:L225-L243](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/prefix_cache.cc#L225-L243)。注意它如何取「最旧」：

```cpp
bool TryFreeMemory() final {
  if (reversed_recycling_seq_lrus_.empty()) return false;  // 没有回收序列可删
  auto [lru, seq_id] = *reversed_recycling_seq_lrus_.begin();  // 最小时间戳 = 最旧
  // ...校验状态为 kRecycling...
  radix_tree_->RemoveSequence(seq_id);
  if (remove_callback_ != nullptr) remove_callback_(seq_id);  // 联动 KV cache
  // ...擦除 seq_states / recycling_seq_lrus_ / reversed... / sliding_window_infos_...
  return true;
}
```

> 注意：`begin()` 取的是**最小时间戳**的元素。因为 `lru_counter_` 单调递增，最小时间戳就是「最早进入回收队列、最久没被复用」的序列——这就是 LRU 的实现。被 `ReuseRecyclingSequence` 接管时它会被从队列摘除（[L269-L276](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/prefix_cache.cc#L269-L276)），所以留在队列里的总是「未被近期请求命中」的，符合 LRU 语义。

**TryFreeMemory 的两个调用语境**（这是综合实践要追踪的重点）：

1. **prefill 时显存不够**：[cpp/serve/engine_actions/batch_prefill_base.cc:L152-L158](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_prefill_base.cc#L152-L158)。当 `HasPrefillSpace` 返回 false（页不够），先 `TryFreeMemory` 驱逐回收序列；返回 false（没回收序列可删）才 `break` 走抢占。
2. **decode/verify 时显存不够**：例如 [cpp/serve/engine_actions/batch_decode.cc:L55-L58](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_decode.cc#L55-L58)，先 `TryFreeMemory`，不行再 `PreemptLastRunningRequestStateEntry`（回顾 u9-l2 的抢占）。

  即**驱逐回收序列优先于抢占运行请求**——因为回收序列是「已经没人用的死序列」，抢它是无代价的；抢占运行请求要丢草稿、回退队列，代价大得多。

**RecycleSequence 的两个调用语境**：

1. **请求正常结束、非 pinned**：[cpp/serve/engine_actions/action_commons.cc:L165-L167](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L165-L167)，`lazy=true`——进 LRU 等复用。
2. **请求被彻底移除（如 abort）**：[action_commons.cc:L406-L407](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L406-L407)，`lazy=false`——立即删 KV。

> 还有个 `pinned_system_prompt` 开关（[action_commons.cc:L165](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L165)）：若请求被标记为「钉住的系统提示」，结束时不回收，让它的 KV 永久常驻，专门服务「同一段 system prompt 反复用」的场景。

**lazy commit** 机制在 [prefix_cache.cc:L150-L169](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/prefix_cache.cc#L150-L169)。`ExtendSequence` 只把 `(seq_id, tokens)` 压进 `uncommitted_extended_token_ids_` 暂存表，**不立即写树**；要等 `CommitSequenceExtention` 被调用才批量落树。注释（[L332-L338](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/prefix_cache.cc#L332-L338)）解释了原因：暂存的是引用，若不及时 commit，中途若有 `InsertSequence`/`RecycleSequence` 会读到不一致状态。因此每个 Action 执行前/后都会调 `CommitSequenceExtention`（如 `InsertSequence` 第一行 [L56](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/prefix_cache.cc#L56)、`RecycleSequence` 开头 [L192](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/prefix_cache.cc#L192)）。

#### 4.4.4 小练习与答案

**练习 1**：`prefix_cache_max_num_recycling_seqs` 设为 0 会怎样？设为 -1 呢？（提示：`CreateRadixPrefixCache` 的参数是 `size_t`。）
**答**：设为 0 时，`RecycleSequence` 的 `lazy && max_num_recycling_seqs_ != 0` 条件不成立，每次回收都走 `else` 分支立即删除 KV——等于「不留任何回收序列做前缀缓存」（fork 活跃序列仍可用）。设为 -1 时，`-1` 被转成 `size_t` 变成极大值，回收队列「永远不满」，相当于无限容量的前缀缓存（见 [config.h:L288-L290](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/config.h#L288-L290) 注释）。

**练习 2**：为什么 `TryFreeMemory` 一定要在 `PreemptLastRunningRequestStateEntry` 之前被调用？
**答**：驱逐回收序列是「无代价」的（那些序列已经没人用，删了不影响任何在途请求），而抢占运行请求要丢草稿、释放 KV、插回 waiting 队列，代价大。所以引擎永远先榨干回收序列的显存，再考虑抢占（见 batch_decode.cc L55-L58 的 `if (TryFreeMemory) continue;` 循环）。

## 5. 综合实践：追踪一次「命中前缀又被驱逐」的完整生命线

本讲的目标是把「匹配 → 复用 → 回收 → 驱逐」串成一条线。请完成下面两个子任务。

### 任务 A：源码阅读——画出请求生命线上的前缀缓存调用

选取一个共用 system prompt 的两请求场景，沿引擎执行顺序填写下表（在源码里找到对应行号）：

| 时刻 | 调用 | 所在文件:行 | 对 radix tree / KV cache 的后果 |
| --- | --- | --- | --- |
| 请求 R1 到达 | `InsertSequence`（全 miss） | `prefix_cache.cc` L56-L66 | `AddSequence`，KV `AddNewSequence` |
| R1 prefill 第一个新 token | `ExtendSequence` + `CommitSequenceExtention` | `prefix_cache.cc` L150-L169 | 新 token 写进 radix tree |
| 请求 R2 到达（与 R1 共享前缀） | `InsertSequence` → fork | `prefix_cache.cc` L113-L135 + `new_request_prefill.cc` L314-L325 | radix tree `ForkSequence` 挂 seq_id；KV cache `ForkSequence` copy-on-write |
| R2 结束 | `RecycleSequence(lazy=true)` | `action_commons.cc` L165-L167 | R2 进 LRU 队列，KV 保留 |
| R1 结束 | `RecycleSequence(lazy=true)` | 同上 | R1 进 LRU 队列 |
| 新请求 R3 到达且与 R1 前缀高度重合 | `InsertSequence` → reuse | `prefix_cache.cc` L102-L112 | 接管 R1 的 id（`ReuseRecyclingSequence`），R1 从 LRU 出队转 `kActive` |
| 显存紧张，又来一批 prefill | `TryFreeMemory` | `batch_prefill_base.cc` L152-L158 → `prefix_cache.cc` L225-L243 | 删最旧回收序列，`remove_callback` 释放 KV page |

**操作步骤**：
1. 先在 `new_request_prefill.cc` 里定位 `InsertSequence` 调用点（L297），确认它的返回值如何分流到三个分支。
2. 在 `action_commons.cc` 里找到请求结束时 `RecycleSequence` 的两处调用（L167 的 `lazy=true` 与 L407 的 `lazy=false`），说出它们的触发条件差异。
3. 在 `batch_prefill_base.cc` 与 `batch_decode.cc` 里找到 `TryFreeMemory` 的调用循环，确认「驱逐回收序列优先于抢占」的顺序。

**需要观察的现象**：你应当发现「fork 活跃序列」和「reuse 回收序列」是两条不同的省算路径——前者序列还在用所以只挂名，后者序列已死所以连 id 一起接管。

### 任务 B：动手实验——观察 fork 不拷贝、reuse 与 match

运行下面这段脚本（基于 [tests/python/serve/test_radix_tree.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/tests/python/serve/test_radix_tree.py) 的 `test_fork_2` 与 `test_remove`）：

```python
from mlc_llm.serve import PagedRadixTree

prt = PagedRadixTree()
cap0 = prt.free_capacity()

# 1) 建一条「长公共前缀」序列
prt.add(0)
prt.extend(0, [10, 11, 12, 13])

# 2) 两条新序列都从前 3 个 token fork
prt.fork(1, 0, 3); prt.extend(1, [20])   # [10,11,12,20]
prt.fork(2, 0, 3); prt.extend(2, [21])   # [10,11,12,21]

print("free_capacity after forks =", prt.free_capacity())   # fork 不应该吃掉很多容量
print("get(1) =", list(prt.get(1)))                          # [10, 11, 12, 20]
print("match [10,11,12,20] =", prt.match([10, 11, 12, 20]))  # (4, (1,))
print("match [10,11,12,99] =", prt.match([10, 11, 12, 99]))  # (3, (0, 1, 2)) 前3共享

# 3) 模拟 reuse：删掉 1、2 后，前缀 [10,11,12] 仍由序列 0 持有
prt.remove(1); prt.remove(2)
print("match [10,11,12,13] after remove =", prt.match([10, 11, 12, 13]))  # (4, (0,))
print("free_capacity after removes =", prt.free_capacity(), "recovered?", prt.free_capacity() == cap0)
```

**需要观察的现象与预期结果**：
1. fork 两条序列后，`free_capacity` 几乎不变（只多用了存新 token `20/21` 的 page），证明 fork 共享前缀 page、不拷贝。
2. `match([10,11,12,99])` 返回 `(3, (0,1,2))`——前 3 个 token `[10,11,12]` 是序列 0/1/2 的公共前缀，所以三个 seq_id 全部命中；这与 `InsertSequence` 调 `MatchPrefix` 后在 `matched_seqs` 里挑候选的逻辑一致。
3. `remove(1)`、`remove(2)` 后，序列 0 仍持有 `[10,11,12,13]`，所以 `match([10,11,12,13])` 命中 `(4, (0,))`；容量基本回到 `cap0`（叶子 page 被回池）。

> 若环境跑不起来：明确标注「待本地验证」，并改用 `pytest tests/python/serve/test_radix_tree.py -v` 直接观察官方断言（尤其 `test_fork_2`、`test_remove`），它们覆盖了上面所有行为。

## 6. 本讲小结

- 前缀缓存是 KV cache 之上的「索引 + 调度」层，目的是让共享前缀的请求复用已算好的 KV，省掉重复 prefill。底层选 radix tree 是因为「按 token 前缀查找」正是它的强项。
- `PrefixCacheObj` 是策略接口，有两个实现：`PrefixCacheImpl`（radix，默认）和 `NoPrefixCache`（禁用）。它只管 token 拓扑，物理 KV 由构造时注入的 `remove_callback` 联动释放（engine.cc L436-L444）。
- `PagedRadixTree` 用「固定大小 page（容量 64）+ 循环缓冲 + 左孩子右兄弟 + 两个对象池」存序列；`ForkSequence` 是写时复制，只挂 seq_id 不拷 token，这是 fork O(1)、不占 KV 预算的根因。
- `InsertSequence` 走「匹配 → 复用 → 分叉」三段决策：先 `pop_back` 保证至少一个 token 要 prefill；无 sliding window 时按「最短回收序列 + 0.9 阈值」贪心接管，否则 fork 最长匹配活跃序列，再否则新建。
- 请求结束后 KV 不立即释放，而是 `RecycleSequence(lazy=true)` 进 LRU 队列；显存紧张时 `TryFreeMemory` 删「时间戳最小（最旧）」的回收序列并回调释放 KV，且**驱逐回收序列优先于抢占运行请求**。`max_num_recycling_seqs` 为 0 等于不留缓存、为 -1 等于无限容量。
- `ExtendSequence` 是 lazy 的，只入暂存表，由每个 Action 前后的 `CommitSequenceExtention` 批量落树，避免半截状态污染索引。

## 7. 下一步学习建议

- **本讲（u10-l2）的索引与驱逐机制，配合 u10-l1 的分页 KV cache 接口**，已经构成「显存管理」的全貌。建议回头把 u10-l1 的 `ForkSequence / PopNFromKVCache / CommitAcceptedTokenTreeNodesToKVCache` 和本讲的索引层调用并排对照，确认「两层 fork」在你脑里对得上。
- 下一讲 **u10-l3 采样器：CPU 与 GPU** 将离开「显存管理」转向「采样」：preifll/decode 产出 logits 后，如何按 temperature、top-p 选出下一个 token，以及在推测解码里如何校验 draft token。采样器会消费本讲序列「该 commit 哪些 token」的决策结果。
- 之后再进 **u10-l4 推测解码动作链**：那里会频繁用到本讲的 `RollBackSequence`（draft 被拒收时回滚）、`ExtendSequence`（draft 被接受时追加）和 lazy commit，是本讲机制的最大用户。
- 想验证理解：读 [tests/python/serve/test_radix_tree.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/tests/python/serve/test_radix_tree.py) 的 `test_rollback`，结合本讲的 `RollBackSequence` 源码，画出「fork 后再 rollback」时 page 的 SplitPage / Free 路径。
