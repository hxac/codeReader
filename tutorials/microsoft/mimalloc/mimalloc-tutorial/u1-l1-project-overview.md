# mimalloc 是什么：一个高性能通用分配器的定位与设计

> 本讲是整本学习手册的第一讲，不要求你写一行代码、也不要求编译任何东西。
> 我们只做一件事：把 `readme.md` 和 `doc/release-notes.md` 这两份「项目宪法」读透，
> 建立起对 mimalloc 的整体认知地图。后续每一讲深入源码时，你都会回到这张地图。

---

## 1. 本讲目标

学完本讲，你应该能够：

1. 用两三句话向同事说清 **mimalloc 是什么、谁在用它、解决什么问题**。
2. 说出 mimalloc 的 **八个设计要点**（small and consistent / free list sharding / free list multi-sharding / eager page purging / secure / first-class heaps / bounded / fast），并理解其中两个核心创新（sharding 与 multi-sharding）的直觉。
3. 区分 **v1 / v2 / v3 三个维护分支** 的定位与差异，知道本手册基于的 v3 相对 v2 改进了什么。
4. 熟悉 `readme.md` 的章节布局，以后遇到构建、覆盖、调优、工具链问题时知道去哪一节找答案。

---

## 2. 前置知识

本讲是入门第一课，只假设你写过 C/C++，下面这些名词用通俗语言解释一遍。

### 2.1 内存分配器是什么

C 程序里你天天写 `malloc(100)` / `free(p)`，但 `malloc` 本身不是系统调用，而是 **内存分配器（allocator）** 提供的函数。分配器向操作系统批发一大块内存（Linux 用 `mmap`，Windows 用 `VirtualAlloc`），再零售成小块响应程序请求。glibc 自带的分配器叫 **ptmalloc**，其他著名实现有 jemalloc（Firefox/FreeBSD）、tcmalloc（Chrome）、Hoard 等。mimalloc 就是这个家族里的一员。

### 2.2 free list：分配器的「零钱盒」

分配器把空闲内存块串成单链表，这个链表叫 **free list**。分配就是从链表头摘一个块，释放就是挂回链表头——都是 O(1)。这个朴素结构是理解 mimalloc 全部创新的起点。

### 2.3 size class 与 mimalloc page

把请求尺寸归入有限个档位（如 32B、48B、64B……）就是 **size class**；同一档位的块集中放在一个 **mimalloc page** 里（64 位系统上通常是 64KiB）。这两个概念本讲只需有印象，第 3 单元会展开。

### 2.4 多线程竞争与 CAS

多个线程同时想改同一个 free list 头指针时会互相踩踏，需要原子操作协调。**CAS**（Compare-And-Swap，比较并交换）是最常用的一种：『只有当内存值仍等于旧值时才替换成新值，否则失败重试』。竞争越激烈，重试越多，性能越差——mimalloc 的核心思路就是**让线程几乎不需要竞争**。

### 2.5 虚拟内存三件事：reserve / commit / purge

- **reserve（保留）**：向 OS 预订一段虚拟地址空间，不占物理内存；
- **commit（提交）**：让这段地址真正可读写，物理内存开始被占用；
- **purge（清洗）**：告诉 OS「这块我暂时不用了」，Windows 上是 `MEM_DECOMMIT` / `MEM_RESET`，Linux 上是 `MADV_DONTNEED` / `MADV_FREE`，从而降低实际内存占用（RSS）。

理解这三个词，后面读「eager page purging」就毫无障碍。

---

## 3. 本讲源码地图

本讲的两份核心材料都在文档层，不涉及 `.c` 代码：

| 文件 | 作用 | 本讲怎么用 |
| --- | --- | --- |
| [readme.md](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md) | 项目总纲：定位、八个设计要点、三个版本、构建方法、使用方式、环境变量、安全/调试/guarded 模式、malloc 覆盖、工具链（Valgrind/ASAN/ETW）、基准测试 | 本讲主战场，逐节精读前半部分 |
| [doc/release-notes.md](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/doc/release-notes.md) | GitHub 二进制/源码发布说明：三版本定位一句话、发布规则 | 第 4.4 节版本谱系的佐证 |

readme 中还提到、后续讲义会真正打开的文件（本讲只需知道存在）：

- `include/mimalloc.h` —— 公共 API 声明（第 u1-l4 讲）；
- `CMakeLists.txt` 与 `src/static.c` —— 构建入口与单文件编译（第 u1-l2 讲）；
- `src/alloc.c`、`src/free.c`、`src/page.c`、`src/arena.c` —— 分配/释放/页/内存区四大核心源文件（第 u3–u6 单元）。

> 阅读建议：把 `readme.md` 在编辑器里打开对着看，它只有约 1000 行，是整个项目信息密度最高的文件。

---

## 4. 核心概念与源码讲解

### 4.1 mimalloc 的定位：为运行时系统而生的通用分配器

#### 4.1.1 概念说明

mimalloc 读作 «me-malloc»，是一个 **通用（general purpose）分配器**：它不针对某种特定负载，而是想在任何程序里都表现良好。它最初由 Daan Leijen 为 **Koka** 和 **Lean** 两个函数式语言的运行时开发——这个出身很重要，它解释了 mimalloc 的两个独特气质：

1. **小巧一致**：约 1 万行 C 代码，便于嵌入和改造；
2. **为 GC 运行时提供钩子**：单调「心跳」与延迟释放接口，让引用计数运行时获得有界的最坏情况时间。

同时它是工业级的：Bing、Azure、《死亡搁浅》PC 版、Unreal Engine 4.25+、SPAdes 等都在用它。

#### 4.1.2 核心流程

mimalloc 的使用方式呈「三级递进」，侵入性从低到高：

```text
方式一（零改动）  : LD_PRELOAD=libmimalloc.so  myprogram
                    程序所有 malloc/free 自动改走 mimalloc
方式二（链接替换）: gcc -o myprogram -lmimalloc myfile.c
                    主动链接 mimalloc，用 mi_ 前缀 API
方式三（完全控制）: mi_heap_new() 建多个一等堆，按区域分配、整堆销毁
```

本讲只需理解方式一的存在及意义：**不重编译、不改源码就能换分配器**，这让 mimalloc 可以被快速拿来和系统分配器做对比实验。

#### 4.1.3 源码精读

readme 开头用四行给出了项目的一句话定位、出身和最新版本号：

- [readme.md:L13-L16](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L13-L16)
  这段说明 mimalloc 是一个性能优异的通用分配器，最初为 Koka 与 Lean 的运行时系统而开发。

- [readme.md:L18-L20](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L18-L20)
  三个分支的最新发布：`v3.5.0`（recommended）/ `v2.5.0`（stable）/ `v1.15.0`（legacy），均发布于 2026-08-18。

- [readme.md:L22-L27](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L22-L27)
  「drop-in replacement」：在 ELF 系统（Linux/BSD）上用 `LD_PRELOAD=/usr/lib/libmimalloc.so myprogram` 即可无改动替换 malloc，Windows 也有对应的动态覆盖方案。

设计清单的第一条强调「小而一致」：

- [readme.md:L30-L38](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L30-L38)
  全库约 10k 行，数据结构简单一致；为运行时提供心跳与延迟释放钩子；已被移植到 Windows、macOS、Linux、WASM、各 BSD、Haiku、MUSL 等系统；同时在数千台机器的大规模服务上有优秀的最坏情况延迟。

谁在用它：

- [readme.md:L162-L170](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L162-L170)
  Usage 一节列出的真实用户：Bing、Azure、Death Stranding、Unreal Engine、SPAdes。

#### 4.1.4 代码实践

**实践一：核对「三行版本表」与 git 历史（阅读型，约 5 分钟）**

1. 实践目标：确认手册所用快照的版本状态，学会把 readme 的说法和仓库客观状态互相对照。
2. 操作步骤：
   - 打开 [readme.md:L18-L20](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L18-L20)，记下三个版本号；
   - 在仓库根目录执行 `git log --oneline -5`，观察最近几条提交信息；
   - 再执行 `git log --oneline --all | grep -i "bump version" | head -5`。
3. 需要观察的现象：git 历史里应能看到与 readme 版本号对应的「bump version」提交。
4. 预期结果：本手册所用 HEAD `cd69707c` 的近期历史中出现 `bump version to v3.5.0`、`bump version to v1.15.0` 等提交，与 readme L18–L20 一致。（若你在自己克隆的仓库里最新提交不同，记录差异即可，这正说明三个分支各自独立发版。）

**实践二（可选）：在本机确认是否已安装 mimalloc**

在终端执行 `ls /usr/lib/libmimalloc* 2>/dev/null || ls /usr/local/lib/libmimalloc* 2>/dev/null`。若有输出，说明系统里已装好，第 u1-l2 讲可以直接跳过编译做 `LD_PRELOAD` 实验；若无输出也没关系，下一讲我们会自己构建。此步**不要求运行任何被测程序**，结果属于「待本地验证」范畴。

#### 4.1.5 小练习与答案

**练习 1**：mimalloc 说自己是 `malloc` 的 "drop-in replacement"，这个词是什么意思？靠什么机制在 Linux 上实现？

**参考答案**：意为「直接替换品」——不需要修改或重新编译目标程序。在 Linux 等 ELF 系统上通过动态链接器的预加载机制实现：`LD_PRELOAD=/usr/lib/libmimalloc.so myprogram` 让 `malloc`/`free` 等符号在解析时优先命中 mimalloc 提供的同名定义，从而把所有标准分配调用重定向到 mimalloc。

**练习 2**：readme 为什么特别强调 mimalloc 是「为 Koka 和 Lean 的运行时系统」开发的？这给 mimalloc 带来了哪两个普通分配器没有的特性？

**参考答案**：函数式语言的运行时（尤其是引用计数 GC）对分配器的需求比普通程序更苛刻：分配频率极高、且需要可控的最坏情况延迟。因此 mimalloc 提供了两个专门钩子：(1) 单调「心跳」（heartbeat），(2) 延迟释放（deferred freeing），让运行时能在安全点批量、有界地做回收（见 readme L30-L38）。

**练习 3**：除了性能，「约 10k LOC」这个小体量给 mimalloc 带来了哪些工程上的好处？至少列两条。

**参考答案**：(1) 便于嵌入与二次开发——运行时作者可以把它整棵搬进自己的项目裁剪；(2) 便于移植——正因结构简单，已被移植到 Windows/macOS/Linux/WASM/BSD/Haiku/MUSL 等众多平台；(3) 便于做动态覆盖（dynamic overriding）这类对符号和初始化顺序要求苛刻的集成。

---

### 4.2 核心创新：free list sharding 与 multi-sharding

#### 4.2.1 概念说明

这是 mimalloc 的灵魂，readme 原文称之为 "the big idea!"。传统分配器（包括 ptmalloc 的 fastbin、tcmalloc 的 thread cache）在**每个 size class 维护一条较大的 free list**。mimalloc 做了两层「切片」：

- **第一层：free list sharding（按页分片）** —— 不按 size class 建一条大链表，而是**每个 mimalloc page 一条独立 free list**。一个 page 只装一种尺寸的块，通常 64KiB。
- **第二层：free list multi-sharding（页内再分片）** —— 每个 page 内部再拆出**多条**链表，关键的两条是：本线程释放用的 `free` 链表，和其他线程并发释放用的 `thread_free` 链表。

两层分片分别收获两种红利：

| 分片层级 | 解决的问题 | 收获 |
| --- | --- | --- |
| 按页分片 | 大链表跨页交错导致碎片与缓存局部性差 | 时间上相近的分配在物理上也相近（局部性↑，碎片↓），且页更容易整体变空 |
| 页内多分片 | 跨线程释放要锁或复杂协调 | 跨线程 free 退化成**一次 CAS**，无锁无协调 |

#### 4.2.2 核心流程

用伪代码看两条链表如何协作（示意，非项目源码）：

```text
本线程释放 p:
  p.next = page.free;  page.free = p;        # 普通读写即可，无需原子操作

其他线程释放 p:
  循环 { old = page.thread_free;
         p.next = old;
         if CAS(&page.thread_free, old, p) 成功: 返回; }   # 一次 CAS 推入

拥有者线程在合适时机（分配慢路径、页面回收等）:
  把 thread_free 链表整体「收割」回 free 链表，并顺带检查页面是否已全空
```

为什么这样做竞争就少了？直觉是一个「生日悖论」式的推导（**示例推导，非 readme 原文**）：设堆中有 \( n \) 条相互独立的 free list，某时刻有 \( k \) 个线程同时释放，则至少两线程碰到同一条链表的概率约为

\[
P \;\approx\; 1-\prod_{i=1}^{k-1}\Bigl(1-\frac{i}{n}\Bigr) \;\approx\; \frac{k(k-1)}{2n}
\]

mimalloc 的 \( n \) 是「页数 × 每页链表数」，轻松达到数千；即便 \( k=32 \) 个线程，\( P \approx 496/n \)，也只在个位数百分比以下。readme 把这个思路类比为跳表等随机化算法：**引入随机性/分散性，就不需要更复杂的协调算法**。精确的收割时机与 ABA 等并发细节，留到 u5-l2 与 u8-l2 用源码验证。

#### 4.2.3 源码精读

设计清单里的两条关键陈述（建议逐词读原文）：

- [readme.md:L39-L43](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L39-L43)
  **free list sharding**：不做每个 size class 一条大 free list，而是每个 mimalloc page 持有多条更小的链表，降低碎片、提升局部性——「时间上分配得近，内存上也分配得近」；并注明一个 mimalloc page 只含一种 size class 的块，64 位系统上通常 64KiB。

- [readme.md:L44-L52](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L44-L52)
  **free list multi-sharding（the big idea!）**：不仅按页分片，每页还有多条链表——一条给线程本地 `free`，一条给并发 `free`；于是跨线程释放只需一次 CAS、无需复杂线程间协调；由于存在数千条独立 free list，竞争天然分散到整个堆，单个位置被争抢的概率很低——类似跳表那种「加入随机源就不用更复杂算法」的思路。

这两条设计在基准章节有直接印证：

- [readme.md:L781-L785](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L781-L785)
  `xmalloc-testN` 模拟「一部分线程只分配、另一部分只释放」的不对称负载；readme 明确说 mimalloc 靠「无竞争的分片线程 free list（non-contended sharded thread free lists）」在这一项上大幅领先，只有 rpmalloc、tbb 和 glibc 也能良好扩展。

- [readme.md:L734-L744](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L734-L744)
  `leanN`（Lean 定理证明器编译自己的标准库）比 tcmalloc 快 13%；readme 推测这种超出纯分配基准的收益来自**更好的分配局部性**改善了程序其他部分的速度——正是 sharding 的第一层红利。

#### 4.2.4 代码实践

**实践：把「设计主张 ↔ 基准证据」配对（阅读型，约 15 分钟）**

1. 实践目标：证明你能把 4.2 节的设计主张落到 readme 的基准观察上，而不是停留在口号。
2. 操作步骤：
   - 精读 [readme.md:L39-L52](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L39-L52)（两条分片主张）；
   - 再精读基准说明 [readme.md:L746-L797](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L746-L797)，重点看 `larsonN`、`sh8bench`、`xmalloc-testN` 三段；
   - 填写下面这张表（示例已给出第一行）：

     | 基准 | 负载特征 | 印证的分片层级 | readme 的表述 |
     | --- | --- | --- | --- |
     | xmalloc-testN | 部分线程只分配、部分只释放 | 页内多分片（thread_free） | "non-contended sharded thread free lists pays off" |
     | larsonN | …… | …… | …… |
     | sh8bench | …… | …… | …… |

3. 需要观察的现象：三个基准的负载描述里都出现「对象在线程间迁移」类字眼。
4. 预期结果：你能指出 `larsonN`/`sh8bench`/`xmalloc-testN` 都涉及对象跨线程迁移，因此主要印证 **multi-sharding**；而 `leanN`/`cfrac` 这类单线程或多线程但无迁移的场景，主要收益来自 **按页分片带来的局部性**。完成后请自行翻回原文核对措辞。
5. 若某一项你无法在 readme 中找到明确依据，请写「待确认」而不是猜测。

#### 4.2.5 小练习与答案

**练习 1**：解释「free list sharding」与「free list multi-sharding」的差别，一句话各说清。

**参考答案**：sharding 是**把每个 size class 的一条大 free list 切成每个 mimalloc page 一条**，换局部性与低碎片；multi-sharding 是**在每页内部再切出多条**（本地 free / 跨线程 thread_free 等），把跨线程释放降到一次 CAS，几乎消除线程竞争。

**练习 2**：为什么按页分片反而让「eager page purging」变得可行甚至高效？

**参考答案**：一条大 free list 里混着来自许多页的空闲块，任何一页都很难「整体」变空；按页分片后每页有独立链表，该页的块被全部释放时这条链表立刻归零，页面更容易整体变空——readme L53-L56 中 "with increased chance due to free list sharding" 说的正是这个因果：分片提高了「页变空」的概率，此时再通知 OS 回收物理内存才有意义。

**练习 3**：跨线程 free 用「一次 CAS 推入 thread_free」为什么不需要像传统做法那样加锁或做复杂的内存协调？

**参考答案**：因为所有权边界清晰：`thread_free` 链表属于拥有该页的线程，其他线程只是把块**原子地挂到链表头**（CAS 保证链表不被挂坏），并不读取或消费链表内容，因此不存在生产者/消费者之间的协调问题；消费（收割）只由拥有者线程单方面进行。竞争只剩「多个线程同时抢同一个链表头」，而分片让这种碰撞概率极低。

---

### 4.3 其余设计支柱：purging、secure、first-class heaps、bounded、fast

#### 4.3.1 概念说明

除两条分片外，readme 的设计清单还有五条，共同构成 mimalloc 的完整画像：

- **eager page purging（及时清洗空页）**：页一变空就尽快向 OS 标记「这段内存不用了」（reset 或 decommit），降低真实内存压力，尤其利好长期运行的服务。
- **secure（安全模式）**：编译期开关 `MI_SECURE=ON`，加入 guard page、随机化分配、加密 free list 等，平均性能损失约 10%。
- **first-class heaps（一等堆）**：可以高效创建多个堆、在不同内存区域分配，并且能**整堆一次性销毁**；v3 更进一步支持任意线程在同一个堆上分配。
- **bounded（有界）**：不会发生 blowup（空间爆炸）、最坏情况分配时间有界、元数据开销约 0.2%、全库只用原子操作而无内部竞争点。
- **fast（快）**：在基准套件中稳定优于 jemalloc、tcmalloc、Hoard 等，且内存占用相当。

#### 4.3.2 核心流程

这五条不是孤立的，它们串成一个闭环：

```text
按页分片 ──► 页更容易整体变空 ──► eager purging 及时还物理内存 ──► 长时服务低 RSS
   │
   └─► 页内多分片 ──► 跨线程 free 一次 CAS ──► 无内部竞争点 ──► bounded + fast
                                              │
first-class heaps / subproc  ◄── v3 加强 ─────┘   （整堆销毁：不必逐对象 free）
secure / debug / guarded     ◄── 独立正交的编译期开关，可与上述任意组合
```

注意一个重要事实：**purge 不是 free**。mimalloc 一般不把 OS 内存「还回去」（不解除虚拟地址保留），而是在保留区内做 decommit，让物理内存可被其他进程复用（见 4.3.3 最后一条引用）。

#### 4.3.3 源码精读

- [readme.md:L53-L56](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L53-L56)
  **eager page purging**：页变空（因分片而更常发生）时把内存标记为 unused（reset 或 decommit），降低真实内存压力与碎片，对长期运行的程序尤其有效。

- [readme.md:L57-L60](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L57-L60)
  **secure**：安全模式加入 guard page、随机化分配、加密 free list 等以对抗各类堆漏洞；基准上平均约 10% 的性能代价。

- [readme.md:L61-L63](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L61-L63)
  **first-class heaps**：高效创建并使用多个堆、跨不同区域分配；堆可整体销毁而无需逐对象释放。v3 拥有「真正的一等堆」——任意线程都能在某个堆上分配。

- [readme.md:L64-L70](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L64-L70)
  **bounded + fast**：不受 blowup 影响、最坏情况分配时间有界（至 OS 原语层面）、元数据约 0.2%、无内部竞争点（仅用原子操作）；基准中稳定优于 jemalloc/tcmalloc/Hoard 等。

- [readme.md:L411-L425](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L411-L425)
  安全模式的完整清单：所有页元数据被 guard page 包围；free list 指针用**每页独立的 key** 加密（既防已知指针覆写，也可检测堆损坏）；检测并忽略 double free；free list 以随机顺序初始化、分配在扩展与复用之间随机选择、大块地址随机化；`MI_SECURE_FULL=ON` 会在每个 64KiB 页尾再加 guard page（一般不推荐，代价高且可能触碰 Linux 的 VMA 上限）。

- [readme.md:L364-L373](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L364-L373)
  `MIMALLOC_PURGE_DELAY=N`（v3 默认 1000ms）：空页延迟 N 毫秒后 purge；`0` 表示页一空立刻 purge（更省内存但更慢）；`-1` 完全关闭 purge。同一段还说明「purge」默认是 decommit（Windows `MEM_DECOMMIT`、Linux `MADV_DONTNEED`，立即降 RSS），设 `MIMALLOC_PURGE_DECOMMITS=0` 则改为 reset（`MEM_RESET` / 通常 `MADV_FREE`，不立即降 RSS）——并强调 mimalloc 一般只 purge 而不 free OS 内存。

- [readme.md:L448-L451](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L448-L451)
  guarded 模式的采样参数 `MIMALLOC_GUARDED_SAMPLE_RATE=N`：每 N 次合适的分配放置一个 OS guard page，可以在生产环境低开销地抓潜伏的缓冲区溢出。

#### 4.3.4 代码实践

**实践：给 purge 三种取值建一张「语义—代价」表（阅读型，约 10 分钟）**

1. 实践目标：把 eager purging 从一个名词变成可调的工程参数。
2. 操作步骤：精读 [readme.md:L364-L373](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L364-L373)，完成下表：

   | `MIMALLOC_PURGE_DELAY` 取值 | 行为 | 内存占用 | 性能 |
   | --- | --- | --- | --- |
   | `-1` | …… | …… | …… |
   | `0`（默认之外的特殊值） | …… | …… | …… |
   | `1000`（v3 默认） | …… | …… | …… |

3. 需要观察的现象：三种取值的差异只体现在「空页后多久通知 OS」这一件事上。
4. 预期结果：`-1`=完全不 purge，内存占用最高、无 purge 系统调用开销；`0`=页一空立即 purge，最省 RSS 但系统调用变多、性能下降；`1000`=折中，1 秒的缓冲让「马上又要用回这块内存」的情形免于反复 decommit/commit。第 u6-l2 讲我们会在本地实测这三种配置。
5. 本表内容依据 readme 原文即可完整作答，无需运行程序；实测数据**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：secure 模式里「free list 指针用每页独立的 key 加密」防的是什么攻击？

**参考答案**：防两类：(1) 攻击者用已知的合法指针覆写 free list 节点（经典的 unsafe unlink / freelist poisoning 变种）——加密后不知道页级 key 就无法构造出有效的下一节点指针；(2) 顺带可用于检测堆损坏——解出的指针不合法即说明链表被破坏（见 readme L414-L418）。

**练习 2**：`bounded` 这一条列出了一连串「有界」：不受 blowup 影响、最坏分配时间有界、元数据 ~0.2%、无内部竞争点。请解释「blowup」在分配器语境下指什么。

**参考答案**：blowup 指空间爆炸——某些分配器在特定（甚至很常见的）分配/释放序列下，实际占用内存远超程序存活对象所需的量（可放大数倍甚至无限），Berger 等人在 Hoard 论文（readme 参考文献 \[1\]）中正式研究过该问题。mimalloc 声称不出现这种放大，元数据开销约 0.2%、内部碎片低，因此总内存占用与存活对象规模保持有界比例。

**练习 3**：「一等堆（first-class heap）」的「一等」体现在哪两件事上？v3 又加了什么？

**参考答案**：(1) 堆是可显式创建/销毁的对象，销毁时**整堆一次性释放**，无需逐对象 free（对阶段性任务、请求级隔离极有用）；(2) 可以同时存在多个堆、在不同内存区域分配。v3 补齐了「真正的」一等堆：**任意线程**都能在同一个堆上分配，而不再局限于创建堆的线程（见 readme L61-L63）。

---

### 4.4 版本谱系：v1 / v2 / v3 的差异与选择

#### 4.4.1 概念说明

mimalloc 同时维护**三个版本**，readme 明确说它们「大体相同，差别主要在 OS 内存的管理方式上」。三者定位：

| 版本 | 定位 | 关键词 | 发布标签 / 开发分支 |
| --- | --- | --- | --- |
| v3 | recommended（推荐） | 简化无锁设计、线程间更好共享内存、更省内存、真一等堆、更高效 heap-walking | `v3.x` / `dev3` |
| v2 | stable（稳定） | 线程本地 segment（thread-local segments）降低碎片 | `v2.x` / `dev2` 和 `main` |
| v1 | legacy（遗留） | 最初设计；文档甚至建议 PR 尽量提到这个版本 | `v1.x` / `dev` |

为什么要有三个？因为分配器一旦被大规模部署（Azure/Bing 级别），升级是高风险动作，必须给用户一个「永远稳」的版本（v2）、一个「激进优化」的版本（v3），以及一个「兼容优先」的老版本（v1）。**本手册全程基于 v3**。

#### 4.4.2 核心流程

三个分支采用**统一发版节奏**：每次发布同时打出三个 tag（例如 2026-08-18 的 `v1.15.0` / `v2.5.0` / `v3.5.0`），v1、v2 的版本号独立递增，发布说明以 v3 的版本号为准。release notes 按时间倒序排列，每条注明「哪些改动属于 v3 专属」，格式形如 `(v3): ...`。所以从 release notes 里筛出所有 `(v3)` 前缀的条目，就能拼出一份「v3 相对 v2 的改进清单」。

#### 4.4.3 源码精读

- [readme.md:L77-L89](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L77-L89)
  Versions 一节：三个版本「大体相同，除 OS 内存的处理方式」；新开发集中在 v3，v1/v2 只收安全与 bug 修复。v3 简化了此前的无锁设计、改进线程间内存共享（某些大负载下内存占用显著更低）、支持真正的一等堆（任意线程可分配）、heap-walking 更高效（点名 CPython GC 场景）。v2 使用线程本地 segment 降低碎片。v1 是最初设计。

- [readme.md:L91-L100](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L91-L100)
  最新一条发布记录（2026-08-18，三版本齐发）：v3 专属改进包括——`free` 使用对齐 chunk 提升性能、清理 cmake 选项、`MI_OPT_ARCH` 要求 armv8.3 以获得更快 load-acquire、retired page 数量从 1 提到 3、secure 模式下更快的 double-free 检测、arena 位图改用更快原子操作（#1346）、更快的 pagemap 查找；另有跨版本修复（NUMA 稀疏节点检测 #1365 等）。

- [readme.md:L133-L141](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L133-L141)
  2026-01-08 的 `v3.2.6`（rc1）记录：「真正的 first-class heap（任意线程可在堆上分配）并支持按 heap 统计」正是在这里落地的；还提到 v3 在 Windows 上用更快的 TLS 访问、`mi_calloc` 与对齐分配性能改进。

- [readme.md:L110-L112](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L110-L112)
  2026-07-14 的记录指出：**v3 中所有元数据都与堆对象分离存放**——这是 v3 架构上很重要的一步，第 u3 单元会看到对应的 `mi_page_t` 独立存放。

- [doc/release-notes.md:L1-L16](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/doc/release-notes.md#L1-L16)
  发布说明全文仅 16 行：v3 = 最新设计、倾向比 v2 **更省内存**但性能相近；v2 = 稳定、使用最广；v1 = 遗留。发布版本号跟随 v3（v1/v2 独立递增）；建议直接下载源码（或用 vcpkg）随项目一起构建；二进制发布包含 release/debug/secure 三种构建。

- 历史注脚 [readme.md:L944-L955](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L944-L955)
  老版本（v1.8.4/v1.8.7）记录了两个对理解 v3 有用的演进：释放逻辑被重构成独立的 `free.c` 模块；废弃段（abandoned segments）的管理从链表改为 **arena 内的位图**，更并发也更激进。这两点分别是第 u5、u6 单元的伏笔。

#### 4.4.4 代码实践

**实践：从 release notes 里「淘」出 v3 专属改进（阅读型，约 15 分钟）**

1. 实践目标：掌握用 release notes 追溯某个分支演进的技能——这是读任何多分支项目的通用功。
2. 操作步骤：
   - 通读 [readme.md:L91-L143](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L91-L143)（近年发布记录）；
   - 每遇到 `(v3):` 或 `(v3)` 前缀的条目就抄到一张清单上；
   - 给每条贴标签：`架构` / `性能` / `内存` / `安全`；
   - 再对照 [doc/release-notes.md:L1-L16](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/doc/release-notes.md#L1-L16) 确认三版本的一句话定位。
3. 需要观察的现象：几乎所有架构级改动都带 `(v3)` 标记，v1/v2 条目以修复为主。
4. 预期结果：你的清单应至少包含：真一等堆（任意线程分配）、按 heap 统计、元数据与堆对象分离、arena 位图更快原子操作、更快的 pagemap 查找、Windows 更快 TLS 访问、`mi_calloc`/对齐分配提速、retired page 1→3、free 用对齐 chunk 提速。这与 readme Versions 一节对 v3 的四点概括（简化无锁设计 / 更好共享内存更省内存 / 真一等堆 / 更高效 heap-walking）应当能一一对上。
5. 全程只需阅读，无需编译运行。

#### 4.4.5 小练习与答案

**练习 1**：三个版本「大体相同，除了一点」，这点是什么？为什么偏偏是这一点造成了三个分支？

**参考答案**：差别在 **OS 内存的管理方式**（readme L79）。分配器的上层逻辑（size class、free list、页组织）相对稳定，而向 OS 要内存、commit、purge、线程退出后内存归属这些策略最影响实际内存占用与并发行为，也最难保证不回归；在这层上做激进重构风险高，因此以分支方式并行演进，v2 保守（thread-local segments）、v3 激进（简化无锁设计 + 更好的线程间共享）。

**练习 2**：团队要给一个长期跑在 Linux 上的低延迟服务选版本，内存占用是首要关切。依据 readme/release-notes 你选哪个？理由？

**参考答案**：选 v3。依据：v3 定位 recommended，且明确「在某些大负载下内存占用可能（明显）更低」（readme L82-L85），`doc/release-notes.md` L3 也说 v3 是「倾向比 v2 更省内存、性能相近」的最新设计。若团队极度保守、要求最长生产验证历史，则退回 v2（stable、使用最广）。

**练习 3**：2026-08-18 这一条发布记录里，哪一项改进直接服务于第 4.2 节讲的「跨线程释放」路径？

**参考答案**：`free` 使用对齐 chunk（aligned chunks）带来的 `free` 性能提升（readme L93-L94）——释放路径正是跨线程 free 所走的代码；同条的「arena 位图使用更快原子操作」（L96-L97）也间接受益于所有依赖原子操作的路径。更精确的代码级论证要等第 u5 单元读完 `free.c` 后才能下结论。

---

## 5. 综合实践

**任务：写一份 200 字的「为什么换掉 ptmalloc」总结 + v3 相对 v2 的三个改进点**

这是本讲的主实践，也是整本手册的「开篇笔记」——以后每学一讲，回头修订它。

### 5.1 实践目标

用最短的篇幅向一个没读过 mimalloc 的工程师说清：(a) glibc ptmalloc 有什么痛点，mimalloc 用什么换掉了它；(b) v3 比 v2 好在哪三点。

### 5.2 操作步骤

1. 通读 [readme.md:L28-L75](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L28-L75)（完整设计清单）与 [readme.md:L91-L143](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L91-L143)（Releases 小节）。
2. 写一段 **约 200 字** 的中文总结，主题：「mimalloc 相比 glibc ptmalloc 解决了哪些问题」。要求至少覆盖：单一大 free list 带来的竞争与局部性问题、空页内存不还给 OS 的问题、以及一个你自己挑的第三点（如元数据/碎片、malloc 覆盖能力或运行时钩子）。
3. 另列 **v3 相对 v2 的三个主要改进点**，每点一句话，并标注出处（readme 行号或 release notes 日期）。
4. 把这两部分存进你自己的学习笔记（建议 `mimalloc-tutorial/` 之外的个人笔记目录，不要提交到讲义目录）。

### 5.3 自检清单（预期结果）

写完后逐条核对，你的总结应当能回答：

- [ ] 是否说清了「每个 size class 一条大 free list」的问题，以及「按页分片 + 页内多分片」如何解决？
- [ ] 是否提到「一次 CAS 的跨线程释放」？
- [ ] 是否解释了 eager page purging 对长期服务内存占用的意义？
- [ ] 是否说明了 mimalloc 可作为 `malloc` 的 drop-in replacement（`LD_PRELOAD`）？
- [ ] 三个 v3 改进点是否都能在源材料里找到出处？

### 5.4 参考答案要点（供核对，不要照抄）

**200 字总结示例**：

> glibc ptmalloc 把同一尺寸的空闲块放在一条较大的 free list 里：多线程释放时链表头成为竞争点，跨线程归还内存需要复杂协调；空闲页的物理内存也常常迟迟不还给 OS。mimalloc 把 free list 切到每个 64KiB 页一条，页内再分本地/跨线程两条链，使跨线程释放退化为一次原子 CAS，竞争被数千条链表天然分散；同时分配局部性更好、页更容易整体变空，配合 eager purging 及时 decommit，长期服务的内存压力显著降低。它还是 malloc 的 drop-in 替代，可 `LD_PRELOAD` 直接上线。

（约 200 字，可再按自己的理解改写。）

**v3 相对 v2 的三个改进点**（任选三条，均有出处）：

1. **真正的 first-class heap**：任意线程都能在同一个堆上分配，并支持按 heap 统计（readme L61-L63、L135-L136）。
2. **更省内存的内存管理**：简化了此前的无锁设计、改进线程间内存共享，某些大负载下内存占用明显更低（readme L82-L85；doc/release-notes.md L3）。
3. **更高效的 heap-walking**：遍历堆更高效，明确服务 CPython GC 场景（readme L85-L86）。
4. （备选）**元数据与堆对象分离**，安全性与并发性更好（readme L112）。

### 5.5 待本地验证清单

本讲全部为阅读型实践，以下问题留到后续讲义用运行结果回答：

- `LD_PRELOAD` 换上 mimalloc 后，你的程序到底快多少？（u2-l1）
- `MIMALLOC_PURGE_DELAY` 三种取值的实测 RSS/耗时曲线？（u6-l2）
- 跨线程 free 真的只有一次 CAS 吗？（u5-l2 读 `free.c` 验证）

---

## 6. 本讲小结

- mimalloc 是一个约 1 万行 C 的**通用分配器**，出身于 Koka/Lean 运行时，现服务于 Bing、Azure、Unreal Engine 等大规模系统；它是 `malloc` 的 drop-in 替代，Linux 上 `LD_PRELOAD` 即可零改动接入。
- 两大核心创新：**free list sharding**（每页一条链表 → 局部性↑碎片↓）与 **free list multi-sharding**（每页再分本地/跨线程链表 → 跨线程释放一次 CAS、竞争被数千条链表分散）。
- **eager page purging** 依赖分片带来的「页更容易整体变空」，及时向 OS decommit，显著改善长期服务的真实内存占用；purge ≠ free，虚拟地址保留不变。
- 其余支柱：**secure**（guard page + 每页 key 加密 free list，约 10% 代价）、**first-class heaps**（整堆销毁；v3 支持任意线程分配）、**bounded**（无 blowup、~0.2% 元数据、无内部竞争点）、**fast**。
- 项目同时维护 **v1（legacy）/ v2（stable，thread-local segments）/ v3（recommended，简化无锁设计 + 更省内存 + 真一等堆 + 高效 heap-walking）** 三分支，统一发版、版本号独立递增；**本手册基于 v3（HEAD `cd69707c`）**。
- `readme.md` 是全项目信息密度最高的文件：构建（L173 起）、使用（L258 起）、环境变量（L348 起）、安全/调试/guarded（L409 起）、malloc 覆盖（L465 起）、工具链（L571 起）、基准（L665 起）——遇到问题先查它。

---

## 7. 下一步学习建议

下一讲 **u1-l2《构建与运行：cmake、debug/secure 模式与单文件编译》** 将把本讲的认识落地为可运行的产物：

- 用 `mkdir -p out/release && cd out/release && cmake ../.. && make` 完成 release 构建；
- 理解 `-DCMAKE_BUILD_TYPE=Debug`、`-DMI_SECURE=ON`、`-DMI_GUARDED=ON` 分别对应本讲 4.3 节的哪种检查；
- 弄清为什么一次构建会同时产出 `.so`、`.a`、`.o` 三种产物，以及 `src/static.c` 如何把整个库合并成单个目标文件。

提前预习建议：浏览 [CMakeLists.txt](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt) 顶部的 option 列表，数一数你能认出几个本讲提到过的开关（`MI_SECURE`、`MI_GUARDED`、`MI_OVERRIDE`……）。

学完构建之后，如果想尽快看到「分配器在工作」，可以直接跳到 u1-l4 用 `mi_` API 写第一个程序，再回过头补 u1-l3 的目录地图。
