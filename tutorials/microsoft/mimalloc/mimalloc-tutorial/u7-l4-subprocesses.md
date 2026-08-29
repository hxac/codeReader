# 子进程（subproc）：为 CPython 多解释器设计的隔离

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `mi_subproc_t` 是什么：mimalloc v3 把原本散落在进程里的「分配器全局状态」（arena 数组、主堆、统计账本）装进一个结构体，从而支持在一个 OS 进程内创建多个**互相隔离的分配域**。
2. 解释隔离的确切含义：subproc 之间 arena 完全分离、abandoned 页互不认领、统计各自记账。
3. 掌握 `mi_subproc_add_current_thread` 的调用时机约束——**线程创建后、第一次分配前**——以及违反约束的后果。
4. 描述 `mi_subproc_new` / `mi_subproc_destroy` 的完整生命周期，理解销毁时「堆 → 统计 → arena → 自身」的回收边界。

## 2. 前置知识

### 2.1 为什么需要 subproc：CPython 多解释器的诉求

传统分配器把 arena 列表、主堆、统计计数写成**文件级静态变量**——一个进程只有一份。但 CPython 从 3.12 起推进「每个解释器一个 GIL」（PEP 684），允许多个 Python 解释器共存于一个进程。解释器之间要的是三件事：

1. **内存不串门**：解释器 A 释放的内存块，绝不能被解释器 B 复用——否则一个解释器的指针错误会污染另一个解释器，安全审计无法收口。
2. **整体销毁**：销毁一个解释器时，它的全部内存一次性回收，不必逐对象 `free`。
3. **独立账本**：每个解释器能独立统计自己的内存占用，互不干扰。

mimalloc v3 的回答是：把这些静态变量收拢成一个 `mi_subproc_t`，主 subproc 静态预分配，其他 subproc 动态创建。官方 API 头文件里一句话点题：

> Advanced: allow sub-processes whose memory arena's stay fully separated (and no reclamation between them). Used for example for separate interpreters in one process.

见 [include/mimalloc.h:L349-L353](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L349-L353)。

### 2.2 前置讲义回顾（本讲直接承接）

- **u3-l1（层级模型）**：所有权链 subproc → heap → theap → page queue → page → block。本讲补上最顶层 subproc 的内部构造。
- **u7-l1（初始化）**：主堆、`mi_tld_detached`、`theap_meta` 等静态自举对象。本讲会看到这些静态对象全部挂在**主 subproc**名下。
- **u7-l2（TLS）**：线程经 `_mi_theap_default()` 拿到默认 theap；本讲的关键链路「theap → tld → subproc」正是建立在这之上。
- **u7-l3（一等堆）**：heap 是跨线程身份、theap 是线程私有执行单元。subproc 是它们的再上一层容器：**每个 subproc 拥有自己的 heap 集合与 arena 集合**。
- **u6-l3（arena）**：arena 是从 OS 批发的 64KiB slice 零售区。本讲的隔离本质就是「arena 按 subproc 私有」。

### 2.3 一条贯穿本讲的隔离不变式

对任何一个 mimalloc 页 `p`，沿所有权链上行会得到**同一个** subproc：

\[ \text{subproc}(p) \;=\; \text{arena}(p)\to\text{subproc} \;=\; \text{heap}(p)\to\text{subproc} \;=\; \text{tld}\to\text{subproc} \]

arena 分配/释放路径里的断言 `mi_assert_internal(arena->subproc == mi_page_subproc(page))`（[src/arena.c:L1258](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1258)）检查的正是这条不变式。subproc 的「不认领别人的页」则由登记位置保证：abandoned 页只登记在**本 subproc 的 arena** 的位图里，别的 subproc 的慢路径根本扫不到它。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/subproc.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/subproc.c) | 本讲主战场：subproc 的创建、销毁、线程加入、元数据分配（`_mi_meta_zalloc`） |
| [include/mimalloc/types.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h) | `mi_subproc_s` 结构体定义（L642-L680），以及 heap/tld/arena 各自指向 subproc 的字段 |
| [include/mimalloc.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h) | subproc 公共 API：`mi_subproc_new/destroy/current/main/add_current_thread/visit_heaps`（L349-L363） |
| [src/init.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c) | 主 subproc 的静态自举（`mi_heap_main_init_once`）、`_mi_thread_init_with_heap`、进程退出的销毁入口 |
| [src/heap.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/heap.c) | `_mi_heap_new_for_subproc`（给 subproc 造主堆）与 `_mi_heap_force_destroy` |
| [src/arena.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c) | `_mi_arenas_unsafe_destroy_all`：销毁 subproc 时整段归还其 arena |
| [test/test-stress-subprocs.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-stress-subprocs.c) + [test/test-stress.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-stress.c) | 官方 subproc 压测：多 subproc × 多线程反复分配/释放/销毁 |

## 4. 核心概念与源码讲解

### 4.1 mi_subproc_t 数据模型：把进程级静态变量装进一个结构体

#### 4.1.1 概念说明

回顾 u6-l3：arena 列表曾经是进程级全局；回顾 u7-l1：主堆与元数据堆曾是静态对象。v3 把这些「一个进程一份」的东西全部塞进 `mi_subproc_t`：

- **arena 集合**：`arenas[MI_MAX_ARENAS]` + `arena_count`——本 subproc 的内存批发来源，别的 subproc 看不见。
- **堆集合**：`heap_main`（主堆）+ `heaps` 双向链表 + `heaps_lock`——本 subproc 的所有一等堆。
- **元数据堆**：`theap_meta` + `theap_meta_lock`——一个挂在 detached tld 上的专用 theap，用来分配 tld、theap、subproc 结构体自身这类元数据。
- **统计账本**：`stats`——arena/OS 层统计（committed、arena 数等）记在这里，堆销毁时堆级统计也并到这里。
- **亲缘关系**：`parent`（在哪个 subproc 里创建的我）+ `next/prev`（全局 subproc 链表）+ `subproc_seq`（全局递增序号）。

主 subproc 是**静态预分配**的（不分配内存的内存管理器才能自举，见 u7-l1 的「静态对象群」原则）：

```c
// pre-allocate the main subprocess structure.
static mi_decl_cache_align mi_subproc_t mi_process_subproc_main = mi_init_struct_zero;
static mi_subproc_t* mi_subprocs = NULL;
static mi_lock_t     mi_subprocs_lock = MI_LOCK_INITIALIZER;
```

见 [src/subproc.c:L12-L15](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/subproc.c#L12-L15)。`mi_subprocs` 是进程内全部 subproc 的链表头（main 也在链上），用一把自旋锁保护。

一个常见的直觉误区要在这里纠正：**subproc 不是 OS 进程，也不对应线程组**。它是纯粹的分配器概念——一组私有的 arena + 堆 + 账本。线程只是「登记」到某个 subproc 名下（经 `tld->subproc`），OS 层面毫无感知。

#### 4.1.2 核心流程

**「当前 subproc」怎么找？** 分配路径里到处需要知道「我现在在哪个 subproc」（比如给页找 arena）。链路只有三跳，全部顺着 u7-l2 讲过的 TLS 快路径：

```text
mi_malloc(...)
  → _mi_theap_default()      // TLS 一跳拿到本线程默认 theap（u7-l2）
  → theap->tld               // theap 持有本线程 tld（未初始化时指向静态 mi_tld_detached）
  → tld->subproc             // tld 创建时被赋值，终身不变
```

若 theap 尚未初始化（`tld == NULL`，见 issue #1289 的边界），兜底返回主 subproc——所以**任何线程在第一次分配之前都属于主 subproc**。

**结构体字段速查**（对照 4.1.3 的源码）：

| 字段组 | 字段 | 作用 |
|---|---|---|
| 链表/身份 | `subproc_seq`, `next`, `prev`, `parent`, `memid` | 全局链表、创建者、自身内存产地证（u3-l1 的 mi_memid_t） |
| arena | `arena_count`, `arenas[]`, `arena_reserve_lock`, `purge_expire` | 私有 arena 数组、一次性保留锁、purge 到期时间戳（u6-l2） |
| 堆 | `heap_main`, `heaps`, `heaps_lock`, `heap_count` | 主堆与堆链表 |
| 元数据 | `theap_meta`, `theap_meta_lock`, `meta_pages` | detached 元数据堆及其锁 |
| 线程 | `thread_count`, `thread_total_count` | 当前/累计登记线程数 |
| 账本 | `stats` | subproc 级统计 |

#### 4.1.3 源码精读

**结构体本体**，注释直接写明了设计意图（"Sub processes do not reclaim or visit pages from other sub processes... can be used for example by CPython to have separate interpreters within one process. Each thread can only belong to one subprocess"）：

[include/mimalloc/types.h:L642-L680](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L642-L680) 定义了 `struct mi_subproc_s`。注意三点：`arenas` 是 `_Atomic` 数组（arena 保留可与分配并发）；`stats` 字段的注释说明它「更新 arena/OS 层统计，堆级统计在堆销毁时并入」；`parent` 记录创建者 subproc，销毁时要把结构体还给它。

**下层结构对 subproc 的反向指针**——隔离不变式的物证：

- heap：`mi_heap_s` 的第一个字段就是 `mi_subproc_t* subproc;`（[include/mimalloc/types.h:L618-L624](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L618-L624)）。
- tld：`mi_tld_s` 有 `mi_subproc_t* subproc;`（[include/mimalloc/types.h:L691-L699](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L691-L699)），`mi_tld_init` 里赋值后终身不变（[src/init.c:L236-L251](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L236-L251)）。
- arena：`mi_arena_s` 的 `mi_subproc_t* subproc;` 注释写作 "`this 'element-of' this->subproc->arenas`"（[include/mimalloc/types.h:L730-L737](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L730-L737)）。
- theap：`_Atomic(mi_subproc_t*) subproc;`，原子缓存的原因见注释「always `subproc == heap->subproc` but needed for safe destruction」（[include/mimalloc/types.h:L563-L572](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L563-L572)）。

**当前 subproc 的推导与 id 转换**：

[src/subproc.c:L103-L133](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/subproc.c#L103-L133) 是一整组小函数：`_mi_subproc()` 沿 theap→tld→subproc 三跳取当前 subproc（未初始化兜底主 subproc）；`mi_subproc_main()` / `mi_subproc_current()` 是公共 API 包装。公共类型 `mi_subproc_id_t` 只是一个装着 `void*` 的不透明壳（[include/mimalloc.h:L355-L363](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L355-L363)），`_mi_subproc_from_id/_mi_subproc_to_id` 在壳与真指针之间互转——不透明是为了把内部结构体藏在 `types.h` 里不进公共头。

**元数据分配 `_mi_meta_zalloc`**：

[src/subproc.c:L18-L37](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/subproc.c#L18-L37)。为什么要专门一套？因为分配 tld/theap/subproc 这些「分配器自身的骨头」时，线程可能还没初始化、甚至进程还没初始化。`theap_meta` 挂在 detached tld 上（不属于任何线程），所以**任何线程任何时候**都能在它上面分配——代价是必须持锁（`theap_meta_lock`），把并发分配串行化。u7-l1 讲过的静态元数据堆 `mi_process_theap_meta` 正是主 subproc 的 `theap_meta`。

顺带一提：持有 meta 锁时不能调 `mi_free`（会经 `mi_stat_free` 再次抢锁，issue #1358），所以 `_mi_meta_rezalloc` 手工「分配→拷贝→释放」，见 [src/subproc.c:L49-L71](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/subproc.c#L49-L71) 的注释。

#### 4.1.4 代码实践：亲眼看见「主线程在主 subproc」

**实践目标**：验证 `mi_subproc_current()` 的推导链，确认主线程属于主 subproc、且 `mi_subproc_id_t` 的不透明壳里装的指针可以打印对比。

**操作步骤**（示例代码）：

```c
// subproc-peek.c —— 示例代码
#include <stdio.h>
#include <mimalloc.h>

int main(void) {
  mi_thread_init();                        // 主动初始化本线程（否则首次分配也会触发）
  mi_subproc_id_t main_id = mi_subproc_main();
  mi_subproc_id_t cur_id   = mi_subproc_current();
  printf("main subproc : %p\n", main_id._mi_subproc_id);
  printf("current      : %p\n", cur_id._mi_subproc_id);
  printf("same?        : %s\n",
         (main_id._mi_subproc_id == cur_id._mi_subproc_id) ? "yes" : "no");
  return 0;
}
```

编译运行（静态链 release 库，库名规则见 u1-l2）：

```bash
gcc -O2 -Iinclude subproc-peek.c out/release/libmimalloc.a -lpthread -o subproc-peek
./subproc-peek
```

**需要观察的现象**：两行指针相同，`same?` 为 `yes`。

**预期结果**：主线程的 tld 是在 `mi_process_init` 时以主 subproc 创建的（[src/init.c:L184-L193](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L184-L193)），所以当前 subproc 就是 `mi_process_subproc_main` 的地址。若删掉 `mi_thread_init()` 一行再跑，首次分配会在 `printf` 内部触发同样的初始化，结果不变。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_mi_subproc()` 在 theap 未初始化时要兜底返回主 subproc，而不是返回 NULL？

**答案**：分配路径（如给页找 arena、记统计）在任何时刻都可能被调用，包括线程第一次分配的自举瞬间；此时 theap 指向静态空 theap、`tld` 为 NULL（见 issue #1289 的崩溃场景，[src/subproc.c:L103-L111](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/subproc.c#L103-L111)）。返回主 subproc 让自举期的临时分配自然落进主 subproc 的元数据堆，调用方无需特判。

**练习 2**：`mi_subproc_id_t` 为什么设计成 `struct { void* _mi_subproc_id; }` 而不是直接 `typedef void*`？

**答案**：不透明结构体可以阻止用户直接解引用或做指针运算，编译器能检查类型混用；同时 `mi_subproc_t` 的真身定义在内部头 `types.h`，公共头 `mimalloc.h` 无需暴露内部结构（[include/mimalloc.h:L355](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L355)）。

**练习 3**：跨 subproc 的 `mi_free` 能工作吗？比如主线程 free 一个子 subproc 里分配的指针。

**答案**：能。释放走 u3-l4 的 page map 从指针反查页，而 page map 是**全局共享**的（只有主 subproc 销毁时才会拆掉它，[src/subproc.c:L253-L256](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/subproc.c#L253-L256)）。free 后 slice 归还到**属主 subproc** 的 arena 位图——内存不串门，但释放动作本身不受 subproc 边界限制。

---

### 4.2 创建与销毁：mi_subproc_new 与 mi_subproc_destroy 的生命周期

#### 4.2.1 概念说明

**创建**的核心难点仍是自举：新 subproc 的结构体、它的元数据堆、它的主堆，这些内存从哪来？答案体现「亲子关系」设计——**孩子出生于母亲的空间**：

- `mi_subproc_t` 结构体本身 → 从**父 subproc**（即调用者当前所在 subproc）的 `theap_meta` 分配；
- 新 subproc 的 `theap_meta` 结构体 → 同样从父的 `theap_meta` 分配；
- 新 subproc 的主堆 `heap_main` 结构体 → 从**父的主堆**分配（[src/heap.c:L128-L134](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/heap.c#L128-L134)）。

而新 subproc 的**用户内存**（arena、页、块）则完全来自它自己的 arena——出生在母亲身边，长大后自立门户。

**销毁**是 subproc 的招牌能力：一次 `mi_subproc_destroy` 等价于「把这个解释器的一切连锅端」。回收边界按顺序是：

1. 全局链表摘牌；
2. 逐个强销毁堆（页全部释放，堆级统计并入 subproc 账本）；
3. 非主 subproc 的账本并回主 subproc（**记账归并，不是内存泄漏**）；
4. 它的全部 arena 整段归还 OS（物理内存真的回去了，RSS 下降）；
5. subproc 结构体自身还给父 subproc。

**主 subproc 不可销毁**：`mi_subproc_destroy` 开头就拦截（[src/subproc.c:L259-L263](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/subproc.c#L259-L263)），它只能随进程退出，在 `mi_option_destroy_on_exit` 开启时由 `_mi_subprocs_unsafe_destroy_all` 收尾（[src/init.c:L625-L628](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L625-L628)），并且只有它会额外拆掉全局 page map。

#### 4.2.2 核心流程

**mi_subproc_new 五步曲**：

```text
mi_subproc_new()
  ├─ 0. mi_thread_init()           // 保证本线程已初始化，_mi_subproc() 才能取到 parent
  ├─ 1. parent = _mi_subproc()     // 谁调用我，谁就是父 subproc
  ├─ 2. _mi_meta_zalloc(parent, sizeof(mi_subproc_t))      // 结构体生于父的元堆
  ├─ 3. _mi_meta_zalloc(parent, sizeof(mi_theap_t))        // 元堆的 theap 也生于父的元堆
  ├─ 4. mi_subproc_init(subproc, parent)                   // 初始化锁/账本，挂全局链表
  ├─ 5. _mi_heap_new_for_subproc(subproc, 0, /*is_main=*/true)  // 主堆结构体生于父的主堆
  └─ 6. _mi_theap_init(theap_meta, heap_main, parent->theap_meta->tld /*detached*/)
       └─ 返回 _mi_subproc_to_id(subproc)
```

任一步失败都会把已分配的部分逐个 `_mi_meta_free` 回滚。

**mi_subproc_unsafe_destroy 七步曲**（顺序即语义）：

```text
mi_subproc_unsafe_destroy(subproc)
  ├─ 1. 从 mi_subprocs 双向链表摘除
  ├─ 2. heaps_lock 下：逐个 _mi_heap_force_destroy 所有非主堆（页全释放）
  ├─ 3. _mi_thread_locals_thread_done() 释放主堆的动态 TLS 槽
  │     （若销毁的是主 subproc：再 _mi_thread_locals_done()）
  ├─ 4. _mi_heap_force_destroy(heap_main)
  ├─ 5. theap_meta = NULL            // 它的统计已在第 4 步并入
  ├─ 6. 非主 subproc：_mi_stats_merge_into(主 subproc.stats, subproc.stats)
  ├─ 7. _mi_arenas_unsafe_destroy_all(subproc)   // arena 整段 _mi_os_free_ex 归还 OS
  ├─ 8. 三把锁 lock_done；_mi_meta_free(parent, subproc, memid)  // 结构体还给父
  └─ 9. 仅主 subproc：打印统计 + _mi_page_map_unsafe_destroy()
```

注意第 6、7 步的先后：**先并账、后还地**。所以销毁完成后，主 subproc 统计里的累计字段（total 类）会包含被销毁 subproc 的历史账目——这是刻意设计（进程总账不丢），不要误读为泄漏。

#### 4.2.3 源码精读

**`mi_subproc_new`**：[src/subproc.c:L158-L194](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/subproc.c#L158-L194)。三个细节值得盯：

- 第一行 `mi_thread_init()`（L159）——先保证当前线程就绪，`_mi_subproc()`（L160）才能取到正确的父。
- 主堆创建调用 `_mi_heap_new_for_subproc(subproc, 0, true)`（L178），`is_main_heap=true` 分支里堆结构体从 `subproc->parent->heap_main` 分配（[src/heap.c:L128-L148](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/heap.c#L128-L148)），并给主堆保留快速 TLS 槽 `mi_thread_local_key_fast`。
- 新 `theap_meta` 用**父的 detached tld** 初始化（L190，断言父的 `theap_meta->tld->thread_id == MI_THREADID_DETACHED`）——元数据堆不属于任何线程，这正是它能服务任意线程的原因。

**`mi_subproc_init`**：[src/subproc.c:L141-L156](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/subproc.c#L141-L156)。初始化三把锁（arena 保留、堆链表、元堆）+ 账本，然后头插进全局 `mi_subprocs` 链表。`subproc_seq` 来自函数内 `static _Atomic(size_t) subproc_total_count`，全局单调递增，统计报表里的 `subproc N` 编号就是它。

**销毁主体**：[src/subproc.c:L202-L257](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/subproc.c#L202-L257)。

- L214-L229：遍历 `subproc->heaps`，非主堆逐个 `_mi_heap_force_destroy`。这个函数（[src/heap.c:L241-L252](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/heap.c#L241-L252)）释放该堆全部 theaps 并销毁全部页——正是 u7-l3 讲的「整堆一次性回收」，subproc 销毁把它推到极致：**所有堆一起收**。
- L223：销毁主堆前先 `_mi_thread_locals_thread_done()`——回收这个 subproc 的动态 TLS 槽位（u7-l2 的自制动态 TLS），注释说明主堆用 fast key 所以安全。
- L235：`_mi_stats_merge_into(&mi_process_subproc_main.stats, &subproc->stats)`——账本并回主 subproc。`_mi_stats_merge_into` 的定义在 [src/stats.c:L446-L451](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L446-L451)：相加后把源清零，避免二次并账。
- L239：`_mi_arenas_unsafe_destroy_all(subproc)`（[src/arena.c:L1540-L1566](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1540-L1566)）：遍历 `subproc->arenas[]`，OS 产地的 arena 整段 `_mi_os_free_ex` 归还，最后 CAS 把 `arena_count` 清零。
- L252：`_mi_meta_free(subproc->parent, subproc, subproc->memid)`——结构体还给**父** subproc 的元堆，亲子关系闭环。

**主 subproc 的静态自举**：[src/subproc.c:L316-L322](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/subproc.c#L316-L322) 的 `_mi_subproc_main_init` 给静态对象配好 memid（MI_MEM_STATIC）并挂链。它的调用点在 u7-l1 讲过的 `mi_heap_main_init_once` 里（[src/init.c:L184-L208](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L184-L208)）：主 subproc → detached tld → 主堆 → `mi_process_theap_meta`（`allow_page_abandon = false` 防止元数据页与其他线程共享）依次就位。对照 `mi_subproc_new` 会发现两者步骤同构——静态版只是把「从父分配」换成「预分配好的静态对象」。

#### 4.2.4 代码实践：源码阅读型——画销毁顺序图

**实践目标**：通过精读 `mi_subproc_unsafe_destroy`，验证你对回收边界的理解，并回答一个反直觉的问题。

**操作步骤**：

1. 打开 [src/subproc.c:L202-L257](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/subproc.c#L202-L257)，把 4.2.2 的七步伪代码与真实代码逐行对上号。
2. 追一步 `_mi_heap_force_destroy`（[src/heap.c:L241-L252](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/heap.c#L241-L252)），确认「销毁堆」最终走到页释放（u6-l4 的 `_mi_arenas_free`：清本 subproc arena 的 slice 位图）。
3. 回答：**第 6 步并账在第 7 步归还 arena 之前，为什么这个顺序不会让主 subproc 的 `committed.current` 虚高？** 提示：第 2、4 步销毁堆时，页释放已把 committed 计数降回去了（[src/arena.c:L1274](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1274) 一带的 `mi_subproc_stat_decrease`）。

**需要观察的现象**：纸面推演，无需运行。

**预期结果**：并账时被销毁 subproc 的 `committed.current` 已经只剩 arena 级保留量，页级提交量在堆销毁阶段就已扣减；arena 整段归还时的最终扣减落在被销毁 subproc 自己的账本上（反正它即将消失）。实际输出的微小出入源于 arena 保留本身的记账，属正常。**待本地验证**（用第 5 节综合实践对比 destroy 前后的统计输出）。

#### 4.2.5 小练习与答案

**练习 1**：`mi_subproc_new` 在哪个 subproc 里分配新 subproc 的结构体？这带来什么约束？

**答案**：在**调用者当前所在** subproc（parent）的 `theap_meta` 里（[src/subproc.c:L160-L167](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/subproc.c#L160-L167)）。约束是：销毁时必须 `_mi_meta_free(subproc->parent, ...)` 还给父（L252）；且若 parent 先于 child 被销毁，child 的 memid 会悬垂——所以销毁顺序上应由使用者保证子先于父销毁（测试里也是先建后销的逆序，见 4.3.3）。

**练习 2**：为什么 `_mi_arenas_unsafe_destroy_all` 的注释强调 "unsafe"？安全的使用前提是什么？

**答案**：因为它无条件把 arena 从数组摘除并整段归还 OS（[src/arena.c:L1540-L1566](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1540-L1566)）。此时若还有任何线程在所属 subproc 里分配、或还有存活指针将被使用，就是 use-after-free。安全前提是：该 subproc 的所有线程都已退出（或不再分配）、所有堆都已销毁——这正是 `mi_subproc_unsafe_destroy` 把它放在最后一步的原因。

**练习 3**：对比 `mi_subproc_new` 与 `_mi_subproc_main_init`，说出一个相同点和一个不同点。

**答案**：相同点：两者都要「建 subproc 结构 + 建主堆 + 建 theap_meta」三件套，且 theap_meta 都挂 detached tld。不同点：主 subproc 三件套全部静态预分配、memid 为 MI_MEM_STATIC、parent 为 NULL；动态 subproc 三件套从父分配、销毁时需归还父（对比 [src/subproc.c:L158-L194](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/subproc.c#L158-L194) 与 [src/subproc.c:L316-L322](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/subproc.c#L316-L322)、[src/init.c:L184-L208](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L184-L208)）。

---

### 4.3 线程加入与隔离边界：mi_subproc_add_current_thread

#### 4.3.1 概念说明

subproc 建好了，怎么让内存真正「落到」它里面？答案是**线程登记**：一个线程的 `tld->subproc` 在 tld 创建那一刻定格、终身不变，此后该线程的全部分配都从该 subproc 的堆与 arena 取内存。`mi_subproc_add_current_thread` 就是「让**当前**线程的下一次分配落在指定 subproc」的登记函数。

由此推出本讲最关键的时机约束（公共 API 注释原文：「this should be called right after a thread is created (and no allocation has taken place yet)」，[include/mimalloc.h:L360](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L360)）：

> **必须在线程创建后、该线程发生任何 mimalloc 分配之前调用。**

原因分两层：

1. **登记不可逆**：线程一旦初始化（比如它自己调了 `mi_malloc`、`printf` 缓冲区分配、甚至某些平台 TLS 的惰性初始化），默认 theap 与 tld 已在某个 subproc（通常是主 subproc）建好，`tld->subproc` 已定格。此后再调用 add_current_thread 只会得到一条警告并**静默失败**。
2. **失败是静默的**：默认构建下这条警告根本不打印——`_mi_warning_message` 需要 `MIMALLOC_SHOW_ERRORS=1` 或 `MIMALLOC_VERBOSE=1` 才输出（[src/options.c:L540-L549](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L540-L549)），且默认最多打印 5 次。也就是说：**搞错时序不会崩，只会悄悄地把内存分到错误的 subproc**——这正是使用 subproc 时最需要警惕的坑。

隔离的三个具体表现（本讲学习目标 1 的展开）：

| 维度 | 隔离方式 | 源码依据 |
|---|---|---|
| arena | 每个 subproc 私有 `arenas[]`，页分配只搜自己的 | [include/mimalloc/types.h:L657-L659](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L657-L659) |
| abandoned 页 | 遗弃页登记在本 subproc 的 arena 位图，别家慢路径扫不到 | [include/mimalloc/types.h:L642-L648](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L642-L648) |
| 统计 | 各自记账；销毁时才并回主 subproc | [src/subproc.c:L233-L236](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/subproc.c#L233-L236) |

#### 4.3.2 核心流程

```text
mi_subproc_add_current_thread(subproc_id)
  ├─ subproc = from_id(subproc_id)；空 id / 无主堆 → 直接返回
  ├─ theap = _mi_theap_default()          // 本线程默认 theap（u7-l2）
  ├─ 分支 A：theap 已初始化
  │    ├─ tld->subproc != subproc → 警告 "already in another subprocess"，返回（失败）
  │    └─ tld->subproc == subproc → 什么都不做（重复登记幂等）
  └─ 分支 B：theap 未初始化（理想时序）
       └─ _mi_thread_init_with_heap(subproc->heap_main)
            ├─ mi_process_init()                     // 进程级自举（u7-l1）
            ├─ mi_tld_create(heap_main->subproc)     // 新 tld，tld->subproc 定格
            ├─ theap = _mi_theap_alloc(heap_main,tld) // 主堆在本线程的 theap
            ├─ _mi_theap_default_set(theap)           // 此后本线程分配全走这里
            └─ mi_subproc_stat_increase(subproc, threads, 1)
```

与 u7-l2 的普通线程初始化相比只有一处差别：`_mi_thread_init_with_heap` 的参数 `heap_main`。传 NULL（普通路径，`_mi_thread_init`）时取主 subproc 的主堆；传 `subproc->heap_main` 时，tld/theap 就挂在**目标 subproc** 的主堆上——u7-l3 讲过「任意线程首次在某个堆分配时惰性创建 theap」，这里正是复用该机制。

#### 4.3.3 源码精读

**登记函数**：[src/subproc.c:L284-L300](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/subproc.c#L284-L300)。

- L291 `mi_theap_is_initialized(theap)` 是唯一的分流判定：初始化过 → 只能警告或幂等返回；没初始化 → L299 `_mi_thread_init_with_heap(subproc->heap_main)` 完成登记。
- L292-294 的警告文本 `"unable to add thread to the subprocess as it was already in another subprocess (at %p)"` 把**实际所属** subproc 的地址打了出来，配合 `MIMALLOC_SHOW_ERRORS=1` 是排查时序错误的第一手段。

**线程初始化**：[src/init.c:L306-L361](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L306-L361) 的 `_mi_thread_init_with_heap`。L335 `mi_tld_create(heap_main->subproc)`——tld 的内存从**目标 subproc** 的元堆分配（[src/init.c:L254-L273](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L254-L273)），`mi_tld_init` 里 `tld->subproc = subproc` 定格；只有主 subproc 的第一个 tld 例外（用静态 `mi_process_tld_main`）。L358 递增该 subproc 的线程计数。

**官方测试的标准姿势**：测试壳只有三行配置（[test/test-stress-subprocs.c:L7-L10](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-stress-subprocs.c#L7-L10)）：

```c
#define TEST_STRESS_SUBPROCS 1
#define NSUBPROCS            2
#define NTHREADS             16
#include "test-stress.c"
```

真正看点在 [test/test-stress.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-stress.c)：

- [L358-L368](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-stress.c#L358-L368) `test_stress_subprocs`：先 `mi_subproc_new()` 建满 NSUBPROCS 个 subproc，然后每个 subproc 派一个「驱动线程」，全跑完后**逆序** `mi_subproc_destroy`。
- [L351-L356](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-stress.c#L351-L356) 驱动线程第一行就是 `mi_subproc_add_current_thread(subproc)`——注意它由**新线程自己**执行，而不是父线程代劳。
- [L485-L494](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-stress.c#L485-L494) 更通用的封装 `thread_entry`：`pthread_create` 的入口函数先按参数登记 subproc 再进入用户函数，是这个时序约束的工程化模板。
- [L544-L564](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-stress.c#L544-L564) `run_os_threads`（pthread 版）：注意 `threads`/`callbacks` 数组用主线程的 `custom_calloc` 分配——**创建线程的开销记在主 subproc，新线程的分配记在目标 subproc**，两边天然分账。
- [L94-L97](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-stress.c#L94-L97) 一个诚实的限制：`#error "cannot test rolling heaps with multiple subprocesses (for now)"`——滚动堆（MI_USE_HEAPS）与 subproc 目前互斥。

构建入口在主 CMakeLists：`MI_BUILD_TESTS` 默认 ON，会生成 `mimalloc-test-stress-subprocs` 目标（[CMakeLists.txt:L953-L972](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L953-L972)）。

#### 4.3.4 代码实践：最小 subproc 全生命周期示例

这是本讲的主实践，覆盖规格要求的全部环节：创建 subproc → 工作线程加入 → 在其堆里分配 → 销毁 → 统计对比。

**实践目标**：用统计证明两件事——(a) subproc 存活期间，主 subproc 的内存账目不含子 subproc 的分配（隔离）；(b) `mi_subproc_destroy` 后无需逐块 free，子域内存整体回收（销毁边界）。

**操作步骤**：

1. 编写以下程序（示例代码）：

```c
// subproc-demo.c —— 示例代码
#include <stdio.h>
#include <pthread.h>
#include <mimalloc.h>
#include <mimalloc-stats.h>

static mi_subproc_id_t g_subproc;

// 只读「本 subproc 独占账本」里的已提交内存（KiB）
static long committed_kib(mi_subproc_id_t sid) {
  mi_stats_t_decl(st);                       // 宏：定义并填好 size/version 头
  if (!mi_subproc_stats_get_exclusive(sid, &st)) return -1;
  return (long)(st.committed.current / 1024);
}

static void* worker(void* arg) {
  (void)arg;
  // 时序约束：这是本线程第一个 mimalloc 相关动作。
  // 在此之前不能 printf、不能 malloc——否则本线程已在主 subproc 定格，登记失败。
  mi_subproc_add_current_thread(g_subproc);

  // 存活 16MiB：4096 个 4KiB 小对象，全部落在子 subproc 的堆里
  char* keep[4096];
  for (int i = 0; i < 4096; i++) {
    keep[i] = (char*)mi_malloc(4096);
    if (keep[i] == NULL) return NULL;
    keep[i][0] = 1;
  }
  // 故意不 free：等 mi_subproc_destroy 整域回收
  return NULL;
}

int main(void) {
  mi_thread_init();                          // 主线程留在主 subproc

  long base = committed_kib(mi_subproc_main());
  printf("[before] main subproc committed: %ld KiB\n", base);

  g_subproc = mi_subproc_new();              // 1. 创建子 subproc
  if (g_subproc._mi_subproc_id == NULL) { fprintf(stderr, "subproc_new failed\n"); return 1; }

  pthread_t t;
  pthread_create(&t, NULL, worker, NULL);
  pthread_join(t, NULL);                     // 2. 工作线程加入并分配

  long mid = committed_kib(mi_subproc_main());
  long sub = committed_kib(g_subproc);
  printf("[during] main subproc committed: %ld KiB (base %ld)\n", mid, base);
  printf("[during] sub  subproc committed: %ld KiB\n", sub);

  mi_subproc_destroy(g_subproc);             // 3. 整域销毁（无需逐块 free）

  long after = committed_kib(mi_subproc_main());
  printf("[after ] main subproc committed: %ld KiB\n", after);
  return 0;
}
```

2. 编译运行（先按 u1-l2 构建 release 库；统计接口需 `mimalloc-stats.h`）：

```bash
gcc -O2 -Iinclude subproc-demo.c out/release/libmimalloc.a -lpthread -o subproc-demo
./subproc-demo
# 进程退出时的整体统计（可选）：
MIMALLOC_SHOW_STATS=1 ./subproc-demo
```

**需要观察的现象**：

- `[during]` 两行：子 subproc 的 committed 约 16MiB 上下（页与元数据开销略多），而主 subproc 的 committed 与 `base` 基本持平（只有本 demo 自身的微量增长）。
- `[after]` 行：销毁后主 subproc 的 committed 不包含那 16MiB——子域内存已整段归还 OS。

**预期结果**：

- **(a) 隔离成立**：worker 的 4096 次分配在子 subproc 的堆/arena 上记账，主 subproc 独占账目几乎不动。统计口径来自 [src/stats.c:L620-L624](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L620-L624)（`mi_subproc_stats_get_exclusive` 直接拷贝 `subproc->stats`，不含别家）。
- **(b) 销毁边界成立**：没有任何 `mi_free(keep[i])`，内存仍被完整回收——这是 `_mi_heap_force_destroy` + `_mi_arenas_unsafe_destroy_all` 的效果。
- 进程 RSS 的回落可用 `/usr/bin/time -v ./subproc-demo` 观察 `Maximum resident set size`，或对比 destroy 前后读 `/proc/self/status` 的 `VmRSS`。**待本地验证**（数值依平台与页提交策略而异）。

3. 附加实验（验证时序约束）：把 `worker` 的第一行改成先 `mi_free(mi_malloc(8));` 再 `mi_subproc_add_current_thread(g_subproc)`，用 `MIMALLOC_SHOW_ERRORS=1 ./subproc-demo` 重跑。

**预期结果**：stderr 出现 `mimalloc: warning: unable to add thread to the subprocess as it was already in another subprocess (at <主subproc地址>)`，且 `[during]` 的 sub subproc committed 只剩元数据量、16MiB 全部落到了主 subproc 账上——失败的登记是**静默降级**而非报错中断。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：CPython 若在解释器线程已运行大量分配之后才调用 `mi_subproc_add_current_thread`，会发生什么？如何被发现？

**答案**：登记失败。默认 theap 已初始化且 `tld->subproc` 是主 subproc，函数只发一条警告就返回（[src/subproc.c:L291-L296](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/subproc.c#L291-L296)），后续分配继续走原 subproc。默认连警告都不打印，需 `MIMALLOC_SHOW_ERRORS=1`（或 `MIMALLOC_VERBOSE=1`）且受警告次数上限约束（[src/options.c:L540-L549](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L540-L549)）。这正是 API 注释要求「线程创建后立即调用」的原因。

**练习 2**：`printf` 为什么也可能破坏时序？

**答案**：线程首次 `printf` 时 glibc 会为 stdout 惰性分配缓冲区，这次分配走的是被覆盖的/默认的 malloc，从而把本线程初始化进了主 subproc。所以 demo 里 worker 在登记前一行输出都没有；官方测试的 `thread_entry` 同样把登记放在任何用户代码之前（[test/test-stress.c:L485-L494](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-stress.c#L485-L494)）。

**练习 3**：一个线程能先后属于两个 subproc 吗？`mi_subproc_visit_heaps` 和线程归属有什么关系？

**答案**：不能。`tld->subproc` 在 tld 创建时定格、终身不变（[src/init.c:L236-L239](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L236-L239)）。但**堆**可以跨线程：`mi_subproc_visit_heaps`（[src/subproc.c:L303-L313](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/subproc.c#L303-L313)）在 `heaps_lock` 保护下遍历本 subproc 的堆链——遍历的是「哪些堆属于这个 subproc」，与「哪个线程此刻登记在此」无关。

---

## 5. 综合实践

跑一遍官方压测 `mimalloc-test-stress-subprocs`，把本讲三个模块串起来观察。

**任务**：

1. 构建（u1-l2 的方式，`MI_BUILD_TESTS` 默认开启）：

```bash
mkdir -p out/release && cd out/release
cmake ../.. -DCMAKE_BUILD_TYPE=Release
make mimalloc-test-stress-subprocs -j
```

2. 阅读测试入口 [test/test-stress.c:L358-L368](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-stress.c#L358-L368)，确认流程是「建 2 个 subproc → 每个驱动 16 线程做 ITER 轮分配/释放/线程重建 → 逆序销毁 subproc」。
3. 运行并观察两个统计视角：

```bash
MIMALLOC_SHOW_STATS=1 ./mimalloc-test-stress-subprocs 2 25 2   # 线程数/规模/轮次都可调小
```

   - 退出时打印的是**主 subproc** 的汇总报表（[src/subproc.c:L242-L246](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/subproc.c#L242-L246)）——思考：被销毁的两个子 subproc 的分配量为什么也出现在这里？（答案：`mi_subproc_unsafe_destroy` 第 6 步并账，[src/subproc.c:L233-L236](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/subproc.c#L233-L236)。）
   - 用 `mi_debug_show_arenas()` 的输出（debug 构建下测试每 10 轮自动调用，[test/test-stress.c:L312-L321](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-stress.c#L312-L321)）对照 u6-l3：两个子 subproc 各自保留了独立的 arena 区。
4. 把 NSUBPROCS 从 2 改成 4 重编（改 [test/test-stress-subprocs.c:L8](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-stress-subprocs.c#L8)，仅用于本地实验，勿提交），观察 arena 总保留量近似线性增长——每个 subproc 首次要页时都会惰性保留自己的 arena（[src/arena.c:L552-L558](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L552-L558) 在 `subproc->arena_reserve_lock` 下按 subproc 独立保留）。**待本地验证**。

**验收标准**：能不看讲义说出「一个块从分配到销毁经过的所有权链，以及链条上每一环属于哪个 subproc」。

## 6. 本讲小结

- `mi_subproc_t` 把 v1/v2 时代的进程级静态状态（arena 数组、主堆、theap_meta、统计）收进一个结构体：主 subproc 静态预分配自举，动态 subproc 的三件套（结构体、元堆、主堆）全部从**父 subproc** 分配，销毁时还给父。
- 隔离三件套：arena 按 subproc 私有（页分配只搜 `subproc->arenas[]`）、abandoned 页登记在本 subproc 的 arena 位图（别家不认领）、统计各自记账（销毁时才并回主 subproc）。
- 线程归属在 tld 创建那一刻定格：`tld->subproc` 终身不变，因此 `mi_subproc_add_current_thread` 必须**在线程创建后、任何分配之前**由新线程自己调用；违反时静默降级到原 subproc，只有开 `MIMALLOC_SHOW_ERRORS=1` 才能看到警告。
- 销毁顺序即回收边界：摘链 → 强销毁全部堆（页全放、账并入）→ 账本并回主 subproc → arena 整段归还 OS（RSS 真实回落）→ 结构体还给父；主 subproc 不可销毁，且只有它会拆全局 page map。
- page map 是全局共享的，所以跨 subproc 的 `mi_free` 合法——释放的自由没有边界，内存的归属有边界。

## 7. 下一步学习建议

本讲补完了 u3-l1 层级模型的最顶层，第七单元（初始化、线程与一等堆）到此结束。后续两个方向：

- **u9-l3（统计系统）**：本讲用了 `mi_subproc_stats_get_exclusive` 做隔离证明，下一讲系统讲 `mi_stats_t` 的 peak/total/current 语义、`mi_stats_get` 与 `mi_subproc_stats_get` 的口径差异、JSON 导出——你会更精确地理解「并账」对峰值统计的影响。
- **u9-l4（堆遍历）**：`mi_subproc_visit_heaps` 已经展示了按 subproc 遍历堆的入口，u9-l4 深入 `mi_heap_visit_blocks` 如何沿页队列与 free list 推断块存活状态，是 GC 集成（也是 CPython GC）的基础。

若想继续读源码，推荐从本讲向外扩两步：`src/arena.c` 中所有带 `subproc` 参数的函数（看 arena 操作如何全程携带归属），以及 `_mi_subprocs_unsafe_destroy_all` 在 `mi_process_done` 里的调用条件（[src/init.c:L619-L648](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L619-L648)，对照 u7-l1 的 `destroy_on_exit` 语义）。
