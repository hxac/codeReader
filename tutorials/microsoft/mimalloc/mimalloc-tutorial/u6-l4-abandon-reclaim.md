# abandon 与 reclaim：线程间共享内存的终极手段

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释线程退出时，其 theap 中仍有活块的页面为什么被 abandon（遗弃）而不是立刻回收。
2. 说出「abandoned」与「abandoned-mapped」两种遗弃状态的编码方式，以及 arena 级 `pages_abandoned` 位图如何让其他线程按 size class 快速认领整页。
3. 跟踪两条认领路径：分配时认领（`mi_arenas_page_try_find_abandoned`）与释放时认领（`mi_free_try_collect_mt` → `mi_abandoned_page_try_reclaim`），并理解并发 free 与并发认领之间那次罕见的 busy-wait。
4. 区分三条「延迟回收」路径——abandon、retire（`retire_expire` 倒计时）、purge（`purge_expire` 到期）——各自解决什么问题、由谁驱动。
5. 会用 `MIMALLOC_SHOW_STATS=1` 输出中的 `abandoned / reclaima / reclaimf / reabandon / waits / retire` 行做诊断，并用 `MIMALLOC_PAGE_RECLAIM_ON_FREE` 做对比实验。

## 2. 前置知识

本讲站在 u6-l3（arena 与位图）与 u5-l1/u5-l2（free 快慢路径）之上，先把几个关键概念串起来：

- **所有权契约**：`mi_page_t` 中的非原子字段（`free`、`local_free`、`used` 等）只有页的属主线程能写；其他线程只能通过原子字段 `xthread_free` 与页交互。`xthread_free` 的最低位是**所有权位**（1 = 有人持有），`mi_page_claim_ownership` 用一次原子 OR 就能尝试夺取所有权（见 [include/mimalloc/internal.h:L1088-L1119](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1088-L1119)）。
- **三条 free list 与计数不变式**：`used - |thread_free| + |free| + |local_free| == capacity`，`alive = used - |thread_free|`（见 [include/mimalloc/types.h:L398-L413](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L398-L413)）。当 `alive == 0`（即 `used` 收割后被修正为 0）时页面才可能被真正释放。
- **arena 与 slice**：arena 是从 OS 批发的内存区，内部以 64KiB 的 slice 为最小零售单位，slice 的占用状态记录在原子位图里。页（page）由一或多个连续 slice 构成。
- **慢路径管理节拍**：分配慢路径每约 1000 次、每约 10000 次 generic 调用会做一次页管理/collect（u4-l2、u5-l3）。本讲的 retire 倒计时与 purge 到期都挂在这个节拍器上。

一个值得先思考的问题：线程 T1 在自己的 theap 里分配了一页，把其中若干块通过队列交给了 T2，然后 T1 退出了。这页里还有 T2 持有的活块，内存 obviously 不能释放；但 `free`/`local_free`/`used` 这些字段 T2 又不能碰（没所有权）。怎么办？答案就是：**把页「遗弃」在原地，等任何人（通过分配或释放路径）把它「认领」回去**。这正是 mimalloc 把内存驻留与线程生命周期解耦的核心手段。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [src/arena.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c) | abandon 的「登记处」：把页写进 arena 级 `pages_abandoned` 位图、提供分配时认领（`mi_arenas_page_try_find_abandoned`）、unabandon、reabandon 以及 purge 调度 |
| [src/free.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c) | 释放时认领：跨线程 free 顺手夺取所有权（`mi_free_block_mt`），随后 `mi_free_try_collect_mt` 依次尝试释放/认领/重新遗弃 |
| [src/page.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c) | 页级状态机：`_mi_page_abandon`、`_mi_theap_page_reclaim`、满页 abandon（`mi_page_to_full`）、空页退役（`_mi_page_retire`） |
| [src/theap.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c) | 线程退出的入口：`_mi_theap_collect_abandon` 遍历该线程所有 theap 的页面并遗弃 |
| [src/init.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c) | `mi_thread_theaps_done`：线程结束时对 tld 的 theaps 链逐个触发 abandon |
| [include/mimalloc/types.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h) | `mi_page_s`、`mi_heap_s.abandoned_count`、`mi_arena_pages_s.pages_abandoned` 等结构定义与特殊 threadid 常量 |
| [include/mimalloc/internal.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h) | abandoned 判定谓词、所有权位操作、`mi_page_set_theap` 等内联辅助 |
| [src/bitmap.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c) | `mi_bitmap_try_find_and_claim`（认领位图条目）与 `mi_bitmap_clear_once_set`（可能自旋等待的清除） |
| [src/stats.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c) | `abandoned / reclaima / reclaimf / reabandon / waits / retire` 各行的打印 |
| [src/options.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c) | `page_reclaim_on_free`、`page_full_retain`、`page_max_reclaim`、`purge_delay` 等选项默认值 |

## 4. 核心概念与源码讲解

### 4.1 abandon：放弃所有权，而不是释放内存

#### 4.1.1 概念说明

**abandon（遗弃）** 是这样一种状态转移：页面的属主线程把 `xthread_id` 改写为特殊的「无属主」标记、放弃 `xthread_free` 上的所有权位，但**不释放任何 slice**。页中的活块继续有效，任何线程之后仍可对这些块调用 `mi_free`。

为什么必须这样而不是「立刻回收」？三个理由：

1. **页里还有活块**：`alive > 0` 时释放内存等于制造悬垂指针。
2. **所有权契约**：即使全空了，把 `local_free` 迁回 `free`、修正 `used` 也需要先收割 `xthread_free`，这些操作只有持所有权者能做——abandon 路径会先做 collect，所以「恰好全空」时确实会直接释放（见下文）。
3. **线程退出时机的脆弱性**：这段代码运行在 TLS 析构/pthread key 回调里，此时分配器不能再去初始化新的 TLS、不能做复杂分配。

特殊 threadid 的编码（u5-l1 讲过 `xthread_id` 低 2 位是页标志）：

- `MI_THREADID_ABANDONED = 0`：遗弃、未映射；
- `MI_THREADID_ABANDONED_MAPPED = 4`：遗弃**且**已登记进某 arena 的 `pages_abandoned` 位图，可被分配路径按 size class 找到。

见 [include/mimalloc/types.h:L380-L386](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L380-L386)：

```c
#define MI_THREADID_ABANDONED           MI_ZU(0)
#define MI_THREADID_ABANDONED_MAPPED    (MI_ZU(1) << MI_PAGE_FLAG_BITS)   // = 4
#define MI_THREADID_DETACHED            (MI_ZU(2) << MI_PAGE_FLAG_BITS)
```

判定谓词在 [include/mimalloc/internal.h:L1022-L1029](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1022-L1029)：`mi_page_is_abandoned` 即 `thread_id(page) <= 4`（0 或 4 都算），`mi_page_is_abandoned_mapped` 即恰好等于 4。注意 4 的低 2 位为 0，所以在 `mi_free_nonnull` 的四分支里，对遗弃页的 free 仍走「标志为零的跨线程分支」→ `mi_free_block_mt`，这正是释放时认领的入口（见 [src/free.c:L239-L248](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L239-L248)。

#### 4.1.2 核心流程

abandon 有两个触发源，汇合到同一个函数 `_mi_page_abandon`：

```text
触发源 A：线程退出
  pthread/TLS 析构 → _mi_thread_done (init.c)
    → mi_thread_theaps_done：遍历 tld->theaps 链上每个 theap
      → _mi_theap_collect_abandon(theap)  (theap.c)
        → mi_theap_collect_ex(theap, MI_ABANDON)
          → 逐页 mi_theap_page_collect：
               先 _mi_page_free_collect 更新 used
               全空 → _mi_page_free（直接释放）
               仍有活块 → _mi_page_abandon

触发源 B：页满了（常态路径！）
  分配填满一页 → mi_page_to_full (page.c)
    → theap->allow_page_abandon 为真 → _mi_page_abandon
  （即：满页平时根本不进 full 队列，而是直接遗弃，
    这样其他线程一旦 free 出空位就能把整页拿走）

_mi_page_abandon (page.c)
  ├─ 全空？ → _mi_page_free 释放
  └─ 否则：从页队列摘除
       mi_page_set_theap(page, NULL)   // xthread_id := 0
       page->theap 仍保留原 theap 指针  // 作为「出生地」标记
       → _mi_arenas_page_abandon (arena.c)
           ├─ arena 页且未满 → 登记 pages_abandoned 位图（mapped，xthread_id := 4）
           ├─ 满/单例 arena 页 → 原地等待 free 时认领
           ├─ OS 直配页 → 挂入 heap->os_abandoned_pages 链表
           └─ 最后 mi_abandoned_page_unown：CAS 放弃所有权位
```

注意一个细节：`page->theap` 在遗弃后**并不置 NULL**，而是保留原 theap 指针。这有两个用途：释放时认领要判断「这页是不是从我这个 theap 出去的」（同源认领更激进）；以及 `mi_abandoned_page_try_reclaim` 里通过 heap 的线程本地量找到「当前线程在该 heap 上的 theap」（`_mi_page_associated_theap_peek`，见 [include/mimalloc/prim-tls.h:L412-L422](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h#L412-L422)）。

#### 4.1.3 源码精读

**① 线程退出的 abandon 入口。** [src/init.c:L378-L391](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L378-L391) 中 `mi_thread_theaps_done` 拿着 `tld->theaps_lock` 遍历该线程的所有 theap，逐个 abandon；注释解释了为什么 theap 本身「永不销毁」——静态链接的 dll 之后可能还有 free 调用进来（issue #207）。

**② 逐页判定：释放还是遗弃。** [src/theap.c:L97-L115](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L97-L115) 的 `mi_theap_page_collect`：

```c
_mi_page_free_collect(page, collect >= MI_FORCE);
if (mi_page_all_free(page)) {
  if (collect >= MI_FORCE || page->retire_expire == 0) {
    _mi_page_free(page, pq);          // 全空：直接释放
  }
}
else if (collect == MI_ABANDON) {
  _mi_page_abandon(page, pq);         // 有活块 + 线程退出：遗弃
}
```

`_mi_theap_collect_abandon` 只是一行转发（[src/theap.c:L150-L152](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L150-L152)）。顺带注意 [src/theap.c:L142-L144](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L142-L144)：`MI_ABANDON` 时**不**触发强制 purge——线程退出不该引发全局内存操作。

**③ 页级状态转移。** [src/page.c:L291-L304](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L291-L304) 的 `_mi_page_abandon`：

```c
static void _mi_page_abandon(mi_page_t* page, mi_page_queue_t* pq) {
  _mi_page_free_collect(page, false);        // 收割 thread_free，刷新 used
  if (mi_page_all_free(page)) {
    _mi_page_free(page, pq);                 // 顺手全空了就直接释放
  }
  else {
    mi_page_queue_remove(pq, page);          // 从 theap 的页队列摘除
    mi_theap_t* theap = page->theap;
    mi_page_set_theap(page, NULL);           // xthread_id := MI_THREADID_ABANDONED
    page->theap = theap;                     // 保留出生地指针（同源认领用）
    _mi_arenas_page_abandon(page, theap);    // 去 arena 登记并放所有权
  }
}
```

`mi_page_set_theap`（[include/mimalloc/internal.h:L1008-L1020](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1008-L1020)）用 CAS 更新 `xthread_id`，因为并发线程可能同时在对低 2 位的页标志置位。

**④ 满页即遗弃（常态）。** [src/page.c:L374-L389](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L374-L389) 的 `mi_page_to_full`：`allow_page_abandon` 为真时满页直接 `_mi_page_abandon`。page.c 中 [L461-L463](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L461-L463) 的注释点明：full 队列平时恒空，只有「可销毁的 theap 或用户关掉了 abandon」才走 full 队列路径。`allow_page_abandon` 由选项 `page_full_retain >= 0` 决定（[src/theap.c:L228-L233](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L228-L233)，默认值 2，见 [src/options.c:L162](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L162)）。

**⑤ arena 登记与放所有权。** [src/arena.c:L1304-L1355](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1304-L1355) 的 `_mi_arenas_page_abandon` 分三类处理：

```c
if (page->memid.memkind==MI_MEM_ARENA && !mi_page_is_full(page)) {
  // 非满的 arena 页：登记为「mapped abandoned」，可被分配路径按 bin 找到
  size_t bin = _mi_bin(mi_page_block_size(page));
  ...
  mi_page_set_abandoned_mapped(page);                      // xthread_id |= 4
  const bool was_clear = mi_bitmap_set(arena_pages->pages_abandoned[bin], slice_index);
  mi_atomic_increment_relaxed(&heap->abandoned_count[bin]); // heap 级快速计数
  mi_theapx_stat_increase(heap, current_theapx, pages_abandoned, 1);
  mi_abandoned_page_unown(page, current_theapx);           // 放所有权
  return;
}
// 满/单例 arena 页：原地等待 free 时认领；
// OS 直配页：挂入 heap->os_abandoned_pages 双向链表（持锁）
```

最后一步 `mi_abandoned_page_unown`（[src/arena.c:L631-L652](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L631-L652)）是一个 CAS 循环：把 `xthread_free` 的所有权位从 1 清成 0；若此刻 `xthread_free` 链上还有并发 free 推进来的块，就先 collect 一次——万一因此全空，立即 unabandon + free。顺序很讲究：**先登记位图、后放所有权**，保证页一旦可被认领，就一定能通过位图找到它。

#### 4.1.4 代码实践

**实践：亲眼看到线程退出产生 abandoned 页。**

1. 实践目标：验证「工作线程带着未释放的块退出」会让页面进入 abandoned 状态，统计输出出现非零 `abandoned`。
2. 操作步骤（示例代码，需自行保存为 `abandon_demo.c`）：

```c
/* 示例代码：工作线程留下 8 个活块后退出 */
#include <mimalloc.h>
#include <pthread.h>
#include <stdio.h>

static void* keep[8];              /* 由主线程持有，故意不释放 */

static void* worker(void* arg) {
  (void)arg;
  void* p[64];
  for (int i = 0; i < 64; i++) p[i] = mi_malloc(100);  /* 同一 size class */
  for (int i = 0; i < 8;  i++) keep[i] = p[i];         /* 转移 8 块给主线程 */
  for (int i = 8; i < 64; i++) mi_free(p[i]);          /* 其余本线程释放 */
  return NULL;                    /* 线程退出 → 其 theap 页面被 abandon */
}

int main(void) {
  pthread_t t;
  pthread_create(&t, NULL, worker, NULL);
  pthread_join(t, NULL);
  /* 此处 keep[0..7] 仍是活块；进程退出时统计会体现 abandoned */
  return 0;
}
```

   用 debug 构建的库编译链接，然后运行：`MIMALLOC_SHOW_STATS=1 ./abandon_demo`。
3. 需要观察的现象：`pages` 段的 `abandoned` 行（见 [src/stats.c:L386-L402](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L386-L402)）以及 `process` 段的峰值统计。
4. 预期结果：出现非零的 abandoned 计数；若在 `pthread_join` 后补一段「主线程分配同尺寸块」，还能观察到 `reclaima`（分配时认领）增长。具体数值取决于线程调度，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_mi_page_abandon` 里 `mi_page_set_theap(page, NULL)` 之后还要写回 `page->theap = theap`？两行不就互相抵消了吗？

**答案**：`mi_page_set_theap(NULL)` 改的是原子字段 `xthread_id`（置为 `MI_THREADID_ABANDONED`），使 free 快路径的四分支判定把它当「无属主页」处理；而 `page->theap` 是普通指针字段，置 NULL 仅仅丢失信息。保留它作为「出生地」标记，是为了释放时认领能区分「同源认领」（`theap == page->theap`，限制更宽松，`page_max_reclaim` 默认 -1 即不限）与「跨线程认领」（默认每 size class 至多 32 页，见 4.3 节）。

**练习 2**：满页被 abandon 后既不在任何 theap 的页队列里、又不在 `pages_abandoned` 位图里（满页不登记），其他线程怎么找到它？

**答案**：不需要「按页」找它。其他线程只要 free 了其中任意一块，就会沿指针→page map/对齐反查→`mi_free_block_mt` 这条路碰到该页，顺手认领所有权并触发 `mi_free_try_collect_mt`（见 4.3.2）。也就是说，满页的认领索引就是**块指针本身**，免费的。

**练习 3**：`MI_THREADID_ABANDONED_MAPPED` 为什么偏偏选 4？

**答案**：`xthread_id` 的低 2 位（`MI_PAGE_FLAG_MASK = 0x03`）被用作页标志（in_full_queue、has_interior_pointers）。真实线程 id 低 2 位恒为 0（u5-l1），因此 0 与 4 这两个低 2 位为零的值可以安全充当「伪线程 id」，使 `mi_free_nonnull` 的一次 XOR 判定无需任何特判就能把遗弃页正确分流到跨线程分支。

### 4.2 abandoned 位图：arena 级索引与 heap 级计数

#### 4.2.1 概念说明

非满的 arena 页被遗弃后，要解决「谁来找」的问题。mimalloc 的答案是把索引挂在 **heap × arena × size class** 三个维度上：

- 每个 heap 对每个 arena 持有一份 `mi_arena_pages_t`（惰性创建，主 heap 直接用 arena 内嵌的 `pages_main`），里面有：
  - `pages`：该 arena 中属于此 heap 的所有页（含遗弃页）的登记位图；
  - `pages_abandoned[MI_ARENA_BIN_COUNT]`：**每个常规 size class bin 一张遗弃位图**，置位的 bit 对应页起始 slice 的下标。
- `heap->abandoned_count[bin]`：原子计数，做「有没有必要去扫位图」的快速预判。

结构定义见 [include/mimalloc/types.h:L723-L727](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L723-L727)：

```c
struct mi_arena_pages_s {
  mi_bitmap_t* pages;                // all registered pages (abandoned and owned)
  mi_bitmap_t* pages_abandoned[MI_ARENA_BIN_COUNT];  // abandoned pages per size bin
};
```

以及 [include/mimalloc/types.h:L618-L636](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L618-L636) 中 `mi_heap_s` 的三个相关字段：`abandoned_count[MI_BIN_COUNT]`、`os_abandoned_pages`（OS 直配遗弃页链表）、`arena_pages[MI_MAX_ARENAS]`。

`MI_ARENA_BIN_COUNT = MI_MAX_SINGLETON_BIN + 1 = 61`（64 位启用大页时，见 [types.h:L484-L493](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L484-L493) 与 [types.h:L716](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L716)），恰好覆盖全部非单例 bin——单例页（huge）与满页本来就不进位图。

按 bin 分图的收益：认领者需要的是「**我这个 size class** 的页」，分图后一次位图查找只会命中尺寸完全匹配的页，`mi_assert_internal(mi_page_block_size(page) == block_size)`（arena.c:771）就是这条不变式的断言。

#### 4.2.2 核心流程

```text
登记（abandon 侧）：
  slice_index 位置 1 于 pages_abandoned[bin]      （原子的 mi_bitmap_set）
  heap->abandoned_count[bin] + 1
  xthread_id |= 4 （abandoned-mapped）

查找（认领侧，分配慢路径）：
  heap->abandoned_count[bin] == 0 ?  → 直接放弃，零成本
  否则遍历合适 arena（复用 mi_forall_suitable_arenas，
    按 heap/线程错开起始下标以减少争用）：
    mi_bitmap_try_find_and_claim(pages_abandoned[bin], ...)
      ① 在位图里原子地找到并清掉一个置位 bit（先占坑）
      ② 对该 slice 的页调 mi_page_claim_ownership（原子 OR 所有权位）
      ② 失败（有并发 free 先认领了）→ 把 bit 置回去，换下一个
      ② 成功 → 认领完成
```

#### 4.2.3 源码精读

**① 分配时的认领查找。** [src/arena.c:L725-L779](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L725-L779) 的 `mi_arenas_page_try_find_abandoned`（节选）：

```c
const size_t bin = _mi_bin(block_size);
if (bin >= MI_ARENA_BIN_COUNT) return NULL;            // 单例档不查位图
if (mi_atomic_load_relaxed(&heap->abandoned_count[bin]) == 0) return NULL;  // 快速预判

mi_forall_suitable_arenas(heap, req_arena, tseq, ..., arena) {
  mi_bitmap_t* const bitmap = arena_pages->pages_abandoned[bin];
  if (mi_bitmap_try_find_and_claim(bitmap, tseq, &slice_index,
                                   &mi_arena_try_claim_abandoned, arena)) {
    ...
    mi_atomic_decrement_relaxed(&heap->abandoned_count[bin]);
    mi_theap_stat_decrease(theap, pages_abandoned, 1);
    mi_theap_stat_counter_increase(theap, pages_reclaim_on_alloc, 1);   // ← "reclaima"
    _mi_page_free_collect(page, false);                // 刷新 used
    return page;
  }
}
```

它的调用者是 [src/arena.c:L1130-L1144](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1130-L1144) 的 `mi_arenas_page_regular_alloc`：**第 1 步先找遗弃页，找不到才第 2 步切新 slice 建新页**——复用优先于扩张。

**② 认领回调与「占坑—复核」两段式。** [src/arena.c:L655-L672](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L655-L672)：

```c
static bool mi_arena_try_claim_abandoned(size_t slice_index, mi_arena_t* arena, bool* keep_abandoned) {
  mi_page_t* const page = mi_arena_page_at_slice(arena, slice_index);
  if (!mi_page_claim_ownership(page)) {
    *keep_abandoned = true;    // 有并发 free 正在认领此页：必须把位图 bit 置回去！
    return false;
  }
  else {
    *keep_abandoned = false;   // 认领成功：bit 保持清零
    return true;
  }
}
```

位图侧的配套实现在 [src/bitmap.c:L1340-L1380](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1340-L1380)：`mi_bitmap_try_find_and_claim_visit` 先 `mi_bchunk_try_find_and_clear` 原子清位「占坑」，再调上面的回调；失败且 `keep_set` 时把位重新置回去。

**③ 为什么失败时必须把 bit 置回去——那次罕见的 busy-wait。** 并发 free 一侧的 `mi_free_try_collect_mt` 在尝试释放/认领前会调 `_mi_arenas_page_unabandon`（见 4.3 节），其中清位图用的是 [src/arena.c:L1406](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1406) 的 `mi_bitmap_clear_once_set`。这个「必须是 1 才能清」的语义在 [src/bitmap.c:L109-L129](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L109-L129)：

```c
// Clear a bit but only when/once it is set. This is used by concurrent free's while
// the page is abandoned and mapped. This can incur a busy wait :-( but it should
// be quite rare (and is accounted for in the stats)
static inline void mi_bfield_atomic_clear_once_set(...) {
  ...
    if ((old&mask)==0) {
      mi_subproc_stat_counter_increase(subproc, pages_unabandon_busy_wait, 1);  // ← "waits"
      while ((old&mask)==0) { _mi_prim_thread_yield(); old = mi_atomic_load_acquire(b); } // 自旋
    }
  ...
}
```

时间线是：认领线程 A 先清了位图 bit（占坑）→ free 线程 B 抢先夺得页所有权并调 `clear_once_set`，发现 bit 已是 0，自旋等待 → A 的 `mi_page_claim_ownership` 失败，把 bit 置回去（`keep_abandoned=true`）→ B 的等待结束，继续释放/认领。若 A 不置回，B 将永久自旋——这正是 arena.c 注释「it is very important to set the abandoned bit again」的含义。统计行 `waits`（`pages_unabandon_busy_wait`）专门度量这个事件，正常应接近 0。

#### 4.2.4 代码实践

**实践：读统计输出认路标。**

1. 实践目标：把 `MIMALLOC_SHOW_STATS=1` 的 `pages` 段各计数行与本讲函数一一对应。
2. 操作步骤：任取一个多线程程序（可直接用仓库自带的压测，见第 5 节），运行并保存输出；对照 [src/stats.c:L386-L402](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L386-L402) 逐行标注。
3. 需要观察的现象：`pages` 段中下列行的含义——`abandoned`（当前/峰值遗弃页数）、`reclaima`（`pages_reclaim_on_alloc`，arena.c:763）、`reclaimf`（`pages_reclaim_on_free`，free.c:474）、`reabandon`（`pages_reabandon_full`，arena.c:1376）、`waits`（busy-wait 次数，bitmap.c:120）、`retire`（`pages_retire`，page.c:444）。
4. 预期结果：能画出「每行统计 ↔ 源码中计数点」的对照表；`waits` 通常为 0 或极小。具体数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `pages_abandoned` 要按 bin 分成 61 张位图，而不是一张总位图？

**答案**：认领请求总是携带确定的 `block_size`（来自分配慢路径的页队列查找）。按 bin 分图使得查找只命中尺寸精确匹配的页（源码断言 `mi_page_block_size(page) == block_size`），无需在认领后再检查尺寸、也不存在「找到的页不合适还得置回」的常规路径浪费；同时不同 size class 的认领天然分散到不同位图，降低了位图内部的原子争用。

**练习 2**：`heap->abandoned_count[bin]` 与 `pages_abandoned` 位图中的置位个数是否恒等？

**答案**：正常路径下是同步维护的（登记时 +1/置位，认领或 unabandon 时 -1/清位），两者语义一致；`abandoned_count` 的价值只是让 `mi_arenas_page_try_find_abandoned` 在常见情形（该 bin 无遗弃页）下一次 relaxed 原子读就返回，省掉遍历 arena 与位图查找。

### 4.3 reclaim：分配时认领与释放时认领

#### 4.3.1 概念说明

认领（reclaim）= 某线程取得遗弃页的所有权，把页重新挂回**自己** theap 的对应页队列。有两条路径：

| 路径 | 触发时机 | 源码入口 | 统计行 |
| --- | --- | --- | --- |
| 分配时认领 | 分配慢路径需要新页时，先按 bin 查遗弃位图 | `mi_arenas_page_try_find_abandoned`（arena.c:725） | `reclaima` |
| 释放时认领 | 任何线程 free 了遗弃页中的一个块，顺手夺所有权 | `mi_free_block_mt` → `mi_free_try_collect_mt` → `mi_abandoned_page_try_reclaim`（free.c:428） | `reclaimf` |

释放时认领是 u5-l2 讲过的「所有权位顺手认领」的直接应用：`mi_free_block_mt` 推块入 `xthread_free` 时，若 `allow_collect` 且原值不含所有权位（`!mi_tf_is_owned(tf_old)`，即页无主），就把新值标为 owned——**一次 CAS 同时完成「挂块」与「夺权」**。见 [src/free.c:L80-L96](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L80-L96)：

```c
do {
  mi_block_set_next(page, block, mi_tf_block(tf_old));
  const bool new_owned = (allow_collect ? true : mi_tf_is_owned(tf_old));
  tf_new = mi_tf_create(block, new_owned);
} while (!mi_atomic_cas_weak_acq_rel(&page->xthread_free, &tf_old, tf_new));

if (allow_collect) {
  const bool is_owned_now = !mi_tf_is_owned(tf_old);   // 原来无主 → 我们刚刚夺到了
  if (is_owned_now) {
    mi_assert_internal(mi_page_is_abandoned(page));
    mi_free_try_collect_mt(page, block);
  }
}
```

#### 4.3.2 核心流程

夺到所有权后，`mi_free_try_collect_mt`（[src/free.c:L480-L515](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L480-L515)）按严格的优先级做四选一：

```text
先刷新 used（小页用免原子 的 _mi_page_free_collect_partly，
             大页用常规 _mi_page_free_collect）
然后依次尝试：
  1. mi_abandoned_page_try_free      —— 全空？ unabandon + 释放整页
  2. mi_abandoned_page_try_reclaim   —— 值得收编？ unabandon + 挂回我的 theap 队列
  3. mi_abandoned_page_try_reabandon_to_mapped
                                     —— 原「满页遗弃」被 free 出空位（不再 mostly used）？
                                        重新登记为 mapped abandoned，
                                        让分配路径今后能按 bin 找到它
  4. 都不行 → mi_abandoned_page_unown_from_free：再放掉所有权
```

「值得收编」的判断（`mi_abandoned_page_try_reclaim`，[src/free.c:L428-L476](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L428-L476)）由三个阈值共同决定：

- 选项 `page_reclaim_on_free`（默认 0）：`-1` 完全禁用释放时认领；`0` 只允许**同源**认领（页的出生 theap 就是当前 theap）；`1` 才允许跨 theap 认领（[src/options.c:L161](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L161)）。对应开关 `theap->allow_page_reclaim`（[src/theap.c:L228-L233](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L228-L233)）。
- 同源上限 `page_max_reclaim`（默认 -1，不限）与跨线程上限 `page_cross_thread_max_reclaim`（默认 32）：该 theap 在此 size class 的页队列长度超过上限就不收编（[src/options.c:L168-L171](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L168-L171)、默认值宏在 [src/options.c:L95-L100](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L95-L100)）。
- 页不能太满（`!mi_page_is_mostly_used`，即空闲块多于 \( \frac{1}{8} \) 页，判据 `reserved - used > reserved/8`，见 [include/mimalloc/internal.h:L924-L929](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L924-L929)）——收编一个快满的页对复用无益。

free.c 里 [L428-L435](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L428-L435) 的注释解释了这套保守设计的动机：认领能显著改善 `larson`、`rbtree-ck` 这类「同源释放」基准；但多线程下过度跨线程认领会各自囤页、降低线程间复用，因此跨线程认领默认关（`reclaim_on_free == 1` 才开），主要靠分配时认领兜底。

收编动本身在 [src/page.c:L277-L289](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L277-L289) 的 `_mi_theap_page_reclaim`：设 theap、collect 刷新 used、把页 push 到对应 bin 队列**尾部**（复用优先级最低，先消耗队首更满的页）。分配时认领也汇到这里：[src/page.c:L319-L334](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L319-L334) 的 `mi_page_fresh_alloc` 发现 `_mi_arenas_page_alloc` 返回的是遗弃页时，先 reclaim，再按需 `mi_page_extend_free` 补 free list。

#### 4.3.3 源码精读（四选一的完整代码）

[src/free.c:L500-L515](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L500-L515)：

```c
const long reclaim_on_free = _mi_option_get_fast(mi_option_page_reclaim_on_free);
// try to: 1. free it, 2. reclaim it, or 3. reabandon it to be mapped
if (mi_abandoned_page_try_free(page)) return;
if (page->block_size <= MI_MEDIUM_MAX_OBJ_SIZE && reclaim_on_free >= 0) {
  if (mi_abandoned_page_try_reclaim(page, reclaim_on_free)) return;
}
if (mi_abandoned_page_try_reabandon_to_mapped(page)) return;
// otherwise unown the page again
mi_abandoned_page_unown_from_free(page, mt_free);
```

三个 helper 分别是：

- **try_free**（[src/free.c:L372-L379](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L372-L379)）：`mi_page_all_free` 时先 `_mi_arenas_page_unabandon`（从位图/OS 链表摘除，可能忙等）再 `_mi_arenas_page_free` 释放 slice。
- **try_reclaim**（[src/free.c:L428-L476](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L428-L476)）：通过 `_mi_page_associated_theap_peek(page)` 拿「当前线程在该页所属 heap 上的 theap」——注意这不是 `page->theap`（那是出生地），而是查 heap 的线程本地量（[include/mimalloc/prim-tls.h:L414-L422](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h#L414-L422)）；随后 `theap == page->theap` 判同源，选 `page_max_reclaim` 或（跨线程时）`page_cross_thread_max_reclaim` 作队列长度上限；成功则 unabandon + `_mi_theap_page_reclaim` + `pages_reclaim_on_free` 计数。另有两道护栏：`_mi_thread_is_initialized()`（线程已终止则不认领，防 TLS 析构期重初始化，issue #944）与 `theap->allow_page_reclaim`（issue #1289）。
- **try_reabandon_to_mapped**（[src/free.c:L382-L391](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L382-L391) → [src/arena.c:L1359-L1381](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1359-L1381)）：只对「不再 mostly used 的非满 arena 页」生效，走 `_mi_arenas_page_abandon` 重新登记（计数 `pages_reabandon_full`）。这一步的意义：满页遗弃时**不在**位图里，一旦被 free 出了可观的空位却仍无人收编，就升级为 mapped，此后分配路径能按 bin 找到它——避免「半空的满页」永远只能靠碰运气的 free 来认领。

#### 4.3.4 代码实践

**实践：用 `MIMALLOC_PAGE_RECLAIM_ON_FREE` 做对照实验。**

1. 实践目标：验证 `page_reclaim_on_free` 三档取值对 `reclaimf` 计数的影响，并解释原因。
2. 操作步骤：
   - 准备一个双线程程序：线程 A 分配 100 万个 32 字节块后，把指针数组交给线程 B；线程 B 全部 free（跨线程 free 洪水，正好反复触发遗弃页上的释放）。
   - 分别以 `MIMALLOC_PAGE_RECLAIM_ON_FREE=-1`、`=0`（默认）、`=1` 运行，各跑 3 次取 `pages` 段的 `reclaimf / reclaima / abandoned` 中位数。
3. 需要观察的现象：`-1` 时 `reclaimf` 恒为 0（认领被完全禁用）；`=0` 时 `reclaimf` 可能非零但仅限同源；`=1` 时 `reclaimf` 明显上升，同时 `abandoned` 峰值下降、总耗时可能变化。
4. 预期结果：`reclaimf(=1) ≥ reclaimf(=0) ≥ reclaimf(-1)=0`；`=1` 档的耗时既可能变好（同源 free 免原子）也可能变差（跨线程收编引发队列维护），这正是源码注释所说的取舍。具体数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`mi_free_try_collect_mt` 里四个尝试的顺序为什么是 free → reclaim → reabandon → unown？换成 reclaim 优先会怎样？

**答案**：free 是唯一能立刻归还内存的终点，条件（全空）又最客观，理应最先；reclaim 把页留在本线程，收益取决于队列上限与页的满度，属次优；reabandon 不改变「无主」状态、只是补登记，供未来分配路径使用；unown 是什么都不做的兜底。若 reclaim 优先，一个恰好全空的页会被白白收编再等下次 collect 才释放，多付一次队列操作与一次 unabandon，还推迟了 slice 归还。

**练习 2**：跨线程认领默认是关的（`page_reclaim_on_free=0` 时仅同源），那跨线程场景下遗弃页主要靠什么回到流通？

**答案**：主要靠**分配时认领**：任何线程的分配慢路径要新页时都会先查 `pages_abandoned[bin]` 位图（`mi_arenas_page_regular_alloc` 的第 1 步）。释放时认领只是锦上添花的加速——它让「碰巧 free 到遗弃页」的线程省掉未来的位图查找与页初始化。

### 4.4 retire 与 purge：两条「延时归还」路径

#### 4.4.1 概念说明

abandon 解决「有活块的页」，retire 与 purge 解决另外两类残留：

- **retire（退役）**：**本线程**把一页 free 到全空（`used==0`）时不立即释放，而是给页打上 `retire_expire` 倒计时，让它以「队首候选页」的身份再留一小会儿。这是滞回（hysteresis）机制：真实负载里「全部释放紧接着又分配」极常见（u5-l1 的 free 快路径在 `used==0` 时触发），立刻归还 slice 会引发「归还—重切—重 commit」的抖动，反而抬高 RSS 与系统调用量。
- **purge（净化）**：slice 已归还给 arena（`slices_free` 置位）后，其**物理页**并不马上还给 OS，而是登记进 `slices_purge` 位图并设一个到期时间戳；到期的 arena 在管理节拍中被逐 chunk 检查，对到期区间做 decommit 或 reset（MADV_DONTNEED/MADV_FREE，语义差异由 u6-l2 的能力表吸收）。延迟 purge 是 mimalloc「eager page purging」卖点的一半——另一半是它确实会做，不像某些分配器把脏页永远攥在手里。

三者时间尺度对比：

| 机制 | 对象 | 触发 | 延迟量级 | 归还物 |
| --- | --- | --- | --- | --- |
| abandon | 有活块的页 | 线程退出/页满 | 无（立即登记） | 所有权 |
| retire | 全空的页 | 本线程 free 到 `used==0` | 16 或 4 个管理节拍 | slice（经 `_mi_page_free`） |
| purge | 已空闲的 slice 区间 | `_mi_arenas_free` 归还 slice | `purge_delay × arena_purge_mult`（默认 1000ms×4=4s） | 物理页（decommit/reset） |

#### 4.4.2 核心流程

```text
retire：
  mi_free_block_local: used 减到 0
    → _mi_page_retire (page.c)
        条件满足（队列页数 ≤ 3、非特殊队列、唯一页或小块）？
          是 → retire_expire := 16（小对象页）或 4（更大页），留在队首
          否 → _mi_page_free 立即释放
  之后每个管理节拍：
    _mi_theap_collect_retired(theap, force)
      → 对每个 [page_retired_min, page_retired_max] 区间内的 bin 队首页：
           mi_page_try_retire：retire_expire 减一
             仍全空且减到 0（或 force）→ _mi_page_free
             期间又被分配占用 → retire_expire := 0（撤销退役）

purge：
  _mi_arenas_page_free → _mi_arenas_free（arena.c）
    → mi_arena_schedule_purge(arena, slice_index, slice_count)
        delay<0 或 pinned → 不 purge
        delay==0          → 立即 mi_arena_purge
        delay>0           → slices_purge 置位 + arena->purge_expire := now+delay
  分配慢路径 / collect 的节拍：
    _mi_arenas_collect(force, visit_all, tld) → mi_arenas_try_purge
      subproc->purge_expire 未到且非 force → 直接返回（零成本检查）
      到期 → 一次只允许一个线程逐 arena 扫 slices_purge，
             对每个到期区间调 mi_arena_purge（decommit/reset）并清位
```

#### 4.4.3 源码精读

**① retire 常量与判定。** [src/page.c:L414-L415](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L414-L415)：

```c
#define MI_RETIRE_CYCLES      (16)   /* keep a retired page around for about 16 "admin cycles" before free'ing it */
#define MI_RETIRE_MAX_PAGES   (3)    /* keep at most N pages per size bin as retired */
```

[src/page.c:L424-L457](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L424-L457) 的 `_mi_page_retire`（节选）：

```c
if (page->retire_expire!=0) return;                 // 已退役的不重复退役
...
mi_page_queue_t* pq = mi_page_queue_of(page);
const size_t bsize = mi_page_block_size(page);
if mi_likely( pq->count <= MI_RETIRE_MAX_PAGES && !mi_page_queue_is_special(pq)) {
  if (pq->count==1 || bsize < MI_SMALL_SIZE_MAX) {  // 唯一页，或 <1KiB 的小块
    mi_theap_stat_counter_increase(theap, pages_retire, 1);   // ← "retire"
    page->retire_expire = (bsize <= MI_SMALL_MAX_OBJ_SIZE ? MI_RETIRE_CYCLES : MI_RETIRE_CYCLES/4);
    ...  // 登记 bin 区间 [page_retired_min, page_retired_max]
    return;   // 不释放
  }
}
_mi_page_free(page, pq);
```

要点：小块页（≤10KiB 对象）等 16 拍，更大的页只等 4 拍（越大的页占内存越多，不宜久留）；每 bin 至多 3 页处于退役态。倒计时的推进者是 [src/page.c:L481-L518](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L481-L518) 的 `_mi_theap_collect_retired`：它只扫 `[page_retired_min, page_retired_max]` 区间内每条队列的**队首**至多 `MI_RETIRE_MAX_PAGES` 页——因为退役页总是停在队首，这是一个有界的小扫描，由分配慢路径的管理节拍调用（page.c 中 `mi_find_page` 与 collect 路径的多处调用，如 [L856](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L856)、[L1038](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L1038)）。`mi_page_try_retire` 里若页在退役期间被重新分配（不再全空），直接 `retire_expire = 0` 撤销退役。

**② purge 的延迟计算。** [src/arena.c:L2242-L2252](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L2242-L2252)：arena 侧有效延迟 = `purge_delay × arena_purge_mult`（默认 1000ms × 4 = 4000ms；两者任一为负则整体关闭，任一为 0 则立即）。u2-l3 讲过的环境变量映射在这里生效：`MIMALLOC_PURGE_DELAY=-1` 关、`=0` 立即、正值延迟。

**③ 归还 slice 时调度 purge。** [src/arena.c:L1467-L1475](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1467-L1475)（`_mi_arenas_free` 中）：

```c
// potentially decommit
if (!arena->memid.is_pinned /* && !arena->memid.initially_committed */) {
  mi_arena_schedule_purge(arena, slice_index, slice_count);   // 先调度 purge
}
// and make it available to others again
bool all_inuse = mi_bbitmap_setN(arena->slices_free, slice_index, slice_count);
```

注意顺序：purge 的调度发生在 slice 重新进入 `slices_free` **之前**——被 purge 的区间必须是「我们仍持有」的状态（`mi_arena_purge` 的断言明确要求 `slices_free` 中该区间为未占用）。调度实现 [src/arena.c:L2288-L2311](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L2288-L2311)：立即档直接 `mi_arena_purge`；延迟档用 CAS 把 `arena->purge_expire` 从 0 设为 `now+delay`（已设则不动，取更早的到期者），同时 `slices_purge` 置位。

**④ 到期执行。** [src/arena.c:L2389-L2435](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L2389-L2435) 的 `mi_arenas_try_purge`：先做两级零成本预检（`subproc->purge_expire` 未到且非 force 直接返回）；然后用一个进程级标志保证同一时刻只有一个线程在 purge；从 `tseq % max_arena` 起旋转访问 arena（继续贯彻「线程错开起点」的减争用策略），每轮默认只访问约 1/4 的 arena（`max_arena/4+1`），除非 `visit_all`。真正的物理操作在 [src/arena.c:L2257-L2283](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L2257-L2283) 的 `mi_arena_purge`：调 `_mi_os_purge_ex`（按 `MIMALLOC_PURGE_DECOMMITS` 选择 decommit 或 reset），并维护 `slices_committed` 位图与 `purged` 统计。驱动源是 [src/arena.c:L1493-L1495](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1493-L1495) 的 `_mi_arenas_collect`，它在 theap collect（[src/theap.c:L142-L144](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L142-L144)）与进程退出时被调用。

#### 4.4.4 代码实践

**实践：观察 purge 延迟对 RSS 的影响。**

1. 实践目标：用三种 `MIMALLOC_PURGE_DELAY` 配置对比「峰值 RSS / 稳态 RSS / purged 统计」，直观理解延迟 purge 的取舍。
2. 操作步骤（示例代码，保存为 `purge_demo.c`）：

```c
/* 示例代码：反复分配并整体释放一批中等大小的块 */
#include <mimalloc.h>
int main(void) {
  for (int round = 0; round < 200; round++) {
    void* p[512];
    for (int i = 0; i < 512; i++) p[i] = mi_malloc(4096);   /* 2MiB/轮 */
    for (int i = 0; i < 512; i++) mi_free(p[i]);
  }
  return 0;
}
```

   分别运行：`MIMALLOC_PURGE_DELAY=-1 ./purge_demo`（不 purge）、`MIMALLOC_PURGE_DELAY=1000 ./purge_demo`（默认档，arena 侧约 4s）、`MIMALLOC_PURGE_DELAY=0 ./purge_demo`（立即）。运行期间在另一终端周期性读取 `/proc/<pid>/status` 的 `VmRSS`（或用 `/usr/bin/time -v` 取峰值）。
3. 需要观察的现象：`-1` 档 RSS 基本不回落；`=1000` 档在轮次间的短暂停顿后会台阶式回落；`=0` 档回落最快但总耗时最高（每轮都付 decommit + 下轮重 commit 的代价）。同时看统计输出 `arenas` 段的 `purged` 与 `arenas_purges`。
4. 预期结果：峰值 RSS 三档接近，稳态 RSS 与耗时的排序为：稳态 `=0` ≤ `=1000` ≤ `-1`，耗时 `=0` ≥ `=1000` ≈ `-1`。具体数值待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：retire 的倒计时单位是什么？「16 个 admin cycle」大约对应多少次分配？

**答案**：单位是分配慢路径的管理节拍——每约 1000 次 generic 分配推进一拍（u4-l2/u5-l3 的节拍器）。16 拍 ≈ 16000 次 generic 分配的窗口（量级估计，实际还与 force collect、`mi_collect` 主动调用有关）。设计意图是「比一次突发分配的典型间隔略长」，让常见的 free-then-malloc 模式命中仍退役的页而无需重建。

**练习 2**：为什么 `mi_arena_schedule_purge` 里 `delay==0` 与 `delay>0` 走完全不同的代码路径，而不统一走「到期时间为 now」的调度？

**答案**：`delay==0` 时没有「等待合批」的收益，立即 purge 可以让调用者直接看到物理页归还（也简化统计：不需要位图与时间戳）；而 `delay>0` 的全部意义就在于合批——把短时间内多次 slice 归还合并成一次 `mi_arena_purge` 的区间操作，并用 `arena->purge_expire`/`subproc->purge_expire` 让无关线程零成本跳过。统一成调度路径反而要处理「已过期但无人推进」的边界。

**练习 3**：满页被 abandon 时既没进 `pages_abandoned` 位图、也不算 retired，它的 slice 何时才可能回到 arena？

**答案**：只有当它的块陆续被 free、某次 free 触发 `mi_free_try_collect_mt` 的四选一：全空则 try_free 释放 slice；被收编则变回普通页走常规路径；否则 try_reabandon_to_mapped 升级为 mapped 遗弃等待分配时认领。在这之前它的 slice 一直被占用（`slices_free` 对应位为 0），也绝不会成为 purge 候选——purge 只处理已归还给 arena 的空闲区间。

## 5. 综合实践

**任务：用官方压测 test-stress 做一次 abandon/reclaim 诊断实验。**

test/test-stress.c 的说明注释（[test/test-stress.c:L7-L16](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-stress.c#L7-L16)）明确列出了它的负载特征：多线程、指针跨线程转移、**线程反复终止与重建且期间保留部分存活对象**——这正是本讲机制的目标工况。默认参数（32 线程、SCALE 50、ITER 50 轮重建线程，见 [test/test-stress.c:L38-L73](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-stress.c#L38-L73)）。

1. 构建（沿用 u1-l2 的 debug 构建，统计最全；产物名 `mimalloc-test-stress`）：

   ```bash
   mkdir -p out/debug && cd out/debug
   cmake -DCMAKE_BUILD_TYPE=Debug ../..
   cmake --build . --target mimalloc-test-stress
   ```

2. 基线运行并保存统计：

   ```bash
   MIMALLOC_SHOW_STATS=1 ./mimalloc-test-stress 2>&1 | tee stress-baseline.txt
   ```

   在 `pages` 段找到 `abandoned / reclaima / reclaimf / reabandon / waits / retire` 各行，并核对它们的计数点：reclaima ← arena.c 的分配时认领、reclaimf ← free.c 的释放时认领、reabandon ← 满页降级重登记、waits ← bitmap.c 的忙等、retire ← page.c 的退役。
3. 改变认领策略重跑：

   ```bash
   MIMALLOC_PAGE_RECLAIM_ON_FREE=-1 MIMALLOC_SHOW_STATS=1 ./mimalloc-test-stress 2>&1 | tee stress-noreclaim.txt
   MIMALLOC_PAGE_RECLAIM_ON_FREE=1  MIMALLOC_SHOW_STATS=1 ./mimalloc-test-stress 2>&1 | tee stress-crossreclaim.txt
   ```

   对比三份输出的 `reclaimf`、`abandoned` 峰值与总耗时，用 4.3 节的源码逻辑解释差异。
4. （可选进阶）把 `MIMALLOC_PURGE_DELAY` 设为 `-1 / 1000 / 0` 各跑一遍，观察 `arenas` 段 `purged` 行与进程峰值内存的变化，对应 4.4 节的调度代码。
5. 交付物：一页实验报告——三组配置的统计表 + 每个数字的源码解释 + 一段「如果我的服务也像 test-stress 一样频繁创建销毁线程，我该开还是关跨线程认领」的结论。运行结果待本地验证。

## 6. 本讲小结

- **abandon 是状态而非释放**：线程退出（经 `_mi_theap_collect_abandon`）或页面变满（`mi_page_to_full`）时，仍有活块的页被改写 `xthread_id` 为 0（普通遗弃）或 4（mapped 遗弃）、登记进 arena 位图、最后用 CAS 放弃 `xthread_free` 的所有权位；slice 一个都不还。
- **索引分三层**：heap 的 `abandoned_count[bin]` 原子计数做零成本预判 → heap×arena 的 `pages_abandoned[bin]` 位图按 size class 精确寻址 → 满/单例页不进位图，靠「块指针即索引」的释放路径碰面。
- **两条认领路径**：分配时认领在 `mi_arenas_page_regular_alloc` 的第 1 步查位图（reclaima）；释放时认领由 `mi_free_block_mt` 的一次 CAS「挂块+夺权」触发，随后 `mi_free_try_collect_mt` 按 free → reclaim → reabandon → unown 的优先级四选一（reclaimf / reabandon）。
- **并发认领与并发 free 的碰撞**由 `mi_bitmap_clear_once_set` 的忙等化解：认领失败方必须把位图 bit 置回，否则 free 方会永久自旋；该事件由 `waits` 统计，正常应接近 0。
- **认领策略是可调的**：`page_reclaim_on_free`（-1/0/1）× `page_max_reclaim`（默认不限）× `page_cross_thread_max_reclaim`（默认 32）共同控制「谁、收多少」，默认偏保守——跨线程主要靠分配时认领。
- **两条延时归还路径**：retire 给全空页 16/4 个管理节拍的滞回（防释放-重分配抖动）；purge 给已归还 slice 约 `purge_delay×4` 的合批窗口后再 decommit/reset（稳态 RSS 与系统调用量的平衡）。

## 7. 下一步学习建议

本讲补完了内存生命周期「分配 → 使用 → 释放 → 遗弃/认领 → 退役 → 净化」的最后几环。接下来建议：

1. **u7-l1（初始化流程）**：看 `mi_process_done` 与 `_mi_thread_done` 如何在进程/线程退出时收尾，本讲的 abandon 正是嵌在 TLS 析构这条链上。
2. **u7-l4（subproc）**：abandon/reclaim 的全部结构都按 subproc 隔离（`subproc->arenas`、heap 链），理解 CPython 多解释器如何复用这套机制。
3. **u9-l3（统计系统）**：本讲反复引用的 `pages` 段各计数行，将在统计讲义中系统拆解 `mi_stat_count_t` 的 peak/total/current 语义与 theap→heap 的合并路径。
4. 若想继续读源码，推荐沿 `mi_arena_try_claim_abandoned` → `mi_bitmap_try_find_and_claim_visit` 这条线把 u8-l3（位图内部）的 `mi_bchunk_try_find_and_clear` 读透——它是本讲「占坑—复核」两段式认领的原子地基。
