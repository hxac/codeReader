# 内存池 njs_mp 与 flathsh 哈希表

## 1. 本讲目标

本讲是进入 njs 内核基础设施的一讲。读完本讲，你应该能够：

- 说清楚 njs 为什么不用标准库的 `malloc`/`free`，而是自研一个内存池 `njs_mp_t`，以及这个池的三级分配策略（小块 chunk / 整页 / 大块）。
- 看懂 `njs_mp_create / njs_mp_alloc / njs_mp_zalloc / njs_mp_free / njs_mp_destroy` 这一组 API 的行为，以及 cleanup 处理器链的作用。
- 理解扁平哈希表 `njs_flathsh_t` 的「一段连续内存装下整张表」的设计、扩缩容机制、以及 `njs_flathsh_proto_t` 三个回调（`test` / `alloc` / `free`）如何把哈希表与「值的语义」和「分配器」解耦。
- 看清 VM（`njs_vm_s.mem_pool`）如何作为单一所有者，统一持有几乎所有运行时分配，从而使「销毁 VM = 销毁内存池」成立。

本讲依赖 [u2-l1（VM 生命周期）](u2-l1-vm-lifecycle-api.md)。在那讲里我们提到 `njs_vm_destroy` 只销毁内存池，本讲就解释为什么这样足够。

## 2. 前置知识

- **malloc/free 的成本**：每次 `malloc` 都要查找空闲块、维护元数据；`free` 要合并相邻空闲块。高频分配小对象（如 JS 里频繁创建字符串、对象）时，这些开销会成为瓶颈，也容易产生内存碎片。
- **内存池（arena / pool allocator）**：一次性向系统申请一大块内存，之后的小分配都从这块里「切」。优点有三：分配近乎指针移动、碎片可控、释放时一次回收整块。代价是单个 `free` 通常不真正归还系统，而是标记复用。
- **红黑树（rbtree）**：一种自平衡二叉搜索树，查找/插入/删除都是 \(O(\log n)\)。njs 用它按「起始地址」排序管理大块内存，便于 `free` 时根据指针反查到所属块。
- **哈希表（hash table）与装载因子**：哈希表用 `hash(key) & mask` 把键分散到若干「桶」里；当元素数 / 桶数（装载因子）过高时冲突变多，需要「扩容」。
- **JS 对象属性 = 哈希表**：在 njs 里，一个 JS 对象的「自有属性」底层就是一张 `njs_flathsh_t`。理解这张表就理解了对象属性存储。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `src/njs_mp.h` | 内存池对外 API 声明：`njs_mp_create/alloc/zalloc/free/destroy/cleanup_add` 等原型与 `njs_mp_cleanup_t`/`njs_mp_stat_t` 结构。 |
| `src/njs_mp.c` | 内存池全部实现：`njs_mp_s` 主结构、三级分配、红黑树块管理、cleanup 链、销毁流程。 |
| `src/njs_flathsh.h` | 扁平哈希表对外 API：`njs_flathsh_t`、元素/描述符结构、`njs_flathsh_proto_t` 回调、find/insert/delete/each 原型。 |
| `src/njs_flathsh.c` | 扁平哈希表实现：单段内存布局、扩容（`njs_expand_elts`）/缩容（`njs_shrink_elts`）、增删查与遍历。 |
| `src/njs_vm.h` / `src/njs_vm.c` | VM 如何创建、持有、销毁 `mem_pool`，以及作为 flathsh 分配器的胶水函数 `njs_flathsh_proto_alloc/free`。 |
| `src/njs_object.c` | 典型应用：对象属性哈希使用的 `njs_object_hash_proto`，展示 proto 三回调如何落地。 |

## 4. 核心概念与源码讲解

### 4.1 内存池分配器 njs_mp_t

#### 4.1.1 概念说明

`njs_mp_t` 是 njs 的内存池。它的设计目标不是「通用分配器」，而是为「一个 VM 实例在生命周期内的大量中小对象分配」量身定制：

- **整体生命周期**：池随 VM 创建而建，随 VM 销毁而一次性释放。期间大部分分配不需要单独 `free`——销毁池就全部回收。这正是 `njs_vm_destroy` 只调一次 `njs_mp_destroy` 就够的原因。
- **分级分配**：小内存走「chunk」快路径（最快），中等的用整「页」，大块直接走系统 `memalign` 并登记到红黑树。三条路径在性能与碎片之间做权衡。
- **cleanup 逃生舱**：有些资源不是内存（如正则引擎上下文、外部句柄），无法靠销毁池回收。池提供一条 cleanup 处理器链，在销毁前依次回调，专门释放这类资源。

源码顶部注释把设计讲得很清楚：

[src/njs_mp.c:11-22](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_mp.c#L11-L22) —— 阐释「按 cluster 分配、cluster 切成 page、page 可整用或切成等大 chunk、大块在 cluster 之外分配、用按地址排序的红黑树记录所有块以便 free 时反查」。

#### 4.1.2 核心流程

内存池把可分配空间组织成三个层级：

```
cluster（簇）        一次性向系统申请的大块，大小 = cluster_size
   └── page（页）     cluster 按 page_size 切分，一个 cluster 最多 256 页
          └── chunk（块） 一页可整用，或切成若干等大的 chunk（最多 32 个，用 4 字节位图管理）
```

一次 `njs_mp_alloc(mp, size)` 的决策：

1. 若 `size <= page_size`：走小分配快路径 `njs_mp_alloc_small`。
   - 若 `size <= page_size/2`：在 chunk 槽位链表里找一个尺寸够用的槽（槽尺寸是 2 的幂），从对应页里切一个 chunk。
   - 否则：分配一整页。
2. 若 `size > page_size`：走大分配 `njs_mp_alloc_large`，直接向系统申请，并把元数据（`njs_mp_block_t`）登记进红黑树。

`free` 时，先用指针在红黑树里二分定位「属于哪个块」，再按块的类型（cluster / discrete / embedded）决定回收方式：cluster 块回到 chunk 位图，大块直接还给系统。

扩容/缩容由「cluster 用完就再申请一个 cluster」自动完成，chunk/page 级别的回收是惰性的。

数学上，chunk 槽尺寸是 2 的幂，最小为 `min_chunk_size`，最大不超过 `page_size`。一页最多 32 个 chunk，是因为位图 `map[4]` 共 32 比特（见 `njs_mp_page_t`）。簇里最多 256 页，是因为页号字段 `number` 是 `uint8_t`。

#### 4.1.3 源码精读

先看主结构 `njs_mp_s`，它持有红黑树（块索引）、空闲页队列、尺寸参数、cleanup 链以及可变长 `slots[]`（每种 chunk 尺寸一个槽）：

[src/njs_mp.c:97-112](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_mp.c#L97-L112) —— `struct njs_mp_s`：`blocks` 红黑树、`free_pages` 队列、页/簇尺寸、`cleanup` 链头、柔性数组 `slots[]`。

页描述符 `njs_mp_page_t` 用一个 4 字节位图管理至多 32 个 chunk 的占用：

[src/njs_mp.c:25-51](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_mp.c#L25-L51) —— 每页记录 chunk 尺寸、页号、空闲 chunk 计数、32 位 chunk 位图。

块的类型分三种，便于 `free` 时区分回收路径：

[src/njs_mp.c:54-79](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_mp.c#L54-L79) —— `NJS_MP_CLUSTER_BLOCK`（簇块）、`NJS_MP_DISCRETE_BLOCK`（大块元数据独立分配）、`NJS_MP_EMBEDDED_BLOCK`（大块元数据紧跟在分配数据之后）。红黑树节点 `node` + 类型 + 尺寸 + 起始地址。

**分配入口** `njs_mp_alloc` 只做一件事：按尺寸分流：

[src/njs_mp.c:312-326](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_mp.c#L312-L326) —— `size <= page_size` 走 `njs_mp_alloc_small`，否则走 `njs_mp_alloc_large`。注意 `NJS_DEBUG_MEMORY` 宏打开时，所有分配都退化为大分配（便于调试）。

小分配 `njs_mp_alloc_small` 是性能关键路径：先在槽位链表里找有空闲 chunk 的页，找到就切一个；找不到就申请一整页并初始化位图：

[src/njs_mp.c:402-475](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_mp.c#L402-L475) —— 找槽（`for (slot = mp->slots; slot->size < size; slot++)`）、从页位图里用 `njs_mp_alloc_chunk` 找空闲 chunk、满页移出链表。整页路径（`size > page_size/2`）则直接占一页。

大分配 `njs_mp_alloc_large` 把元数据登记进红黑树，并区分两种放置策略以省一次 `malloc`：

[src/njs_mp.c:582-636](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_mp.c#L582-L636) —— 尺寸恰为 2 的幂时元数据「离散」分配；否则元数据「嵌入」在数据尾部，只需一次 `memalign`。最后 `njs_rbtree_insert` 登记，便于 `free` 反查。

**free** 用指针在红黑树里定位块，再分流：

[src/njs_mp.c:682-719](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_mp.c#L682-L719) —— `njs_mp_find_block` 反查；cluster 块走 `njs_mp_chunk_free` 回收 chunk（含整页/整簇归零时归还系统的逻辑），大块直接 `njs_free`。指针不在任何块里会触发断言。

**销毁**是池式生命周期的精髓：先跑 cleanup 链，再遍历红黑树一次性释放所有块，最后释放池本身：

[src/njs_mp.c:251-285](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_mp.c#L251-L285) —— `njs_mp_destroy`：先 `for (c = mp->cleanup; ...)` 跑 cleanup 处理器，再 `njs_rbtree_destroy_next` 循环释放每个块的 `start` 数据与块元数据，最后 `njs_free(mp)`。

**cleanup 逃生舱**：`njs_mp_cleanup_add` 把一个处理器挂到链表头，`data` 可附带一块额外内存：

[src/njs_mp.c:651-679](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_mp.c#L651-L679) —— 分配 `njs_mp_cleanup_t`、可选分配 `data`、`handler` 置空待用户填写、头插进 `mp->cleanup`。这些 handler 会在 `njs_mp_destroy` 开头被调用，用于释放非内存资源。

#### 4.1.4 代码实践

**实践目标**：用源码阅读理解 chunk 位图如何分配一个 chunk，并验证「打开 `NJS_DEBUG_MEMORY` 后所有分配都走大块路径」。

**操作步骤**：

1. 打开 [src/njs_mp.c:478-513](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_mp.c#L478-L513) 的 `njs_mp_alloc_chunk`，跟着位图扫描逻辑走一遍：`mask = 0x80` 从最高位起，遇到 `map[n] & mask == 0` 即空闲，置位并返回该 chunk 在页内的偏移。
2. 对照 [src/njs_mp.c:115-120](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_mp.c#L115-L120) 的两个位运算宏 `njs_mp_chunk_is_free` / `njs_mp_chunk_set_free`，确认「位 = 1 表示占用」。
3. （可选，待本地验证）用 `./configure --cc-opt="-DNJS_DEBUG_MEMORY" && make njs` 构建一个调试版，再运行 `./build/njs -c 'var a = {}; a.x = 1'`。此时每次分配都进 `njs_mp_alloc_large`，配合 ASan 更易发现越界。

**需要观察的现象**：第 1 步应能看到「一页内 chunk 按 0,1,2… 顺序被占用，位图相应位置 1」。第 3 步若执行，行为应与正常构建一致（语义不变），只是内部路径不同。

**预期结果**：能用自己的话讲清「chunk 偏移 = 位图中第一个 0 比特的位置 × chunk 尺寸」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `njs_mp_create` 要求 `page_size`、`min_chunk_size`、`page_alignment` 都必须是 2 的幂？

**答案**：因为 chunk 槽尺寸、页内偏移计算都用位移（`chunk_size_shift`、`page_size_shift`）和位与（`& (page_size-1)`）来实现，只有 2 的幂才能用这些 O(1) 运算替代除法/取模。见 [src/njs_mp.c:153-171](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_mp.c#L153-L171) 的校验。

**练习 2**：`NJS_MP_EMBEDDED_BLOCK`（元数据嵌入）相比 `NJS_MP_DISCRETE_BLOCK`（元数据离散）有什么好处？为什么还要保留离散路径？

**答案**：嵌入路径把块元数据紧挨在分配数据之后，一次 `memalign` 同时拿到数据和元数据，省一次 `malloc`，对非 2 的幂的大块更高效。但当 `size` 恰为 2 的幂时，数据本身已自然对齐到尺寸边界，元数据若嵌入会破坏「按地址二分定位」的可预测性，故此时用离散路径把元数据单独分配。见 [src/njs_mp.c:603-627](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_mp.c#L603-L627)。

---

### 4.2 扁平哈希表 njs_flathsh_t

#### 4.2.1 概念说明

`njs_flathsh_t` 是 njs 自研的「扁平」哈希表，被用在三处关键位置：JS 对象的自有属性、Atom 表（见 [u2-l4](u2-l4-atom-table.md)）、模块表。它的名字「flat」来自一个核心特性：

- **整张表（桶数组 + 描述符 + 元素数组）被打包进一段连续内存**。这使得它「可整体搬迁」（relocatable）——扩容/缩容时直接申请新块、拷贝、释放旧块即可，不必逐节点移动。
- **保留插入顺序**：元素数组按插入先后排列，遍历（`njs_flathsh_each`）天然按插入序返回。这对 `for...in` / `Object.keys` 的确定性语义很重要。
- **动态扩缩**：元素满了按 3/2 因子扩容，删除产生空洞多了就缩容整理。
- **算法与数据解耦**：哈希表本身不知道「键长什么样、值怎么比较、内存从哪来」，这三件事交给 `njs_flathsh_proto_t` 的三个回调（`test` / `alloc` / `free`）。同一套表代码能服务于对象属性、Atom、模块等不同场景。

#### 4.2.2 核心流程

一段连续内存的布局如下（从低地址到高地址）：

```
[ HASH_CELLS 桶数组 | DESCRIPTOR 描述符 | ELEMENTS 元素数组 ]
   长度 = 2^k            hash_mask 等       每项一个 njs_flathsh_elt_t
```

桶里存的是「该桶链首元素在 ELEMENTS 数组里的下标」（1-based，0 表示空）。元素 `elt` 里存自己的 `key_hash`、`next_elt`（链上下一个元素下标）和 `value`（指向真正的值，如 `njs_object_prop_t`）。这是经典的「拉链法」哈希，只是链表用数组下标而非指针串联，因而可整体搬迁。

四个核心操作：

- **FIND**：`cell = key_hash & hash_mask` → 沿桶链遍历，命中条件是「`key_hash` 相等」且（若提供 `test` 回调）`test` 返回 `NJS_OK`。
- **INSERT**：若元素已存在，按 `replace` 标志决定替换还是返回 `NJS_DECLINED`；否则追加到 ELEMENTS 末尾并挂到桶链头。ELEMENTS 满则触发 `njs_expand_elts`。
- **DELETE**：标记元素为 `NJS_FREE_FLATHSH_ELEMENT`（留洞），`elts_deleted_count++`；删除达到阈值时 `njs_shrink_elts` 整理空洞。当表空了，整段内存释放、`slot` 置 NULL。
- **EACH**：按下标线性扫描，跳过已删除元素，按插入序返回。

扩容因子为 \(3/2\)，且桶数始终 ≥ 元素数，使平均装载因子 ≤ 1。具体地，扩容后新元素数：

\[
\text{new\_elts\_size} = \max\bigl(\text{elts\_count}+1,\ \lceil \text{elts\_size}\times \tfrac{3}{2}\rceil\bigr)
\]

桶数翻倍直到 ≥ 新元素数：

\[
\text{new\_hash\_size} = 2^{\,\lceil \log_2 \text{new\_elts\_size}\rceil}
\]

源码顶部注释把这整套设计写得非常完整，值得通读：

[src/njs_flathsh.c:8-68](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_flathsh.c#L8-L68)。

#### 4.2.3 源码精读

对外可见的句柄 `njs_flathsh_t` 只有一个 `slot` 指针（空表为 NULL），极其轻量：

[src/njs_flathsh.h:10-12](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_flathsh.h#L10-L12) —— 整张表对外就一个不透明指针，真正的桶/描述符/元素都挂在它指向的那段内存里。

元素结构 `njs_flathsh_elt_t`：把「链指针 + 属性描述符 + 哈希 + 值指针」压在一个紧凑结构里：

[src/njs_flathsh.h:15-28](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_flathsh.h#L15-L28) —— `next_elt`（26 位链指针）+ `type/writable/enumerable/configurable`（属性描述符位域）+ `key_hash` + `value[16/sizeof(void*)]`（值，恰好容纳一个 `njs_value_t`）。

描述符 `njs_flathsh_descr_t` 记录桶掩码、容量、已用数、已删数：

[src/njs_flathsh.h:31-36](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_flathsh.h#L31-L36)。

**解耦的关键**——`njs_flathsh_proto_t` 的三个回调：

[src/njs_flathsh.h:48-52](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_flathsh.h#L48-L52) —— `test`（键相等判断，可空）、`alloc`（分配内部内存）、`free`（释放内部内存）。哈希表代码不直接 `malloc`，而是经 [src/njs_flathsh.c:93-115](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_flathsh.c#L93-L115) 的 `njs_flathsh_malloc/free` 转调 `fhq->proto->alloc/free`，把分配器交给使用方决定（通常是 VM 的内存池）。

查询参数包 `njs_flathsh_query_t` 把「这次操作的键、哈希、proto、pool、replace」打包传递：

[src/njs_flathsh.h:55-67](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_flathsh.h#L55-L67)。

**FIND** 用拉链法沿桶链遍历：

[src/njs_flathsh.c:313-343](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_flathsh.c#L313-L343) —— `cell_num = key_hash & hash_mask`，取桶链首下标 `njs_hash_cells_end(h)[-cell_num-1]`，沿 `next_elt` 遍历，`key_hash` 相等且 `proto->test` 返回 `NJS_OK` 即命中。注意它用 `[-cell_num-1]` 这种「从描述符末端向前索引」的技巧同时定位桶数组和元素数组。

**INSERT** 与扩容：

[src/njs_flathsh.c:377-425](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_flathsh.c#L377-L425) —— 先查重（按 `replace` 决定替换或 `NJS_DECLINED`），无则 `njs_flathsh_add_elt` 追加。空表时调 `njs_flathsh_new` 建初始容量（桶 4、元素 2）。

[src/njs_flathsh.c:225-310](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_flathsh.c#L225-L310) —— `njs_expand_elts`：按 3/2 因子算新元素数，桶数翻倍到 ≥ 元素数；桶数变了就重建（重挂所有元素链），没变就只扩元素区。这就是「可整体搬迁」特性的用武之地。

**DELETE** 与缩容：

[src/njs_flathsh.c:540-604](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_flathsh.c#L540-L604) —— 标记 `NJS_FREE_FLATHSH_ELEMENT` 留洞；当 `elts_deleted_count` 同时 ≥ 8 且 ≥ `elts_count/2` 时调 `njs_shrink_elts` 整理；表空则整段释放、`slot = NULL`。

**unique 变体**：注意 [src/njs_flathsh.c:346-374](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_flathsh.c#L346-L374) 的 `njs_flathsh_unique_find` 只比较 `key_hash`、**不调用 `test`**。这正是对象属性场景的关键：属性键是唯一的 atom_id，`key_hash` 本身就是 atom_id，唯一性已由 atom 机制保证，无需 `test`。

#### 4.2.4 代码实践

**实践目标**：看清 `njs_flathsh_proto_t` 在「对象属性」场景里三回调各扮演什么角色。

**操作步骤**：

1. 打开 [src/njs_object.c:196-202](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_object.c#L196-L202) 的 `njs_object_hash_proto` 定义，看到 `test = NULL`、`alloc = njs_flathsh_proto_alloc`、`free = njs_flathsh_proto_free`。
2. 跟到 [src/njs_vm.c:1627-1638](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L1627-L1638)：`njs_flathsh_proto_alloc` 就是 `njs_mp_align(pool, NJS_MAX_ALIGNMENT, size)`，`njs_flathsh_proto_free` 就是 `njs_mp_free(pool, p)`。`data` 参数就是 `fhq.pool`，即 VM 的内存池。
3. 看 [src/njs_object.c:166-178](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_object.c#L166-L178)：对象注册属性时设 `fhq.proto = &njs_object_hash_proto`、`fhq.pool = vm->mem_pool`、`fhq.key_hash = atom_id`，然后调 `njs_flathsh_unique_insert`。

**需要观察的现象**：`test` 为 NULL 却不崩溃，因为对象属性用的是 `unique_insert/unique_find`（只比 `key_hash`），从不走 `test` 分支；`alloc`/`free` 把表的内部内存（桶+元素）全部记到 VM 内存池账上。

**预期结果**：能解释 proto 三回调在对象属性场景的角色——`test` 闲置（键唯一）、`alloc` 把表内存挂到 `mem_pool`、`free` 归还给 `mem_pool`（实际惰性回收，最终随池销毁）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `njs_flathsh_elt_t` 里用数组下标 `next_elt:26` 来串联链表，而不用指针？

**答案**：用下标而非指针，整张表就没有分散在各处的指针依赖，可以当作一段连续字节整体拷贝/搬迁，这正是「flat」与「relocatable」的由来；扩容/缩容时申请新块、`memcpy`、重挂链即可。代价是元素总数受 26 位限制（约 6700 万），对 njs 场景绰绰有余。见 [src/njs_flathsh.h:15-28](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_flathsh.h#L15-L28)。

**练习 2**：`njs_flathsh_delete` 为什么不立即整理空洞，而是先留洞、积累到阈值才 `njs_shrink_elts`？

**答案**：每次删除都整理是 \(O(n)\) 的，频繁删除时开销巨大。留洞（标记 `NJS_FREE_FLATHSH_ELEMENT`）让单次删除保持近似 \(O(1)\)，只有当空洞占比明显（≥ 8 且 ≥ 半数）才一次性整理摊销成本。这是典型的「惰性回收」权衡。见 [src/njs_flathsh.c:573-589](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_flathsh.c#L573-L589)。

---

### 4.3 VM 与内存池的归属

#### 4.3.1 概念说明

前两节分别讲了「池」和「哈希表」。本节把它们和 VM 缝合起来，回答一个贯穿 [u2-l1](u2-l1-vm-lifecycle-api.md) 的问题：为什么 `njs_vm_destroy` 只销毁内存池就够了？

核心设计：**`njs_vm_s.mem_pool` 是 VM 几乎所有运行时分配的唯一所有者**。VM 结构体本身、所有内建对象原型、`njs_function_t`、`njs_object_t` 及其属性哈希表内部内存、Atom 表、模块表、事件、`njs_arr_t` 动态数组……全部从 `mem_pool` 分配。因此销毁池 = 一次性释放这全部，无需逐个 `free`。

这带来三个好处：

1. **零泄漏**：只要池释放，运行时分配就一定全回收，不存在「忘了 free 某个对象」的泄漏。
2. **极简销毁**：`njs_vm_destroy` 一行代码（调 `njs_mp_destroy`）。
3. **隔离**：每个 VM 克隆（`njs_vm_clone`）有自己的私有池，请求间天然隔离——这在 NGINX 多请求场景下至关重要（见 [u8-l1](u8-l1-ngx-js-shared-layer.md)）。

唯一的例外是「非内存资源」（正则引擎上下文、外部句柄等），它们通过 cleanup 处理器链在销毁前释放。

#### 4.3.2 核心流程

```
njs_vm_create()
   ├─ njs_mp_fast_create(...)   建池 mp（cluster=2*pagesize, align=128, page=512, min_chunk=16）
   ├─ njs_mp_zalign(mp, ...)    从池里分配 vm 结构体本身
   ├─ vm->mem_pool = mp         把池登记为 VM 字段
   └─ 后续一切分配（原型/构造器/哈希表内部内存/事件/数组…）都走 vm->mem_pool

运行期
   └─ flathsh 扩容/对象属性写入 → proto.alloc(vm->mem_pool, …) → 记到池账上

njs_vm_destroy()
   └─ njs_mp_destroy(vm->mem_pool)
         ├─ 跑 cleanup 链（释放非内存资源）
         ├─ 遍历红黑树释放所有块
         └─ njs_free(mp)
```

访问入口是 `njs_vm_memory_pool(vm)`，返回 `vm->mem_pool`，供各子系统统一取池。

#### 4.3.3 源码精读

VM 创建时先建池，再把 VM 自己放进池里：

[src/njs_vm.c:39-49](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L39-L49) —— `njs_mp_fast_create(2*njs_pagesize(), 128, 512, 16)` 建池；`njs_mp_zalign` 从池分配并对齐 `njs_vm_t`；`vm->mem_pool = mp`。注意这里直接用 `njs_mp_fast_create`（跳过校验，因为参数是写死的安全值）。

`mem_pool` 字段在 VM 结构体里的位置：

[src/njs_vm.h:118-189](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L118-L189) —— `struct njs_vm_s`：可以看到同一个 VM 里并存多张 flathsh（`atom_hash`、`atom_hash_shared`、`values_hash`、`modules_hash`，见第 131-139 行），它们的内部内存都最终由 `mem_pool`（第 155 行）买单。

统一的取池访问器：

[src/njs_vm.c:771-775](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L771-L775) —— `njs_vm_memory_pool(vm)` 直接返回 `vm->mem_pool`，是各子系统拿到池的标准入口。

销毁极简：

[src/njs_vm.c:204-208](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L204-L208) —— `njs_vm_destroy` 只调 `njs_mp_destroy(vm->mem_pool)`。这就是「池式整体生命周期」落到代码上的形态。

#### 4.3.4 代码实践

**实践目标**：统计 `njs_vm_s` 中由 `mem_pool` 持有的资源，验证「销毁池即销毁 VM 全部运行时状态」。

**操作步骤**：

1. 在 [src/njs_vm.h:118-189](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.h#L118-L189) 的 `njs_vm_s` 里，逐字段标注哪些是「从 `mem_pool` 分配的堆资源」。明显属于池的有：`protos`（`njs_arr_t*`）、`scope_absolute`（`njs_arr_t*`）、`levels[]`（指针数组）、`top_frame`/`active_frame`（调用帧）、四张 `njs_flathsh_t` 的内部内存、`prototypes`/`constructors`（见 [src/njs_vm.c:586](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L586) 的 `njs_mp_alloc`）、`codes`（`njs_arr_t*`）。
2. 用 `git grep -n "vm->mem_pool" src/njs_vm.c` 看所有直接从池分配的位置（[src/njs_vm.c:158](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L158)、[166](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L166)、[253](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L253)、[363](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L363)、[586](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L586)、[668](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L668)、[677](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L677)、[923](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L923)、[1264](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L1264) 等）。
3. 区分「池内资源」与「非池资源」：`regex_generic_ctx`/`regex_compile_ctx`/`single_match_data` 是正则引擎上下文，不是纯内存——它们正是靠 cleanup 处理器回收的典型（`njs_mp_cleanup_add` 注册的 handler 在 `njs_mp_destroy` 开头被调用）。

**需要观察的现象**：绝大多数字段都最终可追溯到 `mem_pool`；只有少数外部引擎上下文需要 cleanup。

**预期结果**：列一张表，把 `njs_vm_s` 字段分成「池分配（随池销毁）」「cleanup 回收（外部资源）」「内联值（结构体自带，随结构体销毁）」三类。这能让你确信 `njs_vm_destroy` 的一行 `njs_mp_destroy` 不会泄漏。

#### 4.3.5 小练习与答案

**练习 1**：`njs_vm_clone`（克隆 VM，见 [u2-l1](u2-l1-vm-lifecycle-api.md)）会新建一个 `mem_pool`（[src/njs_vm.c:417](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L417) `nvm->mem_pool = nmp`）。为什么克隆出来的 VM 必须有独立池，而不能和模板 VM 共用？

**答案**：克隆 VM 是为了在「多请求」场景下复用编译产物（字节码、原型）却隔离运行时状态（调用帧、临时对象、属性写入）。如果共用池，一个请求的对象分配会和另一个请求混在一起，销毁一个就毁掉全部。独立池让每个请求的运行时分配各自回收，实现隔离。模板 VM 的 `shared`（共享只读内建）则是跨克隆复用的，不在私有池里。

**练习 2**：如果某个子系统真的需要「分配后立刻归还系统」的内存（不随池销毁），它能用 `njs_mp_free` 做到吗？

**答案**：能，但语义要清楚。`njs_mp_free` 会按块类型分流——大块（`NJS_MP_DISCRETE/EMBEDDED_BLOCK`）会立即 `njs_free` 归还系统；而 chunk/页级的小块只是标记为空闲、留在池里复用，并不立即归还系统。所以「立即归还系统」只对大块成立，小块是惰性复用。见 [src/njs_mp.c:694-710](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_mp.c#L694-L710)。

---

## 5. 综合实践

把本讲三个模块串起来，做一个「顺着一次对象属性写入，走完池与哈希表」的源码追踪任务。

**任务**：解释 JS 语句 `var o = {}; o.x = 1;` 在 njs 内核里涉及到的内存与哈希操作。按下面顺序追踪（全部是源码阅读，无需运行）：

1. **建 VM 与池**：从 [src/njs_vm.c:39-49](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L39-L49) 出发，说明 `{}` 字面量创建的 `njs_object_t` 从哪个池来（提示：对象结构体本身从 `vm->mem_pool` 分配，对象自带一个空的 `hash`）。
2. **属性写入触发哈希插入**：`o.x = 1` 走到 [src/njs_object_prop.c:111](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_object_prop.c#L111) 附近的 `njs_flathsh_unique_insert(njs_object_hash(object), &fhq)`。说明此刻 `fhq.proto`、`fhq.pool`、`fhq.key_hash` 各是什么。
3. **哈希内部从池拿内存**：因为对象 `hash` 初始为空，插入会调 `njs_flathsh_new`（[src/njs_flathsh.c:142-147](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_flathsh.c#L142-L147)）→ `njs_flathsh_alloc`（[src/njs_flathsh.c:159-186](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_flathsh.c#L159-L186)）→ `njs_flathsh_malloc`（[src/njs_flathsh.c:93-103](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_flathsh.c#L93-L103)）→ `proto->alloc(pool, size)` → [src/njs_vm.c:1627-1631](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L1627-L1631) 的 `njs_mp_align(vm->mem_pool, …)`。说明这段哈希表内存最终记在了池的哪个块上（一次小分配，进 chunk 或整页路径）。
4. **销毁**：VM 销毁时，[src/njs_vm.c:204-208](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L204-L208) 调 `njs_mp_destroy`，[src/njs_mp.c:251-285](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_mp.c#L251-L285) 一次性释放包括对象结构体、属性哈希表内部内存在内的全部块。

**产出**：画一张时序图或写一段文字，标出「池 → 哈希表 proto → 对象属性」三者的资金流向（谁向谁要内存）。关键结论：对象属性的哈希表内部内存由 proto 代理向 VM 内存池申请，因此整张表随池销毁而消失，无需对象自己实现析构。

> 待本地验证：若你想确认运行行为，构建 `./build/njs` 后执行 `./build/njs -c 'var o={}; o.x=1; console.log(o.x)'`，应输出 `1`；这验证了属性写入链路正常工作（但内部内存流向只能靠源码确认）。

## 6. 本讲小结

- **内存池 `njs_mp_t`** 用「cluster → page → chunk」三级策略分配：小块切 chunk（位图管理，至多 32/页）、中等占整页、大块走系统 `memalign` 并登记进按地址排序的红黑树，`free` 时用指针反查块。
- **池式整体生命周期**：`njs_mp_destroy` 先跑 cleanup 链（释放正则等非内存资源），再遍历红黑树释放所有块。绝大多数运行时分配无需单独 `free`，随池一次性回收。
- **扁平哈希表 `njs_flathsh_t`** 把「桶数组 + 描述符 + 元素数组」打包进一段连续内存，可整体搬迁、保留插入序、动态按 3/2 扩容、按空洞比例缩容；用数组下标而非指针串联拉链链表。
- **`njs_flathsh_proto_t` 三回调（test/alloc/free）** 把哈希算法与「键的语义」和「分配器」解耦；对象属性场景里 `test` 为 NULL（因为用 `unique_*` 变体，只比 atom_id），`alloc/free` 把表内部内存挂到 VM 内存池。
- **VM 是内存的唯一所有者**：`njs_vm_s.mem_pool` 持有几乎所有运行时分配，因此 `njs_vm_destroy` 仅一行 `njs_mp_destroy` 就能零泄漏回收；克隆 VM 拥有独立池以隔离多请求运行时状态。

## 7. 下一步学习建议

- 下一讲 [u2-l4 Atom 表](u2-l4-atom-table.md) 会讲 `njs_vm_s.atom_hash` / `atom_hash_shared` 这两张 flathsh 如何把字符串与符号驻留成 32 位 atom_id——你会看到本讲的哈希表与「`key_hash` 就是 atom_id、用 `unique_` 变体」的完整闭环。
- 进入第三单元（编译前端）前，建议回头再看 [src/njs_vm.c:39-49](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c#L39-L49)，记住「VM 结构体本身也在池里」这一事实，后续读到 `njs_mp_zalloc(vm->mem_pool, …)` 时就能立刻知道这块内存的归宿。
- 想深入池的实现，可继续读 [src/njs_mp.c:539-577](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_mp.c#L539-L577) 的 `njs_mp_alloc_cluster`（簇如何被切成页并挂入空闲队列）和 [src/njs_mp.c:750-856](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_mp.c#L750-L856) 的 `njs_mp_chunk_free`（整页/整簇归零时如何归还系统）。
