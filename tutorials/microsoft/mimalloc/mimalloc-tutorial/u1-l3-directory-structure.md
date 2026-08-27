# 目录结构与代码地图：src、include、prim、test 的分工

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出 mimalloc 仓库的顶层目录划分图，说出 `src/`、`include/mimalloc/`、`src/prim/`、`test/` 各自承担的职责。
2. 说明 `alloc.c`、`free.c`、`page.c`、`arena.c` 这四个枢纽文件分别做什么，并沿着「一次 `mi_malloc` 从进来到返回」说出它会依次经过哪些文件。
3. 知道所有核心数据结构（`mi_page_t`、`mi_theap_t`、`mi_heap_t`、`mi_arena_t`）都定义在 `include/mimalloc/types.h` 这一个文件里。
4. 理解 `src/prim/` 为什么按 `unix/`、`windows/`、`osx/` 等子目录组织，以及它如何把「平台差异」隔离在分配器核心逻辑之外。

本讲是「地图课」：不深入任何算法细节，目标是让你在后续单元读到任何一行代码时，都能立刻知道自己在哪一层。

## 2. 前置知识

### 2.1 翻译单元与 `#include "xxx.c"`

C 编译器以**翻译单元**（translation unit）为单位编译：一个 `.c` 文件加上它递归展开的所有头文件，生成一个目标文件。通常我们只 include 头文件，但 mimalloc 有一个不常见的做法：**把一些 `.c` 文件直接 include 进另一个 `.c` 文件**，让多个源文件合并成一个翻译单元。这样做的目的是让跨文件的调用可以被完全内联（例如 `alloc.c` 里的快路径函数要调用 `free.c` 里定义的内联辅助函数）。你在本讲会看到两处：

- `alloc.c` 包含 `free.c`；
- `page.c` 包含 `page-queue.c`。

所以**文件名 ≠ 独立的目标文件**，看构建产物时不要惊讶。

### 2.2 头文件的两层：公共 API 与内部实现

- `include/mimalloc.h`、`include/mimalloc-new-delete.h`、`include/mimalloc-override.h`、`include/mimalloc-stats.h`：**给使用者看的**公共头文件。里面只有函数声明，用户程序只 include 这些。
- `include/mimalloc/*.h`（`types.h`、`internal.h`、`atomic.h`、`prim.h` 等）：**库内部使用的**。`types.h` 定义全部核心结构体，`internal.h` 定义全部内部函数（多为 `static inline`）。

### 2.3 承接前两讲的术语

u1-l1 已经建立了这些概念，本讲直接使用：**size class**（把请求尺寸归入固定档位）、**mimalloc page**（约 64KiB、只装同一种 size class 块的内存区域）、**free list**（空闲块链表）、**arena**（大块预留内存区）、**reserve/commit**（虚拟内存的预留与提交）。忘了的话先回去翻一眼 u1-l1 的第 4 节。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲怎么看它 |
| --- | --- | --- |
| [src/static.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/static.c) | 单目标文件构建方式的「总装清单」，天然列出了全库所有 .c | 只看 40 行的 include 列表 |
| [src/alloc.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c) | 分配入口：`mi_malloc` 家族与快/慢路径分流 | 看文件头与 `mi_malloc` |
| [src/free.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c) | 释放路径：本地 free 与跨线程 free | 看文件头的 `#error` 与 `mi_free` |
| [src/page.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c) | 页管理：慢路径总调度、新页创建、页回收 | 看文件头注释与 `mi_page_fresh` |
| [src/arena.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c) | 内存大管家：arena 保留、slice 位图分配 | 看文件头注释与 `_mi_arenas_alloc` |
| [include/mimalloc/types.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h) | 所有核心结构体的定义处 | 认识结构体名字与位置 |
| [include/mimalloc/prim.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim.h) | OS 原语接口定义（给 prim 各平台实现用） | 看头部注释与接口清单 |
| [src/prim/readme.md](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/readme.md) | prim 层的官方说明（11 行） | 通读 |
| [test/readme.md](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/readme.md) | 测试策略说明 | 通读 |

## 4. 核心概念与源码讲解

### 4.1 仓库全景：四大目录各管什么

#### 4.1.1 概念说明

mimalloc 仓库顶层可以分成四块（忽略文档与构建辅助目录）：

```
mimalloc/
├── include/                 # 对外 API 头文件
│   ├── mimalloc.h           #    C API 主头文件（用户唯一需要 include 的）
│   ├── mimalloc-new-delete.h#    C++ 全局 new/delete 覆盖
│   ├── mimalloc-override.h  #    malloc 宏覆盖方案
│   ├── mimalloc-stats.h     #    统计结构（供外部读统计）
│   └── mimalloc/            #    ★ 库内部头文件（types/internal/atomic/prim...）
├── src/                     # 分配器实现本体（约 20 个 .c 文件）
│   ├── prim/                #    ★ OS 原语抽象层的各平台实现
│   └── *.c                  #    核心逻辑
├── test/                    # 测试 + 官方示例工程（独立 CMakeLists）
└── CMakeLists.txt           # 构建入口（u1-l2 已讲）
```

一个直观的数量感：`src/` 下 `.c` 文件合计约 1.7 万行，其中最大的三个文件是 `arena.c`（2753 行）、`bitmap.c`（2002 行）、`page.c`（1117 行）。**没有巨型文件，每个文件主题单一**——这正是本讲想让你利用的特点。

#### 4.1.2 核心流程

看全景最快的方法不是 `ls`，而是读 `src/static.c`：它为了生成单目标文件，把全库 `.c` 逐个 include 进来，等于官方亲手写了一份「文件清单 + 一句话职责」：

```text
alloc.c        → 分配（内含 alloc-override.c 与 free.c）
alloc-aligned.c → 对齐分配
alloc-posix.c   → posix_memalign 等 POSIX 接口
arena.c         → arena 内存管理
bitmap.c        → 原子位图
heap.c          → 一等堆
init.c          → 初始化
libc.c          → strdup 等 libc 兼容封装
options.c       → 选项系统
os.c            → OS 内存操作统一层
page.c          → 页管理（内含 page-queue.c）
page-map.c      → 指针→页 的映射表
random.c        → 随机数（secure 模式用）
stats.c         → 统计
subproc.c       → 子进程隔离
theap.c         → 线程本地堆
threadlocal.c   → 线程本地存储封装
prim/prim.c     → OS 原语平台选择器
prim/prim-tls.c → TLS 策略实现
```

#### 4.1.3 源码精读

static.c 的核心就是下面这段 include 列表（省略中间若干行）：

- [src/static.c:19-23](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/static.c#L19-L23)：注释说明「静态覆盖需要把整个库做成一个目标文件，且被链接在最前时可覆盖标准库分配函数」，随后第一行 `#include "alloc.c"`，其注释明确写着它还带上了 `alloc-override.c` 和 `free.c`。
- [src/static.c:23-41](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/static.c#L23-L41)：完整的 include 清单——这就是全库 `.c` 文件的权威列表，其中 [src/static.c:33](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/static.c#L33) 的注释 `// includes page-queue.c` 再次印证「一个 .c 吃掉另一个 .c」的组织方式。
- [src/static.c:40-44](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/static.c#L40-L44)：prim 层也在清单末尾——`prim/prim.c` 与 `prim/prim-tls.c`，以及 macOS 专用的 `prim/osx/alloc-override-zone.c`（用 `#if MI_OSX_ZONE` 条件编译）。

#### 4.1.4 代码实践

1. **实践目标**：把 `static.c` 的 include 清单变成你自己的「文件职责表」。
2. **操作步骤**：
   - 打开 [src/static.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/static.c)，对照上面 4.1.2 的清单，逐个用编辑器打开 `src/` 下对应文件；
   - 每个文件只看两样东西：顶部版权块之后的注释块（多数文件有一段 5-10 行的「本文件做什么」），以及 `grep -n "^void\|^static\|^mi_decl" 文件名` 列出的函数签名。
3. **需要观察的现象**：多数文件头部有主题注释（如 page.c、arena.c，见下节）；少数文件（如 heap.c）几乎直接进入代码。
4. **预期结果**：你得到一张 20 行左右的表：文件名 → 一句话职责 → 三个代表性函数名。这张表就是后续所有单元的「地铁站牌」。

#### 4.1.5 小练习与答案

**练习 1**：`src/` 下哪些文件被「吃进」了其他 `.c` 文件、本身不会生成独立目标文件？

答案：`free.c` 与 `alloc-override.c` 被 `alloc.c` include（见 [src/alloc.c:20-23](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L20-L23)）；`page-queue.c` 被 `page.c` include（见 [src/page.c:24-26](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L24-L26)）。证据是 [src/free.c:7-8](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L7-L8) 里有一个 `#if !defined(MI_IN_ALLOC_C)` 触发的 `#error "this file should be included from 'alloc.c'"`——作者直接用编译错误防止你单独编译它。

**练习 2**：为什么 mimalloc 要把这些文件合并成一个翻译单元，而不是靠链接器？

答案：`mi_free` / 小对象分配的快路径只有几条指令（见 4.2.3 引用的注释），其中跨文件调用（如 `alloc.c` 调 `free.c` 的内联辅助函数）如果跨翻译单元就无法内联，会生成函数调用开销。合并翻译单元让编译器看到全部代码，把快路径压成最短指令序列。

### 4.2 分配主链路的四个枢纽：alloc.c → page.c → arena.c（释放走 free.c）

#### 4.2.1 概念说明

mimalloc 的核心逻辑可以画成一条自顶向下的「漏斗」，每层对应一个文件：

```text
用户程序
   │  mi_malloc(size)                      ┌─────────────────────────┐
   ▼                                       │ alloc.c                 │
mi_theap_malloc → 快路径                    │  · 入口 API 家族        │
   │  （从当前页的 free list 弹出一个块）     │  · 快/慢路径分流         │
   │  free list 空？──是──▶ _mi_malloc_generic ───────────┐          │
   ▼                                       │               ▼        │
                                          │         page.c（页管理） │
                                          │  · 按尺寸找 bin 队列     │
                                          │  · 找/扩展/新建页        │
                                          │  · 页满则 abandon/retire │
                                          │               ▼         │
                                          │  页需要新内存？          │
                                          │               ▼         │
                                          │         arena.c（内存）  │
                                          │  · 从 arena 切 slice     │
                                          │  · 位图原子分配          │
                                          │  · 不够则向 OS 申请      │
                                          └─────────────────────────┘
释放方向：mi_free(p) → free.c（本地/跨线程分流）→ page.c（页变空则回收）
```

四个文件的分工一句话版：

- **alloc.c**——「回答请求」：所有 `mi_malloc/mi_zalloc/mi_calloc...` 入口，先试快路径（当前页 free list 弹块），失败转慢路径。
- **page.c**——「管理页面」：慢路径总调度 `_mi_malloc_generic` 在这里；负责按 size class 找页、初始化新页（`mi_page_fresh`）、回收空页。
- **arena.c**——「批发内存」：所有页的底层内存都从这里来；arena 用原子位图把大块内存切成 64KiB 的 slice 分发（线程共享，需原子操作）。
- **free.c**——「归还」：判断指针属于哪个页、是本线程还是别的线程释放，分别走两条无锁路径。

#### 4.2.2 核心流程

一次 `mi_malloc(24)` 的文件穿越顺序（快路径命中时只走 alloc.c 一个文件）：

```text
mi_malloc(24)                              [alloc.c:256]
 └─ _mi_theap_default()                    取当前线程默认 theap（TLS，见 u7）
 └─ mi_theap_malloc → _mi_theap_malloc_zero_ex [alloc.c:252]
     ├─ 尺寸 ≤ MI_SMALL_SIZE_MAX？
     │   ├─ 是 → mi_theap_malloc_small_zero_nonnull [alloc.c:133]
     │   │        └─ pages_free_direct[size] 直接取页 [alloc.c:150]
     │   │        └─ mi_page_malloc_zero：free list 弹块 [alloc.c:32]
     │   │             └─（free list 空）_mi_malloc_generic
     │   └─ 否 → mi_theap_malloc_generic [alloc.c:163]
     │            └─ _mi_malloc_generic  [page.c:1091]  ← 进入 page.c
     │                 ├─ 在对应 bin 的页队列里找有空位的页
     │                 ├─ 找到 → 扩展它的 free list
     │                 └─ 找不到 → mi_page_fresh [page.c:344] 新建页
     │                          └─ mi_page_fresh_alloc [page.c:308]
     │                               └─ 申请页内存（arena 或 OS）[arena.c:618] ← 进入 arena.c
     └─ 返回块指针
```

#### 4.2.3 源码精读

**alloc.c——分配入口与快路径**

- [src/alloc.c:256-258](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L256-L258)：`mi_malloc` 只有三行——取默认 theap、转调 `mi_theap_malloc`。所有 `mi_` 前缀的公共分配函数（zalloc/calloc/realloc...）都在本文件 250-353 行区域，形态一致：**入口薄壳 + 转发到内层**。
- [src/alloc.c:29-32](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L29-L32)：文件头对快路径的注释——「在页内快速分配：只需从 free list 弹出」「release 模式下内联后约 7 条指令、仅一次判断」。这是理解整个文件布局动机的钥匙。
- [src/alloc.c:41-57](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L41-L57)：快路径本体：读 `page->free`，若为 NULL 转 `_mi_malloc_generic`（慢路径，在 page.c）；否则 `mi_block_next` 取下一个空闲块、`page->free = next`、`page->used = used+1`——三次内存写完成分配。
- [src/alloc.c:20-23](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L20-L23)：`#define MI_IN_ALLOC_C` 后 include `alloc-override.c` 与 `free.c`，证实 4.1 的翻译单元合并。

**page.c——页管理与慢路径**

- [src/page.c:8-12](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L8-L12)：文件头注释自述「分配器的核心（The core of the allocator）……导出的主函数是 `mi_malloc_generic`」。快路径失败后的一切都发生在这里。
- [src/page.c:344-351](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L344-L351)：`mi_page_fresh`——队列里实在没有可用页时新建一个，转调 [src/page.c:308](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L308) 的 `mi_page_fresh_alloc` 去申请底层内存（此时就会调到 arena.c）。
- [src/page.c:1091](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L1091)：`_mi_malloc_generic` 的真实定义处（alloc.c 里 extern 引用的就是它），文件末尾的 [src/page.c:1048](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L1048) `mi_malloc_generic_fallback` 是兜底分支。
- [src/page.c:358-388](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L358-L388)：页满了怎么办的两个分支——`_mi_page_abandon`（遗弃给全进程共享）或进 full 队列。u6-l4 会展开，这里只需留下「页的生命周期也归 page.c 管」的印象。

**arena.c——内存批发**

- [src/arena.c:8-20](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L8-L20)：文件头注释给 arena 下定义：「固定大小的 OS 内存区域，从中分配大块（≥64KiB）」，并强调「与 mimalloc 其余部分不同，arena 被线程共享，必须用原子操作访问」「用原子位图做分配」。
- [src/arena.c:595](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L595) 与 [src/arena.c:618](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L618)：`_mi_arenas_alloc_aligned` / `_mi_arenas_alloc`——page.c 申请页内存时最终到达的对外入口（本文件唯一的「批发窗口」）。
- [src/arena.c:116-128](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L116-L128)：`mi_arena_max_object_size`，允许用户用选项限制「多大以上的对象不进 arena」——arena 与 OS 直接分配的边界是可调的。

**free.c——释放路径**

- [src/free.c:223-249](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L223-L249)：`mi_free_nonnull` 的四分支分流，全部依据一个异或结果 `xtid = 当前线程id ^ 页所属线程id`：为 0 → 本线程普通页（最快）；低位是标志位 → 本线程但页满/有对齐块；标志位为 0 但线程不同 → **跨线程释放**，推入该页的 thread_free 链表；其余 → 跨线程 + 页满的通用路径。u5 整个单元就是精读这 30 行。
- [src/free.c:251-256](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L251-L256)：`mi_free` 入口——先由 `mi_validate_ptr_page_nonnull` 通过 page map（page-map.c）把指针反查成 `mi_page_t*`，再进入上面的分流。注意 free 与 malloc 的方向相反：**free 是「指针 → 页」**（查表），**malloc 是「theap → 页 → 块」**（顺推）。
- [src/free.c:28-57](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L28-L57)：本线程释放 `mi_free_block_local`——头插进 `page->local_free`、`used-1`，若 used 归零则触发页回收 `_mi_page_retire`（回到 page.c 的管辖）。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证「分配主链路」的文件顺序，而不是背下来。
2. **操作步骤**：
   - 在仓库根目录执行（待本地验证）：
     ```bash
     grep -n "_mi_malloc_generic" src/alloc.c src/page.c | head
     grep -n "mi_page_fresh_alloc" src/page.c | head
     grep -n "_mi_arenas_alloc" src/arena.c src/page.c | head
     ```
   - 从输出中确认三个「跨文件接力点」：`alloc.c:47` 调 `_mi_malloc_generic` → 定义在 `page.c:1091`；`page.c:346`（`mi_page_fresh` 内）调 `mi_page_fresh_alloc` → 同文件 308 行，内部最终调 arena.c 的 `_mi_arenas_alloc`。
3. **需要观察的现象**：`_mi_malloc_generic` 在 alloc.c 中只出现「调用」，在 page.c 中出现「定义」；arena 的分配入口只暴露 `_mi_arenas_alloc*` 两个函数。
4. **预期结果**：你在纸上画出的调用箭头 `alloc.c → page.c → arena.c` 每一条都有 grep 输出作为证据。这就是 4.2.1 那张漏斗图的实证版。
5. 本实践为纯源码阅读型，无需编译运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `mi_malloc` 的快路径可以完全不碰 arena.c？

答案：快路径只是从**已经分配好的页**的 free list 里弹出一个块（[src/alloc.c:41-57](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L41-L57)），底层内存早在页第一次创建时就从 arena 批发好了。只有 free list 为空、需要新页时才会一路下沉到 arena.c。这正是「快路径极短」的架构原因。

**练习 2**：`free.c` 里的释放为什么不需要经过 `alloc.c`，却能使用其中的内联函数？

答案：方向反了——是 `alloc.c` include 了 `free.c`（[src/alloc.c:20-23](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L20-L23)），两者处于同一翻译单元，`free.c` 反过来也能用 `internal.h` 里的公共内联辅助函数。文件层面的「谁包含谁」只为内联服务，不代表调用方向的从属关系。

**练习 3**：判断对错：`page.c` 中创建的新页，其内存一定来自 arena。

答案：错。arena 有容量上限与对象尺寸上限（见 [src/arena.c:116-128](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L116-L128) 的 `mi_arena_max_object_size`），超出限制的巨型对象、或 arena 空间耗尽时，会直接向 OS 申请（走 os.c → prim 层）。u4-l3、u6 会详细展开。

### 4.3 include/mimalloc/：types.h 是所有核心结构体的家

#### 4.3.1 概念说明

`include/mimalloc/` 子目录下有 7 个内部头文件，最重要的是 `types.h`（804 行）：**全部分配器核心数据结构集中定义在这一个文件**。其余几个一句话带过：`internal.h`（内部函数与常量，1503 行，第二大）、`atomic.h`（跨编译器原子操作封装）、`prim.h` / `prim-tls.h`（OS/TLS 原语接口，见 4.4）、`bits.h`（位运算与对齐辅助）、`track.h`（valgrind/ASAN 插桩挂钩）。

读源码时「先认结构、再读流程」效率最高，而认结构的入口就是 types.h。

#### 4.3.2 核心流程

types.h 里的结构体构成一条所有权链（v3 的四层模型，u3-l1 将精读）：

```text
mi_subproc_t                     子进程（最外层隔离域，CPython 多解释器用）
   └── mi_heap_t                 一等堆：用户可显式创建/销毁
         └── mi_theap_t          线程本地堆：每线程一份，快路径只碰它
               └── mi_page_queue_t   按 size class 组织的页队列（pages[MI_BIN_COUNT]）
                     └── mi_page_t   页：只装一种 block_size 的块
                           └── mi_block_t  块：malloc 返回给用户的最小单位

旁路：mi_arena_t  ← 页/堆的底层内存来自它（或直接来自 OS）
      mi_memid_t  ← 每块内存的「产地证明」，嵌在 page/theap/heap/arena 里
```

#### 4.3.3 源码精读

- [include/mimalloc/types.h:288-297](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L288-L297)：`mi_memkind_t` 枚举——内存可能来自 arena、OS、外部提供、静态区等 8 种来源。mimalloc 用 `mi_memid_t` 追踪每块内存的出处。
- [include/mimalloc/types.h:326-336](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L326-L336)：`mi_memid_s`——「内存的产地证明」结构：一个 union 按来源记录 arena 切片号或 OS 基址，外加三个布尔标志（是否 pinned、是否已 commit、是否清零）。释放时必须按产地选择正确的归还方式，所以几乎每个结构体里都有一个 `memid` 字段。
- [include/mimalloc/types.h:366-368](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L366-L368)：`mi_block_s`——整个分配器里最小的结构体，只有一个 `next` 字段：空闲块本身充当链表节点（复用块内的第一个指针位），不额外耗内存。
- [include/mimalloc/types.h:399-424](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L399-L424)：**全文件最有价值的注释块**。讲清了每页三条 free list（`free` 可分配 / `local_free` 本线程延迟 / `thread_free` 跨线程）的分工、不变式 `used - |thread_free| + |free| + |local_free| == capacity`、以及「所有权位编码在 xthread_free 最低位」这一无锁设计。u3-l2、u5、u8 都从这段注释出发。
- [include/mimalloc/types.h:425-456](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L425-L456)：`mi_page_s` 本体。注意注释 [types.h:423](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L423)「字段布局为 free.c 的 mi_free 和 alloc.c 的 mi_page_alloc 优化」——数据结构摆放顺序本身是为快路径的缓存行局部性服务的。
- [include/mimalloc/types.h:509-521](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L509-L521)：theap 概念注释——「theap 是拥有页的线程本地堆（因此避免原子操作）」。v3 相对旧版新增的这一层的动机一句话就说完了。
- [include/mimalloc/types.h:561-598](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L561-L598)：`mi_theap_s`。第一个字段就是快路径的秘诀：`pages_free_direct[]` 直接索引数组——小对象按尺寸一步取到候选页（[types.h:563](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L563) 的注释）；末尾的 `pages[MI_BIN_COUNT]`（[types.h:595](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L595)）即 4.3.2 图中的页队列数组。
- [include/mimalloc/types.h:618-638](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L618-L638)：`mi_heap_s`——一等堆：持有属于它的 theap 链表、在各 arena 中的页位图（`arena_pages`，[types.h:635](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L635)）、专属 arena 与 NUMA 偏好。
- [include/mimalloc/types.h:731-758](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L731-L758)：`mi_arena_s`——arena 本体：起始地址、slice 总数、四张位图（`slices_free/committed/dirty/purge`，[types.h:749-752](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L749-L752)）分别记录每个 64KiB slice 的空闲/已提交/脏/待清理状态。u6-l3 精读。

#### 4.3.4 代码实践

1. **实践目标**：用 grep 在 types.h 里建立「结构体 → 行号」索引，作为后续单元的查询表。
2. **操作步骤**（待本地验证）：
   ```bash
   grep -n "^typedef struct mi_\|^struct mi_" include/mimalloc/types.h
   ```
3. **需要观察的现象**：输出应包含 `mi_memid_s`、`mi_block_s`、`mi_page_s`、`mi_page_queue_s`、`mi_theap_s`、`mi_heap_s`、`mi_arena_s` 等定义行号，与 4.3.3 给出的行号一致（366、425、561、618、731...）。
4. **预期结果**：把这张索引表记进笔记。以后读任何 .c 文件遇到陌生结构体，先跳到 types.h 对应行看字段注释，再回去读流程，效率高得多。

#### 4.3.5 小练习与答案

**练习 1**：`mi_block_t` 只有一个字段，为什么这样就够释放时使用？

答案：空闲块不需要携带任何元数据——它属于哪个页由**地址**通过 page map 反查得出（[src/free.c:251-256](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L251-L256)），块大小由页的 `block_size` 给出；块自身只需要在空闲时串进 free list，所以一个 `next` 足矣。把元数据集中在页而非每块，是小内存分配器省内存的关键手段。

**练习 2**：`mi_memid_t` 里的 `initially_zero` 标志有什么用？

答案：OS 新给的内存（如 mmap）保证是零页。记下「这块内存生来为零」，后续 `mi_zalloc` 就可以跳过 memset（对照 [src/alloc.c:95-102](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L95-L102) 的 `free_is_zero` 分支），省掉一次整块写内存。

### 4.4 src/prim/：OS 抽象层为何按平台子目录组织

#### 4.4.1 概念说明

分配器最终必须向操作系统要内存、要线程号、要线程本地存储——而这些系统调用每个 OS 都不同（Linux 用 `mmap`，Windows 用 `VirtualAlloc`，WASM 环境甚至没有传统意义的 mmap）。如果这些调用散落在核心逻辑里，代码会到处是 `#ifdef _WIN32`。

mimalloc 的解法是**接口/实现分离**：

- `include/mimalloc/prim.h`：定义一组 `_mi_prim_*` 前缀的**接口**（内存映射、commit、NUMA、时间等）；
- `src/prim/<平台>/prim.c`：各平台的**实现**；
- `src/prim/prim.c`：一个选择器，按编译目标平台把唯一一个实现的符号暴露给上层。

于是 `arena.c`、`os.c` 等核心文件只 include `prim.h` 调接口，永远不出现平台宏。**移植到新平台 = 新增一个子目录实现这组接口**，核心逻辑零改动。

#### 4.4.2 核心流程

```text
                     核心逻辑（os.c / arena.c / ...）
                            │  只调用 _mi_prim_* 接口
                            ▼
                include/mimalloc/prim.h   ←接口（含详细的契约注释）
                            │  由链接期唯一实现满足
                            ▼
      src/prim/prim.c（选择器，按平台编译进一个实现）
         ├── src/prim/unix/prim.c     Linux/BSD 等（macOS 经 osx/ 转发）
         ├── src/prim/windows/prim.c  Windows（还有 ETW 追踪资源）
         ├── src/prim/osx/prim.c      macOS（含 malloc zone 覆盖）
         ├── src/prim/wasi/prim.c     WASI
         └── src/prim/emscripten/prim.c  Emscripten

TLS 同理：prim-tls.h（接口）+ src/prim/prim-tls.c（多策略实现）
```

#### 4.4.3 源码精读

- [src/prim/readme.md:1-11](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/readme.md#L1-L11)：官方自述——「这是可移植性层，定义了所有需要从 OS 获得的原语」，并列出 prim.h（接口）/prim.c（按宿主平台选择 unix、wasi 或 windows 实现，macOS 由 osx/ 转发到 unix/）的分工；结尾坦承「仍在进展中，源码里可能还有依赖 OS ifdef 的地方」。11 行读完整个 prim 层。
- [include/mimalloc/prim.h:13-21](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim.h#L13-L21)：接口文件的契约注释——每个 OS/宿主需要实现这些原语，并规定了所有原语函数的统一约定（出参非空、地址页对齐、返回 int 错误码 0 为成功）。
- [include/mimalloc/prim.h:25-35](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim.h#L25-L35)：`mi_os_mem_config_t`——启动时各平台实现要填写的「硬件能力问卷」：页大小、大页大小、分配粒度、物理内存、虚拟地址位数、是否支持 overcommit / 部分释放 / 虚拟预留等。核心逻辑据这些能力决定策略（例如 Windows 分配粒度是 64KiB 而非 4KiB）。
- [include/mimalloc/prim.h:38-51](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/prim.h#L38-L51)：两个代表性接口——`_mi_prim_mem_init`（填写上面的问卷）与 `_mi_prim_alloc`（申请 OS 内存；注释详细说明了 `commit=false` 时只需 reserve、`is_zero` 出参等语义，这些正是 u6-l2 要讲的 reserve/commit 两段式）。
- 对照 [src/prim/unix/prim.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/unix/prim.c) 与 [src/prim/windows/prim.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/windows/prim.c)：两个文件里各找 `_mi_prim_alloc` 的定义，就能看到 `mmap` 与 `VirtualAlloc` 的逐条对应关系（u6-l1 的实践任务正是做这张对照表）。

#### 4.4.4 代码实践

1. **实践目标**：验证「核心逻辑不出现平台宏」这一架构主张。
2. **操作步骤**（待本地验证）：
   ```bash
   grep -c "_WIN32\|__APPLE__" src/arena.c src/page.c src/alloc.c src/free.c
   grep -n "_mi_prim_alloc\|_mi_prim_commit" src/prim/unix/prim.c | head -5
   ```
3. **需要观察的现象**：第一条命令在四个核心文件里的命中数应为 0 或个位数（prim/readme.md 也承认还有少量残留）；第二条能在 unix 实现里定位到接口函数。
4. **预期结果**：平台差异确实被压到了 `src/prim/` 与 `os.c` 等少数位置；你之后读 arena.c/page.c 时可以放心假定代码平台无关。

#### 4.4.5 小练习与答案

**练习 1**：如果要支持一个全新的嵌入式 OS，按本讲的理解需要动哪些文件？

答案：新增 `src/prim/<新平台>/prim.c` 实现 `prim.h` 的全部 `_mi_prim_*` 接口（以及视平台情况实现 `prim-tls` 策略），修改选择器 `src/prim/prim.c` 与 CMake 配置把它编进来。`alloc.c/page.c/arena.c/types.h` 等核心逻辑不动。

**练习 2**：`mi_os_mem_config_t` 里的 `alloc_granularity`（分配粒度）为什么 Windows 上是 64KiB 而不是 4KiB？

答案：Windows 的 `VirtualAlloc` 以 64KiB 粒度对齐分配地址（页权限仍以 4KiB 为单位），而 Linux `mmap` 可以按页返回地址。分配器必须尊重每个 OS 的真实粒度来对齐地址，否则 reserve 的地址会与预期不符——这正是「能力问卷」存在的意义。此为 Windows API 的公开行为，细节留待 u6-l1 对照两个实现时验证。

### 4.5 test/：示例工程与测试的双重身份

#### 4.5.1 概念说明

`test/` 有独立于主工程的 `CMakeLists.txt`（u1-l2 已讲过四种链接方式），它同时承担两个角色：测试 mimalloc，以及**充当官方使用示例**。文件不多，但每个都有明确的教学指向：

| 文件 | 指向 |
| --- | --- |
| `main.c` | 安装后的基础链接 + 一等堆等 mi_ API 用法（u1-l4 的素材） |
| `main-override*.c/cpp`、`main-static*.cpp` | 验证 LD_PRELOAD / 静态覆盖等各接入方式（u2 的素材） |
| `test-api.c` | API 表面测试（`make test` 跑的就是它） |
| `test-stress.c` / `test-stress-heaps.c` / `test-stress-subprocs.c` | 多线程压测：线程、堆、子进程（u7 的素材） |
| `test-wrong.c` | 故意的错误用法（double free、越界），验证检查机制（u9 的素材） |

#### 4.5.2 核心流程

测试策略是「双保险」：

1. **内部不变式检查**：debug 构建下每个页、每条链表随时被校验（如 [src/page.c:84-120](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L84-L120) 的 `mi_page_is_valid_init` 检查 `used <= capacity <= reserved`、三条链表上每个块都落在页范围内等）；
2. **外部压测**：把真实基准程序（mimalloc-bench）跑在开着不变式检查的构建上。

#### 4.5.3 源码精读

- [test/readme.md:1-15](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/readme.md#L1-L15)：官方测试哲学——「测试分配器很难，bug 只在特定分配序列下暴露；因此主要手段是广泛的内部不变式检查（举例 `page.c` 的 `page_is_valid`），再用 mimalloc-bench 配合全量检查跑各种高强度程序」，并说明 `test-api.c` 补充 API 表面覆盖。
- [src/page.c:84-120](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L84-L120)：`mi_page_is_valid_init`——上面说的不变式检查实现，[src/page.c:116-117](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L116-L117) 的断言 `used + free_count == capacity` 正是 [types.h:408-409](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L408-L409) 注释里那条不变式的运行时验证。

#### 4.5.4 代码实践

1. **实践目标**：为 5（综合实践）的依赖草图补上 test/ 的「素材库」标注。
2. **操作步骤**：打开 `test/` 下任一 `test-stress-*.c`，只看 `main` 函数前 30 行，记下它用到哪些 `mi_` API（如 `mi_heap_new`、`mi_thread_create`）。
3. **需要观察的现象**：这些测试恰好就是后续单元（u7 一等堆、u7-l4 subproc）的现成示例代码。
4. **预期结果**：你的地图上，test/ 不只是「测试」，而是「按主题索引的可运行示例」。
5. 本实践无需运行，纯阅读。

#### 4.5.5 小练习与答案

**练习**：`test-wrong.c` 里故意 double free 的用例，在 release 构建下可能根本不报错，为什么还有价值？

答案：它面向的是 debug/secure/guarded 构建——那些构建里 padding 校验、canary（[types.h:546-550](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L546-L550) 的 `mi_padding_s`）等机制会把错误当场抓住。u9-l1/l2 会用不同构建模式跑它观察差异。

## 5. 综合实践：绘制你自己的模块依赖草图

这是本讲的核心产出，也是规格指定的实践任务。完成后请保留，后续单元会不断在图上添加注记。

**任务**：手工绘制一张 mimalloc 模块依赖草图，并标注「分配主链路」。

**步骤**：

1. **列文件**：浏览 `src/` 下每个 `.c` 文件顶部注释与主要函数名（4.1.4 的方法），把 19 个文件名抄在纸/白板上。
2. **分层摆放**：按 4.2.1 的漏斗分层——顶层 `alloc.c`/`free.c`/`alloc-aligned.c`/`alloc-posix.c`（入口层），中层 `page.c`/`page-queue.c`/`page-map.c`/`theap.c`/`heap.c`/`subproc.c`（对象管理层），下层 `arena.c`/`bitmap.c`/`os.c`（内存供给层），旁路 `init.c`/`options.c`/`stats.c`/`random.c`/`libc.c`/`threadlocal.c`（支撑设施），最底 `prim/`（OS 层）。
3. **画依赖箭头**：用 grep 验证每条你画出的箭头，例如（待本地验证）：
   ```bash
   grep -n '#include "' src/page.c src/arena.c src/alloc.c
   grep -n "_mi_arenas_alloc\|_mi_page_meta_alloc" src/page.c
   ```
4. **标主链路**：用粗线标出 `mi_malloc` 的文件穿越顺序：`alloc.c`（[L256](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L256)）→ 快路径 [L41-57](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L41-L57)；空则 → `page.c` [`_mi_malloc_generic` L1091](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L1091) → [`mi_page_fresh` L344](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L344) → `arena.c` [`_mi_arenas_alloc` L618](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L618)。再用另一种颜色标释放链路：`free.c`（[L251](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L251)）→ `page-map.c` 反查页 → 本地/跨线程分流。
5. **标注数据结构**：在对应文件旁写上它的核心结构体（都来自 `types.h`）：theap.c→`mi_theap_t`、page.c→`mi_page_t`、arena.c→`mi_arena_t`。
6. **验证与预言**：在图下方写下你的两个「预言」，留到后续单元验证：(a) u3-l1 会证明所有权链 subproc→heap→theap→page queue→page→block 与你的分层一致；(b) u4-l1 精读快路径时会数出它确实只有几次内存访问。

**预期结果**：一张被 grep 证据支撑的依赖草图 + 两个可验证的预言。当你之后任何时刻在源码里「迷路」，回到这张图定位自己在哪一层。

## 6. 本讲小结

- 仓库四大块：`include/`（公共 API + 内部头）、`src/`（核心实现 + `prim/` 平台层）、`test/`（测试兼官方示例）、构建配置；`src/static.c` 的 include 清单是全库文件的一览表。
- 分配主链路按文件分层漏斗下沉：`alloc.c`（入口与快路径）→ `page.c`（慢路径与页管理）→ `arena.c`（内存批发），释放则从 `free.c` 出发经 page map 反查页后按本线程/跨线程分流。
- `include/mimalloc/types.h` 集中定义全部核心结构体：`mi_block_t`(L366)、`mi_page_t`(L425)、`mi_theap_t`(L561)、`mi_heap_t`(L618)、`mi_arena_t`(L731)，其注释是理解设计的首选材料。
- mimalloc 用「一个 .c include 另一个 .c」合并翻译单元（alloc.c⊃free.c、page.c⊃page-queue.c），以保证快路径完全内联。
- `src/prim/` 以「prim.h 接口 + 平台子目录实现 + prim.c 选择器」把 OS 差异隔离在核心逻辑之外，新平台移植只需实现这一层。
- `test/` 的双保险策略：debug 构建的内部不变式检查（如 page.c 的 `mi_page_is_valid_init`）+ 外部基准压测；test 文件同时是按主题索引的示例库。

## 7. 下一步学习建议

下一讲（u1-l4）将第一次真正写代码：用 `include/mimalloc.h` 的 `mi_malloc/mi_zalloc/mi_calloc/mi_strdup` 完成一次完整分配周期，并用 `MIMALLOC_SHOW_STATS=1` 观察统计输出——把你本讲标好的主链路和输出里的 bin 统计对应起来。

在进入 u3（核心数据结构单元）之前，建议提前通读 [include/mimalloc/types.h:399-424](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L399-L424) 关于三条 free list 的注释至少三遍：它是整个 v3 设计（free list 分片、无锁跨线程释放、所有权位）的浓缩，u3/u5/u8 三个单元都在反复展开这一段话。

另外可以顺手读两份小文档作为本讲的延伸：[src/prim/readme.md](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/prim/readme.md)（prim 层自述）与 [test/readme.md](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/readme.md)（测试哲学）。
