# arena：1GiB 内存区、64KiB slice 与原子位图分配

## 1. 本讲目标

上一讲（u6-l2）我们看完了 os.c：它把「向 OS 要内存 / 还内存」统一成 reserve、commit、decommit、reset、purge 五个动词。但 mimalloc 并不是每次要页都直接去找 OS——那样系统调用开销和地址空间碎片都不可接受。真正做法是：**先从 OS 批发一大块内存（arena），再在自己内部零售**。

本讲读完你应该能：

1. 说出 arena 的尺寸体系：slice 64KiB、chunk 32MiB、单个 arena 最小 32MiB、默认保留增量 1GiB、上限 16GiB、全进程最多 160 个（`MI_MAX_ARENAS`）。
2. 跟踪一次 slice 分配的全链路：`_mi_arenas_alloc_aligned` → `mi_arenas_try_alloc` → `mi_arena_try_alloc_at` → 原子位图 `mi_bbitmap_try_find_and_clearN` 一次原子操作拿走连续区间。
3. 解释 bitmap.h 的三层结构（bfield / bchunk / chunkmap）为什么能做到无锁、免 ABA，以及失败时如何回滚。
4. 说明「分箱位图」（bbitmap）如何把小页和大页养在不同的 chunk 里以抑制碎片。
5. 会用 `mi_reserve_os_memory_ex` 预留独占 arena，用 `mi_heap_new_in_arena` 建堆，并用 `mi_arena_contains` 验证分配确实落了进去。

## 2. 前置知识

- **reserve / commit / purge 三件套**（u6-l1、u6-l2）：reserve 只占虚拟地址、commit 才给物理页、purge 把物理页还给 OS。arena 的大量逻辑就是「何时 commit、何时 purge」。
- **mi_memid_t「产地证」**（u3-l1）：每块从 arena 拿到的内存都带着 `memid.mem.arena.{arena, slice_index, slice_count}`，释放时凭它归还。
- **位运算三兄弟**：`ctz`（数末尾 0 的个数，即最低 set 位位置）、`clz`（最高 set 位位置）、`popcount`（数 1 的个数）。mimalloc 的位图算法完全建立在这三条指令上（x86 上是 `tzcnt/lzcnt/popcnt`）。
- **原子读改写**：`atomic OR`、`atomic AND`、`CAS`。置位用 OR、清位用 AND 是「无锁位图」的关键——它们天然不需要 CAS 循环就能完成一次确定性的修改。
- **保守近似（conservative approximation）**：一个索引结构允许「多报」不允许「漏报」。多报只是多扫一次空 chunk，漏报则会丢失空闲内存。chunkmap 就是一个保守近似。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [src/arena.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c) | arena 的创建、扩张、slice 分配与归还、purge 调度、调试打印（约 2750 行） |
| [src/bitmap.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.h) | 原子位图的数据结构（bfield/bchunk/chunkmap/bitmap/bbitmap）与 API 声明 |
| [src/bitmap.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c) | 位图算法实现：原子置/清、区间查找、跨字回滚、SIMD 优化 |
| [include/mimalloc/types.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h) | `mi_arena_t` 结构、全部尺寸宏（slice/chunk/arena 上下限） |
| [src/options.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c) | `arena_reserve`、`arena_eager_commit`、`arena_purge_mult` 等选项默认值 |
| [include/mimalloc.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h) | 公开 API：`mi_reserve_os_memory_ex`、`mi_manage_os_memory_ex`、`mi_heap_new_in_arena`、`mi_arena_contains`、`mi_arenas_print` |

## 4. 核心概念与源码讲解

### 4.1 arena 的骨架：slice 刻度与 mi_arena_t

#### 4.1.1 概念说明

arena 是一块**预先从 OS 批发来的、被 mimalloc 独自管理的固定内存区**。它和 mimalloc 其他部分最大的不同写在文件头注释里：arena 是**全进程共享**的，多个线程同时从中切内存，因此所有操作必须用原子指令完成（页以下的世界是线程私有的，arena 以上的世界是共享的——这正是三层设计的分界线）。

arena 内部以 **slice（切片）** 为最小出租单位，64 位平台下一个 slice = 64KiB。为什么是 64KiB？因为它同时是：

- 最小页尺寸 `MI_SMALL_PAGE_SIZE`（小对象页一页一个 slice）；
- page map 的刻度（u3-l4 讲过：地址→页的反查以 slice 为粒度）；
- 位图中 1 个 bit 的含义（1 bit = 1 slice = 64KiB，1GiB arena 只需 16K bit ≈ 2KiB 位图）。

#### 4.1.2 核心流程

尺寸换算链（64 位平台）：

\[
\begin{aligned}
1\ \text{slice} &= 64\,\text{KiB} = 2^{16}\ \text{字节} \\
1\ \text{bchunk} &= 512\ \text{bits} \Rightarrow 512\ \text{slices} = 32\,\text{MiB} \\
\text{arena 最小} &= 1\ \text{bchunk} = 32\,\text{MiB} \\
\text{arena 最大} &= 512\ \text{bchunks} = 16\,\text{GiB} \\
\text{slice 数} &= \lceil \text{size} / 64\,\text{KiB} \rceil
\end{aligned}
\]

arena 结构体的内存布局是「自举」的：`mi_arena_t` 头部和它管理的全部位图，就存放在这块内存自己的开头几个 slice 里（称为 info slices），剩下的 slice 才对外出租。

#### 4.1.3 源码精读

arena.c 的文件头注释是一份微型设计文档，值得整段读：arena 用来出租 ≥64KiB 的大块、跨线程共享必须原子化、也服务于 1GiB 巨页与 WASI/sbrk 等场景——见 [src/arena.c:8-20](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L8-L20)。

slice 与 chunk 的尺寸宏集中定义在 types.h：[include/mimalloc/types.h:212-229](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L212-L229)。注意三档页尺寸与位图操作的漂亮对应：小页 64KiB = 1 个 bit、中页 512KiB = 1 个字节（8 bit）、大页 4MiB = 1 个机器字（64 bit）。slice 的实际大小由 `MI_ARENA_SLICE_SHIFT = 13 + MI_SIZE_SHIFT` 决定，64 位下为 16（即 64KiB），见 [include/mimalloc/types.h:189-196](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L189-L196)。

`mi_arena_t` 结构体本体在 [include/mimalloc/types.h:731-758](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L731-L758)。最关键的是它挂着**四张位图**加一组页登记位图：

| 字段 | 1 个 set 位表示…… | 类型 |
| --- | --- | --- |
| `slices_free` | 该 slice **空闲**（可分配） | `mi_bbitmap_t`（分箱） |
| `slices_committed` | 该 slice 已 commit（可访问） | `mi_bitmap_t` |
| `slices_dirty` | 该 slice 可能非零（需清零才能当 `mi_zalloc` 用） | `mi_bitmap_t` |
| `slices_purge` | 该 slice 已空闲、等待到期 purge | `mi_bitmap_t` |

注意约定：**`slices_free` 里 set（1）表示空闲**，分配是把 1 清成 0——这与直觉相反，但正是这个方向让「找一段连续空闲」变成「找一段连续的 1」，可以直接复用找 1 的位运算指令。

上下限与数量上限：[include/mimalloc/types.h:716-718](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L716-L718) 定义 `MI_ARENA_MIN_SIZE`（32MiB）与 `MI_ARENA_MAX_SIZE`（16GiB）；[include/mimalloc/types.h:612](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L612) 定义 `MI_MAX_ARENAS = 160`——每个 subproc 持有一个 `arenas[160]` 数组（[include/mimalloc/types.h:658](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L658)）。slice↔字节 的两个换算函数在 [include/mimalloc/internal.h:1302-1309](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1302-L1309)。

arena 元数据自举：`mi_arena_initialize` 在创建时先算出 info slices 需求（`mi_arena_info_slices_needed`，[src/arena.c:1628-1647](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1628-L1647)：4 张位图 + `MI_ARENA_BIN_COUNT` 张 abandoned 页位图 + 1 张 free 的 bbitmap），然后在 arena 自己的内存里依次摆放这四张位图——见 [src/arena.c:1748-1756](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1748-L1756)。最后把除 info 区之外的 slice 全部标成「空闲」（即把 `slices_free` 的对应位全部置 1）：[src/arena.c:1766-1788](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1766-L1788)。这段里还能看到 `MI_PAGE_META_IS_ALIGNED` 构建下每 256MiB 还要再扣掉几个 slice 存页元数据（呼应 u3-l4）。

#### 4.1.4 代码实践

**实践目标**：用 `mi_arenas_print()` 把 arena 的内部状态「显影」出来，建立对 slice/chunk/arena 三级刻度的直观感受。

**操作步骤**（示例代码）：

```c
// arena-print.c —— 示例代码
#include <stdio.h>
#include <mimalloc.h>

int main(void) {
  puts("== before any allocation ==");
  mi_arenas_print();          // 默认惰性创建，此时可能什么都没有

  void* p = mi_malloc(4096);  // 第一次分配会触发保留默认 arena
  puts("== after first allocation ==");
  mi_arenas_print();

  mi_free(p);
  return 0;
}
```

编译运行（假设你已按 u1-l2 完成 release 构建）：

```bash
gcc arena-print.c -o arena-print -I<仓库路径>/include -L<仓库路径>/out/release -lmimalloc
LD_LIBRARY_PATH=<仓库路径>/out/release ./arena-print
```

**需要观察的现象**：打印输出的每一行形如 `arena 0 at 0x...: 16384 slices (1024 MiB), subproc: 0, numa: -1`，随后是一张由 `.`/`p`/`i` 等字符组成的 chunk 图（图例在输出开头）。

**预期结果**：默认 arena 应显示约 16384 个 slice（1GiB / 64KiB）；chunk 图开头有若干 `i`（arena 自身的元数据 slice），后面大片 `.`（free-reserved，已保留未提交）。精确行数与平台相关，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：一个 1GiB 的 arena 有多少个 slice、多少个 bchunk？`slices_free` 位图本身占多少内存？

**答案**：\(16384 = 2^{30}/2^{16}\) 个 slice；\(32 = 16384/512\) 个 bchunk；位图 \(16384\ \text{bit} = 2\,\text{KiB}\)（外加 chunkmap 与头部）。

**练习 2**：为什么 `slices_committed`、`slices_dirty` 用普通 `mi_bitmap_t`，而 `slices_free` 用分箱的 `mi_bbitmap_t`？

**答案**：committed/dirty 只做「按已知下标置位/清位/查询」（分配到了具体区间后才去标记），不需要「找一段连续空闲区间」这种搜索；而 free 需要高效搜索且要抗碎片，因此需要 chunk 分箱索引（见 4.5 节）。

### 4.2 arena 的保留与扩张：从 1GiB 默认值到指数增长

#### 4.2.1 概念说明

mimalloc 从不「按需一个页一个页地向 OS 要」，而是**成批保留**：默认每次保留 1GiB；随着 arena 数量增多，保留量还会**指数加倍**。这样设计有三个动机：

1. 减少 mmap 等系统调用次数；
2. 让地址空间聚集，改善 TLB 局部性；
3. 为 16GiB 大对象预留可能（单个 arena 上限 16GiB 正是位图能表达的上限）。

用户也可以自己「圈地」：把一块自备内存（如显存映射、定制的 huge page 区域）交给 `mi_manage_os_memory_ex` 管理，或者用 `mi_reserve_os_memory_ex` 预留一个**独占（exclusive）arena**——只有明确指定它的堆才能从中分配。

#### 4.2.2 核心流程

自动保留的决策树（`mi_arena_reserve`）：

```text
arena 数 > MI_MAX_ARENAS-4 (=156)?  → 放弃（留 4 个空位给用户自建 arena）
arena_reserve 选项 == 0?            → 放弃（用户明确禁用）
基础保留量 = arena_reserve 选项值（默认 1GiB），向上对齐到 64KiB
若已有 n 个 arena（1 ≤ n ≤ 128）:
    保留量 × 2^clamp(n/8, 0, 16)     ← 每 8 个 arena 翻一倍
保留量 = max(保留量, 本次请求 size + 元数据余量)
钳制到 [32MiB, 16GiB]
保留失败 → 回退再试 4×32MiB = 128MiB
```

扩张公式：

\[
\text{reserve}(n) = \text{reserve}_0 \times 2^{\min(\lfloor n/8 \rfloor,\ 16)}, \quad 1 \le n \le 128
\]

#### 4.2.3 源码精读

默认保留量来自选项表：`MI_DEFAULT_ARENA_RESERVE` 在 64 位下为 `1024*1024`（单位 KiB，即 1GiB），见 [src/options.c:51-56](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L51-L56)，挂到 `mi_option_arena_reserve` 的表项在 [src/options.c:148](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L148)（可用环境变量 `MIMALLOC_ARENA_RESERVE` 覆盖）。

`mi_arena_reserve` 全函数在 [src/arena.c:341-407](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L341-L407)，其中指数扩张的核心五行是 [src/arena.c:355-362](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L355-L362)——`multiplier = 1 << clamp(arena_count/8, 0, 16)`。上限与下限钳制在 [src/arena.c:370-378](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L370-L378)（`MI_ARENA_MAX_SIZE` 注释明确写着 16 GiB）；失败后的 128MiB 回退在 [src/arena.c:397-405](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L397-L405)。

eager commit 策略（连接 u6-l2 的 purge 叙事）：选项 `arena_eager_commit` 默认为 2，表示「只在支持 overcommit 的 OS（如 Linux）或允许大页时才一次性 commit 整个 arena」；置 1 则无条件全 commit——见 [src/arena.c:383-392](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L383-L392)。在 Linux overcommit 下，commit 记账被推迟到 slice 首次真正分配时（同一行的 `mi_subproc_stat_adjust_decrease`）。

新 arena 注册进 subproc 的 `arenas[]` 数组用的是无锁双保险：先扫有没有 NULL 空位（CAS 占坑），没有再 CAS 递增 `arena_count` 开新槽——[src/arena.c:1573-1611](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1573-L1611)。

自备内存路径：`mi_manage_os_memory_ex2` 会先把起点向上对齐到 `MI_ARENA_ALIGNMENT`（默认 64 位构建因 `MI_PAGE_META_IS_ALIGNED` 而为 256MiB，见 [include/mimalloc/types.h:245-251](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L245-L251)），把 slice 总数**向下**取整到 512 的倍数（[src/arena.c:1813-1820](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1813-L1820)），若超过 16GiB 则拆成多个 arena，第一个是 parent、其余是 `parent` 指针指回来的 sub-arena（[src/arena.c:1822-1858](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1822-L1858)）。公开包装函数 `mi_manage_os_memory_ex` / `mi_reserve_os_memory_ex` 在 [src/arena.c:1863-1871](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1863-L1871) 与 [src/arena.c:1910-1912](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1910-L1912)。

官方用法示范可看 [test/main-override-static.c:261-281](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/main-override-static.c#L261-L281)（Windows 下 `VirtualAlloc` 一块内存 → `mi_manage_os_memory_ex` → `mi_heap_new_in_arena`，注释写着「CUDA 显存场景」）。另外 [test/test-api.c:567-591](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-api.c#L567-L591) 中有 `mi_reserve_os_memory_ex(64*1024*1024, ...)` 的测试写法，但注意这段在当前 HEAD 是**被注释掉**的，只作参考。

#### 4.2.4 代码实践

**实践目标**：感受指数扩张与 `MI_MAX_ARENAS` 上限。

**操作步骤**：

1. 给上一节的 arena-print 程序加一句 `mi_option_set(mi_option_arena_reserve, 4096);`（单位 KiB，即把首次保留量压到 4MiB——它会立刻被钳到下限 32MiB）。
2. 再写一个循环，反复 `mi_free(mi_malloc(64*1024*1024))` 之外持续 `mi_malloc(1<<20)` 触发新 arena。
3. 每次分配后调用 `mi_arenas_print()`，记录每个 arena 的 MiB 数。

**需要观察的现象**：第一个 arena 是 32MiB（下限），随后新 arena 的容量逐个/逐批翻倍。

**预期结果**：arena 序列容量大致为 32MiB → … → 64MiB → … → 128MiB → …，具体翻倍节奏取决于触发顺序（每 8 个翻一倍），**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`mi_arena_reserve` 为什么在 `arena_count > MI_MAX_ARENAS - 4` 时就拒绝，而不是等到 160？

**答案**：给用户通过 `mi_manage_os_memory_ex` / `mi_reserve_os_memory_ex` 自建 arena 留出空位（[src/arena.c:344](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L344)）；自动扩张永远不该把表挤满。

**练习 2**：用户 `mi_manage_os_memory_ex` 传入 64MiB 内存，起点未对齐 256MiB，会发生什么？

**答案**：起点被向上对齐、大小相应缩小（[src/arena.c:1801-1811](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1801-L1811)），真实基地址记在 `memid` 里以便将来整体归还；64MiB = 1024 slice = 恰好 2 个 bchunk，满足 ≥32MiB 的最小要求。

### 4.3 slice 的分配与归还：mi_arena_try_alloc_at

#### 4.3.1 概念说明

「从 arena 切 n 个连续 slice」这件事必须满足：任意时刻一个 slice 只能被一个所有者拿到，且全程无锁。mimalloc 的解法是把**「查找空闲区间」和「占为己有」合并成一次原子操作**：在 `slices_free` 位图里找到一段 n 个连续的 1，并用原子 AND 一次清成 0——只要这次原子操作成功，这段区间就归你了；失败（说明有人抢先）就换个位置重试。不存在「先找到再占用」的两步窗口，因此既不需要锁，也不存在 ABA 问题。

拿到区间之后还要做三件簿记：标 dirty（这批 slice 现在被触碰了）、确保 committed（不够就 commit 并记账）、把产地写进 `mi_memid_t`。归还则是镜像操作：先安排 purge，再把位图置回 1。

#### 4.3.2 核心流程

分配（自顶向下）：

```text
_mi_arenas_alloc_aligned(heap, size, ...)
  ├─ 闸门：允许 arena？ size ∈ [64KiB, mi_arena_max_object_size()]？ 对齐 ≤ 64KiB？
  ├─ slice_count = ceil(size / 64KiB)
  └─ mi_arenas_try_alloc
       ├─ mi_arenas_try_find_free：按 NUMA 匹配 → 不限 NUMA 两轮遍历 arena
       │    └─ mi_arena_try_alloc_at（对每个候选 arena）
       │         ├─ mi_bbitmap_try_find_and_clearN ← 一次原子操作认领区间（4.4/4.5 节）
       │         ├─ 标记 slices_dirty
       │         ├─ 需要 commit? → popcount slices_committed，缺多少补多少
       │         └─ 生成 memid{arena, slice_index, slice_count}
       └─ 全部落空且无指定 arena → 加锁保留新 arena → 再找一遍
```

归还（`_mi_arenas_free`）：OS 产地直接 `_mi_os_free`；arena 产地 → `mi_arena_schedule_purge`（延迟 purge，见 u6-l2）→ `mi_bbitmap_setN` 把区间置回 1。

#### 4.3.3 源码精读

入口闸门在 [src/arena.c:595-616](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L595-L616)：只有 `size >= MI_ARENA_MIN_OBJ_SIZE`（64KiB）且 `size <= mi_arena_max_object_size()` 才走 arena，否则回退 OS 直配。上限函数 [src/arena.c:116-128](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L116-L128) 把选项值对齐到 slice、再被固定上限（`mi_arena_max_fixed_object_size`，[src/arena.c:107-113](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L107-L113)）钳住——呼应 u4-l3 讲过的「压低它会把大页挤出 arena」。

**认领的核心一行**在 `mi_arena_try_alloc_at`（[src/arena.c:240-335](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L240-L335)）开头：

[src/arena.c:246](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L246)：`mi_bbitmap_try_find_and_clearN(arena->slices_free, tseq, slice_count, &slice_index)` —— 在整张 free 位图里找 `slice_count` 个连续的 1 并原子清零，成功则得到起始 `slice_index`。这就是「一次原子操作完成查找+占有」。

随后 [src/arena.c:249-264](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L249-L264) 算出用户指针 `p = arena 起点 + slice_index × 64KiB`，生成 memid，并把这段 slice 标记为 dirty（`mi_bitmap_setN(arena->slices_dirty, ...)` 的返回值顺便告诉我们其中多少 slice 原本就是脏的——用来精确统计 touched slices）。

commit 补齐在 [src/arena.c:267-307](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L267-L307)：先用 `mi_bitmap_popcountN` 数出区间内已 commit 的 slice 数，缺额部分调用 `_mi_os_commit`，成功后把 `slices_committed` 对应位补齐；若 commit 失败则把 free 位图恢复原状（`mi_bbitmap_setN`）并返回 NULL——严谨的回滚。

遍历哪些 arena：`mi_arenas_try_find_free`（[src/arena.c:497-522](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L497-L522)）先只找 NUMA 匹配的，找不到再放宽到任意 NUMA。遍历顺序由宏 `mi_forall_suitable_arenas`（[src/arena.c:484-490](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L484-L490)）控制，起点由 `mi_arena_start_idx`（[src/arena.c:429-452](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L429-L452)）按 heap 序号与线程序号分散——**不同线程从不同下标开始转圈**，天然降低争用。某个 heap 是否允许用某个 arena 由 `mi_arena_is_suitable`（[src/arena.c:48-54](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L48-L54)）判定：指定了 `req_arena`（独占 arena）时，只有它自己或它的 sub-arena 合格。

全部落空时的兜底在 `mi_arenas_try_alloc`（[src/arena.c:525-570](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L525-L570)）：用 `mi_lock`（u8-l1 会细讲的自旋锁）保证同一时刻只有一个线程去 `mi_arena_reserve` 保留新 arena，进锁后先复查 arena 数是否已被别人改变（双检），出来后再找一遍。

归还路径 `_mi_arenas_free` 在 [src/arena.c:1433-1490](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1433-L1490)：`MI_MEM_ARENA` 产地先 `mi_arena_schedule_purge`（[src/arena.c:1468-1471](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1468-L1471)），再 [src/arena.c:1474](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1474) 的 `mi_bbitmap_setN(arena->slices_free, slice_index, slice_count)` 把区间置回 1；若 setN 返回 false（说明有的位本来就是 1，即重复释放 arena 块）会报 `EAGAIN` 错误。purge 的调度细节（delay==0 立即 purge；delay>0 先在 `slices_purge` 位图登记、用 CAS 设定到期时间戳）在 [src/arena.c:2288-2312](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L2288-L2312)，到期后由 `mi_arenas_try_purge` 在一个静态 `mi_atomic_guard` 保护下逐 arena 执行（[src/arena.c:2389-2435](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L2389-L2435)），purge 时先用 `mi_bbitmap_try_clearNC` 把整段再次「暂时认领」出来防止与并发分配打架（[src/arena.c:2321-2335](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L2321-L2335)）——这与分配用的是同一套原子位图武器。

#### 4.3.4 代码实践

**实践目标**：跟踪一次真实的 arena 分配调用链（源码阅读型实践）。

**操作步骤**：

1. 在 [src/arena.c:246](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L246) 的 `if (!mi_bbitmap_try_find_and_clearN(...))` 前后各加一行 `fprintf(stderr, ...)`（学习用途，本地改完记得还原，不要提交）。
2. 用 debug 构建编译运行一个 `mi_malloc(100000)`（约 100KiB，需要 2 个 slice）的程序。
3. 对照输出顺序，在纸上画出 4.3.2 节流程图中实际走过的分支。

**需要观察的现象**：一次大分配触发一次 `mi_arena_try_alloc_at`；`slice_index` 的取值总 ≥ info slices 数（前几个 slice 是 arena 自己的元数据，永不外租）。

**预期结果**：`slice_count == 2`（`ceil(100000/65536) = 2`），第一次分配还会看到新 arena 的保留路径（`mi_arena_reserve` 被调用）。具体打印值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么「找连续 1 并原子清零」没有 ABA 问题，而经典的「free list 头插 + pop」需要特别防护？

**答案**：位图清零的成功与否由单次原子 AND 的返回值（旧值）当场判定，区间一旦被清成 0 就在 `slices_free` 中消失，其他线程不可能再找到同一段；而链表的 pop 需要读 head 再 CAS，两步之间 head 可能被改走又改回（ABA）。位图操作一步完成「读—判—改」。

**练习 2**：一个页分配请求 4MiB（大页），在 `slices_free` 里要找几个连续的 1？

**答案**：\(4\,\text{MiB} / 64\,\text{KiB} = 64\) 个，恰好等于一个机器字的位数（`MI_BFIELD_BITS`），所以走 `mi_bbitmap_try_find_and_clearNX` 路径（见 4.5 节的分派表）。

### 4.4 原子位图三层结构：bfield / bchunk / chunkmap

#### 4.4.1 概念说明

如果只用一个巨型 bit 数组表示 1GiB（16K bit），每次找空闲都要线性扫 16K 位，太慢。bitmap.h 的解法是**两级索引 + 缓存行对齐**：

- **bfield**：一个机器字（64 位）的位段，是原子操作的最小单元。所有置位/清位都归结为对它的原子 OR / 原子 AND。
- **bchunk**：512 个 bit（64 位平台 8 个 bfield），**恰好一个缓存行（64B）并对齐**。任何分配区间都不跨 chunk——这是「32MiB 最大 chunk 对象」的来源。
- **chunkmap**：也是一个 bchunk，第 i 位表示「第 i 个 chunk **可能**还有空闲位」。它是保守近似：允许误报（多扫一个空 chunk），绝不漏报。

这个设计还留了 SIMD 后门：一个 chunk 正好是一条 64B 缓存行，可以用 AVX2/NEON 一次比较全部 8 个 bfield。

#### 4.4.2 核心流程

以「找 1 个空闲 slice 并占为己有」（`mi_bchunk_try_find_and_clear_at`）为例：

```text
b = 原子加载 bfield
若 b == 0 → 本字段无货
mask = b & (~b + 1)        ← 经典位技巧：只保留最低的 1 位
old = 原子 AND (bfield, ~mask)
若 (old & mask) == mask    ← 说明这一位确实从 1→0，是我抢到的
    idx = ctz(mask)        ← 最低 1 位的位置就是 slice 下标
否则重试（最多 4 次，防活锁）
```

关键洞察：**原子 AND 的返回值就是旧值**，因此「检查旧位是 1」和「把它清 0」是一次硬件原子操作完成的——抢到与否当场见分晓。

#### 4.4.3 源码精读

bitmap.h 开头 [src/bitmap.h:15-59](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.h#L15-L59) 的注释把整套结构讲得非常清楚，重点读三段：每 bit 一个 64KiB slice、chunk 不跨分配、chunkmap 的保守语义（「可以暂时不同步，只要保证漏掉的位之后会被补上」——[src/bitmap.h:36-51](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.h#L36-L51)）。

结构定义：`mi_bfield_t`（size_t）在 [src/bitmap.h:61-71](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.h#L61-L71)；缓存行对齐的 `mi_bchunk_t` 在 [src/bitmap.h:84-87](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.h#L84-L87)（`_Atomic(mi_bfield_t) bfields[8]`）；`mi_bitmap_t`（chunkmap + 动态长度的 chunks 数组）在 [src/bitmap.h:109-114](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.h#L109-L114)。容量上限由此推导：chunkmap 自身 512 bit → 最多 512 chunk → [src/bitmap.h:103-105](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.h#L103-L105) 明确写着 `MI_BITMAP_MAX_BIT_COUNT = 16 GiB arena / 64KiB`。

掩码生成 `mi_bfield_mask(n, shift)`（[src/bitmap.c:78-84](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L78-L84)）是所有操作的基础：`((1<<n)-1) << shift`，并小心处理 `n == 64` 的移位溢出。

单 bit 原子置位/清位：[src/bitmap.c:92-107](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L92-L107)——置位 `mi_atomic_or_acq_rel`，返回是否 0→1；清位 `mi_atomic_and_acq_rel`，返回是否 1→0，并顺带报告字段是否已全 0（供 chunkmap 维护）。

**乐观清位与回补**是跨多 bit 操作的精髓：`mi_bfield_atomic_try_clear_mask_optimistic`（[src/bitmap.c:163-182](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L163-L182)）先用一次原子 AND 尝试清整个 mask，若旧值显示有些位本来是 0（没抢全），就把误清的位用原子 OR **补回去**。注释点明：在 arm64 上「乐观原子 AND/OR」比强 CAS 更划算。跨字段的 `mi_bchunk_try_clearNC` 则把这个模式扩展成「逐字推进 + `restore:` 标签统一回滚」——失败时把已清的字段全部按原 mask 置回，见 [src/bitmap.c:504-565](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L504-L565)（restore 块在 [src/bitmap.c:551-564](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L551-L564)）。

找最低位的核心实现 `mi_bchunk_try_find_and_clear_at` 在 [src/bitmap.c:594-612](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L594-L612)，即 4.4.2 节伪代码的原文；外层 `mi_bchunk_try_find_and_clear`（[src/bitmap.c:618-706](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L618-L706)）先尝试用 AVX2（整块缓存行两条 256bit load + `movemask` 一步找出非零 bfield）或 NEON 定位候选字段，再落回标量逐字段扫描，且为了防止旧版 GCC 不重载寄存器还加了编译器屏障（issue #1206 的修复痕迹）。

chunkmap 的维护协议是「先查、再清、再复查」：`mi_bitmap_chunkmap_try_clear`（[src/bitmap.c:1029-1042](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1029-L1042)）先确认 chunk 真的全空，清掉 chunkmap 位后**再查一次**——若这期间有并发置位就立刻把 chunkmap 位设回去。这正是「保守近似」的落地：宁可 chunkmap 多报，也不丢内存。

区间置位 `mi_bitmap_setN`（[src/bitmap.c:1124-1151](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1124-L1151)）按 chunk 循环调用 `mi_bchunk_setN`，每处理完一个 chunk 就 `mi_bitmap_chunkmap_set` 保守置位（先置位图后置索引，顺序保证不漏报）。线程错峰的 `cycle iterate` 宏（注释在 [src/bitmap.c:1252-1294](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1252-L1294)）让每个线程从 `tseq % cycle` 处开始扫，与 arena.c 的 `mi_arena_start_idx` 呼应。

#### 4.4.4 代码实践

**实践目标**：手推一遍位图演化，把算法变成肌肉记忆（纸面实践，无需编译）。

**操作步骤**：

1. 设一个 64 位 bfield `b = 0b0110_1111_0000_0111_1111_0000_0000_0000...`（自己编一个）。
2. 手工执行 `mi_bchunk_try_find_and_clear_at` 三轮：算出每轮的 `mask = b & -b`、原子 AND 之后的 `b`、`ctz(mask)` 得到的下标。
3. 再模拟一次「乐观清 8 位失败」：想清 `[8,16)` 这 8 个位但其中第 12 位原本是 0，写出哪几位被误清、回补 OR 的掩码是什么。

**需要观察的现象**：每轮成功清掉的是**最低**的 1 位；下标单调递增。

**预期结果**：三轮依次得到 idx = 最低 1 位的位置、次低、再次低；回补掩码 = 旧值中属于 mask 且为 1 的位（`old & mask`），与 [src/bitmap.c:175-179](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L175-L179) 一致。

#### 4.4.5 小练习与答案

**练习 1**：为什么 bchunk 必须缓存行对齐，且分配区间不许跨 chunk？

**答案**：对齐后一个 chunk 的 8 个 bfield 落在同一缓存行，跨线程对不同 slice 的原子操作只要不同 chunk 就**不产生伪共享**；不许跨 chunk 则保证一次分配的原子操作序列最多涉及一个缓存行内的 8 个字段，回滚逻辑（restore）也只需处理这一个 chunk。

**练习 2**：`mi_bfield_atomic_try_clear_mask_optimistic` 失败后为什么可以直接用原子 OR 回补，而不怕覆盖别人的并发修改？

**答案**：回补的掩码是 `old & mask`——只把「我读到时确实是 1、被我清成 0」的那些位设回 1；其他线程若在这期间把这些位改掉，OR 只会把 1 设成 1，不会破坏 0。

### 4.5 分箱位图 bbitmap：把小页和大页分开养

#### 4.5.1 概念说明

只用一张位图有个碎片隐患：小页分配（每次 1 个 slice）会把大 chunk 切得七零八落，之后想找 64 个连续 slice（4MiB 大页）就难了。mimalloc 的对策是给 `slices_free` 换上**分箱位图 bbitmap**：每个 chunk 可以被贴上一个「尺寸箱」标签（SMALL/MEDIUM/LARGE/HUGE），搜索时**优先只看目标尺寸箱的 chunk，再看完全空闲（NONE 箱）的 chunk**——小对象永远不去碰为中等对象预留的 chunk，反之亦然。这就是 bitmap.h 注释里说的 "Assigns a size class to each chunk such that small blocks don't cause too much fragmentation"。

#### 4.5.2 核心流程

按请求 slice 数分派到专用算法（[src/bitmap.h:333-341](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.h#L333-L341) 的分派器）：

| n（slice 数） | 对应页型 | 走的函数 | 位图技巧 |
| --- | --- | --- | --- |
| 1 | 小页 64KiB | `try_find_and_clear` | 找最低 1 位（可 SIMD 定位字段） |
| 8 | 中页 512KiB | `try_find_and_clear8` | 找最低的「全 1 字节」（SWAR 技巧） |
| ≤ 64 | ≤4MiB | `try_find_and_clearNX` | 单字段内移位掩码 + 跨字段拼接 |
| ≤ 512 | ≤32MiB | `try_find_and_clearNC` | 跨字段扫描 + 回滚 |
| > 512 | >32MiB | `try_find_and_clearN_` | 跨 chunk，整 chunk 起占 |

「找全 1 字节」的 SWAR 魔法（无需逐位比较）：\(h = ((\,\sim b - L_{8}\,) \wedge (b \wedge H_{8})) \gg 7\)，`L8=0x0101..`、`H8=0x8080..`——`h` 的第 k 位为 1 当且仅当 b 的第 k 字节等于 0xFF，再用 `ctz` 直接拿到字节位置，见 [src/bitmap.c:713-739](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L713-L739)。

#### 4.5.3 源码精读

尺寸箱枚举 `mi_chunkbin_t` 定义在 include/mimalloc-stats.h（SMALL=1 slice、MEDIUM=8、LARGE=一个字 64、HUGE=跨 chunk、NONE=chunk 全空闲），分类函数 `mi_chunkbin_of` 在 [src/bitmap.h:249-257](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.h#L249-L257)。`mi_bbitmap_t` 结构在 [src/bitmap.h:259-270](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.h#L259-L270)：相比普通 bitmap 多了 `chunkmap_bins[MI_CBIN_COUNT-1]`（每种尺寸箱一张 chunkmap）和 `chunk_max_accessed`（历史最高访问 chunk 下标）。

`chunk_max_accessed` 是个精巧的「热度圈」：搜索时先只扫 `[0, chunk_max_accessed]` 这个已访问过的圈（[src/bitmap.c:1805-1807](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1805-L1807)），新地圈只在老圈找不到时才开发（更新逻辑在 [src/bitmap.c:1662-1667](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1662-L1667)）——优先复用已触碰的内存，控制 RSS。

搜索主体 `mi_bbitmap_try_find_and_clear_generic`（[src/bitmap.c:1801-1884](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1801-L1884））三层循环：chunkmap 条目（外层，cycle 错峰）→ 尺寸箱（[src/bitmap.c:1826-1832](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1826-L1832) 把每张 `chunkmap_bins` 与主 chunkmap 求交得到「该箱的候选集」）→ chunk 内找区间。**箱优先序**由那个别致的 for 增量表达式控制（[src/bitmap.c:1838-1841](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1838-L1841)）：先试目标箱 `bbin`，一步跳到 NONE（完全空闲 chunk），**跳过所有其他箱**——SMALL 永不落入 MEDIUM 的 chunk。命中且恰好从 chunk 第 0 位开始、原箱为 NONE 时，这个 chunk 被「定制」给该尺寸箱（`mi_bbitmap_set_chunk_bin`，[src/bitmap.c:1638-1650](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1638-L1650)）；chunk 重新全空闲时归还 NONE 箱（[src/bitmap.c:1669-1680](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1669-L1680)）。

字段内区间查找 `mi_bchunk_try_find_and_clearNX`（[src/bitmap.c:793-849](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L793-L849)）展示了一个漂亮的推进技巧：从最低 1 位起试 `mask<<idx`，不匹配时用 `b = b & (b + (1<<idx))` **一次性跳过当前这段连续 1**（注释里的 4 行位运算示例演示了它如何吞掉 `10` 到 `1100` 的连 1），末尾还处理跨字段拼接（用 clz/ctz 量出两侧的连 1 长度）。跨多字段的 `NC` 版本（[src/bitmap.c:855-916](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L855-L916)）则先「预扫描」确认存在足够长的连 1 串、再一次性尝试清零。超大对象（>32MiB）的 `N_` 版本只认整 chunk 空闲的区间、且永远从头开始扫（[src/bitmap.c:1950-1997](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1950-L1997)）。

释放时把 slice 置回 1 用 `mi_bbitmap_setN`（[src/bitmap.c:1705-1728](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1705-L1728)），它会在 chunk 变回全 1（全空闲）时把箱子退回 NONE。分派器 `mi_bbitmap_try_find_and_clearN` 的 if 链（[src/bitmap.h:333-341](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.h#L333-L341)）注释里直接标注了 `n==1 → small pages`、`n==8 → medium pages`，与 types.h 的三档页尺寸一一对应。

#### 4.5.4 代码实践

**实践目标**：从输出反推分箱效果。

**操作步骤**：

1. 写一个程序：先分配 500 个 32 字节小块（会建立许多小页，每个占 1 slice），再分配一个 2MiB 块（需要 32 个连续 slice，LARGE 箱）。
2. 在两个阶段之间各调用一次 `mi_arenas_print()`。
3. 观察 chunk 图的行首字母：每个 chunk 行的第 5 列是尺寸箱标记（`S`/`M`/`L`/`X`，图例见 [src/arena.c:2143-2145](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L2143-L2145)）。

**需要观察的现象**：小页分配后多数 chunk 标 `S`；2MiB 分配应落在某个 `L`（或全空的 ` `，即 NONE）chunk，而不是切进 `S` chunk。

**预期结果**：能看到至少一个 `L` 行或一个全新 chunk 被启用；这验证了 4.5.3 节的箱优先序。具体图案**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：为什么 HUGE（跨 chunk）分配永远从头开始扫，而不做 cycle 错峰？

**答案**：见 [src/bitmap.c:1945-1949](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1945-L1949) 的注释：这类分配极罕见且要求整 chunk 对齐的连续空间，从头扫既降低碎片也简化实现，错峰收益可以忽略。

**练习 2**：一个 chunk 被 SMALL 箱占用后， medium 页还能用它剩下的空闲 slice 吗？

**答案**：不能（除非 chunk 回到 NONE 箱）。搜索优先序是「目标箱 → NONE」，MEDIUM 请求会跳过 SMALL 箱的 chunk。代价是少量空间闲置，收益是大页总有整 chunk 可用——这是典型的用可控浪费换低碎片。

## 5. 综合实践

把本讲三块知识（arena 尺寸体系、独占 arena、位图分配）串成一个可运行实验：**预留一个 64MiB 独占 arena，在上面建堆分配，验证分配确实落在里面**。

```c
// arena-exclusive.c —— 示例代码（依据 test/main-override-static.c:261-281 与 test/test-api.c:567-591 的用法改写）
#include <stdio.h>
#include <mimalloc.h>

int main(void) {
  // (1) 先做一次普通分配，触发默认 arena（约 1GiB）的惰性保留
  void* warm = mi_malloc(4096);

  // (2) 预留 64MiB 独占 arena：64MiB = 1024 slice = 恰好 2 个 bchunk
  mi_arena_id_t arena_id = NULL;
  int err = mi_reserve_os_memory_ex(64 * 1024 * 1024,   // size
                                    true,                // commit
                                    false,               // allow_large
                                    true,                // exclusive!
                                    &arena_id);
  if (err != 0 || arena_id == NULL) { fprintf(stderr, "reserve failed: %d\n", err); return 1; }

  // (3) 观察两个 arena 的形态（默认的 1GiB 与新的 64MiB exclusive）
  mi_arenas_print();

  // (4) 在独占 arena 上建一等堆（heap->exclusive_arena 被绑定）
  mi_heap_t* heap = mi_heap_new_in_arena(arena_id);
  void* p1 = mi_heap_malloc(heap, 128);      // 小对象：1 slice 小页
  void* p2 = mi_heap_malloc(heap, 1 << 20);  // 1MiB：16 slice

  // (5) 验证产地：p1/p2 应在独占 arena 内，warm 应不在
  printf("p1 in exclusive arena : %d\n", mi_arena_contains(arena_id, p1));
  printf("p2 in exclusive arena : %d\n", mi_arena_contains(arena_id, p2));
  printf("warm in exclusive     : %d\n", mi_arena_contains(arena_id, warm));

  size_t area_size = 0;
  void* area = mi_arena_area(arena_id, &area_size);
  printf("exclusive arena area  : [%p, %p) = %zu KiB\n",
         area, (char*)area + area_size, area_size / 1024);

  // (6) 清理：先释放对象，再删堆（heap 的页归还 arena，arena 保留）
  mi_free(p1); mi_free(p2);
  mi_heap_delete(heap);
  mi_free(warm);
  return 0;
}
```

编译与运行：

```bash
gcc arena-exclusive.c -o arena-exclusive \
    -I<仓库路径>/include -L<仓库路径>/out/release -lmimalloc
LD_LIBRARY_PATH=<仓库路径>/out/release ./arena-exclusive
# 或者用 verbose 观察 arena 保留日志：
MIMALLOC_VERBOSE=1 LD_LIBRARY_PATH=<仓库路径>/out/release ./arena-exclusive
```

**验证要点与预期结果**：

1. `mi_arenas_print()` 应打印出两个 arena：默认的一个（约 16384 slices / 1024 MiB）和新的 `... , exclusive` 的一个（1024 slices / 64 MiB）；verbose 模式下还能看到 `reserved 65536 KiB memory` 字样（出自 [src/arena.c:1903](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1903)）。
2. `mi_arena_contains` 对 p1、p2 返回 1，对 warm 返回 0——因为 `heap->exclusive_arena` 使 `mi_arena_is_suitable` 拒绝了一切其他 arena（[src/arena.c:48-54](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L48-L54)），而 `mi_arena_contains` 只是做地址区间判断（[src/arena.c:1528-1533](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1528-L1533)）。
3. `mi_arena_area` 报告的区间应恰好是 65536 KiB，且 p1、p2 落在该区间内。
4. 注意 `mi_heap_new_in_arena` 内部会先调用 `mi_thread_init()` 保证主堆已存在（[src/heap.c:150-155](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/heap.c#L150-L155)），所以它作为进程第一个 mimalloc 调用也是安全的。

以上现象的精确数值（arena 个数、地址）依赖平台与构建类型，**待本地验证**。

## 6. 本讲小结

- arena 是 mimalloc 向 OS **批发**内存的单位：slice 64KiB 是零售刻度，bchunk 32MiB 是位图与碎片的隔离单位，单个 arena 32MiB–16GiB，默认保留增量 1GiB 并按 \(2^{\lfloor n/8 \rfloor}\) 指数扩张，全进程上限 `MI_MAX_ARENAS = 160`。
- `mi_arena_t` 自带四张位图（free/committed/dirty/purge）且元数据就放在自己内存开头的 info slices 里；**free 位图 set=1 表示空闲**，分配是找连续 1 并清零。
- 分配链路 `_mi_arenas_alloc_aligned → mi_arenas_try_alloc → mi_arena_try_alloc_at` 的核心是 `mi_bbitmap_try_find_and_clearN`：**查找与占有合并为一次原子操作**，无锁、免 ABA，失败即回滚（restore 路径）。
- 位图三层结构（bfield 原子字 / bchunk 缓存行 / chunkmap 保守索引）+ 乐观原子 AND/OR + SIMD 扫描，共同支撑跨线程高频切分；chunkmap 只许多报不许漏报。
- `slices_free` 用**分箱位图**：chunk 按尺寸箱（SMALL/MEDIUM/LARGE/HUGE）标签化，搜索只看目标箱与全空箱，用可控的闲置换大页的低碎片；`chunk_max_accessed` 优先复用已触碰区域以压 RSS。
- 用户可用 `mi_reserve_os_memory_ex`(exclusive) / `mi_manage_os_memory_ex` 自建 arena，用 `mi_heap_new_in_arena` 绑定堆，用 `mi_arena_contains` / `mi_arena_area` / `mi_arenas_print` 观测与验证。

## 7. 下一步学习建议

- **u6-l4（abandon 与 reclaim）**：本讲已多次出现 `arena_pages->pages_abandoned[bin]` 位图与 `mi_bitmap_try_find_and_claim`——下一讲讲线程退出时页面如何被「遗弃」进这些位图、又被其他线程按 size class 认领。
- **u8-l1（原子抽象与 mi_lock）**：本讲的原子 OR/AND/CAS 都来自 `include/mimalloc/atomic.h`；想彻底搞清 acquire/release 语义与 `mi_lock` 自旋锁，去读那一讲。
- **u8-l3（位图内部）**：本讲只精读了 `try_find_and_clear` 家族；`_mi_bitmap_forall_setc_rangesn`（purge 用的按对齐区间遍历）与 SIMD 细节留待那一讲展开。
- 顺手复习 **u3-l4（page map）**：arena 的 256MiB 对齐（`MI_ARENA_ALIGNMENT`）正是那里「纯算术定位页元数据」的前提，两讲互为表里。
