# u4-l3 大对象与巨大对象：mi_huge_page_alloc 与直接 OS 分配

## 1. 本讲目标

前两讲我们跟完了 `mi_malloc` 的快路径与慢路径，但有一个分岔口一直被「绕过」了：`mi_find_page` 里那句 `mi_page_queue_is_huge(pq)` 判断为真时，请求不再进入任何 size class 页队列，而是走 `mi_huge_page_alloc`——**一个对象独占一个页**。本讲就把这条支路读完。学完你应该能：

1. 说出 small/medium/large/huge 四档对象的精确尺寸边界，以及超过 `MI_LARGE_MAX_OBJ_SIZE`（512 KiB）后为什么不再适合「一页多块」。
2. 跟踪 `mi_huge_page_alloc` → `mi_page_fresh_alloc` → `_mi_arenas_page_alloc` → `mi_arenas_page_singleton_alloc` 这条调用链，说明单例页（singleton page）的 `reserved/capacity/used` 三个计数如何退化成 1。
3. 解释 `mi_option_arena_max_object_size` 选项（单位是 KiB！）如何决定一块大内存来自 arena 还是直接来自 OS，以及两条路径在统计输出上的可见差异。

## 2. 前置知识

本讲默认你已完成 u3 系列（页、bin、三条 free list）与 u4-l1/u4-l2（快慢路径）。补充三个直觉：

- **为什么要分档**：mimalloc 页是「只装一种块规格」的定长容器（u3-l2）。块越小，一页装得越多，free list 分片的收益越大；块越大，一页只能装一两块，分片优势消失，管理开销反而占比更高。所以存在一条「性价比分界线」。
- **arena 与 slice**：arena 是进程共享的大块预留内存（默认按 64 KiB 一片的 slice 用原子位图批发，u3-l4/u6-l3 会展开）。页的内存 normally 从 arena 切出来；arena 放不下的（太大、或对齐要求太怪），就直接向 OS 要。每个页用 `mi_memid_t` 记着自己的「产地」，释放时按产地原路归还。
- **单例页（singleton page）**：指 `reserved == 1` 的页——整个页只有一个块。它可能因为「块太大」，也可能因为「对齐要求太大」（对齐超过 64 KiB 时无法在 arena 里靠超额分配凑出来）。本讲聚焦前者，后者在 u4-l4 对齐分配中展开。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
| --- | --- |
| [src/page.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c) | `mi_huge_page_alloc`、`mi_find_page` 的 huge 分岔、`_mi_page_init`/`mi_page_extend_free`、`_mi_page_retire`/`_mi_page_free` |
| [src/arena.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c) | 页的批发入口 `_mi_arenas_page_alloc`、单例页 `mi_arenas_page_singleton_alloc`、arena→OS 回退 `mi_arenas_page_alloc_fresh_area`、上限函数 `mi_arena_max_object_size` |
| [include/mimalloc/types.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h) | 全部尺寸常量（`MI_SMALL_MAX_OBJ_SIZE` 等）、`mi_page_kind_t`、`mi_page_t`、`mi_memid_t` |
| src/page-queue.c | `mi_bin` 的最后一档判定、huge 队列的识别谓词（被 page.c 包含） |
| src/options.c / src/os.c | `arena_max_object_size` 选项的定义与 KiB 语义；`_mi_os_good_alloc_size` 的阶梯取整 |
| include/mimalloc/internal.h | `mi_page_is_singleton` / `mi_page_is_huge` / `mi_page_is_full` 等内联谓词 |

## 4. 核心概念与源码讲解

### 4.1 尺寸分档：huge 是 `_mi_bin` 的最后一档

#### 4.1.1 概念说明

u3-l3 讲过 `_mi_bin` 把请求尺寸映射到 75 条页队列之一。这个函数有一个「逃逸口」：当请求的字数超过 `MI_LARGE_MAX_OBJ_WSIZE` 时直接返回 `MI_BIN_HUGE`（73），不再查尺寸表。也就是说，**huge 不是第 74 个尺寸规格，而是「不属于任何规格」的标记**。落进这个标记的请求，绕过全部 size class 机制，为一个对象单独造一个页。

四档边界全部由三个页尺寸常量推导（64 位平台，`MI_ENABLE_LARGE_PAGES=1` 时）：

| 档位 | 页尺寸 | 对象上限 | 定义处 |
| --- | --- | --- | --- |
| small | 64 KiB | 10 KiB（`(64KiB−4KiB)/6`） | [types.h:470](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L470) |
| medium | 512 KiB | 约 84 KiB（`(512KiB−4KiB)/6`） | [types.h:472](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L472) |
| large | 4 MiB | **512 KiB（`4MiB/8`）** | [types.h:473](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L473) |
| huge | 单例页 | 无（直到 `PTRDIFF_MAX`） | [types.h:499-505](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L499-L505) |

注意 512 KiB 这个边界**恰好取等也还算 large**：`mi_bin(524288)` 的字数是 65536，判定条件是「严格大于」才判 huge，代入公式可得它落在 bin 60——正是 `MI_MAX_SINGLETON_BIN` 在 512 KiB 档的取值（见 [types.h:484-493](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L484-L493) 的静态不变式）。所以 4 MiB 的 large 页刚好装 8 个 512 KiB 块；再大 1 字节就跨入 huge。

另外，识别「huge 队列」不靠下标比较，而靠一个不可能的哨兵值：huge 队列的 `block_size` 被设成 `MI_LARGE_MAX_OBJ_SIZE + sizeof(uintptr_t)`（full 队列再加一个字），见 [page-queue.c:40-50](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-queue.c#L40-L50)。

#### 4.1.2 核心流程

```text
mi_malloc(n)
  └─ _mi_malloc_generic                      # 快路径弹空或 n > 1KiB
       └─ mi_find_page(theap, n, huge_alignment=0)
            pq = &theap->pages[ mi_bin(n) ]
            if mi_bin(n) == MI_BIN_HUGE:     # 字数 > 65536（即 n > 512KiB）
                 pq 实际取 huge 队列          # block_size 是哨兵值
                 mi_page_queue_is_huge(pq) == true
                 → mi_huge_page_alloc         # 本讲主线
            else:
                 mi_page_queue_find_free      # u4-l2 的 next-fit 扫描
```

判定的唯一源头是 [page-queue.c:79-81](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-queue.c#L79-L81)：`wsize > MI_LARGE_MAX_OBJ_WSIZE` 时返回 `MI_BIN_HUGE`。debug 构建里每块额外带 8 字节 padding（u3-l3），所以 `mi_malloc(512*1024)` 在 release 下是 large、在 debug 下会加 8 字节而**跨过边界变成 huge**——做边界实验时务必记住这一点。

#### 4.1.3 源码精读

分岔点在 `mi_find_page`（[src/page.c:950-975](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L950-L975)）：

```c
mi_page_queue_t* pq = mi_page_queue(theap, (huge_alignment > 0 ? MI_LARGE_MAX_OBJ_SIZE+1 : size));
mi_page_t* page;
// huge allocation?
if mi_unlikely(mi_page_queue_is_huge(pq) || req_size > MI_MAX_ALLOC_SIZE) {
  page = mi_huge_page_alloc(theap,size,huge_alignment,pq);
}
```

这段先按尺寸（或超对齐请求，见 u4-l4）取出队列，再用 4.1.1 的哨兵判定识别 huge。`mi_page_queue` 本身只是 `&theap->pages[_mi_bin(size)]`（[internal.h:945-949](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L945-L949)），而 `_mi_bin` 的核心是纯位运算（[src/page-queue.c:86-94](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-queue.c#L86-L94)）：

```c
wsize--;
const size_t b = (MI_SIZE_BITS - 1 - mi_clz(wsize));       // 最高位位置
const size_t bin = ((b << 2) + ((wsize >> (b - 2)) & 0x03)) - 3;  // 每倍频程 4 档
```

尺寸常量一侧，[types.h:227-229](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L227-L229) 定义了三档页大小，[types.h:469](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L469) 的注释点明设计意图：**各档上限保证页内浪费不超过约 12.5%**。页种类的枚举则把「单例」正式建模为第四种页（[types.h:499-505](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L499-L505)）：

```c
typedef enum mi_page_kind_e {
  MI_PAGE_SMALL,      // 64KiB 页
  MI_PAGE_MEDIUM,     // 512KiB 页
  MI_PAGE_LARGE,      // 4MiB 页
  MI_PAGE_SINGLETON   // 页内只有一个块：> MI_LARGE_MAX_OBJ_SIZE，或对齐 > 64KiB
} mi_page_kind_t;
```

#### 4.1.4 代码实践

**实践目标**：亲手找到 huge 的边界，并发现「huge 对象的实际块大小不等于 `mi_good_size`」。

1. 写一个小探针程序（**示例代码**，非项目原有）：

   ```c
   #include <mimalloc.h>
   #include <stdio.h>

   int main(void) {
     size_t sizes[] = { 500*1024, 512*1024, 512*1024+16, 600*1024, 3*1024*1024, 33*1024*1024 };
     for (int i = 0; i < 6; i++) {
       size_t n = sizes[i];
       void* p = mi_malloc(n);
       printf("req %8zu  good %8zu  usable %8zu\n", n, mi_good_size(n), mi_usable_size(p));
       mi_free(p);
     }
     return 0;
   }
   ```

2. 用 u1-l2 的方式构建 release 与 debug 两个版本，把程序分别链接两种库运行（也可以直接 `MIMALLOC_SHOW_STATS=1` 观察 malloc 段是否出现 `huge` 行）。
3. 重点对比 release 与 debug 下 `512*1024` 这一行的 `usable`（debug 因 +8 字节 padding 会跳档）。

**需要观察的现象**：请求一旦超过 512 KiB，`mi_usable_size` 返回值呈阶梯状（600 KiB→640 KiB、33 MiB→36 MiB），且**大于** `mi_good_size`（后者只对齐到 OS 页，见 [page-queue.c:114-123](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-queue.c#L114-L123)）；`mi_good_size` 本身在边界处从「bin 规格值」跳变为「4 KiB 对齐值」。

**预期结果**：release 下 512 KiB 请求 `usable == 524288` 且不产生 huge 统计；debug 下同一请求落入 huge。具体数值**待本地验证**。

#### 4.1.5 小练习与答案

1. **为什么 huge 的判定用「字数严格大于」而不是「大于等于」？**
   答：`MI_LARGE_MAX_OBJ_SIZE = 4MiB/8` 本身被设计成能整除出整数块（8 块/页），bin 表里专门有 bin 60 这一档容纳它（`MI_MAX_SINGLETON_BIN` 不变式）；只有超过它的尺寸才无法再装进 4 MiB 页的经济模型。
2. **`mi_page_queue_is_huge` 为什么用 `block_size` 哨兵而不是记一个 flag？**
   答：huge 队列里各页的 `block_size` 各不相同（每个 huge 页一个规格），无法像普通队列那样用 `block_size` 匹配；用一个大于任何合法规格的不可能值当身份标签，可以在快路径上用一次比较完成判定，无需额外字段。
3. **请求 512 KiB 与 512 KiB+1 在数据结构层面的差别是什么？**
   答：前者进 bin 60 的普通页队列，与最多 7 个同规格块共享一个 4 MiB 页、参与 retire/abandon 生命周期；后者走 `mi_huge_page_alloc`，独占一个专用页，分配即满、释放即还。

### 4.2 `mi_huge_page_alloc`：一个对象一个页的诞生

#### 4.2.1 概念说明

huge 分配的核心函数出奇地短（[src/page.c:920-946](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L920-L946)），因为它只做三件事：把尺寸取整成「好的」块大小、走通用的 `mi_page_fresh_alloc` 要一个新页、记两个统计。真正的工作在 arena 侧（4.4 节）。

「好的块大小」由 `_mi_os_good_alloc_size` 决定，它是一张**阶梯对齐表**：越大对齐越粗。这直接决定内部碎片上界——对 \(s \ge 32\mathrm{MiB}\) 的请求，浪费 \(< 4\mathrm{MiB}\)，即比例小于

\[
\frac{4\mathrm{MiB}}{32\mathrm{MiB}} = 12.5\%,
\]

与 size class「约 12.5% 最坏碎片」的设计上界（[types.h:469](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L469) 注释）保持一致。同样可验证 \(2\mathrm{MiB}+1\) 会被 256 KiB 档对齐到 \(2.25\mathrm{MiB}\)，浪费也恰约 12.5%。

#### 4.2.2 核心流程

```text
mi_huge_page_alloc(theap, size, page_alignment, pq)
  block_size = _mi_os_good_alloc_size(size)        # 阶梯取整：≥32MiB 按 4MiB，8~32MiB 按 1MiB，
                                                   # 2~8MiB 按 256KiB，512KiB~2MiB 按 64KiB
  page = mi_page_fresh_alloc(theap, pq, block_size, page_alignment)
      └─ _mi_arenas_page_alloc(theap, block_size, alignment)   # 4.4 节
      └─ 若拿到的是遗弃页 → _mi_theap_page_reclaim 再用
  统计：malloc_huge += block_size; malloc_huge_count += 1
  返回 page（此刻 free list 里恰有一个块）
随后 _mi_page_malloc_zero 从该页弹出唯一的块 → used=1 → 页立刻变满
```

#### 4.2.3 源码精读

huge 分配本体（[src/page.c:923-946](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L923-L946)）：

```c
static mi_page_t* mi_huge_page_alloc(mi_theap_t* theap, size_t size, size_t page_alignment, mi_page_queue_t* pq) {
  const size_t block_size = _mi_os_good_alloc_size(size);
  ...
  mi_page_t* page = mi_page_fresh_alloc(theap, pq, block_size, page_alignment);
  if (page != NULL) {
    mi_assert_internal(mi_page_is_huge(page));
    mi_assert_internal(mi_page_is_singleton(page));
    mi_theap_stat_increase(theap, malloc_huge, mi_page_block_size(page));
    mi_theap_stat_counter_increase(theap, malloc_huge_count, 1);
  }
  return page;
}
```

两点值得注意：断言要求返回的页既是 huge 又是 singleton——**页大小超过 512 KiB 的页必然 `reserved==1`**，这是由 arena 侧按 64 KiB slice 取整保证的（4.3 节）；统计记的是 `block_size`（取整后的整页大小），释放侧 `mi_stat_free` 用同一个口径扣回（[src/free.c:771-774](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L771-L774) 注释明言「match stat in page.c:mi_huge_page_alloc」），两侧配平，`huge` 行的 `current` 列才能正确归零。

阶梯取整表（[src/os.c:88-97](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c#L88-L97)）：

```c
if (size < 512*MI_KiB) align_size = _mi_os_page_size();
else if (size < 2*MI_MiB) align_size = 64*MI_KiB;
else if (size < 8*MI_MiB) align_size = 256*MI_KiB;
else if (size < 32*MI_MiB) align_size = 1*MI_MiB;
else align_size = 4*MI_MiB;
```

新页获取复用了 u4-l2 见过的 `mi_page_fresh_alloc`（[src/page.c:308-341](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L308-L341)）：它调用 `_mi_arenas_page_alloc` 拿页，若拿到的是别的线程遗弃的页则先 `_mi_theap_page_reclaim`；注意其开头的断言 `block_size > MI_LARGE_MAX_OBJ_SIZE || block_size == pq->block_size` 正是为 huge（前者）与普通页（后者）两种入队条件兜底。文件顶部 [src/page.c:926-931](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L926-L931) 的 `#if MI_HUGE_PAGE_ABANDON / #error todo.` 说明「huge 页参与遗弃复用」是未完成的设想，当前实现里 huge 页不进入 abandoned 位图。

#### 4.2.4 代码实践

**实践目标**：用调试器单步看一个 2 MiB 请求如何变成 `block_size` 与 singleton 页。

1. 用 u1-l2 的 debug 构建（含符号与断言）。
2. 编写最小程序（**示例代码**）：

   ```c
   #include <mimalloc.h>
   int main(void) {
     void* p = mi_malloc(2*1024*1024);
     mi_free(p);
     return 0;
   }
   ```

3. `gdb ./prog` 后：`break page.c:923`（`mi_huge_page_alloc` 入口）、`break page.c:932`（调用 `mi_page_fresh_alloc` 前）、`break page.c:945`（返回前）。运行后依次查看 `p size`、`p block_size`、`p *page`（重点看 `reserved`、`capacity`、`used`、`free`、`memid.memkind`）。

**需要观察的现象**：`block_size == 0x200000`（2 MiB 恰为 256 KiB 的倍数，取整不变）；返回前 `page->reserved == 1`、`page->capacity == 1`、`page->free != NULL`（唯一的块已在 free list 上）、`memkind` 为 `MI_MEM_ARENA`（若 arena 放得下）。

**预期结果**：上述字段值与 4.3 节的推导一致。gdb 断在静态内联函数所在行号可能因优化级别漂移，若断不住可改用 `break mi_huge_page_alloc`。**待本地验证**。

#### 4.2.5 小练习与答案

1. **为什么 `_mi_os_good_alloc_size` 的对齐档位随尺寸增大而变粗？**
   答：对齐的目的是让块大小凑成 64 KiB slice 的整数倍（arena 批发的粒度），避免页尾零头；对小对象用 4 MiB 对齐会浪费大量内存，对 ≥32 MiB 的对象用 4 KiB 对齐则产生大量非整 slice 的碎片。阶梯表在「零头尽量小」与「对齐不过度」之间取平衡，并保住 12.5% 的最坏碎片上界。
2. **`mi_huge_page_alloc` 里为什么要单独记 `malloc_huge` 统计，而不并入 `malloc_normal`？**
   答：huge 对象字节数巨大但个数稀少，与普通对象混在一起会让 `normal` 行的均值失真；单独记账还让「分配记 `block_size`、释放记 `block_size`」的配平口径有独立的落点（[src/alloc.c:73-83](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L73-L83) 与 [src/free.c:765-774](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L765-L774) 以 `MI_LARGE_MAX_OBJ_SIZE` 为界分流）。
3. **如果两个线程同时各分配一个 2 MiB 对象，会发生什么？**
   答：各自走各自 theap 的 `mi_huge_page_alloc`，在 arena 位图层面用原子操作各领一段 32 slice 的区间，互不加锁；两个页互不共享，也不进入对方的页队列。

### 4.3 单例页的退化形态：`reserved = capacity = used = 1` 与「即满即弃」

#### 4.3.1 概念说明

单例页没有「新开一个专属结构体」，而是把通用 `mi_page_t`（[types.h:425-456](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L425-L456)）退化使用：u3-l2 的三条 free list、`used/capacity/reserved` 不变式在这里全部退化——

- `reserved == 1`：`mi_page_is_singleton` 的定义就是它（[internal.h:845-847](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L845-L847)）；
- `capacity` 从 0 出发，`_mi_page_init` 调 `mi_page_extend_free` 后恰为 1（`bsize ≥ 8KiB` 时 `max_extend` 被压到 `MI_MIN_EXTEND=1`，见 [page.c:651-652](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L651-L652)）；
- 分配唯一的块后 `used == 1 == reserved`，`mi_page_is_full`（定义就是 `reserved == used`，[internal.h:918-922](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L918-L922)）**立刻为真**；
- 释放后 `used == 0` 即 `mi_page_all_free`，且 huge 队列是「特殊队列」，**跳过 retire 缓冲期、当场归还**。

也就是说单例页的一生是「出生即巅峰，用完即消失」：它从不像普通页那样经历「满页→full 队列/abandon→unfull→retire」的循环。

#### 4.3.2 核心流程

```text
单例页生命周期：
  arena 侧建页：reserved = (页大小-块起点)/block_size == 1
  _mi_page_init → mi_page_extend_free：free list 挂上唯一的块，capacity 0→1
  _mi_page_malloc_zero：弹出该块，used 0→1，free==NULL
  ↓ 页已满（reserved==used）
  mi_malloc_generic_fallback 尾声：mi_page_to_full
    └─ allow_page_abandon（默认真）→ _mi_page_abandon
         ├─ 从 huge 队列摘除、xthread_id 置 0（无主）
         └─ _mi_arenas_page_abandon：满页不进 abandoned 位图，仅释放所有权
  mi_free(p)（任意线程）→ 认领所有权 → used 0 → _mi_page_retire
    └─ huge 队列 is_special → 跳过 retire_expire → _mi_page_free
         └─ _mi_arenas_page_free：按 memid 原路归还（arena slice / OS）
```

#### 4.3.3 源码精读

`reserved` 的计算在 arena 建页处（[src/arena.c:1053](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1053)）：

```c
const size_t reserved = (os_align ? 1 : (page_noguard_size - block_start) / block_size);
```

`block_size` 已被 `_mi_os_good_alloc_size` 取整到 64 KiB 的倍数，而页大小是 slice 数乘 64 KiB，所以除法结果恰为 1（超对齐情形则直接写死 1）。单例分配路径还带着一条硬断言 `mi_assert(page->reserved == 1)`（[src/arena.c:1173](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1173)）。页字段初始化见 [src/arena.c:1062-1068](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1062-L1068)：`reserved`、`block_size`、`memid`（产地证）、`free_is_zero` 在此一次性写入；`mi_memid_t`（[types.h:286-336](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L286-L336)）里的 `memkind` 决定归还路线。

「分配后立即满」的入队处理（[src/page.c:1074-1081](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L1074-L1081)）：

```c
// move full pages to the full queue
if (mi_page_block_size(page) > MI_SMALL_MAX_OBJ_SIZE && mi_page_is_full(page)) {
  mi_page_to_full(page, mi_page_queue_of(page));
}
```

`mi_page_to_full`（[src/page.c:374-389](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L374-L389)）在允许 abandon 时直接调 `_mi_page_abandon`；arena 侧对「满页或单例页」不做位图登记、只解除所有权（[src/arena.c:1316-1344](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1316-L1344)，注释：`page is full (or a singleton), or the page is OS/externally allocated → leave as is`）。非 arena 产地（OS 直配）的页则挂进 `heap->os_abandoned_pages` 链表以便销毁堆时访问。

释放侧的「当场归还」由两段代码合谋完成。本线程释放使 `used` 归零（[src/free.c:44-53](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L44-L53)）：

```c
const mi_used_t used = page->used - 1;
...
page->used = used;
page->local_free = block;
if mi_unlikely(used==0) {
  if (page->retire_expire==0) { _mi_page_retire(page); }
}
```

而 `_mi_page_retire`（[src/page.c:439-456](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L439-L456)）里，huge 队列命中 `mi_page_queue_is_special(pq)` 而跳过「保留 16 个管理周期」的缓冲，直落 `_mi_page_free` → `_mi_arenas_page_free`（[src/arena.c:1285-1298](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1285-L1298)）。最终归还按产地分流（[src/arena.c:1433-1443](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1433-L1443)）：`mi_memkind_is_os` 直接 `_mi_os_free` 交还 OS，`MI_MEM_ARENA` 则清 slice 位图、可能安排延迟 purge。

顺带一提，debug 构建里 `mi_page_is_huge` 还被用来**跳过对整块的调试填充**（[src/alloc.c:90-92](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L90-L92) 与 [src/alloc.c:113-119](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L113-L119)）：给 64 MiB 的块逐字节 memset 调试图案代价太高。`mi_page_is_huge` 的定义（[internal.h:939-943](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L939-L943)）是「singleton 且（块超 512 KiB 或 OS 直配且元数据前置）」——第二个析取支覆盖了 u4-l4 将讲到的超对齐单例页。

#### 4.3.4 代码实践

**实践目标**：验证「huge 页释放立即归还、不进 retire 缓冲」与普通小对象页的差异。

1. 写两个对照循环（**示例代码**）：

   ```c
   #include <mimalloc.h>

   int main(void) {
     for (int i = 0; i < 10000; i++) {          // A：反复生灭一个 2MiB 单例页
       void* p = mi_malloc(2*1024*1024); mi_free(p);
     }
     for (int i = 0; i < 10000; i++) {          // B：同样的节奏，但用小对象
       void* p = mi_malloc(16); mi_free(p);
     }
     return 0;
   }
   ```

2. 分别只保留 A 或 B 编译运行，加 `MIMALLOC_SHOW_STATS=1`，记录 pages 段的 `pages`、`retired`、`abandoned`、`purged` 等计数。

**需要观察的现象**：A 版本中 huge 的 `malloc_huge_count` 累计 10000，但任一时刻存活页数为 1（每次 free 后立即归还），`retired` 几乎不增长；B 版本因小页有 retire 缓冲（`MI_RETIRE_CYCLES=16`，[src/page.c:414-415](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L414-L415)），会看到 `retired` 明显非零。

**预期结果**：两组计数呈上述分化；具体数值受其他 bin 的元数据分配干扰，**待本地验证**。

#### 4.3.5 小练习与答案

1. **若把单例页也放进 retire 缓冲（保留 16 个管理周期再还），会有什么问题？**
   答：一个 retire 中的 huge 页钉住了最多数 MiB 甚至数十 MiB 的已提交内存；而单例页不存在「同规格伙伴很快再来」的局部性收益（下一个 huge 请求尺寸多半不同），保留它只增加 RSS 不减少分配次数。
2. **单例页的 `capacity` 为什么还要走 `mi_page_extend_free` 而不直接置 1？**
   答：复用统一路径可以共享「按需 commit」逻辑（[page.c:664-690](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L664-L690)）与断言检查；且超对齐单例页（block_size 可能很小）也走同一函数，避免两套初始化代码。
3. **`_mi_page_free` 之后，这块内存在 arena 里立即可复用吗？**
   答：slice 位图立即可复用（其他线程可领走），但物理页可能进入延迟 purge 队列按 `purge_delay` 归还 OS（u6-l2/u6-l4 展开）；OS 直配的则同步 `_mi_os_free`。

### 4.4 arena 的边界：`mi_option_arena_max_object_size` 与 OS 直配

#### 4.4.1 概念说明

arena 不是无限大的口袋：默认上限受 `mi_arena_max_fixed_object_size()` 约束（页元数据对齐方案的副产物，约 256 MiB 减去页元数据区），而用户还可以用 **`mi_option_arena_max_object_size` 选项把这条线压低**，强制更大的对象直接向 OS 申请。两个要点：

1. **该选项的单位是 KiB**。选项表把它登记在「size in KiB」清单里（[src/options.c:182-185](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L182-L185)），读取端 `mi_option_get_size` 统一乘 `MI_KiB`（[src/options.c:291-300](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L291-L300)）。所以 `mi_option_set(mi_option_arena_max_object_size, 1024)` 的含义是 **1 MiB**，不是 1024 字节；环境变量 `MIMALLOC_ARENA_MAX_OBJECT_SIZE=1024` 同理（还支持 K/M/G 后缀，见 u2-l3）。
2. **这条线检查的是 slice 数，不是对象尺寸本身**。判定写成 `slice_count <= mi_arena_max_object_size()/MI_ARENA_SLICE_SIZE`（[src/arena.c:798-800](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L798-L800)）。因此把它压到 1 MiB（16 slice）时，受害的不只是 huge 对象——**large 档的 4 MiB 常规页（64 slice）也会被挤出 arena**，small（1 slice）与 medium（8 slice）页不受影响。这是本讲实践里最值得观察的连锁反应。

默认值来自 [src/options.c:59-60](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L59-L60) 的宏：`MI_SIZE_BITS × MI_ARENA_MAX_CHUNK_OBJ_SIZE = 64 × 32 MiB = 2 GiB`（64 位）。但 `mi_arena_max_object_size()` 会再与固定上限取小（[src/arena.c:116-128](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L116-L128)）：`MI_PAGE_META_IS_ALIGNED` 构建下固定上限是 `MI_PAGE_META_ALIGNMENT − 页元数据区`，即 \(256\mathrm{MiB} - \lceil 4096 \times \mathrm{sizeof}(mi\_page\_t) \rceil_{64\mathrm{KiB}}\)（按 64 位 release 构建估算约 255.5 MiB，sizeof 的精确值**待本地验证**），下限则钳到 `MI_ARENA_MIN_OBJ_SIZE`（64 KiB，[types.h:220](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L220)）——所以这个选项最小也只能把「arena 只装 1 slice 的页」作为下界，想彻底禁用 arena 应改用 `mi_option_disallow_arena_alloc`（[src/arena.c:604](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L604)）。

#### 4.4.2 核心流程

```text
_mi_arenas_page_alloc(theap, block_size, block_alignment)     # arena.c:1183
  ├─ 对齐 > 64KiB？ ──────────────→ mi_arenas_page_singleton_alloc   # 超对齐单例
  ├─ block_size ≤ 10KiB？ ────────→ regular(small 页, 1 slice)
  ├─ block_size ≤ ~84KiB？ ───────→ regular(medium 页, 8 slice)
  ├─ block_size ≤ 512KiB？ ───────→ regular(large 页, 64 slice)
  └─ 更大？ ───────────────────────→ mi_arenas_page_singleton_alloc   # huge 单例
        │
        ▼ 两者最终都进
  mi_arenas_page_alloc_fresh → mi_arenas_page_alloc_fresh_area
    ├─ 允许 arena 且 !os_align 且 slice_count ≤ max_object_size/64KiB
    │     → mi_arenas_try_alloc：原子位图领 slice 区间（memid = MI_MEM_ARENA）
    └─ 否则 → mi_arena_os_alloc_aligned → _mi_os_alloc_aligned（memid = MI_MEM_OS*）
```

#### 4.4.3 源码精读

页级分流的总闸（[src/arena.c:1183-1204](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1183-L1204)）：

```c
if mi_unlikely(block_alignment > MI_PAGE_MAX_OVERALLOC_ALIGN) {
  page = mi_arenas_page_singleton_alloc(theap, block_size, block_alignment);
}
else if (block_size <= MI_SMALL_MAX_OBJ_SIZE) { ... /* 1 slice 的 small 页 */ }
else if (block_size <= MI_MEDIUM_MAX_OBJ_SIZE) { ... /* 8 slice */ }
#if MI_ENABLE_LARGE_PAGES
else if (block_size <= MI_LARGE_MAX_OBJ_SIZE) { ... /* 64 slice */ }
#endif
else {
  page = mi_arenas_page_singleton_alloc(theap, block_size, block_alignment);
}
```

单例页需要多少 slice（[src/arena.c:1156-1170](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1156-L1170)）：`MI_PAGE_META_IS_ALIGNED` 构建下 `info_size=0`，于是 `slice_count = ⌈block_size / 64KiB⌉`，且**总是全量 commit**（`commit singletons always`——单例页没有「分批初始化」的余地）。arena/OS 的十字路口（[src/arena.c:794-817](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L794-L817)）：

```c
if (!mi_option_is_enabled(mi_option_disallow_arena_alloc) &&       // 允许用 arena？
    !os_align &&                                                   // 不是超对齐
    slice_count <= mi_arena_max_object_size()/MI_ARENA_SLICE_SIZE) // 且没超上限
{
  start = (uint8_t*)mi_arenas_try_alloc(heap, slice_count, ...);   // 原子位图批发
  ...
}
// otherwise fall back to the OS
if (start == NULL) { ... }
```

OS 回退分支（[src/arena.c:819-867](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L819-L867)）有个精妙细节：即使走 OS，也按 `MI_PAGE_META_ALIGNMENT`（256 MiB）对齐申请，并在前面多留一个 `page_offset`（≥64 KiB）放页元数据——这样 4.3 节的 `page->self` 快速定位（u3-l4 讲的对齐元数据方案）对 OS 直配的页同样成立，free 快路径不必区分产地。最终落到 [src/arena.c:573-591](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L573-L591) 的 `mi_arena_os_alloc_aligned`，它只是 `_mi_os_alloc_aligned(_at_offset)` 的薄封装（并尊重 `mi_option_disallow_os_alloc`）。

非页级的「大块内存」接口（如 `mi_reserve_os_memory`、元数据分配）走的是另一扇门 `_mi_arenas_alloc_aligned`（[src/arena.c:594-616](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L594-L616)），那里的上限检查是按字节（`size <= mi_arena_max_object_size()`），与页路径的 slice 口径等价。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：观察 512 KiB / 2 MiB / 64 MiB 三档对象分别走哪条路径，并用 `mi_option_arena_max_object_size=1MiB` 观察连锁反应。

1. 按 u1-l2 构建 debug 版（统计更细，`MI_STAT=2`）。写程序（**示例代码**）：

   ```c
   #include <mimalloc.h>
   #include <stdio.h>

   int main(void) {
     /* 方式一（程序内）：mi_option_set(mi_option_arena_max_object_size, 1024);  // 单位 KiB */
     void* p1 = mi_malloc(512*1024);        // large 档（release）或 huge（debug，+8B padding）
     void* p2 = mi_malloc(2*1024*1024);     // huge 单例页，32 slice
     void* p3 = mi_malloc(64*1024*1024);    // huge 单例页，1024 slice
     printf("usable: %zu %zu %zu\n",
            mi_usable_size(p1), mi_usable_size(p2), mi_usable_size(p3));
     mi_arenas_print();                     // 打印各 arena 的 slice 占用图（mimalloc.h:333）
     mi_free(p1); mi_free(p2); mi_free(p3);
     return 0;                              // 退出时由 MIMALLOC_SHOW_STATS=1 触发打印
   }
   ```

   编译链接（路径按你的构建目录调整）：`gcc -Iinclude big.c out/debug/libmimalloc-debug.a -lpthread -o big`。

2. **第一轮**：`MIMALLOC_SHOW_STATS=1 ./big`。记录 malloc 段 `huge` 行的 `current/total`（打印处 [src/stats.c:367-377](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L367-L377)）、arenas 段的 `arenas/committed`，以及 `mi_arenas_print()` 输出里三段连续被占用的 slice 区间（512 KiB 那块若是 large 档应占 64 slice 的 4 MiB 页）。
3. **第二轮**：`MIMALLOC_ARENA_MAX_OBJECT_SIZE=1024 MIMALLOC_SHOW_STATS=1 ./big`（等价于程序内 `mi_option_set(..., 1024)`，两种方式任选其一并对比——注意 u2-l3 讲过程序内设置优先级更高、但不打印选项表）。
4. 对照源码解释差异。

**需要观察的现象**：

- 第一轮：`huge` 行 total ≈ 66 MiB（2 MiB + 64 MiB 按块大小记账）；512 KiB 在 release 下不出现在 huge 行（debug 下出现，原因见 4.1.3）；三段内存都在某个 arena 的 slice 图内。
- 第二轮（上限 1 MiB = 16 slice）：2 MiB（32 slice）与 64 MiB（1024 slice）超限 → OS 直配，arena 的 slice 图里不再有这两段，arenas 段 committed 不再包含它们；同时 **512 KiB 对象的 4 MiB large 页（64 slice）也被挤出 arena**（4.4.1 的连锁反应），进程的 OS 直配统计上升。

**预期结果**：两轮的 arenas 段 committed 差值 ≈ 66~70 MiB；`mi_arenas_print()` 第一轮能看到大段连续占用、第二轮看不到。具体输出格式与数值**待本地验证**。

#### 4.4.5 小练习与答案

1. **把 `arena_max_object_size` 设成 64（即 64 KiB）会发生什么？设成 1 呢？**
   答：设 64 → 上限钳到 `MI_ARENA_MIN_OBJ_SIZE`（64 KiB，1 slice），只有 small 页（1 slice）还能留在 arena，medium/large/huge 全部 OS 直配；设 1 会先被 `_mi_align_up` 到 64 KiB 再钳下限，效果相同（[src/arena.c:116-128](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L116-L128)）。
2. **为什么 OS 直配的页也要按 256 MiB 对齐？**
   答：为了让「指针 → 页元数据」的纯算术定位（`MI_PAGE_META_IS_ALIGNED`，u3-l4）对所有页统一成立：元数据放在所属 256 MiB 段头部的数组里，free 快路径无需查 page map，也无需区分产地。
3. **`mi_option_arena_max_object_size` 的默认值注释写着 2 GiB，实际生效的边界是多少？**
   答：`mi_arena_max_object_size()` 会与固定上限取小；默认构建（元数据对齐方案）下实际约 255.5 MiB（256 MiB 对齐边界减去页元数据区），超过它的对象总是 OS 直配。

## 5. 综合实践

把本讲三块知识串成一份「大对象分配画像报告」：

1. 写一个程序，循环读取用户输入的尺寸 n，执行 `mi_malloc(n)`，打印 `mi_good_size(n)`、`mi_usable_size(p)`，并在每次分配后调用 `mi_arenas_print()`（建议重定向到文件，输出较大）。
2. 依次试这些尺寸：100 KiB、500 KiB、512 KiB、513 KiB、1 MiB、2 MiB+1、8 MiB、33 MiB、64 MiB、300 MiB。
3. 对每个尺寸回答三问并填表：(a) 它是 small/medium/large/huge 哪一档？(b) 内存来自 arena 还是 OS？(c) 实际块大小是多少、内部碎片多大？
4. 用 `MIMALLOC_ARENA_MAX_OBJECT_SIZE=1024` 重跑一遍，标出哪些行的 (b) 列翻转了，并对照 [src/arena.c:798-800](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L798-L800) 解释翻转的 slice 数门槛。
5. 挑战题：在表中找出「`mi_good_size(n)` 与 `mi_usable_size(p)` 不相等」的行，用 `_mi_os_good_alloc_size` 的阶梯表（[src/os.c:88-97](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c#L88-L97)）逐行解释差值来源。

验收标准：你的表格应能复现三条规律——512 KiB 处的档位切换、huge 块大小的阶梯取整（最坏约 12.5% 碎片）、以及 arena→OS 的翻转只与 slice 数有关而与「是否 huge」无关。

## 6. 本讲小结

- 超过 `MI_LARGE_MAX_OBJ_SIZE`（512 KiB，`4MiB/8`）的对象不再进 size class 页：`_mi_bin` 返回 `MI_BIN_HUGE`，`mi_find_page` 据此转入 `mi_huge_page_alloc`，一个对象独占一个单例页（`reserved==1`）。
- huge 的块大小由 `_mi_os_good_alloc_size` 阶梯取整（≥32 MiB 按 4 MiB、8~32 MiB 按 1 MiB、2~8 MiB 按 256 KiB、0.5~2 MiB 按 64 KiB），最坏内部碎片约 12.5%，与 size class 的设计上界一致。
- 单例页是通用 `mi_page_t` 的退化使用：`capacity` 经 `mi_page_extend_free` 变为 1，分配唯一的块后 `used==reserved==1` 立即满页，随即脱离 huge 队列、交出所有权；释放使 `used==0` 后因 huge 队列是特殊队列而**跳过 retire 缓冲当场归还**。
- 归还路线由 `memid.memkind` 决定：`MI_MEM_ARENA` 清 slice 位图（可能延迟 purge），OS 系 `mi_memkind` 直接 `_mi_os_free`；OS 直配的页同样按 256 MiB 对齐，保证 free 快路径的元数据定位对所有产地统一。
- `mi_option_arena_max_object_size`（**单位 KiB**）画出 arena 与 OS 的分界，检查口径是 slice 数；把它压到 1 MiB 会连带把 4 MiB 的 large 常规页挤出 arena。默认选项值 2 GiB，但被固定上限（约 255.5 MiB）钳小。

## 7. 下一步学习建议

本讲收尾了 `mi_malloc` 的尺寸全景：小对象走直查数组（u4-l1），中等对象走页队列与分批扩展（u4-l2），超大对象走单例页（本讲）。下一讲（u4-l4）处理这条主线上的最后一批入口——`realloc` 与对齐分配：你会看到 `huge_alignment` 参数（本讲反复出现却始终为 0）终于被填上具体值，以及「对齐超过 64 KiB」如何复用 `mi_arenas_page_singleton_alloc` 造出块不大的单例页。之后进入单元五读 `mi_free`，本讲的「即满即弃、free 时认领归还」将从释放线程的视角再讲一遍。若想提前看到 arena 位图如何无锁切 slice，可先跳读 u6-l3。
