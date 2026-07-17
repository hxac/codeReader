# 虚拟内存管理

## 1. 本讲目标

上一讲（u12-l1）我们看清了内核如何启动、如何用 `trap_entry` 接住异常、如何派发系统调用；再往前的 u7-l1 我们看清了硬件那套**软件管理 TLB**——硬件只查表、缺失就 trap，由软件读页表并 `dtlbinsert`。但那两讲都留下了一个核心问题没有回答：

> 当一条访存指令访问的虚拟地址**根本还没有物理页**（页表项 `present=0`）时，是谁去「分配一页物理内存、把内容填好、再把映射建立起来」？

答案就是本讲的主角——内核的**虚拟内存子系统（vm subsystem）**。它由 `software/kernel/` 下五个 `vm_*.c` 文件加上 `slab.c` 组成，是 Nyuzi 微内核里代码量最大、概念最密集的一块。

学完本讲，你应当能够：

1. 说清内核如何用**两级页表**把 32 位虚拟地址翻译成物理地址，并理解 `vm_translation_map` 如何封装这套硬件机制、如何用 ASID 区分多个地址空间。
2. 理解 `vm_address_space` 如何用「区域（area）+ 地址空间」抽象管理一段段虚拟内存，以及**缺页中断**从 trap 进入内核、经 `soft_fault` 分配物理页、回填页表项的完整链路。
3. 掌握 `vm_page`（物理页帧分配）与 `vm_cache`（页缓存 / 写时复制源链）这对搭档，理解 OS 为什么「把物理内存看作后端存储的缓存」。
4. 理解 `slab` 分配器如何为固定大小的内核对象（地址空间、翻译表、cache）做高效的「分块」分配，以及它为何**故意不是教科书式的 slab**。

## 2. 前置知识

在进入源码前，先用大白话把几个关键概念讲透。本讲假定你已经读过 u7-l1（TLB 与翻译）和 u12-l1（内核启动与 trap）。

### 2.1 为什么要虚拟内存

如果每个用户程序都直接用物理地址，那么两个程序就不能都用地址 `0x10000`——它们会踩到同一块物理内存。虚拟内存的解法是：给每个程序一套**独立的虚拟地址空间**，再用一张**页表（page table）**把虚拟地址翻译成物理地址。翻译由硬件 MMU 每次访存时自动完成；翻译所需的映射关系由内核建立。

页是翻译的最小单位。Nyuzi 页大小为 4 KiB：

\[ \text{PAGE\_SIZE} = 2^{12} = 4096 \text{ 字节} \]

这意味着虚拟地址的低 12 位是**页内偏移**，翻译时直接照搬，只有高位才需要查表。

### 2.2 两级页表

32 位地址空间若用一张大表，每页一项、每项 4 字节，需要 \(2^{20}\) 项 = 4 MiB——每个进程光页表就要 4 MiB，太浪费。解法是**分级**：把页表拆成「页目录 → 页表」两级。

Nyuzi 把 32 位虚拟地址切成三段：

\[ \underbrace{\text{bit 31..22}}_{\text{页目录索引 (10 bit)}} \; \underbrace{\text{bit 21..12}}_{\text{页表索引 (10 bit)}} \; \underbrace{\text{bit 11..0}}_{\text{页内偏移 (12 bit)}} \]

于是：

- **页目录**：\(2^{10} = 1024\) 项，每项 4 字节，正好占 1 页（4 KiB）。每项指向一张页表（或标记「不存在」）。
- **页表**：每张也是 1024 项 × 4 字节 = 4 KiB = 1 页，覆盖 \(1024 \times 4\text{ KiB} = 4\text{ MiB}\) 虚拟地址。

只有实际用到的 4 MiB 区段才需要分配对应的页表，从而省内存。u7-l1 里那个汇编 `tlb_miss_handler` 手工走的「页目录 → 页表」就是这个两级结构。

### 2.3 两种「找不到」要分开

这是本讲最容易混淆、也是最关键的一点。硬件访存时会遇到两种「找不到」，对应两种 trap（见 `asm.h`）：

| Trap 类型 | 编号 | 含义 | 谁来处理 |
|---|---|---|---|
| `TT_TLB_MISS` | 7 | 页表项**存在且 present=1**，只是没被缓存进 TLB | 汇编 `tlb_miss_handler`，读 PTE 后 `dtlbinsert` |
| `TT_PAGE_FAULT` | 6 | 页表项**present=0**（页还没分配），或访问越权 | C 语言 `handle_page_fault` → `soft_fault` |

u7-l1 讲的是 `TT_TLB_MISS` 那条快路径。本讲讲的是 `TT_PAGE_FAULT` 这条**慢路径**——它要真正去分配物理内存、读文件、做写时复制，最后把 present 位置 1。处理完之后，CPU 重新执行那条指令，这次才会先触发一次 `TT_TLB_MISS`，由汇编 handler 把新映射插进 TLB，然后再命中。

> 所以实践任务里说的「建立页表项并插入 TLB」其实是**两个 trap 的接力**：本讲的 C 代码负责「写页表项（`vm_map_page`）」，随后一次 `TT_TLB_MISS` 负责真正「插入 TLB（`dtlbinsert`）」。务必把这两步分开理解。

### 2.4 物理内存是「后端存储的缓存」

`vm_cache.h` 开头有一句点睛之笔：*The OS treats physical memory as a cache for some backing store.*（操作系统把物理内存当作某种后端存储的缓存）。换句话说，一个物理页里的数据，其「权威来源」可能是：

- 一个磁盘文件（程序刚加载，页内容要从 ELF 读进来）；
- 另一个 cache（写时复制：fork 时父子共享同一页，谁要写谁就复制一份）；
- 纯零（匿名页，第一次访问分配全零页）。

`vm_cache` 就是「一组物理页 + 它们的来源」的抽象。这套设计直接借鉴自 Haiku/BeOS 的 VM，理解它就能理解 `soft_fault` 里那条沿着 `cache->source` 链向上找页的循环。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `software/kernel/` 下：

| 文件 | 作用 | 对应最小模块 |
|---|---|---|
| `vm_address_space.c` / `.h` | 地址空间与区域（area）抽象、缺页处理入口 `handle_page_fault`、慢路径 `soft_fault` | 地址空间 |
| `vm_translation_map.c` / `.h` | 封装硬件翻译：两级页表读写、ASID 分配、地址空间切换、内核页表共享 | 页表翻译 |
| `vm_page.c` / `.h` | 物理页帧分配（bump + free list）、引用计数、`pa_to_page` 反查 | 物理页 |
| `vm_cache.c` / `.h` | 页缓存：cache 对象、按 (cache, offset) 哈希查页、引用计数、写时复制源链 | 页缓存 |
| `slab.c` / `.h` | 固定大小对象的分块分配器（地址空间、翻译表、cache 都从这里来） | slab 分配 |
| `trap.c` | 把 `TT_PAGE_FAULT` 派发到 `handle_page_fault`（承自 u12-l1） | 综合实践 |
| `trap_entry.S` | `tlb_miss_handler`：读 PTE 并 `dtlbinsert`（承自 u7-l1） | 综合实践 |
| `memory_map.h` | 内核虚拟地址布局常量（`KERNEL_BASE` 等） | 前置 |
| `asm.h` | 控制寄存器号、trap 类型、`MAX_ASIDS` | 前置 |

一个有用的全局认知：内核启动时这几个模块的初始化**顺序**揭示了它们的依赖关系。在 [software/kernel/main.c:43-47](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/main.c#L43-L47) 里，`kernel_main` 依次调用：

1. `vm_page_init` — 先建好物理页帧簿记；
2. `vm_translation_map_init` — 再拿到页目录；
3. `boot_init_heap` — 在页结构之后初始化内核堆（slab 的底座）；
4. `vm_address_space_init` — 建内核地址空间；
5. `bootstrap_vm_cache` — 建 cache 哈希表。

这个顺序不是随意的——后一个模块往往要调用前一个模块，我们会在各模块里看到原因。

## 4. 核心概念与源码讲解

### 4.1 地址空间（vm_address_space）

#### 4.1.1 概念说明

**地址空间（address space）**是「一张页目录 + 它管辖的所有虚拟内存区域」的集合。每个用户进程有自己的地址空间，内核有一个全局的内核地址空间。同一个虚拟地址 `0x400000` 在进程 A 和进程 B 里可以映射到完全不同的物理页——隔离就是这么来的。

地址空间里又把虚拟地址划分成一段段**区域（area）**。一个 area 是一段连续的虚拟地址，有统一的属性（可读/可写/可执行/是否常驻）和统一的数据来源（挂在哪个 `vm_cache` 上）。比如一个进程的代码段是一个 area，堆是另一个 area，栈又是另一个。

`vm_address_space` 结构体非常紧凑：

```c
struct vm_address_space
{
    struct rwlock mut;                  // 保护区域映射的读写锁
    struct vm_area_map area_map;        // 本空间内所有 area 的集合
    struct vm_translation_map *translation_map;  // 指向硬件翻译表（页目录）
};
```

引自 [software/kernel/vm_address_space.h:24-29](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/vm_address_space.h#L24-L29)。注意它把「逻辑层（area_map）」和「硬件层（translation_map）」组合在一起——area 回答「这段地址该有什么」，translation_map 回答「怎么真正写进页表」。

`vm_area` 则描述单个区域（[software/kernel/vm_area_map.h:37-47](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/vm_area_map.h#L37-L47)）：`low_address/high_address` 圈定范围，`cache + cache_offset + cache_length` 说明数据来源，`flags`（`AREA_WIRED`/`AREA_WRITABLE`/`AREA_EXECUTABLE`）描述属性。

#### 4.1.2 核心流程

地址空间层最核心的流程是**缺页处理**。当硬件检测到 `present=0`，trap 进内核，最终调到 `handle_page_fault(address, is_store)`：

```text
硬件检测 present=0 → TT_PAGE_FAULT
   → trap_entry.S 保存现场，trap.c::handle_trap 按 trap_cause 派发
   → handle_page_fault(address, is_store)
        1. 按 address 范围选地址空间（内核空间 or 当前进程）
        2. 在 area_map 里 lookup_area(address)
           - 找不到 area → 该地址没映射 → 段错误（bad_fault）
        3. 调 soft_fault(space, area, address, is_store)  ← 真正干活
   → 返回用户态，CPU 重新执行那条指令
        → 这次先 TT_TLB_MISS → tlb_miss_handler → dtlbinsert → 命中
```

创建一个用户地址空间的过程则对称：`create_address_space` 分配一个 `vm_address_space`、初始化它的 area_map（用户区从 `PAGE_SIZE` 到 `KERNEL_BASE-1`）、并为它**新建一张翻译表**（`create_translation_map`，这会复制内核页目录项、分配 ASID——见 4.2）。

#### 4.1.3 源码精读

先看缺页入口 `handle_page_fault`：

```c
int handle_page_fault(unsigned int address, int is_store)
{
    struct vm_address_space *space;
    const struct vm_area *area;
    ...
    if (address >= KERNEL_BASE)
        space = &kernel_address_space;          // 内核空间用全局内核地址空间
    else
        space = current_thread()->proc->space;  // 用户空间用当前进程的

    rwlock_lock_read(&space->mut);
    area = lookup_area(&space->area_map, address);
    if (area == 0) { result = 0; goto error1; } // 该地址没有 area → 段错误

    result = soft_fault(space, area, address, is_store);
    ...
}
```

引自 [software/kernel/vm_address_space.c:213-238](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/vm_address_space.c#L213-L238)。两个要点：第一，用 `address >= KERNEL_BASE`（`KERNEL_BASE = 0xc0000000`）来区分这是内核缺页还是用户缺页，因为内核地址空间全局唯一；第二，这里只加**读锁**（`rwlock_lock_read`）——多个线程可以同时处理各自缺页，只有修改 area_map 结构才需要写锁。

`handle_page_fault` 由 `trap.c` 派发，入口在 [software/kernel/trap.c:122-136](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap.c#L122-L136)：`TT_PAGE_FAULT` 和 `TT_ILLEGAL_STORE` 都走这里，`is_store` 由 `trap_cause` 的 bit4（`0x10`，承自 u7-l3 的 trap_cause 标志位）决定。如果 `handle_page_fault` 返回 0（处理失败），就跳到 `bad_fault`——用户态会因此杀线程，内核态直接 panic。

再看内核地址空间怎么初始化的——它揭示了内核虚拟地址布局。`vm_address_space_init` 在 [software/kernel/vm_address_space.c:39-61](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/vm_address_space.c#L39-L61) 一次性创建若干个**常驻（`AREA_WIRED`）**区域：

```c
init_area_map(amap, KERNEL_BASE, 0xffffffff);
create_vm_area(amap, KERNEL_BASE, KERNEL_END - KERNEL_BASE, PLACE_EXACT,
               "kernel", AREA_WIRED | AREA_WRITABLE | AREA_EXECUTABLE);
create_vm_area(amap, PHYS_MEM_ALIAS, memory_size, PLACE_EXACT,
               "memory alias", AREA_WIRED | AREA_WRITABLE);
create_vm_area(amap, KERNEL_HEAP_BASE, KERNEL_HEAP_SIZE, PLACE_EXACT,
               "kernel_heap", AREA_WIRED | AREA_WRITABLE);
create_vm_area(amap, DEVICE_REG_BASE, 0x10000, PLACE_EXACT,
               "device registers", AREA_WIRED | AREA_WRITABLE);
```

配合 [software/kernel/memory_map.h:21-28](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/memory_map.h#L21-L28) 的常量，可以画出内核地址布局：

| 虚拟地址区间 | 名称 | 用途 |
|---|---|---|
| `0xc0000000` ~ `KERNEL_END` | kernel | 内核代码/数据，映射到物理 0 |
| `0xc1000000` ~ | memory alias | 物理内存的「平铺镜像」（`PA_TO_VA` 就靠它） |
| `0xd0000000` ~ | kernel_heap | 内核堆（slab 底座、vm_page 结构数组也在这） |
| `0xffff0000` ~ | device registers | MMIO 外设寄存器 |
| `0xfffe0000` ~ | initial kernel stacks | 各硬件线程的内核栈 |

注意这些 area 都带 `AREA_WIRED`（常驻）。常驻区在 `create_area`（[software/kernel/vm_address_space.c:119-127](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/vm_address_space.c#L119-L127)）里会**立即**对每一页调 `soft_fault` 提前建立映射，从而「永不在运行中缺页」——这正是设备寄存器、内核栈这类不能容忍缺页延迟的对象所要求的。

#### 4.1.4 代码实践

**实践目标**：把「缺页 → 找 area → 软件建映射」这条链路在源码里走通。

**操作步骤**：

1. 打开 [software/kernel/vm_address_space.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/vm_address_space.c)，定位 `handle_page_fault`（213 行起）。
2. 注意第 219 行的 `if (address >= KERNEL_BASE)` 分支：思考一个用户程序访问空指针（地址 0）会发生什么——它会走 `else` 分支拿到进程自己的 space，`lookup_area(0)` 找不到任何 area（用户区从 `PAGE_SIZE` 起），返回 0，最终 `bad_fault` 杀掉线程。
3. 跟进 `soft_fault`（245 行起），这是本讲的「重头戏」，4.3 节会细讲。
4. 对比 `create_area`（94 行起）里 `AREA_WIRED` 分支（119 行）：它在一个 for 循环里对 area 内**每一页**主动调用 `soft_fault(..., 1)`。这说明常驻区的页在 `create_area` 返回时就已经全部映射好了。

**需要观察的现象**：常驻区（如内核代码、设备寄存器）在内核启动时就一次性映射完，运行期不再缺页；而非常驻的用户区则是「按需分页」——第一次访问才进 `handle_page_fault`。

**预期结果**：你能用一句话解释「为什么访问一个没有 area 覆盖的地址会段错误，而访问一个有 area 但没物理页的地址只是触发一次可恢复的缺页」。

（本实践为源码阅读型，无需运行；如需运行验证，可构建内核后在模拟器里跑一个故意解引用空指针的用户程序，观察 `user space thread N crashed` 输出，对应 `trap.c` 的 `bad_fault`。）

#### 4.1.5 小练习与答案

**练习 1**：`handle_page_fault` 为什么用读锁 `rwlock_lock_read` 而不是写锁？

**参考答案**：因为缺页处理大多数情况下只是**读取** area_map（`lookup_area` 查找）和给页表加映射（`vm_map_page`，它有自己的细粒度锁，见 4.2），并不修改 area_map 的结构。用读锁能让多个线程并发处理各自的缺页，提高并行度；只有 `create_area`/`destroy_area` 这类增删 area 的操作才需要写锁。

**练习 2**：一个新进程调用 `create_address_space` 后，它的内核地址（`>= 0xc0000000`）能正常访问吗？为什么？

**参考答案**：能。`create_address_space` 调 `create_translation_map`，后者会把内核页目录项（索引 768..1023）从全局 `kernel_map` 复制到新页目录（见 4.2.3）。所以每个用户进程的页目录都**共享同一套内核页表**，内核空间对所有进程可见且一致。

---

### 4.2 页表翻译（vm_translation_map）

#### 4.2.1 概念说明

`vm_translation_map` 是对「硬件 MMU + 两级页表 + ASID」这套机制的软件封装。它的核心字段（[software/kernel/vm_translation_map.h:35-41](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/vm_translation_map.h#L35-L41)）：

```c
struct vm_translation_map
{
    struct list_node list_entry;   // 串在全局 map_list 上（为共享内核页表）
    spinlock_t lock;               // 保护本翻译表的页表操作
    unsigned int page_dir;         // 页目录的【物理】地址
    int asid;                      // 本地址空间的 ASID
};
```

三个关键点：`page_dir` 存的是**物理地址**（因为要写进控制寄存器 `CR_PAGE_DIR_BASE`）；`asid` 是地址空间标识符（承自 u7-l1，让多个地址空间共享 TLB 而不串扰）；`list_entry` 把所有翻译表串成一条链，这是实现「内核页表全局共享」的钥匙。

页表项（PTE）的低 5 位是属性标志（[software/kernel/vm_translation_map.h:29-33](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/vm_translation_map.h#L29-L33)），与 u7-l1 硬件侧 `tlb_entry_t` 的属性位**完全一致**（这是协同仿真能成立的基础）：

| 位 | 宏 | 含义 |
|---|---|---|
| 0 | `PAGE_PRESENT` | 存在（present=0 就是缺页） |
| 1 | `PAGE_WRITABLE` | 可写 |
| 2 | `PAGE_EXECUTABLE` | 可执行 |
| 3 | `PAGE_SUPERVISOR` | 仅内核态可访问 |
| 4 | `PAGE_GLOBAL` | 全局映射（跨 ASID，见 u7-l1） |

高位则是物理页号（即物理地址的高 20 位，因为低 12 位页内偏移照搬）。

#### 4.2.2 核心流程

**写入一个映射（`vm_map_page`）** 是这一层最核心的操作。给定虚拟地址 `va` 和「物理地址 | 属性」值 `pa`：

```text
vpindex  = va / PAGE_SIZE
pgdindex = vpindex / 1024        // 页目录项下标
pgtindex = vpindex % 1024        // 页表项下标

if va 是内核空间 (>= KERNEL_BASE):
    取全局 kernel_map.page_dir
    若该页目录项不存在 → 分配一页新页表，【写入所有翻译表的页目录】(共享!)
    写页表项 pgtbl[pgtindex] = pa
else:  // 用户空间
    取本 map->page_dir
    若该页目录项不存在 → 分配一页新页表
    写页表项 pgtbl[pgtindex] = pa
tlbinval va   // 失效该 va 的 TLB 项（重要：保证下次访问重新查表）
```

注意 `tlbinval` 这一步：它**不插入**新的 TLB 项，只是把可能存在的旧项清掉。新映射真正进 TLB 要等到下一次访问触发的 `TT_TLB_MISS`——这就是 2.3 节强调的「两个 trap 接力」。

**切换地址空间（`switch_to_translation_map`）** 极简：往控制寄存器 `CR_PAGE_DIR_BASE`（10）写新页目录物理地址，往 `CR_CURRENT_ASID`（9）写新 ASID。

#### 4.2.3 源码精读

先看 `vm_map_page` 中「内核页表共享」的精妙之处（[software/kernel/vm_translation_map.c:199-226](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/vm_translation_map.c#L199-L226)）：

```c
if (va >= KERNEL_BASE)
{
    old_flags = acquire_spinlock_int(&kernel_space_lock);
    pgdir = (unsigned int*) PA_TO_VA(kernel_map.page_dir);
    if ((pgdir[pgdindex] & PAGE_PRESENT) == 0)
    {
        new_pgt = page_to_pa(vm_allocate_page()) | PAGE_PRESENT;
        list_for_each(&map_list, other_map, struct list_node)
        {
            pgdir = (unsigned int*) PA_TO_VA(((struct vm_translation_map*)other_map)->page_dir);
            pgdir[pgdindex] = new_pgt;        // 把新页表登记到【每个】地址空间
        }
    }
    pgtbl = (unsigned int*) PAGE_ALIGN(pgdir[pgdindex]);
    ((unsigned int*)PA_TO_VA(pgtbl))[pgtindex] = pa;
    __asm__("tlbinval %0" : : "s" (va));
    release_spinlock_int(&kernel_space_lock, old_flags);
}
```

这段解决了「内核页表只有一份，但每个进程都有自己的页目录」的矛盾：因为内核页目录项（768..1023）在所有进程的页目录里都指向**同一张**页表（`create_translation_map` 复制的就是这些指针），所以内核要新分配一张页表时，必须把这张新页表的指针**同时写进所有进程的页目录**——这就是遍历 `map_list` 的目的。用户空间映射则不需要这样，因为用户页表是各进程私有的（见 `else` 分支，227-242 行）。

`create_translation_map`（[software/kernel/vm_translation_map.c:141-162](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/vm_translation_map.c#L141-L162)）正好印证这点：

```c
map->page_dir = page_to_pa(vm_allocate_page());
old_flags = acquire_spinlock_int(&kernel_space_lock);
// 把内核页目录项（768..1023，共 256 项）复制到新页目录
memcpy((unsigned int*) PA_TO_VA(map->page_dir) + 768,
       (unsigned int*) PA_TO_VA(kernel_map.page_dir) + 768,
       256 * sizeof(unsigned int));
map->asid = bitmap_alloc(asid_alloc, MAX_ASIDS);   // 分配一个 ASID
list_add_tail(&map_list, (struct list_node*) map);
```

为什么是 `+ 768`、复制 `256` 项？因为 `KERNEL_BASE = 0xc0000000`，对应的页目录下标是：

\[ \frac{0\text{x}c0000000}{4\text{KiB}} \div 1024 = \frac{0\text{x}c0000}{1024} = 768 \]

从 768 到 1023 正好是地址空间最高的 1 GiB（32 位空间的 1/4），全部留给内核。ASID 则用一张位图 `asid_alloc`（\( \lceil 64/32 \rceil = 2 \) 个 `unsigned int`，承自 `MAX_ASIDS = 64`）来分配/回收。

启动期的页表构建（MMU 关闭时）在 `boot_setup_page_tables`（[software/kernel/vm_translation_map.c:89-131](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/vm_translation_map.c#L89-L131)）与 `boot_vm_map_pages`（[software/kernel/vm_translation_map.c:59-87](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/vm_translation_map.c#L59-L87)）。注意文件开头 28-33 行的注释：这些 `boot_*` 函数在 MMU 关闭时运行，跑在物理地址上而非内核链接的虚拟地址，所以**不能用全局变量、不能用 switch**——状态全塞进一个手工传递的 `struct boot_page_setup`。`vm_translation_map_init`（134-139 行）在 MMU 打开后才读 `CR_PAGE_DIR_BASE`（控制寄存器 10）把这张启动页目录「认领」为 `kernel_map`。

地址空间切换是本层最短的函数（[software/kernel/vm_translation_map.c:292-299](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/vm_translation_map.c#L292-L299)）：

```c
void switch_to_translation_map(struct vm_translation_map *map)
{
    __builtin_nyuzi_write_control_reg(CR_PAGE_DIR_BASE, map->page_dir);
    __builtin_nyuzi_write_control_reg(CR_CURRENT_ASID, map->asid);
}
```

这两条写控制寄存器（承自 u2-l4 的 `setcr`）就完成了「换地址空间」——它会在 u12-l3 的上下文切换里被调用。

#### 4.2.4 代码实践

**实践目标**：手工验算两级页表的下标，理解 `vm_map_page` 的索引计算。

**操作步骤**：

1. 假设要映射虚拟地址 `va = 0x00401000`（一个典型的用户程序入口附近的页）。
2. 按源码公式计算：`vpindex = 0x00401000 / 0x1000 = 0x401`；`pgdindex = 0x401 / 1024 = 1`；`pgtindex = 0x401 % 1024 = 0x401`。
3. 对照 `boot_vm_map_pages`（59-87 行）确认：它先用 `pgdir[pgdindex]` 找到页表，再 `pgtbl[pgtindex] = pa | flags` 写入。与你手算的下标一致。
4. 再算一个内核地址 `va = 0xc0100000`：`vpindex = 0xc0100`，`pgdindex = 0xc0100/1024 = 768`（正好落在内核共享区起点），说明它走 `vm_map_page` 的 `if (va >= KERNEL_BASE)` 分支。

**需要观察的现象 / 预期结果**：手算的 `pgdindex` 对内核地址都 `>= 768`，对用户地址都 `< 768`，与「内核页目录项 768..1023 全局共享、用户项私有」的设计吻合。

（纯算术实践，可直接在纸面或计算器完成。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 `vm_map_page` 写完页表项后要执行 `tlbinval va`？如果删掉会怎样？

**参考答案**：`tlbinval` 失效该虚拟地址在 TLB 里的旧映射。如果删掉，TLB 里可能还残留**旧**的翻译（比如同一 va 以前映射到别的物理页，或 present=0 的空项），CPU 会继续用旧映射而看不到新写入的页表项，导致读到脏数据或反复缺页却「修不好」。失效后，下次访问必然重新查表（`TT_TLB_MISS`），从而拿到最新映射。

**练习 2**：`create_translation_map` 里 `bitmap_alloc(asid_alloc, MAX_ASIDS)` 的 ASID 用完了（64 个进程同时存在）会怎样？

**参考答案**：看代码注释（294-295 行），当前实现有个 `XXX`：若 map 数量超过 ASID 数且本 map 没分到 ASID，理应「偷」一个别人的 ASID（并作废其 TLB），但这个逻辑**尚未实现**。`MAX_ASIDS = 64`，超过 64 个地址空间时 `bitmap_alloc` 的行为未在本讲覆盖的代码里处理（待本地确认其返回值与后续影响），这是内核一个已知的简化点。

---

### 4.3 物理页与页缓存（vm_page / vm_cache）

这两个模块是搭档，放在一起讲：`vm_page` 管「物理页帧本身」，`vm_cache` 管「物理页里的内容从哪来、归谁所有」。本节最后给出 `soft_fault` 的完整精读——它把这一切串起来。

#### 4.3.1 概念说明

**`vm_page`：物理页帧的账本。** 系统里每一个 4 KiB 物理页帧都对应一个 `vm_page` 结构（[software/kernel/vm_page.h:36-45](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/vm_page.h#L36-L45)）：

```c
struct vm_page
{
    struct list_node list_entry;   // 挂在 free list 或 cache 的 page_list 上
    struct list_node hash_entry;   // 挂在 vm_cache 的哈希桶上
    unsigned int cache_offset;     // 本页在所属 cache 中的偏移
    struct vm_cache *cache;        // 所属 cache（0 表示空闲）
    volatile int busy;             // 正在从磁盘读入？忙等
    int dirty;                     // 脏页？
    volatile int ref_count;        // 引用计数（映射次数）
};
```

注意一个反直觉的设计：所有 `vm_page` 结构**不是用 slab 分配的**，而是在内核堆起始处预分配一大块连续数组（见 4.3.3）。注释说得很直白：「因为页结构本身和堆扩张之间存在循环依赖」——堆要扩页就得调页分配器，页分配器又依赖页结构数组，所以页结构数组必须先于堆存在。

`vm_page` 和物理地址之间是一一对应的，靠下标算术互转：

\[ \text{pa} = (\text{page} - \text{pages}) \times \text{PAGE\_SIZE}, \qquad \text{page} = \&\text{pages}[\text{pa} / \text{PAGE\_SIZE}] \]

**`vm_cache`：页缓存 / 后端存储抽象。** 如 2.4 节所述，OS 把物理内存看作后端存储的缓存。`vm_cache`（[software/kernel/vm_cache.h:27-33](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/vm_cache.h#L27-L33)）就是「一组属于同一来源的物理页」：

```c
struct vm_cache
{
    struct list_node page_list;    // 本 cache 拥有的所有页
    struct file_handle *file;      // 后端：磁盘文件（可为 0）
    volatile int ref_count;        // 引用计数
    struct vm_cache *source;       // 写时复制的【源】cache
};
```

`source` 指针串成一条链，这是**写时复制（Copy-On-Write, COW）**的基础：一个 cache 可以「继承」另一个 cache 的页，写之前共享、写的时候才复制。

页在 cache 里是按 `(cache, cache_offset)` 二元组索引的。`vm_cache.c` 用一张**哈希表**（37 个桶）来快速查找「某个 cache 的某个偏移上有没有页」：

\[ \text{bucket} = \big( (\text{cache 地址}) + (\text{offset} / \text{PAGE\_SIZE}) \big) \bmod 37 \]

#### 4.3.2 核心流程

**分配一个物理页（`vm_allocate_page`）**：从空闲链表摘一个 `vm_page`，清零引用计数为 1，把对应物理内存清零（`memset`），返回。引用计数 `ref_count` 用原子操作 `__sync_fetch_and_add` 增减，因为同一页可能被多个虚拟地址映射（如 COW 共享）。

**缺页慢路径（`soft_fault`）** 是本讲最复杂、也最能体现「内存是缓存」思想的函数。给定缺页地址，它要决定「这一页的内容从哪来」：

```text
soft_fault(space, area, address, is_store):
  禁中断 + lock_vm_cache()
  沿 cache 链向上找（area->cache → cache->source → ...）:
    在当前 cache 里 lookup_cache_page(cache, cache_offset)
    若找到页 source_page → 跳出
    若该 cache 有 file 后端:
        vm_allocate_page() 分配新页
        标 busy=1，先 insert_cache_page（占位，防 collided fault）
        释放锁 + 开中断
        read_file(...) 从磁盘读入这页  ← 可能阻塞
        重新加锁，busy=0
        跳出
    否则（无 file，继续向上找）:
        标记 is_cow_page = 1
        在顶层 cache 插一个 dummy 占位页（busy=1）防 collided fault
  解析结果:
    若整条链都没页 → 用 dummy 页（匿名零页）
    若 source_page 来自别的 cache（is_cow_page）且是写操作:
        memcpy 把源页内容复制到 dummy 页（真正的「写时复制」）
        source_page = dummy 页
    若是读操作 + COW:
        直接只读映射源页（不复制，dummy 页撤掉）
  inc_page_ref(source_page)
  等待 busy 清零（页可能还在从磁盘读）
  计算页属性（present | writable | executable | supervisor/global）
  vm_map_page(...) 把这页写进页表
  返回 1（成功）
```

关键巧思：

1. **`busy` 位防 collided fault**：当一个线程正在从磁盘读一页时，先把页插入 cache 并置 `busy=1`；若同时另一个线程对同一地址缺页，它会查到这个 busy 页并等待，而不是重复读磁盘。
2. **写时复制延迟到「写」**：多个进程共享同一只读页，只有真正写入时才 `memcpy` 出一份私有副本，省内存。
3. **dirty 位与可写位的配合**（源码 411-413 行）：干净的页即使 area 可写也先**不**给 `PAGE_WRITABLE`，从而下次写会再缺页，内核借机把 `dirty` 位置 1——这是一种廉价的脏页跟踪。

#### 4.3.3 源码精读

先看物理页分配与引用计数（[software/kernel/vm_page.c:51-74](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/vm_page.c#L51-L74)）：

```c
struct vm_page *vm_allocate_page(void)
{
    ...
    page = list_remove_head(&free_page_list, struct vm_page);
    if (page == 0) panic("Out of memory!");
    page->busy = 0; page->cache = 0; page->dirty = 0; page->ref_count = 1;
    ...
    pa = (page - pages) * PAGE_SIZE;          // 下标算术转物理地址
    memset((void*) PA_TO_VA(pa), 0, PAGE_SIZE);  // 清零物理页
    return page;
}
```

`pages` 是预分配数组（[software/kernel/vm_page.c:30](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/vm_page.c#L30)）：`static struct vm_page *pages = (struct vm_page*) KERNEL_HEAP_BASE;`。`vm_page_init`（34-49 行）在启动时把 `boot_pages_used`（启动期已用的页）之后的所有页挂上 free list。注意分配时用了 `disable_interrupts()` + 自旋锁——物理页分配是临界区。

引用计数回收在 `dec_page_ref`（[software/kernel/vm_page.c:81-93](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/vm_page.c#L81-L93)）：原子减 1，降到 0 才把页还回 free list **头部**（`list_add_head`，有利于 LRU 式复用）。

再看 cache 的页查找与哈希（[software/kernel/vm_cache.c:123-139](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/vm_cache.c#L123-L139)）：

```c
struct vm_page *lookup_cache_page(const struct vm_cache *cache, unsigned int offset)
{
    unsigned int bucket = gen_hash(cache, offset) % NUM_HASH_BUCKETS;  // NUM_HASH_BUCKETS=37
    multilist_for_each(&hash_table[bucket], page, hash_entry, struct vm_page)
    {
        if (page->cache_offset == offset && page->cache == cache)
            return page;
    }
    return 0;
}
```

这是「按 (cache, offset) 查页」的入口，`soft_fault` 每次沿 cache 链向上找都要调它。`insert_cache_page`（107-121 行）同时把页挂进哈希桶和 cache 自己的 `page_list`，所以 `vm_cache` 有两种遍历方式：哈希（按 offset 查）和链表（枚举 cache 拥有的所有页，`dec_cache_ref` 销毁时用）。

现在精读 `soft_fault` 的核心两段。第一段是「沿 cache 链向上找页 + 从文件读入」（[software/kernel/vm_address_space.c:274-348](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/vm_address_space.c#L274-L348)）：

```c
for (cache = area->cache; cache; cache = cache->source)   // 沿源链向上
{
    source_page = lookup_cache_page(cache, cache_offset);
    if (source_page) break;                                // 命中：找到了

    if (cache->file)                                       // 后端是磁盘文件
    {
        source_page = vm_allocate_page();
        source_page->busy = 1;                             // 占位防 collided fault
        insert_cache_page(cache, cache_offset, source_page);
        unlock_vm_cache(); restore_interrupts(old_flags);
        ... read_file(cache->file, cache_offset, ..., size_to_read);  // 阻塞读盘
        // BSS 尾巴清零（文件短于整页时）
        memset(..., 0, PAGE_SIZE - size_to_read);
        disable_interrupts(); lock_vm_cache();
        source_page->busy = 0;
        break;
    }

    is_cow_page = 1;                                       // 没 file，继续向上找
    if (cache == area->cache) {                            // 顶层插 dummy 占位
        dummy_page = vm_allocate_page(); dummy_page->busy = 1;
        insert_cache_page(cache, cache_offset, dummy_page);
    }
}
```

第二段是「写时复制决策」（[software/kernel/vm_address_space.c:360-388](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/vm_address_space.c#L360-L388)）：

```c
else if (is_cow_page)        // 源页属于别的 cache
{
    if (is_store)            // 真的要写 → 复制
    {
        memcpy((void*) PA_TO_VA(page_to_pa(dummy_page)),
               (void*) PA_TO_VA(page_to_pa(source_page)), PAGE_SIZE);
        source_page = dummy_page;   // 以后用私有副本
        dummy_page->busy = 0;
    }
    else                     // 只读 → 直接共享源页，不复制
    {
        remove_cache_page(dummy_page);  // 撤掉占位
        dec_page_ref(dummy_page);
    }
}
```

最后是「计算属性 + 写页表」（[software/kernel/vm_address_space.c:408-422](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/vm_address_space.c#L408-L422)）：

```c
page_flags = PAGE_PRESENT;
if ((area->flags & AREA_WRITABLE) != 0 && (source_page->dirty || is_store))
    page_flags |= PAGE_WRITABLE;            // 干净页先不给可写，留作脏页跟踪
if (area->flags & AREA_EXECUTABLE) page_flags |= PAGE_EXECUTABLE;
if (space == &kernel_address_space) page_flags |= PAGE_SUPERVISOR | PAGE_GLOBAL;

vm_map_page(space->translation_map, address, page_to_pa(source_page) | page_flags);
return 1;
```

这一行 `vm_map_page` 就是 4.2 节的主角——它把 PTE 写进页表并 `tlbinval`，但**不**插 TLB。`soft_fault` 返回 1 后，控制权回到 `handle_trap`，`eret` 回用户态，CPU 重执行那条指令，此刻才触发 `TT_TLB_MISS` → 汇编 `tlb_miss_handler`（[software/kernel/trap_entry.S:197-236](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap_entry.S#L197-L236)）→ 读到刚写好的 present PTE → `dtlbinsert`（228 行）→ `eret` → 命中。**这就是「两个 trap 接力」的完整落地。**

#### 4.3.4 代码实践

**实践目标**：跟踪一次「从文件加载的用户代码页」的首次缺页全过程，把本讲三个模块串起来。

**操作步骤**：

1. 假设用户程序入口在某页 `va`，对应 area 的 `cache->file` 指向 `program.elf`，且该页尚未映射（TLB 和页表都没项）。
2. CPU 取指 `va` → MMU 查 TLB 缺失 → 查页表也缺失/present=0 → 触发 `TT_PAGE_FAULT`。
3. 在源码里标注每一跳：
   - `trap.c:122` 派发 → `handle_page_fault`（`vm_address_space.c:213`）→ `lookup_area` 命中代码段 area → `soft_fault`（245 行）。
   - `soft_fault` 沿 cache 链：顶层 cache 没页且有 `file` → `vm_allocate_page`（`vm_page.c:51`）分一页 → `insert_cache_page`（`vm_cache.c:107`）占位 → `read_file` 把 ELF 内容读进这页 → busy 清零。
   - 计算 flags（`PAGE_PRESENT | PAGE_EXECUTABLE`，用户态不加 SUPERVISOR）→ `vm_map_page`（`vm_translation_map.c:188`）写 PTE + `tlbinval`。
   - 返回用户态 → `TT_TLB_MISS` → `trap_entry.S:228` 的 `dtlbinsert` → 命中取指。
4. 试着回答：如果这一页随后被**写**（假设 area 可写），会发生什么？——首次写时 `PAGE_WRITABLE` 没置（页干净），所以又缺页（仍是 page fault），这次 `is_store=1`，`soft_fault` 把 `dirty` 置 1 并补上 `PAGE_WRITABLE` 重新映射，此后写不再缺页。

**需要观察的现象**：一次首次代码页访问实际经历了**两次 trap**（page fault + tlb miss），但这是惰性的——只对真正用到的页付出代价。

**预期结果**：你能画出从「`TT_PAGE_FAULT`」到「`dtlbinsert` 后命中」的状态流转图，并指出 `vm_allocate_page`、`lookup_cache_page`、`vm_map_page` 三个调用分别发生在哪一步。

（源码阅读型实践；若要运行验证，可在 `vm_page.h` 里临时把 `VM_DEBUG` 的 `#if 0` 改成 `#if 1` 重新编译内核，跑一个程序，观察大量 `soft fault va ...`、`reading page from file`、`freeing page pa ...` 调试输出——**注意这会修改源码，仅用于本地实验，验证后请还原**。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 `vm_page` 结构用预分配数组而不是 slab？

**参考答案**：因为存在循环依赖。slab 依赖内核堆（`kmalloc`），而内核堆扩张（`kmalloc` 里 `vm_map_page` 增页）又依赖物理页分配器，物理页分配器需要 `vm_page` 结构来记账。为了打破循环，启动期在 `KERNEL_HEAP_BASE` 处预先划出一块连续空间（`PAGE_STRUCTURES_SIZE`）放 `vm_page` 数组，`vm_page_init` 直接用它。这正是 `kernel_main` 里 `vm_page_init` 必须在 `boot_init_heap` 之前的原因。

**练习 2**：`soft_fault` 里 `busy` 位的作用是什么？如果没有它会出什么问题？

**参考答案**：`busy` 标记「这一页正在从磁盘读入、内容还没就绪」。当线程 A 正在 `read_file` 读某页（期间已释放了 cache 锁），线程 B 若对同一地址缺页，`lookup_cache_page` 会查到这个 busy 页（而不是重复分配、重复读盘）；随后 B 在 `while (source_page->busy) reschedule();` 处让出 CPU 等 A 读完。没有 `busy` 位的话，多个线程会重复分配多个物理页、重复读同一份磁盘数据，既浪费内存又可能造成数据不一致。

**练习 3**：读一个 COW 页（`is_store=0`）时，`soft_fault` 为什么要把 dummy 页撤掉？

**参考答案**：因为只读情况下直接共享源 cache 的页，本 cache 并不「拥有」这页（`page->cache` 仍是源 cache）。dummy 页只是用来占位防止 collided fault 的临时角色；既然不复制，就把它从本 cache 移除并 `dec_page_ref`，避免占着一个无用的物理页，也避免误以为本 cache 拥有该页。

---

### 4.4 slab 分配（slab）

#### 4.4.1 概念说明

前面三个模块反复出现 `slab_alloc(&xxx_slab)`（分配地址空间、翻译表、cache 对象都是）。**slab 分配器**专门用于分配**固定大小**的小对象：同一种类型用同一个 `slab_allocator`，分配/释放都 O(1)，而且无需每对象附带头部。

Nyuzi 的 slab 刻意简化。`slab.h` 注释说得很清楚：*Unlike 'real' slab allocators, this doesn't defer object destruction. It also never releases memory back to the system.*（不像「真正的」slab，它不延迟对象析构，也从不把内存还给系统）。它本质上是个**分块（chunking）分配器**：向底层 `kmalloc` 要一大块（`slab_size`，默认一页），然后按 `object_size` 一块块切下去发。

`slab_allocator` 结构（[software/kernel/slab.h:27-35](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/slab.h#L27-L35)）：

```c
struct slab_allocator
{
    spinlock_t lock;
    unsigned int object_size;     // 每块大小（= sizeof(对象)）
    void *free_list;              // 已释放对象的空闲链
    void *wilderness_slab;        // 当前正在切割的大块
    unsigned int wilderness_offset; // 大块里已切走多少
    unsigned int slab_size;       // 大块大小
};
```

`MAKE_SLAB` 宏（37-38 行）用一个静态初始化器声明一个分配器：`MAKE_SLAB(address_space_slab, struct vm_address_space)` 就是「一个专门分配 `vm_address_space` 的 slab，块大小 = 1 页」。

#### 4.4.2 核心流程

```text
slab_alloc(sa):
    加锁
    if free_list 非空:                  // 优先复用已释放的对象
        object = free_list; free_list = *(void**)object;   // 链表弹头
    else:
        if wilderness 用完或没有:        // 大块不够切了
            wilderness_slab = kmalloc(sa->slab_size)       // 向堆要新一页
            wilderness_offset = 0
        object = wilderness_slab + wilderness_offset       // 切下一块
        wilderness_offset += object_size
    解锁
    return object

slab_free(sa, object):                  // 把对象头插回 free_list
    *(void**)object = free_list; free_list = object
```

巧思在于「空闲链复用」：释放的对象本身被当作链表节点，其头 4 字节存下一个空闲对象指针，所以 free list 不占额外内存（把要回收的对象就地串成隐式链表）。整个分配器在 vm 子系统里被三处用到：

- `MAKE_SLAB(address_space_slab, struct vm_address_space)`（`vm_address_space.c:28`）——分配地址空间；
- `MAKE_SLAB(translation_map_slab, struct vm_translation_map)`（`vm_translation_map.c:49`）——分配翻译表；
- `MAKE_SLAB(cache_slab, struct vm_cache)`（`vm_cache.c:28`）——分配 cache 对象。

#### 4.4.3 源码精读

`slab_alloc` 全貌（[software/kernel/slab.c:22-52](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/slab.c#L22-L52)）：

```c
void *slab_alloc(struct slab_allocator *sa)
{
    void *object = 0;
    int old_flags = acquire_spinlock_int(&sa->lock);     // 关中断+自旋锁
    if (sa->free_list)
    {
        object = sa->free_list;                          // 复用
        sa->free_list = *((void**) object);              // 头 4 字节 = 下一节点
    }
    else
    {
        if (sa->wilderness_slab == 0
            || sa->wilderness_offset + sa->object_size > sa->slab_size)
        {
            sa->wilderness_slab = kmalloc(sa->slab_size); // 要新一页
            sa->wilderness_offset = 0;
        }
        object = (void*)((char*) sa->wilderness_slab + sa->wilderness_offset);
        sa->wilderness_offset += sa->object_size;
    }
    release_spinlock_int(&sa->lock, old_flags);
    return object;
}
```

`slab_free`（[software/kernel/slab.c:54-62](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/slab.c#L54-L62)）就是头插回收。两者都用 `acquire_spinlock_int` / `release_spinlock_int`（关中断的自旋锁），因为内核对象分配可能在中断或并发线程上下文发生。

slab 坐在内核堆（`kmalloc`）之上。`kmalloc`（[software/kernel/kernel_heap.c:41-100](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/kernel_heap.c#L41-L100)）是一个首次适应（first-fit）+ 合并的空闲块分配器，不够时调 `vm_map_page` 给堆扩页（注意 85-87 行它给的属性是 `SUPERVISOR | GLOBAL`）。于是整个内核内存分配的层次是：

```text
vm_allocate_page (物理页)
        ↑
vm_map_page / kmalloc (内核堆：变长块)
        ↑
slab_alloc (固定大小对象：地址空间 / 翻译表 / cache)
```

#### 4.4.4 代码实践

**实践目标**：理解 slab 的「复用 vs 切块」两条路径，并跑通内置自测。

**操作步骤**：

1. 读 `slab.c` 末尾的 `#ifdef TEST_SLAB` 测试（64-108 行）：它声明 `MAKE_SLAB(node_slab, struct linked_node)`，然后循环「分配 j 个节点 → 释放 j-1 个 → 保留 1 个」，最后打印保留节点的 value。
2. 手工推演：当 j=1 时分配 1 个（走切块路径，向 kmalloc 要一页，切第一个），释放 0 个；j=2 时分配 2 个（第 1 个复用上轮留在 free_list 的、第 2 个新切），释放 1 个……验证你理解的复用顺序。
3. 若想实际运行这个自测：查阅 `software/kernel/CMakeLists.txt` 看 `TEST_SLAB` 是否有编译目标；若有则按其方式编译，否则只能在脑中推演（**待本地确认是否有现成的 TEST_SLAB 构建入口**）。

**需要观察的现象**：被 `slab_free` 回收的对象立即进入 `free_list`，下一次 `slab_alloc` 优先取出它（LIFO），所以 slab 长期运行也几乎不增长 wilderness。

**预期结果**：能解释「为什么 slab 分配器在分配大量同型小对象时比直接 kmalloc 高效」——因为没有每对象元数据、复用 O(1)、且对齐友好。

#### 4.4.5 小练习与答案

**练习 1**：`slab_free` 把回收对象的头 4 字节当链表指针用，这意味着什么？

**参考答案**：意味着每个对象必须**至少 4 字节大**，且调用者释放对象后不能再访问它的前 4 字节（那已被改写为 next 指针）。本讲里 `vm_address_space`、`vm_translation_map`、`vm_cache` 都以 `list_node`（含 next 指针）打头，天然满足这个隐式约束。这也是 slab 能「零额外开销」维护空闲链的代价。

**练习 2**：注释说这个 slab「从不把内存还给系统」。这会造成什么影响？为什么 Nyuzi 接受这个简化？

**参考答案**：slab 一旦向 `kmalloc` 要了大块，即使对象全部释放，大块也一直占着堆，造成内核堆只增不减。Nyuzi 接受它是因为：内核对象（地址空间、翻译表、cache）数量相对稳定，且嵌入式/实验性内核对长期内存碎片化不敏感；真正的 slab 还需构造函数/析构函数/per-CPU 缓存等复杂机制，与 Nyuzi 的极简目标不符。

---

## 5. 综合实践：跟踪一次「用户进程读自己代码段」的完整虚拟内存链路

把本讲四个模块串成一个端到端任务。目标是让你能把「一条取指指令」从触发 trap 到最终命中 TLB 的全链路，对着源码逐一标注。

**任务背景**：进程 P 刚被 `exec_program` 装载（u12-l1），其代码段 area 挂在一个 `cache->file = program.elf` 的 cache 上，但代码页**尚未映射**（按需分页）。现在 P 的线程首次执行代码段入口的取指。

**操作步骤**：

1. **画出两级页表结构**。写下一个虚拟地址 `va`（如 `0x00100000`），手算 `pgdindex`、`pgtindex`、页内偏移。对照 [vm_translation_map.c:188-243](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/vm_translation_map.c#L188-L243) 确认索引公式。

2. **标注第一次 trap（`TT_PAGE_FAULT`）的每一跳**，给出文件:行号：
   - 硬件检测 present=0 → trap（参考 u7-l3）；
   - `trap_entry.S` 保存现场 → `trap.c::handle_trap`（[trap.c:122](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap.c#L122)）；
   - `handle_page_fault`（[vm_address_space.c:213](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/vm_address_space.c#L213)）→ `lookup_area` 命中代码段；
   - `soft_fault`（[vm_address_space.c:245](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/vm_address_space.c#L245)）沿 cache 链，顶层有 `file` → `vm_allocate_page`（[vm_page.c:51](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/vm_page.c#L51)）分页 → `read_file` 读 ELF → `vm_map_page`（[vm_translation_map.c:188](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/vm_translation_map.c#L188)）写 PTE + `tlbinval`。

3. **标注第二次 trap（`TT_TLB_MISS`）**：`eret` 回用户态，CPU 重取指 → TLB 仍空 → `TT_TLB_MISS` → 汇编 `tlb_miss_handler`（[trap_entry.S:197](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap_entry.S#L197)）读页目录/页表 → `dtlbinsert`（228 行）→ `eret` → 命中。

4. **回答三个串联问题**：
   - `slab` 在这条链路的哪一步被间接用到？（答：`soft_fault` 里若需要新 cache 会 `create_vm_cache` → `slab_alloc(&cache_slab)`；`create_address_space` 时也会 `slab_alloc(&address_space_slab)` 和 `&translation_map_slab`。本例进程已建好，故 slab 主要在进程**创建**阶段而非本次缺页中发挥作用。）
   - `vm_page` 数组下标和物理地址如何互转？（答：`pa = (page - pages) * PAGE_SIZE`。）
   - 为什么这条链路必须经过**两个**不同 trap？（答：分工——page fault 负责「准备数据 + 写页表」，tlb miss 负责「把页表项搬进 TLB 硬件」。前者是慢路径（可阻塞、可读盘），后者是快路径（纯查表）。）

**预期结果**：你产出一张状态流转图，标清两次 trap、五个 vm 文件的调用点，并能解释每一步为什么落在那个模块。这是把本讲四模块（地址空间 / 翻译表 / 物理页+缓存 / slab）融会贯通的检验。

## 6. 本讲小结

- **地址空间（`vm_address_space`）**=「区域映射（area_map）+ 硬件翻译表（translation_map）」。缺页入口 `handle_page_fault` 按 `KERNEL_BASE` 区分内核/用户空间，用读锁允许并发，找不到 area 即段错误。
- **两级页表**：32 位 VA = 页目录索引(10) + 页表索引(10) + 偏移(12)；`vm_translation_map` 封装页目录/页表读写，内核页表（目录项 768..1023）在所有地址空间间**共享**，用户页表私有；ASID 用位图分配，切换地址空间只需写两个控制寄存器。
- **两种「找不到」要分清**：`TT_PAGE_FAULT`（present=0，本讲的 C 慢路径 `soft_fault` 处理：分配页、读文件、COW、写 PTE）与 `TT_TLB_MISS`（PTE 在、TLB 没有，u7-l1 的汇编快路径 `dtlbinsert` 处理）。一次首次访问往往经历**两次 trap 接力**。
- **`vm_page` 是物理页账本**，预分配数组（非 slab）以打破与堆的循环依赖；`vm_cache` 把物理内存抽象为「后端存储（文件/源 cache）的缓存」，用哈希表按 (cache, offset) 查页，`source` 链支撑写时复制。
- **`soft_fault` 是核心**：沿 cache 链找页 → 找不到则分配+读盘（`busy` 位防 collided fault）→ COW 决策（写才复制）→ 计算属性（干净页暂不给可写以跟踪脏页）→ `vm_map_page` 写 PTE。
- **slab 是极简分块分配器**：free_list 复用（回收对象就地当链表节点）+ wilderness 切块，固定大小内核对象（地址空间/翻译表/cache）都靠它；它不延迟析构、不还内存，是刻意的简化。层次为 `物理页 → 内核堆 kmalloc → slab → 内核对象`。

## 7. 下一步学习建议

- **u12-l3（线程、上下文切换与同步原语）**：本讲的 `switch_to_translation_map` 会在那里被上下文切换调用——进程切换 = 换翻译表（换页目录 + ASID）。建议接着读 `context_switch.S` 与 `thread.c`，看「换地址空间」如何嵌入调度。
- **重读 u7-l1**：现在你已经看过软件侧的 `vm_map_page` / `tlbinval` / PTE 格式，再回头看硬件 TLB 缺失和 `dtlbinsert`，软硬件分工会豁然开朗。
- **延伸阅读源码**：
  - [software/kernel/vm_area_map.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/vm_area_map.c) —— `create_vm_area` / `lookup_area` / `destroy_vm_area` 的区域管理实现（本讲只用了它的接口）。
  - [software/kernel/loader.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/loader.c) —— `exec_program` 如何解析 ELF 并为各段创建 area + cache，把本讲的 area/cache 和实际程序加载连起来。
  - [software/kernel/kernel_heap.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/kernel_heap.c) —— slab 的底座，看 `kmalloc` 如何首次适应 + 合并 + 扩页。
- **动手实验**：在 `vm_page.h` 把 `VM_DEBUG` 改成 `#if 1` 重编内核，跑一个用户程序，观察启动期大量 `soft fault` / `reading page from file` 输出，直观感受「按需分页」；实验后请还原源码。
