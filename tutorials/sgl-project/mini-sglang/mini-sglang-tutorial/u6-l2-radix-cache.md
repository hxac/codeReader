# Radix Cache 实现

## 1. 本讲目标

在上一讲（u6-l1）里，我们把 KV cache 拆成了两层：底下是「池存储」`MHAKVCache`（一块裸显存，按 page 切分），上面是「前缀缓存接口」`BasePrefixCache`。当时只看了占位实现 `NaivePrefixCache`——它永远不命中、也永远不淘汰。本讲就来填上真正的实现：`RadixPrefixCache`。

学完本讲，你应该能够：

1. 说清楚一棵**基数树（radix tree）**如何把「很多请求共享的 token 前缀」压缩成一棵树，从而复用 KV。
2. 解释 `RadixTreeNode` 的关键字段（`_key`/`_value`/`children`/`ref_count`/`timestamp`）各自的作用，以及**节点分裂 `split_at`** 为什么是必须的。
3. 复述 `_tree_walk` 的「按 page 对齐匹配」流程，理解为什么一切匹配都要对齐到 `page_size`。
4. 理解 `ref_count` 如何把节点分成「可淘汰 / 受保护」两桶，以及 `evict` 如何用**最小堆按 timestamp 做 LRU 淘汰**。

本讲只讲基数树本身（`python/minisgl/kvcache/radix_cache.py`），不讲调度器侧如何驱动它——那是下一讲 u6-l3（`CacheManager`）的内容。

## 2. 前置知识

阅读本讲前，建议你已经掌握以下概念（u6-l1 已建立）：

- **KV cache 与 page**：推理时每层的 K/V 张量被分页存储，`page_size` 是分配的最小单位（如 1 或 256）。一个 page 对应若干 token 的 KV。
- **`page_table`**：二维表，行是请求，列是序列位置，存的是「该 token 的 KV 落在池里的哪个槽位下标」。
- **前缀复用（prefix caching）**：如果两个请求开头相同（同一个 system prompt、同一组 few-shot 例子），它们前缀部分的 KV 完全一样，第二次就不用重新 prefill，直接复用第一次算好的 KV。
- **`BasePrefixCache` 的六个方法**：`match_prefix`（只读匹配）、`insert_prefix`（写入新前缀）、`evict`（按需淘汰）、`lock_handle`/`size_info`/`check_integrity`。

补充一个本讲要用的数据结构常识：

> **基数树（radix tree / Patricia trie）** 是一棵「压缩过的前缀树」。普通 trie 每个 token 占一层节点，深度等于序列长度，空间浪费严重；基数树把「只有一个孩子的连续路径」合并到一个节点里，让每个节点保存**一段 token 序列**。当两个序列在某处分叉时，才把那个节点**分裂**成前缀节点 + 后缀节点。这样共享前缀只存一份。

Mini-SGLang 的基数树存的不是 token 本身的 KV，而是「token 序列 → 这些 token 的 KV 在池里的槽位下标」这层映射。`_value` 字段就是 `page_table` 里查到的那些下标。

## 3. 本讲源码地图

本讲涉及的文件：

| 文件 | 作用 |
| --- | --- |
| [radix_cache.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/radix_cache.py) | **本讲主角**。定义 `RadixTreeNode`、`RadixCacheHandle`、`RadixPrefixCache`，实现基数树的匹配、插入、加锁、淘汰。 |
| [base.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/base.py) | 定义抽象接口 `BasePrefixCache` / `BaseCacheHandle` / `SizeInfo` / `InsertResult` / `MatchResult`。本讲引用它的接口契约。 |
| [scheduler/cache.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py) | `CacheManager`——唯一真正调用 radix cache 的地方。本讲用它说明「上层怎么用」。 |
| [kernel/radix.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/radix.py) | `fast_compare_key`：比较两段 int 序列、返回首个不同位置的自定义 C++ kernel。 |
| [utils/misc.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/utils/misc.py) | `align_down`：把一个长度向下对齐到 `page_size` 的倍数。 |
| [kvcache/\_\_init\_\_.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/__init__.py) | 工厂 + 注册表，按 `--cache radix` 选出 `RadixPrefixCache`。 |

## 4. 核心概念与源码讲解

### 4.1 RadixTreeNode：基数树的节点

#### 4.1.1 概念说明

整棵基数树由一个个 `RadixTreeNode` 组成。每个节点保存**一段连续的 token 序列**（`_key`）以及**这段 token 对应的 KV 槽位下标**（`_value`）。可以这样理解它代表的语义：

> 「从根走到我这个节点，把沿途所有节点的 `_key` 拼起来，就得到一条完整的 token 前缀；把沿途所有节点的 `_value` 拼起来，就得到这条前缀对应的全部 KV 槽位下标。」

一个节点有这几个关键字段：

- `children: Dict[Any, RadixTreeNode]`：孩子表。**键不是单个 token，而是「孩子 `_key` 的第一个 page」**（由 `key_fn` 计算）。这样每次向下走一步，就是「按 page 跳」。
- `_parent`：指向父亲，用于加锁/淘汰时往上回溯。
- `ref_count`：当前有多少个 handle 正在「锁住」本节点。`0` 表示无人引用、可淘汰；`>0` 表示受保护、不能动。
- `timestamp`：最近一次被访问（match / 命中 / 分裂）的单调纳秒时间戳，是 LRU 淘汰的排序依据。
- `uuid`：全局自增编号，调试用。
- `_length`：`_key` 的长度（== `_value` 的长度）。

#### 4.1.2 核心流程

一个节点的生命周期：

1. `RadixTreeNode(key_fn)`：构造空节点，`ref_count=0`，`timestamp=now`，`_key/_value` 待填。
2. `set_key_value(key, value)`：填入 token 序列与对应下标，断言两者等长，记录 `_length`。
3. `set_parent(parent)`：把自己挂到父亲名下——既设 `_parent`，又往 `parent.children` 里塞一项（键 = 自己 key 的第一个 page）。
4. 匹配时若发现自己只有「前半段」被命中，就 `split_at(pos)`：把本节点拆成「前缀节点（命中部分）」和「后缀节点（未命中部分）」，前缀节点顶替自己在树里的位置，后缀节点变成前缀节点的孩子。
5. 比较 / 排序：`get_match_len` 调 kernel 算公共前缀长度；`__lt__` 按 `timestamp` 比大小（给堆用）。

#### 4.1.3 源码精读

构造与字段定义（注意 `_key/_value/_length` 在构造时只是占位，稍后由 `set_key_value` 填充）：

[radix_cache.py:20-32](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/radix_cache.py#L20-L32) —— 定义节点的 `children / _parent / ref_count / uuid / timestamp`，以及三个「待更新」字段。

[radix_cache.py:34-42](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/radix_cache.py#L34-L42) —— `set_key_value` 断言 key/value 等长并记录长度；`set_parent` 同时维护父指针和孩子表（孩子表键来自 `key_fn(self._key)`，即自己 key 的第一个 page）。

匹配长度交给 C++ kernel，节点本身只做转发：

[radix_cache.py:63-67](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/radix_cache.py#L63-L67) —— `get_match_len` 调 `fast_compare_key`，返回本节点 key 与输入序列的公共前缀长度（首个不同处的下标）。

> 对应的 kernel 包装在 [kernel/radix.py:18-20](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/radix.py#L18-L20)，注释写明「比较两个 1-D int CPU 张量，找第一个不同的位置」。它只在 CPU 上跑，但需要 tvm-ffi JIT 加载编译好的 `radix.cpp`（见 u10-l2）。

**节点分裂**是本节最关键的机制：

[radix_cache.py:69-81](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/radix_cache.py#L69-L81) —— `split_at(pos)` 的逻辑拆解：

1. 新建 `new_node`，吃掉本节点的前 `pos` 个 token（`_key[:pos]` / `_value[:pos]`），并继承本节点的 `ref_count` 与 `timestamp`。
2. `new_node.set_parent(parent)`：让 `new_node` 顶替自己在父亲孩子表里的位置。
3. 本节点缩成后半段（`_key[pos:]` / `_value[pos:]`），再 `set_parent(new_node)`，挂到 `new_node` 名下。
4. 返回 `new_node`（即「命中部分」对应的节点，调用方会刷新它的 timestamp）。

直观图示（假设 `pos=2`，原节点 key 为 `[1,2,3,4]`）：

```
分裂前：  root ──► A[1,2,3,4]
分裂后：  root ──► new[1,2] ──► A[3,4]
```

最后是给堆排序用的比较运算：

[radix_cache.py:83-84](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/radix_cache.py#L83-L84) —— `__lt__` 直接比 `timestamp`，越老（越小）越优先被淘汰，这正是 LRU 的排序键。

#### 4.1.4 代码实践

**实践目标**：亲手构造节点并触发一次 `split_at`，看清分裂前后的父子结构与 key/value 切分。这段代码只用到纯 Python 的张量切片，**不需要 GPU，也不需要 `fast_compare_key` kernel**，可直接运行。

**操作步骤**：

```python
# 示例代码：手动构造节点并分裂（可在任意装了 torch 的 CPU 环境运行）
import torch
from minisgl.kvcache.radix_cache import RadixTreeNode, _get_key_fn

key_fn = _get_key_fn(page_size=2)          # 孩子 key = 前 2 个 token 组成的 tuple

root = RadixTreeNode(key_fn)
node_a = RadixTreeNode(key_fn)
node_a.set_key_value(torch.tensor([1, 2, 3, 4]), torch.tensor([10, 11, 12, 13]))
node_a.set_parent(root)                     # root.children[(1,2)] = node_a

# 在 pos=2 处分裂：命中前 2 个 token
new_node = node_a.split_at(2)
```

**需要观察的现象**：

1. 分裂前 `root.children[(1,2)] is node_a`。
2. 分裂后 `new_node._key` 是 `[1,2]`、`new_node._value` 是 `[10,11]`；`node_a._key` 缩成 `[3,4]`、`node_a._value` 缩成 `[12,13]`。
3. `node_a.parent is new_node` 且 `new_node.parent is root`；`root.children[(1,2)] is new_node`（`new_node` 顶替了 `node_a` 在父亲里的位置）。
4. `new_node.ref_count == node_a.ref_count`（分裂继承引用计数）。

**预期结果**：分裂把一个 4-token 节点变成「2-token 前缀节点 + 2-token 后缀孩子」，树的高度增加一层，但共享前缀 `[1,2]` 现在可以独立匹配/淘汰。

#### 4.1.5 小练习与答案

**练习 1**：`split_at` 为什么要 `new_node.ref_count = self.ref_count`，而不是设成 0？

**参考答案**：分裂发生在一棵「已经有引用关系」的子树上——原本挂在外面的 handle 引用的是「整段」key，分裂后这些 handle 语义上仍然引用「包含命中前缀的路径」，所以继承 `ref_count` 才能保持「谁在保护这段前缀」的计数正确；如果清零，原本受保护的节点会瞬间变成「可淘汰」，可能在使用中被回收。

**练习 2**：`assert 0 < pos < self.length` 限制了 `pos` 的取值，为什么两端不能取？

**参考答案**：`pos==0` 意为「整段都没命中」，根本没有可分裂的前缀部分，应直接新建节点而非分裂；`pos==self.length` 意为「整段都命中」，无需分裂。两种情况下分裂都没有意义，故用断言挡住。

---

### 4.2 _tree_walk：按 page 对齐的匹配与分裂

#### 4.2.1 概念说明

`_tree_walk` 是匹配与插入共用的「在树里走一遍」的过程。它的核心思想是 **page 对齐匹配**：

- 每次向下走一层，看的是「输入剩余部分的第一个 page」（`key_fn`），跳到对应孩子。
- 走到孩子后，用 `fast_compare_key` 算出公共前缀长度，但必须**向下对齐到 `page_size` 的倍数**：

\[ \text{match\_len} = \mathrm{align\_down}(\text{common\_len},\ \text{page\_size}) = \left\lfloor \frac{\text{common\_len}}{\text{page\_size}} \right\rfloor \cdot \text{page\_size} \]

为什么要对齐？因为 KV cache 的最小分配/复用单位是 page。一个 page 内的 token 必须要么整体复用、要么整体重算——你不能只复用半个 page 的 KV。所以即便 `fast_compare_key` 报告「公共前缀 3 个 token」（page_size=2 时），也只能认定前 2 个 token（1 个 page）命中，第 3 个 token 留给后续 prefill 重算。

> 对应工具 [utils/misc.py:39-41](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/utils/misc.py#L39-L41) 的 `align_down`。

#### 4.2.2 核心流程

`_tree_walk(input_ids)` 的伪代码：

```
node = root, prefix_len = 0, tic = 当前纳秒时间戳
while prefix_len < len(input_ids):
    child = node.children.get( key_fn(input_ids[prefix_len:]) )   # 取剩余输入的第一个 page
    if child is None:        # 没有匹配的孩子 → 停在这里
        return node, prefix_len
    node = child
    match_len = align_down( node.get_match_len(input_ids[prefix_len:]), page_size )
    prefix_len += match_len
    if match_len != node.length:   # 只命中了节点的一部分 → 分裂节点，停在「命中部分」
        node = node.split_at(match_len); node.timestamp = tic
        return node, prefix_len
    node.timestamp = tic           # 整段命中，刷新 LRU 时间戳，继续往下找
return node, prefix_len
```

返回值是 `(停留节点, 命中长度 prefix_len)`。两种「提前返回」：

1. **找不到孩子**：返回当前 `node` 与已有 `prefix_len`（树里没有更长的匹配）。
2. **部分命中需分裂**：分裂后返回「命中部分」对应的节点，`prefix_len` 是对齐后的命中长度。

注意一个重要副作用：`_tree_walk` 每经过一个「整段命中」的节点都会刷新它的 `timestamp`，相当于一次「LRU touch」——被用到的子树会变「年轻」，不容易被淘汰。

`key_fn` 由 `page_size` 决定，定义在 [radix_cache.py:234-237](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/radix_cache.py#L234-L237)：`page_size==1` 时取单个 token（`x[0].item()`），否则取前 `page_size` 个 token 组成的 tuple。

#### 4.2.3 源码精读

完整的树遍历：

[radix_cache.py:205-231](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/radix_cache.py#L205-L231) —— 注意第 217 行的注释「at least 1 page is matched, so match_len >= page_size」：因为能走进这个孩子，说明它的第一个 page 已经和输入对上了（孩子就是靠第一个 page 索引到的），所以 `match_len` 至少是 `page_size`，`align_down` 之后不会变 0。

`match_prefix` 只是薄薄一层封装——走树，把结果包成 handle，**不加锁、不改树**：

[radix_cache.py:132-134](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/radix_cache.py#L132-L134) —— 这与 [base.py:82-93](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/base.py#L82-L93) 的接口契约一致：`match_prefix` 只读，返回的 indices 只有在 handle 被 `lock_handle` 锁住后才安全使用，否则随时可能被 `evict` 回收。

`insert_prefix` 在 `_tree_walk` 基础上「补一个新尾巴」：

[radix_cache.py:136-146](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/radix_cache.py#L136-L146) —— 逐行解读：

1. `insert_len = align_down(len(input_ids), page_size)`：只把「完整的若干个 page」插入，尾巴不足一个 page 的部分丢弃。
2. 截断 `input_ids` 与 `indices` 到 `insert_len`。
3. 走树得到 `(node, prefix_len)`。
4. 若 `prefix_len != insert_len`（还有没缓存的新前缀）：新建节点，key = `input_ids[prefix_len:]`，value = `indices[prefix_len:].clone()`。**注意 `.clone()`**——`indices` 来自 `page_table` 的切片，是个 view，必须复制一份存进树里，否则后面 `page_table` 改动会污染缓存。
5. 新节点以 `ref_count=0` 挂上，`evictable_size += 新节点长度`。
6. 返回 `InsertResult(prefix_len, handle)`：`prefix_len` 是「插入前已在缓存里的长度」（调用方据此释放重复分配），handle 的 `cached_len` 是 `insert_len`（本次插入后覆盖到的总长度）。

`InsertResult` 的字段语义见 [base.py:57-59](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/base.py#L57-L59)。

#### 4.2.4 代码实践

**实践目标**：手动跟踪两次 `match`（一次全命中、一次部分命中）与一次会触发分裂的 `insert`，画出每一步的树。这是「源码阅读型实践」——执行依赖 `fast_compare_key` kernel，若无该 kernel，按下面给出的预期结果对照阅读即可（**待本地验证**执行）。

**场景设定**（`page_size=2`，token 用整数表示，`_value` 是 KV 槽位下标）：

- 初始树只有 root（`ref_count=1`，恒受保护）。
- 步骤 A：`insert_prefix([1,2,3,4], [10,11,12,13])`。
- 步骤 B：`insert_prefix([1,2,5,6], [20,21,22,23])`（与 A 共享前缀 `[1,2]`，会触发分裂）。
- 步骤 C：`match_prefix([1,2,3,4])`（全命中）。
- 步骤 D：`match_prefix([1,2,9,9])`（部分命中 2 个 token）。

**需要观察的现象与预期结果**：

步骤 A 后（root 无孩子，`_tree_walk` 立刻返回 `(root, 0)`，新建整段节点）：

```
root ──► A[1,2,3,4]  value=[10,11,12,13]  ref=0
evictable_size = 4,  protected_size = 0
```

步骤 B：走树时 `root.children[(1,2)]` 命中 A，`get_match_len([1,2,3,4], [1,2,5,6])` 返回 2（首个不同在第 2 位），`align_down(2,2)=2`。`match_len(2) != A.length(4)` → 把 A 在 pos=2 分裂：

```
root ──► AB[1,2] value=[10,11] ref=0
            ├──► A[3,4]   value=[12,13] ref=0   (原 A 的后半段)
            └──► B[5,6]   value=[22,23] ref=0   (本次新增的尾巴)
evictable_size = 6,  protected_size = 0
```

步骤 C：`match_prefix([1,2,3,4])` 一路走 AB（整段命中，prefix_len=2）→ A（整段命中，prefix_len=4），返回 `(A, 4)`。**全命中**，handle 的 `cached_len=4`。命中刷新了 AB、A 的 timestamp。sizes 不变。

步骤 D：`match_prefix([1,2,9,9])` 走到 AB（prefix_len=2），`AB.children[(9,9)]` 不存在，返回 `(AB, 2)`。**部分命中 2 个 token**，handle 的 `cached_len=2`。

**若要实际执行**（需要 `fast_compare_key` kernel 已构建），可仿照 `tests/core/test_cache_allocate.py` 的 fixture 在 CPU 上构造 `RadixPrefixCache`：先 `core.set_global_ctx(core.Context(page_size=2))`，再 `cache = RadixPrefixCache(torch.device("cpu"))`，然后调用上面的 insert/match。注意：首次向空树 insert 不需要 kernel，但只要走到「有孩子且 key 部分重叠」的分支就会触发 `fast_compare_key`。

#### 4.2.5 小练习与答案

**练习 1**：`insert_prefix` 里为什么必须对 `indices[prefix_len:]` 调 `.clone()`，而对 `input_ids` 不需要？

**参考答案**：`indices` 来自调用方传入的 `page_table` 切片，是一个**视图（view）**，后续 `page_table` 被改写时会随之变化，导致缓存里存的下标被污染，所以必须复制。`input_ids` 通常已是独立的 token id 张量（来自 tokenizer），不存在被后续改写的风险，且 key 主要用于比较与计算 `key_fn`，复制与否影响不大，故未强制 clone。

**练习 2**：如果 `page_size=1`，`key_fn` 和「整段命中」的判定会发生什么变化？

**参考答案**：`_get_key_fn(1)` 返回 `x[0].item()`，即孩子按**单个 token** 索引；每次匹配的最小步长变成 1。`align_down(x, 1)==x` 恒成立，所以「公共前缀长度」就是「命中长度」，不再有「半个 page」的舍入。这意味着匹配粒度更细，但孩子节点更密、树更扁更宽。

---

### 4.3 lock / unlock：用 ref_count 区分可淘汰与受保护

#### 4.3.1 概念说明

匹配出来的前缀对应的 KV，**在被 Engine 真正用于前向计算之前，不能被淘汰**，否则读到的是已被覆盖的脏数据。`ref_count` 就是这把「使用中」的锁：

- 每个节点的 `ref_count` 表示「当前有多少个 handle 正在引用本节点」。
- `ref_count > 0` → 受保护（protected），不可淘汰。
- `ref_count == 0` → 无人引用（evictable），可被淘汰。

更关键的一点：**加锁会一路上溯到 root**。锁住一个深处的叶子节点时，它所有的祖先节点 `ref_count` 都会 +1。因为祖先节点承载的是「共享前缀」，只要还有任何后代在被使用，这段共享前缀就不能被回收。

两个尺寸计数器随之维护（见 [radix_cache.py:108-109](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/radix_cache.py#L108-L109) 初始化、[radix_cache.py:180-185](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/radix_cache.py#L180-L185) 的 `size_info`）：

\[ \text{evictable\_size} = \sum_{\substack{n \ne \text{root}\\ \text{ref\_count}(n)=0}} \text{length}(n), \qquad \text{protected\_size} = \sum_{\substack{n \ne \text{root}\\ \text{ref\_count}(n)>0}} \text{length}(n) \]

二者之和 = 树里所有非根节点承载的 token 总数，也就是当前被缓存占用的 KV 槽位总量（`SizeInfo.total_size`，见 [base.py:48-54](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/base.py#L48-L54)）。

#### 4.3.2 核心流程

`lock_handle(handle, unlock=False)` 从 handle 指向的节点出发，**一直走到 root（不含 root）**，逐节点调整：

- **加锁（unlock=False）**：对每个非根节点，若它的 `ref_count` 当前是 0（说明原本在 evictable 桶），先把它的长度从 `evictable_size` 挪到 `protected_size`，然后 `ref_count += 1`。
- **解锁（unlock=True）**：对每个非根节点，先 `ref_count -= 1`（断言不减到负），若减到 0，把它的长度从 `protected_size` 挪回 `evictable_size`。

注意：`lock_handle` **只动两个尺寸计数器和 ref_count，不修改树结构、不移动任何张量**——这与 [base.py:69-80](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/base.py#L69-L80) 的接口契约一致：「This operation will not modify the cache, but change the size info only」。

`RadixCacheHandle.get_matched_indices` 负责把「命中前缀」对应的下标**重新拼出来**：

[radix_cache.py:91-98](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/radix_cache.py#L91-L98) —— 从 handle 节点向 root 收集每个节点的 `_value`，反序（因为收集顺序是从叶到根，而 token 顺序是从根到叶）后 `torch.cat` 成一条完整的下标序列。

#### 4.3.3 源码精读

加锁/解锁的完整逻辑：

[radix_cache.py:113-130](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/radix_cache.py#L113-L130) —— 注意两个细节：

1. 加锁分支里 `if node.ref_count == 0` 才搬尺寸桶；若 `ref_count` 本来就 >0（已被别的 handle 锁着），只做 `+=1`，不重复搬运——避免重复扣减 evictable_size。
2. 解锁分支里 `if node.ref_count == 0`（减完之后）才搬回桶；只要还有其他引用，节点仍受保护。
3. 循环条件 `while not node.is_root()` 保证 root 永不参与计数——root 在 [radix_cache.py:111](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/radix_cache.py#L111) 被设为 `ref_count=1`（「root is always protected」），但它没有 key/value/length，本来也不该计入尺寸。

上层 `CacheManager` 怎么用这套锁？看它在一笔请求 prefill 完成后的处理：

[scheduler/cache.py:55-79](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py#L55-L79) —— `cache_req` 先 `insert_prefix` 得到 `new_handle`，再 `unlock(old_handle)`（释放 prefill 前那把旧锁），若请求未结束则 `lock(new_handle)`（给新前缀上锁），形成「先匹配加锁 → 用完 → 插入新前缀 → 解旧锁加新锁」的闭环。这正是 `BasePrefixCache` 强调的「match 返回的 handle 必须先 lock 再用」。

#### 4.3.4 代码实践

**实践目标**：用真实 `RadixPrefixCache` 在 CPU 上插入若干前缀，再观察 `lock_handle` / `unlock_handle` 引起的 `evictable_size` 与 `protected_size` 此消彼长。本实践**不需要 GPU、也不需要 `fast_compare_key` kernel**——只要插入的前缀「首 page 互不相同」，`_tree_walk` 会在 `root.children.get(...)` 处直接返回，不触发比较 kernel。

**操作步骤**（仿照 `tests/core/test_cache_allocate.py:23-28` 的 fixture）：

```python
# 示例代码：CPU 上观察 lock/unlock 的尺寸搬运
import torch
import minisgl.core as core
from minisgl.kvcache.radix_cache import RadixPrefixCache, RadixCacheHandle

core.set_global_ctx(core.Context(page_size=2))
cache = RadixPrefixCache(torch.device("cpu"))

# 两条首 page 不同(分别是 (10,11) 与 (20,21))的前缀，不会触发 fast_compare_key
h0 = cache.insert_prefix(torch.tensor([10, 11], dtype=torch.int32),
                         torch.tensor([0, 1], dtype=torch.int32)).handle
h1 = cache.insert_prefix(torch.tensor([20, 21], dtype=torch.int32),
                         torch.tensor([2, 3], dtype=torch.int32)).handle
print(cache.size_info)              # 预期: evictable=4, protected=0

cache.lock_handle(h0)               # 锁住第一条
print(cache.size_info)              # 预期: evictable=2, protected=2

cache.lock_handle(h0, unlock=True)  # 解锁
print(cache.size_info)              # 预期: evictable=4, protected=0
```

**需要观察的现象**：每次 `lock_handle`，被锁节点（这里是根下的直接孩子）的长度从 evictable 桶「搬」到 protected 桶；`unlock` 时再搬回。`total_size` 全程不变（=4）。

**预期结果**：`size_info` 在 `evictable=4/protected=0` ↔ `evictable=2/protected=2` 之间切换。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `lock_handle` 要一路走到 root，而不是只锁 handle 指向的那个节点？

**参考答案**：因为一段命中的前缀在物理上由「从 root 到 handle 节点」沿途多个节点拼接而成（见 `get_matched_indices`）。只要这段前缀在使用中，沿途所有承载它的节点（包括共享前缀的祖先）都不能被淘汰，否则拼接出来的下标序列就会缺一段、读到错误槽位。一路加锁保证了整条路径受保护。

**练习 2**：root 节点的 `ref_count` 被初始化为 1，但加锁循环又跳过 root，这两者矛盾吗？

**参考答案**：不矛盾。root 没有 key/value/length，既不计入任何尺寸桶、也不会被淘汰；它的 `ref_count=1` 只是一个「永久受保护」的哨兵，确保即使整棵树只剩 root，root 也不会被误判为可淘汰。加锁循环跳过 root，正是因为 root 本就不参与尺寸统计，调它的 ref_count 没有意义。

---

### 4.4 evict：按 timestamp 的最小堆 LRU 淘汰

#### 4.4.1 概念说明

当 `CacheManager` 的空闲页不够分配时（见 [scheduler/cache.py:106-113](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py#L106-L113) 的 `_allocate`），会调用 `prefix_cache.evict(需要的 token 数)` 回收一批 KV 槽位。淘汰策略是 **LRU（最近最少使用）**：

- 排序键是节点的 `timestamp`（最近一次被 `_tree_walk` 刷新的时间）。
- 用 Python 标准库 `heapq` 维护一个**最小堆**：`__lt__` 按 `timestamp` 比较，timestamp 越小（越老）越在堆顶，越早被淘汰。
- **只淘汰叶子节点**（`is_leaf()` 且 `ref_count==0`）。非叶节点因为还挂着孩子，不能直接删——孩子还在引用下层的 KV。

一个微妙之处：当一个叶子被删掉后，它的父亲可能「变成新叶子」。此时若父亲也 `ref_count==0`，就把它也推进堆里，下一轮可以淘汰它。这保证了**淘汰会向根方向级联**，最终把整段无人引用的子树都回收掉。

#### 4.4.2 核心流程

`evict(size)` 的伪代码：

```
if size == 0: return 空张量                # evict(0) 永远安全
assert size <= evictable_size
leaves = _collect_leave_nodes_for_evict()  # 收集所有 ref_count==0 的叶子
heapq.heapify(leaves)                       # 按timestamp建最小堆
evicted_indices = []; evicted_size = 0
while evicted_size < size:
    node = heappop(leaves)                  # 挑最老的叶子
    assert node.ref_count==0 and node.is_leaf() and not node.is_root()
    evicted_size += node.length
    evicted_indices.append(node.value)      # 收走它的 KV 下标
    evictable_size -= node.length
    parent = node.parent
    del parent.children[ key_fn(node._key) ]# 从树里摘除
    if parent.is_leaf() and parent.ref_count == 0:   # 父亲变新叶子且无人引用
        heappush(leaves, parent)            # 级联：下一轮可淘汰它
return torch.cat(evicted_indices)
```

要点：

1. 返回的是被回收的 KV 槽位下标张量，`CacheManager` 把它们（按 `page_size` 取首下标）还回 `free_slots`。
2. 实际淘汰量 **可能大于** 请求量（`evicted_size` 是按整节点累加，最后一个节点可能让总量越过 `size`），这与 [base.py:108-122](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/base.py#L108-L122) 文档「actual evict size may be larger than requested」一致。
3. `_collect_leave_nodes_for_evict` 用一个栈做 DFS，只挑「叶子且 `ref_count==0`」的节点。

#### 4.4.3 源码精读

收集可淘汰叶子：

[radix_cache.py:190-203](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/radix_cache.py#L190-L203) —— 从 root 开始 DFS，叶子节点只有 `ref_count==0` 才进候选名单；非叶节点把它的孩子压栈继续找。注意这里**不会**把非叶但 `ref_count==0` 的节点直接加入（即使它们的长度已经计入 `evictable_size`），它们要等到孩子被淘汰、自身变成叶子后才会被级联进来。

完整的淘汰循环：

[radix_cache.py:148-175](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/radix_cache.py#L148-L175) —— 重点看三处：

- 第 151-153 行的 `assert size <= self.evictable_size`：请求量不能超过「理论上可回收总量」。
- 第 164-167 行：弹堆后用 `assert` 三重确认节点状态合法（`ref_count==0`、是叶子、非 root），收走它的 `value`。
- 第 169-173 行：从父亲的 `children` 字典里删掉自己；若父亲因此变成叶子且 `ref_count==0`，就 `heappush` 进堆，实现级联淘汰。

堆排序依赖 `__lt__`（4.1.3 已引用的 [radix_cache.py:83-84](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/radix_cache.py#L83-L84)），所以「timestamp 最小 = 最老 = 最先被淘汰」。

#### 4.4.4 代码实践

**实践目标**：在 4.3.4 那棵 CPU 树上调用 `evict`，验证它按「整节点」回收、且会从最老的叶子开始。本实践同样**不需要 kernel**（两条前缀首 page 不同，建树过程不触发 `fast_compare_key`）。

**操作步骤**（接 4.3.4 的 `cache`，已插入两条 2-token 前缀，`evictable_size=4`）：

```python
evicted = cache.evict(2)
print("evicted 下标 =", evicted.tolist())     # 回收一个节点(2 个下标)
print(cache.size_info)                         # evictable 由 4 降到 2
print("剩余可淘汰 =", cache.size_info.evictable_size)
```

**需要观察的现象**：

1. `evict(2)` 恰好回收一个长度为 2 的叶子节点，返回它的两个 KV 下标。
2. `evictable_size` 从 4 降到 2。
3. 再调一次 `cache.evict(2)` 会回收另一个叶子，`evictable_size` 降到 0；此时再 `cache.evict(1)` 会触发 `assert size <= evictable_size` 失败。

**预期结果**：淘汰按节点整段回收，`evictable_size` 阶梯式下降，每步恰好减少被删节点的 `length`。

> 进阶验证（依赖 kernel，**待本地验证**）：若把 4.2.4 中步骤 B 之后的那棵树（`evictable_size=6`，结构为 `root→AB[1,2]→{A[3,4], B[5,6]}`）拿来 `evict(2)`，会回收 A 或 B 中的一个叶子；此时 AB 仍有另一个孩子，不是叶子，不会被级联。只有把 A、B 都淘汰后，AB 才变成叶子、可被进一步淘汰——这印证了「级联」机制。

#### 4.4.5 小练习与答案

**练习 1**：`evictable_size` 计入了某些「非叶但 `ref_count==0`」的节点，但 `_collect_leave_nodes_for_evict` 又不收它们，这两者会不会矛盾、导致 `assert size <= evictable_size` 通过却淘汰不够？

**参考答案**：不矛盾。非叶 `ref_count==0` 节点的长度确实暂时在 `evictable_size` 里但当前不是叶子、无法直接删；然而只要它的孩子都是 `ref_count==0` 的叶子，淘汰循环会把孩子逐个删掉，每删一个就检查父亲是否「变叶子且 `ref_count==0`」并推入堆。于是这些节点会随着孩子被清空而**级联变成可淘汰叶子**，最终整段无人引用的子树都能被回收。所以 `evictable_size` 表示的是「最终可回收总量」，与级联淘汰配合是自洽的。

**练习 2**：为什么淘汰要返回「KV 槽位下标」而不是直接释放某种对象？

**参考答案**：基数树本身**不持有 KV 张量**，它只持有「token → 池槽位下标」的映射。真正的 KV 数据在 `MHAKVCache` 池里（u6-l1）。淘汰的本质是「把这段前缀占用的池槽位标记为可重用」，所以返回下标交给 `CacheManager`，由它把下标还回 `free_slots` 即可。这种「索引层与数据层分离」的设计让基数树保持轻量、纯 CPU 操作。

---

## 5. 综合实践

把四个最小模块串起来。请按下面的顺序，**在纸上**（或可选地在已构建 kernel 的 CPU 环境里）完整走一遍一棵 radix 树的演化，画出每一步的树结构并填写尺寸表。这是本讲的核心练习——它同时覆盖「节点」「分裂」「加锁」「淘汰」四个模块。

**设定**：`page_size=2`，token 用整数，`_value` 为 KV 槽位下标。初始只有 root（`ref_count=1`）。

依次执行：

| 步骤 | 操作 | 类型 |
| --- | --- | --- |
| 1 | `insert_prefix([1,2,3,4], [10,11,12,13])` | 插入（空树，建整段节点） |
| 2 | `insert_prefix([1,2,5,6], [20,21,22,23])` | 插入（共享 `[1,2]`，触发分裂） |
| 3 | `match_prefix([1,2,3,4])` → 然后 `lock(handle)` | 全命中 + 加锁 |
| 4 | `match_prefix([1,2,9,9])` | 部分命中（2 个 token） |
| 5 | `evict(2)` | 淘汰 |

**请画出每一步的树，并填写下表**：

| 步骤 | evictable_size | protected_size | 说明 |
| --- | --- | --- | --- |
| 1 | 4 | 0 | 新增节点 A `[1,2,3,4]`，ref=0 |
| 2 | ? | ? | A 分裂为 AB `[1,2]` + A `[3,4]`，再挂 B `[5,6]` |
| 3 | ? | ? | 全命中刷新 timestamp；锁住 A 的路径（A 与 AB 都 ref→1） |
| 4 | ?（不变） | ?（不变） | match 不改尺寸 |
| 5 | ? | ? | 尝试淘汰 2 个 token——关键看此时谁还是 evictable 叶子 |

**参考答案**（请你先自己填再对照）：

- 步骤 2：`evictable=6, protected=0`。树为 `root → AB[1,2](ref0) → { A[3,4](ref0), B[5,6](ref0) }`。
- 步骤 3：全命中，`prefix_len=4`；`lock(handle)` 沿 A→AB 上溯，A 与 AB 的 `ref_count` 都 0→1，各 2 个 token 从 evictable 搬到 protected。结果 `evictable=2`（只剩 B），`protected=4`（AB + A）。
- 步骤 4：部分命中 2 个 token，handle 指向 AB。**match 不加锁、不改尺寸**，所以 `evictable=2, protected=4` 不变。
- 步骤 5：`evict(2)` 收集 `ref_count==0` 的叶子——此时只有 B（A 被 lock 了，ref=1，不收）。回收 B，返回 `[22,23]`，`evictable` 从 2 降到 0，`protected` 仍为 4。

**关键体会**：

1. 步骤 3 的加锁让 A 这条「正在被某请求使用」的路径免于被步骤 5 淘汰——这正是 `ref_count` 保护「使用中 KV」的作用。
2. 步骤 5 想淘汰 2 个 token 时，可回收的只有 B；如果此时请求 `evict(4)`，虽然 `evictable_size=2 < 4` 会直接触发 `assert` 失败，而不是误删受保护的 A。
3. 整个过程没有任何 GPU 操作——基数树只管「token → 槽位下标」的索引，真正的 KV 数据始终躺在池里（u6-l1）。

**可选的可运行验证**（需要 `fast_compare_key` kernel，**待本地验证**）：用 4.3.4 的 fixture 构造 `RadixPrefixCache`，把上面步骤 1、2 用「共享前缀」的方式执行（会触发 kernel），并在每步打印 `cache.size_info` 与 `_collect_leave_nodes_for_evict()` 的结果对照你的手算。

## 6. 本讲小结

- `RadixPrefixCache` 用一棵**基数树**把多条请求共享的 token 前缀压缩存储，每个 `RadixTreeNode` 保存一段 token（`_key`）及其 KV 槽位下标（`_value`），孩子按「第一个 page」索引。
- 匹配与插入共用 `_tree_walk`，它**按 page 对齐**（`align_down(common_len, page_size)`）推进，遇到「只命中节点一部分」就调 `split_at` 把节点分裂成命中前缀 + 未命中后缀。
- `ref_count` 把节点分成两桶：`>0` 受保护（protected）、`==0` 可淘汰（evictable）；`lock_handle` 沿路径上溯到 root 逐节点调整，且**只动尺寸计数器、不改树结构**。
- `evict` 用 `heapq` 最小堆按 `timestamp` 做 LRU，**只摘叶子**，并在父亲变成新叶子时级联入堆，从而把整段无人引用的子树回收，返回被释放的 KV 槽位下标。
- 基数树是「索引层」，只存 token→槽位映射，不持有 KV 张量；真正的数据在池 `MHAKVCache` 里，淘汰就是把下标还给 `CacheManager` 的 `free_slots`。
- 一切匹配/插入/淘汰都以 `page_size` 为最小粒度，与 paged KV cache 的分配单位严格对齐。

## 7. 下一步学习建议

下一讲 **u6-l3 CacheManager 页分配、回收与淘汰** 会把本讲的基数树接回调度器：看 `CacheManager` 如何用 `free_slots` 做 page 对齐分配、在 `allocate_paged` 里写 `page_table`、在 `cache_req` 里把 prefill 结果 `insert_prefix` 回树并区分 `finished` 释放尾部、以及 `lazy_free_region` 的延迟回收。

延伸阅读建议：

- 对照 [tests/core/test_cache_allocate.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/core/test_cache_allocate.py) 看真实的分配-淘汰-完整性检查循环。
- 若想理解 `fast_compare_key` 这个比较 kernel 如何实现，可在 u10-l2（自定义 kernel）里看 `radix.cpp` 的 AOT 构建。
- 回顾 u6-l1 的 `MHAKVCache` 与 `BasePrefixCache` 接口，确认本讲每个方法都对应一个抽象接口契约。
