# 内存池 ngx_pool_t

## 1. 本讲目标

nginx 几乎所有运行时内存（请求结构、配置、缓冲、连接上下文……）都来自同一个机制：**内存池**。本讲学完后，你应该能够：

- 说清 `ngx_create_pool` / `ngx_palloc` / `ngx_pfree` / `ngx_destroy_pool` 各自做了什么。
- 区分「小块（small）」与「大块（large）」两条分配路径，并能解释为什么要有这两条。
- 理解 cleanup 回调链如何在销毁内存池时自动释放文件描述符等「非内存」资源。
- 在阅读后续讲义（请求生命周期、配置解析、upstream 等）时，看到 `ngx_palloc` / `ngx_pcalloc` 不再感到陌生，而是能立刻判断这块内存的存活范围。

内存池是读懂 nginx 一切上层模块的前提，所以本讲是 core 基础设施的第一讲。

## 2. 前置知识

### 2.1 为什么不用裸 malloc/free

一个 HTTP 请求在 nginx 里会动态创建几十上百个小对象（请求行解析结果、头部键值对、变量值、过滤链节点……）。如果每个对象都调一次 `malloc`、用完再 `free`，会有两个问题：

1. **内存碎片**：大量小对象散落在堆里，长时间运行后分配大块会越来越难。
2. **生命周期管理负担**：请求结束时，你必须记住「每一个」曾经分配的对象并逐个释放，漏一个就内存泄漏，多释放一个就崩溃。

nginx 用内存池同时解决这两个问题：

- **碎片问题** → 把一大块内存当成「竞技场（arena）」，小对象在里面做「指针前移（bump）」式的 O(1) 分配，整块整块地用，不再产生细碎空闲。
- **生命周期问题** → 一个请求对应一个内存池，请求结束时只需 `ngx_destroy_pool` 一次性把整池回收，**不需要也不允许**逐个释放小对象。

### 2.2 对齐（alignment）是什么

CPU 访问「对齐」的地址（例如 8 字节整数放在 8 的倍数地址上）更快，某些平台上访问未对齐地址甚至会触发总线错误。因此 nginx 在分配时会按平台字长（`NGX_ALIGNMENT = sizeof(uintptr_t)`，64 位平台为 8）对齐返回地址。对齐计算的位运算技巧会在源码精读里看到。

### 2.3 与上一讲的衔接

上一讲（u1-l4）我们看到 `main()` 在启动早期就调用 `ngx_create_pool` 建立初始 cycle 的内存池（[src/core/nginx.c:254](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L254)）。本讲就回答：这个池子建出来长什么样，后续 `ngx_palloc` 又是怎么从里面取内存的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/core/ngx_palloc.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_palloc.h) | 内存池的数据结构定义（`ngx_pool_t`、`ngx_pool_data_t`、`ngx_pool_large_t`、`ngx_pool_cleanup_t`）与对外 API 声明。 |
| [src/core/ngx_palloc.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_palloc.c) | 全部内存池逻辑实现：创建、分配（small/large）、释放、reset、cleanup 注册与销毁。 |
| [src/core/ngx_config.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_config.h) | `NGX_ALIGNMENT` 与对齐宏 `ngx_align` / `ngx_align_ptr`。 |
| [src/os/unix/ngx_alloc.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_alloc.c) | 对系统 `malloc` / `posix_memalign` 的薄封装 `ngx_alloc` / `ngx_memalign`，是内存池向操作系统要内存的最终入口。 |

本讲聚焦三个最小模块：

1. `ngx_create_pool` —— 池子的创建与字段初始化。
2. `ngx_palloc` → `ngx_palloc_small` / `ngx_palloc_large` —— 两条分配路径。
3. `ngx_pool_cleanup_add` 与 `ngx_destroy_pool` —— cleanup 回调链与生命周期回收。

## 4. 核心概念与源码讲解

### 4.1 内存池的数据结构与创建 ngx_create_pool

#### 4.1.1 概念说明

一个内存池 `ngx_pool_t` 不是一个「只装内存」的盒子，它内部其实管理着**三条链表**：

- **block 链（小块竞技场）**：由 `d.next` 串起来的一串内存块，小对象从这里 bump 分配。
- **large 链**：`ngx_pool_large_t` 节点链，每个节点指向一个独立 `malloc` 出来的大块。
- **cleanup 链**：`ngx_pool_cleanup_t` 节点链，记录销毁时要调用的资源回收回调。

外加几个管理字段：`max`（区分大小块的阈值）、`current`（小块分配从哪个块开始搜索）、`log`（用于记日志）。

把这三条链装进一个结构体，就是「内存池」。

#### 4.1.2 核心流程

创建一个池子的步骤：

1. 向操作系统要一整块 `size` 大小的内存（对齐到 16 字节）。
2. 把开头的一小段（`sizeof(ngx_pool_t)`）用作池子头部，填好各字段。
3. `d.last` 指向头部之后的空闲起点；`d.end` 指向整块末尾。
4. 计算 `max = min(size - 头部开销, NGX_MAX_ALLOC_FROM_POOL)`，作为「小块」的上限。
5. `current` 指向自己，三条链全部初始化为空。

#### 4.1.3 源码精读

先看结构体定义。[src/core/ngx_palloc.h:57-65](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_palloc.h#L57-L65) 是池子本体：

```c
struct ngx_pool_s {
    ngx_pool_data_t       d;       // 当前块的 last/end/next/failed
    size_t                max;     // 小块上限，超过就走 large
    ngx_pool_t           *current; // 小块分配的搜索起点（优化用）
    ngx_pool_t           *chain;   // buf 链节点回收（见 u2-l4，本讲略）
    ngx_pool_large_t     *large;   // 大块链表头
    ngx_pool_cleanup_t   *cleanup; // cleanup 回调链表头
    ngx_log_t            *log;
};
```

其中小块竞技场的逐块信息在 [src/core/ngx_palloc.h:49-54](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_palloc.h#L49-L54)：

```c
typedef struct {
    u_char       *last;     // 当前块空闲区起点
    u_char       *end;      // 当前块末尾
    ngx_pool_t   *next;     // 下一块（串成 block 链）
    ngx_uint_t    failed;   // 本块分配失败累计次数
} ngx_pool_data_t;
```

注意 `last` / `end` 是 bump 分配的两个关键指针：分配就是把 `last` 往前推。

两个宏常量决定了池子的默认规模，见 [src/core/ngx_palloc.h:20-24](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_palloc.h#L20-L24)：`NGX_MAX_ALLOC_FROM_POOL = ngx_pagesize - 1`（x86 上是 4095），`NGX_DEFAULT_POOL_SIZE = 16 * 1024`（16KB），`NGX_POOL_ALIGNMENT = 16`。

创建逻辑在 [src/core/ngx_palloc.c:18-43](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_palloc.c#L18-L43)：

```c
ngx_pool_t *
ngx_create_pool(size_t size, ngx_log_t *log)
{
    ngx_pool_t  *p;

    p = ngx_memalign(NGX_POOL_ALIGNMENT, size, log);  // 整块对齐分配
    if (p == NULL) {
        return NULL;
    }

    p->d.last = (u_char *) p + sizeof(ngx_pool_t);    // 空闲区从头部之后开始
    p->d.end  = (u_char *) p + size;
    p->d.next = NULL;
    p->d.failed = 0;

    size = size - sizeof(ngx_pool_t);
    p->max = (size < NGX_MAX_ALLOC_FROM_POOL) ? size : NGX_MAX_ALLOC_FROM_POOL;
    //    ↑ max = min(可用空间, 页大小-1)

    p->current = p;       // 搜索起点 = 自己
    p->chain = NULL;
    p->large = NULL;
    p->cleanup = NULL;
    p->log = log;

    return p;
}
```

`ngx_memalign` 最终落在 `posix_memalign`（[src/os/unix/ngx_alloc.c:51-69](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_alloc.c#L51-L69)），保证整块起始地址按 16 对齐。`max` 的取值规则意味着：对一个默认 16KB 的池子，`size - 头部 ≈ 16KB` 远大于 4095，所以 `max` 就被钳到 **4095**；而对 `ngx_create_pool(256, ...)` 这种小池子，`max ≈ 256 - 80`（约 176，平台相关），不会再钳到 4095。

> **小提示**：`NGX_MIN_POOL_SIZE`（[src/core/ngx_palloc.h:25-27](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_palloc.h#L25-L27)）保证传入的 `size` 至少能装下头部加两个 large 节点，否则池子根本没法用。

#### 4.1.4 代码实践

**实践类型：源码阅读 + 计算。**

1. **实践目标**：建立「池子尺寸 → `max` 阈值」的直觉。
2. **操作步骤**：
   - 用 `grep -n "ngx_create_pool(" src/` 列出所有创建点（本讲已列出代表性几处）。
   - 阅读这几处调用各自传入的尺寸：
     - [src/core/nginx.c:254](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L254) 传 `1024`。
     - [src/core/ngx_cycle.c:69](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L69) 传 `NGX_CYCLE_POOL_SIZE`（即 `NGX_DEFAULT_POOL_SIZE` = 16384）。
     - [src/http/ngx_http_request.c:583](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L583) 传 `cscf->request_pool_size`，默认 `4096`（见 [src/http/ngx_http_core_module.c:3571-3572](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L3571-L3572)）。
   - 对每个尺寸套用 `max = min(size - sizeof(ngx_pool_t), 4095)` 手算 `max`。
3. **需要观察的现象**：1024 的小池子 `max` 远小于 4095；16KB 池子 `max` 恰好被钳到 4095。
4. **预期结果**：
   - `ngx_create_pool(1024, ...)` → `max ≈ 1024 - 80 ≈ 944`（64 位平台，约值）。
   - `ngx_create_pool(16384, ...)` → `max = 4095`（被钳）。
   - `ngx_create_pool(4096, ...)` → `max = 4096 - 80` 与 4095 比较，约 4016 < 4095，故 `max ≈ 4016`。
5. 精确字节数「待本地验证」（取决于 `sizeof(ngx_pool_t)` 的实际值），但「小池不被钳、大池被钳到 4095」这一结论是确定的。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `NGX_MAX_ALLOC_FROM_POOL` 要等于 `ngx_pagesize - 1` 而不是更大？
  - **参考答案**：一个 block 的可用空间如果超过一页，分配时容易跨越多页、增加内核锁页/换页的代价；限制在一页内能让每个小块分配尽量落在同一物理页，注释（[src/core/ngx_palloc.h:16-19](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_palloc.h#L16-L19)）也指出在 Windows 上可减少内核锁定页数。
- **练习 2**：`current` 字段为什么不在创建时直接指向 `NULL`，而是指向自己？
  - **参考答案**：`current` 是小块分配的搜索起点（见 4.2），它必须始终指向一个「可能还能分配」的 block；创建时第一块就是可用的，所以指向自己。

---

### 4.2 两条分配路径：ngx_palloc_small 与 ngx_palloc_large

#### 4.2.1 概念说明

`ngx_palloc` 是最常用的分配入口。它面对一次请求要做的第一件事，是**按尺寸分流**：

- 请求大小 `≤ pool->max` → 走 **small 路径**：在 block 链里 bump 分配，O(1) 指针前移，不调 `malloc`，不记元数据。
- 请求大小 `> pool->max` → 走 **large 路径**：单独 `malloc` 一块，再用一个 `ngx_pool_large_t` 节点把它登记到 large 链上，便于将来整体释放。

这种分流是 nginx 内存池的核心设计：**常见的、数量多的小对象走快路径；偶发的、单个很大的对象（比如一个大缓冲）才去打扰系统 malloc。**

#### 4.2.2 核心流程

small 路径（`ngx_palloc_small`）：

```
从 pool->current 指向的块开始
循环遍历 block 链：
    计算对齐后的起点 m
    若 (end - m) >= size：   # 本块装得下
        last = m + size      # 指针前移
        return m             # 直接返回，结束
否则去下一块
若所有块都装不下 → ngx_palloc_block 开新块
```

开新块（`ngx_palloc_block`）的额外逻辑：

- 新块大小 = 第一个块的 `psize`（与初始池子同尺寸，保持块大小一致）。
- 新块只放 `ngx_pool_data_t` 头部（比 `ngx_pool_t` 小，因为后续块不需要 `max/current/large/cleanup/log`）。
- 遍历现有 block，把每块的 `failed++`；**若某块 `failed > 4`，就把 `current` 推到下一块**——这是「跳过基本满了的块」的优化。
- 把新块接到链尾，返回新块里的内存。

large 路径（`ngx_palloc_large`）：

```
p = ngx_alloc(size)          # 普通 malloc
在 large 链前 4 个节点里找有没有 alloc==NULL 的空位（复用已释放的节点）
若有：占住该节点的 alloc = p，return p
否则：从 small 区分配一个新 ngx_pool_large_t 节点，头插到 large 链
```

对齐的位运算（[src/core/ngx_config.h:96-102](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_config.h#L96-L102)）：

```c
#define NGX_ALIGNMENT   sizeof(uintptr_t)            // 平台字长，64 位为 8
#define ngx_align(d, a)     (((d) + (a - 1)) & ~(a - 1))
#define ngx_align_ptr(p, a) (u_char *)(((uintptr_t)(p) + (a-1)) & ~((uintptr_t)a-1))
```

其数学含义是把地址向上取整到 `a` 的倍数（要求 `a` 是 2 的幂）：

\[
\mathrm{align}(p, a) = \left\lceil p / a \right\rceil \times a = ((p + a - 1)\ \&\ \sim(a-1))
\]

位掩码 `~(a-1)` 能把「余数」清零，这正是 2 的幂次对齐的标准技巧。

#### 4.2.3 源码精读

分流入口 [src/core/ngx_palloc.c:122-132](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_palloc.c#L122-L132)：

```c
void *
ngx_palloc(ngx_pool_t *pool, size_t size)
{
#if !(NGX_DEBUG_PALLOC)
    if (size <= pool->max) {
        return ngx_palloc_small(pool, size, 1);   // 1 = 需要对齐
    }
#endif
    return ngx_palloc_large(pool, size);
}
```

> `NGX_DEBUG_PALLOC` 是调试开关：打开后**所有**分配都走 large，便于用 valgrind 等工具逐块追踪内存错误。`ngx_pnalloc`（[src/core/ngx_palloc.c:135-145](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_palloc.c#L135-L145)）与 `ngx_palloc` 唯一区别是传 `align=0`，用于字符串等不需要对齐的场景。

small 路径 [src/core/ngx_palloc.c:148-174](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_palloc.c#L148-L174)：

```c
static ngx_inline void *
ngx_palloc_small(ngx_pool_t *pool, size_t size, ngx_uint_t align)
{
    u_char      *m;
    ngx_pool_t  *p;

    p = pool->current;            // 从 current 开始搜索

    do {
        m = p->d.last;
        if (align) {
            m = ngx_align_ptr(m, NGX_ALIGNMENT);
        }
        if ((size_t) (p->d.end - m) >= size) {   // 本块够装？
            p->d.last = m + size;                 // 指针前移 = 分配
            return m;
        }
        p = p->d.next;
    } while (p);

    return ngx_palloc_block(pool, size);          // 都不够，开新块
}
```

开新块与 `failed` 优化在 [src/core/ngx_palloc.c:177-210](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_palloc.c#L177-L210)，关键 4 行：

```c
for (p = pool->current; p->d.next; p = p->d.next) {
    if (p->d.failed++ > 4) {        // 本块累计失败超过 4 次
        pool->current = p->d.next;  //   就让 current 跳过它
    }
}
p->d.next = new;                    // 新块接尾
```

阈值 `4` 的含义：一个块连续 5 次满足不了分配请求，就被认定为「实际已满」，后续 `ngx_palloc_small` 直接从更靠后的块开始搜，省去一次次徒劳的 `end - m` 比较。

large 路径 [src/core/ngx_palloc.c:213-249](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_palloc.c#L213-L249)：

```c
p = ngx_alloc(size, pool->log);     // 普通 malloc 大块
...
for (large = pool->large; large; large = large->next) {
    if (large->alloc == NULL) {     // 复用已释放的 large 节点
        large->alloc = p;
        return p;
    }
    if (n++ > 3) {                  // 最多看前 4 个，避免链太长时遍历
        break;
    }
}
large = ngx_palloc_small(pool, sizeof(ngx_pool_large_t), 1); // 新节点本身来自 small 区
large->alloc = p;
large->next = pool->large;          // 头插
pool->large = large;
```

这里有个精妙处：**登记大块用的 `ngx_pool_large_t` 节点本身，也是从 small 区分配的**（只有十几个字节），所以 large 路径天然复用了 small 路径。

#### 4.2.4 代码实践

**实践类型：源码阅读 + 思想验证。**

1. **实践目标**：理解 `max` 如何决定一次分配走哪条路，以及 `current` 优化何时生效。
2. **操作步骤**：
   - 假设一个用默认 16KB 创建的池子，则 `max = 4095`。
   - 推演以下三次调用的路径：
     - `ngx_palloc(pool, 100)` → 100 ≤ 4095 → small，从 `current` 块 bump。
     - `ngx_palloc(pool, 5000)` → 5000 > 4095 → large，走 `ngx_alloc`（malloc）。
     - 连续多次 `ngx_palloc(pool, 4000)`，当某块累计装不下 5 次 → `failed > 4` → `current` 前移。
   - 打开 [src/core/ngx_palloc.c:213-249](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_palloc.c#L213-L249)，确认「large 节点来自 small 区」这一复用关系。
3. **需要观察的现象**：small 分配不产生任何 `malloc` 调用；large 分配每次产生一次 `malloc`。
4. **预期结果**：能用自己的话回答「为什么 nginx 内存池在压测时 malloc 调用次数远小于对象数量」——因为绝大多数对象走 small 的 bump 分配。
5. 关于「`failed` 阈值具体何时把 current 推走」的精确触发点「待本地验证」（需构造刚好把某块填满的分配序列）。

#### 4.2.5 小练习与答案

- **练习 1**：`ngx_palloc` 与 `ngx_pcalloc`（[src/core/ngx_palloc.c:297-308](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_palloc.c#L297-L308)）有什么区别？
  - **参考答案**：`ngx_pcalloc = ngx_palloc + ngx_memzero`，即分配后把内存清零。结构体分配通常用它，省去手动 memset。
- **练习 2**：为什么 large 路径搜索空节点时只看前 4 个就 `break`？
  - **参考答案**：large 链可能很长，遍历整条链会成为性能瓶颈；只看前几个既大概率能命中刚释放的空位，又把最坏情况控制在常数时间。找不到再分配新节点即可，不会浪费正确性。
- **练习 3**：小块为什么**不**支持单独 `ngx_pfree` 释放？
  - **参考答案**：bump 分配没有为每个对象保留长度等元数据，无法把单个对象「还回」空闲区。`ngx_pfree`（[src/core/ngx_palloc.c:277-294](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_palloc.c#L277-L294)）只在 large 链里找匹配的 `alloc` 指针，对小块一律返回 `NGX_DECLINED`。小块的回收靠整池销毁。

---

### 4.3 cleanup 回调链与销毁 ngx_destroy_pool

#### 4.3.1 概念说明

内存池管的不只是「内存」。一个请求过程中可能打开临时文件、建立到后端的连接、注册 SSL 上下文——这些资源不是 `free` 能释放的。nginx 的做法是：**在池子上挂一条 cleanup 链**，每个节点记录一个回调 `handler` 和它的参数 `data`；销毁池子时按链依次调用这些回调，资源就随池子一起回收。

cleanup 节点本身（[src/core/ngx_palloc.h:34-38](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_palloc.h#L34-L38)）也分配在池子里：

```c
struct ngx_pool_cleanup_s {
    ngx_pool_cleanup_pt   handler;  // 回调函数
    void                 *data;     // 回调参数
    ngx_pool_cleanup_t   *next;     // 下一节点
};
```

这就是内存池成为 nginx 「对象生命周期中心」的根本原因：**任何随请求存活、随请求消亡的资源，都通过 cleanup 挂在请求池上。**

#### 4.3.2 核心流程

注册 cleanup（`ngx_pool_cleanup_add`）：

```
从池子分配一个 cleanup 节点（+ 可选的 data 区）
handler 先置 NULL（由调用方随后填）
头插到 cleanup 链
返回节点指针给调用方
```

销毁池子（`ngx_destroy_pool`）：

```
1. 顺序遍历 cleanup 链，逐个调用 handler(data)
2. 遍历 large 链，ngx_free 每个 alloc
3. 遍历 block 链，ngx_free 每个块
```

注意第 1 步遍历顺序：因为注册是**头插**，cleanup 链的顺序与注册顺序**相反**，所以销毁时回调按「后注册的先执行」——这是 LIFO（后进先出），与 C++ 析构函数「后构造的先析构」语义一致，天然适合资源依赖（比如先关连接，再释放连接用到的缓冲）。

#### 4.3.3 源码精读

注册逻辑 [src/core/ngx_palloc.c:311-339](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_palloc.c#L311-L339)：

```c
ngx_pool_cleanup_t *
ngx_pool_cleanup_add(ngx_pool_t *p, size_t size)
{
    ngx_pool_cleanup_t  *c;

    c = ngx_palloc(p, sizeof(ngx_pool_cleanup_t));   // 节点本身来自本池
    if (c == NULL) {
        return NULL;
    }

    if (size) {                              // 调用方可附带一块 data 区
        c->data = ngx_palloc(p, size);
        if (c->data == NULL) {
            return NULL;
        }
    } else {
        c->data = NULL;
    }

    c->handler = NULL;                       // 关键：先置空，由调用方填
    c->next = p->cleanup;
    p->cleanup = c;                          // 头插

    return c;
}
```

调用方拿到 `c` 后，必须自己写 `c->handler = my_cleanup;`（必要时再填 `c->data`）。一个真实例子见 [src/event/quic/ngx_event_quic_streams.c:802](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/quic/ngx_event_quic_streams.c#L802)：QUIC 在新建流的池子上注册 cleanup，以便流销毁时关闭对应连接。

销毁逻辑 [src/core/ngx_palloc.c:46-96](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_palloc.c#L46-L96)，三段循环：

```c
// 第 1 段：跑 cleanup 回调（注意在释放内存之前）
for (c = pool->cleanup; c; c = c->next) {
    if (c->handler) {
        ngx_log_debug1(NGX_LOG_DEBUG_ALLOC, pool->log, 0, "run cleanup: %p", c);
        c->handler(c->data);
    }
}
// 第 2 段：释放所有 large 大块
for (l = pool->large; l; l = l->next) {
    if (l->alloc) {
        ngx_free(l->alloc);
    }
}
// 第 3 段：释放所有 block（含第一个块）
for (p = pool, n = pool->d.next; /* void */; p = n, n = n->d.next) {
    ngx_free(p);
    if (n == NULL) {
        break;
    }
}
```

顺序很重要：**先回调（可能还要用到 data 区），再释放大块，最后释放小块**。因为 cleanup 的 `data` 往往就分配在池子的 small 区，若先释放小块，回调里访问 `data` 就是 use-after-free。

nginx 还内置了两个常用 cleanup 回调：`ngx_pool_cleanup_file`（关文件描述符，[src/core/ngx_palloc.c:363-375](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_palloc.c#L363-L375)）和 `ngx_pool_delete_file`（删文件再关 fd，[src/core/ngx_palloc.c:378-401](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_palloc.c#L378-L401)），配合 `ngx_pool_cleanup_file_t`（[src/core/ngx_palloc.h:68-72](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_palloc.h#L68-L72)）使用，专门处理「打开过的文件随请求关闭」。

#### 4.3.4 代码实践

**实践类型：源码阅读。**

1. **实践目标**：把 cleanup 的「注册 → 调用方填 handler → 销毁时触发」三步在真实源码里走一遍。
2. **操作步骤**：
   - 读 [src/core/ngx_palloc.c:311-339](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_palloc.c#L311-L339) 的注册函数，注意它返回的节点 `handler` 还是 `NULL`。
   - 读 [src/event/quic/ngx_event_quic_streams.c:802](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/quic/ngx_event_quic_streams.c#L802) 附近数行，看调用方拿到 `cln` 后如何设置 `cln->handler = ...`。
   - 读 [src/core/ngx_palloc.c:53-59](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_palloc.c#L53-L59) 的销毁回调循环。
3. **需要观察的现象**：注册时 handler 留空，由调用方负责填；销毁时只对 `handler != NULL` 的节点调用回调。
4. **预期结果**：能说清「为什么 `ngx_pool_cleanup_add` 不直接接收回调函数参数」——把「分配节点」和「设定回调」解耦，让调用方有更多自由（例如先分配节点，再决定用哪个 handler）。

#### 4.3.5 小练习与答案

- **练习 1**：若按顺序注册了 cleanup A、B、C，销毁时它们的执行顺序是什么？
  - **参考答案**：注册是头插，链顺序为 C→B→A；销毁从头遍历，故执行顺序是 **C、B、A**（与注册顺序相反，LIFO）。
- **练习 2**：`ngx_reset_pool`（[src/core/ngx_palloc.c:99-119](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_palloc.c#L99-L119)）与 `ngx_destroy_pool` 有何不同？它会跑 cleanup 吗？
  - **参考答案**：`ngx_reset_pool` **不释放**池子本身，而是释放所有 large 大块、把每个 block 的 `last` 重置回头部、清空 large 链，让池子「回到刚创建时的空状态」以便复用。它**不跑 cleanup**——所以 cleanup 注册的资源生命周期与「池子存在」绑定，而非与「池子内容」绑定；如果你 reset 一个挂了 cleanup 的池子，那些回调永远不会被触发（这是一个潜在陷阱，使用 reset 时要确保没有待清理的非内存资源）。

## 5. 综合实践

设计一个**可独立编译运行**的最小程序，用纯 C 模拟 nginx 内存池的三大机制：small 区的 bump 分配、large 链登记、cleanup 链的 LIFO 回收。它不依赖 nginx 源码树，目的是让你**亲眼看到**指针前移和回调顺序。

> 以下为**示例代码**（非 nginx 原有代码），只复刻核心思想，便于观察行为。

```c
/* 示例代码：mini pool —— 演示 nginx 内存池的核心思想 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define BLOCK_SIZE 256                    /* 模拟一个很小的池子，方便观察 */

typedef void (*cleanup_pt)(void *data);

typedef struct cleanup_node {
    cleanup_pt            handler;
    void                 *data;
    struct cleanup_node  *next;
} cleanup_node;

typedef struct large_node {
    struct large_node *next;
    void              *alloc;
} large_node;

typedef struct mini_pool {
    unsigned char *last;            /* small 区空闲起点 */
    unsigned char *end;             /* 本块末尾 */
    size_t         max;             /* 小块上限 */
    large_node    *large;
    cleanup_node  *cleanup;
    unsigned char  buf[BLOCK_SIZE]; /* 真实 nginx 是向 OS 申请，这里用数组简化 */
} mini_pool;

static mini_pool *pool_create(void) {
    mini_pool *p = calloc(1, sizeof(mini_pool));
    p->last = p->buf;
    p->end  = p->buf + BLOCK_SIZE;
    /* 留一点头部开销，模拟 sizeof(ngx_pool_t) */
    p->max  = BLOCK_SIZE - 64;
    return p;
}

/* small 路径：指针前移 */
static void *palloc_small(mini_pool *p, size_t size) {
    if ((size_t)(p->end - p->last) < size) {
        printf("[small] 空间不足（演示里直接失败，真实 nginx 会开新块）\n");
        return NULL;
    }
    void *m = p->last;
    p->last += size;                /* 关键：bump 分配 */
    return m;
}

/* large 路径：单独 malloc + 登记到 large 链 */
static void *palloc_large(mini_pool *p, size_t size) {
    void *m = malloc(size);
    large_node *n = palloc_small(p, sizeof(large_node)); /* 节点本身来自 small 区 */
    n->alloc = m;
    n->next  = p->large;            /* 头插 */
    p->large = n;
    return m;
}

static void *palloc(mini_pool *p, size_t size) {
    return (size <= p->max) ? palloc_small(p, size) : palloc_large(p, size);
}

/* 注册 cleanup（头插，故销毁时 LIFO） */
static cleanup_node *cleanup_add(mini_pool *p) {
    cleanup_node *c = palloc_small(p, sizeof(cleanup_node));
    c->handler = NULL;
    c->next    = p->cleanup;        /* 头插 */
    p->cleanup = c;
    return c;
}

static void pool_destroy(mini_pool *p) {
    /* 1. 跑 cleanup（按链顺序，即注册的逆序） */
    for (cleanup_node *c = p->cleanup; c; c = c->next) {
        if (c->handler) c->handler(c->data);
    }
    /* 2. 释放 large 大块 */
    for (large_node *l = p->large; l; l = l->next) {
        printf("[large] free %p\n", l->alloc);
        free(l->alloc);
    }
    /* 3. 释放池子本身 */
    free(p);
}

/* 三个演示回调 */
static void close_a(void *d) { printf("cleanup A 执行（最先注册）\n"); }
static void close_b(void *d) { printf("cleanup B 执行\n"); }
static void close_c(void *d) { printf("cleanup C 执行（最后注册，应最先执行）\n"); }

int main(void) {
    mini_pool *p = pool_create();

    void *o1 = palloc(p, 32);   /* small */
    void *o2 = palloc(p, 16);   /* small，last 继续前移 */
    void *big = palloc(p, 1000);/* > max → large，单独 malloc */
    printf("o1=%p o2=%p (small, bump) ; big=%p (large)\n", o1, o2, big);

    cleanup_node *a = cleanup_add(p); a->handler = close_a;
    cleanup_node *b = cleanup_add(p); b->handler = close_b;
    cleanup_node *c = cleanup_add(p); c->handler = close_c;

    printf("---- destroy ----\n");
    pool_destroy(p);
    return 0;
}
```

**实践步骤与目标：**

1. 把上面程序存为 `mini_pool.c`，用 `gcc -o mini_pool mini_pool.c` 编译并运行。
2. **需要观察的现象**：
   - `o1` 与 `o2` 的地址非常接近（相差约 32 字节），体现 small 的 bump 分配。
   - `big` 走 large 路径，其地址与 `o1/o2` 完全不在一个区间（来自独立 malloc）。
   - destroy 时，cleanup 的执行顺序是 **C、B、A**（与注册顺序相反），验证 LIFO；large 大块被释放。
3. **预期结果**：输出大致如下（地址因运行而异）：
   ```
   o1=0x... o2=0x... (small, bump) ; big=0x... (large)
   ---- destroy ----
   cleanup C 执行（最后注册，应最先执行）
   cleanup B 执行
   cleanup A 执行（最先注册）
   [large] free 0x...
   ```
4. **进阶**：把 `palloc(p, 32)` 改成循环分配 10 次，观察 small 区何时「空间不足」（真实 nginx 此时调 `ngx_palloc_block` 开新块，本示例简化为失败）；再尝试注册一个 cleanup 但**不设 handler**，验证销毁时会跳过它（对应 `if (c->handler)` 判空）。
5. 编译运行的具体输出「待本地验证」，但「small 地址连续、large 地址独立、cleanup 按 C→B→A 触发」这三点由源码逻辑保证。

## 6. 本讲小结

- nginx 用**内存池** `ngx_pool_t` 同时解决碎片与生命周期两大难题：小对象 bump 分配、整池一次性回收。
- 一个池子内部挂三条链：**block 链**（小块竞技场）、**large 链**（大块登记）、**cleanup 链**（资源回收回调）。
- `ngx_create_pool` 申请整块内存并算出 `max = min(可用空间, 页大小-1)`，作为大小块的分水岭。
- `ngx_palloc` 按 `size ≤ max` 分流：**small 路径**是 O(1) 指针前移（`current` + `failed>4` 优化跳过满块），**large 路径**单独 `malloc` 并把节点登记到 large 链，且节点本身也来自 small 区。
- cleanup 链**头插**、销毁时**顺序遍历**，因此回调按 **LIFO（后注册先执行）** 触发；销毁顺序固定为「跑回调 → 释放 large → 释放 block」，保证回调能安全访问 small 区的 data。
- 小块**不支持**单独 `free`，`ngx_pfree` 只对 large 生效；小块靠整池销毁回收。

## 7. 下一步学习建议

内存池是后续所有讲义的「地基」。建议接下来：

- **u2-l2（字符串与数值解析）**：看 `ngx_str_t` 等结构如何用 `ngx_palloc` 分配，体会「字符串存活于请求池」的用法。
- **u2-l3（容器）**：`ngx_array` / `ngx_list` / `ngx_hash` 全部构建在内存池上（`create` 函数第一个参数就是 `ngx_pool_t *`），届时你会再次看到 small/large 分配的实际调用。
- **u2-l4（buf 与 output_chain）**：池子的 `chain` 字段（本讲略过）在那里用于回收 `ngx_chain_t` 节点，是理解输出链的关键。
- 回到上层：**u6-l2（HTTP 请求生命周期）**会展示一个请求如何 `ngx_create_pool(request_pool_size)`、处理过程中不断 `ngx_palloc`、请求结束时 `ngx_destroy_pool` 一次性清理——那时本讲的所有机制都会在一个真实请求里串起来。
