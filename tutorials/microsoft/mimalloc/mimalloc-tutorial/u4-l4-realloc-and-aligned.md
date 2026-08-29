# realloc 家族与对齐分配：alloc-aligned.c 的取整策略

## 1. 本讲目标

学完本讲，你应该能够：

1. 准确说出 `mi_realloc` 「原地复用」的四个判定条件，并理解 mimalloc 里为什么不存在传统意义上的「向相邻空间扩展」。
2. 画出 `mi_malloc_aligned` 的三层决策树：小对象快路径 → 自然对齐捷径 → 过度分配回退（含超大对齐走单例页）。
3. 解释 `_aligned_at` 偏移版本的语义：保证的是「指针加偏移后对齐」，而不是指针本身对齐。
4. 会用 `mi_posix_memalign`、`mi_aligned_alloc`、`mi_valloc` 等 POSIX 适配接口，并知道它们在 `MI_OVERRIDE` 构建下如何顶替系统同名函数。

本讲是单元四的收尾：u4-l1~u4-l3 讲完了「新分配」的全部尺寸分支，本讲补上「改尺寸」和「要对齐」两类需求——它们复用同一套页机制，但各自多了一层决策逻辑。

## 2. 前置知识

- **对齐（alignment）**：地址 `p` 是 `a` 的倍数（`a` 为 2 的幂）时称 `p` 按 `a` 对齐。SSE/AVX 向量指令、DMA、缓存行伪共享等都要求特定对齐。位运算判断：`(uintptr_t)p & (a-1)) == 0`。
- **过度分配（over-allocation）**：对齐分配的经典手法——多要 `alignment - 1` 字节，在块内向上取整找到对齐地址，代价是平均浪费 `alignment / 2` 字节。
- **自然对齐（natural alignment）**：mimalloc 的加分项。页内块等长、块地址 = 页起点 + 序号 × 块长（u3-l2）。若块长本身是 2 的幂且不超过 4 KiB，则**每个块天然就是对齐的**，根本不需要过度分配。页起点按 64 KiB slice 对齐（u3-l4），所以块的低位地址完全由「序号 × 块长」决定。
- **usable size 与 block size**：`mi_usable_size(p)` 是该块实际可用的字节数，`mi_page_block_size(page)` 是块所在 size class 的规格（u1-l4）。realloc 的原地复用判断建立在 usable size 之上。
- **单例页（singleton page）**：一个对象独占一个 `mi_page_t`（`MI_PAGE_SINGLETON`），用于超大对象或超大对齐（u4-l3 已见过大对象版）。
- **内部指针（interior pointer）**：过度分配对齐后，返回给用户的指针不再位于块边界，而是块的「中间」，free 与 `mi_usable_size` 需要特殊处理才能找回块起点。

已修过的相关讲义：u4-l1（快路径与 `_mi_theap_malloc_zero`）、u3-l2（页与三条 free list）、u3-l4（page map 反查）。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [src/alloc.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c) | realloc 家族的全部实现：核心是 `mi_theap_realloc_zero_ex`，以及 `mi_expand`/`reallocn`/`reallocf`/`rezalloc`/`recalloc` 等变体与公开包装 |
| [src/alloc-aligned.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-aligned.c) | 对齐分配与对齐 realloc 的全部实现：三层决策、过度分配取整、`_aligned_at` 偏移版本 |
| [src/alloc-posix.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-posix.c) | POSIX/Unix/MS 传统函数的 mi 前缀版本：`mi_posix_memalign`、`mi_memalign`、`mi_valloc`、`mi_aligned_alloc`、`mi_reallocarray` 等 |
| [include/mimalloc/types.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h) | 对齐相关常量：`MI_MAX_ALIGN_SIZE`、`MI_PAGE_MAX_OVERALLOC_ALIGN` 等，以及 `MI_PAGE_SINGLETON` 页种类 |
| [include/mimalloc/internal.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h) | `mi_alignment_is_valid`、`_mi_theap_get_free_small_page` 等内联工具 |
| [src/free.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c) | 被本讲引用的 `mi_validate_ptr_page`（page map 反查）与 `_mi_page_usable_size`（内部指针分支） |

三个常量先记牢（均定义于 [types.h:463-467](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L463-L467)）：

| 常量 | 值 | 含义 |
| --- | --- | --- |
| `MI_MAX_ALIGN_SIZE` | 16 | `max_align_t` 大小，普通 malloc 的默认对齐（[types.h:37-38](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L37-L38)） |
| `MI_PAGE_MAX_START_BLOCK_ALIGN2` | 4 KiB | 2 的幂块长在此值以内可保证自然对齐 |
| `MI_PAGE_MAX_OVERALLOC_ALIGN` | 64 KiB（= 一个 arena slice） | 对齐超过它就不再过度分配，改走 OS 单例页 |

## 4. 核心概念与源码讲解

### 4.1 realloc 家族：原地复用的四条件判定

#### 4.1.1 概念说明

`realloc(p, newsize)` 的语义是「把 p 指向的块改成 newsize 字节」。传统分配器（如 glibc 的 ptmalloc）会在物理上尝试**原地扩展**——看相邻高地址的块是否空闲，空闲则合并。mimalloc 的页是**定长块容器**（一个页只装一种 size class，u3-l2），块与块之间没有「相邻可合并空间」，所以根本不存在原地扩展。

mimalloc 的策略更简单：**如果新尺寸还装得进当前这个块，就直接把原指针还给你**——一次访存级别的判断，零拷贝、零分配。装不下（或者缩得太狠浪费超过一半）才走「搬家」路径：新分配 → 拷贝 → 释放旧块。

#### 4.1.2 核心流程

```text
mi_realloc(p, newsize)
 └─ mi_theap_realloc(theap, p, newsize)
     ├─ p == NULL ?  → 退化为 mi_theap_malloc           （realloc(NULL,n) ≡ malloc(n)）
     └─ _mi_theap_realloc_zero → mi_theap_realloc_zero_ex(theap, p, newsize, zero)
         ├─ page map 反查: page = mi_validate_ptr_page(p)   （失败 → 返回 NULL，不动 p）
         ├─ size = _mi_page_usable_size(page, p)
         ├─ 原地复用判定（全部满足则原样返回 p）:
         │    ① newsize <= size        （装得下）
         │    ② newsize >= size / 2    （缩小不超过一半，否则浪费太重）
         │    ③ newsize > 0            （0 走搬家路径，见下）
         │    ④ mi_page_heap(page) == _mi_theap_heap(theap) （块属于目标堆）
         └─ 搬家路径:
              newp = _mi_theap_malloc_zero(theap, newsize, /*zero=*/false)
              copy_size = min(size, newsize)
              （zero 变体只清零 copy_size 之后的部分）
              memcpy(newp, p, copy_size);  mi_free(p)     （newp 成功才 free p）
```

注意第 ③ 条配合注释：`realloc(p, 0)` 并不返回 NULL，而是搬去一个零尺寸块（与 `mi_malloc(0)` 行为一致），并且会把首字节置 0（兼容某些程序的预期，见 issue #725）。

#### 4.1.3 源码精读

核心函数是 [src/alloc.c:379-439](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L379-L439) 的 `mi_theap_realloc_zero_ex`。先看头部对 `p == NULL` 与非法指针的处理：

- [alloc.c:385-399](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L385-L399)：`p==NULL` 时 `page/size` 置零，后面自然走搬家路径等价于 malloc；否则先用 `mi_validate_ptr_page`（实现于 [free.c:216-219](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L216-L219)，内部就是 u3-l4 讲过的 page map 反查）拿到所属页，再取 `size = _mi_page_usable_size(page, p)`。反查失败直接返回 NULL，原指针不被释放。

原地复用的四条件就在这几行：

- [alloc.c:401-416](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L401-L416)：`newsize<=size && newsize>=(size/2) && newsize>0` 三个尺寸条件一次判断；命中后再核对 `mi_page_heap(page)==_mi_theap_heap(theap)`（同一堆）才 `return p`。第④条是为 `mi_heap_realloc` 准备的：把块挪到另一个「一等堆」名下时不能原地赖着不走。

搬家路径的关键细节：

- [alloc.c:417-431](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L417-L431)：新分配时**故意不零初始化**（`false /* no zero */`），因为旧数据马上要被 `memcpy` 覆盖；`rezalloc` 变体则只清零扩展部分——`zero_start` 从 `copy_size - sizeof(intptr_t)` 向下按字对齐再开始清零，顺带把旧块最后一个字也清掉，保证 padding 区域为零（注释提到 issue #763）。`newsize==0` 时单独把首字节写 0。
- [alloc.c:432-436](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L432-L436)：`_mi_memcpy_aligned(newp, p, copy_size)` 之后才 `mi_free(p)`——**搬家成功才释放旧块**，这是 realloc「失败时原指针仍有效」契约的来源。

家族其余成员都是这颗核心的薄包装：

- [alloc.c:365-377](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L365-L377)：`mi_expand`（Microsoft 风格「原地扩或失败」）。注意 `#if MI_PADDING` 分支：debug 构建有 padding 时**直接返回 NULL** 不做原地扩；release 构建则退化为「装得下就返回 p」。
- [alloc.c:445-453](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L445-L453)：`mi_theap_realloc` 对 `p==NULL` 单独优化成 `mi_theap_malloc`，省掉进入 `realloc_zero_ex` 的开销。
- [alloc.c:455-459](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L455-L459)：`mi_theap_reallocn` 先用 `mi_count_size_overflow` 做 `count×size` 乘法溢出检查。
- [alloc.c:463-467](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L463-L467)：`mi_theap_reallocf` 是 BSD 语义——失败时**主动释放** `p`（与标准 realloc 相反）。
- [alloc.c:486-509](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L486-L509)：公开 API `mi_realloc`/`mi_reallocn`/`mi_urealloc`/`mi_reallocf`/`mi_rezalloc`/`mi_recalloc`，全部是「取默认 theap + 转发」一行函数，与 u1-l4 看过的 `mi_malloc` 同款模式。`mi_urealloc` 额外通过出参返回搬家前后的 block size，供运行时精确追踪。

#### 4.1.4 代码实践

**实践目标**：用指针地址是否变化，直接观察「原地复用」与「搬家」两条路径的分岔。

1. 按 u1-l2 构建 release 版 mimalloc。
2. 编写下面程序（示例代码）：

```c
// re: gcc main.c -o re -I<安装目录>/include -L<安装目录>/lib -lmimalloc
#include <stdio.h>
#include <mimalloc.h>

int main(void) {
  char* p = (char*)mi_malloc(100);
  printf("初始   p=%p usable=%zu\n", (void*)p, mi_usable_size(p));

  char* q1 = (char*)mi_realloc(p, 80);    // 缩小不到一半 → 预期原地复用
  printf("缩到80 q=%p usable=%zu\n", (void*)q1, mi_usable_size(q1));

  char* q2 = (char*)mi_realloc(q1, 20);   // 缩小超过一半 → 预期搬家
  printf("缩到20 q=%p usable=%zu\n", (void*)q2, mi_usable_size(q2));

  char* q3 = (char*)mi_realloc(q2, 4000); // 扩大 → 必然搬家（跨 size class）
  printf("扩到4k q=%p usable=%zu\n", (void*)q3, mi_usable_size(q3));
  mi_free(q3);
  return 0;
}
```

3. 需要观察的现象：`缩到80` 一行指针是否与初始指针相同；其余行指针是否变化、`mi_usable_size` 如何跳档。
4. 预期结果：release 构建下 100 与 80 落在同一个 size class（`mi_good_size(100)` 与 `mi_good_size(80)` 相同），`q1 == p` 且 usable 不变；缩到 20 与扩到 4000 都会搬家（指针改变）。具体指针值待本地验证。
5. 加深一步：把 80 改成 `mi_usable_size(p)` 与 `mi_usable_size(p)/2` 附近的几个值，验证条件 ② 的边界（恰好等于 `size/2` 时仍复用）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 mimalloc 无法像 ptmalloc 那样「向高地址相邻空闲块扩展」？

答案：mimalloc 页是单一 size class 的定长块容器，块的布局在页创建时就固定（u3-l2），相邻块要么同规格（扩展无意义），要么根本不在同一页；页与页之间还隔着 arena slice 边界。它用「新尺寸是否仍装得进当前块」这一 O(1) 判断替代了物理扩展。

**练习 2**：`mi_realloc(p, 0)` 返回什么？为什么设计成这样？

答案：不返回 NULL，而是搬到一个零尺寸块（行为同 `mi_malloc(0)`），并且把首字节置 0（[alloc.c:429-431](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L429-L431)，issue #725 的兼容处理）。这样「返回 NULL」就唯一地表示失败，且失败时 `p` 不会被误释放（见 [alloc.c:380-382](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L380-L382) 的注释契约）。

**练习 3**：`mi_reallocf` 与 `mi_realloc` 在失败时的差别是什么？

答案：`mi_reallocf` 失败时会 `mi_free(p)`（[alloc.c:463-467](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L463-L467)），调用者不必再管旧指针；`mi_realloc` 失败时旧指针依然有效，调用者负责善后。

### 4.2 对齐分配入口：三层决策与自然对齐捷径

#### 4.2.1 概念说明

`mi_malloc_aligned(size, alignment)` 要回答的问题是：**这次对齐请求能不能便宜地满足？** mimalloc 按代价从低到高依次尝试三层：

1. **小对象快路径**：块本来就是对齐的，直接从 free list 头部弹一个碰巧对齐的块，几乎等同普通快路径的代价。
2. **自然对齐捷径**：整类块都天然对齐（块长是 ≤4 KiB 的 2 的幂，或块长是 4 KiB 的倍数），直接走普通 malloc，零额外浪费。
3. **过度分配回退**：多要 `alignment - 1` 字节，在块内向上取整（4.3 节）。

`_aligned_at` 版本多一个 `offset` 参数，语义是：**保证 `(uintptr_t)p + offset` 按 alignment 对齐**，`p` 本身不必对齐。这是给「结构体内部某字段需要对齐」的场景用的（对应 Windows 的 `_aligned_offset_malloc`）。

#### 4.2.2 核心流程

```text
mi_malloc_aligned_at(size, alignment, offset)
 └─ mi_theap_malloc_zero_aligned_at(theap, size, alignment, offset, zero)
     ├─ ① 合法性: alignment 必须是 2 的幂 → 否则报 EINVAL 并返回 NULL
     ├─ ② 小对象快路径（需 size ≤ 1KiB 且 alignment ≤ size）:
     │      用 pages_free_direct 直查小对象页（u3-l3）
     │      若 free 链表头满足 ((头指针+offset) & (alignment-1)) == 0
     │      → 弹出该块返回                     ← 零额外代价
     └─ ③ mi_theap_malloc_zero_aligned_at_generic:
          ├─ 尺寸上限检查（> MI_MAX_ALLOC_SIZE → EINVAL）
          ├─ 自然对齐捷径（仅 offset==0）:
          │      mi_malloc_is_naturally_aligned(size, alignment)?
          │      是 → 普通分配并断言结果确实对齐
          └─ 否则 → 过度分配回退（4.3 节）
```

自然对齐的判定逻辑（为什么「块长是 2 的幂」就免费对齐）：

设页起点为 \( P \)（按 64 KiB 对齐），块长为 \( b \)，则任一块地址为

\[ p_k = P + k \cdot b, \quad k = 0, 1, 2, \dots \]

若 \( b \) 是 2 的幂且 \( b \le 4096 \)，则 \( 64\,\mathrm{KiB} \equiv 0 \pmod b \)，于是 \( p_k \bmod b = 0 \)，即**每个块都按 \( b \) 对齐**；又因 alignment ≤ size ≤ b 且两者都是 2 的幂，块也就按 alignment 对齐。这个保证的源头在 arena 建页时：块长是 2 的幂且 ≤4 KiB 时，首块按块长对齐（[arena.c:892-898](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L892-L898)，默认 64 位构建页元数据分离、块区从 slice 起点开始，见 [arena.c:1000-1010](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1000-L1010)，对齐更强）。

#### 4.2.3 源码精读

入口原语是 [src/alloc-aligned.c:197-241](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-aligned.c#L197-L241) 的 `mi_theap_malloc_zero_aligned_at`：

- [alloc-aligned.c:200-202](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-aligned.c#L200-L202)：先验合法性。`mi_alignment_is_valid` 定义于 [internal.h:504-506](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L504-L506)，要求非零且为 2 的幂；失败走 [alloc-aligned.c:191-194](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-aligned.c#L191-L194) 的 `mi_error_bad_alignment` 报 EINVAL。注释明确「不要求 size > offset，只保证 offset 处对齐」。
- [alloc-aligned.c:213-237](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-aligned.c#L213-L237)：小对象快路径。条件 `size <= MI_SMALL_SIZE_MAX && alignment <= size`（1 KiB 以内且对齐不超过尺寸）时，用 `_mi_theap_get_free_small_page`（[internal.h:650-655](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L650-L655)，即 u3-l3 的 `pages_free_direct` 直查数组，注意入参是 `size + MI_PADDING_SIZE`）拿到当前页，若 free 链表头 `page->free` 恰好满足 `(头指针 + offset) & (alignment-1)) == 0`，就直接 `_mi_page_malloc_zero` 弹出它——与 u4-l1 快路径完全同款的代价。
- 快路径落空则转 [alloc-aligned.c:160-188](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-aligned.c#L160-L188) 的 `mi_theap_malloc_zero_aligned_at_generic`：先做尺寸上限检查（防止溢出，注释链到 glibc 的安全公告），再试自然对齐捷径。
- 自然对齐的判定在 [alloc-aligned.c:18-28](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-aligned.c#L18-L28) 的 `mi_malloc_is_naturally_aligned`：`alignment > size` 直接否；否则取 `bsize = mi_good_size(size)`（u1-l4），`bsize <= 4KiB 且为 2 的幂`，或 `alignment == 4KiB 且 bsize 是 4KiB 的倍数`（后一条是为了让 4 KiB 倍数的大块也能免 TLB 抖动地落在 4 KiB 边界）。generic 版本里 [alloc-aligned.c:172-184](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-aligned.c#L172-L184) 只在 `offset==0` 时启用该捷径，并且分配后还复核指针确实对齐——若复核失败说明判定有 bug，断言并释放重走回退（「断言是契约」的又一例）。
- 公开包装层在 [alloc-aligned.c:279-340](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-aligned.c#L279-L340)：`mi_malloc_aligned_at`/`mi_malloc_aligned`/`mi_zalloc_aligned*`/`mi_calloc_aligned*` 及对应 `mi_heap_*` 版本，全部一行转发到 theap 层，其中 `mi_calloc_aligned_at` 会先做 `mi_count_size_overflow` 溢出检查（[alloc-aligned.c:264-268](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-aligned.c#L264-L268)）。

#### 4.2.4 代码实践

**实践目标**：验证自然对齐捷径「不用过度分配也能对齐」。

1. 编写程序（示例代码），对一组 `(size, alignment)` 组合分别调用 `mi_malloc_aligned` 并打印地址低位：

```c
// al: gcc main.c -o al -I<安装目录>/include -L<安装目录>/lib -lmimalloc
#include <stdio.h>
#include <mimalloc.h>

static void try(size_t size, size_t alignment) {
  void* p = mi_malloc_aligned(size, alignment);
  printf("size=%5zu align=%5zu -> %p  %%align=%3zu good=%zu\n",
         size, alignment, p, (size_t)((uintptr_t)p % alignment),
         mi_good_size(size));
  mi_free(p);
}

int main(void) {
  try(1024, 256);   // bsize=1024 是 2 的幂 → 自然对齐捷径
  try(8192, 4096);  // bsize=8192 是 4KiB 的倍数 → 捷径第二分支
  try(100,  256);   // bsize 非 2 的幂 → 快路径/过度分配
  try(64,   64);    // 小对象快路径
  return 0;
}
```

2. 需要观察的现象：每一行 `%align` 是否恒为 0（对齐全部成立），以及 `good` 列（块规格）。
3. 预期结果：所有指针满足对齐；前两行命中捷径（无内部浪费，`mi_usable_size(p)` 即块规格）；`size=100` 一行的块规格不是 2 的幂，须依赖快路径碰运气或过度分配。具体地址待本地验证。
4. 思考题（下节揭晓）：`try(100, 64*1024)` 会走哪条路？（答案在 4.3：alignment 超过 64 KiB 上限，改走 OS 单例页。）

#### 4.2.5 小练习与答案

**练习 1**：`mi_malloc_aligned(2000, 256)` 为什么不能命中小对象快路径？

答案：快路径要求 `size <= MI_SMALL_SIZE_MAX`（1024 字节）且 `alignment <= size`。2000 > 1024，直接进 generic；随后自然对齐检查中 `mi_good_size(2000)` 不是 2 的幂，最终走过度分配（256 ≤ 64 KiB，在块内取整）。

**练习 2**：自然对齐捷径为什么限定 `bsize <= 4 KiB`？

答案：arena 建页时只对「2 的幂且 ≤4 KiB 的块长」保证首块按块长对齐（`MI_PAGE_MAX_START_BLOCK_ALIGN2`，[types.h:465](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L465)与 [arena.c:892-895](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L892-L895)）；页起点按 64 KiB 对齐只能推出「块长整除 4 KiB 的 2 的幂块全对齐」，更大的 2 的幂（如 8 KiB）不满足 `64 KiB % 8KiB` 推导链以外的保证条件，判定函数就保守地拒绝了。

**练习 3**：`mi_malloc_aligned_at(100, 4096, 64)` 的语义是什么？`p` 本身按什么对齐？

答案：保证 `(uintptr_t)p + 64` 是 4096 的倍数；`p` 本身只保证 `mi_good_size` 对应块的默认对齐（至少 16 字节，`MI_MAX_ALIGN_SIZE`）。所以打印 `p % 4096` 通常不是 0，打印 `(p + 64) % 4096` 才是 0。

### 4.3 过度分配回退与超大对齐单例页

#### 4.3.1 概念说明

当捷径全部落空，mimalloc 使用教科书式的过度分配，但按对齐大小分了两档：

- **alignment ≤ 64 KiB（`MI_PAGE_MAX_OVERALLOC_ALIGN`，恰为一个 arena slice）**：在普通页里多要 `alignment - 1` 字节，然后在块内向上取整。浪费可控（平均 `alignment/2`），且块仍在 arena 页中，享受一切页机制。
- **alignment > 64 KiB**：普通页的块根本给不起这种对齐，改为向 OS 申请对齐内存、放进一个**单例页**（`MI_PAGE_SINGLETON`）。此时 `offset` 必须为 0（暂不支持，代码里留有 todo）。

取整之后返回的指针位于块内部——这就是「内部指针」，需要给页打上 `has_interior_pointers` 标记，好让 free 与 `mi_usable_size` 知道这个块的头 8 字节存的是「指回块起点的信息」而不是普通数据（u5-l1 会看到 free 侧如何消费它）。

#### 4.3.2 核心流程

```text
mi_theap_malloc_zero_aligned_at_overalloc(theap, size, alignment, offset, zero)
 ├─ if alignment > 64KiB:
 │    offset != 0 → 报 EINVAL 返回 NULL
 │    oversize = (size <= 1KiB ? 1KiB+1 : size)        // 强制走 generic 路径
 │    p = _mi_theap_malloc_zero_ex(theap, oversize, zero, /*huge_alignment=*/alignment, &page)
 │        └→ mi_theap_malloc_generic → OS 对齐分配 + 单例页
 └─ else:
      oversize = max(size, 16) + alignment - 1          // 小尺寸也按 16 起算
      p = 普通分配 oversize 字节
 └─ 块内取整:
      poffset = ((uintptr_t)p + offset) & (alignment-1)
      adjust  = (poffset == 0 ? 0 : alignment - poffset)
      aligned_p = p + adjust
      若 aligned_p != p: 打 has_interior_pointers 标记、收缩 padding、通知跟踪器
      return aligned_p
```

取整公式展开就是：

\[ \mathrm{adjust} = \left(-(\,p + \mathrm{offset}\,)\right) \bmod \mathrm{alignment} \]

因为 2 的幂取模可以用位与实现，整个过程只有一次读、几次加减与位运算。

#### 4.3.3 源码精读

- [alloc-aligned.c:69-98](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-aligned.c#L69-L98)：两档分支。超大对齐档（L77-90）先拒绝非零 offset（[alloc-aligned.c:81-85](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-aligned.c#L81-L85) 的 todo 注释），再算 `oversize`——**当 size ≤ 1 KiB 时故意加 1 变成 1 KiB+1**，这是为了让 `_mi_theap_malloc_zero_ex` 不走小对象快路径：看 [alloc.c:229-243](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L229-L243)，`size <= MI_SMALL_SIZE_MAX` 时它会无视 `huge_alignment` 直接进小对象路径。之后带着 `huge_alignment` 参数进入 generic，由页管理走 OS 对齐分配与单例页（单例页语义见 [types.h:499-505](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L499-L505) 的枚举注释：对齐超过 `MI_PAGE_MAX_OVERALLOC_ALIGN` 的块也用它）。
- [alloc-aligned.c:91-98](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-aligned.c#L91-L98)：普通过度分配档。`oversize = (size < MI_MAX_ALIGN_SIZE ? MI_MAX_ALIGN_SIZE : size) + alignment - 1`，注释解释了为何用 16 兜底：size 为 0、alignment 为 64 KiB 时，若不多给底数会分配出一个恰 64 KiB 的块而把指针顶到块外。
- [alloc-aligned.c:102-107](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-aligned.c#L102-L107)：块内取整三行，即上面的公式。`adjust < alignment` 有断言保证。
- [alloc-aligned.c:109-128](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-aligned.c#L109-L128)：只要指针被移动过，就 `mi_page_set_has_interior_pointers(page, true)` 并 `_mi_padding_shrink`（debug 构建下收缩块尾 padding，让越界检测边界贴着 `adjust + size`）。L109-112 的注释很值得读：分配完成的一瞬间页可能已经**被遗弃**（变满后被其他机制接管，u6-l4 的 abandon），所以此后只能读页的常量字段。`has_interior_pointers` 的消费方在 [free.c:534-545](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L534-L545) 的 `_mi_page_usable_size`：带此标记的页走 `mi_page_usable_aligned_size_of` 专算内部指针。
- [alloc-aligned.c:150-155](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-aligned.c#L150-L155)：返回前用 `mi_track_align` 通知跟踪器（valgrind/ASAN 等，u9-l5）。
- MI_GUARDED 构建还有一条采样守卫路径 [alloc-aligned.c:30-49](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-aligned.c#L30-L49)，同样用过度分配再取整，细节留给 u9-l2。

#### 4.3.4 代码实践

**实践目标**：亲眼确认「超大对齐走单例页 + OS 直配」与「64 KiB 以内走块内取整」的差别。

1. 编写程序（示例代码）：

```c
// big: gcc main.c -o big -I<安装目录>/include -L<安装目录>/lib -lmimalloc
#include <stdio.h>
#include <mimalloc.h>

int main(void) {
  // 64KiB 对齐：恰好等于上限，仍在普通页内过度分配
  void* a = mi_malloc_aligned(100, 64 * 1024);
  printf("64KiB 对齐: %p  ok=%d\n", a, ((uintptr_t)a % (64*1024)) == 0);

  // 1MiB 对齐：超过上限 → OS 单例页
  void* b = mi_malloc_aligned(100, 1024 * 1024);
  printf(" 1MiB 对齐: %p  ok=%d\n", b, ((uintptr_t)b % (1024*1024)) == 0);

  // 超大对齐 + 偏移：预期失败（EINVAL）
  void* c = mi_malloc_aligned_at(100, 1024 * 1024, 16);
  printf(" 1MiB+offset: %p (预期 NULL)\n", c);

  mi_free(a); mi_free(b); mi_free(c);
  return 0;
}
```

2. 需要观察的现象：三个指针的对齐校验结果；第三次调用是否返回 NULL 并产生错误输出（`_mi_error_message`，[options.c:596-608](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L596-L608)，开启 `MIMALLOC_SHOW_ERRORS=1` 可见）。
3. 预期结果：前两次对齐成立；第三次返回 NULL。`mi_free(NULL)` 是安全的空操作。错误信息文本待本地验证。
4. 用 `MIMALLOC_SHOW_STATS=1` 运行，对比 `1MiB 对齐` 前后 arena 段统计中 huge/OS 分配计数的变化，佐证单例页不占 arena slice。

#### 4.3.5 小练习与答案

**练习 1**：过度分配档为什么要把 `size` 与 16 取较大值再加 `alignment - 1`？

答案：防止「size 很小 + alignment 很大」时块不够挪。极端例子 size=0、alignment=64 KiB：若只分配 64 KiB - 1 字节，向上取整后的指针可能正好落在块尾之外。以 `MI_MAX_ALIGN_SIZE`（16）为下限保证块内总能找到一个对齐地址（[alloc-aligned.c:95](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-aligned.c#L95) 注释原文）。

**练习 2**：`has_interior_pointers` 标记是给谁看的？

答案：给释放与查询路径看的。内部指针不在块边界，free 和 `mi_usable_size` 需要知道先从指针推导真实块起点（[free.c:537-544](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L537-L544) 按该标记分流到 `mi_page_usable_aligned_size_of`）。

**练习 3**：alignment=64 KiB 与 alignment=128 KiB 各走哪档？为什么分界线恰好是 arena slice 大小？

答案：64 KiB 走普通页内过度分配，128 KiB 走 OS 单例页。分界线是 `MI_PAGE_MAX_OVERALLOC_ALIGN = MI_ARENA_SLICE_SIZE`（[types.h:467](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L467)）：slice 是 arena 分配的最小刻度（u3-l4），对齐要求不超过它时块才可能「既在 arena 页里又满足对齐」；超过它就必须让 OS 直接给一段对齐的内存（单例页），否则一个块会横跨多个 slice，page map 的指针反查就会被破坏。

### 4.4 对齐 realloc 与 POSIX 适配层

#### 4.4.1 概念说明

对齐版 realloc 把 4.1 的四条件判定与 4.2/4.3 的对齐分配拼在一起，但多了两个前置捷径：

- alignment ≤ `sizeof(uintptr_t)`（8 或 16 字节）且 offset=0 时，普通分配的默认对齐已达标，直接退化为普通 realloc——绝大多数 `mi_realloc_aligned` 调用（比如对齐 16）根本不进对齐逻辑。
- 原地复用条件里额外要求**现指针加偏移后仍满足对齐**。

`src/alloc-posix.c` 则是把这套能力包装成 POSIX/Unix/MS 世界的历史函数名。它们看似琐碎，却是 `MI_OVERRIDE` 构建下顶替系统函数的弹药库：u2-l1 已看到 `alloc-override.c` 里 `posix_memalign`、`aligned_alloc`、`memalign`、`valloc`、`reallocarray` 等同名导出全部一行转发到这里的 `mi_` 版本。

#### 4.4.2 核心流程

```text
mi_realloc_aligned_at(p, newsize, alignment, offset)
 └─ mi_theap_realloc_zero_aligned_at
     ├─ alignment 非法 → EINVAL / NULL
     ├─ alignment ≤ sizeof(uintptr_t) 且 offset==0 → 普通 _mi_theap_realloc_zero
     ├─ p == NULL → 对齐分配（realloc≡malloc 契约）
     ├─ size = mi_usable_size(p)
     ├─ 复用判定: newsize ≤ size 且 newsize ≥ size − size/2
     │            且 ((uintptr_t)p + offset) & (alignment-1) == 0 → 原样返回 p
     └─ 搬家: 对齐分配新块 → memcpy（非对齐拷贝，因 offset 任意）→ 成功才 mi_free(p)
```

POSIX 适配一览（全部在 [src/alloc-posix.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-posix.c)）：

| 函数 | 语义 | 转发到 |
| --- | --- | --- |
| `mi_posix_memalign(&p, a, n)` | 返回错误码而非指针 | `mi_malloc_aligned` |
| `mi_memalign(a, n)` | 传统 Unix 对齐分配 | `mi_malloc_aligned` |
| `mi_valloc(n)` | 按 OS 页大小对齐 | `mi_memalign(_mi_os_page_size(), n)` |
| `mi_pvalloc(n)` | 页对齐且 size 也向上取整到页 | `mi_malloc_aligned` |
| `mi_aligned_alloc(a, n)` | C11；C11 要求 n 是 a 的倍数（该检查被注释掉以兼容现实程序） | `mi_malloc_aligned` |
| `mi_reallocarray(p, c, n)` | BSD；乘法溢出 → EOVERFLOW | `mi_realloc` |
| `mi_reallocarr(&p, c, n)` | NetBSD；通过二级指针写回 | `mi_realloc` |
| `mi__expand(p, n)` | Microsoft 原地扩展 | `mi_expand` |

#### 4.4.3 源码精读

- [alloc-aligned.c:347-357](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-aligned.c#L347-L357)：`mi_theap_realloc_zero_aligned_at` 的两个前置捷径。L352 是「小对齐退化为普通 realloc」；L355 的复用判定比 4.1 多了对齐项 `(((uintptr_t)p + offset) & (alignment-1)) == 0`，且写法为 `newsize >= (size - (size/2))`（与 `size/2` 等价）。
- [alloc-aligned.c:358-375](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-aligned.c#L358-L375)：搬家路径。与普通 realloc 有两处不同：一是 `zero_start = copy_size - sizeof(intptr_t)` **不做向下字对齐**（对比 [alloc.c:423](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L423) 用了 `_mi_align_down`），因为 offset 任意时新指针未必按字对齐；二是 L371 注释明说用 `_mi_memcpy` 而非 `_mi_memcpy_aligned`——对齐指针加偏移后源/目的都可能未按字对齐。仍然「成功才 free」。
- [alloc-aligned.c:378-382](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-aligned.c#L378-L382)：`mi_theap_realloc_zero_aligned` 再包一层「alignment ≤ 机器字 → 普通 realloc」。整个 aligned realloc/rezalloc/recalloc 的 `_at`、heap 变体矩阵在 [alloc-aligned.c:384-460](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-aligned.c#L384-L460)，全部一行转发。
- [alloc-posix.c:38-49](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-posix.c#L38-L49)：`mi_posix_memalign`。注意它严格遵守 POSIX 两条规定：出错时**不修改 `*p`**（issue #27），以及 alignment 必须是 2 的幂且 ≥ `sizeof(void*)`；分配失败返回 ENOMEM（size 为 0 时成功返回空指针不算错）。
- [alloc-posix.c:57-66](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-posix.c#L57-L66)：`mi_valloc` 只对齐到页大小；`mi_pvalloc` 更严格——先把 size 向上取整到页大小再分配（有溢出检查），这是老 System V 的语义差别。
- [alloc-posix.c:68-82](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-posix.c#L68-L82)：`mi_aligned_alloc`。C11 标准要求 size 是 alignment 的整数倍，但被注释掉的检查旁边写着原因：现实中大量程序违反此规定，检查反而制造不兼容——「不编造行为、以真实代码为准」的一手例证。
- [alloc-posix.c:84-117](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-posix.c#L84-L117)：`mi_reallocarray`/`mi_reallocarr` 都先 `mi_count_size_overflow` 防乘法溢出，失败分别设 EOVERFLOW/ENOMEM。`mi_reallocarr` 在 total==0 时释放并置空指针（NetBSD 语义）。
- [alloc-posix.c:119-123](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-posix.c#L119-L123)：`mi__expand` 包装 `mi_expand`，失败时设 errno。

#### 4.4.4 代码实践

**实践目标**：对比 `mi_posix_memalign` 与 `mi_realloc_aligned` 的行为差异，理解「错误码风格」与「指针风格」两套 API。

1. 编写程序（示例代码）：

```c
// px: gcc main.c -o px -I<安装目录>/include -L<安装目录>/lib -lmimalloc
#include <stdio.h>
#include <errno.h>
#include <mimalloc.h>

int main(void) {
  void* p = NULL;
  int r1 = mi_posix_memalign(&p, 256, 1024);            // 合法请求
  printf("r1=%d p=%p %%256=%zu\n", r1, p, (size_t)((uintptr_t)p % 256));

  void* q = (void*)0xdeadbeef;                          // 哨兵：验证出错不写 *p
  int r2 = mi_posix_memalign(&q, 300, 1024);            // 300 不是 2 的幂
  printf("r2=%d(EINVAL=%d) q 不变=%d\n", r2, EINVAL, q == (void*)0xdeadbeef);

  // 对齐 realloc：16 字节对齐 ≤ 机器字 → 退化为普通 realloc
  void* a = mi_malloc_aligned(100, 16);
  void* b = mi_realloc_aligned(a, 4000, 16);
  printf("a=%p b=%p usable(b)=%zu\n", a, b, mi_usable_size(b));
  mi_free(b); mi_free(p);
  return 0;
}
```

2. 需要观察的现象：`r2` 是否为 EINVAL、哨兵 `q` 是否未被改写；`mi_realloc_aligned(…, 16)` 是否实际走了普通 realloc 路径（从行为上无法直接看出，可结合 4.4.3 的 L352 判据推断）。
3. 预期结果：`r1=0` 且 `p % 256 == 0`；`r2=EINVAL` 且 `q` 保持哨兵值；第三个分配正常。具体地址待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`mi_realloc_aligned(p, n, 8)` 在 64 位平台上会进入对齐分配逻辑吗？

答案：不会。`alignment(8) <= sizeof(uintptr_t)(8)` 在 [alloc-aligned.c:380](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-aligned.c#L380) 直接转 `_mi_theap_realloc_zero`，因为 mimalloc 普通块至少按 `MI_MAX_ALIGN_SIZE`=16 对齐，8 字节对齐天然成立。

**练习 2**：为什么对齐 realloc 搬家时用 `_mi_memcpy` 而普通 realloc 用 `_mi_memcpy_aligned`？

答案：`_aligned_at` 版本的 offset 是任意值，返回的指针加偏移后才对齐，指针本身可能连机器字对齐都不满足，按字对齐拷贝的快路径无法使用（[alloc-aligned.c:371](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-aligned.c#L371) 注释）。

**练习 3**：`mi_aligned_alloc(64, 100)` 按 C11 标准应失败（100 不是 64 的倍数），mimalloc 会怎么处理？

答案：会成功分配。整数倍检查在 [alloc-posix.c:70-77](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-posix.c#L70-L77) 被整体注释掉，注释说明大量真实程序传入非整数倍的 size，严格执行反而破坏兼容性。

## 5. 综合实践

把本讲两条主线串起来：**同一个程序里用三种方式拿到对齐内存，并逐一指认它们各自命中了 alloc-aligned.c 的哪个分支。**

任务（示例代码，基于 release 构建）：

```c
// tour: gcc main.c -o tour -I<安装目录>/include -L<安装目录>/lib -lmimalloc
#include <stdio.h>
#include <mimalloc.h>

int main(void) {
  // ① POSIX 风格：256 对齐 + 1KiB
  void* a = NULL;
  if (mi_posix_memalign(&a, 256, 1024) != 0) { printf("① 失败\n"); return 1; }
  printf("① posix_memalign(256,1KiB): %p %%256=%zu\n", a, (size_t)((uintptr_t)a % 256));

  // ② 偏移对齐：让 (p + 64) 落在 4KiB 边界
  void* b = mi_malloc_aligned_at(256, 4096, 64);
  printf("② aligned_at(256,4KiB,off64): %p (p+64)%%4096=%zu\n",
         b, (size_t)(((uintptr_t)b + 64) % 4096));

  // ③ 对齐 realloc 扩容：搬家路径 + 对齐保持
  void* c = mi_realloc_aligned_at(b, 8192, 4096, 64);
  printf("③ realloc_aligned_at(→8KiB): %p (p+64)%%4096=%zu\n",
         c, (size_t)(((uintptr_t)c + 64) % 4096));

  mi_free(a); mi_free(c);
  return 0;
}
```

完成后请回答（对照源码写下判断依据）：

1. ① 命中哪条分支？——预期是小对象快路径（[alloc-aligned.c:220-236](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-aligned.c#L220-L236)）：size=1024 ≤ `MI_SMALL_SIZE_MAX` 且 alignment=256 ≤ size，release 下块规格 1024 是 2 的幂、天然 256 对齐，free 头校验应直接通过。**注意**：debug 构建有 MI_PADDING，`padsize = 1024 + padding` 会让块规格不再是 2 的幂，快路径可能落空转过度分配——这正好用来验证两套构建的差异（待本地验证）。
2. ② 命中哪条分支？——快路径条件 `alignment <= size` 不满足（4096 > 256），自然对齐检查中 `alignment > size` 直接否（[alloc-aligned.c:22](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-aligned.c#L22)），且 offset≠0 本就排除捷径，于是走过度分配档的块内取整（[alloc-aligned.c:91-107](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-aligned.c#L91-L107)）：实际分配 256+4095 字节。
3. ③ 为什么指针大概率变化？——8192 > usable(256+4095 取整后的块)，复用判定的 `newsize <= size` 不成立，必然搬家；新块再次经偏移对齐，`(p+64) % 4096 == 0` 保持。
4. 验证对齐：三处打印的取模结果应全为 0；②③ 打印 `p % 4096` 则**不应**恒为 0（验证你理解的是「偏移处对齐」语义）。

预期结果：三个取模校验全部通过；指针具体值与②③是否恰好同址待本地验证。

## 6. 本讲小结

- mimalloc 没有「向相邻空间扩展」的 realloc：页是定长块容器，原地路径的真实语义是**四条件复用**——`newsize ≤ usable`、`newsize ≥ usable/2`、`newsize > 0`、块属于目标堆；命中则原指针原样返回，否则「先分配后拷贝、成功才释放旧块」。
- 对齐分配是三层决策：小对象快路径（free 头碰巧对齐）→ 自然对齐捷径（块长是 ≤4 KiB 的 2 的幂或 4 KiB 的倍数，零浪费）→ 过度分配回退（`max(size,16) + alignment - 1` 后块内取整，打 `has_interior_pointers` 标记）。
- 对齐超过 `MI_PAGE_MAX_OVERALLOC_ALIGN`（64 KiB = 一个 arena slice）时改走 OS 直配的单例页，且暂不支持 offset；`size ≤ 1KiB` 时故意 +1 是为了绕开 `_mi_theap_malloc_zero_ex` 的小对象分流。
- `_aligned_at` 的语义是「指针加 offset 后对齐」；对齐 ≤ 机器字时 realloc 家族统一退化为普通版本。
- `alloc-posix.c` 的 `mi_posix_memalign`/`mi_aligned_alloc`/`mi_valloc` 等是 `MI_OVERRIDE` 构建顶替系统同名函数的转发层，严格遵守「出错不写 `*p`」「2 的幂」等 POSIX 细节，却有意豁免了 C11 的整数倍要求。

## 7. 下一步学习建议

本讲结束后，分配侧（malloc/realloc/aligned）的源码已经读完，下一讲进入**释放路径**：

- **u5-l1（mi_free 快路径）**：重点看 `mi_validate_ptr_page` 的完整实现、`has_interior_pointers` 标记如何让 free 从内部指针找回块起点——这正是本讲 4.3 埋下的伏笔。
- **u5-l2（跨线程 free）**：理解一次 CAS 推入 `thread_free` 的设计，与本讲的 realloc「成功才 free」契约互相印证。
- 延伸阅读：`mi_page_set_has_interior_pointers` 在 [src/page.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c) 中的安全原子写法，以及 [src/arena.c:892-898](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L892-L898) 中自然对齐保证的另一半（`mi_page_block_start`），它将在单元六 arena 精读时正式展开。
