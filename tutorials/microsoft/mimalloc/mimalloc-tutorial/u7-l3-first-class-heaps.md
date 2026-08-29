# 一等堆：mi_heap_new/delete/destroy 与跨线程分配

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 v3 中「一等堆（first-class heap）」的准确含义：一个 `mi_heap_t` 可以被**任意线程**用来分配、也可以被任意线程释放，而不再像 v1/v2 那样只能在创建它的线程里使用。
2. 跟踪 `mi_heap_new` → `mi_heap_malloc`（任意线程）→ `mi_heap_delete` / `mi_heap_destroy` 的完整生命周期源码路径。
3. 解释实现跨线程分配的关键机制：**每个 heap 自带一个动态 TLS 槽位**，每个线程首次使用该 heap 时惰性创建一个属于自己的 theap。
4. 区分 `mi_heap_delete`（存活块迁移到主堆）与 `mi_heap_destroy`（整堆一次性释放）的语义差别与各自适用场景。
5. 知道 v3 里 `mi_heap_set_default` 已经不存在了，切换「默认分配目的地」的正确姿势是 `mi_theap_set_default(mi_heap_theap(heap))`。
6. 理解 theap 引用计数存在的原因（`_mi_theap_cached` 这个 TLS 缓存可能还指着它）。

## 2. 前置知识

本讲建立在 u3-l1（堆层级模型）与 u7-l2（TLS 与默认 theap）之上，先把几个关键概念温习一遍：

- **heap（一等堆）与 theap（线程本地堆）的分工**：heap 是「跨线程的身份与账本」，theap 是「真正拥有页、服务分配的线程私有执行单元」。一个 heap 下面可以挂任意多个 theap（每线程至多一个），一个线程的 tld 下面也可以挂多个属于不同 heap 的 theap。
- **TLS（Thread-Local Storage，线程本地存储）**：每个线程各自独立的一份变量。`_mi_theap_default` 就是一个 TLS 变量，保存当前线程的默认 theap。
- **动态 TLS 槽位（`mi_thread_local_t`）**：u7-l2 讲过，mimalloc 在 threadlocal.c 里自制了一套可增长的「每线程槽数组」，`_mi_thread_local_create()` 申请一个 key，之后任何线程都能用这个 key 在**自己**的数组里存取一个指针。本讲的 hero 机制就建立在这里。
- **两条链表**：每个 theap 同时挂在两个链表上——用 `tnext/tprev` 挂在创建线程 tld 的 theaps 链上，用 `hnext/hprev` 挂在所属 heap 的 theaps 链上（types.h 中两组相邻字段）。堆销毁与线程退出都要把这两条链拆干净。
- **引用计数（refcount）**：一个「什么时候才能真的释放内存」的经典问题——当还有别的指针（这里是 TLS 缓存）指着这块内存时，最后一个使用者离开前不能释放。

如果你对「页（page）、bin、free list」还不熟悉，请先回看单元三；本讲只在与页回收交接的地方触及它们。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
|---|---|
| [src/heap.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/heap.c) | heap 的生命周期全集：创建（`mi_heap_new`）、按 heap 取 theap（`_mi_heap_theap_get_or_init`）、销毁（`mi_heap_delete`/`mi_heap_destroy`） |
| [src/theap.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c) | theap 的创建（`_mi_theap_create`/`_mi_theap_alloc`/`_mi_theap_init`）、引用计数、两条链表的拆除 |
| [include/mimalloc/prim-tls.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h) | `_mi_heap_theap()` 内联快路径：两级缓存判定 |
| [src/arena.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c) | `mi_heap_delete_page` 访问器：delete 时页如何搬家、destroy 时页如何当场归还 |
| [include/mimalloc.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h) | 公共 API 合同：heap 族与 theap 族函数声明 |
| [include/mimalloc/types.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h) | `mi_heap_s` 与 `mi_theap_s` 结构定义 |
| [test/test-stress-heaps.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-stress-heaps.c) | 官方压测入口：一个 15 行的宏开关包装器 |
| [test/test-stress.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-stress.c) | 「滚动堆（rolling heaps）」用法示范：多线程在同一个 heap 上分配 |

## 4. 核心概念与源码讲解

### 4.1 一等堆的创建：mi_heap_new 与 heap 自带的 TLS 槽位

#### 4.1.1 概念说明

「一等堆」是 mimalloc 的术语，指 `mi_heap_t` 是一个用户可以显式创建、传递、销毁的对象，地位像文件描述符一样「一等」。公共头文件里一句话给出合同：

> Heaps: first-class. Can allocate from any thread (and be free'd from any thread)
> Heaps keep allocations in separate pages from each other (but share the arena's and free'd pages)

——[include/mimalloc.h:L229-L232](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L229-L232)：堆是一等的，可从任意线程分配、在任意线程释放；不同堆的分配放在彼此隔离的页里，但共享 arena 与已释放的页。

这句话的后半句（页隔离、arena 共享）解释了一等堆的典型用途：把「一个请求」「一个解析任务」「一个脚本实例」的全部对象圈进一个堆，任务结束时整堆一次销毁，不需要逐个 `free`。

要特别对照的是 v1/v2 的旧语义，文档里写得非常直白：

——[doc/mimalloc-doc.h:L505-L513](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/doc/mimalloc-doc.h#L505-L513)：`__v1__`,`__v2__` 里，一个堆**只能在创建它的线程里**用于分配！

这就是本讲要回答的核心问题：**v3 靠什么把「只能在创建线程用」变成「任意线程都能用」？** 答案预告：每个 heap 在创建时就reserve 了一个**专属动态 TLS 槽位**，任意线程首次在这个 heap 上分配时，会在自己的槽位里惰性创建一个属于自己的 theap。

#### 4.1.2 核心流程

`mi_heap_new()` 的调用链：

```text
mi_heap_new()                                  // heap.c:157
  └─ mi_heap_new_in_arena(0)                   // heap.c:150
       ├─ mi_thread_init()                     // 先保证本线程已初始化（见下方注释）
       └─ _mi_heap_new_for_subproc(subproc, 0, false)   // heap.c:128
            ├─ heap = mi_heap_zalloc(heap_main, sizeof(mi_heap_t))
            │      // 注意：heap 结构体本身是从「主堆」分配的
            ├─ theap_slot = _mi_thread_local_create()   // 申请 heap 专属 TLS 槽位
            └─ _mi_heap_init(heap, theap_slot, subproc, 0)
                 ├─ heap->theap = theap_slot   // 记住自己的槽位 key
                 ├─ 初始化三把锁 + 统计头
                 └─ 挂入 subproc->heaps 双向链表，heap_count 加一
```

两个值得咀嚼的细节：

1. **为什么 `mi_heap_new_in_arena` 要先调 `mi_thread_init()`？** 源码注释解释：`mi_heap_new` 可能是进程里第一个 mimalloc 调用，此时主堆还不存在，`_mi_heap_new_for_subproc` 里那句 `mi_heap_zalloc(heap_main, ...)` 会从一个 NULL 的 `subproc->heap_main` 上分配而崩溃。先初始化线程就保证了主堆就位。
2. **创建 heap 本身要分配内存，而分配需要一个堆**——这是自举问题，解法就是「向主堆借」。

#### 4.1.3 源码精读

先看 `mi_heap_s` 结构里本讲最关键的一个字段：

——[include/mimalloc/types.h:L618-L639](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L618-L639)：`mi_heap_s` 的定义。`mi_thread_local_t theap`（第 623 行）就是这个堆的专属动态 TLS 槽位 key；`mi_theap_t* theaps`（第 628 行）是挂在本堆下的所有 theap 链；`stats`（第 638 行）是 periodically 从各 theap 合并上来的堆级统计。

创建路径：

——[src/heap.c:L128-L148](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/heap.c#L128-L148)：`_mi_heap_new_for_subproc`。第 133 行把 heap 结构体从 `heap_main`（当前 subproc 的主堆，或父 subproc 的主堆）用 `mi_heap_zalloc` 分配出来；第 136 行为本堆申请一个新的线程本地槽位（主堆则直接用快速槽 `mi_thread_local_key_fast`，参见 u7-l2），注释还引用了 issue #1230——这个槽位是独立分配的，失败时要回滚（`mi_free(heap)`）。

——[src/heap.c:L102-L126](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/heap.c#L102-L126)：`_mi_heap_init`。逐字段初始化后，第 116-122 行在持 `subproc->heaps_lock` 的情况下把新堆头插进 subproc 的堆双向链表——这就是后面 `mi_subproc_visit_heaps` 能数出堆数的原因；第 123-124 行维护 `heap_count` 与 `heaps` 统计。

——[src/heap.c:L150-L159](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/heap.c#L150-L159)：`mi_heap_new_in_arena` 与 `mi_heap_new`。前者带独占 arena 参数（u6-l3 讲过），后者就是它的 `arena_id=0` 便捷版；第 151-154 行的注释解释了为什么必须先 `mi_thread_init()`。

#### 4.1.4 代码实践：数一数进程里有几个堆

heap 的创建/销毁会实时反映在 subproc 的堆链表上，而 `mi_subproc_visit_heaps` 公开了这张链表，这给了我们一个零成本观察生命周期的窗口。

1. **实践目标**：验证 `mi_heap_new` 确实把堆登记进了 subproc，`mi_heap_destroy` 确实把它摘除。
2. **操作步骤**：把下面的示例代码存为 `count-heaps.c`（示例代码），用 `gcc count-heaps.c -o count-heaps -Iinclude -Lout/release -lmimalloc` 编译链接（路径按你的构建产物调整）：

```c
// 示例代码：统计当前 subproc 中的堆数
#include <mimalloc.h>
#include <stdio.h>

static bool count_heap(mi_heap_t* heap, void* arg) {
  (void)heap;
  (*(size_t*)arg)++;
  return true;   // 继续遍历
}

static size_t heap_count(void) {
  size_t n = 0;
  mi_subproc_visit_heaps(mi_subproc_current(), count_heap, &n);
  return n;
}

int main(void) {
  printf("baseline heaps: %zu\n", heap_count());      // 预期 1（主堆）
  mi_heap_t* h1 = mi_heap_new();
  mi_heap_t* h2 = mi_heap_new();
  mi_heap_t* h3 = mi_heap_new();
  printf("after 3 new : %zu\n", heap_count());        // 预期 4
  mi_heap_destroy(h1); mi_heap_destroy(h2); mi_heap_destroy(h3);
  printf("after destroy: %zu\n", heap_count());       // 预期回到 1
  return 0;
}
```

3. **需要观察的现象**：三行输出分别是 1、4、1。
4. **预期结果**：主堆始终在链表里（它是第 116-122 行那个头插操作的第一个成员），三个新堆加入后计数加三，销毁后被摘除。
5. 若你观察到的数字不同（例如 2 而不是 1），检查是否用了 `mi_heap_new_in_arena` 之外还创建了别的堆，或 `MIMALLOC_VERBOSE=1` 看是否有额外初始化动作——具体计数「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `mi_heap_new` 分配 `mi_heap_t` 结构体时要向主堆借内存，而不能从「正在创建的这个新堆」里分配？

**答案**：新堆此刻还不存在——没有任何页、任何 theap、任何可分配的块；而且堆结构体必须先初始化（包括它自己的 `theap` TLS 槽位字段）才能承载分配。这是一个典型的自举问题，mimalloc 的通用解法是「静态/主对象先行」（u7-l1 讲过主堆、theap_meta 的静态自举，这里是同一思路的堆级版本）。

**练习 2**：`_mi_heap_init` 里把堆挂进 `subproc->heaps` 链表时为什么必须持锁？谁会和它竞争？

**答案**：任意线程都可能同时创建/销毁堆（`mi_heap_new` 是线程安全的），销毁路径 `mi_heap_free` 也要在 `heaps_lock` 下做双向链表摘除（heap.c 第 214-218 行）；不持锁会让链表的 `next/prev` 指针撕裂。

**练习 3**：主堆的 `heap->theap` 槽位和其他堆有什么不同？

**答案**：主堆用的是 `mi_thread_local_key_fast`（一个保留的快速槽，u7-l2 讲过），其他堆走 `_mi_thread_local_create()` 动态申请——见 heap.c 第 136 行的三目表达式，以及 `_mi_heap_init` 末尾第 125 行的断言：主堆的槽位必须等于快速 key。

### 4.2 跨线程分配的实现：heap 专属槽位 + 惰性 theap 创建

#### 4.2.1 概念说明

现在回答本讲的主问题。用户侧 API 长这样：任何线程都能调

```c
void* p = mi_heap_malloc(heap, size);   // heap 可以是别的线程创建的
```

它当然不能直接去碰某个线程私有的 theap——那会破坏「非原子字段仅属主线程可写」的所有权契约（u5-l1）。v3 的解法是**把 heap × thread 的二维矩阵摊开**：

\[ \text{theap 数量} \approx \#\text{heap} \times \#\text{thread（实际使用过的组合）} \]

每个组合一个独立的 theap，各自拥有自己的页和 free list，互不竞争。而「找到本线程在此 heap 上的那个 theap」由 heap 创建时预留的专属 TLS 槽位完成——一次 TLS 读取，O(1)。

对比 v2：v2 里 heap 结构直接内嵌线程相关字段，只有创建线程能用；v3 把线程相关状态全部下沉到 theap，heap 只保留跨线程的账本（theaps 链、arena 页登记、统计），这是「任意线程可用」的架构根源。

#### 4.2.2 核心流程

`mi_heap_malloc(heap, size)` 的取 theap 过程：

```text
mi_heap_malloc(heap, size)                        // alloc.c:260
  └─ _mi_heap_theap(heap)                         // prim-tls.h:389（内联）
       ├─ theap = _mi_theap_cached()              // 第 1 级：读 TLS 缓存
       ├─ if (theap->heap == heap) return theap;  // 命中：2 次读，完事
       └─ _mi_heap_theap_get_or_init(heap)        // heap.c:90
            ├─ theap = _mi_thread_local_get(heap->theap)   // 第 2 级：本线程在此堆的槽位
            ├─ if (theap == NULL)                         // 本线程第一次用这个堆
            │    └─ mi_heap_init_theap(heap)              // heap.c:60
            │         ├─ mi_thread_init()（若本线程还没初始化）
            │         ├─ _mi_theap_create(heap, mi_theap_get_default()->tld)
            │         │     ├─ _mi_theap_alloc(heap, tld)   // theap.c:308
            │         │     └─ _mi_theap_init(theap, heap, tld)  // theap.c:236
            │         └─ _mi_heap_theap_set(heap, theap)    // 写回槽位
            └─ _mi_theap_cached_set(theap)         // 刷新第 1 级缓存（伴随 incref/decref）
```

`_mi_theap_init` 内部（theap.c:236-306）按顺序做六件事：

1. 用只读模板 `_mi_theap_empty` 整体覆盖（`_mi_memcpy_aligned`），保留自己的 `memid`；
2. `refcount` 置 1（release 语义），记录 `subproc`；
3. 头插进**本线程 tld** 的 theaps 链（`tnext/tprev`）——线程退出时要沿这条链 abandon 所有 theap；
4. 从链首的随机数裂变出本 theap 的 `random` 与 `cookie`（安全模式用，见 u9-l1）；
5. **最后**才以 release store 写入 `theap->heap`——注释明说：heap 成员是否为空就是「theap 是否已初始化」的判定标志，必须最后写；
6. 头插进**所属 heap** 的 theaps 链（`hnext/hprev`）——堆销毁时要沿这条链逐个释放。

#### 4.2.3 源码精读

——[src/alloc.c:L260-L262](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L260-L262)：`mi_heap_malloc` 只有一行——把 heap 翻译成 theap 后走与 `mi_malloc` 完全相同的内部分配函数。`mi_heap_zalloc`（[L287-L289](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L287-L289)）、`mi_heap_realloc`（[L512-L514](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L512-L514)）同样形态。**heap 族 API 的全部秘密都在「heap → theap」这一步翻译上**。

——[include/mimalloc/prim-tls.h:L387-L397](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h#L387-L397)：`_mi_heap_theap`。先读 `_mi_theap_cached()`，若其 `heap` 成员匹配就直接返回——这正是 u7-l2 讲过的两级缓存中的第二级；未命中才落入 `_mi_heap_theap_get_or_init`。频繁在同一堆上分配的线程只付「1 次 TLS 读 + 1 次字段读」的代价。

——[src/heap.c:L90-L100](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/heap.c#L90-L100)：`_mi_heap_theap_get_or_init`。第 93 行读本线程在 `heap->theap` 槽位里的值；为空则进入 `mi_heap_init_theap`；注意第 96 行的失败分支返回静态哨兵 `_mi_theap_empty_wrong`，注释说明它会让 `page.c:_mi_malloc_generic` 自然返回 NULL——又一个「用只读空对象把错误并进正常路径」的惯用法（与 u4-l1 的 `mi_page_empty` 同族）。

——[src/heap.c:L60-L86](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/heap.c#L60-L86)：`mi_heap_init_theap`。第 67-69 行先保证本线程初始化；第 77 行是点睛之笔——新 theap 挂在 `mi_theap_get_default()->tld` 之下，即**当前执行分配的这个线程**的 tld，而不是堆创建者的。这就是「theap 归属使用线程」的实现点。

——[src/theap.c:L336-L341](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L336-L341)：`_mi_theap_create` = `_mi_theap_alloc` + `_mi_theap_init`。而 [L308-L334](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L308-L334) 的 `_mi_theap_alloc` 决定内存在哪买：普通堆走 `_mi_meta_zalloc`（元数据堆，u7-l1），带独占 arena 的堆则直接从自己的 arena 里切（第 324 行还有句自嘲注释：至少占一个 64KiB slice，「相当浪费」）。

——[src/theap.c:L296-L305](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L296-L305)：`_mi_theap_init` 的收尾两步——release 语义写入 `theap->heap`（初始化完成的标志），然后持 `heap->theaps_lock` 头插进堆的 theaps 链。

**引用计数**。theap 结构里有 `_Atomic(size_t) refcount`（[include/mimalloc/types.h:L568](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L568)），为什么要它？theap.c 第 357 行的注释一语道破：

——[src/theap.c:L357-L370](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L357-L370)：`_mi_theap_incref`/`_mi_theap_decref`。「因为 `_mi_theap_cached` 线程本地变量可能还指向这个 theap」，所以堆销毁不能直接释放内存，要等最后一个引用者（缓存槽）放手。`decref` 用 `mi_atomic_decrement_acq_rel(...) == 1` 判定「我是最后一个」才调 `mi_theap_free_mem`；两者都先检查 `mi_memid_needs_no_free(theap->memid)`——静态分配的 theap（如 `_mi_theap_empty`）直接跳过。缓存的 incref/decref 配对发生在 [src/prim/prim-tls.c:L211-L229](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/prim-tls.c#L211-L229) 的 `_mi_theap_cached_set` 尾部（第 227-228 行）；pthreads 模型下缓存 key 的析构回调也会 decref（[L157-L162](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/prim-tls.c#L157-L162)）。

**链表拆除与锁反转**。堆销毁要同时操作「堆的 theaps 链」和「各线程 tld 的 theaps 链」，两个方向的锁顺序相反，theap.c 第 372-378 行的注释给出解法：

——[src/theap.c:L381-L412](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L381-L412)：`_mi_heap_detach_theaps`。持 `heap->theaps_lock` 遍历，对每个 theap 用 `mi_lock_try_acquire` 试拿其 tld 的锁；拿不到就记一笔 `heaps_delete_wait` 统计、`_mi_prim_thread_yield()` 让步后重试，直到全部摘除。镜像的 `_mi_tld_detach_theaps`（[L415-L450](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L415-L450)）服务线程退出方向，摘除后把 `theap->heap` 置 NULL（第 435 行，release store）。

#### 4.2.4 代码实践：两个线程在同一个堆上分配

1. **实践目标**：亲眼验证「heap 可以被非创建线程使用」，且两个线程的块确实都登记在同一个堆名下。
2. **操作步骤**：编译运行下面的示例代码（示例代码，pthread 版）：

```c
// 示例代码：跨线程使用同一个 heap
#include <mimalloc.h>
#include <pthread.h>
#include <stdio.h>

static mi_heap_t* g_heap;

static void* worker(void* arg) {
  (void)arg;
  // 注意：这里不是创建 g_heap 的线程！
  void* p = mi_heap_malloc(g_heap, 128);
  printf("worker  : p=%p in_heap=%d\n", p, (int)mi_heap_contains(g_heap, p));
  return p;                       // 把指针带回去，主线程稍后统一 mi_free
}

int main(void) {
  g_heap = mi_heap_new();                       // 主线程创建
  pthread_t t;
  void* ret = NULL;
  pthread_create(&t, NULL, &worker, NULL);
  pthread_join(t, &ret);
  void* q = mi_heap_malloc(g_heap, 128);        // 主线程也在同一堆上分配
  printf("main   : q=%p in_heap=%d\n", q, (int)mi_heap_contains(g_heap, q));
  printf("same heap? %d\n", (int)(mi_heap_of(ret) == mi_heap_of(q)));
  mi_free(ret); mi_free(q);
  mi_heap_delete(g_heap);
  return 0;
}
```

3. **需要观察的现象**：两行 `in_heap=1`，且 `same heap? 1`。
4. **预期结果**：`mi_heap_contains`/`mi_heap_of` 走的是 page map 反查（u3-l4）——`page->heap` 指向同一个堆，说明两个线程各自创建的 theap 都登记在这一个 heap 之下。单线程分配一次的输出「待本地验证」，但机制上 `worker` 内部必然触发了 `mi_heap_init_theap`（本线程在该堆的槽位还是空的）。
5. 若想看得更狠一点：在 debug 构建下跑 `MIMALLOC_SHOW_STATS=1`，在 heap 统计里找 `theaps` 相关计数，确认这个堆下挂了 2 个 theap（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：线程 A 创建了堆 H 但从未分配；线程 B 第一个在 H 上 `mi_heap_malloc`。此时 H 名下的第一个 theap 属于谁、挂在谁的 tld 上？

**答案**：属于线程 B。创建堆本身不产生任何 theap；theap 在「某线程首次在该堆上分配」时才创建，并且挂在**当时执行分配的线程**的 tld 上（heap.c 第 77 行 `mi_theap_get_default()->tld` 取的是当前线程的默认 theap 的 tld）。堆创建者的身份完全不重要——这正是「一等」的含义。

**练习 2**：`_mi_theap_init` 为什么必须**最后**才写 `theap->heap`？如果先写会发生什么？

**答案**：`theap->heap` 是否非空是全库判定「theap 已初始化」的标志（`mi_theap_is_initialized`）。若先写它、再初始化其余字段，另一个并发路径（如缓存命中判定 `_mi_theap_heap_peek(theap)==heap`）会看到一个「自称已初始化但内部还是垃圾」的 theap。用 release store 收尾保证了读者在 acquire 读到 heap 指针时，其余字段必然已就绪。

**练习 3**：既然每线程每堆一个 theap，会不会线程数 × 堆数暴涨导致内存爆炸？mimalloc 用什么缓解？

**答案**：会有这个乘积风险，但被三点缓解：(a) theap 是惰性创建的，只有真正用过的 (heap, thread) 组合才存在；(b) theap 结构本身通过 `_mi_meta_zalloc` 放在元数据区，且不用时会被销毁归还；(c) 线程退出时 `_mi_tld_detach_theaps` 把自己的 theap 从堆链上摘除、堆销毁时 `_mi_heap_detach_theaps` 反向摘除，两个方向都会清理。

### 4.3 堆的销毁：delete vs destroy 与滚动堆

#### 4.3.1 概念说明

销毁有两个语义不同的入口，公共头文件的行内注释就是全部合同：

- `mi_heap_delete(heap)`——「move live blocks to the main heap」：释放堆的内部资源，**存活块不释放**，它们所在的页被迁移到主堆，之后照常 `mi_free`。
- `mi_heap_destroy(heap)`——「free all live blocks」：**整堆一次性释放**，所有仍存活的对象随着页一起归还，无需（也不能）再逐个 `mi_free`。这是「区域化分配（region-based allocation）」的杀手锏：解析完一棵 AST、跑完一个请求，一次调用全清。

两者都不能作用于主堆（会打警告并忽略——见 heap.c 第 232-235、256-259 行），因为其他堆 delete 时的存活块要迁往主堆，主堆必须永生。

destroy 的一次性释放为什么便宜？回忆 u6-l4：堆名下的页在 arena 里有 slice 级登记（`heap->arena_pages` 位图）。destroy 不需要逐块走 free list，只要按页批量归还 slice——把「N 次 free」变成「M 个页归还」（\( M \ll N \)）。

**API 迁移警示**：讲义规格里提到的 `mi_heap_set_default` 在 v3 里**已不存在**。文档明确标注它是 `__v1__`,`__v2__` API，并指路 v3 替代品：

——[doc/mimalloc-doc.h:L534-L543](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/doc/mimalloc-doc.h#L534-L543)：`mi_heap_set_default`/`mi_heap_get_default` 均标注为 v1/v2，v3 请用 `mi_theap_set_default()`/`mi_theap_get_default()`。

原因很本质：v3 里「默认」的粒度是 theap 而不是 heap——你要先回答「这个堆在**当前线程**上的那个 theap 是谁」（`mi_heap_theap(heap)`），才能把它设为默认。而且注意 theap 的约束（[include/mimalloc.h:L366-L374](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L366-L374) 的注释）：**theap 只能在创建它的线程里分配**——所以「设默认」必须在使用线程里做，主线程 set 了不会传染给工作线程。

#### 4.3.2 核心流程

```text
mi_heap_delete(heap)                    // heap.c:229
  ├─ mi_heap_free_theaps(heap)          // 摘链 + 合并统计 + decref 所有 theap
  ├─ _mi_heap_move_pages(heap, heap_main)   // arena.c:2634：页迁移到主堆
  └─ mi_heap_free(heap, true)           // 释放 arena_pages 数组、摘堆链、释放 heap 内存

mi_heap_destroy(heap)                   // heap.c:254
  └─ _mi_heap_force_destroy(heap, true) // heap.c:241
       ├─ mi_heap_free_theaps(heap)
       ├─ _mi_heap_destroy_pages(heap)  // arena.c:2640：页直接归还
       └─ mi_heap_free(heap, true)
```

两条路共用 `mi_heap_delete_pages(heap, heap_target)`，差别只在 `heap_target` 是主堆还是 NULL——单参分叉决定「搬家还是处决」。

`mi_heap_free_theaps`（heap.c:162-185）先调 `_mi_heap_detach_theaps` 把所有 theap 从各线程 tld 链上摘下（保证没有线程还能通过 tld 找到它们），再持锁遍历堆的 theaps 链，把每个 theap 的统计合并进堆统计，最后 `_mi_theap_decref`——注意第 181 行注释：**缓存槽可能还指着它，所以必须走引用计数而不是直接释放**。

#### 4.3.3 源码精读

——[src/heap.c:L229-L239](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/heap.c#L229-L239)：`mi_heap_delete`。先取 `heap_main`，若删除对象就是主堆则警告返回；否则三步走：free_theaps → 迁页到主堆 → free 堆本身。

——[src/heap.c:L241-L261](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/heap.c#L241-L261)：`_mi_heap_force_destroy` 与 `mi_heap_destroy`。与 delete 唯一的实质差别是 `_mi_heap_destroy_pages` 替代了 `_mi_heap_move_pages`。

——[src/arena.c:L2634-L2643](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L2634-L2643)：`_mi_heap_move_pages` 与 `_mi_heap_destroy_pages` 是同一函数 `mi_heap_delete_pages` 的两个壳，前者指定目标为主堆，后者传 NULL。

——[src/arena.c:L2529-L2558](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L2529-L2558)：`mi_heap_delete_page` 的分叉核心。先 `mi_page_claim_ownership` 认领页、若已 abandoned 则反遗弃；然后三岔路口：`used==0`（本来就没活块）→ 直接还页；`heap_target==NULL`（destroy）→ 第 2556 行一句 `page->used=0` 后整页归还——**所有存活块不经过任何 free list，连锅端走**；否则（delete）走搬家分支。

——[src/arena.c:L2559-L2606](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L2559-L2606)：搬家分支。从原堆的 `arena_pages` 位图里清掉该 slice、在目标堆的位图里置上（第 2588-2597 行，中途分配失败还有降级到 arena 主堆的兜底），改写 `page->heap = heap_target`（第 2599 行），最后 `_mi_arenas_page_abandon` 把页按 abandoned 状态挂到新堆——之后新堆的线程会在分配慢路径里按 size class 认领它们（u6-l4 的 reclaim 机制）。

——[src/heap.c:L188-L227](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/heap.c#L188-L227)：`mi_heap_free` 收尾。释放堆在各 arena 的 `arena_pages` 登记数组（主堆除外——它的页信息预铺在 arena 里）；把堆统计合并进主堆/subproc 统计；从 subproc 堆链摘除；销毁三把锁；最后 `_mi_thread_local_free(heap->theap)` 归还那个专属 TLS 槽位并 `_mi_free_subproc_safe(heap)` 释放堆结构体自身。

**官方示范：滚动堆**。

——[test/test-stress-heaps.c:L1-L15](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-stress-heaps.c#L1-L15)：整个文件只有两个实质宏：`TEST_STRESS 1` 与 `MI_USE_HEAPS 4`，然后 include test-stress.c——用一个编译期开关把通用压测改造成堆压测。

——[test/test-stress.c:L94-L106](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-stress.c#L94-L106)：`MI_USE_HEAPS` 生效时，全局变量 `current_heap` 成为分配目的地，`custom_calloc` 被宏替换为 `mi_heap_calloc(current_heap,...)`——注意 32 个工作线程用的都是**主线程创建**的这个堆，正是跨线程分配的官方用法。

——[test/test-stress.c:L281-L291](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-stress.c#L281-L291)：滚动窗口。每轮迭代：删掉 4 轮之前的旧堆、把窗口内堆整体后移一位、`current_heap = mi_heap_new()` 开新堆。之所以「延迟 4 轮才删」，是因为 transfer 缓冲区里的指针会跨轮存活——立即 delete 会把大量仍被引用的页迁去主堆；留 4 轮缓冲，等指针大概率自然消亡后再删，迁移量最小。收尾清理在 [L332-L338](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-stress.c#L332-L338)：窗口里所有堆逐个 `mi_heap_delete`。

——[src/theap.c:L172-L188](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L172-L188)：`mi_theap_get_default`/`mi_theap_set_default`。set 返回旧默认 theap，且只对「已初始化」的 theap 生效（第 184 行判定）。这是 v3 里接替 `mi_heap_set_default` 的 API。

#### 4.3.4 代码实践：阅读并解释滚动堆

1. **实践目标**：不写代码，纯源码阅读——把 test-stress-heaps 的堆调度策略讲给自己听。
2. **操作步骤**：
   - 读 test-stress-heaps.c 全文（15 行），确认它只是个宏包装；
   - 读 test-stress.c 第 94-106 行（宏如何改写 `custom_calloc`）与第 275-291 行（滚动窗口）；
   - 构建并运行：`./mimalloc-test-stress-heaps 4 10 8`（参数依次是线程数、负载、迭代轮数，见 test-stress.c 第 413-428 行的参数解析），观察开场打印的 `using 4 rolling heaps`。
3. **需要观察的现象**：程序在低负载下快速跑完；统计输出里 `heaps` 计数不为零。
4. **预期结果**：能把「新堆每轮创建、旧堆延迟 4 轮 delete」复述出来，并说出延迟删除的动机（transfer 指针跨轮存活，见上文）。实际输出数值「待本地验证」。
5. 思考题自测：如果把第 284 行的 `mi_heap_delete` 换成 `mi_heap_destroy` 会怎样？（答案见下面练习 3。）

#### 4.3.5 小练习与答案

**练习 1**：你的程序把一批中间结果挂在堆 H 上，处理结束后逐个 `mi_free` 了，最后应该调 `mi_heap_delete` 还是 `mi_heap_destroy`？

**答案**：两者皆可，但 `mi_heap_delete` 更稳。既然块已全部手动释放，两者的实际工作量几乎相同（都只剩资源回收）。习惯上：只要还**可能**有漏网指针（异步回调、全局缓存），就用 delete（存活块迁主堆，安全）；确信**再无任何外部引用**时才用 destroy（快刀斩乱麻）。destroy 后再 `mi_free` 旧指针是释放后使用。

**练习 2**：`mi_heap_destroy` 之后，原来那些块的指针还会经过 page map 查到 heap 指针吗？

**答案**：页已归还（arena slice 位图已清、页元数据不复存在），page map 里对应条目也会被清掉，`mi_is_in_heap_region`/`mi_heap_of` 对这些地址将返回否定结果。但**绝不能**拿旧指针去 free 或解引用——那是未定义行为，能否「优雅地」报告取决于构建模式（u9-l2 的 invalid pointer 检测）。

**练习 3**：把 test-stress.c 第 284 行的 `mi_heap_delete(prev_heaps[...])` 换成 `mi_heap_destroy`，压测会发生什么？

**答案**：destroy 会当场释放该堆里**仍存活**的块——而滚动窗口存在的意义正是 transfer 缓冲区里还有跨轮存活的指针（第 305-310 行每轮只释放一半 transfer）。这些指针会在后续 `free_items` 里读到已回收内存，触发第 178-181 行的内存腐蚀检测并 `abort`。这个思想实验正好证明 delete 与 destroy 的语义差异是实打实的。

## 5. 综合实践

把本讲三个模块串起来：**主线程建 3 个堆，3 个工作线程各自「认领」一个堆并设为默认后分配，最后 `mi_heap_destroy` 整体释放，用统计验证**。

完整示例程序（示例代码，存为 `heap-workers.c`）：

```c
// 示例代码：3 堆 × 3 线程，destroy 一次性释放
#include <mimalloc.h>
#include <mimalloc-stats.h>
#include <pthread.h>
#include <stdio.h>

#define NHEAPS   3
#define NALLOCS  100000

typedef struct worker_arg_s {
  mi_heap_t* heap;
  void**     blocks;
} worker_arg_t;

static size_t count_heap(mi_heap_t* heap, void* arg) {
  (void)heap; (*(size_t*)arg)++; return true;
}
static size_t heap_count(void) {
  size_t n = 0;
  mi_subproc_visit_heaps(mi_subproc_current(), count_heap, &n);
  return n;
}

static void* worker(void* varg) {
  worker_arg_t* arg = (worker_arg_t*)varg;
  // v3 正确姿势：把「本线程在此堆上的 theap」设为默认，之后裸 mi_malloc 也落在这个堆
  // 注意：set 必须在使用线程里做（theap 只能在创建它的线程里分配）
  mi_theap_set_default(mi_heap_theap(arg->heap));
  arg->blocks = (void**)mi_malloc(NALLOCS * sizeof(void*));
  for (size_t i = 0; i < NALLOCS; i++) {
    arg->blocks[i] = mi_malloc(32 + 16 * (i % 8));   // 32..144 字节，落在不同 bin
  }
  return NULL;
}

int main(void) {
  printf("heaps before: %zu\n", heap_count());       // 预期 1

  mi_heap_t* heaps[NHEAPS];
  worker_arg_t args[NHEAPS];
  pthread_t threads[NHEAPS];
  for (int i = 0; i < NHEAPS; i++) {
    heaps[i] = mi_heap_new();                        // 主线程创建堆
    args[i].heap = heaps[i]; args[i].blocks = NULL;
    pthread_create(&threads[i], NULL, &worker, &args[i]);
  }
  for (int i = 0; i < NHEAPS; i++) pthread_join(threads[i], NULL);

  printf("heaps after workers: %zu\n", heap_count()); // 预期 4
  for (int i = 0; i < NHEAPS; i++) {
    // 1) 抽查归属：工作线程分配的块确实登记在对应堆下
    printf("heap %d contains blocks[0]? %d\n", i,
           (int)mi_heap_contains(heaps[i], args[i].blocks[0]));
    // 2) 销毁前取堆统计：current > 0 说明堆里确实背着内存
    mi_stats_t st; mi_stats_init(&st);
    mi_heap_stats_get(heaps[i], &st);
    printf("heap %d malloc_normal.current = %lld bytes\n",
           i, (long long)st.malloc_normal.current);
  }

  // 3) 整体释放：三次调用替代 3*NALLOCS 次 mi_free
  for (int i = 0; i < NHEAPS; i++) mi_heap_destroy(heaps[i]);

  printf("heaps after destroy: %zu\n", heap_count()); // 预期回到 1
  // 4) 观察 arena 侧内存是否回落（destroy 的页归还 + 强制 collect 后 purge）
  mi_collect(true);
  mi_debug_show_arenas();
  return 0;
}
```

**操作步骤**：

1. release 构建库（u1-l2 的 `out/release`），编译：`gcc heap-workers.c -o heap-workers -Iinclude -Lout/release -lmimalloc -lpthread`，运行 `LD_LIBRARY_PATH=out/release ./heap-workers`。
2. 也可换成 debug 构建并用 `MIMALLOC_SHOW_STATS=1` 运行，对照完整统计报表。

**需要观察的现象与预期结果**：

- `heaps before: 1` → `heaps after workers: 4` → `heaps after destroy: 1`：堆生命周期完整闭环（对应 4.1 的链表登记机制）。
- 每个 `contains blocks[0]? 1`：跨线程分配的块归属正确（对应 4.2）。
- 每个 `malloc_normal.current` 为显著大于零的字节数（约每线程几 MB 量级）——这是「堆里背着内存」的直接证据；具体数值取决于 size class 台阶，「待本地验证」。
- destroy 之后 `mi_debug_show_arenas` 打印的 arena 使用量相对销毁前应可见回落（页已归还，配合 `mi_collect(true)` 触发 purge）；**注意**：destroy 之后 heap 指针已释放，绝不能再对它调 `mi_heap_stats_get`——所以「归零」的验证要在销毁**前**取数、销毁**后**看进程/arena 侧的回落，而不是去查一个已死的堆。arena 打印的具体变化「待本地验证」。

**思考延伸**：把 `mi_heap_destroy` 换成 `mi_heap_delete` 再跑一次——`heap_count` 同样回到 1，但 `mi_debug_show_arenas` 的回落会变少（存活块迁去了主堆），且之后你仍可以（也应该）逐个 `mi_free(args[i].blocks[j])`。

## 6. 本讲小结

- **一等堆的含义**：v3 中 `mi_heap_t` 可从任意线程分配、任意线程释放（mimalloc.h 的 API 合同），而 v1/v2 的堆只能在创建线程使用——这是 v3 架构级的改进。
- **实现支柱**：每个 heap 创建时预留一个专属动态 TLS 槽位（`heap->theap`，heap.c:136），任意线程首次在该堆分配时惰性创建挂在自己 tld 下的 theap（`mi_heap_init_theap`，heap.c:77）；heap 族 API 的全部工作就是「heap → 本线程 theap」的翻译（alloc.c:260-262 + prim-tls.h:389-397 的两级缓存）。
- **theap 双链挂载**：`tnext/tprev` 挂创建线程的 tld 链（线程退出清理用），`hnext/hprev` 挂所属堆的 theaps 链（堆销毁清理用）；两个方向的拆除用 try-lock + 让步重试化解锁反转（theap.c:381-450）。
- **引用计数保平安**：`_mi_theap_cached` 这个 TLS 缓存可能还指着 theap，所以堆销毁走 `_mi_theap_decref`，最后一个引用者放手才真正释放（theap.c:357-370）。
- **delete vs destroy**：`mi_heap_delete` 把存活页迁移到主堆（arena.c:2559-2606 的搬家分支，页以 abandoned 状态挂入新堆等待认领）；`mi_heap_destroy` 把 `page->used` 直接清零、整页归还（arena.c:2551-2558）——一次调用替代 N 次 free。两者都拒绝作用于主堆。
- **API 迁移**：v3 没有 `mi_heap_set_default`；切换默认分配目的地要用 `mi_theap_set_default(mi_heap_theap(heap))`，且必须在使用线程里调用。

## 7. 下一步学习建议

- **下一讲 u7-l4（子进程 subproc）**：本讲反复出现的 `heap->subproc`、`subproc->heaps` 链与 `heap_main` 将在那里展开——subproc 是「多个互相隔离的一等堆宇宙」，CPython 多解释器的基础。建议先思考：为什么 `_mi_heap_new_for_subproc` 里主堆要从 `subproc->parent->heap_main` 借？
- **回补堆遍历**：u9-l4 的 `mi_heap_visit_blocks` 是本讲 `mi_heap_contains` 的放大版，可用来给综合实践写一个「销毁前逐 bin 存活块计数」的加强版验证。
- **源码延伸阅读**：带着「锁反转」问题重读 [src/theap.c:L372-L378](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L372-L378) 的注释块，再对照 u8-l1 的 `mi_lock`（自旋锁 + try_acquire）实现，体会「宁可让步重试也不嵌套等待」的无锁工程取舍。
- **动手方向**：把综合实践改造成「每请求一堆」的小型 Web 服务骨架（线程池 + 每任务 `mi_heap_new`/`mi_heap_delete`），用 `MIMALLOC_SHOW_STATS=1` 对比「整堆销毁 vs 逐块 free」的吞吐差异。
