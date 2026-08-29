# 堆的层级模型：subproc → heap → theap → page queue → block

## 1. 本讲目标

学完本讲，你应该能够：

1. 准确说出 mimalloc v3 中 `mi_subproc_t`、`mi_heap_t`、`mi_theap_t`、`mi_page_queue_t`、`mi_page_t`、`mi_block_t` 六个核心结构各自的角色，以及它们之间的**所有权（ownership）方向**。
2. 解释 v3 为什么要在 heap 之下再引入 `theap`（线程本地堆）这一层，它换来了什么、付出了什么。
3. 理解 `mi_memid_t` 如何为每一块内存记录「产地」（来自哪个 arena 的哪些 slice，还是直接来自 OS），以及为什么释放时必须知道这件事。
4. 能完整回答：**一个普通线程第一次调用 `mi_malloc` 时，从 `heap_main` 到最终拿到 block，依次经过了哪些结构、触发了哪些初始化**。

本讲是单元三的地基课：我们不深入任何一条链路的细节（那是 u4/u5 的事），只建立一张准确的「对象地图」。以后读任何 mimalloc 代码，先在这张图上定位，就不会迷路。

## 2. 前置知识

本讲假设你已读过单元一（尤其是 u1-l3 的代码地图），这里先把要用到的几个概念用通俗语言再过一遍：

- **所有权（ownership）**：指「谁负责创建、谁负责释放、谁能不加锁地访问」。mimalloc 的整个设计就是围绕「把内存的所有权尽量下沉到线程本地，让快路径无原子操作」展开的。
- **线程本地存储（TLS, Thread Local Storage）**：每个线程各有一份、互不干扰的变量。访问它不需要加锁，这是 `theap` 存在的物理基础。mimalloc 对 TLS 的多平台实现细节在 u7-l2 详讲，本讲只需把它当成「每线程一份的指针」。
- **size class / bin**：分配器不按任意大小分配，而是把请求尺寸归入有限的几档规格，每一档叫一个 size class，mimalloc 内部叫一个 **bin**。同一个 bin 的块由同一个页队列管理。
- **arena 与 slice**：mimalloc 先向 OS 保留大块内存（arena，默认起步 32 MiB），再按 64 KiB 的 **slice** 为单位切给页使用。u6 会专门讲 arena，本讲只把它当作「页内存的批发来源」。
- **原子操作与 CAS**：Compare-And-Swap，一条能「比较并交换」的 CPU 指令，是多线程无锁数据结构的基石。本讲只需要知道：跨线程共享的东西（subproc、heap）要用原子/锁，线程私有的东西（theap、page 的大部分字段）不用。
- **`_Atomic` 标记**：C11 语法，表示该字段必须通过原子操作访问。看结构体定义时，`_Atomic` 字段的多寡直接暴露了「这个结构被谁共享」。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `include/mimalloc/types.h` | **所有核心结构体的唯一定义处**：六个结构体、`mi_memid_t`、页尺寸宏 | 逐字段读注释，画出 ownership 图 |
| `src/heap.c` | 一等堆的生命周期：创建、挂链、销毁 | `heap` 如何被创建并挂入 subproc、如何找到/创建自己的 theap |
| `src/subproc.c` | 子进程（分配域）的创建与元数据分配 | 主 subproc 的静态定义、`heap_main` 的悬挂点 |
| `src/init.c` | 进程/线程初始化（辅助） | 主堆、主 theap、detached tld 的静态预分配 |
| `src/theap.c` | theap 的初始化与分配（辅助） | `_mi_theap_init` 如何把 theap 挂到 tld 和 heap 两条链上 |
| `src/alloc.c`、`src/page.c`、`include/mimalloc/internal.h` | 分配入口（辅助） | 第一次分配时从空 theap 触发初始化的完整链路 |

## 4. 核心概念与源码讲解

### 4.1 模型总览：types.h 开头的六行「蓝图」

#### 4.1.1 概念说明

mimalloc v3 的全部核心对象加起来只有六个，`types.h` 的开头注释就是官方给的心智地图：

- `mi_subproc_t`：子进程/分配域，**整个层次图的根**。通常一个进程只有一个（主 subproc）。
- `mi_heap_t`：一等堆（first-class heap），用户可以用 `mi_heap_new()` 创建多个。
- `mi_theap_t`：线程本地堆（**t**hread-local heap），v3 新增的一层，真正拥有页、直接服务分配。
- `mi_page_queue_t`：页队列，theap 内按 bin 组织页的双向链表表头。
- `mi_page_t`：mimalloc 页（通常 64 KiB / 512 KiB / 4 MiB），**一个页只放一种 size class 的块**。
- `mi_block_t`：块，用户拿到的指针就是一个 block，空闲时它的第一个字段被用作 free list 的 next 指针。

此外还有两个「横切」结构：`mi_tld_t`（线程本地数据，线程身份的载体）和 `mi_arena_t`（内存批发市场，属于 subproc）。

#### 4.1.2 核心流程

自顶向下的所有权与查找流程：

```text
进程
 └─ mi_subproc_t（根：持有 arenas[]、heap_main、heaps 链、theap_meta）
     ├─ mi_arena_t arenas[MI_MAX_ARENAS]      ← 内存批发来源（共享，原子位图）
     └─ mi_heap_t（默认只有 heap_main；可 mi_heap_new() 多个）
         └─ mi_theap_t theaps 链               ← 每线程每堆一个（hnext/hprev）
             └─ mi_page_queue_t pages[MI_BIN_COUNT]   ← 每个 bin 一条页队列
                 └─ mi_page_t（next/prev 挂在队列里）
                     └─ mi_block_t（页内存里切出的块）
                         └─ 用户指针

旁路：
 - mi_tld_t（每线程一个）──theaps 链──→ 该线程所有 theap（tnext/tprev）
 - 任意指针 p ──page map──→ mi_page_t ──page->heap──→ mi_heap_t（free 时反查）
```

三条关键规则：

1. **分配只发生在 theap 层**：`mi_malloc` 永远先取当前线程的默认 theap，再从 `theap->pages_free_direct[]` 或页队列里找页。
2. **释放可以从任何线程发生**：`mi_free` 用 page map 从指针反查出 `mi_page_t`，再读 `page->heap` 找到归属——不需要任何 TLS 查询。
3. **内存（arena）属于 subproc，不属于某个 heap**：多个 heap 共享同一批 arena，页在 arena 里按 slice 领取。

#### 4.1.3 源码精读

types.h 开头的注释直接给出了各结构的一句话定位，是全书最好背的一段：

[include/mimalloc/types.h:L11-L23](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L11-L23) —— 官方类型总览：heap 是「通常只有一个主默认堆」的数据全集；theap 是「属于特定 heap 的线程本地堆，维护有空闲空间的线程本地页列表」；page 是「分配单一尺寸对象的页（通常 64KiB 或 512KiB）」；arena 是「分配页的大型内存区域（进程共享）」；subproc 是「所有堆都隶属于一个子进程（通常只有主子进程）」。

[include/mimalloc/types.h:L260-L279](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L260-L279) —— 三个前置声明（`mi_arena_t`、`mi_heap_t`、`mi_subproc_t`），注释顺带说明了 heaps「自包含但共享 subproc 在 arena 里的内存」、subproc 用于「在一个进程里跑多个 Python 解释器」这样的设计动机。

读结构体时养成一个习惯：**先数 `_Atomic` 字段**。`mi_heap_t`、`mi_subproc_t` 里到处是 `_Atomic`，而 `mi_theap_t`、`mi_page_t` 里几乎没有——这就是共享层与线程私有层的分界线。

#### 4.1.4 代码实践

1. **实践目标**：把六个结构体「谁指向谁」的指针字段亲手抄一遍。
2. **操作步骤**：打开 `include/mimalloc/types.h`，分别定位 `mi_block_s`（L366）、`mi_page_queue_s`（L528）、`mi_page_s`（L425）、`mi_theap_s`（L561）、`mi_heap_s`（L618）、`mi_subproc_s`（L651），只看指针字段，在纸上抄下 `结构A → 字段 → 结构B` 的三元组（例如 `mi_page_t → heap → mi_heap_t`）。
3. **需要观察的现象**：你会得到一张有向图；检查它是否存在环（例如 `theap->heap` 与 `heap->theaps` 是互指的）。
4. **预期结果**：能画出与 4.1.2 一致的层次图，并发现「每一层都同时有向上和向下的指针」——向下是分配路径用的，向上是 free/统计合并路径用的。

#### 4.1.5 小练习与答案

**练习 1**：`mi_arena_t` 归哪一层所有？为什么它不在 heap 下面？

**答案**：归 `mi_subproc_t` 所有（见 `mi_subproc_s.arenas[]` 字段，types.h L658）。arena 是进程级共享资源，多个 heap 的页都从同一批 arena 里领取；若挂在某个 heap 下，跨 heap 的页回收和大对象分配就无法统一管理。

**练习 2**：`mi_tld_t` 与 `mi_theap_t` 都叫「线程本地」，它们是什么关系？

**答案**：`mi_tld_t` 是**每线程一份**的线程身份数据（线程 id、所属 subproc、`theaps` 链表头），一个线程只有一个；`mi_theap_t` 是**每线程每堆一份**（线程在几个 heap 上分配就有几个 theap），通过 `tnext/tprev` 挂在该线程的 `tld->theaps` 链上（types.h L696、L580-L583）。

### 4.2 mi_subproc_t 与 mi_tld_t：层次图的根与线程的身份

#### 4.2.1 概念说明

`mi_subproc_t`（sub-process）是 v3 为 **CPython 多解释器** 这类场景设计的「分配域」：一个进程内可以有多个互相隔离的 subproc，各自持有独立的 arena 集合与主堆，互不回收、互不遍历对方的页。绝大多数程序一生只会用到一个——**主 subproc**，它甚至是静态分配的，进程启动时就在 `.data` 段里了。

`mi_tld_t`（thread local data）则是每个线程的身份卡：记录线程 id、所属 subproc，以及**该线程创建的所有 theap 组成的链表**——线程退出时要顺着这条链把页全部 abandon（u6-l4 详讲）。

#### 4.2.2 核心流程

subproc 的组成（对照字段看）：

- `heap_main`：**该 subproc 的主堆**，`_Atomic` 是因为其他线程会来读它（首次分配时触发初始化）。
- `heaps` + `heaps_lock`：本 subproc 所有堆的双向链表。
- `arenas[MI_MAX_ARENAS]` + `arena_reserve_lock`：本 subproc 的内存仓库（上限 160 个 arena，约 2 TiB）。
- `theap_meta` + `theap_meta_lock`：一个「detached theap」，专门分配**元数据**（tld、theap、heap 结构体本身）——先有鸡还是先有蛋的问题靠它解决：分配器自己的数据结构也要内存。
- `stats`：进程级统计的汇聚点。

主 subproc 的静态预分配（`mi_process_subproc_main`）意味着：**mimalloc 的根不需要任何分配就能存在**，这是能在「第一次 malloc」这种最尴尬的时刻完成自举的前提。

#### 4.2.3 源码精读

[include/mimalloc/types.h:L651-L680](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L651-L680) —— `mi_subproc_s` 定义。注意 L663 的 `_Atomic(mi_heap_t*) heap_main`（主堆指针，原子是因为跨线程读）与 L658 的 `arenas[MI_MAX_ARENAS]`；L667 的 `theap_meta` 配 L668 的锁，就是元数据分配通道。

[src/subproc.c:L13-L15](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/subproc.c#L13-L13) —— 主 subproc 是一个静态变量 `mi_process_subproc_main`，零初始化，不占堆内存。

[src/subproc.c:L113-L115](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/subproc.c#L113-L115) —— `mi_heap_main()`：取主堆的公共入口，内部先确定当前 subproc 再读 `heap_main`。

[src/subproc.c:L103-L111](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/subproc.c#L103-L111) —— `_mi_subproc()`：当前线程所属 subproc 的判定逻辑——先取默认 theap，若 theap 或其 tld 尚不存在（初始化早期），退回主 subproc。

[src/subproc.c:L29-L37](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/subproc.c#L29-L37) —— `_mi_meta_zalloc`：在 `subproc->theap_meta` 上加锁分配元数据，并顺手填好 `mi_memid_t`（标记为 `MI_MEM_MALLOC`）。heap、theap、tld 结构体本身都是这样来的。

[include/mimalloc/types.h:L691-L701](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L691-L701) —— `mi_tld_s`：注意 L696 的 `theaps`（本线程所有 theap 的链表头）和 L695 的 `subproc`——线程与分配域的绑定就这两个字段。

#### 4.2.4 代码实践

1. **实践目标**：确认「主 subproc 与主堆是静态变量」这一事实。
2. **操作步骤**：在仓库里执行 `grep -n "mi_process_heap_main\|mi_process_subproc_main\|mi_process_theap_main" src/init.c src/subproc.c`，再阅读 [src/init.c:L151-L159](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L151-L159)。
3. **需要观察的现象**：`mi_process_heap_main`、`mi_process_theap_meta`、`mi_process_tld_main`、`mi_process_theap_main` 全部是 `static` 且 `mi_init_struct_zero` 初始化。
4. **预期结果**：得出结论——主线程的 tld、默认 theap、主 heap、theap_meta、主 subproc 五件套**零动态分配**即可就位；只有非主线程的 tld/theap 才走 `_mi_meta_zalloc`。这也解释了 `mi_memid_t` 里为什么专门有 `MI_MEM_STATIC` 一档。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `heap_main` 是 `_Atomic` 而 `heaps` 链表却配了一把普通锁？

**答案**：`heap_main` 是**热路径读取**的对象——任何线程首次分配都可能读它（`mi_heap_main()`），只需原子 load 即可无锁安全读取；而 `heaps` 链表涉及多指针修改（prev/next 双向维护），读少写多，用锁保护更简单划算。

**练习 2**：如果一个线程没有调用过 `mi_subproc_add_current_thread` 就分配内存，它的分配落在哪个 subproc？

**答案**：主 subproc。`_mi_subproc()`（subproc.c L103-L111）从默认 theap 的 tld 里取 subproc，而默认 theap 最初挂在主堆（属于主 subproc）下；types.h L648 的注释也明确「每个线程只能属于一个 subproc，且需在任何分配之前调用 `mi_subproc_add_current_thread`」。

### 4.3 mi_heap_t 与 mi_theap_t：一等堆与它的线程本地堆

#### 4.3.1 概念说明

`mi_heap_t` 是用户可见的「堆」对象：`mi_heap_new()` 创建、`mi_heap_malloc(heap, n)` 在其上分配、`mi_heap_destroy(heap)` 整体释放。它是**跨线程的、稳定的**身份——你可以把一个 heap 指针传给别的线程。

`mi_theap_t` 是 v3 的关键新增。v2 里线程本地数据（页面队列等）直接长在 `mi_heap_t` 上，这意味着「一个堆同时只能被一个线程高效使用」；v3 把线程本地状态抽出来放进 theap：

- **heap = 身份与账本**（subproc 归属、页的所有权登记 `arena_pages[]`、abandoned 页追踪、统计汇总）；
- **theap = 车间**（`pages_free_direct[]` 快表、每 bin 页队列、心跳、随机数上下文）。

由此得到 v3 的两个直接收益：

1. **任意线程可以在同一个 heap 上高效分配**——每个线程用自己的 theap，互不加锁（types.h L509-L522 的注释：「让页线程本地化可以避免原子操作」）。
2. 分配快路径上，「heap」只是一个逻辑归属，真正被触碰的结构只有 theap 和 page。

代价是复杂度：theap 需要引用计数（`refcount`）、要同时挂进 tld 链（`tnext/tprev`）与 heap 链（`hnext/hprev`），销毁时要两边的锁配合。

#### 4.3.2 核心流程

heap 与 theap 的生命周期：

```text
mi_heap_new()
  └─ _mi_heap_new_for_subproc()         // 在主堆上 zalloc 一个 mi_heap_t
       ├─ _mi_thread_local_create()     // 为这个 heap 申请一个动态 TLS 槽（存各线程的 theap）
       └─ _mi_heap_init()               // 初始化字段 + 挂入 subproc->heaps 链

线程 T 首次在 heap 上分配（或 mi_heap_theap(heap)）
  └─ _mi_heap_theap_get_or_init()
       ├─ _mi_thread_local_get(heap->theap)   // 查 T 的 TLS 槽 → NULL
       └─ mi_heap_init_theap()
            └─ _mi_theap_create(heap, tld)    // 元数据分配 + _mi_theap_init
                 ├─ 挂入 tld->theaps 链（tnext/tprev）
                 └─ 挂入 heap->theaps 链（hnext/hprev）

线程退出 / heap 销毁
  └─ 沿两条链摘除；线程退出时页被 abandon，heap 销毁时页被移走或销毁
```

`mi_heap_t.theap` 字段容易误读：它**不是 theap 指针，而是一个动态 TLS 槽的 key**——「这个 heap 在每个线程里的 theap 存放在哪个 TLS 槽位」。主堆例外，用固定的快速槽 `mi_thread_local_key_fast`（见 heap.c L136）。

#### 4.3.3 源码精读

[include/mimalloc/types.h:L509-L522](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L509-L522) —— theap 的设计说明注释：所有 theap 属于一个（非线程本地的）heap；theap 只能被创建它的线程用来分配/再分配，但**释放可以从任何线程进行**；每线程总有一个属于默认堆的默认 theap，且初始静态指向一个空 theap 以避免快路径上的初始化判断。

[include/mimalloc/types.h:L561-L598](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L561-L598) —— `mi_theap_s`。重点字段：L563 `pages_free_direct[]`（小对象 O(1) 定位页的快表，放在结构体最前面就是为了缓存友好）；L566-L568 `heap`/`subproc`/`refcount`（全是 `_Atomic`，因为堆销毁是跨线程的）；L580-L583 四个链表指针（t 开头的挂 tld 链、h 开头的挂 heap 链）；L595 `pages[MI_BIN_COUNT]`（每个 bin 一条页队列——下一模块的主角）。

[include/mimalloc/types.h:L618-L639](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L618-L639) —— `mi_heap_s`。对照着看：L623 `theap`（TLS 槽 key）；L628-L629 `theaps` 链与 `theaps_lock`；L631 `abandoned_count[]`、L635 `arena_pages[MI_MAX_ARENAS]`——heap 登记着自己名下每一页（含被遗弃的），这正是 `mi_free` 能从任意指针找回 heap 的保障；L638 `stats`（由各 theap 周期性合并上来）。

[src/heap.c:L102-L126](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/heap.c#L102-L126) —— `_mi_heap_init`：初始化字段后把 heap 挂入 `subproc->heaps` 链（L116-L122 的 `mi_lock` 块），并把 `heap_count`/统计加一。注意 L125 的断言：主堆必须使用快速 TLS 槽。

[src/heap.c:L128-L148](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/heap.c#L128-L148) —— `_mi_heap_new_for_subproc`：新堆的结构体内存来自**父 subproc 主堆**的 `mi_heap_zalloc`（L133——分配器给自己分配，靠 `theap_meta` 自举）；随后申请动态 TLS 槽（L136）。若这是主堆，则写入 `subproc->heap_main`（L142-L144）。

[src/heap.c:L60-L86](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/heap.c#L60-L86) —— `mi_heap_init_theap`：某线程首次使用某 heap 时创建 theap 的路径——查 TLS 槽为空则 `_mi_theap_create`，成功后 `_mi_heap_theap_set` 写回 TLS。

[src/theap.c:L236-L306](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L236-L306) —— `_mi_theap_init`：先把模板 `_mi_theap_empty` 整体拷入（L242），再设置 tld、refcount=1、subproc（L244-L246）；L263-L272 挂入 tld 的 theaps 链；L296 才最后写入 `theap->heap`——注释说明「heap 成员被用来判断 theap 是否已初始化」，所以必须最后写（release 序）；L299-L305 挂入 heap 的 theaps 链。

[src/heap.c:L263-L267](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/heap.c#L263-L267) —— `mi_heap_of(p)`：从任意指针反查 heap 的实现——`_mi_safe_ptr_page(p)` 走 page map 得到页，再取 `page->heap`。这就是「释放无需 TLS」的落地。

#### 4.3.4 代码实践

1. **实践目标**：用调试器亲眼看见「同一个 heap、不同线程、不同 theap」。
2. **操作步骤**：写一个小程序（示例代码，非项目原有）：

   ```c
   #include <mimalloc.h>
   #include <stdio.h>
   #include <pthread.h>

   static mi_heap_t* g_heap;

   static void* worker(void* arg) {
     (void)arg;
     void* p = mi_heap_malloc(g_heap, 32);      // 触发本线程的 theap 创建
     printf("thread  theap=%p heap=%p\n",
            (void*)mi_heap_theap(g_heap), (void*)g_heap);
     mi_free(p);                                 // 从本线程释放
     return NULL;
   }

   int main(void) {
     g_heap = mi_heap_new();
     void* p = mi_heap_malloc(g_heap, 32);
     printf("main   theap=%p heap=%p\n",
            (void*)mi_heap_theap(g_heap), (void*)g_heap);
     pthread_t t; pthread_create(&t, NULL, worker, NULL);
     pthread_join(t, NULL);
     mi_free(p);
     mi_heap_destroy(g_heap);
     return 0;
   }
   ```

   用 debug 构建编译链接（参见 u1-l2），运行观察两行输出的 `theap` 地址。
3. **需要观察的现象**：两行的 `heap` 地址相同，`theap` 地址不同。
4. **预期结果**：验证「heap 跨线程共享、theap 每线程一份」。若在 `mi_heap_destroy` 前加 `MIMALLOC_SHOW_STATS=1`，还能看到两个 theap 的统计被合并进堆统计。（线程库链接参数因平台而异，具体编译命令待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：`mi_heap_t` 里为什么要有 `theaps_lock`，而 `mi_theap_t` 里几乎没有锁？

**答案**：`heap->theaps` 链是跨线程共享的（任何线程创建 theap 都要挂入，堆销毁线程要遍历），所以需要锁；theap 的字段（页队列、used 计数等）只有拥有线程会写，跨线程只碰 `xthread_free` 这一个原子字段，所以不需要通用锁。

**练习 2**：v2 把线程本地状态直接放在 heap 上，v3 拆成 theap 之后，「跨线程堆分配」为什么就变快了？

**答案**：v2 中两个线程用同一个 heap 分配时要竞争同一组页队列（原子操作或加锁）；v3 中每个线程有自己的 theap 与页队列，各自从自己的页上分配，快路径完全无线同步。页内存仍来自共享 arena，但 arena 只在「领新页」这种低频操作时才接触（u6-l3）。

**练习 3**：`_mi_theap_init` 里为什么 `theap->heap` 必须最后以 release 序写入？

**答案**：因为「`theap->heap != NULL`」被用作 **theap 已初始化** 的判据（如 `mi_theap_is_initialized` 的检查、theap.c L295-L296 的注释）。其他线程会原子地读这个字段来判断 theap 可用性，release 写入保证它可见时，tld、refcount、队列等其余字段的写入也已对读者可见。

### 4.4 page queue、page、block 与 mi_memid_t：从队列到块的落地

#### 4.4.1 概念说明

**页队列（`mi_page_queue_t`）**是 theap 里按 bin 组织页的容器：`first/last` 指向链表两端，`count` 是页数，`block_size` 标明这一档的块大小。theap 的 `pages[MI_BIN_COUNT]` 是一个队列数组——**bin 号就是数组下标**，找队列是 O(1) 的。

**页（`mi_page_t`）**是分配的基本单位：一块连续内存（1 个或多个 64 KiB arena slice），只装一种 `block_size` 的块。页通过 `next/prev` 挂在所属 bin 的队列里。它同时持有向上回溯的两个指针：`theap`（拥有线程的 theap，可能为 NULL——abandoned 页）与 `heap`（const，永不变的归属堆）。

**块（`mi_block_t`）**是用户指针的本体。结构上它只有一个字段 `next`：**空闲时**这里是 free list 链指针，**分配出去后**这里就是用户数据的第一个字。空闲块与已分配块共享同一块内存，零元数据开销。

**`mi_memid_t`（内存产地证）**回答「这块内存从哪来、怎么还」：`memkind` 八个枚举值区分 arena/OS/外部/静态等来源；当 `memkind == MI_MEM_ARENA` 时，union 里的 `arena` 指针 + `slice_index` + `slice_count` 精确记录了它在哪个 arena 的哪些 slice——释放页时按此归还。注意这是一个**全层级模式**：`mi_page_t`、`mi_theap_t`、`mi_heap_t`、`mi_subproc_t`、`mi_tld_t` 每个结构体都有自己的 `memid` 字段，记录**自身结构体内存**的来源。

#### 4.4.2 核心流程

一次小对象分配在模型层面的落点（细节留给 u4）：

```text
theap->pages_free_direct[idx]  ──直接索引──→  mi_page_t（当前推荐页）
mi_page_t.free                            ──弹出表头──→  mi_block_t（返回给用户）
```

页内三条 free list 的计数不变式（本讲只认识，u3-l2 精读）：

\[ \text{used} - |\text{thread\_free}| = \text{存活块数} \]

\[ \text{used} - |\text{thread\_free}| + |\text{free}| + |\text{local\_free}| = \text{capacity} \]

即：容量被拆成「已分配存活」「可立即分配」「本线程延迟释放」「他线程延迟释放」四部分，靠两个计数和三条链表维护，不单独存「freed」计数以减少内存访问。

页与内存的关系：

```text
mi_page_t.memid ──→ { arena*, slice_index, slice_count } ──→ 归还时按 slice 还给 arena 位图
                └──→ { os.base, os.size }                  ──→ 或直接 munmap 还给 OS
```

#### 4.4.3 源码精读

[include/mimalloc/types.h:L528-L533](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L528-L533) —— `mi_page_queue_s`：四个字段，简单到极致；注释说明「某种块大小的页放在一个队列里」。

[include/mimalloc/types.h:L425-L456](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L425-L456) —— `mi_page_s`。本讲关注四个回溯/登记字段：L429 `xthread_id`（拥有线程 id，低位存页标志，0 或 4 表示 abandoned 页）；L444 `theap`（注释明确「可能无效或为 NULL，针对被遗弃的页」）；L445 `heap`（const，**永不改变**——free 反查靠它）；L449 `memid`（const，页内存的产地证）。三条 free list（L430 `free`、L432 `local_free`、L443 `xthread_free`）留到 u3-l2。

[include/mimalloc/types.h:L398-L413](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L398-L413) —— 页与三条链的官方注释，包含上面两个不变式，以及「非原子字段只有在取得所有权后才能访问」「`used - |thread_free|` 才是真实存活数」这两个后续单元反复用到的事实。

[include/mimalloc/types.h:L366-L368](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L366-L368) —— `mi_block_s`：只有一个 `next` 字段（编码为 `mi_encoded_t`）。空闲块本身就是链表节点。

[include/mimalloc/types.h:L288-L297](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L288-L297) —— `mi_memkind_e`：八种内存来源。注意「`MI_MEM_STATIC`：静态区分配、不应释放（例如 init.c 里初始主 theap 数据）」与「`MI_MEM_ARENA`：从 arena 分配（**通常情况**）」。

[include/mimalloc/types.h:L309-L336](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L309-L336) —— `mi_memid_s`：union 按 memkind 三选一（os/arena/malloc），外加 `is_pinned`（大页/巨页内存不能 decommit）、`initially_committed`、`initially_zero` 三个释放与零化决策要用的标志。arena 分支的 `slice_index`/`slice_count` 就是「按 slice 归还」的凭据。

[include/mimalloc/types.h:L499-L505](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L499-L505) —— `mi_page_kind_e`：页的四种形态。前三者（SMALL/MEDIUM/LARGE）是「一页多块」，第四种 `MI_PAGE_SINGLETON` 是「一页一块」——超过 `MI_LARGE_MAX_OBJ_SIZE`（512 KiB）的大对象每个独占一个页，页结构退化成大对象的账本（u4-l3）。

#### 4.4.4 代码实践

1. **实践目标**：算出 theap 里页队列数组与快表的体积，建立「theap 是个大对象」的直观感受。
2. **操作步骤**：`MI_BIN_COUNT` 为 75（73 号 bin 是 HUGE，另有 FULL 与自身，见 types.h L233-L237），`mi_page_queue_t` 每项 4 个字段（2 指针 + 2 size_t = 32 字节）；`MI_PAGES_DIRECT` 约为 `MI_SMALL_SIZE_MAX/8 + 1` 项指针。写一个 10 行的小 C 程序 `printf` 出 `sizeof(mi_page_queue_t)`、`MI_BIN_COUNT`、`MI_PAGES_DIRECT`（头文件 `#include <mimalloc.h>`，internal.h 不对用户暴露，前两个常量可从 types.h 推导后手工代入）。
3. **需要观察的现象**：`sizeof(mi_theap_t)`（可通过在 mimalloc 源码里临时加打印，或直接手算）在几 KiB 量级。
4. **预期结果**：`75 × 32B = 2400B` 的队列数组 + 快表约 1KiB，再加随机上下文与统计，theap 总体积约 4~6 KiB——这就是为什么 theap 用 `mi_decl_cache_align` 对齐并值得放进独立 cache line 的原因之一。具体数值随构建选项（MI_STAT/MI_GUARDED）浮动，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `page->heap` 是 const 而 `page->theap` 可以为 NULL？

**答案**：页的堆归属从创建到销毁不变（`mi_heap_delete` 时页会被整体移给主堆而不是原地改挂），const 有利于编译器优化且能被 free 路径安全依赖；而 theap 归属是线程相关的——拥有线程退出后页被 abandon，`theap` 置 NULL，等新线程认领后再挂到它自己的 theap 上（u6-l4）。

**练习 2**：一个 700 B 的请求会落在哪条页队列？`pages_free_direct` 还是页队列先被用到？

**答案**：700 B ≤ `MI_SMALL_SIZE_MAX`（64 位下 1 KiB，u1-l4 已验证过 size class 台阶），属于 small 对象，bin 由 `_mi_bin` 按请求尺寸取整得出；分配时先查 `pages_free_direct[字数下标]` 直接拿页（O(1)），只有它为空才去 `pages[bin]` 队列找（u4-l1 精读）。

**练习 3**：一个页的内存来自 arena 时，`memid.mem.arena.slice_count = 3` 意味着什么？

**答案**：该页占用了 3 个 64 KiB 的 arena slice（即 192 KiB，可能是一个 512 KiB medium 页按 64 KiB 对齐后的占用，或带对齐损耗的分配）。页被释放时，arena 会把这连续 3 个 slice 在空闲位图里重新置为可用（u6-l3）。

### 4.5 第一次 mi_malloc：从 heap_main 到 block 的完整旅程

#### 4.5.1 概念说明

前面四个模块是静态地图，这个模块让地图动起来：跟踪**第一次** `mi_malloc(32)`。这是全代码库最微妙的一条路径，因为它发生在「分配器自己还不存在」的时刻——快路径会先失败一次，借慢路径完成整个自举。

理解这条链的意义：它一次性串起了全部六层结构，也解释了三个静态对象（`_mi_theap_empty`、`mi_process_heap_main`、`mi_process_theap_main`）为什么必须存在。

#### 4.5.2 核心流程

```text
mi_malloc(32)                                    [alloc.c:256]
└─ mi_theap_malloc(_mi_theap_default(), 32)      ← 读 TLS：此刻指向静态空 theap _mi_theap_empty
   └─ _mi_theap_malloc_zero_ex → 小对象分支
      └─ mi_theap_malloc_small_zero_nonnull      [alloc.c:133]
         ├─ _mi_theap_get_free_small_page        [internal.h:650]  ← pages_free_direct 查页
         └─ mi_page_malloc_zero                  [alloc.c:32]
            └─ page->free == NULL（空 theap 的页没有空闲块）
               └─ _mi_malloc_generic             [page.c:1091]
                  └─ theap 未初始化 → mi_malloc_generic_fallback [page.c:1048]
                     └─ mi_malloc_generic_admin  [page.c:1011]
                        └─ _mi_thread_init → _mi_thread_init_withheap [init.c:306]
                           ├─ mi_process_init()                    ← 进程级初始化
                           │   └─ mi_heap_main_init_once [init.c:184]
                           │       ├─ 主 subproc（静态）
                           │       ├─ detached tld（静态）
                           │       ├─ mi_process_heap_main（静态主堆）→ subproc->heap_main
                           │       └─ mi_process_theap_meta（静态元数据 theap）
                           ├─ mi_tld_create：首个 tld 也是静态的 mi_process_tld_main
                           ├─ theap = &mi_process_theap_main（主线程的默认 theap，静态）
                           ├─ _mi_theap_init：挂 tld 链、挂 heap 链、初始化随机数
                           └─ _mi_theap_default_set(theap)：TLS 从此指向真 theap
                        ← 回到分配：mi_find_page → 队列空 → mi_page_fresh
                           └─ 向 arena 领 slice → 初始化页 → 挂入 pages[bin] 队列
                              → 更新 pages_free_direct → 弹出第一个 block 返回
```

主线程与普通线程的唯一差别：主线程的 tld/theap 用静态五件套；普通线程的 tld/theap 通过 `_mi_meta_zalloc` 在 `theap_meta` 上动态分配（init.c L338-L346 的 if/else）。之后所有分配都走快路径，再也不会进入这段自举代码。

#### 4.5.3 源码精读

[src/alloc.c:L256-L258](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L256-L258) —— `mi_malloc` 只有两行：取默认 theap，转内层函数。**「不带 heap 参数的 mi_ 函数第一步都取默认 theap」**在源码上就长这样。

[include/mimalloc/internal.h:L650-L655](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L650-L655) —— `_mi_theap_get_free_small_page`：把尺寸换算成字数下标，直接索引 `theap->pages_free_direct[idx]`，常数时间拿到候选页。

[src/alloc.c:L40-L57](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L40-L57) —— `mi_page_malloc_zero` 的核心：读 `page->free`，为 NULL 就整体转交 `_mi_malloc_generic`（L46-L48）；否则弹出链表头、`used+1`、返回块。注释（L29-L31）说明 release 下内联后**约 7 条指令**。

[src/page.c:L1011-L1021](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L1011-L1021) —— `mi_malloc_generic_admin`：慢路径的管理入口。`!mi_theap_is_initialized(theap)` 成立时调用 `_mi_thread_init()`——**第一次分配就是在这里触发线程/进程初始化的**；若 theap 是 `_mi_theap_empty_wrong`（为一等堆分配 theap 失败的哨兵）则直接返回 NULL。

[src/init.c:L184-L208](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L184-L208) —— `mi_heap_main_init_once`：自举的核心。按顺序：初始化主 subproc → 初始化 detached tld → 静态主堆写入 `subproc_main->heap_main` 并 `_mi_heap_init` → 初始化 `mi_process_theap_meta`（detached 元数据 theap，L203-L204 还特意关掉其 page abandon 以保安全）。全程零动态分配。

[src/init.c:L306-L361](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L306-L361) —— `_mi_thread_init_withheap`：线程初始化。L312-L313 幂等检查（默认 theap 已初始化就直接返回）；L335 创建 tld；L338-L345 的 if/else 正是「主线程用静态 `mi_process_theap_main`，其他线程 `_mi_theap_alloc` 动态分配」的分界；L350-L352 依次设置默认 theap 与 heap 的 TLS 槽。

[src/init.c:L167-L173](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L167-L173) —— `_mi_theap_empty_get` 与 `_mi_is_empty_theap`：静态空 theap 的存在性证明。线程退出时默认 theap 也会被重置回它（init.c L396-L397），因此「空 theap」贯穿进程始终。

[src/theap.c:L172-L180](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L172-L180) —— `mi_theap_get_default`：编程接口侧的兜底——发现默认 theap 未初始化就调 `mi_thread_init()`。

#### 4.5.4 代码实践

1. **实践目标**：用运行时证据核对这些初始化动作真的发生了，并记下它们的先后顺序。
2. **操作步骤**：
   - 按 u1-l2 构建 release 版；
   - 写最小程序 `int main(void){ return mi_malloc(32) != NULL; }` 链接 libmimalloc；
   - 以 `MIMALLOC_VERBOSE=1 ./a.out` 运行（verbose 横幅在 `main` 之前由初始化路径打出，u2-l1 讲过）；
   - 再用 debug 构建 + `MIMALLOC_SHOW_STATS=1` 运行，观察输出顶部是否有线程/堆相关统计行。
3. **需要观察的现象**：verbose 横幅出现**在任何用户输出之前**——证明初始化发生在第一次分配内部而非 main 开头；stats 里 `process` 段有 arena commit 记录——证明第一次分配确实向 arena 领过内存。
4. **预期结果**：能对上 4.5.2 流程图中「mi_process_init 在首次 mi_malloc 内部被触发」这一事实。若在 gdb 里在 `mi_heap_main_init_once` 与 `_mi_theap_init` 上打断点（debug 构建符号完整），可看到前者的调用栈深埋在 `mi_malloc` 之下——**待本地验证**（取决于平台与链接方式）。

#### 4.5.5 小练习与答案

**练习 1**：既然「第一次分配会触发初始化」，为什么快路径代码里看不到任何 `if (initialized)` 判断？

**答案**：判断被「静态空 theap」吸收了：TLS 默认指向 `_mi_theap_empty`（一个合法但全空的 theap），快路径照常索引 `pages_free_direct`、照常读 `page->free`，只是必然得到 NULL，从而自然地落入 `_mi_malloc_generic`。初始化检查只存在于慢路径（`mi_theap_is_initialized`）。types.h L519-L521 注释明说这是「为了避免快路径上的初始化检查」。

**练习 2**：非主线程第一次分配与主线程第一次分配，路径有何不同？

**答案**：共同点：都从空 theap 落入 generic 再触发线程初始化。不同点：主线程的 tld、theap 直接用静态的 `mi_process_tld_main`/`mi_process_theap_main`（init.c L338-L341），且进程初始化可能也要在此完成；非主线程时进程早已初始化，只是 `mi_tld_create` 与 `_mi_theap_alloc` 走 `_mi_meta_zalloc` 动态分配（init.c L342-L344），内存产自 `theap_meta`。

**练习 3**：`_mi_theap_empty_wrong`（init.c L159）这个「错误版空 theap」是给谁用的？

**答案**：给「一等堆的 theap 分配失败」的场景：`_mi_heap_theap_get_or_init`（heap.c L96）在 `mi_heap_init_theap` 失败时返回它，随后 `mi_malloc_generic_admin`（page.c L1014-L1017）识别出它并让分配返回 NULL——用一个哨兵对象把 OOM 传播进慢路径，避免在快路径上多加判断。

## 5. 综合实践

把本讲所有内容串成一张可以留存的「所有权地图」（对应大纲里的实践任务）：

1. **实践目标**：脱离讲义，独立产出 (a) 一张五层 ownership 手绘图；(b) 第一次分配路径的书面回答。
2. **操作步骤**：
   - 通读 [include/mimalloc/types.h:L11-L23](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L11-L23)、L425-L456（page）、L561-L598（theap）、L618-L639（heap）、L651-L680（subproc）、L691-L701（tld）的字段注释；
   - 画出节点（六结构 + tld + arena），用实线箭头标「拥有/挂链」关系（subproc→heap、heap→theap、tld→theap、theap→page queue→page→block、subproc→arena），用虚线箭头标「回溯/反查」关系（page→heap、page→theap、theap→heap、指针→page 的 page map）；
   - 在图上用三种颜色分别标出：**只有拥有线程可写**的字段区（theap 队列、page 的 free/used）、**跨线程共享需原子/锁**的字段区（subproc、heap 的链与位图）、**const 永不改变**的字段（page->heap、page->memid、block_size）；
   - 书面回答：**一个普通线程第一次 `mi_malloc` 时，从 `heap_main` 到最终 block 依次经过哪些结构？** 对照 4.5.2 的流程图自评。
3. **需要观察的现象**：画完后你会发现图上「向下」的路径（subproc→…→block）在分配时几乎不被触碰——分配真正走的只有 TLS→theap→page 三跳；「向上」的路径（page→heap→subproc）主要服务于 free 与统计。
4. **预期结果**：参考答案——普通线程第一次分配：TLS 默认 theap 为空 → 落入 generic → `_mi_thread_init`：进程已初始化（主 subproc、`heap_main` 均就绪），为本线程创建 tld（动态）与默认 theap（动态，挂入 `heap_main->theaps` 与 `tld->theaps`）→ `mi_find_page`：`heap_main` 对应 bin 的队列空 → `mi_page_fresh`：从 subproc 的 arena 领 slice、构造 `mi_page_t`（`page->heap = heap_main`）挂入 theap 的 `pages[bin]` 队列并登记进 `heap->arena_pages` → 页内切出第一个 `mi_block_t` 返回。经过的结构依次是：**TLS → theap（新建）→ page queue → page（新建）→ block；heap_main 与 subproc 作为归属被登记，而非被遍历**。

## 6. 本讲小结

- mimalloc v3 的对象模型是**五层所有权 + 两个横切结构**：`subproc`（根，持有 arena 与 heaps）→ `heap`（一等堆，跨线程身份与页登记）→ `theap`（线程本地堆，真正的分配车间）→ `page queue`（按 bin 组织页）→ `page` → `block`；`tld` 提供线程身份，`arena` 提供内存批发。
- **v3 引入 theap 的动机**：把线程本地状态从 heap 拆出，使任意线程都能在同一个 heap 上无锁分配——heap 管身份与账本，theap 管队列与快表。
- 看 `_Atomic` 字段分布即可判断共享层级：subproc/heap 满是原子与锁，theap/page 几乎全是普通字段；例外是 `page->xthread_free`（跨线程 free 的入口）。
- `mi_memid_t` 是全层级的「内存产地证」：每个结构体都记录自身内存来源；页的 memid 含 arena 指针与 slice 区间，是释放时按 slice 归还的凭据。
- 自举三件套（静态主 subproc/主堆/主 theap + 空头哨兵 `_mi_theap_empty`）让「第一次分配触发全部初始化」无需在快路径加任何判断——初始化检查被空 theap 自然吸收进慢路径。
- 释放走的是完全不同的路：指针 →（page map）→ `page` → `page->heap`，全程不查 TLS——这就是跨线程 free 便宜的结构基础（u5 展开）。

## 7. 下一步学习建议

下一讲 **u3-l2《mi_page_t 深入：一个页的三条 free list》**钻进本讲只是「认识了一下」的 `mi_page_s`：精读 `free`/`local_free`/`thread_free` 三条链的分工与迁移时机、`used`/`capacity`/`reserved` 三个计数的精确语义、`xthread_id` 低位存放页标志的位技巧。建议先自己重读 [include/mimalloc/types.h:L398-L456](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L398-L456) 两遍再开讲。随后 u3-l3 讲 size class 与 bin 如何决定一个请求落在 `pages[MI_BIN_COUNT]` 的哪一条队列，u3-l4 讲 free 反查用的 page map——四讲合起来，单元三「核心数据结构」就闭环了。
