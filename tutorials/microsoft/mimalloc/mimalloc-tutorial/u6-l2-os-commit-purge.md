# os.c：commit/decommit/purge 与大页、NUMA

## 1. 本讲目标

上一讲（u6-l1）我们打开了「向 OS 要内存」的黑盒，认识了 prim 抽象层的接口合同。本讲往上一层，精读建立在该抽象之上的**统一内存操作层** `src/os.c`。读完本讲你应该能够：

1. 准确区分两对容易混淆的操作：**reserve/commit/decommit**（可访问性维度）与 **purge 之下的 decommit/reset 两种模式**（物理内存归还维度，对应 `MADV_DONTNEED` 与 `MADV_FREE`）。
2. 读懂 `_mi_os_purge_ex` 的三路分支，并解释 `MIMALLOC_PURGE_DECOMMITS` 开关如何改变 purge 的行为与 RSS 曲线。
3. 理解 `purge_delay`、`arena_purge_mult`、`minimal_purge_size` 三个选项如何共同调节一个长期运行服务的内存占用（eager page purging 这一 mimalloc 卖点的实现细节）。
4. 说出大页（2MiB）/巨页（1GiB）与 NUMA 节点绑定的申请路径、适用场景与代价。

## 2. 前置知识

- **虚拟内存三阶段**：现代 OS 把「地址空间」和「物理内存」分开管理。一段虚拟地址可以先 **reserve**（占住地址范围，不可访问，不占物理内存），再 **commit**（承诺 backing store，可访问），最后在不需要物理页时归还。上一讲已经见过 prim 层的 `_mi_prim_alloc` / `_mi_prim_commit` / `_mi_prim_decommit` 原语，本讲看 os.c 如何包装它们。
- **RSS（Resident Set Size）**：进程实际驻留在物理内存中的字节数，`top`/`/proc/self/status` 中的 `VmRSS`。分配器调优的一个核心目标就是：释放后的内存尽快（但别太频繁地）从 RSS 中消失。
- **`madvise` 的两个关键 advice**（Linux）：
  - `MADV_DONTNEED`：立刻丢弃物理页，RSS 马上下降；下次访问触发缺页、得到**零页**。
  - `MADV_FREE`：懒丢弃——内核「标记可回收」，RSS 数字暂不下降，内存压力到来时才真正回收；下次访问若页还在则保留原值。更快，但观测不直观。
- **free list 与 arena 的关系**（承接 u3/u4/u6-l1）：小对象来自 arena（默认 1GiB 内存区、按 64KiB slice 切分）中的 mimalloc 页；页全部空闲并退役后，其 slice 回到 arena 的空闲位图——**但物理内存并没有归还 OS**，归还动作就是本讲的 purge。
- **选项系统**（承接 u2-l3）：`MIMALLOC_` 前缀环境变量与 `mi_option_set` 等价；KiB 型选项支持 `K/M/G/T` 后缀。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [src/os.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c) | 本讲主角。prim 原语之上的统一包装层：内存能力表、alloc/free、commit/decommit/reset/reuse、purge、巨页、NUMA。os.c 及以上**不含任何平台 `#ifdef`**，平台差异全部被 prim 吸收 |
| [src/options.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c) | 选项描述表。本讲涉及 `purge_delay`、`purge_decommits`、`arena_purge_mult`、`minimal_purge_size`、`allow_thp`、`allow_large_os_pages`、`reserve_huge_os_pages(_at)`、`use_numa_nodes` 等约 10 个选项的默认值 |
| [src/prim/unix/prim.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c) | 参照物：`_mi_prim_commit/decommit/reset` 的 mmap/mprotect/madvise 实现，用于说明同一个 os.c 语义在不同平台/构建下落到什么系统调用 |
| [src/arena.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c) | purge 的调用方。arena 持有 `slices_purge` 位图与 `purge_expire` 时间戳，决定「何时、按多大粒度」调用 `_mi_os_purge_ex`（完整机制在 u6-l3 展开） |
| [src/stats.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c) | 统计输出。`reset/purged/resets/purges/mmaps/commits` 计数器都来自本讲这些函数的埋点 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**4.1 能力表与 os.c 的分层位置**、**4.2 四个动词：commit / decommit / reset / reuse**、**4.3 purge：把物理内存还给 OS**、**4.4 巨页与 NUMA**。

### 4.1 模块一：os.c 的分层位置与运行时能力表

#### 4.1.1 概念说明

mimalloc 向 OS 要内存的调用栈是三层的：

```
arena.c（切片管理、位图分配）
   └── os.c（统一语义层：对齐、取整、统计、选项判断）   ← 本讲
          └── prim 层（_mi_prim_alloc/commit/decommit/...，按平台实现）
                 └── mmap / mprotect / madvise / VirtualAlloc ...
```

os.c 存在的意义：prim 层只保证「能做成」，os.c 负责「做得对」——包括把任意区间按 OS 页对齐（且区分保守/宽松两种对齐）、把尺寸按阶梯取整以限制内部浪费、维护 `committed/reserved/mmap_calls` 等统计、以及**读取选项系统决定策略分支**（比如 purge 允不允许、走 decommit 还是 reset）。

另一个关键设计是**能力表**：进程启动时探测一次平台能力，存进一个静态结构体，之后 os.c 及上层的所有策略判断都查这张表，而不是每处都写 `#ifdef`。

#### 4.1.2 核心流程

1. `init.c` 在进程初始化时调用 `_mi_os_init`（本讲不展开 init 细节，见 u7-l1）。
2. `_mi_os_init` 调用 `_mi_prim_mem_init(&mi_os_mem_config)`，由 prim 实现填写能力表。
3. 之后所有层通过 `_mi_os_page_size()`、`_mi_os_has_overcommit()` 等只读访问器查询能力。

#### 4.1.3 源码精读

能力表的默认值与字段含义（unix 平台的 prim 会在启动时覆盖其中多数）：

[src/os.c:24-34](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c#L24-L34) 定义 `mi_os_mem_config`：OS 页大小（默认 4096）、大页大小（通常 2MiB，0 表示不支持）、分配粒度、默认物理内存估计（64 位 32GiB）、虚拟地址位数，以及四个能力布尔值——`has_overcommit`（是否用 `MAP_NORESERVE`）、`has_partial_free`（mmap 系统可以释放映射区间中的一部分，Windows 则必须整段释放）、`has_transparent_huge_pages`（是否启用了透明大页 THP，影响 purge 粒度）、`has_virtual_reserve`（能否「只占地址不 commit」）。

[src/prim/unix/prim.c:250-263](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c#L250-L263) 是 unix 侧的填写过程：`sysconf(_SC_PAGESIZE)` 取页大小、读 `/sys` 探测物理内存与 THP、`unix_detect_overcommit()` 探测 overcommit。这正是 u6-l1 所说「能力在运行时探测、驱动策略」的具体落点。

[src/os.c:88-97](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c#L88-L97) 的 `_mi_os_good_alloc_size` 是贯穿全库的尺寸取整函数（u4-l3 已见过它决定单例页块长）：小于 512KiB 按 OS 页对齐，之后随尺寸增大逐级换成 64KiB/256KiB/1MiB/4MiB 的对齐台阶，注释标明设计目标是**浪费不超过 12.5%**。

[src/os.c:56-67](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c#L56-L67) 的 `_mi_os_minimal_purge_size` 决定 purge 的最小粒度，优先级是：选项 `minimal_purge_size` 显式给定（按页大小向上对齐）→ 若探测到 THP 且 `allow_thp==2` 则用大页大小（2MiB）→ 否则用 OS 页大小（4KiB）。它的用武之地在 4.3 节的 arena 调度里。注意代码是权威：选项注释（options.c:174）写着 0 时「解析为 64 或 2048（KiB）」，但实际代码路径给出的是 4KiB 或 2MiB——又一次印证 u3-l2 的教训：**注释可能滞后，以代码为准**。

#### 4.1.4 代码实践

1. **实践目标**：在统计报表中找到本讲相关计数器的位置，为后续实验建立观测手段。
2. **操作步骤**：构建 release 版后运行仓库自带任意测试（如 `mimalloc-test-api`），加环境变量 `MIMALLOC_SHOW_STATS=1 MIMALLOC_SHOW_STATS=1`（release 构建需显式打开 show_stats 才输出统计段）。
3. **需要观察的现象**：输出末尾的 `arenas` 段中出现 `reset`、`purged`、`resets`、`purges`、`mmaps`、`commits` 等行；`process` 段有一行 `numa nodes : N`。
4. **预期结果**：这些行的埋点全部来自本讲讲解的函数（4.2/4.3 会一一对应）。`numa nodes` 就是 `_mi_os_numa_node_count()` 的返回值。具体数值待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`mi_os_mem_config.has_partial_free` 为 false 时，`mi_os_prim_alloc_aligned`（os.c:346-431）的对齐策略有什么不同？

**答案**：mmap 系统可以「过量分配后把前后多余部分 unmap 掉」（os.c:406-424 分支）；Windows 不能部分释放，于是改为「过量 **reserve** 一块不 commit 的内存，对齐后只 commit 中间那段」（os.c:386-405 分支），释放时靠 `mi_memid_t` 里记录的 `base` 字段找回真实起点整段还给 OS。这解释了为什么 `_mi_os_free_ex` 要比对 `memid.mem.os.base != addr`（os.c:266-276）。

**练习 2**：为什么 `_mi_os_minimal_purge_size` 在启用 THP 时要返回 2MiB 而不是 4KiB？

**答案**：透明大页把连续的 4KiB 小页在内核侧合并成 2MiB 大页。若按 4KiB 粒度 purge，会把一个 THP 大页「打碎」成零散状态，破坏后续 THP 合并（即注释所说的 fragment transparent huge pages）。按 2MiB 粒度整块归还就不会破坏。

### 4.2 模块二：四个动词——commit / decommit / reset / reuse

#### 4.2.1 概念说明

prim 层给了四个「改变一段已 reserve 内存状态」的原语，os.c 逐一包装。精确区分它们是本讲的地基：

| 操作 | 语义 | 对可访问性 | 对物理内存/RSS | unix 落点 |
| --- | --- | --- | --- | --- |
| commit | 让区间可读写 | 变为可访问 | 按需分配物理页 | `mprotect(PROT_READ\|PROT_WRITE)` |
| decommit | 撤销 commit | 理论上变不可访问 | **立刻**丢弃物理页 | `madvise(MADV_DONTNEED)` |
| reset | 「我不再用了，但 commit 还算数」 | 保持可访问 | **懒**丢弃物理页 | `madvise(MADV_FREE)`，失败退 `MADV_DONTNEED` |
| reuse | macOS 专属：把 reset 过的区间重新纳入 RSS 记账 | 不变 | 恢复记账 | `madvise(MADV_FREE_REUSE)` |

一句话记忆：**decommit 是「退租」，reset 是「告诉房东房间空了但合同保留」**。reset 过的内存再次访问不会缺页崩溃，只是内容可能已被清掉；decommit 过的内存是否需要重新 commit 才能用，则取决于平台——这正是 u6-l1 介绍的 `needs_recommit` 出参存在的意义。

#### 4.2.2 核心流程

- `_mi_os_commit(addr, size)`：宽松对齐（区间可向两侧扩张到整页）→ `_mi_prim_commit` → 若 OS 报告「本来就是零页」则透传 `is_zero`（供上层省掉 memset，呼应 u4-l1 的 free_is_zero 捷径）→ 统计 `committed` 增量。
- `_mi_os_decommit`：**保守**对齐（只处理完全落在区间内的整页）→ `_mi_prim_decommit`，由 prim 决定 `needs_recommit`。
- `_mi_os_reset`：保守对齐 → 统计 `reset`/`reset_calls` → `_mi_prim_reset`。
- 对齐方向的差异不是随手写的：commit 要「保证请求的每个字节都可用」，所以向外扩张；decommit/reset 要「绝不误伤邻页」，所以向内收缩。

#### 4.2.3 源码精读

[src/os.c:558-586](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c#L558-L586) `_mi_os_commit_ex`：注意两个细节。其一，宽松对齐由 `mi_os_page_align_areax(false, ...)`（os.c:536-552）完成——`conservative` 参数为 false 时向下/向上取整到页边界。其二，末尾的 `mi_subproc_stat_increase(subproc, committed, size - stat_already_committed)`：调用方（arena）可告知「其中多少本来就 committed」，只把差额计入统计，保证 commit/decommit 计数精确对称。

[src/os.c:592-611](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c#L592-L611) `mi_os_decommit_ex`：先写 `*needs_recommit = true` 再把指针交给 `_mi_prim_decommit`，**由平台实现决定是否改写**。这是 u6-l1「同一接口承载相反平台语义」的最典型例子——见下一段 unix 实现的惊人之处。

[src/prim/unix/prim.c:544-569](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c#L544-L569) unix 的 decommit：`MADV_DONTNEED` 丢页之后，**release 构建（`!MI_DEBUG && MI_SECURE<=2`）直接把 `needs_recommit` 置为 false，不做 `mprotect(PROT_NONE)`**。原因：Linux 上 `MADV_DONTNEED` 之后映射的读写权限还在，再次访问自动填零页，根本不需要重新 mprotect；只有 debug/高安全构建才额外上 `PROT_NONE` 保护并要求 recommit。也就是说「committed」这个概念在 Linux release 下与 RSS 完全解耦——这直接影响了 4.3 中 arena commit 位图的更新逻辑。

[src/prim/unix/prim.c:571-598](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c#L571-L598) unix 的 reset：优先 `MADV_FREE`（最快、懒回收），内核不支持时（返回 `EINVAL`）通过一个静态原子变量把建议**永久降级**为 `MADV_DONTNEED`，避免每次重试。注释明确点出取舍：`MADV_FREE` 的缺点是 `top` 里的 RSS 不下降，尽管内存实际已可被其他进程使用；而默认 `MIMALLOC_PURGE_DECOMMITS=1` 保证了默认路径走 decommit（即 `MADV_DONTNEED`），RSS 可见地下降。

[src/os.c:623-640](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c#L623-L640) `_mi_os_reset`：埋点 `reset`（字节数）与 `reset_calls`（次数）两个统计；`MI_DEBUG>1` 且非 secure/track 构建时还会 `memset(start, 0, csize)`「假装已经 eagerly reset」——目的是让 debug 构建下使用未初始化内存的 bug 更容易暴露（读到的一定是 0 或已写值，而不是 reset 前的残骸）。

#### 4.2.4 代码实践

1. **实践目标**：从统计输出验证 commit/reset/decommit 各自的埋点位置，并理解「mmaps vs commits」计数的差异。
2. **操作步骤**：用 debug 构建运行一个程序（debug 默认 `MI_STAT>=2` 且输出统计），观察 `arenas` 段：
   - `mmaps`：每次 `_mi_prim_alloc` 加一（埋点在 os.c:324）；
   - `commits`：每次 `_mi_os_commit_ex` 加一（埋点在 os.c:561）；
   - `resets`：每次 `_mi_os_reset` 加一（os.c:629）。
3. **需要观察的现象**：`commits` 通常远大于 `mmaps`——因为一次 arena reserve 之后，页是按需一片一片 commit 的（呼应 u4-l2 的「分批 commit」）。
4. **预期结果**：三个计数与源码埋点一一对应；具体数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_mi_os_commit` 用宽松对齐而 `_mi_os_decommit` 用保守对齐？如果反过来会怎样？

**答案**：commit 少覆盖边界字节会导致用户数据写入未 commit 的页而 SIGSEGV，必须向外扩张保证全覆盖；decommit 多覆盖邻页会把邻居的物理页误丢，数据损坏。错误的方向分别对应「崩溃」和「静默数据丢失」，后者更危险。

**练习 2**：在 Linux release 构建下，调用 `_mi_os_decommit` 后再直接读写这段内存会发生什么？debug 构建呢？

**答案**：release：`MADV_DONTNEED` 只丢物理页、不动权限，读写触发缺页并得到零页，不崩溃（`needs_recommit=false` 与之配套）。debug/`MI_SECURE>2`：额外 `mprotect(PROT_NONE)`，直接读写会 SIGSEGV，必须先 `_mi_os_commit`。这正是 prim 出参 `needs_recommit` 存在的原因（prim.c:555-560 的条件编译）。

### 4.3 模块三：purge——把物理内存还给 OS

#### 4.3.1 概念说明

mimalloc 的五大卖点之一 **eager page purging**（u1-l1）落在代码上就是这个模块。问题背景：arena 里的 slice 释放回空闲位图后，物理页仍然驻留（RSS 不降）；对长期运行的服务，这表现为「内存只涨不跌」。purge 就是「把确定暂时不用的空闲区间上的物理内存归还 OS」的统称，它**不是第四个系统调用**，而是 decommit 与 reset 的策略性包装：

- `MIMALLOC_PURGE_DECOMMITS=1`（默认）：purge = decommit（Linux 即 `MADV_DONTNEED`），RSS 立刻下降，代价是将来重用时可能要重新 commit/缺页。
- `MIMALLOC_PURGE_DECOMMITS=0`：purge = reset（Linux 优先 `MADV_FREE`），物理页懒回收，重用零成本，但 RSS 数字不直观。

选项表里的注释同样值得读一眼：[src/options.c:127](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L127) `purge_decommits` 默认 1，legacy 名 `reset_decommits`（旧环境变量 `MIMALLOC_RESET_DECOMMITS` 仍可用但会打弃用警告，options.c:630-637）。

#### 4.3.2 核心流程

`_mi_os_purge_ex`（os.c:657-680）的完整决策树：

```
_mi_os_purge_ex(p, size, allow_reset, ...)
│
├─ mi_option_get(mi_option_purge_delay) < 0 ?
│    └─ 是 → return false          // 全局禁用 purge，一个字节都不动
│
├─ 调用方自带 commit_fun（arena 的自定义内存）？
│    └─ 是 → (*commit_fun)(false /*decommit*/, ...)   // 交给内存提供者处理
│
├─ mi_option_purge_decommits 开启 且 未处于 preloading？
│    └─ 是 → mi_os_decommit_ex(...)  → return needs_recommit
│    │         （Linux release: MADV_DONTNEED, needs_recommit=false）
│
└─ 否则 → allow_reset 时 _mi_os_reset(p, size) → return false
```

返回值语义：**true 表示这段内存将来重用前必须重新 commit**。arena 拿到 true 就清掉 `slices_committed` 位图（见下）。

arena 侧的调度（何时调用 purge）由三个选项共同决定：

- `purge_delay`（默认 1000ms，options.c:140）：单次释放的延迟基准。
- `arena_purge_mult`（默认 4，options.c:149）：arena 级放大系数，实际生效延迟 = `purge_delay × arena_purge_mult`（默认 4 秒）。
- `minimal_purge_size`（默认 0，options.c:174）：每次 purge 的最小粒度。

延迟的意义是**合批**：释放发生时只在 `slices_purge` 位图上打个标记并设置到期时间戳；到期后由后台节律（分配慢路径触发的 collect）统一扫描位图批量 purge。这样「释放→purge→再分配」的抖动被限制在每延迟周期至多一轮，madvise 系统调用次数被摊薄。

#### 4.3.3 源码精读

[src/os.c:657-680](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c#L657-L680) `_mi_os_purge_ex` 全文：第一行 `if (mi_option_get(mi_option_purge_delay) < 0) return false;` 就是 `MIMALLOC_PURGE_DELAY=-1` 的短路点——注意它查的是原始选项值而非 arena 放大后的值；`purge_calls`/`purged` 统计在任何实际动作之前累加；`allow_reset` 形参由 arena 传入（仅当待 purge 区间全部已 commit 时才允许 reset，因为 Windows 不允许 reset 未 commit 的内存，见 4.2 表格）。

[src/arena.c:2242-2252](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L2242-L2252) `mi_arena_purge_delay`：任一因子为负则整体禁用（-1），任一为 0 则立即 purge（0），否则相乘。这就是 u2-l3 说过「purge_delay 在 arena 级被 ×4 放大」的实现处。

[src/arena.c:2257-2283](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L2257-L2283) `mi_arena_purge`：先把 `slices_committed` 位图整段置位（借 `setN` 的出参拿到「其中多少本来就已 commit」），再调用 `_mi_os_purge_ex` 并传入 `allow_reset = all_committed`；若返回 `needs_recommit` 则把 committed 位图整段清掉。结合 4.2.3 的结论——Linux release 下 decommit 返回 false——位图**保持置位**，这是正确的：Linux 的「committed」只需权限而不需页表项。

[src/arena.c:2288-2312](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L2288-L2312) `mi_arena_schedule_purge`：延迟为 0 就地 purge；否则用 CAS 把 `now + delay` 写入 `arena->purge_expire`（只在首次设置时成功），并在 `slices_purge` 位图标记区间。到期检查在 [src/arena.c:2362-2386](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L2362-L2386) `mi_arena_try_purge`：时间未到直接返回 0；到期则用 `_mi_bitmap_forall_setc_rangesn(arena->slices_purge, minslices, ...)` 按**至少 `minslices` 个 slice**（来自 `_mi_os_minimal_purge_size()`，即 arena.c:2382）的粒度遍历所有标记区间逐段 purge——这就是 4.1 中最小粒度选项的生效点，防止打碎 THP。

[src/arena.c:2389-2435](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L2389-L2435) `mi_arenas_try_purge`：全局节律。`mi_atomic_guard` 保证同一时刻只有一个线程在做 purge 扫描；从 `tseq % max_arena` 起轮转扫描，普通一轮至多处理 `max_arena/4+1` 个 arena（限制单次停顿）；全部扫完无果则清零全局到期时间。触发源头是 [src/theap.c:123-148](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L123-L148) 的 `mi_theap_collect_ex`——分配慢路径的周期性 collect（u5-l3 的心跳节拍）最终走到这里，`_mi_arenas_collect(collect == MI_FORCE, ...)` 把「是否强制」传下去：`mi_collect(true)` 强制立即 purge，普通周期只处理已到期者。

统计输出侧：[src/stats.c:404-422](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L404-L422) 把 `reset/purged`（字节数）与 `resets/purges`（次数）打印在 `arenas` 段，是验证 purge 行为的第一观测点。

#### 4.3.4 代码实践

1. **实践目标**：用统计计数器证明 `MIMALLOC_PURGE_DECOMMITS` 开关切换了 purge 的底层动作。
2. **操作步骤**：写一个「分配一批 → 全部释放 → `mi_collect(true)`」的小程序（示例代码）：

   ```c
   // 示例代码：purge_mode.c
   #include <mimalloc.h>
   #include <stdio.h>
   int main(void) {
     for (int r = 0; r < 3; r++) {
       void* p = mi_malloc(64*1024*100);  // 约 6MiB，跨多个 arena slice
       mi_free(p);
       mi_collect(true);                  // 强制 collect → 触发到期 purge
     }
     return 0;
   }
   ```

   分别以 `MIMALLOC_SHOW_STATS=1`、`MIMALLOC_SHOW_STATS=1 MIMALLOC_PURGE_DECOMMITS=0`、`MIMALLOC_SHOW_STATS=1 MIMALLOC_PURGE_DECOMMITS=1` 运行。
3. **需要观察的现象**：对比 `arenas` 段中 `reset`/`resets` 与 `purged`/`purges` 两组计数——注意两组都会计数（purge 统计在分支之前），但 `reset` 字节数只在 reset 模式下非零。
4. **预期结果**：`PURGE_DECOMMITS=0` 时 `resets`/`reset` 明显非零；`=1` 时 `reset` 恒为 0。另外可加 `MIMALLOC_PURGE_DELAY=0` 避免 4 秒延迟掩盖现象。具体数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`MIMALLOC_PURGE_DELAY=-1` 时，`_mi_os_purge_ex` 返回 false。这会不会让 arena 误以为内存「不需要 recommit」而漏清 committed 位图，造成后续 bug？

**答案**：不会。禁用 purge 时根本不会走到 purge 分支：arena 侧 `mi_arena_schedule_purge` 在 delay<0 时直接 return（arena.c:2290），位图与统计都不会被触碰。`_mi_os_purge_ex` 的短路只是纵深防御，保证任何路径下禁用即无动作。

**练习 2**：为什么 purge 要「先标记位图 + 设时间戳，到期再批量处理」，而不是释放 slice 时当场归还物理内存？

**答案**：当场归还在「释放→立刻又申请同尺寸」的工作负载下会造成 madvise/缺页风暴（thundering herd）；延迟合批把 N 次归还合并为一轮扫描，且 `arena_purge_mult` 给了跨 arena 的更大缓冲。代价是 RSS 回落有一条可配置的滞后曲线——这正是服务调优的核心旋钮，综合实践将量化它。

**练习 3**：`minimal_purge_size` 设为 128（KiB）意味着什么？

**答案**：`_mi_os_minimal_purge_size` 返回 128KiB（按页对齐后不变），arena 侧换算成 `minslices = 128KiB/64KiB = 2` 个 slice；到期扫描时小于 2 个连续 slice 的空闲区间不会被 purge，零散碎片将滞留在 RSS 中——用粒度换系统调用次数。

### 4.4 模块四：巨页（1GiB）、大页（2MiB）与 NUMA

#### 4.4.1 概念说明

两类「大内存页」不要混淆：

- **大页 large OS pages**：2MiB（`MI_UNIX_LARGE_PAGE_SIZE`），通过 `allow_large_os_pages` 选项启用，用于常规大块分配，减少 TLB miss 与 VMA 数量。
- **巨页 huge OS pages**：固定 1GiB（`MI_HUGE_OS_PAGE_SIZE`，os.c:721），通过 `reserve_huge_os_pages` 在进程启动时**预 reserve** 成专门的 arena。Linux 上依赖管理员事先配置 `nr_hugepages`（`MAP_HUGETLB`），申请即失败若池为空。

适用场景：内存访问密集、页表开销成为瓶颈的常驻服务（数据库、JIT 运行时）。代价：粒度粗（1GiB 起）、不能部分释放（`is_pinned`）、需要特权预配置、与其他特性互斥（MI_GUARDED 构建会强制关掉 `allow_large_os_pages`，见 [src/options.c:195-202](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L195-L202)）。

**NUMA**（Non-Uniform Memory Access）：多路服务器上每个 CPU 插槽有自己的本地内存，跨节点访问慢得多。分配器应尽量「哪个节点上的线程用哪块内存」。mimalloc 在巨页预留时可用 `mbind` 把页绑定到指定节点，并提供了节点探测的缓存加速。

#### 4.4.2 核心流程

巨页预留的完整链路（以 `MIMALLOC_RESERVE_HUGE_OS_PAGES=2` 启动为例）：

1. `init.c` 进程初始化读到选项（init.c:566-572），调用 `mi_reserve_huge_os_pages_at(pages, node, pages*500ms)`（arena.c:2193-2195）。
2. 进入 `_mi_os_alloc_huge_os_pages`（os.c:771）：先用 `mi_os_claim_huge_pages` 在虚拟地址空间 32TiB 之后认领一段对齐范围。
3. **逐页**调用 `_mi_prim_alloc_huge_os_pages`（每次 1GiB）：unix 实现用带 `MAP_HUGETLB` 的 mmap 并按 `numa_node` 调 `mbind(MPOL_PREFERRED)` 绑定节点。
4. 若某页没能落在预期的连续地址，释放它并停止；若耗时预计超时（`max_msecs`），也停止——「能拿几页拿几页」。
5. 成功的部分生成 `MI_MEM_OS_HUGE` 的 `mi_memid_t`（`is_pinned` 为真），交回 arena.c 建成专门 arena（u6-l3 展开）。

NUMA 探测的缓存策略：首次调用 `_mi_os_numa_node_count` 时探测（读 `/sys/devices/system/node/`）并原子缓存；`_mi_os_numa_node` 的快路径在缓存值等于 1 时直接返回 0，**连系统调用都省掉**。

#### 4.4.3 源码精读

[src/os.c:729-761](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c#L729-L761) `mi_os_claim_huge_pages`：巨页区从 32TiB 起——正好接在 `_mi_os_get_aligned_hint` 使用的 2~30TiB 对齐提示区之后（os.c:122-124 的注释），两者共享「低地址留给常规分配」的布局约定。起点在 secure/release 构建下用线程随机数加 0~4TiB 的偏移（12 位随机 × 1GiB）做 ASLR；CAS 循环保证多线程并发认领不重叠。

[src/os.c:771-841](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c#L771-L841) `_mi_os_alloc_huge_os_pages`：注释说明了逐页分配的两个动机——能在超时中途放弃、能拿到系统现存的任意多页。超时估计逻辑（os.c:813-825）用「已耗时/已得页数 × 总页数 > 2×max_msecs」预测后提前止损。成功后 `mi_subproc_stat_increase(committed/reserved)` 与普通分配共用统计口径，`memid->memkind = MI_MEM_OS_HUGE`。

[src/prim/unix/prim.c:630-646](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c#L630-L646) unix 巨页原语：`unix_mmap(..., true, true, ...)` 传入 commit 与 large 标志（内部映射到 `MAP_HUGETLB`，见 u6-l1 对 `_mi_prim_alloc` 参数的讲解）；随后 `mi_prim_mbind` 直接 `syscall(SYS_mbind, ..., MPOL_PREFERRED, &numa_mask, ...)` 把这片内存偏向指定节点，失败仅告警不回滚（代码中的 todo 注释坦承 `mbind` 对巨页的语义存疑，并给出 lkml 讨论链接——真实工程权衡的范例）。

[src/prim/unix/prim.c:677-689](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c#L677-L689) `_mi_prim_numa_node_count`：逐个 `access("/sys/devices/system/node/nodeN")` 枚举，允许节点号稀疏但连续空洞不超过 4。这种「无分配、纯 syscall」的写法是 prim 层的普遍纪律——探测发生在自举期，不能用 malloc。

[src/os.c:860-898](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c#L860-L898) NUMA 缓存：`mi_option_use_numa_nodes` 选项（options.c:141）可强制指定节点数（0 = 自动探测）；`_mi_os_numa_node` 用 `mi_likely(count==1) → 0` 的快路径把单节点机器上的开销降为零。统计输出里的 `numa nodes : N`（stats.c:426）就是它。

[src/options.c:128-132](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L128-L132) 三个相关选项的默认值与注释：`allow_large_os_pages` 默认 0，注释建议配合 eager commit 使用以防 VMA 碎片化；`reserve_huge_os_pages` 默认 0（按 1GiB 计页数）；`reserve_huge_os_pages_at` 默认 -1（不指定节点时 init.c 改走 `mi_reserve_huge_os_pages_interleave` 在各节点间交错预留）。

#### 4.4.4 代码实践

1. **实践目标**：观察巨页预留的失败路径与 NUMA 探测结果。
2. **操作步骤**：运行任意链接了 mimalloc 的程序，加 `MIMALLOC_RESERVE_HUGE_OS_PAGES=1 MIMALLOC_SHOW_ERRORS=1 MIMALLOC_VERBOSE=1`。多数开发机没有预配置 1GiB 巨页池，预期看到 `unable to allocate huge OS page` 警告（os.c:793）。
3. **需要观察的现象**：警告来自 `_mi_os_alloc_huge_os_pages` 的逐页循环；同时统计段 `numa nodes` 显示本机节点数。
4. **预期结果**：无巨页池时预留失败但程序正常运行、后续分配回落到常规 arena——这正是「能拿几页拿几页」设计的容错体现。若机器已配置巨页（`/sys/kernel/mm/hugepages/hugepages-1048576kB/`），则预留成功。结果待本地验证。
5. **延伸**：有兴趣可在测试机 `sudo sysctl` 配置巨页后对比 `MIMALLOC_RESERVE_HUGE_OS_PAGES_AT=0`（绑定节点 0）与不指定的统计差异。

#### 4.4.5 小练习与答案

**练习 1**：为什么巨页内存的 `mi_memid_t` 要标记 `is_pinned`，这个标志如何影响 4.3 的 purge？

**答案**：巨页以整 1GiB 为单位、不能部分释放，也不应被 decommit/reset 打碎（丢掉一页物理内存后无法按原语义找回）。`mi_arena_schedule_purge` 与 `mi_arena_try_purge` 开头都检查 `arena->memid.is_pinned` 直接返回（arena.c:2290、2365），`_mi_os_free_ex` 对 `MI_MEM_OS_HUGE` 走 `mi_os_free_huge_os_pages` 逐页整体释放（os.c:278-281、845-853）。

**练习 2**：`_mi_os_numa_node` 为什么要缓存节点数而不是每次探测？

**答案**：节点数在进程生命周期内不变，而 `_mi_os_numa_node` 位于分配热路径附近（arena 选择、巨页绑定都会查）；用 acquire 读一个原子缓存 + 单节点快路径，把每次 `SYS_getcpu`/读 sysfs 的开销变成一次普通内存读。

**练习 3**：`allow_large_os_pages` 与 `reserve_huge_os_pages` 的启用方式有何本质区别？

**答案**：前者是运行时倾向——常规 `_mi_prim_alloc` 在尺寸与对齐恰好是大页倍数时（`_mi_os_canuse_large_page`，os.c:81-85）才尝试大页，失败可回落；后者是启动时动作——init.c 在进程初始化一次性预留整 GiB 页构成专属 arena，不成功不成立（部分成功则部分可用）。

## 5. 综合实践

把整讲串起来的任务：**量化 `MIMALLOC_PURGE_DELAY` 三档配置下长循环程序的峰值 RSS 与运行时间取舍**。

1. **实践目标**：亲眼看見 purge 延迟如何在小峰值 RSS（好）与低系统调用开销（快）之间交换，并用本讲源码解释三组数据。

2. **操作步骤**：

   a. 编写压测程序（示例代码）：

   ```c
   // 示例代码：purge_bench.c   编译: gcc purge_bench.c -o purge_bench -lmimalloc
   #include <mimalloc.h>
   #include <stdio.h>
   #include <string.h>
   #include <time.h>

   static double now_sec(void) {
     struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
     return ts.tv_sec + ts.tv_nsec / 1e9;
   }

   int main(void) {
     enum { N = 4096, SZ = 64*1024 };   // 每轮约 256MiB，跨约 4096 个 slice
     double t0 = now_sec();
     for (int round = 0; round < 200; round++) {
       void* ps[N];
       for (int i = 0; i < N; i++) {
         ps[i] = mi_malloc(SZ);
         memset(ps[i], 1, SZ);          // 真实触碰，保证物理页驻留
       }
       for (int i = 0; i < N; i++) mi_free(ps[i]);
     }
     printf("elapsed: %.3f s\n", now_sec() - t0);
     return 0;
   }
   ```

   b. 准备一个 RSS 采样器，例如同目录另开终端运行：

   ```bash
   # 示例命令：每 0.2s 记录一次 VmRSS，取峰值
   while kill -0 $PID 2>/dev/null; do
     grep VmRSS /proc/$PID/status | awk '{print $2}' >> rss.log; sleep 0.2;
   done; sort -n rss.log | tail -1
   ```

   c. 分别以三种配置各跑若干次：

   ```bash
   MIMALLOC_PURGE_DELAY=-1   ./purge_bench    # 禁用 purge
   MIMALLOC_PURGE_DELAY=1000 ./purge_bench    # 默认（arena 级 ×4 = 4s 延迟）
   MIMALLOC_PURGE_DELAY=0    ./purge_bench    # 释放即 purge
   ```

   d. 补跑一组 `MIMALLOC_SHOW_STATS=1` 记录三种配置下 `purges`/`purged`/`resets` 计数。

3. **需要观察的现象**：峰值 RSS（`rss.log` 最大值）与 `elapsed` 时间在三档之间的差异；`SHOW_STATS` 输出中 `purged` 字节数与释放总量的比例。

4. **预期结果与解释**（具体数值待本地验证）：

   - `PURGE_DELAY=-1`：`_mi_os_purge_ex` 首行短路（os.c:659），arena 侧 `mi_arena_schedule_purge` 也直接返回（arena.c:2290），`purged` 恒为 0。前一轮释放的 slice 全部留在空闲位图上且物理页驻留，峰值 RSS 最高；但由于零 madvise、且下一轮直接复用已驻留页（无缺页），运行时间应最短。
   - `PURGE_DELAY=1000`：释放只标记 `slices_purge` 位图并设 4 秒到期（arena.c:2299-2310）；在 200 轮快速循环里大概率多数轮次根本没到期就复用了，`purges` 计数很小。RSS 曲线介于两者之间，时间开销接近 -1 档——这是默认值「够好」的原因。
   - `PURGE_DELAY=0`：每次 slice 空闲即触发 `mi_arena_purge`（arena.c:2293-2295），默认 `purge_decommits=1` 下走 `MADV_DONTNEED`。RSS 峰值最低，但 madvise 次数与「purge 后立刻又被申请」造成的重新缺页/commit 使运行时间最长。
   
   三组数据连成一条曲线：**延迟越长 → RSS 越高、速度越快**。这条曲线就是选项 `purge_delay` 的调优空间——内存紧张的长驻服务可下调甚至归零，计算密集的批处理任务可上调或禁用。

5. **思考题**：把 `SZ` 改成 16MiB（超过 `MI_LARGE_MAX_OBJ_SIZE` 的 512KiB，属于 u4-l3 讲过的 huge 单例页）重跑，三档差异会变大还是变小？提示：单例页释放走 `_mi_os_free_ex`（os.c:258-290），若其内存产地是 arena，归还 slice 时同样会进入 `mi_arena_schedule_purge`——此时 purge 粒度不再是「整页退役」而是「整段 slice 区间」，延迟合批的效果会有何不同？

## 6. 本讲小结

- os.c 是 prim 原语之上的**语义层**：负责页对齐（commit 宽松、decommit/reset 保守）、尺寸取整（12.5% 浪费上限）、统计埋点与选项分支；平台差异被 prim 与启动时探测的 `mi_os_mem_config` 能力表完全吸收。
- 四个动词的精确分工：commit=可访问、decommit=立即丢物理页（Linux `MADV_DONTNEED`）、reset=懒丢（`MADV_FREE`，RSS 不降但可回收）、reuse=macOS 记账恢复；`needs_recommit` 出参让同一个 os.c 语义兼容 Windows（必须 recommit）与 Linux release（无需 recommit）。
- purge 是策略包装：`_mi_os_purge_ex` 按「禁用？→自定义 commit_fun？→purge_decommits？→reset」四路分派；`purge_delay<0` 全局禁用、`=0` 立即、`>0` 在 arena 级乘 `arena_purge_mult`（默认 ×4）延迟合批，`minimal_purge_size` 设定最小 purge 粒度以保护 THP。
- 巨页（1GiB，`reserve_huge_os_pages` 启动预留、`mbind` 绑 NUMA 节点、逐页分配可中途止损）与大页（2MiB，`allow_large_os_pages` 运行时倾向）是两套机制；NUMA 节点数探测结果被原子缓存，单节点机器查询零开销。
- 一切行为可观测：统计报表 `arenas` 段的 `reset/purged/resets/purges/mmaps/commits` 与 `process` 段的 `numa nodes` 全部来自本讲讲解的埋点。

## 7. 下一步学习建议

本讲刻意把 arena 侧机制（`slices_purge` 位图、`purge_expire` 时间戳、`mi_arena_purge`）当作黑盒只讲了调用面。下一讲 **u6-l3「arena：1GiB 内存区、64KiB slice 与原子位图分配」** 将正面拆解 arena：保留与扩张策略、slice 位图的无锁区间分配、eager commit 三态（`arena_eager_commit` 的 0/1/2），以及 `mi_manage_os_memory_ex` 如何把自有内存纳入管理。读完 u6-l3 后再回看本讲 4.3 的调度代码，位图操作的含义会完全清晰。若想先补 NUMA 在 arena 层的落点，可顺带读 arena.c 中 `arena_is_numa_local` 选项的消费处。
