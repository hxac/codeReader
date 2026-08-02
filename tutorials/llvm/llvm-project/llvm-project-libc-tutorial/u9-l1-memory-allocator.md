# 内存分配器：block/freelist/freetrie 与 Scudo

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `BlockRef`（`block.h`）如何用一个紧挨一个的「块头 + 可用空间」把一段连续内存切分、合并，并理解它把两个状态位偷偷塞进尺寸低位的省空间技巧。
- 区分 `FreeList`（同尺寸循环链表）与 `FreeTrie`（按尺寸二分重组的 trie）这两种空闲结构的数据结构差异，并理解 `FreeStore` 为什么把二者**组合**起来用——小尺寸走数组、大尺寸走 trie。
- 顺着 `FreeListHeap` 的 `allocate` / `free` 走一遍 malloc/free 的完整流程，看懂「最佳适配取块 → 切分 → 残余回插 → 标记已用」与「标记空闲 → 与左右合并 → 回插」。
- 解释在构建期 Scudo 是如何**在入口点层面**把 `freelist_heap` 版的 `malloc` 替换掉的，从而理解同一函数名为何能挂不同实现。

本讲是「内存管理与并发」单元的第一篇，承接 [u4-l1 __support 总览](u4-l1-internal-support-overview.md)（`__support` 是私有标准库）与 [u8-l2 程序启动](u8-l2-program-startup-crt.md)（链接器符号、运行期初始化）两讲。

## 2. 前置知识

在进入源码前，先用最朴素的语言回顾几个概念。

- **堆（heap）与 malloc/free**：程序运行时需要一块可动态切分的内存，叫堆。`malloc(n)` 从中切出 `n` 字节返回指针，`free(p)` 把它还回去。难点不在「切」，而在「还回去的块以后还能被高效复用，且不要碎成没法用的小渣」。
- **空闲块（free block）与碎片（fragmentation）**：被 free 但尚未被重新分配的块叫空闲块。如果它们又小又散，新请求找不到足够大的连续块，就是碎片。分配器的核心工作之一就是「尽快合并相邻空闲块」（coalescing），把碎片拼回去。
- **最佳适配（best-fit）**：给一个请求尺寸，在所有空闲块里挑「能放下且最小」的那个，避免把大块浪费给小请求。本讲的 `FreeStore` / `FreeTrie` 就是围绕 best-fit 设计的。
- **入口点（entrypoint）与外部入口点**：回顾 [u2-l1 入口点机制](u2-l1-entrypoint-mechanism.md)，每个公开函数是一个独立构建单元。本讲会遇到 `add_entrypoint_external`——它声明「这个入口点的实现来自外部（别处编译好的库），libc 自己不编译它」。这是 Scudo 接管 `malloc` 的关键机制。
- **`__support` 的私有工具**：回顾 [u4-l1](u4-l1-internal-support-overview.md)，`src/__support/` 下的代码不产生公开 C 符号，靠 CMake 的 `DEPENDS` 被入口点引用。本讲的 `block`/`freelist`/`freestore`/`freelist_heap` 都是这种内部目标。

> 名词澄清：仓库里还有一个 [`src/__support/blockstore.h`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/blockstore.h)（一个分块增长的「数组容器」，用于全局对象免堆构造）。它的名字和 `block.h` 像，但**与内存分配器无关**——本讲的「块」专指 `block.h` 里的 `BlockRef`。别被名字误导。

## 3. 本讲源码地图

本讲涉及的关键文件如下表。所有实现都位于 `src/__support/`，是被入口点引用的**内部**目标；唯一例外是 `src/stdlib/` 下的 malloc 入口点与构建选择逻辑。

| 文件 | 角色 | 关键符号 |
| --- | --- | --- |
| `src/__support/block.h` | 最底层：单个内存块的管理 | `BlockRef`、`BlockRef::BlockInfo` |
| `src/__support/freelist.h` / `freelist.cpp` | 同尺寸空闲块的循环 FIFO 链表 | `FreeList`、`FreeList::Node` |
| `src/__support/freetrie.h` | 按尺寸二分重组的「空闲链表之 trie」 | `FreeTrie`、`FreeTrie::Node`、`SizeRange` |
| `src/__support/freestore.h` | **堆真正使用的**混合存储（小数组 + 大 trie） | `FreeStore` |
| `src/__support/freelist_heap.h` / `.cpp` | 组装好的堆，提供 malloc/free | `FreeListHeap`、`freelist_heap` |
| `src/stdlib/baremetal/malloc.cpp` | baremetal/GPU 下的 `malloc` 入口点（委托给 `freelist_heap`） | `LLVM_LIBC_FUNCTION(malloc,…)` |
| `src/stdlib/CMakeLists.txt` | 构建期选择「Scudo 还是 freelist_heap 还是系统 malloc」 | `add_entrypoint_external(malloc …)` |

依赖层次（从底到顶）：

```
BlockRef (block.h)
   ↑ FreeList、FreeTrie 都把 Node 放进 block 的可用空间
FreeList ──┐
           ├── FreeStore (freestore.h)   ← 堆真正持有的是它
FreeTrie ──┘
   ↑
FreeListHeap (freelist_heap.h/.cpp)  ── malloc/free 入口点
   ↑
stdlib/baremetal/malloc.cpp  ── 或被 Scudo 在构建期替换
```

记住这张图，下面的四个最小模块就是把它逐层展开。

## 4. 核心概念与源码讲解

### 4.1 块管理：BlockRef（block.h）

#### 4.1.1 概念说明

`BlockRef` 是整个分配器的地基。给定一段连续内存（比如 1000 字节），`BlockRef` 把它切成「一个挨一个的块」。每个块由两部分组成：

- **块头（header）**：记录这个块的尺寸，以及它与前后块的关系。
- **可用空间（usable space）**：返回给用户的 `malloc` 结果就指向这里。

`BlockRef` 不「拥有」内存，它只是**指向某个块头位置的一个引用**（内部就一个 `header_ptr` 指针）。所有操作（切分、合并、标记已用/空闲）都是通过这个指针读写那片内存里的元数据。

它要解决的核心问题是：**在只多花极少头开销的前提下，支持任意尺寸的切分（split）、合并（merge_next），并能在常数时间内找到前一个/后一个相邻块**，以便 free 时合并相邻空闲块、对抗碎片。

#### 4.1.2 核心流程

一个块头的布局是两个字段，每个 `size_t`：

```
偏移 0........7        含义
+----------+----------+
| prev     | next     |
+----------+----------+
```

- `next` 字段 = **本块的外尺寸（含头）**，同时低两位被偷来做标志位：
  - bit 0 (`PREV_FREE_MASK`)：前一块是否空闲；
  - bit 1 (`LAST_MASK`)：本块是否是「哨兵末块」；
  - 其余位 (`SIZE_MASK`) 才是真正的尺寸。
- `prev` 字段 = 到前一个块的偏移。**只有当前一块空闲时它才「活着」**；前一块若已分配，这 8 字节就被前一块的可用空间「吃掉」复用——这就是文档图里强调的省空间优化。

为什么低两位能拿来存标志？因为块的外尺寸永远对齐到 `MIN_ALIGN`（`max(4, alignof(max_align_t))`，至少 4），所以尺寸的低 2 位恒为 0，白送两位给标志位用。

关键操作的语义：

- `init(region)`：把一段内存初始化成「第一个空闲块 + 一个哨兵末块」。
- `split(new_inner_size)`：把当前块一分为二，前一块留 `new_inner_size`，返回新的后一块。
- `merge_next()`：当前块与紧随其后的块合并（仅当二者都空闲）。
- `mark_used()` / `mark_free()`：在**下一块**的标志位上记录「我是否空闲」，并维护 `prev` 偏移。

分配时的三段切分由 `BlockRef::allocate` 完成，它能把一个块切成最多 3 个块（见 4.1.3）。

#### 4.1.3 源码精读

先看标志位与头大小的常量定义：

[src/__support/block.h:103-118](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/block.h#L103-L118) 定义了 `PREV_FREE_MASK`/`LAST_MASK`/`SIZE_MASK` 三个掩码、头大小 `HEADER_SIZE`，以及保证低两位空闲的 `MIN_ALIGN`。这段是理解后面所有位运算的钥匙。

「外尺寸」「可用尺寸」的换算，揭示了「吃掉下一块 prev 字段」的优化：

[src/__support/block.h:176-194](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/block.h#L176-L194) 中 `inner_size`（已分配时）比 `inner_size_free`（空闲时）多出一个 `PREV_FIELD_SIZE`——因为已分配的块可以吞掉后一块的 prev 字段。这就是文档里「Block 1 (used)」图示的代码根据。

切分是分配的原子动作：

[src/__support/block.h:485-520](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/block.h#L485-L520) `BlockRef::split`：算出满足 `new_inner_size` 的最小外尺寸 → 在该位置 `as_block` 建新块 → 改写当前块的 `next`（保留标志位、换上新尺寸）→ 新块 `mark_free`。注意第 507-515 行 `was_free` 的处理：切分后要保持原有的空闲状态正确传播。

合并用于 free 时拼回碎片：

[src/__support/block.h:522-531](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/block.h#L522-L531) `BlockRef::merge_next`：只在自己和下一块都空闲时才合并，新尺寸 = 二者外尺寸之和，改写当前块 `next` 并更新（新的）下一块的 `prev`。

最精巧的是带对齐的分配——它一次最多产出 3 个块：

[src/__support/block.h:398-415](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/block.h#L398-L415) 定义返回类型 `BlockInfo`，含三个字段：`block`（对齐后的目标块）、`prev`（必要时切出的「填充块」）、`next`（切完目标尺寸后的剩余块）。

[src/__support/block.h:446-482](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/block.h#L446-L482) `BlockRef::allocate` 的实现：若当前块可用空间未按 `alignment` 对齐，先切出一个 `prev`（填充）块把指针推到对齐位置（若前面有空闲块则直接合并掉填充块）；再按 `size` 切出 `next` 残余块。这正是 `aligned_allocate` 的底座。

#### 4.1.4 代码实践

**实践目标**：亲手追踪「一段原始内存如何变成第一个块 + 哨兵末块」。

**操作步骤**：

1. 阅读 [`src/__support/block.h:417-444`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/block.h#L417-L444) 的 `BlockRef::init`：它先用 `next_possible_block_start` 对齐起点，用 `prev_possible_block_start` 对齐终点，再用 `as_block` 建第一块、`make_last_block` 建哨兵末块。
2. 对照同文件顶部 [文档图示 block.h:57-97](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/block.h#L57-L97)，画出两个连续块的字节布局，标出 `prev`/`next` 字段的位置与含义。
3. 想象在第一个块上调用 `split(100)`：根据 [L485-520](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/block.h#L485-L520) 推演「当前块的 next 如何被改写、新块如何被 mark_free」。

**需要观察的现象**：第一块的 `next` 字段原值 = 到哨兵末块的偏移；split 后第一块的 `next` 变成到新块的偏移，新块的 `next` 才是到哨兵的偏移。

**预期结果**：你能用一张图说明 split 前后两个字段值的变化。运行命令方面——本模块是「源码阅读型实践」，无需运行；若想看运行结果，可阅读 [`test/src/__support/block_test.cpp`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/__support/block_test.cpp)（若有）中的断言来核对推演，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SIZE_MASK` 恰好是 `~(PREV_FREE_MASK | LAST_MASK)`，而不用担心它丢掉真实尺寸信息？

**答案**：因为块外尺寸对齐到 `MIN_ALIGN ≥ 4`，低两位恒为 0，所以把这两位用作标志位不会损失任何尺寸信息，掩掉它们即可还原真实尺寸。

**练习 2**：`mark_used()` 为什么是去改「下一块」的标志位，而不是改本块自己的字段？

**答案**：本块的 `next` 字段存的是自己的尺寸，没有多余的「自己是否空闲」位；而「前一块是否空闲」恰好存在**下一块**的 `PREV_FREE_MASK` 位里。所以「我是否空闲」=「下一块的 prev_free 位」，`mark_used`/`mark_free` 自然要去改下一块。见 [block.h:244-256](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/block.h#L244-L256)。

---

### 4.2 同尺寸空闲链表：FreeList（freelist.h / freelist.cpp）

#### 4.2.1 概念说明

`BlockRef` 解决了「单块怎么管」，但分配器还需要一个结构来回答：**「现在有哪些空闲块可用？」** 这就是「空闲结构」。`FreeList` 是其中一种，专门服务于**同一尺寸**的空闲块。

它的特点（见类注释）：

- **循环双向链表**，FIFO（先进先出）。
- 链表只装**尺寸相同**的块——这是它能用极简结构的前提。
- **Node 直接放在块的可用空间里**（overlay），不额外占内存：块空闲时，它的可用空间头部就当一个 `Node`；块一旦分配出去，这个 Node 自然就被用户数据覆盖。所以 `FreeList` 「引用但不拥有」Node。

为什么用 FIFO？注释说得直白：FIFO 让「最久没被用的块最晚被重新分配」，从而**最大化空闲块存活时间**，给相邻块合并留出更多机会，从而降低碎片。

#### 4.2.2 核心流程

一个 `FreeList` 对象只持有一个 `begin_` 指针，指向链表的某个 Node；因为链表是循环的，一个入口即可遍历整条。

- `push(block)`：在块可用空间里 `new` 一个 `Node`，插到 `begin_` 之前（即队尾）。
- `pop()`：摘掉 `begin_`（队首）。
- `remove(node)`：摘掉任意指定 Node。
- `size()`：返回链表里块的 `inner_size`（同一条链表里所有块尺寸相同，取首块即可）。

因为同尺寸，所以 `FreeList` 不需要做任何「找合适尺寸」的搜索——一条链表就是一种尺寸。这也意味着：**要支持多种尺寸，就得有很多条 `FreeList`**（这正是 4.3 里 `FreeStore` 的做法）。

#### 4.2.3 源码精读

类与 Node 定义：

[src/__support/freelist.h:21-49](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freelist.h#L21-L49) 类注释解释了 FIFO 降低碎片的设计意图；`Node` 只有两个私有指针 `prev`/`next`，且提供 `block()` 用 `BlockRef::from_usable_space(this)` 从 Node 反推所属块——再次体现「Node 即块可用空间头部」。

插入与删除的循环链表实现：

[src/__support/freelist.cpp:18-31](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freelist.cpp#L18-L31) `push`：非空时把新 Node 插在 `begin_` 的正前方（`begin_->prev` 与 `begin_` 之间），这是循环链表「插到队尾」的标准手法；空时让 Node 自指。注意第 20-22 行的断言——同一条链表的块外尺寸必须相同。

[src/__support/freelist.cpp:33-47](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freelist.cpp#L33-L47) `remove`：处理「链表只剩这一个 Node（自指）」的边界，否则标准的双向摘除；若摘的恰好是 `begin_`，则把 `begin_` 后移到 `next`。

把块推入链表的便捷入口：

[src/__support/freelist.h:70-76](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freelist.h#L70-L76) `push(BlockRef)`：断言块确实空闲、且大到放得下一个 `FreeList`（`sizeof(FreeList)`，即两个指针），然后 placement-new 出 Node 再 push。

#### 4.2.4 代码实践

**实践目标**：手画一条 `FreeList` 在 push/pop 时的指针变化。

**操作步骤**：

1. 假设有 3 个同尺寸空闲块 A、B、C（可用空间里各放一个 Node）。
2. 按 [`freelist.cpp:18-31`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freelist.cpp#L18-L31) 依次 push A、B、C，画出每个 Node 的 `prev`/`next` 指向（循环回 `begin_`）。
3. 调一次 `pop()`（摘 `begin_`），按 [L33-47](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freelist.cpp#L33-L47) 推演 `begin_` 如何移动。

**需要观察的现象**：push 永远插在队尾、`begin_` 维持指向「最老的」块；pop 永远取 `begin_`——所以确实是 FIFO。

**预期结果**：你得到一张循环双向链表图，能说清「为什么 FIFO 让老块最后才被复用」。本实践为源码阅读型，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `FreeList` 要求同一条链表里所有块尺寸相同？

**答案**：因为它的查找是「整条链表都是这个尺寸」，无需比较。若尺寸不同，单条链表无法直接 best-fit，就需要 4.3 的 trie 或一个尺寸→链表的索引（这正是 `FreeStore` 的 `small_lists` 数组做的事）。

**练习 2**：`Node` 放在块的可用空间里，会不会和用户数据冲突？

**答案**：不会。块空闲时才在链表里，此时可用空间没存用户数据；一旦分配出去，块从链表摘除，Node 位置就被用户数据合法覆盖。这就是 overlay 的妙处——零额外内存。

---

### 4.3 变尺寸最佳适配：FreeTrie 与 FreeStore 混合存储

#### 4.3.1 概念说明

`FreeList` 只管一种尺寸，但堆里空闲块尺寸千差万别。要支持「任意尺寸的 best-fit」，通常需要一棵按尺寸排序的平衡二叉搜索树（如红黑树）。`FreeTrie` 走了另一条路：**一棵「空闲链表之 trie」**，灵感来自 Doug Lea 的 malloc（见类注释 [freetrie.h:21-43](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freetrie.h#L21-L43)）。

核心思想：预先固定一个**尺寸范围** `[min, min+width)`（`width` 是 2 的幂）。每个 trie 节点把自己的范围对半切成 `lower`/`upper` 两个子 trie；节点本身挂一条 `FreeList`（复用 4.2 的结构）。这样：

- 查找 best-fit 的复杂度是对**尺寸范围的宽度**（一个固定量）取对数，而非对节点数取对数。
- 实现比红黑树简单得多——特殊情况少，代码体积小（这对 libc 重要）。

但 `FreeTrie` 的 `Node` 比 `FreeList::Node` 多两个指针（`lower`/`upper`/`parent`），要求块更大才放得下。所以 LLVM-libc 没有二选一，而是用 `FreeStore` 把二者**组合**：小尺寸（放不下 trie 节点）用一组 `FreeList` 数组，大尺寸用 `FreeTrie`。这是本讲最重要的结论之一：**`FreeList` 与 `FreeTrie` 是互补的尺寸分级，不是非此即彼的取舍。**

#### 4.3.2 核心流程

**`FreeTrie` 的 `SizeRange`**：`{min, width}`，`width` 是 2 的幂。`lower()`/`upper()` 各取半区。`find_best_fit(size)` 在 trie 里沿 lower/upper 下钻，必要时「延迟」记住一个 upper 子 trie 待回溯，最终返回能放下 `size` 的最小节点。

**`FreeStore` 的分派**（堆真正持有的就是它）：

```
insert(block):
  if 块太小(< MIN_OUTER_SIZE): 丢弃
  elif 是小块(< MIN_LARGE_OUTER_SIZE): 放进 small_lists[按尺寸算出的下标]
  else: 放进 large_trie

remove_best_fit(size):
  先在 small_lists 里线性找第一个能放下 size 的非空链表  → 取队首
  否则在 large_trie 里 find_best_fit(size)              → 取该节点
```

`small_lists` 是一个 `cpp::array<FreeList, NUM_SMALL_SIZES>`，下标 = `(块外尺寸 - MIN_OUTER_SIZE) / MIN_ALIGN`——每个下标对应一种精确尺寸，所以小块是**精确尺寸桶**（O(1) 索引 + 小常数线性扫描）。大块才需要 trie 的对数 best-fit。

#### 4.3.3 源码精读

`FreeTrie::Node` 复用 `FreeList::Node` 并加上子树指针：

[src/__support/freetrie.h:51-63](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freetrie.h#L51-L63) `class Node : public FreeList::Node`，新增 `lower`/`upper`/`parent`。正因为多了三个指针，trie 节点比链表节点更「胖」，块得更大才装得下——这就是 `MIN_LARGE_OUTER_SIZE` 阈值的由来。

`SizeRange` 的二分：

[src/__support/freetrie.h:66-88](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freetrie.h#L66-L88) `lower()`/`upper()` 把范围对半分；`contains(size)` 判断归属。

`find_best_fit` 的核心循环：

[src/__support/freetrie.h:159-238](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freetrie.h#L159-L238) 这是 trie 最复杂的函数。它维护一个 `best_fit` 候选和一个「延迟处理的 upper 子 trie」（`deferred_upper_trie`）：因为 lower 子 trie 里的任何 fit 都比 upper 里的好，所以优先下钻 lower，把 upper 暂存；只有 lower 没找到更好的才回溯 upper。第 178-179 行「精确匹配直接返回」是快路径。

`FreeStore` 的混合存储定义：

[src/__support/freestore.h:50-68](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freestore.h#L50-L68) 关键三个常量：`MIN_OUTER_SIZE`（放得下 `FreeList::Node` 的最小块）、`MIN_LARGE_OUTER_SIZE`（放得下 `FreeTrie::Node` 的最小块）、`NUM_SMALL_SIZES`（小块桶数量）。成员就两个：`small_lists`（`FreeList` 数组）与 `large_trie`。

`remove_best_fit` 的「先小后大」：

[src/__support/freestore.h:91-103](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freestore.h#L91-L103) 先 `find_best_small_fit` 在小块桶里找，找到就 `pop` 队首；否则才查 `large_trie.find_best_fit`。小块用线性扫描是因为 `NUM_SMALL_SIZES` 是个小常数。

`insert` 的尺寸分派：

[src/__support/freestore.h:71-78](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freestore.h#L71-L78) 太小丢弃、小块入桶、大块入 trie——三行决定一个空闲块的去向。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：对比 `FreeList` 与 `FreeTrie` 的数据结构差异、各自适合的分配负载，并解释 `FreeStore` 为何把二者组合。这正是本讲规格指定的实践任务的前半部分。

**操作步骤**：

1. 打开 [`src/__support/freelist.h:21-49`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freelist.h#L21-L49) 与 [`src/__support/freetrie.h:21-63`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freetrie.h#L21-L63)，记录每个 Node 的字段数：`FreeList::Node` 2 个指针，`FreeTrie::Node` 5 个指针（继承 2 + lower/upper/parent）。
2. 写一段说明（建议 150 字以内），覆盖：
   - **结构差异**：FreeList 是同尺寸的扁平循环链表；FreeTrie 是按尺寸范围二分重组的「链表之树」，支持变尺寸 best-fit。
   - **各自适合的负载**：FreeList 适合「尺寸种类少且固定」的负载（每个尺寸一条链表，零搜索）；FreeTrie 适合「尺寸分散、需要 best-fit」的负载，但单块元数据更大。
3. 阅读 [`src/__support/freestore.h:50-78`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freestore.h#L50-L78)，解释为什么小尺寸用 `FreeList` 数组、大尺寸才用 `FreeTrie`——因为小到放不下 trie 节点的块只能用更瘦的链表节点，且小尺寸桶数有限、线性扫描可接受。

**需要观察的现象**：`MIN_LARGE_OUTER_SIZE` 明显大于 `MIN_OUTER_SIZE`，差值正好决定了有多少个「小块桶」（`NUM_SMALL_SIZES`）。

**预期结果**：你能用一句话回答「FreeList 与 FreeTrie 不是二选一，而是 FreeStore 按尺寸分级的两段」。本实践为源码阅读型，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`FreeTrie::find_best_fit` 的复杂度是对「什么」取对数？为什么类注释说它「可能比红黑树还慢」但仍被采用？

**答案**：对**尺寸范围的宽度**（固定的 2 的幂）取对数，而非对节点数。节点数极大时它不会更快；但它实现简单、特殊情况少、代码体积小，这对 libc 更重要。见 [freetrie.h:30-34](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freetrie.h#L30-L34)。

**练习 2**：`FreeStore` 在小块上为什么用「精确尺寸桶 + 线性扫描」而不是也建一棵 trie？

**答案**：小块种类数 `NUM_SMALL_SIZES` 是个小常数，线性扫描代价固定且很小；而 trie 节点更胖，小到放不下。所以小尺寸用数组最省、最快。

---

### 4.4 堆组装：FreeListHeap 的分配与释放

#### 4.4.1 概念说明

把 4.1 的 `BlockRef`（切块）和 4.3 的 `FreeStore`（管空闲块）装到一起，就是 `FreeListHeap`——一个能直接对外提供 `malloc`/`free`/`realloc`/`calloc` 的堆。它持有：

- 一段堆区 `[begin, end)`；
- 一个 `FreeStore`（注意：不是直接持有 `FreeList` 或 `FreeTrie`，而是持有它们的组合 `FreeStore`）；
- 一个 `is_initialized` 标志——**惰性初始化**，第一次 `allocate` 时才真正把堆区切成初始块。

默认堆区由两个链接器符号界定：`_end`（BSS 段尾，堆起点）与 `__llvm_libc_heap_limit`（堆上限）。这在 [u8-l2 程序启动](u8-l2-program-startup-crt.md) 讲过的链接/运行期上下文里自然衔接。

#### 4.4.2 核心流程

**初始化 `init()`（首次分配时触发）**：

```
BlockRef::init(region())           // 把 [begin,end) 切成「第一块 + 哨兵末块」
free_store.set_range({0, bit_ceil(第一块 inner_size)})  // 设 trie 的尺寸范围
free_store.insert(第一块)          // 把唯一的空闲块放进存储
is_initialized = true
```

**分配 `allocate_impl(alignment, size)`**：

```
request_size = BlockRef::min_size_for_allocation(alignment, size)  // 算最少需要多大块
block = free_store.remove_best_fit(request_size)                   // best-fit 取一块
block_info = BlockRef::allocate(block, alignment, size)            // 切成最多 3 块
if block_info.next: free_store.insert(block_info.next)             // 残余回插
if block_info.prev: free_store.insert(block_info.prev)             // 填充块回插
block_info.block.mark_used()                                       // 标记已用
return block_info.block.usable_space()                             // 返回可用空间指针
```

**释放 `free(ptr)`**：

```
block = BlockRef::from_usable_space(ptr)   // 从用户指针反推块
block.mark_free()
if 前一块空闲: free_store.remove(前一块); block=前一块; block.merge_next()  // 与左合并
if 后一块空闲: free_store.remove(后一块); block.merge_next()                // 与右合并
free_store.insert(block)                   // 合并后的大块回插
```

这就是「free 时尽力合并相邻空闲块」对抗碎片的全过程。

#### 4.4.3 源码精读

堆区由链接器符号界定：

[src/__support/freelist_heap.h:31-44](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freelist_heap.h#L31-L44) `extern _end` 与 `__llvm_libc_heap_limit` 是两个链接器符号；默认构造函数把堆区设为 `[_end, &__llvm_libc_heap_limit)`。`FreeListHeap` 的私有成员里，关键就是 `FreeStore free_store`（L73）——堆真正调用的空闲存储。

惰性初始化：

[src/__support/freelist_heap.h:84-91](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freelist_heap.h#L84-L91) `init()` 调 `BlockRef::init` 切初始块，并用 `cpp::bit_ceil` 把第一块尺寸向上取整为 2 的幂作为 trie 的 `width`，再插入存储。

分配主路径：

[src/__support/freelist_heap.h:93-116](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freelist_heap.h#L93-L116) `allocate_impl`：先 `min_size_for_allocation` 算出考虑对齐后的最小需求（见 [block.h:266-299](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/block.h#L266-L299)），再 `remove_best_fit` 取块，`BlockRef::allocate` 切分，残余/填充块回插，最后 `mark_used`。这段就是 4.4.2 流程图的逐行对应。

释放与合并：

[src/__support/freelist_heap.h:138-167](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freelist_heap.h#L138-L167) `free`：先做 `is_valid_ptr`/`used()` 断言（含「double free」检测，L148），`mark_free` 后尝试与 `prev_free()`、`next()` 合并，每次合并前都要先从 `free_store` 里 `remove` 掉被吞并的块（否则存储里会出现悬空引用），最后把合并后的大块 `insert` 回去。

`realloc` 的「原地缩小 vs 搬家」策略：

[src/__support/freelist_heap.h:207-240](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freelist_heap.h#L207-L240) 新尺寸更小则 `shrink_in_place` 原地切分；更大则新分配一块、`inline_memcpy` 搬数据、free 旧块。注意第 234 行「新分配失败时不 invalidate 旧指针」的细节。

全局堆指针：

[src/__support/freelist_heap.cpp:16-17](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freelist_heap.cpp#L16-L17) 一个 `LIBC_CONSTINIT` 的静态 `FreeListHeap` 对象，配合一个对外指针 `freelist_heap`——用 `constinit` 是为了在 main 之前、无堆的前提下就可用（呼应 [u8-l2](u8-l2-program-startup-crt.md) 的启动约束）。

#### 4.4.4 代码实践

**实践目标**：跑通（或读懂）`freelist_heap` 的单元测试，验证分配不重叠与 free 后复用。

**操作步骤**：

1. 阅读 [`test/src/__support/freelist_heap_test.cpp:22-29`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freelist_heap_test.cpp#L22-L29)：测试用一段内联汇编定义 `_end` 与 `__llvm_libc_heap_limit`，中间 `.fill 1024` 即 1024 字节堆区——这正好对应 4.4.1 说的「堆区由链接器符号界定」。
2. 阅读 [`freelist_heap_test.cpp:48-74`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freelist_heap_test.cpp#L48-L74) 的 `TEST_FOR_EACH_ALLOCATOR` 宏与 `CanAllocate` 用例：它用一个 `FreeListHeapBuffer<2048>`（带内嵌缓冲区的子类，见 [freelist_heap.h:76-82](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freelist_heap.h#L76-L82)）模拟全局堆，再对一个栈上 `FreeListHeap allocator(buf)` 各跑一遍。
3. 若已按 [u1-l3](u1-l3-build-and-run.md) 配好 runtimes 构建，用类似下面的命令单独跑这个测试（目标名以实际生成为准）：

   ```bash
   ninja -C <build> libc.test.src.__support.freelist_heap_test.__unit__
   ```

**需要观察的现象**：`AllocationsDontOverlap` 用例断言两次 `allocate(512)` 返回的区间不重叠（`ptr2_start > ptr1_end`）；`CanFreeAndRealloc` 验证 free 后的块能被再次分配出来。

**预期结果**：测试通过。若未构建，则改为源码阅读型：顺着 `allocate(512)` 在 [freelist_heap.h:118-120](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freelist_heap.h#L118-L120) → `allocate_impl` 的调用链，口头推演两次分配各切出哪段，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`free` 在与左/右块合并前，为什么必须先 `free_store.remove(...)` 被合并的块？

**答案**：被吞并的块从此不再独立存在（它的内存并入邻居），若不从 `FreeStore` 里摘除，存储里就会留下指向已失效块的 Node/trie 节点，下次 `remove_best_fit` 会取到野指针。见 [freelist_heap.h:155-164](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freelist_heap.h#L155-L164)。

**练习 2**：`init()` 为什么不在构造函数里做，而是延迟到第一次 `allocate`？

**答案**：构造函数是 `constexpr`/`constinit`，在 main 之前运行，那时不能做「切内存、写块头」这类有副作用的工作；且若从不分配就不必初始化，省开销。所以用 `is_initialized` 标志惰性触发。见 [freelist_heap.h:84-91](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freelist_heap.h#L84-L91) 与 [L97-98](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freelist_heap.h#L97-L98)。

---

### 4.5 Scudo 集成：构建期的入口点替换

#### 4.5.1 概念说明

`FreeListHeap` 是 LLVM-libc **自带**的、纯算法的、零外部依赖的分配器——它适合 baremetal、GPU 这类没有现成 malloc 的目标。但在主流 Linux 上，LLVM-libc 通常**不**用它，而是接入 compiler-rt 的 **Scudo** 分配器（一个带安全加固、线程缓存的生产级分配器）。

关键在于：这个选择**不在源码里用 `if` 判断，而是在构建期通过「入口点替换」完成**。回顾 [u2-l1 入口点机制](u2-l1-entrypoint-mechanism.md) 与 [u2-l3 CMake 规则](u2-l3-cmake-build-rules.md)：`malloc` 是一个入口点，它的「实现来自哪里」由 CMake 决定。`add_entrypoint_external(malloc …)` 声明「这个 `malloc` 的符号由外部库提供，libc 自己不编译它的 `.cpp`」。于是：

- baremetal/GPU：编译 `src/stdlib/baremetal/malloc.cpp`，它调用 `freelist_heap->allocate`；
- Linux + Scudo：用 `add_entrypoint_external(malloc DEPENDS RTScudoStandalone…)`，`malloc` 符号由 Scudo 提供，`baremetal/malloc.cpp` 根本不参与编译；
- Linux 不开 Scudo：`add_entrypoint_external(malloc)` 无依赖，回退到系统 malloc。

这就是「`freelist_heap` 在构建中被 Scudo 替换」的真实含义——**替换发生在入口点层面，而非运行时的指针改写**。

#### 4.5.2 核心流程

构建选择的判定（伪代码，对应 `src/stdlib/CMakeLists.txt`）：

```
if 目标是 baremetal 或 GPU:
    用 baremetal/*.cpp（DEPENDS freelist_heap）   ← FreeListHeap 提供 malloc
else:                                            ← 通常是 Linux
    if LLVM_LIBC_INCLUDE_SCUDO:
        add_entrypoint_external(malloc DEPENDS RTScudoStandalone RTScudoStandaloneCWrappers)
                                                  ← Scudo 提供 malloc，freelist_heap 不参与
    else:
        add_entrypoint_external(malloc)           ← 系统 malloc
```

baremetal 版入口点极薄：

```cpp
LLVM_LIBC_FUNCTION(void *, malloc, (size_t size)) {
  return freelist_heap->allocate(size);
}
```

#### 4.5.3 源码精读

baremetal 入口点委托给 `freelist_heap`：

[src/stdlib/baremetal/malloc.cpp:17-19](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/stdlib/baremetal/malloc.cpp#L17-L19) 整个实现就一行 `return freelist_heap->allocate(size);`。`LLVM_LIBC_FUNCTION` 宏（回顾 [u2-l2](u2-l2-implementation-standard-and-macros.md)）把它包装成公开 C 符号 `malloc`。

baremetal 入口点的 CMake 依赖：

[src/stdlib/baremetal/CMakeLists.txt:7-15](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/stdlib/baremetal/CMakeLists.txt#L7-L15) `add_entrypoint_object(malloc … DEPENDS libc.src.__support.freelist_heap)`——只有走这条路，`freelist_heap` 才真正被链进产物。

Scudo 在构建期接管：

[src/stdlib/CMakeLists.txt:504-543](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/stdlib/CMakeLists.txt#L504-L543) 先决条件 `NOT BAREMETAL AND NOT GPU`（L504）；打开 `LLVM_LIBC_INCLUDE_SCUDO` 后，引入 compiler-rt 的 `RTScudoStandalone.<arch>` 与 `RTScudoStandaloneCWrappers.<arch>`（L528-529），再用 `add_entrypoint_external(malloc DEPENDS ${SCUDO_DEPS})`（L539-543）声明 `malloc` 由 Scudo 提供。注意这里**没有 SRCS**——libc 不编译任何 malloc 源文件。

不开 Scudo 的回退：

[src/stdlib/CMakeLists.txt:574-577](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/stdlib/CMakeLists.txt#L574-L577) `add_entrypoint_external(malloc)` 不带 DEPENDS，`malloc` 符号留给链接时的系统库（Full 模式下即外部）。

`freelist_heap` 作为内部目标的注册：

[src/__support/CMakeLists.txt:62-83](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/CMakeLists.txt#L62-L83) `freelist_heap` 是个 `add_object_library`，`DEPENDS` 串起 `.block`/`.freelist`/`.freestore`/`.freetrie`，正好对应本讲的依赖层次图。它只有被某个入口点（baremetal malloc）`DEPENDS` 时才会进产物——这正是 Scudo 能把它「整体排除」的原因。

#### 4.5.4 代码实践

**实践目标**：解释「`freelist_heap` 在构建中如何被 Scudo 替换」。这是本讲规格指定的实践任务的后半部分。

**操作步骤**：

1. 打开 [`src/stdlib/CMakeLists.txt:504`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/stdlib/CMakeLists.txt#L504)，确认 baremetal/GPU 不走这段。
2. 顺着 [`L505`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/stdlib/CMakeLists.txt#L505) → [`L528-529`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/stdlib/CMakeLists.txt#L528-L529) → [`L539-543`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/stdlib/CMakeLists.txt#L539-L543) 读完 Scudo 分支。
3. 用自己的话写一段（建议 120 字以内）回答：**Scudo 替换 `freelist_heap` 不是删掉它的代码，而是让 `malloc` 这个入口点改由 Scudo 的 `RTScudoStandalone` 提供；`freelist_heap` 因为不再被任何入口点 `DEPENDS`，自然不会进入最终归档。**

**需要观察的现象**：`add_entrypoint_external` 与 `add_entrypoint_object` 的区别——前者无 SRCS（符号外部来），后者有 SRCS（libc 自己编译）。

**预期结果**：你能指出「替换发生在入口点层面，`freelist_heap` 的源码依然在仓库里、只是不被链接」。本实践为源码阅读型，标注「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：为什么 Scudo 分支用 `add_entrypoint_external` 而不是 `add_entrypoint_object`？

**答案**：因为 Scudo 是 compiler-rt 里**已编译好**的库，`malloc` 符号由它提供；libc 不需要、也不应该再编译一份 `malloc.cpp`。`add_entrypoint_external` 正是「符号来自外部」的声明方式（回顾 [u2-l3](u2-l3-cmake-build-rules.md)）。

**练习 2**：假如一个 baremetal 目标也想用 Scudo，能直接套用 Linux 那段 CMake 吗？

**答案**：不能。Linux 分支被 `NOT BAREMETAL AND NOT GPU` 守卫（[L504](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/stdlib/CMakeLists.txt#L504)），baremetal 强制走 `baremetal/*.cpp` + `freelist_heap`。Scudo 依赖运行期线程支持等基础设施，baremetal 通常不具备，故设计上不让二者混用。

---

## 5. 综合实践

把本讲四层串起来，做一个**端到端的「分配—释放—合并」推演**。

**任务**：假设一个刚初始化的 `FreeListHeap`，堆区被 `BlockRef::init` 切成「一个大空闲块 + 一个哨兵末块」。请按顺序推演下列操作，并在每一步标注：①哪个 `BlockRef` 方法被调用；②`FreeStore` 里发生了什么 insert/remove；③块布局如何变化。

1. `p1 = malloc(100)` —— [`allocate_impl`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freelist_heap.h#L93-L116) 取出大块，`BlockRef::allocate` 切出 100 字节块与残余块，残余块回插 `FreeStore`。
2. `p2 = malloc(200)` —— 从残余块再切，得到另一段不重叠区间。
3. `free(p1)` —— [`free`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freelist_heap.h#L138-L167) 把 p1 标记空闲；此时它两侧分别是「堆起点」与「p2（已用）」，故暂无可合并邻居，直接回插。
4. `free(p2)` —— p2 标记空闲后，**左侧是已空闲的 p1**，于是触发 `merge_next`，p1 与 p2 合并成一个大空闲块回插。观察到：经过两次 free，`FreeStore` 里重新出现了一个接近原始大小的大空闲块——这就是合并对抗碎片的效果。

**进阶**：把上面的推演对照 [`freelist_heap_test.cpp`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freelist_heap_test.cpp) 的 `CanFreeAndRealloc` 用例核对；若条件允许，在 `free` 的 [`L149`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/freelist_heap.h#L149) 与 `merge_next` 前各加一行调试日志（仅本地实验，勿提交），观察合并是否如期发生（标注「待本地验证」）。

## 6. 本讲小结

- **`BlockRef`（block.h）** 是地基：用「prev/next 偏移 + 偷占尺寸低两位做标志」的紧凑块头管理连续内存，支持 `split`/`merge_next`/带对齐的 `allocate`，并让「已用块吞掉下一块 prev 字段」来省空间。
- **`FreeList`（freelist.h）** 是同尺寸循环 FIFO 链表，Node 直接 overlay 在块可用空间里；FIFO 顺序意在最大化空闲块存活时间、增加合并机会。
- **`FreeTrie`（freetrie.h）** 是按尺寸范围二分重组的「链表之 trie」，灵感来自 Doug Lea 的 malloc，用更简单的实现换取对尺寸范围（而非节点数）的对数 best-fit。
- **`FreeStore`（freestore.h）** 才是堆真正持有的存储：小尺寸用 `FreeList` 数组（精确桶 + 线性扫描）、大尺寸用 `FreeTrie`——所以 `FreeList` 与 `FreeTrie` 是**互补的尺寸分级**，不是二选一。
- **`FreeListHeap`（freelist_heap.h/.cpp）** 把上述组件装成可用堆，惰性初始化，`allocate` 走 best-fit 取块 + 切分 + 残余回插，`free` 走标记空闲 + 与左右合并 + 回插，对抗碎片。
- **Scudo 集成** 在构建期完成：baremetal/GPU 用 `freelist_heap`，Linux + `LLVM_LIBC_INCLUDE_SCUDO` 时 `malloc` 入口点改由 compiler-rt 的 `RTScudoStandalone` 提供，`freelist_heap` 因不再被 `DEPENDS` 而不进产物——替换发生在**入口点层面**。

## 7. 下一步学习建议

- 下一讲 [u9-l2 线程与同步原语](u9-l2-threads-synchronization.md) 会进入 `__support/threads`，讲解 `raw_mutex`/`futex_utils`/`thread`。本讲的 `freelist_heap` 目前是**单线程**的，学完 u9-l2 后可回来思考：如何用 `raw_mutex` 给 `FreeListHeap::allocate`/`free` 加锁，以及 Scudo 自身的线程缓存为何更适合多线程。
- 想深入块层细节，可读 [`test/src/__support/block_test.cpp`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/__support/block_test.cpp) 中对 `split`/`merge_next`/带对齐 `allocate` 的边界测试。
- 想理解 Scudo 本体，可跳到仓库 `compiler-rt/lib/scudo/standalone/`，对照本讲的 [`stdlib/CMakeLists.txt:528-529`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/stdlib/CMakeLists.txt#L528-L529) 看 `RTScudoStandalone` 与 `RTScudoStandaloneCWrappers` 两个目标的来源。
- 若对「入口点替换」这一构建期机制仍想巩固，可重读 [u2-l1](u2-l1-entrypoint-mechanism.md) 的 SKIP/外部入口点部分，并对比本讲 `add_entrypoint_external(malloc …)` 与普通 `add_entrypoint_object` 的差异。
