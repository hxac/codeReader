# 统计系统：从 mi_stats_t 到 JSON 导出

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `mi_stat_count_t` 的 `peak` / `total` / `current` 三列的确切语义，以及它们在跨线程合并时为什么只能「近似」。
2. 画出统计数字的完整旅程：埋点（alloc.c / free.c / arena.c / os.c）→ theap → heap → subproc，并解释每个环节的合并时机与「破坏式合并」这一副作用。
3. 读懂 `MIMALLOC_SHOW_STATS=1` 输出的 blocks / pages / arenas / process 四段报表，包括五列含义、`ok` / `not all freed` 标记和 S/M/L/H 档位。
4. 按进程（`mi_stats_get`）、堆（`mi_heap_stats_get`）、线程堆（`mi_theap_stats_get`）、子进程（`mi_subproc_stats_get` / `mi_subproc_stats_get_exclusive`）四种粒度取数，知道各自的口径差异。
5. 用 `mi_stats_get_json` 导出 JSON，配合 `stat_version` / `mimalloc_version` 字段搭建自动化的内存回归监控。

## 2. 前置知识

本讲是专家层的「观测」课，假定你已经完成单元三与单元七（尤其 u3-l1 的五层所有权模型、u7-l3 的一等堆）。需要 recall 的概念：

- **theap / heap / subproc 三层**：theap 是线程私有堆（真正执行分配），heap 是跨线程的一等堆（身份与账本），subproc 是分配域（arena 与主堆的容器）。统计字段恰好各挂一份：theap→`stats`、heap→`stats`、subproc→`stats`，本讲的核心就是这三份账本如何对账。
- **MI_STAT 编译级别**：统计不是免费的功能。`MI_STAT` 宏（0/1/2）在编译期决定埋点是否存在；release 构建默认为 0，malloc 级统计整段消失（u1-l4 已观察过这一现象，本讲从源码解释原因）。
- **原子操作三层级**（u5-l1 的术语）：普通读写、原子 load/store、原子 RMW/CAS。统计更新原语分 `_mt`（多线程原子版）与非 `_mt`（线程本地普通读写版）两套，选择依据正是「这个账本是否会被多个线程同时写」。
- **X-macro 技法**：用一个宏列表（`MI_STAT_FIELDS()`）在「定义结构体」「合并」「打印」「转 JSON」四处展开，保证字段清单只有一份权威来源。看到 `#define MI_STAT_COUNT(stat) ...` 后跟 `MI_STAT_FIELDS()` 再 `#undef`，就是这一技法。
- **JSON 与回归监控**：把内存指标序列化成机器可读格式，可以在 CI 里对每个 commit 做快照对比，发现内存泄漏或碎片化劣化。本讲最后会给出一个最小方案。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| `include/mimalloc-stats.h` | 统计的「公共头」：`mi_stat_count_t` / `mi_stats_t` 结构、`MI_STAT_FIELDS()` 字段清单、`MI_STAT_VERSION` 版本号、全部取数/打印/JSON 接口声明。v3 起独立成头，未并入 mimalloc.h |
| `src/stats.c` | 全部实现：更新原语、合并函数、四段报表打印、`mi_stats_get` 家族、JSON 序列化（约 850 行） |
| `include/mimalloc.h` | 只保留废弃接口（`mi_stats_print` 等）与 `mi_output_fun` 输出回调、`mi_process_info` 进程信息 |
| `include/mimalloc/types.h` | `MI_STAT` 级别定义（L80-L87）；三个结构体各自的 `stats` 字段挂载点（theap L597、heap L638、subproc L678） |
| `include/mimalloc/internal.h` | `mi_heap_stat_*` / `mi_theap_stat_*` / `mi_subproc_stat_*` 宏族（L388-L404），是全库埋点的统一入口 |
| `src/alloc.c`、`src/free.c`、`src/page.c`、`src/arena.c`、`src/os.c` | 埋点调用方：malloc/free/页/arena/OS 各层在哪里记账 |
| `src/theap.c`、`src/heap.c`、`src/init.c` | 合并时机：collect 与线程退出（theap.c）、堆销毁（heap.c）、进程退出打印（init.c） |
| `test/test-api.c` | 官方统计用例 `heap-os2`：前后两次 `mi_stats_get` 对比 `pages.current`，是本讲综合实践的范本 |

## 4. 核心概念与源码讲解

### 4.1 数据模型：mi_stat_count_t、MI_STAT_FIELDS 与版本护栏

#### 4.1.1 概念说明

mimalloc 的统计要回答三类问题：

1. 「现在还有多少？」——`current`；
2. 「历史上一共经过多少？」——`total`；
3. 「最忙的时候有多少？」——`peak`。

于是最核心的计数结构 `mi_stat_count_t` 就是三个 `int64_t`。有些指标只有累计意义（如 `mmap_calls` 调用次数），则退化为只有 `total` 的 `mi_stat_counter_t`。

所有指标字段不是手工写进结构体的，而是集中列在 `MI_STAT_FIELDS()` 宏里，再在结构体定义、合并、打印、JSON 四处重复展开。新增一个指标只需改这一处清单。结构体头部还有 `size` / `version` 两个哨兵字段，构成一道 ABI 护栏：你的程序和 mimalloc 库必须对 `mi_stats_t` 的布局达成一致，否则取数接口直接返回 `false` 而不是给出错乱的数字。

#### 4.1.2 核心流程

一个指标字段的一生：

```text
MI_STAT_FIELDS() 清单（唯一权威来源）
   ├─ 在 mi_stats_t 里展开成字段定义      （mimalloc-stats.h L106）
   ├─ 在 mi_stats_add 里展开成逐字段合并   （stats.c L129）
   ├─ 在 _mi_stats_print 里展开成逐字段打印 （实际打印按段落手工挑选）
   └─ 在 mi_stats_get_json_from 里展开成逐字段 JSON 键 （stats.c L797）
```

字段按「记账地点」分成三组，这个分组决定了后面 4.3 节的合并方向：

- **theap 级**（线程私有账本）：`malloc_normal`、`malloc_huge`、`malloc_bins[]`、`malloc_requested`、`pages`、`page_bins[]`、`page_committed`、`pages_abandoned` 等——由分配/释放路径记录；
- **subproc 级**（共享账本，直接记在分配域上）：`reserved`、`committed`、`reset`、`purged`、`mmap_calls`、`commit_calls`、`arena_count`、`threads`、`theaps`、`heaps`、`chunk_bins[]` 等——由 OS/arena/线程管理路径记录；
- **v1/v2 遗留**：`segments` 等四个字段在 v3 已无埋点，仅占位以兼容结构体布局。

尺寸细分统计有三张数组：`malloc_bins[74]`（每个 size bin 的分配）、`page_bins[74]`（每个 bin 的页数）、`chunk_bins[MI_CBIN_COUNT]`（arena chunk 的尺寸桶）。

#### 4.1.3 源码精读

计数结构与字段清单：

- [include/mimalloc-stats.h:L30-L39](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc-stats.h#L30-L39)：`mi_stat_count_t`（total/peak/current 三列）与 `mi_stat_counter_t`（仅 total）。注释明确「count allocation over time」——current 会随释放下降，counter 只增不减。
- [include/mimalloc-stats.h:L41-L82](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc-stats.h#L41-L82)：`MI_STAT_FIELDS()` 完整字段清单，每个字段带一行注释说明记账内容与级别；L70-L74 标注 `segments*` 四个字段「only on v1 and v2」，L75-L82 是 v3 专有的 `heaps`/`theaps`/reclaim 系列。
- [include/mimalloc-stats.h:L96-L116](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc-stats.h#L96-L116)：`mi_stats_t` 本体。头部 `size`/`version` 两个哨兵；`mi_decl_align(8)` 保证原子字段 8 字节对齐（32 位平台上 int64_t 原子操作的前提）；尾部 `_stat_reserved[4]` 预留扩展位；三张 bins 数组垫底。
- [include/mimalloc-stats.h:L15](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc-stats.h#L15)：`MI_STAT_VERSION 5`——「每次向后不兼容变更时递增」，v3.5 当前为 5。
- [include/mimalloc-stats.h:L122-L131](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc-stats.h#L122-L131)：`mi_stats_init`（memset 清零再写头）与 `mi_stats_t_decl(name)` 声明即初始化的便捷宏。

MI_STAT 编译级别与字段挂载点：

- [include/mimalloc/types.h:L80-L87](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L80-L87)：`MI_STAT` 默认值规则——`MI_DEBUG>0` 时为 2（细粒度），否则为 0（只保留 essential）。这解释了 u1-l4 的观察：release 构建连 blocks 段都没有。
- [include/mimalloc/types.h:L594-L598](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L594-L598)：theap 的 `stats` 字段，注释「thread-local statistics」。
- [include/mimalloc/types.h:L635-L639](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L635-L639)：heap 的 `stats` 字段，注释「periodically updated by merging from each theap」——点明它是被动汇合点。
- [include/mimalloc/types.h:L675-L680](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L675-L680)：subproc 的 `stats` 字段，注释「updated for arena/OS stats like committed, and otherwise merged with heap stats when those are deleted」——subproc 账本有两条进项：OS 层直接记账 + 堆销毁时并入。

#### 4.1.4 代码实践

**实践目标**：亲手验证 ABI 护栏与字段清单。

1. 写一个只包含两行的小程序（示例代码）：

   ```c
   // 示例代码：abi-check.c
   #include <stdio.h>
   #include <mimalloc.h>
   #include <mimalloc-stats.h>
   int main(void) {
     printf("sizeof(mi_stats_t) = %zu, MI_STAT_VERSION = %d\n",
            sizeof(mi_stats_t), MI_STAT_VERSION);
     mi_stats_t s;                       // 故意不初始化
     printf("uninit get -> %d\n", mi_stats_get(&s));
     mi_stats_t_decl(t);                 // 声明即初始化
     printf("init   get -> %d\n", mi_stats_get(&t));
     return 0;
   }
   ```

2. 编译运行（需让编译器找到仓库头文件）：

   ```bash
   gcc -I<path-to-repo>/include abi-check.c -o abi-check \
       -L<path-to-repo>/out/debug -lmimalloc-debug && ./abi-check
   ```

3. **需要观察的现象**：第一处 `mi_stats_get` 返回 0（false），第二处返回 1。
4. **预期结果**：`mi_stats_copy` 在 [src/stats.c:L613-L618](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L613-L618) 先校验目标缓冲的 `size` 与 `version` 头，未初始化的栈上结构体通不过校验，宁可失败也不写脏数据。这正是 `mi_stats_t_decl` 存在的意义。
5. 具体打印的 `sizeof` 数值随平台与 `MI_STAT` 级别变化，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `mi_stats_t` 里要放 `_stat_reserved[4]` 预留字段，而不是每次加字段就改 `MI_STAT_VERSION`？

**答案**：两者配合使用。小改动走预留位（结构体大小不变，老程序传进来的缓冲仍然够大，`size` 校验能通过）；布局真正变化（字段顺序调整、数组长度变化）才递增 `MI_STAT_VERSION` 让老程序显式失败。这是共享结构体演化的常规 ABI 策略。

**练习 2**：`malloc_bins` 数组长度是 `MI_BIN_HUGE+1` 即 74，这个 73 与单元三讲的 bin 编号是什么关系？

**答案**：`MI_BIN_HUGE` 为 73（[include/mimalloc-stats.h:L97](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc-stats.h#L97)，注释指向 types.h），即 u3-l3 学过的 huge 哨兵档。0..72 是真实 size bin，73 收容所有超过 `MI_LARGE_MAX_OBJ_SIZE` 的巨大对象，所以数组必须开到 74 项，循环上界写作 `i <= MI_BIN_HUGE`（如 [src/stats.c:L136-L138](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L136-L138)）。

### 4.2 更新原语与全库埋点：_mt 后缀、adjust 与 peak 近似

#### 4.2.1 概念说明

记账动作只有四种基本原语：`increase` / `decrease` / `adjust_increase` / `adjust_decrease`，再乘上「是否原子」得到八个函数。规则很清晰：

- **写线程私有账本（theap->stats）** → 普通版，三次普通内存写，零开销心智能跟上快路径；
- **写共享账本（heap->stats / subproc->stats）** → `_mt` 版（multi-thread），用 relaxed 原子加，因为多个线程可能同时写同一个 subproc 的 `committed`。

`adjust` 系列解决「避免重复计数」：例如 commit 一段内存前发现其中一部分已 commit 过，先把已 commit 的部分 adjust 掉再记新增。它的特别之处在于对 `peak` 的处理——只有当 `total == peak`（即统计处于历史最高点）时才同步抬高 `peak`，否则只动 `total`/`current`。

最微妙的是**跨线程合并时的 peak**。`total` 和 `current` 满足可加性，直接相加即可；但「各线程的 peak 之和 ≥ 全局 peak ≥ 各线程 peak 的最大值」，两个极端都不对。mimalloc 采用的近似是：`新全局 peak ≈ max(旧全局 peak, 合并后的全局 current + 源线程 peak)`。

#### 4.2.2 核心流程

单线程更新一个 `mi_stat_count_t`（以 `increase` 为例）：

```text
输入 amount > 0
current += amount                 ← 普通写（或 relaxed 原子加）
if current > peak: peak = current ← 水位线抬高
total += amount                   ← 只在正增量时累计
```

合并两个账本（逐字段执行）：

```text
total_dst   += total_src                      （可加）
current_dst += current_src                    （可加）
peak_dst     = max(peak_dst, current_dst + peak_src)   （近似，见 4.2.3）
然后：src 整体清零（破坏式合并，见 4.3）
```

peak 近似的直觉：源线程的 peak 是「它自己在场时的最高水位」；把它叠加到合并后的全局 current 上，得到一个「如果时序最坏交错，全局可能达到的高度」的下界估计，再用 max 保底不回退。既不像求和那样系统性高估，也不像取 max 那样在「两线程各自不高的峰恰好同时出现」时低估。

#### 4.2.3 源码精读

两套更新原语：

- [src/stats.c:L24-L40](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L24-L40)：`mi_stat_update_mt`（relaxed 原子加 + 原子 max）与 `mi_stat_update`（纯普通读写）并肩而立，函数体逐行对应，差别只在原子性——这是理解「_mt 后缀」语义的最短路径。
- [src/stats.c:L69-L83](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L69-L83)：`mi_stat_adjust_mt` / `mi_stat_adjust`。注释给出使用场景「before committing a range, first adjust downwards with parts that were already committed」；`if (prev_total == peak) peak += amount` 只在水位线顶端才跟随。
- [src/stats.c:L100-L114](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L100-L114)：`mi_stat_count_add_mt` 的 peak 近似。L107-L111 的注释坦承「peak scores do not really work across threads」，并说明曾经试过直接求和（高估）与取 max（低估），现行方案来自 Artem Kharytoniuk 的 PR#1112——源码注释里少见的算法选型讨论，值得细读。

宏族与典型埋点：

- [include/mimalloc/internal.h:L388-L404](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L388-L404)：`mi_theap_stat_increase` 等三组宏把「取哪个账本 + 哪个字段」压缩成一个调用点，全库埋点统一从这里走。
- [src/alloc.c:L73-L83](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L73-L83)：malloc 快路径的记账，被 `#if (MI_STAT>0)` 与 `#if (MI_STAT>1)` 双层包裹——`malloc_normal` 记块字节数（L75），`malloc_bins[bin]` 每块记 1（L79），`malloc_requested` 记用户请求字节数（L80，含 `size - MI_PADDING_SIZE` 的口径修正）。release 构建（MI_STAT=0）这一整段被预处理器删除，这就是 blocks 段消失的源头。
- [src/free.c:L763-L773](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L763-L773)：free 的对称记账（`malloc_normal` 减、`malloc_bins` 减）。
- [src/arena.c:L1117-L1118](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1117-L1118)：新页入账 `pages` 与 `page_bins`（注意按 `_mi_page_stats_bin(page)` 取统计档，而非分配 bin）。
- [src/os.c:L324-L328](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c#L324-L328)：OS 层直接记到 **subproc** 账本：`mmap_calls`、`reserved`、`committed`。注意这是 `mi_subproc_stat_*` 宏——这三类字段从不经过任何 theap/heap，是 4.3 节「进程统计 ≠ 堆统计之和」的第一个原因。
- [src/init.c:L358](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L358) 与 [src/init.c:L471](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L471)：线程创建/退出时 subproc 账本上 `threads` 的增减。

#### 4.2.4 代码实践

**实践目标**：对比 MI_STAT=0 与 MI_STAT=2 两种构建下同一程序的报表差异。

1. 按单元一的方法构建两份库：`out/release`（默认，MI_STAT=0）与 `out/debug`（MI_DEBUG>0 级联出 MI_STAT=2）。
2. 写一个分配 1000 个 32 字节块再全部释放的小程序，分别链接两份库，均以 `MIMALLOC_SHOW_STATS=1` 运行。
3. **需要观察的现象**：debug 版输出包含完整 blocks 段（每个有分配的 bin 一行、binned/huge/total 汇总行、malloc req 行）；release 版从 blocks 段直接跳到后面段落。
4. **预期结果**：与 [src/stats.c:L367](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L367) 的条件 `stats->malloc_normal.total + stats->malloc_huge.total != 0` 对应——release 下埋点不存在，total 恒为 0，整段被跳过。若你的 release 输出仍有 blocks 段，检查是否误链了 debug 库。
5. 页数与 arena 段两种构建都在（`pages`/`arena_count` 属于 essential 统计），待本地验证具体行数。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `mi_stat_update` 里 `total` 只在 `amount > 0` 时累加，而 `current` 正负都加？

**答案**：`total` 的语义是「历史累计流过量」（总共分配了多少字节），释放（负增量）不应削减它；`current` 是存量（还占着多少），分配加、释放减。两者相减的废值没有意义，但报表上 `total` 用于算吞吐、`current` 用于看泄漏，各司其职。

**练习 2**：`adjust` 与先 `decrease` 再 `increase` 在数学上对 `current` 等价，为什么还要单独的 `adjust` 原语？

**答案**：差别在 `peak` 与 `total`。`decrease+increase` 会把中间态的 `current` 抬高/压低，可能错误地刷新 `peak`，且 `total` 不回退导致虚增；`adjust`（[src/stats.c:L78-L83](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L78-L83)）把 `total` 与 `current` 同向修正，并且只在 `total==peak`（统计正处历史高点）时才同步抬 `peak`，表达的是「修正记账口径」而非「真实的分配/释放事件」。

**练习 3**：两个线程各自 peak 100 MiB，先后合并进同一账本，合并后的 peak 一定 ≥ 100 MiB 吗？可能到 200 MiB 吗？

**答案**：一定 ≥ 100 MiB（max 保底）；理论上界是 200 MiB 但通常达不到——第二个线程合并时算的是「合并后的全局 current + 该线程 peak」，若第一个线程的对象已释放，current 已回落，结果会明显低于 200 MiB。这正是注释所说「peak 分数跨线程本来就不太成立」的含义：它是启发式，不是精确值。

### 4.3 合并链路与三粒度取数 API：theap → heap → subproc

#### 4.3.1 概念说明

三份账本不是镜像，而是**层级汇交**：theap 的数字定期搬进 heap，heap 的数字在销毁/退出时搬进 subproc。关键性质有三：

1. **合并是搬运不是复制**：`_mi_stats_merge_into` 把源账本加进目标后，将源整体清零（保留 header）。所以任意时刻一个数字只住在一层账本里，不会重复计数；代价是「取数」这个读操作会改变数字的分布——把调用线程 theap 的存量搬进 heap。
2. **搬运有明确时机**：theap→heap 发生在该线程的 collect（含线程退出的 abandon collect）尾部；heap→subproc 发生在堆销毁与进程退出打印前。活着且没触发过 collect 的线程，其分配数字仍在 theap 里，未进入任何聚合视图。
3. **subproc 账本自带「底账」**：OS/arena/线程管理层的字段（committed、reserved、mmap_calls、threads、theaps、heaps、chunk_bins…）从一开始就直接记在 subproc 上，不经过任何堆。所以「进程统计 ≠ 各堆统计之和」是设计使然而非 bug。

另外还有一个刻意为之的缺口：元数据堆 `theap_meta`（承载 tld/theap 结构体分配的堆，见 u7-l1）的统计在 subproc 聚合里被「藏起来」了——聚合函数里那行合并代码被注释掉，注释写着「hide meta data stats」。

#### 4.3.2 核心流程

```text
分配路径埋点                 OS/arena/线程管理层埋点
      │                              │
      ▼                              │ (直接记账)
 theap->stats ──┐                    │
 (线程私有)      │ collect 尾部 /      │
      │         │ 线程退出 abandon     ▼
      │         └──────────► heap->stats ──堆销毁/进程退出──► subproc->stats
      │                        (一等堆账本)                      (分配域账本+底账)
      │                                                              ▲
      └── mi_stats_get 时：仅「调用线程」的 theap 被顺手并入 heap ──────┤
                                                                     │
 mi_stats_get(stats) = subproc->stats ＋ Σ(遍历该 subproc 所有堆的 heap->stats)
```

取数家族（都经 `mi_stats_copy` 做 ABI 校验后 memcpy）：

| 粒度 | 接口 | 口径 |
|---|---|---|
| theap | `mi_theap_stats_get(theap, stats)` | 纯该线程账本，不含任何已搬走的数字 |
| heap | `mi_heap_stats_get(heap, stats)` | heap 账本 + 顺手并入调用线程的 theap |
| subproc（聚合） | `mi_subproc_stats_get(id, stats)` | subproc 底账 + 遍历全部堆求和 |
| subproc（独占） | `mi_subproc_stats_get_exclusive(id, stats)` | 仅 subproc 底账，不含堆——正好用来观察「元数据与 arena 部分」 |
| 进程 | `mi_stats_get(stats)` | 当前 subproc 的聚合视图 |

#### 4.3.3 源码精读

合并与取数核心：

- [src/stats.c:L440-L451](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L440-L451)：`_mi_stats_merge_into`——`mi_stats_add(to, from)` 之后紧跟 `mi_stats_init(from)` 把源清零。破坏式合并的实现就这两行。
- [src/stats.c:L121-L142](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L121-L142)：`mi_stats_add` 逐字段合并：`MI_STAT_FIELDS()` 展开覆盖全部标量字段，再补三张 bins 数组。注释提醒它「must be thread safe as it is called from stats_merge」——目标侧全部走原子操作。
- [src/stats.c:L453-L465](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L453-L465)：`mi_stats_merge_theap_to_heap` 与 `mi_heap_get_stats`。后者是取数家族的心脏：先 `_mi_heap_theap_peek(heap)` 查**当前线程**在这个堆上的 theap（不创建，见 [include/mimalloc/prim-tls.h:L399-L409](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim-tls.h#L399-L409)），查到就把它的账本并入 heap 再返回 heap 视角；查不到直接返回 heap 账本。
- [src/stats.c:L634-L653](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L634-L653)：`mi_heap_aggregate_visitor`（访问器模式逐堆求和）、`mi_subproc_stats_get`（底账 + `mi_subproc_visit_heaps` 聚合，L646-L647 被注释掉的两行即「hide meta data stats」）、`mi_stats_get`（一行：当前 subproc 的聚合）。
- [src/stats.c:L620-L632](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L620-L632)：`mi_subproc_stats_get_exclusive`（只拷底账）与 heap/theap 两个 get 的实现。

搬运时机：

- [src/theap.c:L117-L121](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L117-L121)：`_mi_theap_merge_stats`，theap→heap 的唯一实现。
- [src/theap.c:L142-L147](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L142-L147)：每次 collect（正常/强制/abandon）的收尾动作就是合并统计。
- [src/init.c:L378-L388](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L378-L388)：线程退出时 `mi_thread_theaps_done` 对该线程每个 theap 调 `_mi_theap_collect_abandon`——由上一条可知这同时完成了统计合并。**线程 join 之后，它生前的分配数字才进入 heap 账本**。
- [src/heap.c:L27-L35](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/heap.c#L27-L35)：`mi_heap_stats_merge_to_subproc`（heap→subproc）与 `mi_heap_stats_merge_to_main`（销毁堆时并入主堆，对应 u7-l3 讲过的 delete 语义）。
- [src/init.c:L633-L639](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/init.c#L633-L639)：进程退出且开了 show_stats/verbose 时的对账顺序：theap_meta → 默认 theap → 主堆 → subproc，然后打印。L638 行尾注释还坦白了此处可能触碰已释放 thread_local 的风险。

废弃接口（别在新代码里用）：

- [src/stats.c:L472-L477](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L472-L477)：`mi_stats_reset` 名为 reset 实为「把默认 theap 与主堆的数字合并上去」，并不会清零 subproc 账本——名不符实，这大概也是它被标记 deprecated 的原因。
- [include/mimalloc.h:L452-L456](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L452-L456)：deprecated 分组。注意 `mi_stats_merge` 在 v3.5 全仓库只有这一处声明、没有定义（grep 仅命中头文件与文档），调用它会在链接期报未定义符号；`mi_stats_print(NULL)` 是可用的老接口，第一参数必须传 NULL（test/main.c 与 test-stress.c 都这样用）。

#### 4.3.4 代码实践

**实践目标**：亲眼看到「读取有副作用」与「聚合 ≠ 独占 + Σ堆」。

1. 编写下面的程序（示例代码）：

   ```c
   // 示例代码：merge-side-effect.c
   #include <stdio.h>
   #include <mimalloc.h>
   #include <mimalloc-stats.h>

   static long long sum_field(const mi_stats_t* s, int which) {
     // which: 0=current, 1=total（演示用，只看 malloc_normal 一个字段）
     const mi_stat_count_t* c = &s->malloc_normal;
     return (which == 0 ? c->current : c->total);
   }

   int main(void) {
     void* p = mi_malloc(1024 * 1024);      // 记在主线程 theap 里

     mi_stats_t_decl(a);  mi_stats_get(&a);      // 第一次读取
     mi_stats_t_decl(b);  mi_stats_get(&b);      // 第二次读取
     printf("1st: current=%lld total=%lld\n", sum_field(&a,0), sum_field(&a,1));
     printf("2nd: current=%lld total=%lld\n", sum_field(&b,0), sum_field(&b,1));

     mi_subproc_stats_get_exclusive(mi_subproc_current(), &b);  // 只看底账
     printf("exclusive: malloc_normal.current=%lld (预期为 0)\n", sum_field(&b,0));
     mi_free(p);
     return 0;
   }
   ```

2. 用 debug 构建编译运行（malloc 级统计需要 MI_STAT>0）。
3. **需要观察的现象**：两次 `mi_stats_get` 的 `current`/`total` 完全一致（合并搬运守恒）；而 exclusive 视图里 `malloc_normal.current` 为 0——malloc 数字从来不住在 subproc 底账里。
4. **预期结果**：第一次 `mi_stats_get` 内部把主线程 theap 的数字搬进了主堆账本（[src/stats.c:L460-L465](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L460-L465)），第二次取数时数字已在 heap 层，总数不变。若在两次读取之间再执行一次 `mi_malloc`，第二次的 current 会比第一次大约多出该块大小——增量依然被「顺手并入」逻辑捕获。
5. 具体数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：工作线程还活着、从未触发 collect 时，`mi_stats_get` 能看到它的分配吗？怎样才能看到？

**答案**：看不到完整数字。`mi_subproc_stats_get` 遍历堆时只通过 `mi_heap_get_stats` 顺手合并**调用线程**的 theap，其他线程的 theap 数字要等搬运时机：该线程发生 collect（慢路径节拍或显式 `mi_collect`）或线程退出（`mi_thread_theaps_done` → abandon collect → 合并）。所以监控程序应当先 join 工作线程、或先调一次 `mi_collect(false)`，再取进程统计。

**练习 2**：`mi_heap_delete` 与 `mi_heap_destroy` 之后，那个堆的统计数字去哪了？（承接 u7-l3）

**答案**：两者最终都会把 heap 账本并入上层——delete 是把存活页迁去主堆，destroy 是整页归还；统计上对应的搬运是 `mi_heap_stats_merge_to_main` / 销毁路径的 `mi_heap_stats_merge_to_subproc`（[src/heap.c:L27-L35](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/heap.c#L27-L35)）。total 类字段在 destroy 后仍可在进程统计里查到历史流量，current 则随内存归还而回落。

**练习 3**：为什么 `mi_stats_add` 里目标侧全部用原子操作，源侧却只是普通 load？

**答案**：合并可能并发发生（多个线程同时 collect、同时把各自 theap 并入同一个 heap，或退出打印时并 subproc），目标是共享账本必须原子；源（某个 theap 的 stats）只被它自己的属主线程写入、且合并即清零，属主与合并者对同一源的并发在 collect 场景下由调用时序约束，读一次快照即可，不需要 RMW。

### 4.4 报表打印与 JSON 导出：四段结构、五列语义与缓冲契约

#### 4.4.1 概念说明

打印入口 `mi_stats_print_out(out, arg)` 最终落到 `_mi_stats_print`，输出固定四段：

1. **blocks 段**（有 malloc 统计才出现）：逐 bin 行 + binned/huge/total 汇总 + malloc req；
2. **pages 段**（有页统计才出现）：touched / pages / abandoned / reclaima / reclaimf / reabandon / waits / extended / retire / searches——几乎每行都对应单元四至六讲过的一个机制；
3. **arenas 段**（用过 arena 才出现）：reserved / committed / reset / purged 及各种调用计数、heaps/theaps 数量；
4. **process 段**（无条件）：threads、numa nodes、elapsed、user/system 时间、page faults、peak rss/commit。

表头五列是 `peak / total / current / block / total#`。行的「单位参数」决定列的形态：

- `unit > 0`（字节型数据，字段值以 unit 为粒度记录）：前三列打印「字段值 × unit」得到字节；block 列打印 unit 本身（bin 行就是块大小）；total# 列打印字段原始值（分配次数）；行尾按 `current != 0` 标 `not all freed` 否则 `ok`——**泄漏在报表上一眼可见**。
- `unit < 0`（计数型数据，如 pages/abandoned）：前三列按二进制 K/M/G 打印计数（无 B 后缀），后两列留空。
- `unit == 0`（纯计数器行，如 mmaps）：只打印 total 一列。

JSON 侧是同一份 `mi_stats_t` 的机械序列化：头部两个版本字段、process 信息对象、`MI_STAT_FIELDS()` 全部字段、三张 bins 数组。缓冲策略两种：调用方给缓冲（不 realloc，装不下返回 NULL）或不给（库内用 `mi_rezalloc` 从 12 KiB 起倍增，成功则返回堆指针，**用完必须 `mi_free`**）。

#### 4.4.2 核心流程

```text
mi_stats_print_out(out, arg)
  └─ mi_subproc_stats_print_out(当前 subproc)          聚合取数
       └─ _mi_stats_print("subproc", seq, stats, out)
            ├─ 行缓冲包装（256B 栈缓冲，遇 \n 才刷给 out）
            ├─ blocks 段：条件 malloc_normal.total + malloc_huge.total != 0
            ├─ pages  段：条件 pages.total != 0
            ├─ arenas 段：条件 arena_count.total > 0
            └─ process 段：无条件（mi_process_info 现场采集）

mi_stats_get_json(buf_size, buf)
  └─ mi_subproc_stats_get_json → mi_stats_get_json_from
       ├─ 校验 stats 头（size/version）
       ├─ buf==NULL ? 库内分配(12KiB 起倍增) : 使用调用方缓冲(can_realloc=false)
       ├─ "{ stat_version, mimalloc_version, process{...}, <全部字段>, malloc_bins[], page_bins[], chunk_bins[] }"
       └─ 末尾空间检查：不够 → NULL（库内缓冲则先 mi_free）
```

#### 4.4.3 源码精读

格式化与打印：

- [src/stats.c:L154-L185](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L154-L185)：`mi_printf_amount` 的单位规则——`unit>0` 后缀 B、基数 1024（KiB/MiB/GiB）；`unit<=0` 后缀空格、基数仍 1024（K/M/G）；`unit==0` 基数 1000（十进制 1.8 M）。L163-L165 还有「1 B 不打印」的小心思（unit 列为 1 字节时留空）。
- [src/stats.c:L197-L236](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L197-L236)：`mi_stat_print_ex`，五列的权威实现。L221-L228 是 `ok` / `not all freed`（或调用方自定义提示）的判定；L373-L374 那个三目（`malloc_normal_count.total == 0 ? -1 : 1`）说明 binned 行在「有块计数」时按字节显示、否则退化为计数显示。
- [src/stats.c:L271-L295](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L271-L295)：表头 `mi_print_header` 与 bin 行打印——S/M/L/H 档位由 bin 的块大小对照 `MI_SMALL/MEDIUM/LARGE_MAX_OBJ_SIZE` 判定（L284-L286），只打印 `total > 0` 的 bin。
- [src/stats.c:L356-L430](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L356-L430)：`_mi_stats_print` 全流程，四段的触发条件分别在 L367、L386、L404、L424。L388-L400 那串 pages 行与 u4-l2/u6-l4 的机制一一对应（extended=页扩展、retire=退役、reclaima/reclaimf=认领、waits=认领忙等）。
- [src/stats.c:L304-L328](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L304-L328)：行缓冲包装器——256 字节栈缓冲，遇换行才调用真正的输出函数，方便对接外部日志系统。
- [src/stats.c:L568-L597](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L568-L597)：`mi_process_info`。有意思的细节：commit 数字直接取主 subproc 账本的 `committed.current/peak`（L573-L582），RSS/时间/缺页则交给 `_mi_prim_process_info`（getrusage 等平台 API）；elapsed 的起点 `mi_process_start` 由 [src/stats.c:L433-L438](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L433-L438) 在进程初始化时记录。

打印入口家族与 JSON：

- [src/stats.c:L480-L529](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L480-L529)：`mi_heap_stats_print_out`（单堆）、`mi_subproc_heap_stats_print_out`（逐堆打印 + meta + subproc 汇总，适合看分布）、`mi_subproc_stats_print_out` / `mi_stats_print_out`（聚合）。输出回调 `mi_output_fun` 的定义在 [include/mimalloc.h:L187-L188](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L187-L188)，传 NULL 走默认输出，也可用 `mi_register_output` 全局注册。
- [src/stats.c:L660-L694](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L660-L694)：JSON 缓冲结构 `mi_json_buf_t` 与 `mi_json_buf_expand`——初始 `mi_good_size(12*MI_KiB)`（分配器自家 API 上场）、倍增扩容、`can_realloc=false` 时静默停止。
- [src/stats.c:L756-L827](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L756-L827)：`mi_stats_get_json_from` 主体：头校验（L757）、缓冲二选一（L759-L767）、process 段现场采集（L773-L791）、`MI_STAT_FIELDS()` 展开成键值（L794-L797，X-macro 第四次出场）、三张 bins 数组（L802-L817）、末尾空间检查（L819-L823）。
- [src/stats.c:L829-L847](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L829-L847)：四个 JSON 入口（subproc/heap/进程/任意结构体），头文件注释「use mi_free to free the result if the input buf == NULL」是内存契约。test-stress.c 中对应调用仍被注释着（L464），印证其「实验性」定位。

真实输出样例（readme 中 `MIMALLOC_SHOW_STATS=1 ./cfrac` 的运行结果，[readme.md:L295-L341](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L295-L341)）：

```text
subproc 0
 blocks          peak       total     current       block      total#
  bin S    4:    75.3 KiB    55.2 MiB     0          32   B       1.8 M    ok
  ...
  binned    :    84.2 Ki     41.5 Mi      0                                ok
  total     :    84.2 KiB    41.5 MiB     0
  malloc req:                29.7 MiB

 pages           peak       total     current       block      total#
  touched   :   152.8 KiB   152.8 KiB   152.8 KiB
  pages     :     8          14           0                                ok
  ...
 arenas          peak       total     current       block      total#
  reserved  :     1.0 GiB     1.0 GiB     1.0 GiB
  committed :     4.8 MiB     4.8 MiB     4.4 MiB
  ...
 process         peak       total     current       block      total#
  threads   :     1           1           1
  elapsed   :     0.553 s
```

读法示例：bin S 4 行 total 列 55.2 MiB = 1.8M 次分配 × 32B 块；current 全为 0 且行尾 ok 表示退出时全部释放；arenas 段 reserved 1.0 GiB 是 u6-l3 讲的默认 arena 保留量。注意该样例由较早的 v3 构建生成，个别行的单位后缀（如 `41.5 Mi` 无 B）与当前代码的格式化规则略有出入，以后续本地运行为准。

#### 4.4.4 代码实践

**实践目标**：把统计接到自己的日志/文件里，并产出第一份 JSON。

1. 写一个带自定义输出回调的程序（示例代码）：

   ```c
   // 示例代码：json-export.c
   #include <stdio.h>
   #include <mimalloc.h>
   #include <mimalloc-stats.h>

   static void my_out(const char* msg, void* arg) {
     fputs(msg, (FILE*)arg);              // 转发到文件；_mi_stats_print 已做行缓冲
   }

   int main(void) {
     for (int i = 0; i < 1000; i++) { mi_free(mi_malloc(32)); }

     FILE* f = fopen("stats.txt", "w");
     mi_stats_print_out(my_out, f);        // 人读报表
     fclose(f);

     char* json = mi_stats_get_json(0, NULL);   // 库内分配缓冲
     if (json != NULL) {
       FILE* j = fopen("stats.json", "w");
       fputs(json, j);
       fclose(j);
       mi_free(json);                      // 契约：buf==NULL 时用 mi_free 释放
     }
     return 0;
   }
   ```

2. debug 构建编译运行，检查生成的两个文件。
3. **需要观察的现象**：`stats.txt` 是四段报表；`stats.json` 是带 `stat_version` / `mimalloc_version` / `process` / 全部字段 / 三张 bins 数组的合法 JSON。
4. **预期结果**：可用 `python3 -m json.tool stats.json` 校验合法性，并能用 `jq '.malloc_bins[4]'` 抽出单个 bin 的 `{total, peak, current, block_size, page_size}` 对象（字段名见 [src/stats.c:L696-L705](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L696-L705)）。若把 `mi_stats_get_json(0, NULL)` 换成自带 64 字节小缓冲，预期返回 NULL——缓冲装不下近 30 KB 的完整输出，具体体量待本地验证。
5. 连续两次调用 `mi_stats_get_json` 时第一次的指针要先释放再取第二次，否则泄漏。

#### 4.4.5 小练习与答案

**练习 1**：报表里某 bin 行行尾是 `not all freed`，total 列 10 MiB，current 列 3 MiB，这说明什么？

**答案**：该 size bin 历史上累计分配 10 MiB，退出时仍有 3 MiB 没释放——要么是泄漏，要么是仍在用的活数据。`current != 0` 正是 L221-L228 打印 `not all freed` 的条件（[src/stats.c:L221-L228](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L221-L228)）。结合 malloc req 行可以进一步区分泄漏与 size class 取整浪费。

**练习 2**：`process` 段的 `peak commit` 数字来自哪里？为什么不调用系统 API 获取？

**答案**：直接取主 subproc 账本的 `committed.peak`（[src/stats.c:L573-L582](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L573-L582)）。这是分配器自己的记账，粒度是 mimalloc 的 commit/decommit 事件而非 OS 视角，免去一次系统调用，且与 arenas 段的 committed 行同源可互相印证；RSS/缺页等才走 `_mi_prim_process_info`。

**练习 3**：如果想让 JSON 写进一个固定 64 KiB 的静态缓冲，需要注意什么？

**答案**：传 `mi_stats_get_json(65536, buf)` 后 `can_realloc=false`：写入会在装满时静默截断，函数末尾检查后**返回 NULL**（[src/stats.c:L819-L823](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L819-L823)），此时缓冲内容不完整不可用；且此路径不会 free 你的缓冲。bins 数组固定 74+74+6 项，JSON 体量基本有下限，缓冲别小于几十 KB。

## 5. 综合实践：多线程程序的对账实验与 JSON 留档

这是本讲的贯穿任务，把 4.1-4.4 串起来：**进程级统计与「各堆统计之和」为什么对不上？差在哪一层？**（即规格中的「元数据归属」问题）

**实践目标**：量化三个口径的差异，并用 JSON 留档。

### 步骤一：编写对账程序

```c
// 示例代码：reconcile.c —— 需链接 debug 构建（malloc 级统计要求 MI_STAT>0）
#include <stdio.h>
#include <pthread.h>
#include <mimalloc.h>
#include <mimalloc-stats.h>

#define NTHREADS 3
#define NALLOC   200000

static mi_heap_t* g_heap;                 // 一等堆：三个线程共享（u7-l3）

static void* worker(void* arg) {
  (void)arg;
  void* keep[NALLOC / 2];
  for (int i = 0; i < NALLOC; i++) {
    void* p = mi_heap_malloc(g_heap, 48); // 记入本线程在 g_heap 上的 theap
    if (i % 2 == 0) keep[i / 2] = p;      // 留一半不释放，制造 nonzero current
    else mi_heap_free(p);
  }
  return NULL;                            // 线程退出 → abandon collect → 统计并入 g_heap
}

int main(void) {
  g_heap = mi_heap_new();
  pthread_t th[NTHREADS];
  for (int i = 0; i < NTHREADS; i++) pthread_create(&th[i], NULL, worker, NULL);
  for (int i = 0; i < NTHREADS; i++) pthread_join(th[i], NULL);  // 先 join 再对账！

  // 口径 A：进程级（当前 subproc 聚合）
  mi_stats_t_decl(proc);   mi_stats_get(&proc);
  // 口径 B：各堆之和（共享堆 + 主堆）
  mi_stats_t_decl(hs);     mi_heap_stats_get(g_heap, &hs);
  mi_stats_t_decl(ms);     mi_heap_stats_get(mi_heap_main(), &ms);
  // 口径 C：subproc 独占底账（不含任何堆）
  mi_stats_t_decl(excl);   mi_subproc_stats_get_exclusive(mi_subproc_current(), &excl);

  printf("           %14s %14s %14s\n", "proc(A)", "sum-heaps(B)", "exclusive(C)");
  printf("malloc_cur %14lld %14lld %14lld\n",
         (long long)proc.malloc_normal.current,
         (long long)(hs.malloc_normal.current + ms.malloc_normal.current),
         (long long)excl.malloc_normal.current);
  printf("committed  %14lld %14lld %14lld\n",
         (long long)proc.committed.current,
         (long long)(hs.committed.current + ms.committed.current),
         (long long)excl.committed.current);
  printf("pages_cur  %14lld %14lld %14lld\n",
         (long long)proc.pages.current,
         (long long)(hs.pages.current + ms.pages.current),
         (long long)excl.pages.current);

  // JSON 留档
  char* json = mi_stats_get_json(0, NULL);
  if (json != NULL) {
    FILE* f = fopen("reconcile.json", "w");
    if (f != NULL) { fputs(json, f); fclose(f); }
    mi_free(json);
  }
  return 0;
}
```

编译（路径按你的构建目录调整）：

```bash
gcc -I<path-to-repo>/include reconcile.c -o reconcile \
    -L<path-to-repo>/out/debug -lmimalloc-debug -lpthread && ./reconcile
```

### 步骤二：预测再验证

动手前先写下预测，再对照输出（数值待本地验证，方向性结论应稳定）：

| 字段 | A（进程） vs B（Σ堆） | 解释 |
|---|---|---|
| `malloc_normal.current` | 基本相等（先 join 了线程） | malloc 埋点全在 theap→heap 链上；线程退出时已搬运。若去掉 join 直接对账，A 会明显小于真实值——活线程的数字还在各自 theap 里（4.3 练习 1） |
| `committed.current` | A 明显大于 B | committed 直接记在 subproc 底账（[src/os.c:L324-L328](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c#L324-L328)、arena.c 同类埋点），堆账本里几乎是 0；这正是口径 C 非零的主体 |
| `pages.current` | 基本相等，可能有少量差 | 页埋点在 theap（[src/arena.c:L1117-L1118](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1117-L1118)）；abandoned 未认领页等边角会造成小差 |
| `mmap_calls` / `arena_count` / `threads` / `theaps` / `heaps` | 只在 A（和 C）出现 | 纯 subproc 底账字段，任何堆求和都不含 |

### 步骤三：解释「元数据归属」差异

对照源码写出三条根因（这就是本实践的交付物之一）：

1. **底账归属**：OS/arena/线程管理层的事件从一开始就记在 subproc 账本（4.2.3 列出的 `mi_subproc_stat_*` 调用点），堆账本天生不含这些字段。
2. **元数据堆被隐藏**：`mi_subproc_stats_get` 中对 `theap_meta` 的合并被注释禁用（「hide meta data stats」，[src/stats.c:L646-L647](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L646-L647)）——元数据堆（tld/theap 结构体所在，u7-l1）的流量在聚合视图里是刻意不可见的，只能靠口径 C 间接观察。
3. **peak 不可加**：跨线程 peak 是近似值（4.2 的 PR#1112 方案），任何涉及 peak 的「A 对 B」比较都不该期望严格相等。

### 步骤四：搭最小回归监控

1. 把 `reconcile.json` 按 commit 归档（如 `stats/<git-sha>.json`）。
2. 用脚本抽取关键指标做趋势对比，例如：

   ```bash
   jq '{malloc_cur: .malloc_normal.current, committed_cur: .committed.current,
        pages_cur: .pages.current, bin48: .malloc_bins[10].current}' reconcile.json
   ```

3. 在 CI 里比较相邻 commit：`malloc_normal.current` 在「应当全部释放」的基准程序里持续上涨即为泄漏信号；`committed.current / malloc_normal.current` 比值恶化即为碎片化信号。
4. 解析前先检查 `stat_version`（当前为 5）与 `mimalloc_version`，不匹配就跳过——这正是 4.1 里 ABI 护栏在数据文件侧的镜像用法。

## 6. 本讲小结

- `mi_stat_count_t` 三列各有分工：`current` 是存量（泄漏探测）、`total` 是累计流量（吞吐分析）、`peak` 是水位线；跨线程合并时只有 peak 是近似值（全局 current + 源 peak 取 max）。
- 更新原语分 `_mt`（原子，写共享账本）与非 `_mt`（普通写，写线程私有 theap 账本）两套；`adjust` 系列用于修正重复计数且只在水位线顶端同步 peak。
- 统计住三层账本：theap（分配/释放埋点）→ heap（collect 与线程退出时破坏式并入）→ subproc（OS/arena 底账 + 堆销毁时并入）。**合并即清零源**，因此取数接口有搬运副作用，但总量守恒。
- `MIMALLOC_SHOW_STATS=1` 的四段报表（blocks/pages/arenas/process）各有出现条件；五列 peak/total/current/block/total# 的形态由行的单位参数决定；行尾 `not all freed` 直接暴露泄漏。release 构建 MI_STAT=0 时 malloc 级统计整段消失。
- 取数 API 按粒度分四档（theap/heap/subproc 聚合/subproc 独占），全部经过 `size`+`version` 双哨兵的 ABI 校验，未初始化的目标结构体会使调用失败返回 false。
- `mi_stats_get_json` 支持库内分配（用后 `mi_free`）与自带缓冲（装不下返回 NULL）两种模式；JSON 自带 `stat_version`/`mimalloc_version`，适合做 CI 内存回归基线。
- 「进程统计 ≠ Σ堆统计」是设计结果：底账字段直接记 subproc、元数据堆被刻意隐藏、peak 不可加。

## 7. 下一步学习建议

- **u9-l4（堆遍历）**：统计只告诉你「多少」，`mi_heap_visit_blocks` 告诉你「哪一块」。两者结合可以做带调用栈的泄漏定位工具，是 CPython GC 集成的基础。
- **u9-l5（工具链与基准）**：把本讲的 JSON 回归监控与 ASAN/valgrind 构建结合：前者看趋势、后者抓个案；再用 LD_PRELOAD 对比测试量化 mimalloc 与系统 malloc 在真实负载上的差异。
- **源码延伸阅读**：对照 [src/stats.c:L107-L113](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L107-L113) 提到的 PR#1112 讨论体会统计精度的取舍；再读 [test/test-api.c:L461-L487](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-api.c#L461-L487) 的 `heap-os2` 用例，看官方如何用两次 `mi_stats_get` 的 `pages.current` 相等性来断言「堆销毁后内存真的还干净了」。
