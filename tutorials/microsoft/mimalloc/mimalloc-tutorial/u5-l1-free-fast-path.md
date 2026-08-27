# mi_free 快路径：本地 free 一次链表插入

## 1. 本讲目标

学完本讲，你应该能够：

1. 顺着 `mi_free` 的入口，说出释放路径「指针 → 页 → 分流 → 入链」的完整步骤。
2. 读懂 `mi_free_nonnull` 中用一次异或（XOR）划分出的四个分支的精确判定条件，并能回答「为什么一次异或就够」。
3. 精读 `mi_free_block_local`：数清本地释放到底做了哪几次读、哪几次写，确认其中没有任何原子读-改-写（CAS）操作。
4. 区分 `mi_free_block_local` 里两个 `mi_unlikely` 分支（`used==0` 触发的延迟退役、`check_full` 触发的满页出队）的进入条件，并理解 `local_free` 链表名符其实的「延迟」语义。

本讲是单元五的第一讲，只关注**本线程释放**这条快路径；跨线程释放（`mi_free_block_mt`）留给下一讲 u5-l2。

## 2. 前置知识

### 2.1 需要你已经掌握的内容（来自前面各讲）

- **u3-l2**：一个 `mi_page_t` 只装一种 size class 的块；页内有 `free` / `local_free` / `xthread_free` 三条链表，空闲块的头上 8 字节被复用为指向下一块的 `next` 指针（侵入式链表，`mi_block_t` 只有一个字段）。
- **u3-l4**：`mi_free` 手里只有一个裸指针，必须反查出它属于哪个页；默认 64 位构建启用了 `MI_PAGE_META_IS_ALIGNED`，页元数据按 256 MiB 对齐排在段头，反查可以纯算术完成，page map 只作权威登记簿。
- **u4-l1**：快路径的代码生成合同——`mi_decl_forceinline` 强制内联、`mi_likely`/`mi_unlikely` 引导分支预测；malloc 快路径在 release 下约 8 次访存、零原子操作。

### 2.2 本讲的新术语：原子操作的三个层级

讨论「free 快路径有没有原子操作」时，必须把「原子操作」拆开说，否则会失真：

| 层级 | 例子 | 汇编形态（x86-64） | 相对代价 |
| --- | --- | --- | --- |
| 普通读/写 | `page->used = used;` | `mov` | 最便宜 |
| 原子 load（relaxed / acquire） | `mi_atomic_load_relaxed(&page->xthread_id)` | 仍是 `mov`（x86 天然满足） | 与普通读几乎相同 |
| 原子读-改-写（RMW / CAS） | `mi_atomic_cas_weak_acq_rel`、`mi_atomic_or_relaxed` | 带 `lock` 前缀的 `cmpxchg` 等 | 会独占缓存行，贵一个数量级 |

本讲的核心结论将精确表述为：**本地 free 快路径没有任何 `lock` 前缀的 RMW/CAS 指令**，只有两次原子 load——它们在 x86 上就是普通 `mov`。

### 2.3 复习：xthread_id 的位编码（来自 u3-l2）

页的属主信息压缩在一个字段里：`page->xthread_id = 属主线程 id | 页标志`。低 2 位是页标志（`MI_PAGE_FLAG_MASK = 0x03`），高位是线程 id。本讲将看到 free 路径如何用**一次异或**同时检测「属主是不是我」和「标志位是否为零」。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [src/free.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c) | 释放路径本体：`mi_free` 入口、`mi_free_nonnull` 四路分流、`mi_free_block_local` 本地快路径、`mi_free_block_mt` 跨线程路径 |
| [include/mimalloc/types.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h) | `mi_block_t`、页标志常量、特殊线程 id、`mi_page_s` 结构与三条链表的文档注释 |
| [include/mimalloc/internal.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h) | `mi_page_xthread_id` 等取值内联函数、`_mi_aligned_ptr_page0` 对齐反查、`mi_block_set_next`、所有权位辅助函数 |
| [src/page.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c) | `mi_free_block_local` 两个 unlikely 分支的去向：`_mi_page_retire`、`_mi_page_unfull` |
| [include/mimalloc/prim-tls.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h) | `_mi_prim_thread_id()`：当前线程 id 的来源与低位对齐保证 |

提醒（u1-l3 已讲）：`free.c` 不是独立编译单元，它被 `include` 进 `alloc.c`（见 [src/free.c:L7-L13](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L7-L13) 的 `MI_IN_ALLOC_C` 守卫），因此本讲涉及的内联函数能跨 `mi_free` API 边界真正内联展开。

## 4. 核心概念与源码讲解

### 4.1 从裸指针到页：mi_free 的入口与页反查

#### 4.1.1 概念说明

`mi_malloc` 的参数里至少有尺寸信息，而 `mi_free(p)` 只有一个指针——连块多大都不知道。所以释放路径的第一步永远是「这个指针属于哪个页」。这一步在 u3-l4 已经从数据结构角度讲过（对齐反查 + page map 兜底），本讲从调用链角度把它放进 `mi_free` 的完整上下文：反查成功后，页指针会一路传给 `mi_free_nonnull`，后面的所有判断都基于这个 `mi_page_t*`。

#### 4.1.2 核心流程

```text
mi_free(p)
  │
  ├─ mi_validate_ptr_page_nonnull(p, "mi_free", false, &page)
  │     ├─ MI_PAGE_META_IS_ALIGNED 构建：
  │     │     page0 = _mi_aligned_ptr_page0(p)   ← 纯算术：按 256MiB 对齐下降 + 除以 64KiB slice
  │     │     page  = acquire load(page0->self)  ← 跨多 slice 的页由此跳到真实元数据
  │     └─ 其他构建：page = _mi_ptr_page(p)      ← 查 page map（u3-l4）
  │     └─ 失败（野指针/未对齐，debug 下报错）→ 直接返回，不释放
  │
  └─ mi_free_nonnull(p, page, NULL, true)       ← 进入 4.2 的四路分流
```

#### 4.1.3 源码精读

`mi_free` 本体只有三行——校验出页，然后交棒：

[src/free.c:L251-L256](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L251-L256)
```c
void mi_free(void* p) mi_attr_noexcept {
  mi_page_t* page;
  if mi_likely(mi_validate_ptr_page_nonnull(p,"mi_free",false,&page)) {
    mi_free_nonnull(p, page, NULL, true /* allow collect? */);
  }
}
```
中文说明：`mi_free` 把「指针是否合法、属于哪一页」整体委托给 `mi_validate_ptr_page_nonnull`，成功后以 `allow_collect=true` 进入 `mi_free_nonnull`（该参数只在跨线程分支用到，见 u5-l2）。

反查函数的主体是一个三层结构：

[src/free.c:L183-L198](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L183-L198)
```c
  mi_page_t* page;
  #if MI_PAGE_META_IS_ALIGNED
    page = _mi_aligned_ptr_page0(p);
    if mi_unlikely(page==NULL) return false; // p==NULL => page==NULL
    ...
    #if MI_SMALL_PAGE_SIZE == MI_ARENA_SLICE_SIZE
    // for mi_free_small we can avoid a load-acquire
    if (free_small) { mi_assert_internal(page == mi_atomic_load_ptr_acquire(mi_page_t,&page->self)); }
               else { page = mi_atomic_load_ptr_acquire(mi_page_t,&page->self); }
    #else
    ...
    page = mi_atomic_load_ptr_acquire(mi_page_t,&page->self);
    #endif
```
中文说明：默认构建走对齐反查。算术部分在 internal.h：

[include/mimalloc/internal.h:L775-L780](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L775-L780)
```c
static inline mi_page_t* _mi_aligned_ptr_page0(const void* p) {
  mi_page_t* const page_metas = (mi_page_t*)_mi_align_down_ptr(p,MI_PAGE_META_ALIGNMENT);
  const ptrdiff_t page_idx = ((uint8_t*)p - (uint8_t*)page_metas)/MI_ARENA_SLICE_SIZE;
  ...
  return &page_metas[page_idx];
}
```
中文说明：把指针按 `MI_PAGE_META_ALIGNMENT`（256 MiB）向下对齐得到本段页元数据数组的基址，再用指针与基址的差除以 64 KiB（一个 arena slice）得到下标——整个反查是两次算术运算，一次内存都没碰。随后那次 `acquire load(page0->self)` 是为了处理「页占多个 slice」的情形：`page0` 只是占位数组里的第一个槽，真实页结构由 `self` 指向。

值得注意的细节：注释明确说 `mi_free_small` 可以**省掉这次 acquire load**（小页恰好一个 slice 大小时 `page0` 就是页本身，只需 debug 断言确认）。这是 free 路径上被压榨掉的又一次访存，对应的 `mi_free_small` 实现在 [src/free.c:L268-L292](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L268-L292)。

非 `MI_PAGE_META_IS_ALIGNED` 的构建退回 page map 查表（[src/free.c:L204-L210](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L204-L210) 的 `_mi_ptr_page(p)`），那是 u3-l4 的两级基数树。

#### 4.1.4 代码实践

**实践目标**：确认「对齐反查是纯算术、无查表」，并理解 `mi_free` 与 `mi_free_small` 的成本差。

**操作步骤**（源码阅读型）：

1. 打开 [src/free.c:L172-L214](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L172-L214)，对照 [internal.h:L775-L780](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L775-L780)，数一数从 `p` 到 `page` 一共需要几次内存访问（提示：算术 0 次 + `self` 一次 acquire load）。
2. 再读 [src/free.c:L268-L292](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L268-L292)，找出 `mi_free_small` 在 `MI_PAGE_META_SMALL_IS_ALIGNED` 分支里连 `mi_validate_ptr_page_nonnull` 都不调、直接对齐下降取页的那两行。

**需要观察的现象 / 预期结果**：`mi_free` 反查 = 1 次 acquire load；`mi_free_small`（小页对齐构建）反查 = 0 次内存访问。若你的构建不满足 `MI_SMALL_PAGE_SIZE == MI_ARENA_SLICE_SIZE`，则两者等价。

**待本地验证**：具体宏取值可用 `cmake -B out/release && grep MI_PAGE_META_IS_ALIGNED out/release/CMakeCache.txt`（或看编译命令）确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `page0->self` 的加载要用 acquire 序，而后面 `xthread_id` 的读取用 relaxed 就够？

**答案**：`self` 指向的 `mi_page_t` 里包含会被并发修改的字段（如 `xthread_free`、标志位）。acquire load 保证我们看到 `self` 指针后，后续对该页字段的读取不会被打乱到 load 之前，从而读到页结构初始化完成后的状态。而 `xthread_id` 本身是 `_Atomic` 字段，单次读它的值用 relaxed 已能得到「某一时刻的快照」，free 分流逻辑对短暂过期是容忍的（最坏情况只是走了 generic 慢路径）。

**练习 2**：`mi_validate_ptr_page_nonnull` 在什么情况下返回 false？

**答案**：两种：`p==NULL`（对齐反查退化为 `page==NULL`，见 free.c:185 的注释）；以及 debug 构建下指针未对齐或 page map 查不到（打印 `invalid pointer` 错误后返回 false，[src/free.c:L176-L180](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L176-L180) 与 [L204-L210](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L204-L210)）。release 构建对无效指针基本不设防——这就是 u2-l1 讲过的 `mimalloc-override.h` 跨堆混用风险在 free 侧的体现。

### 4.2 一次异或完成四路分流：mi_free_nonnull

#### 4.2.1 概念说明

拿到页之后，free 要回答两个问题：

1. **这个页是我的还是别的线程的？**（决定能否直接写页的非原子字段）
2. **这个页有没有特殊标志？**（在满页队列？含对齐块的内部指针？）

朴素写法是两次比较：`tid == mi_page_thread_id(page) && mi_page_flags(page) == 0`。但 mimalloc 把两个问题压成了**一次异或加一次比较**。回忆 u3-l2：`page->xthread_id = 属主 tid | 页标志（低 2 位）`，而线程 id 的低 2 位保证为 0（本讲 4.2.3 会看到谁来保证）。于是：

\[ \text{xtid} \;=\; \text{mytid} \oplus (\text{owner\_tid} \,|\, \text{flags}) \]

- 若 `mytid == owner_tid` 且 `flags == 0`，则 `xtid == 0`（两问同时答「否」）。
- 若 `mytid == owner_tid` 且 `flags != 0`，则 `xtid` 的值就等于 `flags` 本身，即 \( 1 \le \text{xtid} \le 3 \)。
- 若 `mytid != owner_tid`，高位异或结果非零，`xtid` 至少 4；此时 `(xtid & 3)` 恰好还原 `flags` 是否为零。

一次 `xor` + 一次 `cmp`（外加把 `xtid` 留在寄存器里复用），四个分支全部判定完毕。

#### 4.2.2 核心流程

| 分支 | 判定条件 | 等价语义（代码注释原话） | 去向 |
| --- | --- | --- | --- |
| ① 本地·干净页 | `xtid == 0` | `tid == page 的属主 && flags == 0` | `mi_free_block_local`（内联快路径） |
| ② 本地·特殊页 | `xtid <= 3`（即 `0 < xtid <= MI_PAGE_FLAG_MASK`） | `tid == 属主 && flags != 0` | `mi_free_generic_local`（noinline） |
| ③ 跨线程·干净页 | `(xtid & 3) == 0`（且 `xtid != 0`） | `tid != 属主 && flags == 0` | `mi_free_block_mt`（内联，一次 CAS） |
| ④ 跨线程·特殊页 | 其余情形 | `tid != 属主 && flags != 0` | `mi_free_generic_mt`（noinline） |

「特殊页」指两种标志之一：`MI_PAGE_IN_FULL_QUEUE`（页满后被移入满页队列）或 `MI_PAGE_HAS_INTERIOR_POINTERS`（页里有带偏移的对齐分配，裸指针 `p` 不等于块起点，需要先换算回块起点，即 [src/free.c:L104-L114](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L104-L114) 的 `_mi_page_ptr_unalign`）。

#### 4.2.3 源码精读

四路分流的全部代码，值得逐行背下来：

[src/free.c:L223-L249](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L223-L249)
```c
static mi_decl_forceinline void mi_free_nonnull(void* p, mi_page_t* page, size_t* pblock_size, bool allow_collect)
{
  mi_assert_internal(p!=NULL && page!=NULL);
  if (pblock_size!=NULL) { *pblock_size = mi_page_block_size(page); }

  const mi_threadid_t ptid = mi_page_xthread_id(page);
  const mi_threadid_t xtid = (_mi_prim_thread_id() ^ ptid);
  if mi_likely(xtid == 0) {                        // 本地 + 无标志
    mi_block_t* const block = mi_validate_block_from_ptr(page,p);
    mi_free_block_local(page, block, false, true, false);
  }
  else if (xtid <= MI_PAGE_FLAG_MASK) {            // 本地 + 有标志
    mi_free_generic_local(page, p);
  }
  else if ((xtid & MI_PAGE_FLAG_MASK) == 0) {      // 跨线程 + 无标志
    mi_block_t* const block = mi_validate_block_from_ptr(page,p);
    mi_free_block_mt(page,block,false, allow_collect);
  }
  else {                                           // 跨线程 + 有标志
    mi_free_generic_mt(page, p, allow_collect);
  }
}
```
中文说明：`mi_page_xthread_id` 是一次 relaxed 原子读；`_mi_prim_thread_id()` 读 TLS 拿当前线程 id；二者异或得 `xtid` 后按上表四分流。注意函数签名上的 `mi_decl_forceinline` 和文件顶部 [src/free.c:L222](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L222) 的注释 `Fast path written carefully to prevent register spilling on the stack`——和 u4-l1 的 malloc 快路径同一套代码生成合同：不溢栈、不分调。

支撑这次异或的两个定义。其一，`xthread_id` 的读取与拆解：

[include/mimalloc/internal.h:L961-L972](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L961-L972)
```c
// Thread id of thread that owns this page (with flags in the bottom 2 bits)
static inline mi_threadid_t mi_page_xthread_id(const mi_page_t* page) {
  return mi_atomic_load_relaxed(&((mi_page_t*)page)->xthread_id);
}

// Plain thread id of the thread that owns this page
static inline mi_threadid_t mi_page_thread_id(const mi_page_t* page) {
  return (mi_page_xthread_id(page) & ~MI_PAGE_FLAG_MASK);
}

static inline mi_page_flags_t mi_page_flags(const mi_page_t* page) {
  return (mi_page_xthread_id(page) & MI_PAGE_FLAG_MASK);
}
```
中文说明：慢路径代码用后两个函数把字段拆开；唯独 free 快路径直接用未拆解的 `xthread_id` 异或，省掉掩码运算。

其二，线程 id 低 2 位为 0 的保证：

[include/mimalloc/prim-tls.h:L185-L190](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h#L185-L190)
```c
static inline mi_threadid_t _mi_prim_thread_id(void) {
  const mi_threadid_t tid = __mi_prim_thread_id();
  mi_assert_internal(tid > MI_THREADID_DETACHED);
  mi_assert_internal((tid & MI_PAGE_FLAG_MASK) == 0);  // bottom 2 bits are clear?
  return tid;
}
```
中文说明：线程 id 来自线程指针 / TLS 地址（见 [prim-tls.h:L166-L183](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h#L166-L183)），天然指针对齐，低 2 位为 0；debug 构建下有断言兜底。这是异或技巧成立的**前提不变式**。同理，写入侧 `mi_page_set_theap` 也有断言 `(tid & MI_PAGE_FLAG_MASK) == 0`（[internal.h:L1011-L1012](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1011-L1012)）。

标志常量与特殊线程 id 的权威定义在 types.h：

[include/mimalloc/types.h:L371-L386](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L371-L386)
```c
// The page flags are put in the bottom 2 bits of the thread_id (for a fast test in `mi_free`)
#define MI_PAGE_IN_FULL_QUEUE           MI_ZU(0x01)
#define MI_PAGE_HAS_INTERIOR_POINTERS   MI_ZU(0x02)
#define MI_PAGE_FLAG_MASK               MI_ZU(0x03)
...
#define MI_THREADID_ABANDONED           MI_ZU(0)
#define MI_THREADID_ABANDONED_MAPPED    (MI_ZU(1) << MI_PAGE_FLAG_BITS)   // = 4
#define MI_THREADID_DETACHED            (MI_ZU(2) << MI_PAGE_FLAG_BITS)   // = 8
```
中文说明：第一行注释直说了设计动机——标志放低 2 位就是为了 `mi_free` 里那次快速测试。特殊值 `0` 与 `4` 表示「无属主页」（遗弃页），它们不等于任何真实线程 id，所以遗弃页上的 free 会自然落入分支 ③④（跨线程侧），这个设计在 u5-l2 和 u6-l4 会被反复利用。

最后，分支 ② 的去向（分支 ④ 同理，见 [src/free.c:L156-L161](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L156-L161)）：

[src/free.c:L148-L153](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L148-L153)
```c
static void mi_decl_noinline mi_free_generic_local(mi_page_t* page, void* p) mi_attr_noexcept {
  mi_block_t* const block = (mi_page_has_interior_pointers(page) ? _mi_page_ptr_unalign(page, p)
                                                                 : mi_validate_block_from_ptr(page,p));
  const bool was_guarded = mi_block_check_unguard(page, block, p);
  mi_free_block_local(page, block, was_guarded, true /* track stats */, true /* check for a full page */);
}
```
中文说明：`mi_decl_noinline`——特殊页是少数情形，拆出去保住 `mi_free_nonnull` 本体的体积；换算块起点后最终**还是调用 `mi_free_block_local`**，但多传了 `check_full=true`（4.3 节会看到这个参数的用途）。

#### 4.2.4 代码实践

**实践目标**：验证自己对四分支判定条件的理解，方法是给四个分支做「真值表推演」。

**操作步骤**（纸面推演 + 源码对照）：

1. 假设页字段 `xthread_id` 的值为 `0x7f00`（属主 tid=`0x7f00`，flags=0），当前线程 id 为 `0x7f00` / `0x7f01` 不可能（低 2 位必为 0），改用 `0x7f04` / `0x9000` 两个候选；再假设 `xthread_id = 0x7f01`（flags=`IN_FULL_QUEUE`）时同样两个候选。共 4 种组合。
2. 对每种组合手算 `xtid = mytid ^ ptid`，查 4.2.2 的表判定落入哪个分支。
3. 对照 [src/free.c:L230-L248](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L230-L248) 每个分支上方注释里的等价语义，检查你的判定是否一致。

**需要观察的现象 / 预期结果**：`(0x7f00, 0x7f00)→xtid=0→分支①`；`(0x7f01, 0x7f00)→xtid=1→分支②`；`(0x7f00, 0x9000)→xtid=0xef00, 低2位0→分支③`；`(0x7f01, 0x9000)→xtid=0xef01→分支④`。

**待本地验证**：可在 debug 构建下用 gdb 在 `mi_free_nonnull` 断点处 `p/x xtid` 实测（需先 `break mi_free` 再 `step` 进入）。

#### 4.2.5 小练习与答案

**练习 1**：为什么分支 ① 传给 `mi_free_block_local` 的最后一个参数是 `false /* no need to check if the page is full */`，而 `mi_free_generic_local` 传 `true`？

**答案**：分支 ① 的进入条件 `xtid == 0` 已经蕴含 `flags == 0`，而「在满页队列」正是 flag `MI_PAGE_IN_FULL_QUEUE`（值 1）——所以分支 ① 的页**不可能**在满页队列，检查必然失败，编译期直接省掉。分支 ② 恰恰是 `flags != 0` 的本地页，其中可能包含满页队列标志，所以要传 `true` 让 `mi_free_block_local` 在释放后检查并触发 `_mi_page_unfull`。

**练习 2**：如果把线程 id 的低位断言去掉、允许线程 id 低 2 位非 0，这个分流会出什么 bug？

**答案**：异或结果的低 2 位将混入「两个 tid 低位差异」，`xtid <= 3` 不再能等价于「同属主且 flags 非 0」。例如属主 tid 低位为 1、我的 tid 低位为 0 时，即使 flags=0 也可能得出 `xtid==1` 误入分支 ②，或两个不同线程 tid 仅低位不同时 `xtid` 落在 1..3 内，把跨线程释放误判成本地释放——直接无锁写别人的页，数据竞争。所以 [prim-tls.h:L188](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h#L188) 的断言是分流正确性的前提。

**练习 3**：一个遗弃页（abandoned，`xthread_id` 为 0 或 4）上发生 free，会走哪个分支？

**答案**：走分支 ③（flags==0 时）或 ④（有标志时）。因为 0/4 不等于任何真实线程 id，`xtid = mytid ^ (0|4)` 高位必非零且不为 0 本身。也就是说遗弃页上的任何 free 都被当作「跨线程 free」处理，推入 `xthread_free` 链表并有机会顺手认领页的所有权——这是 u5-l2 的主题。

### 4.3 mi_free_block_local：三写两读与两个 unlikely 分支

#### 4.3.1 概念说明

本讲标题说的「本地 free 一次链表插入」就发生在这个函数里。它做的事可以概括为：**把块头插进 `page->local_free` 链表，把 `used` 减一**——对链表和计数的写全是普通（非原子）写。

「延迟（deferred）」在本函数有两个层面，正好对应标题里要求区分的内容：

1. **块层面的延迟**：释放的块进入的是 `local_free` 而不是 `free`。types.h 的字段注释直说 `local_free` 是 "list of deferred free blocks by this thread (migrates to free)"——刚释放的块**不能**立刻被 `mi_malloc` 分配出去，要等拥有线程在某次 free list 耗尽/管理时机把它搬迁进 `free`（u4-l2 讲过的 collect/extend）。这个延迟是故意的：它让「`free` 链表变空」成为拥有线程的确定性事件，支撑 u5-l3 将讲的心跳与延迟回收回调。
2. **页层面的延迟**：当本次释放使 `used` 降到 0（页完全变空），并不立即归还内存，而是进入**延迟退役**——设置 `retire_expire` 倒计时，约 16 个管理周期后仍空才真正释放，防止「释放→立刻又要分配」的抖动。

#### 4.3.2 核心流程

```text
mi_free_block_local(page, block, was_guarded, track_stats, check_full)
  │
  ├─ ① padding/双重释放检查（MI_PADDING 构建才有实质内容）
  ├─ ② 统计：mi_stat_free + mi_track_free_size（MI_STAT>0 才有内容）
  ├─ ③ debug 构建：把块内存涂成 MI_DEBUG_FREED 模式
  │
  ├─ ④ 实际释放（3 写 2 读，全部普通读写）：
  │      used      = page->used - 1        // 读 page->used，写 page->used
  │      block->next = page->local_free    // 读 page->local_free，写 block 头 8 字节
  │      page->local_free = block          // 写 page->local_free
  │
  ├─ ⑤ unlikely 分支 A：used==0 且 retire_expire==0
  │        → _mi_page_retire(page)         // 延迟退役，不是立即释放
  └─ ⑥ unlikely 分支 B：check_full 且页带 IN_FULL_QUEUE 标志
           → _mi_page_unfull(page)         // 从满页队列搬回常规 bin 队列
```

#### 4.3.3 源码精读

函数全文（release 构建下 ①②③ 全部编译为空，只剩 ④ 和两个 unlikely 分支）：

[src/free.c:L26-L57](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L26-L57)
```c
// regular free of a (thread local) block pointer
// fast path written carefully to prevent spilling on the stack
static inline void mi_free_block_local(mi_page_t* page, mi_block_t* block, bool was_guarded, bool track_stats, bool check_full)
{
  // checks
  size_t usable_size;
  if mi_unlikely(!mi_check_padding_on_free(page, block, was_guarded, &usable_size)) return;
  ...
  // actual free: push on the local free list
  const mi_used_t used = page->used - 1;
  mi_block_set_next(page, block, page->local_free);
  page->used = used;
  page->local_free = block;
  if mi_unlikely(used==0) {
    if (page->retire_expire==0) { // no need to re-retire retired pages ...
      _mi_page_retire(page);
    }
  }
  else if mi_unlikely(check_full && mi_page_is_in_full(page)) {
    _mi_page_unfull(page);
  }
}
```
中文说明：注意 44 行注释 `actual free: push on the local free list` 之后的三行就是释放的全部本质——`used` 减一、块头插 `local_free`。没有锁、没有原子 RMW、没有函数调用（`mi_block_set_next` 在非 `MI_ENCODE_FREELIST` 构建下就是一次普通指针写入，见 [internal.h:L1286-L1293](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1286-L1293)）。

块结构本身——空闲块的「头插」插在哪里：

[include/mimalloc/types.h:L365-L368](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L365-L368)
```c
// free lists contain blocks
typedef struct mi_block_s {
  mi_encoded_t next;
} mi_block_t;
```
中文说明：`mi_block_t` 只有一个 8 字节字段。分配出去时这 8 字节是用户数据的前 8 字节；释放后立刻被复用为链表指针（u3-l2 讲的「无每块头」设计）。所以「头插」只需写用户区域的头 8 字节。

被写的两个页字段在页结构里的位置与官方注释：

[include/mimalloc/types.h:L429-L432](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L429-L432)
```c
  _Atomic(mi_threadid_t)    xthread_id;  // thread this page belongs to. (= theap->thread_id ... | page_flags`)
  mi_block_t*               free;        // list of available free blocks (`malloc` allocates from this list)
  mi_used_t                 used;        // number of blocks in use (including blocks in `thread_free`)
  mi_block_t*               local_free;  // list of deferred free blocks by this thread (migrates to `free`)
```
中文说明：`free` 与 `local_free` 都是**普通指针字段**（非 `_Atomic`）——types.h 的结构注释（[types.h:L416-L418](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L416-L418)）明确：非原子字段只允许**拥有页的线程在持有所有权时**访问。这就是本地 free 敢用普通写的契约依据（4.4 节展开）。

unlikely 分支 A 的去向——延迟退役：

[src/page.c:L414-L415](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L414-L415) 定义 `MI_RETIRE_CYCLES (16)`，[src/page.c:L424-L457](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L424-L457) 的 `_mi_page_retire`：
```c
void _mi_page_retire(mi_page_t* page) mi_attr_noexcept {
  ...
  if (page->retire_expire!=0) return;  // already retired, just keep it retired
  ...
  if mi_likely( pq->count <= MI_RETIRE_MAX_PAGES && !mi_page_queue_is_special(pq)) {
    if (pq->count==1 || bsize < MI_SMALL_SIZE_MAX) {
      ...
      page->retire_expire = (bsize <= MI_SMALL_MAX_OBJ_SIZE ? MI_RETIRE_CYCLES : MI_RETIRE_CYCLES/4);
      ...
      return; // don't free after all
    }
  }
  _mi_page_free(page, pq);
}
```
中文说明：进入分支 A 后**多数情况并不释放页**，只是给 `retire_expire` 赋 16（小对象）或 4（较大对象）个管理周期的倒计时并返回。free.c:50 的 `retire_expire==0` 检查是为了「在一个已空的页上反复分配/释放同一个块」时不重复挂退役。这也解释了 free 快路径的条件写法：`if (used==0) { if (retire_expire==0) retire; }`——两次比较都几乎总为假。

unlikely 分支 B 的去向——满页出队：

[src/page.c:L359-L372](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L359-L372)
```c
// Move a page from the full list back to a regular list (called from thread-local mi_free)
void _mi_page_unfull(mi_page_t* page) {
  ...
  mi_theap_t* theap = mi_page_theap(page);
  mi_page_queue_t* pqfull = &theap->pages[MI_BIN_FULL];
  mi_page_set_in_full(page, false); // to get the right queue
  mi_page_queue_t* pq = mi_theap_page_queue_of(theap, page);
  mi_page_set_in_full(page, true);
  mi_page_queue_enqueue_from_full(pq, pqfull, page);
}
```
中文说明：页满时被移入 `MI_BIN_FULL` 特殊队列（u3-l3 讲过末尾的特殊队列）；一旦有线程在它上面 free 了一个块（说明它不再满），就把它搬回所属 bin 的常规队列，重新参与分配候选。注释点明它正是被 thread-local 的 `mi_free` 调用的——但只有 `mi_free_generic_local`（4.2 的分支 ②）会以 `check_full=true` 到达这里。

#### 4.3.4 代码实践

**实践目标**：观察分支 A（延迟退役）确实发生且被计数。

**操作步骤**：

1. 构建 debug 版（debug 构建默认 `MI_STAT=2`，见 [types.h:L72-L86](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L72-L86)）：
   ```bash
   cmake -B out/debug -DCMAKE_BUILD_TYPE=Debug && cmake --build out/debug
   ```
2. 写一个程序：循环 1000 次「分配 512 个 64 字节块 → 全部释放」，让页反复整页变空。
3. 以 `MIMALLOC_SHOW_STATS=1` 运行，在输出的 process 段找 `retire` 计数行。

**需要观察的现象**：统计中 `retire` 计数非零（该行由 [src/stats.c:L399](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L399) 打印），且程序结束时 `pages` 计数不随循环次数增长——说明空页被退役机制控制住了，没有泄漏式增长。

**预期结果**：`retire` 计数≥1；若把循环改成「只分配不释放」，则 `retire` 为 0 且 blocks 段出现 `not all freed`（u1-l4 讲过的标记）。

**待本地验证**：具体数值随平台与循环细节不同，重点看 `retire > 0` 与页数是否稳定。

#### 4.3.5 小练习与答案

**练习 1**：release 默认构建（`MI_PADDING=0`、`MI_STAT=0`、`MI_ENCODE_FREELIST=0`）下，从 `mi_free(p)` 进入到 `mi_free_block_local` 返回，总共几次内存访问？分别是什么？

**答案**：约 9 次——读 ① `page0->self`（acquire，反查页）、读 ② `page->xthread_id`（relaxed）、读 ③ TLS 取线程 id、读 ④ `page->block_size`（`mi_check_padding_on_free` 无 padding 版本里算 usable size 用）、读 ⑤ `page->used`、读 ⑥ `page->local_free`；写 ① `block->next`、写 ② `page->used`、写 ③ `page->local_free`。其中没有一次是 RMW/CAS，两次原子 load 在 x86 上都是普通 `mov`。与 u4-l1 数出的 malloc 快路径 8 次访存基本对称——malloc 与 free 同样便宜，这是 mimalloc 的设计目标之一。

**练习 2**：`used` 字段的注释说它 "including blocks in `thread_free`"。既然如此，本地 free 把 `used` 减一为什么是对的？

**答案**：`used` 统计的是「不在 `free`/`local_free` 链上的块数」，即「已分配出去的块数（含被其他线程释放进 `xthread_free` 但尚未被拥有线程收割的）」。本地 free 的块确实曾经分配给了本线程，此刻从「已分配」进入 `local_free` 链，所以拥有线程直接减一。而跨线程 free **不动** `used`（u5-l2 会看到 `mi_free_block_mt` 只 CAS `xthread_free`），等拥有线程收割 `xthread_free` 链时才把这一批一起减掉。这条记账规则正是 u3-l2 不变式 \( \text{capacity} = \text{used} + |\text{free}| + |\text{local\_free}| \) 的日常维护方式。

**练习 3**：为什么分支 A 里 `used==0` 就敢断定页完全空闲（`mi_page_all_free`），不需要检查 `xthread_free`？

**答案**：`mi_page_all_free` 的实现就是一行 `page->used == 0`（[internal.h:L898-L901](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L898-L901)），不检查 `xthread_free`——这依然是对的，关键在 `used` 的记账口径：`used` 统计「已分配出去的块，**包括**被其他线程释放进 `xthread_free` 但尚未收割的块」（[types.h:L431](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L431)）。`mi_free_block_mt` 只 CAS 链表、不减 `used`；要等拥有线程收割时才把整批一起减掉。因此只要 `xthread_free` 里还有块，`used` 就必然大于 0；反过来 `used==0` 时三条链（`free`/`local_free`/`xthread_free`）之外不可能还有存活块，由不变式 \( \text{capacity} = \text{used} + |\text{free}| + |\text{local\_free}| \) 可知此时全部块都在本地两条链上，页确实全空。

### 4.4 为什么本地 free 不需要任何锁：所有权不变式

#### 4.4.1 概念说明

「无原子写也能并发安全」不是巧合，而是一条被显式写进 types.h 注释的契约：**页的非原子字段（`free`、`local_free`、`used` 等）只有拥有该页的 theap 线程可以访问；其他线程一律只能碰 `_Atomic` 字段（`xthread_id`、`xthread_free`）**。free 快路径的 XOR 分流正是这条契约的执法机构：分支 ①② 证明「我是属主」后才写普通字段；分支 ③④ 只走原子路径。本模块把这条契约的原文与所有权位的机制串起来，为 u5-l2 铺路。

#### 4.4.2 核心流程

```text
谁可以写 page->free / local_free / used ？   只有属主线程（且持有所有权位）
其他线程想释放块怎么办？                    CAS 头插 page->xthread_free（原子字段）
无主页（遗弃页）怎么重新有主？              跨线程 free 的一次 CAS 同时完成「推块 + 认领所有权位」
属主线程何时收割 xthread_free？              free list 耗尽 / 管理周期（u5-l2 详述）
```

#### 4.4.3 源码精读

契约原文（页结构上方的注释块）：

[include/mimalloc/types.h:L415-L418](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L415-L418)
```c
// Notes:
// - Non-atomic fields can only be accessed if having _ownership_ (low bit of `xthread_free` is 1).
//   Combining the `thread_free` list with an ownership bit allows a concurrent `free` to atomically
//   free an object and (re)claim ownership if the page was abandoned.
```
中文说明：所有权 = `xthread_free` 的最低位为 1。非原子字段必须在持有所有权时才能访问；所有权位与跨线程链表合用一个原子字，使得「释放一个块」与「认领一个无主页」能在同一次 CAS 里完成。

`xthread_free` 字段与所有权位辅助函数：

[include/mimalloc/types.h:L388-L393](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L388-L393)
```c
// Thread free list.
// Points to a list of blocks that are freed by other threads.
// The least-bit is set if the page is owned by the current thread. (`mi_page_is_owned`).
// Ownership is required before we can read any non-atomic fields in the page.
typedef uintptr_t mi_thread_free_t;
```

[include/mimalloc/internal.h:L1089-L1097](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1089-L1097)
```c
static inline mi_block_t* mi_tf_block(mi_thread_free_t tf) {
  return (mi_block_t*)(tf & ~1);
}
static inline bool mi_tf_is_owned(mi_thread_free_t tf) {
  return ((tf & 1) == 1);
}
static inline mi_thread_free_t mi_tf_create(mi_block_t* block, bool owned) {
  return (mi_thread_free_t)((uintptr_t)block | (owned ? 1 : 0));
}
```
中文说明：`mi_thread_free_t` 是「链表头指针 | 所有权位」的打包值，三个辅助函数负责拆包/打包。本讲的本地路径完全不触碰它；但理解这个位的存在，才能理解本地路径为什么「有资格」忽略它——分支 ①② 已经由 `xthread_id` 证明属主身份。

对照：跨线程路径确实只有一次 CAS（本讲只看形状，细节留 u5-l2）：

[src/free.c:L80-L87](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L80-L87)
```c
  // push atomically on the page thread free list
  mi_thread_free_t tf_new;
  mi_thread_free_t tf_old = mi_atomic_load_relaxed(&page->xthread_free);
  do {
    mi_block_set_next(page, block, mi_tf_block(tf_old));
    const bool new_owned = (allow_collect ? true : mi_tf_is_owned(tf_old));
    tf_new = mi_tf_create(block, new_owned);
  } while (!mi_atomic_cas_weak_acq_rel(&page->xthread_free, &tf_old, tf_new));
```
中文说明：与本地路径的三行普通写相比，跨线程路径把同样的「头插」包进 `mi_atomic_cas_weak_acq_rel` 循环——这是整个 free 代码里唯一一处 CAS。本地/跨线程的代价差就浓缩在这两段代码的对照里。

#### 4.4.4 代码实践

**实践目标**：用反汇编确认「本地 free 快路径无 `lock` 前缀指令」。

**操作步骤**（源码阅读 + 工具验证）：

1. release 构建并生成汇编（u4-l1 用过的开关）：
   ```bash
   cmake -B out/release -DMI_SEE_ASM=ON && cmake --build out/release
   ```
   或直接 `objdump -d out/release/libmimalloc.so | less`。
2. 在汇编中定位 `mi_free`（导出符号一定能找到），浏览其函数体。
3. 搜索 `lock` 前缀：`objdump -d out/release/libmimalloc.so | awk '/<mi_free>:/,/^$/' | grep -c lock`。

**需要观察的现象 / 预期结果**：`mi_free` 的快路径指令序列中不含 `lock` 前缀（CAS 只会出现在慢路径函数如 `mi_free_block_mt` 展开处）。`mi_free` 与 `mi_malloc` 的主体都应只有 `mov/cmp/jmp` 与一次 TLS 读取（fs/gs 段前缀）。

**待本地验证**：不同编译器版本指令序列有差异，判据只有一条——快路径无 `lock`。

#### 4.4.5 小练习与答案

**练习 1**：既然本地 free 不碰 `xthread_free`，属主线程怎么知道有别的线程释放过块？

**答案**：属主线程不会在 free 时知道。收割发生在分配侧/管理时机：当 `free` 链耗尽、`mi_theap_malloc_generic` 走慢路径时（u4-l2），`_mi_page_free_collect` 会原子读取 `xthread_free`，把整条跨线程链搬进 `local_free` 并修正 `used`。这正是「分配零原子、释放近零原子」的代价转移设计。

**练习 2**：`mi_free_block_local` 里 `used` 的读-减-写不是原子的，如果同时另一个线程在 CAS `xthread_free`，会不会破坏 `used`？

**答案**：不会，因为两边写的**不是同一个字段**：`used` 是普通字段，只有属主线程写（分支 ①② 已验证身份）；其他线程只 CAS 原子字段 `xthread_free`，根本不读不写 `used`。字段级的写者唯一性代替了锁——这是「所有权」契约的核心收益。

**练习 3**：`mi_free_nonnull` 的 `allow_collect` 参数为什么在 `mi_free` 里恒为 `true`，而 `_mi_free_subproc_safe`（[src/free.c:L299-L304](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L299-L304)）传 `false`？

**答案**：`allow_collect` 只影响分支 ③④ 中「发现页是无主页时是否尝试认领并收割」（[src/free.c:L90-L96](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L90-L96)）。普通 `mi_free` 允许顺手做这件事；而 `_mi_free_subproc_safe` 服务于子进程（subproc，u7-l4）销毁场景，此时页可能属于正在被销毁的其他 subproc，认领/收割会踩到正在回收的内存，所以禁止。这体现了同一快路径在不同生命周期场景下的安全阀门。

## 5. 综合实践

把本讲内容串起来的任务：**给四个分支写注释 + 用调试器证明本地 free 无 CAS**。

### 步骤一：给 mi_free_nonnull 的四个分支各写一行注释

在你**自己克隆的仓库副本**（不要改共享环境）中打开 `src/free.c`，找到 [L230-L248](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L230-L248) 的四个分支，在每个 `if` 上方用中文写一行触发条件。参考写法（内容即 4.2.2 的表）：

```c
// 示例代码：读者自己副本中的注释，非仓库原有内容
if mi_likely(xtid == 0) {
  // 触发：当前线程 == 页属主 且 页标志为 0（不在满页队列、无对齐内部指针）→ 本地快路径
  ...
}
else if (xtid <= MI_PAGE_FLAG_MASK) {
  // 触发：当前线程 == 页属主 但 页标志非 0（IN_FULL_QUEUE 或 HAS_INTERIOR_POINTERS）→ 本地慢路径
  ...
}
else if ((xtid & MI_PAGE_FLAG_MASK) == 0) {
  // 触发：当前线程 != 页属主（含遗弃页）且 页标志为 0 → 跨线程快路径，一次 CAS 推入 xthread_free
  ...
}
else {
  // 触发：当前线程 != 页属主 且 页标志非 0 → 跨线程慢路径（需先换算对齐块起点/处理满页）
  ...
}
```

### 步骤二：单线程 free 一个块，调试器确认走本地路径且无原子 CAS

1. 构建 debug 静态库并写测试程序 `free_once.c`：
   ```bash
   cmake -B out/debug -DCMAKE_BUILD_TYPE=Debug && cmake --build out/debug --target mimalloc-static
   ```
   ```c
   /* 示例代码：free_once.c */
   #include <mimalloc.h>
   int main(void) {
     void* p = mi_malloc(32);
     mi_free(p);    /* 在这一行设断点 */
     return 0;
   }
   ```
   编译（库名带 `-debug` 后缀，见 u1-l2）：
   ```bash
   gcc -g -O0 free_once.c out/debug/libmimalloc-debug.a -lpthread -lm -ldl -o free_once
   ```
2. 用 gdb 单步：
   ```bash
   gdb ./free_once
   (gdb) break mi_free
   (gdb) run
   (gdb) step        # 进入 mi_free_nonnull（forceinline，会直接展开）
   (gdb) break mi_free_block_local   # static inline；若因内联无法命中，就一直 step 观察调用序列
   (gdb) continue
   ```
3. 验证无 CAS：`disassemble` 你所停的函数区域（或 `disassemble /r mi_free`），检查指令里**没有 `lock` 前缀**；`mi_free_block_local` 展开中应看到对 `local_free` 和 `used` 的普通 `mov` 写入。

### 需要观察的现象与预期结果

- 单线程场景下，`mi_free` 的调用序列应依次经过：页反查（对齐下降 + `self` 读取）→ `xtid` 计算 → `mi_free_block_local` → 三次普通写入返回；全程不进入 `mi_free_block_mt`。
- 反汇编中没有 `lock` 前缀指令；debug 构建因 `MI_PADDING=1`、`MI_ENCODE_FREELIST=1`（[types.h:L96-L110](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L96-L110)）会多出 padding 校验与异或编码指令，但仍无 `lock`。
- 额外对照实验：把 `free_once.c` 改成两个线程（主线程 `mi_malloc`，子线程 `mi_free`），重复 gdb 步骤，这次应进入 `mi_free_block_mt` 并在反汇编中看到 `lock cmpxchg`。

**待本地验证**：debug 构建优化等级与内联程度在不同编译器下有差异；若 `break mi_free_block_local` 不命中，属正常现象（已被内联），改用连续 `step` + `disassemble` 完成同样的观察即可。

## 6. 本讲小结

- `mi_free` 的结构是「反查页 → XOR 四路分流 → 入链」：反查在默认构建下是纯算术加一次 `self` 的 acquire load，不查表。
- `xthread_id = 属主 tid | 页标志(低2位)`，配合低 2 位为 0 的线程 id，`xtid = mytid ^ ptid` 一次异或同时判定「是否属主」与「标志是否为零」，划分出本地/跨线程 × 干净页/特殊页四个分支。
- 本地快路径 `mi_free_block_local` 的本质是三行普通写：`used` 减一、块头插 `local_free`、更新链表头——release 下约 9 次访存、零原子 RMW，与 malloc 快路径对称。
- 「延迟」有两层：块进 `local_free` 暂不可分配（迁移动机由 u4-l2 的 collect 承接）；页全空触发的是 `retire_expire` 倒计时的延迟退役（16 或 4 个管理周期），而非立即归还。
- 无锁安全的根基是所有权契约：非原子字段只有属主可写，其他线程只能 CAS 原子字段 `xthread_free`（其低位是所有权位）——这为 u5-l2 的跨线程释放铺路。

## 7. 下一步学习建议

下一讲 **u5-l2 跨线程 free：一次 CAS 推入 thread_free 链表** 将精读本讲只「看了一眼形状」的 `mi_free_block_mt`（[src/free.c:L63-L97](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L63-L97)）：CAS 循环为什么不需要 ABA 防护、所有权位如何在同一次 CAS 里认领无主页（`mi_free_try_collect_mt`，[src/free.c:L480-L515](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L480-L515)）、以及拥有线程在 page.c 里的收割时机。建议先复习 u3-l2 的三条链表不变式与 u8-l1 将用到的原子内存序基础（relaxed/acquire/release 的语义可先翻阅 [include/mimalloc/atomic.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/atomic.h) 建立印象）。
