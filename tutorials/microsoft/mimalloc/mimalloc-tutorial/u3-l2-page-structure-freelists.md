# mi_page_t 深入：一个页的三条 free list

## 1. 本讲目标

上一讲（u3-l1）我们建立了 subproc → heap → theap → page queue → page → block 的五层所有权地图。本讲把放大镜对准其中最关键的一层——`mi_page_t`（mimalloc 页），读完本讲你应该能够：

1. 说出一个页内 `free`、`local_free`、`thread_free` 三条链表各自的分工，以及块在它们之间迁移的时机。
2. 推导 `used`、`capacity`、`reserved` 三个计数器之间的不变式，并能据此判断一个页处于「满 / 全空 / 可扩展 / 可立即分配」中的哪种状态。
3. 解释 `xthread_id` 低 2 位页标志的位编码技巧，以及 `xthread_free` 最低 1 位「所有权位」如何支撑无锁的跨线程释放。
4. 会用 debug 构建的统计输出间接验证这些不变式。

## 2. 前置知识

本讲需要上一讲的两个结论，先快速回顾：

- **页（page）与 OS 页的区别**：mimalloc 自己的页通常是 64KiB（小页）、512KiB（中页）或 4MiB（大页），一个页只装**一种尺寸**（size class）的块；OS 页只是操作系统 4KiB 粒度的内存页。mimalloc 源码注释里特意用 "OS page" 指前者、用 "page" 指后者（见 [include/mimalloc/types.h:L16-L19](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L16-L19)）。
- **本线程 / 跨线程**：每个页属于唯一一个 theap（线程本地堆），由该 theap 所属线程分配；但**释放可以来自任意线程**。本讲大量内容都是在回答「两种释放如何不打架」。

另外需要两个 C 语言背景概念：

- **侵入式单链表**：空闲块本身不额外分配链表结点，而是把块的头 8 个字节复用为 `next` 指针（`mi_block_t`）。
- **指针低位藏标志位**：块和页都对齐到至少 8 字节，指针的最低 2~3 位恒为 0，可以拿来存布尔标志而不丢失信息。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [include/mimalloc/types.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L425-L456) | `mi_page_s`、`mi_block_s` 结构体定义，三条链表与不变式的官方注释 |
| [src/page.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L146-L243) | 页管理核心：链表收割（collect）、扩容（extend）、退役（retire） |
| [include/mimalloc/internal.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L896-L922) | 页状态判定与位编码的内联辅助函数（`mi_page_all_free`、`mi_tf_*` 等） |
| [src/free.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L223-L249) | 释放入口：用 `xthread_id` 的 XOR 分流到本地/跨线程两条路径 |
| [src/alloc.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L32-L58) | 分配快路径：从 `free` 链表头部弹出一个块 |

本讲以 types.h 和 page.c 为主，internal.h 是二者的「粘合剂」，free.c / alloc.c 只取与页结构直接相关的片段（完整释放/分配链路是 u4、u5 两个单元的主题）。

## 4. 核心概念与源码讲解

### 4.1 页与块：mi_block_t 与 mi_page_s 的骨架

#### 4.1.1 概念说明

一个页是一块连续内存，被切成 \( N \) 个等大的块（block），块尺寸记为 `block_size`。用户拿到的指针就是某个块的起始地址。块只有两种状态：

- **被占用**：内容归用户所有，mimalloc 不碰它；
- **空闲**：块的头 8 个字节被复用为指向下一个空闲块的指针——这就是 `mi_block_t` 的全部：

```c
// free lists contain blocks
typedef struct mi_block_s {
  mi_encoded_t next;
} mi_block_t;
```

见 [include/mimalloc/types.h:L365-L368](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L365-L368)。`next` 的类型是 `mi_encoded_t`（即 `uintptr_t`）而不是指针：在 debug/secure 构建下它会与页内随机 key 异或编码（`MI_ENCODE_FREELIST`），用于检测 free list 被写坏，编码逻辑见 [include/mimalloc/internal.h:L1258-L1266](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1258-L1266)。本讲先把它当成普通 `next` 指针理解。

#### 4.1.2 核心流程

第 \( i \) 个块的地址是纯算术出来的，**不需要任何链表**：

\[ \text{block}_i = \text{page\_start} + \text{page\_offset} + i \times \text{block\_size} \]

一个 64KiB 小页装 64 字节的块时，\( \text{reserved} \) 最多约 \( 65536 / 64 = 1024 \) 个。`capacity` 和 `reserved` 都是 `uint16_t`，所以每页块数不能超过 65535——`_mi_page_init` 里有对应断言（[src/page.c:L718](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L718)：`page_size / block_size < (1L<<16)`）。

#### 4.1.3 源码精读

`mi_page_s` 全貌（注释省略，64 位下约一个半缓存行）：

```c
typedef struct mi_page_s {
  #if (MI_PAGE_META_IS_ALIGNED)
  _Atomic(struct mi_page_s*) self;             // 指向真实页元信息（跨多个 slice 的页用）
  #endif
  _Atomic(mi_threadid_t)    xthread_id;        // 拥有线程 id | 页标志（低 2 位）
  mi_block_t*               free;              // 可直接分配的空闲块链表
  mi_used_t                 used;              // 已分配块数（含挂在 thread_free 上的）
  mi_block_t*               local_free;        // 本线程延迟释放的块（未对 malloc 可见）

  size_t                    block_size;        // const: 每块的可用字节数
  size_t                    page_offset;       // const: 页起点到第一个块的偏移
  uint16_t                  capacity;          // 已初始化（串上链表）的块数
  uint16_t                  reserved;          // 页内存总共容纳的块数
  uint16_t                  slice_pcommitted;  // 已 commit 的 OS 页数（0 表示全 commit）
  uint8_t                   retire_expire;     // 退役倒计时
  bool                      free_is_zero;      // 空闲块是否保证为零

  // next cache line
  _Atomic(mi_thread_free_t) xthread_free;      // 跨线程释放链表 | 所有权位（最低 1 位）
  mi_theap_t*               theap;             // 拥有此页的 theap（abandoned 页可能为 NULL）
  mi_heap_t*                heap;              // const: 拥有此页的 heap
  struct mi_page_s*         next;              // 同 bin 页队列的双链
  struct mi_page_s*         prev;
  mi_memid_t                memid;             // const: 页内存的「产地证」
  ...
} mi_page_t;
```

定义见 [include/mimalloc/types.h:L425-L456](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L425-L456)。三个值得注意的布局细节：

1. 结构体里源码注释写着 `// next cache line`（[types.h:L442](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L442)）：第一行放 `xthread_id`/`free`/`used`/`local_free` 等 malloc/free 快路径要摸的字段，`xthread_free`/`theap`/`heap` 挤到下一行。文件头注释明说「The layout below is optimized for `free.c:mi_free` and `alloc.c:mi_page_alloc`」（[types.h:L423](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L423)）。
2. 带 `_Atomic` 前缀的字段（`xthread_id`、`xthread_free`、`self`）是**可能被其他线程读写**的字段——这正是 u3-l1 说的「原子字段分布即共享层与线程私有层的分界」在页级别的体现：其余字段只有拥有线程在持有所有权时才能碰。
3. 块地址的计算函数就一行乘法，见 [src/page.c:L34-L39](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L34-L39) 的 `mi_page_block_at`。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：建立「页 = 定长块数组」的直觉，算出一个页能装多少块。
2. **操作步骤**：
   - 读 [types.h:L425-L456](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L425-L456)，把字段按「malloc 快路径要读的 / free 快路径要读的 / 只有慢路径才碰的」分三类。
   - 手算三组数：64KiB 页装 16B、64B、1KiB 块时 `reserved` 的近似值。
3. **需要观察的现象**：`reserved` 随块尺寸增大而减小，且 `uint16_t` 的上限意味着极小块只会出现在小页里。
4. **预期结果**：约 4096 / 1024 / 64（忽略 `page_offset` 与对齐的少量损耗）。精确值待本地验证（可在 debug 构建里用 `mi_good_size` 与统计输出交叉验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `next` 指针可以直接放在空闲块里，而不用担心被用户改写？

**答案**：块空闲期间内容本来就无人使用；块被分配出去的那一刻，分配器把 `next` 清零（[src/alloc.c:L55](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L55) 注释 `don't leak internal data`），此后整个块归用户。反之，用户 free 之后块回到链表，头部重新变回 `next`——所以 use-after-free 的写操作会破坏链表，debug 构建的编码 free list 正是为了抓住这种情况。

**练习 2**：`mi_page_t` 里 `heap` 字段标注了 `const`，`theap` 却没有。为什么？

**答案**：页自诞生到销毁始终属于同一个 heap（一等堆身份不变），所以 `heap` 是常量；而 `theap` 会变——线程退出时页可能被 abandon（`theap` 视角失效），之后又被别的 theap 认领复用（见 [src/page.c:L291-L304](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L291-L304) 的 `_mi_page_abandon` 与 [src/page.c:L277-L289](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L277-L289) 的 `_mi_theap_page_reclaim`）。

### 4.2 三条 free list：free、local_free、thread_free 的分工与迁移

#### 4.2.1 概念说明

types.h 里对三条链表有一段权威注释（[types.h:L398-L413](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L398-L413)），翻译过来是：

| 链表 | 谁往里放 | 谁能取 | 语义 |
| --- | --- | --- | --- |
| `free` | 扩容初始化、迁移 | **malloc 快路径** | 立即可分配的块 |
| `local_free` | 拥有线程自己的 free | 只有迁移 | 本线程释放、但**故意**不立即对 malloc 可见 |
| `xthread_free` | 其他线程的 free | 只有拥有线程收割（collect） | 跨线程释放的块暂存区 |

为什么搞三条而不是一条？注释给了两个理由：

1. **`local_free` 独立出来是为了实现单调心跳（monotonic heartbeat）**：本线程释放的块不立即回到 `free`，`free` 耗尽是确定性的周期事件，Koka/Lean 这类运行时系统借此获得「每分配固定数量就回调一次」的延迟回收钩子（`mi_register_deferred_free`，u5-l3 详讲）。
2. **`xthread_free` 独立出来是为了让拥有线程分配时完全不需要原子操作**：跨线程释放的块堆在旁边，不打扰拥有线程的快路径；收割时机由拥有线程自己决定。

这就是 readme 里「free list sharding + multi-sharding」思想在数据结构上的落地。

#### 4.2.2 核心流程

块在三条链表间的迁移是个单向环流：

```text
            扩容初始化(mi_page_free_list_extend)
                 ┌──────────────────────────┐
                 ▼                          │
   ┌───────   free   ───────┐              │
   │  弹出=malloc 快路径      │              │
   │ (alloc.c:52-57)        │              │
   ▼                        │              │
 用户占用 (alive)            │              │
   │                        │              │
   │ 本线程 free             │ 迁移① 头搬(O(1))│
   │ (free.c:44-48)         │ mi_page_free_quick_collect
   ▼                        │ (page.c:203-212)
 local_free ────────────────┘
   ▲
   │ 迁移② 原子交换整条链 + 计数
   │ mi_page_thread_free_collect (page.c:185-201)
   │
 xthread_free ◄──── 其他线程 free，一次 CAS 头插
                   (free.c:80-87)
```

关键点：**malloc 永远只从 `free` 弹**；`free == NULL` 时才进入慢路径，慢路径先做迁移再决定要不要新页。

#### 4.2.3 源码精读

**① 分配：弹出即计数**（[src/alloc.c:L41-L57](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L41-L57)）——读 `free` 头、写入 `page->free = next`、`used+1`，三次内存访问完成分配：

```c
  mi_block_t* const block = page->free;
  ...
  if (block == NULL) {
    return _mi_malloc_generic(theap, size, ...);   // free 耗尽 → 慢路径
  }
  mi_block_t* next = mi_block_next(page,block);
  block->next = 0;
  page->free = next;
  page->used = used+1;
```

**② 本线程释放：头插 `local_free`，`used-1`**（[src/free.c:L44-L48](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L44-L48)）——纯普通读写，零原子操作：

```c
  const mi_used_t used = page->used - 1;
  mi_block_set_next(page, block, page->local_free);
  page->used = used;
  page->local_free = block;
```

**③ 跨线程释放：一次 CAS 头插 `xthread_free`**（[src/free.c:L80-L87](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L80-L87)）——注意**没有碰 `used`**：

```c
  mi_thread_free_t tf_old = mi_atomic_load_relaxed(&page->xthread_free);
  do {
    mi_block_set_next(page, block, mi_tf_block(tf_old));
    ...
    tf_new = mi_tf_create(block, new_owned);
  } while (!mi_atomic_cas_weak_acq_rel(&page->xthread_free, &tf_old, tf_new));
```

**④ 收割 `xthread_free`：原子交换整条链**（[src/page.c:L185-L201](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L185-L201)）——一次 CAS 把整条链摘下来，低位标志保留：

```c
  mi_thread_free_t tfree = mi_atomic_load_relaxed(&page->xthread_free);
  do {
    head = mi_tf_block(tfree);
    if (head == NULL) return;                          // 空链直接走
    tfreex = mi_tf_create(NULL, mi_tf_is_owned(tfree)); // 置空，保留所有权位
  } while (!mi_atomic_cas_weak_acq_rel(&page->xthread_free, &tfree, tfreex));
```

摘下来之后由 [src/page.c:L150-L183](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L150-L183) 的 `mi_page_thread_collect_to_local` 接手：**遍历一遍数出块数**（顺带做完整性检查，链长超过 `capacity` 判定 corruption），挂到 `local_free` 头上，再把 `used` 一次性减掉这个数。

**⑤ `local_free` → `free`：O(1) 头搬**（[src/page.c:L203-L212](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L203-L212)）——这就是 `free` 耗尽时的快速补救，慢路径一进来先试它（[src/page.c:L879-L901](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L879-L901) 的 `mi_page_queue_lookup_free_first`）：

```c
  if (page->free != NULL) return true;
  if (page->local_free == NULL) return false;
  page->free = page->local_free;      // 整条链直接搬，O(1)
  page->local_free = NULL;
```

完整的统一入口是 [src/page.c:L214-L243](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L214-L243) 的 `_mi_page_free_collect`：先 ④ 后 ⑤；`force=true` 时（仅退出阶段）才做 O(n) 的两条链拼接。另有一个跨线程视角的优化版 [src/page.c:L251-L269](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L251-L269) `_mi_page_free_collect_partly`，供 free.c 在不碰原子字段的情况下收割链表尾部。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：把「事件 → 链表与计数变化」的对应关系背下来，这是读懂后续所有分配/释放代码的钥匙。
2. **操作步骤**：对着上面五个代码点，填写下面的事件表（每行写出 `free`/`local_free`/`xthread_free`/`used` 的变化）：

| 事件 | 代码位置 | free | local_free | xthread_free | used |
| --- | --- | --- | --- | --- | --- |
| malloc 弹出一块 | alloc.c:52-57 | 长度-1 | 不变 | 不变 |  |
| 本线程 free | free.c:44-48 | | | | |
| 跨线程 free | free.c:80-87 | | | | |
| collect（thread→local） | page.c:150-183 | | | | |
| quick collect（local→free） | page.c:203-212 | | | | |
| extend（扩容初始化） | page.c:589-612 | | | | capacity+extend |

3. **需要观察的现象**：只有「malloc」和「extend」两类事件触碰分配可用性；只有「本线程 free」和「collect」改变 `used`；「跨线程 free」什么都不改（除了链表本身）。
4. **预期结果**：填完后 used 列依次为 `+1 / -1 / 0 / -count / 0 / 0`；extend 行的 free 列为 `+extend`。这正是 4.3 节不变式的逐事件证明。

#### 4.2.5 小练习与答案

**练习 1**：为什么本线程 free 不直接推入 `free` 链表，而要多绕一道 `local_free`？

**答案**：注释（[types.h:L403-L406](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L403-L406)）给出两个理由：(a) `local_free` 与 `free` 分离让「`free` 耗尽」成为确定性事件，从而支撑单调心跳与延迟释放回调（见 [src/page.c:L979-L1004](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L979-L1004) 的 `_mi_deferred_free`，每次慢路径都会推进 `heartbeat`）；(b) 释放的块不立即复用，也让「页整体变空」更容易发生，配合 eager page purging 把内存还给 OS。

**练习 2**：`mi_page_thread_collect_to_local` 为什么要辛苦地遍历整条链数块数，而不是维护一个原子计数器？

**答案**：见 [src/page.c:L154-L155](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L154-L155) 的注释「also to get a proper use count (without data races)」。若每次跨线程 free 都原子递增计数器，快路径上就多一次原子写竞争；改成收割时一次性数，原子操作从 O(free 次数) 降到 O(collect 次数)。遍历还顺带做了 corruption 检查（链长 > capacity 或 > used 都报 EFAULT，[page.c:L165-L173](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L165-L173)）。

### 4.3 used / capacity / reserved：计数不变式与页状态判定

#### 4.3.1 概念说明

三个 `uint16_t`/`size_t` 计数器刻画页的「水位」：

- **`reserved`**：这个页的内存**总共**能容纳多少块（常量，由页大小和 `block_size` 决定）。
- **`capacity`**：已经**初始化**（被串上 free list 至少一次）的块数。mimalloc 不在建页时一口气初始化全部块，而是分批 extend，避免触碰太多内存（少 commit、少缺页）。
- **`used`**：当前「已分配出去」口径的计数——注意它**包含**已被跨线程释放、还挂在 `xthread_free` 上没收割的块。

三者恒有 \( 0 \le \text{used} \le \text{capacity} \le \text{reserved} \)，这也是 debug 断言直接检查的内容（[src/page.c:L86-L87](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L86-L87)）。

#### 4.3.2 核心流程：守恒式

记 \( |\text{free}| \)、\( |\text{local\_free}| \)、\( |\text{thread\_free}| \) 为三条链表当前长度，alive 为真实存活的块数。types.h 注释（[types.h:L408](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L408)）给出第一条：

\[ \text{alive} = \text{used} - |\text{thread\_free}| \quad(\text{used 包含未收割的跨线程释放块}) \]

运行时真正被断言检查的是第二条（[src/page.c:L116-L117](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L116-L117)，debug 构建下 `mi_page_is_valid_init` 会遍历链表数出 free_count 再核对）：

\[ \text{capacity} = \text{used} + |\text{free}| + |\text{local\_free}| \]

两条联立，得到完整守恒式——每个已初始化的块恰好处于四种状态之一：

\[ \text{capacity} = \text{alive} + |\text{free}| + |\text{local\_free}| + |\text{thread\_free}| \]

> **读注释的一个陷阱**：types.h 第 409 行注释字面写作 `used - |thread_free| + |free| + |local_free| == capacity`，与上面第二条联立时会差一个 \( |\text{thread\_free}| \)。对照 page.c:116 的运行时断言可知，该注释行在 `xthread_free` 已被收割（长度为 0）时才严格成立；阅读时以断言与守恒式为准。这也提醒我们：**注释是入口，断言才是契约**。

用 4.2.4 的事件表可以逐事件验证守恒式不被破坏（每类事件对 `used + |free| + |local_free|` 的净影响为 0，只有 extend 让 `capacity` 与该和同增量增长）。

**由不变式导出的页状态判定**（全部在 [include/mimalloc/internal.h:L896-L922](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L896-L922)）：

| 判定函数 | 条件 | 含义 |
| --- | --- | --- |
| `mi_page_all_free` | `used == 0` | 全空（需先 collect 才准，见函数上方注释） |
| `mi_page_immediate_available` | `free != NULL` | 有立即可分配的块 |
| `mi_page_is_expandable` | `capacity < reserved` | 还能 extend 出新块 |
| `mi_page_is_full` | `reserved == used` | 满：连保留区都用完了 |

注意「满」的比较对象是 `reserved` 而不是 `capacity`：`free`/`local_free` 全空但 `capacity < reserved` 的页不算满，它只是**暂时无货**，下次分配走 extend（见 4.3.3 的 `mi_page_extend_free`）。

#### 4.3.3 源码精读

**建页：从 0 到第一批块**。[src/page.c:L709-L758](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L709-L758) 的 `_mi_page_init` 断言新页处于「全零」状态后，调用 `mi_page_extend_free` 初始化第一批块。注意其中一条断言很能说明问题：

```c
  mi_assert_internal(page->xthread_free == 1);   // 新页天生「被拥有」且链表为空
```

（[src/page.c:L741](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L741)——`1` 不是长度，是所有权位，4.4 节解释。）

**扩容：分批初始化**。[src/page.c:L630-L706](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L630-L706) 的 `mi_page_extend_free` 决定这次初始化多少块：

```c
  size_t extend = (size_t)page->reserved - page->capacity;
  size_t max_extend = (bsize >= MI_MAX_EXTEND_SIZE ? MI_MIN_EXTEND : MI_MAX_EXTEND_SIZE/bsize);
  ...
  if (extend > max_extend) {
    extend = max_extend;   // 一次最多初始化约 8KiB 的量，避免触碰太多内存
  }
```

`MI_MAX_EXTEND_SIZE` 为 8KiB（[page.c:L618](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L618)），注释明说这是为了减少 commit 与 RSS（`Going from 1 to 8 increases rss by 50%` 的实验记录）。随后 [page.c:L589-L612](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L589-L612) 的 `mi_page_free_list_extend` 把第 `capacity` 到 `capacity+extend-1` 号块按地址顺序串成链表头插进 `free`，`capacity += extend`。secure 模式（MI_SECURE>=2）则改用随机穿插版本（[page.c:L533-L587](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L533-L587)）。

**变空：retire 而非立刻归还**。本线程释放把 `used` 减到 0 时（[src/free.c:L49-L53](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L49-L53)）触发 [src/page.c:L424-L457](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L424-L457) 的 `_mi_page_retire`：小尺寸页通常不马上释放，而是设 `retire_expire = 16`（`MI_RETIRE_CYCLES`，[page.c:L414-L415](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L414-L415)），留在队列里等 16 个「管理周期」，防止马上又来同尺寸分配。

**慢路径里如何用这些判定**。[src/page.c:L766-L876](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L766-L876) 的 `mi_page_queue_find_free_ex` 扫描同 bin 页队列：先 `_mi_page_free_collect` 刷新状态，`immediate_available` 则作为候选；「完全满且不可扩展」的页移入 full 队列（`mi_page_to_full`）；全空候选页直接 `_mi_page_free` 释放；最后对选中页做 extend——整段代码就是上表四个判定函数的编排。

#### 4.3.4 代码实践（本讲主实践）

**推导题**：当 \( \text{used} - |\text{thread\_free}| = \text{capacity} \) 时，页面处于什么状态？

**推导**：该式左边就是 alive。alive == capacity 意味着每个已初始化的块都被占用。再联立守恒式 capacity = used + |free| + |local_free|，得 used == capacity，于是 \( |\text{free}| = |\text{local\_free}| = 0 \)，进而 \( |\text{thread\_free}| = \text{used} - \text{alive} = 0 \)——**三条链表全空**。此时分两种情况：

- `capacity < reserved`：页「无货但没满」，是 expandable，下次同尺寸分配触发 `mi_page_extend_free`；
- `capacity == reserved`：`used == reserved`，`mi_page_is_full` 为真，页在慢路径里被移入 full 队列或 abandon（[page.c:L374-L389](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L374-L389)）。

一句话：**该条件 ⇔ 页被塞满活对象、无任何空闲块**。

**运行验证**（debug 构建）：

1. **实践目标**：用统计输出间接验证「分配推高 used、本地释放推低 used、used==0 触发 retire」的计数行为。
2. **操作步骤**：
   - 构建 debug 版（MI_STAT=2，计数器最全）：
     ```bash
     cmake -B out/debug -DCMAKE_BUILD_TYPE=Debug && cmake --build out/debug -j
     ```
   - 编写 `pagefreelist_demo.c`（**示例代码**，非项目原有文件）：
     ```c
     #include <stdio.h>
     #include <mimalloc.h>

     int main(void) {
       enum { N = 1000 };
       void* p[N];
       printf("block size for a 64B request: %zu\n", mi_good_size(64));
       for (int i = 0; i < N; i++) p[i] = mi_malloc(64);
       mi_stats_print_out(NULL, NULL);            /* 窗口1：1000 块全部存活 */
       for (int i = 0; i < N; i++) mi_free(p[i]);
       mi_stats_print_out(NULL, NULL);            /* 窗口2：全部本地释放 */
       return 0;
     }
     ```
   - 编译运行（静态库优先，避免 SO 版本号差异）：
     ```bash
     gcc -Iinclude -o demo pagefreelist_demo.c out/debug/libmimalloc-debug.a -pthread
     MIMALLOC_SHOW_STATS=1 ./demo
     ```
3. **需要观察的现象**：两次 `mi_stats_print_out` 的差异——对应 bin 行（S 段、block 列等于 `mi_good_size(64)` 的输出）的 current；pages 段的 `extended` 与 `retire` 计数变化。
4. **预期结果**：窗口 1 该 bin current ≈ 1000（全存活、thread_free 为空，alive == used）；窗口 2 current == 0 且行尾无 `not all freed`；`extended` ≥ 1（1000 块按约 8KiB 一批初始化，64B 块每批约 128 块，约需 8 次 extend）；`retire` 计数在窗口 2 增加（最后一次 free 使 used==0，触发 `_mi_page_retire`）。页内部字段无法从公共 API 读取，上述数值是间接验证；具体计数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`mi_page_all_free` 只看 `used == 0`，为什么函数注释特意提醒「needs up-to-date used count」？

**答案**：跨线程 free 不减 `used`（见 4.2.3 ③），所以其它线程可能已经把页里所有块都释放了，`used` 却还大于 0。必须先 `_mi_page_free_collect` 收割 `xthread_free`（收割会把 count 从 `used` 里扣掉，[page.c:L182](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L182)），`used == 0` 才真实代表全空。abandon/释放页的入口（[page.c:L291-L292](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L291-L292)）都是先 collect 再判断，就是这个原因。

**练习 2**：一个 64KiB 页、`block_size = 64`，`reserved = 1024`。已分配 900 块、本线程释放 300 块、其它线程释放 100 块（未收割）。写出各计数与链表长度。

**答案**：alive = 900 - 300 - 100 = 500；used = 900 - 300 = 600（跨线程的 100 块仍计入）；由守恒式 capacity = used + |free| + |local_free|，且 |local_free| = 300、|thread_free| = 100：若 capacity 已扩满 1024，则 |free| = 1024 - 600 - 300 = 124；验证完整守恒式 500 + 124 + 300 + 100 = 1024 ✓。此时页未满（used=600 < reserved=1024）。

**练习 3**：为什么 `mi_page_is_full` 用 `reserved == used` 而不是 `capacity == used`？

**答案**：`capacity == used` 只说明「当前初始化的块都活着」，页可能还能 extend 出新块（`capacity < reserved`），把它当「满」会白白放弃保留内存、去开新页，增加碎片。真正的「满」是连保留区都无法再提供新块，即 `reserved == used`。而由于 used ≤ capacity ≤ reserved，两者只在 capacity == reserved 时同时成立。

### 4.4 位技巧：xthread_id 的低 2 位页标志与 xthread_free 的所有权位

#### 4.4.1 概念说明

`mi_free` 的第一件事是回答：「这个指针所在的页，是不是我自己线程的页？」朴素做法要读 `xthread_id`、抹掉标志位、再和当前线程 id 比较——两次移位/掩码加一次比较。mimalloc 的做法是把**页标志直接塞进 `xthread_id` 的低 2 位**，然后用**一次异或**完成全部判定。

三个常量（[types.h:L371-L378](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L371-L378)）：

```c
#define MI_PAGE_IN_FULL_QUEUE           MI_ZU(0x01)  // 页在 full 队列里
#define MI_PAGE_HAS_INTERIOR_POINTERS   MI_ZU(0x02)  // 页内有带偏移的对齐块
#define MI_PAGE_FLAG_MASK               MI_ZU(0x03)  // 低 2 位都是标志
```

线程 id 本身从 bit 2 开始（线程 id 是 4 的倍数，`mi_page_set_theap` 里有断言，[internal.h:L1012](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1012)）。两个特殊「线程 id」也借此编码（[types.h:L380-L386](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L380-L386)）：0 = abandoned（无主）、4 = abandoned-mapped（无主但登记在 arena 的 abandoned 位图里）、8 = detached。`mi_page_is_abandoned` 就是判 `thread_id <= 4`（[internal.h:L1022-L1025](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1022-L1025)）。

`xthread_free` 则用**最低 1 位**存「所有权」（ownership，[types.h:L388-L393](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L388-L393) 的 `mi_thread_free_t` 注释）：

\[ \text{xthread\_free} = \text{链表头指针} \,|\, (\text{owned} ? 1 : 0) \]

owned=1 表示当前有线程拥有该页、可以安全读写页内**非原子**字段。妙处在于：一个跨线程 free 在 CAS 推块入链的同时，可以顺手把所有权位从 0 翻成 1——**释放一个块与认领一个无主页，合并成一次原子操作**。

#### 4.4.2 核心流程

`mi_free` 快路径的 XOR 分流（四个分支覆盖全部情况）：

```text
ptid = page->xthread_id                    // 一次原子读：线程id | 页标志
xtid = 当前线程id ^ ptid                   // 一次异或

xtid == 0            → 本线程页 + 无标志   → mi_free_block_local    (最快)
xtid <= 3 (1 或 2)   → 本线程页 + 有标志   → mi_free_generic_local
(xtid & 3) == 0      → 他线程页 + 无标志   → mi_free_block_mt       (一次 CAS)
else                 → 他线程页 + 有标志   → mi_free_generic_mt
```

原理：两个 4 的倍数异或结果仍是 4 的倍数（低 2 位为 0）；「是否同一线程」看高位是否全同，「有无标志」看低位。四种组合恰好被上面四个条件一刀切开。

所有权位的生命周期：

```text
建页:  xthread_free = 1            (空链 + owned，见 page.c:741 断言)
线程退出 abandon: owned 位清 0     (页变成无主，只能被原子访问)
跨线程 free 无主页: CAS 推块 + 置 owned=1  → 调用者顺手成为新主人
拥有者收割: 原子交换时保留 owned 位      (page.c:195)
```

#### 4.4.3 源码精读

**XOR 分流的真身**（[src/free.c:L223-L249](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L223-L249)）：

```c
  const mi_threadid_t ptid = mi_page_xthread_id(page);
  const mi_threadid_t xtid = (_mi_prim_thread_id() ^ ptid);
  if mi_likely(xtid == 0) {                        // 本线程 + 无标志
    mi_free_block_local(page, block, ...);
  }
  else if (xtid <= MI_PAGE_FLAG_MASK) {            // 本线程 + 有标志
    mi_free_generic_local(page, p);
  }
  else if ((xtid & MI_PAGE_FLAG_MASK) == 0) {      // 他线程 + 无标志
    mi_free_block_mt(page, block, ...);
  }
  else {                                           // 他线程 + 有标志
    mi_free_generic_mt(page, p, allow_collect);
  }
```

**线程 id 与标志的存取**（[include/mimalloc/internal.h:L960-L979](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L960-L979)）——取真线程 id 是 `& ~MI_PAGE_FLAG_MASK`，设标志用原子 OR/AND（别的线程可能在 `alloc-aligned.c` 里并发设 `MI_PAGE_HAS_INTERIOR_POINTERS`，所以 `mi_page_set_theap` 用 CAS 合并新旧标志，见 [internal.h:L1014-L1019](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1014-L1019) 的注释）。

**所有权位的三个操作**（[include/mimalloc/internal.h:L1089-L1119](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1089-L1119)）：

```c
static inline mi_block_t* mi_tf_block(mi_thread_free_t tf)      { return (mi_block_t*)(tf & ~1); }
static inline bool mi_tf_is_owned(mi_thread_free_t tf)          { return ((tf & 1) == 1); }
static inline mi_thread_free_t mi_tf_create(mi_block_t* b, bool owned) {
  return (mi_thread_free_t)((uintptr_t)b | (owned ? 1 : 0));
}
// 认领所有权：一次原子 OR，返回是否原本无主
static inline bool mi_page_claim_ownership(mi_page_t* page) {
  const uintptr_t old = mi_atomic_or_acq_rel(&page->xthread_free, (uintptr_t)1);
  return ((old&1)==0);
}
```

**认领与释放的合并**已在 4.2.3 ③ 看到：`mi_free_block_mt` 的 CAS 循环里，若 `allow_collect` 且原值无主，则新值直接置 owned=1（[src/free.c:L85-L95](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L85-L95)），随后调用者以新主人身份尝试收割整页——这是 v3「free 时顺手 reclaim 无主页」机制的原子基础（完整 reclaim 流程在 u6-l4）。

> 小心一处注释过时：[types.h:L422](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L422) 写 "page flags are in the bottom 3 bits"，而实际 `MI_PAGE_FLAG_BITS == 2`，bit 2 起是线程 id / 特殊 id 的区分位。以宏定义为准。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：吃透 XOR 分流，为 u5-l1 精读 free 快路径做铺垫。
2. **操作步骤**：在编辑器里打开 [src/free.c:L223-L249](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L223-L249)，给四个分支各写一行中文注释，说明「谁拥有页 / 有无标志 / 走哪条链」。然后用 ptid=12（线程 12，无标志）、ptid=13（线程 12，在 full 队列）、ptid=0（abandoned）三组值，手算当前线程 id=16 时的 xtid 并确定分支。
3. **需要观察的现象**：abandoned 页（ptid=0）永远走第三或第四分支——即被当作「他线程页」处理，这正是无主页只能通过原子路径被触碰的保证。
4. **预期结果**：三组 xtid 分别为 16^12=28（&3==0 → `mi_free_block_mt`）、16^13=29（&3!=0 → generic_mt）、16^0=16（&3==0 → `mi_free_block_mt`）。手算结果可直接核对源码条件。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `MI_PAGE_HAS_INTERIOR_POINTERS` 标志必须放在 `xthread_id` 里，而不能是 `mi_page_t` 里一个普通 bool？

**答案**：设置该标志的时机（对齐分配发现块带内部偏移，alloc-aligned.c）可能与其它线程的 free 并发。普通 bool 的并发写是数据竞争；放进 `xthread_id` 后用原子 OR/AND 修改（`mi_page_flags_set`，[internal.h:L974-L979](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L974-L979)），且 free 快路径无需额外读一次内存——同一次 `xthread_id` 加载就带出了「是否本线程页 + 是否有标志」全部信息。这是典型的「用位 packing 换内存访问次数」。

**练习 2**：新页的 `xthread_free` 为什么初始化为 1（而不是 0）？

**答案**：1 = 空链表（NULL & ~1）+ owned=1。新建页天然属于创建它的 theap，必须一出生就标记「被拥有」，否则其它线程的 free 会误以为页无主而尝试认领并收割一个尚未初始化好的页。[page.c:L741](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L741) 的断言 `_mi_page_init` 把这一点固化成了契约。

**练习 3**：`mi_page_is_owned` 与 4.2 节的 `mi_page_all_free`（used==0）是什么关系？为什么判断页「真的全空」需要同时用到两者？

**答案**：`mi_page_is_owned`（[internal.h:L1111-L1113](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1111-L1113)）回答「我能不能碰非原子字段」（前提），`mi_page_all_free` 回答「used 是否为 0」（内容），且后者要求 used 是新鲜的（先 collect，见练习 4.3-1）。释放/abandon 一个页的完整条件是：拥有（或刚认领）所有权 → collect 刷新 used → used==0。两问分别对应 [page.c:L291-L304](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L291-L304) `_mi_page_abandon` 中 `_mi_page_free_collect` 与 `mi_page_all_free` 的先后调用。

## 5. 综合实践

**任务：三窗口实验——用统计输出「看见」三条链表。**

写一个程序（**示例代码**），在三个时间点打印统计，对照本讲不变式解释每个差异：

```c
/* freelist_windows.c (示例代码) */
#include <stdio.h>
#include <mimalloc.h>

int main(void) {
  enum { N = 2000 };
  void* p[N];

  /* 窗口 A：从空页开始连续分配，观察 extend 的“台阶” */
  for (int i = 0; i < N; i++) p[i] = mi_malloc(64);
  mi_stats_print_out(NULL, NULL);

  /* 窗口 B：全部本地释放，观察 current 归零与 retire */
  for (int i = 0; i < N; i++) mi_free(p[i]);
  mi_stats_print_out(NULL, NULL);

  /* 窗口 C：释放后再分配同样多的块，观察是否复用（extended 不应显著增加） */
  for (int i = 0; i < N; i++) p[i] = mi_malloc(64);
  mi_stats_print_out(NULL, NULL);
  for (int i = 0; i < N; i++) mi_free(p[i]);
  return 0;
}
```

构建与运行：

```bash
cmake -B out/debug -DCMAKE_BUILD_TYPE=Debug && cmake --build out/debug -j
gcc -Iinclude -o windows freelist_windows.c out/debug/libmimalloc-debug.a -pthread
MIMALLOC_SHOW_STATS=1 ./windows
```

需要回答的三个问题：

1. **窗口 A → B**：对应 bin 行的 current 从 ≈2000 变为 0；`retire` 计数增加。用 4.3 节的推导解释：最后一次 `mi_free` 使 used 减到 0 时发生了什么（提示：[free.c:L49-L53](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L49-L53) → [page.c:L424-L457](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L424-L457)）。
2. **窗口 B → C**：`extended` 计数几乎不再增长。解释这 2000 个新块从哪条链表来（提示：本地释放把块堆在 `local_free`，慢路径的 `mi_page_free_quick_collect` 把整条链 O(1) 搬回 `free`；retired 页被复用时 `retire_expire` 清零，[page.c:L869](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L869)）。
3. **进阶（可选）**：把窗口 B 改成由另一个线程执行释放（生产者-消费者），对比 `retire` 计数是否还增长。结合 4.2.3 ③（mt free 不减 used）解释差异，并说明为什么拥有者线程的下一次分配才是收割时机。

预期现象的定性判断如上；具体计数值随平台与构建类型浮动，待本地验证。若窗口 C 的 `extended` 仍在明显增长，先检查是否误用了 release 构建（MI_STAT=0 时看不到这些计数器）。

## 6. 本讲小结

- `mi_page_t` 是 64KiB/512KiB/4MiB 的「定长块容器」，空闲块头 8 字节复用为 `next` 指针（`mi_block_t`），块地址可由 `page_start + page_offset + i × block_size` 纯算术得出。
- 页内三条链表分工明确：`free` 供 malloc 快路径弹出、`local_free` 暂存本线程释放（支撑单调心跳）、`xthread_free` 暂存跨线程释放（让拥有线程分配零原子操作）；迁移单向进行：`xthread_free` —CAS 收割→ `local_free` —O(1) 头搬→ `free`。
- 计数不变式：\( \text{alive} = \text{used} - |\text{thread\_free}| \)、\( \text{capacity} = \text{used} + |\text{free}| + |\text{local\_free}| \)（后者是 page.c:116 的运行时断言）；`used - |thread_free| == capacity` ⇔ 页被活对象塞满、三条链表全空。
- `capacity < reserved` 的页是 expandable，`mi_page_extend_free` 每次只初始化约 8KiB 的量；`used == 0`（collect 后）触发 retire，页被保留约 16 个管理周期再真正释放。
- 位编码两处：`xthread_id` 低 2 位存页标志（full 队列 / 内部指针），配合 XOR 让 `mi_free` 一次比较分流四种情况；`xthread_free` 低 1 位是所有权位，让跨线程 free 能用一次 CAS 同时「推块入链 + 认领无主页」。
- 读源码时**断言与宏是契约，注释可能有滞后**：本讲遇到两处（types.h:409 的不变式表述、types.h:422 的 "3 bits"），都以运行时断言和宏定义为准。

## 7. 下一步学习建议

本讲搞定了「页内静态结构」，接下来两条线自然延伸：

1. **u3-l3（size class 与 bin）**：本讲反复出现「同 size 的页队列」，下一讲讲清 `_mi_bin` 如何把请求尺寸映射到 bin、`pages_free_direct` 如何让小对象分配 O(1) 找到页——那是 `mi_page_t` 之上的组织层。
2. **u3-l4（page map）**：4.4 节的 free 分流入口处，`mi_free` 是先从指针反查出 `mi_page_t` 的，这个「指针 → 页」的反查机制下一讲剖析。
3. 预习 u4/u5：把本讲 4.2.3 的五个代码点分别放进 `mi_malloc`（u4-l1）与 `mi_free`（u5-l1）的完整调用链里看，你会看到它们正好是两条链路上最热的几行。
