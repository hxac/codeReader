# u3-l3 size class 与 bin：page-queue 的组织方式

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 mimalloc 如何把任意请求尺寸映射到一个 bin（size class），并亲手执行 `mi_bin` 的位运算算法。
2. 画出 `mi_theap_t.pages[MI_BIN_COUNT]` 这组页队列的组织方式：同一个 bin 的页挂在同一条双向链表上。
3. 解释 `pages_free_direct` 直查数组如何让 ≤ 1KiB 的小对象分配「一次数组下标」就拿到目标页。
4. 准确区分 small / medium / large / huge 四档对象的尺寸边界（`MI_SMALL_MAX_OBJ_SIZE` 等宏），并理解统计输出里 `bin-S/M/L/H` 标记的真正含义。

本讲是 u3-l1（堆层级模型）与 u3-l2（页与三条 free list）之后的第三块拼图：**尺寸如何路由到页**。

## 2. 前置知识

- **size class（尺寸类）/ bin**：分配器不直接按请求字节数管理内存，而是把尺寸归入有限个「规格」。mimalloc 的一个 bin 就是一种块规格；u1-l4 里你在统计输出中见过的 `bin` 行，每一行对应一个 bin。
- **机器字（word）**：`sizeof(void*)`，64 位平台是 8 字节。mimalloc 内部先用「字数」思考尺寸，再换算回字节。
- **mimalloc page**：只装一种 bin 的块容器（u3-l2 讲过它的三条 free list 与 `block_size` 字段）。页本身从 arena 切片而来，小页 64KiB、中页 512KiB、大页 4MiB。
- **MI_PADDING**：debug 构建里每个块末尾附带的 8 字节哨兵（u3-l2 讲过 `mi_padding_t`）。**查 bin 时用的是「请求尺寸 + padding」**，这会让 debug 构建的边界值与 release 不同——本讲实践会专门利用这一点。
- **直查数组（direct index array）**：用「尺寸 → 数组下标」替代「尺寸 → 计算 → 查表」，是分配器常见的 O(1) 加速手段。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/page-queue.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-queue.c) | 本讲主角：`mi_bin` 尺寸映射算法、页队列的插入/删除/搬迁，以及 `pages_free_direct` 的区间维护（注意：它被 `page.c` include，不是独立编译单元） |
| [include/mimalloc/types.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h) | 四档尺寸边界宏、`mi_page_queue_t` 结构、`mi_theap_s` 里的 `pages[MI_BIN_COUNT]` 与 `pages_free_direct` |
| [include/mimalloc/internal.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h) | `_mi_wsize_from_size`（字节→字数）、`_mi_theap_get_free_small_page`（直查）、`mi_page_queue`（按尺寸取队列） |
| [src/init.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c) | 只读模板 `_mi_theap_empty`：整张 bin 尺寸表与直查数组的初始值都在这里 |
| [src/stats.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c) | 统计输出里 `bin-S/M/L/H` 字母的判定逻辑（实践环节要用） |
| [src/alloc.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c) / [src/page.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c) | 直查数组与页队列的两个消费方：快路径与慢路径入口 |

## 4. 核心概念与源码讲解

### 4.1 尺寸四档：small / medium / large / huge 的边界

#### 4.1.1 概念说明

mimalloc 把对象分成四档，每档对应一种页尺寸（页本身又由 arena 的 64KiB 切片拼成，见 u6）：

| 档位 | 对象尺寸上限（64 位默认构建） | 住在哪种页 | 页大小 |
| --- | --- | --- | --- |
| small (S) | `MI_SMALL_MAX_OBJ_SIZE` = 10 KiB | `MI_PAGE_SMALL` 小页 | 64 KiB |
| medium (M) | `MI_MEDIUM_MAX_OBJ_SIZE` ≈ 84.7 KiB | `MI_PAGE_MEDIUM` 中页 | 512 KiB |
| large (L) | `MI_LARGE_MAX_OBJ_SIZE` = 512 KiB | `MI_PAGE_LARGE` 大页 | 4 MiB |
| huge (H) | 超过 512 KiB | `MI_PAGE_SINGLETON` 单例页 | 一个对象独占 |

**命名陷阱**（本讲最容易混淆的一对名字）：

- `MI_SMALL_SIZE_MAX` = **1024 B（1 KiB）**，定义在公共头 [include/mimalloc.h:L122-L123](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L122-L123)。它是「小对象**快路径**」的上限：≤ 1 KiB 的分配走 `pages_free_direct` 直查，不需要计算 bin。
- `MI_SMALL_MAX_OBJ_SIZE` = **10 KiB**，定义在内部头 types.h。它是「能住进 64 KiB 小页」的对象上限，也是统计里 `S` 档的上限。

两者相差 10 倍，名字却只差一个词。记住：**SIZE_MAX 管快路径，MAX_OBJ_SIZE 管页种类**。

#### 4.1.2 核心流程

一次 `mi_malloc(n)` 按尺寸路由（示意图，细节在单元四展开）：

```
mi_malloc(n)
 ├─ n ≤ 1024 (MI_SMALL_SIZE_MAX)        → 小对象快路径：pages_free_direct 直查页（4.4 节）
 ├─ 1024 < n ≤ 10 KiB                   → S 档：计算 bin，在 64 KiB 小页里分配
 ├─ 10 KiB < n ≤ ~84.7 KiB              → M 档：512 KiB 中页
 ├─ ~84.7 KiB < n ≤ 512 KiB             → L 档：4 MiB 大页
 └─ n > 512 KiB                         → H 档：单例页，整页只放这一个对象，
                                          内存直接来自 arena/OS（不再走 size class）
```

注意：**四档边界由「落到哪个 bin」间接决定**。统计输出里的字母标记的是 *bin 的 block_size* 属于哪一档，而不是请求本身——请求被向上取整进 bin 后，可能「跳档」。例如 85 KiB 的请求会落进 block_size 为 96 KiB 的 bin，被标记为 L 而非 M。这一点在综合实践中会亲手验证。

#### 4.1.3 源码精读

页尺寸的定义——小页 64KiB、中页 512KiB、大页 4MiB，全部由 arena 切片单位（64 KiB）推导：

- [include/mimalloc/types.h:L227-L229](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L227-L229)：`MI_SMALL_PAGE_SIZE`、`MI_MEDIUM_PAGE_SIZE`、`MI_LARGE_PAGE_SIZE` 三个宏逐级 ×8。

四档对象上限。注释说明了设计意图——不让对象相对页尺寸浪费超过约 12.5%：

- [include/mimalloc/types.h:L469-L478](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L469-L478)：`MI_SMALL_MAX_OBJ_SIZE = (64KiB − 4KiB)/6 = 10240`；`MI_MEDIUM_MAX_OBJ_SIZE = (512KiB − 4KiB)/6 ≈ 86698`；`MI_LARGE_MAX_OBJ_SIZE = 4MiB/8 = 524288`；以及字数版本 `MI_LARGE_MAX_OBJ_WSIZE = 65536`。

  为什么是「÷6」「÷8」？保证一个页里**至少**装得下 6（或 8）个块：若块太大导致一页只装 5 块，最后一块放不下时浪费可达整块。上限取 `(页 − 4KiB)/6`，其中 4KiB 是块起始对齐预留（`MI_PAGE_OSPAGE_BLOCK_ALIGN2`）。

页种类枚举，与四档一一对应（huge 用单例页表示）：

- [include/mimalloc/types.h:L499-L505](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L499-L505)：`mi_page_kind_t` 的四个值 `MI_PAGE_SMALL/MEDIUM/LARGE/SINGLETON`。

公共头里的快路径上限（区别于上面的 10 KiB）：

- [include/mimalloc.h:L122-L123](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L122-L123)：`MI_SMALL_WSIZE_MAX = 128`（128 字 = 1024 B），`MI_SMALL_SIZE_MAX` 由它乘 `sizeof(void*)` 得出。

统计输出里 S/M/L/H 字母的判定处——拿 bin 的 `block_size`（局部变量 `unit`）逐级比较：

- [src/stats.c:L277-L295](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L277-L295)：`unit <= MI_SMALL_MAX_OBJ_SIZE ? "S" : (unit <= MI_MEDIUM_MAX_OBJ_SIZE ? "M" : (unit <= MI_LARGE_MAX_OBJ_SIZE ? "L" : "H"))`，随后以 `bin S 24` 这样的标签打印每个非空 bin。

#### 4.1.4 代码实践

**实践目标**：用一个小程序把四档边界宏的真实数值打印出来，与手算值对照。

操作步骤（示例代码，非项目原有文件）：

```c
// bounds.c —— 示例代码
#include <stdio.h>
#include <mimalloc.h>
#include <mimalloc/types.h>   // 内部头：MI_SMALL_MAX_OBJ_SIZE 等宏在此

int main(void) {
  printf("MI_SMALL_SIZE_MAX      = %zu\n", (size_t)MI_SMALL_SIZE_MAX);
  printf("MI_SMALL_MAX_OBJ_SIZE  = %zu\n", (size_t)MI_SMALL_MAX_OBJ_SIZE);
  printf("MI_MEDIUM_MAX_OBJ_SIZE = %zu\n", (size_t)MI_MEDIUM_MAX_OBJ_SIZE);
  printf("MI_LARGE_MAX_OBJ_SIZE  = %zu\n", (size_t)MI_LARGE_MAX_OBJ_SIZE);
  return 0;
}
```

```bash
cc bounds.c -o bounds -I<仓库路径>/include && ./bounds
```

需要观察的现象（64 位平台的预期结果，待本地验证）：

```
MI_SMALL_SIZE_MAX      = 1024
MI_SMALL_MAX_OBJ_SIZE  = 10240
MI_MEDIUM_MAX_OBJ_SIZE = 86698
MI_LARGE_MAX_OBJ_SIZE  = 524288
```

预期结果：四个数值与 types.h 中 `(64KiB−4KiB)/6`、`(512KiB−4KiB)/6`、`4MiB/8` 的手算结果一致。若在 32 位平台或开启 `MI_SECURE>=5`（16KiB OS 页的平台会把 arena 切片提到 128KiB）构建，数值会整体变化——这正是这些宏存在的意义。

#### 4.1.5 小练习与答案

**练习 1**：`MI_SMALL_SIZE_MAX` 和 `MI_SMALL_MAX_OBJ_SIZE` 各自控制什么？为什么需要两个不同的「小」上限？

**答案**：前者（1024 B）控制**分配快路径**：不超过它的请求直接用 `pages_free_direct[wsize]` 拿页，免去 bin 计算与队列查找，直查数组的大小也由它决定。后者（10 KiB）控制**页种类**：不超过它的对象住 64 KiB 小页。两个概念正交：一个 5 KiB 的请求不走快路径（>1024），但仍然住小页。

**练习 2**：为什么 `MI_SMALL_MAX_OBJ_SIZE` 取 `(64KiB − 4KiB)/6` 而不是 `64KiB/6` 或干脆 `64KiB/2`？

**答案**：减 4KiB 是为块起始对齐预留（`MI_PAGE_OSPAGE_BLOCK_ALIGN2`）；÷6 保证一页至少容纳 6 个块，使得「最后一格放不下」造成的内部碎片不超过约 1/6，源码注释把总体目标标注为「不超过约 12.5%」（types.h:L469）。若取 64KiB/2，一页只装 2 块，单块放不下时最坏浪费接近一半页空间。

**练习 3**：统计输出里一行 `bin M 49  80.0KiB ...`，字母 M 是按请求尺寸还是按 bin 的块尺寸判定的？

**答案**：按 bin 的块尺寸（`_mi_bin_size(bin)` 返回的 `block_size`）判定，见 stats.c:L283-L286。所以一个 70 KiB 的请求若被取整进 96 KiB 的 bin，统计行会标 L。

---

### 4.2 `mi_bin`：从字节数到 bin 编号

#### 4.2.1 概念说明

给定请求尺寸，`mi_bin` 计算它属于哪个 bin。mimalloc 共有 **74 个常规 bin（编号 0..72）外加两个特殊队列（73=huge、74=full）**：

- [include/mimalloc-stats.h:L97](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc-stats.h#L97)：`#define MI_BIN_HUGE (73U)`，types.h:L233-L235 用编译期检查把它焊死为 73。
- [include/mimalloc/types.h:L232-L237](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L232-L237)：`MI_BIN_FULL = 74`（满页队列）、`MI_BIN_COUNT = 75`（队列总数）。

bin 的分布规律（对照 4.3 节的静态表）：

- **bin 1..8**：8 字到 64 字，**每字一个精确 bin**——小尺寸最常见，值得零浪费。
- **bin 9..60**：几何级数，**每个二进制倍频程（octave）均分 4 档**，相邻 bin 尺寸比约 1.25（注释标注最坏内部碎片约 12.5%）。
- **bin 61..72**：默认 64 位构建下不可达（见 4.3 节说明），为大切片配置预留。
- **bin 73 (HUGE)**：超过 `MI_LARGE_MAX_OBJ_WSIZE`（65536 字 = 512 KiB）的一切尺寸。
- **bin 0**：占位符（wsize 为 0 的情形折入 bin 1），常规分配不会返回它。

#### 4.2.2 核心流程

`mi_bin(size)` 分三段处理（`wsize = ⌈size/8⌉`，即向上取整的机器字数）：

```
wsize = ⌈size / sizeof(void*)⌉

① wsize ≤ 8（默认 64 位走 MI_ALIGN2W 分支）：
     bin = wsize ≤ 1 ? 1 : (wsize + 1) & ~1      // 按 2 字（16B）取整
     ⇒ wsize 1,2 → bin 1,2；3,4 → 4；5,6 → 6；7,8 → 8
     （bin 3/5/7 因此空置——保证块满足 16 字节最大对齐）

② 8 < wsize ≤ 65536（MI_LARGE_MAX_OBJ_WSIZE）：
     w = wsize − 1                                // 保证 w ≠ 0
     b = ⌊log₂ w⌋                                 // 最高位位置（mi_clz 前导零计数）
     t = (w ≫ (b−2)) & 0b11                       // 最高位后面的 2 个比特
     bin = 4b + t − 3

③ wsize > 65536：
     bin = MI_BIN_HUGE (73)
```

② 式的直觉：\( w \in [2^b, 2^{b+1}) \) 的倍频程被最高位之后的 2 个比特均分成 4 档：

\[ \text{bin} = 4b + t - 3, \qquad t = \left\lfloor w / 2^{b-2} \right\rfloor \bmod 4 \in \{0,1,2,3\} \]

「−3」是校准量：bin 1..8 已经用 8 个精确 bin 覆盖了 wsize 1..8，公式从 b=3（wsize 9..16）接续时正好落在 bin 9..12（对应块尺寸 10/12/14/16 字）。

手算示例（release，无 padding）：

| 请求 | wsize | w=wsize−1 | b | t | bin | 块尺寸 |
| --- | --- | --- | --- | --- | --- | --- |
| 100 B | 13 | 12 | 3 | (12≫1)&3 = 2 | 11 | 14 字 = 112 B |
| 1024 B | 128 | 127 | 6 | (127≫4)&3 = 3 | 24 | 128 字 = 1024 B |
| 10240 B (10KiB) | 1280 | 1279 | 10 | (1279≫8)&3 = 0 | 37 | 1280 字 = 10240 B |
| 524288 B (512KiB) | 65536 | 65535 | 15 | (65535≫13)&3 = 3 | 60 | 65536 字 = 512 KiB |

最后一行正是 `MI_MAX_SINGLETON_BIN = 60`（types.h:L484-L493 的静态不变式「`MI_MAX_SINGLETON_BIN ≥ _mi_bin(MI_LARGE_MAX_OBJ_SIZE)`」）。

#### 4.2.3 源码精读

字节→字数的换算（注意是向上取整）：

- [include/mimalloc/internal.h:L570-L574](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L570-L574)：`_mi_wsize_from_size` 返回 `(size + sizeof(uintptr_t) - 1) / sizeof(uintptr_t)`。

`mi_bin` 本体，三段结构与上面的伪代码逐行对应：

- [src/page-queue.c:L64-L96](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-queue.c#L64-L96)：
  - L66 先换算成 `wsize`；
  - L70-L73 是 `MI_ALIGN2W` 分支（默认 64 位平台 `MI_MAX_ALIGN_SIZE=16 > 8` 会激活，见同文件 L24-L32 的条件编译）：`(wsize+1)&~1` 把 3..8 字取整到偶数字，保证 16 字节对齐；
  - L79-L81：超过 `MI_LARGE_MAX_OBJ_WSIZE` 直接判 HUGE；
  - L86-L94：核心位运算——`mi_clz` 数前导零得到最高位 `b`，取次高 2 比特算出 `bin = ((b << 2) + ((wsize >> (b-2)) & 0x03)) - 3`，注释标注「用最高 3 个比特决定 bin，最坏内部碎片约 12.5%；减 3 是因为前 8 个尺寸各有一个精确 bin」。

对外的薄封装与它的消费方：

- [src/page-queue.c:L104-L106](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-queue.c#L104-L106)：`_mi_bin(size)` 只是转调 `mi_bin`，供 `arena.c`、`alloc.c`（统计）与调试使用。
- [include/mimalloc/internal.h:L945-L949](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L945-L949)：`mi_page_queue(theap, size)` = `&theap->pages[_mi_bin(size)]`——慢路径找队列的统一入口。
- [src/page.c:L950-L960](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L950-L960)：`mi_find_page` 用它取队列；若结果队列是 huge 队列（或对齐要求过大，此时直接传 `MI_LARGE_MAX_OBJ_SIZE+1` 强制入 huge 队列），转 `mi_huge_page_alloc` 走单例页。

#### 4.2.4 代码实践

**实践目标**：不看源码输出，手算 3 个尺寸的 bin，再用 `mi_good_size` 交叉验证。

操作步骤：

1. 对请求尺寸 `24`、`100`、`8192` 各写一行：按 ② 式手算 wsize、b、t、bin 与块尺寸。
2. 写一个小程序（示例代码）：

```c
// bincheck.c —— 示例代码
#include <stdio.h>
#include <mimalloc.h>

int main(void) {
  size_t req[] = {24, 100, 8192};
  for (int i = 0; i < 3; i++) {
    printf("request %5zu -> good_size %5zu\n", req[i], mi_good_size(req[i]));
  }
  return 0;
}
```

3. 分别用 release 与 debug 构建链接运行（`mi_good_size` 的实现见 4.3.3）。

需要观察的现象：release 下 `good_size` 返回所在 bin 的 `block_size`；debug 下因 `mi_good_size` 内部先加 `MI_PADDING_SIZE`（8 B）再查 bin，边界附近的请求会落到下一个 bin。

预期结果（待本地验证）：

| 请求 | release 预期 | debug 预期（+8B padding 后查 bin） |
| --- | --- | --- |
| 24 | 32（wsize 3 → 取整 bin 4 = 4 字） | 32（32 → wsize 4 → bin 4） |
| 100 | 112（bin 11） | 112（108 → wsize 14 → bin 11） |
| 8192 | 8192（wsize 1024 → bin 36 = 1024 字，恰好装下） | 8192+8 → wsize 1025 → bin 37 = 1280 字 → **10240** |

第三行是 debug 构建最典型的「跳 bin」现象：仅仅多 8 字节 padding，块尺寸从 8 KiB 跳到 10 KiB。

#### 4.2.5 小练习与答案

**练习 1**：为什么 bin 1..8 每个字给一个精确 bin，之后却改成每倍频程 4 档？

**答案**：小对象（≤64 B）在真实程序里出现频率最高，精确分档把内部碎片压到 7 字节以内；大对象若仍逐字分档，bin 数量会随尺寸线性膨胀。倍频程 4 分档让 bin 数量只随 log₂(尺寸) 增长（512 KiB 只需到 bin 60），代价是最坏约 12.5% 的取整浪费（page-queue.c:L89-L91 注释）。

**练习 2**：`mi_bin(0)` 返回什么？bin 0 的队列会被用到吗？

**答案**：wsize 为 0 时返回 1（page-queue.c:L76 与 L72 的 `wsize <= 1 ? 1 : ...`）。bin 0 只是让「bin 编号 == wsize」在 1..8 区间成立的占位槽位，常规分配路径不会返回 0。

**练习 3**：公式里 `wsize--`（先减 1）的作用是什么？

**答案**：保证 `w` 非零（`mi_clz(0)` 无定义）；同时使各倍频程的边界值（如 wsize 恰为 128 字）正确落进「恰好装下」的 bin：wsize 128 → w=127 → bin 24（128 字块），块刚好等于请求，零浪费。

---

### 4.3 页队列 `pages[MI_BIN_COUNT]`：同 bin 页的双向链表

#### 4.3.1 概念说明

u3-l2 讲过：一个 `mi_page_t` 只装一种 `block_size` 的块。那么「当前线程手里所有 112 B 的页」去哪找？答案是一个 **bin 一条队列**：`mi_theap_t.pages[75]`，下标就是 bin 编号。队列结构极简：

- [include/mimalloc/types.h:L527-L533](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L527-L533)：`mi_page_queue_t` 只有 4 个字段——`first`/`last`（双向链表首尾）、`count`（页数）、`block_size`（该队列的块尺寸，const 性质）。

- [include/mimalloc/types.h:L595](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L595)：`mi_theap_s` 内嵌 `mi_page_queue_t pages[MI_BIN_COUNT]`，75 个队列 × 32 B ≈ 2.4 KiB，随 theap 一起线程本地化，无需加锁。

每个队列的 `block_size` 从哪来？来自一张**编译期写死的静态表**。此外还有两个特殊队列：

- **huge 队列（bin 73）**：`block_size = MI_LARGE_MAX_OBJ_SIZE + 8`（一个指针大小），所有单例大页挂这里；
- **full 队列（bin 74）**：`block_size = MI_LARGE_MAX_OBJ_SIZE + 16`，页满了（`reserved == used`，u3-l2 的满页判定）之后从原 bin 队列搬到这里，腾出常规队列头部给「还有空闲块的页」；释放一个块时再搬回去。

#### 4.3.2 核心流程

队列的基本操作全部是 O(1) 双向链表操作，且**每次首页变化都要同步维护直查数组**（4.4 节）：

```
入队 push（新页/重新有空的页）      → 头插，然后 mi_theap_queue_first_update
出队 remove（页满/页空被回收）      → 摘链；若移除的是 first，同样 first_update
搬迁 enqueue_from（bin 队列 ⇄ full 队列）→ 摘链 + 尾插（或头插），两端都按需 first_update
```

bin 尺寸表则完全静态：`_mi_bin_size(bin)` 直接读只读模板 `_mi_theap_empty.pages[bin].block_size`——运行期**零初始化成本**，新 theap 只需 memcpy 模板。

#### 4.3.3 源码精读

整张 bin 尺寸表（数字是「字数」，`QNULL(sz)` 展开为 `block_size = sz * sizeof(void*)`）：

- [src/init.c:L66-L80](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L66-L80)：`MI_PAGE_QUEUES_EMPTY` 宏。可读出：bin 9=10 字、bin 11=14 字（112 B）、bin 24=128 字（1024 B）、bin 37=1280 字（10240 B）、bin 49=10240 字（80 KiB）、bin 60=65536 字（512 KiB）；L79-L80 是 huge 与 full 两个特殊队列（`MI_LARGE_MAX_OBJ_WSIZE + 1/+2` 字）。

- [src/init.c:L120-L145](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L120-L145)：只读模板 `_mi_theap_empty`，L142 处填入上面的队列表；它在 theap 创建时被整块拷贝（[src/theap.c:L242](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L242) 的 `_mi_memcpy_aligned(theap, &_mi_theap_empty, sizeof(mi_theap_t))`）。

bin 编号 ↔ 块尺寸的查询函数与用户可见的 `mi_good_size`：

- [src/page-queue.c:L108-L111](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-queue.c#L108-L111)：`_mi_bin_size(bin)` 读模板表；它也是统计输出里每个 bin 行尺寸列的数据源。
- [src/page-queue.c:L114-L124](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-queue.c#L114-L124)：`mi_good_size`（公共 API，u1-l4 介绍过）——请求 ≤ `MI_LARGE_MAX_OBJ_SIZE − padding` 时返回 `_mi_bin_size(mi_bin(size + padding))`（所在 bin 的块尺寸）；超过后走 huge 分支，按 OS 页大小向上对齐。

队列的三类操作（都带动 `mi_theap_queue_first_update`）：

- [src/page-queue.c:L252-L274](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-queue.c#L252-L274)：`mi_page_queue_remove` 摘链，L263-L268 在移除首页时更新直查数组；
- [src/page-queue.c:L277-L304](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-queue.c#L277-L304)：`mi_page_queue_push` 头插，L287 顺手调用 `mi_page_set_in_full` 设置「在满队列中」标志（u3-l2 讲过的页标志位），L301-L303 更新直查数组；
- [src/page-queue.c:L344-L423](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-queue.c#L344-L423)：`mi_page_queue_enqueue_from_ex` 在两条队列间搬迁页（full 队列 ⇄ 常规队列的主要搬运工），L421-L423 的封装 `mi_page_queue_enqueue_from_full` 注释说明了取舍：插回队头虽提高复用，但会拖慢 `alloc-test` 类基准，故选择尾插。

队列身份判定（huge/full 用 `block_size` 而非下标判断，容错性更好）：

- [src/page-queue.c:L40-L50](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-queue.c#L40-L50)：`mi_page_queue_is_huge` / `is_full` / `is_special`。

页 → bin 的反查（统计与队列定位用）：

- [src/page-queue.c:L174-L178](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-queue.c#L174-L178)：`mi_page_bin`——满页给 `MI_BIN_FULL`，单例大页给 `MI_BIN_HUGE`，普通页用 `mi_bin(block_size)`。

**关于 bin 61..72 的说明**：默认 64 位构建里 `mi_bin` 的 ③ 段把超过 512 KiB 的一切直接判 HUGE，因此静态表中 bin 61..72（640 KiB..4 MiB）平时不可达。它们为「大 arena 切片」配置预留：例如 `MI_SECURE>=5` 且 OS 页为 16 KiB 的平台（如 Apple Silicon）会把切片提到 128 KiB（types.h:L192-L193），此时 `MI_LARGE_MAX_OBJ_SIZE` 变为 1 MiB，按 ② 式 `_mi_bin(1MiB)` 恰好落在 bin 64（= 131072 字，与静态表一致）。同一张表服务多种配置，这正是它做成编译期常量表的原因。

#### 4.3.4 代码实践

**实践目标**：把静态表「读」成一张人可读的对照卡，为综合实践做准备。

操作步骤（源码阅读型）：

1. 打开 [src/init.c:L66-L80](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L66-L80)，把每行 8 个 `QNULL(sz)` 的 `sz` 乘 8 换算成字节，抄下 bin 编号 1、2、4、11、24、37、49、60 的块尺寸。
2. 用 `mi_good_size` 反向验证：对请求 16、100、1024、10240 释放查询（release 构建），确认返回值与你抄的表一致。

需要观察的现象：bin → 块尺寸是单调不减的台阶序列；相邻台阶的比值在小尺寸区为 1（精确）或 2（对齐取整），在大尺寸区趋近 1.25。

预期结果：bin 1=8B、2=16B、4=32B、11=112B、24=1024B、37=10240B、49=81920B、60=524288B（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么满页要搬去专门的 full 队列，而不是留在原 bin 队列里？

**答案**：队列头部应当尽量放「还有空闲块的页」，这样慢路径从队首找页时命中率最高。满页的 free list 为空，留在队列里只会让每次查找白扫一遍；集中放进 full 队列后，释放其中一个块时再搬回来（`mi_page_queue_enqueue_from_full`）。另外 theap 用 `pages_full_size` 字段汇总满页总大小，避免遍历（u3-l2 提过 issue #1220 优化）。

**练习 2**：`_mi_bin_size` 为什么可以读静态常量 `_mi_theap_empty` 而不用当前 theap？

**答案**：所有 theap 的队列表都拷贝自同一只读模板，`pages[bin].block_size` 对同一 bin 恒等且运行期不变（queue 的 `block_size` 是 const 性质字段）。读静态模板避免了「必须先有一个 theap」的依赖，统计代码可以在任意时刻调用。

**练习 3**：一个 300 KiB 的请求会进哪个 bin？住在哪种页？

**答案**：300 KiB = 30720 B → wsize 38400 ≤ 65536，走 ② 式：w=38399，b=15，t=(38399≫13)&3=0，bin = 60−3 = 57（40960 字 = 320 KiB 块）。320 KiB > `MI_MEDIUM_MAX_OBJ_SIZE`(≈84.7 KiB) 且 ≤ 512 KiB，属 L 档，住 4 MiB 大页。

---

### 4.4 `pages_free_direct`：小对象 O(1) 直达页

#### 4.4.1 概念说明

有了 bin 与队列，一次 ≤ 1 KiB 的分配仍要经历「`mi_bin` 计算 → 取队列 → 检查队首页」。对最高频的小对象，mimalloc 把这条路进一步压扁成**一次数组下标**：

- [include/mimalloc/types.h:L557](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L557)：`MI_PAGES_DIRECT = MI_SMALL_WSIZE_MAX + MI_PADDING_WSIZE + 1`，debug 64 位下 = 128+1+1 = **130 项**（release 为 129 项）。
- [include/mimalloc/types.h:L560-L563](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L560-L563)：`pages_free_direct[MI_PAGES_DIRECT]` 是 `mi_theap_s` 的**第一个成员**（注释：put in front for fast small allocations）——排在结构体开头，缓存友好且偏移量编译期已知。

不变式：**`pages_free_direct[w]` 永远指向「w 个字的请求所在 bin 的队列首页」**（注释原文：a page with *possibly* free blocks）。若队列空，则指向只读哨兵页 `mi_page_empty`——它的 `block_size == 0`、`free == NULL`，分配时会自然滑入慢路径。

#### 4.4.2 核心流程

快路径（alloc.c，仅 ~7 条指令，源码注释原文如此）：

```
mi_malloc(n)，n ≤ 1024：
  w = wsize(n + MI_PADDING_SIZE)          // 一次除法（编译器优化为移位+加法）
  page = theap->pages_free_direct[w]      // 1 次读：直达队首页
  block = page->free                      // 1 次读：free list 头
  若 block == NULL（空哨兵页或页满）→ 转慢路径 _mi_malloc_generic
  否则：page->free = next(block); page->used++   // 1 读 1 写 + 1 读改写，返回 block
```

慢路径维护（page-queue.c）：队列首页一旦变化（push/remove/搬迁），`mi_theap_queue_first_update` 把**同一个页指针写进一段连续下标区间**。为什么是区间？因为「wsize → bin」是多对一：取整让 wsize 3 和 4 共用 bin 4，对齐让 bin 3/5/7 空置。所以更新 bin 4 的队列时，下标 3 和 4 两项都要指向新队首页。该函数还向前跳过至多 3 个「空 bin」找到区间起点（源码注释：due to minimal alignment upto 3 previous bins may need to be skipped）。

#### 4.4.3 源码精读

直查入口（inline，热路径专用）：

- [include/mimalloc/internal.h:L650-L655](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L650-L655)：`_mi_theap_get_free_small_page(theap, size)`——断言 `size ≤ MI_SMALL_SIZE_MAX + MI_PADDING_SIZE` 后直接 `return theap->pages_free_direct[_mi_wsize_from_size(size)]`。

快路径消费方，注意它传入的是**加了 padding 的尺寸**：

- [src/alloc.c:L133-L160](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L133-L160)：`mi_theap_malloc_small_zero_nonnull`，L150 一行拿到页，L151 进入弹块逻辑；
- [src/alloc.c:L29-L57](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L29-L57)：`mi_page_malloc_zero`——L34 先判断 `page->block_size != 0`（识别空哨兵页），L41-L48 检查 free list，空则转 `_mi_malloc_generic`；L52-L57 弹块 + `used++`。L31 注释写明：release 下内联后约 7 条指令、仅 1 次判断。

空哨兵页的定义与直查数组的初始值：

- [src/init.c:L17-L47](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L17-L47)：静态 `mi_page_empty`（L24 `block_size = 0`）与宏 `MI_PAGE_EMPTY()`；
- [src/init.c:L57-L63](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L57-L63)：`MI_SMALL_PAGES_EMPTY` 把 130 项全部初始化为哨兵页，随 `_mi_theap_empty` 模板拷贝进每个新 theap。因此「线程第一次分配」不需要任何初始化分支：查到的哨兵页 free 为空，自然走慢路径，慢路径建好页、更新队列后回填直查数组。

区间维护的核心函数：

- [src/page-queue.c:L204-L244](https://github.com/microsoft/microsoft/mocalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-queue.c#L204-L244)：`mi_theap_queue_first_update`。逐段读：L212 超过 `MI_SMALL_SIZE_MAX` 的队列直接返回（直查数组只覆盖小对象）；L214-L215 队列空则用哨兵页；L218-L219 目标下标 `idx = wsize(block_size)`；L224-L237 **向前跳过与当前 bin 相同的「前驱队列」**求区间起点 `start`——循环条件 `mi_bin(prev->block_size) == bin` 正是「多对一」的体现；L240-L243 把 `pages_free[start..idx]` 统一写成新队首页。

  （更正上面的链接笔误，正确地址：[src/page-queue.c:L204-L244](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-queue.c#L204-L244)）

三个调用时机：

- [src/page-queue.c:L263-L268](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-queue.c#L263-L268)：`mi_page_queue_remove` 移除的是首页时；
- [src/page-queue.c:L301-L303](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-queue.c#L301-L303)：`mi_page_queue_push` 头插之后；
- [src/page-queue.c:L363-L368](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-queue.c#L363-L368) 与 L385、L409：`enqueue_from_ex` 中源/目标队列首变化时。

#### 4.4.4 代码实践

**实践目标**：在源码上完成一次「内存访问计数」，理解直查数组的性能含义。

操作步骤（源码阅读型）：

1. 从 [src/alloc.c:L150](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L150) 出发，沿 `_mi_theap_get_free_small_page` → `pages_free_direct[w]` → `page->free` → `block->next` → `page->free = next` → `page->used++` 逐行标注，数出快路径的读写次数。
2. 对比慢路径：同样一次分配若走 `mi_page_queue`（internal.h:L945-L949）需要「算 bin → 读队列 → 读 first」三步起步，还要承受队首页无空闲块的回退。
3. （可选，待本地验证）按 u1-l2 的 `MI_SEE_ASM` 方式生成汇编，观察 `mi_malloc_small` 快路径的指令数是否与 alloc.c:L31 注释的「约 7 条指令」吻合。

需要观察的现象：快路径唯一的「查找」就是一次数组读；没有分支（除了那次 free list 判空）、没有原子操作、没有函数调用（全部 forceinline）。

预期结果：约 3~4 次内存读（TLS 中的 theap 指针、直查数组项、free list 头、块内 next 指针）+ 2 次写（page->free、page->used）。

#### 4.4.5 小练习与答案

**练习 1**：`pages_free_direct` 为什么不覆盖全部 75 个 bin，只到 130 项？

**答案**：数组按下标 = wsize 索引，若覆盖到 512 KiB 需要 65537 项（512 KiB 内存），每个 theap 一份、每线程都要，空间代价不可接受；而大对象出现频率低，用 `mi_bin` 计算一次完全可以接受。1 KiB 截断是「频率 × 空间」的折中：覆盖最高频的小对象，数组只有约 1 KiB。

**练习 2**：线程第一次 `mi_malloc(16)` 时 `pages_free_direct[2]` 指向什么？这次分配走哪条路？

**答案**：指向静态哨兵页 `mi_page_empty`（初始值来自 `_mi_theap_empty` 模板）。它的 `free == NULL`，于是 `mi_page_malloc_zero` 转入 `_mi_malloc_generic` 慢路径：初始化线程 theap、向 arena 要切片建页、把页 push 进 bin 2 队列——push 时 `mi_theap_queue_first_update` 把直查数组对应区间回填为新页。**用哨兵页代替初始化检查，是 mimalloc 消除快路径分支的惯用手法**（与默认 theap 初始指向空 theap 同理，见 u3-l1）。

**练习 3**：`mi_free` 释放一个块时，需要更新 `pages_free_direct` 吗？

**答案**：不需要。`pages_free_direct` 的不变式只约束「指向所在队列的首页」，而本线程释放只是把块挂到页的 `local_free`（u3-l2 与 u5-l1），既不改变页在队列中的位置，更不会改变队首页。只有队列首变化（页满出队、空页回收、新页入队、full 队列搬回）时才由队列操作函数触发更新。

## 5. 综合实践：尺寸扫描——肉眼验证 size class 路由

**任务**：写一个扫描程序，从 1 B 到 1 MiB 取代表性尺寸各分配一次，用 `mi_good_size` 打印落点，并在 debug 构建下用统计输出的 bin 行验证「请求 → bin → 四档」的完整路由。

### 步骤 1：构建 debug 版本（沿用 u1-l2 的方法）

```bash
cd <仓库根目录>
mkdir -p out/debug && cd out/debug
cmake ../.. -DCMAKE_BUILD_TYPE=Debug
cmake --build . --target mimalloc
# 产物为 libmimalloc-debug.*（debug 构建会把类型追加进库名，见 u1-l2；以实际产物为准）
```

### 步骤 2：编写扫描程序（示例代码，非项目原有文件）

```c
// sizesweep.c —— 示例代码
#include <stdio.h>
#include <mimalloc.h>

static void probe(const char* kind, size_t n) {
  void* p = mi_malloc(n);
  if (p == NULL) { printf("%-6s %8zu B : allocation failed\n", kind, n); return; }
  printf("%-6s %8zu B -> good_size %8zu B, usable %8zu B\n",
         kind, n, mi_good_size(n), mi_usable_size(p));
  mi_free(p);
}

int main(void) {
  probe("tiny",   1);              // 最小请求
  probe("tiny",   16);
  probe("small",  100);
  probe("small",  1024);           // = MI_SMALL_SIZE_MAX，直查数组内最后一个精确档
  probe("small",  1025);           // 刚超过快路径上限
  probe("small",  10*1024);        // = MI_SMALL_MAX_OBJ_SIZE
  probe("med",    32*1024);
  probe("med",    80*1024);        // debug 下仍属 M 档的最大 bin（80 KiB 块）
  probe("large",  100*1024);
  probe("large",  512*1024 - 8);   // debug 下仍能落入常规 bin 的最大请求
  probe("huge",   512*1024);       // release 下是最后一个 L bin；debug 下因 padding 变 huge
  probe("huge",   1024*1024);
  return 0;
}
```

### 步骤 3：编译运行（链接 debug 库，打开统计）

```bash
cc ../..//mimalloc-tutorial/sizesweep.c -o sizesweep \
   -I../../include -I. -L. -lmimalloc-debug
MIMALLOC_SHOW_STATS=1 ./sizesweep 2>&1 | tee sizesweep.log
```

（源文件放哪随你，关键是 `-I` 指到仓库 `include/`、`-L` 指到构建目录。）

### 步骤 4：需要观察的现象

1. **`good_size` 的台阶**：请求连续增大时，`good_size` 长时间不动、然后跳一档——这就是 size class 的台阶效应（u1-l4 用两个 API 对照过，这次看全谱）。
2. **统计输出的 blocks 段**（debug 默认 `MI_STAT=2`，u1-l4）：每个非空 bin 一行，格式类似 `bin S 24  1.0KiB ...`；对照每行的 bin 编号与字母，与你预测的落点一致。
3. **huge 行**：malloc 段的 `huge` 计数只被 > 512 KiB（debug 下 > 512 KiB − 8）的请求推高——这些请求不走任何 size class，由 `mi_huge_page_alloc` 直接要一个单例页（page.c:L920-L946）。

### 步骤 5：预期结果——尺寸 → 分类判断表

下表按源码推导（debug 构建，padding = 8 B；「预期 bin」列待本地验证）：

| 请求 | +padding 后 wsize | 预期 bin | good_size（块尺寸） | 统计字母 | 档位理由 |
| --- | --- | --- | --- | --- | --- |
| 1 B | 2 | 2 | 16 B | S | 对齐取整到 2 字 |
| 16 B | 3 | 4 | 32 B | S | 3 字取整到 4 字（bin 3 空置） |
| 100 B | 14 | 11 | 112 B | S | 14 字 → bin 11 = 14 字 |
| 1024 B | 129 | 25 | 1280 B | S | 刚越过的 128 字档 |
| 1025 B | 130 | 25 | 1280 B | S | 与 1024 同 bin |
| 10 KiB | 1281 | 38 | 12288 B | **M** | padding 把 1280 字顶过 bin 37（10240 B），落 bin 38 = 1536 字 > `MI_SMALL_MAX_OBJ_SIZE` → 标 M！ |
| 32 KiB | 4097 | 45 | 40960 B | M | 40 KiB ≤ 84.7 KiB |
| 80 KiB | 10001 | 49 | 81920 B | M | debug 下能遇到的最后一个 M bin |
| 100 KiB | 12801 | 51 | 114688 B | L | 112 KiB > 84.7 KiB |
| 512 KiB − 8 | 65536 | 60 | 524288 B | L | 最后一个常规 bin（= `MI_MAX_SINGLETON_BIN`） |
| 512 KiB | 65537 > 65536 | **73 (HUGE)** | 528384 B（OS 页对齐） | H（计入 `huge` 行） | padding 把它顶出常规 bin |
| 1 MiB | — | **73 (HUGE)** | 1052672 B | H | 单例页 |

release 构建下重跑（无 padding），重点对比三处：`1024 → bin 24（恰 1024 B）`、`10 KiB → bin 37（恰 10240 B，标 S）`、`512 KiB → bin 60（标 L 而非 huge）`。

**验收标准**：你手工推出的 bin 编号与统计输出逐行一致；并能向别人解释 512 KiB 请求为何在 debug 下「升级」成了 huge——这正是 `mi_good_size` 与 `mi_find_page` 都以「请求 + `MI_PADDING_SIZE`」查 bin 的直接后果（page-queue.c:L115-L116、alloc.c:L150）。

## 6. 本讲小结

- **尺寸路由两步走**：先 `_mi_wsize_from_size` 化成字数，再 `mi_bin` 三段式定位——wsize ≤ 8 按 16 B 对齐取精确档；8 < wsize ≤ 65536 用「最高位 + 次 2 比特」把每个倍频程均分 4 档（bin = 4b + t − 3）；再大直接判 `MI_BIN_HUGE`。
- **四档边界**（64 位默认）：S ≤ 10 KiB（64 KiB 小页）、M ≤ ≈84.7 KiB（512 KiB 中页）、L ≤ 512 KiB（4 MiB 大页）、超过即 huge 单例页；统计里的字母按 **bin 的块尺寸**判定，不是按请求。
- **一个 bin 一条队列**：`mi_theap_t.pages[75]`（73 个常规 + huge + full），队列只是 4 字段的双向链表；全部 `block_size` 来自 init.c 的编译期静态表，新 theap 整块 memcpy 模板即得。
- **直查数组**：`pages_free_direct[130]` 是 theap 的第一个成员，令 ≤ 1 KiB 的分配「一次下标」直达队首页；多对一的 wsize→bin 映射由 `mi_theap_queue_first_update` 的区间写维护。
- **两个易混上限**：`MI_SMALL_SIZE_MAX`（1 KiB，管快路径与直查数组大小）≠ `MI_SMALL_MAX_OBJ_SIZE`（10 KiB，管小页/统计 S 档）。
- **debug 的 padding 陷阱**：查 bin 的输入是「请求 + 8 B padding」，边界请求（恰好 1024 B、10 KiB、512 KiB）在 debug 与 release 下可能落入不同 bin 甚至不同档位。

## 7. 下一步学习建议

本讲回答了「尺寸如何路由到页队列」。接下来：

- **u3-l4（page map）**：分配解决「尺寸 → 页」，释放要解决「指针 → 页」——两级基数树如何 O(1) 反查，是本讲的镜像话题。
- **u4-l1（mi_malloc 快路径）**：把本讲 4.4 节的快路径放进完整调用链（`mi_malloc` → `_mi_theap_malloc_zero` → `mi_page_malloc_zero`）逐行精读，并用汇编验证「约 7 条指令」。
- **u4-l2（慢路径）**：`mi_find_page` 在队列里找不到可用页时如何扩展 free list、申请新页——本讲只打开了它的入口（page.c:L950-L975）。
- 延伸阅读：`mi_good_size` 的对偶 API `mi_usable_size`（u1-l4）与 test/test-api.c:L296-L317 中按尺寸×对齐双重扫描的 `malloc-aligned13` 用例，可当作本讲综合实践的自动化版本。
