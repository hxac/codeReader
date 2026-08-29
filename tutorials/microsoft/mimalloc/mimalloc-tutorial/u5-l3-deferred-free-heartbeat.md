# 延迟释放与心跳：为运行时系统提供的钩子

## 1. 本讲目标

mimalloc 出身于 Koka/Lean 这类函数式语言的运行时，这类运行时普遍使用引用计数（reference counting）管理对象：对象死亡时要做引用计数减一、可能级联释放一整棵对象树。如果每次 `free` 都立刻做这些事，最坏情况延迟不可控。mimalloc 为此专门暴露了一对钩子：**单调心跳（monotonic heartbeat）** 与 **延迟释放（deferred freeing）**。

学完本讲，你应该能够：

1. 掌握 `mi_register_deferred_free` 回调的注册时机（进程内只装一个、且应在开始分配之前装）与触发节奏（由慢路径分配计数驱动，每 1000 次 generic 分配一拍）。
2. 理解 `theap->heartbeat` 单调计数如何配合 `local_free` 链表的分离，让运行时系统在安全点（safepoint）做**有界**（bounded）的批量回收。
3. 厘清三层容易混淆的「延迟/计数」语义：运行时层面的延迟释放钩子、页内 `local_free` 的延迟可用、以及跨线程 free 才有的「计数不减 `used`」。

本讲承接 u5-l2 的结论——`thread_free` 链与所有权位——并把 u3-l2 埋下的伏笔（`local_free` 独立出来是为了实现单调心跳）讲透。

## 2. 前置知识

- **引用计数运行时的痛点**：对象通过引用计数管理时，一次 `free` 可能触发任意深的级联 decref。运行时希望把这类工作攒起来、每隔一段确定的时间批量做一点，从而把单次停顿压在常数界内——这就是「有界最坏情况时间」（bounded worst-case times）。
- **安全点（safepoint）**：运行时保证自身数据结构处于一致状态、可以安全执行回收动作的位置。mimalloc 的心跳回调就是一个天然的安全点：它由分配器内部在确定的管理时机触发，此时页的三条链表处于稳定状态。
- **快路径与慢路径**：u4-l1 讲过，`mi_malloc` 对小对象先走直查数组的快路径；快路径失败（典型情形是当前页的 `free` 链弹空）就进入 `_mi_malloc_generic` 慢路径。**本讲的心跳就挂在慢路径的管理逻辑上**。
- **generic 分配**：即进入 `_mi_malloc_generic` 的分配。一个页的 `free` 链有多长，就能连续服务多少次快路径分配；`free` 弹空那一次才落进 generic 路径。因此 generic 调用的频率是可预测的——这是心跳「确定性」的来源。
- **原子 load/store 与 acquire/release 序**：u8-l1 会展开，这里只需知道「先 release 写、后 acquire 读」能保证读者看到回调指针时一定也能看到它的参数。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [include/mimalloc.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h) | 公共 API：`mi_deferred_free_fun` 回调类型、`mi_register_deferred_free`、`mi_collect`、`mi_option_generic_collect` 选项 |
| [src/page.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c) | 心跳的**实现**（`_mi_deferred_free`、`mi_register_deferred_free`）与**触发**（`mi_malloc_generic_admin` 的千次节拍）；`local_free → free` 的迁移 |
| [src/alloc.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c) | 分配入口：快路径失败后转发 `_mi_malloc_generic`（本讲链路的起点） |
| [src/free.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c) | 释放侧对照：本地 free（减 `used`）与跨线程 free（不减 `used`） |
| [src/theap.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c) | `mi_theap_collect_ex`：force 语义下心跳回调的另一触发点 |
| [include/mimalloc/types.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h) | 数据结构：`heartbeat`、`generic_count`、`recurse` 字段与三条链表的设计注释 |
| [src/options.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c) | `mi_option_generic_collect` 选项默认值（10000） |

## 4. 核心概念与源码讲解

### 4.1 回调的注册与调用：mi_register_deferred_free 与 _mi_deferred_free

#### 4.1.1 概念说明

mimalloc 提供的不是「帮你延迟释放 mimalloc 的内存」，而是一个**通知钩子**：宿主运行时（如 Koka/Lean）注册一个回调，mimalloc 在自己内部的管理时机调用它，运行时借此把自己的延迟工作（引用计数批量 decref）安排到安全点上。readme 把它列为设计要点之一：

> it provides hooks for a monotonic _heartbeat_ and deferred freeing (for bounded worst-case times with reference counting)——[readme.md:30-38](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L30-L38)

两个关键词要分开理解：

- **heartbeat**：一个只会递增的 64 位计数，每拍 +1，随回调一起传给运行时；
- **deferred free**：回调本身。运行时在回调里处理自己积压的释放队列。

#### 4.1.2 核心流程

```text
运行时启动早期（任何分配之前）
  └─ mi_register_deferred_free(fn, arg)
       ├─ release 写 deferred_arg = arg
       └─ release 写 deferred_free = fn        ← 先参数后函数，顺序有讲究

此后某线程的分配慢路径
  └─ _mi_deferred_free(theap, force)
       ├─ theap->heartbeat++                    ← 无条件递增（即使没注册回调）
       ├─ acquire 读 deferred_free
       ├─ 若 fn != NULL 且 !theap->tld->recurse  ← 递归保护
       │    ├─ recurse = true
       │    ├─ acquire 读 deferred_arg
       │    ├─ fn(force, theap->heartbeat, arg) ← 真正的安全点
       │    └─ recurse = false
       └─ 返回
```

注意三点：回调是**每个线程独立触发**的（心跳长在每个 theap 上）；`heartbeat++` 在判空**之前**，所以计数与是否注册回调无关；`recurse` 标志防止回调里再分配内存、再次进入慢路径造成的无限递归。

#### 4.1.3 源码精读

公共 API 只有一行类型定义加一行声明（[include/mimalloc.h:184-185](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L184-L185)）：

```c
typedef void (mi_cdecl mi_deferred_free_fun)(bool force, unsigned long long heartbeat, void* arg);
mi_decl_export void mi_register_deferred_free(mi_deferred_free_fun* deferred_free, void* arg) mi_attr_noexcept;
```

回调收到三个参数：`force`（是否要求一口气清完积压）、`heartbeat`（本 theap 的单调计数）、`arg`（注册时传入的用户指针）。

实现与存储都在 page.c 的一个专门小节里（[src/page.c:979-1004](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L979-L1004)）。开头的注释直接说出了确定性来源：

```c
/* -----------------------------------------------------------
  Users can register a deferred free function called
  when the `free` list is empty. Since the `local_free`
  is separate this is deterministically called after
  a certain number of allocations.
----------------------------------------------------------- */

// The program should only install a single deferred free handler before doing allocation.
static _Atomic(void*) deferred_free; // is `mi_deferred_free_fun*` (but some platforms don't support atomic function pointers)
static _Atomic(void*) deferred_arg;
```

- 存储是**进程级**的两个静态原子指针，不是 per-thread 的：所有 theap 共享同一个回调。注释明确了使用契约——只装一个、在开始分配之前装。
- 用 `_Atomic(void*)` 而不是函数指针原子，是因为部分平台不支持原子函数指针类型。

注册函数（[src/page.c:1001-1004](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L1001-L1004)）：

```c
void mi_register_deferred_free(mi_deferred_free_fun* fn, void* arg) mi_attr_noexcept {
  mi_atomic_store_ptr_release(void,&deferred_arg, arg);
  mi_atomic_store_ptr_release(void,&deferred_free, (void*)fn);
}
```

**先写 `arg` 再写 `fn`**，两次都是 release。这样任何线程 acquire 读到 `fn != NULL` 时，`arg` 必然已就绪——经典的发布顺序。

调用侧（[src/page.c:990-999](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L990-L999)）：

```c
void _mi_deferred_free(mi_theap_t* theap, bool force) {
  theap->heartbeat++;
  mi_deferred_free_fun* const fun = (mi_deferred_free_fun*)mi_atomic_load_ptr_acquire(void,&deferred_free);
  if (fun != NULL && !theap->tld->recurse) {
    theap->tld->recurse = true;
    void* const arg = mi_atomic_load_ptr_acquire(void,&deferred_arg);
    fun(force, theap->heartbeat, arg);
    theap->tld->recurse = false;
  }
}
```

`recurse` 是线程本地数据 `mi_tld_t` 里的一个布尔字段（[include/mimalloc/types.h:690-701](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L690-L701)）：

```c
// Thread local data
struct mi_tld_s {
  ...
  bool                  recurse;              // true if deferred was called; used to prevent infinite recursion.
  ...
};
```

它防护的场景很具体：回调里调了 `mi_malloc`（比如运行时打印日志）→ 快路径失败进 generic → 又到了 `_mi_deferred_free` → 再次调回调……`recurse` 让这条链在第二环断开。心跳字段则挂在 theap 上、每线程一份（[include/mimalloc/types.h:560-598](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L560-L598)）：

```c
struct mi_theap_s {
  ...
  unsigned long long    heartbeat;                           // monotonic heartbeat count
  ...
  long                  generic_count;                       // how often is `_mi_malloc_generic` called?
  long                  generic_collect_count;               // how often is `_mi_malloc_generic` called without collecting?
```

主线程的静态自举 theap 里它初始化为 0（[src/init.c:120-126](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L120-L126)），detached tld 的 `recurse` 初始化为 false（[src/init.c:108-118](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L108-L118)）。

#### 4.1.4 代码实践

**实践目标**：验证「注册 → 被调用 → 拿到单调心跳」这条最小链路。

**操作步骤**：

1. 按 u1-l2 构建 release 版库（`mkdir -p out/release && cd out/release && cmake ../.. && make`）。
2. 编写下面的程序（**示例代码**，仓库 test/ 目录中没有使用 `mi_register_deferred_free` 的现成测试，需自行编写）：

```c
/* deferred_min.c —— 示例代码：最小心跳验证 */
#include <stdio.h>
#include <mimalloc.h>

static void on_deferred_free(bool force, unsigned long long heartbeat, void* arg) {
  (void)arg;
  printf("deferred: force=%d heartbeat=%llu\n", force ? 1 : 0, heartbeat);
}

int main(void) {
  mi_register_deferred_free(on_deferred_free, NULL);
  for (int i = 0; i < 200000; i++) {
    void* p = mi_malloc(32);
    mi_free(p);
  }
  mi_collect(true);   /* 收尾时主动 force 一次 */
  return 0;
}
```

3. 编译运行（静态链接最省事）：

```bash
gcc -O2 deferred_min.c -Iinclude -o deferred_min out/release/libmimalloc.a -lpthread -lm
./deferred_min
```

**需要观察的现象**：输出多行 `deferred: force=0 heartbeat=1..N`，heartbeat 严格递增、每次 +1；最后一行因 `mi_collect(true)` 呈 `force=1`。

**预期结果**：心跳序号连续无重复（每线程独立计数）；若把 `mi_register_deferred_free` 挪到循环之后，则几乎收不到非 force 回调——印证「应在分配之前注册」。精确的回调总次数属 4.3 的节奏问题，此处先不纠结。具体输出数值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `mi_register_deferred_free` 要先 release 写 `arg`、后 release 写 `fn`？反过来写会怎样？

**答案**：先写 `fn` 的话，另一个线程可能 acquire 读到非空的 `fn` 但 `arg` 还是旧值/NULL，回调一执行就拿错参数。先 `arg` 后 `fn` 的 release 顺序保证了「看到 `fn` 就必然看到配套的 `arg`」，这是无锁发布（publication）的标准写法。

**练习 2**：如果回调函数里调用 `mi_malloc` 且快路径失败，会发生什么？哪行代码救了场？

**答案**：会沿 `_mi_malloc_generic` 再次走到 `_mi_deferred_free`。救场的是 [src/page.c:993](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L993) 的 `!theap->tld->recurse` 判定——第一层调用已把 `recurse` 置 true，第二层只递增心跳、不再重入回调，无限递归被截断。

**练习 3**：两个线程各自大量分配，注册的回调会被并发调用吗？心跳会串吗？

**答案**：回调可能被并发调用（每个线程在自己的慢路径上各自触发），但**不会串心跳**——`heartbeat` 是 `mi_theap_s` 的普通字段（每线程一份，[types.h:570](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L570)），各线程只读写自己的 theap，因此无需原子操作也无线程竞争；`recurse` 同理在 `mi_tld_t` 里。这也是 u3-l1「theap 是线程私有层」结论的又一次体现。

### 4.2 心跳为什么是「确定性的」：local_free 的分离

#### 4.2.1 概念说明

「每 1000 次 generic 分配触发一拍」只是心跳的**节拍器**；心跳对运行时有价值，靠的是**拍与拍之间经过的分配次数是可预测的**。这正是 `local_free` 链存在的理由。types.h 的页结构注释写得很直白（[include/mimalloc/types.h:398-413](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L398-L413)）：

```c
// `free` for blocks that can be allocated,
// `local_free` for freed blocks that are not yet available to `mi_malloc`
// `thread_free` for freed blocks by other threads
// The `local_free` and `thread_free` lists are migrated to the `free` list
// when it is exhausted. The separate `local_free` list is necessary to
// implement a monotonic heartbeat. The thread_free list is needed for
// avoiding atomic operations when allocating from the owning thread.
```

假如没有 `local_free`：每次 `mi_free` 立刻把块塞回 `free`，那么 `free` 什么时候耗尽取决于释放的时机——完全由应用逻辑决定，不可预测。把本线程释放的块先扣在 `local_free` 里，`free` 的长度就只由**上一次收割时迁移进来多少块**决定；`free` 弹空 → 下一次分配进 generic 路径 → 管理节拍可预期地推进。运行时因此敢做这样的承诺：**两拍之间我最多处理 N 个 decref，且这个 N 有上界**——有界回收的「界」就是这么来的。

#### 4.2.2 核心流程

页内块的一次完整「延迟—回收」周期：

```text
mi_free(p)（本线程，p 属于页 page）
  └─ used-- ；p 头插 page->local_free          ← 已死，但对 mi_malloc 不可见
       ↓ （free 链继续被快路径消耗……）
free == NULL
  └─ 分配落入 generic 路径 → 某次管理节拍到达
       └─ _mi_page_free_collect / mi_page_free_quick_collect
            └─ page->free = page->local_free; page->local_free = NULL   ← O(1) 整链搬迁
```

跨线程释放的块则先经 u5-l2 讲的 CAS 推入 `xthread_free`，由属主在收割时并入 `local_free`，再随上述周期回到 `free`。

#### 4.2.3 源码精读

释放侧：本线程 free 只做三件事——减 `used`、链表头插 `local_free`（[src/free.c:28-57](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L28-L57)）：

```c
  // actual free: push on the local free list
  const mi_used_t used = page->used - 1;
  mi_block_set_next(page, block, page->local_free);
  page->used = used;
  page->local_free = block;
```

跨线程释放的块并入时也是**接在 `local_free` 头上**（[src/page.c:150-183](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L150-L183)）：

```c
  // and append the current local free list
  mi_block_set_next(page, last, page->local_free);
  page->local_free = head;
  ...
  page->used = page->used - (uint16_t)count;
```

也就是说 `local_free` 汇聚了「本线程释放 + 收割来的跨线程释放」两类延迟块，`used` 在收割那一刻才被成批修正。

`local_free → free` 的迁移是 O(1) 的指针赋值。快版本在队首页直查时使用（[src/page.c:204-212](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L204-L212)）：

```c
// returns `true` if after collection `mi_page_immediate_available` is true.
static inline bool mi_page_free_quick_collect(mi_page_t* page) {
  if mi_likely(page->free != NULL) return true;
  if (page->local_free == NULL) return false;
  // move local_free to free
  page->free = page->local_free;
  page->local_free = NULL;
  page->free_is_zero = false;
  return true;
}
```

注意条件顺序：`free` 非空直接成功（连 `local_free` 都不碰），只有 `free` 空了才整链搬——「迁移发生在耗尽之时」的精确体现。完整版 `_mi_page_free_collect`（[src/page.c:214-243](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L214-L243)）先收割 `thread_free` 再搬 `local_free`，且 `force` 时才做两链拼接的线性操作（仅在关闭阶段用）。

「立即可分配」的判定只看 `free` 一个字段（[include/mimalloc/internal.h:904-907](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L904-L907)）：

```c
static inline bool mi_page_immediate_available(const mi_page_t* page) {
  return (page->free != NULL);
}
```

`local_free` 里的块对它「不可见」——这正是延迟可用性的定义。

结合 u3-l2 的不变式，`free` 的长度上界为 \( |free| = capacity - used - |local\_free| \)，`capacity` 又由 extend 每次约 8 KiB 分批推进（u4-l2）。所以两次 generic 调用之间至多发生 \( capacity \) 次快路径分配，**每拍之间的分配次数有硬上界**。

#### 4.2.4 代码实践

**实践目标**：用源码阅读型实践确认「`local_free` 迁移只发生在 `free` 耗尽之后」。

**操作步骤**：

1. 读 [src/page.c:879-901](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L879-L901) 的 `mi_page_queue_lookup_free_first`：它对队首页调 `mi_page_free_quick_collect`，返回 false 才进入 `mi_page_queue_find_free_ex` 的完整搜索。
2. 在 [src/page.c:786-789](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L786-L789) 处确认完整搜索里 `_mi_page_free_collect` 的调用条件是 `!immediate_available`——即只有当前页 `free == NULL` 才触发收集。
3. 画出时序：分配 32 字节块 N 个 → 全部 free → 再分配。标注每一步 `free`/`local_free`/`used` 的变化。

**需要观察的现象**：纸面推演中，「free 全部弹空」那次分配是唯一进入 generic 路径的分配；此前所有 free 都只改 `local_free`。

**预期结果**：你应当能得出结论——对一个固定 size class 的纯「分配→释放→再分配」循环，generic 调用大约每 \( \text{capacity} \) 次分配发生一次，节奏与应用的释放时机无关。这是纸面推演实践，结论可用 4.3 的运行实验交叉验证（精确数值**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：把 `local_free` 合并进 `free`（每次 free 直接头插 `free`），心跳的确定性会被破坏吗？为什么？

**答案**：会。`free` 的长度将同时取决于分配和释放两个节奏，`free` 何时耗尽由应用的 free 时机决定，generic 调用的间隔变得不可预测；运行时也就无法对两拍之间的工作量给出上界。分离 `local_free` 把「释放」从 `free` 的补给线里摘除，只留下收割这一个确定的补给时机。

**练习 2**：`mi_page_free_quick_collect` 为什么先判 `page->free != NULL` 就直接返回 true，哪怕 `local_free` 非空？

**答案**：它的契约是「页是否立即可分配」，而不是「是否要收集」。`free` 非空时快路径本来就能弹块，提前搬 `local_free` 只会让 `free` 变长、白白把延迟块提前放出来，破坏心跳节拍，还多写两个缓存行。

**练习 3**：跨线程释放的块什么时候才修正 `used` 计数？

**答案**：推入 `xthread_free` 时不改 `used`（u5-l2 讲过，[src/free.c:80-97](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L80-L97) 的 CAS 路径完全不碰 `used`）；要等属主线程收割整链、在 [src/page.c:182](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L182) `page->used = page->used - count` 处一次性成批修正。这也是为什么页的存活数要写成 \( alive = used - |thread\_free| \)。

### 4.3 触发节奏：千次节拍、万次全量回收与 generic_collect 调参

#### 4.3.1 概念说明

心跳不是每次 generic 分配都跳，而是由一个两级节拍器控制：

- **第一级（1000 次）**：每 1000 次 generic 分配做一次「管理」，心跳 +1；
- **第二级（默认 10000 次）**：累计的 generic 分配数达到 `mi_option_generic_collect`（默认 10000）时，这次管理升级为**全量 collect**（遍历所有页、迁移所有延迟链、回收空页、收集 arena）。

mini 与 full 的区别直接影响运行时的行为：full collect 会把**所有页**的 `local_free`/`thread_free` 都收进 `free`，相当于一次彻底的大补给。

#### 4.3.2 核心流程

```text
每次分配：快路径失败
  └─ _mi_malloc_generic(theap, …)
       ├─ ++theap->generic_count < 1000 且小对象？
       │    └─ 是：只做队首快查，直接返回              ← 不产生心跳
       └─ 否：mi_malloc_generic_fallback
            └─ mi_malloc_generic_admin
                 ├─ generic_count >= 1000 ?
                 │    ├─ generic_collect_count += generic_count; generic_count = 0
                 │    ├─ generic_collect_count >= mi_option_generic_collect（默认 10000，钳位 [1,10^6]）？
                 │    │    ├─ 是：mi_theap_collect(theap,false)   ← 全量回收（内部也会 _mi_deferred_free）
                 │    │    └─ 否：_mi_deferred_free(theap,false)  ← mini：心跳 +1 + 回调
                 │    │            ＋ _mi_theap_collect_retired    ← 顺手释放到期的退役页
```

一个容易忽略的细节：mini 分支里心跳来自 `_mi_deferred_free`；full 分支里 `_mi_deferred_free` 藏在 `mi_theap_collect_ex` 内部（见 4.4）。两条路都会让心跳 +1，所以**心跳的步长恒为 1**，变化的只是每拍附带的工作量。

#### 4.3.3 源码精读

计数入口在 `_mi_malloc_generic` 的快试条件里（[src/page.c:1091-1117](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L1091-L1117)）：

```c
  // fast path objects that fit in a small page
  if mi_likely(mi_theap_is_initialized(theap) && ++theap->generic_count < 1000 && huge_alignment==0) {
    ...  // 队首快查，成功则直接返回，不走管理逻辑
  }
  // otherwise fallback
  return mi_malloc_generic_fallback(theap,size,zero, huge_alignment,ppage);
```

`++theap->generic_count` 说明**每次进 generic 路径都计数**，但小于 1000 时只做轻量快试——千次以内的心跳一拍都不产生。节拍器的主体是 `mi_malloc_generic_admin`（[src/page.c:1011-1042](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L1011-L1042)）：

```c
  // do administrative tasks every N generic mallocs
  if mi_unlikely(theap->generic_count >= 1000) {
    theap->generic_collect_count += theap->generic_count;
    theap->generic_count = 0;

    // do a full theap collect every once in a while (10000 by default)
    const long generic_collect = mi_option_get_clamp(mi_option_generic_collect, 1, 1000000L);
    if (theap->generic_collect_count >= generic_collect) {
      theap->generic_collect_count = 0;
      mi_theap_collect(theap, false /* force? */);
    }
    else {
      // otherwise we do a mini-collect
      _mi_deferred_free(theap, false);         // call potential deferred free routines
      _mi_theap_collect_retired(theap, false); // free retired pages
    }
  }
```

值得注意的实现细节：

- 阈值 1000 是**硬编码**的（同一常量出现在 L1025 与 L1101 两处配合使用），用户可调的只有第二级的 `mi_option_generic_collect`；
- `mi_option_get_clamp(..., 1, 1000000L)` 把选项钳位在 \([1, 10^6]\)，防止配置成 0 或负数让节拍器失灵；
- mini 分支除了心跳还顺手做 `_mi_theap_collect_retired`——把退役倒计时到期的空页真正归还（u5-l1 讲过 retire 机制：空页先带 `retire_expire` 缓冲约 16 个管理周期，[src/page.c:414-457](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L414-L457)）。所以**心跳拍同时驱动了退役页的回收节奏**。

选项定义在 [src/options.c:160](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L160)，枚举项在 [include/mimalloc.h:500](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L500)：

```c
  { 10000, MI_OPTION_UNINIT, MI_OPTION(generic_collect) },        // collect theaps every N (=10000) generic allocation calls
```

按 u2-l3 的规则，它对应环境变量 `MIMALLOC_GENERIC_COLLECT`，也可用 `mi_option_set(mi_option_generic_collect, N)` 编程设置。

#### 4.3.4 代码实践

**实践目标**：编写模拟引用计数运行时，量化「每多少次分配触发一拍回调」，并通过 `mi_option_generic_collect` 观察节奏变化。

**操作步骤**：

1. 编写程序（**示例代码**）：

```c
/* rc_runtime.c —— 示例代码：模拟引用计数运行时的心跳消费 */
#include <stdio.h>
#include <mimalloc.h>

static long g_allocs = 0;          /* 本线程累计分配次数 */
static long g_last_allocs = 0;
static unsigned long long g_ticks = 0;

static void rc_safepoint(bool force, unsigned long long heartbeat, void* arg) {
  (void)arg;
  g_ticks++;
  long delta = g_allocs - g_last_allocs;
  g_last_allocs = g_allocs;
  /* 模拟有界回收：每拍最多处理 B 个积压 decref */
  printf("tick#%llu force=%d hb=%llu allocs_since_last=%ld\n",
         g_ticks, force ? 1 : 0, heartbeat, delta);
}

int main(void) {
  mi_register_deferred_free(rc_safepoint, NULL);
  enum { N = 100000, ROUNDS = 10 };
  for (int r = 0; r < ROUNDS; r++) {
    void* p[N];
    for (int i = 0; i < N; i++) { p[i] = mi_malloc(32); g_allocs++; }
    for (int i = 0; i < N; i++) { mi_free(p[i]); }
  }
  printf("total allocs=%ld ticks=%llu avg allocs/tick=%.1f\n",
         g_allocs, g_ticks, g_ticks ? (double)g_allocs / g_ticks : 0.0);
  return 0;
}
```

2. 分别以三组配置运行，各跑 3 次取平均（**待本地验证**）：

```bash
gcc -O2 rc_runtime.c -Iinclude -o rc_runtime out/release/libmimalloc.a -lpthread -lm

./rc_runtime                                     # 默认 generic_collect=10000
env MIMALLOC_GENERIC_COLLECT=1    ./rc_runtime   # 每拍都升级为全量 collect
env MIMALLOC_GENERIC_COLLECT=1000000 ./rc_runtime # 几乎全是 mini 拍
```

**需要观察的现象**：

- `allocs_since_last` 围绕某个均值小幅波动，但存在清晰上界（对应 4.2 推导的 capacity 上界效应）；
- `MIMALLOC_GENERIC_COLLECT=1` 时每拍都是全量 collect：所有页的延迟链被彻底搬空，`free` 补给更足，预期 `allocs_since_last` 变大、总拍数变少，但单拍耗时上升（每拍要遍历全部页并收集 arena）；
- `=1000000` 时几乎全是 mini 拍：拍数最多、单拍最便宜，但跨线程/深延迟链回收更不及时。

**预期结果**：三组配置下「总分配数 / 总拍数」明显不同，说明该选项直接调节了运行时安全点的密度与强度的取舍。具体数值依赖平台与 32 字节 bin 的页容量，**待本地验证**；若结果与上述定性预测不符，回到 4.3.3 的源码逐行核对是哪条分支造成了偏差。

#### 4.3.5 小练习与答案

**练习 1**：心跳每拍 +1 恒定，那 `mi_option_generic_collect` 改的是什么？

**答案**：改的是**每拍的附带工作量**，不是心跳步长。阈值到达时 mini 拍升级为 `mi_theap_collect` 全量回收（遍历所有页、迁移全部延迟链、回收空页、收集 arena）；两次全量之间都是只做心跳 + 退役页回收的 mini 拍。默认 10000 意味着约每 10 拍一次全量。

**练习 2**：把 `MIMALLOC_GENERIC_COLLECT` 设成 0 或 -1 会怎样？

**答案**：不会生效为 0/-1。[src/page.c:1030](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L1030) 用 `mi_option_get_clamp(..., 1, 1000000L)` 把值钳位到 \([1,10^6]\)：0/-1 会被抬成 1（每次千次节拍都全量回收），\(10^6\) 以上的值会被压回 \(10^6\)。

**练习 3**：为什么 mini 拍要顺手调 `_mi_theap_collect_retired`，而不是等全量拍一起做？

**答案**：退役页（`used==0` 但还挂在队列里的页）占着 arena slice 与虚拟内存。mini 拍每 1000 次 generic 分配就来一次，让 `retire_expire`（小对象页 16 个周期、大对象页 4 个周期，[src/page.c:445](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L445)）以秒级而非分钟级的节奏推进，空页能及时归还（配合 eager purging 降低 RSS）；攒到全量拍才处理会让「完全空转」的 size class 长期占住内存。

### 4.4 force 语义与有界回收：mi_collect、线程退出与三层「延迟」的辨析

#### 4.4.1 概念说明

回调签名里的 `force` 参数划分了两种回收模式：

- `force == false`：**常态**。运行时应做**有界**的一批——处理最多 N 个积压 decref 就返回，把停顿压在常数界内。这正是心跳存在的意义。
- `force == true`：**清算**。运行时应把积压队列**清空**。触发时机是进程主动 `mi_collect(true)` 或线程退出（abandon 路径）——这些时刻不再在乎单次停顿，只求把账结清。

`force=true` 的两个来源：公共 API `mi_collect(true)` / `mi_heap_collect(true)`；线程终止时 `_mi_theap_collect_abandon`（经由 `mi_theap_collect_ex(theap, MI_ABANDON)`，`MI_ABANDON >= MI_FORCE`）。

最后必须辨析一个高频混淆点——**「deferred free 不减 used」到底指哪一层**。三层语义各不相同：

| 层次 | 机制 | `used` 何时变 | 块何时可再分配 |
| --- | --- | --- | --- |
| 运行时层（本讲主题） | `mi_register_deferred_free` 回调 | 与 mimalloc 的 `used` 无关 | 不涉及（管的是运行时自己的对象） |
| 页内延迟 | 本线程 free → `local_free`（[free.c:44-48](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L44-L48)） | **立即减** | 延迟到 `free` 耗尽被收割后 |
| 跨线程 free | 一次 CAS → `xthread_free`（u5-l2） | **不减**，收割时成批减（[page.c:182](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L182)） | 同上，且须经属主收割 |

所以「计数不减 `used`」精确地说**只描述跨线程 free 路径**；本线程 free 是「减了计数但延迟可用」；而 `mi_register_deferred_free` 延迟的是**宿主运行时的清理工作**，跟页计数完全是两码事。

#### 4.4.2 核心流程

```text
force 的传播路径
  A. mi_collect(true) / mi_heap_collect(true)          [公共 API]
  B. 线程退出 → _mi_thread_done → _mi_theap_collect_abandon → collect=MI_ABANDON
       ↓
  mi_theap_collect_ex(theap, collect)
       force = (collect >= MI_FORCE)                    ← MI_FORCE=1, MI_ABANDON=2
       ├─ _mi_deferred_free(theap, force)               ← 回调在此拿到 force
       ├─ _mi_theap_collect_retired(theap, force)
       ├─ 遍历所有页：_mi_page_free_collect(page, force) ← force 时两链拼接、清空 local_free
       │    └─ 全空页直接 _mi_page_free；ABANDON 时仍有存活块则 _mi_page_abandon
       ├─ _mi_arenas_collect(force, …)
       └─ 合并统计
```

对照 4.3：慢路径节拍里的 `mi_theap_collect(theap, false)` 走的是 `MI_NORMAL`，`force` 为 false——所以日常心跳回调见到的 `force` 几乎总是 0，`force=1` 只出现在上述清算时刻。

#### 4.4.3 源码精读

`mi_theap_collect_ex` 是全量回收的主干（[src/theap.c:123-148](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L123-L148)）：

```c
static void mi_theap_collect_ex(mi_theap_t* theap, mi_collect_t collect)
{
  if (theap==NULL || !mi_theap_is_initialized(theap)) return;
  ...
  const bool force = (collect >= MI_FORCE);
  _mi_deferred_free(theap, force);
  ...
  // collect all pages owned by this thread
  mi_theap_visit_pages(theap, &mi_theap_page_collect, (collect!=MI_NORMAL), &collect, NULL);
  ...
}
```

`_mi_deferred_free` 排在**第一行**——先把回调机会交给运行时，让它在页被遍历、块被归还之前处理完自己的账（例如运行时可能还要再分配）。枚举与访问函数（[src/theap.c:90-115](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L90-L115)）：

```c
typedef enum mi_collect_e {
  MI_NORMAL,
  MI_FORCE,
  MI_ABANDON
} mi_collect_t;

static bool mi_theap_page_collect(...) {
  mi_collect_t collect = *((mi_collect_t*)arg_collect);
  _mi_page_free_collect(page, collect >= MI_FORCE);
  if (mi_page_all_free(page)) { ... _mi_page_free(page, pq); }
  else if (collect == MI_ABANDON) { _mi_page_abandon(page, pq); }
  return true;
}
```

公共包装（[src/theap.c:154-166](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L154-L166)）：

```c
void mi_theap_collect(mi_theap_t* theap, bool force) mi_attr_noexcept {
  mi_theap_collect_ex(theap, (force ? MI_FORCE : MI_NORMAL));
}

void mi_collect(bool force) mi_attr_noexcept {
  // cannot really collect process wide, just a theap..
  mi_theap_collect(_mi_theap_default(), force);
}
```

注意 `mi_collect` 的注释：它实际只 collect **当前线程的默认 theap**，不是全进程——「force 清算」的边界也是线程级的。线程退出走 abandon 路径（[src/theap.c:150-152](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L150-L152)，由 [src/theap.c:489](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L489) 与 [src/init.c:387](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L387) 调用）：

```c
void _mi_theap_collect_abandon(mi_theap_t* theap) {
  mi_theap_collect_ex(theap, MI_ABANDON);
}
```

`force` 对页内链表的影响体现在 `_mi_page_free_collect` 的 `force` 分支（[src/page.c:228-239](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L228-L239)）：常态下 `free` 非空就**不搬** `local_free`（保持节拍），force 时才走线性拼接把两条链彻底合并清空——「有界 vs 清算」在页级别的一一对应。

#### 4.4.4 代码实践

**实践目标**：观察 `force` 的两种触发方式，并验证「回调先于页遍历执行」的顺序。

**操作步骤**：

1. 在 4.3 的 `rc_safepoint` 里加一个计数器区分 force 与非 force 拍：

```c
/* 示例代码片段 */
static unsigned long long g_force_ticks = 0;
static void rc_safepoint(bool force, unsigned long long heartbeat, void* arg) {
  ...
  if (force) { g_force_ticks++; printf(">>> FORCE tick #%llu (hb=%llu)\n", g_ticks, heartbeat); }
}
```

2. 在 `main` 末尾（return 前）加 `mi_collect(true);`，重新编译运行。
3. 进阶：写一个子线程 `pthread_create` 后在回调里 `mi_thread_done()` 前后打点（或简单地在子线程里 `return`，让 TLS 析构触发 abandon 路径），观察子线程退出时是否出现 `force=1` 的拍。

**需要观察的现象**：程序收尾出现至少一条 `FORCE tick`；普通循环期间的拍全部 `force=0`；子线程退出前后各有一条 force 拍（分属两个 theap 的心跳）。

**预期结果**：force 拍数量极少（个位数），非 force 拍占绝对多数——印证 4.4.1 的「常态有界、偶发清算」。子线程场景的具体输出**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`mi_collect(true)` 能不能回收**其他线程**的延迟块？

**答案**：不能。`mi_collect` 只作用于当前线程的默认 theap（[theap.c:158-161](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L158-L161) 注释写明 "cannot really collect process wide, just a theap"）。其他线程的 `local_free` 只有该线程自己进入慢路径节拍或退出时才会被收集；跨线程 free 的块更是只能由属主收割。

**练习 2**：运行时在 `force=false` 的回调里「少处理一点」是安全的吗？会不会丢块？

**答案**：安全且这正是设计意图。回调只是通知，mimalloc 不依赖回调完成任何内存回收——页的 `local_free`、`thread_free` 迁移与空页归还由节拍器和 collect 流程自行推进（4.4.3）。运行时积压的是**它自己的** decref 队列，少处理只是让对象晚一点真正死亡，下拍继续即可；`force=true` 时才需要清空。

**练习 3**：为什么 `_mi_deferred_free` 要放在 `mi_theap_collect_ex` 的第一行，而不是所有页收集完之后？

**答案**：回调里运行时可能还要分配内存（打印、临时对象）。先回调再遍历页，运行时在回调里的分配行为会被本次 collect 的后续步骤「看见」并妥善处理；若放在最后，回调中的分配会落在一个已收集完的状态上，错过本次清算，还得等下一个周期。此外回调中若触发慢路径，`recurse` 保护也能让它安静地跳过重入。

## 5. 综合实践

**任务：实现一个「有界 decref 消费者」并绘制心跳节奏曲线。**

结合本讲全部内容，把 4.3 的示例扩展成一个完整的模拟引用计数运行时：

1. **积压队列**：主循环里每次 `mi_free(p)` 前，把 `p` 对应的「模拟对象」（含一个 `deferred` 标志）压入一个定长环形缓冲 `pending[]`（容量 4096，**示例代码**，自实现）。
2. **有界消费者**：回调 `force=false` 时每拍最多处理 512 个积压 decref（弹出、计数、真正 `mi_free`）；`force=true` 时清空整个环形缓冲。
3. **打点**：每拍记录 `(tick 序号, heartbeat, 距上拍的分配数, 本拍处理的 decref 数, 积压余量)` 到 CSV。
4. **三组配置**：默认、`MIMALLOC_GENERIC_COLLECT=1`、`=1000000`，各跑一次，用表格对比：总拍数、平均/最大「每拍分配数」、程序总耗时、结束时积压余量是否为 0（`mi_collect(true)` 之后应为 0）。
5. **分析**：用 4.2 的 \( |free| \) 上界推导解释「每拍分配数」的波动来源；指出哪一组配置对「要求停顿上界严格、允许回收稍滞后」的实时运行时最合适，并说明理由。

**验收要点**（数值**待本地验证**，但以下定性结论应当成立）：心跳严格单调且每拍 +1；force 拍只在收尾/线程退出出现；`generic_collect=1` 组总拍数最少而单拍最重；三组结束后积压均为 0。若某组结束积压非 0，检查是否漏掉了 `mi_collect(true)` 收尾或环形缓冲溢出丢弃了未处理项。

## 6. 本讲小结

- **心跳是 per-theap 的单调计数**：`_mi_deferred_free` 每被调用一次 `theap->heartbeat++`（在判空之前），每线程独立、无需原子；回调与参数通过两个进程级静态原子指针以「先 arg 后 fn」的 release/acquire 顺序发布。
- **触发节奏是两级节拍器**：每 1000 次 generic 分配做一次管理（mini 拍：心跳 + 退役页回收）；累计达到 `mi_option_generic_collect`（默认 10000，钳位 \([1,10^6]\)）升级为全量 collect。心跳步长恒为 1，选项调节的是每拍的工作量。
- **确定性来自 `local_free` 的分离**：本线程释放的块扣在 `local_free` 里不可再分配，`free` 的补给只发生在「耗尽后的收割」这一确定时机，因此两拍之间的分配次数有硬上界——这是运行时敢承诺有界回收的根基。
- **`force` 划分常态与清算**：`force=false` 时运行时做有界批量；`force=true` 只出现在 `mi_collect(true)` 与线程退出（abandon）时刻，且 `mi_collect` 实际只作用于当前线程的默认 theap。
- **三层「延迟/计数」不可混为一谈**：回调延迟的是宿主运行时自己的清理；页内 `local_free` 是「减了 `used` 但延迟可用」；只有跨线程 free 才是「不减 `used`、等收割成批修正」。
- **递归保护**：`tld->recurse` 使回调内的分配不会重入回调，心跳计数不受影响。

## 7. 下一步学习建议

本讲结束了单元五（释放路径）的全部内容。接下来建议：

1. **进入单元六**，从 `u6-l1`（prim OS 抽象层）开始自底向上看内存的最终来源；本讲多次出现的「退役页归还、arena 收集」将在 u6-l3/u6-l4 展开真正的去向。
2. **回读源码**：把 [src/page.c:1011-1042](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L1011-L1042)（节拍器）与 [src/theap.c:123-148](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L123-L148)（collect 主干）对照重读一遍，画出完整的「分配 → 节拍 → 回调 → 收割 → 归还」时序图。
3. **为 u8 做准备**：本讲出现的 acquire/release 序、`recurse` 防重入、无锁发布等模式，将在 u8-l1（原子抽象与 mi_lock）中系统化；届时回来验证你对注册顺序正确性的直觉。
