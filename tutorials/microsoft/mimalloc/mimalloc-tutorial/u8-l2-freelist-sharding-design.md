# free list 分片与多分片：mimalloc 的核心创新

## 1. 本讲目标

学完本讲，你应该能够：

1. 复述 readme 中 mimalloc 的两大核心设计——**free list sharding**（每个 mimalloc 页一条独立 free list）与 **free list multi-sharding**（每页再分出本地 `free`/`local_free` 与跨线程 `xthread_free` 多条链），并说明它们各自解决什么问题。
2. 用定量的语言论证：为什么「每页多条 free list」能把并发竞争自然分散到整个堆，使「在单个位置上撞车」的概率随页数增加而趋近于零。
3. 对比「每个 size class 一条全局 free list」与「每页分片 free list」在最坏情形下的差异（分配端原子操作次数、跨线程 free 的竞争点个数、最坏延迟是否有界）。
4. 把设计要点准确映射到 mimalloc 在 `xmalloc-testN`、`sh8bench`、`larsonN`、`leanN` 等基准上的表现。
5. 动手实现一个玩具分配器，实测两种组织的吞吐比，并写出「分片为何胜出」的分析。

本讲是**设计综合课**：不再引入新机制，而是把 u3-l2（三条 free list 的结构）、u4-l1（分配快路径）、u5-l1/u5-l2（本地与跨线程释放路径）、u8-l1（原子原语）已学过的代码拼成一张完整的设计图，回答「mimalloc 凭什么又快又抗并发」。

## 2. 前置知识

### 2.1 一个分配器的两种基本操作

free list 分配器只有两个核心动作：

- **分配（pop）**：从链表头取走一个空闲块交给用户；
- **释放（push）**：把块挂回链表头。

这两个动作天然是「读头指针 → 改头指针」两步。单线程下它们就是两三条普通指令；多线程下，只要两个线程操作**同一条链**，这两步就必须合成一个原子 RMW（compare-and-swap，下称 CAS），失败还要重试。u8-l1 已讲过 CAS 与内存序，本讲只关心一个朴素事实：**对同一缓存行上的原子操作是串行化的**——两个核同时 CAS 同一个位置，硬件只允许一个成功，另一个必须等缓存行在核间「弹跳」一趟，代价通常是几十到上百纳秒，比一次普通访存贵一到两个数量级。

### 2.2 mimalloc 的页与三条链（复习）

mimalloc 页（`mi_page_t`）是一个只装**一种 size class** 块的定长容器，64 位系统上通常 64KiB。每页有三条 free list（u3-l2 详细讲过）：

- `free`：可直接分配的块，**只有属主线程**能碰；
- `local_free`：本线程释放、暂不可分配的块（支撑单调心跳）；
- `xthread_free`：**其他线程**释放的块，原子字段，一次 CAS 头插。

本讲要回答的设计问题是：**为什么恰好是「每页三条」，而不是「每个 size class 一条」？**

### 2.3 readme 的自述

readme 把 multi-sharding 称为 "the big idea!"，并给出了一个非常有味道的类比：竞争被几千条独立 free list 自然摊开，就像跳表（skip list）这类随机化算法——**加入一个随机源，就不再需要更复杂的算法**。这句话是本讲 quantitative 分析的题眼，我们会在 4.4 用数学把它说清楚。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [readme.md](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md) | 两大设计思想的官方表述与基准结论（性能章节） |
| [include/mimalloc/types.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h) | `mi_page_s` 结构：三条链的字段定义、所有权契约与计数不变式的权威注释 |
| [src/free.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c) | 释放路径：本地 free 的三次普通写 vs 跨线程 free 的一次 CAS |
| [src/page.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c) | free list 的初始化（顺序串联）、收割（原子整链交换）与页队列扫描 |
| [src/alloc.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c) | 分配端：从 `page->free` 弹块的三条指令 |
| [include/mimalloc/internal.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h) | `mi_tf_*` 辅助函数：`xthread_free` 低位所有权位的编码与解码 |

## 4. 核心概念与源码讲解

### 4.1 设计问题：一条大 free list 的代价

#### 4.1.1 概念说明

设想一个「教科书式」的分配器：每个 size class 一条全局 free list，配一把锁或用 CAS 维护。这个设计在单线程下工作得很好，但它有一个致命的结构性缺陷——**所有线程在这个 size class 上的每一次分配和每一次释放，都要竞争同一个内存位置（链表头）**。这个位置就是一个「内部竞争点」（internal point of contention）。

readme 在 "bounded" 卖点里专门强调了这一点：mimalloc "has no internal points of contention using only atomic operations"（没有内部竞争点，只使用原子操作）。注意这句话的精确含义：**不是不用原子操作，而是不存在所有线程都要碰的那一个原子位置**。

#### 4.1.2 核心流程

设双线程各做 \( N \) 次操作（一个只分配、一个只释放，即 `xmalloc-testN` 的工作负载原型），一次竞争 CAS 的代价为 \( T_c \)：

**单一大 free list：**

- 分配线程：pop = 一次对共享头的 CAS；
- 释放线程：push = 一次对**同一个**共享头的 CAS；
- 两个方向的操作在同一个缓存行上串行化，合并吞吐上界为：

\[ \text{吞吐} \;\le\; \frac{1}{T_c} \quad\text{（与核数无关，加线程只会更慢）} \]

**每页一条链（\( P \) 个页）：**

- 分配线程只碰自己属主链（**零原子操作**）；
- 释放线程仍做一次 CAS，但落在**块所属那一页**的链头上；
- 两个操作撞上同一个缓存行的概率约为 \( 1/P \)；CAS 重试次数期望为 \( \frac{1}{1-1/P} \to 1 \)。

也就是说：分片没有消除 CAS，它消除的是 **CAS 之间的碰撞**。

#### 4.1.3 源码精读

契约的权威表述在 types.h 的结构体注释里。所有权规则一句话：**非原子字段只有拥有页的线程能碰；跨线程只允许写原子字段 `xthread_free`**：

- [include/mimalloc/types.h:L416-L421](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L416-L421) 注明：非原子字段必须在**取得所有权**后才能访问，而所有权位就藏在 `xthread_free` 的低位，使并发 free 能「顺手」原子认领页。
- [include/mimalloc/types.h:388-L393](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L388-L393) 定义 `mi_thread_free_t` 并注释：最低位为 1 表示页被当前线程拥有，这是「一次 CAS 同时推块 + 认领」的前提。

readme 对两大思想的原始表述（本讲全篇的出发点点）：

- [readme.md:L39-L43](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L39-L43)：free list sharding——「不是每个 size class 一条大链，而是每个 mimalloc 页许多小链」，直接收益是**降低碎片、提高局部性**：时间上相近的分配在空间上也相近。
- [readme.md:L44-L52](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L44-L52)：free list multi-sharding——"the big idea!"：每页再分**多条**链，本地 free 一条、并发 free 一条，跨线程释放从此只需一次 CAS、无需复杂的线程间协调；几千条独立 free list 让竞争**自然分布**到整个堆，并给出跳表类比。
- [readme.md:L64-L66](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L64-L66)：bounded 卖点——无 blowup、有界最坏分配时间、**无内部竞争点**。

#### 4.1.4 代码实践（源码阅读型）

**目标**：用文本搜索验证所有权契约在代码里被严格执行——非原子链的所有写点都在「属主一侧」的代码路径上。

**步骤**：

1. 在仓库根目录执行 `grep -n "free =" src/*.c | grep -v "xthread_free\|pages_free_direct"`，列出对 `page->free`、`page->local_free` 的全部赋值点。
2. 对每一处，阅读它所在的函数，判断调用者是否一定是页的属主线程。

**需要观察的现象**（笔者已执行过此搜索，结果确定）：

- `page->free =` 只出现在 `src/page.c`（收割与扩展：L208、L224、L236、L258、L586、L611）和 `src/alloc.c:56`（快路径弹块）；
- `page->local_free =` 只出现在 `src/page.c`（收割，L177、L209 等）和 `src/free.c:48`（本地释放）；
- 对 `xthread_free` 的 CAS 全库只有 4 处：`src/free.c:87`（跨线程推块）、`src/free.c:403`（放弃所有权）、`src/page.c:196`（属主收割）、`src/arena.c:650`（遗弃页放权）。

**预期结果**：非原子链的全部写点都在属主路径上（分配弹链、收割、扩展初始化、本地释放）；跨线程代码只碰 `xthread_free`。这正是「多条链」能成立的根基：**没有分片，就没有安全的非原子访问**。

#### 4.1.5 小练习与答案

**练习 1**：如果把三条链合并成一条（页只有 `free`），跨线程 free 必须做什么才能不破坏链表？
**答案**：要么给这条链加锁，要么把 push 做成 CAS、并把 pop 也做成 CAS（因为其他线程可能同时改头指针）。更糟的是 `used` 计数也得原子化。合并成一条链意味着**所有访问该链的线程都要付出原子操作**；拆成三条后，属主的两条链（`free`/`local_free`）可以纯普通读写。

**练习 2**：为什么说「竞争点个数」是比「是否用了锁」更本质的指标？
**答案**：无锁 ≠ 可扩展。一个用 CAS 维护的全局链表同样无锁，但所有线程竞争同一个缓存行，吞吐被 \( 1/T_c \) 封顶、不随核数扩展。mimalloc 的做法是把竞争点从「每 size class 一个」变成「每页一个」，个数等于堆中页数（数千量级），碰撞概率自然趋零。

### 4.2 第一层分片：free list sharding——每页一条链

#### 4.2.1 概念说明

第一层分片解决的是**单线程侧**的两个问题：

1. **局部性**：一个页只装一种块规格，块在页内连续排布；free list 按地址顺序初始化，于是「时间上相邻的分配，地址也相邻」。同一页的块几乎必然落在同一组缓存行与 TLB 表项里，还把同 size 的活跃数据聚在一起，让整个页更可能整体变空。
2. **页更易变空**：一个大链上的空闲块来自四面八方的页，谁也清不空；分片后每页的空闲块只属于自己，`used==0` 成为可达状态，eager page purging（变空即归还 OS）才有操作对象（readme [L53-L56](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L53-L56) 明确说变空概率的增加正是来自 free list sharding）。

#### 4.2.2 核心流程

一个新页从 arena 拿到内存后，属主线程做「分批初始化」：

1. 计算本次扩展数量 `extend`（受 `MI_MAX_EXTEND_SIZE = 8KiB` 约束，按块大小折算，避免触碰太多内存）；
2. 把 `[capacity, capacity+extend)` 区间的块**按地址顺序**串成单链表；
3. 头插到 `page->free`，`capacity += extend`。

之后每次分配就是「弹出链头、`used++`」。当快路径弹空（`free == NULL`）才走慢路径找页/扩展/收割（u4-l2 已讲）。

#### 4.2.3 源码精读

**顺序初始化——局部性的直接来源**。[src/page.c:L589-L612](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L589-L612) 的 `mi_page_free_list_extend` 从 `page->capacity` 位置出发，把每个块的 `next` 指向**下一个相邻地址**的块，最后头插进 `page->free`。因此新页上的连续分配在地址上是严格连续的。（secure 模式另有随机化版本 `mi_page_free_list_extend_secure`，L533-L587，用分片交错打乱顺序，是安全性与局部性的权衡。）

**分配端只有三条指令**。[src/alloc.c:L52-L57](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L52-L57)：`block = page->free; page->free = block->next; page->used = used+1;`——两次普通读、两次普通写，无原子（u4-l1 精读过，此处只作设计视角引用）。

**页队列扫描优先复用更满的页**——「让页变空」的推手。[src/page.c:L800-L814](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L800-L814)：在 `mi_page_queue_find_free_ex` 的候选挑选中，优先选 `used` 更高的非 mostly-used 页（"prefer to reuse fuller pages (in the hope the less used page gets freed)"），甚至直接释放全空候选（L807-L809）。分片让「把块集中到少数页、让其余页变空」成为可行的策略。

**变空后的防抖退役**。[src/page.c:L414-L415](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L414-L415) 定义 `MI_RETIRE_CYCLES = 16`；[src/page.c:L439-L456](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L439-L456) 让全空页滞留约 16 个管理节拍再真正释放，避免「释放-再申请」抖动。这是第一层分片收益链条（局部性 → 页变空 → 归还 OS）的最后一环。

#### 4.2.4 代码实践

**目标**：用可运行的小实验验证「顺序 free list 带来地址连续性」。

**步骤**：

1. 参照 `test/test-api.c` 写一个最小程序，在一个全新堆上连续 `mi_malloc(40)` 100 次（避免混入其他分配，可在 `main` 开头先做一次分配预热）。
2. 打印相邻指针差值 `p[i+1] - p[i]`，并与 `mi_good_size(40)` 的返回值对照。
3. 释放全部指针，再按**倒序**重新分配 100 次，再次打印差值。

**需要观察的现象**：第一轮的差值恒定且等于该 bin 的块大小（40 字节请求落在哪个 bin 由 `_mi_bin` 决定，具体数值待本地验证）；倒序释放后重新分配时，得到的地址序列大致也连续，但方向可能反转——这正是 readme 解读 `sh6bench` 时提到的「reverse free-ing」模式（[readme.md:L774-L777](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L774-L777)）能被 mimalloc 廉价处理的原因：头插链表对倒序释放天然友好。

**预期结果**：地址差恒定 = 顺序初始化的直接证据；`MIMALLOC_SHOW_STATS=1` 下 100 个 40 字节块全部落在一个 bin 行。

#### 4.2.5 小练习与答案

**练习 1**：为什么「每页一种 size class」是局部性论证的前提？
**答案**：若一页混装多种规格，free list 串联的块地址步长不等，连续分配不再地址连续；而且页变空要求所有规格的块都被释放，概率大幅下降。单一规格使块像数组一样排布，free list 退化成「隐藏的 bump pointer」。

**练习 2**：`mi_page_extend_free` 为什么每次至多初始化约 8KiB 的块，而不是一次性初始化整页？
**答案**：见 [src/page.c:L655-L659](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L655-L659) 的注释——避免触碰太多（尚未 commit 的）内存以降低 RSS；注释还引用了 `lean` 基准：把扩展量从 1 提到 8 会让 RSS 增加 50%。这是「局部性收益」与「物理内存开销」之间的显式权衡。

**练习 3**：`leanN` 上 mimalloc 比 tcmalloc 快 13%，为什么作者认为这超出了纯分配开销能解释的范围？
**答案**：[readme.md:L741-L744](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L741-L744) 给出猜想：更好的分配局部性改善了程序**其他计算**的缓存命中——分配器的局部性收益会外溢到整个程序，而纯分配微基准测不到这部分。

### 4.3 第二层分片：multi-sharding——每页两条释放链

#### 4.3.1 概念说明

第一层分片后仍有死角：一个线程释放**别的线程**拥有的页里的块时，若只有一条链，属主的非原子访问（pop、`used` 计数）就会与外部 push 数据竞争。常见的解法有三种：全局锁、每页锁、或把跨线程 free 攒到队列里延迟处理。mimalloc 的答案是**第二层分片**：把释放流按「释放者是否是属主」拆到两条链上——

- 属主释放 → `local_free`（普通写，三次访存，零原子）；
- 他人释放 → `xthread_free`（一次 CAS 头插，不碰 `used`、不碰 `free`）。

于是**属主的分配路径完全无原子操作**，而跨线程释放虽然用原子，但落在页私有的字段上，多个线程释放不同页的块时互不干扰。readme 的表述是："Free-ing from another thread can now be a single CAS without needing sophisticated coordination between threads"（[readme.md:L46-L48](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L46-L48)）。

#### 4.3.2 核心流程

`mi_free` 的完整分流（u5-l1/u5-l2 讲过路径，这里按**竞争视角**重新归纳）：

```
mi_free(p)
  ├─ 反查 page（默认构建：纯算术对齐，不查表）
  ├─ xtid = 当前线程id XOR page->xthread_id      // 一次读 + 一次异或
  ├─ xtid==0        → 属主 + 干净页：mi_free_block_local   // 3 次普通写
  ├─ xtid<=flagmask → 属主 + 特殊页：generic local          // 同上，多些检查
  ├─ 低2位==0       → 他人 + 干净页：mi_free_block_mt      // 1 次 CAS
  └─ 其余           → 他人 + 特殊页：generic mt             // 1 次 CAS，多些检查
```

`mi_free_block_mt` 的 push 是 Treiber 栈式：先写 `block->next = 当前链头`，再 CAS 把链头换成 `block`。CAS 的期望值失败时会被更新为最新链头，循环重试。**没有 ABA 问题**，因为只有属主会把元素取下链（收割时整链交换），push 方之间不产生「取下再挂回」的窗口。

跨线程 free 还**不改 `used`**：账目在属主收割时批量修正。`alive = used − |thread_free|`（[types.h:L408-L409](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L408-L409) 的不变式），所以他人 free 一个块绝不影响属主对页状态的判断。

#### 4.3.3 源码精读

**属主路径：三次普通写**。[src/free.c:L44-L48](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L44-L48)：`used = page->used - 1`、`block->next = page->local_free`、`page->local_free = block`。注意进的是 `local_free` 而不是 `free`——这一步分拣正是 u5-l3 心跳机制的基础，也让「free 耗尽」成为属主可预测的事件。

**跨线程路径：一次 CAS**。[src/free.c:L80-L87](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L80-L87)：读 `tf_old`、写新块的 next、`mi_atomic_cas_weak_acq_rel(&page->xthread_free, &tf_old, tf_new)`。对照 u8-l1：成功序 acq_rel（发布的链头对收割方的后续读取可见），失败序 acquire（重试时能读到别人刚发布的 next）。低位 `new_owned` 的处理（L85-L86）让这次 CAS 在页被遗弃时**顺手认领所有权**，一次原子操作干两件事。

**分流点**。[src/free.c:L228-L244](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L228-L244)：`xtid = _mi_prim_thread_id() ^ mi_page_xthread_id(page)`，用一次异或同时判出「是否属主」与「页标志是否为零」，四个分支（u5-l1 的四象限）。

**所有权位的编码**。[include/mimalloc/internal.h:L1089-L1097](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1089-L1097)：`mi_tf_block` 屏蔽最低位取块指针、`mi_tf_is_owned` 测最低位、`mi_tf_create` 拼装。指针按 8 字节对齐，最低两位本来空闲——**一个免费的标志位**。

**结构体布局也在为这两层分片服务**。[include/mimalloc/types.h:L425-L456](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L425-L456)：`free`、`used`、`local_free` 在第一个缓存行内（属主热字段），`xthread_free`、`theap`、`heap` 排到下一行（跨线程字段），L442 的注释 `// next cache line` 明示了这一意图——属主的普通写与他人的 CAS 落在**不同缓存行**上，连「假共享」（false sharing）都避免了。这正是 `cache-scratch` 基准考察的内容（readme [L787-L795](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L787-L795)）。

#### 4.3.4 代码实践

**目标**：用**真实 mimalloc** 做一个双线程交接微基准，验证「跨线程 free 只贵一次 CAS」——即跨线程交接与同线程分配释放的吞吐差距应当**很小**。

**步骤**：

1. 写一个程序，模式 A（本地）：单线程循环 100 万次 `mi_malloc(64)` 存入数组再全部 `mi_free`，计时。
2. 模式 B（跨线程）：线程 A 只 `mi_malloc(64)` 并把指针塞进一个有锁（或 CAS）的队列，线程 B 取出后 `mi_free`，共 100 万对，计时（队列开销两模式分摊方式不同，注意在分析里扣除）。
3. 用 release 构建（`-O2`）各跑 5 次取中位数。

**需要观察的现象**：模式 B 每对操作多出的时间应当只有「一次几乎无竞争的 CAS + 队列开销」的量级（几纳秒到十几纳秒），不应出现数量级的劣化。

**预期结果**：若 mimalloc 采用全局链设计，模式 B 会因每对操作在共享链头上碰撞而显著劣化；实际差距很小恰恰是 multi-sharding 的**反面验证**。具体倍率待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：跨线程 free 为什么可以不更新 `used`？不更新会不会让属主误判「页已满」？
**答案**：`used` 是非原子字段，只有属主能写；跨线程 free 改它就是数据竞争。不改的代价是 `used` 暂时高估存活数——属主可能短期把页当满页处理（满页会被 abandon，u6-l4），但属主收割 `xthread_free` 时会数出链长并把 `used` 扣减回来（4.4.2 的收割流程），账目最终一致。`alive = used − |thread_free|` 的不变式保证不会**永久**误判。

**练习 2**：两个外部线程同时 free 同一页的两个块，会发生什么？
**答案**：两者都对这一页的 `xthread_free` 做 CAS，硬件串行化，一个成功、一个带着最新链头重试后成功，块都被头插进同一条链。没有锁、没有等待，最坏多几次重试。而如果两个线程 free 的是**不同页**的块，两次 CAS 落在不同缓存行，完全并行——这就是「竞争分布在堆上」的微观形态。

**练习 3**：为什么 `free` 与 `local_free` 要拆开，而不是合并成一条属主链？
**答案**：合并后「属主 free 的块立即可分配」，`free` 永不耗尽，u5-l3 的单调心跳（`mi_register_deferred_free` 需要确定性地知道「何时积累了一批待回收对象」）就无法实现。拆链让「free 弹空」成为确定性事件，两拍之间的分配次数有硬上界——这是给 Koka/Lean 这类引用计数运行时的承诺，types.h [L401-L406](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L401-L406) 的注释写明了这一动机。

### 4.4 量化对比与基准映射：玩具分配器实测

#### 4.4.1 概念说明

现在把两层分片合起来做一个可运行的对照实验：实现两个玩具分配器——

- **变体 G（global）**：整个 size class 一条全局 free list，pop 与 push 都是 CAS（无锁，但有单点竞争）——「教科书式」设计；
- **变体 S（sharded）**：块归属固定页；属主从每页的 `free` 链**普通读写**式分配，耗尽时用一次原子交换整链收割该页的 `xthread_free`；释放线程按块归属 CAS 头插对应页的 `xthread_free`——mimalloc 的两层分片骨架。

两个变体共享同一个 SPSC 环形队列交接指针，队列开销两边完全相同，可互相抵消，从而把测量差异**隔离在 free list 组织方式**上。工作负载就是 `xmalloc-testN` 的原型：**一个线程只分配、一个线程只释放**，各 100 万次。

#### 4.4.2 核心流程

变体 S 的属主分配逻辑（伪代码）：

```
sharded_alloc():
  loop:
    for p in 0..PAGE_COUNT:                       # 1) 属主链：普通读写
      if pages[p].free != NULL: 弹出头块返回
    for p in 0..PAGE_COUNT:                       # 2) 收割：每页一次原子交换
      head = atomic_exchange(pages[p].xthread_free, NULL)
      if head != NULL: 接到 pages[p].free 头部
    若仍无块可用，说明全部在途 → 让步重试
```

收割对应 mimalloc 的 `mi_page_thread_free_collect`：[src/page.c:L186-L201](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L186-L201) 用一次 `mi_atomic_cas_weak_acq_rel` 整链交换把 `xthread_free` 置空，再交 `mi_page_thread_collect_to_local`（[L150-L183](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L150-L183)）走到尾部、数出块数 \( k \)、一次修正 `used -= k`、整链接到 `local_free`。**一次原子操作收割 \( k \) 个块，每块摊销 \( 1/k \) 次原子**——这是第三重摊销。

定量对比（最坏情形）：

| 维度 | 变体 G（每 size class 一条） | 变体 S / mimalloc（每页多条） |
|---|---|---|
| 分配端原子操作 | 每次 1 CAS（必竞争） | **0**（属主普通读写） |
| 本线程 free | 1 CAS | **0**（3 次普通写） |
| 跨线程 free | 1 CAS，且与所有分配/释放撞同一行 | 1 CAS，落在块所属页，碰撞概率 \( \approx 1/P \) |
| 竞争点个数 | 每 size class 1 个 | ≈ 堆中页数（数千） |
| 吞吐可扩展性 | 被 \( 1/T_c \) 封顶，加核无效 | 随核数近线性 |

再补一个 readme 跳表类比的精确化：把 100 万次跨线程 free 看作向 \( P \) 个页（箱子）随机投球（每个块归属哪个页由分配时的页序决定，近似随机），则最忙页的负载期望为 \( \Theta(\ln P / \ln\ln P) \)——**最大竞争也只是对数级增长**，这正是「加入随机源就不需要更复杂算法」的数学含义。

#### 4.4.3 源码精读（玩具分配器，示例代码）

下面的完整程序是**示例代码**（不是 mimalloc 源码），刻意模仿 mimalloc 的命名与手法：

```c
// toy-shard.c —— 示例代码：全局单链 vs 每页分片 的双线程交接微基准
// 编译: gcc -O2 -pthread toy-shard.c -o toy-shard && ./toy-shard
#define _GNU_SOURCE
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdatomic.h>
#include <pthread.h>
#include <sched.h>
#include <time.h>

#define PAGE_BLOCKS 1024          // 每页块数（模拟 64KiB 页 / 64B 块）
#define PAGE_COUNT  64            // 堆中同 size class 的页数
#define TOTAL       (1000*1000)   // 分配/释放各 100 万次
#define RING_SIZE   1024          // SPSC 环容量（两变体相同，开销抵消）

typedef struct block { struct block* next; } block_t;

static block_t pool[PAGE_COUNT * PAGE_BLOCKS];    // 块池：归属页 = 下标/PAGE_BLOCKS

/* ---------- 变体 G：每个 size class 一条全局 free list ---------- */
static _Atomic(block_t*) g_free;
static block_t* g_alloc(void) {                   // pop：CAS（对照 mi_page_malloc，但必须原子）
  block_t* head = atomic_load_explicit(&g_free, memory_order_relaxed);
  while (head == NULL)                            // 全部在途，等释放线程归还
    head = atomic_load_explicit(&g_free, memory_order_acquire);
  while (1) {
    block_t* next = head->next;                   // 只有本线程弹链，next 稳定（见分析）
    if (atomic_compare_exchange_weak_explicit(&g_free, &head, next,
        memory_order_acq_rel, memory_order_acquire)) return head;
  }
}
static void g_free_push(block_t* b) {             // push：CAS，与 g_alloc 撞同一缓存行
  block_t* head = atomic_load_explicit(&g_free, memory_order_relaxed);
  do { b->next = head; }
  while (!atomic_compare_exchange_weak_explicit(&g_free, &head, b,
           memory_order_acq_rel, memory_order_acquire));
}

/* ---------- 变体 S：每页 free(属主) + xthread_free(跨线程) ---------- */
typedef struct page {
  block_t*  free;                 // 仅属主线程读写（模仿 mi_page_t.free）
  _Atomic(block_t*) xthread_free; // 跨线程 CAS 头插（模仿 mi_page_t.xthread_free）
} page_t;
static page_t pages[PAGE_COUNT];

static size_t page_of(block_t* b) { return (size_t)(b - pool) / PAGE_BLOCKS; }  // 模拟 page map

static size_t harvest_all(void) {                 // 模仿 mi_page_thread_free_collect
  size_t got = 0;
  for (size_t p = 0; p < PAGE_COUNT; p++) {
    block_t* head = atomic_exchange_explicit(&pages[p].xthread_free,
                                             NULL, memory_order_acq_rel);
    if (head) {
      block_t* tail = head;
      while (tail->next) tail = tail->next;       // 数链并入属主链（走到尾部）
      got++;
      tail->next = pages[p].free; pages[p].free = head;
    }
  }
  return got;
}
static block_t* s_alloc(void) {
  for (;;) {
    for (size_t p = 0; p < PAGE_COUNT; p++)
      if (pages[p].free) {                        // 属主链：纯普通读写，零原子
        block_t* b = pages[p].free; pages[p].free = b->next; return b;
      }
    if (harvest_all() == 0) sched_yield();        // 全部在途：让步等释放
  }
}
static void s_free(block_t* b) {                  // 模仿 mi_free_block_mt：一次 CAS
  _Atomic(block_t*)* tf = &pages[page_of(b)].xthread_free;
  block_t* head = atomic_load_explicit(tf, memory_order_relaxed);
  do { b->next = head; }
  while (!atomic_compare_exchange_weak_explicit(tf, &head, b,
           memory_order_acq_rel, memory_order_acquire));
}

/* ---------- SPSC 环：两变体完全相同 ---------- */
static block_t* ring[RING_SIZE];
static _Atomic size_t rhead, rtail;
static void ring_push(block_t* b) {
  size_t t = atomic_load_explicit(&rtail, memory_order_relaxed);
  while (t - atomic_load_explicit(&rhead, memory_order_acquire) == RING_SIZE) ;
  ring[t & (RING_SIZE-1)] = b;
  atomic_store_explicit(&rtail, t + 1, memory_order_release);
}
static block_t* ring_pop(void) {
  size_t h = atomic_load_explicit(&rhead, memory_order_relaxed);
  while (h == atomic_load_explicit(&rtail, memory_order_acquire)) ;
  block_t* b = ring[h & (RING_SIZE-1)];
  atomic_store_explicit(&rhead, h + 1, memory_order_release);
  return b;
}

static int sharded;                               // 当前跑哪个变体
static void* producer(void* arg) {                // 只分配
  (void)arg;
  for (long i = 0; i < TOTAL; i++)
    ring_push(sharded ? s_alloc() : g_alloc());
  return NULL;
}
static void* consumer(void* arg) {                // 只释放
  (void)arg;
  for (long i = 0; i < TOTAL; i++) {
    block_t* b = ring_pop();
    if (sharded) s_free(b); else g_free_push(b);
  }
  return NULL;
}
static double now_sec(void) {
  struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
  return ts.tv_sec + 1e-9 * ts.tv_nsec;
}
static void g_reset(void) {                       // 变体 G 重置：全部块头插回全局链
  atomic_store_explicit(&g_free, NULL, memory_order_relaxed);  // 单线程时刻，relaxed 足够
  for (size_t i = 0; i < PAGE_COUNT * PAGE_BLOCKS; i++) {
    pool[i].next = atomic_load_explicit(&g_free, memory_order_relaxed);
    atomic_store_explicit(&g_free, &pool[i], memory_order_relaxed);
  }
}
static double run_once(int use_sharded) {
  sharded = use_sharded;
  rhead = rtail = 0;
  if (use_sharded) {
    for (size_t p = 0; p < PAGE_COUNT; p++) {
      pages[p].free = NULL; pages[p].xthread_free = NULL;
      for (size_t i = 0; i < PAGE_BLOCKS; i++) {  // 全部块挂回属主链（头插）
        block_t* b = &pool[p * PAGE_BLOCKS + i];
        b->next = pages[p].free; pages[p].free = b;
      }
    }
  } else {
    g_reset();
  }
  pthread_t a, b_;
  double t0 = now_sec();
  pthread_create(&a, NULL, producer, NULL);
  pthread_create(&b_, NULL, consumer, NULL);
  pthread_join(a, NULL); pthread_join(b_, NULL);
  return now_sec() - t0;
}
int main(void) {
  double tg = 1e9, ts = 1e9;
  for (int r = 0; r < 5; r++) { double t = run_once(0); if (t < tg) tg = t; }
  for (int r = 0; r < 5; r++) { double t = run_once(1); if (t < ts) ts = t; }
  printf("global : %7.1f ms  (%6.2f Mops/s)\n", tg*1e3, TOTAL/tg/1e6);
  printf("sharded: %7.1f ms  (%6.2f Mops/s)\n", ts*1e3, TOTAL/ts/1e6);
  printf("speedup: %.2fx\n", tg/ts);
  return 0;
}
```

**注记**：这段示例的关键在四处与 mimalloc 的对应关系：

1. `s_free` 与 [src/free.c:L80-L87](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L80-L87) 的 `mi_free_block_mt` 逐行对应（含成功 acq_rel/失败 acquire 的内存序选择，u8-l1）；
2. `harvest_all` 与 [src/page.c:L186-L201](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L186-L201) 的整链交换对应；
3. `g_alloc` 里「只有属主弹链，所以 `head->next` 稳定」的论证，就是 mimalloc 免 ABA 的同一论证（只有收割方取链，push 方从不摘除元素）；
4. `s_alloc` 属主链的两次普通读写（`b = pages[p].free; pages[p].free = b->next;`）对应 [src/alloc.c:L52-L57](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L52-L57) `mi_page_malloc_zero` 的弹块三指令——分配端零原子操作正是两层分片送出的最大红利。

#### 4.4.4 代码实践

**目标**：实测两种 free list 组织的吞吐比并写出分析。

**步骤**：

1. 把上面的示例代码存为 `toy-shard.c`（建议放在仓库外的练习目录；不要改动 mimalloc 源码树），`gcc -O2 -pthread toy-shard.c -o toy-shard` 编译。
2. 运行 `./toy-shard`，记录两行输出与 speedup。
3. 用 `perf stat -e cache-misses,context-switchs ./toy-shard`（或 `perf c2c`）观察变体 G 的缓存行弹跳证据（可选）。
4. 写一段 200 字左右的分析，回答：为什么分片胜出？分配线程在哪条路径上省掉了原子操作？释放线程的 CAS 为什么「存在但不竞争」？

**需要观察的现象**：变体 G 下两个线程的每次操作都在 `g_free` 这一个缓存行上做 RMW；变体 S 下分配线程几乎全是普通读写，释放线程的 CAS 分散在 64 个页的 `xthread_free` 上。具体加速比待本地验证（在典型多核桌面/服务器上预期为明显加速，数量级取决于核间缓存一致性代价；单核机器上差距会缩小，因为没有跨核弹跳）。

**预期结果**：`speedup > 1` 且通常显著大于 1；若测得 speedup ≈ 1，优先排查：是否绑到了同一核（`taskset`）、编译器是否把变体 S 的循环优化到没有真正竞争、或 `sched_yield` 路径被频繁触发（说明 `harvest_all` 逻辑有误）。

#### 4.4.5 小练习与答案

**练习 1**：变体 S 中释放线程的 CAS 完全无竞争吗？什么时候仍会竞争？
**答案**：不是完全无竞争。同一页的 `xthread_free` 仍是单点：如果两个线程同时释放**同一页**的块，就会在该页上竞争。分片的价值在于把这种碰撞概率压到 \( \approx 1/P \)，并把最忙页的负载压到 \( \Theta(\ln P/\ln\ln P) \)，而不是彻底消灭原子操作。

**练习 2**：把 `PAGE_COUNT` 从 64 改成 1，变体 S 退化成什么？预期吞吐如何变化？
**答案**：退化成「每堆一条跨线程链 + 属主普通链」的两级结构：释放线程之间仍无冲突（单释放线程），但分配线程收割时与释放线程在唯一一个 `xthread_free` 上碰撞，且属主链频繁耗尽、收割频繁发生，摊销优势消失——预期吞吐显著下降、逐步趋近变体 G。这正是综合实践要扫的参数。

**练习 3**：readme 为什么说 `xmalloc-testN` 上 "most allocators do not do well"，连 jemalloc/tcmalloc 也包括在内？
**答案**：见 [readme.md:L781-L785](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L781-L785)。该基准模拟「部分线程只分配、部分线程只释放」的非对称负载（来自真实大型服务器的观察）。任何把跨线程 free 汇入全局/size-class 级结构的设计都会在这个负载下把两条流绞在同一个竞争点上；mimalloc 的 "non-contended sharded thread free lists" 让释放流分散到页级，因此大幅领先，只有 rpmalloc、tbb、glibc 同样能扩展。

### 4.5 基准结论映射：设计要点如何兑现为数字

> 本模块是 4.4 的收束，把源码要点与 readme 性能章节一一对应。它没有独立的代码实践（实践已并入 4.4 与第 5 节），重点是建立「设计决策 ↔ 基准现象」的条件反射。

#### 4.5.1 概念说明

一个分配器设计好不好，最终要看它在**多样性负载**下是否稳定——readme 强调 "it does consistently well over the wide range of benchmarks"（[readme.md:L669-L674](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L669-L674)）。两层分片分别在两类负载上兑现：

- **multi-sharding** 兑现为**并发交接类**基准的优势（`xmalloc-testN`、`sh8bench`、`larsonN`）；
- **free list sharding** 兑现为**局部性类**基准的优势（`leanN`、`cfrac`）与更低的碎片/内存占用（页更易变空 → eager purging）。

#### 4.5.2 核心流程与映射表

| 基准 | 负载特征 | 对应设计点 | readme 结论 |
|---|---|---|---|
| `xmalloc-testN` | 部分线程只分配、部分只释放（非对称） | 每页 `xthread_free`，释放流分散、分配零原子 | "non-contended sharded thread free lists pays off… outperforms others by a very large margin"（[L781-L785](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L781-L785)） |
| `sh8bench` | 对象在线程间迁移（sh6 + 迁移） | 跨线程 free 一次 CAS + 收割摊销 | tcmalloc 加迁移后慢 10 倍，mimalloc 稳定（[L772-L779](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L772-L779)） |
| `larsonN` | 服务器 bleeding：跨线程分配释放 | 同上 + abandon/reclaim（u6-l4） | 比 tcmalloc/jemalloc 快不少，归因于对象跨线程迁移（[L748-L750](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L748-L750)） |
| `sh6bench` | 单线程「倒序释放」 | 顺序初始化 free list + 头插释放 | 比 jemalloc 快 2.5 倍以上（[L774-L777](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L774-L777)） |
| `leanN` | 真实大规模并发编译负载 | 每页单一 size class 的局部性 | 13% 加速，猜想局部性外溢到其他计算（[L734-L744](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L734-L744)） |
| `cache-scratch` | 多线程假共享探测 | `mi_page_t` 缓存行布局：属主字段与跨线程字段分行 | 仅 tbb/rpmalloc/mesh 同样完全避免（[L787-L795](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L787-L795)） |

#### 4.5.3 小练习与答案

**练习**：`mstressN` 会反复创建销毁工作线程并让对象活过创建它的线程——这超出了本讲的 free list 机制，靠什么支撑？
**答案**：靠 u6-l4 的 abandon/reclaim：线程退出时含活块的页不释放，遗弃进 arena 的 abandoned 位图，由其他线程按 size class 认领。multi-sharding 是它的前提——正因为跨线程 free 不减 `used`、所有权位随 CAS 流转，一个「无主」页的账目才能被任意认领者安全接手。

## 5. 综合实践

**任务：竞争稀释曲线——把「contention is naturally distributed over the heap」画出来。**

readme 声称几千条 free list 让「在单个位置上撞车的概率」很低。请用 4.4 的玩具分配器把这句定性描述变成一条曲线：

1. 把 `PAGE_COUNT` 参数化（命令行参数或宏），分别取 1、2、4、8、16、32、64、128，其余不变，各跑 5 次取最优，记录变体 S 的吞吐。
2. 同时记录变体 G 的吞吐作为水平基线（它不随 `PAGE_COUNT` 变化，因为只有一条链）。
3. 以 `PAGE_COUNT` 为横轴（对数刻度）、吞吐为纵轴画图（gnuplot 或 Python matplotlib 均可）。
4. 在图上标注三个理论参考点：
   - \( P=1 \)：两级结构但收割竞争剧烈，接近变体 G；
   - \( P \) 中等：竞争稀释开始起效；
   - \( P \) 很大：属主链越来越长（总块数固定），收割次数下降、摊销上升，吞吐趋于饱和。
5. 写一份简短报告（半页即可），必须回答三个问题：
   - 加速主要来自「分配端零原子」还是「释放端 CAS 免碰撞」？（提示：做一个只改 `s_alloc` 退回 CAS 弹链的对照变体，即可分离两个因素。）
   - 实测曲线的拐点与 \( 1/P \) 碰撞模型的预测是否一致？
   - 把 `s_free` 改成先对块做 `page_of` 再 CAS——mimalloc 里对应的是 free 快路径的页反查（默认构建是纯算术对齐，u3-l4），你的玩具里这一步的代价是否可以被观察到？

**预期结果**：吞吐随 `PAGE_COUNT` 上升先快速爬升、后趋平；对照变体证明两个因素都存在但分配端零原子占主导（待本地验证）。

## 6. 本讲小结

- **第一层分片（每页一条 free list）**解决单线程侧问题：顺序初始化的链表让相邻分配地址相邻（局部性外溢到程序其他计算，`leanN` 的 13%）、单一 size class 让页可达全空（eager purging 与 retire 的前提）。
- **第二层分片（每页三条链）**解决并发侧问题：属主的 `free`/`local_free` 走普通读写（分配零原子、本地 free 三次普通写），跨线程 free 落在页私有的原子字段 `xthread_free` 上（一次 CAS + 低位所有权位顺手认领）。
- **竞争的数学**：单一大链把吞吐封顶在 \( 1/T_c \)；\( P \) 条分片链使碰撞概率 \( \approx 1/P \)、最忙链负载 \( \Theta(\ln P/\ln\ln P) \)，CAS 仍在但不再排队——这就是 readme 的跳表类比。
- **三重摊销**：属主分配零原子（0/次）、跨线程 free 单次几乎无竞争 CAS（1/次）、属主收割整链交换（1/k 次/块）。
- **契约先于技巧**：`grep` 可验证非原子链的全部写点都在属主路径、CAS 全库仅 4 处；所有权位藏在指针空闲低位、属主与跨线程字段分缓存行——这些细节是分片正确性与低竞争的工程保障。
- **基准映射**：非对称交接（`xmalloc-testN`）、对象迁移（`sh8bench`/`larsonN`）兑现 multi-sharding；局部性（`leanN`）、倒序释放（`sh6bench`）兑现 free list sharding；缓存行布局兑现 `cache-scratch`。

## 7. 下一步学习建议

本讲把 free list 的**动态行为**讲完了，下一讲 u8-l3《原子位图内部：bitmap.c 的区间查找与 x64 优化》转向 free list 之下的另一块共享数据结构——arena 的 slice 位图：它是全进程共享的，无法靠分片私有化，只能靠「一次原子 RMW 找到并占有连续区间」的算法设计避免竞争（`mi_bbitmap_try_find_and_clearN`），并大量使用 u8-l1 的位扫描原语与 x64 BMI 指令优化。建议阅读顺序：先读 [src/bitmap.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.h) 的分桶结构，再对照 [src/arena.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c) 的调用点理解它如何服务于「从 arena 零售 64KiB slice」。如果你更关心应用侧，也可以先跳到 u9-l3 统计系统，学会用 `MIMALLOC_SHOW_STATS=1` 的输出验证本讲关于页变空、purge 与碎片的论断。
