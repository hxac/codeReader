# 堆遍历：visit_blocks 与 GC 集成

## 1. 本讲目标

学完本讲，你应该能够：

1. 正确使用 `mi_heap_visit_blocks` / `mi_heap_visit_abandoned_blocks`：说清 visitor 回调的调用顺序（先 area、后 block）、`mi_heap_area_t` 七个字段的语义，以及「返回 false 提前终止」的传播路径。
2. 解释**在没有每块元数据**的前提下，mimalloc 如何推断「一个块是否存活」：先收割三条 free list，再用一张栈上位图把空闲块标记出来，剩下的就是存活块。
3. 画出堆级遍历的完整调用链：`mi_heap_visit_blocks` → arena 注册位图 → `mi_page_t` → `_mi_theap_area_visit_blocks`，并指出公共入口的实际实现位置在 `src/arena.c` 而非 theap.c。
4. 说清 abandoned 页在两套遍历 API 下的可见性差异：废弃的 theap 级遍历天然看不见 abandoned 页，v3 的 heap 级遍历则通过 arena 注册位图把它们包含进来。
5. 归纳 v3 堆遍历的效率改进来源（注册位图 O(置位数) 枚举、满页/单例页快速路径、free 位图 + `mi_ctz` + 魔数快速除法），并理解 GC / 泄漏检测器为什么需要这套接口。

## 2. 前置知识

本讲是专家层「观测」课的第二讲，建立在单元三、单元五与 u9-l3 之上。需要 recall 的概念：

- **无每块头**（u4-l1）：mimalloc 的用户指针就是块地址，块前没有 header。分配快路径因此极快，但代价是：**给定一个页，分配器没有任何「第 i 号块是否存活」的现成答案**——这正是本讲要解决的核心矛盾。
- **三条 free list**（u3-l2）：`free`（可立即分配）、`local_free`（本线程释放、延迟可用）、`xthread_free`（跨线程释放、待属主收割）。计数不变式：`capacity = used + |free| + |local_free|`，而跨线程 free **不减 `used`**。
- **页的归属链**（u3-l1 / u6-l4）：页属于 heap、挂在某个 theap 的页队列上；线程退出时含活块的页被 **abandon**——`xthread_id` 置 0（或 4 = abandoned-mapped）、从 theap 队列摘除，但仍登记在 heap 的 `arena_pages->pages` 位图中。
- **arena 与 slice**（u6-l3）：arena 是从 OS 批发的内存区，以 64 KiB slice 为单位零售；heap 在每个用过的 arena 上挂一份 `mi_arena_pages_t` 登记簿（`pages` + `pages_abandoned[]` 两张位图）。
- **原子位图**（u8-l3）：`mi_bitmap_t` 分 bfield/bchunk/chunkmap 三层，chunkmap 是保守索引——查「哪些位是 1」时可以整 chunk 跳过。
- **X-macro 与 ABI 护栏**（u9-l3）：对照记忆——统计系统的取数有副作用（破坏式合并）；本讲的遍历同样**有副作用**（收割 free list），两者都是「观测即改变」的例子。

另外一个术语：**根集扫描（root set scanning）**。托管运行时（如 CPython、Koka）的垃圾回收器需要从栈/寄存器/全局变量出发找活对象；有些 GC 还需要**枚举堆内全部活块**（比如做压缩整理、或验证可达集）。「按 size class 枚举一个堆里所有存活块」正是 `mi_heap_visit_blocks` 提供的能力。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| `include/mimalloc.h` | 遍历 API 合同：`mi_heap_area_t`（L303-L311）、`mi_block_visit_fun`（L313）、`mi_heap_visit_blocks` / `mi_heap_visit_abandoned_blocks`（L315-L316）、`mi_subproc_visit_heaps`（L362-L363）、已废弃的 `mi_theap_visit_blocks`（L447） |
| `src/theap.c` | **单页块枚举核心**：`_mi_heap_area_init`（L542-L552）、魔数快速除法（L554-L564）、`_mi_theap_area_visit_blocks`（L566-L668）；以及废弃的 theap 级遍历（L677-L723，沿页队列走） |
| `src/arena.c` | **堆级遍历的实际实现**：`_mi_heap_visit_blocks`（L2471-L2512）沿 heap 的 arena 注册位图发现页；abandon 时只补记 `pages_abandoned` 位图不清 `pages`（L1304-L1355）；页释放时才清注册位（L1251-L1259） |
| `src/page.c` | 遍历的前置副作用：`_mi_page_free_collect`（L214-L243）把 `xthread_free`/`local_free` 收割合并进 `free`，使存活判定成为可能 |
| `src/bitmap.c` | `_mi_bitmap_forall_set`（L1437-L1460）：按 chunkmap 跳空 chunk，O(置位数) 枚举注册页 |
| `include/mimalloc/types.h` | `mi_arena_pages_s`（L723-L727）、heap 上的登记字段（L631-L636）、`mi_block_s`（L366） |
| `include/mimalloc/internal.h` | `mi_page_start` / `mi_page_area`（L817-L829）、`mi_page_usable_block_size`（L851-L853）、解码 free list 的 `mi_block_next`（L1271-L1284） |
| `test/main-override-static.c` | 官方堆遍历示例 `test_heap_walk`（L232-L249）：area 与 block 两级回调的标准写法 |

> 注意一个容易搞错的点：讲义规格与本讲标题都指向 theap.c，但**公共入口 `mi_heap_visit_blocks` 的实现并不在 theap.c**——v3 把「发现页」的职责移到了 heap 级的 arena 登记位图上（arena.c），theap.c 只保留「一个页内部怎么枚举块」的算法（`_mi_theap_area_visit_blocks`）与已废弃的 theap 级遍历。本讲两者都讲，以源码为准。

## 4. 核心概念与源码讲解

### 4.1 遍历 API 合同：mi_block_visit_fun 与 mi_heap_area_t

#### 4.1.1 概念说明

堆遍历要回答的问题是分配的逆问题：分配是「给我一个块」，遍历是「把所有活块列出来」。使用场景：

- **GC 集成**：运行时枚举堆内活块做标记整理、或扫描候选根；
- **泄漏检测**：程序跑到某个检查点，看哪些 size class 还有不该存在的存活块；
- **堆快照 / 调试器**：`test_heap_walk` 那样打印每个区域的组成。

mimalloc 把遍历设计成**两级回调**：一个堆由若干 area（区域）组成，每个 area 是一个页的用户数据区，内含**单一尺寸**的定长块。visitor 先以 `block == NULL` 被调用一次（报告 area），随后（若 `visit_blocks` 为 true）对 area 内**每个存活块**各调用一次。返回 `false` 表示提前终止。

#### 4.1.2 核心流程

```text
mi_heap_visit_blocks(heap, visit_blocks, visitor, arg)
   │
   │  对 heap 的每个 area（即每个登记在册的页）：
   ├─(1) visitor(heap, &area, block=NULL, area.block_size, arg)   ← 区域级回调
   │
   └─(2) 若 visit_blocks==true：
         对该 area 内每个存活块：
            visitor(heap, &area, block, ubsize, arg)              ← 块级回调
            （返回 false 则整条链路短路，最终返回 false）
```

`mi_heap_area_t` 各字段（由 `_mi_heap_area_init` 从 `mi_page_t` 现算）：

| 字段 | 含义 | 来源 |
|---|---|---|
| `blocks` | 区域起始地址 | `mi_page_start(page)` |
| `reserved` | 虚拟保留字节数 | `page->reserved * block_size` |
| `committed` | 已提交字节数 | `page->capacity * block_size` |
| `used` | **存活块数**（v1/v2 文档写的是字节数，v3 头文件明确是块数） | `page->used` |
| `block_size` | 每块可用字节数（不含 padding） | `mi_page_usable_block_size(page)` |
| `full_block_size` | 每块完整字节数（含 padding） | `page->block_size` |
| `reserved1` | 内部字段，回传 `mi_page_t*` | `page` |

#### 4.1.3 源码精读

- [include/mimalloc.h:L298-L316](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L298-L316)：遍历 API 全家福。L302 注释点明「An area of heap space contains blocks of a single size」——area 即页的用户区；L313 定义 visitor 签名；L315-L316 是两个公共入口：`mi_heap_visit_blocks`（全部页）与 `mi_heap_visit_abandoned_blocks`（只看被遗弃的页）。
- [include/mimalloc.h:L362-L363](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L362-L363)：`mi_subproc_visit_heaps`，按 subproc 枚举全部堆。与 `mi_heap_visit_blocks` 组合可得到「整个分配域的全部活块」——GC 全堆扫描的入口形态。
- [include/mimalloc.h:L440-L447](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L440-L447)：`mi_theap_visit_blocks` 被明确放在 **Deprecated** 段（L447）。它是「只遍历单个 theap 页队列」的旧接口，4.4 节展开。
- [src/subproc.c:L303-L313](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/subproc.c#L303-L313)：`mi_subproc_visit_heaps` 实现——拿 `subproc->heaps_lock`，沿堆链表逐个回调。
- [test/main-override-static.c:L232-L249](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/main-override-static.c#L232-L249)：官方示例 `test_visit` + `test_heap_walk`。注意它怎么区分两级回调：`block == NULL` 时打印 `area->full_block_size`，否则打印块尺寸并用 `mi_usable_size` 交叉验证。这是写自己 visitor 的标准模板。

#### 4.1.4 代码实践

**实践目标**：亲手观察两级回调的调用顺序与字段含义。

1. 阅读并理解 `test/main-override-static.c` 的 `test_heap_walk`（示例见上）。
2. 把 `test_visit` 抄进自己的小程序（示例代码）：建一个新堆，分配 `mi_heap_malloc(heap, 40)` 与 `mi_heap_malloc(heap, 4096)` 各若干次，再 `mi_heap_visit_blocks(heap, true, &test_visit, NULL)`。
3. 观察输出：应当先看到若干行「visiting an area with blocks of size …」，每行之后跟着该区域每个存活块的「block of size …」。
4. 预期结果：40 字节的请求会聚在一个 `full_block_size` 相同的 area 里（size class 取整），4096 的请求在另一个 area。具体数值取决于 size class 表，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：visitor 的 `block_size` 参数传的是 `block_size` 还是 `full_block_size`？两者何时相等？

答案：块级回调传的是**可用尺寸** `ubsize`（`_mi_theap_area_visit_blocks` 里 `visitor(heap, area, block, ubsize, arg)`，theap.c L585/L593/L648/L660），而区域级回调的 `area->full_block_size` 是含 padding 的完整块长。debug/secure 构建（`MI_PADDING_SIZE > 0`）下两者差 8 字节；普通 release 构建下 `MI_PADDING_SIZE` 为 0，两者相等。

**练习 2**：visitor 返回 `false` 之后，mimalloc 还会继续遍历后续 arena 的页吗？

答案：当轮位图枚举立即短路（`_mi_bitmap_forall_set` 返回 false），`_mi_heap_visit_blocks` 里 `ok` 变 false 后其余 arena 的访问都被 `ok &&` 挡住，且 `if (!ok) return false;` 直接跳过 OS abandoned 链表的遍历（arena.c L2497）。函数返回 `false` 表示「没有遍历完」——调用方不能把返回 false 当错误，只能当「被 visitor 打断」。

### 4.2 堆级驱动：arena 注册位图上的页发现（src/arena.c）

#### 4.2.1 概念说明

要遍历一个堆，第一个问题是：**这个堆的页都在哪？**

v3 之前（v1/v2）的答案是「沿每个线程的 theap 页队列走」——但堆是跨线程的，页分散在任意线程的 theap 里，还得单独处理 abandoned 页。v3 的答案是把登记簿搬进 heap：heap 在**每个用到的 arena** 上挂一份 `mi_arena_pages_t`，其中 `pages` 位图记录「哪些 slice 是本堆页的起点」。于是「堆的页集合」= 各 arena 上 `pages` 位图的置位集合，遍历变成**位图枚举**：

- 位图天然按地址序输出（即按内存布局顺序访问页，缓存友好）；
- chunkmap 让空 chunk 整段跳过，代价正比于**页数**而非 arena 体积。

关键性质：abandon 一个页**不清除**它在 `pages` 位图上的登记位（只有真正释放页时才清，arena.c L1259），所以堆级遍历自动覆盖 abandoned 页。

#### 4.2.2 核心流程

```text
_mi_heap_visit_blocks(heap, abandoned_only, visit_blocks, visitor, arg)
 1. heap==NULL ? 用主堆兜底
 2. mi_forall_arenas：对 subproc 的每个 arena（循环起点轮转，但访问语义不变）
     arena_pages = heap->arena_pages[arena->arena_idx]   （可能为 NULL，跳过）
     若 abandoned_only：
        对每个尺寸 bin：
          heap->abandoned_count[bin] > 0 才扫 pages_abandoned[bin] 位图
     否则：
        扫 arena_pages->pages 位图（含 owned + abandoned 全部登记页）
     每个置位 slice_index：
        page = mi_arena_page_at_slice(arena, slice_index)   ← 纯算术/查表定位页
        mi_heap_visit_page(page, vinfo):
            _mi_heap_area_init(&area, page)
            visitor(heap, &area, NULL, area.block_size)     ← 区域回调
            若 visit_blocks：_mi_theap_area_visit_blocks(...)  ← 块枚举（4.3 节）
 3. 最后沿 heap->os_abandoned_pages 链表访问 OS 直配的 abandoned 页
```

约束（源码注释明说）：**「we assume we are the only thread running (with this heap)」**——堆遍历假定停世界或独占堆。原因见 4.3：块枚举要改页的非原子字段。

#### 4.2.3 源码精读

- [src/arena.c:L2471-L2512](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L2471-L2512)：`_mi_heap_visit_blocks` 主体。L2474 空堆兜底为主堆；L2476-L2477 注释交代「无需先 claim，因为我们假定只有本线程在用这个堆（也可以先 reclaim 再 reabandon 来做原子 claim）」；L2483-L2489 是 abandoned_only 分支——先用 `heap->abandoned_count[bin]` 做零成本预判再扫对应 bin 的位图（u6-l4 讲过的分层加速）；L2492 是常规分支，直接 `_mi_bitmap_forall_set(arena_pages->pages, ...)`；L2499-L2509 补扫 OS 直配页的 abandoned 链表（先读 `next` 再回调，防 visitor 释放页）。
- [src/arena.c:L2514-L2520](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L2514-L2520)：两个公共入口只是给 `abandoned_only` 传不同实参的薄封装。
- [src/arena.c:L2449-L2469](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L2449-L2469)：`mi_heap_visit_page`（两级回调的编排）与 `mi_heap_visit_page_at`（位图回调 → slice → 页）。注意 L2452 断言 `vinfo->heap == mi_page_heap(page)`——页的归属堆与遍历的堆必须一致，这是登记簿正确的体现。
- [src/arena.c:L166-L184](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L166-L184)：`mi_arena_page_at_slice`——从 slice 下标拿页元数据。默认构建 `MI_PAGE_META_IS_ALIGNED` 下是纯算术对齐反查（u3-l4 讲过的免查表路径）。
- [src/arena.c:L677-L682](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L677-L682)：`mi_heap_arena_pages`——按 `arena_idx` 从 `heap->arena_pages[]` 原子取登记簿。
- [include/mimalloc/types.h:L723-L727](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L723-L727)：`mi_arena_pages_s`——`pages` 注释即「all registered pages (abandoned and owned)」，`pages_abandoned[MI_ARENA_BIN_COUNT]` 按 size class 分桶记录被遗弃页。
- [src/arena.c:L1101-L1105](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1101-L1105)：页创建时 `mi_bitmap_set(arena_pages->pages, slice_index)` 完成登记——遍历的数据来源就在这里。
- [src/arena.c:L1304-L1355](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1304-L1355)：`_mi_arenas_page_abandon`——遗弃页：非满 arena 页走 L1332 `mi_bitmap_set(arena_pages->pages_abandoned[bin], slice_index)` 补记 abandoned 位图，**完全不碰 `pages` 位**；OS 直配页挂进 `heap->os_abandoned_pages` 链表（L1345-L1351）。这就是堆遍历能看见 abandoned 页的机制根源。
- [src/arena.c:L1251-L1259](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1251-L1259)：对照点——只有页**真正销毁**（`mi_arenas_page_free_prim`）时才 `mi_bitmap_clear(arena_pages->pages, slice_index)` 注销。
- [src/bitmap.c:L1437-L1460](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1437-L1460)：`_mi_bitmap_forall_set`——先读 chunkmap（L1441），只对 chunkmap 置位的 chunk 逐字扫描（L1448-L1456），每个置位 bit 回调一次。空 chunk 的成本是一次 chunkmap 读，这就是「O(置位数)」的出处。

#### 4.2.4 代码实践

**实践目标**：把「堆 → arena → 登记位图 → 页」的发现链在源码里走一遍，并核对 arena 数量。

1. 源码阅读型实践：从 `mi_heap_visit_blocks`（arena.c L2514）出发，依次跳进 `_mi_heap_visit_blocks` → `mi_forall_arenas` 宏（arena.c L454-L482，注意它是带轮转起点的 for 循环展开）→ `_mi_bitmap_forall_set` → `mi_heap_visit_page_at` → `mi_arena_page_at_slice`，画出这条链涉及的数据结构（heap、arena、arena_pages、page）。
2. 运行观察：写一个小程序，分配几 MB 后调用 `mi_arenas_print()`（声明见 mimalloc.h L333），看进程实际保留了几个 arena。
3. 需要观察的现象：小规模分配通常只出现 1 个 arena（默认 1 GiB 保留增量足够）；继续分配超过后再看，arena 数增加，而堆遍历会跨多个 arena 收集页。
4. 预期结果：`mi_arenas_print` 的输出格式与 arena slice 使用统计（u6-l3 已见过），**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_mi_heap_visit_blocks` 的 abandoned_only 分支要先看 `heap->abandoned_count[bin]` 再扫位图？

答案：这是分层过滤：`abandoned_count[bin]` 是 heap 上的一个原子计数，读一次就能判断「这个尺寸 bin 有没有 abandoned 页」，没有就完全跳过对 `pages_abandoned[bin]` 位图的扫描。绝大多数 bin 大多数时刻为 0，省掉的是整张位图的遍历成本。

**练习 2**：`mi_forall_arenas` 的轮转起点（`mi_arena_start_idx`）会影响遍历结果的完整性吗？

答案：不会。宏的轮转只改变**访问顺序**（从哪个 arena 开始转一圈），`for (size_t _i = 0; _i < _arena_count; _i++)` 保证每个 arena 恰好访问一次。轮转是为分配时的局部性设计的（避免所有线程都从第一个 arena 开始争抢），遍历复用一个宏只是顺带继承了顺序无关性。

### 4.3 单页块枚举：free 位图与快速除法（src/theap.c + src/page.c）

#### 4.3.1 概念说明

发现了页之后，第二个问题是：**一个页里，哪些块是存活的？**

mimalloc 的块没有 header、没有 per-block 标记位，唯一的「死亡名册」是 free list。于是判定规则是纯逻辑推导：

\[
\text{block}_i \text{ 存活} \iff i < \text{capacity} \;\wedge\; \text{block}_i \notin \text{free list}
\]

但直接用这条规则有个坑：跨线程释放的块还在 `xthread_free` 链上（没减 `used`、不在 `page->free` 上），本线程延迟释放的块在 `local_free` 上。所以**遍历的第一步是把三条链合并成一条**——调用 `_mi_page_free_collect(page, true)`。这也解释了「堆遍历有副作用」：它顺手做了一次强制收割，遍历后页的 `used`/`free`/`local_free` 都可能变了。

合并后不变式收紧为 \( |\text{free}| = \text{capacity} - \text{used} \)，枚举结果的数量必等于 `used`（源码有断言兜底）。

#### 4.3.2 核心流程

`_mi_theap_area_visit_blocks` 对一个页的完整处理：

```text
1. _mi_page_free_collect(page, /*force=*/true)
     ├─ mi_page_thread_free_collect：CAS 交换抢走整条 xthread_free，
     │   数块数、used -= count、挂上 local_free
     └─ local_free 整链并入 free（force 下若 free 非空则 O(n) 走到尾再拼接）
2. used == 0 ? 直接返回（整个页没有存活块）
3. capacity == 1 ?（单例/巨对象页）→ 只回调一次 visitor，结束
4. used == capacity ?（满页）→ 线性走 capacity 个块逐个回调，结束
5. 一般情形：栈上建 free 位图
     a. 位图大小 = ceil(capacity / 64) 个机器字，尾部越界位预置 1（视为"空闲"）
     b. 预计算 bsize 的除法魔数（magic, shift）
     c. 沿 free 链逐块：offset = block - pstart
        blockidx = mi_fast_divide(offset, magic, shift)   ← 乘法+移位代替除法
        free_map[blockidx/64] |= 1 << (blockidx%64)
     d. 逐字扫描：字为 0 → 连续 64 个块全部存活，直接线性回调
                   字非 0 → m = ~free_map[i]，mi_ctz 逐个取 1 位回调
```

快速除法的数学原理（以 64 位为例）：对常数除数 \(d\)，预计算

\[
s = 32 - \mathrm{clz}(d-1), \qquad M = \left\lfloor \frac{2^{32} \cdot (2^{s} - d)}{d} \right\rfloor + 1
\]

则对 \(n \le 2^{32}\) 有 \(\lfloor n/d \rfloor = \lfloor ((M \cdot n) \gg 32 + n) \gg s \rfloor\)——用一次乘法、一次加法、两次移位替掉一次硬件除法（除法指令延迟通常是乘法的数倍）。页内块数最多几千，省的是内层循环里的每一轮。

#### 4.3.3 源码精读

- [src/theap.c:L566-L574](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L566-L574)：函数入口。L572 一行注释点明前置条件——`_mi_page_free_collect(page,true)` 同时收割 thread（跨线程）与 local 两条延迟链；L573 断言 `local_free == NULL`；L574 空页早退。
- [src/page.c:L214-L243](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L214-L243)：`_mi_page_free_collect`。L218 先收 `xthread_free`；L221-L239 合并 `local_free`：快情形直接头搬（L222-L227），`force` 且 free 非空时走到链尾拼接（L228-L239，注释说明这是线性操作、只在遍历/停机时发生）。L242 断言 force 后 local_free 必空。
- [src/page.c:L185-L201](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L185-L201)：`mi_page_thread_free_collect`——一次 CAS 弱交换把整条跨线程链抢走（u5-l2 讲过的整链收割）。
- [src/page.c:L150-L183](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L150-L183)：`mi_page_thread_collect_to_local`——数链长（以 `capacity` 为上界防损坏）、L165-L173 两级损坏检测（跨线程 double free / 元数据损坏报 EFAULT）、L182 `used -= count` 修正计数。
- [src/theap.c:L583-L597](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L583-L597)：两条快速路径。capacity==1 是巨对象/单例页（u4-l3），断言其 free 必空、used 必为 1，一次回调完事；满页（used==capacity）不需要位图也不需要走 free 链——由不变式可知 free 链必空，直接线性步进回调。
- [src/theap.c:L600-L609](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L600-L609)：free 位图是**栈上数组**：`MI_MAX_BLOCKS = MI_SMALL_PAGE_SIZE / sizeof(void*)`（64 KiB / 8 = 8192 位 = 128 个 uintptr_t，types.h L227 与 bits.h L66）。L604-L609 把尾部不足 64 的余数位预置成「空闲」，避免把 `capacity` 之外的位错判为存活。
- [src/theap.c:L619-L633](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L619-L633)：沿 free 链标位。`mi_block_next`（internal.h L1271-L1284）在 `MI_ENCODE_FREELIST` 下用页 keys 解码指针并校验 next 是否同页（u9-l1 的安全机制在这里被遍历复用）；L627 用魔数除法算块下标，L628 的 debug 断言 `blockidx == offset / bsize` 正是快速除法正确性的在位验证。
- [src/theap.c:L641-L665](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L641-L665)：扫描阶段。字为 0（L642-L651）时连续 64 块全存活，无位运算直接步进；字非 0（L653-L664）时取反后用 `mi_ctz` 逐个弹最低位的 1（`m &= m - 1` 清最低位，u8-l3 的位技巧）。L666 断言枚举数 == `page->used`，把推导闭环钉死。
- [src/theap.c:L542-L552](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L542-L552)：`_mi_heap_area_init`——area 七个字段全部由页现算。注意 `area->reserved1 = page`：公共结构里藏了一个内部回传通道，`mi_heap_delete_page`（arena.c L2534）靠它从 area 拿回 `mi_page_t`。
- [src/theap.c:L554-L564](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L554-L564)：`mi_get_fast_divisor` / `mi_fast_divide`——上一节公式的直接实现。
- [src/theap.c:L131](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L131)：collect 路径上注明 `python/cpython#112532`——collect 可能由非属主线程调用。这是仓库内少数直接指向 CPython 集成的注释，说明这套「收割 + 遍历」机制确实为 Python 这类运行时打磨过。

#### 4.3.4 代码实践

**实践目标**：验证「满页快速路径」与 free 位图路径的行为差异。

1. 写一个示例程序，新建堆后连续 `mi_heap_malloc(heap, 64)` 分配 **20000** 个块且全部保留（不 free）——远超一个 64 KiB 小页能容纳的 1024 块，必然铺满多个整页。
2. 用上一节的 visitor 统计：每个 area 的「区域回调次数」与「块回调次数」。
3. 需要观察的现象：多数 area 的块回调次数恰好等于 `area->committed / area->block_size`（整页全存活）；最后一个 area 是半满页。
4. 对照源码说明：整页的那些 area 命中了 theap.c L590-L597 的满页快速路径（没有位图、没有 free 链遍历），半满页走 L600-L665 的位图路径。
5. 预期结果：整页 area 的枚举是纯线性步进；具体页数取决于 size class（64 字节请求实际块长可能更大），**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果遍历前不做 `_mi_page_free_collect(page, true)`，哪些块会被错误地报告为「存活」？

答案：两类。(1) 跨线程释放的块：它们在 `xthread_free` 链上，`used` 尚未减、也不在 `page->free` 上，位图法会把它们判成存活；(2) 本线程延迟释放的块：在 `local_free` 上、同样不在 `free` 上。collect 之后三条链合一，`|free| = capacity - used` 才成立，枚举数恰为 `used`。

**练习 2**：为什么满页（`used == capacity`）可以直接线性枚举而不查 free 链？

答案：由不变式 `capacity = used + |free| + |local_free|`，collect 后 `local_free` 并入 `free`，得 `capacity = used + |free|`；`used == capacity` 推出 `|free| == 0`，链上没有任何块需要排除。源码还在 capacity==1 的分支断言了 `page->free == NULL`，同理由。

**练习 3**：`mi_fast_divide` 为什么要求 `n <= UINT32_MAX`（断言在 L562 与 L626 两处出现）？

答案：魔数 \(M\) 是按 32 位精度推导的：\(M = \lfloor 2^{32}(2^s - d)/d \rfloor + 1\) 只在被除数不超过 \(2^{32}\) 时保证 \(\lfloor n/d \rfloor\) 的结果精确（这是经典的 division-by-invariant-multiplication 结论）。页内偏移量天然小于 64 KiB 页/最大页尺寸且远小于 \(2^{32}\)，所以断言成立；若 n 超界，乘法中间值会丢失高位导致下标错乱。

### 4.4 abandoned 可见性、废弃 API 与 v3 效率改进、GC 集成

#### 4.4.1 概念说明

本模块收束三个问题。

**其一：abandoned 页在两套 API 下的可见性。** 废弃的 `mi_theap_visit_blocks` 沿 theap 的页队列走（`mi_theap_visit_pages`），而 abandon 的定义动作就是把页从队列摘除、`xthread_id` 清零——所以 **theap 级遍历天然看不见 abandoned 页**，theap.c L536-L539 的 Todo 注释（"enable visiting abandoned pages"）承认这正是缺口。v3 的 heap 级遍历改走 `arena_pages->pages` 登记位图，而遗弃不清登记位（4.2 节），于是 abandoned 页**默认被包含**；另有 `mi_heap_visit_abandoned_blocks` 专门只看被遗弃子集（mapped 位图 + OS 链表）。v1/v2 时代靠 `mi_option_visit_abandoned` 开关控制是否包含，v3.5 里这个选项已更名为 `mi_option_deprecated_visit_abandoned` 且**全库没有任何运行时读取者**——职能被两个显式函数取代。

**其二：v3 相对旧版 heap-walking 的效率改进来源**，可归纳为三层：

1. **发现层**：heap 级注册位图 + chunkmap 跳空（O(页数) 而非 O(队列长度×线程数)），且按地址序访问页（缓存友好）；不必再逐线程找 theap。
2. **页内层**：free 位图 + `mi_ctz` + 魔数快速除法；满页/单例页免位图免链表的两条快速路径。
3. **覆盖层**：abandoned 页不再需要独立的遍历通道（v1/v2 的 `mi_abandoned_visit_blocks` 一族在 v3 头文件中已不存在，见 doc/mimalloc-doc.h L715-L724 对 v1/v2 的标注），一次堆遍历即全量。

**其三：GC 集成的视角。** 一台「精确枚举某堆全部活块」的机器正好是增量 GC / 堆压缩 / 泄漏检测需要的原语。仓库内可见的集成痕迹：`mi_subproc_visit_heaps` + `mi_heap_visit_blocks` 组合成全分配域扫描；theap.c L131 引用 cpython#112532（collect 由非属主线程调用的场景）；`mi_heap_delete_page`（arena.c L2613）本身就用 `_mi_heap_visit_blocks(heap, false, false, ...)` 以「只走区域回调」的方式枚举页来实现堆销毁——遍历机制连内部功能都在复用。关于 CPython GC 具体如何用它做根集扫描，仓库内没有实现代码，属于其仓库侧的集成工作，此处不展开臆测。

#### 4.4.2 核心流程

两套遍历 API 的对比：

| 维度 | `mi_heap_visit_blocks`（现行） | `mi_theap_visit_blocks`（已废弃） |
|---|---|---|
| 页发现 | heap 的 arena 注册位图 + OS abandoned 链表 | 单个 theap 的页队列（含 full 队列） |
| abandoned 页 | **包含**（登记位不清） | **不包含**（页已不在任何队列） |
| 前提约束 | 假定独占该堆（停世界） | 必须由属主线程调用（要改非原子字段） |
| 返回值 | false = 被 visitor 打断 | 同左 |
| 声明位置 | mimalloc.h L315 | mimalloc.h L447（Deprecated 段） |

#### 4.4.3 源码精读

- [src/theap.c:L536-L540](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L536-L540)：节注释与 Todo——「enable visiting abandoned pages, and enable visiting all blocks of all theaps across threads」，明确记录了 theap 级遍历的两个缺口，也解释了为何被废弃。
- [src/theap.c:L720-L723](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L720-L723)：`mi_theap_visit_blocks` 实现——组装参数后转调 `mi_theap_visit_areas`。
- [src/theap.c:L685-L699](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L685-L699)：每个页包成 `mi_theap_area_ex_t`（L677-L681：公共 area + 私有 page 指针，注释「keep mi_page_t out of the public interface」）再回调；L698 把函数指针转 `void*` 传递（源码自己也标注了 `:-{`）。
- [src/theap.c:L25-L51](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L25-L51)：`mi_theap_visit_pages`——theap 级页发现：按 bin 0..max_bin 走页队列，先存 `next` 再回调（防回调中页被摘除）。这套机制如今主要服务于 collect（`mi_theap_page_collect`）而非公开遍历。
- [include/mimalloc.h:L494](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L494) 与 [src/options.c:L153](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L153)：`mi_option_deprecated_visit_abandoned`——枚举注释「allow visiting theap blocks from abandoned threads (=0)」，表内默认 1；全库检索无任何 `mi_option_get(...deprecated_visit_abandoned...)` 调用，是 v1/v2 的遗迹。
- [doc/mimalloc-doc.h:L690-L713](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/doc/mimalloc-doc.h#L690-L713)：文档化的调用合同（visitor 先 area 后 block、返回 false 即停），L711 的注意事项仍写着「requires mi_option_visit_abandoned()」——文档相对 v3 源码已滞后，以头文件与实现为准。
- [src/arena.c:L2613](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L2613)：`_mi_heap_visit_blocks(heap, false, /*visit_blocks=*/false, &mi_heap_delete_page, &info)`——堆销毁复用遍历框架、只要区域级回调的内部用例。
- [test/test-stress.c:L187-L196](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-stress.c#L187-L196)：压测里的 visitor：只数块尺寸合计；L246-L250 与 L299-L303 分别在 `MI_HEAP_WALK` 编译开关下演示 theap 级与 heap 级遍历的调用点（线程收尾/每轮迭代）。

#### 4.4.4 代码实践

**实践目标**：用一个双线程实验直观看到「heap 遍历包含 abandoned 页」。

1. 示例程序：主线程 `mi_heap_new()`；起一个工作线程，在该堆上分配 10000 个 128 字节块（泄漏，不释放）；工作线程直接退出（其 theap 的页随后被 abandon，时机由退出路径决定）。
2. 主线程 join 后调用 `mi_heap_visit_blocks(heap, true, visitor, ...)` 统计存活块数，再调用 `mi_heap_visit_abandoned_blocks(heap, true, visitor, ...)` 统计一次。
3. 需要观察的现象：两次统计的块数量级都接近 10000（前者数的是全部存活块，后者只数被遗弃页上的）。
4. 说明：线程退出到页被 abandon 之间有回收时机问题，若输出为 0，可在 join 后先 `mi_collect(true)` 再遍历对照。**待本地验证**。
5. 源码对照：把观察结果与 arena.c L1304-L1355（abandon 时不清 `pages` 登记位）互相印证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `mi_theap_visit_blocks` 必须由该 theap 的属主线程调用，而 `mi_heap_visit_blocks` 只要求「独占该堆」？

答案：两者最终都会进 `_mi_theap_area_visit_blocks`，其中的 `_mi_page_free_collect` 要写页的非原子字段（`free`/`local_free`/`used`）。所有权契约（u5-l1）规定这些字段只有属主线程能写。theap 级 API 直接碰属主的页，所以必须属主调用；heap 级 API 遍历的页可能属于任意线程的 theap（包括已退出的），无法要求「都是属主」，只能退而求其次要求「没有别的线程并发使用这个堆」——即停世界语义，arena.c L2476 注释原文。

**练习 2**：`area->used` 与块级回调的实测个数一定相等吗？

答案：不一定，有一个微妙的时序差：`mi_heap_visit_page` 先 `_mi_heap_area_init`（此时抄录 `page->used`）再调区域回调，之后才进 `_mi_theap_area_visit_blocks` 做 collect。如果一个块在 area 初始化之后、collect 之前恰好被跨线程释放，`area->used` 是旧值，而枚举数按 collect 后的 `used` 走——源码 L666 的断言只保证「枚举数 == collect 后的 used」。在「独占堆」的正常使用前提下两者一致；这是个只在并发遍历（本来就不被支持）下才会暴露的缝隙。

**练习 3**：GC 想要在 mutator 运行的同时增量遍历堆，直接用 `mi_heap_visit_blocks` 有什么风险？

答案：三个。(1) 正确性：块枚举要收割并改写 free list，与并发分配/释放竞争会破坏所有权契约；(2) 安全性：遍历期间页可能被其他线程释放或认领（arena.c 注释提到理论上可先 reclaim 再 reabandon 做原子 claim，但当前未实现）；(3) 语义：visitor 拿到的块在回调返回后随时可能被别人分配掉，不能缓存块指针当长期引用。所以现实用法是**在安全点/停世界时遍历**——这也与 mimalloc 为 Koka/Lean 提供的安全点心跳（u5-l3）是同一套协作模型。

## 5. 综合实践：一个简易「堆快照」泄漏定位工具

把本讲内容串起来：用 `mi_heap_visit_blocks` 做按 size class 的存活块统计，并验证它能定位泄漏。

**第 1 步：编写快照程序**（示例代码）：

```c
// 示例代码：heap-snapshot.c
#include <stdio.h>
#include <mimalloc.h>

#define MAX_BUCKETS 64

typedef struct snap_s {
  size_t block_size[MAX_BUCKETS];  // 该桶的块可用尺寸
  size_t count[MAX_BUCKETS];       // 该桶的存活块数
  size_t areas;                    // 区域（页）数
} snap_t;

static bool snap_visit(const mi_heap_t* heap, const mi_heap_area_t* area,
                       void* block, size_t block_size, void* arg) {
  snap_t* s = (snap_t*)arg;
  if (block == NULL) {                    // 区域级回调：一个 area = 一个页
    s->areas++;
    return true;
  }
  for (size_t i = 0; i < MAX_BUCKETS; i++) {   // 块级回调：按尺寸分桶
    if (s->count[i] == 0 || s->block_size[i] == block_size) {
      s->block_size[i] = block_size;
      s->count[i]++;
      break;
    }
  }
  return true;                            // 返回 false 会提前终止遍历
}

static void snap_print(const char* label, snap_t* s) {
  printf("== %s: %zu areas ==\n", label, s->areas);
  for (size_t i = 0; i < MAX_BUCKETS && s->count[i] > 0; i++) {
    printf("  block_size %6zu B : %8zu blocks alive\n",
           s->block_size[i], s->count[i]);
  }
}

int main(void) {
  mi_heap_t* heap = mi_heap_new();        // 专用堆：快照只含我们自己的对象
  enum { N = 4000 };
  void* keep[N]; void* drop[N];
  for (int i = 0; i < N; i++) {
    keep[i] = mi_heap_malloc(heap, 24);   // 会泄漏：只分配不释放
    drop[i] = mi_heap_malloc(heap, 128);  // 会释放：对照组成
  }
  for (int i = 0; i < N; i++) mi_heap_free(drop[i]);

  snap_t s = {0};
  mi_heap_visit_blocks(heap, /*visit_blocks=*/true, snap_visit, &s);
  snap_print("snapshot after freeing the 128B group", &s);
  mi_heap_destroy(heap);                  // 整堆回收，泄漏一并了结
  return 0;
}
```

**第 2 步：构建与运行**。按 u1-l2 的方式先构建库（`mkdir -p out/release && cd out/release && cmake ../.. && make`），然后编译链接（路径按实际安装位置调整；未 `make install` 时可直接用构建目录里的库与头文件，**待本地验证**）：

```bash
gcc -I<include 目录> heap-snapshot.c -o heap-snapshot -L<lib 目录> -lmimalloc
./heap-snapshot
```

**第 3 步：需要观察的现象**。

- 快照里应出现两个桶：`24 B` 一类与 `128 B` 一类（实际是 size class 取整后的 `block_size`，例如 24 可能取整为 24 或 32，128 可能带 debug padding 变大）。
- 24 B 桶的存活块数 ≈ 4000（泄漏组），128 B 桶存活块数 ≈ 0（已全释放）。
- `areas` 行给出总页数：每个 area 一个页，一个页装同尺寸块。

**第 4 步：泄漏定位验证**。把 `keep` 组改成两个尺寸（比如一半 24 B、一半 900 B），快照应准确显示**两个**泄漏桶；再把 `keep` 改为全部释放，快照应清零。这就是「快照能定位泄漏的 size class」的含义——它不知道泄漏在代码哪一行（那需要分配栈），但能告诉你泄漏的是什么规格。

**第 5 步：进阶对照**（可选）：

- 用 `MIMALLOC_SHOW_STATS=1` 跑同一程序，对照 `mi_heap_destroy` 前后 blocks 段的 `current` 变化（u9-l3 的口径）与你的快照计数是否一致；差异部分回想 4.4.5 练习 2 的时序缝隙与统计口径（元数据归属）。
- 把 `visit_blocks` 参数换成 `false` 再跑：只剩区域级回调，输出退化为「这个堆有多少个页、各装多大块」——这正是 `mi_heap_delete_page` 内部使用的方式。

## 6. 本讲小结

- 遍历是分配的逆问题。mimalloc 块无 header，存活判定靠推导：先 `_mi_page_free_collect(page, true)` 把三条 free list 收割合一，再按 \( i < \text{capacity} \wedge \text{block}_i \notin \text{free} \) 排除空闲块——**遍历有副作用，观测即改变**。
- 两级回调合同：每个 area（= 一个页的用户区，单一块尺寸）先以 `block == NULL` 回调一次，再对每个存活块回调；返回 false 逐层短路，最终返回值 false 表示「未遍历完」。
- 公共入口 `mi_heap_visit_blocks` 实现在 **arena.c**：沿 heap 在各 arena 的 `pages` 注册位图发现页（chunkmap 跳空、按地址序），`mi_heap_visit_abandoned_blocks` 则只走 `pages_abandoned[bin]` 位图与 OS abandoned 链表。
- abandoned 页的可见性分水岭：遗弃不清 `pages` 登记位（只有销毁才清），所以 heap 级遍历**默认包含** abandoned 页；废弃的 `mi_theap_visit_blocks` 沿 theap 页队列走，天然看不见它们（Todo 注释承认缺口）。v1/v2 的 `visit_abandoned` 选项在 v3.5 已无任何运行时读取者。
- 效率改进三层：发现层（注册位图 O(页数)、免逐线程找 theap）、页内层（free 位图 + `mi_ctz` + 魔数快速除法，满页/单例页免位图快速路径）、覆盖层（abandoned 并入一次遍历，无需独立通道）。
- 使用约束：遍历假定独占堆（停世界）；`mi_theap_visit_blocks` 更严格地要求属主线程；`area->reserved1` 是内部回传 `mi_page_t` 的通道，不要依赖。

## 7. 下一步学习建议

本讲收束了单元九的「观测」主线（统计 → 遍历）。建议：

1. **读 `mi_heap_delete_page` 的完整实现**（src/arena.c L2523 起）：看堆销毁如何以「区域回调」模式复用遍历框架做页迁移/整页释放，把 u7-l3 的堆生命周期与本讲串起来。
2. **动手实验 `mi_subproc_visit_heaps`**：写一个双 subproc 程序，验证各自遍历互不越界（结合 u7-l4 的隔离语义），这正是 CPython 多解释器场景的内存审计形态。
3. **对比 v1/v2 分支的同名函数**（git 里切到 `master` 分支看 `heap.c` 的 `mi_heap_visit_blocks`）：亲眼确认「页队列遍历 → arena 注册位图遍历」的结构差异，加深对 v3 改进的理解。
4. 若你关注 GC 集成，可接着读 Koka/Lean 运行时如何组合 `mi_register_deferred_free`（u5-l3）与安全点遍历，把「延迟释放 + 停世界枚举」拼成完整的协作式回收图景。
