# u4-l1 mi_malloc 快路径：几条指令完成一次小对象分配

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐行说出 `mi_malloc` 到真正取出一个块的完整调用链：`mi_malloc` → `_mi_theap_default()` → `mi_theap_malloc` → `_mi_theap_malloc_zero_ex` → `mi_theap_malloc_small_zero_nonnull` → `_mi_theap_get_free_small_page` → `mi_page_malloc_zero`。
2. 数出这条快路径上**最少的内存读写次数**（默认 64 位 release 构建约 5 次读 + 3 次写，共 8 次），并说出每一次访问落在哪条缓存行上。
3. 解释为什么「从 free list 头部弹出一个块」不需要任何原子操作、不需要锁、也不需要查 page map。
4. 说出 mimalloc 用哪些手段保证这条路径的机器码质量：`mi_decl_forceinline`、`mi_likely/mi_unlikely`、`mi_decl_cache_align`、一处手写的编译器屏障 `__asm("" : : : "memory")`，以及源码注释里那句「约 7 条指令、一次判断」的出处。

单元三把数据结构的地基打完了：u3-l1 的所有权链（heap→theap→页队列→page→block）、u3-l2 的三条 free list、u3-l3 的 `pages_free_direct` 直查数组。本讲是第一次把这些零件**串成一条正在运行的指令流**——也就是每次 `mi_malloc` 真正执行的那十来条机器指令。

## 2. 前置知识

- **快路径 / 慢路径（fast path / slow path）**：分配器把「最常见的情况」单独写成一段极短的直线代码，把所有罕见情况（free list 空、需要新页、尺寸超大）集中甩给一个兜底函数。快路径追求的是**最好的缓存局部性与最少的分支**，慢路径追求正确与完备。
- **TLS（线程本地存储）**：每个线程各有一份的变量。不带 `heap` 参数的 `mi_*` 函数第一步都要从 TLS 取「本线程默认 theap」。u7-l2 会专门讲 TLS 的多种实现，本讲只需要知道：在 Linux 默认构建下，这一步就是**一条 `fs:` 段寄存器寻址的装载指令**。
- **wsize（字数）**：字节数向上取整除以机器字长（`sizeof(void*)`，64 位下 8）。u3-l3 已介绍，`pages_free_direct` 的下标就是 wsize。
- **侵入式 free list**：空闲块不额外占用内存，块的头 8 字节直接复用为指向下一个空闲块的指针（`mi_block_t`，见 u3-l2）。所以「分配一个块」=「把链表头摘下来」。
- **release 构建的宏形态**：默认 release 下 `MI_DEBUG=0`、`MI_STAT=0`、`MI_PADDING=0`、`MI_ENCODE_FREELIST=0`、`MI_TRACK_ENABLED=0`（[include/mimalloc/types.h:L72-L110](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L72-L110)）。这意味着源码里大量 `#if` 块在 release 里**整体消失**——读快路径源码时要时刻问自己「这行在 release 里还在吗」。
- **缓存行（cache line）**：CPU 以 64 字节为单位搬运内存。mimalloc 刻意把快路径要摸的字段排进同一条缓存行，本讲会数给你看。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/alloc.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c) | 本讲主角：从 `mi_malloc` 入口到 `mi_page_malloc_zero` 弹块的整条快路径，以及全部 `mi_*` 公共入口的「一行转发」实现 |
| [include/mimalloc/types.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h) | `mi_block_t`、`mi_page_t`（字段布局就是为快路径排的）、`mi_theap_t`（`pages_free_direct` 放在结构体最前面）、`mi_used_t`，以及 release 下各检查宏的取值 |
| [include/mimalloc/internal.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h) | 三块拼图：`_mi_theap_get_free_small_page`（直查页）、`_mi_wsize_from_size`（字节→字数）、`mi_decl_forceinline`/`mi_likely` 的宏定义、`mi_block_next`（取下一个空闲块） |
| [include/mimalloc/prim-tls.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h) | `_mi_theap_default()` 的内联实现（Linux 默认 `MI_TLS_MODEL_LOCAL` 模型下就是一次 TLS 装载），是快路径的第一条指令 |
| [src/init.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c) | 只读模板 `mi_page_empty` 与 `_mi_theap_empty`：解释「线程第一次分配为什么不需要特判」 |
| [CMakeLists.txt](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt) | `MI_SEE_ASM` 选项（L49 定义，L342 起生效）：给 GCC/Clang 加 `-save-temps`，把汇编中间文件留下来，本讲综合实践要用 |

## 4. 核心概念与源码讲解

### 4.1 入口漏斗：从 mi_malloc 到弹块的四层内联塔

#### 4.1.1 概念说明

用户调用 `mi_malloc(n)` 时，代码沿着一个**层层内联的漏斗**下沉：

```
mi_malloc(n)                          ← 公共入口（alloc.c L256，唯一有函数调用的边界）
 └─ _mi_theap_default()               ← 第 1 次访存：从 TLS 取本线程默认 theap
 └─ mi_theap_malloc(theap, n)         ← extern inline（L252）
     └─ _mi_theap_malloc_zero_ex      ← 分岔口：n ≤ 1024 吗？（L229）
         ├─ 是 → mi_theap_malloc_small_zero_nonnull   ← 快路径（L133）
         │         ├─ _mi_theap_get_free_small_page    ← 第 2 次访存：直查页（internal.h L650）
         │         └─ mi_page_malloc_zero              ← 弹块（L32，真正干活的地方）
         └─ 否 → mi_theap_malloc_generic → _mi_malloc_generic（page.c 的慢路径，u4-l2 专讲）
```

关键认知有两点：

1. **漏斗上除了 `mi_malloc` 本身，全部是 `static` + `mi_decl_forceinline`**。编译后它们不是函数调用，而是被平铺进 `mi_malloc` 的函数体里，整条链是**一段直线机器码**。唯一的例外是慢路径 `_mi_malloc_generic`，它住在 page.c，是一个真正的 out-of-line 调用。
2. **快慢路径的分岔口只有一个条件：`size <= MI_SMALL_SIZE_MAX`**。这个常量在公共头里定义为 128 个字，64 位下就是 1024 字节：

- [include/mimalloc.h:L122-L123](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L122-L123)：`MI_SMALL_WSIZE_MAX = 128`，`MI_SMALL_SIZE_MAX = 128 * sizeof(void*)` = 1024 B。注意 u3-l3 提醒过的命名陷阱：这是「快路径上限」（1 KiB），不是「小页对象上限」`MI_SMALL_MAX_OBJ_SIZE`（10 KiB）。

#### 4.1.2 核心流程

一次 `mi_malloc(100)`（release、Linux、非首次分配）的完整流程：

1. **取 theap**：从 TLS 装载默认 theap 指针（1 次读）。
2. **判尺寸**：`100 <= 1024` 成立 → 走小对象路径。这是一次寄存器比较 + 一个几乎总被预测正确的分支。
3. **查页**：`idx = (100+7)/8 = 13`，取 `theap->pages_free_direct[13]` 得到页指针（1 次读）。
4. **判链表**：读 `page->free`（1 次读）；非 NULL 则继续，NULL 则调用慢路径。
5. **弹块**：读 `block->next`（1 次读），把 `block->next` 清零（1 次写），`page->free = next`（1 次写），`page->used = used+1`（1 次写，前面顺带读过 `used`）。
6. **返回**：`block` 本身就是用户指针——mimalloc **没有每块头（per-block header）**。

伪代码（把所有 release 下被裁剪的代码去掉后，快路径就剩这些）：

```text
theap = TLS.__mi_theap_default            # 读 1
if size > 1024: goto generic               # 分岔口（唯一总在的判断之一）
page = theap->pages_free_direct[(size+7)/8]  # 读 2
block = page->free                          # 读 3
used  = page->used                          # 读 4（与读 3 并行发射）
if block == NULL: goto generic              # 分岔口
next  = block->next                         # 读 5
block->next = 0                             # 写 1
page->free  = next                          # 写 2
page->used  = used + 1                      # 写 3
return block
```

**合计 5 次读 + 3 次写 = 8 次内存访问，触及 3 条缓存行**（TLS/theap 一条、页头一条、块本身一条）。

#### 4.1.3 源码精读

**公共入口只有一行**——先取默认 theap，再进内层：

- [src/alloc.c:L256-L258](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L256-L258)：`mi_malloc` 调用 `mi_theap_malloc(_mi_theap_default(), size)`。u1-l4 讲过「所有不带 `heap` 的入口第一步都是取默认 theap」，就是这一行。
- [src/alloc.c:L252-L254](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L252-L254)：`mi_theap_malloc` 是 `extern inline`，直接转发到 `_mi_theap_malloc_zero_ex(theap, size, false, 0, NULL)`。
- [include/mimalloc/prim-tls.h:L255-L260](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h#L255-L260)：Linux 默认的 `MI_TLS_MODEL_LOCAL` 模型下，`_mi_theap_default()` 就是 `return __mi_theap_default;`——一条 TLS 装载指令，没有函数调用、没有判断。

**分岔口**——快路径的「门票检查」：

- [src/alloc.c:L229-L243](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L229-L243)：`_mi_theap_malloc_zero_ex`。`if mi_likely(size <= MI_SMALL_SIZE_MAX)` 进小对象路径，否则进 `mi_theap_malloc_generic`。注意 `#if MI_THEAP_INITASNULL` 分支：在 pthread/Win32 这类「默认 theap 可能是 NULL」的 TLS 模型下，条件变成 `theap!=NULL && size <= MI_SMALL_SIZE_MAX`，多一次判断；Linux 的 LOCAL 模型下默认 theap 永远指向静态空 theap，不会是 NULL，这次判断被编译掉。
- [src/alloc.c:L190-L201](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L190-L201)：`mi_theap_malloc_small_zero` 是同一个判断的另一个入口（`mi_malloc_small`/`mi_zalloc_small` 走这里），`MI_THEAP_INITASNULL` 时对 NULL theap 直接 tailcall 到 generic 路径。

**小对象非空路径**——一切检查在 release 里都不存在：

- [src/alloc.c:L133-L160](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L133-L160)：`mi_theap_malloc_small_zero_nonnull`。函数体看似不短，但逐段看：
  - L135-L139 全是 `mi_assert`（release 空宏）；
  - L140-L142 的 `size == 0` 补丁只在 `MI_PADDING || MI_GUARDED` 时存在（release 两者都为 0）；
  - L143-L147 的 guarded 采样只在 `MI_GUARDED` 时存在；
  - **真正剩下来的只有 L149-L152 两行**：取页，然后调用 `mi_page_malloc_zero`。注释写得很清楚："get page in constant time"。
- [src/alloc.c:L163-L187](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L163-L187)：`mi_theap_malloc_generic` 是对 page.c 里 `_mi_malloc_generic` 的薄封装，本讲只把它当作「快路径失败后甩锅的对象」。

**「线程第一次分配」为什么不特判？** 这是本模块最漂亮的一处设计：

- [src/init.c:L16-L45](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L16-L45)：静态只读的 `mi_page_empty`，字段全零——`free = NULL`、`used = 0`、`block_size = 0`。
- [src/theap.c:L524-L526](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L524-L526)：新 theap 的 `pages_free_direct` 每一项都初始化为 `_mi_page_empty_get()`。所以直查数组**条目永不为 NULL**，快路径不需要对页指针做空判断。
- 一个从未分配过的线程：TLS 指向静态空 theap（[src/init.c:L396-L397](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L396-L397) 把默认值设为 `&_mi_theap_empty`），直查落到 `mi_page_empty`，它的 `free == NULL` → 第 4 步的判断自然失败 → 落进慢路径 → 慢路径完成主堆初始化与本线程 theap 的创建。**同一个分支既服务热路径又服务冷路径，无需任何额外判断。**
- [src/alloc.c:L34-L38](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L34-L38)：`if (page->block_size != 0)` 包住一组断言，就是为空页准备的（空页 `block_size == 0`，断言会炸）；release 下断言为空，整个 if 被编译器消除，一次读都不要。

#### 4.1.4 代码实践

**实践目标**：确认「哪些公共 API 共享同一条快路径」，并把调用链抄成自己的注释。

1. 在 [src/alloc.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c) 中用编辑器搜索 `_mi_theap_malloc_zero_ex` 与 `mi_theap_malloc_small_zero_nonnull` 的全部调用点。
2. 把 4.1.2 的伪代码抄到自己的笔记里，对每个公共函数（`mi_malloc`、`mi_zalloc`、`mi_calloc`、`mi_heap_malloc`、`mi_strdup`）在伪代码旁边标注它从哪一层进入漏斗。
3. 回答：`mi_calloc(10, 100)`（共 1000 字节）走快路径吗？`mi_calloc(11, 100)`（1100 字节）呢？

**需要观察的现象 / 预期结果**：`mi_malloc`、`mi_zalloc`、`mi_heap_malloc`（尺寸 ≤ 1024 时）都汇入 `_mi_theap_malloc_zero_ex` → `mi_theap_malloc_small_zero_nonnull` → `mi_page_malloc_zero`；`mi_calloc` 多一步乘法溢出检查（[src/alloc.c:L291-L295](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L291-L295)）再转 `mi_theap_zalloc`；`mi_strdup` 要先 `strlen` 再 `mi_theap_malloc`。1000 ≤ 1024 走快路径；1100 > 1024 走 generic——但它仍是 S 档（≤ 10 KiB），只是要经过 `mi_bin` 计算而已（这正是 u3-l3 说的两个「小」的区别）。

#### 4.1.5 小练习与答案

**练习 1**：`mi_malloc` 入口的第一次内存访问读的是什么？落在哪个数据结构上？
**答案**：读 TLS 变量 `__mi_theap_default`（[include/mimalloc/prim-tls.h:L249](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h#L249)），得到本线程默认 `mi_theap_t` 的指针。它是后面 `pages_free_direct` 数组访问的基址。

**练习 2**：为什么 `mi_theap_malloc_small_zero_nonnull` 名字里带 `nonnull`？谁保证 theap 非空？
**答案**：它假定 `theap != NULL`，因此省掉一次空判断。在 Linux 的 `MI_TLS_MODEL_LOCAL` 模型下，默认 theap 被初始化为静态空 theap 的地址（[src/init.c:L396](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L396)），永不为 NULL；而 `mi_heap_malloc` 系列传进来的是 `_mi_heap_theap(heap)`，也非空。可能为 NULL 的模型（pthread/Win32 TLS）下，判空在更外层的 `_mi_theap_malloc_zero_ex`（L231-L235）已经做过了。

**练习 3**：如果把 `MI_SMALL_SIZE_MAX` 从 128 个字改成 64 个字，`pages_free_direct` 数组会怎么变？有什么代价？
**答案**：数组大小 `MI_PAGES_DIRECT = MI_SMALL_WSIZE_MAX + MI_PADDING_WSIZE + 1`（[include/mimalloc/types.h:L557](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L557)）从 129 项缩到 65 项，每个 theap 省 512 字节；但 65..128 字（513..1024 B）的请求会跌落到 generic 路径，每次都要计算 `mi_bin` 并查页队列，慢路径比例上升。这是一个「空间换直达性」的典型权衡。

### 4.2 一步找到页：pages_free_direct 直查

#### 4.2.1 概念说明

u3-l3 已经介绍了 `pages_free_direct` 的组织方式，本讲从**执行成本**的角度再看它一次。

分配一个小对象时，最朴素的做法是「算 bin → 找到该 bin 的页队列 → 取队首页」，至少要做移位、查表、再解一次链表指针。mimalloc 的做法是把这套流程**预先算好缓存起来**：theap 里维护一个数组，下标是「请求尺寸的 wsize」，值是「该尺寸当前应该从哪个页分配」。于是找页退化成**一次数组下标访问**：

\[
\text{page} = \text{theap}\rightarrow\text{pages\_free\_direct}\left[\left\lceil \frac{\text{size}}{8} \right\rceil\right]
\]

注意定义域：下标范围是 0..128（release 下共 129 项），恰好覆盖 0..1024 字节。数组被刻意放在 `mi_theap_t` 的**第一个字段**，注释写着 "put in front for fast small allocations"——这样 `theap` 指针加上一个小常数就是目标地址，偏移固定、无需乘法。

#### 4.2.2 核心流程

```
请求 size 字节
   │
   ├─ _mi_wsize_from_size(size) = (size + 7) / 8     ← 一次加法、一次移位，无查表
   │        （internal.h L571-L574）
   ├─ 取 theap->pages_free_direct[idx]               ← 一次内存读
   │
   └─ 得到 page，交给 mi_page_malloc_zero
```

数组条目的**不变式**（由 page-queue.c 维护，u3-l3 已讲）：

- 条目**永不为 NULL**：某个 wsize 暂时没有可用页时，条目指向静态 `mi_page_empty`（其 `free == NULL`），于是快路径自然失败、落入慢路径，由慢路径刷新条目。
- 条目指向的页**可能有空闲块，也可能刚好用完**（`free == NULL`）——它只是「该尺寸 bin 的页队列的队首页」的缓存，不保证非空。这就是为什么 `mi_page_malloc_zero` 里仍要判一次 `page->free == NULL`。
- 相邻几个 wsize 条目常常指向同一个页：一个 bin 的 `block_size` 覆盖多个相邻 wsize（比如块长 128 字节的 bin 同时服务 121..128 字节的请求），队首变化时由 `mi_theap_queue_first_update` 成段刷新。

#### 4.2.3 源码精读

- [include/mimalloc/internal.h:L650-L655](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L650-L655)：`_mi_theap_get_free_small_page` 的全部实现——算 wsize 下标、断言边界、返回数组元素。函数体内**没有循环、没有判断**（两条断言在 release 下消失），这就是"constant time"的含义。
- [include/mimalloc/internal.h:L571-L574](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L571-L574)：`_mi_wsize_from_size`，就是 `(size + sizeof(uintptr_t) - 1) / sizeof(uintptr_t)`，编译器会把除以 8 优化成移位。
- [include/mimalloc/types.h:L561-L563](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L561-L563)：`mi_theap_s` 的开头——`pages_free_direct[MI_PAGES_DIRECT]` 是结构体的第一个字段，注释明确说明它是「每个条目指向对应尺寸队列中可能有空闲块的页」的优化数组。
- [include/mimalloc/types.h:L557](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L557)：`MI_PAGES_DIRECT = MI_SMALL_WSIZE_MAX + MI_PADDING_WSIZE + 1`。release 64 位下 = 128 + 0 + 1 = **129 项**（约 1 KiB）；debug 下 `MI_PADDING_WSIZE = 1`，为 130 项。
- [src/alloc.c:L149-L151](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L149-L151)：调用点。注意传给下层的是 `size + MI_PADDING_SIZE`——release 下 `MI_PADDING_SIZE == 0`（[include/mimalloc/types.h:L550-L555](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L550-L555)），这个加法在 release 里也不产生指令；debug 下则把 padding 算进下标，使 debug 构建的边界尺寸可能跳档（u3-l3 实践里已经见过）。
- [include/mimalloc/internal.h:L31](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L31)：`mi_decl_cache_align = mi_decl_align(64)`。theap 按 64 字节缓存行对齐，保证 `pages_free_direct` 不会和别的线程频繁读写的字段挤在同一条缓存行里（避免伪共享）。

#### 4.2.4 代码实践

**实践目标**：从外部可观察地验证「多个相邻请求尺寸共享同一个直查条目（同一个 bin）」。

1. 写一个程序，循环 `n` 从 1 到 1032，每次打印 `n`、`(n+7)/8`（自己算的 wsize 下标）和 `mi_good_size(n)`（mimalloc 告诉你的实际块长，u1-l4 介绍过这个 API）。
2. 统计：每个 `mi_good_size` 返回值覆盖了几个不同的 wsize 下标。
3. 特别打印 `n = 1024` 与 `n = 1025` 两行，并对照 `mi_good_size` 的结果。

```c
/* 示例代码：验证直查下标与 size class 的覆盖关系 */
#include <stdio.h>
#include <mimalloc.h>

int main(void) {
  for (size_t n = 1; n <= 1032; n++) {
    printf("n=%4zu wsize=%3zu good=%5zu%s\n",
           n, (n + 7) / 8, mi_good_size(n),
           (n == 1024 || n == 1025) ? "   <-- 边界" : "");
  }
  return 0;
}
```

**需要观察的现象 / 预期结果**：小于 8 字节的请求 `good` 都是 8（一个 bin 覆盖 wsize 1）；在 512..1024 字节区间，一个 `good` 值通常连续覆盖 2 个甚至更多相邻 wsize——这正是「一个 bin 服务多个 wsize、多个直查条目指向同一个页」的外部证据。`n=1024` 与 `n=1025` 的 `good` 值相同（同一个 size class），但内部路径不同：1024 走直查快路径，1025 在 `_mi_theap_malloc_zero_ex` 的分岔口被判 `> 1024` 而进入 generic 路径。具体覆盖几个 wsize 随编译目标与 debug/release 而异——待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`pages_free_direct` 条目指向的页可能同时被两个不同 wsize 的请求用到吗？
**答案**：会，而且很常见。条目值是「该尺寸所属 bin 的页队列队首页」的缓存，一个 bin 的 `block_size` 大于 8 字节时就覆盖多个 wsize。例如块长 128 字节的 bin 同时是 wsize 15（113..120 B）和 wsize 16（121..128 B）两个条目的目标。

**练习 2**：既然条目指向的页可能已满（`free == NULL`），为什么不让维护代码保证「条目永远指向有空闲块的页」？
**答案**：因为「有空闲块」这个性质随时可能被**本线程自己的上一次分配**破坏（最后一个块刚好被取走）。要保持该不变式就得在每次分配后判断并刷新条目，快路径反而多一次写和一次判断。mimalloc 选择了更便宜的方案：允许条目失效，让 `page->free == NULL` 这个本来就要做的判断顺便兜底，失败才进慢路径去刷新。

**练习 3**：`_mi_theap_get_free_small_page` 的参数是 `size + MI_PADDING_SIZE`，断言要求 `size <= MI_SMALL_SIZE_MAX + MI_PADDING_SIZE`。为什么 debug 构建的断言边界要放宽？
**答案**：debug 构建 `MI_PADDING = 1`，`MI_PADDING_SIZE = 8`（[include/mimalloc/types.h:L545-L551](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L545-L551)），padding 也占一个字，所以合法请求的内部尺寸上限变成 1024+8，下标上限也相应 +1，这就是 `MI_PAGES_DIRECT` 里 `+ MI_PADDING_WSIZE` 的来历。

### 4.3 mi_page_malloc_zero：从 free list 头部弹出一个块

#### 4.3.1 概念说明

`mi_page_malloc_zero` 是整条链上**真正把内存交给用户**的函数。它做的事情用一句话概括：**从页的 `free` 链表头摘下一个块，更新两个页字段，返回块地址**。

三个让它能做到极简的前提，都建立在单元三的结构设计上：

1. **块地址即用户指针**：mimalloc 没有 per-block header。块空闲时头 8 字节是 `next` 指针（`mi_block_t` 只有一个字段，[include/mimalloc/types.h:L366-L368](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L366-L368)），分配出去之后这 8 字节就归用户。所以不需要「指针 + 偏移」的计算。
2. **`free` 链表只归本线程碰**：u3-l2 讲过的三链分工——`free` 是唯一对 malloc 可见的链，本线程释放进 `local_free`，跨线程释放进 `xthread_free`，后两者只在慢路径被收割。因此这里的读、写全部是**普通（非原子）访存**，一个原子指令都没有。
3. **`used` 计数足够**：分配只需 `used+1`，不需要维护 `|free|`（types.h 的注释 L411-L413 明确说「不用统计 |free|，只用 `used`，以减少访存次数」）。

源码作者在这段函数上方留了一句量化的注释，值得原样读一遍：

> "Fast allocation in a page: just pop from the free list. Fall back to generic allocation only if the list is empty. **Note: in release mode the (inlined) routine is about 7 instructions with a single test.**"

——[src/alloc.c:L29-L31](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L29-L31)。本讲的综合实践就是拿汇编来核对这句话。

#### 4.3.2 核心流程

```
进入 mi_page_malloc_zero(theap, page, size, zero)
 1. block = page->free          ← 读
 2. used  = page->used          ← 读（与第 1 步并行发射，见 4.4）
 3. 若 block == NULL → 返回 _mi_malloc_generic(...)     ← 慢路径，唯一出口
 4. next  = block->next         ← 读（release 下 MI_ENCODE_FREELIST=0，就是一次裸装载）
 5. block->next = 0             ← 写（防止把内部指针泄漏给用户）
 6. page->free  = next          ← 写（链表头弹出）
 7. page->used  = used + 1      ← 写
 8. （release、zero=false 时到此为止）返回 block
```

对照 u3-l2 的计数不变式：

\[
\text{capacity} \;=\; \text{used} - |\text{thread\_free}| + |\text{free}| + |\text{local\_free}|
\]

快路径只改 `used`（+1）和 `|free|`（−1），两者相互抵消，`capacity` 不动，不变式保持成立。这也是为什么分配**不需要**碰 `capacity`/`reserved`。

关于 `zero` 参数的三种结局（对理解 zalloc 的成本很重要）：

| 场景 | 代价 |
| --- | --- |
| `zero == false`（`mi_malloc`） | release 下**零额外成本**；debug 下把整块 memset 成 `0xD0`（未初始化毒化） |
| `zero == true` 且 `page->free_is_zero == false` | `_mi_memzero_aligned` 清零整块——按块长线性成本 |
| `zero == true` 且 `free_is_zero == true` | 只需保证头 8 字节为 0，**而第 5 步已经写过了**，无额外成本 |

第三行是 `free_is_zero` 标志存在的意义：从全新页（OS 刚给的记忆体本来就是零）弹出的块，唯一被弄脏的 8 字节就是 `next` 指针本身，第 5 步的 `block->next = 0` 顺手把它清掉了。

#### 4.3.3 源码精读

**判空与读计数**：

- [src/alloc.c:L40-L48](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L40-L48)：读 `page->free` 到 `block`、读 `page->used` 到局部变量 `used`，然后 `if (block == NULL) return _mi_malloc_generic(...)`。L43-L45 那行 `__asm("" : : : "memory")` 是一处**手写的编译器屏障**，作用见 4.4 节。
- 注意 `used` 被读进局部变量、稍后才写回（L57），这个「早读晚写」是刻意的：早读让两次装载并行发射，晚写合并成一次 store。

**弹块五连**（本讲的精华，建议逐字读）：

- [src/alloc.c:L52-L57](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L52-L57)：
  - L53 `mi_block_next(page, block)` 取下一个空闲块；release 下 `MI_ENCODE_FREELIST == 0`，[include/mimalloc/internal.h:L1271-L1284](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1271-L1284) 退化为直接 `return block->next`（secure/debug 构建才会解码并做同页校验）。
  - L55 `block->next = 0`，注释 "don't leak internal data"：不把内部链表指针泄漏给用户（对 zalloc 尤其重要，也是安全上的良好习惯）。
  - L56-L57 更新页头：链表头弹出 + `used+1`。
- [include/mimalloc/types.h:L366-L368](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L366-L368)：`mi_block_t` 只有一个 `mi_encoded_t next` 字段——整个空闲块链表的「节点结构」就 8 字节，而且借用的是用户内存。

**页头布局**：为什么这 3+2 次访问只占一两条缓存行？

- [include/mimalloc/types.h:L425-L437](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L425-L437)：快路径要摸的三个字段 `free`（L430）、`used`（L431）、`block_size`（L434）排在结构体最前面，L423 的注释直说 "The layout below is optimized for `free.c:mi_free` and `alloc.c:mi_page_alloc`"。64 位下 `self`+`xthread_id`+`free`+`used`+`local_free`+`block_size`+`page_offset`+几个小字段合计不足 48 字节，**全部落在第一条缓存行**；`xthread_free` 起头第二条（L442 注释 "next cache line"），快路径完全不碰它。
- [include/mimalloc/types.h:L396](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L396)：`used` 的类型 `mi_used_t = size_t`。

**release 里会消失的代码**（读源码时别被它们吓到）：

- [src/alloc.c:L61-L65](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L61-L65)：`MI_DEBUG>3` 的整块零检查。
- [src/alloc.c:L73-L83](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L73-L83)：`MI_STAT>0` 的统计累计（debug 下每次分配要更新 `malloc_normal`、`malloc_bins[bin]` 等多个计数器——这就是 u1-l4 说「细粒度统计有性能代价」的出处）。
- [src/alloc.c:L85-L102](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L85-L102)：zero 分支。`mi_likely(!zero)` 提示编译器把非零初始化作为直线代码；debug 下 L91 用 `MI_DEBUG_UNINIT`（= 0xD0，[include/mimalloc/types.h:L793-L794](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L793-L794)）毒化整块。
- [src/alloc.c:L104-L120](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L104-L120)：`MI_PADDING` 的哨兵写入（debug/secure 才有），在块尾写 `canary` 与 `delta`。
- [src/alloc.c:L122](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L122)：`return block;`——块地址就是返回值，没有任何加减偏移。

#### 4.3.4 代码实践

**实践目标**：验证「块地址即用户指针、且弹出后头 8 字节必为 0」。

```c
/* 示例代码：观察快路径弹块后的块首 8 字节 */
#include <stdio.h>
#include <string.h>
#include <mimalloc.h>

int main(void) {
  unsigned char buf[8];
  for (int i = 0; i < 5; i++) {
    void* p = mi_malloc(100);            /* 连续分配，前 4 次几乎必走快路径 */
    memcpy(buf, p, 8);                   /* 读出块的头 8 字节 */
    unsigned long long first = 0;
    memcpy(&first, buf, 8);
    printf("p=%p  first8=%llx\n", p, first);
    /* 故意不释放，让后续分配继续从 free list 弹块 */
  }
  return 0;
}
```

1. 用 release 构建编译链接（`-lmimalloc`），运行观察输出。
2. 再用 debug 构建（库名 `libmimalloc-debug`，见 u1-l2）编译同样的程序，对比输出。

**需要观察的现象 / 预期结果**：release 下每次 `first8` 都应为 `0`——因为 [src/alloc.c:L55](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L55) 的 `block->next = 0` 无条件执行。debug 下块首应变成 `d0d0d0d0...`——因为 L91 的 memset 用 `MI_DEBUG_UNINIT`（0xD0）重新毒化了整块。若 debug 输出不是 0xD0 图样，请核对是否真的链接了 debug 库。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：快路径上一次原子操作都没有，为什么是安全的？假如另一个线程同时在释放这个页上的块呢？
**答案**：`free`、`used`、`local_free`、`block_size` 都是**只有拥有线程能碰**的非原子字段（u3-l2 的所有权规则：非原子字段的访问前提是持有页所有权）。跨线程释放走的是 `_Atomic` 的 `xthread_free` 链表（一次 CAS），完全不触碰 `free` 和 `used`。所以拥有线程在快路径上做的普通读写不可能与他人竞争——这就是「multi-sharding」设计换来的结果。

**练习 2**：为什么 `used` 要在判断 `block == NULL` **之前**就读出来，而不是等进了快路径再读？
**答案**：为了把 `page->free` 和 `page->used` 两次装载**并行发射**给内存子系统（它们在同一条缓存行，但流水线上仍是两次访存）。如果 `used` 的读被排到分支之后，快路径就要多等一次访存延迟。作者甚至加了一行内联汇编屏障（L43-L45）强制编译器维持这个顺序，详见 4.4。

**练习 3**：`mi_zalloc(1024)` 和 `mi_zalloc(1032)` 都要清零内存，两者的清零成本来源一样吗？
**答案**：不一定。快路径内的清零在 `mi_page_malloc_zero` 里（L94-L102）：若目标页 `free_is_zero == true`（通常是刚扩展的新块）则几乎免费；否则 `_mi_memzero_aligned` 清零整个块。而 `mi_zalloc(1032)` 超过 1024 走 generic 路径，清零逻辑由 `_mi_malloc_generic` 一侧处理（u4-l2 展开）。共同点是两者都按 `block_size`（而非请求尺寸）清零——源码注释 L85 明确写 "we need to zero the full block size (issue #63)"。

### 4.4 快路径的代码生成：forceinline、likely 与一次编译器屏障

#### 4.4.1 概念说明

把 4.1 的漏斗写成普通 C 函数调用也能工作，但每次 `mi_malloc` 要付出 4~5 次 call/ret 与寄存器保存恢复的代价。mimalloc 用了一组「代码生成合同」来保证编译器产出理想机器码：

| 手段 | 定义位置 | 作用 |
| --- | --- | --- |
| `mi_decl_forceinline` | internal.h L33-L48 | 强制内联（GCC/Clang 是 `__attribute__((always_inline)) inline`） |
| `mi_likely` / `mi_unlikely` | internal.h L81-L90 | `__builtin_expect`，把快路径摆成直线代码、慢路径甩出 |
| `__asm("" : : : "memory")` | alloc.c L43-L45 | 纯编译器屏障，锁定 `used` 读与判空的相对顺序 |
| `mi_decl_cache_align` | internal.h L31 | 64 字节对齐，防伪共享 |
| `mi_decl_restrict` / `mi_attr_malloc` | mimalloc.h L69-L70 | 告诉编译器返回指针是新内存、不与已有指针别名 |
| `_mi_externs[]` | alloc.c L956-L976 | 强制为 `extern inline` 函数生成 out-of-line 副本 |

#### 4.4.2 核心流程

编译器视角下这段代码的「理想形态」：

```
        ┌──────────────────────────────┐
        │ TLS 装载 theap               │  1 条指令（fs: 寻址）
        │ 比较 size ≤ 1024             │  1 条
        │ ja  generic                  │  1 条（预测不跳）
        │ 装载 pages_free_direct[idx]  │  1 条
        │ 装载 page->free              │  1 条 ┐
        │ 装载 page->used              │  1 条 ┘ 并行发射
        │ 测试 + je generic            │  2 条
        │ 装载 block->next             │  1 条
        │ 写 0 到 block->next          │  1 条
        │ 写 next 到 page->free        │  1 条
        │ 加 1 并写回 page->used       │  2 条
        │ 返回 block                   │
        └──────────────────────────────┘
generic:  ← 冷代码，单独放远处
        call _mi_malloc_generic
```

源码注释声称「约 7 条指令、一次判断」（alloc.c L31），指的就是中间这段弹块核心（不含 TLS 与直查的入口部分，且随编译器版本略有出入）。无论精确数字是 7 还是 12，**关键在结构**：直线、无调用、无原子、只有两个几乎总被预测正确的分支。

#### 4.4.3 源码精读

**强制内联**：

- [include/mimalloc/internal.h:L43-L48](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L43-L48)：GCC/Clang 下 `mi_decl_forceinline = __attribute__((always_inline)) inline`。注意 L44-L47 的特例：**ASAN 构建下退化为普通 `inline`**——ASAN 插桩会让函数膨胀，强行多层内联反而撑爆代码体积与指令缓存。
- [src/alloc.c:L32](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L32)、[L133](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L133)、[L190](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L190)、[L218](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L218)：漏斗每一层都带这个修饰。配合 u1-l3 讲过的「翻译单元合并」（alloc.c 直接 include free.c / alloc-override.c），整条链在**同一个翻译单元内**，内联不会被链接边界打断。

**分支提示**：

- [include/mimalloc/internal.h:L81-L90](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L81-L90)：`mi_likely/mi_unlikely` 映射到 `__builtin_expect`（C++20 下用 `[[likely]]` 属性）。
- 使用点：[src/alloc.c:L220](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L220)（`size <= MI_SMALL_SIZE_MAX` 几乎总真）、[src/alloc.c:L86](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L86)（`!zero` 对 `mi_malloc` 总真）。

**编译器屏障**：

- [src/alloc.c:L43-L45](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L43-L45)：`__asm("" : : : "memory")` 是一条**空汇编**，但 clobber 列表里写了 `memory`，等于告诉编译器「这条伪指令可能读写任何内存」。效果是编译器不敢把 `used` 的装载下沉到 `if (block == NULL)` 之后——否则一旦进入慢路径的函数调用，寄存器分配会被打乱，快路径也得多搬一次数据。它**不生成任何机器指令**，纯粹是给优化器的约束。

**别名提示**：

- [include/mimalloc.h:L69-L70](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L69-L70)：GCC/Clang 下 `mi_attr_malloc = __attribute__((malloc))`、`mi_decl_restrict` 为空；MSVC 下 `mi_decl_restrict` 是 `__declspec(restrict)`（L51-L55）。两者都向编译器承诺「返回的指针指向新内存，不与任何现存指针别名」，调用方在 `p = mi_malloc(n)` 之后可以放心重排对旧对象的访问。

**强制生成函数体**：

- [src/alloc.c:L956-L976](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L956-L976)：`_mi_externs[]` 数组对一串函数**取地址**。C 标准里 `inline`（不带 `extern`）的函数定义不保证生成可链接的函数体；这里通过取地址强迫编译器为 `_mi_theap_malloc_zero`、`mi_theap_malloc` 等生成 out-of-line 副本，供 page.c、alloc-aligned.c 等其他翻译单元调用。文件尾注释写得很直白："ensure explicit external inline definitions are emitted!"。

#### 4.4.4 代码实践

**实践目标**：在成品库里亲眼看到快路径的机器码（不用重新配置 MI_SEE_ASM 的前置练习）。

1. 用 u1-l2 的方法构建 release 共享库。
2. 执行 `objdump -d --no-show-raw-insn out/release/libmimalloc.so | awk '/<mi_malloc>:/,/^$/'`（或用 `gdb` 里 `disassemble mi_malloc`）。
3. 在反汇编里找到：一条 TLS 装载（`mov rax, fs:...`）、一次与 1024 的比较、以及**恰好一处** `call ...<_mi_malloc_generic>`。
4. 数一数从函数入口到 `ret` 之间直线段的指令条数。

**需要观察的现象 / 预期结果**：`mi_malloc` 是真实导出的函数符号（[src/alloc.c:L256](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L256) 没有 `inline` 修饰，且被 L957-L976 的取地址强制生成），所以一定能反汇编到。预期整个函数体是几十条以内的直线代码加一个对 `_mi_malloc_generic` 的调用；具体条数随编译器版本与是否开 LTO 变化——待本地验证。若开的是 `MI_OVERRIDE` 构建且系统开了 `-fno-builtin`，`malloc` 符号还可能与它同地址（u2-l1 讲过 alias 手法）。

#### 4.4.5 小练习与答案

**练习 1**：`__asm("" : : : "memory")` 与 `mi_atomic_*` 原子操作里出现的 memory order 屏障是一回事吗？
**答案**：不是。这里的 `asm`屏障只约束**编译器**的指令排布（编译期），不生成任何 CPU 屏障指令（运行期零成本）；原子操作的 acquire/release 语义约束的是**处理器**的内存可见性顺序。快路径上没有任何原子操作，这条 asm 屏障解决的是纯粹的代码生成质量问题。

**练习 2**：为什么 `mi_page_malloc_zero` 不直接写成 `page->used++`？
**答案**：功能上等价，但 `used = page->used`（早读）+ `page->used = used+1`（晚写）的写法把「读」提前到判空之前，与 `page->free` 的读并行；若写成 `page->used++`，编译器可能把读合并进自增指令、排到分支之后，快路径就串行多等一次访存。配合 asm 屏障，作者把理想的指令排布固化了下来。

**练习 3**：ASAN 构建下为什么放弃 `always_inline`？
**答案**：ASAN 会在每次访存前后插桩检查，函数体膨胀数倍；把 4 层漏斗全部强内联会把 `mi_malloc` 撑得非常大，污染指令缓存、拖慢正常路径。所以 [include/mimalloc/internal.h:L44-L47](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L44-L47) 在 `MI_TRACK_ASAN` 时把 `mi_decl_forceinline` 降级为普通 `inline`，把内联决策还给编译器。

## 5. 综合实践

**任务：亲手数出快路径的访存次数，并用汇编核对「约 7 条指令」的注释。**

这是本讲规格中指定的实践，分两步：先做**源码标注**（静态），再做**汇编核对**（动态产物）。

### 第一步：源码标注

打开 [src/alloc.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c)，从 `mi_theap_malloc_small_zero`（[L190](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L190)）进入，逐行标注，直到 `mi_page_malloc_zero` 返回。为每一行标三件事：

1. **release 下是否存在**（`mi_assert_internal`、`MI_STAT`、`MI_PADDING`、`MI_GUARDED`、`mi_track_*` 全部标注为「无」）；
2. **是否访存**，读还是写，落在哪个对象（TLS / theap / page / block）；
3. **是否分支或调用**。

参考答案（release、64 位、Linux、`zero=false`）：

| # | 语句 | 访存 | 目标 |
| --- | --- | --- | --- |
| 1 | `_mi_theap_default()`（经调用方传入） | 读 | TLS 变量 `__mi_theap_default` |
| 2 | `_mi_theap_get_free_small_page`：算 wsize 下标 | 无 | 寄存器运算 |
| 3 | `theap->pages_free_direct[idx]` | 读 | theap（结构体首字段） |
| 4 | `page->free` | 读 | page 头（第 1 条缓存行） |
| 5 | `page->used` | 读 | page 头（第 1 条缓存行） |
| 6 | `block == NULL` 判断 | 无 | 分支（预测不跳） |
| 7 | `mi_block_next` → `block->next` | 读 | block 本身 |
| 8 | `block->next = 0` | 写 | block 本身 |
| 9 | `page->free = next` | 写 | page 头 |
| 10 | `page->used = used+1` | 写 | page 头 |
| 11 | `return block` | 无 | 块地址即指针 |

**结论：5 读 + 3 写 = 8 次访存，2 个几乎总预测正确的分支，0 次原子操作，触及 3 条缓存行（TLS、theap、page+block 各算一块时为 4 个不同对象）。**

### 第二步：汇编核对

1. 配置一个开汇编输出的构建：

```bash
mkdir -p out/asm && cd out/asm
cmake ../.. -DMI_SEE_ASM=ON -DCMAKE_BUILD_TYPE=Release
make -j4
```

   `MI_SEE_ASM` 在 [CMakeLists.txt:L49](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L49) 定义，在 [CMakeLists.txt:L342-L351](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L342-L351) 生效：GCC/Clang 加 `-save-temps`，MSVC 加 `-FA`。
2. `-save-temps` 会把中间文件（`.i` 预处理、`.s` 汇编）留在编译的工作目录。用 `find . -name 'alloc.s'`（或 `find . -name '*.s'`）定位，注意 alloc.c 会连同它 include 的 free.c、alloc-override.c 一起生成一个大 `.s` 文件。
3. 在 `alloc.s` 里搜 `mi_malloc:`，从标签往下读，对照第一步的表格逐条打勾：
   - 找 `fs:` 寻址的那条 TLS 装载；
   - 找与 `$1024`（或 `0x400`）的比较；
   - 数 `mov`/`movq` 中对 `[r??]` 的读写，与表格 8 次访存一一对应；
   - 确认慢路径只是远处一段 `call _mi_malloc_generic`。
4. 顺手对比 `mi_zalloc` 的汇编：多出的清零逻辑是否被 `mi_likely(!zero)` 排到了直线代码之外。

**预期结果**：表格与汇编基本对得上；`mi_malloc` 直线段在十来条到几十条指令之间（含栈帧建立与 TLS 装载），对 `_mi_malloc_generic` 的调用只有一处。若你数出的访存多于 8 次，先检查是不是链接了 debug 库（debug 下 padding、统计、断言全都会回来，访存次数显著增加——这本身就是一次很好的对照实验）。不同编译器与优化等级下指令条数会有出入，请以本地输出为准——待本地验证。

## 6. 本讲小结

- `mi_malloc(n)`（n ≤ 1024）沿一条**四层强制内联的漏斗**下滑：取 TLS 默认 theap → 判 `size <= MI_SMALL_SIZE_MAX` → `pages_free_direct` 直查页 → `mi_page_malloc_zero` 弹块；慢路径只有一个出口 `_mi_malloc_generic`。
- 快路径最少 **5 次读 + 3 次写 = 8 次内存访问**，触及 theap、page 头、块本身三处；`free`/`used`/`block_size` 被刻意排在 `mi_page_t` 第一条缓存行内。
- `pages_free_direct` 让找页退化为「wsize 下标一次装载」：数组放在 theap 首字段、条目永不为 NULL（空时指向 `mi_page_empty`），因此线程首次分配无需特判，自然落入慢路径完成初始化。
- 弹块五连（读 next、清 next、写 free、写 used、返回块地址）**没有任何原子操作与锁**——这靠 u3-l2 的三链分工保证：`free` 链只有拥有线程能碰。
- 代码生成合同包括：`mi_decl_forceinline`（ASAN 下降级）、`mi_likely/mi_unlikely`、`mi_decl_cache_align`、`__asm("" : : : "memory")` 编译器屏障（强制 `used` 早读）、`mi_attr_malloc` 别名提示，以及文件尾 `_mi_externs[]` 强制生成 `extern inline` 的函数体。
- 源码注释自述快路径「约 7 条指令、一次判断」（alloc.c L29-L31）；综合实践用 `MI_SEE_ASM=ON`（`-save-temps`）或 `objdump -d` 可以核对。

## 7. 下一步学习建议

本讲只回答了「free list 非空时怎么办」。接下来自然要问「**空了怎么办**」——这正是下一讲 u4-l2《慢路径：mi_find_page、mi_page_fresh 与 free list 扩展》的内容：`_mi_malloc_generic` 如何收割 `local_free`/`xthread_free`、如何在页队列里找候选页、何时申请新页。建议带着两个问题去读：

1. 快路径失败后，控制流会经过哪几个「尝试」才最终向 arena 要新内存？每次尝试的成本是多少？
2. `mi_page_extend_free` 为什么**分批**初始化块（而不是一次初始化整页）？这与本讲 `free_is_zero` 的免清零捷径有什么联系？

如果你更关心释放侧，也可以先跳到 u5-l1 看 `mi_free` 的快路径——它会用与本讲完全对称的手法（页头字段排布、一次 XOR 分流、零原子操作）把块放回链表，两讲对读会加深对「布局即性能」的理解。
