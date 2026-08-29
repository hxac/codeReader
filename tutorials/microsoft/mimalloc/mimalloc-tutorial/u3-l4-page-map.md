# u3-l4 page map:从任意指针找回它的页

## 1. 本讲目标

学完本讲,你应该能够:

1. 说清楚 `mi_free(p)` 只有裸指针时,如何反查出 `p` 所属的 `mi_page_t`——这是 page map 存在的全部理由。
2. 理解 64 KiB 的 **arena slice** 是整个地址空间的"刻度",page map 的体积由这个刻度直接决定。
3. 手算出 flat 与 2-level 两种 page map 在 48 位地址空间下各占多少字节(4 GiB 虚拟保留 vs 4 MiB),并说出源码里按什么条件自动选择。
4. 精读 `mi_page_map_ensure_committed`,解释**按需 commit** 如何让进程只为真正用到的地址范围付出物理内存。
5. 知道默认 64 位构建里 `mi_free` 快路径其实走的是 `MI_PAGE_META_IS_ALIGNED` 的**纯算术对齐定位**,根本不查 page map;page map 退居"权威登记簿",服务校验、debug、secure 与 `mi_is_in_heap_region`。

## 2. 前置知识

本讲是单元三的收官,默认你已读过:

- **u3-l1 的五层对象模型**:subproc → heap → theap → page queue → page → block,以及 `mi_memid_t` 这张"内存产地证"。
- **u3-l2 的页内三条 free list**,尤其是「free 快路径的第一步就是拿到 `page`」这件事——本讲专门回答那个 `page` 是怎么来的。

再补充三个本讲反复用到的概念:

- **反查(inverse mapping)**:malloc 方向是 `page → 指针`,很好算(页起点 + 块序号 × 块长);free 方向是 `指针 → page`,没有现成算式(块地址不携带页信息),必须有一张全局表。这张表就是 page map。
- **基数树(radix tree)**:把键(这里是地址)按位分段、逐级索引的多叉树。两级基数树 = 一次数组下标 + 一次指针解引用,类似页表的思想。
- **reserved vs committed**:现代 OS 允许先**保留**(reserve)一大段虚拟地址空间(不占物理内存、不占页表项),用到哪里再**提交**(commit)哪里。page map 正是靠这个技巧,敢一次性保留几 MiB(甚至 flat 模式下 4 GiB)虚拟空间,而物理内存只随实际使用量增长。这一对概念在 u6-l2(os.c)会全面展开,本讲先用起来。

一个术语约定:mimalloc 源码里 "page map" 指本讲的全局反查表;而 OS 的 4 KiB 内存页,源码里写作 `os_page_size`。两者不要混淆。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [include/mimalloc/internal.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h) | page map 的**数据结构与全部查询 inline**:`mi_page_map_t`、`_mi_page_map_index`、`_mi_checked_ptr_page`、`_mi_aligned_ptr_page`,以及 `_mi_ptr_page` 的编译期分发 |
| [src/page-map.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-map.c) | page map 的**实现**:初始化、按需 commit、子表分配、页的注册/注销,以及导出 API `mi_is_in_heap_region` |
| [include/mimalloc/types.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h) | 一切尺寸常量的源头:`MI_ARENA_SLICE_SHIFT`(64 KiB 刻度)、`MI_PAGE_META_IS_ALIGNED` 开关、`MI_PAGE_META_ALIGNMENT`(256 MiB)、`mi_page_t.self` 字段 |
| [include/mimalloc/bits.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/bits.h) | 编译期选择:`MI_MAX_VABITS` / `MI_MIN_VABITS` / `MI_PAGE_MAP_FLAT` |
| [src/free.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c) | page map 最大的"客户":free 快路径开头反查页 |
| [src/arena.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c) | 另一个"客户":页从 arena 诞生时把自己注册进 page map,并初始化 `self` 指针 |

## 4. 核心概念与源码讲解

本讲的最小模块:

1. 为什么需要 page map:free 只有一个指针
2. 地址刻度与两种形态:64 KiB slice、flat vs 2-level
3. 2-level page map 精读:查找、注册与按需 commit
4. MI_PAGE_META_IS_ALIGNED:免查 page map 的对齐快速路径

### 4.1 为什么需要 page map:free 只有一个指针

#### 4.1.1 概念说明

回忆 u3-l2:`mi_free` 的快路径要从 `mi_page_t` 里读 `xthread_id`(判断块是不是本线程释放的)、`local_free`/`xthread_free`(决定挂到哪条链)。可是调用方交给 `mi_free` 的只有一个裸指针 `p`——块地址本身不携带"我属于哪个页"的任何信息。

分配方向没有这个问题:`mi_page_malloc` 手里本来就有 page,块地址 = 页起点 + 序号 × 块长,纯算术。释放方向则是**反问题**:给定任意地址(还可能是悬垂指针、别家分配器的指针、甚至野指针),回答两件事:

1. 它落在哪个 `mi_page_t` 管辖的地址区间里?
2. (校验型接口还要回答)它到底是不是 mimalloc 的内存?

page map 就是这张全局登记表:internal.h 里一句话点题——

> [include/mimalloc/internal.h:666-668](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L666-L668) "The page map maps addresses to `mi_page_t` pointers"(page map 把地址映射到 `mi_page_t` 指针)。

#### 4.1.2 核心流程

以 `mi_free(p)` 为例,查页发生在整条链路的最前面:

```text
mi_free(p)
  └─ mi_validate_ptr_page_nonnull(p, ...)     // free.c: 查页 + 校验
       ├─ 默认构建: _mi_aligned_ptr_page0(p)  // 4.4 的算术路径,不查表
       │    └─ page = load(page->self)        // 读 self 指针
       ├─ 其他构建: page = _mi_ptr_page(p)    // internal.h 分发 → 查 page map
       └─ 得到 page
  └─ mi_free_nonnull(p, page, ...)            // u3-l2/u5-l1 的三条链表分流
```

配套的**写路径**(登记/注销)发生在页的生命周期两端:arena 把一片 slice 切成一个新页时调用 `_mi_page_map_register(page)`,把该页覆盖的每一个 slice 在 page map 里都指向这个 page;页被归还给 arena 时调用 `_mi_page_map_unregister` 把这些条目清空。于是 page map 的不变式是:

> **凡是落在某个存活 mimalloc 页地址区间内的指针,沿 page map 一定能找回那个页;否则查表结果为 NULL。**

#### 4.1.3 源码精读

free 快路径里查页的那段(注意 `#if MI_PAGE_META_IS_ALIGNED` 的两个分支——默认 64 位构建走上面,其余走下面的 page map):

- [src/free.c:182-211](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L182-L211):`mi_validate_ptr_page_nonnull`。`MI_PAGE_META_IS_ALIGNED` 时用 `_mi_aligned_ptr_page0(p)` 直接算出页元数据槽位,再一次 acquire 载荷 `page->self`;否则退到 `page = _mi_ptr_page(p)` 查 page map,查不到(NULL)就当作非法指针拒绝。
- [src/free.c:223-229](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L223-L229):`mi_free_nonnil` 开头立刻用查到的 `page` 读 `xthread_id`——这就是 u3-l2 讲过的 XOR 分流,可见"先有 page 才有一切"。

page map 的统一入口 `_mi_ptr_page`,按构建模式在编译期四选一:

- [include/mimalloc/internal.h:796-807](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L796-L807):`_mi_ptr_page` 的分发链——`MI_SECURE || MI_FREE_IS_CHECKED` 用**带校验**的 `_mi_checked_ptr_page`;否则 `MI_PAGE_META_IS_ALIGNED` 用对齐算术;否则 debug 构建用 checked,release 用**不校验**的 `_mi_unchecked_ptr_page`。也就是说:free 越"需要防野指针"(secure/checked),越依赖 page map;纯性能模式则尽量绕开它。

导出 API `mi_is_in_heap_region` 是 page map 的"公开脸面",直接复用安全查页:

- [src/page-map.c:508-515](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-map.c#L508-L515):`_mi_safe_ptr_page` 只是转调 `_mi_checked_ptr_page`;`mi_is_in_heap_region(p)` 等价于"查表非 NULL"。

#### 4.1.4 代码实践

1. **实践目标**:用 `mi_is_in_heap_region`(page map 的唯一公开出口)亲手触摸这张表,并体会"它跟踪的是**页**,不是**块**"。
2. **操作步骤**:
   - 构建 release 版库(见 u1-l2:`mkdir -p out/release && cd out/release && cmake ../.. && make`)。
   - 编写下面的探针程序并链接 mimalloc(示例代码,非仓库文件):

   ```c
   // probe.c(示例代码)
   #include <stdio.h>
   #include <mimalloc.h>

   int main(void) {
     int stack_var = 0;
     void* p1 = mi_malloc(32);          // 小对象
     void* p2 = mi_malloc(300 * 1024);  // 大对象(专属页)
     printf("NULL        : %d\n", mi_is_in_heap_region(NULL));
     printf("stack       : %d\n", mi_is_in_heap_region(&stack_var));
     printf("p1 (32B)    : %d  %p\n", mi_is_in_heap_region(p1), p1);
     printf("p2 (300KiB) : %d  %p\n", mi_is_in_heap_region(p2), p2);
     mi_free(p1);
     printf("p1 after mi_free: %d\n", mi_is_in_heap_region(p1));
     mi_free(p2);
     return 0;
   }
   ```

   - 编译运行:`gcc probe.c -o probe -I<安装路径>/include -L<安装路径>/lib -lmimalloc && ./probe`(路径以你 u1-l2 的安装/构建目录为准)。
3. **需要观察的现象**:前两行输出 `0`;`p1`、`p2` 两行输出 `1`,且两个地址相距通常在 MB 级别以上(来自不同 arena 区域)。
4. **预期结果**:关键在最后一行——`mi_free(p1)` 之后地址大概率**仍然**返回 `1`。因为 free 只是把块挂回页内 free list,页本身通常还活着、还在 page map 里登记着。这正说明 page map 的粒度是页:它回答"这个地址是否属于某个 mimalloc 页",不回答"这个块是否已被释放"。
5. 具体输出数值依平台与构建而异,**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**:为什么 malloc 方向不需要 page map,free 方向需要?
**答案**:malloc 手里已有 page 与块长,块地址 = 页起点 + `序号 × block_size`,是纯前向算术;free 只有调用方给的裸地址,块内也不存页指针(空闲块那 8 字节要留给 free list 的 `next`),属于反问题,必须靠全局表(或 4.4 的对齐布局约定)反查。

**练习 2**:`_mi_ptr_page` 在哪几种构建下会走"带校验"的查表路径?
**答案**:见 internal.h 分发链——`MI_SECURE || MI_FREE_IS_CHECKED` 优先;其次 `MI_DEBUG` 构建也走 `_mi_checked_ptr_page`;只有普通 release(且未启用对齐优化)才走 `_mi_unchecked_ptr_page`。

**练习 3**:调用 `mi_is_in_heap_region(p)` 返回真,能证明 `p` 是合法未释放的块吗?
**答案**:不能。它只证明 `p` 落在某个仍在 page map 中登记的 mimalloc 页的地址区间里;块可能已被释放(页还活着),`p` 甚至可以是页内任意偏移的字节地址,不必是块起点。

### 4.2 地址刻度与两种形态:64 KiB slice、flat vs 2-level

#### 4.2.1 概念说明

page map 要为"每一个可能被分配的地址"服务,但显然不能按字节建表——必须先选一个**刻度**:把地址空间切成等大的格子,每格一个表项。mimalloc 选的刻度就是 u3-l3 提过的 **arena slice**:

- [include/mimalloc/types.h:189-197](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L189-L197):`MI_ARENA_SLICE_SHIFT = 13 + MI_SIZE_SHIFT`,64 位下即 \(13 + 3 = 16\),一个 slice \(2^{16}\) 字节 = 64 KiB(注释同时写明 32 位平台为 32 KiB;secure≥5 且 OS 页 16 KiB 时放宽到 128 KiB)。
- [include/mimalloc/types.h:213](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L213):`MI_ARENA_SLICE_SIZE = 1 << MI_ARENA_SLICE_SHIFT`,注释"arena's allocate in slices of 64 KiB"。

为什么是 64 KiB?它同时是最小页(`MI_SMALL_PAGE_SIZE`,见 [include/mimalloc/types.h:227-229](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L227-L229):小/中/大页 = 64 KiB / 512 KiB / 4 MiB,全是 slice 的整数倍)与 arena 批发内存的最小单位。**页边界永远落在 slice 边界上**,于是"指针属于哪个页"可以分解为"指针落在哪几个 slice、这些 slice 登记到哪个页"。刻度越大表越小,但最小页也被抬得越大——64 KiB 是权衡结果。

有了刻度,表的**形态**有两条路,源码用 `MI_PAGE_MAP_FLAT` 二选一:

- **flat(一维数组)**:每 slice 一个字节,直接以 `地址 >> 16` 为下标。单次访存、编码巧妙,但表长 \(2^{\text{vabits}-16}\)——48 位空间要 4 GiB **虚拟**保留。
- **2-level(两级基数树)**:顶层是子表指针数组,每个子表管一段地址。两次访存,但保留量骤降。

#### 4.2.2 核心流程

选择逻辑在 bits.h(编译期固定,不看运行时地址宽度):

```text
MI_MAX_VABITS(平台最大虚拟地址位数)
  MI_ARCH_X64            → 47   // x86-64 规范地址只有低 128 TiB
  其他 64 位             → 48
  32 位                  → 32

MI_PAGE_MAP_FLAT = 1  当且仅当  MI_MAX_VABITS ≤ 40
                     且 非 Apple、非 MI_SECURE、非 MI_FREE_IS_CHECKED
                     且 MI_FREE_USE_PAGEMAP(用户显式定义的构建宏,仓库默认不定义)
→ 由于最后一个条件默认不成立,所有默认构建都走 MI_PAGE_MAP_FLAT = 0,即 2-level;
   flat 是专为小地址空间(32 位等)准备的"手动挡"选项
```

两种形态的体积账(64 位,slice = 64 KiB):

\[
\text{flat 表长} = 2^{\text{vabits} - 16} \text{ 字节},\qquad
\text{2-level 顶层} = 2^{\text{vabits} - 16 - 13} \times 8 \text{ 字节}
\]

- 48 位:flat = \(2^{32}\) B = **4 GiB**;2-level 顶层 = \(2^{19} \times 8\) B = **4 MiB**,另加每个在用子表 64 KiB。
- x86-64 实际按 47 位预留:顶层 = \(2^{18} \times 8\) B = **2 MiB**。
- 32 位(4 GiB 空间):flat 表只有几十 KiB 量级(源码注释按 64 KiB slice 估为 64 KiB;32 位平台 slice 实为 32 KiB,则需 \(2^{32-15} = 128\) KiB)——这也是 flat 门槛设在 ≤40 位空间的原因:小地址空间里表足够小,一维平铺换单次访存才划算;而 64 位平台若用 flat 就要 4 GiB 级别的虚拟保留,不划算,于是改用 2-level。

这正是"64 KiB slice 粒度决定 page map 体积"的定量含义:刻度每放大一倍,两种表的字节数都减半。

#### 4.2.3 源码精读

- [include/mimalloc/bits.h:119-128](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/bits.h#L119-L128):`MI_MAX_VABITS` 三分支定义,x64 特判为 47(注释:canonical address 受限)。
- [include/mimalloc/bits.h:130-137](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/bits.h#L130-L137):`MI_MIN_VABITS`(64 位下 = 43,约 8 TiB)——page map 顶层**至少**全量 commit 的地址范围,给 `_mi_checked_ptr_page` 一条免判断的快路径。
- [include/mimalloc/bits.h:139-146](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/bits.h#L139-L146):`MI_PAGE_MAP_FLAT` 的选择条件,与上面流程一致。
- flat 形态的编码注释:[src/page-map.c:18-26](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-map.c#L18-L26)。每 slice 一个字节 `ofs`:0 = 未用;1 = 本 slice 是某页的**起始**;`1 < ofs ≤ 127` = 本 slice 属于一个页,页起点在 `((idx - ofs + 1) << 16)`。**一个字节既表达"在不在",又表达"往前偏移几个 slice 就是页头"**——查表不需要存页指针,元数据(页结构本身)就放在页头,反推地址即可。注释还给出体积账:1 TiB 空间 = 16 MiB 表、48 位 = 4 GiB、32 位 = 64 KiB。
- flat 的查询 inline:[include/mimalloc/internal.h:686-691](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L686-L691):`_mi_ptr_page_ex` 读出一个字节,按上表公式 `(slice序号 + 1 - ofs) << 16` 还原页头地址,强转回 `mi_page_t*`(flat 模式下页元数据就嵌在页头,见 [include/mimalloc/types.h:133-139](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L133-L139) 的 `MI_PAGE_META_IS_SEPARATED = 0`)。
- flat 的注册:[src/page-map.c:147-171](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-map.c#L147-L171):`_mi_page_map_register` 对页覆盖的每个 slice 写入 `i+1`——首 slice 得 1,后续递增,与查询公式严丝合缝;`mi_assert_internal(i < 128)` 说明单页最多登记 128 个 slice(uint8 编码上限)。

#### 4.2.4 代码实践

1. **实践目标**:把"刻度 → 表体积"的定量关系亲手算一遍,并与源码注释对账。
2. **操作步骤**:
   - 读 [include/mimalloc/internal.h:710-716](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L710-L716) 的注释(它给出了 48 位 → 4 MiB 的结论);
   - 独立完成下表(空格处自己算):

   | 地址空间(统一按 64 KiB slice 折算) | slice 数 \(2^{\text{vabits}-16}\) | flat 表 | 2-level 顶层 |
   | --- | --- | --- | --- |
   | 48 位 | \(2^{32}\) | 4 GiB | _____ |
   | 47 位(x64 默认) | \(2^{31}\) | 2 GiB | _____ |
   | 40 位 | \(2^{24}\) | 16 MiB | _____ |

   (32 位平台不适用此表:slice 实为 32 KiB,各列需重算;且 flat 只有在显式定义 `MI_FREE_USE_PAGEMAP` 时才会启用,默认仍走 2-level。)

3. **需要观察的现象**:你的计算值与 internal.h:713-716 注释("usually 4 MiB (for 48 bit virtual addresses)")、page-map.c:24-26 注释("A full 256 TiB address space (48 bit) needs a 4 GiB page map")逐条吻合。
4. **预期结果**:48 位顶层 = \(2^{48-16-13} \times 8 = 2^{19} \times 8 = 4\,\text{MiB}\);47 位 = \(2^{18} \times 8 = 2\,\text{MiB}\);40 位 = \(2^{40-16-13} \times 8 = 2^{11} \times 8 = 16\,\text{KiB}\)——地址空间每缩 8 倍,顶层就缩 8 倍。
5. 纯纸笔 + 源码对照,无需运行。

#### 4.2.5 小练习与答案

**练习 1**:为什么 flat 模式要求"页元数据不分离"(`MI_PAGE_META_IS_SEPARATED = 0`)?
**答案**:flat 的每个字节只存"向前偏移几个 slice",查表还原的是**页头地址**;要把它直接当 `mi_page_t*` 用,页结构必须恰好位于页头([include/mimalloc/types.h:176-178](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L176-L178) 甚至用 `#error` 禁止"flat + 分离"组合)。2-level 存的是完整指针,元数据放哪都行,于是默认分离存放(更安全、省对齐浪费)。

**练习 2**:把 slice 从 64 KiB 改成 128 KiB(其余不变),48 位空间下 2-level 顶层变成多大?
**答案**:顶层项数 \(= 2^{48}/(2^{17} \cdot 2^{13}) = 2^{18}\),即 \(2^{18} \times 8 = 2\) MiB——刻度翻倍,表减半;代价是最小页也从 64 KiB 涨到 128 KiB。

**练习 3**:为什么 `MI_MAX_VABITS` 在 x86-64 上取 47 而不是 48?
**答案**:x86-64 的 48 位规范地址形如"低 48 位 + 符号扩展",用户空间实际只能用到低一半,即 \(2^{47}\) 字节(bits.h:122-123 注释 "canonical address is limited to the first 128 TiB");按 47 预留可把表砍半。

### 4.3 2-level page map 精读:查找、注册与按需 commit

#### 4.3.1 概念说明

默认 64 位平台用的是两级基数树:

- **顶层** `mi_page_map_t`:一个变长数组 `submaps[]`,第 \(i\) 项指向负责第 \(i\) 段地址的子表;另带三个管家字段——`committed_count`(已 commit 到第几项)、`reserved_size`(保留总量)、`memid`(顶层自身内存的产地证,u3-l1 概念的又一次出现)和一把分配子表时用的 `mi_lock_t`。
- **子表(submap)**:`mi_page_t*` 数组,共 \(2^{13}\) 项,每项直接指向页结构。一个子表覆盖 \(2^{13} \times 64\,\text{KiB} = 512\,\text{MiB}\) 地址空间。

它的三个设计要点,每一个都值得品:

1. **查询是纯数组下标,没有搜索**:地址除以 slice 大小得到全局 slice 序号,高位做顶层下标、低 13 位做子表下标。所谓"基数树"在这里朴素得就是两次连续访存。
2. **顶层与子表都按需生成**:顶层一次性**保留** \(2^{19}\) 项的虚拟空间,但只 commit 前一小段;子表更是用到才分配。物理内存 ∝ 实际用到的地址范围,而不是地址空间大小。
3. **NULL 永远安全**:初始化前有一个静态空表兜底,`mi_free(NULL)` 在 mimalloc 尚未初始化时也能正确返回(代码注释标注了 issue #1341)。

#### 4.3.2 核心流程

**查询**(以带校验的 `_mi_checked_ptr_page` 为例):

```text
p ──÷64KiB──▶ u(全局 slice 序号)
idx = u >> 13          (顶层下标)
sub_idx = u & 0x1FFF   (子表下标)

pmap = load(&__ri_page_map)                 // 全局根指针
if idx >= pmap->committed_count → return NULL   // 高地址未 commit,必非法
sub  = load-acquire(&pmap->submaps[idx])    // 子表指针
if sub == NULL → return NULL                // 该段从未用过
return sub[sub_idx]                          // mi_page_t*(或 NULL)
```

**注册**(arena 造出新页时,arena.c 调用):

```text
_mi_page_map_register(page)
  idx, sub_idx, slice_count = 由页覆盖区间算出
  for 每个 slice:
    ensure_committed(idx)        // 顶层不够 → commit 扩一段
    sub = submaps[idx]
    if sub == NULL:              // 子表不存在
      加锁 → 双检 → _mi_os_zalloc 新子表 → CAS 装入(输者释放自己的)
    sub[sub_idx] = page
```

**初始化**(进程首次用到时做一次):

```text
确定 vbits(min/max 钳位、x64 特判 47)
reserve_count = 2^(vbits - 29) 项 → 一次性保留顶层 + 额外一个子表的空间
小表(≤64KiB)或选项要求或 OS 支持 overcommit → 干脆全 commit;
否则只 commit MI_MIN_VABITS 对应的前 2^14 项(=128 KiB)
把"NULL 子表"放在保留区尾部,装入 submaps[0] 并清零 → free(NULL) 安全
```

#### 4.3.3 源码精读

**数据结构**:

- [include/mimalloc/internal.h:710-721](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L710-L721):2-level 注释与 `MI_PAGE_MAP_SUB_SHIFT = 13`、`mi_submap_t = mi_page_t**`。注释给出全部关键数字:顶层通常 4 MiB(48 位)、子表 64 KiB、一个子表盖 512 MiB、48 位需要 \(2^{19}\) 个子表指针。
- [include/mimalloc/internal.h:722-730](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L722-L730):`mi_page_map_t` 定义与全局根 `__ri_page_map`。注意 `submaps[1]` 是柔性数组技巧,`committed_count` 是 `_Atomic`。

**查询 inline**(被 free、`mi_usable_size`、各处断言高频调用):

- [include/mimalloc/internal.h:732-736](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L732-L736):`_mi_page_map_index` 一次除法 + 取模拆出 `idx` 与 `sub_idx`。
- [include/mimalloc/internal.h:746-751](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L746-L751):`_mi_unchecked_ptr_page`——信任指针合法时的极简版:两次访存直接返回,连分支都没有。
- [include/mimalloc/internal.h:753-768](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L753-L768):`_mi_checked_ptr_page`——防野指针版:先比 `committed_count`(relaxed 读,一次性把"高到没 commit 的地址"挡掉),再判子表 NULL,最后才解引用。被注释掉的一段显示作者曾考虑再加 `MI_MIN_VABITS` 短路,最终用"初始化时至少 commit \(2^{14}\) 项"替代了它。

**实现(page-map.c,2-level 分支)**:

- [src/page-map.c:218-226](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-map.c#L218-L226):静态空表 `mi_page_map_empty`(`committed_count = 1`、`submaps[0] = NULL`)与根指针初始化——保证初始化前 `_mi_ptr_page(NULL) == NULL`。
- [src/page-map.c:272-357](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-map.c#L272-L357):`mi_page_map_init_once`。vbits 钳位(273-288 行:`mi_option_max_vabits` 可覆盖 → 探测 OS → x64 上 ≥48 一律取 47 → 下限 `MI_PAGE_MAP_SUB_SHIFT + MI_ARENA_SLICE_SHIFT` 与 `MI_MIN_VABITS`、上限 `MI_MAX_VABITS`);293-302 行按 vbits 算 `reserve_count` 并一次性**保留** `reserve_size + 一个子表`;298-299 行决定"全 commit 还是按需"(表小 / `mi_option_pagemap_commit` / OS 支持 overcommit 时全 commit);320-331 行至少 commit `MI_MIN_VABITS` 对应的 128 KiB;335-345 行把 NULL 子表放在保留区尾部并整体清零(issue #1087);347-354 行填字段、发布根指针。
- [src/page-map.c:236-257](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-map.c#L236-L257):`mi_page_map_commit_entries`——把顶层 commit 到覆盖 `required_idx` 为止(按 OS 页大小向上取整),247 行注释点明并发约定:**依赖未 commit 内存首次 commit 时为全零**(OS 保证),因此并发扩展不会读到脏数据。
- [src/page-map.c:259-269](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-map.c#L259-L269):`mi_page_map_ensure_committed`——先 relaxed 快读 `committed_count`,不够再 acquire 复查并扩表;这就是**按需 commit** 的全部:顶层虚拟空间 \(2^{19}\) 项早已保留,物理内存只在第一次用到某地址段时,以 64 KiB(slice 对齐,242 行 `_mi_align_up(..., MI_ARENA_SLICE_SIZE)`)为步长增长。
- [src/page-map.c:386-412](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-map.c#L386-L412):`mi_page_map_alloc_submap_at`——子表级按需:锁内双检、`_mi_os_zalloc` 分配、CAS 装入;竞争输家释放自己多分配的那份(403-407 行)。慢路径才碰锁,读侧永远无锁。
- [src/page-map.c:429-447](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-map.c#L429-L447):`mi_page_map_set_range_prim`——把页指针(或注销时的 NULL)写进覆盖到的每个 slice;外层 while 处理"页横跨两个子表"的跨界情形。
- [src/page-map.c:468-482](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-map.c#L468-L482) 与 [src/page-map.c:484-497](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-map.c#L484-L497):`_mi_page_map_register` / `_mi_page_map_unregister`——先算 `idx/sub_idx/slice_count` 再成段写入;register 里 473-475 行是惰性初始化兜底(pmap 为 NULL 时先 `_mi_page_map_init`)。
- [src/page-map.c:460-466](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-map.c#L460-L466):`mi_page_map_get_idx`——`slice_count` 按**块区**大小计算,且把 `page_size` 钳到 `MI_LARGE_PAGE_SIZE - MI_ARENA_SLICE_SIZE`("furthest interior pointer"):巨大对象专属页只需登记到"内部指针可能落到的最远处",再远的 slice 不登记,省表项。

**注册的调用点**(把读、写两侧接起来):

- [src/arena.c:1110-1114](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1110-L1114):arena 把 slice 区间造成新页后调用 `_mi_page_map_register(page)`,失败则当场把页退回 arena。从此这个页地址区间内的任何指针都能被 free 反查到。

flat 形态的对照(小地址空间的可选实现,`#if MI_PAGE_MAP_FLAT` 分支):[src/page-map.c:108-136](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-map.c#L108-L136) 是 flat 版的 `mi_page_map_ensure_committed`——它不用扩子表,而是维护一张 **commit 位图**(每比特管 64 KiB 表项,即 `MI_PAGE_MAP_ENTRIES_PER_COMMIT_BIT = MI_ARENA_SLICE_SIZE`,见 35-36 行),比特为 0 才对对应表段调 `_mi_os_commit`;117-128 行同样是"可能竞争,重复 commit 无害"的宽容语义。

#### 4.3.4 代码实践

这是本讲规格指定的核心实践,分两问:

**第一问:48 位地址空间下,2-level 顶层需要多少字节?**

1. **实践目标**:独立推导并与源码注释对账。
2. **操作步骤**:
   - 从 [include/mimalloc/types.h:195](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L195) 取 slice = \(2^{16}\) B,从 [internal.h:717](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L717) 取每子表 \(2^{13}\) 项;
   - 一个子表覆盖 \(2^{13+16} = 2^{29}\) B = 512 MiB;48 位空间 \(2^{48}\) B 需要 \(2^{48-29} = 2^{19}\) 个子表指针;每指针 8 B,共 \(2^{19} \times 8 = 2^{22}\) B。
3. **需要观察的现象**:你的推导逐步对应 internal.h:713-716 注释里的每个数字(64 KiB 子表、512 MiB 覆盖、19 位、4 MiB)。
4. **预期结果**:**4 MiB(\(2^{22}\) 字节)**;x64 默认按 47 位预留则为 2 MiB。
5. 纸笔可完成,无需运行。

**第二问:按需 commit 如何避免一次性付出全部内存?**

1. **实践目标**:读懂 `mi_page_map_ensure_committed` 的两层含义,并能向别人复述。
2. **操作步骤**:
   - 精读 [src/page-map.c:259-269](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-map.c#L259-L269),注意 261-265 行的"relaxed 快读 → acquire 复查 → 扩表"两段式;再读它调用的 [mi_page_map_commit_entries(236-257 行)](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-map.c#L236-L257),看 commit 粒度如何对齐到 64 KiB;
   - 回答三个小问题:(a) 顶层 \(2^{19}\) 项何时全部占有虚拟地址空间?(b) 物理内存从几个项开始?(c) OS 地址随机化把一个 arena 映到很高地址时,会发生几次 commit、每次多大?
3. **需要观察的现象**:(a) 进程初始化时一次性**保留**(reserve)但未必 commit;(b) 初始化只 commit `MI_MIN_VABITS`(43 位)对应的 \(2^{43-29} = 2^{14}\) 项 = 128 KiB([page-map.c:319-331](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-map.c#L319-L331));(c) 高地址段第一次被用到时,`committed_count` 被一次性推到覆盖该段的下标,commit 量按 OS 页向上取整——此后这段内的查询全是纯读。
4. **预期结果**:4 MiB 只是**虚拟**账面;一个普通进程实际为顶层付出的是 128 KiB 起步、按用到的地址段成块增长的物理内存。若想反向验证,可用 `MIMALLOC_PAGEMAP_COMMIT=1`([src/options.c:166](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L166) 的 `mi_option_pagemap_commit`)让初始化直接全量 commit,对比两种配置下程序的启动 RSS 差异(约几 MiB 量级)。
5. RSS 具体差值依平台而异,**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**:`_mi_checked_ptr_page` 为什么先比较 `idx >= committed_count` 就能直接返回 NULL,而不需要遍历什么?
**答案**:`committed_count` 单调不减地记录"顶层已 commit 到第几项"。下标超出它的地址段要么从未使用、要么超出保留范围,其 `submaps[idx]` 所在内存根本未 commit,碰都会触发段错误;先比一次整数即可安全拒绝(这正是初始化时宁可先 commit 128 KiB 也要让该检查成立的原因)。

**练习 2**:两个线程同时首次触达同一个尚无子表的地址段,会发生什么?
**答案**:都走 `mi_page_map_alloc_submap_at`(386-412 行):锁内双检后只有一方分配,另一方 CAS 失败、释放自己多分配的子表并改用赢家的。读侧始终无锁,锁只在"某地址段第一次使用"这一瞬间可能出现竞争。

**练习 3**:初始化为什么要把 NULL 子表特意放在顶层保留区的**末尾**并装入 `submaps[0]`?
**答案**:让 `free(NULL)` 在任何时刻都安全:(1) `submaps[0]` 非 NULL,`_mi_checked_ptr_page` 对低地址段不必碰未 commit 内存;(2) 子表内容被显式清零,`sub[0]` 即 NULL,`_mi_ptr_page(NULL)` 返回 NULL。335-345 行注释标明这同时修复了 issue #1087 与 #1341 两类"预初始化阶段 free(NULL)"崩溃。

### 4.4 MI_PAGE_META_IS_ALIGNED:免查 page map 的对齐快速路径

#### 4.4.1 概念说明

查表再快也是两次访存。mimalloc 在默认 64 位构建里祭出更狠的招:**让页元数据的位置本身可预测**——把每个 arena 按 256 MiB 对齐,并把该 256 MiB 区域内每个 slice 的 `mi_page_t` 结构**排成数组放在区域开头**。于是:

\[
\text{页元数据地址} = \text{base} + \left\lfloor \frac{p - \text{base}}{2^{16}} \right\rfloor \times \text{sizeof}(mi\_page\_t),\quad \text{base} = \left\lfloor p \right\rfloor_{2^{28}}
\]

一次掩码对齐 + 一次乘法,就把"指针 → 它的元数据槽位"算出来了,**完全不碰 page map**。槽位里再读一个 `self` 指针(`mi_page_t` 的第一个字段)得到真正的页结构——多 slice 的大页会把覆盖到的每个槽位的 `self` 都指向同一个页结构,于是任意内部指针一步到位。

代价与边界:

- 需要 arena 以 `MI_PAGE_META_ALIGNMENT`(64 位 = \(4096 \times 64\,\text{KiB} = 256\,\text{MiB}\))对齐,元数据区占据每个 256 MiB 段开头的 4096 个 slice(每 slice 一个 `mi_page_t`);
- **只对合法指针成立**:野指针也会被"算"出一个槽位,读它的 `self` 得到的多半是 NULL(空槽为 NULL),但语义上属于未定义行为;所以校验型接口(`mi_cfree`、debug、secure)仍然回到 page map——见 types.h:143-144 注释 "This only works if valid pointers are passed to `mi_free` though"。

#### 4.4.2 核心流程

```text
_mi_ptr_page(p) 编译期分发(internal.h:796-807)
  ├─ MI_SECURE / MI_FREE_IS_CHECKED → _mi_checked_ptr_page   (查表,安全)
  ├─ MI_PAGE_META_IS_ALIGNED        → _mi_aligned_ptr_page    (算术,默认 64 位 release)
  ├─ MI_DEBUG                       → _mi_checked_ptr_page    (查表,debug 构建)
  └─ 其余                            → _mi_unchecked_ptr_page (查表,不校验)

_mi_aligned_ptr_page(p):
  base     = p & ~(256MiB - 1)        // 对齐到段头
  slot     = &((mi_page_t*)base)[ (p - base) / 64KiB ]
  page     = load-acquire(&slot->self) // 多 slice 页 → 真正的页结构
  (debug 下再用 page map 复核一次,两者必须一致)
```

也就是说:**page map 并没有被淘汰,而是分工**——release 快路径用算术,page map 仍作为权威登记簿服务于校验路径、`mi_is_in_heap_region`、arena 自身(`mi_arena_page_at_slice` 反过来用对齐算术找页元数据)。

#### 4.4.3 源码精读

**开关与常量**:

- [include/mimalloc/types.h:141-155](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L141-L155):`MI_PAGE_META_IS_ALIGNED` 的启用条件——未启用 checked free、未强制用 page map、且元数据分离(即 2-level 形态);注释写明动机是"faster `mi_free(_small)` as we can avoid a page_map lookup"。
- [include/mimalloc/types.h:245-251](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L245-L251):`MI_PAGE_META_ALIGNED_COUNT = MI_INTPTR_SIZE × MI_BCHUNK_BITS = 8 × 512 = 4096` 个槽;`MI_PAGE_META_ALIGNMENT = 4096 × 64 KiB = 256 MiB`(注释:32 位为 32 MiB);**arena 的对齐要求即由此而来**(`MI_ARENA_ALIGNMENT = MI_PAGE_META_ALIGNMENT`)。

**结构体配合**:

- [include/mimalloc/types.h:425-428](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L425-L428):`mi_page_t` 在本模式下第一个字段是 `_Atomic(struct mi_page_s*) self`,注释"points to the actual page info (for pages that span multiple slices)"——单 slice 页 `self` 指向自己,多 slice 页所有覆盖槽位的 `self` 都指向首个槽位的真身。
- [src/arena.c:1074-1084](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1074-L1084):造页时写 `self`——先 `page->self = page`(1075 行),若页横跨多 slice,把后续每个槽位的 `self` 也指向同一页(1079-1082 行)。写侧一次赋值,读侧终身受益。

**查询 inline**:

- [include/mimalloc/internal.h:772-780](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L772-L780):`_mi_aligned_ptr_page0`——两行算术:向下对齐到 `MI_PAGE_META_ALIGNMENT` 得元数据数组基址,按 slice 序号索引。开头的注释直接说明本模式的存在意义("find it efficiently without needing to go through the page map")。
- [include/mimalloc/internal.h:782-793](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L782-L793):`_mi_aligned_ptr_page`——读 `self` 得最终页指针;`MI_DEBUG` 下额外用 `_mi_checked_ptr_page` 复核,不一致报 invalid pointer(这就是"算术路径对野指针不设防"的补丁)。
- [include/mimalloc/internal.h:796-807](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L796-L807):`_mi_ptr_page` 四路分发(前面流程图的原型)。

**读侧客户与写侧客户**:

- [src/free.c:183-198](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L183-L198):free 快路径实际用的就是 `page0 + load(self)`;191-194 行还有一层小优化——当小页恰好等于一个 slice 时,`mi_free_small` 连 `self` 的 acquire 载荷都能省(assert 直接断言槽位即页)。
- [src/arena.c:166-170](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L166-L170):`mi_arena_page_at_slice`——arena 自己按 slice 序号找页元数据,同样复用 `_mi_aligned_ptr_page`(元数据区是"自定位"的,arena 结构里不必存元数据基址)。

#### 4.4.4 代码实践

1. **实践目标**:用运行时数据验证"arena 按 256 MiB 对齐、元数据在段头"的布局,并手算一个指针的元数据槽位。
2. **操作步骤**:
   - 在 4.1.4 的探针程序里追加:

   ```c
   // 追加到 probe.c(示例代码)
   for (int i = 0; i < 4; i++) {
     void* q = mi_malloc(64);
     uintptr_t a = (uintptr_t)q;
     printf("q=%p  段基址=%p  段内偏移=%zu KiB  slice序号=%zu\n",
            q, (void*)(a & ~(uintptr_t)0xFFFFFFF),
            (a & 0xFFFFFFF) >> 10, (a & 0xFFFFFFF) >> 16);
   }
   ```

   - 运行并记录 4 个 `q` 的地址。
3. **需要观察的现象**:连续小分配的 `q` 彼此靠近,且**段基址(`a & ~0xFFFFFFF`,即低 28 位清零)相同**——它们来自同一个 256 MiB 对齐的 arena 段;`slice 序号`(段内偏移 ÷ 64 KiB)应该明显大于 0,因为段开头的 4096 个 slice 被页元数据数组占据,块数据从其后开始。
4. **预期结果**:同一批小对象落在同一 256 MiB 段、相同或相邻 slice;这与 [types.h:247](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L247) 的 `MI_PAGE_META_ALIGNMENT = 256 MiB` 一致。再用公式 `槽位 = 段基址 + slice序号 × sizeof(mi_page_t)` 手算一次,与 `_mi_aligned_ptr_page0` 的两行代码逐项对照。
5. 地址具体值依平台与 ASLR 而异,以上现象级结论**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**:既然算术路径更快,为什么 debug、secure、checked free 还要退回 page map?
**答案**:算术路径对任意 32 字节对齐的"像模像样"的野指针都会算出一个槽位并解引用,行为未定义;page map 是唯一能权威回答"这段地址是否真的属于某个存活页"的数据结构。所以"性能换校验"——校验在场时查表(internal.h:796-807 的分发顺序就是这层取舍的代码化)。

**练习 2**:一个横跨 3 个 slice 的页,free 一个位于第 3 个 slice 的块,算术路径怎么找到正确的 `mi_page_t`?
**答案**:`_mi_aligned_ptr_page0` 算出的是第 3 个 slice 对应的**槽位**;因为造页时 arena.c:1079-1082 把 3 个槽位的 `self` 都写成了首个槽位的真身,一次 `load-acquire(&slot->self)` 就跳到正确的页结构。

**练习 3**:启用 `MI_PAGE_META_IS_ALIGNED` 后,page map 还有哪些不可替代的用途?
**答案**:至少三类——(1) 校验型入口(`mi_cfree`、debug/secure 的 `_mi_checked_ptr_page`);(2) 公开 API `mi_is_in_heap_region`(page-map.c:508-515);(3) 巨大对象页只登记到"最远内部指针"的截断登记(flat 模式编码、以及 2-level 的 `mi_page_map_get_idx` 钳位)等管理性用途。

## 5. 综合实践

**任务:写一个 20 行的"指针验尸器",把本讲三条路径全部走一遍。**

程序行为(示例代码):

```c
// verify.c(示例代码)
#include <stdio.h>
#include <stdint.h>
#include <mimalloc.h>

static void autopsy(const char* name, void* p) {
  uintptr_t a = (uintptr_t)p;
  printf("%-10s p=%-14p in_heap=%d 段=%p slice#=%zu\n",
         name, p, mi_is_in_heap_region(p),
         (void*)(a & ~(uintptr_t)0xFFFFFFF), (a & 0xFFFFFFF) >> 16);
}

int main(void) {
  void* small = mi_malloc(24);
  void* medium = mi_malloc(100 * 1024);
  void* huge = mi_malloc(4 * 1024 * 1024);
  int local = 0;
  autopsy("small", small);
  autopsy("medium", medium);
  autopsy("huge", huge);
  autopsy("stack", &local);
  autopsy("NULL", NULL);
  mi_free(small); mi_free(medium); mi_free(huge);
  return 0;
}
```

完成后,依次回答并动手验证:

1. **读表**:五个指针里哪些 `in_heap=1`?(对应 4.1 的 page map 查询路径)
2. **算术**:`small` 与 `medium` 的段基址是否相同?哪个更可能跨 slice?手算 `small` 的元数据槽位地址,对照 4.4 的公式。(对应 4.4 的对齐路径)
3. **体积**:把 `huge` 换成分配/释放循环(1 万次 4 MiB),分别在 `MIMALLOC_PAGEMAP_COMMIT=1` 与默认配置下运行,用 `/usr/bin/time -v` 看 RSS 峰值差异,解释按需 commit 的作用。(对应 4.3)
4. **粒度**:free 全部三个块后再调一次 `autopsy("small", small)`,`in_heap` 变了吗?为什么?(回到 4.1.4 的结论:page map 跟踪页,不跟踪块。)

预期:第 1 问 small/medium/huge 为 1、stack/NULL 为 0;第 2 问两者常同段,`medium` 跨 slice 概率更高;第 3 问全量 commit 版 RSS 略高(几 MiB 内);第 4 问大概率仍为 1(页未归还)。具体数值**待本地验证**。

## 6. 本讲小结

- free 只有裸指针,`指针 → mi_page_t` 是反问题,必须靠全局反查表——page map;它跟踪的是**页**,不是块。
- 地址刻度是 64 KiB 的 arena slice;表体积由刻度决定:flat 形态每 slice 1 字节(48 位 = 4 GiB 虚拟保留;仅 ≤40 位地址空间且显式定义 `MI_FREE_USE_PAGEMAP` 时启用),默认的 2-level 形态顶层 \(2^{19}\) 个指针 = 4 MiB(48 位)/2 MiB(x64 的 47 位),每子表 64 KiB、盖 512 MiB。
- 2-level 查询 = 除法拆下标 + 两次访存,无锁;`_mi_checked_ptr_page` 用单调递增的 `committed_count` 一次比较安全拒绝高地址野指针。
- **按需 commit** 是 page map 的灵魂:顶层一次性 reserve、初始化只 commit `MI_MIN_VABITS` 对应的 128 KiB、此后按用到的地址段以 64 KiB 步长增长,子表用到才 zalloc(锁 + CAS 双检);flat 形态则用 commit 位图管理同一件事。
- 默认 64 位构建的 `mi_free` 快路径其实**不查表**:`MI_PAGE_META_IS_ALIGNED` 让 arena 按 256 MiB 对齐、页元数据排在段头数组,`_mi_aligned_ptr_page0` 两次算术 + 一次 `self` 载荷即得页;page map 退居权威登记簿,服务校验、debug、secure 与 `mi_is_in_heap_region`。
- 两条免费的安全设计:`free(NULL)` 靠静态空表与段尾 NULL 子表在任意时刻安全;巨大对象页只登记到"最远内部指针"以省表项。

## 7. 下一步学习建议

本讲补完了对象模型的最后一块基石(指针 → 页),单元三到此收官。接下来:

- **u4-l1(mi_malloc 快路径)**:去看分配方向的"少几次访存"——与本讲的 free 反查路径对照,你会发现 mimalloc 的性能哲学就是让两个方向的最热路径都退化为个位数访存。
- **u6-l3(arena 与原子位图)**:本讲反复出现的 slice、256 MiB 对齐、`mi_memid_t`,都由 arena 批发;去读 `mi_arena_alloc_ex` 看 page map 的对齐要求如何传导到 arena 保留内存的参数上。
- **u6-l2(os.c 的 commit/purge)**:本讲的 reserve/commit 只是预演,os.c 里有完整的 commit/decommit/purge 语义与 overcommit 判定(`_mi_os_has_overcommit`,正是 2-level 初始化决定"是否全量 commit"的依据)。
- 想再抠本讲细节,可以追一条支线:`mi_option_max_vabits`(options.c:164)如何让用户强制收缩 page map——顺手验证你算的 47/48 位体积差。
