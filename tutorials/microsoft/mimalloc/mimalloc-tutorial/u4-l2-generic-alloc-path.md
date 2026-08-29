# 慢路径：mi_find_page、mi_page_fresh 与 free list 扩展

## 1. 本讲目标

上一讲（u4-l1）我们看到：不超过 1 KiB 的请求沿「TLS 取 theap → `pages_free_direct` 直查 → `mi_page_malloc_zero` 弹块」四层内联漏斗，约 7 条指令完成。但漏斗最深处有一个 `if (block == NULL)` 的落空分支——当前页的 `free` 链表空了。本讲就沿着这个分支往下走，学完后你应当能够：

1. 完整描述 `_mi_malloc_generic` 的补救流程：页队列搜索 → free list 扩展 → 申请新页 → 回收兜底。
2. 理解 `mi_page_extend_free` 为什么要「分批初始化」一个页的块，而不是一次性把整页串成 free list（答案核心：少触碰内存、少 commit、降 RSS）。
3. 说清 `generic_count` / `generic_collect_count` 两个计数器如何与选项 `mi_option_generic_collect` 联动，驱动周期性的管理任务。

## 2. 前置知识

本讲假设你已读过 u3-l2、u3-l3 与 u4-l1。用三句话唤醒记忆：

- **一个页三条链**：`mi_page_t` 里有 `free`（快路径弹出用）、`local_free`（本线程释放暂存）、`xthread_free`（跨线程释放暂存）。计数不变式为 \( \text{used} \le \text{capacity} \le \text{reserved} \)，且 `capacity = used + |free| + |local_free|`。
- **页队列与直查数组**：每个 theap 有按 bin 组织的页队列数组 `theap->pages[]`；同时有一个下标即字数的直查数组 `pages_free_direct`，让 ≤1 KiB 的分配一次访存定页。队列首变化时由 `mi_theap_queue_first_update` 成段刷新直查数组（见 u3-l3）。
- **reserve 与 commit**：虚拟内存先 reserve（保留地址段）再 commit（提交物理页），两步分离是 mimalloc 控制内存占用的基础（见 u1-l1、u6 单元会展开）。

本讲的新关键概念是 **capacity 与 reserved 的差**：一个 64 KiB 的 small 页「预留」了大约两千多个块的位置（`reserved`），但一开始只「初始化」了其中一小段（`capacity`）。`capacity < reserved` 的页称为**可扩展的（expandable）**——这正是 [include/mimalloc/internal.h:911-915](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L911-L915) 中 `mi_page_is_expandable` 判定的内容。慢路径的大量精妙设计都围绕「什么时候、扩展多少」展开。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
|---|---|
| [src/alloc.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c) | 分配入口与快路径漏斗；`mi_theap_malloc_generic` 与弹空分岔点都在这里 |
| [src/page.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c) | 慢路径核心：`_mi_malloc_generic`、页队列搜索、`mi_page_extend_free`、`mi_page_fresh`、管理节拍 |
| [src/page-queue.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-queue.c) | 页队列的双链表操作（push/remove/move_to_front），被 page.c include 进同一翻译单元 |
| [include/mimalloc/internal.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h) | 判定谓词 `mi_page_immediate_available` / `mi_page_is_expandable` / `mi_page_is_full` 等 |
| [src/options.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c) | `generic_collect`、`page_max_candidates`、`page_full_retain` 三个选项的默认值 |
| [src/theap.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c) | `mi_theap_collect`：full collect 到底收集了什么 |
| [src/stats.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c) | 统计输出中 `extended`、`searches` 等观察指标的定义处 |

提醒：page-queue.c 不能单独编译，它通过 `#define MI_IN_PAGE_C` 被 [src/page.c:24-26](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L24-L26) 包含——这是 u1-l3 讲过的「翻译单元合并」手法，让队列操作在 page.c 内完全内联。

## 4. 核心概念与源码讲解

### 4.1 慢路径入口：从弹空到 _mi_malloc_generic

#### 4.1.1 概念说明

「慢路径」并不是一个失败处理函数，而是分配器的**工程部**：快路径只管弹块，凡是需要「找页、扩页、建页、做管理」的活全在这里。触发它的情形有四类：

1. 当前页 `free` 链表弹空（最常见的触发）；
2. 请求超过 `MI_SMALL_SIZE_MAX`（1 KiB），直查数组管不到；
3. 线程尚未初始化（TLS 里还是静态空 theap，见 u3-l1）；
4. 要求超大对齐（`huge_alignment > 0`，此时走 huge 单例页，u4-l3 详讲）。

值得注意的设计：慢路径函数 `_mi_malloc_generic` 自己内部还藏着一段**小对象快速再试**。因为绝大多数进入慢路径的小请求，其实只是「当前那页暂时空了」，队列里马上能找到别的页——为这种情形走完整的兜底流程（初始化检查、管理任务、OOM 处理）太浪费。

#### 4.1.2 核心流程

```text
mi_page_malloc_zero（快路径末梢）
  └─ page->free == NULL？
       ├─ 否 → 弹块返回（u4-l1 的内容）
       └─ 是 → _mi_malloc_generic(theap, size, zero|huge_alignment)
              ├─ 【内嵌快试】theap 已初始化 && ++generic_count < 1000
              │    && huge_alignment == 0 && req_size < MI_SMALL_MAX_OBJ_SIZE？
              │    ├─ 是 → mi_page_queue_find_free(选中的页队列) → 直接弹块返回
              │    └─ 否 ↓
              └─ mi_malloc_generic_fallback（完整兜底）
                   ├─ mi_malloc_generic_admin：必要时初始化线程；周期性管理
                   ├─ mi_find_page：找/建一个有货的页
                   │    └─ 失败 → mi_theap_collect(force) 后再试一次
                   ├─ _mi_page_malloc_zero：这次必然成功（不该再递归）
                   └─ 大页满员 → mi_page_to_full（abandon 或移入 full 队列）
```

一个容易被忽略的细节：`zero` 与 `huge_alignment` 被打包进同一个参数 `zero_huge_alignment`（zero 占最低位）。函数注释说明了动机——4 参数形式在 MSVC 上对 malloc 快路径的代码生成更友好（见 [src/page.c:1085-1090](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L1085-L1090) 的注释）。

#### 4.1.3 源码精读

弹空分岔点——快路径的最后一行判断，就是本讲的起点（[src/alloc.c:41-48](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L41-L48)）：

```c
  // check the free list
  mi_block_t* const block = page->free;
  const mi_used_t used = page->used;
  ...
  if (block == NULL) {
    return _mi_malloc_generic(theap, size, (zero ? 1 : 0), ppage);
  }
```

`mi_theap_malloc_generic` 是 alloc.c 侧的包装层（[src/alloc.c:163-187](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L163-L187)）：加上 padding 尺寸、处理 guarded 采样，然后把 `zero` 装进最低位转交 `_mi_malloc_generic`：

```c
static mi_decl_forceinline void* mi_theap_malloc_generic(...) {
  ...
  void* const p = _mi_malloc_generic(theap, size + MI_PADDING_SIZE,
                                     (zero ? 1 : 0) | huge_alignment, ppage);
  ...
}
```

主体在 page.c（[src/page.c:1091-1117](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L1091-L1117)）：

```c
void* _mi_malloc_generic(mi_theap_t* theap, size_t size, size_t zero_huge_alignment, mi_page_t** ppage) mi_attr_noexcept
{
  const bool zero = ((zero_huge_alignment & 1) != 0);
  const size_t huge_alignment = (zero_huge_alignment & ~1);
  mi_page_t* page = NULL;

  // fast path objects that fit in a small page
  if mi_likely(mi_theap_is_initialized(theap) && ++theap->generic_count < 1000 && huge_alignment==0) {
    const size_t req_size = size - MI_PADDING_SIZE;
    if (req_size < MI_SMALL_MAX_OBJ_SIZE) {
      mi_page_queue_t* pq = mi_page_queue(theap, size);
      page = mi_page_queue_find_free(theap,pq);
      if (page!=NULL) {
        if (ppage!=NULL) { *ppage = page; }
        return _mi_page_malloc_zero(theap,page,size,zero);
      }
    }
  }
  // otherwise fallback
  return mi_malloc_generic_fallback(theap,size,zero, huge_alignment,ppage);
}
```

注意 `++theap->generic_count`——**无论走内嵌快试还是 fallback，慢路径计数都会累加**，这是 4.5 节管理节拍的心跳来源。注意这里的尺寸判断用的是 `MI_SMALL_MAX_OBJ_SIZE`（10 KiB，小**页**的对象上限），比快路径的 `MI_SMALL_SIZE_MAX`（1 KiB）宽：中等的 small/medium 对象也允许在这条内嵌路径上找页。

完整兜底 `mi_malloc_generic_fallback`（[src/page.c:1048-1082](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L1048-L1082)）：

```c
static mi_decl_noinline void* mi_malloc_generic_fallback(...) {
  theap = mi_malloc_generic_admin(theap);          // 1. 初始化 + 周期管理
  if (theap==NULL) return NULL;

  mi_page_t* page = mi_find_page(theap, size, huge_alignment);   // 2. 找/建页
  if mi_unlikely(page == NULL) {                   //    第一次失败：强制回收后重试
    mi_theap_collect(theap, true /* force? */);
    page = mi_find_page(theap, size, huge_alignment);
  }
  if mi_unlikely(page == NULL) {                   // 3. 真的没内存了
    _mi_error_message(ENOMEM, "unable to allocate memory (%zu bytes)\n", req_size);
    return NULL;
  }
  ...
  void* const p = _mi_page_malloc_zero(theap,page,size,zero);    // 4. 必然成功
  ...
  if (mi_page_block_size(page) > MI_SMALL_MAX_OBJ_SIZE && mi_page_is_full(page)) {
    mi_page_to_full(page, mi_page_queue_of(page));               // 5. 大页满员处理
  }
  return p;
}
```

注释里特意强调「this should never recurse through _mi_page_malloc」：走到这里时页必须已有可用块，否则说明逻辑有 bug。

#### 4.1.4 代码实践

用尺寸制造「每次都走慢路径」与「几乎不走慢路径」的对照（**示例代码**，非项目原有文件）：

```c
// slowpath-demo.c —— 编译时链接 debug 版 mimalloc
#include <mimalloc.h>
#include <stdio.h>
#include <time.h>

int main(void) {
  clock_t t0 = clock();
  for (int i = 0; i < 2000000; i++) {
    mi_free(mi_malloc(64));            // 64B：常驻快路径
  }
  clock_t t1 = clock();
  for (int i = 0; i < 2000000; i++) {
    mi_free(mi_malloc(2000));          // 2000B > 1KiB：每次必进 generic
  }
  clock_t t2 = clock();
  printf("small: %ld ms, generic: %ld ms\n",
         (long)((t1-t0)*1000/CLOCKS_PER_SEC),
         (long)((t2-t1)*1000/CLOCKS_PER_SEC));
  return 0;
}
```

1. **实践目标**：体感确认「尺寸超过 `MI_SMALL_SIZE_MAX` 后每次分配都要进 `_mi_malloc_generic`」。
2. **操作步骤**：在 u1-l2 的 debug 构建产物上编译链接（例如 `gcc slowpath-demo.c -o slowpath-demo /path/to/out/debug/libmimalloc-debug.a -lm`，具体库名以构建输出为准），运行。
3. **需要观察的现象**：两段的耗时差；再用 `MIMALLOC_SHOW_STATS=1` 运行，对比两次输出中 pages 段的 `searches` 平均值。
4. **预期结果**：2000B 那段明显更慢、`searches` 更活跃。注意 debug 构建本身有断言与 padding 开销，绝对值无意义，看相对差即可。具体倍数**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`_mi_malloc_generic` 已经是慢路径了，为什么内部还要再放一段「小对象快试」？直接全部交给 fallback 不行吗？

**答案**：进入慢路径的小请求大多数只是当前页暂时弹空，页队列里立刻能找到可用页。内嵌快试跳过了 fallback 中的线程初始化检查、周期管理分支和 OOM 二次尝试，路径更短；同时它把 `mi_page_queue_find_free`（可能内联）与弹块动作直接串联，少一层 noinline 函数调用。只有在内嵌快试失败（队列真没页）或不满足条件（大对象、未初始化、超对齐）时才付出完整兜底的代价。

**练习 2**：`zero` 和 `huge_alignment` 为什么合并成一个参数传递？

**答案**：见 [src/page.c:1089-1090](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L1089-L1090) 的注释：合并后 `_mi_malloc_generic` 只有 4 个参数，在 MSVC 上对 malloc 快路径的代码生成更好（参数可全部放进寄存器）。`zero` 只占最低 1 位（布尔），`huge_alignment` 本身是 `MI_ARENA_SLICE_SIZE` 的倍数（低位为 0），二者按位或即可无损打包，入口处再用 `& 1` 与 `& ~1` 拆开。

### 4.2 页队列搜索：mi_find_page 与候选策略

#### 4.2.1 概念说明

`mi_find_page` 是「找页」的统一入口：先根据尺寸（或超对齐要求）定位到页队列，再在队列里找有货的页。搜索采用 **next-fit 扫描 + 有限候选**的混合策略，而不是简单 first-fit。原因有二：

- 同一 bin 的页可能很多，每次都从头扫会反复踩长寿命的满页（缓存不友好也浪费时间）；
- 满页应该被「隔离」出去（移到 full 队列或直接 abandon），不让后续搜索再碰到。

搜索时还有一条**偏好规则**：在候选页中优先选 `used` 更高（更满）的页。直觉是让高使用率的页继续吃满、让低使用率的页有机会整页变空被释放——这是 mimalloc 降低碎片的一贯思路（u1-l1 讲的 free list sharding 让「整页变空」成为常态）。

#### 4.2.2 核心流程

`mi_find_page` 的分流（[src/page.c:950-975](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L950-L975)）：

```text
mi_find_page(theap, size, huge_alignment)
  ├─ req_size > MI_MAX_ALLOC_SIZE？ → 报 EOVERFLOW，返回 NULL（防溢出）
  ├─ pq = mi_page_queue(theap, huge_alignment>0 ? MI_LARGE_MAX_OBJ_SIZE+1 : size)
  ├─ pq 是 huge 队列？ → mi_huge_page_alloc（u4-l3 详讲）
  └─ 否则 → mi_page_queue_find_free(theap, pq)
       ├─ ① lookup_free_first：只看队首
       │     队首页 free 非空 → 直接用（最常见）
       │     队首页 local_free 非空 → quick_collect 搬到 free → 用
       │     都空 → 返回 NULL
       └─ ② find_free_ex：next-fit 扫描整个队列
             每页：收集释放 → 仍无货且不可扩展 → 满页处理（retain/abandon）
                   有货或可扩展 → 记为候选，candidate_limit 倒数
             找到候选后：若 free 仍空（可扩展页）→ extend_free
             全部落空 → collect_retired → mi_page_fresh → 仍失败且 first_try → 递归再试一次
```

`mi_page_free_quick_collect` 是 ① 的核心（[src/page.c:204-212](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L204-L212)）：它只做 `free = local_free` 的一次 O(1) 搬运，**不碰**需要原子操作的 `xthread_free`——快路径的「快」在这里再次得到体现。

#### 4.2.3 源码精读

`mi_find_page` 本体（[src/page.c:950-968](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L950-L968)）：

```c
static mi_page_t* mi_find_page(mi_theap_t* theap, size_t size, size_t huge_alignment) mi_attr_noexcept {
  const size_t req_size = size - MI_PADDING_SIZE;  // correct for padding_size in case of an overflow on `size`
  if mi_unlikely(req_size > MI_MAX_ALLOC_SIZE) {
    _mi_error_message(EOVERFLOW, "allocation request is too large (%zu bytes)\n", req_size);
    return NULL;
  }
  mi_page_queue_t* pq = mi_page_queue(theap, (huge_alignment > 0 ? MI_LARGE_MAX_OBJ_SIZE+1 : size));
  mi_page_t* page;
  if mi_unlikely(mi_page_queue_is_huge(pq) || req_size > MI_MAX_ALLOC_SIZE) {
    page = mi_huge_page_alloc(theap,size,huge_alignment,pq);
  }
  else {
    page = mi_page_queue_find_free(theap,pq);
  }
  ...
}
```

`mi_page_queue` 只是 `_mi_bin(size)` 下标取队列（[include/mimalloc/internal.h:945-949](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L945-L949)）；`mi_page_queue_is_huge` 用「不可能的 block_size」识别特殊队列（[src/page-queue.c:40-50](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-queue.c#L40-L50)）。

扫描主体 `mi_page_queue_find_free_ex`（[src/page.c:766-836](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L766-L836)），节选关键决策：

```c
  long candidate_limit = 0;
  long page_full_retain = (pq->block_size > MI_SMALL_MAX_OBJ_SIZE ? 0 : theap->page_full_retain);
  mi_page_t* page_candidate = NULL;
  mi_page_t* page = pq->first;

  while (page != NULL) {
    mi_page_t* next = page->next;
    count++;
    candidate_limit--;

    bool immediate_available = mi_page_immediate_available(page);
    if (!immediate_available) {
      _mi_page_free_collect(page, false);        // 收集本线程与跨线程释放
      immediate_available = mi_page_immediate_available(page);
    }

    if (!immediate_available && !mi_page_is_expandable(page)) {
      page_full_retain--;                        // 彻底满且不可扩展的页
      if (page_full_retain < 0) {
        mi_page_to_full(page, pq);               // 隔离出去
      }
    }
    else {
      if (page_candidate == NULL) {
        page_candidate = page;
        candidate_limit = _mi_option_get_fast(mi_option_page_max_candidates);  // 默认 4
      }
      ...
      else if (page->used >= page_candidate->used && !mi_page_is_mostly_used(page)) {
        page_candidate = page;                   // 优先更满的候选页
      }
      if (immediate_available || candidate_limit <= 0) break;
    }
    page = next;
  }
```

扫描结束后的收尾（[src/page.c:842-871](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L842-L871)）：

```c
  if (page_candidate != NULL) { page = page_candidate; }
  if (page != NULL) {
    if (!mi_page_immediate_available(page)) {
      mi_assert_internal(mi_page_is_expandable(page));
      if (!mi_page_extend_free(theap, page)) { page = NULL; }   // 候选是可扩展页 → 扩展
    }
  }
  if (page == NULL) {
    _mi_theap_collect_retired(theap, false);     // 或许能让 retired 页复活
    page = mi_page_fresh(theap, pq);             // 实在没有 → 申请新页
    if (page == NULL && first_try) {
      page = mi_page_queue_find_free_ex(theap, pq, false);      // 再试一次（可能回收了 abandoned 页）
    }
  }
  else {
    mi_page_queue_move_to_front(theap, pq, page);  // 用过的页搬到队首
    page->retire_expire = 0;
  }
```

把页搬到队首这步很重要：队首就是 `pages_free_direct` 指向的页（经 `mi_theap_queue_first_update` 刷新，[src/page-queue.c:209-244](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-queue.c#L209-L244)），于是后续的快路径又能一次访存命中它——**慢路径的尾声是在为下一批快路径铺路**。

两个搜索相关选项的默认值在 [src/options.c:162-163](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L162-L163)：`page_full_retain = 2`（满页容忍 2 个不隔离，且只对 small 页生效）、`page_max_candidates = 4`（最多搜 4 个候选就停）。按 u2-l3 讲过的规则，它们对应环境变量 `MIMALLOC_PAGE_FULL_RETAIN`、`MIMALLOC_PAGE_MAX_CANDIDATES`。

#### 4.2.4 代码实践

统计输出里直接有一个衡量搜索长度的指标：`searches`（[src/stats.c:400](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L400) 打印「平均每次搜索扫描的页数」，分子 `page_searches` 在 [src/page.c:838](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L838) 累加）。

1. **实践目标**：验证 `page_max_candidates` 对搜索行为的影响。
2. **操作步骤**：写一个程序，交替分配与释放同一 bin（如 2000 字节）的块几万次，制造「队列里多页、部分有货」的局面；分别以 `MIMALLOC_PAGE_MAX_CANDIDATES=1`、`MIMALLOC_PAGE_MAX_CANDIDATES=32` 与默认值运行，`MIMALLOC_SHOW_STATS=1` 收集输出。
3. **需要观察的现象**：pages 段 `searches` 行的平均值变化。
4. **预期结果**：调大后平均值上升（允许扫更多页才停）。若你的分配模式下队列从不满员，差异可能不明显——那本身也是一个有价值的观察。具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`mi_page_queue_lookup_free_first` 只看队首页就敢返回，凭什么保证队首大概率有货？

**答案**：因为 4.2.3 末尾的 `mi_page_queue_move_to_front`——每次慢路径选中一个页都会把它搬到队首。于是队首永远是「最近一次证明有货」的页，多数请求看它一眼就够了。这和 `pages_free_direct` 直查数组指向队首页是同一套思想：让最热的数据结构位置指向最可能命中的页。

**练习 2**：候选选择时为什么要排除 `mi_page_is_mostly_used(page)`（超过 7/8 满的页，[include/mimalloc/internal.h:925-929](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L925-L929)）？

**答案**：见 [src/page.c:811-814](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L811-L814) 的注释「prefer to reuse fuller pages (in the hope the less used page gets freed)」：优先复用较满的页，让较空的页保持低使用率、有朝一日在 `used==0` 时被 retire 释放。但一个即将满（>7/8）的页做候选意义不大——它很快也要被隔离成满页，不如把机会留给还有较大空间的页，减少后续进慢路径的频率。

**练习 3**：`find_free_ex` 末尾失败时为什么用 `first_try` 参数控制只递归一次，而不是循环重试？

**答案**：重试的唯一收益场景是 `mi_page_fresh` 顺带回收了某个 abandoned 页（注释见 [src/page.c:860-862](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L860-L862)），这种「一次分配顺带改变队列状态」的事件不会连续大量发生；无界重试则在真 OOM 时变成死循环。用 `first_try=false` 限制深度为 1，是把「罕见的二阶机会」和「确定的失败」分开处理。

### 4.3 mi_page_extend_free：分批初始化与按需 commit

#### 4.3.1 概念说明

这是本讲最重要的模块，直接回答本讲的核心问题：**一个新页的块是「一次性全部初始化」还是「分批初始化」？** 答案是分批，且每次至多约 8 KiB。

「初始化块」意味着把空闲块串成 free list——每个块的头 8 字节要写入 `next` 指针。这件事看似便宜，实则有两个隐藏代价：

1. **触碰即 commit**：写一个字节就会触发操作系统按 4 KiB OS 页提交物理内存。一次性串好 64 KiB 页的全部两千多个块，等于立刻付出整页的 RSS，哪怕程序只用了其中 10 个块。
2. **破坏局部性**：顺序初始化的 free list 弹出顺序等于地址顺序，会把后续分配散到整页上，缓存行与 TLB 的压力更大（这也是 secure 模式随机化 free list 的动机之一）。

所以 mimalloc 让 `capacity` 渐进逼近 `reserved`：free list 弹空了才扩展下一段。函数注释（[src/page.c:626-629](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L626-L629)）还记录了教训：作者试过首次分配用 bump pointer 方式，但没让任何基准变快，于是保持现方案。

#### 4.3.2 核心流程

```text
mi_page_extend_free(theap, page)
  ├─ page->free != NULL 或 capacity >= reserved → 无事可做，返回 true
  ├─ 计算本次扩展块数：
  │    extend = reserved - capacity                       （想扩这么多）
  │    max_extend = bsize >= 8KiB ? 1 : floor(8KiB / bsize)（每次至多碰 8KiB）
  │    extend = min(extend, max(max_extend, MI_MIN_EXTEND))
  ├─ 若该 slice 只部分 commit（slice_committed > 0）：
  │    extend 再压到不超过一个 arena slice（64 KiB）
  │    needed = align_up((capacity+extend)*bsize, min_commit)
  │    needed > slice_committed → _mi_os_commit 补提交，更新 slice_pcommitted
  └─ 初始化 free list：
       普通模式 → mi_page_free_list_extend（顺序串联）
       MI_SECURE>=2 且 extend>=2 → ..._secure（随机交错串联）
       capacity += extend；pages_extended 计数 +1
```

扩展量的数学表达：

\[ \text{extend} \;=\; \min\Big(\text{reserved} - \text{capacity},\; \max\Big(\Big\lfloor \frac{8192}{\text{bsize}} \Big\rfloor,\; \text{MI\_MIN\_EXTEND}\Big)\Big) \]

于是把一个 fresh 页吃满所需的扩展次数约为

\[ N_{\text{extend}} \;\approx\; \left\lceil \frac{R}{\lfloor 8192/\text{bsize} \rfloor} \right\rceil , \qquad R = \left\lfloor \frac{\text{页大小}}{\text{bsize}} \right\rfloor . \]

以 64 位 Linux 的 debug 构建、请求 16 字节为例：块实际规格为 \( 16 + 8 = 24 \) 字节（debug 每块 +8 padding，见 u3-l3），64 KiB 的 small 页可容 \( R = \lfloor 65536/24 \rfloor = 2730 \) 块，每次扩展上限 \( \lfloor 8192/24 \rfloor = 341 \) 块，故约需 \( \lceil 2730/341 \rceil = 9 \) 次扩展才能吃满一页。

#### 4.3.3 源码精读

扩展上限的宏定义（[src/page.c:618-623](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L618-L623)）：

```c
#define MI_MAX_EXTEND_SIZE    (8*1024)      // heuristic, one or two OS pages seems to work well.
#if (MI_SECURE>=2)
#define MI_MIN_EXTEND         (8*MI_SECURE) // extend at least by this many
#else
#define MI_MIN_EXTEND         (1)
#endif
```

`mi_page_extend_free` 的扩展量计算（[src/page.c:646-659](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L646-L659)）：

```c
  const size_t bsize = mi_page_block_size(page);
  size_t extend = (size_t)page->reserved - page->capacity;
  ...
  size_t max_extend = (bsize >= MI_MAX_EXTEND_SIZE ? MI_MIN_EXTEND : MI_MAX_EXTEND_SIZE/bsize);
  if (max_extend < MI_MIN_EXTEND) { max_extend = MI_MIN_EXTEND; }
  ...
  if (extend > max_extend) {
    // ensure we don't touch memory beyond the page to reduce page commit.
    // the `lean` benchmark tests this. Going from 1 to 8 increases rss by 50%.
    extend = max_extend;
  }
```

注释里的 `lean` 基准数据是「分批」价值的直接证据：扩展粒度选择不当（注释所述从 1 到 8 的变化）可使 RSS 增加 50%。

按需 commit 部分（[src/page.c:664-690](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L664-L690)）：

```c
  const size_t slice_committed = mi_page_slice_committed(page);
  if (slice_committed > 0) {
    // reduce extend if it commits more than an arena slice
    if ((extend * bsize) > MI_ARENA_SLICE_SIZE) {
      extend = _mi_divide_up(MI_ARENA_SLICE_SIZE, bsize);
    }
    const size_t needed_size = (page->capacity + extend)*bsize;
    size_t needed_commit = _mi_align_up( mi_page_slice_offset_of(page, needed_size), mi_page_min_commit_size());
    ...
    if (needed_commit > slice_committed) {
      if (!_mi_os_commit(_mi_theap_subproc(theap), mi_page_slice_start(page) + slice_committed,
                         needed_commit - slice_committed, NULL)) {
        return false;                                   // commit 失败 → 扩展失败
      }
      page->slice_pcommitted = (uint16_t)(needed_commit / _mi_os_page_size());
    }
  }
```

即：扩展不能越过已 commit 的边界，需要时按 OS 页粒度补提交（arena 与 slice 的细节在 u6-l3 展开）。

free list 的顺序初始化（[src/page.c:589-612](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L589-L612)）：

```c
static mi_decl_noinline void mi_page_free_list_extend( mi_page_t* const page, const size_t bsize, const size_t extend)
{
  ...
  mi_block_t* const start = mi_page_block_at(page, page_area, bsize, page->capacity);
  mi_block_t* const last  = mi_page_block_at(page, page_area, bsize, page->capacity + extend - 1);
  mi_block_t* block = start;
  while(block <= last) {
    mi_block_t* next = (mi_block_t*)((uint8_t*)block + bsize);
    mi_block_set_next(page,block,next);      // 块地址 = 页起点 + 序号×bsize，纯算术
    block = next;
  }
  mi_block_set_next(page, last, page->free); // 接到现有链前面
  page->free = start;
}
```

收尾两行（[src/page.c:692-705](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L692-L705)）：

```c
  if (extend < MI_MIN_SLICES || MI_SECURE<2) {
    mi_page_free_list_extend(page, bsize, extend );
  }
  else {
    mi_page_free_list_extend_secure(theap, page, bsize, extend);   // 随机化（secure 模式）
  }
  page->capacity += (uint16_t)extend;                              // 关键：capacity 渐增
  #if MI_STAT>0
  mi_theap_stat_increase(theap, page_committed, extend * bsize);
  #endif
```

最后，fresh 页的第一次扩展发生在 `_mi_page_init`（[src/page.c:754-757](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L754-L757)）：页从 arena 拿到时 `capacity == 0`、`free == NULL`（该函数开头的断言串明确列出了这些初始条件），init 只调用一次 `mi_page_extend_free`——后续扩展全部由本讲的慢路径在搜索时按需触发。

#### 4.3.4 代码实践

纯推导型实践，验证 `capacity` 的分批增长：

1. **实践目标**：为「分批初始化」给出一个可检验的数量预测。
2. **操作步骤**：按 4.3.2 的公式，对块规格 24 字节（debug 构建 16 字节请求）算出「吃满一页需 9 次扩展」；再对 128 字节请求（debug 规格仍为 128+8=136？请先用 `mi_good_size(128)` 确认实际规格）重算一遍。
3. **需要观察的现象**：第 5 节综合实践中会用到这个预测——统计输出的 `extended` 计数应约等于「页数 × 每页扩展次数」。
4. **预期结果**：推导值与实测值在同一数量级且偏差可解释（末次扩展的零头、其他 bin 的干扰）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `max_extend` 对大块（`bsize >= 8KiB`）反而取最小值 1？

**答案**：`MI_MAX_EXTEND_SIZE/bsize` 此时为 0，取 `max(..., MI_MIN_EXTEND)` 后为 1。语义很自然：块本身就超过 8 KiB 时，每初始化一块就已经触碰了超过 8 KiB 的内存，再成批初始化毫无节省可言，不如一块一块来，最大程度推迟触碰。

**练习 2**：`mi_page_free_list_extend` 里的 `mi_page_block_at(page, page_area, bsize, page->capacity)` 是怎么算出块地址的？为什么可以这样算？

**答案**：`(uint8_t*)page_start + i * block_size`（[src/page.c:34-39](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L34-L39)）。可行 because 一个页内所有块等长（单一 size class，u3-l2 讲过「页是定长容器」），块位置纯由序号决定，无需任何 per-block 头部或索引结构——这也是 mimalloc「无每块头」设计的根基。

**练习 3**：如果让 `mi_page_extend_free` 一次性把 `reserved` 全部初始化，最直接的两个后果是什么？

**答案**：其一，fresh 页创建瞬间触碰整页地址范围，RSS/commit 立即到位，违背「用多少碰多少」的原则（注释中 lean 基准 RSS +50% 的教训即属此类）；其二，`pages_extended` 与 `page_committed` 统计失去意义，且 `slice_pcommitted` 的按需 commit 逻辑退化为整页 commit。好处只有省掉后续进慢路径的次数，而慢路径本身已由 move_to_front 与直查数组优化得很便宜。

### 4.4 mi_page_fresh：新页申请、abandoned 页回收与 OOM 兜底

#### 4.4.1 概念说明

页队列搜索彻底落空后，就要向内存管理层要新页。`mi_page_fresh` 是薄封装，真正干活的是 `mi_page_fresh_alloc`，它有一个容易忽略的分支：从 arena 拿到的页**不一定是全新的**——可能是某个已退出线程遗弃的页（abandoned，u6-l4 的主题）。此时要做的是「认领」（reclaim）：把页挂到当前 theap 的队列上、刷新 `used` 计数，让它重新可用。这使线程间可以复用彼此的残留内存而不必归还 OS。

另外注意 fresh 页的初始化分工：`_mi_arenas_page_alloc` 返回时页结构已由 arena 侧「部分初始化」（`_mi_page_init` 的注释与断言表明 heap/theap 已挂好、capacity 为 0），随后 `_mi_page_init` 做第一次 `mi_page_extend_free`。`mi_page_fresh_alloc` 内部调用链中没有显式出现 `_mi_page_init`——它发生在 `_mi_arenas_page_alloc` 的内部（arena.c，u6 单元核实），本讲只需知道契约：**返回的页要么 immediate_available，要么可扩展**。

#### 4.4.2 核心流程

```text
mi_page_fresh(theap, pq)
  └─ mi_page_fresh_alloc(theap, pq, pq->block_size, 0)
       ├─ page = _mi_arenas_page_alloc(theap, block_size, 0)   // 向 arena 要 slice
       │    └─ NULL → OOM 返回 NULL
       ├─ page 是 abandoned 页？
       │    ├─ _mi_theap_page_reclaim：挂上新 theap、刷新 used、推入队尾
       │    └─ 仍无立即可用块且可扩展 → mi_page_extend_free
       │         └─ 失败（commit 不出来）→ _mi_page_abandon 再度遗弃，返回 NULL
       └─ 全新页 → mi_page_queue_push 挂到队首（联动刷新 pages_free_direct）
```

#### 4.4.3 源码精读

`mi_page_fresh` 与 `mi_page_fresh_alloc`（[src/page.c:308-351](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L308-L351)）：

```c
static mi_page_t* mi_page_fresh_alloc(mi_theap_t* theap, mi_page_queue_t* pq, size_t block_size, size_t page_alignment) {
  ...
  mi_page_t* page = _mi_arenas_page_alloc(theap, block_size, page_alignment);
  if (page == NULL) {
    return NULL;                       // out-of-memory
  }
  if (mi_page_is_abandoned(page)) {
    _mi_theap_page_reclaim(theap, page);                 // 认领遗弃页
    if (!mi_page_immediate_available(page)) {
      if (mi_page_is_expandable(page)) {
        if (!mi_page_extend_free(theap, page)) {
          _mi_page_abandon(page,pq);   // commit 失败只好再遗弃
          return NULL;
        };
      }
      ...
    }
  }
  else if (pq != NULL) {
    mi_page_queue_push(theap, pq, page);                 // 全新页挂队首
  }
  ...
  return page;
}

static mi_page_t* mi_page_fresh(mi_theap_t* theap, mi_page_queue_t* pq) {
  ...
  mi_page_t* page = mi_page_fresh_alloc(theap, pq, pq->block_size, 0);
  ...
  return page;
}
```

reclaim 的实现（[src/page.c:277-289](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L277-L289)）——注意它调用了 4.2 节见过的 `_mi_page_free_collect` 来刷新 `used`：

```c
void _mi_theap_page_reclaim(mi_theap_t* theap, mi_page_t* page)
{
  ...
  mi_page_set_theap(page,theap);
  _mi_page_free_collect(page, false);        // ensure used count is up to date
  mi_page_queue_t* pq = mi_theap_page_queue_of(theap, page);
  mi_page_queue_push_at_end(theap, pq, page);
  ...
}
```

全新页挂队首用的 `mi_page_queue_push`（[src/page-queue.c:277-304](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-queue.c#L277-L304)）末尾一行 `mi_theap_queue_first_update(theap, queue)` 把直查数组同步指向新队首——再次体现「慢路径为快路径铺路」。

`mi_page_queue_push_at_end`（reclaim 用，[src/page-queue.c:306-333](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-queue.c#L306-L333)）则把认领的页放在队尾：刚认领的页状态未知，先不抢队首的热位。

`_mi_page_free_collect` 本体（[src/page.c:214-243](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L214-L243)）：先用原子交换收割 `xthread_free`（`mi_page_thread_free_collect`，[src/page.c:186-201](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L186-L201)），再把 `local_free` 搬进 `free`，使 `used` 计数与真实存活数对齐——这是 abandon/reclaim 能跨线程安全交接的前提（细节属于 u5、u6）。

#### 4.4.4 代码实践

源码阅读型实践（不运行代码）：

1. **实践目标**：理清「一次 fresh 分配」可能经历的三种结局。
2. **操作步骤**：对照 [src/page.c:308-341](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L308-L341)，为每条路径写一行注释：①`_mi_arenas_page_alloc` 返回 NULL；②返回 abandoned 页；③返回全新页。再回答：结局②中为什么认领后还要检查 `mi_page_is_expandable`？
3. **需要观察的现象**：自己的注释能否覆盖源码的全部分支（还有一个 `mi_assert(false)` 的「不应发生」分支）。
4. **预期结果**：②中 abandoned 页可能 `capacity < reserved`（原线程没吃满就退出了），此时靠 `mi_page_extend_free` 提供第一批可用块；若 commit 失败则放弃认领。

#### 4.4.5 小练习与答案

**练习 1**：`mi_page_fresh_alloc` 拿到 abandoned 页时用 `push_at_end`，拿到全新页时用 `push`（队首），为什么区别对待？

**答案**：全新页一定是刚扩展过、`free` 有货的热页，放队首让快路径立刻命中；认领的 abandoned 页虽然刷新了计数，但其 free list 状态未知、可能只剩零星块，放队尾让它先被扫描而非霸占直查数组指向的热位。两个 push 函数的差异正是 [src/page-queue.c:277-333](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-queue.c#L277-L333) 中 `mi_theap_queue_first_update` 的调用条件不同。

**练习 2**：`mi_malloc_generic_fallback` 在 `mi_find_page` 失败后调用 `mi_theap_collect(theap, true)` 再试一次，这次强制回收大概能腾出什么？

**答案**：看 [src/theap.c:123-148](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L123-L148)：`mi_theap_collect_ex` 依次做 `_mi_deferred_free`（触发外部注册的延迟释放回调，可能释放一批用户对象）、`_mi_theap_collect_retired(force)`（把到期 retired 页真正释放）、逐页 `mi_theap_page_collect`（收集各页的 thread_free 链，让 `used==0` 的页可释放）、`_mi_arenas_collect`（purge arena）。也就是说兜底动用的是「已被释放但还没被记账」的内存——这正是三链设计中 thread_free 延迟收割的一面。

### 4.5 管理节拍：generic_count 与 mi_option_generic_collect

#### 4.5.1 概念说明

慢路径还有一个副产品：它是分配器做**周期性管理**的天然时机。每次进 `_mi_malloc_generic` 都让 `theap->generic_count` 加一（包括 4.1 节的内嵌快试路径），攒够 1000 次就执行一轮管理动作。这带来一个重要的推论：**管理任务的触发频率与程序的「慢路径命中率」成正比**，而与墙钟时间或分配总量无关——分配稳定的程序几乎不做管理，颠簸的程序管理更勤。

两级节拍：

- **每 1000 次慢路径**：mini-collect——调用注册的 deferred free 回调 + 释放到期的 retired 页；
- **累计 `mi_option_generic_collect` 次（默认 10000）**：full collect——`mi_theap_collect` 完整收集（见 4.4.5 练习 2 的清单）。

#### 4.5.2 核心流程

```text
mi_malloc_generic_admin(theap)
  ├─ theap 未初始化？ → _mi_thread_init()（首次分配的自举，见 u3-l1/u7-l1）
  ├─ generic_count >= 1000？
  │    ├─ generic_collect_count += generic_count；generic_count = 0
  │    ├─ generic_collect_count >= mi_option_generic_collect（默认 10000，clamp 到 [1,10^6]）？
  │    │    ├─ 是 → generic_collect_count = 0；mi_theap_collect(force=false)   // full
  │    │    └─ 否 → _mi_deferred_free + _mi_theap_collect_retired               // mini
  └─ 返回 theap
```

即 full collect 的平均触发周期为每 \( C \) 次慢路径一次（\( C \) 默认 10000），mini-collect 周期为每 1000 次。用环境变量 `MIMALLOC_GENERIC_COLLECT` 可调 \( C \)（u2-l3 的命名规则：`MIMALLOC_` + 小写选项名）。

#### 4.5.3 源码精读

节拍器全部逻辑（[src/page.c:1011-1042](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L1011-L1042)）：

```c
static mi_theap_t* mi_malloc_generic_admin(mi_theap_t* theap)
{
  if mi_unlikely(!mi_theap_is_initialized(theap)) {
    ...
    theap = _mi_thread_init();                  // 首次分配：初始化线程与其 theap
    ...
  }
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
      _mi_deferred_free(theap, false);          // call potential deferred free routines
      _mi_theap_collect_retired(theap, false);  // free retired pages
    }
  }
  return theap;
}
```

选项默认值（[src/options.c:160](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L160)）：`{ 10000, MI_OPTION_UNINIT, MI_OPTION(generic_collect) }`，注释写明「collect theaps every N (=10000) generic allocation calls」。

`_mi_deferred_free` 里藏着递增 `theap->heartbeat` 的那一行（[src/page.c:990-999](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L990-L999)）：

```c
void _mi_deferred_free(mi_theap_t* theap, bool force) {
  theap->heartbeat++;
  mi_deferred_free_fun* const fun = (mi_deferred_free_fun*)mi_atomic_load_ptr_acquire(void,&deferred_free);
  if (fun != NULL && !theap->tld->recurse) {
    ...
    fun(force, theap->heartbeat, arg);
    ...
  }
}
```

这个 heartbeat 与 `local_free` 链（u3-l2 讲过它使「free 耗尽」成为确定性事件）共同构成 u5-l3「延迟释放与心跳」机制的实现基础——本讲先记住调用点在这里。

#### 4.5.4 代码实践

1. **实践目标**：验证管理节拍由「慢路径次数」而非「分配总量」驱动。
2. **操作步骤**：写两个程序（**示例代码**思路）：A 循环 `mi_malloc(64)` 两百万次再统一释放（命中大量快路径）；B 循环 `mi_malloc(64)` 并立即 `mi_free` 两百万次（free 后再 malloc 常从 free/local_free 链取，慢路径比例不同）。都开 `MIMALLOC_SHOW_STATS=1`。也可对 B 分别设 `MIMALLOC_GENERIC_COLLECT=100` 与默认值对比运行时间。
3. **需要观察的现象**：两程序的耗时差，以及 `MIMALLOC_GENERIC_COLLECT=100` 时 B 是否明显变慢。
4. **预期结果**：调小后 full collect 频率上升约 100 倍，若 collect 本身有成本则 B 变慢；如果程序根本没有可收集的东西（无 retired 页、无 deferred 回调），差异可能很小。**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：为什么 mimalloc 用「慢路径计数」而不是定时器或每 N 次总分配来做管理节拍？

**答案**：管理动作（收集 retired 页、触发 deferred free）只与堆的「混乱程度」相关，而堆变混乱正是反复进慢路径的信号；纯快路径的程序堆状态稳定，做管理是纯浪费。计数器方案零额外硬件（不需要定时器中断）、无锁（`generic_count` 是 theap 私有字段），且天然随负载自适应。

**练习 2**：`mi_option_get_clamp(mi_option_generic_collect, 1, 1000000L)` 里的 clamp 起什么作用？

**答案**：把用户通过 `MIMALLOC_GENERIC_COLLECT` 或 `mi_option_set` 设置的值限制在 \([1, 10^6]\) 区间：下界 1 防止设成 0 后每 1000 次慢路径都做 full collect（甚至除零式语义混乱），上界 \(10^6\) 防止设成超大值后 full collect 永远不发生、retired 页与延迟释放无限堆积。

## 5. 综合实践

本讲综合实践就是规格里给出的核心问题：**在 debug 构建里连续分配同 size 块直到统计输出出现新的 page，结合 `mi_page_fresh`/`mi_page_extend_free` 源码回答：一个新页的块是「一次性全部初始化」还是「分批初始化」？写出证据。**

**结论先行**（你将亲手验证）：分批初始化。一个 fresh 页在 `_mi_page_init` 时只做第一次扩展（最多约 8 KiB 的块），此后每当 `free` 弹空且该页仍可扩展，慢路径就再扩一段，直到 `capacity == reserved`。

### 步骤

1. **构建**（沿用 u1-l2 的方法）：

   ```bash
   mkdir -p out/debug && cd out/debug
   cmake -DCMAKE_BUILD_TYPE=Debug ../..
   make
   ```

   debug 构建默认 `MI_STAT=2`、`MI_PADDING=1`（[include/mimalloc/types.h:81-85,97](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L81-L97)），统计里才有 `extended` 等计数。

2. **编写测试程序**（**示例代码**，非项目原有文件；`snap.c`）：

   ```c
   #include <mimalloc.h>
   #include <stdio.h>

   int main(void) {
     printf("good_size(16) = %zu\n", mi_good_size(16));   // 确认块的实际规格（debug 下含 padding）
     const int N = 2800;                                   // 约 2730 块/页（24B 规格），略多
     void* p[N];
     for (int i = 0; i < N; i++) { p[i] = mi_malloc(16); } // 只分配不释放：单向吃页
     for (int i = 0; i < N; i++) { mi_free(p[i]); }
     return 0;
   }
   ```

   编译链接 debug 库（库名以构建输出为准，形如 `libmimalloc-debug.a`）。

3. **运行**：`MIMALLOC_SHOW_STATS=1 ./snap`，找到输出中的 `pages` 段（该段打印逻辑在 [src/stats.c:386-402](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L386-L402)），记下 `pages` 与 `extended` 两个数字。

### 需要观察的现象与证据链

- `extended` 明显大于对应 bin 的 `pages` 数。若「一次性初始化」成立，每页只需一次链表初始化动作，`extended` 应约等于 `pages`；若分批成立，比值应接近 4.3.2 推导的每页扩展次数（24 字节规格约 9 次，受 `mi_good_size` 实际输出与其他 bin 干扰，数量级对上即可）。
- 源码证据三件套：
  1. 扩展上限截断：[src/page.c:651-659](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L651-L659)（`extend = max_extend`，注释明言为了减少 page commit、引 lean 基准 RSS 证据）；
  2. `capacity` 渐增：[src/page.c:700](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L700)（`page->capacity += (uint16_t)extend;`——若一次性初始化，这里应该是 `= page->reserved`）；
  3. 计数器累加点：[src/page.c:642-644](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L642-L644)（`pages_extended` 在**每次** extend 时 +1，而非每页一次）。

### 预期结果

`extended / pages` 的比值显著大于 1（理想情况下接近「吃满一页所需扩展次数」）。把你的实测比值与 4.3.2 公式的推导值写在一起，偏差通常来自：其他 bin 的页也在扩展、末段扩展的零头、程序退出前释放触发的 retire。若想减少干扰，可把 N 调到刚好一页的块数并对比 N 略小时的输出差异。具体数值**待本地验证**。

## 6. 本讲小结

- 慢路径 `_mi_malloc_generic` 是分配器的工程部：快路径弹空、超大请求、线程未初始化、超对齐四种情形都汇入这里；它内部还有一段「小对象快试」，让最常见的「暂时弹空」不必走完整兜底。
- 页队列搜索采用 next-fit 扫描 + 有限候选（`page_max_candidates` 默认 4）：优先复用更满的页，隔离彻底满员的页（`page_full_retain` 默认 2），选中的页搬到队首、刷新 `pages_free_direct`——慢路径的尾声在为下一批快路径铺路。
- `mi_page_extend_free` 是「分批初始化」的实现：每次最多初始化约 8 KiB 的块、必要时按 OS 页粒度补 commit，`capacity` 渐进逼近 `reserved`；动机是少触碰内存、降 RSS（lean 基准的 +50% 教训写在注释里）。
- `mi_page_fresh` 向 arena 要页，可能拿到别的线程遗弃的 abandoned 页——reclaim 后挂队尾继续用；真 OOM 前还有「强制 collect 后重试一次」的兜底。
- 管理节拍由慢路径计数驱动：每 1000 次一次 mini-collect，累计 `mi_option_generic_collect`（默认 10000）次一次 full collect；`heartbeat` 与 deferred free 的调用点也在这里，是 u5-l3 的伏笔。

## 7. 下一步学习建议

本讲补完了 `mi_malloc` 的另一半，下一讲（u4-l3）顺着 `mi_find_page` 里已经露面的 `mi_huge_page_alloc` 往下，看超过 512 KiB 的大对象如何绕过 size class 页、直接向 arena/OS 申请专属单例页。之后进入单元五读 `mi_free`：你会再次遇到本讲的 `_mi_page_free_collect`、`mi_page_to_full` 与 retire 机制——从分配侧看它们是「找页时的障碍处理」，从释放侧看它们是「让页变空、变闲、被复用」的主角。建议在进入下一讲前，先把综合实践的 `extended/pages` 比值测出来，它会是你在 u9-l3 解读统计报表时的第一手直觉。
