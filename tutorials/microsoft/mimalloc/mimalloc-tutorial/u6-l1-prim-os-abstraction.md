# OS 原语抽象层：src/prim 如何隔离平台差异

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `include/mimalloc/prim.h` 定义的 21 个 `_mi_prim_*` 原语函数，并按「内存映射、commit/reset、大页与 NUMA、时钟与进程信息、输出/环境/随机、线程」六大函数族归类。
2. 对照 unix 与 windows 两个实现，说出 `mmap`/`mprotect`/`madvise` 与 `VirtualAlloc`/`VirtualProtect` 的对应关系，特别是 `needs_recommit` 这类「同一接口、不同平台语义相反」的细节。
3. 理解「一份接口合同 + 选择器 + 每平台一个实现文件」这种抽象层设计如何支撑 Linux/macOS/BSD/Windows/WASI/Emscripten 等众多平台移植——新平台只需要实现这一层。

本讲是单元六的第一讲。前面五个单元里我们一直把「向 OS 要内存」当成一个黑盒（u1-l3 的代码地图里只提了一句「底层内存由 arena.c 批发」），从本讲开始，我们把这个黑盒打开。

## 2. 前置知识

### 2.1 虚拟内存三阶段：reserve、commit、fault

现代操作系统给每个进程一套虚拟地址空间，分配器真正关心的不是物理内存，而是虚拟地址的**状态机**：

- **reserve（保留）**：在虚拟地址空间里圈定一段区间，不分配物理内存，访问会段错误。Linux 上 `mmap(PROT_NONE)` 即是保留；Windows 上 `VirtualAlloc(MEM_RESERVE)`。
- **commit（提交）**：承诺这段地址「可以访问」，OS 在首次访问时（缺页中断）才真正给物理页。Linux 上 `mprotect(PROT_READ|PROT_WRITE)`；Windows 上 `VirtualAlloc(MEM_COMMIT)`。
- **purge/reset（归还）**：把已 commit 的页交还 OS，降低常驻内存（RSS），但虚拟地址保留着下次复用。Linux 上 `madvise(MADV_DONTNEED)` 或 `MADV_FREE`；Windows 上 `VirtualAlloc(MEM_RESET)` 或 `VirtualFree(MEM_DECOMMIT)`。

u1-l1 引入过「虚拟内存 reserve/commit/purge」这组术语，u6-l2 将专门讲 mimalloc 如何用它们实现 eager page purging；本讲先看这些操作在源码里的最底层形态。

### 2.2 平台差异为什么必须隔离

同一件「保留 1GiB 地址空间」的事，在不同 OS 上是不同的系统调用、不同的参数、不同的返回值约定（Linux 用 `errno`，Windows 用 `GetLastError()`）。如果 `arena.c`、`os.c` 里到处写 `#ifdef _WIN32`，每支持一个新平台就要改几十处。mimalloc 的做法是把**所有**直接触碰 OS 的调用收拢进一个约 130 行的接口文件 `prim.h`，每个平台实现一遍，其余全部源码只面向接口编程。

### 2.3 本讲会用到的两个小知识

- **errno 约定**：POSIX 系统调用失败时返回 `-1` 并设置全局 `errno`；prim 接口统一把错误码作为 `int` 返回值（0 表示成功）。
- **运行时动态绑定**：Windows 的新 API（如 `VirtualAlloc2`）只在 Win10+ 存在，不能直接链接，要在运行时用 `GetProcAddress` 取函数指针——这是 Windows 实现里大量函数指针的由来。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [include/mimalloc/prim.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim.h) | **接口合同**：定义 21 个 `_mi_prim_*` 原语函数与能力结构体 `mi_os_mem_config_t`，全文无任何实现 |
| [src/prim/prim.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/prim.c) | **选择器**：按宏条件 `#include` 某一个平台实现，并附加通用的进程 attach/detach 逻辑 |
| [src/prim/unix/prim.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c) | **Linux/BSD/macOS 实现**（本讲精读对象）：基于 `mmap`/`mprotect`/`madvise`/`syscall` |
| [src/prim/windows/prim.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/windows/prim.c) | **Windows 实现**：基于 `VirtualAlloc`/`VirtualFree`/`VirtualProtect` 及一批动态绑定的扩展 API |
| [src/prim/osx/prim.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/osx/prim.c) | macOS 实现：全文只有一行，直接 `#include "../unix/prim.c"`，靠 unix 文件内的 `__APPLE__` 分支做差异化 |
| [src/prim/wasi/prim.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/wasi/prim.c) | WASM/WASI 实现：无虚拟内存，用 `memory_grow` 或 `sbrk` 单向扩张 |
| [src/os.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c) | **prim 层的唯一大客户**：在原语之上包一层「取整、对齐兜底、统计、memid 登记」，是 prim 与 arena 之间的中间层 |
| [src/prim/readme.md](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/readme.md) | prim 目录的自述文档（约 10 行） |

调用链位置回顾（承接 u1-l3 的地图）：

```
mi_malloc（alloc.c）
  └─ 页内存不足 → arena.c（向 arena 要 slice）
                   └─ arena 也不够 → os.c（包装层：取整/对齐/统计）
                                      └─ prim.h 接口（本讲）
                                           ├─ unix/prim.c   → mmap/madvise/mprotect
                                           ├─ windows/prim.c → VirtualAlloc/VirtualFree
                                           └─ ...
```

TLS 相关的原语（`_mi_prim_thread_associate_default_theap` 等）由 [src/prim/prim-tls.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/prim-tls.c) 配合实现，属于 u7-l2 的主题，本讲只提接口位置。

## 4. 核心概念与源码讲解

### 4.1 prim.h：一份 21 个函数的可移植性合同

#### 4.1.1 概念说明

`prim.h` 是 mimalloc 与操作系统之间**唯一的**边界。它做的事情类似于很多项目里的「HAL（硬件抽象层）」：上层（os.c、arena.c、init.c）只调用 `_mi_prim_*` 函数，永远不直接调 `mmap` 或 `VirtualAlloc`。

为什么值得单独一层，而不是让 os.c 直接写平台分支？三个理由：

1. **移植成本最小化**：新平台只需要写一个实现文件加进选择器，其余约万行代码零改动。
2. **合同先行**：接口注释里明确写了前置条件（pre）和语义（如 `needs_recommit` 的含义），平台实现只需满足合同，上层无需关心差异。
3. **测试与审查聚焦**：所有「危险」的直接 OS 交互集中在一个目录，出问题时排查范围极小。

#### 4.1.2 核心流程

prim.h 的接口按功能分成六族：

| 函数族 | 函数 | 作用 |
|---|---|---|
| 初始化与能力 | `_mi_prim_mem_init` | 启动时填一张「本机能力表」`mi_os_mem_config_t` |
| 内存映射（7 个） | `_mi_prim_alloc` / `_mi_prim_free` / `_mi_prim_commit` / `_mi_prim_decommit` / `_mi_prim_reset` / `_mi_prim_reuse` / `_mi_prim_protect` | 虚拟内存的保留/释放/提交/归还/复用/保护 |
| 大页与 NUMA（3 个） | `_mi_prim_alloc_huge_os_pages` / `_mi_prim_numa_node` / `_mi_prim_numa_node_count` | 1GiB 巨页申请与 NUMA 拓扑查询 |
| 时钟与进程信息（2 个） | `_mi_prim_clock_now` / `_mi_prim_process_info` | 毫秒时钟；RSS/缺页数等统计来源 |
| 输出/环境/随机（3 个） | `_mi_prim_out_stderr` / `_mi_prim_getenv` / `_mi_prim_random_buf` | 警告输出、读环境变量（选项系统 u2-l3 的底层）、安全随机数（u9-l1 的底层） |
| 线程（5 个） | `_mi_prim_thread_init_auto_done` / `_mi_prim_thread_done_auto_done` / `_mi_prim_thread_associate_default_theap` / `_mi_prim_thread_is_in_threadpool` / `_mi_prim_thread_yield` | 线程结束回调注册、TLS 关联、自旋让核 |

其中「内存映射族」是核心中的核心，也是 mimalloc 「bounded（有界内存）」支柱的物理基础。

#### 4.1.3 源码精读

**（1）合同总纲。** prim.h 开头的注释块规定了所有原语的通用约定——参数非 NULL、地址页对齐、尺寸为正且页对齐、返回 `int` 错误码（0 成功）：

[include/mimalloc/prim.h:L18-L22](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim.h#L18-L22)
```c
// note: on all primitive functions, we always have result parameters != NULL, and:
//  addr != NULL and page aligned
//  size > 0     and page aligned
//  the return value is an error code as an `int` where 0 is success
```
这段注释就是「合同条款」：平台实现可以放心假设输入合法，上层（os.c）负责保证对齐与取整——这就是为什么 os.c 里到处是 `_mi_align_up(size, _mi_os_page_size())`。

**（2）能力表 `mi_os_mem_config_t`。** 

[include/mimalloc/prim.h:L25-L35](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim.h#L25-L35)
```c
typedef struct mi_os_mem_config_s {
  size_t  page_size;              // default to 4KiB
  size_t  large_page_size;        // 0 if not supported, usually 2MiB 
  size_t  alloc_granularity;      // smallest allocation size (usually 4KiB, on Windows 64KiB)
  size_t  physical_memory_in_kib; // physical memory size in KiB
  size_t  virtual_address_bits;   // usually 48 or 56 bits on 64-bit systems. ...
  bool    has_overcommit;         // can we reserve more memory than can be actually committed?
  bool    has_partial_free;       // can allocated blocks be freed partially? (true for mmap, false for VirtualAlloc)
  bool    has_virtual_reserve;    // supports virtual address space reservation? ...
  bool    has_transparent_huge_pages;  // true if transparent huge pages are enabled (on Linux)
} mi_os_mem_config_t;
```
初始化时 `_mi_prim_mem_init` 负责填这张表，之后**整个分配器**的策略都跟着它走。四个 `bool` 尤其重要：

- `has_partial_free`：mmap 可以对已映射区间的一部分 `munmap`，而 Windows 的 `VirtualFree(MEM_RELEASE)` 只能整段释放、且必须传基准地址。os.c 的对齐兜底路径会根据它选择不同策略（见 [src/os.c:L386-L389](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c#L386-L389) 的分支注释）。
- `has_overcommit`：决定 unix 实现是否给 mmap 加 `MAP_NORESERVE`。
- `has_virtual_reserve`：WASM 上为 false，直接改变 arena 的可行策略。
- `has_transparent_huge_pages`：影响最小 purge 粒度（[src/os.c:L55-L67](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c#L55-L67)）。

os.c 里有一份静态默认值（4KiB 页、32GiB 假设物理内存等），平台实现只覆盖自己探测得到的部分：

[src/os.c:L24-L33](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c#L24-L33)
```c
static mi_os_mem_config_t mi_os_mem_config = {
  4096,     // page size
  0,        // large page size (usually 2MiB)
  ...
```

**（3）`_mi_prim_alloc` 的完整签名与前置条件。**

[include/mimalloc/prim.h:L43-L51](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim.h#L43-L51)
```c
// Allocate OS memory. Return NULL on error.
// The `try_alignment` is just a hint and the returned pointer does not have to be aligned.
// If `commit` is false, the virtual memory range only needs to be reserved (with no access)
// which will later be committed explicitly using `_mi_prim_commit`.
// `is_zero` is set to true if the memory was zero initialized (as on most OS's)
// pre: !commit => !allow_large
//      try_alignment >= _mi_os_page_size() and a power of 2
int _mi_prim_alloc(void* hint_addr, size_t size, size_t try_alignment, bool commit, bool allow_large, bool* is_large, bool* is_zero, void** addr);
```
注意两个关键语义：

- `try_alignment` **只是提示**，返回地址不保证对齐——兜底责任在 os.c 的 `mi_os_prim_alloc_aligned`（过度分配再掐头去尾）。这个设计让 Linux 实现可以简单地用「hint 地址」技巧而不是昂贵的对齐重试。
- `is_zero` 出参让 mimalloc 知道内存是否天然清零（fresh mmap 的匿名页是零页），从而决定 `mi_zalloc` 等要不要补 memset——这是 u4-l1 讲过的 `free_is_zero` 捷径在 OS 层的来源。

**（4）`_mi_prim_decommit` 的 `needs_recommit` 出参。**

[include/mimalloc/prim.h:L58-L62](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim.h#L58-L62)
```c
// Decommit memory. Returns error code or 0 on success. The `needs_recommit` result is true
// if the memory would need to be re-committed. For example, on Windows this is always true,
// but on Linux we could use MADV_DONTNEED to decommit which does not need a recommit.
int _mi_prim_decommit(void* addr, size_t size, bool* needs_recommit);
```
这是全接口中最能体现「抽象层吸收平台差异」的一个参数：Windows 上 `VirtualFree(MEM_DECOMMIT)` 之后必须重新 commit 才能访问；Linux 上 `MADV_DONTNEED` 之后地址仍是可读写的（只是内容变零）。上层通过这个出参统一处理两种世界，u6-l2 讲 purge 时会用到。

#### 4.1.4 代码实践

**实践：数一数合同里到底有多少个函数，并验证六族划分。**

1. 实践目标：对 prim 接口建立量化认识，确认「21 个函数」的说法。
2. 操作步骤：
   ```bash
   cd <仓库根目录>
   grep -c "^int _mi_prim_\|^void _mi_prim_\|^bool _mi_prim_\|^size_t _mi_prim_\|^mi_msecs_t _mi_prim_" include/mimalloc/prim.h
   grep -n "^int _mi_prim_\|^void _mi_prim_\|^bool _mi_prim_\|^size_t _mi_prim_\|^mi_msecs_t _mi_prim_" include/mimalloc/prim.h
   ```
3. 需要观察的现象：第一个命令应输出 `21`；第二个命令逐行列出全部接口声明。
4. 预期结果：把 21 行输出贴到表格里，按 4.1.2 的六族归类，检查是否与表格一致（1+7+3+2+3+5 = 21）。
5. 本实践只做静态统计，不依赖构建，可直接完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_mi_prim_alloc` 的对齐参数叫 `try_alignment` 而不是 `alignment`？

**答案**：因为它只是提示（hint），实现可以返回未对齐的指针（比如 mmap 恰好没落在对齐边界上）。真正的对齐保证由上层 os.c 的 `mi_os_prim_alloc_aligned` 提供：先尝试直接分配，若未对齐则释放后「过度分配 + 掐头去尾」。把对齐做成尽力而为，可以让常见路径（OS 恰好返回对齐地址）保持一次系统调用的低成本。

**练习 2**：`mi_os_mem_config_t` 里 `alloc_granularity` 在 Windows 上是 64KiB 而不是 4KiB 页大小，这会影响什么？

**答案**：Windows 的 `VirtualAlloc` 分配必须以 64KiB（allocation granularity）为界对齐起始地址，即使页大小是 4KiB。mimalloc 用它来判断「多小的对齐请求可以免费满足」（如 [src/prim/windows/prim.c:L294](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/windows/prim.c#L294) 中 `try_alignment > win_allocation_granularity` 才动用 `VirtualAlloc2`），也影响 arena slice 尺寸等设计取舍。

**练习 3**：如果一个新平台「没有虚拟内存概念，只能顺序扩张堆」，prim 层的哪些函数会变得没法实现？

**答案**：这正是 WASI 的处境：`_mi_prim_free` 只能返回 0 不做任何事（[src/prim/wasi/prim.c:L34-L38](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/wasi/prim.c#L34-L38)），`has_virtual_reserve=false`、`has_partial_free=false`。可见合同允许「降级实现」，上层靠能力表避开不可用特性——这就是抽象层的容错价值。

### 4.2 选择器 prim.c：一份接口、五个平台实现

#### 4.2.1 概念说明

`src/prim/prim.c` 只有 77 行，它不实现任何原语，只做两件事：

1. **选择实现**：根据编译期宏决定 `#include` 哪个平台文件——注意是源码级包含而不是链接期多态，这样整个 prim 层编译后就是普通的一坨函数，零间接调用开销。
2. **补充通用逻辑**：进程 attach/detach（constructor/destructor）与默认的 allocator init 回调，这些是不依赖平台的。

macOS 的实现 `osx/prim.c` 全文只有一行有效代码，直接包含 unix 实现再靠后者内部的 `__APPLE__` 分支差异化——这是一种「实现复用实现」的轻量继承。

#### 4.2.2 核心流程

编译期选择流程：

```
src/prim/prim.c 被编译
  ├─ _WIN32        → include windows/prim.c   (VirtualAlloc 家族)
  ├─ __APPLE__     → include osx/prim.c       → 再 include unix/prim.c (mmap)
  ├─ __wasi__      → define MI_USE_SBRK; include wasi/prim.c  (memory-grow / sbrk)
  ├─ __EMSCRIPTEN__→ include emscripten/prim.c (emmalloc_* + pthread)
  └─ 其他           → include unix/prim.c      (Linux/BSD/Illumos/Haiku/...)
```

运行期流程（以 GCC/Clang 为例）：`mi_process_attach`（constructor 属性）→ `_mi_auto_process_init()` → 最终驱动选项解析与 `_mi_os_init` → `_mi_prim_mem_init` 填能力表。

#### 4.2.3 源码精读

**（1）选择器本体。**

[src/prim/prim.c:L11-L27](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/prim.c#L11-L27)
```c
#if defined(_WIN32)
#include "windows/prim.c"  // VirtualAlloc (Windows)

#elif defined(__APPLE__)
#include "osx/prim.c"      // macOSX (actually defers to mmap in unix/prim.c)

#elif defined(__wasi__)
#define MI_USE_SBRK
#include "wasi/prim.c"     // memory-grow or sbrk (Wasm)

#elif defined(__EMSCRIPTEN__)
#include "emscripten/prim.c" // emmalloc_*, + pthread support

#else
#include "unix/prim.c"     // mmap() (Linux, macOSX, BSD, Illumnos, Haiku, DragonFly, etc.)
#endif
```
注意 `#else` 分支是兜底的 unix 实现——绝大多数类 Unix 平台不需要单独写文件。要支持一个全新 OS，理论上只需在此处加一个分支并提供对应实现文件。

**（2）osx 的「一行实现」。**

[src/prim/osx/prim.c:L8-L9](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/osx/prim.c#L8-L9)
```c
// We use the unix/prim.c with the mmap API on macOSX
#include "../unix/prim.c"
```
macOS 与 Linux 共享 POSIX mmap 接口，差异（如 `MADV_FREE_REUSABLE`、`VM_MAKE_TAG`、`mach_task_basic_info`）全部在 unix/prim.c 内部用 `#if defined(__APPLE__)` 处理。

**（3）通用进程 attach。**

[src/prim/prim.c:L30-L46](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/prim.c#L30-L46)
```c
#if !defined(MI_PRIM_HAS_PROCESS_ATTACH)
#if defined(__GNUC__) || defined(__clang__)
  ...
  #if defined(__clang__)
    #define mi_attr_constructor __attribute__((constructor(101)))
    #define mi_attr_destructor  __attribute__((destructor(101)))
  #else
    ...
  #endif
  static void mi_attr_constructor mi_process_attach(void) {
    _mi_auto_process_init();
  }
```
这段解释了 u2-l1 的一个现象：`MIMALLOC_VERBOSE=1` 的版本横幅为何在 `main` 之前打印——constructor 属性让 `mi_process_attach` 先于普通构造函数执行。Clang 下优先级 `(101)` 进一步抢在大多数 constructor 之前。Windows 实现自己定义了 `MI_PRIM_HAS_PROCESS_ATTACH`（走 CRT/TLS 段魔数），于是跳过这段通用代码——这是「平台可覆盖通用逻辑」的钩子设计。

**（4）诚实的免责声明。** prim 目录自述文件承认抽象尚未 100% 完成：

[src/prim/readme.md:L11](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/readme.md#L11)
```
Note: still work in progress, there may still be places in the sources that still depend on OS ifdef's.
```
读源码时要保持这种警觉：例如 [src/page-map.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-map.c) 等文件里仍有平台分支，prim 层是「主边界」而非「唯一边界」。

#### 4.2.4 代码实践

**实践：确认你的构建选中了哪个实现文件。**

1. 实践目标：把「编译期选择」从抽象概念变成可观察事实。
2. 操作步骤（Linux 为例，承接 u1-l2 的构建方式）：
   ```bash
   mkdir -p out/release && cd out/release && cmake ../.. && make -j4
   # 在构建产物中确认 unix 实现的符号存在
   nm -C libmimalloc.so | grep _mi_prim_
   ```
   再用预处理直接观察选择结果（不需要完整构建）：
   ```bash
   cd <仓库根目录>
   gcc -E -dD -D_GNU_SOURCE src/prim/prim.c -Iinclude -Isrc 2>/dev/null | grep -c "unix_mmap_prim"
   ```
3. 需要观察的现象：`nm` 输出中应出现 `_mi_prim_alloc`、`_mi_prim_commit` 等符号（T 标记）；预处理输出中 `unix_mmap_prim` 出现次数大于 0，说明 unix/prim.c 被包含进了编译单元。
4. 预期结果：Linux 上两步都成立。Windows 上对应的观察方式是检查 `win_virtual_alloc` 符号。若预处理命令因头文件路径报错，可用 cmake 生成的 `compile_commands.json` 中的真实编译命令替换。（本命令组合在 Ubuntu + GCC 环境下预期可直接工作，具体输出待本地验证。）
5. 预期结论：整个 prim 层确实通过 `#include` 融入单一翻译单元，链接产物中没有单独的「prim 库」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 mimalloc 用「源码包含」而不是「每平台编译成独立库再链接」来实现多态？

**答案**：源码包含让 prim 函数与调用者（os.c 等）处于同一翻译单元的可能性存在，编译器可以内联跨层调用；同时避免链接期符号解析的不确定性。这与 u1-l3 讲过的「翻译单元合并」（alloc.c include 其他 .c）是同一手法。

**练习 2**：`MI_PRIM_HAS_PROCESS_ATTACH` 这个宏在什么情况下会被定义？定义它意味着什么？

**答案**：Windows 实现（[src/prim/windows/prim.c:L795](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/windows/prim.c#L795) 等四处）在选中 CRT/TLS/raw-DllMain 初始化方案时定义它。它意味着「该平台自己处理了进程 attach/detach」，prim.c 末尾那段通用的 constructor/destructor 代码会被 `#if !defined(...)` 排除，避免重复注册。

### 4.3 unix/prim.c：mmap/madvise 家族精读

#### 4.3.1 概念说明

unix/prim.c 约 1066 行，是五个实现中最大的，覆盖 Linux、macOS、FreeBSD、Solaris/Illumos、Haiku、QNX 等一大票平台（子差异再用 `#if defined(__linux__)` 等内部分支处理）。它要解决的问题：

- 用 `mmap` 家族实现 7 个内存映射原语，且 commit 与 reserve 都用 mmap 的 `prot` 参数区分（而不是像 Windows 那样有两个独立操作）。
- 探测本机能力（overcommit、THP、物理内存、地址位数）填能力表。
- 在不触发堆分配的前提下读文件、取随机数——因为 prim 函数可能在 mimalloc 自举阶段被调用，此时分配器还不能用。

第三点是这个文件里很多「怪写法」的根源，也是初读时最容易困惑的地方。

#### 4.3.2 核心流程

**分配（reserve ± commit）调用链：**

```
_mi_prim_alloc(commit?)
  └─ unix_mmap(large_only=false, allow_large)
       ├─ [允许大页?] MAP_HUGETLB / MAP_HUGE_2MB / MAP_HUGE_1GB 尝试
       │    └─ unix_mmap_prim_aligned → unix_mmap_prim → mmap()
       │         （失败则 large_page_try_ok=8，之后 8 次不再尝试）
       └─ [常规] unix_mmap_prim_aligned
            ├─ BSD: MAP_ALIGNED(n) 直接对齐
            ├─ 64位: _mi_os_get_aligned_hint 给 2TiB~30TiB 区间的 hint 地址
            └─ 兜底: 普通 mmap
            └─ [THP 允许?] madvise(MADV_HUGEPAGE)
```

prot 参数的取值就是 reserve/commit 的分界：`commit ? (PROT_WRITE|PROT_READ) : PROT_NONE`。

**commit/decommit/reset 的语义链：**

| 原语 | 系统调用 | 内存可访问？ | 内容 | RSS |
|---|---|---|---|---|
| `_mi_prim_commit` | `mprotect(RW)` | 是 | 不清零（保守假设） | 按需增长 |
| `_mi_prim_decommit` | `madvise(MADV_DONTNEED)` | 仍是 | 清零 | 立即下降 |
| `_mi_prim_reset` | `madvise(MADV_FREE)`，失败退 `MADV_DONTNEED` | 仍是 | 可能随时清零 | 惰性下降 |

#### 4.3.3 源码精读

**（1）防递归的 syscall 包装器。**

[src/prim/unix/prim.c:L84-L99](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c#L84-L99)
```c
//------------------------------------------------------------------------------------
// Use syscalls for some primitives to allow for libraries that override open/read/close etc.
// and do allocation themselves; using syscalls prevents recursion when mimalloc is
// still initializing (issue #713)
//------------------------------------------------------------------------------------

#if defined(MI_HAS_SYSCALL_H) && defined(SYS_open) && ...
static inline int mi_prim_open(const char* fpath, int open_flags) {
  return syscall(SYS_open,fpath,open_flags,0);
}
```
为什么读 `/proc/sys/vm/overcommit_memory` 不直接用 `open(3)` 库函数？因为 `fopen` 会 `malloc`，而 mimalloc 初始化期间调用 malloc 会递归进入自己（issue #713）。绕开 libc、直接陷内核是最安全的。这是分配器开发特有的「自举期洁癖」，全文件随处可见（读环境变量也是手工遍历 `environ` 数组而不是 `getenv`）。

**（2）能力探测与 `_mi_prim_mem_init`。**

[src/prim/unix/prim.c:L250-L263](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c#L250-L263)
```c
void _mi_prim_mem_init( mi_os_mem_config_t* config )
{
  long psize = sysconf(_SC_PAGESIZE);
  if (psize > 0 && (unsigned long)psize < SIZE_MAX) {
    config->page_size = (size_t)psize;
    config->alloc_granularity = (size_t)psize;
    unix_detect_physical_memory(config->page_size, &config->physical_memory_in_kib);
  }
  config->large_page_size = MI_UNIX_LARGE_PAGE_SIZE;      // 固定 2MiB（见 L82 的 todo 注释）
  config->has_overcommit = unix_detect_overcommit();
  config->has_partial_free = true;    // mmap can free in parts
  config->has_virtual_reserve = true;
  config->has_transparent_huge_pages = unix_detect_thp();
  config->virtual_address_bits = unix_detect_virtual_address_bits();
```
`unix_detect_overcommit`（[L129-L153](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c#L129-L153)）读 `/proc/sys/vm/overcommit_memory`，值为 `2`（never overcommit）时 `has_overcommit=false`，后续 mmap 就不加 `MAP_NORESERVE`。`unix_detect_thp`（[L155-L172](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c#L155-L172)）解析 `/sys/kernel/mm/transparent_hugepage/enabled` 里方括号的当前值。这两个探测解释了：为什么同一份 mimalloc 在不同机器上策略不同——能力表是**运行时**填的，不是编译期写死的。

**（3）`_mi_prim_alloc`：reserve/commit 的分界只有一行三目。**

[src/prim/unix/prim.c:L490-L498](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c#L490-L498)
```c
int _mi_prim_alloc(void* hint_addr, size_t size, size_t try_alignment, bool commit, bool allow_large, bool* is_large, bool* is_zero, void** addr) {
  ...
  *is_zero = true;
  int protect_flags = (commit ? (PROT_WRITE | PROT_READ) : PROT_NONE);
  *addr = unix_mmap(hint_addr, size, try_alignment, protect_flags, false, allow_large, is_large);
  return (*addr != NULL ? 0 : errno);
}
```
`*is_zero = true` 值得咀嚼：POSIX 保证 `MAP_ANONYMOUS` 的 mmap 内容为零，所以 fresh 映射天然满足 `mi_zalloc` 语义——一次免费的清零。

**（4）`unix_mmap_prim`：给映射打上 mimalloc 名字。**

[src/prim/unix/prim.c:L308-L316](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c#L308-L316)
```c
static void* unix_mmap_prim(void* addr, size_t size, int protect_flags, int flags, int fd) {
  void* p = mmap(addr, size, protect_flags, flags, fd, 0 /* offset */);
  #if defined(__linux__) && defined(PR_SET_VMA)
  if (p!=MAP_FAILED && p!=NULL) {
    prctl(PR_SET_VMA, PR_SET_VMA_ANON_NAME, p, size, "mimalloc");
  }
  #endif
  return p;
}
```
`PR_SET_VMA_ANON_NAME`（内核 5.17+ 开放）把匿名映射命名为 `mimalloc`，之后 `cat /proc/<pid>/maps` 能直接看到哪些区间是分配器保留的——绝佳的调试实践素材（见 4.3.4）。

**（5）对齐三层策略。**

[src/prim/unix/prim.c:L342-L360](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c#L342-L360)
```c
#if (MI_INTPTR_SIZE >= 8) && !defined(MAP_ALIGNED)
// on 64-bit systems, use the virtual address area after 2TiB for 4MiB aligned allocations
if (addr == NULL) {
  void* hint = _mi_os_get_aligned_hint(try_alignment, size);
  if (hint != NULL) {
    p = unix_mmap_prim(hint, size, protect_flags, flags, fd);
    ...
```
Linux 没有 `MAP_ALIGNED`，于是用「hint 地址」技巧：`_mi_os_get_aligned_hint`（[src/os.c:L126-L158](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c#L126-L158)）在 2TiB~30TiB 的专属区域里线性分发对齐过的地址，mmap 传 hint 后内核大概率恰好落在对齐边界。这就是 u3-l4 讲过的「arena 按 256MiB 对齐」在 prim 层的落地手段之一。

**（6）commit/decommit/reset 三兄弟。**

[src/prim/unix/prim.c:L521-L534](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c#L521-L534)
```c
int _mi_prim_commit(void* start, size_t size, bool* is_zero) {
  // note: we may think that *is_zero can be true since the memory
  // was either from mmap PROT_NONE, or from decommit MADV_DONTNEED, but
  // we sometimes call commit on a range with still partially committed
  // memory and `mprotect` does not zero the range.
  *is_zero = false;
  int err = mprotect(start, size, (PROT_READ | PROT_WRITE));
```
注释解释了为什么这里保守地设 `*is_zero = false`：commit 可能作用在「部分已提交」的区间上，而 mprotect 不清零。错误路径上的 `unix_mprotect_hint`（[L505-L515](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c#L505-L515)）在 ENOMEM 时提示用户调大 `vm.max_map_count`——secure 模式下 guard page 数量多，容易撞映射数上限。

[src/prim/unix/prim.c:L544-L560](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c#L544-L560)
```c
int _mi_prim_decommit(void* start, size_t size, bool* needs_recommit) {
  int err = 0;
  #if 1
    #if defined(__APPLE__) && defined(MADV_FREE_REUSABLE)
      err = unix_madvise(start, size, MADV_FREE_REUSABLE);
      if (err) { err = unix_madvise(start, size, MADV_DONTNEED); }
    #else
      err = unix_madvise(start, size, MADV_DONTNEED);
    #endif
    #if !MI_DEBUG && MI_SECURE<=2
      *needs_recommit = false;
    #else
      *needs_recommit = true;
      mprotect(start, size, PROT_NONE);
    #endif
```
两个细节：macOS 用 `MADV_FREE_REUSABLE` 是为了让 RSS 立即下降（issue #1097）；`needs_recommit` 在 release 构建下为 `false`（MADV_DONTNEED 之后仍可读写），但在 debug/secure 构建下额外 `mprotect(PROT_NONE)` 并返回 `true`——同一平台、不同构建模式给出不同答案，上层完全无感。

[src/prim/unix/prim.c:L581-L596](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c#L581-L596)
```c
  #if defined(MADV_FREE)
  static _Atomic(size_t) advice = MI_ATOMIC_VAR_INIT(MADV_FREE);
  int oadvice = (int)mi_atomic_load_relaxed(&advice);
  while ((err = unix_madvise(start, size, oadvice)) != 0 && err == EAGAIN) { /* try again */ };
  if (err == EINVAL && oadvice == MADV_FREE) {
    // if MADV_FREE is not supported, fall back to MADV_DONTNEED from now on
    mi_atomic_store_release(&advice, (size_t)MADV_DONTNEED);
```
reset 优先 `MADV_FREE`（更快、内核可懒回收），遇到 EINVAL 说明内核不支持，就**原子地永久切换**到 `MADV_DONTNEED`。用静态 `_Atomic` 变量做一次性降级，是这个文件里反复出现的模式（大页失败计数 `large_page_try_ok`、getrandom 不可用标志同理）。

**（7）NUMA 与其他杂项原语。**

[src/prim/unix/prim.c:L665-L689](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c#L665-L689)
```c
size_t _mi_prim_numa_node(void) {
  #if defined(MI_HAS_SYSCALL_H) && defined(SYS_getcpu)
    unsigned int node = 0;
    unsigned int ncpu = 0;
    int err = syscall(SYS_getcpu, &ncpu, &node, NULL);
    ...
size_t _mi_prim_numa_node_count(void) {
  char buf[128];
  unsigned last_found = 0;
  for(unsigned node = 1; node < 256; node++) {
    _mi_snprintf(buf, 127, "/sys/devices/system/node/node%u", node);
    if (mi_prim_access(buf,R_OK) != 0) { ... }
```
NUMA 节点号用 `getcpu` 系统调用（注意参数顺序的 quirks），节点数靠枚举 `/sys/devices/system/node/nodeN` 目录并允许稀疏。巨页 NUMA 绑定走 `mbind` 系统调用（[L630-L646](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c#L630-L646)）。

随机数（[L958-L996](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c#L958-L996)）优先 `getrandom` 系统调用，ENOSYS 时降级读 `/dev/urandom`（仍用 syscall 包装器防递归）；时钟（[L762-L775](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c#L762-L775)）用 `CLOCK_MONOTONIC` 的 `clock_gettime`；进程信息（[L799-L843](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c#L799-L843)）用 `getrusage`（注意 macOS 的 `ru_maxrss` 单位是字节而 Linux 是 KiB）；线程结束钩子（[L1011-L1041](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c#L1011-L1041)）用 `pthread_key_create` 的析构回调调用 `_mi_thread_done`——这正是 u7 将讲的「线程退出时 theap 被 abandon」的触发源头。

#### 4.3.4 代码实践

**实践：用 strace 与 /proc/<pid>/maps 观察 prim 层的真实系统调用。**

1. 实践目标：验证「mimalloc 不为每次 malloc 调 mmap，而是整块保留 arena」以及「映射被命名为 mimalloc」。
2. 操作步骤：
   ```bash
   # 示例代码：保存为 prim-demo.c（示例代码，非项目原有文件）
   #include <mimalloc.h>
   #include <stdio.h>
   int main(void) {
     for (int i = 0; i < 100000; i++) {
       void* p = mi_malloc(64);
       mi_free(p);
     }
     printf("done\n");
     return 0;
   }
   cc prim-demo.c -I<安装路径>/include -L<安装路径>/lib -lmimalloc -o prim-demo

   # 观察一：统计系统调用
   strace -f -e trace=mmap,munmap,mprotect,madvise,brk ./prim-demo 2>&1 | grep -c mmap

   # 观察二：看映射名字（需要 Linux 5.17+ 且内核启用 CONFIG_ANON_VMA_NAME）
   ./prim-demo &  # 若程序太快退出，可在末尾加 getchar()
   grep mimalloc /proc/$!/maps
   ```
3. 需要观察的现象：观察一中 `mmap` 次数应该是**个位数到几十**（arena 保留），远小于 100000；观察二中每行映射尾部应出现 `[mimalloc]` 标签。
4. 预期结果：如果 mmap 次数与分配次数同量级，说明链接的还是系统 malloc（回顾 u2-l1 的 `LD_PRELOAD` 检查）。`[mimalloc]` 标签取决于内核版本，旧内核上只是看不到标签，不影响功能。具体数值待本地验证。
5. 思考题延伸：对比 `strace -e trace=madvise` 下 debug 与 release 构建的差异，能否印证 4.3.3（6）中 `needs_recommit` 的构建模式分支？

#### 4.3.5 小练习与答案

**练习 1**：`_mi_prim_decommit` 在 release 构建下 `*needs_recommit = false`，这意味着后续访问该内存会发生什么？

**答案**：`MADV_DONTNEED` 只是归还物理页并保证下次读到零，地址上仍有读写权限，因此后续直接读写会触发缺页并拿到零页，无需先 `_mi_prim_commit`。而 debug/secure 构建额外 `mprotect(PROT_NONE)`，直接访问会段错误，必须重新 commit——用可观测的崩溃换取更严格的检查。

**练习 2**：为什么 `unix_detect_*` 系列函数用 `mi_prim_open`（syscall）而不是 `fopen`？

**答案**：`fopen` 内部会分配缓冲区，而 `_mi_prim_mem_init` 在 mimalloc 自举期间运行，此时任何 malloc 都会递归进入尚未初始化的 mimalloc（issue #713）。直接 `syscall(SYS_open, ...)` 不经过 libc 的分配路径，杜绝递归。

**练习 3**：`unix_mmap` 里 `large_page_try_ok` 静态原子变量的作用是什么？

**答案**：大页申请（MAP_HUGETLB）在系统未配置或无权限时**总是失败**，而失败的 mmap 调用很昂贵。一旦失败，代码把它设为 8，接下来的 8 次分配直接跳过大页尝试，避免反复撞墙（[src/prim/unix/prim.c:L400-L455](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c#L400-L455)）。这是「失败记忆」式的自适应降级。

### 4.4 windows/prim.c：VirtualAlloc 家族与跨平台对照

#### 4.4.1 概念说明

windows/prim.c 约 1200 行（含大量初始化方案的条件编译），实现同一份 prim 合同，但底座换成 `VirtualAlloc` 家族。与 unix 实现相比，它有三块显著不同的「 Windows 味」：

1. **动态绑定 API**：`VirtualAlloc2`（对齐分配）、`NtAllocateVirtualMemoryEx`（1GiB 巨页）、各种 NUMA 函数只在较新 Windows 存在，必须 `GetProcAddress` 运行时获取。
2. **大页需要特权**：要先取得 `SeLockMemoryPrivilege` 才能用 `MEM_LARGE_PAGES`。
3. **初始化钩子复杂**：静态链接的库要抢在 main 前运行，Windows 上没有 constructor 属性，得靠 CRT 段（`.CRT$XLB` 等）魔数或 DllMain 方案。

#### 4.4.2 核心流程

Windows 的 reserve/commit 是 `VirtualAlloc` 的两个独立标志位，可同时给：

```
_mi_prim_alloc(commit?)
  └─ win_virtual_alloc(large_only=false, allow_large)
       ├─ [允许大页?] flags |= MEM_LARGE_PAGES → win_virtual_alloc_prim
       └─ win_virtual_alloc_prim（内含 OOM 重试循环，至多 10 次/2.2 秒）
            └─ win_virtual_alloc_prim_once
                 ├─ 64位: _mi_os_get_aligned_hint → VirtualAlloc(hint, ...)
                 ├─ 对齐>64KiB 且有 VirtualAlloc2: MI_MEM_ADDRESS_REQUIREMENTS 精确对齐
                 └─ 兜底: VirtualAlloc(addr, size, flags, PAGE_READWRITE)
```

其中 `flags = MEM_RESERVE`（保留），`commit` 时追加 `MEM_COMMIT`（提交）——一次调用完成两阶段。

#### 4.4.3 源码精读

**（1）动态绑定。**

[src/prim/windows/prim.c:L32-L35](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/windows/prim.c#L32-L35)
```c
// We use VirtualAlloc2 for aligned allocation, but it is only supported on Windows 10 and Windows Server 2016.
// So, we need to look it up dynamically to run on older systems. (use __stdcall for 32-bit compatibility)
// NtAllocateVirtualAllocEx is used for huge OS page allocation (1GiB)
```
随后的 [L59-L63](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/windows/prim.c#L59-L63) 定义函数指针，[L196-L210](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/windows/prim.c#L196-L210) 在 `_mi_prim_mem_init` 里从 `kernelbase.dll`/`ntdll.dll` 取地址。文件甚至自定义了 `MI_MEM_EXTENDED_PARAMETER` 结构体以兼容老 SDK——为了在 Windows XP 上也能跑，可谓用心良苦。

**（2）能力表：与 unix 相反的三项。**

[src/prim/windows/prim.c:L176-L189](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/windows/prim.c#L176-L189)
```c
static DWORD win_allocation_granularity = 64*MI_KiB;

void _mi_prim_mem_init( mi_os_mem_config_t* config )
{
  config->has_overcommit = false;
  config->has_partial_free = false;
  config->has_virtual_reserve = true;
  ...
  if (si.dwAllocationGranularity > 0) {
    config->alloc_granularity = si.dwAllocationGranularity;
```
`has_overcommit=false`（Windows 提交即受页面文件限制）、`has_partial_free=false`（`VirtualFree(MEM_RELEASE)` 只能整段释放且 size 参数必须为 0）、`alloc_granularity=64KiB`——这三个差异足以改变上层 os.c 的策略分支。

**（3）`_mi_prim_alloc`：MEM_RESERVE ± MEM_COMMIT。**

[src/prim/windows/prim.c:L383-L392](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/windows/prim.c#L383-L392)
```c
int _mi_prim_alloc(void* hint_addr, size_t size, size_t try_alignment, bool commit, bool allow_large, bool* is_large, bool* is_zero, void** addr) {
  ...
  *is_zero = true;
  int flags = MEM_RESERVE;
  if (commit) { flags |= MEM_COMMIT; }
  *addr = win_virtual_alloc(hint_addr, size, try_alignment, flags, false, allow_large, is_large);
  return (*addr != NULL ? 0 : (int)GetLastError());
}
```

**（4）对齐专用通道。**

[src/prim/windows/prim.c:L293-L306](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/windows/prim.c#L293-L306)
```c
// on modern Windows try use VirtualAlloc2 for aligned allocation
if (addr == NULL && try_alignment > win_allocation_granularity && (try_alignment % _mi_os_page_size()) == 0 && pVirtualAlloc2 != NULL) {
  MI_MEM_ADDRESS_REQUIREMENTS reqs = { 0, 0, 0 };
  reqs.Alignment = try_alignment;
  MI_MEM_EXTENDED_PARAMETER param = { {0, 0}, {0} };
  param.Type.Type = MiMemExtendedParameterAddressRequirements;
  param.Arg.Pointer = &reqs;
  void* p = (*pVirtualAlloc2)(GetCurrentProcess(), addr, size, flags, PAGE_READWRITE, &param, 1);
```
Windows 的 `VirtualAlloc2` 支持**精确对齐**要求（不是 hint），这比 unix 的三层尽力而为更干脆；只有老系统才退回 hint/过度分配。

**（5）commit/decommit：语义与 unix 相反。**

[src/prim/windows/prim.c:L402-L423](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/windows/prim.c#L402-L423)
```c
int _mi_prim_commit(void* addr, size_t size, bool* is_zero) {
  *is_zero = false;
  ...
  void* p = VirtualAlloc(addr, size, MEM_COMMIT, PAGE_READWRITE);
  if (p == NULL) return (int)GetLastError();
  return 0;
}

int _mi_prim_decommit(void* addr, size_t size, bool* needs_recommit) {
  BOOL ok = VirtualFree(addr, size, MEM_DECOMMIT);
  *needs_recommit = true;  // for safety, assume always decommitted even in the case of an error.
  return (ok ? 0 : (int)GetLastError());
}
```
对比 4.3.3（6）：unix 的 decommit 是 `madvise`（权限不变），Windows 是 `VirtualFree(MEM_DECOMMIT)`（取消提交），因此 `needs_recommit` 恒为 `true`。同一个出参，两边给出相反答案，上层 os.c 却写得完全一样——这就是抽象层的价值最直观的展示。

**（6）其余原语一瞥。**

| 原语 | Windows 实现 | 位置 |
|---|---|---|
| `_mi_prim_free` | `VirtualFree(MEM_RELEASE)`，含 `ERROR_INVALID_ADDRESS` 时的基址回退 | [L254-L273](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/windows/prim.c#L254-L273) |
| `_mi_prim_reset` | `VirtualAlloc(MEM_RESET)` | [L425-L434](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/windows/prim.c#L425-L434) |
| `_mi_prim_reuse` | 空操作 | [L436-L439](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/windows/prim.c#L436-L439) |
| `_mi_prim_protect` | `VirtualProtect(PAGE_NOACCESS)` | [L441-L445](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/windows/prim.c#L441-L445) |
| `_mi_prim_clock_now` | `QueryPerformanceCounter` | [L570-L578](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/windows/prim.c#L570-L578) |
| `_mi_prim_getenv` | `GetEnvironmentVariableA` | [L684-L688](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/windows/prim.c#L684-L688) |
| `_mi_prim_random_buf` | `BCryptGenRandom` | [L718-L730](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/windows/prim.c#L718-L730) |

另外两处值得一提：OOM 重试循环（[L321-L347](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/windows/prim.c#L321-L347)，提交内存遇到 OOM 时按递增间隔重试至多 10 次，issue #894）；`_mi_prim_free` 的基址回退（对齐兜底路径可能返回段中间地址，释放时用 `VirtualQuery` 找回 `AllocationBase`）。

#### 4.4.4 代码实践（本讲指定实践任务）

**实践：制作「prim 接口 → unix/windows 实现」对照表。**

1. 实践目标：亲手完成虚拟内存 reserve/commit 相关接口的双平台映射，把本讲知识固化为可查阅的表格。
2. 操作步骤：
   - 打开 [include/mimalloc/prim.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim.h)，圈出内存映射族的 7 个接口。
   - 在 [src/prim/unix/prim.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c) 与 [src/prim/windows/prim.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/windows/prim.c) 中用 `grep -n "_mi_prim_" 各文件` 定位每个接口的实现起点。
   - 对每个接口回答三列：unix 底层调用是什么、windows 底层调用是什么、两者语义有无差别（尤其 `needs_recommit` 与 `is_zero`）。
3. 需要观察的现象：两个文件的函数名**完全同构**（一一对应），但函数体几乎每一行都不同。
4. 预期结果：应得到类似下面的表（读者可自行补充 `is_zero` 列）：

| prim 接口 | unix 实现 | windows 实现 |
|---|---|---|
| `_mi_prim_alloc`（reserve） | `mmap(PROT_NONE, MAP_PRIVATE\|MAP_ANONYMOUS\|MAP_NORESERVE)` | `VirtualAlloc(MEM_RESERVE, PAGE_*)` |
| `_mi_prim_alloc`（reserve+commit） | `mmap(PROT_READ\|PROT_WRITE, ...)` | `VirtualAlloc(MEM_RESERVE\|MEM_COMMIT, PAGE_READWRITE)` |
| `_mi_prim_free` | `munmap(addr, size)`（可部分释放） | `VirtualFree(addr, 0, MEM_RELEASE)`（整段） |
| `_mi_prim_commit` | `mprotect(PROT_READ\|PROT_WRITE)` | `VirtualAlloc(MEM_COMMIT)` |
| `_mi_prim_decommit` | `madvise(MADV_DONTNEED)`，`needs_recommit=false`（release） | `VirtualFree(MEM_DECOMMIT)`，`needs_recommit=true` |
| `_mi_prim_reset` | `madvise(MADV_FREE)`→降级 `MADV_DONTNEED` | `VirtualAlloc(MEM_RESET)` |
| `_mi_prim_reuse` | no-op（macOS: `MADV_FREE_REUSE`） | no-op |
| `_mi_prim_protect` | `mprotect(PROT_NONE)` / RW | `VirtualProtect(PAGE_NOACCESS)` / RW |

5. 本实践为纯源码阅读型，无需运行环境；每行结论都应附上你定位到的行号作为证据。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `_mi_prim_free` 在 unix 上要传 `size` 而 Windows 实现里 `MI_UNUSED(size)`？

**答案**：`munmap` 需要长度来决定释放多大区间（所以 mmap 系统支持部分释放）；`VirtualFree(MEM_RELEASE)` 的契约是传 0 长度、释放 `addr` 所在的整个分配区域（由内核查询区域基址），size 传非 0 反而错误。Windows 实现的 `ERROR_INVALID_ADDRESS` 回退正是为了处理「addr 是段中间地址」的情形。

**练习 2**：两个实现里 `_mi_prim_alloc` 都设 `*is_zero = true`，但 `_mi_prim_commit` 都设 `*is_zero = false`，为什么不对称？

**答案**：全新映射（无论 mmap 匿名页还是 Windows 首次 commit）都保证零初始化；而 commit 原语可能作用于「部分已提交、写过数据」的区间，`mprotect` 与再次 `MEM_COMMIT` 都不会清零已有内容，所以必须保守地报告非零，让上层按需 memset。

**练习 3**：如果不支持 `VirtualAlloc2`（老 Windows），对齐分配会怎么走？

**答案**：`win_virtual_alloc_prim_once` 的三层瀑布退到最后一层 `VirtualAlloc`（[src/prim/windows/prim.c:L305-L307](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/windows/prim.c#L305-L307)），返回地址可能未对齐；此时 os.c 的 `mi_os_prim_alloc_aligned` 会释放它并走「过度分配 + 掐头去尾」兜底——由于 `has_partial_free=false`，Windows 的兜底先 reserve 更大的范围、对齐后把两边的多余部分 decommit，与 unix 的直接 munmap 部分区间不同（见 [src/os.c:L386-L389](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c#L386-L389)）。

## 5. 综合实践

**任务：写一份《mimalloc 平台移植备忘》小文档，并以 Linux 上的运行证据支撑其中一半结论。**

具体步骤：

1. **静态部分**：以 4.4.4 的对照表为基础，扩展到 prim.h 全部六族接口（21 个函数），每个函数给出 unix 与 windows 的实现函数名 + 行号 + 一句话差异说明。这逼你把两个 1000+ 行的文件通读一遍，但只读函数签名和头几行即可完成。
2. **动态部分**：完成 4.3.4 的 strace 实验，记录：mmap 调用次数、是否有 `madvise(MADV_DONTNEED)`（可开 `MIMALLOC_PURGE_DELAY=0` 强化观察）、`/proc/<pid>/maps` 中的 `[mimalloc]` 标签。
3. **综合分析**：回答一个问题——「如果要给一个全新的类 Unix OS（假设有 mmap 但没有 madvise）移植 mimalloc，prim 层的 21 个函数里哪些可以直接抄 unix/prim.c，哪些必须改写，哪些可以降级为空操作？」提示：`_mi_prim_reset`/`_mi_prim_decommit` 可降级（能力表相应置 false 或复用 commit 语义），`_mi_prim_alloc`/`_mi_prim_free` 是硬依赖。
4. **预期产出**：一份 Markdown 表格 + 一段 strace 输出摘录 + 一段移植分析。这份文档在你后续阅读 u6-l2（os.c 的 purge 策略）、u6-l3（arena 如何调用 os 层）时会反复派上用场。

## 6. 本讲小结

- mimalloc 用 `include/mimalloc/prim.h` 这份约 130 行的接口合同（21 个 `_mi_prim_*` 函数 + 能力表 `mi_os_mem_config_t`）把所有 OS 交互收拢到一处；上层 os.c 是它唯一的直接客户。
- `src/prim/prim.c` 是编译期选择器，用 `#include` 把五个平台实现之一并入翻译单元；macOS 直接复用 unix 实现，WASI 降级为 memory-grow/sbrk，新平台移植原则上只需实现这一层。
- unix 实现的核心是「一个 mmap 打天下」：reserve/commit 的差别只是 `PROT_NONE` 与 `PROT_READ|PROT_WRITE`；decommit/reset 用 `MADV_DONTNEED`/`MADV_FREE`，且大量使用「syscall 直陷 + 静态原子标志降级」来安全度过自举期并自适应内核能力。
- windows 实现围绕 `VirtualAlloc` 家族：`MEM_RESERVE`/`MEM_COMMIT` 两个标志、`VirtualAlloc2` 精确对齐、`MEM_LARGE_PAGES` 需要特权，大量新 API 靠 `GetProcAddress` 动态绑定以兼容老系统。
- 同一接口在不同平台语义可以相反（`needs_recommit`、`has_partial_free`、`is_zero`），抽象层的意义正是把这些差异吸收掉，让 os.c 及以上完全不用写 `#ifdef`。

## 7. 下一步学习建议

下一讲 **u6-l2（os.c：commit/decommit/purge 与大页、NUMA）** 顺着本讲往上走一层：看 os.c 如何在 prim 原语之上做取整（`_mi_os_good_alloc_size`）、对齐兜底（`mi_os_prim_alloc_aligned`）、`memid` 登记与 purge 延迟调度。建议预先重读 [src/os.c:L88-L101](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c#L88-L101) 与 purge 相关函数。若对线程相关原语（`_mi_prim_thread_associate_default_theap` 等）感兴趣，可提前浏览 [src/prim/prim-tls.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/prim-tls.c)，它是 u7-l2 的主角。
